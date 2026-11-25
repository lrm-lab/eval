import React from 'react';
import { ArrowDownRight, ArrowUpRight, HelpCircle, ShieldAlert, ShieldCheck } from 'lucide-react';
import { fmtDeltaPP, fmtRelPct } from '../../lib/format';

export interface GainGuardrailPillProps {
  mode: 'gain' | 'guardrail';
  status: 'pass' | 'regressed' | 'undeterminable';
  deltaPp?: number | null;
  relPct?: number | null;
  isLoading?: boolean;
  reason?: string;
}

/**
 * Bespoke UI Element #3: GainGuardrailPill (v2.2.1 01-1 §2.1.6)
 * Strictly differentiates Agentic optimize_gain (arrow + pp / rel%) vs
 * Text/Vision non_regression_guardrail (3-state Shield: Pass / Regressed / Undeterminable).
 */
export function GainGuardrailPill({
  mode,
  status,
  deltaPp,
  relPct,
  isLoading = false,
  reason,
}: GainGuardrailPillProps) {
  if (isLoading) {
    return <div className="lrm-skeleton h-6 w-24 rounded-full" aria-busy="true" />;
  }

  if (status === 'undeterminable') {
    return (
      <span
        title={reason || '缺测或不可比，无法判定'}
        className="inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium border border-[var(--sem-unscored-border)] bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)]"
      >
        <HelpCircle className="w-3.5 h-3.5 shrink-0" />
        <span>无法判定</span>
      </span>
    );
  }

  if (mode === 'gain') {
    const positive = (deltaPp ?? 0) >= 0;
    return (
      <span
        title={reason}
        className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold tabular-nums border ${
          positive
            ? 'border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
            : 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]'
        }`}
      >
        {positive ? <ArrowUpRight className="w-3.5 h-3.5 shrink-0" /> : <ArrowDownRight className="w-3.5 h-3.5 shrink-0" />}
        <span>{fmtDeltaPP(deltaPp)}</span>
        {relPct !== null && relPct !== undefined && (
          <span className="opacity-80 font-normal">({fmtRelPct(relPct)})</span>
        )}
      </span>
    );
  }

  const isPass = status === 'pass';
  return (
    <span
      title={reason}
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold tabular-nums border ${
        isPass
          ? 'border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
          : 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]'
      }`}
    >
      {isPass ? <ShieldCheck className="w-3.5 h-3.5 shrink-0" /> : <ShieldAlert className="w-3.5 h-3.5 shrink-0" />}
      <span>{isPass ? '护栏维持' : '触及退步'}</span>
      {deltaPp !== null && deltaPp !== undefined && <span className="opacity-85">{fmtDeltaPP(deltaPp)}</span>}
    </span>
  );
}
