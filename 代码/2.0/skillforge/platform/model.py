from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from ..core.errors import AppError, InvalidInput, ModelError
from ..core.ids import canonical_hash

#: 可重试的 HTTP 状态：429 与 5xx
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})
#: 明确不重试（认证 / 参数 / 不存在）
NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 405, 422})
MAX_RETRIES = 2
BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 30.0
#: ``Retry-After`` 的上限，避免异常值导致长时间挂起
RETRY_AFTER_CAP_S = 60.0

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_ACTION_KEYS = ("tool", "tool_name", "name", "action", "function")
_ARGUMENT_KEYS = ("arguments", "args", "parameters", "input")


class TransportError(Exception):
    """transport 层错误：把 HTTP/网络故障统一成可判定「是否重试」的异常。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        retryable: bool = False,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.retryable = retryable
        self.body = body


@dataclass
class ModelConfig:
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key_ref: str | None = None
    temperature: float = 0.2
    max_output_tokens: int = 2048
    timeout_s: float = 60.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_ref": self.api_key_ref,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "timeout_s": self.timeout_s,
            "extra": dict(self.extra or {}),
        }

    @classmethod
    def from_row(cls, row) -> "ModelConfig":
        if row is None:
            return cls()
        data = {k: row[k] for k in row.keys()} if hasattr(row, "keys") else dict(row)
        extra = data.get("extra") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except (TypeError, ValueError):
                extra = {}
        return cls(
            provider=data.get("provider") or "openai_compatible",
            base_url=data.get("base_url") or "",
            model=data.get("model") or "",
            api_key_ref=data.get("api_key_ref"),
            temperature=float(data.get("temperature") if data.get("temperature") is not None else 0.2),
            max_output_tokens=int(data.get("max_output_tokens") or 2048),
            timeout_s=float(data.get("timeout_s") or 60.0),
            extra=dict(extra or {}),
        )

    def fingerprint_payload(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "extra": self.extra or {},
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "raw_arguments": self.raw_arguments,
        }

    def to_wire(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments or "{}"},
        }


@dataclass
class ModelReply:
    text: str
    tool_calls: list[ToolCall]
    usage: dict
    model_id: str | None
    finish_reason: str | None
    usage_known: bool
    raw: dict
    latency_ms: int

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "usage": self.usage,
            "model_id": self.model_id,
            "finish_reason": self.finish_reason,
            "usage_known": self.usage_known,
            "latency_ms": self.latency_ms,
        }


@dataclass
class ChatMessage:
    role: str
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_wire(self) -> dict:
        wire: dict = {"role": self.role, "content": self.content or ""}
        if self.tool_calls:
            wire["tool_calls"] = [c.to_wire() for c in self.tool_calls]
            if wire["content"] is None:
                wire["content"] = ""
        if self.tool_call_id:
            wire["tool_call_id"] = self.tool_call_id
        if self.name:
            wire["name"] = self.name
        return wire

    @classmethod
    def from_wire(cls, data: dict) -> "ChatMessage":
        calls = None
        if data.get("tool_calls"):
            calls = [
                ToolCall(
                    id=str(c.get("id") or ""),
                    name=str((c.get("function") or {}).get("name") or ""),
                    arguments=_maybe_json((c.get("function") or {}).get("arguments")),
                    raw_arguments=str((c.get("function") or {}).get("arguments") or ""),
                )
                for c in data["tool_calls"]
            ]
        return cls(
            role=data.get("role") or "user",
            content=data.get("content") or "",
            tool_calls=calls,
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )


class ModelClient:
    def __init__(
        self,
        config: ModelConfig,
        credentials,
        transport: Callable[[dict, dict], dict] | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self._transport = transport
        #: 测试可替换的 sleep 钩子（用于断言退避与 Retry-After，不真的等待）
        self._sleep: Callable[[float], None] = time.sleep
        #: 记录每次退避等待的秒数，便于诊断与测试
        self.sleep_history: list[float] = []

    # ------------------------------------------------------------- 请求
    def _endpoint(self) -> str:
        base = (self.config.base_url or "").rstrip("/")
        if not base:
            raise InvalidInput("模型 base_url 未配置", field_errors={"base_url": "required"})
        return f"{base}/chat/completions"

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "skillforge/1.0 (+stdlib)",
        }
        ref = self.config.api_key_ref
        if ref:
            secret = self.credentials.get(ref) if self.credentials is not None else None
            if secret:
                headers["Authorization"] = f"Bearer {secret}"
        return headers

    def _build_payload(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None,
        max_output_tokens: int | None,
        response_format: dict | None,
    ) -> dict:
        limit = int(max_output_tokens or self.config.max_output_tokens or 2048)
        payload: dict = dict(self.config.extra or {})
        payload.update(
            {
                "model": self.config.model,
                "messages": [m.to_wire() for m in messages],
                "temperature": self.config.temperature,
                "max_tokens": limit,
            }
        )
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = payload.get("tool_choice") or "auto"
        if response_format:
            payload["response_format"] = response_format
        return payload

    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> ModelReply:
        """发一次补全请求（含最多 2 次重试），返回结构化回复。"""
        if not messages:
            raise InvalidInput("messages 不能为空", field_errors={"messages": "empty"})
        if not self.config.model:
            raise InvalidInput("模型名未配置", field_errors={"model": "required"})
        payload = self._build_payload(messages, tools, max_output_tokens, response_format)
        headers = self._headers()

        attempt = 0
        last_error: TransportError | None = None
        while True:
            started = time.monotonic()
            try:
                raw = self._call_transport(payload, headers)
                latency_ms = int((time.monotonic() - started) * 1000)
                return self._parse_response(raw, latency_ms)
            except TransportError as exc:
                last_error = exc
                if exc.retryable and attempt < MAX_RETRIES:
                    delay = self._delay_for(exc, attempt)
                    self.sleep_history.append(delay)
                    self._sleep(delay)
                    attempt += 1
                    continue
                raise self._model_error(exc) from None

    def _call_transport(self, payload: dict, headers: dict) -> dict:
        if self._transport is not None:
            result = self._transport(payload, headers)
            if isinstance(result, tuple):  # 兼容 (status, body) 形式
                status, body = result
                if int(status) >= 400:
                    raise TransportError(
                        f"HTTP {status}", status=int(status), retryable=int(status) in RETRYABLE_STATUSES
                    )
                return body
            if not isinstance(result, dict):
                raise TransportError("transport 返回值必须是 dict", retryable=False)
            return result
        return self._urllib_transport(payload, headers)

    def _urllib_transport(self, payload: dict, headers: dict) -> dict:
        url = self._endpoint()
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=float(self.config.timeout_s)) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - 读取错误体失败
                body = ""
            status = int(exc.code)
            headers_in = exc.headers
            retry_after = _parse_retry_after(headers_in.get("Retry-After") if headers_in else None)
            retryable = status not in NON_RETRYABLE_STATUSES and (
                status in RETRYABLE_STATUSES or status >= 500
            )
            raise TransportError(
                f"模型服务返回 HTTP {status}", status=status, retry_after=retry_after,
                retryable=retryable, body=_truncate(body),
            ) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise TransportError(f"模型服务连接失败: {exc}", retryable=True) from None
        try:
            return json.loads(body)
        except (TypeError, ValueError):
            raise TransportError(
                "模型响应不是合法 JSON", retryable=False, body=_truncate(body)
            ) from None

    def _delay_for(self, exc: TransportError, attempt: int) -> float:
        if exc.retry_after is not None:
            return max(0.0, min(float(exc.retry_after), RETRY_AFTER_CAP_S))
        return min(BACKOFF_BASE_S * (2 ** attempt), BACKOFF_MAX_S)

    def _model_error(self, exc: TransportError) -> ModelError:
        detail = exc.message
        if exc.status == 401 or exc.status == 403:
            detail = f"{detail}（认证失败，请检查凭据）"
        elif exc.status == 404:
            detail = f"{detail}（endpoint 不存在，请检查 base_url）"
        elif exc.status == 400:
            detail = f"{detail}（参数错误）"
        return ModelError(
            detail,
            retryable=bool(exc.retryable),
            field_errors={"status": str(exc.status)} if exc.status else {},
        )

    # ------------------------------------------------------------- 响应解析
    def _parse_response(self, raw: dict, latency_ms: int) -> ModelReply:
        if not isinstance(raw, dict):
            raise ModelError("模型响应格式非法", retryable=False)
        if raw.get("error"):
            err = raw["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise ModelError(f"模型服务返回错误: {message}", retryable=False)
        choices = raw.get("choices") or []
        if not choices:
            raise ModelError("模型响应缺少 choices", retryable=False)
        choice = choices[0] or {}
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # 部分兼容实现返回分段内容
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in text
            )
        tool_calls = _parse_native_tool_calls(message.get("tool_calls"))
        if not tool_calls:
            fallback = parse_action_json(text)
            if fallback:
                tool_calls = fallback
        usage_raw = raw.get("usage")
        usage_known = isinstance(usage_raw, dict) and bool(usage_raw)
        usage = dict(usage_raw) if usage_known else {}
        return ModelReply(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            model_id=raw.get("model"),
            finish_reason=choice.get("finish_reason"),
            usage_known=usage_known,
            raw=raw,
            latency_ms=int(latency_ms),
        )

    # ------------------------------------------------------------- 元信息
    def fingerprint(self) -> str:
        """冻结配置指纹：不含密钥。"""
        return canonical_hash(self.config.fingerprint_payload())

    def describe(self) -> dict:
        """不含密钥的配置视图。"""
        data = self.config.to_dict()
        data["api_key_configured"] = bool(
            self.config.api_key_ref and self.credentials is not None
            and self.credentials.exists(self.config.api_key_ref)
        )
        data["fingerprint"] = self.fingerprint()
        return data

    def check_connection(self) -> dict:
        """发最小测试请求（明确会发送最小测试请求）。"""
        started = time.monotonic()
        try:
            reply = self.complete(
                [ChatMessage(role="user", content="ping")],
                max_output_tokens=min(16, int(self.config.max_output_tokens or 16)),
            )
        except AppError as exc:
            return {
                "ok": False,
                "model_id": None,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
            }
        except Exception as exc:  # pragma: no cover - 兜底，避免 UI 崩溃
            return {
                "ok": False,
                "model_id": None,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": {"code": "model_error", "message": str(exc), "retryable": False},
            }
        return {
            "ok": True,
            "model_id": reply.model_id,
            "latency_ms": reply.latency_ms,
            "error": None,
        }


# ------------------------------------------------------------------ 解析辅助
def parse_action_json(text: str) -> list[ToolCall] | None:
    """结构化文本动作兜底：解析 ```json 代码块或裸 JSON 对象。

    支持形态：

    - ``{"tool": "read_text", "arguments": {...}}``
    - ``{"action": "read_text", "args": {...}}``
    - ``{"name": "read_text", "parameters": {...}}``
    - ``{"tool_calls": [{"name": ..., "arguments": ...}]}``
    - 上述对象的 JSON 数组

    解析不出有效动作时返回 ``None``。
    """
    if not text or not isinstance(text, str):
        return None
    candidates: list[str] = []
    for match in _FENCE_RE.finditer(text):
        candidates.append(match.group(1).strip())
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        candidates.append(stripped)
    if not candidates:
        # 文本里夹带 JSON 对象或数组（allow 前后有解释性文字）
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start, end = stripped.find(open_ch), stripped.rfind(close_ch)
            if start != -1 and end > start:
                candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        calls = _calls_from_payload(payload)
        if calls:
            return calls
    return None


def _calls_from_payload(payload) -> list[ToolCall] | None:
    if isinstance(payload, list):
        calls: list[ToolCall] = []
        for item in payload:
            calls.extend(_calls_from_payload(item) or [])
        return calls or None
    if not isinstance(payload, dict):
        return None
    for key in ("tool_calls", "calls", "actions"):
        if isinstance(payload.get(key), list):
            return _calls_from_payload(payload[key])
    name = None
    for key in _ACTION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            name = value
            break
        if isinstance(value, dict) and value.get("name"):
            name = str(value["name"])
            break
    if not name:
        return None
    arguments: dict = {}
    for key in _ARGUMENT_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            arguments = value
            break
        if isinstance(value, str) and value.strip():
            arguments = _maybe_json(value)
            break
    raw = json.dumps(arguments, ensure_ascii=False)
    return [ToolCall(id=str(payload.get("id") or payload.get("call_id") or "call_text_1"),
                     name=name, arguments=arguments, raw_arguments=raw)]


def _parse_native_tool_calls(raw_calls) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if not isinstance(raw_calls, list):
        return calls
    for index, item in enumerate(raw_calls, start=1):
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = function.get("name") or item.get("name")
        if not name:
            continue
        raw_arguments = function.get("arguments")
        if raw_arguments is None:
            raw_arguments = item.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
            raw_text = json.dumps(arguments, ensure_ascii=False)
        else:
            raw_text = str(raw_arguments or "")
            arguments = _maybe_json(raw_text)
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{index}"),
                name=str(name),
                arguments=arguments,
                raw_arguments=raw_text,
            )
        )
    return calls


def _maybe_json(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_retry_after(value) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        # HTTP-date 形式不解析，退回默认退避
        return None


def _truncate(text: str, limit: int = 500) -> str:
    if text is None:
        return ""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"
