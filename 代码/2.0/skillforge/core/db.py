from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .clock import utc_now
from .errors import StorageError

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "2.0.0"   # v2.0 追加 P1 四层记忆表

#: 旧库升级用的增量列（新库已由 schema.sql 建好，这里是幂等补列）
COLUMN_ADDITIONS: dict[str, list[tuple[str, str]]] = {
    "run": [("body_state", "TEXT NOT NULL DEFAULT 'available'"), ("body_expired_at", "TEXT")],
    "feedback": [("body_state", "TEXT NOT NULL DEFAULT 'available'"), ("body_expired_at", "TEXT")],
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in COLUMN_ADDITIONS.items():
        try:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:  # pragma: no cover - 表不存在时跳过
            continue
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


class Store:
    """SQLite 访问入口。所有模块只能通过本类访问数据库。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_connection(self._conn)

    # ------------------------------------------------------------- 连接
    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._init_connection(sqlite3.connect(str(self.db_path), timeout=30.0))
            self._local.conn = conn
        return conn

    def _init_connection(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    # ------------------------------------------------------------- 迁移
    def migrate(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.tx() as conn:
            conn.executescript(sql)
            _ensure_columns(conn)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION,),
            )

    @property
    def schema_version(self) -> str | None:
        try:
            row = self.query_one("SELECT value FROM schema_meta WHERE key = 'schema_version'")
        except sqlite3.Error:
            return None
        return row["value"] if row else None

    # ------------------------------------------------------------- 事务
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE，异常回滚。可重入（嵌套时复用外层事务）。"""
        conn = self._conn
        if getattr(self._local, "in_tx", False):
            yield conn
            return
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:  # pragma: no cover - 极端并发
                raise StorageError(f"无法开始事务: {exc}") from exc
            self._local.in_tx = True
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                self._local.in_tx = False

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        yield self._conn

    # ------------------------------------------------------------- 便捷查询
    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        with self.tx() as conn:
            conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self.tx() as conn:
            conn.executemany(sql, rows)

    def scalar(self, sql: str, params: tuple | list = (), default=None):
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # ------------------------------------------------------------- 维护
    def vacuum(self) -> None:
        self._conn.execute("VACUUM")

    def checkpoint(self) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:  # pragma: no cover
            pass

    def integrity_check(self) -> str:
        return self.scalar("PRAGMA integrity_check", default="unknown")


def ensure_schema(store: Store) -> Store:
    store.migrate()
    return store


def now() -> str:
    return utc_now()
