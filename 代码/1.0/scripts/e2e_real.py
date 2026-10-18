#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skillforge.core.config import AppConfig  # noqa: E402

STEP_WIDTH = 78


def step(title: str) -> None:
    print("\n" + "=" * STEP_WIDTH)
    print(f"▶ {title}")
    print("=" * STEP_WIDTH)


def show(label: str, data) -> None:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > 1200:
        text = text[:1200] + " …（截断）"
    print(f"  {label}: {text}")


def wait_run(services, run_id: str, timeout: float = 900.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = services.get_run(run_id)["run"]
        if run["execution_status"] in {"completed", "failed", "cancelled", "budget_stopped", "interrupted"}:
            return run
        time.sleep(0.5)
    return services.get_run(run_id)["run"]


def wait_job(services, job_id: str, timeout: float = 3600.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = services.evals.get_job(job_id)
        job = job.get("job", job)
        if job.get("status") in {"completed", "failed", "cancelled", "budget_stopped"}:
            return job
        time.sleep(1.0)
    job = services.evals.get_job(job_id)
    return job.get("job", job)


def build_fixture_skill(root: Path) -> Path:
    package = root / "skill" / "evidence-summary"
    (package / "references").mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        "---\n"
        "name: evidence-summary\n"
        "description: 把调研笔记整理成一页结构化摘要，每条结论必须标注来源。\n"
        "---\n\n"
        "# 适用条件\n"
        "输入是包含若干条笔记的文本资料，需要输出一页结构化摘要时使用。\n\n"
        "# 步骤\n"
        "1. 先通读全部输入资料，列出事实点。\n"
        "2. 归纳成不超过 5 条结论，每条结论后面用（来源：文件名）标注出处。\n"
        "3. 最后给出「结论」小节与「未解决疑问」小节。\n\n"
        "# 验证方法\n"
        "- 输出包含「结论」小节。\n"
        "- 每条结论都带来源标注。\n",
        encoding="utf-8",
    )
    (package / "references" / "guide.md").write_text(
        "写作要求：\n- 结论必须可追溯\n- 不要编造资料里没有的事实\n", encoding="utf-8"
    )
    return package


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="真实模型端到端验证")
    parser.add_argument("--base-url", default=os.environ.get("SKILLFORGE_E2E_BASE_URL"))
    parser.add_argument("--model", default=os.environ.get("SKILLFORGE_E2E_MODEL"))
    parser.add_argument("--api-key", default=os.environ.get("SKILLFORGE_E2E_API_KEY"))
    parser.add_argument("--home", default=None, help="数据目录（默认临时目录）")
    parser.add_argument("--keep", action="store_true", help="保留数据目录，便于人工检查")
    parser.add_argument("--skip-eval", action="store_true", help="跳过配对评测（省 token）")
    args = parser.parse_args(argv)

    if not args.base_url or not args.model:
        parser.error("需要 --base-url 与 --model（或对应环境变量）")

    tmp = None
    if args.home:
        home = Path(args.home).expanduser()
    else:
        tmp = tempfile.TemporaryDirectory(prefix="skillforge-e2e-")
        home = Path(tmp.name)
    home.mkdir(parents=True, exist_ok=True)

    config = AppConfig.load(str(home))
    config.ensure_dirs()

    from skillforge.api.services import AppServices

    services = AppServices(config)
    summary: dict = {"home": str(home), "steps": {}}
    try:
        step("1/7 配置模型并检查连接（真实请求）")
        services.set_model_config(provider="openai_compatible", base_url=args.base_url,
                                  model=args.model, api_key=args.api_key or None)
        check = services.check_model()
        show("连接检查", check)
        summary["steps"]["model_check"] = check
        if not check.get("ok"):
            print("模型不可用，终止验证。")
            return 2

        step("2/7 创建工作区与输入资料")
        input_dir = home / "input"
        input_dir.mkdir(exist_ok=True)
        (input_dir / "notes.md").write_text(
            "# 调研笔记\n"
            "- 用户访谈 6 人，其中 5 人提到整理资料耗时最多。\n"
            "- 现有工具在跨会话复用工作方法上几乎没有支持。\n"
            "- 试点建议：先固定 3 个高频任务类型。\n",
            encoding="utf-8",
        )
        workspace = services.add_workspace(name="e2e", root_path=str(input_dir))["workspace"]
        show("workspace", workspace["id"])

        step("3/7 导入技能包（原目录只读，脚本不执行）")
        package = build_fixture_skill(home)
        imported = services.import_skill(source_path=str(package), scope="workspace",
                                         workspace_id=workspace["id"])
        show("import", {k: imported.get(k) for k in ("skill_id", "version_id", "created")})
        skill_id = imported["skill_id"]
        summary["steps"]["skill"] = {"skill_id": skill_id, "version_id": imported["version_id"]}

        step("4/7 真实执行一次任务")
        created = services.create_run(
            request="请把 input/notes.md 整理成一页结构化摘要，结论要标注来源。",
            workspace_id=workspace["id"],
            completion_criteria=["包含「结论」小节", "每条结论带来源标注"],
            skill_ids=[skill_id],
        )
        run_id = created.get("run_id") or created.get("id")
        show("run_id", run_id)
        services.start_workers()
        run = wait_run(services, run_id)
        services.stop_workers()
        detail = services.get_run(run_id)
        show("execution_status", run["execution_status"])
        show("outcome（验收独立）", run["outcome"])
        show("usage", run.get("usage"))
        show("产物", [a["path"] for a in detail.get("artifacts", [])])
        show("加载记录", [(l["file_path"], l["load_kind"]) for l in detail.get("loads", [])])
        summary["steps"]["run"] = {"run_id": run_id, "execution_status": run["execution_status"],
                                   "outcome": run["outcome"],
                                   "artifacts": [a["path"] for a in detail.get("artifacts", [])]}

        step("5/7 提交用户纠正（长期范围，可进入候选）")
        feedback = services.submit_feedback(
            run_id=run_id, type="correction",
            content="结论缺少来源引用，以后同类任务每条结论都必须标注来源。",
            scope="workspace", target_skill_id=skill_id,
        )
        show("feedback_id", feedback.get("feedback_id"))
        outcomes = services.submit_feedback(run_id=run_id, type="acceptance", verdict="partially_accepted",
                                            scope="task", target_skill_id=skill_id)
        show("验收反馈", outcomes.get("feedback_id"))

        step("6/7 运行后台学习：经验提炼 + 候选生成")
        services.lessons.enqueue_from_feedback(feedback.get("feedback_id"))
        services.workers.run_once(["learning.extract"])
        lessons = services.list_lessons(skill_id=skill_id)["lessons"]
        show("经验数", len(lessons))
        for lesson in lessons[:3]:
            show(f"lesson {lesson['id']}", f"{lesson['attribution']} | {lesson['rule_text'][:80]}")
        summary["steps"]["lessons"] = [{"id": l["id"], "attribution": l["attribution"],
                                        "rule_text": l["rule_text"]} for l in lessons]
        candidate_lessons = [l["id"] for l in lessons if l["attribution"] in ("skill_gap", "skill_conflict")]
        proposal = None
        if candidate_lessons:
            proposal = services.propose_change(skill_id=skill_id, lesson_ids=candidate_lessons)
            show("proposal", {k: proposal.get(k) for k in ("proposal_id", "status")})
            summary["steps"]["proposal"] = {k: proposal.get(k) for k in ("proposal_id", "status")}
        else:
            print("  没有可进入候选的经验（归因不是 skill_gap/skill_conflict），这是合规结果，不是失败。")

        if args.skip_eval or not proposal:
            step("7/7 跳过配对评测")
            summary["steps"]["evaluation"] = {"skipped": True}
        else:
            step("7/7 冻结评测集并做真实配对评测")
            cases = [
                {"name": "样例1", "prompt": "整理 input/notes.md 为一页摘要。", "role": "regression",
                 "completion_criteria": [{"name": "含结论小节", "type": "contains", "value": "结论", "critical": True}],
                 "verifier": {"checks": [{"name": "含结论小节", "type": "contains", "value": "结论", "critical": True},
                                         {"name": "含来源标注", "type": "contains", "value": "来源"}]}},
                {"name": "样例2", "prompt": "把资料整理成结构化摘要，标注出处。", "role": "regression",
                 "completion_criteria": [{"name": "含来源", "type": "contains", "value": "来源", "critical": True}],
                 "verifier": {"checks": [{"name": "含来源", "type": "contains", "value": "来源", "critical": True}]}},
            ]
            suite = services.create_suite(skill_id=skill_id, name="e2e 摘要回归集", cases=cases,
                                          rubric={"dimensions": [{"name": "结构", "weight": 60}, {"name": "可追溯", "weight": 40}],
                                          "pass_threshold": 70},
                                          critical_checks=[{"name": "含结论小节", "type": "contains", "value": "结论"}],
                                          thresholds={}, freeze=True)
            suite_data = suite.get("suite", {})
            suite_data = suite_data.get("suite", suite_data)
            suite_id = suite_data.get("id") or suite_data.get("suite_id")
            show("suite", {"id": suite_id, "status": suite_data.get("status"),
                           "hash": str(suite_data.get("suite_hash"))[:12]})
            started = services.start_evaluation(proposal.get("proposal_id"), suite_id=suite_id)
            job_id = started.get("job_id") or started.get("id")
            services.start_workers()
            job = wait_job(services, job_id)
            services.stop_workers()
            report = services.get_eval_job(job_id)["report"]
            show("job status", job.get("status"))
            show("verdict", job.get("verdict"))
            show("report", {k: report.get(k) for k in ("verdict", "independent_cases", "holdout_cases",
                                                       "complete_pairs", "planned_pairs") if k in report})
            summary["steps"]["evaluation"] = {"job_id": job_id, "status": job.get("status"),
                                              "verdict": job.get("verdict")}

        print("\n" + "=" * STEP_WIDTH)
        print("端到端验证结束。数据目录：" + str(home))
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        print("=" * STEP_WIDTH)
        return 0
    finally:
        services.close()
        if tmp is not None and not args.keep:
            tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
