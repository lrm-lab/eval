import { useEffect, useId, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  ArrowDownRight, ArrowRight, ArrowUpRight, ChartNoAxesColumn, Check, ChevronDown, Cpu, Database, Search, SlidersHorizontal, Wrench,
} from 'lucide-react';
import { ApiError, apiFetch } from '../../lib/api';
import { fmtDeltaPP, fmtPct, runScopeLabel } from '../../lib/format';
import {
  availableInContext, changeMatrix, choicesFromRows, dimensionChoice, evaluationAxes, harnessGapReason, harnessName, harnessPairs, inChartOrder, modelName,
  previewBody, previewReason, scoreSources,
  type EvaluationAxis, type EvaluationChoice, type EvaluationContext, type PreviewPair, type PreviewScore, type ResultRow,
} from '../../lib/homeEvaluation';
import type { GainGuardrailVerdict } from '../../types/api';
import { benchmarkGroupLink } from './DatasetPerformancePanel';
import { HomeDatasetSwitcher } from './HomeDatasetSwitcher';
import { StateNoticeCard, TableSkeleton } from './Skeletons';

const icons = { dataset: Database, model: Cpu, harness: Wrench };
const linkClass = 'inline-flex items-center gap-1.5 font-semibold text-[var(--brand-primary)] hover:underline';
const selectClass = 'w-full min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-2 text-xs text-[var(--text-primary)]';

interface DirectoryItem {
  benchmark_slug?: string;
  display_name?: string;
  track_slug?: string;
  capability?: string;
  model_canonical?: string;
  model_routes?: string[];
  harness?: string;
  versions?: string[];
  runner?: string;
  batch_count: number;
  legacy_bench_count?: number;
}

interface LegacyScore {
  benchmark_slug: string | null;
  display_name: string;
  dataset_release_id: string | null;
  dataset_version?: string | null;
  split?: string | null;
  metric_name?: string | null;
  model_col?: string;
  score: number | null;
  source: string;
}

interface EvaluationProfile {
  dataset_performance?: ResultRow[];
  grouped_comparison?: { contexts: EvaluationContext[] };
  track_scores?: Record<string, LegacyScore[]>;
}

interface ModelTrend {
  models: string[];
  title: string;
  verdict: GainGuardrailVerdict;
  evidence: (slug: string, release: string | null) => string;
}

function useProfile(resource: string, key: string) {
  return useQuery({
    queryKey: ['home-performance-profile', resource, key],
    queryFn: ({ signal }) => apiFetch<EvaluationProfile>('/api/v1/' + resource + '/' + encodeURIComponent(key), { signal }),
    enabled: Boolean(key),
  });
}

function directoryChoices(items: DirectoryItem[], axis: EvaluationAxis): EvaluationChoice[] {
  const choices = new Map<string, EvaluationChoice>();
  for (const item of items) {
    const key = axis === 'dataset' ? item.benchmark_slug : axis === 'model' ? item.model_canonical : item.harness;
    if (!key) continue;
    const label = axis === 'harness' && key === 'codex' ? 'Codex' : item.display_name || key;
    const search = [key, label, item.capability, item.runner, ...(item.model_routes ?? []), ...(item.versions ?? [])].filter(Boolean).join(' ');
    const existing = choices.get(key);
    choices.set(key, {
      key, label, search: [existing?.search, search].filter(Boolean).join(' '),
      count: (existing?.count ?? 0) + (item.batch_count ?? 0) + (item.legacy_bench_count ?? 0),
      track: item.track_slug,
    });
  }
  return [...choices.values()].sort((a, b) => (b.count ?? 0) - (a.count ?? 0) || a.label.localeCompare(b.label));
}

function subjectLink(axis: EvaluationAxis, key: string, row?: ResultRow) {
  if (axis === 'dataset' && row) return benchmarkGroupLink(row) ?? '/benchmarks/' + encodeURIComponent(key) + '?tab=groups';
  if (axis === 'dataset') return '/benchmarks/' + encodeURIComponent(key) + '?tab=groups';
  const query = new URLSearchParams();
  if (axis === 'model' && row) query.set('tab', 'dataset-performance');
  if (row) {
    query.set('perf_batch', row.batch_id);
    query.set('perf_generation', row.generation_id);
    if (axis === 'model' && row.harness_key) query.set('perf_harness', row.harness_key);
    if (axis === 'harness' && row.model_key) query.set('perf_model', row.model_key);
    if (axis === 'harness' && row.harness_version) query.set('perf_version', row.harness_version);
  }
  return '/' + (axis === 'model' ? 'models' : 'harnesses') + '/' + encodeURIComponent(key)
    + (query.size ? '?' + query.toString() : '') + (row ? '#dataset-performance' : '');
}

function ScoreBar({ score }: { score: number | null }) {
  return (
    <div className="h-1.5 overflow-hidden rounded-full bg-[var(--border-subtle)]" aria-hidden="true">
      {score != null && (
        <div
          className="lrm-bar-grow h-full rounded-full bg-gradient-to-r from-[var(--brand-primary)] to-[var(--sem-running-fg)]"
          style={{ width: Math.max(0, Math.min(100, score)) + '%' }}
        />
      )}
    </div>
  );
}

function BatchChart({ rows, axis }: { rows: ResultRow[]; axis: EvaluationAxis }) {
  return (
    <ul aria-label="实测分数图" className="m-0 list-none divide-y divide-[var(--border-subtle)] p-0">
      {rows.map((row) => {
        const title = axis === 'dataset' ? harnessName(row) : row.display_name;
        const combination = axis === 'harness' ? modelName(row) : axis === 'model' ? harnessName(row) : modelName(row);
        return (
          <li key={row.batch_id + ':' + row.generation_id}>
            <Link
              to={'/eval/batches/' + encodeURIComponent(row.batch_id)}
              className="group/chart block min-w-0 rounded-lg px-1.5 py-2.5 no-underline hover:bg-[var(--bg-elevated)] sm:px-2"
              aria-label={'查看 ' + title + ' 批次：' + row.batch_name}
            >
              <div className="flex min-w-0 items-baseline justify-between gap-2">
                <span className="min-w-0 [overflow-wrap:anywhere] text-xs font-semibold text-[var(--text-primary)] sm:text-sm">
                  {title}<span className="ml-2 text-[11px] font-normal text-[var(--text-secondary)]">{combination}</span>
                </span>
                <strong className="shrink-0 text-sm tabular-nums text-[var(--text-primary)]">{fmtPct(row.score)}</strong>
              </div>
              <div className="my-1.5"><ScoreBar score={row.score} /></div>
              <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5 text-[11px] text-[var(--text-secondary)]">
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  {row.dataset_version ?? '版本未记录'} · {row.split ?? 'Split 未记录'} · {row.metric_name ?? '指标未记录'} · {row.scoring_protocol} · {runScopeLabel(row.run_scope)}
                </span>
                <span className="inline-flex min-w-0 flex-wrap items-center gap-x-1.5 tabular-nums">
                  {scoreSources[row.score_kind]} · 有效试验 {row.scored ?? '—'} / {row.recorded ?? '—'}
                  <span>· 题目覆盖 {row.observed_unique_tasks ?? '未知'} / {row.known_task_count ?? '未知'}</span>
                  {row.score_kind === 'official_summary' && <span>· 官方分母 {row.denominator ?? '未知'}</span>}
                  <ArrowRight aria-hidden="true" className="size-3 shrink-0 text-[var(--brand-primary)] motion-safe:transition-transform motion-safe:group-hover/chart:translate-x-0.5" />
                </span>
              </div>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function HistoricalChart({ rows, subject }: { rows: LegacyScore[]; subject: string }) {
  return (
    <ul aria-label="登记历史汇总图" className="m-0 list-none divide-y divide-[var(--border-subtle)] p-0">
      {rows.map((row, index) => (
        <li key={[row.dataset_release_id, row.model_col, row.metric_name, index].join(':')}>
          <Link to={row.benchmark_slug ? '/benchmarks/' + encodeURIComponent(row.benchmark_slug) + '?tab=groups' : subjectLink('model', subject)}
            className="block rounded-lg px-1.5 py-2.5 no-underline hover:bg-[var(--bg-elevated)]">
            <div className="flex items-baseline justify-between gap-2 text-sm text-[var(--text-primary)]">
              <strong className="min-w-0 [overflow-wrap:anywhere]">{row.display_name}</strong>
              <strong className="shrink-0 tabular-nums">{fmtPct(row.score)}</strong>
            </div>
            <div className="my-1.5"><ScoreBar score={row.score} /></div>
            <span className="text-[11px] text-[var(--text-secondary)]">
              {row.dataset_version ?? row.dataset_release_id ?? '版本未记录'} · {row.split ?? 'Split 未记录'} · {row.metric_name ?? '指标未记录'} · 历史汇总 · 分母未登记
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function TrendHighlights({ trend }: { trend: ModelTrend }) {
  const entries = [...trend.verdict.agentic_gains, ...trend.verdict.guardrails]
    .flatMap((item) => item.delta_pp != null && item.metric_name ? [{ ...item, delta_pp: item.delta_pp }] : []);
  const rising = entries.filter((item) => item.delta_pp > 0).sort((a, b) => b.delta_pp - a.delta_pp).slice(0, 2);
  const falling = entries.filter((item) => item.delta_pp < 0).sort((a, b) => a.delta_pp - b.delta_pp).slice(0, 2);
  if (!rising.length && !falling.length) return null;
  return (
    <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
      <h3 className="m-0 text-xs font-semibold text-[var(--text-primary)]">{trend.title}</h3>
      <ul aria-label="模型同口径变化" className="mt-2 grid list-none gap-1.5 p-0 sm:grid-cols-2">
        {[...rising, ...falling].map((item) => {
          const up = item.delta_pp > 0;
          const Direction = up ? ArrowUpRight : ArrowDownRight;
          return (
            <li key={item.benchmark_slug + ':' + item.dataset_release_id}>
              <Link to={trend.evidence(item.benchmark_slug, item.dataset_release_id)} className="block rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-2 text-xs no-underline hover:border-[var(--brand-primary)]">
                <div className="flex flex-wrap items-center justify-between gap-x-2">
                  <strong className="min-w-0 [overflow-wrap:anywhere] text-[var(--text-primary)]">{item.benchmark_slug}</strong>
                  <span className={'inline-flex items-center gap-0.5 font-semibold tabular-nums ' + (up ? 'text-[var(--sem-pass-fg)]' : 'text-[var(--sem-err-fg)]')}>
                    <Direction aria-hidden="true" className="size-3.5" /> {up ? '上升' : '回落'} {fmtDeltaPP(item.delta_pp)}
                  </span>
                </div>
                <div className="mt-0.5 text-[11px] tabular-nums text-[var(--text-secondary)]">
                  {fmtPct(item.baseline_score)} → {fmtPct(item.candidate_score)} · {item.metric_name} · 历史汇总 · 分母未登记
                </div>
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function PairResult({
  pair, result, failed, harness, busy, onOpen,
}: {
  pair: PreviewPair; result?: PreviewScore; failed: boolean; harness?: boolean; busy: boolean; onOpen: (pair: PreviewPair) => void;
}) {
  const delta = result?.delta_pp;
  if (delta == null) {
    const message = failed ? '比较加载失败' : result ? previewReason(result.reason) : '正在核对共同题…';
    return <span className="block text-[11px] text-[var(--text-secondary)]">
      {message}{result?.common_covered != null && ' · 覆盖 ' + result.common_covered + ' 题'}
    </span>;
  }
  const positive = delta > 0;
  const negative = delta < 0;
  const direction = positive ? (harness ? '领先' : '上升') : negative ? (harness ? '落后' : '回落') : '持平';
  const color = positive ? 'text-[var(--sem-pass-fg)]' : negative ? 'text-[var(--sem-err-fg)]' : 'text-[var(--text-secondary)]';
  return (
    <button type="button" disabled={busy} onClick={() => onOpen(pair)}
      title={pair.baseline.batch_name + ' [' + pair.baseline.generation_id + '] → ' + pair.candidate.batch_name + ' [' + pair.candidate.generation_id + ']'}
      className="group/pair block w-full min-w-0 cursor-pointer rounded-md text-left disabled:opacity-65">
      <span className={'flex items-center gap-1 text-xs font-semibold tabular-nums ' + color}>
        {positive && <ArrowUpRight aria-hidden="true" className="size-3.5 shrink-0" />}
        {negative && <ArrowDownRight aria-hidden="true" className="size-3.5 shrink-0" />}
        {direction} {fmtDeltaPP(delta)}
        <ArrowRight aria-hidden="true" className="ml-auto size-3 shrink-0 text-[var(--brand-primary)] group-hover/pair:translate-x-0.5 motion-safe:transition-transform" />
      </span>
      <span className="mt-0.5 block text-[11px] tabular-nums text-[var(--text-secondary)]">
        {fmtPct(result?.baseline_score)} → {fmtPct(result?.candidate_score)} · 有效 {result?.common_valid} / 覆盖 {result?.common_covered}
      </span>
      <span className="mt-0.5 block [overflow-wrap:anywhere] text-[11px] text-[var(--text-muted)]">
        {pair.note && pair.note + ' · '}{result?.metric_name ?? '指标未记录'} · {pair.baseline.dataset_version} / {pair.baseline.split}
        {' · '}{pair.baseline.scoring_protocol}
      </span>
    </button>
  );
}

export function HomeEvaluationExplorer({ trend }: { trend?: ModelTrend }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const titleId = useId();
  const panelId = useId();
  const filterId = useId();
  const tabs = useRef<Array<HTMLButtonElement | null>>([]);
  const chooser = useRef<HTMLDetailsElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const current = evaluationAxes.find((axis) => axis.id === params.get('view')) ?? evaluationAxes[0];
  const axis = current.id;
  const secondary = evaluationAxes.filter((item) => item.id !== axis);
  const catalog = useQuery({
    queryKey: ['home-performance-directory', current.resource],
    queryFn: ({ signal }) => apiFetch<{ items: DirectoryItem[] }>('/api/v1/' + current.resource, { signal }),
  });
  const choices = directoryChoices(catalog.data?.items ?? [], axis);
  const defaultSubject = choices.find((item) => item.key === current.preferred && item.count)
    ?? choices.find((item) => item.count) ?? choices[0];
  const subject = params.get('subject') || defaultSubject?.key || '';
  const displaySubject = choices.find((item) => item.key === subject)?.label ?? subject;
  const searchResults = choices.filter((item) => item.search?.toLowerCase().includes(query.trim().toLowerCase())).slice(0, 8);
  const profile = useProfile(current.resource, subject);
  const allRows = profile.data?.dataset_performance ?? [];
  const contexts = profile.data?.grouped_comparison?.contexts ?? [];
  const readyGroups = (item: EvaluationContext) => item.groups.filter((group) => group.batches.some((batch) =>
    batch.has_visible_task_scores && batch.task_details_allowed)).length;
  const sortedContexts = [...contexts].sort((a, b) => readyGroups(b) - readyGroups(a)
    || Number(b.kind === 'trials') - Number(a.kind === 'trials'));
  const requestedContext = params.get('context');
  const context = requestedContext ? sortedContexts.find((item) => item.context_id === requestedContext) : sortedContexts[0];
  const contextRows = axis === 'dataset' && context
    ? allRows.filter((row) => context.kind === 'trials' && availableInContext(row, context)) : allRows;
  const filters = secondary.map((item) => ({
    ...item, selected: params.get('lock_' + item.id) ?? '', choices: choicesFromRows(contextRows, item.id),
  }));
  const shown = contextRows.filter((row) => filters.every((item) => !item.selected
    || dimensionChoice(row, item.id)?.key === item.selected));
  const plotRows = inChartOrder(shown, axis).slice(0, 4);
  const chosenDataset = axis === 'dataset' ? subject : filters.find((item) => item.id === 'dataset')?.selected || plotRows[0]?.benchmark_slug || '';
  const relatedBenchmark = useProfile('benchmarks', axis === 'dataset' ? '' : chosenDataset);
  const benchmarkRows = axis === 'dataset' ? contextRows : relatedBenchmark.data?.dataset_performance ?? [];
  const reference = shown.find((row) => row.benchmark_slug === chosenDataset && row.model_key);
  const lockedHarness = filters.find((item) => item.id === 'harness')?.selected;
  const peerRows = benchmarkRows.filter((row) =>
    (axis !== 'model' || row.model_canonical === subject)
    && filters.every((item) => item.id !== 'model' || !item.selected || row.model_key === item.selected)
  );
  const peerPairs = harnessPairs(peerRows, axis === 'harness' ? subject
    : lockedHarness ? reference?.harness ?? null : null, reference);
  const modelForMatrix = axis === 'dataset' ? reference?.model_canonical || '' : '';
  const matrixProfile = useProfile('models', modelForMatrix);
  const matrixRows = (axis === 'dataset' ? matrixProfile.data?.dataset_performance ?? [] : allRows)
    .filter((row) => filters.every((item) => item.id === 'dataset' || !item.selected
      || dimensionChoice(row, item.id)?.key === item.selected));
  const matrix = changeMatrix(matrixRows, chosenDataset);
  const matrixPairs = matrix.items.flatMap((item) => matrix.columns.flatMap(([slug]) => {
    const pair = item.cells.get(slug)?.pair;
    return pair ? [pair] : [];
  }));
  const pairs = [...new Map([...peerPairs, ...matrixPairs].map((pair) => [pair.id, pair])).values()];
  const preview = useQuery({
    queryKey: ['home-common-score-preview', ...pairs.map((pair) => pair.id)],
    queryFn: ({ signal }) => apiFetch<{ items: PreviewScore[] }>('/api/v1/compare/preview', {
      method: 'POST', body: JSON.stringify({ pairs: pairs.map(previewBody) }), signal,
    }),
    enabled: pairs.length > 0,
    staleTime: 60_000,
    retry: false,
  });

  const openComparison = useMutation({
    mutationFn: (pair: PreviewPair) => {
      const subjects = [pair.baseline, pair.candidate].map((row, index) => ({
        id: index === 0 ? 'baseline' : 'candidate', kind: 'batch', batch_id: row.batch_id,
        expected_generation_id: row.generation_id, alias: harnessName(row) + ' · ' + row.batch_name,
      }));
      return apiFetch<{ comparison_id: string }>('/api/v1/compare/query', {
        method: 'POST', body: JSON.stringify({
          subjects,
          baseline_subject_id: 'baseline',
          scoring_protocol: pair.baseline.scoring_protocol,
          scope: { dataset_release_ids: [pair.baseline.dataset_release_id] },
          include_verdict: false,
        }),
      });
    },
    onSuccess: (result, pair) => {
      const query = new URLSearchParams({
        cmp: result.comparison_id, focus_dataset: pair.baseline.dataset_release_id ?? '', diff_filter: 'all',
      });
      navigate('/eval/compare?' + query);
    },
  });

  useEffect(() => {
    setQuery('');
    if (chooser.current) chooser.current.open = false;
  }, [axis, subject]);

  function update(changes: Record<string, string>) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      for (const [key, value] of Object.entries(changes)) {
        if (value) next.set(key, value);
        else next.delete(key);
      }
      return next;
    });
  }

  function resetScope(view: EvaluationAxis, value = '') {
    update({ view, subject: value, context: '', lock_dataset: '', lock_model: '', lock_harness: '' });
    if (chooser.current) chooser.current.open = false;
  }

  const historical = axis === 'model' ? Object.values(profile.data?.track_scores ?? {}).flat()
    .filter((row) => row.source === 'legacy_20261120_csv')
    : context?.kind === 'historical_summary' ? context.groups.flatMap((group) => (group.historical_scores ?? []).map((row) => ({
      ...row, display_name: group.model_canonical ?? row.model_col, benchmark_slug: subject,
      dataset_release_id: context.dataset_release_id, dataset_version: context.dataset_version,
      split: context.split, metric_name: context.metric_name, source: 'legacy_20261120_csv',
    }))) : [];
  const visibleHistory = historical.filter((row) => !lockedHarness &&
    (axis !== 'dataset' || !filters.some((item) => item.id === 'model' && item.selected)) &&
    filters.every((item) => item.id !== 'dataset' || !item.selected || item.selected === row.benchmark_slug));
  const groupLink = new URLSearchParams({ tab: 'groups', group_context: context?.context_id ?? '' });
  const anchor = filters.find((item) => item.selected);
  if (axis === 'dataset' && anchor && context?.kind === 'trials') {
    groupLink.set('group_dimension', anchor.id);
    groupLink.set('group_anchor', anchor.selected);
  }
  const chartLink = axis === 'dataset' && context
    ? '/benchmarks/' + encodeURIComponent(subject) + '?' + groupLink
    : subjectLink(axis, subject, plotRows[0]);
  const scores = new Map((preview.data?.items ?? []).map((item) => [item.id, item]));
  const changeHighlights = matrix.items.flatMap((item) => {
    const pair = item.cells.get(chosenDataset)?.pair;
    const result = pair ? scores.get(pair.id) : undefined;
    if (!pair || !result?.delta_pp || (axis === 'dataset' && context && !availableInContext(pair.baseline, context))) return [];
    return [{ pair, result }];
  }).slice(0, 2);
  const activeFilter = filters.some((item) => item.selected);
  const unavailableFilters = axis === 'dataset'
    ? filters.filter((item) => item.selected && !item.choices.some((choice) => choice.key === item.selected)) : [];
  const showVersionFilter = axis === 'dataset' && sortedContexts.length > 1;
  const hasFilters = filters.some((item) => item.choices.length) || showVersionFilter;
  const hasSubjectError = profile.error instanceof ApiError && profile.error.status === 403;
  const isChoosing = catalog.isPending && !subject;
  const visibleTrend = axis === 'model' && trend?.models.includes(subject) ? trend : undefined;

  return (
    <section aria-labelledby={titleId}
      className="relative isolate min-w-0 space-y-4 rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 shadow-sm sm:p-5">
      <div aria-hidden="true" className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-28 rounded-t-2xl bg-gradient-to-b from-[var(--brand-tint)]/70 to-transparent" />
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h2 id={titleId} className="m-0 inline-flex items-center gap-2 text-base font-bold text-[var(--text-primary)] sm:text-lg">
          <ChartNoAxesColumn aria-hidden="true" className="size-5 shrink-0 text-[var(--brand-primary)]" /> 实测表现
        </h2>
        <div role="tablist" aria-label="实测维度" className="grid w-full grid-cols-3 gap-1 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-1 sm:w-auto">
          {evaluationAxes.map((item, index) => {
            const Icon = icons[item.id];
            return (
              <button key={item.id} ref={(element) => { tabs.current[index] = element; }} id={panelId + '-' + item.id}
                type="button" role="tab" tabIndex={axis === item.id ? 0 : -1} aria-selected={axis === item.id} aria-controls={panelId}
                onClick={() => { if (axis !== item.id) resetScope(item.id); }}
                onKeyDown={(event) => {
                  const key = event.key;
                  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key)) return;
                  event.preventDefault();
                  const count = evaluationAxes.length;
                  const next = key === 'Home' ? 0 : key === 'End' ? count - 1
                    : (index + (key === 'ArrowLeft' ? count - 1 : 1)) % count;
                  if (next !== index) resetScope(evaluationAxes[next].id);
                  tabs.current[next]?.focus();
                }}
                className={'inline-flex min-h-9 min-w-0 cursor-pointer items-center justify-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-semibold motion-safe:transition-colors sm:px-4 '
                  + (axis === item.id ? 'bg-[color-mix(in_srgb,var(--brand-primary),black_22%)] text-white shadow-sm'
                    : 'text-[var(--text-secondary)] hover:bg-[var(--brand-tint)] hover:text-[var(--text-primary)]')}>
                <Icon aria-hidden="true" className="size-3.5 shrink-0" />{item.label}
              </button>
            );
          })}
        </div>
      </header>

      <div role="tabpanel" id={panelId} aria-labelledby={panelId + '-' + axis} className="space-y-4">
        <div className="space-y-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3">
          <div className="relative flex flex-wrap items-center justify-between gap-2">
            {axis === 'dataset' ? <HomeDatasetSwitcher choices={choices} subject={subject} label={displaySubject}
              pending={catalog.isPending} failed={catalog.isError} onRetry={catalog.refetch}
              onChange={(key) => update({ view: 'dataset', subject: key, context: '' })} />
              : <span className="min-w-0 [overflow-wrap:anywhere] text-sm font-bold text-[var(--text-primary)]">{displaySubject || current.label}</span>}
            <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
              {axis !== 'dataset' && <details ref={chooser} className="group/search sm:relative"
                onToggle={(event) => { if (event.currentTarget.open) searchInput.current?.focus(); }}
                onKeyDown={(event) => {
                  if (event.key === 'Escape' && chooser.current?.open) {
                    chooser.current.open = false;
                    chooser.current.querySelector('summary')?.focus();
                  }
                }}>
                <summary className="flex cursor-pointer list-none items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-2.5 py-1.5 text-[var(--text-secondary)] hover:text-[var(--text-primary)] [&::-webkit-details-marker]:hidden">
                  <Search aria-hidden="true" className="size-3.5 shrink-0" /> 选择{current.label}
                </summary>
                <div className="absolute inset-x-0 top-full z-20 mt-1.5 w-full space-y-2 rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)] p-2.5 shadow-xl sm:left-auto sm:right-0 sm:w-72">
                  <form onSubmit={(event) => {
                    event.preventDefault();
                    if (searchResults[0]) resetScope(axis, searchResults[0].key);
                  }}>
                    <input ref={searchInput} type="search" aria-label={'搜索' + current.label} placeholder={'搜索' + current.label}
                      value={query} onChange={(event) => setQuery(event.target.value)}
                      className={selectClass} />
                  </form>
                  {catalog.isError ? (
                    <button type="button" className={linkClass} onClick={() => catalog.refetch()}>目录加载失败，重试</button>
                  ) : catalog.isPending ? <span role="status">正在加载目录…</span> : searchResults.length ? (
                    <ul aria-label={current.label + '候选'} className="m-0 max-h-56 list-none space-y-0.5 overflow-auto p-0">
                      {searchResults.map((item) => (
                        <li key={item.key}>
                          <button type="button" aria-pressed={item.key === subject} onClick={() => resetScope(axis, item.key)}
                            className="flex w-full min-w-0 cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-[var(--text-primary)] hover:bg-[var(--bg-elevated)]">
                            <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{item.label}</span>
                            {item.key === subject && <Check aria-hidden="true" className="size-3.5 shrink-0 text-[var(--brand-primary)]" />}
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : <p className="m-0 text-[var(--text-secondary)]">没有匹配的{current.label}。</p>}
                </div>
              </details>}
              {subject && <Link to={chartLink} className={linkClass}>
                {axis === 'dataset' ? '打开分组对比' : '打开' + current.label + '档案'}
                <ArrowRight aria-hidden="true" className="size-3.5 shrink-0" />
              </Link>}
            </div>
          </div>
          {(hasFilters || activeFilter || requestedContext) && (
            <details className="group/filter border-t border-[var(--border-subtle)] pt-2">
              <summary id={filterId} className="flex cursor-pointer list-none items-center gap-1.5 rounded text-xs text-[var(--text-secondary)] [&::-webkit-details-marker]:hidden">
                <SlidersHorizontal aria-hidden="true" className="size-3.5 shrink-0" /> 锁定维度
                {activeFilter && <span className="text-[var(--brand-primary)]">· 已筛选</span>}
                <ChevronDown aria-hidden="true" className="size-3.5 shrink-0 group-open/filter:rotate-180" />
                {context && <span className="ml-auto min-w-0 [overflow-wrap:anywhere] text-right">{context.dataset_version} · {context.split} · {context.scoring_protocol}</span>}
              </summary>
              <div className="mt-2 grid gap-2 sm:grid-cols-3" aria-labelledby={filterId}>
                {filters.map((item) => (
                  <label key={item.id} className="grid min-w-0 gap-1 text-xs text-[var(--text-secondary)]">
                    固定{item.label}
                    <select className={selectClass} value={item.selected} onChange={(event) => update({ ['lock_' + item.id]: event.target.value })}>
                      <option value="">全部{item.label}</option>
                      {item.selected && !item.choices.some((choice) => choice.key === item.selected) && <option value={item.selected}>当前范围无此项</option>}
                      {item.choices.map((choice) => <option key={choice.key} value={choice.key}>{choice.label}</option>)}
                    </select>
                  </label>
                ))}
                {axis === 'dataset' && sortedContexts.length > 0 && (
                  <label className="grid min-w-0 gap-1 text-xs text-[var(--text-secondary)]">
                    发行版与评分协议
                    <select className={selectClass} value={context?.context_id ?? ''} onChange={(event) => update({ context: event.target.value, lock_model: '', lock_harness: '' })}>
                      {!context && <option value="">自动选择可见分区</option>}
                      {sortedContexts.map((item) => (
                        <option key={item.context_id} value={item.context_id}>
                          {item.dataset_version} · {item.split} · {item.scoring_protocol} · {item.metric_name}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
              </div>
            </details>
          )}
        </div>

        {isChoosing || (subject && profile.isPending) ? <TableSkeleton rows={3} cols={2} /> : profile.isError ? (
          <StateNoticeCard kind={hasSubjectError ? 'forbidden' : 'error'}
            title={hasSubjectError ? '无权查看该对象的表现' : '实测结果加载失败'}
            description={profile.error.message} onRetry={hasSubjectError ? undefined : () => profile.refetch()} />
        ) : requestedContext && axis === 'dataset' && !context ? (
          <StateNoticeCard kind="empty" title="选定的发行版与评分协议已不可用"
            action={<button type="button" className={linkClass} onClick={() => update({ context: '' })}>查看当前分区</button>} />
        ) : (
          <>
            {visibleTrend && <TrendHighlights trend={visibleTrend} />}
            {!visibleTrend && changeHighlights.length > 0 && (
              <ul aria-label="当前数据集的组合变化" className="m-0 grid list-none grid-cols-2 gap-2 p-0">
                {changeHighlights.map(({ pair, result }) => (
                  <li key={pair.id} className="min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-2.5">
                    <div className="mb-1 text-xs font-semibold [overflow-wrap:anywhere] text-[var(--text-primary)]">
                      {harnessName(pair.baseline)}
                      <span className="ml-1 text-[11px] font-normal text-[var(--text-secondary)]">{modelName(pair.baseline)}</span>
                    </div>
                    <PairResult pair={pair} result={result} failed={preview.isError} busy={openComparison.isPending}
                      onOpen={openComparison.mutate} />
                  </li>
                ))}
              </ul>
            )}
            {plotRows.length ? <BatchChart axis={axis} rows={plotRows} />
              : visibleHistory.length ? <HistoricalChart rows={visibleHistory.slice(0, 4)} subject={subject} />
                : <StateNoticeCard kind="empty" title={activeFilter ? '当前筛选暂无实测记录' : '暂无可读取的实测批次'}
                  description={unavailableFilters.length ? '当前数据集没有符合已锁定' + unavailableFilters.map((item) => item.label).join('、') + '的实测记录。' : undefined}
                  action={unavailableFilters.length ? <button type="button" className={linkClass}
                    onClick={() => update(Object.fromEntries(unavailableFilters.map((item) => ['lock_' + item.id, ''])))}>清除无匹配的锁定</button>
                    : subject ? <Link to={chartLink} className={linkClass}>查看档案与接入状态</Link> : undefined} />}
            {shown.length > plotRows.length && <Link to={chartLink} className={linkClass + ' text-xs'}>
              查看全部档案结果 <ArrowRight aria-hidden="true" className="size-3.5" />
            </Link>}

            {(axis === 'harness' || peerPairs.length > 0) && (
              <section aria-label="Harness 共同题对照" className="space-y-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
                <h3 className="m-0 text-sm font-semibold text-[var(--text-primary)]">Harness 共同题对照</h3>
                {peerPairs.length ? (
                  <ul aria-label="Harness 领先与落后" className="m-0 grid list-none gap-2 p-0 sm:grid-cols-2">
                    {peerPairs.map((pair) => {
                      const result = scores.get(pair.id);
                      const better = result?.delta_pp
                        ? (result.delta_pp > 0 ? pair.candidate : pair.baseline) : null;
                      return <li key={pair.id} className="min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-2.5">
                        <div className="mb-1 text-xs font-semibold [overflow-wrap:anywhere] text-[var(--text-primary)]">
                          {harnessName(pair.candidate)} 相对 {harnessName(pair.baseline)}
                        </div>
                        <p className="mb-1 text-[11px] [overflow-wrap:anywhere] text-[var(--text-secondary)]">
                          {pair.baseline.display_name} · 固定 {modelName(pair.baseline)}
                          {better && <span className="ml-1 font-semibold text-[var(--sem-pass-fg)]">领先 {harnessName(better)}</span>}
                        </p>
                        <PairResult pair={pair} result={result} failed={preview.isError} harness
                          busy={openComparison.isPending} onOpen={openComparison.mutate} />
                      </li>;
                    })}
                  </ul>
                ) : <p className="m-0 text-xs text-[var(--text-secondary)]">
                  {relatedBenchmark.isFetching ? '正在核对同数据集的 Harness…'
                    : relatedBenchmark.isError ? '当前数据集的对照加载失败。' : harnessGapReason(peerRows, reference)}
                </p>}
              </section>
            )}

            {matrix.items.length > 0 && (
              <section aria-label="模型与 Harness 变化矩阵" className="space-y-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
                <div className="flex flex-wrap items-baseline justify-between gap-1.5">
                  <h3 className="m-0 text-sm font-semibold text-[var(--text-primary)]">模型 × Harness 变化</h3>
                  <span className="text-[11px] text-[var(--text-secondary)]">逐数据集 · 基线绑定批次发布代</span>
                </div>
                <div className="overflow-x-auto rounded-lg" tabIndex={0} aria-label="按数据集横向查看变化">
                  <table className="w-full border-separate border-spacing-1.5 text-left text-xs">
                    <thead>
                      <tr>
                        <th scope="col" className="min-w-32 py-1 text-[var(--text-secondary)]">组合</th>
                        {matrix.columns.map(([slug, label]) => <th scope="col" key={slug} className="min-w-40 py-1 text-[var(--text-secondary)]">{label}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {matrix.items.map((item) => (
                        <tr key={item.key}>
                          <th scope="row" className="min-w-32 pr-1.5 align-top font-medium [overflow-wrap:anywhere] text-[var(--text-primary)]">
                            {modelName(item.row)}<br />{harnessName(item.row)}
                            {item.row.model_route && <span className="mt-0.5 block text-[10px] font-normal text-[var(--text-muted)]">{item.row.model_route}</span>}
                          </th>
                          {matrix.columns.map(([slug]) => {
                            const cell = item.cells.get(slug);
                            return (
                              <td key={slug} className="min-w-40 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-2 align-top">
                                {cell?.pair ? (
                                  <PairResult pair={cell.pair} result={scores.get(cell.pair.id)} failed={preview.isError}
                                    busy={openComparison.isPending} onOpen={openComparison.mutate} />
                                ) : <span className="text-[11px] text-[var(--text-secondary)]">{cell?.reason || '暂无可见批次'}</span>}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}
            {openComparison.isError && <p role="alert" className="m-0 text-xs text-[var(--sem-err-fg)]">
              {openComparison.error instanceof ApiError && openComparison.error.status === 409
                ? '批次发布代已变化，请刷新首页重选。' : openComparison.error.message}
            </p>}
          </>
        )}
      </div>
    </section>
  );
}
