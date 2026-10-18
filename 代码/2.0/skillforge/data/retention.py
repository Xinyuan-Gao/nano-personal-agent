from __future__ import annotations

from pathlib import Path

from ..core.clock import utc_now
from ..core.config import SETTING_RETENTION_DAYS, read_json_setting, write_json_setting
from ..core.errors import InvalidInput

EXPIRED_MARK = "[正文已按保留策略清除]"

#: 每个 run 的正文类列
RUN_BODY_COLUMNS = ("request", "output_text", "input_snapshot", "checkpoint")
#: 工具动作的正文类列
ACTION_BODY_COLUMNS = ("result",)
FEEDBACK_BODY_COLUMNS = ("content",)


class RetentionService:
    def __init__(self, store, repo, events=None, idem=None, config=None) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.idem = idem
        self.config = config

    # ------------------------------------------------------------- 策略
    def policy(self) -> dict:
        days = read_json_setting(self.store, SETTING_RETENTION_DAYS, None)
        return {
            "retention_days": days,
            "mode": "auto_expire" if days else "keep_forever",
            "description": (
                f"正文保留 {days} 天，到期后标记证据不可用（结构化证据与包保留）"
                if days
                else "默认本地长期保留正文，不自动清理"
            ),
            "expired_runs": self.store.scalar(
                "SELECT COUNT(*) FROM run WHERE body_state = 'expired'", default=0
            ),
            "expired_feedback": self.store.scalar(
                "SELECT COUNT(*) FROM feedback WHERE body_state = 'expired'", default=0
            ),
        }

    def set_policy(self, days: int | None, *, actor: str = "owner") -> dict:
        if days is not None:
            if not isinstance(days, int) or days < 1 or days > 3650:
                raise InvalidInput("retention_days 必须是 1～3650 的整数",
                                   field_errors={"retention_days": "out_of_range"})
        write_json_setting(self.store, SETTING_RETENTION_DAYS, days)
        if self.events is not None:
            self.events.audit("retention.policy_set", "setting", SETTING_RETENTION_DAYS,
                              actor=actor, after={"retention_days": days},
                              reason="设置正文保留期限")
        return self.policy()

    # ------------------------------------------------------------- 影响预览
    def _cutoff(self, days: int | None) -> tuple[int, str | None]:
        days = days if days is not None else read_json_setting(self.store, SETTING_RETENTION_DAYS, None)
        if not days:
            return 0, None
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        return int(days), cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def preview(self, days: int | None = None) -> dict:
        """展示到期会清除什么，以及对历史重跑的影响。"""
        days, cutoff = self._cutoff(days)
        if not cutoff:
            return {
                "retention_days": None,
                "cutoff": None,
                "affected_runs": [],
                "affected_count": 0,
                "replay_impact": "未设置保留期限：正文长期保留，历史可重现。",
                "artifacts_kept": True,
                "packages_kept": True,
            }
        runs = self.store.query(
            "SELECT id, request, created_at, body_state FROM run "
            "WHERE created_at < ? AND body_state = 'available' ORDER BY created_at ASC LIMIT 500",
            (cutoff,),
        )
        affected = [
            {
                "run_id": row["id"],
                "created_at": row["created_at"],
                "request_preview": (row["request"] or "")[:60],
                "artifacts": self.store.scalar(
                    "SELECT COUNT(*) FROM artifact WHERE run_id = ?", (row["id"],), default=0
                ),
                "evaluated": bool(
                    self.store.scalar("SELECT COUNT(*) FROM eval_result WHERE baseline_run_id = ? OR candidate_run_id = ?",
                                      (row["id"], row["id"]), default=0)
                ),
            }
            for row in runs
        ]
        evaluated = [r for r in affected if r["evaluated"]]
        return {
            "retention_days": days,
            "cutoff": cutoff,
            "affected_count": len(affected),
            "affected_runs": affected[:50],
            "evaluated_affected": len(evaluated),
            "replay_impact": (
                f"{len(affected)} 个任务的历史正文将不可用，其中 {len(evaluated)} 个参与过评测："
                "对应评测结论仍可查，但无法用原输入重现；需要重现时必须提供新的输入快照并新建任务。"
                if affected
                else "没有超过保留期限的任务正文。"
            ),
            "artifacts_kept": True,
            "packages_kept": True,
            "note": "到期只清除正文；产物制品与技能包版本保留，需要清理制品请显式指定。",
        }

    # ------------------------------------------------------------- 执行
    def apply(self, days: int | None = None, *, confirm: bool = False, purge_artifacts: bool = False,
              idempotency_key: str | None = None, actor: str = "owner") -> dict:
        if not confirm:
            raise InvalidInput("清除正文需要显式确认（confirm=True）", field_errors={"confirm": "required"})

        def _run() -> dict:
            days_value, cutoff = self._cutoff(days)
            if not cutoff:
                raise InvalidInput("未设置保留期限，无需执行到期清理",
                                   field_errors={"retention_days": "not_set"})
            now = utc_now()
            with self.store.tx() as conn:
                run_ids = [
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM run WHERE created_at < ? AND body_state = 'available'",
                        (cutoff,),
                    )
                ]
                for run_id in run_ids:
                    sets = ", ".join(f"{col} = ?" for col in RUN_BODY_COLUMNS)
                    conn.execute(
                        f"UPDATE run SET {sets}, body_state = 'expired', body_expired_at = ?, updated_at = ? "
                        "WHERE id = ?",
                        (EXPIRED_MARK, "", "{}", "{}", now, now, run_id),
                    )
                    action_sets = ", ".join(f"{col} = ?" for col in ACTION_BODY_COLUMNS)
                    conn.execute(
                        f"UPDATE action_record SET {action_sets} WHERE run_id = ?",
                        (EXPIRED_MARK, run_id),
                    )
                feedback_rows = conn.execute(
                    "SELECT id FROM feedback WHERE created_at < ? AND body_state = 'available'",
                    (cutoff,),
                ).fetchall()
                for row in feedback_rows:
                    feedback_sets = ", ".join(f"{col} = ?" for col in FEEDBACK_BODY_COLUMNS)
                    conn.execute(
                        f"UPDATE feedback SET {feedback_sets}, body_state = 'expired', body_expired_at = ?, "
                        "updated_at = ? WHERE id = ?",
                        (EXPIRED_MARK, now, now, row["id"]),
                    )
                audit_id = None
                if self.events is not None:
                    audit_id = self.events.audit(
                        "retention.applied", "setting", SETTING_RETENTION_DAYS, actor=actor,
                        after={"runs": len(run_ids), "feedback": len(feedback_rows),
                               "retention_days": days_value, "cutoff": cutoff},
                        reason="正文到期清理", conn=conn,
                    )
            purged = {"artifacts": 0, "blobs": 0}
            if purge_artifacts:
                purged = self._purge_artifacts(run_ids)
            return {
                "retention_days": days_value,
                "cutoff": cutoff,
                "expired_runs": len(run_ids),
                "expired_feedback": len(feedback_rows),
                "audit_id": audit_id,
                "purged": purged,
                "artifacts_kept": not purge_artifacts,
                "packages_kept": True,
                "note": "正文已清除，结构化证据（ID/时间/状态/hash/加载记录/审计）保留。",
            }

        if self.idem is not None and idempotency_key:
            return self.idem.run("retention.apply", idempotency_key,
                                 {"days": days, "purge_artifacts": purge_artifacts}, _run)
        return _run()

    def _purge_artifacts(self, run_ids: list[str]) -> dict:
        """显式清理产物制品（默认不执行）。只删除没有其他引用的 blob。"""
        artifacts = 0
        blobs_removed = 0
        for run_id in run_ids:
            rows = self.store.query("SELECT id, blob_id FROM artifact WHERE run_id = ?", (run_id,))
            for row in rows:
                references = self.store.scalar(
                    "SELECT COUNT(*) FROM artifact WHERE blob_id = ?", (row["blob_id"],), default=0
                )
                self.store.execute("DELETE FROM artifact WHERE id = ?", (row["id"],))
                artifacts += 1
                if references <= 1 and self.config is not None:
                    path = Path(self.config.blob_dir) / row["blob_id"][:2] / row["blob_id"]
                    try:
                        path.unlink(missing_ok=True)
                        blobs_removed += 1
                    except OSError:  # pragma: no cover
                        pass
        return {"artifacts": artifacts, "blobs": blobs_removed}

    # ------------------------------------------------------------- 证据状态
    def evidence_state(self, run_id: str) -> dict:
        row = self.repo.get("run", run_id)
        if row is None:
            raise InvalidInput(f"任务不存在: {run_id}", field_errors={"run_id": "not_found"})
        expired = (row["body_state"] or "available") == "expired"
        return {
            "run_id": run_id,
            "available": not expired,
            "state": row["body_state"] or "available",
            "expired_at": row["body_expired_at"],
            "context_hash": row["context_hash"],
            "reason": (
                "正文已按保留策略清除，无法用原输入重现该任务；结构化证据与 hash 仍可用于核对。"
                if expired
                else "结构化证据与正文均可用。"
            ),
        }

    def mark_expired(self, run_id: str, *, reason: str | None = None, conn=None) -> dict:
        """按单个任务标记正文到期（供数据清除流程复用）。"""
        now = utc_now()
        sets = ", ".join(f"{col} = ?" for col in RUN_BODY_COLUMNS)
        params = tuple([EXPIRED_MARK] * len(RUN_BODY_COLUMNS)) + (now, now, run_id)
        sql = f"UPDATE run SET {sets}, body_state = 'expired', body_expired_at = ?, updated_at = ? WHERE id = ?"
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.store.execute(sql, params)
        return {"run_id": run_id, "body_state": "expired", "expired_at": now, "reason": reason}
