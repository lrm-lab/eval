import React, { useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  type ColumnDef,
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type SortingState,
  useReactTable,
} from '@tanstack/react-table';
import type * as echarts from 'echarts';
import {
  ArrowUpDown,
  BarChart3,
  Check,
  Copy,
  GitCompareArrows,
  LineChart,
  Plus,
  ScatterChart,
  Search,
  SlidersHorizontal,
  Table2,
} from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDuration, fmtPct, stableColorForKey } from '../lib/format';
import { type ColumnPreset, useAppStore } from '../lib/store';
import type {
  BatchDTO,
  DatasetTreeTrack,
  EfficiencyScatterPoint,
  LeaderboardBenchmarkCell,
  LeaderboardItem,
  LeaderboardResponse,
} from '../types/api';
import { PageTransition, SlidingTabs } from '../components/motion/primitives';
import { PublicBaselinePanel } from '../components/ui/PublicBaselinePanel';
import { CompositeScoreCell } from '../components/ui/CompositeScoreCell';
import { TokenBreakdownBar } from '../components/ui/TokenBreakdownBar';
import { MethodologyPopover } from '../components/ui/MethodologyPopover';
import { SlideOverDrawer } from '../components/ui/SlideOverDrawer';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';
import { EChartBox, glassTooltipConfig } from '../components/charts/EChartBox';
import { EfficiencyScatterChart } from '../components/charts/EfficiencyScatterChart';
import { LeaderboardScoreCell, leaderboardScopeLabel, leaderboardScoreBasis } from '../components/leaderboard/LeaderboardScoreCell';

type ViewMode = 'table' | 'trend' | 'bar' | 'scatter';

const filterFieldClass = 'h-9 w-full min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-xs text-[var(--text-primary)] hover:border-[var(--border-strong)]';
const toolbarButtonClass = 'inline-flex h-9 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg border px-2.5 text-xs font-medium cursor-pointer motion-safe:transition-colors';
const defaultButtonClass = 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)]';

const columnHelper = createColumnHelper<LeaderboardItem>();
const scopeOrder = ['full_split', 'subset', 'smoke', 'reference'];
const scopePosition = new Map(scopeOrder.map((scope, position) => [scope, position]));

function filterOptions(list: string[] = [], selected: string) {
  return selected && !list.includes(selected) ? [...list, selected] : list;
}

function LeaderboardTrendBarChart({
  items,
  benchmarkKeys,
  viewMode,
  activeTrack,
}: {
  items: LeaderboardItem[];
  benchmarkKeys: Array<{ key: string; label: string }>;
  viewMode: 'trend' | 'bar';
  activeTrack: string;
}) {
  const theme = useAppStore((s) => s.theme);
  const chartOption = useMemo<echarts.EChartsCoreOption>(() => {
    const textColor = theme === 'dark' ? '#a1a1aa' : '#475569';
    const gridColor = theme === 'dark' ? '#27272a' : '#e5e7eb';
    const topSubjects = items.slice(0, 8);
    const categories = benchmarkKeys.map((b) => b.label);

    return {
      tooltip: { ...glassTooltipConfig(theme), trigger: 'axis' },
      legend: { type: 'scroll', bottom: 4, textStyle: { color: textColor, fontSize: 11 } },
      grid: { top: 24, right: 24, bottom: 56, left: 48 },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { color: textColor, fontSize: 11, rotate: categories.length > 5 ? 18 : 0 },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { color: textColor, fontSize: 11, formatter: '{value}%' },
        splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
      },
      series: topSubjects.map((s) => ({
        name: `${s.model_canonical} (${s.harness})`,
        type: viewMode === 'trend' ? 'line' : 'bar',
        smooth: viewMode === 'trend',
        universalTransition: true,
        itemStyle: { color: stableColorForKey(s.subject_id), borderRadius: viewMode === 'bar' ? [4, 4, 0, 0] : 0 },
        data: benchmarkKeys.map((bk) => s.benchmarks[bk.key]?.score ?? null),
      })),
    };
  }, [items, benchmarkKeys, viewMode, theme]);

  return <EChartBox option={chartOption} height={420} exportName={`leaderboard-${activeTrack}-${viewMode}`} />;
}

export function LeaderboardPage() {
  const navigate = useNavigate();
  const params = useParams<{ track?: string }>();
  const [searchParams, setSearchParams] = useSearchParams();

  const density = useAppStore((s) => s.density);
  const setDensity = useAppStore((s) => s.setDensity);
  const columnPreset = useAppStore((s) => s.columnPreset);
  const setColumnPreset = useAppStore((s) => s.setColumnPreset);
  const customColumns = useAppStore((s) => s.customColumns);
  const setCustomColumns = useAppStore((s) => s.setCustomColumns);
  const compareBasket = useAppStore((s) => s.compareBasket);
  const addToCompare = useAppStore((s) => s.addToCompare);
  const pushToast = useAppStore((s) => s.pushToast);

  const activeTrack = params.track || searchParams.get('track') || 'all';
  const benchmark = searchParams.get('benchmark') || '';
  const viewMode = (searchParams.get('view') as ViewMode) || 'table';
  const runScope = searchParams.get('run_scope') || '';
  const protocol = searchParams.get('protocol') || '';
  const harness = searchParams.get('harness') || '';
  const runner = searchParams.get('runner') || '';
  const thinkingMode = searchParams.get('thinking_mode') || '';
  const includeLegacy = searchParams.get('include_legacy') !== 'false';
  const trainedOnly = searchParams.get('trained_only') === 'true';
  const hasAtif = searchParams.get('has_atif') === 'true';
  const hasGateway = searchParams.get('has_gateway') === 'true';
  const modelFamily = searchParams.get('q') || '';

  const [sorting, setSorting] = useState<SortingState>([]);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [copiedLink, setCopiedLink] = useState(false);
  const advancedFilterCount = [runner, protocol, thinkingMode, !includeLegacy, trainedOnly, hasAtif, hasGateway].filter(Boolean).length;

  // L2 Slide-Over Drawer state (preserves underlying table filters, scroll, Compare Dock!)
  const [drawerBatchId, setDrawerBatchId] = useState<string | null>(null);
  const [drawerCell, setDrawerCell] = useState<{ item?: LeaderboardItem; cell?: LeaderboardBenchmarkCell; pt?: EfficiencyScatterPoint } | null>(null);

  const updateParam = (key: string, val: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (!val) next.delete(key);
    else next.set(key, val);
    setSearchParams(next, { replace: true });
  };

  // 100% dynamic taxonomy from backend (never hardcoded track/benchmark enums)
  const treeQ = useQuery({
    queryKey: ['datasets-tree'],
    queryFn: () => apiFetch<{ tracks: DatasetTreeTrack[] }>('/api/v1/datasets/tree'),
  });

  const lbQueryUrl = useMemo(() => {
    const qs = new URLSearchParams();
    qs.set('track_slug', activeTrack);
    qs.set('include_legacy', String(includeLegacy));
    if (runScope) qs.set('run_scope', runScope);
    if (protocol) qs.set('protocol', protocol);
    if (harness) qs.set('harness', harness);
    if (runner) qs.set('runner', runner);
    if (thinkingMode) qs.set('thinking_mode', thinkingMode);
    if (trainedOnly) qs.set('trained_only', 'true');
    if (hasAtif) qs.set('has_atif', 'true');
    if (hasGateway) qs.set('has_gateway', 'true');
    if (modelFamily.trim()) qs.set('model_family', modelFamily.trim());
    qs.set('page_size', '100');
    return `/api/v1/leaderboard?${qs.toString()}`;
  }, [activeTrack, includeLegacy, runScope, protocol, harness, runner, thinkingMode, trainedOnly, hasAtif, hasGateway, modelFamily]);

  const lbQ = useQuery({
    queryKey: ['leaderboard', lbQueryUrl],
    queryFn: () => apiFetch<LeaderboardResponse>(lbQueryUrl),
  });

  const drawerBatchQ = useQuery({
    queryKey: ['drawer-batch-detail', drawerBatchId],
    enabled: Boolean(drawerBatchId),
    queryFn: () => apiFetch<BatchDTO>(`/api/v1/batches/${drawerBatchId}`),
  });
  const drawerBasis = drawerCell?.cell ? leaderboardScoreBasis(drawerCell.cell) : null;
  const drawerBatch = drawerBatchQ.data;

  const selectedTrack = activeTrack === 'all' ? undefined : activeTrack;
  const datasetChoices = useMemo(() => {
    const choices = new Map<string, string>();
    for (const track of treeQ.data?.tracks ?? []) {
      if (selectedTrack && track.track_slug !== selectedTrack) continue;
      for (const node of track.benchmarks) choices.set(node.benchmark_slug, node.display_name);
    }
    for (const row of lbQ.data?.items ?? []) {
      for (const cell of Object.values(row.benchmarks)) {
        if (!choices.has(cell.benchmark_slug)) choices.set(cell.benchmark_slug, cell.raw_dataset_name || cell.benchmark_slug);
      }
    }
    if (benchmark && !choices.has(benchmark)) choices.set(benchmark, benchmark);
    return Array.from(choices).sort((a, b) => a[1].localeCompare(b[1]));
  }, [selectedTrack, treeQ.data, lbQ.data, benchmark]);
  const items = useMemo(() => {
    const rows = lbQ.data?.items ?? [];
    return benchmark ? rows.filter((row) => Object.values(row.benchmarks).some((cell) => cell.benchmark_slug === benchmark)) : rows;
  }, [lbQ.data, benchmark]);
  const availableFilters = lbQ.data?.available_filters;
  const commonCount = lbQ.data?.common_comparable_benchmarks.length ?? 0;

  const releaseLabels = useMemo(() => new Map(
    (treeQ.data?.tracks ?? []).flatMap((track) => track.benchmarks.flatMap((node) =>
      node.releases.map((release) => [release.dataset_release_id, `${release.dataset_version} · ${release.split}`] as const)
    ))
  ), [treeQ.data]);

  // Collect all unique benchmark cell keys present in current filtered result
  const benchmarkKeys = useMemo(() => {
    const map = new Map<string, { key: string; label: string; benchmarkSlug: string; releaseId: string; proto: string; scope: string; count: number; maxScore: number }>();
    for (const row of items) {
      for (const [k, cell] of Object.entries(row.benchmarks)) {
        if (benchmark && cell.benchmark_slug !== benchmark) continue;
        const cur = map.get(k);
        const sc = cell.score ?? -1;
        if (!cur) {
          map.set(k, {
            key: k,
            label: cell.raw_dataset_name || cell.benchmark_slug,
            benchmarkSlug: cell.benchmark_slug,
            releaseId: cell.dataset_release_id,
            proto: cell.scoring_protocol,
            scope: cell.run_scope,
            count: 1,
            maxScore: sc,
          });
        } else {
          cur.count += 1;
          if (sc > cur.maxScore) cur.maxScore = sc;
        }
      }
    }
    const sorted = Array.from(map.values()).sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
    return benchmark ? sorted : sorted.slice(0, columnPreset === 'diagnostics' ? 14 : 8);
  }, [items, columnPreset, benchmark]);

  const showCol = (colId: string) => {
    if (columnPreset === 'overview') return ['comparative_macro_avg', 'self_coverage_avg'].includes(colId);
    if (columnPreset === 'efficiency') return ['comparative_macro_avg', 'avg_duration_sec', 'tokens'].includes(colId);
    if (columnPreset === 'diagnostics') return true;
    return customColumns.includes(colId);
  };

  const columns = useMemo<ColumnDef<LeaderboardItem, unknown>[]>(() => {
    const cols: ColumnDef<LeaderboardItem, any>[] = [
      columnHelper.accessor('model_canonical', {
        id: 'identity',
        header: () => <span>模型 / Harness</span>,
        cell: (info) => {
          const r = info.row.original;
          const firstBatchId = Object.values(r.benchmarks).find((c) => c.batch_id && (!benchmark || c.benchmark_slug === benchmark))?.batch_id || null;
          const inBasket = compareBasket.some((x) => x.id === (firstBatchId || r.subject_id));
          return (
            <div className="flex min-w-[124px] max-w-[235px] items-center justify-between gap-1.5 sm:min-w-[178px]">
              <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-1.5">
                  {!benchmark && r.comparative_macro_avg != null && (
                    <span className="shrink-0 text-[10px] tabular-nums text-[var(--text-muted)]" aria-label={`共同成绩第 ${r.rank} 名`}>
                      {r.rank}.
                    </span>
                  )}
                  <Link
                    to={`/models/${encodeURIComponent(r.model_canonical)}`}
                    className="font-semibold text-xs text-[var(--text-primary)] hover:text-[var(--brand-primary)] no-underline truncate"
                    title={r.model_canonical}
                  >
                    {r.model_canonical}
                  </Link>
                </div>
                <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5 text-[10px] text-[var(--text-muted)]">
                  <Link
                    to={`/harnesses/${encodeURIComponent(r.harness)}`}
                    className="max-w-full truncate rounded bg-[var(--bg-elevated)] px-1 text-[var(--text-secondary)] no-underline hover:text-[var(--brand-primary)]"
                    title={`${r.harness}${r.harness_version ? `@${r.harness_version}` : ''}`}
                  >
                    {r.harness}
                    {r.harness_version ? `@${r.harness_version}` : ''}
                  </Link>
                  <span>{r.runner}</span>
                  {r.thinking_mode && <span className="text-[var(--sem-legacy-fg)]">{r.thinking_mode}</span>}
                  {r.has_gateway && <span className="text-[var(--sem-running-fg)]">GW</span>}
                </div>
              </div>
              <button
                type="button"
                title={firstBatchId ? '加入对比' : '历史汇总对象加入对比'}
                aria-label={`${r.model_canonical} · ${r.harness}${inBasket ? ' 已加入对比' : ' 加入对比'}`}
                aria-pressed={inBasket}
                onClick={(e) => {
                  e.stopPropagation();
                  if (firstBatchId) {
                    addToCompare({
                      id: firstBatchId,
                      kind: 'batch',
                      batch_id: firstBatchId,
                      alias: `${r.model_canonical} (${r.harness})`,
                    });
                  } else {
                    addToCompare({
                      id: r.subject_id,
                      kind: 'legacy_model',
                      model_cols: [r.model_canonical],
                      alias: r.model_canonical,
                    });
                  }
                }}
                className={`shrink-0 rounded-md p-1 border transition-colors cursor-pointer ${
                  inBasket
                    ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                    : 'border-[var(--border-subtle)] text-[var(--text-muted)] hover:text-[var(--text-primary)]'
                }`}
              >
                {inBasket ? <Check className="w-3.5 h-3.5" /> : <Plus className="w-3.5 h-3.5" />}
              </button>
            </div>
          );
        },
      }),
    ];

    if (!benchmark && showCol('comparative_macro_avg')) {
      cols.push(
        columnHelper.accessor('comparative_macro_avg', {
          id: 'comparative_macro_avg',
          header: () => <span>可比宏观均分</span>,
          cell: (info) => {
            const r = info.row.original;
            return (
              <CompositeScoreCell
                score={r.comparative_macro_avg}
                coverage={r.comparative_bench_count > 0 ? `${r.comparative_bench_count} 个共同基准` : undefined}
              />
            );
          },
        })
      );
    }

    if (!benchmark && showCol('self_coverage_avg')) {
      cols.push(
        columnHelper.accessor('self_coverage_avg', {
          id: 'self_coverage_avg',
          header: () => <span>自覆盖参考均分</span>,
          cell: (info) => {
            const r = info.row.original;
            return <CompositeScoreCell score={r.self_coverage_avg} coverage={`${r.self_bench_count} 个自身基准`} />;
          },
        })
      );
    }

    if (!benchmark && showCol('avg_duration_sec')) {
      cols.push(
        columnHelper.accessor('avg_duration_sec', {
          id: 'avg_duration_sec',
          header: () => <span>平均总耗时</span>,
          cell: (info) => <span className="text-xs tabular-nums text-[var(--text-secondary)]">{fmtDuration(info.getValue())}</span>,
        })
      );
    }

    if (!benchmark && showCol('tokens')) {
      cols.push(
        columnHelper.display({
          id: 'tokens',
          header: () => (
            <span className="inline-flex items-center gap-1">
              <span>Token 构成</span>
              <MethodologyPopover
                title="Token 语义区分"
                summary="仅当来源明确核验为互斥 4 段（mutually_exclusive_verified）时绘制堆叠条；否则采用独立标签并排展示。"
                anchor="#token-semantics"
              />
            </span>
          ),
          cell: (info) => <TokenBreakdownBar tokens={info.row.original.tokens} />,
        })
      );
    }

    const datasetOrder = [...new Set(benchmarkKeys.map((key) => key.benchmarkSlug))];
    const displayedKeys = [...benchmarkKeys].sort((a, b) =>
      datasetOrder.indexOf(a.benchmarkSlug) - datasetOrder.indexOf(b.benchmarkSlug)
      || (scopePosition.get(a.scope) ?? scopeOrder.length) - (scopePosition.get(b.scope) ?? scopeOrder.length)
    );
    const scoreColumns = displayedKeys.map((bk) => {
      const sameScope = benchmarkKeys.filter((entry) => entry.label === bk.label && entry.scope === bk.scope);
      const release = releaseLabels.get(bk.releaseId) || bk.releaseId;
      const includeRelease = sameScope.some((entry) => entry.releaseId !== bk.releaseId);
      const includeProtocol = sameScope.some((entry) => entry.proto !== bk.proto);
      const distinction = [includeRelease ? release : '', includeProtocol ? bk.proto : ''].filter(Boolean).join(' / ');
      return columnHelper.accessor((row): unknown => row.benchmarks[bk.key]?.score ?? undefined, {
        id: `bm_${bk.key}`,
        sortUndefined: 'last',
        sortDescFirst: true,
        header: () => (
          <span className="block min-w-20">
            <span className="block max-w-[140px] truncate" title={`${bk.label} · ${release} · ${bk.proto}`}>
              {bk.label}
            </span>
            <span className={`mt-0.5 block text-[10px] font-medium ${bk.scope === 'full_split' ? 'text-[var(--text-secondary)]' : 'text-[var(--sem-zero-fg)]'}`}>
              {leaderboardScopeLabel(bk.scope)}
            </span>
            {distinction && <span className="block max-w-[140px] break-all whitespace-normal text-[10px] font-normal text-[var(--text-muted)]">{distinction}</span>}
          </span>
        ),
        cell: (info) => {
          const r = info.row.original;
          const cell = r.benchmarks[bk.key];
          if (!cell) return <span className="block px-1.5 text-xs text-[var(--text-muted)]">缺测</span>;
          const isBest = cell.score !== null && Math.abs(cell.score - bk.maxScore) < 1e-6;
          return (
            <LeaderboardScoreCell
              cell={cell}
              isBest={isBest}
              onOpen={() => {
                setDrawerBatchId(cell.batch_id);
                setDrawerCell({ item: r, cell });
              }}
            />
          );
        },
      });
    });

    return [cols[0], ...scoreColumns, ...cols.slice(1)];
  }, [benchmark, benchmarkKeys, releaseLabels, columnPreset, customColumns, compareBasket, addToCompare]);

  const table = useReactTable({
    data: items,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const visibleRows = table.getRowModel().rows;
  const publicCandidates = useMemo(() => visibleRows.flatMap(({ original: item }) =>
    Object.entries(item.benchmarks).filter(([, cell]) => !benchmark || cell.benchmark_slug === benchmark).map(([key, cell]) => ({
      id: `${item.subject_id}:${key}`,
      label: `${item.model_canonical} · ${item.harness} · ${cell.raw_dataset_name || cell.benchmark_slug}`,
      batchId: cell.batch_id,
      benchmark: cell.benchmark_slug,
    }))
  ), [visibleRows, benchmark]);

  const changeTrack = (track: string) => {
    const next = new URLSearchParams(searchParams);
    const activeTree = treeQ.data?.tracks.find((item) => item.track_slug === track);
    if (activeTree && !activeTree.benchmarks.some((node) => node.benchmark_slug === benchmark)) next.delete('benchmark');
    if (params.track) {
      next.delete('track');
      navigate({ pathname: `/leaderboard/${track}`, search: next.toString() });
    } else {
      if (track === 'all') next.delete('track');
      else next.set('track', track);
      setSearchParams(next, { replace: true });
    }
  };

  const copyShareLink = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopiedLink(true);
      pushToast('success', '当前主榜链接已复制');
      window.setTimeout(() => setCopiedLink(false), 1600);
    } catch {
      pushToast('error', '无法访问剪贴板，请从地址栏复制链接');
    }
  };

  return (
    <PageTransition className="mx-auto min-w-0 max-w-[1600px] space-y-3 px-3 py-4 sm:px-5 sm:py-5">
      <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h1 className="m-0 text-xl font-bold tracking-tight text-[var(--text-primary)]">综合主榜</h1>
        <SlidingTabs
          layoutId="lb-view-switcher"
          className="max-w-full max-sm:grid max-sm:w-full max-sm:grid-cols-4 max-sm:[&>button]:min-w-0 max-sm:[&>button]:justify-center max-sm:[&>button]:px-1"
          size="sm"
          value={viewMode}
          onChange={(v) => updateParam('view', v === 'table' ? null : v)}
          options={[
            { id: 'table', label: '成绩表', icon: <Table2 className="size-3.5" /> },
            { id: 'trend', label: '趋势', icon: <LineChart className="size-3.5" /> },
            { id: 'bar', label: '柱图', icon: <BarChart3 className="size-3.5" /> },
            { id: 'scatter', label: '散点', icon: <ScatterChart className="size-3.5" /> },
          ]}
        />
      </header>

      {viewMode !== 'scatter' && (
        <div role="search" aria-label="主榜筛选" className="@container rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-2.5">
          <div className="@min-[68rem]:flex @min-[68rem]:items-center @min-[68rem]:gap-2">
            <div className="grid min-w-0 grid-cols-2 gap-1.5 @min-[30rem]:grid-cols-4 @min-[51rem]:grid-cols-[minmax(0,1.4fr)_minmax(0,.8fr)_minmax(0,1fr)_minmax(0,.7fr)_minmax(0,.85fr)] @min-[68rem]:flex-1">
              <div className="relative col-span-full @min-[51rem]:col-span-1">
                <Search aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--text-muted)]" />
                <input
                  type="search"
                  aria-label="搜索模型族或模型名"
                  value={modelFamily}
                  onChange={(e) => updateParam('q', e.target.value || null)}
                  placeholder="搜索模型"
                  className={`${filterFieldClass} pl-8 pr-2`}
                />
              </div>
              <select aria-label="筛选轨道" value={activeTrack} onChange={(e) => changeTrack(e.target.value)} className={`${filterFieldClass} truncate px-2`}>
                <option value="all">全部轨道</option>
                {(treeQ.data?.tracks || []).map((track) => <option key={track.track_slug} value={track.track_slug}>{track.display_name}</option>)}
              </select>
              <select
                aria-label="筛选题库"
                value={benchmark}
                onChange={(e) => {
                  setSorting([]);
                  updateParam('benchmark', e.target.value || null);
                }}
                className={`${filterFieldClass} truncate px-2`}
              >
                <option value="">全部题库</option>
                {datasetChoices.map(([slug, label]) => <option key={slug} value={slug}>{label}</option>)}
              </select>
              <select aria-label="筛选评估范围" value={runScope} onChange={(e) => updateParam('run_scope', e.target.value || null)} className={`${filterFieldClass} truncate px-2`}>
                <option value="">全部范围</option>
                {scopeOrder.map((scope) => <option key={scope} value={scope}>{leaderboardScopeLabel(scope)}</option>)}
              </select>
              <select aria-label="筛选 Harness" value={harness} onChange={(e) => updateParam('harness', e.target.value || null)} className={`${filterFieldClass} truncate px-2`}>
                <option value="">全部 Harness</option>
                {filterOptions(availableFilters?.harnesses, harness).map((option) => <option key={option}>{option}</option>)}
              </select>
            </div>

            <div className="mt-2 flex flex-wrap items-center justify-between gap-1.5 @min-[68rem]:mt-0">
              <button
                type="button"
                aria-controls="leaderboard-advanced"
                aria-expanded={advancedOpen}
                onClick={() => setAdvancedOpen((open) => !open)}
                className={`${toolbarButtonClass} ${defaultButtonClass}`}
              >
                <SlidersHorizontal aria-hidden="true" className="size-3.5" />
                进阶筛选与显示{advancedFilterCount ? ` (${advancedFilterCount})` : ''}
              </button>
              <button
                type="button"
                onClick={() => navigate('/eval/compare')}
                className={`${toolbarButtonClass} border-transparent bg-[color-mix(in_srgb,var(--brand-primary)_80%,black)] text-white hover:bg-[color-mix(in_srgb,var(--brand-hover)_75%,black)]`}
              >
                <GitCompareArrows aria-hidden="true" className="size-3.5" />
                对比{compareBasket.length ? ` (${compareBasket.length})` : ''}
              </button>
            </div>
          </div>

          <div id="leaderboard-advanced" hidden={!advancedOpen} className="mt-2 border-t border-[var(--border-subtle)] pt-2">
            <div className="grid grid-cols-2 gap-1.5 @min-[37rem]:grid-cols-4">
              <select aria-label="筛选 Runner" value={runner} onChange={(e) => updateParam('runner', e.target.value || null)} className={`${filterFieldClass} truncate px-2`}>
                <option value="">全部 Runner</option>
                {filterOptions(availableFilters?.runners, runner).map((option) => <option key={option}>{option}</option>)}
              </select>
              <select aria-label="筛选协议" value={protocol} onChange={(e) => updateParam('protocol', e.target.value || null)} className={`${filterFieldClass} truncate px-2`}>
                <option value="">全部评分协议</option>
                {filterOptions(availableFilters?.protocols, protocol).map((option) => <option key={option}>{option}</option>)}
              </select>
              {!benchmark && (
                <select aria-label="列预设" value={columnPreset} onChange={(e) => setColumnPreset(e.target.value as ColumnPreset)} className={`${filterFieldClass} truncate px-2`}>
                  <option value="overview">常用列</option>
                  <option value="efficiency">效率列</option>
                  <option value="diagnostics">诊断列</option>
                  <option value="custom">自定义列</option>
                </select>
              )}
              <button type="button" onClick={() => setDensity(density === 'compact' ? 'comfortable' : 'compact')} className={`${toolbarButtonClass} ${defaultButtonClass}`}>
                {density === 'compact' ? '切换舒适密度' : '切换紧凑密度'}
              </button>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-[var(--text-secondary)]">
              {[
                { key: 'include_legacy', label: '含 2026-11-20 基线', checked: includeLegacy, next: includeLegacy ? 'false' : null },
                { key: 'trained_only', label: '仅训练检查点', checked: trainedOnly, next: trainedOnly ? null : 'true' },
                { key: 'has_atif', label: '含 ATIF', checked: hasAtif, next: hasAtif ? null : 'true' },
                { key: 'has_gateway', label: '含网关抓包', checked: hasGateway, next: hasGateway ? null : 'true' },
              ].map(({ key, label, checked, next }) => (
                <label key={key} className="inline-flex cursor-pointer items-center gap-1.5">
                  <input type="checkbox" checked={checked} onChange={() => updateParam(key, next)} />
                  {label}
                </label>
              ))}
              <button type="button" onClick={copyShareLink} className={`${toolbarButtonClass} ${defaultButtonClass}`}>
                {copiedLink ? <Check className="size-3.5 text-[var(--sem-pass-fg)]" /> : <Copy className="size-3.5" />}
                复制链接
              </button>
            </div>
            {!benchmark && columnPreset === 'custom' && (
              <fieldset className="mt-2 flex flex-wrap gap-x-3 gap-y-1.5 text-xs text-[var(--text-secondary)]">
                <legend className="mb-1 font-medium">自定义汇总列</legend>
                {[
                  { id: 'comparative_macro_avg', label: '可比宏观均分' },
                  { id: 'self_coverage_avg', label: '自覆盖参考均分' },
                  { id: 'avg_duration_sec', label: '平均总耗时' },
                  { id: 'tokens', label: 'Token 构成' },
                ].map(({ id, label }) => {
                  const checked = customColumns.includes(id);
                  return (
                    <label key={id} className="inline-flex cursor-pointer items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => setCustomColumns(checked ? customColumns.filter((column) => column !== id) : [...customColumns, id])}
                      />
                      {label}
                    </label>
                  );
                })}
              </fieldset>
            )}
          </div>
        </div>
      )}

      {viewMode !== 'scatter' && (
        <PublicBaselinePanel
          compact
          candidates={publicCandidates}
          filterBenchmark
          benchmark={benchmark || undefined}
          emptyMessage={lbQ.isLoading ? '正在读取当前主榜批次…' : '当前主榜筛选没有该基准的站内成绩。'}
        />
      )}

      {viewMode === 'scatter' ? (
        <EfficiencyScatterChart
          onSelectBatch={(pt) => {
            setDrawerBatchId(pt.batch_id);
            setDrawerCell({ pt });
          }}
        />
      ) : lbQ.isLoading ? (
        <TableSkeleton rows={10} cols={7} />
      ) : lbQ.isError ? (
        <StateNoticeCard kind="error" title="主榜加载失败" description={(lbQ.error as Error).message} onRetry={() => lbQ.refetch()} />
      ) : items.length === 0 ? (
        <StateNoticeCard
          kind="empty"
          title="没有符合条件的成绩"
          description={benchmark && lbQ.data && lbQ.data.total > lbQ.data.items.length
            ? `当前前 ${lbQ.data.items.length} 个组合没有该题库成绩，可先按轨道、模型或 Harness 缩小范围。`
            : '可切换题库或放宽筛选条件。'}
        />
      ) : viewMode === 'table' ? (
        <section aria-label="模型与 Harness 成绩" className="min-w-0 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)]">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-xs">
            <span className="font-medium text-[var(--text-primary)]">
              {benchmark ? `${items.length} 个组合` : `${lbQ.data?.total ?? items.length} 个组合`}
            </span>
            <span className="inline-flex items-center gap-1 text-[var(--text-secondary)]">
              {benchmark ? '按同一列比较' : commonCount ? `${commonCount} 个共同基准` : '无共同口径，无法统一排名'}
              <MethodologyPopover
                title="成绩与比较口径"
                summary="同一列对应同一题库版本、评分协议和计划范围。非官方分数以有效评分数为均值分母，已记录数是去重后的记录总数。正式全量（计划）表示计划使用全量题集，实际完成度见批次。可比宏观均分使用共同基准，自覆盖均分仅作参考。"
                anchor="#macro-avg"
              />
            </span>
            {lbQ.data && lbQ.data.total > lbQ.data.items.length && (
              <span className="text-[var(--text-muted)]">按主榜前 {lbQ.data.items.length} 个组合展示，可用筛选缩小范围</span>
            )}
          </div>
          <div role="region" aria-label="成绩表，可横向滚动" tabIndex={0} className="min-w-0 overflow-x-auto rounded-b-xl">
            <table className="w-full border-collapse text-left">
              <thead>
                {table.getHeaderGroups().map((hg) => (
                  <tr key={hg.id} className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-xs text-[var(--text-secondary)]">
                    {hg.headers.map((header, idx) => (
                      <th
                        key={header.id}
                        scope="col"
                        aria-sort={header.column.getIsSorted() === 'asc' ? 'ascending' : header.column.getIsSorted() === 'desc' ? 'descending' : undefined}
                        className={`whitespace-nowrap px-2 py-2 text-left align-top font-semibold sm:px-3 ${
                          idx === 0
                            ? 'sticky left-0 z-20 w-[140px] max-w-[140px] bg-[var(--bg-elevated)] shadow-[2px_0_6px_rgba(15,23,42,0.06)] sm:w-[220px] sm:max-w-[220px]'
                            : ''
                        }`}
                      >
                        {header.column.getCanSort() ? (
                          <button type="button" onClick={header.column.getToggleSortingHandler()} className="inline-flex cursor-pointer items-start gap-1 text-left hover:text-[var(--text-primary)]">
                            {flexRender(header.column.columnDef.header, header.getContext())}
                            <ArrowUpDown aria-hidden="true" className="mt-0.5 size-3 shrink-0 opacity-50" />
                          </button>
                        ) : flexRender(header.column.columnDef.header, header.getContext())}
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {visibleRows.map((row) => (
                  <tr key={row.id} className="hover:bg-[var(--bg-elevated)]/65 transition-colors">
                    {row.getVisibleCells().map((cell, idx) => (
                      <td
                        key={cell.id}
                        className={`px-2 sm:px-3 ${density === 'compact' ? 'py-1' : 'py-2'} ${
                          idx === 0
                            ? 'sticky left-0 z-10 w-[140px] max-w-[140px] bg-[var(--bg-surface)] shadow-[2px_0_6px_rgba(15,23,42,0.05)] sm:w-[220px] sm:max-w-[220px]'
                            : ''
                        }`}
                      >
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 shadow-sm">
          <p className="m-0 mb-2 text-xs text-[var(--text-secondary)]">显示前 {Math.min(8, items.length)} 个组合，缺测留空。</p>
          <LeaderboardTrendBarChart
            items={items}
            benchmarkKeys={benchmarkKeys}
            viewMode={viewMode === 'trend' ? 'trend' : 'bar'}
            activeTrack={activeTrack}
          />
        </div>
      )}

      {/* L2 Right Slide-Over Drawer (Preserves underlying table filters, scroll, Compare Dock!) */}
      <SlideOverDrawer
        open={Boolean(drawerCell)}
        onClose={() => {
          setDrawerCell(null);
          setDrawerBatchId(null);
        }}
        title={
          drawerCell?.pt
            ? `${drawerCell.pt.model_canonical || drawerCell.pt.batch_name}`
            : drawerCell?.item
              ? `${drawerCell.item.model_canonical} · ${drawerCell.cell?.raw_dataset_name || ''}`
              : '批次详情预览'
        }
        subtitle={
          drawerCell?.cell
            ? `${drawerCell.cell.scoring_protocol} · ${leaderboardScopeLabel(drawerCell.cell.run_scope)}`
            : drawerCell?.pt
              ? `Harness: ${drawerCell.pt.harness || '—'} · N=${drawerCell.pt.common_valid_n} common valid`
              : undefined
        }
        onOpenFullPage={
          drawerBatchId
            ? () => navigate(`/eval/batches/${drawerBatchId}`)
            : drawerCell?.cell?.benchmark_slug
              ? () => navigate(`/benchmarks/${drawerCell.cell!.benchmark_slug}`)
              : undefined
        }
      >
        {drawerBatchId && drawerBatchQ.isLoading ? (
          <TableSkeleton rows={5} cols={2} />
        ) : drawerBatchId && drawerBatchQ.isError ? (
          <StateNoticeCard kind="error" title="无法读取此批次" description={(drawerBatchQ.error as Error).message} onRetry={() => drawerBatchQ.refetch()} />
        ) : drawerBatchId && drawerBatch ? (
          <div className="space-y-4 text-xs">
            {drawerCell?.cell && drawerBasis && (
              <section aria-label="主榜成绩" className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
                <h3 className="m-0 font-semibold">主榜成绩</h3>
                <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1 tabular-nums">
                  <strong className="text-lg text-[var(--text-primary)]">{fmtPct(drawerCell.cell.score)}</strong>
                  <span className="text-[var(--text-secondary)]">{drawerBasis.denominator}</span>
                  {drawerBasis.recorded && <span className="text-[var(--text-secondary)]">{drawerBasis.recorded}</span>}
                </div>
              </section>
            )}
            <section aria-label={drawerCell?.cell ? '代表批次' : '批次统计'} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
              <h3 className="m-0 font-semibold">{drawerCell?.cell ? '代表批次' : '批次统计'}</h3>
              <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[var(--text-secondary)]">
                <span>逐题有效均值 <strong className="tabular-nums text-[var(--text-primary)]">
                  {drawerBatch.valid_mean == null ? '—' : fmtPct(drawerBatch.valid_mean <= 1 ? drawerBatch.valid_mean * 100 : drawerBatch.valid_mean)}
                </strong></span>
                {drawerBatch.planned != null && drawerBatch.planned > 0 && (
                  <span>记录完成度 <strong className="tabular-nums text-[var(--text-primary)]">{fmtPct(drawerBatch.recorded / drawerBatch.planned * 100, 1)}</strong></span>
                )}
              </div>
              <dl className="m-0 mt-3 grid grid-cols-3 gap-2 text-[var(--text-secondary)]">
                <div><dt>有效评分</dt><dd className="m-0 mt-0.5 font-semibold tabular-nums text-[var(--text-primary)]">{drawerBatch.scored}</dd></div>
                <div><dt>已记录</dt><dd className="m-0 mt-0.5 font-semibold tabular-nums text-[var(--text-primary)]">{drawerBatch.recorded}</dd></div>
                <div><dt>计划样本</dt><dd className="m-0 mt-0.5 font-semibold tabular-nums text-[var(--text-primary)]">{drawerBatch.planned ?? '未提供'}</dd></div>
              </dl>
            </section>

            {drawerBatch.official_result && (
              <div className="rounded-lg border border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)]/50 p-3">
                <div className="font-semibold text-[var(--sem-pass-fg)]">官方汇总结果（独立于逐题有效均值）</div>
                <div className="mt-1 tabular-nums text-[var(--text-primary)]">
                  {drawerBatch.official_result.metric}: <strong>{drawerBatch.official_result.score}</strong>{' '}
                  （分母 {drawerBatch.official_result.denominator} · 来源 {drawerBatch.official_result.source}）
                </div>
              </div>
            )}

            <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 space-y-1.5">
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">Batch ID</span>
                <span className="font-mono">{drawerBatch.batch_id}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">Scoring Protocol</span>
                <span className="font-mono">{drawerBatch.scoring_protocol}</span>
              </div>
              {drawerCell?.cell && (
                <div className="flex flex-wrap justify-between gap-x-2">
                  <span className="text-[var(--text-muted)]">来源 / 标尺</span>
                  <span className="break-all font-mono">{drawerCell.cell.source_type} / {drawerCell.cell.scale_type}</span>
                </div>
              )}
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">Runner / Harness</span>
                <span>
                  {drawerBatch.runner} / {drawerBatch.harness || '—'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">Token Semantics</span>
                <span className="font-mono">{drawerBatch.token_semantics}</span>
              </div>
            </div>

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => navigate(`/eval/batches/${drawerBatchId}`)}
                className="rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-3.5 py-2 font-semibold text-white cursor-pointer"
              >
                进入批次详情 &amp; 逐题矩阵
              </button>
              <button
                type="button"
                onClick={() =>
                  addToCompare({
                    id: drawerBatch.batch_id,
                    kind: 'batch',
                    batch_id: drawerBatch.batch_id,
                    alias: drawerBatch.batch_name,
                  })
                }
                className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3.5 py-2 font-medium text-[var(--text-primary)] hover:bg-[var(--bg-elevated)] cursor-pointer"
              >
                + 加入对比
              </button>
              {compareBasket.find((x) => x.kind === 'batch' && x.batch_id && x.batch_id !== drawerBatchId) && (
                <button
                  type="button"
                  onClick={() => {
                    const base = compareBasket.find((x) => x.kind === 'batch' && x.batch_id && x.batch_id !== drawerBatchId)!;
                    navigate(`/compare/1v1/${encodeURIComponent(base.batch_id!)}/vs/${encodeURIComponent(drawerBatchId)}`);
                  }}
                  className="rounded-lg border border-[var(--brand-primary)] bg-[var(--brand-tint)] px-3.5 py-2 font-semibold text-[var(--brand-primary)] hover:bg-[var(--brand-primary)] hover:text-white transition-colors cursor-pointer"
                >
                  查看差异题 (vs 对比基线)
                </button>
              )}
            </div>
          </div>
        ) : drawerCell?.cell ? (
          <div className="space-y-3 text-xs">
            <div className="rounded-lg border border-[var(--sem-legacy-border)] bg-[var(--sem-legacy-bg)] p-3 text-[var(--sem-legacy-fg)]">
              该条成绩来自 2026-11-20 历史全量基线总表（<code>legacy-20261120-csv</code>），无单批 Harbor 目录，可前往基准档案或三线对比查看护栏明细。
            </div>
            <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 space-y-1.5">
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">基准</span>
                <span className="font-semibold">{drawerCell.cell.raw_dataset_name}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">归一化分值 (0-100)</span>
                <span className="font-bold tabular-nums">{fmtPct(drawerCell.cell.score)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">原始标尺 / 原值</span>
                <span className="font-mono">
                  {drawerCell.cell.scale_type} / {drawerCell.cell.raw_score ?? '—'}
                </span>
              </div>
            </div>
            <Link
              to={`/benchmarks/${drawerCell.cell.benchmark_slug}`}
              className="inline-flex items-center gap-1 rounded-lg bg-[var(--brand-primary)] px-3.5 py-2 font-semibold text-white no-underline"
            >
              前往 {drawerCell.cell.benchmark_slug} 基准档案
            </Link>
          </div>
        ) : null}
      </SlideOverDrawer>
    </PageTransition>
  );
}
