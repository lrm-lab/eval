import { DismissibleDetails } from '../ui/DismissibleDetails';
import { useEffect, useMemo, useRef, useState } from 'react';
import type * as echarts from 'echarts';
import { escapeHtml, fmtDeltaPP, fmtPct, stableColorForKey } from '../../lib/format';
import { useAppStore } from '../../lib/store';
import type { CompareQueryResponse } from '../../types/api';
import { EChartBox, glassTooltipConfig } from './EChartBox';
import { leaderName, useComparePublicLeaders, type CompareSubject } from './comparePublicLeaders';

const LEADER_SERIES = '公开榜首';
const MIXED_REFERENCE_SERIES = '公开榜首 / 论文最佳';
const LEADER_COLOR = '#14b8a6';
const LONGMEMEVAL_VARIANTS: Record<string, string> = {
  'oracle-con': 'Oracle · CoN',
  'oracle-direct': 'Oracle · Direct',
  's-con': 'S · CoN',
  's-direct': 'S · Direct',
};

export function CompareScoreCharts({
  comparisonId,
  rows,
  subjects,
  snapshot,
}: {
  comparisonId: string;
  rows: CompareQueryResponse['rows'];
  subjects: CompareSubject[];
  snapshot: CompareQueryResponse['bound_snapshot'];
}) {
  const theme = useAppStore((s) => s.theme);
  const sectionRef = useRef<HTMLElement>(null);
  const visibleLegend = useRef<Record<string, boolean>>({});
  const [stacked, setStacked] = useState(false);

  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;
    const observer = new ResizeObserver(([entry]) => setStacked(entry.contentRect.width < 760));
    observer.observe(section);
    return () => observer.disconnect();
  }, []);

  const overlays = useComparePublicLeaders(rows, subjects, snapshot);
  const publicSeries = overlays.some(({ point }) => point?.source.reference_kind === 'paper')
    ? MIXED_REFERENCE_SERIES
    : LEADER_SERIES;
  const { axisLabels, datasetNames } = useMemo(() => {
    const variants = rows.map((row) => row.benchmark_slug === 'longmemeval'
      ? LONGMEMEVAL_VARIANTS[row.dataset_release_id.split('__').at(-1) ?? '']
      : undefined);
    return {
      axisLabels: rows.map((row, index) => variants[index] ?? row.raw_dataset_name),
      datasetNames: rows.map((row, index) => variants[index] ? `LongMemEval · ${variants[index]}` : row.raw_dataset_name),
    };
  }, [rows]);
  const hasRadar = rows.length >= 3;
  const option = useMemo<echarts.EChartsOption>(() => {
    const textColor = theme === 'dark' ? '#a1a1aa' : '#475569';
    const gridColor = theme === 'dark' ? '#3f3f46' : '#e5e7eb';
    const radarCenter = stacked ? '50%' : '16.67%';
    const barTitleTop = stacked ? (hasRadar ? 284 : 88) : 6;
    const titleStyle = { color: textColor, fontSize: 13, fontWeight: 600 };
    const scores = subjects.map(({ id, alias }) => ({
      name: alias === publicSeries || subjects.some((other) => other.id !== id && other.alias === alias) ? `${alias} (${id})` : alias,
      color: stableColorForKey(id),
      values: rows.map((row) => row.cells[id]?.common_valid?.score ?? null),
    }));

    const leaderValues = overlays.map(({ point }) => point?.leader.score_pct ?? null);
    const hasLeader = leaderValues.some((score) => score != null);
    const legendNames = scores.map(({ name }) => name);
    if (hasLeader) {
      if (stacked) legendNames.unshift(publicSeries);
      else legendNames.push(publicSeries);
    }
    const radarY = stacked ? 148 : 158;
    const radarRadius = stacked ? 78 : 86;

    function leaderTooltip(index: number) {
      const { point, deltas, missing, reasons } = overlays[index];
      if (point && visibleLegend.current[publicSeries] === false) return '';
      if (!point) return '<div>' + escapeHtml(LEADER_SERIES) + ' · ' + escapeHtml(missing) + '</div>';
      const { leader, source } = point;
      const pointLabel = source.reference_kind === 'paper' ? '论文最佳' : LEADER_SERIES;
      const denominator = leader.task_count + ' 题 · ' + (leader.sample_count == null ? '计分次数未公布' : leader.sample_count + ' 次计分');
      const scope = [source.dataset_version, source.split, source.metric, denominator].join(' · ');
      const date = source.captured_on ? ' · 快照 ' + source.captured_on : '';
      const differences = deltas.map(({ subject, delta }) =>
        '<div>' + escapeHtml(subject) + ' · ' + escapeHtml(fmtDeltaPP(delta)) + '</div>').join('');
      const reference = reasons.length ? '<div>参考分 · ' + escapeHtml(source.reference_note || reasons[0]) + '</div>' : '';
      return '<div><strong>' + escapeHtml(pointLabel + ' · ' + escapeHtml(fmtPct(leader.score_pct))) + '</strong></div>'
        + '<div>' + escapeHtml(leaderName(leader)) + '</div><div>' + escapeHtml(source.label + date) + '</div>'
        + '<div>' + escapeHtml(scope) + '</div>' + differences + reference;
    }

    const leaderBars: echarts.BarSeriesOption = {
      name: publicSeries,
      type: 'bar',
      itemStyle: { color: LEADER_COLOR, borderRadius: [4, 4, 0, 0] },
      data: leaderValues,
    };
    // Native radar layouts place missing values at the center. Connect only
    // adjacent measured axes so a gap never looks like a score of zero.
    const leaderRadar: echarts.CustomSeriesOption = {
      name: publicSeries,
      type: 'custom',
      z: 5,
      coordinateSystem: 'none',
      clip: false,
      data: leaderValues.flatMap((value, index) => value == null ? [] : [[index, value]]),
      tooltip: { trigger: 'item' },
      renderItem: (_params, api) => {
        const index = Number(api.value(0));
        function position(axis: number, score: number) {
          const angle = Math.PI / 2 + Math.PI * 2 * axis / rows.length;
          const radius = score / 100 * radarRadius;
          return {
            x: api.getWidth() * (stacked ? 0.5 : 0.1667) + radius * Math.cos(angle),
            y: radarY - radius * Math.sin(angle),
          };
        }
        const { x, y } = position(index, Number(api.value(1)));
        const marker: echarts.CustomSeriesRenderItemReturn = {
          type: 'circle',
          shape: { cx: x, cy: y, r: 4 },
          style: { fill: LEADER_COLOR, stroke: theme === 'dark' ? '#18181b' : '#ffffff', lineWidth: 1 },
        };
        const next = (index + 1) % rows.length;
        const nextScore = leaderValues[next];
        if (nextScore == null) return marker;
        const end = position(next, nextScore);
        return {
          type: 'group',
          children: [{
            type: 'line',
            silent: true,
            shape: { x1: x, y1: y, x2: end.x, y2: end.y },
            style: { stroke: LEADER_COLOR, opacity: 0.85, lineWidth: 1.5, lineDash: [4, 3] },
          }, marker],
        };
      },
    };

    return {
      title: [
        {
          text: '多维雷达图',
          subtext: hasRadar ? undefined : '至少需要 3 个共同可比基准',
          textStyle: titleStyle,
          subtextStyle: { color: textColor, fontSize: 11 },
          left: radarCenter,
          top: 6,
          textAlign: 'center',
        },
        {
          text: '分组柱状图',
          textStyle: titleStyle,
          left: stacked ? '50%' : '66.67%',
          top: barTitleTop,
          textAlign: 'center',
        },
      ],
      tooltip: {
        ...glassTooltipConfig(theme),
        trigger: 'axis',
        confine: true,
        formatter: (params) => {
          const items = Array.isArray(params) ? params : [params];
          const first = items[0];
          if (!first) return '';
          if (first.seriesType === 'custom' && Array.isArray(first.value)) {
            const index = Number(first.value[0]);
            return '<strong>' + escapeHtml(datasetNames[index]) + '</strong>' + leaderTooltip(index);
          }
          if (!Array.isArray(params)) {
            const values = Array.isArray(first.value) ? first.value : [];
            return '<strong>' + escapeHtml(first.name) + '</strong>' + datasetNames.map((name, index) =>
              '<div>' + escapeHtml(name) + ' · ' + escapeHtml(fmtPct(typeof values[index] === 'number' ? values[index] : null)) + '</div>').join('');
          }
          const scoreLines = items.filter((item) => item.seriesName !== publicSeries && typeof item.value === 'number')
            .map((item) => '<div>' + escapeHtml(item.seriesName) + ' · ' + escapeHtml(fmtPct(item.value as number)) + '</div>');
          return '<strong>' + escapeHtml(datasetNames[first.dataIndex]) + '</strong>' + scoreLines.join('') + leaderTooltip(first.dataIndex);
        },
      },
      legend: {
        type: 'scroll',
        selected: visibleLegend.current,
        data: legendNames,
        ...(stacked ? { itemWidth: 16, itemGap: 8 } : {}),
        left: 4,
        right: 4,
        bottom: 32,
        textStyle: { color: textColor, fontSize: 11 },
        pageTextStyle: { color: textColor },
      },
      radar: hasRadar ? {
        center: [radarCenter, radarY],
        radius: radarRadius,
        indicator: axisLabels.map((name) => ({ name, max: 100 })),
        axisName: { color: textColor, fontSize: 10, width: 68, overflow: 'truncate' },
        axisNameGap: 6,
        axisLine: { lineStyle: { color: gridColor } },
        splitLine: { lineStyle: { color: gridColor } },
        splitArea: { show: false },
      } : undefined,
      grid: { top: barTitleTop + 35, right: 12, bottom: 90, left: stacked ? 42 : '38%' },
      xAxis: {
        type: 'category',
        data: axisLabels,
        axisLabel: {
          color: textColor,
          fontSize: 10,
          width: stacked ? 58 : 82,
          overflow: 'truncate',
          rotate: rows.length > 6 || stacked ? 30 : 0,
          hideOverlap: true,
        },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { color: textColor, fontSize: 11, formatter: '{value}%' },
        splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
      },
      series: [
        ...scores.map(({ name, color, values }): echarts.BarSeriesOption => ({
          name,
          type: 'bar',
          itemStyle: { color, borderRadius: [4, 4, 0, 0] },
          data: values,
        })),
        ...(hasLeader ? [leaderBars] : []),
        ...(hasRadar ? [{
          type: 'radar' as const,
          tooltip: { trigger: 'item' as const },
          symbolSize: 4,
          data: scores.map(({ name, color, values }) => ({
            name,
            value: values.map((value) => value ?? '-'),
            lineStyle: { color, width: 2 },
            itemStyle: { color },
          })),
        }] : []),
        ...(hasRadar && hasLeader ? [leaderRadar] : []),
      ],
    };
  }, [axisLabels, datasetNames, hasRadar, overlays, publicSeries, rows, stacked, subjects, theme]);

  const height = stacked ? (hasRadar ? 600 : 390) : 340;
  return (
    <section
      ref={sectionRef}
      aria-labelledby="compare-scores-heading"
      className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4"
    >
      <h2 id="compare-scores-heading" className="mb-2 text-sm font-bold text-[var(--text-primary)]">评测得分与公开参考</h2>
      <EChartBox
        option={option}
        height={height}
        exportName={`compare-${comparisonId}`}
        onChartReady={(chart) => chart.on('legendselectchanged', (event) => {
          visibleLegend.current = (event as { selected: Record<string, boolean> }).selected;
        })}
      />
      <DismissibleDetails className="border-t border-[var(--border-subtle)] pt-2 text-xs" summary={(
        <summary className="cursor-pointer text-[var(--text-secondary)]">
          公开榜首来源与覆盖（{overlays.filter(({ point }) => point).length}/{rows.length}）
        </summary>
      )}>
        <ul className="mt-2 space-y-2 p-0">
          {overlays.map(({ point, missing, deltas, reasons }, index) => (
            <li key={rows[index].dataset_release_id} className="rounded-lg bg-[var(--bg-elevated)] px-2.5 py-2">
              <strong className="text-[var(--text-primary)]">{datasetNames[index]}</strong>
              {point ? (
                <>
                  <div className="mt-1 text-[var(--text-secondary)]">
                    {leaderName(point.leader)} · {fmtPct(point.leader.score_pct)} · {point.source.metric}
                  </div>
                  <div className="mt-1 text-[var(--text-muted)]">
                    {point.source.dataset_version} · {point.source.split} · {point.leader.task_count} 题
                    {point.leader.sample_count == null ? ' · 计分次数未公布' : ' · ' + point.leader.sample_count + ' 次计分'}
                    {point.source.status === 'stale' && ' · 显示上次核验数据'}
                    {point.source.synced_at && ' · 同步于 ' + point.source.synced_at.slice(0, 10)}
                    {point.source.captured_on && ' · 快照 ' + point.source.captured_on}
                    {' · '}
                    <a href={point.source.url} target="_blank" rel="noreferrer" className="text-[var(--brand-primary)] hover:underline">
                      {point.source.label}
                    </a>
                  </div>
                  <div className="mt-1 text-[var(--text-muted)]">{point.source.scoring_rule}</div>
                  {deltas.map(({ subject, delta }) => (
                    <div key={subject} className="mt-1 text-[var(--text-secondary)]">{subject} · {fmtDeltaPP(delta)}</div>
                  ))}
                  {reasons.length > 0 && <div className="mt-1 text-[var(--text-muted)]">仅供参考 · {reasons.join('；')}</div>}
                </>
              ) : <div className="mt-1 text-[var(--text-muted)]">{missing}</div>}
            </li>
          ))}
        </ul>
      </DismissibleDetails>
    </section>
  );
}
