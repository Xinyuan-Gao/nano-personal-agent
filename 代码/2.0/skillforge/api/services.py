from __future__ import annotations

import inspect
import json
from pathlib import Path

from ..core.clock import utc_now
from ..core.config import (
    DEFAULT_CONTAINER_IMAGE,
    SETTING_CONTAINER_ENABLED,
    SETTING_CONTAINER_IMAGE,
    SETTING_LEARNING_PAUSED,
    AppConfig,
    read_json_setting,
)
from ..core.errors import InvalidInput
from ..core.blobs import BlobStore
from ..core.db import Store
from ..core.events import EventLog
from ..core.repository import IdempotencyStore, Repo

PROFILE_ID = "local"


def _filter_kwargs(cls_or_fn, pool: dict) -> dict:
    """按构造函数签名挑选依赖，避免各层签名细微差异导致装配失败。"""
    target = cls_or_fn
    if inspect.isclass(cls_or_fn):
        target = cls_or_fn.__init__
    signature = inspect.signature(target)
    accepted: dict = {}
    missing: list[str] = []
    for name, param in signature.parameters.items():
        if name in ("self", "args", "kwargs"):
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if name in pool:
            accepted[name] = pool[name]
        elif param.default is inspect.Parameter.empty:
            missing.append(name)
    if missing:
        raise InvalidInput(
            f"无法装配 {getattr(cls_or_fn, '__name__', cls_or_fn)}：缺少依赖 {', '.join(missing)}"
        )
    return accepted


class AppServices:
    """本地产品的全部业务能力。"""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig.load()
        self.config.ensure_dirs()
        self.store = Store(self.config.db_path)
        self.store.migrate()
        self.repo = Repo(self.store)
        self.blobs = BlobStore(self.config.blob_dir)
        self.events = EventLog(self.store)
        self.idem = IdempotencyStore(self.store)

        from ..platform.budget import BudgetLedger
        from ..platform.container import ContainerRuntime
        from ..platform.credentials import CredentialStore
        from ..platform.model import ModelClient, ModelConfig
        from ..platform.queue import JobQueue, WorkerPool
        from ..platform.settings import SettingsService

        self.creds = CredentialStore(self.config.secret_dir)
        self.settings = SettingsService(self.store, self.repo, self.events, self.idem, self.creds, self.config)
        self.profile = self.settings.bootstrap()
        self.budget = BudgetLedger(self.store)
        container_enabled = bool(read_json_setting(self.store, SETTING_CONTAINER_ENABLED, True))
        image = read_json_setting(self.store, SETTING_CONTAINER_IMAGE, DEFAULT_CONTAINER_IMAGE)
        self.container = ContainerRuntime(image=image or DEFAULT_CONTAINER_IMAGE, enabled=container_enabled)
        # skills 层不能依赖 platform：把容器可用性写进 config.extra，供回退/启用时校验脚本能力
        try:
            self.config.extra["container_available"] = bool(self.container.status().available)
        except Exception:  # pragma: no cover - 探测失败按不可用处理
            self.config.extra["container_available"] = False
        self.queue = JobQueue(self.store, self.repo, self.events)
        self._model_client_class = ModelClient
        self._model_config_class = ModelConfig

        pool: dict = {
            "store": self.store,
            "repo": self.repo,
            "blobs": self.blobs,
            "events": self.events,
            "idem": self.idem,
            "creds": self.creds,
            "config": self.config,
            "settings": self.settings,
            "budget": self.budget,
            "container": self.container,
            "queue": self.queue,
            "model_factory": self.model_factory,
            "profile_id": PROFILE_ID,
        }

        # ---------------------------------------------------------- skills
        from ..skills.activation import ActivationService
        from ..skills.selection import SkillSelector
        from ..skills.service import SkillService

        self.skills = SkillService(**self._wire(SkillService, pool))
        pool["skills"] = self.skills
        self.selector = SkillSelector(**self._wire(SkillSelector, pool))
        pool["selector"] = self.selector
        self.activation = ActivationService(**self._wire(ActivationService, pool))
        pool["activation"] = self.activation

        # ---------------------------------------------------------- memory（P1）
        from ..memory.service import MemoryService

        self.memory = MemoryService(**self._wire(MemoryService, pool))
        pool["memory"] = self.memory

        # ---------------------------------------------------------- runtime
        from ..runtime.runs import RunService

        self.runs = RunService(**self._wire(RunService, pool))
        pool["runs"] = self.runs

        # ---------------------------------------------------------- learning
        from ..learning.feedback import FeedbackService
        from ..learning.lessons import LessonService
        from ..learning.proposals import ProposalService

        self.feedback = FeedbackService(**self._wire(FeedbackService, pool))
        pool["feedback"] = self.feedback
        self.lessons = LessonService(**self._wire(LessonService, pool))
        pool["lessons"] = self.lessons
        self.proposals = ProposalService(**self._wire(ProposalService, pool))
        pool["proposals"] = self.proposals

        # ---------------------------------------------------------- evaluation
        from ..evaluation.jobs import EvalService
        from ..evaluation.suites import EvalSuiteService

        self.suites = EvalSuiteService(**self._wire(EvalSuiteService, pool))
        pool["suites"] = self.suites
        self.evals = EvalService(**self._wire(EvalService, pool))
        pool["evals"] = self.evals

        # ---------------------------------------------------------- data
        from ..data.backup import DataService
        from ..data.metrics import MetricsService
        from ..data.retention import RetentionService

        self.data = DataService(**self._wire(DataService, pool))
        pool["data"] = self.data
        self.metrics_service = MetricsService(**self._wire(MetricsService, pool))
        self.retention = RetentionService(**self._wire(RetentionService, pool))

        self.workers = WorkerPool(
            self.queue,
            {
                "run.execute": self._handle_run_job,
                "learning.extract": self._handle_learning_job,
                "eval.execute": self._handle_eval_job,
                "memory.dream": self._handle_dream_job,
            },
            max_execution=2,
            max_learning=1,
        )

    # ------------------------------------------------------------------ 装配
    def _wire(self, cls, pool: dict) -> dict:
        return _filter_kwargs(cls, pool)

    def model_factory(self):
        """返回绑定当前生效模型配置的客户端；未配置时返回 None。"""
        data = self.settings.get_model_config(PROFILE_ID)
        if not data or not data.get("base_url") or not data.get("model"):
            return None
        cfg = self._model_config_class(
            provider=data.get("provider") or "openai_compatible",
            base_url=data["base_url"],
            model=data["model"],
            api_key_ref=data.get("api_key_ref"),
            temperature=float(data.get("temperature") or 0.2),
            max_output_tokens=int(data.get("max_output_tokens") or 2048),
            timeout_s=float(data.get("timeout_s") or 60.0),
            extra=data.get("extra") or {},
        )
        return self._model_client_class(cfg, self.creds)

    # ------------------------------------------------------------------ worker
    def _handle_run_job(self, job) -> dict:
        payload = job.payload or {}
        run_id = payload.get("run_id") or job.run_id
        # 用真实租约所有者作为 worker_id：heartbeat 续租依赖它匹配
        worker_id = getattr(job, "lease_owner", None) or f"worker-exec-{job.id}"
        self.runs.execute(run_id, worker_id)
        return {"run_id": run_id}

    def _handle_learning_job(self, job) -> dict:
        payload = job.payload or {}
        feedback_ids = payload.get("feedback_ids") or ([payload["feedback_id"]] if payload.get("feedback_id") else [])
        lessons = self.lessons.extract(feedback_ids, job_id=job.id)
        return {"lesson_ids": [lesson.get("id") for lesson in lessons]}

    def _handle_eval_job(self, job) -> dict:
        # 队列作业 id 与评测作业 id 不同：payload 里携带真正的 eval_job_id
        payload = job.payload or {}
        eval_job_id = payload.get("job_id")
        if not eval_job_id:
            raise InvalidInput("评测作业缺少 job_id", field_errors={"job_id": "required"})
        worker_id = getattr(job, "lease_owner", None) or f"worker-eval-{job.id}"
        self.evals.execute(eval_job_id, worker_id)
        return {"job_id": eval_job_id}

    def _handle_dream_job(self, job) -> dict:
        payload = job.payload or {}
        dream_job_id = payload.get("dream_job_id")
        if not dream_job_id:
            raise InvalidInput("整合作业缺少 dream_job_id", field_errors={"dream_job_id": "required"})
        result = self.memory.dream.run(dream_job_id)
        return {"dream_job_id": dream_job_id, "status": result.get("status"),
                "changed_topics": len((result.get("result") or {}).get("changed_topic_ids") or [])}

    def start_workers(self) -> None:
        self.workers.start()

    def stop_workers(self) -> None:
        self.workers.stop()

    def close(self) -> None:
        try:
            self.stop_workers()
        finally:
            self.store.close()

    # ------------------------------------------------------------------ 基础
    def health(self) -> dict:
        from .. import __version__

        model = self.settings.get_model_config(PROFILE_ID)
        return {
            "status": "ok",
            "version": __version__,
            "schema_version": self.store.schema_version,
            "home": str(self.config.home),
            "model": {"provider": model.get("provider"), "model": model.get("model"),
                      "configured": bool(model.get("base_url") and model.get("model"))},
            "learning_enabled": self.lessons.is_learning_enabled(),
            "memory_enabled": self.memory.is_enabled(),
            "memory_layers": ["working", "daily_log", "durable_topic", "memory_index"],
        }

    def doctor(self) -> dict:
        return self.settings.doctor(container=self.container, model_client=self.model_factory())

    def settings_snapshot(self) -> dict:
        return {
            "config": self.config.describe(),
            "model": self.settings.get_model_config(PROFILE_ID),
            "workspaces": self.settings.list_workspaces(PROFILE_ID),
            "budgets": self.settings.get_budget_defaults(),
            "learning": self.get_learning_settings(),
            "memory": self.memory.settings(),
            "container": self.container.status().__dict__,
        }

    def record_page_view(self, page_id: str) -> dict:
        self.events.product_event("page_view", {"page_id": page_id, "profile_id": PROFILE_ID})
        return {"ok": True}

    # ------------------------------------------------------------------ 模型
    def get_model_config(self) -> dict:
        return {"model": self.settings.get_model_config(PROFILE_ID)}

    def set_model_config(self, **kwargs) -> dict:
        allowed = {"provider", "base_url", "model", "api_key", "api_key_ref", "temperature",
                   "max_output_tokens", "timeout_s", "extra"}
        payload = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        payload.setdefault("provider", "openai_compatible")
        if not payload.get("base_url") or not payload.get("model"):
            raise InvalidInput("base_url 与 model 必填", field_errors={"base_url": "required"})
        result = self.settings.set_model_config(**payload, profile_id=PROFILE_ID)
        return {"model": result}

    def check_model(self, **_kwargs) -> dict:
        client = self.model_factory()
        if client is None:
            raise InvalidInput("尚未配置模型 endpoint 与模型名", field_errors={"model": "not_configured"})
        result = client.check_connection()
        self.settings.record_model_check(bool(result.get("ok")), result, PROFILE_ID)
        return result

    # ------------------------------------------------------------------ 工作区
    def list_workspaces(self, include_removed: bool = False) -> dict:
        return {"workspaces": self.settings.list_workspaces(PROFILE_ID, include_removed=include_removed)}

    def add_workspace(self, **kwargs) -> dict:
        name = (kwargs.get("name") or "").strip()
        root_path = (kwargs.get("root_path") or "").strip()
        if not name or not root_path:
            raise InvalidInput("name 与 root_path 必填", field_errors={"name": "required"})
        workspace = self.settings.add_workspace(
            name=name,
            root_path=root_path,
            artifact_path=kwargs.get("artifact_path") or None,
            allowed_tools=kwargs.get("allowed_tools"),
            allow_network=bool(kwargs.get("allow_network", False)),
            allow_scripts=bool(kwargs.get("allow_scripts", True)),
            profile_id=PROFILE_ID,
            idempotency_key=kwargs.get("idempotency_key"),
        )
        return {"workspace": workspace}

    def remove_workspace(self, workspace_id: str) -> dict:
        return {"workspace": self.settings.remove_workspace(workspace_id)}

    def check_workspace(self, workspace_id: str) -> dict:
        return self.settings.check_workspace(workspace_id)

    # ------------------------------------------------------------------ 技能库
    def list_skills(self, query: str | None = None, workspace_id: str | None = None,
                    include_archived: bool = True) -> dict:
        skills = self.skills.list_skills(workspace_id=workspace_id, query=query,
                                        include_archived=include_archived, profile_id=PROFILE_ID)
        return {"skills": skills}

    def import_skill(self, **kwargs) -> dict:
        source_path = (kwargs.get("source_path") or "").strip()
        if not source_path:
            raise InvalidInput("source_path 必填", field_errors={"source_path": "required"})
        scope = kwargs.get("scope") or "personal"
        workspace_id = kwargs.get("workspace_id") or None
        if scope == "workspace" and not workspace_id:
            raise InvalidInput("workspace 作用域必须指定 workspace_id",
                               field_errors={"workspace_id": "required"})
        return self.skills.import_skill(
            source_path,
            scope=scope,
            workspace_id=workspace_id,
            profile_id=PROFILE_ID,
            idempotency_key=kwargs.get("idempotency_key"),
            on_conflict=kwargs.get("on_conflict") or "reject",
            new_slug=kwargs.get("new_slug"),
        )

    def create_skill_draft(self, **kwargs) -> dict:
        name = (kwargs.get("name") or "").strip()
        if not name:
            raise InvalidInput("name 必填", field_errors={"name": "required"})
        return self.skills.create_draft(
            name=name,
            description=kwargs.get("description") or "",
            body=kwargs.get("body") or "",
            scope=kwargs.get("scope") or "personal",
            workspace_id=kwargs.get("workspace_id") or None,
            profile_id=PROFILE_ID,
            references=kwargs.get("references"),
            idempotency_key=kwargs.get("idempotency_key"),
        )

    def get_skill(self, skill_id: str) -> dict:
        skill = self.skills.get_skill(skill_id)
        skill_data = skill.get("skill") if isinstance(skill, dict) and "skill" in skill else skill
        versions = self.skills.list_versions(skill_id)
        current = self.activation.current(skill_id)
        files = self.skills.get_version_files(current["version_id"]) if current else []
        result = {
            "skill": skill_data,
            "versions": versions,
            "current": current,
            "activation": {
                "revision": (current or {}).get("revision"),
                "origin": (current or {}).get("origin"),
                "unverified": bool((current or {}).get("unverified")),
                "version_id": (current or {}).get("version_id"),
            },
            "activations": self.activation.history(skill_id),
            "files": files,
        }
        for key in ("findings", "checks", "shadowed"):
            if isinstance(skill, dict) and key in skill:
                result[key] = skill[key]
        return result

    def archive_skill(self, skill_id: str, **_kwargs) -> dict:
        return self.skills.archive(skill_id)

    def restore_skill(self, skill_id: str, **_kwargs) -> dict:
        return self.skills.restore(skill_id)

    def export_skill(self, skill_id: str, dest: str | None = None, **_kwargs) -> dict:
        target = dest or str(self.config.export_dir / f"skill-{skill_id}-{utc_now().replace(':', '')}")
        return self.skills.export_skill(skill_id, target)

    def list_versions(self, skill_id: str) -> dict:
        return {"versions": self.skills.list_versions(skill_id)}

    def list_version_files(self, skill_id: str, version_id: str) -> dict:
        return {"files": self.skills.get_version_files(version_id)}

    def read_version_file(self, skill_id: str, version_id: str, path: str = "SKILL.md") -> dict:
        return {"path": path, "content": self.skills.read_version_file(version_id, path)}

    def rollback_skill(self, skill_id: str, **kwargs) -> dict:
        target_version_id = kwargs.get("target_version_id")
        expected_revision = kwargs.get("expected_revision")
        if not target_version_id or expected_revision is None:
            raise InvalidInput("target_version_id 与 expected_revision 必填")
        return self.activation.rollback(
            skill_id=skill_id,
            target_version_id=target_version_id,
            expected_revision=int(expected_revision),
            idempotency_key=kwargs.get("idempotency_key"),
        )

    def activate_manual_trial(self, skill_id: str, **kwargs) -> dict:
        source_version_id = kwargs.get("source_version_id")
        expected_revision = kwargs.get("expected_revision")
        if not source_version_id or expected_revision is None:
            raise InvalidInput("source_version_id 与 expected_revision 必填")
        if not kwargs.get("unverified_ack"):
            raise InvalidInput("手工试用必须显式确认“效果未验证”",
                               field_errors={"unverified_ack": "required"})
        return self.activation.activate_manual_trial(
            source_version_id=source_version_id,
            unverified_ack=True,
            scope=kwargs.get("scope"),
            expected_revision=int(expected_revision),
            idempotency_key=kwargs.get("idempotency_key"),
        )

    # ------------------------------------------------------------------ 任务
    def create_run(self, **kwargs) -> dict:
        request = (kwargs.get("request") or "").strip()
        workspace_id = kwargs.get("workspace_id")
        if not request:
            raise InvalidInput("request 必填", field_errors={"request": "required"})
        if not workspace_id:
            workspaces = self.settings.list_workspaces(PROFILE_ID)
            if not workspaces:
                raise InvalidInput("尚未配置工作区，请先添加工作区",
                                   field_errors={"workspace_id": "not_configured"})
            workspace_id = workspaces[0]["id"]
        criteria = kwargs.get("completion_criteria") or []
        if isinstance(criteria, str):
            criteria = [line.strip() for line in criteria.splitlines() if line.strip()]
        skill_ids = kwargs.get("skill_ids") or None
        if isinstance(skill_ids, str):
            skill_ids = [skill_ids] if skill_ids else None
        result = self.runs.create_run(
            request=request,
            workspace_id=workspace_id,
            completion_criteria=criteria,
            skill_ids=skill_ids,
            budget=kwargs.get("budget"),
            parent_run_id=kwargs.get("parent_run_id"),
            profile_id=PROFILE_ID,
            idempotency_key=kwargs.get("idempotency_key"),
            learning_enabled=bool(kwargs.get("learning_enabled", True)),
            session_id=kwargs.get("session_id"),
        )
        self.events.product_event(
            "run_started",
            {"run_id": result.get("run_id") or result.get("id"), "status": "queued",
             "workspace_id": workspace_id},
        )
        return result

    def list_runs(self, status: str | None = None, workspace_id: str | None = None,
                  limit: int = 100) -> dict:
        runs = self.runs.list_runs(workspace_id=workspace_id, status=status, limit=int(limit or 100))
        return {"runs": runs}

    def get_run(self, run_id: str, after_seq: int = 0) -> dict:
        data = self.runs.get_run(run_id)
        if isinstance(data, dict) and "run" in data:
            run = data["run"]
        else:
            run = data
        payload = {"run": run}
        payload["events"] = _safe_call(self.runs.get_events, run_id, after_seq=after_seq)
        payload["actions"] = _safe_call(self.runs.get_actions, run_id)
        payload["artifacts"] = _safe_call(self.runs.get_artifacts, run_id)
        payload["loads"] = self.repo.list("skill_load", {"run_id": run_id}, order="created_at ASC", limit=200)
        payload["feedback"] = self.repo.list("feedback", {"run_id": run_id}, order="created_at ASC", limit=100)
        payload["loads"] = [dict(row) for row in payload["loads"]]
        payload["feedback"] = [_decode_json(dict(row), ("locator",)) for row in payload["feedback"]]
        payload["evidence_state"] = self.retention.evidence_state(run_id)
        return payload

    def cancel_run(self, run_id: str, **_kwargs) -> dict:
        return self.runs.cancel(run_id)

    def resume_run(self, run_id: str, **_kwargs) -> dict:
        return self.runs.resume(run_id)

    # ------------------------------------------------------------------ 反馈
    def submit_feedback(self, **kwargs) -> dict:
        run_id = kwargs.get("run_id")
        feedback_type = kwargs.get("type") or ("acceptance" if kwargs.get("verdict") else "correction")
        if not run_id and feedback_type != "correction":
            raise InvalidInput("run_id 必填", field_errors={"run_id": "required"})
        if feedback_type == "correction" and not (kwargs.get("content") or "").strip():
            raise InvalidInput("文字纠正需要填写原话", field_errors={"content": "required"})
        result = self.feedback.submit(
            run_id=run_id,
            type=feedback_type,
            content=kwargs.get("content"),
            verdict=kwargs.get("verdict"),
            scope=kwargs.get("scope") or "task",
            target_skill_id=kwargs.get("target_skill_id"),
            target_version_id=kwargs.get("target_version_id"),
            locator=kwargs.get("locator"),
            source_kind=kwargs.get("source_kind") or "user",
            supersedes_id=kwargs.get("supersedes_id"),
            idempotency_key=kwargs.get("idempotency_key"),
            profile_id=PROFILE_ID,
        )
        # 可学习事件进入后台队列：由应用层统一编排
        # 入队失败不影响反馈已保存这一事实，只在返回体里如实说明。
        result["learning_job_id"] = self._enqueue_learning(result)
        return result

    def _enqueue_learning(self, feedback: dict) -> str | None:
        if not _learning_wanted(feedback):
            return None
        feedback_id = feedback.get("feedback_id") or feedback.get("id")
        if not feedback_id:
            return None
        try:
            return self.lessons.enqueue_from_feedback(feedback_id)
        except Exception as exc:  # noqa: BLE001 - 学习入队失败不得回滚反馈
            feedback["learning_enqueue_error"] = str(exc)
            return None

    def list_feedback(self, run_id: str | None = None, skill_id: str | None = None,
                      limit: int = 200) -> dict:
        return {"feedback": self.feedback.list_feedback(run_id=run_id, skill_id=skill_id, limit=int(limit or 200))}

    def withdraw_feedback(self, feedback_id: str, **kwargs) -> dict:
        return self.feedback.withdraw(feedback_id, reason=kwargs.get("reason"))

    # ------------------------------------------------------------------ 学习
    def get_learning_settings(self) -> dict:
        return {
            "enabled": self.lessons.is_learning_enabled(),
            "paused": bool(read_json_setting(self.store, SETTING_LEARNING_PAUSED, False)),
            "paused_skills": read_json_setting(self.store, "learning.paused_skills", []),
            "auto_activate": False,
        }

    def set_learning_settings(self, **kwargs) -> dict:
        enabled = kwargs.get("enabled")
        paused = kwargs.get("paused")
        skill_id = kwargs.get("skill_id")
        if enabled is not None:
            self.lessons.set_learning_paused(not bool(enabled), skill_id=skill_id)
        elif paused is not None:
            self.lessons.set_learning_paused(bool(paused), skill_id=skill_id)
        else:
            raise InvalidInput("需要提供 enabled 或 paused")
        return self.get_learning_settings()

    def list_lessons(self, skill_id: str | None = None, status: str | None = None,
                     limit: int = 200) -> dict:
        return {"lessons": self.lessons.list_lessons(skill_id=skill_id, status=status, limit=int(limit or 200))}

    def ignore_lesson(self, lesson_id: str, **kwargs) -> dict:
        reason = kwargs.get("reason_code")
        if not reason:
            raise InvalidInput("reason_code 必填", field_errors={"reason_code": "required"})
        return self.lessons.ignore(lesson_id, reason_code=reason)

    # ------------------------------------------------------------------ 候选
    def list_proposals(self, skill_id: str | None = None, status: str | None = None,
                       limit: int = 200) -> dict:
        return {"proposals": self.proposals.list_proposals(skill_id=skill_id, status=status, limit=int(limit or 200))}

    def propose_change(self, **kwargs) -> dict:
        skill_id = kwargs.get("skill_id")
        lesson_ids = kwargs.get("lesson_ids") or []
        if isinstance(lesson_ids, str):
            lesson_ids = [lesson_ids]
        if not skill_id or not lesson_ids:
            raise InvalidInput("skill_id 与 lesson_ids 必填",
                               field_errors={"skill_id": "required", "lesson_ids": "required"})
        baseline = kwargs.get("baseline_version_id")
        if not baseline:
            current = self.activation.current(skill_id)
            if not current:
                raise InvalidInput("该 skill 尚无生效版本，无法生成候选",
                                   field_errors={"baseline_version_id": "missing"})
            baseline = current["version_id"]
        return self.proposals.propose_change(
            skill_id=skill_id,
            baseline_version_id=baseline,
            lesson_ids=lesson_ids,
            budget=kwargs.get("budget"),
            scope=kwargs.get("scope"),
            idempotency_key=kwargs.get("idempotency_key"),
            profile_id=PROFILE_ID,
        )

    def get_proposal(self, proposal_id: str) -> dict:
        data = self.proposals.get_proposal(proposal_id)
        proposal = data.get("proposal") if isinstance(data, dict) and "proposal" in data else data
        diff = data.get("diff") if isinstance(data, dict) and "diff" in data else self.proposals.get_diff(proposal_id)
        sources = []
        try:
            rows = self.repo.list("proposal_source", {"proposal_id": proposal_id}, order="lesson_id ASC", limit=100)
            for row in rows:
                lesson = self.repo.get("lesson", row["lesson_id"])
                sources.append({
                    "lesson_id": row["lesson_id"],
                    "rule_text": (lesson["rule_text"] if lesson else None),
                    "attribution": (lesson["attribution"] if lesson else None),
                })
        except Exception:  # pragma: no cover - 来源表缺失时降级
            sources = []
        return {"proposal": proposal, "diff": diff, "checks": (proposal or {}).get("checks", {}), "sources": sources}

    def edit_candidate(self, proposal_id: str, **kwargs) -> dict:
        edits = kwargs.get("edits") or {}
        if not edits:
            raise InvalidInput("edits 必填", field_errors={"edits": "required"})
        return self.proposals.edit_candidate(proposal_id, edits=edits, note=kwargs.get("note"),
                                            idempotency_key=kwargs.get("idempotency_key"))

    def reject_proposal(self, proposal_id: str, **kwargs) -> dict:
        reason = kwargs.get("reason_code")
        if not reason:
            raise InvalidInput("reason_code 必填", field_errors={"reason_code": "required"})
        return self.proposals.reject(proposal_id, reason_code=reason)

    def check_proposal(self, proposal_id: str) -> dict:
        return self.proposals.check_proposal(proposal_id)

    # ------------------------------------------------------------------ 评测
    def list_suites(self, skill_id: str | None = None) -> dict:
        return {"suites": self.suites.list_suites(skill_id=skill_id)}

    def create_suite(self, **kwargs) -> dict:
        skill_id = kwargs.get("skill_id")
        name = (kwargs.get("name") or "").strip()
        cases = kwargs.get("cases")
        if isinstance(cases, str):
            try:
                cases = json.loads(cases)
            except ValueError as exc:
                raise InvalidInput(f"cases 不是合法 JSON: {exc}") from exc
        if not skill_id or not name or not cases:
            raise InvalidInput("skill_id、name、cases 必填",
                               field_errors={"skill_id": "required", "cases": "required"})
        rubric = kwargs.get("rubric") or {}
        if isinstance(rubric, str):
            rubric = json.loads(rubric)
        if not rubric:
            # 用户未指定评分维度时使用显式的单一维度默认标准，并在冻结记录里标明，
            # 不把默认标准伪装成用户自定义门槛（结论边界）。
            rubric = {
                "dimensions": [{"name": "整体质量", "weight": 100}],
                "pass_threshold": 60,
                "notes": "用户未指定 rubric，使用单一维度默认评分标准；可在新 revision 中调整",
            }
        critical = kwargs.get("critical_checks") or []
        if isinstance(critical, str):
            critical = json.loads(critical)
        thresholds = kwargs.get("thresholds") or {}
        if isinstance(thresholds, str):
            thresholds = json.loads(thresholds)
        suite = self.suites.create_suite(
            skill_id=skill_id,
            name=name,
            category=kwargs.get("category") or "",
            cases=cases,
            rubric=rubric,
            critical_checks=critical,
            thresholds=thresholds,
            objective=kwargs.get("objective") or "quality",
            profile_id=PROFILE_ID,
            idempotency_key=kwargs.get("idempotency_key"),
        )
        if kwargs.get("freeze"):
            suite_id = suite.get("suite_id") or suite.get("id")
            frozen = self.suites.freeze(suite_id)
            return {"suite": frozen, "frozen": True}
        return {"suite": suite}

    def get_suite(self, suite_id: str) -> dict:
        data = self.suites.get_suite(suite_id)
        suite = data.get("suite") if isinstance(data, dict) and "suite" in data else data
        cases = self.suites.get_cases(suite_id)
        return {"suite": suite, "cases": cases}

    def freeze_suite(self, suite_id: str, **_kwargs) -> dict:
        return {"suite": self.suites.freeze(suite_id)}

    def start_evaluation(self, proposal_id: str, **kwargs) -> dict:
        suite_id = kwargs.get("suite_id")
        if not suite_id:
            proposal = self.proposals.get_proposal(proposal_id)
            skill_id = (proposal.get("proposal") if "proposal" in proposal else proposal).get("skill_id")
            suites = self.suites.list_suites(skill_id=skill_id)
            frozen = [s for s in suites if s.get("status") == "frozen"]
            if not frozen:
                raise InvalidInput("该技能没有已冻结的评测集，请先创建并冻结",
                                   field_errors={"suite_id": "not_found"})
            suite_id = frozen[0].get("suite_id") or frozen[0].get("id")
        return self.evals.start_evaluation(
            proposal_id=proposal_id,
            suite_id=suite_id,
            config=kwargs.get("config"),
            budget=kwargs.get("budget"),
            idempotency_key=kwargs.get("idempotency_key"),
            profile_id=PROFILE_ID,
        )

    def list_eval_jobs(self, proposal_id: str | None = None, status: str | None = None) -> dict:
        return {"jobs": self.evals.list_jobs(proposal_id=proposal_id, status=status)}

    def get_eval_job(self, job_id: str) -> dict:
        job = self.evals.get_job(job_id)
        job_data = job.get("job") if isinstance(job, dict) and "job" in job else job
        report = _safe_call(self.evals.build_report, job_id)
        return {"job": job_data, "results": self.evals.get_results(job_id), "report": report}

    def cancel_eval_job(self, job_id: str, **_kwargs) -> dict:
        return self.evals.cancel(job_id)

    def resume_eval_job(self, job_id: str, **_kwargs) -> dict:
        return self.evals.resume(job_id)

    def activate_proposal(self, proposal_id: str, **kwargs) -> dict:
        expected_revision = kwargs.get("expected_revision")
        if expected_revision is None:
            raise InvalidInput("expected_revision 必填", field_errors={"expected_revision": "required"})
        report_id = kwargs.get("report_id")
        if not report_id:
            proposal = self.proposals.get_proposal(proposal_id)
            proposal_data = proposal.get("proposal") if "proposal" in proposal else proposal
            report_id = (proposal_data.get("evaluation") or {}).get("report_id")
        if not report_id:
            raise InvalidInput("缺少可通过校验的评测报告，不能启用",
                               field_errors={"report_id": "required"})
        return self.activation.activate_proposal(
            proposal_id=proposal_id,
            report_id=report_id,
            scope=kwargs.get("scope"),
            expected_revision=int(expected_revision),
            idempotency_key=kwargs.get("idempotency_key"),
        )

    # ------------------------------------------------------------------ 作业
    def list_jobs(self, status: str | None = None, type: str | None = None, limit: int = 100) -> dict:
        jobs = self.queue.list(status=status, type=type, limit=int(limit or 100))
        return {"jobs": [job.to_dict() if hasattr(job, "to_dict") else dict(job) for job in jobs]}

    def cancel_job(self, job_id: str, **_kwargs) -> dict:
        return {"job_id": job_id, "status": self.queue.cancel(job_id)}

    def run_pending_jobs(self, limit: int = 20) -> dict:
        """手动排空队列（CLI 与测试使用），不放后台线程。"""
        executed = self.workers.drain(limit=int(limit or 20))
        return {"executed": executed}

    # ------------------------------------------------------------------ 个人记忆（P1）
    def memory_status(self) -> dict:
        return {"memory": self.memory.status()}

    def memory_settings(self) -> dict:
        return {"settings": self.memory.settings()}

    def set_memory_settings(self, **kwargs) -> dict:
        if kwargs.get("enabled") is not None:
            self.memory.set_enabled(bool(kwargs["enabled"]))
        if kwargs.get("recall_limit") is not None:
            self.memory.set_recall_limit(int(kwargs["recall_limit"]))
        if "retention_days" in kwargs:
            self.memory.set_retention_days(kwargs.get("retention_days"))
        return {"settings": self.memory.settings()}

    def remember(self, content: str | None = None, **kwargs) -> dict:
        content = ((content if content is not None else kwargs.get("content")) or "").strip()
        if not content:
            raise InvalidInput("content 必填", field_errors={"content": "required"})
        return self.memory.remember(
            content,
            scope=kwargs.get("scope") or "task",
            workspace_id=kwargs.get("workspace_id") or None,
            topic_type=kwargs.get("topic_type"),
            title=kwargs.get("title"),
            summary=kwargs.get("summary"),
            session_id=kwargs.get("session_id"),
            run_id=kwargs.get("run_id"),
            tags=kwargs.get("tags"),
            topic_id=kwargs.get("topic_id"),
            create_topic=bool(kwargs.get("create_topic", True)),
            idempotency_key=kwargs.get("idempotency_key"),
        )

    def list_memory_logs(self, day: str | None = None, scope: str | None = None,
                         workspace_id: str | None = None, status: str = "active",
                         source: str | None = None, limit: int = 200) -> dict:
        return {"logs": self.memory.logs.list(day=day, scope=scope, workspace_id=workspace_id,
                                             status=status, source=source, limit=int(limit or 200)),
                "days": self.memory.logs.days()}

    def list_memory_topics(self, scope: str | None = None, workspace_id: str | None = None,
                           status: str | None = None, topic_type: str | None = None,
                           limit: int = 200) -> dict:
        return {"topics": self.memory.topics.list(scope=scope, workspace_id=workspace_id,
                                                 status=status, topic_type=topic_type,
                                                 limit=int(limit or 200)),
                "conflicts": self.memory.topics.conflicts()}

    def get_memory_topic(self, topic_id: str) -> dict:
        return {"topic": self.memory.read_topic(topic_id)}

    def update_memory_topic(self, topic_id: str, **kwargs) -> dict:
        fields = {key: kwargs[key] for key in ("title", "summary", "body", "tags", "scope",
                                               "workspace_id", "topic_type") if key in kwargs}
        if not fields:
            raise InvalidInput("没有需要更新的字段")
        return {"topic": self.memory.topics.update(topic_id, **fields)}

    def confirm_memory_topic(self, topic_id: str, **kwargs) -> dict:
        return {"topic": self.memory.topics.confirm(topic_id, note=kwargs.get("note") or "")}

    def withdraw_memory_topic(self, topic_id: str, **kwargs) -> dict:
        return {"topic": self.memory.topics.withdraw(topic_id, reason=kwargs.get("reason") or "")}

    def delete_memory_topic(self, topic_id: str, **kwargs) -> dict:
        if not kwargs.get("confirm"):
            raise InvalidInput("删除记忆必须显式确认（confirm=true）",
                               field_errors={"confirm": "required"})
        return {"topic": self.memory.topics.delete(topic_id, reason=kwargs.get("reason") or "")}

    def withdraw_memory_log(self, log_id: str, **kwargs) -> dict:
        return {"log": self.memory.logs.withdraw(log_id, reason=kwargs.get("reason") or "")}

    def delete_memory_log(self, log_id: str, **kwargs) -> dict:
        if not kwargs.get("confirm"):
            raise InvalidInput("删除记忆必须显式确认（confirm=true）",
                               field_errors={"confirm": "required"})
        return {"log": self.memory.logs.delete(log_id, reason=kwargs.get("reason") or "")}

    def memory_index(self) -> dict:
        return {"index": self.memory.index_text()}

    def memory_rebuild_index(self) -> dict:
        return {"index": self.memory.rebuild_index()}

    def memory_recall(self, query: str = "", workspace_id: str | None = None,
                      session_id: str | None = None, limit: int | None = None) -> dict:
        if not (query or "").strip():
            raise InvalidInput("query 必填", field_errors={"query": "required"})
        return {"recall": self.memory.recall(query, workspace_id=workspace_id,
                                             session_id=session_id, limit=limit)}

    def memory_history(self, run_id: str | None = None, limit: int = 200) -> dict:
        return self.memory.history(run_id=run_id, limit=int(limit or 200))

    def memory_helpful(self, recall_id: str, helpful: bool = True) -> dict:
        return {"recall": self.memory.mark_helpful(recall_id, bool(helpful))}

    def memory_conflicts(self) -> dict:
        return self.memory.conflicts()

    def memory_sessions(self, limit: int = 100) -> dict:
        return {"sessions": self.memory.working.list_sessions(limit=int(limit or 100))}

    def close_memory_session(self, session_id: str | None = None, **_kwargs) -> dict:
        return {"session": self.memory.working.close(session_id)}

    def memory_dream_start(self, **kwargs) -> dict:
        return self.memory.dream.enqueue(trigger=kwargs.get("trigger") or "manual",
                                        idempotency_key=kwargs.get("idempotency_key"))

    def memory_dream_preview(self) -> dict:
        return {"gate": self.memory.dream.gate(trigger="auto"),
                "jobs": self.memory.dream.list_jobs(limit=20)}

    def memory_dream_run(self, dream_job_id: str) -> dict:
        return {"dream": self.memory.dream.run(dream_job_id)}

    def memory_expire(self, days: int | None = None) -> dict:
        return {"expire": self.memory.expire(days=days)}

    def memory_export(self, dest: str | None = None, include_body: bool = True) -> dict:
        return self.memory.export(dest=dest, include_body=bool(include_body))

    # ------------------------------------------------------------------ 指标与审计
    def metrics(self, workspace_id: str | None = None, skill_id: str | None = None) -> dict:
        return {"metrics": self.metrics_service.dashboard(workspace_id=workspace_id, skill_id=skill_id)}

    def list_audit(self, limit: int = 200, entity_id: str | None = None, action: str | None = None) -> dict:
        return {"audit": self.events.list_audit(limit=int(limit or 200), entity_id=entity_id, action=action)}

    # ------------------------------------------------------------------ 数据
    def data_usage(self) -> dict:
        usage = dict(self.data.usage())
        usage.update(self.config.disk_usage())
        return {"usage": usage}

    def backup(self, dest: str | None = None, include_body: bool = True, **_kwargs) -> dict:
        target = dest or str(self.config.export_dir / f"backup-{utc_now().replace(':', '').replace('.', '')}")
        return self.data.backup(target, include_body=bool(include_body))

    def restore(self, backup_path: str, dry_run: bool = True, **_kwargs) -> dict:
        return self.data.restore(backup_path, dry_run=bool(dry_run))

    def export_bundle(self, dest: str | None = None, scope: dict | None = None,
                      include_body: bool = True, **_kwargs) -> dict:
        target = dest or str(self.config.export_dir / f"export-{utc_now().replace(':', '').replace('.', '')}")
        return self.data.export_bundle(target, scope=scope or {}, include_body=bool(include_body))

    def clear_data(self, scope: dict | None = None, confirm: bool = False, **_kwargs) -> dict:
        return self.data.clear_data(scope or {}, confirm=bool(confirm))

    def preview_clear(self, scope: dict | None = None) -> dict:
        return self.data.preview_clear(scope or {})

    # ------------------------------------------------------------------ 正文保留期限
    def retention_policy(self) -> dict:
        return {"retention": self.retention.policy()}

    def set_retention(self, days: int | None = None, **_kwargs) -> dict:
        return {"retention": self.retention.set_policy(None if days in (None, 0) else int(days))}

    def retention_preview(self, days: int | None = None) -> dict:
        return self.retention.preview(None if days in (None, 0) else int(days))

    def apply_retention(self, days: int | None = None, confirm: bool = False,
                        purge_artifacts: bool = False, **_kwargs) -> dict:
        return self.retention.apply(None if days in (None, 0) else int(days), confirm=bool(confirm),
                                    purge_artifacts=bool(purge_artifacts))

    def evidence_state(self, run_id: str) -> dict:
        return self.retention.evidence_state(run_id)


# ---------------------------------------------------------------------- 辅助
def _learning_wanted(feedback: dict) -> bool:
    """是否把这条反馈作为可学习事件入队：模型复盘默认不自动触发学习。"""
    if not feedback:
        return False
    if str(feedback.get("source_kind") or "user") == "model":
        return False
    return True


def _safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # pragma: no cover - 汇总视图不应因单块失败而整体失败
        return []


def _decode_json(row: dict, fields: tuple[str, ...]) -> dict:
    for field in fields:
        value = row.get(field)
        if isinstance(value, str):
            try:
                row[field] = json.loads(value)
            except (TypeError, ValueError):
                pass
    return row
