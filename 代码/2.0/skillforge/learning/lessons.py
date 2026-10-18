from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from ..core import config as core_config
from ..core.clock import utc_now
from ..core.errors import InvalidInput, InvalidState, MissingDependency
from ..core.ids import canonical_hash, new_id
from ..platform.budget import BudgetSpec, spec_from_budget
from . import checks
from .feedback import conflict_feedback_ids

#: 归因枚举
ATTRIBUTIONS = (
    "skill_gap",
    "skill_conflict",
    "model_noncompliance",
    "tool_failure",
    "environment_change",
    "input_gap",
    "unknown",
)
#: 只有这两类归因可以进入候选；其余只给“调整模型 / 工具 / 输入 / 环境”的建议
CANDIDATE_ATTRIBUTIONS = ("skill_gap", "skill_conflict")
#: 忽略经验的原因枚举
IGNORE_REASONS = ("only_this_time", "wrong_attribution", "wrong_rule", "duplicate", "not_worth_cost")
LESSON_STATUSES = ("draft", "proposed", "ignored", "rejected", "activated", "stale")

#: 学习作业：类型前缀 ``learning.`` 使用独立 worker 槽位；优先级低于任务执行（默认 100）
LEARNING_JOB_TYPE = "learning.extract"
LEARNING_JOB_PRIORITY = 500
#: 单次提炼最多 3 次模型调用（首次 + 2 次修复），仍受学习预算上限约束
MAX_EXTRACT_ATTEMPTS = 3
#: 单文件注入材料的字符上限（原始日志另存，不整段塞进上下文）
MAX_FILE_CHARS = 4_000
MAX_FEEDBACK_CHARS = 2_000

_PAUSED_KEY = getattr(core_config, "LEARNING_PAUSED_KEY", None) or core_config.SETTING_LEARNING_PAUSED
_PAUSED_SKILLS_KEY = core_config.SETTING_LEARNING_PAUSED_SKILLS

_FIELD_SPECS = (
    ("condition_text", ("condition", "condition_text", "适用条件", "条件")),
    ("rule_text", ("rule", "rule_text", "操作", "学到什么", "rule_text_summary")),
    ("rationale_text", ("rationale", "reason", "rationale_text", "理由", "为什么")),
    ("verification_text", ("verification", "verify", "verification_text", "验证方法", "验证")),
    ("boundary_text", ("boundary", "counterexample", "boundary_text", "反例边界", "反例")),
)


class LessonService:
    """经验提炼服务（worker handler 为 :meth:`extract`）。"""

    def __init__(
        self,
        store,
        repo,
        events,
        queue,
        model_factory,
        budget,
        skills,
        idem,
        config=None,
    ) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.queue = queue
        self.model_factory = model_factory
        self.budget = budget
        self.skills = skills
        self.idem = idem
        self.config = config

    # ==================================================================== 学习开关
    def is_learning_enabled(self, skill_id=None) -> bool:
        """全局或单 skill 的学习是否开启（关闭学习**不影响**任务执行）。"""
        if core_config.read_json_setting(self.store, _PAUSED_KEY, False):
            return False
        if skill_id:
            paused = core_config.read_json_setting(self.store, _PAUSED_SKILLS_KEY, []) or []
            if skill_id in paused:
                return False
        return True

    def set_learning_paused(self, paused: bool, *, skill_id=None) -> dict:
        """暂停 / 恢复学习；``skill_id`` 为空表示全局。只写 ``setting``，不动任务与既有队列。"""
        if skill_id is None:
            core_config.write_json_setting(self.store, _PAUSED_KEY, bool(paused))
            action = "learning.pause" if paused else "learning.resume"
            self.events.audit(action, "setting", _PAUSED_KEY, after={"paused": bool(paused)})
        else:
            if self.repo.get("skill", skill_id) is None:
                raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
            paused_skills = list(core_config.read_json_setting(self.store, _PAUSED_SKILLS_KEY, []) or [])
            if paused and skill_id not in paused_skills:
                paused_skills.append(skill_id)
            if not paused and skill_id in paused_skills:
                paused_skills.remove(skill_id)
            core_config.write_json_setting(self.store, _PAUSED_SKILLS_KEY, paused_skills)
            self.events.audit(
                "learning.pause" if paused else "learning.resume",
                "skill",
                skill_id,
                after={"paused": bool(paused)},
            )
        data = self.learning_settings()
        # 暂停不自动丢弃证据：把可取消的既有学习作业暴露给调用方
        data["cancellable_job_ids"] = self._queued_learning_jobs(skill_id) if paused else []
        return data

    def _queued_learning_jobs(self, skill_id=None) -> list[str]:
        if self.queue is None:
            return []
        jobs = self.queue.list(status="queued", type=LEARNING_JOB_TYPE, limit=500)
        if skill_id:
            jobs = [job for job in jobs if (job.payload or {}).get("target_skill_id") in (skill_id, None)]
        return [job.id for job in jobs]

    def learning_settings(self) -> dict:
        paused_skills = list(core_config.read_json_setting(self.store, _PAUSED_SKILLS_KEY, []) or [])
        return {
            "paused": bool(core_config.read_json_setting(self.store, _PAUSED_KEY, False)),
            "paused_skills": paused_skills,
            "auto_activate": bool(core_config.read_json_setting(self.store, core_config.SETTING_AUTO_ACTIVATE, False)),
            "budget": dict(core_config.DEFAULT_LEARNING_BUDGET),
            "note": "暂停学习只影响后台提炼；任务执行与验收不受影响",
        }

    # ==================================================================== 入队
    def enqueue_from_feedback(self, feedback_id, *, idempotency_key=None) -> str | None:
        """按事件指纹 + 基线去重入队学习作业；返回 ``None`` 表示不学习。"""
        row = self.repo.get("feedback", feedback_id)
        if row is None:
            raise InvalidInput(f"反馈不存在: {feedback_id}", field_errors={"feedback_id": "not_found"})
        if row["status"] != "active":
            return None
        if self.queue is None:
            raise MissingDependency("作业队列不可用，无法入队学习作业")
        skill_id = row["target_skill_id"]
        if not self.is_learning_enabled(skill_id):
            return None
        run = self.repo.get("run", row["run_id"]) if row["run_id"] else None
        if run is not None and not int(run["learning_enabled"] or 0):
            return None

        candidates = self.store.query(
            "SELECT * FROM feedback WHERE status = 'active' AND target_skill_id IS ? "
            "ORDER BY created_at ASC, rowid ASC LIMIT 500",
            (skill_id,),
        )
        conflicts = conflict_feedback_ids(candidates)
        if feedback_id in conflicts:
            self.events.product_event(
                "learning.blocked_by_conflict",
                {"skill_id": skill_id or "", "scope": row["scope"]},
                event_id="conflict:" + feedback_id,
            )
            return None

        fingerprint = self._event_fingerprint(row)
        grouped = [
            item["id"] for item in candidates if self._event_fingerprint(item) == fingerprint
        ] or [feedback_id]
        payload = {
            "feedback_ids": grouped,
            "fingerprint": fingerprint,
            "target_skill_id": skill_id,
            "baseline_version_id": row["target_version_id"],
            "run_id": row["run_id"],
        }
        baseline = dict(core_config.DEFAULT_LEARNING_BUDGET)

        def _enqueue() -> dict:
            job_id = self.queue.enqueue(
                LEARNING_JOB_TYPE,
                payload,
                idempotency_key=f"{LEARNING_JOB_TYPE}:{fingerprint}",
                budget=baseline,
                priority=LEARNING_JOB_PRIORITY,
                run_id=row["run_id"],
            )
            self.events.audit(
                "learning.enqueued",
                "job",
                job_id,
                after={
                    "fingerprint": fingerprint,
                    "feedback_count": len(grouped),
                    "baseline_version_id": row["target_version_id"],
                    "priority": LEARNING_JOB_PRIORITY,
                },
            )
            self.events.product_event(
                "learning.enqueued",
                {"feedback_count": len(grouped)},
                event_id=f"learning.enqueue:{fingerprint}",
            )
            return {"job_id": job_id}

        if idempotency_key:
            return self.idem.run(
                "lesson.enqueue", idempotency_key, {"feedback_id": feedback_id}, _enqueue
            )["job_id"]
        return _enqueue()["job_id"]

    # ==================================================================== 提炼
    def extract(self, feedback_ids: list[str], *, job_id=None) -> list[dict]:
        """worker handler：调用模型提炼经验，落 lesson + lesson_source。

        既可直接传 ``feedback_ids``，也可把 :class:`skillforge.platform.queue.Job` 当作唯一
        位置参数传入（此时从 ``payload['feedback_ids']`` 取输入、``job.id`` 作为 ``job_id``），
        这样它可以被直接注册为 ``learning.extract`` 的 handler。
        """
        job = None
        if hasattr(feedback_ids, "payload") and hasattr(feedback_ids, "id"):
            job = feedback_ids
            feedback_ids = list(job.payload.get("feedback_ids") or [])
            job_id = job_id or job.id
        feedbacks = []
        for feedback_id in feedback_ids or []:
            row = self.repo.get("feedback", feedback_id)
            if row is not None and row["status"] == "active":
                feedbacks.append(row)
        if not feedbacks:
            return []
        skill_id = next((row["target_skill_id"] for row in feedbacks if row["target_skill_id"]), None)
        if not self.is_learning_enabled(skill_id):
            return []

        fingerprint = self._event_fingerprint(feedbacks[0])
        scope_id = job_id or f"learning_job:{fingerprint}"
        spec = self._budget_spec(job_id)
        self.budget.open(scope_id, spec, estimated_tokens=2_000)

        materials, filtered = self._build_materials(feedbacks)
        prompts = self._extract_messages(materials)
        lesson_stats: dict[str, dict] = {}
        budget_exceeded = False
        pending: list[dict] = []
        calls = 0
        for attempt in range(1, MAX_EXTRACT_ATTEMPTS + 1):
            text, meta = self._call_model(scope_id, prompts)
            if meta.get("reason") == "budget_exceeded":
                budget_exceeded = True
                break
            if text is None:
                break
            calls += 1
            parsed = checks.extract_json_object(text)
            items = _lesson_items(parsed)
            touched, pending = self._persist_lessons(items, feedbacks, job_id=job_id)
            for lesson_id, is_new in touched:
                stats = lesson_stats.setdefault(lesson_id, {"created": is_new, "merged": not is_new})
                stats["merged"] = stats["merged"] or not is_new
            if not pending or attempt >= MAX_EXTRACT_ATTEMPTS:
                break
            prompts = self._repair_messages(materials, pending)

        lesson_ids = list(lesson_stats)
        if budget_exceeded:
            now = utc_now()
            with self.store.tx() as conn:
                for lesson_id in lesson_ids:
                    conn.execute(
                        "UPDATE lesson SET reason_code = COALESCE(reason_code, 'budget_exceeded'), "
                        "updated_at = ? WHERE id = ?",
                        (now, lesson_id),
                    )
            if job_id and self.queue is not None:
                self.queue.progress(
                    job_id,
                    {
                        "incomplete": True,
                        "reason": "budget_exceeded",
                        "lesson_ids": lesson_ids,
                        "feedback_ids": [row["id"] for row in feedbacks],
                    },
                )
        self.events.audit(
            "learning.extract",
            "job" if job_id else "feedback",
            job_id or fingerprint,
            after={
                "feedback_count": len(feedbacks),
                "lesson_count": len(lesson_ids),
                "model_calls": calls,
                "budget_exceeded": budget_exceeded,
                "filtered_materials": filtered,
                "pending_items": len(pending),
            },
        )
        self.events.product_event(
            "learning.extracted",
            {
                "lesson_count": len(lesson_ids),
                "budget_exceeded": budget_exceeded,
                "incomplete": budget_exceeded,
            },
            event_id="learning.extract:" + canonical_hash({"scope_id": scope_id, "n": len(lesson_ids)}),
        )
        result = []
        for lesson_id in lesson_ids:
            data = self.get_lesson(lesson_id)
            data["created"] = bool(lesson_stats[lesson_id]["created"])
            data["merged"] = bool(lesson_stats[lesson_id]["merged"])
            data["incomplete"] = budget_exceeded
            result.append(data)
        return result

    # ==================================================================== 忽略 / 查询
    def ignore(self, lesson_id, *, reason_code, actor: str = "owner") -> dict:
        """忽略一条经验（原因枚举），并保证相同证据不重复提示。"""
        if reason_code not in IGNORE_REASONS:
            raise InvalidInput(
                f"不支持的忽略原因: {reason_code}", field_errors={"reason_code": "unsupported"}
            )
        row = self.repo.get("lesson", lesson_id)
        if row is None:
            raise InvalidInput(f"经验不存在: {lesson_id}", field_errors={"lesson_id": "not_found"})
        if row["status"] == "ignored":
            data = self.get_lesson(lesson_id)
            data["already_ignored"] = True
            return data
        if row["status"] == "activated":
            raise InvalidState("已随候选启用的经验不能忽略", field_errors={"status": "activated"})
        with self.store.tx() as conn:
            self.repo.update(
                "lesson",
                lesson_id,
                {"status": "ignored", "reason_code": reason_code, "updated_at": utc_now()},
                conn=conn,
            )
            self.events.audit(
                "lesson.ignore",
                "lesson",
                lesson_id,
                before={"status": row["status"]},
                after={"status": "ignored", "reason_code": reason_code},
                actor=actor,
                conn=conn,
            )
        self.events.product_event(
            "lesson.ignored",
            {"reason_code": reason_code, "attribution": row["attribution"]},
            event_id="lesson.ignore:" + row["fingerprint"],
        )
        data = self.get_lesson(lesson_id)
        data["already_ignored"] = False
        return data

    def list_lessons(self, *, skill_id=None, status=None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM lesson"
        clauses: list[str] = []
        params: list = []
        if skill_id:
            clauses.append("target_skill_id = ?")
            params.append(skill_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        return [self._lesson_dict(row) for row in self.store.query(sql, tuple(params))]

    def get_lesson(self, lesson_id) -> dict:
        row = self.repo.get("lesson", lesson_id)
        if row is None:
            raise InvalidInput(f"经验不存在: {lesson_id}", field_errors={"lesson_id": "not_found"})
        data = self._lesson_dict(row)
        data["sources"] = self._sources(lesson_id)
        return data

    # ==================================================================== 内部：材料
    def _budget_spec(self, job_id) -> BudgetSpec:
        budget = None
        if job_id:
            try:
                budget = self.queue.get(job_id).budget if self.queue is not None else None
            except Exception:  # pragma: no cover - 作业已被清理
                budget = None
        return spec_from_budget(budget, core_config.DEFAULT_LEARNING_BUDGET)

    def _build_materials(self, feedbacks: list) -> tuple[dict, list[dict]]:
        """只读被授权来源：反馈原文 + 实际加载（load_status='loaded'）的技能文件。

        生成阶段禁止访问 holdout 夹具与预期答案，统一经 ``checks.sanitize_materials`` 过滤。
        """
        run_ids = [row["run_id"] for row in feedbacks if row["run_id"]]
        loaded: list[dict] = []
        run_info: list[dict] = []
        for run_id in dict.fromkeys(run_ids):
            run = self.repo.get("run", run_id)
            if run is not None:
                run_info.append(
                    {
                        "run_id": run_id,
                        "request": _clip(run["request"], 1_200),
                        "execution_status": run["execution_status"],
                        "outcome": run["outcome"],
                        "completion_criteria": _json(run["completion_criteria"], []),
                    }
                )
            rows = self.store.query(
                "SELECT skill_id, version_id, file_path, content_hash, chars FROM skill_load "
                "WHERE run_id = ? AND load_status = 'loaded' ORDER BY created_at ASC LIMIT 50",
                (run_id,),
            )
            for row in rows:
                loaded.append(
                    {
                        "run_id": run_id,
                        "skill_id": row["skill_id"],
                        "version_id": row["version_id"],
                        "file_path": row["file_path"],
                        "chars": int(row["chars"] or 0),
                        "content": self._blob_text(row["content_hash"]),
                    }
                )
        materials = {
            "task_context": run_info,
            "loaded_skill_files": loaded,
            "feedback": [
                {
                    "id": row["id"],
                    "run_id": row["run_id"],
                    "type": row["type"],
                    "source_kind": row["source_kind"],
                    "author": row["author"],
                    "scope": row["scope"],
                    "verdict": row["verdict"],
                    "content": _clip(row["content"], MAX_FEEDBACK_CHARS),
                    "locator": _json(row["locator"], {}),
                }
                for row in feedbacks
            ],
        }
        clean, filtered = checks.sanitize_materials(materials)
        return clean, filtered

    def _blob_text(self, content_hash: str) -> str:
        blobs = getattr(self.skills, "blobs", None)
        if blobs is None or not content_hash:
            return ""
        try:
            if not blobs.exists(content_hash):
                return ""
            return _clip(blobs.get_text(content_hash), MAX_FILE_CHARS)
        except Exception:  # pragma: no cover - 制品缺失时材料降级
            return ""

    def _extract_messages(self, materials: dict) -> list:
        from ..platform.model import ChatMessage

        system = (
            "你是技能改进助手。只根据给定材料提炼可复用经验，不要发明来源，不要改写用户原话。"
            "输出严格 JSON，不要输出多余解释。"
        )
        user = (
            "下面是本次学习事件允许你读取的全部材料（已过滤隐藏样例与预期答案）：\n"
            + json.dumps(materials, ensure_ascii=False)
            + "\n\n请提炼经验，返回 JSON："
            '{"lessons":[{"attribution":"skill_gap|skill_conflict|model_noncompliance|tool_failure|'
            'environment_change|input_gap|unknown","condition":"适用条件","rule":"操作规则",'
            '"rationale":"理由","verification":"验证方法","boundary":"反例边界",'
            '"applicability":"何时用/何时不用","scope":"task|workspace|personal",'
            '"feedback_ids":["来源反馈 id"]}]}。'
            "每条经验必须同时给出条件、操作、理由、验证方法与反例边界，并至少引用一条来源反馈；"
            "没有任何可定位来源的通用“最佳实践”不要输出。"
        )
        return [ChatMessage("system", system), ChatMessage("user", user)]

    def _repair_messages(self, materials: dict, pending: list[dict]) -> list:
        from ..platform.model import ChatMessage

        problems = [
            {"index": item.get("index"), "missing": item.get("missing"), "rule": _clip(str(item.get("rule") or ""), 200)}
            for item in pending[:5]
        ]
        user = (
            "你上一次输出的以下经验缺少必需字段或来源，请补齐后只返回修正后的 JSON（同样的 schema）：\n"
            + json.dumps(problems, ensure_ascii=False)
            + "\n材料如下：\n"
            + json.dumps(materials, ensure_ascii=False)
        )
        return [ChatMessage("system", "你是技能改进助手。只补齐缺失字段，不改变已提供的来源。"), ChatMessage("user", user)]

    def _call_model(self, scope_id: str, messages: list) -> tuple[str | None, dict]:
        client = self.model_factory() if callable(self.model_factory) else self.model_factory
        if client is None:
            return None, {"reason": "model_not_configured"}
        if not self.budget.reserve_call(scope_id, 2_000):
            return None, {"reason": "budget_exceeded"}
        try:
            reply = client.complete(messages, max_output_tokens=1_500)
        except Exception as exc:
            self.budget.release_call(scope_id, 2_000)
            self.events.audit(
                "learning.model_error", "job", scope_id, after={"message": _clip(str(exc), 300)}
            )
            return None, {"reason": "model_error", "message": str(exc)}
        usage = reply.usage or {}
        tokens = int(usage.get("total_tokens") or 0)
        self.budget.consume(scope_id, tokens=tokens, calls=1, usage_known=bool(reply.usage_known))
        return reply.text or "", {"ok": True, "tokens": tokens}

    # ==================================================================== 内部：落库
    def _persist_lessons(self, items: list, feedbacks: list, *, job_id) -> tuple[list[tuple[str, bool]], list[dict]]:
        by_id = {row["id"]: row for row in feedbacks}
        touched: list[tuple[str, bool]] = []
        pending: list[dict] = []
        for index, raw in enumerate(items):
            if not isinstance(raw, Mapping):
                continue
            normalized, problem = self._normalize_lesson(raw, by_id)
            if normalized is None:
                if problem and problem.get("retryable"):
                    problem["index"] = index
                    pending.append(problem)
                continue
            lesson_id, is_new = self._upsert_lesson(normalized, job_id=job_id)
            touched.append((lesson_id, is_new))
        return touched, pending

    def _normalize_lesson(self, raw: Mapping, by_id: Mapping[str, Any]) -> tuple[dict | None, dict | None]:
        fields: dict[str, str] = {}
        missing: list[str] = []
        for target, aliases in _FIELD_SPECS:
            value = _first(raw, aliases)
            text = str(value).strip() if value is not None else ""
            fields[target] = text
            if not text:
                missing.append(target)
        attribution = str(_first(raw, ("attribution", "归因", "cause")) or "unknown").strip().lower()
        if attribution not in ATTRIBUTIONS:
            attribution = "unknown"
        rule = fields["rule_text"]
        if missing:
            return None, {
                "retryable": True,
                "missing": missing,
                "rule": rule,
                "reason": "missing_required_fields",
            }

        # 来源：模型没写或写空时，默认引用本批次被授权的反馈（它们本身就是证据来源）；
        # 引用了不存在的 feedback_id 视为无来源，不生成。
        requested_sources = _first_present(raw, ("feedback_ids", "sources", "source_ids"))
        if requested_sources is _MISSING or requested_sources in ([], (), "", None):
            sources = list(by_id.values())
        else:
            if isinstance(requested_sources, str):
                requested_sources = [requested_sources]
            items = list(requested_sources) if isinstance(requested_sources, (list, tuple)) else []
            sources = [by_id[item] for item in items if item in by_id]
            if not sources:
                return None, {"retryable": False, "reason": "unknown_source"}
        if not sources:
            return None, {"retryable": False, "reason": "no_source"}

        # 纯模型观察必须有可定位证据，且标记未获用户确认
        if all(row["source_kind"] == "model" for row in sources):
            if not any(_has_evidence(_json(row["locator"], {})) for row in sources):
                return None, {"retryable": False, "reason": "no_locatable_evidence"}

        declared_scope = str(_first(raw, ("scope", "范围")) or "task").strip().lower()
        if declared_scope not in ("task", "workspace", "personal"):
            declared_scope = "task"
        scope = _clamp_scope(declared_scope, sources)

        target_skills = [row["target_skill_id"] for row in sources if row["target_skill_id"]]
        target_skill_id = target_skills[0] if target_skills else None
        target_version_ids = [row["target_version_id"] for row in sources if row["target_version_id"]]
        target_version_id = target_version_ids[0] if target_version_ids else None

        evidence = _evidence_strength(sources)
        fingerprint = checks.lesson_fingerprint(
            target_skill_id=target_skill_id,
            attribution=attribution,
            rule_text=rule,
            condition_text=fields["condition_text"],
        )
        return (
            {
                "target_skill_id": target_skill_id,
                "target_version_id": target_version_id,
                "attribution": attribution,
                "applicability": str(_first(raw, ("applicability", "适用性", "何时使用")) or fields["condition_text"]).strip(),
                "scope": scope,
                "fingerprint": fingerprint,
                "evidence_strength": evidence,
                "sources": sources,
                **fields,
            },
            None,
        )

    def _upsert_lesson(self, normalized: Mapping, *, job_id) -> tuple[str, bool]:
        fingerprint = normalized["fingerprint"]
        existing = self.store.query_one("SELECT * FROM lesson WHERE fingerprint = ?", (fingerprint,))
        now = utc_now()
        workspace_id = None
        if normalized["target_skill_id"]:
            skill = self.repo.get("skill", normalized["target_skill_id"])
            if skill is not None and skill["scope"] == "workspace":
                workspace_id = skill["workspace_id"]
        if existing is None:
            lesson_id = new_id("lesson")
            row = {
                "id": lesson_id,
                "profile_id": "local",
                "target_skill_id": normalized["target_skill_id"],
                "target_version_id": normalized["target_version_id"],
                "attribution": normalized["attribution"],
                "applicability": normalized["applicability"],
                "scope": normalized["scope"],
                "workspace_id": workspace_id,
                "rule_text": normalized["rule_text"],
                "condition_text": normalized["condition_text"],
                "rationale_text": normalized["rationale_text"],
                "verification_text": normalized["verification_text"],
                "boundary_text": normalized["boundary_text"],
                "evidence_strength": normalized["evidence_strength"],
                "status": "draft",
                "reason_code": None,
                "fingerprint": fingerprint,
                "source_count": 0,
                "job_id": job_id,
                "created_at": now,
                "updated_at": now,
            }
            is_new = True
        else:
            lesson_id = existing["id"]
            is_new = False
            row = None
        with self.store.tx() as conn:
            if is_new:
                self.repo.insert("lesson", row, conn=conn)
                self.events.audit(
                    "lesson.create",
                    "lesson",
                    lesson_id,
                    after={
                        "attribution": normalized["attribution"],
                        "scope": normalized["scope"],
                        "evidence_strength": normalized["evidence_strength"],
                        "target_skill_id": normalized["target_skill_id"],
                        "needs_attribution": normalized["target_skill_id"] is None,
                    },
                    conn=conn,
                )
            else:
                # 同一指纹只保留一条经验；被撤回过的经验在有新来源时回到 draft。
                conn.execute(
                    "UPDATE lesson SET source_count = source_count, updated_at = ?, "
                    "status = CASE WHEN status = 'stale' THEN 'draft' ELSE status END, "
                    "job_id = COALESCE(job_id, ?) WHERE id = ?",
                    (now, job_id, lesson_id),
                )
            for source in normalized["sources"]:
                conn.execute(
                    "INSERT OR IGNORE INTO lesson_source (lesson_id, feedback_id, evidence_locator) "
                    "VALUES (?, ?, ?)",
                    (
                        lesson_id,
                        source["id"],
                        json.dumps(_json(source["locator"], {}), ensure_ascii=False),
                    ),
                )
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM lesson_source WHERE lesson_id = ?", (lesson_id,)
            ).fetchone()["n"]
            conn.execute(
                "UPDATE lesson SET source_count = ?, updated_at = ? WHERE id = ?",
                (int(count or 0), now, lesson_id),
            )
        return lesson_id, is_new

    # ==================================================================== 内部：读取
    def _lesson_dict(self, row) -> dict:
        data = {key: row[key] for key in row.keys()}
        data["lesson_id"] = data["id"]
        data["needs_attribution"] = data.get("target_skill_id") is None
        data["confirmed_by_user"] = data.get("evidence_strength") == "user_correction"
        data["incomplete"] = data.get("reason_code") == "budget_exceeded"
        data["attribution_advice"] = attribution_advice(data.get("attribution"))
        data["can_become_proposal"] = data.get("attribution") in CANDIDATE_ATTRIBUTIONS
        return data

    def _sources(self, lesson_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT ls.feedback_id, ls.evidence_locator, f.source_kind, f.type, f.scope, f.status, "
            "f.verdict, f.content, f.author "
            "FROM lesson_source ls JOIN feedback f ON f.id = ls.feedback_id "
            "WHERE ls.lesson_id = ? ORDER BY f.created_at ASC",
            (lesson_id,),
        )
        return [
            {
                "feedback_id": row["feedback_id"],
                "source_kind": row["source_kind"],
                "type": row["type"],
                "scope": row["scope"],
                "status": row["status"],
                "verdict": row["verdict"],
                "author": row["author"],
                "content": row["content"],
                "evidence_locator": _json(row["evidence_locator"], {}),
            }
            for row in rows
        ]

    def _event_fingerprint(self, row) -> str:
        return checks.event_fingerprint(
            {
                "kind": "feedback_event",
                "event": row["run_id"] or f"feedback:{row['id']}",
                "target_skill_id": row["target_skill_id"] or "",
                "baseline_version_id": row["target_version_id"] or "",
                "type": row["type"],
                "scope": row["scope"],
                "source_kind": row["source_kind"],
            }
        )


# --------------------------------------------------------------------------- 辅助
def attribution_advice(attribution: str | None) -> dict | None:
    """非 skill 类归因的处理选择：调整模型 / 工具 / 输入 / 环境。"""
    mapping = {
        "model_noncompliance": {
            "category": "model",
            "title": "模型未遵循已有明确步骤",
            "actions": ["检查模型/提示配置与步骤明确性", "优先固化检查点，不要靠重复加“必须”解决"],
            "rewrites_skill": False,
        },
        "tool_failure": {
            "category": "tool",
            "title": "工具或容器能力失败",
            "actions": ["检查工具授权、容器可用性与依赖", "必要时重新配置后重跑"],
            "rewrites_skill": False,
        },
        "input_gap": {
            "category": "input",
            "title": "输入资料不足",
            "actions": ["补充输入或明确完成条件", "必要时在任务中要求澄清"],
            "rewrites_skill": False,
        },
        "environment_change": {
            "category": "environment",
            "title": "环境条件发生变化",
            "actions": ["确认新的稳定环境条件后再考虑修改方法"],
            "rewrites_skill": False,
        },
        "unknown": {
            "category": "unknown",
            "title": "归因不明",
            "actions": ["保留反馈并补充证据", "unknown 不自动改写 skill"],
            "rewrites_skill": False,
        },
        "skill_gap": {
            "category": "skill",
            "title": "技能缺少必要步骤",
            "actions": ["可生成最小改动候选"],
            "rewrites_skill": True,
        },
        "skill_conflict": {
            "category": "skill",
            "title": "技能内部规则冲突",
            "actions": ["可生成候选以消除冲突"],
            "rewrites_skill": True,
        },
    }
    return mapping.get(attribution or "unknown")


def _lesson_items(parsed) -> list:
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, Mapping):
        for key in ("lessons", "items", "data", "result"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, Mapping):
                return [value]
        if any(key in parsed for key in ("rule", "rule_text", "操作")):
            return [parsed]
    return []


def _first(raw: Mapping, keys: Iterable[str]):
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


_MISSING = object()


def _first_present(raw: Mapping, keys: Iterable[str]):
    """与 :func:`_first` 的区别：显式的空列表算“给了值”，``None`` 仍视为未给。"""
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return _MISSING


def _clamp_scope(declared: str, sources: list) -> str:
    """经验范围不能超过来源反馈的事实范围；纯模型观察只能是本次。"""
    from .feedback import SCOPE_RANK

    ranks = [SCOPE_RANK.get(row["scope"], 0) for row in sources if row["source_kind"] in ("user", "objective")]
    allowed = max(ranks) if ranks else 0
    final = min(SCOPE_RANK.get(declared, 0), allowed)
    return ("task", "workspace", "personal")[final]


def _evidence_strength(sources: list) -> str:
    kinds = [row["source_kind"] for row in sources]
    if "user" in kinds:
        return "user_correction"
    objective = kinds.count("objective")
    if objective >= 2:
        return "objective_repeat"
    if objective == 1:
        return "objective_single"
    return "single_inference"


#: 视为“可定位证据”的 locator 字段
_EVIDENCE_KEYS = ("artifact_id", "step_seq", "snippet", "quote", "text", "path", "line", "file")


def _has_evidence(locator) -> bool:
    if not isinstance(locator, Mapping):
        return False
    return any(locator.get(key) not in (None, "", [], {}) for key in _EVIDENCE_KEYS)


def _clip(text, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
