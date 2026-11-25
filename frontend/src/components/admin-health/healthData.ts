export interface IndexStatus {
  last_hot_sync_at?: string | null;
  last_hot_stats?: { elapsed_ms?: number | null };
  recent_poll_ms?: number[];
  cold_status?: string;
  last_cold_sync_at?: string | null;
  last_cold_stats?: { elapsed_ms?: number | null };
  write_protected?: boolean;
  write_block_reason?: string | null;
}

export interface IsolationProbe {
  ok?: boolean;
  configured?: boolean;
  fstype?: string;
  mode?: string;
  code?: string;
  message?: string;
  note?: string;
}

export interface StorageStatus {
  production_mode?: boolean;
  root_free_gb?: number;
  root_used_pct?: number;
  state_fs_free_gb?: number;
  root_disk_alert?: boolean;
  write_guard_ok?: boolean;
  write_guard_error?: string | null;
  local_runtime_mb?: number;
  local_budget_mb?: number;
  budget_alert?: boolean;
  isolation_probes?: Record<string, IsolationProbe | string>;
}

export interface RunnerStatus {
  isolated_runner_ready?: boolean;
  runner_connected?: boolean;
  runner_configured?: boolean;
  runner_mode?: string;
  runner_pid?: number | null;
  supported_runners?: string[];
  reason?: string | null;
  jobs_by_status?: Record<string, number>;
  docker_group_attached?: boolean;
}

export interface SourceRow {
  source?: string;
  source_key?: string;
  status?: string;
  discovered_dirs?: number;
  indexed_batches?: number;
  permission_gaps?: number;
  cached_files?: number;
  error_files?: number;
  indexed_items?: number;
  size_mb?: number;
}

export interface IndexWarning {
  warning_id?: number;
  tier?: string;
  severity?: string;
  code?: string;
  target_path?: string;
  message?: string;
  detected_at?: string;
}

export interface ColdFile {
  file_path?: string;
  cache_kind?: string;
  size_bytes?: number;
  item_count?: number;
  status?: string;
  error_message?: string | null;
  updated_at?: string;
}

export interface AdminHealthResp {
  indexer?: IndexStatus;
  storage?: StorageStatus;
  source_coverage?: {
    by_runner?: Array<{ runner?: string; batches?: number; trials?: number }>;
    running_placeholders?: Array<{
      batch_id?: string;
      batch_name?: string;
      runner?: string;
      planned?: number | null;
      recorded?: number;
      batch_status?: string | null;
      indexed_at?: string;
    }>;
    cold_cache?: Array<{ cache_kind?: string; status?: string; files?: number; items?: number }>;
    cold_files?: ColdFile[];
    by_source?: SourceRow[];
    warnings?: IndexWarning[];
  };
  gateway_profiles?: Array<{ profile_id?: string; display_name?: string; kind?: string; key_configured?: boolean }>;
  runner_status?: RunnerStatus;
}

interface Usage {
  total_bytes?: number | null;
  cap_bytes?: number | null;
  within_cap?: boolean | null;
}

export interface BackupStatus {
  status?: string;
  retained_count?: number | null;
  retention_limit?: number;
  size_limit_bytes?: number;
  latest_name?: string | null;
  latest_size_bytes?: number | null;
  latest_at?: string | null;
  age_hours?: number | null;
  note?: string;
}

export interface ExecutionTelemetryResp {
  web_cgroup?: {
    source?: string;
    scope?: string;
    cpu_usage_usec?: number | null;
    memory_peak_bytes?: number | null;
    memory_current_bytes?: number | null;
    memory_max_bytes?: number | null;
    pids_current?: number | null;
    note?: string;
  };
  stdio_policy?: {
    per_job_cap_bytes?: number;
    segment_bytes?: number;
    backup_segments?: number;
    cumulative_cap_bytes?: number;
    detail_read_bytes?: number;
  };
  ops?: {
    daemon_logs?: Usage & {
      logs_dir_present?: boolean;
      components?: Array<{
        component?: string;
        max_files?: number;
        max_bytes_per_file?: number;
        current_bytes?: number;
        files_present?: number;
      }>;
    };
    job_stdio_usage?: Usage & { jobs_with_stdio?: number | null };
    backup?: BackupStatus;
  } | null;
}

export type HealthAction =
  | { kind: 'hot' | 'full' | 'taxonomy' }
  | { kind: 'batch'; batchId: string }
  | { kind: 'cold-file'; filePath: string };

export type Tone = 'ok' | 'warning' | 'error' | 'neutral';

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function dataRows<T extends object>(value: T[] | undefined): T[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

export function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function textValue(value: unknown, fallback = '未报告'): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

export function fmtMetric(value: unknown, unit = '', digits = 1): string {
  const n = numberValue(value);
  return n === null ? '—' : `${n.toLocaleString('zh-CN', { maximumFractionDigits: digits })}${unit}`;
}

export function fmtBytes(value: unknown): string {
  const bytes = numberValue(value);
  if (bytes === null) return '—';
  if (bytes >= 1024 ** 3) return fmtMetric(bytes / 1024 ** 3, ' GiB', 2);
  if (bytes >= 1024 ** 2) return fmtMetric(bytes / 1024 ** 2, ' MiB');
  if (bytes >= 1024) return fmtMetric(bytes / 1024, ' KiB');
  return fmtMetric(bytes, ' B', 0);
}

function dateMillis(value: unknown): number | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value)) return null;
  const millis = Date.parse(value);
  return Number.isFinite(millis) ? millis : null;
}

export function fmtTimestamp(value: unknown): string {
  const millis = dateMillis(value);
  return millis === null ? '未报告时间' : `${new Date(millis).toISOString().slice(0, 19).replace('T', ' ')} UTC`;
}

export function fmtFreshness(value: unknown, asOf: number): string {
  const millis = dateMillis(value);
  if (millis === null || !asOf || millis > asOf + 5_000) return '新鲜度未知';
  const seconds = Math.max(0, Math.round((asOf - millis) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

export const RUNNERS: Record<string, string> = {
  harbor: 'Harbor', harbor_gateway: 'Harbor Gateway', direct: 'Direct', vlmevalkit: 'VLMEvalKit',
};

export const JOBS: Record<string, string> = {
  queued: '排队', running: '运行', cancelling: '停止中', finished: '已完成', failed: '失败',
  interrupted: '已中断', paused_by_guard: '保护暂停',
};

export const BACKUPS: Record<string, { label: string; tone: Tone }> = {
  ok: { label: '备份正常', tone: 'ok' },
  missing: { label: '尚无备份', tone: 'warning' },
  multiple: { label: '超过保留份数', tone: 'warning' },
  oversize: { label: '超过体积上限', tone: 'error' },
  unavailable: { label: '备份无法读取', tone: 'warning' },
};

export const SOURCE_STATUS: Record<string, string> = {
  indexed: '已索引', ready: '就绪', running: '处理中', error: '失败', partial_permission_denied: '部分目录不可读',
  permission_denied: '不可读', error_or_permission_denied: '失败或不可读', pending_cold_scan: '等待冷层扫描',
  non_task_bundle: '非任务归档', empty_contract_ready: '已接入 · 暂无数据',
  registered_media_source: '已登记媒体源', raw_visible_unintegrated: '尚未接入索引',
};

export function displayLabel(value: unknown, labels: Record<string, string>): string {
  const key = textValue(value);
  return Object.hasOwn(labels, key) ? labels[key] : key;
}
