from __future__ import annotations

import json
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ..core import config as core_config
from ..core.clock import utc_now
from ..core.errors import (
    BaselineConflict,
    EvidenceUnavailable,
    InvalidInput,
    InvalidState,
    MissingDependency,
    StorageError,
)
from ..core.ids import new_id
from ..platform.budget import spec_from_budget
from . import checks
from .lessons import CANDIDATE_ATTRIBUTIONS, attribution_advice

#: 仍会被基线 / 配置变化置为 stale 的候选状态
NON_TERMINAL_STATUSES = (
    "draft",
    "needs_clarification",
    "blocked",
    "ready",
    "evaluating",
    "eligible",
    "needs_evidence",
)
#: 终态：不再变化
TERMINAL_STATUSES = ("activated", "rejected", "superseded")
#: 用户拒绝候选的可选原因
REJECT_REASONS = (
    "not_worth_cost",
    "wrong_rule",
    "wrong_attribution",
    "duplicate",
    "only_this_time",
    "risk_too_high",
    "evidence_insufficient",
    "other",
)
#: 生成阶段最多 2 次模型调用（首次 + 1 次格式修复）
MAX_GENERATION_ATTEMPTS = 2
MAX_MATERIAL_FILE_CHARS = 6_000


class ProposalService:
    """候选服务。

    构造签名见 ARCHITECTURE §7::

        ProposalService(store, repo, events, idem, skills, lessons, model_factory, budget, blobs)
    """

    def __init__(
        self,
        store,
        repo,
        events,
        idem,
        skills,
        lessons,
        model_factory,
        budget,
        blobs,
        config=None,
        profile_id: str = "local",
    ) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.idem = idem
        self.skills = skills
        self.lessons = lessons
        self.model_factory = model_factory
        self.budget = budget
        self.blobs = blobs
        self.config = config
        self.profile_id = profile_id

    # ==================================================================== 生成候选
    def propose_change(
        self,
        *,
        skill_id,
        baseline_version_id=None,
        lesson_ids,
        budget=None,
        scope=None,
        idempotency_key=None,
        profile_id: str = "local",
    ) -> dict:
        """生成候选；``baseline_version_id=None`` 时取当前生效版本。"""
        payload = {
            "skill_id": skill_id,
            "baseline_version_id": baseline_version_id,
            "lesson_ids": list(lesson_ids or []),
            "budget": budget,
            "scope": scope,
            "profile_id": profile_id,
        }
        return self.idem.run(
            "proposal.propose", idempotency_key, payload, lambda: self._propose(payload)
        )

    def _propose(self, payload: Mapping) -> dict:
        skill_id = payload["skill_id"]
        skill = self.repo.get("skill", skill_id)
        if skill is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        baseline_version_id = self._resolve_baseline(skill_id, payload.get("baseline_version_id"))
        version_row = self.repo.get("skill_version", baseline_version_id)
        baseline_pkg = self._package_from_version(
            baseline_version_id, expected_hash=version_row["package_hash"]
        )
        activation = self._current_activation(skill_id)

        eligible, excluded, advice, dependency_hints = self._classify_lessons(
            skill_id, payload.get("lesson_ids") or []
        )
        if not eligible:
            return {
                "proposal_id": None,
                "status": "no_modification",
                "skill_id": skill_id,
                "baseline_version_id": baseline_version_id,
                "eligible_lesson_ids": [],
                "excluded": excluded,
                "advice": advice,
                "dependency_hints": dependency_hints,
                "reason": "没有可进入候选的经验（只有 skill_gap / skill_conflict 可改写 skill）",
            }

        scope_value = payload.get("scope") or skill["scope"] or "personal"
        if scope_value not in ("personal", "workspace"):
            raise InvalidInput(f"不支持的候选作用域: {scope_value}", field_errors={"scope": "unsupported"})
        proposal_id = new_id("prop")
        now = utc_now()
        baseline_meta = {
            "version_id": baseline_version_id,
            "package_hash": baseline_pkg.package_hash,
            "activation_revision": int(activation["revision"]) if activation else None,
            "activation_id": activation["id"] if activation else None,
        }
        generation_budget = {
            "spec": spec_from_budget(payload.get("budget"), core_config.DEFAULT_LEARNING_BUDGET).to_dict(),
            "baseline": baseline_meta,
            "model_fingerprint": self._current_model_fingerprint(),
            "lesson_ids": [row["id"] for row in eligible],
            "profile_id": payload.get("profile_id") or self.profile_id,
        }
        with self.store.tx() as conn:
            self.repo.insert(
                "proposal",
                {
                    "id": proposal_id,
                    "profile_id": payload.get("profile_id") or self.profile_id,
                    "skill_id": skill_id,
                    "baseline_version_id": baseline_version_id,
                    "candidate_version_id": None,
                    "supersedes_id": None,
                    "status": "draft",
                    "scope": scope_value,
                    "reason": "根据用户反馈 / 客观失败提炼的经验生成最小改动候选",
                    "summary": "",
                    "expected_benefit": "",
                    "potential_regression": "",
                    "attribution": eligible[0]["attribution"],
                    "checks": json.dumps({}, ensure_ascii=False),
                    "diff": json.dumps([], ensure_ascii=False),
                    "capability_change": json.dumps({}, ensure_ascii=False),
                    "generation_budget": json.dumps(generation_budget, ensure_ascii=False),
                    "evaluation": json.dumps({}, ensure_ascii=False),
                    "stale_reason": None,
                    "created_at": now,
                    "updated_at": now,
                },
                conn=conn,
            )
            for lesson in eligible:
                conn.execute(
                    "INSERT OR IGNORE INTO proposal_source (proposal_id, lesson_id) VALUES (?, ?)",
                    (proposal_id, lesson["id"]),
                )
            self.events.audit(
                "proposal.draft",
                "proposal",
                proposal_id,
                after={
                    "skill_id": skill_id,
                    "baseline_version_id": baseline_version_id,
                    "lesson_ids": [row["id"] for row in eligible],
                },
                conn=conn,
            )
        # 生成阶段的证据在模型调用之前就已落库：模型失败也不会丢来源链
        materials, filtered = self._generation_materials(skill, version_row, baseline_pkg, eligible)
        scope_id = f"proposal:{proposal_id}"
        self.budget.open(scope_id, spec_from_budget(payload.get("budget"), core_config.DEFAULT_LEARNING_BUDGET))
        reply, meta = self._generate(scope_id, materials)

        if reply is None:
            reason = meta.get("message") or meta.get("reason") or "generation_failed"
            self._update_proposal(
                proposal_id,
                {
                    "status": "draft",
                    "checks": json.dumps(
                        {
                            "generation_failed": True,
                            "generation_error": str(reason)[:500],
                            "generated_at": utc_now(),
                            "filtered_materials": filtered,
                            "model_meta": meta,
                        },
                        ensure_ascii=False,
                    ),
                    "updated_at": utc_now(),
                },
            )
            self.events.audit(
                "proposal.generation_failed",
                "proposal",
                proposal_id,
                after={"error": str(reason)[:300], "evidence_kept": True},
            )
            data = self.get_proposal(proposal_id, refresh_stale=False)
            data["generation_failed"] = True
            data["excluded"] = excluded
            data["advice"] = advice
            data["dependency_hints"] = dependency_hints
            return data

        return self._apply_generated(
            proposal_id=proposal_id,
            skill=skill,
            baseline_pkg=baseline_pkg,
            version_row=version_row,
            eligible=eligible,
            reply=reply,
            meta=meta,
            filtered=filtered,
            generation_budget=generation_budget,
            excluded=excluded,
            advice=advice,
            dependency_hints=dependency_hints,
        )

    def _apply_generated(
        self,
        *,
        proposal_id,
        skill,
        baseline_pkg,
        version_row,
        eligible,
        reply,
        meta,
        filtered,
        generation_budget,
        excluded,
        advice,
        dependency_hints,
    ) -> dict:
        skill_id = skill["id"]
        baseline_version_id = version_row["id"]
        try:
            candidate_files = self._apply_edits(baseline_pkg, reply)
            candidate_pkg = checks.build_package(candidate_files)
        except InvalidInput as exc:
            # 生成产物本身不可用（例如删掉 SKILL.md）：保留证据，标记需要重新生成
            self._update_proposal(
                proposal_id,
                {
                    "checks": json.dumps(
                        {
                            "generation_failed": True,
                            "generation_error": exc.message,
                            "generated_at": utc_now(),
                            "filtered_materials": filtered,
                        },
                        ensure_ascii=False,
                    ),
                    "updated_at": utc_now(),
                },
            )
            self.events.audit(
                "proposal.generation_failed", "proposal", proposal_id, after={"error": exc.message}
            )
            data = self.get_proposal(proposal_id, refresh_stale=False)
            data["generation_failed"] = True
            data["excluded"] = excluded
            data["advice"] = advice
            data["dependency_hints"] = dependency_hints
            return data

        static = checks.run_static_checks(candidate_pkg, baseline_pkg)
        capability = static["capability_change"]
        hard_failed = bool(static["hard_failed"])
        candidate_version_id = None
        if not hard_failed:
            candidate_version_id = self._create_candidate_version(
                skill_id=skill_id,
                candidate_pkg=candidate_pkg,
                parent_version_id=baseline_version_id,
                proposal_id=proposal_id,
                note=(reply.get("summary") if isinstance(reply, Mapping) else None),
            )
        if hard_failed:
            status = "blocked"
        elif capability.get("requires_authorization"):
            status = "needs_clarification"
        else:
            status = "ready"

        summary = str(reply.get("summary") or "").strip() if isinstance(reply, Mapping) else ""
        expected_benefit = str(reply.get("expected_benefit") or "").strip() if isinstance(reply, Mapping) else ""
        potential_regression = (
            str(reply.get("potential_regression") or "").strip() if isinstance(reply, Mapping) else ""
        )
        changed_steps = _as_str_list(reply.get("changed_steps") if isinstance(reply, Mapping) else None)
        generation_budget = dict(generation_budget)
        generation_budget["usage"] = meta
        generation_budget["changed_steps"] = changed_steps
        diff = checks.diff_report(baseline_pkg, candidate_pkg)
        changed_diff = [item for item in diff if item.get("change") != "unchanged"]
        checks_payload = {
            "level_hard": static["level_hard"],
            "level_hint": static["level_hint"],
            "level_risk": static["level_risk"],
            "hard_failed": hard_failed,
            "blocked_reason": checks.blocked_reason(static),
            "rewrite_review_required": static["rewrite_review_required"],
            "needs_manual_confirmation": static["rewrite_review_required"],
            "body_diff": static["body_diff"],
            "baseline_package_hash": static["baseline_package_hash"],
            "candidate_package_hash": static["candidate_package_hash"],
            "checked_at": static["checked_at"],
            "filtered_materials": filtered,
            "unchanged_files": [item["path"] for item in diff if item.get("change") == "unchanged"],
            # 兼容别名：便于界面与其他层按通用键名读取
            "hard": static["level_hard"],
            "hints": static["level_hint"],
            "risks": static["level_risk"],
        }
        now = utc_now()
        updates: dict[str, Any] = {
            "candidate_version_id": candidate_version_id,
            "status": status,
            "summary": summary,
            "expected_benefit": expected_benefit,
            "potential_regression": potential_regression,
            "attribution": eligible[0]["attribution"],
            "checks": json.dumps(checks_payload, ensure_ascii=False),
            "diff": json.dumps(changed_diff, ensure_ascii=False),
            "capability_change": json.dumps(capability, ensure_ascii=False),
            "generation_budget": json.dumps(generation_budget, ensure_ascii=False),
            "updated_at": now,
        }
        self._update_proposal(proposal_id, updates)
        with self.store.tx() as conn:
            for lesson in eligible:
                if lesson["status"] == "draft":
                    conn.execute(
                        "UPDATE lesson SET status = 'proposed', updated_at = ? WHERE id = ?",
                        (now, lesson["id"]),
                    )
            self.events.audit(
                "proposal.create",
                "proposal",
                proposal_id,
                after={
                    "status": status,
                    "candidate_version_id": candidate_version_id,
                    "hard_failed": hard_failed,
                    "capability_change": capability.get("requires_authorization", False),
                    "rewrite_review_required": static["rewrite_review_required"],
                    "diff_files": len(diff),
                },
                conn=conn,
            )
        self.events.product_event(
            "proposal.created",
            {
                "status": status,
                "hard_failed": hard_failed,
                "capability_change": bool(capability.get("requires_authorization")),
                "attribution": eligible[0]["attribution"],
            },
            event_id=f"proposal.created:{proposal_id}",
        )
        data = self.get_proposal(proposal_id, refresh_stale=False)
        data["excluded"] = excluded
        data["advice"] = advice
        data["dependency_hints"] = dependency_hints
        data["generation_failed"] = False
        return data

    # ==================================================================== 编辑候选
    def edit_candidate(
        self,
        proposal_id,
        *,
        edits: dict[str, str],
        note=None,
        idempotency_key=None,
    ) -> dict:
        """用户编辑 → 新的不可变候选版本；旧候选 superseded，旧检查与旧报告失效。"""
        payload = {"proposal_id": proposal_id, "edits": dict(edits or {}), "note": note}
        return self.idem.run(
            "proposal.edit", idempotency_key, payload, lambda: self._edit(proposal_id, dict(edits or {}), note)
        )

    def _edit(self, proposal_id, edits: Mapping, note) -> dict:
        row = self.repo.get("proposal", proposal_id)
        if row is None:
            raise InvalidInput(f"候选不存在: {proposal_id}", field_errors={"proposal_id": "not_found"})
        if row["status"] in TERMINAL_STATUSES:
            raise InvalidState(
                f"候选状态为 {row['status']}，不能再编辑", field_errors={"status": "terminal"}
            )
        if not edits:
            raise InvalidInput("edits 不能为空", field_errors={"edits": "empty"})
        base_version_id = row["candidate_version_id"] or row["baseline_version_id"]
        base_row = self.repo.get("skill_version", base_version_id)
        base_pkg = self._package_from_version(base_version_id, expected_hash=base_row["package_hash"])
        files = self._apply_edits(base_pkg, {"edits": edits})
        candidate_pkg = checks.build_package(files)
        baseline_pkg = self._package_from_version(row["baseline_version_id"])
        static = checks.run_static_checks(candidate_pkg, baseline_pkg)
        capability = static["capability_change"]
        hard_failed = bool(static["hard_failed"])
        new_id_value = new_id("prop")
        now = utc_now()
        if hard_failed:
            status = "blocked"
        elif capability.get("requires_authorization"):
            status = "needs_clarification"
        else:
            status = "ready"
        candidate_version_id = None
        old_evaluation = _json(row["evaluation"], {})
        old_evaluation.update(
            {
                "report_valid": False,
                "invalidated": True,
                "invalidated_reason": "candidate_edited",
                "invalidated_at": now,
                "superseded_by": new_id_value,
            }
        )
        old_checks = _json(row["checks"], {})
        old_checks.update(
            {"invalidated": True, "invalidated_reason": "candidate_edited", "invalidated_at": now}
        )
        generation_budget = _json(row["generation_budget"], {})
        generation_budget = dict(generation_budget)
        generation_budget["edited_from"] = proposal_id
        generation_budget["changed_steps"] = ["用户编辑候选内容"]
        diff = checks.diff_report(baseline_pkg, candidate_pkg)
        changed_diff = [item for item in diff if item.get("change") != "unchanged"]
        new_checks = {
            "level_hard": static["level_hard"],
            "level_hint": static["level_hint"],
            "level_risk": static["level_risk"],
            "hard_failed": hard_failed,
            "blocked_reason": checks.blocked_reason(static),
            "rewrite_review_required": static["rewrite_review_required"],
            "needs_manual_confirmation": static["rewrite_review_required"],
            "body_diff": static["body_diff"],
            "checked_at": static["checked_at"],
            "edited": True,
        }
        with self.store.tx() as conn:
            # 新候选版本与旧候选 superseded 必须同事务提交
            if not hard_failed:
                candidate_version_id = self._create_candidate_version(
                    skill_id=row["skill_id"],
                    candidate_pkg=candidate_pkg,
                    parent_version_id=base_version_id,
                    proposal_id=new_id_value,
                    note=note or f"用户编辑候选 {proposal_id}",
                    validation_status="passed",
                    conn=conn,
                )
            self.repo.insert(
                "proposal",
                {
                    "id": new_id_value,
                    "profile_id": row["profile_id"],
                    "skill_id": row["skill_id"],
                    "baseline_version_id": row["baseline_version_id"],
                    "candidate_version_id": candidate_version_id,
                    "supersedes_id": proposal_id,
                    "status": status,
                    "scope": row["scope"],
                    "reason": row["reason"],
                    "summary": note or f"用户编辑候选 {proposal_id} 生成的新候选",
                    "expected_benefit": row["expected_benefit"],
                    "potential_regression": row["potential_regression"],
                    "attribution": row["attribution"],
                    "checks": json.dumps(new_checks, ensure_ascii=False),
                    "diff": json.dumps(changed_diff, ensure_ascii=False),
                    "capability_change": json.dumps(capability, ensure_ascii=False),
                    "generation_budget": json.dumps(generation_budget, ensure_ascii=False),
                    "evaluation": json.dumps({}, ensure_ascii=False),
                    "created_at": now,
                    "updated_at": now,
                },
                conn=conn,
            )
            sources = conn.execute(
                "SELECT lesson_id FROM proposal_source WHERE proposal_id = ?", (proposal_id,)
            ).fetchall()
            for source in sources:
                conn.execute(
                    "INSERT OR IGNORE INTO proposal_source (proposal_id, lesson_id) VALUES (?, ?)",
                    (new_id_value, source["lesson_id"]),
                )
            conn.execute(
                "UPDATE proposal SET status = 'superseded', checks = ?, evaluation = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    json.dumps(old_checks, ensure_ascii=False),
                    json.dumps(old_evaluation, ensure_ascii=False),
                    now,
                    proposal_id,
                ),
            )
            self.events.audit(
                "proposal.edit",
                "proposal",
                new_id_value,
                before={"superseded_proposal_id": proposal_id},
                after={
                    "status": status,
                    "candidate_version_id": candidate_version_id,
                    "old_reports_invalidated": True,
                },
                conn=conn,
            )
            self.events.audit(
                "proposal.superseded",
                "proposal",
                proposal_id,
                before={"status": row["status"]},
                after={"status": "superseded", "superseded_by": new_id_value, "reports_invalidated": True},
                conn=conn,
            )
        self.events.product_event(
            "proposal.edited",
            {"reports_invalidated": True, "hard_failed": hard_failed},
            event_id=f"proposal.edited:{new_id_value}",
        )
        invalidated_reports = self._invalidate_reports(proposal_id, reason="candidate_edited")
        data = self.get_proposal(new_id_value, refresh_stale=False)
        data["superseded_proposal_id"] = proposal_id
        data["reports_invalidated"] = True
        data["invalidated_reports"] = invalidated_reports
        return data

    def _invalidate_reports(self, proposal_id: str, *, reason: str) -> list[str]:
        """把该候选已有的评测报告标记为不可用（不改评测层的原始记录）。"""
        rows = self.store.query(
            "SELECT id FROM eval_job WHERE proposal_id = ? ORDER BY created_at ASC", (proposal_id,)
        )
        ids = [row["id"] for row in rows]
        for job_id in ids:
            self.events.audit(
                "proposal.report_invalidated",
                "eval_job",
                job_id,
                after={"reason": reason, "proposal_id": proposal_id},
            )
        return ids

    # ==================================================================== 状态
    def mark_stale(self, proposal_id, *, reason, conn=None) -> dict:
        row = self.repo.get("proposal", proposal_id)
        if row is None:
            raise InvalidInput(f"候选不存在: {proposal_id}", field_errors={"proposal_id": "not_found"})
        if row["status"] in TERMINAL_STATUSES:
            data = self.get_proposal(proposal_id, refresh_stale=False)
            data["stale"] = row["status"] == "stale"
            data["note"] = f"候选已是终态 {row['status']}，保持原状（保留旧差异与报告）"
            return data
        now = utc_now()
        sql = "UPDATE proposal SET status = 'stale', stale_reason = ?, updated_at = ? WHERE id = ?"
        params = (reason, now, proposal_id)
        if conn is not None:
            conn.execute(sql, params)
            self.events.audit(
                "proposal.stale",
                "proposal",
                proposal_id,
                before={"status": row["status"]},
                after={"status": "stale", "reason": reason},
                conn=conn,
            )
        else:
            with self.store.tx() as own_conn:
                own_conn.execute(sql, params)
                self.events.audit(
                    "proposal.stale",
                    "proposal",
                    proposal_id,
                    before={"status": row["status"]},
                    after={"status": "stale", "reason": reason},
                    conn=own_conn,
                )
        self.events.product_event(
            "proposal.stale", {"reason": str(reason)[:80]}, event_id=f"proposal.stale:{proposal_id}:{now}"
        )
        data = self.get_proposal(proposal_id, refresh_stale=False)
        data["stale"] = True
        return data

    def reject(self, proposal_id, *, reason_code, actor: str = "owner") -> dict:
        if reason_code not in REJECT_REASONS:
            raise InvalidInput(
                f"不支持的拒绝原因: {reason_code}", field_errors={"reason_code": "unsupported"}
            )
        row = self.repo.get("proposal", proposal_id)
        if row is None:
            raise InvalidInput(f"候选不存在: {proposal_id}", field_errors={"proposal_id": "not_found"})
        if row["status"] in TERMINAL_STATUSES:
            raise InvalidState(
                f"候选状态为 {row['status']}，不能再次拒绝", field_errors={"status": "terminal"}
            )
        with self.store.tx() as conn:
            self.repo.update(
                "proposal",
                proposal_id,
                {"status": "rejected", "reject_reason_code": reason_code, "updated_at": utc_now()},
                conn=conn,
            )
            self.events.audit(
                "proposal.reject",
                "proposal",
                proposal_id,
                before={"status": row["status"]},
                after={"status": "rejected", "reason_code": reason_code},
                actor=actor,
                conn=conn,
            )
        self.events.product_event(
            "proposal.rejected", {"reason_code": reason_code}, event_id=f"proposal.reject:{proposal_id}"
        )
        data = self.get_proposal(proposal_id, refresh_stale=False)
        data["rejected"] = True
        return data

    def check_proposal(self, proposal_id) -> dict:
        """重跑硬检查 + 质量提示；硬检查失败立即 blocked。"""
        row = self.repo.get("proposal", proposal_id)
        if row is None:
            raise InvalidInput(f"候选不存在: {proposal_id}", field_errors={"proposal_id": "not_found"})
        stored = _json(row["checks"], {})
        if not row["candidate_version_id"]:
            out = dict(stored)
            out.update(
                {
                    "proposal_id": proposal_id,
                    "rechecked": False,
                    "status": row["status"],
                    "note": "候选未落盘（硬检查阻断或生成失败），沿用已有检查结论",
                }
            )
            return out
        baseline_row = self.repo.get("skill_version", row["baseline_version_id"])
        baseline_pkg = self._package_from_version(
            row["baseline_version_id"],
            expected_hash=baseline_row["package_hash"] if baseline_row else None,
        )
        try:
            candidate_pkg = self._package_from_version(row["candidate_version_id"])
        except (StorageError, EvidenceUnavailable) as exc:
            payload = {
                "level_hard": [
                    {
                        "level": "hard",
                        "code": "candidate_package_unavailable",
                        "message": f"候选完整包不可用或 hash 不一致: {exc.message}",
                        "locator": None,
                    }
                ],
                "level_hint": [],
                "level_risk": [],
                "hard_failed": True,
                "blocked_reason": f"候选完整包不可用：{exc.message}",
                "rewrite_review_required": False,
                "needs_manual_confirmation": False,
                "checked_at": utc_now(),
            }
            status = "blocked" if row["status"] not in TERMINAL_STATUSES else row["status"]
            self._update_proposal(
                proposal_id,
                {"checks": json.dumps(payload, ensure_ascii=False), "status": status, "updated_at": utc_now()},
            )
            out = dict(payload)
            out.update({"proposal_id": proposal_id, "rechecked": True, "status": status})
            return out
        static = checks.run_static_checks(candidate_pkg, baseline_pkg)
        capability = static["capability_change"]
        hard_failed = bool(static["hard_failed"])
        payload = {
            "level_hard": static["level_hard"],
            "level_hint": static["level_hint"],
            "level_risk": static["level_risk"],
            "hard_failed": hard_failed,
            "blocked_reason": checks.blocked_reason(static),
            "rewrite_review_required": static["rewrite_review_required"],
            "needs_manual_confirmation": static["rewrite_review_required"],
            "body_diff": static["body_diff"],
            "checked_at": static["checked_at"],
        }
        status = row["status"]
        if status not in TERMINAL_STATUSES:
            if hard_failed:
                # 硬检查失败一律阻断：不允许继续评测或启用
                status = "blocked"
            elif capability.get("requires_authorization"):
                status = "needs_clarification"
            elif status == "blocked":
                status = "ready"
            # stale 保持 stale：基线已变化，需要重新生成候选
        self._update_proposal(
            proposal_id,
            {
                "checks": json.dumps(payload, ensure_ascii=False),
                "capability_change": json.dumps(capability, ensure_ascii=False),
                "status": status,
                "updated_at": utc_now(),
            },
        )
        self.events.audit(
            "proposal.check",
            "proposal",
            proposal_id,
            after={"hard_failed": hard_failed, "status": status},
        )
        out = dict(payload)
        out.update({"proposal_id": proposal_id, "rechecked": True, "status": status, "capability_change": capability})
        return out

    # ==================================================================== 查询
    def get_proposal(self, proposal_id, *, refresh_stale: bool = True) -> dict:
        row = self.repo.get("proposal", proposal_id)
        if row is None:
            raise InvalidInput(f"候选不存在: {proposal_id}", field_errors={"proposal_id": "not_found"})
        if refresh_stale and row["status"] in NON_TERMINAL_STATUSES:
            self._refresh_staleness(row)
            row = self.repo.get("proposal", proposal_id)
        return self._proposal_dict(row)

    def list_proposals(self, *, skill_id=None, status=None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM proposal"
        clauses: list[str] = []
        params: list = []
        if skill_id:
            clauses.append("skill_id = ?")
            params.append(skill_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        return [self._proposal_dict(row) for row in self.store.query(sql, tuple(params))]

    def get_diff(self, proposal_id) -> list[dict]:
        row = self.repo.get("proposal", proposal_id)
        if row is None:
            raise InvalidInput(f"候选不存在: {proposal_id}", field_errors={"proposal_id": "not_found"})
        return _json(row["diff"], [])

    # ==================================================================== 内部：候选
    def _proposal_dict(self, row) -> dict:
        data = {key: row[key] for key in row.keys()}
        checks_payload = _json(data.get("checks"), {})
        evaluation = _json(data.get("evaluation"), {})
        diff = _json(data.get("diff"), [])
        capability = _json(data.get("capability_change"), {})
        generation_budget = _json(data.get("generation_budget"), {})
        reports_invalidated = bool(
            evaluation.get("report_valid") is False
            or evaluation.get("invalidated")
            or checks_payload.get("invalidated")
        )
        activated = bool(data.get("candidate_version_id")) and self._version_activated(
            data.get("candidate_version_id")
        )
        data.update(
            {
                "proposal_id": data["id"],
                "checks": checks_payload,
                "evaluation": evaluation,
                "diff": diff,
                "capability_change": capability,
                "generation_budget": generation_budget,
                "sources": self._sources(data["id"]),
                "hard_blocked": bool(checks_payload.get("hard_failed")),
                "blocked_reason": checks_payload.get("blocked_reason") or "",
                "needs_manual_confirmation": bool(checks_payload.get("needs_manual_confirmation")),
                "reports_invalidated": reports_invalidated,
                "report_valid": not reports_invalidated,
                "activated": activated,
                "can_evaluate": data.get("status") in ("ready", "needs_evidence", "evaluating"),
                "can_activate": data.get("status") == "eligible",
                "authorization_required": bool(capability.get("requires_authorization")),
            }
        )
        return data

    def _sources(self, proposal_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT ps.lesson_id, l.attribution, l.status, l.scope, l.evidence_strength, l.rule_text, "
            "l.condition_text, l.target_skill_id "
            "FROM proposal_source ps JOIN lesson l ON l.id = ps.lesson_id "
            "WHERE ps.proposal_id = ? ORDER BY l.created_at ASC",
            (proposal_id,),
        )
        return [
            {
                "lesson_id": row["lesson_id"],
                "attribution": row["attribution"],
                "status": row["status"],
                "scope": row["scope"],
                "evidence_strength": row["evidence_strength"],
                "rule_text": row["rule_text"],
                "condition_text": row["condition_text"],
                "target_skill_id": row["target_skill_id"],
            }
            for row in rows
        ]

    def _classify_lessons(self, skill_id, lesson_ids: list) -> tuple[list, list[dict], list[dict], list[dict]]:
        eligible: list = []
        excluded: list[dict] = []
        advice: list[dict] = []
        dependency_hints: list[dict] = []
        seen: set[str] = set()
        for lesson_id in lesson_ids:
            if lesson_id in seen:
                continue
            seen.add(lesson_id)
            row = self.repo.get("lesson", lesson_id)
            if row is None:
                raise InvalidInput(f"经验不存在: {lesson_id}", field_errors={"lesson_id": "not_found"})
            if row["target_skill_id"] and row["target_skill_id"] != skill_id:
                dependency_hints.append(
                    {
                        "lesson_id": lesson_id,
                        "target_skill_id": row["target_skill_id"],
                        "message": "该经验指向另一个 skill，需为它单独生成候选（不跨 skill 原子发布）",
                    }
                )
                continue
            if row["status"] in ("ignored", "rejected"):
                excluded.append({"lesson_id": lesson_id, "reason": f"lesson_{row['status']}"})
                continue
            if row["status"] == "stale":
                excluded.append({"lesson_id": lesson_id, "reason": "source_withdrawn"})
                continue
            if row["target_skill_id"] is None:
                excluded.append({"lesson_id": lesson_id, "reason": "needs_attribution"})
                advice.append(
                    {
                        "lesson_id": lesson_id,
                        "attribution": row["attribution"],
                        "advice": {
                            "category": "attribution",
                            "title": "目标未归因",
                            "actions": ["先确认目标 skill，再生成候选"],
                            "rewrites_skill": False,
                        },
                    }
                )
                continue
            if row["attribution"] not in CANDIDATE_ATTRIBUTIONS:
                excluded.append({"lesson_id": lesson_id, "reason": f"attribution:{row['attribution']}"})
                advice.append(
                    {
                        "lesson_id": lesson_id,
                        "attribution": row["attribution"],
                        "advice": attribution_advice(row["attribution"]),
                    }
                )
                continue
            eligible.append(row)
        return eligible, excluded, advice, dependency_hints

    def _resolve_baseline(self, skill_id, baseline_version_id) -> str:
        activation = self._current_activation(skill_id)
        effective = activation["version_id"] if activation else None
        if baseline_version_id is None:
            if not effective:
                raise InvalidInput(
                    "缺少基线版本：技能没有生效指针，请显式给出 baseline_version_id",
                    field_errors={"baseline_version_id": "required"},
                )
            baseline_version_id = effective
        row = self.repo.get("skill_version", baseline_version_id)
        if row is None:
            raise InvalidInput(
                f"基线版本不存在: {baseline_version_id}", field_errors={"baseline_version_id": "not_found"}
            )
        if row["skill_id"] != skill_id:
            raise InvalidInput(
                "基线版本不属于该技能", field_errors={"baseline_version_id": "skill_mismatch"}
            )
        if effective and baseline_version_id != effective:
            raise BaselineConflict(
                "基线不是候选生成时的实际生效完整包",
                current_revision=int(activation["revision"]),
                field_errors={"baseline_version_id": "not_effective"},
            )
        return baseline_version_id

    def _current_activation(self, skill_id):
        return self.store.query_one(
            "SELECT * FROM activation WHERE skill_id = ? ORDER BY revision DESC LIMIT 1", (skill_id,)
        )

    def _version_activated(self, version_id) -> bool:
        return (
            self.store.query_one("SELECT id FROM activation WHERE version_id = ? LIMIT 1", (version_id,))
            is not None
        )

    def _package_from_version(self, version_id, *, expected_hash: str | None = None):
        version_row = self.repo.get("skill_version", version_id)
        if version_row is None:
            raise InvalidInput(f"版本不存在: {version_id}", field_errors={"version_id": "not_found"})
        package = None
        materialize = getattr(self.skills, "materialize_version", None) if self.skills is not None else None
        if callable(materialize):
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(materialize(version_id, Path(temp_dir)))
                package = checks.read_package(root)
        if package is None:
            if self.blobs is None:
                raise MissingDependency("缺少制品库，无法重建完整包")
            files = checks.files_from_version(self.store, self.blobs, version_id)
            package = checks.build_package(files)
        expected = expected_hash if expected_hash is not None else version_row["package_hash"]
        if expected and package.package_hash != expected:
            raise StorageError(
                f"版本 {version_id} 的完整包 hash 与记录不一致（期望 {expected[:12]}…）",
                field_errors={"package_hash": "mismatch"},
            )
        return package

    def _create_candidate_version(
        self,
        *,
        skill_id,
        candidate_pkg,
        parent_version_id,
        proposal_id,
        note,
        validation_status: str = "passed",
        conn=None,
    ) -> str:
        if self.skills is None:
            raise MissingDependency("skills 层未就绪，无法创建候选版本")
        return self.skills.create_version_from_package(
            skill_id,
            candidate_pkg,
            origin="proposal",
            parent_version_id=parent_version_id,
            validation_status=validation_status,
            note=note,
            proposal_id=proposal_id,
            conn=conn,
        )

    def _apply_edits(self, baseline_pkg, reply: Mapping) -> dict[str, bytes]:
        files = checks.package_files(baseline_pkg)
        edits = reply.get("edits") if isinstance(reply, Mapping) else None
        if not isinstance(edits, Mapping) or not edits:
            raise InvalidInput(
                "模型未返回可用的文件内容（edits 为空）", field_errors={"edits": "empty"}
            )
        for raw_path, content in edits.items():
            path = _safe_relpath(raw_path)
            if not isinstance(content, (str, bytes)):
                raise InvalidInput(
                    f"edits[{raw_path}] 必须是完整的文件内容文本", field_errors={"edits": "invalid"}
                )
            files[path] = content.encode("utf-8") if isinstance(content, str) else content
        removed = reply.get("removed_files") if isinstance(reply, Mapping) else None
        for raw_path in removed or []:
            files.pop(_safe_relpath(raw_path), None)
        return files

    # ==================================================================== 内部：材料 / 模型
    def _generation_materials(self, skill, version_row, baseline_pkg, lessons: list) -> tuple[dict, list[dict]]:
        """只读被授权来源：基线包、经验与来源反馈、（非隐藏的）回归样例。

        ``role='holdout'`` 的样例、``expected_answers`` / rubric 等预期答案字段在这里被显式过滤
        生成模型无法访问。
        """
        files = {}
        for path, data in checks.package_files(baseline_pkg).items():
            text = data.decode("utf-8", errors="replace")
            files[path] = text if len(text) <= MAX_MATERIAL_FILE_CHARS else text[:MAX_MATERIAL_FILE_CHARS] + "…"
        activation = self._current_activation(skill["id"])
        materials = {
            "skill": {
                "skill_id": skill["id"],
                "slug": skill["slug"],
                "name": skill["name"],
                "description": skill["description"],
                "scope": skill["scope"],
            },
            "baseline": {
                "version_id": version_row["id"],
                "package_hash": version_row["package_hash"],
                "origins": version_row["origin"],
                "activation_revision": int(activation["revision"]) if activation else None,
                "files": files,
            },
            "lessons": [
                {
                    "lesson_id": row["id"],
                    "attribution": row["attribution"],
                    "scope": row["scope"],
                    "applicability": row["applicability"],
                    "condition": row["condition_text"],
                    "rule": row["rule_text"],
                    "rationale": row["rationale_text"],
                    "verification": row["verification_text"],
                    "boundary": row["boundary_text"],
                    "evidence_strength": row["evidence_strength"],
                    "sources": self._lesson_sources(row["id"]),
                }
                for row in lessons
            ],
            "public_regression_examples": self._public_eval_cases(skill["id"]),
            "constraints": [
                "只允许读取本材料；不得引用或猜测隐藏样例与预期答案",
                "不得改写评测评分标准（rubric / critical checks / thresholds）",
                "新增脚本、命令或网络需求必须在 edits 中如实体现，权限由用户另行授权",
            ],
        }
        return checks.sanitize_materials(materials)

    def _lesson_sources(self, lesson_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT ls.feedback_id, f.source_kind, f.type, f.verdict, f.content, f.locator "
            "FROM lesson_source ls JOIN feedback f ON f.id = ls.feedback_id "
            "WHERE ls.lesson_id = ? ORDER BY f.created_at ASC",
            (lesson_id,),
        )
        return [
            {
                "feedback_id": row["feedback_id"],
                "source_kind": row["source_kind"],
                "type": row["type"],
                "verdict": row["verdict"],
                "content": _clip(row["content"], 1_000),
                "locator": _json(row["locator"], {}),
            }
            for row in rows
        ]

    def _public_eval_cases(self, skill_id: str) -> list[dict]:
        """只取 role != 'holdout' 的回归样例，并且不带 expected_answers。"""
        rows = self.store.query(
            "SELECT c.id, c.name, c.prompt, c.input_files, c.role FROM eval_case c "
            "JOIN eval_suite s ON s.id = c.suite_id "
            "WHERE s.skill_id = ? AND c.role != 'holdout' ORDER BY c.created_at DESC LIMIT 5",
            (skill_id,),
        )
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "role": row["role"],
                "prompt": _clip(row["prompt"], 1_000),
                "input_files": _json(row["input_files"], []),
            }
            for row in rows
        ]

    def _generate(self, scope_id: str, materials: dict) -> tuple[dict | None, dict]:
        client = self.model_factory() if callable(self.model_factory) else self.model_factory
        if client is None:
            return None, {"reason": "model_not_configured", "message": "模型未配置"}
        prompts = self._generation_messages(materials)
        last_error = ""
        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            if not self.budget.reserve_call(scope_id, 3_000):
                return None, {"reason": "budget_exceeded", "message": "学习预算不足以生成候选"}
            try:
                reply = client.complete(prompts, max_output_tokens=2_048)
            except Exception as exc:  # 模型失败：保留已有证据
                self.budget.release_call(scope_id, 3_000)
                return None, {"reason": "model_error", "message": str(exc)}
            usage = reply.usage or {}
            tokens = int(usage.get("total_tokens") or 0)
            self.budget.consume(scope_id, tokens=tokens, calls=1, usage_known=bool(reply.usage_known))
            parsed = checks.extract_json_object(reply.text or "")
            if isinstance(parsed, Mapping) and parsed.get("edits"):
                return parsed, {"ok": True, "tokens": tokens, "attempt": attempt}
            last_error = "模型回复不是可用的 JSON（缺少 edits）"
            if attempt < MAX_GENERATION_ATTEMPTS:
                from ..platform.model import ChatMessage

                prompts = prompts + [
                    ChatMessage("assistant", _clip(reply.text or "", 1_000)),
                    ChatMessage(
                        "user",
                        "回复必须是 JSON 对象，且包含 edits（{相对路径: 完整文件内容}）。请重新只返回 JSON。",
                    ),
                ]
        return None, {"reason": "invalid_reply", "message": last_error}

    def _generation_messages(self, materials: dict) -> list:
        from ..platform.model import ChatMessage

        system = (
            "你是技能改进助手，只允许输出 JSON。你只能读取给定材料与基线包，"
            "禁止引用隐藏样例或预期答案，禁止改写评测评分标准，"
            "不得通过正文声明扩大权限（新增脚本/命令/网络需求需用户另行授权）。"
        )
        user = (
            "根据下面的经验与基线包，生成**最小改动**的新完整包。\n"
            + json.dumps(materials, ensure_ascii=False)
            + '\n\n只返回 JSON：{"summary":"改了什么","changed_steps":["步骤"],'
            '"expected_benefit":"预期收益","potential_regression":"潜在退化",'
            '"edits":{"SKILL.md":"修改后的完整文件内容"}, "removed_files":["要删除的相对路径"]}。'
            "edits 中的值必须是完整文件内容（不是 diff）；不改动的文件不要出现在 edits 中。"
        )
        return [ChatMessage("system", system), ChatMessage("user", user)]

    # ==================================================================== 内部：staleness
    def _refresh_staleness(self, row) -> list[str]:
        reasons: list[str] = []
        if row["candidate_version_id"] and self._version_activated(row["candidate_version_id"]):
            return reasons
        generation = _json(row["generation_budget"], {})
        baseline_meta = generation.get("baseline") or {}
        activation = self._current_activation(row["skill_id"])
        stored_revision = baseline_meta.get("activation_revision")
        current_revision = int(activation["revision"]) if activation else None
        if activation is None:
            reasons.append("activation_removed:技能当前没有生效版本")
        elif stored_revision != current_revision:
            reasons.append(
                "activation_changed:生效 revision 由 {} 变为 {}".format(
                    stored_revision if stored_revision is not None else "无", current_revision
                )
            )
        elif activation["version_id"] != row["baseline_version_id"]:
            reasons.append("baseline_changed:基线不再是当前生效版本")
        if row["baseline_version_id"] and not self._version_files_available(row["baseline_version_id"]):
            reasons.append("baseline_package_missing:基线包文件缺失，需重建候选")
        stored_fingerprint = generation.get("model_fingerprint")
        current_fingerprint = self._current_model_fingerprint()
        if stored_fingerprint and current_fingerprint and stored_fingerprint != current_fingerprint:
            reasons.append("model_config_changed:模型配置已变化")
        if reasons and row["status"] in NON_TERMINAL_STATUSES:
            self.mark_stale(row["id"], reason="; ".join(reasons))
        return reasons

    def _version_files_available(self, version_id) -> bool:
        rows = self.store.query(
            "SELECT blob_id FROM skill_version_file WHERE version_id = ?", (version_id,)
        )
        if not rows:
            return False
        if self.blobs is None:
            return True
        return all(self.blobs.exists(row["blob_id"]) for row in rows)

    def _current_model_fingerprint(self) -> str:
        client = self.model_factory() if callable(self.model_factory) else self.model_factory
        if client is None:
            return ""
        try:
            return client.fingerprint()
        except Exception:  # pragma: no cover - 未配置模型时无法取指纹
            return ""

    def _update_proposal(self, proposal_id: str, updates: Mapping) -> None:
        self.repo.update("proposal", proposal_id, dict(updates))


# --------------------------------------------------------------------------- 辅助
def _safe_relpath(raw) -> str:
    text = str(raw).replace("\\", "/")
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ".." in pure.parts:
        raise InvalidInput(f"非法文件路径: {raw}", field_errors={"path": "invalid"})
    normalized = "/".join(part for part in pure.parts if part not in ("", "."))
    if not normalized:
        raise InvalidInput(f"非法文件路径: {raw}", field_errors={"path": "empty"})
    return normalized


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _clip(text, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
