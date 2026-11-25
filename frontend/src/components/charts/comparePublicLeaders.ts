import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '../../lib/api';
import { useAppStore } from '../../lib/store';
import type { CompareQueryResponse } from '../../types/api';

interface PublicScore {
  id: string;
  model: string | null;
  harness: string;
  configuration: string | null;
  score_pct: number;
  task_count: number;
  sample_count: number;
  published_at: string | null;
}

interface PublicSource {
  release_id: string;
  legacy_release_id: string | null;
  legacy_metric: string | null;
  benchmark_slug: string;
  label: string;
  url: string;
  dataset_version: string;
  split: string;
  metric: string;
  protocol: string;
  task_count: number;
  status: 'pending' | 'ready' | 'stale' | 'unavailable';
  synced_at: string | null;
  published_at: string | null;
  rows: PublicScore[];
}

interface ComparedBatch {
  batch_id: string;
  generation_id?: string;
  local_score_pct?: number | null;
  delta_pp: number | null;
  reasons: string[];
}

type CompareRow = CompareQueryResponse['rows'][number];
export type CompareSubject = { id: string; alias: string };
type Snapshot = CompareQueryResponse['bound_snapshot'];

export type PublicPoint = { source: PublicSource; leader: PublicScore; legacy: boolean };
type Overlay = {
  point?: PublicPoint;
  missing?: string;
  deltas: Array<{ subject: string; delta: number }>;
  reasons: string[];
};

export function leaderName(leader: PublicScore) {
  return [leader.model, leader.harness, leader.configuration].filter(Boolean).join(' · ');
}

export function useComparePublicLeaders(rows: CompareRow[], subjects: CompareSubject[], snapshot: Snapshot) {
  const userId = useAppStore((state) => state.user?.user_id);
  const catalog = useQuery({
    queryKey: ['public-baselines', userId],
    queryFn: ({ signal }) => apiFetch<{ sources: PublicSource[] }>('/api/v1/public-baselines', { signal }),
    staleTime: 60_000,
    refetchInterval: 60_000,
    enabled: rows.length > 0,
  });

  const candidates = rows.map((row): Omit<Overlay, 'deltas' | 'reasons'> => {
    if (!catalog.data) return { missing: catalog.isError ? '公开榜单加载失败' : '正在加载公开榜单' };
    const legacy = row.scoring_protocol === 'legacy-20261120-csv';
    const forBenchmark = catalog.data.sources.filter((source) => source.benchmark_slug === row.benchmark_slug);
    const source = forBenchmark.find((source) => legacy
      ? source.legacy_release_id === row.dataset_release_id && source.legacy_metric === row.legacy_metric
      : source.release_id === row.dataset_release_id);
    if (!source) return { missing: forBenchmark.length ? '没有版本、分片与指标可匹配的公开榜' : '暂无已接入的公开榜' };
    const leader = source.rows[0];
    if (!leader) return { missing: source.status === 'pending' ? '公开榜等待首次同步' : '暂无已核验的公开成绩' };
    return { point: { source, leader, legacy } };
  });

  const requests = candidates.flatMap(({ point }, index) => {
    if (!point || point.legacy || snapshot.sample_set_id || rows[index].scoring_protocol !== point.source.protocol
      || rows[index].intersection.common_valid_scored_tasks !== point.source.task_count) return [];
    const batches = snapshot.batches.filter((bound) => rows[index].cells[bound.subject_id]?.common_valid.score != null);
    return batches.length ? [{ index, leaderId: point.leader.id, syncedAt: point.source.synced_at, batches }] : [];
  });

  const comparisons = useQuery({
    queryKey: ['compare-chart-public-leader', userId, requests],
    enabled: requests.length > 0,
    retry: false,
    queryFn: async ({ signal }) => new Map(await Promise.all(requests.map(async (item) => {
      const params = new URLSearchParams({ baseline_id: item.leaderId });
      for (const id of new Set(item.batches.map((batch) => batch.batch_id))) params.append('batch_id', id);
      const page = await apiFetch<{ synced_at: string; items: ComparedBatch[] }>(
        `/api/v1/public-baselines/compare?${params}`, { signal },
      );
      return [item.index, page.synced_at === item.syncedAt
        ? new Map(page.items.map((batch) => [batch.batch_id, batch]))
        : null] as const;
    }))),
  });

  return candidates.map((candidate, index): Overlay => {
    const { point } = candidate;
    const deltas: Overlay['deltas'] = [];
    const reasons = new Set<string>();
    if (point?.legacy) reasons.add('历史总表缺少可核验的逐题评分与计分分母');
    else if (point && snapshot.sample_set_id) reasons.add('对比使用自定义样本集');
    else if (point && rows[index].scoring_protocol !== point.source.protocol) reasons.add('站内与公开榜的评分协议不同');
    else if (point && rows[index].intersection.common_valid_scored_tasks !== point.source.task_count) {
      reasons.add(`共同有效题为 ${rows[index].intersection.common_valid_scored_tasks ?? '未知'} 题，公榜分差需完整 ${point.source.task_count} 题`);
    } else if (point) {
      const checked = comparisons.data?.get(index);
      if (!checked) reasons.add(comparisons.isError ? '暂时无法核对分差' : '等待口径核对');
      else {
        for (const bound of snapshot.batches) {
          const cell = rows[index].cells[bound.subject_id]?.common_valid;
          if (cell?.score == null) continue;
          const comparison = checked.get(bound.batch_id);
          const alias = subjects.find(({ id }) => id === bound.subject_id)?.alias || bound.subject_id;
          if (comparison?.delta_pp == null) {
            reasons.add(`${alias} · ${comparison?.reasons.join('；') || '缺少分差依据'}`);
          } else if (comparison.generation_id !== bound.generation_id || comparison.local_score_pct == null
            || Math.abs(comparison.local_score_pct - cell.score) > 0.00501) {
            reasons.add(`${alias} · 发布代或图中分数与公榜校验结果不一致`);
          } else deltas.push({ subject: alias, delta: comparison.delta_pp });
        }
      }
    }
    return { ...candidate, deltas, reasons: [...reasons] };
  });
}
