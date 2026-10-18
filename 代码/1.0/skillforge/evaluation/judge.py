from __future__ import annotations

import json
import re
import time

from ..core.errors import AppError, InvalidInput
from ..core.ids import canonical_hash

#: 允许进入裁判 prompt 的用例字段（白名单，其余一律剥离）
BLIND_ALLOWED_KEYS = ("name", "prompt", "completion_criteria", "role", "category")

#: 一旦出现在裁判输入里就说明没有盲评的字段（用于自检）
FORBIDDEN_KEYS = (
    "candidate_version_id",
    "baseline_version_id",
    "target_version_id",
    "version_id",
    "version_label",
    "package_hash",
    "proposal_id",
    "supersedes_id",
    "reason",
    "expected_benefit",
    "potential_regression",
    "summary",
    "attribution",
    "author_model",
    "judge_model",
    "origin",
)

_JUDGE_SYSTEM = (
    "你是独立评审裁判，只在给定 rubric 下为产物打分。\n"
    "你不知道、也不需要知道产物来自哪个版本、谁写的、为什么写；不得猜测或要求版本信息。\n"
    "评分维度与权重已冻结，必须逐维度给出 0～100 的整数分数与不确定性说明；\n"
    "缺少信息时给出低分并说明不确定，不允许用满分代替未知。\n"
    "只输出 JSON，不要输出多余文字。"
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def blind_view(case: dict, *, include_expected: bool = False) -> dict:
    """剥离版本身份与候选理由，只保留可公开的用例信息。"""
    data = dict(case or {})
    view = {key: data[key] for key in BLIND_ALLOWED_KEYS if key in data and data[key] is not None}
    if include_expected and data.get("expected_answers"):
        # 预期答案只提供给 verifier / judge
        view["expected_answers"] = data["expected_answers"]
    return view


def find_leaks(text: str, extra_terms=None) -> list[str]:
    """在裁判输入里找禁止出现的字段名或额外身份串（自检用）。"""
    haystack = text or ""
    leaks = [key for key in FORBIDDEN_KEYS if key in haystack]
    for term in extra_terms or []:
        if term and str(term) in haystack:
            leaks.append(str(term))
    return sorted(set(leaks))


def rubric_dimensions(rubric: dict) -> list[dict]:
    dims = (rubric or {}).get("dimensions")
    if isinstance(dims, dict):
        dims = [{"name": k, "weight": v} for k, v in dims.items()]
    out = []
    for item in dims or []:
        if isinstance(item, str):
            item = {"name": item, "weight": 0}
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "name": str(item.get("name") or ""),
                "weight": float(item.get("weight") or 0),
                "description": str(item.get("description") or ""),
            }
        )
    return out


class BlindJudge:
    """模型盲评；只产出评审证据，不产出最终 verdict。"""

    def __init__(self, *, model_factory=None, budget=None, scope_id=None, budget_guard=None,
                 max_output_tokens: int = 1024) -> None:
        self.model_factory = model_factory
        self.budget = budget
        self.scope_id = scope_id
        self.budget_guard = budget_guard
        self.max_output_tokens = int(max_output_tokens or 1024)

    # ------------------------------------------------------------- 单份评分
    def score(self, *, rubric, case, artifacts_text, model=None, author_model=None,
              forbidden_terms=None, include_expected=False, max_output_tokens=None) -> dict:
        """对一次执行的产物按冻结 rubric 打分（contract §7 签名）。"""
        view = blind_view(case, include_expected=include_expected)
        system = self._system_prompt(rubric)
        user = self._single_user_prompt(view, artifacts_text)
        leaks = find_leaks(system + "\n" + user, forbidden_terms)
        if leaks:  # pragma: no cover - 自检防线，正常路径不触发
            raise InvalidInput(
                f"盲评输入包含版本身份信息: {leaks}", field_errors={"judge": "not_blind"}
            )
        call = self._call(system, user, model=model, max_output_tokens=max_output_tokens)
        payload = _extract_json(call.get("text"))
        dims = _parse_dimensions(payload)
        weighted = _weighted_score(rubric, dims)
        model_info = call.get("model") or {}
        return {
            "judge_type": "model_blind",
            "score": weighted["score"],
            "dimension_scores": weighted["dimension_scores"],
            "missing_dimensions": weighted["missing_dimensions"],
            "complete": weighted["complete"],
            "weights_total": weighted["weights_total"],
            "pass_threshold": _pass_threshold(rubric),
            "passed_hint": weighted["passed_hint"],
            "model_says_passed": _says_passed(payload, call.get("text")),
            "uncertainty": _uncertainty(payload),
            "disclosure": self._disclosure(model_info, author_model),
            "model": model_info,
            "usage": call.get("usage") or {},
            "usage_known": bool(call.get("usage_known")),
            "error": call.get("error"),
            "raw_text": (call.get("text") or "")[:4000],
            "prompt": {"system": system, "user": user},
            "elapsed_ms": call.get("elapsed_ms"),
        }

    # ------------------------------------------------------------- 配对评分
    def score_pair(self, *, rubric, case, outputs, model=None, author_model=None,
                   forbidden_terms=None, include_expected=False, max_output_tokens=None) -> dict:
        """一次调用同时匿名评审 A/B 两份产物。

        ``outputs`` 只允许包含 ``{"A": {"text": ...}, "B": {"text": ...}}``
        哪一份是候选、哪一份是基线由调用方记录，绝不进入 prompt。
        """
        view = blind_view(case, include_expected=include_expected)
        system = self._system_prompt(rubric, pair=True)
        user = self._pair_user_prompt(view, outputs)
        leaks = find_leaks(system + "\n" + user, forbidden_terms)
        if leaks:  # pragma: no cover - 自检防线
            raise InvalidInput(
                f"盲评输入包含版本身份信息: {leaks}", field_errors={"judge": "not_blind"}
            )
        call = self._call(system, user, model=model, max_output_tokens=max_output_tokens)
        payload = _extract_json(call.get("text")) or {}
        raw_a = _dims_from_container(payload, "A")
        raw_b = _dims_from_container(payload, "B")
        weighted_a = _weighted_score(rubric, raw_a)
        weighted_b = _weighted_score(rubric, raw_b)
        preferred = str(payload.get("preferred") or payload.get("better") or "").strip().upper()
        if preferred not in ("A", "B", "TIE"):
            preferred = None
        computed = _preference(weighted_a["score"], weighted_b["score"])
        disagreement = bool(
            not weighted_a["complete"]
            or not weighted_b["complete"]
            or (preferred is not None and computed != "tie" and preferred != computed)
        )
        model_info = call.get("model") or {}
        disclosure = self._disclosure(model_info, author_model)
        side = {
            "A": {
                "score": weighted_a["score"],
                "dimension_scores": weighted_a["dimension_scores"],
                "missing_dimensions": weighted_a["missing_dimensions"],
                "complete": weighted_a["complete"],
            },
            "B": {
                "score": weighted_b["score"],
                "dimension_scores": weighted_b["dimension_scores"],
                "missing_dimensions": weighted_b["missing_dimensions"],
                "complete": weighted_b["complete"],
            },
        }
        return {
            "judge_type": "model_blind_pair",
            "A": side["A"],
            "B": side["B"],
            "preferred": preferred,
            "computed_preference": computed,
            "disagreement": disagreement,
            "complete": bool(weighted_a["complete"] and weighted_b["complete"]),
            "weights_total": weighted_a["weights_total"],
            "pass_threshold": _pass_threshold(rubric),
            "model_says_passed": _says_passed(payload, call.get("text")),
            "uncertainty": _uncertainty(payload),
            "disclosure": disclosure,
            "model": model_info,
            "usage": call.get("usage") or {},
            "usage_known": bool(call.get("usage_known")),
            "error": call.get("error"),
            "raw_text": (call.get("text") or "")[:4000],
            "prompt": {"system": system, "user": user},
            "elapsed_ms": call.get("elapsed_ms"),
        }

    # ------------------------------------------------------------- prompt
    def _system_prompt(self, rubric: dict, *, pair: bool = False) -> str:
        dims = rubric_dimensions(rubric)
        lines = [_JUDGE_SYSTEM, "", "冻结 rubric（权重合计 100）："]
        for dim in dims:
            suffix = f"：{dim['description']}" if dim["description"] else ""
            lines.append(f"- {dim['name']}（权重 {dim['weight']}）{suffix}")
        threshold = _pass_threshold(rubric)
        lines.append(f"- 通过阈值：{threshold} 分（0～100）")
        if pair:
            lines += [
                "",
                "你将看到两份匿名产物（输出 A / 输出 B）。请分别逐维度打分，并给出你更偏好的一份。",
                '输出 JSON：{"dimensions": {"A": {"<维度名>": {"score": 0-100, "uncertainty": "..."}}, '
                '"B": {...}}, "preferred": "A|B|tie", "uncertainty": "整体不确定性说明"}',
            ]
        else:
            lines += [
                "",
                "请逐维度打分。",
                '输出 JSON：{"dimensions": {"<维度名>": {"score": 0-100, "uncertainty": "..."}}, '
                '"uncertainty": "整体不确定性说明"}',
            ]
        return "\n".join(lines)

    def _single_user_prompt(self, view: dict, artifacts_text) -> str:
        parts = ["任务请求（prompt）：", str(view.get("prompt") or "")]
        criteria = view.get("completion_criteria") or []
        if criteria:
            parts += ["", "完成条件："]
            for item in criteria:
                if isinstance(item, dict):
                    parts.append(f"- {item.get('name') or item.get('criterion')}")
                else:
                    parts.append(f"- {item}")
        expected = view.get("expected_answers")
        if expected:
            parts += ["", "预期答案（仅裁判可见，用于核对关键事实）：",
                      json.dumps(expected, ensure_ascii=False)]
        parts += ["", "待评产物：", _render_artifacts(artifacts_text)]
        return "\n".join(parts)

    def _pair_user_prompt(self, view: dict, outputs: dict) -> str:
        parts = ["任务请求（prompt）：", str(view.get("prompt") or "")]
        criteria = view.get("completion_criteria") or []
        if criteria:
            parts += ["", "完成条件："]
            for item in criteria:
                if isinstance(item, dict):
                    parts.append(f"- {item.get('name') or item.get('criterion')}")
                else:
                    parts.append(f"- {item}")
        if view.get("expected_answers"):
            parts += ["", "预期答案（仅裁判可见，用于核对关键事实）：",
                      json.dumps(view["expected_answers"], ensure_ascii=False)]
        for label in ("A", "B"):
            item = (outputs or {}).get(label) or {}
            parts += ["", f"===== 输出 {label} =====", _render_artifacts(item.get("text") or item.get("artifacts_text") or "")]
        return "\n".join(parts)

    # ------------------------------------------------------------- 模型调用
    def _call(self, system: str, user: str, *, model=None, max_output_tokens=None) -> dict:
        from ..platform.model import ChatMessage

        client = model or (self.model_factory() if self.model_factory else None)
        if client is None:
            return {"error": "model_unavailable", "text": None, "usage": {}, "usage_known": False}
        blocked = self._budget_blocked()
        if blocked:
            return {"error": f"budget_exceeded:{blocked}", "text": None, "usage": {},
                    "usage_known": False}
        messages = [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]
        started = time.monotonic()
        try:
            reply = client.complete(
                messages, max_output_tokens=int(max_output_tokens or self.max_output_tokens)
            )
        except AppError as exc:
            return {"error": exc.code, "message": exc.message, "text": None, "usage": {},
                    "usage_known": False, "elapsed_ms": int((time.monotonic() - started) * 1000)}
        except Exception as exc:  # pragma: no cover - 兜底，不让裁判异常打死作业
            return {"error": "judge_error", "message": str(exc), "text": None, "usage": {},
                    "usage_known": False, "elapsed_ms": int((time.monotonic() - started) * 1000)}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        usage = dict(getattr(reply, "usage", None) or {})
        known = bool(getattr(reply, "usage_known", False))
        tokens = _total_tokens(usage) if known else 0
        self._consume(tokens, known)
        return {
            "text": getattr(reply, "text", "") or "",
            "usage": usage,
            "usage_known": known,
            "model": {
                "model_id": getattr(reply, "model_id", None),
                "requested": getattr(getattr(client, "config", None), "model", None),
                "fingerprint": client.fingerprint() if hasattr(client, "fingerprint") else None,
            },
            "elapsed_ms": elapsed_ms,
            "error": None,
        }

    def _budget_blocked(self) -> str | None:
        if self.budget_guard is not None:
            return self.budget_guard()
        if self.budget is None or not self.scope_id:
            return None
        try:
            state = self.budget.state(self.scope_id)
        except AppError:  # pragma: no cover - 账本缺失
            return "budget_uninitialized"
        if state.status != "open":
            return state.exceeded_type or "closed"
        if state.remaining_calls < 1:
            return "max_model_calls"
        if state.remaining_tokens <= 0:
            return "max_tokens"
        return None

    def _consume(self, tokens: int, usage_known: bool) -> None:
        if self.budget is None or not self.scope_id:
            return
        try:
            self.budget.consume(self.scope_id, tokens=tokens, calls=1, usage_known=usage_known)
        except AppError:  # pragma: no cover - 记账失败不阻断（上界已被 guard 保护）
            pass

    # ------------------------------------------------------------- 披露
    def _disclosure(self, model_info: dict, author_model) -> dict:
        judge_model = model_info.get("model_id") or model_info.get("requested")
        same = None
        if author_model and judge_model:
            same = _same_model(str(author_model), str(judge_model))
        if same is True:
            text = ("裁判与候选作者使用同一模型：本评审**不构成**独立事实验证，"
                    "只作为参考证据。")
        elif same is False:
            text = "裁判模型与候选作者模型不同；仍不等同于独立事实核查。"
        else:
            text = "无法确认候选作者模型与裁判模型是否相同：不得宣称评审独立。"
        return {
            "judge_type": "model_blind",
            "judge_model": judge_model,
            "judge_fingerprint": model_info.get("fingerprint"),
            "author_model": author_model,
            "same_model_as_author": same,
            "text": text,
        }


# --------------------------------------------------------------------- 解析辅助
def _extract_json(text):
    if not text:
        return None
    candidate = text.strip()
    fence = _JSON_FENCE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()
    for attempt in (candidate, _first_object(candidate)):
        if not attempt:
            continue
        try:
            return json.loads(attempt)
        except (TypeError, ValueError):
            continue
    return None


def _first_object(text: str):
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return None


def _dims_from_container(payload, label: str) -> dict:
    if not isinstance(payload, dict):
        return {}
    dims = payload.get("dimensions")
    if isinstance(dims, dict):
        container = dims.get(label) or dims.get(label.lower()) or {}
        if isinstance(container, dict):
            # 兼容 {"dimensions": {"质量": {"A": {...}, "B": {...}}}} 形式
            if container and all(isinstance(v, dict) for v in container.values()):
                nested = {k: v.get(label) or v.get(label.lower()) for k, v in container.items()}
                if all(v is not None for v in nested.values()):
                    return {
                        k: (v if isinstance(v, dict) else {"score": v})
                        for k, v in nested.items()
                    }
            return container
    side = payload.get(label) or payload.get(label.lower())
    if isinstance(side, dict):
        inner = side.get("dimensions") if isinstance(side.get("dimensions"), dict) else side
        return inner
    return {}


def _parse_dimensions(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    dims = payload.get("dimensions")
    if isinstance(dims, dict):
        return dims
    if isinstance(dims, list):
        out = {}
        for item in dims:
            if isinstance(item, dict) and item.get("name"):
                out[str(item["name"])] = item
        return out
    return {}


def _weighted_score(rubric: dict, raw_dims: dict) -> dict:
    dims = rubric_dimensions(rubric)
    dimension_scores: dict = {}
    missing: list[str] = []
    weighted = 0.0
    total_weight = 0.0
    for dim in dims:
        entry = raw_dims.get(dim["name"]) if isinstance(raw_dims, dict) else None
        if entry is None:
            for key, value in (raw_dims or {}).items():
                if str(key).strip() == dim["name"]:
                    entry = value
                    break
        if isinstance(entry, dict):
            score = entry.get("score", entry.get("value"))
            uncertainty = entry.get("uncertainty") or entry.get("reason")
        else:
            score = entry
            uncertainty = None
        try:
            value = float(score)
        except (TypeError, ValueError):
            value = None
        if value is None:
            missing.append(dim["name"])
            dimension_scores[dim["name"]] = {
                "score": None,
                "weight": dim["weight"],
                "uncertainty": uncertainty,
                "missing": True,
            }
            total_weight += dim["weight"]
            continue
        value = max(0.0, min(100.0, value))
        weighted += value * dim["weight"]
        total_weight += dim["weight"]
        dimension_scores[dim["name"]] = {
            "score": round(value, 3),
            "weight": dim["weight"],
            "uncertainty": uncertainty,
            "missing": False,
        }
    if not dims:
        return {
            "score": None,
            "dimension_scores": {},
            "missing_dimensions": [],
            "complete": False,
            "weights_total": 0.0,
            "passed_hint": None,
        }
    score = round(weighted / 100.0, 3) if total_weight else None
    threshold = _pass_threshold(rubric)
    return {
        "score": score,
        "dimension_scores": dimension_scores,
        "missing_dimensions": missing,
        "complete": not missing,
        "weights_total": 100.0,
        "passed_hint": None if score is None else bool(score >= threshold),
    }


def _pass_threshold(rubric: dict) -> float:
    try:
        return float((rubric or {}).get("pass_threshold", 60.0))
    except (TypeError, ValueError):
        return 60.0


def _preference(score_a, score_b) -> str:
    if score_a is None or score_b is None:
        return "tie"
    if abs(score_a - score_b) < 0.5:
        return "tie"
    return "A" if score_a > score_b else "B"


def _says_passed(payload, text) -> bool | None:
    for source in (json.dumps(payload, ensure_ascii=False) if payload else "", text or ""):
        if not source:
            continue
        if '"passed": true' in source.replace(" ", "") or '"passed":true' in source:
            return True
        if '"passed": false' in source.replace(" ", "") or '"passed":false' in source:
            return False
    lowered = (text or "").lower()
    if "通过" in (text or "") or "pass" in lowered:
        return True
    return None


def _uncertainty(payload) -> str:
    if isinstance(payload, dict):
        value = payload.get("uncertainty") or payload.get("uncertainties")
        if value:
            return str(value)[:2000]
        dims = _parse_dimensions(payload)
        notes = [
            str(v.get("uncertainty"))
            for v in dims.values()
            if isinstance(v, dict) and v.get("uncertainty")
        ]
        if notes:
            return "；".join(notes)[:2000]
    return "模型未给出不确定性说明：按不确定性处理，不视为独立事实核查"


def _render_artifacts(artifacts_text) -> str:
    if artifacts_text is None:
        return "（无产物）"
    if isinstance(artifacts_text, str):
        return artifacts_text[:60_000] or "（空产物）"
    parts = []
    if isinstance(artifacts_text, dict):
        artifacts_text = [{"path": k, "content": v} for k, v in artifacts_text.items()]
    for item in artifacts_text or []:
        if isinstance(item, dict):
            parts.append(
                f"--- {item.get('path') or 'artifact'} ---\n{str(item.get('content') or item.get('text') or '')[:20_000]}"
            )
        else:
            parts.append(str(item)[:20_000])
    return "\n".join(parts) or "（无产物）"


def _total_tokens(usage: dict) -> int:
    total = usage.get("total_tokens")
    if total:
        return int(total)
    return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)


def _same_model(a: str, b: str) -> bool:
    norm = lambda s: s.strip().lower().replace(" ", "")  # noqa: E731
    left, right = norm(a), norm(b)
    return bool(left) and bool(right) and (left == right or left in right or right in left)


def judge_input_hash(system: str, user: str) -> str:
    """裁判输入的 hash（报告可用它证明评审输入不含版本身份）。"""
    return canonical_hash({"system": system, "user": user})
