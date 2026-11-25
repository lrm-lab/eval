import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, AlertTriangle, CheckCircle2, HardDrive, RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react';
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
