import { useId } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { RowActionIcon } from './RowActionIcon';
import { StateNoticeCard } from './Skeletons';
import { fmtPct, runScopeLabel } from '../../lib/format';

export interface DatasetPerformanceRow {
  benchmark_slug: string | null;
  display_name: string;
  dataset_release_id: string | null;
  dataset_version: string | null;
  split: string | null;
  metric_name: string | null;
  scoring_protocol: string;
  run_scope: string;
  model_canonical: string | null;
  model_route: string | null;
  model_key: string | null;
  checkpoint: string | null;
  thinking_mode: string | null;
  harness: string | null;
  harness_version: string | null;
  harness_key: string | null;
  runner: string;
  score: number | null;
  score_kind: 'trial_valid_mean' | 'official_summary' | 'unscored';
  denominator: number | null;
  scored: number | null;
  recorded: number | null;
  planned: number | null;
  unique_tasks?: number | null;
  known_task_count?: number | null;
  batch_status: string | null;
  identity_status: string;
  attempts_per_task: number | null;
  timeout_seconds: number | null;
  batch_id: string;
  generation_id: string;
  batch_name: string;
}

const selectClass = 'w-full min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-2 text-xs text-[var(--text-primary)]';
const linkClass = 'text-[var(--brand-primary)] hover:underline';

const scoreLabels = {
  trial_valid_mean: '有效均分',
  official_summary: '官方汇总',
  unscored: '未计分',
};

function harnessLabel(row: DatasetPerformanceRow) {
  return row.harness_key && row.harness ? [row.harness, row.harness_version].filter(Boolean).join(' @ ') : 'Harness 未确认';
}

function benchmarkLink(row: DatasetPerformanceRow) {
  if (!row.benchmark_slug || !row.dataset_release_id) return null;
  const query = new URLSearchParams({
    tab: 'groups',
    group_release: row.dataset_release_id,
    group_protocol: row.scoring_protocol,
    group_focus_batch: row.batch_id,
    group_focus_generation: row.generation_id,
  });
  return `/benchmarks/${encodeURIComponent(row.benchmark_slug)}?${query}`;
}

function peerLink(row: DatasetPerformanceRow, axis: 'model' | 'harness') {
  const identity = axis === 'harness' && row.model_key ? row.model_canonical
    : axis === 'model' && row.harness_key ? row.harness : null;
  if (!identity) return null;
  const query = new URLSearchParams({ perf_batch: row.batch_id, perf_generation: row.generation_id });
  if (axis === 'harness') {
    query.set('tab', 'dataset-performance');
    if (row.harness_key) query.set('perf_harness', row.harness_key);
    return `/models/${encodeURIComponent(identity)}?${query}#dataset-performance`;
  }
  if (row.model_key) query.set('perf_model', row.model_key);
  if (row.harness_version) query.set('perf_version', row.harness_version);
  return `/harnesses/${encodeURIComponent(identity)}?${query}#dataset-performance`;
}

export function DatasetPerformancePanel({
  axis,
  rows,
}: {
  axis: 'model' | 'harness';
  rows?: DatasetPerformanceRow[];
}) {
  const [params, setParams] = useSearchParams();
  const headingId = useId();
  const available = rows ?? [];
  const models = new Map<string, string>();
  const harnesses = new Map<string, string>();
  const versions = new Set<string>();
  for (const row of available) {
    if (row.model_key && row.model_canonical) {
      const label = [row.model_canonical, row.checkpoint, row.thinking_mode].filter(Boolean).join(' · ');
      models.set(row.model_key, label);
    }
    if (row.harness_key && row.harness) harnesses.set(row.harness_key, harnessLabel(row));
    if (row.harness_key && row.harness_version) versions.add(row.harness_version);
  }
  const requestedModel = params.get('perf_model') ?? '';
  const requestedHarness = params.get('perf_harness') ?? '';
  const requestedVersion = params.get('perf_version') ?? '';
  const model = models.has(requestedModel) ? requestedModel : '';
  const harness = harnesses.has(requestedHarness) ? requestedHarness : '';
  const version = versions.has(requestedVersion) ? requestedVersion : '';
  const filtered = available.filter((row) => axis === 'harness'
    ? (!model || row.model_key === model) && (!version || row.harness_version === version)
    : !harness || row.harness_key === harness);
  const focusedBatch = params.get('perf_batch');
  const focusedGeneration = params.get('perf_generation');
  const isFocused = (row: DatasetPerformanceRow) => row.batch_id === focusedBatch && row.generation_id === focusedGeneration;
  const ordered = [...filtered.filter(isFocused), ...filtered.filter((row) => !isFocused(row))];

  function setFilter(key: string, value: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value) next.set(key, value);
      else next.delete(key);
      next.delete('perf_batch');
      next.delete('perf_generation');
      return next;
    }, { replace: true });
  }

  return (
    <section id="dataset-performance" aria-labelledby={headingId} className="min-w-0 space-y-3 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={headingId} className="m-0 text-sm font-semibold text-[var(--text-primary)]">跨数据集表现</h2>
        {rows && <span className="text-xs text-[var(--text-muted)]">{filtered.length} 个批次</span>}
      </div>

      {available.length > 0 && (
        <div className="grid gap-2.5 sm:grid-cols-2">
          {axis === 'harness' ? (
            <>
              <label className="grid min-w-0 gap-1 text-xs text-[var(--text-secondary)]">
                固定模型
                <select value={model} onChange={(event) => setFilter('perf_model', event.target.value)} className={selectClass}>
                  <option value="">全部模型</option>
                  {[...models].sort(([, a], [, b]) => a.localeCompare(b)).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
                </select>
              </label>
              <label className="grid min-w-0 gap-1 text-xs text-[var(--text-secondary)]">
                Harness 版本
                <select value={version} onChange={(event) => setFilter('perf_version', event.target.value)} className={selectClass}>
                  <option value="">全部版本</option>
                  {[...versions].sort().map((item) => <option key={item} value={item}>{item}</option>)}
                </select>
              </label>
            </>
          ) : (
            <label className="grid min-w-0 gap-1 text-xs text-[var(--text-secondary)]">
              固定 Harness 与版本
              <select value={harness} onChange={(event) => setFilter('perf_harness', event.target.value)} className={selectClass}>
                <option value="">全部 Harness</option>
                {[...harnesses].map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
            </label>
          )}
        </div>
      )}

      {focusedBatch && focusedGeneration && !filtered.some(isFocused) && (
        <p role="status" className="m-0 text-xs text-[var(--sem-zero-fg)]">当前档案中没有可读取的目标批次发布代。</p>
      )}
      {!ordered.length ? (
        <StateNoticeCard
          kind="empty"
          title={rows ? '当前筛选暂无可读取的批次表现' : '当前服务尚未提供逐批次表现'}
          description={rows && available.length ? '调整上方筛选以查看其他实际批次。' : undefined}
        />
      ) : (
        <ul aria-label="各数据集批次表现" className="m-0 list-none space-y-2 p-0">
          {ordered.map((row) => {
            const benchmark = benchmarkLink(row);
            const peer = peerLink(row, axis);
            const focused = isFocused(row);
            const modelLabel = [row.model_key ? row.model_canonical : null, row.checkpoint, row.thinking_mode].filter(Boolean).join(' · ');
            return (
              <li
                key={`${row.batch_id}:${row.generation_id}`}
                className={`grid min-w-0 gap-2.5 rounded-lg border p-3 text-xs md:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)_minmax(0,1.2fr)_minmax(0,1fr)] ${
                  focused ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)]' : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)]'
                }`}
              >
                <div className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
                  {benchmark ? (
                    <Link to={benchmark} className={`font-semibold ${linkClass}`} aria-label={`查看 ${row.display_name} 的分组对比`}>
                      {row.display_name}
                    </Link>
                  ) : <strong>{row.display_name}</strong>}
                  <div className="text-[var(--text-secondary)]">{row.dataset_version || '版本未记录'} · {row.split || 'Split 未记录'}</div>
                  {row.dataset_release_id
                    ? <div className="text-[11px] text-[var(--text-muted)]">{row.dataset_release_id}</div>
                    : <div className="text-[11px] text-[var(--text-muted)]">发行版未登记</div>}
                </div>
                <div className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
                  <div>{modelLabel || '模型未确认'}</div>
                  {row.model_route && <div className="text-[11px] text-[var(--text-muted)]">{row.model_route}</div>}
                  <div className="text-[var(--text-secondary)]">{harnessLabel(row)} · Runner {row.runner || '未记录'}</div>
                  {row.identity_status === 'unmapped' && <div className="text-[11px] text-[var(--text-muted)]">来源身份未匹配</div>}
                  {peer && <Link to={peer} className={linkClass}>{axis === 'harness' ? '在模型档案查看本批次' : '在 Harness 档案查看本批次'}</Link>}
                </div>
                <div className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
                  <div>{fmtPct(row.score)} · {row.metric_name || '指标未记录'}</div>
                  <div className="text-[11px] text-[var(--text-muted)]">{row.scoring_protocol} · {runScopeLabel(row.run_scope)}</div>
                  <div className="tabular-nums text-[var(--text-secondary)]">
                    {scoreLabels[row.score_kind]} · 分母 {row.denominator ?? '未知'}
                  </div>
                  <div className="text-[11px] tabular-nums text-[var(--text-muted)]">
                    有效 / 已录试验 {row.scored ?? '—'} / {row.recorded ?? '—'} · 题目覆盖 {row.unique_tasks ?? '—'} / {row.known_task_count ?? '—'}
                  </div>
                  {(row.attempts_per_task != null || row.timeout_seconds != null) && (
                    <div className="text-[11px] text-[var(--text-muted)]">
                      每题计划尝试 {row.attempts_per_task ?? '未记录'} · 超时 {row.timeout_seconds == null ? '未记录' : `${row.timeout_seconds} 秒`}
                    </div>
                  )}
                </div>
                <div className="flex min-w-0 items-start justify-between gap-2 [overflow-wrap:anywhere]">
                  <div className="min-w-0 space-y-1">
                    <div>{row.batch_name}</div>
                    {row.batch_status && <div className="text-[11px] text-[var(--text-muted)]">{row.batch_status}</div>}
                    {focused && <div className="text-[11px] text-[var(--text-secondary)]">定位批次 · 发布代 {row.generation_id}</div>}
                  </div>
                  <RowActionIcon
                    variant="details"
                    label={`查看批次详情：${row.batch_name}`}
                    to={`/eval/batches/${encodeURIComponent(row.batch_id)}`}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
