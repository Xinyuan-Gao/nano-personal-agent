from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core.clock import utc_now
from ..core.errors import InvalidInput
from ..core.ids import canonical_hash, sha256_hex

# --------------------------------------------------------------------------- 常量
LEVEL_HARD = "hard"
LEVEL_HINT = "hint"
LEVEL_RISK = "risk"
LEVEL_SKIPPED = "skipped"

#: 判定“需要人工确认整体重写意图”的质量提示码
REWRITE_REVIEW_CODES = frozenset({"body_growth_review", "diff_large_review"})

#: 生成材料中一律不得出现的字段（预期答案 / 评分标准 / 隐藏样例）
FORBIDDEN_MATERIAL_KEYS = frozenset(
    {
        "expected_answers",
        "expected_answer",
        "expectedanswers",
        "answer_key",
        "answer_keys",
        "answers",
        "gold",
        "golden",
        "holdout_cases",
        "holdout",
        "rubric",
        "rubrics",
        "critical_checks",
        "thresholds",
        "scoring",
    }
)

#: 这些 role 的评测样例是隐藏样例，候选生成阶段不可见
HOLDOUT_ROLES = frozenset({"holdout", "hidden", "reserved", "secret"})

#: 视为脚本的后缀（与 skills.package.SCRIPT_SUFFIXES 对齐）
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

_NETWORK_RE = re.compile(
    r"(?i)(https?://|\bcurl\b|\bwget\b|\brequests\b|\burllib\b|需要网络|联网|访问网络|network\s*[:=]\s*true)"
)
_COMMAND_RE = re.compile(
    r"(?i)(\brun_command\b|\bbash\b|\bsh\s+-c\b|\bsubprocess\b|os\.system|\bshell\b|执行命令|执行脚本)"
)
#: 正文里试图用文字扩大权限的话术：只记录、不授予
_ESCALATION_RE = re.compile(
    r"(?i)(忽略限制|忽略权限|忽略上述限制|无需授权|不需要授权|无需确认|绕过限制|绕过权限|越权|"
    r"ignore\s+(?:the\s+)?(?:restrictions?|permissions?|limits?)|bypass\s+(?:the\s+)?(?:restrictions?|permissions?))"
)


# --------------------------------------------------------------------------- 包 API
def package_module():
    """返回 ``skillforge.skills.package`` 模块（冻结接口，直接复用其硬检查与提示）。"""
    from ..skills import package as module

    return module


def read_package(root: Path | str, **kwargs):
    return package_module().read_package(Path(root), **kwargs)


def build_package(files: Mapping[str, bytes | str], **kwargs):
    return package_module().build_package(files, **kwargs)


def diff_manifests(before, after) -> list[dict]:
    return package_module().diff_manifests(before, after)


def finding_dicts(findings: Iterable[Any]) -> list[dict]:
    out: list[dict] = []
    for item in findings:
        out.append(item.to_dict() if hasattr(item, "to_dict") else dict(item))
    return out


# --------------------------------------------------------------------------- 包内容
def package_files(pkg) -> dict[str, bytes]:
    """把内存中的包还原成 ``{相对路径: 内容字节}``（候选编辑与 diff 使用）。"""
    module = package_module()
    files: dict[str, bytes] = {}
    for entry in pkg.manifest:
        try:
            files[entry.path] = module.entry_bytes(pkg, entry)
        except OSError as exc:  # pragma: no cover - 文件被并发删除
            raise InvalidInput(
                f"读取基线包文件失败: {entry.path}（{exc}）",
                field_errors={"path": "unreadable"},
            )
    return files


def files_from_version(store, blobs, version_id: str) -> dict[str, bytes]:
    """从 ``skill_version_file`` + blob 库重建完整包内容（不依赖 skills 服务实例）。"""
    rows = store.query(
        "SELECT path, blob_id FROM skill_version_file WHERE version_id = ? ORDER BY path ASC",
        (version_id,),
    )
    files: dict[str, bytes] = {}
    for row in rows:
        if not blobs.exists(row["blob_id"]):
            from ..core.errors import EvidenceUnavailable

            raise EvidenceUnavailable(
                f"文件内容已不可用: {row['path']}（blob {row['blob_id']}）",
                field_errors={"blob_id": "missing"},
            )
        files[row["path"]] = blobs.get_bytes(row["blob_id"])
    return files


def package_from_files(files: Mapping[str, bytes | str], *, root: Path | str | None = None):
    return build_package(dict(files), root=root)


# --------------------------------------------------------------------------- 差异
def _unified_diff(old_text: str, new_text: str, *, max_chars: int = 20_000) -> tuple[str, bool]:
    diff = "\n".join(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="baseline",
            tofile="candidate",
            lineterm="",
            n=3,
        )
    )
    if len(diff) > max_chars:
        return diff[:max_chars] + "\n…（差异过长已截断）", True
    return diff, False


def diff_report(baseline, candidate, *, max_chars: int = 20_000) -> list[dict]:
    """逐文件差异 + 行级统计。旧/新内容按需截断保留，便于界面展示。"""
    old_files = package_files(baseline)
    new_files = package_files(candidate)
    manifest_diff = {item["path"]: item for item in diff_manifests(baseline.manifest, candidate.manifest)}
    report: list[dict] = []
    for path in sorted(set(old_files) | set(new_files)):
        item = dict(manifest_diff.get(path) or {"path": path, "change": "modified"})
        old_bytes = old_files.get(path)
        new_bytes = new_files.get(path)
        old_text = _decode(old_bytes)
        new_text = _decode(new_bytes)
        unified = ""
        truncated = False
        added = removed = 0
        if old_text is not None and new_text is not None:
            unified, truncated = _unified_diff(old_text, new_text, max_chars=max_chars)
            for line in unified.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    removed += 1
        report.append(
            {
                "path": path,
                "change": item.get("change") or "modified",
                "old_hash": item.get("old_hash"),
                "new_hash": item.get("new_hash"),
                "old_size": len(old_bytes) if old_bytes is not None else 0,
                "new_size": len(new_bytes) if new_bytes is not None else 0,
                "added_lines": added,
                "removed_lines": removed,
                "binary": old_text is None or new_text is None,
                "unified_diff": unified,
                "diff_truncated": truncated,
            }
        )
    return report


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def body_diff_stats(baseline, candidate) -> dict:
    """SKILL.md 正文差异统计（供大改确认与界面展示）。"""
    old_body = baseline.body or ""
    new_body = candidate.body or ""
    diff_lines = [
        line
        for line in difflib.unified_diff(old_body.splitlines(), new_body.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    old_len = len(old_body)
    growth_ratio = ((len(new_body) - old_len) / old_len) if old_len else 0.0
    return {
        "old_chars": old_len,
        "new_chars": len(new_body),
        "growth_ratio": round(growth_ratio, 4),
        "diff_lines": len(diff_lines),
        "rewrite_review_required": bool((old_len and len(new_body) > old_len * 1.2) or len(diff_lines) > 200),
    }


# --------------------------------------------------------------------------- 能力变化
def detect_capability_changes(baseline, candidate) -> dict:
    """显式列出候选相对基线的能力变化。

    ``authorized`` 在生成阶段恒为 ``False``：能力变化必须由用户用更新后的明确配置
    另行授权后才能进入评测，正文里的“忽略限制”不产生任何授权效果。
    """
    old = baseline.entries_by_path() if baseline is not None else {}
    new = candidate.entries_by_path()
    added_scripts = sorted(p for p in new if p not in old and p.endswith(SCRIPT_SUFFIXES))
    removed_scripts = sorted(p for p in old if p not in new and p.endswith(SCRIPT_SUFFIXES))
    newly_executable = sorted(p for p in new if p in old and new[p].executable and not old[p].executable)

    old_text = _all_text(baseline)
    new_text = _all_text(candidate)

    def _new_matches(pattern: re.Pattern) -> list[str]:
        out: list[str] = []
        for line in new_text.splitlines():
            if not pattern.search(line):
                continue
            if pattern.search(line) and line.strip() and line.strip() not in old_text:
                out.append(line.strip()[:200])
        return out

    commands_added = [] if _has_any(old_text, _COMMAND_RE) else _new_matches(_COMMAND_RE)
    network_added = [] if _has_any(old_text, _NETWORK_RE) else _new_matches(_NETWORK_RE)
    network_declared = bool(re.search(r"(?i)network\s*[:=]\s*(true|1|yes)", new_text))
    escalation = [
        line.strip()[:200]
        for line in new_text.splitlines()
        if _ESCALATION_RE.search(line) and line.strip() not in old_text
    ]

    details: list[dict] = []
    if added_scripts:
        details.append({"kind": "scripts_added", "detail": added_scripts})
    if removed_scripts:
        details.append({"kind": "scripts_removed", "detail": removed_scripts})
    if newly_executable:
        details.append({"kind": "executable_added", "detail": newly_executable})
    if commands_added:
        details.append({"kind": "commands_added", "detail": commands_added})
    if network_added or network_declared:
        details.append(
            {"kind": "network_requested", "detail": network_added or ["metadata.network=true"]}
        )
    if escalation:
        details.append({"kind": "permission_escalation_attempt", "detail": escalation})

    requires = bool(
        added_scripts or newly_executable or commands_added or network_added or network_declared or escalation
    )
    hints: list[dict] = []
    if requires:
        hints.append(
            {
                "level": LEVEL_RISK,
                "code": "capability_change",
                "message": "能力变化需另行授权后才能进入评测：" + "；".join(
                    f"{item['kind']}={item['detail']}" for item in details
                ),
                "locator": candidate.lead_file,
            }
        )
    if escalation:
        hints.append(
            {
                "level": LEVEL_RISK,
                "code": "permission_escalation_attempt",
                "message": "正文试图用文字扩大权限（如“忽略限制”），不产生任何授权效果",
                "locator": candidate.lead_file,
            }
        )
    return {
        "requires_authorization": requires,
        "authorized": False,
        "scripts_added": added_scripts,
        "scripts_removed": removed_scripts,
        "executable_added": newly_executable,
        "commands_added": commands_added,
        "network_requested": bool(network_added or network_declared),
        "permission_escalation_attempt": bool(escalation),
        "details": details,
        "hints": hints,
        "previous_authorization": "未变化：候选不改变当前工具/网络/脚本授权",
    }


def _all_text(pkg) -> str:
    if pkg is None:
        return ""
    chunks: list[str] = []
    for path, data in package_files(pkg).items():
        text = _decode(data)
        if text is not None:
            chunks.append(text)
    return "\n".join(chunks)


def _has_any(text: str, pattern: re.Pattern) -> bool:
    return bool(pattern.search(text or ""))


# --------------------------------------------------------------------------- 静态检查
def run_static_checks(candidate, baseline=None, *, checked_at: str | None = None) -> dict:
    """硬检查 + 质量提示 + 能力变化。"""
    module = package_module()
    hard = finding_dicts(module.hard_checks(candidate, baseline=baseline))
    hard = [item for item in hard if item.get("level") == LEVEL_HARD]
    hints = finding_dicts(module.quality_hints(candidate, baseline=baseline))
    hints = [item for item in hints if item.get("level") != LEVEL_HARD]
    capability = detect_capability_changes(baseline, candidate)
    risks = [item for item in capability["hints"]]
    rewrite_review = any(item.get("code") in REWRITE_REVIEW_CODES for item in hints)
    return {
        "level_hard": hard,
        "level_hint": [item for item in hints if item.get("level") == LEVEL_HINT],
        "level_risk": [item for item in hints if item.get("level") == LEVEL_RISK] + risks,
        "hard_failed": bool(hard),
        "rewrite_review_required": bool(rewrite_review),
        "capability_change": capability,
        "body_diff": body_diff_stats(baseline, candidate) if baseline is not None else None,
        "baseline_package_hash": getattr(baseline, "package_hash", None),
        "candidate_package_hash": getattr(candidate, "package_hash", None),
        "checked_at": checked_at or utc_now(),
    }


def blocked_reason(checks: Mapping) -> str:
    items = list(checks.get("level_hard") or [])
    if not items:
        return ""
    return "硬检查未通过：" + "；".join(
        f"{item.get('code')}: {item.get('message')}" for item in items[:5]
    )


# --------------------------------------------------------------------------- 材料过滤
def sanitize_materials(value: Any, *, path: str = "$") -> tuple[Any, list[dict]]:
    """递归过滤生成材料：去掉隐藏样例与预期答案字段。

    返回 ``(清理后的副本, 过滤记录)``；过滤记录只保留位置与原因，不复制被过滤的内容。
    """
    filtered: list[dict] = []

    def _walk(node: Any, where: str) -> Any:
        if isinstance(node, Mapping):
            role = str(node.get("role") or "").lower()
            if role in HOLDOUT_ROLES:
                filtered.append(
                    {
                        "kind": "holdout_case",
                        "at": where,
                        "id": node.get("id") or node.get("case_id"),
                        "reason": "holdout_role",
                    }
                )
                return None
            clean: dict = {}
            for key, item in node.items():
                key_text = str(key)
                if key_text.lower() in FORBIDDEN_MATERIAL_KEYS:
                    filtered.append(
                        {"kind": "forbidden_field", "at": f"{where}.{key_text}", "reason": "expected_answer"}
                    )
                    continue
                walked = _walk(item, f"{where}.{key_text}")
                if walked is not None:
                    clean[key] = walked
            return clean
        if isinstance(node, (list, tuple)):
            out = []
            for index, item in enumerate(node):
                walked = _walk(item, f"{where}[{index}]")
                if walked is not None:
                    out.append(walked)
            return out
        return node

    return _walk(value, path), filtered


# --------------------------------------------------------------------------- JSON 解析
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json_object(text: str):
    """从模型文本回复中解析结构化对象：支持 fenced block、纯 JSON 与花括号扫描。"""
    if not text:
        return None
    candidates = [item.strip() for item in _FENCE_RE.findall(text)]
    candidates.append(text.strip())
    for candidate in candidates:
        parsed = _try_json(candidate)
        if isinstance(parsed, (dict, list)):
            return parsed
    start = text.find("{")
    while start != -1:
        end = _balanced_end(text, start)
        if end is not None:
            parsed = _try_json(text[start : end + 1])
            if isinstance(parsed, (dict, list)):
                return parsed
        start = text.find("{", start + 1)
    return None


def _try_json(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _balanced_end(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


# --------------------------------------------------------------------------- 指纹
def lesson_fingerprint(*, target_skill_id: str | None, attribution: str, rule_text: str, condition_text: str) -> str:
    """经验指纹：同一目标 / 归因 / 规则只保留一条经验（去重口径）。"""
    return canonical_hash(
        {
            "target_skill_id": target_skill_id or "",
            "attribution": attribution,
            "rule": _normalize_text(rule_text),
            "condition": _normalize_text(condition_text),
        }
    )


def event_fingerprint(payload: Mapping) -> str:
    return canonical_hash(dict(payload))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def content_fingerprint(content: str) -> str:
    return sha256_hex(_normalize_text(content))
