from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Callable

from ..core.clock import parse_ts, seconds_since, utc_now
from ..core.config import DEFAULT_RUN_BUDGET, GLOBAL_DAILY_BUDGET
from ..core.errors import InvalidInput
from ..core.ids import canonical_hash

_BUDGET_FIELDS = ("max_model_calls", "max_tool_actions", "max_wall_seconds", "max_tokens")


@dataclass
class BudgetSpec:
    max_model_calls: int = DEFAULT_RUN_BUDGET["max_model_calls"]
    max_tool_actions: int = DEFAULT_RUN_BUDGET["max_tool_actions"]
    max_wall_seconds: int = DEFAULT_RUN_BUDGET["max_wall_seconds"]
    max_tokens: int = DEFAULT_RUN_BUDGET["max_tokens"]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "BudgetSpec":
        data = dict(d or {})
        kwargs = {}
        for name in _BUDGET_FIELDS:
            if name in data and data[name] is not None:
                try:
                    kwargs[name] = int(data[name])
                except (TypeError, ValueError):
                    raise InvalidInput(
                        f"预算字段 {name} 必须是整数", field_errors={name: "invalid"}
                    ) from None
        spec = cls(**kwargs)
        spec.validate()
        return spec

    def validate(self) -> None:
        for name in _BUDGET_FIELDS:
            if getattr(self, name) < 0:
                raise InvalidInput(f"预算字段 {name} 不能为负", field_errors={name: "invalid"})

    def fingerprint(self) -> str:
        return canonical_hash(self.to_dict())


@dataclass
class BudgetState:
    scope_id: str = ""
    model_calls_used: int = 0
    tool_actions_used: int = 0
    tokens_used: int = 0
    elapsed_s: float = 0.0
    remaining_calls: int = 0
    remaining_tokens: int = 0
    exceeded_type: str | None = None
    unknown_usage: int = 0
    reserved_calls: int = 0
    reserved_tokens: int = 0
    status: str = "open"
    spec: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class BudgetLedger:
    def __init__(self, store, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self._clock = clock or utc_now
        self.daily_budget = dict(GLOBAL_DAILY_BUDGET)

    # ------------------------------------------------------------- 内部
    def _now(self) -> str:
        return self._clock()

    def _today(self) -> str:
        return self._now()[:10]

    def _row(self, scope_id: str):
        return self.store.query_one("SELECT * FROM budget_ledger WHERE scope_id = ?", (scope_id,))

    def _require(self, scope_id: str):
        row = self._row(scope_id)
        if row is None:
            raise InvalidInput(
                f"预算未初始化: {scope_id}", field_errors={"scope_id": "not_found"}
            )
        return row

    # ------------------------------------------------------------- 生命周期
    def open(self, scope_id: str, spec: BudgetSpec | dict, *, estimated_tokens: int = 0) -> None:
        """创建账本；已存在时保留既有用量（续跑不重置已消费预算）。"""
        if not scope_id:
            raise InvalidInput("scope_id 不能为空", field_errors={"scope_id": "required"})
        if isinstance(spec, dict):
            spec = BudgetSpec.from_dict(spec)
        spec.validate()
        now = self._now()
        with self.store.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM budget_ledger WHERE scope_id = ?", (scope_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO budget_ledger (scope_id, scope_kind, spec, reserved_calls, "
                    "reserved_tokens, used_calls, used_tokens, used_tool_actions, unknown_usage, "
                    "external_calls, started_at, updated_at, status, exceeded_type) "
                    "VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, ?, ?, 'open', NULL)",
                    (
                        scope_id,
                        scope_id.split("_")[0] or "scope",
                        _dump(spec.to_dict()),
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE budget_ledger SET spec = ?, updated_at = ?, status = 'open', "
                    "exceeded_type = NULL WHERE scope_id = ?",
                    (_dump(spec.to_dict()), now, scope_id),
                )

    def close(self, scope_id: str, status: str) -> None:
        """关闭账本：释放剩余预留，后续 ``reserve_call`` 一律返回 False。"""
        self._require(scope_id)
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE budget_ledger SET status = ?, reserved_calls = 0, reserved_tokens = 0, "
                "updated_at = ? WHERE scope_id = ?",
                (status, self._now(), scope_id),
            )

    # ------------------------------------------------------------- 预留 / 消费
    def reserve_call(self, scope_id: str, estimated_tokens: int = 0) -> bool:
        """调用前预留一次模型调用 + token 上界；不足返回 False（原子）。"""
        estimated_tokens = max(0, int(estimated_tokens or 0))
        now = self._now()
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT * FROM budget_ledger WHERE scope_id = ?", (scope_id,)
            ).fetchone()
            if row is None:
                raise InvalidInput(
                    f"预算未初始化: {scope_id}", field_errors={"scope_id": "not_found"}
                )
            spec = BudgetSpec.from_dict(_load(row["spec"]))
            if row["status"] != "open":
                self._mark_exceeded(conn, scope_id, "closed", now)
                return False

            exceeded = None
            if int(row["used_calls"]) + int(row["reserved_calls"]) + 1 > spec.max_model_calls:
                exceeded = "max_model_calls"
            elif int(row["used_tokens"]) + int(row["reserved_tokens"]) + estimated_tokens > spec.max_tokens:
                exceeded = "max_tokens"
            else:
                elapsed = seconds_since(row["started_at"], parse_ts(now))
                if spec.max_wall_seconds and elapsed > spec.max_wall_seconds:
                    exceeded = "max_wall_seconds"
                else:
                    daily = conn.execute(
                        "SELECT COALESCE((SELECT SUM(reserved_calls) FROM budget_ledger "
                        "  WHERE status = 'open'), 0) AS inflight_calls, "
                        "COALESCE((SELECT SUM(reserved_tokens) FROM budget_ledger "
                        "  WHERE status = 'open'), 0) AS inflight_tokens, "
                        "COALESCE((SELECT model_calls FROM daily_usage WHERE day = ?), 0) AS day_calls, "
                        "COALESCE((SELECT tokens FROM daily_usage WHERE day = ?), 0) AS day_tokens",
                        (self._today(), self._today()),
                    ).fetchone()
                    if int(daily["day_calls"]) + int(daily["inflight_calls"]) + 1 > int(
                        self.daily_budget.get("max_model_calls", 0)
                    ):
                        exceeded = "daily_model_calls"
                    elif int(daily["day_tokens"]) + int(daily["inflight_tokens"]) + estimated_tokens > int(
                        self.daily_budget.get("max_tokens", 0)
                    ):
                        exceeded = "daily_tokens"
            if exceeded:
                self._mark_exceeded(conn, scope_id, exceeded, now)
                return False

            conn.execute(
                "UPDATE budget_ledger SET reserved_calls = reserved_calls + 1, "
                "reserved_tokens = reserved_tokens + ?, updated_at = ? WHERE scope_id = ?",
                (estimated_tokens, now, scope_id),
            )
            return True

    def release_call(self, scope_id: str, estimated_tokens: int = 0) -> None:
        """释放一次未消费的预留（请求失败或未发出时调用）。"""
        self._require(scope_id)
        estimated_tokens = max(0, int(estimated_tokens or 0))
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE budget_ledger SET reserved_calls = MAX(0, reserved_calls - 1), "
                "reserved_tokens = MAX(0, reserved_tokens - ?), updated_at = ? WHERE scope_id = ?",
                (estimated_tokens, self._now(), scope_id),
            )

    def consume(
        self,
        scope_id: str,
        *,
        tokens: int,
        calls: int = 1,
        tool_actions: int = 0,
        usage_known: bool = True,
    ) -> None:
        """记账实际用量，并释放这一次调用占用的 token 预留。

        - ``usage_known=True``：扣减实际 token 后释放本批预留；当该 scope 没有在途
          调用（``reserved_calls`` 归零）时预留整体清零，避免估算值长期泄漏预算。
        - ``usage_known=False``：保留预留作为保守上界并累加 ``unknown_usage``
          不外推 token 数字。
        """
        tokens = max(0, int(tokens or 0))
        calls = max(0, int(calls or 0))
        tool_actions = max(0, int(tool_actions or 0))
        today = self._today()
        now = self._now()
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT * FROM budget_ledger WHERE scope_id = ?", (scope_id,)
            ).fetchone()
            if row is None:
                raise InvalidInput(
                    f"预算未初始化: {scope_id}", field_errors={"scope_id": "not_found"}
                )
            remaining_reserved_calls = max(0, int(row["reserved_calls"]) - calls)
            if not usage_known:
                # 计费不明：保留预留作为保守上界
                reserved_tokens = int(row["reserved_tokens"])
                unknown_delta = 1
            elif remaining_reserved_calls == 0:
                reserved_tokens = 0
                unknown_delta = 0
            else:
                reserved_tokens = max(0, int(row["reserved_tokens"]) - tokens)
                unknown_delta = 0
            conn.execute(
                "UPDATE budget_ledger SET used_calls = used_calls + ?, used_tokens = used_tokens + ?, "
                "used_tool_actions = used_tool_actions + ?, reserved_calls = ?, reserved_tokens = ?, "
                "unknown_usage = unknown_usage + ?, updated_at = ? WHERE scope_id = ?",
                (
                    calls,
                    tokens,
                    tool_actions,
                    remaining_reserved_calls,
                    reserved_tokens,
                    unknown_delta,
                    now,
                    scope_id,
                ),
            )
            conn.execute(
                "INSERT INTO daily_usage (day, model_calls, tokens, unknown_usage, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET model_calls = model_calls + excluded.model_calls, "
                "tokens = tokens + excluded.tokens, "
                "unknown_usage = unknown_usage + excluded.unknown_usage, "
                "updated_at = excluded.updated_at",
                (today, calls, tokens, unknown_delta, now),
            )

    def add_tool_action(self, scope_id: str) -> bool:
        """预留一次工具动作；超限返回 False（原子）。"""
        now = self._now()
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT * FROM budget_ledger WHERE scope_id = ?", (scope_id,)
            ).fetchone()
            if row is None:
                raise InvalidInput(
                    f"预算未初始化: {scope_id}", field_errors={"scope_id": "not_found"}
                )
            spec = BudgetSpec.from_dict(_load(row["spec"]))
            if row["status"] != "open" or int(row["used_tool_actions"]) + 1 > spec.max_tool_actions:
                self._mark_exceeded(conn, scope_id, "max_tool_actions", now)
                return False
            conn.execute(
                "UPDATE budget_ledger SET used_tool_actions = used_tool_actions + 1, updated_at = ? "
                "WHERE scope_id = ?",
                (now, scope_id),
            )
            return True

    # ------------------------------------------------------------- 查询
    def state(self, scope_id: str) -> BudgetState:
        row = self._require(scope_id)
        spec = BudgetSpec.from_dict(_load(row["spec"]))
        used_calls = int(row["used_calls"])
        reserved_calls = int(row["reserved_calls"])
        used_tokens = int(row["used_tokens"])
        reserved_tokens = int(row["reserved_tokens"])
        elapsed = seconds_since(row["started_at"], parse_ts(self._now()))
        exceeded = row["exceeded_type"]
        if exceeded is None:
            if used_calls + reserved_calls > spec.max_model_calls:
                exceeded = "max_model_calls"
            elif used_tokens + reserved_tokens > spec.max_tokens:
                exceeded = "max_tokens"
            elif spec.max_wall_seconds and elapsed > spec.max_wall_seconds:
                exceeded = "max_wall_seconds"
            elif int(row["used_tool_actions"]) > spec.max_tool_actions:
                exceeded = "max_tool_actions"
        return BudgetState(
            scope_id=scope_id,
            model_calls_used=used_calls,
            tool_actions_used=int(row["used_tool_actions"]),
            tokens_used=used_tokens,
            elapsed_s=round(elapsed, 3),
            remaining_calls=max(0, spec.max_model_calls - used_calls - reserved_calls),
            remaining_tokens=max(0, spec.max_tokens - used_tokens - reserved_tokens),
            exceeded_type=exceeded,
            unknown_usage=int(row["unknown_usage"]),
            reserved_calls=reserved_calls,
            reserved_tokens=reserved_tokens,
            status=row["status"],
            spec=spec.to_dict(),
        )

    def check_wall_clock(self, scope_id: str) -> bool:
        """墙钟超时检测；超时返回 True 并记录 exceeded_type。"""
        row = self._require(scope_id)
        spec = BudgetSpec.from_dict(_load(row["spec"]))
        elapsed = seconds_since(row["started_at"], parse_ts(self._now()))
        if spec.max_wall_seconds and elapsed > spec.max_wall_seconds:
            if row["exceeded_type"] != "max_wall_seconds":
                with self.store.tx() as conn:
                    self._mark_exceeded(conn, scope_id, "max_wall_seconds", self._now())
            return True
        return False

    def daily_state(self, day: str | None = None) -> dict:
        """当天全局用量（含在途预留），供设置页与作业列表展示。"""
        key = day or self._today()
        row = self.store.query_one("SELECT * FROM daily_usage WHERE day = ?", (key,))
        inflight = self.store.query_one(
            "SELECT COALESCE(SUM(reserved_calls), 0) AS calls, COALESCE(SUM(reserved_tokens), 0) AS tokens "
            "FROM budget_ledger WHERE status = 'open'"
        )
        return {
            "day": key,
            "model_calls": int(row["model_calls"]) if row else 0,
            "tokens": int(row["tokens"]) if row else 0,
            "unknown_usage": int(row["unknown_usage"]) if row else 0,
            "reserved_calls": int(inflight["calls"]) if inflight else 0,
            "reserved_tokens": int(inflight["tokens"]) if inflight else 0,
            "limit": dict(self.daily_budget),
        }

    # ------------------------------------------------------------- 内部
    @staticmethod
    def _mark_exceeded(conn, scope_id: str, exceeded_type: str, now: str) -> None:
        conn.execute(
            "UPDATE budget_ledger SET exceeded_type = ?, updated_at = ? "
            "WHERE scope_id = ? AND (exceeded_type IS NULL OR exceeded_type != ?)",
            (exceeded_type, now, scope_id, exceeded_type),
        )


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def spec_from_budget(budget: dict | None, default: dict | None = None) -> BudgetSpec:
    """把作业 ``budget`` dict 转成 ``BudgetSpec``（未提供的字段取默认值）。"""
    merged = dict(default or DEFAULT_RUN_BUDGET)
    merged.update({k: v for k, v in (budget or {}).items() if v is not None})
    return BudgetSpec.from_dict(merged)
