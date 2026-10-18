from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..core.clock import utc_now
from ..core.ids import new_id
from .scope import DEFAULT_RECALL_LIMIT, MAX_RECALL_LIMIT

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
#: 常见无信息中文词（避免“的/了/请”这类词造成假命中）
_CJK_STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "它", "和", "与", "请", "把", "都", "要", "会",
    "有", "没有", "这", "那", "个", "一个", "一下", "可以", "需要", "进行", "重新", "本次",
}
_EN_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at", "is", "are", "be",
    "please", "with", "this", "that", "it", "as", "by", "from", "we", "you", "i",
}


def tokenize(text: str) -> set[str]:
    """中英混合切词：英文按词，中文按二元组（单字仅在该片段本身就是单字时保留）。

    采用二元组而非单字枚举，避免“的/源/来”这类单字命中把分数抬成假高相关。
    """
    value = (text or "").lower()
    tokens: set[str] = {word for word in _WORD_RE.findall(value)
                        if len(word) > 1 and word not in _EN_STOPWORDS}
    for chunk in _CJK_RE.findall(value):
        chars = [ch for ch in chunk if ch not in _CJK_STOPWORDS]
        if len(chars) == 1:
            tokens.add(chars[0])
            continue
        for index in range(len(chars) - 1):
            tokens.add(chars[index] + chars[index + 1])
    return {token for token in tokens if token}


class RecallService:
    def __init__(self, store, repo, events, config=None, profile_id: str = "local",
                 topics=None, working=None, logs=None, index=None) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.config = config
        self.profile_id = profile_id
        self.topics = topics
        self.working = working
        self.logs = logs
        self.index = index

    # ------------------------------------------------------------------ 主入口
    def recall(self, query: str, *, workspace_id: str | None, session_id: str | None = None,
               run_id: str | None = None, limit: int = DEFAULT_RECALL_LIMIT,
               record: bool = True, statuses: tuple[str, ...] | None = None) -> dict:
        """按当前请求召回有限条目；返回 ``{"entries": [...], "status": hit|none|...}``。"""
        capped = max(1, min(int(limit or DEFAULT_RECALL_LIMIT), MAX_RECALL_LIMIT))
        tokens = tokenize(query)
        candidates = self._candidates(workspace_id=workspace_id, session_id=session_id,
                                      statuses=statuses)
        now = datetime.now(timezone.utc)
        scored: list[dict] = []
        for topic in candidates:
            score, reason = _score(topic, tokens, now)
            if score <= 0:
                continue
            scored.append({
                "memory_id": topic["id"],
                "layer": "topic",
                "title": topic["title"],
                "summary": topic["summary"],
                "scope": topic["scope"],
                "topic_type": topic["topic_type"],
                "revision": topic["revision"],
                "score": round(score, 4),
                "reason": reason,
                "sources": self._source_refs(topic["id"]),
            })
        scored.sort(key=lambda item: (-item["score"], item["memory_id"]))
        entries = scored[:capped]
        status = "hit" if entries else "none"
        if record:
            self.record(run_id=run_id, session_id=session_id, workspace_id=workspace_id,
                        entries=entries, status=status, query_tokens=sorted(tokens)[:40],
                        mode="full" if entries else "none")
        return {
            "entries": entries,
            "status": status,
            "candidate_count": len(candidates),
            "matched_count": len(scored),
            "limit": capped,
            "query_tokens": sorted(tokens)[:40],
        }

    def index_context(self, *, record: bool = False, run_id: str | None = None,
                      session_id: str | None = None, workspace_id: str | None = None) -> dict:
        """索引（MEMORY.md）注入上下文；总是先注入索引。"""
        if self.index is None:
            return {"text": "", "entries": 0, "truncated": False, "status": "skipped"}
        rendered = self.index.render()
        result = {
            "text": rendered["text"],
            "entries": rendered["entries"],
            "truncated": rendered["truncated"],
            "omitted": rendered["omitted"],
            "degraded_reason": rendered["degraded_reason"],
            "status": "injected" if rendered["entries"] else "empty",
        }
        if record:
            self.record(run_id=run_id, session_id=session_id, workspace_id=workspace_id,
                        entries=[], status="hit" if rendered["entries"] else "none",
                        layer="index", mode="index", injected=bool(rendered["entries"]),
                        reason={"entries": rendered["entries"], "truncated": rendered["truncated"],
                                "omitted": rendered["omitted"]},
                        memory_id=None)
        return result

    # ------------------------------------------------------------------ 记录
    def record(self, *, run_id: str | None, session_id: str | None, workspace_id: str | None,
               entries: list[dict], status: str, query_tokens: list[str] | None = None,
               layer: str = "topic", mode: str = "full", injected: bool = True,
               reason: dict | None = None, memory_id: str | None = None) -> list[str]:
        """写召回记录并发送 ``memory_recalled`` 产品事件。"""
        ids: list[str] = []
        rows = entries or [{
            "memory_id": memory_id,
            "scope": None,
            "score": None,
            "reason": dict(reason or {}, query_tokens=query_tokens or []),
            "layer": layer,
        }]
        with self.store.tx() as conn:
            for item in rows:
                recall_id = new_id("mrc")
                ids.append(recall_id)
                conn.execute(
                    "INSERT INTO memory_recall (id, profile_id, run_id, session_id, workspace_id, layer, "
                    "memory_id, scope, mode, score, reason, status, injected, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (recall_id, self.profile_id, run_id, session_id, workspace_id,
                     item.get("layer") or layer, item.get("memory_id"), item.get("scope"),
                     mode, item.get("score"), json.dumps(item.get("reason") or {}, ensure_ascii=False),
                     status, 1 if (injected and status == "hit") else 0, utc_now()),
                )
        for item in rows:
            self.events.product_event(
                "memory_recalled",
                {"memory_id": item.get("memory_id"), "scope": item.get("scope"),
                 "source": item.get("layer") or layer, "layer": item.get("layer") or layer,
                 "status": status, "run_id": run_id},
            )
        return ids

    def mark_helpful(self, recall_id: str, helpful: bool) -> dict:
        """用户确认某次召回是否有帮助（记忆召回命中率的分母口径）。"""
        row = self.repo.get("memory_recall", recall_id)
        if row is None:
            from ..core.errors import InvalidInput

            raise InvalidInput("召回记录不存在", field_errors={"recall_id": "not_found"})
        with self.store.tx() as conn:
            conn.execute("UPDATE memory_recall SET helpful = ? WHERE id = ?",
                         (1 if helpful else 0, recall_id))
        return {"recall_id": recall_id, "helpful": bool(helpful)}

    def history(self, *, run_id: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM memory_recall WHERE profile_id = ?"
        params: list = [self.profile_id]
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [_decode(row) for row in self.store.query(sql, params)]

    # ------------------------------------------------------------------ 内部
    def _candidates(self, *, workspace_id: str | None, session_id: str | None,
                    statuses: tuple[str, ...] | None = None) -> list[dict]:
        if self.topics is None:
            return []
        kwargs = {}
        if statuses:
            kwargs["statuses"] = statuses
        return self.topics.recallable(workspace_id=workspace_id, session_id=session_id, **kwargs)

    def _source_refs(self, topic_id: str) -> list[dict]:
        if self.topics is None:
            return []
        return [{"source_kind": item["source_kind"], "source_id": item["source_id"]}
                for item in self.topics.sources(topic_id)]


def _score(topic: dict, tokens: set[str], now: datetime) -> tuple[float, dict]:
    """可解释打分：标签 / 标题 / 摘要词命中 + 新鲜度，并在 reason 里说明每一分来源。"""
    if not tokens:
        return 0.0, {}
    tags = {str(tag).lower() for tag in (topic.get("tags") or [])}
    title_tokens = tokenize(topic.get("title") or "")
    summary_tokens = tokenize(topic.get("summary") or "")
    body_tokens = tokenize((topic.get("body") or "")[:2000])
    tag_hits = sorted(tokens & tags)
    title_hits = sorted(tokens & title_tokens)
    summary_hits = sorted(tokens & summary_tokens)
    body_hits = sorted(tokens & body_tokens)
    score = (3.0 * len(tag_hits)) + (2.0 * len(title_hits)) + (1.0 * len(summary_hits)) + \
            (0.5 * len(body_hits))
    if score <= 0:
        return 0.0, {}
    age_days = _age_days(topic.get("updated_at"), now)
    freshness = 0.0
    if age_days is not None:
        if age_days <= 1:
            freshness = 1.0
        elif age_days <= 7:
            freshness = 0.6
        elif age_days <= 30:
            freshness = 0.3
    if topic.get("scope") == "personal":
        # personal 事实更稳定，但绝不因为作用域就压过词命中
        freshness += 0.1
    reason = {
        "matched_tags": tag_hits,
        "matched_title_terms": title_hits,
        "matched_summary_terms": summary_hits,
        "matched_body_terms": body_hits[:10],
        "freshness_bonus": round(freshness, 3),
        "age_days": age_days,
        "scope": topic.get("scope"),
        "revision": topic.get("revision"),
    }
    return score + freshness, reason


def _age_days(updated_at: str | None, now: datetime) -> float | None:
    if not updated_at:
        return None
    value = str(updated_at).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return round((now - moment).total_seconds() / 86400.0, 3)


def _decode(row) -> dict:
    data = {key: row[key] for key in row.keys()}
    if isinstance(data.get("reason"), str):
        try:
            data["reason"] = json.loads(data["reason"])
        except (TypeError, ValueError):
            data["reason"] = {}
    return data
