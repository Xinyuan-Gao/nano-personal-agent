from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from ..core import paths as core_paths
from ..core.errors import InvalidInput, PermissionDenied
from ..core.ids import package_hash as compute_package_hash
from ..core.ids import sha256_hex

#: 包主文件（ARCHITECTURE §5）。
MAIN_FILE = "SKILL.md"

#: 视为“脚本”的后缀：这些文件必须声明依赖。
SCRIPT_SUFFIXES = (
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".rb",
    ".pl",
    ".ps1",
)

#: 依赖声明可以写在整包级别的清单文件里。
DEPENDENCY_MANIFEST_FILES = (
    "requirements.txt",
    "constraints.txt",
    "pyproject.toml",
    "package.json",
    "Pipfile",
    "environment.yml",
    "environment.yaml",
    "Gemfile",
    "go.mod",
)

#: 脚本内联依赖声明：``# requires: requests==2.31`` / ``# 依赖: none``。
_DEP_MARKER_RE = re.compile(
    r"(?im)^[ \t]*(?:#|//|--|;|REM[ \t])[ \t]*"
    r"(?:requires|requirement|deps|dependencies|依赖)[ \t]*[:：][ \t]*(.+?)[ \t]*$"
)

#: Markdown 链接 / 图片：``[文字](相对路径)``。
_REF_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

#: metadata 中出现这些键时提示：未知字段不授予能力。
_CAPABILITY_KEYS = (
    "allowed_tools",
    "tools",
    "permissions",
    "capabilities",
    "mcp",
    "network",
    "shell",
    "exec",
)

_POSITIVE_DIRECTIVES = ("必须", "务必", "一定要", "总是", "永远", "always", "must", "required")
_NEGATIVE_DIRECTIVES = (
    "不要",
    "禁止",
    "不得",
    "不能",
    "never",
    "do not",
    "don't",
    "must not",
    "avoid",
)
_APPLICABILITY_KEYWORDS = ("适用", "何时", "使用条件", "前提", "场景", "when to use", "applies")
_VERIFICATION_KEYWORDS = ("验证", "校验", "检查", "测试", "验收", "verify", "check", "test")


# --------------------------------------------------------------------------- 数据结构
@dataclass
class PackageLimits:
    """导入上限：默认 200 文件 / 单文件 2 MB / 总量 20 MB。"""

    max_files: int = 200
    max_file_bytes: int = 2_000_000
    max_total_bytes: int = 20_000_000

    def to_dict(self) -> dict:
        return {
            "max_files": self.max_files,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
        }

    @classmethod
    def from_dict(cls, data: Mapping | None) -> "PackageLimits":
        data = dict(data or {})
        allowed = {"max_files", "max_file_bytes", "max_total_bytes"}
        return cls(**{k: int(v) for k, v in data.items() if k in allowed})


@dataclass
class FileEntry:
    """清单条目：路径 + 内容 hash + 大小（+ 制品 id 与可执行位）。"""

    path: str
    content_hash: str
    size: int
    blob_id: str = ""
    executable: bool = False

    def __post_init__(self) -> None:
        if not self.blob_id:
            # 制品库是内容寻址的，blob_id 与内容 hash 同值。
            self.blob_id = self.content_hash

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "content_hash": self.content_hash,
            "sha256": self.content_hash,
            "size": int(self.size),
            "blob_id": self.blob_id,
            "executable": bool(self.executable),
        }


@dataclass
class CheckFinding:
    """检查发现：level ∈ hard | hint | risk | skipped。"""

    level: str
    code: str
    message: str
    locator: str | None = None

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "locator": self.locator,
        }


@dataclass
class SkillPackage:
    """解析后的完整包（内存表示）。"""

    root: Path
    manifest: list[FileEntry]
    package_hash: str
    name: str
    description: str
    metadata: dict
    body: str
    findings: list[CheckFinding]
    total_bytes: int
    lead_file: str = MAIN_FILE
    # --- 向后兼容的扩展字段（ARCHITECTURE §5 未列出，均为加法扩展） ---
    references: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    raw_files: dict[str, bytes] | None = None
    limits: PackageLimits | None = None
    parse_findings: list[CheckFinding] = field(default_factory=list)

    # ---------------------------------------------------------------- 便捷方法
    def paths(self) -> set[str]:
        return {entry.path for entry in self.manifest}

    def entries_by_path(self) -> dict[str, FileEntry]:
        return {entry.path: entry for entry in self.manifest}

    def hard_findings(self) -> list[CheckFinding]:
        return [f for f in self.findings if f.level == "hard"]

    def manifest_dicts(self) -> list[dict]:
        return [entry.to_dict() for entry in self.manifest]


# --------------------------------------------------------------------------- frontmatter
_FRONTMATTER_OPEN_RE = re.compile(r"^---[ \t]*\r?\n")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 ``---`` 包裹的 YAML 子集 frontmatter，返回 ``(metadata, body)``。

    支持：``key: value``、``- item`` 列表、一层嵌套 dict、内联 ``[a, b]`` / ``{k: v}``、
    引号字符串、``#`` 注释行、``true/false``、整数与浮点。未知字段原样保留。
    """
    if not text.startswith("---"):
        return {}, text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = _FRONTMATTER_OPEN_RE.match(normalized)
    if match is None:
        return {}, text
    lines = normalized[match.end() :].split("\n")
    for index, line in enumerate(lines):
        if line.strip() in ("---", "..."):
            metadata = _parse_yaml_subset("\n".join(lines[:index]))
            body = "\n".join(lines[index + 1 :])
            return metadata, body.lstrip("\n")
    raise InvalidInput(
        "SKILL.md frontmatter 未闭合", field_errors={"frontmatter": "unterminated"}
    )


def _parse_yaml_subset(text: str) -> dict:
    result: dict = {}
    current_key: str | None = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped.startswith("- "):
            if current_key is None:
                raise InvalidInput(
                    f"frontmatter 列表项缺少父键: {stripped}",
                    field_errors={"frontmatter": "orphan_list_item"},
                )
            bucket = result.get(current_key)
            if isinstance(bucket, dict) and not bucket:
                bucket = []
                result[current_key] = bucket
            if not isinstance(bucket, list):
                raise InvalidInput(
                    f"frontmatter 键 {current_key} 同时是标量与列表",
                    field_errors={"frontmatter": "type_conflict"},
                )
            bucket.append(_parse_scalar(stripped[2:].strip()))
            continue
        if ":" not in stripped:
            raise InvalidInput(
                f"frontmatter 行无法解析: {stripped}",
                field_errors={"frontmatter": "unparsable_line"},
            )
        key, _, value = stripped.partition(":")
        key = key.strip().strip("\"'")
        value = value.strip()
        if not key:
            raise InvalidInput(
                f"frontmatter 键名为空: {stripped}", field_errors={"frontmatter": "empty_key"}
            )
        if indent == 0:
            if value == "":
                result[key] = {}
                current_key = key
            else:
                result[key] = _parse_scalar(value)
                current_key = None
            continue
        if current_key is None or not isinstance(result.get(current_key), dict):
            raise InvalidInput(
                f"frontmatter 嵌套层级过深: {stripped}",
                field_errors={"frontmatter": "too_deep"},
            )
        result[current_key][key] = _parse_scalar(value) if value else {}
    return result


def _parse_scalar(value: str):
    if value == "":
        return ""
    if value in ("~", "null", "Null", "NULL"):
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in _split_inline(inner)]
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        if not inner:
            return {}
        out: dict = {}
        for part in _split_inline(inner):
            k, _, v = part.partition(":")
            out[k.strip().strip("\"'")] = _parse_scalar(v.strip())
        return out
    if re.fullmatch(r"[+-]?\d+", value):
        try:
            return int(value)
        except ValueError:  # pragma: no cover
            return value
    if re.fullmatch(r"[+-]?\d+\.\d+", value):
        try:
            return float(value)
        except ValueError:  # pragma: no cover
            return value
    return value


def _split_inline(text: str) -> list[str]:
    parts, buf, quote = [], [], None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p for p in (p.strip() for p in parts) if p]


# --------------------------------------------------------------------------- 路径与读取
def _safe_rel(rel: str) -> str:
    """校验包内相对路径，返回规范化的 posix 路径。"""
    raw = str(rel).replace("\\", "/")
    pure = PurePosixPath(raw)
    if not raw or pure.is_absolute() or _DRIVE_RE.match(raw) or ".." in pure.parts:
        raise PermissionDenied(
            f"包内路径不合法: {rel}", field_errors={"path": "invalid_package_path"}
        )
    normalized = "/".join(part for part in pure.parts if part not in ("", "."))
    if not normalized:
        raise InvalidInput(f"包内路径为空: {rel}", field_errors={"path": "empty"})
    return normalized


def entry_bytes(pkg: SkillPackage, entry: FileEntry) -> bytes:
    if pkg.raw_files is not None and entry.path in pkg.raw_files:
        return pkg.raw_files[entry.path]
    return (Path(pkg.root) / entry.path).read_bytes()


def iter_text_entries(pkg: SkillPackage):
    """产出 ``(entry, text)``：可 UTF-8 解码且非二进制的文件。"""
    for entry in pkg.manifest:
        try:
            data = entry_bytes(pkg, entry)
        except OSError:  # pragma: no cover - 文件被并发删除
            continue
        if b"\x00" in data[:4096]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        yield entry, text


def read_package(
    root: Path | str,
    *,
    limits: PackageLimits | None = None,
    allow_absolute_refs: bool = False,
) -> SkillPackage:
    """读取目录中的 skill 包（只读，不执行任何脚本）。

    - 跳过 ``.git`` / 缓存 / 依赖目录，并把跳过项记入 ``findings(level=skipped)``。
    - 绝对路径、越界引用、外部 symlink 一律拒绝。
    - 超过文件数 / 单文件 / 总量上限直接报错，不产生部分导入。
    """
    limits = limits or PackageLimits()
    root_path = core_paths.normalize(root)
    if not root_path.exists() or not root_path.is_dir():
        raise InvalidInput(f"来源目录不存在: {root_path}", field_errors={"source": "not_found"})

    lead = root_path / MAIN_FILE
    if not lead.is_file():
        raise InvalidInput(
            f"包内缺少 {MAIN_FILE}", field_errors={"SKILL.md": "missing"}
        )

    skipped: list[str] = []
    entries: list[FileEntry] = []
    raw_files: dict[str, bytes] = {}
    total = 0

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in sorted(dirnames):
            full = current / name
            rel = os.path.relpath(str(full), str(root_path)).replace(os.sep, "/")
            if name in core_paths.IGNORED_DIRS or core_paths.is_ignored_path(rel + "/x"):
                skipped.append(rel + "/")
                continue
            if full.is_symlink() and not core_paths.is_within(root_path, full.resolve()):
                raise PermissionDenied(
                    f"外部符号链接目录被拒绝: {rel} -> {full.resolve()}",
                    field_errors={"path": "symlink_escape"},
                )
            kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            full = current / name
            rel = os.path.relpath(str(full), str(root_path)).replace(os.sep, "/")
            if core_paths.is_ignored_path(rel):
                skipped.append(rel)
                continue
            if full.is_symlink():
                resolved = full.resolve()
                if not core_paths.is_within(root_path, resolved):
                    raise PermissionDenied(
                        f"外部符号链接被拒绝: {rel} -> {resolved}",
                        field_errors={"path": "symlink_escape"},
                    )
            if not full.is_file():
                skipped.append(rel)
                continue
            size = full.stat().st_size
            if size > limits.max_file_bytes:
                raise InvalidInput(
                    f"单文件超过上限 {limits.max_file_bytes} 字节: {rel}（{size} 字节）",
                    field_errors={"file_size": "exceeded"},
                )
            if len(entries) + 1 > limits.max_files:
                raise InvalidInput(
                    f"文件数超过上限 {limits.max_files}（已含 {rel}）",
                    field_errors={"file_count": "exceeded"},
                )
            data = full.read_bytes()
            total += size
            if total > limits.max_total_bytes:
                raise InvalidInput(
                    f"包总大小超过上限 {limits.max_total_bytes} 字节",
                    field_errors={"total_size": "exceeded"},
                )
            digest = sha256_hex(data)
            try:
                mode = full.stat().st_mode
            except OSError:  # pragma: no cover
                mode = 0
            entries.append(
                FileEntry(
                    path=rel,
                    content_hash=digest,
                    size=size,
                    blob_id=digest,
                    executable=bool(mode & 0o111),
                )
            )
            raw_files[rel] = data

    skipped = sorted(set(skipped))
    package = _assemble(
        root=root_path,
        entries=entries,
        raw_files=raw_files,
        total_bytes=total,
        lead_file=MAIN_FILE,
        limits=limits,
        allow_absolute_refs=allow_absolute_refs,
        skipped=skipped,
    )
    return package


def build_package(
    files: Mapping[str, bytes | str],
    *,
    lead_file: str = MAIN_FILE,
    limits: PackageLimits | None = None,
    allow_absolute_refs: bool = False,
    root: Path | str | None = None,
) -> SkillPackage:
    """由内存文件映射构造完整包（草稿 / 候选 / 导出再导入使用）。

    与 :func:`read_package` 使用同一套清单、hash 与检查逻辑，所以两条路径产出的
    ``package_hash`` 可直接比对。
    """
    limits = limits or PackageLimits()
    normalized: dict[str, bytes] = {}
    for rel, content in files.items():
        path = _safe_rel(rel)
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        normalized[path] = data
    if not normalized:
        raise InvalidInput("包内没有任何文件", field_errors={"manifest": "empty"})
    if lead_file not in normalized:
        raise InvalidInput(
            f"包内缺少 {lead_file}", field_errors={lead_file: "missing"}
        )
    if len(normalized) > limits.max_files:
        raise InvalidInput(
            f"文件数超过上限 {limits.max_files}", field_errors={"file_count": "exceeded"}
        )
    entries: list[FileEntry] = []
    total = 0
    for path in sorted(normalized):
        data = normalized[path]
        size = len(data)
        if size > limits.max_file_bytes:
            raise InvalidInput(
                f"单文件超过上限 {limits.max_file_bytes} 字节: {path}",
                field_errors={"file_size": "exceeded"},
            )
        total += size
        if total > limits.max_total_bytes:
            raise InvalidInput(
                f"包总大小超过上限 {limits.max_total_bytes} 字节",
                field_errors={"total_size": "exceeded"},
            )
        digest = sha256_hex(data)
        entries.append(
            FileEntry(path=path, content_hash=digest, size=size, blob_id=digest, executable=False)
        )
    return _assemble(
        root=Path(root) if root is not None else Path("."),
        entries=entries,
        raw_files=normalized,
        total_bytes=total,
        lead_file=lead_file,
        limits=limits,
        allow_absolute_refs=allow_absolute_refs,
        skipped=[],
    )


def _assemble(
    *,
    root: Path,
    entries: list[FileEntry],
    raw_files: dict[str, bytes],
    total_bytes: int,
    lead_file: str,
    limits: PackageLimits,
    allow_absolute_refs: bool,
    skipped: list[str],
) -> SkillPackage:
    entries = sorted(entries, key=lambda e: e.path)
    manifest_hash = compute_package_hash([entry.to_dict() for entry in entries])

    parse_findings: list[CheckFinding] = []
    lead_data = raw_files.get(lead_file, b"")
    try:
        lead_text = lead_data.decode("utf-8")
    except UnicodeDecodeError:
        lead_text = lead_data.decode("utf-8", errors="replace")
        parse_findings.append(
            CheckFinding(
                "hard",
                "not_utf8",
                f"{lead_file} 不是合法 UTF-8 文本",
                lead_file,
            )
        )
    try:
        metadata, body = parse_frontmatter(lead_text)
    except InvalidInput as exc:
        metadata, body = {}, lead_text
        parse_findings.append(
            CheckFinding("hard", "frontmatter_unparsable", f"frontmatter 无法解析: {exc.message}", lead_file)
        )
    if not isinstance(metadata, dict):  # pragma: no cover - parse 保证了 dict
        metadata = {}

    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()

    available = {entry.path for entry in entries}
    references, ref_findings = extract_references(
        body,
        available,
        allow_absolute_refs=allow_absolute_refs,
        locator_prefix=lead_file,
    )
    parse_findings.extend(ref_findings)

    for rel in skipped:
        parse_findings.append(
            CheckFinding("skipped", "skipped_path", f"已跳过（不进入清单）: {rel}", rel)
        )

    package = SkillPackage(
        root=root,
        manifest=entries,
        package_hash=manifest_hash,
        name=name,
        description=description,
        metadata=metadata,
        body=body,
        findings=[],
        total_bytes=total_bytes,
        lead_file=lead_file,
        references=references,
        skipped=skipped,
        raw_files=raw_files,
        limits=limits,
        parse_findings=parse_findings,
    )
    package.findings = hard_checks(package)
    return package


# --------------------------------------------------------------------------- 引用
def _resolve_relative(target: str) -> str | None:
    parts: list[str] = []
    for part in PurePosixPath(target).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) if parts else None


def extract_references(
    body: str,
    available: set[str],
    *,
    allow_absolute_refs: bool = False,
    locator_prefix: str = MAIN_FILE,
) -> tuple[list[str], list[CheckFinding]]:
    """从正文抽取相对引用（Markdown 链接 / 图片），并校验完整性（PRP-03）。

    外部 URL（``http:`` / ``mailto:`` / 锚点）不算引用；内联代码里的路径**不**当作引用
    避免把正文中的普通路径文字误判成缺引用。
    """
    level = "hint" if allow_absolute_refs else "hard"
    references: list[str] = []
    findings: list[CheckFinding] = []
    seen: set[str] = set()
    for lineno, line in enumerate(body.splitlines(), start=1):
        for match in _REF_LINK_RE.finditer(line):
            target = match.group(1).strip()
            locator = f"{locator_prefix}:{lineno}"
            if not target or target.startswith("#") or _SCHEME_RE.match(target):
                continue
            if target.startswith("/") or _DRIVE_RE.match(target):
                findings.append(
                    CheckFinding(
                        level,
                        "reference_absolute_path",
                        f"引用使用绝对路径被拒绝: {target}",
                        locator,
                    )
                )
                continue
            resolved = _resolve_relative(target.split("#", 1)[0].split("?", 1)[0])
            if resolved is None:
                findings.append(
                    CheckFinding(
                        level,
                        "reference_out_of_bounds",
                        f"引用越出包范围被拒绝: {target}",
                        locator,
                    )
                )
                continue
            if resolved not in available:
                findings.append(
                    CheckFinding(
                        "hard",
                        "reference_missing",
                        f"引用文件不存在: {resolved}",
                        locator,
                    )
                )
                continue
            if resolved not in seen:
                seen.add(resolved)
                references.append(resolved)
    return references, findings


# --------------------------------------------------------------------------- 硬检查
def hard_checks(pkg: SkillPackage, *, baseline: SkillPackage | None = None) -> list[CheckFinding]:
    """硬检查：失败即阻断导入 / 评测 / 启用。

    覆盖：格式可解析、身份完整且不被静默改变、引用完整、路径合法、无明文凭据
    脚本依赖声明完整、包大小合规。
    """
    limits = pkg.limits or PackageLimits()
    findings: list[CheckFinding] = list(pkg.parse_findings)
    paths = pkg.paths()

    # --- 包结构 ---------------------------------------------------------
    if pkg.lead_file not in paths:
        findings.append(
            CheckFinding("hard", "missing_lead_file", f"包内缺少主文件 {pkg.lead_file}", pkg.lead_file)
        )
    for entry in pkg.manifest:
        raw = str(entry.path)
        pure = PurePosixPath(raw)
        if pure.is_absolute() or _DRIVE_RE.match(raw) or ".." in pure.parts or "\\" in raw:
            findings.append(
                CheckFinding("hard", "manifest_path_invalid", f"清单路径不合法: {raw}", raw)
            )

    # --- 身份 ---------------------------------
    if not pkg.name.strip():
        findings.append(
            CheckFinding("hard", "identity_missing", f"{pkg.lead_file} 缺少 name", pkg.lead_file)
        )
    if not pkg.description.strip():
        findings.append(
            CheckFinding(
                "hint",
                "description_missing",
                f"{pkg.lead_file} 缺少 description，技能索引将缺少用途说明",
                pkg.lead_file,
            )
        )
    if baseline is not None and baseline.name.strip() and pkg.name.strip() != baseline.name.strip():
        findings.append(
            CheckFinding(
                "hard",
                "identity_changed",
                f"身份被静默改变: {baseline.name!r} → {pkg.name!r}",
                f"{pkg.lead_file}:name",
            )
        )

    # --- 引用完整 -------------------------------------------------------
    references, ref_findings = extract_references(
        pkg.body, paths, allow_absolute_refs=False, locator_prefix=pkg.lead_file
    )
    findings.extend(ref_findings)
    if pkg.metadata:
        declared_refs = pkg.metadata.get("references")
        candidates = declared_refs if isinstance(declared_refs, list) else []
        for item in candidates:
            rel = _resolve_relative(str(item))
            if rel and rel not in paths:
                findings.append(
                    CheckFinding(
                        "hard",
                        "reference_missing",
                        f"metadata.references 声明的文件不存在: {rel}",
                        f"{pkg.lead_file}:metadata.references",
                    )
                )

    # --- 路径合法（不允许越界引用）--------------------------------------
    for rel in pkg.references:
        try:
            _safe_rel(rel)
        except (PermissionDenied, InvalidInput) as exc:
            findings.append(CheckFinding("hard", "reference_invalid", str(exc), rel))

    # --- 明文凭据 -----------------------------------------
    for entry, text in iter_text_entries(pkg):
        for secret in core_paths.scan_secrets(text):
            findings.append(
                CheckFinding(
                    "hard",
                    "secret_detected",
                    f"疑似明文凭据（{secret['kind']}）: {secret['excerpt']}",
                    f"{entry.path}:{secret['line']}",
                )
            )

    # --- 脚本依赖声明完整 ----------------------------------
    global_declared = _global_dependency_declaration(pkg)
    has_manifest_file = any(entry.path in DEPENDENCY_MANIFEST_FILES for entry in pkg.manifest)
    for entry in pkg.manifest:
        if not entry.path.endswith(SCRIPT_SUFFIXES):
            continue
        if global_declared is not None or has_manifest_file:
            continue
        try:
            text = entry_bytes(pkg, entry).decode("utf-8", errors="replace")
        except OSError:  # pragma: no cover
            text = ""
        if _DEP_MARKER_RE.search(text):
            continue
        findings.append(
            CheckFinding(
                "hard",
                "script_dependency_undeclared",
                f"脚本未声明依赖（可在脚本内写 `# requires: ...`，或在 metadata.dependencies / requirements.txt 中声明）: {entry.path}",
                entry.path,
            )
        )

    # --- 大小合规 -----------------------------------------
    if len(pkg.manifest) > limits.max_files:
        findings.append(
            CheckFinding("hard", "too_many_files", f"文件数 {len(pkg.manifest)} 超过上限 {limits.max_files}", None)
        )
    for entry in pkg.manifest:
        if entry.size > limits.max_file_bytes:
            findings.append(
                CheckFinding(
                    "hard",
                    "file_too_large",
                    f"单文件 {entry.size} 字节超过上限 {limits.max_file_bytes}",
                    entry.path,
                )
            )
    if pkg.total_bytes > limits.max_total_bytes:
        findings.append(
            CheckFinding(
                "hard",
                "package_too_large",
                f"包总大小 {pkg.total_bytes} 字节超过上限 {limits.max_total_bytes}",
                None,
            )
        )

    # --- 清单 hash 自洽 --------------------------------------------------
    expected = compute_package_hash([entry.to_dict() for entry in pkg.manifest])
    if expected != pkg.package_hash:
        findings.append(
            CheckFinding("hard", "package_hash_mismatch", "package_hash 与文件清单不一致", None)
        )

    # --- metadata 不授予能力（提示级） ------------------------
    capability_keys = [k for k in pkg.metadata if str(k).lower() in _CAPABILITY_KEYS]
    if capability_keys:
        findings.append(
            CheckFinding(
                "hint",
                "metadata_not_authoritative",
                "metadata 中的能力声明不授予权限，未知字段仅原样保留: " + ", ".join(map(str, capability_keys)),
                f"{pkg.lead_file}:metadata",
            )
        )
    return _dedupe(findings)


def _global_dependency_declaration(pkg: SkillPackage) -> object | None:
    for key in ("dependencies", "requires", "deps", "依赖"):
        if key in pkg.metadata:
            return pkg.metadata[key]
    return None


def _dedupe(findings: Iterable[CheckFinding]) -> list[CheckFinding]:
    out: list[CheckFinding] = []
    seen: set[tuple] = set()
    for item in findings:
        key = (item.level, item.code, item.message, item.locator)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# --------------------------------------------------------------------------- 质量提示
def quality_hints(pkg: SkillPackage, *, baseline: SkillPackage | None = None) -> list[CheckFinding]:
    """质量提示：重复、矛盾、过泛规则、缺适用条件、缺验证方式、正文膨胀。

    提示不阻断流程；正文增长超 20% 或 diff 超 200 行时，额外要求人工确认整体重写意图。
    """
    findings: list[CheckFinding] = []
    body = pkg.body or ""

    # --- 重复 -----------------------------------------------------------
    counts: dict[str, int] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if len(stripped) < 24 or stripped[0] in "#|-*>1234567890`":
            continue
        counts[stripped] = counts.get(stripped, 0) + 1
    for line, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if count >= 2:
            findings.append(
                CheckFinding(
                    "hint",
                    "duplicate_rule",
                    f"疑似重复规则（出现 {count} 次）: {line[:40]}",
                    pkg.lead_file,
                )
            )
        if len([f for f in findings if f.code == "duplicate_rule"]) >= 5:
            break

    # --- 矛盾 -----------------------------------------------------------
    for conflict in find_contradictions([body]):
        findings.append(
            CheckFinding(
                "hint",
                "contradictory_rule",
                f"疑似矛盾规则（同一对象正反指令）: {conflict['subject']}",
                pkg.lead_file,
            )
        )

    # --- 过泛规则 -------------------------------------------------------
    overbroad = 0
    for raw in re.split(r"[\n。；;！!？?]+", body):
        sentence = raw.strip()
        if len(sentence) < 4:
            continue
        low = sentence.lower()
        if not any(kw in low for kw in ("总是", "永远", "所有", "任何", "always", "never", "every")):
            continue
        if any(kw in low for kw in _APPLICABILITY_KEYWORDS) or any(
            kw in low for kw in ("如果", "当", "除非", "if ", "unless", "when ")
        ):
            continue
        overbroad += 1
        if overbroad <= 3:
            findings.append(
                CheckFinding("hint", "overbroad_rule", f"过泛规则缺少适用条件: {sentence[:40]}", pkg.lead_file)
            )

    # --- 缺适用条件 / 缺验证方式 -----------------------------------------
    low_body = body.lower()
    if not any(kw in low_body for kw in _APPLICABILITY_KEYWORDS):
        findings.append(
            CheckFinding("hint", "missing_applicability", "正文缺少适用条件说明（何时用 / 何时不用）", pkg.lead_file)
        )
    if not any(kw in low_body for kw in _VERIFICATION_KEYWORDS):
        findings.append(
            CheckFinding("hint", "missing_verification", "正文缺少验证方式说明（如何确认结果正确）", pkg.lead_file)
        )

    # --- 正文膨胀 / 大改人工确认 ----------------------------
    if baseline is not None:
        old_len = len(baseline.body or "")
        new_len = len(body)
        if old_len > 0 and new_len > old_len * 1.2:
            growth = (new_len - old_len) / old_len * 100
            findings.append(
                CheckFinding(
                    "hint",
                    "body_growth_review",
                    f"正文增长 {growth:.0f}%（>20%），需要人工确认是否为整体重写意图",
                    pkg.lead_file,
                )
            )
        diff_lines = [
            line
            for line in difflib.unified_diff(
                (baseline.body or "").splitlines(),
                body.splitlines(),
                lineterm="",
                n=0,
            )
            if line[:1] in "+-" and line[:3] not in ("+++", "---")
        ]
        if len(diff_lines) > 200:
            findings.append(
                CheckFinding(
                    "hint",
                    "diff_large_review",
                    f"SKILL.md 差异 {len(diff_lines)} 行（>200），需要人工确认整体重写意图",
                    pkg.lead_file,
                )
            )
        findings.extend(_capability_change_hints(baseline, pkg))
    return _dedupe(findings)


def _capability_change_hints(baseline: SkillPackage, pkg: SkillPackage) -> list[CheckFinding]:
    """能力变化提示：新增脚本 / 可执行位 / 网络需求必须显式展示。"""
    old = baseline.entries_by_path()
    new = pkg.entries_by_path()
    added_scripts = sorted(
        path
        for path in new
        if path not in old and path.endswith(SCRIPT_SUFFIXES)
    )
    newly_executable = sorted(
        path
        for path in new
        if path in old and new[path].executable and not old[path].executable
    )
    network_mentions = 0
    for entry, text in iter_text_entries(pkg):
        network_mentions += len(re.findall(r"(?i)\b(?:http|https|curl|wget|requests|urllib)\b", text))
    findings: list[CheckFinding] = []
    if added_scripts or newly_executable:
        findings.append(
            CheckFinding(
                "risk",
                "capability_change",
                "能力变化：新增脚本 {scripts}；新增可执行位 {execs}，需另行授权".format(
                    scripts=added_scripts or "无", execs=newly_executable or "无"
                ),
                None,
            )
        )
    if network_mentions and not any(
        re.search(r"(?i)\b(?:http|https|curl|wget)\b", text) for _e, text in iter_text_entries(baseline)
    ):
        findings.append(
            CheckFinding("risk", "capability_change", "能力变化：正文新增网络访问需求，需另行授权", pkg.lead_file)
        )
    return findings


# --------------------------------------------------------------------------- 差异
def diff_manifests(a: list[FileEntry], b: list[FileEntry]) -> list[dict]:
    """逐文件差异：added / removed / modified / unchanged。"""
    old = {entry.path: entry for entry in a}
    new = {entry.path: entry for entry in b}
    out: list[dict] = []
    for path in sorted(set(old) | set(new)):
        before = old.get(path)
        after = new.get(path)
        if before is None:
            change = "added"
        elif after is None:
            change = "removed"
        elif before.content_hash != after.content_hash:
            change = "modified"
        else:
            change = "unchanged"
        out.append(
            {
                "path": path,
                "change": change,
                "old_hash": before.content_hash if before else None,
                "new_hash": after.content_hash if after else None,
            }
        )
    return out


# --------------------------------------------------------------------------- 矛盾检测
def _polarity(sentence: str) -> int:
    low = sentence.lower()
    for keyword in _NEGATIVE_DIRECTIVES:
        if keyword in low:
            return -1
    for keyword in _POSITIVE_DIRECTIVES:
        if keyword in low:
            return 1
    return 0


def _subject(sentence: str) -> str:
    text = re.sub(r"^[\s\-*\d.、)）]+", "", sentence)
    for keyword in list(_POSITIVE_DIRECTIVES) + list(_NEGATIVE_DIRECTIVES):
        text = re.sub(re.escape(keyword), " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s，,。：:；;、]+", "", text)
    return text[:12]


def find_contradictions(texts: list[str]) -> list[dict]:
    """找出同一对象上的正反指令（矛盾、 不得暗中合并）。

    返回 ``[{"subject","index_a","index_b","positive","negative"}]``。
    """
    per_text: list[dict[str, list[tuple[int, str]]]] = []
    for text in texts:
        directives: dict[str, list[tuple[int, str]]] = {}
        for raw in re.split(r"[\n。；;！!？?]+", text or ""):
            sentence = raw.strip()
            if len(sentence) < 3:
                continue
            polarity = _polarity(sentence)
            if polarity == 0:
                continue
            subject = _subject(sentence)
            if not subject:
                continue
            directives.setdefault(subject, []).append((polarity, sentence))
        per_text.append(directives)

    conflicts: list[dict] = []
    for i in range(len(per_text)):
        # 同一文本内部的互斥指令
        for subject, entries in per_text[i].items():
            positives = [s for p, s in entries if p > 0]
            negatives = [s for p, s in entries if p < 0]
            if positives and negatives:
                conflicts.append(
                    {
                        "subject": subject,
                        "index_a": i,
                        "index_b": i,
                        "positive": positives[0][:60],
                        "negative": negatives[0][:60],
                    }
                )
        # 跨文本的互斥指令
        for j in range(i + 1, len(per_text)):
            for subject, entries in per_text[i].items():
                if subject not in per_text[j]:
                    continue
                positives = [s for p, s in entries if p > 0]
                negatives = [s for p, s in per_text[j][subject] if p < 0]
                if positives and negatives:
                    conflicts.append(
                        {
                            "subject": subject,
                            "index_a": i,
                            "index_b": j,
                            "positive": positives[0][:60],
                            "negative": negatives[0][:60],
                        }
                    )
                positives = [s for p, s in per_text[j][subject] if p > 0]
                negatives = [s for p, s in entries if p < 0]
                if positives and negatives:
                    conflicts.append(
                        {
                            "subject": subject,
                            "index_a": j,
                            "index_b": i,
                            "positive": positives[0][:60],
                            "negative": negatives[0][:60],
                        }
                    )
    return conflicts
