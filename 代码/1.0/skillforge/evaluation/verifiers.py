from __future__ import annotations

import json
import re
from pathlib import Path

from ..core import paths as pathmod
from ..core.errors import AppError, InvalidInput, PermissionDenied  # noqa: F401  (PermissionDenied 供调用方识别)
from ..core.ids import canonical_hash, sha256_hex

#: 结构化断言类型
DETERMINISTIC_CHECKS = (
    "file_exists",
    "contains",
    "regex",
    "min_length",
    "section_present",
    "max_length",
    "json_field_equals",
    "artifact_count",
    "answer_contains",
)

#: verifier 类型
VERIFIER_TYPES = ("deterministic", "script", "manual")

#: 单次校验读入的最大字符数（防止异常产物拖垮报告）
DEFAULT_MAX_CHARS = 400_000

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


# --------------------------------------------------------------------- 归一化
def normalize_verifier(spec) -> dict:
    """把用户书写的 verifier 说明归一成 ``{"type","checks":[...]}``。

    - ``["file_exists:output.md"]`` / ``[{"check": "file_exists", "path": "output.md"}]``
    - ``{"type": "script", "script": "...", "checks": [...]}``
    - ``{"type": "manual"}``：用户验收（写作质量），结果为未判定，不能自动算通过。
    """
    if spec is None:
        spec = {}
    if isinstance(spec, (list, tuple)):
        spec = {"type": "deterministic", "checks": list(spec)}
    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, dict):
        raise InvalidInput("verifier 必须是对象或检查列表", field_errors={"verifier": "invalid"})

    kind = str(spec.get("type") or spec.get("kind") or "deterministic").strip().lower()
    if kind in ("auto", "assert", "assertion", "assertions", "deterministic", "structured"):
        kind = "deterministic"
    if kind not in VERIFIER_TYPES:
        raise InvalidInput(
            f"不支持的 verifier 类型: {kind}", field_errors={"verifier.type": "unsupported"}
        )
    raw_checks = spec.get("checks") or spec.get("assertions") or []
    if isinstance(raw_checks, dict):
        raw_checks = [raw_checks]
    checks = [normalize_check(item, index) for index, item in enumerate(raw_checks)]
    out = {
        "type": kind,
        "checks": checks,
        "script": spec.get("script"),
        "timeout_s": int(spec.get("timeout_s") or 60),
        "description": spec.get("description") or spec.get("note") or "",
    }
    return out


def normalize_check(item, index: int = 0) -> dict:
    """归一单个检查；``check``/``type``/``kind`` 任一给出检查类型。"""
    if isinstance(item, str):
        text = item.strip()
        if ":" in text:
            name, _, value = text.partition(":")
            item = {"check": name.strip(), "path": value.strip()}
        else:
            item = {"check": text}
    if not isinstance(item, dict):
        raise InvalidInput(
            f"检查项 #{index + 1} 必须是对象", field_errors={"verifier.checks": "invalid"}
        )
    data = dict(item)
    kind = data.get("check") or data.get("type") or data.get("kind")
    if not kind and isinstance(data.get("name"), str) and data["name"].strip() in DETERMINISTIC_CHECKS:
        kind = data["name"].strip()
    kind = str(kind or "").strip()
    label = data.get("name") or data.get("label") or kind or f"check_{index + 1}"
    params: dict = {}
    if isinstance(data.get("params"), dict):
        # 已归一过的检查（例如库里存回来的 suite/case）：参数在 params 里
        params.update(data["params"])
    params.update(
        {
            key: value
            for key, value in data.items()
            if key not in ("check", "type", "kind", "name", "label", "critical", "description",
                           "params")
        }
    )
    return {
        "name": str(label),
        "check": kind,
        "critical": bool(data.get("critical", False)),
        "params": params,
        "description": data.get("description") or "",
    }


def checks_from_criteria(criteria, verifier: dict | None = None) -> list[dict]:
    """把完成条件中内联的、可判定的检查项升级为真正的检查。

    只处理带 ``check``/``type``/``kind`` 且类型受支持的条件；用于保证
    「至少一项可判定标准」在运行期真的被执行，而不是只写在说明里。
    """
    existing = {c["name"] for c in (verifier or {}).get("checks") or []}
    out: list[dict] = []
    for index, item in enumerate(criteria or []):
        if not isinstance(item, dict):
            continue
        kind = item.get("check") or item.get("type") or item.get("kind")
        if isinstance(kind, str) and kind.strip() in DETERMINISTIC_CHECKS:
            spec = normalize_check(item, index)
            if spec["name"] in existing:
                continue
            out.append(spec)
    return out


def describe_checks(checks) -> dict:
    """检查集合的可判定性摘要（供 suite 校验与报告展示）。"""
    items = list(checks or [])
    return {
        "total": len(items),
        "critical": len([c for c in items if c.get("critical")]),
        "kinds": sorted({c.get("check") for c in items if c.get("check")}),
        "undecidable": [c.get("name") for c in items if c.get("check") not in DETERMINISTIC_CHECKS],
    }


# --------------------------------------------------------------------- Verifier
class Verifier:
    """运行产物校验器；只读运行产物，不改任何标准。"""

    def __init__(self, *, blobs=None, container=None, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self.blobs = blobs
        self.container = container
        self.max_chars = int(max_chars or DEFAULT_MAX_CHARS)

    # ------------------------------------------------------------- 主入口
    def verify_case(self, case: dict, artifacts: list[dict], run_dir: Path | None) -> dict:
        """校验一个样例的一次执行。

        返回 ``{"passed","score","checks","critical_failed","failed","undetermined",...}``。
        ``passed`` 为真要求**每一个**检查都通过：缺项、未知类型、路径越界都不得算通过。
        """
        case = dict(case or {})  # 只读：拷贝后再使用
        case_id = case.get("id") or case.get("case_id")
        spec = normalize_verifier(case.get("verifier") or {})
        checks = list(spec["checks"])
        checks.extend(checks_from_criteria(case.get("completion_criteria") or [], spec))

        # 套件级 critical_checks（已归一）也参与执行：critical 标识必须在运行期生效
        for extra in case.get("_critical_checks") or []:
            spec_extra = normalize_check(extra)
            if spec_extra["name"] not in {c["name"] for c in checks}:
                spec_extra["critical"] = True
                checks.append(spec_extra)

        base = {
            "case_id": case_id,
            "verifier_type": spec["type"],
            "source": "deterministic" if spec["type"] == "deterministic" else spec["type"],
            "checks": [],
            "critical_failed": [],
            "failed": [],
            "undetermined": [],
            "notes": [],
        }

        if spec["type"] == "manual":
            base["passed"] = False
            base["score"] = None
            base["notes"].append("用户验收类检查：未判定，不计通过也不计入模型评分")
            base["status"] = "undetermined"
            return base

        if spec["type"] == "script":
            return self._verify_script(case, spec, artifacts, run_dir, base)

        if not checks:
            base["passed"] = False
            base["score"] = None
            base["status"] = "undetermined"
            base["notes"].append("没有任何可判定的检查项：不得自动给高分")
            return base

        index = _artifact_index(artifacts)
        results = [self._run_check(check, case, index, run_dir) for check in checks]
        return self._aggregate(base, results)

    # ------------------------------------------------------------- 脚本 verifier
    def _verify_script(self, case, spec, artifacts, run_dir, base) -> dict:
        script = (spec.get("script") or "").strip()
        if not script:
            base["status"] = "undetermined"
            base["passed"] = False
            base["score"] = None
            base["notes"].append("脚本 verifier 缺少脚本内容")
            return base
        if self.container is None:
            base["status"] = "undetermined"
            base["passed"] = False
            base["score"] = None
            base["notes"].append("容器运行时不可用：受隔离脚本检查未判定，不降级为宿主执行")
            return base
        outputs_dir = Path(run_dir or ".").parent / f".verify-{case.get('id') or 'case'}"
        try:
            outputs_dir.mkdir(parents=True, exist_ok=True)
            result = self.container.run(
                script,
                inputs_dir=Path(run_dir) if run_dir else None,
                outputs_dir=outputs_dir,
                timeout_s=int(spec.get("timeout_s") or 60),
                network=False,
            )
        except AppError as exc:
            base["status"] = "undetermined"
            base["passed"] = False
            base["score"] = None
            base["notes"].append(f"脚本 verifier 无法执行（{exc.code}: {exc.message}）")
            return base
        except Exception as exc:  # pragma: no cover - 兜底
            base["status"] = "undetermined"
            base["passed"] = False
            base["score"] = None
            base["notes"].append(f"脚本 verifier 异常：{exc}")
            return base

        payload = _extract_json(result.stdout or "")
        checks = []
        if isinstance(payload, dict) and isinstance(payload.get("checks"), list):
            for item in payload["checks"]:
                if not isinstance(item, dict):
                    continue
                status = "passed" if item.get("passed") else "failed"
                checks.append(
                    {
                        "name": str(item.get("name") or "script_check"),
                        "check": "script",
                        "critical": bool(item.get("critical", True)),
                        "status": status,
                        "detail": {"message": item.get("detail") or item.get("message") or ""},
                    }
                )
        if not checks:
            checks.append(
                {
                    "name": "script_exit_code",
                    "check": "script",
                    "critical": True,
                    "status": "passed" if result.exit_code == 0 else "failed",
                    "detail": {
                        "exit_code": result.exit_code,
                        "stdout": (result.stdout or "")[:2000],
                        "stderr": (result.stderr or "")[:2000],
                    },
                }
            )
        base["checks"] = []
        return self._aggregate(base, checks, extra_note=None if result.exit_code == 0 else "脚本退出码非 0")

    # ------------------------------------------------------------- 单检查
    def _run_check(self, check: dict, case: dict, index: dict, run_dir) -> dict:
        kind = check.get("check")
        handler = getattr(self, f"_check_{kind}", None) if kind in DETERMINISTIC_CHECKS else None
        entry = {
            "name": check["name"],
            "check": kind or "",
            "critical": bool(check.get("critical")),
            "status": "failed",
            "detail": {},
        }
        if handler is None:
            entry["detail"] = {
                "reason": "unsupported_check",
                "message": f"未知或缺失的检查类型: {kind or '(缺失)'}；缺项不得自动给高分",
            }
            return entry
        try:
            status, detail = handler(check.get("params") or {}, case, index, run_dir)
        except PermissionDenied as exc:
            return {
                "name": check["name"],
                "check": kind,
                "critical": bool(check.get("critical")),
                "status": "failed",
                "detail": {"reason": "path_escape", "message": exc.message},
            }
        except InvalidInput as exc:
            return {
                "name": check["name"],
                "check": kind,
                "critical": bool(check.get("critical")),
                "status": "failed",
                "detail": {"reason": "invalid_check", "message": exc.message},
            }
        entry["status"] = status
        entry["detail"] = detail
        return entry

    # ------------------------------------------------------------- 检查实现
    def _check_file_exists(self, params, case, index, run_dir):
        rel = params.get("path") or params.get("value")
        if not rel:
            return "failed", {"reason": "missing_parameter", "message": "file_exists 需要 path"}
        text, error = self._load(rel, index, run_dir)
        if error == "artifact_missing":
            return "failed", {"reason": "artifact_missing", "path": _norm(rel)}
        if error == "evidence_unavailable":
            return "failed", {"reason": "evidence_unavailable", "path": _norm(rel)}
        if error:
            return "failed", {"reason": error, "path": _norm(rel)}
        size = len(text.encode("utf-8")) if text is not None else 0
        if params.get("min_bytes") is not None and size < int(params["min_bytes"]):
            return "failed", {"reason": "too_small", "path": _norm(rel), "size": size,
                              "min_bytes": int(params["min_bytes"])}
        return "passed", {"path": _norm(rel), "size": size}

    def _check_contains(self, params, case, index, run_dir):
        needle = params.get("text", params.get("value", params.get("needle")))
        if needle is None:
            return "failed", {"reason": "missing_parameter", "message": "contains 需要 text"}
        rel = params.get("path")
        text, error = self._scope_text(rel, index, run_dir)
        if error:
            return "failed", {"reason": error, "path": _norm(rel) if rel else None}
        haystack = text if params.get("case_sensitive", True) else text.lower()
        target = str(needle) if params.get("case_sensitive", True) else str(needle).lower()
        count = haystack.count(target)
        need = int(params.get("count") or params.get("min_count") or 1)
        if count < need:
            return "failed", {"reason": "not_found", "needle": str(needle)[:200], "count": count,
                              "required": need, "path": _norm(rel) if rel else None}
        if params.get("max_count") is not None and count > int(params["max_count"]):
            return "failed", {"reason": "too_many", "needle": str(needle)[:200], "count": count,
                              "max_count": int(params["max_count"])}
        return "passed", {"needle": str(needle)[:200], "count": count, "path": _norm(rel) if rel else None}

    def _check_regex(self, params, case, index, run_dir):
        pattern = params.get("pattern") or params.get("regex") or params.get("value")
        if not pattern:
            return "failed", {"reason": "missing_parameter", "message": "regex 需要 pattern"}
        flags = re.M | (re.I if params.get("ignore_case") else 0)
        try:
            compiled = re.compile(str(pattern), flags)
        except re.error as exc:
            return "failed", {"reason": "invalid_pattern", "message": str(exc)}
        rel = params.get("path")
        text, error = self._scope_text(rel, index, run_dir)
        if error:
            return "failed", {"reason": error, "path": _norm(rel) if rel else None}
        match = compiled.search(text)
        if not match:
            return "failed", {"reason": "no_match", "pattern": str(pattern)[:200],
                              "path": _norm(rel) if rel else None}
        return "passed", {"pattern": str(pattern)[:200], "match": match.group(0)[:200]}

    def _check_min_length(self, params, case, index, run_dir):
        minimum = params.get("min", params.get("value", params.get("min_length")))
        if minimum is None:
            return "failed", {"reason": "missing_parameter", "message": "min_length 需要 min"}
        rel = params.get("path")
        text, error = self._scope_text(rel, index, run_dir, longest_if_unscoped=bool(rel is None))
        if error:
            return "failed", {"reason": error, "path": _norm(rel) if rel else None}
        length = len(text.strip())
        if length < int(minimum):
            return "failed", {"reason": "too_short", "length": length, "min": int(minimum),
                              "path": _norm(rel) if rel else None}
        return "passed", {"length": length, "min": int(minimum)}

    def _check_max_length(self, params, case, index, run_dir):
        maximum = params.get("max", params.get("value", params.get("max_length")))
        if maximum is None:
            return "failed", {"reason": "missing_parameter", "message": "max_length 需要 max"}
        rel = params.get("path")
        text, error = self._scope_text(rel, index, run_dir, longest_if_unscoped=bool(rel is None))
        if error:
            return "failed", {"reason": error, "path": _norm(rel) if rel else None}
        length = len(text.strip())
        if length > int(maximum):
            return "failed", {"reason": "too_long", "length": length, "max": int(maximum),
                              "path": _norm(rel) if rel else None}
        return "passed", {"length": length, "max": int(maximum)}

    def _check_section_present(self, params, case, index, run_dir):
        heading = params.get("heading", params.get("section", params.get("value")))
        if not heading:
            return "failed", {"reason": "missing_parameter", "message": "section_present 需要 heading"}
        level = params.get("level")
        rel = params.get("path")
        text, error = self._scope_text(rel, index, run_dir)
        if error:
            return "failed", {"reason": error, "path": _norm(rel) if rel else None}
        hashes = f"{{{int(level)}}}" if level else "{1,6}"
        pattern = re.compile(
            rf"^[ \t]{{0,3}}#{hashes}[ \t]*{re.escape(str(heading).strip())}[ \t]*#*[ \t]*$",
            re.M,
        )
        if not pattern.search(text):
            return "failed", {"reason": "section_missing", "heading": str(heading)[:200],
                              "path": _norm(rel) if rel else None}
        return "passed", {"heading": str(heading)[:200], "path": _norm(rel) if rel else None}

    def _check_json_field_equals(self, params, case, index, run_dir):
        rel = params.get("path")
        field = params.get("field") or params.get("key")
        if not rel or not field:
            return "failed", {"reason": "missing_parameter",
                              "message": "json_field_equals 需要 path 与 field"}
        if "value" not in params and "expected" not in params:
            return "failed", {"reason": "missing_parameter", "message": "json_field_equals 需要 value"}
        expected = params.get("value", params.get("expected"))
        text, error = self._load(rel, index, run_dir)
        if error:
            return "failed", {"reason": error, "path": _norm(rel)}
        payload = _extract_json(text)
        if payload is None:
            return "failed", {"reason": "invalid_json", "path": _norm(rel)}
        found, value = _dotted_get(payload, str(field))
        if not found:
            return "failed", {"reason": "field_missing", "field": str(field), "path": _norm(rel)}
        if not _value_equal(value, expected):
            return "failed", {"reason": "value_mismatch", "field": str(field),
                              "expected": expected, "actual": value}
        return "passed", {"field": str(field), "value": value}

    def _check_artifact_count(self, params, case, index, run_dir):
        items = list(index.values())
        prefix = params.get("path_prefix") or params.get("prefix")
        if prefix:
            items = [i for i in items if _norm(str(i.get("path") or "")).startswith(_norm(str(prefix)))]
        count = len(items)
        exact = params.get("value", params.get("count", params.get("exact")))
        if exact is not None and count != int(exact):
            return "failed", {"reason": "count_mismatch", "count": count, "expected": int(exact)}
        if params.get("min") is not None and count < int(params["min"]):
            return "failed", {"reason": "count_below_min", "count": count, "min": int(params["min"])}
        if params.get("max") is not None and count > int(params["max"]):
            return "failed", {"reason": "count_above_max", "count": count, "max": int(params["max"])}
        return "passed", {"count": count,
                          "paths": sorted(_norm(str(i.get("path") or "")) for i in items)[:50]}

    def _check_answer_contains(self, params, case, index, run_dir):
        """关键事实检查：用预期答案（只有 verifier / judge 可读）核对产物。"""
        expected = case.get("expected_answers") or {}
        key = str(params.get("key") or params.get("field") or "answer")
        value = expected.get(key) if isinstance(expected, dict) else None
        if value is None:
            return "failed", {"reason": "expected_answer_missing", "key": key}
        alternatives = value if isinstance(value, list) else [value]
        rel = params.get("path")
        text, error = self._scope_text(rel, index, run_dir)
        if error:
            return "failed", {"reason": error, "path": _norm(rel) if rel else None}
        for candidate in alternatives:
            if str(candidate) and str(candidate) in text:
                return "passed", {"key": key, "matched": str(candidate)[:200]}
        return "failed", {"reason": "expected_answer_absent", "key": key,
                          "expected": [str(a)[:120] for a in alternatives][:5]}

    # ------------------------------------------------------------- 聚合
    def _aggregate(self, base: dict, results: list[dict], *, extra_note: str | None = None) -> dict:
        base["checks"] = results
        critical_failed = [r["name"] for r in results if r["critical"] and r["status"] != "passed"]
        failed = [r["name"] for r in results if not r["critical"] and r["status"] == "failed"]
        undetermined = [r["name"] for r in results if r["status"] == "undetermined"]
        total = len(results)
        passed_count = len([r for r in results if r["status"] == "passed"])
        base["critical_failed"] = critical_failed
        base["failed"] = failed
        base["undetermined"] = undetermined
        base["critical_total"] = len([r for r in results if r["critical"]])
        base["coverage"] = {
            "checks": total,
            "passed": passed_count,
            "failed": len([r for r in results if r["status"] == "failed"]),
            "undetermined": len(undetermined),
        }
        if undetermined:
            base["passed"] = False
            base["status"] = "undetermined"
            base["score"] = None
        elif critical_failed or failed:
            base["passed"] = False
            base["status"] = "failed"
            base["score"] = round(100.0 * passed_count / total, 3) if total else None
        else:
            base["passed"] = True
            base["status"] = "passed"
            base["score"] = 100.0
        if extra_note:
            base["notes"].append(extra_note)
        if base["status"] != "passed":
            base["notes"].append("关键检查或必需章节未全部满足：不得仅凭模型打分算通过")
        return base

    # ------------------------------------------------------------- 产物读取
    def _load(self, rel, index: dict, run_dir) -> tuple[str | None, str | None]:
        rel_norm = _norm(rel)
        if not rel_norm:
            return None, "artifact_missing"
        target = None
        if run_dir is not None:
            target = pathmod.resolve_within(Path(run_dir), rel_norm)  # 越界 → PermissionDenied
            if target.is_file():
                try:
                    raw = target.read_bytes()[: self.max_chars]
                except OSError:
                    return None, "evidence_unavailable"
                return raw.decode("utf-8", errors="replace"), None
        entry = index.get(rel_norm)
        if entry is not None:
            blob_id = entry.get("blob_id") or entry.get("content_hash")
            if blob_id and self.blobs is not None and self.blobs.exists(blob_id):
                try:
                    return (self.blobs.get_bytes(blob_id)[: self.max_chars]).decode(
                        "utf-8", errors="replace"
                    ), None
                except Exception:  # pragma: no cover - 内容寻址读取失败
                    return None, "evidence_unavailable"
            return None, "artifact_missing"
        return None, "artifact_missing"

    def _scope_text(self, rel, index, run_dir, *, longest_if_unscoped: bool = False):
        if rel:
            return self._load(rel, index, run_dir)
        texts = []
        for path in sorted(index):
            text, error = self._load(path, index, run_dir)
            if error is None and text is not None:
                texts.append(text)
        if not texts:
            return "", "artifact_missing"
        return (max(texts, key=len) if longest_if_unscoped else "\n".join(texts)), None


# --------------------------------------------------------------------- 辅助
def _artifact_index(artifacts) -> dict:
    index: dict[str, dict] = {}
    for item in artifacts or []:
        if isinstance(item, dict):
            rel = _norm(str(item.get("path") or ""))
            if rel:
                index[rel] = item
        elif isinstance(item, str):
            rel = _norm(item)
            if rel:
                index[rel] = {"path": rel}
    return index


def _norm(rel: str) -> str:
    text = str(rel or "").replace("\\", "/").strip()
    parts = [p for p in text.split("/") if p not in ("", ".")]
    return "/".join(parts)


def _extract_json(text: str | None):
    if not text:
        return None
    candidate = text.strip()
    fence = _JSON_FENCE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()
    for attempt in (candidate, _first_json_object(candidate)):
        if not attempt:
            continue
        try:
            return json.loads(attempt)
        except (TypeError, ValueError):
            continue
    return None


def _first_json_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return None


def _dotted_get(payload, field: str):
    current = payload
    for part in str(field).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, current


def _value_equal(a, b) -> bool:
    if a == b:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return str(a).lower() == str(b).lower()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a).strip() == str(b).strip()


def fixture_hash(input_files) -> str:
    """固定输入夹具 hash：按 path 排序后取 canonical hash。"""
    entries = []
    for item in input_files or []:
        if isinstance(item, dict):
            path = _norm(str(item.get("path") or ""))
            content = item.get("content")
            if isinstance(content, bytes):
                digest = sha256_hex(content)
            else:
                digest = sha256_hex(str(content if content is not None else ""))
            entries.append({"path": path, "sha256": digest})
        elif isinstance(item, str):
            entries.append({"path": _norm(item), "sha256": ""})
    entries.sort(key=lambda e: e["path"])
    return canonical_hash(entries)
