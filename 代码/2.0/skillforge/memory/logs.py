from __future__ import annotations

import json

from ..core.clock import utc_now
from ..core.errors import InvalidInput
from ..core.ids import sha256_hex
from .scope import (
    LOG_STATUS_ACTIVE,
    LOG_STATUS_DELETED,
    LOG_STATUS_WITHDRAWN,
    SCOPE_PERSONAL,
    SCOPE_TASK,
    validate_scope,
)

SOURCE_USER_REQUEST = "user_request"
SOURCE_MODEL_MARK = "model_mark"
SOURCE_TASK_RESULT = "task_result"
SOURCE_DREAM = "dream"
SOURCES = (SOURCE_USER_REQUEST, SOURCE_MODEL_MARK, SOURCE_TASK_RESULT, SOURCE_DREAM)


class DailyLogService:
    def __init__(self, store, repo, events, config=None, profile_id: str = "local") -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.config = config
        self.profile_id = profile_id

    # ------------------------------------------------------------------ 写入
    def append(
        self,
        content: str,
        *,
        scope: str = SCOPE_TASK,
        workspace_id: str | None = None,
        source: str = SOURCE_USER_REQUEST,
        source_kind: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        tags: list | None = None,
        supersedes_id: str | None = None,
    ) -> dict:
        """追加一条日志；append-only，不修改既有记录。"""
        text = (content or "").strip()
        if not text:
            raise InvalidInput("记忆内容不能为空", field_errors={"content": "required"})
        source = source if source in SOURCES else SOURCE_USER_REQUEST
        kind = source_kind or ("model" if source == SOURCE_MODEL_MARK else
                               "objective" if source == SOURCE_TASK_RESULT else "user")
        resolved_scope = validate_scope(scope, workspace_id=workspace_id, default=SCOPE_TASK)
        if resolved_scope != SCOPE_PERSONAL and not workspace_id and resolved_scope == "workspace":
            raise InvalidInput("workspace 作用域必须指定 workspace_id")
        now = utc_now()
        log_id = f"mlg_{sha256_hex(f'{now}|{text}')[:20]}"
        row = {
            "id": log_id,
            "profile_id": self.profile_id,
            "workspace_id": workspace_id,
            "scope": resolved_scope,
            "day": now[:10],
            "content": text,
            "source": source,
            "source_kind": kind,
            "run_id": run_id,
            "session_id": session_id,
            "tags": json.dumps(list(tags or []), ensure_ascii=False),
            "content_hash": sha256_hex(text),
            "supersedes_id": supersedes_id,
            "status": LOG_STATUS_ACTIVE,
            "body_state": "available",
            "created_at": now,
        }
        with self.store.tx() as conn:
            existing = conn.execute("SELECT id FROM memory_log WHERE id = ?", (log_id,)).fetchone()
            if existing:
                # 同一时间同一原文重复追加：返回既有记录，不产生重复行
                return self.get(log_id) or row
            self.repo.insert("memory_log", row, conn=conn)
        self.events.product_event(
            "memory_appended",
            {"memory_id": log_id, "scope": resolved_scope, "source": source,
             "layer": "daily", "status": LOG_STATUS_ACTIVE},
        )
        return self.get(log_id) or row

    # ------------------------------------------------------------------ 读取
    def get(self, log_id: str) -> dict | None:
        row = self.repo.get("memory_log", log_id)
        return _decode(row) if row else None

    def list(
        self,
        *,
        day: str | None = None,
        scope: str | None = None,
        workspace_id: str | None = None,
        status: str = LOG_STATUS_ACTIVE,
        source: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT * FROM memory_log WHERE profile_id = ?"
        params: list = [self.profile_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if day:
            sql += " AND day = ?"
            params.append(day)
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if source:
            sql += " AND source = ?"
            params.append(source)
        if workspace_id:
            sql += " AND (workspace_id IS NULL OR workspace_id = ?)"
            params.append(workspace_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [_decode(row) for row in self.store.query(sql, params)]

    def days(self, *, limit: int = 60) -> list[dict]:
        rows = self.store.query(
            "SELECT day, COUNT(*) AS count FROM memory_log WHERE profile_id = ? AND status = ? "
            "GROUP BY day ORDER BY day DESC LIMIT ?",
            (self.profile_id, LOG_STATUS_ACTIVE, int(limit)),
        )
        return [{"day": row["day"], "count": int(row["count"])} for row in rows]

    def session_ids(self, *, since: str | None = None) -> list[str]:
        """出现过的 session（dream 门槛用）。"""
        sql = ("SELECT DISTINCT session_id FROM memory_log WHERE profile_id = ? "
               "AND session_id IS NOT NULL AND status = ?")
        params: list = [self.profile_id, LOG_STATUS_ACTIVE]
        if since:
            sql += " AND created_at > ?"
            params.append(since)
        return [row["session_id"] for row in self.store.query(sql, params)]

    # ------------------------------------------------------------------ 生命周期
    def withdraw(self, log_id: str, *, reason: str = "") -> dict:
        record = self.get(log_id)
        if not record:
            raise InvalidInput("日志不存在", field_errors={"log_id": "not_found"})
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE memory_log SET status = ?, withdrawn_at = ? WHERE id = ?",
                (LOG_STATUS_WITHDRAWN, utc_now(), log_id),
            )
        self.events.audit("memory.withdraw", "memory_log", log_id,
                          before={"status": record.get("status")},
                          after={"status": LOG_STATUS_WITHDRAWN}, reason=reason or None)
        return self.get(log_id) or record

    def delete(self, log_id: str, *, reason: str = "") -> dict:
        """删除正文：标记 ``deleted`` 并清空原文，删除后不可再被召回。"""
        record = self.get(log_id)
        if not record:
            raise InvalidInput("日志不存在", field_errors={"log_id": "not_found"})
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE memory_log SET status = ?, content = '', body_state = 'deleted', "
                "expired_at = ? WHERE id = ?",
                (LOG_STATUS_DELETED, utc_now(), log_id),
            )
            conn.execute("DELETE FROM memory_topic_source WHERE source_kind = 'log' AND source_id = ?",
                         (log_id,))
        self.events.audit("memory.delete", "memory_log", log_id,
                          before={"status": record.get("status")}, after={"status": LOG_STATUS_DELETED},
                          reason=reason or None)
        deleted = self.get(log_id) or record
        deleted["deleted"] = True
        return deleted

    def expire(self, *, days: int, now: str | None = None) -> dict:
        """按保留期限标记正文过期：正文清空、状态保留，历史仍可定位（口径）。"""
        if days is None or int(days) <= 0:
            return {"expired": 0, "days": None}
        from datetime import datetime, timedelta, timezone

        base = datetime.strptime(now or utc_now(), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        cutoff = (base - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rows = self.store.query(
            "SELECT id FROM memory_log WHERE profile_id = ? AND status = ? AND body_state = 'available' "
            "AND created_at < ?",
            (self.profile_id, LOG_STATUS_ACTIVE, cutoff),
        )
        ids = [row["id"] for row in rows]
        if ids:
            with self.store.tx() as conn:
                for log_id in ids:
                    conn.execute(
                        "UPDATE memory_log SET content = '', body_state = 'expired', expired_at = ? "
                        "WHERE id = ?",
                        (utc_now(), log_id),
                    )
        return {"expired": len(ids), "days": int(days), "cutoff": cutoff}


def _decode(row) -> dict:
    data = {key: row[key] for key in row.keys()}
    if isinstance(data.get("tags"), str):
        try:
            data["tags"] = json.loads(data["tags"])
        except (TypeError, ValueError):
            data["tags"] = []
    return data
