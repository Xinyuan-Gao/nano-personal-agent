from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core import paths as pathmod
from ..core.blobs import BlobStore
from ..core.clock import parse_ts, seconds_since, utc_now
from ..core.config import DEFAULT_RUN_BUDGET
from ..core.errors import AppError, InvalidInput
from ..core.ids import canonical_hash, new_id
from ..core.repository import row_to_dict, rows_to_dicts
from .tools import ToolContext, ToolRegistry

#: 任务执行状态机
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "budget_stopped"})
#: 循环可以正常返回的终态（``awaiting_input`` 是等待用户补充信息 / 授权，不是终态）
LOOP_OUTCOMES = TERMINAL_STATUSES | {"awaiting_input"}
#: 允许的状态迁移；未列出的一律拒绝（防止越权跳转）
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelled", "failed", "interrupted"},
    "running": {"awaiting_input", "cancelling", "completed", "failed", "budget_stopped", "interrupted"},
    "awaiting_input": {"running", "cancelling", "cancelled", "failed"},
    "cancelling": {"cancelled", "failed", "budget_stopped"},
    "interrupted": {"running", "awaiting_input", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "budget_stopped": set(),
}
#: 终态 → 作业终态（queue.TERMINAL_STATUSES 子集）
JOB_STATUS_BY_RUN_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "budget_stopped": "budget_stopped",
}
#: 文本动作兜底解析用的键（避免 skills / platform 之外的隐式依赖）
_INPUT_SNAPSHOT_KEYS = ("request", "completion_criteria", "workspace", "input_files", "skills",
                        "tools", "budget", "context_hash")
MODEL_SNAPSHOT_KEYS = ("provider", "base_url", "model", "temperature", "max_output_tokens",
                       "timeout_s", "api_key_ref", "extra", "fingerprint")
_FEEDBACK_COLUMNS = ("id", "run_id", "type", "verdict", "scope", "target_skill_id",
                     "target_version_id", "source_kind", "status", "content", "created_at")
#: ``update_action`` 允许更新的列白名单（列名不来自外部输入，仅做显式校验）
_IDENT = {
    name: name
    for name in ("arguments", "permission_snapshot", "result", "artifact_refs", "finished_at",
                 "status", "side_effect_status", "error_code", "duration_ms", "attempt",
                 "tool_name", "started_at")
}


@dataclass
class RunSnapshot:
    """冻结的不可变输入快照。运行中生效版本变化不影响它。"""

    request: str
    workspace_id: str
    completion_criteria: list = field(default_factory=list)
    input_files: list = field(default_factory=list)
    model: dict = field(default_factory=dict)
    skills: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    context_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "request": self.request,
            "workspace_id": self.workspace_id,
            "completion_criteria": list(self.completion_criteria or []),
            "input_files": list(self.input_files or []),
            "model": dict(self.model or {}),
            "skills": dict(self.skills or {}),
            "tools": list(self.tools or []),
            "budget": dict(self.budget or {}),
            "context_hash": self.context_hash,
        }


class RunService:
    def __init__(self, store, repo, events, idem, queue, skills, selector, model_factory,
                 budget, container, config) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.idem = idem
        self.queue = queue
        self.skills = skills
        self.selector = selector
        self.model_factory = model_factory
        self.budget = budget
        self.container = container
        self.config = config
        try:
            self.settings = getattr(config, "settings", None)
        except Exception:  # pragma: no cover - 兜底
            self.settings = None
        try:
            self.blobs = BlobStore(Path(config.blob_dir))
        except Exception:  # pragma: no cover - 制品库不可用时仍允许记录动作
            self.blobs = None

    # ================================================================== 创建
    def create_run(
        self,
        *,
        request: str,
        workspace_id: str,
        completion_criteria=None,
        skill_ids=None,
        budget: dict | None = None,
        parent_run_id: str | None = None,
        profile_id: str = "local",
        idempotency_key: str | None = None,
        allow_existing_write: bool = False,
        learning_enabled: bool = True,
        origin: str = "user",
        eval_job_id: str | None = None,
        artifacts_dir: str | None = None,
        skill_versions: dict | None = None,
    ) -> dict:
        """冻结快照 + 入队；正常情况下 1 秒内返回 run_id。

        - ``learning_enabled=False`` 与 ``origin="evaluation"`` 供评测使用
          评测 run 关闭学习、不产生新候选。
        - ``artifacts_dir`` 允许评测为两组指定**互相隔离**的初始环境。
        - ``skill_versions={skill_id: version_id}`` 把本次 run 钉在指定技能版本上。
        """
        started = time.monotonic()
        request = (request or "").strip()
        if not request:
            raise InvalidInput("任务请求不能为空", field_errors={"request": "required"})
        if not workspace_id:
            raise InvalidInput("workspace_id 不能为空", field_errors={"workspace_id": "required"})
        criteria = _normalize_criteria(completion_criteria)
        requested_skills = [str(s) for s in (skill_ids or [])]
        if parent_run_id:
            parent = self.repo.get("run", parent_run_id)
            if parent is None:
                raise InvalidInput(
                    f"父任务不存在: {parent_run_id}", field_errors={"parent_run_id": "not_found"}
                )
        budget_defaults = self._budget_defaults()
        merged_budget = dict(budget_defaults)
        merged_budget.update({k: v for k, v in (budget or {}).items() if v is not None})
        from ..platform.budget import BudgetSpec  # 局部导入：避免 runtime 依赖 platform 的导入顺序

        spec = BudgetSpec.from_dict(merged_budget)

        payload = {
            "request": request,
            "workspace_id": workspace_id,
            "completion_criteria": criteria,
            "skill_ids": requested_skills,
            "budget": spec.to_dict(),
            "parent_run_id": parent_run_id,
            "profile_id": profile_id,
            "allow_existing_write": bool(allow_existing_write),
            "learning_enabled": bool(learning_enabled),
            "origin": origin or "user",
            "eval_job_id": eval_job_id,
            "artifacts_dir": artifacts_dir,
            "skill_versions": dict(skill_versions or {}),
        }

        def _create() -> dict:
            return self._create_run_row(
                request=request,
                workspace_id=workspace_id,
                criteria=criteria,
                requested_skills=requested_skills,
                spec=spec,
                parent_run_id=parent_run_id,
                profile_id=profile_id,
                allow_existing_write=bool(allow_existing_write),
                learning_enabled=bool(learning_enabled),
                origin=origin or "user",
                eval_job_id=eval_job_id,
                override_artifacts_dir=artifacts_dir,
                skill_versions=dict(skill_versions or {}),
            )

        result = self.idem.run("run.create", idempotency_key, payload, _create)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("run_id", result.get("id"))
            result["create_duration_ms"] = elapsed_ms
            result["deadline_met"] = elapsed_ms <= 1000
        return result

    def _create_run_row(self, *, request, workspace_id, criteria, requested_skills, spec,
                        parent_run_id, profile_id, allow_existing_write,
                        learning_enabled: bool = True, origin: str = "user",
                        eval_job_id: str | None = None, override_artifacts_dir: str | None = None,
                        skill_versions: dict | None = None) -> dict:
        workspace = self._require_workspace(workspace_id)
        run_id = new_id("run")
        now = utc_now()

        # 先落占位 run 行：`skill_load` 有 run_id 外键，技能加载记录必须在其后写入
        # （要求真实注入可追溯）。冻结成功后立即补齐快照
        # 任何失败都会清理占位行，不留半状态。
        with self.store.tx() as conn:
            self.repo.insert(
                "run",
                {
                    "id": run_id,
                    "profile_id": profile_id,
                    "workspace_id": workspace_id,
                    "parent_run_id": parent_run_id,
                    "request": request,
                    "completion_criteria": json.dumps(criteria, ensure_ascii=False),
                    "skill_ids": "[]",
                    "input_snapshot": "{}",
                    "model_snapshot": "{}",
                    "tools_snapshot": "[]",
                    "permissions": "{}",
                    "budget": json.dumps(spec.to_dict(), ensure_ascii=False),
                    "execution_status": "queued",
                    "outcome": "unknown",
                    "context_hash": "pending",
                    "origin": origin or "user",
                    "eval_job_id": eval_job_id,
                    "learning_enabled": 1 if learning_enabled else 0,
                    "output_text": "",
                    "cancel_requested": 0,
                    "checkpoint": "{}",
                    "usage": "{}",
                    "created_at": now,
                    "updated_at": now,
                },
                conn=conn,
            )

        try:
            model_snapshot = self._freeze_model()
            skills_snapshot = self._freeze_skills(
                request, workspace_id=workspace_id, profile_id=profile_id,
                requested_skills=requested_skills, run_id=run_id, pinned=skill_versions,
            )
            permissions = self._freeze_permissions(
                workspace, run_id=run_id, allow_existing_write=allow_existing_write,
                override_artifacts_dir=override_artifacts_dir,
            )
            run_artifacts_dir = Path(permissions["artifacts_dir"])
            run_artifacts_dir.mkdir(parents=True, exist_ok=True)

            input_files = self._input_inventory(workspace)
            tools_snapshot = self._freeze_tools(workspace, permissions, run_artifacts_dir)
            # 只有真正可用的工具能进入白名单，避免冻结出不可执行的动作
            permissions["tools"] = list(tools_snapshot)

            context_hash = canonical_hash(
                {
                    "request": request,
                    "completion_criteria": criteria,
                    "workspace_id": workspace_id,
                    "workspace_root": workspace.get("root_path"),
                    "input_files": input_files,
                    "model": model_snapshot,
                    "skills": skills_snapshot,
                    "tools": tools_snapshot,
                    "budget": spec.to_dict(),
                    "permissions": {
                        "read_roots": permissions.get("read_roots"),
                        "write_roots": permissions.get("write_roots"),
                        "artifacts_dir": permissions.get("artifacts_dir"),
                        "tools": permissions.get("tools"),
                        "network": permissions.get("network"),
                        "scripts": permissions.get("scripts"),
                        "allow_existing_write": permissions.get("allow_existing_write"),
                    },
                }
            )
            input_snapshot = {
                "request": request,
                "completion_criteria": criteria,
                "workspace": {
                    "id": workspace.get("id"),
                    "name": workspace.get("name"),
                    "root_path": workspace.get("root_path"),
                    "artifact_path": workspace.get("artifact_path"),
                    "status": workspace.get("status"),
                    "root_fingerprint": workspace.get("root_fingerprint"),
                },
                "input_files": input_files,
                "skills": skills_snapshot,
                "tools": tools_snapshot,
                "budget": spec.to_dict(),
                "context_hash": context_hash,
            }
            checkpoint = {
                "phase": "queued",
                "note": "输入快照已冻结；恢复时不得混用运行中变更的模型 / 技能版本",
                "context_hash": context_hash,
                "next_action_seq": 1,
                "next_step_seq": 1,
            }

            with self.store.tx() as conn:
                self.repo.update(
                    "run",
                    run_id,
                    {
                        "parent_run_id": parent_run_id,
                        "request": request,
                        "completion_criteria": json.dumps(criteria, ensure_ascii=False),
                        "skill_ids": json.dumps(
                            [s["skill_id"] for s in skills_snapshot.get("chosen", []) if s.get("skill_id")],
                            ensure_ascii=False,
                        ),
                        "input_snapshot": json.dumps(input_snapshot, ensure_ascii=False, default=str),
                        "model_snapshot": json.dumps(model_snapshot, ensure_ascii=False, default=str),
                        "tools_snapshot": json.dumps(tools_snapshot, ensure_ascii=False, default=str),
                        "permissions": json.dumps(permissions, ensure_ascii=False, default=str),
                        "budget": json.dumps(spec.to_dict(), ensure_ascii=False),
                        "execution_status": "queued",
                        "outcome": "unknown",
                        "context_hash": context_hash,
                        "origin": origin or "user",
                        "eval_job_id": eval_job_id,
                        "learning_enabled": 1 if learning_enabled else 0,
                        "output_text": "",
                        "cancel_requested": 0,
                        "checkpoint": json.dumps(checkpoint, ensure_ascii=False, default=str),
                        "usage": "{}",
                        "updated_at": utc_now(),
                    },
                    conn=conn,
                )
                self.events.run_event(
                    run_id,
                    "run.created",
                    {
                        "context_hash": context_hash,
                        "workspace_id": workspace_id,
                        "skills": [s.get("version_id") for s in skills_snapshot.get("chosen", [])],
                        "pinned_versions": dict(skill_versions or {}),
                        "tools": tools_snapshot,
                        "budget": spec.to_dict(),
                        "parent_run_id": parent_run_id,
                        "origin": origin or "user",
                        "learning_enabled": bool(learning_enabled),
                    },
                    conn=conn,
                )
                self.events.audit(
                    "run.create",
                    "run",
                    run_id,
                    after={"execution_status": "queued", "context_hash": context_hash,
                           "origin": origin or "user"},
                    permission_scope=_permission_audit_view(permissions),
                    conn=conn,
                )
        except BaseException:
            self._discard_placeholder_run(run_id)
            raise

        # 预算账本：调用前按上界预留
        self.budget.open(run_id, spec)
        # 步骤计划在创建时就落盘（重启后可判断最后已完成动作及未决动作）
        self._init_plan(run_id, criteria)
        # 加载记录：真实 SkillSelector 会在 load_context 内写库，测试替身不会；
        # 这里按 (version_id, file_path) 去重补齐，保证任何实现下都只记一次。
        existing_loads = {
            (row["version_id"], row["file_path"])
            for row in self.store.query(
                "SELECT version_id, file_path FROM skill_load WHERE run_id = ?", (run_id,)
            )
        }
        for load in skills_snapshot.get("pending_loads") or []:
            key = (str(load.get("version_id") or ""), str(load.get("file_path") or "SKILL.md"))
            if not key[0] or key in existing_loads:
                continue
            try:
                self.record_skill_load(run_id, load)
                existing_loads.add(key)
            except Exception as exc:  # 登记失败必须可见
                self.events.run_event(run_id, "run.skill_load_failed",
                                      {"version_id": key[0], "error": str(exc)})
        for failure in skills_snapshot.get("load_failures") or []:
            self.events.run_event(run_id, "run.skill_load_failed", failure)
        job_id = None
        if self.queue is not None:
            job_id = self.queue.enqueue(
                "run.execute",
                {"run_id": run_id, "workspace_id": workspace_id},
                idempotency_key=f"run.execute:{run_id}",
                budget=spec.to_dict(),
                run_id=run_id,
            )
        return {
            "run_id": run_id,
            "id": run_id,
            "job_id": job_id,
            "status": "queued",
            "execution_status": "queued",
            "outcome": "unknown",
            "context_hash": context_hash,
            "created": True,
        }

    def _discard_placeholder_run(self, run_id: str) -> None:
        """清理冻结失败留下的占位行（含已写入的加载记录）。"""
        try:
            with self.store.tx() as conn:
                self.repo.delete("skill_load", {"run_id": run_id}, conn=conn)
                self.repo.delete("run_event", {"run_id": run_id}, conn=conn)
                self.repo.delete("run", {"id": run_id}, conn=conn)
        except Exception:  # pragma: no cover - 清理失败不掩盖原始异常
            pass

    # ================================================================== 执行
    def execute(self, run_id: str, worker_id: str) -> None:
        """worker handler：执行 run 并落终态；异常一律转成明确终态（不打死 worker）。"""
        row = self.repo.get("run", run_id)
        if row is None:
            raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})
        status = row["execution_status"]
        if status in TERMINAL_STATUSES:
            self._finish_job(run_id, status, error_code=row["error_code"])
            return
        if _truthy(row["cancel_requested"]):
            final = self._transition(run_id, "cancelled", reason="取消请求在派发前生效",
                                     error_code="cancelled")
            self._finish_job(run_id, final, error_code="cancelled")
            return

        # 原子抢占执行权：评测 worker 与队列 worker 可能同时拿到同一 run，
        # 重复执行会导致同一动作被执行两次与非法状态迁移。
        claimed, claim_state = self._claim_execution(run_id, worker_id)
        if not claimed:
            if claim_state in TERMINAL_STATUSES:
                self._finish_job(run_id, claim_state, error_code=row["error_code"])
            else:
                self.events.run_event(
                    run_id, "run.execute_skipped",
                    {"worker": worker_id, "state": claim_state,
                     "reason": "同一任务已有执行者，跳过重复执行"},
                )
            return

        self._transition(run_id, "running", reason=f"worker {worker_id} 接受任务")
        outcome = "failed"
        error_code: str | None = None
        error_message: str | None = None
        final = "failed"

        from .loop import AgentLoop  # 局部导入，避免与 loop 的循环依赖

        try:
            snapshot = self.snapshot_of(run_id)
            permissions = self._permissions_of(run_id)
            artifacts_dir = Path(permissions.get("artifacts_dir")
                                 or self.config.workspace_artifacts_dir(snapshot.workspace_id) / run_id)
            from ..platform.budget import BudgetSpec

            self.budget.open(run_id, BudgetSpec.from_dict(snapshot.budget or DEFAULT_RUN_BUDGET))
            workspace = self._workspace_of(run_id, snapshot)
            ctx = ToolContext(
                run_id=run_id,
                workspace=workspace,
                permissions=permissions,
                artifacts_dir=artifacts_dir,
                container=self.container,
                input_roots=[Path(p) for p in (permissions.get("read_roots") or [])],
                blobs=self.blobs,
            )
            registry = ToolRegistry(ctx)
            model = self._make_model()
            if model is None:
                raise AppError("模型未配置：请先在设置中配置模型连接", code="model_error")
            loop = AgentLoop(
                self,
                registry,
                model,
                self.budget,
                self.events,
                self.repo,
                self.container,
                artifacts=self.blobs,
                artifact_persister=lambda item: self.record_artifact(run_id, item),
            )
            final = loop.run(run_id)
            outcome = final
        except AppError as exc:
            error_code, error_message = exc.code, exc.message
            self._record_error(run_id, error_code, error_message)
        except Exception as exc:  # pragma: no cover - 非契约异常兜底，保证终态
            error_code, error_message = "internal_error", str(exc)
            self._record_error(run_id, error_code, error_message)

        if outcome not in LOOP_OUTCOMES:
            outcome = "failed"
            error_code = error_code or "invalid_state"
            error_message = error_message or "循环返回了非法终态"
        final = outcome

        applied = self._transition(run_id, final, reason=error_message, error_code=error_code)
        try:
            self.budget.close(run_id, applied)
        except Exception:  # pragma: no cover - 预算关闭失败不影响终态
            pass
        self._finish_job(run_id, applied, error_code=error_code, error_message=error_message)
        self._release_claim(run_id, worker_id)

    # ================================================================== 执行权
    def _claim_execution(self, run_id: str, worker_id: str,
                         lease_seconds: int = 1800) -> tuple[bool, str]:
        """原子抢占执行权；返回 ``(是否抢到, 当前状态或原因)``。"""
        from datetime import datetime, timedelta, timezone

        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT execution_status, cancel_requested, checkpoint FROM run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})
            status = row["execution_status"]
            if status in TERMINAL_STATUSES:
                return False, status
            if _truthy(row["cancel_requested"]):
                return False, "cancelling"
            checkpoint = _json_load(row["checkpoint"], {})
            holder = checkpoint.get("active_worker")
            if holder and holder != worker_id:
                expires = parse_ts(checkpoint.get("claim_expires_at"))
                if expires is None or expires > datetime.now(timezone.utc):
                    return False, "running_elsewhere"
            checkpoint["active_worker"] = worker_id
            checkpoint["claim_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            checkpoint["claimed_at"] = utc_now()
            conn.execute(
                "UPDATE run SET checkpoint = ?, updated_at = ? WHERE id = ?",
                (json.dumps(checkpoint, ensure_ascii=False, default=str), utc_now(), run_id),
            )
            self.events.run_event(
                run_id, "run.claimed",
                {"worker": worker_id, "lease_seconds": lease_seconds}, conn=conn,
            )
        return True, "claimed"

    def _release_claim(self, run_id: str, worker_id: str | None = None) -> None:
        """释放执行权（幂等）；持有者不匹配时不覆盖新的持有者。"""
        try:
            with self.store.tx() as conn:
                row = conn.execute("SELECT checkpoint FROM run WHERE id = ?", (run_id,)).fetchone()
                if row is None:
                    return
                checkpoint = _json_load(row["checkpoint"], {})
                holder = checkpoint.get("active_worker")
                if holder and worker_id and holder != worker_id:
                    return
                checkpoint.pop("active_worker", None)
                checkpoint.pop("claim_expires_at", None)
                conn.execute(
                    "UPDATE run SET checkpoint = ? WHERE id = ?",
                    (json.dumps(checkpoint, ensure_ascii=False, default=str), run_id),
                )
        except Exception:  # pragma: no cover - 释放失败不影响终态
            pass

    # ================================================================== 取消
    def cancel(self, run_id: str) -> dict:
        """请求取消：queued 直接取消；running 置 cancel_requested。"""
        row = self.repo.get("run", run_id)
        if row is None:
            raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})
        status = row["execution_status"]
        if status in TERMINAL_STATUSES:
            return {"run_id": run_id, "execution_status": status, "cancel_requested":
                    _truthy(row["cancel_requested"]), "already_terminal": True}
        if status == "queued":
            final = self._transition(run_id, "cancelled", reason="任务在派发前被取消",
                                     error_code="cancelled", set_cancel_requested=True)
            self._cancel_job(run_id)
            return {"run_id": run_id, "execution_status": final, "cancel_requested": True,
                    "already_terminal": False, "dispatch_stopped": True}
        if status in ("cancelling",):
            return {"run_id": run_id, "execution_status": status, "cancel_requested": True,
                    "already_terminal": False, "dispatch_stopped": True}
        if status == "interrupted":
            final = self._transition(run_id, "cancelled", reason="中断任务被取消",
                                     error_code="cancelled", set_cancel_requested=True)
            self._cancel_job(run_id)
            return {"run_id": run_id, "execution_status": final, "cancel_requested": True,
                    "already_terminal": False, "dispatch_stopped": True}
        # running / awaiting_input → cancelling：循环在安全检查点停止派发（≤2s）
        final = self._transition(run_id, "cancelling", reason="用户取消：停止派发新动作",
                                 set_cancel_requested=True)
        self._cancel_job(run_id)
        return {"run_id": run_id, "execution_status": final, "cancel_requested": True,
                "already_terminal": False, "dispatch_stopped": True}

    # ================================================================== 恢复
    def resume(self, run_id: str, *, confirm_safe: bool = False) -> dict:
        """中断恢复：先核对未决副作用，可安全继续才续跑。"""
        row = self.repo.get("run", run_id)
        if row is None:
            raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})
        status = row["execution_status"]
        pending = self.pending_side_effects(run_id)

        if status in ("completed", "cancelled"):
            return {"run_id": run_id, "execution_status": status, "resumed": False,
                    "requires_review": False, "pending_side_effects": pending,
                    "reason": "任务已处于终态，如需重跑请新建 run"}
        if status == "budget_stopped":
            return {"run_id": run_id, "execution_status": status, "resumed": False,
                    "requires_review": False, "pending_side_effects": pending,
                    "reason": "预算已耗尽且账本保留用量，请新建 run 并调整预算"}
        if status == "failed" and pending:
            return {"run_id": run_id, "execution_status": status, "resumed": False,
                    "requires_review": True, "pending_side_effects": pending,
                    "reason": "存在结果未知的副作用动作，请先核对产物后再新建 run"}
        if status == "failed":
            return {"run_id": run_id, "execution_status": status, "resumed": False,
                    "requires_review": False, "pending_side_effects": pending,
                    "reason": "任务已失败，请新建 run"}

        if pending and not confirm_safe:
            self._transition(
                run_id, "awaiting_input",
                reason="存在结果未知的副作用动作，需人工核对后才能继续",
                set_checkpoint={
                    "pending_side_effects": pending,
                    "awaiting_reason": "pending_side_effect",
                    "resume_kind": "requires_review",
                },
            )
            self.events.run_event(
                run_id,
                "run.resume_blocked",
                {"pending_side_effects": pending, "reason": "side_effect_status=unknown"},
            )
            return {
                "run_id": run_id,
                "execution_status": "awaiting_input",
                "resumed": False,
                "requires_review": True,
                "pending_side_effects": pending,
                "reason": "存在结果未知的副作用动作；请核对产物后再次 resume(confirm_safe=True)，"
                          "或新建 run（不允许直接重放）",
            }

        if status in ("interrupted", "awaiting_input"):
            self._transition(run_id, "running", reason="确认可安全继续，记录恢复点",
                             set_checkpoint={"resumed_at": utc_now(), "resume_confirmed": bool(confirm_safe)})
        self.budget.open(run_id, self.snapshot_of(run_id).budget or DEFAULT_RUN_BUDGET)
        job_id = None
        if self.queue is not None:
            job_id = self.queue.enqueue(
                "run.execute",
                {"run_id": run_id, "resume": True},
                idempotency_key=f"run.resume:{run_id}:{utc_now()}",
                run_id=run_id,
            )
        self.events.run_event(run_id, "run.resumed",
                              {"confirm_safe": bool(confirm_safe), "job_id": job_id})
        return {"run_id": run_id, "execution_status": "running", "resumed": True,
                "requires_review": False, "pending_side_effects": pending, "job_id": job_id}

    def pending_side_effects(self, run_id: str) -> list[dict]:
        """未决副作用动作：状态未落定或 ``side_effect_status='unknown'``。"""
        self._require_run_row(run_id)
        rows = self.store.query(
            "SELECT id, seq, action_id, tool_name, status, side_effect_status, started_at "
            "FROM action_record WHERE run_id = ? AND (status = 'running' "
            "OR side_effect_status = 'unknown') ORDER BY seq ASC",
            (run_id,),
        )
        return [row_to_dict(r) for r in rows]

    # ================================================================== 查询
    def get_run(self, run_id) -> dict:
        """任务详情：快照 + 事件 / 动作 / 产物 / 技能加载 / 反馈汇总。"""
        run = self._require_run_row(run_id)
        data = self._run_dict(run)
        data["events"] = self.get_events(run_id)
        data["actions"] = self.get_actions(run_id)
        data["artifacts"] = self.get_artifacts(run_id)
        data["loads"] = self.get_loads(run_id)
        data["feedback"] = self.get_feedback(run_id)
        data["feedback_summary"] = _feedback_summary(data["feedback"])
        data["steps"] = self.get_steps(run_id)
        data["pending_side_effects"] = self.pending_side_effects(run_id)
        return data

    def list_runs(self, *, workspace_id: str | None = None, status: str | None = None,
                  limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM run"
        clauses: list[str] = []
        params: list = []
        if workspace_id:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if status:
            clauses.append("execution_status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        return [self._run_dict(r) for r in self.store.query(sql, params)]

    def get_events(self, run_id, after_seq: int = 0) -> list[dict]:
        self._require_run_row(run_id)
        return self.events.list_run_events(run_id, after_seq=after_seq)

    def get_actions(self, run_id) -> list[dict]:
        self._require_run_row(run_id)
        rows = self.store.query(
            "SELECT * FROM action_record WHERE run_id = ? ORDER BY seq ASC", (run_id,)
        )
        return _decode_rows(rows, ("arguments", "permission_snapshot", "result", "artifact_refs"))

    def get_artifacts(self, run_id) -> list[dict]:
        self._require_run_row(run_id)
        rows = self.store.query(
            "SELECT * FROM artifact WHERE run_id = ? ORDER BY created_at ASC, rowid ASC", (run_id,)
        )
        return _decode_rows(rows, ())

    def get_loads(self, run_id) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM skill_load WHERE run_id = ? ORDER BY created_at ASC, rowid ASC", (run_id,)
        )
        return _decode_rows(rows, ())

    def get_steps(self, run_id) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM run_step WHERE run_id = ? ORDER BY seq ASC", (run_id,)
        )
        return _decode_rows(rows, ("detail",))

    def get_feedback(self, run_id) -> list[dict]:
        """反馈汇总：learning 层未就绪时返回空列表，不阻断 run 详情（审计）。"""
        try:
            rows = self.store.query(
                f"SELECT {', '.join(_FEEDBACK_COLUMNS)} FROM feedback WHERE run_id = ? "
                "ORDER BY created_at ASC",
                (run_id,),
            )
        except Exception:  # pragma: no cover - 表不存在时按无反馈处理
            return []
        return [row_to_dict(r) for r in rows]

    def snapshot_of(self, run_id) -> RunSnapshot:
        run = self._require_run_row(run_id)
        data = self._run_dict(run)
        snap = data.get("input_snapshot") or {}
        return RunSnapshot(
            request=data["request"],
            workspace_id=data["workspace_id"],
            completion_criteria=snap.get("completion_criteria") or data.get("completion_criteria") or [],
            input_files=snap.get("input_files") or [],
            model=data.get("model_snapshot") or {},
            skills=snap.get("skills") or {},
            tools=data.get("tools_snapshot") or [],
            budget=data.get("budget") or {},
            context_hash=data.get("context_hash") or "",
        )

    # ---------------------------------------------------------- loop 协作接口
    def run_row(self, run_id: str) -> dict:
        return self._run_dict(self._require_run_row(run_id))

    def permissions_of(self, run_id: str) -> dict:
        return self._permissions_of(run_id)

    def workspace_of(self, run_id: str) -> dict:
        run = self._require_run_row(run_id)
        snap = self._run_dict(run).get("input_snapshot") or {}
        return dict(snap.get("workspace") or {})

    def cancel_requested(self, run_id: str) -> bool:
        row = self.repo.get("run", run_id)
        if row is None:
            return True
        return _truthy(row["cancel_requested"]) or row["execution_status"] in (
            "cancelling", "cancelled", "failed", "budget_stopped", "completed"
        )

    def save_step(self, run_id: str, seq: int, *, title=None, intent=None, status=None,
                  detail: dict | None = None) -> dict:
        """幂等写入 ``run_step``（同 run + seq 只更新状态与详情）。"""
        now = utc_now()
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT * FROM run_step WHERE run_id = ? AND seq = ?", (run_id, int(seq))
            ).fetchone()
            if row is None:
                step_id = new_id("step")
                conn.execute(
                    "INSERT INTO run_step (id, run_id, seq, title, intent, status, detail, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        step_id,
                        run_id,
                        int(seq),
                        title or "",
                        intent or "",
                        status or "pending",
                        json.dumps(detail or {}, ensure_ascii=False, default=str),
                        now,
                        now,
                    ),
                )
            else:
                merged = _json_load(row["detail"], {})
                merged.update(detail or {})
                conn.execute(
                    "UPDATE run_step SET title = ?, intent = ?, status = ?, detail = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        title if title is not None else row["title"],
                        intent if intent is not None else row["intent"],
                        status if status is not None else row["status"],
                        json.dumps(merged, ensure_ascii=False, default=str),
                        now,
                        row["id"],
                    ),
                )
                step_id = row["id"]
        return {"id": step_id, "run_id": run_id, "seq": int(seq)}

    def record_action(self, run_id: str, action: dict) -> str:
        """幂等写入动作记录：同 ``action_id`` 只落一行。"""
        action_id = str(action.get("action_id") or new_id("act"))
        with self.store.tx() as conn:
            existing = conn.execute(
                "SELECT id FROM action_record WHERE run_id = ? AND action_id = ?",
                (run_id, action_id),
            ).fetchone()
            if existing is not None:
                return existing["id"]
            seq = int(action.get("seq") or self._next_action_seq(conn, run_id))
            record_id = new_id("actrow")
            conn.execute(
                "INSERT INTO action_record (id, run_id, seq, action_id, tool_name, arguments, "
                "permission_snapshot, started_at, finished_at, status, side_effect_status, result, "
                "artifact_refs, attempt, error_code, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    run_id,
                    seq,
                    action_id,
                    str(action.get("tool_name") or ""),
                    json.dumps(action.get("arguments") or {}, ensure_ascii=False, default=str),
                    json.dumps(action.get("permission_snapshot") or {}, ensure_ascii=False, default=str),
                    action.get("started_at") or utc_now(),
                    action.get("finished_at"),
                    str(action.get("status") or "running"),
                    str(action.get("side_effect_status") or "unknown"),
                    _dump_limited(action.get("result")),
                    json.dumps(action.get("artifact_refs") or [], ensure_ascii=False, default=str),
                    int(action.get("attempt") or 1),
                    action.get("error_code"),
                    action.get("duration_ms"),
                ),
            )
        return record_id

    def update_action(self, run_id: str, action_id: str, **fields) -> None:
        """按 (run_id, action_id) 更新动作记录；action_id 不是主键，无需重复定位。"""
        if not fields:
            return
        payload: dict = {}
        for key, value in fields.items():
            if key in ("arguments", "permission_snapshot") and not isinstance(value, str):
                payload[key] = json.dumps(value or {}, ensure_ascii=False, default=str)
            elif key == "artifact_refs" and not isinstance(value, str):
                payload[key] = json.dumps(value or [], ensure_ascii=False, default=str)
            elif key == "result":
                payload[key] = _dump_limited(value)
            else:
                payload[key] = value
        payload.setdefault("finished_at", utc_now())
        sets = ", ".join(f"{_IDENT[k]} = ?" for k in payload)
        with self.store.tx() as conn:
            conn.execute(
                f"UPDATE action_record SET {sets} WHERE run_id = ? AND action_id = ?",
                tuple(payload.values()) + (run_id, action_id),
            )

    def record_artifact(self, run_id: str, item: dict) -> str | None:
        """幂等登记产物（同 run + path 只保留一条）。"""
        rel = str(item.get("path") or "")
        if not rel:
            return None
        blob_id = str(item.get("blob_id") or item.get("content_hash") or "")
        content_hash = str(item.get("content_hash") or blob_id)
        size = int(item.get("size") or 0)
        if self.blobs is not None and blob_id:
            try:
                if self.blobs.exists(blob_id):
                    raw = self.blobs.get_bytes(blob_id)
                    content_hash = content_hash or canonical_hash(raw)
                    size = size or len(raw)
            except Exception:  # pragma: no cover - 制品库读取失败按已知元数据记录
                pass
        with self.store.tx() as conn:
            existing = conn.execute(
                "SELECT id, content_hash FROM artifact WHERE run_id = ? AND path = ?",
                (run_id, rel),
            ).fetchone()
            if existing is not None:
                if content_hash and existing["content_hash"] != content_hash:
                    conn.execute(
                        "UPDATE artifact SET blob_id = ?, content_hash = ?, size = ? WHERE id = ?",
                        (blob_id, content_hash, size, existing["id"]),
                    )
                return existing["id"]
            artifact_id = new_id("art")
            conn.execute(
                "INSERT INTO artifact (id, run_id, path, blob_id, content_hash, size, kind, label, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    run_id,
                    rel,
                    blob_id,
                    content_hash,
                    size,
                    str(item.get("kind") or "output"),
                    item.get("label"),
                    utc_now(),
                ),
            )
        return artifact_id

    def record_skill_load(self, run_id: str, load: dict) -> str:
        """登记技能实际加载（只有真正注入上下文的内容才记 loaded）。"""
        with self.store.tx() as conn:
            load_id = new_id("load")
            conn.execute(
                "INSERT INTO skill_load (id, run_id, skill_id, version_id, file_path, content_hash, "
                "load_kind, chars, load_status, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    load_id,
                    run_id,
                    str(load.get("skill_id") or ""),
                    str(load.get("version_id") or ""),
                    str(load.get("file_path") or "SKILL.md"),
                    str(load.get("content_hash") or ""),
                    str(load.get("load_kind") or "full"),
                    int(load.get("chars") or 0),
                    str(load.get("load_status") or "loaded"),
                    load.get("reason"),
                    utc_now(),
                ),
            )
        return load_id

    def update_checkpoint(self, run_id: str, patch: dict) -> dict:
        merged: dict = {}
        with self.store.tx() as conn:
            row = conn.execute("SELECT checkpoint FROM run WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})
            merged = _json_load(row["checkpoint"], {})
            merged.update(patch or {})
            conn.execute(
                "UPDATE run SET checkpoint = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False, default=str), utc_now(), run_id),
            )
        return merged

    def set_output_text(self, run_id: str, text: str) -> None:
        self.repo.update("run", run_id, {"output_text": _clip(text, 20000), "updated_at": utc_now()})

    def add_usage(self, run_id: str, patch: dict) -> None:
        """合并写入 run.usage（调用数、未知计费、模型自述完成次数等）。"""
        with self.store.tx() as conn:
            row = conn.execute("SELECT usage FROM run WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return
            merged = _json_load(row["usage"], {})
            for key, value in (patch or {}).items():
                if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                    merged[key] = merged[key] + value
                else:
                    merged[key] = value
            conn.execute(
                "UPDATE run SET usage = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False, default=str), utc_now(), run_id),
            )

    def transition(self, run_id: str, status: str, *, reason: str | None = None,
                   error_code: str | None = None, checkpoint: dict | None = None) -> str:
        """对 loop 开放的状态迁移入口（幂等）。"""
        return self._transition(run_id, status, reason=reason, error_code=error_code,
                                set_checkpoint=checkpoint)

    # ================================================================== 内部
    def _transition(self, run_id: str, status: str, *, reason: str | None = None,
                    error_code: str | None = None, set_checkpoint: dict | None = None,
                    set_cancel_requested: bool = False) -> str:
        if status not in ALLOWED_TRANSITIONS and status not in TERMINAL_STATUSES:
            raise InvalidInput(f"非法任务状态: {status}", field_errors={"status": "invalid"})
        now = utc_now()
        with self.store.tx() as conn:
            row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})
            current = row["execution_status"]
            if current == status and not set_cancel_requested and not set_checkpoint:
                return current  # 幂等：重复迁移不重复写
            if current != status and status not in ALLOWED_TRANSITIONS.get(current, set()):
                raise InvalidInput(
                    f"非法状态迁移: {current} → {status}",
                    field_errors={"execution_status": "invalid_transition"},
                )
            checkpoint = _json_load(row["checkpoint"], {})
            checkpoint.update(set_checkpoint or {})
            checkpoint.setdefault("last_status", current)
            payload: dict = {"execution_status": status, "updated_at": now}
            checkpoint["last_status_at"] = now
            if set_cancel_requested:
                payload["cancel_requested"] = 1
            if status == "running" and not row["started_at"]:
                payload["started_at"] = now
            if status in TERMINAL_STATUSES:
                payload["finished_at"] = now
            if error_code is not None:
                payload["error_code"] = error_code
            if reason is not None:
                # error_message 只承载失败/等待类原因，避免正常迁移把说明写进错误字段
                if error_code is not None or status in ("failed", "budget_stopped",
                                                        "cancelled", "interrupted", "awaiting_input"):
                    payload["error_message"] = _clip(reason, 2000)
                else:
                    checkpoint["last_reason"] = _clip(reason, 2000)
            payload["checkpoint"] = json.dumps(checkpoint, ensure_ascii=False, default=str)
            self.repo.update("run", run_id, payload, conn=conn)
            self.events.run_event(
                run_id,
                f"run.{status}",
                {"from": current, "to": status, "reason": reason, "error_code": error_code},
                conn=conn,
            )
            self.events.audit(
                "run.transition",
                "run",
                run_id,
                before={"execution_status": current},
                after={"execution_status": status, "error_code": error_code},
                reason=reason,
                conn=conn,
            )
        return status

    def _record_error(self, run_id: str, error_code: str, message: str) -> None:
        try:
            self.events.run_event(run_id, "run.error",
                                  {"error_code": error_code, "message": _clip(message, 500)})
        except Exception:  # pragma: no cover
            pass

    def _finish_job(self, run_id: str, run_status: str, *, error_code=None,
                    error_message=None) -> None:
        if self.queue is None:
            return
        job_status = JOB_STATUS_BY_RUN_STATUS.get(run_status)
        if job_status is None:
            return
        try:
            jobs = self.queue.list(type="run.execute", limit=50)
        except Exception:  # pragma: no cover
            return
        for job in jobs:
            if (job.run_id == run_id or (job.payload or {}).get("run_id") == run_id) \
                    and not job.is_terminal():
                try:
                    self.queue.finish(job.id, job_status, result={"run_id": run_id,
                                                                  "run_status": run_status},
                                      error_code=error_code, error_message=error_message)
                except Exception:  # pragma: no cover
                    pass
                if job_status != "cancelled":
                    break

    def _cancel_job(self, run_id: str) -> None:
        if self.queue is None:
            return
        try:
            jobs = self.queue.list(type="run.execute", limit=50)
        except Exception:  # pragma: no cover
            return
        for job in jobs:
            if (job.run_id == run_id or (job.payload or {}).get("run_id") == run_id) \
                    and not job.is_terminal():
                try:
                    self.queue.cancel(job.id)
                except Exception:  # pragma: no cover
                    pass

    def _init_plan(self, run_id: str, criteria: list) -> list[dict]:
        """创建时的初始步骤计划（后续步骤由 loop 按需追加）。"""
        steps = [{"seq": 1, "title": "执行任务并产出结果",
                  "intent": "按请求执行并写入产物",
                  "detail": {"criteria": list(criteria or [])}}]
        for step in steps:
            self.save_step(run_id, step["seq"], title=step["title"], intent=step["intent"],
                           status="pending", detail=step["detail"])
        self.update_checkpoint(run_id, {"planned_steps": [s["title"] for s in steps],
                                        "next_step_seq": len(steps) + 1})
        return steps

    def _next_action_seq(self, conn, run_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM action_record WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row["s"]) + 1

    def _require_run_row(self, run_id):
        if not run_id:
            raise InvalidInput("run_id 不能为空", field_errors={"run_id": "required"})
        row = self.repo.get("run", run_id)
        if row is None:
            raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})
        return row

    def _run_dict(self, row) -> dict:
        data = row_to_dict(
            row,
            json_fields=("completion_criteria", "skill_ids", "input_snapshot", "model_snapshot",
                         "tools_snapshot", "permissions", "budget", "checkpoint", "usage"),
        ) or {}
        data["cancel_requested"] = _truthy(data.get("cancel_requested"))
        data["learning_enabled"] = bool(data.get("learning_enabled"))
        data["is_terminal"] = data.get("execution_status") in TERMINAL_STATUSES
        snapshot = data.get("input_snapshot") or {}
        data["snapshot"] = {
            "request": snapshot.get("request"),
            "completion_criteria": snapshot.get("completion_criteria") or [],
            "input_files": snapshot.get("input_files") or [],
            "skills": snapshot.get("skills") or {},
            "tools": snapshot.get("tools") or [],
            "budget": snapshot.get("budget") or {},
            "context_hash": snapshot.get("context_hash") or data.get("context_hash"),
        }
        return data

    def _permissions_of(self, run_id: str) -> dict:
        row = self._require_run_row(run_id)
        return _json_load(row["permissions"], {})

    def _workspace_of(self, run_id: str, snapshot: RunSnapshot | None = None) -> dict:
        run = self._require_run_row(run_id)
        snap = _json_load(run["input_snapshot"], {})
        return dict(snap.get("workspace") or {})

    def _require_workspace(self, workspace_id: str) -> dict:
        row = self.repo.get("workspace", workspace_id)
        if row is None:
            raise InvalidInput(f"工作区不存在: {workspace_id}", field_errors={"workspace_id": "not_found"})
        if row["status"] not in ("active", "removed"):
            raise InvalidInput(
                f"工作区不可用: {workspace_id}", field_errors={"workspace_id": row["status"]}
            )
        data = row_to_dict(row) or {}
        data["allowed_tools"] = _json_load(data.get("allowed_tools"), []) or []
        data["allow_network"] = _truthy(data.get("allow_network"))
        data["allow_scripts"] = _truthy(data.get("allow_scripts"))
        return data

    def _budget_defaults(self) -> dict:
        settings = self._settings_service()
        try:
            defaults = settings.get_budget_defaults()
            return dict(defaults.get("run") or DEFAULT_RUN_BUDGET)
        except Exception:  # pragma: no cover - 设置层不可用时用核心默认
            return dict(DEFAULT_RUN_BUDGET)

    def _settings_service(self):
        if self.settings is not None:
            return self.settings
        try:  # pragma: no cover - 兼容未注入 settings 的装配方式
            from ..platform.settings import SettingsService

            self.settings = SettingsService(self.store, self.repo, self.events, self.idem, None, self.config)
        except Exception:
            self.settings = None
        return self.settings

    def _freeze_permissions(self, workspace: dict, *, run_id: str, allow_existing_write: bool,
                            override_artifacts_dir: str | None = None) -> dict:
        settings = self._settings_service()
        effective: dict = {}
        try:
            if settings is not None:
                effective = settings.effective_permissions(workspace["id"]) or {}
        except Exception:  # pragma: no cover - 设置层异常时退回工作区行
            effective = {}
        tools = list(effective.get("tools") or workspace.get("allowed_tools") or [])
        read_roots = [str(p) for p in (effective.get("read_roots") or [workspace.get("root_path")]) if p]
        write_roots = [str(p) for p in (effective.get("write_roots") or [workspace.get("artifact_path")]) if p]
        base_artifacts = Path(write_roots[0]) if write_roots else self.config.workspace_artifacts_dir(
            workspace["id"]
        )
        # 评测两组必须使用互相隔离的初始环境
        artifacts_dir = Path(override_artifacts_dir) if override_artifacts_dir else base_artifacts / run_id
        return {
            "read_roots": read_roots,
            "write_roots": write_roots or [str(base_artifacts)],
            "base_artifacts_dir": str(base_artifacts),
            "artifacts_dir": str(artifacts_dir),
            # 只有用户在创建任务时显式授权才允许改既有文件
            "allow_existing_write": bool(allow_existing_write),
            "tools": tools,
            "network": bool(effective.get("network", workspace.get("allow_network"))),
            "scripts": bool(effective.get("scripts", workspace.get("allow_scripts"))),
            "container_required": True,
            "workspace_id": workspace["id"],
            "frozen_at": utc_now(),
        }

    def _freeze_tools(self, workspace: dict, permissions: dict, artifacts_dir: Path) -> list[str]:
        ctx = ToolContext(
            run_id="probe",
            workspace=workspace,
            permissions=permissions,
            artifacts_dir=artifacts_dir,
            container=self.container,
            input_roots=[Path(p) for p in (permissions.get("read_roots") or [])],
            blobs=self.blobs,
        )
        return sorted(ToolRegistry(ctx).available())

    def _freeze_model(self) -> dict:
        """模型配置描述 + 指纹（不含明文密钥）。"""
        client = self._make_model()
        if client is None:
            return {"configured": False, "fingerprint": None,
                    "error": "模型未配置", "frozen_at": utc_now()}
        note = ""
        try:
            data = dict(client.describe())
        except Exception as exc:  # pragma: no cover - describe 异常兜底
            data = {"provider": getattr(getattr(client, "config", None), "provider", None)}
            note = str(exc)
        # 冻结时确认实际生效的 current 技能/模型指针：这里只记录描述与指纹
        data.pop("api_key", None)
        data.setdefault("fingerprint", None)
        data["configured"] = bool(getattr(getattr(client, "config", None), "model", None)
                                  and data.get("fingerprint"))
        data["frozen_at"] = utc_now()
        if note:
            data["describe_note"] = note
        return data

    def _make_model(self):
        if self.model_factory is None:
            return None
        try:
            return self.model_factory()
        except Exception:  # pragma: no cover - 工厂异常按未配置处理
            return None

    def _freeze_skills(self, request: str, *, workspace_id: str, profile_id: str,
                       requested_skills: list[str], run_id: str,
                       pinned: dict | None = None) -> dict:
        """冻结技能包版本与文件 hash；同时登记 ``skill_load``。"""
        if self.skills is None or self.selector is None:
            if requested_skills:
                raise InvalidInput(
                    "本次运行指定了技能，但技能库不可用；请先导入技能包",
                    field_errors={"skill_ids": "unavailable"},
                )
            return {"chosen": [], "candidates": [], "skipped": [], "reason": "技能层不可用",
                    "loaded": [], "frozen_at": utc_now()}
        try:
            result = self.selector.choose(
                request,
                workspace_id=workspace_id,
                explicit_skill_ids=requested_skills or None,
                chosen_by="model" if not requested_skills else "user",
            )
        except Exception as exc:
            if requested_skills:
                raise
            return {"chosen": [], "candidates": [], "skipped": [], "reason": f"技能选择失败: {exc}",
                    "loaded": [], "frozen_at": utc_now()}

        chosen = [dict(c) for c in (getattr(result, "chosen", None) or [])]
        if pinned:
            # 钉扎的版本属于调用方已冻结并授权的技能包（评测 worker），
            # 即使其作用域与当前（隔离评测）工作区不同也必须加载，否则两组都拿不到方法、
            # 对照评测失去意义。权限仍由工作区决定，不因钉扎扩大。
            chosen = self._apply_pinned_versions(chosen, pinned, result)
            chosen = self._ensure_pinned_selected(chosen, pinned, result)
        candidates = [dict(c) for c in (getattr(result, "candidates", None) or [])]
        skipped = [dict(c) for c in (getattr(result, "skipped", None) or [])]
        loaded: list[dict] = []
        try:
            loads = self.selector.load_context(run_id, result=result) or []
            for load in loads:
                if isinstance(load, (list, tuple)) and len(load) >= 2:
                    load = {"version_id": load[0], "file_path": load[1]}
                loaded.append(dict(load))
        except Exception as exc:  # pragma: no cover - 加载失败必须可见
            loaded.append({"load_status": "failed", "reason": str(exc)})

        skill_map = {s.get("skill_id"): s for s in chosen}
        loaded_ok: list[dict] = []
        pending_loads: list[dict] = []
        for load in loaded:
            version_id = load.get("version_id")
            skill_id = load.get("skill_id") or _skill_id_for(skill_map, version_id)
            if not skill_id:
                continue
            entry = next((s for s in chosen if s.get("version_id") == version_id), None) or {}
            record = {
                "skill_id": skill_id,
                "version_id": version_id or entry.get("version_id"),
                "file_path": load.get("file_path") or "SKILL.md",
                "content_hash": load.get("content_hash") or entry.get("package_hash") or "",
                "load_kind": load.get("load_kind") or "full",
                "chars": int(load.get("chars") or 0),
                "load_status": load.get("load_status") or "loaded",
                "reason": load.get("reason"),
            }
            if not record["version_id"]:
                continue
            pending_loads.append(record)
            if record["load_status"] == "loaded":
                # 只有真正注入上下文的内容才作为「已加载」进入冻结快照。
                # 内容必须随快照冻结并注入 prompt：技能包存在制品库中，模型无法用文件工具读取。
                record["content"] = self._loaded_content(record)
                loaded_ok.append(record)
        return {
            "chosen": chosen,
            "candidates": candidates or chosen,
            "skipped": skipped,
            "reason": getattr(result, "reason", "") or "",
            "loaded": loaded_ok,
            "pending_loads": pending_loads,
            "frozen_at": utc_now(),
        }

    def _ensure_pinned_selected(self, chosen: list[dict], pinned: dict, result) -> list[dict]:
        """把被钉扎但未进入选择的技能补进选择结果（评测隔离工作区场景）。"""
        present = {str(entry.get("skill_id")) for entry in chosen}
        added = False
        for skill_id, version_id in (pinned or {}).items():
            if not version_id or str(skill_id) in present:
                continue
            entry: dict = {
                "skill_id": skill_id,
                "version_id": version_id,
                "pinned": True,
                "source": "pinned",
                "load_kind": "full",
                "reason": "按冻结版本钉扎加载（评测对照）",
            }
            if self.skills is not None:
                try:
                    skill = self.skills.get_skill(skill_id)
                    data = skill.get("skill", skill) if isinstance(skill, dict) else {}
                    entry.update({
                        "slug": data.get("slug"),
                        "name": data.get("name"),
                        "description": data.get("description"),
                        "scope": data.get("scope"),
                        "workspace_id": data.get("workspace_id"),
                    })
                    version = self.skills.get_version(version_id)
                    entry["package_hash"] = version.get("package_hash")
                    entry["version_label"] = version.get("version_label")
                except Exception:  # 元数据缺失不影响加载；加载失败会被记入 load_failures
                    pass
            chosen.append(entry)
            added = True
        if added:
            try:
                result.chosen = chosen
            except Exception:  # pragma: no cover
                pass
        return chosen

    def _loaded_content(self, record: dict, *, limit: int = 60_000) -> str:
        """取回已加载技能文件的正文（内容寻址制品库优先，其次从版本包读取）。"""
        text = ""
        content_hash = str(record.get("content_hash") or "")
        if content_hash and self.blobs is not None:
            try:
                text = self.blobs.get_text(content_hash)
            except Exception:
                text = ""
        if not text and self.skills is not None:
            try:
                text = self.skills.read_version_file(record["version_id"], record.get("file_path") or "SKILL.md")
            except Exception:
                text = ""
        if len(text) > limit:
            return text[:limit] + f"\n…（已按上下文预算截断，原文 {len(text)} 字符）"
        return text

    def _apply_pinned_versions(self, chosen: list[dict], pinned: dict, result) -> list[dict]:
        """把本次 run 钉在指定技能版本上（两组只有目标 skill 版本不同）。"""
        for entry in chosen:
            skill_id = entry.get("skill_id")
            target = pinned.get(skill_id)
            if not target or target == entry.get("version_id"):
                continue
            version = None
            if self.skills is not None:
                try:
                    version = self.skills.get_version(target)
                except Exception as exc:
                    raise InvalidInput(
                        f"钉扎的技能版本不存在: {target}",
                        field_errors={"skill_versions": "not_found"},
                    ) from exc
            if isinstance(version, dict) and version.get("skill_id") not in (None, skill_id):
                raise InvalidInput(
                    f"版本 {target} 不属于技能 {skill_id}",
                    field_errors={"skill_versions": "mismatch"},
                )
            entry["version_id"] = target
            entry["pinned"] = True
            if isinstance(version, dict):
                entry["package_hash"] = version.get("package_hash") or entry.get("package_hash")
                entry["version_label"] = version.get("version_label") or entry.get("version_label")
        try:  # 让 load_context 使用钉扎后的版本
            result.chosen = chosen
        except Exception:  # pragma: no cover - SelectionResult 不可变时忽略
            pass
        return chosen

    def _input_inventory(self, workspace: dict, limit: int = 200) -> list[dict]:
        """冻结输入文件 hash 清单（只列直接输入，跳过忽略路径与符号链接）。"""
        root = Path(workspace.get("root_path") or "")
        out: list[dict] = []
        if not root.is_dir():
            return out
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            return out
        for entry in entries:
            if len(out) >= limit:
                break
            try:
                rel = pathmod.safe_relpath(root, entry)
            except Exception:  # pragma: no cover
                continue
            if pathmod.is_ignored_path(rel) or entry.is_symlink() or entry.is_dir():
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            out.append({"path": rel, "size": int(stat.st_size), "mtime": int(stat.st_mtime)})
        return out


# ------------------------------------------------------------------ 辅助
def _normalize_criteria(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    raise InvalidInput("completion_criteria 必须是字符串或列表",
                       field_errors={"completion_criteria": "invalid"})


def _permission_audit_view(permissions: dict) -> dict:
    keep = ("read_roots", "write_roots", "artifacts_dir", "tools", "network", "scripts",
            "allow_existing_write")
    return {k: permissions.get(k) for k in keep if k in permissions}


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value not in ("", "0", "false", "False")
    return bool(value)


def _json_load(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _decode_rows(rows, json_fields: tuple[str, ...]) -> list[dict]:
    return rows_to_dicts(rows, json_fields=json_fields)


def _dump_limited(value, limit: int = 8000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover
            value = str(value)
    return _clip(value, limit)


def _clip(text, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def _skill_id_for(skill_map: dict, version_id) -> str | None:
    for skill_id, entry in skill_map.items():
        if entry.get("version_id") == version_id:
            return skill_id
    return None


def _feedback_summary(feedback: list[dict]) -> dict:
    summary: dict = {"count": len(feedback), "verdicts": {}, "types": {},
                     "outcome_sources": []}
    for item in feedback:
        verdict = item.get("verdict") or "none"
        summary["verdicts"][verdict] = summary["verdicts"].get(verdict, 0) + 1
        kind = item.get("type") or "unknown"
        summary["types"][kind] = summary["types"].get(kind, 0) + 1
    summary["outcome_source"] = "user" if feedback else "none"
    return summary
