import { ChevronDown, ShieldCheck } from 'lucide-react';
import type { CompareIssue, GainGuardrailVerdict, VerdictItem } from '../../types/api';
import { GainGuardrailPill } from './GainGuardrailPill';

export const compareIssueLabel: Record<CompareIssue, string> = {
  not_integrated: '未接入评测',
  pending_dataset_version: '题库版本待确认',
  different_scoring_protocol: '评分口径不同',
  different_dataset_release: '数据集版本不同',
  no_common_valid_tasks: '没有共同有效题',
  missing_comparable_scores: '缺少可比成绩',
};

function verdictLabel(item: VerdictItem) {
  const reason = item.reason || '';
  if (item.reason_code && compareIssueLabel[item.reason_code]) return compareIssueLabel[item.reason_code];
  // Frozen links retain the reason text stored before structured reasons were available.
  if (reason.includes('未接入评测')) return compareIssueLabel.not_integrated;
  if (reason.includes('待固定版本')) return compareIssueLabel.pending_dataset_version;
  return compareIssueLabel.missing_comparable_scores;
}

function VerdictRow({ item, mode }: { item: VerdictItem; mode: 'gain' | 'guardrail' }) {
  const isUnmeasured = item.status === 'undeterminable';
  const reason = item.reason?.replace(/[，,]无法判定(?:是否退步)?[。]?$/, '。');
  const label = isUnmeasured ? verdictLabel(item) : undefined;
  const canExplain = Boolean(isUnmeasured && reason && reason.replace(/[。.]$/, '') !== label);
  const row = (
    <>
      <span className="min-w-0 [overflow-wrap:anywhere]">
        <span className="font-semibold text-[var(--text-primary)]">{item.benchmark_slug}</span>
        {!isUnmeasured && <span className="ml-1 text-[11px] text-[var(--text-muted)]">{mode === 'gain' ? item.coverage : item.track_slug}</span>}
      </span>
      <span className="ml-auto shrink-0">
        <GainGuardrailPill mode={mode} status={item.status} deltaPp={item.delta_pp} relPct={item.rel_pct} reason={reason} statusLabel={label} />
      </span>
    </>
  );
  const card = 'min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)]';
  if (!canExplain) return <div className={card + ' flex items-center gap-2 px-3 py-2'}>{row}</div>;

  return (
    <details className={card + ' group/verdict'}>
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-3 py-2 [&::-webkit-details-marker]:hidden">
        {row}
        <ChevronDown aria-hidden="true" className="size-3.5 shrink-0 text-[var(--text-secondary)] group-open/verdict:rotate-180" />
      </summary>
      <p className="m-0 px-3 pb-3 pt-1 leading-relaxed text-[var(--text-secondary)]">{reason}</p>
    </details>
  );
}

export function CompareVerdict({ verdict }: { verdict: GainGuardrailVerdict }) {
  return (
    <section aria-labelledby="compare-verdict-title" className="space-y-3 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
      <h2 id="compare-verdict-title" className="m-0 flex items-center gap-1.5 text-xs font-bold text-[var(--text-primary)]">
        <ShieldCheck aria-hidden="true" className="size-4 shrink-0 text-[var(--brand-primary)]" />
        三线提分与防退化护栏（阈值 {verdict.regression_threshold_pp} pp）
      </h2>
      <div className="grid grid-cols-1 items-start gap-3 text-xs md:grid-cols-2">
        {verdict.agentic_gains.map((item) => (
          <VerdictRow key={`gain-${item.benchmark_slug}-${item.dataset_release_id ?? ''}`} mode="gain" item={item} />
        ))}
        {verdict.guardrails.map((item) => (
          <VerdictRow key={`guardrail-${item.benchmark_slug}-${item.dataset_release_id ?? ''}`} mode="guardrail" item={item} />
        ))}
      </div>
    </section>
  );
}
