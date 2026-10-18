from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 默认预算
# 每任务最多 30 次模型调用、20 次工具动作、15 分钟、100,000 token
DEFAULT_RUN_BUDGET = {
    "max_model_calls": 30,
    "max_tool_actions": 20,
    "max_wall_seconds": 900,
    "max_tokens": 100_000,
}
# 学习作业上限 8 次模型调用、20,000 token、5 分钟
DEFAULT_LEARNING_BUDGET = {
    "max_model_calls": 8,
    "max_tool_actions": 0,
    "max_wall_seconds": 300,
    "max_tokens": 20_000,
}
# 评测总上限 400 次调用、500,000 token、60 分钟
DEFAULT_EVAL_BUDGET = {
    "max_model_calls": 400,
    "max_tool_actions": 200,
    "max_wall_seconds": 3600,
    "max_tokens": 500_000,
}
# 全局日调用与 token 上限（日配额）
GLOBAL_DAILY_BUDGET = {
    "max_model_calls": 2000,
    "max_tokens": 4_000_000,
}
# 评测单次执行 ≤ 8 次模型调用
DEFAULT_EVAL_RUN_BUDGET = dict(DEFAULT_RUN_BUDGET, max_model_calls=8)
# 5 样例（≥2 holdout）× 3 次
DEFAULT_EVAL_REPETITIONS = 3
DEFAULT_EVAL_MIN_CASES = 5
DEFAULT_EVAL_MIN_HOLDOUT = 2

SETTING_LEARNING_PAUSED = "learning.paused"
SETTING_LEARNING_PAUSED_SKILLS = "learning.paused_skills"
SETTING_AUTO_ACTIVATE = "activation.auto"  # P0 恒为 False
SETTING_RETENTION_DAYS = "data.retention_days"
SETTING_CONTAINER_ENABLED = "container.enabled"
SETTING_CONTAINER_IMAGE = "container.image"

#: P1 记忆设置
SETTING_MEMORY_ENABLED = "memory.enabled"
SETTING_MEMORY_RECALL_LIMIT = "memory.recall_limit"
SETTING_MEMORY_RETENTION_DAYS = "memory.retention_days"

DEFAULT_CONTAINER_IMAGE = "python:3.12-slim"


@dataclass
class AppConfig:
    home: Path
    db_path: Path
    blob_dir: Path
    secret_dir: Path
    export_dir: Path
    log_dir: Path
    workspace_dir: Path
    memory_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, home: str | os.PathLike | None = None) -> "AppConfig":
        base = home or os.environ.get("SKILLFORGE_HOME") or (Path.home() / ".skillforge")
        base_path = Path(base).expanduser()
        return cls(
            home=base_path,
            db_path=base_path / "skillforge.db",
            blob_dir=base_path / "blobs",
            secret_dir=base_path / "secrets",
            export_dir=base_path / "exports",
            log_dir=base_path / "logs",
            workspace_dir=base_path / "workspaces",
            memory_dir=base_path / "memory",
            host=os.environ.get("SKILLFORGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("SKILLFORGE_PORT", "8765")),
        )

    def workspace_artifacts_dir(self, workspace_id: str) -> Path:
        return self.workspace_dir / workspace_id / "artifacts"

    def workspace_runs_dir(self, workspace_id: str) -> Path:
        return self.workspace_dir / workspace_id / "runs"

    def ensure_dirs(self) -> None:
        for path in (
            self.home,
            self.blob_dir,
            self.secret_dir,
            self.export_dir,
            self.log_dir,
            self.workspace_dir,
            self.memory_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.secret_dir, 0o700)
        except OSError:  # pragma: no cover - 非 POSIX 环境
            pass

    def describe(self) -> dict:
        return {
            "home": str(self.home),
            "db_path": str(self.db_path),
            "blob_dir": str(self.blob_dir),
            "memory_dir": str(self.memory_dir),
            "host": self.host,
            "port": self.port,
            "version": _version(),
        }

    def disk_usage(self) -> dict:
        def size_of(path: Path) -> int:
            total = 0
            if not path.exists():
                return 0
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += (Path(root) / name).stat().st_size
                    except OSError:
                        continue
            return total

        return {
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "blob_bytes": size_of(self.blob_dir),
            "export_bytes": size_of(self.export_dir),
            "free_bytes": shutil.disk_usage(self.home if self.home.exists() else Path.home()).free,
        }


def _version() -> str:
    from skillforge import __version__

    return __version__


def read_json_setting(store, key: str, default=None):
    row = store.query_one("SELECT value FROM setting WHERE key = ?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def write_json_setting(store, key: str, value) -> None:
    from .clock import utc_now

    now = utc_now()
    store.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), now),
    )
