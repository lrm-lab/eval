PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA wal_autocheckpoint = 1000;

-- 1. 用户、项目归属与 6 项明细 Scope 鉴权
CREATE TABLE IF NOT EXISTS users (
    user_id        TEXT PRIMARY KEY,
    username       TEXT UNIQUE NOT NULL,
    display_name   TEXT NOT NULL,
    password_hash  TEXT NOT NULL,          -- argon2id / scrypt
    role           TEXT NOT NULL CHECK (role IN ('viewer', 'researcher', 'admin')),
    scopes_json    TEXT NOT NULL DEFAULT '[]', -- ["trace:read","gateway:read","verifier_log:read","dataset:answer_read","dataset:holdout_read","direct_vlm_qa:read"]
    projects_json  TEXT NOT NULL DEFAULT '["default"]', -- 资源 project_id 授权列表，显式控权
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 2. Benchmark 稳定身份（一级档案 URL /benchmarks/{benchmark_slug}，统一 track_slug 机器键）
CREATE TABLE IF NOT EXISTS benchmarks (
    benchmark_slug     TEXT PRIMARY KEY,   -- URL-safe 稳定标识，如 deepswe, terminal-bench, gaia, wildclawbench, cmmmu
    display_name       TEXT NOT NULL,
    track_slug         TEXT NOT NULL,      -- 统一机器键: agentic | llm-text | vision-image | synthetic | extended | custom
    track_display_name TEXT NOT NULL,      -- 展示名: Agentic / LLM — Text / Vision — Image / Synthetic / Extended / Custom
    goal_type          TEXT NOT NULL DEFAULT 'extended_baseline', -- optimize_gain / non_regression_guardrail / synthesis_loop / extended_baseline
    capability         TEXT NOT NULL,
    modality           TEXT NOT NULL DEFAULT 'agent', -- agent / text / vision (前向兼容)
    language           TEXT NOT NULL DEFAULT 'en',    -- en / zh / multi
    status             TEXT NOT NULL,      -- integrated / legacy_only / raw_visible_unintegrated / pending_version_pin
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    updated_at         TEXT NOT NULL
);

-- 3. 数据集版本 + Split 发布实体（支持同一 Benchmark 多版本/多 Split 共存同名 task_id）
CREATE TABLE IF NOT EXISTS dataset_releases (
    dataset_release_id TEXT PRIMARY KEY,   -- URL-safe 稳定键，如 cmmmu__2026-11-24__val 或 terminal-bench__2.1__default
    benchmark_slug     TEXT NOT NULL REFERENCES benchmarks(benchmark_slug) ON DELETE CASCADE,
    dataset_version    TEXT NOT NULL,
    split              TEXT NOT NULL DEFAULT 'default',
    runner             TEXT NOT NULL,      -- harbor / direct / vlmevalkit / legacy / none (前向兼容)
    known_task_count   INTEGER,
    metric_name        TEXT NOT NULL DEFAULT 'reward',
    default_protocol   TEXT NOT NULL,
    scale_type         TEXT NOT NULL DEFAULT 'ratio_0_1', -- ratio_0_1 / pct_0_100 / score_0_1000
    media_rule_ver     TEXT,
    source_approval    TEXT NOT NULL DEFAULT 'pending',   -- approved_public / approved_restricted / pending
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    updated_at         TEXT NOT NULL,
    UNIQUE (benchmark_slug, dataset_version, split)
);

-- 4. 已核验来源别名映射表（把带 run_scope/空格括号的 raw_dataset_name 映射至固定版本+split）
CREATE TABLE IF NOT EXISTS dataset_source_aliases (
    alias_id           TEXT PRIMARY KEY,
    raw_dataset_name   TEXT NOT NULL,      -- 如 "CMMMU validation smoke (6 items)"
    source_namespace   TEXT NOT NULL DEFAULT '*',
    runner             TEXT NOT NULL DEFAULT '*',
    dataset_release_id TEXT NOT NULL REFERENCES dataset_releases(dataset_release_id) ON DELETE CASCADE,
    inferred_run_scope TEXT,               -- 可选：如 smoke / subset / full_split
    verified_by        TEXT NOT NULL,
    verified_at        TEXT NOT NULL,
    UNIQUE (raw_dataset_name, source_namespace, runner)
);

-- 5. 题库任务定义（按 dataset_release_id + task_id 为主键，允许不同版本/split 保存同名题）
CREATE TABLE IF NOT EXISTS task_definitions (
    dataset_release_id TEXT NOT NULL REFERENCES dataset_releases(dataset_release_id) ON DELETE CASCADE,
    task_id            TEXT NOT NULL,
    task_dir           TEXT,
    task_checksum      TEXT,
    split              TEXT,
    source             TEXT,
    environment        TEXT,
    difficulty         TEXT,
    category           TEXT,
    language           TEXT,
    tags_json          TEXT NOT NULL DEFAULT '[]',
    resources_json     TEXT NOT NULL DEFAULT '{}',
    extra_json         TEXT NOT NULL DEFAULT '{}',
    instruction_ref    TEXT,
    PRIMARY KEY (dataset_release_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_defs_meta ON task_definitions(dataset_release_id, difficulty, category, split);

-- 6. 统一批次主表（URL-safe batch_id + 内部 source_locator + 同代 generation_id + 项目/属主归属）
CREATE TABLE IF NOT EXISTS batches (
    batch_id                    TEXT PRIMARY KEY,       -- URL-safe 稳定 ID，如 b_8f4c1a90d3e27b51c408（绝不含 '/' 或 ':'）
    source_locator              TEXT UNIQUE NOT NULL,   -- 原始完整来源标识，如 native_harbor:terminal-bench2.1/jhpPiV5__Qwen3_8-27B
    source_namespace            TEXT NOT NULL,          -- harbor_runs / native_harbor / legacy_json / legacy_csv
    batch_name                  TEXT NOT NULL,
    project_id                  TEXT NOT NULL DEFAULT 'default', -- 资源权限归属项目
    owner_id                    TEXT NOT NULL DEFAULT 'system',  -- 资源属主
    evidence_approval           TEXT NOT NULL DEFAULT 'pending', -- approved_public / approved_restricted / pending
    is_running_placeholder      INTEGER NOT NULL DEFAULT 0,
    internal_source_fingerprint TEXT,                 -- 未脱敏源位置哈希，仅用于占位回填，严禁输出到 API DTO
    native_job_id               TEXT,
    source_host_redacted        TEXT,
    source_path_redacted        TEXT,                   -- 通用前缀脱敏（含 /home/* 与 /tmp/*）
    source_tags_json            TEXT NOT NULL DEFAULT '[]',
    benchmark_slug              TEXT REFERENCES benchmarks(benchmark_slug),
    dataset_release_id          TEXT REFERENCES dataset_releases(dataset_release_id),
    identity_status             TEXT NOT NULL DEFAULT 'mapped', -- mapped / unmapped / running_placeholder
    raw_dataset_name            TEXT,                   -- 保留原始名称（含空格/括号）
    model_route                 TEXT,
    model_canonical             TEXT,
    model_provider              TEXT,
    checkpoint                  TEXT,
    thinking_mode               TEXT,
    harness                     TEXT,
    harness_version             TEXT,
    runner                      TEXT NOT NULL DEFAULT 'harbor', -- 前向兼容，无硬 CHECK
    run_scope                   TEXT NOT NULL CHECK (run_scope IN ('full_split', 'subset', 'smoke', 'reference', 'unknown')),
    scoring_protocol            TEXT NOT NULL DEFAULT 'unknown',
    fault_policy                TEXT NOT NULL DEFAULT 'exclude_errors_valid_mean',
    official_status             TEXT NOT NULL DEFAULT 'unscored',
    batch_status                TEXT,
    report_origin               TEXT NOT NULL DEFAULT 'published_report', -- published_report / recomputed_from_episodes / running_placeholder / legacy_adapter
    generation_id               TEXT NOT NULL,          -- 不可变发布代标识（老批次按文件指纹派生）
    trial_digests_sha256        TEXT,                   -- trial-digests.jsonl 摘要，支撑惰性读取同代校验
    absent_optional_json        TEXT NOT NULL DEFAULT '[]',
    unique_tasks                INTEGER NOT NULL DEFAULT 0,
    planned                     INTEGER,
    recorded                    INTEGER NOT NULL DEFAULT 0,
    scored                      INTEGER NOT NULL DEFAULT 0,
    unscored                    INTEGER NOT NULL DEFAULT 0,
    coverage                    REAL,
    valid_mean                  REAL,
    passed                      INTEGER,
    pass_threshold              REAL,
    outcomes_json               TEXT NOT NULL DEFAULT '{}',
    failure_kinds_json          TEXT NOT NULL DEFAULT '{}',
    vlmeval_metrics_json        TEXT,
    run_config_json             TEXT,
    token_semantics             TEXT NOT NULL DEFAULT 'none',
    usage_json                  TEXT NOT NULL DEFAULT '{}',
    checks_json                 TEXT NOT NULL DEFAULT '{}',
    has_gateway                 INTEGER NOT NULL DEFAULT 0,
    has_selection               INTEGER NOT NULL DEFAULT 0,
    started_at                  TEXT,
    finished_at                 TEXT,
    content_digest              TEXT NOT NULL,
    indexed_at                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_query ON batches(benchmark_slug, dataset_release_id, run_scope, scoring_protocol, model_canonical, harness);
CREATE INDEX IF NOT EXISTS idx_batches_runner ON batches(runner, is_running_placeholder, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_batches_placeholder ON batches(internal_source_fingerprint) WHERE is_running_placeholder = 1;

-- 7. 批次本地单次 Trial 明细（始终保留每批次自身原始记录与本地分母，不因全局去重丢弃）
CREATE TABLE IF NOT EXISTS episodes (
    batch_id               TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    trial_id               TEXT NOT NULL,
    canonical_trial_key    TEXT NOT NULL, -- 有 UUID 用 UUID；无 UUID 必须包含真实 run_id(batch_id)+checkpoint+harness_version（同配置两次重跑互不冲突）
    generation_id          TEXT NOT NULL, -- 绑定所属批次发布代，防止迟读跨代拼接
    source_tags_json       TEXT NOT NULL DEFAULT '[]',
    task_id                TEXT NOT NULL,
    task_checksum          TEXT,
    attempt_index          INTEGER NOT NULL DEFAULT 1,
    retry_ids_json         TEXT NOT NULL DEFAULT '[]',
    status                 TEXT NOT NULL, -- scored / failed / timeout / cancelled / unscored (前向兼容)
    reward                 REAL,
    reward_valid           INTEGER NOT NULL DEFAULT 0,   -- 按本批次 scoring_protocol 判定
    native_reward_valid    INTEGER,                      -- 原生 Harbor TrialResult.reward_valid（允许与 exception_info 共存，如 TB2.1 230 条）
    native_reward          REAL,                         -- 原生 Harbor 主分
    exception_type         TEXT,
    exception_policy       TEXT,                         -- 原生 Harbor exception_info.policy
    http_status            INTEGER,
    failure_kind           TEXT,
    failure_summary        TEXT,
    failure_host_redacted  TEXT,
    failure_log_redacted   TEXT,
    verifier_rewards_json  TEXT NOT NULL DEFAULT '{}',
    -- M0 一步到位预留的 6 个关键扩展列：
    started_at             TEXT,
    finished_at            TEXT,
    cached_tokens          INTEGER,
    reasoning_tokens       INTEGER,
    tool_call_count        INTEGER,
    phase_timings_json     TEXT,
    -- 资源、成本与语义元数据：
    input_tokens           INTEGER,
    output_tokens          INTEGER,
    token_semantics        TEXT NOT NULL DEFAULT 'none',
    reasoning_key_source   TEXT,
    cost_usd               REAL,
    cost_source            TEXT NOT NULL DEFAULT 'none', -- recorded_native / billed_reconciled / estimated / none
    duration_sec           REAL,
    trajectory_status      TEXT,
    gateway_status         TEXT,
    training_status        TEXT,
    has_verifier_output    INTEGER NOT NULL DEFAULT 0,
    has_artifacts          INTEGER NOT NULL DEFAULT 0,
    has_inline_media       INTEGER NOT NULL DEFAULT 0,
    evidence_refs_json     TEXT NOT NULL DEFAULT '{}',   -- 绑定 {kind: {evidence_id, generation_id, sha256, storage_tier}}
    observable_clues_json  TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (batch_id, trial_id)
);
CREATE INDEX IF NOT EXISTS idx_episodes_trial_key ON episodes(canonical_trial_key);
CREATE INDEX IF NOT EXISTS idx_episodes_task ON episodes(batch_id, task_id, status);
CREATE INDEX IF NOT EXISTS idx_episodes_failure ON episodes(failure_kind, batch_id);

-- 8. 按评分协议的候选评分观测表（严守“先按项目授权与选定来源过滤，再在授权集合内按协议去重”！）
CREATE TABLE IF NOT EXISTS trial_protocol_scores (
    canonical_trial_key    TEXT NOT NULL,
    scoring_protocol       TEXT NOT NULL, -- 如 harbor-native-reward-valid-v1 / lrm-eval-report-v1 / gaia-official-scorer
    batch_id               TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    trial_id               TEXT NOT NULL,
    project_id             TEXT NOT NULL DEFAULT 'default', -- 与所属 batch 同步，供查询第一步权限裁剪
    source_namespace       TEXT NOT NULL, -- harbor_runs / native_harbor / legacy_json / legacy_csv
    source_priority_rank   INTEGER NOT NULL DEFAULT 10,     -- 同授权集同协议内确定性决胜序（越小越优先）
    dataset_release_id     TEXT,
    task_id                TEXT NOT NULL,
    status                 TEXT NOT NULL,
    reward                 REAL,
    reward_valid           INTEGER NOT NULL DEFAULT 0,
    exception_type         TEXT,
    exception_policy       TEXT,
    source_tags_json       TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (canonical_trial_key, scoring_protocol, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_tps_auth_dedup ON trial_protocol_scores(project_id, scoring_protocol, dataset_release_id, canonical_trial_key, source_priority_rank);

-- 9. 2026-11-20 历史总榜表 & 冷层文件切片缓存
CREATE TABLE IF NOT EXISTS legacy_leaderboard_rows (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    benchmark          TEXT NOT NULL,
    metric             TEXT NOT NULL,
    benchmark_slug     TEXT,
    dataset_release_id TEXT,
    model_col          TEXT NOT NULL,
    model_canonical    TEXT NOT NULL,
    checkpoint         TEXT,
    thinking_mode      TEXT,
    harness            TEXT NOT NULL DEFAULT 'direct',
    raw_score          REAL NOT NULL,
    norm_score_100     REAL NOT NULL,
    scale_type         TEXT NOT NULL,
    source_file        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cold_file_cache (
    file_path     TEXT PRIMARY KEY,
    cache_kind    TEXT NOT NULL, -- legacy_json_dict / legacy_jsonl / bundle_tar
    mtime_ns      INTEGER NOT NULL,
    size_bytes    INTEGER NOT NULL,
    item_count    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL, -- ready / running / error
    error_message TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_sample_offsets (
    file_path          TEXT NOT NULL REFERENCES cold_file_cache(file_path) ON DELETE CASCADE,
    sample_id          TEXT NOT NULL,
    start_offset       INTEGER NOT NULL,
    end_offset         INTEGER NOT NULL,
    status             TEXT,
    reward             REAL,
    category           TEXT,
    difficulty         TEXT,
    media_match_status TEXT,               -- unique_hit / missing / ambiguous_multi_hit / unregistered_source
    media_refs_json    TEXT NOT NULL DEFAULT '[]',
    meta_json          TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (file_path, sample_id)
);

-- 10. 合成选题、官方分、自定义样本集、保存对比与规范化对比会话（绑定快照与权限归属）
CREATE TABLE IF NOT EXISTS selection_tasks (
    batch_id       TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    task_id        TEXT NOT NULL,
    task_directory TEXT,
    attempts       INTEGER NOT NULL,
    scored         INTEGER NOT NULL,
    passed         INTEGER NOT NULL,
    excluded_json  TEXT NOT NULL DEFAULT '{}',
    lane           TEXT NOT NULL CHECK (lane IN ('grpo', 'teacher', 'regression', 'review')),
    PRIMARY KEY (batch_id, task_id)
);

CREATE TABLE IF NOT EXISTS official_results (
    batch_id     TEXT PRIMARY KEY REFERENCES batches(batch_id) ON DELETE CASCADE,
    dataset      TEXT NOT NULL,
    metric       TEXT NOT NULL,
    score        REAL NOT NULL,
    denominator  INTEGER NOT NULL,
    source       TEXT NOT NULL,
    origin       TEXT NOT NULL DEFAULT 'batch_file', -- batch_file / manual_attach
    fault_policy TEXT NOT NULL DEFAULT 'official_protocol',
    attached_by  TEXT NOT NULL,
    attached_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sample_sets (
    set_id             TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    description        TEXT,
    dataset_release_id TEXT NOT NULL,
    task_ids_json      TEXT NOT NULL,
    filter_spec_json   TEXT,
    project_id         TEXT NOT NULL DEFAULT 'default',
    owner_id           TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_comparisons (
    short_id         TEXT PRIMARY KEY,       -- 用于 /compare/s/:shortId 与 comparison_id
    name             TEXT NOT NULL,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'snapshot', 'ephemeral')),
    project_id       TEXT NOT NULL DEFAULT 'default',
    owner_id         TEXT NOT NULL,
    config_json      TEXT NOT NULL,          -- 完整规范化查询（含 bound batch_ids/generation_ids/scoring_protocol/align_mode/agg）
    snapshot_json    TEXT,                   -- mode='snapshot'/'ephemeral' 冻结聚合结果与参与批次指纹（严禁包含无权明细正文）
    methodology_ver  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 11. 受控执行作业、实时事件流、Direct 原生验证台、运行参数对照台账与索引告警
CREATE TABLE IF NOT EXISTS exec_jobs (
    job_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL, -- harbor / harbor_gateway / vlmevalkit / direct / synth_generate / synth_check
    project_id      TEXT NOT NULL DEFAULT 'default',
    owner_id        TEXT NOT NULL,
    batch_name      TEXT,
    batch_id        TEXT,
    status          TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    command_preview TEXT NOT NULL,
    runner_id       TEXT,
    exit_code       INTEGER,
    last_event_seq  INTEGER NOT NULL DEFAULT 0,
    log_ref         TEXT,          -- 位于隔离 Runner 私有目录的证据引用，绝不落 0777 CIFS
    notify_json     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS exec_batch_reservations (
    source_namespace TEXT NOT NULL,
    batch_name       TEXT NOT NULL,
    job_id           TEXT NOT NULL UNIQUE REFERENCES exec_jobs(job_id) ON DELETE CASCADE,
    PRIMARY KEY (source_namespace, batch_name)
);

CREATE TABLE IF NOT EXISTS exec_job_events (
    job_id       TEXT NOT NULL REFERENCES exec_jobs(job_id) ON DELETE CASCADE,
    seq_id       INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (job_id, seq_id)
);

CREATE TABLE IF NOT EXISTS direct_lab_runs (
    run_id          TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL DEFAULT 'default',
    owner_id        TEXT NOT NULL,
    suite_id        TEXT NOT NULL,
    cases_sha256    TEXT NOT NULL,           -- 题集指纹，支撑同题 A/B 对照校验
    scorer_id       TEXT NOT NULL,           -- 白名单评分器 ID（'exact_match' 或受审 score.py）
    scorer_sha256   TEXT NOT NULL,           -- 评分器指纹
    adapter_variant TEXT NOT NULL DEFAULT 'native_direct_v1', -- native_direct_v1 / extended_adapter_pending
    model_route     TEXT NOT NULL,
    gateway_profile TEXT NOT NULL,
    params_json     TEXT NOT NULL DEFAULT '{}',
    summary_json    TEXT NOT NULL DEFAULT '{}',
    batch_id        TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS param_compare_groups (
    group_id           TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL DEFAULT 'default',
    owner_id           TEXT NOT NULL,
    test_topic         TEXT NOT NULL,
    label              TEXT NOT NULL,
    dataset_release_id TEXT NOT NULL,
    batch_ids_json     TEXT NOT NULL,
    notes              TEXT,
    created_at         TEXT NOT NULL
);

-- 离线校验与算分报告表（支撑 /toolkit/verify 与 GET/DELETE /api/v1/toolkit/offline-reports）
CREATE TABLE IF NOT EXISTS offline_reports (
    report_id          TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL DEFAULT 'default',
    owner_id           TEXT NOT NULL,
    report_name        TEXT NOT NULL,
    upload_format      TEXT NOT NULL CHECK (upload_format IN ('shared_batch_or_episodes', 'native_harbor_job', 'summary_only')),
    adapter_used       TEXT NOT NULL,        -- report_py / native_harbor_adapter / summary_passthrough
    model_name         TEXT,
    dataset_name       TEXT,
    staging_ref        TEXT,                 -- 指向 0700 隔离待审区内部引用（绝不落 0777 CIFS）
    summary_json       TEXT NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'ready', -- ready / error / expired
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_warnings (
    warning_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tier          TEXT NOT NULL,
    severity      TEXT NOT NULL,
    code          TEXT NOT NULL,
    target_path   TEXT NOT NULL, -- 脱敏路径
    message       TEXT NOT NULL,
    detected_at   TEXT NOT NULL
);

-- 12. 全局 ⌘K FTS5 全文检索引擎（带 project_id 与 required_scope 查询时双重过滤）
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    entity_type, entity_id, title, subtitle, body,
    project_id UNINDEXED,
    required_scope UNINDEXED,
    tokenize = 'unicode61'
);
