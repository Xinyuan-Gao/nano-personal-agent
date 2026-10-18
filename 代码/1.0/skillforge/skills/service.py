from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Mapping

from ..core import paths as core_paths
from ..core.clock import utc_now
from ..core.errors import (
    EvidenceUnavailable,
    InvalidInput,
    InvalidState,
    PermissionDenied,
    StorageError,
)
from ..core.ids import canonical_hash, new_id, sha256_hex
from .package import (
    MAIN_FILE,
    CheckFinding,
    FileEntry,
    PackageLimits,
    SkillPackage,
    build_package,
    diff_manifests,
    hard_checks,
    quality_hints,
    read_package,
)

#: 候选生命周期中仍可被基线变化置为 stale 的状态。
NON_TERMINAL_PROPOSAL_STATUSES = (
    "draft",
    "needs_clarification",
    "blocked",
    "ready",
    "evaluating",
    "eligible",
    "needs_evidence",
)

_ORIGINS = ("import", "draft", "proposal", "manual_trial", "user_edit", "rollback", "restore")
_VALIDATION_STATUSES = ("unchecked", "passed", "failed")

_BUNDLE_FORMAT = "skillforge-skill-bundle"
_BUNDLE_VERSION = 1

_SKILL_JSON_FIELDS = ("tags",)


def slugify(value: str) -> str:
    """由名称生成稳定 slug；保留中文与字母数字，其余折叠为 ``-``。"""
    text = (value or "").strip().lower()
    text = re.sub(r"[\s_/\\]+", "-", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff\-]+", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        text = "skill-" + sha256_hex(value or "skill")[:8]
    return text[:80]


class SkillService:
    def __init__(self, store, repo, blobs, events, idem, config) -> None:
        self.store = store
        self.repo = repo
        self.blobs = blobs
        self.events = events
        self.idem = idem
        self.config = config

    # ==================================================================== 导入
    def import_skill(
        self,
        source_path: str,
        *,
        scope: str = "workspace",
        workspace_id: str | None = None,
        profile_id: str = "local",
        idempotency_key: str | None = None,
        on_conflict: str = "reject",
        new_slug: str | None = None,
        actor: str = "owner",
        limits: PackageLimits | None = None,
    ) -> dict:
        """导入一个 skill 包。

        冲突策略：``reject`` 抛 ``InvalidInput``；``new_version`` 在既有身份下新增不可变版本
        ``new_slug`` 用 ``new_slug`` 建立新身份。新技能的首个版本会建立
        ``unverified=1`` 的初始生效指针，使技能立即可被选择但明确标注“效果未验证”
        。
        """
        scope, workspace_id = self._resolve_scope(scope, workspace_id)
        if on_conflict not in ("reject", "new_version", "new_slug"):
            raise InvalidInput(
                f"不支持的冲突策略: {on_conflict}",
                field_errors={"on_conflict": "unsupported"},
            )
        if on_conflict == "new_slug" and not new_slug:
            raise InvalidInput("new_slug 策略必须提供 new_slug", field_errors={"new_slug": "required"})
        source = core_paths.normalize(source_path)
        payload = {
            "source_path": str(source),
            "scope": scope,
            "workspace_id": workspace_id,
            "profile_id": profile_id,
            "on_conflict": on_conflict,
            "new_slug": new_slug,
        }
        return self.idem.run(
            "skill.import",
            idempotency_key,
            payload,
            lambda: self._import_impl(
                source, scope, workspace_id, profile_id, on_conflict, new_slug, actor, limits
            ),
        )

    def _import_impl(
        self,
        source: Path,
        scope: str,
        workspace_id: str | None,
        profile_id: str,
        on_conflict: str,
        new_slug: str | None,
        actor: str,
        limits: PackageLimits | None,
    ) -> dict:
        package = self._read_source(source, limits)
        findings = self._all_findings(package)
        blocked = [f for f in findings if f.level == "hard"]
        if blocked:
            raise self._blocked_error(findings, "硬检查未通过，导入被阻止")

        self._ensure_profile(profile_id)
        self._ensure_workspace(workspace_id, profile_id)

        desired_slug = slugify(new_slug or package.name)
        existing = None
        if on_conflict != "new_slug":
            existing = self._find_skill(profile_id, scope, workspace_id, desired_slug)
        if existing is not None and on_conflict == "reject":
            raise InvalidInput(
                f"同范围已存在同名技能（slug={desired_slug}），请选择新版本或新身份",
                field_errors={"slug": "conflict"},
            )

        self._ensure_blobs(package)
        now = utc_now()
        created = False

        if existing is not None and on_conflict == "new_version":
            skill_id = existing["id"]
            with self.store.tx() as conn:
                current = self._current_activation_row(skill_id)
                parent = current["version_id"] if current else self._latest_version_id(skill_id)
                version_id = self.create_version_from_package(
                    skill_id,
                    package,
                    origin="import",
                    parent_version_id=parent,
                    validation_status="unchecked",
                    note=f"导入新版本（来源 {source}）",
                    conn=conn,
                    created_by=actor,
                )
                self.repo.update("skill", skill_id, {"updated_at": now}, conn=conn)
                self.events.audit(
                    "skill.import_version",
                    "skill",
                    skill_id,
                    actor=actor,
                    after={"version_id": version_id, "slug": existing["slug"]},
                    reason="导入产生新版本（不自动启用）",
                    conn=conn,
                )
        else:
            if existing is not None:  # new_slug
                desired_slug = slugify(new_slug or "")
                if self._find_skill(profile_id, scope, workspace_id, desired_slug) is not None:
                    raise InvalidInput(
                        f"新身份 slug 仍然冲突: {desired_slug}",
                        field_errors={"slug": "conflict"},
                    )
            skill_id = new_id("skill")
            with self.store.tx() as conn:
                self.repo.insert(
                    "skill",
                    {
                        "id": skill_id,
                        "profile_id": profile_id,
                        "workspace_id": workspace_id,
                        "slug": desired_slug,
                        "name": package.name,
                        "description": package.description,
                        "scope": scope,
                        "status": "active",
                        "origin": "import",
                        "tags": [],
                        "created_at": now,
                        "updated_at": now,
                    },
                    conn=conn,
                )
                version_id = self.create_version_from_package(
                    skill_id,
                    package,
                    origin="import",
                    parent_version_id=None,
                    validation_status="unchecked",
                    note=f"导入初始版本（来源 {source}）",
                    conn=conn,
                    created_by=actor,
                )
                self._ensure_initial_activation(skill_id, version_id, scope=scope, origin="import", conn=conn)
                self.events.audit(
                    "skill.import",
                    "skill",
                    skill_id,
                    actor=actor,
                    after={
                        "version_id": version_id,
                        "slug": desired_slug,
                        "package_hash": package.package_hash,
                        "file_count": len(package.manifest),
                    },
                    reason=f"从 {source} 导入（只读，未执行脚本）",
                    conn=conn,
                )
            created = True

        findings = self._all_findings(package)
        return {
            "skill_id": skill_id,
            "version_id": version_id,
            "created": created,
            "slug": desired_slug,
            "scope": scope,
            "workspace_id": workspace_id,
            "package_hash": package.package_hash,
            "file_count": len(package.manifest),
            "total_bytes": package.total_bytes,
            "checks": self._summarize_findings(findings),
            "findings": [f.to_dict() for f in findings],
            "skipped": list(package.skipped),
            "source_modified": False,
            "scripts_executed": False,
        }

    def _read_source(self, source: Path, limits: PackageLimits | None) -> SkillPackage:
        resolved_limits = limits or self._limits()
        if source.is_dir():
            return read_package(source, limits=resolved_limits)
        if source.is_file():
            if source.name == MAIN_FILE:
                return read_package(source.parent, limits=resolved_limits)
            return build_package(
                {source.name: source.read_bytes()},
                lead_file=source.name,
                limits=resolved_limits,
            )
        raise InvalidInput(f"导入来源不存在: {source}", field_errors={"source": "not_found"})

    # ==================================================================== 草稿
    def create_draft(
        self,
        *,
        name: str,
        description: str,
        body: str,
        scope: str = "personal",
        workspace_id: str | None = None,
        profile_id: str = "local",
        references: Mapping[str, str | bytes] | list | None = None,
        origin: str = "draft",
        idempotency_key: str | None = None,
        metadata: Mapping | None = None,
        on_conflict: str = "reject",
        new_slug: str | None = None,
        actor: str = "owner",
        limits: PackageLimits | None = None,
    ) -> dict:
        """人工创建草稿：初始版标记效果未验证，不宣称能力提升。

        ``references`` 支持 ``{相对路径: 内容}`` 或 ``[{"path","content"}]``。
        """
        scope, workspace_id = self._resolve_scope(scope, workspace_id)
        if not (name or "").strip():
            raise InvalidInput("name 必填", field_errors={"name": "required"})
        ref_files = self._normalize_references(references)
        files = self._draft_files(name, description, body, ref_files, metadata)
        payload = {
            "name": name,
            "description": description,
            "body": body,
            "scope": scope,
            "workspace_id": workspace_id,
            "profile_id": profile_id,
            "origin": origin,
            "references": {k: sha256_hex(v) if isinstance(v, bytes) else sha256_hex(v.encode("utf-8")) for k, v in ref_files.items()},
            "metadata": dict(metadata or {}),
        }
        return self.idem.run(
            "skill.draft",
            idempotency_key,
            payload,
            lambda: self._create_draft_impl(
                name,
                description,
                scope,
                workspace_id,
                profile_id,
                files,
                origin,
                on_conflict,
                new_slug,
                actor,
                limits,
            ),
        )

    def _create_draft_impl(
        self,
        name: str,
        description: str,
        scope: str,
        workspace_id: str | None,
        profile_id: str,
        files: dict[str, bytes],
        origin: str,
        on_conflict: str,
        new_slug: str | None,
        actor: str,
        limits: PackageLimits | None,
    ) -> dict:
        if origin not in _ORIGINS:
            raise InvalidInput(f"不支持的 origin: {origin}", field_errors={"origin": "unsupported"})
        if on_conflict not in ("reject", "new_version", "new_slug"):
            raise InvalidInput(f"不支持的冲突策略: {on_conflict}", field_errors={"on_conflict": "unsupported"})
        package = build_package(files, limits=limits or self._limits())
        findings = self._all_findings(package)
        if [f for f in findings if f.level == "hard"]:
            raise self._blocked_error(findings, "草稿硬检查未通过")

        self._ensure_profile(profile_id)
        self._ensure_workspace(workspace_id, profile_id)
        slug = slugify(new_slug or name)
        existing = self._find_skill(profile_id, scope, workspace_id, slug)
        if existing is not None and on_conflict == "reject":
            raise InvalidInput(
                f"同范围已存在同名技能（slug={slug}）", field_errors={"slug": "conflict"}
            )

        self._ensure_blobs(package)
        now = utc_now()
        created = False
        if existing is not None and on_conflict == "new_version":
            skill_id = existing["id"]
            with self.store.tx() as conn:
                parent = self._latest_version_id(skill_id)
                version_id = self.create_version_from_package(
                    skill_id,
                    package,
                    origin=origin,
                    parent_version_id=parent,
                    validation_status="unchecked",
                    note="人工新版本",
                    conn=conn,
                    created_by=actor,
                )
                self.repo.update("skill", skill_id, {"updated_at": now}, conn=conn)
                self.events.audit(
                    "skill.new_version", "skill", skill_id, actor=actor,
                    after={"version_id": version_id}, reason="人工创建新版本", conn=conn,
                )
        else:
            if existing is not None:
                slug = slugify(new_slug or "")
                if self._find_skill(profile_id, scope, workspace_id, slug) is not None:
                    raise InvalidInput(f"新身份 slug 仍然冲突: {slug}", field_errors={"slug": "conflict"})
            skill_id = new_id("skill")
            with self.store.tx() as conn:
                self.repo.insert(
                    "skill",
                    {
                        "id": skill_id,
                        "profile_id": profile_id,
                        "workspace_id": workspace_id,
                        "slug": slug,
                        "name": name,
                        "description": description or "",
                        "scope": scope,
                        "status": "active",
                        "origin": origin,
                        "tags": [],
                        "created_at": now,
                        "updated_at": now,
                    },
                    conn=conn,
                )
                version_id = self.create_version_from_package(
                    skill_id,
                    package,
                    origin=origin,
                    parent_version_id=None,
                    validation_status="unchecked",
                    note="初始草稿（效果未验证）",
                    conn=conn,
                    created_by=actor,
                )
                self._ensure_initial_activation(skill_id, version_id, scope=scope, origin=origin, conn=conn)
                self.events.audit(
                    "skill.create_draft", "skill", skill_id, actor=actor,
                    after={"version_id": version_id, "origin": origin, "unverified": True},
                    reason="人工创建草稿（效果未验证）", conn=conn,
                )
            created = True

        return {
            "skill_id": skill_id,
            "version_id": version_id,
            "created": created,
            "slug": slug,
            "scope": scope,
            "workspace_id": workspace_id,
            "package_hash": package.package_hash,
            "file_count": len(package.manifest),
            "total_bytes": package.total_bytes,
            "checks": self._summarize_findings(findings),
            "findings": [f.to_dict() for f in findings],
            "skipped": list(package.skipped),
            "source_modified": False,
            "scripts_executed": False,
        }

    def _draft_files(
        self,
        name: str,
        description: str,
        body: str,
        references: dict[str, bytes],
        metadata: Mapping | None,
    ) -> dict[str, bytes]:
        lines = ["---", f"name: {_yaml_scalar(name)}", f"description: {_yaml_scalar(description)}"]
        for key, value in (metadata or {}).items():
            if key in ("name", "description"):
                continue
            lines.append(f"{key}: {_yaml_scalar(value)}")
        lines.append("---")
        text = "\n".join(lines) + "\n\n" + (body or "").rstrip() + "\n"
        files: dict[str, bytes] = {MAIN_FILE: text.encode("utf-8")}
        files.update(references)
        return files

    @staticmethod
    def _normalize_references(references) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        if not references:
            return out
        items: list[tuple[str, object]]
        if isinstance(references, Mapping):
            items = list(references.items())
        elif isinstance(references, (list, tuple)):
            items = []
            for item in references:
                if isinstance(item, Mapping):
                    items.append((str(item.get("path")), item.get("content", "")))
                else:
                    items.append((str(item), ""))
        else:
            raise InvalidInput("references 必须是 map 或 list", field_errors={"references": "invalid"})
        for rel, content in items:
            if not rel:
                raise InvalidInput("references 中的路径不能为空", field_errors={"path": "empty"})
            data = content if isinstance(content, bytes) else str(content).encode("utf-8")
            out[str(rel).replace("\\", "/")] = data
        return out

    # ==================================================================== 版本
    def create_version_from_package(
        self,
        skill_id,
        pkg: SkillPackage,
        *,
        origin: str,
        parent_version_id: str | None,
        validation_status: str,
        note: str | None = None,
        conn=None,
        proposal_id: str | None = None,
        created_by: str = "owner",
    ) -> str:
        """落一个不可变版本（含完整 file 清单）；版本创建后不再修改。

        ``conn`` 非空时复用调用方事务，保证与 activation / audit 同事务提交。
        """
        if self.repo.get("skill", skill_id) is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        if origin not in _ORIGINS:
            raise InvalidInput(f"不支持的 origin: {origin}", field_errors={"origin": "unsupported"})
        if validation_status not in _VALIDATION_STATUSES:
            raise InvalidInput(
                f"不支持的 validation_status: {validation_status}",
                field_errors={"validation_status": "unsupported"},
            )
        if parent_version_id is not None and self.repo.get("skill_version", parent_version_id) is None:
            raise InvalidInput(
                f"父版本不存在: {parent_version_id}", field_errors={"parent_version_id": "not_found"}
            )

        self._ensure_blobs(pkg)

        version_id = new_id("ver")
        count = self.repo.count("skill_version", {"skill_id": skill_id})
        label = f"0.{count + 1}.0"
        hard_codes = [f.code for f in hard_checks(pkg) if f.level == "hard"]
        detail = {
            "checks": {"hard_codes": hard_codes},
            "manifest": [entry.to_dict() for entry in pkg.manifest],
        }
        now = utc_now()
        reference_paths = set(pkg.references)

        def _write(connection) -> None:
            self.repo.insert(
                "skill_version",
                {
                    "id": version_id,
                    "skill_id": skill_id,
                    "parent_version_id": parent_version_id,
                    "version_label": label,
                    "package_hash": pkg.package_hash,
                    "origin": origin,
                    "validation_status": validation_status,
                    "validation_detail": detail,
                    "file_count": len(pkg.manifest),
                    "total_bytes": pkg.total_bytes,
                    "note": note,
                    "proposal_id": proposal_id,
                    "created_by": created_by,
                    "created_at": now,
                },
                conn=connection,
            )
            for entry in pkg.manifest:
                self.repo.insert(
                    "skill_version_file",
                    {
                        "version_id": version_id,
                        "path": entry.path,
                        "content_hash": entry.content_hash,
                        "blob_id": entry.blob_id,
                        "size": entry.size,
                        "is_executable": 1 if entry.executable else 0,
                        "is_reference": 1 if entry.path in reference_paths else 0,
                    },
                    conn=connection,
                )

        if conn is not None:
            _write(conn)
        else:
            with self.store.tx() as connection:
                _write(connection)
        return version_id

    def list_versions(self, skill_id) -> list[dict]:
        if self.repo.get("skill", skill_id) is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        rows = self.store.query(
            "SELECT * FROM skill_version WHERE skill_id = ? ORDER BY created_at ASC, rowid ASC",
            (skill_id,),
        )
        current = self._current_activation_row(skill_id)
        current_version = current["version_id"] if current else None
        return [self._version_dict(row, current_version=current_version) for row in rows]

    def get_version(self, version_id) -> dict:
        row = self.repo.get("skill_version", version_id)
        if row is None:
            raise InvalidInput(f"版本不存在: {version_id}", field_errors={"version_id": "not_found"})
        data = self._version_dict(row, current_version=self._current_version_id(row["skill_id"]))
        data["files"] = self.get_version_files(version_id)
        return data

    def get_version_files(self, version_id) -> list[dict]:
        if self.repo.get("skill_version", version_id) is None:
            raise InvalidInput(f"版本不存在: {version_id}", field_errors={"version_id": "not_found"})
        rows = self.store.query(
            "SELECT * FROM skill_version_file WHERE version_id = ? ORDER BY path ASC", (version_id,)
        )
        return [
            {
                "path": row["path"],
                "content_hash": row["content_hash"],
                "blob_id": row["blob_id"],
                "size": int(row["size"]),
                "is_executable": bool(row["is_executable"]),
                "is_reference": bool(row["is_reference"]),
            }
            for row in rows
        ]

    def read_version_file(self, version_id, rel_path) -> str:
        if self.repo.get("skill_version", version_id) is None:
            raise InvalidInput(f"版本不存在: {version_id}", field_errors={"version_id": "not_found"})
        raw = str(rel_path).replace("\\", "/")
        pure = PurePosixPath(raw)
        if not raw or pure.is_absolute() or ".." in pure.parts:
            raise PermissionDenied(f"非法文件路径: {rel_path}", field_errors={"path": "invalid"})
        normalized = "/".join(part for part in pure.parts if part not in ("", "."))
        row = self.store.query_one(
            "SELECT * FROM skill_version_file WHERE version_id = ? AND path = ?",
            (version_id, normalized),
        )
        if row is None:
            raise InvalidInput(
                f"版本 {version_id} 中不存在文件: {normalized}",
                field_errors={"path": "not_found"},
            )
        if not self.blobs.exists(row["blob_id"]):
            raise EvidenceUnavailable(f"文件内容已不可用: {normalized}（blob {row['blob_id']}）")
        return self.blobs.get_text(row["blob_id"])

    # ==================================================================== 材料化
    def materialize_version(self, version_id, dest_dir: Path) -> Path:
        """把完整包重建到目录（含 scripts 与 references，执行位保留）。"""
        files = self.get_version_files(version_id)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        for entry in files:
            target = core_paths.resolve_within(dest, entry["path"])
            if not self.blobs.exists(entry["blob_id"]):
                raise EvidenceUnavailable(
                    f"制品缺失，无法重建完整包: {entry['path']}（blob {entry['blob_id']}）"
                )
            self.blobs.write_to(entry["blob_id"], target)
            if entry["is_executable"]:
                try:
                    os.chmod(target, 0o755)
                except OSError:  # pragma: no cover
                    pass
        return dest

    def stage_version(self, version_id, dest_dir: Path | None = None) -> Path:
        """先落完整包并逐文件校验 hash；失败抛 ``StorageError``。"""
        files = self.get_version_files(version_id)
        base = Path(dest_dir) if dest_dir is not None else Path(self.config.export_dir) / "staging"
        dest = base / str(version_id)
        self.materialize_version(version_id, dest)
        for entry in files:
            target = core_paths.resolve_within(dest, entry["path"])
            if not target.is_file() or sha256_hex(target.read_bytes()) != entry["content_hash"]:
                raise StorageError(f"完整包落盘校验失败: {entry['path']}")
        return dest

    def verify_version_integrity(self, version_id) -> dict:
        """校验所有 file blob 存在且 hash 匹配。"""
        files = self.get_version_files(version_id)
        missing: list[str] = []
        mismatched: list[str] = []
        for entry in files:
            if not self.blobs.exists(entry["blob_id"]):
                missing.append(entry["path"])
                continue
            if not self.blobs.verify(entry["blob_id"]) or not self.blobs.verify(entry["content_hash"]):
                mismatched.append(entry["path"])
        return {
            "ok": not missing and not mismatched,
            "missing": missing,
            "mismatched": mismatched,
            "file_count": len(files),
        }

    def version_package(self, version_id) -> SkillPackage:
        """从制品库重建内存包（用于候选包再检查与人工试用）。"""
        files = self.get_version_files(version_id)
        if not files:
            raise InvalidState(f"版本没有文件清单: {version_id}")
        mapping: dict[str, bytes] = {}
        for entry in files:
            if not self.blobs.exists(entry["blob_id"]):
                raise EvidenceUnavailable(f"制品缺失: {entry['path']}（blob {entry['blob_id']}）")
            mapping[entry["path"]] = self.blobs.get_bytes(entry["blob_id"])
        lead = MAIN_FILE if MAIN_FILE in mapping else files[0]["path"]
        return build_package(mapping, lead_file=lead)

    def has_scripts(self, version_id) -> bool:
        return any(entry["is_executable"] for entry in self.get_version_files(version_id)) or any(
            entry["path"].endswith(
                (".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1")
            )
            for entry in self.get_version_files(version_id)
        )

    # ==================================================================== 查询
    def list_skills(
        self,
        *,
        workspace_id: str | None = None,
        query: str | None = None,
        include_archived: bool = True,
        profile_id: str = "local",
    ) -> list[dict]:
        """技能列表（工作区同名优先于个人范围，并暴露 ``shadowed``）。"""
        rows = self.repo.list("skill", {"profile_id": profile_id}, order="created_at ASC", limit=5000)
        if workspace_id:
            rows = [
                row
                for row in rows
                if row["scope"] == "personal" or row["workspace_id"] == workspace_id
            ]
        if not include_archived:
            rows = [row for row in rows if row["status"] != "archived"]
        if query:
            needle = query.lower()
            rows = [
                row
                for row in rows
                if needle in (row["slug"] or "").lower()
                or needle in (row["name"] or "").lower()
                or needle in (row["description"] or "").lower()
            ]

        currents = self._current_activation_map()
        shadowed_ids: dict[str, str] = {}
        if workspace_id:
            for row in rows:
                if row["scope"] == "workspace" and row["workspace_id"] == workspace_id and row["status"] == "active":
                    shadowed_ids[row["slug"]] = row["id"]

        out: list[dict] = []
        for row in rows:
            current = currents.get(row["id"])
            out.append(
                {
                    "id": row["id"],
                    "skill_id": row["id"],
                    "profile_id": row["profile_id"],
                    "workspace_id": row["workspace_id"],
                    "slug": row["slug"],
                    "name": row["name"],
                    "description": row["description"],
                    "scope": row["scope"],
                    "status": row["status"],
                    "origin": row["origin"],
                    "tags": _loads(row["tags"], []),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "current_version_id": current["version_id"] if current else None,
                    "current_revision": int(current["revision"]) if current else None,
                    "unverified": bool(current["unverified"]) if current else None,
                    "shadowed": bool(
                        row["scope"] == "personal"
                        and workspace_id
                        and row["slug"] in shadowed_ids
                        and shadowed_ids[row["slug"]] != row["id"]
                    ),
                    "shadowed_by": (
                        shadowed_ids.get(row["slug"])
                        if row["scope"] == "personal" and workspace_id and row["slug"] in shadowed_ids
                        else None
                    ),
                }
            )
        out.sort(key=lambda item: (0 if item["scope"] == "workspace" else 1, item["name"].lower(), item["id"]))
        return out

    def get_skill(self, skill_id) -> dict:
        row = self.repo.get("skill", skill_id)
        if row is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        current = self._current_activation_row(skill_id)
        data = {
            "id": row["id"],
            "skill_id": row["id"],
            "profile_id": row["profile_id"],
            "workspace_id": row["workspace_id"],
            "slug": row["slug"],
            "name": row["name"],
            "description": row["description"],
            "scope": row["scope"],
            "status": row["status"],
            "origin": row["origin"],
            "tags": _loads(row["tags"], []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "current_version_id": current["version_id"] if current else None,
            "current_revision": int(current["revision"]) if current else None,
            "unverified": bool(current["unverified"]) if current else None,
            "version_count": self.repo.count("skill_version", {"skill_id": skill_id}),
        }
        data["versions"] = [
            {"version_id": v["version_id"], "version_label": v["version_label"], "created_at": v["created_at"]}
            for v in self.list_versions(skill_id)
        ]
        return data

    def find_skill(self, skill_id) -> dict | None:
        row = self.repo.get("skill", skill_id)
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def current_activation(self, skill_id) -> dict | None:
        """当前生效指针（revision 最大者）；无版本时返回 ``None``。"""
        row = self._current_activation_row(skill_id)
        if row is None:
            return None
        return self._activation_dict(row)

    # ==================================================================== 导出 / 导入
    def export_skill(self, skill_id, dest_dir: str, *, include_versions: bool = True) -> dict:
        """导出一个技能的可搬运 bundle（元数据 + 完整包 + 清单 hash）。"""
        skill = self.repo.get("skill", skill_id)
        if skill is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        dest = Path(dest_dir).expanduser()
        if dest.exists() and any(dest.iterdir()):
            if not (dest / "bundle.json").exists():
                raise InvalidInput(
                    f"导出目录非空且不是 skillforge bundle: {dest}",
                    field_errors={"dest": "not_empty"},
                )
        dest.mkdir(parents=True, exist_ok=True)

        versions = self.list_versions(skill_id)
        if not include_versions:
            current = self._current_activation_row(skill_id)
            versions = [v for v in versions if current and v["version_id"] == current["version_id"]]

        version_payloads: list[dict] = []
        for version in versions:
            version_id = version["version_id"]
            files = self.get_version_files(version_id)
            for entry in files:
                target = core_paths.resolve_within(dest, f"versions/{version_id}/{entry['path']}")
                if not self.blobs.exists(entry["blob_id"]):
                    raise EvidenceUnavailable(f"制品缺失，无法导出: {entry['path']}")
                self.blobs.write_to(entry["blob_id"], target)
                if entry["is_executable"]:
                    try:
                        os.chmod(target, 0o755)
                    except OSError:  # pragma: no cover
                        pass
            version_payloads.append(
                {
                    "version_id": version_id,
                    "version_label": version["version_label"],
                    "parent_version_id": version["parent_version_id"],
                    "package_hash": version["package_hash"],
                    "origin": version["origin"],
                    "validation_status": version["validation_status"],
                    "note": version["note"],
                    "created_at": version["created_at"],
                    "file_count": version["file_count"],
                    "total_bytes": version["total_bytes"],
                    "files": files,
                }
            )

        current = self._current_activation_row(skill_id)
        skill_payload = {
            "slug": skill["slug"],
            "name": skill["name"],
            "description": skill["description"],
            "scope": skill["scope"],
            "workspace_id": skill["workspace_id"],
            "profile_id": skill["profile_id"],
            "tags": _loads(skill["tags"], []),
            "current_version_id": current["version_id"] if current else None,
        }
        manifest_hash = canonical_hash(
            {
                "skill": {k: skill_payload[k] for k in ("slug", "name", "scope")},
                "versions": [
                    {"version_id": v["version_id"], "package_hash": v["package_hash"]}
                    for v in version_payloads
                ],
            }
        )
        bundle = {
            "format": _BUNDLE_FORMAT,
            "format_version": _BUNDLE_VERSION,
            "skill": skill_payload,
            "versions": version_payloads,
            "manifest_hash": manifest_hash,
        }
        bundle_path = dest / "bundle.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "skill_id": skill_id,
            "dest": str(dest),
            "bundle_path": str(bundle_path),
            "manifest_hash": manifest_hash,
            "file_count": sum(v["file_count"] for v in version_payloads),
            "versions": [
                {"version_id": v["version_id"], "package_hash": v["package_hash"], "file_count": v["file_count"]}
                for v in version_payloads
            ],
        }

    def import_export_bundle(
        self,
        bundle_dir: str,
        *,
        scope: str,
        workspace_id: str | None,
        idempotency_key: str | None = None,
        actor: str = "owner",
    ) -> dict:
        """再导入 bundle（导出再导入 hash 一致）。

        身份已存在时按不可变版本追加：已存在同 ``package_hash`` 的版本会跳过
        因此同一 bundle 重复导入是幂等的。
        """
        scope, workspace_id = self._resolve_scope(scope, workspace_id)
        base = Path(bundle_dir).expanduser()
        payload = {"bundle_dir": str(base), "scope": scope, "workspace_id": workspace_id}
        return self.idem.run(
            "skill.import_bundle",
            idempotency_key,
            payload,
            lambda: self._import_bundle_impl(base, scope, workspace_id, actor),
        )

    def _import_bundle_impl(self, base: Path, scope: str, workspace_id: str | None, actor: str) -> dict:
        bundle_path = base / "bundle.json" if base.is_dir() else base
        if not bundle_path.is_file():
            raise InvalidInput(f"bundle.json 不存在: {bundle_path}", field_errors={"bundle": "not_found"})
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise InvalidInput(f"bundle.json 无法解析: {exc}", field_errors={"bundle": "invalid"}) from exc
        if bundle.get("format") != _BUNDLE_FORMAT:
            raise InvalidInput("不是 skillforge skill bundle", field_errors={"format": "unsupported"})

        root = bundle_path.parent
        packages: list[tuple[dict, SkillPackage]] = []
        for version in bundle.get("versions") or []:
            files: dict[str, bytes] = {}
            for entry in version.get("files") or []:
                rel = str(entry["path"])
                target = core_paths.resolve_within(root, f"versions/{version['version_id']}/{rel}")
                if not target.is_file():
                    raise StorageError(f"导出包缺少文件: {rel}")
                data = target.read_bytes()
                if sha256_hex(data) != entry["content_hash"]:
                    raise StorageError(f"导出包文件 hash 不一致: {rel}")
                files[rel] = data
            package = build_package(files)
            expected = version.get("package_hash")
            if expected and package.package_hash != expected:
                raise StorageError(
                    f"导出包 package_hash 不一致: {version['version_id']}"
                )
            packages.append((version, package))
        if not packages:
            raise InvalidInput("bundle 内没有版本", field_errors={"versions": "empty"})

        skill_payload = bundle.get("skill") or {}
        name = str(skill_payload.get("name") or packages[-1][1].name)
        slug = slugify(skill_payload.get("slug") or name)
        self._ensure_profile("local")
        self._ensure_workspace(workspace_id, "local")
        existing = self._find_skill("local", scope, workspace_id, slug)
        created = False
        version_ids: list[str] = []

        if existing is None:
            skill_id = new_id("skill")
            with self.store.tx() as conn:
                self.repo.insert(
                    "skill",
                    {
                        "id": skill_id,
                        "profile_id": "local",
                        "workspace_id": workspace_id,
                        "slug": slug,
                        "name": name,
                        "description": str(skill_payload.get("description") or packages[-1][1].description),
                        "scope": scope,
                        "status": "active",
                        "origin": "import",
                        "tags": skill_payload.get("tags") or [],
                        "created_at": utc_now(),
                        "updated_at": utc_now(),
                    },
                    conn=conn,
                )
                parent = None
                for version, package in packages:
                    self._ensure_blobs(package)
                    version_id = self.create_version_from_package(
                        skill_id,
                        package,
                        origin="import",
                        parent_version_id=parent,
                        validation_status=version.get("validation_status") or "unchecked",
                        note=version.get("note") or f"bundle 再导入 {version.get('version_id')}",
                        conn=conn,
                        created_by=actor,
                    )
                    parent = version_id
                    version_ids.append(version_id)
                self._ensure_initial_activation(skill_id, version_ids[-1], scope=scope, origin="import", conn=conn)
                self.events.audit(
                    "skill.import_bundle", "skill", skill_id, actor=actor,
                    after={"versions": version_ids, "manifest_hash": bundle.get("manifest_hash")},
                    reason=f"从 bundle 再导入: {bundle_path}", conn=conn,
                )
            created = True
        else:
            skill_id = existing["id"]
            known = {
                row["package_hash"]
                for row in self.store.query(
                    "SELECT package_hash FROM skill_version WHERE skill_id = ?", (skill_id,)
                )
            }
            with self.store.tx() as conn:
                parent = self._latest_version_id(skill_id)
                for version, package in packages:
                    if package.package_hash in known:
                        version_ids.append(
                            self.store.query_one(
                                "SELECT id FROM skill_version WHERE skill_id = ? AND package_hash = ?",
                                (skill_id, package.package_hash),
                            )["id"]
                        )
                        continue
                    self._ensure_blobs(package)
                    version_id = self.create_version_from_package(
                        skill_id,
                        package,
                        origin="import",
                        parent_version_id=parent,
                        validation_status=version.get("validation_status") or "unchecked",
                        note=version.get("note") or f"bundle 再导入 {version.get('version_id')}",
                        conn=conn,
                        created_by=actor,
                    )
                    known.add(package.package_hash)
                    parent = version_id
                    version_ids.append(version_id)
                self.repo.update("skill", skill_id, {"updated_at": utc_now()}, conn=conn)
                self.events.audit(
                    "skill.import_bundle", "skill", skill_id, actor=actor,
                    after={"versions": version_ids}, reason="bundle 追加版本", conn=conn,
                )

        primary = packages[-1][1]
        return {
            "skill_id": skill_id,
            "version_id": version_ids[-1] if version_ids else None,
            "version_ids": version_ids,
            "created": created,
            "slug": slug,
            "package_hash": primary.package_hash,
            "manifest_hash": bundle.get("manifest_hash"),
            "checks": self._summarize_findings(self._all_findings(primary)),
            "findings": [f.to_dict() for f in self._all_findings(primary)],
            "skipped": list(primary.skipped),
            "source_modified": False,
            "scripts_executed": False,
        }

    # ==================================================================== 归档 / 恢复
    def archive(self, skill_id, *, actor: str = "owner") -> dict:
        """归档：不参与新任务选择，历史证据与版本全部保留。"""
        row = self.repo.get("skill", skill_id)
        if row is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        now = utc_now()
        with self.store.tx() as conn:
            self.repo.update("skill", skill_id, {"status": "archived", "updated_at": now}, conn=conn)
            self.events.audit(
                "skill.archive", "skill", skill_id, actor=actor,
                before={"status": row["status"]}, after={"status": "archived"},
                reason="归档不删除历史证据", conn=conn,
            )
        return {
            "skill_id": skill_id,
            "status": "archived",
            "archived_at": now,
            "versions_kept": self.repo.count("skill_version", {"skill_id": skill_id}),
            "history_kept": self.repo.count("activation", {"skill_id": skill_id}),
        }

    def restore(self, skill_id, *, actor: str = "owner") -> dict:
        """恢复：重新检查当前版本包完整性，报告缺失制品。"""
        row = self.repo.get("skill", skill_id)
        if row is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        current = self._current_activation_row(skill_id)
        integrity = self.verify_version_integrity(current["version_id"]) if current else None
        now = utc_now()
        with self.store.tx() as conn:
            self.repo.update("skill", skill_id, {"status": "active", "updated_at": now}, conn=conn)
            self.events.audit(
                "skill.restore", "skill", skill_id, actor=actor,
                before={"status": row["status"]}, after={"status": "active", "integrity": integrity},
                reason="恢复后重新检查权限与包完整性", conn=conn,
            )
        return {
            "skill_id": skill_id,
            "status": "active",
            "restored_at": now,
            "version_id": current["version_id"] if current else None,
            "integrity": integrity,
        }

    def mark_stale_proposals(
        self,
        skill_id,
        *,
        reason: str,
        conn=None,
        except_proposal_ids: list[str] | None = None,
    ) -> list[str]:
        """把该技能未终态的候选标记为 stale。

        已 ``activated`` / ``rejected`` / ``superseded`` / ``stale`` 的候选保持原状
        ``except_proposal_ids`` 用于手工试用时保留源候选结论。
        """
        placeholders = ", ".join("?" for _ in NON_TERMINAL_PROPOSAL_STATUSES)
        rows = self.store.query(
            f"SELECT id FROM proposal WHERE skill_id = ? AND status IN ({placeholders})",
            (skill_id, *NON_TERMINAL_PROPOSAL_STATUSES),
        )
        skip = set(except_proposal_ids or [])
        ids = [row["id"] for row in rows if row["id"] not in skip]
        if not ids:
            return []
        now = utc_now()
        marked: list[str] = []
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start : chunk_start + 500]
            marks = ", ".join("?" for _ in chunk)
            sql = (
                f"UPDATE proposal SET status = 'stale', stale_reason = ?, updated_at = ? "
                f"WHERE id IN ({marks})"
            )
            params = (reason, now, *chunk)
            if conn is not None:
                conn.execute(sql, params)
            else:
                self.store.execute(sql, params)
            marked.extend(chunk)
        return marked

    # ==================================================================== 加载记录
    def list_loads(self, run_id: str, *, kinds: tuple[str, ...] | None = None) -> list[dict]:
        sql = "SELECT * FROM skill_load WHERE run_id = ?"
        params: list = [run_id]
        if kinds:
            sql += " AND load_kind IN (" + ", ".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        sql += " ORDER BY created_at ASC, rowid ASC"
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "skill_id": row["skill_id"],
                "version_id": row["version_id"],
                "file_path": row["file_path"],
                "content_hash": row["content_hash"],
                "load_kind": row["load_kind"],
                "chars": int(row["chars"]),
                "load_status": row["load_status"],
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in self.store.query(sql, params)
        ]

    def learning_source_loads(self, run_id: str) -> list[dict]:
        """可作为学习来源的加载记录：只有真正注入上下文的全文/引用。

        仅看索引（``load_kind=index``）或未加载的记录不得归因成任务执行方法。
        """
        return [
            row
            for row in self.list_loads(run_id, kinds=("full", "reference"))
            if row["load_status"] == "loaded"
        ]

    # ==================================================================== 内部
    def _all_findings(self, package: SkillPackage) -> list[CheckFinding]:
        findings = list(hard_checks(package))
        findings.extend(quality_hints(package))
        seen: set[tuple] = set()
        out: list[CheckFinding] = []
        for finding in findings:
            key = (finding.level, finding.code, finding.message, finding.locator)
            if key in seen:
                continue
            seen.add(key)
            out.append(finding)
        return out

    @staticmethod
    def _summarize_findings(findings: list[CheckFinding]) -> dict:
        buckets = {"hard": [], "hint": [], "risk": [], "skipped": []}
        for finding in findings:
            buckets.setdefault(finding.level, []).append(finding.to_dict())
        return {
            "passed": not buckets.get("hard"),
            "counts": {key: len(value) for key, value in buckets.items()},
            "hard": buckets.get("hard", []),
            "hint": buckets.get("hint", []),
            "risk": buckets.get("risk", []),
            "skipped": buckets.get("skipped", []),
        }

    @staticmethod
    def _blocked_error(findings: list[CheckFinding], message: str) -> InvalidInput:
        hard = [f for f in findings if f.level == "hard"]
        error = InvalidInput(
            message,
            field_errors={f.code: f"{f.message} @ {f.locator}" if f.locator else f.message for f in hard},
        )
        error.findings = [f.to_dict() for f in findings]
        error.codes = [f.code for f in hard]
        return error

    def _limits(self) -> PackageLimits:
        extra = getattr(self.config, "extra", None) or {}
        configured = extra.get("package_limits")
        if isinstance(configured, dict):
            return PackageLimits.from_dict(configured)
        return PackageLimits()

    @staticmethod
    def _resolve_scope(scope: str, workspace_id: str | None) -> tuple[str, str | None]:
        if scope not in ("personal", "workspace"):
            raise InvalidInput(f"不支持的 scope: {scope}", field_errors={"scope": "unsupported"})
        if scope == "personal":
            return scope, None
        if not workspace_id:
            raise InvalidInput("workspace 范围必须提供 workspace_id", field_errors={"workspace_id": "required"})
        return scope, workspace_id

    def _ensure_profile(self, profile_id: str) -> None:
        if self.repo.get("profile", profile_id) is not None:
            return
        now = utc_now()
        self.repo.insert(
            "profile",
            {
                "id": profile_id,
                "name": profile_id,
                "kind": "local_owner",
                "status": "active",
                "created_at": now,
                "updated_at": now,
            },
        )

    def _ensure_workspace(self, workspace_id: str | None, profile_id: str) -> None:
        if not workspace_id:
            return
        row = self.repo.get("workspace", workspace_id)
        if row is None:
            raise InvalidInput(
                f"工作区不存在: {workspace_id}", field_errors={"workspace_id": "not_found"}
            )
        if row["profile_id"] != profile_id:
            raise PermissionDenied(
                f"工作区 {workspace_id} 不属于 profile {profile_id}",
                field_errors={"workspace_id": "wrong_profile"},
            )

    def _find_skill(self, profile_id: str, scope: str, workspace_id: str | None, slug: str):
        return self.store.query_one(
            "SELECT * FROM skill WHERE profile_id = ? AND scope = ? AND IFNULL(workspace_id, '') = ? AND slug = ?",
            (profile_id, scope, workspace_id or "", slug),
        )

    def _ensure_blobs(self, package: SkillPackage) -> None:
        for entry in package.manifest:
            target = self.blobs.path_for(entry.content_hash)
            if target.exists() and not self.blobs.verify(entry.content_hash):
                try:  # 修复损坏制品：制品是内容寻址的，重写是安全操作
                    target.unlink()
                except OSError:  # pragma: no cover
                    pass
            data = (
                package.raw_files[entry.path]
                if package.raw_files is not None and entry.path in package.raw_files
                else (Path(package.root) / entry.path).read_bytes()
            )
            digest = self.blobs.put_bytes(data)
            if digest != entry.content_hash:
                raise StorageError(f"写入制品后 hash 不一致: {entry.path}")

    def _ensure_initial_activation(
        self, skill_id: str, version_id: str, *, scope: str, origin: str, conn
    ) -> str | None:
        """新技能的首个版本建立初始生效指针，显式标注效果未验证。"""
        if self._current_activation_row(skill_id) is not None:
            return None
        activation_id = new_id("act")
        now = utc_now()
        record = {
            "id": activation_id,
            "skill_id": skill_id,
            "version_id": version_id,
            "scope": scope,
            "revision": 1,
            "origin": origin,
            "proposal_id": None,
            "report_id": None,
            "unverified": 1,
            "reason": "初始版本启用（效果未验证，未通过对照评测）",
            "actor": "owner",
            "created_at": now,
        }
        self.repo.insert("activation", record, conn=conn)
        self.events.audit(
            "activation.activate",
            "skill",
            skill_id,
            after={"activation_id": activation_id, "version_id": version_id, "revision": 1, "unverified": True},
            reason="初始版本启用（效果未验证）",
            conn=conn,
        )
        return activation_id

    def _current_activation_row(self, skill_id: str):
        return self.store.query_one(
            "SELECT * FROM activation WHERE skill_id = ? ORDER BY revision DESC LIMIT 1", (skill_id,)
        )

    def _current_version_id(self, skill_id: str) -> str | None:
        row = self._current_activation_row(skill_id)
        return row["version_id"] if row else None

    def _current_activation_map(self) -> dict[str, dict]:
        rows = self.store.query(
            "SELECT a.* FROM activation a JOIN "
            "(SELECT skill_id, MAX(revision) AS r FROM activation GROUP BY skill_id) m "
            "ON a.skill_id = m.skill_id AND a.revision = m.r"
        )
        return {row["skill_id"]: {key: row[key] for key in row.keys()} for row in rows}

    def _latest_version_id(self, skill_id: str) -> str | None:
        row = self.store.query_one(
            "SELECT id FROM skill_version WHERE skill_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (skill_id,),
        )
        return row["id"] if row else None

    def _activation_dict(self, row) -> dict:
        version = self.repo.get("skill_version", row["version_id"])
        return {
            "activation_id": row["id"],
            "skill_id": row["skill_id"],
            "version_id": row["version_id"],
            "scope": row["scope"],
            "revision": int(row["revision"]),
            "origin": row["origin"],
            "proposal_id": row["proposal_id"],
            "report_id": row["report_id"],
            "unverified": bool(row["unverified"]),
            "reason": row["reason"],
            "actor": row["actor"],
            "created_at": row["created_at"],
            "package_hash": version["package_hash"] if version else None,
            "version_label": version["version_label"] if version else None,
            "validation_status": version["validation_status"] if version else None,
            "is_current": True,
        }

    @staticmethod
    def _version_dict(row, *, current_version: str | None) -> dict:
        return {
            "version_id": row["id"],
            "skill_id": row["skill_id"],
            "parent_version_id": row["parent_version_id"],
            "version_label": row["version_label"],
            "package_hash": row["package_hash"],
            "origin": row["origin"],
            "validation_status": row["validation_status"],
            "validation_detail": _loads(row["validation_detail"], {}),
            "file_count": int(row["file_count"]),
            "total_bytes": int(row["total_bytes"]),
            "note": row["note"],
            "proposal_id": row["proposal_id"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "is_current": row["id"] == current_version,
        }

    def diff_versions(self, version_id_a, version_id_b) -> list[dict]:
        """两个版本的逐文件差异。"""
        files_a = [FileEntry(**{k: v for k, v in entry.items() if k in ("path", "content_hash", "size", "blob_id", "executable")}) for entry in self.get_version_files(version_id_a)]
        files_b = [FileEntry(**{k: v for k, v in entry.items() if k in ("path", "content_hash", "size", "blob_id", "executable")}) for entry in self.get_version_files(version_id_b)]
        return diff_manifests(files_a, files_b)


def _loads(text, default):
    if text is None:
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _yaml_scalar(value) -> str:
    """把标量写成 YAML 子集可解析的形式（复杂值双引号包裹）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_yaml_scalar(v)}" for k, v in value.items()) + "}"
    text = str(value)
    if text == "":
        return '""'
    if re.search(r"[:#\[\]{}]", text) or text.strip() != text:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text
