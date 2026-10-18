from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

from ..core.errors import InvalidInput, PermissionDenied

REF_PREFIX = "file:"
REDACTED = "***"

#: 凭据名允许的字符集（单段文件名，无路径分隔符）
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: 小于该长度的「密钥」不做替换，避免把常见短串（如 "test"）误伤成 ***
_MIN_REDACT_LEN = 4


class CredentialStore:
    """本机凭据存储：明文只进文件，业务侧只流转引用。"""

    def __init__(self, secret_dir: Path) -> None:
        self.secret_dir = Path(secret_dir)
        self.secret_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.secret_dir, 0o700)
        except OSError:  # pragma: no cover - 非 POSIX 环境
            pass

    # ------------------------------------------------------------- 名字与路径
    @staticmethod
    def validate_name(name: str) -> str:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise InvalidInput(
                f"非法凭据名: {name!r}（只允许字母数字与 . _ -，且不能是路径）",
                field_errors={"name": "invalid_credential_name"},
            )
        return name

    def _path(self, name: str) -> Path:
        self.validate_name(name)
        root = self.secret_dir.resolve(strict=False)
        target = (self.secret_dir / name).resolve(strict=False)
        if target.parent != root:
            raise PermissionDenied(
                f"凭据路径越界: {name}", field_errors={"name": "outside_secret_dir"}
            )
        return target

    @staticmethod
    def _name_from_ref(ref: str | None) -> str | None:
        if not ref or not isinstance(ref, str):
            return None
        name = ref[len(REF_PREFIX):] if ref.startswith(REF_PREFIX) else ref
        if not _NAME_RE.match(name):
            return None
        return name

    # ------------------------------------------------------------- 读写
    def put(self, name: str, secret: str) -> str:
        """写入凭据，返回 ``file:<name>``。文件权限固定 0600。"""
        if not isinstance(secret, str) or secret == "":
            raise InvalidInput("凭据内容不能为空", field_errors={"secret": "empty"})
        path = self._path(name)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secret)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:  # pragma: no cover
                pass
            raise
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - 非 POSIX 环境
            pass
        return f"{REF_PREFIX}{name}"

    def get(self, ref: str) -> str | None:
        """按引用读取明文；引用非法或文件缺失/损坏时返回 ``None``。"""
        name = self._name_from_ref(ref)
        if name is None:
            return None
        path = self.secret_dir / name
        try:
            if not path.is_file():
                return None
            return path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - 读取失败按不可用处理
            return None

    def exists(self, ref: str) -> bool:
        name = self._name_from_ref(ref)
        return bool(name) and (self.secret_dir / name).is_file()

    def delete(self, ref: str) -> None:
        """删除凭据；引用非法时抛 ``InvalidInput``，文件已不存在时静默成功。"""
        name = self._name_from_ref(ref)
        if name is None:
            raise InvalidInput(f"非法凭据引用: {ref!r}", field_errors={"ref": "invalid"})
        path = self.secret_dir / name
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:  # pragma: no cover
            raise InvalidInput(f"删除凭据失败: {name}") from exc

    def list_refs(self) -> list[str]:
        """只返回引用，不返回值。"""
        if not self.secret_dir.exists():
            return []
        refs = [
            f"{REF_PREFIX}{p.name}"
            for p in sorted(self.secret_dir.iterdir(), key=lambda p: p.name)
            if p.is_file() and not p.name.startswith(".") and _NAME_RE.match(p.name)
        ]
        return refs

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"CredentialStore(secret_dir={str(self.secret_dir)!r}, refs={len(self.list_refs())})"


def redact(text: str, secrets: Iterable[str]) -> str:
    """把 ``secrets`` 中出现的明文替换为 ``***``（日志/导出/审计统一入口）。"""
    if text is None:
        return text
    out = str(text)
    if not out:
        return out
    # 长密钥优先替换，避免短密钥破坏长密钥的部分匹配
    for secret in sorted({s for s in secrets if s and len(s) >= _MIN_REDACT_LEN}, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, REDACTED)
    return out


def redact_structure(value, secrets: Iterable[str]):
    """递归脱敏 dict / list / str（导出与事件 payload 使用）。"""
    secrets = list(secrets or [])
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {k: redact_structure(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_structure(v, secrets) for v in value]
    return value
