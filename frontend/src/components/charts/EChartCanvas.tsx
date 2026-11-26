import React, { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import { Download } from 'lucide-react';
import { useAppStore } from '../../lib/store';
import { usePrefersReducedMotion } from '../ui/DismissiblePanel';

export default function EChartCanvas({
  option,
  height = 380,
  onPointClick,
  onBrushSelected,
  onChartReady,
  exportName,
  className = '',
}: {
  option: echarts.EChartsCoreOption;
  height?: number;
  onPointClick?: (params: unknown) => void;
  onBrushSelected?: (params: unknown) => void;
  onChartReady?: (chart: echarts.ECharts) => void;
  exportName?: string;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const theme = useAppStore((s) => s.theme);
  const reduced = usePrefersReducedMotion();

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = echarts.init(el, undefined, { renderer: 'canvas' });
    chartRef.current = chart;
    (el as HTMLDivElement & { __lrmChart?: echarts.ECharts }).__lrmChart = chart;
    onChartReady?.(chart);

    const ro = new ResizeObserver(() => {
      chart.resize();
    });
    ro.observe(el);
    const dismissTooltip = (event: MouseEvent) => {
      if (!event.composedPath().includes(el)) chart.dispatchAction({ type: 'hideTip' });
    };
    el.ownerDocument.addEventListener('click', dismissTooltip, true);

    return () => {
      el.ownerDocument.removeEventListener('click', dismissTooltip, true);
      ro.disconnect();
      delete (el as HTMLDivElement & { __lrmChart?: echarts.ECharts }).__lrmChart;
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const merged: echarts.EChartsCoreOption = {
      animation: !reduced,
      animationDuration: reduced ? 0 : 420,
      animationDurationUpdate: reduced ? 0 : 520,
      animationEasingUpdate: 'cubicOut',
      backgroundColor: 'transparent',
      ...option,
    };
    if (reduced && merged.tooltip) {
      merged.tooltip = Array.isArray(merged.tooltip)
        ? merged.tooltip.map((tooltip) => ({ ...tooltip, transitionDuration: 0 }))
        : { ...merged.tooltip, transitionDuration: 0 };
    }
    chart.setOption(merged, { notMerge: true });
  }, [option, theme, reduced]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !onPointClick) return;
    const handler = (params: unknown) => onPointClick(params);
    chart.on('click', handler);
    return () => {
      chart.off('click', handler);
    };
  }, [onPointClick]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !onBrushSelected) return;
    const handler = (params: unknown) => onBrushSelected(params);
    chart.on('brushSelected', handler);
    return () => {
      chart.off('brushSelected', handler);
    };
  }, [onBrushSelected]);

  const handleExportPng = () => {
    const chart = chartRef.current;
    if (!chart) return;
    const url = chart.getDataURL({
      type: 'png',
      pixelRatio: 2,
      backgroundColor: theme === 'dark' ? '#111114' : '#ffffff',
    });
    const a = document.createElement('a');
    a.href = url;
    a.download = `${exportName || 'lrm-eval-chart'}.png`;
    a.click();
  };

  return (
    <div className={`relative ${className}`}>
      <div ref={containerRef} style={{ width: '100%', height }} />
      {exportName && (
        <button
          type="button"
          onClick={handleExportPng}
          title="导出高清 PNG"
          className="absolute right-2 bottom-2 inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)]/90 px-2 py-1 text-[11px] font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:border-[var(--border-strong)] cursor-pointer"
        >
          <Download className="w-3 h-3" />
          <span>PNG</span>
        </button>
      )}
    </div>
  );
}
