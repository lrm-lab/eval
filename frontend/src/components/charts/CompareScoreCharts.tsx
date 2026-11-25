import { useEffect, useMemo, useRef, useState } from 'react';
import type * as echarts from 'echarts';
import { escapeHtml, fmtDeltaPP, fmtPct, stableColorForKey } from '../../lib/format';
import { useAppStore } from '../../lib/store';
import type { CompareQueryResponse } from '../../types/api';
import { EChartBox, glassTooltipConfig } from './EChartBox';
import { leaderName, useComparePublicLeaders, type CompareSubject } from './comparePublicLeaders';

const LEADER_SERIES = '公开榜首';
const LEADER_COLOR = '#14b8a6';

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
  const hasRadar = rows.length >= 3;
  const option = useMemo<echarts.EChartsOption>(() => {
    const textColor = theme === 'dark' ? '#a1a1aa' : '#475569';
    const gridColor = theme === 'dark' ? '#3f3f46' : '#e5e7eb';
    const radarCenter = stacked ? '50%' : '16.67%';
    const barTitleTop = stacked ? (hasRadar ? 284 : 88) : 6;
    const titleStyle = { color: textColor, fontSize: 13, fontWeight: 600 };
    const datasetNames = rows.map((row) => row.raw_dataset_name);
    const scores = subjects.map(({ id, alias }) => ({
      name: alias === LEADER_SERIES || subjects.some((other) => other.id !== id && other.alias === alias) ? `${alias} (${id})` : alias,
      color: stableColorForKey(id),
      values: rows.map((row) => row.cells[id]?.common_valid?.score ?? null),
    }));

    const leaderValues = overlays.map(({ point }) => point?.leader.score_pct ?? null);
    const hasLeader = leaderValues.some((score) => score != null);
    const radarY = stacked ? 148 : 158;
    const radarRadius = stacked ? 78 : 86;

    function leaderTooltip(index: number) {
      const { point, deltas, missing, reasons } = overlays[index];
      if (point && visibleLegend.current[LEADER_SERIES] === false) return '';
      if (!point) return '<div>' + escapeHtml(LEADER_SERIES) + ' · ' + escapeHtml(missing) + '</div>';
      const { leader, source } = point;
      const denominator = leader.task_count + ' 题 / ' + leader.sample_count + ' 次计分';
      const scope = [source.dataset_version, source.split, source.metric, denominator].join(' · ');
      const differences = deltas.map(({ subject, delta }) =>
        '<div>' + escapeHtml(subject) + ' · ' + escapeHtml(fmtDeltaPP(delta)) + '</div>').join('');
      const reference = reasons.length ? '<div>未列分差的分数仅供参考</div>' : '';
      return '<div><strong>' + escapeHtml(LEADER_SERIES) + ' · ' + escapeHtml(fmtPct(leader.score_pct)) + '</strong></div>'
        + '<div>' + escapeHtml(leaderName(leader)) + '</div><div>' + escapeHtml(source.label) + '</div>'
        + '<div>' + escapeHtml(scope) + '</div>' + differences + reference;
    }

    const leaderBars: echarts.BarSeriesOption = {
      name: LEADER_SERIES,
      type: 'bar',
      itemStyle: { color: LEADER_COLOR, borderRadius: [4, 4, 0, 0] },
      data: leaderValues,
    };
    // Native radar layouts place missing values at the center. Plot symbols
    // only on measured axes so missing scores cannot look like zero scores.
    const leaderRadar: echarts.CustomSeriesOption = {
      name: LEADER_SERIES,
      type: 'custom',
      z: 5,
      coordinateSystem: 'none',
      clip: false,
      data: leaderValues.flatMap((value, index) => value == null ? [] : [[index, value]]),
      tooltip: { trigger: 'item' },
      renderItem: (_params, api) => {
        const index = Number(api.value(0));
        const angle = Math.PI / 2 + Math.PI * 2 * index / rows.length;
        const radius = Number(api.value(1)) / 100 * radarRadius;
        return {
          type: 'circle',
          shape: {
            cx: api.getWidth() * (stacked ? 0.5 : 0.1667) + radius * Math.cos(angle),
            cy: radarY - radius * Math.sin(angle),
            r: 5,
          },
          style: { fill: LEADER_COLOR, stroke: theme === 'dark' ? '#18181b' : '#ffffff', lineWidth: 1 },
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
          const scoreLines = items.filter((item) => item.seriesName !== LEADER_SERIES && typeof item.value === 'number')
            .map((item) => '<div>' + escapeHtml(item.seriesName) + ' · ' + escapeHtml(fmtPct(item.value as number)) + '</div>');
          return '<strong>' + escapeHtml(datasetNames[first.dataIndex]) + '</strong>' + scoreLines.join('') + leaderTooltip(first.dataIndex);
        },
      },
      legend: {
        type: 'scroll',
        selected: visibleLegend.current,
        data: [...scores.map(({ name }) => name), ...(hasLeader ? [LEADER_SERIES] : [])],
        left: 4,
        right: 4,
        bottom: 32,
        textStyle: { color: textColor, fontSize: 11 },
        pageTextStyle: { color: textColor },
      },
      radar: hasRadar ? {
        center: [radarCenter, radarY],
        radius: radarRadius,
        indicator: datasetNames.map((name) => ({ name, max: 100 })),
        axisName: { color: textColor, fontSize: 10, width: 68, overflow: 'truncate' },
        axisNameGap: 6,
        axisLine: { lineStyle: { color: gridColor } },
        splitLine: { lineStyle: { color: gridColor } },
        splitArea: { show: false },
      } : undefined,
      grid: { top: barTitleTop + 35, right: 12, bottom: 90, left: stacked ? 42 : '38%' },
      xAxis: {
        type: 'category',
        data: datasetNames,
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
  }, [hasRadar, overlays, rows, stacked, subjects, theme]);

  const height = stacked ? (hasRadar ? 600 : 390) : 340;
  return (
    <section
      ref={sectionRef}
      aria-labelledby="compare-scores-heading"
      className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4"
    >
      <h2 id="compare-scores-heading" className="mb-2 text-sm font-bold text-[var(--text-primary)]">共同可比基准得分可视化</h2>
      <EChartBox
        option={option}
        height={height}
        exportName={`compare-${comparisonId}`}
        onChartReady={(chart) => chart.on('legendselectchanged', (event) => {
          visibleLegend.current = (event as { selected: Record<string, boolean> }).selected;
        })}
      />
      <details className="border-t border-[var(--border-subtle)] pt-2 text-xs">
        <summary className="cursor-pointer text-[var(--text-secondary)]">
          公开榜首来源与覆盖（{overlays.filter(({ point }) => point).length}/{rows.length}）
        </summary>
        <ul className="mt-2 space-y-2 p-0">
          {overlays.map(({ point, missing, deltas, reasons }, index) => (
            <li key={rows[index].dataset_release_id} className="rounded-lg bg-[var(--bg-elevated)] px-2.5 py-2">
              <strong className="text-[var(--text-primary)]">{rows[index].raw_dataset_name}</strong>
              {point ? (
                <>
                  <div className="mt-1 text-[var(--text-secondary)]">
                    {leaderName(point.leader)} · {fmtPct(point.leader.score_pct)} · {point.source.metric}
                  </div>
                  <div className="mt-1 text-[var(--text-muted)]">
                    {point.source.dataset_version} · {point.source.split} · {point.leader.task_count} 题 / {point.leader.sample_count} 次计分
                    {point.source.status === 'stale' && ' · 显示上次核验数据'}
                    {point.source.synced_at && ' · 同步于 ' + point.source.synced_at.slice(0, 10)}
                    {' · '}
                    <a href={point.source.url} target="_blank" rel="noreferrer" className="text-[var(--brand-primary)] hover:underline">
                      {point.source.label}
                    </a>
                  </div>
                  {deltas.map(({ subject, delta }) => (
                    <div key={subject} className="mt-1 text-[var(--text-secondary)]">{subject} · {fmtDeltaPP(delta)}</div>
                  ))}
                  {reasons.length > 0 && <div className="mt-1 text-[var(--text-muted)]">仅供参考 · {reasons.join('；')}</div>}
                </>
              ) : <div className="mt-1 text-[var(--text-muted)]">{missing}</div>}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}
