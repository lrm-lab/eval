import { useId, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowUpRight } from 'lucide-react';
import { apiFetch, ApiError } from '../../lib/api';
import { fmtDeltaPP, fmtPct } from '../../lib/format';
import { useAppStore } from '../../lib/store';

interface PublicBaselineRow {
  id: string;
  model: string | null;
  harness: string;
  configuration: string | null;
  score_pct: number;
  task_count: number;
  sample_count: number;
  published_at: string | null;
}

interface PublicBaselineSource {
  id: string;
  label: string;
  benchmark_slug: string;
  url: string;
  dataset_version: string;
  split: string;
  task_count: number;
  metric: string;
  protocol: string;
  scoring_rule: string;
  runner: string | null;
  status: 'pending' | 'ready' | 'stale' | 'unavailable';
  synced_at: string | null;
  published_at: string | null;
  rows: PublicBaselineRow[];
}

interface CandidateResult {
  batch_id: string;
  local_score_pct?: number | null;
  dataset_version?: string | null;
  split?: string | null;
  task_count?: number;
  sample_count?: number;
  protocol?: string;
  harness?: string | null;
  harness_version?: string | null;
  delta_pp: number | null;
  reasons: string[];
}

interface BaselineComparison {
  baseline: PublicBaselineRow;
  synced_at: string;
  items: CandidateResult[];
}

interface DefaultBaseline {
  baseline_id: string | null;
  mode: 'comparable' | 'reference' | null;
}

export interface PublicBaselineCandidate {
  id: string;
  label: string;
  batchId?: string | null;
  benchmark?: string | null;
}

const statusLabels = {
  pending: '等待首次同步',
  ready: '已同步',
  stale: '同步延迟，显示上次已核验数据',
  unavailable: '同步暂不可用',
};

function rowLabel(row: PublicBaselineRow) {
  return [row.model, row.harness, row.configuration].filter(Boolean).join(' · ');
}

function timestamp(value: string) {
  return new Date(value).toLocaleString();
}

function chunks(batchIds: string[]) {
  const groups: string[][] = [];
  for (let start = 0; start < batchIds.length; start += 50) groups.push(batchIds.slice(start, start + 50));
  return groups;
}

export function PublicBaselinePanel({
  candidates,
  filterBenchmark = false,
  requireSameModel = false,
  benchmark,
  emptyMessage,
  disabledReason,
  thresholdPp,
  compact = false,
}: {
  candidates: PublicBaselineCandidate[];
  filterBenchmark?: boolean;
  requireSameModel?: boolean;
  benchmark?: string;
  emptyMessage: string;
  disabledReason?: string;
  thresholdPp?: number;
  compact?: boolean;
}) {
  const headingId = useId();
  const detailsId = useId();
  const [expanded, setExpanded] = useState(false);
  const showDetails = !compact || expanded;
  const userId = useAppStore((state) => state.user?.user_id);
  const selected = useAppStore((state) => state.publicBaselineId);
  const setSelected = useAppStore((state) => state.setPublicBaselineId);
  const catalog = useQuery({
    queryKey: ['public-baselines', userId],
    queryFn: () => apiFetch<{ sources: PublicBaselineSource[] }>('/api/v1/public-baselines'),
    staleTime: 60_000,
    refetchInterval: 60_000,
  });
  const sources = (catalog.data?.sources || []).filter((item) => !benchmark || item.benchmark_slug === benchmark);
  const defaultBatchIds = [...new Set(candidates.flatMap((item) => item.batchId ? [item.batchId] : []))];
  const automatic = useQuery({
    queryKey: ['public-baseline-default', userId, requireSameModel, sources.map((item) => item.synced_at), defaultBatchIds],
    enabled: selected === 'auto' && !disabledReason && defaultBatchIds.length > 0 && sources.some((item) => item.rows.length),
    queryFn: async ({ signal }) => {
      let reference: DefaultBaseline | null = null;
      for (const ids of chunks(defaultBatchIds)) {
        const params = new URLSearchParams();
        if (requireSameModel) params.set('require_same_model', 'true');
        ids.forEach((id) => params.append('batch_id', id));
        const result = await apiFetch<DefaultBaseline>(`/api/v1/public-baselines/default?${params}`, { signal });
        if (result.mode === 'comparable') return result;
        if (result.mode === 'reference' && !reference) reference = result;
      }
      return reference;
    },
  });
  const baselineId = selected === 'auto' ? automatic.data?.baseline_id : selected;
  const source = sources.find((item) => item.id === baselineId?.split(':')[0]);
  const row = source?.rows.find((item) => item.id === baselineId);
  const visibleCandidates = filterBenchmark
    ? candidates.filter((item) => item.benchmark === source?.benchmark_slug)
    : candidates;
  const batchIds = [...new Set(visibleCandidates.flatMap((item) => item.batchId ? [item.batchId] : []))];
  const comparison = useQuery({
    queryKey: ['public-baseline-comparison', userId, requireSameModel, baselineId, source?.synced_at, batchIds],
    enabled: showDetails && Boolean(row && batchIds.length && !disabledReason),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 1,
    queryFn: async ({ signal }) => {
      const pages = await Promise.all(chunks(batchIds).map((ids) => {
        const params = new URLSearchParams({ baseline_id: baselineId || '' });
        if (requireSameModel) params.set('require_same_model', 'true');
        ids.forEach((id) => params.append('batch_id', id));
        return apiFetch<BaselineComparison>(`/api/v1/public-baselines/compare?${params}`, { signal });
      }));
      // A refresh between chunks can produce a mixed snapshot. Wait for the next catalog poll.
      const sameSnapshot = pages.every((page) => page.synced_at === source?.synced_at);
      return sameSnapshot ? new Map(pages.flatMap((page) => page.items.map((item) => [item.batch_id, item]))) : null;
    },
  });

  const details = showDetails && (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2.5">
        {compact
          ? <span className="text-xs font-semibold text-[var(--text-secondary)]">站内批次与公开成绩</span>
          : <h2 id={headingId} className="m-0 text-sm font-semibold text-[var(--text-primary)]">公开榜单基准</h2>}
        <select
          aria-label="选择公开榜单基准"
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
          className="min-w-0 w-full max-w-sm truncate rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-primary)] sm:w-auto"
        >
          <option value="auto">自动选择公开基准</option>
          <option value="">不使用公开基准</option>
          {selected && selected !== 'auto' && !row && <option value={selected} disabled>所选条目当前不可用</option>}
          {sources.filter((item) => item.rows.length).map((item) => (
            <optgroup key={item.id} label={item.label}>
              {item.rows.map((entry) => (
                <option key={entry.id} value={entry.id}>{rowLabel(entry)} · {fmtPct(entry.score_pct)}</option>
              ))}
            </optgroup>
          ))}
        </select>
      </div>

      {catalog.isError && <p className="mt-2 text-xs text-[var(--text-muted)]" role="status">公开榜单加载失败，稍后重试。</p>}
      {catalog.isPending && <p className="mt-2 text-xs text-[var(--text-muted)]" role="status">正在读取公开榜单…</p>}
      {!row && sources.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--text-muted)]">
          {sources.map((item) => (
            <span key={item.id}>
              <a className="text-[var(--brand-primary)] hover:underline" href={item.url} target="_blank" rel="noreferrer">{item.label}</a>
              {item.status !== 'ready' && ` · ${statusLabels[item.status]}`}
            </span>
          ))}
        </div>
      )}
      {benchmark && catalog.isSuccess && !sources.length && (
        <p className="m-0 mt-2 text-xs text-[var(--text-muted)]">该基准暂无已接入的公开榜单。</p>
      )}
      {selected === 'auto' && !row && sources.some((item) => item.rows.length) && (
        <p className="m-0 mt-2 text-xs text-[var(--text-muted)]" role="status">
          {disabledReason || (automatic.isFetching ? '正在查找可比的公开成绩…'
            : automatic.isError ? '暂时无法自动匹配公开基准。'
            : '当前没有可匹配的站内批次，可手动选择查看公开成绩。')}
        </p>
      )}
      {source && row && (
        <div className="mt-3 space-y-2 border-t border-[var(--border-subtle)] pt-3 text-xs">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <strong className="break-words text-[var(--text-primary)]">{rowLabel(row)}</strong>
            <span className="font-semibold tabular-nums text-[var(--text-primary)]">{fmtPct(row.score_pct)} {source.metric}</span>
            <a href={source.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[var(--brand-primary)] hover:underline">
              {source.label} <ArrowUpRight className="size-3" aria-hidden="true" />
            </a>
          </div>
          <p className="m-0 break-words text-[var(--text-secondary)]">
            {source.dataset_version} · {source.split} · {row.task_count} 题 / {row.sample_count} 次计分 · {source.protocol}
            {source.runner && ` · ${source.runner}`}
          </p>
          <p className="m-0 text-[var(--text-muted)]">{source.scoring_rule}</p>
          <p className="m-0 text-[var(--text-muted)]">
            {statusLabels[source.status]}
            {source.synced_at && <> · <time dateTime={source.synced_at}>{timestamp(source.synced_at)}</time></>}
            {(row.published_at || source.published_at) && ` · 上游发布 ${(row.published_at || source.published_at)?.slice(0, 10)}`}
          </p>

          {selected === 'auto' && (
            <p className="m-0 text-[var(--text-muted)]">
              {automatic.data?.mode === 'reference'
                ? '当前批次未与公开榜单对齐，显示首个有公榜基准的最高公开分作参考。同分按条目 ID 排序。'
                : '自动选择当前首个可比批次对应的最高公开分，同分按条目 ID 排序。'}
            </p>
          )}
          {disabledReason ? <p className="m-0 text-[var(--text-secondary)]">{disabledReason}</p>
            : !visibleCandidates.length ? <p className="m-0 text-[var(--text-secondary)]">{emptyMessage}</p>
            : comparison.isError ? <p className="m-0 text-[var(--text-secondary)]" role="status">暂时无法读取公开基准分差。</p>
            : comparison.isFetching ? <p className="m-0 text-[var(--text-secondary)]" role="status">正在核对站内批次口径…</p>
            : comparison.data === null ? <p className="m-0 text-[var(--text-secondary)]" role="status">公榜正在更新，下一次同步后重试。</p>
            : <ul className="m-0 max-h-60 space-y-1 overflow-y-auto p-0">
              {visibleCandidates.map((candidate) => {
                const item = candidate.batchId ? comparison.data?.get(candidate.batchId) : undefined;
                const delta = item?.delta_pp;
                return (
                  <li key={candidate.id} className="grid gap-1 rounded-lg bg-[var(--bg-elevated)] px-2.5 py-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
                    <div className="min-w-0 text-[var(--text-primary)]">
                      <div className="break-words font-semibold">{candidate.label}</div>
                      {item?.protocol && (
                        <div className="mt-0.5 break-words text-[var(--text-muted)]">
                          {item.local_score_pct != null && `${fmtPct(item.local_score_pct)} · `}
                          {item.dataset_version || '版本未知'} · {item.split || '分片未知'} · {item.task_count} 题 / {item.sample_count} 次计分
                          {item.harness && ` · ${item.harness}${item.harness_version ? `@${item.harness_version}` : ''}`}
                          {item.protocol && ` · ${item.protocol}`}
                        </div>
                      )}
                    </div>
                    {delta == null ? (
                      <span className="min-w-0 break-words text-[var(--text-secondary)]">
                        <strong>不可比 · </strong>{item?.reasons.join('；') || '缺少可核验的站内批次'}
                      </span>
                    ) : (
                      <span className="font-semibold tabular-nums text-[var(--text-primary)]">
                        {fmtDeltaPP(delta)} · {thresholdPp === undefined ? '口径已对齐' : delta < thresholdPp ? '低于退步阈值' : '达到当前阈值'}
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>}
        </div>
      )}
    </>
  );

  return (
    <section aria-labelledby={headingId} className={`min-w-0 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] ${compact ? 'px-3 py-2' : 'p-3.5'}`}>
      {compact && (
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 text-xs sm:grid-cols-[auto_minmax(0,1fr)_auto]">
          <h2 id={headingId} className="m-0 shrink-0 font-semibold text-[var(--text-secondary)]">公开榜单</h2>
          {row && source ? (
            <div className="col-span-2 row-start-2 flex min-w-0 items-center gap-x-2 gap-y-0.5 max-sm:flex-wrap sm:col-span-1 sm:row-start-1">
              <span className="font-semibold tabular-nums text-[var(--text-primary)]">
                {fmtPct(row.score_pct)} {source.metric}
              </span>
              <a href={source.url} target="_blank" rel="noreferrer" className="min-w-0 shrink truncate text-[var(--brand-primary)] hover:underline max-sm:flex-1" title={`${rowLabel(row)} · ${source.label}`}>
                {rowLabel(row)} <ArrowUpRight aria-hidden="true" className="inline size-3" />
              </a>
              <span className="shrink-0 text-[var(--text-muted)] max-sm:basis-full">
                {selected === 'auto' && automatic.data?.mode === 'reference' ? '公开参考 · ' : ''}
                {row.task_count} 题 / {row.sample_count} 次计分
              </span>
              {source.status !== 'ready' && <span className="text-[var(--text-muted)]">{statusLabels[source.status]}</span>}
            </div>
          ) : (
            <span className="col-span-2 row-start-2 min-w-0 text-[var(--text-muted)] sm:col-span-1 sm:row-start-1" role="status">
              {!selected ? '已停用' : catalog.isPending || automatic.isFetching ? '匹配中…' : catalog.isError ? '暂无法读取' : '可展开选择公开成绩'}
            </span>
          )}
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={detailsId}
            onClick={() => setExpanded((open) => !open)}
            className="col-start-2 row-start-1 shrink-0 cursor-pointer rounded-md px-2 py-1.5 font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)] sm:col-start-3"
          >
            {expanded ? '收起公榜详情' : '展开公榜详情'}
          </button>
        </div>
      )}
      <div id={detailsId} hidden={!showDetails} className={compact ? "mt-2 border-t border-[var(--border-subtle)] pt-2" : undefined}>
        {details}
      </div>
    </section>
  );
}
