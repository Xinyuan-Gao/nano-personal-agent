from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core.blobs import BlobStore
from ..core.errors import InvalidInput, MissingDependency, PermissionDenied

SIDE_EFFECTS = {"list_dir": "none", "read_text": "none", "search_text": "none",
                "write_artifact": "write", "patch_file": "write", "run_command": "exec"}

TOOL_SPECS: dict[str, dict] = {
    "list_dir": {
        "description": "列出授权目录内的文件与子目录。参数 path 为相对授权根目录的路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径，默认为 ."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "read_text": {
        "description": "读取授权目录内文本文件的内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 400000, "default": 200000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "search_text": {
        "description": "在授权目录内检索文本，返回匹配行与位置。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "is_regex": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "write_artifact": {
        "description": "把产物写入本次任务的产物目录（唯一默认可写位置）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "产物目录内相对路径"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "patch_file": {
        "description": "在已明确授权的既有用户文件内做精确文本替换（需 allow_existing_write）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "find": {"type": "string"},
                "replace": {"type": "string"},
                "expected_hash": {"type": "string", "description": "可选：修改前文件内容 hash，用于并发保护"},
            },
            "required": ["path", "find", "replace"],
            "additionalProperties": False,
        },
    },
    "run_command": {
        "description": "在受隔离容器内执行脚本（输入只读、产物可写、默认无网络）。",
        "parameters": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "timeout_s": {"type": "integer", "minimum": 1, "maximum": 600, "default": 60},
                "network": {"type": "boolean", "default": False},
            },
            "required": ["script"],
            "additionalProperties": False,
        },
    },
}

_TYPE_MAP = {"string": str, "integer": int, "boolean": bool, "number": (int, float), "object": dict,
             "array": list}


@dataclass
class ToolResult:
    status: str                       # ok | failed | partial | denied
    output: str = ""
    artifacts: list[dict] = field(default_factory=list)
    side_effect_status: str = "none"  # none | applied | unknown
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "output": self.output,
            "artifacts": self.artifacts,
            "side_effect_status": self.side_effect_status,
            "error_code": self.error_code,
        }


@dataclass
class ToolContext:
    run_id: str
    workspace: dict
    permissions: dict
    artifacts_dir: Path
    container: object | None = None
    input_roots: list[Path] = field(default_factory=list)
    action_seq: int = 0
    blobs: BlobStore | None = None

    # ------------------------------------------------------------- 权限辅助
    @property
    def read_roots(self) -> list[Path]:
        roots = [Path(p) for p in (self.permissions.get("read_roots") or [])]
        if self.artifacts_dir not in roots:
            roots.append(Path(self.artifacts_dir))
        return roots

    @property
    def write_roots(self) -> list[Path]:
        roots = [Path(p) for p in (self.permissions.get("write_roots") or [])]
        if self.artifacts_dir not in roots:
            roots.append(Path(self.artifacts_dir))
        return roots

    @property
    def allowed_tools(self) -> list[str]:
        return list(self.permissions.get("tools") or [])

    def tool_allowed(self, name: str) -> bool:
        return name in self.allowed_tools


class ToolRegistry:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._handlers = _build_handlers(ctx)

    # ------------------------------------------------------------- 查询
    def available(self) -> list[str]:
        out = []
        for name in TOOL_SPECS:
            if name not in self._handlers or not self.ctx.tool_allowed(name):
                continue
            if name == "run_command" and not self.ctx.permissions.get("scripts", False):
                continue
            out.append(name)
        return out

    def specs(self) -> list[dict]:
        """OpenAI 兼容的 tools 参数（含副作用类别，供审计与恢复判断）。"""
        out = []
        for name in self.available():
            spec = TOOL_SPECS[name]
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": spec["description"],
                        "parameters": spec["parameters"],
                    },
                    "side_effect": SIDE_EFFECTS[name],
                }
            )
        return out

    def side_effect(self, name: str) -> str:
        if name not in TOOL_SPECS:
            raise InvalidInput(f"未知工具: {name}", field_errors={"tool": "unknown_tool"})
        return SIDE_EFFECTS[name]

    def get(self, name: str):
        if name not in TOOL_SPECS:
            raise InvalidInput(f"未知工具: {name}", field_errors={"tool": "unknown_tool"})
        if not self.ctx.tool_allowed(name):
            raise PermissionDenied(
                f"工具 {name} 未在本次运行授权范围内", field_errors={"tool": "not_authorized"}
            )
        if name == "run_command" and not self.ctx.permissions.get("scripts", False):
            raise PermissionDenied(
                "本次运行未授权脚本执行", field_errors={"tool": "script_execution_not_authorized"}
            )
        if name not in self._handlers:
            raise MissingDependency(
                f"工具 {name} 在当前环境不可用（依赖缺失，如容器运行时不可用）",
                field_errors={"tool": "unavailable"},
            )
        return self._handlers[name]

    # ------------------------------------------------------------- 校验与执行
    def validate(self, name: str, arguments: dict | None) -> dict:
        """按 schema 校验参数；未知参数、类型错误、缺必填都拒绝。"""
        if name not in TOOL_SPECS:
            raise InvalidInput(f"未知工具: {name}", field_errors={"tool": "unknown_tool"})
        schema = TOOL_SPECS[name]["parameters"]
        args = dict(arguments or {})
        properties = schema.get("properties", {})
        unknown = [k for k in args if k not in properties]
        if unknown and not schema.get("additionalProperties", True):
            raise InvalidInput(
                f"工具 {name} 收到未知参数: {', '.join(sorted(unknown))}",
                field_errors={k: "unknown_parameter" for k in unknown},
            )
        cleaned: dict = {}
        for key, spec in properties.items():
            if key in args and args[key] is not None:
                value = args[key]
                expected = _TYPE_MAP.get(spec.get("type"))
                if expected and isinstance(value, bool) and spec.get("type") != "boolean":
                    raise InvalidInput(f"参数 {key} 类型错误", field_errors={key: "invalid_type"})
                if expected and not isinstance(value, expected):
                    raise InvalidInput(f"参数 {key} 类型错误", field_errors={key: "invalid_type"})
                if spec.get("type") == "integer" and isinstance(value, bool):
                    raise InvalidInput(f"参数 {key} 类型错误", field_errors={key: "invalid_type"})
                if "minimum" in spec and isinstance(value, (int, float)) and value < spec["minimum"]:
                    raise InvalidInput(f"参数 {key} 小于最小值", field_errors={key: "out_of_range"})
                if "maximum" in spec and isinstance(value, (int, float)) and value > spec["maximum"]:
                    raise InvalidInput(f"参数 {key} 超过最大值", field_errors={key: "out_of_range"})
                cleaned[key] = value
            elif "default" in spec:
                cleaned[key] = spec["default"]
        missing = [k for k in schema.get("required", []) if k not in cleaned]
        if missing:
            raise InvalidInput(
                f"工具 {name} 缺少必填参数: {', '.join(missing)}",
                field_errors={k: "required" for k in missing},
            )
        return cleaned

    def execute(self, name: str, arguments: dict | None) -> ToolResult:
        """校验并执行一个工具动作。异常统一转成失败结果，不抛出到循环外。"""
        try:
            handler = self.get(name)
            args = self.validate(name, arguments)
        except (InvalidInput, PermissionDenied) as exc:
            return ToolResult(status="denied", output=exc.message, error_code=exc.code,
                              side_effect_status="none")
        except MissingDependency as exc:
            return ToolResult(status="failed", output=exc.message, error_code=exc.code,
                              side_effect_status="none")
        try:
            result = handler(**args)
        except PermissionDenied as exc:
            return ToolResult(status="denied", output=exc.message, error_code=exc.code,
                              side_effect_status="none")
        except MissingDependency as exc:
            return ToolResult(status="failed", output=exc.message, error_code=exc.code,
                              side_effect_status="none")
        except InvalidInput as exc:
            return ToolResult(status="failed", output=exc.message, error_code=exc.code,
                              side_effect_status="none")
        except OSError as exc:
            return ToolResult(status="failed", output=f"文件系统错误: {exc}", error_code="storage_error",
                              side_effect_status="unknown")
        if not isinstance(result, ToolResult):
            return ToolResult(status="failed", output="工具返回了非法结果", error_code="invalid_state")
        return result


def _build_handlers(ctx: ToolContext) -> dict:
    from functools import partial

    from . import tools_exec, tools_fs

    handlers = {
        "list_dir": tools_fs.list_dir,
        "read_text": tools_fs.read_text,
        "search_text": tools_fs.search_text,
        "write_artifact": tools_fs.write_artifact,
        "patch_file": tools_fs.patch_file,
        "run_command": tools_exec.run_command,
    }
    available = set(ctx.allowed_tools)
    if ctx.container is not None and hasattr(ctx.container, "status"):
        try:
            status = ctx.container.status()
            if not getattr(status, "available", False):
                available.discard("run_command")
        except Exception:  # pragma: no cover - 探测失败按不可用处理
            available.discard("run_command")
    if not ctx.permissions.get("scripts", False):
        available.discard("run_command")
    return {name: partial(fn, ctx) for name, fn in handlers.items() if name in available}
