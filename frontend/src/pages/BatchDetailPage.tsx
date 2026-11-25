import React, { useMemo, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { ArrowLeft, BookmarkPlus, CheckCircle2, Plus, Search, ShieldAlert } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDateShort, fmtPct, runScopeLabel } from '../lib/format';
import { useAppStore } from '../lib/store';
import type { BatchDTO, BatchTasksResponse } from '../types/api';
import { PageTransition } from '../components/motion/primitives';
import { MultiAttemptPill } from '../components/ui/MultiAttemptPill';
import { TokenBreakdownBar } from '../components/ui/TokenBreakdownBar';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

export function BatchDetailPage() {
  const { batchId = '' } = useParams<{ batchId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const user = useAppStore((s) => s.user);
  const addToCompare = useAppStore((s) => s.addToCompare);
  const pushToast = useAppStore((s) => s.pushToast);

  const [taskQuery, setTaskQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'passed' | 'failed' | 'fault'>('all');
  const [selectedTasks, setSelectedTasks] = useState<string[]>([]);
  const [sampleSetName, setSampleSetName] = useState('');

  const batchQ = useQuery({
    queryKey: ['batch-detail', batchId],
    queryFn: () => apiFetch<BatchDTO>(`/api/v1/batches/${encodeURIComponent(batchId)}`),
  });

  const tasksQ = useQuery({
    queryKey: ['batch-tasks', batchId],
    queryFn: () => apiFetch<BatchTasksResponse>(`/api/v1/batches/${encodeURIComponent(batchId)}/tasks`),
  });

  const saveSampleSetMut = useMutation({
    mutationFn: () =>
      apiFetch<{ sample_set_id: string }>('/api/v1/sample-sets', {
        method: 'POST',
        body: JSON.stringify({
          name: sampleSetName.trim(),
          dataset_release_id: batchQ.data?.dataset_release_id,
          task_ids: selectedTasks,
          project_id: batchQ.data?.project_id || 'default',
        }),
      }),
    onSuccess: (res) => {
      pushToast('success', `已保存自定义样本集（${selectedTasks.length} 题 · ${res.sample_set_id}），可在对比页直接选用`);
      setSampleSetName('');
      setSelectedTasks([]);
    },
    onError: (err: Error) => pushToast('error', `保存样本集失败：${err.message}`),
  });

  const batch = batchQ.data;
  const tasks = tasksQ.data?.items || [];

  const filteredTasks = useMemo(() => {
    const kw = taskQuery.trim().toLowerCase();
    return tasks.filter((t) => {
      if (kw && !t.task_id.toLowerCase().includes(kw)) return false;
      if (statusFilter === 'passed') return t.passed > 0;
      if (statusFilter === 'failed') return t.scored > 0 && t.passed === 0;
      if (statusFilter === 'fault') return t.recorded > t.scored;
      return true;
    });
  }, [tasks, taskQuery, statusFilter]);

  const toggleTask = (tid: string) => {
    setSelectedTasks((prev) => (prev.includes(tid) ? prev.filter((x) => x !== tid) : [...prev, tid]));
  };

  const openTrial = (trialId: string) => {
    const ret = encodeURIComponent(location.pathname + location.search);
    navigate(`/eval/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(trialId)}?returnTo=${ret}`);
  };

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <Link
            to="/eval/reports"
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] no-underline shrink-0"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> 返回报告中心
          </Link>
          <div className="min-w-0">
            <h1 className="m-0 text-lg font-bold text-[var(--text-primary)] truncate">{batch?.batch_name || batchId}</h1>
            {batch && (
              <div className="mt-0.5 text-xs text-[var(--text-secondary)] flex flex-wrap gap-1.5">
                <span>{batch.model_canonical || batch.model_route || '—'}</span>
                <span>·</span>
                <span>
                  {batch.harness || batch.runner} ({batch.runner})
                </span>
                <span>·</span>
                <span className="font-mono">{batch.scoring_protocol}</span>
                <span>·</span>
                <span>{runScopeLabel(batch.run_scope)}</span>
              </div>
            )}
          </div>
        </div>

        {batch && !batch.is_running_placeholder && (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => {
                const payload = { batch, tasks: tasksQ.data || null };
                const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `batch-${batch.batch_id}.json`;
                a.click();
                URL.revokeObjectURL(url);
                pushToast('success', '已导出批次 JSON 摘要报告');
              }}
              className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
            >
              导出 JSON
            </button>
            <button
              type="button"
              onClick={() => window.print()}
              className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
            >
              打印 / PDF
            </button>
            <button
              type="button"
              onClick={() => addToCompare({ id: batch.batch_id, kind: 'batch', batch_id: batch.batch_id, alias: batch.batch_name })}
              className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-3 py-1.5 text-xs font-semibold text-white cursor-pointer"
            >
              <Plus className="w-3.5 h-3.5" /> 加入对比
            </button>
          </div>
        )}
      </div>

      {batchQ.isLoading ? (
        <TableSkeleton rows={6} cols={4} />
      ) : batchQ.isError ? (
        <StateNoticeCard kind="error" title="批次详情加载失败" description={(batchQ.error as Error).message} onRetry={() => batchQ.refetch()} />
      ) : batch ? (
        <>
          {/* Summary KPI Grid — Strict separation of Official Result vs Valid Mean */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5 text-xs">
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-[var(--text-muted)]">逐题有效均值（valid_mean）</div>
              <div className="mt-1.5 text-2xl font-bold tabular-nums text-[var(--text-primary)]">
                {batch.valid_mean !== null && batch.valid_mean !== undefined
                  ? fmtPct(batch.valid_mean <= 1 ? batch.valid_mean * 100 : batch.valid_mean)
                  : '—'}
              </div>
              <div className="mt-1 text-[11px] text-[var(--text-muted)] tabular-nums">
                有效评分 {batch.scored} / 计划 {batch.planned || batch.recorded} · 未评分 {batch.unscored}
              </div>
            </div>

            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-[var(--text-muted)]">官方汇总结果（独立口径）</div>
              {batch.official_result ? (
                <>
                  <div className="mt-1.5 text-2xl font-bold tabular-nums text-[var(--sem-pass-fg)]">
                    {batch.official_result.score}
                  </div>
                  <div className="mt-1 text-[11px] text-[var(--text-secondary)]">
                    {batch.official_result.metric} · 分母 {batch.official_result.denominator} ({batch.official_result.source})
                  </div>
                </>
              ) : (
                <div className="mt-2 text-xs text-[var(--text-muted)]">无独立官方汇总文件（以逐题有效评分为准）</div>
              )}
            </div>

            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-[var(--text-muted)]">Pass@k 指标（显式可算分母）</div>
              {tasksQ.data?.pass_at_k_summary && Object.keys(tasksQ.data.pass_at_k_summary).length > 0 ? (
                <div className="mt-1.5 space-y-1.5">
                  <div className="text-[11px] font-medium text-[var(--brand-primary)] tabular-nums">
                    {tasksQ.data.unique_tasks} 题 / {tasksQ.data.total_attempts} 尝试
                  </div>
                  {Object.entries(tasksQ.data.pass_at_k_summary).map(([k, info]) => (
                    <div key={k} className="space-y-0.5">
                      <div className="flex items-center justify-between gap-2 tabular-nums">
                        <span className="font-mono text-[var(--text-secondary)]">pass@{k.replace(/^k/, '')}</span>
                        <span className="font-bold text-[var(--text-primary)]">
                          {info.value !== null ? fmtPct(info.value * 100) : '—'}{' '}
                          <span className="text-[10px] font-normal text-[var(--text-muted)]">
                            ({info.computable_tasks}/{info.total_tasks})
                          </span>
                        </span>
                      </div>
                      {info.ci95 && (
                        <div className="text-[10px] tabular-nums text-[var(--text-muted)]">
                          95% CI: [{fmtPct(info.ci95[0] * 100)}, {fmtPct(info.ci95[1] * 100)}]
                        </div>
                      )}
                      {info.note && <div className="text-[10px] text-[var(--sem-zero-fg)]">{info.note}</div>}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-2 text-[var(--text-muted)]">加载中或无多尝试统计</div>
              )}
            </div>

            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-[var(--text-muted)]">结果分布与 Token 汇总</div>
              <div className="mt-1.5 flex flex-wrap gap-1.5 text-[11px] tabular-nums">
                {Object.entries(batch.outcomes || {}).map(([k, v]) => (
                  <span key={k} className="rounded px-1.5 py-0.5 bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                    {k}: <strong>{v}</strong>
                  </span>
                ))}
              </div>
              {batch.usage && (
                <div className="mt-2">
                  <TokenBreakdownBar
                    tokens={{
                      semantics: batch.token_semantics,
                      input: batch.usage.input_tokens?.total ?? null,
                      cached: batch.usage.cached_tokens?.total ?? null,
                      output: batch.usage.output_tokens?.total ?? null,
                      reasoning: batch.usage.reasoning_tokens?.total ?? null,
                    }}
                  />
                </div>
              )}
            </div>
          </div>

          {/* Metadata & Failure Breakdown Strip */}
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5 flex flex-wrap items-center justify-between gap-3 text-[11px] text-[var(--text-secondary)]">
            <div className="flex flex-wrap items-center gap-3">
              <span>
                Generation: <code className="font-mono text-[var(--text-primary)]">{batch.generation_id.slice(0, 12)}</code>
              </span>
              <span>
                Dataset Release: <code className="font-mono text-[var(--text-primary)]">{batch.dataset_release_id || '—'}</code>
              </span>
              <span>
                更新时间: <span className="tabular-nums">{fmtDateShort(batch.finished_at || batch.indexed_at)}</span>
              </span>
              {batch.source_host_redacted && <span>Host: {batch.source_host_redacted}</span>}
            </div>
            {batch.failure_kinds && Object.keys(batch.failure_kinds).length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <ShieldAlert className="w-3.5 h-3.5 text-[var(--sem-err-fg)]" />
                {Object.entries(batch.failure_kinds).map(([fk, cnt]) => (
                  <span key={fk} className="rounded px-1.5 py-0.5 bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)] font-mono">
                    {fk}: {cnt}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* VLMEvalKit Subject / Metrics Sub-breakdown (A-10) */}
          {batch.vlmeval_metrics && (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 text-xs">
              <div className="font-bold text-[var(--text-primary)] mb-2">
                VLMEvalKit 官方分项指标（vlmevalkit/metrics.json）
              </div>
              {Array.isArray(batch.vlmeval_metrics) ? (
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
                  {batch.vlmeval_metrics.map((row, idx) => (
                    <div key={idx} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-2.5">
                      {Object.entries(row).map(([k, v]) => (
                        <div key={k} className="flex justify-between gap-2 text-[11px]">
                          <span className="text-[var(--text-muted)] truncate">{k}</span>
                          <span className="font-mono font-semibold text-[var(--text-primary)]">
                            {typeof v === 'number' ? v.toFixed(4) : String(v ?? '—')}
                          </span>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
                  {Object.entries(batch.vlmeval_metrics).map(([k, v]) => (
                    <div key={k} className="flex items-center justify-between gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2">
                      <span className="text-[var(--text-secondary)] truncate">{k}</span>
                      <span className="font-mono font-semibold text-[var(--text-primary)]">
                        {typeof v === 'number' ? v.toFixed(4) : typeof v === 'object' ? JSON.stringify(v) : String(v ?? '—')}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Task Matrix + MultiAttemptPill + Save as Custom Sample Set */}
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
            <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-3 text-xs">
              <div className="flex items-center gap-2">
                <span className="font-bold text-[var(--text-primary)]">逐题矩阵（{filteredTasks.length} / {tasks.length} 题）</span>
                <div className="relative">
                  <Search className="w-3.5 h-3.5 text-[var(--text-muted)] absolute left-2.5 top-1/2 -translate-y-1/2" />
                  <input
                    type="text"
                    value={taskQuery}
                    onChange={(e) => setTaskQuery(e.target.value)}
                    placeholder="过滤 Task ID..."
                    className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] pl-8 pr-2.5 py-1 text-xs outline-none focus:border-[var(--brand-primary)]"
                  />
                </div>
                <select
                  aria-label="过滤题目结果"
                  value={statusFilter}
                  onChange={(e) => setStatusFilter(e.target.value as 'all' | 'passed' | 'failed' | 'fault')}
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2 py-1 text-xs cursor-pointer"
                >
                  <option value="all">全部状态</option>
                  <option value="passed">已通过 (Pass)</option>
                  <option value="failed">有效未过 (Valid 0)</option>
                  <option value="fault">含故障/未评分</option>
                </select>
              </div>

              {selectedTasks.length > 0 && user && ['researcher', 'admin'].includes(user.role) && (
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={sampleSetName}
                    onChange={(e) => setSampleSetName(e.target.value)}
                    placeholder="输入自定义样本集名称..."
                    className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1 text-xs w-44"
                  />
                  <button
                    type="button"
                    disabled={!sampleSetName.trim() || saveSampleSetMut.isPending}
                    onClick={() => saveSampleSetMut.mutate()}
                    className="inline-flex items-center gap-1 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] disabled:opacity-50 px-2.5 py-1 text-xs font-semibold text-white cursor-pointer"
                  >
                    <BookmarkPlus className="w-3.5 h-3.5" /> 保存 {selectedTasks.length} 题为样本集
                  </button>
                </div>
              )}
            </div>

            {tasksQ.isLoading ? (
              <TableSkeleton rows={8} cols={5} />
            ) : tasksQ.data?.redacted && tasks.length === 0 ? (
              <div className="p-5">
                <StateNoticeCard
                  kind="forbidden"
                  title="留出集逐题明细受保护"
                  description={`${tasksQ.data.redacted_reason || '缺少 dataset:holdout_read 权限'}（上方已保留聚合指标与 Pass@k 汇总，共 ${tasksQ.data.unique_tasks} 题 / ${tasksQ.data.total_attempts} 尝试）。`}
                />
              </div>
            ) : filteredTasks.length === 0 ? (
              <div className="p-6">
                <StateNoticeCard kind="empty" title="该批次当前筛选下无逐题记录" description="若为仅官方汇总型批次（如 VLM 汇总），请查看上方官方汇总结果。" />
              </div>
            ) : (
              <div>
                {tasksQ.data?.redacted && (
                  <div className="px-4 py-2.5 border-b border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] text-xs text-[var(--text-secondary)]">
                    {tasksQ.data.redacted_reason || '部分留出集题目已按权限隐藏，上方保留完整聚合指标。'}
                  </div>
                )}
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                      <th className="py-2 px-3 w-8">选</th>
                      <th className="py-2 px-3">Task ID</th>
                      <th className="py-2 px-3">多尝试结果胶囊（点击圆点入显微镜）</th>
                      <th className="py-2 px-3 text-right">通过 / 有效 / 记录</th>
                      {Object.keys(tasksQ.data?.pass_at_k_summary || {})
                        .filter((k) => k !== 'k1')
                        .map((k) => (
                          <th key={k} className="py-2 px-3 text-right font-mono">
                            pass@{k.replace(/^k/, '')}
                          </th>
                        ))}
                      <th className="py-2 px-3.5 text-right">valid_mean</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {filteredTasks.map((t) => {
                      const firstTrial = t.attempt_dots[0]?.trial_id;
                      const checked = selectedTasks.includes(t.task_id);
                      const multiKKeys = Object.keys(tasksQ.data?.pass_at_k_summary || {}).filter((k) => k !== 'k1');
                      return (
                        <tr
                          key={t.task_id}
                          onClick={() => firstTrial && openTrial(firstTrial)}
                          className="hover:bg-[var(--bg-elevated)]/65 cursor-pointer"
                        >
                          <td className="py-2 px-3" onClick={(e) => e.stopPropagation()}>
                            <input type="checkbox" checked={checked} onChange={() => toggleTask(t.task_id)} />
                          </td>
                          <td className="py-2 px-3 font-mono font-medium text-[var(--text-primary)]">{t.task_id}</td>
                          <td className="py-2 px-3">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <MultiAttemptPill dots={t.attempt_dots} onSelectTrial={openTrial} />
                              {(() => {
                                const errTypes = Array.from(
                                  new Set(
                                    t.attempt_dots
                                      .filter((d) => d.status !== 'scored' && d.status !== 'unscored')
                                      .map((d) => d.exception_type || d.failure_kind)
                                      .filter((x): x is string => Boolean(x))
                                  )
                                );
                                return errTypes.map((et) => (
                                  <span
                                    key={et}
                                    className="rounded px-1.5 py-0.5 text-[10px] font-mono bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]"
                                  >
                                    {et}
                                  </span>
                                ));
                              })()}
                            </div>
                          </td>
                          <td className="py-2 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                            {t.passed} / {t.scored} / {t.recorded}
                          </td>
                          {multiKKeys.map((k) => {
                            const field = `pass_at_${k.replace(/^k/, '')}`;
                            const val = t[field] as number | null | undefined;
                            return (
                              <td key={k} className="py-2 px-3 text-right font-mono tabular-nums text-[var(--text-secondary)]">
                                {val !== null && val !== undefined ? fmtPct(val * 100) : '—'}
                              </td>
                            );
                          })}
                          <td className="py-2 px-3.5 text-right font-bold tabular-nums">
                            {t.valid_mean !== null ? (
                              <span className={t.valid_mean >= 1 ? 'text-[var(--sem-pass-fg)]' : 'text-[var(--sem-zero-fg)]'}>
                                {fmtPct(t.valid_mean <= 1 ? t.valid_mean * 100 : t.valid_mean)}
                              </span>
                            ) : (
                              <span className="text-[var(--text-muted)]">—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              </div>
            )}
          </div>
        </>
      ) : null}
    </PageTransition>
  );
}
