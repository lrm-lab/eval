import React from 'react';
import type { TokenTelemetry } from '../../types/api';
import { fmtTokens } from '../../lib/format';

/**
 * TokenBreakdownBar (v2.2.1 01-1 §2.2 & A-12)
 * ONLY draws mutually-exclusive 4-segment stacked bar when `semantics === 'mutually_exclusive_verified'`.
 * Otherwise renders honest side-by-side pills without misleading additive stacking.
 */
export function TokenBreakdownBar({ tokens }: { tokens?: TokenTelemetry | null }) {
  if (!tokens || (tokens.input === null && tokens.output === null && tokens.reasoning === null)) {
    return <span className="text-xs text-[var(--text-muted)] tabular-nums">—</span>;
  }

  if (
    tokens.semantics === 'mutually_exclusive_verified' &&
    tokens.input !== null &&
    tokens.output !== null &&
    tokens.cached !== null &&
    tokens.reasoning !== null
  ) {
    const netIn = Math.max(0, tokens.input - tokens.cached);
    const netOut = Math.max(0, tokens.output - tokens.reasoning);
    const total = Math.max(1, tokens.input + tokens.output);
    const segs = [
      { label: 'Input(Net)', val: netIn, color: '#2563EB' },
      { label: 'Cached', val: tokens.cached, color: '#06B6D4' },
      { label: 'Reasoning', val: tokens.reasoning, color: '#8B5CF6' },
      { label: 'Output(Net)', val: netOut, color: '#10B981' },
    ];
    return (
      <div
        className="min-w-[120px]"
        title={`已核验互斥 4 段：In ${fmtTokens(tokens.input)} (Cached ${fmtTokens(tokens.cached)}) · Out ${fmtTokens(tokens.output)} (Reason ${fmtTokens(tokens.reasoning)})`}
      >
        <div className="flex h-2 w-full rounded-full overflow-hidden bg-[var(--border-subtle)]">
          {segs.map((s) => (
            <div
              key={s.label}
              style={{ width: `${(s.val / total) * 100}%`, backgroundColor: s.color }}
              className="h-full"
            />
          ))}
        </div>
        <div className="mt-1 flex items-center justify-between text-[10px] tabular-nums text-[var(--text-secondary)]">
          <span>In {fmtTokens(tokens.input)}</span>
          <span>Out {fmtTokens(tokens.output)}</span>
        </div>
      </div>
    );
  }

  // Unverified containment semantics -> honest side-by-side pills (NEVER stacked!)
  return (
    <div
      className="flex flex-wrap items-center gap-1 text-[10px] tabular-nums"
      title="来源未核验 Token 包含语义，采用并排独立展示而非互斥堆叠"
    >
      {tokens.input !== null && (
        <span className="rounded px-1.5 py-0.5 bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-secondary)]">
          In {fmtTokens(tokens.input)}
        </span>
      )}
      {tokens.cached !== null && (
        <span className="rounded px-1.5 py-0.5 bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--sem-running-fg)]">
          Cache {fmtTokens(tokens.cached)}
        </span>
      )}
      {tokens.output !== null && (
        <span className="rounded px-1.5 py-0.5 bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-secondary)]">
          Out {fmtTokens(tokens.output)}
        </span>
      )}
      {tokens.reasoning !== null && (
        <span className="rounded px-1.5 py-0.5 bg-[var(--sem-legacy-bg)] border border-[var(--sem-legacy-border)] text-[var(--sem-legacy-fg)]">
          Think {fmtTokens(tokens.reasoning)}
        </span>
      )}
    </div>
  );
}
