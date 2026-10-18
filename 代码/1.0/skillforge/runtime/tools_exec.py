from __future__ import annotations

from pathlib import Path

from ..core.errors import MissingDependency, PermissionDenied
from .tools import ToolContext, ToolResult


def run_command(ctx: ToolContext, script: str, timeout_s: int = 60, network: bool = False) -> ToolResult:
    if not ctx.permissions.get("scripts", False):
        return ToolResult(status="denied", output="本次运行未授权脚本执行", error_code="permission_denied")
    container = ctx.container
    if container is None:
        return ToolResult(
            status="failed",
            output="容器运行时未配置：脚本与命令功能不可用（不会降级为宿主机执行）",
            error_code="missing_dependency",
        )
    status = container.status()
    if not getattr(status, "available", False):
        return ToolResult(
            status="failed",
            output=f"容器运行时不可用（{getattr(status, 'reason', '未安装')}）：脚本与命令功能不可用",
            error_code="missing_dependency",
        )
    if network and not ctx.permissions.get("network", False):
        return ToolResult(status="denied", output="本次运行未授权网络访问", error_code="permission_denied")

    inputs_dir = Path(ctx.read_roots[0]) if ctx.read_roots else None
    outputs_dir = Path(ctx.artifacts_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = container.run(
            script,
            inputs_dir=inputs_dir,
            outputs_dir=outputs_dir,
            timeout_s=int(timeout_s),
            network=bool(network),
        )
    except MissingDependency as exc:
        return ToolResult(status="failed", output=exc.message, error_code="missing_dependency")
    except PermissionDenied as exc:
        return ToolResult(status="denied", output=exc.message, error_code="permission_denied")

    output = result.stdout or ""
    if result.stderr:
        output += ("\n" if output else "") + "[stderr]\n" + result.stderr
    if result.timed_out:
        return ToolResult(
            status="failed",
            output=f"命令超时（{timeout_s}s 后终止）\n{output}",
            error_code="budget_exceeded",
            side_effect_status="unknown",
        )
    if result.exit_code != 0:
        return ToolResult(
            status="failed",
            output=f"命令退出码 {result.exit_code}\n{output}",
            error_code="tool_error",
            side_effect_status="unknown",
        )
    return ToolResult(
        status="ok",
        output=f"命令成功（{result.duration_ms} ms）\n{output}",
        side_effect_status="applied",
    )
