import { useId, useRef, useState } from 'react';
import { SlidersHorizontal } from 'lucide-react';
import { useDismissibleLayer } from '../../lib/dismissibleLayer';
import { fmtDeltaPP, fmtPct } from '../../lib/format';
import { DismissiblePanel } from './DismissiblePanel';

export interface CompositeScoreCellProps {
  score: number | null | undefined;
  deltaPp?: number | null;
  coverage?: string | null;
  isBest?: boolean;
  isLoading?: boolean;
  isError?: boolean;
  isValidZero?: boolean;
  subtitle?: string | null;
  breakdown?: Array<{ label: string; value: string }>;
  onClick?: () => void;
  ariaLabel?: string;
}

/**
 * Bespoke UI Element #1: CompositeScoreCell (v2.2.1 01-1 §2.1.6)
 * Covers all 8 states: Default, Hover, Active, Focus-Visible, Loading (Skeleton),
 * Missing (淡灰 —), Error (故障), Reduced-Motion.
 */
export function CompositeScoreCell({
  score,
  deltaPp,
  coverage,
  isBest = false,
  isLoading = false,
  isError = false,
  isValidZero = false,
  subtitle,
  breakdown,
  onClick,
  ariaLabel,
}: CompositeScoreCellProps) {
  const [showBreakdown, setShowBreakdown] = useState(false);
  const panelId = useId();
  const breakdownLayer = useRef<HTMLDivElement>(null);
  const breakdownTrigger = useRef<HTMLButtonElement>(null);
  const dismissBreakdown = useDismissibleLayer(breakdownLayer, showBreakdown,
    () => setShowBreakdown(false), () => breakdownTrigger.current?.focus({ preventScroll: true }));

  if (isLoading) {
    return (
      <div className="py-1 px-1.5 min-w-[92px]" aria-busy="true">
        <div className="lrm-skeleton h-4 w-14 mb-1.5" />
        <div className="lrm-skeleton h-1 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <button
        type="button"
        onClick={onClick}
        aria-label={ariaLabel || '运行故障'}
        className="group relative w-full text-left rounded-md px-2 py-1 lrm-press cursor-pointer border border-transparent hover:border-[var(--sem-err-border)] bg-[var(--sem-err-bg)]/50"
      >
        <div className="flex items-center justify-between gap-1">
          <span className="text-xs font-medium text-[var(--sem-err-fg)]">故障</span>
          {coverage && <span className="text-[10px] tabular-nums text-[var(--text-muted)]">{coverage}</span>}
        </div>
        <div className="mt-1 h-[2px] w-full rounded-full bg-[var(--sem-err-fg)]/40" />
      </button>
    );
  }

  if (score === null || score === undefined || !Number.isFinite(score)) {
    return (
      <div
        className="px-2 py-1 text-center text-xs tabular-nums text-[var(--text-muted)] select-none"
        title="缺测或无有效评分（严禁填 0）"
      >
        —
      </div>
    );
  }

  const clamped = Math.max(0, Math.min(100, score));
  const isZeroVal = isValidZero || score === 0;
  const scoreColor = isBest
    ? 'text-[var(--best-cell)] font-bold'
    : isZeroVal
      ? 'text-[var(--sem-zero-fg)] font-semibold'
      : 'text-[var(--text-primary)] font-semibold';
  const barColor = isBest
    ? 'var(--best-cell)'
    : isZeroVal
      ? 'var(--sem-zero-fg)'
      : 'var(--brand-primary)';

  const innerContent = (
    <>
      <div className="flex items-baseline justify-between gap-1.5">
        <span className={`text-[13px] leading-tight tabular-nums ${scoreColor}`}>{fmtPct(score)}</span>
        {deltaPp !== undefined && deltaPp !== null && Number.isFinite(deltaPp) && (
          <span
            className={`text-[10px] font-medium tabular-nums ${
              deltaPp > 0
                ? 'text-[var(--sem-pass-fg)]'
                : deltaPp < 0
                  ? 'text-[var(--sem-err-fg)]'
                  : 'text-[var(--text-muted)]'
            }`}
          >
            {fmtDeltaPP(deltaPp)}
          </span>
        )}
      </div>

      <div className="mt-0.5 flex items-center justify-between gap-1 text-[10px] text-[var(--text-muted)]">
        <span className="truncate">{subtitle || (isZeroVal ? '有效 0 分' : '')}</span>
        {coverage && <span className="tabular-nums shrink-0">{coverage}</span>}
      </div>

      <div className="mt-1 h-[2px] w-full rounded-full bg-[var(--border-subtle)] overflow-hidden">
        <div
          className="h-full rounded-full lrm-bar-grow"
          style={{ width: `${clamped}%`, backgroundColor: barColor }}
        />
      </div>
    </>
  );

  return (
    <div className="relative group min-w-[96px]">
      {onClick ? (
        <button
          type="button"
          onClick={onClick}
          aria-label={ariaLabel || `得分 ${fmtPct(score)}`}
          className="w-full text-left rounded-md px-2 py-1 lrm-press border border-transparent transition-colors cursor-pointer hover:bg-[var(--bg-elevated)] hover:border-[var(--border-subtle)] active:bg-[var(--brand-tint)]"
        >
          {innerContent}
        </button>
      ) : (
        <div className="w-full text-left rounded-md px-2 py-1 border border-transparent">
          {innerContent}
        </div>
      )}

      {breakdown && breakdown.length > 0 && (
        <div ref={breakdownLayer} {...dismissBreakdown} className="absolute right-1 top-1 z-20">
          <button
            type="button"
            ref={breakdownTrigger}
            onClick={(e) => {
              e.stopPropagation();
              setShowBreakdown((v) => !v);
            }}
            title="查看子指标拆解"
            aria-expanded={showBreakdown}
            aria-controls={showBreakdown ? panelId : undefined}
            className="rounded p-0.5 text-[var(--text-secondary)] hover:bg-[var(--bg-surface)] hover:text-[var(--brand-primary)] transition-opacity cursor-pointer pointer-fine:opacity-0 pointer-fine:group-hover:opacity-100 focus-visible:opacity-100 aria-expanded:opacity-100"
          >
            <SlidersHorizontal className="w-3 h-3" />
          </button>
          <DismissiblePanel
            open={showBreakdown}
            id={panelId}
            tabIndex={-1}
            onClick={(e) => e.stopPropagation()}
            className="lrm-glass absolute right-0 top-6 z-40 w-52 rounded-lg p-2.5 text-xs"
          >
            <div className="flex items-center justify-between mb-1.5 pb-1 border-b border-[var(--border-subtle)]">
              <span className="font-semibold text-[var(--text-primary)]">子指标拆解</span>
              <button
                type="button"
                onClick={() => {
                  setShowBreakdown(false);
                  breakdownTrigger.current?.focus({ preventScroll: true });
                }}
                className="text-[10px] text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer"
              >
                关闭
              </button>
            </div>
            <div className="space-y-1">
              {breakdown.map((b) => (
                <div key={b.label} className="flex items-center justify-between gap-2">
                  <span className="text-[var(--text-secondary)] truncate">{b.label}</span>
                  <span className="font-mono tabular-nums text-[var(--text-primary)]">{b.value}</span>
                </div>
              ))}
            </div>
          </DismissiblePanel>
        </div>
      )}
    </div>
  );
}
