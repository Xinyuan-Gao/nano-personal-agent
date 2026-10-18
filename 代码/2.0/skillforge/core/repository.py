from __future__ import annotations

import json
import sqlite3
from typing import Callable

from .clock import utc_now
from .db import Store
from .errors import InvalidInput, InvalidState
from .ids import canonical_hash

_TABLE_COLUMNS_CACHE: dict[str, set[str]] = {}


class Repo:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------- 元信息
    def columns(self, table: str) -> set[str]:
        if table not in _TABLE_COLUMNS_CACHE:
            rows = self.store.query(f"PRAGMA table_info({_safe_ident(table)})")
            _TABLE_COLUMNS_CACHE[table] = {r["name"] for r in rows}
        return _TABLE_COLUMNS_CACHE[table]

    # ------------------------------------------------------------- 通用 CRUD
    def insert(self, table: str, data: dict, *, conn: sqlite3.Connection | None = None) -> None:
        payload = {k: _encode(v) for k, v in data.items()}
        cols = ", ".join(_safe_ident(k) for k in payload)
        marks = ", ".join("?" for _ in payload)
        sql = f"INSERT INTO {_safe_ident(table)} ({cols}) VALUES ({marks})"
        if conn is not None:
            conn.execute(sql, tuple(payload.values()))
        else:
            self.store.execute(sql, tuple(payload.values()))

    def update(
        self,
        table: str,
        pk_value: str,
        data: dict,
        *,
        pk: str = "id",
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if not data:
            return
        payload = {k: _encode(v) for k, v in data.items()}
        sets = ", ".join(f"{_safe_ident(k)} = ?" for k in payload)
        sql = f"UPDATE {_safe_ident(table)} SET {sets} WHERE {_safe_ident(pk)} = ?"
        params = tuple(payload.values()) + (pk_value,)
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.store.execute(sql, params)

    def get(self, table: str, pk_value: str, *, pk: str = "id") -> sqlite3.Row | None:
        return self.store.query_one(
            f"SELECT * FROM {_safe_ident(table)} WHERE {_safe_ident(pk)} = ?", (pk_value,)
        )

    def require(self, table: str, pk_value: str, *, pk: str = "id", what: str | None = None):
        row = self.get(table, pk_value, pk=pk)
        if row is None:
            from .errors import InvalidInput

            raise InvalidInput(f"{what or table} 不存在: {pk_value}", field_errors={pk: "not_found"})
        return row

    def list(
        self,
        table: str,
        where: dict | None = None,
        *,
        order: str = "created_at DESC",
        limit: int = 200,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        sql = f"SELECT * FROM {_safe_ident(table)}"
        params: list = []
        if where:
            clauses = []
            for key, value in where.items():
                if value is None:
                    clauses.append(f"{_safe_ident(key)} IS NULL")
                else:
                    clauses.append(f"{_safe_ident(key)} = ?")
                    params.append(_encode(value))
            sql += " WHERE " + " AND ".join(clauses)
        if order:
            sql += f" ORDER BY {_order_clause(order)}"
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        return self.store.query(sql, params)

    def delete(self, table: str, where: dict, *, conn: sqlite3.Connection | None = None) -> None:
        clauses = []
        params: list = []
        for key, value in where.items():
            if value is None:
                clauses.append(f"{_safe_ident(key)} IS NULL")
            else:
                clauses.append(f"{_safe_ident(key)} = ?")
                params.append(_encode(value))
        sql = f"DELETE FROM {_safe_ident(table)} WHERE " + " AND ".join(clauses)
        if conn is not None:
            conn.execute(sql, tuple(params))
        else:
            self.store.execute(sql, tuple(params))

    def count(self, table: str, where: dict | None = None) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {_safe_ident(table)}"
        params: list = []
        if where:
            clauses = []
            for key, value in where.items():
                if value is None:
                    clauses.append(f"{_safe_ident(key)} IS NULL")
                else:
                    clauses.append(f"{_safe_ident(key)} = ?")
                    params.append(_encode(value))
            sql += " WHERE " + " AND ".join(clauses)
        row = self.store.query_one(sql, params)
        return int(row["n"]) if row else 0


class IdempotencyStore:
    """按 (scope, key) 去重写操作：同 key 同 payload 返回首次结果。

    - key 为空 → 直接执行（不做去重）。
    - 上次执行中断（status=started）→ 抛 InvalidState，要求调用方核实状态后续跑，
      不能把半结果当成功。
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def run(self, scope: str, key: str | None, payload: dict, fn: Callable[[], dict]) -> dict:
        if not key:
            return fn()
        payload_hash = canonical_hash(payload)
        row = self.store.query_one(
            "SELECT * FROM idempotency WHERE scope = ? AND key = ?", (scope, key)
        )
        if row is not None:
            if row["payload_hash"] != payload_hash:
                raise InvalidInput(
                    f"幂等键 {key} 已用于不同请求",
                    field_errors={"idempotency_key": "payload_mismatch"},
                )
            if row["status"] == "done":
                return json.loads(row["response"] or "{}")
            raise InvalidState(
                f"幂等键 {key} 的上一次执行未完成，请先核对状态",
                field_errors={"idempotency_key": "in_progress"},
            )
        now = utc_now()
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO idempotency (scope, key, payload_hash, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'started', ?, ?)",
                (scope, key, payload_hash, now, now),
            )
        try:
            result = fn()
        except BaseException:
            with self.store.tx() as conn:
                conn.execute(
                    "DELETE FROM idempotency WHERE scope = ? AND key = ?", (scope, key)
                )
            raise
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE idempotency SET status = 'done', response = ?, updated_at = ? "
                "WHERE scope = ? AND key = ?",
                (json.dumps(result, ensure_ascii=False, default=str), utc_now(), scope, key),
            )
        return result


def _safe_ident(name: str) -> str:
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise InvalidInput(f"非法标识符: {name}")
    return name


def _order_clause(order: str) -> str:
    parts = []
    for chunk in order.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        bits = chunk.split()
        col = _safe_ident(bits[0])
        direction = "DESC" if len(bits) > 1 and bits[1].upper() == "DESC" else "ASC"
        parts.append(f"{col} {direction}")
    return ", ".join(parts) or "rowid DESC"


def _encode(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def row_to_dict(row: sqlite3.Row | None, *, json_fields: tuple[str, ...] = ()) -> dict | None:
    """把 sqlite3.Row 转 dict，并解码已知 JSON 字段。"""
    if row is None:
        return None
    data = {k: row[k] for k in row.keys()}
    for field in json_fields:
        if field in data and isinstance(data[field], str):
            try:
                data[field] = json.loads(data[field])
            except (TypeError, ValueError):
                pass
    return data


def rows_to_dicts(rows, *, json_fields: tuple[str, ...] = ()) -> list[dict]:
    return [row_to_dict(r, json_fields=json_fields) for r in rows]
