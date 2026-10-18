from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from ..core.clock import parse_ts, utc_now

#: 最小样本量：低于此值不得把比率当结论
MIN_SAMPLE = 5
#: 审阅负担单次计时上限（10 分钟）
REVIEW_DURATION_CAP_MS = 600_000
#: 回退率观察窗口
OBSERVATION_WINDOW_DAYS = 14
#: 有效改进要求的后续任务使用次数
EFFECTIVE_IMPROVEMENT_MIN_USES = 3

EXPLICIT_OUTCOMES = ("accepted", "partially_accepted", "rejected")

#: 候选状态分类：等待评测 / 等待用户不进入采纳率分母
PROPOSAL_WAITING_EVAL = {
    "draft",
    "generating",
    "checking",
    "checks_failed",
    "queued",
    "evaluating",
    "awaiting_eval",
    "pending",
    "in_progress",
}
PROPOSAL_WAITING_USER = {
    "eligible",
    "ready",
    "ready_for_review",
    "awaiting_review",
    "review",
    "passed",
}
PROPOSAL_FINAL_POSITIVE = {"activated", "adopted", "accepted"}
PROPOSAL_FINAL_NEGATIVE = {"rejected", "declined", "withdrawn"}

ROLLBACK_REGRESSION = {"quality_regression", "effect_regression", "regression", "verdict_regression"}
ROLLBACK_OPERATION_ERROR = {"user_mistake", "operation_error", "wrong_activation", "misclick", "manual_error"}
ROLLBACK_ENVIRONMENT = {
    "environment_change",
    "env_change",
    "dependency_missing",
    "container_unavailable",
    "tool_unavailable",
}

MEMORY_METRICS = (
    "memory_recall_hit_rate",
    "memory_integration_accuracy",
    "memory_deletion_effectiveness",
)


class MetricsService:
    """核心指标（P0 + P1 记忆类）。"""

    def __init__(self, store, repo, memory=None) -> None:
        self.store = store
        self.repo = repo
        #: P1 记忆服务：未装配时记忆指标明确返回 not_applicable，不伪造 0
        self.memory = memory

    # ------------------------------------------------------------------ 入口
    def dashboard(self, *, workspace_id: str | None = None, skill_id: str | None = None) -> dict:
        restrict = bool(workspace_id or skill_id)
        runs = self._runs(workspace_id=workspace_id, skill_id=skill_id)
        run_ids = {r["id"] for r in runs}
        skills = self._skills(workspace_id=workspace_id, skill_id=skill_id)
        skill_ids = {s["id"] for s in skills}
        versions = self._versions(skill_ids, restrict=restrict)
        proposals = self._proposals(skill_ids, restrict=restrict)
        proposal_ids = {p["id"] for p in proposals}

        feedback = self._feedback(run_ids, skill_ids, restrict=restrict)
        lessons = self._lessons(skill_ids, restrict=restrict)
        activations = self._activations(skill_ids, restrict=restrict)
        eval_jobs = self._eval_jobs(proposal_ids, restrict=restrict)

        first_pass = self._first_pass_acceptance(runs, feedback)
        coverage = self._acceptance_coverage(runs)
        repeat = self._repeat_correction(runs, lessons, feedback)
        effective = self._effective_improvements(activations, versions)
        adoption = self._proposal_adoption(proposals, activations, eval_jobs)
        rollback = self._rollback_rate(activations)
        cost = self._learning_cost_share(run_ids=run_ids, restrict=restrict)
        burden = self._review_burden(proposals)

        metrics = {
            "first_pass_acceptance": first_pass,
            "acceptance_coverage": coverage,
            "repeat_correction": repeat,
            "effective_improvements": effective,
            "proposal_adoption": adoption,
            "rollback_rate": rollback,
            "learning_cost_share": cost,
            "review_burden": burden,
        }
        insufficient = sorted(
            name
            for name, payload in metrics.items()
            if isinstance(payload, dict) and payload.get("evidence_insufficient")
        )
        return {
            "generated_at": utc_now(),
            "filters": {"workspace_id": workspace_id, "skill_id": skill_id},
            "min_sample": MIN_SAMPLE,
            "review_duration_cap_ms": REVIEW_DURATION_CAP_MS,
            "observation_window_days": OBSERVATION_WINDOW_DAYS,
            "evidence_insufficient": bool(insufficient),
            "evidence_insufficient_metrics": insufficient,
            "metrics": metrics,
            "memory": self._memory_metrics(workspace_id=workspace_id),
            "data_quality": {
                "runs_total": len(runs),
                "runs_completed": len([r for r in runs if r.get("execution_status") == "completed"]),
                "feedback_total": len(feedback),
                "lessons_total": len(lessons),
                "proposals_total": len(proposals),
                "activations_total": len(activations),
                "eval_jobs_total": len(eval_jobs),
                "skills_total": len(skills),
            },
        }

    # ------------------------------------------------------------------ 记忆指标
    def _memory_metrics(self, *, workspace_id: str | None = None) -> dict:
        """记忆类指标；样本不足时明确 evidence_insufficient，不当作能力提升。"""
        if self.memory is None:
            return {
                "applicable": False,
                "status": "not_applicable",
                "reason": "记忆服务未装配，返回 not_applicable 而非 0",
                "metrics": list(MEMORY_METRICS),
            }
        payload = self.memory.metrics(workspace_id=workspace_id)
        payload.setdefault("metrics_order", list(MEMORY_METRICS))
        return payload

    # ------------------------------------------------------------------ 数据选择
    def _runs(self, *, workspace_id: str | None, skill_id: str | None) -> list[dict]:
        sql = "SELECT * FROM run"
        clauses: list[str] = []
        params: list = []
        if workspace_id:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if skill_id:
            clauses.append("id IN (SELECT run_id FROM skill_load WHERE skill_id = ?)")
            params.append(skill_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        return [dict(r) for r in self.store.query(sql, params)]

    def _skills(self, *, workspace_id: str | None, skill_id: str | None) -> list[dict]:
        sql = "SELECT * FROM skill"
        clauses: list[str] = []
        params: list = []
        if skill_id:
            clauses.append("id = ?")
            params.append(skill_id)
        if workspace_id:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [dict(r) for r in self.store.query(sql, params)]

    def _versions(self, skill_ids: set[str], *, restrict: bool) -> list[dict]:
        rows = [dict(r) for r in self.store.query("SELECT * FROM skill_version")]
        if not restrict:
            return rows
        return [row for row in rows if row.get("skill_id") in skill_ids]

    def _feedback(self, run_ids: set[str], skill_ids: set[str], *, restrict: bool) -> list[dict]:
        rows = [dict(r) for r in self.store.query("SELECT * FROM feedback")]
        if not restrict:
            return rows
        return [
            row
            for row in rows
            if (row.get("run_id") in run_ids) or (row.get("target_skill_id") in skill_ids)
        ]

    def _lessons(self, skill_ids: set[str], *, restrict: bool) -> list[dict]:
        rows = [dict(r) for r in self.store.query("SELECT * FROM lesson")]
        if not restrict:
            return rows
        return [row for row in rows if row.get("target_skill_id") in skill_ids]

    def _proposals(self, skill_ids: set[str], *, restrict: bool) -> list[dict]:
        rows = [dict(r) for r in self.store.query("SELECT * FROM proposal")]
        if not restrict:
            return rows
        return [row for row in rows if row.get("skill_id") in skill_ids]

    def _activations(self, skill_ids: set[str], *, restrict: bool) -> list[dict]:
        rows = [dict(r) for r in self.store.query("SELECT * FROM activation")]
        if not restrict:
            return rows
        return [row for row in rows if row.get("skill_id") in skill_ids]

    def _eval_jobs(self, proposal_ids: set[str], *, restrict: bool) -> list[dict]:
        rows = [dict(r) for r in self.store.query("SELECT * FROM eval_job")]
        if not restrict:
            return rows
        return [row for row in rows if row.get("proposal_id") in proposal_ids]

    # ------------------------------------------------------------------ 指标
    def _first_pass_acceptance(self, runs: list[dict], feedback: list[dict]) -> dict:
        corrections: dict[str, int] = {}
        acceptance: dict[str, str] = {}
        for row in feedback:
            run_id = row.get("run_id")
            if not run_id or row.get("status") == "withdrawn":
                continue
            if row.get("type") == "correction":
                corrections[run_id] = corrections.get(run_id, 0) + 1
            if row.get("type") in ("acceptance", "rejection") and row.get("verdict"):
                acceptance[run_id] = row["verdict"]

        completed = [r for r in runs if r.get("execution_status") == "completed"]
        explicit: list[dict] = []
        unknown: list[dict] = []
        for run in completed:
            outcome = run.get("outcome")
            if outcome in EXPLICIT_OUTCOMES or run["id"] in acceptance:
                explicit.append(run)
            else:
                unknown.append(run)

        accepted = 0
        partially = 0
        rejected = 0
        first_pass = 0
        for run in explicit:
            outcome = run.get("outcome")
            if outcome not in EXPLICIT_OUTCOMES:
                outcome = acceptance.get(run["id"])
            if outcome == "accepted":
                accepted += 1
            elif outcome == "partially_accepted":
                partially += 1
            elif outcome == "rejected":
                rejected += 1
            if outcome == "accepted" and not corrections.get(run["id"]):
                first_pass += 1

        denominator = len(explicit)
        return {
            "metric": "first_pass_acceptance",
            "description": "首次输出即 accepted 的任务数 / 所有完成且已验收任务数",
            "value": _rate(first_pass, denominator),
            "numerator": first_pass,
            "denominator": denominator,
            "completed_total": len(completed),
            "unknown_outcome_count": len(unknown),
            "accepted_count": accepted,
            "partially_accepted_count": partially,
            "rejected_count": rejected,
            "corrected_run_count": len(corrections),
            "sample_size": denominator,
            "required_sample": MIN_SAMPLE,
            "evidence_insufficient": denominator < MIN_SAMPLE,
        }

    def _acceptance_coverage(self, runs: list[dict]) -> dict:
        completed = [r for r in runs if r.get("execution_status") == "completed"]
        explicit = [r for r in completed if r.get("outcome") in EXPLICIT_OUTCOMES]
        unknown = [r for r in completed if r.get("outcome") not in EXPLICIT_OUTCOMES]
        return {
            "metric": "acceptance_coverage",
            "description": "有明确 outcome 的已完成任务数 / 全部已完成任务数",
            "value": _rate(len(explicit), len(completed)),
            "numerator": len(explicit),
            "denominator": len(completed),
            "unknown_outcome_count": len(unknown),
            "sample_size": len(completed),
            "required_sample": MIN_SAMPLE,
            "evidence_insufficient": len(completed) < MIN_SAMPLE,
            "note": "unknown 单列，不计入成功",
        }

    def _repeat_correction(
        self, runs: list[dict], lessons: list[dict], feedback: list[dict]
    ) -> dict:
        activated = [l for l in lessons if l.get("status") == "activated"]
        targets = {
            (l.get("target_skill_id"), l.get("target_version_id"))
            for l in activated
            if l.get("target_skill_id") or l.get("target_version_id")
        }
        run_ids = {r["id"] for r in runs}
        loads = [
            dict(r) for r in self.store.query("SELECT run_id, skill_id, version_id FROM skill_load")
        ]
        covered: set[str] = set()
        for row in loads:
            if run_ids and row["run_id"] not in run_ids:
                continue
            if any(
                (skill_id and row["skill_id"] == skill_id)
                or (version_id and row["version_id"] == version_id)
                for skill_id, version_id in targets
            ):
                covered.add(row["run_id"])

        corrections = [
            row
            for row in feedback
            if row.get("type") == "correction"
            and row.get("status") != "withdrawn"
            and (not run_ids or row.get("run_id") in run_ids)
        ]
        matched = 0
        insufficient = 0
        out_of_scope = 0
        by_skill: dict[str, dict] = {}
        for row in corrections:
            target_skill = row.get("target_skill_id")
            target_version = row.get("target_version_id")
            if not (target_skill or target_version):
                insufficient += 1
                continue
            if row.get("run_id") in covered:
                matched += 1
                bucket = by_skill.setdefault(
                    target_skill or target_version or "unknown", {"numerator": 0, "run_ids": []}
                )
                bucket["numerator"] += 1
                if row.get("run_id"):
                    bucket["run_ids"].append(row["run_id"])
            else:
                out_of_scope += 1
        denominator = len(covered)
        return {
            "metric": "repeat_correction",
            "description": "同一已启用经验在同类任务中再次被纠正的次数 / 使用该版的可比任务数",
            "value": _rate(matched, denominator),
            "numerator": matched,
            "denominator": denominator,
            "attribution_insufficient_count": insufficient,
            "uncovered_correction_count": out_of_scope,
            "activated_lesson_count": len(activated),
            "correction_total": len(corrections),
            "by_skill": {
                key: {"numerator": value["numerator"], "run_ids": sorted(set(value["run_ids"]))}
                for key, value in sorted(by_skill.items())
            },
            "sample_size": denominator,
            "required_sample": MIN_SAMPLE,
            "evidence_insufficient": denominator < MIN_SAMPLE,
            "note": "关联不足单列；模型匹配仅辅助，需用户确认重复关系",
        }

    def _effective_improvements(self, activations: list[dict], versions: list[dict]) -> dict:
        version_ids = {v["id"] for v in versions}
        candidates = [
            a
            for a in activations
            if a.get("origin") == "proposal"
            and a.get("report_id")
            and (not version_ids or a.get("version_id") in version_ids)
        ]
        manual_trials = [
            a for a in activations if a.get("origin") == "manual_trial" and a.get("version_id")
        ]
        qualified: list[dict] = []
        for activation in candidates:
            uses = self._subsequent_uses(activation["version_id"], activation.get("created_at"))
            if len(uses) >= EFFECTIVE_IMPROVEMENT_MIN_USES:
                qualified.append(
                    {
                        "activation_id": activation["id"],
                        "skill_id": activation.get("skill_id"),
                        "version_id": activation["version_id"],
                        "report_id": activation.get("report_id"),
                        "subsequent_uses": len(uses),
                        "run_ids": uses[:20],
                    }
                )
        return {
            "metric": "effective_improvements",
            "description": "passed 后启用，且至少 3 次后续任务使用的版本数",
            "value": len(qualified),
            "numerator": len(qualified),
            "denominator": len(candidates),
            "min_subsequent_uses": EFFECTIVE_IMPROVEMENT_MIN_USES,
            "versions": qualified,
            "manual_trial_excluded": len(manual_trials),
            "sample_size": len(candidates),
            "required_sample": MIN_SAMPLE,
            "evidence_insufficient": len(candidates) < MIN_SAMPLE,
            "note": "不包含手工试用，不能等同于长期普遍有效",
        }

    def _subsequent_uses(self, version_id: str, since: str | None) -> list[str]:
        rows = self.store.query(
            "SELECT DISTINCT sl.run_id, r.created_at FROM skill_load sl "
            "JOIN run r ON r.id = sl.run_id WHERE sl.version_id = ?",
            (version_id,),
        )
        threshold = parse_ts(since)
        uses = []
        for row in rows:
            if row["run_id"] is None:
                continue
            created = parse_ts(row["created_at"])
            if threshold and created and created <= threshold:
                continue
            uses.append(row["run_id"])
        return sorted(set(uses))

    def _proposal_adoption(
        self, proposals: list[dict], activations: list[dict], eval_jobs: list[dict]
    ) -> dict:
        activated_ids = {a.get("proposal_id") for a in activations if a.get("proposal_id")}
        verdicts = {j.get("proposal_id"): j.get("verdict") for j in eval_jobs}
        funnel: dict[str, int] = {}
        waiting_eval = 0
        waiting_user = 0
        other = 0
        numerator = 0
        denominator = 0
        for proposal in proposals:
            status = (proposal.get("status") or "unknown").lower()
            funnel[status] = funnel.get(status, 0) + 1
            is_activated = status in PROPOSAL_FINAL_POSITIVE or proposal["id"] in activated_ids
            if is_activated:
                numerator += 1
                denominator += 1
                continue
            if status in PROPOSAL_FINAL_NEGATIVE:
                denominator += 1
                continue
            if status in PROPOSAL_WAITING_USER:
                waiting_user += 1
                continue
            if status in PROPOSAL_WAITING_EVAL or status == "unknown":
                waiting_eval += 1
                continue
            other += 1
        return {
            "metric": "proposal_adoption",
            "description": "经评测启用的候选数 / 已经获得用户最终审阅的候选数",
            "value": _rate(numerator, denominator),
            "numerator": numerator,
            "denominator": denominator,
            "excluded_waiting_eval": waiting_eval,
            "excluded_waiting_user": waiting_user,
            "excluded_other": other,
            "eval_verdicts": {k: v for k, v in verdicts.items() if k},
            "funnel": dict(sorted(funnel.items())),
            "funnel_total": len(proposals),
            "sample_size": denominator,
            "required_sample": MIN_SAMPLE,
            "evidence_insufficient": denominator < MIN_SAMPLE,
            "note": "等待评测 / 等待用户不进入分母；另报漏斗全量",
        }

    def _rollback_rate(self, activations: list[dict]) -> dict:
        forward_origins = {"import", "draft", "proposal", "manual_trial", "restore"}
        forward = [a for a in activations if (a.get("origin") or "") in forward_origins]
        rollbacks = [a for a in activations if a.get("origin") == "rollback"]
        in_window = [a for a in forward if self._in_observation_window(a)]
        rollbacks_in_window = [a for a in rollbacks if self._in_observation_window(a)]

        buckets = {"effect_regression": 0, "operation_error": 0, "environment_change": 0, "other": 0}
        regression = 0
        for activation in rollbacks:
            kind = classify_rollback_reason(activation.get("reason"))
            buckets[kind] += 1
            if kind == "effect_regression":
                regression += 1
        denominator = len(in_window)
        return {
            "metric": "rollback_rate",
            "description": "用户因效果退化回退的启用数 / 达到观察窗口的启用数",
            "value": _rate(regression, denominator),
            "numerator": regression,
            "denominator": denominator,
            "rollback_total": len(rollbacks),
            "rollbacks_in_window": len(rollbacks_in_window),
            "operation_error_count": buckets["operation_error"],
            "environment_change_count": buckets["environment_change"],
            "other_reason_count": buckets["other"],
            "by_reason": buckets,
            "observation_window_days": OBSERVATION_WINDOW_DAYS,
            "sample_size": denominator,
            "required_sample": MIN_SAMPLE,
            "evidence_insufficient": denominator < MIN_SAMPLE,
            "note": "操作错误、环境变化分开统计，不混入效果退化",
        }

    def _in_observation_window(self, activation: dict) -> bool:
        created = parse_ts(activation.get("created_at"))
        now = datetime.now(timezone.utc)
        if created is not None and (now - created) >= timedelta(days=OBSERVATION_WINDOW_DAYS):
            return True
        version_id = activation.get("version_id")
        if not version_id:
            return False
        uses = self._subsequent_uses(version_id, activation.get("created_at"))
        return len(uses) >= EFFECTIVE_IMPROVEMENT_MIN_USES

    def _learning_cost_share(self, *, run_ids: set[str], restrict: bool) -> dict:
        job_types = {row["id"]: row["type"] for row in self.store.query("SELECT id, type FROM job")}
        categories = ("learning", "proposal", "evaluation", "run", "other")
        by_category = {
            name: {"tokens": 0, "model_calls": 0, "entries": 0} for name in categories
        }
        unknown_ledger = 0
        for row in self.store.query("SELECT * FROM budget_ledger"):
            scope_id = row["scope_id"] or ""
            scope_kind = row["scope_kind"] or ""
            category = classify_cost_scope(scope_id, scope_kind, job_types)
            if restrict and category == "run" and scope_id not in run_ids:
                continue
            by_category[category]["tokens"] += int(row["used_tokens"] or 0)
            by_category[category]["model_calls"] += int(row["used_calls"] or 0)
            by_category[category]["entries"] += 1
            unknown_ledger += int(row["unknown_usage"] or 0)

        total_tokens = sum(v["tokens"] for v in by_category.values())
        total_calls = sum(v["model_calls"] for v in by_category.values())
        learning_tokens = sum(
            by_category[name]["tokens"] for name in ("learning", "proposal", "evaluation")
        )
        learning_calls = sum(
            by_category[name]["model_calls"] for name in ("learning", "proposal", "evaluation")
        )

        daily_unknown = int(self.store.scalar("SELECT COALESCE(SUM(unknown_usage),0) FROM daily_usage", default=0) or 0)
        runs_without_usage = 0
        for row in self.store.query("SELECT id, usage FROM run"):
            if restrict and row["id"] not in run_ids:
                continue
            payload = row["usage"]
            if not payload or payload in ("{}", "null"):
                runs_without_usage += 1
        known_events = total_calls
        unknown_events = unknown_ledger + daily_unknown + runs_without_usage
        coverage = None
        if known_events + unknown_events > 0:
            coverage = unknown_events / (known_events + unknown_events)
        return {
            "metric": "learning_cost_share",
            "description": "学习、候选与评测的已知费用 / 总已知费用（本期以 token 与调用数为单位，不承诺精确金额）",
            "value": _ratio(learning_tokens, total_tokens),
            "numerator": learning_tokens,
            "denominator": total_tokens,
            "numerator_tokens": learning_tokens,
            "denominator_tokens": total_tokens,
            "numerator_calls": learning_calls,
            "denominator_calls": total_calls,
            "by_category": by_category,
            "unknown_cost_coverage": coverage,
            "unknown_usage": {
                "ledger_unknown": unknown_ledger,
                "daily_unknown": daily_unknown,
                "runs_without_usage": runs_without_usage,
                "unknown_events": unknown_events,
                "known_events": known_events,
            },
            "token_note": "未知费用覆盖率与 token 同时显示",
            "sample_size": total_calls,
            "required_sample": 1,
            "evidence_insufficient": total_tokens == 0 and total_calls == 0,
        }

    def _review_burden(self, proposals: list[dict]) -> dict:
        durations: list[int] = []
        missing = 0
        excluded_over = 0
        excluded_invalid = 0
        for proposal in proposals:
            raw = proposal.get("review_duration_ms")
            if raw is None:
                missing += 1
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                excluded_invalid += 1
                continue
            if value > REVIEW_DURATION_CAP_MS:
                excluded_over += 1
                continue
            if value <= 0:
                excluded_invalid += 1
                continue
            durations.append(value)
        median = statistics.median(durations) if durations else None
        return {
            "metric": "review_burden",
            "description": "每张建议或候选详情的有效查看时长中位数",
            "value": median,
            "median_ms": median,
            "samples": len(durations),
            "excluded_over_limit": excluded_over,
            "excluded_invalid": excluded_invalid,
            "excluded_missing": missing,
            "cap_ms": REVIEW_DURATION_CAP_MS,
            "sample_size": len(durations),
            "required_sample": MIN_SAMPLE,
            "evidence_insufficient": len(durations) < MIN_SAMPLE,
            "note": "排除后台挂页；最大一次连续计时 10 分钟",
        }


def _ratio(numerator: int, denominator: int) -> float | None:
    """分母为 0 时返回 ``None``，绝不外推。"""
    if not denominator:
        return None
    return numerator / denominator


def _rate(numerator: int, denominator: int, min_sample: int = MIN_SAMPLE) -> float | None:
    """比率类指标：样本量不足 ``min_sample`` 时返回 ``None``（只给分子 / 分母 / 样本量）。"""
    if denominator < min_sample:
        return None
    return numerator / denominator


def classify_rollback_reason(reason: str | None) -> str:
    """把回退原因分类：效果退化 / 操作错误 / 环境变化 / 其他。"""
    text = (reason or "").strip().lower()
    if not text:
        return "other"
    code = text.split()[0].strip(":：,，") if text.split() else text
    if code in ROLLBACK_REGRESSION:
        return "effect_regression"
    if code in ROLLBACK_OPERATION_ERROR:
        return "operation_error"
    if code in ROLLBACK_ENVIRONMENT:
        return "environment_change"
    if any(key in text for key in ("退化", "回归", "效果下降", "变差", "regression", "degrad")):
        return "effect_regression"
    if any(key in text for key in ("误操作", "误启用", "操作错误", "点错", "mistake", "misclick", "wrong activation")):
        return "operation_error"
    if any(key in text for key in ("环境", "environment", "依赖缺失", "容器不可用", "工具不可用")):
        return "environment_change"
    return "other"


def classify_cost_scope(scope_id: str, scope_kind: str, job_types: dict[str, str]) -> str:
    """把预算流水归类到学习 / 候选 / 评测 / 任务 / 其他。"""
    job_type = job_types.get(scope_id)
    if job_type:
        if job_type.startswith("learning"):
            return "learning"
        if job_type.startswith("proposal"):
            return "proposal"
        if job_type.startswith("eval"):
            return "evaluation"
        if job_type.startswith("run"):
            return "run"
        return "other"
    kind = (scope_kind or "").lower()
    if kind in ("learning", "proposal", "evaluation", "eval", "run"):
        return "evaluation" if kind == "eval" else kind
    if scope_id.startswith("run_"):
        return "run"
    return "other"
