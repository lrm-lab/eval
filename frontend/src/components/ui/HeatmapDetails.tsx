import { ArrowRight, GitCompareArrows } from 'lucide-react';
import { Link } from 'react-router-dom';
import {
  changeState, datasetName, groupedCompareHref, harnessName, historicalCompareHref, historyReason, modelName, referenceBadge,
  type HistoryItem, type MapCell,
} from '../../lib/evaluationMap';
import { fmtDateShort, fmtPct, runScopeLabel } from '../../lib/format';

const linkClass = 'inline-flex min-h-9 min-w-0 items-center gap-1.5 text-sm font-semibold text-[var(--brand-primary)] no-underline hover:underline';

function batchHref(batchId: string) {
  return `/eval/batches/${encodeURIComponent(batchId)}`;
}

function recordedTime(iso: string | null | undefined, source?: string | null) {
  if (!iso) return '结束时间未登记';
  return fmtDateShort(iso) + (source === 'latest_trial_finished_at' ? '（末次试验结束）' : ' UTC');
}

export function HeatmapDetails({ cell, history, pending, failed, onRetry }: {
  cell: MapCell;
  history?: HistoryItem;
  pending: boolean;
  failed: boolean;
  onRetry: () => void;
}) {
  const change = changeState(history, pending);
  const compare = history && historicalCompareHref(history);
  const zeroBaseline = Boolean(history?.baseline_kind);
  const zeroReference = history?.baseline_kind === 'zero_reference';
  const showsBaseline = zeroBaseline || Boolean(compare);
  const reference = zeroBaseline ? null : referenceBadge(history);
  const grouped = groupedCompareHref(cell);
  const unavailable = history?.reason === 'BATCH_UNAVAILABLE' || history?.reason === 'RELEASE_GENERATION_MISMATCH';
  const scoredBatch = unavailable ? null : cell.latest_scored_batch;
  const latest = unavailable && cell.latest_batch?.batch_id === cell.latest_scored_batch?.batch_id ? null : cell.latest_batch;
  const candidate = unavailable ? latest : history?.candidate ?? scoredBatch ?? latest;
  const before = history?.baseline;
  const companion = [cell.checkpoint, cell.thinking_mode, cell.model_route === cell.model_canonical ? null : cell.model_route]
    .filter(Boolean).join(' · ');
  const datasetContext = [cell.dataset_version && `v${cell.dataset_version}`, cell.split].filter(Boolean).join(' · ');
  const scopeCounts = Object.entries(cell.published_run_counts_by_scope ?? {})
    .filter(([, count]) => count > 0).map(([scope, count]) => `${runScopeLabel(scope)} ${count}`);
  const settings = [
    cell.attempts_per_task != null ? `每题 ${cell.attempts_per_task} 次` : null,
    cell.timeout_seconds != null ? `限时 ${cell.timeout_seconds} 秒` : null,
  ].filter(Boolean);
  const hasGuardrailPreset = cell.historical_scores.some(({ model_col }) =>
    ['Qwen3_8-27B', 'Qwen3_8-27B_think', 'Qwen3_8-27B_Trained_ck133', 'Qwen3_8-27B_Trained_ck133_think'].includes(model_col));
  return (
    <div className="space-y-4 [overflow-wrap:anywhere]">
      <div>
        <h3 className="m-0 text-lg leading-snug font-bold">
          {cell.model_canonical ? <Link to={`/models/${encodeURIComponent(cell.model_canonical)}`} className="text-inherit no-underline hover:underline">
            {modelName(cell)}
          </Link> : modelName(cell)}
        </h3>
        {companion && <p className="mt-0.5 mb-0 text-xs text-[var(--text-secondary)]">{companion}</p>}
        <p className="mt-1 mb-0 text-sm font-semibold text-[var(--text-primary)]">
          {cell.harness ? <Link to={`/harnesses/${encodeURIComponent(cell.harness)}`} className="text-inherit no-underline hover:underline">
            {harnessName(cell)}
          </Link> : harnessName(cell)}
        </p>
        <p className="mt-1 mb-0 text-sm text-[var(--text-secondary)]">
          {datasetName(cell)}{datasetContext && ` · ${datasetContext}`}
        </p>
      </div>

      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-[var(--text-secondary)]">协议 / 指标</dt>
        <dd className="m-0">{[cell.scoring_protocol, cell.metric_name].filter(Boolean).join(' / ') || '未登记'}</dd>
        {cell.runner && <><dt className="text-[var(--text-secondary)]">Runner</dt><dd className="m-0">{cell.runner}</dd></>}
        {settings.length > 0 && <><dt className="text-[var(--text-secondary)]">运行设置</dt><dd className="m-0">{settings.join(' · ')}</dd></>}
        <dt className="text-[var(--text-secondary)]">题库已登记</dt>
        <dd className="m-0">{cell.registered_task_count == null ? '题量未登记' : `${cell.registered_task_count.toLocaleString()} 道`}</dd>
        <dt className="text-[var(--text-secondary)]">评测次数</dt>
        <dd className="m-0">
          {cell.published_run_count} 次已发布{cell.running_count > 0 && ` · ${cell.running_count} 次运行中`}
          {scopeCounts.length > 0 && <span className="block text-[var(--text-secondary)]">{scopeCounts.join(' · ')}</span>}
        </dd>
      </dl>

      {cell.kind === 'historical_summary' ? (
        <div className="rounded-lg bg-[var(--bg-elevated)] p-3">
          <p className="mt-0 mb-2 text-sm font-bold">历史汇总成绩</p>
          <ul className="m-0 list-none space-y-1 p-0 text-xs">
            {cell.historical_scores.map((score) => (
              <li key={score.model_col} className="flex justify-between gap-2">
                <span>{score.model_col}</span>
                <span className="shrink-0 font-semibold tabular-nums">{score.score == null ? '缺少评分' : fmtPct(score.score)}</span>
              </li>
            ))}
          </ul>
          {cell.historical_scores.some((score) => score.denominator == null) && (
            <p className="mt-2 mb-0 text-xs text-[var(--text-secondary)]">有效题分母未登记</p>
          )}
        </div>
      ) : (
        <section aria-label="前后成绩" className="rounded-lg bg-[var(--bg-elevated)] p-3">
          <div className="flex items-baseline justify-between gap-2">
            <h4 className="m-0 text-sm font-bold">{reference ?? (zeroReference ? '对照 0 分基线' : zeroBaseline ? '首次有效评测' : compare ? '共同有效题得分' : '本次成绩')}</h4>
            {change.value && <span data-tone={change.tone} className="heatmap-change font-bold tabular-nums">
              {change.symbol} {change.value}
            </span>}
          </div>
          {compare && (
            <p className="mt-1 mb-3 text-xs text-[var(--text-secondary)]">
              共同有效 {history?.common_valid ?? '—'} 题 · 共同覆盖 {history?.common_covered ?? '—'} 题
            </p>
          )}
          {reference && (
            <p className="mt-1 mb-3 text-xs text-[var(--text-secondary)]">
              {history?.reference_label}{history?.reference_is_partial && ' · 有批次未完成，分差以当前已计分的共同题为准'}
            </p>
          )}
          {zeroReference && (
            <p className="m-0 mb-3 text-xs text-[var(--text-secondary)]">{historyReason(cell, history)}，本次成绩对照 0 分。</p>
          )}
          {candidate && (
            <div className={showsBaseline ? 'grid grid-cols-2 gap-3' : ''}>
              {showsBaseline && (
                <div>
                  <p className="m-0 text-xs text-[var(--text-secondary)]">{zeroBaseline ? '起始基线' : '前次'}</p>
                  <strong className="text-base tabular-nums">{fmtPct(history?.baseline_score)}</strong>
                  {before && <p className="m-0 text-[11px] text-[var(--text-secondary)]">{recordedTime(before.recorded_at, before.recorded_at_source)}</p>}
                </div>
              )}
              <div>
                <p className="m-0 text-xs text-[var(--text-secondary)]">
                  {showsBaseline ? '本次' : scoredBatch ? '最新已出分批次' : '最新可见批次'}
                </p>
                <strong className="text-base tabular-nums">
                  {showsBaseline ? fmtPct(history?.candidate_score) : fmtPct((scoredBatch ?? latest)?.score)}
                </strong>
                <p className="m-0 text-[11px] text-[var(--text-secondary)]">{recordedTime(candidate.recorded_at, candidate.recorded_at_source)}</p>
                {!compare && (scoredBatch ?? latest)?.score_kind === 'official_summary' && (
                  <p className="m-0 text-xs text-[var(--text-secondary)]">官方汇总</p>
                )}
              </div>
            </div>
          )}
          {latest && (
            <p className="mt-2 mb-0 text-xs text-[var(--text-secondary)]">
              {runScopeLabel(latest.run_scope)}
              {latest.recorded != null && ` · 已记录 ${latest.recorded}${latest.planned != null ? ` / ${latest.planned}` : ''} 条`}
            </p>
          )}
        </section>
      )}
      {!change.value && (
        <p className="m-0 text-sm text-[var(--text-secondary)]" role="status">
          {pending ? '正在核对前次成绩…' : failed ? (
            <>历史分差加载失败。<button type="button" onClick={onRetry} className="cursor-pointer font-semibold text-[var(--brand-primary)]">重试</button></>
          ) : unavailable ? (
            <>{historyReason(cell, history)}。<button type="button" onClick={onRetry} className="cursor-pointer font-semibold text-[var(--brand-primary)]">刷新记录</button></>
          ) : historyReason(cell, history)}
        </p>
      )}

      <nav aria-label="评测证据" className="flex flex-wrap gap-x-4 border-t border-[var(--border-subtle)] pt-1">
        {before && compare && <Link className={linkClass} to={batchHref(before.batch_id)}>前次批次 <ArrowRight className="size-3.5" aria-hidden /></Link>}
        {candidate && <Link className={linkClass} to={batchHref(candidate.batch_id)}>{compare ? '本次批次' : candidate.batch_name} <ArrowRight className="size-3.5 shrink-0" aria-hidden /></Link>}
        {latest && latest.batch_id !== candidate?.batch_id && (
          <Link className={linkClass} to={batchHref(latest.batch_id)}>最近可见批次 <ArrowRight className="size-3.5" aria-hidden /></Link>
        )}
        {compare && <Link className={linkClass} to={compare}><GitCompareArrows className="size-4 shrink-0" aria-hidden /> 同题逐题对比</Link>}
        {grouped && !unavailable && <Link className={linkClass} to={grouped}>{cell.model_key && cell.harness_key && cell.kind === 'trials' ? '同模型下对比 Harness' : '同题分组对比'} <ArrowRight className="size-3.5" aria-hidden /></Link>}
        {hasGuardrailPreset && <Link className={linkClass} to={`/eval/compare?preset=qwen38-3track-ck133&focus_benchmark=${encodeURIComponent(cell.benchmark_slug ?? '')}`}>
          查看三线提分与护栏 <ArrowRight className="size-3.5" aria-hidden />
        </Link>}
      </nav>
    </div>
  );
}
