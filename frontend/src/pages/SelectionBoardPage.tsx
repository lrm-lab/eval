import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import type * as echarts from 'echarts';
import { AlertCircle, BookmarkPlus, Layers, Lock, SlidersHorizontal } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { escapeHtml, fmtPct } from '../lib/format';
import { useAppStore } from '../lib/store';
import { PageTransition } from '../components/motion/primitives';
import { EChartBox, glassTooltipConfig } from '../components/charts/EChartBox';
import { CardGridSkeleton, StateNoticeCard } from '../components/ui/Skeletons';

interface SelBatchItem {
  batch_id: string;
  batch_name: string;
  model_canonical: string | null;
  harness: string | null;
  unique_tasks: number;
  recorded: number;
  has_selection: boolean;
}

interface SelBoardResp {
  batch_id: string;
  batch_name: string;
  dataset_release_id: string | null;
  unique_tasks: number;
  total_attempts: number;
  required_attempts: number;
  min_grpo: number;
  threshold_met: boolean;
  totals: Record<string, number>;
  redacted: boolean;
  redacted_reason?: string | null;
  simulated?: boolean;
  curves: Array<{
    k: number;
    pass_at_k: number | null;
    pass_hat_k: number | null;
    computable_tasks: number;
    total_tasks: number;
    pass_at_k_ci95: [number, number] | null;
  }>;
  tasks: Array<{
    task_id: string;
    lane: 'grpo' | 'teacher' | 'regression' | 'review';
    attempts: number;
    scored: number;
    passed: number;
    pass_at_k: Record<string, number | null>;
  }>;
}

const LANE_META: Record<string, { title: string; desc: string; tone: string }> = {
  grpo: { title: 'GRPO RL 候选', desc: '部分通过且满足有效尝试数，最适合强化学习探索', tone: 'text-[var(--brand-primary)]' },
  teacher: { title: 'Teacher / SFT 候选', desc: '有效尝试全部失败 (0/k)，优先蒸馏或强教师示范', tone: 'text-[var(--sem-zero-fg)]' },
  regression: { title: 'Regression 回归集', desc: '有效尝试全部通过 (k/k)，沉淀为防退化守护集', tone: 'text-[var(--sem-pass-fg)]' },
  review: { title: 'Review 待人工排查', desc: '含故障尝试导致有效数不足 required_attempts', tone: 'text-[var(--sem-err-fg)]' },
};

export function SelectionBoardPage() {
  const theme = useAppStore((s) => s.theme);
  const user = useAppStore((s) => s.user);
  const pushToast = useAppStore((s) => s.pushToast);
  const canWrite = Boolean(user && ['researcher', 'admin'].includes(user.role));

  const [batchId, setBatchId] = useState('');
  const [reqAttempts, setReqAttempts] = useState(8);
  const [minGrpo, setMinGrpo] = useState(10);
  const [activeLane, setActiveLane] = useState<string>('all');
  const [simData, setSimData] = useState<SelBoardResp | null>(null);

  const batchesQ = useQuery({
    queryKey: ['synth-selection-batches'],
    queryFn: () => apiFetch<{ items: SelBatchItem[] }>('/api/v1/synth/selection/batches'),
  });

  useEffect(() => {
    if (!batchId && batchesQ.data?.items?.length) {
      const preferred = batchesQ.data.items.find((b) => b.has_selection) || batchesQ.data.items[0];
      setBatchId(preferred.batch_id);
    }
  }, [batchesQ.data, batchId]);

  const boardQ = useQuery({
    queryKey: ['synth-selection-board', batchId],
    enabled: Boolean(batchId),
    queryFn: () => apiFetch<SelBoardResp>(`/api/v1/synth/selection/${encodeURIComponent(batchId)}`),
  });

  useEffect(() => {
    setSimData(null);
    if (boardQ.data) {
      setReqAttempts(boardQ.data.required_attempts);
      setMinGrpo(boardQ.data.min_grpo);
    }
  }, [boardQ.data]);

  const previewMut = useMutation({
    mutationFn: () =>
      apiFetch<SelBoardResp>(`/api/v1/synth/selection/${encodeURIComponent(batchId)}/preview`, {
        method: 'POST',
        body: JSON.stringify({ required_attempts: reqAttempts, min_grpo: minGrpo }),
      }),
    onSuccess: (res) => {
      setSimData(res);
      pushToast('success', '已完成 4 通道重分桶模拟');
    },
    onError: (err: Error) => pushToast('error', `模拟失败：${err.message}`),
  });

  const saveLaneSetMut = useMutation({
    mutationFn: (lane: string) => {
      const board = simData || boardQ.data;
      const tids = (board?.tasks || []).filter((t) => t.lane === lane).map((t) => t.task_id);
      return apiFetch('/api/v1/sample-sets', {
        method: 'POST',
        body: JSON.stringify({
          name: `${board?.batch_name || batchId} · ${lane}`,
          project_id: 'default',
          dataset_release_id: board?.dataset_release_id || 'custom',
          task_ids: tids,
        }),
      });
    },
    onSuccess: () => pushToast('success', '已将该通道题目保存为自定义样本集'),
    onError: (err: Error) => pushToast('error', `保存样本集失败：${err.message}`),
  });

  const board = simData || boardQ.data;

  const curveOption = useMemo<echarts.EChartsCoreOption>(() => {
    const pts = board?.curves || [];
    return {
      grid: { left: 52, right: 24, top: 28, bottom: 36 },
      legend: { top: 0, textStyle: { color: theme === 'dark' ? '#d4d4d8' : '#334155', fontSize: 11 } },
      tooltip: {
        ...glassTooltipConfig(theme),
        trigger: 'axis',
        formatter: (params: unknown) => {
          const arr = Array.isArray(params) ? params : [params];
          const idx = (arr[0] as { dataIndex?: number })?.dataIndex ?? 0;
          const p = pts[idx];
          if (!p) return '';
          return `<div><strong>k = ${p.k}</strong>（可算分母 <b>${p.computable_tasks}/${p.total_tasks}</b>）<br/>pass@${p.k}: <b>${p.pass_at_k !== null ? escapeHtml(fmtPct(p.pass_at_k * 100)) : '—'}</b><br/>pass^${p.k}: <b>${p.pass_hat_k !== null ? escapeHtml(fmtPct(p.pass_hat_k * 100)) : '—'}</b></div>`;
        },
      },
      xAxis: {
        type: 'category',
        data: pts.map((p) => `k=${p.k} (${p.computable_tasks}/${p.total_tasks})`),
        axisLabel: { fontSize: 11, color: theme === 'dark' ? '#d4d4d8' : '#334155' },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { formatter: '{value}%' },
        splitLine: { lineStyle: { color: theme === 'dark' ? '#27272a' : '#e5e7eb' } },
      },
      series: [
        {
          name: 'pass@k (%)',
          type: 'line',
          smooth: true,
          data: pts.map((p) => (p.pass_at_k !== null ? Number((p.pass_at_k * 100).toFixed(2)) : null)),
          itemStyle: { color: '#2563eb' },
        },
        {
          name: 'pass^k 稳定性 (%)',
          type: 'line',
          smooth: true,
          data: pts.map((p) => (p.pass_hat_k !== null ? Number((p.pass_hat_k * 100).toFixed(2)) : null)),
          itemStyle: { color: '#10b981' },
        },
      ],
    };
  }, [board, theme]);

  const visibleTasks = useMemo(() => {
    const all = board?.tasks || [];
    return activeLane === 'all' ? all : all.filter((t) => t.lane === activeLane);
  }, [board, activeLane]);

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <Layers className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>RL / SFT 四通道选题板与 pass@k 缺测曲线</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            真实对齐 197 selection.json（如 round1 40 题 × 8 尝试 = 320 trials，pass@8 可算分母 19/40）
          </p>
        </div>

        <select
          aria-label="选择多尝试批次"
          value={batchId}
          onChange={(e) => setBatchId(e.target.value)}
          className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs font-mono"
        >
          {(batchesQ.data?.items || []).map((b) => (
            <option key={b.batch_id} value={b.batch_id}>
              {b.batch_name} ({b.unique_tasks} 题 / {b.recorded} 尝试){b.has_selection ? ' ★' : ''}
            </option>
          ))}
        </select>
      </div>

      {boardQ.isLoading ? (
        <CardGridSkeleton count={4} />
      ) : board ? (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {(['grpo', 'teacher', 'regression', 'review'] as const).map((lane) => {
              const cnt = (board.totals || {})[lane] || 0;
              const meta = LANE_META[lane];
              return (
                <div
                  key={lane}
                  onClick={() => setActiveLane(activeLane === lane ? 'all' : lane)}
                  className={`rounded-xl border p-4 cursor-pointer transition-colors ${
                    activeLane === lane
                      ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)]/40'
                      : 'border-[var(--border-subtle)] bg-[var(--bg-surface)]'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className={`text-xs font-bold ${meta.tone}`}>{meta.title}</span>
                    {canWrite && cnt > 0 && !board.redacted && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          saveLaneSetMut.mutate(lane);
                        }}
                        title="保存此通道为自定义样本集"
                        className="text-[var(--text-muted)] hover:text-[var(--brand-primary)] cursor-pointer"
                      >
                        <BookmarkPlus className="w-3.5 h-3.5" />
                      </button>
                    )}
                  </div>
                  <div className="mt-1 text-2xl font-bold tabular-nums">{cnt}</div>
                  <div className="mt-1 text-[11px] text-[var(--text-muted)]">{meta.desc}</div>
                </div>
              );
            })}
          </div>

          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 text-xs flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <AlertCircle className={`w-4 h-4 ${board.threshold_met ? 'text-[var(--sem-pass-fg)]' : 'text-[var(--sem-zero-fg)]'}`} />
              <span>
                GRPO 门槛 <code>min_grpo = {board.min_grpo}</code>：
                <strong>{board.threshold_met ? '已达标' : '未达标 (threshold_met = false)'}</strong>
                {board.simulated ? '（当前为模拟阈值结果）' : ''}
              </span>
            </div>
            {canWrite && (
              <div className="flex flex-wrap items-center gap-2">
                <SlidersHorizontal className="w-3.5 h-3.5 text-[var(--text-muted)]" />
                <label className="inline-flex items-center gap-1">
                  required_attempts
                  <input
                    type="number"
                    min={1}
                    max={32}
                    value={reqAttempts}
                    onChange={(e) => setReqAttempts(Number(e.target.value) || 1)}
                    className="w-14 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-1.5 py-0.5 font-mono"
                  />
                </label>
                <label className="inline-flex items-center gap-1">
                  min_grpo
                  <input
                    type="number"
                    min={1}
                    max={500}
                    value={minGrpo}
                    onChange={(e) => setMinGrpo(Number(e.target.value) || 1)}
                    className="w-16 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-1.5 py-0.5 font-mono"
                  />
                </label>
                <button
                  type="button"
                  disabled={previewMut.isPending}
                  onClick={() => previewMut.mutate()}
                  className="rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-3 py-1 text-white font-semibold cursor-pointer"
                >
                  模拟重分桶
                </button>
              </div>
            )}
          </div>

          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
            <div className="text-xs font-bold mb-2">
              pass@k 与 pass^k 稳定性曲线（严格标明各 k 可算分母，n_scored &lt; k 记缺测）
            </div>
            <EChartBox option={curveOption} height={300} exportName={`selection-${board.batch_id}`} />
          </div>

          {board.redacted && board.tasks.length === 0 ? (
            <StateNoticeCard kind="forbidden" title="留出集逐题明细受保护" description={board.redacted_reason || '缺少 dataset:holdout_read 权限'} />
          ) : (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
              {board.redacted && (
                <div className="px-4 py-2 bg-[var(--sem-zero-bg)] border-b border-[var(--sem-zero-border)] text-xs flex items-center gap-2 text-[var(--text-secondary)]">
                  <Lock className="w-3.5 h-3.5 text-[var(--sem-zero-fg)]" />
                  <span>{board.redacted_reason}</span>
                </div>
              )}
              <div className="px-4 py-2.5 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex items-center justify-between text-xs">
                <span className="font-bold">逐题分桶明细（显示 {visibleTasks.length} / {board.tasks.length} 题）</span>
                <Link to={`/eval/batches/${encodeURIComponent(board.batch_id)}`} className="text-[var(--brand-primary)] no-underline hover:underline">
                  查看批次完整报告 →
                </Link>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                      <th className="py-2 px-3">Task ID</th>
                      <th className="py-2 px-3">通道</th>
                      <th className="py-2 px-3 text-right">通过 / 有效 / 尝试</th>
                      <th className="py-2 px-3 text-right">pass@1</th>
                      <th className="py-2 px-3 text-right">pass@{board.required_attempts}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)] tabular-nums">
                    {visibleTasks.map((t) => {
                      const p1 = (t.pass_at_k || {})['k1'] ?? null;
                      const pk = (t.pass_at_k || {})[`k${board.required_attempts}`] ?? null;
                      return (
                        <tr key={t.task_id} className="hover:bg-[var(--bg-elevated)]/60">
                          <td className="py-2 px-3 font-mono">{t.task_id}</td>
                          <td className="py-2 px-3 font-mono font-semibold">{t.lane}</td>
                          <td className="py-2 px-3 text-right">
                            {t.passed} / {t.scored} / {t.attempts}
                          </td>
                          <td className="py-2 px-3 text-right">{p1 !== null ? fmtPct(p1 * 100) : '—'}</td>
                          <td className="py-2 px-3 text-right">{pk !== null ? fmtPct(pk * 100) : '— (缺测)'}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      ) : (
        <StateNoticeCard kind="empty" title="暂无多尝试评测批次" />
      )}
    </PageTransition>
  );
}
