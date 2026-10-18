from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from ..core.config import AppConfig
from ..core.errors import AppError, to_error_dict

TERMINAL_RUN_STATES = {"completed", "failed", "cancelled", "budget_stopped", "interrupted"}


# ---------------------------------------------------------------------- 输出
class Printer:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def out(self, data: Any, *, human: str | None = None) -> None:
        if self.as_json or human is None:
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        else:
            print(human)

    def fail(self, exc: Exception) -> int:
        payload = to_error_dict(exc)
        if self.as_json:
            print(json.dumps({"error": payload}, ensure_ascii=False, indent=2))
        else:
            print(f"错误[{payload['code']}]: {payload['message']}", file=sys.stderr)
            if payload.get("field_errors"):
                print(f"  字段: {payload['field_errors']}", file=sys.stderr)
            if payload.get("current_revision") is not None:
                print(f"  当前 revision: {payload['current_revision']}", file=sys.stderr)
        return 1


def _short(value: Any, limit: int = 60) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "（无记录）"
    widths = [len(h) for h in headers]
    cells = [[_short(c, 48) for c in row] for row in rows]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in cells:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


# ---------------------------------------------------------------------- 参数
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillforge",
        description="Skills 自进化框架：执行、反馈、候选、对照评测、启用与回退（本机运行）",
    )
    parser.add_argument("--home", help="数据目录（默认 $SKILLFORGE_HOME 或 ~/.skillforge）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    # 允许 --home/--json 也写在子命令之后（两种位置都支持）
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS：子命令未提供时不覆盖主解析器已解析的值
    common.add_argument("--home", dest="home", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--json", dest="json", action="store_true", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    parser.set_defaults(home=None, json=False)

    sub.add_parser("init", help="初始化数据目录与数据库", parents=[common])
    sub.add_parser("doctor", help="首次启动可用性检查（模型 / 数据目录 / 容器 / 工具）", parents=[common])
    serve = sub.add_parser("serve", help="启动本地 Web 工作台（仅监听 loopback）", parents=[common])
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    # -------- 模型
    model = sub.add_parser("model", help="模型配置", parents=[common]).add_subparsers(dest="model_cmd", required=True)
    model_set = model.add_parser("set", help="保存模型配置")
    model_set.add_argument("--provider", default="openai_compatible")
    model_set.add_argument("--base-url", required=True)
    model_set.add_argument("--model", required=True, dest="model_name")
    model_set.add_argument("--api-key", default=None, help="只写入本机凭据存储，不进数据库/日志")
    model_set.add_argument("--temperature", type=float, default=0.2)
    model_set.add_argument("--max-output-tokens", type=int, default=2048)
    model_set.add_argument("--timeout", type=float, default=60.0)
    model.add_parser("show", help="查看模型配置（不含密钥）")
    model.add_parser("check", help="检查连接（发送最小测试请求）")

    # -------- 工作区
    ws = sub.add_parser("workspace", help="工作区", parents=[common]).add_subparsers(dest="ws_cmd", required=True)
    ws_add = ws.add_parser("add", help="添加工作区")
    ws_add.add_argument("--name", required=True)
    ws_add.add_argument("--root", required=True, help="可读输入目录")
    ws_add.add_argument("--artifacts", default=None, help="产物目录（默认数据目录下）")
    ws_add.add_argument("--tools", default="list_dir,read_text,search_text,write_artifact")
    ws_add.add_argument("--no-scripts", action="store_true", help="禁止脚本/命令执行")
    ws.add_parser("list", help="列出工作区")
    ws_remove = ws.add_parser("remove", help="移除工作区")
    ws_remove.add_argument("workspace_id")
    ws_check = ws.add_parser("check", help="检查目录访问权限")
    ws_check.add_argument("workspace_id")

    # -------- 技能
    skill = sub.add_parser("skill", help="技能库", parents=[common]).add_subparsers(dest="skill_cmd", required=True)
    skill_import = skill.add_parser("import", help="导入 SKILL.md 技能目录（原目录只读）")
    skill_import.add_argument("path")
    skill_import.add_argument("--scope", default="personal", choices=["personal", "workspace"])
    skill_import.add_argument("--workspace", default=None, dest="workspace_id")
    skill_import.add_argument("--on-conflict", default="reject",
                              choices=["reject", "new_version", "new_slug"])
    skill_list = skill.add_parser("list", help="列出技能")
    skill_list.add_argument("--query", default=None)
    skill_list.add_argument("--workspace", default=None, dest="workspace_id")
    skill_show = skill.add_parser("show", help="技能详情")
    skill_show.add_argument("skill_id")
    skill_versions = skill.add_parser("versions", help="版本列表")
    skill_versions.add_argument("skill_id")
    skill_archive = skill.add_parser("archive", help="归档技能")
    skill_archive.add_argument("skill_id")
    skill_restore = skill.add_parser("restore", help="恢复归档技能")
    skill_restore.add_argument("skill_id")
    skill_export = skill.add_parser("export", help="导出完整包")
    skill_export.add_argument("skill_id")
    skill_export.add_argument("--dest", default=None)
    skill_rollback = skill.add_parser("rollback", help="回退到历史完整包")
    skill_rollback.add_argument("skill_id")
    skill_rollback.add_argument("--version", required=True, dest="version_id")
    skill_rollback.add_argument("--expected-revision", type=int, required=True)
    trial = skill.add_parser("manual-trial", help="手工试用某版本（标记效果未验证）")
    trial.add_argument("skill_id")
    trial.add_argument("--version", required=True, dest="version_id")
    trial.add_argument("--expected-revision", type=int, required=True)
    trial.add_argument("--ack", action="store_true", help="确认“效果未验证”")

    # -------- 任务
    task = sub.add_parser("task", help="任务", parents=[common]).add_subparsers(dest="task_cmd", required=True)
    task_run = task.add_parser("run", help="发起任务并等待执行")
    task_run.add_argument("--request", required=True)
    task_run.add_argument("--workspace", default=None, dest="workspace_id")
    task_run.add_argument("--criteria", action="append", default=[], help="完成条件，可重复")
    task_run.add_argument("--skill", action="append", default=[], dest="skill_ids")
    task_run.add_argument("--budget-calls", type=int, default=None)
    task_run.add_argument("--no-learning", action="store_true", help="本次关闭后台学习")
    task_run.add_argument("--no-wait", action="store_true", help="只提交，不等待执行")
    task_run.add_argument("--timeout", type=float, default=600.0)
    task_list = task.add_parser("list", help="任务列表")
    task_list.add_argument("--status", default=None)
    task_list.add_argument("--workspace", default=None, dest="workspace_id")
    task_list.add_argument("--limit", type=int, default=20)
    task_show = task.add_parser("show", help="任务详情（含产物、加载记录与工具动作）")
    task_show.add_argument("run_id")
    task_cancel = task.add_parser("cancel", help="取消任务")
    task_cancel.add_argument("run_id")
    task_resume = task.add_parser("resume", help="中断恢复")
    task_resume.add_argument("run_id")
    fb = task.add_parser("feedback", help="提交验收/纠正")
    fb.add_argument("run_id")
    fb.add_argument("--verdict", default=None, choices=["accepted", "partially_accepted", "rejected"])
    fb.add_argument("--content", default=None, help="文字纠正原话")
    fb.add_argument("--scope", default="task", choices=["task", "workspace", "personal"])
    fb.add_argument("--type", default=None)
    fb.add_argument("--no-learn", action="store_true", help="只保存反馈，不立即执行后台学习")

    # -------- 经验与候选
    lesson = sub.add_parser("lesson", help="经验", parents=[common]).add_subparsers(dest="lesson_cmd", required=True)
    lesson_list = lesson.add_parser("list", help="列出经验")
    lesson_list.add_argument("--skill", default=None, dest="skill_id")
    lesson_list.add_argument("--status", default=None)
    lesson_ignore = lesson.add_parser("ignore", help="忽略一条经验")
    lesson_ignore.add_argument("lesson_id")
    lesson_ignore.add_argument("--reason", required=True,
                               choices=["only_this_time", "wrong_attribution", "wrong_rule",
                                        "duplicate", "not_worth_cost"])
    lesson.add_parser("pause", help="暂停后台学习")
    lesson.add_parser("resume", help="恢复后台学习")

    propose = sub.add_parser("propose", help="基于经验生成候选修改", parents=[common])
    propose.add_argument("--skill", required=True, dest="skill_id")
    propose.add_argument("--baseline", default=None, dest="baseline_version_id")
    propose.add_argument("--lesson", action="append", required=True, dest="lesson_ids")
    propose.add_argument("--scope", default=None)

    proposal = sub.add_parser("proposal", help="候选", parents=[common]).add_subparsers(dest="proposal_cmd", required=True)
    proposal_list = proposal.add_parser("list", help="列出候选")
    proposal_list.add_argument("--skill", default=None, dest="skill_id")
    proposal_list.add_argument("--status", default=None)
    proposal_show = proposal.add_parser("show", help="候选详情")
    proposal_show.add_argument("proposal_id")
    proposal_diff = proposal.add_parser("diff", help="逐文件差异")
    proposal_diff.add_argument("proposal_id")
    proposal_check = proposal.add_parser("check", help="重新运行静态检查")
    proposal_check.add_argument("proposal_id")
    proposal_reject = proposal.add_parser("reject", help="拒绝候选")
    proposal_reject.add_argument("proposal_id")
    proposal_reject.add_argument("--reason", required=True,
                                 choices=["only_this_time", "wrong_attribution", "wrong_rule",
                                          "duplicate", "not_worth_cost"])
    proposal_activate = proposal.add_parser("activate", help="启用候选（需 eligible 且报告匹配）")
    proposal_activate.add_argument("proposal_id")
    proposal_activate.add_argument("--expected-revision", type=int, required=True)
    proposal_activate.add_argument("--report", default=None, dest="report_id")

    # -------- 评测
    ev = sub.add_parser("eval", help="评测中心", parents=[common]).add_subparsers(dest="eval_cmd", required=True)
    suite_create = ev.add_parser("suite-create", help="创建评测集（可立即冻结）")
    suite_create.add_argument("--skill", required=True, dest="skill_id")
    suite_create.add_argument("--name", required=True)
    suite_create.add_argument("--category", default="")
    suite_create.add_argument("--cases", required=True, help="cases JSON 文件路径或内联 JSON")
    suite_create.add_argument("--rubric", default="{}", help="rubric JSON 文件或内联")
    suite_create.add_argument("--critical", default="[]", help="关键检查 JSON 文件或内联")
    suite_create.add_argument("--thresholds", default="{}")
    suite_create.add_argument("--objective", default="quality", choices=["quality", "efficiency"])
    suite_create.add_argument("--no-freeze", action="store_true")
    suite_list = ev.add_parser("suite-list", help="列出评测集")
    suite_list.add_argument("--skill", default=None, dest="skill_id")
    suite_show = ev.add_parser("suite-show", help="评测集详情")
    suite_show.add_argument("suite_id")
    suite_freeze = ev.add_parser("suite-freeze", help="冻结评测集")
    suite_freeze.add_argument("suite_id")
    ev_start = ev.add_parser("start", help="启动配对评测")
    ev_start.add_argument("--proposal", required=True, dest="proposal_id")
    ev_start.add_argument("--suite", default=None, dest="suite_id")
    ev_start.add_argument("--wait", action="store_true", help="等待作业结束")
    ev_start.add_argument("--timeout", type=float, default=1800.0)
    ev_jobs = ev.add_parser("jobs", help="评测作业列表")
    ev_jobs.add_argument("--proposal", default=None, dest="proposal_id")
    ev_report = ev.add_parser("report", help="配对评测报告")
    ev_report.add_argument("job_id")
    ev_cancel = ev.add_parser("cancel", help="取消评测作业")
    ev_cancel.add_argument("job_id")
    ev_resume = ev.add_parser("resume", help="续跑未完成配对")
    ev_resume.add_argument("job_id")

    # -------- 作业
    jobs = sub.add_parser("jobs", help="后台作业", parents=[common]).add_subparsers(dest="jobs_cmd", required=True)
    jobs_list = jobs.add_parser("list", help="作业列表")
    jobs_list.add_argument("--status", default=None)
    jobs_cancel = jobs.add_parser("cancel", help="取消作业")
    jobs_cancel.add_argument("job_id")
    jobs_drain = jobs.add_parser("run-pending", help="同步执行待处理作业")
    jobs_drain.add_argument("--limit", type=int, default=20)

    # -------- 数据
    data = sub.add_parser("data", help="数据管理", parents=[common]).add_subparsers(dest="data_cmd", required=True)
    data_usage = data.add_parser("usage", help="本地用量统计")
    data_backup = data.add_parser("backup", help="备份（不含凭据）")
    data_backup.add_argument("--dest", default=None)
    data_backup.add_argument("--meta-only", action="store_true", help="不包含正文")
    data_restore = data.add_parser("restore", help="恢复备份")
    data_restore.add_argument("backup_path")
    data_restore.add_argument("--apply", action="store_true", help="真正应用（默认只做预览校验）")
    data_export = data.add_parser("export", help="导出包")
    data_export.add_argument("--dest", default=None)
    data_export.add_argument("--skill", default=None, dest="skill_id")
    data_clear = data.add_parser("clear", help="清除数据（需 --confirm）")
    data_clear.add_argument("--scope", default="{}", help="scope JSON")
    data_clear.add_argument("--confirm", action="store_true")
    data_preview = data.add_parser("preview-clear", help="预览将被清除的关联对象")
    data_preview.add_argument("--scope", default="{}")
    retention_show = data.add_parser("retention-show", help="查看正文保留策略")
    retention_set = data.add_parser("retention-set", help="设置正文保留期限（天，0 表示长期保留）")
    retention_set.add_argument("days", type=int)
    retention_preview = data.add_parser("retention-preview", help="预览到期清理的影响")
    retention_preview.add_argument("--days", type=int, default=None)
    retention_apply = data.add_parser("retention-apply", help="执行到期清理（清除正文，保留结构化证据）")
    retention_apply.add_argument("--days", type=int, default=None)
    retention_apply.add_argument("--confirm", action="store_true")
    retention_apply.add_argument("--purge-artifacts", action="store_true",
                                 help="同时清理产物制品（默认保留）")
    evidence = sub.add_parser("evidence", help="查看某个任务的证据可用状态", parents=[common])
    evidence.add_argument("run_id")

    sub.add_parser("metrics", help="核心指标看板", parents=[common])
    audit = sub.add_parser("audit", help="审计记录", parents=[common])
    audit.add_argument("--limit", type=int, default=50)
    return parser


# ---------------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    printer = Printer(args.json)
    config = AppConfig.load(args.home)
    if args.command == "init":
        config.ensure_dirs()
        from ..core.db import Store

        store = Store(config.db_path)
        store.migrate()
        store.close()
        printer.out({"home": str(config.home), "db_path": str(config.db_path), "status": "initialized"},
                    human=f"已初始化数据目录：{config.home}")
        return 0
    if args.command == "serve":
        return _serve(args, config, printer)

    from .services_factory import build_services

    services = build_services(config)
    try:
        return _dispatch(args, services, printer)
    except AppError as exc:
        return printer.fail(exc)
    except KeyboardInterrupt:  # pragma: no cover
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转错误契约
        return printer.fail(exc)
    finally:
        services.close()


def _serve(args, config: AppConfig, printer: Printer) -> int:
    from ..api.app import serve as serve_http
    from .services_factory import build_services

    services = build_services(config)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    services.start_workers()
    try:
        def _ready(url, server):
            printer.out({"url": url, "home": str(config.home), "workers": "started"},
                        human=f"本地工作台已启动：{url}\n数据目录：{config.home}\n按 Ctrl+C 停止。")

        serve_http(services, config, on_ready=_ready)
    finally:
        services.close()
    return 0


def _dispatch(args, services, printer: Printer) -> int:
    cmd = args.command
    if cmd == "doctor":
        data = services.doctor()
        model = data.get("model") or {}
        container = data.get("container") or {}
        human = "\n".join([
            f"模型：{'已配置' if model.get('configured') else '未配置'} {model.get('model') or ''}".rstrip(),
            f"数据目录：{data.get('data_dir', {}).get('path', services.config.home)}",
            f"容器：{'可用' if container.get('available') else '不可用（脚本/命令功能被阻断，不降级宿主 shell）'}",
            f"可用工具：{', '.join(data.get('tools_available') or [])}",
            f"不可用工具：{', '.join(t.get('tool', '?') for t in (data.get('tools_unavailable') or [])) or '无'}",
        ])
        printer.out(data, human=human)
        return 0

    if cmd == "model":
        if args.model_cmd == "set":
            data = services.set_model_config(
                provider=args.provider, base_url=args.base_url, model=args.model_name,
                api_key=args.api_key, temperature=args.temperature,
                max_output_tokens=args.max_output_tokens, timeout_s=args.timeout,
            )
            printer.out(data, human=f"模型配置已保存：{args.model_name} @ {args.base_url}（密钥仅存本机凭据库）")
        elif args.model_cmd == "show":
            data = services.get_model_config()
            model = data["model"]
            printer.out(data, human=f"provider={model.get('provider')}\nbase_url={model.get('base_url')}\n"
                                    f"model={model.get('model')}\ntemperature={model.get('temperature')}\n"
                                    f"api_key_ref={model.get('api_key_ref')}")
        else:
            data = services.check_model()
            printer.out(data, human=("连接正常：" if data.get("ok") else "连接失败：")
                        + f"{data.get('model_id') or data.get('error')}")
        return 0

    if cmd == "workspace":
        if args.ws_cmd == "add":
            data = services.add_workspace(name=args.name, root_path=args.root,
                                          artifact_path=args.artifacts,
                                          allowed_tools=args.tools.split(","),
                                          allow_scripts=not args.no_scripts)
            ws = data["workspace"]
            printer.out(data, human=f"已添加工作区 {ws['id']}：{ws['name']}（输入 {ws['root_path']}）")
        elif args.ws_cmd == "list":
            data = services.list_workspaces()
            rows = [[w["id"], w["name"], w["root_path"], w["artifact_path"], w["status"]] for w in data["workspaces"]]
            printer.out(data, human=_table(["id", "名称", "输入目录", "产物目录", "状态"], rows))
        elif args.ws_cmd == "remove":
            data = services.remove_workspace(args.workspace_id)
            printer.out(data, human=f"已移除工作区 {args.workspace_id}（历史证据保留）")
        else:
            data = services.check_workspace(args.workspace_id)
            printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if cmd == "skill":
        return _skill(args, services, printer)
    if cmd == "task":
        return _task(args, services, printer)
    if cmd == "lesson":
        if args.lesson_cmd == "list":
            data = services.list_lessons(skill_id=args.skill_id, status=args.status)
            rows = [[l["id"], l["attribution"], l["scope"], l["status"], _short(l["rule_text"], 44)]
                    for l in data["lessons"]]
            printer.out(data, human=_table(["id", "归因", "范围", "状态", "规则"], rows))
        elif args.lesson_cmd == "ignore":
            data = services.ignore_lesson(args.lesson_id, reason_code=args.reason)
            printer.out(data, human=f"已忽略经验 {args.lesson_id}（原因 {args.reason}）")
        elif args.lesson_cmd == "pause":
            data = services.set_learning_settings(enabled=False)
            printer.out(data, human="已暂停后台学习（任务执行不受影响）")
        else:
            data = services.set_learning_settings(enabled=True)
            printer.out(data, human="已恢复后台学习")
        return 0

    if cmd == "propose":
        data = services.propose_change(skill_id=args.skill_id, lesson_ids=args.lesson_ids,
                                       baseline_version_id=args.baseline_version_id, scope=args.scope)
        proposal = data.get("proposal", data)
        printer.out(data, human=f"候选已生成：{proposal.get('proposal_id') or proposal.get('id')} "
                                f"状态={proposal.get('status')}")
        return 0

    if cmd == "proposal":
        return _proposal(args, services, printer)
    if cmd == "eval":
        return _eval(args, services, printer)
    if cmd == "jobs":
        if args.jobs_cmd == "list":
            data = services.list_jobs(status=args.status)
            rows = [[j["id"], j["type"], j["status"], j.get("attempts"), _short(j.get("error_message"), 30)]
                    for j in data["jobs"]]
            printer.out(data, human=_table(["id", "类型", "状态", "尝试", "错误"], rows))
        elif args.jobs_cmd == "cancel":
            data = services.cancel_job(args.job_id)
            printer.out(data, human=f"作业 {args.job_id} → {data['status']}")
        else:
            data = services.run_pending_jobs(limit=args.limit)
            printer.out(data, human=f"已同步执行 {data['executed']} 个作业")
        return 0

    if cmd == "data":
        return _data(args, services, printer)
    if cmd == "metrics":
        data = services.metrics()
        printer.out(data, human=json.dumps(data["metrics"], ensure_ascii=False, indent=2))
        return 0
    if cmd == "evidence":
        data = services.evidence_state(args.run_id)
        human = (f"任务 {args.run_id} 证据状态：{data['state']}（可用={data['available']}）\n"
                 f"{data['reason']}\ncontext_hash={data['context_hash']}")
        printer.out(data, human=human)
        return 0
    if cmd == "audit":
        data = services.list_audit(limit=args.limit)
        rows = [[a["created_at"], a["action"], a["entity_type"], a["entity_id"], a["actor"]] for a in data["audit"]]
        printer.out(data, human=_table(["时间", "动作", "对象类型", "对象", "操作者"], rows))
        return 0
    raise AppError(f"未实现的命令: {cmd}")


def _skill(args, services, printer: Printer) -> int:
    if args.skill_cmd == "import":
        data = services.import_skill(source_path=args.path, scope=args.scope,
                                     workspace_id=args.workspace_id, on_conflict=args.on_conflict)
        checks = data.get("checks") or {}
        printer.out(data, human=(
            f"已导入：skill={data.get('skill_id')} version={data.get('version_id')}\n"
            f"文件数={checks.get('file_count', '?')} 包 hash={_short(checks.get('package_hash'), 16)}\n"
            "原目录未被修改，包内脚本未执行。"
        ))
    elif args.skill_cmd == "list":
        data = services.list_skills(query=args.query, workspace_id=args.workspace_id)
        rows = [[s["id"], s["slug"], s["scope"], s["status"],
                 (s.get("current") or {}).get("version_id", "-"),
                 (s.get("current") or {}).get("revision", "-")] for s in data["skills"]]
        printer.out(data, human=_table(["id", "slug", "范围", "状态", "生效版本", "rev"], rows))
    elif args.skill_cmd == "show":
        data = services.get_skill(args.skill_id)
        skill = data["skill"]
        current = data["current"] or {}
        human = "\n".join([
            f"技能：{skill.get('name')} ({skill.get('slug')}) 范围={skill.get('scope')} 状态={skill.get('status')}",
            f"用途：{skill.get('description')}",
            f"生效版本：{current.get('version_id')} revision={current.get('revision')} "
            f"来源={current.get('origin')} 未验证={bool(current.get('unverified'))}",
            "文件清单：\n" + _table(["路径", "大小", "hash"],
                                    [[f["path"], f.get("size"), _short(f.get("content_hash"), 12)]
                                     for f in data.get("files", [])]),
            "启用历史：\n" + _table(["rev", "版本", "来源", "未验证", "时间"],
                                    [[a["revision"], _short(a["version_id"], 20), a["origin"],
                                      bool(a.get("unverified")), a.get("created_at")] for a in data.get("activations", [])]),
        ])
        printer.out(data, human=human)
    elif args.skill_cmd == "versions":
        data = services.list_versions(args.skill_id)
        rows = [[v["id"], v["origin"], v["validation_status"], v.get("file_count"),
                 _short(v.get("package_hash"), 12), v.get("created_at")] for v in data["versions"]]
        printer.out(data, human=_table(["版本", "来源", "校验", "文件数", "包hash", "时间"], rows))
    elif args.skill_cmd == "archive":
        data = services.archive_skill(args.skill_id)
        printer.out(data, human=f"已归档 {args.skill_id}（不参与新任务选择，历史保留）")
    elif args.skill_cmd == "restore":
        data = services.restore_skill(args.skill_id)
        printer.out(data, human=f"已恢复 {args.skill_id}")
    elif args.skill_cmd == "export":
        data = services.export_skill(args.skill_id, dest=args.dest)
        printer.out(data, human=f"已导出到：{data.get('dest') or data.get('path')}")
    elif args.skill_cmd == "rollback":
        data = services.rollback_skill(args.skill_id, target_version_id=args.version_id,
                                       expected_revision=args.expected_revision)
        printer.out(data, human=f"已回退到完整包 {args.version_id}（新 activation {data.get('activation_id')}）")
    elif args.skill_cmd == "manual-trial":
        if not args.ack:
            return printer.fail(AppError("手工试用必须显式确认效果未验证（--ack）"))
        data = services.activate_manual_trial(args.skill_id, source_version_id=args.version_id,
                                              unverified_ack=True, expected_revision=args.expected_revision)
        printer.out(data, human=f"已创建手工试用版本 {data.get('version_id')}（标记效果未验证，不计有效改进）")
    return 0


def _task(args, services, printer: Printer) -> int:
    if args.task_cmd == "run":
        budget = None
        if args.budget_calls:
            budget = {"max_model_calls": args.budget_calls}
        data = services.create_run(request=args.request, workspace_id=args.workspace_id,
                                   completion_criteria=args.criteria, skill_ids=args.skill_ids or None,
                                   budget=budget, learning_enabled=not args.no_learning)
        run_id = data.get("run_id") or data.get("id")
        if args.no_wait:
            printer.out(data, human=f"已提交任务：{run_id}（异步执行，可用 task show 查看）")
            return 0
        services.start_workers()
        deadline = time.time() + float(args.timeout)
        current = {}
        try:
            while time.time() < deadline:
                run = services.get_run(run_id)["run"]
                current = run
                if run.get("execution_status") in TERMINAL_RUN_STATES:
                    break
                time.sleep(0.2)
        finally:
            services.stop_workers()
        detail = services.get_run(run_id)
        artifacts = detail.get("artifacts", [])
        human = "\n".join([
            f"任务 {run_id}：执行状态={current.get('execution_status')} 验收状态={current.get('outcome')}",
            f"产物 {len(artifacts)} 个：" + ", ".join(a["path"] for a in artifacts[:8]),
            f"错误：{current.get('error_message') or '无'}（执行说明见 checkpoint.last_reason）",
            "提示：completed 不等于 accepted，请用 task feedback 提交验收或纠正。",
        ])
        printer.out({"run": current, "artifacts": artifacts}, human=human)
    elif args.task_cmd == "list":
        data = services.list_runs(status=args.status, workspace_id=args.workspace_id, limit=args.limit)
        rows = [[r["id"], r["execution_status"], r["outcome"], _short(r["request"], 40), r.get("created_at")]
                for r in data["runs"]]
        printer.out(data, human=_table(["run_id", "执行", "验收", "请求", "时间"], rows))
    elif args.task_cmd == "show":
        data = services.get_run(args.run_id)
        run = data["run"]
        human = "\n".join([
            f"任务 {run['id']}",
            f"请求：{run['request']}",
            f"执行状态={run['execution_status']} 验收状态={run['outcome']}",
            f"模型快照：{json.dumps(run.get('model_snapshot'), ensure_ascii=False)}",
            f"权限快照：{json.dumps(run.get('permissions'), ensure_ascii=False)}",
            f"预算：{json.dumps(run.get('budget'), ensure_ascii=False)} 用量：{json.dumps(run.get('usage'), ensure_ascii=False)}",
            "产物：\n" + _table(["路径", "大小", "hash"],
                                [[a["path"], a.get("size"), _short(a.get("content_hash"), 12)]
                                 for a in data.get("artifacts", [])]),
            "工具动作：\n" + _table(["#", "工具", "状态", "副作用", "耗时ms"],
                                    [[a["seq"], a["tool_name"], a["status"], a["side_effect_status"],
                                      a.get("duration_ms")] for a in data.get("actions", [])]),
            "技能加载（学习归因只认这里）：\n" + _table(
                ["版本", "文件", "类型", "字符", "状态"],
                [[_short(l["version_id"], 18), l["file_path"], l["load_kind"], l["chars"], l["load_status"]]
                 for l in data.get("loads", [])]),
            f"模型输出：\n{run.get('output_text') or '（无）'}",
        ])
        printer.out(data, human=human)
    elif args.task_cmd == "cancel":
        data = services.cancel_run(args.run_id)
        printer.out(data, human=f"任务 {args.run_id} → {data.get('execution_status')}")
    elif args.task_cmd == "resume":
        data = services.resume_run(args.run_id)
        printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
    else:
        feedback_type = args.type or ("acceptance" if args.verdict else "correction")
        data = services.submit_feedback(run_id=args.run_id, type=feedback_type,
                                        verdict=args.verdict, content=args.content, scope=args.scope)
        lines = [f"反馈已保存：{data.get('feedback_id')}（范围 {args.scope}）"]
        job_id = data.get("learning_job_id")
        if job_id and not args.no_learn:
            executed = services.workers.drain(["learning.extract"], limit=5)
            lessons = services.list_lessons(limit=20)["lessons"]
            lines.append(f"已执行后台学习作业 {job_id}（{executed} 个）："
                         f"当前经验 {len(lessons)} 条")
            if lessons:
                latest = lessons[0]
                lines.append(f"  最近一条：[{latest['attribution']}] {latest['rule_text'][:60]}")
                lines.append("  下一步：propose --skill <id> --lesson <lesson_id> 生成候选修改")
        elif job_id:
            lines.append(f"已入队学习作业 {job_id}（--no-learn，未立即执行）")
        elif data.get("learning_enqueue_error"):
            lines.append(f"学习入队失败（不影响反馈保存）：{data['learning_enqueue_error']}")
        else:
            lines.append("本次反馈未进入长期学习队列（仅本次任务 / 学习已暂停 / 无可归因目标）")
        printer.out(data, human="\n".join(lines))
    return 0


def _proposal(args, services, printer: Printer) -> int:
    cmd = args.proposal_cmd
    if cmd == "list":
        data = services.list_proposals(skill_id=args.skill_id, status=args.status)
        rows = [[p["id"], p["status"], _short(p.get("baseline_version_id"), 18),
                 _short(p.get("candidate_version_id"), 18), _short(p.get("summary") or p.get("reason"), 36)]
                for p in data["proposals"]]
        printer.out(data, human=_table(["id", "状态", "基线", "候选", "摘要"], rows))
    elif cmd == "show":
        data = services.get_proposal(args.proposal_id)
        proposal = data["proposal"]
        human = "\n".join([
            f"候选 {proposal.get('id')} 状态={proposal.get('status')} 作用域={proposal.get('scope')}",
            f"基线 {proposal.get('baseline_version_id')} → 候选 {proposal.get('candidate_version_id')}",
            f"理由：{proposal.get('reason')}",
            f"预期收益：{proposal.get('expected_benefit')}",
            f"潜在退化：{proposal.get('potential_regression')}",
            f"能力变化：{json.dumps(proposal.get('capability_change'), ensure_ascii=False)}",
            f"检查：{json.dumps(proposal.get('checks'), ensure_ascii=False)[:1200]}",
            f"评测：{json.dumps(proposal.get('evaluation'), ensure_ascii=False)}",
            "来源：\n" + _table(["lesson", "归因", "规则"],
                                [[s["lesson_id"], s.get("attribution"), _short(s.get("rule_text"), 44)]
                                 for s in data.get("sources", [])]),
            "差异：\n" + _table(["文件", "变更"], [[d["path"], d["change"]] for d in data.get("diff", [])]),
        ])
        printer.out(data, human=human)
    elif cmd == "diff":
        data = {"diff": services.proposals.get_diff(args.proposal_id)}
        lines = []
        for item in data["diff"]:
            lines.append(f"=== {item['path']} ({item['change']}) ===")
            lines.append(item.get("diff") or "(无文本差异)")
        printer.out(data, human="\n".join(lines) or "（无差异）")
    elif cmd == "check":
        data = services.check_proposal(args.proposal_id)
        printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
    elif cmd == "reject":
        data = services.reject_proposal(args.proposal_id, reason_code=args.reason)
        printer.out(data, human=f"已拒绝候选 {args.proposal_id}（原因 {args.reason}）")
    else:
        data = services.activate_proposal(args.proposal_id, expected_revision=args.expected_revision,
                                          report_id=getattr(args, "report_id", None))
        printer.out(data, human=f"已启用：version={data.get('version_id')} revision={data.get('revision')}")
    return 0


def _load_json_arg(value: str) -> Any:
    if value.strip().startswith("[") or value.strip().startswith("{"):
        return json.loads(value)
    with open(value, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _eval(args, services, printer: Printer) -> int:
    cmd = args.eval_cmd
    if cmd == "suite-create":
        data = services.create_suite(
            skill_id=args.skill_id, name=args.name, category=args.category,
            cases=_load_json_arg(args.cases), rubric=_load_json_arg(args.rubric),
            critical_checks=_load_json_arg(args.critical), thresholds=_load_json_arg(args.thresholds),
            objective=args.objective, freeze=not args.no_freeze,
        )
        suite = data["suite"]
        suite = suite.get("suite", suite) if isinstance(suite, dict) else suite
        printer.out(data, human=f"评测集已创建：{suite.get('id') or suite.get('suite_id')} "
                                f"revision={suite.get('revision')} "
                                f"{'（已冻结）' if suite.get('status') == 'frozen' else ''}")
    elif cmd == "suite-list":
        data = services.list_suites(skill_id=args.skill_id)
        rows = [[s.get("id") or s.get("suite_id"), s.get("name"), s.get("revision"), s.get("status"),
                 s.get("objective"), _short(s.get("suite_hash"), 12)] for s in data["suites"]]
        printer.out(data, human=_table(["id", "名称", "rev", "状态", "目标", "hash"], rows))
    elif cmd == "suite-show":
        data = services.get_suite(args.suite_id)
        printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
    elif cmd == "suite-freeze":
        data = services.freeze_suite(args.suite_id)
        printer.out(data, human=f"评测集已冻结：{json.dumps(data.get('suite', {}), ensure_ascii=False)[:400]}")
    elif cmd == "start":
        data = services.start_evaluation(args.proposal_id, suite_id=args.suite_id)
        job_id = data.get("job_id") or data.get("id")
        if not args.wait:
            printer.out(data, human=f"评测作业已启动：{job_id}（后台执行，jobs/eval report 查看）")
            return 0
        services.start_workers()
        deadline = time.time() + float(args.timeout)
        try:
            while time.time() < deadline:
                job = services.evals.get_job(job_id)
                job = job.get("job", job)
                if job.get("status") in ("completed", "failed", "cancelled", "budget_stopped"):
                    break
                time.sleep(0.3)
        finally:
            services.stop_workers()
        report = services.get_eval_job(job_id)
        printer.out(report, human=json.dumps(report.get("report") or {}, ensure_ascii=False, indent=2)[:4000])
    elif cmd == "jobs":
        data = services.list_eval_jobs(proposal_id=args.proposal_id)
        rows = [[j["id"], j["status"], j.get("verdict"), _short(j.get("baseline_version_id"), 16),
                 _short(j.get("candidate_version_id"), 16)] for j in data["jobs"]]
        printer.out(data, human=_table(["job_id", "状态", "结论", "基线", "候选"], rows))
    elif cmd == "report":
        data = services.get_eval_job(args.job_id)
        report = data.get("report") or {}
        human = json.dumps(report, ensure_ascii=False, indent=2)
        printer.out(data, human=human)
    elif cmd == "cancel":
        data = services.cancel_eval_job(args.job_id)
        printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
    else:
        data = services.resume_eval_job(args.job_id)
        printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def _data(args, services, printer: Printer) -> int:
    cmd = args.data_cmd
    if cmd == "usage":
        data = services.data_usage()
        usage = data["usage"]
        human = (f"模型调用 {usage.get('model_calls', 0)} 次 · token {usage.get('tokens', 0)} · "
                 f"用量未知 {usage.get('unknown_usage', 0)} 次\n"
                 f"制品 {usage.get('blob_count', 0)} 个 / {usage.get('blob_bytes', 0)} B · "
                 f"数据库 {usage.get('db_bytes', 0)} B · 剩余磁盘 {usage.get('free_bytes', 0)} B\n"
                 "说明：缺少价格信息时只报告 token 与调用数，不虚构金额。")
        printer.out(data, human=human)
    elif cmd == "backup":
        dest = args.dest
        data = services.backup(dest, include_body=not args.meta_only)
        manifest = data.get("manifest") or {}
        printer.out(data, human=f"备份完成：{data.get('dest') or data.get('path')}\n"
                                f"包含对象：{json.dumps({k: v for k, v in manifest.items() if k.endswith('count') or k == 'objects'}, ensure_ascii=False)[:300]}\n"
                                "凭据不入备份。")
    elif cmd == "restore":
        data = services.restore(args.backup_path, dry_run=not args.apply)
        printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
    elif cmd == "export":
        scope = {"skill_id": args.skill_id} if args.skill_id else {}
        data = services.export_bundle(args.dest, scope=scope)
        printer.out(data, human=f"导出完成：{data.get('dest') or data.get('path')}（无凭据）")
    elif cmd == "preview-clear":
        data = services.preview_clear(_load_json_arg(args.scope))
        printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
    elif cmd == "retention-show":
        data = services.retention_policy()
        policy = data["retention"]
        printer.out(data, human=(f"正文保留策略：{policy['description']}\n"
                                 f"已到期任务：{policy['expired_runs']} · 已到期反馈：{policy['expired_feedback']}"))
    elif cmd == "retention-set":
        data = services.set_retention(args.days)
        printer.out(data, human="已更新：" + data["retention"]["description"])
    elif cmd == "retention-preview":
        data = services.retention_preview(args.days)
        printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
    elif cmd == "retention-apply":
        data = services.apply_retention(args.days, confirm=args.confirm,
                                        purge_artifacts=args.purge_artifacts)
        printer.out(data, human=(f"已清除正文：任务 {data['expired_runs']} 个、反馈 {data['expired_feedback']} 条\n"
                                 f"{data['note']}"))
    else:
        data = services.clear_data(_load_json_arg(args.scope), confirm=args.confirm)
        printer.out(data, human=json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
