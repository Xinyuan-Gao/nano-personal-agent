from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ..core.errors import InvalidInput, MissingDependency

#: 容器内固定挂载点
INPUTS_MOUNT = "/inputs"
OUTPUTS_MOUNT = "/outputs"
#: 绝不允许出现在挂载源里的宿主路径
FORBIDDEN_MOUNT_SUFFIXES = ("docker.sock",)
#: 默认资源限制
DEFAULT_PIDS_LIMIT = 256
DEFAULT_TMPFS_MB = 64


@dataclass
class ContainerStatus:
    available: bool
    engine: str | None = None
    version: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    engine: str

    def to_dict(self) -> dict:
        return asdict(self)


class ContainerRuntime:
    def __init__(self, image: str = "python:3.12-slim", enabled: bool = True) -> None:
        self.image = image
        self.enabled = bool(enabled)
        self.engine = os.environ.get("SKILLFORGE_CONTAINER_ENGINE", "docker")
        self._status_cache: ContainerStatus | None = None

    # ------------------------------------------------------------- 探测
    def status(self) -> ContainerStatus:
        """探测容器运行时可用性；结果缓存（同一次进程内不重复探测）。"""
        if self._status_cache is not None:
            return self._status_cache
        if not self.enabled:
            self._status_cache = ContainerStatus(
                available=False, engine=self.engine, version=None, reason="容器运行时已在设置中禁用"
            )
            return self._status_cache
        if not self.engine or (shutil.which(self.engine) is None and not os.path.isabs(self.engine)):
            self._status_cache = ContainerStatus(
                available=False,
                engine=self.engine,
                version=None,
                reason=f"未找到容器引擎可执行文件: {self.engine}",
            )
            return self._status_cache
        available, version, reason = self._probe_engine()
        self._status_cache = ContainerStatus(
            available=available, engine=self.engine, version=version, reason=reason
        )
        return self._status_cache

    def _probe_engine(self) -> tuple[bool, str | None, str | None]:
        """默认探测：``<engine> version --format {{.Server.Version}}``。"""
        argv = [self.engine, "version", "--format", "{{.Server.Version}}"]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, None, f"容器引擎探测失败: {exc}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return False, None, f"容器引擎不可用: {detail[0] if detail else '未知错误'}"
        version = (proc.stdout or "").strip() or None
        return True, version, None

    def invalidate_status(self) -> None:
        self._status_cache = None

    # ------------------------------------------------------------- 执行
    def describe(self) -> dict:
        status = self.status()
        return {
            "engine": self.engine,
            "image": self.image,
            "enabled": self.enabled,
            "available": status.available,
            "version": status.version,
            "reason": status.reason,
        }

    def build_argv(
        self,
        *,
        inputs_dir: Path | None = None,
        outputs_dir: Path | None = None,
        network: bool = False,
        cpu: float = 1.0,
        memory_mb: int = 512,
        env: dict | None = None,
    ) -> list[str]:
        """拼装 docker run 参数（隔离约束的唯一定义处）。"""
        argv = [self.engine, "run", "--rm", "--interactive"]
        argv += ["--network", "bridge" if network else "none"]
        argv += ["--cpus", str(float(cpu))]
        argv += ["--memory", f"{int(memory_mb)}m"]
        argv += ["--pids-limit", str(DEFAULT_PIDS_LIMIT)]
        argv += ["--read-only", "--tmpfs", f"/tmp:rw,size={DEFAULT_TMPFS_MB}m"]
        argv += ["--security-opt", "no-new-privileges"]
        argv += ["--workdir", OUTPUTS_MOUNT if outputs_dir is not None else "/tmp"]

        mounts: list[tuple[Path, str, str]] = []
        if inputs_dir is not None:
            mounts.append((Path(inputs_dir), INPUTS_MOUNT, "ro"))
        if outputs_dir is not None:
            mounts.append((Path(outputs_dir), OUTPUTS_MOUNT, "rw"))
        for source, target, mode in mounts:
            resolved = self._safe_mount_source(source)
            argv += ["-v", f"{resolved}:{target}:{mode}"]

        for key, value in (env or {}).items():
            _validate_env_key(key)
            argv += ["-e", f"{key}={value}"]

        argv += [self.image, "python", "-"]
        return argv

    def _safe_mount_source(self, source: Path) -> Path:
        resolved = Path(source).expanduser().resolve(strict=False)
        for suffix in FORBIDDEN_MOUNT_SUFFIXES:
            if resolved.name == suffix or str(resolved).endswith(suffix):
                raise InvalidInput(
                    f"拒绝挂载敏感路径: {resolved}", field_errors={"mount": "forbidden"}
                )
        if resolved.is_socket():
            raise InvalidInput(f"拒绝挂载 socket: {resolved}", field_errors={"mount": "socket"})
        return resolved

    def run(
        self,
        script: str,
        *,
        inputs_dir: Path | None = None,
        outputs_dir: Path | None = None,
        timeout_s: int = 60,
        network: bool = False,
        cpu: float = 1.0,
        memory_mb: int = 512,
        env: dict | None = None,
    ) -> CommandResult:
        if not isinstance(script, str) or not script.strip():
            raise InvalidInput("脚本内容不能为空", field_errors={"script": "required"})
        status = self.status()
        if not status.available:
            raise MissingDependency(
                f"容器运行时不可用: {status.reason or '未知原因'}（不降级为宿主 shell）"
            )
        if inputs_dir is not None and not Path(inputs_dir).exists():
            raise InvalidInput(
                f"输入目录不存在: {inputs_dir}", field_errors={"inputs_dir": "not_found"}
            )
        if outputs_dir is not None:
            Path(outputs_dir).mkdir(parents=True, exist_ok=True)

        argv = self.build_argv(
            inputs_dir=inputs_dir,
            outputs_dir=outputs_dir,
            network=network,
            cpu=cpu,
            memory_mb=memory_mb,
            env=env,
        )
        exit_code, stdout, stderr, timed_out, duration_ms = self._spawn(
            argv, timeout_s=int(timeout_s), input_bytes=script.encode("utf-8"), env=env or {}
        )
        return CommandResult(
            exit_code=int(exit_code),
            stdout=stdout,
            stderr=stderr,
            timed_out=bool(timed_out),
            duration_ms=int(duration_ms),
            engine=self.engine,
        )

    def _spawn(
        self, argv: list[str], *, timeout_s: int, input_bytes: bytes, env: dict
    ) -> tuple[int, str, str, bool, int]:
        """真正启动子进程（测试覆写点）。"""
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                input=input_bytes,
                capture_output=True,
                timeout=max(1, int(timeout_s)),
                check=False,
                env={**os.environ, **_stringify_env(env)},
            )
        except subprocess.TimeoutExpired as exc:
            duration = int((time.monotonic() - started) * 1000)
            return (
                -1,
                _decode(exc.stdout),
                _decode(exc.stderr) or f"执行超时（>{timeout_s}s）",
                True,
                duration,
            )
        except OSError as exc:
            duration = int((time.monotonic() - started) * 1000)
            return -1, "", f"容器启动失败: {exc}", False, duration
        duration = int((time.monotonic() - started) * 1000)
        return proc.returncode, _decode(proc.stdout), _decode(proc.stderr), False, duration


def _validate_env_key(key: str) -> None:
    if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
        raise InvalidInput(f"非法环境变量名: {key!r}", field_errors={"env": "invalid_key"})


def _stringify_env(env: dict) -> dict:
    return {str(k): str(v) for k, v in (env or {}).items()}


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def container_status_to_dict(status: ContainerStatus | dict) -> dict:
    """把 status 统一成可 JSON 序列化的 dict（doctor 与 API 共用）。"""
    if isinstance(status, dict):
        return status
    return status.to_dict()


def parse_docker_json(output: str) -> dict:
    """尽力解析 docker 输出的 JSON（诊断用，失败返回空 dict）。"""
    try:
        return json.loads(output or "{}")
    except (TypeError, ValueError):
        return {}
