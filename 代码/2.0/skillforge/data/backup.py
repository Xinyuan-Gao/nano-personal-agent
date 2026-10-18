from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from ..core.clock import utc_now
from ..core.db import SCHEMA_VERSION
from ..core.errors import InvalidInput, StorageError
from ..core.ids import canonical_hash, hash_file, new_id
from ..core.paths import is_ignored_path, scan_secrets

#: 备份 / 导出 manifest 自身版本（与 schema_version 分开）
MANIFEST_VERSION = "1.0"
SUPPORTED_MANIFEST_VERSIONS = ("1.0",)

BACKUP_KIND = "skillforge.backup"
EXPORT_KIND = "skillforge.export"

#: 永不进入备份 / 导出的顶层目录（凭据只存 local secrets）
EXCLUDED_DIR_NAMES = {"secrets", ".ssh", ".aws", ".gnupg"}
EXCLUDED_FILE_NAMES = {".env", ".env.local", "credentials.json", "id_rsa", "id_ed25519"}
EXCLUDED_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")

#: 制品密钥扫描上限（超过则视为二进制，不扫描）
MAX_SECRET_SCAN_BYTES = 1 << 20

#: manifest 里统计的对象表
COUNTED_TABLES = (
    "profile",
    "workspace",
    "model_config",
    "skill",
    "skill_version",
    "skill_version_file",
    "activation",
    "run",
    "run_step",
    "action_record",
    "artifact",
    "skill_load",
    "feedback",
    "lesson",
    "lesson_source",
    "proposal",
    "proposal_source",
    "eval_suite",
    "eval_case",
    "eval_job",
    "eval_result",
    "job",
    "budget_ledger",
    "daily_usage",
    "audit_event",
    "product_event",
    "data_op",
    # P1 四层记忆
    "memory_working",
    "memory_log",
    "memory_topic",
    "memory_topic_source",
    "memory_index_entry",
    "memory_recall",
    "dream_job",
)

#: ``include_body=False`` 时需要清空的正文类字段
BODY_FIELDS: dict[str, tuple[str, ...]] = {
    "run": ("request", "output_text", "input_snapshot", "checkpoint"),
    "run_step": ("title", "intent", "detail"),
    "run_event": ("payload",),
    "action_record": ("arguments", "result"),
    "feedback": ("content", "locator"),
    "lesson": (
        "rule_text",
        "condition_text",
        "rationale_text",
        "verification_text",
        "boundary_text",
        "applicability",
    ),
    "proposal": (
        "reason",
        "summary",
        "expected_benefit",
        "potential_regression",
        "diff",
        "checks",
    ),
    "eval_case": ("prompt", "input_files", "expected_answers", "verifier"),
    "eval_job": ("report", "verdict_detail", "config", "checkpoint"),
    "eval_result": ("notes", "judge", "baseline_checks", "candidate_checks"),
    "skill_version": ("note",),
    "audit_event": ("before_state", "after_state", "reason"),
    # 记忆正文：include_body=False 时一并脱敏（数据发送与删除）
    "memory_log": ("content", "tags"),
    "memory_topic": ("summary", "body", "tags"),
    "memory_working": ("task_summary", "recent_files"),
    "memory_recall": ("reason",),
    "dream_job": ("gate", "scan", "result", "usage"),
}

#: 清除范围覆盖的业务表（``id`` 为主键）
CLEAR_ID_TABLES = (
    "run",
    "feedback",
    "lesson",
    "proposal",
    "eval_suite",
    "eval_case",
    "eval_job",
    "eval_result",
    "activation",
    "skill_version",
    "skill",
    "artifact",
    "action_record",
    "run_event",
    "run_step",
    "skill_load",
    "skill_version_file",
    # P1 记忆：删除/撤回后必须真正不可召回
    "memory_topic",
    "memory_log",
    "memory_working",
    "memory_recall",
    "memory_index_entry",
    "dream_job",
)

#: ``all`` 范围额外清除的治理 / 统计表
GOVERNANCE_TABLES = ("job", "budget_ledger", "daily_usage", "idempotency", "product_event", "data_op")
AUDIT_TABLE = "audit_event"

MAX_LISTED_IDS = 500


# --------------------------------------------------------------------------- 工具


def _json_load(text: Any, default: Any = None) -> Any:
    if text is None:
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _is_excluded_rel(rel: str) -> bool:
    parts = PurePosixPath(str(rel).replace("\\", "/")).parts
    if any(p in EXCLUDED_DIR_NAMES for p in parts):
        return True
    name = parts[-1] if parts else ""
    if name in EXCLUDED_FILE_NAMES:
        return True
    if name.startswith(".tmp-"):
        return True
    return name.endswith(EXCLUDED_FILE_SUFFIXES)


def _safe_rel(rel: str) -> str | None:
    """相对路径规范化；绝对路径或 ``..`` 逃逸返回 ``None``。"""
    if not rel:
        return None
    pure = PurePosixPath(str(rel).replace("\\", "/"))
    if pure.is_absolute() or any(p == ".." for p in pure.parts):
        return None
    return pure.as_posix()


def _safe_join(base: Path, rel: str) -> Path | None:
    clean = _safe_rel(rel)
    if clean is None:
        return None
    target = (base / clean).resolve(strict=False)
    try:
        base_r = base.resolve(strict=False)
    except OSError:  # pragma: no cover
        return None
    if target != base_r and base_r not in target.parents:
        return None
    return target


def _iter_tree(root: Path) -> Iterator[Path]:
    """遍历目录下所有普通文件（跳过缓存 / 临时 / 凭据类路径）。"""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIR_NAMES and not d.startswith(".tmp-"))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if _is_excluded_rel(rel) or is_ignored_path(rel):
                continue
            if path.is_file():
                yield path


def _copy_and_hash(src: Path, dst: Path) -> tuple[str, int]:
    """流式复制并计算 sha256，避免大文件一次性读入内存。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    h = hashlib.sha256()
    with open(src, "rb") as fh, open(dst, "wb") as out:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
            out.write(chunk)
    return h.hexdigest(), size


def _file_entry(root: Path, path: Path) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hash_file(path),
        "size": path.stat().st_size,
    }


def _bak_sibling(path: Path, ts: str) -> Path:
    """``<name>.bak-<ts>``；已存在时追加序号，保证多次恢复不互相覆盖。"""
    candidate = path.with_name(f"{path.name}.bak-{ts}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{ts}-{counter}")
        counter += 1
    return candidate


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                continue
    return total


def _version_tuple(value: str) -> tuple:
    parts = []
    for chunk in str(value or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _looks_textual(data: bytes) -> bool:
    if not data:
        return True
    if b"\x00" in data[:4096]:
        return False
    try:
        data[:65536].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _table_counts(conn: sqlite3.Connection, tables: Iterable[str] = COUNTED_TABLES) -> dict:
    counts: dict[str, int] = {}
    for table in tables:
        if not _table_exists(conn, table):
            continue
        try:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:  # pragma: no cover
            continue
    return counts


def _delete_ids(conn: sqlite3.Connection, table: str, ids: Iterable[str], column: str = "id") -> int:
    values = [v for v in ids if v is not None]
    if not values:
        return 0
    deleted = 0
    for start in range(0, len(values), 400):
        chunk = values[start : start + 400]
        marks = ", ".join("?" for _ in chunk)
        cur = conn.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", tuple(chunk))
        deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return deleted


# --------------------------------------------------------------------------- 服务


class DataService:
    """数据控制面：用量、备份、恢复、导出、预览与清除。"""

    def __init__(self, store, repo, blobs, events, idem, config) -> None:
        self.store = store
        self.repo = repo
        self.blobs = blobs
        self.events = events
        self.idem = idem
        self.config = config

    # ------------------------------------------------------------------ 路径
    @property
    def db_path(self) -> Path:
        return Path(getattr(self.store, "db_path", None) or self.config.db_path)

    @property
    def blob_root(self) -> Path:
        root = getattr(self.blobs, "root", None) or self.config.blob_dir
        return Path(root)

    @property
    def export_dir(self) -> Path:
        return Path(getattr(self.config, "export_dir", Path(self.config.home) / "exports"))

    # ------------------------------------------------------------------ 用量
    def usage(self) -> dict:
        """本地用量统计。

        返回模型调用数、token、未知用量次数、按天聚合、制品数量与体积、数据库体积。
        """
        daily = [dict(r) for r in self.store.query("SELECT * FROM daily_usage ORDER BY day ASC")]
        by_day = [
            {
                "day": row.get("day"),
                "model_calls": int(row.get("model_calls") or 0),
                "tokens": int(row.get("tokens") or 0),
                "unknown_usage": int(row.get("unknown_usage") or 0),
                "updated_at": row.get("updated_at"),
            }
            for row in daily
        ]
        model_calls = sum(row["model_calls"] for row in by_day)
        tokens = sum(row["tokens"] for row in by_day)
        daily_unknown = sum(row["unknown_usage"] for row in by_day)

        ledger_row = self.store.query_one(
            "SELECT COUNT(*) AS entries, "
            "COALESCE(SUM(used_calls), 0) AS used_calls, "
            "COALESCE(SUM(used_tokens), 0) AS used_tokens, "
            "COALESCE(SUM(reserved_tokens), 0) AS reserved_tokens, "
            "COALESCE(SUM(used_tool_actions), 0) AS used_tool_actions, "
            "COALESCE(SUM(unknown_usage), 0) AS unknown_usage, "
            "COALESCE(SUM(external_calls), 0) AS external_calls "
            "FROM budget_ledger"
        )
        ledger = {
            "entries": int(ledger_row["entries"] or 0),
            "used_calls": int(ledger_row["used_calls"] or 0),
            "used_tokens": int(ledger_row["used_tokens"] or 0),
            "reserved_tokens": int(ledger_row["reserved_tokens"] or 0),
            "used_tool_actions": int(ledger_row["used_tool_actions"] or 0),
            "unknown_usage": int(ledger_row["unknown_usage"] or 0),
            "external_calls": int(ledger_row["external_calls"] or 0),
        }

        run_total = 0
        run_reported = 0
        run_unknown = 0
        run_tokens = 0
        run_calls = 0
        for row in self.store.query("SELECT usage FROM run"):
            run_total += 1
            payload = _json_load(row["usage"], {}) or {}
            if not isinstance(payload, dict) or not payload:
                continue
            calls = int(payload.get("model_calls") or payload.get("calls") or 0)
            tok = int(payload.get("tokens") or payload.get("total_tokens") or 0)
            unknown = int(
                payload.get("unknown_usage") or payload.get("usage_unknown") or payload.get("unknown") or 0
            )
            if calls or tok:
                run_reported += 1
            run_unknown += unknown
            run_tokens += tok
            run_calls += calls

        blob_usage = self.blobs.usage() if hasattr(self.blobs, "usage") else {}
        db_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "generated_at": utc_now(),
            "model_calls": model_calls,
            "tokens": tokens,
            "unknown_usage_count": daily_unknown + ledger["unknown_usage"],
            "by_day": by_day,
            "daily_usage": {
                "rows": len(by_day),
                "model_calls": model_calls,
                "tokens": tokens,
                "unknown_usage": daily_unknown,
            },
            "unknown_usage": {
                "daily": daily_unknown,
                "ledger": ledger["unknown_usage"],
                "runs_reported_unknown": run_unknown,
                "total": daily_unknown + ledger["unknown_usage"],
            },
            "budget_ledger": ledger,
            "runs": {
                "total": run_total,
                "with_usage": run_reported,
                "model_calls_reported": run_calls,
                "tokens_reported": run_tokens,
            },
            "blob_count": int(blob_usage.get("blob_count") or 0),
            "blob_bytes": int(blob_usage.get("blob_bytes") or 0),
            "db_bytes": db_bytes,
            "db_path": str(self.db_path),
            "export_bytes": _dir_size(self.export_dir),
            "home": str(self.config.home),
            "secrets_included": False,
        }

    # ------------------------------------------------------------------ 备份
    def backup(self, dest: str, *, include_body: bool = True, idempotency_key: str | None = None) -> dict:
        """版本化备份：manifest + 一致性数据库副本 + 制品副本，不含凭据。"""
        target = str(Path(dest).expanduser())
        payload = {"dest": target, "include_body": bool(include_body)}
        return self.idem.run(
            "data.backup",
            idempotency_key,
            payload,
            lambda: self._backup(Path(target), include_body=bool(include_body)),
        )

    def _backup(self, dest: Path, *, include_body: bool) -> dict:
        if dest.exists():
            if dest.is_file():
                raise InvalidInput(f"备份目标已是文件: {dest}", field_errors={"dest": "not_a_directory"})
            if any(dest.iterdir()):
                raise InvalidInput(
                    f"备份目标目录非空，拒绝覆盖: {dest}", field_errors={"dest": "not_empty"}
                )

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".tmp-{dest.name}-{os.getpid()}-{new_id('bk')}"
        if tmp.exists():  # pragma: no cover - 极端冲突
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)

        try:
            db_snapshot = tmp / "db" / "skillforge.db"
            self._snapshot_db(db_snapshot)

            redacted: list[str] = []
            if not include_body:
                redacted = self._redact_bodies(db_snapshot)

            blob_result = self._copy_blobs(tmp / "blobs")

            manifest = self._build_backup_manifest(
                tmp,
                db_snapshot,
                include_body=include_body,
                redacted=redacted,
                blob_result=blob_result,
            )
            self._write_manifest(tmp, manifest)
            if dest.exists():
                dest.rmdir()
            os.replace(tmp, dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        manifest_path = dest / "manifest.json"
        summary = {
            "kind": "backup",
            "dest": str(dest),
            "manifest_path": str(manifest_path),
            "manifest_version": manifest["manifest_version"],
            "manifest_hash": manifest["manifest_hash"],
            "schema_version": manifest["schema_version"],
            "created_at": manifest["created_at"],
            "include_body": include_body,
            "object_counts": manifest["object_counts"],
            "file_count": len(manifest["files"]) + 1,
            "total_bytes": _dir_size(dest),
            "blob_count": manifest["blob_count"],
            "blob_bytes": manifest["blob_bytes"],
            "excluded_blobs": manifest["excluded_blobs"],
            "redacted_fields": redacted,
            "excludes": ["secrets/", "凭据文件（.env / *.pem / *id_rsa 等）", "命中静态密钥模式的制品"],
        }
        op_id = self._record_data_op(
            kind="backup", scope={"include_body": include_body}, status="done",
            manifest_path=str(manifest_path), manifest=manifest, result=summary,
        )
        audit_id = self.events.audit(
            "data.backup",
            "data_op",
            op_id,
            after={"manifest_path": str(manifest_path), "manifest_hash": manifest["manifest_hash"]},
            reason="本地备份",
            permission_scope={"scope": "local", "credentials": False},
        )
        self._product_event(
            "backup_exported",
            {"object_scope": "local", "manifest_id": manifest["manifest_hash"], "status": "done"},
        )
        summary["data_op_id"] = op_id
        summary["audit_id"] = audit_id
        return summary

    def _snapshot_db(self, dest_path: Path) -> None:
        src_path = self.db_path
        if not src_path.is_file():
            raise StorageError(f"数据库文件不存在，无法备份: {src_path}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(src_path), timeout=30.0)
        try:
            src.execute("PRAGMA busy_timeout=30000")
            dst = sqlite3.connect(str(dest_path), timeout=30.0)
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        except sqlite3.Error as exc:  # pragma: no cover - 磁盘 / 锁异常
            raise StorageError(f"生成数据库一致性副本失败: {exc}") from exc
        finally:
            src.close()

    def _redact_bodies(self, db_file: Path) -> list[str]:
        """``include_body=False``：清空正文类字段，只保留结构与引用。"""
        redacted: list[str] = []
        conn = sqlite3.connect(str(db_file))
        try:
            for table, columns in BODY_FIELDS.items():
                if not _table_exists(conn, table):
                    continue
                available = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column in columns:
                    if column not in available:
                        continue
                    conn.execute(f"UPDATE {table} SET {column} = '' WHERE {column} IS NOT NULL")
                    redacted.append(f"{table}.{column}")
            conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover
            raise StorageError(f"正文删减失败: {exc}") from exc
        finally:
            conn.close()
        return redacted

    def _copy_blobs(self, dest_blob_dir: Path) -> dict:
        """复制制品库；命中静态密钥模式的制品被排除并显式记录（绝不带凭据）。"""
        copied: list[dict] = []
        excluded: list[dict] = []
        root = self.blob_root
        for path in _iter_tree(root):
            rel = path.relative_to(root).as_posix()
            findings = self._scan_file_for_secrets(path)
            if findings:
                excluded.append(
                    {
                        "blob_id": path.name,
                        "path": rel,
                        "reason": "secret_detected",
                        "kinds": sorted({f["kind"] for f in findings}),
                    }
                )
                continue
            digest, size = _copy_and_hash(path, dest_blob_dir / rel)
            copied.append({"blob_id": path.name, "path": rel, "sha256": digest, "size": size})
        copied.sort(key=lambda item: item["path"])
        excluded.sort(key=lambda item: item["path"])
        return {
            "blobs": copied,
            "excluded": excluded,
            "blob_count": len(copied),
            "blob_bytes": sum(item["size"] for item in copied),
        }

    def _scan_file_for_secrets(self, path: Path) -> list[dict]:
        try:
            size = path.stat().st_size
        except OSError:  # pragma: no cover
            return []
        if size == 0 or size > MAX_SECRET_SCAN_BYTES:
            return []
        try:
            data = path.read_bytes()
        except OSError:  # pragma: no cover
            return []
        if not _looks_textual(data):
            return []
        return scan_secrets(data.decode("utf-8", errors="replace"))

    def _build_backup_manifest(
        self,
        root: Path,
        db_snapshot: Path,
        *,
        include_body: bool,
        redacted: list[str],
        blob_result: dict,
    ) -> dict:
        conn = sqlite3.connect(f"file:{db_snapshot}?mode=ro", uri=True)
        try:
            object_counts = _table_counts(conn)
            packages = self._package_manifest(conn, blob_result)
        finally:
            conn.close()

        files: list[dict] = []
        for path in _iter_tree(root):
            rel = path.relative_to(root).as_posix()
            if rel in ("manifest.json", "db/skillforge.db-wal", "db/skillforge.db-shm"):
                continue
            files.append(_file_entry(root, path))
        files.sort(key=lambda item: item["path"])

        manifest: dict[str, Any] = {
            "kind": BACKUP_KIND,
            "manifest_version": MANIFEST_VERSION,
            "schema_version": SCHEMA_VERSION,
            "app_version": _app_version(),
            "created_at": utc_now(),
            "include_body": include_body,
            "credentials_included": False,
            "excludes": sorted(EXCLUDED_DIR_NAMES) + sorted(EXCLUDED_FILE_NAMES),
            "database": {
                "path": "db/skillforge.db",
                "sha256": hash_file(db_snapshot),
                "size": db_snapshot.stat().st_size,
            },
            "object_counts": object_counts,
            "packages": packages,
            "files": files,
            "blobs": blob_result["blobs"],
            "blob_count": blob_result["blob_count"],
            "blob_bytes": blob_result["blob_bytes"],
            "excluded_blobs": blob_result["excluded"],
            "redacted_fields": redacted,
            "file_count": len(files),
            "total_bytes": sum(item["size"] for item in files),
        }
        manifest["manifest_hash"] = canonical_hash(manifest)
        return manifest

    def _package_manifest(self, conn: sqlite3.Connection, blob_result: dict) -> list[dict]:
        available = {item["blob_id"] for item in blob_result["blobs"]}
        packages: list[dict] = []
        if not _table_exists(conn, "skill_version"):
            return packages
        rows = conn.execute(
            "SELECT v.id, v.skill_id, v.version_label, v.package_hash, v.file_count, v.total_bytes, "
            "       v.origin, v.validation_status, s.slug, s.name "
            "FROM skill_version v LEFT JOIN skill s ON s.id = v.skill_id "
            "ORDER BY v.created_at ASC"
        ).fetchall()
        for row in rows:
            files = []
            if _table_exists(conn, "skill_version_file"):
                for f in conn.execute(
                    "SELECT path, content_hash, blob_id, size FROM skill_version_file "
                    "WHERE version_id = ? ORDER BY path ASC",
                    (row[0],),
                ).fetchall():
                    files.append(
                        {
                            "path": f[0],
                            "sha256": f[1],
                            "blob_id": f[2],
                            "size": int(f[3] or 0),
                            "available": f[2] in available,
                        }
                    )
            packages.append(
                {
                    "version_id": row[0],
                    "skill_id": row[1],
                    "version_label": row[2],
                    "package_hash": row[3],
                    "file_count": int(row[4] or 0),
                    "total_bytes": int(row[5] or 0),
                    "origin": row[6],
                    "validation_status": row[7],
                    "slug": row[8],
                    "name": row[9],
                    "files": files,
                    "complete": all(f["available"] for f in files) if files else False,
                }
            )
        return packages

    def _write_manifest(self, root: Path, manifest: dict) -> None:
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    # ------------------------------------------------------------------ 恢复
    def restore(self, backup_path: str, *, dry_run: bool = True, idempotency_key: str | None = None) -> dict:
        """校验并（可选）应用恢复；校验失败绝不覆盖当前库。"""
        source = Path(str(backup_path)).expanduser()
        if dry_run:
            return self._restore(source, dry_run=True)
        payload = {"backup_path": str(source), "dry_run": False}
        return self.idem.run(
            "data.restore", idempotency_key, payload, lambda: self._restore(source, dry_run=False)
        )

    def _restore(self, source: Path, *, dry_run: bool) -> dict:
        validation = self._validate_backup(source)
        manifest = validation["manifest"]
        public_manifest = {
            "kind": manifest.get("kind"),
            "manifest_version": manifest.get("manifest_version"),
            "schema_version": manifest.get("schema_version"),
            "app_version": manifest.get("app_version"),
            "created_at": manifest.get("created_at"),
            "manifest_hash": manifest.get("manifest_hash"),
            "include_body": manifest.get("include_body"),
            "object_counts": manifest.get("object_counts", {}),
            "file_count": manifest.get("file_count"),
            "blob_count": manifest.get("blob_count"),
            "blob_bytes": manifest.get("blob_bytes"),
            "packages": len(manifest.get("packages") or []),
        }
        result: dict[str, Any] = {
            "kind": "restore",
            "dry_run": dry_run,
            "ok": True,
            "backup_path": str(source),
            "manifest": public_manifest,
            "validation": {
                "checks": validation["checks"],
                "warnings": validation["warnings"],
                "errors": [],
            },
        }
        if dry_run:
            result["preview"] = self._restore_preview(manifest, validation)
            result["applied"] = None
            return result

        applied = self._apply_restore(source, manifest, validation)
        result["applied"] = applied
        result["preview"] = None
        summary = {
            "backup_path": str(source),
            "manifest_hash": manifest.get("manifest_hash"),
            "moved_aside": applied["moved_aside"],
            "restored_blobs": applied["restored_blobs"],
            "rebuilt": applied["rebuilt"],
        }
        op_id = self._record_data_op(
            kind="restore",
            scope={"backup_path": str(source)},
            status="done",
            manifest_path=str(source / "manifest.json"),
            manifest=manifest,
            result=summary,
        )
        audit_id = self.events.audit(
            "data.restore",
            "data_op",
            op_id,
            before={"db": str(self.db_path)},
            after={"backup_path": str(source), "manifest_hash": manifest.get("manifest_hash")},
            reason="恢复备份",
            permission_scope={"scope": "local", "dry_run": False},
        )
        self._product_event(
            "restored",
            {"object_scope": "local", "manifest_id": manifest.get("manifest_hash"), "status": "done"},
        )
        result["data_op_id"] = op_id
        result["audit_id"] = audit_id
        return result

    def _validate_backup(self, source: Path) -> dict:
        if not source.exists() or not source.is_dir():
            raise InvalidInput(
                f"备份路径不存在或不是目录: {source}", field_errors={"backup_path": "not_found"}
            )
        manifest_path = source / "manifest.json"
        if not manifest_path.is_file():
            raise InvalidInput(
                f"备份缺少 manifest.json: {source}", field_errors={"backup_path": "manifest_missing"}
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError(f"备份 manifest 无法解析: {exc}") from exc
        if not isinstance(manifest, dict):
            raise StorageError("备份 manifest 结构非法")

        errors: list[dict] = []
        warnings: list[dict] = []
        checks: list[dict] = []

        if manifest.get("kind") != BACKUP_KIND:
            errors.append(
                {
                    "check": "kind",
                    "detail": f"manifest.kind={manifest.get('kind')!r}，恢复只接受 {BACKUP_KIND}",
                }
            )
        checks.append({"check": "kind", "status": "ok" if not errors else "failed"})

        version = str(manifest.get("manifest_version") or "")
        if version not in SUPPORTED_MANIFEST_VERSIONS:
            errors.append(
                {
                    "check": "manifest_version",
                    "detail": f"不支持的 manifest 版本 {version!r}，支持 {SUPPORTED_MANIFEST_VERSIONS}",
                }
            )
        checks.append({"check": "manifest_version", "status": "ok" if version in SUPPORTED_MANIFEST_VERSIONS else "failed", "value": version})

        schema_version = str(manifest.get("schema_version") or "")
        if schema_version and _version_tuple(schema_version) > _version_tuple(SCHEMA_VERSION):
            errors.append(
                {
                    "check": "schema_version",
                    "detail": f"备份 schema {schema_version} 高于当前程序 {SCHEMA_VERSION}",
                }
            )
        elif schema_version and schema_version != SCHEMA_VERSION:
            warnings.append(
                {"check": "schema_version", "detail": f"备份 schema {schema_version} 与当前 {SCHEMA_VERSION} 不同，将按当前 schema 迁移"}
            )
        checks.append({"check": "schema_version", "status": "ok", "value": schema_version or SCHEMA_VERSION})

        recomputed = canonical_hash({k: v for k, v in manifest.items() if k != "manifest_hash"})
        if recomputed != manifest.get("manifest_hash"):
            errors.append(
                {
                    "check": "manifest_hash",
                    "detail": "manifest 内容 hash 不匹配（备份清单可能被篡改）",
                    "expected": manifest.get("manifest_hash"),
                    "actual": recomputed,
                }
            )
        checks.append(
            {"check": "manifest_hash", "status": "ok" if recomputed == manifest.get("manifest_hash") else "failed"}
        )

        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            errors.append({"check": "files", "detail": "manifest.files 缺失或为空"})
            files = []
        has_db = False
        for entry in files:
            if not isinstance(entry, dict):
                errors.append({"check": "files", "detail": "files 条目结构非法"})
                continue
            rel = entry.get("path")
            clean = _safe_rel(str(rel)) if rel else None
            if clean is None:
                errors.append({"check": "file_path", "detail": f"非法文件路径: {rel!r}"})
                continue
            if clean == "db/skillforge.db":
                has_db = True
            path = source / clean
            if not path.is_file():
                errors.append({"check": "file_missing", "path": clean, "detail": "清单中的文件缺失"})
                continue
            actual = hash_file(path)
            if actual != entry.get("sha256"):
                errors.append(
                    {
                        "check": "file_hash",
                        "path": clean,
                        "expected": entry.get("sha256"),
                        "actual": actual,
                        "detail": "文件 sha256 与清单不一致",
                    }
                )
                continue
            if int(entry.get("size") or 0) != path.stat().st_size:
                errors.append({"check": "file_size", "path": clean, "detail": "文件大小与清单不一致"})
        if not has_db:
            errors.append({"check": "database", "detail": "备份不含 db/skillforge.db"})
        checks.append({"check": "files", "status": "ok" if not [e for e in errors if e["check"].startswith("file") or e["check"] == "database"] else "failed", "count": len(files)})

        db_entry = manifest.get("database") or {}
        if db_entry.get("sha256") and not any(e["check"] == "file_hash" and e.get("path") == "db/skillforge.db" for e in errors):
            db_file = source / "db" / "skillforge.db"
            if db_file.is_file() and hash_file(db_file) != db_entry.get("sha256"):
                errors.append({"check": "database_hash", "detail": "数据库副本 hash 不匹配"})

        missing_blobs = self._missing_blobs(source, manifest)
        if missing_blobs:
            warnings.append(
                {
                    "check": "blobs",
                    "detail": f"{len(missing_blobs)} 个被引用的制品在备份与当前库中都不存在，相关引用会标记 unavailable",
                }
            )
        checks.append({"check": "blobs", "status": "ok" if not missing_blobs else "warning", "missing": len(missing_blobs)})

        if errors:
            detail = "; ".join(f"{e['check']}: {e.get('detail', '')}" for e in errors[:6])
            raise StorageError(
                f"备份校验失败（当前数据未被修改）: {detail}",
                field_errors={"backup_path": "validation_failed"},
            )

        return {
            "manifest": manifest,
            "manifest_path": manifest_path,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
            "missing_blobs": missing_blobs,
        }

    def _missing_blobs(self, source: Path, manifest: dict) -> list[dict]:
        backup_blobs = {item["blob_id"] for item in (manifest.get("blobs") or [])}
        db_file = source / "db" / "skillforge.db"
        missing: list[dict] = []
        if not db_file.is_file():
            return missing
        local = self.blob_root
        referenced: set[tuple[str, str]] = set()
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            for table, column in (("skill_version_file", "blob_id"), ("artifact", "blob_id")):
                if not _table_exists(conn, table):
                    continue
                for row in conn.execute(f"SELECT DISTINCT {column}, {column} FROM {table}"):
                    if row[0]:
                        referenced.add((table, row[0]))
        except sqlite3.Error:  # pragma: no cover
            return missing
        finally:
            conn.close()
        for table, blob_id in sorted(referenced):
            if blob_id in backup_blobs:
                continue
            if (local / blob_id[:2] / blob_id).is_file():
                continue
            missing.append({"table": table, "blob_id": blob_id, "unavailable": True, "reason": "blob_missing"})
        return missing

    def _restore_preview(self, manifest: dict, validation: dict) -> dict:
        backup_counts = manifest.get("object_counts") or {}
        current_counts = self._current_counts()
        comparisons = {}
        for table in sorted(set(backup_counts) | set(current_counts)):
            current = int(current_counts.get(table, 0))
            incoming = int(backup_counts.get(table, 0))
            comparisons[table] = {"current": current, "backup": incoming, "delta": incoming - current}
        would_move = []
        if self.db_path.exists():
            would_move.append(str(self.db_path))
        if self.blob_root.exists() and any(self.blob_root.iterdir()):
            would_move.append(str(self.blob_root))
        return {
            "object_counts": backup_counts,
            "current_counts": current_counts,
            "comparison": comparisons,
            "packages": len(manifest.get("packages") or []),
            "missing_blobs": validation["missing_blobs"],
            "would_move_aside": would_move,
            "backup_bytes": manifest.get("total_bytes"),
            "note": "dry_run 预览不改动当前数据；dry_run=False 时当前库与制品会先移动到 .bak-<ts>",
        }

    def _current_counts(self) -> dict:
        counts: dict[str, int] = {}
        for table in COUNTED_TABLES:
            try:
                counts[table] = int(self.store.scalar(f"SELECT COUNT(*) FROM {table}", default=0))
            except Exception:  # pragma: no cover - 表缺失
                continue
        return counts

    def _apply_restore(self, source: Path, manifest: dict, validation: dict) -> dict:
        ts = utc_now().replace("-", "").replace(":", "").replace(".", "")[:21]
        moved: list[str] = []

        db_path = self.db_path
        backup_db = source / "db" / "skillforge.db"
        if not backup_db.is_file():  # pragma: no cover - 已在校验中拦截
            raise StorageError("备份缺少数据库副本")

        # 释放本进程连接，避免后续读到被替换前的旧 inode
        try:
            self.store.close()
        except Exception:  # pragma: no cover
            pass

        if db_path.exists():
            bak_db = _bak_sibling(db_path, ts)
            os.replace(db_path, bak_db)
            moved.append(str(bak_db))
            for suffix in ("-wal", "-shm"):
                side = Path(str(db_path) + suffix)
                if side.exists():
                    os.replace(side, Path(str(bak_db) + suffix))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(backup_db, db_path)

        blob_root = self.blob_root
        backup_blobs = source / "blobs"
        restored_blobs = 0
        merged_blobs = 0
        old_blob_bak: Path | None = None
        if blob_root.exists() and any(blob_root.iterdir()):
            old_blob_bak = _bak_sibling(blob_root, ts)
            os.replace(blob_root, old_blob_bak)
            moved.append(str(old_blob_bak))
        blob_root.mkdir(parents=True, exist_ok=True)
        if backup_blobs.is_dir():
            for path in _iter_tree(backup_blobs):
                rel = path.relative_to(backup_blobs).as_posix()
                target = blob_root / rel
                digest, _size = _copy_and_hash(path, target)
                if digest != path.name:
                    target.unlink(missing_ok=True)
                    raise StorageError(f"备份制品 hash 不匹配: {rel}")
                restored_blobs += 1
        if old_blob_bak is not None and old_blob_bak.is_dir():
            for path in _iter_tree(old_blob_bak):
                rel = path.relative_to(old_blob_bak).as_posix()
                target = blob_root / rel
                if not target.exists():
                    _copy_and_hash(path, target)
                    merged_blobs += 1

        if hasattr(self.config, "ensure_dirs"):
            self.config.ensure_dirs()

        # 重建：在当前（新）库上执行 schema 迁移并核对版本 / 关联
        self.store.migrate()
        rebuilt = self._rebuild_report()

        return {
            "backup_path": str(source),
            "restored_db": str(db_path),
            "restored_blobs": restored_blobs,
            "merged_blobs_from_previous": merged_blobs,
            "moved_aside": moved,
            "rebuilt": rebuilt,
            "warnings": validation["warnings"],
        }

    def _rebuild_report(self) -> dict:
        counts = self._current_counts()
        missing: list[dict] = []
        rows = self.store.query(
            "SELECT f.version_id, f.path, f.blob_id FROM skill_version_file f"
        )
        for row in rows:
            blob_id = row["blob_id"]
            if not blob_id:
                continue
            if not (self.blob_root / blob_id[:2] / blob_id).is_file():
                missing.append(
                    {
                        "version_id": row["version_id"],
                        "path": row["path"],
                        "blob_id": blob_id,
                        "unavailable": True,
                        "reason": "blob_missing",
                    }
                )
        activations = [
            dict(r)
            for r in self.store.query(
                "SELECT a.skill_id, a.version_id, a.revision FROM activation a "
                "WHERE a.revision = (SELECT MAX(revision) FROM activation WHERE skill_id = a.skill_id)"
            )
        ]
        return {
            "skills": counts.get("skill", 0),
            "versions": counts.get("skill_version", 0),
            "version_files": counts.get("skill_version_file", 0),
            "activations": counts.get("activation", 0),
            "runs": counts.get("run", 0),
            "feedback": counts.get("feedback", 0),
            "proposals": counts.get("proposal", 0),
            "evaluation_jobs": counts.get("eval_job", 0),
            "active_pointers": activations,
            "missing_blobs": missing,
            "integrity_check": self.store.integrity_check(),
        }

    # ------------------------------------------------------------------ 导出
    def export_bundle(self, dest: str, *, scope: dict, include_body: bool = True) -> dict:
        """导出契约：版本化 manifest + 完整包 + 输入 / 产物快照
        评测报告与来源引用；无凭据；缺失依赖与删减引用显式 ``unavailable``。"""
        target = Path(str(dest)).expanduser()
        if target.exists() and (target.is_file() or any(target.iterdir())):
            raise InvalidInput(f"导出目标非空，拒绝覆盖: {target}", field_errors={"dest": "not_empty"})
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".tmp-{target.name}-{os.getpid()}-{new_id('exp')}"
        if tmp.exists():  # pragma: no cover
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)

        try:
            selection = self._export_selection(dict(scope or {}))
            package_entries = self._export_packages(tmp, selection, include_body=include_body)
            run_entries, references = self._export_runs(tmp, selection, include_body=include_body)
            evaluation_entries, source_entries = self._export_evaluations(
                tmp, selection, include_body=include_body
            )
            blob_result = self._copy_blobs(tmp / "blobs")

            files = [
                _file_entry(tmp, path)
                for path in _iter_tree(tmp)
                if path.relative_to(tmp).as_posix() not in ("manifest.json",)
            ]
            files.sort(key=lambda item: item["path"])
            unavailable = [
                ref for ref in references + evaluation_entries + package_entries if ref.get("unavailable")
            ]
            manifest: dict[str, Any] = {
                "kind": EXPORT_KIND,
                "manifest_version": MANIFEST_VERSION,
                "schema_version": SCHEMA_VERSION,
                "app_version": _app_version(),
                "created_at": utc_now(),
                "scope": selection["public_scope"],
                "include_body": include_body,
                "credentials_included": False,
                "object_counts": selection["object_counts"],
                "packages": package_entries,
                "runs": run_entries,
                "references": references,
                "evaluations": evaluation_entries,
                "sources": source_entries,
                "unavailable": unavailable,
                "unavailable_count": len(unavailable),
                "blobs": blob_result["blobs"],
                "excluded_blobs": blob_result["excluded"],
                "files": files,
                "file_count": len(files),
                "total_bytes": sum(item["size"] for item in files),
            }
            manifest["manifest_hash"] = canonical_hash(manifest)
            self._write_manifest(tmp, manifest)
            if target.exists():
                target.rmdir()
            os.replace(tmp, target)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        summary = {
            "kind": "export",
            "dest": str(target),
            "manifest_path": str(target / "manifest.json"),
            "manifest_version": manifest["manifest_version"],
            "manifest_hash": manifest["manifest_hash"],
            "created_at": manifest["created_at"],
            "scope": manifest["scope"],
            "include_body": include_body,
            "object_counts": manifest["object_counts"],
            "packages": len(package_entries),
            "runs": len(run_entries),
            "evaluations": len(evaluation_entries),
            "references": len(references),
            "unavailable_count": len(unavailable),
            "file_count": manifest["file_count"] + 1,
            "excluded_blobs": manifest["excluded_blobs"],
        }
        op_id = self._record_data_op(
            kind="export",
            scope=manifest["scope"],
            status="done",
            manifest_path=str(target / "manifest.json"),
            manifest=manifest,
            result=summary,
        )
        audit_id = self.events.audit(
            "data.export",
            "data_op",
            op_id,
            after={"dest": str(target), "scope": manifest["scope"], "manifest_hash": manifest["manifest_hash"]},
            reason="导出数据包",
            permission_scope={"scope": manifest["scope"], "credentials": False},
        )
        self._product_event(
            "backup_exported",
            {"object_scope": manifest["scope"], "manifest_id": manifest["manifest_hash"], "status": "done"},
        )
        summary["data_op_id"] = op_id
        summary["audit_id"] = audit_id
        return summary

    def _export_selection(self, scope: dict) -> dict:
        all_ = bool(scope.get("all") or not scope)
        workspace_id = scope.get("workspace_id")
        skill_ids = set(filter(None, scope.get("skill_ids") or []))
        if scope.get("skill_id"):
            skill_ids.add(scope["skill_id"])
        version_ids = set(filter(None, scope.get("version_ids") or []))
        run_ids = set(filter(None, scope.get("run_ids") or []))
        if scope.get("run_id"):
            run_ids.add(scope["run_id"])

        skills: list[dict] = []
        for row in self.store.query("SELECT * FROM skill ORDER BY created_at ASC"):
            if all_ or row["id"] in skill_ids or (workspace_id and row["workspace_id"] == workspace_id):
                skills.append(dict(row))
        skill_id_set = {s["id"] for s in skills} | skill_ids

        versions: list[dict] = []
        for row in self.store.query("SELECT * FROM skill_version ORDER BY created_at ASC"):
            if all_ or row["skill_id"] in skill_id_set or row["id"] in version_ids:
                versions.append(dict(row))
        version_id_set = {v["id"] for v in versions} | version_ids

        runs: list[dict] = []
        for row in self.store.query("SELECT * FROM run ORDER BY created_at ASC"):
            if all_ or row["id"] in run_ids or (workspace_id and row["workspace_id"] == workspace_id):
                runs.append(dict(row))
        run_id_set = {r["id"] for r in runs} | run_ids

        proposals = [
            dict(r)
            for r in self.store.query("SELECT * FROM proposal ORDER BY created_at ASC")
            if all_ or r["skill_id"] in skill_id_set or r["baseline_version_id"] in version_id_set
        ]
        proposal_ids = {p["id"] for p in proposals}
        suites = [
            dict(r)
            for r in self.store.query("SELECT * FROM eval_suite ORDER BY created_at ASC")
            if all_ or r["skill_id"] in skill_id_set
        ]
        suite_ids = {s["id"] for s in suites}
        jobs = [
            dict(r)
            for r in self.store.query("SELECT * FROM eval_job ORDER BY created_at ASC")
            if all_ or r["proposal_id"] in proposal_ids or r["suite_id"] in suite_ids
        ]
        job_ids = {j["id"] for j in jobs}
        feedback = [
            dict(r)
            for r in self.store.query("SELECT * FROM feedback ORDER BY created_at ASC")
            if all_ or r["run_id"] in run_id_set or r["target_skill_id"] in skill_id_set
        ]
        feedback_ids = {f["id"] for f in feedback}
        lessons = [
            dict(r)
            for r in self.store.query("SELECT * FROM lesson ORDER BY created_at ASC")
            if all_ or r["target_skill_id"] in skill_id_set
        ]
        lesson_ids = {l["id"] for l in lessons}

        public_scope = {
            "all": all_,
            "workspace_id": workspace_id,
            "skill_ids": sorted(skill_id_set),
            "version_ids": sorted(version_id_set),
            "run_ids": sorted(run_id_set),
        }
        object_counts = {
            "skill": len(skills),
            "skill_version": len(versions),
            "run": len(runs),
            "feedback": len(feedback),
            "lesson": len(lessons),
            "proposal": len(proposals),
            "eval_suite": len(suites),
            "eval_job": len(jobs),
            "eval_result": len(job_ids),
        }
        return {
            "all": all_,
            "public_scope": public_scope,
            "skills": skills,
            "skill_ids": skill_id_set,
            "versions": versions,
            "version_ids": version_id_set,
            "runs": runs,
            "run_ids": run_id_set,
            "proposals": proposals,
            "proposal_ids": proposal_ids,
            "suites": suites,
            "suite_ids": suite_ids,
            "jobs": jobs,
            "job_ids": job_ids,
            "feedback": feedback,
            "feedback_ids": feedback_ids,
            "lessons": lessons,
            "lesson_ids": lesson_ids,
            "object_counts": object_counts,
        }

    def _export_packages(self, root: Path, selection: dict, *, include_body: bool) -> list[dict]:
        entries: list[dict] = []
        skill_by_id = {s["id"]: s for s in selection["skills"]}
        for version in selection["versions"]:
            skill = skill_by_id.get(version["skill_id"], {})
            slug = skill.get("slug") or version["skill_id"]
            base = root / "packages" / slug / version["id"]
            files = []
            for row in self.store.query(
                "SELECT path, content_hash, blob_id, size FROM skill_version_file "
                "WHERE version_id = ? ORDER BY path ASC",
                (version["id"],),
            ):
                rel = row["path"]
                item = {
                    "path": rel,
                    "sha256": row["content_hash"],
                    "blob_id": row["blob_id"],
                    "size": int(row["size"] or 0),
                    "unavailable": False,
                }
                if not include_body:
                    item["unavailable"] = True
                    item["reason"] = "body_excluded"
                    files.append(item)
                    continue
                if not self.blobs.exists(row["blob_id"]):
                    item["unavailable"] = True
                    item["reason"] = "blob_missing"
                    files.append(item)
                    continue
                target = _safe_join(base, rel)
                if target is None:
                    item["unavailable"] = True
                    item["reason"] = "unsafe_path"
                    files.append(item)
                    continue
                try:
                    self.blobs.write_to(row["blob_id"], target)
                except Exception:  # pragma: no cover - 制品库异常
                    item["unavailable"] = True
                    item["reason"] = "blob_write_failed"
                files.append(item)
            entries.append(
                {
                    "type": "package",
                    "skill_id": version["skill_id"],
                    "skill_slug": slug,
                    "version_id": version["id"],
                    "version_label": version.get("version_label"),
                    "package_hash": version.get("package_hash"),
                    "origin": version.get("origin"),
                    "validation_status": version.get("validation_status"),
                    "files": files,
                    "unavailable": any(f["unavailable"] for f in files),
                    "unavailable_reason": "body_excluded"
                    if (not include_body and files)
                    else ("missing_blob" if any(f.get("reason") == "blob_missing" for f in files) else None),
                }
            )
        return entries

    def _export_runs(self, root: Path, selection: dict, *, include_body: bool) -> tuple[list[dict], list[dict]]:
        run_entries: list[dict] = []
        references: list[dict] = []
        for run in selection["runs"]:
            base = root / "runs" / run["id"]
            entry: dict[str, Any] = {
                "type": "run",
                "run_id": run["id"],
                "workspace_id": run.get("workspace_id"),
                "execution_status": run.get("execution_status"),
                "outcome": run.get("outcome"),
                "created_at": run.get("created_at"),
                "input_snapshot": {"path": None, "unavailable": True},
                "request_unavailable": not include_body,
                "request": run.get("request") if include_body else None,
                "output_text": run.get("output_text") if include_body else None,
            }
            if include_body:
                base.mkdir(parents=True, exist_ok=True)
                snapshot = base / "input_snapshot.json"
                snapshot.write_text(
                    json.dumps(_json_load(run.get("input_snapshot"), {}), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                entry["input_snapshot"] = {
                    "path": snapshot.relative_to(root).as_posix(),
                    "unavailable": False,
                }
            else:
                entry["request_unavailable"] = True
                entry["reason"] = "body_excluded"

            artifacts = []
            for row in self.store.query(
                "SELECT id, path, blob_id, content_hash, size, kind, label FROM artifact "
                "WHERE run_id = ? ORDER BY created_at ASC",
                (run["id"],),
            ):
                item = {
                    "type": "artifact",
                    "id": row["id"],
                    "run_id": run["id"],
                    "path": row["path"],
                    "blob_id": row["blob_id"],
                    "sha256": row["content_hash"],
                    "size": int(row["size"] or 0),
                    "kind": row["kind"],
                    "unavailable": False,
                    "exported_path": None,
                }
                if not include_body:
                    item["unavailable"] = True
                    item["reason"] = "body_excluded"
                elif not self.blobs.exists(row["blob_id"]):
                    item["unavailable"] = True
                    item["reason"] = "blob_missing"
                else:
                    target = _safe_join(base / "artifacts", row["path"])
                    if target is None:
                        item["unavailable"] = True
                        item["reason"] = "unsafe_path"
                    else:
                        try:
                            self.blobs.write_to(row["blob_id"], target)
                            item["exported_path"] = target.relative_to(root).as_posix()
                        except Exception:  # pragma: no cover
                            item["unavailable"] = True
                            item["reason"] = "blob_write_failed"
                artifacts.append(item)
                references.append(
                    {
                        "type": "artifact",
                        "id": row["id"],
                        "run_id": run["id"],
                        "blob_id": row["blob_id"],
                        "unavailable": item["unavailable"],
                        "reason": item.get("reason"),
                    }
                )
            entry["artifacts"] = artifacts
            run_entries.append(entry)
        return run_entries, references

    def _export_evaluations(
        self, root: Path, selection: dict, *, include_body: bool
    ) -> tuple[list[dict], list[dict]]:
        entries: list[dict] = []
        sources: list[dict] = []
        base = root / "evaluations"
        for job in selection["jobs"]:
            payload = {
                "type": "evaluation",
                "job_id": job["id"],
                "proposal_id": job.get("proposal_id"),
                "suite_id": job.get("suite_id"),
                "baseline_version_id": job.get("baseline_version_id"),
                "candidate_version_id": job.get("candidate_version_id"),
                "status": job.get("status"),
                "verdict": job.get("verdict"),
            }
            if include_body:
                base.mkdir(parents=True, exist_ok=True)
                path = base / f"{job['id']}.json"
                path.write_text(
                    json.dumps(
                        {
                            "job": {k: job.get(k) for k in job},
                            "report": _json_load(job.get("report"), {}),
                            "verdict_detail": _json_load(job.get("verdict_detail"), {}),
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    encoding="utf-8",
                )
                payload["report_path"] = path.relative_to(root).as_posix()
                payload["unavailable"] = False
            else:
                payload["report_path"] = None
                payload["unavailable"] = True
                payload["reason"] = "body_excluded"
            entries.append(payload)

        for proposal in selection["proposals"]:
            for row in self.store.query(
                "SELECT lesson_id FROM proposal_source WHERE proposal_id = ?", (proposal["id"],)
            ):
                sources.append(
                    {
                        "type": "proposal_source",
                        "proposal_id": proposal["id"],
                        "lesson_id": row["lesson_id"],
                        "unavailable": row["lesson_id"] not in selection["lesson_ids"],
                        "reason": None
                        if row["lesson_id"] in selection["lesson_ids"]
                        else "source_out_of_scope",
                    }
                )
        for lesson in selection["lessons"]:
            for row in self.store.query(
                "SELECT feedback_id FROM lesson_source WHERE lesson_id = ?", (lesson["id"],)
            ):
                sources.append(
                    {
                        "type": "lesson_source",
                        "lesson_id": lesson["id"],
                        "feedback_id": row["feedback_id"],
                        "unavailable": row["feedback_id"] not in selection["feedback_ids"],
                        "reason": None
                        if row["feedback_id"] in selection["feedback_ids"]
                        else "source_out_of_scope",
                    }
                )
        for feedback in selection["feedback"]:
            unavailable = not include_body or (
                feedback.get("run_id") is not None and feedback["run_id"] not in selection["run_ids"]
            )
            sources.append(
                {
                    "type": "feedback",
                    "id": feedback["id"],
                    "run_id": feedback.get("run_id"),
                    "type_detail": feedback.get("type"),
                    "scope": feedback.get("scope"),
                    "created_at": feedback.get("created_at"),
                    "content_unavailable": not include_body,
                    "unavailable": unavailable,
                    "reason": "body_excluded"
                    if not include_body
                    else ("run_out_of_scope" if unavailable else None),
                }
            )
        return entries, sources

    # ------------------------------------------------------------------ 预览清除
    def preview_clear(self, scope: dict) -> dict:
        """列出将被清除的关联对象；有运行引用的包默认保留。

        只读操作：空 scope 视为「预览全部」；真正删除必须用 ``clear_data`` 并显式指定范围。
        """
        namespace = self._normalize_scope(scope, default_all=True)
        plan = self._clear_plan(namespace)
        return plan["preview"]

    def clear_data(self, scope: dict, *, confirm: bool, idempotency_key: str | None = None) -> dict:
        """清除指定范围内的数据；``confirm`` 必须为 True。"""
        if confirm is not True:
            raise InvalidInput(
                "clear_data 需要 confirm=True 明确确认", field_errors={"confirm": "required"}
            )
        namespace = self._normalize_scope(scope)
        payload = {"scope": namespace["public"], "confirm": True}
        return self.idem.run(
            "data.clear", idempotency_key, payload, lambda: self._clear(namespace)
        )

    def _normalize_scope(self, scope: dict, *, default_all: bool = False) -> dict:
        if not isinstance(scope, dict):
            raise InvalidInput("scope 必须是对象", field_errors={"scope": "invalid_type"})
        all_ = bool(scope.get("all") or scope.get("purge_all"))
        workspace_id = scope.get("workspace_id")
        skill_ids = {s for s in (scope.get("skill_ids") or []) if s}
        if scope.get("skill_id"):
            skill_ids.add(scope["skill_id"])
        run_ids = {r for r in (scope.get("run_ids") or []) if r}
        if scope.get("run_id"):
            run_ids.add(scope["run_id"])
        if not (all_ or workspace_id or skill_ids or run_ids):
            if default_all:
                all_ = True
            else:
                raise InvalidInput(
                    "scope 必须指定 all / workspace_id / skill_id(s) / run_id(s) 之一；"
                    '清除全部数据请显式传 {"all": true}',
                    field_errors={"scope": "empty"},
                )
        return {
            "all": all_,
            "workspace_id": workspace_id,
            "skill_ids": skill_ids,
            "run_ids": run_ids,
            "purge_versions": bool(scope.get("purge_versions", False)),
            "purge_audit": bool(scope.get("purge_audit", False) or scope.get("include_audit", False) or all_),
            "purge_metrics": bool(scope.get("purge_metrics", False) or all_),
            "purge_backups": bool(scope.get("purge_backups", False)),
            "public": {
                "all": all_,
                "workspace_id": workspace_id,
                "skill_ids": sorted(skill_ids),
                "run_ids": sorted(run_ids),
                "purge_versions": bool(scope.get("purge_versions", False)),
                "purge_audit": bool(scope.get("purge_audit", False) or scope.get("include_audit", False) or all_),
                "purge_backups": bool(scope.get("purge_backups", False)),
            },
        }

    def _clear_plan(self, namespace: dict) -> dict:
        sets: dict[str, set[str]] = {name: set() for name in CLEAR_ID_TABLES}
        sets["lesson_source"] = set()
        sets["proposal_source"] = set()
        sets["skill_version_file"] = set()
        sets["memory_topic_source"] = set()

        if namespace["all"]:
            for table in ("run", "skill", "feedback", "lesson", "proposal", "eval_suite", "eval_case",
                          "eval_job", "eval_result", "activation", "memory_topic", "memory_log",
                          "memory_working", "memory_recall", "dream_job"):
                sets[table] = {
                    row["id"] for row in self.store.query(f"SELECT id FROM {table}")  # noqa: S608
                }
        else:
            if namespace["workspace_id"]:
                sets["run"] |= {
                    row["id"]
                    for row in self.store.query(
                        "SELECT id FROM run WHERE workspace_id = ?", (namespace["workspace_id"],)
                    )
                }
                sets["skill"] |= {
                    row["id"]
                    for row in self.store.query(
                        "SELECT id FROM skill WHERE workspace_id = ?", (namespace["workspace_id"],)
                    )
                }
            if namespace["workspace_id"]:
                for table in ("memory_topic", "memory_log", "memory_working", "memory_recall"):
                    sets[table] |= {
                        row["id"]
                        for row in self.store.query(
                            f"SELECT id FROM {table} WHERE workspace_id = ?",  # noqa: S608
                            (namespace["workspace_id"],),
                        )
                    }
            if namespace["run_ids"]:
                sets["memory_recall"] |= self._by_column("memory_recall", "run_id", namespace["run_ids"])
                sets["memory_log"] |= self._by_column("memory_log", "run_id", namespace["run_ids"])
            sets["run"] |= self._existing_ids("run", namespace["run_ids"])
            sets["skill"] |= self._existing_ids("skill", namespace["skill_ids"])
            if sets["skill"]:
                marks = ", ".join("?" for _ in sets["skill"])
                sets["skill_version"] |= {
                    row["id"]
                    for row in self.store.query(
                        f"SELECT id FROM skill_version WHERE skill_id IN ({marks})",  # noqa: S608
                        tuple(sets["skill"]),
                    )
                }
            if sets["run"]:
                marks = ", ".join("?" for _ in sets["run"])
                sets["feedback"] |= {
                    row["id"]
                    for row in self.store.query(
                        f"SELECT id FROM feedback WHERE run_id IN ({marks})", tuple(sets["run"])  # noqa: S608
                    )
                }
            if sets["skill"]:
                marks = ", ".join("?" for _ in sets["skill"])
                sets["feedback"] |= {
                    row["id"]
                    for row in self.store.query(
                        f"SELECT id FROM feedback WHERE target_skill_id IN ({marks})",  # noqa: S608
                        tuple(sets["skill"]),
                    )
                }
                sets["lesson"] |= {
                    row["id"]
                    for row in self.store.query(
                        f"SELECT id FROM lesson WHERE target_skill_id IN ({marks})",  # noqa: S608
                        tuple(sets["skill"]),
                    )
                }
                sets["proposal"] |= {
                    row["id"]
                    for row in self.store.query(
                        f"SELECT id FROM proposal WHERE skill_id IN ({marks})",  # noqa: S608
                        tuple(sets["skill"]),
                    )
                }
                sets["eval_suite"] |= {
                    row["id"]
                    for row in self.store.query(
                        f"SELECT id FROM eval_suite WHERE skill_id IN ({marks})",  # noqa: S608
                        tuple(sets["skill"]),
                    )
                }

        self._expand_clear_sets(sets)
        retained = self._apply_version_retention(sets, namespace)
        related = self._related_backups(namespace)
        return {
            "namespace": namespace,
            "sets": sets,
            "retained": retained,
            "related": related,
            "preview": self._build_preview(namespace, sets, retained, related),
        }

    def _by_column(self, table: str, column: str, values: Iterable[str]) -> set[str]:
        wanted = {value for value in values if value}
        if not wanted:
            return set()
        marks = ", ".join("?" for _ in wanted)
        return {
            row["id"]
            for row in self.store.query(
                f"SELECT id FROM {table} WHERE {column} IN ({marks})", tuple(wanted)  # noqa: S608
            )
        }

    def _existing_ids(self, table: str, ids: Iterable[str]) -> set[str]:
        values = {value for value in ids if value}
        if not values:
            return set()
        marks = ", ".join("?" for _ in values)
        return {
            row["id"]
            for row in self.store.query(
                f"SELECT id FROM {table} WHERE id IN ({marks})", tuple(values)  # noqa: S608
            )
        }

    def _expand_clear_sets(self, sets: dict[str, set[str]]) -> None:
        """FK 闭包：被删对象引用的行必须一起删除，否则事务无法提交。"""

        def ids(table: str, column: str, values: set[str]) -> set[str]:
            if not values:
                return set()
            marks = ", ".join("?" for _ in values)
            return {
                row["id"]
                for row in self.store.query(
                    f"SELECT id FROM {table} WHERE {column} IN ({marks})", tuple(values)  # noqa: S608
                )
            }

        def pick(table: str, want: str, column: str, values: set[str]) -> set[str]:
            if not values:
                return set()
            marks = ", ".join("?" for _ in values)
            return {
                row[0]
                for row in self.store.query(
                    f"SELECT DISTINCT {want} FROM {table} WHERE {column} IN ({marks})",  # noqa: S608
                    tuple(values),
                )
                if row[0]
            }

        for _ in range(8):
            before = sum(len(v) for v in sets.values())
            if sets["skill"]:
                sets["skill_version"] |= ids("skill_version", "skill_id", sets["skill"])
                sets["proposal"] |= ids("proposal", "skill_id", sets["skill"])
                sets["lesson"] |= ids("lesson", "target_skill_id", sets["skill"])
                sets["eval_suite"] |= ids("eval_suite", "skill_id", sets["skill"])
            if sets["skill_version"]:
                sets["skill_version_file"] |= pick(
                    "skill_version_file", "version_id", "version_id", sets["skill_version"]
                )
                sets["activation"] |= ids("activation", "version_id", sets["skill_version"])
                sets["feedback"] |= ids("feedback", "target_version_id", sets["skill_version"])
                sets["lesson"] |= ids("lesson", "target_version_id", sets["skill_version"])
                sets["proposal"] |= ids("proposal", "baseline_version_id", sets["skill_version"])
                sets["proposal"] |= ids("proposal", "candidate_version_id", sets["skill_version"])
            if sets["eval_suite"]:
                sets["eval_case"] |= ids("eval_case", "suite_id", sets["eval_suite"])
                sets["eval_job"] |= ids("eval_job", "suite_id", sets["eval_suite"])
            if sets["proposal"]:
                sets["eval_job"] |= ids("eval_job", "proposal_id", sets["proposal"])
            if sets["eval_job"]:
                sets["eval_result"] |= ids("eval_result", "job_id", sets["eval_job"])
            if sets["eval_case"]:
                sets["eval_result"] |= ids("eval_result", "case_id", sets["eval_case"])
            if sets["run"]:
                sets["feedback"] |= ids("feedback", "run_id", sets["run"])
                sets["artifact"] |= ids("artifact", "run_id", sets["run"])
                sets["action_record"] |= ids("action_record", "run_id", sets["run"])
                sets["run_event"] |= ids("run_event", "run_id", sets["run"])
                sets["run_step"] |= ids("run_step", "run_id", sets["run"])
                sets["skill_load"] |= ids("skill_load", "run_id", sets["run"])
                sets["eval_result"] |= ids("eval_result", "baseline_run_id", sets["run"])
                sets["eval_result"] |= ids("eval_result", "candidate_run_id", sets["run"])
            if sets["run"]:
                sets["memory_recall"] |= ids("memory_recall", "run_id", sets["run"])
                sets["memory_log"] |= ids("memory_log", "run_id", sets["run"])
            if sets["memory_topic"]:
                sets["memory_index_entry"] |= ids("memory_index_entry", "topic_id", sets["memory_topic"])
                sets["memory_topic_source"] |= {
                    f"topic:{value}"
                    for value in pick("memory_topic_source", "topic_id", "topic_id", sets["memory_topic"])
                }
            if sets["memory_log"]:
                sets["memory_topic_source"] |= {
                    f"log:{value}"
                    for value in pick("memory_topic_source", "source_id", "source_id", sets["memory_log"])
                }
            if sets["feedback"]:
                sets["lesson"] |= pick("lesson_source", "lesson_id", "feedback_id", sets["feedback"])
                sets["lesson_source"] |= {
                    f"feedback:{value}"
                    for value in pick("lesson_source", "feedback_id", "feedback_id", sets["feedback"])
                }
            if sets["lesson"]:
                sets["lesson_source"] |= {
                    f"lesson:{value}"
                    for value in pick("lesson_source", "lesson_id", "lesson_id", sets["lesson"])
                }
                sets["proposal_source"] |= {
                    f"lesson:{value}"
                    for value in pick("proposal_source", "lesson_id", "lesson_id", sets["lesson"])
                }
                sets["proposal"] |= pick("proposal_source", "proposal_id", "lesson_id", sets["lesson"])
            if sets["proposal"]:
                sets["proposal_source"] |= {
                    f"proposal:{value}"
                    for value in pick("proposal_source", "proposal_id", "proposal_id", sets["proposal"])
                }
            after = sum(len(v) for v in sets.values())
            if after == before:
                break

    def _apply_version_retention(self, sets: dict[str, set[str]], namespace: dict) -> list[dict]:
        """有运行引用的包默认不清除，除非 scope 明确 purge_versions=True。"""
        retained: list[dict] = []
        versions = sets["skill_version"]
        if not versions:
            return retained
        references: dict[str, set[str]] = {}
        for row in self.store.query("SELECT version_id, run_id FROM skill_load"):
            if row["version_id"] in versions:
                references.setdefault(row["version_id"], set()).add(row["run_id"])
        skill_refs: dict[str, set[str]] = {}
        for row in self.store.query("SELECT id, skill_ids FROM run"):
            for skill_id in _json_load(row["skill_ids"], []) or []:
                if skill_id in sets["skill"]:
                    skill_refs.setdefault(skill_id, set()).add(row["id"])

        if namespace["purge_versions"]:
            for version_id, run_ids in sorted(references.items()):
                retained.append(
                    {
                        "type": "skill_version",
                        "id": version_id,
                        "reason": "force_purged",
                        "run_ids": sorted(run_ids)[:20],
                        "note": "scope 显式 purge_versions=True，有运行引用的包也被清除",
                    }
                )
            return retained

        for version_id, run_ids in sorted(references.items()):
            sets["skill_version"].discard(version_id)
            retained.append(
                {
                    "type": "skill_version",
                    "id": version_id,
                    "reason": "referenced_by_run",
                    "run_ids": sorted(run_ids)[:20],
                    "note": "有运行引用的包默认保留；如确需删除请在 scope 中显式设置 purge_versions=True",
                }
            )
        # 版本被保留时其所属 skill 不能删除（FK: skill_version.skill_id）
        for skill_id in sorted(sets["skill"]):
            remaining = {
                row["id"]
                for row in self.store.query(
                    "SELECT id FROM skill_version WHERE skill_id = ?", (skill_id,)
                )
            } - sets["skill_version"]
            if remaining:
                sets["skill"].discard(skill_id)
                retained.append(
                    {
                        "type": "skill",
                        "id": skill_id,
                        "reason": "has_retained_versions",
                        "version_ids": sorted(remaining)[:20],
                    }
                )
        for skill_id, run_ids in sorted(skill_refs.items()):
            if skill_id in sets["skill"]:
                sets["skill"].discard(skill_id)
                retained.append(
                    {
                        "type": "skill",
                        "id": skill_id,
                        "reason": "referenced_by_run_skill_ids",
                        "run_ids": sorted(run_ids)[:20],
                    }
                )
        # activation 依赖 version：被保留版本的 activation 一并保留
        for row in self.store.query("SELECT id, version_id FROM activation"):
            if row["version_id"] not in sets["skill_version"]:
                sets["activation"].discard(row["id"])
        # 被保留版本的文件清单不进入待清除清单
        for row in self.store.query("SELECT version_id FROM skill_version_file"):
            if row["version_id"] not in sets["skill_version"]:
                sets["skill_version_file"].discard(row["version_id"])
        return retained

    def _related_backups(self, namespace: dict) -> list[dict]:
        """旧备份（含数据）不会被自动删除，需单独清除。"""
        related: list[dict] = []
        export_dir = self.export_dir
        if export_dir.is_dir():
            for child in sorted(export_dir.iterdir()):
                if not child.is_dir():
                    continue
                manifest_file = child / "manifest.json"
                if not manifest_file.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    manifest = {}
                related.append(
                    {
                        "type": "backup",
                        "path": str(child),
                        "kind": manifest.get("kind"),
                        "created_at": manifest.get("created_at"),
                        "manifest_hash": manifest.get("manifest_hash"),
                        "object_counts": manifest.get("object_counts", {}),
                        "reason": "requires_separate_delete",
                        "note": "旧备份仍含这些数据；默认不随清除删除，需单独删除该目录",
                    }
                )
        for row in self.store.query(
            "SELECT id, manifest_path, created_at, status FROM data_op WHERE kind = 'backup'"
        ):
            path = row["manifest_path"]
            if path and not any(item["path"] and str(Path(path).parent) == item["path"] for item in related):
                related.append(
                    {
                        "type": "backup_record",
                        "id": row["id"],
                        "path": str(Path(path).parent) if path else None,
                        "created_at": row["created_at"],
                        "status": row["status"],
                        "reason": "requires_separate_delete",
                    }
                )
        return related

    def _build_preview(self, namespace: dict, sets: dict, retained: list[dict], related: list[dict]) -> dict:
        counts = {table: len(ids) for table, ids in sets.items() if ids}
        business = {
            "feedback": counts.get("feedback", 0),
            "lesson": counts.get("lesson", 0) + counts.get("lesson_source", 0),
            "proposal": counts.get("proposal", 0) + counts.get("proposal_source", 0),
            "evaluation": sum(
                counts.get(table, 0)
                for table in ("eval_suite", "eval_case", "eval_job", "eval_result")
            ),
            "run": counts.get("run", 0)
            + sum(counts.get(table, 0) for table in ("run_step", "action_record", "run_event", "artifact", "skill_load")),
            "memory": sum(
                counts.get(table, 0)
                for table in ("memory_topic", "memory_topic_source", "memory_log", "memory_working",
                              "memory_index_entry", "memory_recall", "dream_job")
            ),
            "package": counts.get("skill", 0)
            + counts.get("skill_version", 0)
            + counts.get("skill_version_file", 0)
            + counts.get("activation", 0),
        }
        total = sum(business.values())
        governance = {
            table: int(self.store.scalar(f"SELECT COUNT(*) FROM {table}", default=0))  # noqa: S608
            for table in GOVERNANCE_TABLES
            if namespace["purge_metrics"]
        }
        audit_count = (
            int(self.store.scalar(f"SELECT COUNT(*) FROM {AUDIT_TABLE}", default=0))  # noqa: S608
            if namespace["purge_audit"]
            else 0
        )
        listed = {
            table: sorted(ids)[:MAX_LISTED_IDS] if len(ids) > MAX_LISTED_IDS else sorted(ids)
            for table, ids in sets.items()
            if ids
        }
        truncated = sorted(table for table, ids in sets.items() if len(ids) > MAX_LISTED_IDS)
        notes = []
        if retained:
            notes.append(
                f"{len(retained)} 个对象因有运行引用被保留；确需清除请在 scope 中显式设置 purge_versions=True"
            )
        if related:
            notes.append(
                f"旧备份含数据，需单独删除（{len(related)} 个）："
                + "、".join(item["path"] for item in related[:3] if item.get("path"))
            )
        notes.append("使用中的包与版本元数据默认不自动清理；清除后审计与产品事件按 scope 一并处理")
        return {
            "scope": namespace["public"],
            "counts": counts,
            "business": business,
            "total": total,
            "empty": total == 0,
            "governance": governance,
            "audit_event": audit_count,
            "governance_total": sum(governance.values()) + audit_count,
            "objects": listed,
            "truncated_tables": truncated,
            "retained": retained,
            "related": related,
            "backups": related,
            "notes": notes,
        }

    def _clear(self, namespace: dict) -> dict:
        plan = self._clear_plan(namespace)
        sets = plan["sets"]
        preview = plan["preview"]
        deleted_dirs: list[str] = []
        failed_dirs: list[dict] = []

        with self.store.tx() as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            self._delete_sets(conn, sets)
            if namespace["purge_metrics"]:
                for table in GOVERNANCE_TABLES:
                    conn.execute(f"DELETE FROM {table}")  # noqa: S608
            if namespace["purge_audit"]:
                conn.execute(f"DELETE FROM {AUDIT_TABLE}")  # noqa: S608

            result = {
                "kind": "clear",
                "scope": namespace["public"],
                "cleared": preview["counts"],
                "business": preview["business"],
                "total": preview["total"],
                "retained": plan["retained"],
                "governance_cleared": preview["governance"],
                "audit_cleared": preview["audit_event"],
                "backups_not_deleted": plan["related"],
                "backup_hint": "旧备份仍包含这些数据，需单独删除对应备份目录（或使用 scope.purge_backups=True）",
                "notes": preview["notes"],
            }
            op_id = new_id("dop")
            conn.execute(
                "INSERT INTO data_op (id, kind, scope, status, manifest_path, manifest, result, actor, created_at, finished_at) "
                "VALUES (?, 'clear', ?, 'done', NULL, ?, ?, 'owner', ?, ?)",
                (
                    op_id,
                    json.dumps(namespace["public"], ensure_ascii=False),
                    json.dumps({}, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False, default=str),
                    utc_now(),
                    utc_now(),
                ),
            )
            audit_id = self.events.audit(
                "data.clear",
                "data_op",
                op_id,
                before={"scope": namespace["public"]},
                after={"cleared": preview["business"], "retained": len(plan["retained"])},
                reason="用户明确清除数据",
                permission_scope={"scope": namespace["public"], "confirm": True},
                conn=conn,
            )
            result["data_op_id"] = op_id
            result["audit_id"] = audit_id

        if namespace["purge_backups"]:
            for item in plan["related"]:
                path = item.get("path")
                if not path:
                    continue
                target = Path(path)
                if target.is_dir() and self._backup_in_scope(item, namespace):
                    try:
                        shutil.rmtree(target)
                        deleted_dirs.append(str(target))
                    except OSError as exc:  # pragma: no cover
                        failed_dirs.append({"path": str(target), "error": str(exc)})

        result["deleted_backup_dirs"] = deleted_dirs
        result["failed_backup_dirs"] = failed_dirs
        self._product_event(
            "data_cleared",
            {
                "object_scope": namespace["public"],
                "status": "done",
                "cleared_total": result["total"],
                "retained_total": len(result["retained"]),
            },
        )
        after = self.preview_clear(namespace["public"])
        result["preview_after"] = after
        result["empty_after"] = after["total"] == 0
        return result

    def _backup_in_scope(self, item: dict, namespace: dict) -> bool:
        if namespace["all"]:
            return True
        return bool(item.get("path"))

    def _delete_sets(self, conn: sqlite3.Connection, sets: dict[str, set[str]]) -> None:
        order = (
            "memory_topic_source",
            "memory_index_entry",
            "memory_recall",
            "memory_log",
            "memory_topic",
            "memory_working",
            "dream_job",
            "lesson_source",
            "proposal_source",
            "eval_result",
            "eval_job",
            "eval_case",
            "eval_suite",
            "feedback",
            "lesson",
            "action_record",
            "run_event",
            "run_step",
            "artifact",
            "skill_load",
            "run",
            "proposal",
            "activation",
            "skill_version_file",
            "skill_version",
            "skill",
        )
        feedback_ids = sets.get("feedback", set())
        lesson_ids = sets.get("lesson", set())
        if feedback_ids:
            _delete_ids(conn, "lesson_source", feedback_ids, "feedback_id")
        if lesson_ids:
            _delete_ids(conn, "lesson_source", lesson_ids, "lesson_id")
        if sets.get("proposal"):
            _delete_ids(conn, "proposal_source", sets["proposal"], "proposal_id")
        if lesson_ids:
            _delete_ids(conn, "proposal_source", lesson_ids, "lesson_id")
        if sets.get("skill_version"):
            _delete_ids(conn, "skill_version_file", sets["skill_version"], "version_id")
        if sets.get("memory_topic"):
            _delete_ids(conn, "memory_topic_source", sets["memory_topic"], "topic_id")
        if sets.get("memory_log"):
            _delete_ids(conn, "memory_topic_source", sets["memory_log"], "source_id")
        for table in order:
            if table in ("lesson_source", "proposal_source", "skill_version_file",
                         "memory_topic_source"):
                continue
            ids = sets.get(table, set())
            if ids:
                _delete_ids(conn, table, ids)

    # ------------------------------------------------------------------ 内部记录
    def _record_data_op(
        self,
        *,
        kind: str,
        scope: dict,
        status: str,
        manifest_path: str | None,
        manifest: dict,
        result: dict,
    ) -> str:
        op_id = new_id("dop")
        self.repo.insert(
            "data_op",
            {
                "id": op_id,
                "kind": kind,
                "scope": scope,
                "status": status,
                "manifest_path": manifest_path,
                "manifest": manifest,
                "result": result,
                "actor": "owner",
                "created_at": utc_now(),
                "finished_at": utc_now(),
            },
        )
        return op_id

    def _product_event(self, name: str, params: dict) -> None:
        try:
            self.events.product_event(name, params)
        except Exception:  # pragma: no cover - 产品事件失败不应影响数据操作
            pass


def _app_version() -> str:
    try:
        from skillforge import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "unknown"
