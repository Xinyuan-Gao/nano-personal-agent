from __future__ import annotations

import json

from ..core.clock import utc_now
from ..core.errors import (
    BaselineConflict,
    EvidenceUnavailable,
    InvalidInput,
    InvalidState,
    MissingDependency,
    StorageError,
)
from ..core.ids import canonical_hash, new_id
from .package import hard_checks

_ORIGINS = ("import", "draft", "proposal", "manual_trial", "rollback", "restore")


class ActivationService:
    def __init__(self, store, repo, events, idem, skills) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.idem = idem
        self.skills = skills

    # ------------------------------------------------------------------ 当前指针
    def current(self, skill_id) -> dict | None:
        """当前生效指针：``revision`` 最大者。"""
        if self.repo.get("skill", skill_id) is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        return self.skills.current_activation(skill_id)

    def history(self, skill_id) -> list[dict]:
        """启用 / 回退历史（revision 升序），最新一条 ``is_current=True``。"""
        if self.repo.get("skill", skill_id) is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        rows = self.store.query(
            "SELECT * FROM activation WHERE skill_id = ? ORDER BY revision ASC", (skill_id,)
        )
        current = self.skills.current_activation(skill_id)
        out: list[dict] = []
        for row in rows:
            item = self.skills._activation_dict(row)
            item["is_current"] = bool(current and current["activation_id"] == row["id"])
            out.append(item)
        return out

    # ------------------------------------------------------------------ 启用候选
    def activate_proposal(
        self,
        *,
        proposal_id,
        report_id,
        scope=None,
        expected_revision,
        idempotency_key=None,
        actor: str = "owner",
    ) -> dict:
        payload = {
            "proposal_id": proposal_id,
            "report_id": report_id,
            "scope": scope,
            "expected_revision": expected_revision,
        }
        return self.idem.run(
            "activation.activate_proposal",
            idempotency_key,
            payload,
            lambda: self._activate_proposal(
                proposal_id, report_id, scope, expected_revision, actor
            ),
        )

    def _activate_proposal(self, proposal_id, report_id, scope, expected_revision, actor) -> dict:
        proposal = self.repo.get("proposal", proposal_id)
        if proposal is None:
            raise InvalidInput(f"候选不存在: {proposal_id}", field_errors={"proposal_id": "not_found"})
        skill_id = proposal["skill_id"]
        skill = self.repo.get("skill", skill_id)
        if skill is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})

        # --- 只接受 eligible --------------------------------
        if proposal["status"] != "eligible":
            raise InvalidState(
                f"候选状态为 {proposal['status']}，只有 eligible 可以启用",
                field_errors={"status": "not_eligible"},
            )
        candidate_version_id = proposal["candidate_version_id"]
        if not candidate_version_id:
            raise InvalidState("候选缺少 candidate_version_id", field_errors={"candidate_version_id": "missing"})
        version = self.repo.get("skill_version", candidate_version_id)
        if version is None:
            raise InvalidInput(
                f"候选版本不存在: {candidate_version_id}", field_errors={"version_id": "not_found"}
            )

        # --- 报告存在且与当前候选包 / config 匹配 ------------
        job = self.repo.get("eval_job", report_id)
        if job is None:
            # 契约要求：报告存在是启用前提，否则 InvalidState
            # 报告存在但制品缺失时用 EvidenceUnavailable。
            raise InvalidState(
                f"启用依据的报告不存在或已被清除: {report_id}",
                field_errors={"report_id": "unavailable"},
            )
        if job["proposal_id"] != proposal_id:
            raise InvalidInput(
                f"报告 {report_id} 不属于候选 {proposal_id}", field_errors={"report_id": "mismatch"}
            )
        if job["candidate_version_id"] != candidate_version_id:
            raise InvalidState(
                "报告对应的候选版本已变化，旧报告不能作为启用凭据",
                field_errors={"candidate_version_id": "mismatch"},
            )
        if (job["verdict"] or "") != "passed":
            raise InvalidState(
                f"报告结论为 {job['verdict'] or '未判定'}，不能启用",
                field_errors={"verdict": "not_passed"},
            )
        if (job["status"] or "") != "completed":
            raise InvalidState(
                f"评测作业状态为 {job['status']}，报告不完整不能启用",
                field_errors={"report_status": "incomplete"},
            )
        config = _json(job["config"], {})
        frozen_hash = config.get("candidate_package_hash") or config.get("package_hash")
        if frozen_hash and frozen_hash != version["package_hash"]:
            raise InvalidState(
                "报告冻结的候选包 hash 与当前候选包不匹配",
                field_errors={"package_hash": "mismatch"},
            )
        frozen_config = config.get("frozen") or config.get("payload")
        if isinstance(frozen_config, dict) and job["config_hash"]:
            if canonical_hash(frozen_config) != job["config_hash"]:
                raise InvalidState(
                    "报告冻结的配置 hash 与记录不一致，报告不可信",
                    field_errors={"config_hash": "mismatch"},
                )
        if config.get("config_hash") and job["config_hash"] and config["config_hash"] != job["config_hash"]:
            raise InvalidState(
                "报告配置 hash 不一致", field_errors={"config_hash": "mismatch"}
            )

        return self._commit_activation(
            skill_id=skill_id,
            version_id=candidate_version_id,
            origin="proposal",
            scope=scope or proposal["scope"] or skill["scope"],
            expected_revision=expected_revision,
            proposal_id=proposal_id,
            report_id=report_id,
            unverified=0,
            reason="评测通过的候选启用",
            actor=actor,
            extra_updates=[("proposal", proposal_id, {"status": "activated"})],
            except_proposal_ids=[proposal_id],
        )

    # ------------------------------------------------------------------ 手工试用
    def activate_manual_trial(
        self,
        *,
        source_version_id,
        unverified_ack: bool,
        scope=None,
        expected_revision,
        idempotency_key=None,
        actor: str = "owner",
    ) -> dict:
        """手工试用：必须显式确认“效果未验证”。

        创建 ``origin=manual_trial`` 的**新派生版本** + 独立 activation
        源候选 verdict 不变，其余候选标 stale；硬阻断不可绕过。
        """
        if not unverified_ack:
            raise InvalidInput(
                "手工试用必须显式确认“效果未验证”（unverified_ack=true）",
                field_errors={"unverified_ack": "required"},
            )
        payload = {
            "source_version_id": source_version_id,
            "unverified_ack": bool(unverified_ack),
            "scope": scope,
            "expected_revision": expected_revision,
        }
        return self.idem.run(
            "activation.manual_trial",
            idempotency_key,
            payload,
            lambda: self._activate_manual_trial(source_version_id, scope, expected_revision, actor),
        )

    def _activate_manual_trial(self, source_version_id, scope, expected_revision, actor) -> dict:
        source = self.repo.get("skill_version", source_version_id)
        if source is None:
            raise InvalidInput(
                f"源版本不存在: {source_version_id}", field_errors={"version_id": "not_found"}
            )
        skill_id = source["skill_id"]
        skill = self.repo.get("skill", skill_id)

        integrity = self.skills.verify_version_integrity(source_version_id)
        if integrity["missing"]:
            raise EvidenceUnavailable(
                f"源版本包不完整，不能手工试用: 缺失 {integrity['missing'][:5]}",
                field_errors={"blob": "missing"},
            )
        if integrity["mismatched"]:
            raise StorageError(
                f"源版本包 hash 不匹配: {integrity['mismatched'][:5]}", field_errors={"blob": "mismatch"}
            )

        package = self.skills.version_package(source_version_id)
        blocked = [f for f in hard_checks(package) if f.level == "hard"]
        if blocked:
            raise InvalidState(
                "硬检查未通过，人工试用不能绕过路径 / 权限等硬阻断",
                field_errors={f.code: f.message for f in blocked},
            )

        source_proposal_ids = [
            row["id"]
            for row in self.store.query(
                "SELECT id FROM proposal WHERE candidate_version_id = ?", (source_version_id,)
            )
        ]
        activation_id = new_id("act")
        now = utc_now()
        current = self.skills.current_activation(skill_id)
        resolved_scope = (
            scope or (current["scope"] if current else None) or (skill["scope"] if skill else "personal")
        )

        with self.store.tx() as conn:
            # 事务内重新读取生效 revision，保证并发提交返回明确冲突
            current = self.skills.current_activation(skill_id)
            current_revision = current["revision"] if current else 0
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise BaselineConflict(
                    f"生效 revision 已变化：期望 {expected_revision}，当前 {current_revision}",
                    current_revision=current_revision,
                )
            revision = current_revision + 1
            derived_version_id = self.skills.create_version_from_package(
                skill_id,
                package,
                origin="manual_trial",
                parent_version_id=source_version_id,
                validation_status=source["validation_status"] if source["validation_status"] != "passed" else "unchecked",
                note="手工试用：效果未验证",
                conn=conn,
                created_by=actor,
            )
            self._insert_activation(
                conn,
                {
                    "id": activation_id,
                    "skill_id": skill_id,
                    "version_id": derived_version_id,
                    "scope": resolved_scope,
                    "revision": revision,
                    "origin": "manual_trial",
                    "proposal_id": None,
                    "report_id": None,
                    "unverified": 1,
                    "reason": f"手工试用（效果未验证），派生自 {source_version_id}",
                    "actor": actor,
                    "created_at": now,
                },
            )
            self.events.audit(
                "activation.manual_trial",
                "skill",
                skill_id,
                actor=actor,
                before={"revision": current_revision, "version_id": current["version_id"] if current else None},
                after={
                    "activation_id": activation_id,
                    "version_id": derived_version_id,
                    "revision": revision,
                    "unverified": True,
                    "source_version_id": source_version_id,
                    "scope": resolved_scope,
                },
                reason="手工试用：效果未验证，不计有效改进，源候选结论不变",
                conn=conn,
            )
            stale = self.skills.mark_stale_proposals(
                skill_id,
                reason=f"手工试用版本生效（revision {revision}）",
                conn=conn,
                except_proposal_ids=source_proposal_ids,
            )
            self.repo.update("skill", skill_id, {"updated_at": now}, conn=conn)

        return {
            "activation_id": activation_id,
            "skill_id": skill_id,
            "version_id": derived_version_id,
            "source_version_id": source_version_id,
            "revision": revision,
            "origin": "manual_trial",
            "scope": resolved_scope,
            "unverified": True,
            "counterpart_revision": current_revision,
            "stale_proposals": stale,
            "candidate_verdict_unchanged": True,
        }

    # ------------------------------------------------------------------ 回退
    def rollback(
        self,
        *,
        skill_id,
        target_version_id,
        expected_revision,
        idempotency_key=None,
        actor: str = "owner",
    ) -> dict:
        """显式回退到历史完整版本：校验完整性、不删候选与评测。"""
        payload = {
            "skill_id": skill_id,
            "target_version_id": target_version_id,
            "expected_revision": expected_revision,
        }
        return self.idem.run(
            "activation.rollback",
            idempotency_key,
            payload,
            lambda: self._rollback(skill_id, target_version_id, expected_revision, actor),
        )

    def _rollback(self, skill_id, target_version_id, expected_revision, actor) -> dict:
        skill = self.repo.get("skill", skill_id)
        if skill is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        target = self.repo.get("skill_version", target_version_id)
        if target is None:
            raise InvalidInput(
                f"目标版本不存在: {target_version_id}", field_errors={"version_id": "not_found"}
            )
        if target["skill_id"] != skill_id:
            raise InvalidInput(
                f"目标版本 {target_version_id} 不属于技能 {skill_id}",
                field_errors={"version_id": "wrong_skill"},
            )

        integrity = self.skills.verify_version_integrity(target_version_id)
        if integrity["missing"]:
            raise EvidenceUnavailable(
                f"目标版本制品缺失，无法回退: {integrity['missing'][:5]}",
                field_errors={"blob": "missing"},
            )
        if integrity["mismatched"]:
            raise StorageError(
                f"目标版本制品 hash 不匹配，拒绝回退: {integrity['mismatched'][:5]}",
                field_errors={"blob": "mismatch"},
            )

        container_available = (getattr(self.skills.config, "extra", None) or {}).get("container_available")
        if container_available is False and self.skills.has_scripts(target_version_id):
            raise MissingDependency(
                "容器能力不可用，不能回退到含脚本版本",
                field_errors={"container": "unavailable"},
            )

        # 先落完整包（校验通过后）再同事务写指针与审计。
        staging = self.skills.stage_version(target_version_id)
        activation_id = new_id("act")
        now = utc_now()
        unverified = 0 if target["validation_status"] == "passed" else 1

        with self.store.tx() as conn:
            current = self.skills.current_activation(skill_id)
            current_revision = current["revision"] if current else 0
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise BaselineConflict(
                    f"生效 revision 已变化：期望 {expected_revision}，当前 {current_revision}",
                    current_revision=current_revision,
                )
            revision = current_revision + 1
            self._insert_activation(
                conn,
                {
                    "id": activation_id,
                    "skill_id": skill_id,
                    "version_id": target_version_id,
                    "scope": current["scope"] if current else skill["scope"],
                    "revision": revision,
                    "origin": "rollback",
                    "proposal_id": None,
                    "report_id": None,
                    "unverified": unverified,
                    "reason": f"显式回退到历史版本 {target['version_label']}（{target_version_id}）",
                    "actor": actor,
                    "created_at": now,
                },
            )
            self.events.audit(
                "activation.rollback",
                "skill",
                skill_id,
                actor=actor,
                before={"revision": current_revision, "version_id": current["version_id"] if current else None},
                after={
                    "activation_id": activation_id,
                    "version_id": target_version_id,
                    "revision": revision,
                    "unverified": bool(unverified),
                },
                reason="回退保留候选与评测记录",
                conn=conn,
            )
            stale = self.skills.mark_stale_proposals(
                skill_id, reason=f"回退到历史版本（revision {revision}）", conn=conn
            )
            self.repo.update("skill", skill_id, {"updated_at": now}, conn=conn)

        return {
            "activation_id": activation_id,
            "skill_id": skill_id,
            "version_id": target_version_id,
            "revision": revision,
            "origin": "rollback",
            "scope": current["scope"] if current else skill["scope"],
            "unverified": bool(unverified),
            "previous_version_id": current["version_id"] if current else None,
            "counterpart_revision": current_revision,
            "staging_dir": str(staging),
            "stale_proposals": stale,
            "proposals_kept": self.repo.count("proposal", {"skill_id": skill_id}),
            "eval_jobs_kept": len(
                self.store.query(
                    "SELECT id FROM eval_job WHERE proposal_id IN "
                    "(SELECT id FROM proposal WHERE skill_id = ?)",
                    (skill_id,),
                )
            ),
        }

    # ------------------------------------------------------------------ 内部
    def _insert_activation(self, conn, record: dict) -> None:
        """插入生效指针，并把唯一约束冲突翻译成明确的 revision 冲突。"""
        import sqlite3

        try:
            self.repo.insert("activation", record, conn=conn)
        except sqlite3.IntegrityError as exc:
            raise BaselineConflict(
                f"并发启用冲突：revision {record['revision']} 已被其他操作占用",
                current_revision=int(record["revision"]),
            ) from exc

    def _commit_activation(
        self,
        *,
        skill_id,
        version_id,
        origin,
        scope,
        expected_revision,
        proposal_id,
        report_id,
        unverified,
        reason,
        actor,
        extra_updates=None,
        except_proposal_ids=None,
    ) -> dict:
        """先落完整包 → 同一事务写 activation + audit。"""
        if origin not in _ORIGINS:
            raise InvalidInput(f"不支持的 origin: {origin}", field_errors={"origin": "unsupported"})
        skill = self.repo.get("skill", skill_id)
        if skill is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        if self.repo.get("skill_version", version_id) is None:
            raise InvalidInput(f"版本不存在: {version_id}", field_errors={"version_id": "not_found"})

        integrity = self.skills.verify_version_integrity(version_id)
        if integrity["missing"]:
            raise EvidenceUnavailable(
                f"完整包制品缺失，不能启用: {integrity['missing'][:5]}",
                field_errors={"blob": "missing"},
            )
        if integrity["mismatched"]:
            raise StorageError(
                f"完整包 hash 不匹配，拒绝启用: {integrity['mismatched'][:5]}",
                field_errors={"blob": "mismatch"},
            )

        staging = self.skills.stage_version(version_id)
        activation_id = new_id("act")
        now = utc_now()

        with self.store.tx() as conn:
            # 事务内重新读取生效 revision：并发提交返回明确冲突而不是覆盖
            current = self.skills.current_activation(skill_id)
            current_revision = current["revision"] if current else 0
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise BaselineConflict(
                    f"生效 revision 已变化：期望 {expected_revision}，当前 {current_revision}",
                    current_revision=current_revision,
                )
            revision = current_revision + 1
            self._insert_activation(
                conn,
                {
                    "id": activation_id,
                    "skill_id": skill_id,
                    "version_id": version_id,
                    "scope": scope or skill["scope"],
                    "revision": revision,
                    "origin": origin,
                    "proposal_id": proposal_id,
                    "report_id": report_id,
                    "unverified": 1 if unverified else 0,
                    "reason": reason,
                    "actor": actor,
                    "created_at": now,
                },
            )
            self.events.audit(
                "activation.activate",
                "skill",
                skill_id,
                actor=actor,
                before={"revision": current_revision, "version_id": current["version_id"] if current else None},
                after={
                    "activation_id": activation_id,
                    "version_id": version_id,
                    "revision": revision,
                    "origin": origin,
                    "proposal_id": proposal_id,
                    "report_id": report_id,
                    "unverified": bool(unverified),
                },
                reason=reason,
                conn=conn,
            )
            for table, pk, data in extra_updates or []:
                payload = dict(data)
                payload["updated_at"] = now
                self.repo.update(table, pk, payload, conn=conn)
            stale = self.skills.mark_stale_proposals(
                skill_id,
                reason=f"新版本已生效（revision {revision}）",
                conn=conn,
                except_proposal_ids=except_proposal_ids,
            )
            self.repo.update("skill", skill_id, {"updated_at": now}, conn=conn)

        return {
            "activation_id": activation_id,
            "skill_id": skill_id,
            "version_id": version_id,
            "revision": revision,
            "origin": origin,
            "scope": scope or skill["scope"],
            "proposal_id": proposal_id,
            "report_id": report_id,
            "unverified": bool(unverified),
            "previous_version_id": current["version_id"] if current else None,
            "counterpart_revision": current_revision,
            "staging_dir": str(staging),
            "stale_proposals": stale,
        }


def _json(text, default):
    if text is None:
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
