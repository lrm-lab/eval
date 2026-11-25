import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { Award, Compass, Plus, Sliders } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDuration, fmtPct, fmtTokens, trackDisplayName } from '../lib/format';
import { useAppStore } from '../lib/store';
import { PageTransition } from '../components/motion/primitives';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

interface RecommendItem {
  subject_id: string;
  model_canonical: string;
  harness: string;
  runner: string;
  weighted_score: number;
  track_scores: Record<string, number>;
  covered_tracks: number;
  avg_duration_sec: number | null;
  avg_total_tokens: number | null;
  sample_batch_ids: string[];
  pareto_optimal: boolean;
  tradeoff_note: string;
}

interface RecommendResp {
  items: RecommendItem[];
  total: number;
}

export function RecommendPage() {
  const addToCompare = useAppStore((s) => s.addToCompare);
  const [wAgentic, setWAgentic] = useState(50);
  const [wText, setWText] = useState(30);
  const [wVision, setWVision] = useState(20);
  const [minScore, setMinScore] = useState('');
  const [maxDuration, setMaxDuration] = useState('');
  const [maxTokens, setMaxTokens] = useState('');
  const [formalOnly, setFormalOnly] = useState(false);

  const mut = useMutation({
    mutationFn: () =>
      apiFetch<RecommendResp>('/api/v1/recommend', {
        method: 'POST',
        body: JSON.stringify({
          track_weights: {
            agentic: wAgentic / 100,
            'llm-text': wText / 100,
            'vision-image': wVision / 100,
          },
          min_score_pct: minScore ? Number(minScore) : null,
          max_avg_duration_sec: maxDuration ? Number(maxDuration) : null,
          max_avg_tokens: maxTokens ? Number(maxTokens) : null,
          formal_only: formalOnly,
        }),
      }),
  });

  useEffect(() => {
    mut.mutate();
  }, []);

  const items = mut.data?.items || [];

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div>
        <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
          <Compass className="w-5 h-5 text-[var(--brand-primary)]" />
          <span>约束驱动选型推荐（Constraint Recommender）</span>
        </h1>
        <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
          仅基于本平台已实测的轨道得分、单题平均耗时与总 Token 消耗计算 Pareto 前沿推荐，绝不使用外部商业报价或虚构字段
        </p>
      </div>

      {/* Real Harbor Constraint Form */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mut.mutate();
        }}
        className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-4 text-xs"
      >
        <div className="flex items-center gap-2 font-semibold text-[var(--text-primary)]">
          <Sliders className="w-4 h-4 text-[var(--brand-primary)]" />
          <span>三线权重与实测效率约束</span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <label className="block">
            <div className="flex justify-between mb-1">
              <span className="text-[var(--text-secondary)]">Agentic 提分权重</span>
              <span className="font-mono font-semibold">{wAgentic}%</span>
            </div>
            <input type="range" min={0} max={100} value={wAgentic} onChange={(e) => setWAgentic(Number(e.target.value))} className="w-full" />
          </label>
          <label className="block">
            <div className="flex justify-between mb-1">
              <span className="text-[var(--text-secondary)]">LLM-Text 护栏权重</span>
              <span className="font-mono font-semibold">{wText}%</span>
            </div>
            <input type="range" min={0} max={100} value={wText} onChange={(e) => setWText(Number(e.target.value))} className="w-full" />
          </label>
          <label className="block">
            <div className="flex justify-between mb-1">
              <span className="text-[var(--text-secondary)]">Vision-Image 护栏权重</span>
              <span className="font-mono font-semibold">{wVision}%</span>
            </div>
            <input type="range" min={0} max={100} value={wVision} onChange={(e) => setWVision(Number(e.target.value))} className="w-full" />
          </label>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 items-end">
          <label className="block">
            <span className="text-[var(--text-secondary)] block mb-1">最低加权得分 (%)</span>
            <input
              type="number"
              min={0}
              max={100}
              step="0.5"
              value={minScore}
              onChange={(e) => setMinScore(e.target.value)}
              placeholder="例如 30"
              className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-primary)]"
            />
          </label>

          <label className="block">
            <span className="text-[var(--text-secondary)] block mb-1">最大平均单题耗时 (s)</span>
            <input
              type="number"
              min={1}
              value={maxDuration}
              onChange={(e) => setMaxDuration(e.target.value)}
              placeholder="例如 300"
              className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-primary)]"
            />
          </label>

          <label className="block">
            <span className="text-[var(--text-secondary)] block mb-1">最大均总 Tokens</span>
            <input
              type="number"
              min={100}
              value={maxTokens}
              onChange={(e) => setMaxTokens(e.target.value)}
              placeholder="例如 200000"
              className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-primary)]"
            />
          </label>

          <div className="flex items-center justify-between gap-2">
            <label className="inline-flex items-center gap-1.5 cursor-pointer text-[var(--text-secondary)]">
              <input type="checkbox" checked={formalOnly} onChange={(e) => setFormalOnly(e.target.checked)} />
              <span>仅 Formal Full</span>
            </label>
            <button
              type="submit"
              disabled={mut.isPending}
              className="rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-4 py-1.5 font-semibold text-white cursor-pointer"
            >
              {mut.isPending ? '计算中...' : '计算推荐'}
            </button>
          </div>
        </div>
      </form>

      {mut.isPending ? (
        <TableSkeleton rows={6} cols={6} />
      ) : mut.isError ? (
        <StateNoticeCard kind="error" title="推荐计算失败" description={(mut.error as Error).message} onRetry={() => mut.mutate()} />
      ) : items.length === 0 ? (
        <StateNoticeCard kind="empty" title="当前约束下无满足条件的实测组合" description="可适当放宽得分下限或单题耗时/Token 上限。" />
      ) : (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                <th className="py-2.5 px-3.5">候选模型 × Harness</th>
                <th className="py-2.5 px-3 text-right">加权综合分</th>
                <th className="py-2.5 px-3">分轨道均分</th>
                <th className="py-2.5 px-3 text-right">平均耗时</th>
                <th className="py-2.5 px-3 text-right">均总 Tokens</th>
                <th className="py-2.5 px-3.5 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {items.map((item) => (
                <tr key={item.subject_id} className="hover:bg-[var(--bg-elevated)]/65">
                  <td className="py-2.5 px-3.5">
                    <div className="flex items-center gap-2">
                      <Link
                        to={`/models/${encodeURIComponent(item.model_canonical)}`}
                        className="font-semibold text-[var(--text-primary)] hover:text-[var(--brand-primary)] no-underline"
                      >
                        {item.model_canonical}
                      </Link>
                      {item.pareto_optimal && (
                        <span className="inline-flex items-center gap-0.5 rounded-full px-2 py-0.5 text-[10px] font-semibold bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]">
                          <Award className="w-3 h-3" /> Pareto 最优
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] text-[var(--text-muted)]">
                      {item.harness} · {item.runner} · {item.tradeoff_note}
                    </div>
                  </td>
                  <td className="py-2.5 px-3 text-right font-bold text-sm tabular-nums text-[var(--brand-primary)]">
                    {fmtPct(item.weighted_score)}
                  </td>
                  <td className="py-2.5 px-3">
                    <div className="flex flex-wrap gap-1">
                      {Object.entries(item.track_scores).map(([ts, sc]) => (
                        <span
                          key={ts}
                          className="rounded px-1.5 py-0.5 text-[10px] tabular-nums bg-[var(--bg-elevated)] text-[var(--text-secondary)]"
                        >
                          {trackDisplayName(ts).split(' ')[0]}: {fmtPct(sc)}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="py-2.5 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                    {fmtDuration(item.avg_duration_sec)}
                  </td>
                  <td className="py-2.5 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                    {fmtTokens(item.avg_total_tokens)}
                  </td>
                  <td className="py-2.5 px-3.5 text-right">
                    {item.sample_batch_ids[0] && (
                      <button
                        type="button"
                        onClick={() =>
                          addToCompare({
                            id: item.sample_batch_ids[0],
                            kind: 'batch',
                            batch_id: item.sample_batch_ids[0],
                            alias: `${item.model_canonical} (${item.harness})`,
                          })
                        }
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
      )}
    </PageTransition>
  );
}
