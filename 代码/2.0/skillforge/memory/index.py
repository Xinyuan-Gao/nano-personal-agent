from __future__ import annotations

from pathlib import Path

from ..core.clock import utc_now
from ..core.errors import InvalidInput
from ..core.ids import new_id
from .scope import MAX_INDEX_CHARS, MAX_INDEX_LINES, TOPIC_STATUS_ACTIVE

INDEX_TITLE = "# MEMORY.md（长期记忆索引）"
SUMMARY_CLIP = 120
TITLE_CLIP = 60


class MemoryIndexService:
    def __init__(self, store, repo, events, config=None, profile_id: str = "local") -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.config = config
        self.profile_id = profile_id

    # ------------------------------------------------------------------ 同步
    def sync_topic(self, topic_id: str) -> dict | None:
        """按主题状态同步索引行：生效则写入，其余状态隐藏。"""
        topic = self.repo.get("memory_topic", topic_id)
        if topic is None:
            return None
        status = topic["status"]
        if status != TOPIC_STATUS_ACTIVE or not (topic["summary"] or "").strip():
            with self.store.tx() as conn:
                conn.execute("DELETE FROM memory_index_entry WHERE topic_id = ?", (topic_id,))
            self._rebuild_lines()
            return None
        title = _clip(topic["title"], TITLE_CLIP)
        summary = _clip(topic["summary"], SUMMARY_CLIP)
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO memory_index_entry (id, profile_id, topic_id, line_no, title, summary, "
                "status, updated_at) VALUES (?, ?, ?, "
                "(SELECT COALESCE(MAX(line_no), 0) + 1 FROM memory_index_entry WHERE profile_id = ?), "
                "?, ?, 'active', ?) "
                "ON CONFLICT(topic_id) DO UPDATE SET title = excluded.title, summary = excluded.summary, "
                "status = 'active', updated_at = excluded.updated_at",
                (new_id("mix"), self.profile_id, topic_id, self.profile_id, title, summary, utc_now()),
            )
        self._rebuild_lines()
        return self.get(topic_id)

    def rebuild(self) -> dict:
        """从主题表重建索引（含行号重排）；索引损坏或人工误删后可恢复。"""
        rows = self.store.query(
            "SELECT id, title, summary, updated_at FROM memory_topic "
            "WHERE profile_id = ? AND status = ? AND summary != '' ORDER BY updated_at DESC LIMIT ?",
            (self.profile_id, TOPIC_STATUS_ACTIVE, MAX_INDEX_LINES * 4),
        )
        with self.store.tx() as conn:
            conn.execute("DELETE FROM memory_index_entry WHERE profile_id = ?", (self.profile_id,))
            for position, row in enumerate(rows, start=1):
                conn.execute(
                    "INSERT INTO memory_index_entry (id, profile_id, topic_id, line_no, title, summary, "
                    "status, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                    (new_id("mix"), self.profile_id, row["id"], position,
                     _clip(row["title"], TITLE_CLIP), _clip(row["summary"], SUMMARY_CLIP),
                     row["updated_at"] or utc_now()),
                )
        return {"entries": len(rows)}

    def _rebuild_lines(self) -> None:
        rows = self.store.query(
            "SELECT id FROM memory_index_entry WHERE profile_id = ? AND status = 'active' "
            "ORDER BY line_no ASC, updated_at DESC",
            (self.profile_id,),
        )
        with self.store.tx() as conn:
            for position, row in enumerate(rows, start=1):
                conn.execute("UPDATE memory_index_entry SET line_no = ? WHERE id = ?",
                             (position, row["id"]))

    # ------------------------------------------------------------------ 读取
    def get(self, topic_id: str) -> dict | None:
        row = self.repo.get("memory_index_entry", topic_id, pk="topic_id")
        return {key: row[key] for key in row.keys()} if row else None

    def entries(self, *, limit: int | None = None) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM memory_index_entry WHERE profile_id = ? AND status = 'active' "
            "ORDER BY line_no ASC LIMIT ?",
            (self.profile_id, int(limit or MAX_INDEX_LINES)),
        )
        return [{key: row[key] for key in row.keys()} for row in rows]

    def render(self) -> dict:
        """渲染 MEMORY.md；超过行数上限时截断并给出降级说明。"""
        all_entries = self.entries(limit=MAX_INDEX_LINES * 4)
        visible = all_entries[:MAX_INDEX_LINES]
        header = [INDEX_TITLE, "",
                  f"> 只列主题索引与短摘要（上限 {MAX_INDEX_LINES} 行）；正文按需读取，不全部注入上下文。",
                  ""]
        entry_lines: list[str] = []
        for entry in visible:
            topic = self.repo.get("memory_topic", entry["topic_id"])
            topic_type = (topic["topic_type"] if topic is not None else "topic")
            scope = (topic["scope"] if topic is not None else "personal")
            entry_lines.append(f"- [{topic_type}/{scope}] {entry['title']} — {entry['summary']}")
        omitted = len(all_entries) - len(visible)
        notes: list[str] = []
        if omitted > 0:
            notes.append(f"> ⚠ 索引超过 {MAX_INDEX_LINES} 行上限，本次省略 {omitted} 条主题；"
                         "请整理、撤回或合并主题后再注入")
        # 字符上限只裁剪条目行，降级说明必须保留（否则等于静默丢弃）
        note_text = "\n".join(notes)
        budget = MAX_INDEX_CHARS - len("\n".join(header)) - len(note_text) - 4
        kept: list[str] = []
        used = 0
        for line in entry_lines:
            if used + len(line) + 1 > budget:
                break
            kept.append(line)
            used += len(line) + 1
        char_truncated = len(kept) < len(entry_lines)
        if char_truncated:
            notes.append(f"> ⚠ 索引文本超过 {MAX_INDEX_CHARS} 字符，已省略 "
                         f"{len(entry_lines) - len(kept)} 条主题行")
        lines = list(header) + kept
        if notes:
            lines += [""] + notes
        text = "\n".join(lines)
        return {
            "text": text,
            "entries": len(kept),
            "total_entries": len(all_entries),
            "lines": len(text.splitlines()),
            "truncated": bool(omitted) or char_truncated,
            "omitted": omitted,
            "char_truncated": char_truncated,
            "degraded_reason": notes[0][3:] if notes else None,
        }

    def write_file(self) -> dict:
        """把索引写到 ``<home>/memory/MEMORY.md``（派生文件，可重建）。"""
        rendered = self.render()
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered["text"], encoding="utf-8")
        return {"path": str(path), **rendered}

    def path(self) -> Path:
        base = getattr(self.config, "memory_dir", None)
        if base is None:
            base = Path(getattr(self.config, "home", Path.cwd())) / "memory"
        return Path(base) / "MEMORY.md"

    def read_body(self, topic_id: str) -> dict:
        """按需读取主题正文，并记录这是一次「正文按需读取」。"""
        topic = self.repo.get("memory_topic", topic_id)
        if topic is None:
            raise InvalidInput("主题不存在", field_errors={"topic_id": "not_found"})
        if topic["status"] not in (TOPIC_STATUS_ACTIVE, "pending_review"):
            raise InvalidInput("主题当前状态不可读取正文",
                               field_errors={"topic_id": "unavailable"})
        return {
            "topic_id": topic_id,
            "title": topic["title"],
            "summary": topic["summary"],
            "body": topic["body"] or "",
            "scope": topic["scope"],
            "topic_type": topic["topic_type"],
            "status": topic["status"],
            "revision": topic["revision"],
            "read_mode": "on_demand",
        }


def _clip(text: str, limit: int) -> str:
    text = " ".join(("" if text is None else str(text)).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
