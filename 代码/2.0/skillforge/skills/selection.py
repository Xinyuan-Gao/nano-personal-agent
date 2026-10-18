from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core.clock import utc_now
from ..core.errors import InvalidInput, InvalidState
from ..core.ids import new_id, sha256_hex
from .package import MAIN_FILE, find_contradictions

#: 默认最多加载 3 个 skill。
DEFAULT_MAX_SKILLS = 3

#: 默认上下文字符预算。
DEFAULT_CHAR_BUDGET = 48_000

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}")


@dataclass
class SelectionResult:
    candidates: list[dict]
    chosen: list[dict]
    skipped: list[dict]
    reason: str
    #: 互斥指令：[{"skill_ids": [...], "subject": ..., "detail": ...}]
    conflicts: list[dict] = field(default_factory=list)

    def chosen_ids(self) -> list[str]:
        return [item["skill_id"] for item in self.chosen]


class SkillSelector:
    def __init__(self, skills) -> None:
        self.skills = skills

    # ------------------------------------------------------------------ 索引
    def build_index(
        self,
        *,
        workspace_id,
        profile_id: str = "local",
        query: str | None = None,
        include_archived: bool = False,
    ) -> list[dict]:
        """轻量索引：skill_id / slug / name / description / scope / version_id。"""
        rows = self.skills.list_skills(
            workspace_id=workspace_id,
            query=query,
            include_archived=True,
            profile_id=profile_id,
        )
        index: list[dict] = []
        for row in rows:
            if row["status"] != "active" and not include_archived:
                continue
            if row["current_version_id"] is None:
                continue
            index.append(
                {
                    "skill_id": row["id"],
                    "slug": row["slug"],
                    "name": row["name"],
                    "description": row["description"],
                    "scope": row["scope"],
                    "version_id": row["current_version_id"],
                    "status": row["status"],
                    "revision": row["current_revision"],
                    "unverified": bool(row["unverified"]),
                    "shadowed": bool(row["shadowed"]),
                }
            )
        return index

    # ------------------------------------------------------------------ 选择
    def choose(
        self,
        request: str,
        *,
        workspace_id,
        explicit_skill_ids=None,
        chosen_by: str = "model",
        model_choice=None,
        max_skills: int = DEFAULT_MAX_SKILLS,
        profile_id: str = "local",
    ) -> SelectionResult:
        """选择要使用的 skill。

        - 用户显式指定的 skill 优先，且不会被模型选择挤出。
        - 默认最多 ``max_skills``（默认 3）个。
        - 越界 / 已归档 / 无版本的技能被跳过，并在 ``skipped`` 中给出 reason。
        """
        if max_skills <= 0:
            raise InvalidInput("max_skills 必须为正整数", field_errors={"max_skills": "invalid"})
        if chosen_by not in ("model", "user"):
            raise InvalidInput(f"不支持的 chosen_by: {chosen_by}", field_errors={"chosen_by": "unsupported"})

        candidates = self.build_index(workspace_id=workspace_id, profile_id=profile_id)
        by_id = {c["skill_id"]: c for c in candidates}
        skipped: list[dict] = []
        chosen: list[dict] = []
        chosen_ids: set[str] = set()
        explicit_ids = [str(i) for i in (explicit_skill_ids or [])]
        model_ids = _normalize_choice(model_choice)

        def take(skill_id: str, source: str) -> bool:
            if skill_id in chosen_ids:
                return False
            if len(chosen) >= max_skills:
                skipped.append({"skill_id": skill_id, "reason": "over_limit", "source": source})
                return False
            candidate = by_id.get(skill_id)
            if candidate is None:
                skipped.append(
                    {"skill_id": skill_id, "reason": self._unavailable_reason(skill_id, workspace_id), "source": source}
                )
                return False
            entry = dict(candidate)
            entry["chosen_by"] = source
            chosen.append(entry)
            chosen_ids.add(skill_id)
            return True

        for skill_id in explicit_ids:
            take(skill_id, "user")
        if chosen_by == "model":
            for skill_id in model_ids:
                take(skill_id, "model")
            ranked = sorted(
                (c for c in candidates if c["skill_id"] not in chosen_ids),
                key=lambda c: (-_score(request, c), c["name"], c["skill_id"]),
            )
            for candidate in ranked:
                take(candidate["skill_id"], "relevance")

        for row in self.skills.list_skills(
            workspace_id=workspace_id, include_archived=True, profile_id=profile_id
        ):
            if row["status"] != "active" or row["current_version_id"] is None:
                if row["id"] in chosen_ids:
                    continue
                reason = "archived" if row["status"] == "archived" else "no_version"
                if not any(item["skill_id"] == row["id"] for item in skipped):
                    skipped.append({"skill_id": row["id"], "reason": reason, "source": "scan"})

        reason = (
            f"候选 {len(candidates)} 个；显式指定 {len(explicit_ids)} 个；"
            f"命中 {len(chosen)} 个；跳过 {len(skipped)} 个"
        )
        return SelectionResult(
            candidates=candidates,
            chosen=chosen,
            skipped=skipped[:50],
            reason=reason,
            conflicts=[],
        )

    def _unavailable_reason(self, skill_id: str, workspace_id) -> str:
        row = self.skills.find_skill(skill_id)
        if row is None:
            return "not_found"
        if row["status"] != "active":
            return "archived"
        if row["scope"] == "workspace" and row["workspace_id"] != workspace_id:
            return "out_of_scope"
        if row["scope"] not in ("personal", "workspace"):
            return "out_of_scope"
        if self.skills.current_activation(skill_id) is None:
            return "no_version"
        return "unavailable"

    # ------------------------------------------------------------------ 加载
    def load_context(
        self,
        run_id: str,
        *,
        result: SelectionResult,
        allow_references: bool = True,
        char_budget: int = DEFAULT_CHAR_BUDGET,
        only_version_ids=None,
        record_index: bool = False,
    ) -> list[dict]:
        """把选中的 skill 正文（必要时含引用）注入上下文并记录 ``skill_load``。

        - 只有真正注入的内容记 ``loaded``；未加载 / 超预算记 ``skipped`` / ``truncated``
          并在 ``reason`` 里说明哪一部分没有加载。
        - 两个 skill 出现互斥指令时，保留优先级更高者，另一个记
          ``conflicting_instructions_need_user_decision``，不暗中合并。
        """
        if not isinstance(result, SelectionResult):
            raise InvalidInput("result 必须是 SelectionResult", field_errors={"result": "invalid"})
        budget = max(0, int(char_budget))
        allowed = set(str(v) for v in only_version_ids) if only_version_ids else None
        chosen = [c for c in result.chosen if allowed is None or str(c.get("version_id")) in allowed]
        if not chosen:
            raise InvalidState("没有可加载的已选技能", field_errors={"chosen": "empty"})

        records: list[dict] = []
        used = 0
        bodies: dict[str, tuple[dict, str, str]] = {}
        for candidate in chosen:
            version_id = candidate.get("version_id")
            files = self.skills.get_version_files(version_id)
            if not files:
                records.append(
                    _record(candidate, MAIN_FILE, "", "failed", "full", "版本没有文件清单")
                )
                continue
            lead = MAIN_FILE if any(f["path"] == MAIN_FILE for f in files) else files[0]["path"]
            try:
                text = self.skills.read_version_file(version_id, lead)
            except Exception as exc:  # noqa: BLE001 - 读取失败必须记录，不能让任务崩掉
                records.append(_record(candidate, lead, "", "failed", "full", f"读取失败: {exc}"))
                continue
            bodies[candidate["skill_id"]] = (candidate, lead, text)

        # --- 互斥指令检测 -----------------------------------
        ordered_ids = [c["skill_id"] for c in chosen if c["skill_id"] in bodies]
        conflicts = find_contradictions([bodies[sid][2] for sid in ordered_ids])
        dropped: dict[str, str] = {}
        conflict_records: list[dict] = []
        for conflict in conflicts:
            first = ordered_ids[conflict["index_a"]]
            second = ordered_ids[conflict["index_b"]]
            if first == second:
                conflict_records.append(
                    {
                        "skill_ids": [first],
                        "subject": conflict["subject"],
                        "kind": "internal",
                        "detail": "同一 skill 内存在互斥指令，需人工确认适用方法",
                    }
                )
                continue
            if second in dropped or first in dropped:
                continue
            dropped[second] = first
            conflict_records.append(
                {
                    "skill_ids": [first, second],
                    "subject": conflict["subject"],
                    "kind": "cross_skill",
                    "detail": "检测到互斥指令，等待用户确定适用方法，不暗中合并",
                }
            )
        result.conflicts = conflict_records

        if record_index:
            for candidate in result.candidates:
                line = f"{candidate['name']}: {candidate['description']}"[:200]
                records.append(
                    _record(candidate, "__index__", line, "loaded", "index", "轻量索引注入上下文")
                )
                used += len(line)

        for candidate in chosen:
            skill_id = candidate["skill_id"]
            if skill_id not in bodies:
                continue
            candidate, lead, text = bodies[skill_id]
            version_id = candidate.get("version_id")
            if skill_id in dropped:
                records.append(
                    _record(
                        candidate,
                        lead,
                        "",
                        "skipped",
                        "full",
                        f"与 {dropped[skill_id]} 存在互斥指令，未加载",
                    )
                )
                continue
            remaining = budget - used
            if len(text) > remaining:
                if remaining > 0:
                    injected = text[:remaining]
                    records.append(
                        _record(
                            candidate,
                            lead,
                            injected,
                            "truncated",
                            "full",
                            f"字符预算不足：{lead} 仅注入前 {remaining} 字符，其余 {len(text) - remaining} 字符未加载",
                        )
                    )
                    used += len(injected)
                else:
                    records.append(
                        _record(
                            candidate,
                            lead,
                            "",
                            "truncated",
                            "full",
                            f"字符预算已用尽：{lead}（{len(text)} 字符）整篇未加载",
                        )
                    )
                continue

            records.append(_record(candidate, lead, text, "loaded", "full", None))
            used += len(text)

            for entry in self.skills.get_version_files(version_id):
                if not entry["is_reference"]:
                    continue
                remaining = budget - used
                if not allow_references:
                    records.append(
                        _record(
                            candidate,
                            entry["path"],
                            "",
                            "skipped",
                            "reference",
                            "本次不允许加载引用文件（allow_references=False）",
                        )
                    )
                    continue
                try:
                    ref_text = self.skills.read_version_file(version_id, entry["path"])
                except Exception as exc:  # noqa: BLE001
                    records.append(
                        _record(candidate, entry["path"], "", "failed", "reference", f"引用读取失败: {exc}")
                    )
                    continue
                if len(ref_text) > remaining:
                    records.append(
                        _record(
                            candidate,
                            entry["path"],
                            "",
                            "truncated",
                            "reference",
                            f"字符预算不足：引用 {entry['path']}（{len(ref_text)} 字符）未加载",
                        )
                    )
                    continue
                records.append(_record(candidate, entry["path"], ref_text, "loaded", "reference", None))
                used += len(ref_text)

        with self.skills.store.tx() as conn:
            for record in records:
                conn.execute(
                    "INSERT INTO skill_load "
                    "(id, run_id, skill_id, version_id, file_path, content_hash, load_kind, chars, load_status, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("sload"),
                        run_id,
                        record["skill_id"],
                        record["version_id"],
                        record["file_path"],
                        record["content_hash"],
                        record["load_kind"],
                        record["chars"],
                        record["load_status"],
                        record["reason"],
                        utc_now(),
                    ),
                )
        return records

    def record_index_load(self, run_id: str, *, result: SelectionResult) -> list[dict]:
        """单独记录“只读轻量索引”这一步（索引确实注入上下文，故记 loaded+index）。

        索引记录不计入学习来源。
        """
        with self.skills.store.tx() as conn:
            for candidate in result.candidates:
                line = f"{candidate['name']}: {candidate['description']}"[:200]
                conn.execute(
                    "INSERT INTO skill_load "
                    "(id, run_id, skill_id, version_id, file_path, content_hash, load_kind, chars, load_status, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("sload"),
                        run_id,
                        candidate["skill_id"],
                        candidate["version_id"],
                        "__index__",
                        sha256_hex(line),
                        "index",
                        len(line),
                        "loaded",
                        "轻量索引注入上下文（不可作为学习来源）",
                        utc_now(),
                    ),
                )
        return self.skills.list_loads(run_id, kinds=("index",))


def _normalize_choice(model_choice) -> list[str]:
    if not model_choice:
        return []
    if isinstance(model_choice, str):
        return [model_choice]
    out: list[str] = []
    for item in model_choice:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            value = item.get("skill_id") or item.get("id")
            if value:
                out.append(str(value))
    return out


def _score(request: str, candidate: dict) -> float:
    """确定性的相关度打分：请求与 slug/name/description 的词面重合。"""
    if not request:
        return 0.0
    lowered = request.lower()
    text = f"{candidate['slug']} {candidate['name']} {candidate['description']}".lower()
    score = 0.0
    for token in set(_TOKEN_RE.findall(lowered)):
        if token in text:
            score += 1.0 if len(token) > 1 else 0.5
    return score


def _record(candidate: dict, file_path: str, text: str, status: str, kind: str, reason) -> dict:
    record = {
        "skill_id": candidate["skill_id"],
        "version_id": candidate["version_id"],
        "file_path": file_path,
        "chars": len(text),
        "load_kind": kind,
        "load_status": status,
        "reason": reason,
        "content_hash": sha256_hex(text if text else ""),
        "text": text,
        "injected": bool(text) and status in ("loaded", "truncated"),
    }
    return record
