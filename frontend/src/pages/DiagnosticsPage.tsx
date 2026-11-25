import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import type * as echarts from 'echarts';
import {
  Activity,
  AlertTriangle,
  Cpu,
  Eye,
  Flame,
  Gauge,
  Grid,
  ShieldAlert,
  Wrench,
} from 'lucide-react';
import { apiFetch } from '../lib/api';
import { escapeHtml, fmtPct } from '../lib/format';
import { useAppStore } from '../lib/store';
import { PageTransition, SlidingTabs } from '../components/motion/primitives';
import { EChartBox, glassTooltipConfig } from '../components/charts/EChartBox';
import { CardGridSkeleton, StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

type DiagTab = 'failures' | 'submetrics' | 'efficiency' | 'tools' | 'clues';

interface DiagBanner {
  total_trials: number;
  batch_count: number;
  scored_positive: number;
  scored_zero: number;
  unscored: number;
  fault: number;
}

interface DiagFacets {
  runners: string[];
  harnesses: string[];
  benchmarks: string[];
  dataset_releases: string[];
}

interface FailuresResp {
  banner: DiagBanner;
  facets: DiagFacets;
  total_fault_trials: number;
  holdout_trials_redacted: number;
  clusters: Array<{
    failure_kind: string;
    label: string;
    category: string;
    count: number;
    batch_count: number;
    harnesses: string[];
    sample_summary: string;
  }>;
  pivot_by_harness: Array<{ harness: string; total: number; counts: Record<string, number> }>;
  pivot_by_runner: Array<{ runner: string; total: number; counts: Record<string, number> }>;
  pivot_by_host: Array<{ host_redacted: string; total: number; counts: Record<string, number> }>;
  trials: Array<{
    batch_id: string;
    batch_name: string;
    trial_id: string;
    task_id: string;
    harness: string | null;
    runner: string;
    status: string;
    failure_kind: string;
    exception_type: string | null;
    http_status: number | null;
    failure_summary: string | null;
    host_redacted: string | null;
  }>;
}

interface SubmetricsResp {
  banner: DiagBanner;
  facets: DiagFacets;
  matrices: Array<{
    group_key: string;
    title: string;
    source_kind: string;
    submetrics: string[];
    subject_rows: Array<{
      subject: string;
      cells: Record<string, { mean_raw: number; pct: number | null; n: number } | null>;
    }>;
    task_rows: Array<{
      task_id: string;
      cells: Record<string, { mean_raw: number; pct: number | null; n: number } | null>;
    }>;
  }>;
}

interface EfficiencyResp {
  banner: DiagBanner;
  facets: DiagFacets;
  x_metric: string;
  honest_latency_notice: string;
  token_coverage: {
    total_trials: number;
    semantics_breakdown: Record<string, number>;
    reasoning_key_sources: Record<string, number>;
    cached_tokens_observed: number;
    reasoning_tokens_observed: number;
    cost_recorded_observed: number;
  };
  batch_points: Array<{
    batch_id: string;
    batch_name: string;
    model_canonical: string | null;
    harness: string | null;
    runner: string;
    score_pct: number | null;
    x_avg: number | null;
    token_semantics: string;
    on_pareto: boolean;
  }>;
  trial_points: Array<{
    task_id: string;
    model_canonical: string | null;
    harness: string | null;
    x_value: number | null;
    reward_pct: number | null;
  }>;
}

interface ToolsResp {
  banner: DiagBanner;
  facets: DiagFacets;
  coverage_guard: {
    tool_recording_trials: number;
    total_trials_in_scope: number;
    coverage_pct: number;
    included_harnesses: Record<string, number>;
    excluded_harnesses: Record<string, number>;
    note: string;
  };
  tool_ranking: Array<{
    function_name: string;
    calls: number;
    errors: number;
    error_rate_pct: number;
    trials: number;
  }>;
  by_harness: Array<{ harness: string; total_calls: number; top_tools: Record<string, number> }>;
}

interface CluesResp {
  banner: DiagBanner;
  facets: DiagFacets;
  honest_clue_policy: string;
  clues: Array<{
    code: string;
    title: string;
    formula: string;
    applicable_denominator: number;
    denominator_label: string;
    hit_count: number;
    hit_rate_pct: number;
    usage_note: string;
    trials: Array<{
      batch_id: string;
      batch_name: string;
      trial_id: string;
      task_id: string;
      status: string;
      reward: number | null;
      evidence: Record<string, unknown>;
    }>;
  }>;
}

export function DiagnosticsPage() {
  const theme = useAppStore((s) => s.theme);
  const [tab, setTab] = useState<DiagTab>('failures');
  const [runner, setRunner] = useState('');
  const [harness, setHarness] = useState('');
  const [benchmark, setBenchmark] = useState('');
  const [selectedKind, setSelectedKind] = useState('');
  const [xMetric, setXMetric] = useState('duration_sec');
  const [minRepeat, setMinRepeat] = useState(3);
  const [minConsecErr, setMinConsecErr] = useState(4);

  const filterQs = useMemo(() => {
    const qp = new URLSearchParams();
    if (runner) qp.set('runner', runner);
    if (harness) qp.set('harness', harness);
    if (benchmark) qp.set('benchmark_slug', benchmark);
    return qp.toString();
  }, [runner, harness, benchmark]);

  const failuresQ = useQuery({
    queryKey: ['diag-failures', filterQs, selectedKind],
    enabled: tab === 'failures',
    queryFn: () => {
      const qp = new URLSearchParams(filterQs);
      if (selectedKind) qp.set('failure_kind', selectedKind);
      return apiFetch<FailuresResp>(`/api/v1/diagnostics/failures?${qp.toString()}`);
    },
  });

  const submetricsQ = useQuery({
    queryKey: ['diag-submetrics', filterQs],
    enabled: tab === 'submetrics',
    queryFn: () => apiFetch<SubmetricsResp>(`/api/v1/diagnostics/submetrics?${filterQs}`),
  });

  const efficiencyQ = useQuery({
    queryKey: ['diag-efficiency', filterQs, xMetric],
    enabled: tab === 'efficiency',
    queryFn: () => {
      const qp = new URLSearchParams(filterQs);
      qp.set('x_metric', xMetric);
      return apiFetch<EfficiencyResp>(`/api/v1/diagnostics/efficiency?${qp.toString()}`);
    },
  });

  const toolsQ = useQuery({
    queryKey: ['diag-tools', filterQs],
    enabled: tab === 'tools',
    queryFn: () => apiFetch<ToolsResp>(`/api/v1/diagnostics/tools?${filterQs}`),
  });

  const cluesQ = useQuery({
    queryKey: ['diag-clues', filterQs, minRepeat, minConsecErr],
    enabled: tab === 'clues',
    queryFn: () => {
      const qp = new URLSearchParams(filterQs);
      qp.set('min_repeat', String(minRepeat));
      qp.set('min_consec_errors', String(minConsecErr));
      return apiFetch<CluesResp>(`/api/v1/diagnostics/clues?${qp.toString()}`);
    },
  });

  const activeBanner =
    failuresQ.data?.banner ||
    submetricsQ.data?.banner ||
    efficiencyQ.data?.banner ||
    toolsQ.data?.banner ||
    cluesQ.data?.banner;
  const activeFacets =
    failuresQ.data?.facets ||
    submetricsQ.data?.facets ||
    efficiencyQ.data?.facets ||
    toolsQ.data?.facets ||
    cluesQ.data?.facets;

  const failureBarOption = useMemo<echarts.EChartsCoreOption>(() => {
    const clusters = (failuresQ.data?.clusters || []).slice(0, 12);
    return {
      grid: { left: 160, right: 24, top: 18, bottom: 28 },
      tooltip: {
        ...glassTooltipConfig(theme),
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: unknown) => {
          const arr = Array.isArray(params) ? params : [params];
          const p = arr[0] as { name?: string; value?: number; dataIndex?: number };
          const idx = p?.dataIndex ?? 0;
          const c = clusters[clusters.length - 1 - idx];
          if (!c) return '';
          return `<div><strong>${escapeHtml(c.failure_kind)}</strong> (${escapeHtml(c.label)})<br/>故障试验数：<b>${c.count}</b> · 涉及批次：${c.batch_count}<br/><span style="opacity:0.75">${escapeHtml(c.sample_summary.slice(0, 140))}</span></div>`;
        },
      },
      xAxis: { type: 'value', splitLine: { lineStyle: { color: theme === 'dark' ? '#27272a' : '#e5e7eb' } } },
      yAxis: {
        type: 'category',
        data: clusters.map((c) => c.failure_kind).reverse(),
        axisLabel: { fontSize: 11, color: theme === 'dark' ? '#d4d4d8' : '#334155' },
      },
      series: [
        {
          type: 'bar',
          data: clusters.map((c) => c.count).reverse(),
          itemStyle: { color: '#ef4444', borderRadius: [0, 4, 4, 0] },
          barMaxWidth: 20,
        },
      ],
    };
  }, [failuresQ.data, theme]);

  const batchScatterPoints = useMemo(() =>
    (efficiencyQ.data?.batch_points ?? [])
      .filter((point) => Number.isFinite(point.x_avg) && Number.isFinite(point.score_pct)),
  [efficiencyQ.data]);
  const trialScatterPoints = useMemo(() =>
    (efficiencyQ.data?.trial_points ?? [])
      .filter((point) => Number.isFinite(point.x_value) && Number.isFinite(point.reward_pct)),
  [efficiencyQ.data]);

  const effScatterOption = useMemo<echarts.EChartsCoreOption>(() => {
    const batchData = batchScatterPoints.map((point) => ({
      value: [point.x_avg, point.score_pct],
      meta: point,
    }));
    const series = [
      {
        type: 'scatter',
        name: '试验样本',
        symbolSize: 6,
        itemStyle: { color: '#94a3b8', opacity: 0.45 },
        data: trialScatterPoints.map((point) => ({ value: [point.x_value, point.reward_pct], meta: point })),
      },
      {
        type: 'scatter',
        name: '批次均值',
        symbolSize: 11,
        itemStyle: { color: theme === 'dark' ? '#60a5fa' : '#2563eb' },
        data: batchData.filter(({ meta }) => !meta.on_pareto),
        z: 3,
      },
      {
        type: 'scatter',
        name: 'Pareto 前沿',
        symbol: 'diamond',
        symbolSize: 15,
        itemStyle: { color: theme === 'dark' ? '#fbbf24' : '#b45309' },
        data: batchData.filter(({ meta }) => meta.on_pareto),
        z: 4,
      },
    ].filter(({ data }) => data.length > 0);

    return {
      grid: { left: 56, right: 28, top: 54, bottom: 52 },
      legend: { top: 0, textStyle: { color: theme === 'dark' ? '#d4d4d8' : '#334155' } },
      tooltip: {
        ...glassTooltipConfig(theme),
        trigger: 'item',
        formatter: (param: unknown) => {
          type Point = EfficiencyResp['batch_points'][number] | EfficiencyResp['trial_points'][number];
          const point = (param as { data?: { meta?: Point } })?.data?.meta;
          if (!point) return '';
          const model = escapeHtml(point.model_canonical || '—');
          if ('task_id' in point) {
            return `<div><strong>${escapeHtml(point.task_id)}</strong><br/>试验 · ${model} · ${escapeHtml(point.harness || '—')}<br/>得分 <b>${fmtPct(point.reward_pct)}</b> · 横轴 <b>${point.x_value}</b></div>`;
          }
          return `<div><strong>${escapeHtml(point.batch_name)}</strong><br/>批次 · ${model} · ${escapeHtml(point.harness || point.runner)}<br/>均分 <b>${fmtPct(point.score_pct)}</b> · 横轴均值 <b>${point.x_avg}</b><br/>Token 语义 ${escapeHtml(point.token_semantics)}</div>`;
        },
      },
      xAxis: {
        type: 'value',
        name: xMetric === 'duration_sec' ? '耗时 (s)' : xMetric === 'total_tokens' ? '总 Tokens' : 'Reasoning Tokens',
        nameLocation: 'middle',
        nameGap: 28,
        splitLine: { lineStyle: { color: theme === 'dark' ? '#27272a' : '#e5e7eb' } },
      },
      yAxis: {
        type: 'value',
        name: '得分 (%)',
        min: (extent: { min: number }) => Math.min(0, extent.min),
        max: (extent: { max: number }) => Math.max(100, extent.max),
        splitLine: { lineStyle: { color: theme === 'dark' ? '#27272a' : '#e5e7eb' } },
      },
      series,
    };
  }, [batchScatterPoints, trialScatterPoints, xMetric, theme]);

  const toolBarOption = useMemo<echarts.EChartsCoreOption>(() => {
    const rows = (toolsQ.data?.tool_ranking || []).slice(0, 12);
    return {
      grid: { left: 150, right: 24, top: 18, bottom: 28 },
      tooltip: {
        ...glassTooltipConfig(theme),
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: unknown) => {
          const arr = Array.isArray(params) ? params : [params];
          const idx = (arr[0] as { dataIndex?: number })?.dataIndex ?? 0;
          const t = rows[rows.length - 1 - idx];
          if (!t) return '';
          return `<div><strong>${escapeHtml(t.function_name)}</strong><br/>总调用：<b>${t.calls}</b> · 报错：${t.errors} (${t.error_rate_pct}%)<br/>覆盖试验数：${t.trials}</div>`;
        },
      },
      xAxis: { type: 'value', splitLine: { lineStyle: { color: theme === 'dark' ? '#27272a' : '#e5e7eb' } } },
      yAxis: {
        type: 'category',
        data: rows.map((r) => r.function_name).reverse(),
        axisLabel: { fontSize: 11, color: theme === 'dark' ? '#d4d4d8' : '#334155' },
      },
      series: [
        {
          type: 'bar',
          data: rows.map((r) => r.calls).reverse(),
          itemStyle: { color: '#0ea5e9', borderRadius: [0, 4, 4, 0] },
          barMaxWidth: 20,
        },
      ],
    };
  }, [toolsQ.data, theme]);

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <Activity className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>Diagnostics 深度诊断与客观轨迹线索</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            故障分类聚类 · WildClaw/VLM 多子指标矩阵 · Token 包含语义与效率 · ATIF 工具覆盖护栏 · 4 项纯客观线索（零主观定性）
          </p>
        </div>

        <SlidingTabs
          layoutId="diag-panel-tabs"
          size="sm"
          value={tab}
          onChange={(v) => setTab(v as DiagTab)}
          options={[
            { id: 'failures', label: '故障聚类', icon: <Flame className="w-3.5 h-3.5" /> },
            { id: 'submetrics', label: '子指标热图', icon: <Grid className="w-3.5 h-3.5" /> },
            { id: 'efficiency', label: '效率与 Token', icon: <Gauge className="w-3.5 h-3.5" /> },
            { id: 'tools', label: '工具画像', icon: <Wrench className="w-3.5 h-3.5" /> },
            { id: 'clues', label: '客观线索', icon: <Eye className="w-3.5 h-3.5" /> },
          ]}
        />
      </div>

      {/* Global Scope Banner + Filters */}
      <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5 flex flex-wrap items-center justify-between gap-3 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="过滤运行器"
            value={runner}
            onChange={(e) => setRunner(e.target.value)}
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1 text-xs"
          >
            <option value="">全部 Runner</option>
            {(activeFacets?.runners || []).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <select
            aria-label="过滤 Harness"
            value={harness}
            onChange={(e) => setHarness(e.target.value)}
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1 text-xs"
          >
            <option value="">全部 Harness</option>
            {(activeFacets?.harnesses || []).map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>
          <select
            aria-label="过滤 Benchmark"
            value={benchmark}
            onChange={(e) => setBenchmark(e.target.value)}
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1 text-xs"
          >
            <option value="">全部 Benchmark</option>
            {(activeFacets?.benchmarks || []).map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
        </div>

        {activeBanner && (
          <div className="flex flex-wrap items-center gap-2 tabular-nums text-[11px]">
            <span className="rounded px-2 py-0.5 bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
              批次 {activeBanner.batch_count} · 试验 {activeBanner.total_trials}
            </span>
            <span className="rounded px-2 py-0.5 bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]">
              正分 {activeBanner.scored_positive}
            </span>
            <span className="rounded px-2 py-0.5 bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]">
              有效 0 分 {activeBanner.scored_zero}
            </span>
            <span className="rounded px-2 py-0.5 bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)]">
              未评分 {activeBanner.unscored}
            </span>
            <span className="rounded px-2 py-0.5 bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]">
              故障 {activeBanner.fault}
            </span>
          </div>
        )}
      </div>

      {/* Panel 1: Failures */}
      {tab === 'failures' && (
        failuresQ.isLoading ? (
          <CardGridSkeleton count={3} />
        ) : failuresQ.isError ? (
          <StateNoticeCard kind="error" title="故障诊断加载失败" description={(failuresQ.error as Error).message} onRetry={() => failuresQ.refetch()} />
        ) : failuresQ.data ? (
          <div className="space-y-4">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
                <div className="text-xs font-bold text-[var(--text-primary)] mb-2">
                  故障指纹 Top 12（共 {failuresQ.data.total_fault_trials} 条故障试验）
                </div>
                {failuresQ.data.clusters.length === 0 ? (
                  <StateNoticeCard kind="empty" title="当前筛选范围内无故障试验" />
                ) : (
                  <EChartBox option={failureBarOption} height={300} exportName="diagnostics-failures" />
                )}
              </div>

              <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3 text-xs">
                <div className="font-bold text-[var(--text-primary)]">按故障分类点击过滤（含 VLMEvalKit / Agent 协议错误）</div>
                <div className="flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    onClick={() => setSelectedKind('')}
                    className={`rounded-full px-2.5 py-1 text-[11px] border cursor-pointer ${
                      !selectedKind
                        ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                        : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                    }`}
                  >
                    全部 ({failuresQ.data.total_fault_trials})
                  </button>
                  {failuresQ.data.clusters.map((c) => (
                    <button
                      key={c.failure_kind}
                      type="button"
                      onClick={() => setSelectedKind(selectedKind === c.failure_kind ? '' : c.failure_kind)}
                      className={`rounded-full px-2.5 py-1 text-[11px] border cursor-pointer ${
                        selectedKind === c.failure_kind
                          ? 'border-[var(--sem-err-fg)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)] font-semibold'
                          : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                      }`}
                    >
                      {c.failure_kind} ({c.count})
                    </button>
                  ))}
                </div>
                {failuresQ.data.holdout_trials_redacted > 0 && (
                  <div className="rounded-lg border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] px-3 py-2 text-[11px] text-[var(--text-secondary)]">
                    已按 dataset:holdout_read 权限隐藏 {failuresQ.data.holdout_trials_redacted} 条留出集故障明细。
                  </div>
                )}
              </div>
            </div>

            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
              <div className="px-4 py-2.5 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-xs font-bold">
                故障试验样本（点击直达 Trial 显微镜）
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                      <th className="py-2 px-3">故障指纹</th>
                      <th className="py-2 px-3">批次 / Harness</th>
                      <th className="py-2 px-3">Task ID</th>
                      <th className="py-2 px-3">脱敏摘要</th>
                      <th className="py-2 px-3 text-right">定位</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {failuresQ.data.trials.slice(0, 60).map((t) => (
                      <tr key={`${t.batch_id}-${t.trial_id}`} className="hover:bg-[var(--bg-elevated)]/60">
                        <td className="py-2 px-3 font-mono text-[var(--sem-err-fg)]">
                          {t.failure_kind}
                          {t.http_status ? ` (HTTP ${t.http_status})` : ''}
                        </td>
                        <td className="py-2 px-3">
                          <div className="font-medium truncate max-w-[220px]">{t.batch_name}</div>
                          <div className="text-[10px] text-[var(--text-muted)]">{t.harness || t.runner}</div>
                        </td>
                        <td className="py-2 px-3 font-mono">{t.task_id}</td>
                        <td className="py-2 px-3 text-[var(--text-secondary)] max-w-md truncate">
                          {t.failure_summary || t.exception_type || '—'}
                        </td>
                        <td className="py-2 px-3 text-right">
                          <Link
                            to={`/eval/batches/${encodeURIComponent(t.batch_id)}/trials/${encodeURIComponent(t.trial_id)}`}
                            className="text-[var(--brand-primary)] no-underline hover:underline"
                          >
                            显微镜
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        ) : null
      )}

      {/* Panel 2: Submetrics */}
      {tab === 'submetrics' && (
        submetricsQ.isLoading ? (
          <TableSkeleton rows={6} cols={5} />
        ) : submetricsQ.isError ? (
          <StateNoticeCard kind="error" title="子指标矩阵加载失败" description={(submetricsQ.error as Error).message} />
        ) : submetricsQ.data && submetricsQ.data.matrices.length > 0 ? (
          <div className="space-y-4">
            {submetricsQ.data.matrices.map((mat) => (
              <div key={mat.group_key} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
                <div className="px-4 py-2.5 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex items-center justify-between text-xs">
                  <span className="font-bold text-[var(--text-primary)]">{mat.title}</span>
                  <span className="text-[11px] text-[var(--text-muted)]">{mat.submetrics.length} 项子指标</span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left border-collapse text-xs">
                    <thead>
                      <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                        <th className="py-2 px-3 sticky left-0 bg-[var(--bg-surface)]">对比对象 / 批次</th>
                        {mat.submetrics.map((sm) => (
                          <th key={sm} className="py-2 px-2.5 font-mono text-[11px] whitespace-nowrap">
                            {sm}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--border-subtle)] tabular-nums">
                      {mat.subject_rows.map((sr) => (
                        <tr key={sr.subject}>
                          <td className="py-2 px-3 font-medium sticky left-0 bg-[var(--bg-surface)] whitespace-nowrap">
                            {sr.subject}
                          </td>
                          {mat.submetrics.map((sm) => {
                            const c = sr.cells[sm];
                            return (
                              <td key={sm} className="py-2 px-2.5 font-mono text-[11px]">
                                {c ? (c.pct !== null ? `${c.pct.toFixed(1)}%` : c.mean_raw) : '—'}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <StateNoticeCard kind="empty" title="当前筛选范围内暂无多子指标或学科分项数据" />
        )
      )}

      {/* Panel 3: Efficiency & Token Semantics */}
      {tab === 'efficiency' && (
        efficiencyQ.isLoading ? (
          <CardGridSkeleton count={3} />
        ) : efficiencyQ.isError ? (
          <StateNoticeCard kind="error" title="效率诊断加载失败" description={(efficiencyQ.error as Error).message} />
        ) : efficiencyQ.data ? (
          <div className="space-y-4">
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 text-xs space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="font-bold text-[var(--text-primary)]">效率散点图（严格区分耗时、Token 包含语义与原生已记录成本）</div>
                <div className="flex flex-wrap items-center gap-1.5">
                  {[
                    { id: 'duration_sec', label: '均耗时 (s)' },
                    { id: 'total_tokens', label: '均总 Tokens' },
                    { id: 'reasoning_tokens', label: '均 Reasoning Tokens' },
                  ].map((m) => (
                    <button
                      key={m.id}
                      type="button"
                      onClick={() => setXMetric(m.id)}
                      aria-pressed={xMetric === m.id}
                      className={`rounded-lg px-2.5 py-1 text-[11px] border cursor-pointer ${
                        xMetric === m.id
                          ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                          : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                      }`}
                    >
                      {m.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="rounded-lg bg-[var(--bg-elevated)] px-3 py-2 text-[11px] text-[var(--text-secondary)]">
                {efficiencyQ.data.honest_latency_notice}
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] tabular-nums">
                <div className="rounded-lg border border-[var(--border-subtle)] p-2.5">
                  <div className="text-[var(--text-muted)]">Reasoning 观测试验</div>
                  <div className="mt-1 font-bold text-sm">{efficiencyQ.data.token_coverage.reasoning_tokens_observed.toLocaleString()}</div>
                </div>
                <div className="rounded-lg border border-[var(--border-subtle)] p-2.5">
                  <div className="text-[var(--text-muted)]">Cached Token 观测</div>
                  <div className="mt-1 font-bold text-sm">{efficiencyQ.data.token_coverage.cached_tokens_observed.toLocaleString()}</div>
                </div>
                <div className="rounded-lg border border-[var(--border-subtle)] p-2.5">
                  <div className="text-[var(--text-muted)]">Token 语义分类</div>
                  <div className="mt-1 font-mono text-[10px]">
                    {Object.entries(efficiencyQ.data.token_coverage.semantics_breakdown)
                      .map(([k, v]) => `${k}:${v}`)
                      .join(' · ') || '—'}
                  </div>
                </div>
                <div className="rounded-lg border border-[var(--border-subtle)] p-2.5">
                  <div className="text-[var(--text-muted)]">Reasoning 键名双键</div>
                  <div className="mt-1 font-mono text-[10px]">
                    {Object.entries(efficiencyQ.data.token_coverage.reasoning_key_sources)
                      .map(([k, v]) => `${k}:${v}`)
                      .join(' · ') || '—'}
                  </div>
                </div>
              </div>
              {batchScatterPoints.length || trialScatterPoints.length ? (
                <section aria-label="效率散点图">
                  <div className="mb-2 text-[11px] text-[var(--text-secondary)]">
                    批次可绘制 {batchScatterPoints.length} / {efficiencyQ.data.banner.batch_count} · 试验样本 {trialScatterPoints.length}
                  </div>
                  <EChartBox option={effScatterOption} height={340} exportName="diagnostics-efficiency" />
                </section>
              ) : (
                <StateNoticeCard
                  title="当前指标暂无可绘制的效率数据"
                  description={`当前筛选的 ${efficiencyQ.data.banner.batch_count} 个批次中，暂无同时具备该指标观测与有效得分的批次。可切换横轴指标或调整筛选。`}
                />
              )}
            </div>
          </div>
        ) : null
      )}

      {/* Panel 4: Tools Profile with Coverage Guard */}
      {tab === 'tools' && (
        toolsQ.isLoading ? (
          <CardGridSkeleton count={3} />
        ) : toolsQ.isError ? (
          <StateNoticeCard kind="error" title="工具画像加载失败" description={(toolsQ.error as Error).message} />
        ) : toolsQ.data ? (
          <div className="space-y-4">
            <div className="rounded-xl border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] p-3.5 text-xs flex items-start gap-2.5">
              <AlertTriangle className="w-4 h-4 text-[var(--sem-zero-fg)] shrink-0 mt-0.5" />
              <div>
                <div className="font-semibold text-[var(--text-primary)]">
                  工具覆盖分母护栏：{toolsQ.data.coverage_guard.tool_recording_trials} / {toolsQ.data.coverage_guard.total_trials_in_scope} 试验含 ATIF 工具事件（{toolsQ.data.coverage_guard.coverage_pct}%）
                </div>
                <div className="mt-1 text-[var(--text-secondary)]">{toolsQ.data.coverage_guard.note}</div>
              </div>
            </div>
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="text-xs font-bold mb-2">工具调用频次与错误率排行</div>
              {toolsQ.data.tool_ranking.length === 0 ? (
                <StateNoticeCard kind="empty" title="当前范围无 ATIF 工具事件" />
              ) : (
                <EChartBox option={toolBarOption} height={320} exportName="diagnostics-tools" />
              )}
            </div>
          </div>
        ) : null
      )}

      {/* Panel 5: 4 Objective Trajectory Clues */}
      {tab === 'clues' && (
        cluesQ.isLoading ? (
          <CardGridSkeleton count={4} />
        ) : cluesQ.isError ? (
          <StateNoticeCard kind="error" title="客观线索加载失败" description={(cluesQ.error as Error).message} />
        ) : cluesQ.data ? (
          <div className="space-y-4">
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5 flex flex-wrap items-center justify-between gap-3 text-xs">
              <span className="text-[var(--text-secondary)] inline-flex items-center gap-1.5">
                <ShieldAlert className="w-4 h-4 text-[var(--brand-primary)]" />
                {cluesQ.data.honest_clue_policy}
              </span>
              <div className="flex items-center gap-3 text-[11px]">
                <label className="inline-flex items-center gap-1">
                  连续同参数阈值:
                  <input
                    type="number"
                    min={2}
                    max={20}
                    value={minRepeat}
                    onChange={(e) => setMinRepeat(Math.max(2, Number(e.target.value) || 3))}
                    className="w-14 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-1.5 py-0.5 font-mono"
                  />
                </label>
                <label className="inline-flex items-center gap-1">
                  连续报错阈值:
                  <input
                    type="number"
                    min={2}
                    max={20}
                    value={minConsecErr}
                    onChange={(e) => setMinConsecErr(Math.max(2, Number(e.target.value) || 4))}
                    className="w-14 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-1.5 py-0.5 font-mono"
                  />
                </label>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {cluesQ.data.clues.map((card) => (
                <div key={card.code} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-2.5 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-bold text-[var(--text-primary)]">{card.title}</div>
                    <span className="rounded-full px-2 py-0.5 font-mono text-[11px] bg-[var(--brand-tint)] text-[var(--brand-primary)] tabular-nums">
                      命中 {card.hit_count} / 适用 {card.applicable_denominator} ({card.hit_rate_pct.toFixed(1)}%)
                    </span>
                  </div>
                  <div className="text-[var(--text-secondary)] font-mono text-[11px]">{card.formula}</div>
                  <div className="text-[11px] text-[var(--text-muted)]">
                    分母：{card.denominator_label} · {card.usage_note}
                  </div>
                  {(card.trials || []).length > 0 && (
                    <div className="pt-2 border-t border-[var(--border-subtle)] space-y-1">
                      {card.trials.slice(0, 5).map((s) => (
                        <div key={`${s.batch_id}-${s.trial_id}`} className="flex items-center justify-between gap-2 text-[11px]">
                          <span className="font-mono truncate">
                            {s.task_id} · <span className="text-[var(--text-muted)]">{s.batch_name}</span>
                          </span>
                          <Link
                            to={`/eval/batches/${encodeURIComponent(s.batch_id)}/trials/${encodeURIComponent(s.trial_id)}`}
                            className="text-[var(--brand-primary)] no-underline hover:underline shrink-0"
                          >
                            显微镜
                          </Link>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ) : null
      )}
    </PageTransition>
  );
}
