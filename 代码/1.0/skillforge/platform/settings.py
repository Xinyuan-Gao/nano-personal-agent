from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core import paths as pathmod
from ..core.clock import utc_now
from ..core.config import (
    DEFAULT_EVAL_BUDGET,
    DEFAULT_LEARNING_BUDGET,
    DEFAULT_RUN_BUDGET,
    SETTING_CONTAINER_ENABLED,
    read_json_setting,
    write_json_setting,
)
from ..core.errors import InvalidInput, PermissionDenied
from ..core.ids import canonical_hash, new_id
from ..core.repository import row_to_dict

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，避免运行时循环依赖
    from .container import ContainerRuntime

#: 工作区允许工具白名单
ALLOWED_TOOLS = (
    "list_dir",
    "read_text",
    "search_text",
    "write_artifact",
    "patch_file",
    "run_command",
)
DEFAULT_WORKSPACE_TOOLS = ["list_dir", "read_text", "search_text", "write_artifact"]
#: 需要脚本执行能力的工具：容器不可用或未授权脚本时必须从可用工具中移除。
SCRIPT_TOOLS = ("run_command",)

PROFILE_ID = "local"
MODEL_LABEL = "default"

BUDGET_KEYS = ("max_model_calls", "max_tool_actions", "max_wall_seconds", "max_tokens")
BUDGET_SETTING_KEYS = {
    "run": "budget.run",
    "learning": "budget.learning",
    "evaluation": "budget.evaluation",
}


class SettingsService:
    def __init__(self, store, repo, events, idem, creds, config) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.idem = idem
        self.creds = creds
        self.config = config

    # ------------------------------------------------------------- profile
    def bootstrap(self) -> dict:
        """确保 ``profile='local'`` 存在，返回 profile。幂等。"""
        row = self.repo.get("profile", PROFILE_ID)
        now = utc_now()
        if row is None:
            with self.store.tx() as conn:
                conn.execute(
                    "INSERT INTO profile (id, name, kind, status, created_at, updated_at) "
                    "VALUES (?, ?, 'local_owner', 'active', ?, ?)",
                    (PROFILE_ID, "本机用户", now, now),
                )
            row = self.repo.get("profile", PROFILE_ID)
        elif row["status"] != "active":
            self.repo.update("profile", PROFILE_ID, {"status": "active", "updated_at": now})
            row = self.repo.get("profile", PROFILE_ID)
        return row_to_dict(row) or {}

    def _ensure_profile(self, profile_id: str) -> None:
        if profile_id == PROFILE_ID:
            self.bootstrap()
            return
        if self.repo.get("profile", profile_id) is None:
            raise InvalidInput(
                f"profile 不存在: {profile_id}", field_errors={"profile_id": "not_found"}
            )

    # ------------------------------------------------------------- 模型配置
    def _model_row(self, profile_id: str):
        return self.store.query_one(
            "SELECT * FROM model_config WHERE profile_id = ? AND label = ?",
            (profile_id, MODEL_LABEL),
        )

    def get_model_config(self, profile_id: str = PROFILE_ID) -> dict:
        """返回模型配置；**不含密钥**，只含 ``api_key_ref``。"""
        row = self._model_row(profile_id)
        if row is None:
            return {
                "profile_id": profile_id,
                "configured": False,
                "provider": "openai_compatible",
                "base_url": "",
                "model": "",
                "api_key_ref": None,
                "has_credential": False,
                "temperature": 0.2,
                "max_output_tokens": 2048,
                "timeout_s": 60.0,
                "extra": {},
                "is_active": False,
                "last_check": None,
                "last_check_ok": None,
            }
        data = row_to_dict(row, json_fields=("extra",)) or {}
        data["configured"] = bool(data.get("model") and data.get("base_url"))
        data["has_credential"] = bool(data.get("api_key_ref")) and self.creds.exists(
            data.get("api_key_ref") or ""
        )
        data.pop("id", None)
        return data

    def set_model_config(
        self,
        *,
        provider: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
        api_key_ref: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
        timeout_s: float = 60.0,
        extra: dict | None = None,
        profile_id: str = PROFILE_ID,
    ) -> dict:
        """保存模型配置。``api_key`` 明文只写入 ``CredentialStore``，库中只存 ref。"""
        self._ensure_profile(profile_id)
        if not (base_url or "").strip():
            raise InvalidInput("base_url 不能为空", field_errors={"base_url": "required"})
        if not (model or "").strip():
            raise InvalidInput("model 不能为空", field_errors={"model": "required"})
        if not (provider or "").strip():
            raise InvalidInput("provider 不能为空", field_errors={"provider": "required"})
        if max_output_tokens is not None and int(max_output_tokens) <= 0:
            raise InvalidInput(
                "max_output_tokens 必须为正整数", field_errors={"max_output_tokens": "invalid"}
            )
        if timeout_s is not None and float(timeout_s) <= 0:
            raise InvalidInput("timeout_s 必须为正数", field_errors={"timeout_s": "invalid"})
        if extra is not None and not isinstance(extra, dict):
            raise InvalidInput("extra 必须是对象", field_errors={"extra": "invalid"})

        ref = api_key_ref
        if api_key:
            # 明文只进凭据存储，返回值仅是引用
            ref = self.creds.put(f"model.{profile_id}.{MODEL_LABEL}", api_key)

        now = utc_now()
        row = self._model_row(profile_id)
        payload = {
            "provider": provider,
            "base_url": base_url.rstrip("/"),
            "model": model,
            "api_key_ref": ref,
            "temperature": float(temperature),
            "max_output_tokens": int(max_output_tokens),
            "timeout_s": float(timeout_s),
            "extra": json.dumps(extra or {}, ensure_ascii=False),
            "is_active": 1,
            "updated_at": now,
        }
        before = row_to_dict(row, json_fields=("extra",)) if row is not None else {}
        with self.store.tx() as conn:
            if row is None:
                payload.update(
                    {"id": new_id("mc"), "profile_id": profile_id, "label": MODEL_LABEL, "created_at": now}
                )
                self.repo.insert("model_config", payload, conn=conn)
            else:
                self.repo.update("model_config", row["id"], payload, conn=conn)
            self.events.audit(
                "model_config.set",
                "model_config",
                f"{profile_id}:{MODEL_LABEL}",
                before=_safe_model(before),
                after=_safe_model(payload),
                conn=conn,
            )
        return self.get_model_config(profile_id)

    def clear_model_config(self, profile_id: str = PROFILE_ID) -> dict:
        row = self._model_row(profile_id)
        if row is None:
            return {"cleared": False, "profile_id": profile_id}
        ref = row["api_key_ref"]
        with self.store.tx() as conn:
            self.repo.delete("model_config", {"id": row["id"]}, conn=conn)
            self.events.audit(
                "model_config.clear",
                "model_config",
                f"{profile_id}:{MODEL_LABEL}",
                before=_safe_model(row_to_dict(row)),
                after={},
                conn=conn,
            )
        if ref:
            try:
                self.creds.delete(ref)
            except InvalidInput:  # pragma: no cover - 引用异常时忽略
                pass
        return {"cleared": True, "profile_id": profile_id, "api_key_ref": ref}

    def record_model_check(self, ok: bool, detail: dict, profile_id: str = PROFILE_ID) -> dict:
        row = self._model_row(profile_id)
        now = utc_now()
        safe_detail = _safe_detail(detail)
        if row is None:
            return {"profile_id": profile_id, "ok": bool(ok), "checked_at": now, "detail": safe_detail}
        self.repo.update(
            "model_config",
            row["id"],
            {"last_check": now, "last_check_ok": 1 if ok else 0, "updated_at": now},
        )
        self.events.audit(
            "model_config.check",
            "model_config",
            f"{profile_id}:{MODEL_LABEL}",
            after={"ok": bool(ok), "detail": safe_detail},
        )
        return {"profile_id": profile_id, "ok": bool(ok), "checked_at": now, "detail": safe_detail}

    # ------------------------------------------------------------- 工作区
    def list_workspaces(self, profile_id: str = PROFILE_ID, include_removed: bool = False) -> list[dict]:
        sql = "SELECT * FROM workspace WHERE profile_id = ?"
        params: list = [profile_id]
        if not include_removed:
            sql += " AND status = 'active'"
        sql += " ORDER BY created_at ASC"
        rows = self.store.query(sql, params)
        return [self._workspace_dict(r) for r in rows]

    def get_workspace(self, workspace_id: str) -> dict:
        row = self.repo.get("workspace", workspace_id)
        if row is None:
            raise InvalidInput(f"工作区不存在: {workspace_id}", field_errors={"workspace_id": "not_found"})
        return self._workspace_dict(row)

    def add_workspace(
        self,
        *,
        name: str,
        root_path: str,
        artifact_path: str | None = None,
        allowed_tools: list[str] | None = None,
        allow_network: bool = False,
        allow_scripts: bool = True,
        profile_id: str = PROFILE_ID,
        idempotency_key: str | None = None,
    ) -> dict:
        self._ensure_profile(profile_id)
        if not (name or "").strip():
            raise InvalidInput("工作区名称不能为空", field_errors={"name": "required"})
        if not (root_path or "").strip():
            raise InvalidInput("root_path 不能为空", field_errors={"root_path": "required"})

        root = pathmod.normalize(root_path)
        self._reject_secret_path(root)
        if not root.exists():
            raise InvalidInput(
                f"可读目录不存在: {root}", field_errors={"root_path": "not_found"}
            )
        if not root.is_dir():
            raise InvalidInput(
                f"可读目录不是目录: {root}", field_errors={"root_path": "not_a_directory"}
            )

        tools = list(allowed_tools) if allowed_tools is not None else list(DEFAULT_WORKSPACE_TOOLS)
        unknown = [t for t in tools if t not in ALLOWED_TOOLS]
        if unknown:
            raise InvalidInput(
                f"未知工具: {', '.join(unknown)}",
                field_errors={"allowed_tools": "unknown_tool"},
            )
        if not allow_scripts:
            tools = [t for t in tools if t not in SCRIPT_TOOLS]

        artifact = pathmod.normalize(artifact_path) if artifact_path else None
        if artifact is not None:
            self._reject_secret_path(artifact)
            if artifact.exists() and not artifact.is_dir():
                raise InvalidInput(
                    f"产物路径不是目录: {artifact}", field_errors={"artifact_path": "not_a_directory"}
                )

        payload = {
            "name": name,
            "root_path": str(root),
            "artifact_path": str(artifact) if artifact else None,
            "allowed_tools": sorted(tools),
            "allow_network": bool(allow_network),
            "allow_scripts": bool(allow_scripts),
            "profile_id": profile_id,
        }

        def _create() -> dict:
            existing = self.store.query_one(
                "SELECT * FROM workspace WHERE profile_id = ? AND root_path = ?",
                (profile_id, str(root)),
            )
            if existing is not None and existing["status"] == "active":
                raise InvalidInput(
                    f"该目录已授权给工作区: {existing['name']}",
                    field_errors={"root_path": "already_authorized"},
                )
            workspace_id = new_id("ws")
            if existing is not None:  # 已移除的同路径工作区：重新启用并更新
                workspace_id = existing["id"]
            artifact_dir = artifact or self.config.workspace_artifacts_dir(workspace_id)
            try:
                artifact_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise InvalidInput(
                    f"无法创建产物目录: {artifact_dir}", field_errors={"artifact_path": "not_writable"}
                ) from exc
            now = utc_now()
            data = {
                "profile_id": profile_id,
                "name": name,
                "root_path": str(root),
                "artifact_path": str(artifact_dir),
                "allowed_tools": json.dumps(sorted(tools), ensure_ascii=False),
                "allow_network": 1 if allow_network else 0,
                "allow_scripts": 1 if allow_scripts else 0,
                "status": "active",
                "updated_at": now,
            }
            with self.store.tx() as conn:
                if existing is not None:
                    self.repo.update("workspace", workspace_id, data, conn=conn)
                else:
                    data.update({"id": workspace_id, "created_at": now})
                    self.repo.insert("workspace", data, conn=conn)
                self.events.audit(
                    "workspace.add",
                    "workspace",
                    workspace_id,
                    after={**_workspace_public(data), "allowed_tools": sorted(tools)},
                    conn=conn,
                )
            return self.get_workspace(workspace_id)

        return self.idem.run("workspace.add", idempotency_key, payload, _create)

    def remove_workspace(self, workspace_id: str) -> dict:
        row = self.repo.get("workspace", workspace_id)
        if row is None:
            raise InvalidInput(f"工作区不存在: {workspace_id}", field_errors={"workspace_id": "not_found"})
        now = utc_now()
        with self.store.tx() as conn:
            self.repo.update(
                "workspace", workspace_id, {"status": "removed", "updated_at": now}, conn=conn
            )
            self.events.audit(
                "workspace.remove",
                "workspace",
                workspace_id,
                before={"status": row["status"]},
                after={"status": "removed"},
                conn=conn,
            )
        return {"workspace_id": workspace_id, "status": "removed", "removed_at": now}

    def check_workspace(self, workspace_id: str) -> dict:
        """读/写探测 + 规范化 symlink 后的实际路径。"""
        row = self.repo.get("workspace", workspace_id)
        if row is None:
            raise InvalidInput(f"工作区不存在: {workspace_id}", field_errors={"workspace_id": "not_found"})
        configured_root = Path(row["root_path"])
        resolved_root = pathmod.normalize(row["root_path"])
        configured_artifact = Path(row["artifact_path"])
        resolved_artifact = pathmod.normalize(row["artifact_path"])

        warnings: list[str] = []
        errors: list[dict] = []

        root_exists = resolved_root.is_dir()
        if not root_exists:
            errors.append({"field": "root_path", "code": "not_found", "message": f"目录不存在: {resolved_root}"})
        else:
            try:
                os.listdir(resolved_root)
            except OSError as exc:
                errors.append({"field": "root_path", "code": "not_readable", "message": str(exc)})
        try:
            self._reject_secret_path(resolved_root)
        except PermissionDenied as exc:
            errors.append({"field": "root_path", "code": exc.code, "message": exc.message})

        if resolved_root != configured_root.expanduser():
            warnings.append(f"路径已规范化到实际位置: {resolved_root}")

        artifact_ok = True
        probe_error: str | None = None
        try:
            self._reject_secret_path(resolved_artifact)
            resolved_artifact.mkdir(parents=True, exist_ok=True)
            probe = resolved_artifact / f".skillforge_write_probe_{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except (OSError, PermissionDenied) as exc:
            artifact_ok = False
            probe_error = str(exc)
            errors.append({"field": "artifact_path", "code": "not_writable", "message": probe_error})

        if resolved_artifact != configured_artifact.expanduser():
            warnings.append(f"产物路径已规范化到实际位置: {resolved_artifact}")

        ok = root_exists and artifact_ok and not any(
            e["code"] in {"outside_authorized_root", "symlink_escape"} for e in errors
        )
        return {
            "workspace_id": workspace_id,
            "ok": ok,
            "status": row["status"],
            "root_path": row["root_path"],
            "resolved_path": str(resolved_root),
            "artifact_path": row["artifact_path"],
            "artifact_resolved_path": str(resolved_artifact),
            "root_exists": root_exists,
            "root_readable": root_exists and not any(e["field"] == "root_path" for e in errors),
            "artifact_writable": artifact_ok,
            "write_probe_error": probe_error,
            "path_changed": resolved_root != configured_root.expanduser(),
            "warnings": warnings,
            "errors": errors,
            "checked_at": utc_now(),
        }

    def effective_permissions(self, workspace_id: str) -> dict:
        """返回本次授权范围；``run_command`` 只在授权脚本且容器启用时可用。"""
        row = self.repo.get("workspace", workspace_id)
        if row is None:
            raise InvalidInput(f"工作区不存在: {workspace_id}", field_errors={"workspace_id": "not_found"})
        allowed = _json_list(row["allowed_tools"]) or list(DEFAULT_WORKSPACE_TOOLS)
        allow_scripts = bool(row["allow_scripts"])
        container_enabled = bool(read_json_setting(self.store, SETTING_CONTAINER_ENABLED, True))
        tools = list(allowed)
        unavailable: list[dict] = []
        for tool in SCRIPT_TOOLS:
            if not allow_scripts:
                if tool in tools:
                    tools.remove(tool)
                unavailable.append({"tool": tool, "reason": "工作区未授权脚本执行"})
            elif not container_enabled:
                if tool in tools:
                    tools.remove(tool)
                unavailable.append({"tool": tool, "reason": "容器运行时已禁用，不降级为宿主 shell"})
        return {
            "workspace_id": workspace_id,
            "read_roots": [row["root_path"]],
            "write_roots": [row["artifact_path"]],
            "tools": tools,
            "tools_unavailable": unavailable,
            "network": bool(row["allow_network"]),
            "scripts": allow_scripts and container_enabled,
            "allowed_tools": allowed,
        }

    # ------------------------------------------------------------- 预算默认值
    def set_budget_defaults(
        self, *, run: dict | None = None, learning: dict | None = None, evaluation: dict | None = None
    ) -> dict:
        updates = {"run": run, "learning": learning, "evaluation": evaluation}
        with self.store.tx() as conn:
            for kind, value in updates.items():
                if value is None:
                    continue
                self._validate_budget(value, kind)
            for kind, value in updates.items():
                if value is None:
                    continue
                key = BUDGET_SETTING_KEYS[kind]
                now = utc_now()
                conn.execute(
                    "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
            self.events.audit(
                "budget_defaults.set",
                "setting",
                "budget",
                after={k: v for k, v in updates.items() if v is not None},
                conn=conn,
            )
        return self.get_budget_defaults()

    def get_budget_defaults(self) -> dict:
        return {
            "run": read_json_setting(self.store, BUDGET_SETTING_KEYS["run"], None) or dict(DEFAULT_RUN_BUDGET),
            "learning": read_json_setting(self.store, BUDGET_SETTING_KEYS["learning"], None)
            or dict(DEFAULT_LEARNING_BUDGET),
            "evaluation": read_json_setting(self.store, BUDGET_SETTING_KEYS["evaluation"], None)
            or dict(DEFAULT_EVAL_BUDGET),
        }

    @staticmethod
    def _validate_budget(value: dict, kind: str) -> None:
        if not isinstance(value, dict):
            raise InvalidInput(f"{kind} 预算必须是对象", field_errors={kind: "invalid"})
        for key, raw in value.items():
            if key not in BUDGET_KEYS:
                raise InvalidInput(f"未知预算字段: {key}", field_errors={key: "unknown_field"})
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise InvalidInput(f"预算字段 {key} 必须是整数", field_errors={key: "invalid"}) from None
            if number < 0:
                raise InvalidInput(f"预算字段 {key} 不能为负", field_errors={key: "invalid"})

    # ------------------------------------------------------------- 可用性检查
    def doctor(self, *, container: "ContainerRuntime", model_client=None) -> dict:
        """首次启动可用性检查：模型 + 数据目录写入 + 容器状态。"""
        model = self.get_model_config()
        model_report: dict[str, Any] = {
            "configured": bool(model.get("configured")),
            "provider": model.get("provider"),
            "base_url": model.get("base_url"),
            "model": model.get("model"),
            "has_credential": bool(model.get("has_credential")),
            "api_key_ref": model.get("api_key_ref"),
            "last_check": model.get("last_check"),
            "last_check_ok": model.get("last_check_ok"),
            "ok": bool(model.get("configured")),
            "check": None,
            "reason": None if model.get("configured") else "尚未配置模型连接",
        }
        if model_client is not None:
            try:
                result = model_client.check_connection()
            except Exception as exc:  # pragma: no cover - 客户端自身异常兜底
                result = {"ok": False, "error": {"code": "model_error", "message": str(exc)}}
            model_report["check"] = result
            model_report["ok"] = bool(result.get("ok"))
            if not result.get("ok"):
                model_report["reason"] = (result.get("error") or {}).get("message") or "连接检查失败"
            self.record_model_check(bool(result.get("ok")), result)

        data_dir = self._doctor_data_dir()

        try:
            status = container.status()
            container_report = {
                "available": bool(getattr(status, "available", False)),
                "engine": getattr(status, "engine", None),
                "version": getattr(status, "version", None),
                "reason": getattr(status, "reason", None),
            }
        except Exception as exc:  # pragma: no cover - 探测异常按不可用处理
            container_report = {
                "available": False,
                "engine": None,
                "version": None,
                "reason": f"容器探测失败: {exc}",
            }

        tools_available = ["list_dir", "read_text", "search_text", "write_artifact", "patch_file"]
        tools_unavailable: list[dict] = []
        if container_report["available"]:
            tools_available.append("run_command")
        else:
            tools_unavailable.append(
                {
                    "tool": "run_command",
                    "reason": f"容器运行时不可用: {container_report['reason'] or '未知原因'}（不降级为宿主 shell）",
                }
            )

        return {
            "model": model_report,
            "data_dir": data_dir,
            "container": container_report,
            "tools_available": tools_available,
            "tools_unavailable": tools_unavailable,
            "profile": self.bootstrap(),
            "checked_at": utc_now(),
        }

    def _doctor_data_dir(self) -> dict:
        home = Path(self.config.home)
        probe_error = None
        writable = False
        try:
            home.mkdir(parents=True, exist_ok=True)
            probe = home / f".doctor_probe_{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            writable = True
        except OSError as exc:
            probe_error = str(exc)
        usage: dict = {}
        try:
            usage = self.config.disk_usage()
        except Exception:  # pragma: no cover - 磁盘信息非关键
            usage = {}
        return {
            "home": str(home),
            "db_path": str(self.config.db_path),
            "writable": writable,
            "error": probe_error,
            "usage": usage,
        }

    # ------------------------------------------------------------- 内部
    def _reject_secret_path(self, target: Path) -> None:
        secret_dir = pathmod.normalize(self.config.secret_dir)
        if pathmod.is_within(secret_dir, target) or pathmod.is_within(target, secret_dir):
            raise PermissionDenied(
                f"凭据目录不能作为工作区路径: {target}",
                field_errors={"path": "secret_dir_not_allowed"},
            )

    def _workspace_dict(self, row) -> dict:
        data = row_to_dict(row) or {}
        data["allowed_tools"] = _json_list(data.get("allowed_tools")) or list(DEFAULT_WORKSPACE_TOOLS)
        data["allow_network"] = bool(data.get("allow_network"))
        data["allow_scripts"] = bool(data.get("allow_scripts"))
        data["root_fingerprint"] = canonical_hash(
            {"root_path": data.get("root_path"), "artifact_path": data.get("artifact_path")}
        )
        return data


def _json_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def _safe_model(data: dict | None) -> dict:
    """模型配置的审计视图：只保留引用，绝不落明文。"""
    data = dict(data or {})
    for key in ("id", "created_at", "updated_at"):
        data.pop(key, None)
    if "extra" in data and isinstance(data["extra"], str):
        try:
            data["extra"] = json.loads(data["extra"])
        except (TypeError, ValueError):
            pass
    data.pop("api_key", None)
    data.pop("secret", None)
    return data


def _safe_detail(detail: dict | None) -> dict:
    data = dict(detail or {})
    for key in ("api_key", "secret", "authorization", "token"):
        data.pop(key, None)
    return data


def _workspace_public(data: dict) -> dict:
    return {
        k: v
        for k, v in (data or {}).items()
        if k in {"name", "root_path", "artifact_path", "allow_network", "allow_scripts", "status"}
    }
