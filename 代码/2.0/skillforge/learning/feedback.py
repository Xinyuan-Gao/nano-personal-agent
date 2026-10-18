from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from ..core.clock import utc_now
from ..core.errors import InvalidInput, InvalidState
from ..core.ids import canonical_hash, new_id

FEEDBACK_TYPES = ("acceptance", "correction", "verifier", "review", "rejection")
SOURCE_KINDS = ("user", "objective", "model")
SCOPES = ("task", "workspace", "personal")
VERDICTS = ("accepted", "partially_accepted", "rejected")
OUTCOMES = ("unknown", "accepted", "rejected", "partially_accepted")
SCOPE_RANK = {"task": 0, "workspace": 1, "personal": 2}
PROFILE_ID = "local"

#: 参与“澄清”判定的反馈状态
_ACTIVE = "active"

#: 明确长期范围的说法（用户原话）：只有用户显式这样说才允许长期范围
_LONG_TERM_RE = re.compile(
    r"(以后|今后|往后|下次|未来|总是|永远|每次都|一律|from now on|always|every time|going forward|in future)",
    re.I,
)
_LONG_TERM_SPECIFIC_RE = re.compile(
    r"(以后同类|同类任务|以后都|今后都|总是|永远|每次都|一律|以后遇到|以后按|"
    r"以后所有|所有项目|所有任务|所有工作区|跨项目|个人长期|"
    r"from now on|always|every time|all projects|every project)",
    re.I,
)
_PERSONAL_RE = re.compile(r"(个人|跨项目|所有项目|全部项目|所有任务|所有工作区|personal|across projects)", re.I)

_POSITIVE_MARKERS = ("必须", "务必", "应该", "保持", "使用", "加上", "增加", "包含", "标注", "优先", "always", "must", "should", "use", "include", "add", "keep")
_NEGATIVE_MARKERS = ("不要", "不得", "禁止", "避免", "不能", "无需", "删掉", "去掉", "别用", "别", "never", "do not", "don't", "avoid", "must not", "remove", "omit", "skip")
_ALL_MARKERS = tuple(
    sorted(set(_POSITIVE_MARKERS + _NEGATIVE_MARKERS + ("任务", "要求", "用户", "必须的")), key=len, reverse=True)
)


class FeedbackService:
    """反馈与验收服务。

    构造签名（ARCHITECTURE §7 未固定构造函数，这里给出被 api 层使用的形式）::

        FeedbackService(store, repo, events, idem, skills=None, config=None)
    """

    def __init__(self, store, repo, events, idem, skills=None, config=None) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.idem = idem
        self.skills = skills
        self.config = config

    # ==================================================================== 提交
    def submit(
        self,
        *,
        run_id,
        type,
        content=None,
        verdict=None,
        scope: str = "task",
        target_skill_id=None,
        target_version_id=None,
        locator=None,
        source_kind: str = "user",
        supersedes_id=None,
        idempotency_key=None,
        profile_id: str = PROFILE_ID,
    ) -> dict:
        """保存一条反馈（原文 + 来源 + 范围 + 定位），并同步必要的派生状态。"""
        payload = {
            "run_id": run_id,
            "type": type,
            "content": content,
            "verdict": verdict,
            "scope": scope,
            "target_skill_id": target_skill_id,
            "target_version_id": target_version_id,
            "locator": locator,
            "source_kind": source_kind,
            "supersedes_id": supersedes_id,
            "profile_id": profile_id,
        }
        return self.idem.run(
            "feedback.submit",
            idempotency_key,
            payload,
            lambda: self._submit(payload),
        )

    def _submit(self, payload: Mapping) -> dict:
        run_id = payload.get("run_id")
        source_kind = str(payload.get("source_kind") or "user")
        if source_kind not in SOURCE_KINDS:
            raise InvalidInput(
                f"不支持的 source_kind: {source_kind}", field_errors={"source_kind": "unsupported"}
            )
        profile_id = payload.get("profile_id") or PROFILE_ID
        if self.repo.get("profile", profile_id) is None:
            raise InvalidInput(f"profile 不存在: {profile_id}", field_errors={"profile_id": "not_found"})

        run = None
        if run_id:
            run = self.repo.get("run", run_id)
            if run is None:
                raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})

        raw_type = payload.get("type")
        verdict = payload.get("verdict")
        content = (payload.get("content") or "").strip()
        req_scope = payload.get("scope") or "task"
        if req_scope not in SCOPES:
            raise InvalidInput(f"不支持的范围: {req_scope}", field_errors={"scope": "unsupported"})

        # --- 来源与类型的一致性：模型不得冒充用户确认 ---------
        if source_kind == "model":
            if verdict:
                raise InvalidInput(
                    "模型复盘不得写入验收结论，也不能冒充用户确认",
                    field_errors={"verdict": "model_cannot_confirm"},
                )
            if raw_type in ("acceptance", "rejection", "verifier"):
                raise InvalidInput(
                    "模型复盘只能以 review/correction 记录，不得充当验收或客观检查",
                    field_errors={"type": "model_cannot_confirm"},
                )
            if not content:
                raise InvalidInput("模型复盘必须给出内容", field_errors={"content": "required"})
            author = "model"
            type_ = raw_type or "review"
        elif source_kind == "objective":
            if raw_type not in (None, "verifier"):
                raise InvalidInput(
                    "客观检查结果只能记为 verifier",
                    field_errors={"type": "objective_mismatch"},
                )
            type_ = "verifier"
            author = "verifier"
            if verdict is not None and verdict not in VERDICTS:
                raise InvalidInput(f"非法结论: {verdict}", field_errors={"verdict": "unsupported"})
        else:  # user
            author = "owner"
            type_ = raw_type
            if verdict is not None and verdict not in VERDICTS:
                raise InvalidInput(f"非法结论: {verdict}", field_errors={"verdict": "unsupported"})

        if type_ is None:
            type_ = "acceptance" if verdict else "correction"
        if type_ not in FEEDBACK_TYPES:
            raise InvalidInput(f"不支持的反馈类型: {type_}", field_errors={"type": "unsupported"})
        if type_ in ("acceptance", "rejection"):
            verdict = verdict or ("accepted" if type_ == "acceptance" else "rejected")
        if type_ in ("correction", "review") and not content:
            raise InvalidInput(
                f"{type_} 必须保留原话", field_errors={"content": "required"}
            )

        # --- 目标 skill / 版本：确定不了就保留待归因 -----------
        target_skill_id = payload.get("target_skill_id")
        target_version_id = payload.get("target_version_id")
        if target_skill_id is not None:
            if self.repo.get("skill", target_skill_id) is None:
                raise InvalidInput(
                    f"目标技能不存在: {target_skill_id}", field_errors={"target_skill_id": "not_found"}
                )
            if target_version_id is None:
                target_version_id = self._current_version_id(target_skill_id)
            attribution_state = "attributed"
            attribution_source = "explicit"
        elif run is not None:
            target_skill_id, target_version_id, attribution_state = self._infer_target(run_id)
            attribution_source = "single_loaded_skill" if attribution_state == "attributed" else "ambiguous"
        else:
            attribution_state = "pending"
            attribution_source = "none"

        if target_version_id is not None and self.repo.get("skill_version", target_version_id) is None:
            raise InvalidInput(
                f"目标版本不存在: {target_version_id}", field_errors={"target_version_id": "not_found"}
            )

        locator_dict, locator_verified = self._normalize_locator(payload.get("locator"), run_id=run_id)

        # --- 范围：默认本次，长期范围需用户显式选择或明确说法 ---
        scope, scope_info = self._resolve_scope(
            requested=req_scope, source_kind=source_kind, content=content
        )

        supersedes_id = payload.get("supersedes_id")
        if supersedes_id:
            old = self.repo.get("feedback", supersedes_id)
            if old is None:
                raise InvalidInput(
                    f"被更正的反馈不存在: {supersedes_id}", field_errors={"supersedes_id": "not_found"}
                )
            if old["status"] != _ACTIVE:
                raise InvalidState(
                    f"反馈 {supersedes_id} 状态为 {old['status']}，不能再次更正",
                    field_errors={"supersedes_id": "not_active"},
                )

        now = utc_now()
        feedback_id = new_id("fb")
        row = {
            "id": feedback_id,
            "run_id": run_id,
            "profile_id": profile_id,
            "target_skill_id": target_skill_id,
            "target_version_id": target_version_id,
            "type": type_,
            "verdict": verdict,
            "source_kind": source_kind,
            "scope": scope,
            "content": content,
            "locator": json.dumps(locator_dict, ensure_ascii=False),
            "supersedes_id": supersedes_id,
            "status": _ACTIVE,
            "attribution_state": attribution_state,
            "author": author,
            "created_at": now,
            "updated_at": now,
        }
        outcome_updated = False
        with self.store.tx() as conn:
            self.repo.insert("feedback", row, conn=conn)
            if supersedes_id:
                self.repo.update(
                    "feedback",
                    supersedes_id,
                    {"status": "superseded", "updated_at": now},
                    conn=conn,
                )
                self.events.audit(
                    "feedback.superseded",
                    "feedback",
                    supersedes_id,
                    before={"status": "active"},
                    after={"status": "superseded", "superseded_by": feedback_id},
                    conn=conn,
                )
            self.events.audit(
                "feedback.submit",
                "feedback",
                feedback_id,
                after={
                    "type": type_,
                    "source_kind": source_kind,
                    "scope": scope,
                    "author": author,
                    "attribution_state": attribution_state,
                    "run_id": run_id,
                },
                conn=conn,
            )
            # 用户动作或客观检查才能改变 outcome；模型复盘不能
            if run_id and verdict and source_kind in ("user", "objective"):
                outcome_updated = self._apply_outcome(conn, run_id, verdict, feedback_id)
        self.events.product_event(
            "feedback.submitted",
            {
                "type": type_,
                "source_kind": source_kind,
                "scope": scope,
                "attribution_state": attribution_state,
                "has_target": bool(target_skill_id),
            },
            event_id=f"feedback:{feedback_id}",
        )
        data = self._feedback_dict(self.repo.get("feedback", feedback_id))
        data["outcome_updated"] = outcome_updated
        data["locator_verified"] = locator_verified
        data["attribution_source"] = attribution_source
        data.update(scope_info)
        data["clarification"] = self._clarification(
            attribution_state=attribution_state,
            needs_scope_confirmation=bool(scope_info.get("needs_scope_confirmation")),
            scope_suggestion=scope_info.get("scope_suggestion"),
        )
        return data

    # ==================================================================== 撤回
    def withdraw(self, feedback_id, *, reason=None, actor: str = "owner") -> dict:
        """撤回反馈：未启用候选失效；已启用版本标记“来源已撤回”但不改指针。"""
        row = self.repo.get("feedback", feedback_id)
        if row is None:
            raise InvalidInput(f"反馈不存在: {feedback_id}", field_errors={"feedback_id": "not_found"})
        if row["status"] == "withdrawn":
            return self._withdraw_result(feedback_id, reason=reason, already=True)
        if row["status"] != _ACTIVE:
            raise InvalidState(
                f"反馈 {feedback_id} 状态为 {row['status']}，不能撤回",
                field_errors={"feedback_id": "not_active"},
            )
        now = utc_now()
        invalidated: list[str] = []
        activated: list[dict] = []
        affected_lessons: list[str] = []
        with self.store.tx() as conn:
            self.repo.update(
                "feedback",
                feedback_id,
                {"status": "withdrawn", "updated_at": now},
                conn=conn,
            )
            self.events.audit(
                "feedback.withdraw",
                "feedback",
                feedback_id,
                before={"status": "active"},
                after={"status": "withdrawn", "reason": reason},
                actor=actor,
                reason=reason,
                conn=conn,
            )
            lessons = conn.execute(
                "SELECT DISTINCT lesson_id FROM lesson_source WHERE feedback_id = ?", (feedback_id,)
            ).fetchall()
            for lesson_row in lessons:
                lesson_id = lesson_row["lesson_id"]
                affected_lessons.append(lesson_id)
                remaining = conn.execute(
                    "SELECT COUNT(*) AS n FROM lesson_source ls JOIN feedback f ON f.id = ls.feedback_id "
                    "WHERE ls.lesson_id = ? AND f.status = 'active'",
                    (lesson_id,),
                ).fetchone()["n"]
                if int(remaining or 0) == 0:
                    conn.execute(
                        "UPDATE lesson SET status = 'stale', updated_at = ? "
                        "WHERE id = ? AND status NOT IN ('activated','ignored','rejected')",
                        (now, lesson_id),
                    )
                    self.events.audit(
                        "lesson.source_withdrawn",
                        "lesson",
                        lesson_id,
                        after={"status": "stale", "feedback_id": feedback_id},
                        actor=actor,
                        conn=conn,
                    )
                proposals = conn.execute(
                    "SELECT DISTINCT proposal_id FROM proposal_source WHERE lesson_id = ?",
                    (lesson_id,),
                ).fetchall()
                for proposal_row in proposals:
                    proposal_id = proposal_row["proposal_id"]
                    proposal = self.repo.get("proposal", proposal_id)
                    if proposal is None or proposal["status"] in ("activated", "rejected", "superseded"):
                        continue
                    candidate_version_id = proposal["candidate_version_id"]
                    is_activated = bool(candidate_version_id) and self._version_activated(candidate_version_id)
                    if is_activated:
                        if not any(item["version_id"] == candidate_version_id for item in activated):
                            activated.append(
                                {
                                    "proposal_id": proposal_id,
                                    "version_id": candidate_version_id,
                                    "skill_id": proposal["skill_id"],
                                    "notice": "来源已撤回：当前生效版本的学习来源被用户撤回，请审阅（不自动改版本指针）",
                                }
                            )
                        self.events.audit(
                            "proposal.source_withdrawn",
                            "proposal",
                            proposal_id,
                            after={
                                "status": proposal["status"],
                                "source_withdrawn_feedback_id": feedback_id,
                                "notice": "来源已撤回，需审阅当前生效版本",
                                "version_pointer_changed": False,
                            },
                            actor=actor,
                            conn=conn,
                        )
                    else:
                        conn.execute(
                            "UPDATE proposal SET status = 'stale', stale_reason = ?, updated_at = ? "
                            "WHERE id = ?",
                            (f"feedback_withdrawn:{feedback_id}", now, proposal_id),
                        )
                        if proposal_id not in invalidated:
                            invalidated.append(proposal_id)
                        self.events.audit(
                            "proposal.invalidated",
                            "proposal",
                            proposal_id,
                            before={"status": proposal["status"]},
                            after={
                                "status": "stale",
                                "reason": "source_feedback_withdrawn",
                                "feedback_id": feedback_id,
                            },
                            actor=actor,
                            conn=conn,
                        )
        self.events.product_event(
            "feedback.withdrawn",
            {
                "invalidated_proposals": len(invalidated),
                "activated_versions": len(activated),
            },
            event_id=f"feedback.withdraw:{feedback_id}",
        )
        result = self._withdraw_result(feedback_id, reason=reason, already=False)
        result["invalidated_proposals"] = invalidated
        result["activated_versions"] = activated
        result["affected_lessons"] = affected_lessons
        return result

    def _withdraw_result(self, feedback_id: str, *, reason, already: bool) -> dict:
        row = self.repo.get("feedback", feedback_id)
        data = self._feedback_dict(row)
        data.update(
            {
                "feedback_id": feedback_id,
                "already_withdrawn": already,
                "reason": reason,
                "version_pointer_changed": False,
                "notice": "撤回只影响未启用候选；已启用版本标记来源已撤回，需用户审阅",
            }
        )
        return data

    # ==================================================================== 验收
    def update_outcome(self, run_id, outcome: str, *, feedback_id=None) -> dict:
        """只改 ``run.outcome``；``execution_status`` 不会被倒改。"""
        if outcome not in OUTCOMES:
            raise InvalidInput(f"非法验收结论: {outcome}", field_errors={"outcome": "unsupported"})
        run = self.repo.get("run", run_id)
        if run is None:
            raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})
        if feedback_id and self.repo.get("feedback", feedback_id) is None:
            raise InvalidInput(f"反馈不存在: {feedback_id}", field_errors={"feedback_id": "not_found"})
        with self.store.tx() as conn:
            changed = self._apply_outcome(conn, run_id, outcome, feedback_id)
        self.events.product_event(
            "run.outcome_updated",
            {"outcome": outcome, "changed": changed},
            event_id=f"outcome:{run_id}:{outcome}:{feedback_id or ''}",
        )
        fresh = self.repo.get("run", run_id)
        return {
            "run_id": run_id,
            "outcome": fresh["outcome"],
            "execution_status": fresh["execution_status"],
            "feedback_id": feedback_id,
            "changed": changed,
            "note": "验收结论与执行状态分离：completed 只表示执行结束",
        }

    def _apply_outcome(self, conn, run_id: str, outcome: str, feedback_id: str | None) -> bool:
        row = conn.execute(
            "SELECT outcome, execution_status FROM run WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return False
        changed = row["outcome"] != outcome
        conn.execute(
            "UPDATE run SET outcome = ?, updated_at = ? WHERE id = ?",
            (outcome, utc_now(), run_id),
        )
        self.events.audit(
            "run.outcome",
            "run",
            run_id,
            before={"outcome": row["outcome"], "execution_status": row["execution_status"]},
            after={
                "outcome": outcome,
                "execution_status": row["execution_status"],
                "feedback_id": feedback_id,
                "execution_status_unchanged": True,
            },
            conn=conn,
        )
        return changed

    # ==================================================================== 查询
    def list_feedback(self, *, run_id=None, skill_id=None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM feedback"
        clauses: list[str] = []
        params: list = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if skill_id:
            clauses.append("target_skill_id = ?")
            params.append(skill_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        return [self._feedback_dict(row) for row in self.store.query(sql, tuple(params))]

    def pending_attribution(self) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM feedback WHERE status = 'active' AND attribution_state = 'pending' "
            "ORDER BY created_at ASC, rowid ASC LIMIT 500"
        )
        return [self._feedback_dict(row) for row in rows]

    def get_conflicts(self, *, skill_id) -> list[dict]:
        """同范围矛盾 → 待澄清。

        同一 ``scope`` 内比较；``task`` 范围以任务为单位（不同任务之间不构成范围冲突）。
        只有用户更正（``supersedes_id``）会让旧反馈失效，时间先后不构成覆盖。
        """
        if self.repo.get("skill", skill_id) is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        rows = self.store.query(
            "SELECT * FROM feedback WHERE target_skill_id = ? AND status = 'active' "
            "ORDER BY created_at ASC, rowid ASC LIMIT 500",
            (skill_id,),
        )
        conflicts = detect_conflicts(rows)
        for item in conflicts:
            self.events.product_event(
                "feedback.clarification_pending",
                {
                    "skill_id": skill_id,
                    "scope": item["scope"],
                    "kind": item["kind"],
                    "feedback_count": len(item["feedback_ids"]),
                },
                event_id="clarify:" + canonical_hash(
                    {"skill_id": skill_id, "scope": item["scope"], "ids": sorted(item["feedback_ids"])}
                ),
            )
        return conflicts

    # ==================================================================== 内部
    def _feedback_dict(self, row) -> dict:
        if row is None:
            return {}
        data = {key: row[key] for key in row.keys()}
        data["locator"] = _json(data.get("locator"), {})
        data["feedback_id"] = data["id"]
        data["is_user_confirmed"] = data.get("source_kind") in ("user", "objective")
        data["original_text_preserved"] = data.get("source_kind") != "model"
        data["effective"] = data.get("status") == _ACTIVE
        return data

    def _current_version_id(self, skill_id) -> str | None:
        row = self.store.query_one(
            "SELECT version_id FROM activation WHERE skill_id = ? ORDER BY revision DESC LIMIT 1",
            (skill_id,),
        )
        return row["version_id"] if row else None

    def _version_activated(self, version_id) -> bool:
        row = self.store.query_one(
            "SELECT id FROM activation WHERE version_id = ? LIMIT 1", (version_id,)
        )
        return row is not None

    def _infer_target(self, run_id) -> tuple[str | None, str | None, str]:
        """按“实际加载的 skill”推断目标；多于一个或没有则待归因。"""
        loaded = self.store.query(
            "SELECT DISTINCT skill_id, version_id FROM skill_load "
            "WHERE run_id = ? AND load_status = 'loaded' ORDER BY skill_id ASC",
            (run_id,),
        )
        skill_ids: list[str] = [row["skill_id"] for row in loaded]
        versions = [row["version_id"] for row in loaded]
        run = self.repo.get("run", run_id)
        if run is not None:
            for skill_id in _json(run["skill_ids"], []) or []:
                if skill_id not in skill_ids:
                    skill_ids.append(skill_id)
        if len(skill_ids) == 1:
            return skill_ids[0], (versions[0] if versions else None), "attributed"
        if not skill_ids and run is not None:
            explicit = _json(run["skill_ids"], []) or []
            if len(explicit) == 1:
                return explicit[0], None, "attributed"
        return None, None, "pending"

    def _normalize_locator(self, locator, *, run_id=None) -> tuple[dict, bool]:
        """定位任务 / 步骤 / 产物片段：能核对的就核对，核对不了保持原样但不假装已验证。"""
        if locator is None:
            return {}, False
        if isinstance(locator, str):
            parsed = _json(locator, None)
            locator = parsed if isinstance(parsed, dict) else {"text": locator}
        if not isinstance(locator, Mapping):
            raise InvalidInput("locator 必须是对象或字符串", field_errors={"locator": "invalid"})
        data = {str(k): v for k, v in locator.items()}
        verified = False
        if run_id and data.get("artifact_id"):
            row = self.store.query_one(
                "SELECT id FROM artifact WHERE id = ? AND run_id = ?",
                (data["artifact_id"], run_id),
            )
            if row is None:
                raise InvalidInput(
                    "locator.artifact_id 不属于该任务",
                    field_errors={"locator": "artifact_mismatch"},
                )
            verified = True
        if run_id and data.get("step_seq") is not None:
            row = self.store.query_one(
                "SELECT id FROM run_step WHERE run_id = ? AND seq = ?",
                (run_id, int(data["step_seq"])),
            )
            if row is None:
                raise InvalidInput(
                    "locator.step_seq 在该任务中不存在", field_errors={"locator": "step_mismatch"}
                )
            verified = True
        return data, verified

    def _resolve_scope(self, *, requested: str, source_kind: str, content: str) -> tuple[str, dict]:
        """范围判定：用户显式为事实；模型推断只是建议。"""
        info: dict = {"scope_source": "default_task", "scope_suggestion": None, "needs_scope_confirmation": False}
        explicit = requested != "task"
        specific = bool(_LONG_TERM_SPECIFIC_RE.search(content or ""))
        vague = bool(_LONG_TERM_RE.search(content or ""))
        guessed = "personal" if _PERSONAL_RE.search(content or "") else "workspace"

        if source_kind == "model":
            if explicit or specific or vague:
                info["scope_source"] = "model_suggestion_only"
                info["scope_suggestion"] = requested if explicit else guessed
                info["needs_scope_confirmation"] = True
            return "task", info

        if explicit:
            info["scope_source"] = "user_explicit"
            return requested, info
        if source_kind == "user" and specific:
            info["scope_source"] = "user_wording_explicit"
            return guessed, info
        if source_kind == "objective" and specific:
            info["scope_source"] = "objective_suggestion_only"
            info["scope_suggestion"] = guessed
            info["needs_scope_confirmation"] = True
        elif vague:
            info["scope_source"] = "ambiguous_long_term"
            info["scope_suggestion"] = guessed
            info["needs_scope_confirmation"] = True
        return "task", info

    def _clarification(self, *, attribution_state: str, needs_scope_confirmation: bool, scope_suggestion):
        """未明确时只问一个必要问题。"""
        if attribution_state == "pending":
            return {
                "kind": "attribution",
                "question": "这条反馈应该记到哪个 skill 上？（当前无法确定唯一目标）",
                "options": ["指定一个 skill", "仅保留反馈不做学习"],
            }
        if needs_scope_confirmation:
            return {
                "kind": "scope",
                "question": "这条要求只用于本次任务，还是以后同类任务都要这样？",
                "options": ["仅本次", "当前项目同类任务", "个人长期（跨项目）"],
                "suggested_scope": scope_suggestion,
            }
        return None


# --------------------------------------------------------------------------- 冲突判定
def detect_conflicts(rows: Iterable[Any]) -> list[dict]:
    """纯函数形式的同范围矛盾判定（供 lessons 的入队闸门复用）。"""
    items = [row for row in rows if row["status"] == "active" and row["target_skill_id"]]
    groups: dict[tuple, list] = {}
    for row in items:
        key = (
            row["target_skill_id"],
            row["scope"],
            row["run_id"] if row["scope"] == "task" else None,
        )
        groups.setdefault(key, []).append(row)
    conflicts: list[dict] = []
    for (skill_id, scope, run_id), group in groups.items():
        for index, first in enumerate(group):
            for second in group[index + 1 :]:
                if _is_supersede_pair(first, second):
                    continue
                kind = _conflict_kind(first, second)
                if kind is None:
                    continue
                conflicts.append(
                    {
                        "skill_id": skill_id,
                        "scope": scope,
                        "run_id": run_id,
                        "kind": kind,
                        "feedback_ids": [first["id"], second["id"]],
                        "status": "pending_clarification",
                        "question": "同一范围内出现相互矛盾的要求，请确认以哪一条为准（时间先后不构成覆盖）。",
                        "detected_at": utc_now(),
                    }
                )
    return conflicts


def conflict_feedback_ids(rows: Iterable[Any]) -> set[str]:
    ids: set[str] = set()
    for item in detect_conflicts(rows):
        ids.update(item["feedback_ids"])
    return ids


def _is_supersede_pair(a, b) -> bool:
    if a["supersedes_id"] and a["supersedes_id"] == b["id"]:
        return True
    if b["supersedes_id"] and b["supersedes_id"] == a["id"]:
        return True
    return False


def _conflict_kind(first, second) -> str | None:
    verdicts = {str(first["verdict"] or ""), str(second["verdict"] or "")}
    if "accepted" in verdicts and "rejected" in verdicts:
        return "verdict_conflict"
    polarity_a = _polarity(first["content"] or "")
    polarity_b = _polarity(second["content"] or "")
    if not polarity_a or not polarity_b or polarity_a == polarity_b:
        return None
    if _keywords(first["content"] or "") & _keywords(second["content"] or ""):
        return "rule_contradiction"
    return None


def _polarity(text: str) -> str | None:
    """句子的指令极性；被否定的正面词（“不要标注”）不算正面。"""
    low = (text or "").lower()
    negative = any(marker in low for marker in _NEGATIVE_MARKERS)
    positive = False
    for marker in _POSITIVE_MARKERS:
        index = low.find(marker)
        while index != -1:
            prefix = low[max(0, index - 3) : index]
            if not any(item in prefix for item in ("不", "别", "无", "禁", "未", "莫", "not", "no")):
                positive = True
                break
            index = low.find(marker, index + 1)
        if positive:
            break
    if negative and not positive:
        return "negative"
    if positive and not negative:
        return "positive"
    if positive and negative:
        return "mixed"
    return None


def _keywords(text: str) -> set[str]:
    cleaned = re.sub(r"[\s，。；、！？：“”‘’（）()\[\]【】,.;:!?\"'`]+", " ", text or "")
    found: set[str] = set()
    for token in cleaned.split():
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", token):
            found.add(word.lower())
        for chunk in re.findall(r"[\u4e00-\u9fff]+", token):
            for size in (2, 3, 4):
                for start in range(len(chunk) - size + 1):
                    found.add(chunk[start : start + size])
    return {word for word in found if not any(marker in word for marker in _ALL_MARKERS)}


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
