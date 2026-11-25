import React, { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  AlertTriangle,
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Clock,
  Cpu,
  FileCode2,
  FolderOpen,
  Image as ImageIcon,
  Keyboard,
  Lock,
  Radio,
  Terminal,
} from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDuration } from '../lib/format';
import { useAppStore } from '../lib/store';
import type { AtifStep, BatchTasksResponse, TrialDetailDTO } from '../types/api';
import { PageTransition, SlidingTabs } from '../components/motion/primitives';
import { PairedAtifStepCard } from '../components/ui/PairedAtifStepCard';
import { TokenBreakdownBar } from '../components/ui/TokenBreakdownBar';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

interface TrajectoryResp {
  total_steps: number;
  step_offset: number;
  step_limit: number;
  trajectory_status: string | null;
  steps: AtifStep[];
}

interface GatewayResp {
  redacted: boolean;
  summary: {
    model_calls: number;
    validated: boolean | null;
    turn_count: number;
    discarded_turns: number;
    finish_reasons: string[];
  };
  turns: Array<Record<string, unknown>>;
}

interface VerifierOutResp {
  truncated: boolean;
  size_bytes: number;
  content: string;
}

interface ArtifactsResp {
  items: Array<{ rel_path: string; size_bytes: number; preview: string | null }>;
  total: number;
}

interface TrainingCaptureResp {
  captured: boolean;
  training_status: string;
  message?: string;
  trace_sha256?: string;
  summary?: Record<string, unknown>;
}

export function TrialInspectorPage() {
  const { batchId = '', trialId = '' } = useParams<{ batchId: string; trialId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const pushToast = useAppStore((s) => s.pushToast);
  const user = useAppStore((s) => s.user);

  const returnToRaw = searchParams.get('returnTo');
  const safeReturnTo =
    returnToRaw && returnToRaw.startsWith('/') && !returnToRaw.startsWith('//')
      ? returnToRaw
      : `/eval/batches/${encodeURIComponent(batchId)}`;

  const [tab, setTab] = useState<'overview' | 'atif' | 'gateway' | 'verifier' | 'qa' | 'training'>('overview');
  const [onlyTools, setOnlyTools] = useState(false);
  const [stepOffset, setStepOffset] = useState(0);
  const [globalReasonOpen, setGlobalReasonOpen] = useState<boolean | undefined>(undefined);
  const [instantToggle, setInstantToggle] = useState(false);
  const [highlightedStep, setHighlightedStep] = useState<number | null>(null);
  const [includeGwTools, setIncludeGwTools] = useState(false);
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null);

  const trialQ = useQuery({
    queryKey: ['trial-detail', batchId, trialId],
    queryFn: () =>
      apiFetch<TrialDetailDTO>(`/api/v1/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(trialId)}`),
  });

  // Load batch tasks for Prev/Next & keyboard shortcuts j/k, [/]
  const batchTasksQ = useQuery({
    queryKey: ['batch-tasks', batchId],
    queryFn: () => apiFetch<BatchTasksResponse>(`/api/v1/batches/${encodeURIComponent(batchId)}/tasks`),
  });

  const trajQ = useQuery({
    queryKey: ['trial-traj', batchId, trialId, stepOffset, onlyTools],
    enabled: tab === 'atif',
    queryFn: () =>
      apiFetch<TrajectoryResp>(
        `/api/v1/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(
          trialId
        )}/trajectory?step_offset=${stepOffset}&step_limit=50&only_tools=${onlyTools}`
      ),
  });

  const gwQ = useQuery({
    queryKey: ['trial-gw', batchId, trialId, includeGwTools],
    enabled: tab === 'gateway',
    queryFn: () =>
      apiFetch<GatewayResp>(
        `/api/v1/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(
          trialId
        )}/gateway?turn_offset=0&turn_limit=25&include_tools=${includeGwTools}`
      ),
  });

  const verQ = useQuery({
    queryKey: ['trial-verifier', batchId, trialId],
    enabled: tab === 'verifier',
    queryFn: () =>
      apiFetch<VerifierOutResp>(
        `/api/v1/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(trialId)}/verifier-output`
      ),
  });

  const artQ = useQuery({
    queryKey: ['trial-artifacts', batchId, trialId],
    enabled: tab === 'verifier',
    queryFn: () =>
      apiFetch<ArtifactsResp>(
        `/api/v1/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(trialId)}/artifacts`
      ),
  });

  const trainQ = useQuery({
    queryKey: ['trial-training', batchId, trialId],
    enabled: tab === 'training' && Boolean(user && ['researcher', 'admin'].includes(user.role)),
    queryFn: () =>
      apiFetch<TrainingCaptureResp>(
        `/api/v1/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(trialId)}/training-capture`
      ),
  });

  // Compute Prev/Next task & Prev/Next attempt navigation
  const navInfo = useMemo(() => {
    const tasks = batchTasksQ.data?.items || [];
    const curTaskIdx = tasks.findIndex((t) => t.attempt_dots.some((d) => d.trial_id === trialId));
    const curTask = curTaskIdx >= 0 ? tasks[curTaskIdx] : null;
    const prevTaskTrial = curTaskIdx > 0 ? tasks[curTaskIdx - 1].attempt_dots[0]?.trial_id : null;
    const nextTaskTrial = curTaskIdx >= 0 && curTaskIdx < tasks.length - 1 ? tasks[curTaskIdx + 1].attempt_dots[0]?.trial_id : null;
    const dots = curTask?.attempt_dots || [];
    const curAttemptIdx = dots.findIndex((d) => d.trial_id === trialId);
    const prevAttempt = curAttemptIdx > 0 ? dots[curAttemptIdx - 1].trial_id : null;
    const nextAttempt = curAttemptIdx >= 0 && curAttemptIdx < dots.length - 1 ? dots[curAttemptIdx + 1].trial_id : null;
    return { prevTaskTrial, nextTaskTrial, prevAttempt, nextAttempt, curAttemptIdx, totalAttempts: dots.length };
  }, [batchTasksQ.data, trialId]);

  const goTrial = (nextId: string | null) => {
    if (!nextId) return;
    const ret = returnToRaw ? `?returnTo=${encodeURIComponent(returnToRaw)}` : '';
    navigate(`/eval/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(nextId)}${ret}`);
  };

  // Keyboard shortcuts: j/k switch task, [/] switch attempt, t batch-toggle reasoning (0ms instant!)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === 'j' && navInfo.nextTaskTrial) {
        goTrial(navInfo.nextTaskTrial);
      } else if (e.key === 'k' && navInfo.prevTaskTrial) {
        goTrial(navInfo.prevTaskTrial);
      } else if (e.key === ']' && navInfo.nextAttempt) {
        goTrial(navInfo.nextAttempt);
      } else if (e.key === '[' && navInfo.prevAttempt) {
        goTrial(navInfo.prevAttempt);
      } else if (e.key === 't') {
        setInstantToggle(true);
        setGlobalReasonOpen((prev) => !(prev ?? false));
        window.setTimeout(() => setInstantToggle(false), 60);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navInfo]);

  const trial = trialQ.data;
  const phaseTimings = trial?.phase_timings || null;
  const phaseEntries = phaseTimings
    ? Object.entries(phaseTimings).filter(([, v]) => v !== null && v !== undefined && Number.isFinite(v))
    : [];
  const totalPhaseSec = phaseEntries.reduce((acc, [, v]) => acc + Number(v || 0), 0);

  const jumpToStep = (stepId: number) => {
    const el = document.getElementById(`step-${stepId}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setHighlightedStep(stepId);
      window.setTimeout(() => setHighlightedStep(null), 900);
    }
  };

  // Honor deep-link anchor #step-N on mount or tab load (02-3 §3)
  useEffect(() => {
    const hash = window.location.hash;
    const m = /^#step-(\d+)$/.exec(hash);
    if (!m) return;
    const sid = Number(m[1]);
    if (tab !== 'atif') {
      setTab('atif');
      return;
    }
    if (trajQ.data?.steps?.length) {
      window.setTimeout(() => jumpToStep(sid), 120);
    }
  }, [tab, trajQ.data]);

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-5 space-y-5">
      {/* Sticky Microscope Header */}
      <div className="sticky top-14 z-20 -mx-5 px-5 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-surface)]/95 backdrop-blur-md flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <Link
            to={safeReturnTo}
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] no-underline shrink-0"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> 返回来源页
          </Link>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h1 className="m-0 text-sm font-bold font-mono text-[var(--text-primary)] truncate">
                {trial?.task_id || trialId}
              </h1>
              {trial && (
                <span
                  className={`rounded-full px-2 py-0.5 text-[11px] font-semibold tabular-nums ${
                    trial.reward_valid && (trial.reward ?? 0) >= 1
                      ? 'bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
                      : trial.reward_valid
                        ? 'bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)]'
                        : 'bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]'
                  }`}
                >
                  {trial.reward_valid
                    ? `Reward ${trial.reward}`
                    : `${trial.exception_type || trial.failure?.kind || '故障'} (${trial.status})`}
                </span>
              )}
            </div>
            <div className="text-[11px] font-mono text-[var(--text-muted)] truncate">
              Trial: {trialId} · Attempt {navInfo.curAttemptIdx >= 0 ? `${navInfo.curAttemptIdx + 1}/${navInfo.totalAttempts}` : '1'}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="hidden lg:inline-flex items-center gap-1 text-[11px] text-[var(--text-muted)] mr-1">
            <Keyboard className="w-3.5 h-3.5" /> j/k 切题 · [/] 切尝试 · t 折思考链
          </span>
          <button
            type="button"
            disabled={!navInfo.prevTaskTrial}
            onClick={() => goTrial(navInfo.prevTaskTrial)}
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1 disabled:opacity-40 cursor-pointer"
          >
            <ChevronLeft className="w-3.5 h-3.5" /> 上一题 (k)
          </button>
          <button
            type="button"
            disabled={!navInfo.nextTaskTrial}
            onClick={() => goTrial(navInfo.nextTaskTrial)}
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1 disabled:opacity-40 cursor-pointer"
          >
            下一题 (j) <ChevronRight className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      <div className="overflow-x-auto pb-1">
        <SlidingTabs
          layoutId="trial-microscope-tabs"
          size="sm"
          value={tab}
          onChange={(v) => setTab(v as typeof tab)}
          options={[
            { id: 'overview', label: '总览 / 4阶段耗时 / 故障', icon: <Clock className="w-3.5 h-3.5" /> },
            { id: 'atif', label: 'ATIF 工具轨迹', icon: <Terminal className="w-3.5 h-3.5" /> },
            { id: 'gateway', label: 'OpenEnv 网关抓包', icon: <Radio className="w-3.5 h-3.5" /> },
            { id: 'verifier', label: '验证器日志 & 产物', icon: <FileCode2 className="w-3.5 h-3.5" /> },
            { id: 'qa', label: 'Direct / VLM 问答与图像', icon: <ImageIcon className="w-3.5 h-3.5" /> },
            { id: 'training', label: '训练轨迹采集', icon: <Cpu className="w-3.5 h-3.5" /> },
          ]}
        />
      </div>

      {trialQ.isLoading ? (
        <TableSkeleton rows={6} cols={3} />
      ) : trialQ.isError ? (
        <StateNoticeCard kind="error" title="Trial 详情加载失败" description={(trialQ.error as Error).message} onRetry={() => trialQ.refetch()} />
      ) : !trial ? null : tab === 'overview' ? (
        <div className="space-y-4 text-xs">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-2">
              <div className="font-bold text-[var(--text-primary)]">评分与状态</div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">有效评分 (reward)</span>
                <span className="font-mono font-bold">{trial.reward ?? '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">原生评分 (native_reward)</span>
                <span className="font-mono">{trial.native_reward ?? '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">总耗时</span>
                <span className="tabular-nums font-semibold">{fmtDuration(trial.duration_sec)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">工具调用次数</span>
                <span className="tabular-nums">{trial.tool_call_count ?? '未记录'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-[var(--text-muted)]">已记录原生成本</span>
                <span className="font-mono">
                  {trial.cost.amount_usd !== null ? `$${trial.cost.amount_usd.toFixed(4)} (${trial.cost.source})` : '未记录'}
                </span>
              </div>
            </div>

            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-2">
              <div className="font-bold text-[var(--text-primary)]">Token 构成</div>
              <TokenBreakdownBar tokens={trial.tokens} />
              <div className="pt-2 text-[11px] text-[var(--text-muted)] font-mono">
                semantics: {trial.tokens.semantics}
              </div>
            </div>

            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-2">
              <div className="font-bold text-[var(--text-primary)]">4 阶段耗时分布</div>
              {phaseEntries.length > 0 && totalPhaseSec > 0 ? (
                <>
                  <div className="flex h-3 w-full rounded-full overflow-hidden bg-[var(--border-subtle)]">
                    {phaseEntries.map(([k, v], i) => {
                      const colors = ['#2563EB', '#10B981', '#F59E0B', '#8B5CF6'];
                      return (
                        <div
                          key={k}
                          style={{ width: `${(Number(v) / totalPhaseSec) * 100}%`, backgroundColor: colors[i % colors.length] }}
                          title={`${k}: ${fmtDuration(Number(v))}`}
                        />
                      );
                    })}
                  </div>
                  <div className="space-y-1 pt-1">
                    {phaseEntries.map(([k, v]) => (
                      <div key={k} className="flex justify-between tabular-nums">
                        <span className="text-[var(--text-secondary)]">{k}</span>
                        <span className="font-mono">{fmtDuration(Number(v))}</span>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <div className="rounded-lg bg-[var(--bg-elevated)] p-2.5 text-[var(--text-muted)]">
                  仅总耗时（{fmtDuration(trial.duration_sec)}）· 该批次未输出 4 阶段时间戳，严禁伪造 TTFT / TPS。
                </div>
              )}
            </div>
          </div>

          {(trial.failure || trial.exception_type) && (
            <div className="rounded-xl border border-[var(--sem-err-border)] bg-[var(--sem-err-bg)]/45 p-4 space-y-2">
              <div className="font-bold text-[var(--sem-err-fg)] flex items-center gap-1.5">
                <AlertTriangle className="w-4 h-4" /> 运行故障诊断（已脱敏内网路径与凭证）
              </div>
              <div className="font-mono">
                异常类型: <strong>{trial.exception_type || trial.failure?.kind || 'unknown'}</strong>
                {trial.failure?.kind && trial.exception_type && trial.failure.kind !== trial.exception_type
                  ? ` · 故障签名: ${trial.failure.kind}`
                  : ''}
                {' · '}HTTP {trial.http_status ?? '—'}
              </div>
              {trial.failure?.summary && <div className="text-[var(--text-primary)]">{trial.failure.summary}</div>}
              {trial.failure?.log_redacted && (
                <pre className="m-0 rounded-lg bg-[var(--bg-surface)] border border-[var(--border-subtle)] p-3 text-[11px] font-mono whitespace-pre-wrap break-words max-h-60 overflow-y-auto">
                  {trial.failure.log_redacted}
                </pre>
              )}
            </div>
          )}

          {Object.keys(trial.verifier_rewards || {}).length > 0 && (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
              <div className="font-bold text-[var(--text-primary)] mb-2">Verifier 子指标（verifier_rewards）</div>
              <pre className="m-0 rounded-lg bg-[var(--bg-elevated)] p-3 text-[11px] font-mono whitespace-pre-wrap overflow-x-auto">
                {JSON.stringify(trial.verifier_rewards, null, 2)}
              </pre>
            </div>
          )}
        </div>
      ) : tab === 'atif' ? (
        trajQ.isLoading ? (
          <TableSkeleton rows={6} cols={2} />
        ) : trajQ.isError ? (
          <StateNoticeCard kind="forbidden" title="ATIF 轨迹不可用或无 trace:read 权限" description={(trajQ.error as Error).message} />
        ) : trajQ.data ? (
          <div className="space-y-4">
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3 flex flex-wrap items-center justify-between gap-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-[var(--text-primary)] tabular-nums">
                  共 {trajQ.data.total_steps} 步（当前展示 {trajQ.data.step_offset + 1}–{trajQ.data.step_offset + trajQ.data.steps.length}）
                </span>
                <label className="inline-flex items-center gap-1.5 cursor-pointer text-[var(--text-secondary)]">
                  <input
                    type="checkbox"
                    checked={onlyTools}
                    onChange={(e) => {
                      setStepOffset(0);
                      setOnlyTools(e.target.checked);
                    }}
                  />
                  <span>仅显示含工具调用步骤</span>
                </label>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setInstantToggle(true);
                    setGlobalReasonOpen((prev) => !(prev ?? false));
                    window.setTimeout(() => setInstantToggle(false), 60);
                  }}
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1 text-xs cursor-pointer"
                >
                  {globalReasonOpen ? '全部折叠思考过程 (t)' : '全部展开思考过程 (t)'}
                </button>
              </div>
            </div>

            {/* Step Mini-Map */}
            {trajQ.data.steps.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {trajQ.data.steps.map((s) => {
                  const err = s.paired_tools.some((t) => t.observation?.is_error);
                  return (
                    <button
                      key={s.step_id}
                      type="button"
                      onClick={() => jumpToStep(s.step_id)}
                      className={`rounded px-1.5 py-0.5 text-[10px] font-mono tabular-nums cursor-pointer border ${
                        err
                          ? 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]'
                          : s.paired_tools.length > 0
                            ? 'border-[var(--brand-primary)]/40 bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                            : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-muted)]'
                      }`}
                    >
                      #{s.step_id}
                    </button>
                  );
                })}
              </div>
            )}

            <div className="space-y-3">
              {trajQ.data.steps.map((step) => (
                <PairedAtifStepCard
                  key={step.step_id}
                  step={step}
                  highlighted={highlightedStep === step.step_id}
                  globalReasoningOpen={globalReasonOpen}
                  instantToggle={instantToggle}
                  onCopyAnchor={(sid) => {
                    const anchorUrl = `${window.location.origin}${window.location.pathname}${window.location.search}#step-${sid}`;
                    navigator.clipboard?.writeText(anchorUrl).catch(() => {});
                    pushToast('success', `已复制步骤锚点 #step-${sid}`);
                  }}
                />
              ))}
            </div>

            {trajQ.data.total_steps > 50 && (
              <div className="flex items-center justify-between text-xs">
                <button
                  type="button"
                  disabled={stepOffset === 0}
                  onClick={() => setStepOffset((o) => Math.max(0, o - 50))}
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1.5 disabled:opacity-40 cursor-pointer"
                >
                  上一页 50 步
                </button>
                <button
                  type="button"
                  disabled={stepOffset + 50 >= trajQ.data.total_steps}
                  onClick={() => setStepOffset((o) => o + 50)}
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1.5 disabled:opacity-40 cursor-pointer"
                >
                  下一页 50 步
                </button>
              </div>
            )}
          </div>
        ) : null
      ) : tab === 'gateway' ? (
        gwQ.isLoading ? (
          <TableSkeleton rows={5} cols={2} />
        ) : gwQ.isError ? (
          <StateNoticeCard kind="empty" title="该 Trial 无 OpenEnv 网关抓包" description={(gwQ.error as Error).message} />
        ) : gwQ.data ? (
          <div className="space-y-4 text-xs">
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap gap-4 tabular-nums">
                <span>模型调用: <strong>{gwQ.data.summary.model_calls}</strong></span>
                <span>总轮次: <strong>{gwQ.data.summary.turn_count}</strong></span>
                <span>丢弃轮次: <strong>{gwQ.data.summary.discarded_turns}</strong></span>
                <span>Finish Reasons: <code className="font-mono">{gwQ.data.summary.finish_reasons.join(', ') || '—'}</code></span>
              </div>
              {!gwQ.data.redacted && (
                <label className="inline-flex items-center gap-1.5 cursor-pointer">
                  <input type="checkbox" checked={includeGwTools} onChange={(e) => setIncludeGwTools(e.target.checked)} />
                  <span>含完整 request_tools 定义</span>
                </label>
              )}
            </div>

            {gwQ.data.redacted ? (
              <div className="rounded-xl border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] p-4 flex items-center gap-2 text-[var(--sem-zero-fg)]">
                <Lock className="w-4 h-4 shrink-0" />
                <span>当前账号缺少 gateway:read 权限或来源未批准 gateway_capture，仅展示汇总统计。</span>
              </div>
            ) : (
              <div className="space-y-3">
                {gwQ.data.turns.map((turn, idx) => (
                  <div key={idx} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5">
                    <div className="font-semibold text-[var(--text-primary)] mb-2">Turn #{idx + 1}</div>
                    <pre className="m-0 rounded-lg bg-[var(--bg-elevated)] p-3 text-[11px] font-mono whitespace-pre-wrap break-words max-h-80 overflow-y-auto">
                      {JSON.stringify(turn, null, 2)}
                    </pre>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : null
      ) : tab === 'verifier' ? (
        <div className="space-y-4 text-xs">
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
            <div className="font-bold text-[var(--text-primary)] mb-2">验证器原始输出（verifier-output）</div>
            {verQ.isLoading ? (
              <TableSkeleton rows={4} cols={1} />
            ) : verQ.isError ? (
              <div className="text-[var(--text-muted)]">{(verQ.error as Error).message}</div>
            ) : verQ.data ? (
              <>
                {verQ.data.truncated && (
                  <div className="mb-2 text-[11px] text-[var(--sem-zero-fg)]">
                    日志原大小 {(verQ.data.size_bytes / 1024).toFixed(1)} KiB，已智能截断保留头部 32 KiB + 尾部 96 KiB。
                  </div>
                )}
                <pre className="m-0 rounded-lg bg-[var(--bg-elevated)] p-3 text-[11px] font-mono whitespace-pre-wrap break-words max-h-96 overflow-y-auto">
                  {verQ.data.content}
                </pre>
              </>
            ) : null}
          </div>

          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
            <div className="font-bold text-[var(--text-primary)] mb-2 flex items-center gap-1.5">
              <FolderOpen className="w-4 h-4 text-[var(--brand-primary)]" /> 沙箱产物列表（artifacts）
            </div>
            {artQ.isLoading ? (
              <TableSkeleton rows={3} cols={2} />
            ) : artQ.isError ? (
              <div className="text-[var(--text-muted)]">{(artQ.error as Error).message}</div>
            ) : (artQ.data?.items || []).length === 0 ? (
              <div className="text-[var(--text-muted)]">该 Trial 未附带独立 artifacts 文件。</div>
            ) : (
              <div className="space-y-2.5">
                {artQ.data!.items.map((a) => (
                  <div key={a.rel_path} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
                    <div className="flex items-center justify-between font-mono text-[11px]">
                      <span className="font-semibold text-[var(--text-primary)]">{a.rel_path}</span>
                      <span className="text-[var(--text-muted)]">{a.size_bytes} bytes</span>
                    </div>
                    {a.preview && (
                      <pre className="mt-2 m-0 rounded bg-[var(--bg-surface)] p-2 text-[11px] font-mono whitespace-pre-wrap break-words max-h-60 overflow-y-auto">
                        {a.preview}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : tab === 'qa' ? (
        <div className="space-y-4 text-xs">
          {trial.qa_redacted ? (
            <StateNoticeCard kind="forbidden" title="Direct / VLMEvalKit 问答正文已脱敏" description={trial.qa_redacted_reason} />
          ) : trial.qa ? (
            <>
              {trial.qa.media_count > 0 && (
                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
                  <div className="font-bold text-[var(--text-primary)] mb-3">
                    受鉴权多模态图像（{trial.qa.media_count} 张 · 点击放大）
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
                    {Array.from({ length: trial.qa.media_count }).map((_, idx) => (
                      <button
                        key={idx}
                        type="button"
                        onClick={() => setLightboxIdx(idx)}
                        className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-2 hover:border-[var(--brand-primary)] cursor-pointer"
                      >
                        <img
                          src={`/api/v1/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(trialId)}/media/${idx}`}
                          alt={`trial-media-${idx}`}
                          className="max-h-56 w-full object-contain rounded"
                        />
                      </button>
                    ))}
                  </div>
                </div>
              )}

              <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
                <div className="font-bold text-[var(--text-primary)] mb-2">问答记录正文</div>
                <pre className="m-0 rounded-lg bg-[var(--bg-elevated)] p-3 text-[11px] font-mono whitespace-pre-wrap break-words max-h-[60vh] overflow-y-auto">
                  {JSON.stringify(trial.qa.vlmeval_result || trial.qa.direct_result || trial.qa, null, 2)}
                </pre>
              </div>
            </>
          ) : (
            <StateNoticeCard kind="empty" title="该 Trial 不含 Direct / VLMEvalKit 问答结构" description="Agentic 批次请切换至 ATIF 工具轨迹或 OpenEnv 网关抓包标签页。" />
          )}
        </div>
      ) : (
        <div className="text-xs">
          {!user || !['researcher', 'admin'].includes(user.role) ? (
            <StateNoticeCard kind="forbidden" title="仅 Researcher / Admin 可查阅训练轨迹采集" />
          ) : trainQ.isLoading ? (
            <TableSkeleton rows={4} cols={2} />
          ) : trainQ.isError ? (
            <StateNoticeCard kind="error" title="训练轨迹读取失败" description={(trainQ.error as Error).message} />
          ) : trainQ.data && !trainQ.data.captured ? (
            <StateNoticeCard kind="empty" title="未开启训练轨迹采集 (not_captured)" description={trainQ.data.message} />
          ) : trainQ.data ? (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-2">
              <div className="font-mono text-[11px] text-[var(--text-secondary)]">
                Status: {trainQ.data.training_status} · SHA-256: {trainQ.data.trace_sha256}
              </div>
              <pre className="m-0 rounded-lg bg-[var(--bg-elevated)] p-3 text-[11px] font-mono whitespace-pre-wrap break-words max-h-96 overflow-y-auto">
                {JSON.stringify(trainQ.data.summary, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>
      )}

      {/* Lightbox Modal for Authenticated Trial Media */}
      {lightboxIdx !== null && (
        <div
          onClick={() => setLightboxIdx(null)}
          className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-6 cursor-zoom-out"
        >
          <img
            src={`/api/v1/batches/${encodeURIComponent(batchId)}/trials/${encodeURIComponent(trialId)}/media/${lightboxIdx}`}
            alt={`lightbox-${lightboxIdx}`}
            className="max-h-[90vh] max-w-[92vw] object-contain rounded-xl shadow-2xl"
          />
        </div>
      )}
    </PageTransition>
  );
}
