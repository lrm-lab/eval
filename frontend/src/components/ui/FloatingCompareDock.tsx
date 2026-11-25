import React from 'react';
import { useNavigate } from 'react-router-dom';
import { AnimatePresence, m } from 'motion/react';
import { ArrowRight, Crown, Trash2, X } from 'lucide-react';
import { useAppStore } from '../../lib/store';
import { SPRING_SNAPPY } from '../motion/primitives';

/**
 * Bespoke UI Element #6: FloatingCompareDock (v2.2.1 01-1 §2.1.6)
 * Centered bottom glass pill dock rising smoothly when 1..8 items are selected,
 * with Crown baseline switching, reorder, and 1-click launch into /eval/compare.
 */
export function FloatingCompareDock() {
  const navigate = useNavigate();
  const basket = useAppStore((s) => s.compareBasket);
  const baselineId = useAppStore((s) => s.baselineId);
  const setBaselineId = useAppStore((s) => s.setBaselineId);
  const removeFromCompare = useAppStore((s) => s.removeFromCompare);
  const clearCompare = useAppStore((s) => s.clearCompare);
  const reduced = useAppStore((s) => s.reducedMotion);

  if (basket.length === 0) return null;

  const canCompare = basket.length >= 2;

  return (
    <AnimatePresence>
      <m.div
        initial={reduced ? false : { opacity: 0, y: 28, scale: 0.96 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={reduced ? undefined : { opacity: 0, y: 24, scale: 0.96 }}
        transition={SPRING_SNAPPY}
        className="fixed bottom-5 left-1/2 -translate-x-1/2 z-40 max-w-[92vw]"
        role="region"
        aria-label="悬浮对比停靠栏"
      >
        <div className="lrm-glass rounded-2xl px-3.5 py-2.5 flex flex-wrap items-center gap-2.5">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-[var(--text-primary)] pr-2 border-r border-[var(--border-subtle)]">
            <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-[var(--brand-primary)] text-white text-[11px] tabular-nums">
              {basket.length}
            </span>
            <span>对比篮</span>
          </div>

          <div className="flex flex-wrap items-center gap-1.5 max-w-[56vw]">
            {basket.map((item) => {
              const isBase = item.id === baselineId;
              return (
                <div
                  key={item.id}
                  className={`inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs border transition-colors ${
                    isBase
                      ? 'border-[var(--rank-gold)] bg-[var(--sem-zero-bg)] text-[var(--text-primary)]'
                      : 'border-[var(--border-subtle)] bg-[var(--bg-surface)] text-[var(--text-secondary)]'
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => setBaselineId(item.id)}
                    title={isBase ? '当前基线 (Baseline)' : '点击设为基线'}
                    className={`cursor-pointer ${isBase ? 'text-[var(--rank-gold)]' : 'text-[var(--text-muted)] hover:text-[var(--rank-gold)]'}`}
                  >
                    <Crown className="w-3.5 h-3.5" />
                  </button>
                  <span className="max-w-[150px] truncate font-medium text-[var(--text-primary)]">{item.alias}</span>
                  <button
                    type="button"
                    onClick={() => removeFromCompare(item.id)}
                    aria-label={`移除 ${item.alias}`}
                    className="text-[var(--text-muted)] hover:text-[var(--sem-err-fg)] cursor-pointer"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              );
            })}
          </div>

          <div className="flex items-center gap-1.5 pl-2 border-l border-[var(--border-subtle)]">
            <button
              type="button"
              onClick={clearCompare}
              title="清空对比篮"
              className="rounded-lg p-1.5 text-[var(--text-muted)] hover:text-[var(--sem-err-fg)] hover:bg-[var(--bg-elevated)] cursor-pointer"
            >
              <Trash2 className="w-4 h-4" />
            </button>
            <button
              type="button"
              disabled={!canCompare}
              onClick={() => navigate('/eval/compare?fromDock=1')}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-xs font-semibold lrm-press ${
                canCompare
                  ? 'bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] text-white cursor-pointer'
                  : 'bg-[var(--bg-elevated)] text-[var(--text-muted)] cursor-not-allowed'
              }`}
            >
              <span>发起对比 ({basket.length})</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </m.div>
    </AnimatePresence>
  );
}
