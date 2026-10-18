from __future__ import annotations

from ..core.errors import InvalidInput

SCOPE_TASK = "task"
SCOPE_WORKSPACE = "workspace"
SCOPE_PERSONAL = "personal"
SCOPES = (SCOPE_TASK, SCOPE_WORKSPACE, SCOPE_PERSONAL)

#: durable topics 首期只接受四类主题
TOPIC_USER_PREFERENCES = "user_preferences"
TOPIC_PROJECT_CONVENTIONS = "project_conventions"
TOPIC_KEY_DECISIONS = "key_decisions"
TOPIC_DEPENDENCY_FACTS = "dependency_facts"
TOPIC_TYPES = (
    TOPIC_USER_PREFERENCES,
    TOPIC_PROJECT_CONVENTIONS,
    TOPIC_KEY_DECISIONS,
    TOPIC_DEPENDENCY_FACTS,
)
TOPIC_UNCLASSIFIED = "pending_classification"
TOPIC_TYPE_CHOICES = TOPIC_TYPES + (TOPIC_UNCLASSIFIED,)

#: 主题状态
TOPIC_STATUS_ACTIVE = "active"
TOPIC_STATUS_PENDING_REVIEW = "pending_review"
TOPIC_STATUS_SUPERSEDED = "superseded"
TOPIC_STATUS_WITHDRAWN = "withdrawn"
TOPIC_STATUS_EXPIRED = "expired"
TOPIC_STATUS_DELETED = "deleted"

#: 日志 / 工作记忆状态
LOG_STATUS_ACTIVE = "active"
LOG_STATUS_WITHDRAWN = "withdrawn"
LOG_STATUS_DELETED = "deleted"

WORKING_STATUS_ACTIVE = "active"
WORKING_STATUS_CLOSED = "closed"
WORKING_STATUS_INVALIDATED = "invalidated"

#: MEMORY.md 索引长度上限（建议 200 行）
MAX_INDEX_LINES = 200
#: 注入 prompt 的索引字符上限（超限时降级并说明）
MAX_INDEX_CHARS = 6000
#: 默认召回条数上限
DEFAULT_RECALL_LIMIT = 3
MAX_RECALL_LIMIT = 10

#: dream 自动触发门槛
DREAM_MIN_INTERVAL_HOURS = 24
DREAM_MIN_SESSIONS = 5
DREAM_LOCK_SECONDS = 600
#: 单次整合默认上限：沿用学习作业预算口径，不额外扩大
DREAM_MAX_MODEL_CALLS = 4
DREAM_MAX_LOGS = 200

#: 默认 session：CLI / Web 未显式指定时使用
DEFAULT_SESSION = "default"


def validate_scope(scope: str | None, *, workspace_id: str | None = None,
                   default: str = SCOPE_TASK) -> str:
    """校验并返回作用域；workspace 作用域必须有 workspace_id。"""
    value = (scope or default or SCOPE_TASK).strip()
    if value not in SCOPES:
        raise InvalidInput(
            f"作用域必须是 {' / '.join(SCOPES)} 之一，收到 {value!r}",
            field_errors={"scope": "invalid"},
        )
    if value == SCOPE_WORKSPACE and not workspace_id:
        raise InvalidInput(
            "workspace 作用域必须指定 workspace_id", field_errors={"workspace_id": "required"}
        )
    return value


def normalize_topic_type(topic_type: str | None) -> str:
    """四类之外的输入一律进入待分类状态，不静默当成稳定事实。"""
    value = (topic_type or "").strip()
    return value if value in TOPIC_TYPE_CHOICES else TOPIC_UNCLASSIFIED


def visible_scope_sql(column: str = "scope") -> str:
    """召回 / 列表的可见性片段：personal 永远可见，其余按工作区与会话过滤。"""
    return (
        f"({column} = '{SCOPE_PERSONAL}'"
        f" OR ({column} = '{SCOPE_WORKSPACE}' AND workspace_id = :workspace_id)"
        f" OR ({column} = '{SCOPE_TASK}' AND (workspace_id IS NULL OR workspace_id = :workspace_id)"
        f"     AND (session_id IS NULL OR session_id = :session_id)))"
    )
