import React, { useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { FileSpreadsheet, Plus, Search } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDateShort, fmtPct, runScopeLabel } from '../lib/format';
import { useAppStore } from '../lib/store';
import type { BatchDTO } from '../types/api';
import { PageTransition, SlidingTabs } from '../components/motion/primitives';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

interface BatchListResp {
  items: BatchDTO[];
  total: number;
  page: number;
  page_size: number;
}

export function ReportsPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const addToCompare = useAppStore((s) => s.addToCompare);

  const [groupBy, setGroupBy] = useState<'batch' | 'model' | 'benchmark'>('batch');
  const runner = searchParams.get('runner') || '';
  const runScope = searchParams.get('run_scope') || '';
  const modality = searchParams.get('modality') || '';
  const hasGateway = searchParams.get('has_gateway') === 'true';
  const q = searchParams.get('q') || '';
  const page = Number(searchParams.get('page') || '1');

  const updateParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (!v) next.delete(k);
    else next.set(k, v);
    if (k !== 'page') next.delete('page');
    setSearchParams(next, { replace: true });
  };

  const url = useMemo(() => {
    const qs = new URLSearchParams();
    qs.set('page', String(page));
    qs.set('page_size', '100');
    if (runner) qs.set('runner', runner);
    if (runScope) qs.set('run_scope', runScope);
    if (modality) qs.set('modality', modality);
    if (hasGateway) qs.set('has_gateway', 'true');
    if (q.trim()) qs.set('q', q.trim());
    return `/api/v1/batches?${qs.toString()}`;
  }, [page, runner, runScope, modality, hasGateway, q]);

  const listQ = useQuery({
    queryKey: ['reports-batches', url],
    queryFn: () => apiFetch<BatchListResp>(url),
  });

  const items = listQ.data?.items || [];

  const grouped = useMemo(() => {
    if (groupBy === 'batch') return null;
    const map = new Map<string, BatchDTO[]>();
    for (const b of items) {
      const key =
        groupBy === 'model'
          ? b.model_canonical || b.model_route || '未命名模型'
          : b.benchmark_slug || b.raw_dataset_name || '未映射基准';
      const arr = map.get(key) || [];
      arr.push(b);
      map.set(key, arr);
    }
    return Array.from(map.entries()).sort((a, b) => b[1].length - a[1].length);
  }, [items, groupBy]);

  const renderStatusBadge = (b: BatchDTO) => {
    if (b.is_running_placeholder) {
      return (
        <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium bg-[var(--sem-running-bg)] text-[var(--sem-running-fg)]">
          <span className="w-1.5 h-1.5 rounded-full bg-[var(--sem-running-fg)] lrm-calm-pulse" /> 运行中占位
        </span>
      );
    }
    if (b.runner === 'vlmevalkit') {
      if (b.scored === 0 && b.official_result) {
        return (
          <span className="rounded-full px-2 py-0.5 text-[10px] bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]">
            VLM 仅官方汇总
          </span>
        );
      }
      return (
        <span className="rounded-full px-2 py-0.5 text-[10px] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]">
          VLM 逐题校验
        </span>
      );
    }
    return (
      <span className="rounded-full px-2 py-0.5 text-[10px] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
        {b.official_status}
      </span>
    );
  };

  const renderTable = (rows: BatchDTO[]) => (
    <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
      <table className="w-full text-left border-collapse text-xs">
        <thead>
          <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
            <th className="py-2.5 px-3.5">批次名称 / 来源</th>
            <th className="py-2.5 px-3">基准 / 协议</th>
            <th className="py-2.5 px-3">模型 / Harness</th>
            <th className="py-2.5 px-3">状态 / 范围</th>
            <th className="py-2.5 px-3 text-right">覆盖</th>
            <th className="py-2.5 px-3 text-right">有效均值 / 官方分</th>
            <th className="py-2.5 px-3.5 text-right">操作</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--border-subtle)]">
          {rows.map((b) => (
            <tr
              key={b.batch_id}
              onClick={() => navigate(`/eval/batches/${b.batch_id}`)}
              className="hover:bg-[var(--bg-elevated)]/65 cursor-pointer transition-colors"
            >
              <td className="py-2.5 px-3.5 max-w-[260px]">
                <div className="font-semibold text-[var(--text-primary)] truncate" title={b.batch_name}>
                  {b.batch_name}
                </div>
                <div className="text-[10px] font-mono text-[var(--text-muted)] truncate">
                  {b.source_namespace} · {fmtDateShort(b.finished_at || b.indexed_at)}
                </div>
              </td>
              <td className="py-2.5 px-3">
                <div className="font-medium text-[var(--text-primary)]">{b.benchmark_slug || b.raw_dataset_name || '—'}</div>
                <div className="text-[10px] font-mono text-[var(--text-muted)]">{b.scoring_protocol}</div>
              </td>
              <td className="py-2.5 px-3">
                <div className="text-[var(--text-primary)]">{b.model_canonical || b.model_route || '—'}</div>
                <div className="text-[10px] text-[var(--text-muted)]">
                  {b.harness || b.runner} · {b.runner} {b.has_gateway ? '· GW' : ''}
                </div>
              </td>
              <td className="py-2.5 px-3">
                <div className="flex flex-wrap items-center gap-1">
                  {renderStatusBadge(b)}
                  <span className="rounded px-1.5 py-0.5 text-[10px] bg-[var(--bg-elevated)] text-[var(--text-muted)]">
                    {runScopeLabel(b.run_scope)}
                  </span>
                </div>
              </td>
              <td className="py-2.5 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                {b.scored}/{b.planned || b.recorded}
              </td>
              <td className="py-2.5 px-3 text-right tabular-nums">
                <div className="font-bold text-[var(--text-primary)]">
                  {b.valid_mean !== null && b.valid_mean !== undefined
                    ? fmtPct(b.valid_mean <= 1 ? b.valid_mean * 100 : b.valid_mean)
                    : '—'}
                </div>
                {b.official_result && (
                  <div className="text-[10px] text-[var(--sem-pass-fg)]">
                    官方: {b.official_result.score} ({b.official_result.metric})
                  </div>
                )}
              </td>
              <td className="py-2.5 px-3.5 text-right" onClick={(e) => e.stopPropagation()}>
                {!b.is_running_placeholder && (
                  <button
                    type="button"
                    onClick={() => addToCompare({ id: b.batch_id, kind: 'batch', batch_id: b.batch_id, alias: b.batch_name })}
                    className="inline-flex items-center gap-1 rounded border border-[var(--border-subtle)] px-2 py-1 text-[11px] text-[var(--text-secondary)] hover:text-[var(--brand-primary)] cursor-pointer"
                  >
                    <Plus className="w-3 h-3" /> 对比
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <FileSpreadsheet className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>评测报告中心（Reports &amp; Batches）</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            统一纳管热层 Harbor / Direct / VLMEvalKit 批次与 2026-11-20 冷层作业，共 {listQ.data?.total ?? '—'} 批
          </p>
        </div>

        <SlidingTabs
          layoutId="reports-group-tabs"
          size="sm"
          value={groupBy}
          onChange={(v) => setGroupBy(v as 'batch' | 'model' | 'benchmark')}
          options={[
            { id: 'batch', label: '按批次列表' },
            { id: 'model', label: '按模型分组' },
            { id: 'benchmark', label: '按基准分组' },
          ]}
        />
      </div>

      {/* Filters */}
      <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3 flex flex-wrap items-center gap-2 text-xs">
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="w-3.5 h-3.5 text-[var(--text-muted)] absolute left-2.5 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={q}
            onChange={(e) => updateParam('q', e.target.value || null)}
            placeholder="过滤批次名 / 模型 / 基准..."
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] pl-8 pr-3 py-1.5 text-xs text-[var(--text-primary)] outline-none focus:border-[var(--brand-primary)]"
          />
        </div>

        <select
          aria-label="筛选 Runner"
          value={runner}
          onChange={(e) => updateParam('runner', e.target.value || null)}
          className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs cursor-pointer"
        >
          <option value="">Runner: 全部</option>
          <option value="harbor">harbor</option>
          <option value="direct">direct</option>
          <option value="vlmevalkit">vlmevalkit</option>
          <option value="legacy">legacy</option>
        </select>

        <select
          aria-label="筛选范围"
          value={runScope}
          onChange={(e) => updateParam('run_scope', e.target.value || null)}
          className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs cursor-pointer"
        >
          <option value="">范围: 全部</option>
          <option value="full_split">Formal Full</option>
          <option value="subset">Subset</option>
          <option value="smoke">Smoke</option>
          <option value="reference">Reference</option>
        </select>

        <select
          aria-label="筛选模态"
          value={modality}
          onChange={(e) => updateParam('modality', e.target.value || null)}
          className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs cursor-pointer"
        >
          <option value="">模态: 全部</option>
          <option value="agentic">agentic</option>
          <option value="text">text</option>
          <option value="vision">vision</option>
          <option value="multimodal">multimodal</option>
        </select>

        <button
          type="button"
          onClick={() => updateParam('has_gateway', hasGateway ? null : 'true')}
          className={`rounded-lg border px-2.5 py-1.5 font-medium cursor-pointer ${
            hasGateway
              ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)]'
              : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
          }`}
        >
          仅含网关抓包
        </button>
      </div>

      {listQ.isLoading ? (
        <TableSkeleton rows={10} cols={6} />
      ) : listQ.isError ? (
        <StateNoticeCard kind="error" title="批次列表加载失败" description={(listQ.error as Error).message} onRetry={() => listQ.refetch()} />
      ) : items.length === 0 ? (
        <StateNoticeCard kind="empty" title="当前筛选条件下无匹配批次" />
      ) : groupBy === 'batch' ? (
        renderTable(items)
      ) : (
        <div className="space-y-4">
          {grouped?.map(([groupKey, groupRows]) => (
            <div key={groupKey} className="space-y-2">
              <div className="flex items-center justify-between text-xs px-1">
                <Link
                  to={groupBy === 'model' ? `/models/${encodeURIComponent(groupKey)}` : `/benchmarks/${encodeURIComponent(groupKey)}`}
                  className="font-bold text-sm text-[var(--text-primary)] hover:text-[var(--brand-primary)] no-underline"
                >
                  {groupKey}
                </Link>
                <span className="text-[var(--text-muted)] tabular-nums">{groupRows.length} 批</span>
              </div>
              {renderTable(groupRows)}
            </div>
          ))}
        </div>
      )}
    </PageTransition>
  );
}
