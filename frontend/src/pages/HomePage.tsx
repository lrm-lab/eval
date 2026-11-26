import { ArrowRight, GitCompareArrows, Play, ShieldCheck, Trophy } from 'lucide-react';
import { Link } from 'react-router-dom';
import { PageTransition } from '../components/motion/primitives';
import { HomeEvaluationHeatmap, useHomeEvaluationMap } from '../components/ui/HomeEvaluationHeatmap';
import { fmtDateShort } from '../lib/format';
import type { EvaluationMap, MapBatch } from '../lib/evaluationMap';

const navigationLink = 'lrm-press inline-flex min-h-9 shrink-0 items-center justify-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs font-semibold text-[var(--text-primary)] no-underline hover:bg-[var(--bg-elevated)] sm:px-3 sm:text-sm';

const statusLabels: Record<string, string> = {
  running: '运行中', queued: '排队中', pending: '待开始', created: '已创建',
  starting: '启动中', preparing: '准备中', paused: '已暂停', cancelling: '取消中', retrying: '重试中',
  finished: '已完成', completed: '已完成', succeeded: '已完成', failed: '失败', interrupted: '已中断',
  cancelled: '已取消', canceled: '已取消', stopped: '已停止',
};

const inProgress = new Set(['running', 'queued', 'pending', 'created', 'starting', 'preparing', 'paused', 'cancelling', 'retrying']);

function recentBatches(data: EvaluationMap | undefined) {
  const batches = new Map<string, MapBatch>();
  for (const cell of data?.cells ?? []) {
    for (const batch of [cell.latest_batch, cell.latest_scored_batch]) {
      if (batch) batches.set(batch.batch_id, batch);
    }
  }
  const rows = [...batches.values()];
  const running = rows.filter((batch) => inProgress.has(batch.batch_status ?? ''));
  const ended = rows.filter((batch) => batch.batch_status && !inProgress.has(batch.batch_status))
    .sort((a, b) => (b.recorded_at ?? '').localeCompare(a.recorded_at ?? ''));
  return [...running.slice(0, 4), ...ended.slice(0, 5)];
}

export function HomePage() {
  const catalogue = useHomeEvaluationMap();
  const recent = recentBatches(catalogue.data);

  return (
    <PageTransition>
      <div className="space-y-3 px-2.5 pt-2 pb-4 sm:space-y-4 sm:px-4 sm:pt-3">
        <HomeEvaluationHeatmap catalogue={catalogue} actions={
          <nav aria-label="首页常用操作" className="grid w-full grid-cols-3 gap-1.5 sm:flex sm:w-auto">
            <Link to="/eval/execution"
              className="lrm-press inline-flex min-h-9 items-center justify-center gap-1.5 rounded-lg bg-[color-mix(in_srgb,var(--brand-primary),black_24%)] px-2.5 py-1.5 text-xs font-semibold text-white no-underline hover:bg-[color-mix(in_srgb,var(--brand-primary),black_18%)] sm:px-3 sm:text-sm">
              <Play className="size-3.5" aria-hidden /> 运行评测
            </Link>
            <Link to="/leaderboard" className={navigationLink}><Trophy className="size-3.5" aria-hidden /> 查看主榜</Link>
            <Link to="/eval/compare" className={navigationLink}><GitCompareArrows className="size-3.5" aria-hidden /> 自由对比</Link>
          </nav>
        } />

        <section aria-labelledby="recent-runs-title" className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)]">
          <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5 sm:px-4">
            <h2 id="recent-runs-title" className="m-0 text-sm font-bold">最近运行</h2>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              <Link to="/eval/compare?preset=qwen38-3track-ck133"
                className="inline-flex items-center gap-1 text-xs font-semibold text-[var(--brand-primary)] no-underline hover:underline">
                <ShieldCheck className="size-3.5" aria-hidden /> 三线提分与防退化护栏
              </Link>
              <Link to="/eval/reports"
                className="inline-flex items-center gap-1 text-xs font-semibold text-[var(--brand-primary)] no-underline hover:underline">
                全部评测报告 <ArrowRight className="size-3.5" aria-hidden />
              </Link>
            </div>
          </div>
          {recent.length > 0 ? (
            <ul className="m-0 list-none divide-y divide-[var(--border-subtle)] border-t border-[var(--border-subtle)] p-0">
              {recent.map((batch) => {
                const running = inProgress.has(batch.batch_status ?? '');
                return (
                  <li key={batch.batch_id}>
                    <Link to={`/eval/batches/${encodeURIComponent(batch.batch_id)}`}
                      className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-xs text-[var(--text-primary)] no-underline hover:bg-[var(--bg-elevated)] sm:px-4">
                      <span className={`shrink-0 rounded px-1.5 py-0.5 font-semibold ${running
                        ? 'bg-[var(--sem-running-bg)] text-[var(--sem-running-fg)]' : 'bg-[var(--bg-elevated)] text-[var(--text-secondary)]'}`}>
                        {statusLabels[batch.batch_status ?? ''] ?? batch.batch_status}
                      </span>
                      <span className="min-w-0 flex-1 truncate font-semibold">{batch.batch_name}</span>
                      <span className="shrink-0 tabular-nums text-[var(--text-secondary)]">
                        {running
                          ? `已记录 ${batch.recorded ?? 0}${batch.planned != null ? ` / ${batch.planned}` : ''}`
                          : batch.recorded_at ? fmtDateShort(batch.recorded_at) : '结束时间未登记'}
                      </span>
                      <ArrowRight className="size-3.5 shrink-0 text-[var(--text-secondary)]" aria-hidden />
                    </Link>
                  </li>
                );
              })}
            </ul>
          ) : <p className="mt-0 mb-3 px-3 text-xs text-[var(--text-secondary)] sm:px-4">
            {catalogue.isPending ? '正在加载…' : catalogue.isError ? '运行记录暂不可用。' : '暂无可见运行。'}
          </p>}
        </section>
      </div>
    </PageTransition>
  );
}
