import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import type * as echarts from 'echarts';
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Cpu,
  GitBranch,
  Info,
  Lock,
  Network,
  Play,
  RefreshCw,
  ServerOff,
  ShieldAlert,
  ShieldCheck,
  Terminal,
  TrendingDown,
  TrendingUp,
} from 'lucide-react';
import { apiFetch } from '../lib/api';
import { CHART_PALETTE, escapeHtml, fmtDeltaPP, fmtPct, fmtTokens, stableColorForKey } from '../lib/format';
import { useAppStore } from '../lib/store';
import { PageTransition } from '../components/motion/primitives';
import { EChartBox, glassTooltipConfig } from '../components/charts/EChartBox';
import { RowActionIcon } from '../components/ui/RowActionIcon';
import { CardGridSkeleton, ChartSkeleton, StateNoticeCard } from '../components/ui/Skeletons';

interface TrainingRunSummary {
  run_id: string;
  source_path_redacted: string;
  student_model: string | null;
  checkpoint_count: number;
  steps_completed: number;
  latest_mean_reverse_kl: number | null;
  latest_mean_reward: number | null;
  total_routed_tasks: number;
  expert_count: number;
}

interface RegisteredTrainDataset {
  dataset_alias: string;
  display_name: string;
  split: string;
}

interface EmptyStateGuide {
  title: string;
  honesty_notice: string;
  prerequisites: string[];
  gpu_notice: string;
}

interface TrainingRunsListResp {
  methodology_version: string;
  training_runs_dir_redacted: string;
  is_empty_production_state: boolean;
  items: TrainingRunSummary[];
  total: number;
  registered_train_datasets: RegisteredTrainDataset[];
  empty_state_guide: EmptyStateGuide;
}

interface RoutingTopologyNode {
  name: string;
  label: string;
  stage: string;
}

interface RoutingTopologyLink {
  source: string;
  target: string;
  value: number;
}

interface ExpertSummary {
  expert: string;
  model: string;
  task_count: number;
  datasets: string[];
  environments: string[];
  sources: string[];
}

interface RoutingTopology {
  total_routed_tasks: number;
  nodes: RoutingTopologyNode[];
  links: RoutingTopologyLink[];
  experts: ExpertSummary[];
}

interface StepExpertBreakdown {
  mean_reverse_kl: number | null;
  mean_reward: number | null;
  tokens: number;
  rollouts: number;
}

interface RolloutEvidence {
  dataset: string;
  task: string;
  expert: string;
  reward: number | null;
  tokens: number;
  sampled_reverse_kl: number | null;
}

interface StepSeriesPoint {
  step: number;
  checkpoint: string;
  rollout_checkpoint: string;
  mean_reverse_kl: number | null;
  mean_reward: number | null;
  total_tokens: number;
  rollout_count: number;
  by_expert: Record<string, StepExpertBreakdown>;
  rollouts: RolloutEvidence[];
}

interface ExpertStepPoint {
  step: number;
  checkpoint: string;
  mean_reverse_kl: number | null;
  mean_reward: number | null;
  tokens: number;
}

interface HoldoutComparisonItem {
  dataset_release_id: string;
  scoring_protocol: string;
  base_checkpoint: string;
  base_batch_id: string;
  base_score_pct: number;
  candidate_checkpoint: string;
  candidate_batch_id: string;
  candidate_score_pct: number;
  delta_pp: number;
  status: string;
}

interface HoldoutComparison {
  status: string;
  reason: string | null;
  items: HoldoutComparisonItem[];
}

interface TrainingRunDetailResp {
  methodology_version: string;
  run_id: string;
  source_path_redacted: string;
  student_model: string | null;
  checkpoint_count: number;
  checkpoints: string[];
  steps_completed: number;
  latest_mean_reverse_kl: number | null;
  latest_mean_reward: number | null;
  total_routed_tasks: number;
  expert_count: number;
  routing_topology: RoutingTopology;
  step_series: StepSeriesPoint[];
  expert_series: Record<string, ExpertStepPoint[]>;
  holdout_comparison: HoldoutComparison;
}

interface TrainingPlanPayload {
  backend: 'opd' | 'trl';
  datasets: string[];
  student: string;
  run_name: string;
  steps: number;
  rollouts_per_step?: number | null;
  seed: number;
}

interface TrainingPlanResp {
  ok: boolean;
  backend: string;
  gpu_host_required: boolean;
  plan_command: string;
  train_command: string;
  expected_artifacts: string[];
}

const STAGE_DEPTH: Record<string, number> = {
  dataset: 0,
  environment_source: 1,
  expert: 2,
  expert_model: 3,
};

const STAGE_LABEL_ZH: Record<string, string> = {
  dataset: 'Dataset',
  environment_source: 'Environment / Source',
  expert: 'Expert',
  expert_model: 'Expert Model',
};

const STAGE_COLOR: Record<string, string> = {
  dataset: '#2563eb',
  environment_source: '#0891b2',
  expert: '#7c3aed',
  expert_model: '#059669',
};

function fmtKl(val: number | null | undefined): string {
  if (val === null || val === undefined || !Number.isFinite(val)) return '—';
  return val.toFixed(4);
}

function fmtReward(val: number | null | undefined): string {
  if (val === null || val === undefined || !Number.isFinite(val)) return '—';
  return val.toFixed(4);
}

function CopyableCommandBox({
  label,
  badge,
  command,
  onCopy,
}: {
  label: string;
  badge?: string;
  command: string;
  onCopy: (text: string, label: string) => void;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    onCopy(command, label);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };

  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-1.5 border-b border-[var(--border-subtle)] bg-[var(--bg-surface)]/70">
        <div className="flex items-center gap-2 text-[11px] font-semibold text-[var(--text-primary)]">
          <span>{label}</span>
          {badge && (
            <span className="rounded-full px-2 py-0.5 text-[10px] font-medium bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)] border border-[var(--sem-zero-border)]">
              {badge}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={handleCopy}
          aria-label={`复制 ${label}`}
          className="inline-flex items-center gap-1 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-surface)] hover:border-[var(--brand-primary)] px-2 py-0.5 text-[11px] font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
        >
          {copied ? <Check className="w-3 h-3 text-[var(--sem-pass-fg)]" /> : <Copy className="w-3 h-3" />}
          <span>{copied ? '已复制' : '复制命令'}</span>
        </button>
      </div>
      <pre className="m-0 p-3 font-mono text-[11px] leading-relaxed text-[var(--text-primary)] overflow-x-auto whitespace-pre-wrap break-all">
        {command}
      </pre>
    </div>
  );
}

function TrainingCliGenerator({
  datasets,
  canWrite,
  gpuNotice,
}: {
  datasets: RegisteredTrainDataset[];
  canWrite: boolean;
  gpuNotice: string;
}) {
  const pushToast = useAppStore((s) => s.pushToast);

  // Strictly exclude any -holdout datasets per framework/training_opd.py:97
  const trainOnlyDatasets = useMemo(
    () =>
      datasets.filter(
        (d) =>
          !d.dataset_alias.toLowerCase().endsWith('-holdout') &&
          !d.dataset_alias.toLowerCase().includes('holdout') &&
          d.split === 'train'
      ),
    [datasets]
  );

  const [backend, setBackend] = useState<'opd' | 'trl'>('opd');
  const [selectedDatasets, setSelectedDatasets] = useState<string[]>(['tianxi-synth-v1-train']);
  const [student, setStudent] = useState('Qwen3_8-27B');
  const [runName, setRunName] = useState('opd-run-01');
  const [steps, setSteps] = useState(2);
  const [rolloutsPerStep, setRolloutsPerStep] = useState('');
  const [seed, setSeed] = useState(42);
  const [planResult, setPlanResult] = useState<TrainingPlanResp | null>(null);

  useEffect(() => {
    if (trainOnlyDatasets.length === 0) return;
    const validAliases = new Set(trainOnlyDatasets.map((d) => d.dataset_alias));
    setSelectedDatasets((prev) => {
      const kept = prev.filter((alias) => validAliases.has(alias));
      if (kept.length === prev.length && kept.length > 0) return prev;
      if (kept.length > 0) return kept;
      const preferred =
        trainOnlyDatasets.find((d) => d.dataset_alias === 'tianxi-synth-v1-train')?.dataset_alias ||
        trainOnlyDatasets[0].dataset_alias;
      return [preferred];
    });
  }, [trainOnlyDatasets]);

  const toggleDataset = (alias: string) => {
    setSelectedDatasets((prev) =>
      prev.includes(alias) ? prev.filter((item) => item !== alias) : [...prev, alias]
    );
  };

  const planMut = useMutation({
    mutationFn: (payload: TrainingPlanPayload) =>
      apiFetch<TrainingPlanResp>('/api/v1/training/plan', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    onSuccess: (res) => {
      setPlanResult(res);
      pushToast('success', `已生成 ${res.backend.toUpperCase()} --plan 预检与训练机命令`);
    },
    onError: (err: Error) => {
      pushToast('error', `命令生成失败：${err.message}`);
    },
  });

  const handleGenerate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canWrite) return;
    const parsedRollouts = rolloutsPerStep.trim() ? Number(rolloutsPerStep.trim()) : null;
    planMut.mutate({
      backend,
      datasets: selectedDatasets.length > 0 ? selectedDatasets : ['tianxi-synth-v1-train'],
      student: student.trim() || 'Qwen3_8-27B',
      run_name: runName.trim() || 'opd-run-01',
      steps: Math.max(1, Math.min(10000, Math.round(steps) || 1)),
      rollouts_per_step:
        parsedRollouts !== null && Number.isFinite(parsedRollouts) && parsedRollouts >= 1
          ? Math.round(parsedRollouts)
          : null,
      seed: Number.isFinite(seed) ? Math.round(seed) : 42,
    });
  };

  const copyCommand = (text: string, label: string) => {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      navigator.clipboard
        .writeText(text)
        .then(() => pushToast('success', `已复制 ${label}`))
        .catch(() => pushToast('warning', '浏览器剪贴板受限，请手动选中复制'));
    } else {
      pushToast('warning', '当前环境不支持自动剪贴板，请手动选中复制');
    }
  };

  const displayDatasets: RegisteredTrainDataset[] =
    trainOnlyDatasets.length > 0
      ? trainOnlyDatasets
      : [{ dataset_alias: 'tianxi-synth-v1-train', display_name: 'tianxi-synth-v1-train', split: 'train' }];

  return (
    <form onSubmit={handleGenerate} className="space-y-4 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 font-bold text-sm text-[var(--text-primary)]">
          <Terminal className="w-4 h-4 text-[var(--brand-primary)]" />
          <span>train.py --backend opd --plan 预检命令生成器</span>
        </div>
        <span className="inline-flex items-center gap-1 rounded-full border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] px-2.5 py-0.5 text-[11px] font-medium text-[var(--sem-zero-fg)]">
          <ServerOff className="w-3.5 h-3.5 shrink-0" />
          <span>仅生成 CLI 命令 · 不在 197 本地启动训练</span>
        </span>
      </div>

      <p className="m-0 text-[11px] text-[var(--text-secondary)] leading-relaxed">{gpuNotice}</p>

      {/* Backend selector */}
      <div className="space-y-1.5">
        <span className="block text-[11px] font-medium text-[var(--text-secondary)]">训练后端 (backend)</span>
        <div className="inline-flex rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-0.5" role="radiogroup" aria-label="训练后端选择">
          {(['opd', 'trl'] as const).map((opt) => {
            const active = backend === opt;
            return (
              <button
                key={opt}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => setBackend(opt)}
                className={`rounded-md px-3 py-1 font-mono text-xs font-semibold cursor-pointer transition-colors ${
                  active
                    ? 'bg-[var(--brand-primary)] text-white'
                    : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
                }`}
              >
                --backend {opt}
              </button>
            );
          })}
        </div>
      </div>

      {/* Registered train datasets multi-select */}
      <div className="space-y-1.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[11px] font-medium text-[var(--text-secondary)]">
            已注册训练集（多选 · 已按 training_opd.py:97 自动排除 -holdout）
          </span>
          <span className="text-[11px] font-mono text-[var(--text-muted)]">
            已选 {selectedDatasets.length} 个
          </span>
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="训练集多选">
          {displayDatasets.map((ds) => {
            const selected = selectedDatasets.includes(ds.dataset_alias);
            return (
              <button
                key={ds.dataset_alias}
                type="button"
                aria-pressed={selected}
                onClick={() => toggleDataset(ds.dataset_alias)}
                className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-mono border cursor-pointer transition-colors ${
                  selected
                    ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                    : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)] hover:border-[var(--border-strong)]'
                }`}
              >
                {selected && <Check className="w-3 h-3" />}
                <span>{ds.dataset_alias}</span>
                {ds.display_name && ds.display_name !== ds.dataset_alias && (
                  <span className="text-[10px] opacity-75">({ds.display_name})</span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      {/* Form parameters */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
        <label className="space-y-1 block">
          <span className="block text-[11px] text-[var(--text-secondary)]">Student 模型 (--student)</span>
          <input
            type="text"
            value={student}
            onChange={(e) => setStudent(e.target.value)}
            placeholder="Qwen3_8-27B"
            required
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)]"
          />
        </label>

        <label className="space-y-1 block">
          <span className="block text-[11px] text-[var(--text-secondary)]">运行名称 (run_name)</span>
          <input
            type="text"
            value={runName}
            onChange={(e) => setRunName(e.target.value)}
            placeholder="opd-run-01"
            required
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)]"
          />
        </label>

        <label className="space-y-1 block">
          <span className="block text-[11px] text-[var(--text-secondary)]">训练步数 (--steps)</span>
          <input
            type="number"
            min={1}
            max={10000}
            value={steps}
            onChange={(e) => setSteps(Number(e.target.value) || 1)}
            required
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)] tabular-nums"
          />
        </label>

        <label className="space-y-1 block">
          <span className="block text-[11px] text-[var(--text-secondary)]">每步采样数 (--rollouts-per-step)</span>
          <input
            type="number"
            min={1}
            max={1024}
            value={rolloutsPerStep}
            onChange={(e) => setRolloutsPerStep(e.target.value)}
            placeholder="留空=自动(等于专家数)"
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)] tabular-nums"
          />
        </label>

        <label className="space-y-1 block">
          <span className="block text-[11px] text-[var(--text-secondary)]">随机种子 (--seed)</span>
          <input
            type="number"
            value={seed}
            onChange={(e) => setSeed(Number(e.target.value) || 0)}
            required
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-primary)] tabular-nums"
          />
        </label>
      </div>

      {/* Submit row */}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={!canWrite || planMut.isPending || selectedDatasets.length === 0}
          className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] disabled:opacity-50 disabled:cursor-not-allowed px-3.5 py-1.5 text-xs font-semibold text-white cursor-pointer transition-colors"
        >
          <Play className="w-3.5 h-3.5" />
          <span>{planMut.isPending ? '正在生成命令...' : '生成 --plan 预检与训练 CLI'}</span>
        </button>

        {!canWrite && (
          <span className="inline-flex items-center gap-1.5 text-[11px] text-[var(--text-muted)]">
            <Lock className="w-3.5 h-3.5 text-[var(--sem-zero-fg)]" />
            <span>需 Researcher 或 Admin 权限调用 POST /api/v1/training/plan</span>
          </span>
        )}
      </div>

      {/* Inline validation / permission error */}
      {planMut.isError && (
        <div
          role="alert"
          className="rounded-lg border border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] px-3 py-2 text-[11px] text-[var(--sem-err-fg)]"
        >
          命令生成失败：{planMut.error instanceof Error ? planMut.error.message : String(planMut.error)}
        </div>
      )}

      {/* Generated output */}
      {planResult && (
        <div className="space-y-3 pt-2 border-t border-[var(--border-subtle)]">
          <CopyableCommandBox
            label="步骤 1 · 路由与参数预检命令 (--plan)"
            badge={planResult.backend.toUpperCase()}
            command={planResult.plan_command}
            onCopy={copyCommand}
          />
          <CopyableCommandBox
            label="步骤 2 · GPU 训练机执行命令"
            badge={planResult.gpu_host_required ? '需 GPU 训练机执行' : undefined}
            command={planResult.train_command}
            onCopy={copyCommand}
          />

          <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 space-y-1.5">
            <div className="text-[11px] font-semibold text-[var(--text-primary)]">
              预期落盘产物契约（发布至共享盘后 197 自动发现，无需重启服务）
            </div>
            <ul className="m-0 pl-4 space-y-1 font-mono text-[11px] text-[var(--text-secondary)]">
              {planResult.expected_artifacts.map((art) => (
                <li key={art}>{art}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </form>
  );
}

export function TrainingOpdPage() {
  const theme = useAppStore((s) => s.theme);
  const user = useAppStore((s) => s.user);
  const canWrite = Boolean(user && ['researcher', 'admin'].includes(user.role));

  const [selectedRunId, setSelectedRunId] = useState<string>('');
  const [showGlobalSeries, setShowGlobalSeries] = useState<boolean>(true);
  const [enabledExperts, setEnabledExperts] = useState<string[]>([]);
  const [expertMetricMode, setExpertMetricMode] = useState<'both' | 'kl' | 'reward'>('both');
  const [expandedStep, setExpandedStep] = useState<number | null>(null);
  const [cliPanelOpen, setCliPanelOpen] = useState<boolean>(false);

  const runsQ = useQuery({
    queryKey: ['training-opd-runs'],
    queryFn: () => apiFetch<TrainingRunsListResp>('/api/v1/training/runs'),
  });

  const runs = useMemo(() => runsQ.data?.items ?? [], [runsQ.data]);

  useEffect(() => {
    if (runs.length === 0) {
      if (selectedRunId) setSelectedRunId('');
      return;
    }
    if (!selectedRunId || !runs.some((r) => r.run_id === selectedRunId)) {
      setSelectedRunId(runs[0].run_id);
    }
  }, [runs, selectedRunId]);

  const detailQ = useQuery({
    queryKey: ['training-opd-run-detail', selectedRunId],
    enabled: Boolean(selectedRunId),
    queryFn: () => apiFetch<TrainingRunDetailResp>(`/api/v1/training/runs/${encodeURIComponent(selectedRunId)}`),
  });

  const detail = detailQ.data;
  const expertNames = useMemo(() => Object.keys(detail?.expert_series || {}).sort(), [detail]);

  // Reset per-run view states cleanly when switching runs
  useEffect(() => {
    setEnabledExperts([]);
    setExpandedStep(null);
  }, [selectedRunId]);

  const expertColorMap = useMemo(() => {
    const map = new Map<string, string>();
    expertNames.forEach((name, idx) => {
      map.set(name, CHART_PALETTE[idx % CHART_PALETTE.length]);
    });
    (detail?.routing_topology?.experts || []).forEach((exp) => {
      if (!map.has(exp.expert)) {
        map.set(exp.expert, stableColorForKey(exp.expert));
      }
    });
    return map;
  }, [expertNames, detail]);

  const toggleExpert = (exp: string) => {
    setEnabledExperts((prev) => (prev.includes(exp) ? prev.filter((x) => x !== exp) : [...prev, exp]));
  };

  // Multi-Expert Routing Topology Sankey Option
  const sankeyOption = useMemo<echarts.EChartsCoreOption>(() => {
    const topology = detail?.routing_topology;
    if (!topology || topology.nodes.length === 0) return {};

    const labelById = new Map<string, string>();
    const stageById = new Map<string, string>();
    topology.nodes.forEach((n) => {
      labelById.set(n.name, n.label);
      stageById.set(n.name, n.stage);
    });

    const sankeyNodes = topology.nodes.map((n) => {
      let color = STAGE_COLOR[n.stage] || '#64748b';
      if (n.stage === 'expert') {
        const expKey = n.name.startsWith('exp:') ? n.name.slice(4) : n.label;
        color = expertColorMap.get(expKey) || stableColorForKey(expKey);
      }
      return {
        name: n.name,
        depth: STAGE_DEPTH[n.stage] ?? 0,
        itemStyle: {
          color,
          borderColor: theme === 'dark' ? '#18181b' : '#ffffff',
          borderWidth: 1,
        },
      };
    });

    return {
      tooltip: {
        ...glassTooltipConfig(theme),
        trigger: 'item',
        triggerOn: 'mousemove',
        formatter: (params: unknown) => {
          const p = params as {
            dataType?: string;
            name?: string;
            value?: number;
            data?: { source?: string; target?: string; value?: number };
          };
          if (p.dataType === 'edge' && p.data) {
            const src = labelById.get(p.data.source || '') || p.data.source || '';
            const tgt = labelById.get(p.data.target || '') || p.data.target || '';
            return `<div><strong>${escapeHtml(src)}</strong> → <strong>${escapeHtml(tgt)}</strong><br/>路由任务数：<b>${p.data.value ?? 0}</b></div>`;
          }
          const nid = p.name || '';
          const lbl = labelById.get(nid) || nid;
          const stg = STAGE_LABEL_ZH[stageById.get(nid) || ''] || stageById.get(nid) || '';
          return `<div><div style="opacity:0.75;font-size:11px">${escapeHtml(stg)}</div><strong>${escapeHtml(lbl)}</strong><br/>经过任务数：<b>${p.value ?? 0}</b></div>`;
        },
      },
      series: [
        {
          type: 'sankey',
          left: 16,
          right: 170,
          top: 16,
          bottom: 16,
          nodeWidth: 16,
          nodeGap: 14,
          draggable: false,
          layoutIterations: 0,
          emphasis: { focus: 'adjacency' },
          data: sankeyNodes,
          links: topology.links,
          lineStyle: {
            color: 'gradient',
            curveness: 0.45,
            opacity: theme === 'dark' ? 0.42 : 0.35,
          },
          label: {
            color: theme === 'dark' ? '#e4e4e7' : '#0f172a',
            fontSize: 11,
            fontFamily: '"JetBrains Mono", "Inter", sans-serif',
            formatter: (params: unknown) => {
              const p = params as { name?: string };
              const raw = p.name || '';
              return labelById.get(raw) || raw;
            },
          },
        },
      ],
    };
  }, [detail, theme, expertColorMap]);

  // Dual-Axis Step Trend Chart Option (Left Y: sampled_reverse_kl, Right Y: Harbor reward)
  const stepTrendOption = useMemo<echarts.EChartsCoreOption>(() => {
    const steps = detail?.step_series || [];
    if (steps.length === 0) return {};

    const xLabels = steps.map((s) => `Step ${s.step}`);
    const stepByIdx = steps;

    const allRewards: number[] = [];
    steps.forEach((s) => {
      if (s.mean_reward !== null) allRewards.push(s.mean_reward);
      Object.values(s.by_expert || {}).forEach((ex) => {
        if (ex.mean_reward !== null) allRewards.push(ex.mean_reward);
      });
    });
    const maxRewardObserved = allRewards.length > 0 ? Math.max(...allRewards) : 1;
    const rewardYAxisMax = maxRewardObserved <= 1.0 ? 1.0 : undefined;

    const series: Array<Record<string, unknown>> = [];

    if (showGlobalSeries) {
      series.push({
        name: '全局 Reverse KL ↓',
        type: 'line',
        yAxisIndex: 0,
        smooth: true,
        symbol: 'circle',
        symbolSize: 7,
        lineStyle: { width: 3, color: '#ef4444' },
        itemStyle: { color: '#ef4444' },
        data: steps.map((s) => s.mean_reverse_kl),
      });
      series.push({
        name: '全局 Harbor Reward ↑',
        type: 'line',
        yAxisIndex: 1,
        smooth: true,
        symbol: 'diamond',
        symbolSize: 8,
        lineStyle: { width: 3, color: '#10b981' },
        itemStyle: { color: '#10b981' },
        data: steps.map((s) => s.mean_reward),
      });
    }

    enabledExperts.forEach((exp) => {
      const color = expertColorMap.get(exp) || stableColorForKey(exp);
      if (expertMetricMode === 'both' || expertMetricMode === 'kl') {
        series.push({
          name: `${exp} · KL`,
          type: 'line',
          yAxisIndex: 0,
          smooth: true,
          symbol: 'emptyCircle',
          symbolSize: 5,
          lineStyle: { width: 1.75, type: 'dashed', color },
          itemStyle: { color },
          data: steps.map((s) => s.by_expert?.[exp]?.mean_reverse_kl ?? null),
        });
      }
      if (expertMetricMode === 'both' || expertMetricMode === 'reward') {
        series.push({
          name: `${exp} · Reward`,
          type: 'line',
          yAxisIndex: 1,
          smooth: true,
          symbol: 'rect',
          symbolSize: 5,
          lineStyle: { width: 1.75, type: 'dotted', color },
          itemStyle: { color },
          data: steps.map((s) => s.by_expert?.[exp]?.mean_reward ?? null),
        });
      }
    });

    const axisText = theme === 'dark' ? '#d4d4d8' : '#334155';
    const gridLine = theme === 'dark' ? '#27272a' : '#e5e7eb';

    return {
      grid: { left: 60, right: 60, top: 44, bottom: 38 },
      legend: {
        top: 0,
        type: 'scroll',
        textStyle: { color: axisText, fontSize: 11 },
      },
      tooltip: {
        ...glassTooltipConfig(theme),
        trigger: 'axis',
        formatter: (params: unknown) => {
          const arr = Array.isArray(params) ? params : [params];
          const first = arr[0] as { dataIndex?: number } | undefined;
          const idx = first?.dataIndex ?? 0;
          const pt = stepByIdx[idx];
          if (!pt) return '';
          const expLines = Object.entries(pt.by_expert || {})
            .sort(([a], [b]) => a.localeCompare(b))
            .map(
              ([exp, v]) =>
                `<div style="margin-top:2px"><span style="font-family:monospace">${escapeHtml(exp)}</span>: KL <b>${fmtKl(v.mean_reverse_kl)}</b> · Reward <b>${fmtReward(v.mean_reward)}</b> · ${fmtTokens(v.tokens)} tok</div>`
            )
            .join('');
          return `<div>
            <div style="font-weight:700;margin-bottom:4px">Step ${pt.step} · ${escapeHtml(pt.checkpoint)}</div>
            <div style="font-size:11px;opacity:0.8">Rollout 来源: <code>${escapeHtml(pt.rollout_checkpoint)}</code> · Rollouts: ${pt.rollout_count} · Tokens: ${fmtTokens(pt.total_tokens)}</div>
            <div style="margin-top:4px">全局 Reverse KL: <b style="color:#ef4444">${fmtKl(pt.mean_reverse_kl)}</b> · 全局 Reward: <b style="color:#10b981">${fmtReward(pt.mean_reward)}</b></div>
            ${expLines ? `<div style="margin-top:6px;padding-top:4px;border-top:1px solid rgba(148,163,184,0.25);font-size:11px">${expLines}</div>` : ''}
          </div>`;
        },
      },
      xAxis: {
        type: 'category',
        data: xLabels,
        axisLabel: { color: axisText, fontSize: 11 },
        axisLine: { lineStyle: { color: gridLine } },
      },
      yAxis: [
        {
          type: 'value',
          name: 'sampled_reverse_kl ↓',
          position: 'left',
          min: 0,
          nameTextStyle: { color: '#ef4444', fontSize: 11, fontWeight: 600 },
          axisLabel: { color: axisText, fontSize: 11 },
          splitLine: { lineStyle: { color: gridLine } },
        },
        {
          type: 'value',
          name: 'Harbor reward ↑',
          position: 'right',
          min: 0,
          max: rewardYAxisMax,
          nameTextStyle: { color: '#10b981', fontSize: 11, fontWeight: 600 },
          axisLabel: { color: axisText, fontSize: 11 },
          splitLine: { show: false },
        },
      ],
      series,
    };
  }, [detail, theme, showGlobalSeries, enabledExperts, expertMetricMode, expertColorMap]);

  const firstStep = detail?.step_series?.[0] ?? null;
  const latestStep = detail?.step_series?.length ? detail.step_series[detail.step_series.length - 1] : null;
  const klDelta =
    firstStep?.mean_reverse_kl !== null &&
    firstStep?.mean_reverse_kl !== undefined &&
    latestStep?.mean_reverse_kl !== null &&
    latestStep?.mean_reverse_kl !== undefined &&
    detail &&
    detail.step_series.length >= 2
      ? latestStep.mean_reverse_kl - firstStep.mean_reverse_kl
      : null;
  const rewardDelta =
    firstStep?.mean_reward !== null &&
    firstStep?.mean_reward !== undefined &&
    latestStep?.mean_reward !== null &&
    latestStep?.mean_reward !== undefined &&
    detail &&
    detail.step_series.length >= 2
      ? latestStep.mean_reward - firstStep.mean_reward
      : null;

  const emptyGuide = runsQ.data?.empty_state_guide;
  const fallbackPrerequisites = [
    '1. 在带 GPU 的训练机上基于 1 个已通过 report.py --check-training-captures 的 harbor-runs/ 采集批次，运行 1 次最小步数（如 1–2 steps）的 training_opd.py；',
    '2. 将产出的 routing.json 与至少 1 个 checkpoint-XXXX/opd.json 发布到 /home/share-data/tianxi_benches/training-runs/<run_id>/；',
    '3. 197 平台自动发现该目录并即时渲染多专家路由拓扑图与步级双轴趋势曲线（无需重启服务）。',
  ];
  const displayedPrerequisites =
    emptyGuide?.prerequisites && emptyGuide.prerequisites.length > 0
      ? emptyGuide.prerequisites
      : fallbackPrerequisites;

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <Network className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>Training &amp; 多专家 OPD 监控</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            对接 framework/training_opd.py 产物契约（routing.json + checkpoint-XXXX/opd.json）· 实时扫描{' '}
            <code className="font-mono text-[11px]">
              {runsQ.data?.training_runs_dir_redacted || '<TIANXI_SHARED>/training-runs'}
            </code>
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {runs.length > 0 && (
            <select
              aria-label="选择 OPD 训练运行"
              value={selectedRunId}
              onChange={(e) => setSelectedRunId(e.target.value)}
              className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs font-mono text-[var(--text-primary)]"
            >
              {runs.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {r.run_id} · {r.student_model || 'unknown'} ({r.steps_completed} steps / {r.expert_count} experts)
                </option>
              ))}
            </select>
          )}

          <button
            type="button"
            onClick={() => {
              runsQ.refetch();
              if (selectedRunId) detailQ.refetch();
            }}
            disabled={runsQ.isFetching || detailQ.isFetching}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] hover:border-[var(--border-strong)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] cursor-pointer disabled:opacity-60"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${runsQ.isFetching || detailQ.isFetching ? 'animate-spin' : ''}`} />
            <span>重新扫描目录</span>
          </button>

          <Link
            to="/synth/selection"
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] hover:border-[var(--brand-primary)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] no-underline"
          >
            RL/SFT 选题板 →
          </Link>
        </div>
      </div>

      {/* Main Content */}
      {runsQ.isLoading ? (
        <div className="space-y-4">
          <CardGridSkeleton count={4} />
          <ChartSkeleton height={340} />
        </div>
      ) : runsQ.isError ? (
        <StateNoticeCard
          kind="error"
          title="OPD 训练运行目录加载失败"
          description={(runsQ.error as Error).message}
          onRetry={() => runsQ.refetch()}
        />
      ) : runs.length === 0 ? (
        /* ==================== Honest Production Empty-State View ==================== */
        <div className="space-y-5">
          {/* Transparent Empty-State Banner */}
          <section
            aria-label="当前 training-runs 目录空态声明"
            className="rounded-xl border border-[var(--sem-zero-border)] bg-[var(--bg-surface)] overflow-hidden"
          >
            <div className="px-5 py-4 bg-[var(--sem-zero-bg)] border-b border-[var(--sem-zero-border)] flex flex-wrap items-start justify-between gap-3">
              <div className="flex items-start gap-3">
                <AlertTriangle className="w-5 h-5 text-[var(--sem-zero-fg)] shrink-0 mt-0.5" />
                <div>
                  <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">
                    {emptyGuide?.title || '当前 training-runs/ 目录为空（0 次真实 OPD 训练产物）'}
                  </h2>
                  <p className="m-0 mt-1 text-xs text-[var(--text-secondary)] leading-relaxed">
                    {emptyGuide?.honesty_notice ||
                      '平台严禁将隔离测试夹具注入生产数据库或混入线上榜单；下方提供基于真实 framework/train.py 与 framework/training_opd.py 的预检命令生成器。'}
                  </p>
                </div>
              </div>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--sem-zero-border)] bg-[var(--bg-surface)] px-2.5 py-1 text-[11px] font-mono font-semibold text-[var(--sem-zero-fg)]">
                0 Real Runs · 零假数据污染
              </span>
            </div>

            <div className="p-5 space-y-4 text-xs">
              <div className="space-y-2">
                <div className="font-semibold text-[var(--text-primary)] flex items-center gap-1.5">
                  <Info className="w-4 h-4 text-[var(--brand-primary)]" />
                  <span>发布首份真实 OPD 训练产物所需前置条件</span>
                </div>
                <ol className="m-0 list-none p-0 grid grid-cols-1 md:grid-cols-3 gap-3">
                  {displayedPrerequisites.map((stepText, idx) => (
                    <li
                      key={idx}
                      className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 text-[var(--text-secondary)] leading-relaxed"
                    >
                      <div className="text-[11px] font-mono font-bold text-[var(--brand-primary)] mb-1">
                        STEP 0{idx + 1}
                      </div>
                      <div>{stepText}</div>
                    </li>
                  ))}
                </ol>
              </div>

              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3.5 py-2.5 flex items-center gap-2 text-[var(--text-secondary)]">
                <ServerOff className="w-4 h-4 text-[var(--sem-zero-fg)] shrink-0" />
                <span>
                  {emptyGuide?.gpu_notice ||
                    '197 (LRM-02) 无本地训练 GPU，本页表单仅生成 --plan 预检与训练机可执行 CLI 命令。'}
                </span>
              </div>
            </div>
          </section>

          {/* Embedded Interactive CLI Command Generator */}
          <section
            aria-label="OPD 预检与训练命令生成器"
            className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-5"
          >
            <TrainingCliGenerator
              datasets={runsQ.data?.registered_train_datasets || []}
              canWrite={canWrite}
              gpuNotice={
                emptyGuide?.gpu_notice ||
                '197 (LRM-02) 无本地训练 GPU，本页表单仅生成 --plan 预检与训练机可执行 CLI 命令。'
              }
            />
          </section>
        </div>
      ) : (
        /* ==================== Full OPD Run Dashboard ==================== */
        <div className="space-y-5">
          {detailQ.isLoading ? (
            <div className="space-y-4">
              <CardGridSkeleton count={6} />
              <ChartSkeleton height={340} />
              <ChartSkeleton height={340} />
            </div>
          ) : detailQ.isError ? (
            <StateNoticeCard
              kind="error"
              title={`训练运行 ${selectedRunId} 详情加载失败`}
              description={(detailQ.error as Error).message}
              onRetry={() => detailQ.refetch()}
            />
          ) : detail ? (
            <>
              {/* KPI Summary Cards */}
              <section aria-label="OPD 训练运行核心指标" className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5">
                  <div className="text-[11px] text-[var(--text-muted)] flex items-center gap-1">
                    <Cpu className="w-3.5 h-3.5 text-[var(--brand-primary)]" />
                    <span>Student 模型</span>
                  </div>
                  <div
                    className="mt-1.5 text-sm font-bold font-mono text-[var(--text-primary)] truncate"
                    title={detail.student_model || '—'}
                  >
                    {detail.student_model || '—'}
                  </div>
                  <div className="mt-1 text-[10px] text-[var(--text-muted)] font-mono truncate" title={detail.run_id}>
                    {detail.run_id}
                  </div>
                </div>

                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5">
                  <div className="text-[11px] text-[var(--text-muted)] flex items-center gap-1">
                    <Activity className="w-3.5 h-3.5 text-[var(--brand-primary)]" />
                    <span>已完成步数</span>
                  </div>
                  <div className="mt-1 text-2xl font-bold tabular-nums text-[var(--text-primary)]">
                    {detail.steps_completed}
                  </div>
                  <div className="mt-0.5 text-[11px] text-[var(--text-muted)] tabular-nums">
                    {detail.checkpoint_count} 个 checkpoint
                  </div>
                </div>

                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5">
                  <div className="text-[11px] text-[var(--text-muted)] flex items-center gap-1">
                    <GitBranch className="w-3.5 h-3.5 text-[var(--brand-primary)]" />
                    <span>专家数量</span>
                  </div>
                  <div className="mt-1 text-2xl font-bold tabular-nums text-[var(--text-primary)]">
                    {detail.expert_count}
                  </div>
                  <div className="mt-0.5 text-[11px] text-[var(--text-muted)]">多专家协同蒸馏</div>
                </div>

                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5">
                  <div className="text-[11px] text-[var(--text-muted)] flex items-center gap-1">
                    <Network className="w-3.5 h-3.5 text-[var(--brand-primary)]" />
                    <span>已路由任务数</span>
                  </div>
                  <div className="mt-1 text-2xl font-bold tabular-nums text-[var(--text-primary)]">
                    {detail.total_routed_tasks}
                  </div>
                  <div className="mt-0.5 text-[11px] text-[var(--text-muted)] font-mono">routing.json</div>
                </div>

                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5">
                  <div className="text-[11px] text-[var(--text-muted)] flex items-center gap-1">
                    <TrendingDown className="w-3.5 h-3.5 text-[#ef4444]" />
                    <span>最新 Reverse KL</span>
                  </div>
                  <div className="mt-1 text-2xl font-bold tabular-nums text-[var(--text-primary)]">
                    {fmtKl(detail.latest_mean_reverse_kl)}
                  </div>
                  <div
                    className={`mt-0.5 text-[11px] tabular-nums ${
                      klDelta !== null && klDelta <= 0 ? 'text-[var(--sem-pass-fg)]' : 'text-[var(--text-muted)]'
                    }`}
                  >
                    {klDelta !== null ? `较 Step 1 ${klDelta > 0 ? '+' : ''}${klDelta.toFixed(4)}` : '越低越贴近专家'}
                  </div>
                </div>

                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5">
                  <div className="text-[11px] text-[var(--text-muted)] flex items-center gap-1">
                    <TrendingUp className="w-3.5 h-3.5 text-[#10b981]" />
                    <span>最新 Harbor Reward</span>
                  </div>
                  <div className="mt-1 text-2xl font-bold tabular-nums text-[var(--text-primary)]">
                    {fmtReward(detail.latest_mean_reward)}
                  </div>
                  <div
                    className={`mt-0.5 text-[11px] tabular-nums ${
                      rewardDelta !== null && rewardDelta >= 0 ? 'text-[var(--sem-pass-fg)]' : 'text-[var(--text-muted)]'
                    }`}
                  >
                    {rewardDelta !== null ? `较 Step 1 ${rewardDelta > 0 ? '+' : ''}${rewardDelta.toFixed(4)}` : '越高越好'}
                  </div>
                </div>
              </section>

              {/* Multi-Expert Routing Topology Sankey + Expert Summary Table */}
              <section
                aria-label="多专家路由拓扑图"
                className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden"
              >
                <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">
                      多专家路由拓扑（Dataset → Environment/Source → Expert → Expert Model）
                    </h2>
                    <p className="m-0 mt-0.5 text-[11px] text-[var(--text-muted)]">
                      来源：<code>{detail.source_path_redacted}/routing.json</code> · 共 {detail.total_routed_tasks} 条路由记录
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-[11px]">
                    {(['dataset', 'environment_source', 'expert', 'expert_model'] as const).map((stg) => (
                      <span
                        key={stg}
                        className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2 py-0.5 text-[var(--text-secondary)]"
                      >
                        <span
                          className="w-2 h-2 rounded-full"
                          style={{ backgroundColor: STAGE_COLOR[stg] }}
                        />
                        <span>{STAGE_LABEL_ZH[stg]}</span>
                      </span>
                    ))}
                  </div>
                </div>

                <div className="p-4">
                  {detail.routing_topology.nodes.length > 0 ? (
                    <EChartBox
                      option={sankeyOption}
                      height={340}
                      exportName={`opd-routing-${detail.run_id}`}
                    />
                  ) : (
                    <StateNoticeCard kind="empty" title="routing.json 为空或缺少路由节点" />
                  )}
                </div>

                {/* Expert Summary Table */}
                {detail.routing_topology.experts.length > 0 && (
                  <div className="border-t border-[var(--border-subtle)] overflow-x-auto">
                    <table className="w-full text-left border-collapse text-xs">
                      <thead>
                        <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)]/60 text-[var(--text-muted)]">
                          <th className="py-2 px-4 font-semibold">Expert</th>
                          <th className="py-2 px-4 font-semibold">Expert Model</th>
                          <th className="py-2 px-4 font-semibold text-right">路由任务数</th>
                          <th className="py-2 px-4 font-semibold">Datasets</th>
                          <th className="py-2 px-4 font-semibold">Environments</th>
                          <th className="py-2 px-4 font-semibold">Sources</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-[var(--border-subtle)] tabular-nums">
                        {detail.routing_topology.experts.map((exp) => {
                          const share =
                            detail.total_routed_tasks > 0
                              ? (exp.task_count / detail.total_routed_tasks) * 100
                              : 0;
                          const dotColor = expertColorMap.get(exp.expert) || stableColorForKey(exp.expert);
                          return (
                            <tr key={exp.expert} className="hover:bg-[var(--bg-elevated)]/50">
                              <td className="py-2.5 px-4 font-mono font-semibold text-[var(--text-primary)]">
                                <span className="inline-flex items-center gap-2">
                                  <span
                                    className="w-2.5 h-2.5 rounded-full shrink-0"
                                    style={{ backgroundColor: dotColor }}
                                  />
                                  <span>{exp.expert}</span>
                                </span>
                              </td>
                              <td className="py-2.5 px-4 font-mono text-[var(--text-secondary)]">{exp.model}</td>
                              <td className="py-2.5 px-4 text-right font-semibold text-[var(--text-primary)]">
                                {exp.task_count}{' '}
                                <span className="text-[11px] font-normal text-[var(--text-muted)]">
                                  ({fmtPct(share, 1)})
                                </span>
                              </td>
                              <td className="py-2.5 px-4 font-mono text-[11px] text-[var(--text-secondary)]">
                                {exp.datasets.join(', ') || '—'}
                              </td>
                              <td className="py-2.5 px-4 font-mono text-[11px] text-[var(--text-secondary)]">
                                {exp.environments.join(', ') || '—'}
                              </td>
                              <td className="py-2.5 px-4">
                                <div className="flex flex-wrap gap-1">
                                  {exp.sources.length > 0 ? (
                                    exp.sources.map((src) => (
                                      <span
                                        key={src}
                                        className="rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--text-secondary)]"
                                      >
                                        {src}
                                      </span>
                                    ))
                                  ) : (
                                    <span className="text-[var(--text-muted)]">—</span>
                                  )}
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              {/* Dual-Axis Step Trend Chart + Checkpoint/Token Table */}
              <section
                aria-label="步级指标双轴趋势与 Checkpoint 明细"
                className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden"
              >
                <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] space-y-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">
                        步级指标双轴趋势（Reverse KL ↓ vs Harbor Reward ↑）
                      </h2>
                      <p className="m-0 mt-0.5 text-[11px] text-[var(--text-muted)]">
                        左轴 <code>sampled_reverse_kl</code>（实线下降）· 右轴 <code>Harbor reward</code>（实线爬升）· 支持按 Expert 分色叠加拆解
                      </p>
                    </div>

                    {enabledExperts.length > 0 && (
                      <div className="inline-flex rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-0.5 text-[11px]" role="group" aria-label="分专家显示指标筛选">
                        {(
                          [
                            ['both', '分专家: KL+Reward'],
                            ['kl', '分专家: 仅 KL'],
                            ['reward', '分专家: 仅 Reward'],
                          ] as const
                        ).map(([mode, lbl]) => (
                          <button
                            key={mode}
                            type="button"
                            aria-pressed={expertMetricMode === mode}
                            onClick={() => setExpertMetricMode(mode)}
                            className={`rounded-md px-2 py-0.5 font-medium cursor-pointer ${
                              expertMetricMode === mode
                                ? 'bg-[var(--brand-primary)] text-white'
                                : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
                            }`}
                          >
                            {lbl}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Per-Expert Series Toggles */}
                  <div className="flex flex-wrap items-center gap-1.5 text-[11px]" role="group" aria-label="曲线系列开关">
                    <button
                      type="button"
                      aria-pressed={showGlobalSeries}
                      onClick={() => setShowGlobalSeries((v) => !v)}
                      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border font-semibold cursor-pointer ${
                        showGlobalSeries
                          ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                          : 'border-[var(--border-subtle)] bg-[var(--bg-surface)] text-[var(--text-secondary)]'
                      }`}
                    >
                      <span>全局汇总曲线</span>
                    </button>

                    {expertNames.map((exp) => {
                      const active = enabledExperts.includes(exp);
                      const color = expertColorMap.get(exp) || stableColorForKey(exp);
                      return (
                        <button
                          key={exp}
                          type="button"
                          aria-pressed={active}
                          onClick={() => toggleExpert(exp)}
                          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border font-mono cursor-pointer transition-colors ${
                            active
                              ? 'border-[var(--border-strong)] bg-[var(--bg-surface)] text-[var(--text-primary)] font-semibold'
                              : 'border-[var(--border-subtle)] bg-transparent text-[var(--text-muted)] hover:text-[var(--text-secondary)]'
                          }`}
                        >
                          <span
                            className="w-2 h-2 rounded-full shrink-0"
                            style={{ backgroundColor: color, opacity: active ? 1 : 0.45 }}
                          />
                          <span>{exp}</span>
                        </button>
                      );
                    })}

                    {expertNames.length > 0 && (
                      <button
                        type="button"
                        onClick={() =>
                          setEnabledExperts((prev) => (prev.length === expertNames.length ? [] : [...expertNames]))
                        }
                        className="ml-1 text-[11px] text-[var(--brand-primary)] hover:underline cursor-pointer bg-transparent border-0 p-0"
                      >
                        {enabledExperts.length === expertNames.length ? '隐藏全部分专家' : '展开全部分专家'}
                      </button>
                    )}
                  </div>
                </div>

                <div className="p-4">
                  {detail.step_series.length === 0 ? (
                    <StateNoticeCard kind="empty" title="尚无 Step 1+ 的 checkpoint opd.json 记录" />
                  ) : !showGlobalSeries && enabledExperts.length === 0 ? (
                    <StateNoticeCard
                      kind="empty"
                      title="未选择任何趋势曲线"
                      description="请点击上方「全局汇总曲线」或任意 Expert 芯片以显示步级 Reverse KL 与 Harbor Reward 曲线。"
                    />
                  ) : (
                    <EChartBox
                      option={stepTrendOption}
                      height={340}
                      exportName={`opd-steps-${detail.run_id}`}
                    />
                  )}
                </div>

                {/* Checkpoint & Token Table */}
                {detail.step_series.length > 0 && (
                  <div className="border-t border-[var(--border-subtle)] overflow-x-auto">
                    <table className="w-full text-left border-collapse text-xs">
                      <thead>
                        <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)]/60 text-[var(--text-muted)]">
                          <th className="py-2 px-3 font-semibold w-10" aria-label="展开行" />
                          <th className="py-2 px-3 font-semibold">Step</th>
                          <th className="py-2 px-3 font-semibold">Checkpoint</th>
                          <th className="py-2 px-3 font-semibold">Rollout Checkpoint</th>
                          <th className="py-2 px-3 font-semibold text-right">Rollouts</th>
                          <th className="py-2 px-3 font-semibold text-right">Tokens</th>
                          <th className="py-2 px-3 font-semibold text-right">Mean Reverse KL</th>
                          <th className="py-2 px-3 font-semibold text-right">Mean Reward</th>
                          <th className="py-2 px-3 font-semibold">分专家拆解 (KL / Reward)</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-[var(--border-subtle)] tabular-nums">
                        {detail.step_series.map((row) => {
                          const isExpanded = expandedStep === row.step;
                          return (
                            <React.Fragment key={row.checkpoint}>
                              <tr className="hover:bg-[var(--bg-elevated)]/50">
                                <td className="py-2 px-3">
                                  <button
                                    type="button"
                                    aria-expanded={isExpanded}
                                    aria-label={`展开 Step ${row.step} Rollout 明细`}
                                    onClick={() => setExpandedStep(isExpanded ? null : row.step)}
                                    className="p-0.5 rounded hover:bg-[var(--bg-elevated)] text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer bg-transparent border-0"
                                  >
                                    {isExpanded ? (
                                      <ChevronDown className="w-3.5 h-3.5" />
                                    ) : (
                                      <ChevronRight className="w-3.5 h-3.5" />
                                    )}
                                  </button>
                                </td>
                                <td className="py-2 px-3 font-mono font-bold text-[var(--text-primary)]">
                                  Step {row.step}
                                </td>
                                <td className="py-2 px-3 font-mono text-[var(--text-primary)]">{row.checkpoint}</td>
                                <td className="py-2 px-3 font-mono text-[var(--text-secondary)]">
                                  {row.rollout_checkpoint}
                                </td>
                                <td className="py-2 px-3 text-right">{row.rollout_count}</td>
                                <td className="py-2 px-3 text-right font-mono">{fmtTokens(row.total_tokens)}</td>
                                <td className="py-2 px-3 text-right font-mono font-semibold text-[#ef4444]">
                                  {fmtKl(row.mean_reverse_kl)}
                                </td>
                                <td className="py-2 px-3 text-right font-mono font-semibold text-[#10b981]">
                                  {fmtReward(row.mean_reward)}
                                </td>
                                <td className="py-2 px-3">
                                  <div className="flex flex-wrap gap-1">
                                    {Object.entries(row.by_expert || {})
                                      .sort(([a], [b]) => a.localeCompare(b))
                                      .map(([exp, agg]) => {
                                        const c = expertColorMap.get(exp) || stableColorForKey(exp);
                                        return (
                                          <span
                                            key={exp}
                                            className="inline-flex items-center gap-1 rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--text-secondary)]"
                                          >
                                            <span
                                              className="w-1.5 h-1.5 rounded-full"
                                              style={{ backgroundColor: c }}
                                            />
                                            <span>{exp}:</span>
                                            <span>{fmtKl(agg.mean_reverse_kl)}</span>
                                            <span>/</span>
                                            <span>{fmtReward(agg.mean_reward)}</span>
                                          </span>
                                        );
                                      })}
                                  </div>
                                </td>
                              </tr>
                              {isExpanded && (
                                <tr className="bg-[var(--bg-elevated)]/40">
                                  <td colSpan={9} className="px-6 py-3">
                                    <div className="text-[11px] font-semibold text-[var(--text-secondary)] mb-2">
                                      Step {row.step} 逐条 Rollout 证据（共 {row.rollouts.length} 条）
                                    </div>
                                    <div className="overflow-x-auto rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)]">
                                      <table className="w-full text-left border-collapse text-[11px]">
                                        <thead>
                                          <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                                            <th className="py-1.5 px-3">Dataset</th>
                                            <th className="py-1.5 px-3">Task</th>
                                            <th className="py-1.5 px-3">Expert</th>
                                            <th className="py-1.5 px-3 text-right">Tokens</th>
                                            <th className="py-1.5 px-3 text-right">sampled_reverse_kl</th>
                                            <th className="py-1.5 px-3 text-right">Harbor Reward</th>
                                          </tr>
                                        </thead>
                                        <tbody className="divide-y divide-[var(--border-subtle)] font-mono tabular-nums">
                                          {row.rollouts.map((ro, rIdx) => (
                                            <tr key={`${ro.dataset}-${ro.task}-${rIdx}`}>
                                              <td className="py-1.5 px-3 text-[var(--text-secondary)]">{ro.dataset}</td>
                                              <td className="py-1.5 px-3 text-[var(--text-primary)]">{ro.task}</td>
                                              <td className="py-1.5 px-3 text-[var(--text-secondary)]">{ro.expert}</td>
                                              <td className="py-1.5 px-3 text-right">{ro.tokens.toLocaleString()}</td>
                                              <td className="py-1.5 px-3 text-right text-[#ef4444]">
                                                {fmtKl(ro.sampled_reverse_kl)}
                                              </td>
                                              <td className="py-1.5 px-3 text-right text-[#10b981]">
                                                {fmtReward(ro.reward)}
                                              </td>
                                            </tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    </div>
                                  </td>
                                </tr>
                              )}
                            </React.Fragment>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              {/* Guarded Holdout Before/After Comparison Card */}
              <section
                aria-label="训练前后留出集严格护栏对照卡"
                className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden"
              >
                <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <ShieldCheck className="w-4 h-4 text-[var(--brand-primary)]" />
                    <h2 className="m-0 text-sm font-bold text-[var(--text-primary)]">
                      训练前后留出集（-holdout）增益对照卡
                    </h2>
                  </div>
                  <span className="text-[11px] text-[var(--text-muted)]">
                    严格护栏：明确 checkpoint 身份 + 同留出题集 + 同评分协议 + dataset:holdout_read
                  </span>
                </div>

                <div className="p-4">
                  {detail.holdout_comparison.status === 'available' && detail.holdout_comparison.items.length > 0 ? (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      {detail.holdout_comparison.items.map((item) => {
                        const improved = item.status === 'improved' || item.delta_pp > 0;
                        const regressed = item.status === 'regressed' || item.delta_pp < 0;
                        return (
                          <div
                            key={`${item.dataset_release_id}-${item.scoring_protocol}-${item.base_batch_id}-${item.candidate_batch_id}`}
                            className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]/60 p-4 space-y-3 text-xs"
                          >
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <div>
                                <div className="font-mono font-bold text-[var(--text-primary)]">
                                  {item.dataset_release_id}
                                </div>
                                <div className="text-[11px] text-[var(--text-muted)] font-mono">
                                  protocol: {item.scoring_protocol}
                                </div>
                              </div>
                              <span
                                className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold tabular-nums border ${
                                  improved
                                    ? 'border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                                    : regressed
                                      ? 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]'
                                      : 'border-[var(--sem-unscored-border)] bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)]'
                                }`}
                              >
                                {improved ? (
                                  <ArrowUpRight className="w-3.5 h-3.5" />
                                ) : regressed ? (
                                  <ArrowDownRight className="w-3.5 h-3.5" />
                                ) : null}
                                <span>{fmtDeltaPP(item.delta_pp)}</span>
                              </span>
                            </div>

                            <div className="group/row-action flex items-center justify-between gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-2.5 tabular-nums">
                              <div className="min-w-0">
                                <div className="text-[10px] text-[var(--text-muted)]">Base ({item.base_checkpoint})</div>
                                <div className="text-base font-bold text-[var(--text-primary)]">
                                  {fmtPct(item.base_score_pct)}
                                </div>
                                <RowActionIcon
                                  variant="details"
                                  label="查看 Base 批次"
                                  to={`/eval/batches/${encodeURIComponent(item.base_batch_id)}`}
                                />
                              </div>

                              <ArrowRight className="w-4 h-4 text-[var(--text-muted)] shrink-0" />

                              <div className="min-w-0 text-right">
                                <div className="text-[10px] text-[var(--text-muted)]">
                                  Candidate ({item.candidate_checkpoint})
                                </div>
                                <div className="text-base font-bold text-[var(--text-primary)]">
                                  {fmtPct(item.candidate_score_pct)}
                                </div>
                                <RowActionIcon
                                  variant="details"
                                  label="查看 Candidate 批次"
                                  to={`/eval/batches/${encodeURIComponent(item.candidate_batch_id)}`}
                                />
                              </div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div
                      className={`rounded-lg border px-4 py-3.5 flex items-start gap-2.5 text-xs ${
                        detail.holdout_comparison.status === 'redacted_missing_scope'
                          ? 'border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] text-[var(--text-primary)]'
                          : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                      }`}
                    >
                      {detail.holdout_comparison.status === 'redacted_missing_scope' ? (
                        <Lock className="w-4 h-4 text-[var(--sem-zero-fg)] shrink-0 mt-0.5" />
                      ) : (
                        <ShieldAlert className="w-4 h-4 text-[var(--text-muted)] shrink-0 mt-0.5" />
                      )}
                      <div className="space-y-1">
                        <div className="font-semibold text-[var(--text-primary)]">
                          {detail.holdout_comparison.status === 'redacted_missing_scope'
                            ? '留出集前后对比受权限保护'
                            : '暂不满足留出集前后对照护栏条件'}
                        </div>
                        <div className="leading-relaxed">
                          {detail.holdout_comparison.reason ||
                            '尚未发现同时满足明确 checkpoint 身份、同留出题集与同评分协议的成对批次。'}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </section>
            </>
          ) : null}

          {/* Collapsible CLI Generator (Accessible even when runs exist) */}
          <section
            aria-label="OPD 预检与训练命令生成器（折叠面板）"
            className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden"
          >
            <button
              type="button"
              aria-expanded={cliPanelOpen}
              onClick={() => setCliPanelOpen((v) => !v)}
              className="w-full px-4 py-3 bg-[var(--bg-elevated)] flex items-center justify-between gap-2 text-left cursor-pointer border-0"
            >
              <span className="inline-flex items-center gap-2 text-xs font-bold text-[var(--text-primary)]">
                <Terminal className="w-4 h-4 text-[var(--brand-primary)]" />
                <span>生成新一轮 train.py --backend opd --plan 预检与训练 CLI 命令</span>
              </span>
              <span className="inline-flex items-center gap-1 text-[11px] text-[var(--text-secondary)]">
                <span>{cliPanelOpen ? '收起' : '展开生成器'}</span>
                {cliPanelOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
              </span>
            </button>

            {cliPanelOpen && (
              <div className="p-5 border-t border-[var(--border-subtle)]">
                <TrainingCliGenerator
                  datasets={runsQ.data?.registered_train_datasets || []}
                  canWrite={canWrite}
                  gpuNotice={
                    emptyGuide?.gpu_notice ||
                    '197 (LRM-02) 无本地训练 GPU，本页表单仅生成 --plan 预检与训练机可执行 CLI 命令。'
                  }
                />
              </div>
            )}
          </section>
        </div>
      )}
    </PageTransition>
  );
}
