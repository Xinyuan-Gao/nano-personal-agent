PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ------------------------------------------------------------------ 身份与配置
CREATE TABLE IF NOT EXISTS profile (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'local_owner',
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace (
    id            TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL REFERENCES profile(id),
    name          TEXT NOT NULL,
    root_path     TEXT NOT NULL,              -- 可读输入目录
    artifact_path TEXT NOT NULL,              -- 产物目录
    allowed_tools TEXT NOT NULL DEFAULT '["list_dir","read_text","search_text","write_artifact"]',
    allow_network INTEGER NOT NULL DEFAULT 0,
    allow_scripts INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_root ON workspace(profile_id, root_path);

CREATE TABLE IF NOT EXISTS model_config (
    id            TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL REFERENCES profile(id),
    label         TEXT NOT NULL DEFAULT 'default',
    provider      TEXT NOT NULL DEFAULT 'openai_compatible',
    base_url      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    api_key_ref   TEXT,
    temperature   REAL NOT NULL DEFAULT 0.2,
    max_output_tokens INTEGER NOT NULL DEFAULT 2048,
    timeout_s     REAL NOT NULL DEFAULT 60.0,
    extra         TEXT NOT NULL DEFAULT '{}',
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_check    TEXT,
    last_check_ok INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_model_config_label ON model_config(profile_id, label);

CREATE TABLE IF NOT EXISTS setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ------------------------------------------------------------------ 技能库
CREATE TABLE IF NOT EXISTS skill (
    id           TEXT PRIMARY KEY,
    profile_id   TEXT NOT NULL REFERENCES profile(id),
    workspace_id TEXT REFERENCES workspace(id),   -- NULL = personal 范围
    slug         TEXT NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    scope        TEXT NOT NULL,                   -- personal | workspace
    status       TEXT NOT NULL DEFAULT 'active',  -- active | archived
    origin       TEXT NOT NULL DEFAULT 'import',  -- import | draft | proposal | manual_trial
    tags         TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_skill_identity
    ON skill(profile_id, scope, IFNULL(workspace_id, ''), slug);
CREATE INDEX IF NOT EXISTS ix_skill_status ON skill(status);

CREATE TABLE IF NOT EXISTS skill_version (
    id                TEXT PRIMARY KEY,
    skill_id          TEXT NOT NULL REFERENCES skill(id),
    parent_version_id TEXT REFERENCES skill_version(id),
    version_label     TEXT NOT NULL DEFAULT '0.1.0',
    package_hash      TEXT NOT NULL,
    origin            TEXT NOT NULL,              -- import|draft|proposal|manual_trial|user_edit
    validation_status TEXT NOT NULL DEFAULT 'unchecked',  -- unchecked|passed|failed
    validation_detail TEXT NOT NULL DEFAULT '{}',
    file_count        INTEGER NOT NULL DEFAULT 0,
    total_bytes       INTEGER NOT NULL DEFAULT 0,
    note              TEXT,
    proposal_id       TEXT,
    created_by        TEXT NOT NULL DEFAULT 'owner',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_skill_version_skill ON skill_version(skill_id, created_at);

CREATE TABLE IF NOT EXISTS skill_version_file (
    version_id   TEXT NOT NULL REFERENCES skill_version(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    blob_id      TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    is_executable INTEGER NOT NULL DEFAULT 0,
    is_reference INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, path)
);

CREATE TABLE IF NOT EXISTS activation (
    id             TEXT PRIMARY KEY,
    skill_id       TEXT NOT NULL REFERENCES skill(id),
    version_id     TEXT NOT NULL REFERENCES skill_version(id),
    scope          TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    origin         TEXT NOT NULL,                 -- import|draft|proposal|manual_trial|rollback|restore
    proposal_id    TEXT,
    report_id      TEXT,
    unverified     INTEGER NOT NULL DEFAULT 0,
    reason         TEXT,
    actor          TEXT NOT NULL DEFAULT 'owner',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_activation_skill ON activation(skill_id, revision);
-- 每个 skill 最多一个生效指针（revision 最大者）
CREATE UNIQUE INDEX IF NOT EXISTS ux_activation_current ON activation(skill_id, revision);

-- ------------------------------------------------------------------ 任务执行
CREATE TABLE IF NOT EXISTS run (
    id                  TEXT PRIMARY KEY,
    profile_id          TEXT NOT NULL REFERENCES profile(id),
    workspace_id        TEXT NOT NULL REFERENCES workspace(id),
    parent_run_id       TEXT REFERENCES run(id),
    request             TEXT NOT NULL,
    completion_criteria TEXT NOT NULL DEFAULT '[]',
    skill_ids           TEXT NOT NULL DEFAULT '[]',
    input_snapshot      TEXT NOT NULL DEFAULT '{}',
    model_snapshot      TEXT NOT NULL DEFAULT '{}',
    tools_snapshot      TEXT NOT NULL DEFAULT '[]',
    permissions         TEXT NOT NULL DEFAULT '{}',
    budget              TEXT NOT NULL DEFAULT '{}',
    execution_status    TEXT NOT NULL DEFAULT 'queued',
    outcome             TEXT NOT NULL DEFAULT 'unknown',
    context_hash        TEXT NOT NULL,
    origin              TEXT NOT NULL DEFAULT 'user',   -- user | evaluation
    eval_job_id         TEXT,
    learning_enabled    INTEGER NOT NULL DEFAULT 1,
    output_text         TEXT NOT NULL DEFAULT '',
    error_code          TEXT,
    error_message       TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    checkpoint          TEXT NOT NULL DEFAULT '{}',
    usage               TEXT NOT NULL DEFAULT '{}',
    body_state          TEXT NOT NULL DEFAULT 'available',  -- available | expired
    body_expired_at     TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_workspace ON run(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS ix_run_status ON run(execution_status);

CREATE TABLE IF NOT EXISTS run_step (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    intent     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending|running|done|failed|skipped
    detail     TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_run_step ON run_step(run_id, seq);

CREATE TABLE IF NOT EXISTS action_record (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,
    action_id           TEXT NOT NULL,
    tool_name           TEXT NOT NULL,
    arguments           TEXT NOT NULL DEFAULT '{}',
    permission_snapshot TEXT NOT NULL DEFAULT '{}',
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running',   -- running|ok|failed|partial|denied|unknown
    side_effect_status  TEXT NOT NULL DEFAULT 'unknown',   -- none|applied|unknown
    result              TEXT,
    artifact_refs       TEXT NOT NULL DEFAULT '[]',
    attempt             INTEGER NOT NULL DEFAULT 1,
    error_code          TEXT,
    duration_ms         INTEGER
);
CREATE INDEX IF NOT EXISTS ix_action_run ON action_record(run_id, seq);

CREATE TABLE IF NOT EXISTS artifact (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    blob_id      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    kind         TEXT NOT NULL DEFAULT 'output',
    label        TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifact_run ON artifact(run_id);

CREATE TABLE IF NOT EXISTS skill_load (
    id                   TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    skill_id             TEXT NOT NULL REFERENCES skill(id),
    version_id           TEXT NOT NULL REFERENCES skill_version(id),
    file_path            TEXT NOT NULL DEFAULT 'SKILL.md',
    content_hash         TEXT NOT NULL,
    load_kind            TEXT NOT NULL,             -- index|full|reference
    chars                INTEGER NOT NULL DEFAULT 0,
    load_status          TEXT NOT NULL DEFAULT 'loaded',  -- loaded|skipped|truncated|failed
    reason               TEXT,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_skill_load_run ON skill_load(run_id);

CREATE TABLE IF NOT EXISTS run_event (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    name       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_run_event ON run_event(run_id, seq);

-- ------------------------------------------------------------------ 反馈与经验
CREATE TABLE IF NOT EXISTS feedback (
    id                TEXT PRIMARY KEY,
    run_id            TEXT REFERENCES run(id),
    profile_id        TEXT NOT NULL REFERENCES profile(id),
    target_skill_id   TEXT REFERENCES skill(id),
    target_version_id TEXT REFERENCES skill_version(id),
    type              TEXT NOT NULL,               -- acceptance|correction|verifier|review|rejection
    verdict           TEXT,                        -- accepted|partially_accepted|rejected
    source_kind       TEXT NOT NULL DEFAULT 'user',-- user|objective|model
    scope             TEXT NOT NULL DEFAULT 'task',-- task|workspace|personal
    content           TEXT NOT NULL DEFAULT '',
    locator           TEXT NOT NULL DEFAULT '{}',
    supersedes_id     TEXT REFERENCES feedback(id),
    status            TEXT NOT NULL DEFAULT 'active', -- active|superseded|withdrawn
    attribution_state TEXT NOT NULL DEFAULT 'pending',
    body_state        TEXT NOT NULL DEFAULT 'available',  -- available | expired
    body_expired_at   TEXT,
    author            TEXT NOT NULL DEFAULT 'owner',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feedback_run ON feedback(run_id);
CREATE INDEX IF NOT EXISTS ix_feedback_skill ON feedback(target_skill_id, scope);

CREATE TABLE IF NOT EXISTS lesson (
    id                TEXT PRIMARY KEY,
    profile_id        TEXT NOT NULL REFERENCES profile(id),
    target_skill_id   TEXT REFERENCES skill(id),
    target_version_id TEXT REFERENCES skill_version(id),
    attribution       TEXT NOT NULL,               -- 见 learning.lessons.ATTRIBUTIONS
    applicability     TEXT NOT NULL DEFAULT '',    -- 适用条件
    scope             TEXT NOT NULL DEFAULT 'task',
    workspace_id      TEXT REFERENCES workspace(id),
    rule_text         TEXT NOT NULL,
    condition_text    TEXT NOT NULL DEFAULT '',
    rationale_text    TEXT NOT NULL DEFAULT '',
    verification_text TEXT NOT NULL DEFAULT '',
    boundary_text     TEXT NOT NULL DEFAULT '',
    evidence_strength TEXT NOT NULL DEFAULT 'single_inference',
    status            TEXT NOT NULL DEFAULT 'draft', -- draft|proposed|ignored|rejected|activated|stale
    reason_code       TEXT,
    fingerprint       TEXT NOT NULL,
    source_count      INTEGER NOT NULL DEFAULT 0,
    job_id            TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_lesson_fingerprint ON lesson(fingerprint);
CREATE INDEX IF NOT EXISTS ix_lesson_skill ON lesson(target_skill_id, status);

CREATE TABLE IF NOT EXISTS lesson_source (
    lesson_id        TEXT NOT NULL REFERENCES lesson(id) ON DELETE CASCADE,
    feedback_id      TEXT NOT NULL REFERENCES feedback(id),
    evidence_locator TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (lesson_id, feedback_id)
);

-- ------------------------------------------------------------------ 候选修改
CREATE TABLE IF NOT EXISTS proposal (
    id                   TEXT PRIMARY KEY,
    profile_id           TEXT NOT NULL REFERENCES profile(id),
    skill_id             TEXT NOT NULL REFERENCES skill(id),
    baseline_version_id  TEXT NOT NULL REFERENCES skill_version(id),
    candidate_version_id TEXT REFERENCES skill_version(id),
    supersedes_id        TEXT REFERENCES proposal(id),
    status               TEXT NOT NULL DEFAULT 'draft',
    scope                TEXT NOT NULL,
    reason               TEXT NOT NULL DEFAULT '',
    summary              TEXT NOT NULL DEFAULT '',
    expected_benefit     TEXT NOT NULL DEFAULT '',
    potential_regression TEXT NOT NULL DEFAULT '',
    attribution          TEXT NOT NULL DEFAULT 'unknown',
    checks               TEXT NOT NULL DEFAULT '{}',
    diff                 TEXT NOT NULL DEFAULT '[]',
    capability_change    TEXT NOT NULL DEFAULT '{}',
    generation_budget    TEXT NOT NULL DEFAULT '{}',
    evaluation           TEXT NOT NULL DEFAULT '{}',
    stale_reason         TEXT,
    review_duration_ms   INTEGER,
    reject_reason_code   TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_proposal_skill ON proposal(skill_id, created_at);
CREATE INDEX IF NOT EXISTS ix_proposal_status ON proposal(status);

CREATE TABLE IF NOT EXISTS proposal_source (
    proposal_id TEXT NOT NULL REFERENCES proposal(id) ON DELETE CASCADE,
    lesson_id   TEXT NOT NULL REFERENCES lesson(id),
    PRIMARY KEY (proposal_id, lesson_id)
);

-- ------------------------------------------------------------------ 评测
CREATE TABLE IF NOT EXISTS eval_suite (
    id              TEXT PRIMARY KEY,
    profile_id      TEXT NOT NULL REFERENCES profile(id),
    skill_id        TEXT NOT NULL REFERENCES skill(id),
    name            TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT '',
    revision        INTEGER NOT NULL DEFAULT 1,
    root_suite_id   TEXT,                        -- 首次创建时为自身 id
    suite_hash      TEXT,
    rubric          TEXT NOT NULL DEFAULT '{}',
    critical_checks TEXT NOT NULL DEFAULT '[]',
    thresholds      TEXT NOT NULL DEFAULT '{}',
    objective       TEXT NOT NULL DEFAULT 'quality',  -- quality|efficiency
    status          TEXT NOT NULL DEFAULT 'draft',    -- draft|frozen
    frozen_at       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_suite_skill ON eval_suite(skill_id, revision);
CREATE UNIQUE INDEX IF NOT EXISTS ux_suite_identity ON eval_suite(skill_id, revision);

CREATE TABLE IF NOT EXISTS eval_case (
    id                  TEXT PRIMARY KEY,
    suite_id            TEXT NOT NULL REFERENCES eval_suite(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    prompt              TEXT NOT NULL,
    input_files         TEXT NOT NULL DEFAULT '[]',   -- [{"path","content"}]
    fixture_hash        TEXT NOT NULL,
    rubric_hash         TEXT NOT NULL,
    role                TEXT NOT NULL DEFAULT 'regression',  -- regression|holdout
    completion_criteria TEXT NOT NULL DEFAULT '[]',
    verifier            TEXT NOT NULL DEFAULT '{}',
    expected_answers    TEXT NOT NULL DEFAULT '{}',   -- 仅 verifier/judge 可读
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_case_suite ON eval_case(suite_id, role);

CREATE TABLE IF NOT EXISTS eval_job (
    id                   TEXT PRIMARY KEY,
    profile_id           TEXT NOT NULL REFERENCES profile(id),
    proposal_id          TEXT NOT NULL REFERENCES proposal(id),
    suite_id             TEXT NOT NULL REFERENCES eval_suite(id),
    baseline_version_id  TEXT NOT NULL,
    candidate_version_id TEXT NOT NULL,
    config_hash          TEXT NOT NULL,
    config               TEXT NOT NULL DEFAULT '{}',
    budget               TEXT NOT NULL DEFAULT '{}',
    usage                TEXT NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL DEFAULT 'queued',
    verdict              TEXT,
    verdict_detail       TEXT NOT NULL DEFAULT '{}',
    report               TEXT NOT NULL DEFAULT '{}',
    error_code           TEXT,
    cancel_requested     INTEGER NOT NULL DEFAULT 0,
    checkpoint           TEXT NOT NULL DEFAULT '{}',
    lease_owner          TEXT,
    lease_expires_at     TEXT,
    heartbeat_at         TEXT,
    attempts             INTEGER NOT NULL DEFAULT 0,
    started_at           TEXT,
    finished_at          TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_eval_job_proposal ON eval_job(proposal_id, created_at);

CREATE TABLE IF NOT EXISTS eval_result (
    id                TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL REFERENCES eval_job(id) ON DELETE CASCADE,
    case_id           TEXT NOT NULL REFERENCES eval_case(id),
    repetition        INTEGER NOT NULL DEFAULT 1,
    baseline_run_id   TEXT REFERENCES run(id),
    candidate_run_id  TEXT REFERENCES run(id),
    result_status     TEXT NOT NULL DEFAULT 'pending',
    baseline_checks   TEXT NOT NULL DEFAULT '{}',
    candidate_checks  TEXT NOT NULL DEFAULT '{}',
    baseline_score    REAL,
    candidate_score   REAL,
    judge             TEXT NOT NULL DEFAULT '{}',
    notes             TEXT,
    attempt           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_eval_result ON eval_result(job_id, case_id, repetition);

-- ------------------------------------------------------------------ 作业 / 预算 / 幂等 / 审计
CREATE TABLE IF NOT EXISTS job (
    id               TEXT PRIMARY KEY,
    type             TEXT NOT NULL,                -- run.execute|learning.extract|eval.execute|...
    status           TEXT NOT NULL DEFAULT 'queued',
    priority         INTEGER NOT NULL DEFAULT 100,
    payload          TEXT NOT NULL DEFAULT '{}',
    budget           TEXT NOT NULL DEFAULT '{}',
    result           TEXT,
    error_code       TEXT,
    error_message    TEXT,
    idempotency_key  TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    lease_owner      TEXT,
    lease_expires_at TEXT,
    heartbeat_at     TEXT,
    checkpoint       TEXT NOT NULL DEFAULT '{}',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    run_id           TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_job_idem ON job(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_job_status ON job(status, priority, created_at);

CREATE TABLE IF NOT EXISTS budget_ledger (
    scope_id            TEXT PRIMARY KEY,          -- run_id 或 job_id
    scope_kind          TEXT NOT NULL,
    spec                TEXT NOT NULL DEFAULT '{}',
    reserved_calls      INTEGER NOT NULL DEFAULT 0,
    reserved_tokens     INTEGER NOT NULL DEFAULT 0,
    used_calls          INTEGER NOT NULL DEFAULT 0,
    used_tokens         INTEGER NOT NULL DEFAULT 0,
    used_tool_actions   INTEGER NOT NULL DEFAULT 0,
    unknown_usage       INTEGER NOT NULL DEFAULT 0,
    external_calls      INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',
    exceeded_type       TEXT
);

CREATE TABLE IF NOT EXISTS daily_usage (
    day          TEXT PRIMARY KEY,
    model_calls  INTEGER NOT NULL DEFAULT 0,
    tokens       INTEGER NOT NULL DEFAULT 0,
    unknown_usage INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    scope         TEXT NOT NULL,
    key           TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'started',   -- started|done
    response      TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS audit_event (
    id               TEXT PRIMARY KEY,
    profile_id       TEXT,
    entity_type      TEXT NOT NULL,
    entity_id        TEXT NOT NULL,
    action           TEXT NOT NULL,
    actor            TEXT NOT NULL DEFAULT 'owner',
    before_state     TEXT NOT NULL DEFAULT '{}',
    after_state      TEXT NOT NULL DEFAULT '{}',
    permission_scope TEXT NOT NULL DEFAULT '{}',
    reason           TEXT,
    correlation_id   TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_event(entity_id, created_at);

CREATE TABLE IF NOT EXISTS product_event (
    id         TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL,
    name       TEXT NOT NULL,
    profile_id TEXT,
    params     TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_product_event ON product_event(event_id);
CREATE INDEX IF NOT EXISTS ix_product_event_name ON product_event(name, created_at);

CREATE TABLE IF NOT EXISTS data_op (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,                   -- backup|restore|export|clear|import
    scope         TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'started',
    manifest_path TEXT,
    manifest      TEXT NOT NULL DEFAULT '{}',
    result        TEXT NOT NULL DEFAULT '{}',
    actor         TEXT NOT NULL DEFAULT 'owner',
    created_at    TEXT NOT NULL,
    finished_at   TEXT
);
