from __future__ import annotations

import json
from pathlib import Path

from .verdict import SCOPE_NOTE, VERDICT_LABELS

#: 报告版本，便于导出后长期定位
REPORT_VERSION = "eval-report/1.0"

#: 额度/成本披露文案
COST_UNKNOWN_TEXT = "未知"


def cost_view(*, usage: dict | None, pricing: dict | None, unknown_usage: int = 0) -> dict:
    """金额视图：没有 usage 或价格时显示「未知」，绝不显示 0 元。"""
    usage = dict(usage or {})
    pricing = dict(pricing or {})
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens") or usage.get("tokens")
    priced = any(
        pricing.get(key) is not None
        for key in ("prompt_per_1k", "completion_per_1k", "per_1k_tokens")
    )
    tokens_known = bool(usage.get("usage_known", usage.get("tokens_known", True)))
    priced_fields_available = bool(
        (pricing.get("per_1k_tokens") is not None and total_tokens)
        or (pricing.get("prompt_per_1k") is not None and prompt_tokens is not None)
        or (pricing.get("completion_per_1k") is not None and completion_tokens is not None)
    )
    if not priced or not tokens_known or not priced_fields_available:
        reason = []
        if not priced:
            reason.append("未配置价格")
        if not tokens_known:
            reason.append("provider 未返回可靠 usage")
        if priced and not priced_fields_available:
            reason.append("缺少可计费的 token 字段")
        return {
            "currency": pricing.get("currency"),
            "amount": None,
            "display": COST_UNKNOWN_TEXT,
            "pricing_known": bool(priced),
            "usage_known": tokens_known,
            "unknown_usage": int(unknown_usage or 0),
            "note": (
                "金额未知（" + "、".join(reason or ["缺少用量"]) + "）：不显示 0 元，"
                "也不以金额为依据判定通过。"
            ),
        }
    amount = 0.0
    if pricing.get("prompt_per_1k") is not None:
        amount += float(prompt_tokens or 0) / 1000.0 * float(pricing["prompt_per_1k"])
    if pricing.get("completion_per_1k") is not None:
        amount += float(completion_tokens or 0) / 1000.0 * float(pricing["completion_per_1k"])
    if pricing.get("per_1k_tokens") is not None:
        amount = float(total_tokens or 0) / 1000.0 * float(pricing["per_1k_tokens"])
    currency = pricing.get("currency") or "CNY"
    return {
        "currency": currency,
        "amount": round(amount, 6),
        "display": f"{round(amount, 6)} {currency}",
        "pricing_known": True,
        "usage_known": True,
        "unknown_usage": int(unknown_usage or 0),
        "note": "金额按 provider usage 与冻结价格计算，仅作成本参考，不参与门禁判定。",
    }


def build_report(
    *,
    job: dict,
    suite: dict,
    cases: list[dict],
    results: list[dict],
    verdict: dict,
    usage: dict,
    coverage: dict,
    pairing: dict | None = None,
    isolation: dict | None = None,
    disclosure: dict | None = None,
    model_versions: dict | None = None,
    cost: dict | None = None,
    timeline: dict | None = None,
    report_valid: bool = True,
    invalid_reasons: list | None = None,
    artifacts: dict | None = None,
) -> dict:
    """组装完整报告（纯函数；不做 IO，便于导出与审计）。"""
    coverage = dict(coverage or {})
    cases = list(cases or [])
    results = list(results or [])
    per_case = list(verdict.get("per_case") or [])
    ok_rows = [r for r in results if r.get("result_status") == "ok"]
    undetermined = [r for r in results if r.get("result_status") == "undetermined"]
    infra = [r for r in results if r.get("result_status") == "infra_error"]
    cancelled = [r for r in results if r.get("result_status") == "cancelled"]
    budget_rows = [r for r in results if r.get("result_status") == "budget_stopped"]
    pending = [r for r in results if r.get("result_status") == "pending"]

    independent_cases = int(coverage.get("cases") or len({r.get("case_id") for r in results}))
    holdout = int(coverage.get("holdout") or len([c for c in cases if c.get("role") == "holdout"]))
    repetitions = int(coverage.get("repetitions") or 0)
    planned_pairs = int(coverage.get("planned_pairs") or len(results))
    executions = int(coverage.get("executions") or coverage.get("executions_planned") or planned_pairs * 2)
    executions_planned = int(coverage.get("planned_executions") or planned_pairs * 2)

    report = {
        "report_version": REPORT_VERSION,
        "generated_at": job.get("finished_at") or job.get("updated_at"),
        "report_valid": bool(report_valid),
        "invalid_reasons": list(invalid_reasons or []),
        "header": {
            "job_id": job.get("id"),
            "proposal_id": job.get("proposal_id"),
            "skill_id": suite.get("skill_id"),
            "suite_id": job.get("suite_id"),
            "suite_name": suite.get("name"),
            "suite_revision": suite.get("revision"),
            "suite_hash": suite.get("suite_hash"),
            "objective": suite.get("objective") or job.get("objective"),
            "config_hash": job.get("config_hash"),
            "baseline_version_id": job.get("baseline_version_id"),
            "candidate_version_id": job.get("candidate_version_id"),
            "status": job.get("status"),
            "verdict": verdict.get("verdict"),
            "verdict_label": VERDICT_LABELS.get(verdict.get("verdict") or "", ""),
            "attempt": job.get("attempts"),
        },
        "conclusion": {
            "verdict": verdict.get("verdict"),
            "label": verdict.get("verdict_label"),
            "mode": verdict.get("mode"),
            "objective": verdict.get("objective"),
            "message": verdict.get("message"),
            "reasons": verdict.get("reason_details") or [],
            "failed_conditions": verdict.get("failed_conditions") or [],
            "evidence_sufficient": verdict.get("evidence_sufficient"),
            "scope_note": verdict.get("scope_note") or SCOPE_NOTE,
        },
        "coverage": {
            "independent_cases": independent_cases,
            "holdout_cases": holdout,
            "regression_cases": independent_cases - holdout,
            "repetitions_per_case": repetitions,
            "planned_pairs": planned_pairs,
            "complete_pairs": len(ok_rows),
            "executions": executions,
            "executions_planned": executions_planned,
            "undetermined_pairs": len(undetermined),
            "infra_error_pairs": len(infra),
            "cancelled_pairs": len(cancelled),
            "budget_stopped_pairs": len(budget_rows),
            "pending_pairs": len(pending),
            "missing_pairs": int(coverage.get("missing_pairs") or 0),
            "complete": bool(coverage.get("complete")),
            "usage_reliable": bool(coverage.get("usage_reliable", True)),
            "unknown_usage": int(coverage.get("unknown_usage") or 0),
            "isolated": bool(coverage.get("isolated", True)),
            "model_drift": bool(coverage.get("model_drift")),
            "budget_exceeded": coverage.get("budget_exceeded"),
            "resumed": bool(coverage.get("resumed")),
            "filled_pairs": coverage.get("filled_pairs"),
            "elapsed_s": coverage.get("elapsed_s"),
            "case_roles": {c.get("id"): c.get("role") for c in cases},
            "statement": (
                f"独立样例 {independent_cases} 个（holdout {holdout} 个），每例重复 {repetitions} 次；"
                f"配对 {planned_pairs} 个、执行 {executions} 次。{repetitions} 次重复不代表"
                f" {planned_pairs} 个独立任务。"
            ),
        },
        "success_rates": {
            "overall": {
                "baseline_pass_rate": verdict.get("metrics", {}).get("baseline_pass_rate"),
                "candidate_pass_rate": verdict.get("metrics", {}).get("candidate_pass_rate"),
                "delta_pp": verdict.get("metrics", {}).get("total_gain_pp"),
                "denominator": "计划执行次数（未判定不计成功）",
            },
            "per_case": per_case,
        },
        "results": [_result_row(r, cases) for r in results],
        "critical_regressions": verdict.get("critical_regressions") or [],
        "quality_difference": {
            "baseline_mean": verdict.get("metrics", {}).get("baseline_mean_quality"),
            "candidate_mean": verdict.get("metrics", {}).get("candidate_mean_quality"),
            "delta": verdict.get("metrics", {}).get("quality_delta"),
            "improved_cases": verdict.get("metrics", {}).get("improved_cases") or [],
            "per_case": [
                {
                    "case_id": c.get("case_id"),
                    "case_name": c.get("case_name"),
                    "role": c.get("role"),
                    "baseline_mean_quality": c.get("baseline_mean_quality"),
                    "candidate_mean_quality": c.get("candidate_mean_quality"),
                    "quality_delta": c.get("quality_delta"),
                }
                for c in per_case
            ],
            "rubric": suite.get("rubric"),
            "thresholds": verdict.get("thresholds"),
        },
        "failures": {
            "undetermined": _failure_list(undetermined),
            "infrastructure": _failure_list(infra),
            "cancelled": _failure_list(cancelled),
            "budget_stopped": _failure_list(budget_rows),
            "pending": _failure_list(pending),
            "note": (
                "基础设施失败、未判定、取消与预算耗尽分别列出，不静默丢弃、不反复挑选最好一次。"
            ),
        },
        "time_and_cost": {
            "elapsed_s": (timeline or {}).get("elapsed_s"),
            "started_at": (timeline or {}).get("started_at"),
            "finished_at": (timeline or {}).get("finished_at"),
            "model_calls": (usage or {}).get("model_calls"),
            "tokens": (usage or {}).get("tokens"),
            "unknown_usage": (usage or {}).get("unknown_usage", 0),
            "per_group": (usage or {}).get("per_group"),
            "cost": cost or cost_view(usage=usage, pricing=None, unknown_usage=(usage or {}).get("unknown_usage", 0)),
        },
        "model_versions": model_versions or {},
        "pairing": pairing or {},
        "isolation": isolation or {},
        "disclosures": {
            "judge": disclosure or {},
            "notes": [
                "模型评审不能替代客观 verifier，也不构成独立事实验证。",
                "最终 verdict 由程序按冻结规则计算，模型输出不能直接决定。",
            ],
        },
        "artifacts": artifacts or {},
        "evidence_boundary": _evidence_boundary(verdict, coverage),
    }
    return report


def _result_row(row: dict, cases: list[dict]) -> dict:
    baseline = dict(row.get("baseline") or {})
    candidate = dict(row.get("candidate") or {})
    return {
        "case_id": row.get("case_id"),
        "case_name": row.get("case_name"),
        "role": row.get("role"),
        "repetition": row.get("repetition"),
        "attempt": row.get("attempt"),
        "attempt_history": row.get("attempt_history") or [],
        "order": row.get("order"),
        "pin": row.get("pin") or {},
        "result_status": row.get("result_status"),
        "baseline_run_id": row.get("baseline_run_id"),
        "candidate_run_id": row.get("candidate_run_id"),
        "baseline": _side_view(baseline),
        "candidate": _side_view(candidate),
        "judge": row.get("judge") or {},
        "notes": row.get("notes"),
        "filled_by_resume": bool(row.get("filled_by_resume")),
    }


def _side_view(side: dict) -> dict:
    checks = side.get("checks") or []
    if isinstance(checks, dict):
        checks = [{"name": k, **(v if isinstance(v, dict) else {"status": v})} for k, v in checks.items()]
    return {
        "passed": side.get("passed"),
        "score": side.get("score"),
        "status": side.get("status"),
        "critical_failed": side.get("critical_failed") or [],
        "failed": side.get("failed") or [],
        "undetermined": side.get("undetermined") or [],
        "checks": [
            {
                "name": c.get("name"),
                "status": c.get("status"),
                "critical": bool(c.get("critical")),
                "detail": c.get("detail"),
            }
            for c in checks
            if isinstance(c, dict)
        ],
        "tokens": side.get("tokens"),
        "tokens_known": side.get("tokens_known"),
        "tool_actions": side.get("tool_actions"),
        "wall_s": side.get("wall_s"),
        "model_ids": side.get("model_ids") or [],
        "artifacts": side.get("artifacts") or [],
    }


def _failure_list(rows: list[dict]) -> list[dict]:
    return [
        {
            "case_id": r.get("case_id"),
            "case_name": r.get("case_name"),
            "role": r.get("role"),
            "repetition": r.get("repetition"),
            "attempt": r.get("attempt"),
            "error_code": r.get("error_code"),
            "error_message": r.get("error_message"),
            "baseline_run_id": r.get("baseline_run_id"),
            "candidate_run_id": r.get("candidate_run_id"),
        }
        for r in rows
    ]


def _evidence_boundary(verdict: dict, coverage: dict) -> dict:
    insufficient = not verdict.get("evidence_sufficient", True)
    return {
        "evidence_sufficient": bool(verdict.get("evidence_sufficient")),
        "statement": (
            "已证实的失败被保留，其证据范围仅限完成配对的样例；未检出的结果不能推断为通过，"
            "基础设施错误不归因为 skill 缺陷。"
            if verdict.get("verdict") == "failed" and insufficient
            else "结论只覆盖本次冻结配置与已完成配对；缺失配对与未判定项不计成功。"
        ),
        "cannot_conclude_passed": bool(
            coverage.get("missing_pairs")
            or coverage.get("undetermined_pairs")
            or coverage.get("infra_error_pairs")
            or coverage.get("budget_stopped_pairs")
        ),
        "statistical_significance": False,
        "scope_note": SCOPE_NOTE,
    }


# --------------------------------------------------------------------- 导出
def to_markdown(report: dict) -> str:
    """把报告渲染成便于人工审阅的 Markdown（允许导出）。"""
    header = report.get("header") or {}
    conclusion = report.get("conclusion") or {}
    coverage = report.get("coverage") or {}
    rates = (report.get("success_rates") or {}).get("overall") or {}
    quality = report.get("quality_difference") or {}
    cost = (report.get("time_and_cost") or {}) or {}
    lines = [
        f"# 评测报告 {header.get('job_id')}",
        "",
        f"- 结论：**{conclusion.get('verdict')}**（{conclusion.get('label') or ''}）",
        f"- 评测集：{header.get('suite_name')} revision {header.get('suite_revision')}"
        f"（suite_hash {str(header.get('suite_hash'))[:12]}）",
        f"- 目标：{header.get('objective')}；基线版本 {header.get('baseline_version_id')}；"
        f"候选版本 {header.get('candidate_version_id')}",
        f"- 配置指纹：{str(header.get('config_hash'))[:16]}；报告有效：{report.get('report_valid')}",
        "",
        "## 覆盖范围",
        f"- {coverage.get('statement')}",
        f"- 完成配对 {coverage.get('complete_pairs')}/{coverage.get('planned_pairs')}；"
        f"未判定 {coverage.get('undetermined_pairs')}；基础设施失败 {coverage.get('infra_error_pairs')}；"
        f"取消 {coverage.get('cancelled_pairs')}；预算停止 {coverage.get('budget_stopped_pairs')}；"
        f"缺失 {coverage.get('missing_pairs')}",
        "",
        "## 两组成功率",
        f"- 基线 {_pct(rates.get('baseline_pass_rate'))} → 候选 {_pct(rates.get('candidate_pass_rate'))}"
        f"（{_pp(rates.get('delta_pp'))} 个百分点，分母：{rates.get('denominator')}）",
        "",
        "## 逐案结果",
        "| 样例 | role | 基线通过 | 候选通过 | 基线质量 | 候选质量 | 质量差 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in (report.get("success_rates") or {}).get("per_case") or []:
        lines.append(
            f"| {case.get('case_name')} | {case.get('role')} | "
            f"{case.get('baseline_passed')}/{case.get('planned')} | "
            f"{case.get('candidate_passed')}/{case.get('planned')} | "
            f"{_num(case.get('baseline_mean_quality'))} | {_num(case.get('candidate_mean_quality'))} | "
            f"{_num(case.get('quality_delta'))} |"
        )
    lines += [
        "",
        "## 质量差异",
        f"- 基线均值 {_num(quality.get('baseline_mean'))} → 候选均值 {_num(quality.get('candidate_mean'))}"
        f"（差 {_num(quality.get('delta'))}）",
        f"- 提高 ≥ 门槛的样例：{', '.join(quality.get('improved_cases') or []) or '无'}",
        "",
        "## 关键回归",
    ]
    regressions = report.get("critical_regressions") or []
    if regressions:
        for item in regressions:
            lines.append(f"- {item.get('case_name')} 第 {item.get('repetition')} 次：{item.get('check')}")
    else:
        lines.append("- 无（基线通过的 critical 检查在候选中均未失败）")
    failures = report.get("failures") or {}
    lines += [
        "",
        "## 未判定 / 基础设施失败 / 取消 / 预算耗尽（单列）",
        f"- 未判定 {len(failures.get('undetermined') or [])}；"
        f"基础设施失败 {len(failures.get('infrastructure') or [])}；"
        f"取消 {len(failures.get('cancelled') or [])}；"
        f"预算耗尽 {len(failures.get('budget_stopped') or [])}",
        "",
        "## 耗时与成本",
        f"- 耗时 {cost.get('elapsed_s')} 秒；模型调用 {cost.get('model_calls')}；"
        f"token {cost.get('tokens')}（未知 usage {cost.get('unknown_usage')}）",
        f"- 成本：{((cost.get('cost') or {}).get('display'))}（{((cost.get('cost') or {}).get('note'))}）",
        "",
        "## 模型版本披露",
        f"- 请求模型 {((report.get('model_versions') or {}).get('requested'))}",
        f"- 观察到的模型标识 {((report.get('model_versions') or {}).get('observed'))}",
        f"- 漂移：{((report.get('model_versions') or {}).get('drift'))}；"
        f"{((report.get('model_versions') or {}).get('reproducibility_note'))}",
        "",
        "## 结论边界",
        f"- {conclusion.get('scope_note')}",
        f"- {(report.get('evidence_boundary') or {}).get('statement')}",
        "",
        "## 披露",
    ]
    for note in (report.get("disclosures") or {}).get("notes") or []:
        lines.append(f"- {note}")
    judge = (report.get("disclosures") or {}).get("judge") or {}
    if judge:
        lines.append(f"- 裁判：{judge.get('judge_model')}；{judge.get('text')}")
    if report.get("invalid_reasons"):
        lines += ["", "## 报告失效原因"]
        for item in report["invalid_reasons"]:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def write_export(report: dict, dest: str | Path) -> dict:
    """导出报告：目录则写 ``*.json`` + ``*.md``，文件则按后缀写单一格式。"""
    path = Path(dest)
    written = []
    if path.suffix:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == ".md":
            path.write_text(to_markdown(report), encoding="utf-8")
        else:
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        written.append(str(path))
    else:
        path.mkdir(parents=True, exist_ok=True)
        job_id = (report.get("header") or {}).get("job_id") or "eval"
        json_path = path / f"{job_id}.json"
        md_path = path / f"{job_id}.md"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        md_path.write_text(to_markdown(report), encoding="utf-8")
        written += [str(json_path), str(md_path)]
    return {"written": written, "report_version": report.get("report_version")}


def _pct(value) -> str:
    return "未知" if value is None else f"{round(float(value) * 100, 2)}%"


def _pp(value) -> str:
    return "未知" if value is None else f"{round(float(value), 2)}"


def _num(value) -> str:
    return "—" if value is None else str(round(float(value), 2))
