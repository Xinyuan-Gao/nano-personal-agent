from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..core.clock import utc_now
from ..core.errors import AppError, InvalidInput
from ..core.ids import new_id
from .scope import (
    DEFAULT_SESSION,
    DREAM_LOCK_SECONDS,
    DREAM_MAX_LOGS,
    DREAM_MAX_MODEL_CALLS,
    DREAM_MIN_INTERVAL_HOURS,
    DREAM_MIN_SESSIONS,
    SCOPE_PERSONAL,
    SCOPE_TASK,
    SCOPE_WORKSPACE,
    TOPIC_TYPES,
    normalize_topic_type,
)
from .logs import SOURCE_USER_REQUEST

ACTIVE_JOB_STATUSES = ("queued", "running")


class DreamService:
    def __init__(self, store, repo, events, config=None, profile_id: str = "local",
                 topics=None, logs=None, working=None, index=None, budget=None,
                 model_factory=None, queue=None, idem=None) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.config = config
        self.profile_id = profile_id
        self.topics = topics
        self.logs = logs
        self.working = working
        self.index = index
        self.budget = budget
        self.model_factory = model_factory
        self.queue = queue
        self.idem = idem

    # ------------------------------------------------------------------ 门槛
    def gate(self, *, trigger: str = "manual") -> dict:
        """评估自动触发门槛；手动触发只检查并发锁。"""
        last = self.store.query_one(
            "SELECT finished_at, created_at FROM dream_job WHERE profile_id = ? AND status = 'completed' "
            "ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1",
            (self.profile_id,),
        )
        last_at = (last["finished_at"] or last["created_at"]) if last else None
        hours_since = _hours_since(last_at)
        sessions_since = len(self._sessions_since(last_at))
        active = self.store.query_one(
            "SELECT id FROM dream_job WHERE profile_id = ? AND status IN (?, ?) LIMIT 1",
            (self.profile_id, *ACTIVE_JOB_STATUSES),
        )
        lock = self._lock_state()
        reasons: list[str] = []
        if active is not None:
            reasons.append("已有整合作业在进行")
        if lock.get("held") and trigger == "auto":
            reasons.append("整合锁被占用")
        if trigger == "auto":
            if hours_since is not None and hours_since < DREAM_MIN_INTERVAL_HOURS:
                reasons.append(f"距上次整合不足 {DREAM_MIN_INTERVAL_HOURS} 小时")
            if sessions_since < DREAM_MIN_SESSIONS:
                reasons.append(f"新 session 数 {sessions_since} < {DREAM_MIN_SESSIONS}")
        return {
            "trigger": trigger,
            "allowed": not reasons,
            "reasons": reasons,
            "last_consolidated_at": last_at,
            "hours_since_last": hours_since,
            "sessions_since_last": sessions_since,
            "min_interval_hours": DREAM_MIN_INTERVAL_HOURS,
            "min_sessions": DREAM_MIN_SESSIONS,
            "lock": lock,
        }

    def enqueue(self, *, trigger: str = "manual", idempotency_key: str | None = None) -> dict:
        """创建 dream 作业并（如有队列）入队 ``memory.dream``。"""
        evaluation = self.gate(trigger=trigger)
        if not evaluation["allowed"]:
            raise InvalidInput("整合门槛未满足：" + "；".join(evaluation["reasons"]),
                               field_errors={"dream": "gate_not_met"})
        dream_id = new_id("drm")
        now = utc_now()
        with self.store.tx() as conn:
            self.repo.insert("dream_job", {
                "id": dream_id,
                "profile_id": self.profile_id,
                "trigger_kind": trigger,
                "status": "queued",
                "gate": json.dumps(evaluation, ensure_ascii=False),
                "scan": "{}",
                "result": "{}",
                "usage": "{}",
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
            }, conn=conn)
        job_id = None
        if self.queue is not None:
            job = self.queue.enqueue(
                "memory.dream",
                {"dream_job_id": dream_id, "trigger": trigger},
                idempotency_key=idempotency_key or f"memory.dream:{dream_id}",
            )
            job_id = getattr(job, "id", job)
            with self.store.tx() as conn:
                conn.execute("UPDATE dream_job SET job_id = ?, updated_at = ? WHERE id = ?",
                             (job_id, utc_now(), dream_id))
        self.events.audit("memory.dream_enqueue", "dream_job", dream_id,
                          after={"trigger": trigger, "job_id": job_id})
        return {"dream_job_id": dream_id, "job_id": job_id, "status": "queued", "gate": evaluation}

    # ------------------------------------------------------------------ 执行
    def run(self, dream_job_id: str, worker_id: str = "worker-learning") -> dict:
        """执行一次整合作业；只写记忆层。

        ``failed`` 与因锁被占用而 ``skipped`` 的作业允许重新领取执行（attempts 递增
        审计保留）；``completed`` / ``budget_stopped`` 直接返回既有结果，不重复产生副作用
        。
        """
        row = self.repo.get("dream_job", dream_job_id)
        if row is None:
            raise InvalidInput("整合作业不存在", field_errors={"dream_job_id": "not_found"})
        if row["status"] in ("completed", "budget_stopped"):
            return self.get(dream_job_id) or {}
        if not self._acquire_lock(worker_id):
            return self._finish(dream_job_id, "skipped",
                                error_code="lock_held",
                                error_message="整合锁被占用，未启动（不产生副作用）")
        self.events.product_event("dream_started", {"job_id": dream_job_id,
                                                    "trigger": row["trigger_kind"]})
        with self.store.tx() as conn:
            conn.execute("UPDATE dream_job SET status = 'running', lock_owner = ?, started_at = ?, "
                         "attempts = attempts + 1, updated_at = ? WHERE id = ?",
                         (worker_id, utc_now(), utc_now(), dream_job_id))
        try:
            result = self._integrate(dream_job_id, worker_id)
        except AppError as exc:
            self._release_lock(worker_id)
            self.events.product_event("dream_failed", {"job_id": dream_job_id, "status": exc.code})
            return self._finish(dream_job_id, "failed", error_code=exc.code, error_message=exc.message)
        except Exception as exc:  # pragma: no cover - 兜底保证锁释放与状态可读
            self._release_lock(worker_id)
            self.events.product_event("dream_failed", {"job_id": dream_job_id, "status": "internal_error"})
            return self._finish(dream_job_id, "failed", error_code="internal_error",
                                error_message=str(exc))
        self._release_lock(worker_id)
        self.events.product_event(
            "dream_finished",
            {"job_id": dream_job_id, "session_count": result.get("session_count"),
             "changed_topics": len(result.get("changed_topic_ids") or []),
             "status": "completed"},
        )
        return self._finish(dream_job_id, "completed", result=result)

    # ------------------------------------------------------------------ 整合
    def _integrate(self, dream_job_id: str, worker_id: str) -> dict:
        last = self.store.query_one(
            "SELECT finished_at, created_at FROM dream_job WHERE profile_id = ? AND status = 'completed' "
            "AND id != ? ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1",
            (self.profile_id, dream_job_id),
        )
        since = (last["finished_at"] or last["created_at"]) if last else None
        logs = self.logs.list(status="active", limit=DREAM_MAX_LOGS) if self.logs else []
        if since:
            logs = [item for item in logs if (item.get("created_at") or "") > since]
        sessions = {item.get("session_id") for item in logs if item.get("session_id")}
        changed: list[str] = []
        created: list[str] = []
        pending: list[str] = []
        skipped: list[dict] = []
        model_usage = {"model_calls": 0, "tokens": 0, "unknown_usage": 0}

        # 1) 确定性路径：用户显式记忆请求直接成为生效主题
        for item in logs:
            if item.get("source") != SOURCE_USER_REQUEST:
                continue
            topic_type = _topic_type_from_tags(item.get("tags") or [])
            title = _title_from_content(item.get("content") or "")
            topic = self.topics.create(
                title=title,
                summary=item.get("content") or "",
                topic_type=topic_type,
                scope=item.get("scope") or SCOPE_TASK,
                workspace_id=item.get("workspace_id"),
                session_id=item.get("session_id"),
                body=item.get("content") or "",
                source="dream",
                source_kind="user",
                tags=item.get("tags") or [],
                sources=[{"source_kind": "log", "source_id": item["id"]}],
            )
            changed.append(topic["id"])
            created.append(topic["id"])

        # 2) 可选模型辅助：只生成候选，必须引用真实日志来源
        model_factory = self.model_factory
        if model_factory is not None and logs:
            try:
                client = model_factory()
            except Exception:  # pragma: no cover - 模型不可用时退回确定性路径
                client = None
            if client is not None:
                candidates, usage = self._model_candidates(client, logs)
                model_usage.update(usage)
                valid_ids = {item["id"] for item in logs}
                for candidate in candidates:
                    refs = [{"source_kind": "log", "source_id": log_id}
                            for log_id in candidate.get("log_ids") or [] if log_id in valid_ids]
                    if not refs:
                        skipped.append({"reason": "source_not_found",
                                        "title": candidate.get("title")})
                        continue
                    origin_log = next((item for item in logs
                                       if item["id"] == refs[0]["source_id"]), logs[0])
                    topic = self.topics.create(
                        title=candidate.get("title") or "",
                        summary=candidate.get("summary") or "",
                        topic_type=candidate.get("topic_type") or "",
                        scope=origin_log.get("scope") or SCOPE_TASK,
                        workspace_id=origin_log.get("workspace_id"),
                        session_id=origin_log.get("session_id"),
                        source="dream",
                        source_kind="model",
                        tags=candidate.get("tags") or [],
                        sources=refs,
                    )
                    changed.append(topic["id"])
                    if topic.get("status") == "pending_review":
                        pending.append(topic["id"])
                    else:
                        created.append(topic["id"])

        # 3) 索引重建 + 记忆目录写盘（派生视图）
        index_info = {}
        if self.index is not None:
            index_info = self.index.rebuild()
            try:
                index_info.update(self.index.write_file())
            except OSError as exc:  # pragma: no cover - 目录不可写时保留 DB 索引
                index_info["write_error"] = str(exc)

        scan = {
            "logs_considered": len(logs),
            "since": since,
            "session_count": len(sessions),
            "sessions": sorted(s for s in sessions if s),
        }
        result = {
            "changed_topic_ids": sorted(set(changed)),
            "created_topic_ids": sorted(set(created)),
            "pending_review_topic_ids": sorted(set(pending)),
            "skipped_candidates": skipped,
            "session_count": len(sessions),
            "logs_considered": len(logs),
            "index": {key: value for key, value in index_info.items() if key != "text"},
            "usage": model_usage,
            "write_scope": ["memory_log", "memory_topic", "memory_topic_source",
                            "memory_index_entry", "memory_recall"],
            "external_writes": [],
        }
        self._assert_memory_only()
        return result

    def _model_candidates(self, client, logs: list[dict]) -> tuple[list[dict], dict]:
        """模型辅助提炼：严格 JSON、来源必须真实存在、类型必须是四类之一或待分类。"""
        from ..platform.model import ChatMessage

        sample = logs[:60]
        catalogue = "\n".join(
            f"- log_id={item['id']} scope={item.get('scope')} 内容={_clip(item.get('content'), 200)}"
            for item in sample
        )
        prompt = (
            "你是记忆整合器。请从下列记录中提炼**稳定事实**（用户偏好、项目约定、关键决策、依赖事实），"
            "不要提炼一次性任务状态。只输出 JSON："
            '{"topics":[{"title":"...","summary":"...","topic_type":"user_preferences|project_conventions|'
            'key_decisions|dependency_facts","tags":["..."],"log_ids":["真实 log_id"]}]}。'
            "每条主题必须至少引用一个真实 log_id；无法引用的不要输出。\n\n记录：\n" + catalogue
        )
        calls = 0
        tokens = 0
        unknown = 0
        candidates: list[dict] = []
        while calls < DREAM_MAX_MODEL_CALLS:
            calls += 1
            reply = client.complete([ChatMessage(role="user", content=prompt)])
            usage = dict(getattr(reply, "usage", None) or {})
            if getattr(reply, "usage_known", False):
                tokens += int(usage.get("total_tokens") or 0)
            else:
                unknown += 1
            parsed = _parse_topics(reply.text or "")
            if parsed:
                candidates = parsed
                break
        return candidates, {"model_calls": calls, "tokens": tokens, "unknown_usage": unknown}

    def _assert_memory_only(self) -> None:
        """整合作业的写入边界自检：本服务不持有 skill / activation 依赖。"""
        for attr in ("skills", "activation"):
            if hasattr(self, attr):  # pragma: no cover - 只有误装配才会触发
                raise InvalidInput(f"整合服务不允许持有 {attr} 依赖，避免越权修改技能")

    # ------------------------------------------------------------------ 锁与状态
    def _lock_state(self, now: str | None = None) -> dict:
        row = self.store.query_one("SELECT * FROM dream_lock WHERE profile_id = ?", (self.profile_id,))
        if not row:
            return {"held": False}
        moment = now or utc_now()
        return {"held": row["expires_at"] > moment, "owner": row["owner"],
                "acquired_at": row["acquired_at"], "expires_at": row["expires_at"]}

    def _acquire_lock(self, owner: str) -> bool:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        expires = (now_dt + timedelta(seconds=DREAM_LOCK_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self.store.tx() as conn:
            row = conn.execute("SELECT * FROM dream_lock WHERE profile_id = ?",
                               (self.profile_id,)).fetchone()
            if row is not None and row["expires_at"] > now and row["owner"] != owner:
                return False
            conn.execute(
                "INSERT INTO dream_lock (profile_id, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(profile_id) DO UPDATE SET owner = excluded.owner, "
                "acquired_at = excluded.acquired_at, expires_at = excluded.expires_at",
                (self.profile_id, owner, now, expires),
            )
        return True

    def _release_lock(self, owner: str) -> None:
        try:
            with self.store.tx() as conn:
                conn.execute("DELETE FROM dream_lock WHERE profile_id = ? AND owner = ?",
                             (self.profile_id, owner))
        except Exception:  # pragma: no cover - 释放失败由租约到期兜底
            pass

    def _sessions_since(self, since: str | None) -> list[str]:
        sessions: set[str] = set()
        if self.logs is not None:
            sessions.update(self.logs.session_ids(since=since))
        rows = self.store.query(
            "SELECT session_id FROM memory_working WHERE profile_id = ? AND session_id IS NOT NULL"
            + (" AND updated_at > ?" if since else ""),
            (self.profile_id, since) if since else (self.profile_id,),
        )
        sessions.update(row["session_id"] for row in rows)
        return sorted(sessions)

    def _finish(self, dream_job_id: str, status: str, *, result: dict | None = None,
                error_code: str | None = None, error_message: str | None = None) -> dict:
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE dream_job SET status = ?, result = ?, error_code = ?, error_message = ?, "
                "finished_at = ?, updated_at = ?, lock_owner = NULL WHERE id = ?",
                (status, json.dumps(result or {}, ensure_ascii=False), error_code, error_message,
                 utc_now(), utc_now(), dream_job_id),
            )
        self.events.audit(f"memory.dream_{status}", "dream_job", dream_job_id,
                          after={"status": status, "error_code": error_code},
                          reason=error_message)
        return self.get(dream_job_id) or {}

    # ------------------------------------------------------------------ 读取
    def get(self, dream_job_id: str) -> dict | None:
        row = self.repo.get("dream_job", dream_job_id)
        return _decode(row) if row else None

    def list_jobs(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM dream_job WHERE profile_id = ?"
        params: list = [self.profile_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [_decode(row) for row in self.store.query(sql, params)]


# ---------------------------------------------------------------------- 辅助
def _hours_since(moment: str | None) -> float | None:
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0, 3)


def _topic_type_from_tags(tags: list) -> str:
    for tag in tags:
        value = str(tag).strip()
        if value in TOPIC_TYPES:
            return value
    return ""


def _title_from_content(content: str) -> str:
    first = (content or "").strip().splitlines()[0] if content.strip() else "记忆"
    return _clip(first, 60)


def _parse_topics(text: str) -> list[dict]:
    payload = _extract_json(text)
    if not isinstance(payload, dict):
        return []
    topics = payload.get("topics")
    if not isinstance(topics, list):
        return []
    result = []
    for item in topics:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not title or not summary:
            continue
        log_ids = [str(value) for value in (item.get("log_ids") or []) if value]
        result.append({
            "title": _clip(title, 60),
            "summary": _clip(summary, 400),
            "topic_type": normalize_topic_type(item.get("topic_type")),
            "tags": [str(tag) for tag in (item.get("tags") or [])][:8],
            "log_ids": log_ids,
        })
    return result


def _extract_json(text: str):
    import re

    body = text or ""
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fenced:
        body = fenced.group(1)
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(body[start:end + 1])
    except ValueError:
        return None


def _decode(row) -> dict:
    data = {key: row[key] for key in row.keys()}
    for field in ("gate", "scan", "result", "usage"):
        if isinstance(data.get(field), str):
            try:
                data[field] = json.loads(data[field])
            except (TypeError, ValueError):
                data[field] = {}
    return data


def _clip(text, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "…"
