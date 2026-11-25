import React, { useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import type * as echarts from 'echarts';
import { ArrowLeft, BarChart3, Crosshair, Flame, GitCompareArrows, Layers3, ListChecks, Search } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDuration, fmtPct, runScopeLabel, trackDisplayName } from '../lib/format';
import { useAppStore } from '../lib/store';
import type { BatchDTO, DatasetReleaseDTO } from '../types/api';
import { PageTransition, SlidingTabs, SpotlightCard } from '../components/motion/primitives';
import { RowActionIcon } from '../components/ui/RowActionIcon';
import { PublicBaselinePanel } from '../components/ui/PublicBaselinePanel';
import { GroupedComparePanel, type GroupedComparison } from '../components/ui/GroupedComparePanel';
import { CardGridSkeleton, StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';
import { EChartBox, glassTooltipConfig } from '../components/charts/EChartBox';
import { EfficiencyScatterChart } from '../components/charts/EfficiencyScatterChart';

interface BenchmarkListItem {
  benchmark_slug: string;
  display_name: string;
  track_slug: string;
  track_display_name: string;
  goal_type: string;
  capability: string;
  modality: string;
  language: string;
  status: string;
  core_14: boolean;
  batch_count: number;
  recorded_trials: number;
  releases: DatasetReleaseDTO[];
}

interface BenchmarkProfileResp {
  grouped_comparison?: GroupedComparison;
  benchmark: {
    benchmark_slug: string;
    display_name: string;
    track_slug: string;
    track_display_name: string;
    goal_type: string;
    capability: string;
    modality: string;
    language: string;
    status: string;
    metadata: Record<string, unknown>;
  };
  releases: DatasetReleaseDTO[];
  scatter_configs: Array<{ dataset_release_id: string; scoring_protocol: string }>;
  mini_leaderboard: Array<{
    model_canonical: string | null;
    harness: string | null;
    harness_version: string | null;
    runner: string;
    run_scope: string;
    dataset_release_id: string | null;
    scoring_protocol: string;
    score: number | null;
    scored: number | null;
    recorded: number | null;
    batch_id: string | null;
  }>;
  task_summary: {
    indexed_tasks: number;
    by_category: Array<{ category: string; count: number }>;
    by_difficulty: Array<{ difficulty: string; count: number }>;
  };
  batches: BatchDTO[];
}

interface HardestTaskItem {
  task_id: string;
  total_attempts: number;
  valid_attempts: number;
  passed_attempts: number;
  fault_attempts: number;
  pass_rate_pct: number | null;
  mean_reward: number | null;
  avg_duration_sec: number | null;
  sample_batch_id: string;
  sample_trial_id: string;
}

export function BenchmarksPage() {
  const { benchmarkSlug } = useParams<{ benchmarkSlug?: string }>();
  if (benchmarkSlug) {
    return <BenchmarkProfileView benchmarkSlug={benchmarkSlug} />;
  }
  return <BenchmarkDirectoryView />;
}

function BenchmarkDirectoryView() {
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const [trackFilter, setTrackFilter] = useState('all');

  const listQ = useQuery({
    queryKey: ['benchmarks-list'],
    queryFn: () => apiFetch<{ items: BenchmarkListItem[] }>('/api/v1/benchmarks'),
  });

  const tracks = useMemo(() => {
    const s = new Set<string>();
    (listQ.data?.items || []).forEach((b) => s.add(b.track_slug));
    return ['all', ...Array.from(s)];
  }, [listQ.data]);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return (listQ.data?.items || []).filter((b) => {
      if (trackFilter !== 'all' && b.track_slug !== trackFilter) return false;
      if (!kw) return true;
      return (
        b.benchmark_slug.toLowerCase().includes(kw) ||
        b.display_name.toLowerCase().includes(kw) ||
        b.capability.toLowerCase().includes(kw)
      );
    });
  }, [listQ.data, q, trackFilter]);

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>Benchmark 基准档案</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            覆盖三线 14 项核心基准与扩展基准，展示分榜、效率前沿、最难题目榜与发行版属性
          </p>
        </div>

        <div className="relative w-64">
          <Search className="w-3.5 h-3.5 text-[var(--text-muted)] absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索基准名 / 能力维度..."
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] pl-8 pr-3 py-1.5 text-xs text-[var(--text-primary)] outline-none focus:border-[var(--brand-primary)]"
          />
        </div>
      </div>

      <SlidingTabs
        layoutId="bm-dir-tracks"
        className="max-w-full flex-wrap [&_[role=tab]]:min-h-8"
        value={trackFilter}
        onChange={setTrackFilter}
        options={tracks.map((track) => ({
          id: track,
          label: track === 'all' ? '全部轨道' : trackDisplayName(track),
          badge: (listQ.data?.items || []).filter((benchmark) => track === 'all' || benchmark.track_slug === track).length,
        }))}
      />

      {listQ.isLoading ? (
        <CardGridSkeleton count={8} />
      ) : listQ.isError ? (
        <StateNoticeCard kind="error" title="基准列表加载失败" description={(listQ.error as Error).message} onRetry={() => listQ.refetch()} />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {filtered.map((b) => (
            <SpotlightCard key={b.benchmark_slug} onClick={() => navigate(`/benchmarks/${b.benchmark_slug}`)} className="p-4">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="font-bold text-sm text-[var(--text-primary)]">{b.display_name}</div>
                  <div className="text-[11px] text-[var(--text-muted)]">{b.capability}</div>
                </div>
                {b.core_14 && (
                  <span className="rounded-full px-2 py-0.5 text-[10px] font-semibold bg-[var(--brand-tint)] text-[var(--brand-primary)] shrink-0">
                    Core-14
                  </span>
                )}
              </div>
              <div className="mt-2.5 flex flex-wrap gap-1.5 text-[11px]">
                <span className="rounded px-1.5 py-0.5 bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  {trackDisplayName(b.track_slug)}
                </span>
                <span className="rounded px-1.5 py-0.5 bg-[var(--bg-elevated)] text-[var(--text-muted)]">
                  {b.modality} · {b.language}
                </span>
                <span
                  className={`rounded px-1.5 py-0.5 ${
                    b.status === 'integrated'
                      ? 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                      : 'bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)]'
                  }`}
                >
                  {b.status}
                </span>
              </div>
              <div className="mt-3 pt-2 border-t border-[var(--border-subtle)] flex items-center justify-between text-[11px] text-[var(--text-muted)] tabular-nums">
                <span>发行版: {b.releases.length}</span>
                <span>批次: {b.batch_count} · Trials: {b.recorded_trials}</span>
              </div>
            </SpotlightCard>
          ))}
        </div>
      )}
    </PageTransition>
  );
}

function BenchmarkProfileView({ benchmarkSlug }: { benchmarkSlug: string }) {
  const navigate = useNavigate();
  const theme = useAppStore((s) => s.theme);
  const [params, setParams] = useSearchParams();
  const requestedTab = params.get('tab');
  const tab = requestedTab && ['groups', 'scatter', 'hardest', 'tasks', 'properties'].includes(requestedTab)
    ? requestedTab : 'leaderboard';
  const setTab = (next: string) => setParams((previous) => {
    const search = new URLSearchParams(previous);
    if (next === 'leaderboard') search.delete('tab');
    else search.set('tab', next);
    return search;
  });

  const profQ = useQuery({
    queryKey: ['benchmark-profile', benchmarkSlug],
    queryFn: () => apiFetch<BenchmarkProfileResp>(`/api/v1/benchmarks/${encodeURIComponent(benchmarkSlug)}`),
  });

  const hardestQ = useQuery({
    queryKey: ['benchmark-hardest', benchmarkSlug],
    enabled: tab === 'hardest',
    queryFn: () => apiFetch<{ items: HardestTaskItem[] }>(`/api/v1/benchmarks/${encodeURIComponent(benchmarkSlug)}/hardest-tasks`),
  });

  const data = profQ.data;

  const domainChartOpt = useMemo<echarts.EChartsCoreOption>(() => {
    const cats = data?.task_summary.by_category || [];
    if (cats.length === 0) return {};
    const textColor = theme === 'dark' ? '#a1a1aa' : '#475569';
    return {
      tooltip: { ...glassTooltipConfig(theme), trigger: 'axis' },
      grid: { top: 16, right: 20, bottom: 24, left: 130 },
      xAxis: { type: 'value', axisLabel: { color: textColor, fontSize: 11 } },
      yAxis: { type: 'category', data: cats.map((c) => c.category), axisLabel: { color: textColor, fontSize: 11 } },
      series: [{ type: 'bar', data: cats.map((c) => c.count), itemStyle: { color: '#2563EB', borderRadius: [0, 4, 4, 0] } }],
    };
  }, [data, theme]);

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link
            to="/benchmarks"
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] no-underline"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> 返回基准目录
          </Link>
          <div>
            <h1 className="m-0 text-xl font-bold text-[var(--text-primary)]">
              {data?.benchmark.display_name || benchmarkSlug}
            </h1>
            {data && (
              <div className="mt-0.5 text-xs text-[var(--text-secondary)]">
                {data.benchmark.capability} · {trackDisplayName(data.benchmark.track_slug)} · {data.benchmark.modality} · {data.benchmark.language}
              </div>
            )}
          </div>
        </div>
      </div>

      <SlidingTabs
        layoutId="bm-profile-tabs"
        className="max-w-full flex-wrap [&_[role=tab]]:min-h-8"
        value={tab}
        onChange={setTab}
        options={[
          { id: 'leaderboard', label: '基准分榜', badge: data?.mini_leaderboard.length, icon: <BarChart3 size={14} /> },
          { id: 'groups', label: '分组对比', icon: <GitCompareArrows size={14} /> },
          { id: 'scatter', label: '效率前沿散点', icon: <Crosshair size={14} /> },
          { id: 'hardest', label: '最难题目榜 (Hardest)', icon: <Flame size={14} /> },
          { id: 'tasks', label: '题目分布', badge: data?.task_summary.indexed_tasks, icon: <ListChecks size={14} /> },
          { id: 'properties', label: '发行版与属性', badge: data?.releases.length, icon: <Layers3 size={14} /> },
        ]}
      />

      {tab === 'groups' && data && !profQ.isError && (
        <GroupedComparePanel
          benchmark={benchmarkSlug}
          data={data.grouped_comparison}
          datasetReleases={data.releases}
          onReload={profQ.refetch}
        />
      )}
      {(tab === 'leaderboard' || tab === 'groups') && data && (
        <PublicBaselinePanel
          benchmark={benchmarkSlug}
          candidates={data.mini_leaderboard.map((row, index) => ({
            id: `${row.batch_id || 'score'}-${index}`,
            label: `${row.model_canonical || '模型未知'} · ${row.harness || row.runner}`,
            batchId: row.batch_id,
          }))}
          emptyMessage="该基准暂无可用于比较的站内批次。"
        />
      )}

      {profQ.isLoading ? (
        <TableSkeleton rows={6} cols={5} />
      ) : profQ.isError ? (
        <StateNoticeCard kind="error" title="基准档案加载失败" description={(profQ.error as Error).message} onRetry={() => profQ.refetch()} />
      ) : !data ? null : tab === 'leaderboard' ? (
        data.mini_leaderboard.length === 0 ? (
          <StateNoticeCard kind="empty" title="该基准暂无已入库成绩" description={`当前接入状态：${data.benchmark.status}`} />
        ) : (
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  <th className="py-2.5 px-3.5">#</th>
                  <th className="py-2.5 px-3">模型</th>
                  <th className="py-2.5 px-3">Harness / Runner</th>
                  <th className="py-2.5 px-3">协议 / 范围</th>
                  <th className="py-2.5 px-3 text-right">覆盖</th>
                  <th className="py-2.5 px-3.5 text-right">得分 (0-100)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {data.mini_leaderboard.map((row, idx) => (
                  <tr
                    key={`${row.model_canonical}-${row.harness}-${row.scoring_protocol}-${idx}`}
                    onClick={() => row.batch_id && navigate(`/eval/batches/${row.batch_id}`)}
                    className={row.batch_id ? 'hover:bg-[var(--bg-elevated)]/70 cursor-pointer' : ''}
                  >
                    <td className="py-2 px-3.5 font-mono text-[var(--text-muted)]">#{idx + 1}</td>
                    <td className="py-2 px-3 font-semibold text-[var(--text-primary)]">{row.model_canonical || '—'}</td>
                    <td className="py-2 px-3 text-[var(--text-secondary)]">
                      {row.harness || row.runner} · {row.runner}
                    </td>
                    <td className="py-2 px-3 font-mono text-[11px] text-[var(--text-muted)]">
                      {row.scoring_protocol} · {runScopeLabel(row.run_scope)}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                      {row.scored !== null ? `${row.scored}/${row.recorded}` : 'full_split'}
                    </td>
                    <td className="py-2 px-3.5 text-right font-bold tabular-nums text-[var(--best-cell)]">
                      {fmtPct(row.score)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : tab === 'groups' ? null : tab === 'scatter' ? (
        data.scatter_configs.length === 0 ? (
          <StateNoticeCard kind="empty" title="该基准暂无多批次效率散点数据" description="需要至少包含有效单题耗时或 Token 数据的批次。" />
        ) : (
          <EfficiencyScatterChart
            initialReleaseId={data.scatter_configs[0].dataset_release_id}
            initialProtocol={data.scatter_configs[0].scoring_protocol}
            presets={data.scatter_configs.map((c) => ({
              dataset_release_id: c.dataset_release_id,
              scoring_protocol: c.scoring_protocol,
              label: `${c.dataset_release_id} (${c.scoring_protocol})`,
            }))}
            onSelectBatch={(pt) => navigate(`/eval/batches/${pt.batch_id}`)}
          />
        )
      ) : tab === 'hardest' ? (
        hardestQ.isLoading ? (
          <TableSkeleton rows={8} cols={6} />
        ) : (hardestQ.data?.items || []).length === 0 ? (
          <StateNoticeCard kind="empty" title="暂无逐题通过率统计" description="需包含单题级 episodes 记录且未被 holdout 权限屏蔽。" />
        ) : (
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  <th className="py-2.5 px-3.5">Task ID</th>
                  <th className="py-2.5 px-3 text-right">通过率</th>
                  <th className="py-2.5 px-3 text-right">均分</th>
                  <th className="py-2.5 px-3 text-right">通过 / 有效 / 故障</th>
                  <th className="py-2.5 px-3 text-right">平均耗时</th>
                  <th scope="col" className="w-12 whitespace-nowrap py-2.5 px-2 text-center">定位</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {hardestQ.data!.items.map((t) => (
                  <tr key={t.task_id} className="group/row-action hover:bg-[var(--bg-elevated)]/65">
                    <td className="py-2 px-3.5 font-mono text-[var(--text-primary)] flex items-center gap-1.5">
                      <Flame className="w-3.5 h-3.5 text-[var(--sem-err-fg)] shrink-0" />
                      <span>{t.task_id}</span>
                    </td>
                    <td className="py-2 px-3 text-right font-bold tabular-nums text-[var(--sem-err-fg)]">
                      {fmtPct(t.pass_rate_pct)}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums">{t.mean_reward ?? '—'}</td>
                    <td className="py-2 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                      {t.passed_attempts} / {t.valid_attempts} / {t.fault_attempts}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums text-[var(--text-muted)]">
                      {fmtDuration(t.avg_duration_sec)}
                    </td>
                    <td className="w-12 py-2 px-2 text-center">
                      <RowActionIcon
                        variant="microscope"
                        to={`/eval/batches/${encodeURIComponent(t.sample_batch_id)}/trials/${encodeURIComponent(t.sample_trial_id)}`}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : tab === 'tasks' ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
            <h2 className="m-0 text-sm font-bold text-[var(--text-primary)] mb-2">
              题目类别分布（已索引 {data.task_summary.indexed_tasks} 题）
            </h2>
            {data.task_summary.by_category.length > 0 ? (
              <EChartBox option={domainChartOpt} height={320} exportName={`benchmark-domains-${benchmarkSlug}`} />
            ) : (
              <StateNoticeCard kind="empty" title="该基准暂未索引 task_definitions 类别元数据" />
            )}
          </div>
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
            <h2 className="m-0 text-sm font-bold text-[var(--text-primary)] mb-3">难度分布</h2>
            {data.task_summary.by_difficulty.length > 0 ? (
              <div className="space-y-2 text-xs">
                {data.task_summary.by_difficulty.map((d) => (
                  <div key={d.difficulty} className="flex items-center justify-between rounded-lg bg-[var(--bg-elevated)] px-3 py-2">
                    <span className="font-medium text-[var(--text-primary)]">{d.difficulty}</span>
                    <span className="font-mono tabular-nums text-[var(--text-secondary)]">{d.count} 题</span>
                  </div>
                ))}
              </div>
            ) : (
              <StateNoticeCard kind="empty" title="暂无难度字段" />
            )}
          </div>
        </div>
      ) : (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                <th className="py-2.5 px-3.5">dataset_release_id</th>
                <th className="py-2.5 px-3">版本 / Split</th>
                <th className="py-2.5 px-3">Runner / 协议</th>
                <th className="py-2.5 px-3">标尺 / 已知题数</th>
                <th className="py-2.5 px-3.5">审批状态</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {data.releases.map((r) => (
                <tr key={r.dataset_release_id}>
                  <td className="py-2.5 px-3.5 font-mono text-[var(--text-primary)]">{r.dataset_release_id}</td>
                  <td className="py-2.5 px-3 text-[var(--text-secondary)]">
                    {r.dataset_version} / {r.split}
                  </td>
                  <td className="py-2.5 px-3 font-mono text-[11px] text-[var(--text-muted)]">
                    {r.runner} · {r.default_protocol}
                  </td>
                  <td className="py-2.5 px-3 tabular-nums text-[var(--text-secondary)]">
                    {r.scale_type} · {r.known_task_count ?? '—'} 题
                  </td>
                  <td className="py-2.5 px-3.5">
                    <span className="rounded px-1.5 py-0.5 text-[11px] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                      {r.source_approval}
                    </span>
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
