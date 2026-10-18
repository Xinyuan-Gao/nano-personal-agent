from __future__ import annotations

import os
import re
from pathlib import Path

from ..core import paths as pathmod
from ..core.errors import InvalidInput, PermissionDenied
from ..core.ids import sha256_hex
from .tools import ToolContext, ToolResult

MAX_SEARCH_FILES = 2000
MAX_SEARCH_FILE_BYTES = 512 * 1024


def _pick_root(ctx: ToolContext, rel: str, roots: list[Path]) -> Path:
    """在候选根中挑第一个能合法解析 rel 的根；都不行则报权限错误。"""
    last_error: Exception | None = None
    for root in roots:
        try:
            return pathmod.resolve_within(root, rel)
        except (PermissionDenied, InvalidInput) as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise PermissionDenied("没有可用的授权目录", field_errors={"path": "no_authorized_root"})


def _format_rel(ctx: ToolContext, target: Path) -> str:
    for root in ctx.read_roots + ctx.write_roots:
        if pathmod.is_within(root, target):
            return pathmod.safe_relpath(root, target)
    return str(target)


# ------------------------------------------------------------------ list_dir
def list_dir(ctx: ToolContext, path: str = ".", depth: int = 1) -> ToolResult:
    target = _pick_root(ctx, path, ctx.read_roots)
    if not target.exists():
        return ToolResult(status="failed", output=f"目录不存在: {path}", error_code="invalid_input",
                          side_effect_status="none")
    if not target.is_dir():
        return ToolResult(status="failed", output=f"不是目录: {path}", error_code="invalid_input",
                          side_effect_status="none")
    lines: list[str] = []
    max_depth = max(1, min(int(depth or 1), 3))
    base_depth = len(target.parts)
    for root, dirs, files in os.walk(target):
        rel_root = Path(root)
        level = len(rel_root.parts) - base_depth
        if level >= max_depth:
            dirs[:] = []
        kept: list[str] = []
        for name in sorted(dirs):
            rel = pathmod.safe_relpath(target, rel_root / name)
            if pathmod.is_ignored_path(rel):
                continue
            kept.append(name)
            lines.append(f"{rel}/")
        dirs[:] = kept
        for name in sorted(files):
            rel = pathmod.safe_relpath(target, rel_root / name)
            if pathmod.is_ignored_path(rel):
                continue
            try:
                size = (rel_root / name).stat().st_size
            except OSError:
                size = 0
            lines.append(f"{rel}  ({size} B)")
        if len(lines) > 4000:
            lines.append("...(结果已截断)")
            break
    body = "\n".join(lines) if lines else "(空目录)"
    return ToolResult(status="ok", output=f"目录 {path} 共 {len(lines)} 项:\n{body}")


# ------------------------------------------------------------------ read_text
def read_text(ctx: ToolContext, path: str, max_bytes: int = 200_000) -> ToolResult:
    target = _pick_root(ctx, path, ctx.read_roots)
    if not target.exists():
        return ToolResult(status="failed", output=f"文件不存在: {path}", error_code="invalid_input")
    if target.is_dir():
        return ToolResult(status="failed", output=f"目标是目录: {path}", error_code="invalid_input")
    size = target.stat().st_size
    raw = target.read_bytes()[: int(max_bytes)]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ToolResult(status="failed", output=f"非 UTF-8 文本文件，已跳过: {path}",
                          error_code="invalid_input")
    truncated = size > len(raw)
    header = f"文件 {path}（{size} B{ '，已截断' if truncated else ''}）：\n"
    return ToolResult(
        status="ok",
        output=header + text,
        side_effect_status="none",
    )


# ------------------------------------------------------------------ search_text
def search_text(ctx: ToolContext, query: str, path: str = ".", is_regex: bool = False,
                max_results: int = 50) -> ToolResult:
    if not query:
        return ToolResult(status="failed", output="query 不能为空", error_code="invalid_input")
    start = _pick_root(ctx, path, ctx.read_roots)
    matcher = None
    if is_regex:
        try:
            matcher = re.compile(query)
        except re.error as exc:
            return ToolResult(status="failed", output=f"正则表达式无效: {exc}", error_code="invalid_input")
    results: list[str] = []
    scanned = 0
    for root, dirs, files in os.walk(start):
        dirs[:] = [d for d in sorted(dirs) if not pathmod.is_ignored_path(
            pathmod.safe_relpath(start, Path(root) / d))]
        for name in sorted(files):
            rel = pathmod.safe_relpath(start, Path(root) / name)
            if pathmod.is_ignored_path(rel):
                continue
            if scanned >= MAX_SEARCH_FILES or len(results) >= max_results:
                break
            file_path = Path(root) / name
            try:
                if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), start=1):
                hit = matcher.search(line) if matcher else (query in line)
                if hit:
                    results.append(f"{rel}:{lineno}: {line.strip()[:240]}")
                    if len(results) >= max_results:
                        break
    if not results:
        return ToolResult(status="ok", output=f"未找到匹配（扫描 {scanned} 个文件）")
    return ToolResult(status="ok", output=f"找到 {len(results)} 条匹配（扫描 {scanned} 个文件）:\n" + "\n".join(results))


# ------------------------------------------------------------------ write_artifact
def write_artifact(ctx: ToolContext, path: str, content: str) -> ToolResult:
    if ctx.blobs is None:
        return ToolResult(status="failed", output="制品库未就绪", error_code="storage_error")
    target = pathmod.resolve_within(Path(ctx.artifacts_dir), path)
    if target == Path(ctx.artifacts_dir):
        return ToolResult(status="failed", output="产物路径不能是目录本身", error_code="invalid_input")
    data = content.encode("utf-8")
    digest = ctx.blobs.put_bytes(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_bytes() if target.exists() else None
    if existing is not None and sha256_hex(existing) == digest:
        return ToolResult(
            status="ok",
            output=f"产物内容未变化: {path}",
            artifacts=[{"path": path, "blob_id": digest, "content_hash": digest, "size": len(data)}],
            side_effect_status="none",
        )
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return ToolResult(status="failed", output=f"写入产物失败: {exc}", error_code="storage_error",
                          side_effect_status="unknown")
    return ToolResult(
        status="ok",
        output=f"已写入产物 {path}（{len(data)} B）",
        artifacts=[{"path": path, "blob_id": digest, "content_hash": digest, "size": len(data)}],
        side_effect_status="applied",
    )


# ------------------------------------------------------------------ patch_file
def patch_file(ctx: ToolContext, path: str, find: str, replace: str,
               expected_hash: str | None = None) -> ToolResult:
    """精确文本替换。需 `allow_existing_write`，否则拒绝。"""
    if not ctx.permissions.get("allow_existing_write", False):
        return ToolResult(
            status="denied",
            output="修改既有用户文件需在任务中明确授权（allow_existing_write）",
            error_code="permission_denied",
            side_effect_status="none",
        )
    roots = list(ctx.write_roots)
    target = _pick_root(ctx, path, roots)
    if not target.exists():
        return ToolResult(status="failed", output=f"文件不存在: {path}", error_code="invalid_input")
    if not find:
        return ToolResult(status="failed", output="find 不能为空", error_code="invalid_input")
    current = target.read_text(encoding="utf-8")
    current_hash = sha256_hex(current.encode("utf-8"))
    if expected_hash and expected_hash != current_hash:
        return ToolResult(
            status="failed",
            output=f"文件已被外部修改，拒绝覆盖（当前 hash {current_hash[:12]}）",
            error_code="invalid_state",
            side_effect_status="none",
        )
    if find not in current:
        return ToolResult(status="failed", output=f"未找到待替换内容: {find[:60]}", error_code="invalid_input")
    updated = current.replace(find, replace, 1)
    if ctx.blobs is not None:
        ctx.blobs.put_text(updated)
    backup = target.with_suffix(target.suffix + ".sfbak")
    try:
        backup.write_text(current, encoding="utf-8")
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return ToolResult(status="failed", output=f"写入失败: {exc}", error_code="storage_error",
                          side_effect_status="unknown")
    return ToolResult(
        status="ok",
        output=f"已修改 {path}（备份 {backup.name}，新旧 hash {current_hash[:12]} → {sha256_hex(updated.encode())[:12]}）",
        artifacts=[{"path": path, "blob_id": sha256_hex(updated.encode()), "content_hash": sha256_hex(updated.encode()),
                    "size": len(updated.encode())}],
        side_effect_status="applied",
    )
