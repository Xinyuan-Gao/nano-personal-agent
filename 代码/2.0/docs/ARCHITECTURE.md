# Skills 自进化框架 v2.0 技术设计（实现契约）

本文是 `代码/2.0/` 的实现契约。所有实现必须严格对齐本文的**接口签名、数据表、错误码与目录归属**；任何偏离都要先改本文再改代码。

- 上游依据：`../../docs/0.2 PRD.md`（v0.2）。本文覆盖 **P0 67 条需求**（REQ-SET/SKL/RUN/FBK/LRN/PRP/EVL/VER/SYS）+ **P1 四层个人记忆 8 条**（REQ-MEM-01～08），合计 75 条。
- v2.0 相对 v1.0 的增量：`skillforge/memory/` 记忆层（working memory / daily log / durable topic / MEMORY.md index + 按需召回 + 受控 dream）、记忆相关数据表、任务创建时的记忆冻结与 prompt 注入、`memory` CLI/HTTP/Web 入口、记忆指标与数据治理（备份脱敏、清除、导出）。
- 技术决策（对应 PRD DEC-02 / DEC-03 的初稿默认方案，由本文细化）：
  - Python 3.11+，**运行时零第三方依赖**（全部 stdlib），保证干净环境可直接启动。
  - 元数据用 SQLite（`sqlite3`，WAL，`foreign_keys=ON`）；不可变制品用内容寻址文件库（sha256）。
  - 本地 Web 用 `http.server.ThreadingHTTPServer` + 自写路由；仅监听 loopback，随机会话令牌 + Origin 校验 + 写操作 CSRF。
  - CLI 用 `argparse`，与 Web 走同一个 `AppServices`。
  - 脚本/shell 一律在容器中执行（`docker run --rm --network none`）；容器不可用时该能力明确不可用，**不降级为宿主 shell**。
  - 模型走 OpenAI 兼容 `POST {base_url}/chat/completions`，原生 `tool_calls` 优先，结构化文本动作兜底。

## 1. 目录归属（写入边界）

```
代码/2.0/
├─ README.md                  安装 / 启动 / 验收操作手册（Lead）
├─ docs/ARCHITECTURE.md       本文件（Lead）
├─ pyproject.toml             打包元数据，无第三方依赖（Lead）
├─ skillforge/
│  ├─ __main__.py             python -m skillforge（Lead）
│  ├─ core/                   ids/errors/config/db/schema.sql/paths/blobs/repository/events/clock
│  ├─ platform/               credentials/model/budget/container/queue/worker
│  ├─ skills/                 package/service/activation/selection
│  ├─ runtime/                tools/tools_fs/tools_exec/loop/runs
│  ├─ learning/               feedback/lessons/proposals/checks
│  ├─ evaluation/             suites/verifiers/judge/jobs/verdict/report
│  ├─ memory/                 v2.0 新增：scope/working/logs/topics/index/recall/dream/service
│  ├─ data/                   backup/metrics/retention
│  ├─ api/                    services(应用接口)/security/app/web/*
└─ cli/                       main.py
```

规则：只写自己归属目录内的文件；需要改 core 或他人目录时，在最终报告里说明，不要直接改。

## 2. 全局约定

- 时间：内部一律 UTC ISO8601（`core.clock.utc_now()` → `2026-01-01T00:00:00.000000Z`）。
- ID：`core.ids.new_id("run")` → `run_01JC...`（时间有序 + 随机），前缀与实体绑定。
- hash：`sha256_hex(bytes|str)`；结构化对象用 `canonical_hash(obj)`（排序键、紧凑 JSON）。
- 包 hash：对 `[{"path": p, "sha256": h, "size": n}]`（按 path 排序）做 canonical_hash。
- 事务：写操作走 `with store.tx() as conn:`；跨表写必须同一事务；审计写失败必须回滚该事务（REQ-VER-02）。
- 幂等：所有写接口接收 `idempotency_key`，经 `core.repository.IdempotencyStore` 去重，重放返回首次结果（REQ-SYS-03）。
- 模块拿数据库：只用 `core.db.Store`，不要自己打开 sqlite 连接。
- 记忆上下文：`run.input_snapshot.memory` 保存冻结的记忆上下文（索引 + 召回 + working），并带自己的 `context_hash`；`run.context_hash` 仍只覆盖请求 / 模型 / 技能 / 工具 / 预算 / 权限，记忆变化不改变既有 run 的 `context_hash`（REQ-MEM-05/06、REQ-RUN-02）。

## 3. core 层（Lead 提供，勿改）

### core/errors.py
```python
class AppError(Exception):
    code: str            # 见下
    message: str
    retryable: bool
    field_errors: dict[str, str]
    current_revision: int | None
    http_status: int
    def to_dict(self) -> dict   # {"code","message","retryable","correlation_id","field_errors","current_revision"}

class InvalidInput(AppError)        # invalid_input, 400
class PermissionDenied(AppError)    # permission_denied, 403
class MissingDependency(AppError)   # missing_dependency, 409
class ModelError(AppError)          # model_error, 502
class BudgetExceeded(AppError)      # budget_exceeded, 429
class BaselineConflict(AppError)    # baseline_conflict, 409（必带 current_revision）
class InvalidState(AppError)        # invalid_state, 409
class StorageError(AppError)        # storage_error, 500
class EvidenceUnavailable(AppError) # evidence_unavailable, 410
```

### core/clock.py
`utc_now() -> str`、`parse_ts(s) -> datetime`、`new_correlation_id() -> str`。

### core/ids.py
`new_id(prefix: str) -> str`、`sha256_hex(data: bytes | str) -> str`、`canonical_hash(obj) -> str`、`short_hash(h, n=12)`。

### core/config.py
```python
@dataclass class AppConfig:
    home: Path            # 默认 $SKILLFORGE_HOME 或 ~/.skillforge
    db_path: Path; blob_dir: Path; secret_dir: Path; export_dir: Path; log_dir: Path
    host: str = "127.0.0.1"; port: int = 8765
    @classmethod def load(cls, home: str | None = None) -> "AppConfig"
    def ensure_dirs(self) -> None
```
默认预算常量（REQ-RUN-06 / REQ-LRN-06 / REQ-EVL-10）集中在此：
`DEFAULT_RUN_BUDGET`、`DEFAULT_LEARNING_BUDGET`、`DEFAULT_EVAL_BUDGET`、`GLOBAL_DAILY_BUDGET`、`LEARNING_PAUSED_KEY`。

### core/db.py
```python
class Store:
    def __init__(self, db_path: Path) -> None
    def migrate(self) -> None                     # 执行 schema.sql，幂等
    @contextmanager def tx(self) -> Iterator[sqlite3.Connection]   # BEGIN IMMEDIATE + commit/rollback
    @contextmanager def read(self) -> Iterator[sqlite3.Connection]
    def query(self, sql, params=()) -> list[sqlite3.Row]
    def query_one(self, sql, params=()) -> sqlite3.Row | None
    def execute(self, sql, params=()) -> None
```

### core/paths.py
```python
def normalize(p: str | Path) -> Path                      # expanduser + resolve(strict=False)
def resolve_within(root: Path, rel: str) -> Path          # 越界/绝对路径/符号链接逃逸 → PermissionDenied
def is_within(root: Path, target: Path) -> bool
def safe_relpath(root: Path, target: Path) -> str
def check_writable_target(root: Path, rel: str, allow_existing: bool) -> Path
SECRET_PATTERNS: list[re.Pattern]                         # 明显凭据的静态扫描（REQ-PRP-03）
def scan_secrets(text: str) -> list[dict]                 # [{"kind","line","excerpt"}]
def is_ignored_path(rel: str) -> bool                     # .git/__pycache__/node_modules/...
```

### core/blobs.py
```python
class BlobStore:
    def __init__(self, root: Path) -> None
    def put_bytes(self, data: bytes) -> str               # 返回 sha256，内容寻址，已存在即复用
    def put_text(self, text: str) -> str
    def put_file(self, path: Path) -> str
    def get_bytes(self, h: str) -> bytes
    def get_text(self, h: str) -> str
    def exists(self, h: str) -> bool
    def path_for(self, h: str) -> Path
    def verify(self, h: str) -> bool
```

### core/repository.py
```python
class Repo:
    def __init__(self, store: Store) -> None
    # 通用
    def insert(self, table: str, data: dict) -> None
    def update(self, table: str, pk: str, pk_value: str, data: dict) -> None
    def get(self, table: str, pk_value: str, pk: str = "id") -> sqlite3.Row | None
    def list(self, table: str, where: dict | None = None, order: str = "created_at DESC", limit: int = 200) -> list[sqlite3.Row]
    def delete(self, table: str, where: dict) -> None
class IdempotencyStore:
    def __init__(self, store: Store) -> None
    def run(self, scope: str, key: str | None, payload: dict, fn: Callable[[], dict]) -> dict
    # key 为空时直接执行；同 key 且 payload 相同 → 返回首次结果；payload 不同 → InvalidInput
    # 执行中崩溃 → 记录 status=started，重放时抛 InvalidState 要求重试（禁止半结果）
```

### core/events.py
```python
class EventLog:
    def __init__(self, store: Store) -> None
    def run_event(self, run_id: str, name: str, payload: dict) -> int      # 每 run seq 递增
    def audit(self, action: str, entity_type: str, entity_id: str, *, actor="owner",
              before=None, after=None, permission_scope=None, reason=None, correlation_id=None) -> str
    def list_audit(self, limit=200, entity_id=None) -> list[dict]
    def product_event(self, name: str, params: dict, event_id: str | None = None) -> None  # 按 event_id 去重
    def list_product_events(self, limit=500) -> list[dict]
```
审计必须与业务写在同一事务：调用方用 `with store.tx() as conn:` 后传 `conn`，见 §8 约定 —— 实现方式：`EventLog` 支持 `conn` 参数：
`EventLog(store).audit(..., conn=conn)` 时使用调用方事务，否则自开事务。

`ProductEvent` 参数**禁止**包含任务正文/反馈原文/文件内容/凭据（PRD 11.1）。

## 4. platform 层

### platform/credentials.py
```python
class CredentialStore:
    def __init__(self, secret_dir: Path) -> None
    def put(self, name: str, secret: str) -> str        # 文件名安全，chmod 600，返回 ref="file:name"
    def get(self, ref: str) -> str | None
    def delete(self, ref: str) -> None
    def list_refs(self) -> list[str]                    # 只返回 ref，不返回值
def redact(text: str, secrets: Iterable[str]) -> str     # 日志/导出脱敏
```

### platform/settings.py（REQ-SET-01～04 的配置与工作区部分）
```python
class SettingsService:
    def __init__(self, store, repo, events, idem, creds, config) -> None
    def bootstrap(self) -> dict                       # 确保 profile='local' 存在，返回 profile
    def get_model_config(self, profile_id="local") -> dict      # 不含密钥，只含 api_key_ref
    def set_model_config(self, *, provider, base_url, model, api_key=None, api_key_ref=None,
                         temperature=0.2, max_output_tokens=2048, timeout_s=60.0,
                         extra=None, profile_id="local") -> dict
        # api_key 明文只写入 CredentialStore，库中仅存 ref（REQ-SET-01）
    def clear_model_config(self, profile_id="local") -> dict
    def record_model_check(self, ok: bool, detail: dict, profile_id="local") -> dict
    def list_workspaces(self, profile_id="local", include_removed=False) -> list[dict]
    def add_workspace(self, *, name, root_path, artifact_path=None, allowed_tools=None,
                      allow_network=False, allow_scripts=True, profile_id="local",
                      idempotency_key=None) -> dict
    def remove_workspace(self, workspace_id) -> dict            # status=removed，不物理删除
    def get_workspace(self, workspace_id) -> dict
    def check_workspace(self, workspace_id) -> dict   # 读/写探测 + 规范化实际路径（REQ-SET-02）
    def effective_permissions(self, workspace_id) -> dict       # {"read_roots","write_roots","tools","network","scripts"}
    def set_budget_defaults(self, *, run=None, learning=None, evaluation=None) -> dict
    def get_budget_defaults(self) -> dict
    def doctor(self, *, container: ContainerRuntime, model_client=None) -> dict
        # {"model": {...}, "data_dir": {...}, "container": {...}, "tools_available": [...],
        #  "tools_unavailable": [{"tool","reason"}]}（REQ-SET-03）
```
`allowed_tools` 白名单：`list_dir/read_text/search_text/write_artifact/patch_file/run_command`；
`run_command` 只有在容器可用且 `allow_scripts=True` 时才进入可用工具，**不得降级为宿主 shell**。

### platform/model.py
```python
@dataclass class ModelConfig:
    provider: str = "openai_compatible"; base_url: str = ""; model: str = ""
    api_key_ref: str | None = None; temperature: float = 0.2
    max_output_tokens: int = 2048; timeout_s: float = 60.0; extra: dict = field(default_factory=dict)
    def to_dict(self) -> dict; @classmethod def from_row(cls, row) -> "ModelConfig"
@dataclass class ToolCall: id: str; name: str; arguments: dict; raw_arguments: str
@dataclass class ModelReply:
    text: str; tool_calls: list[ToolCall]; usage: dict; model_id: str | None
    finish_reason: str | None; usage_known: bool; raw: dict; latency_ms: int
@dataclass class ChatMessage: role: str; content: str = ""; tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None; name: str | None = None
    def to_wire(self) -> dict

class ModelClient:
    def __init__(self, config: ModelConfig, credentials: CredentialStore,
                 transport: Callable[[dict, dict], dict] | None = None) -> None
        # transport(payload, headers) -> raw response dict；默认 urllib 实现
    def complete(self, messages: list[ChatMessage], tools: list[dict] | None = None,
                 max_output_tokens: int | None = None, response_format: dict | None = None) -> ModelReply
    def fingerprint(self) -> str          # provider|base_url|model|temperature|extra 的 canonical_hash
    def describe(self) -> dict            # 不含密钥
    def check_connection(self) -> dict    # {"ok":bool,"model_id":...,"latency_ms":...,"error":...} 发最小测试请求
```
重试规则（REQ-RUN-07）：网络/超时/429/5xx 最多 2 次退避重试，尊重 `Retry-After`；401/403/400/404 不重试；`usage` 缺失时 `usage_known=False`。
结构化文本兜底：`parse_action_json(text) -> list[ToolCall] | None`，支持 ```json fenced block 与 `{"tool":..., "arguments":{...}}`。

### platform/budget.py
```python
@dataclass class BudgetSpec:
    max_model_calls: int; max_tool_actions: int; max_wall_seconds: int; max_tokens: int
    def to_dict(self) -> dict; @classmethod def from_dict(cls, d) -> "BudgetSpec"
@dataclass class BudgetState: model_calls_used, tool_actions_used, tokens_used, elapsed_s,
    remaining_calls, remaining_tokens, exceeded_type: str | None, unknown_usage: int
class BudgetLedger:
    def __init__(self, store: Store, clock=...) -> None
    def open(self, scope_id: str, spec: BudgetSpec, *, estimated_tokens: int = 0) -> None
    def reserve_call(self, scope_id: str, estimated_tokens: int = 0) -> bool   # 不足返回 False（并发安全，原子）
    def consume(self, scope_id: str, *, tokens: int, calls: int = 1, tool_actions: int = 0, usage_known: bool = True) -> None
    def release_call(self, scope_id: str, estimated_tokens: int = 0) -> None
    def add_tool_action(self, scope_id: str) -> bool
    def state(self, scope_id: str) -> BudgetState
    def close(self, scope_id: str, status: str) -> None
    def check_wall_clock(self, scope_id: str) -> bool
```
预算表 `budget_ledger` 按 `scope_id`（run_id / job_id）一行，`reserved_tokens` 预留上界，`used_*` 实际；`usage_unknown` 计入保守预留。

### platform/container.py
```python
@dataclass class ContainerStatus: available: bool; engine: str | None; version: str | None; reason: str | None
@dataclass class CommandResult: exit_code: int; stdout: str; stderr: str; timed_out: bool; duration_ms: int; engine: str
class ContainerRuntime:
    def __init__(self, image: str = "python:3.12-slim", enabled: bool = True) -> None
    def status(self) -> ContainerStatus
    def run(self, script: str, *, inputs_dir: Path | None = None, outputs_dir: Path | None = None,
            timeout_s: int = 60, network: bool = False, cpu: float = 1.0, memory_mb: int = 512,
            env: dict | None = None) -> CommandResult
    # --network none 默认；inputs 只读挂载；outputs 读写挂载；不挂载宿主凭据/docker.sock
```
容器不可用时 `run()` 抛 `MissingDependency("missing_dependency", "容器运行时不可用: ...")`。

### platform/queue.py
```python
@dataclass class Job: id, type, status, payload, budget, priority, attempts, lease_owner,
    lease_expires_at, heartbeat_at, result, error_code, created_at, updated_at
class JobQueue:
    def __init__(self, store: Store, repo: Repo, events: EventLog) -> None
    def enqueue(self, type: str, payload: dict, *, idempotency_key: str | None = None,
                budget: dict | None = None, priority: int = 100) -> str      # 幂等：同 key 返回同 job_id
    def lease(self, types: list[str], worker_id: str, lease_seconds: int = 60) -> Job | None
    def heartbeat(self, job_id: str, worker_id: str) -> bool
    def finish(self, job_id: str, status: str, *, result=None, error_code=None) -> None
    def progress(self, job_id: str, checkpoint: dict) -> None
    def cancel(self, job_id: str) -> str                 # "cancel_requested" / 终态原样返回
    def get(self, job_id: str) -> Job
    def list(self, status: str | None = None, type: str | None = None, limit=100) -> list[Job]
    def recover_stale(self, now=None) -> list[str]       # 租约过期 → queued（interrupted 语义），返回 job_ids
    def request_cancel_seen(self, job_id: str) -> bool
class WorkerPool:
    def __init__(self, queue: JobQueue, handlers: dict[str, Callable[[Job], None]],
                 max_execution: int = 2, max_learning: int = 1) -> None
    def start(self) -> None; def stop(self, timeout=5.0) -> None; def run_once(self, types=None) -> bool
```

## 5. skills 层

### skills/package.py
```python
@dataclass class FileEntry: path: str; content_hash: str; size: int; blob_id: str; executable: bool
@dataclass class CheckFinding: level: str  # "hard" | "hint" | "risk" | "skipped"
    code: str; message: str; locator: str | None = None
@dataclass class SkillPackage:
    root: Path; manifest: list[FileEntry]; package_hash: str; name: str; description: str
    metadata: dict; body: str; findings: list[CheckFinding]; total_bytes: int
    lead_file: str = "SKILL.md"
def parse_frontmatter(text: str) -> tuple[dict, str]        # 支持 --- YAML 子集（key: value / 列表 / 嵌套一层）
def read_package(root: Path, *, limits: PackageLimits | None = None, allow_absolute_refs=False) -> SkillPackage
def diff_manifests(a: list[FileEntry], b: list[FileEntry]) -> list[dict]
    # [{"path","change":"added|removed|modified|unchanged","old_hash","new_hash"}]
def hard_checks(pkg: SkillPackage, *, baseline: SkillPackage | None = None) -> list[CheckFinding]
def quality_hints(pkg: SkillPackage, *, baseline: SkillPackage | None = None) -> list[CheckFinding]
@dataclass class PackageLimits: max_files: int = 200; max_file_bytes: int = 2_000_000; max_total_bytes: int = 20_000_000
```
硬检查（REQ-PRP-03）：SKILL.md 可解析、name/description 存在且身份不被静默改变、相对引用存在、路径合法（无绝对路径/`..`/外部 symlink）、无明文凭据、脚本依赖声明完整、包大小合规；`.git`/缓存目录记为 skipped。

### skills/service.py
```python
class SkillService:
    def __init__(self, store, repo, blobs, events, idem, config) -> None
    def import_skill(self, source_path: str, *, scope="workspace", workspace_id=None,
                     profile_id="local", idempotency_key=None, on_conflict="reject",
                     new_slug=None, actor="owner") -> dict   # {"skill_id","version_id","created","checks","findings","skipped"}
    def create_draft(self, *, name, description, body, scope="personal", workspace_id=None,
                     profile_id="local", references=None, origin="draft", idempotency_key=None) -> dict
    def create_version_from_package(self, skill_id, pkg: SkillPackage, *, origin, parent_version_id,
                                    validation_status, note=None, conn=None) -> str
    def list_skills(self, *, workspace_id=None, query=None, include_archived=True, profile_id="local") -> list[dict]
    def get_skill(self, skill_id) -> dict
    def list_versions(self, skill_id) -> list[dict]
    def get_version(self, version_id) -> dict
    def get_version_files(self, version_id) -> list[dict]
    def read_version_file(self, version_id, rel_path) -> str
    def materialize_version(self, version_id, dest_dir: Path) -> Path     # 重建完整包到目录
    def export_skill(self, skill_id, dest_dir: str, *, include_versions=True) -> dict
    def import_export_bundle(self, bundle_dir: str, *, scope, workspace_id, idempotency_key=None) -> dict
    def archive(self, skill_id, *, actor="owner") -> dict
    def restore(self, skill_id, *, actor="owner") -> dict
    def mark_stale_proposals(self, skill_id, *, reason: str, conn=None) -> list[str]
```
导入约束（REQ-SKL-01～04）：原目录只读、不执行脚本；同范围同 slug 冲突 → `on_conflict="reject"` 抛 `InvalidInput`，`"new_version"` 生成新版本，`"new_slug"` 改身份；工作区同名优先于个人范围并暴露 `shadowed` 字段。

### skills/selection.py
```python
@dataclass class SelectionResult:
    candidates: list[dict]      # 轻量索引：skill_id, slug, name, description, scope, version_id, status
    chosen: list[dict]          # 最多 max_skills 个
    skipped: list[dict]         # [{"skill_id","reason"}]
    reason: str
class SkillSelector:
    def __init__(self, skills: SkillService) -> None
    def build_index(self, *, workspace_id, profile_id="local", query=None) -> list[dict]
    def choose(self, request: str, *, workspace_id, explicit_skill_ids=None, chosen_by="model",
               model_choice=None, max_skills=3) -> SelectionResult
    def load_context(self, run_id: str, *, result: SelectionResult, allow_references=True,
                     char_budget: int = 48_000, only_version_ids=None) -> list[dict]
        # 记录 skill_load：load_kind=index|full|reference，返回 [{version_id,file_path,chars,load_status}]
```
只有真正注入上下文的内容才记 `loaded`；未加载/仅索引不得作为学习来源（REQ-SKL-05/06）。

### skills/activation.py
```python
class ActivationService:
    def __init__(self, store, repo, events, idem, skills: SkillService) -> None
    def current(self, skill_id) -> dict | None      # {"activation_id","version_id","revision","origin",...}
    def activate_proposal(self, *, proposal_id, report_id, scope=None, expected_revision,
                          idempotency_key=None, actor="owner") -> dict
    def activate_manual_trial(self, *, source_version_id, unverified_ack: bool, scope=None,
                              expected_revision, idempotency_key=None, actor="owner") -> dict
    def rollback(self, *, skill_id, target_version_id, expected_revision,
                 idempotency_key=None, actor="owner") -> dict
    def history(self, skill_id) -> list[dict]
```
- `activate_proposal` 只接受 `proposal.status == "eligible"` 且 `report` 与当前候选包/config hash 匹配（REQ-VER-01）。
- 事务：先落完整包（blob 已存在）→ 同事务写 `activation` + `audit_event`；`revision` 必须是 `expected_revision + 1`，否则 `BaselineConflict(current_revision=...)`（REQ-VER-02/03）。
- 手工试用创建 `origin="manual_trial"` 的**新派生版本** + 独立 activation，不改候选 verdict（REQ-VER-04）。
- 回退校验包完整性（所有 file blob 存在且 hash 匹配）与依赖（脚本能力在容器可用时才可回退到含脚本版本）；不删候选与评测（REQ-VER-05）。

## 6. runtime 层

### runtime/tools.py
运行权限快照 `permissions`（`RunService` 冻结、`ToolRegistry` 强制校验，**技能正文无法扩大它**）::

```python
{
  "read_roots": [abs_path, ...],        # 只读输入根
  "write_roots": [abs_path, ...],       # 可写产物根
  "artifacts_dir": abs_path,            # 本次 run 产物目录（默认唯一可写位置）
  "tools": ["list_dir", "read_text", "search_text", "write_artifact"],  # 白名单
  "allow_existing_write": False,        # 修改既有用户文件需另行明确授权
  "network": False,                     # 容器内网络
  "scripts": False,                     # 是否允许 run_command
  "container_required": True
}
```
```python
@dataclass class ToolResult: status: str      # "ok"|"failed"|"partial"|"denied"
    output: str; artifacts: list[dict]; side_effect_status: str; error_code: str | None = None
    # side_effect_status: "none"|"applied"|"unknown"
@dataclass class ToolContext:
    run_id: str; workspace: dict; permissions: dict; artifacts_dir: Path; container: ContainerRuntime
    input_roots: list[Path]; action_seq: int
class ToolRegistry:
    def __init__(self, ctx: ToolContext) -> None
    def specs(self) -> list[dict]                 # 参数 schema + 副作用类别（REQ-RUN-03）
    def get(self, name: str) -> Callable[[dict], ToolResult]
    def side_effect(self, name: str) -> str       # "none"|"write"|"exec"
    def validate(self, name: str, arguments: dict) -> dict   # 参数 schema 校验，失败抛 InvalidInput
```
工具集（P0）：`list_dir`、`read_text`、`search_text`、`write_artifact`（产物目录，副作用 write）、`patch_file`（显式授权时改既有用户文件）、`run_command`（容器，副作用 exec）。

### runtime/runs.py
`RunService` 冻结权限快照时，以 `SettingsService.effective_permissions(workspace_id)` 为基础，并补两个每-run 字段：
`permissions["artifacts_dir"] = <config.workspace_artifacts_dir(ws)/run_id>`、`permissions["allow_existing_write"] = False`（只有用户在创建任务时显式授权既有文件写入才置 True）。

```python
@dataclass class RunSnapshot:  request, workspace_id, completion_criteria, input_files, model, skills, tools, budget, context_hash
class RunService:
    def __init__(self, store, repo, events, idem, queue, skills, selector, model_factory,
                 budget, container, config) -> None
    def create_run(self, *, request, workspace_id, completion_criteria=None, skill_ids=None,
                   budget=None, parent_run_id=None, profile_id="local", idempotency_key=None,
                   learning_enabled: bool = True, origin: str = "user",
                   eval_job_id: str | None = None, artifacts_dir: str | None = None,
                   skill_versions: dict[str, str] | None = None) -> dict
        # 冻结快照 + 入队；1 秒内返回 run_id（REQ-RUN-02 / 4.3）
        # origin='evaluation' 与 learning_enabled=False 供评测使用（REQ-EVL-04）：
        #   评测 run 关闭学习、使用独立 artifacts_dir（初始环境隔离）
        # skill_versions={skill_id: version_id}：把本次 run 钉在指定版本上（REQ-EVL-01/02），
        #   冻结快照中该 skill 的 version_id 必须等于钉扎值，并记 pinned=True
    def get_run(self, run_id) -> dict
    def list_runs(self, *, workspace_id=None, status=None, limit=100) -> list[dict]
    def get_events(self, run_id, after_seq=0) -> list[dict]
    def get_actions(self, run_id) -> list[dict]
    def get_artifacts(self, run_id) -> list[dict]
    def cancel(self, run_id) -> dict             # 2 秒内停止派发新动作（REQ-RUN-09）
    def execute(self, run_id: str, worker_id: str) -> None    # worker handler
    def resume(self, run_id) -> dict             # 中断恢复：先核对未决副作用
    def snapshot_of(self, run_id) -> RunSnapshot
```
协作对象约定（各层共用）：
- `model_factory: Callable[[], ModelClient | None]`：无参工厂，返回绑定当前生效模型配置的客户端；模型未配置时返回 `None`，调用方必须把 run/job 置为失败（`model_error`），不得抛未捕获异常。
- `budget: BudgetLedger`、`container: ContainerRuntime`、`queue: JobQueue`、`selector: SkillSelector` 均为已构造实例；`config: AppConfig`。

### runtime/loop.py
```python
class AgentLoop:
    def __init__(self, run_service, tools: ToolRegistry, model: ModelClient, budget: BudgetLedger,
                 events: EventLog, repo: Repo, container: ContainerRuntime) -> None
    def run(self, run_id: str) -> str            # 返回终态：completed/failed/budget_stopped/cancelled/awaiting_input
```
Loop 规则：一次迭代派发**一个有副作用的动作**（只读动作可多次）；结构化动作解析失败修复重试 ≤2；每次模型调用前 `budget.reserve_call()`，超限 → `budget_stopped`；`completed` 不改变 `outcome`（REQ-RUN-08）；无实际工具结果不得伪造完成（REQ-RUN-05）。

## 7. learning / evaluation / data 层

### learning/feedback.py
```python
class FeedbackService:
    def submit(self, *, run_id, type, content=None, verdict=None, scope="task",
               target_skill_id=None, target_version_id=None, locator=None, source_kind="user",
               supersedes_id=None, idempotency_key=None, profile_id="local") -> dict
    def withdraw(self, feedback_id, *, reason=None, actor="owner") -> dict   # 撤回：未启用候选失效，已启用版标记来源已撤回
    def update_outcome(self, run_id, outcome: str, *, feedback_id=None) -> dict   # 只改 outcome，不倒改执行状态
    def list_feedback(self, *, run_id=None, skill_id=None, limit=200) -> list[dict]
    def get_conflicts(self, *, skill_id) -> list[dict]      # 同范围矛盾 → 待澄清（REQ-FBK-05）
    def pending_attribution(self) -> list[dict]
```
`scope ∈ {task, workspace, personal}`；“这次/本篇”默认 `task`，只有明确长期或用户显式选择才允许 `workspace|personal`。

### learning/lessons.py
```python
ATTRIBUTIONS = ("skill_gap","skill_conflict","model_noncompliance","tool_failure",
                "environment_change","input_gap","unknown")
class LessonService:
    def __init__(self, store, repo, events, queue, model_factory, budget, skills, idem) -> None
    def is_learning_enabled(self, skill_id=None) -> bool
    def set_learning_paused(self, paused: bool, *, skill_id=None) -> dict
    def enqueue_from_feedback(self, feedback_id, *, idempotency_key=None) -> str | None
        # 事件指纹+基线去重：同一指纹只生成一个学习作业（REQ-LRN-05）
    def extract(self, feedback_ids: list[str], *, job_id=None) -> list[dict]   # worker handler 调用
    def ignore(self, lesson_id, *, reason_code, actor="owner") -> dict
    def list_lessons(self, *, skill_id=None, status=None, limit=200) -> list[dict]
    def get_lesson(self, lesson_id) -> dict
```
产出 lesson 必须含：条件/操作/理由/验证方法/反例边界/来源（REQ-LRN-04），至少一条 `lesson_source`；预算上限走 `DEFAULT_LEARNING_BUDGET`。

### learning/checks.py + learning/proposals.py
```python
class ProposalService:
    def __init__(self, store, repo, events, idem, skills, lessons, model_factory, budget, blobs) -> None
    def propose_change(self, *, skill_id, baseline_version_id, lesson_ids, budget=None,
                       scope=None, idempotency_key=None, profile_id="local") -> dict
    def get_proposal(self, proposal_id) -> dict
    def list_proposals(self, *, skill_id=None, status=None, limit=200) -> list[dict]
    def get_diff(self, proposal_id) -> list[dict]
    def edit_candidate(self, proposal_id, *, edits: dict[str, str], note=None,
                       idempotency_key=None) -> dict    # 生成新的 immutable 候选版本，旧候选 superseded，旧报告失效
    def reject(self, proposal_id, *, reason_code, actor="owner") -> dict
    def mark_stale(self, proposal_id, *, reason, conn=None) -> dict
    def check_proposal(self, proposal_id) -> dict        # 硬检查 + 质量提示
```
生成阶段只能读取被授权来源与基线包，**禁止访问 holdout 夹具与预期答案**（REQ-PRP-06）。

### evaluation/*.py
```python
class EvalSuiteService:
    def create_suite(self, *, skill_id, name, category, cases: list[dict], rubric: dict,
                     critical_checks: list[dict], thresholds: dict, objective="quality",
                     profile_id="local", idempotency_key=None) -> dict   # revision 自增，旧 revision 不可变
    def get_suite(self, suite_id) -> dict
    def list_suites(self, *, skill_id=None) -> list[dict]
    def get_cases(self, suite_id, *, role=None, include_expected=False) -> list[dict]
    def freeze(self, suite_id) -> dict                   # 计算 suite_hash；冻结后禁止修改
class Verifier:
    def verify_case(self, case: dict, artifacts: list[dict], run_dir: Path) -> dict
        # {"passed":bool,"score":float|None,"checks":[{"name","status","detail"}],"critical_failed":[..]}
class BlindJudge:
    def score(self, *, rubric, case, artifacts_text, model) -> dict   # 隐藏版本身份；同模型需披露
class EvalService:
    def __init__(self, store, repo, events, idem, queue, skills, runs, budget, container, model_factory, config) -> None
    def start_evaluation(self, *, proposal_id, suite_id, config=None, budget=None,
                         idempotency_key=None, profile_id="local") -> dict   # 冻结 base/candidate/suite/config hash
    def execute(self, job_id: str, worker_id: str) -> None    # worker handler
    def resume(self, job_id) -> dict                     # 只补未完成配对
    def cancel(self, job_id) -> dict
    def get_job(self, job_id) -> dict
    def list_jobs(self, *, proposal_id=None, status=None) -> list[dict]
    def get_results(self, job_id) -> list[dict]
    def build_report(self, job_id) -> dict
    def apply_verdict(self, job_id) -> str               # 程序计算 passed/failed/inconclusive，模型不可写
def compute_verdict(*, results, thresholds, coverage, objective, budget_exceeded) -> dict
```
评测 execution 关闭学习与外部发布动作（REQ-EVL-04）；任一缺失配对不得 passed（REQ-EVL-08）；报告不完整不得当通过。

### data/backup.py + data/metrics.py
```python
class DataService:
    def usage(self) -> dict                       # 本地用量统计（调用/token/未知费用）
    def backup(self, dest: str, *, include_body=True, idempotency_key=None) -> dict   # 不含凭据
    def restore(self, backup_path: str, *, dry_run=True, idempotency_key=None) -> dict
    def export_bundle(self, dest: str, *, scope: dict, include_body=True) -> dict
    def preview_clear(self, scope: dict) -> dict  # 关联反馈/经验/候选/评测/备份
    def clear_data(self, scope: dict, *, confirm: bool, idempotency_key=None) -> dict
class MetricsService:
    def dashboard(self, *, workspace_id=None, skill_id=None) -> dict
```
备份 manifest 校验版本、清单与 hash；恢复失败不覆盖当前库（REQ-SYS-06）。

## 7.5 memory 层（v2.0 / P1，REQ-MEM-01～08）

四层记忆的写入边界：**memory 层不依赖 skills / evaluation**，保证召回与整合不会越权修改技能、启用版本或外部系统。

### 数据表（`core/schema.sql` 追加）

| 表 | 用途 | 关键约束 |
| --- | --- | --- |
| `memory_working` | 第 1 层 working memory：`session_id` 唯一、任务摘要、最近文件（含 `content_hash` 与 `state`） | 只服务当前 session；不自动升级为长期事实（REQ-MEM-02） |
| `memory_log` | 第 2 层 daily log：原文、`source`（`user_request`/`model_mark`/`task_result`/`dream`）、`source_kind`、`day`、`scope`、`content_hash` | append-only；删除清正文并置 `deleted`（REQ-MEM-03） |
| `memory_topic` | 第 3 层 durable topic：`topic_type`（四类 + `pending_classification`）、`scope`、`session_id`、`fingerprint`、`supersedes_id`/`superseded_by`、`status`、`revision` | 无来源不得创建；推断内容 `pending_review`（REQ-MEM-04/08） |
| `memory_topic_source` | 主题来源链（`log`/`run`/`feedback`/`user_request`/`dream`） | 每条主题至少一条来源（REQ-MEM-04） |
| `memory_index_entry` | 第 4 层 MEMORY.md 索引行（`line_no`、标题、短摘要） | 只承载索引；权威数据在 `memory_topic`（REQ-MEM-05） |
| `memory_recall` | 召回记录：`layer`、`mode`（`index`/`full`/`none`）、`score`、`reason`、`status`、`injected`、`helpful` | 每条召回可回指来源、作用域与 revision（REQ-MEM-06） |
| `dream_job` / `dream_lock` | 整合作业与互斥锁（含门槛快照 `gate`、扫描 `scan`、结果 `result`、租约） | 只写记忆层；失败可重试（REQ-MEM-07） |

作用域规则（`memory/scope.py`）：`personal` 跨工作区可见；`workspace` 仅本工作区；`task` 绑定 `session_id`。**默认不把 workspace 事实带入其他工作区**（REQ-MEM-01）。

### 组件（`skillforge/memory/`）

```python
# scope.py
SCOPES = ("task", "workspace", "personal")
TOPIC_TYPES = ("user_preferences","project_conventions","key_decisions","dependency_facts")
MAX_INDEX_LINES = 200; DEFAULT_RECALL_LIMIT = 3
DREAM_MIN_INTERVAL_HOURS = 24; DREAM_MIN_SESSIONS = 5; DREAM_LOCK_SECONDS = 600

# working.py
class WorkingMemoryService:
    def ensure(session_id=None, workspace_id=None) -> dict
    def get(session_id=None) -> dict | None
    def current(session_id, workspace_id, *, refresh=True) -> dict | None   # 关闭/失效/跨工作区不返回
    def capture_run(*, run_id, request, session_id=None, workspace_id=None,
                    output_text="", input_files=None) -> dict                 # 任务结束更新摘要与文件
    def refresh_files(session_id, *, workspace_id=None, read_roots=None) -> dict | None
        # hash 校验：内容变化 → state='stale'；文件消失 → state='missing'（REQ-MEM-02 / AT-27）
    def close(session_id=None) -> dict        # 只标记状态，不生成长期事实
    def fresh_file_entries(memory) -> list[dict]

# logs.py
class DailyLogService:
    def append(content, *, scope="task", workspace_id=None, source="user_request",
               source_kind=None, session_id=None, run_id=None, tags=None,
               supersedes_id=None) -> dict
    def list(*, day=None, scope=None, workspace_id=None, status="active", source=None, limit=200) -> list[dict]
    def days(*, limit=60) -> list[dict]; def session_ids(*, since=None) -> list[str]
    def withdraw(log_id, *, reason="") -> dict; def delete(log_id, *, reason="") -> dict
    def expire(*, days, now=None) -> dict

# topics.py
class TopicService:
    def create(*, title, summary, topic_type, scope="task", workspace_id=None, session_id=None,
               body="", source="user_request", source_kind=None, tags=None, sources=None,
               expires_at=None, supersedes_id=None, allow_conflict=False) -> dict
    def confirm(topic_id, *, note="") -> dict          # 待裁决 → 生效 + supersedes
    def update(topic_id, *, title=None, summary=None, body=None, tags=None, scope=None,
               workspace_id=None, topic_type=None, sources=None) -> dict
    def withdraw(topic_id, *, reason="") -> dict; def delete(topic_id, *, reason="") -> dict
    def expire(*, now=None) -> dict
    def recallable(*, workspace_id, session_id, statuses=("active",), limit=200) -> list[dict]
    def conflicts() -> list[dict]; def sources(topic_id) -> list[dict]

# index.py（MEMORY.md 是派生视图，可 rebuild）
class MemoryIndexService:
    def sync_topic(topic_id) -> dict | None; def rebuild() -> dict
    def render() -> dict          # {"text","entries","truncated","omitted","degraded_reason"}
    def write_file() -> dict      # <home>/memory/MEMORY.md
    def entries(*, limit=None) -> list[dict]; def read_body(topic_id) -> dict

# recall.py
def tokenize(text) -> set[str]     # 中文二元组 + 英文按词 + 停用词
class RecallService:
    def recall(query, *, workspace_id, session_id=None, run_id=None,
               limit=DEFAULT_RECALL_LIMIT, record=True, statuses=None) -> dict
        # {"entries":[{"memory_id","layer","title","summary","scope","score","reason","sources"}],
        #  "status": "hit"|"none", "candidate_count","matched_count","limit","query_tokens"}
    def index_context(*, record=False, ...) -> dict      # 索引先注入
    def record(*, run_id, session_id, workspace_id, entries, status, ...) -> list[str]
    def mark_helpful(recall_id, helpful) -> dict; def history(*, run_id=None, limit=200) -> list[dict]

# dream.py
class DreamService:
    def gate(*, trigger="manual") -> dict               # allowed / reasons / 门槛快照
    def enqueue(*, trigger="manual", idempotency_key=None) -> dict   # 门槛不满足 → InvalidInput
    def run(dream_job_id, worker_id="worker-learning") -> dict       # failed/skipped 可重试
    def get(dream_job_id) -> dict | None; def list_jobs(*, status=None, limit=100) -> list[dict]

# service.py（门面）
class MemoryService:
    def build_run_context(*, request, workspace_id, session_id=None, profile_id=None, origin="user") -> dict
        # 纯读；origin="evaluation" → {"enabled": False, "reason": "evaluation_isolation"}（REQ-EVL-04）
    def record_run_recall(run_id, context) -> list[str]      # run 行落库后写召回记录
    def update_working_after_run(run_id, snapshot, *, output_text="", status="completed") -> dict | None
    def remember(content, *, scope, workspace_id=None, topic_type=None, title=None, summary=None,
                 session_id=None, run_id=None, source="user_request", tags=None, topic_id=None,
                 create_topic=True, idempotency_key=None) -> dict
    def recall(...) / index_text() / rebuild_index() / read_topic(...) / history(...) / mark_helpful(...)
    def status() / settings() / set_enabled(...) / set_recall_limit(...) / set_retention_days(...)
    def conflicts() / expire(*, days=None) / purge_deleted() / export(*, dest=None, include_body=True)
    def metrics(*, workspace_id=None) -> dict     # PRD 11.3 三个记忆指标
```

### 与运行时 / 队列 / 数据的衔接

| 衔接点 | 实现口径 |
| --- | --- |
| 任务创建冻结 | `RunService.create_run` 调 `_freeze_memory` → `build_run_context`；写进 `input_snapshot.memory` 并附独立 `context_hash`；run 行提交成功后才写 `memory_recall`（避免创建失败留孤儿记录） |
| prompt 注入 | `AgentLoop._render_memory`：MEMORY.md 索引 → 相关召回（带来源与作用域）→ working memory；明确声明「记忆是事实、不扩大授权」，未命中显式写 `none` |
| working 更新 | `RunService._update_working_memory` 在终态前更新 session 摘要与最近文件；失败只写 run 事件，不影响终态 |
| 队列 | `LEARNING_TYPE_PREFIXES = ("learning.", "memory.")`：`memory.dream` 走学习槽位（1 个），不占用执行槽位（REQ-SYS-01） |
| 指标 | `MetricsService.dashboard()["memory"]` → `MemoryService.metrics()`；未装配记忆服务时返回 `not_applicable` 而非 0 |
| 备份 / 导出 | 备份 DB 快照天然含记忆表；`include_body=False` 通过 `BODY_FIELDS` 脱敏 `memory_log.content`、`memory_topic.summary/body` 等；`memory export` 输出 `manifest.json` + `memory-export.json` + `MEMORY.md`，不含凭据 |
| 清除 | `CLEAR_ID_TABLES` 含全部记忆表；`_expand_clear_sets` 补 FK 闭包（run→memory_recall/memory_log，topic→memory_index_entry/memory_topic_source）；按工作区清除只影响该工作区记忆 |
| 集成边界 | `DreamService` 不持有 skills / activation 依赖，并自检 `_assert_memory_only()`；整合结果记录 `write_scope` 与 `external_writes=[]`（REQ-MEM-07 / AT-28） |

## 8. api / cli 层

### api/services.py
```python
class AppServices:
    def __init__(self, config: AppConfig) -> None
    # 属性：store, repo, blobs, events, idem, creds, skills, selector, activation, memory, runs,
    #       feedback, lessons, proposals, evalsuites, evals, queue, workers, data, metrics, config
    def close(self) -> None
    def doctor(self) -> dict                     # 首次启动可用性检查（REQ-SET-03）
    def start_workers(self) -> None; def stop_workers(self) -> None
    # 每个业务操作一个方法，与 CLI/HTTP 一一对应，返回可 JSON 序列化 dict
```
约定：`AppServices` 是所有业务入口；Web 与 CLI 不直接碰 repo。

### api/security.py
```python
class SessionSecurity:
    def __init__(self, host: str) -> None
    token: str
    def check_origin(self, origin: str | None) -> bool
    def check_host(self, host_header: str | None) -> bool          # 仅 loopback
    def check_csrf(self, headers: dict, method: str) -> bool       # 写操作要求 X-CSRF-Token == token
    def cookie_header(self) -> str
    def check_request(self, method: str, path: str, headers: dict) -> None   # 失败抛 PermissionDenied
```

### api/app.py
`build_server(services, config) -> ThreadingHTTPServer`；`serve(services, config)`；静态资源从 `skillforge/api/web/` 提供；`/api/*` 返回 JSON，错误统一 `{"error": {...}}` + 正确 HTTP 状态码。

### HTTP 路由表（Web 与 CLI 共用 AppServices）

| 方法 | 路径 | AppServices 方法 |
| --- | --- | --- |
| GET | `/api/health` | `health()` |
| GET | `/api/doctor` | `doctor()` |
| GET | `/api/settings` | `settings_snapshot()` |
| GET/PUT | `/api/model` | `get_model_config()` / `set_model_config()` |
| POST | `/api/model/check` | `check_model()` |
| GET | `/api/workspaces` | `list_workspaces()` |
| POST | `/api/workspaces` | `add_workspace()` |
| DELETE | `/api/workspaces/{id}` | `remove_workspace()` |
| POST | `/api/workspaces/{id}/check` | `check_workspace()` |
| GET | `/api/skills` | `list_skills()` |
| POST | `/api/skills/import` | `import_skill()` |
| POST | `/api/skills/draft` | `create_skill_draft()` |
| GET | `/api/skills/{id}` | `get_skill()` |
| POST | `/api/skills/{id}/archive` \| `/restore` | `archive_skill()` / `restore_skill()` |
| POST | `/api/skills/{id}/export` | `export_skill()` |
| POST | `/api/events/page-view` | `record_page_view()` |
| GET | `/api/skills/{id}/versions` | `list_versions()` |
| GET | `/api/skills/{id}/versions/{vid}/files` | `list_version_files()` |
| GET | `/api/skills/{id}/versions/{vid}/file?path=` | `read_version_file()` |
| POST | `/api/skills/{id}/rollback` | `rollback_skill()` |
| POST | `/api/skills/{id}/manual-trial` | `activate_manual_trial()` |
| GET/POST | `/api/runs` | `list_runs()` / `create_run()` |
| GET | `/api/runs/{id}` | `get_run()`（含 events/actions/artifacts/loads） |
| POST | `/api/runs/{id}/cancel` | `cancel_run()` |
| POST | `/api/runs/{id}/resume` | `resume_run()` |
| POST | `/api/runs/{id}/feedback` | `submit_feedback()` |
| GET/POST | `/api/feedback` | `list_feedback()` / `submit_feedback()` |
| POST | `/api/feedback/{id}/withdraw` | `withdraw_feedback()` |
| GET | `/api/lessons` | `list_lessons()` |
| POST | `/api/lessons/{id}/ignore` | `ignore_lesson()` |
| GET | `/api/learning/settings` · PUT | `set_learning_paused()` |
| GET/POST | `/api/proposals` | `list_proposals()` / `propose_change()` |
| GET | `/api/proposals/{id}` | `get_proposal()`（含 diff、checks、sources） |
| POST | `/api/proposals/{id}/edit` \| `/reject` \| `/evaluate` | `edit_candidate()` / `reject_proposal()` / `start_evaluation()` |
| POST | `/api/proposals/{id}/activate` | `activate_proposal()` |
| GET/POST | `/api/eval/suites` | `list_suites()` / `create_suite()` |
| GET | `/api/eval/suites/{id}` | `get_suite()` |
| POST | `/api/eval/suites/{id}/freeze` | `freeze_suite()` |
| GET | `/api/eval/jobs` | `list_eval_jobs()` |
| GET | `/api/eval/jobs/{id}` | `get_eval_job()`（含 results/report） |
| POST | `/api/eval/jobs/{id}/cancel` \| `/resume` | `cancel_eval_job()` / `resume_eval_job()` |
| GET | `/api/jobs` | `list_jobs()` |
| POST | `/api/jobs/{id}/cancel` | `cancel_job()` |
| GET | `/api/memory` | `memory_status()` |
| GET/PUT | `/api/memory/settings` | `memory_settings()` / `set_memory_settings()` |
| POST | `/api/memory/remember` | `remember()` |
| GET | `/api/memory/logs` | `list_memory_logs()` |
| POST | `/api/memory/logs/{id}/withdraw` \| `/delete` | `withdraw_memory_log()` / `delete_memory_log()` |
| GET | `/api/memory/topics` | `list_memory_topics()` |
| GET/PUT | `/api/memory/topics/{id}` | `get_memory_topic()` / `update_memory_topic()` |
| POST | `/api/memory/topics/{id}/confirm` \| `/withdraw` \| `/delete` | `confirm_memory_topic()` / `withdraw_memory_topic()` / `delete_memory_topic()` |
| GET | `/api/memory/index` · POST `/api/memory/index/rebuild` | `memory_index()` / `memory_rebuild_index()` |
| GET | `/api/memory/recall` \| `/recalls` \| `/conflicts` | `memory_recall()` / `memory_history()` / `memory_conflicts()` |
| POST | `/api/memory/recalls/{id}/helpful` | `memory_helpful()` |
| GET | `/api/memory/sessions` · POST `/api/memory/sessions/close` | `memory_sessions()` / `close_memory_session()` |
| GET/POST | `/api/memory/dream` · POST `/api/memory/dream/{id}/run` | `memory_dream_preview()` / `memory_dream_start()` / `memory_dream_run()` |
| POST | `/api/memory/expire` \| `/api/memory/export` | `memory_expire()` / `memory_export()` |
| GET | `/api/metrics` | `metrics()`（含 `memory` 段） |
| GET | `/api/audit` | `list_audit()` |
| GET | `/api/data/usage` | `data_usage()` |
| POST | `/api/data/backup` \| `/restore` \| `/export` \| `/clear` | `backup()` / `restore()` / `export_bundle()` / `clear_data()` |

写操作（POST/PUT/DELETE）要求 `X-CSRF-Token` 与 `Origin` 校验通过；`GET /api/health` 与静态资源除外。


### cli/main.py
`main(argv=None) -> int`；子命令：`init`、`doctor`、`serve`、`model set|show|check`、`workspace add|list|remove|check`、`skill import|list|show|versions|archive|restore|export`、`task run|list|show|cancel|feedback`、`lesson list|ignore`、`propose`、`proposal show|diff|reject|activate`、`eval suite-create|suite-list|start|report|jobs`、`skill rollback|manual-trial`、`data backup|restore|export|usage|clear`、`memory status|settings|remember|log|log-delete|log-withdraw|topic|topic-show|topic-confirm|topic-withdraw|topic-delete|index|index-rebuild|recall|recall-history|recall-helpful|conflicts|sessions|session-close|dream|dream-run|dream-list|expire|export`、`metrics`、`audit`。全部支持 `--json`；`task run --session <id>` 指定 working memory 的 session。

## 8.5 集成阶段确认的实现口径（Lead 复核后固化）

以下几条在集成联调中被实测发现并修正，属于**必须保持**的行为：

| 主题 | 口径 | 依据 |
| --- | --- | --- |
| 执行权抢占（双重保险） | 两层都在：`RunService._claim_execution` 原子抢占 run 执行权；`EvalService` 侧再用 CAS 抢占该 run 的队列作业（抢不到则等待其终态，不重复执行）。评测 handler 传真实 `job.lease_owner` 作为 worker_id，保证 heartbeat 续租匹配 | REQ-SYS-01/03 |
| 执行权抢占（Run 侧） | `RunService.execute` 先用 `_claim_execution` 原子抢占（checkpoint 里写 `active_worker` + 租约）；同一 run 已有未过期执行者时直接跳过，不再重入循环 | REQ-SYS-01/03（评测 worker 与队列 worker 可能同时拿到同一 run，重复执行会重复副作用并触发非法迁移） |
| 技能正文注入 | 冻结快照里 `skills.loaded[].content` 保存被加载文件的正文，`AgentLoop._render_context` 必须把正文渲染进 prompt；只记 `skill_load` 而不注入等于技能没生效 | REQ-SKL-05/06、REQ-RUN-01（技能包存在制品库，模型无法用文件工具读到） |
| 钉扎版本必须可选 | 传入 `skill_versions` 时，被钉扎但作用域与当前（隔离评测）工作区不同的技能，要补进 `chosen` 并记 `pinned=True`；权限仍由工作区决定 | REQ-EVL-01/02（否则评测两组都拿不到方法，对照失去意义） |
| 快照可比性 | 两组一致性比较使用语义投影：排除 `frozen_at` 等易变字段 | REQ-EVL-02（否则每次评测都因时间戳不同被判「环境不可比」，结论恒为 inconclusive） |
| 状态迁移 | `awaiting_input`/`interrupted` 继续执行前先回到 `running`；`error_message` 只承载失败/等待类原因，正常迁移的说明写 `checkpoint.last_reason` | PRD 10.1.4 状态机 |
| 正文保留期限 | `data/retention.py` 的 `RetentionService`：`policy/set_policy/preview/apply/evidence_state`；到期只清正文，保留 ID/时间/状态/hash/加载记录/审计，制品与技能包默认保留；执行需 `confirm=True` 并写审计 | REQ-SYS-07 |
| 评测集 rubric | `rubric` 必须含 `dimensions`（权重合计 100）与 `pass_threshold`；API 层在用户未提供时写入显式的单一维度默认标准并标注来源 | REQ-EVL-05/12 |
| 终态可观测即证据齐全 | `AgentLoop` 的所有终态处理（completed / failed / budget_stopped / cancelled / awaiting_input）必须**先把用量与 `loop_summary` 写盘，再落状态**；观测到终态时用量、产物、动作记录都已可读 | REQ-SYS-03（避免读到 completed 但用量尚未落盘的竞态） |
| 模型标识留痕 | 每次模型回复把服务端返回的 `model_id` 写入 run 事件 `loop.model_reply` 与 `usage.model_ids`，供评测检测服务版本漂移 | REQ-EVL-02 |
| 学习入队编排 | `AppServices.submit_feedback` 在反馈保存成功后调用 `LessonService.enqueue_from_feedback` 把可学习事件入队（模型复盘来源不入队）；入队失败不影响反馈已保存，只在返回体给 `learning_enqueue_error` | PRD 10.2.5「可学习事件进入队列」、REQ-LRN-01/05 |
| 队列作业 id | 队列 `job.id` 与业务实体 id 不同：`eval.execute` 的真实作业 id 在 `job.payload["job_id"]`；`learning.extract` 用 `payload["feedback_ids"]`；`memory.dream` 用 `payload["dream_job_id"]` | REQ-EVL-08、REQ-LRN-05、REQ-MEM-07 |
| task 作用域必须绑 session | `memory_topic.session_id` 与 `memory_working.session_id` 是 task 作用域的边界：缺省视为 `default`，不同 session 不互相可见，避免「这次一页」串到下次任务 | REQ-MEM-01/02（AT-26） |
| 文件摘要新鲜度 | working memory 记录 `content_hash`：输入清单没带 hash 时按工作区根目录补算；内容变化 → `stale`，文件消失 → `missing`，两者都不注入 prompt 并说明失效数量 | REQ-MEM-02（AT-27） |
| 索引长度 | `MEMORY.md` 上限 200 行与字符上限；超限时截断并在正文里保留降级说明（说明不被截断吃掉），权威数据仍在 `memory_topic`，可 `index-rebuild` 重建 | REQ-MEM-05 |
| 召回可解释 | 打分 = 标签/标题/摘要/正文词命中 + 新鲜度，`reason` 里逐项给出命中词、age_days、scope、revision；未命中写 `status='none'`；默认最多 3 条 | REQ-MEM-06 |
| 冲突不静默覆盖 | 显式请求更新生效事实（revision+1）；dream 推断出的冲突新建 `pending_review` + `supersedes_id`，旧事实保持 active，用户 `topic-confirm` 后才切换 | REQ-MEM-08（AT-29） |
| 评测隔离记忆 | `build_run_context(origin="evaluation")` 直接返回 `enabled=False / reason=evaluation_isolation`，不写召回记录，prompt 明确说明「评测执行已关闭记忆召回」 | REQ-EVL-04、REQ-MEM-01 |

## 9. 验收约定

- 自动化测试已移除；启动前用 `python3 -m skillforge doctor` 做首次启动可用性检查（模型 / 数据目录 / 容器 / 工具）。
- 真实模型 / 容器的端到端验证由 `scripts/e2e_real.py` 手动执行。

## 10. 需求 → 模块追踪（75 条）

| 需求集合 | 主要落点 |
| --- | --- |
| REQ-SET-01～04 | `platform/credentials.py`、`platform/model.py`、`skills/service.py`(workspace)、`api/security.py`、`api/services.py#doctor` |
| REQ-SKL-01～07 | `skills/package.py`、`skills/service.py`、`skills/selection.py` |
| REQ-RUN-01～09 | `runtime/*`、`platform/model.py`、`platform/budget.py`、`platform/container.py` |
| REQ-FBK-01～06 | `learning/feedback.py` |
| REQ-LRN-01～06 | `learning/lessons.py`、`platform/queue.py` |
| REQ-PRP-01～08 | `learning/proposals.py`、`learning/checks.py`、`skills/package.py` |
| REQ-EVL-01～12 | `evaluation/*` |
| REQ-VER-01～07 | `skills/activation.py`、`skills/service.py`(archive) |
| REQ-SYS-01～08 | `platform/queue.py`、`platform/budget.py`、`runtime/runs.py`、`evaluation/jobs.py`、`data/backup.py`、`data/retention.py`（REQ-SYS-07 正文保留期限） |
| REQ-MEM-01～08 | `memory/scope.py`、`memory/working.py`、`memory/logs.py`、`memory/topics.py`、`memory/index.py`、`memory/recall.py`、`memory/dream.py`、`memory/service.py`；`runtime/runs.py#_freeze_memory`、`runtime/loop.py#_render_memory`、`api/services.py`、`cli/main.py`、`data/metrics.py`、`data/backup.py` |
