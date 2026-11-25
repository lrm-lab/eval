import React, { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronLeft, ChevronRight, GitCompareArrows, Save, Search, Sliders, Trash2, X } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDuration, fmtPct, runScopeLabel } from '../lib/format';
import { useAppStore } from '../lib/store';
import { PageTransition } from '../components/motion/primitives';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';
import type { AuthUser, BatchDTO } from '../types/api';

const MIN_BATCHES = 2;
const MAX_BATCHES = 12;
const COMPARE_LIMIT = 6;
const BATCH_PAGE_SIZE = 12;

const panelClass = 'min-w-0 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)]';
const fieldClass = 'h-10 min-w-0 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-elevated)] px-3 text-sm text-[var(--text-primary)]';
const buttonClass = 'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-elevated)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] hover:border-[var(--brand-primary)] disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer motion-safe:transition-colors';
const primaryClass = 'inline-flex min-h-9 items-center justify-center gap-2 rounded-lg bg-[color-mix(in_srgb,var(--brand-primary)_80%,black)] px-3 py-2 text-sm font-semibold text-white hover:bg-[color-mix(in_srgb,var(--brand-hover)_75%,black)] disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer motion-safe:transition-colors';

interface ParamBatchHydrated {
  batch_id: string;
  batch_name: string;
  dataset_release_id: string | null;
  runner: string;
  harness: string | null;
  harness_version: string | null;
  model_route: string | null;
  model_canonical: string | null;
  thinking_mode: string | null;
  run_scope: string;
  scoring_protocol: string;
  official_score: number | null;
  valid_mean: number | null;
  scored: number;
  planned: number | null;
  avg_duration_sec: number | null;
  failed_trials: number;
}

interface ParamGroupRow {
  group_id: string;
  test_topic: string;
  label: string;
  batches: ParamBatchHydrated[];
}

function BatchPicker({ selected, onToggle, saving, scope }: {
  selected: BatchDTO[];
  onToggle: (batch: BatchDTO) => void;
  saving: boolean;
  scope: readonly [string, AuthUser['role'], string[]];
}) {
  const [search, setSearch] = useState('');
  const [filters, setFilters] = useState({ q: '', runner: '', page: 1 });
  const pendingSearch = search.trim() !== filters.q;
  useEffect(() => {
    if (!pendingSearch) return;
    const timer = window.setTimeout(() => {
      setFilters((previous) => ({ ...previous, q: search.trim(), page: 1 }));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [search, pendingSearch]);

  const batches = useQuery({
    queryKey: ['toolkit-param-batches', scope, filters],
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ page: String(filters.page), page_size: String(BATCH_PAGE_SIZE) });
      if (filters.q) params.set('q', filters.q);
      if (filters.runner) params.set('runner', filters.runner);
      return apiFetch<{ items: BatchDTO[]; total: number }>(`/api/v1/batches?${params}`, { signal });
    },
  });
  const total = batches.data?.total ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / BATCH_PAGE_SIZE));
  const atLimit = selected.length === MAX_BATCHES;

  return (
    <section aria-labelledby="param-batches-title" className={panelClass}>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-subtle)] px-4 py-4">
        <div>
          <h2 id="param-batches-title" className="m-0 text-base font-semibold">选择评测批次</h2>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">从已索引的批次中选择 {MIN_BATCHES}–{MAX_BATCHES} 个。</p>
        </div>
        <a href="#param-draft" className="text-xs font-medium text-[var(--brand-primary)] no-underline hover:underline lg:hidden">
          查看已选（{selected.length}）
        </a>
      </div>
      <div className="flex flex-wrap gap-2 p-4" role="search" aria-label="筛选评测批次">
        <div className="relative min-w-0 basis-full flex-1 sm:basis-48">
          <Search aria-hidden="true" className="pointer-events-none absolute top-3 left-3 size-4 text-[var(--text-secondary)]" />
          <input
            type="search"
            aria-label="搜索批次、基准、模型或 Harness"
            placeholder="搜索批次名、基准、模型或 Harness"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className={`${fieldClass} w-full pl-9`}
          />
        </div>
        <select
          aria-label="按执行器筛选"
          value={filters.runner}
          onChange={(e) => setFilters({ runner: e.target.value, q: search.trim(), page: 1 })}
          className={`${fieldClass} w-full sm:w-36`}
        >
          <option value="">全部执行器</option>
          <option value="harbor">Harbor</option>
          <option value="direct">Direct</option>
          <option value="vlmevalkit">VLMEvalKit</option>
        </select>
      </div>
      {atLimit && (
        <p role="status" className="m-0 px-4 pb-3 text-xs text-[var(--sem-zero-fg)]">
          已选满 {MAX_BATCHES} 个批次。取消勾选或从已选中移除批次后，可以继续选择。
        </p>
      )}
      {pendingSearch || batches.isPending ? (
        <div className="min-h-60 px-4 pb-3"><TableSkeleton rows={4} cols={4} /></div>
      ) : batches.isError ? (
        <div className="px-4 pb-3">
          <StateNoticeCard kind="error" title="批次加载失败" description={batches.error.message} onRetry={() => batches.refetch()} />
        </div>
      ) : !batches.data.items.length ? (
        <div className="px-4 pb-3">
          <StateNoticeCard kind="empty" title="未找到匹配的批次" description="试试更短的关键词，或切换执行器。" />
        </div>
      ) : (
        <div tabIndex={0} role="region" aria-label="可选批次表" className="max-h-[29rem] overflow-auto border-y border-[var(--border-subtle)]">
          <table className="w-full min-w-[36rem] table-fixed text-left text-xs">
            <thead className="sticky top-0 z-10 bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
              <tr>
                <th scope="col" className="w-10 px-3 py-2.5"><span className="sr-only">选择</span></th>
                <th scope="col" className="w-[33%] px-3 py-2.5">批次名称</th>
                <th scope="col" className="w-[16%] px-3 py-2.5">基准</th>
                <th scope="col" className="w-[22%] px-3 py-2.5">Harness / 模型</th>
                <th scope="col" className="px-3 py-2.5">协议 / 范围</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {batches.data.items.map((batch) => {
                const checked = selected.some((item) => item.batch_id === batch.batch_id);
                const checkboxId = `param-batch-${batch.batch_id}`;
                return (
                  <tr key={batch.batch_id} className={checked ? 'bg-[var(--brand-tint)]' : 'hover:bg-[var(--bg-elevated)]'}>
                    <td className="px-3 py-3 align-top">
                      <input
                        id={checkboxId}
                        type="checkbox"
                        checked={checked}
                        disabled={saving || (atLimit && !checked)}
                        onChange={() => onToggle(batch)}
                        className="mt-0.5 size-4 cursor-pointer align-middle accent-[var(--brand-primary)] disabled:cursor-not-allowed"
                      />
                    </td>
                    <td className="px-3 py-3 align-top [overflow-wrap:anywhere]">
                      <label htmlFor={checkboxId} className="cursor-pointer font-medium text-[var(--text-primary)]">{batch.batch_name}</label>
                      {batch.is_running_placeholder && <div className="mt-1 text-[var(--sem-running-fg)]">运行中</div>}
                    </td>
                    <td className="px-3 py-3 align-top [overflow-wrap:anywhere]">{batch.raw_dataset_name || batch.benchmark_slug || batch.dataset_release_id || '—'}</td>
                    <td className="px-3 py-3 align-top [overflow-wrap:anywhere]">
                      {batch.harness || batch.runner}
                      <div className="mt-1 text-[var(--text-secondary)]">{batch.model_canonical || batch.model_route || '—'}</div>
                    </td>
                    <td className="px-3 py-3 align-top [overflow-wrap:anywhere]">
                      {batch.scoring_protocol}
                      <div className="mt-1 text-[var(--text-secondary)]">{runScopeLabel(batch.run_scope)}</div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2 p-4 text-xs text-[var(--text-secondary)]">
        <span aria-live="polite">
          {pendingSearch || batches.isPending ? '正在查找批次…' : batches.data && `共 ${total} 个批次 · 第 ${Math.min(filters.page, lastPage)} / ${lastPage} 页`}
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            className={buttonClass}
            disabled={filters.page === 1 || pendingSearch || batches.isPending}
            onClick={() => setFilters((previous) => ({ ...previous, page: Math.min(previous.page - 1, lastPage) }))}
          ><ChevronLeft aria-hidden="true" className="size-4" /> 上一页</button>
          <button
            type="button"
            className={buttonClass}
            disabled={filters.page >= lastPage || pendingSearch || batches.isPending}
            onClick={() => setFilters((previous) => ({ ...previous, page: previous.page + 1 }))}
          >下一页 <ChevronRight aria-hidden="true" className="size-4" /></button>
        </div>
      </div>
    </section>
  );
}

function ParamGroupCard({ group, headingRef, canWrite, confirmingDelete, deleting, onAskDelete, onCancelDelete, onDelete, onCompare }: {
  group: ParamGroupRow;
  headingRef?: React.Ref<HTMLHeadingElement>;
  canWrite: boolean;
  confirmingDelete: boolean;
  deleting: boolean;
  onAskDelete: () => void;
  onCancelDelete: () => void;
  onDelete: () => void;
  onCompare: () => void;
}) {
  const count = group.batches.length;
  const overflow = count > COMPARE_LIMIT;
  return (
    <article className={panelClass}>
      <header className="flex flex-wrap items-start justify-between gap-3 p-4">
        <div className="min-w-0 [overflow-wrap:anywhere]">
          <h3 ref={headingRef} tabIndex={-1} className="m-0 scroll-mt-5 text-base font-semibold">{group.test_topic}</h3>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">{group.label} · {count} 个可读批次</p>
        </div>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={count < MIN_BATCHES}
              onClick={onCompare}
              aria-describedby={overflow || count < MIN_BATCHES ? `${group.group_id}-compare-hint` : undefined}
              className={buttonClass}
            >
              <GitCompareArrows aria-hidden="true" className="size-4" /> {overflow ? `送前 ${COMPARE_LIMIT} 个批次对比` : '送入对比'}
            </button>
            {canWrite && !confirmingDelete && (
              <button type="button" disabled={deleting} onClick={onAskDelete} className={`${buttonClass} hover:text-[var(--sem-err-fg)]`}>
                <Trash2 aria-hidden="true" className="size-4" /> 删除台账
              </button>
            )}
          </div>
          {(overflow || count < MIN_BATCHES) && (
            <p id={`${group.group_id}-compare-hint`} className="m-0 mt-1.5 text-xs text-[var(--text-secondary)]">
              {overflow ? `对比工作台最多接收 ${COMPARE_LIMIT} 个批次，按下表顺序取前 ${COMPARE_LIMIT} 个。` : `送入对比需要至少 ${MIN_BATCHES} 个可读批次。`}
            </p>
          )}
        </div>
      </header>
      {confirmingDelete && (
        <div className="mx-4 mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] px-3 py-2 text-xs">
          <span className="mr-auto text-[var(--sem-err-fg)]">删除这份对照台账？</span>
          <button type="button" disabled={deleting} className={buttonClass} onClick={onCancelDelete}>取消</button>
          <button type="button" disabled={deleting} className={`${buttonClass} text-[var(--sem-err-fg)]`} onClick={onDelete}>
            {deleting ? '正在删除…' : '确认删除'}
          </button>
        </div>
      )}
      {count > 0 && (
        <div tabIndex={0} role="region" aria-label={`${group.label}的对照表`} className="overflow-x-auto rounded-b-xl border-t border-[var(--border-subtle)]">
          <table className="w-full min-w-[43rem] table-fixed text-left text-xs">
            <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
              <tr>
                <th scope="col" className="w-[29%] px-4 py-2.5">批次</th>
                <th scope="col" className="w-[23%] px-3 py-2.5">模型 / 运行参数</th>
                <th scope="col" className="w-[21%] px-3 py-2.5">协议 / 范围</th>
                <th scope="col" className="w-[9%] px-3 py-2.5 text-right">主分</th>
                <th scope="col" className="px-4 py-2.5 text-right">覆盖 / 均耗时 / 故障</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)] tabular-nums">
              {group.batches.map((batch) => {
                const score = batch.official_score ?? batch.valid_mean;
                return (
                  <tr key={batch.batch_id} className="align-top">
                    <td className="px-4 py-3 [overflow-wrap:anywhere]">
                      <Link to={`/eval/batches/${encodeURIComponent(batch.batch_id)}`} className="font-medium text-[var(--brand-primary)] no-underline hover:underline">{batch.batch_name}</Link>
                      {batch.dataset_release_id && <div className="mt-1 text-[var(--text-secondary)]">{batch.dataset_release_id}</div>}
                    </td>
                    <td className="px-3 py-3 [overflow-wrap:anywhere]">
                      {batch.model_canonical || batch.model_route || '—'}
                      <div className="mt-1 text-[var(--text-secondary)]">
                        {[batch.runner, batch.harness && `${batch.harness}${batch.harness_version ? `@${batch.harness_version}` : ''}`, batch.thinking_mode || 'default'].filter(Boolean).join(' · ')}
                      </div>
                    </td>
                    <td className="px-3 py-3 [overflow-wrap:anywhere]">
                      {batch.scoring_protocol}
                      <div className="mt-1 text-[var(--text-secondary)]">{runScopeLabel(batch.run_scope)}</div>
                    </td>
                    <td className="px-3 py-3 text-right font-semibold">{score !== null ? fmtPct(score <= 1 ? score * 100 : score) : '—'}</td>
                    <td className="px-4 py-3 text-right">{batch.scored}/{batch.planned ?? '—'} · {fmtDuration(batch.avg_duration_sec)} · {batch.failed_trials}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </article>
  );
}

export function ToolkitParamsPage() {
  const user = useAppStore((s) => s.user);
  const pushToast = useAppStore((s) => s.pushToast);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const canWrite = user?.role === 'researcher' || user?.role === 'admin';
  const scope = user ? [user.user_id, user.role, user.projects] as const : null;
  const groupsKey = ['toolkit-param-groups', scope];
  const projectId = user && (user.role === 'admin' || user.projects.includes('default') ? 'default' : `personal:${user.user_id}`);

  const [topic, setTopic] = useState('');
  const [label, setLabel] = useState('');
  const [selected, setSelected] = useState<BatchDTO[]>([]);
  const [recentId, setRecentId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const resultHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!recentId) return;
    resultHeading.current?.focus({ preventScroll: true });
    resultHeading.current?.scrollIntoView({ block: 'start' });
  }, [recentId]);

  const groups = useQuery({
    queryKey: groupsKey,
    enabled: Boolean(scope),
    queryFn: ({ signal }) => apiFetch<{ items: ParamGroupRow[] }>('/api/v1/toolkit/params', { signal }),
  });
  const create = useMutation({
    mutationFn: (payload: { test_topic: string; label: string; batch_ids: string[]; project_id: string }) =>
      apiFetch<ParamGroupRow>('/api/v1/toolkit/params', { method: 'POST', body: JSON.stringify(payload) }),
    onSuccess: async (group) => {
      await queryClient.cancelQueries({ queryKey: groupsKey });
      queryClient.setQueryData<{ items: ParamGroupRow[] }>(groupsKey, (previous) => ({
        ...previous,
        items: [group, ...(previous?.items ?? []).filter((item) => item.group_id !== group.group_id)].slice(0, 100),
      }));
      setTopic('');
      setLabel('');
      setSelected([]);
      setRecentId(group.group_id);
      pushToast('success', '已保存参数对照台账');
      void queryClient.invalidateQueries({ queryKey: groupsKey });
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/toolkit/params/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    onSuccess: async (_, id) => {
      await queryClient.cancelQueries({ queryKey: groupsKey });
      queryClient.setQueryData<{ items: ParamGroupRow[] }>(groupsKey, (previous) => previous && ({
        ...previous,
        items: previous.items.filter((item) => item.group_id !== id),
      }));
      setDeleteId(null);
      pushToast('success', '已删除参数对照台账');
      void queryClient.invalidateQueries({ queryKey: groupsKey });
    },
    onError: (error: Error) => pushToast('error', error.message),
  });

  const toggle = (batch: BatchDTO) => setSelected((previous) => {
    if (previous.some((item) => item.batch_id === batch.batch_id)) {
      return previous.filter((item) => item.batch_id !== batch.batch_id);
    }
    return previous.length < MAX_BATCHES ? [...previous, batch] : previous;
  });

  const sendToCompare = (group: ParamGroupRow) => {
    const compareBasket = group.batches.slice(0, COMPARE_LIMIT).map((batch) => ({
      id: batch.batch_id,
      kind: 'batch' as const,
      batch_id: batch.batch_id,
      alias: batch.batch_name,
    }));
    if (compareBasket.length < MIN_BATCHES) return;
    useAppStore.setState({ compareBasket, baselineId: compareBasket[0].id });
    navigate('/eval/compare?fromDock=1');
  };

  const missing = [
    selected.length < MIN_BATCHES && `再选 ${MIN_BATCHES - selected.length} 个批次`,
    !topic.trim() && '填写实验主题',
    !label.trim() && '填写对比组名',
  ].filter(Boolean);

  return (
    <PageTransition className="mx-auto max-w-7xl space-y-6 px-5 py-6">
      <header>
        <h1 className="m-0 flex items-center gap-2.5 text-xl font-bold text-[var(--text-primary)]">
          <Sliders aria-hidden="true" className="size-5 shrink-0 text-[var(--brand-primary)]" /> 运行参数跨批次对照台账
        </h1>
        <p className="m-0 mt-2 text-sm text-[var(--text-secondary)]">
          {canWrite ? '筛选批次并保存对照表，查看各批次的运行参数、得分和覆盖。' : '查看已有台账的运行参数、得分和覆盖。'}
        </p>
      </header>

      {canWrite && scope && projectId && (
        <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,0.43fr)]">
          <BatchPicker selected={selected} onToggle={toggle} saving={create.isPending} scope={scope} />
          <form
            id="param-draft"
            aria-labelledby="param-draft-title"
            className={`${panelClass} scroll-mt-5`}
            onSubmit={(event) => {
              event.preventDefault();
              if (missing.length || create.isPending) return;
              create.mutate({
                test_topic: topic.trim(),
                label: label.trim(),
                batch_ids: selected.map((batch) => batch.batch_id),
                project_id: projectId,
              });
            }}
          >
            <fieldset disabled={create.isPending} className="m-0 min-w-0 border-0 p-0">
              <div className="flex items-center justify-between gap-2 px-4 py-4">
                <h2 id="param-draft-title" className="m-0 text-base font-semibold">已选批次</h2>
                <span className="rounded-md bg-[var(--brand-tint)] px-2 py-1 text-xs font-semibold tabular-nums text-[var(--text-primary)]">
                  {selected.length} / {MAX_BATCHES}
                </span>
              </div>
              {selected.length === 0 ? (
                <p className="mx-4 mt-0 mb-4 rounded-lg border border-dashed border-[var(--border-strong)] px-3 py-4 text-xs text-[var(--text-secondary)]">从批次表勾选至少 {MIN_BATCHES} 个批次。</p>
              ) : (
                <ol aria-label="已选批次" className="mx-4 mt-0 mb-4 max-h-56 list-decimal space-y-1.5 overflow-auto pl-6 text-xs marker:tabular-nums marker:text-[var(--text-secondary)]">
                  {selected.map((batch) => (
                    <li key={batch.batch_id} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">
                      <div className="flex items-start justify-between gap-2 py-1.5 pr-1 pl-2.5">
                        <span className="min-w-0 py-1 leading-relaxed [overflow-wrap:anywhere]">{batch.batch_name}</span>
                        <button type="button" onClick={() => toggle(batch)} aria-label={`移除 ${batch.batch_name}`} className="grid size-7 shrink-0 cursor-pointer place-items-center rounded-md text-[var(--text-secondary)] hover:bg-[var(--sem-err-bg)] hover:text-[var(--sem-err-fg)]">
                          <X aria-hidden="true" className="size-4" />
                        </button>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
              <div className="space-y-3 border-t border-[var(--border-subtle)] p-4">
                <label className="block text-xs font-medium">
                  实验主题
                  <input required value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="例如 Thinking 开关消融" className={`${fieldClass} mt-1.5 w-full`} />
                </label>
                <label className="block text-xs font-medium">
                  对比组名
                  <input required value={label} onChange={(e) => setLabel(e.target.value)} placeholder="例如 DeepSWE · OpenHands" className={`${fieldClass} mt-1.5 w-full`} />
                </label>
                {projectId.startsWith('personal:') && <p className="m-0 text-xs text-[var(--text-secondary)]">台账将保存到个人项目。</p>}
                <button type="submit" disabled={Boolean(missing.length) || create.isPending} aria-describedby={missing.length ? 'param-save-hint' : undefined} className={`${primaryClass} w-full`}>
                  <Save aria-hidden="true" className="size-4" /> {create.isPending ? '正在保存…' : '保存对照台账'}
                </button>
                {missing.length > 0 && (
                  <p id="param-save-hint" aria-live="polite" className="m-0 text-xs leading-relaxed text-[var(--text-secondary)]">保存前请{missing.join('、')}。</p>
                )}
                {create.isError && <p role="alert" className="m-0 text-xs text-[var(--sem-err-fg)]">{create.error.message}</p>}
              </div>
            </fieldset>
          </form>
        </div>
      )}

      <section aria-labelledby="param-results" className="space-y-3">
        <h2 id="param-results" className="m-0 text-base font-semibold">已保存的对照台账</h2>
        {groups.isPending ? (
          <TableSkeleton rows={4} cols={5} />
        ) : groups.isError && !groups.data ? (
          <StateNoticeCard kind="error" title="台账加载失败" description={groups.error.message} onRetry={() => groups.refetch()} />
        ) : !groups.data?.items.length ? (
          <StateNoticeCard kind="empty" title="尚无参数对照台账" />
        ) : (
          groups.data.items.map((group) => (
            <ParamGroupCard
              key={group.group_id}
              group={group}
              headingRef={recentId === group.group_id ? resultHeading : undefined}
              canWrite={canWrite}
              confirmingDelete={deleteId === group.group_id}
              deleting={remove.isPending}
              onAskDelete={() => setDeleteId(group.group_id)}
              onCancelDelete={() => setDeleteId(null)}
              onDelete={() => remove.mutate(group.group_id)}
              onCompare={() => sendToCompare(group)}
            />
          ))
        )}
      </section>
    </PageTransition>
  );
}
