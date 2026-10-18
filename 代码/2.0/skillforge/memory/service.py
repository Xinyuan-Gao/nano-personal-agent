from __future__ import annotations

import json
from pathlib import Path

from ..core.clock import utc_now
from ..core.config import read_json_setting, write_json_setting
from ..core.errors import InvalidInput
from ..core.ids import canonical_hash
from .dream import DreamService
from .index import MemoryIndexService
from .logs import SOURCE_MODEL_MARK, SOURCE_USER_REQUEST, DailyLogService
from .recall import RecallService
from .scope import (
    DEFAULT_RECALL_LIMIT,
    LOG_STATUS_ACTIVE,
    MAX_INDEX_LINES,
    MAX_RECALL_LIMIT,
    SCOPE_TASK,
    TOPIC_STATUS_ACTIVE,
    TOPIC_STATUS_DELETED,
    TOPIC_STATUS_PENDING_REVIEW,
    TOPIC_STATUS_WITHDRAWN,
    TOPIC_TYPES,
    TOPIC_UNCLASSIFIED,
    validate_scope,
)
from .topics import TopicService
from .working import WorkingMemoryService

SETTING_MEMORY_ENABLED = "memory.enabled"
SETTING_MEMORY_RECALL_LIMIT = "memory.recall_limit"
SETTING_MEMORY_RETENTION_DAYS = "memory.retention_days"

MIN_MEMORY_SAMPLE = 5


class MemoryService:
    def __init__(self, store, repo, events, config, profile_id: str = "local", idem=None,
                 model_factory=None, budget=None, queue=None) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.config = config
        self.profile_id = profile_id
        self.idem = idem
        self.budget = budget
        self.queue = queue
        self.model_factory = model_factory
        self.working = WorkingMemoryService(store, repo, events, config, profile_id)
        self.logs = DailyLogService(store, repo, events, config, profile_id)
        self.index = MemoryIndexService(store, repo, events, config, profile_id)
        self.topics = TopicService(store, repo, events, config, profile_id, index=self.index)
        self.recall_service = RecallService(store, repo, events, config, profile_id,
                                            topics=self.topics, working=self.working,
                                            logs=self.logs, index=self.index)
        self.dream = DreamService(store, repo, events, config, profile_id, topics=self.topics,
                                  logs=self.logs, working=self.working, index=self.index,
                                  budget=budget, model_factory=model_factory, queue=queue, idem=idem)

    # ================================================================== 设置
    def is_enabled(self) -> bool:
        return bool(read_json_setting(self.store, SETTING_MEMORY_ENABLED, True))

    def set_enabled(self, enabled: bool) -> dict:
        write_json_setting(self.store, SETTING_MEMORY_ENABLED, bool(enabled))
        self.events.audit("memory.set_enabled", "setting", SETTING_MEMORY_ENABLED,
                          after={"enabled": bool(enabled)})
        return self.settings()

    def recall_limit(self) -> int:
        value = read_json_setting(self.store, SETTING_MEMORY_RECALL_LIMIT, DEFAULT_RECALL_LIMIT)
        try:
            limit = int(value)
        except (TypeError, ValueError):
            limit = DEFAULT_RECALL_LIMIT
        return max(1, min(limit, MAX_RECALL_LIMIT))

    def set_recall_limit(self, limit: int) -> dict:
        value = max(1, min(int(limit), MAX_RECALL_LIMIT))
        write_json_setting(self.store, SETTING_MEMORY_RECALL_LIMIT, value)
        return self.settings()

    def retention_days(self) -> int | None:
        value = read_json_setting(self.store, SETTING_MEMORY_RETENTION_DAYS, None)
        return int(value) if value else None

    def set_retention_days(self, days: int | None) -> dict:
        write_json_setting(self.store, SETTING_MEMORY_RETENTION_DAYS,
                           None if not days else int(days))
        self.events.audit("memory.set_retention", "setting", SETTING_MEMORY_RETENTION_DAYS,
                          after={"days": None if not days else int(days)})
        return self.settings()

    def settings(self) -> dict:
        return {
            "enabled": self.is_enabled(),
            "recall_limit": self.recall_limit(),
            "retention_days": self.retention_days(),
            "layers": ["working", "daily_log", "durable_topic", "memory_index"],
            "topic_types": list(TOPIC_TYPES) + [TOPIC_UNCLASSIFIED],
        }

    # ================================================================== 显式写入
    def remember(
        self,
        content: str,
        *,
        scope: str = SCOPE_TASK,
        workspace_id: str | None = None,
        topic_type: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        source: str = SOURCE_USER_REQUEST,
        source_kind: str | None = None,
        tags: list | None = None,
        topic_id: str | None = None,
        idempotency_key: str | None = None,
        create_topic: bool = True,
    ) -> dict:
        """用户明确要求记住：先追加 daily log，再（默认）形成长期主题。

        ``create_topic=False`` 只写日志（例如“只记住这次任务”）。
        """
        text = (content or "").strip()
        if not text:
            raise InvalidInput("记忆内容不能为空", field_errors={"content": "required"})
        resolved_scope = validate_scope(scope, workspace_id=workspace_id, default=SCOPE_TASK)
        payload = {
            "content": text, "scope": resolved_scope, "workspace_id": workspace_id,
            "topic_type": topic_type, "title": title, "session_id": session_id, "run_id": run_id,
        }

        def _write() -> dict:
            log = self.logs.append(
                text,
                scope=resolved_scope,
                workspace_id=workspace_id,
                source=source,
                source_kind=source_kind,
                session_id=session_id,
                run_id=run_id,
                tags=list(tags or ([topic_type] if topic_type else [])),
            )
            result = {"log": log, "topic": None, "created_topic": False}
            if not create_topic:
                return result
            topic = None
            if topic_id:
                current = self.topics.require(topic_id)
                refs = [{"source_kind": "log", "source_id": log["id"]}]
                topic = self.topics.update(
                    topic_id,
                    title=title or current["title"],
                    summary=summary or text,
                    body=text,
                    sources=refs,
                )
            else:
                topic = self.topics.create(
                    title=title or _title_of(text),
                    summary=summary or text,
                    topic_type=topic_type or "",
                    scope=resolved_scope,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    body=text,
                    source=SOURCE_USER_REQUEST,
                    source_kind="user",
                    tags=list(tags or ([topic_type] if topic_type else [])),
                    sources=[{"source_kind": "log", "source_id": log["id"]}],
                )
            result["topic"] = topic
            result["created_topic"] = True
            return result

        if idempotency_key and self.idem is not None:
            return self.idem.run("memory.remember", idempotency_key, payload, _write)
        return _write()

    def append_log(self, *args, **kwargs) -> dict:
        return self.logs.append(*args, **kwargs)

    def mark_from_run(self, run_id: str, content: str, *, session_id: str | None = None,
                      workspace_id: str | None = None, tags: list | None = None) -> dict:
        """任务结果里的显式 memory 标记：只写日志，不直接进长期主题。"""
        return self.logs.append(
            content,
            scope=SCOPE_TASK,
            workspace_id=workspace_id,
            source=SOURCE_MODEL_MARK,
            source_kind="model",
            session_id=session_id,
            run_id=run_id,
            tags=tags,
        )

    # ================================================================== 任务召回
    def build_run_context(self, *, request: str, workspace_id: str, session_id: str | None = None,
                          profile_id: str | None = None, origin: str = "user") -> dict:
        """任务创建时冻结的记忆上下文（纯读，不写库）。"""
        base = {
            "enabled": False,
            "reason": None,
            "session_id": session_id,
            "workspace_id": workspace_id,
            "index": {"text": "", "entries": 0, "truncated": False, "omitted": 0},
            "recalled": [],
            "working": None,
            "status": "disabled",
        }
        if origin == "evaluation":
            # 评测 run 关闭个人长期记忆，保证两组初始环境隔离
            base["reason"] = "evaluation_isolation"
            return base
        if not self.is_enabled():
            base["reason"] = "memory_disabled"
            return base
        index_ctx = self.recall_service.index_context(record=False)
        recalled = self.recall_service.recall(
            request,
            workspace_id=workspace_id,
            session_id=session_id,
            limit=self.recall_limit(),
            record=False,
        )
        working = self.working.current(session_id, workspace_id, refresh=True)
        working_view = None
        if working is not None:
            working_view = {
                "session_id": working.get("session_id"),
                "task_summary": working.get("task_summary") or "",
                "run_count": working.get("run_count"),
                "files": [
                    {"path": item.get("path"), "summary": item.get("summary"),
                     "state": item.get("state", "current"), "content_hash": item.get("content_hash")}
                    for item in self.working.fresh_file_entries(working)
                ],
                "stale_files": [
                    {"path": item.get("path"), "state": item.get("state"),
                     "reason": item.get("stale_reason")}
                    for item in (working.get("recent_files") or [])
                    if item.get("state") not in ("current", None)
                ],
            }
        status = "injected" if (index_ctx.get("entries") or recalled.get("entries")) else "empty"
        return {
            "enabled": True,
            "reason": None,
            "session_id": session_id,
            "workspace_id": workspace_id,
            "index": index_ctx,
            "recalled": recalled.get("entries") or [],
            "recall_status": recalled.get("status"),
            "recall_meta": {
                "candidate_count": recalled.get("candidate_count"),
                "matched_count": recalled.get("matched_count"),
                "limit": recalled.get("limit"),
                "query_tokens": recalled.get("query_tokens"),
            },
            "working": working_view,
            "status": status,
        }

    def record_run_recall(self, run_id: str, context: dict) -> list[str]:
        """run 成功落库后写召回记录与 ``memory_recalled`` 事件。"""
        if not context or not context.get("enabled"):
            return []
        workspace_id = context.get("workspace_id")
        session_id = context.get("session_id")
        index_ctx = context.get("index") or {}
        ids: list[str] = []
        try:
            ids.extend(self.recall_service.record(
                run_id=run_id, session_id=session_id, workspace_id=workspace_id,
                entries=[], status="hit" if index_ctx.get("entries") else "none",
                layer="index", mode="index", injected=bool(index_ctx.get("entries")),
                reason={"entries": index_ctx.get("entries"), "truncated": index_ctx.get("truncated"),
                        "omitted": index_ctx.get("omitted"),
                        "degraded_reason": index_ctx.get("degraded_reason")},
            ))
            recalled = context.get("recalled") or []
            if recalled:
                ids.extend(self.recall_service.record(
                    run_id=run_id, session_id=session_id, workspace_id=workspace_id,
                    entries=recalled, status="hit", layer="topic", mode="full", injected=True,
                ))
            else:
                ids.extend(self.recall_service.record(
                    run_id=run_id, session_id=session_id, workspace_id=workspace_id,
                    entries=[], status="none", layer="topic", mode="none", injected=False,
                    reason={"query_tokens": (context.get("recall_meta") or {}).get("query_tokens")},
                ))
        except Exception as exc:  # pragma: no cover - 召回记录失败不影响任务本身
            try:
                self.events.product_event("memory_recall_record_failed",
                                          {"run_id": run_id, "error": str(exc)[:200]})
            except Exception:
                pass
        return ids

    def update_working_after_run(self, run_id: str, snapshot, *, output_text: str = "",
                                 status: str = "completed") -> dict | None:
        """任务结束后更新 working memory（不晋升为长期事实）。"""
        if not self.is_enabled():
            return None
        memory_context = getattr(snapshot, "memory", None) or {}
        if memory_context.get("enabled") is False and memory_context.get("reason") == "evaluation_isolation":
            return None
        try:
            return self.working.capture_run(
                run_id=run_id,
                request=getattr(snapshot, "request", "") or "",
                session_id=memory_context.get("session_id"),
                workspace_id=getattr(snapshot, "workspace_id", None),
                output_text=output_text or "",
                input_files=getattr(snapshot, "input_files", None),
            )
        except Exception:  # pragma: no cover - 工作记忆失败不得影响任务终态
            return None

    # ================================================================== 召回查询
    def recall(self, query: str, *, workspace_id: str | None = None, session_id: str | None = None,
               limit: int | None = None, run_id: str | None = None, record: bool = True) -> dict:
        if not self.is_enabled():
            return {"entries": [], "status": "disabled", "reason": "memory_disabled"}
        return self.recall_service.recall(
            query, workspace_id=workspace_id, session_id=session_id, run_id=run_id,
            limit=limit or self.recall_limit(), record=record,
        )

    def index_text(self) -> dict:
        return self.recall_service.index_context(record=False)

    def rebuild_index(self) -> dict:
        result = self.index.rebuild()
        try:
            result.update(self.index.write_file())
        except OSError as exc:  # pragma: no cover
            result["write_error"] = str(exc)
        return result

    def read_topic(self, topic_id: str, *, run_id: str | None = None,
                   session_id: str | None = None, workspace_id: str | None = None) -> dict:
        """按需读取主题正文，并记录一次「正文按需读取」召回。"""
        body = self.index.read_body(topic_id)
        if run_id or session_id:
            self.recall_service.record(
                run_id=run_id, session_id=session_id, workspace_id=workspace_id,
                entries=[{"memory_id": topic_id, "scope": body.get("scope"), "layer": "topic",
                          "score": None, "reason": {"mode": "on_demand_body"}}],
                status="hit", layer="topic", mode="full", injected=True,
            )
        return body

    def history(self, *, run_id: str | None = None, limit: int = 200) -> dict:
        return {"recalls": self.recall_service.history(run_id=run_id, limit=limit)}

    def mark_helpful(self, recall_id: str, helpful: bool) -> dict:
        return self.recall_service.mark_helpful(recall_id, helpful)

    # ================================================================== 状态与治理
    def status(self) -> dict:
        counts = {
            "working_sessions": self._count("memory_working"),
            "working_active": self._count("memory_working", "status = 'active'"),
            "logs": self._count("memory_log", "status != 'deleted'"),
            "logs_active": self._count("memory_log", f"status = '{LOG_STATUS_ACTIVE}'"),
            "topics": self._count("memory_topic", "status != 'deleted'"),
            "topics_active": self._count("memory_topic", f"status = '{TOPIC_STATUS_ACTIVE}'"),
            "topics_pending_review": self._count("memory_topic",
                                                 f"status = '{TOPIC_STATUS_PENDING_REVIEW}'"),
            "topics_withdrawn": self._count("memory_topic",
                                            f"status IN ('{TOPIC_STATUS_WITHDRAWN}', "
                                            f"'{TOPIC_STATUS_DELETED}')"),
            "index_entries": self._count("memory_index_entry", "status = 'active'"),
            "recalls": self._count("memory_recall"),
            "dream_jobs": self._count("dream_job"),
        }
        by_scope = {
            row["scope"]: int(row["count"])
            for row in self.store.query(
                "SELECT scope, COUNT(*) AS count FROM memory_topic WHERE status != 'deleted' "
                "GROUP BY scope")
        }
        return {
            "settings": self.settings(),
            "counts": counts,
            "topics_by_scope": by_scope,
            "index": self.index.render(),
            "gate": self.dream.gate(trigger="auto"),
            "layers": {
                "working_memory": "当前 session 的任务摘要与最近文件；不自动升级为长期事实",
                "daily_log": "按日期 append-only 的原始记录；不直接注入每轮 prompt",
                "durable_topic": "经整合的稳定事实（四类）；可撤回、删除、过期与 supersedes",
                "memory_index": f"MEMORY.md 短索引（上限 {MAX_INDEX_LINES} 行，"
                                "权威数据仍在主题表）",
            },
            "last_dream": (self.dream.list_jobs(limit=1) or [None])[0],
        }

    def conflicts(self) -> dict:
        return {"conflicts": self.topics.conflicts()}

    def expire(self, *, days: int | None = None) -> dict:
        """执行过期标记：主题按 ``expires_at`` 到期，日志按保留期限清正文。"""
        topics = self.topics.expire()
        effective = days if days is not None else self.retention_days()
        if not effective:
            return {"logs_expired": 0, "topics_expired": topics["expired"], "days": None,
                    "note": "未设置日志保留期限：正文长期本地保留；主题仍按 expires_at 到期"}
        logs = self.logs.expire(days=int(effective))
        return {"logs_expired": logs["expired"], "topics_expired": topics["expired"],
                "days": int(effective), "cutoff": logs.get("cutoff")}

    def purge_deleted(self) -> dict:
        """物理清理已删除 / 已撤回的正文与来源链（数据清除流程的一部分）。"""
        with self.store.tx() as conn:
            conn.execute("UPDATE memory_log SET content = '', tags = '[]' "
                         "WHERE status IN ('deleted', 'withdrawn')")
            conn.execute("UPDATE memory_topic SET body = '', summary = '', tags = '[]' "
                         "WHERE status IN ('deleted', 'withdrawn', 'superseded', 'expired')")
            conn.execute("DELETE FROM memory_index_entry WHERE topic_id IN "
                         "(SELECT id FROM memory_topic WHERE status != 'active')")
        return {"purged": True}

    def export(self, *, dest: str | None = None, include_body: bool = True,
               workspace_id: str | None = None) -> dict:
        """导出记忆：版本化 manifest + 四层内容；默认不含凭据（导出契约）。"""
        base = Path(dest) if dest else (Path(self.config.export_dir) / f"memory-{utc_now().replace(':', '')}")
        base.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "2.0",
            "exported_at": utc_now(),
            "scope": {"workspace_id": workspace_id},
            "settings": self.settings(),
            "working": self.working.list_sessions(),
            "logs": self.logs.list(limit=2000),
            "topics": self.topics.list(limit=2000),
            "index": self.index.entries(limit=1000),
            "recalls": self.recall_service.history(limit=2000),
            "dream_jobs": self.dream.list_jobs(limit=200),
        }
        if not include_body:
            for log in payload["logs"]:
                log["content"] = _redact(log.get("content"))
            for topic in payload["topics"]:
                topic["body"] = _redact(topic.get("body"))
                topic["summary"] = _redact(topic.get("summary"))
        manifest_path = base / "memory-export.json"
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (base / "MEMORY.md").write_text(self.index.render()["text"], encoding="utf-8")
        manifest = {
            "manifest_version": "2.0",
            "created_at": utc_now(),
            "objects": {key: len(value) if isinstance(value, list) else 1 for key, value in payload.items()
                        if key not in ("schema_version", "exported_at", "settings", "scope")},
            "include_body": bool(include_body),
            "credentials_included": False,
        }
        (base / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        manifest["manifest_hash"] = canonical_hash(manifest)
        manifest["dest"] = str(base)
        self.events.product_event("memory_exported", {"object_scope": "memory",
                                                      "manifest_id": manifest["manifest_hash"]})
        return manifest

    def metrics(self, *, workspace_id: str | None = None) -> dict:
        """记忆类三个指标（样本不足时返回 evidence_insufficient）。"""
        hit_rate = self._recall_hit_rate()
        integration = self._integration_accuracy()
        deletion = self._deletion_effectiveness()
        payload = {
            "applicable": True,
            "status": "available",
            "metrics": {
                "memory_recall_hit_rate": hit_rate,
                "memory_integration_accuracy": integration,
                "memory_deletion_effectiveness": deletion,
            },
        }
        payload["evidence_insufficient"] = any(
            item.get("evidence_insufficient") for item in payload["metrics"].values()
        )
        return payload

    # ------------------------------------------------------------------ 指标细节
    def _recall_hit_rate(self) -> dict:
        row = self.store.query_one(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END) AS helped "
            "FROM memory_recall WHERE profile_id = ? AND helpful IS NOT NULL",
            (self.profile_id,),
        )
        total = int(row["total"] or 0)
        helped = int(row["helped"] or 0)
        return {
            "value": (round(helped / total, 4) if total else None),
            "feedback_count": total,
            "helpful_count": helped,
            "definition": "用户确认有帮助的召回次数 / 有反馈的召回次数",
            "evidence_insufficient": total < MIN_MEMORY_SAMPLE,
        }

    def _integration_accuracy(self) -> dict:
        rows = self.store.query(
            "SELECT status FROM memory_topic WHERE profile_id = ? AND source = 'dream'",
            (self.profile_id,),
        )
        reviewed = [row["status"] for row in rows
                    if row["status"] != TOPIC_STATUS_PENDING_REVIEW]
        kept = [status for status in reviewed if status == TOPIC_STATUS_ACTIVE]
        return {
            "value": (round(len(kept) / len(reviewed), 4) if reviewed else None),
            "reviewed_count": len(reviewed),
            "kept_count": len(kept),
            "pending_review_count": len(rows) - len(reviewed),
            "definition": "dream 后仍生效且未在观察期撤回的主题数 / 已裁决的主题数",
            "evidence_insufficient": len(reviewed) < MIN_MEMORY_SAMPLE,
        }

    def _deletion_effectiveness(self) -> dict:
        rows = self.store.query(
            "SELECT id, COALESCE(withdrawn_at, updated_at) AS moment FROM memory_topic "
            "WHERE profile_id = ? AND status IN (?, ?, ?)",
            (self.profile_id, TOPIC_STATUS_WITHDRAWN, TOPIC_STATUS_DELETED, "expired"),
        )
        logs = self.store.query(
            "SELECT id, COALESCE(withdrawn_at, expired_at) AS moment FROM memory_log "
            "WHERE profile_id = ? AND status IN ('withdrawn', 'deleted')",
            (self.profile_id,),
        )
        records = [dict(row) for row in rows] + [dict(row) for row in logs]
        leaked = 0
        for record in records:
            moment = record.get("moment")
            if not moment:
                continue
            count = self.store.query_one(
                "SELECT COUNT(*) AS total FROM memory_recall WHERE memory_id = ? AND created_at > ?",
                (record["id"], moment),
            )
            if int(count["total"] or 0) > 0:
                leaked += 1
        total = len(records)
        return {
            "value": (round((total - leaked) / total, 4) if total else None),
            "removed_count": total,
            "re_recalled_count": leaked,
            "definition": "删除或撤回后未再次进入 prompt 的记录数 / 已删除或撤回记录数",
            "evidence_insufficient": total < MIN_MEMORY_SAMPLE,
        }

    def _count(self, table: str, where: str | None = None) -> int:
        sql = f"SELECT COUNT(*) AS total FROM {table} WHERE profile_id = ?"
        params: list = [self.profile_id]
        if where:
            sql += f" AND {where}"
        row = self.store.query_one(sql, params)
        return int(row["total"] or 0) if row else 0


def _title_of(text: str) -> str:
    first = (text or "").strip().splitlines()[0] if text.strip() else "记忆"
    return first if len(first) <= 60 else first[:59] + "…"


def _redact(text) -> str:
    return "（已按导出设置省略正文）" if text else ""
