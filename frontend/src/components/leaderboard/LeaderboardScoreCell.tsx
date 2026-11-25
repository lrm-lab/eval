import { fmtPct } from '../../lib/format';
import type { LeaderboardBenchmarkCell } from '../../types/api';

export function leaderboardScopeLabel(scope: string) {
  switch (scope) {
    case 'full_split': return '正式全量';
    case 'subset': return '子集';
    case 'smoke': return '抽测';
    case 'reference': return '参考';
    default: return scope || '未知范围';
  }
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
  const { score, scored, planned } = cell;
  const historical = cell.scoring_protocol === 'legacy-20261120-csv';
  const official = cell.official_status === 'official_verified';
  const hasCounts = scored != null && planned != null && planned > 0;
  const partial = !official && hasCounts && scored < planned;
  const coverage = official && planned != null && planned > 0
    ? `官方分母 ${planned}`
    : hasCounts ? `${scored}/${planned} 次计分` : '分母未提供';
  const status = score == null ? '未评分' : partial ? '部分计分' : historical ? '历史基线' : score === 0 ? '有效 0 分' : '';
  const scoreColor = score == null ? 'text-[var(--text-muted)]'
    : isBest ? 'text-[var(--best-cell)]' : 'text-[var(--text-primary)]';

  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`${score == null ? '未评分' : `得分 ${fmtPct(score)}`}，${cell.raw_dataset_name || cell.benchmark_slug}，${leaderboardScopeLabel(cell.run_scope)}，${coverage}，查看成绩详情`}
      className="block w-full min-w-24 cursor-pointer rounded-md px-1.5 py-1 text-left hover:bg-[var(--bg-elevated)] focus-visible:outline-2 focus-visible:outline-[var(--brand-primary)]"
    >
      <span className={`block text-[13px] font-semibold tabular-nums ${scoreColor}`}>{fmtPct(score)}</span>
      <span className="mt-0.5 block text-[11px] tabular-nums text-[var(--text-secondary)]">{coverage}</span>
      {status && (
        <span className={`block text-[10px] ${partial ? 'text-[var(--sem-zero-fg)]' : 'text-[var(--text-muted)]'}`}>{status}</span>
      )}
    </button>
  );
}
