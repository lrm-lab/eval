import { type ReactNode } from 'react';
import { fmtMetric, numberValue, type Tone } from './healthData';

const TONES: Record<Tone, string> = {
  ok: 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]',
  warning: 'bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]',
  error: 'bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]',
  neutral: 'bg-[var(--bg-elevated)] text-[var(--text-secondary)]',
};

export const HEALTH_BUTTON = 'inline-flex min-h-8 cursor-pointer items-center justify-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs font-medium text-[var(--text-primary)] hover:border-[var(--border-strong)] hover:bg-[var(--bg-elevated)] disabled:cursor-not-allowed disabled:opacity-50';
export const HEALTH_LINK = 'font-medium text-[var(--brand-primary)] underline-offset-2 hover:underline';

export function StatusTag({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md px-1.5 py-0.5 text-[11px] font-medium ${TONES[tone]}`}>
      <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-current" />
      {children}
    </span>
  );
}

export function HealthMeter({ label, value, cap, tone = 'ok' }: { label: string; value: unknown; cap: unknown; tone?: Tone }) {
  const amount = numberValue(value);
  const ceiling = numberValue(cap);
  if (amount === null || ceiling === null || ceiling === 0) {
    return <p className="m-0 text-xs text-[var(--text-muted)]">{label}用量或上限未报告</p>;
  }
  const ratio = (amount / ceiling) * 100;
  const pct = Math.min(100, ratio);
  const color = tone === 'error' || ratio > 100 ? 'var(--sem-err-fg)' : tone === 'warning' ? 'var(--sem-zero-fg)' : 'var(--brand-primary)';
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--bg-elevated)]"
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(pct)}
      aria-valuetext={fmtMetric(ratio, '%')}
    >
      <div
        className="h-full rounded-full motion-safe:transition-[width] motion-safe:duration-300"
        style={{ width: `${pct}%`, backgroundColor: color }}
      />
    </div>
  );
}

export function IndexSamples({ value }: { value: unknown }) {
  const reported: unknown[] = Array.isArray(value) ? value : [];
  const samples = reported.map(numberValue);
  const valid = samples.filter((sample): sample is number => sample !== null);
  if (!valid.length) {
    return <p className="m-0 text-xs text-[var(--text-muted)]">{reported.length ? '暂无有效时延样本' : '暂无时延样本'}</p>;
  }
  const maximum = Math.max(...valid, 1);
  const series = samples.map((sample) => sample === null ? '缺测' : fmtMetric(sample, ' ms')).join('、');
  return (
    <div className="space-y-1">
      <div role="img" aria-label={`热扫描耗时，从旧到新：${series}`} className="flex h-9 items-end gap-1 rounded-md bg-[var(--bg-elevated)] px-1.5 py-1" title={series}>
        {samples.map((sample, index) => (
          <div
            key={index}
            aria-hidden="true"
            className={`min-w-0 flex-1 rounded-t-sm ${sample === null ? 'bg-transparent' : index === samples.length - 1 ? 'bg-[var(--brand-primary)]' : 'bg-[var(--brand-primary)]/45'}`}
            style={{ height: sample === null ? 0 : `${Math.max(6, (sample / maximum) * 100)}%` }}
          />
        ))}
      </div>
      <div className="flex flex-wrap justify-between gap-x-1 text-[11px] tabular-nums text-[var(--text-muted)]">
        <span>{valid.length} 次实测{valid.length !== samples.length && ` · ${samples.length - valid.length} 次缺测`}</span>
        <span>峰值 {fmtMetric(Math.max(...valid), ' ms')}</span>
      </div>
    </div>
  );
}
