import React from 'react';
import { AlertCircle, CheckCircle2, Info, Lock, RefreshCw, X } from 'lucide-react';
import { useAppStore } from '../../lib/store';

export function TableSkeleton({ rows = 8, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden" aria-busy="true">
      <div className="grid gap-3 px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)]" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
        {Array.from({ length: cols }).map((_, i) => (
          <div key={i} className="lrm-skeleton h-4 w-20" />
        ))}
      </div>
      <div className="divide-y divide-[var(--border-subtle)]">
        {Array.from({ length: rows }).map((_, r) => (
          <div key={r} className="grid gap-3 px-4 py-2.5" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
            {Array.from({ length: cols }).map((_, c) => (
              <div key={c} className="lrm-skeleton h-4 w-full" />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function CardGridSkeleton({ count = 4 }: { count?: number }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4" aria-busy="true">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
          <div className="lrm-skeleton h-3.5 w-24 mb-3" />
          <div className="lrm-skeleton h-7 w-20 mb-2" />
          <div className="lrm-skeleton h-3 w-32" />
        </div>
      ))}
    </div>
  );
}

export function ChartSkeleton({ height = 380 }: { height?: number }) {
  return (
    <div
      className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 flex flex-col justify-between"
      style={{ height }}
      aria-busy="true"
    >
      <div className="flex items-center justify-between">
        <div className="lrm-skeleton h-4 w-48" />
        <div className="lrm-skeleton h-6 w-28" />
      </div>
      <div className="lrm-skeleton flex-1 my-4 w-full" />
      <div className="lrm-skeleton h-3 w-64" />
    </div>
  );
}

export function StateNoticeCard({
  kind = 'empty',
  title,
  description,
  onRetry,
  action,
}: {
  kind?: 'empty' | 'error' | 'forbidden';
  title: string;
  description?: string;
  onRetry?: () => void;
  action?: React.ReactNode;
}) {
  const Icon = kind === 'error' ? AlertCircle : kind === 'forbidden' ? Lock : Info;
  const fg =
    kind === 'error'
      ? 'text-[var(--sem-err-fg)]'
      : kind === 'forbidden'
        ? 'text-[var(--sem-zero-fg)]'
        : 'text-[var(--text-secondary)]';
  return (
    <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-6 py-10 text-center">
      <Icon className={`w-8 h-8 mx-auto mb-2.5 ${fg}`} />
      <h3 className="m-0 text-sm font-semibold text-[var(--text-primary)]">{title}</h3>
      {description && <p className="m-0 mt-1.5 text-xs text-[var(--text-secondary)] max-w-lg mx-auto">{description}</p>}
      <div className="mt-4 flex items-center justify-center gap-2">
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] hover:border-[var(--border-strong)] cursor-pointer"
          >
            <RefreshCw className="w-3.5 h-3.5" /> 重试
          </button>
        )}
        {action}
      </div>
    </div>
  );
}

export function ToastContainer() {
  const toasts = useAppStore((s) => s.toasts);
  const dismiss = useAppStore((s) => s.dismissToast);
  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-16 right-4 z-50 flex flex-col gap-2 max-w-sm">
      {toasts.map((t) => {
        const icon =
          t.tone === 'success' ? (
            <CheckCircle2 className="w-4 h-4 text-[var(--sem-pass-fg)] shrink-0" />
          ) : t.tone === 'error' ? (
            <AlertCircle className="w-4 h-4 text-[var(--sem-err-fg)] shrink-0" />
          ) : (
            <Info className="w-4 h-4 text-[var(--brand-primary)] shrink-0" />
          );
        return (
          <div key={t.id} className="lrm-glass rounded-xl px-3.5 py-2.5 flex items-center gap-2.5 text-xs text-[var(--text-primary)]">
            {icon}
            <span className="flex-1">{t.text}</span>
            <button
              type="button"
              onClick={() => dismiss(t.id)}
              className="text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
