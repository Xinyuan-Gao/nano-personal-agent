from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from .errors import InvalidInput, PermissionDenied

#: 导入时跳过并展示的目录/文件名
IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    ".DS_Store",
    ".tox",
    "dist-info",
}
IGNORED_FILE_SUFFIXES = (".pyc", ".pyo", ".log", ".swp", ".tmp")
IGNORED_FILE_NAMES = {".DS_Store", "Thumbs.db"}

#: 明显凭据模式（硬检查阻断）
SECRET_PATTERNS = [
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token|client[_-]?secret)\b"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+=]{12,}"
        ),
    ),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("feishu_secret", re.compile(r"\b(?:u-|cli_)[A-Za-z0-9]{16,}\b")),
]


def normalize(path: str | os.PathLike) -> Path:
    return Path(os.path.expanduser(str(path))).resolve(strict=False)


def is_within(root: Path, target: Path) -> bool:
    root = normalize(root)
    target = normalize(target)
    try:
        return os.path.commonpath([str(root), str(target)]) == str(root)
    except ValueError:  # 不同盘符
        return False


def _reject_absolute(rel: str) -> None:
    if not rel or rel in (".", "./"):
        return
    pure = PurePosixPath(str(rel).replace("\\", "/"))
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", str(rel)):
        raise InvalidInput(f"拒绝绝对路径: {rel}", field_errors={"path": "absolute_path_not_allowed"})


def resolve_within(root: Path, rel: str) -> Path:
    """把 ``rel`` 解析到 ``root`` 内，阻止目录穿越与符号链接逃逸。

    校验的是**符号链接解析后的真实位置**。
    """
    _reject_absolute(rel)
    root_n = normalize(root)
    candidate = (root_n / str(rel)).resolve(strict=False)
    if candidate != root_n and not is_within(root_n, candidate):
        raise PermissionDenied(
            f"路径越界: {rel} 不在授权目录 {root_n} 内",
            field_errors={"path": "outside_authorized_root"},
        )
    # 逐级检查已存在的父目录，防止中途 symlink 指向外部
    probe = candidate
    while True:
        if probe.exists() and probe.is_symlink():
            resolved = probe.resolve(strict=False)
            if not is_within(root_n, resolved):
                raise PermissionDenied(
                    f"符号链接逃逸: {probe} -> {resolved}", field_errors={"path": "symlink_escape"}
                )
        if probe.parent == probe or probe == root_n:
            break
        probe = probe.parent
    return candidate


def safe_relpath(root: Path, target: Path) -> str:
    root_n = normalize(root)
    target_n = normalize(target)
    if not is_within(root_n, target_n):
        raise PermissionDenied(f"目标不在 {root_n} 内: {target_n}")
    return os.path.relpath(str(target_n), str(root_n)).replace(os.sep, "/")


def check_writable_target(root: Path, rel: str, *, allow_existing: bool = False) -> Path:
    """校验写目标；``allow_existing=False`` 时禁止覆盖既有用户文件。"""
    target = resolve_within(root, rel)
    if target.exists() and not allow_existing:
        raise PermissionDenied(
            f"目标已存在，覆盖既有用户文件需另行明确授权: {rel}",
            field_errors={"path": "existing_file_requires_authorization"},
        )
    return target


def check_readable_source(root: Path, rel: str) -> Path:
    target = resolve_within(root, rel)
    if not target.exists():
        raise InvalidInput(f"文件不存在: {rel}", field_errors={"path": "not_found"})
    if target.is_dir():
        raise InvalidInput(f"目标是目录: {rel}", field_errors={"path": "is_directory"})
    return target


def is_ignored_path(rel: str) -> bool:
    parts = PurePosixPath(str(rel).replace("\\", "/")).parts
    if any(p in IGNORED_DIRS for p in parts):
        return True
    name = parts[-1] if parts else ""
    if name in IGNORED_FILE_NAMES:
        return True
    return name.endswith(IGNORED_FILE_SUFFIXES)


def scan_secrets(text: str) -> list[dict]:
    findings: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match:
                excerpt = match.group(0)
                findings.append(
                    {
                        "kind": kind,
                        "line": lineno,
                        "excerpt": excerpt[:12] + "***" if len(excerpt) > 12 else "***",
                    }
                )
                break
    return findings


def ensure_inside(root: Path, candidate: Path, *, what: str = "路径") -> Path:
    if not is_within(root, candidate):
        raise PermissionDenied(f"{what}越界: {candidate}")
    return candidate
