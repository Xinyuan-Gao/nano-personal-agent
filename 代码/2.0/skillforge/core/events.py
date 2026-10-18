from __future__ import annotations

import json
import sqlite3
from typing import Any

from .clock import new_correlation_id, utc_now
from .db import Store
from .ids import new_id

SENSITIVE_EVENT_KEYS = {
    "request",
    "content",
    "body",
    "prompt",
    "text",
    "file_content",
    "api_key",
    "secret",
    "token_value",
    "password",
}


class EventLog:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------- run events
    def run_event(
        self,
        run_id: str,
        name: str,
        payload: dict | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        payload = payload or {}
        now = utc_now()
        def _write(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM run_event WHERE run_id = ?", (run_id,)
            ).fetchone()
            seq = int(row["s"]) + 1
            connection.execute(
                "INSERT INTO run_event (id, run_id, seq, name, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    new_id("evt"),
                    run_id,
                    seq,
                    name,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                ),
            )
            return seq

        if conn is not None:
            return _write(conn)
        with self.store.tx() as connection:
            return _write(connection)

    def list_run_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM run_event WHERE run_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
            (run_id, int(after_seq), int(limit)),
        )
        return [_decode(r, ("payload",)) for r in rows]

    # ------------------------------------------------------------- 审计
    def audit(
        self,
        action: str,
        entity_type: str,
        entity_id: str,
        *,
        actor: str = "owner",
        before: Any = None,
        after: Any = None,
        permission_scope: Any = None,
        reason: str | None = None,
        correlation_id: str | None = None,
        profile_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        audit_id = new_id("aud")
        row = (
            audit_id,
            profile_id,
            entity_type,
            entity_id,
            action,
            actor,
            json.dumps(before or {}, ensure_ascii=False, default=str),
            json.dumps(after or {}, ensure_ascii=False, default=str),
            json.dumps(permission_scope or {}, ensure_ascii=False, default=str),
            reason,
            correlation_id or new_correlation_id(),
            utc_now(),
        )
        sql = (
            "INSERT INTO audit_event (id, profile_id, entity_type, entity_id, action, actor, "
            "before_state, after_state, permission_scope, reason, correlation_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, row)
        else:
            with self.store.tx() as connection:
                connection.execute(sql, row)
        return audit_id

    def list_audit(
        self, *, limit: int = 200, entity_id: str | None = None, action: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM audit_event"
        clauses: list[str] = []
        params: list = []
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        rows = self.store.query(sql, params)
        return [_decode(r, ("before_state", "after_state", "permission_scope")) for r in rows]

    # ------------------------------------------------------------- 产品事件
    def product_event(
        self,
        name: str,
        params: dict | None = None,
        *,
        event_id: str | None = None,
        profile_id: str | None = None,
    ) -> None:
        safe = {k: v for k, v in (params or {}).items() if k not in SENSITIVE_EVENT_KEYS}
        eid = event_id or new_id("pev")
        with self.store.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO product_event (id, event_id, name, profile_id, params, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    new_id("pevrow"),
                    eid,
                    name,
                    profile_id,
                    json.dumps(safe, ensure_ascii=False, default=str),
                    utc_now(),
                ),
            )

    def list_product_events(self, *, limit: int = 500, name: str | None = None) -> list[dict]:
        sql = "SELECT * FROM product_event"
        params: list = []
        if name:
            sql += " WHERE name = ?"
            params.append(name)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [_decode(r, ("params",)) for r in self.store.query(sql, params)]


def _decode(row: sqlite3.Row, json_fields: tuple[str, ...]) -> dict:
    data = {k: row[k] for k in row.keys()}
    for field in json_fields:
        if field in data and isinstance(data[field], str):
            try:
                data[field] = json.loads(data[field])
            except (TypeError, ValueError):
                pass
    return data
