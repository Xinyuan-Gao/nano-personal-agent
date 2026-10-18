from __future__ import annotations

import uuid
from datetime import datetime, timezone


def utc_now() -> str:
    """当前 UTC 时间，微秒精度，形如 ``2026-01-01T00:00:00.000000Z``。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def seconds_since(value: str | None, now: datetime | None = None) -> float:
    dt = parse_ts(value)
    if dt is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds())


def new_correlation_id() -> str:
    return f"cor_{uuid.uuid4().hex[:16]}"


def utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
