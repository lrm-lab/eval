import React, { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Database,
  FileSearch,
  FolderTree,
  Image as ImageIcon,
  Lock,
  Search,
  Table2,
} from 'lucide-react';
import { apiFetch } from '../lib/api';
import { trackDisplayName } from '../lib/format';
import { useAppStore } from '../lib/store';
import type { BenchmarkTreeNode, DatasetReleaseDTO, DatasetTreeTrack } from '../types/api';
import { PageTransition, SlidingTabs } from '../components/motion/primitives';
import { RowActionIcon } from '../components/ui/RowActionIcon';
import { SlideOverDrawer } from '../components/ui/SlideOverDrawer';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

interface TaskRow {
  dataset_release_id: string;
  task_id: string;
  difficulty: string | null;
  category: string | null;
  language: string | null;
  split: string | null;
}

interface LegacyMediaRef {
  kind?: string;
  source?: string;
  rel_path?: string;
  tsv_key?: string;
  url?: string;
  is_image?: boolean;
  reason?: string;
  basename?: string;
  candidate_count?: number;
  confidence?: string;
}

interface LegacySampleRow {
  model_file: string;
  sample_id: string;
  status: string;
  reward: number | null;
  category: string | null;
  difficulty: string | null;
  media_match_status: string;
  media_refs: LegacyMediaRef[];
}

interface LegacySampleDetailResp {
  benchmark_dir: string;
  model_file: string;
  sample_id: string;
  status: string;
  reward: number | null;
  media_match_status: string;
  media_refs: LegacyMediaRef[];
  sample: Record<string, unknown>;
}

const FALLBACK_LEGACY_DIRS = [
  'CMMMU',
  'MMMU_Pro_10c',
  'MM-IFEval',
  'RealWorldQA',
  'AI2D_TEST',
  'AA-LCR-v1.1',
  'IFBench',
  'IFEval',
  'GPQA_diamond',
  'hle_llmjudge',
  'Multi-LCB',
  'MathVista',
  'CharXiv_reasoning',
  'HallusionBench',
  'MMBench_v1_1_CN',
  'OCRBench',
  'Apex-shortlist',
  'Multi-Challenge',
  'LongMemEval_oracle_CoN',
  'LongMemEval_oracle_direct',
  'LongMemEval_S_CoN',
  'LongMemEval_S_direct',
];

function getVerifiedImageRefs(status: string, refs: LegacyMediaRef[]): Array<{ url: string; label: string }> {
  if (status !== 'unique_hit') return [];
  const out: Array<{ url: string; label: string }> = [];
  refs.forEach((m, idx) => {
    if (m.is_image === false) return;
    if (m.kind === 'tsv_inline' && (m.url || (m.source && m.tsv_key))) {
      const url = m.url || `/api/v1/media/${encodeURIComponent(m.source!)}/tsv-inline/${encodeURIComponent(m.tsv_key!)}?idx=${idx}`;
      out.push({ url, label: `${m.source}/tsv-inline/${m.tsv_key}` });
    } else if ((m.kind === 'file' || (!m.kind && m.source && m.rel_path)) && (m.url || (m.source && m.rel_path))) {
      const url = m.url || `/api/v1/media/${encodeURIComponent(m.source!)}/${m.rel_path}`;
      out.push({ url, label: `${m.source}/${m.rel_path}` });
    }
  });
  return out;
}

export function DatasetsPage() {
  const navigate = useNavigate();
  const [view, setView] = useState<'tree' | 'table' | 'legacy'>('tree');
  const [searchKw, setSearchKw] = useState('');
  const [goalFilter, setGoalFilter] = useState<string>('all');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [collapsedTracks, setCollapsedTracks] = useState<Record<string, boolean>>({});
  const [drawerRelease, setDrawerRelease] = useState<{ bm: BenchmarkTreeNode; rel: DatasetReleaseDTO } | null>(null);
  const [taskPage, setTaskPage] = useState(1);

  // Legacy O(1) sample browser state
  const [legacyDir, setLegacyDir] = useState('CMMMU');
  const [legacyPage, setLegacyPage] = useState(1);
  const [selectedLegacySample, setSelectedLegacySample] = useState<{ dir: string; modelFile: string; sampleId: string } | null>(null);

  const treeQ = useQuery({
    queryKey: ['datasets-tree'],
    queryFn: () => apiFetch<{ tracks: DatasetTreeTrack[] }>('/api/v1/datasets/tree'),
  });

  const drawerTasksQ = useQuery({
    queryKey: ['dataset-release-tasks', drawerRelease?.rel.dataset_release_id, taskPage],
    enabled: Boolean(drawerRelease),
    queryFn: () =>
      apiFetch<{ redacted: boolean; redacted_reason?: string; items: TaskRow[]; total: number }>(
        `/api/v1/datasets/${encodeURIComponent(drawerRelease!.rel.dataset_release_id)}/tasks?page=${taskPage}&page_size=30`
      ),
  });

  const openRelease = (bm: BenchmarkTreeNode, rel: DatasetReleaseDTO) => {
    setTaskPage(1);
    setDrawerRelease({ bm, rel });
  };

  const legacyDirsQ = useQuery({
    queryKey: ['legacy-dirs'],
    enabled: view === 'legacy',
    queryFn: () => apiFetch<{ items: Array<{ name: string; indexed_samples: number }> }>('/api/v1/legacy/dirs'),
  });

  const legacyDirOptions = useMemo(() => {
    if (legacyDirsQ.isSuccess) {
      return (legacyDirsQ.data?.items || []).map((x) => x.name);
    }
    return FALLBACK_LEGACY_DIRS;
  }, [legacyDirsQ.isSuccess, legacyDirsQ.data]);

  const activeLegacyDir = useMemo(() => {
    if (legacyDirOptions.length === 0) return '';
    return legacyDirOptions.includes(legacyDir) ? legacyDir : legacyDirOptions[0];
  }, [legacyDirOptions, legacyDir]);

  const openLegacySample = (row: LegacySampleRow) => {
    setSelectedLegacySample({ dir: activeLegacyDir, modelFile: row.model_file, sampleId: row.sample_id });
  };

  const legacyListQ = useQuery({
    queryKey: ['legacy-samples', activeLegacyDir, legacyPage],
    enabled: view === 'legacy' && Boolean(activeLegacyDir),
    queryFn: () =>
      apiFetch<{ items: LegacySampleRow[]; total: number }>(
        `/api/v1/legacy/${encodeURIComponent(activeLegacyDir)}/samples?page=${legacyPage}&page_size=30`
      ),
  });

  const legacyDetailQ = useQuery({
    queryKey: ['legacy-sample-detail', selectedLegacySample?.dir, selectedLegacySample?.modelFile, selectedLegacySample?.sampleId],
    enabled: Boolean(selectedLegacySample),
    queryFn: () =>
      apiFetch<LegacySampleDetailResp>(
        `/api/v1/legacy/${encodeURIComponent(selectedLegacySample!.dir)}/samples/${encodeURIComponent(
          selectedLegacySample!.modelFile
        )}/${encodeURIComponent(selectedLegacySample!.sampleId)}`
      ),
  });

  const tracks = treeQ.data?.tracks || [];
  const filteredTracks = useMemo(() => {
    const kw = searchKw.trim().toLowerCase();
    return tracks
      .filter((t) => goalFilter === 'all' || t.goal_type === goalFilter)
      .map((t) => ({
        ...t,
        benchmarks: t.benchmarks.filter((b) => {
          if (statusFilter !== 'all' && b.status !== statusFilter) return false;
          if (!kw) return true;
          return (
            b.display_name.toLowerCase().includes(kw) ||
            b.benchmark_slug.toLowerCase().includes(kw) ||
            b.capability.toLowerCase().includes(kw) ||
            b.releases.some((r) => r.dataset_release_id.toLowerCase().includes(kw))
          );
        }),
      }))
      .filter((t) => t.benchmarks.length > 0);
  }, [tracks, searchKw, goalFilter, statusFilter]);

  const flatReleases = useMemo(() => {
    const out: Array<{ track: DatasetTreeTrack; bm: BenchmarkTreeNode; rel: DatasetReleaseDTO }> = [];
    for (const t of filteredTracks) {
      for (const bm of t.benchmarks) {
        if (bm.releases.length === 0) {
          out.push({
            track: t,
            bm,
            rel: {
              dataset_release_id: `${bm.benchmark_slug}__unintegrated`,
              benchmark_slug: bm.benchmark_slug,
              dataset_version: '—',
              split: '—',
              runner: '—',
              known_task_count: null,
              metric_name: '—',
              default_protocol: '—',
              scale_type: '—',
              source_approval: bm.status,
              batch_count: 0,
            },
          });
        } else {
          for (const rel of bm.releases) {
            out.push({ track: t, bm, rel });
          }
        }
      }
    }
    return out;
  }, [filteredTracks]);

  const expandCollapseAll = (collapsed: boolean) => {
    const map: Record<string, boolean> = {};
    tracks.forEach((t) => (map[t.track_slug] = collapsed));
    setCollapsedTracks(map);
  };

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <Database className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>Datasets 三线能力树与历史样本浏览器</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            100% 由 taxonomy.yaml 动态驱动 · 支持 L2 抽屉预览题库（Holdout 按角色脱敏）与 O(1) 字节偏移秒开历史多模态样本
          </p>
        </div>

        <SlidingTabs
          layoutId="datasets-view-tabs"
          size="sm"
          value={view}
          onChange={(v) => setView(v as 'tree' | 'table' | 'legacy')}
          options={[
            { id: 'tree', label: '三线能力树', icon: <FolderTree className="w-3.5 h-3.5" /> },
            { id: 'table', label: '结构化发行版表', icon: <Table2 className="w-3.5 h-3.5" /> },
            { id: 'legacy', label: '2026-11-20 样本 O(1) 浏览', icon: <FileSearch className="w-3.5 h-3.5" /> },
          ]}
        />
      </div>

      {view !== 'legacy' && (
        <div className="space-y-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="relative flex-1 min-w-[220px] max-w-md">
              <Search className="w-3.5 h-3.5 text-[var(--text-muted)] absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={searchKw}
                onChange={(e) => setSearchKw(e.target.value)}
                placeholder="搜索轨道 / 能力维度（如 多模态指令遵循）/ 基准 / Release ID..."
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] pl-8 pr-3 py-1.5 text-xs text-[var(--text-primary)] outline-none focus:border-[var(--brand-primary)]"
              />
            </div>
            {view === 'tree' && (
              <div className="flex items-center gap-1.5 text-xs">
                <button
                  type="button"
                  onClick={() => expandCollapseAll(false)}
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
                >
                  全部展开
                </button>
                <button
                  type="button"
                  onClick={() => expandCollapseAll(true)}
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
                >
                  全部折叠
                </button>
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <span className="text-[var(--text-muted)]">目标线:</span>
            {[
              { id: 'all', label: '全部' },
              { id: 'optimize_gain', label: 'Agentic 提分主线' },
              { id: 'non_regression_guardrail', label: 'Text/Vision 防退化护栏' },
              { id: 'synthesis_loop', label: '合成训练闭环' },
              { id: 'extended_baseline', label: '历史扩展基线' },
            ].map((g) => (
              <button
                key={g.id}
                type="button"
                onClick={() => setGoalFilter(g.id)}
                className={`rounded-full px-2.5 py-0.5 border cursor-pointer ${
                  goalFilter === g.id
                    ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                    : 'border-[var(--border-subtle)] bg-[var(--bg-surface)] text-[var(--text-secondary)]'
                }`}
              >
                {g.label}
              </button>
            ))}
            <span className="ml-2 text-[var(--text-muted)]">状态:</span>
            {[
              { id: 'all', label: '全部' },
              { id: 'integrated', label: '已接入实跑' },
              { id: 'legacy_available', label: '历史基线' },
              { id: 'raw_visible_unintegrated', label: '原始数据可见·未接入' },
              { id: 'pending_version_pin', label: '待定版本' },
            ].map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => setStatusFilter(s.id)}
                className={`rounded-full px-2.5 py-0.5 border cursor-pointer ${
                  statusFilter === s.id
                    ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                    : 'border-[var(--border-subtle)] bg-[var(--bg-surface)] text-[var(--text-secondary)]'
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {view === 'tree' ? (
        treeQ.isLoading ? (
          <TableSkeleton rows={8} cols={4} />
        ) : treeQ.isError ? (
          <StateNoticeCard kind="error" title="三线能力树加载失败" description={(treeQ.error as Error).message} onRetry={() => treeQ.refetch()} />
        ) : (
          <div className="space-y-3.5">
            {filteredTracks.map((track) => {
              const isCollapsed = Boolean(collapsedTracks[track.track_slug]);
              return (
                <div key={track.track_slug} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
                  <button
                    type="button"
                    onClick={() => setCollapsedTracks((prev) => ({ ...prev, [track.track_slug]: !isCollapsed }))}
                    className="w-full flex items-center justify-between px-4 py-3 bg-[var(--bg-elevated)] text-left cursor-pointer"
                  >
                    <div className="flex items-center gap-2">
                      {isCollapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                      <span className="text-sm font-bold text-[var(--text-primary)]">{trackDisplayName(track.track_slug)}</span>
                      <span className="rounded-full px-2 py-0.5 text-[11px] bg-[var(--bg-surface)] border border-[var(--border-subtle)] text-[var(--text-secondary)]">
                        {track.goal_type === 'optimize_gain'
                          ? '提分主线'
                          : track.goal_type === 'non_regression_guardrail'
                            ? '防退化护栏'
                            : track.goal_type}
                      </span>
                    </div>
                    <span className="text-xs tabular-nums text-[var(--text-muted)]">{track.benchmarks.length} 个基准</span>
                  </button>

                  {!isCollapsed && (
                    <div className="p-4 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                      {track.benchmarks.map((bm) => (
                        <div
                          key={bm.benchmark_slug}
                          className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-canvas)]/50 p-3.5 space-y-2.5"
                        >
                          <div className="flex items-start justify-between gap-2">
                            <div>
                              <Link
                                to={`/benchmarks/${bm.benchmark_slug}`}
                                className="text-xs font-bold text-[var(--text-primary)] hover:text-[var(--brand-primary)] no-underline"
                              >
                                {bm.display_name}
                              </Link>
                              <div className="text-[11px] text-[var(--text-secondary)] mt-0.5">{bm.capability}</div>
                            </div>
                            <span
                              className={`rounded px-1.5 py-0.5 text-[10px] shrink-0 ${
                                bm.status === 'integrated'
                                  ? 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                                  : 'bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)]'
                              }`}
                            >
                              {bm.status}
                            </span>
                          </div>

                          {bm.releases.length === 0 ? (
                            <div className="text-[11px] text-[var(--text-muted)] italic">待接入固定发行版</div>
                          ) : (
                            <div className="space-y-1.5">
                              {bm.releases.map((rel) => (
                                <button
                                  key={rel.dataset_release_id}
                                  type="button"
                                  onClick={() => openRelease(bm, rel)}
                                  className="w-full text-left rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] hover:border-[var(--brand-primary)] px-2.5 py-1.5 text-[11px] transition-colors cursor-pointer"
                                >
                                  <div className="flex items-center justify-between gap-1 font-mono text-[var(--text-primary)]">
                                    <span className="truncate">{rel.dataset_release_id}</span>
                                    <span className="tabular-nums text-[var(--brand-primary)] shrink-0">
                                      {rel.batch_count || 0} 批
                                    </span>
                                  </div>
                                  <div className="mt-0.5 flex items-center justify-between text-[10px] text-[var(--text-muted)]">
                                    <span>
                                      {rel.runner} · {rel.split}
                                    </span>
                                    <span className="tabular-nums">{rel.known_task_count ?? '—'} 题 · {rel.scale_type}</span>
                                  </div>
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )
      ) : view === 'table' ? (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                <th className="py-2.5 px-3.5">轨道</th>
                <th className="py-2.5 px-3">基准 / 能力维度</th>
                <th className="py-2.5 px-3">dataset_release_id</th>
                <th className="py-2.5 px-3">默认协议 / 标尺</th>
                <th className="py-2.5 px-3 text-right">题数 / 批次数</th>
                <th className="w-12 py-2.5 px-2 text-center">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {flatReleases.map(({ track, bm, rel }) => (
                <tr
                  key={`${bm.benchmark_slug}-${rel.dataset_release_id}`}
                  onClick={() => {
                    if (!rel.dataset_release_id.endsWith('__unintegrated')) openRelease(bm, rel);
                  }}
                  className="group/row-action hover:bg-[var(--bg-elevated)]/65 cursor-pointer"
                >
                  <td className="py-2 px-3.5 text-[var(--text-secondary)]">{trackDisplayName(track.track_slug)}</td>
                  <td className="py-2 px-3">
                    <div className="font-semibold text-[var(--text-primary)]">{bm.display_name}</div>
                    <div className="text-[11px] text-[var(--text-muted)]">{bm.capability}</div>
                  </td>
                  <td className="py-2 px-3 font-mono text-[11px] text-[var(--text-primary)]">{rel.dataset_release_id}</td>
                  <td className="py-2 px-3 font-mono text-[11px] text-[var(--text-muted)]">
                    {rel.default_protocol} · {rel.scale_type}
                  </td>
                  <td className="py-2 px-3 text-right tabular-nums text-[var(--text-secondary)]">
                    {rel.known_task_count ?? '—'} 题 / {rel.batch_count ?? 0} 批
                  </td>
                  <td className="px-2 text-center">
                    {!rel.dataset_release_id.endsWith('__unintegrated') && (
                      <RowActionIcon
                        variant="details"
                        label={`查看发行版详情：${rel.dataset_release_id}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          openRelease(bm, rel);
                        }}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        /* O(1) Legacy Sample Browser with versioned multimodal media matcher */
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-[var(--text-secondary)] font-medium">2026-11-20 历史目录：</span>
            <select
              aria-label="选择历史基准目录"
              value={activeLegacyDir}
              disabled={legacyDirOptions.length === 0}
              onChange={(e) => {
                setLegacyDir(e.target.value);
                setLegacyPage(1);
              }}
              className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs text-[var(--text-primary)] cursor-pointer disabled:opacity-50"
            >
              {legacyDirOptions.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
            {legacyListQ.data && (
              <span className="text-[var(--text-muted)] tabular-nums">
                已索引 {legacyListQ.data.total.toLocaleString()} 条字节偏移样本（第 {legacyPage} 页）
              </span>
            )}
          </div>

          {legacyDirsQ.isSuccess && legacyDirOptions.length === 0 ? (
            <StateNoticeCard kind="forbidden" title="当前账号无已授权的历史样本目录" description="2026-11-20 历史样本按来源清册的项目权限裁剪，请联系管理员授权对应项目。" />
          ) : legacyListQ.isLoading ? (
            <TableSkeleton rows={8} cols={5} />
          ) : legacyListQ.isError ? (
            <StateNoticeCard kind="error" title="历史样本读取失败" description={(legacyListQ.error as Error).message} onRetry={() => legacyListQ.refetch()} />
          ) : (legacyListQ.data?.items || []).length === 0 ? (
            <StateNoticeCard kind="empty" title="该目录暂无字节偏移索引记录" description="请先在系统健康面板执行冷层索引扫描。" />
          ) : (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
              <table className="w-full text-left border-collapse text-xs">
                <thead>
                  <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                    <th className="py-2.5 px-3.5">模型结果文件</th>
                    <th className="py-2.5 px-3">Sample ID</th>
                    <th className="py-2.5 px-3">状态 / Reward</th>
                    <th className="py-2.5 px-3">多模态图像匹配</th>
                    <th className="w-12 py-2.5 px-2 text-center">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-subtle)]">
                  {legacyListQ.data!.items.map((row) => (
                    <tr
                      key={`${row.model_file}-${row.sample_id}`}
                      onClick={() => openLegacySample(row)}
                      className="group/row-action hover:bg-[var(--bg-elevated)]/65 cursor-pointer"
                    >
                      <td className="py-2 px-3.5 font-mono text-[11px] text-[var(--text-secondary)]">{row.model_file}</td>
                      <td className="py-2 px-3 font-mono font-semibold text-[var(--text-primary)]">{row.sample_id}</td>
                      <td className="py-2 px-3 tabular-nums">
                        {row.status} · {row.reward ?? '—'}
                      </td>
                      <td className="py-2 px-3">
                        {(() => {
                          const verifiedImgs = getVerifiedImageRefs(row.media_match_status, row.media_refs || []);
                          if (row.media_match_status === 'ambiguous_multi_hit') {
                            return (
                              <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]">
                                <AlertTriangle className="w-3 h-3" /> 多版本冲突（已拒绝猜测）
                              </span>
                            );
                          }
                          if (row.media_match_status === 'unregistered_source') {
                            return (
                              <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)]">
                                未登记数据源（已降级占位）
                              </span>
                            );
                          }
                          if (row.media_match_status === 'missing') {
                            return (
                              <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] bg-[var(--bg-elevated)] text-[var(--text-muted)]">
                                媒体文件缺失
                              </span>
                            );
                          }
                          if (verifiedImgs.length > 0) {
                            return (
                              <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]">
                                <ImageIcon className="w-3 h-3" /> {verifiedImgs.length} 张已核验图像
                              </span>
                            );
                          }
                          return <span className="text-[var(--text-muted)]">{row.media_match_status || '无图像'}</span>;
                        })()}
                      </td>
                      <td className="px-2 text-center">
                        <RowActionIcon
                          variant="details"
                          label={`秒开详情：${row.sample_id}`}
                          onClick={(event) => {
                            event.stopPropagation();
                            openLegacySample(row);
                          }}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="px-4 py-2.5 border-t border-[var(--border-subtle)] flex items-center justify-between text-xs">
                <button
                  type="button"
                  disabled={legacyPage <= 1}
                  onClick={() => setLegacyPage((p) => Math.max(1, p - 1))}
                  className="rounded border border-[var(--border-subtle)] px-2.5 py-1 disabled:opacity-40 cursor-pointer"
                >
                  上一页
                </button>
                <span className="tabular-nums text-[var(--text-muted)]">第 {legacyPage} 页</span>
                <button
                  type="button"
                  disabled={legacyPage * 30 >= (legacyListQ.data?.total || 0)}
                  onClick={() => setLegacyPage((p) => p + 1)}
                  className="rounded border border-[var(--border-subtle)] px-2.5 py-1 disabled:opacity-40 cursor-pointer"
                >
                  下一页
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* L2 Drawer: Dataset Release Task List Preview */}
      <SlideOverDrawer
        open={Boolean(drawerRelease)}
        onClose={() => setDrawerRelease(null)}
        title={drawerRelease ? `${drawerRelease.bm.display_name} · ${drawerRelease.rel.dataset_release_id}` : ''}
        subtitle={
          drawerRelease
            ? `${drawerRelease.rel.runner} · split=${drawerRelease.rel.split} · protocol=${drawerRelease.rel.default_protocol}`
            : undefined
        }
        onOpenFullPage={drawerRelease ? () => navigate(`/benchmarks/${drawerRelease.bm.benchmark_slug}`) : undefined}
      >
        {drawerTasksQ.isLoading ? (
          <TableSkeleton rows={6} cols={3} />
        ) : drawerTasksQ.data?.redacted && (drawerTasksQ.data.items || []).length === 0 ? (
          <div className="rounded-xl border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] p-4 text-xs flex items-start gap-2.5">
            <Lock className="w-4 h-4 text-[var(--sem-zero-fg)] shrink-0 mt-0.5" />
            <div>
              <div className="font-semibold text-[var(--text-primary)]">Holdout 题面受保护</div>
              <div className="mt-1 text-[var(--text-secondary)]">
                {drawerTasksQ.data.redacted_reason}（共 {drawerTasksQ.data.total} 题）。
              </div>
            </div>
          </div>
        ) : (drawerTasksQ.data?.items || []).length === 0 ? (
          <StateNoticeCard kind="empty" title="该发行版暂未索引单题 task_definitions" description="历史统计或汇总型数据集可切换至「2026-11-20 样本 O(1) 浏览」查阅原题。" />
        ) : (
          <div className="space-y-3 text-xs">
            {drawerTasksQ.data?.redacted && (
              <div className="rounded-xl border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] p-3 text-xs flex items-start gap-2">
                <Lock className="w-3.5 h-3.5 text-[var(--sem-zero-fg)] shrink-0 mt-0.5" />
                <span className="text-[var(--text-secondary)]">{drawerTasksQ.data.redacted_reason}</span>
              </div>
            )}
            <div className="text-[var(--text-secondary)] tabular-nums">
              已索引可见题目共 {drawerTasksQ.data!.total} 道（点击进入题目详情页）
            </div>
            <div className="divide-y divide-[var(--border-subtle)] rounded-xl border border-[var(--border-subtle)]">
              {drawerTasksQ.data!.items.map((t) => (
                <Link
                  key={t.task_id}
                  to={`/datasets/${encodeURIComponent(t.dataset_release_id)}/tasks/${encodeURIComponent(t.task_id)}`}
                  className="flex items-center justify-between gap-2 px-3.5 py-2.5 hover:bg-[var(--bg-elevated)] no-underline text-[var(--text-primary)]"
                >
                  <span className="font-mono truncate">{t.task_id}</span>
                  <span className="text-[11px] text-[var(--text-muted)] shrink-0">
                    {t.category || '—'} · {t.difficulty || '—'}
                  </span>
                </Link>
              ))}
            </div>
          </div>
        )}
      </SlideOverDrawer>

      {/* L2 Drawer: O(1) Legacy Sample Detail & Authenticated Media */}
      <SlideOverDrawer
        open={Boolean(selectedLegacySample)}
        onClose={() => setSelectedLegacySample(null)}
        wide
        title={selectedLegacySample ? `${selectedLegacySample.dir} / ${selectedLegacySample.sampleId}` : ''}
        subtitle={selectedLegacySample?.modelFile}
      >
        {legacyDetailQ.isLoading ? (
          <TableSkeleton rows={6} cols={2} />
        ) : legacyDetailQ.isError ? (
          <StateNoticeCard kind="forbidden" title="无法查看样本正文" description={(legacyDetailQ.error as Error).message} />
        ) : legacyDetailQ.data ? (
          (() => {
            const verifiedImgs = getVerifiedImageRefs(
              legacyDetailQ.data.media_match_status,
              legacyDetailQ.data.media_refs || []
            );
            return (
              <div className="space-y-4 text-xs">
                {legacyDetailQ.data.media_match_status === 'ambiguous_multi_hit' && (
                  <div className="rounded-lg border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] px-3 py-2 text-[var(--sem-zero-fg)]">
                    检测到多套题库图像目录同时命中该样本 ID（ambiguous_multi_hit），按安全口径拒绝自动猜测展示，以免错配原图。
                  </div>
                )}
                {legacyDetailQ.data.media_match_status === 'unregistered_source' && (
                  <div className="rounded-lg border border-[var(--sem-unscored-border)] bg-[var(--sem-unscored-bg)] px-3 py-2 text-[var(--sem-unscored-fg)]">
                    未登记媒体数据源（unregistered_source）：该基准尚未在 media_matching_rules.yaml 登记授权图像目录，已降级占位展示（零 404 破图）。
                  </div>
                )}
                {legacyDetailQ.data.media_match_status === 'missing' && (
                  <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-[var(--text-secondary)]">
                    该样本未命中本地登记图像文件（missing），以下保留文本问答与评分记录。
                  </div>
                )}
                {verifiedImgs.length > 0 && (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {verifiedImgs.map((m, idx) => (
                      <div key={idx} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-2">
                        <img
                          src={m.url}
                          alt={`sample-${idx}`}
                          className="max-h-72 w-full object-contain rounded"
                        />
                        <div className="mt-1 text-[10px] font-mono text-[var(--text-muted)] truncate">
                          {m.label}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                <SampleJsonPreview sample={legacyDetailQ.data.sample} />
              </div>
            );
          })()
        ) : null}
      </SlideOverDrawer>
    </PageTransition>
  );
}

function SampleJsonPreview({ sample }: { sample: unknown }) {
  const dark = useAppStore((state) => state.theme === 'dark');
  const lines = useMemo(() => (JSON.stringify(sample, null, 2) ?? '').split('\n'), [sample]);
  const colors = dark
    ? { property: '#9cdcfe', string: '#ce9178', number: '#b5cea8', literal: '#c4a7ff' }
    : { property: '#005787', string: '#9d301a', number: '#087045', literal: '#7446a8' };

  return (
    <section aria-label="样本 JSON" className="overflow-hidden rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">
      <div className="border-b border-[var(--border-subtle)] px-3.5 py-2 font-mono text-[11px] text-[var(--text-secondary)]">JSON</div>
      <pre className="m-0 max-h-[60vh] overflow-auto py-2 font-mono text-xs leading-relaxed">
        <code>
          {lines.map((line, index) => {
            // Split complete JSON tokens so escape sequences and HTML-like text stay intact.
            const tokens = line.split(/("(?:\\.|[^"\\])*"(?=\s*:)|"(?:\\.|[^"\\])*"|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/);
            return (
              <React.Fragment key={index}>
                <span className="flex">
                  <span aria-hidden="true" data-line={index + 1} className="w-12 shrink-0 select-none border-r border-[var(--border-subtle)] pr-2 text-right tabular-nums text-[var(--text-muted)] before:content-[attr(data-line)]" />
                  <span className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere] px-3">
                    {tokens.map((token, tokenIndex) => {
                      if (tokenIndex % 2 === 0) return token;
                      const kind = token.startsWith('"')
                        ? tokens[tokenIndex + 1]?.trimStart().startsWith(':') ? 'property' : 'string'
                        : /^[a-z]/.test(token) ? 'literal' : 'number';
                      return <span key={tokenIndex} data-json-token={kind} style={{ color: colors[kind] }}>{token}</span>;
                    })}
                  </span>
                </span>
                {index < lines.length - 1 && '\n'}
              </React.Fragment>
            );
          })}
        </code>
      </pre>
    </section>
  );
}
