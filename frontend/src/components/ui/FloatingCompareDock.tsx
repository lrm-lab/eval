import React from 'react';
import { useNavigate } from 'react-router-dom';
import { AnimatePresence, m, useReducedMotion } from 'motion/react';
import { ArrowRight, Crown, Trash2, X } from 'lucide-react';
import { useAppStore } from '../../lib/store';
import { SPRING_SNAPPY } from '../motion/primitives';

export function FloatingCompareDock() {
  const navigate = useNavigate();
  const basket = useAppStore((s) => s.compareBasket);
  const baselineId = useAppStore((s) => s.baselineId);
  const setBaselineId = useAppStore((s) => s.setBaselineId);
  const removeFromCompare = useAppStore((s) => s.removeFromCompare);
  const clearCompare = useAppStore((s) => s.clearCompare);
  const reducedMotion = useAppStore((s) => s.reducedMotion);
  const prefersReducedMotion = useReducedMotion();
  const reduced = reducedMotion || prefersReducedMotion;
  const canCompare = basket.length >= 2;

  return (
    <div className="pointer-events-none fixed inset-x-3 bottom-[max(0.75rem,env(safe-area-inset-bottom))] z-40 flex justify-center sm:bottom-5">
      <AnimatePresence>
        {basket.length > 0 && (
          <m.div
            key="compare-dock"
            initial={reduced ? false : { opacity: 0, y: 36 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduced ? undefined : { opacity: 0, y: 24 }}
            transition={reduced ? { duration: 0 } : SPRING_SNAPPY}
            className="lrm-glass pointer-events-auto flex w-full max-w-4xl flex-wrap items-center justify-between gap-x-3 gap-y-2 rounded-2xl px-3 py-2.5 sm:w-fit sm:flex-nowrap"
            role="region"
            aria-label="悬浮对比停靠栏"
          >
            <div className="flex shrink-0 items-center gap-2 text-sm font-semibold text-[var(--text-primary)]">
              <span>对比</span>
              <span aria-live="polite" className="inline-flex h-6 w-6 items-center justify-center rounded-md bg-[var(--brand-tint)] text-xs tabular-nums text-[var(--brand-primary)]">
                <m.span
                  key={basket.length}
                  animate={reduced ? undefined : { y: [0, -4, 0], scale: [0.9, 1.18, 1] }}
                  transition={{ duration: 0.26, ease: 'easeOut' }}
                >
                  {basket.length}
                </m.span>
              </span>
            </div>

            <ul className="order-last flex max-h-[34vh] min-w-0 basis-full flex-wrap items-center gap-1.5 overflow-y-auto p-1 sm:order-none sm:basis-auto">
              <AnimatePresence propagate>
                {basket.map((item) => {
                  const isBase = item.id === baselineId;
                  return (
                    <m.li
                      key={item.id}
                      initial={reduced ? false : { opacity: 0, y: 7, scale: 0.84 }}
                      animate={{ opacity: 1, y: 0, scale: 1 }}
                      exit={reduced ? undefined : { opacity: 0, y: 3, scale: 0.8, transition: { duration: 0.14 } }}
                      transition={reduced ? { duration: 0 } : SPRING_SNAPPY}
                      className={`inline-flex max-w-full items-center gap-1 rounded-lg border px-1 py-0.5 text-xs ${
                        isBase
                          ? 'border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)]'
                          : 'border-[var(--border-subtle)] bg-[var(--bg-surface)]'
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => setBaselineId(item.id)}
                        aria-label={`将 ${item.alias} 设为基线`}
                        aria-pressed={isBase}
                        title={isBase ? '当前基线' : '设为基线'}
                        className={`shrink-0 cursor-pointer rounded-md p-1 hover:bg-[var(--bg-elevated)] ${isBase ? 'text-[var(--rank-gold)]' : 'text-[var(--text-secondary)] hover:text-[var(--rank-gold)]'}`}
                      >
                        <Crown className="size-3.5" />
                      </button>
                      <span className="max-w-[150px] truncate font-medium text-[var(--text-primary)]" title={item.alias}>{item.alias}</span>
                      <button
                        type="button"
                        onClick={() => removeFromCompare(item.id)}
                        aria-label={`移除 ${item.alias}`}
                        className="shrink-0 cursor-pointer rounded-md p-1 text-[var(--text-secondary)] hover:bg-[var(--sem-err-bg)] hover:text-[var(--sem-err-fg)]"
                      >
                        <X className="size-3.5" />
                      </button>
                    </m.li>
                  );
                })}
              </AnimatePresence>
            </ul>

            <div className="flex shrink-0 items-center gap-1.5 border-l border-[var(--border-subtle)] pl-2">
              <button
                type="button"
                onClick={clearCompare}
                aria-label="清空对比"
                title="清空对比"
                className="cursor-pointer rounded-lg p-2 text-[var(--text-secondary)] hover:bg-[var(--sem-err-bg)] hover:text-[var(--sem-err-fg)]"
              >
                <Trash2 className="size-4" />
              </button>
              <button
                type="button"
                disabled={!canCompare}
                onClick={() => navigate('/eval/compare?fromDock=1')}
                className={`group relative inline-flex items-center gap-1.5 overflow-hidden whitespace-nowrap rounded-lg px-3 py-2 text-xs font-semibold ${
                  canCompare
                    ? 'cursor-pointer bg-linear-to-r from-blue-700 to-indigo-700 text-white shadow-md shadow-indigo-950/25 hover:from-blue-600 hover:to-indigo-600'
                    : 'cursor-not-allowed bg-[var(--bg-elevated)] text-[var(--text-muted)]'
                }`}
              >
                {canCompare && !reduced && (
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-y-0 -left-1/2 w-1/2 -skew-x-12 bg-linear-to-r from-transparent via-white/35 to-transparent transition-transform duration-500 group-hover:translate-x-[350%]"
                  />
                )}
                <span className="relative">发起对比 ({basket.length})</span>
                <ArrowRight className="relative size-3.5" />
              </button>
            </div>
          </m.div>
        )}
      </AnimatePresence>
    </div>
  );
}
