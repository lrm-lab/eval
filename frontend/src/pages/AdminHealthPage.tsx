import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, AlertTriangle, Archive, CheckCircle2, Cpu, FileText, HardDrive, RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDateShort } from '../lib/format';
import { useAppStore } from '../lib/store';
import { PageTransition } from '../components/motion/primitives';
import { CardGridSkeleton, StateNoticeCard } from '../components/ui/Skeletons';

interface AdminHealthResp {
  methodology_version: string;
  indexer: Record<string, unknown>;
  storage: {
    production_mode: boolean;
    root_free_gb: number;
    root_used_pct: number;
    state_fs_free_gb: number;
    root_disk_alert: boolean;
    write_guard_ok: boolean;
    write_guard_error: string | null;
    local_runtime_mb: number;
    local_budget_mb: number;
    budget_alert: boolean;
    isolation_probes: Record<string, Record<string, unknown> | string>;
  };
  source_coverage: {
    by_runner: Array<{ runner: string; batches: number; trials: number }>;
    running_placeholders?: Array<{
      batch_id: string;
      batch_name: string;
      runner: string;
      planned: number | null;
      recorded: number;
      batch_status: string | null;
      indexed_at: string;
    }>;
    cold_cache: Array<{ cache_kind: string; status: string; files: number; items: number }>;
    cold_files?: Array<{
      file_path: string;
      cache_kind: string;
      size_bytes: number;
      item_count: number;
      status: string;
      updated_at: string;
    }>;
    by_source: Array<{
      source_key: string;
      status: string;
      file_count?: number;
      batch_count?: number;
      sample_count?: number;
      note?: string;
    }>;
    warnings: Array<{
      warning_id: number;
      tier: string;
      severity: string;
      code: string;
      target_path: string;
      message: string;
      detected_at: string;
    }>;
  };
  gateway_profiles?: Array<{
    profile_id: string;
    display_name: string;
    kind: string;
    base_url: string;
    api_key_env: string;
    key_configured: boolean;
  }>;
  runner_status?: {
    isolated_runner_ready: boolean;
    runner_connected?: boolean;
    runner_pid?: number | null;
    supported_runners?: string[];
    reason?: string | null;
    jobs_by_status: Record<string, number>;
    docker_group_attached: boolean;
  };
}

interface CgroupTelemetry {
  source: string;
  scope?: string;
  cpu_usage_usec: number | null;
  memory_peak_bytes: number | null;
  memory_current_bytes?: number | null;
  memory_max_bytes?: number | null;
  pids_current?: number | null;
  note?: string;
}

interface ExecutionTelemetryResp {
  generated_at: string;
  web_cgroup: CgroupTelemetry;
  streaming: { mode: string; sample_file: string; note: string };
  stdio_policy: {
    per_job_cap_bytes: number;
    segment_bytes: number;
    backup_segments: number;
    cumulative_cap_bytes: number;
    detail_read_bytes: number;
  };
  ops: {
    daemon_logs: {
      logs_dir_present: boolean;
      components: Array<{
        component: string;
        max_files: number;
        max_bytes_per_file: number;
        cap_bytes: number;
        current_bytes: number;
        files_present: number;
      }>;
      total_bytes: number;
      cap_bytes: number;
      within_cap: boolean;
    };
    job_stdio_usage: { total_bytes: number | null; jobs_with_stdio: number | null; cap_bytes: number; within_cap: boolean | null };
    backup: {
      status: 'ok' | 'missing' | 'multiple' | 'oversize' | 'unavailable';
      retained_count: number | null;
      retention_limit: number;
      size_limit_bytes: number;
      latest_name: string | null;
      latest_size_bytes: number | null;
      latest_at: string | null;
      age_hours: number | null;
      note?: string;
    };
  } | null;
}

function fmtBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—';
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}

function UsageBar({ value, cap, alert }: { value: number; cap: number; alert: boolean }) {
  const pct = Math.min(100, (value / Math.max(1, cap)) * 100);
  return (
    <div className="h-1.5 w-full rounded-full bg-[var(--border-subtle)] overflow-hidden">
      <div
        className="h-full rounded-full"
        style={{ width: `${pct}%`, backgroundColor: alert ? 'var(--sem-err-fg)' : 'var(--sem-pass-fg)' }}
      />
    </div>
  );
}

const OPS_GRID = 'grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4';
const OPS_CARD = 'rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 h-[196px] overflow-hidden text-xs flex flex-col';

const BACKUP_LABEL: Record<string, string> = {
  ok: '单份备份正常',
  missing: '尚无备份',
  multiple: '超出单份保留',
  oversize: '超出 45 MB',
  unavailable: '无法读取',
};

function OpsTelemetryPanel({ q }: { q: { data?: ExecutionTelemetryResp; isPending: boolean; isError: boolean; error: unknown } }) {
  if (q.isPending) {
    return (
      <div className={OPS_GRID} aria-busy="true" aria-label="运维遥测加载中">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className={`${OPS_CARD} motion-safe:animate-pulse`}>
            <div className="h-3 w-1/2 rounded bg-[var(--border-subtle)]" />
            <div className="mt-4 h-6 w-2/3 rounded bg-[var(--border-subtle)]" />
            <div className="mt-4 h-1.5 w-full rounded bg-[var(--border-subtle)]" />
          </div>
        ))}
      </div>
    );
  }
  const d = q.data;
  if (q.isError || !d || !d.ops) {
    return (
      <div className="h-[832px] md:h-[408px] xl:h-[196px] overflow-hidden flex items-start">
        <StateNoticeCard
          kind="error"
          title="运维遥测接口不可用"
          description={q.isError ? (q.error as Error).message : '未返回运维状态（需 admin 角色）'}
        />
      </div>
    );
  }
  const { daemon_logs: logs, job_stdio_usage: stdio, backup } = d.ops;
  const cg = d.web_cgroup;
  const backupOk = backup.status === 'ok';
  return (
    <div className={OPS_GRID}>
      <div className={OPS_CARD} aria-label="守护进程日志轮转上限">
        <div className="flex items-center justify-between text-[var(--text-secondary)]">
          <span className="inline-flex items-center gap-1.5">
            <FileText className="w-4 h-4 text-[var(--brand-primary)]" /> 守护进程日志轮转
          </span>
          <span className="font-mono tabular-nums">
            {fmtBytes(logs.total_bytes)} / {fmtBytes(logs.cap_bytes)}
          </span>
        </div>
        <div className="mt-2">
          <UsageBar value={logs.total_bytes} cap={logs.cap_bytes} alert={!logs.within_cap} />
        </div>
        <div className="mt-2.5 space-y-1 font-mono text-[11px]">
          {logs.components.map((c) => (
            <div key={c.component} className="flex justify-between gap-2 text-[var(--text-secondary)]">
              <span>{c.component}.log</span>
              <span className="tabular-nums">
                {fmtBytes(c.current_bytes)} · {c.files_present}/{c.max_files} 份 × {fmtBytes(c.max_bytes_per_file)}
              </span>
            </div>
          ))}
        </div>
        <div className="mt-auto text-[11px] text-[var(--text-muted)] truncate">
          {logs.logs_dir_present ? '达到单文件上限时轮转' : '日志目录尚未创建'}
        </div>
      </div>

      <div className={OPS_CARD} aria-label="作业 stdio 限额">
        <div className="flex items-center justify-between text-[var(--text-secondary)]">
          <span className="inline-flex items-center gap-1.5">
            <HardDrive className="w-4 h-4 text-[var(--brand-primary)]" /> 作业 stdio 累计留存
          </span>
          <span className="font-mono tabular-nums">
            {fmtBytes(stdio.total_bytes)} / {fmtBytes(stdio.cap_bytes)}
          </span>
        </div>
        <div className="mt-2">
          {stdio.total_bytes != null ? (
            <UsageBar value={stdio.total_bytes} cap={stdio.cap_bytes} alert={stdio.within_cap === false} />
          ) : <div className="h-1.5 rounded-full bg-[var(--border-subtle)]" />}
        </div>
        <div className="mt-2.5 space-y-1 font-mono text-[11px] text-[var(--text-secondary)]">
          <div className="flex justify-between">
            <span>含输出作业</span>
            <span className="tabular-nums">{stdio.jobs_with_stdio ?? '—'}</span>
          </div>
          <div className="flex justify-between">
            <span>单作业硬上限</span>
            <span className="tabular-nums">
              {fmtBytes(d.stdio_policy.per_job_cap_bytes)}（{1 + d.stdio_policy.backup_segments} × {fmtBytes(d.stdio_policy.segment_bytes)}）
            </span>
          </div>
          <div className="flex justify-between">
            <span>详情尾部读取</span>
            <span className="tabular-nums">≤ {fmtBytes(d.stdio_policy.detail_read_bytes)} / 200 行</span>
          </div>
        </div>
        <div className="mt-auto text-[11px] text-[var(--text-muted)] truncate">
          {stdio.total_bytes == null ? '无法读取作业日志用量' : '超出累计上限时优先裁剪最旧已结束作业'}
        </div>
      </div>

      <div className={OPS_CARD} aria-label="SQLite 单份 db.zst 备份状态">
        <div className="flex items-center justify-between text-[var(--text-secondary)]">
          <span className="inline-flex items-center gap-1.5">
            <Archive className="w-4 h-4 text-[var(--brand-primary)]" /> 单份 .db.zst 备份
          </span>
          <span
            className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
              backupOk ? 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]' : 'bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]'
            }`}
          >
            {BACKUP_LABEL[backup.status] ?? backup.status}
          </span>
        </div>
        <div className="mt-2 font-mono text-[11px] text-[var(--text-primary)] truncate" title={backup.latest_name ?? undefined}>
          {backup.latest_name ?? '—'}
        </div>
        <div className="mt-2 space-y-1 font-mono text-[11px] text-[var(--text-secondary)]">
          <div className="flex justify-between">
            <span>体积 / 上限</span>
            <span className="tabular-nums">
              {backup.latest_size_bytes == null ? '—' : `${(backup.latest_size_bytes / 1e6).toFixed(2)} MB`} / {(backup.size_limit_bytes / 1e6).toFixed(0)} MB
            </span>
          </div>
          <div className="flex justify-between">
            <span>保留份数</span>
            <span className="tabular-nums">
              {backup.retained_count ?? '—'} / {backup.retention_limit}
            </span>
          </div>
          <div className="flex justify-between">
            <span>最近备份</span>
            <span className="tabular-nums">
              {backup.latest_at ? `${fmtDateShort(backup.latest_at)}（${backup.age_hours} h 前）` : '—'}
            </span>
          </div>
        </div>
        <div className="mt-auto text-[11px] text-[var(--text-muted)] truncate" title={backup.note}>
          {backup.note || 'zstd 在线快照，原子替换，仅保留最新 1 份'}
        </div>
      </div>

      <div className={OPS_CARD} aria-label="可选遥测">
        <div className="flex items-center justify-between text-[var(--text-secondary)]">
          <span className="inline-flex items-center gap-1.5">
            <Cpu className="w-4 h-4 text-[var(--brand-primary)]" /> 可选遥测
          </span>
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] font-mono ${
              cg.source === 'cgroup_v2'
                ? 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                : 'bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)]'
            }`}
          >
            {cg.source}
          </span>
        </div>
        <div className="mt-2 space-y-1 font-mono text-[11px] text-[var(--text-secondary)]">
          <div className="flex justify-between">
            <span>Web cgroup 作用域</span>
            <span>{cg.scope ?? 'unknown'}</span>
          </div>
          <div className="flex justify-between">
            <span>内存峰值 / 上限</span>
            <span className="tabular-nums">
              {fmtBytes(cg.memory_peak_bytes)} / {fmtBytes(cg.memory_max_bytes)}
            </span>
          </div>
          <div className="flex justify-between">
            <span>CPU 累计</span>
            <span className="tabular-nums">
              {cg.cpu_usage_usec != null ? `${(cg.cpu_usage_usec / 1e6).toFixed(1)} s` : '—'}
            </span>
          </div>
          <div className="flex justify-between">
            <span>流式 TTFT/TPS</span>
            <span>在作业详情查看</span>
          </div>
        </div>
        <div className="mt-auto text-[11px] text-[var(--text-muted)] line-clamp-2" title={cg.note || d.streaming.note}>
          {cg.note || d.streaming.note}
        </div>
      </div>
    </div>
  );
}

export function AdminHealthPage() {
  const user = useAppStore((s) => s.user);
  const pushToast = useAppStore((s) => s.pushToast);
  const qc = useQueryClient();
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const isAdmin = user?.role === 'admin';

  const healthQ = useQuery({
    queryKey: ['admin-health'],
    enabled: isAdmin,
    queryFn: () => apiFetch<AdminHealthResp>('/api/v1/admin/health'),
  });

  const telemetryQ = useQuery({
    queryKey: ['admin-execution-telemetry'],
    enabled: isAdmin,
    queryFn: () => apiFetch<ExecutionTelemetryResp>('/api/v1/execution/telemetry'),
  });

  const reindexMut = useMutation({
    mutationFn: async (opts: { force: boolean; includeCold: boolean }) => {
      setBusyAction(opts.includeCold ? 'full' : 'hot');
      return apiFetch<{ ok: boolean }>(`/api/v1/admin/reindex?force=${opts.force}&include_cold=${opts.includeCold}`, {
        method: 'POST',
      });
    },
    onSuccess: () => {
      pushToast('success', '索引扫描完成，已刷新健康视图');
      qc.invalidateQueries({ queryKey: ['admin-health'] });
      qc.invalidateQueries({ queryKey: ['admin-execution-telemetry'] });
      qc.invalidateQueries({ queryKey: ['system-overview'] });
    },
    onError: (err: Error) => pushToast('error', `重扫失败：${err.message}`),
    onSettled: () => setBusyAction(null),
  });

  const reloadTaxMut = useMutation({
    mutationFn: async () => {
      setBusyAction('tax');
      return apiFetch<{ ok: boolean }>('/api/v1/admin/taxonomy/reload', { method: 'POST' });
    },
    onSuccess: () => {
      pushToast('success', 'taxonomy.yaml 已热重载并同步至 SQLite');
      qc.invalidateQueries({ queryKey: ['datasets-tree'] });
      qc.invalidateQueries({ queryKey: ['admin-health'] });
    },
    onError: (err: Error) => pushToast('error', `热重载失败：${err.message}`),
    onSettled: () => setBusyAction(null),
  });

  if (!isAdmin) {
    return (
      <div className="max-w-4xl mx-auto px-5 py-10">
        <StateNoticeCard
          kind="forbidden"
          title="仅管理员可访问系统健康与运维面板"
          description="当前账户缺少 admin 角色权限，无法查看本地磁盘探针、索引告警或触发重建。"
        />
      </div>
    );
  }

  const data = healthQ.data;
  const st = data?.storage;
  const budgetPct = st ? Math.min(100, (st.local_runtime_mb / Math.max(1, st.local_budget_mb)) * 100) : 0;

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <Activity className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>系统健康、磁盘预算与数据源覆盖</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            197 本地 ext4 预算硬上限 ≤ 750 MB · 安全隔离探针（0700/0600 Fail-Closed）· 索引告警与热重载
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <button
            type="button"
            disabled={busyAction !== null}
            onClick={() => reindexMut.mutate({ force: false, includeCold: false })}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1.5 font-medium text-[var(--text-primary)] hover:bg-[var(--bg-elevated)] disabled:opacity-50 cursor-pointer"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${busyAction === 'hot' ? 'animate-spin' : ''}`} />
            <span>热层增量扫描</span>
          </button>
          <button
            type="button"
            disabled={busyAction !== null}
            onClick={() => reindexMut.mutate({ force: true, includeCold: true })}
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-3 py-1.5 font-semibold text-white disabled:opacity-50 cursor-pointer"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${busyAction === 'full' ? 'animate-spin' : ''}`} />
            <span>重扫热层 + 冷层</span>
          </button>
          <button
            type="button"
            disabled={busyAction !== null}
            onClick={() => reloadTaxMut.mutate()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-1.5 font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:opacity-50 cursor-pointer"
          >
            <span>重载 taxonomy.yaml</span>
          </button>
        </div>
      </div>

      <section aria-label="日志限额、备份与可选遥测" className="space-y-2">
        <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">日志限额、备份与可选遥测</h2>
        <OpsTelemetryPanel q={telemetryQ} />
      </section>

      {healthQ.isLoading ? (
        <CardGridSkeleton count={4} />
      ) : healthQ.isError ? (
        <StateNoticeCard kind="error" title="健康接口请求失败" description={(healthQ.error as Error).message} onRetry={() => healthQ.refetch()} />
      ) : st && data ? (
        <>
          {/* Storage & Budget Cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="flex items-center justify-between text-xs text-[var(--text-secondary)]">
                <span className="inline-flex items-center gap-1.5">
                  <HardDrive className="w-4 h-4 text-[var(--brand-primary)]" /> 本地运行目录占用
                </span>
                <span className="font-mono tabular-nums">
                  {st.local_runtime_mb.toFixed(1)} / {st.local_budget_mb} MB
                </span>
              </div>
              <div className="mt-3 h-2.5 w-full rounded-full bg-[var(--border-subtle)] overflow-hidden">
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${budgetPct}%`,
                    backgroundColor: st.budget_alert ? 'var(--sem-err-fg)' : 'var(--sem-pass-fg)',
                  }}
                />
              </div>
              <div className="mt-2 text-[11px] text-[var(--text-muted)]">
                警戒线 600 MB · 硬上限 {st.local_budget_mb} MB · {st.budget_alert ? '⚠ 已达警戒线' : '健康余量充足'}
              </div>
            </div>

            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-xs text-[var(--text-secondary)]">根分区 / 与状态盘余量</div>
              <div className="mt-1.5 text-2xl font-bold tabular-nums text-[var(--text-primary)]">
                {st.root_free_gb} GB <span className="text-xs font-normal text-[var(--text-muted)]">已用 {st.root_used_pct}%</span>
              </div>
              <div className="mt-1.5 text-[11px] flex items-center gap-1.5">
                {st.write_guard_ok ? (
                  <span className="text-[var(--sem-pass-fg)] inline-flex items-center gap-1">
                    <CheckCircle2 className="w-3.5 h-3.5" /> 写入保护正常（状态盘剩余 {st.state_fs_free_gb} GB）
                  </span>
                ) : (
                  <span className="text-[var(--sem-err-fg)] inline-flex items-center gap-1">
                    <AlertTriangle className="w-3.5 h-3.5" /> {st.write_guard_error || '写入保护触发 (503)'}
                  </span>
                )}
              </div>
            </div>

            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-xs text-[var(--text-secondary)]">运行模式与配置挂载类型</div>
              <div className="mt-1.5 text-lg font-bold text-[var(--text-primary)]">
                {st.production_mode ? 'Production (严格 Fail-Closed)' : 'Development'}
              </div>
              <div className="mt-1.5 text-[11px] text-[var(--text-muted)] font-mono">
                config_dir fstype: {String(st.isolation_probes.config_dir_fstype || 'unknown')}
              </div>
            </div>
          </div>

          {/* Security Isolation Probes */}
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
            <h2 className="m-0 text-sm font-bold text-[var(--text-primary)] mb-3">存储隔离安全探针（0700 / 0600）</h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 text-xs">
              {Object.entries(st.isolation_probes)
                .filter(([, v]) => typeof v === 'object' && v !== null)
                .map(([key, val]) => {
                  const obj = val as Record<string, unknown>;
                  const ok = obj.ok !== false;
                  return (
                    <div
                      key={key}
                      className={`rounded-lg border p-3 ${
                        ok
                          ? 'border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)]/35'
                          : 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)]/50'
                      }`}
                    >
                      <div className="flex items-center justify-between font-semibold">
                        <span className="font-mono text-[var(--text-primary)]">{key}</span>
                        {ok ? (
                          <ShieldCheck className="w-4 h-4 text-[var(--sem-pass-fg)]" />
                        ) : (
                          <ShieldAlert className="w-4 h-4 text-[var(--sem-err-fg)]" />
                        )}
                      </div>
                      <div className="mt-1 text-[11px] text-[var(--text-secondary)] font-mono break-all">
                        {ok
                          ? `fstype=${String(obj.fstype || '—')} · mode=${String(obj.mode || '—')}`
                          : `${String(obj.code || 'ERROR')}: ${String(obj.message || '')}`}
                      </div>
                    </div>
                  );
                })}
            </div>
          </div>

          {/* Source Coverage Table */}
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
            <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex items-center justify-between">
              <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">
                数据源覆盖全景（{data.source_coverage.by_source.length} 源）
              </h2>
              <div className="flex flex-wrap gap-2 text-[11px]">
                {data.source_coverage.by_runner.map((r) => (
                  <span key={r.runner} className="rounded px-2 py-0.5 bg-[var(--bg-surface)] border border-[var(--border-subtle)] tabular-nums">
                    {r.runner}: {r.batches} 批 / {r.trials} trials
                  </span>
                ))}
              </div>
            </div>
            <div className="overflow-x-auto max-h-96">
              <table className="w-full text-left border-collapse text-xs">
                <thead>
                  <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)]/70 text-[var(--text-secondary)]">
                    <th className="py-2 px-3.5">数据源键</th>
                    <th className="py-2 px-3">接入状态</th>
                    <th className="py-2 px-3 text-right">文件 / 批次 / 样本</th>
                    <th className="py-2 px-3.5">说明</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-subtle)]">
                  {data.source_coverage.by_source.map((s, idx) => (
                    <tr key={`${s.source_key}-${idx}`}>
                      <td className="py-2 px-3.5 font-mono text-[var(--text-primary)]">{s.source_key}</td>
                      <td className="py-2 px-3">
                        <span className="rounded px-1.5 py-0.5 text-[11px] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                          {s.status}
                        </span>
                      </td>
                      <td className="py-2 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                        {s.file_count ?? '—'} / {s.batch_count ?? '—'} / {s.sample_count ?? '—'}
                      </td>
                      <td className="py-2 px-3.5 text-[var(--text-muted)]">{s.note || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Gateway Profiles & Runner Status (M2) */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 text-xs space-y-2.5">
              <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">
                服务端 Gateway / Teacher / Judge Profiles（零密钥明文）
              </h2>
              {(data.gateway_profiles || []).length === 0 ? (
                <div className="text-[var(--text-muted)]">暂未配置网关 Profile。</div>
              ) : (
                <div className="space-y-2">
                  {data.gateway_profiles!.map((gp) => (
                    <div key={gp.profile_id} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <div className="font-semibold text-[var(--text-primary)]">
                          {gp.display_name} <code className="font-mono text-[10px] text-[var(--text-muted)]">({gp.kind})</code>
                        </div>
                        <div className="font-mono text-[10px] text-[var(--text-muted)] truncate">
                          {gp.base_url} · env={gp.api_key_env}
                        </div>
                      </div>
                      <span
                        className={`rounded-full px-2 py-0.5 text-[10px] font-semibold shrink-0 ${
                          gp.key_configured
                            ? 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                            : 'bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]'
                        }`}
                      >
                        {gp.key_configured ? '凭据就绪' : '待配 0600 密钥'}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 text-xs space-y-2.5">
              <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">
                运行中占位批次（running_placeholder）与 Runner 状态
              </h2>
              <div className="flex flex-wrap gap-2 text-[11px]">
                <span className="rounded px-2 py-0.5 bg-[var(--bg-elevated)] font-mono">
                  M3 隔离 Runner:{' '}
                  {data.runner_status?.isolated_runner_ready
                    ? `就绪 (isolated_runner_ready${data.runner_status.runner_pid ? `, PID ${data.runner_status.runner_pid}` : ''})`
                    : '未连接（只读与 --plan 模式）'}
                </span>
                <span className="rounded px-2 py-0.5 bg-[var(--bg-elevated)] font-mono">
                  Web Docker 组隔离: {data.runner_status?.docker_group_attached ? '异常' : '已隔离 (无 docker 组)'}
                </span>
                <span className="rounded px-2 py-0.5 bg-[var(--bg-elevated)] font-mono">
                  运行中占位: {(data.source_coverage.running_placeholders || []).length}
                </span>
                {data.runner_status?.supported_runners && data.runner_status.supported_runners.length > 0 && (
                  <span className="rounded px-2 py-0.5 bg-[var(--bg-elevated)] font-mono">
                    可用运行器: {data.runner_status.supported_runners.join(', ')}
                  </span>
                )}
              </div>
              {(data.source_coverage.running_placeholders || []).length > 0 && (
                <div className="space-y-1.5">
                  {data.source_coverage.running_placeholders!.map((rp) => (
                    <div key={rp.batch_id} className="rounded bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-[11px] flex justify-between">
                      <span>{rp.batch_name} ({rp.runner})</span>
                      <span>{rp.recorded}/{rp.planned ?? '?'}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Warnings */}
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
            <h2 className="m-0 text-sm font-bold text-[var(--text-primary)] mb-3">
              索引告警日志（最近 {data.source_coverage.warnings.length} 条）
            </h2>
            {data.source_coverage.warnings.length === 0 ? (
              <div className="text-xs text-[var(--sem-pass-fg)]">暂无索引告警，全部解析干净通过。</div>
            ) : (
              <div className="space-y-2 max-h-72 overflow-y-auto">
                {data.source_coverage.warnings.map((w) => (
                  <div
                    key={w.warning_id}
                    className="rounded-lg border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)]/50 px-3 py-2 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono font-semibold text-[var(--sem-zero-fg)]">
                        [{w.tier}] {w.code}
                      </span>
                      <span className="text-[11px] tabular-nums text-[var(--text-muted)]">{fmtDateShort(w.detected_at)}</span>
                    </div>
                    <div className="mt-1 text-[var(--text-primary)]">{w.message}</div>
                    <div className="mt-0.5 text-[11px] font-mono text-[var(--text-muted)] break-all">{w.target_path}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      ) : null}
    </PageTransition>
  );
}
