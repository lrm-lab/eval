import type { DatasetPerformanceRow } from '../components/ui/DatasetPerformancePanel';
import type { GroupedComparison } from '../components/ui/GroupedComparePanel';

export type EvaluationAxis = 'dataset' | 'model' | 'harness';
export type ResultRow = DatasetPerformanceRow;

export interface EvaluationChoice {
  key: string;
  label: string;
  search?: string;
  count?: number;
  track?: string;
}

export type EvaluationContext = GroupedComparison['contexts'][number];

export interface PreviewPair {
  id: string;
  baseline: ResultRow;
  candidate: ResultRow;
  note?: string;
  reviewed?: boolean;
}

export interface PreviewScore {
  id: string;
  reason: string | null;
  common_valid?: number;
  common_covered?: number;
  baseline_score?: number | null;
  candidate_score?: number | null;
  delta_pp?: number | null;
  metric_name?: string | null;
}

export const evaluationAxes = [
  { id: 'dataset', label: '数据集', resource: 'benchmarks', preferred: 'deepswe' },
  { id: 'model', label: '模型', resource: 'models', preferred: 'Qwen3_8-27B' },
  { id: 'harness', label: 'Harness', resource: 'harnesses', preferred: 'codex' },
] as const;

export const scoreSources = {
  trial_valid_mean: '有效均分',
  official_summary: '官方汇总',
  unscored: '未计分',
};

export function modelName(row: ResultRow) {
  return [row.model_canonical || '模型未确认', row.checkpoint, row.thinking_mode].filter(Boolean).join(' · ');
}

export function harnessName(row: ResultRow) {
  return row.harness_key && row.harness
    ? [row.harness, row.harness_version].filter(Boolean).join(' @ ') : 'Harness 未确认';
}

export function dimensionChoice(row: ResultRow, axis: EvaluationAxis): EvaluationChoice | null {
  if (axis === 'dataset') {
    return row.benchmark_slug ? { key: row.benchmark_slug, label: row.display_name } : null;
  }
  if (axis === 'model') {
    return row.model_key ? { key: row.model_key, label: modelName(row) } : null;
  }
  return row.harness_key ? { key: row.harness_key, label: harnessName(row) } : null;
}

export function choicesFromRows(rows: ResultRow[], axis: EvaluationAxis) {
  const unique = new Map<string, EvaluationChoice>();
  for (const row of rows) {
    const choice = dimensionChoice(row, axis);
    if (choice) unique.set(choice.key, choice);
  }
  return [...unique.values()].sort((a, b) => a.label.localeCompare(b.label));
}

export function availableInContext(row: ResultRow, context: EvaluationContext) {
  return context.dataset_release_id === row.dataset_release_id && context.scoring_protocol === row.scoring_protocol;
}

function contextKey(row: ResultRow) {
  return JSON.stringify([row.dataset_release_id, row.split, row.scoring_protocol, row.metric_name]);
}

function identityKey(row: ResultRow) {
  return JSON.stringify([row.model_key, row.model_route, row.harness_key]);
}

function modelKey(row: ResultRow) {
  return JSON.stringify([row.model_key, row.model_route]);
}

function hasTrialScore(row: ResultRow) {
  return row.score != null && row.scored != null && row.scored > 0 && row.score_kind === 'trial_valid_mean'
    && Boolean(row.dataset_release_id && row.scoring_protocol && row.metric_name)
    // Coverage is null when the current account cannot inspect this split's trial evidence.
    && row.observed_unique_tasks != null;
}

export function inChartOrder(rows: ResultRow[], axis: EvaluationAxis) {
  const representatives = new Map<string, ResultRow>();
  const ordered = [...rows].sort((a, b) => Number(b.score != null) - Number(a.score != null));
  for (const row of ordered) {
    const key = axis === 'dataset' ? row.harness ?? identityKey(row) : row.benchmark_slug ?? row.display_name;
    const previous = representatives.get(key);
    if (!previous || (row.score != null && previous.score != null
      && (row.observed_unique_tasks ?? -1) > (previous.observed_unique_tasks ?? -1))) {
      representatives.set(key, row);
    }
  }
  const first = [...representatives.values()];
  const included = new Set(first);
  return [...first, ...ordered.filter((row) => !included.has(row))];
}

function makePair(baseline: ResultRow, candidate: ResultRow, note?: string): PreviewPair {
  const id = [baseline.batch_id, baseline.generation_id, candidate.batch_id, candidate.generation_id].join(':');
  return { id, baseline, candidate, note };
}

// Verified reference pairs use recorded configuration labels and still pass through the
// same permission, generation, context and common-task checks as every automatic pair.
interface ReviewedPair {
  before: readonly [batchId: string, generation: string];
  after: readonly [batchId: string, generation: string];
  note?: string;
}

const reviewedChanges: ReviewedPair[] = [
  { before: ['b_f06f30c356c973d75426', 'gen_legacy_0ce76e7872def6cd'],
    after: ['b_da4ba4ef9c476d91b1f2', 'gen_legacy_08510fbff2dc2549'], note: 'Chat → Responses' },
  { before: ['b_063baf00fc22e9fa6a8a', 'gen_legacy_3163bc26df982d64'],
    after: ['b_344aefc389bad47e526f', 'gen_legacy_ce3c238db6d11fbf'] },
  { before: ['b_75d302a7caed6cce5567', 'gen_legacy_75241f45b4a68d7a'],
    after: ['b_ac357fc458e58bca38ed', 'gen_legacy_60b7331a6b5575a5'] },
];
const terminalHarnessPair: ReviewedPair = {
  before: ['b_883f7272b46bd87ff0b5', 'gen_legacy_520afe9bbffea1f9'],
  after: ['b_b2056e7535b6c91d64fc', 'gen_legacy_0d8e53cc96510188'],
};

function reviewedPair(rows: ResultRow[], pair: ReviewedPair) {
  const find = ([id, generation]: ReviewedPair['before']) =>
    rows.find((row) => row.batch_id === id && row.generation_id === generation);
  const baseline = find(pair.before);
  const candidate = find(pair.after);
  return baseline && candidate && hasTrialScore(baseline) && hasTrialScore(candidate)
    && modelKey(baseline) === modelKey(candidate)
    && contextKey(baseline) === contextKey(candidate)
    ? { ...makePair(baseline, candidate, pair.note), reviewed: true } : undefined;
}

export function harnessPairs(rows: ResultRow[], selectedHarness: string | null, reference?: ResultRow) {
  const reviewed = reviewedPair(rows, terminalHarnessPair);
  if (reviewed && (!selectedHarness || reviewed.baseline.harness === selectedHarness || reviewed.candidate.harness === selectedHarness)) {
    return [reviewed];
  }
  const eligible = rows.filter((row) => row.harness_key && row.model_key && hasTrialScore(row));
  const baselines = selectedHarness ? eligible.filter((row) => row.harness === selectedHarness) : eligible;
  const nearReference = reference ? baselines.filter((row) => contextKey(row) === contextKey(reference)) : [];
  for (const baseline of [...nearReference, ...baselines]) {
    const seen = new Set([baseline.harness]);
    const matching = eligible.filter((row) => {
      if (seen.has(row.harness) || modelKey(row) !== modelKey(baseline)
        || contextKey(row) !== contextKey(baseline)) return false;
      seen.add(row.harness);
      return true;
    });
    if (matching.length) return matching.slice(0, 2).map((row) => makePair(baseline, row));
  }
  return [];
}

export function harnessGapReason(rows: ResultRow[], reference?: ResultRow) {
  if (!reference?.model_key || !reference.harness) return '缺少已确认的模型和 Harness 批次。';
  const peers = rows.filter((row) => row.harness && row.harness !== reference.harness && modelKey(row) === modelKey(reference));
  if (!peers.length) return '当前数据集缺少相同模型与路由的其他 Harness 批次。';
  const sameRelease = peers.filter((row) => row.dataset_release_id === reference.dataset_release_id && row.split === reference.split);
  if (!sameRelease.length) return '其他 Harness 使用的数据集发行版或 Split 不同。';
  const sameProtocol = sameRelease.filter((row) => row.scoring_protocol === reference.scoring_protocol);
  if (!sameProtocol.length) return '同发行版的 Harness 批次使用不同评分协议。';
  if (!sameProtocol.some((row) => row.metric_name === reference.metric_name)) return '同发行版的 Harness 批次记录了不同指标。';
  return '当前对照缺少可读取的有效逐题评分。';
}

export interface ChangeCell {
  pair?: PreviewPair;
  reason?: string;
}

function changeCell(rows: ResultRow[]): ChangeCell {
  if (!rows.length) return { reason: '暂无可见批次' };
  if (rows.length < 2) return { reason: '缺少第二个批次' };
  if (rows.filter(hasTrialScore).length < 2) {
    return { reason: rows.some((row) => row.observed_unique_tasks == null && row.split === 'holdout')
      ? '逐题证据受限' : '缺少有效逐题评分' };
  }
  const eligible = rows.filter(hasTrialScore);
  for (const pair of reviewedChanges) {
    const reviewed = reviewedPair(eligible, pair);
    if (reviewed) return { pair: reviewed };
  }

  const byContext = new Map<string, ResultRow[]>();
  const time = (row: ResultRow) => Date.parse(row.finished_at || row.indexed_at || '');
  for (const row of eligible) {
    const key = contextKey(row);
    const bucket = byContext.get(key) ?? [];
    bucket.push(row);
    byContext.set(key, bucket);
  }
  const choices = [...byContext.values()].filter((bucket) => bucket.length >= 2)
    .map((bucket) => bucket.filter((row) => Number.isFinite(time(row))).sort((a, b) => time(a) - time(b)))
    .filter((bucket) => bucket.length >= 2 && time(bucket[0]) < time(bucket[bucket.length - 1]))
    .sort((a, b) => time(b[b.length - 1]) - time(a[a.length - 1]));
  if (choices.length > 0) {
    const sequence = choices[0];
    const latest = sequence[sequence.length - 1];
    const previous = [...sequence].reverse().find((row) => time(row) < time(latest));
    if (previous) return { pair: makePair(previous, latest, previous.finished_at && latest.finished_at ? '两次完成记录' : '按入库记录') };
  }
  if ([...byContext.values()].some((bucket) => bucket.length > 1)) return { reason: '缺少可核对的基线顺序' };
  if (new Set(eligible.map((row) => row.dataset_release_id)).size > 1) return { reason: '题库发行版或 Split 不同' };
  if (new Set(eligible.map((row) => row.scoring_protocol)).size > 1) return { reason: '评分协议不同' };
  return { reason: '评分指标不同' };
}

export function changeMatrix(rows: ResultRow[], selectedDataset: string | null) {
  const combinations = new Map<string, { row: ResultRow; datasets: Map<string, ResultRow[]> }>();
  const names = new Map<string, string>();
  for (const row of rows) {
    if (!row.model_key || !row.harness_key || !row.benchmark_slug) continue;
    names.set(row.benchmark_slug, row.display_name);
    const key = identityKey(row);
    const group = combinations.get(key) ?? { row, datasets: new Map() };
    const bucket = group.datasets.get(row.benchmark_slug) ?? [];
    bucket.push(row);
    group.datasets.set(row.benchmark_slug, bucket);
    combinations.set(key, group);
  }
  const counts = new Map<string, number>();
  const all = [...combinations.entries()].map(([key, { row, datasets }]) => {
    const cells = new Map([...datasets].map(([slug, batchRows]) => [slug, changeCell(batchRows)]));
    const comparable = [...cells].filter(([, cell]) => cell.pair);
    for (const [slug] of comparable) counts.set(slug, (counts.get(slug) ?? 0) + 1);
    const focusReviewed = selectedDataset && cells.get(selectedDataset)?.pair?.reviewed;
    const reviewedCount = comparable.filter(([, cell]) => cell.pair?.reviewed).length;
    return { key, row, cells, count: comparable.length, focusReviewed: Boolean(focusReviewed), reviewedCount };
  });
  const columns = [...names]
    .sort(([a], [b]) => Number(b === selectedDataset) - Number(a === selectedDataset)
      || (counts.get(b) ?? 0) - (counts.get(a) ?? 0))
    .slice(0, 4);
  const ranked = all.sort((a, b) => Number(b.focusReviewed) - Number(a.focusReviewed) || b.reviewedCount - a.reviewedCount || b.count - a.count
    || (a.row.harness || '').localeCompare(b.row.harness || ''));
  const firstByHarness = new Map<string, (typeof ranked)[number]>();
  for (const item of ranked) {
    const harness = item.row.harness?.toLowerCase();
    if (harness && item.count > 0 && !firstByHarness.has(harness)) firstByHarness.set(harness, item);
  }
  const firstItems = [...firstByHarness.values()];
  const firstKeys = new Set(firstItems.map((item) => item.key));
  const items = [...firstItems, ...ranked.filter((item) => !firstKeys.has(item.key))].slice(0, 4);
  return { columns, items };
}

export function previewBody(pair: PreviewPair) {
  const pinned = (row: ResultRow) => ({ batch_id: row.batch_id, generation_id: row.generation_id });
  return {
    id: pair.id, baseline: pinned(pair.baseline), candidate: pinned(pair.candidate),
    dataset_release_id: pair.baseline.dataset_release_id, scoring_protocol: pair.baseline.scoring_protocol,
  };
}

export function previewReason(code?: string | null) {
  if (code === 'GENERATION_MISMATCH') return '批次发布代已变化';
  if (code === 'INCOMPARABLE_PROTOCOL') return '评分协议不同';
  if (code === 'INCOMPARABLE_RELEASE') return '题库发行版或 Split 不同';
  if (code === 'NO_COMMON_VALID_TASKS') return '没有共同有效题';
  if (code === 'PROJECT_FORBIDDEN' || code === 'SCOPE_FORBIDDEN') return '无权查看';
  return '当前缺少可比结果';
}
