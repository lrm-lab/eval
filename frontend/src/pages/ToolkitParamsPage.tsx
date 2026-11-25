import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { GitCompareArrows, Plus, Sliders, Trash2 } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDuration, fmtPct } from '../lib/format';
import { useAppStore } from '../lib/store';
import { PageTransition } from '../components/motion/primitives';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

interface ParamBatchHydrated {
  batch_id: string;
  batch_name: string;
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
  dataset_release_id: string;
  batch_ids: string[];
  notes: string | null;
  batches: ParamBatchHydrated[];
}

interface BatchListOpt {
  batch_id: string;
  batch_name: string;
  model_canonical: string | null;
  harness: string | null;
  runner: string;
}

export function ToolkitParamsPage() {
  const user = useAppStore((s) => s.user);
  const pushToast = useAppStore((s) => s.pushToast);
  const clearCompare = useAppStore((s) => s.clearCompare);
  const addToCompare = useAppStore((s) => s.addToCompare);
  const navigate = useNavigate();
  const canWrite = Boolean(user && ['researcher', 'admin'].includes(user.role));

  const [topic, setTopic] = useState('');
  const [label, setLabel] = useState('');
  const [selectedBids, setSelectedBids] = useState<string[]>([]);

  const groupsQ = useQuery({
    queryKey: ['toolkit-param-groups'],
    queryFn: () => apiFetch<{ items: ParamGroupRow[] }>('/api/v1/toolkit/params'),
  });

  const batchesQ = useQuery({
    queryKey: ['toolkit-param-batches'],
    queryFn: () => apiFetch<{ items: BatchListOpt[] }>('/api/v1/batches?page_size=100'),
  });

  const createMut = useMutation({
    mutationFn: () =>
      apiFetch('/api/v1/toolkit/params', {
        method: 'POST',
        body: JSON.stringify({
          test_topic: topic.trim(),
          label: label.trim(),
          batch_ids: selectedBids,
          project_id: 'default',
        }),
      }),
    onSuccess: () => {
      pushToast('success', '已创建真实批次参数对照台账');
      setTopic('');
      setLabel('');
      setSelectedBids([]);
      groupsQ.refetch();
    },
    onError: (err: Error) => pushToast('error', err.message),
  });

  const deleteMut = useMutation({
    mutationFn: (gid: string) => apiFetch(`/api/v1/toolkit/params/${encodeURIComponent(gid)}`, { method: 'DELETE' }),
    onSuccess: () => {
      pushToast('success', '已删除参数对照台账');
      groupsQ.refetch();
    },
  });

  const sendGroupToCompare = (g: ParamGroupRow) => {
    clearCompare();
    g.batches.slice(0, 6).forEach((b, idx) => {
      addToCompare({
        id: `s${idx + 1}`,
        kind: 'batch',
        batch_id: b.batch_id,
        alias: b.batch_name,
      });
    });
    navigate('/eval/compare?fromDock=1');
  };

  const toggleBid = (bid: string) => {
    setSelectedBids((prev) => (prev.includes(bid) ? prev.filter((x) => x !== bid) : [...prev, bid].slice(0, 12)));
  };

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div>
        <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
          <Sliders className="w-5 h-5 text-[var(--brand-primary)]" />
          <span>运行参数跨批次对照台账（100% 绑定真实 batch_id）</span>
        </h1>
        <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
          聚合 Harbor / Direct / VLMEvalKit 真实运行配置与指标（严禁手填虚构分数或假 TTFT），一键送入自由对比工作台
        </p>
      </div>

      {canWrite && (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3 text-xs">
          <div className="font-bold">新建参数对照台账（勾选 2–12 个真实已索引批次）</div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <input
              type="text"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="实验主题 (如 Thinking on vs off 消融)"
              className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5"
            />
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="对比组名 (如 Qwen3.8-27B CMMMU)"
              className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5"
            />
          </div>
          <div className="max-h-36 overflow-y-auto rounded-lg border border-[var(--border-subtle)] p-2 flex flex-wrap gap-1.5">
            {(batchesQ.data?.items || []).slice(0, 40).map((b) => {
              const active = selectedBids.includes(b.batch_id);
              return (
                <button
                  key={b.batch_id}
                  type="button"
                  onClick={() => toggleBid(b.batch_id)}
                  className={`rounded-full px-2.5 py-1 text-[11px] font-mono border cursor-pointer ${
                    active
                      ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                      : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                  }`}
                >
                  {b.batch_name}
                </button>
              );
            })}
          </div>
          <button
            type="button"
            disabled={!topic.trim() || !label.trim() || selectedBids.length < 2 || createMut.isPending}
            onClick={() => createMut.mutate()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] disabled:opacity-40 px-3 py-1.5 text-white font-semibold cursor-pointer"
          >
            <Plus className="w-3.5 h-3.5" /> 创建台账（已选 {selectedBids.length} 批次）
          </button>
        </div>
      )}

      {groupsQ.isLoading ? (
        <TableSkeleton rows={4} cols={5} />
      ) : (groupsQ.data?.items || []).length === 0 ? (
        <StateNoticeCard kind="empty" title="尚无参数对照台账" description="请在上方选择 2 个及以上真实批次创建消融对照台账。" />
      ) : (
        <div className="space-y-4">
          {groupsQ.data!.items.map((g) => (
            <div key={g.group_id} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden text-xs">
              <div className="px-4 py-2.5 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-2">
                <div>
                  <span className="font-bold text-[var(--text-primary)]">{g.test_topic}</span>{' '}
                  <span className="text-[var(--text-muted)]">· {g.label}</span>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => sendGroupToCompare(g)}
                    className="inline-flex items-center gap-1 rounded-lg bg-[var(--brand-primary)] px-2.5 py-1 text-[11px] font-semibold text-white cursor-pointer"
                  >
                    <GitCompareArrows className="w-3.5 h-3.5" /> 一键送入对比
                  </button>
                  {canWrite && (
                    <button
                      type="button"
                      onClick={() => deleteMut.mutate(g.group_id)}
                      className="text-[var(--text-muted)] hover:text-[var(--sem-err-fg)] cursor-pointer"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse">
                  <thead>
                    <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                      <th className="py-2 px-3">批次</th>
                      <th className="py-2 px-3">Runner / Harness / Thinking</th>
                      <th className="py-2 px-3">Scope / 协议</th>
                      <th className="py-2 px-3 text-right">主分</th>
                      <th className="py-2 px-3 text-right">覆盖 / 均耗时 / 故障</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)] tabular-nums">
                    {g.batches.map((b) => {
                      const mainScore = b.official_score ?? b.valid_mean;
                      return (
                        <tr key={b.batch_id}>
                          <td className="py-2 px-3 font-mono font-medium">{b.batch_name}</td>
                          <td className="py-2 px-3">
                            {b.runner} / {b.harness || '—'}
                            {b.harness_version ? `@${b.harness_version}` : ''} · {b.thinking_mode || 'default'}
                          </td>
                          <td className="py-2 px-3 font-mono text-[11px]">
                            {b.run_scope} · {b.scoring_protocol}
                          </td>
                          <td className="py-2 px-3 text-right font-bold">
                            {mainScore !== null ? fmtPct(mainScore <= 1 ? mainScore * 100 : mainScore) : '—'}
                          </td>
                          <td className="py-2 px-3 text-right">
                            {b.scored}/{b.planned ?? '—'} · {fmtDuration(b.avg_duration_sec)} · 故障 {b.failed_trials}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </PageTransition>
  );
}
