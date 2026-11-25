import React, { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, GitCompareArrows, Plus, Sparkles, X } from 'lucide-react';
import type * as echarts from 'echarts';
import { apiFetch } from '../../lib/api';
import { escapeHtml, fmtDuration, fmtPct, fmtTokens, stableColorForKey } from '../../lib/format';
import { useAppStore } from '../../lib/store';
import type { EfficiencyScatterPoint, EfficiencyScatterResponse } from '../../types/api';
import { EChartBox, glassTooltipConfig } from './EChartBox';
import { ChartSkeleton, StateNoticeCard } from '../ui/Skeletons';
import { SlidingTabs } from '../motion/primitives';

export function formatScatterTooltipHtml(
  pt: EfficiencyScatterPoint,
  xLabel: string,
  xDisplay: string,
  gridColor: string
): string {
  const covPct = pt.common_valid_n > 0 ? Math.round((pt.x_coverage_tasks / pt.common_valid_n) * 100) : 0;
  const titleText = escapeHtml(pt.model_canonical || pt.batch_name);
  const harnessText = escapeHtml(pt.harness || '—');
  const batchText = escapeHtml(pt.batch_name);
  const xLabelSafe = escapeHtml(xLabel);
  const xDisplaySafe = escapeHtml(xDisplay);
  const fullCovSafe = escapeHtml(pt.full_coverage);
  const paretoBadge = pt.on_pareto ? '★ Pareto 前沿点' : pt.eligible_for_pareto ? '可入前沿' : '缺覆盖不入前沿';
  return `
    <div style="min-width:220px;">
      <div style="font-weight:700;margin-bottom:4px;">${titleText}</div>
      <div style="font-size:11px;opacity:0.78;margin-bottom:8px;">Harness: <b>${harnessText}</b> · Batch: ${batchText}</div>
      <div style="display:flex;justify-content:space-between;gap:12px;margin-bottom:3px;">
        <span>共同有效得分 (N=${Number(pt.common_valid_n)})</span>
        <b style="font-family:JetBrains Mono,monospace;color:${pt.on_pareto ? '#d97706' : 'inherit'}">${escapeHtml(fmtPct(pt.score_pct))}</b>
      </div>
      <div style="display:flex;justify-content:space-between;gap:12px;margin-bottom:6px;">
        <span>${xLabelSafe}</span>
        <b style="font-family:JetBrains Mono,monospace;">${xDisplaySafe}</b>
      </div>
      <div style="height:4px;border-radius:99px;background:${escapeHtml(gridColor)};overflow:hidden;margin-bottom:6px;">
        <div style="height:100%;width:${covPct}%;background:${pt.eligible_for_pareto ? '#10B981' : '#F59E0B'};"></div>
      </div>
      <div style="font-size:11px;opacity:0.75;">
        全批覆盖: ${fullCovSafe} · ${paretoBadge} · 点击下钻 / Shift+点击或框选加入对比
      </div>
    </div>
  `;
}

export interface ScatterBenchmarkPreset {
  dataset_release_id: string;
  scoring_protocol: string;
  label: string;
}

const DEFAULT_AGENTIC_PRESETS: ScatterBenchmarkPreset[] = [
  { dataset_release_id: 'terminal-bench__2.1__default', scoring_protocol: 'harbor-native-reward-valid-v1', label: 'Terminal 2.1' },
  { dataset_release_id: 'deepswe__1.1__default', scoring_protocol: 'deepswe-f2p-p2p', label: 'DeepSWE 1.1' },
  { dataset_release_id: 'gaia__2026-11-20__val', scoring_protocol: 'gaia-official-scorer', label: 'GAIA' },
  { dataset_release_id: 'wildclawbench__2026-11-20__default', scoring_protocol: 'wildclaw-overall-score-v1', label: 'WildClawBench' },
];

const X_AXIS_OPTIONS = [
  { id: 'duration_p50_sec', label: '耗时 P50 (s)', unit: 's' },
  { id: 'total_tokens_avg', label: '均总 Tokens', unit: 'tok' },
  { id: 'tool_calls_avg', label: '均工具步数', unit: 'calls' },
  { id: 'recorded_cost_usd', label: '已记录成本 ($)', unit: '$' },
] as const;

/**
 * ARC Prize-style Efficiency Frontier & Family Trajectory Scatter Chart (v2.2.1 02-1 §3 & A-15)
 * 100% anchored to real Harbor/historical metrics, explicit task_set_policy & coverage split,
 * Focus Spotlight (hover dims unrelated points to 20%), Pareto frontier guardrail, GlassChartTooltip.
 */
export function EfficiencyScatterChart({
  initialReleaseId = 'terminal-bench__2.1__default',
  initialProtocol = 'harbor-native-reward-valid-v1',
  presets = DEFAULT_AGENTIC_PRESETS,
  hidePresetSwitcher = false,
  height = 420,
  onSelectBatch,
}: {
  initialReleaseId?: string;
  initialProtocol?: string;
  presets?: ScatterBenchmarkPreset[];
  hidePresetSwitcher?: boolean;
  height?: number;
  onSelectBatch?: (point: EfficiencyScatterPoint, scatter: EfficiencyScatterResponse) => void;
}) {
  const navigate = useNavigate();
  const theme = useAppStore((s) => s.theme);
  const addToCompare = useAppStore((s) => s.addToCompare);

  const [selectedPresetIdx, setSelectedPresetIdx] = useState(() => {
    const idx = presets.findIndex((p) => p.dataset_release_id === initialReleaseId);
    return idx >= 0 ? idx : 0;
  });
  const activePreset = hidePresetSwitcher
    ? { dataset_release_id: initialReleaseId, scoring_protocol: initialProtocol, label: initialReleaseId }
    : presets[selectedPresetIdx] || presets[0];

  const [xAxis, setXAxis] = useState<string>('duration_p50_sec');
  const [taskSetPolicy, setTaskSetPolicy] = useState<'fixed_y_common' | 'recompute_xy_common'>('fixed_y_common');
  const [logAxis, setLogAxis] = useState<boolean>(false);
  const [colorBy, setColorBy] = useState<'harness' | 'model'>('harness');
  const [selectedBatchIds, setSelectedBatchIds] = useState<string[]>([]);

  const toggleSelectBatch = (batchId: string) => {
    setSelectedBatchIds((prev) => (prev.includes(batchId) ? prev.filter((id) => id !== batchId) : [...prev, batchId].slice(0, 8)));
  };

  const query = useQuery({
    queryKey: ['efficiency-scatter', activePreset.dataset_release_id, activePreset.scoring_protocol, xAxis, taskSetPolicy],
    queryFn: () =>
      apiFetch<EfficiencyScatterResponse>(
        `/api/v1/leaderboard/efficiency-scatter?dataset_release_id=${encodeURIComponent(
          activePreset.dataset_release_id
        )}&scoring_protocol=${encodeURIComponent(activePreset.scoring_protocol)}&x_axis=${encodeURIComponent(
          xAxis
        )}&task_set_policy=${encodeURIComponent(taskSetPolicy)}`
      ),
  });

  const data = query.data;
  const xMeta = X_AXIS_OPTIONS.find((x) => x.id === xAxis) || X_AXIS_OPTIONS[0];
  const cov = data?.x_axis_coverage?.[xAxis];
  const availableAxes = X_AXIS_OPTIONS.filter((axis) =>
    axis.id !== xAxis && (data?.x_axis_coverage?.[axis.id]?.batches_with_any_records ?? 0) > 0,
  );
  const hasCoordinates = data?.points.some((point) => point.x_value !== null || point.partial_x_value !== null);
  const hasFullCoverage = data?.points.some((point) => point.eligible_for_pareto && point.x_value !== null);

  const option = useMemo<echarts.EChartsCoreOption>(() => {
    if (!data) return {};
    const textColor = theme === 'dark' ? '#a1a1aa' : '#475569';
    const gridColor = theme === 'dark' ? '#27272a' : '#e5e7eb';

    const plottable = data.points.filter((p) => p.eligible_for_pareto && p.x_value !== null);
    const partial = data.points.filter((p) => !p.eligible_for_pareto && p.partial_x_value !== null);
    const paretoPoints = plottable
      .filter((p) => p.on_pareto)
      .sort((a, b) => (a.x_value ?? 0) - (b.x_value ?? 0));

    const fmtX = (val: number | null) => {
      if (val === null) return '—';
      if (xAxis === 'duration_p50_sec') return fmtDuration(val);
      if (xAxis === 'total_tokens_avg') return fmtTokens(val);
      if (xAxis === 'recorded_cost_usd') return `$${val.toFixed(4)} (recorded_native)`;
      return val.toFixed(1);
    };

    const byGroup = new Map<string, EfficiencyScatterPoint[]>();
    for (const pt of plottable) {
      const grp = colorBy === 'model' ? pt.model_canonical || 'unknown' : pt.harness || 'unknown';
      const arr = byGroup.get(grp) || [];
      arr.push(pt);
      byGroup.set(grp, arr);
    }

    const series: echarts.SeriesOption[] = [];

    // Verified ordered family trajectory connectors (ONLY when backend confirms same model+harness ordered series!)
    const pointByBatchId = new Map(plottable.map((p) => [p.batch_id, p]));
    for (const conn of data.series_connectors || []) {
      const coords = conn.batch_ids
        .map((bid) => pointByBatchId.get(bid))
        .filter((p): p is EfficiencyScatterPoint => Boolean(p && p.x_value !== null))
        .map((p) => [p.x_value, p.score_pct]);
      if (coords.length >= 2) {
        const lineColor = stableColorForKey(conn.series_id);
        series.push({
          name: `轨迹 · ${conn.label || conn.series_id}`,
          type: 'line',
          universalTransition: true,
          symbol: 'none',
          lineStyle: { color: lineColor, width: 1.75, type: 'dotted', opacity: 0.75 },
          emphasis: { lineStyle: { width: 3, opacity: 1 } },
          blur: { lineStyle: { opacity: 0.15 } },
          data: coords,
          z: 3,
        });
      }
    }

    // Pareto frontier dashed glowing guide (only when >= 2 plottable frontier points)
    if (paretoPoints.length >= 2) {
      series.push({
        name: 'Pareto Frontier',
        type: 'line',
        universalTransition: true,
        symbol: 'none',
        lineStyle: {
          color: theme === 'dark' ? '#fbbf24' : '#d97706',
          width: 2,
          type: 'dashed',
        },
        data: paretoPoints.map((p) => [p.x_value, p.score_pct]),
        z: 2,
      });
    }

    for (const [grp, pts] of byGroup.entries()) {
      const color = stableColorForKey(grp);
      series.push({
        name: grp,
        type: 'scatter',
        universalTransition: true,
        symbolSize: 15,
        emphasis: {
          focus: 'series',
          scale: 1.35,
          itemStyle: {
            shadowBlur: 16,
            shadowColor: color,
            borderWidth: 2,
            borderColor: theme === 'dark' ? '#ffffff' : '#0f172a',
          },
        },
        blur: {
          itemStyle: { opacity: 0.2 },
          label: { opacity: 0.2 },
        },
        itemStyle: {
          color,
          borderColor: theme === 'dark' ? '#111114' : '#ffffff',
          borderWidth: 1.5,
        },
        label: {
          show: true,
          position: 'top',
          distance: 6,
          fontSize: 11,
          color: theme === 'dark' ? '#e4e4e7' : '#334155',
          formatter: (p: unknown) => {
            const pt = (p as { data?: { point?: EfficiencyScatterPoint } })?.data?.point;
            if (!pt) return '';
            return `${pt.harness || ''} · ${pt.model_canonical || pt.batch_name}`;
          },
        },
        data: pts.map((pt) => {
          const isSel = selectedBatchIds.includes(pt.batch_id);
          return {
            value: [pt.x_value, pt.score_pct],
            point: pt,
            symbolSize: isSel ? 19 : 15,
            itemStyle: isSel
              ? {
                  borderColor: theme === 'dark' ? '#fbbf24' : '#0068b5',
                  borderWidth: 3,
                  shadowBlur: 12,
                  shadowColor: color,
                }
              : undefined,
          };
        }),
        z: 5,
      });
    }

    // Incomplete coverage points dimmed explicitly (NEVER eligible for Pareto frontier!)
    if (partial.length > 0) {
      series.push({
        name: '覆盖不足 (不入前沿)',
        type: 'scatter',
        symbol: 'diamond',
        symbolSize: 12,
        itemStyle: {
          color: theme === 'dark' ? '#52525b' : '#94a3b8',
          opacity: 0.55,
          borderType: 'dashed',
          borderWidth: 1,
        },
        data: partial.map((pt) => ({
          value: [pt.partial_x_value, pt.score_pct],
          point: pt,
        })),
        z: 3,
      });
    }

    return {
      toolbox: {
        right: 12,
        top: 0,
        feature: {
          brush: {
            type: ['rect', 'clear'],
            title: { rect: '矩形框选对比点', clear: '清除框选' },
          },
        },
      },
      brush: {
        toolbox: ['rect', 'clear'],
        xAxisIndex: 0,
        yAxisIndex: 0,
        brushStyle: {
          borderWidth: 1.5,
          color: theme === 'dark' ? 'rgba(59,130,246,0.18)' : 'rgba(0,104,181,0.12)',
          borderColor: theme === 'dark' ? '#60a5fa' : '#0068b5',
        },
      },
      tooltip: {
        ...glassTooltipConfig(theme),
        trigger: 'item',
        formatter: (params: { data?: { point?: EfficiencyScatterPoint } }) => {
          const pt = params.data?.point;
          if (!pt) return '';
          const xDisplay = pt.eligible_for_pareto
            ? fmtX(pt.x_value)
            : `${fmtX(pt.partial_x_value)} (部分覆盖 ${pt.x_coverage_tasks}/${pt.common_valid_n})`;
          return formatScatterTooltipHtml(pt, xMeta.label, xDisplay, gridColor);
        },
      },
      legend: {
        type: 'scroll',
        bottom: 4,
        textStyle: { color: textColor, fontSize: 11 },
      },
      grid: { top: 34, right: 32, bottom: 58, left: 56 },
      xAxis: {
        type: logAxis ? 'log' : 'value',
        name: xMeta.label,
        nameLocation: 'middle',
        nameGap: 28,
        nameTextStyle: { color: textColor, fontSize: 11, fontWeight: 600 },
        axisLabel: { color: textColor, fontSize: 11 },
        splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
      },
      yAxis: {
        type: 'value',
        name: 'Score (%)',
        min: 0,
        max: 100,
        nameTextStyle: { color: textColor, fontSize: 11, fontWeight: 600 },
        axisLabel: { color: textColor, fontSize: 11, formatter: '{value}%' },
        splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
      },
      series,
    };
  }, [data, theme, xAxis, logAxis, colorBy, xMeta.label, selectedBatchIds]);

  const missingPoints = (data?.points || []).filter((p) => !p.eligible_for_pareto);

  return (
    <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 mb-3 border-b border-[var(--border-subtle)]">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="m-0 text-sm font-bold tracking-wide uppercase text-[var(--text-primary)]">
              {data?.raw_dataset_name || activePreset.label} · 效率前沿轨迹图
            </h3>
            <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)] border border-[var(--sem-pass-border)]">
              <CheckCircle2 className="w-3 h-3" /> VERIFIED / FORMAL
            </span>
            {data && (
              <span className="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-mono tabular-nums bg-[var(--brand-tint)] text-[var(--brand-primary)]">
                N = {data.common_valid_tasks} common valid tasks
              </span>
            )}
          </div>
          {cov && (
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--text-secondary)]">
              <span className="tabular-nums">
                记录覆盖: <strong>{cov.batches_with_any_records}/{cov.total_batches}</strong> 批有记录 · 满覆盖可绘制:{' '}
                <strong>{cov.plottable_full_coverage_points}</strong> 点
              </span>
              {cov.partial_record_coverage && (
                <span className="inline-flex items-center gap-1 text-[var(--sem-zero-fg)]">
                  <AlertTriangle className="w-3.5 h-3.5" /> {cov.partial_record_coverage}
                </span>
              )}
              {cov.note && <span className="text-[var(--text-muted)]">· {cov.note}</span>}
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <select
            aria-label="切换 X 轴指标"
            value={xAxis}
            onChange={(e) => setXAxis(e.target.value)}
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-primary)] cursor-pointer"
          >
            {X_AXIS_OPTIONS.map((o) => (
              <option key={o.id} value={o.id}>
                X 轴: {o.label}
              </option>
            ))}
          </select>

          <select
            aria-label="切轴共同题集策略"
            value={taskSetPolicy}
            onChange={(e) => setTaskSetPolicy(e.target.value as 'fixed_y_common' | 'recompute_xy_common')}
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-primary)] cursor-pointer"
          >
            <option value="fixed_y_common">锁定基准共同题 (fixed_y_common)</option>
            <option value="recompute_xy_common">按 X/Y 重取共同题 (recompute_xy_common)</option>
          </select>

          <button
            type="button"
            onClick={() => setLogAxis((v) => !v)}
            className={`rounded-lg border px-2.5 py-1.5 text-xs font-medium cursor-pointer ${
              logAxis
                ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
            }`}
          >
            {logAxis ? '对数轴 Log' : '线性轴 Linear'}
          </button>

          <button
            type="button"
            onClick={() => setColorBy((v) => (v === 'harness' ? 'model' : 'harness'))}
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
          >
            配色: {colorBy === 'harness' ? 'Harness' : 'Model'}
          </button>

          {data && data.points.length >= 2 && (
            <button
              type="button"
              onClick={() => {
                data.points.slice(0, 6).forEach((pt) =>
                  addToCompare({
                    id: pt.batch_id,
                    kind: 'batch',
                    batch_id: pt.batch_id,
                    alias: `${pt.harness || ''} · ${pt.model_canonical || pt.batch_name}`.trim(),
                  })
                );
              }}
              className="inline-flex items-center gap-1 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-2.5 py-1.5 text-xs font-medium text-white lrm-press cursor-pointer"
            >
              <Plus className="w-3.5 h-3.5" /> 全部加入对比
            </button>
          )}
        </div>
      </div>

      {query.isLoading ? (
        <ChartSkeleton height={height} />
      ) : query.isError ? (
        <StateNoticeCard kind="error" title="效率前沿数据加载失败" description={(query.error as Error).message} onRetry={() => query.refetch()} />
      ) : !hasCoordinates ? (
        <div className="rounded-lg border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] px-4 py-6 text-center text-xs">
          <Sparkles className="mx-auto mb-2 h-6 w-6 text-[var(--sem-zero-fg)]" aria-hidden="true" />
          <div className="mb-1 font-semibold text-[var(--text-primary)]">
            {data?.common_valid_tasks
              ? `共同有效题缺少「${xMeta.label}」的数据`
              : '当前基准和评分协议没有可比较的共同有效题'}
          </div>
          <p className="mb-0 mt-1 text-[var(--text-secondary)]">
            {availableAxes.length > 0
              ? '下列指标有可用记录，可直接切换。'
              : '可以选择其他基准，或在自由对比中查看各批次实际覆盖。'}
          </p>
          <div className="mt-3 flex flex-wrap justify-center gap-2">
            {availableAxes.map((axis) => (
              <button
                key={axis.id}
                type="button"
                onClick={() => setXAxis(axis.id)}
                className="rounded-md border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 font-semibold text-[var(--brand-primary)] hover:bg-[var(--brand-tint)]"
              >
                切换到{axis.label}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div className="space-y-2.5">
          {!hasFullCoverage && (
            <p className="m-0 rounded-lg bg-[var(--sem-zero-bg)] px-3 py-2 text-xs text-[var(--text-primary)]">
              空心点使用部分试题的观测值。前沿需有完整的共同题覆盖。
            </p>
          )}
          <EChartBox
            option={option}
            height={height}
            exportName={`efficiency-${activePreset.dataset_release_id}-${xAxis}`}
            onPointClick={(params: unknown) => {
              const typed = params as {
                data?: { point?: EfficiencyScatterPoint };
                event?: { event?: { shiftKey?: boolean } };
              };
              const p = typed?.data?.point;
              if (!p || !data) return;
              if (typed?.event?.event?.shiftKey) {
                toggleSelectBatch(p.batch_id);
                return;
              }
              onSelectBatch?.(p, data);
            }}
            onBrushSelected={(params: unknown) => {
              const batch = (params as { batch?: Array<{ selected?: Array<{ seriesIndex: number; dataIndex: number[] }> }> })?.batch?.[0];
              if (!batch?.selected || !data) return;
              const optSeries = (option.series as Array<{ data?: Array<{ point?: EfficiencyScatterPoint }> }>) || [];
              const picked: string[] = [];
              for (const sel of batch.selected) {
                const sData = optSeries[sel.seriesIndex]?.data || [];
                for (const dIdx of sel.dataIndex || []) {
                  const bid = sData[dIdx]?.point?.batch_id;
                  if (bid && !picked.includes(bid)) picked.push(bid);
                }
              }
              if (picked.length > 0) {
                setSelectedBatchIds(picked.slice(0, 8));
              }
            }}
          />

          {/* Interactive Brush / Shift-Click / Point-Chip Multi-Select Bar (A-15) */}
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-[11px]">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[var(--text-secondary)] font-medium">框选 / Shift+点选送对比：</span>
              {data.points.map((pt) => {
                const active = selectedBatchIds.includes(pt.batch_id);
                return (
                  <button
                    key={pt.batch_id}
                    type="button"
                    aria-pressed={active}
                    onClick={() => toggleSelectBatch(pt.batch_id)}
                    className={`rounded-full border px-2 py-0.5 text-[11px] transition-colors cursor-pointer ${
                      active
                        ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                        : 'border-[var(--border-subtle)] bg-[var(--bg-surface)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
                    }`}
                  >
                    {pt.harness || pt.batch_name} ({fmtPct(pt.score_pct, 1)})
                  </button>
                );
              })}
            </div>

            {selectedBatchIds.length > 0 && (
              <div className="flex items-center gap-1.5 shrink-0">
                <span className="font-mono text-[var(--brand-primary)]">已选 {selectedBatchIds.length} 组</span>
                <button
                  type="button"
                  onClick={() => {
                    selectedBatchIds.forEach((bid) => {
                      const pt = data.points.find((x) => x.batch_id === bid);
                      if (pt) {
                        addToCompare({
                          id: pt.batch_id,
                          kind: 'batch',
                          batch_id: pt.batch_id,
                          alias: `${pt.harness || ''} · ${pt.model_canonical || pt.batch_name}`.trim(),
                        });
                      }
                    });
                  }}
                  className="inline-flex items-center gap-1 rounded-lg bg-[var(--brand-tint)] text-[var(--brand-primary)] hover:bg-[var(--brand-primary)] hover:text-white px-2.5 py-1 font-semibold cursor-pointer"
                >
                  <Plus className="w-3 h-3" /> 选中项加入对比
                </button>
                {selectedBatchIds.length >= 2 && (
                  <button
                    type="button"
                    onClick={() => {
                      selectedBatchIds.forEach((bid) => {
                        const pt = data.points.find((x) => x.batch_id === bid);
                        if (pt) {
                          addToCompare({
                            id: pt.batch_id,
                            kind: 'batch',
                            batch_id: pt.batch_id,
                            alias: `${pt.harness || ''} · ${pt.model_canonical || pt.batch_name}`.trim(),
                          });
                        }
                      });
                      navigate('/eval/compare?fromDock=1');
                    }}
                    className="inline-flex items-center gap-1 rounded-lg bg-[var(--brand-primary)] text-white hover:bg-[var(--brand-hover)] px-2.5 py-1 font-semibold cursor-pointer"
                  >
                    <GitCompareArrows className="w-3 h-3" /> 立即对比
                  </button>
                )}
                <button
                  type="button"
                  aria-label="清空散点选择"
                  onClick={() => setSelectedBatchIds([])}
                  className="rounded p-1 text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {missingPoints.length > 0 && cov && cov.plottable_full_coverage_points > 0 && (
        <div className="mt-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-[11px] text-[var(--text-secondary)]">
          <strong className="text-[var(--text-primary)]">未入前沿批次（X 轴缺测或覆盖不足，{missingPoints.length} 组）：</strong>{' '}
          {missingPoints.map((m) => `${m.batch_name} (${m.x_coverage_tasks}/${m.common_valid_n})`).join(' · ')}
        </div>
      )}

      {!hidePresetSwitcher && presets.length > 1 && (
        <div className="mt-3 pt-3 border-t border-[var(--border-subtle)] flex items-center justify-center">
          <SlidingTabs
            layoutId="arc-scatter-presets"
            size="sm"
            value={String(selectedPresetIdx)}
            onChange={(v) => setSelectedPresetIdx(Number(v))}
            options={presets.map((p, i) => ({ id: String(i), label: p.label }))}
          />
        </div>
      )}
    </div>
  );
}
