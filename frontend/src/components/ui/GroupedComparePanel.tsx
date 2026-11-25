import { useId, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { ArrowRight, GitCompareArrows } from 'lucide-react';
import { ApiError, apiFetch } from '../../lib/api';
import { fmtDateShort, fmtDeltaPP, fmtPct, runScopeLabel } from '../../lib/format';
import { StateNoticeCard } from './Skeletons';
import type { DatasetReleaseDTO } from '../../types/api';

interface GroupedBatch {
  batch_id: string;
  batch_name: string;
  generation_id: string;
  run_scope: string;
  planned: number | null;
  recorded: number;
  scored: number;
  observed_unique_tasks?: number | null;
  batch_status: string | null;
  attempts_per_task: number | null;
  timeout_seconds: number | null;
  has_visible_task_scores: boolean;
  task_details_allowed: boolean;
  official_result: { metric: string; score: number; denominator: number | null } | null;
  finished_at: string | null;
  indexed_at: string | null;
}

interface HistoricalScore {
  model_col: string;
  score: number | null;
  raw_score: number | null;
  scale_type: string;
  denominator: number | null;
}

interface ComparisonGroup {
  group_id: string;
  model_key: string | null;
  harness_key: string | null;
  model_canonical: string | null;
  model_route: string | null;
  checkpoint: string | null;
  thinking_mode: string | null;
  harness: string | null;
  harness_version: string | null;
  runner: string | null;
  default_batch_id: string | null;
  batches: GroupedBatch[];
  historical_scores: HistoricalScore[];
}

interface ComparisonContext {
  context_id: string;
  dataset_release_id: string;
  dataset_version: string;
  split: string;
  scoring_protocol: string;
  metric_name: string;
  kind: 'trials' | 'historical_summary';
  groups: ComparisonGroup[];
}

export interface GroupedComparison {
  contexts: ComparisonContext[];
  empty_reason: 'no_authorized_results' | null;
}

type GroupComparisonSubject =
  | { id: string; alias: string; kind: 'batch'; batch_id: string; expected_generation_id: string }
  | { id: string; alias: string; kind: 'legacy_model'; model_cols: string[] };

interface GroupComparisonRequest {
  subjects: GroupComparisonSubject[];
  baseline_subject_id: string;
  scoring_protocol: string;
  scope: { dataset_release_ids: string[]; legacy_metric?: string; legacy_scale_type?: string };
  align_mode?: 'intersection';
  multi_attempt_agg?: 'valid_mean';
  include_verdict?: boolean;
}

type Dimension = 'model' | 'harness' | 'combination';

const dimensions: Array<{ id: Dimension; label: string }> = [
  { id: 'model', label: '固定模型比 Harness' },
  { id: 'harness', label: '固定 Harness 比模型' },
  { id: 'combination', label: '模型 × Harness' },
];

const selectClass = 'w-full min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-2 text-xs text-[var(--text-primary)]';
const maxSubjects = 8;

function modelLabel(group: ComparisonGroup) {
  const model = group.model_key ? (group.model_canonical || group.model_route) : null;
  return [model || '模型未确认', group.checkpoint, group.thinking_mode].filter(Boolean).join(' · ');
}

function harnessLabel(group: ComparisonGroup) {
  return group.harness_key && group.harness
    ? `${group.harness}${group.harness_version ? `@${group.harness_version}` : ''}`
    : 'Harness 未确认';
}

function groupLabel(group: ComparisonGroup, dimension: Dimension) {
  if (dimension === 'model') return harnessLabel(group);
  if (dimension === 'harness') return modelLabel(group);
  return `${modelLabel(group)} · ${harnessLabel(group)}`;
}

function canCompareBatch(batch: GroupedBatch) {
  return batch.task_details_allowed && batch.has_visible_task_scores;
}

function batchToken(batch: GroupedBatch) {
  return `${batch.batch_id}:${batch.generation_id}`;
}

function hasBatchEvidence(group: ComparisonGroup) {
  return group.batches.some(canCompareBatch);
}

function readyGroups(context: ComparisonContext) {
  return context.kind === 'historical_summary'
    ? context.groups.filter((group) => group.historical_scores.some((score) => score.score != null)).length
    : context.groups.filter(hasBatchEvidence).length;
}

function contextLabel(context: ComparisonContext) {
  return context.kind === 'historical_summary'
    ? `${context.scoring_protocol} · ${context.metric_name}（历史汇总）`
    : context.scoring_protocol;
}

function HistoricalScores({
  rows,
  baseline,
  setBaseline,
  dimension,
  baselineName,
  onOpenEvidence,
}: {
  rows: Array<{ group: ComparisonGroup; score: HistoricalScore }>;
  baseline: { group: ComparisonGroup; score: HistoricalScore };
  setBaseline: (id: string) => void;
  dimension: Dimension;
  baselineName: string;
  onOpenEvidence: (group: ComparisonGroup, score: HistoricalScore) => void;
}) {
  return (
    <div className="space-y-2">
      <p className="m-0 text-xs text-[var(--text-secondary)]">历史汇总 · 已登记总分，分差按当前分区的汇总分计算。</p>
      <ul aria-label="历史汇总成绩" className="m-0 list-none space-y-2 p-0">
        {rows.map(({ group, score }) => (
          <li key={score.model_col} className="grid gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 sm:grid-cols-[minmax(0,1fr)_auto]">
            <div className="min-w-0 break-words">
              <div className="text-xs font-semibold text-[var(--text-primary)]">{groupLabel(group, dimension)}</div>
              <div className="text-[11px] text-[var(--text-muted)]">
                {score.model_col} · {score.denominator == null ? '分母未知' : `样本数 ${score.denominator}`}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              <strong className="tabular-nums">{fmtPct(score.score)}</strong>
              <span className="tabular-nums text-[var(--text-secondary)]">
                {score.model_col === baseline.score.model_col
                  ? '基线'
                  : fmtDeltaPP(score.score != null && baseline.score.score != null && score.scale_type === baseline.score.scale_type
                    ? score.score - baseline.score.score : null)}
              </span>
              <label className="inline-flex items-center gap-1.5 text-[var(--text-secondary)]">
                <input
                  type="radio"
                  name={baselineName}
                  checked={score.model_col === baseline.score.model_col}
                  disabled={score.score == null}
                  onChange={() => setBaseline(score.model_col)}
                  aria-label={`将 ${score.model_col} 设为历史基线`}
                />
                设为基线
              </label>
              {score.model_col !== baseline.score.model_col && score.scale_type === baseline.score.scale_type && (
                <button
                  type="button"
                  aria-label={`查看 ${score.model_col} 的历史回答对照`}
                  onClick={() => onOpenEvidence(group, score)}
                  className="font-medium text-[var(--brand-primary)] hover:underline"
                >
                  查看历史回答对照
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function GroupedComparePanel({
  benchmark,
  data,
  datasetReleases,
  onReload,
}: {
  benchmark: string;
  data: GroupedComparison | undefined;
  datasetReleases: DatasetReleaseDTO[];
  onReload: () => Promise<unknown>;
}) {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const headingId = useId();
  const baselineName = useId();
  const [previews, setPreviews] = useState<Record<string, string>>({});
  const contexts = [...(data?.contexts ?? [])].sort((a, b) =>
    Number(readyGroups(b) >= 2) - Number(readyGroups(a) >= 2)
    || Number(b.kind === 'trials') - Number(a.kind === 'trials')
  );
  const releaseHint = params.get('group_release');
  const protocolHint = params.get('group_protocol');
  const batchHint = params.get('group_focus_batch');
  const generationHint = params.get('group_focus_generation');
  const context = contexts.find((item) => item.context_id === params.get('group_context'))
    ?? contexts.find((item) => item.dataset_release_id === releaseHint && item.scoring_protocol === protocolHint)
    ?? contexts[0];
  const configuredHarnesses = new Map<string, Set<string>>();
  for (const group of context?.groups ?? []) {
    if (!group.model_key || !group.harness_key || !hasBatchEvidence(group)) continue;
    const keys = configuredHarnesses.get(group.model_key) ?? new Set<string>();
    keys.add(group.harness_key);
    configuredHarnesses.set(group.model_key, keys);
  }
  const defaultDimension = !batchHint && [...configuredHarnesses.values()].some((keys) => keys.size >= 2) ? 'model' : 'combination';
  const dimension = dimensions.find((item) => item.id === params.get('group_dimension'))?.id ?? defaultDimension;
  const knownTaskCount = datasetReleases.find((release) => release.dataset_release_id === context?.dataset_release_id)?.known_task_count;
  const contextsForRelease = contexts.filter((item) => item.dataset_release_id === context?.dataset_release_id);
  const releases = [...new Map(contexts.map((item) => [item.dataset_release_id, item])).values()];

  const groupKey = (group: ComparisonGroup) => dimension === 'model' ? group.model_key : group.harness_key;
  const anchors = new Map<string, ComparisonGroup>();
  if (dimension !== 'combination') {
    for (const group of context?.groups ?? []) {
      const key = groupKey(group);
      if (key && group.model_key && group.harness_key) anchors.set(key, group);
    }
  }
  const anchorOptions = [...anchors].sort(([a], [b]) => dimension === 'model'
    ? (configuredHarnesses.get(b)?.size ?? 0) - (configuredHarnesses.get(a)?.size ?? 0) : 0);
  const requestedAnchor = params.get('group_anchor');
  const anchor = requestedAnchor && anchors.has(requestedAnchor) ? requestedAnchor : anchorOptions[0]?.[0];
  const visibleGroups = (context?.groups ?? []).filter((group) =>
    dimension === 'combination' || (group.model_key && group.harness_key && groupKey(group) === anchor)
  );
  const focusToken = batchHint && generationHint ? `${batchHint}:${generationHint}` : null;
  const pickTokens = params.getAll('group_batch');
  const activeRows = visibleGroups.flatMap((group) => {
    const batch = group.batches.find((item) => canCompareBatch(item) && pickTokens.includes(batchToken(item)));
    return batch ? [{ group, batch }] : [];
  });
  const base = activeRows.find(({ group }) => group.group_id === params.get('group_base')) ?? activeRows[0];
  const staleSelection = activeRows.length !== pickTokens.length;
  const histories = visibleGroups.flatMap((group) => group.historical_scores.map((score) => ({ group, score })));
  const historyBase = histories.find(({ score }) => score.model_col === params.get('group_base') && score.score != null)
    ?? histories.find(({ score }) => score.score != null) ?? histories[0];

  function update(changes: Record<string, string | undefined>, picks?: string[]) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      for (const [key, value] of Object.entries(changes)) {
        if (value === undefined) next.delete(key);
        else next.set(key, value);
      }
      if (picks) {
        next.delete('group_batch');
        for (const token of picks) next.append('group_batch', token);
      }
      return next;
    }, { replace: true });
  }

  function selectContext(next: ComparisonContext) {
    comparison.reset();
    update({
      group_context: next.context_id, group_anchor: undefined, group_base: undefined,
      group_release: undefined, group_protocol: undefined, group_focus_batch: undefined, group_focus_generation: undefined,
    }, []);
  }

  const comparison = useMutation({
    mutationFn: (payload: GroupComparisonRequest) => apiFetch<{ comparison_id: string }>('/api/v1/compare/query', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  });

  function chooseBatch(group: ComparisonGroup, batch?: GroupedBatch) {
    comparison.reset();
    const ownIds = new Set(group.batches.map((candidate) => candidate.batch_id));
    const picks = pickTokens.filter((token) => !ownIds.has(token.split(':', 1)[0]));
    if (batch) picks.push(batchToken(batch));
    const nextBaseline = !base && batch ? group.group_id
      : base?.group.group_id === group.group_id && !batch
        ? activeRows.find((row) => row.group.group_id !== group.group_id)?.group.group_id
        : base?.group.group_id;
    update({
      group_context: context?.context_id, group_dimension: dimension, group_anchor: anchor, group_base: nextBaseline,
    }, picks);
  }

  function submit(payload: GroupComparisonRequest) {
    if (!context) return;
    comparison.mutate(payload, {
      onSuccess: ({ comparison_id }) => {
        const next = new URLSearchParams({
          cmp: comparison_id,
          focus_benchmark: benchmark,
          focus_dataset: context.dataset_release_id,
          ...(context.kind === 'historical_summary' ? { diff_filter: 'all' } : {}),
        });
        navigate(`/eval/compare?${next}`, { state: { groupedReturnTo: `/benchmarks/${encodeURIComponent(benchmark)}?${params}` } });
      },
    });
  }

  function openComparison() {
    if (!context || !base || activeRows.length < 2 || activeRows.length > maxSubjects || staleSelection) return;
    const ordered = [base, ...activeRows.filter(({ group }) => group.group_id !== base.group.group_id)];
    submit({
      subjects: ordered.map(({ group, batch }) => ({
        id: group.group_id, kind: 'batch', batch_id: batch.batch_id, expected_generation_id: batch.generation_id,
        alias: [groupLabel(group, dimension), group.model_route].filter(Boolean).join(' · '),
      })),
      baseline_subject_id: base.group.group_id,
      scoring_protocol: context.scoring_protocol,
      scope: { dataset_release_ids: [context.dataset_release_id] },
      align_mode: 'intersection',
      multi_attempt_agg: 'valid_mean',
      include_verdict: false,
    });
  }

  function openHistoricalEvidence(group: ComparisonGroup, score: HistoricalScore) {
    if (!context || !historyBase || score.scale_type !== historyBase.score.scale_type) return;
    submit({
      subjects: [
        { id: 's1', kind: 'legacy_model', model_cols: [historyBase.score.model_col], alias: groupLabel(historyBase.group, dimension) },
        { id: 's2', kind: 'legacy_model', model_cols: [score.model_col], alias: groupLabel(group, dimension) },
      ],
      baseline_subject_id: 's1',
      scoring_protocol: context.scoring_protocol,
      scope: {
        dataset_release_ids: [context.dataset_release_id],
        legacy_metric: context.metric_name,
        legacy_scale_type: score.scale_type,
      },
      include_verdict: false,
    });
  }

  function reloadChoices() {
    void onReload().finally(() => comparison.reset());
  }

  const generationChanged = comparison.error instanceof ApiError && comparison.error.code === 'GENERATION_MISMATCH';

  return (
    <section aria-labelledby={headingId} className="min-w-0 space-y-4 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={headingId} className="m-0 flex items-center gap-2 text-sm font-semibold">
          <GitCompareArrows className="size-4 text-[var(--brand-primary)]" aria-hidden="true" /> 分组对比
        </h2>
        {context && context.kind === 'trials' && (
          <span className="text-xs text-[var(--text-muted)]">已选 {activeRows.length} / {maxSubjects} 组</span>
        )}
      </div>

      {!context ? (
        <StateNoticeCard
          kind="empty"
          title={data ? '该基准暂无可用分组' : '当前服务尚未提供分组数据'}
          description={data ? '当前账号可访问的发行版没有已登记的逐题成绩或历史汇总。' : '更新后可按发行版和评分协议查找组合。'}
        />
      ) : (
        <fieldset disabled={comparison.isPending} className="m-0 min-w-0 space-y-4 border-0 p-0">
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            <label className="grid min-w-0 gap-1 text-xs text-[var(--text-secondary)]">
              数据集发行版与 Split
              <select className={selectClass} value={context.dataset_release_id} onChange={(event) => {
                const next = contexts.find((item) => item.dataset_release_id === event.target.value);
                if (next) selectContext(next);
              }}>
                {releases.map((item) => (
                  <option key={item.dataset_release_id} value={item.dataset_release_id}>
                    {item.dataset_version} · {item.split}
                  </option>
                ))}
              </select>
            </label>
            <label className="grid min-w-0 gap-1 text-xs text-[var(--text-secondary)]">
              分组方式
              <select className={selectClass} value={dimension} onChange={(event) => update({
                group_dimension: event.target.value, group_anchor: undefined, group_base: undefined,
              }, [])}>
                {dimensions.map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
              </select>
            </label>
            {dimension !== 'combination' && (
              <label className="grid min-w-0 gap-1 text-xs text-[var(--text-secondary)]">
                {dimension === 'model' ? '固定模型' : '固定 Harness'}
                <select className={selectClass} value={anchor ?? ''} disabled={!anchors.size} onChange={(event) => update({
                  group_anchor: event.target.value, group_base: undefined,
                }, [])}>
                  {!anchors.size && <option value="">暂无已确认身份</option>}
                  {anchorOptions.map(([key, group]) => (
                    <option key={key} value={key}>{dimension === 'model' ? modelLabel(group) : harnessLabel(group)}</option>
                  ))}
                </select>
              </label>
            )}
          </div>

          <details className="rounded-lg border border-[var(--border-subtle)] p-2.5 text-xs text-[var(--text-secondary)]">
            <summary className="cursor-pointer font-medium">比较口径与评分协议 · {context.kind === 'trials' ? '共同有效题' : '历史汇总'}</summary>
            <div className="mt-2">
              <label className="grid min-w-0 gap-1 text-xs text-[var(--text-secondary)]">
                评分协议
                <select className={selectClass} value={context.context_id} onChange={(event) => {
                  const next = contextsForRelease.find((item) => item.context_id === event.target.value);
                  if (next) selectContext(next);
                }}>
                  {contextsForRelease.map((item) => <option key={item.context_id} value={item.context_id}>{contextLabel(item)}</option>)}
                </select>
              </label>
            </div>
          </details>

          {!visibleGroups.length ? (
            <StateNoticeCard kind="empty" title="此分组方式暂无已确认身份" description="可切换到模型 × Harness 查看已登记的实际组合。" />
          ) : context.kind === 'historical_summary' ? (
            historyBase ? <HistoricalScores
              rows={histories} baseline={historyBase}
              setBaseline={(id) => update({ group_context: context.context_id, group_base: id })}
              dimension={dimension} baselineName={baselineName} onOpenEvidence={openHistoricalEvidence}
            /> : <StateNoticeCard kind="empty" title="当前分区暂无已登记的汇总分" />
          ) : (
            <>
              {staleSelection && (
                <div role="status" className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--border-subtle)] p-2.5 text-xs">
                  <span>部分已选批次或发布代已不可用，请重新选择对应批次。</span>
                  <button type="button" onClick={() => update({}, activeRows.map(({ batch }) => batchToken(batch)))} className="text-[var(--brand-primary)] underline">
                    移除失效选择
                  </button>
                </div>
              )}
              {focusToken && !visibleGroups.some((group) => group.batches.some((batch) => batchToken(batch) === focusToken)) && (
                <p role="status" className="m-0 text-xs text-[var(--text-muted)]">当前分组中没有可读取的目标批次发布代。</p>
              )}
              <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-2.5">
                {activeRows.length < 2 && <span className="text-xs text-[var(--text-muted)]">选择至少两组以查看共同题。</span>}
                <button
                  type="button"
                  onClick={openComparison}
                  disabled={activeRows.length < 2 || staleSelection || generationChanged || comparison.isPending}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
                >
                  {comparison.isPending ? '正在计算共同题…' : '查看共同题对比'}
                  <ArrowRight className="size-3.5" aria-hidden="true" />
                </button>
              </div>
              <ul aria-label="可选对比组" className="m-0 grid list-none items-start gap-2.5 p-0 md:grid-cols-2">
                {visibleGroups.map((group) => {
                  const label = groupLabel(group, dimension);
                  const chosen = activeRows.find((row) => row.group.group_id === group.group_id)?.batch;
                  const displayed = chosen
                    ?? group.batches.find((candidate) => batchToken(candidate) === previews[group.group_id])
                    ?? group.batches.find((candidate) => batchToken(candidate) === focusToken)
                    ?? group.batches.find((candidate) => candidate.batch_id === group.default_batch_id)
                    ?? group.batches[0];
                  const focused = displayed && batchToken(displayed) === focusToken;
                  const atLimit = !chosen && activeRows.length >= maxSubjects;
                  return (
                    <li key={group.group_id} className={`min-w-0 space-y-2 rounded-lg border p-3 ${focused
                      ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)]'
                      : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)]'}`}>
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <label className="flex min-w-0 flex-1 items-start gap-2 break-words text-xs font-semibold text-[var(--text-primary)]">
                          <input
                            type="checkbox"
                            className="mt-0.5 shrink-0 accent-[var(--brand-primary)]"
                            aria-label={`选择 ${label}`}
                            checked={Boolean(chosen)}
                            disabled={!displayed || !canCompareBatch(displayed) || atLimit}
                            onChange={() => chooseBatch(group, chosen ? undefined : displayed)}
                          />
                          <span className="min-w-0 break-words">{label}</span>
                        </label>
                        <label className="inline-flex shrink-0 items-center gap-1.5 text-[11px] text-[var(--text-secondary)]">
                          <input
                            type="radio"
                            name={baselineName}
                            aria-label={`将 ${label} 设为基线`}
                            checked={base?.group.group_id === group.group_id}
                            disabled={!chosen}
                            onChange={() => update({ group_base: group.group_id })}
                          />
                          基线
                        </label>
                      </div>
                      <div className="break-words text-[11px] text-[var(--text-muted)]">
                        {group.runner ? `Runner ${group.runner}` : 'Runner 未记录'}
                        {group.model_route && ` · ${group.model_route}`}
                      </div>
                      {displayed ? (
                        <>
                          <div className="break-words text-xs font-medium text-[var(--text-primary)]">
                            {displayed.batch_name}
                          </div>
                          {group.batches.length > 1 && (
                            <details className="text-[11px] text-[var(--text-secondary)]">
                              <summary className="cursor-pointer">更换批次（{group.batches.length}）</summary>
                              <select
                                aria-label={`${label} 候选批次`}
                                className={`mt-1 ${selectClass}`}
                                value={batchToken(displayed)}
                                onChange={(event) => {
                                  const next = group.batches.find((batch) => batchToken(batch) === event.target.value);
                                  if (!next) return;
                                  setPreviews((previous) => ({ ...previous, [group.group_id]: batchToken(next) }));
                                  if (chosen) chooseBatch(group, canCompareBatch(next) ? next : undefined);
                                }}
                              >
                                {group.batches.map((batch) => (
                                  <option key={batchToken(batch)} value={batchToken(batch)}>
                                    {batch.batch_name} · {batch.scored} 有效 · {fmtDateShort(batch.finished_at || batch.indexed_at)}
                                  </option>
                                ))}
                              </select>
                            </details>
                          )}
                          <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 text-[11px]">
                            <span className="tabular-nums text-[var(--text-secondary)]">
                              题目覆盖 {displayed.observed_unique_tasks ?? '未知'} / {knownTaskCount ?? '未知'}
                              {' · '}{displayed.scored}/{displayed.recorded} 有效试验 · 计划 {displayed.planned ?? '未知'} · {runScopeLabel(displayed.run_scope)}
                            </span>
                            <Link className="text-[var(--brand-primary)] hover:underline" to={`/eval/batches/${encodeURIComponent(displayed.batch_id)}`}>
                              查看批次
                            </Link>
                          </div>
                          <div className="break-words text-[11px] text-[var(--text-muted)]">
                            每题计划尝试 {displayed.attempts_per_task ?? '未记录'}
                            {' · '}超时 {displayed.timeout_seconds == null ? '未记录' : `${displayed.timeout_seconds} 秒`}
                            {displayed.batch_status && ` · ${displayed.batch_status}`}
                            {focused && ` · 定位批次发布代 ${displayed.generation_id}`}
                          </div>
                          {displayed.official_result && (
                            <div className="text-[11px] text-[var(--text-muted)]">
                              登记官方分 {displayed.official_result.score} · {displayed.official_result.metric}
                              {displayed.official_result.denominator != null && ` · N=${displayed.official_result.denominator}`}
                            </div>
                          )}
                          {!canCompareBatch(displayed) && (
                            <div className="text-[11px] text-[var(--text-muted)]">
                              {displayed.task_details_allowed ? '暂无可用的逐题评分' : '当前账号可看此批次汇总，逐题明细受权限保护'}
                            </div>
                          )}
                        </>
                      ) : <div className="text-[11px] text-[var(--text-muted)]">当前没有可授权读取的候选批次。</div>}
                    </li>
                  );
                })}
              </ul>
              {activeRows.length > 1 && (['attempts_per_task', 'timeout_seconds'] as const).some((key) =>
                new Set(activeRows.map(({ batch }) => batch[key]).filter((value) => value != null)).size > 1
              ) && <p className="m-0 text-xs text-[var(--text-muted)]">所选批次的逐题尝试次数或超时配置不同，请结合各批次设置解读分差。</p>}
            </>
          )}

          {comparison.isError && (
            <div role="alert" className="flex flex-wrap items-center gap-2 text-xs text-[var(--sem-err-fg)]">
              <span>{generationChanged ? '所选批次已发布新版本，请刷新候选后重选批次。' : comparison.error.message}</span>
              {generationChanged && (
                <button type="button" onClick={reloadChoices} className="font-semibold text-[var(--brand-primary)] underline">刷新候选</button>
              )}
            </div>
          )}
        </fieldset>
      )}
    </section>
  );
}
