import React, { useState } from 'react';
import type { AttemptDot } from '../../types/api';
import { fmtDuration } from '../../lib/format';

export interface MultiAttemptPillProps {
  dots: AttemptDot[];
  isLoading?: boolean;
  onSelectTrial?: (trialId: string) => void;
}

/**
 * Bespoke UI Element #2: MultiAttemptPill (v2.2.1 01-1 §2.1.6)
 * Compact semantic dot capsule (Pass / Valid Zero / Error / Unscored) with hover summary & click -> Trial.
 */
export function MultiAttemptPill({ dots, isLoading = false, onSelectTrial }: MultiAttemptPillProps) {
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

  if (isLoading) {
    return <div className="lrm-skeleton h-6 w-28 rounded-full" aria-busy="true" />;
  }

  if (!dots || dots.length === 0) {
    return (
      <span className="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] bg-[var(--sem-unscored-bg)] text-[var(--text-muted)]">
        —
      </span>
    );
  }

  const dotMeta = (d: AttemptDot) => {
    if (d.status === 'scored' && d.reward !== null && d.reward >= 1.0) {
      return { color: 'var(--sem-pass-fg)', label: `Pass (${d.reward})` };
    }
    if (d.status === 'scored' && d.reward !== null) {
      return { color: 'var(--sem-zero-fg)', label: `有效 0 分 (${d.reward})` };
    }
    if (d.status === 'unscored') {
      return { color: 'var(--sem-unscored-fg)', label: '未评分 (unscored)' };
    }
    const errLabel = d.exception_type || d.failure_kind || d.status;
    return { color: 'var(--sem-err-fg)', label: `故障 (${errLabel})` };
  };

  const activeDot = hoveredIdx !== null ? dots[hoveredIdx] : null;

  return (
    <div className="relative inline-flex items-center">
      <div className="inline-flex items-center gap-1 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1">
        {dots.map((d, idx) => {
          const meta = dotMeta(d);
          return (
            <button
              key={d.trial_id}
              type="button"
              onMouseEnter={() => setHoveredIdx(idx)}
              onMouseLeave={() => setHoveredIdx(null)}
              onFocus={() => setHoveredIdx(idx)}
              onBlur={() => setHoveredIdx(null)}
              onClick={(e) => {
                e.stopPropagation();
                onSelectTrial?.(d.trial_id);
              }}
              aria-label={`尝试 #${idx + 1}: ${meta.label}`}
              className="w-2.5 h-2.5 rounded-full transition-transform duration-100 hover:scale-150 focus-visible:scale-150 cursor-pointer"
              style={{ backgroundColor: meta.color }}
            />
          );
        })}
      </div>

      {activeDot && hoveredIdx !== null && (
        <div className="lrm-glass pointer-events-none absolute left-0 bottom-full mb-1.5 z-30 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-[11px]">
          <div className="font-semibold text-[var(--text-primary)]">
            Attempt #{hoveredIdx + 1} · {dotMeta(activeDot).label}
          </div>
          <div className="text-[var(--text-secondary)] tabular-nums">
            耗时 {fmtDuration(activeDot.duration_sec)} · 点击进入 Trial 显微镜
          </div>
        </div>
      )}
    </div>
  );
}
