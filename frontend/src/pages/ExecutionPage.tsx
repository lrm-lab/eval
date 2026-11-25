import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  Bell,
  CheckCircle2,
  ChevronDown,
  Copy,
  Cpu,
  ExternalLink,
  FileText,
  History,
  Info,
  Lock,
  Play,
  Radio,
  RefreshCw,
  RotateCcw,
  Server,
  ShieldCheck,
  Square,
  Terminal,
  X,
  XCircle,
  Zap,
} from 'lucide-react';
import { ApiError, apiFetch } from '../lib/api';
import { fmtDateShort, fmtDuration, fmtPct, runScopeLabel } from '../lib/format';
import { useAppStore, type ToastMessage } from '../lib/store';
import { AnimatedNumber, PageTransition } from '../components/motion/primitives';
import { RowActionIcon } from '../components/ui/RowActionIcon';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

/* ============================================================================
 * Types matching backend execution_service.py & routes_execution.py contracts
 * ============================================================================ */

export type RunnerKind = 'harbor' | 'harbor_gateway' | 'vlmevalkit' | 'direct';
export type SelectionMode = 'all' | 'tasks';
export type VlmEvalKitMode = 'all' | 'infer';
export type JobStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'paused_by_guard'
  | 'interrupted'
  | 'finished'
  | 'failed';

export interface RunnerKindMeta {
  kind: RunnerKind;
  display_name: string;
  requires_docker: boolean;
  available_on_runner?: boolean;
  native_resume_supported: boolean;
  derived_resume_supported: boolean;
  manual_pause_supported: boolean;
  guard_pause_exit_code: number | null;
  partial_task_forces_infer: boolean;
  notes: string;
}

export interface RegisteredSuite {
  suite_id: string;
  display_name: string;
  runner: string;
  split?: string;
  handoff_profile?: string | null;
  reward_key?: string;
  task_count?: number | null;
  ready_task_count?: number;
  ready_tasks?: string[];
  sample_tasks?: string[];
  task_discovery_status?: string;
  registered: boolean;
  selectable: boolean;
  reason: string | null;
}

function defaultSuiteForRunner(suites: RegisteredSuite[], kind: RunnerKind) {
  const runner = kind === 'harbor_gateway' ? 'harbor' : kind;
  const matching = suites.filter((suite) => suite.runner === runner && suite.selectable);
  const smokeSuites = matching.filter((suite) => suite.split === 'smoke' && (suite.task_count ?? 0) > 0);
  smokeSuites.sort((first, second) => (first.task_count ?? 0) - (second.task_count ?? 0));
  return smokeSuites[0] ?? matching[0];
}

function readyRunners(caps?: ExecutionCapabilitiesResp): string[] {
  const environment = caps?.runner_environment;
  return environment?.isolated_runner_ready && !environment.docker_group_attached
    ? environment.supported_runners ?? []
    : [];
}

function runnerForSuite(suite: RegisteredSuite, available: string[], preference: RunnerKind | null, agent: string) {
  const choices: RunnerKind[] =
    suite.runner === 'harbor'
      ? (['oracle', 'nop'].includes(agent.trim()) ? ['harbor'] : ['harbor', 'harbor_gateway'])
      : suite.runner === 'direct' || suite.runner === 'vlmevalkit' ? [suite.runner] : [];
  const preferred = preference && choices.includes(preference) ? preference : choices[0];
  if (preferred && available.includes(preferred)) return preferred;
  return choices.find((choice) => available.includes(choice)) ?? preferred;
}

function suiteCanRun(suite: RegisteredSuite, available: string[], agent: string): boolean {
  const runner = runnerForSuite(suite, available, null, agent);
  return suite.selectable && (suite.task_count ?? 0) > 0 && Boolean(runner && available.includes(runner));
}

export interface UnintegratedSuite {
  suite_id: string;
  display_name: string;
  runner: string;
  registered: false;
  selectable: false;
  reason: string;
}

export interface GatewayPublicProfile {
  profile_id: string;
  display_name: string;
  kind?: string;
  default_model?: string;
  api_key_env?: string;
  key_configured: boolean;
}

export interface WebhookProfile {
  profile_id: string;
  display_name: string;
}

export interface CgroupTelemetry {
  source: string;
  cpu_usage_usec: number | null;
  memory_peak_bytes: number | null;
  memory_current_bytes?: number | null;
  memory_max_bytes?: number | null;
  pids_current?: number | null;
  scope?: string;
  note?: string;
}

export interface StreamTelemetry {
  source: string;
  ttft_ms: number | null;
  tps: number | null;
  ttft_p95_ms?: number | null;
  sample_count?: number;
  tps_sample_count?: number;
  note?: string;
}

export interface JobStdioStatus {
  present: boolean;
  total_bytes: number;
  segments: number;
  rotated: boolean;
  per_job_cap_bytes: number;
  segment_bytes: number;
  backup_segments: number;
  cumulative_cap_bytes: number;
  detail_read_bytes: number;
}

export interface IsolationProbeResult {
  ok: boolean;
  code?: string;
  message?: string;
  [k: string]: unknown;
}

export interface ExecutionCapabilitiesResp {
  methodology_version: string;
  runner_kinds: RunnerKindMeta[];
  suites: RegisteredSuite[];
  unintegrated_suites: UnintegratedSuite[];
  gateway_profiles: GatewayPublicProfile[];
  webhook_profiles: WebhookProfile[];
  runner_environment: {
    isolated_runner_ready: boolean;
    runner_connected?: boolean;
    runner_configured?: boolean;
    runner_mode?: string;
    runner_id?: string | null;
    runner_pid?: number | null;
    docker_available?: boolean;
    vlmevalkit_ready?: boolean;
    direct_ready?: boolean;
    supported_runners?: string[];
    reason?: string | null;
    docker_group_attached: boolean;
    runner_work_isolation: IsolationProbeResult;
    evidence_store_isolation: IsolationProbeResult;
    trusted_event_channel: string;
    stdout_drives_progress: boolean;
    telemetry: {
      streaming_ttft_tps: string;
      container_cgroup: CgroupTelemetry;
    };
  };
}

export interface ExecutionPlanSpec {
  kind: RunnerKind;
  suite_id: string;
  suite_display_name: string;
  selection_mode: SelectionMode;
  tasks: string[];
  vlmevalkit_mode: VlmEvalKitMode | null;
  forced_infer_by_task_selection: boolean;
  effective_run_scope: string;
  effective_official_status: string;
  model_route: string;
  gateway_profile_id: string;
  judge_profile_id: string | null;
  agent: string;
  harness_version: string;
  concurrent: number;
  attempts: number;
  pause_after: number;
  batch_name: string;
  project_id: string;
  notify: {
    in_app: boolean;
    webhook_profile_id: string | null;
    on_events: string[];
  };
  command_preview: string;
  warnings: string[];
  native_resume_supported: boolean;
  derived_resume_supported: boolean;
  requires_docker: boolean;
  execution_driver: string;
  planned_tasks: string[];
}

export interface ExecutionPlanResp {
  ok: boolean;
  methodology_version: string;
  plan: ExecutionPlanSpec;
  storage_boundary: {
    raw_work_storage: string;
    restricted_evidence_storage: string;
    shared_output_layer: string;
  };
  docker_group_attached: boolean;
}

export interface SubmitJobResp {
  job_id: string;
  status: JobStatus;
  kind: RunnerKind;
  batch_name: string;
  vlmevalkit_mode: VlmEvalKitMode | null;
  forced_infer_by_task_selection: boolean;
  effective_run_scope: string;
  effective_official_status: string;
  command_preview: string;
  stream_url: string;
  warnings: string[];
  parent_job_id?: string | null;
  resume_mode?: string | null;
  remaining_tasks?: string[];
}

export interface ExecutionProgress {
  planned: number;
  completed: number;
  scored: number;
  errors: number;
}

export interface LiveTrialEventPayload {
  seq_id?: number;
  job_id?: string;
  ts?: string;
  source?: string;
  trial_id: string;
  task_id: string;
  attempt?: number;
  status: string;
  reward: number | null;
  duration_sec?: number | null;
  progress?: ExecutionProgress;
}

export interface ReconciledTotals {
  planned: number;
  recorded: number;
  scored: number;
  coverage: number;
  valid_mean: number | null;
  passed: number;
  pass_threshold: number;
  outcomes: Record<string, number>;
}

export interface ExecutionJobSummary {
  job_id: string;
  kind: RunnerKind;
  project_id: string;
  owner_id: string;
  batch_name: string;
  batch_id: string | null;
  status: JobStatus;
  suite_id: string | null;
  suite_display_name: string | null;
  model_route: string | null;
  agent: string | null;
  harness_version: string | null;
  vlmevalkit_mode: VlmEvalKitMode | null;
  forced_infer_by_task_selection: boolean;
  effective_run_scope: string | null;
  effective_official_status: string | null;
  native_resume_supported: boolean;
  parent_job_id: string | null;
  resume_mode: string | null;
  command_preview: string;
  runner_id: string | null;
  exit_code: number | null;
  last_event_seq: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ExecutionJobListResp {
  methodology_version: string;
  items: ExecutionJobSummary[];
  total: number;
}

export interface ExecutionJobEvent {
  seq_id: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ExecutionJobDetail extends ExecutionJobSummary {
  live_channel: {
    source: string;
    last_seq_id: number;
    progress: ExecutionProgress;
    trials: LiveTrialEventPayload[];
    trusted_channel_note: string;
  };
  shared_snapshot: {
    reconciled: boolean;
    batch_id: string | null;
    generation_id: string | null;
    snapshot_digest: string | null;
    totals: ReconciledTotals | null;
  };
  events: ExecutionJobEvent[];
  redacted_stdio_logs: string[];
  stdio_log_truncated?: boolean;
  stdio_log_cap_bytes?: number;
  telemetry: {
    cgroup: CgroupTelemetry;
    streaming_ttft_tps: StreamTelemetry;
    stdio?: JobStdioStatus;
  };
}

export interface ExecutionNotificationItem {
  job_id: string;
  seq_id: number;
  batch_name: string;
  status: JobStatus;
  message: string;
  terminal_status: string | null;
  created_at: string;
}

export interface ExecutionNotificationsResp {
  items: ExecutionNotificationItem[];
  total: number;
}

interface AuthorizedEvidenceResp {
  job_id: string;
  trial_id: string;
  authorized_scope: string;
  storage_tier: string;
  evidence: Record<string, unknown>;
}

/* ============================================================================
 * Static fallback metadata (prevents CLS before capabilities query resolves)
 * ============================================================================ */

const FALLBACK_RUNNER_KINDS: RunnerKindMeta[] = [
  {
    kind: 'harbor',
    display_name: 'Harbor 标准模式',
    requires_docker: true,
    native_resume_supported: true,
    derived_resume_supported: true,
    manual_pause_supported: false,
    guard_pause_exit_code: 75,
    partial_task_forces_infer: false,
    notes:
      '隔离容器 Runner；原生 --resume 需锁定 harness_version 且私有 0700 --work 锁完整；运行中仅支持优雅 Stop，paused_by_guard (exit 75) 仅由 DispatchGuard 连续故障熔断触发。',
  },
  {
    kind: 'harbor_gateway',
    display_name: 'Harbor Gateway 模式',
    requires_docker: true,
    native_resume_supported: false,
    derived_resume_supported: true,
    manual_pause_supported: false,
    guard_pause_exit_code: 75,
    partial_task_forces_infer: false,
    notes:
      'OpenEnv 代理抓包落 0700 独立授权证据存储；原生不支持 --resume（调用原生续跑返回 409 RESUME_UNSUPPORTED），支持 M3 任务级派生续跑。',
  },
  {
    kind: 'vlmevalkit',
    display_name: 'VLMEvalKit 多模态模式',
    requires_docker: false,
    native_resume_supported: false,
    derived_resume_supported: true,
    manual_pause_supported: false,
    guard_pause_exit_code: null,
    partial_task_forces_infer: true,
    notes:
      '无需 Docker；VLMEVALKIT.md:44 硬约束：指定 --task 局部运行时强制进入 mode=infer（仅推理不跑官方评分器、run_scope=smoke/subset、official_status=unscored）；原生不支持 --resume。',
  },
  {
    kind: 'direct',
    display_name: 'Direct 原生直测模式',
    requires_docker: false,
    native_resume_supported: false,
    derived_resume_supported: true,
    manual_pause_supported: false,
    guard_pause_exit_code: null,
    partial_task_forces_infer: false,
    notes: '仅需 Python + 服务端 Gateway Profile，无需 Docker；原生不支持 --resume，支持 M3 任务级派生续跑。',
  },
];

const RUNNER_SHORT_LABELS: Record<RunnerKind, string> = {
  harbor: 'Harbor 标准',
  harbor_gateway: 'Harbor Gateway',
  vlmevalkit: 'VLMEvalKit 多模态',
  direct: 'Direct 原生直测',
};

const TERMINAL_STATUSES = new Set<JobStatus>(['finished', 'failed', 'interrupted', 'paused_by_guard']);
const RESUMABLE_STATUSES = new Set<JobStatus>(['paused_by_guard', 'interrupted', 'failed']);

const SSE_EVENT_TYPES = [
  'job_queued',
  'job_started',
  'trial_finished',
  'paused_by_guard',
  'job_cancelling',
  'job_interrupted',
  'job_failed',
  'reconciled',
  'notification',
] as const;

const NOTIFY_EVENT_OPTIONS: Array<{ id: string; label: string }> = [
  { id: 'finished', label: '完工 finished' },
  { id: 'failed', label: '失败 failed' },
  { id: 'paused_by_guard', label: '熔断 paused_by_guard' },
  { id: 'interrupted', label: '中止 interrupted' },
];

/* ============================================================================
 * Helpers
 * ============================================================================ */

function parseTaskList(raw: string): string[] {
  const parts = raw
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of parts) {
    if (!seen.has(item)) {
      seen.add(item);
      out.push(item);
    }
  }
  return out;
}

function shellQuote(arg: string): string {
  if (/^[A-Za-z0-9._:/<>-]+$/.test(arg)) return arg;
  return `'${arg.replace(/'/g, "'\"'\"'")}'`;
}

function buildLocalCommandPreview(params: {
  kind: RunnerKind;
  suiteId: string;
  selectionMode: SelectionMode;
  tasks: string[];
  effectiveVlmMode: VlmEvalKitMode | null;
  agent: string;
  harnessVersion: string;
  attempts: number;
  pauseAfter: number;
  modelRoute: string;
  batchName: string;
  concurrent: number;
}): string {
  const suite = params.suiteId || '<suite_id>';
  const batch = params.batchName.trim() || `${suite}-${params.kind}-<ts>`;
  const parts: string[] = ['python3', 'framework/run.py', suite];
  if (params.kind === 'vlmevalkit') {
    const cfg =
      params.selectionMode === 'all' && params.effectiveVlmMode === 'infer'
        ? `<RUNNER_WORK_0700>/${batch}/.resolved_harbor.toml`
        : 'datasets/multimodal/harbor.toml';
    parts.push('--config', cfg);
  } else if (params.kind === 'harbor' || params.kind === 'harbor_gateway') {
    parts.push('--config', `<RUNNER_WORK_0700>/${batch}/.resolved_harbor.toml`);
  }
  if (params.kind === 'direct') {
    const dt = params.tasks.length > 0 ? params.tasks : ['<task_id>'];
    for (const t of dt) {
      parts.push('--task', t);
    }
  } else if (params.selectionMode === 'all') {
    parts.push('--all');
  } else {
    for (const t of params.tasks) {
      parts.push('--task', t);
    }
  }
  if (params.kind === 'harbor' || params.kind === 'harbor_gateway') {
    parts.push('--agent', params.agent || 'opencode', '--agent-version', params.harnessVersion || '1.19.3');
    if (params.kind === 'harbor_gateway') {
      parts.push('--gateway-capture');
    }
    parts.push('--attempts', String(params.attempts), '--pause-after', String(params.pauseAfter));
  } else if (params.kind === 'direct') {
    parts.push('--attempts', String(params.attempts));
  }
  parts.push(
    '--model',
    params.modelRoute || 'Qwen3_8-27B',
    '--batch',
    batch,
    '--concurrent',
    String(params.concurrent),
    '--work',
    `<RUNNER_WORK_0700>/${batch}`,
    '--output',
    `<PUBLIC_DTO_OUTPUT>/${batch}`
  );
  return parts.map(shellQuote).join(' ');
}

function statusPillClasses(status: JobStatus | string): string {
  switch (status) {
    case 'finished':
      return 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)] border-[var(--sem-pass-border)]';
    case 'running':
      return 'bg-[var(--sem-running-bg)] text-[var(--sem-running-fg)] border-[var(--sem-running-border)]';
    case 'paused_by_guard':
    case 'cancelling':
      return 'bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)] border-[var(--sem-zero-border)]';
    case 'failed':
      return 'bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)] border-[var(--sem-err-border)]';
    default:
      return 'bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)] border-[var(--sem-unscored-border)]';
  }
}

function statusLabel(status: JobStatus | string): string {
  switch (status) {
    case 'queued':
      return '排队中 queued';
    case 'running':
      return '运行中 running';
    case 'cancelling':
      return '排空中止 cancelling';
    case 'paused_by_guard':
      return '熔断暂停 paused_by_guard (75)';
    case 'interrupted':
      return '已中止 interrupted';
    case 'finished':
      return '已完成 finished';
    case 'failed':
      return '已失败 failed';
    default:
      return status;
  }
}

function trialStatusPill(status: string, reward: number | null): { cls: string; label: string } {
  if (status === 'scored') {
    if (reward !== null && reward >= 1) {
      return {
        cls: 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)] border-[var(--sem-pass-border)]',
        label: 'scored · pass',
      };
    }
    return {
      cls: 'bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)] border-[var(--sem-zero-border)]',
      label: 'scored · 0',
    };
  }
  if (status === 'failed' || status === 'timeout') {
    return {
      cls: 'bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)] border-[var(--sem-err-border)]',
      label: status,
    };
  }
  return {
    cls: 'bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)] border-[var(--sem-unscored-border)]',
    label: status || 'unscored',
  };
}

function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—';
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}

function formatCpuUsec(usec: number | null | undefined): string {
  if (usec === null || usec === undefined || !Number.isFinite(usec)) return '—';
  const sec = usec / 1_000_000;
  if (sec >= 60) return `${(sec / 60).toFixed(1)} min`;
  return `${sec.toFixed(2)} s`;
}

/* ============================================================================
 * Smooth SVG Animated Circular Progress Ring (respects reducedMotion)
 * ============================================================================ */

export function ProgressRing({
  label,
  sublabel,
  value,
  total,
  strokeVar,
  accentClass,
  reducedMotion,
}: {
  label: string;
  sublabel: string;
  value: number;
  total: number;
  strokeVar: string;
  accentClass: string;
  reducedMotion: boolean;
}) {
  const size = 92;
  const strokeWidth = 7;
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const ratio = total > 0 ? Math.max(0, Math.min(1, value / total)) : 0;
  const dashOffset = circumference * (1 - ratio);
  const pct = total > 0 ? Math.round(ratio * 100) : 0;

  return (
    <div
      role="progressbar"
      aria-label={`${label}: ${value} / ${total} (${pct}%)`}
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={Math.max(total, 1)}
      className="flex items-center gap-3.5 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]/70 p-3 min-h-[112px]"
    >
      <div className="relative shrink-0" style={{ width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90 block">
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="var(--border-subtle)"
            strokeWidth={strokeWidth}
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={strokeVar}
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={dashOffset}
            style={{
              transition: reducedMotion
                ? 'none'
                : 'stroke-dashoffset 420ms cubic-bezier(0.16, 1, 0.3, 1)',
            }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center tabular-nums">
          <span className={`text-sm font-bold leading-none ${accentClass}`}>
            <AnimatedNumber value={pct} durationMs={360} suffix="%" />
          </span>
          <span className="mt-1 text-[10px] text-[var(--text-muted)]">
            {value}/{total}
          </span>
        </div>
      </div>
      <div className="min-w-0">
        <div className="text-xs font-semibold text-[var(--text-primary)]">{label}</div>
        <div className="mt-0.5 text-[11px] text-[var(--text-secondary)]">{sublabel}</div>
        <div className="mt-1.5 text-[11px] font-mono tabular-nums text-[var(--text-muted)]">
          计数 <strong className="text-[var(--text-primary)]">{value}</strong> / 计划 {total}
        </div>
      </div>
    </div>
  );
}

/* ============================================================================
 * Live SSE Overlay State for the Currently Selected Job
 * ============================================================================ */

interface LiveStreamOverlay {
  jobId: string;
  status: JobStatus | null;
  exitCode: number | null;
  lastSeqId: number;
  dedupEventCount: number;
  progress: ExecutionProgress | null;
  trialsById: Record<string, LiveTrialEventPayload>;
  reason: string | null;
  cgroup: CgroupTelemetry | null;
  sharedSnapshot: {
    reconciled: boolean;
    batch_id: string | null;
    generation_id: string | null;
    snapshot_digest: string | null;
    totals: ReconciledTotals | null;
  } | null;
}

type SseConnState = 'idle' | 'connecting' | 'streaming' | 'completed' | 'reconnecting' | 'disconnected';

/* ============================================================================
 * Main Page Component: ExecutionPage (/eval/execution)
 * ============================================================================ */

function ExecutionStep({ number, title, children }: { number: number; title: string; children: ReactNode }) {
  const headingId = `execution-step-${number}`;
  return (
    <section
      aria-labelledby={headingId}
      className="min-w-0 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 sm:p-5"
    >
      <h2 id={headingId} className="mb-3 flex items-center gap-2.5 text-sm font-bold text-[var(--text-primary)]">
        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-[var(--brand-tint)] text-xs tabular-nums text-[var(--brand-primary)]">
          {number}
        </span>
        {title}
      </h2>
      {children}
    </section>
  );
}

function ExecutionFieldLabel({ label, field }: { label: string; field?: string }) {
  return <span title={field} className="block font-semibold text-[var(--text-primary)]">{label}</span>;
}

export function ExecutionPage() {
  const user = useAppStore((s) => s.user);
  const userId = user?.user_id;
  const reducedMotion = useAppStore((s) => s.reducedMotion);
  const pushToast = useCallback((tone: ToastMessage['tone'], text: string) => {
    const session = useAppStore.getState();
    if (session.user?.user_id === userId) session.pushToast(tone, text);
  }, [userId]);
  const canStart = Boolean(user);

  // ---------- Section 1: Test Configuration State ----------
  const [runnerChoice, setKind] = useState<RunnerKind | null>(null);
  const [suiteId, setSuiteId] = useState<string>('');
  const [selectionMode, setSelectionMode] = useState<SelectionMode>('all');
  const [tasksInput, setTasksInput] = useState<string>('');
  const [vlmevalkitMode, setVlmevalkitMode] = useState<VlmEvalKitMode>('all');
  const [modelRoute, setModelRoute] = useState<string>('Qwen3_8-27B');
  const [gatewayProfileId, setGatewayProfileId] = useState<string>('qwen197-vllm');
  const [judgeProfileId, setJudgeProfileId] = useState<string>('');
  const [agent, setAgent] = useState<string>('opencode');
  const [harnessVersion, setHarnessVersion] = useState<string>('1.19.3');
  const [concurrent, setConcurrent] = useState<number>(2);
  const [attempts, setAttempts] = useState<number>(1);
  const [pauseAfter, setPauseAfter] = useState<number>(3);
  const [batchName, setBatchName] = useState<string>('');
  const [notifyInApp, setNotifyInApp] = useState<boolean>(true);
  const [webhookProfileId, setWebhookProfileId] = useState<string>('');
  const [notifyEvents, setNotifyEvents] = useState<string[]>(['finished', 'failed', 'paused_by_guard']);

  const [planResult, setPlanResult] = useState<ExecutionPlanResp | null>(null);
  const [environmentNotice, setEnvironmentNotice] = useState(false);
  const [dismissedNotifBanner, setDismissedNotifBanner] = useState<boolean>(false);
  const cliPreviewRef = useRef<HTMLDetailsElement>(null);

  // ---------- Section 2 & 3: Selected Job + Real SSE State ----------
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [sseState, setSseState] = useState<SseConnState>('idle');
  const [streamNonce, setStreamNonce] = useState<number>(0);
  const [liveOverlay, setLiveOverlay] = useState<LiveStreamOverlay | null>(null);
  const [evidenceViewer, setEvidenceViewer] = useState<AuthorizedEvidenceResp | null>(null);

  // Client-side (job_id, seq_id) deduplication map + monotonic lastSeq cursor
  const seenEventKeysRef = useRef<Map<string, true>>(new Map());
  const lastSeqRef = useRef<number>(0);
  const connectFromSeqRef = useRef<number>(0);
  const terminalObservedRef = useRef<boolean>(false);
  const selectedJobTerminalRef = useRef<boolean>(false);

  // ---------- Queries ----------
  const capsQ = useQuery({
    queryKey: ['execution-capabilities', userId],
    queryFn: () => apiFetch<ExecutionCapabilitiesResp>('/api/v1/execution/capabilities'),
  });
  const webhookProfiles = capsQ.data?.webhook_profiles ?? [];
  const configuredRunners = readyRunners(capsQ.data);
  const registeredSuites = capsQ.data?.suites ?? [];
  const selectedSuite = registeredSuites.find((suite) => suite.suite_id === suiteId);
  const firstReadyRunner = capsQ.data?.runner_kinds.find((runner) => configuredRunners.includes(runner.kind))?.kind;
  const suggestedKind = configuredRunners.includes('direct') ? 'direct' : firstReadyRunner;
  const suiteRunner = selectedSuite && runnerForSuite(selectedSuite, configuredRunners, runnerChoice, agent);
  const kind = suiteRunner ?? runnerChoice ?? suggestedKind ?? 'harbor';

  const jobsQ = useQuery({
    queryKey: ['execution-jobs', userId],
    queryFn: () => apiFetch<ExecutionJobListResp>('/api/v1/execution/jobs?limit=50'),
  });

  const notificationsQ = useQuery({
    queryKey: ['execution-notifications', userId],
    queryFn: () => apiFetch<ExecutionNotificationsResp>('/api/v1/execution/notifications?limit=20'),
  });

  const jobDetailQ = useQuery({
    queryKey: ['execution-job-detail', userId, selectedJobId],
    enabled: Boolean(selectedJobId),
    queryFn: () => apiFetch<ExecutionJobDetail>(`/api/v1/execution/jobs/${encodeURIComponent(selectedJobId!)}`),
  });

  const refetchJobDetail = jobDetailQ.refetch;
  const refetchJobs = jobsQ.refetch;
  const refetchNotifications = notificationsQ.refetch;

  // Derived VLMEvalKit VLMEVALKIT.md:44 hard constraint
  const vlmTaskGuardrailActive = kind === 'vlmevalkit' && selectionMode === 'tasks';
  const effectiveVlmMode: VlmEvalKitMode | null =
    kind === 'vlmevalkit' ? (vlmTaskGuardrailActive ? 'infer' : vlmevalkitMode) : null;

  useEffect(() => {
    if (vlmTaskGuardrailActive && vlmevalkitMode !== 'infer') {
      setVlmevalkitMode('infer');
    }
  }, [vlmTaskGuardrailActive, vlmevalkitMode]);

  const parsedTasks = useMemo(() => parseTaskList(tasksInput), [tasksInput]);
  const readyTaskCount = selectedSuite?.ready_task_count;
  const readyTasks = selectedSuite?.ready_tasks;
  const taskImagesPending = readyTaskCount !== undefined && readyTaskCount < (selectedSuite?.task_count ?? 1);
  const hasUnreadyTask = readyTasks !== undefined && parsedTasks.some((task) => !readyTasks.includes(task));
  const executionScopeReady = readyTaskCount === undefined ||
    (readyTaskCount > 0 && (selectionMode === 'all' ? !taskImagesPending : parsedTasks.length > 0 && !hasUnreadyTask));

  useEffect(() => {
    if (taskImagesPending && selectionMode === 'all') {
      setSelectionMode('tasks');
      setPlanResult(null);
    }
  }, [taskImagesPending, selectionMode]);

  const effectiveRunScope =
    selectionMode === 'all'
      ? (selectedSuite?.split === 'smoke' ? 'smoke' : 'full_split')
      : (parsedTasks.length <= 10 ? 'smoke' : 'subset');
  const effectiveOfficialStatus =
    kind === 'vlmevalkit' && effectiveVlmMode === 'infer'
      ? 'unscored'
      : kind === 'vlmevalkit'
        ? 'official_pending'
        : 'unofficial_valid_mean';

  // Initialize default suite & gateway profile once capabilities load
  useEffect(() => {
    const caps = capsQ.data;
    if (!caps) return;
    if (!suiteId) {
      const runnable = caps.suites.filter((suite) => suiteCanRun(suite, readyRunners(caps), agent));
      const match = defaultSuiteForRunner(runnable, kind) ?? runnable[0];
      if (match) setSuiteId(match.suite_id);
    }
    if (caps.gateway_profiles.length > 0) {
      const exists = caps.gateway_profiles.some((g) => g.profile_id === gatewayProfileId);
      if (!exists) {
        setGatewayProfileId(caps.gateway_profiles[0].profile_id);
      }
    }
  }, [agent, capsQ.data, gatewayProfileId, kind, suiteId]);

  // Auto-select latest job from history if none selected yet
  useEffect(() => {
    if (!selectedJobId && jobsQ.data && jobsQ.data.items.length > 0) {
      setSelectedJobId(jobsQ.data.items[0].job_id);
    }
  }, [jobsQ.data, selectedJobId]);

  // Handle runner switch with smart suite & agent defaults
  const handleSelectRunner = (nextKind: RunnerKind) => {
    if (!configuredRunners.includes(nextKind)) return;
    setKind(nextKind);
    setPlanResult(null);
    setEnvironmentNotice(false);
    if (nextKind === 'vlmevalkit' && selectionMode === 'tasks') {
      setVlmevalkitMode('infer');
    }
    if (nextKind === 'harbor' || nextKind === 'harbor_gateway') {
      if (!agent || agent === 'vlmevalkit' || agent === 'direct') {
        setAgent('opencode');
        setHarnessVersion('1.19.3');
      }
    }
    const suites = registeredSuites.filter((suite) => suiteCanRun(suite, configuredRunners, agent));
    if (suites.length > 0) {
      const targetRunner = nextKind === 'harbor_gateway' ? 'harbor' : nextKind;
      const curSuite = suites.find((s) => s.suite_id === suiteId);
      if (!curSuite || curSuite.runner !== targetRunner) {
        const match = defaultSuiteForRunner(suites, nextKind);
        if (match) {
          setSuiteId(match.suite_id);
          setTasksInput('');
        }
      }
    }
  };

  const toggleNotifyEvent = (evtId: string) => {
    setNotifyEvents((prev) =>
      prev.includes(evtId) ? prev.filter((x) => x !== evtId) : [...prev, evtId]
    );
  };

  // Build strict whitelisted request payload (ZERO raw API key or secret fields)
  const buildExecutionPayload = useCallback((resolvedKind = kind) => {
    const isHarborFamily = resolvedKind === 'harbor' || resolvedKind === 'harbor_gateway';
    return {
      kind: resolvedKind,
      suite_id: suiteId,
      selection_mode: selectionMode,
      tasks: selectionMode === 'tasks' ? parsedTasks : [],
      vlmevalkit_mode: resolvedKind === 'vlmevalkit' ? (selectionMode === 'tasks' ? 'infer' : vlmevalkitMode) : 'all',
      model_route: modelRoute.trim(),
      gateway_profile_id: gatewayProfileId,
      judge_profile_id: judgeProfileId.trim() || null,
      agent: isHarborFamily ? agent.trim() || 'opencode' : resolvedKind,
      harness_version: isHarborFamily ? harnessVersion.trim() || '1.19.3' : 'locked-v1',
      concurrent: Math.max(1, Math.min(32, Number(concurrent) || 2)),
      attempts: isHarborFamily ? Math.max(1, Math.min(16, Number(attempts) || 1)) : 1,
      pause_after: isHarborFamily ? Math.max(1, Math.min(50, Number(pauseAfter) || 3)) : 3,
      ...(batchName.trim() ? { batch_name: batchName.trim() } : {}),
      notify: {
        in_app: notifyInApp,
        webhook_profile_id: user?.role === 'viewer' ? null : webhookProfileId.trim() || null,
        on_events: notifyEvents.length > 0 ? notifyEvents : ['finished', 'failed', 'paused_by_guard'],
      },
    };
  }, [
    agent,
    attempts,
    batchName,
    concurrent,
    gatewayProfileId,
    harnessVersion,
    judgeProfileId,
    kind,
    modelRoute,
    notifyEvents,
    notifyInApp,
    parsedTasks,
    pauseAfter,
    selectionMode,
    suiteId,
    user?.role,
    vlmevalkitMode,
    webhookProfileId,
  ]);

  // ---------- Mutations ----------
  const planMut = useMutation({
    mutationFn: () =>
      apiFetch<ExecutionPlanResp>('/api/v1/execution/plan', {
        method: 'POST',
        body: JSON.stringify(buildExecutionPayload()),
      }),
    onSuccess: (res) => {
      setPlanResult(res);
      pushToast('success', `已通过服务端 --plan 预检：${res.plan.batch_name}`);
    },
    onError: (err: Error) => {
      pushToast('error', `预检失败：${err.message}`);
    },
  });

  const submitMut = useMutation({
    mutationFn: async () => {
      setEnvironmentNotice(false);
      const fresh = await capsQ.refetch();
      const suite = fresh.data?.suites.find((item) => item.suite_id === suiteId);
      const available = readyRunners(fresh.data);
      const resolvedKind = suite && runnerForSuite(suite, available, runnerChoice, agent);
      if (fresh.isError || !suite || !suiteCanRun(suite, available, agent) || !resolvedKind) {
        setEnvironmentNotice(true);
        return null;
      }
      return apiFetch<SubmitJobResp>('/api/v1/execution/jobs', {
        method: 'POST',
        body: JSON.stringify(buildExecutionPayload(resolvedKind)),
      });
    },
    onSuccess: (res) => {
      if (!res) return;
      pushToast('success', `已提交隔离执行作业 ${res.job_id} (${res.batch_name})`);
      setEvidenceViewer(null);
      connectFromSeqRef.current = 0;
      setSelectedJobId(res.job_id);
      setStreamNonce((n) => n + 1);
      void refetchJobs();
    },
    onError: (err: Error) => {
      if (err instanceof ApiError && ['RUNNER_NOT_ATTACHED', 'RUNNER_CAPABILITY_UNAVAILABLE', 'RUNNER_UNAVAILABLE'].includes(err.code)) {
        setEnvironmentNotice(true);
        void capsQ.refetch();
        return;
      }
      pushToast('error', `启动作业失败：${err.message}`);
    },
  });

  const cancelMut = useMutation({
    mutationFn: (jobId: string) =>
      apiFetch<{ job_id: string; status: JobStatus; message?: string }>(
        `/api/v1/execution/jobs/${encodeURIComponent(jobId)}/cancel`,
        { method: 'POST' }
      ),
    onSuccess: (res) => {
      pushToast('info', res.message || `作业 ${res.job_id} 已进入排空中止`);
      void refetchJobDetail();
      void refetchJobs();
    },
    onError: (err: Error) => {
      pushToast('error', `优雅中止失败：${err.message}`);
    },
  });

  const resumeMut = useMutation({
    mutationFn: ({
      jobId,
      resumeMode,
      lockHarnessVersion,
    }: {
      jobId: string;
      resumeMode: 'native' | 'derived_task_subset';
      lockHarnessVersion?: string | null;
    }) =>
      apiFetch<SubmitJobResp>(`/api/v1/execution/jobs/${encodeURIComponent(jobId)}/resume`, {
        method: 'POST',
        body: JSON.stringify({
          resume_mode: resumeMode,
          ...(resumeMode === 'native' && lockHarnessVersion ? { harness_version: lockHarnessVersion } : {}),
        }),
      }),
    onSuccess: (res) => {
      const modeTag = res.resume_mode === 'native' ? '原生 --resume' : '任务级派生续跑';
      pushToast('success', `${modeTag} 已启动：${res.job_id}`);
      setEvidenceViewer(null);
      connectFromSeqRef.current = 0;
      setSelectedJobId(res.job_id);
      setStreamNonce((n) => n + 1);
      void refetchJobs();
    },
    onError: (err: Error) => {
      pushToast('error', `续跑失败：${err.message}`);
    },
  });

  const evidenceMut = useMutation({
    mutationFn: ({ jobId, trialId }: { jobId: string; trialId: string }) =>
      apiFetch<AuthorizedEvidenceResp>(
        `/api/v1/execution/jobs/${encodeURIComponent(jobId)}/evidence/${encodeURIComponent(trialId)}`
      ),
    onSuccess: (res) => {
      setEvidenceViewer(res);
    },
    onError: (err: Error) => {
      pushToast('error', `受限证据访问被拒绝：${err.message}`);
    },
  });

  // ---------- Reset per-job live buffers on selectedJobId switch (MUST run before detail seed) ----------
  useEffect(() => {
    seenEventKeysRef.current = new Map();
    lastSeqRef.current = 0;
    connectFromSeqRef.current = 0;
    terminalObservedRef.current = false;
    selectedJobTerminalRef.current = false;
    setEvidenceViewer(null);
    if (!selectedJobId) {
      setLiveOverlay(null);
      setSseState('idle');
      return;
    }
    setLiveOverlay({
      jobId: selectedJobId,
      status: null,
      exitCode: null,
      lastSeqId: 0,
      dedupEventCount: 0,
      progress: null,
      trialsById: {},
      reason: null,
      cgroup: null,
      sharedSnapshot: null,
    });
  }, [selectedJobId]);

  // ---------- Seed dedup map when jobDetailQ resolves ----------
  useEffect(() => {
    const detail = jobDetailQ.data;
    if (!detail || detail.job_id !== selectedJobId) return;

    for (const ev of detail.events) {
      seenEventKeysRef.current.set(`${detail.job_id}:${ev.seq_id}`, true);
      if (ev.seq_id > lastSeqRef.current) {
        lastSeqRef.current = ev.seq_id;
      }
    }
    if (detail.live_channel.last_seq_id > lastSeqRef.current) {
      lastSeqRef.current = detail.live_channel.last_seq_id;
    }
    if (TERMINAL_STATUSES.has(detail.status)) {
      terminalObservedRef.current = true;
      selectedJobTerminalRef.current = true;
    }

    setLiveOverlay((prev) => {
      const prevForJob = prev && prev.jobId === detail.job_id ? prev : null;
      const mergedTrials: Record<string, LiveTrialEventPayload> = {};
      for (const tr of detail.live_channel.trials) {
        mergedTrials[tr.trial_id] = tr;
      }
      if (prevForJob) {
        for (const [tid, tr] of Object.entries(prevForJob.trialsById)) {
          mergedTrials[tid] = mergedTrials[tid] ? { ...mergedTrials[tid], ...tr } : tr;
        }
      }
      const nextSeq = Math.max(detail.live_channel.last_seq_id, prevForJob?.lastSeqId ?? 0);
      return {
        jobId: detail.job_id,
        status: prevForJob?.status && TERMINAL_STATUSES.has(prevForJob.status) ? prevForJob.status : detail.status,
        exitCode: prevForJob?.exitCode ?? detail.exit_code,
        lastSeqId: nextSeq,
        dedupEventCount: seenEventKeysRef.current.size,
        progress:
          prevForJob?.progress && prevForJob.progress.completed > detail.live_channel.progress.completed
            ? prevForJob.progress
            : detail.live_channel.progress,
        trialsById: mergedTrials,
        reason: prevForJob?.reason ?? null,
        cgroup: prevForJob?.cgroup ?? detail.telemetry.cgroup,
        sharedSnapshot: detail.shared_snapshot.reconciled
          ? detail.shared_snapshot
          : prevForJob?.sharedSnapshot ?? detail.shared_snapshot,
      };
    });
  }, [jobDetailQ.data, selectedJobId]);

  // ---------- Real Browser EventSource SSE Subscriber ----------
  useEffect(() => {
    if (!selectedJobId) return;

    const fromSeq = connectFromSeqRef.current;
    const baseUrl = `/api/v1/execution/jobs/${encodeURIComponent(selectedJobId)}/stream`;
    const streamUrl = fromSeq > 0 ? `${baseUrl}?last_event_id=${fromSeq}` : baseUrl;

    setSseState('connecting');
    const es = new EventSource(streamUrl, { withCredentials: true });
    let closed = false;

    const handleNamedEvent = (eventType: string): EventListener => (rawEv: Event) => {
      if (closed) return;
      const ev = rawEv as MessageEvent<string>;
      let parsed: Record<string, unknown> = {};
      try {
        parsed = JSON.parse(ev.data) as Record<string, unknown>;
      } catch {
        return;
      }

      const rawId = Number(ev.lastEventId || parsed.seq_id || 0);
      const seqId = Number.isFinite(rawId) && rawId > 0 ? rawId : 0;
      const dedupKey = `${selectedJobId}:${seqId}`;

      // Strict client-side (job_id, seq_id) deduplication
      if (seqId > 0 && seenEventKeysRef.current.has(dedupKey)) {
        return;
      }
      if (seqId > 0) {
        seenEventKeysRef.current.set(dedupKey, true);
        if (seqId > lastSeqRef.current) {
          lastSeqRef.current = seqId;
        }
      }

      const progressObj =
        parsed.progress && typeof parsed.progress === 'object'
          ? (parsed.progress as ExecutionProgress)
          : null;

      setLiveOverlay((prev) => {
        const base: LiveStreamOverlay =
          prev && prev.jobId === selectedJobId
            ? prev
            : {
                jobId: selectedJobId,
                status: null,
                exitCode: null,
                lastSeqId: 0,
                dedupEventCount: 0,
                progress: null,
                trialsById: {},
                reason: null,
                cgroup: null,
                sharedSnapshot: null,
              };

        const next: LiveStreamOverlay = {
          ...base,
          lastSeqId: Math.max(base.lastSeqId, seqId),
          dedupEventCount: seenEventKeysRef.current.size,
          progress: progressObj ?? base.progress,
        };

        switch (eventType) {
          case 'job_queued':
            if (!next.status) next.status = 'queued';
            break;
          case 'job_started':
            next.status = 'running';
            if (parsed.cgroup_telemetry && typeof parsed.cgroup_telemetry === 'object') {
              next.cgroup = parsed.cgroup_telemetry as CgroupTelemetry;
            }
            break;
          case 'trial_finished': {
            const tid = String(parsed.trial_id || `trial-${seqId}`);
            const row: LiveTrialEventPayload = {
              seq_id: seqId,
              job_id: selectedJobId,
              ts: typeof parsed.ts === 'string' ? parsed.ts : undefined,
              source: typeof parsed.source === 'string' ? parsed.source : 'runner_live',
              trial_id: tid,
              task_id: String(parsed.task_id || ''),
              attempt: typeof parsed.attempt === 'number' ? parsed.attempt : 1,
              status: String(parsed.status || 'scored'),
              reward: typeof parsed.reward === 'number' ? parsed.reward : null,
              duration_sec: typeof parsed.duration_sec === 'number' ? parsed.duration_sec : null,
              progress: progressObj ?? undefined,
            };
            next.trialsById = { ...base.trialsById, [tid]: row };
            break;
          }
          case 'paused_by_guard':
            next.status = 'paused_by_guard';
            next.exitCode = typeof parsed.exit_code === 'number' ? parsed.exit_code : 75;
            next.reason = typeof parsed.reason === 'string' ? parsed.reason : 'DispatchGuard 连续故障熔断 (exit 75)';
            terminalObservedRef.current = true;
            selectedJobTerminalRef.current = true;
            break;
          case 'job_cancelling':
            next.status = 'cancelling';
            break;
          case 'job_interrupted':
            next.status = 'interrupted';
            next.exitCode = typeof parsed.exit_code === 'number' ? parsed.exit_code : base.exitCode;
            next.reason = typeof parsed.reason === 'string' ? parsed.reason : '用户优雅中止';
            terminalObservedRef.current = true;
            selectedJobTerminalRef.current = true;
            break;
          case 'job_failed':
            next.status = 'failed';
            next.exitCode = typeof parsed.exit_code === 'number' ? parsed.exit_code : 1;
            next.reason = typeof parsed.reason === 'string' ? parsed.reason : '隔离执行作业异常退出';
            terminalObservedRef.current = true;
            selectedJobTerminalRef.current = true;
            break;
          case 'reconciled':
            next.status = 'finished';
            next.exitCode = 0;
            next.sharedSnapshot = {
              reconciled: true,
              batch_id: typeof parsed.batch_id === 'string' ? parsed.batch_id : null,
              generation_id: typeof parsed.generation_id === 'string' ? parsed.generation_id : null,
              snapshot_digest: typeof parsed.snapshot_digest === 'string' ? parsed.snapshot_digest : null,
              totals:
                parsed.totals && typeof parsed.totals === 'object'
                  ? (parsed.totals as ReconciledTotals)
                  : null,
            };
            terminalObservedRef.current = true;
            selectedJobTerminalRef.current = true;
            break;
          default:
            break;
        }
        return next;
      });

      if (eventType === 'reconciled') {
        void refetchJobDetail();
        void refetchJobs();
        void refetchNotifications();
      } else if (
        eventType === 'paused_by_guard' ||
        eventType === 'job_interrupted' ||
        eventType === 'job_failed'
      ) {
        void refetchJobDetail();
        void refetchJobs();
        void refetchNotifications();
      } else if (eventType === 'notification') {
        void refetchNotifications();
      }
    };

    es.onopen = () => {
      if (!closed) setSseState('streaming');
    };

    const listeners: Array<[string, EventListener]> = SSE_EVENT_TYPES.map((t) => {
      const fn = handleNamedEvent(t);
      es.addEventListener(t, fn);
      return [t, fn];
    });

    es.onerror = () => {
      if (closed) return;
      if (terminalObservedRef.current || selectedJobTerminalRef.current) {
        closed = true;
        es.close();
        setSseState('completed');
        return;
      }
      if (es.readyState === EventSource.CLOSED) {
        setSseState('disconnected');
      } else {
        setSseState('reconnecting');
      }
    };

    return () => {
      closed = true;
      for (const [t, fn] of listeners) {
        es.removeEventListener(t, fn);
      }
      es.close();
    };
  }, [selectedJobId, streamNonce, refetchJobDetail, refetchJobs, refetchNotifications]);

  // ---------- Manual Reconnect using ?last_event_id=${lastSeq} ----------
  const handleReconnectSse = () => {
    if (!selectedJobId) return;
    connectFromSeqRef.current = lastSeqRef.current;
    terminalObservedRef.current = false;
    setStreamNonce((n) => n + 1);
    pushToast('info', `正在以 ?last_event_id=${lastSeqRef.current} 断线补读 SSE 事件流`);
  };

  // ---------- Copy CLI Command ----------
  const localCommandPreview = useMemo(
    () =>
      buildLocalCommandPreview({
        kind,
        suiteId,
        selectionMode,
        tasks: parsedTasks,
        effectiveVlmMode,
        agent,
        harnessVersion,
        attempts,
        pauseAfter,
        modelRoute,
        batchName,
        concurrent,
      }),
    [
      agent,
      attempts,
      batchName,
      concurrent,
      effectiveVlmMode,
      harnessVersion,
      kind,
      modelRoute,
      parsedTasks,
      pauseAfter,
      selectionMode,
      suiteId,
    ]
  );

  const activeCommandPreview = planResult?.plan.command_preview ?? localCommandPreview;

  const handleCopyCli = async () => {
    try {
      await navigator.clipboard.writeText(activeCommandPreview);
      pushToast('success', '已复制脱敏 CLI 命令到剪贴板');
    } catch {
      pushToast('warning', '浏览器剪贴板受限，请手动选中命令块复制');
    }
  };

  // ---------- Computed View Data ----------
  const runnerKinds = capsQ.data?.runner_kinds ?? FALLBACK_RUNNER_KINDS;
  const dockerGroupAttached =
    planResult?.docker_group_attached ?? capsQ.data?.runner_environment.docker_group_attached ?? false;
  const isolatedRunnerReady = Boolean(capsQ.data?.runner_environment.isolated_runner_ready);
  const activeRunnerAvailable = Boolean(selectedSuite && suiteCanRun(selectedSuite, configuredRunners, agent));
  const runnerStatus = capsQ.isPending
    ? '正在检查运行状态'
    : activeRunnerAvailable && !dockerGroupAttached ? '评测可启动' : '此题库暂不可启动';
  const workProbeOk = capsQ.data?.runner_environment.runner_work_isolation?.ok ?? true;
  const evProbeOk = capsQ.data?.runner_environment.evidence_store_isolation?.ok ?? true;

  const selectedJobDetail = jobDetailQ.data && jobDetailQ.data.job_id === selectedJobId ? jobDetailQ.data : null;
  const selectedJobFromList = useMemo(
    () => (jobsQ.data?.items || []).find((j) => j.job_id === selectedJobId) ?? null,
    [jobsQ.data, selectedJobId]
  );

  const activeStatus: JobStatus | null =
    liveOverlay?.status ?? selectedJobDetail?.status ?? selectedJobFromList?.status ?? null;
  const activeKind: RunnerKind | null = selectedJobDetail?.kind ?? selectedJobFromList?.kind ?? null;
  const activeOwnerId = selectedJobDetail?.owner_id ?? selectedJobFromList?.owner_id;
  const canManageActive = Boolean(user && (user.role !== 'viewer' || activeOwnerId === user.user_id));
  const activeBatchName = selectedJobDetail?.batch_name ?? selectedJobFromList?.batch_name ?? '';
  const activeLastSeq = Math.max(
    liveOverlay?.lastSeqId ?? 0,
    selectedJobDetail?.live_channel.last_seq_id ?? 0,
    selectedJobFromList?.last_event_seq ?? 0
  );
  const activeProgress: ExecutionProgress = liveOverlay?.progress ??
    selectedJobDetail?.live_channel.progress ?? { planned: 0, completed: 0, scored: 0, errors: 0 };

  const activeSnapshot =
    (liveOverlay?.sharedSnapshot?.reconciled ? liveOverlay.sharedSnapshot : null) ??
    selectedJobDetail?.shared_snapshot ??
    (selectedJobFromList?.batch_id
      ? {
          reconciled: true,
          batch_id: selectedJobFromList.batch_id,
          generation_id: null,
          snapshot_digest: null,
          totals: null,
        }
      : null);

  const liveTrialRows = useMemo(() => {
    const map = liveOverlay?.trialsById ?? {};
    const rows = Object.values(map);
    rows.sort((a, b) => (a.seq_id ?? 0) - (b.seq_id ?? 0));
    return rows;
  }, [liveOverlay]);

  const activeCgroup = liveOverlay?.cgroup ?? selectedJobDetail?.telemetry.cgroup;
  const activeStreamTelemetry = selectedJobDetail?.telemetry.streaming_ttft_tps;
  const activeStdio = selectedJobDetail?.telemetry.stdio;
  const stdioCapBytes = activeStdio?.per_job_cap_bytes ?? selectedJobDetail?.stdio_log_cap_bytes ?? null;
  const stdioTruncated = selectedJobDetail?.stdio_log_truncated;

  const canStopActive = Boolean(
    canManageActive && selectedJobId && (activeStatus === 'queued' || activeStatus === 'running')
  );
  const isResumableStatus = Boolean(activeStatus && RESUMABLE_STATUSES.has(activeStatus));
  const isHarborStandard = activeKind === 'harbor';
  const canNativeResume = Boolean(canManageActive && selectedJobId && isHarborStandard && isResumableStatus);
  const canDerivedResume = Boolean(canManageActive && selectedJobId && isResumableStatus);

  const nativeResumeTooltip = !selectedJobId
    ? '请先选择执行作业'
    : !canManageActive
      ? '当前账号无权管理此作业'
      : !isHarborStandard
        ? `运行器 ${activeKind ?? 'unknown'} 原生不支持 --resume（调用将返回 409 RESUME_UNSUPPORTED），请使用「任务级派生续跑」`
        : !isResumableStatus
          ? `仅 paused_by_guard / interrupted / failed 状态允许原生 --resume（当前：${activeStatus ?? '—'}）`
          : '锁定相同 harness_version 与私有 0700 --work 锁执行 Harbor 原生 --resume';

  const isHarborFamily = kind === 'harbor' || kind === 'harbor_gateway';
  const notifications = notificationsQ.data?.items ?? [];

  if (capsQ.isPending || notificationsQ.isPending || jobsQ.isPending) {
    return (
      <PageTransition className="max-w-7xl mx-auto px-5 py-6">
        <p role="status" className="m-0 text-sm text-[var(--text-secondary)]">
          正在加载评测执行配置与作业…
        </p>
      </PageTransition>
    );
  }

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      {/* Page Header */}
      <div>
        <h1 className="m-0 flex items-center gap-2 text-xl font-bold text-[var(--text-primary)]">
          <Terminal aria-hidden="true" className="size-5 shrink-0 text-[var(--brand-primary)]" />
          <span>受控评测执行与实时监控 (Benchmark Execution)</span>
        </h1>
        <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
          配置评测、启动隔离作业并查看实时进度。
        </p>
      </div>

      {/* In-App Notifications Banner */}
      {!dismissedNotifBanner && notifications.length > 0 && (
        <section
          aria-label="站内执行终态通知"
          className="rounded-xl border border-[var(--sem-running-border)] bg-[var(--sem-running-bg)] px-4 py-3 text-xs"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="flex min-w-0 flex-1 items-start gap-2.5">
              <Bell className="w-4 h-4 text-[var(--sem-running-fg)] shrink-0 mt-0.5" />
              <div className="min-w-0 flex-1 space-y-1.5">
                <div className="font-semibold text-[var(--text-primary)] flex items-center gap-2">
                  <span>最新站内作业通知</span>
                  <span className="rounded-full bg-[var(--bg-surface)] border border-[var(--border-subtle)] px-1.5 py-0.2 text-[10px] font-mono tabular-nums text-[var(--text-secondary)]">
                    {notifications.length} 条
                  </span>
                </div>
                <div className="flex min-w-0 flex-wrap gap-2">
                  {notifications.slice(0, 3).map((n) => (
                    <button
                      key={`${n.job_id}-${n.seq_id}`}
                      type="button"
                      onClick={() => {
                        connectFromSeqRef.current = 0;
                        setSelectedJobId(n.job_id);
                      }}
                      className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] hover:border-[var(--brand-primary)] px-2.5 py-1 text-[11px] text-left cursor-pointer transition-colors"
                    >
                      <span className={`inline-block w-1.5 h-1.5 shrink-0 rounded-full ${n.status === 'finished' ? 'bg-[var(--sem-pass-fg)]' : n.status === 'failed' ? 'bg-[var(--sem-err-fg)]' : 'bg-[var(--sem-zero-fg)]'}`} />
                      <span className="min-w-0 truncate font-medium text-[var(--text-primary)]">{n.message}</span>
                      <time dateTime={n.created_at} className="shrink-0 whitespace-nowrap font-mono text-[10px] text-[var(--text-muted)]">
                        {fmtDateShort(n.created_at)}
                      </time>
                    </button>
                  ))}
                </div>
              </div>
            </div>
            <button
              type="button"
              aria-label="收起站内通知横幅"
              onClick={() => setDismissedNotifBanner(true)}
              className="text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer shrink-0"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </section>
      )}

      <section
        aria-label="Test Configuration 评测配置卡"
        className="grid items-start gap-4 text-xs lg:grid-cols-[minmax(0,1fr)_minmax(16rem,0.43fr)]"
      >
        <div className="min-w-0 space-y-4">
          <ExecutionStep number={1} title="选择题库与范围">
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
              <div className="space-y-2">
                <label className="block space-y-1">
                  <ExecutionFieldLabel label="评测题库" field="harbor.toml" />
                  <select
                    aria-label="选择已登记评测题库"
                    value={suiteId}
                    onChange={(e) => {
                      const nextSuiteId = e.target.value;
                      setSuiteId(nextSuiteId);
                      setKind(null);
                      if (nextSuiteId === 'gaia') {
                        // Use the native configuration that has completed grading on this host.
                        setAgent('pi');
                        setHarnessVersion('0.87.1');
                        setSelectionMode('tasks');
                        setTasksInput('076c8171-9b3b-49b9-a477-244d2a532826');
                      } else {
                        if (suiteId === 'gaia') setSelectionMode('all');
                        setTasksInput('');
                      }
                      setPlanResult(null);
                      setEnvironmentNotice(false);
                    }}
                    className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-2 text-xs text-[var(--text-primary)]"
                  >
                    <option value="" disabled>{capsQ.isPending ? '正在加载题库' : '请选择可运行的题库'}</option>
                    <optgroup label="已登记题库">
                      {registeredSuites.map((s) => {
                        const runnable = suiteCanRun(s, configuredRunners, agent);
                        return (
                          <option key={s.suite_id} value={s.suite_id} disabled={!runnable}>
                            {s.display_name} ({s.suite_id}
                            {s.ready_task_count !== undefined
                              ? ` · ${s.ready_task_count}/${s.task_count ?? 0} 题可执行`
                              : typeof s.task_count === 'number' ? ` · ${s.task_count} 题` : ''}
                            {s.split ? ` · ${s.split}` : ''})
                            {!runnable && ` · ${s.reason ?? '暂不可运行'}`}
                          </option>
                        );
                      })}
                    </optgroup>
                    {(capsQ.data?.unintegrated_suites || []).length > 0 && (
                      <optgroup label="原始数据可见、未接入评测（禁止提交）">
                        {(capsQ.data?.unintegrated_suites || []).map((u) => (
                          <option key={u.suite_id} value={u.suite_id} disabled>
                            [未接入] {u.display_name} — 原始数据可见、未接入评测
                          </option>
                        ))}
                      </optgroup>
                    )}
                  </select>
                </label>

                {capsQ.isPending ? (
                  <div aria-hidden="true" className="min-h-16 rounded-lg bg-[var(--bg-elevated)]" />
                ) : (capsQ.data?.unintegrated_suites || []).length > 0 && (
                  <div
                    aria-label="未接入评测题库列表"
                    className="min-h-16 space-y-1.5 rounded-lg border border-dashed border-[var(--border-strong)] p-2.5"
                  >
                    <div className="text-[10px] font-semibold text-[var(--text-muted)] uppercase tracking-wide">
                      尚未接入的题库
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {capsQ.data!.unintegrated_suites.map((u) => (
                        <button
                          key={u.suite_id}
                          type="button"
                          disabled
                          aria-disabled="true"
                          title={u.reason}
                          className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] bg-[var(--sem-unscored-bg)]/70 px-2 py-1 text-[11px] text-[var(--text-muted)] opacity-75 cursor-not-allowed"
                        >
                          <Lock className="w-3 h-3 shrink-0" />
                          <span className="font-medium line-through">{u.display_name}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* 1.3 Selection Mode (--all vs --task) & VLMEvalKit Guardrail */}
              <div className="space-y-2.5">
                <div className="space-y-1">
                  <ExecutionFieldLabel label="题集范围" field="selection_mode" />
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      disabled={taskImagesPending}
                      aria-pressed={selectionMode === 'all'}
                      onClick={() => {
                        setSelectionMode('all');
                        setPlanResult(null);
                      }}
                      className={`rounded-lg border px-3 py-1.5 text-xs font-medium cursor-pointer transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                        selectionMode === 'all'
                          ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                          : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                      }`}
                    >
                      全量评测 <span className="font-mono opacity-75">--all</span>
                    </button>
                    <button
                      type="button"
                      aria-label="--task 局部抽测"
                      aria-pressed={selectionMode === 'tasks'}
                      onClick={() => {
                        setSelectionMode('tasks');
                        if (kind === 'vlmevalkit') {
                          setVlmevalkitMode('infer');
                        }
                        setPlanResult(null);
                      }}
                      className={`rounded-lg border px-3 py-1.5 text-xs font-medium cursor-pointer transition-colors ${
                        selectionMode === 'tasks'
                          ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                          : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                      }`}
                    >
                      局部抽测 <span className="font-mono opacity-75">--task</span>
                    </button>
                  </div>
                  <p title="run_scope · official_status" className="font-mono text-[11px] text-[var(--text-secondary)]">
                    {runScopeLabel(effectiveRunScope)} · {effectiveOfficialStatus}
                  </p>
                  {taskImagesPending && (
                    <p className="text-[11px] text-[var(--text-secondary)]">
                      {readyTaskCount}/{selectedSuite?.task_count ?? 0} 题可执行，全部就绪后开放全量评测。
                    </p>
                  )}
                </div>

                {selectionMode === 'tasks' && (
                  <label className="block space-y-1">
                    <span className="text-[var(--text-secondary)]">
                      指定任务 ID（逗号或空格分隔，已解析{' '}
                      <strong className="font-mono text-[var(--text-primary)]">{parsedTasks.length}</strong> 题）
                    </span>
                    <input
                      type="text"
                      aria-label="指定任务 ID 列表"
                      placeholder="例如：task-01, task-02, task-03 或 0 1 2"
                      value={tasksInput}
                      onChange={(e) => {
                        setTasksInput(e.target.value);
                        setPlanResult(null);
                      }}
                      className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)]"
                    />
                  </label>
                )}

                {selectionMode === 'tasks' && hasUnreadyTask && (
                  <p className="text-[11px] text-[var(--text-secondary)]">部分任务的镜像尚未就绪，请选择已就绪的题目。</p>
                )}

                {selectionMode === 'tasks' && readyTaskCount !== undefined && Boolean(selectedSuite?.sample_tasks?.length) && (
                  <div className="flex flex-wrap gap-1.5" aria-label="已就绪题目">
                    {selectedSuite?.sample_tasks?.map((task) => (
                      <button
                        key={task}
                        type="button"
                        onClick={() => {
                          setTasksInput(task);
                          setPlanResult(null);
                        }}
                        className="rounded-md border border-[var(--border-subtle)] px-2 py-1 font-mono text-[11px] text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)]"
                      >
                        {task}
                      </button>
                    ))}
                  </div>
                )}

                {/* VLMEvalKit mode selector + VLMEVALKIT.md:44 lock */}
                {kind === 'vlmevalkit' && (
                  <div className="space-y-2 pt-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <ExecutionFieldLabel label="VLMEvalKit 评分模式" field="TOML mode" />
                      <button
                        type="button"
                        disabled={vlmTaskGuardrailActive}
                        aria-disabled={vlmTaskGuardrailActive}
                        aria-pressed={effectiveVlmMode === 'all'}
                        title={
                          vlmTaskGuardrailActive
                            ? 'VLMEVALKIT.md:44 硬约束：--task 局部运行禁止 mode=all，已强制锁定 infer'
                            : '全量推理并运行原生官方评分器'
                        }
                        onClick={() => {
                          if (!vlmTaskGuardrailActive) {
                            setVlmevalkitMode('all');
                            setPlanResult(null);
                          }
                        }}
                        className={`rounded-lg border px-2.5 py-1 text-[11px] font-mono transition-colors ${
                          vlmTaskGuardrailActive
                            ? 'border-[var(--border-subtle)] bg-[var(--sem-unscored-bg)] text-[var(--text-muted)] line-through opacity-60 cursor-not-allowed'
                            : effectiveVlmMode === 'all'
                              ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold cursor-pointer'
                              : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)] cursor-pointer'
                        }`}
                      >
                        mode=all（推理+官方评分）
                      </button>
                      <button
                        type="button"
                        aria-pressed={effectiveVlmMode === 'infer'}
                        onClick={() => {
                          setVlmevalkitMode('infer');
                          setPlanResult(null);
                        }}
                        className={`rounded-lg border px-2.5 py-1 text-[11px] font-mono cursor-pointer transition-colors ${
                          effectiveVlmMode === 'infer'
                            ? 'border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)] font-semibold'
                            : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                        }`}
                      >
                        mode=infer（仅推理 · unscored）{vlmTaskGuardrailActive ? ' [已强制锁定]' : ''}
                      </button>
                    </div>

                    {vlmTaskGuardrailActive && (
                      <div
                        role="alert"
                        className="rounded-lg border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] p-2.5 text-[11px] text-[var(--sem-zero-fg)] flex items-start gap-2"
                      >
                        <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                        <div>
                          <div className="font-bold">
                            VLMEVALKIT.md:44 硬约束已触发：自动锁定 vlmevalkit_mode=&quot;infer&quot;
                          </div>
                          <div className="mt-0.5 text-[var(--text-primary)]">
                            指定 <code>--task</code> 局部运行时仅执行推理、不运行原生官方评分器且不生成{' '}
                            <code>official-result.json</code>；系统自动标记为{' '}
                            <code>run_scope={effectiveRunScope}</code>（smoke/subset）与{' '}
                            <code>official_status=unscored</code>，已禁用 <code>mode=all</code>。
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </ExecutionStep>

          <details className="group rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-xs font-semibold text-[var(--text-secondary)]">
              运行环境（高级）
              <ChevronDown aria-hidden="true" className="size-4 transition-transform group-open:rotate-180" />
            </summary>
            <div className="mt-3 grid auto-rows-fr grid-cols-1 gap-2.5 min-[360px]:grid-cols-2" role="radiogroup" aria-label="评测运行器模式选择">
              {runnerKinds.map((runner) => {
                const selected = runner.kind === kind;
                return (
                  <button
                    key={runner.kind}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    aria-label={`选择运行器 ${RUNNER_SHORT_LABELS[runner.kind]}`}
                    disabled={!configuredRunners.includes(runner.kind) || !registeredSuites.some((suite) =>
                      suite.runner === (runner.kind === 'harbor_gateway' ? 'harbor' : runner.kind)
                      && suiteCanRun(suite, [runner.kind], agent)
                    )}
                    onClick={() => handleSelectRunner(runner.kind)}
                    className={`lrm-press flex min-h-16 min-w-0 cursor-pointer items-center gap-2 rounded-lg border px-2.5 py-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50 sm:gap-3 sm:px-3 ${
                      selected
                        ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] ring-1 ring-[var(--brand-primary)] shadow-[0_0_16px_var(--hero-glow-1)]'
                        : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] hover:border-[var(--brand-primary)]'
                    }`}
                  >
                    <span aria-hidden="true" className={`grid size-7 shrink-0 place-items-center rounded-md ${
                      selected ? 'bg-[var(--brand-primary)] text-white' : 'bg-[var(--bg-surface)] text-[var(--text-secondary)]'
                    }`}>
                      {selected ? <CheckCircle2 className="size-4" /> : <Cpu className="size-4" />}
                    </span>
                    <span className="min-w-0">
                      <span className="block font-bold text-[var(--text-primary)]">{RUNNER_SHORT_LABELS[runner.kind]}</span>
                      <span className="block text-[11px] text-[var(--text-secondary)]">
                        {capsQ.isPending
                          ? '正在检查状态'
                          : isolatedRunnerReady && configuredRunners.includes(runner.kind)
                            ? '已就绪 · 可启动'
                            : '暂不可用'}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          </details>

          <ExecutionStep number={2} title="模型与参数">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="space-y-1">
                <ExecutionFieldLabel label="模型路由" field="model_route" />
                <input
                  type="text"
                  aria-label="模型路由 model_route"
                  value={modelRoute}
                  onChange={(e) => {
                    setModelRoute(e.target.value);
                    setPlanResult(null);
                  }}
                  className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)]"
                />
              </label>

              <label className="space-y-1">
                <ExecutionFieldLabel label="模型网关" field="Gateway Profile · 0600" />
                <select
                  aria-label="选择服务端 Gateway Profile"
                  value={gatewayProfileId}
                  onChange={(e) => {
                    setGatewayProfileId(e.target.value);
                    setPlanResult(null);
                  }}
                  className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-primary)]"
                >
                  {!capsQ.data?.gateway_profiles.length && (
                    <option value={gatewayProfileId}>{capsQ.isPending ? '正在加载网关' : '暂无可用网关'}</option>
                  )}
                  {(capsQ.data?.gateway_profiles || []).map((g) => (
                    <option key={g.profile_id} value={g.profile_id}>
                      {g.display_name} ({g.profile_id}) {g.key_configured ? '✓' : '[未配密钥]'}
                    </option>
                  ))}
                </select>
              </label>

              <label className="space-y-1">
                <ExecutionFieldLabel label="裁判网关（可选）" field="judge_profile_id" />
                <select
                  aria-label="选择可选裁判 Gateway Profile"
                  value={judgeProfileId}
                  onChange={(e) => {
                    setJudgeProfileId(e.target.value);
                    setPlanResult(null);
                  }}
                  className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-primary)]"
                >
                  <option value="">不指定裁判网关（默认）</option>
                  {(capsQ.data?.gateway_profiles || []).map((g) => (
                    <option key={g.profile_id} value={g.profile_id}>
                      {g.display_name} ({g.profile_id})
                    </option>
                  ))}
                </select>
              </label>

              <label className="space-y-1">
                <ExecutionFieldLabel label="批次名称" field="batch_name · 留空自动生成" />
                <input
                  type="text"
                  aria-label="批次名 batch_name"
                  placeholder="留空自动生成"
                  value={batchName}
                  onChange={(e) => {
                    setBatchName(e.target.value);
                    setPlanResult(null);
                  }}
                  className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)]"
                />
              </label>

              {isHarborFamily && (
                <>
                  <label className="space-y-1">
                    <ExecutionFieldLabel label="评测代理" field="Harness / Agent" />
                    <input
                      type="text"
                      aria-label="Harbor Agent 名称"
                      value={agent}
                      onChange={(e) => {
                        setAgent(e.target.value);
                        setPlanResult(null);
                      }}
                      className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)]"
                    />
                  </label>

                  <label className="space-y-1">
                    <ExecutionFieldLabel
                      label="评测框架版本"
                      field={kind === 'harbor' ? 'harness_version · 续跑时锁定' : 'harness_version'}
                    />
                    <input
                      type="text"
                      aria-label="Harness 版本 harness_version"
                      value={harnessVersion}
                      onChange={(e) => {
                        setHarnessVersion(e.target.value);
                        setPlanResult(null);
                      }}
                      className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)]"
                    />
                  </label>
                </>
              )}
            </div>
            <details className="group mt-4 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 rounded-lg px-3 py-2.5 font-semibold text-[var(--text-primary)]">
                高级设置
                <ChevronDown aria-hidden="true" className="size-4 text-[var(--text-secondary)] group-open:rotate-180" />
              </summary>
              <div className="border-t border-[var(--border-subtle)] p-3">
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
                  {isHarborFamily && (
                    <>
                      <label className="space-y-1">
                        <ExecutionFieldLabel label="每题尝试次数" field="attempts · 1–16 次" />
                        <input
                          type="number"
                          min={1}
                          max={16}
                          aria-label="每题尝试次数 attempts"
                          value={attempts}
                          onChange={(e) => {
                            setAttempts(Number(e.target.value));
                            setPlanResult(null);
                          }}
                          className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs tabular-nums text-[var(--text-primary)]"
                        />
                      </label>

                      <label className="space-y-1">
                        <ExecutionFieldLabel label="熔断阈值" field="pause_after · exit 75" />
                        <input
                          type="number"
                          min={1}
                          max={50}
                          aria-label="DispatchGuard 熔断阈值 pause_after"
                          value={pauseAfter}
                          onChange={(e) => {
                            setPauseAfter(Number(e.target.value));
                            setPlanResult(null);
                          }}
                          className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs tabular-nums text-[var(--text-primary)]"
                        />
                      </label>
                    </>
                  )}

                  <label className="space-y-1">
                    <ExecutionFieldLabel label="并发数" field="concurrent · 1–32" />
                    <input
                      type="number"
                      min={1}
                      max={32}
                      aria-label="并发数 concurrent"
                      value={concurrent}
                      onChange={(e) => {
                        setConcurrent(Number(e.target.value));
                        setPlanResult(null);
                      }}
                      className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs tabular-nums text-[var(--text-primary)]"
                    />
                  </label>
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-[var(--border-subtle)] pt-3">
                  <label className="inline-flex items-center gap-1.5 cursor-pointer font-medium text-[var(--text-primary)]">
                    <input
                      type="checkbox"
                      aria-label="开启站内终态通知"
                      checked={notifyInApp}
                      onChange={(e) => setNotifyInApp(e.target.checked)}
                    />
                    <span>站内通知</span>
                  </label>

                  {webhookProfiles.length > 0 && (
                    <label className="inline-flex items-center gap-1.5">
                      <span className="text-[var(--text-secondary)]">Webhook 通知</span>
                      <select
                        aria-label="选择 Webhook 通知通道"
                        value={webhookProfileId}
                        onChange={(e) => setWebhookProfileId(e.target.value)}
                        className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2 py-1 text-[11px] text-[var(--text-primary)]"
                      >
                        <option value="">不推送 Webhook</option>
                        {webhookProfiles.map((w) => (
                          <option key={w.profile_id} value={w.profile_id}>
                            {w.display_name}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}

                  <div className="flex w-full flex-wrap items-center gap-1.5">
                    <span className="text-[10px] text-[var(--text-muted)]">触发事件</span>
                    {NOTIFY_EVENT_OPTIONS.map((opt) => {
                      const active = notifyEvents.includes(opt.id);
                      return (
                        <button
                          key={opt.id}
                          type="button"
                          aria-pressed={active}
                          onClick={() => toggleNotifyEvent(opt.id)}
                          className={`rounded px-1.5 py-0.5 text-[10px] border cursor-pointer ${
                            active
                              ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-medium'
                              : 'border-[var(--border-subtle)] bg-[var(--bg-surface)] text-[var(--text-muted)]'
                          }`}
                        >
                          {opt.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            </details>
          </ExecutionStep>

          <ExecutionStep number={3} title="预览并启动">
            <details ref={cliPreviewRef} className="group rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">
              <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 rounded-lg px-3 py-2.5 font-semibold text-[var(--text-primary)]">
                <Terminal aria-hidden="true" className="size-4 shrink-0 text-[var(--brand-primary)]" />
                <span>CLI 命令预览</span>
                {planResult && (
                  <span className="inline-flex items-center gap-1 rounded-full border border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] px-2 py-0.5 text-[10px] text-[var(--sem-pass-fg)]">
                    <CheckCircle2 aria-hidden="true" className="size-3" /> 服务端 --plan 已校验
                  </span>
                )}
                <ChevronDown aria-hidden="true" className="ml-auto size-4 text-[var(--text-secondary)] group-open:rotate-180" />
              </summary>
              <div className="space-y-2.5 border-t border-[var(--border-subtle)] p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-[11px] text-[var(--text-secondary)]">命令已脱敏，网关凭据由服务端注入。</span>
                  <button
                    type="button"
                    onClick={() => void handleCopyCli()}
                    aria-label="复制脱敏 CLI 命令"
                    className="lrm-press inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 font-medium text-[var(--text-primary)] hover:border-[var(--brand-primary)]"
                  >
                    <Copy aria-hidden="true" className="size-3.5 text-[var(--text-secondary)]" />
                    复制命令
                  </button>
                </div>
                <pre className="m-0 max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-2 font-mono text-[11px] text-[var(--text-primary)]">
                  {activeCommandPreview}
                </pre>
                <div className="flex flex-wrap gap-1.5 text-[11px]">
                  <span className={`rounded-md border px-2 py-0.5 ${
                    workProbeOk
                      ? 'border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                      : 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]'
                  }`}>
                    0700 私有 --work
                  </span>
                  <span className={`rounded-md border px-2 py-0.5 ${
                    evProbeOk
                      ? 'border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                      : 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]'
                  }`}>
                    0700 独立证据存储
                  </span>
                  <span className="rounded-md border border-[var(--sem-running-border)] bg-[var(--sem-running-bg)] px-2 py-0.5 text-[var(--sem-running-fg)]">
                    共享层仅输出 Public DTO
                  </span>
                </div>
                {planResult && planResult.plan.warnings.length > 0 && (
                  <div className="space-y-1">
                    {planResult.plan.warnings.map((w, idx) => (
                      <div
                        key={idx}
                        className="rounded-md border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] px-2.5 py-1.5 text-[11px] text-[var(--sem-zero-fg)] flex items-start gap-1.5"
                      >
                        <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                        <span>{w}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </details>
          </ExecutionStep>
        </div>

        <aside
          aria-labelledby="execution-summary-heading"
          className="min-w-0 space-y-4 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 shadow-[var(--shadow-card)] sm:p-5 lg:sticky lg:top-[4.5rem]"
        >
          <h2 id="execution-summary-heading" className="text-sm font-bold text-[var(--text-primary)]">作业摘要</h2>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-2.5 text-[var(--text-secondary)]">
            <dt>运行环境</dt>
            <dd className="truncate text-right font-semibold text-[var(--text-primary)]">
              {activeRunnerAvailable ? RUNNER_SHORT_LABELS[kind] : '待匹配'}
            </dd>
            <dt>题库</dt>
            <dd title={selectedSuite?.display_name} className="truncate text-right font-semibold text-[var(--text-primary)]">
              {selectedSuite?.display_name || '待选择'}
            </dd>
            <dt>范围</dt>
            <dd className="truncate text-right font-semibold text-[var(--text-primary)]">
              {selectionMode === 'all' ? '全量评测' : `抽测 · ${parsedTasks.length} 题`}
            </dd>
            <dt>模型</dt>
            <dd title={modelRoute} className="truncate text-right font-mono text-[var(--text-primary)]">{modelRoute || '待填写'}</dd>
          </dl>
          <div
            className={`min-h-24 space-y-1.5 rounded-lg border px-3 py-2.5 ${
              activeRunnerAvailable && !dockerGroupAttached
                ? 'border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                : 'border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]'
            }`}
          >
            <div aria-label={`isolated_runner_ready: ${String(isolatedRunnerReady)}`} className="flex items-start gap-1.5 font-semibold">
              {activeRunnerAvailable && !dockerGroupAttached
                ? <CheckCircle2 aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                : <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />}
              <span>{runnerStatus}</span>
            </div>
            <p className="text-[11px] leading-4">
              {capsQ.isPending
                ? '正在查询当前可用的评测模式。'
                : activeRunnerAvailable && !dockerGroupAttached
                  ? '配置确认后即可启动，页面会实时更新进度。'
                  : '请选择一个标为可运行的题库，系统会自动匹配运行环境。'}
            </p>
            {environmentNotice && <p role="status" className="text-[11px] leading-4">运行环境状态已变化。请选择当前可运行的题库。</p>}
          </div>
          {!canStart && (
            <p className="flex items-start gap-1.5 text-[11px] text-[var(--text-secondary)]">
              <Lock aria-hidden="true" className="mt-0.5 size-3 shrink-0" /> 请登录后预检或启动评测。
            </p>
          )}
          <div className="grid gap-2">
            <button
              type="button"
              disabled={!canStart || submitMut.isPending || !activeRunnerAvailable || !executionScopeReady || (selectionMode === 'tasks' && parsedTasks.length === 0)}
              onClick={() => submitMut.mutate()}
              aria-label="启动隔离执行作业"
              className="lrm-press inline-flex min-h-10 cursor-pointer items-center justify-center gap-2 rounded-lg bg-[var(--brand-primary)] px-3 py-2 text-sm font-semibold text-white hover:bg-[var(--brand-hover)] disabled:cursor-not-allowed disabled:opacity-45"
            >
              <Play aria-hidden="true" className="size-4" />
              {submitMut.isPending ? '正在启动…' : '启动执行'}
            </button>
            <button
              type="button"
              disabled={!canStart || planMut.isPending || !activeRunnerAvailable || (selectionMode === 'tasks' && parsedTasks.length === 0)}
              onClick={() => {
                const preview = cliPreviewRef.current;
                if (preview) {
                  preview.open = true;
                  preview.scrollIntoView({ block: 'nearest' });
                }
                planMut.mutate();
              }}
              aria-label="生成 --plan 预检与 CLI 命令"
              className="lrm-press inline-flex min-h-9 cursor-pointer items-center justify-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-elevated)] px-3 py-2 font-semibold text-[var(--text-primary)] hover:border-[var(--brand-primary)] disabled:cursor-not-allowed disabled:opacity-45"
            >
              <Terminal aria-hidden="true" className="size-3.5 text-[var(--brand-primary)]" />
              {planMut.isPending ? '正在预检…' : '生成 --plan 预览'}
            </button>
          </div>
        </aside>
      </section>

      {/* =====================================================================
          SECTION 2 — Test Progress 实时进度卡
         ===================================================================== */}
      <section
        aria-label="Test Progress 实时进度监控卡"
        className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden"
        style={{ boxShadow: 'var(--shadow-card)' }}
      >
        {/* Two-Pill Separation Header: Runner Live Stream vs Published Shared Snapshot */}
        <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <Activity className="w-4 h-4 text-[var(--brand-primary)]" />
            <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">2. Test Progress 实时进度卡</h2>

            {/* Pill 1: Runner Live Trusted Event Stream */}
            <span
              aria-live="polite"
              className="inline-flex items-center gap-1.5 rounded-full border border-[var(--sem-running-border)] bg-[var(--sem-running-bg)] px-2.5 py-0.5 text-[11px] font-semibold text-[var(--sem-running-fg)] tabular-nums"
            >
              <span className="relative flex h-2 w-2">
                {sseState === 'streaming' && !reducedMotion && (
                  <span className="lrm-calm-pulse absolute inline-flex h-full w-full rounded-full bg-[var(--sem-running-fg)] opacity-75" />
                )}
                <span
                  className={`relative inline-flex rounded-full h-2 w-2 ${
                    sseState === 'streaming'
                      ? 'bg-[var(--sem-running-fg)]'
                      : sseState === 'completed'
                        ? 'bg-[var(--sem-pass-fg)]'
                        : 'bg-[var(--text-muted)]'
                  }`}
                />
              </span>
              <span>⚡ Runner 实时事件流 (seq #{activeLastSeq} · 专用可信通道)</span>
            </span>

            {/* Pill 2: Published Shared Snapshot */}
            <span
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold font-mono ${
                activeSnapshot?.reconciled
                  ? 'border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                  : 'border-[var(--sem-unscored-border)] bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)]'
              }`}
            >
              <span>
                ✅ 已发布共享快照 (
                {activeSnapshot?.reconciled
                  ? activeSnapshot.generation_id || '已对账'
                  : '待 Common Finalizer 发布'}
                )
              </span>
            </span>
          </div>

          {/* SSE Status & Reconnect Button using ?last_event_id=${lastSeq} */}
          {selectedJobId && (
            <div className="flex flex-wrap items-center gap-2 text-[11px]">
              <span className="inline-flex items-center gap-1 font-mono text-[var(--text-muted)] tabular-nums">
                <Radio className="w-3.5 h-3.5 text-[var(--sem-running-fg)]" />
                SSE: {sseState} · 去重事件 {liveOverlay?.dedupEventCount ?? 0}
              </span>
              <button
                type="button"
                onClick={handleReconnectSse}
                aria-label={`断线重连补读事件流 last_event_id=${activeLastSeq}`}
                className="lrm-press inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] hover:border-[var(--brand-primary)] px-2.5 py-1 text-[11px] font-medium text-[var(--text-primary)] cursor-pointer"
              >
                <RefreshCw className="w-3 h-3" />
                <span>重连补读 (?last_event_id={activeLastSeq})</span>
              </button>
            </div>
          )}
        </div>

        {!selectedJobId ? (
          <div className="p-6">
            <StateNoticeCard
              kind="empty"
              title="尚未选中监控作业"
              description="可在上方配置并启动隔离执行作业，或在下方执行历史中点击任意作业查看实时事件流与脱敏日志。"
            />
          </div>
        ) : (
          <div className="p-4 space-y-4 text-xs">
            {/* Active Job Meta + Control Buttons */}
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]/60 p-3">
              <div className="space-y-1 min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono font-bold text-[var(--text-primary)]">{selectedJobId}</span>
                  <span className="font-medium text-[var(--text-secondary)] truncate">{activeBatchName}</span>
                  {activeKind && (
                    <span className="rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--text-secondary)]">
                      {RUNNER_SHORT_LABELS[activeKind]}
                    </span>
                  )}
                  {activeStatus && (
                    <span
                      className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${statusPillClasses(activeStatus)}`}
                    >
                      {statusLabel(activeStatus)}
                    </span>
                  )}
                  {selectedJobDetail?.forced_infer_by_task_selection && (
                    <span className="rounded-full border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)] px-2 py-0.5 text-[10px] font-mono">
                      mode=infer · unscored
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-[var(--text-muted)] font-mono flex flex-wrap gap-x-3 gap-y-0.5">
                  <span>suite={selectedJobDetail?.suite_id ?? selectedJobFromList?.suite_id ?? '—'}</span>
                  <span>model={selectedJobDetail?.model_route ?? selectedJobFromList?.model_route ?? '—'}</span>
                  <span>
                    harness={selectedJobDetail?.agent ?? selectedJobFromList?.agent ?? '—'}@
                    {selectedJobDetail?.harness_version ?? selectedJobFromList?.harness_version ?? '—'}
                  </span>
                  {selectedJobDetail?.parent_job_id && (
                    <span>
                      续跑自 {selectedJobDetail.parent_job_id} ({selectedJobDetail.resume_mode})
                    </span>
                  )}
                </div>
              </div>

              {/* Action Controls: Stop (NO manual Pause!), Native --resume, Derived Task Subset Resume */}
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={!canStopActive || cancelMut.isPending}
                  onClick={() => cancelMut.mutate(selectedJobId)}
                  aria-label="优雅中止作业 Stop"
                  title="发送 SIGTERM 优雅排空中止（源码无手动 Pause；paused_by_guard 仅由 DispatchGuard 熔断触发）"
                  className="lrm-press inline-flex items-center gap-1.5 rounded-lg border border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)] disabled:opacity-40 disabled:cursor-not-allowed px-3 py-1.5 font-semibold cursor-pointer"
                >
                  <Square className="w-3.5 h-3.5 fill-current" />
                  <span>{cancelMut.isPending ? '中止中...' : 'Stop (优雅中止)'}</span>
                </button>

                <button
                  type="button"
                  disabled={!canNativeResume || resumeMut.isPending}
                  onClick={() =>
                    resumeMut.mutate({
                      jobId: selectedJobId,
                      resumeMode: 'native',
                      lockHarnessVersion: selectedJobDetail?.harness_version ?? selectedJobFromList?.harness_version,
                    })
                  }
                  aria-label="Harbor 原生 --resume 续跑"
                  title={nativeResumeTooltip}
                  className="lrm-press inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] text-[var(--text-primary)] disabled:opacity-45 disabled:cursor-not-allowed px-3 py-1.5 font-medium cursor-pointer"
                >
                  <RotateCcw className="w-3.5 h-3.5 text-[var(--brand-primary)]" />
                  <span>原生 --resume</span>
                </button>

                <button
                  type="button"
                  disabled={!canDerivedResume || resumeMut.isPending}
                  onClick={() =>
                    resumeMut.mutate({
                      jobId: selectedJobId,
                      resumeMode: 'derived_task_subset',
                    })
                  }
                  aria-label="任务级派生续跑 derived_task_subset"
                  title="基于已完成任务集合计算剩余子集，派生新作业续跑（四运行器通用）"
                  className="lrm-press inline-flex items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] disabled:opacity-40 disabled:cursor-not-allowed px-3 py-1.5 font-semibold text-white cursor-pointer"
                >
                  <Zap className="w-3.5 h-3.5" />
                  <span>任务级派生续跑</span>
                </button>
              </div>
            </div>

            {/* Guard / Failure Reason Alert & Non-Harbor Native Resume Hint */}
            {liveOverlay?.reason && (
              <div
                role="alert"
                className={`rounded-lg border px-3 py-2 text-[11px] flex items-center gap-2 ${
                  activeStatus === 'paused_by_guard'
                    ? 'border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]'
                    : activeStatus === 'failed'
                      ? 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]'
                      : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                }`}
              >
                <AlertTriangle className="w-4 h-4 shrink-0" />
                <span>{liveOverlay.reason}</span>
              </div>
            )}

            {activeKind && !isHarborStandard && (
              <div className="text-[11px] text-[var(--text-muted)] flex items-center gap-1.5">
                <Info className="w-3.5 h-3.5 shrink-0" />
                <span>
                  当前运行器 <code>{activeKind}</code> 原生不支持 <code>--resume</code>（仅 Harbor 标准模式支持），中止或失败后请使用「任务级派生续跑」。运行中不提供手动 Pause，熔断暂停 (exit 75) 仅由 DispatchGuard 自动触发。
                </span>
              </div>
            )}

            {/* 3 Smooth SVG Circular Progress Rings */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <ProgressRing
                label="已完成 Completed"
                sublabel="实时可信通道已回收 Trial"
                value={activeProgress.completed}
                total={activeProgress.planned}
                strokeVar="var(--sem-running-fg)"
                accentClass="text-[var(--sem-running-fg)]"
                reducedMotion={reducedMotion}
              />
              <ProgressRing
                label="有效评分 Scored"
                sublabel="已获有效评分结果 Trial"
                value={activeProgress.scored}
                total={activeProgress.planned}
                strokeVar="var(--sem-pass-fg)"
                accentClass="text-[var(--sem-pass-fg)]"
                reducedMotion={reducedMotion}
              />
              <ProgressRing
                label="故障错误 Errors"
                sublabel="失败 / 超时 / 异常 Trial"
                value={activeProgress.errors}
                total={activeProgress.planned}
                strokeVar="var(--sem-err-fg)"
                accentClass="text-[var(--sem-err-fg)]"
                reducedMotion={reducedMotion}
              />
            </div>

            <div className="grid grid-cols-1 gap-2.5 text-[11px] sm:grid-cols-2">
              <div
                aria-label="流式 TTFT 和 TPS 遥测状态"
                aria-busy={jobDetailQ.isPending}
                title={activeStreamTelemetry?.note || '按 SSE 输出到达时间采样，TPS 取网关返回的输出 token 数'}
                className="min-w-0 h-14 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono"
              >
                <div className="flex items-center gap-1.5 text-[var(--text-secondary)]">
                  <Zap className="w-3.5 h-3.5 text-[var(--text-muted)]" />
                  流式 TTFT / TPS
                </div>
                <div className="mt-0.5 truncate text-[var(--text-primary)] font-semibold tabular-nums">
                  {activeStreamTelemetry?.ttft_ms != null ? (
                    <>
                      {activeStreamTelemetry.ttft_ms} ms
                      {activeStreamTelemetry.ttft_p95_ms != null && ` (p95 ${activeStreamTelemetry.ttft_p95_ms})`} ·{' '}
                      {activeStreamTelemetry.tps != null ? `${activeStreamTelemetry.tps} tok/s` : 'TPS 缺测'}
                      {activeStreamTelemetry.sample_count != null && ` · n=${activeStreamTelemetry.sample_count}`}
                    </>
                  ) : (jobDetailQ.isPending ? '读取中…' : jobDetailQ.isError ? '读取失败' : 'unavailable')}
                </div>
              </div>

              <div
                aria-label="隔离 Runner cgroup v2 遥测状态"
                aria-busy={jobDetailQ.isPending}
                title={activeCgroup?.note || '隔离 Runner 容器启动时快照'}
                className="min-w-0 h-14 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono"
              >
                <div className="flex items-center gap-1.5 text-[var(--text-secondary)]">
                  <Cpu className="w-3.5 h-3.5 text-[var(--text-muted)]" />
                  Runner cgroup v2
                </div>
                <div className="mt-0.5 truncate text-[var(--text-primary)] font-semibold tabular-nums">
                  {activeCgroup?.source === 'cgroup_v2' && ['container', 'container_namespace_root'].includes(activeCgroup.scope ?? '') ? (
                    <>CPU {formatCpuUsec(activeCgroup.cpu_usage_usec)} · 内存峰值 {formatBytes(activeCgroup.memory_peak_bytes)}</>
                  ) : (jobDetailQ.isPending ? '读取中…' : jobDetailQ.isError ? '读取失败' : 'unavailable')}
                </div>
              </div>

              <div className="h-8 min-w-0 flex items-center gap-1.5 font-mono text-[var(--text-muted)] sm:col-span-2">
                <Server className="w-3.5 h-3.5 shrink-0" />
                <span className="truncate">快照摘要 {activeSnapshot?.snapshot_digest ?? '—'}</span>
              </div>
            </div>

            {/* Redacted Stdio Log Viewer (Explicitly NEVER drives progress/state machine) */}
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]/55 overflow-hidden">
              <div className="px-3 py-2 border-b border-[var(--border-subtle)] flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-[11px] font-semibold text-[var(--text-primary)]">
                  <FileText className="w-3.5 h-3.5 text-[var(--text-muted)]" />
                  <span>普通 stdout/stderr（仅脱敏展示，绝不驱动进度或状态机）</span>
                </div>
                <button
                  type="button"
                  onClick={() => void refetchJobDetail()}
                  aria-label="刷新脱敏 stdout/stderr 日志"
                  className="inline-flex items-center gap-1 rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2 py-0.5 text-[10px] text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
                >
                  <RefreshCw className="w-3 h-3" /> 刷新日志
                </button>
              </div>
              <div
                role="status"
                aria-label="stdout/stderr 截断与限额提示"
                className="h-14 sm:h-10 px-3 py-1 border-b border-[var(--border-subtle)] text-[11px] leading-4 text-[var(--text-muted)]"
              >
                <div>
                  单作业留存上限 {formatBytes(stdioCapBytes)}
                  {activeStdio?.present && ` · 已留存 ${formatBytes(activeStdio.total_bytes)}（${activeStdio.segments} 段）`}
                </div>
                <div className={stdioTruncated ? 'font-semibold text-[var(--sem-zero-fg)]' : undefined}>
                  {stdioTruncated
                    ? `已截断 · 仅展示末尾至多 ${formatBytes(activeStdio?.detail_read_bytes ?? 128 * 1024)}、200 行`
                    : activeStdio?.rotated ? '日志已轮转' : '详情仅展示日志尾部'}
                </div>
              </div>
              <pre
                aria-label="脱敏 stdout 与 stderr 日志输出"
                className="m-0 h-44 overflow-y-auto bg-[var(--bg-canvas)]/70 p-3 font-mono text-[11px] leading-relaxed text-[var(--text-secondary)] whitespace-pre-wrap break-all"
              >
                {selectedJobDetail && selectedJobDetail.redacted_stdio_logs.length > 0
                  ? selectedJobDetail.redacted_stdio_logs.join('\n')
                  : '暂无普通 stdout/stderr 输出（作业进度与状态严格由独立 0600 HMAC 可信事件通道驱动）。'}
              </pre>
            </div>
          </div>
        )}
      </section>

      {/* =====================================================================
          SECTION 3 — Test Result 实时回收表 & 执行作业历史
         ===================================================================== */}
      <section aria-label="Test Result 实时回收表" className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden" style={{ boxShadow: 'var(--shadow-card)' }}>
        <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-2 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-[var(--brand-primary)]" />
            <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">3. Test Result 实时回收表</h2>
            <span className="rounded px-2 py-0.5 text-[10px] font-mono border border-[var(--border-subtle)] bg-[var(--bg-surface)] text-[var(--text-secondary)] tabular-nums">
              已回收 {liveTrialRows.length} 条 Trial
            </span>
            {activeSnapshot?.reconciled ? (
              <span className="rounded-full px-2 py-0.5 text-[10px] font-semibold bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)] border border-[var(--sem-pass-border)]">
                权威快照已对账
              </span>
            ) : (
              <span className="rounded-full px-2 py-0.5 text-[10px] font-semibold bg-[var(--sem-running-bg)] text-[var(--sem-running-fg)] border border-[var(--sem-running-border)]">
                实时事件流追加中
              </span>
            )}
          </div>

          {/* Authoritative Reconciled Batch Link once `reconciled` arrives */}
          {activeSnapshot?.reconciled && activeSnapshot.batch_id && (
            <div className="flex flex-wrap items-center gap-2">
              {activeSnapshot.totals && (
                <span className="text-[11px] font-mono tabular-nums text-[var(--text-secondary)]">
                  valid_mean={' '}
                  <strong className="text-[var(--text-primary)]">
                    {activeSnapshot.totals.valid_mean !== null
                      ? fmtPct(activeSnapshot.totals.valid_mean * 100)
                      : '—'}
                  </strong>{' '}
                  · passed {activeSnapshot.totals.passed}/{activeSnapshot.totals.scored}
                </span>
              )}
              <Link
                to={`/eval/batches/${encodeURIComponent(activeSnapshot.batch_id)}`}
                aria-label={`打开已发布权威批次详情 ${activeSnapshot.batch_id}`}
                className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--sem-pass-fg)] hover:opacity-90 px-3 py-1 text-[11px] font-semibold text-white no-underline"
              >
                <span>打开权威批次快照 (/eval/batches/{activeSnapshot.batch_id})</span>
                <ExternalLink className="w-3 h-3" />
              </Link>
            </div>
          )}
        </div>

        {!selectedJobId ? (
          <div className="p-6">
            <StateNoticeCard kind="empty" title="尚无选定作业的回收记录" description="启动或选择执行作业后，此处将实时展示 SSE trial_finished 回收结果。" />
          </div>
        ) : liveTrialRows.length === 0 ? (
          <div className="p-6">
            <StateNoticeCard
              kind="empty"
              title="等待 trial_finished 事件"
              description="当前作业尚未收到逐题回收事件；一旦可信事件通道上报 trial_finished，将立即逐行追加。"
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)] bg-[var(--bg-elevated)]/40">
                  <th className="py-2 px-3">Task ID</th>
                  <th className="py-2 px-3">Trial ID</th>
                  <th className="py-2 px-3 text-center">Attempt</th>
                  <th className="py-2 px-3">状态</th>
                  <th className="py-2 px-3 text-right">Reward</th>
                  <th className="py-2 px-3 text-right">耗时</th>
                  <th className="py-2 px-3 text-right">可信序号 / 证据</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)] tabular-nums">
                {liveTrialRows.map((tr) => {
                  const pill = trialStatusPill(tr.status, tr.reward);
                  return (
                    <tr key={tr.trial_id} className="hover:bg-[var(--bg-elevated)]/50">
                      <td className="py-2 px-3 font-mono font-semibold text-[var(--text-primary)]">{tr.task_id}</td>
                      <td className="py-2 px-3 font-mono text-[11px] text-[var(--text-secondary)]" title={tr.trial_id}>
                        {tr.trial_id.slice(0, 14)}…
                      </td>
                      <td className="py-2 px-3 text-center font-mono text-[11px]">#{tr.attempt ?? 1}</td>
                      <td className="py-2 px-3">
                        <span className={`inline-block rounded-full border px-2 py-0.5 text-[10px] font-semibold ${pill.cls}`}>
                          {pill.label}
                        </span>
                      </td>
                      <td className="py-2 px-3 text-right font-mono font-semibold">
                        {tr.reward !== null && tr.reward !== undefined ? tr.reward.toFixed(2) : '—'}
                      </td>
                      <td className="py-2 px-3 text-right font-mono text-[var(--text-secondary)]">
                        {fmtDuration(tr.duration_sec)}
                      </td>
                      <td className="py-2 px-3 text-right font-mono text-[10px] text-[var(--text-muted)]">
                        <span className="mr-2">seq #{tr.seq_id ?? '—'}</span>
                        {activeSnapshot?.reconciled && (
                          <button
                            type="button"
                            onClick={() => evidenceMut.mutate({ jobId: selectedJobId, trialId: tr.trial_id })}
                            className="inline-flex items-center gap-1 rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] hover:border-[var(--brand-primary)] px-1.5 py-0.5 text-[10px] text-[var(--text-primary)] cursor-pointer"
                          >
                            <Lock className="w-2.5 h-2.5" /> 受限证据
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Authorized 0700 Evidence Viewer Drawer Strip */}
        {evidenceViewer && evidenceViewer.job_id === selectedJobId && (
          <div className="border-t border-[var(--border-subtle)] bg-[var(--bg-elevated)]/70 p-3 space-y-2 text-xs">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 font-mono text-[11px]">
                <ShieldCheck className="w-3.5 h-3.5 text-[var(--sem-pass-fg)]" />
                <span className="font-semibold text-[var(--text-primary)]">
                  0700 受限证据已鉴权 ({evidenceViewer.authorized_scope})
                </span>
                <span className="text-[var(--text-muted)]">trial={evidenceViewer.trial_id}</span>
              </div>
              <button
                type="button"
                aria-label="关闭受限证据预览"
                onClick={() => setEvidenceViewer(null)}
                className="text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer"
              >
                <XCircle className="w-4 h-4" />
              </button>
            </div>
            <pre className="m-0 max-h-48 overflow-auto rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-2.5 font-mono text-[11px] text-[var(--text-secondary)]">
              {JSON.stringify(evidenceViewer.evidence, null, 2)}
            </pre>
          </div>
        )}
      </section>

      {/* Execution Job History List */}
      <section aria-label="执行作业历史列表" className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden" style={{ boxShadow: 'var(--shadow-card)' }}>
        <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-2 text-xs">
          <div className="flex items-center gap-2">
            <History className="w-4 h-4 text-[var(--brand-primary)]" />
            <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">执行作业历史 (Execution Jobs)</h2>
            <span className="rounded px-1.5 py-0.5 text-[10px] font-mono bg-[var(--bg-surface)] border border-[var(--border-subtle)] text-[var(--text-secondary)] tabular-nums">
              {jobsQ.data?.total ?? 0} 个作业
            </span>
          </div>
          <button
            type="button"
            onClick={() => {
              void refetchJobs();
              void refetchNotifications();
            }}
            aria-label="刷新执行作业历史"
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] hover:border-[var(--border-strong)] px-2.5 py-1 text-[11px] font-medium text-[var(--text-primary)] cursor-pointer"
          >
            <RefreshCw className="w-3 h-3" /> 刷新列表
          </button>
        </div>

        {jobsQ.isLoading ? (
          <TableSkeleton rows={4} cols={6} />
        ) : jobsQ.isError ? (
          <div className="p-6">
            <StateNoticeCard
              kind="error"
              title="加载执行作业历史失败"
              description={(jobsQ.error as Error)?.message || '请稍后重试'}
              onRetry={() => void refetchJobs()}
            />
          </div>
        ) : (jobsQ.data?.items || []).length === 0 ? (
          <div className="p-6">
            <StateNoticeCard
              kind="empty"
              title="尚无受控执行作业记录"
              description="使用上方配置卡生成 --plan 预检并启动首个隔离执行作业。"
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)] bg-[var(--bg-elevated)]/40">
                  <th className="py-2 px-3">Job ID / Batch</th>
                  <th className="py-2 px-3">运行器 & 题库</th>
                  <th className="py-2 px-3">模型 / Harness</th>
                  <th className="py-2 px-3">状态 / seq</th>
                  <th className="py-2 px-3">已发布快照</th>
                  <th className="py-2 px-3 text-right">时间 / 监控</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)] tabular-nums">
                {jobsQ.data!.items.map((job) => {
                  const isSelected = job.job_id === selectedJobId;
                  return (
                    <tr
                      key={job.job_id}
                      onClick={() => {
                        connectFromSeqRef.current = 0;
                        setSelectedJobId(job.job_id);
                      }}
                      className={`group/row-action cursor-pointer transition-colors ${
                        isSelected ? 'bg-[var(--brand-tint)]/55' : 'hover:bg-[var(--bg-elevated)]/60'
                      }`}
                    >
                      <td className="py-2.5 px-3">
                        <div className="font-mono font-semibold text-[var(--text-primary)]">{job.job_id}</div>
                        <div className="text-[11px] text-[var(--text-secondary)] truncate max-w-[240px]">
                          {job.batch_name}
                        </div>
                        {job.parent_job_id && (
                          <div className="text-[10px] font-mono text-[var(--text-muted)]">
                            ↳ 续跑({job.resume_mode}) ← {job.parent_job_id}
                          </div>
                        )}
                      </td>
                      <td className="py-2.5 px-3">
                        <div className="font-medium text-[var(--text-primary)]">
                          {job.suite_display_name || job.suite_id || '—'}
                        </div>
                        <div className="mt-0.5 flex flex-wrap gap-1">
                          <span className="rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)] px-1.5 py-0.2 font-mono text-[10px] text-[var(--text-secondary)]">
                            {RUNNER_SHORT_LABELS[job.kind] ?? job.kind}
                          </span>
                          {job.effective_run_scope && (
                            <span className="rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)] px-1.5 py-0.2 font-mono text-[10px] text-[var(--text-muted)]">
                              {runScopeLabel(job.effective_run_scope)}
                            </span>
                          )}
                          {job.forced_infer_by_task_selection && (
                            <span className="rounded bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)] border border-[var(--sem-zero-border)] px-1.5 py-0.2 font-mono text-[10px]">
                              infer · unscored
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="py-2.5 px-3 font-mono text-[11px] text-[var(--text-secondary)]">
                        <div>{job.model_route || '—'}</div>
                        <div className="text-[10px] text-[var(--text-muted)]">
                          {job.agent || '—'}@{job.harness_version || '—'}
                        </div>
                      </td>
                      <td className="py-2.5 px-3">
                        <span
                          className={`inline-block rounded-full border px-2 py-0.5 text-[10px] font-semibold ${statusPillClasses(job.status)}`}
                        >
                          {statusLabel(job.status)}
                        </span>
                        <div className="mt-0.5 font-mono text-[10px] text-[var(--text-muted)]">
                          seq #{job.last_event_seq}
                          {job.exit_code !== null ? ` · exit=${job.exit_code}` : ''}
                        </div>
                      </td>
                      <td className="py-2.5 px-3" onClick={(e) => e.stopPropagation()}>
                        {job.batch_id ? (
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-[11px] text-[var(--text-secondary)]">{job.batch_id}</span>
                            <RowActionIcon
                              variant="details"
                              label={`查看批次详情 ${job.batch_id}`}
                              to={`/eval/batches/${encodeURIComponent(job.batch_id)}`}
                            />
                          </div>
                        ) : (
                          <span className="text-[11px] text-[var(--text-muted)]">—</span>
                        )}
                      </td>
                      <td className="py-2.5 px-3 text-right">
                        <div className="text-[10px] text-[var(--text-muted)] font-mono">{fmtDateShort(job.created_at)}</div>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            connectFromSeqRef.current = 0;
                            setSelectedJobId(job.job_id);
                          }}
                          className="mt-1 rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] hover:border-[var(--brand-primary)] px-2 py-0.5 text-[10px] font-medium text-[var(--text-primary)] cursor-pointer"
                        >
                          {isSelected ? '当前监控中' : '查看监控'}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </PageTransition>
  );
}
