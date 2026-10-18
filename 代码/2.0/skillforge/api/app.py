from __future__ import annotations

import json
import mimetypes
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..core.errors import AppError, InvalidInput, to_error_dict
from .security import CSRF_COOKIE, SessionSecurity

WEB_ROOT = Path(__file__).with_name("web")

# (方法, 路由正则, AppServices 方法名, 是否读 JSON body)
ROUTES: list[tuple[str, str, str, bool]] = [
    ("GET", r"^/api/health$", "health", False),
    ("GET", r"^/api/doctor$", "doctor", False),
    ("GET", r"^/api/settings$", "settings_snapshot", False),
    ("GET", r"^/api/model$", "get_model_config", False),
    ("PUT", r"^/api/model$", "set_model_config", True),
    ("POST", r"^/api/model/check$", "check_model", True),
    ("GET", r"^/api/workspaces$", "list_workspaces", False),
    ("POST", r"^/api/workspaces$", "add_workspace", True),
    ("DELETE", r"^/api/workspaces/(?P<workspace_id>[^/]+)$", "remove_workspace", False),
    ("POST", r"^/api/workspaces/(?P<workspace_id>[^/]+)/check$", "check_workspace", True),
    ("GET", r"^/api/skills$", "list_skills", False),
    ("POST", r"^/api/skills/import$", "import_skill", True),
    ("POST", r"^/api/skills/draft$", "create_skill_draft", True),
    ("GET", r"^/api/skills/(?P<skill_id>[^/]+)$", "get_skill", False),
    ("POST", r"^/api/skills/(?P<skill_id>[^/]+)/archive$", "archive_skill", True),
    ("POST", r"^/api/skills/(?P<skill_id>[^/]+)/restore$", "restore_skill", True),
    ("POST", r"^/api/skills/(?P<skill_id>[^/]+)/export$", "export_skill", True),
    ("GET", r"^/api/skills/(?P<skill_id>[^/]+)/versions$", "list_versions", False),
    ("GET", r"^/api/skills/(?P<skill_id>[^/]+)/versions/(?P<version_id>[^/]+)/files$", "list_version_files", False),
    ("GET", r"^/api/skills/(?P<skill_id>[^/]+)/versions/(?P<version_id>[^/]+)/file$", "read_version_file", False),
    ("POST", r"^/api/skills/(?P<skill_id>[^/]+)/rollback$", "rollback_skill", True),
    ("POST", r"^/api/skills/(?P<skill_id>[^/]+)/manual-trial$", "activate_manual_trial", True),
    ("GET", r"^/api/runs$", "list_runs", False),
    ("POST", r"^/api/runs$", "create_run", True),
    ("GET", r"^/api/runs/(?P<run_id>[^/]+)$", "get_run", False),
    ("POST", r"^/api/runs/(?P<run_id>[^/]+)/cancel$", "cancel_run", True),
    ("POST", r"^/api/runs/(?P<run_id>[^/]+)/resume$", "resume_run", True),
    ("POST", r"^/api/runs/(?P<run_id>[^/]+)/feedback$", "submit_feedback", True),
    ("GET", r"^/api/feedback$", "list_feedback", False),
    ("POST", r"^/api/feedback$", "submit_feedback", True),
    ("POST", r"^/api/feedback/(?P<feedback_id>[^/]+)/withdraw$", "withdraw_feedback", True),
    ("GET", r"^/api/lessons$", "list_lessons", False),
    ("POST", r"^/api/lessons/(?P<lesson_id>[^/]+)/ignore$", "ignore_lesson", True),
    ("GET", r"^/api/learning/settings$", "get_learning_settings", False),
    ("PUT", r"^/api/learning/settings$", "set_learning_settings", True),
    ("GET", r"^/api/proposals$", "list_proposals", False),
    ("POST", r"^/api/proposals$", "propose_change", True),
    ("GET", r"^/api/proposals/(?P<proposal_id>[^/]+)$", "get_proposal", False),
    ("POST", r"^/api/proposals/(?P<proposal_id>[^/]+)/edit$", "edit_candidate", True),
    ("POST", r"^/api/proposals/(?P<proposal_id>[^/]+)/reject$", "reject_proposal", True),
    ("POST", r"^/api/proposals/(?P<proposal_id>[^/]+)/evaluate$", "start_evaluation", True),
    ("POST", r"^/api/proposals/(?P<proposal_id>[^/]+)/activate$", "activate_proposal", True),
    ("GET", r"^/api/eval/suites$", "list_suites", False),
    ("POST", r"^/api/eval/suites$", "create_suite", True),
    ("GET", r"^/api/eval/suites/(?P<suite_id>[^/]+)$", "get_suite", False),
    ("POST", r"^/api/eval/suites/(?P<suite_id>[^/]+)/freeze$", "freeze_suite", True),
    ("GET", r"^/api/eval/jobs$", "list_eval_jobs", False),
    ("GET", r"^/api/eval/jobs/(?P<job_id>[^/]+)$", "get_eval_job", False),
    ("POST", r"^/api/eval/jobs/(?P<job_id>[^/]+)/cancel$", "cancel_eval_job", True),
    ("POST", r"^/api/eval/jobs/(?P<job_id>[^/]+)/resume$", "resume_eval_job", True),
    ("GET", r"^/api/jobs$", "list_jobs", False),
    ("POST", r"^/api/jobs/(?P<job_id>[^/]+)/cancel$", "cancel_job", True),
    # ---------------------------------------------------------- P1 个人记忆
    ("GET", r"^/api/memory$", "memory_status", False),
    ("GET", r"^/api/memory/settings$", "memory_settings", False),
    ("PUT", r"^/api/memory/settings$", "set_memory_settings", True),
    ("POST", r"^/api/memory/remember$", "remember", True),
    ("GET", r"^/api/memory/logs$", "list_memory_logs", False),
    ("POST", r"^/api/memory/logs/(?P<log_id>[^/]+)/withdraw$", "withdraw_memory_log", True),
    ("POST", r"^/api/memory/logs/(?P<log_id>[^/]+)/delete$", "delete_memory_log", True),
    ("GET", r"^/api/memory/topics$", "list_memory_topics", False),
    ("GET", r"^/api/memory/topics/(?P<topic_id>[^/]+)$", "get_memory_topic", False),
    ("PUT", r"^/api/memory/topics/(?P<topic_id>[^/]+)$", "update_memory_topic", True),
    ("POST", r"^/api/memory/topics/(?P<topic_id>[^/]+)/confirm$", "confirm_memory_topic", True),
    ("POST", r"^/api/memory/topics/(?P<topic_id>[^/]+)/withdraw$", "withdraw_memory_topic", True),
    ("POST", r"^/api/memory/topics/(?P<topic_id>[^/]+)/delete$", "delete_memory_topic", True),
    ("GET", r"^/api/memory/index$", "memory_index", False),
    ("POST", r"^/api/memory/index/rebuild$", "memory_rebuild_index", True),
    ("GET", r"^/api/memory/recall$", "memory_recall", False),
    ("GET", r"^/api/memory/recalls$", "memory_history", False),
    ("POST", r"^/api/memory/recalls/(?P<recall_id>[^/]+)/helpful$", "memory_helpful", True),
    ("GET", r"^/api/memory/conflicts$", "memory_conflicts", False),
    ("GET", r"^/api/memory/sessions$", "memory_sessions", False),
    ("POST", r"^/api/memory/sessions/close$", "close_memory_session", True),
    ("GET", r"^/api/memory/dream$", "memory_dream_preview", False),
    ("POST", r"^/api/memory/dream$", "memory_dream_start", True),
    ("POST", r"^/api/memory/dream/(?P<dream_job_id>[^/]+)/run$", "memory_dream_run", True),
    ("POST", r"^/api/memory/expire$", "memory_expire", True),
    ("POST", r"^/api/memory/export$", "memory_export", True),
    ("GET", r"^/api/metrics$", "metrics", False),
    ("GET", r"^/api/audit$", "list_audit", False),
    ("GET", r"^/api/data/usage$", "data_usage", False),
    ("POST", r"^/api/data/backup$", "backup", True),
    ("POST", r"^/api/data/restore$", "restore", True),
    ("POST", r"^/api/data/export$", "export_bundle", True),
    ("POST", r"^/api/data/clear$", "clear_data", True),
    ("GET", r"^/api/data/retention$", "retention_policy", False),
    ("PUT", r"^/api/data/retention$", "set_retention", True),
    ("GET", r"^/api/data/retention/preview$", "retention_preview", False),
    ("POST", r"^/api/data/retention/apply$", "apply_retention", True),
    ("GET", r"^/api/runs/(?P<run_id>[^/]+)/evidence$", "evidence_state", False),
    ("POST", r"^/api/events/page-view$", "record_page_view", True),
]

_COMPILED = [(method, re.compile(pattern), name, body) for method, pattern, name, body in ROUTES]

_INT_QUERY_KEYS = {"limit", "offset", "expected_revision", "repetition", "revision"}
_BOOL_QUERY_KEYS = {"dry_run", "include_body", "unverified_ack", "include_archived", "confirm",
                    "enabled", "paused", "helpful", "create_topic"}


def build_server(services, config) -> ThreadingHTTPServer:
    security = SessionSecurity(config.host)
    handler = _make_handler(services, security)
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    server.security = security  # type: ignore[attr-defined]
    return server


def serve(services, config, *, on_ready=None) -> None:
    server = build_server(services, config)
    url = f"http://{config.host}:{server.server_address[1]}/"
    if on_ready is not None:
        on_ready(url, server)
    else:  # pragma: no cover - 人工启动路径
        print(f"Skills 自进化框架本地工作台已启动：{url}")
        print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.shutdown()
        server.server_close()


def _make_handler(services, security: SessionSecurity):
    class Handler(BaseHTTPRequestHandler):
        server_version = "skillforge/2.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 精简日志，避免输出正文
            return

        # --------------------------------------------------------- 方法分发
        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_HEAD(self):  # noqa: N802
            self._handle("HEAD")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def do_PUT(self):  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self):  # noqa: N802
            self._handle("DELETE")

        def do_OPTIONS(self):  # noqa: N802
            self._send_json(200, {"ok": True})

        # --------------------------------------------------------- 主流程
        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            try:
                security.check_request(method, path, dict(self.headers.items()))
            except AppError as exc:
                self._send_error(exc)
                return
            if path.startswith("/api/"):
                self._handle_api(method, path, parse_qs(parsed.query))
                return
            self._handle_static(method, path)

        def _handle_api(self, method: str, path: str, query: dict) -> None:
            try:
                body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
                params = self._query_params(query)
                for route_method, pattern, name, needs_body in _COMPILED:
                    if route_method != method:
                        continue
                    match = pattern.match(path)
                    if not match:
                        continue
                    call_params = dict(params)
                    call_params.update(match.groupdict())
                    if needs_body and body:
                        if not isinstance(body, dict):
                            raise InvalidInput("请求体必须是 JSON 对象")
                        call_params.update(body)
                    handler = getattr(services, name, None)
                    if handler is None:  # pragma: no cover - 路由与实现不一致时暴露
                        raise InvalidInput(f"接口未实现: {name}")
                    result = handler(**call_params)
                    self._send_json(200, result if isinstance(result, dict) else {"result": result})
                    return
                self._send_error(InvalidInput(f"未知接口: {method} {path}"), status=404)
            except AppError as exc:
                self._send_error(exc)
            except TypeError as exc:
                self._send_error(InvalidInput(f"参数不匹配: {exc}"), status=400)
            except Exception as exc:  # noqa: BLE001 - 统一转成错误契约
                payload = to_error_dict(exc)
                payload["traceback"] = traceback.format_exc(limit=3)
                self._send_json(500, {"error": payload})
                return

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw.strip():
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise InvalidInput(f"请求体不是合法 JSON: {exc}") from exc

        @staticmethod
        def _query_params(query: dict) -> dict:
            params: dict = {}
            for key, values in query.items():
                value = values[-1] if values else ""
                if key in _INT_QUERY_KEYS:
                    try:
                        params[key] = int(value)
                    except ValueError:
                        params[key] = value
                elif key in _BOOL_QUERY_KEYS:
                    params[key] = str(value).lower() in ("1", "true", "yes", "on")
                else:
                    params[key] = value
            return params

        # --------------------------------------------------------- 静态资源
        def _handle_static(self, method: str, path: str) -> None:
            if method not in ("GET", "HEAD"):
                self._send_error(InvalidInput("静态资源只支持 GET"), status=405)
                return
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (WEB_ROOT / rel).resolve()
            if not str(target).startswith(str(WEB_ROOT.resolve())) or not target.is_file():
                self._send_error(InvalidInput("资源不存在"), status=404)
                return
            data = target.read_bytes()
            mime, _ = mimetypes.guess_type(str(target))
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", security.cookie_header())
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'")
            self.end_headers()
            if method == "GET":
                self.wfile.write(data)

        # --------------------------------------------------------- 响应
        def _send_json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", security.cookie_header())
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, exc: Exception, status: int | None = None) -> None:
            payload = to_error_dict(exc)
            code = status or getattr(exc, "http_status", 500)
            self._send_json(code, {"error": payload})

    return Handler
