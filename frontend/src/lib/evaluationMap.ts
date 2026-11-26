import { fmtDeltaPP } from './format';

export type MapAxis = 'dataset' | 'model' | 'harness';

export const mapAxes = [
  { id: 'dataset', label: '数据集' },
  { id: 'model', label: '模型' },
  { id: 'harness', label: 'Harness' },
] as const;

interface MapRelease {
  dataset_release_id: string;
  registered_task_count: number | null;
}

type TimeSource = 'batch_finished_at' | 'latest_trial_finished_at' | null;

interface BatchRef {
  batch_id: string;
  generation_id: string;
  batch_name: string;
  recorded_at: string | null;
  recorded_at_source?: TimeSource;
}

export interface MapBatch extends BatchRef {
  batch_status: string | null;
  run_scope: string;
  score: number | null;
  score_kind: 'trial_valid_mean' | 'official_summary' | 'unscored';
  scored: number | null;
  recorded: number | null;
  planned: number | null;
}

export interface MapCell {
  id: string;
  kind: 'trials' | 'historical_summary';
  benchmark_slug: string | null;
  dataset_release_id: string | null;
  dataset_version: string | null;
  split: string | null;
  display_name: string | null;
  registered_task_count: number | null;
  model_key: string | null;
  model_canonical: string | null;
  model_route: string | null;
  checkpoint: string | null;
  thinking_mode: string | null;
  harness_key: string | null;
  harness: string | null;
  harness_version: string | null;
  runner: string | null;
  scoring_protocol: string | null;
  metric_name: string | null;
  missing_identity: string[];
  published_run_count: number;
  scored_run_count: number;
  has_scored_history_peer?: boolean;
  published_run_counts_by_scope?: Record<string, number>;
  attempts_per_task?: number | null;
  timeout_seconds?: number | null;
  running_count: number;
  latest_batch: MapBatch | null;
  latest_scored_batch: MapBatch | null;
  reviewed_batch?: Pick<BatchRef, 'batch_id' | 'generation_id'>;
  historical_scores: Array<{ model_col: string; score: number | null; denominator: number | null }>;
}

export interface MapGroup {
  id: string;
  label: string;
  dataset_release_ids: string[];
  registered_task_count: number | null;
  published_run_count: number;
  cell_ids: string[];
}

export interface EvaluationMap {
  cells: MapCell[];
  groups: Record<MapAxis, MapGroup[]>;
  releases: MapRelease[];
}

export interface HistoryItem {
  id: string;
  reason: string | null;
  candidate: BatchRef | null;
  baseline: BatchRef | null;
  dataset_release_id: string | null;
  split: string | null;
  scoring_protocol: string | null;
  metric_name: string | null;
  common_valid: number | null;
  common_covered: number | null;
  baseline_score: number | null;
  candidate_score: number | null;
  delta_pp: number | null;
  baseline_kind?: 'zero' | 'zero_reference';
  zero_reference_reason?: string;
  reference_label?: string;
  reference_is_partial?: boolean;
}

export function zeroBaselineHistory(cell: MapCell, history?: HistoryItem): HistoryItem | undefined {
  const batch = cell.latest_scored_batch;
  if (cell.kind !== 'trials' || !batch || batch.score == null || !Number.isFinite(batch.score)) return;
  const firstScore = cell.scored_run_count === 1 && !cell.missing_identity.length && !cell.reviewed_batch
    && cell.has_scored_history_peer === false;
  if (history) {
    if (history.reason === null || history.reason === 'BATCH_UNAVAILABLE' || history.reason === 'RELEASE_GENERATION_MISMATCH'
        || history.candidate?.batch_id !== batch.batch_id
        || history.candidate.generation_id !== batch.generation_id) return;
  } else if (!firstScore) return;
  return {
    id: cell.id, reason: null, candidate: batch, baseline: null,
    baseline_kind: firstScore ? 'zero' : 'zero_reference',
    zero_reference_reason: !firstScore ? history?.reason ?? undefined : undefined,
    dataset_release_id: cell.dataset_release_id, split: cell.split, scoring_protocol: cell.scoring_protocol,
    metric_name: cell.metric_name, common_valid: null, common_covered: null,
    baseline_score: 0, candidate_score: batch.score, delta_pp: batch.score,
  };
}

export function referenceBadge(result: HistoryItem | undefined) {
  if (result?.baseline_kind === 'zero') return '首评基线';
  if (result?.baseline_kind === 'zero_reference') return '0 分基线';
  if (!result?.reference_label) return null;
  return result.reference_is_partial ? '运行快照' : '批次对照';
}

export interface Sector extends MapGroup {
  cells: MapCell[];
}

export function findMapLock(data: EvaluationMap, axis: MapAxis, id: string): Pick<MapGroup, 'id' | 'label' | 'cell_ids'> | undefined {
  if (!id) return undefined;
  const group = data.groups[axis].find((candidate) => candidate.id === id);
  if (group || axis === 'dataset') return group;

  // Earlier homepage links used the exact model or Harness configuration keys.
  const key = axis === 'model' ? 'model_key' : 'harness_key';
  const members = data.cells.filter((cell) => cell[key] === id);
  const first = members[0];
  if (!first) return undefined;
  const label = axis === 'model'
    ? [modelName(first), first.checkpoint, first.thinking_mode].filter(Boolean).join(' · ')
    : harnessName(first);
  return { id, label, cell_ids: members.map((cell) => cell.id) };
}

export function visibleSectors(data: EvaluationMap, axis: MapAxis, locks: Record<MapAxis, string>) {
  const cells = new Map(data.cells.map((cell) => [cell.id, cell]));
  const lockedCells = mapAxes.map(({ id }) => {
    const lock = findMapLock(data, id, locks[id]);
    return lock ? new Set(lock.cell_ids) : null;
  }).filter((ids) => ids !== null);
  const releaseSizes = new Map(data.releases.map((release) => [release.dataset_release_id, release.registered_task_count]));
  return data.groups[axis].flatMap((group): Sector[] => {
    const members = (group.cell_ids.map((id) => cells.get(id)).filter((cell) => cell !== undefined))
      .filter((cell) => lockedCells.every((ids) => ids.has(cell.id)));
    if (!members.length) return [];
    if (!lockedCells.length) return [{ ...group, cells: members }];
    const releaseIds = [...new Set(members.map((cell) => cell.dataset_release_id).filter((id) => id !== null))];
    const sizes = releaseIds.map((id) => releaseSizes.get(id)).filter((size) => size != null);
    return [{
      ...group,
      cells: members,
      dataset_release_ids: releaseIds,
      registered_task_count: sizes.length ? sizes.reduce((sum, size) => sum + size, 0) : null,
      published_run_count: members.reduce((sum, cell) => sum + cell.published_run_count, 0),
    }];
  });
}

export function datasetName(cell: MapCell) {
  return cell.display_name || cell.benchmark_slug || '数据集未登记';
}

export function modelName(cell: MapCell) {
  return cell.model_canonical || cell.model_route || '模型未登记';
}

export function harnessName(cell: MapCell) {
  return cell.harness
    ? [cell.harness, cell.harness_version].filter(Boolean).join(' @ ')
    : 'Harness 未登记';
}

export function cellTitle(cell: MapCell, axis: MapAxis) {
  if (axis === 'model') return [harnessName(cell), datasetName(cell)];
  if (axis === 'harness') return [modelName(cell), datasetName(cell)];
  return [modelName(cell), harnessName(cell)];
}

export function cellSearchText(cell: MapCell) {
  return [
    datasetName(cell), cell.dataset_release_id, cell.dataset_version, cell.split, cell.benchmark_slug,
    modelName(cell), cell.model_route, cell.checkpoint, cell.thinking_mode,
    harnessName(cell), cell.runner, cell.scoring_protocol, cell.metric_name,
    cell.latest_batch?.batch_name, cell.latest_batch?.batch_id,
  ].join(' ').toLowerCase();
}

const reasons: Record<string, string> = {
  NO_PREVIOUS_RUN: '没有前次可比记录',
  MISSING_RECORDED_TIME: '缺少可核实的结束时间',
  NO_COMMON_VALID_TASKS: '没有共同有效题',
  INCOMPARABLE_PROTOCOL: '评分协议或指标不同',
  INCOMPARABLE_RELEASE: '数据集发行版或 Split 不同',
  INCOMPARABLE_CONFIGURATION: '无法核实运行配置一致',
  MISSING_IDENTITY: '评测组合身份未登记',
  MISSING_VALID_SCORE: '缺少有效评分',
  BATCH_UNAVAILABLE: '批次不可用',
  RELEASE_GENERATION_MISMATCH: '批次发布代已更新',
};

export function historyReason(cell: MapCell, result: HistoryItem | undefined) {
  const reason = result?.reason ?? result?.zero_reference_reason;
  if (reason) return reasons[reason] ?? '缺少可比记录';
  if (cell.kind === 'historical_summary') return '历史汇总缺少可比前次';
  if (cell.missing_identity.length) return '评测组合身份未登记';
  if (cell.running_count && !cell.published_run_count) return '运行中，尚未发布';
  return '缺少有效评分';
}

export function changeState(result: HistoryItem | undefined, pending = false, failed = false) {
  const delta = result?.reason == null ? result?.delta_pp : null;
  if (delta == null || !Number.isFinite(delta)) {
    return { tone: 'unknown', symbol: pending ? '…' : '—',
      label: pending ? '等待计算' : failed ? '加载失败' : '尚无可比前次', value: null };
  }
  if (delta === 0) return { tone: 'flat', symbol: '=', label: '持平', value: fmtDeltaPP(delta) };
  return delta > 0
    ? { tone: 'up', symbol: '↑', label: '上升', value: fmtDeltaPP(delta) }
    : { tone: 'down', symbol: '↓', label: '回落', value: fmtDeltaPP(delta) };
}

export function historicalCompareHref(result: HistoryItem) {
  if (result.reason || result.delta_pp == null || !result.baseline || !result.candidate
    || !result.dataset_release_id || !result.scoring_protocol) return null;
  const query = new URLSearchParams({
    baseline_generation: result.baseline.generation_id,
    candidate_generation: result.candidate.generation_id,
    scoring_protocol: result.scoring_protocol,
    focus_dataset: result.dataset_release_id,
    diff_filter: 'all',
  });
  return `/compare/1v1/${encodeURIComponent(result.baseline.batch_id)}/vs/${encodeURIComponent(result.candidate.batch_id)}?${query}`;
}

export function groupedCompareHref(cell: MapCell) {
  if (!cell.benchmark_slug || !cell.dataset_release_id || !cell.scoring_protocol) return null;
  const query = new URLSearchParams({
    tab: 'groups', group_release: cell.dataset_release_id, group_protocol: cell.scoring_protocol,
  });
  const batch = cell.latest_scored_batch ?? cell.latest_batch;
  if (cell.model_key && cell.harness_key && cell.kind === 'trials') {
    query.set('group_dimension', 'model');
    query.set('group_anchor', cell.model_key);
  }
  if (batch) {
    query.set('group_focus_batch', batch.batch_id);
    query.set('group_focus_generation', batch.generation_id);
  }
  return `/benchmarks/${encodeURIComponent(cell.benchmark_slug)}?${query}`;
}

export interface Rectangle {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface Placed<T> extends Rectangle {
  item: T;
}

// Partition the longer side so every rectangle keeps its share of the original area.
export function layoutTreemap<T>(items: readonly T[], weight: (item: T) => number, bounds: Rectangle): Placed<T>[] {
  const sorted = items.map((item) => ({ item, count: weight(item) }))
    .filter(({ count }) => Number.isFinite(count) && count > 0)
    .sort((a, b) => b.count - a.count);
  if (!sorted.length || bounds.width <= 0 || bounds.height <= 0) return [];
  const placed: Placed<T>[] = [];

  function divide(start: number, end: number, total: number, rect: Rectangle) {
    if (start + 1 === end) {
      placed.push({ item: sorted[start].item, ...rect });
      return;
    }
    let middle = start + 1;
    let before = sorted[start].count;
    while (middle < end - 1 && before + sorted[middle].count / 2 < total / 2) {
      before += sorted[middle].count;
      middle += 1;
    }
    const ratio = before / total;
    if (rect.width >= rect.height) {
      const width = rect.width * ratio;
      divide(start, middle, before, { ...rect, width });
      divide(middle, end, total - before, { ...rect, x: rect.x + width, width: rect.width - width });
    } else {
      const height = rect.height * ratio;
      divide(start, middle, before, { ...rect, height });
      divide(middle, end, total - before, { ...rect, y: rect.y + height, height: rect.height - height });
    }
  }

  divide(0, sorted.length, sorted.reduce((sum, entry) => sum + entry.count, 0), bounds);
  return placed;
}
