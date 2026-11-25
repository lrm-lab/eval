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
  Columns3,
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
import { fmtDuration, fmtPct, runScopeLabel, stableColorForKey } from '../lib/format';
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

type ViewMode = 'table' | 'trend' | 'bar' | 'scatter';

const filterFieldClass = 'h-9 w-full min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-xs text-[var(--text-primary)] hover:border-[var(--border-strong)]';
const toolbarButtonClass = 'inline-flex h-9 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg border px-2.5 text-xs font-medium cursor-pointer motion-safe:transition-colors';
const defaultButtonClass = 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)]';

const columnHelper = createColumnHelper<LeaderboardItem>();

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

  // URL-synced state (100% real Harbor/lrm-eval filters only — zero fake commercial fields!)
  const activeTrack = params.track || searchParams.get('track') || 'all';
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
  const [colsMenuOpen, setColsMenuOpen] = useState(false);
  const [copiedLink, setCopiedLink] = useState(false);
  const [tableScrolled, setTableScrolled] = useState(false);

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

  const items = lbQ.data?.items || [];
  const availableFilters = lbQ.data?.available_filters;

  // Collect all unique benchmark cell keys present in current filtered result
  const benchmarkKeys = useMemo(() => {
    const map = new Map<string, { key: string; label: string; proto: string; scope: string; count: number; maxScore: number }>();
    for (const row of items) {
      for (const [k, cell] of Object.entries(row.benchmarks)) {
        const cur = map.get(k);
        const sc = cell.score ?? -1;
        if (!cur) {
          map.set(k, {
            key: k,
            label: cell.raw_dataset_name || cell.benchmark_slug,
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
    return Array.from(map.values())
      .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
      .slice(0, columnPreset === 'diagnostics' ? 14 : 8);
  }, [items, columnPreset]);

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
          const firstBatchId = Object.values(r.benchmarks).find((c) => c.batch_id)?.batch_id || null;
          const inBasket = compareBasket.some((x) => x.id === (firstBatchId || r.subject_id));
          return (
            <div className="flex items-center justify-between gap-2 min-w-[230px] max-w-[280px]">
              <div className="min-w-0">
                <div className="flex items-center gap-1.5">
                  {r.rank <= 3 ? (
                    <span
                      className="inline-flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-bold tabular-nums shrink-0 text-white"
                      style={{
                        backgroundColor:
                          r.rank === 1
                            ? 'var(--rank-gold)'
                            : r.rank === 2
                              ? 'var(--rank-silver)'
                              : 'var(--rank-bronze)',
                      }}
                    >
                      {r.rank}
                    </span>
                  ) : (
                    <span className="text-[11px] font-mono text-[var(--text-muted)] w-5 text-center shrink-0">
                      #{r.rank}
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
                <div className="mt-0.5 pl-6 flex flex-wrap items-center gap-1 text-[10px] text-[var(--text-muted)]">
                  <Link
                    to={`/harnesses/${encodeURIComponent(r.harness)}`}
                    className="rounded px-1 py-0.2 bg-[var(--bg-elevated)] text-[var(--text-secondary)] hover:text-[var(--brand-primary)] no-underline"
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

    if (showCol('comparative_macro_avg')) {
      cols.push(
        columnHelper.accessor('comparative_macro_avg', {
          id: 'comparative_macro_avg',
          header: () => (
            <span className="inline-flex items-center gap-1">
              <span>可比宏观均分</span>
              <MethodologyPopover
                title="Comparative Macro Avg（可比宏观均分）"
                summary="仅对当前筛选集合中所有对象在相同协议与范围内共同有分的基准求等权均值，严格杜绝缺测偏置；共同基准为 0 时显示 —。"
                anchor="#macro-avg"
              />
            </span>
          ),
          cell: (info) => {
            const r = info.row.original;
            return (
              <CompositeScoreCell
                score={r.comparative_macro_avg}
                coverage={r.comparative_bench_count > 0 ? `${r.comparative_bench_count} common` : undefined}
              />
            );
          },
        })
      );
    }

    if (showCol('self_coverage_avg')) {
      cols.push(
        columnHelper.accessor('self_coverage_avg', {
          id: 'self_coverage_avg',
          header: () => (
            <span className="inline-flex items-center gap-1">
              <span>自覆盖参考均分</span>
              <MethodologyPopover
                title="Self-Coverage Avg（自覆盖参考均分）"
                summary="该对象自身全部已测基准的均值，仅作覆盖参考，不可替代共同基准交集排序。"
                anchor="#macro-avg"
              />
            </span>
          ),
          cell: (info) => {
            const r = info.row.original;
            return <CompositeScoreCell score={r.self_coverage_avg} coverage={`${r.self_bench_count} bench`} />;
          },
        })
      );
    }

    if (showCol('avg_duration_sec')) {
      cols.push(
        columnHelper.accessor('avg_duration_sec', {
          id: 'avg_duration_sec',
          header: () => <span>平均总耗时</span>,
          cell: (info) => <span className="text-xs tabular-nums text-[var(--text-secondary)]">{fmtDuration(info.getValue())}</span>,
        })
      );
    }

    if (showCol('tokens')) {
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

    for (const bk of benchmarkKeys) {
      cols.push(
        columnHelper.display({
          id: `bm_${bk.key}`,
          header: () => (
            <div className="min-w-[100px]">
              <div className="truncate max-w-[130px]" title={`${bk.label} (${bk.proto} · ${bk.scope})`}>
                {bk.label}
              </div>
              <div className="text-[10px] font-normal text-[var(--text-muted)] truncate max-w-[130px]">
                {runScopeLabel(bk.scope)}
              </div>
            </div>
          ),
          cell: (info) => {
            const r = info.row.original;
            const cell = r.benchmarks[bk.key];
            if (!cell) return <CompositeScoreCell score={null} />;
            const isBest = cell.score !== null && Math.abs(cell.score - bk.maxScore) < 1e-6;
            const cov =
              cell.scored !== undefined && cell.planned !== undefined && cell.planned > 0
                ? `${cell.scored}/${cell.planned}`
                : undefined;
            return (
              <CompositeScoreCell
                score={cell.score}
                isBest={isBest}
                coverage={cov}
                subtitle={cell.scoring_protocol === 'legacy-20261120-csv' ? '2026-11-20' : undefined}
                onClick={() => {
                  setDrawerBatchId(cell.batch_id);
                  setDrawerCell({ item: r, cell });
                }}
                breakdown={[
                  { label: '协议', value: cell.scoring_protocol },
                  { label: '范围', value: runScopeLabel(cell.run_scope) },
                  { label: '来源', value: cell.source_type },
                  { label: '标尺', value: cell.scale_type },
                ]}
              />
            );
          },
        })
      );
    }

    return cols;
  }, [benchmarkKeys, columnPreset, customColumns, compareBasket, addToCompare]);

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
    Object.entries(item.benchmarks).map(([key, cell]) => ({
      id: `${item.subject_id}:${key}`,
      label: `${item.model_canonical} · ${item.harness} · ${cell.raw_dataset_name || cell.benchmark_slug}`,
      batchId: cell.batch_id,
      benchmark: cell.benchmark_slug,
    }))
  ), [visibleRows]);

  const trackTabs = useMemo(() => {
    const base = [{ id: 'all', label: '全部轨道' }];
    const dynamic = (treeQ.data?.tracks || []).map((t) => ({
      id: t.track_slug,
      label: t.display_name,
      badge: t.benchmarks.length,
    }));
    return [...base, ...dynamic];
  }, [treeQ.data]);

  const copyShareLink = () => {
    navigator.clipboard?.writeText(window.location.href).catch(() => {});
    setCopiedLink(true);
    pushToast('success', '当前排行榜筛选链接已复制');
    window.setTimeout(() => setCopiedLink(false), 1600);
  };

  return (
    <PageTransition className="max-w-[1600px] mx-auto px-5 py-6 space-y-5">
      {/* Header & Track Switcher */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="m-0 text-xl font-bold tracking-tight text-[var(--text-primary)]">综合主榜 &amp; 效率前沿</h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            按协议与范围严格隔离 · 缺测严禁填 0 · 单元格或散点点击均在右侧 L2 抽屉展开以保留当前上下文
          </p>
        </div>

        <SlidingTabs
          layoutId="lb-view-switcher"
          className="max-w-full max-sm:grid max-sm:w-full max-sm:grid-cols-2 max-sm:items-stretch max-sm:[&>button]:min-w-0 max-sm:[&>button]:justify-center"
          size="sm"
          value={viewMode}
          onChange={(v) => updateParam('view', v)}
          options={[
            { id: 'table', label: 'Table', icon: <Table2 className="w-3.5 h-3.5" /> },
            { id: 'trend', label: 'Trend', icon: <LineChart className="w-3.5 h-3.5" /> },
            { id: 'bar', label: 'Grouped Bar', icon: <BarChart3 className="w-3.5 h-3.5" /> },
            { id: 'scatter', label: 'Efficiency Scatter', icon: <ScatterChart className="w-3.5 h-3.5" /> },
          ]}
        />
      </div>

      {/* Dynamic Taxonomy Track Tabs */}
      <div className="overflow-x-auto pb-1">
        <SlidingTabs
          layoutId="lb-track-tabs"
          className="whitespace-nowrap"
          size="sm"
          value={activeTrack}
          onChange={(t) => {
            if (params.track) navigate(`/leaderboard/${t}?${searchParams.toString()}`);
            else updateParam('track', t === 'all' ? null : t);
          }}
          options={trackTabs}
        />
      </div>

      <div role="search" aria-label="主榜筛选" className="@container rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3">
        <div className="grid grid-cols-2 gap-2 @min-[31rem]:grid-cols-4 @min-[49rem]:grid-cols-[minmax(0,1fr)_repeat(4,minmax(0,9rem))]">
          <div className="relative col-span-full @min-[49rem]:col-span-1">
            <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[var(--text-muted)]" />
            <input
              type="search"
              aria-label="搜索模型族或模型名"
              value={modelFamily}
              onChange={(e) => updateParam('q', e.target.value || null)}
              placeholder="搜索模型族 / 模型名..."
              className={`${filterFieldClass} pl-9 pr-2.5`}
            />
          </div>

          <select
            aria-label="筛选评估范围"
            value={runScope}
            onChange={(e) => updateParam('run_scope', e.target.value || null)}
            className={`${filterFieldClass} cursor-pointer truncate px-2.5`}
          >
            <option value="">范围: 全部</option>
            <option value="full_split">Formal Full</option>
            <option value="subset">Subset</option>
            <option value="smoke">Smoke</option>
            <option value="reference">Reference</option>
          </select>

          <select
            aria-label="筛选 Harness"
            value={harness}
            onChange={(e) => updateParam('harness', e.target.value || null)}
            className={`${filterFieldClass} cursor-pointer truncate px-2.5`}
          >
            <option value="">Harness: 全部</option>
            {(availableFilters?.harnesses || []).map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>

          <select
            aria-label="筛选 Runner"
            value={runner}
            onChange={(e) => updateParam('runner', e.target.value || null)}
            className={`${filterFieldClass} cursor-pointer truncate px-2.5`}
          >
            <option value="">Runner: 全部</option>
            {(availableFilters?.runners || []).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>

          <select
            aria-label="筛选协议"
            value={protocol}
            onChange={(e) => updateParam('protocol', e.target.value || null)}
            className={`${filterFieldClass} cursor-pointer truncate px-2.5`}
          >
            <option value="">协议: 全部</option>
            {(availableFilters?.protocols || []).map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-x-5 gap-y-2 border-t border-[var(--border-subtle)] pt-3">
          <div role="group" aria-label="附加筛选" className="flex max-w-full flex-wrap items-center gap-2">
            {[
              { key: 'include_legacy', label: '含 2026-11-20 基线', active: includeLegacy, next: includeLegacy ? 'false' : null },
              { key: 'trained_only', label: '仅 Trained CK', active: trainedOnly, next: trainedOnly ? null : 'true' },
              { key: 'has_atif', label: '含 ATIF', active: hasAtif, next: hasAtif ? null : 'true' },
              { key: 'has_gateway', label: '含网关抓包', active: hasGateway, next: hasGateway ? null : 'true' },
            ].map(({ key, label, active, next }) => (
              <button
                key={key}
                type="button"
                aria-pressed={active}
                onClick={() => updateParam(key, next)}
                className={`${toolbarButtonClass} ${defaultButtonClass} aria-pressed:border-[var(--brand-primary)] aria-pressed:bg-[var(--brand-tint)] aria-pressed:text-[var(--text-primary)]`}
              >
                <span aria-hidden="true" className={`grid size-3.5 place-items-center rounded-sm border ${
                  active ? 'border-[var(--brand-primary)] text-[var(--brand-primary)]' : 'border-[var(--border-strong)]'
                }`}>
                  {active && <Check className="size-3" />}
                </span>
                {label}
              </button>
            ))}
          </div>

          <div role="group" aria-label="主榜操作" className="relative flex max-w-full flex-wrap items-center gap-2 @min-[31rem]:ml-auto">
            {/* Column Presets Menu */}
            <div>
              <button
                type="button"
                aria-expanded={colsMenuOpen}
                onClick={() => setColsMenuOpen((v) => !v)}
                className={`${toolbarButtonClass} ${defaultButtonClass}`}
              >
                <Columns3 className="w-3.5 h-3.5" />
                <span>Columns</span>
              </button>
              {colsMenuOpen && (
                <div
                  onMouseLeave={() => setColsMenuOpen(false)}
                  className="lrm-glass absolute start-0 top-full z-40 mt-1 w-56 max-w-full rounded-xl p-2.5 text-xs space-y-2"
                >
                  <div className="font-semibold text-[var(--text-primary)]">列视图预设</div>
                  {(
                    [
                      { id: 'overview', label: '核心总览 (默认 L0)' },
                      { id: 'efficiency', label: '效率对比 (耗时 + Token)' },
                      { id: 'diagnostics', label: '完整诊断 (全列展开)' },
                      { id: 'custom', label: '自定义勾选' },
                    ] as Array<{ id: ColumnPreset; label: string }>
                  ).map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      onClick={() => setColumnPreset(p.id)}
                      className={`w-full text-left rounded-md px-2 py-1.5 cursor-pointer ${
                        columnPreset === p.id
                          ? 'bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                          : 'text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)]'
                      }`}
                    >
                      {p.label}
                    </button>
                  ))}
                  {columnPreset === 'custom' && (
                    <div className="pt-2 border-t border-[var(--border-subtle)] space-y-1">
                      {[
                        { id: 'comparative_macro_avg', label: '可比宏观均分' },
                        { id: 'self_coverage_avg', label: '自覆盖参考均分' },
                        { id: 'avg_duration_sec', label: '平均总耗时' },
                        { id: 'tokens', label: 'Token 构成' },
                      ].map((c) => {
                        const checked = customColumns.includes(c.id);
                        return (
                          <label key={c.id} className="flex items-center gap-2 text-[11px] cursor-pointer">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() =>
                                setCustomColumns(
                                  checked ? customColumns.filter((x) => x !== c.id) : [...customColumns, c.id]
                                )
                              }
                            />
                            <span>{c.label}</span>
                          </label>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
            </div>

            <button
              type="button"
              onClick={() => setDensity(density === 'compact' ? 'comfortable' : 'compact')}
              title="切换表格密度"
              className={`${toolbarButtonClass} ${defaultButtonClass}`}
            >
              <SlidersHorizontal className="w-3.5 h-3.5" />
              <span>{density === 'compact' ? '紧凑' : '舒适'}</span>
            </button>

            <button
              type="button"
              onClick={copyShareLink}
              className={`${toolbarButtonClass} ${defaultButtonClass}`}
            >
              {copiedLink ? <Check className="w-3.5 h-3.5 text-[var(--sem-pass-fg)]" /> : <Copy className="w-3.5 h-3.5" />}
              <span>Copy link</span>
            </button>

            <button
              type="button"
              onClick={() => navigate('/eval/compare')}
              className={`${toolbarButtonClass} border-transparent bg-[color-mix(in_srgb,var(--brand-primary)_80%,black)] text-white hover:bg-[color-mix(in_srgb,var(--brand-hover)_75%,black)]`}
            >
              <GitCompareArrows className="w-3.5 h-3.5" />
              <span>Compare ({compareBasket.length})</span>
            </button>
          </div>
        </div>
      </div>

      <PublicBaselinePanel
        candidates={publicCandidates}
        filterBenchmark
        emptyMessage={lbQ.isLoading ? '正在读取当前主榜批次…' : '当前主榜筛选没有该基准的站内成绩。'}
      />

      {/* Main Content Area: 4 Contextual Views */}
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
        <StateNoticeCard kind="empty" title="当前筛选条件下无匹配排行对象" description="可尝试放宽协议、范围或清除模型搜索关键字。" />
      ) : viewMode === 'table' ? (
        <div
          data-scrolled={tableScrolled ? 'true' : 'false'}
          onScroll={(e) => setTableScrolled(e.currentTarget.scrollLeft > 4)}
          className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto shadow-sm"
        >
          <table className="w-full text-left border-collapse">
            <thead>
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id} className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-xs text-[var(--text-secondary)]">
                  {hg.headers.map((header, idx) => (
                    <th
                      key={header.id}
                      onClick={header.column.getCanSort() ? header.column.getToggleSortingHandler() : undefined}
                      className={`py-2.5 px-3 font-semibold whitespace-nowrap ${
                        idx === 0
                          ? 'sticky left-0 z-20 bg-[var(--bg-elevated)] shadow-[2px_0_6px_rgba(15,23,42,0.06)]'
                          : ''
                      } ${header.column.getCanSort() ? 'cursor-pointer select-none hover:text-[var(--text-primary)]' : ''}`}
                    >
                      <div className="inline-flex items-center gap-1">
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {header.column.getCanSort() && <ArrowUpDown className="w-3 h-3 opacity-50" />}
                      </div>
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {table.getRowModel().rows.map((row) => (
                <tr key={row.id} className="hover:bg-[var(--bg-elevated)]/65 transition-colors">
                  {row.getVisibleCells().map((cell, idx) => (
                    <td
                      key={cell.id}
                      className={`${density === 'compact' ? 'py-1.5 px-2.5' : 'py-2.5 px-3'} ${
                        idx === 0
                          ? 'sticky left-0 z-10 bg-[var(--bg-surface)] shadow-[2px_0_6px_rgba(15,23,42,0.05)]'
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
      ) : (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 shadow-sm">
          <div className="mb-2 text-xs text-[var(--text-secondary)]">
            展示当前筛选前 {Math.min(8, items.length)} 个对象在主榜关键基准上的{viewMode === 'trend' ? '跨基准趋势折线' : '分组柱状对比'}（缺失点自动断线/留空，严禁填 0）。
          </div>
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
            ? `${drawerCell.cell.scoring_protocol} · ${runScopeLabel(drawerCell.cell.run_scope)}`
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
        ) : drawerBatchId && drawerBatchQ.data ? (
          <div className="space-y-4 text-xs">
            <div className="grid grid-cols-2 gap-3">
              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
                <div className="text-[var(--text-muted)]">有效均值 (valid_mean)</div>
                <div className="mt-1 text-lg font-bold tabular-nums text-[var(--text-primary)]">
                  {drawerBatchQ.data.valid_mean !== null && drawerBatchQ.data.valid_mean !== undefined
                    ? fmtPct(drawerBatchQ.data.valid_mean <= 1 ? drawerBatchQ.data.valid_mean * 100 : drawerBatchQ.data.valid_mean)
                    : '—'}
                </div>
              </div>
              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
                <div className="text-[var(--text-muted)]">覆盖率 (scored / planned)</div>
                <div className="mt-1 text-lg font-bold tabular-nums text-[var(--text-primary)]">
                  {drawerBatchQ.data.scored} / {drawerBatchQ.data.planned || drawerBatchQ.data.recorded}
                </div>
              </div>
            </div>

            {drawerBatchQ.data.official_result && (
              <div className="rounded-lg border border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)]/50 p-3">
                <div className="font-semibold text-[var(--sem-pass-fg)]">官方汇总结果（独立于逐题有效均值）</div>
                <div className="mt-1 tabular-nums text-[var(--text-primary)]">
                  {drawerBatchQ.data.official_result.metric}: <strong>{drawerBatchQ.data.official_result.score}</strong>{' '}
                  （分母 {drawerBatchQ.data.official_result.denominator} · 来源 {drawerBatchQ.data.official_result.source}）
                </div>
              </div>
            )}

            <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 space-y-1.5">
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">Batch ID</span>
                <span className="font-mono">{drawerBatchQ.data.batch_id}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">Scoring Protocol</span>
                <span className="font-mono">{drawerBatchQ.data.scoring_protocol}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">Runner / Harness</span>
                <span>
                  {drawerBatchQ.data.runner} / {drawerBatchQ.data.harness || '—'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">Token Semantics</span>
                <span className="font-mono">{drawerBatchQ.data.token_semantics}</span>
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
                    id: drawerBatchQ.data!.batch_id,
                    kind: 'batch',
                    batch_id: drawerBatchQ.data!.batch_id,
                    alias: drawerBatchQ.data!.batch_name,
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
