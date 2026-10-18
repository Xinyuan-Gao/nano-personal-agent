from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

from ..core.blobs import BlobStore
from ..core.clock import utc_now
from ..core.config import DEFAULT_EVAL_BUDGET, DEFAULT_EVAL_REPETITIONS, DEFAULT_EVAL_RUN_BUDGET
from ..core.errors import (
    AppError,
    EvidenceUnavailable,
    InvalidInput,
    InvalidState,
    MissingDependency,
)
from ..core.ids import canonical_hash, new_id
from ..core.repository import row_to_dict
from ..platform.budget import spec_from_budget
from . import report as report_mod
from .judge import BlindJudge
from .suites import EvalSuiteService, rubric_hash
from .verdict import compute_verdict
from .verifiers import Verifier

#: 评测作业类型（队列）
EVAL_JOB_TYPE = "eval.execute"
#: 允许启动评测的候选状态（候选生命周期：ready → evaluating）
ALLOWED_PROPOSAL_STATUSES = ("ready", "needs_evidence")
#: 可以续跑的状态（评测作业状态机）
RESUMABLE_JOB_STATUSES = ("queued", "running", "interrupted", "failed", "budget_stopped", "completed")
#: 每个配对的结果状态
RESULT_STATUSES = ("ok", "infra_error", "undetermined", "cancelled", "budget_stopped", "pending")
#: 评测 run 的工具白名单（两组必须完全一致；脚本只在容器可用时进入）
EVAL_TOOLS = ("list_dir", "read_text", "search_text", "write_artifact")
#: 评测同步执行 run 时对队列作业的认领说明（避免与 worker 重复消费同一 run）
RUN_CLAIM_NOTE = "由评测作业同步执行（避免 worker 重复消费同一 run）"
#: 作业已被 worker 领取时，等待其跑完的上限与轮询间隔
RUN_WAIT_TIMEOUT_S = 120.0
RUN_WAIT_POLL_S = 0.02
SCRIPT_TOOL = "run_command"
#: 评测作业 JSON 字段
_JOB_JSON_FIELDS = ("config", "budget", "usage", "verdict_detail", "report", "checkpoint")

_JOB_STATUS_BY_RESULT = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "budget_stopped": "budget_stopped",
}


def pair_order(plan_index: int) -> str:
    """配对执行顺序：交替。

    偶数编号的配对先跑基线，奇数编号先跑候选；报告与测试都使用同一个函数
    避免「顺序」在实现与披露之间出现偏差。
    """
    return "baseline_first" if plan_index % 2 == 0 else "candidate_first"


class EvalService:
    """评测中心服务：作业编排 + 结果落库 + 结论应用。"""

    def __init__(self, store, repo, events=None, idem=None, queue=None, skills=None, runs=None,
                 budget=None, container=None, model_factory=None, config=None, *,
                 suites=None, verifier=None, judge=None) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.idem = idem
        self.queue = queue
        self.skills = skills
        self.runs = runs
        self.budget = budget
        self.container = container
        self.model_factory = model_factory
        self.config = config
        self.suites = suites or EvalSuiteService(store, repo, events, idem, config)
        blobs = None
        if config is not None:
            try:
                blobs = BlobStore(Path(config.blob_dir))
            except Exception:  # pragma: no cover - 制品库不可用时不阻断构造
                blobs = None
        self.verifier = verifier or Verifier(blobs=blobs, container=container)
        self.judge = judge
        self.blobs = blobs
        self._current_job_id: str | None = None

    # ================================================================== 启动
    def start_evaluation(self, *, proposal_id, suite_id, config=None, budget=None,
                         idempotency_key=None, profile_id="local") -> dict:
        """冻结 base/candidate/suite/config hash 并入队评测作业。"""
        payload = {
            "proposal_id": proposal_id,
            "suite_id": suite_id,
            "config": config or {},
            "budget": budget or {},
            "profile_id": profile_id,
        }
        if self.idem is not None:
            return self.idem.run("eval.start", idempotency_key, payload, self._start_impl_for(payload))
        return self._start_impl_for(payload)()

    def _start_impl_for(self, payload: dict):
        def _run() -> dict:
            return self._start_impl(
                proposal_id=payload["proposal_id"],
                suite_id=payload["suite_id"],
                config=payload["config"],
                budget=payload["budget"],
                profile_id=payload["profile_id"],
            )

        return _run

    def _start_impl(self, *, proposal_id, suite_id, config, budget, profile_id) -> dict:
        proposal = self._require_proposal(proposal_id)
        status = proposal["status"]
        if status == "evaluating":
            existing = self._active_job_for(proposal_id)
            if existing is not None:
                return {
                    "job_id": existing["id"],
                    "status": existing["status"],
                    "suite_id": existing["suite_id"],
                    "created": False,
                    "note": "已有进行中的评测作业，返回同一作业",
                }
        if status not in ALLOWED_PROPOSAL_STATUSES:
            raise InvalidInput(
                f"候选当前状态不允许启动评测: {status}（需要 ready/needs_evidence）",
                field_errors={"status": "invalid_state"},
            )
        candidate_version_id = proposal.get("candidate_version_id")
        if not candidate_version_id:
            raise InvalidInput(
                "候选尚未生成完整包：无法启动对照评测",
                field_errors={"candidate_version_id": "required"},
            )
        if self.skills is None:
            raise MissingDependency("技能层不可用：无法冻结候选包", field_errors={"skills": "unavailable"})
        self._assert_version_complete(candidate_version_id, what="候选包")
        baseline_version_id = proposal.get("baseline_version_id")
        if not baseline_version_id:
            raise InvalidInput(
                "候选缺少基线版本：无法做新旧版本对照",
                field_errors={"baseline_version_id": "required"},
            )
        self._assert_version_complete(baseline_version_id, what="基线包")

        suite = self.suites.require_frozen(suite_id)
        if suite["skill_id"] != proposal["skill_id"]:
            raise InvalidInput(
                "评测集与候选不属于同一 skill",
                field_errors={"suite_id": "skill_mismatch"},
            )
        cases = self.suites.get_cases(suite_id, include_expected=True)
        if not cases:
            raise InvalidInput("评测集没有用例", field_errors={"suite_id": "empty"})

        spec = spec_from_budget(
            budget or (suite.get("thresholds") or {}).get("budget"), default=DEFAULT_EVAL_BUDGET
        )
        workspace = self._eval_workspace(profile_id, proposal["skill_id"])
        permissions = self._permissions_for_workspace(workspace, scripts_allowed=self._scripts_allowed())
        model_info = self._model_info()
        deps = self._dependency_versions(profile_id, proposal["skill_id"])
        candidate_package_hash = self._package_hash(candidate_version_id)
        baseline_package_hash = self._package_hash(baseline_version_id)

        frozen_config = {
            "profile_id": profile_id,
            "model": model_info,
            "sampling": {
                "temperature": (model_info or {}).get("temperature"),
                "max_output_tokens": (model_info or {}).get("max_output_tokens"),
                "timeout_s": (model_info or {}).get("timeout_s"),
                "extra": (model_info or {}).get("extra") or {},
            },
            "tool_permissions": permissions,
            "budget": spec.to_dict(),
            "run_budget": dict(DEFAULT_EVAL_RUN_BUDGET),
            "dependencies": deps,
            "suite": {
                "suite_id": suite["id"],
                "revision": suite["revision"],
                "suite_hash": suite["suite_hash"],
                "rubric_hash": rubric_hash(suite.get("rubric") or {}),
                "thresholds_hash": canonical_hash(suite.get("thresholds") or {}),
                "objective": suite.get("objective"),
                "cases": len(cases),
                "holdout": len([c for c in cases if c["role"] == "holdout"]),
            },
            "candidate_package_hash": candidate_package_hash,
            "baseline_package_hash": baseline_package_hash,
            "workspace": {
                "id": workspace["id"],
                "root_path": workspace["root_path"],
                "artifact_path": workspace["artifact_path"],
                "allowed_tools": list(self._allowed_tools(self._scripts_allowed())),
                "allow_network": bool(workspace["allow_network"]),
                "allow_scripts": bool(workspace["allow_scripts"]),
            },
            "flags": {
                "learning_enabled": False,
                "origin": "evaluation",
                "long_term_memory": False,
                "external_publish": False,
                "fixed_task_context": True,
            },
            "overrides": {
                "config": dict(config or {}),
                "per_run_calls": int(DEFAULT_EVAL_RUN_BUDGET.get("max_model_calls") or 8),
            },
        }
        config_hash = canonical_hash(frozen_config)
        job_id = new_id("eval")
        now = utc_now()
        checkpoint = {
            "phase": "queued",
            "created_at": now,
            "per_group": {"baseline": {"calls": 0, "tokens": 0}, "candidate": {"calls": 0, "tokens": 0}},
        }
        queue_job_id = None
        if self.queue is not None:
            queue_job_id = self.queue.enqueue(
                EVAL_JOB_TYPE,
                {"job_id": job_id, "proposal_id": proposal_id, "suite_id": suite_id},
                idempotency_key=f"eval.execute:{job_id}",
                budget=spec.to_dict(),
            )
            checkpoint["queue_job_id"] = queue_job_id
        with self.store.tx() as conn:
            self.repo.insert(
                "eval_job",
                {
                    "id": job_id,
                    "profile_id": profile_id,
                    "proposal_id": proposal_id,
                    "suite_id": suite_id,
                    "baseline_version_id": baseline_version_id,
                    "candidate_version_id": candidate_version_id,
                    "config_hash": config_hash,
                    "config": json.dumps(frozen_config, ensure_ascii=False, default=str),
                    "budget": json.dumps(spec.to_dict(), ensure_ascii=False),
                    "usage": json.dumps({}, ensure_ascii=False),
                    "status": "queued",
                    "verdict": None,
                    "verdict_detail": "{}",
                    "report": "{}",
                    "cancel_requested": 0,
                    "checkpoint": json.dumps(checkpoint, ensure_ascii=False, default=str),
                    "attempts": 0,
                    "created_at": now,
                    "updated_at": now,
                },
                conn=conn,
            )
            self.repo.update(
                "proposal",
                proposal_id,
                {"status": "evaluating", "updated_at": now},
                conn=conn,
            )
            if self.events is not None:
                self.events.audit(
                    "eval.start",
                    "eval_job",
                    job_id,
                    after={
                        "proposal_id": proposal_id,
                        "suite_id": suite_id,
                        "suite_hash": suite["suite_hash"],
                        "config_hash": config_hash,
                        "baseline_version_id": baseline_version_id,
                        "candidate_version_id": candidate_version_id,
                        "budget": spec.to_dict(),
                    },
                    conn=conn,
                )
        if self.events is not None:
            self.events.product_event(
                "eval_started",
                {
                    "job_id": job_id,
                    "suite_revision": suite["revision"],
                    "cases": len(cases),
                    "holdout": len([c for c in cases if c["role"] == "holdout"]),
                    "planned_pairs": len(cases) * self._repetitions_of(suite, cases),
                },
                profile_id=profile_id,
            )
        return {
            "job_id": job_id,
            "id": job_id,
            "queue_job_id": queue_job_id,
            "status": "queued",
            "suite_id": suite_id,
            "suite_revision": suite["revision"],
            "suite_hash": suite["suite_hash"],
            "baseline_version_id": baseline_version_id,
            "candidate_version_id": candidate_version_id,
            "config_hash": config_hash,
            "budget": spec.to_dict(),
            "created": True,
        }

    # ================================================================== 执行
    def execute(self, job_id: str, worker_id: str = "worker") -> None:
        """worker handler：按冻结配置补齐未完成配对并产出结论。

        进入执行前用 CAS 把作业从 ``queued/interrupted`` 原子推进到 ``running``
        并发的第二个 worker（或重复派发）拿不到所有权，因此不会重复执行同一个配对。
        """
        job = self._require_job(job_id)
        if job["status"] in ("completed", "failed", "cancelled", "budget_stopped"):
            # 幂等：终态作业不再执行；需要续跑时先调用 resume()
            return
        attempt = self._claim_for_running(job_id, worker_id)
        if not attempt:
            return
        self._execute_job(job_id, worker_id, attempt)

    def _claim_for_running(self, job_id: str, worker_id: str) -> int:
        """原子认领一次执行；返回本次 attempt 编号，0 表示已被别人认领。"""
        now = utc_now()
        with self.store.tx() as conn:
            cursor = conn.execute(
                "UPDATE eval_job SET status = 'running', attempts = attempts + 1, "
                "lease_owner = ?, heartbeat_at = ?, started_at = COALESCE(started_at, ?), "
                "updated_at = ? WHERE id = ? AND status IN ('queued', 'interrupted')",
                (worker_id, now, now, now, job_id),
            )
            claimed = cursor.rowcount == 1
        if not claimed:
            return 0
        row = self.repo.get("eval_job", job_id)
        attempt = int(row["attempts"] or 1) if row is not None else 1
        if self.events is not None:
            self.events.audit(
                "eval.running", "eval_job", job_id,
                after={"attempt": attempt, "worker": worker_id},
            )
        return attempt

    def _execute_job(self, job_id: str, worker_id: str, attempt: int) -> None:
        started = utc_now()
        started_monotonic = time.monotonic()
        job = self._require_job(job_id)
        spec = spec_from_budget(job.get("budget"), default=DEFAULT_EVAL_BUDGET)
        self.budget.open(job_id, spec)

        suite_row = self.repo.get("eval_suite", job["suite_id"])
        if suite_row is None:
            self._fail_job(job_id, "evidence_unavailable", "评测集已不可用")
            return
        suite = row_to_dict(suite_row, json_fields=("rubric", "critical_checks", "thresholds")) or {}
        cases = self.suites.get_cases(job["suite_id"], include_expected=True)
        case_map = {c["id"]: c for c in cases}
        thresholds = dict(suite.get("thresholds") or {})
        objective = suite.get("objective") or "quality"

        plan = self._pair_plan(job, suite, cases)
        existing = self._result_rows(job_id)
        existing_map = {(r["case_id"], int(r["repetition"])): r for r in existing}
        resume_mode = bool(existing)
        pending = [
            p for p in plan
            if not (
                (p["case_id"], p["repetition"]) in existing_map
                and existing_map[(p["case_id"], p["repetition"])]["result_status"] == "ok"
            )
        ]

        self._current_job_id = job_id
        self._update_job(
            job_id,
            {
                "attempts": attempt,
                "started_at": job.get("started_at") or started,
                "lease_owner": worker_id,
                "heartbeat_at": started,
                "updated_at": started,
            },
        )
        self._heartbeat(job_id, worker_id)
        self._set_checkpoint(
            job_id,
            {
                "phase": "running",
                "attempt": attempt,
                "resumed": resume_mode,
                "pending_pairs": len(pending),
                "planned_pairs": len(plan),
                "started_at": started,
            },
        )

        usage = self._usage_state(job_id)
        scripts_allowed = self._scripts_allowed()
        cancelled = False
        budget_stopped: str | None = None
        executed_now = 0

        for index, pair in enumerate(plan):
            key = (pair["case_id"], pair["repetition"])
            prior = existing_map.get(key)
            if prior is not None and prior["result_status"] == "ok":
                continue  # 续跑只补未完成配对
            if self._cancel_requested(job_id):
                cancelled = True
                break
            blocked = self._budget_blocked(job_id, spec)
            if blocked:
                budget_stopped = blocked
                break
            if not self._reserve_pair(job_id):
                # 两组各需一次执行的预算不足：超限停止并保留已有结果
                budget_stopped = "max_model_calls"
                break
            self._heartbeat(job_id, worker_id)
            case = case_map.get(pair["case_id"])
            if case is None:
                self._release_pair(job_id)
                continue
            try:
                payload = self._run_pair(
                    job=job,
                    suite=suite,
                    case=case,
                    pair=pair,
                    worker_id=worker_id,
                    usage=usage,
                    scripts_allowed=scripts_allowed,
                    objective=objective,
                    thresholds=thresholds,
                    attempt=attempt,
                    plan_index=index,
                )
            except AppError as exc:
                payload = self._error_payload(pair, exc.code, exc.message, attempt)
            except Exception as exc:  # pragma: no cover - 兜底，绝不让作业崩掉
                payload = self._error_payload(pair, "internal_error", str(exc), attempt)
            self._release_pair(job_id)
            self._store_result(job_id, pair, payload, prior=prior)
            executed_now += 1
            usage = self._usage_state(job_id)
            self._persist_usage(job_id, usage)
            self._set_checkpoint(
                job_id,
                {
                    "phase": "running",
                    "done_pairs": len(self._result_rows(job_id)),
                    "planned_pairs": len(plan),
                    "last_case_id": pair["case_id"],
                    "last_repetition": pair["repetition"],
                },
            )

        elapsed_s = round(time.monotonic() - started_monotonic, 3)
        self._finalize(
            job_id,
            job=job,
            suite=suite,
            cases=cases,
            plan=plan,
            thresholds=thresholds,
            objective=objective,
            budget_stopped=budget_stopped,
            cancelled=cancelled,
            started_at=job.get("started_at") or started,
            elapsed_s=elapsed_s,
            attempt=attempt,
            resume_mode=resume_mode,
            executed_now=executed_now,
        )

    # ------------------------------------------------------------- 单个配对
    def _run_pair(self, *, job, suite, case, pair, worker_id, usage, scripts_allowed,
                  objective, thresholds, attempt, plan_index) -> dict:
        order = pair_order(plan_index)
        groups = ("baseline", "candidate") if order == "baseline_first" else ("candidate", "baseline")
        versions = {
            "baseline": job["baseline_version_id"],
            "candidate": job["candidate_version_id"],
        }
        executions: dict[str, dict] = {}
        environment_mismatch: list[str] = []
        for group in groups:
            execution = self._execute_one_run(
                job=job,
                case=case,
                pair=pair,
                group=group,
                version_id=versions[group],
                worker_id=worker_id,
                scripts_allowed=scripts_allowed,
                attempt=attempt,
            )
            executions[group] = execution
            usage.setdefault("per_group", {}).setdefault(group, {"calls": 0, "tokens": 0})
            usage["per_group"][group]["calls"] += execution["usage"]["calls"]
            usage["per_group"][group]["tokens"] += execution["usage"]["tokens"]
            if execution.get("environment_mismatch"):
                environment_mismatch.append(f"{group}:{execution['environment_mismatch']}")
        # 配对预留已释放，按两个 run 的实际用量记入作业总预算
        for group in ("baseline", "candidate"):
            self._charge_job_budget(job["id"], executions[group]["usage"])
        self._persist_usage(job["id"], usage)

        sides: dict[str, dict] = {}
        undetermined = False
        for group in ("baseline", "candidate"):
            execution = executions[group]
            if execution["run_status"] != "completed":
                sides[group] = self._side_from_execution(execution, passed=None)
                continue
            result = self.verifier.verify_case(case, execution["artifacts"], execution["run_dir"])
            sides[group] = self._side_from_execution(execution, verifier=result)
            if result.get("status") == "undetermined":
                undetermined = True

        judge_info = self._judge_pair(
            job=job,
            case=case,
            sides=sides,
            executions=executions,
            thresholds=thresholds,
            objective=objective,
            plan_index=plan_index,
        )
        pass_threshold = float((suite.get("rubric") or {}).get("pass_threshold") or 0.0)
        for group in ("baseline", "candidate"):
            side = sides[group]
            if side.get("verifier_status") == "ok":
                side["passed"] = bool(side["verifier_passed"]) and (
                    side.get("score") is None or float(side["score"]) >= pass_threshold
                )
                side["status"] = "passed" if side["passed"] else "failed"
            else:
                side["passed"] = None

        run_statuses = {g: executions[g]["run_status"] for g in ("baseline", "candidate")}
        if any(s in ("cancelled",) for s in run_statuses.values()):
            result_status = "cancelled"
        elif any(s == "budget_stopped" for s in run_statuses.values()):
            result_status = "budget_stopped"
        elif any(s != "completed" for s in run_statuses.values()):
            result_status = "infra_error"
        elif undetermined:
            result_status = "undetermined"
        else:
            result_status = "ok"

        error_code = None
        error_message = None
        for group in ("baseline", "candidate"):
            if executions[group].get("error_code"):
                error_code = executions[group]["error_code"]
                error_message = executions[group].get("error_message")
                break

        return {
            "case_id": pair["case_id"],
            "case_name": pair["case_name"],
            "role": pair["role"],
            "repetition": pair["repetition"],
            "order": order,
            "attempt": attempt,
            "result_status": result_status,
            "baseline_run_id": executions["baseline"]["run_id"],
            "candidate_run_id": executions["candidate"]["run_id"],
            "baseline": sides["baseline"],
            "candidate": sides["candidate"],
            "judge": judge_info,
            "error_code": error_code,
            "error_message": error_message,
            "environment_mismatch": environment_mismatch,
            "pin": {
                group: (executions[group].get("pin") or {}).get("mode")
                for group in ("baseline", "candidate")
            },
            "notes": (
                "评测 run 关闭学习与外部发布动作；两组仅目标 skill 版本不同"
            ),
        }

    def _execute_one_run(self, *, job, case, pair, group, version_id, worker_id,
                         scripts_allowed, attempt) -> dict:
        job_id = job["id"]
        workspace = self._case_workspace(job, case, group, scripts_allowed=scripts_allowed)
        # 每次执行用独立 artifacts 目录：不共享残留产物
        planned_dir = Path(workspace["artifact_path"]) / new_id("evalrun")
        key = f"eval.run:{job_id}:{case['id']}:{pair['repetition']}:{group}:{attempt}"
        kwargs = {
            "request": case["prompt"],
            "workspace_id": workspace["id"],
            "completion_criteria": self._criteria_text(case),
            "skill_ids": [job.get("skill_id") or self._skill_id_of_job(job)],
            "budget": dict(DEFAULT_EVAL_RUN_BUDGET),
            "profile_id": job.get("profile_id") or "local",
            "idempotency_key": key,
            "learning_enabled": False,
            "origin": "evaluation",
            "eval_job_id": job_id,
            "artifacts_dir": str(planned_dir),
            # 版本钉扎：只有目标 skill 版本不同
            "skill_versions": {self._skill_id_of_job(job): version_id},
            "only_version_ids": [version_id],
        }
        created = self._call_create_run(kwargs)
        run_id = created.get("run_id") or created.get("id")
        if not run_id:
            return self._execution_error("run_not_created", "create_run 未返回 run_id")
        # 原子认领该 run 的队列作业：认领成功才自己执行，否则等 worker 跑完（绝不重复执行）
        claimed = self._claim_run_job(created.get("job_id"))
        pin = self._ensure_pinned(run_id, self._skill_id_of_job(job), version_id)
        execution = {
            "run_id": run_id,
            "workspace_id": workspace["id"],
            "artifacts_dir": str(planned_dir),
            "pin": pin,
            "usage": {"calls": 0, "tokens": 0, "known": True, "unknown_usage": 0},
            "artifacts": [],
            "run_dir": None,
            "run_status": "queued",
            "error_code": None,
            "error_message": None,
            "model_ids": [],
            "environment_mismatch": None,
        }
        if not pin.get("pinned"):
            execution["run_status"] = "environment_mismatch"
            execution["environment_mismatch"] = pin.get("reason") or "pin_failed"
            execution["error_code"] = "environment_mismatch"
            execution["error_message"] = (
                f"无法把 run 钉在指定 skill 版本（{pin.get('reason')}）：结果不可比"
            )
            return execution
        if claimed:
            try:
                self.runs.execute(run_id, worker_id)
            except AppError as exc:
                execution["error_code"] = exc.code
                execution["error_message"] = exc.message
            except Exception as exc:  # pragma: no cover - 兜底
                execution["error_code"] = "internal_error"
                execution["error_message"] = str(exc)
        elif not self._wait_for_run(run_id, worker_id):
            execution["error_code"] = "worker_timeout"
            execution["error_message"] = (
                "run 已被 worker 领取但未在等待上限内结束：本配对按未判定处理，可用 resume 补齐"
            )

        row = self.repo.get("run", run_id)
        if row is not None:
            execution["run_status"] = row["execution_status"]
            execution["error_code"] = execution["error_code"] or row["error_code"]
            execution["error_message"] = execution["error_message"] or row["error_message"]
        usage = self._run_usage(run_id, row)
        execution["usage"] = usage
        execution["artifacts"] = self._run_artifacts(run_id)
        execution["run_dir"] = self._run_dir(run_id, workspace, case, group, planned_dir)
        execution["model_ids"] = self._observed_model_ids(run_id, row)
        return execution

    def _execution_error(self, code: str, message: str) -> dict:
        return {
            "run_id": None,
            "workspace_id": None,
            "artifacts_dir": None,
            "pin": {"pinned": False, "reason": code},
            "usage": {"calls": 0, "tokens": 0, "known": True, "unknown_usage": 0},
            "artifacts": [],
            "run_dir": None,
            "run_status": "failed",
            "error_code": code,
            "error_message": message,
            "model_ids": [],
            "environment_mismatch": None,
        }

    def _side_from_execution(self, execution: dict, *, verifier: dict | None = None,
                             passed=None) -> dict:
        if verifier is not None and verifier.get("status") != "undetermined":
            passed = verifier.get("passed")
        if verifier is None and execution["run_status"] != "completed":
            passed = None if execution["run_status"] != "completed" else passed
        side = {
            "passed": passed,
            "score": (verifier or {}).get("score"),
            "verifier_passed": (verifier or {}).get("passed"),
            "verifier_status": "ok" if verifier is not None and verifier.get("status") != "undetermined"
            else ("undetermined" if verifier is not None else execution["run_status"]),
            "status": execution["run_status"],
            "checks": list((verifier or {}).get("checks") or []),
            "critical_failed": list((verifier or {}).get("critical_failed") or []),
            "failed": list((verifier or {}).get("failed") or []),
            "undetermined": list((verifier or {}).get("undetermined") or []),
            "tokens": execution["usage"].get("tokens"),
            "tokens_known": bool(execution["usage"].get("known")),
            "tool_actions": execution["usage"].get("tool_actions"),
            "wall_s": execution["usage"].get("wall_s"),
            "model_ids": list(execution.get("model_ids") or []),
            "run_id": execution.get("run_id"),
            "run_status": execution["run_status"],
        }
        return side

    # ------------------------------------------------------------- 盲评
    def _judge_pair(self, *, job, case, sides, executions, thresholds, objective, plan_index) -> dict:
        info: dict = {
            "type": "model_blind_pair",
            "a_is_candidate": plan_index % 2 == 0,
            "baseline_score": None,
            "candidate_score": None,
            "complete": False,
            "disagreement": False,
        }
        if objective != "quality":
            info["skipped"] = "objective_not_quality"
            return info
        rubric = self._rubric_for(job)
        if not (rubric.get("dimensions") or {}):
            info["skipped"] = "rubric_empty"
            return info
        if thresholds.get("judge_enabled") is False or case.get("judge") is False:
            info["skipped"] = "judge_disabled"
            return info
        if any(executions[g]["run_status"] != "completed" for g in ("baseline", "candidate")):
            info["skipped"] = "run_not_completed"
            return info
        texts = {}
        for group in ("baseline", "candidate"):
            side = sides[group]
            texts[group] = self._artifacts_text(executions[group])
            if side.get("verifier_status") == "undetermined":
                info["skipped"] = "verifier_undetermined"
                return info
        # 匿名配对：A/B 与版本的映射只留在记录里，绝不进入 prompt
        a_is_candidate = info["a_is_candidate"]
        outputs = {
            "A": {"text": texts["candidate" if a_is_candidate else "baseline"]},
            "B": {"text": texts["baseline" if a_is_candidate else "candidate"]},
        }
        judge = self._judge_for_job(job["id"])
        author_model = self._author_model(job)
        try:
            result = judge.score_pair(
                rubric=rubric,
                case=case,
                outputs=outputs,
                author_model=author_model,
                forbidden_terms=self._forbidden_terms(job),
            )
        except AppError as exc:
            info["error"] = exc.code
            info["error_message"] = exc.message
            return info
        except Exception as exc:  # pragma: no cover - 裁判异常不打死作业
            info["error"] = "judge_error"
            info["error_message"] = str(exc)
            return info
        if result.get("error"):
            info["error"] = result["error"]
            return info
        score_a = (result.get("A") or {}).get("score")
        score_b = (result.get("B") or {}).get("score")
        candidate_score = score_a if a_is_candidate else score_b
        baseline_score = score_b if a_is_candidate else score_a
        info.update(
            {
                "complete": bool(result.get("complete")),
                "disagreement": bool(result.get("disagreement")),
                "baseline_score": baseline_score,
                "candidate_score": candidate_score,
                "preferred": result.get("preferred"),
                "computed_preference": result.get("computed_preference"),
                "dimensions": (
                    (result.get("A") if a_is_candidate else result.get("B")) or {}
                ).get("dimension_scores"),
                "missing_dimensions": (
                    (result.get("A") if a_is_candidate else result.get("B")) or {}
                ).get("missing_dimensions"),
                "uncertainty": result.get("uncertainty"),
                "disclosure": result.get("disclosure") or {},
                "same_model_as_author": (result.get("disclosure") or {}).get("same_model_as_author"),
                "model": result.get("model") or {},
                "usage": result.get("usage") or {},
                "usage_known": bool(result.get("usage_known")),
                "model_says_passed": result.get("model_says_passed"),
                "prompt_hash": canonical_hash(result.get("prompt") or {}),
            }
        )
        if candidate_score is not None:
            sides["candidate"]["judge_score"] = candidate_score
        if baseline_score is not None:
            sides["baseline"]["judge_score"] = baseline_score
        for group, score in (("baseline", baseline_score), ("candidate", candidate_score)):
            if score is not None:
                sides[group]["score"] = score
        return info

    # ------------------------------------------------------------- 落库
    def _store_result(self, job_id: str, pair: dict, payload: dict, *, prior: dict | None) -> None:
        """写入/更新一个配对结果；旧记录不删除，重试写进 attempt 链。"""
        now = utc_now()
        history = list((prior or {}).get("notes", {}).get("attempt_history") or [])
        if prior is not None:
            history.append(
                {
                    "attempt": prior.get("attempt"),
                    "result_status": prior.get("result_status"),
                    "baseline_run_id": prior.get("baseline_run_id"),
                    "candidate_run_id": prior.get("candidate_run_id"),
                    "recorded_at": prior.get("created_at"),
                    "error_code": (prior.get("notes") or {}).get("error_code"),
                }
            )
        data = {
            "baseline_run_id": payload.get("baseline_run_id"),
            "candidate_run_id": payload.get("candidate_run_id"),
            "result_status": payload.get("result_status") or "pending",
            "baseline_checks": payload.get("baseline") or {},
            "candidate_checks": payload.get("candidate") or {},
            "baseline_score": _score_of(payload.get("baseline")),
            "candidate_score": _score_of(payload.get("candidate")),
            "judge": payload.get("judge") or {},
            "notes": {
                "attempt": payload.get("attempt"),
                "order": payload.get("order"),
                "error_code": payload.get("error_code"),
                "error_message": payload.get("error_message"),
                "environment_mismatch": payload.get("environment_mismatch") or [],
                "pin": payload.get("pin") or {},
                "note": payload.get("notes"),
                "attempt_history": history,
            },
            "attempt": int(payload.get("attempt") or 1),
            "updated_at": now,
        }
        with self.store.tx() as conn:
            if prior is None:
                row = {
                    "id": new_id("evalres"),
                    "job_id": job_id,
                    "case_id": pair["case_id"],
                    "repetition": int(pair["repetition"]),
                    "created_at": now,
                }
                row.update(data)
                self.repo.insert("eval_result", row, conn=conn)
            else:
                self.repo.update("eval_result", prior["id"], data, conn=conn)

    # ================================================================== 续跑
    def resume(self, job_id) -> dict:
        """只补未完成配对，保留旧记录与 attempt 链。"""
        job = self._require_job(job_id)
        if job["status"] not in RESUMABLE_JOB_STATUSES:
            raise InvalidState(
                f"当前作业状态不允许续跑: {job['status']}",
                field_errors={"status": "invalid_state"},
            )
        plan, pending = self._pending_pairs(job)
        if not pending:
            return {
                "job_id": job_id,
                "status": job["status"],
                "pending_pairs": 0,
                "attempt": int(job.get("attempts") or 0),
                "note": "没有未完成配对；旧结果保留",
            }
        next_attempt = int(job.get("attempts") or 0) + 1
        queue_job_id = (job.get("checkpoint") or {}).get("queue_job_id")
        if self.queue is not None:
            queue_job_id = self.queue.enqueue(
                EVAL_JOB_TYPE,
                {"job_id": job_id, "resume": True, "attempt": next_attempt},
                idempotency_key=f"eval.execute:{job_id}:{next_attempt}",
                budget=job.get("budget") or {},
            )
        checkpoint = dict(job.get("checkpoint") or {})
        checkpoint.update(
            {
                "phase": "queued",
                "queue_job_id": queue_job_id,
                "resumed_at": utc_now(),
                "resume_attempt": next_attempt,
                "pending_pairs": [
                    {"case_id": p["case_id"], "repetition": p["repetition"]} for p in pending
                ],
            }
        )
        self._update_job(
            job_id,
            {
                "status": "queued",
                "cancel_requested": 0,
                "checkpoint": checkpoint,
                "updated_at": utc_now(),
            },
        )
        if self.events is not None:
            self.events.audit(
                "eval.resume",
                "eval_job",
                job_id,
                after={"pending_pairs": len(pending), "attempt": next_attempt},
                reason="只补未完成配对，旧记录与 attempt 链保留",
            )
            self.events.product_event(
                "eval_resumed", {"job_id": job_id, "pending_pairs": len(pending)},
                profile_id=job.get("profile_id"),
            )
        return {
            "job_id": job_id,
            "status": "queued",
            "queue_job_id": queue_job_id,
            "pending_pairs": len(pending),
            "pairs": [
                {"case_id": p["case_id"], "case_name": p["case_name"], "repetition": p["repetition"],
                 "result_status": p.get("result_status")}
                for p in pending
            ],
            "attempt": next_attempt,
        }

    def pending_pairs(self, job_id) -> list[dict]:
        job = self._require_job(job_id)
        _, pending = self._pending_pairs(job)
        return pending

    # ================================================================== 取消与查询
    def cancel(self, job_id) -> dict:
        job = self._require_job(job_id)
        if job["status"] in ("completed", "failed", "cancelled", "budget_stopped"):
            return {"job_id": job_id, "status": job["status"], "cancel_requested": False,
                    "note": "作业已处于终态，不宣称撤销已完成动作"}
        now = utc_now()
        fields = {"cancel_requested": 1, "updated_at": now}
        if job["status"] in ("queued", "interrupted"):
            fields["status"] = "cancelled"
            fields["finished_at"] = now
        self._update_job(job_id, fields)
        queue_job_id = (job.get("checkpoint") or {}).get("queue_job_id")
        if self.queue is not None and queue_job_id:
            try:
                self.queue.cancel(queue_job_id)
            except AppError:  # pragma: no cover
                pass
        if self.events is not None:
            self.events.audit("eval.cancel", "eval_job", job_id,
                              after={"status": fields.get("status", job["status"])})
            self.events.product_event("eval_cancelled", {"job_id": job_id},
                                      profile_id=job.get("profile_id"))
        return {
            "job_id": job_id,
            "status": fields.get("status", job["status"]),
            "cancel_requested": True,
            "note": "取消请求已记录；运行中的配对在当前安全检查点停止，已完成证据保留",
        }

    def get_job(self, job_id) -> dict:
        job = self._require_job(job_id)
        results = self.get_results(job_id)
        validity = self.check_report_valid(job_id)
        data = dict(job)
        data["results"] = results
        data["result_count"] = len(results)
        data["report"] = job.get("report") or {}
        data["report_valid"] = validity["valid"]
        data["report_invalid_reasons"] = validity["reasons"]
        data["pending_pairs"] = len(self.pending_pairs(job_id))
        return data

    def list_jobs(self, *, proposal_id=None, status=None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM eval_job WHERE (? IS NULL OR proposal_id = ?) AND (? IS NULL OR status = ?) " \
              "ORDER BY created_at DESC, rowid DESC LIMIT ?"
        rows = self.store.query(sql, (proposal_id, proposal_id, status, status, int(limit)))
        out = []
        for row in rows:
            data = row_to_dict(row, json_fields=_JOB_JSON_FIELDS) or {}
            data.pop("report", None)
            data["report_valid"] = self.check_report_valid(row["id"])["valid"]
            out.append(data)
        return out

    def get_results(self, job_id) -> list[dict]:
        self._require_job(job_id)
        case_map = self._case_map_for_job(job_id)
        artifact_map = self._artifact_map(job_id)
        return [
            self._canonical_result(r, case_map, artifact_map) for r in self._result_rows(job_id)
        ]

    # ================================================================== 报告
    def build_report(self, job_id) -> dict:
        """重新组装报告并持久化（允许导出；报告 hash 用于启用校验）。"""
        job = self._require_job(job_id)
        suite_row = self.repo.get("eval_suite", job["suite_id"])
        suite = row_to_dict(suite_row, json_fields=("rubric", "critical_checks", "thresholds")) or {}
        cases = self.suites.get_cases(job["suite_id"], include_expected=False) if suite_row else []
        raw_rows = self._result_rows(job_id)
        case_map = {c["id"]: c for c in cases}
        artifact_map = self._artifact_map(job_id)
        results = [self._canonical_result(r, case_map, artifact_map) for r in raw_rows]
        plan = self._pair_plan(job, suite, cases) if cases else []
        thresholds = dict(suite.get("thresholds") or {})
        coverage = self._coverage(job, suite, cases, plan, raw_rows)
        checkpoint = job.get("checkpoint") or {}
        coverage["resumed"] = bool(checkpoint.get("resumed"))
        coverage["filled_pairs"] = checkpoint.get("filled_pairs")
        coverage["budget_exceeded"] = checkpoint.get("budget_stopped")
        coverage["elapsed_s"] = checkpoint.get("elapsed_s", coverage.get("elapsed_s"))
        verdict = compute_verdict(
            results=results,
            thresholds=thresholds,
            coverage=coverage,
            objective=suite.get("objective") or "quality",
            budget_exceeded=coverage.get("budget_exceeded"),
        )
        validity = self.check_report_valid(job_id)
        usage = self._usage_state(job_id)
        report = report_mod.build_report(
            job=job,
            suite=suite,
            cases=cases,
            results=results,
            verdict=verdict,
            usage=usage,
            coverage=coverage,
            pairing=coverage.get("pairing") or {},
            isolation=coverage.get("isolation") or {},
            disclosure=coverage.get("judge_disclosure") or {},
            model_versions=coverage.get("model_versions") or {},
            cost=report_mod.cost_view(
                usage=usage,
                pricing=self._pricing(),
                unknown_usage=usage.get("unknown_usage", 0),
            ),
            timeline={
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "elapsed_s": coverage.get("elapsed_s"),
            },
            report_valid=validity["valid"],
            invalid_reasons=validity["reasons"],
        )
        report["verdict_detail"] = verdict
        report["job_status"] = job["status"]
        with self.store.tx() as conn:
            self.repo.update(
                "eval_job",
                job_id,
                {
                    "report": json.dumps(report, ensure_ascii=False, default=str),
                    "verdict": verdict.get("verdict"),
                    "verdict_detail": json.dumps(verdict, ensure_ascii=False, default=str),
                    "updated_at": utc_now(),
                },
                conn=conn,
            )
        return report

    def export_report(self, job_id, dest=None) -> dict:
        """导出报告（JSON + Markdown），供人工审阅与归档。"""
        report = self.build_report(job_id)
        target = dest or (
            str(Path(self.config.export_dir) / "evaluation") if self.config is not None else None
        )
        if target is None:
            raise InvalidInput("未配置导出目录，请显式给出 dest", field_errors={"dest": "required"})
        return report_mod.write_export(report, target)

    # ================================================================== 结论
    def apply_verdict(self, job_id) -> str:
        """按程序计算结果迁移候选状态；模型输出不能写 verdict。"""
        job = self._require_job(job_id)
        verdict_detail = job.get("verdict_detail") or {}
        verdict = job.get("verdict") or verdict_detail.get("verdict")
        if not verdict:
            report = self.build_report(job_id)
            verdict_detail = report.get("verdict_detail") or {}
            verdict = verdict_detail.get("verdict")
        validity = self.check_report_valid(job_id)
        effective = verdict
        notes = []
        if verdict == "passed" and (not validity["valid"] or not verdict_detail.get("evidence_sufficient")):
            effective = "inconclusive"
            notes.append(
                "报告已失效或证据不足：不授予 passed"
                + (f"（{','.join(validity['reasons'])}）" if validity["reasons"] else "")
            )
        mapping = {"passed": "eligible", "failed": "rejected", "inconclusive": "needs_evidence"}
        target = mapping.get(effective or "", "needs_evidence")
        proposal = self.repo.get("proposal", job["proposal_id"])
        if proposal is None:
            raise EvidenceUnavailable("候选已不可用：无法应用结论")
        report = job.get("report") or {}
        evaluation = {
            "job_id": job_id,
            "verdict": effective,
            "computed_verdict": verdict,
            "report_hash": canonical_hash(report) if report else None,
            "suite_id": job["suite_id"],
            "suite_revision": (report.get("header") or {}).get("suite_revision"),
            "config_hash": job["config_hash"],
            "baseline_version_id": job["baseline_version_id"],
            "candidate_version_id": job["candidate_version_id"],
            "report_valid": validity["valid"],
            "evidence_sufficient": verdict_detail.get("evidence_sufficient"),
            "computed_at": utc_now(),
            "notes": notes,
        }
        now = utc_now()
        with self.store.tx() as conn:
            self.repo.update(
                "proposal",
                job["proposal_id"],
                {
                    "status": target,
                    "evaluation": json.dumps(evaluation, ensure_ascii=False, default=str),
                    "updated_at": now,
                },
                conn=conn,
            )
            if self.events is not None:
                self.events.audit(
                    "eval.verdict",
                    "eval_job",
                    job_id,
                    after={
                        "verdict": effective,
                        "computed_verdict": verdict,
                        "proposal_status": target,
                        "report_valid": validity["valid"],
                    },
                    reason="; ".join(notes) or None,
                    conn=conn,
                )
        if self.events is not None:
            self.events.product_event(
                "eval_finished",
                {
                    "job_id": job_id,
                    "suite_revision": evaluation.get("suite_revision"),
                    "status": job["status"],
                    "verdict": effective,
                    "complete_pairs": (job.get("report") or {}).get("coverage", {}).get("complete_pairs"),
                },
                profile_id=job.get("profile_id"),
            )
        return effective

    def check_report_valid(self, job_id) -> dict:
        """正文/包/suite/rubric/权限/模型/预算变化都让旧报告失效。"""
        row = self.repo.get("eval_job", job_id)
        if row is None:
            raise InvalidInput(f"评测作业不存在: {job_id}", field_errors={"job_id": "not_found"})
        job = row_to_dict(row, json_fields=_JOB_JSON_FIELDS) or {}
        stored = job.get("config") or {}
        reasons: list[str] = []
        current: dict = {}
        if not job.get("verdict"):
            reasons.append("job_not_finished")
        if stored:
            suite_row = self.repo.get("eval_suite", job["suite_id"])
            if suite_row is None:
                reasons.append("suite_missing")
            else:
                suite = row_to_dict(suite_row, json_fields=("rubric", "critical_checks", "thresholds")) or {}
                current["suite_hash"] = suite.get("suite_hash")
                current["rubric_hash"] = rubric_hash(suite.get("rubric") or {})
                current["thresholds_hash"] = canonical_hash(suite.get("thresholds") or {})
                if current["suite_hash"] != (stored.get("suite") or {}).get("suite_hash"):
                    reasons.append("suite_hash_changed")
                if current["rubric_hash"] != (stored.get("suite") or {}).get("rubric_hash"):
                    reasons.append("rubric_changed")
                if current["thresholds_hash"] != (stored.get("suite") or {}).get("thresholds_hash"):
                    reasons.append("thresholds_changed")
            proposal = self.repo.get("proposal", job["proposal_id"])
            if proposal is None:
                reasons.append("proposal_missing")
            else:
                current["candidate_version_id"] = proposal["candidate_version_id"]
                if proposal["candidate_version_id"] != job["candidate_version_id"]:
                    reasons.append("candidate_version_changed")
            if self.skills is not None:
                try:
                    current["candidate_package_hash"] = self._package_hash(job["candidate_version_id"])
                except AppError as exc:
                    current["candidate_package_hash"] = None
                    reasons.append("package_evidence_missing")
                    current["package_error"] = exc.code
                if current.get("candidate_package_hash") not in (
                    None,
                    stored.get("candidate_package_hash"),
                ):
                    reasons.append("candidate_package_changed")
            workspace = self._find_eval_workspace(
                stored.get("profile_id") or job.get("profile_id") or "local",
                (stored.get("workspace") or {}).get("id"),
            )
            if workspace is not None:
                current["tool_permissions"] = self._permissions_for_workspace(
                    workspace, scripts_allowed=self._scripts_allowed()
                )
                if _permissions_equal(
                    current["tool_permissions"], stored.get("tool_permissions") or {}
                ) is False:
                    reasons.append("tool_permissions_changed")
            model_info = self._model_info()
            current["model"] = model_info
            if (model_info or {}).get("fingerprint") != (stored.get("model") or {}).get("fingerprint"):
                reasons.append("model_config_changed")
            current["config_hash"] = self._recompute_config_hash(stored, current)
            if current["config_hash"] != job.get("config_hash"):
                reasons.append("config_hash_changed")
        return {
            "job_id": job_id,
            "valid": not reasons,
            "reasons": sorted(set(reasons)),
            "current": current,
            "stored": {
                "suite": stored.get("suite"),
                "model": stored.get("model"),
                "candidate_package_hash": stored.get("candidate_package_hash"),
                "config_hash": job.get("config_hash"),
            },
        }

    # ================================================================== 内部：配对与覆盖
    def _pair_plan(self, job: dict, suite: dict, cases: list[dict]) -> list[dict]:
        plan = []
        for case in cases:
            reps = int(case.get("repetitions") or suite.get("thresholds", {}).get("repetitions")
                       or DEFAULT_EVAL_REPETITIONS)
            for repetition in range(1, reps + 1):
                plan.append(
                    {
                        "case_id": case["id"],
                        "case_name": case.get("name"),
                        "role": case.get("role"),
                        "repetition": repetition,
                        "planned": reps,
                    }
                )
        return plan

    def _repetitions_of(self, suite: dict, cases: list[dict]) -> int:
        reps = {int(c.get("repetitions") or (suite.get("thresholds") or {}).get("repetitions")
                    or DEFAULT_EVAL_REPETITIONS) for c in cases}
        return max(reps) if reps else DEFAULT_EVAL_REPETITIONS

    def _pending_pairs(self, job: dict) -> tuple[list[dict], list[dict]]:
        suite_row = self.repo.get("eval_suite", job["suite_id"])
        suite = row_to_dict(suite_row, json_fields=("rubric", "critical_checks", "thresholds")) or {}
        cases = self.suites.get_cases(job["suite_id"], include_expected=False)
        plan = self._pair_plan(job, suite, cases)
        rows = self._result_rows(job["id"])
        by_key = {(r["case_id"], int(r["repetition"])): r for r in rows}
        pending = []
        for item in plan:
            row = by_key.get((item["case_id"], item["repetition"]))
            status = row["result_status"] if row else None
            if status == "ok" or status == "failed_checks":
                continue  # 真实失败计入，不重测
            item = dict(item)
            item["result_status"] = status or "pending"
            pending.append(item)
        return plan, pending

    def _coverage(self, job: dict, suite: dict, cases: list[dict], plan: list[dict],
                  rows: list[dict]) -> dict:
        case_map = {c["id"]: c for c in cases}
        planned_pairs = len(plan)
        per_case_planned: dict[str, int] = {}
        for item in plan:
            per_case_planned[item["case_id"]] = per_case_planned.get(item["case_id"], 0) + 1
        statuses = {}
        for row in rows:
            statuses[row["result_status"]] = statuses.get(row["result_status"], 0) + 1
        executions = 0
        for row in rows:
            executions += 1 if row.get("baseline_run_id") else 0
            executions += 1 if row.get("candidate_run_id") else 0
        judged = 0
        disagreements = 0
        disclosures = []
        for row in rows:
            judge = row.get("judge") or {}
            if judge.get("baseline_score") is not None or judge.get("candidate_score") is not None:
                judged += 1
                if judge.get("disagreement"):
                    disagreements += 1
            if judge.get("disclosure"):
                disclosures.append(judge["disclosure"])
        usage = self._usage_state(job["id"])
        baseline_ids: set[str] = set()
        candidate_ids: set[str] = set()
        for row in rows:
            baseline = row.get("baseline_checks") or {}
            candidate = row.get("candidate_checks") or {}
            baseline_ids.update(baseline.get("model_ids") or [])
            candidate_ids.update(candidate.get("model_ids") or [])
        added = set()
        if baseline_ids:
            added.update(baseline_ids)
        if candidate_ids:
            added.update(candidate_ids)
        model_drift = bool(baseline_ids and candidate_ids and baseline_ids != candidate_ids)
        observed = sorted(added) if (baseline_ids or candidate_ids) else []
        pairing = self._pairing_check(rows)
        isolation = self._isolation_check(rows)
        environment_mismatch = False
        for row in rows:
            notes = row.get("notes") or {}
            if notes.get("environment_mismatch"):
                environment_mismatch = True
        if not isolation.get("isolated", True):
            environment_mismatch = True
        if not pairing.get("equal", True):
            environment_mismatch = True
        if model_drift:
            environment_mismatch = True
        holdout = len([c for c in cases if c.get("role") == "holdout"])
        return {
            "cases": len(cases),
            "holdout": holdout,
            "repetitions": self._repetitions_of(suite, cases) if cases else 0,
            "planned_pairs": planned_pairs,
            "planned_executions": planned_pairs * 2,
            "per_case_planned": per_case_planned,
            "executions": executions,
            "complete_pairs": statuses.get("ok", 0),
            "undetermined_pairs": statuses.get("undetermined", 0),
            "infra_error_pairs": statuses.get("infra_error", 0),
            "cancelled_pairs": statuses.get("cancelled", 0),
            "budget_stopped_pairs": statuses.get("budget_stopped", 0),
            "pending_pairs": statuses.get("pending", 0),
            "missing_pairs": max(0, planned_pairs - len(rows)),
            "case_roles": {c["id"]: c.get("role") for c in cases},
            "complete": bool(
                planned_pairs
                and len(rows) == planned_pairs
                and statuses.get("ok", 0) == planned_pairs
            ),
            "hard_check_failed": bool(((job.get("config") or {}).get("overrides") or {}).get("hard_check_failed")),
            "isolated": bool(isolation.get("isolated", True)),
            "isolation": isolation,
            "pairing": pairing,
            "usage_reliable": int(usage.get("unknown_usage") or 0) == 0,
            "unknown_usage": int(usage.get("unknown_usage") or 0),
            "model_drift": model_drift,
            "observed_models": observed,
            "model_versions": {
                "requested": (job.get("config") or {}).get("model", {}).get("model"),
                "fingerprint": (job.get("config") or {}).get("model", {}).get("fingerprint"),
                "observed": observed,
                "baseline_observed": sorted(baseline_ids),
                "candidate_observed": sorted(candidate_ids),
                "drift": model_drift,
                "reproducibility_note": (
                    "服务返回的模型标识在两组的集合不同：判定为不可比，不授予 passed。"
                    if model_drift
                    else (
                        "已记录服务返回的模型标识。"
                        if observed
                        else "provider 未返回可核对的模型版本标识：无法保证服务端权重一致，"
                        "结论存在重现限制。"
                    )
                ),
            },
            "judged_pairs": judged,
            "judge_disagreements": disagreements,
            "judge_disclosure": disclosures[0] if disclosures else {},
            "environment_mismatch": environment_mismatch,
            "environment_mismatch_detail": (
                ([] if pairing.get("equal", True) else pairing.get("differences") or [])
                + ([] if isolation.get("isolated", True) else ["isolation"])
                + (["model_drift"] if model_drift else [])
            ),
            "budget_exceeded": job.get("checkpoint", {}).get("budget_stopped"),
            "elapsed_s": job.get("checkpoint", {}).get("elapsed_s"),
            "per_group_usage": usage.get("per_group") or {},
            "group_budget_note": (
                self._group_budget_blocked(usage, spec_from_budget(job.get("budget")))
                or "两组使用相同的单次执行预算，共用同一个总预算"
            ),
        }

    def _pairing_check(self, rows: list[dict]) -> dict:
        baseline_row = next((r for r in rows if r.get("baseline_run_id")), None)
        candidate_row = next((r for r in rows if r.get("candidate_run_id")), None)
        if baseline_row is None or candidate_row is None:
            return {"equal": True, "checks": {}, "differences": [], "note": "无配对可比较"}
        base = self.repo.get("run", baseline_row["baseline_run_id"])
        cand = self.repo.get("run", candidate_row["candidate_run_id"])
        if base is None or cand is None:
            return {"equal": False, "checks": {}, "differences": ["run_missing"]}
        checks = {
            "model_snapshot_equal": _json_equal_semantic(base["model_snapshot"], cand["model_snapshot"]),
            "tools_equal": _json_equal(base["tools_snapshot"], cand["tools_snapshot"]),
            "budget_equal": _json_equal(base["budget"], cand["budget"]),
            # 采样参数包含在模型快照里：同样按语义投影比较（排除 frozen_at）
            "sampling_equal": _json_equal_semantic(base["model_snapshot"], cand["model_snapshot"]),
            "learning_disabled": (
                int(base["learning_enabled"] or 0) == 0 and int(cand["learning_enabled"] or 0) == 0
            ),
            "origin_evaluation": base["origin"] == "evaluation" and cand["origin"] == "evaluation",
        }
        differences = [key for key, ok in checks.items() if not ok]
        base_perms = _loads(base["permissions"])
        cand_perms = _loads(cand["permissions"])
        # 路径（read_roots/write_roots/artifacts_dir）按组隔离是设计要求，比较语义权限即可
        permission_keys = ("tools", "network", "scripts", "allow_existing_write")
        same_perms = all(base_perms.get(k) == cand_perms.get(k) for k in permission_keys)
        checks["permissions_equal_except_paths"] = same_perms
        if not same_perms:
            differences.append("permissions_changed")
        if base_perms.get("artifacts_dir") == cand_perms.get("artifacts_dir"):
            differences.append("artifacts_dir_shared")
        try:
            base_skills = (_loads(base["input_snapshot"]).get("skills") or {}).get("chosen") or []
            cand_skills = (_loads(cand["input_snapshot"]).get("skills") or {}).get("chosen") or []
            base_versions = sorted(s.get("version_id") for s in base_skills)
            cand_versions = sorted(s.get("version_id") for s in cand_skills)
            checks["only_skill_version_differs"] = (
                len(base_versions) == len(cand_versions) == 1 and base_versions != cand_versions
            )
            if not checks["only_skill_version_differs"]:
                differences.append("skill_versions_not_pinned")
        except Exception:  # pragma: no cover
            checks["only_skill_version_differs"] = False
            differences.append("skill_snapshot_unreadable")
        return {
            "equal": not differences,
            "checks": checks,
            "differences": sorted(set(differences)),
            "baseline_run_id": base["id"],
            "candidate_run_id": cand["id"],
        }

    def _isolation_check(self, rows: list[dict]) -> dict:
        base_dirs, cand_dirs, base_reads, cand_reads = [], [], [], []
        for row in rows:
            for run_id, dirs, reads in (
                (row.get("baseline_run_id"), base_dirs, base_reads),
                (row.get("candidate_run_id"), cand_dirs, cand_reads),
            ):
                if not run_id:
                    continue
                run = self.repo.get("run", run_id)
                if run is None:
                    continue
                perms = _loads(run["permissions"])
                if perms.get("artifacts_dir"):
                    dirs.append(perms["artifacts_dir"])
                reads.extend(perms.get("read_roots") or [])
        overlap = bool(set(base_dirs) & set(cand_dirs))
        candidate_reads_baseline = bool(set(cand_reads) & set(base_dirs)) or bool(
            set(base_reads) & set(cand_dirs)
        )
        return {
            "isolated": not overlap and not candidate_reads_baseline,
            "separate_artifacts": not overlap,
            "candidate_can_read_baseline": candidate_reads_baseline,
            "baseline_artifacts_dirs": sorted(set(base_dirs))[:10],
            "candidate_artifacts_dirs": sorted(set(cand_dirs))[:10],
            "baseline_read_roots": sorted(set(base_reads))[:10],
            "candidate_read_roots": sorted(set(cand_reads))[:10],
            "note": "每组独立 artifacts 目录与输入根；候选读不到基线答案或残留产物",
        }

    def _artifact_map(self, job_id: str) -> dict[str, list[dict]]:
        """按 run 汇总产物指针（报告里失败项可直接打开产物）。"""
        rows = self.store.query(
            "SELECT run_id, path, size, content_hash FROM artifact WHERE run_id IN "
            "(SELECT baseline_run_id FROM eval_result WHERE job_id = ? AND baseline_run_id IS NOT NULL "
            " UNION SELECT candidate_run_id FROM eval_result WHERE job_id = ? AND candidate_run_id IS NOT NULL) "
            "ORDER BY run_id ASC, path ASC",
            (job_id, job_id),
        )
        out: dict[str, list[dict]] = {}
        for row in rows:
            out.setdefault(row["run_id"], []).append(
                {
                    "path": row["path"],
                    "size": int(row["size"] or 0),
                    "content_hash": row["content_hash"],
                }
            )
        return out

    def _canonical_result(self, row: dict, case_map: dict,
                          artifact_map: dict | None = None) -> dict:
        case = case_map.get(row["case_id"]) or {}
        artifact_map = artifact_map or {}
        notes = row.get("notes") or {}
        baseline = row.get("baseline_checks") or {}
        candidate = row.get("candidate_checks") or {}
        judge = row.get("judge") or {}
        return {
            "case_id": row["case_id"],
            "case_name": case.get("name") or row["case_id"],
            "role": case.get("role") or "regression",
            "repetition": int(row["repetition"]),
            "attempt": int(row.get("attempt") or 1),
            "order": notes.get("order"),
            "result_status": row["result_status"],
            "baseline_run_id": row.get("baseline_run_id"),
            "candidate_run_id": row.get("candidate_run_id"),
            "baseline": {
                "passed": baseline.get("passed"),
                "score": row.get("baseline_score"),
                "status": baseline.get("status"),
                "checks": baseline.get("checks") or [],
                "critical_failed": baseline.get("critical_failed") or [],
                "failed": baseline.get("failed") or [],
                "undetermined": baseline.get("undetermined") or [],
                "tokens": baseline.get("tokens"),
                "tokens_known": baseline.get("tokens_known"),
                "tool_actions": baseline.get("tool_actions"),
                "wall_s": baseline.get("wall_s"),
                "model_ids": baseline.get("model_ids") or [],
                "artifacts": artifact_map.get(row.get("baseline_run_id")) or [],
            },
            "candidate": {
                "passed": candidate.get("passed"),
                "score": row.get("candidate_score"),
                "status": candidate.get("status"),
                "checks": candidate.get("checks") or [],
                "critical_failed": candidate.get("critical_failed") or [],
                "failed": candidate.get("failed") or [],
                "undetermined": candidate.get("undetermined") or [],
                "tokens": candidate.get("tokens"),
                "tokens_known": candidate.get("tokens_known"),
                "tool_actions": candidate.get("tool_actions"),
                "wall_s": candidate.get("wall_s"),
                "model_ids": candidate.get("model_ids") or [],
                "artifacts": artifact_map.get(row.get("candidate_run_id")) or [],
            },
            "judge": judge,
            "attempt_history": notes.get("attempt_history") or [],
            "error_code": notes.get("error_code"),
            "error_message": notes.get("error_message"),
            "environment_mismatch": notes.get("environment_mismatch") or [],
            "pin": notes.get("pin") or {},
            "notes": notes.get("note"),
            "filled_by_resume": bool(notes.get("attempt", 1) > 1),
        }
    # ================================================================== 内部：收尾
    def _finalize(self, job_id, *, job, suite, cases, plan, thresholds, objective,
                  budget_stopped, cancelled, started_at, elapsed_s, attempt, resume_mode,
                  executed_now) -> None:
        rows = self._result_rows(job_id)
        case_map = {c["id"]: c for c in cases}
        artifact_map = self._artifact_map(job_id)
        results = [self._canonical_result(r, case_map, artifact_map) for r in rows]
        fresh = self._require_job(job_id)
        coverage = self._coverage(fresh, suite, cases, plan, rows)
        coverage["budget_exceeded"] = budget_stopped
        coverage["elapsed_s"] = elapsed_s
        coverage["resumed"] = resume_mode
        coverage["filled_pairs"] = executed_now
        if cancelled:
            status = "cancelled"
            error_code = "cancelled"
        elif budget_stopped:
            status = "budget_stopped"
            error_code = "budget_exceeded"
        elif not rows:
            status = "failed"
            error_code = "infrastructure_error"
        elif all(r["result_status"] == "infra_error" for r in rows):
            status = "failed"
            error_code = "infrastructure_error"
        else:
            status = "completed"
            error_code = None
        verdict = compute_verdict(
            results=results,
            thresholds=thresholds,
            coverage=coverage,
            objective=objective,
            budget_exceeded=budget_stopped,
        )
        finished_at = utc_now()
        fields = {
            "status": status,
            "verdict": verdict["verdict"],
            "verdict_detail": json.dumps(verdict, ensure_ascii=False, default=str),
            "usage": json.dumps(self._usage_state(job_id), ensure_ascii=False, default=str),
            "error_code": error_code,
            "finished_at": finished_at,
            "heartbeat_at": finished_at,
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": finished_at,
            "started_at": started_at,
        }
        checkpoint = dict(fresh.get("checkpoint") or {})
        checkpoint.update(
            {
                "phase": status,
                "completed_at": finished_at,
                "attempt": attempt,
                "done_pairs": len(rows),
                "planned_pairs": len(plan),
                "verdict": verdict["verdict"],
                "resumed": resume_mode,
                "filled_pairs": executed_now,
                "budget_stopped": budget_stopped,
            }
        )
        fields["checkpoint"] = checkpoint
        self._update_job(job_id, fields)
        self._finish_queue_job(job_id, status)
        self.build_report(job_id)
        try:
            self.apply_verdict(job_id)
        except AppError as exc:  # pragma: no cover - 候选缺失时不影响报告
            if self.events is not None:
                self.events.audit("eval.verdict_failed", "eval_job", job_id,
                                  reason=f"{exc.code}: {exc.message}")

    def _error_payload(self, pair: dict, code: str, message: str, attempt: int) -> dict:
        return {
            "case_id": pair["case_id"],
            "case_name": pair["case_name"],
            "role": pair["role"],
            "repetition": pair["repetition"],
            "attempt": attempt,
            "result_status": "infra_error",
            "baseline_run_id": None,
            "candidate_run_id": None,
            "baseline": {"passed": None, "score": None},
            "candidate": {"passed": None, "score": None},
            "judge": {},
            "error_code": code,
            "error_message": message,
            "notes": "配对执行前失败：按基础设施失败单列，不静默丢弃",
        }

    def _fail_job(self, job_id: str, code: str, message: str) -> None:
        now = utc_now()
        self._update_job(
            job_id,
            {"status": "failed", "error_code": code, "finished_at": now, "updated_at": now},
        )
        self._finish_queue_job(job_id, "failed", error_code=code, error_message=message)

    # ================================================================== 内部：运行环境
    def _eval_workspace(self, profile_id: str, skill_id: str) -> dict:
        """评测模板工作区（冻结权限与配置 hash 用）。"""
        if self.config is None:
            raise MissingDependency("缺少应用配置：无法建立隔离的评测目录")
        self._ensure_profile(profile_id)
        base = Path(self.config.home) / "evaluation" / str(skill_id)
        root = base / "inputs"
        artifacts = base / "artifacts"
        name = f"eval:{skill_id}"
        row = self.store.query_one(
            "SELECT * FROM workspace WHERE profile_id = ? AND root_path = ?",
            (profile_id, str(root)),
        )
        if row is None:
            workspace_id = new_id("ws")
            now = utc_now()
            with self.store.tx() as conn:
                self.repo.insert(
                    "workspace",
                    {
                        "id": workspace_id,
                        "profile_id": profile_id,
                        "name": name,
                        "root_path": str(root),
                        "artifact_path": str(artifacts),
                        "allowed_tools": json.dumps(self._allowed_tools(True), ensure_ascii=False),
                        "allow_network": 0,
                        "allow_scripts": 1,
                        "status": "active",
                        "created_at": now,
                        "updated_at": now,
                    },
                    conn=conn,
                )
            row = self.repo.get("workspace", workspace_id)
        return row_to_dict(row) or {}

    def _find_eval_workspace(self, profile_id: str, workspace_id) -> dict | None:
        if workspace_id:
            row = self.repo.get("workspace", workspace_id)
            if row is not None:
                return row_to_dict(row)
        row = self.store.query_one(
            "SELECT * FROM workspace WHERE profile_id = ? AND name LIKE 'eval:%' "
            "ORDER BY created_at ASC LIMIT 1",
            (profile_id,),
        )
        return row_to_dict(row) if row else None

    def _case_workspace(self, job: dict, case: dict, group: str, *, scripts_allowed: bool) -> dict:
        """每个 (作业, 用例, 组) 独立工作区：初始环境与产物目录隔离。"""
        if self.config is None:
            raise MissingDependency("缺少应用配置：无法建立隔离的评测目录")
        profile_id = job.get("profile_id") or "local"
        self._ensure_profile(profile_id)
        base = Path(self.config.home) / "evaluation" / job["id"] / case["id"] / group
        root = base / "inputs"
        artifacts = base / "artifacts"
        row = self.store.query_one(
            "SELECT * FROM workspace WHERE profile_id = ? AND root_path = ?",
            (profile_id, str(root)),
        )
        if row is None:
            workspace_id = new_id("ws")
            now = utc_now()
            with self.store.tx() as conn:
                self.repo.insert(
                    "workspace",
                    {
                        "id": workspace_id,
                        "profile_id": profile_id,
                        "name": f"eval:{job['id']}:{case['id']}:{group}",
                        "root_path": str(root),
                        "artifact_path": str(artifacts),
                        "allowed_tools": json.dumps(self._allowed_tools(scripts_allowed),
                                                    ensure_ascii=False),
                        "allow_network": 0,
                        "allow_scripts": 1 if scripts_allowed else 0,
                        "status": "active",
                        "created_at": now,
                        "updated_at": now,
                    },
                    conn=conn,
                )
            row = self.repo.get("workspace", workspace_id)
        root.mkdir(parents=True, exist_ok=True)
        (artifacts).mkdir(parents=True, exist_ok=True)
        self._write_fixtures(root, case)
        return row_to_dict(row) or {}

    def _write_fixtures(self, root: Path, case: dict) -> None:
        for item in case.get("input_files") or []:
            path = str(item.get("path") or "")
            if not path:
                continue
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            content = item.get("content") or ""
            if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != content:
                target.write_text(content, encoding="utf-8")

    def _ensure_profile(self, profile_id: str) -> None:
        if self.repo.get("profile", profile_id) is not None:
            return
        now = utc_now()
        self.repo.insert(
            "profile",
            {
                "id": profile_id,
                "name": profile_id,
                "kind": "local_owner",
                "status": "active",
                "created_at": now,
                "updated_at": now,
            },
        )

    def _allowed_tools(self, scripts_allowed: bool) -> list[str]:
        tools = list(EVAL_TOOLS)
        if scripts_allowed:
            tools.append(SCRIPT_TOOL)
        return tools

    def _scripts_allowed(self) -> bool:
        if self.container is None:
            return False
        try:
            status = self.container.status()
            return bool(getattr(status, "available", False))
        except Exception:  # pragma: no cover
            return False

    def _permissions_for_workspace(self, workspace: dict, *, scripts_allowed: bool) -> dict:
        allowed = _json_list(workspace.get("allowed_tools")) or list(EVAL_TOOLS)
        tools = [t for t in allowed if t != SCRIPT_TOOL or scripts_allowed]
        return {
            "workspace_id": workspace.get("id"),
            "read_roots": [workspace.get("root_path")],
            "write_roots": [workspace.get("artifact_path")],
            "tools": tools,
            "network": bool(workspace.get("allow_network")),
            "scripts": bool(workspace.get("allow_scripts")) and scripts_allowed,
            "allow_existing_write": False,
            "artifacts_dir_template": str(
                Path(workspace.get("artifact_path") or "") / "{run_id}"
            ),
        }

    # ================================================================== 内部：run 集成
    def _call_create_run(self, kwargs: dict) -> dict:
        if self.runs is None:
            raise MissingDependency("任务执行层不可用：无法创建评测 run")
        accepted = _accepted_kwargs(self.runs.create_run)
        filtered = (
            dict(kwargs) if accepted is None
            else {key: value for key, value in kwargs.items() if key in accepted}
        )
        result = self.runs.create_run(**filtered)
        if not isinstance(result, dict):
            raise InvalidInput("create_run 返回值非法")
        run_id = result.get("run_id") or result.get("id")
        if run_id:
            # 无论 RunService 是否原生支持评测标记，都兜底确认 origin/learning_enabled/eval_job_id
            self._enforce_eval_flags(run_id, kwargs.get("eval_job_id"))
        return result

    def _enforce_eval_flags(self, run_id: str, eval_job_id) -> None:
        row = self.repo.get("run", run_id)
        if row is None:
            return
        fields = {}
        if row["origin"] != "evaluation":
            fields["origin"] = "evaluation"
        if eval_job_id and row["eval_job_id"] != eval_job_id:
            fields["eval_job_id"] = eval_job_id
        if int(row["learning_enabled"] or 0) != 0:
            fields["learning_enabled"] = 0
        if fields:
            fields["updated_at"] = utc_now()
            self.repo.update("run", run_id, fields)

    def _ensure_pinned(self, run_id: str, skill_id: str, version_id: str) -> dict:
        """确认 run 冻结的技能版本就是目标版本。"""
        row = self.repo.get("run", run_id)
        if row is None:
            return {"pinned": False, "reason": "run_not_found"}
        snapshot = _loads(row["input_snapshot"])
        chosen = ((snapshot.get("skills") or {}).get("chosen")) or []
        if any(str(entry.get("version_id")) == str(version_id) for entry in chosen):
            return {"pinned": True, "mode": "native", "version_id": version_id}
        if row["execution_status"] != "queued":
            return {
                "pinned": False,
                "reason": "run_already_started",
                "actual": [entry.get("version_id") for entry in chosen],
            }
        return self._repin_snapshot(row, snapshot, skill_id, version_id)

    def _repin_snapshot(self, row, snapshot: dict, skill_id: str, version_id: str) -> dict:
        """兼容路径：RunService 不支持 skill_versions 时，在派发前重写冻结技能选择。

        这是**受控补偿**：只在 run 仍为 queued 时执行，重写后立即校验；任何不一致都
        返回未钉扎，让该配对按环境不可比处理，绝不静默用错版本出结论。
        """
        if self.skills is None:
            return {"pinned": False, "reason": "skills_unavailable"}
        try:
            files = self.skills.get_version_files(version_id)
        except AppError as exc:
            return {"pinned": False, "reason": f"version_unavailable:{exc.code}"}
        if not files:
            return {"pinned": False, "reason": "version_empty"}
        lead = "SKILL.md" if any(f["path"] == "SKILL.md" for f in files) else files[0]["path"]
        try:
            text = self.skills.read_version_file(version_id, lead)
        except AppError as exc:
            return {"pinned": False, "reason": f"read_failed:{exc.code}"}
        skills_snapshot = dict(snapshot.get("skills") or {})
        chosen = [dict(entry) for entry in (skills_snapshot.get("chosen") or [])]
        if chosen:
            for entry in chosen:
                if entry.get("skill_id") == skill_id:
                    entry["version_id"] = version_id
            if not any(entry.get("skill_id") == skill_id for entry in chosen):
                chosen.append({"skill_id": skill_id, "version_id": version_id})
        else:
            chosen = [{"skill_id": skill_id, "version_id": version_id}]
        skills_snapshot["chosen"] = chosen
        skills_snapshot["loaded"] = [
            {
                "skill_id": skill_id,
                "version_id": version_id,
                "file_path": lead,
                "load_kind": "full",
                "chars": len(text),
                "load_status": "loaded",
                "reason": "evaluation pin (post_freeze)",
            }
        ]
        permissions = _loads(row["permissions"])
        model_snapshot = _loads(row["model_snapshot"])
        new_hash = canonical_hash(
            {
                "request": snapshot.get("request"),
                "completion_criteria": snapshot.get("completion_criteria"),
                "workspace_id": row["workspace_id"],
                "workspace_root": (snapshot.get("workspace") or {}).get("root_path"),
                "input_files": snapshot.get("input_files"),
                "model": model_snapshot,
                "skills": skills_snapshot,
                "tools": json.loads(row["tools_snapshot"] or "[]"),
                "budget": snapshot.get("budget"),
                "permissions": {
                    key: permissions.get(key)
                    for key in ("read_roots", "write_roots", "artifacts_dir", "tools", "network",
                                "scripts", "allow_existing_write")
                },
            }
        )
        snapshot["skills"] = skills_snapshot
        snapshot["context_hash"] = new_hash
        with self.store.tx() as conn:
            self.repo.update(
                "run",
                row["id"],
                {
                    "input_snapshot": json.dumps(snapshot, ensure_ascii=False, default=str),
                    "context_hash": new_hash,
                    "skill_ids": json.dumps([skill_id], ensure_ascii=False),
                    "updated_at": utc_now(),
                },
                conn=conn,
            )
        verified = self.repo.get("run", row["id"])
        verify_snapshot = _loads(verified["input_snapshot"]) if verified is not None else {}
        ok = any(
            str(entry.get("version_id")) == str(version_id)
            for entry in ((verify_snapshot.get("skills") or {}).get("chosen") or [])
        )
        if not ok:
            return {"pinned": False, "reason": "repin_verify_failed"}
        return {
            "pinned": True,
            "mode": "post_freeze",
            "version_id": version_id,
            "context_hash": new_hash,
            "note": "RunService 未原生支持 skill_versions，已在派发前重写冻结技能选择并校验",
        }

    def _claim_run_job(self, queue_job_id) -> bool:
        """用 CAS 把 ``run.execute`` 作业标记为「由评测作业执行」。

        只有 ``status='queued'`` 时更新成功，说明没有 worker 会再领取它
        因此随后的同步执行不会与 WorkerPool 重复消费同一个 run。
        """
        if self.queue is None or not queue_job_id:
            return True
        now = utc_now()
        with self.store.tx() as conn:
            cursor = conn.execute(
                "UPDATE job SET status = 'completed', result = ?, error_code = NULL, "
                "lease_owner = NULL, lease_expires_at = NULL, finished_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (
                    json.dumps({"note": RUN_CLAIM_NOTE, "claimed_by": "eval"}, ensure_ascii=False),
                    now,
                    now,
                    queue_job_id,
                ),
            )
            claimed = cursor.rowcount == 1
        if claimed and self.events is not None:
            self.events.audit(
                "eval.run_claimed",
                "job",
                queue_job_id,
                after={"claimed_by": "eval"},
                reason=RUN_CLAIM_NOTE,
            )
        return claimed

    def _wait_for_run(self, run_id: str, worker_id: str, timeout: float | None = None) -> bool:
        """等待已由 worker 领取的 run 到达终态；超时返回 False（不重复执行）。"""
        deadline = time.monotonic() + float(timeout or RUN_WAIT_TIMEOUT_S)
        ticks = 0
        while time.monotonic() < deadline:
            row = self.repo.get("run", run_id)
            if row is not None and row["execution_status"] in (
                "completed", "failed", "cancelled", "budget_stopped"
            ):
                return True
            ticks += 1
            if ticks % 100 == 0:
                self._heartbeat_self(worker_id)
            time.sleep(RUN_WAIT_POLL_S)
        return False

    def _run_artifacts(self, run_id: str) -> list[dict]:
        if self.runs is None:
            return []
        try:
            if hasattr(self.runs, "get_artifacts"):
                return list(self.runs.get_artifacts(run_id) or [])
        except AppError:  # pragma: no cover
            return []
        rows = self.store.query("SELECT * FROM artifact WHERE run_id = ? ORDER BY path ASC", (run_id,))
        return [row_to_dict(r) or {} for r in rows]

    def _run_dir(self, run_id: str, workspace: dict, case: dict, group: str,
                 planned_dir: Path | None = None) -> Path:
        permissions = self._run_permissions(run_id)
        if permissions.get("artifacts_dir"):
            return Path(permissions["artifacts_dir"])
        if planned_dir is not None:
            return Path(planned_dir)
        return Path(workspace.get("artifact_path") or ".") / run_id

    def _run_permissions(self, run_id: str) -> dict:
        if self.runs is not None and hasattr(self.runs, "permissions_of"):
            try:
                return dict(self.runs.permissions_of(run_id) or {})
            except AppError:  # pragma: no cover
                pass
        row = self.repo.get("run", run_id)
        return _loads(row["permissions"]) if row is not None else {}

    def _run_usage(self, run_id: str, row) -> dict:
        usage = _loads(row["usage"]) if row is not None else {}
        calls = int(usage.get("model_calls") or 0)
        tokens = int(usage.get("tokens") or 0)
        unknown = int(usage.get("unknown_usage") or 0)
        tool_actions = usage.get("tool_actions") or usage.get("loop_summary", {}).get("tool_actions") or 0
        if self.budget is not None:
            try:
                state = self.budget.state(run_id)
                calls = max(calls, int(state.model_calls_used or 0))
                tokens = max(tokens, int(state.tokens_used or 0))
                unknown = max(unknown, int(state.unknown_usage or 0))
                tool_actions = max(int(tool_actions or 0), int(state.tool_actions_used or 0))
            except AppError:
                pass
        return {
            "calls": calls,
            "tokens": tokens,
            "tool_actions": tool_actions,
            "known": unknown == 0,
            "unknown_usage": unknown,
            "wall_s": _elapsed_seconds(row),
        }

    def _charge_job_budget(self, job_id: str, usage: dict) -> None:
        if self.budget is None:
            return
        try:
            self.budget.consume(
                job_id,
                calls=int(usage.get("calls") or 0),
                tokens=int(usage.get("tokens") or 0),
                tool_actions=int(usage.get("tool_actions") or 0),
                usage_known=bool(usage.get("known", True)),
            )
        except AppError:  # pragma: no cover - 记账失败不阻断
            pass

    def _observed_model_ids(self, run_id: str, row) -> list[str]:
        found: set[str] = set()
        usage = _loads(row["usage"]) if row is not None else {}
        for key in ("model_id", "model_ids", "observed_models"):
            value = usage.get(key)
            if isinstance(value, str):
                found.add(value)
            elif isinstance(value, (list, tuple)):
                found.update(str(v) for v in value if v)
        if self.runs is not None and hasattr(self.runs, "get_events"):
            try:
                for event in self.runs.get_events(run_id) or []:
                    payload = event.get("payload") or {}
                    for key in ("model_id", "model_ids", "observed_models"):
                        value = payload.get(key)
                        if isinstance(value, str) and value:
                            found.add(value)
                        elif isinstance(value, (list, tuple)):
                            found.update(str(v) for v in value if v)
            except AppError:  # pragma: no cover
                pass
        return sorted(found)

    def _artifacts_text(self, execution: dict, limit: int = 40_000) -> str:
        parts = []
        run_dir = execution.get("run_dir")
        for item in execution.get("artifacts") or []:
            path = str(item.get("path") or "")
            if not path:
                continue
            text = None
            if run_dir is not None:
                target = Path(run_dir) / path
                if target.is_file():
                    try:
                        text = target.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        text = None
            if text is None and self.blobs is not None and item.get("blob_id"):
                try:
                    if self.blobs.exists(item["blob_id"]):
                        text = self.blobs.get_text(item["blob_id"])
                except AppError:
                    text = None
            parts.append(f"--- {path} ---\n{(text or '(内容不可用)')[:limit]}")
        return "\n".join(parts) or "（无产物）"

    # ================================================================== 内部：预算
    def _budget_blocked(self, job_id: str, spec) -> str | None:
        if self.budget is None:
            return None
        try:
            state = self.budget.state(job_id)
        except AppError:
            return None
        if state.status != "open":
            return state.exceeded_type or "closed"
        if state.exceeded_type:
            return state.exceeded_type
        if state.remaining_calls < 1:
            return "max_model_calls"
        if state.remaining_tokens <= 0:
            return "max_tokens"
        if spec.max_wall_seconds and state.elapsed_s > spec.max_wall_seconds:
            return "max_wall_seconds"
        return None

    def _group_budget_blocked(self, usage: dict, spec) -> str | None:
        """两组预算相等性提示（不阻断）：仅用于报告披露。"""
        per_group = usage.get("per_group") or {}
        cap = max(1, int(spec.max_model_calls) // 2)
        for group in ("baseline", "candidate"):
            if int((per_group.get(group) or {}).get("calls") or 0) > cap:
                return f"group_over_half:{group}"
        return None

    def _reserve_pair(self, job_id: str) -> bool:
        """为一个配对预留两组各一次执行（两组预算相等、共用总预算）。"""
        if self.budget is None:
            return True
        try:
            if not self.budget.reserve_call(job_id, 0):
                return False
            if not self.budget.reserve_call(job_id, 0):
                self.budget.release_call(job_id, 0)
                return False
            return True
        except AppError:  # pragma: no cover - 账本缺失时不做硬阻断
            return True

    def _release_pair(self, job_id: str) -> None:
        if self.budget is None:
            return
        try:
            self.budget.release_call(job_id, 0)
            self.budget.release_call(job_id, 0)
        except AppError:  # pragma: no cover
            pass

    def _usage_state(self, job_id: str) -> dict:
        row = self.repo.get("eval_job", job_id)
        usage = _loads(row["usage"]) if row is not None else {}
        checkpoint = _loads(row["checkpoint"]) if row is not None else {}
        usage.setdefault("model_calls", 0)
        usage.setdefault("tokens", 0)
        usage.setdefault("tool_actions", 0)
        usage.setdefault("unknown_usage", 0)
        usage.setdefault(
            "per_group",
            checkpoint.get("per_group")
            or {"baseline": {"calls": 0, "tokens": 0}, "candidate": {"calls": 0, "tokens": 0}},
        )
        usage["total_tokens"] = usage.get("tokens")
        usage["usage_known"] = int(usage.get("unknown_usage") or 0) == 0
        if self.budget is not None:
            try:
                state = self.budget.state(job_id)
                usage["model_calls"] = state.model_calls_used
                usage["tokens"] = state.tokens_used
                usage["tool_actions"] = state.tool_actions_used
                usage["unknown_usage"] = state.unknown_usage
                usage["remaining_calls"] = state.remaining_calls
                usage["remaining_tokens"] = state.remaining_tokens
                usage["exceeded_type"] = state.exceeded_type
                usage["spec"] = state.spec
            except AppError:
                pass
        return usage

    def _persist_usage(self, job_id: str, usage: dict) -> None:
        row = self.repo.get("eval_job", job_id)
        checkpoint = _loads(row["checkpoint"]) if row is not None else {}
        checkpoint["per_group"] = usage.get("per_group") or {}
        self.repo.update(
            "eval_job",
            job_id,
            {
                "usage": json.dumps(usage, ensure_ascii=False, default=str),
                "checkpoint": json.dumps(checkpoint, ensure_ascii=False, default=str),
            },
        )

    # ================================================================== 内部：杂项
    def _rubric_for(self, job: dict) -> dict:
        suite_row = self.repo.get("eval_suite", job["suite_id"])
        if suite_row is None:
            return {}
        return _loads(suite_row["rubric"])

    def _author_model(self, job: dict) -> str | None:
        proposal = self.repo.get("proposal", job["proposal_id"])
        if proposal is None:
            return None
        evaluation = _loads(proposal["evaluation"]) if "evaluation" in proposal.keys() else {}
        author = evaluation.get("author_model") if isinstance(evaluation, dict) else None
        if author:
            return str(author)
        config = job.get("config") or {}
        return ((config.get("model") or {}).get("model")) or None

    def _forbidden_terms(self, job: dict) -> list[str]:
        return [
            str(job.get("baseline_version_id") or ""),
            str(job.get("candidate_version_id") or ""),
            str(job.get("proposal_id") or ""),
        ]

    def _judge_for_job(self, job_id: str) -> BlindJudge:
        if self.judge is not None:
            judge = self.judge
        else:
            judge = BlindJudge(model_factory=self.model_factory)
        if isinstance(judge, BlindJudge):
            judge.budget = self.budget
            judge.scope_id = job_id
            judge.budget_guard = lambda: self._budget_blocked_light(job_id)
        return judge

    def _budget_blocked_light(self, job_id: str) -> str | None:
        if self.budget is None:
            return None
        try:
            state = self.budget.state(job_id)
        except AppError:
            return None
        if state.status != "open":
            return state.exceeded_type or "closed"
        if state.remaining_calls < 1:
            return "max_model_calls"
        if state.remaining_tokens <= 0:
            return "max_tokens"
        return None

    def _criteria_text(self, case: dict) -> list:
        out = []
        for item in case.get("completion_criteria") or []:
            if isinstance(item, dict):
                out.append(item.get("criterion") or item.get("name"))
            else:
                out.append(str(item))
        return [text for text in out if text]

    def _skill_id_of_job(self, job: dict) -> str:
        proposal = self.repo.get("proposal", job["proposal_id"])
        if proposal is None:
            raise EvidenceUnavailable("候选已不可用：无法确定目标 skill")
        return proposal["skill_id"]

    def _require_proposal(self, proposal_id) -> dict:
        row = self.repo.get("proposal", proposal_id)
        if row is None:
            raise InvalidInput(f"候选不存在: {proposal_id}", field_errors={"proposal_id": "not_found"})
        return row_to_dict(row) or {}

    def _require_job(self, job_id) -> dict:
        row = self.repo.get("eval_job", job_id)
        if row is None:
            raise InvalidInput(f"评测作业不存在: {job_id}", field_errors={"job_id": "not_found"})
        return row_to_dict(row, json_fields=_JOB_JSON_FIELDS) or {}

    def _active_job_for(self, proposal_id) -> dict | None:
        row = self.store.query_one(
            "SELECT * FROM eval_job WHERE proposal_id = ? AND status IN ('queued','running','interrupted') "
            "ORDER BY created_at DESC LIMIT 1",
            (proposal_id,),
        )
        return row_to_dict(row, json_fields=_JOB_JSON_FIELDS) if row else None

    def _result_rows(self, job_id) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM eval_result WHERE job_id = ? ORDER BY created_at ASC, repetition ASC",
            (job_id,),
        )
        out = []
        for row in rows:
            data = row_to_dict(
                row, json_fields=("baseline_checks", "candidate_checks", "judge", "notes")
            ) or {}
            out.append(data)
        return out

    def _case_map_for_job(self, job_id) -> dict:
        job = self.repo.get("eval_job", job_id)
        if job is None:
            return {}
        cases = self.suites.get_cases(job["suite_id"], include_expected=False)
        return {c["id"]: c for c in cases}

    def _cancel_requested(self, job_id) -> bool:
        row = self.repo.get("eval_job", job_id)
        if row is None:
            return False
        if int(row["cancel_requested"] or 0):
            return True
        queue_job_id = (_loads(row["checkpoint"]) or {}).get("queue_job_id")
        if self.queue is not None and queue_job_id:
            try:
                return bool(self.queue.request_cancel_seen(queue_job_id))
            except AppError:  # pragma: no cover
                return False
        return False

    def _heartbeat(self, job_id: str, worker_id: str) -> None:
        queue_job_id = (self._require_job(job_id).get("checkpoint") or {}).get("queue_job_id")
        if self.queue is None or not queue_job_id:
            return
        try:
            if worker_id and self.queue.heartbeat(queue_job_id, worker_id):
                return
            # 调用方标签与租约所有者不一致（例如由内部 handler 触发）时按真实租约所有者续租
            job = self.queue.get(queue_job_id)
            if job.lease_owner:
                self.queue.heartbeat(queue_job_id, job.lease_owner)
        except AppError:  # pragma: no cover
            pass

    def _heartbeat_self(self, worker_id: str) -> None:
        """在等待 worker 跑 run 期间维持作业租约（避免租约过期被回收）。"""
        if self._current_job_id is None:
            return
        self._heartbeat(self._current_job_id, worker_id)

    def _finish_queue_job(self, job_id: str, status: str, *, error_code=None,
                          error_message=None) -> None:
        if self.queue is None:
            return
        queue_job_id = (self._require_job(job_id).get("checkpoint") or {}).get("queue_job_id")
        if not queue_job_id:
            return
        mapped = _JOB_STATUS_BY_RESULT.get(status, status)
        try:
            self.queue.finish(
                queue_job_id,
                mapped,
                result={"eval_job_id": job_id, "status": status},
                error_code=error_code,
                error_message=error_message,
            )
        except AppError:  # pragma: no cover - 队列中不存在或已是终态
            pass

    def _update_job(self, job_id: str, fields: dict) -> None:
        payload = dict(fields)
        for key in ("config", "budget", "usage", "verdict_detail", "report", "checkpoint"):
            if key in payload and isinstance(payload[key], (dict, list)):
                payload[key] = json.dumps(payload[key], ensure_ascii=False, default=str)
        payload.setdefault("updated_at", utc_now())
        self.repo.update("eval_job", job_id, payload)

    def _set_checkpoint(self, job_id: str, patch: dict) -> None:
        row = self.repo.get("eval_job", job_id)
        checkpoint = _loads(row["checkpoint"]) if row is not None else {}
        checkpoint.update(patch)
        self.repo.update(
            "eval_job",
            job_id,
            {"checkpoint": json.dumps(checkpoint, ensure_ascii=False, default=str), "updated_at": utc_now()},
        )

    def _assert_version_complete(self, version_id, *, what: str) -> None:
        if self.skills is None:
            raise MissingDependency("技能层不可用")
        try:
            version = self.skills.get_version(version_id)
        except AppError as exc:
            raise EvidenceUnavailable(f"{what}不可用: {version_id}（{exc.code}）") from exc
        missing = []
        for entry in version.get("files") or []:
            if self.blobs is None:
                break
            if not self.blobs.exists(entry.get("blob_id") or ""):
                missing.append(entry.get("path"))
        if missing:
            raise EvidenceUnavailable(
                f"{what}缺少制品，无法用于对照评测: {', '.join(str(m) for m in missing[:5])}"
            )

    def _package_hash(self, version_id) -> str | None:
        if self.skills is None:
            return None
        version = self.skills.get_version(version_id)
        return version.get("package_hash")

    def _dependency_versions(self, profile_id: str, skill_id: str) -> dict:
        if self.skills is None:
            return {}
        try:
            skills = self.skills.list_skills(include_archived=False, profile_id=profile_id)
        except Exception:  # pragma: no cover
            return {}
        out = {}
        for item in skills:
            if item.get("id") == skill_id:
                continue
            version = item.get("current_version_id") or item.get("version_id")
            if version:
                out[item.get("slug") or item.get("id")] = version
        return dict(sorted(out.items()))

    def _model_info(self) -> dict:
        if self.model_factory is None:
            return {}
        try:
            client = self.model_factory()
        except Exception as exc:  # pragma: no cover
            return {"error": str(exc)}
        if client is None:
            return {}
        try:
            data = client.describe()
        except Exception:  # pragma: no cover
            data = {}
        config = getattr(client, "config", None)
        if config is not None:
            data.setdefault("model", config.model)
            data.setdefault("base_url", config.base_url)
            data.setdefault("temperature", config.temperature)
            data.setdefault("max_output_tokens", config.max_output_tokens)
            data.setdefault("timeout_s", config.timeout_s)
            data.setdefault("extra", dict(config.extra or {}))
            # base_url 可能含本机端口：指纹已覆盖，报告只保留模型名与指纹
        try:
            data.setdefault("fingerprint", client.fingerprint())
        except Exception:  # pragma: no cover
            pass
        return data

    def _pricing(self) -> dict:
        config = self.config
        if config is None:
            return {}
        extra = getattr(config, "extra", None) or {}
        pricing = extra.get("model_pricing") if isinstance(extra, dict) else None
        return dict(pricing or {})

    def _recompute_config_hash(self, stored: dict, current: dict) -> str:
        rebuilt = json.loads(json.dumps(stored, default=str))
        suite = rebuilt.get("suite") or {}
        if current.get("suite_hash") is not None:
            suite["suite_hash"] = current["suite_hash"]
        if current.get("rubric_hash") is not None:
            suite["rubric_hash"] = current["rubric_hash"]
        if current.get("thresholds_hash") is not None:
            suite["thresholds_hash"] = current["thresholds_hash"]
        rebuilt["suite"] = suite
        if current.get("candidate_package_hash") is not None:
            rebuilt["candidate_package_hash"] = current["candidate_package_hash"]
        if current.get("tool_permissions") is not None:
            rebuilt["tool_permissions"] = current["tool_permissions"]
        if current.get("model"):
            rebuilt["model"] = current["model"]
        return canonical_hash(rebuilt)


# --------------------------------------------------------------------- 辅助
def _score_of(side) -> float | None:
    if not isinstance(side, dict):
        return None
    value = side.get("score")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _loads(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


#: 冻结快照中的非语义字段：只记录"何时冻结"，不参与两组可比性判断（否则每次评测
#: 都会被判为环境不可比，结论恒为 inconclusive）。
_VOLATILE_SNAPSHOT_KEYS = {"frozen_at", "created_at", "updated_at"}


def _semantic_snapshot(value) -> dict:
    """去掉时间戳等易变字段后的快照投影，用于两组一致性比较。"""
    data = _loads(value)
    return {k: v for k, v in (data or {}).items() if k not in _VOLATILE_SNAPSHOT_KEYS}


def _json_equal(a, b) -> bool:
    return _loads(a) == _loads(b) or _loads_list(a) == _loads_list(b)


def _json_equal_semantic(a, b) -> bool:
    return _semantic_snapshot(a) == _semantic_snapshot(b)


def _loads_list(value):
    if isinstance(value, list):
        return value
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return None
    return data


def _json_list(value) -> list:
    """把库里存的 JSON 列表（字符串或列表）读成 list[str]。"""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def _accepted_kwargs(func) -> set[str] | None:
    """函数接受的关键字参数名；``**kwargs`` 表示接受任意关键字（返回 None）。"""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover
        return set()
    names = set()
    for name, param in signature.parameters.items():
        if param.kind == param.VAR_KEYWORD:
            return None
        if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            names.add(name)
    return names


def _permissions_equal(a: dict, b: dict) -> bool:
    keys = ("tools", "read_roots", "write_roots", "network", "scripts", "allow_existing_write")
    return all((a or {}).get(k) == (b or {}).get(k) for k in keys)


def _elapsed_seconds(row) -> float | None:
    if row is None:
        return None
    started = row["started_at"]
    finished = row["finished_at"]
    if not started or not finished:
        return None
    from ..core.clock import parse_ts

    try:
        return round((parse_ts(finished) - parse_ts(started)).total_seconds(), 3)
    except Exception:  # pragma: no cover
        return None
