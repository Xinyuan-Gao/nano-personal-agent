from __future__ import annotations

import json
from typing import Any

from ..core.clock import utc_now
from ..core.errors import InvalidInput
from ..core.ids import canonical_hash, new_id
from ..core.repository import row_to_dict
from .verdict import DEFAULT_THRESHOLDS, normalize_thresholds
from .verifiers import (
    DETERMINISTIC_CHECKS,
    VERIFIER_TYPES,
    checks_from_criteria,
    describe_checks,
    fixture_hash,
    normalize_check,
    normalize_verifier,
)

#: 用例角色
ROLES = ("regression", "holdout")
#: 评测目标
OBJECTIVES = ("quality", "efficiency")
#: 评测集状态
SUITE_STATUSES = ("draft", "frozen")
#: 默认重复次数
DEFAULT_REPETITIONS = 3

_SUITE_JSON_FIELDS = ("rubric", "critical_checks", "thresholds")
_CASE_JSON_FIELDS = ("input_files", "completion_criteria", "verifier", "expected_answers")


# --------------------------------------------------------------------- 归一化
def normalize_rubric(rubric) -> dict:
    """归一 rubric：维度权重合计必须为 100%，且必须有通过阈值。"""
    if not isinstance(rubric, dict) or not rubric:
        raise InvalidInput("rubric 必须是非空对象", field_errors={"rubric": "required"})
    raw_dims = rubric.get("dimensions")
    if isinstance(raw_dims, dict):
        raw_dims = [{"name": k, "weight": v} for k, v in raw_dims.items()]
    if not isinstance(raw_dims, list) or not raw_dims:
        raise InvalidInput(
            "rubric 必须包含 dimensions（维度与权重）", field_errors={"rubric.dimensions": "required"}
        )
    dimensions = []
    for index, item in enumerate(raw_dims):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise InvalidInput(
                f"rubric 维度 #{index + 1} 必须是对象", field_errors={"rubric.dimensions": "invalid"}
            )
        name = str(item.get("name") or item.get("dimension") or "").strip()
        if not name:
            raise InvalidInput(
                f"rubric 维度 #{index + 1} 缺少名称", field_errors={"rubric.dimensions": "invalid"}
            )
        try:
            weight = float(item.get("weight"))
        except (TypeError, ValueError):
            raise InvalidInput(
                f"rubric 维度 {name} 的权重必须是数字", field_errors={"rubric.dimensions": "invalid"}
            ) from None
        if weight <= 0:
            raise InvalidInput(
                f"rubric 维度 {name} 的权重必须大于 0", field_errors={"rubric.dimensions": "invalid"}
            )
        dimensions.append(
            {
                "name": name,
                "weight": weight,
                "description": str(item.get("description") or item.get("criteria") or ""),
            }
        )
    names = [d["name"] for d in dimensions]
    if len(set(names)) != len(names):
        raise InvalidInput("rubric 维度名称不能重复", field_errors={"rubric.dimensions": "duplicate"})
    total = round(sum(d["weight"] for d in dimensions), 6)
    if abs(total - 100.0) > 1e-6:
        raise InvalidInput(
            f"rubric 维度权重合计必须为 100%，当前为 {total}",
            field_errors={"rubric.dimensions": "weight_sum"},
        )
    threshold = rubric.get("pass_threshold", rubric.get("threshold"))
    if threshold is None:
        raise InvalidInput(
            "rubric 必须包含 pass_threshold（通过阈值）",
            field_errors={"rubric.pass_threshold": "required"},
        )
    try:
        pass_threshold = float(threshold)
    except (TypeError, ValueError):
        raise InvalidInput(
            "rubric.pass_threshold 必须是数字", field_errors={"rubric.pass_threshold": "invalid"}
        ) from None
    if not 0 <= pass_threshold <= 100:
        raise InvalidInput(
            "rubric.pass_threshold 必须在 0～100 之间",
            field_errors={"rubric.pass_threshold": "invalid"},
        )
    return {
        "dimensions": dimensions,
        "weight_total": 100.0,
        "pass_threshold": pass_threshold,
        "notes": str(rubric.get("notes") or rubric.get("description") or ""),
    }


def normalize_critical_checks(critical_checks) -> list[dict]:
    """套件级 critical 检查：归一为检查项，critical 强制为真。"""
    if critical_checks is None:
        return []
    if isinstance(critical_checks, dict):
        critical_checks = [critical_checks]
    if not isinstance(critical_checks, (list, tuple)):
        raise InvalidInput(
            "critical_checks 必须是列表", field_errors={"critical_checks": "invalid"}
        )
    out = []
    for index, item in enumerate(critical_checks):
        spec = normalize_check(item, index)
        spec["critical"] = True
        out.append(spec)
    names = [c["name"] for c in out]
    if len(set(names)) != len(names):
        raise InvalidInput("critical_checks 名称不能重复", field_errors={"critical_checks": "duplicate"})
    return out


def normalize_criteria(criteria) -> list[dict]:
    """完成条件：至少一项有可判定标准；critical 标识必须明确。"""
    if criteria is None:
        raise InvalidInput(
            "completion_criteria 不能为空：至少需要一项可判定标准",
            field_errors={"completion_criteria": "required"},
        )
    if isinstance(criteria, str):
        criteria = [criteria]
    if isinstance(criteria, dict):
        criteria = [criteria]
    if not isinstance(criteria, (list, tuple)) or not criteria:
        raise InvalidInput(
            "completion_criteria 不能为空：至少需要一项可判定标准",
            field_errors={"completion_criteria": "required"},
        )
    out = []
    for index, item in enumerate(criteria):
        if isinstance(item, str):
            out.append(
                {
                    "name": item.strip(),
                    "criterion": item.strip(),
                    "critical": False,
                    "decidable": False,
                }
            )
            continue
        if not isinstance(item, dict):
            raise InvalidInput(
                f"完成条件 #{index + 1} 必须是对象或文本",
                field_errors={"completion_criteria": "invalid"},
            )
        name = str(item.get("name") or item.get("criterion") or item.get("check") or "").strip()
        if not name:
            raise InvalidInput(
                f"完成条件 #{index + 1} 缺少名称",
                field_errors={"completion_criteria": "invalid"},
            )
        kind = item.get("check") or item.get("type") or item.get("kind")
        decidable = isinstance(kind, str) and kind.strip() in DETERMINISTIC_CHECKS
        entry = {
            "name": name,
            "criterion": str(item.get("criterion") or item.get("description") or name),
            "critical": bool(item.get("critical", False)),
            "critical_declared": "critical" in item,
            "decidable": decidable,
        }
        if decidable:
            entry["check"] = kind.strip()
            entry.update(
                {k: v for k, v in item.items()
                 if k not in ("name", "criterion", "description", "critical", "check", "type", "kind")}
            )
        out.append(entry)
    return out


def normalize_input_files(input_files) -> list[dict]:
    """输入夹具：固定 hash 的本地文件快照；不携带生产凭据。"""
    if input_files is None:
        return []
    if isinstance(input_files, dict):
        input_files = [{"path": k, "content": v} for k, v in input_files.items()]
    if not isinstance(input_files, (list, tuple)):
        raise InvalidInput("input_files 必须是对象或列表", field_errors={"input_files": "invalid"})
    out = []
    for index, item in enumerate(input_files):
        if isinstance(item, str):
            item = {"path": item, "content": ""}
        if not isinstance(item, dict):
            raise InvalidInput(
                f"输入夹具 #{index + 1} 必须是对象", field_errors={"input_files": "invalid"}
            )
        path = str(item.get("path") or "").replace("\\", "/").lstrip("/")
        if not path or ".." in path.split("/"):
            raise InvalidInput(
                f"输入夹具 #{index + 1} 路径非法", field_errors={"input_files": "invalid"}
            )
        content = item.get("content")
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        out.append({"path": path, "content": "" if content is None else str(content)})
    paths = [i["path"] for i in out]
    if len(set(paths)) != len(paths):
        raise InvalidInput("输入夹具路径不能重复", field_errors={"input_files": "duplicate"})
    return out


def normalize_case(raw: dict, index: int, *, rubric: dict, critical_checks: list[dict]) -> dict:
    """归一单个用例，并校验「至少一项可判定标准」。"""
    if not isinstance(raw, dict):
        raise InvalidInput(f"用例 #{index + 1} 必须是对象", field_errors={"cases": "invalid"})
    name = str(raw.get("name") or raw.get("title") or f"case_{index + 1}").strip()
    prompt = str(raw.get("prompt") or raw.get("request") or "").strip()
    if not prompt:
        raise InvalidInput(
            f"用例 {name} 缺少 prompt（任务输入）", field_errors={"cases.prompt": "required"}
        )
    role = str(raw.get("role") or "regression").strip().lower()
    if role not in ROLES:
        raise InvalidInput(
            f"用例 {name} 的 role 必须是 regression/holdout",
            field_errors={"cases.role": "unsupported"},
        )
    criteria = normalize_criteria(raw.get("completion_criteria") or raw.get("criteria"))
    verifier = normalize_verifier(raw.get("verifier") or {})
    input_files = normalize_input_files(raw.get("input_files") or raw.get("fixtures"))
    expected = raw.get("expected_answers")
    if expected is None:
        expected = raw.get("expected") or {}
    if not isinstance(expected, dict):
        raise InvalidInput(
            f"用例 {name} 的 expected_answers 必须是对象",
            field_errors={"cases.expected_answers": "invalid"},
        )

    checks = list(verifier["checks"]) + checks_from_criteria(criteria, verifier)
    decidable = _decidable_count(verifier, checks, critical_checks)
    if decidable < 1:
        raise InvalidInput(
            f"用例 {name} 的完成条件缺少可判定标准：关键事实/文件存在/必需章节必须由确定性检查覆盖",
            field_errors={"cases.completion_criteria": "undecidable"},
        )
    if not _critical_declared(criteria, checks, critical_checks):
        raise InvalidInput(
            f"用例 {name} 未明确 critical 检查标识",
            field_errors={"cases.completion_criteria": "critical_undeclared"},
        )

    repetitions = raw.get("repetitions")
    if repetitions is not None:
        try:
            repetitions = int(repetitions)
        except (TypeError, ValueError):
            raise InvalidInput(
                f"用例 {name} 的 repetitions 必须是整数",
                field_errors={"cases.repetitions": "invalid"},
            ) from None
        if repetitions < 1:
            raise InvalidInput(
                f"用例 {name} 的 repetitions 必须 ≥ 1",
                field_errors={"cases.repetitions": "invalid"},
            )
    budget = raw.get("budget")
    if budget is not None and not isinstance(budget, dict):
        raise InvalidInput(
            f"用例 {name} 的 budget 必须是对象", field_errors={"cases.budget": "invalid"}
        )

    return {
        "id": new_id("case"),
        "name": name,
        "prompt": prompt,
        "role": role,
        "input_files": input_files,
        "fixture_hash": fixture_hash(input_files),
        "rubric_hash": canonical_hash(rubric),
        "completion_criteria": criteria,
        "verifier": verifier,
        "expected_answers": dict(expected),
        "repetitions": repetitions,
        "budget": dict(budget or {}),
        "critical_declared": True,
    }


def _decidable_count(verifier: dict, checks: list[dict], critical_checks: list[dict]) -> int:
    if verifier.get("type") == "script" and verifier.get("script"):
        return 1 + len([c for c in checks if c.get("check") in DETERMINISTIC_CHECKS])
    if verifier.get("type") == "manual":
        return 0
    count = len([c for c in checks if c.get("check") in DETERMINISTIC_CHECKS])
    count += len([c for c in critical_checks if c.get("check") in DETERMINISTIC_CHECKS])
    return count


def _critical_declared(criteria: list[dict], checks: list[dict], critical_checks: list[dict]) -> bool:
    if critical_checks:
        return True
    if any(c.get("critical") for c in checks):
        return True
    if any(c.get("critical") for c in criteria):
        return True
    # 没有 critical 时，要求所有完成条件都显式声明 critical 字段（明确为「非关键」）
    return bool(criteria) and all(c.get("critical_declared") for c in criteria)


def rubric_hash(rubric: dict) -> str:
    """rubric 的规范 hash（用于报告失效判定与 config_hash）。"""
    return canonical_hash(normalize_rubric(rubric))


def critical_hash(critical_checks) -> str:
    return canonical_hash(normalize_critical_checks(critical_checks))


def suite_hash_payload(suite: dict, cases: list[dict], critical_checks: list[dict]) -> dict:
    """冻结 hash 的输入：用例集合 + rubric + critical + thresholds。"""
    case_items = []
    for case in sorted(cases, key=lambda c: (c.get("name") or "", c.get("prompt") or "")):
        cases_verifier = case.get("verifier") or {}
        case_items.append(
            {
                "name": case.get("name"),
                "prompt": case.get("prompt"),
                "role": case.get("role"),
                "fixture_hash": case.get("fixture_hash"),
                "rubric_hash": case.get("rubric_hash"),
                "completion_criteria": _criteria_view(case.get("completion_criteria")),
                "verifier": {
                    "type": cases_verifier.get("type"),
                    "checks": cases_verifier.get("checks"),
                    "script": cases_verifier.get("script"),
                },
                "expected_answers_hash": canonical_hash(case.get("expected_answers") or {}),
                "repetitions": case.get("repetitions"),
                "budget": case.get("budget") or {},
            }
        )
    return {
        "skill_id": suite.get("skill_id"),
        "name": suite.get("name"),
        "category": suite.get("category"),
        "objective": suite.get("objective"),
        "rubric": suite.get("rubric"),
        "critical_checks": critical_checks,
        "thresholds": suite.get("thresholds"),
        "cases": case_items,
    }


def _criteria_view(criteria) -> list[dict]:
    out = []
    for item in criteria or []:
        if not isinstance(item, dict):
            out.append({"name": item, "critical": False})
            continue
        out.append({k: item[k] for k in sorted(item) if k != "decidable"})
    return out


# --------------------------------------------------------------------- 服务
class EvalSuiteService:
    """评测集读写：不可变 revision、冻结、按角色取用例、预期答案访问分离。"""

    def __init__(self, store, repo, events=None, idem=None, config=None) -> None:
        self.store = store
        self.repo = repo
        self.events = events
        self.idem = idem
        self.config = config

    # ------------------------------------------------------------- 创建
    def create_suite(
        self,
        *,
        skill_id,
        name,
        category="",
        cases,
        rubric,
        critical_checks=None,
        thresholds=None,
        objective="quality",
        profile_id="local",
        idempotency_key=None,
        revision_of=None,
        note=None,
    ) -> dict:
        """新建评测集 revision（旧 revision 不可变）。

        ``revision_of`` 指定时，新 revision 与它共享 ``root_suite_id``（同一评测集的新版本）。
        """
        if self.repo.get("skill", skill_id) is None:
            raise InvalidInput(f"技能不存在: {skill_id}", field_errors={"skill_id": "not_found"})
        name = str(name or "").strip()
        if not name:
            raise InvalidInput("评测集名称不能为空", field_errors={"name": "required"})
        objective = str(objective or "quality").strip().lower()
        if objective not in OBJECTIVES:
            raise InvalidInput(
                f"不支持的评测目标: {objective}", field_errors={"objective": "unsupported"}
            )
        if not isinstance(cases, (list, tuple)) or not cases:
            raise InvalidInput("cases 不能为空", field_errors={"cases": "required"})

        norm_rubric = normalize_rubric(rubric)
        norm_critical = normalize_critical_checks(critical_checks)
        norm_thresholds = normalize_thresholds(thresholds)
        if thresholds and "budget" in (thresholds or {}):
            norm_thresholds["budget"] = dict(thresholds["budget"])
        norm_cases = [
            normalize_case(raw, index, rubric=norm_rubric, critical_checks=norm_critical)
            for index, raw in enumerate(cases)
        ]
        names = [c["name"] for c in norm_cases]
        if len(set(names)) != len(names):
            raise InvalidInput("用例名称不能重复", field_errors={"cases": "duplicate"})

        payload = {
            "skill_id": skill_id,
            "name": name,
            "category": str(category or ""),
            "case_names": names,
            "objective": objective,
            "rubric_hash": canonical_hash(norm_rubric),
            "critical_hash": canonical_hash(norm_critical),
            "thresholds": norm_thresholds,
            "revision_of": revision_of,
            "profile_id": profile_id,
        }

        def _create() -> dict:
            return self._create_impl(
                skill_id=skill_id,
                name=name,
                category=category,
                cases=norm_cases,
                rubric=norm_rubric,
                critical=norm_critical,
                thresholds=norm_thresholds,
                objective=objective,
                profile_id=profile_id,
                revision_of=revision_of,
                note=note,
            )

        if self.idem is not None:
            return self.idem.run("eval.suite.create", idempotency_key, payload, _create)
        return _create()

    def _create_impl(self, *, skill_id, name, category, cases, rubric, critical, thresholds,
                     objective, profile_id, revision_of, note) -> dict:
        root_id = None
        if revision_of:
            base = self.repo.get("eval_suite", revision_of)
            if base is None:
                raise InvalidInput(
                    f"评测集不存在: {revision_of}", field_errors={"revision_of": "not_found"}
                )
            if base["skill_id"] != skill_id:
                raise InvalidInput(
                    "新 revision 必须与来源评测集属于同一 skill",
                    field_errors={"revision_of": "skill_mismatch"},
                )
            root_id = base["root_suite_id"] or base["id"]
        row = self.store.query_one(
            "SELECT MAX(revision) AS r FROM eval_suite WHERE skill_id = ?", (skill_id,)
        )
        revision = int(row["r"] or 0) + 1
        suite_id = new_id("suite")
        now = utc_now()
        root_id = root_id or suite_id
        warnings = self._coverage_warnings(cases, thresholds)
        with self.store.tx() as conn:
            self.repo.insert(
                "eval_suite",
                {
                    "id": suite_id,
                    "profile_id": profile_id,
                    "skill_id": skill_id,
                    "name": name,
                    "category": str(category or ""),
                    "revision": revision,
                    "root_suite_id": root_id,
                    "suite_hash": None,
                    "rubric": json.dumps(rubric, ensure_ascii=False, sort_keys=True),
                    "critical_checks": json.dumps(critical, ensure_ascii=False, sort_keys=True),
                    "thresholds": json.dumps(thresholds, ensure_ascii=False, sort_keys=True),
                    "objective": objective,
                    "status": "draft",
                    "frozen_at": None,
                    "created_at": now,
                    "updated_at": now,
                },
                conn=conn,
            )
            for case in cases:
                self.repo.insert(
                    "eval_case",
                    {
                        "id": case["id"],
                        "suite_id": suite_id,
                        "name": case["name"],
                        "prompt": case["prompt"],
                        "input_files": json.dumps(case["input_files"], ensure_ascii=False),
                        "fixture_hash": case["fixture_hash"],
                        "rubric_hash": case["rubric_hash"],
                        "role": case["role"],
                        "completion_criteria": json.dumps(
                            case["completion_criteria"], ensure_ascii=False
                        ),
                        "verifier": json.dumps(case["verifier"], ensure_ascii=False, sort_keys=True),
                        "expected_answers": json.dumps(case["expected_answers"], ensure_ascii=False),
                        "created_at": now,
                    },
                    conn=conn,
                )
            if self.events is not None:
                self.events.audit(
                    "eval.suite.create",
                    "eval_suite",
                    suite_id,
                    after={
                        "skill_id": skill_id,
                        "revision": revision,
                        "root_suite_id": root_id,
                        "cases": len(cases),
                        "objective": objective,
                        "note": note,
                    },
                    conn=conn,
                )
        suite = self.get_suite(suite_id)
        suite["warnings"] = warnings
        if self.events is not None:
            self.events.product_event(
                "eval_suite_created",
                {
                    "suite_id": suite_id,
                    "skill_id": skill_id,
                    "revision": revision,
                    "cases": len(cases),
                    "holdout": len([c for c in cases if c["role"] == "holdout"]),
                },
                profile_id=profile_id,
            )
        return suite

    @staticmethod
    def _coverage_warnings(cases: list[dict], thresholds: dict) -> list[str]:
        warnings = []
        holdout = len([c for c in cases if c["role"] == "holdout"])
        min_cases = int(thresholds.get("min_cases", 0) or 0)
        min_holdout = int(thresholds.get("min_holdout", 0) or 0)
        if len(cases) < min_cases:
            warnings.append(
                f"样例不足：当前 {len(cases)} 个独立样例 < 门禁要求 {min_cases} 个，只给探索报告"
            )
        if holdout < min_holdout:
            warnings.append(
                f"holdout 不足：当前 {holdout} 个 < 门禁要求 {min_holdout} 个，不足以支撑 passed 结论"
            )
        return warnings

    # ------------------------------------------------------------- 读取
    def get_suite(self, suite_id) -> dict:
        row = self.repo.get("eval_suite", suite_id)
        if row is None:
            raise InvalidInput(f"评测集不存在: {suite_id}", field_errors={"suite_id": "not_found"})
        data = row_to_dict(row, json_fields=_SUITE_JSON_FIELDS) or {}
        counts = self.store.query_one(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN role = 'holdout' THEN 1 ELSE 0 END) AS h "
            "FROM eval_case WHERE suite_id = ?",
            (suite_id,),
        )
        data["case_count"] = int(counts["n"] or 0) if counts else 0
        data["holdout_count"] = int(counts["h"] or 0) if counts else 0
        data["frozen"] = data.get("status") == "frozen"
        data["immutable"] = data["frozen"]
        data["warnings"] = self._coverage_warnings(
            self.get_cases(suite_id), data.get("thresholds") or {}
        )
        return data

    def list_suites(self, *, skill_id=None) -> list[dict]:
        rows = self.store.query(
            "SELECT id FROM eval_suite WHERE (? IS NULL OR skill_id = ?) "
            "ORDER BY skill_id ASC, revision DESC",
            (skill_id, skill_id),
        )
        return [self.get_suite(r["id"]) for r in rows]

    def get_cases(self, suite_id, *, role=None, include_expected: bool = False) -> list[dict]:
        """按角色取用例；默认 **不返回 expected_answers**。"""
        if self.repo.get("eval_suite", suite_id) is None:
            raise InvalidInput(f"评测集不存在: {suite_id}", field_errors={"suite_id": "not_found"})
        if role is not None and role not in ROLES:
            raise InvalidInput(f"不支持的 role: {role}", field_errors={"role": "unsupported"})
        rows = self.store.query(
            "SELECT * FROM eval_case WHERE suite_id = ? AND (? IS NULL OR role = ?) "
            "ORDER BY created_at ASC, rowid ASC",
            (suite_id, role, role),
        )
        suite_json = row_to_dict(
            self.repo.get("eval_suite", suite_id), json_fields=_SUITE_JSON_FIELDS
        ) or {}
        cases = []
        for row in rows:
            case = row_to_dict(row, json_fields=_CASE_JSON_FIELDS) or {}
            case["suite_id"] = suite_id
            if include_expected:
                # 内部通道：只有 verifier / judge 可以读预期答案
                case["_critical_checks"] = list(suite_json.get("critical_checks") or [])
            else:
                case.pop("expected_answers", None)
            cases.append(case)
        return cases

    def get_case(self, case_id, *, include_expected: bool = False) -> dict:
        row = self.repo.get("eval_case", case_id)
        if row is None:
            raise InvalidInput(f"用例不存在: {case_id}", field_errors={"case_id": "not_found"})
        case = row_to_dict(row, json_fields=_CASE_JSON_FIELDS) or {}
        if include_expected:
            suite_json = row_to_dict(
                self.repo.get("eval_suite", case["suite_id"]), json_fields=_SUITE_JSON_FIELDS
            ) or {}
            case["_critical_checks"] = list(suite_json.get("critical_checks") or [])
        else:
            case.pop("expected_answers", None)
        return case

    # ------------------------------------------------------------- 冻结
    def freeze(self, suite_id) -> dict:
        """计算 ``suite_hash`` 并冻结该 revision；冻结后禁止修改。"""
        suite = self.get_suite(suite_id)
        if suite["status"] == "frozen":
            return suite
        cases = self.get_cases(suite_id, include_expected=True)
        critical = list(suite.get("critical_checks") or [])
        payload = suite_hash_payload(suite, cases, critical)
        digest = canonical_hash(payload)
        now = utc_now()
        with self.store.tx() as conn:
            self.repo.update(
                "eval_suite",
                suite_id,
                {"status": "frozen", "suite_hash": digest, "frozen_at": now, "updated_at": now},
                conn=conn,
            )
            if self.events is not None:
                self.events.audit(
                    "eval.suite.freeze",
                    "eval_suite",
                    suite_id,
                    after={"suite_hash": digest, "revision": suite["revision"],
                           "cases": len(cases)},
                    conn=conn,
                )
        frozen = self.get_suite(suite_id)
        frozen["suite_hash_payload"] = payload
        return frozen

    # ------------------------------------------------------------- 校验辅助
    def require_frozen(self, suite_id) -> dict:
        """评测启动前必须拿到已冻结且 hash 完整的 suite。"""
        suite = self.get_suite(suite_id)
        if suite["status"] != "frozen" or not suite.get("suite_hash"):
            raise InvalidInput(
                "评测集尚未冻结：执行前必须冻结 suite revision",
                field_errors={"suite_id": "not_frozen"},
            )
        return suite

    def assert_immutable(self, suite_id) -> dict:
        """冻结后禁止修改本 revision：任何写入口都必须先过这一关。"""
        suite = self.get_suite(suite_id)
        if suite["status"] == "frozen":
            raise InvalidInput(
                f"评测集 revision {suite['revision']} 已冻结，不可修改；请新建 revision",
                field_errors={"suite_id": "frozen"},
            )
        return suite

    def resolve_repetitions(self, suite: dict, case: dict | None = None) -> int:
        if case and case.get("repetitions"):
            return int(case["repetitions"])
        value = (suite.get("thresholds") or {}).get("repetitions")
        if value:
            return int(value)
        return DEFAULT_REPETITIONS

    def describe_case_plan(self, suite_id, *, case: dict | None = None) -> dict:
        """单用例的可判定性摘要（供 UI 与报告披露）。"""
        cases = [case] if case else self.get_cases(suite_id, include_expected=True)
        suite = self.get_suite(suite_id)
        items = []
        for item in cases:
            checks = list((item.get("verifier") or {}).get("checks") or [])
            checks += checks_from_criteria(item.get("completion_criteria"), item.get("verifier"))
            items.append(
                {
                    "case_id": item.get("id"),
                    "name": item.get("name"),
                    "role": item.get("role"),
                    "fixture_hash": item.get("fixture_hash"),
                    "checks": describe_checks(checks),
                    "critical": describe_checks(
                        [c for c in checks if c.get("critical")] + list(suite.get("critical_checks") or [])
                    ),
                    "verifier_type": (item.get("verifier") or {}).get("type"),
                }
            )
        return {"suite_id": suite_id, "suite_hash": suite.get("suite_hash"), "cases": items}
