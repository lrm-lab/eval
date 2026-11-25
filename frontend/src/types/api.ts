export interface AuthUser {
  user_id: string;
  username: string;
  display_name: string;
  role: 'viewer' | 'researcher' | 'admin';
  scopes: string[];
  projects: string[];
}

export interface ApiErrorPayload {
  error: {
    code: string;
    message: string;
    detail?: Record<string, unknown>;
    request_id?: string;
  };
}

export interface OfficialResult {
  dataset?: string;
  metric: string;
  score: number;
  denominator: number;
  source: string;
  origin?: string;
  fault_policy?: string;
}

export interface BatchDTO {
  batch_id: string;
  source_locator: string;
  source_namespace: string;
  batch_name: string;
  project_id: string;
  owner_id: string;
  evidence_approval: string;
  is_running_placeholder: boolean;
  source_host_redacted?: string | null;
  source_path_redacted?: string | null;
  source_tags?: string[];
  benchmark_slug?: string | null;
  dataset_release_id?: string | null;
  identity_status: string;
  raw_dataset_name?: string | null;
  model_route?: string | null;
  model_canonical?: string | null;
  model_provider?: string | null;
  checkpoint?: string | null;
  thinking_mode?: string | null;
  harness?: string | null;
  harness_version?: string | null;
  runner: string;
  run_scope: string;
  scoring_protocol: string;
  fault_policy: string;
  official_status: string;
  batch_status?: string | null;
  report_origin: string;
  generation_id: string;
  unique_tasks: number;
  planned?: number | null;
  recorded: number;
  scored: number;
  unscored: number;
  coverage?: number | null;
  valid_mean?: number | null;
  passed?: number | null;
  pass_threshold?: number | null;
  outcomes?: Record<string, number>;
  failure_kinds?: Record<string, number>;
  vlmeval_metrics?: Record<string, unknown>[] | Record<string, unknown> | null;
  run_config?: Record<string, unknown> | null;
  token_semantics: string;
  usage?: Record<string, { observed: number; total: number | null }>;
  has_gateway: boolean;
  has_selection: boolean;
  started_at?: string | null;
  finished_at?: string | null;
  indexed_at: string;
  modality?: string | null;
  language?: string | null;
  track_slug?: string | null;
  official_result?: OfficialResult | null;
  totals?: {
    unique_tasks: number;
    planned: number | null;
    recorded: number;
    scored: number;
    unscored: number;
    coverage: number | null;
    valid_mean: number | null;
    passed: number | null;
    pass_threshold: number | null;
    outcomes: Record<string, number>;
  };
}

export interface SystemOverviewResponse {
  methodology_version: string;
  kpis: {
    benchmarks: number;
    dataset_releases: number;
    readable_batches: number;
    running_placeholders: number;
    total_recorded_trials: number;
    total_scored_trials: number;
    dedup_recorded_trials: number;
    dedup_scored_trials: number;
    model_harness_combos: number;
    index_warnings: number;
  };
  recent_batches: BatchDTO[];
}

export type CompareIssue =
  | 'not_integrated'
  | 'pending_dataset_version'
  | 'different_scoring_protocol'
  | 'different_dataset_release'
  | 'no_common_valid_tasks'
  | 'missing_comparable_scores';

export interface VerdictItem {
  benchmark_slug: string;
  dataset_release_id: string | null;
  metric_name?: string | null;
  track_slug: string;
  scoring_protocol: string | null;
  denominator_kind: string | null;
  baseline_score: number | null;
  candidate_score: number | null;
  delta_pp: number | null;
  rel_pct: number | null;
  coverage: string;
  status: 'pass' | 'regressed' | 'undeterminable';
  reason: string;
  reason_code?: CompareIssue;
}

export interface GainGuardrailVerdict {
  baseline_subject_id: string;
  candidate_subject_id: string | null;
  regression_threshold_pp: number;
  agentic_gains: VerdictItem[];
  guardrails: VerdictItem[];
}

export interface LeaderboardBenchmarkCell {
  benchmark_slug: string;
  dataset_release_id: string;
  raw_dataset_name: string;
  score: number | null;
  raw_score: number | null;
  scale_type: string;
  run_scope: string;
  scoring_protocol: string;
  official_status: string;
  source_type: string;
  scored?: number;
  planned?: number;
  batch_id: string | null;
}

export interface TokenTelemetry {
  semantics: string;
  input: number | null;
  cached: number | null;
  output: number | null;
  reasoning: number | null;
  reasoning_key_source?: string | null;
}

export interface LeaderboardItem {
  rank: number;
  subject_id: string;
  model_canonical: string;
  harness: string;
  harness_version: string | null;
  runner: string;
  checkpoint?: string | null;
  thinking_mode?: string | null;
  has_gateway?: boolean;
  has_atif?: boolean;
  latest_at?: string | null;
  avg_duration_sec?: number | null;
  tokens?: TokenTelemetry;
  comparative_macro_avg: number | null;
  comparative_bench_count: number;
  self_coverage_avg: number | null;
  self_bench_count: number;
  benchmarks: Record<string, LeaderboardBenchmarkCell>;
}

export interface BestByTaskEntry {
  dimension: string;
  benchmark_slug: string;
  dataset_release_id: string;
  model_canonical: string;
  harness: string;
  runner: string;
  run_scope: string;
  scoring_protocol: string;
  denominator_kind: string;
  score: number;
  coverage: string;
  batch_id: string | null;
}

export interface LeaderboardResponse {
  methodology_version: string;
  track_slug: string;
  track_display_name: string;
  common_comparable_benchmarks: string[];
  best_by_task: BestByTaskEntry[];
  available_filters?: {
    harnesses: string[];
    runners: string[];
    protocols: string[];
  };
  items: LeaderboardItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface EfficiencyScatterPoint {
  batch_id: string;
  batch_name: string;
  model_canonical: string | null;
  harness: string | null;
  series_id: string | null;
  series_order: number | null;
  score_pct: number;
  x_value: number | null;
  partial_x_value: number | null;
  x_coverage_tasks: number;
  common_valid_n: number;
  full_coverage: string;
  eligible_for_pareto: boolean;
  on_pareto: boolean;
}

export interface EfficiencyScatterResponse {
  methodology_version: string;
  benchmark_slug: string;
  dataset_release_id: string;
  raw_dataset_name: string;
  scoring_protocol: string;
  task_set_policy: string;
  common_valid_tasks: number;
  x_axis: string;
  x_axis_coverage: Record<
    string,
    {
      total_batches: number;
      batches_with_any_records: number;
      plottable_full_coverage_points: number;
      partial_record_coverage?: string;
      note?: string;
    }
  >;
  points: EfficiencyScatterPoint[];
  series_connectors: Array<{ series_id: string; label?: string; batch_ids: string[] }>;
  pareto_frontier_batch_ids: string[];
}

export interface DatasetReleaseDTO {
  dataset_release_id: string;
  benchmark_slug: string;
  dataset_version: string;
  split: string;
  runner: string;
  known_task_count: number | null;
  metric_name: string;
  default_protocol: string;
  scale_type: string;
  media_rule_ver?: string | null;
  source_approval: string;
  batch_count?: number;
}

export interface BenchmarkTreeNode {
  benchmark_slug: string;
  display_name: string;
  track_slug: string;
  track_display_name: string;
  goal_type: string;
  capability: string;
  modality: string;
  language: string;
  status: string;
  metadata?: { core_14?: boolean; [k: string]: unknown };
  releases: DatasetReleaseDTO[];
}

export interface DatasetTreeTrack {
  track_slug: string;
  display_name: string;
  zh_name?: string;
  goal_type: string;
  default_visible: boolean;
  benchmarks: BenchmarkTreeNode[];
}

export interface AttemptDot {
  trial_id: string;
  status: string;
  reward: number | null;
  failure_kind?: string | null;
  exception_type?: string | null;
  duration_sec?: number | null;
}

export interface BatchTaskRow {
  task_id: string;
  planned: number;
  recorded: number;
  scored: number;
  passed: number;
  valid_mean: number | null;
  pass_at_1: number | null;
  attempt_dots: AttemptDot[];
  selection_lane: string | null;
  [k: string]: unknown;
}

export interface BatchTasksResponse {
  batch_id: string;
  unique_tasks: number;
  total_attempts: number;
  pass_at_k_summary: Record<
    string,
    {
      value: number | null;
      computable_tasks: number;
      total_tasks: number;
      ci95: [number, number] | null;
      note: string | null;
    }
  >;
  items: BatchTaskRow[];
  redacted?: boolean;
  hidden_holdout_tasks?: number;
  redacted_reason?: string | null;
  methodology_version: string;
}

export interface TrialDetailDTO {
  batch_id: string;
  generation_id: string;
  trial_id: string;
  canonical_trial_key: string;
  task_id: string;
  task_checksum: string | null;
  status: string;
  reward: number | null;
  reward_valid: boolean;
  native_reward_valid: boolean | null;
  native_reward: number | null;
  exception_type: string | null;
  exception_policy: string | null;
  http_status: number | null;
  failure: {
    kind: string | null;
    summary: string | null;
    host_redacted: string | null;
    log_redacted: string | null;
  } | null;
  verifier_rewards: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  duration_sec: number | null;
  phase_timings: Record<string, number | null> | null;
  tokens: TokenTelemetry;
  cost: { amount_usd: number | null; source: string };
  tool_call_count: number | null;
  trajectory_status: string | null;
  gateway_status: string | null;
  training_status: string | null;
  has_verifier_output: boolean;
  has_artifacts: boolean;
  has_inline_media: boolean;
  evidence_approval: string;
  qa?: {
    vlmeval_result?: Record<string, unknown>;
    direct_result?: Record<string, unknown>;
    media_count: number;
  };
  qa_redacted?: boolean;
  qa_redacted_reason?: string;
  qa_unavailable_reason?: string;
  methodology_version: string;
}

export interface PairedToolCall {
  tool_call_id: string;
  function_name: string | null;
  arguments: unknown;
  observation: {
    content: string | null;
    is_error: boolean;
  } | null;
}

export interface AtifStep {
  step_id: number;
  source: string | null;
  timestamp: string | null;
  message: unknown;
  reasoning_content: string | null;
  paired_tools: PairedToolCall[];
  metrics?: Record<string, unknown> | null;
}

export interface CompareSubjectRef {
  id: string;
  kind: 'batch' | 'legacy_model';
  batch_id?: string;
  expected_generation_id?: string;
  model_col?: string;
  model_cols?: string[];
  alias: string;
  subtitle?: string;
}

export interface CompareQueryResponse {
  comparison_id: string;
  methodology_version: string;
  bound_snapshot: {
    scoring_protocol: string | null;
    align_mode: string;
    multi_attempt_agg: string;
    regression_threshold_pp: number;
    sample_set_id: string | null;
    batches: Array<{ subject_id: string; batch_id: string; generation_id: string }>;
    legacy_subjects?: Array<{ subject_id: string; alias: string; model_cols: string[] }>;
  };
  gain_vs_guardrail_verdict: GainGuardrailVerdict | null;
  auto_insights: Array<{ text: string; filter_link?: Record<string, unknown> }>;
  comparative_summary: {
    common_comparable_benchmarks: string[];
    by_subject: Record<
      string,
      {
        comparative_macro_avg: number | null;
        is_best: boolean;
        delta_pp: number | null;
        self_full_batch_avg: number | null;
        self_bench_count: number;
      }
    >;
  };
  rows: Array<{
    benchmark_slug: string;
    dataset_release_id: string;
    raw_dataset_name: string;
    track_slug?: string;
    goal_type?: string;
    scoring_protocol: string;
    legacy_metric?: string | null;
    comparable: boolean;
    comparison_issue?: CompareIssue | null;
    version_drift?: { drifted_task_count: number };
    intersection: {
      common_covered_tasks: number | null;
      common_valid_scored_tasks: number | null;
      fault_excluded_tasks: number;
    };
    cells: Record<
      string,
      {
        common_valid: {
          score: number | null;
          raw_score?: number | null;
          passed: number | null;
          denominator: number | null;
          is_best: boolean;
          delta_pp: number | null;
          rel_pct: number | null;
        };
        common_covered_faults_as_zero: {
          enabled_by_protocol: boolean;
          score?: number | null;
          passed?: number | null;
          denominator?: number | null;
          delta_pp?: number | null;
        };
        self_full_batch_valid: {
          score: number | null;
          valid_scored: number | null;
          captured_tasks: number | null;
          split_total: number | null;
        };
      }
    >;
  }>;
  trace_links_redacted?: boolean;
  qa_links_redacted?: boolean;
}
