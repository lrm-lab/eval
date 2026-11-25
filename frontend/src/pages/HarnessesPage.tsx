import React from 'react';
import { m, useReducedMotion } from 'motion/react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, BarChart3, Info, Plus, Wrench } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDuration, fmtPct, runScopeLabel, stableColorForKey } from '../lib/format';
import { useAppStore } from '../lib/store';
import type { BatchDTO } from '../types/api';
import { PageTransition, SpotlightCard } from '../components/motion/primitives';
import { CardGridSkeleton, StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

interface HarnessSummary {
  harness: string;
  runner: string;
  versions: string[];
  batch_count: number;
  total_recorded: number;
  total_scored: number;
  gateway_batches: number;
  tool_observed_trials: number;
  avg_tool_calls: number | null;
  recording_boundary: string;
}

interface HarnessProfileResp {
  harness: string;
  runners: string[];
  versions: string[];
  recording_boundary: string;
  tool_profile: {
    has_tool_events: boolean;
    total_trials: number;
    tool_observed_trials: number;
    avg_tool_calls: number | null;
    max_tool_calls: number | null;
    avg_duration_sec: number | null;
    reasoning_observed_trials: number;
  };
  score_matrix: Array<{
    model_canonical: string | null;
    benchmark_slug: string | null;
    dataset_release_id: string | null;
    display_name: string;
    run_scope: string;
    scoring_protocol: string;
    score: number | null;
    scored: number;
    recorded: number;
    batch_id: string | null;
  }>;
  batches: BatchDTO[];
}

export function HarnessesPage() {
  const { harnessName } = useParams<{ harnessName?: string }>();
  if (harnessName) {
    return <HarnessProfileView harnessName={decodeURIComponent(harnessName)} />;
  }
  return <HarnessDirectoryView />;
}

function HarnessDirectoryView() {
  const navigate = useNavigate();
  const q = useQuery({
    queryKey: ['harnesses-list'],
    queryFn: () => apiFetch<{ items: HarnessSummary[] }>('/api/v1/harnesses'),
  });

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div>
        <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
          <Wrench className="w-5 h-5 text-[var(--brand-primary)]" />
          <span>Agent Harness &amp; Runner 档案</span>
        </h1>
        <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
          独立刻画各代理框架的版本、日志记录边界（如 OpenClaw 不记录逐条工具调用）与跨基准表现
        </p>
      </div>

      {q.isLoading ? (
        <CardGridSkeleton count={6} />
      ) : q.isError ? (
        <StateNoticeCard kind="error" title="Harness 列表加载失败" description={(q.error as Error).message} onRetry={() => q.refetch()} />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {(q.data?.items || []).map((h) => (
            <SpotlightCard
              key={`${h.harness}-${h.runner}`}
              onClick={() => navigate(`/harnesses/${encodeURIComponent(h.harness)}`)}
              className="p-4"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="font-bold text-sm text-[var(--text-primary)]">{h.harness}</div>
                <span className="rounded px-2 py-0.5 text-[11px] font-mono bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  {h.runner}
                </span>
              </div>
              {h.versions.length > 0 && (
                <div className="mt-1.5 text-[11px] font-mono text-[var(--text-muted)]">
                  版本: {h.versions.join(', ')}
                </div>
              )}
              <p className="m-0 mt-2 text-xs text-[var(--text-secondary)] leading-relaxed">{h.recording_boundary}</p>
              <div className="mt-3 pt-2.5 border-t border-[var(--border-subtle)] grid grid-cols-3 gap-2 text-[11px] tabular-nums text-[var(--text-muted)]">
                <div>
                  批次数: <strong className="text-[var(--text-primary)]">{h.batch_count}</strong>
                </div>
                <div>
                  有效题: <strong className="text-[var(--text-primary)]">{h.total_scored}</strong>
                </div>
                <div>
                  均工具: <strong className="text-[var(--text-primary)]">{h.avg_tool_calls ?? '未记录'}</strong>
                </div>
              </div>
            </SpotlightCard>
          ))}
        </div>
      )}
    </PageTransition>
  );
}

function HarnessProfileView({ harnessName }: { harnessName: string }) {
  const navigate = useNavigate();
  const addToCompare = useAppStore((s) => s.addToCompare);

  const q = useQuery({
    queryKey: ['harness-profile', harnessName],
    queryFn: () => apiFetch<HarnessProfileResp>(`/api/v1/harnesses/${encodeURIComponent(harnessName)}`),
  });

  const data = q.data;

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex items-center gap-3">
        <Link
          to="/harnesses"
          className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] no-underline"
        >
          <ArrowLeft className="w-3.5 h-3.5" /> 返回 Harness 目录
        </Link>
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)]">{harnessName}</h1>
          {data && (
            <div className="mt-0.5 text-xs text-[var(--text-secondary)]">
              Runners: {data.runners.join(', ')} · 版本: {data.versions.join(', ') || '未标明'}
            </div>
          )}
        </div>
      </div>

      {q.isLoading ? (
        <TableSkeleton rows={6} cols={4} />
      ) : q.isError ? (
        <StateNoticeCard kind="error" title="Harness 档案加载失败" description={(q.error as Error).message} onRetry={() => q.refetch()} />
      ) : data ? (
        <>
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 flex items-start gap-3">
            <Info className="w-4 h-4 text-[var(--brand-primary)] shrink-0 mt-0.5" />
            <div className="text-xs leading-relaxed">
              <div className="font-semibold text-[var(--text-primary)] mb-1">日志与轨迹记录边界声明</div>
              <div className="text-[var(--text-secondary)]">{data.recording_boundary}</div>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3.5 text-xs">
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-[var(--text-muted)]">记录总 Trials</div>
              <div className="mt-1 text-xl font-bold tabular-nums">{data.tool_profile.total_trials}</div>
            </div>
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-[var(--text-muted)]">工具调用画像</div>
              <div className="mt-1 text-xl font-bold tabular-nums">
                {data.tool_profile.has_tool_events ? `${data.tool_profile.avg_tool_calls} avg` : '未记录事件'}
              </div>
              <div className="text-[11px] text-[var(--text-muted)]">
                {data.tool_profile.has_tool_events
                  ? `最大 ${data.tool_profile.max_tool_calls} · 观测 ${data.tool_profile.tool_observed_trials} trials`
                  : '该框架不输出逐条 tool_call'}
              </div>
            </div>
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-[var(--text-muted)]">平均单题总耗时</div>
              <div className="mt-1 text-xl font-bold tabular-nums">{fmtDuration(data.tool_profile.avg_duration_sec)}</div>
            </div>
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-[var(--text-muted)]">含 Reasoning Token 观测</div>
              <div className="mt-1 text-xl font-bold tabular-nums">{data.tool_profile.reasoning_observed_trials}</div>
            </div>
          </div>

          {data.score_matrix.length > 0 && (
            <div className="grid gap-3.5 lg:grid-cols-2">
              <HarnessMetricChart rows={data.score_matrix} metric="coverage" title="有效评分覆盖率" />
              <HarnessMetricChart rows={data.score_matrix} metric="score" title="有效试验均分" />
            </div>
          )}

          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
            <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-xs font-bold text-[var(--text-primary)]">
              跨基准与模型去重得分矩阵 ({data.score_matrix.length})
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse text-xs">
                <thead>
                  <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                    <th className="py-2 px-3.5">基准</th>
                    <th className="py-2 px-3">模型</th>
                    <th className="py-2 px-3">协议 / 范围</th>
                    <th className="py-2 px-3 text-right">覆盖</th>
                    <th className="py-2 px-3.5 text-right">得分</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-subtle)]">
                  {data.score_matrix.map((row, i) => (
                    <tr
                      key={`${row.dataset_release_id}-${row.model_canonical}-${i}`}
                      onClick={() => row.batch_id && navigate(`/eval/batches/${row.batch_id}`)}
                      className="hover:bg-[var(--bg-elevated)]/70 cursor-pointer"
                    >
                      <td className="py-2 px-3.5 font-medium text-[var(--text-primary)]">{row.display_name}</td>
                      <td className="py-2 px-3 text-[var(--text-secondary)]">{row.model_canonical || '—'}</td>
                      <td className="py-2 px-3 font-mono text-[11px] text-[var(--text-muted)]">
                        {row.scoring_protocol} · {runScopeLabel(row.run_scope)}
                      </td>
                      <td className="py-2 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                        {row.scored}/{row.recorded}
                      </td>
                      <td className="py-2 px-3.5 text-right font-bold tabular-nums text-[var(--text-primary)]">
                        {fmtPct(row.score)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
            <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-xs font-bold text-[var(--text-primary)]">
              全部关联批次 ({data.batches.length})
            </div>
            <div className="divide-y divide-[var(--border-subtle)]">
              {data.batches.map((b) => (
                <div key={b.batch_id} className="px-4 py-2.5 flex items-center justify-between gap-3 text-xs hover:bg-[var(--bg-elevated)]/50">
                  <div className="min-w-0">
                    <Link to={`/eval/batches/${b.batch_id}`} className="font-semibold text-[var(--brand-primary)] no-underline hover:underline">
                      {b.batch_name}
                    </Link>
                    <div className="text-[11px] text-[var(--text-muted)]">
                      {b.model_canonical || '—'} · {b.benchmark_slug || b.raw_dataset_name} · {b.scored}/{b.planned || b.recorded}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => addToCompare({ id: b.batch_id, kind: 'batch', batch_id: b.batch_id, alias: b.batch_name })}
                    className="inline-flex items-center gap-1 rounded border border-[var(--border-subtle)] px-2 py-1 text-[11px] text-[var(--text-secondary)] hover:text-[var(--brand-primary)] cursor-pointer"
                  >
                    <Plus className="w-3 h-3" /> 对比
                  </button>
                </div>
              ))}
            </div>
          </div>
        </>
      ) : null}
    </PageTransition>
  );
}

function HarnessMetricChart({
  rows, metric, title,
}: {
  rows: HarnessProfileResp['score_matrix'];
  metric: 'coverage' | 'score';
  title: string;
}) {
  const reduceInApp = useAppStore((state) => state.reducedMotion);
  const reduceInSystem = useReducedMotion();
  const reduced = reduceInApp || reduceInSystem;
  return (
    <section className="min-w-0 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
      <h2 className="m-0 flex items-center gap-2 text-sm font-semibold">
        <BarChart3 className="h-4 w-4 text-[var(--brand-primary)]" aria-hidden="true" />
        {title}
      </h2>
      <p className="mb-3 mt-1 text-xs text-[var(--text-secondary)]">
        {metric === 'coverage' ? '有效评分数 ÷ 已记录试验数' : '按基准、模型、评分协议和评测范围展示'}
      </p>
      <ul className="m-0 grid max-h-96 list-none gap-1 overflow-y-auto p-0">
        {rows.map((row, index) => {
          const value = metric === 'score' ? row.score : row.recorded > 0 ? row.scored / row.recorded * 100 : null;
          const width = Math.max(0, Math.min(100, value ?? 0));
          const color = stableColorForKey(row.benchmark_slug || row.display_name);
          const scope = runScopeLabel(row.run_scope);
          const detail = [row.model_canonical, scope, row.scoring_protocol, `有效评分 ${row.scored}/${row.recorded}`].filter(Boolean).join(' · ');
          const label = value === null ? (metric === 'score' ? '未评分' : '无记录') : fmtPct(value, 1);
          return (
            <li key={`${row.dataset_release_id}-${row.model_canonical}-${row.scoring_protocol}-${index}`} className="min-w-0">
              <div className="rounded-lg p-2 motion-safe:transition-colors hover:bg-[var(--bg-elevated)]" title={detail}>
                <div className="flex items-baseline justify-between gap-2 text-xs">
                  {row.batch_id ? (
                    <Link className="truncate font-medium text-[var(--text-primary)] hover:text-[var(--brand-primary)]" to={`/eval/batches/${row.batch_id}`}>{row.display_name}</Link>
                  ) : <span className="truncate font-medium">{row.display_name}</span>}
                  <span className="shrink-0 tabular-nums text-[var(--text-secondary)]">{label}</span>
                </div>
                <div aria-hidden="true" className="my-1.5 h-2 overflow-hidden rounded-full bg-[var(--bg-elevated)]">
                  <m.span
                    className="block h-full rounded-full"
                    initial={reduced ? false : { width: 0 }}
                    animate={{ width: `${width}%` }}
                    transition={{ duration: reduced ? 0 : 0.36, delay: reduced ? 0 : Math.min(index, 7) * 0.022 }}
                    style={{ background: `linear-gradient(90deg, color-mix(in srgb, ${color} 56%, transparent), ${color})` }}
                  />
                </div>
                <div className="truncate text-[11px] text-[var(--text-muted)]">{detail}</div>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
