from __future__ import annotations

import hmac
import re
import secrets

from ..core.errors import PermissionDenied

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE = "skillforge_csrf"
REQUESTED_WITH = "X-Requested-With"


class SessionSecurity:
    def __init__(self, host: str = "127.0.0.1", token: str | None = None) -> None:
        self.bind_host = host
        self.token = token or secrets.token_urlsafe(32)

    # ------------------------------------------------------------- 校验
    @staticmethod
    def _host_name(value: str | None) -> str | None:
        if not value:
            return None
        text = value.strip()
        if text.startswith("["):  # IPv6 字面量 [::1]:8765
            end = text.find("]")
            return text[: end + 1].lower() if end > 0 else text.lower()
        return text.split(":", 1)[0].lower()

    def check_host(self, host_header: str | None) -> bool:
        name = self._host_name(host_header)
        return name in LOOPBACK_HOSTS if name else False

    def _origin_allowed(self, value: str | None) -> bool:
        if not value:
            return False
        text = value.strip().lower()
        if text in ("null",):
            return False
        match = re.match(r"^https?://([^/]+)", text)
        if not match:
            return False
        authority = match.group(1)
        # 允许 http://127.0.0.1:port / http://localhost:port / http://[::1]:port
        return self._host_name(authority) in LOOPBACK_HOSTS

    def check_origin(self, origin: str | None) -> bool:
        return self._origin_allowed(origin)

    def check_csrf(self, headers: dict, method: str) -> bool:
        if method.upper() not in WRITE_METHODS:
            return True
        provided = _header(headers, CSRF_HEADER) or ""
        expected = self.token
        if not provided:
            return False
        try:
            return hmac.compare_digest(str(provided), expected)
        except (TypeError, ValueError):  # pragma: no cover
            return False

    def check_request(self, method: str, path: str, headers: dict) -> None:
        """按顺序校验 Host → Origin/Referer → CSRF；失败抛 PermissionDenied。"""
        host_header = _header(headers, "Host")
        if not self.check_host(host_header):
            raise PermissionDenied(
                f"拒绝非本机 Host 的请求: {host_header!r}",
                field_errors={"host": "not_loopback"},
            )
        origin = _header(headers, "Origin")
        referer = _header(headers, "Referer")
        if origin is not None:
            if not self.check_origin(origin):
                raise PermissionDenied(
                    f"拒绝来自其他站点的请求: {origin}",
                    field_errors={"origin": "cross_origin"},
                )
        elif referer is not None:
            if not self._origin_allowed(referer):
                raise PermissionDenied(
                    f"拒绝来自其他站点的请求: {referer}",
                    field_errors={"referer": "cross_origin"},
                )
        elif method.upper() in WRITE_METHODS:
            # 写操作必须能证明来源：浏览器同源请求会带 Origin 或 Referer；
            # 本机 CLI/脚本调用可使用 X-Requested-With + CSRF 令牌。
            if not _header(headers, REQUESTED_WITH):
                raise PermissionDenied(
                    "写操作缺少来源标识（Origin/Referer 或 X-Requested-With）",
                    field_errors={"origin": "missing_origin"},
                )
        if not self.check_csrf(headers, method):
            raise PermissionDenied(
                "CSRF 校验失败：缺少或错误的会话令牌",
                field_errors={"csrf": "invalid_token"},
            )

    # ------------------------------------------------------------- 浏览器侧
    def cookie_header(self) -> str:
        # 不用 HttpOnly：前端需要读取令牌并放入 X-CSRF-Token 头。
        return f"{CSRF_COOKIE}={self.token}; Path=/; SameSite=Strict"

    def bootstrap_payload(self) -> dict:
        return {"token": self.token, "cookie_name": CSRF_COOKIE, "header_name": CSRF_HEADER}


def _header(headers: dict, name: str) -> str | None:
    if not headers:
        return None
    if name in headers:
        return headers[name]
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return value
    return None
