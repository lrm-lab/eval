import { fmtPct } from '../../lib/format';
import type { LeaderboardBenchmarkCell } from '../../types/api';

export function leaderboardScopeLabel(scope: string) {
  switch (scope) {
    case 'full_split': return '正式全量（计划）';
    case 'subset': return '子集';
    case 'smoke': return '抽测';
    case 'reference': return '参考';
    default: return scope || '未知范围';
  }
}

export function leaderboardScoreBasis({ score, scored, planned, official_status }: LeaderboardBenchmarkCell) {
  if (official_status === 'official_verified') {
    return { denominator: planned != null && planned > 0 ? `官方分母 ${planned}` : '分母未提供', recorded: null, partial: false };
  }
  // For non-official leaderboard cells the API's planned field counts deduplicated records.
  return {
    denominator: scored == null ? '分母未提供' : `${score == null || scored === 0 ? '有效评分' : '均值分母'} ${scored}`,
    recorded: planned == null ? null : `已记录 ${planned}`,
    partial: scored != null && planned != null && scored < planned,
  };
}

export function LeaderboardScoreCell({
  cell,
  isBest,
  onOpen,
}: {
  cell: LeaderboardBenchmarkCell;
  isBest: boolean;
  onOpen: () => void;
}) {
  const { score } = cell;
  const historical = cell.scoring_protocol === 'legacy-20261120-csv';
  const { denominator, recorded, partial } = leaderboardScoreBasis(cell);
  const basis = [denominator, recorded].filter(Boolean).join(' · ');
  const status = score == null ? '未评分' : partial ? '部分计分' : historical ? '历史基线' : score === 0 ? '有效 0 分' : '';
  const scoreColor = score == null ? 'text-[var(--text-muted)]'
    : isBest ? 'text-[var(--best-cell)]' : 'text-[var(--text-primary)]';

  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`${score == null ? '未评分' : `得分 ${fmtPct(score)}`}，${cell.raw_dataset_name || cell.benchmark_slug}，${leaderboardScopeLabel(cell.run_scope)}，${basis}，查看成绩详情`}
      className="block w-full min-w-24 cursor-pointer rounded-md px-1.5 py-1 text-left hover:bg-[var(--bg-elevated)] focus-visible:outline-2 focus-visible:outline-[var(--brand-primary)]"
    >
      <span className={`block text-[13px] font-semibold tabular-nums ${scoreColor}`}>{fmtPct(score)}</span>
      <span className="mt-0.5 flex flex-wrap gap-x-1.5 text-[11px] tabular-nums text-[var(--text-secondary)]">
        <span className="whitespace-nowrap">{denominator}</span>
        {recorded && <span className="whitespace-nowrap">{recorded}</span>}
      </span>
      {status && (
        <span className={`block text-[10px] ${partial ? 'text-[var(--sem-zero-fg)]' : 'text-[var(--text-muted)]'}`}>{status}</span>
      )}
    </button>
  );
}
