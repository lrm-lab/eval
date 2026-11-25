import React, { useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import type * as echarts from 'echarts';
import { ArrowLeft, Cpu, GitCompareArrows, Layers, Plus, Search, ShieldCheck } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDateShort, fmtPct, runScopeLabel, stableColorForKey, trackDisplayName } from '../lib/format';
import { useAppStore } from '../lib/store';
import type { BatchDTO } from '../types/api';
import { PageTransition, SlidingTabs, SpotlightCard } from '../components/motion/primitives';
import { RowActionIcon } from '../components/ui/RowActionIcon';
import { DatasetPerformancePanel, type DatasetPerformanceRow } from '../components/ui/DatasetPerformancePanel';
import { CardGridSkeleton, StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';
import { EChartBox, glassTooltipConfig } from '../components/charts/EChartBox';

interface ModelSummaryItem {
  model_canonical: string;
  model_routes: string[];
  checkpoints: string[];
  thinking_modes: string[];
  harnesses: string[];
  runners: string[];
  benchmark_count: number;
  batch_count: number;
  legacy_bench_count: number;
}

interface ModelProfileScore {
  benchmark_slug: string | null;
  dataset_release_id: string | null;
  display_name: string;
  track_slug: string;
  track_display_name: string;
  goal_type: string;
  capability: string;
  harness: string | null;
  harness_version: string | null;
  runner: string;
  run_scope: string;
  scoring_protocol: string;
  checkpoint: string | null;
  thinking_mode: string | null;
  score: number | null;
  scored: number | null;
  recorded: number | null;
  batch_id: string | null;
  source: string;
}

interface ModelProfileResp {
  dataset_performance?: DatasetPerformanceRow[];
  header: {
    model_canonical: string;
    model_routes: string[];
    checkpoints: string[];
    harnesses: string[];
    runners: string[];
    thinking_modes: string[];
    batch_count: number;
    legacy_score_count: number;
  };
  verdict_suggestion: {
    kind: string;
    baseline_col: string;
    candidate_col: string;
    label: string;
  } | null;
  track_scores: Record<string, ModelProfileScore[]>;
  harness_matrix: ModelProfileScore[];
  checkpoint_trends: Array<{
    dataset_release_id: string;
    scoring_protocol: string;
    harness: string;
    points: Array<{ checkpoint: string; score: number; scored?: number; batch_id?: string | null }>;
  }>;
  batches: BatchDTO[];
}

export function ModelsPage() {
  const { modelCanonical } = useParams<{ modelCanonical?: string }>();
  if (modelCanonical) {
    return <ModelProfileView modelCanonical={decodeURIComponent(modelCanonical)} />;
  }
  return <ModelDirectoryView />;
}

function ModelDirectoryView() {
  const navigate = useNavigate();
  const [q, setQ] = useState('');

  const listQ = useQuery({
    queryKey: ['models-list'],
    queryFn: () => apiFetch<{ items: ModelSummaryItem[]; total: number }>('/api/v1/models'),
  });

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    const list = listQ.data?.items || [];
    if (!kw) return list;
    return list.filter(
      (m) =>
        m.model_canonical.toLowerCase().includes(kw) ||
        m.harnesses.some((h) => h.toLowerCase().includes(kw)) ||
        m.checkpoints.some((c) => c.toLowerCase().includes(kw))
    );
  }, [listQ.data, q]);

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <Cpu className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>模型档案目录（Model Profiles）</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            聚合真实 Harbor / Direct / VLMEvalKit 批次及 2026-11-20 历史基线，绝不伪造商业定价或未实测参数
          </p>
        </div>

        <div className="relative w-72">
          <Search className="w-3.5 h-3.5 text-[var(--text-muted)] absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="过滤模型名 / Harness / Checkpoint..."
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] pl-8 pr-3 py-1.5 text-xs text-[var(--text-primary)] outline-none focus:border-[var(--brand-primary)]"
          />
        </div>
      </div>

      {listQ.isLoading ? (
        <CardGridSkeleton count={8} />
      ) : listQ.isError ? (
        <StateNoticeCard kind="error" title="模型列表加载失败" description={(listQ.error as Error).message} onRetry={() => listQ.refetch()} />
      ) : filtered.length === 0 ? (
        <StateNoticeCard kind="empty" title="未找到匹配的模型档案" />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {filtered.map((m) => (
            <SpotlightCard
              key={m.model_canonical}
              onClick={() => navigate(`/models/${encodeURIComponent(m.model_canonical)}`)}
              className="p-4"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="font-bold text-sm text-[var(--text-primary)] break-all">{m.model_canonical}</div>
                <span className="rounded-full px-2 py-0.5 text-[11px] font-mono tabular-nums bg-[var(--brand-tint)] text-[var(--brand-primary)] shrink-0">
                  {m.benchmark_count} bench
                </span>
              </div>
              <div className="mt-2.5 flex flex-wrap gap-1.5 text-[11px]">
                {m.harnesses.map((h) => (
                  <span key={h} className="rounded px-1.5 py-0.5 bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-secondary)]">
                    {h}
                  </span>
                ))}
                {m.checkpoints.map((ck) => (
                  <span key={ck} className="rounded px-1.5 py-0.5 bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)] font-mono">
                    {ck}
                  </span>
                ))}
                {m.thinking_modes.map((tm) => (
                  <span key={tm} className="rounded px-1.5 py-0.5 bg-[var(--sem-legacy-bg)] text-[var(--sem-legacy-fg)]">
                    {tm}
                  </span>
                ))}
              </div>
              <div className="mt-3 pt-2.5 border-t border-[var(--border-subtle)] flex items-center justify-between text-[11px] text-[var(--text-muted)] tabular-nums">
                <span>评测批次: {m.batch_count}</span>
                <span>2026-11-20 历史项: {m.legacy_bench_count}</span>
              </div>
            </SpotlightCard>
          ))}
        </div>
      )}
    </PageTransition>
  );
}

function ModelProfileView({ modelCanonical }: { modelCanonical: string }) {
  const navigate = useNavigate();
  const theme = useAppStore((s) => s.theme);
  const addToCompare = useAppStore((s) => s.addToCompare);
  const [params, setParams] = useSearchParams();
  const requestedTab = params.get('tab');
  const tab = requestedTab && ['harness', 'dataset-performance', 'checkpoints', 'batches'].includes(requestedTab) ? requestedTab : 'scorecard';
  const setTab = (next: string) => setParams((previous) => {
    const search = new URLSearchParams(previous);
    if (next === 'scorecard') search.delete('tab');
    else search.set('tab', next);
    return search;
  });

  const profQ = useQuery({
    queryKey: ['model-profile', modelCanonical],
    queryFn: () => apiFetch<ModelProfileResp>(`/api/v1/models/${encodeURIComponent(modelCanonical)}`),
  });

  const data = profQ.data;

  const radarOption = useMemo<echarts.EChartsCoreOption>(() => {
    if (!data) return {};
    const textColor = theme === 'dark' ? '#a1a1aa' : '#475569';
    const entries = Object.entries(data.track_scores)
      .map(([ts, list]) => {
        const vals = list.map((x) => x.score).filter((v): v is number => v !== null);
        const avg = vals.length > 0 ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
        return { name: trackDisplayName(ts), avg: Number(avg.toFixed(2)), count: vals.length };
      })
      .filter((x) => x.count > 0);
    if (entries.length < 2) return {};
    return {
      tooltip: glassTooltipConfig(theme),
      radar: {
        indicator: entries.map((e) => ({ name: `${e.name} (${e.count})`, max: 100 })),
        axisName: { color: textColor, fontSize: 11 },
      },
      series: [
        {
          type: 'radar',
          data: [
            {
              value: entries.map((e) => e.avg),
              name: modelCanonical,
              areaStyle: { color: 'rgba(37, 99, 235, 0.22)' },
              lineStyle: { color: '#2563EB', width: 2 },
              itemStyle: { color: '#2563EB' },
            },
          ],
        },
      ],
    };
  }, [data, theme, modelCanonical]);

  const trendOption = useMemo<echarts.EChartsCoreOption>(() => {
    if (!data || data.checkpoint_trends.length === 0) return {};
    const textColor = theme === 'dark' ? '#a1a1aa' : '#475569';
    const gridColor = theme === 'dark' ? '#27272a' : '#e5e7eb';
    const ckSet = new Set<string>();
    data.checkpoint_trends.forEach((s) => s.points.forEach((p) => ckSet.add(p.checkpoint)));
    const xCats = Array.from(ckSet).sort((a, b) => (a === 'base' ? -1 : b === 'base' ? 1 : a.localeCompare(b)));

    return {
      tooltip: { ...glassTooltipConfig(theme), trigger: 'axis' },
      legend: { type: 'scroll', bottom: 4, textStyle: { color: textColor, fontSize: 11 } },
      grid: { top: 24, right: 24, bottom: 56, left: 48 },
      xAxis: { type: 'category', data: xCats, axisLabel: { color: textColor, fontSize: 11 } },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { color: textColor, fontSize: 11, formatter: '{value}%' },
        splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
      },
      series: data.checkpoint_trends.slice(0, 12).map((s) => {
        const ptMap = new Map(s.points.map((p) => [p.checkpoint, p.score]));
        const name = `${s.dataset_release_id} (${s.harness})`;
        return {
          name,
          type: 'line',
          connectNulls: false,
          itemStyle: { color: stableColorForKey(name) },
          data: xCats.map((ck) => ptMap.get(ck) ?? null),
        };
      }),
    };
  }, [data, theme]);

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link
            to="/models"
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] no-underline"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> 返回模型目录
          </Link>
          <div>
            <h1 className="m-0 text-xl font-bold text-[var(--text-primary)]">{modelCanonical}</h1>
            {data && (
              <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-[var(--text-secondary)]">
                <span>Harnesses: {data.header.harnesses.join(', ') || '—'}</span>
                <span>·</span>
                <span>Checkpoints: {data.header.checkpoints.join(', ') || 'base'}</span>
                <span>·</span>
                <span>Runners: {data.header.runners.join(', ')}</span>
              </div>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {data?.verdict_suggestion && (
            <button
              type="button"
              onClick={() =>
                navigate(
                  `/eval/compare?kind=legacy_model&baseline=${encodeURIComponent(
                    data.verdict_suggestion!.baseline_col
                  )}&candidate=${encodeURIComponent(data.verdict_suggestion!.candidate_col)}`
                )
              }
              className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-3 py-1.5 text-xs font-semibold text-white cursor-pointer"
            >
              <ShieldCheck className="w-3.5 h-3.5" />
              <span>一键 Gain vs Guardrail 对比 ({data.verdict_suggestion.label})</span>
            </button>
          )}
        </div>
      </div>

      <SlidingTabs
        layoutId="model-profile-tabs"
        className="max-w-full flex-wrap"
        size="sm"
        value={tab}
        onChange={setTab}
        options={[
          { id: 'scorecard', label: '三线成绩单 & 雷达' },
          { id: 'dataset-performance', label: '跨数据集表现', badge: data?.dataset_performance?.length },
          { id: 'harness', label: '跨 Harness 矩阵', badge: data?.harness_matrix.length },
          { id: 'checkpoints', label: '已验证 Checkpoint 趋势', badge: data?.checkpoint_trends.length },
          { id: 'batches', label: '全部批次', badge: data?.batches.length },
        ]}
      />

      {profQ.isLoading ? (
        <TableSkeleton rows={8} cols={5} />
      ) : profQ.isError ? (
        <StateNoticeCard kind="error" title="模型档案加载失败" description={(profQ.error as Error).message} onRetry={() => profQ.refetch()} />
      ) : !data ? null : tab === 'scorecard' ? (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          {Object.keys(radarOption).length > 0 && (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <h2 className="m-0 text-sm font-bold text-[var(--text-primary)] mb-2">三线能力均值雷达</h2>
              <EChartBox option={radarOption} height={320} exportName={`model-radar-${modelCanonical}`} />
            </div>
          )}

          <div className={`${Object.keys(radarOption).length > 0 ? 'lg:col-span-2' : 'lg:col-span-3'} space-y-4`}>
            {Object.entries(data.track_scores).map(([trackSlug, list]) => (
              <div key={trackSlug} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
                <div className="px-4 py-2.5 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-xs font-bold text-[var(--text-primary)]">
                  {trackDisplayName(trackSlug)} ({list.length})
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left border-collapse text-xs">
                    <thead>
                      <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                        <th className="py-2 px-3.5">基准</th>
                        <th className="py-2 px-3">Harness / Checkpoint</th>
                        <th className="py-2 px-3">协议 / 范围</th>
                        <th className="py-2 px-3.5 text-right">得分 (0-100)</th>
                        <th className="w-12 py-2 px-2 text-center">操作</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--border-subtle)]">
                      {list.map((s, idx) => {
                        const target = s.batch_id
                          ? `/eval/batches/${s.batch_id}`
                          : s.benchmark_slug && `/benchmarks/${s.benchmark_slug}`;
                        return (
                          <tr
                            key={`${s.dataset_release_id}-${s.scoring_protocol}-${s.checkpoint}-${idx}`}
                            onClick={() => target && navigate(target)}
                            className={`group/row-action hover:bg-[var(--bg-elevated)]/70 ${target ? 'cursor-pointer' : ''}`}
                          >
                            <td className="py-2 px-3.5 font-medium text-[var(--text-primary)]">{s.display_name}</td>
                            <td className="py-2 px-3 text-[var(--text-secondary)]">
                              {s.harness || s.runner} {s.checkpoint ? `· ${s.checkpoint}` : ''}
                            </td>
                            <td className="py-2 px-3 text-[11px] font-mono text-[var(--text-muted)]">
                              {s.scoring_protocol} · {runScopeLabel(s.run_scope)}
                            </td>
                            <td className="py-2 px-3.5 text-right font-bold tabular-nums text-[var(--text-primary)]">
                              {fmtPct(s.score)}
                            </td>
                            <td className="px-2 text-center" onClick={(event) => event.stopPropagation()}>
                              {target && (
                                <RowActionIcon
                                  variant="details"
                                  label={`${s.batch_id ? '查看批次' : '查看基准'}详情：${s.display_name}`}
                                  to={target}
                                />
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : tab === 'dataset-performance' ? (
        <DatasetPerformancePanel axis="model" rows={data.dataset_performance} />
      ) : tab === 'harness' ? (
        data.harness_matrix.length === 0 ? (
          <StateNoticeCard kind="empty" title="该模型暂无可按 Harness 拆分的原生去重成绩" />
        ) : (
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  <th className="py-2.5 px-3.5">基准</th>
                  <th className="py-2.5 px-3">Harness</th>
                  <th className="py-2.5 px-3">Runner / 范围</th>
                  <th className="py-2.5 px-3">协议</th>
                  <th className="py-2.5 px-3 text-right">覆盖</th>
                  <th className="py-2.5 px-3.5 text-right">得分</th>
                  <th className="w-12 py-2.5 px-2 text-center">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {data.harness_matrix.map((row, idx) => (
                  <tr
                    key={`${row.dataset_release_id}-${row.harness}-${idx}`}
                    onClick={() => row.batch_id && navigate(`/eval/batches/${row.batch_id}`)}
                    className={`group/row-action hover:bg-[var(--bg-elevated)]/70 ${row.batch_id ? 'cursor-pointer' : ''}`}
                  >
                    <td className="py-2 px-3.5 font-semibold text-[var(--text-primary)]">{row.display_name}</td>
                    <td className="py-2 px-3">
                      <div className="flex items-center gap-2">
                        <span>
                          {row.harness || row.runner}
                          {row.harness_version ? `@${row.harness_version}` : ''}
                        </span>
                        <span onClick={(event) => event.stopPropagation()}>
                          <RowActionIcon
                            variant="open"
                            label={`打开 Harness 档案：${row.harness || row.runner}`}
                            to={`/harnesses/${encodeURIComponent(row.harness || row.runner)}`}
                          />
                        </span>
                      </div>
                    </td>
                    <td className="py-2 px-3 text-[var(--text-secondary)]">
                      {row.runner} · {runScopeLabel(row.run_scope)}
                    </td>
                    <td className="py-2 px-3 font-mono text-[11px] text-[var(--text-muted)]">{row.scoring_protocol}</td>
                    <td className="py-2 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                      {row.scored ?? '—'}/{row.recorded ?? '—'}
                    </td>
                    <td className="py-2 px-3.5 text-right font-bold tabular-nums text-[var(--text-primary)]">{fmtPct(row.score)}</td>
                    <td className="px-2 text-center" onClick={(event) => event.stopPropagation()}>
                      {row.batch_id && (
                        <RowActionIcon
                          variant="details"
                          label={`查看批次详情：${row.display_name}`}
                          to={`/eval/batches/${row.batch_id}`}
                        />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : tab === 'checkpoints' ? (
        data.checkpoint_trends.length === 0 ? (
          <StateNoticeCard
            kind="empty"
            title="暂无可绘制的同基准同协议多 Checkpoint 趋势"
            description="系统仅在相同 dataset_release_id 与 scoring_protocol 下存在 ≥ 2 个明确 checkpoint 时绘制演进折线，拒绝跨口径串联。"
          />
        ) : (
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
            <div className="text-xs text-[var(--text-secondary)] mb-2">
              严格按相同（数据集版本 × 评分协议 × Harness）绘制已核验 Checkpoint 得分演变（共 {data.checkpoint_trends.length} 条曲线）。
            </div>
            <EChartBox option={trendOption} height={400} exportName={`model-checkpoints-${modelCanonical}`} />
          </div>
        )
      ) : (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                <th className="py-2.5 px-3.5">批次</th>
                <th className="py-2.5 px-3">基准 / 协议</th>
                <th className="py-2.5 px-3">Harness / 范围</th>
                <th className="py-2.5 px-3 text-right">覆盖</th>
                <th className="py-2.5 px-3 text-right">有效均值</th>
                <th className="py-2.5 px-3.5 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {data.batches.map((b) => (
                <tr key={b.batch_id} className="group/row-action hover:bg-[var(--bg-elevated)]/65">
                  <td className="py-2.5 px-3.5">
                    <div className="font-semibold text-[var(--text-primary)]">{b.batch_name}</div>
                    <div className="text-[11px] text-[var(--text-muted)]">{fmtDateShort(b.finished_at || b.indexed_at)}</div>
                  </td>
                  <td className="py-2.5 px-3">
                    <div className="text-[var(--text-primary)]">{b.benchmark_slug || b.raw_dataset_name}</div>
                    <div className="text-[11px] font-mono text-[var(--text-muted)]">{b.scoring_protocol}</div>
                  </td>
                  <td className="py-2.5 px-3 text-[var(--text-secondary)]">
                    {b.harness || b.runner} · {runScopeLabel(b.run_scope)}
                  </td>
                  <td className="py-2.5 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                    {b.scored}/{b.planned || b.recorded}
                  </td>
                  <td className="py-2.5 px-3 text-right font-bold tabular-nums text-[var(--text-primary)]">
                    {b.valid_mean !== null && b.valid_mean !== undefined
                      ? fmtPct(b.valid_mean <= 1 ? b.valid_mean * 100 : b.valid_mean)
                      : '—'}
                  </td>
                  <td className="py-2.5 px-3.5 text-right whitespace-nowrap">
                    <RowActionIcon
                      variant="details"
                      label={`查看批次详情：${b.batch_name}`}
                      to={`/eval/batches/${b.batch_id}`}
                    />
                    <button
                      type="button"
                      onClick={() =>
                        addToCompare({ id: b.batch_id, kind: 'batch', batch_id: b.batch_id, alias: b.batch_name })
                      }
                      className="ml-2 inline-flex items-center gap-1 rounded border border-[var(--border-subtle)] px-2 py-1 text-[11px] text-[var(--text-secondary)] hover:text-[var(--brand-primary)] cursor-pointer"
                    >
                      <Plus className="w-3 h-3" /> 对比
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </PageTransition>
  );
}
