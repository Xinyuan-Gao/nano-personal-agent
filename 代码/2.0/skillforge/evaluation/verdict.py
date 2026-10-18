from __future__ import annotations

import statistics

#: 冻结前的默认阈值（判定表 +  覆盖要求）
DEFAULT_THRESHOLDS: dict = {
    # --- failed 条件 ---
    "quality_drop_fail": 5.0,
    # --- passed：质量改进 ---
    "pass_rate_gain_pp": 10.0,
    "quality_gain_pass": 5.0,
    "min_improved_cases": 3,
    "overall_quality_gain": 5.0,
    "token_ratio_max": 1.2,
    # --- passed：效率改进 ---
    "efficiency_metric": "tokens",
    "efficiency_drop_pct": 20.0,
    "efficiency_quality_tolerance": 2.0,
    # --- 覆盖门槛（首期默认 5 样例、≥2 holdout、每例 3 次）---
    "min_cases": 5,
    "min_holdout": 2,
    "repetitions": 3,
    # --- 冻结的资源对比阈值（可选）---
    "max_candidate_tokens": None,
    "max_candidate_tool_actions": None,
    "max_candidate_wall_s": None,
    # --- 质量模式要求 usage 可靠；不可靠时结论为 inconclusive ---
    "require_usage_known": True,
}

#: 结论边界声明：任何报告/结论都必须带上它
SCOPE_NOTE = (
    "本结论只表示在该冻结评测集（固定样例、固定 rubric 与阈值）内符合试点门禁；"
    "5 个样例规模不构成统计显著，也不能推断普遍提升。"
)

#: 结论中文标签
VERDICT_LABELS = {
    "passed": "通过（符合该冻结评测集门禁）",
    "failed": "失败（已证实退化或硬检查不通过）",
    "inconclusive": "证据不足（不下通过结论）",
}

#: 原因码说明
REASON_TEXT = {
    "hard_check_failed": "静态/权限硬检查不通过",
    "critical_regression": "同一配对中基线通过的 critical 检查在候选中失败",
    "quality_regression": "完整样例平均质量下降超过冻结阈值",
    "resource_threshold_exceeded": "候选资源用量超过冻结的对比阈值",
    "insufficient_samples": "独立样例或 holdout 数量不足",
    "missing_pairs": "存在未完成的配对：任一缺失配对不得 passed",
    "infrastructure_failure": "存在基础设施失败：结果不可用于通过结论",
    "undetermined_pairs": "存在未判定配对：不计成功并单列",
    "cancelled": "评测作业被取消：证据不完整",
    "budget_exceeded": "预算耗尽提前停止：证据不完整",
    "environment_mismatch": "两组环境不可比（隔离/工具权限/输入不一致）",
    "model_drift": "检测到服务端模型版本漂移：结论不可比",
    "usage_unreliable": "provider 未能提供可靠 usage/token 计数：质量模式结论为 inconclusive",
    "judge_disagreement": "评审分歧未解决：不下通过结论",
    "pass_rate_regression": "存在样例通过率下降",
    "gain_below_threshold": "总通过率与质量提升均未达到冻结门槛",
    "token_ratio_exceeded": "候选平均已知 token 超过基线 1.2 倍",
    "efficiency_gain_below_threshold": "预选资源指标中位数降低未达冻结门槛",
    "efficiency_quality_drop": "效率模式中单个样例平均质量下降超过允许值",
    "metric_unavailable": "预选资源指标不可得，无法支持效率结论",
    "no_measured_pairs": "没有任何可用的完整配对",
}

VALID_RESULT_STATUSES = (
    "ok",
    "infra_error",
    "undetermined",
    "cancelled",
    "budget_stopped",
    "pending",
)


# --------------------------------------------------------------------- 归一化
def normalize_thresholds(thresholds) -> dict:
    """合并默认阈值并做基本类型校验；未知键原样保留（向后兼容）。"""
    merged = dict(DEFAULT_THRESHOLDS)
    for key, value in (thresholds or {}).items():
        merged[key] = value
    numeric = {
        "quality_drop_fail",
        "pass_rate_gain_pp",
        "quality_gain_pass",
        "overall_quality_gain",
        "token_ratio_max",
        "efficiency_drop_pct",
        "efficiency_quality_tolerance",
    }
    for key in numeric:
        if merged.get(key) is None:
            continue
        try:
            merged[key] = float(merged[key])
        except (TypeError, ValueError):
            from ..core.errors import InvalidInput

            raise InvalidInput(
                f"阈值 {key} 必须是数字", field_errors={f"thresholds.{key}": "invalid"}
            ) from None
    for key in ("min_cases", "min_holdout", "repetitions", "min_improved_cases"):
        if merged.get(key) is None:
            continue
        try:
            merged[key] = int(merged[key])
        except (TypeError, ValueError):
            from ..core.errors import InvalidInput

            raise InvalidInput(
                f"阈值 {key} 必须是整数", field_errors={f"thresholds.{key}": "invalid"}
            ) from None
        if merged[key] < 0:
            from ..core.errors import InvalidInput

            raise InvalidInput(
                f"阈值 {key} 不能为负", field_errors={f"thresholds.{key}": "invalid"}
            )
    metric = merged.get("efficiency_metric")
    if metric not in ("tokens", "tool_actions", "wall_s"):
        from ..core.errors import InvalidInput

        raise InvalidInput(
            "efficiency_metric 必须是 tokens/tool_actions/wall_s（执行前冻结）",
            field_errors={"thresholds.efficiency_metric": "unsupported"},
        )
    return merged


def normalize_result(raw: dict) -> dict:
    """把外部结果条目归一成判定表需要的结构。"""
    data = dict(raw or {})
    baseline = _side(data.get("baseline") or data.get("baseline_checks") or {})
    candidate = _side(data.get("candidate") or data.get("candidate_checks") or {})
    judge = data.get("judge") if isinstance(data.get("judge"), dict) else None
    status = str(data.get("result_status") or data.get("status") or "pending")
    if status not in VALID_RESULT_STATUSES:
        status = "undetermined"
    return {
        "case_id": data.get("case_id") or data.get("case") or "",
        "case_name": data.get("case_name") or data.get("name") or data.get("case_id") or "",
        "role": data.get("role") or "regression",
        "repetition": int(data.get("repetition") or 1),
        "result_status": status,
        "baseline": baseline,
        "candidate": candidate,
        "judge": judge,
        "attempt": int(data.get("attempt") or 1),
    }


def _side(raw: dict) -> dict:
    data = dict(raw or {})
    checks = data.get("checks")
    if isinstance(checks, list):
        check_map = {
            str(c.get("name")): {
                "status": c.get("status"),
                "critical": bool(c.get("critical")),
                "detail": c.get("detail"),
            }
            for c in checks
            if isinstance(c, dict)
        }
    elif isinstance(checks, dict):
        check_map = dict(checks)
    else:
        check_map = {}
    if check_map and "critical_failed" not in data:
        data["critical_failed"] = [
            name for name, entry in check_map.items()
            if isinstance(entry, dict) and entry.get("critical") and entry.get("status") != "passed"
        ]
        data["failed"] = [
            name for name, entry in check_map.items()
            if isinstance(entry, dict) and not entry.get("critical") and entry.get("status") == "failed"
        ]
    return {
        "passed": data.get("passed"),
        "score": _number(data.get("score")),
        "critical_failed": list(data.get("critical_failed") or []),
        "failed": list(data.get("failed") or []),
        "undetermined": list(data.get("undetermined") or []),
        "tokens": _int_or_none(data.get("tokens")),
        "tokens_known": bool(data.get("tokens_known", data.get("tokens") is not None)),
        "tool_actions": _int_or_none(data.get("tool_actions")),
        "wall_s": _number(data.get("wall_s")),
        "status": data.get("status"),
        "checks": check_map,
    }


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- 主函数
def compute_verdict(*, results, thresholds, coverage, objective, budget_exceeded) -> dict:
    """按判定表计算结论；纯函数，不产生副作用。"""
    th = normalize_thresholds(thresholds)
    cov = dict(coverage or {})
    rows = [normalize_result(r) for r in (results or [])]
    objective = str(objective or "quality").lower()
    if objective not in ("quality", "efficiency"):
        objective = "quality"
    budget_flag = _budget_flag(budget_exceeded)

    ok_rows = [r for r in rows if r["result_status"] == "ok"]
    infra_rows = [r for r in rows if r["result_status"] == "infra_error"]
    undetermined_rows = [r for r in rows if r["result_status"] == "undetermined"]
    cancelled_rows = [r for r in rows if r["result_status"] == "cancelled"]
    budget_rows = [r for r in rows if r["result_status"] == "budget_stopped"]
    pending_rows = [r for r in rows if r["result_status"] == "pending"]

    planned_pairs = int(cov.get("planned_pairs") or len(rows) or 0)
    missing_pairs = int(cov.get("missing_pairs") or 0) + len(pending_rows)
    complete_pairs = len(ok_rows)

    failed_conditions = []
    reasons: list[dict] = []

    # ---------------------------------------------------------- failed 条件
    if cov.get("hard_check_failed"):
        failed_conditions.append(
            {"code": "hard_check_failed", "detail": cov.get("hard_check_detail") or "硬检查不通过"}
        )

    critical_regressions = []
    for row in ok_rows:
        base_critical = set(row["baseline"]["critical_failed"])
        for name in row["candidate"]["critical_failed"]:
            baseline_status = _check_status(row, "baseline", name)
            candidate_status = _check_status(row, "candidate", name)
            if baseline_status is None:
                # 没有检查明细时退化为「基线未失败、候选失败」
                is_regression = name not in base_critical
            else:
                is_regression = baseline_status == "passed" and candidate_status != "passed"
            if is_regression:
                critical_regressions.append(
                    {
                        "case_id": row["case_id"],
                        "case_name": row["case_name"],
                        "repetition": row["repetition"],
                        "check": name,
                        "baseline_status": baseline_status,
                        "candidate_status": candidate_status,
                    }
                )
    if critical_regressions:
        failed_conditions.append(
            {"code": "critical_regression", "count": len(critical_regressions),
             "items": critical_regressions[:20]}
        )

    baseline_scores = [r["baseline"]["score"] for r in ok_rows if r["baseline"]["score"] is not None]
    candidate_scores = [r["candidate"]["score"] for r in ok_rows if r["candidate"]["score"] is not None]
    mean_baseline = _mean(baseline_scores)
    mean_candidate = _mean(candidate_scores)
    quality_delta = (
        None if mean_baseline is None or mean_candidate is None else round(mean_candidate - mean_baseline, 4)
    )
    if quality_delta is not None and quality_delta < -abs(th["quality_drop_fail"]):
        failed_conditions.append(
            {"code": "quality_regression", "delta": quality_delta,
             "threshold": -abs(th["quality_drop_fail"]),
             "baseline_mean": mean_baseline, "candidate_mean": mean_candidate}
        )

    resource_breach = _resource_breach(ok_rows, th)
    if resource_breach:
        failed_conditions.append(resource_breach)

    # ---------------------------------------------------------- 覆盖/证据
    cases = int(cov.get("cases") if cov.get("cases") is not None else len({r["case_id"] for r in rows}))
    holdout = int(cov.get("holdout") or len({r["case_id"] for r in rows if r["role"] == "holdout"}))
    min_cases = int(th["min_cases"])
    min_holdout = int(th["min_holdout"])
    samples_sufficient = cases >= min_cases and holdout >= min_holdout

    evidence_issues = []
    if not rows:
        evidence_issues.append({"code": "no_measured_pairs", "message": REASON_TEXT["no_measured_pairs"]})
    if not samples_sufficient:
        evidence_issues.append(
            {
                "code": "insufficient_samples",
                "message": REASON_TEXT["insufficient_samples"],
                "detail": {"cases": cases, "min_cases": min_cases, "holdout": holdout,
                           "min_holdout": min_holdout},
            }
        )
    if missing_pairs:
        evidence_issues.append(
            {"code": "missing_pairs", "message": REASON_TEXT["missing_pairs"],
             "detail": {"missing_pairs": missing_pairs, "planned_pairs": planned_pairs}}
        )
    if infra_rows:
        evidence_issues.append(
            {"code": "infrastructure_failure", "message": REASON_TEXT["infrastructure_failure"],
             "detail": {"count": len(infra_rows),
                        "cases": sorted({r["case_name"] for r in infra_rows})}}
        )
    if undetermined_rows:
        evidence_issues.append(
            {"code": "undetermined_pairs", "message": REASON_TEXT["undetermined_pairs"],
             "detail": {"count": len(undetermined_rows),
                        "cases": sorted({r["case_name"] for r in undetermined_rows})}}
        )
    if cancelled_rows or cov.get("cancelled"):
        evidence_issues.append({"code": "cancelled", "message": REASON_TEXT["cancelled"]})
    if budget_flag or budget_rows:
        evidence_issues.append(
            {"code": "budget_exceeded", "message": REASON_TEXT["budget_exceeded"],
             "detail": {"exceeded_type": budget_flag or "budget", "pairs": len(budget_rows)}}
        )
    if cov.get("environment_mismatch"):
        evidence_issues.append(
            {"code": "environment_mismatch", "message": REASON_TEXT["environment_mismatch"],
             "detail": {"items": cov.get("environment_mismatch_detail") or []}}
        )
    if cov.get("model_drift"):
        evidence_issues.append(
            {"code": "model_drift", "message": REASON_TEXT["model_drift"],
             "detail": {"observed": cov.get("observed_models") or []}}
        )
    if not cov.get("isolated", True):
        evidence_issues.append(
            {"code": "environment_mismatch", "message": "两组初始环境未隔离，候选可能读到基线残留产物",
             "detail": {"isolation": cov.get("isolation") or {}}}
        )

    usage_reliable = bool(cov.get("usage_reliable", True))
    if objective == "quality" and th.get("require_usage_known", True) and not usage_reliable:
        evidence_issues.append(
            {"code": "usage_unreliable", "message": REASON_TEXT["usage_unreliable"]}
        )

    judged = int(cov.get("judged_pairs") or 0)
    disagreements = int(cov.get("judge_disagreements") or 0)
    if judged and disagreements * 2 > judged:
        evidence_issues.append(
            {"code": "judge_disagreement", "message": REASON_TEXT["judge_disagreement"],
             "detail": {"judged_pairs": judged, "disagreements": disagreements}}
        )

    evidence_sufficient = not evidence_issues
    candidate_comparable = not (
        cov.get("model_drift")
        or cov.get("environment_mismatch")
        or not cov.get("isolated", True)
    )

    # ---------------------------------------------------------- passed 条件
    per_case = _per_case_stats(rows, cov)
    total_gain_pp = round((per_case["candidate_rate"] - per_case["baseline_rate"]) * 100, 4)
    improved_cases = [c for c in per_case["cases"] if c["quality_delta"] is not None
                      and c["quality_delta"] >= th["quality_gain_pass"]]
    mode = None
    passed_detail: dict = {}

    if objective == "quality":
        if (
            evidence_sufficient
            and candidate_comparable
            and not failed_conditions
        ):
            regressed = [c for c in per_case["cases"] if c["rate_delta"] < 0]
            gain_ok = total_gain_pp >= th["pass_rate_gain_pp"]
            quality_ok = (
                len(improved_cases) >= int(th["min_improved_cases"])
                and quality_delta is not None
                and quality_delta >= th["overall_quality_gain"]
            )
            token_ok, token_detail = _token_ratio_ok(ok_rows, th)
            if regressed:
                reasons.append(
                    {"code": "pass_rate_regression", "message": REASON_TEXT["pass_rate_regression"],
                     "detail": {"cases": [c["case_name"] for c in regressed]}}
                )
            if not (gain_ok or quality_ok):
                reasons.append(
                    {"code": "gain_below_threshold", "message": REASON_TEXT["gain_below_threshold"],
                     "detail": {"total_gain_pp": total_gain_pp, "need_pp": th["pass_rate_gain_pp"],
                                "improved_cases": len(improved_cases),
                                "need_cases": th["min_improved_cases"],
                                "quality_delta": quality_delta,
                                "need_quality_delta": th["overall_quality_gain"]}}
                )
            if not token_ok:
                reasons.append(
                    {"code": "token_ratio_exceeded", "message": REASON_TEXT["token_ratio_exceeded"],
                     "detail": token_detail}
                )
            if not regressed and (gain_ok or quality_ok) and token_ok:
                mode = "quality"
                passed_detail = {
                    "gain_ok": gain_ok,
                    "quality_ok": quality_ok,
                    "total_gain_pp": total_gain_pp,
                    "improved_cases": [c["case_name"] for c in improved_cases],
                    "quality_delta": quality_delta,
                    "token": token_detail,
                }
            if not passed_detail:
                evidence_sufficient = False
        else:
            if not evidence_sufficient:
                reasons.extend(_as_reasons(evidence_issues))
    else:  # efficiency
        if evidence_sufficient and candidate_comparable and not failed_conditions:
            regressed = [c for c in per_case["cases"] if c["rate_delta"] < 0]
            quality_drops = [
                c for c in per_case["cases"]
                if c["quality_delta"] is not None
                and c["quality_delta"] < -abs(th["efficiency_quality_tolerance"])
            ]
            metric = th["efficiency_metric"]
            base_median, cand_median, drop_pct = _metric_median(ok_rows, metric)
            if regressed:
                reasons.append(
                    {"code": "pass_rate_regression", "message": REASON_TEXT["pass_rate_regression"],
                     "detail": {"cases": [c["case_name"] for c in regressed]}}
                )
            if quality_drops:
                reasons.append(
                    {"code": "efficiency_quality_drop",
                     "message": REASON_TEXT["efficiency_quality_drop"],
                     "detail": {"cases": [c["case_name"] for c in quality_drops],
                                "tolerance": th["efficiency_quality_tolerance"]}}
                )
            if drop_pct is None:
                reasons.append(
                    {"code": "metric_unavailable", "message": REASON_TEXT["metric_unavailable"],
                     "detail": {"metric": metric}}
                )
            elif drop_pct < th["efficiency_drop_pct"]:
                reasons.append(
                    {"code": "efficiency_gain_below_threshold",
                     "message": REASON_TEXT["efficiency_gain_below_threshold"],
                     "detail": {"metric": metric, "drop_pct": drop_pct,
                                "need_pct": th["efficiency_drop_pct"]}}
                )
            if not regressed and not quality_drops and drop_pct is not None \
                    and drop_pct >= th["efficiency_drop_pct"]:
                mode = "efficiency"
                passed_detail = {
                    "metric": metric,
                    "baseline_median": base_median,
                    "candidate_median": cand_median,
                    "drop_pct": drop_pct,
                }
            if not passed_detail:
                evidence_sufficient = False
        elif not evidence_sufficient:
            reasons.extend(_as_reasons(evidence_issues))

    # ---------------------------------------------------------- 结论优先级
    if failed_conditions:
        verdict = "failed"
    elif mode:
        verdict = "passed"
    else:
        verdict = "inconclusive"

    if verdict == "inconclusive" and not reasons:
        reasons = _as_reasons(evidence_issues) or [
            {"code": "gain_below_threshold", "message": REASON_TEXT["gain_below_threshold"]}
        ]
    if verdict == "failed":
        # 已证实的失败保留；同时说明证据范围（failed 与证据不足同时存在时的处理）
        reasons = _as_reasons(failed_conditions) + (
            [] if evidence_sufficient else _as_reasons(evidence_issues)
        )

    metrics = {
        "planned_pairs": planned_pairs,
        "complete_pairs": complete_pairs,
        "executions": int(cov.get("executions") or len(rows) * 2),
        "independent_cases": cases,
        "holdout_cases": holdout,
        "repetitions": int(cov.get("repetitions") or th.get("repetitions") or 0),
        "baseline_pass_rate": per_case["baseline_rate"],
        "candidate_pass_rate": per_case["candidate_rate"],
        "total_gain_pp": total_gain_pp,
        "baseline_mean_quality": mean_baseline,
        "candidate_mean_quality": mean_candidate,
        "quality_delta": quality_delta,
        "improved_cases": [c["case_name"] for c in improved_cases],
        "undetermined_pairs": len(undetermined_rows),
        "infra_error_pairs": len(infra_rows),
        "missing_pairs": missing_pairs,
        "budget_exceeded": budget_flag or bool(budget_rows),
        "unknown_usage": int(cov.get("unknown_usage") or 0),
    }

    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "objective": objective,
        "mode": mode,
        "reasons": [r["code"] for r in reasons],
        "reason_details": reasons,
        "failed_conditions": failed_conditions,
        "evidence_issues": evidence_issues,
        "evidence_sufficient": bool(evidence_sufficient and verdict != "failed"),
        "message": _message(verdict, mode, reasons),
        "scope_note": SCOPE_NOTE,
        "thresholds": th,
        "metrics": metrics,
        "per_case": per_case["cases"],
        "critical_regressions": critical_regressions,
        "passed_detail": passed_detail,
        "coverage": cov,
        "independent_cases": cases,
        "executions": metrics["executions"],
    }


# --------------------------------------------------------------------- 辅助
def _budget_flag(budget_exceeded) -> str | None:
    if not budget_exceeded:
        return None
    if isinstance(budget_exceeded, str):
        return budget_exceeded
    if isinstance(budget_exceeded, dict):
        return budget_exceeded.get("exceeded_type") or "budget"
    return "budget"


def _check_status(row: dict, side: str, name: str) -> str | None:
    checks = row[side].get("checks") if isinstance(row[side], dict) else None
    if isinstance(checks, dict):
        entry = checks.get(name)
        if isinstance(entry, dict):
            return entry.get("status")
        if isinstance(entry, str):
            return entry
    return None


def _as_reasons(items) -> list[dict]:
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"code": item, "message": REASON_TEXT.get(item, item)})
        elif isinstance(item, dict):
            code = item.get("code") or "unknown"
            out.append(
                {
                    "code": code,
                    "message": item.get("message") or REASON_TEXT.get(code, code),
                    "detail": item.get("detail"),
                }
            )
    return out


def _message(verdict: str, mode: str | None, reasons: list[dict]) -> str:
    if verdict == "passed":
        label = "质量改进" if mode == "quality" else "效率改进"
        return f"结论：passed（{label}）——仅在该冻结评测集内符合试点门禁。"
    if verdict == "failed":
        return "结论：failed —— " + "；".join(r.get("message", "") for r in reasons[:3])
    return "结论：inconclusive —— " + "；".join(r.get("message", "") for r in reasons[:3])


def _mean(values):
    items = [v for v in values if v is not None]
    if not items:
        return None
    return round(sum(items) / len(items), 4)


def _median(values):
    items = sorted(v for v in values if v is not None)
    if not items:
        return None
    return round(statistics.median(items), 4)


def _resource_breach(rows: list[dict], th: dict) -> dict | None:
    checks = (
        ("max_candidate_tokens", "tokens", "tokens"),
        ("max_candidate_tool_actions", "tool_actions", "tool_actions"),
        ("max_candidate_wall_s", "wall_s", "wall_s"),
    )
    for key, field, label in checks:
        limit = th.get(key)
        if limit is None:
            continue
        values = [r["candidate"].get(field) for r in rows if r["candidate"].get(field) is not None]
        if not values:
            continue
        observed = _mean(values)
        if observed is not None and observed > float(limit):
            return {
                "code": "resource_threshold_exceeded",
                "message": REASON_TEXT["resource_threshold_exceeded"],
                "detail": {"metric": label, "observed_mean": observed, "limit": float(limit)},
            }
    return None


def _token_ratio_ok(rows: list[dict], th: dict) -> tuple[bool, dict]:
    base = [r["baseline"]["tokens"] for r in rows
            if r["baseline"]["tokens"] is not None and r["baseline"]["tokens_known"]]
    cand = [r["candidate"]["tokens"] for r in rows
            if r["candidate"]["tokens"] is not None and r["candidate"]["tokens_known"]]
    if not base or not cand:
        return False, {"reason": "tokens_unknown", "baseline_samples": len(base),
                       "candidate_samples": len(cand)}
    mean_base = _mean(base) or 0.0
    mean_cand = _mean(cand) or 0.0
    ratio = None if mean_base <= 0 else round(mean_cand / mean_base, 4)
    limit = float(th["token_ratio_max"])
    if mean_base <= 0:
        ok = mean_cand <= 0
    else:
        ok = mean_cand <= mean_base * limit
    return ok, {"baseline_mean_tokens": mean_base, "candidate_mean_tokens": mean_cand,
                "ratio": ratio, "limit": limit}


def _metric_median(rows: list[dict], metric: str) -> tuple[float | None, float | None, float | None]:
    base = [r["baseline"].get(metric) for r in rows if r["baseline"].get(metric) is not None]
    cand = [r["candidate"].get(metric) for r in rows if r["candidate"].get(metric) is not None]
    base_median = _median(base)
    cand_median = _median(cand)
    if base_median is None or cand_median is None:
        return base_median, cand_median, None
    if base_median <= 0:
        drop = 0.0 if cand_median <= 0 else None
    else:
        drop = round((base_median - cand_median) / base_median * 100, 4)
    return base_median, cand_median, drop


def _per_case_stats(rows: list[dict], cov: dict) -> dict:
    planned_by_case = dict(cov.get("per_case_planned") or {})
    order: list[str] = []
    for row in rows:
        if row["case_id"] not in order:
            order.append(row["case_id"])
    cases = []
    total_base_pass = 0
    total_cand_pass = 0
    total_planned = 0
    for case_id in order:
        case_rows = [r for r in rows if r["case_id"] == case_id]
        planned = int(
            planned_by_case.get(case_id)
            or len(case_rows)
            or cov.get("repetitions")
            or 1
        )
        base_pass = len([r for r in case_rows if r["baseline"]["passed"] is True])
        cand_pass = len([r for r in case_rows if r["candidate"]["passed"] is True])
        base_rate = round(base_pass / planned, 6) if planned else 0.0
        cand_rate = round(cand_pass / planned, 6) if planned else 0.0
        base_scores = [r["baseline"]["score"] for r in case_rows if r["baseline"]["score"] is not None]
        cand_scores = [r["candidate"]["score"] for r in case_rows if r["candidate"]["score"] is not None]
        base_mean = _mean(base_scores)
        cand_mean = _mean(cand_scores)
        cases.append(
            {
                "case_id": case_id,
                "case_name": case_rows[0]["case_name"] if case_rows else case_id,
                "role": case_rows[0]["role"] if case_rows else "regression",
                "planned": planned,
                "executed": len(case_rows),
                "baseline_passed": base_pass,
                "candidate_passed": cand_pass,
                "baseline_pass_rate": base_rate,
                "candidate_pass_rate": cand_rate,
                "rate_delta": round(cand_rate - base_rate, 6),
                "baseline_mean_quality": base_mean,
                "candidate_mean_quality": cand_mean,
                "quality_delta": (
                    None if base_mean is None or cand_mean is None
                    else round(cand_mean - base_mean, 4)
                ),
            }
        )
        total_base_pass += base_pass
        total_cand_pass += cand_pass
        total_planned += planned
    planned_total = total_planned or int(cov.get("planned_pairs") or len(rows) or 1)
    return {
        "cases": cases,
        "baseline_rate": round(total_base_pass / planned_total, 6) if planned_total else 0.0,
        "candidate_rate": round(total_cand_pass / planned_total, 6) if planned_total else 0.0,
        "planned_total": planned_total,
    }
