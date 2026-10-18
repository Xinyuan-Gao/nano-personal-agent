from __future__ import annotations

import json
from pathlib import Path

from ..core.clock import utc_now
from ..core.errors import InvalidInput
from ..core.ids import sha256_hex
from .scope import DEFAULT_SESSION, WORKING_STATUS_ACTIVE, WORKING_STATUS_CLOSED, WORKING_STATUS_INVALIDATED


class WorkingMemoryService:
    def __init__(self, store, repo, events, config=None, profile_id: str = "local") -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.config = config
        self.profile_id = profile_id

    # ------------------------------------------------------------------ 读写
    def ensure(self, session_id: str | None = None, workspace_id: str | None = None) -> dict:
        sid = (session_id or DEFAULT_SESSION).strip() or DEFAULT_SESSION
        row = self.repo.get("memory_working", sid, pk="session_id")
        if row is None:
            now = utc_now()
            with self.store.tx() as conn:
                conn.execute(
                    "INSERT INTO memory_working (id, profile_id, session_id, workspace_id, scope, "
                    "task_summary, recent_files, status, run_count, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'task', '', '[]', ?, 0, ?, ?)",
                    (f"mwk_{sid}", self.profile_id, sid, workspace_id, WORKING_STATUS_ACTIVE, now, now),
                )
            row = self.repo.get("memory_working", sid, pk="session_id")
        return _decode(row)

    def get(self, session_id: str | None = None) -> dict | None:
        sid = (session_id or DEFAULT_SESSION).strip() or DEFAULT_SESSION
        row = self.repo.get("memory_working", sid, pk="session_id")
        return _decode(row) if row else None

    def list_sessions(self, *, limit: int = 200) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM memory_working WHERE profile_id = ? ORDER BY updated_at DESC LIMIT ?",
            (self.profile_id, int(limit)),
        )
        return [_decode(row) for row in rows]

    def current(self, session_id: str | None, workspace_id: str, *, refresh: bool = True) -> dict | None:
        """返回当前 session 的可用工作记忆；已关闭 / 已失效 / 跨工作区一律不返回。"""
        memory = self.get(session_id)
        if not memory or memory.get("status") != WORKING_STATUS_ACTIVE:
            return None
        if memory.get("workspace_id") and workspace_id and memory["workspace_id"] != workspace_id:
            return None
        if refresh:
            memory = self.refresh_files(memory.get("session_id"), workspace_id=workspace_id)
        return memory

    # ------------------------------------------------------------------ 写入
    def capture_run(self, *, run_id: str, request: str, session_id: str | None = None,
                    workspace_id: str | None = None, output_text: str = "",
                    input_files: list | None = None) -> dict:
        """任务结束后更新任务摘要与最近文件；不创建任何长期主题。"""
        memory = self.ensure(session_id, workspace_id)
        if memory.get("status") != WORKING_STATUS_ACTIVE:
            return memory
        summary = _clip((request or "").strip(), 400)
        if output_text:
            summary = f"{summary}\n产出摘要：{_clip(output_text.strip(), 400)}"
        files = list(memory.get("recent_files") or [])
        known = {item.get("path") for item in files}
        roots = self._read_roots(workspace_id)
        for item in (input_files or [])[:20]:
            path = str((item or {}).get("path") or "")
            if not path or path in known:
                continue
            content_hash = item.get("content_hash") or item.get("sha256")
            if not content_hash:
                # 输入清单没有带 hash：按工作区根目录补算，否则新鲜度无法校验
                resolved = _resolve_candidate(path, roots)
                content_hash = _file_hash(resolved) if resolved is not None else None
            files.append({
                "path": path,
                "summary": f"{_clip(str(item.get('label') or ''), 60)}"
                           f"大小 {item.get('size', 0)} B",
                "content_hash": content_hash,
                "captured_at": utc_now(),
                "state": "current" if content_hash else "stale",
                "stale_reason": None if content_hash else "hash_unavailable",
            })
            known.add(path)
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE memory_working SET task_summary = ?, recent_files = ?, run_count = run_count + 1, "
                "updated_at = ? WHERE session_id = ?",
                (summary, json.dumps(files[-40:], ensure_ascii=False), utc_now(), memory["session_id"]),
            )
        # 事件只对真实存在的 run 写入（离线调用 / 单元测试不产生孤儿 run_event）
        if self.repo.get("run", run_id) is not None:
            self.events.run_event(
                run_id,
                "memory.working_updated",
                {"session_id": memory["session_id"], "files": len(files),
                 "status": WORKING_STATUS_ACTIVE},
            )
        return self.get(memory["session_id"]) or memory

    def refresh_files(self, session_id: str | None, *, workspace_id: str | None = None,
                      read_roots: list[str] | None = None) -> dict | None:
        """按文件内容 hash 校验短摘要新鲜度；变化的标记 ``stale``，消失的标记 ``missing``。"""
        memory = self.get(session_id)
        if not memory:
            return None
        files = list(memory.get("recent_files") or [])
        if not files:
            return memory
        roots = [Path(p) for p in (read_roots or [])]
        if not roots:
            roots = self._read_roots(workspace_id or memory.get("workspace_id"))
        changed = False
        for item in files:
            if item.get("state") not in ("current", None):
                continue
            resolved = _resolve_candidate(item.get("path"), roots)
            if resolved is None:
                # 相对路径且没有任何可校验根目录：无法确认新鲜度，标明未校验而不是假装有效
                item["state"] = "stale"
                item["stale_reason"] = "path_unresolved"
                changed = True
                continue
            actual = _file_hash(resolved)
            if actual is None:
                item["state"] = "missing"
                item["stale_reason"] = "file_missing"
                changed = True
            elif item.get("content_hash") and actual != item["content_hash"]:
                item["state"] = "stale"
                item["stale_reason"] = "content_changed"
                changed = True
        if changed:
            with self.store.tx() as conn:
                conn.execute(
                    "UPDATE memory_working SET recent_files = ?, updated_at = ? WHERE session_id = ?",
                    (json.dumps(files, ensure_ascii=False), utc_now(), memory["session_id"]),
                )
            memory = self.get(memory["session_id"])
        return memory

    def close(self, session_id: str | None = None) -> dict:
        """关闭 session：只标记状态，不生成长期事实。"""
        memory = self.get(session_id)
        if not memory:
            raise InvalidInput("session 不存在", field_errors={"session_id": "not_found"})
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE memory_working SET status = ?, closed_at = ?, updated_at = ? WHERE session_id = ?",
                (WORKING_STATUS_CLOSED, utc_now(), utc_now(), memory["session_id"]),
            )
        self.events.audit("memory.session_close", "memory_working", memory["session_id"],
                          after={"status": WORKING_STATUS_CLOSED})
        return self.get(memory["session_id"]) or memory

    def invalidate(self, session_id: str | None = None, *, reason: str = "") -> dict:
        memory = self.get(session_id)
        if not memory:
            raise InvalidInput("session 不存在", field_errors={"session_id": "not_found"})
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE memory_working SET status = ?, updated_at = ? WHERE session_id = ?",
                (WORKING_STATUS_INVALIDATED, utc_now(), memory["session_id"]),
            )
        self.events.audit("memory.session_invalidate", "memory_working", memory["session_id"],
                          after={"status": WORKING_STATUS_INVALIDATED}, reason=reason or None)
        return self.get(memory["session_id"]) or memory

    def _read_roots(self, workspace_id: str | None) -> list[Path]:
        if not workspace_id:
            return []
        ws = self.repo.get("workspace", workspace_id)
        if ws is None:
            return []
        return [Path(ws["root_path"])]

    def fresh_file_entries(self, memory: dict | None) -> list[dict]:
        """只返回仍然新鲜的最近文件（供注入 prompt）。"""
        return [item for item in ((memory or {}).get("recent_files") or [])
                if item.get("state") in ("current", None)][:10]


def _resolve_candidate(path: str | None, roots: list[Path]) -> Path | None:
    """把记录里的路径解析成可校验的绝对路径；无法定位时返回 None（不冒充已校验）。"""
    if not path:
        return None
    candidate = Path(str(path))
    if candidate.is_absolute():
        return candidate
    if not roots:
        return None
    for root in roots:
        target = root / candidate
        if target.exists():
            return target
    # 相对路径：按第一个已知根目录解析，由 hash 校验判定 missing（文件已消失）
    return roots[0] / candidate


def _file_hash(path: Path) -> str | None:
    try:
        return sha256_hex(path.read_bytes())
    except OSError:
        return None


def _decode(row) -> dict:
    data = {key: row[key] for key in row.keys()}
    if isinstance(data.get("recent_files"), str):
        try:
            data["recent_files"] = json.loads(data["recent_files"])
        except (TypeError, ValueError):
            data["recent_files"] = []
    return data


def _clip(text: str, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "…"
