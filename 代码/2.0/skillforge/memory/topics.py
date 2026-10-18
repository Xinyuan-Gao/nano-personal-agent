from __future__ import annotations

import json

from ..core.clock import utc_now
from ..core.errors import InvalidInput
from ..core.ids import canonical_hash, new_id
from .scope import (
    DEFAULT_SESSION,
    SCOPE_PERSONAL,
    SCOPE_TASK,
    TOPIC_STATUS_ACTIVE,
    TOPIC_STATUS_DELETED,
    TOPIC_STATUS_EXPIRED,
    TOPIC_STATUS_PENDING_REVIEW,
    TOPIC_STATUS_SUPERSEDED,
    TOPIC_STATUS_WITHDRAWN,
    TOPIC_TYPE_CHOICES,
    TOPIC_UNCLASSIFIED,
    normalize_topic_type,
    validate_scope,
)

SOURCE_USER_REQUEST = "user_request"
SOURCE_DREAM = "dream"
SOURCES = (SOURCE_USER_REQUEST, SOURCE_DREAM)

#: 允许被召回的长期主题状态（撤回 / 删除 / 被替代 / 过期的不得召回）
RECALLABLE_STATUSES = (TOPIC_STATUS_ACTIVE,)


class TopicService:
    def __init__(self, store, repo, events, config=None, profile_id: str = "local", index=None) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.config = config
        self.profile_id = profile_id
        self.index = index

    # ------------------------------------------------------------------ 写入
    def create(
        self,
        *,
        title: str,
        summary: str,
        topic_type: str,
        scope: str = SCOPE_TASK,
        workspace_id: str | None = None,
        session_id: str | None = None,
        body: str = "",
        source: str = SOURCE_USER_REQUEST,
        source_kind: str | None = None,
        tags: list | None = None,
        sources: list | None = None,
        expires_at: str | None = None,
        supersedes_id: str | None = None,
        allow_conflict: bool = False,
    ) -> dict:
        """创建一条长期主题；无来源的直接拒绝。

        ``allow_conflict=True`` 供冲突路径使用：明确要求新建一条与现有主题同键的主题
        由 ``supersedes_id`` 记录取代关系，而不是与已有主题合并。
        """
        name = (title or "").strip()
        text = (summary or "").strip()
        if not name or not text:
            raise InvalidInput("主题标题与摘要必填",
                               field_errors={"title": "required", "summary": "required"})
        source = source if source in SOURCES else SOURCE_USER_REQUEST
        resolved_scope = validate_scope(scope, workspace_id=workspace_id, default=SCOPE_TASK)
        refs = [dict(item) for item in (sources or []) if (item or {}).get("source_id")]
        if not refs:
            raise InvalidInput("长期主题必须带来源（log / run / feedback / user_request）",
                               field_errors={"sources": "required"})
        resolved_type = normalize_topic_type(topic_type)
        explicit = source == SOURCE_USER_REQUEST or (source_kind or "") == "user"
        session_id = _session_for(resolved_scope, session_id)
        fingerprint = _fingerprint(resolved_scope, workspace_id, session_id, resolved_type, name)
        existing = None if (allow_conflict or supersedes_id) else self.active_by_fingerprint(fingerprint)
        if existing is not None:
            return self._merge_existing(existing, summary=text, body=body, refs=refs,
                                        tags=tags, explicit=explicit, source=source)
        topic_id = new_id("mtp")
        now = utc_now()
        status = TOPIC_STATUS_ACTIVE if explicit else TOPIC_STATUS_PENDING_REVIEW
        row = {
            "id": topic_id,
            "profile_id": self.profile_id,
            "workspace_id": workspace_id,
            "scope": resolved_scope,
            "session_id": session_id,
            "topic_type": resolved_type,
            "title": name,
            "summary": text,
            "body": body or "",
            "source": source,
            "source_kind": source_kind or ("user" if explicit else "model"),
            "confidence": "explicit" if explicit else "inferred",
            "fingerprint": fingerprint,
            "supersedes_id": supersedes_id,
            "status": status,
            "revision": 1,
            "tags": json.dumps(list(tags or []), ensure_ascii=False),
            "expires_at": expires_at,
            "created_at": now,
            "updated_at": now,
        }
        with self.store.tx() as conn:
            self.repo.insert("memory_topic", row, conn=conn)
            self._insert_sources(conn, topic_id, refs)
        if supersedes_id and status == TOPIC_STATUS_ACTIVE:
            # 只有已生效的新事实才取代旧事实；待复核的推断冲突保留旧事实
            self._mark_superseded(supersedes_id, topic_id)
        self._sync_index(topic_id)
        self.events.audit("memory.topic_create", "memory_topic", topic_id,
                          after={"status": status, "topic_type": resolved_type, "scope": resolved_scope})
        return self.get(topic_id) or row

    def confirm(self, topic_id: str, *, note: str = "") -> dict:
        """用户裁决：待复核主题转为生效；若存在被它取代的旧主题，同时完成 supersedes。"""
        topic = self.require(topic_id)
        supersedes_id = topic.get("supersedes_id")
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE memory_topic SET status = ?, confidence = 'explicit', source_kind = 'user', "
                "updated_at = ? WHERE id = ?",
                (TOPIC_STATUS_ACTIVE, utc_now(), topic_id),
            )
        if supersedes_id:
            self._mark_superseded(supersedes_id, topic_id)
        self._sync_index(topic_id)
        self.events.audit("memory.topic_confirm", "memory_topic", topic_id,
                          before={"status": topic.get("status")},
                          after={"status": TOPIC_STATUS_ACTIVE}, reason=note or None)
        return self.get(topic_id) or topic

    def update(self, topic_id: str, *, title: str | None = None, summary: str | None = None,
               body: str | None = None, tags: list | None = None, scope: str | None = None,
               workspace_id: str | None = None, topic_type: str | None = None,
               sources: list | None = None) -> dict:
        """修改主题内容：revision 递增并重新同步索引（内容变更必须可追溯）。"""
        topic = self.require(topic_id)
        new_scope = topic["scope"] if scope is None else validate_scope(
            scope, workspace_id=workspace_id or topic.get("workspace_id"))
        new_session = _session_for(new_scope, topic.get("session_id"))
        new_type = normalize_topic_type(topic_type) if topic_type else topic["topic_type"]
        new_title = (title if title is not None else topic["title"]).strip()
        values = {
            "title": new_title,
            "summary": (summary if summary is not None else topic["summary"]).strip(),
            "body": body if body is not None else topic["body"],
            "tags": (json.dumps(list(tags), ensure_ascii=False) if tags is not None
                     else _encode_tags(topic.get("tags"))),
            "scope": new_scope,
            "workspace_id": workspace_id or topic.get("workspace_id"),
            "session_id": new_session,
            "topic_type": new_type,
            "fingerprint": _fingerprint(new_scope, workspace_id or topic.get("workspace_id"),
                                        new_session, new_type, new_title),
            "revision": int(topic.get("revision") or 1) + 1,
            "updated_at": utc_now(),
        }
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE memory_topic SET title = :title, summary = :summary, body = :body, tags = :tags, "
                "scope = :scope, workspace_id = :workspace_id, session_id = :session_id, "
                "topic_type = :topic_type, "
                "fingerprint = :fingerprint, revision = :revision, updated_at = :updated_at WHERE id = :id",
                dict(values, id=topic_id),
            )
            if sources:
                self._insert_sources(conn, topic_id, [dict(item) for item in sources])
        self._sync_index(topic_id)
        self.events.audit("memory.topic_update", "memory_topic", topic_id,
                          after={"revision": values["revision"]})
        return self.get(topic_id) or topic

    def add_sources(self, topic_id: str, sources: list[dict]) -> int:
        self.require(topic_id)
        refs = [dict(item) for item in (sources or []) if (item or {}).get("source_id")]
        if not refs:
            return 0
        with self.store.tx() as conn:
            inserted = self._insert_sources(conn, topic_id, refs)
        return inserted

    # ------------------------------------------------------------------ 生命周期
    def withdraw(self, topic_id: str, *, reason: str = "") -> dict:
        """撤回：不得再被召回；与删除分开记录。"""
        topic = self.require(topic_id)
        with self.store.tx() as conn:
            conn.execute("UPDATE memory_topic SET status = ?, withdrawn_at = ?, updated_at = ? WHERE id = ?",
                         (TOPIC_STATUS_WITHDRAWN, utc_now(), utc_now(), topic_id))
            conn.execute("UPDATE memory_index_entry SET status = 'hidden' WHERE topic_id = ?", (topic_id,))
        self.events.audit("memory.topic_withdraw", "memory_topic", topic_id,
                          before={"status": topic.get("status")},
                          after={"status": TOPIC_STATUS_WITHDRAWN}, reason=reason or None)
        return self.get(topic_id) or topic

    def delete(self, topic_id: str, *, reason: str = "") -> dict:
        """删除主题：正文清空、索引行删除，来源链与审计保留以便核对。"""
        topic = self.require(topic_id)
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE memory_topic SET status = ?, body = '', summary = '', title = title, "
                "updated_at = ? WHERE id = ?",
                (TOPIC_STATUS_DELETED, utc_now(), topic_id),
            )
            conn.execute("DELETE FROM memory_index_entry WHERE topic_id = ?", (topic_id,))
        self.events.audit("memory.topic_delete", "memory_topic", topic_id,
                          before={"status": topic.get("status")},
                          after={"status": TOPIC_STATUS_DELETED}, reason=reason or None)
        deleted = self.get(topic_id) or topic
        deleted["deleted"] = True
        return deleted

    def expire(self, *, now: str | None = None) -> dict:
        """把到期的长期事实标记为 expired，并隐藏索引。"""
        moment = now or utc_now()
        rows = self.store.query(
            "SELECT id FROM memory_topic WHERE profile_id = ? AND status = ? "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (self.profile_id, TOPIC_STATUS_ACTIVE, moment),
        )
        ids = [row["id"] for row in rows]
        for topic_id in ids:
            with self.store.tx() as conn:
                conn.execute("UPDATE memory_topic SET status = ?, updated_at = ? WHERE id = ?",
                             (TOPIC_STATUS_EXPIRED, utc_now(), topic_id))
                conn.execute("UPDATE memory_index_entry SET status = 'hidden' WHERE topic_id = ?",
                             (topic_id,))
        return {"expired": len(ids)}

    # ------------------------------------------------------------------ 读取
    def get(self, topic_id: str) -> dict | None:
        row = self.repo.get("memory_topic", topic_id)
        if row is None:
            return None
        topic = _decode(row)
        topic["sources"] = self.sources(topic_id)
        return topic

    def require(self, topic_id: str) -> dict:
        topic = self.get(topic_id)
        if topic is None:
            raise InvalidInput("主题不存在", field_errors={"topic_id": "not_found"})
        return topic

    def list(self, *, scope: str | None = None, workspace_id: str | None = None,
             status: str | None = None, topic_type: str | None = None,
             limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM memory_topic WHERE profile_id = ?"
        params: list = [self.profile_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if topic_type:
            sql += " AND topic_type = ?"
            params.append(topic_type)
        if workspace_id is not None:
            sql += (" AND (scope = 'personal' OR workspace_id IS NULL OR workspace_id = ?)")
            params.append(workspace_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        return [_decode(row) for row in self.store.query(sql, params)]

    def recallable(self, *, workspace_id: str | None, session_id: str | None,
                   statuses: tuple[str, ...] = RECALLABLE_STATUSES, limit: int = 200) -> list[dict]:
        """可见且可召回的长期主题：作用域 + session + 状态过滤。"""
        placeholders = ", ".join("?" for _ in statuses)
        session = session_id or DEFAULT_SESSION
        sql = (
            f"SELECT * FROM memory_topic WHERE profile_id = ? AND status IN ({placeholders})"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " AND (scope = 'personal'"
            "      OR (scope = 'workspace' AND workspace_id = ?)"
            "      OR (scope = 'task' AND (workspace_id IS NULL OR workspace_id = ?)"
            "          AND (session_id IS NULL OR session_id = ?)))"
            " ORDER BY updated_at DESC LIMIT ?"
        )
        params: list = [self.profile_id, *statuses, utc_now(), workspace_id, workspace_id,
                        session, int(limit)]
        return [_decode(row) for row in self.store.query(sql, params)]

    def sources(self, topic_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM memory_topic_source WHERE topic_id = ? ORDER BY created_at ASC",
            (topic_id,),
        )
        return [_decode_source(row) for row in rows]

    def conflicts(self) -> list[dict]:
        """待裁决的冲突：待复核主题 + 它取代的旧主题。"""
        pending = self.list(status=TOPIC_STATUS_PENDING_REVIEW, limit=200)
        result = []
        for topic in pending:
            old = self.get(topic["supersedes_id"]) if topic.get("supersedes_id") else None
            result.append({"topic": topic, "supersedes": old, "state": "pending_review"})
        return result

    def active_by_fingerprint(self, fingerprint: str) -> dict | None:
        row = self.store.query_one(
            "SELECT * FROM memory_topic WHERE profile_id = ? AND fingerprint = ? AND status = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (self.profile_id, fingerprint, TOPIC_STATUS_ACTIVE),
        )
        return _decode(row) if row else None

    # ------------------------------------------------------------------ 内部
    def _merge_existing(self, existing: dict, *, summary: str, body: str, refs: list[dict],
                        tags: list | None, explicit: bool, source: str) -> dict:
        """同一 scope/类型/标题 的重复写入：补来源、按需更新摘要，不制造重复主题。"""
        changed = False
        values: dict = {"updated_at": utc_now()}
        if summary and summary != existing["summary"]:
            if explicit:
                values["summary"] = summary
                values["body"] = body or existing.get("body") or ""
                values["source"] = source
                values["source_kind"] = "user"
                values["confidence"] = "explicit"
                changed = True
            else:
                # 推断内容与已有事实不同：不改写生效事实，另立待复核主题（冲突可见）
                return self._create_conflicting(existing, summary=summary, body=body, refs=refs,
                                                tags=tags, source=source)
        if tags:
            merged = sorted(set(list(existing.get("tags") or [])) | set(tags))
            if merged != list(existing.get("tags") or []):
                values["tags"] = json.dumps(merged, ensure_ascii=False)
                changed = True
        if changed:
            values["revision"] = int(existing.get("revision") or 1) + 1
        with self.store.tx() as conn:
            assignments = ", ".join(f"{key} = :{key}" for key in values)
            conn.execute(f"UPDATE memory_topic SET {assignments} WHERE id = :id",
                         dict(values, id=existing["id"]))
            self._insert_sources(conn, existing["id"], refs)
        self._sync_index(existing["id"])
        return self.get(existing["id"]) or existing

    def _create_conflicting(self, existing: dict, *, summary: str, body: str, refs: list[dict],
                            tags: list | None, source: str) -> dict:
        return self.create(
            title=existing["title"],
            summary=summary,
            topic_type=existing["topic_type"],
            scope=existing["scope"],
            workspace_id=existing.get("workspace_id"),
            session_id=existing.get("session_id"),
            body=body,
            source=source,
            source_kind="model",
            tags=tags,
            sources=refs,
            supersedes_id=existing["id"],
            allow_conflict=True,
        )

    def _mark_superseded(self, old_id: str, new_id_: str) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE memory_topic SET status = ?, superseded_by = ?, updated_at = ? WHERE id = ?",
                (TOPIC_STATUS_SUPERSEDED, new_id_, utc_now(), old_id),
            )
            conn.execute("UPDATE memory_index_entry SET status = 'hidden' WHERE topic_id = ?", (old_id,))

    def _insert_sources(self, conn, topic_id: str, refs: list[dict]) -> int:
        inserted = 0
        for ref in refs:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO memory_topic_source "
                "(topic_id, source_kind, source_id, evidence_locator, created_at) VALUES (?, ?, ?, ?, ?)",
                (topic_id, str(ref.get("source_kind") or "user_request"), str(ref["source_id"]),
                 json.dumps(ref.get("evidence_locator") or {}, ensure_ascii=False), utc_now()),
            )
            inserted += cursor.rowcount or 0
        return inserted

    def _sync_index(self, topic_id: str) -> None:
        if self.index is None:
            return
        try:
            self.index.sync_topic(topic_id)
        except Exception as exc:  # pragma: no cover - 索引同步失败不掩盖主题写入
            self.events.product_event("memory_index_sync_failed",
                                      {"topic_id": topic_id, "error": str(exc)[:200]})


def _session_for(scope: str, session_id: str | None) -> str | None:
    """task 作用域必须绑定 session：缺省即视为默认 session，避免跨任务串味。"""
    if scope != SCOPE_TASK:
        return None
    return (session_id or DEFAULT_SESSION).strip() or DEFAULT_SESSION


def _fingerprint(scope: str, workspace_id: str | None, session_id: str | None,
                 topic_type: str, title: str) -> str:
    return canonical_hash({
        "scope": scope,
        "workspace_id": workspace_id or "",
        "session_id": session_id or "",
        "topic_type": topic_type,
        "title": " ".join((title or "").split()).lower(),
    })


def _encode_tags(tags) -> str:
    if isinstance(tags, str):
        return tags
    return json.dumps(list(tags or []), ensure_ascii=False)


def _decode(row) -> dict:
    data = {key: row[key] for key in row.keys()}
    if isinstance(data.get("tags"), str):
        try:
            data["tags"] = json.loads(data["tags"])
        except (TypeError, ValueError):
            data["tags"] = []
    return data


def _decode_source(row) -> dict:
    data = {key: row[key] for key in row.keys()}
    if isinstance(data.get("evidence_locator"), str):
        try:
            data["evidence_locator"] = json.loads(data["evidence_locator"])
        except (TypeError, ValueError):
            data["evidence_locator"] = {}
    return data
