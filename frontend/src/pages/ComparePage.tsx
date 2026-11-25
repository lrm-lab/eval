import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import type * as echarts from 'echarts';
import {
  Bookmark,
  Check,
  ChevronDown,
  Crown,
  Download,
  GitCompareArrows,
  Lock,
  Play,
  Plus,
  Share2,
  ShieldCheck,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { ApiError, apiDownload, apiFetch } from '../lib/api';
import { fmtDeltaPP, fmtDuration, fmtPct, stableColorForKey } from '../lib/format';
import { useAppStore } from '../lib/store';
import type { CompareQueryResponse, CompareSubjectRef, TrialDetailDTO } from '../types/api';
import { PageTransition, SlidingTabs } from '../components/motion/primitives';
import { CompositeScoreCell } from '../components/ui/CompositeScoreCell';
import { GainGuardrailPill } from '../components/ui/GainGuardrailPill';
import { MethodologyPopover } from '../components/ui/MethodologyPopover';
import { RowActionIcon } from '../components/ui/RowActionIcon';
import { SlideOverDrawer } from '../components/ui/SlideOverDrawer';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';
import { EChartBox, glassTooltipConfig } from '../components/charts/EChartBox';

interface CompareOptionsResp {
  writable_projects?: string[];
  batch_subjects: Array<{
    batch_id: string;
    batch_name: string;
    benchmark_slug: string | null;
    model_canonical: string | null;
    harness: string | null;
    scoring_protocol: string;
    scored: number;
    planned: number | null;
    recorded: number;
  }>;
  legacy_model_subjects: Array<{
    model_col: string;
    model_canonical: string;
    bench_count: number;
  }>;
  presets: Array<{
    preset_id: string;
    title: string;
    common_note: string;
    payload: {
      subjects: CompareSubjectRef[];
      baseline_subject_id: string;
      scoring_protocol?: string;
      regression_threshold_pp?: number;
      include_verdict?: boolean;
    };
  }>;
  saved_comparisons: Array<{
    short_id: string;
    name: string;
    mode: string;
    created_at: string;
  }>;
  sample_sets: Array<{
    set_id: string;
    sample_set_id?: string;
    name: string;
    dataset_release_id: string | null;
    task_ids: string[];
  }>;
}

const COLLAPSED_PRESET_COUNT = 6;
const PRESET_TRACK_LABELS: Record<string, string> = {
  'qwen38-3track-ck133': '跨赛道',
  'qwen38-text-ck133': 'LLM-Text',
  'qwen38-vision-ck133': 'Vision',
  'qwen35-4b-ck105': '模型检查点',
  'deepswe-2way': 'Agentic',
  'deepswe-3way': 'Agentic',
  'gaia-2way': 'Agentic',
  'tb21-6way': 'Agentic',
};

interface TaskDiffResp {
  comparison_id: string;
  dataset_release_id?: string;
  kind?: 'batch' | 'legacy_sample_diff';
  benchmark_dir?: string;
  model_file_a?: string;
  model_file_b?: string;
  per_sample_score_absent?: boolean;
  scoring_protocol: string;
  intersection: {
    common_covered_tasks: number;
    common_valid_scored_tasks: number;
  };
  win_tie_loss: { a_wins: number; both_pass: number; both_fail: number; b_wins: number; fault_excluded: number };
  checksum_mismatch_tasks?: number;
  version_drift_warning?: boolean;
  checksum_drift_groups?: Array<{
    task_id: string;
    task_checksum: string | null;
    subjects: Array<{
      subject_id: string;
      batch_id?: string;
      trial_id: string;
      status: string;
      reward: number | null;
      reward_valid: boolean;
      duration_sec: number | null;
      tool_calls: number | null;
    }>;
  }>;
  redacted?: boolean;
  hidden_holdout_tasks?: number;
  redacted_reason?: string | null;
  items: Array<{
    task_id: string;
    task_checksum?: string | null;
    task_checksum_match: boolean;
    diff_type: 'a_wins' | 'both_pass' | 'both_fail' | 'b_wins' | 'unscored_pair';
    a: { batch_id: string; trial_id: string; model_file?: string; status: string; reward: number | null; duration_sec: number | null; tool_calls: number | null };
    b: { batch_id: string; trial_id: string; model_file?: string; status: string; reward: number | null; duration_sec: number | null; tool_calls: number | null };
  }>;
  total: number;
}

interface SavedOpenResp {
  short_id: string;
  name: string;
  mode: string;
  config?: {
    payload?: {
      subjects?: CompareSubjectRef[];
      baseline_subject_id?: string;
      scoring_protocol?: string;
      regression_threshold_pp?: number;
      scope?: { sample_set_id?: string };
    };
    bound_batches?: Array<{ subject_id: string; alias: string; batch_id: string; generation_id: string }>;
  };
  result: CompareQueryResponse;
}

type DiffFilterKind = 'all' | 'divergent' | 'a_wins' | 'b_wins' | 'both_pass' | 'both_fail';

interface SplitPairState {
  kind: 'batch' | 'legacy';
  taskId: string;
  aBatch: string;
  aTrial: string;
  bBatch: string;
  bTrial: string;
  aModelFile?: string;
  bModelFile?: string;
}

function parseDiffFilter(raw: string | null): DiffFilterKind {
  if (raw === 'all' || raw === 'divergent' || raw === 'a_wins' || raw === 'b_wins' || raw === 'both_pass' || raw === 'both_fail') {
    return raw;
  }
  return 'divergent';
}

function CompareScoreCharts({
  comparisonId,
  rows,
  subjects,
}: {
  comparisonId: string;
  rows: CompareQueryResponse['rows'];
  subjects: Array<{ id: string; alias: string }>;
}) {
  const theme = useAppStore((s) => s.theme);
  const sectionRef = useRef<HTMLElement>(null);
  const [stacked, setStacked] = useState(false);

  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;
    const observer = new ResizeObserver(([entry]) => setStacked(entry.contentRect.width < 760));
    observer.observe(section);
    return () => observer.disconnect();
  }, []);

  const hasRadar = rows.length >= 3;
  const option = useMemo<echarts.EChartsOption>(() => {
    const textColor = theme === 'dark' ? '#a1a1aa' : '#475569';
    const gridColor = theme === 'dark' ? '#3f3f46' : '#e5e7eb';
    const radarCenter = stacked ? '50%' : '16.67%';
    const barTitleTop = stacked ? (hasRadar ? 284 : 88) : 6;
    const titleStyle = { color: textColor, fontSize: 13, fontWeight: 600 };
    const datasetNames = rows.map((row) => row.raw_dataset_name);
    const scores = subjects.map(({ id, alias }) => ({
      name: subjects.some((other) => other.id !== id && other.alias === alias) ? `${alias} (${id})` : alias,
      color: stableColorForKey(id),
      values: rows.map((row) => row.cells[id]?.common_valid?.score ?? null),
    }));

    return {
      title: [
        {
          text: '多维雷达图',
          subtext: hasRadar ? undefined : '至少需要 3 个共同可比基准',
          textStyle: titleStyle,
          subtextStyle: { color: textColor, fontSize: 11 },
          left: radarCenter,
          top: 6,
          textAlign: 'center',
        },
        {
          text: '分组柱状图',
          textStyle: titleStyle,
          left: stacked ? '50%' : '66.67%',
          top: barTitleTop,
          textAlign: 'center',
        },
      ],
      tooltip: { ...glassTooltipConfig(theme), trigger: 'axis' },
      legend: {
        type: 'scroll',
        data: scores.map(({ name }) => name),
        left: 4,
        right: 4,
        bottom: 32,
        textStyle: { color: textColor, fontSize: 11 },
        pageTextStyle: { color: textColor },
      },
      radar: hasRadar ? {
        center: [radarCenter, stacked ? 148 : 158],
        radius: stacked ? 78 : 86,
        indicator: datasetNames.map((name) => ({ name, max: 100 })),
        axisName: { color: textColor, fontSize: 10, width: 68, overflow: 'truncate' },
        axisNameGap: 6,
        axisLine: { lineStyle: { color: gridColor } },
        splitLine: { lineStyle: { color: gridColor } },
        splitArea: { show: false },
      } : undefined,
      grid: { top: barTitleTop + 35, right: 12, bottom: 90, left: stacked ? 42 : '38%' },
      xAxis: {
        type: 'category',
        data: datasetNames,
        axisLabel: {
          color: textColor,
          fontSize: 10,
          width: stacked ? 58 : 82,
          overflow: 'truncate',
          rotate: rows.length > 6 || stacked ? 30 : 0,
          hideOverlap: true,
        },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { color: textColor, fontSize: 11, formatter: '{value}%' },
        splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
      },
      series: [
        ...scores.map(({ name, color, values }): echarts.BarSeriesOption => ({
          name,
          type: 'bar',
          itemStyle: { color, borderRadius: [4, 4, 0, 0] },
          data: values,
        })),
        ...(hasRadar ? [{
          type: 'radar' as const,
          tooltip: { trigger: 'item' as const },
          symbolSize: 4,
          data: scores.map(({ name, color, values }) => ({
            name,
            value: values.map((value) => value ?? '-'),
            lineStyle: { color, width: 2 },
            itemStyle: { color },
          })),
        }] : []),
      ],
    };
  }, [hasRadar, rows, stacked, subjects, theme]);

  const height = stacked ? (hasRadar ? 600 : 390) : 340;
  return (
    <section
      ref={sectionRef}
      aria-labelledby="compare-scores-heading"
      className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4"
    >
      <h2 id="compare-scores-heading" className="mb-2 text-sm font-bold text-[var(--text-primary)]">共同可比基准得分可视化</h2>
      <EChartBox option={option} height={height} exportName={`compare-${comparisonId}`} />
    </section>
  );
}

export function ComparePage() {
  const { shortId, a: routeA, b: routeB } = useParams<{ shortId?: string; a?: string; b?: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();

  const user = useAppStore((s) => s.user);
  const compareBasket = useAppStore((s) => s.compareBasket);
  const basketBaselineId = useAppStore((s) => s.baselineId);
  const pushToast = useAppStore((s) => s.pushToast);

  const cmpQueryId = searchParams.get('cmp') || undefined;
  const openId = shortId || cmpQueryId;
  const focusBenchmarkParam = searchParams.get('focus_benchmark') || '';
  const focusDatasetParam = searchParams.get('focus_dataset') || '';

  const [modeKind, setModeKind] = useState<'batch' | 'legacy_model'>('batch');
  const [subjects, setSubjects] = useState<CompareSubjectRef[]>([]);
  const [baselineId, setBaselineId] = useState<string>('');
  const [scoringProtocol, setScoringProtocol] = useState<string>('');
  const [thresholdPp, setThresholdPp] = useState<number>(() => {
    const t = searchParams.get('regression_threshold_pp');
    return t !== null && !Number.isNaN(Number(t)) ? Number(t) : -1.0;
  });
  const [enableFaultsAsZero, setEnableFaultsAsZero] = useState<boolean>(true);
  const [sampleSetId, setSampleSetId] = useState<string>('');
  const [presetsExpanded, setPresetsExpanded] = useState(false);
  const [diffFilter, setDiffFilter] = useState<DiffFilterKind>(() => parseDiffFilter(searchParams.get('diff_filter')));
  const [diffSubjectA, setDiffSubjectA] = useState<string>(() => searchParams.get('diff_a') || '');
  const [diffSubjectB, setDiffSubjectB] = useState<string>(() => searchParams.get('diff_b') || '');
  const [diffDataset, setDiffDataset] = useState<string>(() => searchParams.get('diff_ds') || focusDatasetParam);
  const [diffBenchmark, setDiffBenchmark] = useState<string>(() => searchParams.get('diff_bm') || focusBenchmarkParam);

  const [result, setResult] = useState<CompareQueryResponse | null>(null);
  const [taskDiff, setTaskDiff] = useState<TaskDiffResp | null>(null);
  const [taskDiffRestrictedMsg, setTaskDiffRestrictedMsg] = useState<string | null>(null);
  const [saveName, setSaveName] = useState('');
  const [saveMode, setSaveMode] = useState<'snapshot' | 'live'>('snapshot');
  const [saveProjectId, setSaveProjectId] = useState<string>('');

  const taskDiffSectionRef = useRef<HTMLDivElement | null>(null);
  const activeCmpIdRef = useRef<string | null>(null);
  const initTriggeredRef = useRef<string | null>(null);

  // Split Diff L2 Drawer state (restored seamlessly from URL on return from Trial Microscope!)
  const [splitPair, setSplitPair] = useState<SplitPairState | null>(() => {
    const stTask = searchParams.get('split_task');
    const stAb = searchParams.get('split_ab');
    const stAt = searchParams.get('split_at');
    const stBb = searchParams.get('split_bb');
    const stBt = searchParams.get('split_bt');
    if (stTask && stAb && stAt && stBb && stBt) {
      return {
        kind: searchParams.get('split_kind') === 'legacy' ? 'legacy' : 'batch',
        taskId: stTask,
        aBatch: stAb,
        aTrial: stAt,
        bBatch: stBb,
        bTrial: stBt,
        aModelFile: searchParams.get('split_ma') || undefined,
        bModelFile: searchParams.get('split_mb') || undefined,
      };
    }
    return null;
  });
  const [showSplitTrace, setShowSplitTrace] = useState(false);

  const syncCompareUrl = (updates: Record<string, string | null>) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(updates)) {
          if (v === null || v === '') next.delete(k);
          else next.set(k, v);
        }
        return next;
      },
      { replace: true }
    );
  };

  const optsQ = useQuery({
    queryKey: ['compare-options'],
    queryFn: () => apiFetch<CompareOptionsResp>('/api/v1/compare/options'),
  });

  const writableProjects = useMemo(() => {
    if (optsQ.data?.writable_projects && optsQ.data.writable_projects.length > 0) {
      return optsQ.data.writable_projects;
    }
    const base = (user?.projects || []).filter((p) => Boolean(p));
    if (user?.user_id) {
      const personal = `personal:${user.user_id}`;
      if (!base.includes(personal)) base.push(personal);
    }
    return base.length > 0 ? base : ['default'];
  }, [optsQ.data?.writable_projects, user]);
  const effectiveSaveProject = saveProjectId && writableProjects.includes(saveProjectId) ? saveProjectId : writableProjects[0];

  const savedOpenQ = useQuery({
    queryKey: ['compare-saved-open', openId],
    enabled: Boolean(openId) && activeCmpIdRef.current !== openId,
    queryFn: () => apiFetch<SavedOpenResp>(`/api/v1/compare/s/${encodeURIComponent(openId!)}`),
  });

  const splitAQ = useQuery<TrialDetailDTO | Record<string, unknown>>({
    queryKey: ['split-a', splitPair?.kind, splitPair?.aBatch, splitPair?.aModelFile, splitPair?.aTrial],
    enabled: Boolean(splitPair),
    queryFn: () =>
      splitPair!.kind === 'legacy'
        ? apiFetch<Record<string, unknown>>(
            `/api/v1/legacy/${encodeURIComponent(splitPair!.aBatch)}/samples/${encodeURIComponent(splitPair!.aModelFile || '')}/${encodeURIComponent(splitPair!.aTrial)}`
          )
        : apiFetch<TrialDetailDTO>(
            `/api/v1/batches/${encodeURIComponent(splitPair!.aBatch)}/trials/${encodeURIComponent(splitPair!.aTrial)}`
          ),
  });
  const splitBQ = useQuery<TrialDetailDTO | Record<string, unknown>>({
    queryKey: ['split-b', splitPair?.kind, splitPair?.bBatch, splitPair?.bModelFile, splitPair?.bTrial],
    enabled: Boolean(splitPair),
    queryFn: () =>
      splitPair!.kind === 'legacy'
        ? apiFetch<Record<string, unknown>>(
            `/api/v1/legacy/${encodeURIComponent(splitPair!.bBatch)}/samples/${encodeURIComponent(splitPair!.bModelFile || '')}/${encodeURIComponent(splitPair!.bTrial)}`
          )
        : apiFetch<TrialDetailDTO>(
            `/api/v1/batches/${encodeURIComponent(splitPair!.bBatch)}/trials/${encodeURIComponent(splitPair!.bTrial)}`
          ),
  });
  const splitTrajAQ = useQuery({
    queryKey: ['split-traj-a', splitPair?.aBatch, splitPair?.aTrial],
    enabled: Boolean(splitPair && splitPair.kind === 'batch' && showSplitTrace),
    queryFn: () =>
      apiFetch<{ steps: Array<{ step_id: number; source: string | null; message: unknown; paired_tools: Array<{ function_name: string | null }> }> }>(
        `/api/v1/batches/${encodeURIComponent(splitPair!.aBatch)}/trials/${encodeURIComponent(splitPair!.aTrial)}/trajectory?step_limit=20`
      ),
  });
  const splitTrajBQ = useQuery({
    queryKey: ['split-traj-b', splitPair?.bBatch, splitPair?.bTrial],
    enabled: Boolean(splitPair && splitPair.kind === 'batch' && showSplitTrace),
    queryFn: () =>
      apiFetch<{ steps: Array<{ step_id: number; source: string | null; message: unknown; paired_tools: Array<{ function_name: string | null }> }> }>(
        `/api/v1/batches/${encodeURIComponent(splitPair!.bBatch)}/trials/${encodeURIComponent(splitPair!.bTrial)}/trajectory?step_limit=20`
      ),
  });

  const runTaskDiffMut = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      apiFetch<TaskDiffResp>('/api/v1/compare/task-diff', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    onSuccess: (data) => {
      setTaskDiffRestrictedMsg(null);
      setTaskDiff(data);
    },
    onError: (err: Error) => {
      setTaskDiff(null);
      if (err instanceof ApiError && (err.status === 403 || err.code === 'INCOMPARABLE_PROTOCOL')) {
        setTaskDiffRestrictedMsg(err.message || '当前对比对象或账号不满足逐题明细展示条件，上方保留聚合指标。');
        return;
      }
      setTaskDiffRestrictedMsg(null);
      pushToast('error', `逐题 Diff 加载失败：${err.message}`);
    },
  });

  const triggerTaskDiff = (
    cmpData: CompareQueryResponse,
    opts?: {
      filter?: DiffFilterKind;
      subjectA?: string;
      subjectB?: string;
      datasetReleaseId?: string;
      benchmarkSlug?: string;
      pageSize?: number;
      scrollToDiff?: boolean;
      syncUrl?: boolean;
    }
  ) => {
    const f = opts?.filter ?? diffFilter;
    const isBatch = cmpData.bound_snapshot.batches.length >= 2;
    const legSubjs = cmpData.bound_snapshot.legacy_subjects || [];
    const subA =
      opts?.subjectA ||
      (isBatch ? cmpData.bound_snapshot.batches[0].subject_id : legSubjs[0]?.subject_id || 's1');
    const subB =
      opts?.subjectB ||
      (isBatch ? cmpData.bound_snapshot.batches[1].subject_id : legSubjs[1]?.subject_id || 's2');
    const ds = opts?.datasetReleaseId ?? (isBatch ? cmpData.rows[0]?.dataset_release_id || '' : '');
    const bm = opts?.benchmarkSlug ?? '';
    const pageSize = opts?.pageSize ?? (f === 'all' ? 200 : 50);

    setDiffFilter(f);
    setDiffSubjectA(subA);
    setDiffSubjectB(subB);
    if (ds) setDiffDataset(ds);
    if (bm) setDiffBenchmark(bm);

    if (opts?.syncUrl !== false) {
      syncCompareUrl({
        cmp: shortId ? null : cmpData.comparison_id,
        diff_filter: f,
        diff_a: subA,
        diff_b: subB,
        diff_ds: ds || null,
        diff_bm: bm || null,
      });
    }

    if (isBatch) {
      runTaskDiffMut.mutate({
        comparison_id: cmpData.comparison_id,
        subject_a_id: subA,
        subject_b_id: subB,
        dataset_release_id: ds || undefined,
        filter: f,
        page_size: pageSize,
      });
    } else if (bm || ds) {
      runTaskDiffMut.mutate({
        comparison_id: cmpData.comparison_id,
        subject_a_id: subA,
        subject_b_id: subB,
        dataset_release_id: ds || undefined,
        benchmark_slug: bm || undefined,
        filter: f,
        page_size: pageSize,
      });
    }

    if (opts?.scrollToDiff) {
      window.setTimeout(() => {
        taskDiffSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 120);
    }
  };

  const runCompareMut = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      apiFetch<CompareQueryResponse>('/api/v1/compare/query', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    onSuccess: (data) => {
      activeCmpIdRef.current = data.comparison_id;
      setResult(data);
      setTaskDiff(null);
      setTaskDiffRestrictedMsg(null);
      if (!shortId) {
        syncCompareUrl({ cmp: data.comparison_id });
      }
      if (data.bound_snapshot.batches.length >= 2) {
        const compRow =
          (focusDatasetParam ? data.rows.find((r) => r.dataset_release_id === focusDatasetParam && r.comparable) : null) ||
          data.rows.find((r) => r.comparable);
        if (compRow) {
          triggerTaskDiff(data, {
            filter: diffFilter,
            subjectA: data.bound_snapshot.batches[0].subject_id,
            subjectB: data.bound_snapshot.batches[1].subject_id,
            datasetReleaseId: compRow.dataset_release_id,
            scrollToDiff: Boolean(focusBenchmarkParam || focusDatasetParam),
            syncUrl: true,
          });
        } else {
          setTaskDiffRestrictedMsg('当前对比双方不属于同一可比数据集版本或协议不一致，无法生成跨版本逐题胜负明细。');
        }
      } else if (focusBenchmarkParam || focusDatasetParam) {
        triggerTaskDiff(data, {
          filter: diffFilter,
          datasetReleaseId: focusDatasetParam,
          benchmarkSlug: focusBenchmarkParam,
          scrollToDiff: true,
          syncUrl: true,
        });
      }
    },
    onError: (err: Error) => pushToast('error', `对比查询失败：${err.message}`),
  });

  const saveMut = useMutation({
    mutationFn: () =>
      apiFetch<{ short_id: string }>('/api/v1/compare/saved', {
        method: 'POST',
        body: JSON.stringify({
          name: saveName.trim(),
          mode: saveMode,
          project_id: effectiveSaveProject,
          comparison_id: result!.comparison_id,
        }),
      }),
    onSuccess: (res) => {
      const link = `${window.location.origin}/compare/s/${res.short_id}`;
      navigator.clipboard?.writeText(link).catch(() => {});
      pushToast('success', `已保存并复制对比短链：/compare/s/${res.short_id}`);
      setSaveName('');
      optsQ.refetch();
    },
    onError: (err: Error) => pushToast('error', `保存短链失败：${err.message}`),
  });

  const deleteSavedMut = useMutation({
    mutationFn: (sid: string) =>
      apiFetch<{ ok: boolean }>(`/api/v1/compare/saved/${encodeURIComponent(sid)}`, { method: 'DELETE' }),
    onSuccess: () => {
      pushToast('success', '已删除保存的对比短链');
      optsQ.refetch();
    },
    onError: (err: Error) => pushToast('error', `删除失败：${err.message}`),
  });

  const copyMarkdownMatrix = () => {
    if (!result) return;
    const cols = subjectMetaList.map((sm) => sm.alias);
    const lines = [
      `| Benchmark | Protocol | Common Valid / Covered | ${cols.join(' | ')} |`,
      `|---|---|---|${cols.map(() => '---').join('|')}|`,
      ...result.rows.map((r) => {
        const denom =
          r.intersection.common_valid_scored_tasks !== null
            ? `${r.intersection.common_valid_scored_tasks}/${r.intersection.common_covered_tasks}`
            : '2026-11-20 full_split';
        const vals = subjectMetaList.map((sm) => {
          const cv = r.cells[sm.id]?.common_valid;
          if (!cv || cv.score === null) return '—';
          return cv.delta_pp !== null && cv.delta_pp !== 0
            ? `${cv.score.toFixed(2)}% (${cv.delta_pp > 0 ? '+' : ''}${cv.delta_pp.toFixed(2)} pp)`
            : `${cv.score.toFixed(2)}%`;
        });
        return `| ${r.raw_dataset_name} | \`${r.scoring_protocol}\` | ${denom} | ${vals.join(' | ')} |`;
      }),
    ];
    navigator.clipboard?.writeText(lines.join('\n')).catch(() => {});
    pushToast('success', '已复制对比矩阵 Markdown 表格');
  };

  // Hydrate full compare state + Task Diff from saved shortId OR restored `cmp` query param (E-01 & E-04!)
  useEffect(() => {
    if (!savedOpenQ.data?.result) return;
    const data = savedOpenQ.data.result;
    activeCmpIdRef.current = data.comparison_id;
    setResult(data);
    const cfgPayload = savedOpenQ.data.config?.payload;
    if (cfgPayload?.subjects && cfgPayload.subjects.length > 0) {
      setSubjects(cfgPayload.subjects);
      setModeKind(cfgPayload.subjects[0]?.kind === 'legacy_model' ? 'legacy_model' : 'batch');
      setBaselineId(cfgPayload.baseline_subject_id || cfgPayload.subjects[0].id);
      setScoringProtocol(cfgPayload.scoring_protocol || '');
      if (cfgPayload.regression_threshold_pp !== undefined) {
        setThresholdPp(cfgPayload.regression_threshold_pp);
      }
      setSampleSetId(cfgPayload.scope?.sample_set_id || '');
    } else if (data.bound_snapshot.batches.length > 0) {
      const boundMeta = savedOpenQ.data.config?.bound_batches || [];
      setModeKind('batch');
      setSubjects(
        data.bound_snapshot.batches.map((b) => ({
          id: b.subject_id,
          kind: 'batch' as const,
          batch_id: b.batch_id,
          alias: boundMeta.find((m) => m.subject_id === b.subject_id)?.alias || b.batch_id,
        }))
      );
      setBaselineId(data.bound_snapshot.batches[0].subject_id);
      setScoringProtocol(data.bound_snapshot.scoring_protocol || '');
    }

    const restoredFilter = parseDiffFilter(searchParams.get('diff_filter'));
    const restoredA = searchParams.get('diff_a') || undefined;
    const restoredB = searchParams.get('diff_b') || undefined;
    const restoredDs = searchParams.get('diff_ds') || focusDatasetParam || data.rows[0]?.dataset_release_id || '';
    const restoredBm = searchParams.get('diff_bm') || focusBenchmarkParam || '';

    if (data.bound_snapshot.batches.length >= 2) {
      const compRow =
        (restoredDs ? data.rows.find((r) => r.dataset_release_id === restoredDs && r.comparable) : null) ||
        data.rows.find((r) => r.comparable);
      if (compRow) {
        triggerTaskDiff(data, {
          filter: restoredFilter,
          subjectA: restoredA || data.bound_snapshot.batches[0].subject_id,
          subjectB: restoredB || data.bound_snapshot.batches[1].subject_id,
          datasetReleaseId: compRow.dataset_release_id,
          syncUrl: false,
        });
      } else {
        setTaskDiffRestrictedMsg('当前对比双方不属于同一可比数据集版本或协议不一致，无法生成跨版本逐题胜负明细。');
      }
    } else if (restoredBm || restoredDs) {
      triggerTaskDiff(data, {
        filter: restoredFilter,
        subjectA: restoredA,
        subjectB: restoredB,
        datasetReleaseId: restoredDs,
        benchmarkSlug: restoredBm,
        syncUrl: false,
      });
    }
  }, [savedOpenQ.data]);

  useEffect(() => {
    if (openId) return;
    const presetParam = searchParams.get('preset');
    const kindParam = searchParams.get('kind');
    const bParam = searchParams.get('baseline');
    const cParam = searchParams.get('candidate');
    const thrParam = searchParams.get('regression_threshold_pp');
    const initKey = `${routeA || ''}|${routeB || ''}|${presetParam || ''}|${kindParam || ''}|${bParam || ''}|${cParam || ''}|${thrParam || ''}|${focusBenchmarkParam}|${focusDatasetParam}`;
    if (initTriggeredRef.current === initKey) return;

    if (kindParam === 'legacy_model' && bParam && cParam) {
      initTriggeredRef.current = initKey;
      const thr = thrParam !== null && !Number.isNaN(Number(thrParam)) ? Number(thrParam) : thresholdPp;
      const subjs: CompareSubjectRef[] = [
        { id: 's1', kind: 'legacy_model', model_cols: bParam.split(','), alias: bParam },
        { id: 's2', kind: 'legacy_model', model_cols: cParam.split(','), alias: cParam },
      ];
      setModeKind('legacy_model');
      setSubjects(subjs);
      setBaselineId('s1');
      setThresholdPp(thr);
      runCompareMut.mutate({ subjects: subjs, baseline_subject_id: 's1', regression_threshold_pp: thr, include_verdict: true });
      return;
    }

    if (routeA && routeB) {
      initTriggeredRef.current = initKey;
      const subjs: CompareSubjectRef[] = [
        { id: 's1', kind: 'batch', batch_id: routeA, alias: routeA },
        { id: 's2', kind: 'batch', batch_id: routeB, alias: routeB },
      ];
      setModeKind('batch');
      setSubjects(subjs);
      setBaselineId('s1');
      runCompareMut.mutate({ subjects: subjs, baseline_subject_id: 's1', regression_threshold_pp: thresholdPp, include_verdict: true });
      return;
    }

    if (presetParam) {
      if (!optsQ.data?.presets) return;
      const p = optsQ.data.presets.find((x) => x.preset_id === presetParam);
      if (p) {
        initTriggeredRef.current = initKey;
        const overrideThr = thrParam !== null && !Number.isNaN(Number(thrParam)) ? Number(thrParam) : undefined;
        applyPreset(p, overrideThr);
        return;
      }
    }

    const fromDock = searchParams.get('fromDock') === '1';
    if ((fromDock || subjects.length === 0) && compareBasket.length >= 2) {
      const firstKind = compareBasket[0].kind;
      const sameKind = compareBasket.filter((x) => x.kind === firstKind);
      if (sameKind.length >= 2) {
        initTriggeredRef.current = initKey;
        setModeKind(firstKind);
        setSubjects(sameKind);
        const base = sameKind.some((x) => x.id === basketBaselineId) ? basketBaselineId! : sameKind[0].id;
        setBaselineId(base);
        runCompareMut.mutate({
          subjects: sameKind,
          baseline_subject_id: base,
          regression_threshold_pp: thresholdPp,
          enable_faults_as_zero_protocol: enableFaultsAsZero,
          include_verdict: true,
        });
        return;
      }
    } else if (subjects.length === 0 && !result && optsQ.data?.presets?.length) {
      initTriggeredRef.current = initKey;
      applyPreset(optsQ.data.presets[0]);
    }
  }, [optsQ.data, openId, routeA, routeB, searchParams]);

  const applyPreset = (p: CompareOptionsResp['presets'][number], overrideThresholdPp?: number) => {
    const k = p.payload.subjects[0]?.kind || 'batch';
    const effThr = overrideThresholdPp !== undefined ? overrideThresholdPp : p.payload.regression_threshold_pp ?? -1.0;
    setModeKind(k);
    setSubjects(p.payload.subjects);
    setBaselineId(p.payload.baseline_subject_id);
    setScoringProtocol(p.payload.scoring_protocol || '');
    setThresholdPp(effThr);
    setSampleSetId('');
    setSplitPair(null);
    runCompareMut.mutate({
      ...p.payload,
      regression_threshold_pp: effThr,
      enable_faults_as_zero_protocol: enableFaultsAsZero,
      include_verdict: true,
    });
  };

  const handleRun = () => {
    if (subjects.length < 2) {
      pushToast('warning', '请至少选择 2 个同类型对比对象');
      return;
    }
    const base = subjects.some((s) => s.id === baselineId) ? baselineId : subjects[0].id;
    runCompareMut.mutate({
      subjects,
      baseline_subject_id: base,
      scoring_protocol: scoringProtocol || undefined,
      regression_threshold_pp: thresholdPp,
      enable_faults_as_zero_protocol: enableFaultsAsZero,
      include_verdict: true,
      scope: sampleSetId ? { sample_set_id: sampleSetId } : undefined,
    });
  };

  const handleExport = async (fmt: 'csv' | 'json' | 'markdown' | 'html') => {
    if (!result) return;
    try {
      const ext = fmt === 'markdown' ? 'md' : fmt;
      await apiDownload('/api/v1/compare/export', { comparison_id: result.comparison_id, format: fmt }, `compare-${result.comparison_id}.${ext}`);
      pushToast('success', `已导出 ${fmt.toUpperCase()} 报告`);
    } catch (err) {
      pushToast('error', (err as Error).message);
    }
  };

  const subjectMetaList = useMemo(() => {
    if (!result) return [];
    if (result.bound_snapshot.legacy_subjects && result.bound_snapshot.legacy_subjects.length > 0) {
      return result.bound_snapshot.legacy_subjects.map((x) => ({ id: x.subject_id, alias: x.alias }));
    }
    return result.bound_snapshot.batches.map((b) => ({
      id: b.subject_id,
      alias: subjects.find((s) => s.id === b.subject_id)?.alias || b.batch_id,
    }));
  }, [result, subjects]);

  const chartRows = useMemo(() => result?.rows.filter((row) => row.comparable).slice(0, 14) ?? [], [result]);
  const presets = optsQ.data?.presets ?? [];
  const selectedPreset = presets.find(({ payload }) =>
    !sampleSetId &&
    baselineId === payload.baseline_subject_id &&
    scoringProtocol === (payload.scoring_protocol || '') &&
    subjects.length === payload.subjects.length &&
    payload.subjects.every((presetSubject, index) => {
      const subject = subjects[index];
      return subject.id === presetSubject.id &&
        subject.kind === presetSubject.kind &&
        subject.batch_id === presetSubject.batch_id &&
        (subject.model_cols?.join(',') ?? subject.model_col) ===
          (presetSubject.model_cols?.join(',') ?? presetSubject.model_col);
    })
  );
  const visiblePresets = presetsExpanded ? presets : presets.slice(0, COLLAPSED_PRESET_COUNT);
  if (!presetsExpanded && selectedPreset && !visiblePresets.includes(selectedPreset)) {
    visiblePresets[COLLAPSED_PRESET_COUNT - 1] = selectedPreset;
  }

  return (
    <PageTransition className="max-w-[1520px] mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <GitCompareArrows className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>动态自由对比工作台（Dynamic Compare &amp; Split Diff）</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            绑定发布代快照 · 严格区分三大分母口径（common_valid / faults_as_zero / self_full_batch）· 支持保存短链与离线导出
          </p>
        </div>

        {result && (
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            {(['csv', 'json', 'markdown', 'html'] as const).map((fmt) => (
              <button
                key={fmt}
                type="button"
                onClick={() => handleExport(fmt)}
                className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
              >
                <Download className="w-3.5 h-3.5" />
                <span className="uppercase">{fmt}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      {presets.length > 0 && (
        <section
          aria-labelledby="compare-presets-heading"
          className="@container rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5"
        >
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 id="compare-presets-heading" className="flex items-center gap-1.5 text-sm font-bold text-[var(--text-primary)]">
              <Sparkles aria-hidden="true" className="size-4 shrink-0 text-[var(--brand-primary)]" /> 197 实测一键预设对比组合
            </h2>
            {presets.length > COLLAPSED_PRESET_COUNT && (
              <button
                type="button"
                aria-expanded={presetsExpanded}
                aria-controls="compare-preset-grid"
                onClick={() => setPresetsExpanded(!presetsExpanded)}
                className="inline-flex cursor-pointer items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-[var(--brand-primary)] hover:bg-[var(--brand-tint)]"
              >
                {presetsExpanded ? '收起预设' : `展开全部 ${presets.length} 组`}
                <ChevronDown aria-hidden="true" className={`size-3.5 ${presetsExpanded ? 'rotate-180' : ''}`} />
              </button>
            )}
          </div>
          <div id="compare-preset-grid" className="grid auto-rows-fr grid-cols-1 gap-2.5 @min-[310px]:grid-cols-2 @min-[780px]:grid-cols-3">
            {visiblePresets.map((preset) => {
              const selected = preset === selectedPreset;
              return (
                <button
                  key={preset.preset_id}
                  type="button"
                  aria-pressed={selected}
                  title={`${preset.title} · ${preset.common_note}`}
                  onClick={() => applyPreset(preset)}
                  className={`grid min-h-27 min-w-0 cursor-pointer grid-rows-[auto_1fr_auto] gap-1 rounded-lg border px-2.5 py-2 text-left transition-colors hover:border-[var(--brand-primary)] @min-[480px]:px-3 ${
                    selected
                      ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)]'
                      : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)]'
                  }`}
                >
                  <span className="flex items-center justify-between gap-1">
                    <span className="rounded bg-[var(--bg-surface)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--text-secondary)]">
                      {PRESET_TRACK_LABELS[preset.preset_id] ?? '其他'}
                    </span>
                    {selected && <Check aria-hidden="true" className="size-4 shrink-0 text-[var(--brand-primary)]" />}
                  </span>
                  <span className="line-clamp-3 text-xs leading-4 font-semibold text-[var(--text-primary)] @min-[480px]:line-clamp-2">
                    {preset.title}
                  </span>
                  <span className="line-clamp-2 text-[11px] leading-4 text-[var(--text-secondary)]">{preset.common_note}</span>
                </button>
              );
            })}
          </div>
        </section>
      )}

      {/* 4-Step Builder Panel */}
      <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3 text-xs">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SlidingTabs
            layoutId="compare-mode-kind"
            size="sm"
            value={modeKind}
            onChange={(v) => {
              setModeKind(v as 'batch' | 'legacy_model');
              setSubjects([]);
            }}
            options={[
              { id: 'batch', label: '评测批次对照（含逐题 Split Diff）' },
              { id: 'legacy_model', label: '2026-11-20 三线模型总表对比' },
            ]}
          />

          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="添加对比对象"
              value=""
              onChange={(e) => {
                const val = e.target.value;
                if (!val) return;
                if (modeKind === 'batch') {
                  const b = optsQ.data?.batch_subjects.find((x) => x.batch_id === val);
                  if (b && !subjects.some((s) => s.batch_id === b.batch_id)) {
                    const id = `s_${Date.now().toString(36)}`;
                    setSubjects((prev) => [...prev, { id, kind: 'batch', batch_id: b.batch_id, alias: b.batch_name }]);
                    if (!baselineId) setBaselineId(id);
                  }
                } else {
                  const lm = optsQ.data?.legacy_model_subjects.find((x) => x.model_col === val);
                  if (lm) {
                    const id = `s_${Date.now().toString(36)}`;
                    setSubjects((prev) => [
                      ...prev,
                      { id, kind: 'legacy_model', model_cols: lm.model_col.split(','), alias: lm.model_canonical },
                    ]);
                    if (!baselineId) setBaselineId(id);
                  }
                }
              }}
              className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs max-w-xs cursor-pointer"
            >
              <option value="">+ 添加{modeKind === 'batch' ? '授权批次' : '历史模型列'}...</option>
              {modeKind === 'batch'
                ? (optsQ.data?.batch_subjects || []).map((b) => (
                    <option key={b.batch_id} value={b.batch_id}>
                      [{b.benchmark_slug || 'unmapped'}] {b.batch_name} ({b.scored}/{b.planned || b.recorded})
                    </option>
                  ))
                : (optsQ.data?.legacy_model_subjects || []).map((m) => (
                    <option key={m.model_col} value={m.model_col}>
                      {m.model_canonical} ({m.bench_count} 项)
                    </option>
                  ))}
            </select>

            {modeKind === 'batch' && (optsQ.data?.sample_sets || []).length > 0 && (
              <select
                aria-label="选择自定义样本集"
                value={sampleSetId}
                onChange={(e) => setSampleSetId(e.target.value)}
                className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs cursor-pointer"
              >
                <option value="">全量共同题（无样本集过滤）</option>
                {optsQ.data!.sample_sets.map((ss) => (
                  <option key={ss.set_id} value={ss.set_id}>
                    样本集: {ss.name} ({ss.task_ids.length} 题)
                  </option>
                ))}
              </select>
            )}

            <select
              aria-label="防退化护栏阈值"
              value={String(thresholdPp)}
              onChange={(e) => setThresholdPp(Number(e.target.value))}
              className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs cursor-pointer"
            >
              <option value="-0.5">护栏阈值: -0.5 pp</option>
              <option value="-1">护栏阈值: -1.0 pp</option>
              <option value="-2">护栏阈值: -2.0 pp</option>
            </select>

            <label className="inline-flex items-center gap-1.5 cursor-pointer text-[var(--text-secondary)]">
              <input
                type="checkbox"
                checked={enableFaultsAsZero}
                onChange={(e) => setEnableFaultsAsZero(e.target.checked)}
              />
              <span>展示故障按0分口径</span>
            </label>

            <button
              type="button"
              onClick={handleRun}
              disabled={runCompareMut.isPending}
              className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-4 py-1.5 font-semibold text-white cursor-pointer"
            >
              <Play className="w-3.5 h-3.5" />
              <span>{runCompareMut.isPending ? '计算中...' : '执行对比'}</span>
            </button>
          </div>
        </div>

        {/* Selected Subject Pills with Crown Baseline Picker */}
        <div className="flex flex-wrap items-center gap-2">
          {subjects.map((s) => {
            const isBase = s.id === baselineId;
            return (
              <div
                key={s.id}
                className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 ${
                  isBase
                    ? 'border-[var(--best-cell)] bg-[var(--best-cell-bg)] text-[var(--text-primary)]'
                    : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                }`}
              >
                <button
                  type="button"
                  onClick={() => setBaselineId(s.id)}
                  title="设为基线 (Baseline)"
                  className={`cursor-pointer ${isBase ? 'text-[var(--best-cell)]' : 'text-[var(--text-muted)] hover:text-[var(--best-cell)]'}`}
                >
                  <Crown className="w-3.5 h-3.5" />
                </button>
                <span className="font-medium max-w-[200px] truncate">{s.alias}</span>
                <button
                  type="button"
                  onClick={() => setSubjects((prev) => prev.filter((x) => x.id !== s.id))}
                  className="text-[var(--text-muted)] hover:text-[var(--sem-err-fg)] cursor-pointer"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
            );
          })}
          {subjects.length === 0 && <span className="text-[var(--text-muted)]">请从上方预设或下拉框添加至少 2 个对比对象。</span>}
        </div>
      </div>

      {/* Results Area */}
      {runCompareMut.isPending ? (
        <TableSkeleton rows={8} cols={5} />
      ) : result ? (
        <div className="space-y-5">
          {/* Auto Insights & Save Short Link Bar */}
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 flex flex-wrap items-center justify-between gap-3 text-xs">
            <div className="space-y-1 flex-1 min-w-[280px]">
              {result.auto_insights.map((ins, i) => (
                <div key={i} className="text-[var(--text-primary)] leading-relaxed">
                  {ins.text}
                </div>
              ))}
              <div className="text-[11px] font-mono text-[var(--text-muted)]">
                comparison_id: {result.comparison_id} · common benchmarks: {result.comparative_summary.common_comparable_benchmarks.length}
              </div>
            </div>

            {user && ['researcher', 'admin'].includes(user.role) && (
              <div className="flex flex-wrap items-center gap-1.5">
                <input
                  type="text"
                  value={saveName}
                  onChange={(e) => setSaveName(e.target.value)}
                  placeholder="命名以保存短链..."
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs w-40"
                />
                <select
                  aria-label="保存目标项目"
                  value={effectiveSaveProject}
                  onChange={(e) => setSaveProjectId(e.target.value)}
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1.5 text-xs"
                >
                  {writableProjects.map((pid) => (
                    <option key={pid} value={pid}>
                      项目: {pid}
                    </option>
                  ))}
                </select>
                <select
                  aria-label="保存模式"
                  value={saveMode}
                  onChange={(e) => setSaveMode(e.target.value as 'snapshot' | 'live')}
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1.5 text-xs"
                >
                  <option value="snapshot">冻结快照</option>
                  <option value="live">动态 Live</option>
                </select>
                <button
                  type="button"
                  disabled={!saveName.trim() || saveMut.isPending}
                  onClick={() => saveMut.mutate()}
                  className="inline-flex items-center gap-1 rounded-lg bg-[var(--brand-tint)] px-3 py-1.5 font-semibold text-[var(--brand-primary)] hover:bg-[var(--brand-primary)] hover:text-white disabled:opacity-45 cursor-pointer"
                >
                  <Share2 className="w-3.5 h-3.5" /> 保存短链
                </button>
              </div>
            )}
          </div>

          {/* Comparative Macro Summary vs Self-Coverage Avg */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
            {subjectMetaList.map((sm) => {
              const info = result.comparative_summary.by_subject[sm.id];
              return (
                <div
                  key={sm.id}
                  className={`rounded-xl border p-4 ${
                    info?.is_best
                      ? 'border-[var(--best-cell)] bg-[var(--best-cell-bg)]/50'
                      : 'border-[var(--border-subtle)] bg-[var(--bg-surface)]'
                  }`}
                >
                  <div className="font-bold text-[var(--text-primary)] truncate" title={sm.alias}>
                    {sm.alias}
                  </div>
                  <div className="mt-2 flex items-baseline justify-between">
                    <span className="text-[var(--text-muted)]">可比宏观均分</span>
                    <span className="text-lg font-bold tabular-nums text-[var(--text-primary)]">
                      {fmtPct(info?.comparative_macro_avg)}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center justify-between text-[11px]">
                    <span className="text-[var(--text-muted)]">相对基线 Δ</span>
                    <span className="font-mono tabular-nums">{fmtDeltaPP(info?.delta_pp)}</span>
                  </div>
                  <div className="mt-1 flex items-center justify-between text-[11px] text-[var(--text-muted)]">
                    <span>自覆盖参考均分 ({info?.self_bench_count || 0})</span>
                    <span className="tabular-nums">{fmtPct(info?.self_full_batch_avg)}</span>
                  </div>
                </div>
              );
            })}
          </div>

          {chartRows.length > 0 && (
            <CompareScoreCharts comparisonId={result.comparison_id} rows={chartRows} subjects={subjectMetaList} />
          )}

          {/* Multi-Denominator Pivot Matrix */}
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
            <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-2 text-xs font-bold text-[var(--text-primary)]">
              <div className="flex items-center gap-2">
                <span>三大分母口径对照矩阵（{result.rows.length} 项）</span>
                <MethodologyPopover
                  title="三大分母口径"
                  summary="主值展示共同有效题交集 (common_valid)；下方附共同覆盖故障按0分 (faults_as_zero) 与各批次独立全批有效均值 (self_full_batch_valid)，三者严禁混算。"
                  anchor="#three-denominators"
                />
              </div>
              <button
                type="button"
                onClick={copyMarkdownMatrix}
                className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1 text-[11px] font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
              >
                Copy Markdown Table
              </button>
            </div>
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                  <th className="py-2.5 px-3.5">基准 / 协议</th>
                  <th className="py-2.5 px-3">共同题交集</th>
                  {subjectMetaList.map((sm) => (
                    <th key={sm.id} className="py-2.5 px-3">
                      {sm.alias}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {result.rows.map((row) => (
                  <tr key={row.dataset_release_id} className="hover:bg-[var(--bg-elevated)]/50">
                    <td className="py-2.5 px-3.5">
                      <Link
                        to={`/benchmarks/${row.benchmark_slug}`}
                        className="font-semibold text-[var(--text-primary)] hover:text-[var(--brand-primary)] no-underline"
                      >
                        {row.raw_dataset_name}
                      </Link>
                      <div className="text-[10px] font-mono text-[var(--text-muted)]">{row.scoring_protocol}</div>
                    </td>
                    <td className="py-2.5 px-3 tabular-nums text-[11px] text-[var(--text-secondary)]">
                      {row.intersection.common_valid_scored_tasks !== null ? (
                        <button
                          type="button"
                          aria-label={`查看全部 ${row.intersection.common_valid_scored_tasks} 道共同有效题`}
                          onClick={() =>
                            triggerTaskDiff(result, {
                              filter: 'all',
                              subjectA: diffSubjectA || result.bound_snapshot.batches[0]?.subject_id,
                              subjectB: diffSubjectB || result.bound_snapshot.batches[1]?.subject_id,
                              datasetReleaseId: row.dataset_release_id,
                              pageSize: Math.max(200, row.intersection.common_valid_scored_tasks || 200),
                              scrollToDiff: true,
                            })
                          }
                          className="text-left rounded-lg border border-transparent hover:border-[var(--brand-primary)] hover:bg-[var(--brand-tint)]/50 px-2 py-1 -mx-2 -my-1 transition-colors cursor-pointer"
                        >
                          <div className="text-[var(--brand-primary)] font-medium">
                            有效 <strong>{row.intersection.common_valid_scored_tasks}</strong> / 覆盖{' '}
                            {row.intersection.common_covered_tasks}
                          </div>
                          {row.intersection.fault_excluded_tasks > 0 && (
                            <div className="text-[var(--sem-err-fg)]">故障排除 {row.intersection.fault_excluded_tasks}</div>
                          )}
                          <div className="text-[10px] text-[var(--text-muted)]">点击展开全部共同有效题 →</div>
                        </button>
                      ) : (
                        <button
                          type="button"
                          aria-label={`查看 ${row.raw_dataset_name} 同题双栏证据`}
                          onClick={() =>
                            triggerTaskDiff(result, {
                              filter: 'divergent',
                              datasetReleaseId: row.dataset_release_id,
                              benchmarkSlug: row.benchmark_slug,
                              pageSize: 100,
                              scrollToDiff: true,
                            })
                          }
                          className="text-left rounded-lg border border-transparent hover:border-[var(--brand-primary)] hover:bg-[var(--brand-tint)]/50 px-2 py-1 -mx-2 -my-1 transition-colors cursor-pointer"
                        >
                          <div className="text-[var(--brand-primary)] font-medium">2026-11-20 全量</div>
                          <div className="text-[10px] text-[var(--text-muted)]">点击查同题证据 →</div>
                        </button>
                      )}
                    </td>
                    {subjectMetaList.map((sm) => {
                      const c = row.cells[sm.id];
                      if (!c) return <td key={sm.id} className="py-2 px-3">—</td>;
                      return (
                        <td key={sm.id} className="py-2 px-3 align-top">
                          <CompositeScoreCell
                            score={c.common_valid.score}
                            deltaPp={c.common_valid.delta_pp}
                            isBest={c.common_valid.is_best}
                            coverage={
                              c.common_valid.denominator
                                ? `${c.common_valid.passed}/${c.common_valid.denominator}`
                                : undefined
                            }
                          />
                          {c.common_covered_faults_as_zero.enabled_by_protocol &&
                            c.common_covered_faults_as_zero.score !== undefined &&
                            c.common_covered_faults_as_zero.score !== null && (
                              <div className="px-2 text-[10px] tabular-nums text-[var(--text-muted)]">
                                故障按0: {fmtPct(c.common_covered_faults_as_zero.score)} ({c.common_covered_faults_as_zero.passed}/
                                {c.common_covered_faults_as_zero.denominator})
                              </div>
                            )}
                          {c.self_full_batch_valid.valid_scored !== null && (
                            <div className="px-2 text-[10px] tabular-nums text-[var(--text-muted)]">
                              全批参考: {fmtPct(c.self_full_batch_valid.score)} ({c.self_full_batch_valid.valid_scored}/
                              {c.self_full_batch_valid.split_total})
                            </div>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* 3-Track Gain vs Guardrail Verdict */}
          {result.gain_vs_guardrail_verdict && (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3">
              <div className="font-bold text-xs text-[var(--text-primary)] flex items-center gap-1.5">
                <ShieldCheck className="w-4 h-4 text-[var(--brand-primary)]" /> 三线提分与防退化护栏裁决（阈值{' '}
                {result.gain_vs_guardrail_verdict.regression_threshold_pp} pp）
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
                {result.gain_vs_guardrail_verdict.agentic_gains.map((g) => (
                  <div
                    key={`ag-${g.benchmark_slug}`}
                    className="flex items-center justify-between gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2"
                  >
                    <div>
                      <span className="font-semibold">{g.benchmark_slug}</span>{' '}
                      <span className="text-[11px] text-[var(--text-muted)]">({g.coverage})</span>
                    </div>
                    <GainGuardrailPill mode="gain" status={g.status} deltaPp={g.delta_pp} relPct={g.rel_pct} reason={g.reason} />
                  </div>
                ))}
                {result.gain_vs_guardrail_verdict.guardrails.map((g) => (
                  <div
                    key={`gr-${g.track_slug}-${g.benchmark_slug}`}
                    className="flex items-center justify-between gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2"
                  >
                    <div>
                      <span className="font-semibold">{g.benchmark_slug}</span>{' '}
                      <span className="text-[10px] text-[var(--text-muted)]">[{g.track_slug}]</span>
                    </div>
                    <GainGuardrailPill mode="guardrail" status={g.status} deltaPp={g.delta_pp} relPct={g.rel_pct} reason={g.reason} />
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Side-by-Side Task Diff & 4-Cell Win/Tie/Loss Matrix (Batch & Legacy Paired Samples!) */}
          <div ref={taskDiffSectionRef} id="compare-task-diff">
          {taskDiffRestrictedMsg && !taskDiff && (
            <div className="rounded-xl border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] p-4 text-xs flex items-start gap-2.5">
              <Lock className="w-4 h-4 text-[var(--sem-zero-fg)] shrink-0 mt-0.5" />
              <div>
                <div className="font-semibold text-[var(--text-primary)]">逐题明细受权限保护（仅保留上方聚合指标）</div>
                <div className="mt-1 text-[var(--text-secondary)]">{taskDiffRestrictedMsg}</div>
              </div>
            </div>
          )}
          {taskDiff && (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
              <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-3 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-bold text-[var(--text-primary)]">
                    {taskDiff.kind === 'legacy_sample_diff'
                      ? `同题双栏历史样本证据 · ${taskDiff.benchmark_dir}`
                      : '逐题胜负分布'}
                    （共同有效 {taskDiff.intersection.common_valid_scored_tasks} / 共同覆盖{' '}
                    {taskDiff.intersection.common_covered_tasks}）
                  </span>
                  {subjectMetaList.length >= 3 && result.bound_snapshot.batches.length >= 3 && (
                    <div className="inline-flex items-center gap-1 text-[11px]">
                      <select
                        aria-label="选择 A 方对比对象"
                        value={diffSubjectA || subjectMetaList[0]?.id}
                        onChange={(e) =>
                          triggerTaskDiff(result, {
                            filter: diffFilter,
                            subjectA: e.target.value,
                            subjectB: diffSubjectB || subjectMetaList[1]?.id,
                            datasetReleaseId: diffDataset,
                          })
                        }
                        className="rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-1.5 py-0.5 text-[11px]"
                      >
                        {subjectMetaList.map((sm) => (
                          <option key={sm.id} value={sm.id}>
                            A: {sm.alias}
                          </option>
                        ))}
                      </select>
                      <span>vs</span>
                      <select
                        aria-label="选择 B 方对比对象"
                        value={diffSubjectB || subjectMetaList[1]?.id}
                        onChange={(e) =>
                          triggerTaskDiff(result, {
                            filter: diffFilter,
                            subjectA: diffSubjectA || subjectMetaList[0]?.id,
                            subjectB: e.target.value,
                            datasetReleaseId: diffDataset,
                          })
                        }
                        className="rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-1.5 py-0.5 text-[11px]"
                      >
                        {subjectMetaList.map((sm) => (
                          <option key={sm.id} value={sm.id}>
                            B: {sm.alias}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>
                <div className="flex flex-wrap gap-1.5 tabular-nums">
                  {(
                    [
                      { id: 'all', label: `全部有效 (${taskDiff.intersection.common_valid_scored_tasks})` },
                      { id: 'divergent', label: `全部差异 (${taskDiff.win_tie_loss.a_wins + taskDiff.win_tie_loss.b_wins})` },
                      { id: 'a_wins', label: `A 独胜 (${taskDiff.win_tie_loss.a_wins})` },
                      { id: 'b_wins', label: `B 独胜 (${taskDiff.win_tie_loss.b_wins})` },
                      { id: 'both_pass', label: `同过 (${taskDiff.win_tie_loss.both_pass})` },
                      { id: 'both_fail', label: `同错 (${taskDiff.win_tie_loss.both_fail})` },
                    ] as const
                  ).map((f) => (
                    <button
                      key={f.id}
                      type="button"
                      onClick={() =>
                        triggerTaskDiff(result, {
                          filter: f.id,
                          subjectA: diffSubjectA || subjectMetaList[0]?.id,
                          subjectB: diffSubjectB || subjectMetaList[1]?.id,
                          datasetReleaseId: diffDataset,
                          benchmarkSlug: diffBenchmark,
                          pageSize: f.id === 'all' ? Math.max(200, taskDiff.intersection.common_valid_scored_tasks) : 50,
                        })
                      }
                      className={`rounded-full px-2.5 py-1 text-[11px] font-medium cursor-pointer border ${
                        diffFilter === f.id
                          ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                          : 'border-[var(--border-subtle)] bg-[var(--bg-surface)] text-[var(--text-secondary)]'
                      }`}
                    >
                      {f.label}
                    </button>
                  ))}
                  {taskDiff.win_tie_loss.fault_excluded > 0 && (
                    <span className="rounded-full px-2.5 py-1 text-[11px] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]">
                      故障排除 {taskDiff.win_tie_loss.fault_excluded}
                    </span>
                  )}
                  {(taskDiff.checksum_mismatch_tasks || 0) > 0 && (
                    <span className="rounded-full px-2.5 py-1 text-[11px] bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]">
                      版本漂移分组 {taskDiff.checksum_mismatch_tasks}
                    </span>
                  )}
                </div>
              </div>

              {taskDiff.redacted && (
                <div className="px-4 py-2.5 border-b border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] text-xs flex items-center gap-2 text-[var(--text-secondary)]">
                  <Lock className="w-3.5 h-3.5 text-[var(--sem-zero-fg)] shrink-0" />
                  <span>{taskDiff.redacted_reason || `已隐藏 ${taskDiff.hidden_holdout_tasks ?? 0} 道留出集题目明细（仅展示公开题目）`}</span>
                </div>
              )}

              {taskDiff.checksum_drift_groups && taskDiff.checksum_drift_groups.length > 0 && (
                <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--sem-zero-bg)]/40 text-xs space-y-2">
                  <div className="font-semibold text-[var(--sem-zero-fg)]">
                    版本漂移提示：同版本内存在 {taskDiff.checksum_mismatch_tasks} 道同名但 checksum 不同的题目，已按 checksum 独立分组且不计入胜负交集
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {taskDiff.checksum_drift_groups.map((grp, idx) => (
                      <div
                        key={`${grp.task_id}-${grp.task_checksum || 'none'}-${idx}`}
                        className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-2"
                      >
                        <div className="font-mono font-semibold text-[var(--text-primary)]">
                          {grp.task_id}{' '}
                          <span className="text-[10px] text-[var(--text-muted)]">
                            checksum={grp.task_checksum || 'null'}
                          </span>
                        </div>
                        <div className="mt-1 space-y-0.5 text-[11px] text-[var(--text-secondary)]">
                          {grp.subjects.map((s) => (
                            <div key={`${s.subject_id}-${s.trial_id}`}>
                              {s.subject_id}: {s.status} · reward={s.reward ?? '—'}
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                      <th className="py-2 px-3.5">Task ID</th>
                      <th className="py-2 px-3">胜负分类</th>
                      <th className="py-2 px-3">
                        A ({subjectMetaList.find((s) => s.id === diffSubjectA)?.alias || subjectMetaList[0]?.alias})
                      </th>
                      <th className="py-2 px-3">
                        B ({subjectMetaList.find((s) => s.id === diffSubjectB)?.alias || subjectMetaList[1]?.alias})
                      </th>
                      <th className="py-2 px-3.5 text-right">同屏 Split Diff</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {taskDiff.items.map((row) => (
                      <tr
                        key={row.task_id}
                        onClick={() => {
                          const nextPair: SplitPairState = {
                            kind: taskDiff.kind === 'legacy_sample_diff' ? 'legacy' : 'batch',
                            taskId: row.task_id,
                            aBatch: row.a.batch_id,
                            aTrial: row.a.trial_id,
                            bBatch: row.b.batch_id,
                            bTrial: row.b.trial_id,
                            aModelFile: row.a.model_file || taskDiff.model_file_a,
                            bModelFile: row.b.model_file || taskDiff.model_file_b,
                          };
                          setSplitPair(nextPair);
                          syncCompareUrl({
                            split_kind: nextPair.kind,
                            split_task: nextPair.taskId,
                            split_ab: nextPair.aBatch,
                            split_at: nextPair.aTrial,
                            split_bb: nextPair.bBatch,
                            split_bt: nextPair.bTrial,
                            split_ma: nextPair.aModelFile || null,
                            split_mb: nextPair.bModelFile || null,
                          });
                        }}
                        className="hover:bg-[var(--bg-elevated)]/65 cursor-pointer"
                      >
                        <td className="py-2 px-3.5 font-mono font-medium">{row.task_id}</td>
                        <td className="py-2 px-3">
                          <span
                            className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                              row.diff_type === 'a_wins'
                                ? 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                                : row.diff_type === 'b_wins'
                                  ? 'bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                                  : 'bg-[var(--bg-elevated)] text-[var(--text-muted)]'
                            }`}
                          >
                            {row.diff_type}
                          </span>
                        </td>
                        <td className="py-2 px-3 tabular-nums">
                          Reward {row.a.reward ?? '—'}
                          {row.a.duration_sec !== null ? ` · ${fmtDuration(row.a.duration_sec)}` : ''}
                        </td>
                        <td className="py-2 px-3 tabular-nums">
                          Reward {row.b.reward ?? '—'}
                          {row.b.duration_sec !== null ? ` · ${fmtDuration(row.b.duration_sec)}` : ''}
                        </td>
                        <td className="py-2 px-3.5 text-right text-[var(--brand-primary)] font-medium">左右分屏对比</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          </div>

          {/* Saved Comparisons Quick List */}
          {optsQ.data && optsQ.data.saved_comparisons.length > 0 && (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 text-xs">
              <div className="font-bold text-[var(--text-primary)] mb-2 flex items-center gap-1.5">
                <Bookmark className="w-3.5 h-3.5 text-[var(--brand-primary)]" /> 已保存的对比短链
              </div>
              <div className="flex flex-wrap gap-2">
                {optsQ.data.saved_comparisons.map((sc) => (
                  <div
                    key={sc.short_id}
                    className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] hover:border-[var(--brand-primary)] px-2.5 py-1 text-xs"
                  >
                    <button
                      type="button"
                      onClick={() => navigate(`/compare/s/${sc.short_id}`)}
                      className="text-left text-[var(--text-primary)] cursor-pointer"
                    >
                      {sc.name} <span className="font-mono text-[10px] text-[var(--text-muted)]">({sc.mode})</span>
                    </button>
                    {user && ['researcher', 'admin'].includes(user.role) && (
                      <button
                        type="button"
                        title="删除此短链"
                        onClick={() => deleteSavedMut.mutate(sc.short_id)}
                        className="ml-1 text-[var(--text-muted)] hover:text-[var(--sem-err-fg)] cursor-pointer"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      ) : (
        <StateNoticeCard kind="empty" title="请选择对比对象或点击上方 197 实测预设开始分析" />
      )}

      {/* Wide L2 Split Diff Drawer */}
      <SlideOverDrawer
        open={Boolean(splitPair)}
        onClose={() => {
          setSplitPair(null);
          setShowSplitTrace(false);
          syncCompareUrl({
            split_kind: null,
            split_task: null,
            split_ab: null,
            split_at: null,
            split_bb: null,
            split_bt: null,
            split_ma: null,
            split_mb: null,
          });
        }}
        wide
        title={splitPair ? `左右分屏 Split Diff · ${splitPair.taskId}` : ''}
        subtitle={
          splitPair?.kind === 'legacy'
            ? '2026-11-20 历史同题双栏证据 · 按权限脱敏展示模型回答与判分'
            : '保留底层对比矩阵与滚动位置 · 支持双栏 ATIF 轨迹对照与直入任一侧 Trial 显微镜'
        }
      >
        {splitPair && (() => {
          const returnQp = new URLSearchParams(location.search);
          if (result && !shortId) returnQp.set('cmp', result.comparison_id);
          returnQp.set('diff_filter', diffFilter);
          if (diffSubjectA) returnQp.set('diff_a', diffSubjectA);
          if (diffSubjectB) returnQp.set('diff_b', diffSubjectB);
          if (diffDataset) returnQp.set('diff_ds', diffDataset);
          if (diffBenchmark) returnQp.set('diff_bm', diffBenchmark);
          returnQp.set('split_kind', splitPair.kind);
          returnQp.set('split_task', splitPair.taskId);
          returnQp.set('split_ab', splitPair.aBatch);
          returnQp.set('split_at', splitPair.aTrial);
          returnQp.set('split_bb', splitPair.bBatch);
          returnQp.set('split_bt', splitPair.bTrial);
          if (splitPair.aModelFile) returnQp.set('split_ma', splitPair.aModelFile);
          if (splitPair.bModelFile) returnQp.set('split_mb', splitPair.bModelFile);
          const returnToUrl = `${location.pathname}?${returnQp.toString()}`;

          return (
            <div className="space-y-4 text-xs">
              {splitPair.kind === 'batch' && (
                <div className="flex items-center justify-between">
                  <span className="text-[var(--text-secondary)]">按权限惰性加载两侧 ATIF 工具轨迹对比：</span>
                  <button
                    type="button"
                    onClick={() => setShowSplitTrace((v) => !v)}
                    className={`rounded-lg border px-3 py-1 font-medium cursor-pointer ${
                      showSplitTrace
                        ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                        : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                    }`}
                  >
                    {showSplitTrace ? '隐藏双栏轨迹' : '展开双栏 ATIF 轨迹 Diff'}
                  </button>
                </div>
              )}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {[
                  {
                    label: `A · ${subjectMetaList.find((s) => s.id === diffSubjectA)?.alias || subjectMetaList[0]?.alias || splitPair.aBatch}`,
                    q: splitAQ,
                    tq: splitTrajAQ,
                    batch: splitPair.aBatch,
                    trial: splitPair.aTrial,
                  },
                  {
                    label: `B · ${subjectMetaList.find((s) => s.id === diffSubjectB)?.alias || subjectMetaList[1]?.alias || splitPair.bBatch}`,
                    q: splitBQ,
                    tq: splitTrajBQ,
                    batch: splitPair.bBatch,
                    trial: splitPair.bTrial,
                  },
                ].map((side) => (
                  <div key={side.label} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-4 space-y-3">
                    <div className="group/row-action flex items-center justify-between gap-2 pb-2 border-b border-[var(--border-subtle)]">
                      <span className="font-bold text-[var(--text-primary)] truncate">{side.label}</span>
                      {splitPair.kind === 'batch' && (
                        <RowActionIcon
                          variant="microscope"
                          to={`/eval/batches/${encodeURIComponent(side.batch)}/trials/${encodeURIComponent(side.trial)}?returnTo=${encodeURIComponent(returnToUrl)}`}
                        />
                      )}
                    </div>
                    {side.q.isLoading ? (
                      <TableSkeleton rows={4} cols={1} />
                    ) : side.q.isError ? (
                      <div className="text-[var(--text-muted)]">{(side.q.error as Error).message}</div>
                    ) : side.q.data && splitPair.kind === 'legacy' ? (
                      (() => {
                        const leg = side.q.data as { status?: string; reward?: number | null; sample?: Record<string, unknown> };
                        return (
                          <div className="space-y-2">
                            <div className="flex justify-between">
                              <span className="text-[var(--text-muted)]">状态 / Reward</span>
                              <span className="font-mono font-bold">
                                {leg.status || '—'} · {leg.reward ?? '—'}
                              </span>
                            </div>
                            <pre className="m-0 rounded bg-[var(--bg-surface)] p-2.5 text-[11px] font-mono whitespace-pre-wrap break-words max-h-96 overflow-y-auto">
                              {JSON.stringify(leg.sample || {}, null, 2)}
                            </pre>
                          </div>
                        );
                      })()
                    ) : side.q.data ? (
                      (() => {
                        const td = side.q.data as TrialDetailDTO;
                        return (
                          <div className="space-y-2">
                            <div className="flex justify-between">
                              <span className="text-[var(--text-muted)]">状态 / Reward</span>
                              <span className="font-mono font-bold">
                                {td.status} · {td.reward ?? '—'}
                              </span>
                            </div>
                            <div className="flex justify-between">
                              <span className="text-[var(--text-muted)]">总耗时 / 工具步数</span>
                              <span className="tabular-nums">
                                {fmtDuration(td.duration_sec)} · {td.tool_call_count ?? '未记录'}
                              </span>
                            </div>
                            {td.failure?.summary && (
                              <div className="rounded bg-[var(--sem-err-bg)] p-2 text-[var(--sem-err-fg)]">
                                {td.failure.summary}
                              </div>
                            )}
                            <pre className="m-0 rounded bg-[var(--bg-surface)] p-2.5 text-[11px] font-mono whitespace-pre-wrap break-words max-h-60 overflow-y-auto">
                              {JSON.stringify(td.verifier_rewards || {}, null, 2)}
                            </pre>
                            {showSplitTrace && (
                              <div className="pt-2 border-t border-[var(--border-subtle)] space-y-1.5">
                                <div className="font-semibold text-[var(--text-primary)]">ATIF 步骤预览（前 20 步）</div>
                                {side.tq.isLoading ? (
                                  <div className="text-[var(--text-muted)]">加载轨迹中...</div>
                                ) : side.tq.isError ? (
                                  <div className="text-[var(--text-muted)]">{(side.tq.error as Error).message}</div>
                                ) : (side.tq.data?.steps || []).length === 0 ? (
                                  <div className="text-[var(--text-muted)]">无 ATIF 步骤</div>
                                ) : (
                                  <div className="space-y-1.5 max-h-72 overflow-y-auto">
                                    {side.tq.data!.steps.map((st) => (
                                      <div key={st.step_id} className="rounded bg-[var(--bg-surface)] p-2 text-[11px]">
                                        <div className="font-mono text-[var(--brand-primary)]">
                                          #step-{st.step_id} ({st.source || 'agent'})
                                          {st.paired_tools.length > 0 && ` · tools: ${st.paired_tools.map((t) => t.function_name).join(', ')}`}
                                        </div>
                                        {st.message !== null && st.message !== undefined && (
                                          <div className="mt-0.5 text-[var(--text-secondary)] line-clamp-3 break-words">
                                            {typeof st.message === 'string' ? st.message : JSON.stringify(st.message)}
                                          </div>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })()
                    ) : (
                      <div className="text-[var(--text-muted)]">无法加载该侧明细</div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          );
        })()}
      </SlideOverDrawer>
    </PageTransition>
  );
}
