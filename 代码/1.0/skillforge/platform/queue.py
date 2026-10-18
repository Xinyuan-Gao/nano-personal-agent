from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..core.clock import parse_ts, utc_now
from ..core.errors import AppError, InvalidInput
from ..core.ids import new_id

#: 终态集合：进入后不再被 worker 领取
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "budget_stopped"})
#: 学习类作业类型前缀：单独一个 worker 槽位，不许挤占执行槽位
LEARNING_TYPE_PREFIXES = ("learning.",)
DEFAULT_LEASE_SECONDS = 60


@dataclass
class Job:
    id: str
    type: str
    status: str
    payload: dict
    budget: dict
    priority: int
    attempts: int
    lease_owner: str | None
    lease_expires_at: str | None
    heartbeat_at: str | None
    result: dict | None
    error_code: str | None
    created_at: str
    updated_at: str
    # --- 契约之外但表中存在、对调用方有用的附加字段（只增不改） ---
    error_message: str | None = None
    checkpoint: dict = field(default_factory=dict)
    cancel_requested: bool = False
    run_id: str | None = None
    idempotency_key: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class JobQueue:
    def __init__(self, store, repo, events) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.default_lease_seconds = DEFAULT_LEASE_SECONDS
        self._lease_windows: dict[str, int] = {}

    # ------------------------------------------------------------- 入队
    def enqueue(
        self,
        type: str,
        payload: dict | None = None,
        *,
        idempotency_key: str | None = None,
        budget: dict | None = None,
        priority: int = 100,
        run_id: str | None = None,
    ) -> str:
        """入队一个作业；同 ``idempotency_key`` 返回同一 ``job_id``。"""
        if not type or not isinstance(type, str):
            raise InvalidInput("作业类型不能为空", field_errors={"type": "required"})
        if payload is not None and not isinstance(payload, dict):
            raise InvalidInput("payload 必须是对象", field_errors={"payload": "invalid"})
        payload = dict(payload or {})
        budget = dict(budget or {})

        if idempotency_key:
            row = self.store.query_one(
                "SELECT id FROM job WHERE idempotency_key = ?", (idempotency_key,)
            )
            if row is not None:
                return row["id"]

        job_id = new_id("job")
        now = utc_now()
        data = {
            "id": job_id,
            "type": type,
            "status": "queued",
            "priority": int(priority),
            "payload": json.dumps(payload, ensure_ascii=False),
            "budget": json.dumps(budget, ensure_ascii=False),
            "idempotency_key": idempotency_key,
            "attempts": 0,
            "checkpoint": "{}",
            "cancel_requested": 0,
            "run_id": run_id,
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self.store.tx() as conn:
                self.repo.insert("job", data, conn=conn)
                self.events.audit(
                    "job.enqueue",
                    "job",
                    job_id,
                    after={"type": type, "priority": int(priority), "run_id": run_id},
                    conn=conn,
                )
        except sqlite3.IntegrityError:
            # 并发同 key：另一个线程刚写入，返回它的 job_id（幂等）
            row = self.store.query_one(
                "SELECT id FROM job WHERE idempotency_key = ?", (idempotency_key,)
            )
            if row is not None:
                return row["id"]
            raise
        return job_id

    # ------------------------------------------------------------- 领取与租约
    def lease(self, types: list[str], worker_id: str, lease_seconds: int = 60) -> Job | None:
        """原子领取一个待执行作业；没有可领取时返回 ``None``。"""
        if not worker_id:
            raise InvalidInput("worker_id 不能为空", field_errors={"worker_id": "required"})
        types = [t for t in (types or []) if t]
        now = utc_now()
        expires = _offset_iso(lease_seconds)
        with self.store.tx() as conn:
            sql = "SELECT * FROM job WHERE status = 'queued' AND cancel_requested = 0"
            params: list = []
            if types:
                sql += f" AND type IN ({', '.join('?' for _ in types)})"
                params.extend(types)
            sql += " ORDER BY priority ASC, created_at ASC, rowid ASC LIMIT 1"
            row = conn.execute(sql, tuple(params)).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                "UPDATE job SET status = 'running', lease_owner = ?, lease_expires_at = ?, "
                "heartbeat_at = ?, attempts = attempts + 1, "
                "started_at = COALESCE(started_at, ?), updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (worker_id, expires, now, now, now, row["id"]),
            )
            if updated.rowcount != 1:  # 竞争失败：交给下一次 lease
                return None
            fresh = conn.execute("SELECT * FROM job WHERE id = ?", (row["id"],)).fetchone()
            self.events.audit(
                "job.lease", "job", row["id"], after={"worker_id": worker_id}, conn=conn
            )
        self._lease_windows[row["id"]] = int(lease_seconds)
        return _to_job(fresh)

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        """续租：只有持有租约的 worker 能续，返回是否成功。"""
        job = self.get(job_id)
        if job.status != "running" or job.lease_owner != worker_id:
            return False
        window = self._lease_windows.get(job_id, self.default_lease_seconds)
        now = utc_now()
        with self.store.tx() as conn:
            updated = conn.execute(
                "UPDATE job SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ? "
                "WHERE id = ? AND lease_owner = ? AND status = 'running'",
                (_offset_iso(window, now=now), now, now, job_id, worker_id),
            )
        return updated.rowcount == 1

    def finish(
        self,
        job_id: str,
        status: str,
        *,
        result=None,
        error_code=None,
        error_message=None,
    ) -> None:
        """结束作业（写入终态、结果与错误码，并清空租约）。"""
        if status not in TERMINAL_STATUSES:
            raise InvalidInput(
                f"非法终态: {status}", field_errors={"status": "invalid"}
            )
        self.get(job_id)
        now = utc_now()
        payload = {
            "status": status,
            "result": json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
            "error_code": error_code,
            "error_message": error_message,
            "lease_owner": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "finished_at": now,
            "updated_at": now,
        }
        with self.store.tx() as conn:
            self.repo.update("job", job_id, payload, conn=conn)
            self.events.audit(
                "job.finish",
                "job",
                job_id,
                after={"status": status, "error_code": error_code},
                conn=conn,
            )
        self._lease_windows.pop(job_id, None)

    def progress(self, job_id: str, checkpoint: dict) -> None:
        """写入检查点（合并语义），供重启后判断最后已完成步骤。"""
        if not isinstance(checkpoint, dict):
            raise InvalidInput("checkpoint 必须是对象", field_errors={"checkpoint": "invalid"})
        job = self.get(job_id)
        merged = dict(job.checkpoint or {})
        merged.update(checkpoint)
        with self.store.tx() as conn:
            self.repo.update(
                "job",
                job_id,
                {"checkpoint": json.dumps(merged, ensure_ascii=False, default=str), "updated_at": utc_now()},
                conn=conn,
            )

    # ------------------------------------------------------------- 取消
    def cancel(self, job_id: str) -> str:
        """请求取消。

        - 已终态：原样返回该状态。
        - queued（尚未执行）：直接置 ``cancelled``，返回 ``"cancelled"``。
        - running：置 ``cancel_requested``，返回 ``"cancel_requested"``，由执行方在
          下一个安全检查点停止。
        """
        job = self.get(job_id)
        if job.status in TERMINAL_STATUSES:
            return job.status
        now = utc_now()
        if job.status == "queued":
            with self.store.tx() as conn:
                self.repo.update(
                    "job",
                    job_id,
                    {
                        "cancel_requested": 1,
                        "status": "cancelled",
                        "finished_at": now,
                        "updated_at": now,
                    },
                    conn=conn,
                )
                self.events.audit(
                    "job.cancel", "job", job_id, after={"status": "cancelled"}, conn=conn
                )
            return "cancelled"
        with self.store.tx() as conn:
            self.repo.update(
                "job", job_id, {"cancel_requested": 1, "updated_at": now}, conn=conn
            )
            self.events.audit(
                "job.cancel", "job", job_id, after={"status": "cancel_requested"}, conn=conn
            )
        return "cancel_requested"

    def request_cancel_seen(self, job_id: str) -> bool:
        job = self.get(job_id)
        return bool(job.cancel_requested)

    # ------------------------------------------------------------- 查询
    def get(self, job_id: str) -> Job:
        row = self.repo.get("job", job_id)
        if row is None:
            raise InvalidInput(f"作业不存在: {job_id}", field_errors={"job_id": "not_found"})
        return _to_job(row)

    def list(self, status: str | None = None, type: str | None = None, limit: int = 100) -> list[Job]:
        sql = "SELECT * FROM job"
        clauses: list[str] = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if type:
            clauses.append("type = ?")
            params.append(type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        return [_to_job(r) for r in self.store.query(sql, params)]

    def counts(self) -> dict:
        rows = self.store.query("SELECT status, COUNT(*) AS n FROM job GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------- 恢复
    def recover_stale(self, now=None) -> list[str]:
        """租约过期的 running 作业回到 queued（``interrupted`` 语义）；返回 job_ids。"""
        moment = now or utc_now()
        if not isinstance(moment, str):
            moment = str(moment)
        recovered: list[str] = []
        with self.store.tx() as conn:
            rows = conn.execute(
                "SELECT id FROM job WHERE status = 'running' AND "
                "(lease_expires_at IS NULL OR lease_expires_at < ?)",
                (moment,),
            ).fetchall()
            for row in rows:
                updated = conn.execute(
                    "UPDATE job SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL, "
                    "heartbeat_at = NULL, error_code = 'interrupted', updated_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (moment, row["id"]),
                )
                if updated.rowcount == 1:
                    recovered.append(row["id"])
                    self.events.audit(
                        "job.recover",
                        "job",
                        row["id"],
                        after={"status": "queued", "error_code": "interrupted"},
                        reason="租约过期，按 interrupted 语义回收",
                        conn=conn,
                    )
        for job_id in recovered:
            self._lease_windows.pop(job_id, None)
        return recovered


class WorkerPool:
    """线程池：最多 ``max_execution`` 个执行 worker + ``max_learning`` 个学习 worker。

    - 学习类作业单独占用槽位，不与执行作业争抢。
    - 租约 + heartbeat 由 ``JobQueue`` 保证不会重复消费同一作业。
    - ``run_once()`` 同步驱动一个作业，供测试与手动排空使用。
    """

    def __init__(
        self,
        queue: JobQueue,
        handlers: dict[str, Callable[[Job], None]],
        max_execution: int = 2,
        max_learning: int = 1,
        *,
        poll_interval: float = 0.05,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.queue = queue
        self.handlers = dict(handlers or {})
        self.max_execution = max(0, int(max_execution))
        self.max_learning = max(0, int(max_learning))
        self.poll_interval = float(poll_interval)
        self.lease_seconds = int(lease_seconds)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._running_jobs: set[str] = set()
        self._lock = threading.Lock()
        self.started = False

    # ------------------------------------------------------------- 类型划分
    @property
    def execution_types(self) -> list[str]:
        return [t for t in self.handlers if not is_learning_type(t)]

    @property
    def learning_types(self) -> list[str]:
        return [t for t in self.handlers if is_learning_type(t)]

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self.started:
            return
        self.started = True
        self._stop.clear()
        try:  # 启动时回收租约过期的作业，避免作业永久卡在 running
            self.queue.recover_stale()
        except Exception:  # pragma: no cover - 恢复失败不应阻断启动
            pass
        pid = os.getpid()
        for index in range(self.max_execution):
            self._spawn_thread(f"exec-{index}", self.execution_types, pid)
        for index in range(self.max_learning):
            self._spawn_thread(f"learn-{index}", self.learning_types, pid)

    def _spawn_thread(self, role: str, types: list[str], pid: int) -> None:
        worker_id = f"worker-{role}-{pid}"

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    if not self.run_once(types, worker_id=worker_id):
                        self._stop.wait(self.poll_interval)
                except Exception:  # pragma: no cover - worker 循环不得因单次异常退出
                    self._stop.wait(self.poll_interval)

        thread = threading.Thread(target=_loop, name=worker_id, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        deadline = max(0.0, float(timeout))
        for thread in self._threads:
            thread.join(timeout=deadline)
        self._threads = [t for t in self._threads if t.is_alive()]
        if not self._threads:
            self.started = False

    # ------------------------------------------------------------- 执行
    def run_once(self, types=None, *, worker_id: str | None = None) -> bool:
        """同步领取并执行一个作业；有作业执行返回 True，否则 False。"""
        if types is None:
            candidates = list(self.handlers)
        elif isinstance(types, str):
            candidates = [types]
        else:
            candidates = list(types)
        if not candidates:
            return False
        worker_id = worker_id or f"worker-sync-{os.getpid()}"
        job = self.queue.lease(candidates, worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        self._execute(job)
        return True

    def drain(self, types=None, limit: int = 100) -> int:
        """同步排空队列（测试与 CLI 使用）；返回执行条数。"""
        done = 0
        while done < limit and self.run_once(types):
            done += 1
        return done

    def _execute(self, job: Job) -> None:
        with self._lock:
            self._running_jobs.add(job.id)
        try:
            handler = self.handlers.get(job.type)
            if handler is None:
                self.queue.finish(
                    job.id, "failed", error_code="unknown_job_type",
                    error_message=f"没有注册处理器: {job.type}",
                )
                return
            outcome = None
            error_code = None
            error_message = None
            try:
                outcome = handler(job)
            except AppError as exc:
                error_code = exc.code
                error_message = exc.message
            except Exception as exc:  # pragma: no cover - 非契约异常兜底
                error_code = "internal_error"
                error_message = str(exc)
            current = self.queue.get(job.id)
            if current.status in TERMINAL_STATUSES:
                return  # handler 自己写了终态，不覆盖
            if error_code:
                self.queue.finish(
                    job.id, "failed", error_code=error_code, error_message=error_message
                )
                return
            if self.queue.request_cancel_seen(job.id):
                self.queue.finish(job.id, "cancelled", result={"reason": "cancel_requested"})
                return
            result = outcome if isinstance(outcome, dict) else None
            self.queue.finish(job.id, "completed", result=result)
        finally:
            with self._lock:
                self._running_jobs.discard(job.id)


def is_learning_type(job_type: str) -> bool:
    return any(str(job_type).startswith(prefix) for prefix in LEARNING_TYPE_PREFIXES)


def _offset_iso(seconds: int, *, now: str | None = None) -> str:
    base = parse_ts(now) if now else None
    if base is None:
        base = datetime.now(timezone.utc)
    return (base + timedelta(seconds=int(seconds))).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _to_job(row) -> Job:
    data = {k: row[k] for k in row.keys()}
    return Job(
        id=data["id"],
        type=data["type"],
        status=data["status"],
        payload=_json(data.get("payload"), {}),
        budget=_json(data.get("budget"), {}),
        priority=int(data.get("priority") or 100),
        attempts=int(data.get("attempts") or 0),
        lease_owner=data.get("lease_owner"),
        lease_expires_at=data.get("lease_expires_at"),
        heartbeat_at=data.get("heartbeat_at"),
        result=_json(data.get("result"), None),
        error_code=data.get("error_code"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        error_message=data.get("error_message"),
        checkpoint=_json(data.get("checkpoint"), {}),
        cancel_requested=bool(data.get("cancel_requested")),
        run_id=data.get("run_id"),
        idempotency_key=data.get("idempotency_key"),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
    )


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
