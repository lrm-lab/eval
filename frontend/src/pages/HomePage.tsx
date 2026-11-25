import React, { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  AlertTriangle,
  ArrowRight,
  Clock,
  GitCompareArrows,
  Layers,
  ShieldCheck,
  Play,
  Trophy,
} from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDateShort, fmtPct, runScopeLabel } from '../lib/format';
import type { GainGuardrailVerdict, LeaderboardResponse, SystemOverviewResponse } from '../types/api';
import {
  AnimatedNumber,
  PageTransition,
  SlidingTabs,
  SpotlightCard,
  StaggerContainer,
  StaggerItem,
} from '../components/motion/primitives';
import { RowActionIcon } from '../components/ui/RowActionIcon';
import { GainGuardrailPill } from '../components/ui/GainGuardrailPill';
import { StateNoticeCard } from '../components/ui/Skeletons';
import { MethodologyPopover } from '../components/ui/MethodologyPopover';

interface CompareOptionPreset {
  preset_id: string;
  title: string;
  common_note: string;
  payload: {
    subjects: Array<{
      id: string;
      kind: 'batch' | 'legacy_model';
      batch_id?: string;
      model_cols?: string[];
      alias?: string;
    }>;
    baseline_subject_id?: string;
    scoring_protocol?: string;
  };
}

interface CompareOptionsResp {
  presets: CompareOptionPreset[];
}

interface VerdictResp {
  comparison_id: string;
  gain_vs_guardrail_verdict: GainGuardrailVerdict | null;
  auto_insights: Array<{ text: string }>;
}

const THRESHOLD_OPTIONS = [
  { id: '-0.5', label: '严苛 (-0.5 pp)' },
  { id: '-1', label: '标准 (-1.0 pp)' },
  { id: '-2', label: '宽松 (-2.0 pp)' },
];

export function HomePage() {
  const navigate = useNavigate();
  const [selectedPresetIdx, setSelectedPresetIdx] = useState(0);
  const [thresholdStr, setThresholdStr] = useState('-1');

  const overviewQ = useQuery({
    queryKey: ['system-overview'],
    queryFn: () => apiFetch<SystemOverviewResponse>('/api/v1/system/overview'),
  });

  const lbQ = useQuery({
    queryKey: ['home-best-by-task'],
    queryFn: () =>
      apiFetch<LeaderboardResponse>('/api/v1/leaderboard?track_slug=all&run_scope=full_split&include_legacy=true&page_size=20'),
  });

  const optsQ = useQuery({
    queryKey: ['home-compare-options'],
    queryFn: () => apiFetch<CompareOptionsResp>('/api/v1/compare/options'),
  });

  const presets = optsQ.data?.presets || [];
  const activePreset = presets[selectedPresetIdx] || presets[0];

  const verdictUrl = useMemo(() => {
    if (!activePreset || activePreset.payload.subjects.length < 2) return null;
    const [s0, s1] = activePreset.payload.subjects;
    const thr = Number(thresholdStr);
    if (s0.kind === 'legacy_model' && s1.kind === 'legacy_model') {
      const b = (s0.model_cols || []).join(',');
      const c = (s1.model_cols || []).join(',');
      return `/api/v1/compare/verdict?kind=legacy_model&baseline=${encodeURIComponent(b)}&candidate=${encodeURIComponent(
        c
      )}&regression_threshold_pp=${thr}`;
    }
    if (s0.batch_id && s1.batch_id) {
      const proto = activePreset.payload.scoring_protocol
        ? `&scoring_protocol=${encodeURIComponent(activePreset.payload.scoring_protocol)}`
        : '';
      return `/api/v1/compare/verdict?kind=batch&baseline=${encodeURIComponent(s0.batch_id)}&candidate=${encodeURIComponent(
        s1.batch_id
      )}&regression_threshold_pp=${thr}${proto}`;
    }
    return null;
  }, [activePreset, thresholdStr]);

  const verdictQ = useQuery({
    queryKey: ['home-verdict', verdictUrl],
    enabled: Boolean(verdictUrl),
    placeholderData: keepPreviousData,
    queryFn: () => apiFetch<VerdictResp>(verdictUrl!),
  });

  const kpis = overviewQ.data?.kpis;
  const verdict = verdictQ.data?.gain_vs_guardrail_verdict;
  const bestByTask = (lbQ.data?.best_by_task || []).slice(0, 8);
  const recentBatches = overviewQ.data?.recent_batches || [];

  const guardPass = (verdict?.guardrails || []).filter((g) => g.status === 'pass').length;
  const guardReg = (verdict?.guardrails || []).filter((g) => g.status === 'regressed').length;
  const guardUndet = (verdict?.guardrails || []).filter((g) => g.status === 'undeterminable').length;
  const verdictLoading =
    optsQ.isLoading ||
    verdictQ.isLoading ||
    (Boolean(verdictUrl) && verdictQ.isPending && !verdict);

  const buildEvidenceUrl = (benchmarkSlug: string, datasetReleaseId: string | null) => {
    const qp = new URLSearchParams();
    if (activePreset) {
      qp.set('preset', activePreset.preset_id);
    }
    qp.set('regression_threshold_pp', thresholdStr);
    qp.set('focus_benchmark', benchmarkSlug);
    if (datasetReleaseId) {
      qp.set('focus_dataset', datasetReleaseId);
    }
    return `/eval/compare?${qp.toString()}`;
  };

  return (
    <PageTransition>
      <section aria-labelledby="home-hero-title" className="lrm-hero-bg">
        <div aria-hidden="true" className="lrm-hero-grid" />
        <div className="relative mx-auto max-w-7xl px-5 py-12 text-center sm:px-8 sm:py-16 lg:py-20">
          <h1
            id="home-hero-title"
            className="m-0 text-[clamp(2rem,5.6vw,4.75rem)] leading-[1.12] font-bold tracking-[-0.045em] text-[var(--text-primary)]"
          >
            <span className="inline-block">大模型与 <span className="text-[var(--brand-primary)]">Agent</span></span>{' '}
            <span className="inline-block">评测平台</span>
          </h1>
          <p className="mx-auto mt-4 mb-0 max-w-prose text-base leading-relaxed text-[var(--text-secondary)] sm:text-lg lg:text-xl">
            运行评测、对比模型、追查每道题的证据
          </p>
          <nav aria-label="首页常用操作" className="mx-auto mt-7 grid max-w-sm grid-cols-2 gap-2.5 sm:flex sm:max-w-none sm:flex-wrap sm:justify-center">
            <Link
              to="/eval/execution"
              className="lrm-press col-span-2 inline-flex min-h-11 items-center justify-center gap-2 rounded-xl bg-[color-mix(in_srgb,var(--brand-primary),black_24%)] px-5 py-2.5 text-sm font-semibold text-white no-underline shadow-sm hover:bg-[color-mix(in_srgb,var(--brand-primary),black_18%)]"
            >
              <Play aria-hidden="true" className="size-4" />
              运行评测
            </Link>
            <Link
              to="/leaderboard"
              className="lrm-press inline-flex min-h-11 items-center justify-center gap-2 rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)] px-4 py-2.5 text-sm font-semibold text-[var(--text-primary)] no-underline hover:border-[var(--brand-primary)] hover:bg-[var(--bg-elevated)]"
            >
              <Trophy aria-hidden="true" className="size-4 text-[var(--brand-primary)]" />
              查看主榜
            </Link>
            <Link
              to="/eval/compare"
              className="lrm-press inline-flex min-h-11 items-center justify-center gap-2 rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)] px-4 py-2.5 text-sm font-semibold text-[var(--text-primary)] no-underline hover:border-[var(--brand-primary)] hover:bg-[var(--bg-elevated)]"
            >
              <GitCompareArrows aria-hidden="true" className="size-4 text-[var(--brand-primary)]" />
              对比模型
            </Link>
          </nav>
        </div>
      </section>

      <div className="max-w-7xl mx-auto px-5 py-7 space-y-8">
        {/* 1. KPI Overview Band */}
        <section className="min-h-[110px]">
          {overviewQ.isLoading ? (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3.5" aria-busy="true">
              {Array.from({ length: 5 }).map((_, i) => (
                <div
                  key={i}
                  className="h-[110px] rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 flex flex-col justify-between"
                >
                  <div className="lrm-skeleton h-3.5 w-24" />
                  <div className="lrm-skeleton h-7 w-20" />
                  <div className="lrm-skeleton h-3 w-28" />
                </div>
              ))}
            </div>
          ) : overviewQ.isError ? (
            <StateNoticeCard
              kind="error"
              title="系统总览加载失败"
              description={(overviewQ.error as Error).message}
              onRetry={() => overviewQ.refetch()}
            />
          ) : kpis ? (
            <StaggerContainer className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3.5">
              <StaggerItem className="h-[110px]">
                <SpotlightCard onClick={() => navigate('/datasets')} className="p-4 h-[110px] flex flex-col justify-between">
                  <div className="text-xs text-[var(--text-secondary)]">纳管基准 / 发行版</div>
                  <div className="mt-1.5 flex items-baseline gap-1.5 whitespace-nowrap">
                    <AnimatedNumber value={kpis.benchmarks} className="text-2xl font-bold text-[var(--text-primary)]" />
                    <span className="text-xs text-[var(--text-muted)]">/ {kpis.dataset_releases} releases</span>
                  </div>
                  <div className="mt-1 text-[11px] text-[var(--text-muted)] truncate">动态配置 taxonomy.yaml</div>
                </SpotlightCard>
              </StaggerItem>

              <StaggerItem className="h-[110px]">
                <SpotlightCard onClick={() => navigate('/eval/reports')} className="p-4 h-[110px] flex flex-col justify-between">
                  <div className="text-xs text-[var(--text-secondary)]">已可读批次</div>
                  <div className="mt-1.5 flex items-baseline gap-2 whitespace-nowrap">
                    <AnimatedNumber value={kpis.readable_batches} className="text-2xl font-bold text-[var(--text-primary)]" />
                    {kpis.running_placeholders > 0 && (
                      <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium bg-[var(--sem-running-bg)] text-[var(--sem-running-fg)]">
                        <span className="w-1.5 h-1.5 rounded-full bg-[var(--sem-running-fg)] lrm-calm-pulse" />
                        {kpis.running_placeholders} 运行中
                      </span>
                    )}
                  </div>
                  <div className="mt-1 text-[11px] text-[var(--text-muted)] truncate">热层 + 历史冷层</div>
                </SpotlightCard>
              </StaggerItem>

              <StaggerItem className="h-[110px]">
                <SpotlightCard onClick={() => navigate('/leaderboard')} className="p-4 h-[110px] flex flex-col justify-between">
                  <div className="text-xs text-[var(--text-secondary)]">去重有效评分 Trials</div>
                  <div className="mt-1.5 flex items-baseline gap-1.5 whitespace-nowrap">
                    <AnimatedNumber value={kpis.dedup_scored_trials} className="text-2xl font-bold text-[var(--text-primary)]" />
                    <span className="text-xs text-[var(--text-muted)]">/ {kpis.dedup_recorded_trials.toLocaleString()}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-[var(--text-muted)] truncate">
                    原始总记录 {kpis.total_recorded_trials.toLocaleString()}
                  </div>
                </SpotlightCard>
              </StaggerItem>

              <StaggerItem className="h-[110px]">
                <SpotlightCard onClick={() => navigate('/models')} className="p-4 h-[110px] flex flex-col justify-between">
                  <div className="text-xs text-[var(--text-secondary)]">模型 × Harness 组合</div>
                  <div className="mt-1.5">
                    <AnimatedNumber value={kpis.model_harness_combos} className="text-2xl font-bold text-[var(--text-primary)]" />
                  </div>
                  <div className="mt-1 text-[11px] text-[var(--text-muted)] truncate">独立建模模型与代理框架</div>
                </SpotlightCard>
              </StaggerItem>

              <StaggerItem className="h-[110px]">
                <SpotlightCard onClick={() => navigate('/admin/health')} className="p-4 h-[110px] flex flex-col justify-between">
                  <div className="text-xs text-[var(--text-secondary)] flex items-center justify-between">
                    <span>索引告警</span>
                    {kpis.index_warnings > 0 && <AlertTriangle className="w-3.5 h-3.5 text-[var(--sem-zero-fg)]" />}
                  </div>
                  <div className="mt-1.5">
                    <AnimatedNumber
                      value={kpis.index_warnings}
                      className={`text-2xl font-bold ${
                        kpis.index_warnings > 0 ? 'text-[var(--sem-zero-fg)]' : 'text-[var(--sem-pass-fg)]'
                      }`}
                    />
                  </div>
                  <div className="mt-1 text-[11px] text-[var(--text-muted)] truncate">点击查看系统健康与探针</div>
                </SpotlightCard>
              </StaggerItem>
            </StaggerContainer>
          ) : null}
        </section>

        {/* 2. Interactive 3-Track Gain vs Guardrail Verdict */}
        <section className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-5 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-4 pb-4 border-b border-[var(--border-subtle)]">
            <div>
              <div className="flex items-center gap-2">
                <ShieldCheck className="w-5 h-5 text-[var(--brand-primary)]" />
                <h2 className="m-0 text-base font-bold text-[var(--text-primary)]">
                  三线提分与防退化护栏裁决（Gain vs Guardrail）
                </h2>
                <MethodologyPopover
                  title="Gain vs Guardrail 裁决口径"
                  summary="Agentic 主线关注相对基线提分（Δpp 与未舍入相对变化）；LLM-Text 与 Vision-Image 护栏采用三态判定：维持/提升、触及退步阈值、缺测无法判定（严禁把缺测算通过）。"
                  anchor="#gain-vs-guardrail"
                />
              </div>
              <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
                实时调整退步阈值即可观察护栏状态翻转（例如 MMMU-Pro -0.81pp 在 -1.0pp 下维持，在 -0.5pp 下触发退步告警）。
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2.5 min-h-[32px]">
              {presets.length > 0 ? (
                <select
                  aria-label="选择三线对比组合"
                  value={selectedPresetIdx}
                  onChange={(e) => setSelectedPresetIdx(Number(e.target.value))}
                  className="h-[30px] w-60 max-w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-1 text-xs text-[var(--text-primary)] cursor-pointer"
                >
                  {presets.map((p, idx) => (
                    <option key={p.preset_id} value={idx}>
                      {p.title}
                    </option>
                  ))}
                </select>
              ) : (
                <div
                  className={`h-[30px] w-60 max-w-full rounded-lg ${optsQ.isLoading ? 'lrm-skeleton' : 'invisible'}`}
                  aria-hidden="true"
                />
              )}

              <SlidingTabs
                layoutId="home-threshold-tabs"
                size="sm"
                value={thresholdStr}
                onChange={setThresholdStr}
                options={THRESHOLD_OPTIONS}
              />

              <button
                type="button"
                onClick={() =>
                  navigate(
                    activePreset
                      ? `/eval/compare?preset=${encodeURIComponent(activePreset.preset_id)}&regression_threshold_pp=${encodeURIComponent(thresholdStr)}`
                      : `/eval/compare?regression_threshold_pp=${encodeURIComponent(thresholdStr)}`
                  )
                }
                className="inline-flex items-center gap-1 rounded-lg bg-[var(--brand-tint)] px-3 py-1.5 text-xs font-semibold text-[var(--brand-primary)] hover:bg-[var(--brand-primary)] hover:text-white transition-colors cursor-pointer"
              >
                <span>深度对比</span>
                <ArrowRight className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          <div className="mt-4 min-h-[470px]">
          {verdictLoading ? (
            <div className="space-y-4" aria-busy="true">
              <div className="lrm-skeleton h-[58px] w-full rounded-xl" />
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]/60 p-4 h-[396px] flex flex-col gap-2.5">
                  <div className="lrm-skeleton h-4 w-48 mb-1" />
                  {Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className="lrm-skeleton h-[54px] w-full rounded-lg" />
                  ))}
                </div>
                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]/60 p-4 h-[396px] flex flex-col gap-2">
                  <div className="lrm-skeleton h-4 w-56 mb-1" />
                  {Array.from({ length: 6 }).map((_, i) => (
                    <div key={i} className="lrm-skeleton h-[44px] w-full rounded-lg" />
                  ))}
                </div>
              </div>
            </div>
          ) : verdict ? (
            <div className="space-y-4">
              <div className="min-h-[58px] space-y-2">
                {(verdictQ.data?.auto_insights || []).length > 0 ? (
                  verdictQ.data!.auto_insights.map((ins, i) => (
                    <div
                      key={i}
                      className="min-h-[58px] flex items-center rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3.5 py-2 text-xs text-[var(--text-primary)] leading-relaxed"
                    >
                      {ins.text}
                    </div>
                  ))
                ) : (
                  <div className="min-h-[58px] flex items-center rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3.5 py-2 text-xs text-[var(--text-secondary)]">
                    当前对比组合已按同题交集口径对齐计算。
                  </div>
                )}
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {/* Agentic Gain Column */}
                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]/60 p-4 h-[396px] flex flex-col">
                  <div className="flex items-center justify-between mb-3 shrink-0">
                    <span className="text-xs font-bold uppercase tracking-wider text-[var(--brand-primary)]">
                      Agentic 提分主线（Optimize Gain）
                    </span>
                    <span className="text-[11px] text-[var(--text-muted)] tabular-nums">
                      {verdict.agentic_gains.length} 项核心基准
                    </span>
                  </div>
                  <div className="space-y-2 flex-1 overflow-y-auto pr-1">
                    {verdict.agentic_gains.map((item) => (
                      <div
                        key={item.benchmark_slug}
                        className="flex items-center justify-between gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-2 text-xs hover:border-[var(--brand-primary)]/50 transition-colors"
                      >
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <Link
                              to={
                                item.status !== 'undeterminable'
                                  ? buildEvidenceUrl(item.benchmark_slug, item.dataset_release_id)
                                  : `/benchmarks/${item.benchmark_slug}`
                              }
                              className="font-semibold text-[var(--text-primary)] hover:text-[var(--brand-primary)] no-underline"
                            >
                              {item.benchmark_slug}
                            </Link>
                            {item.status !== 'undeterminable' && (
                              <Link
                                to={buildEvidenceUrl(item.benchmark_slug, item.dataset_release_id)}
                                className="text-[10px] text-[var(--brand-primary)] no-underline hover:underline"
                              >
                                同题证据 →
                              </Link>
                            )}
                          </div>
                          <div className="text-[11px] text-[var(--text-muted)] tabular-nums">
                            {item.baseline_score !== null && item.candidate_score !== null
                              ? `${fmtPct(item.baseline_score)} → ${fmtPct(item.candidate_score)} · ${item.coverage}`
                              : item.reason}
                          </div>
                        </div>
                        <GainGuardrailPill
                          mode="gain"
                          status={item.status}
                          deltaPp={item.delta_pp}
                          relPct={item.rel_pct}
                          reason={item.reason}
                        />
                      </div>
                    ))}
                  </div>
                </div>

                {/* Guardrails Column */}
                <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]/60 p-4 h-[396px] flex flex-col">
                  <div className="flex flex-wrap items-center justify-between gap-2 mb-3 shrink-0">
                    <span className="text-xs font-bold uppercase tracking-wider text-[var(--text-primary)]">
                      LLM-Text &amp; Vision 防退化护栏（阈值 {Number(thresholdStr).toFixed(1)} pp）
                    </span>
                    <div className="flex items-center gap-1.5 text-[11px] tabular-nums">
                      <span className="rounded px-1.5 py-0.5 bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]">
                        维持 {guardPass}
                      </span>
                      <span className="rounded px-1.5 py-0.5 bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]">
                        退步 {guardReg}
                      </span>
                      <span className="rounded px-1.5 py-0.5 bg-[var(--sem-unscored-bg)] text-[var(--sem-unscored-fg)]">
                        无法判定 {guardUndet}
                      </span>
                    </div>
                  </div>
                  <div className="space-y-1.5 flex-1 overflow-y-auto pr-1">
                    {verdict.guardrails.map((item) => (
                      <div
                        key={`${item.track_slug}-${item.benchmark_slug}`}
                        className="flex items-center justify-between gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs hover:border-[var(--brand-primary)]/50 transition-colors"
                      >
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5">
                            <Link
                              to={
                                item.status !== 'undeterminable'
                                  ? buildEvidenceUrl(item.benchmark_slug, item.dataset_release_id)
                                  : `/benchmarks/${item.benchmark_slug}`
                              }
                              className="font-semibold text-[var(--text-primary)] hover:text-[var(--brand-primary)] no-underline truncate"
                            >
                              {item.benchmark_slug}
                            </Link>
                            <span className="rounded px-1 py-0.2 text-[10px] bg-[var(--bg-elevated)] text-[var(--text-muted)]">
                              {item.track_slug}
                            </span>
                            {item.status !== 'undeterminable' && (
                              <Link
                                to={buildEvidenceUrl(item.benchmark_slug, item.dataset_release_id)}
                                className="text-[10px] text-[var(--brand-primary)] no-underline hover:underline shrink-0"
                              >
                                同题证据 →
                              </Link>
                            )}
                          </div>
                          <div className="text-[11px] text-[var(--text-muted)] tabular-nums truncate">
                            {item.baseline_score !== null && item.candidate_score !== null
                              ? `${fmtPct(item.baseline_score)} → ${fmtPct(item.candidate_score)}`
                              : item.reason}
                          </div>
                        </div>
                        <GainGuardrailPill
                          mode="guardrail"
                          status={item.status}
                          deltaPp={item.delta_pp}
                          relPct={item.rel_pct}
                          reason={item.reason}
                        />
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <StateNoticeCard kind="empty" title="暂无预设三线对比数据" description="请前往动态对比页手动选取两个以上批次进行对比。" />
          )}
          </div>
        </section>

        {/* 3. Best by Task King Cards */}
        <section>
          <div className="flex items-center justify-between mb-3.5">
            <div className="flex items-center gap-2">
              <Trophy className="w-4 h-4 text-[var(--best-cell)]" />
              <h2 className="m-0 text-base font-bold text-[var(--text-primary)]">各能力维度当前领先（Best by Task · Formal Full）</h2>
            </div>
            <Link to="/leaderboard" className="inline-flex items-center gap-1 text-xs font-medium text-[var(--brand-primary)] no-underline hover:underline">
              <span>查看完整主榜</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          </div>

          {lbQ.isLoading ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5" aria-busy="true">
              {Array.from({ length: 8 }).map((_, i) => (
                <div
                  key={i}
                  className="min-h-[124px] rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 flex flex-col justify-between"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="lrm-skeleton h-3 w-20" />
                    <div className="lrm-skeleton h-4 w-16 rounded" />
                  </div>
                  <div className="lrm-skeleton h-6 w-24" />
                  <div className="lrm-skeleton h-3.5 w-36" />
                  <div className="lrm-skeleton h-3 w-28" />
                </div>
              ))}
            </div>
          ) : bestByTask.length === 0 ? (
            <StateNoticeCard kind="empty" title="暂无 Formal Full 领先记录" />
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
              {bestByTask.map((b) => (
                <SpotlightCard
                  key={`${b.dataset_release_id}-${b.scoring_protocol}`}
                  onClick={() =>
                    b.batch_id ? navigate(`/eval/batches/${b.batch_id}`) : navigate(`/benchmarks/${b.benchmark_slug}`)
                  }
                  className="p-4 min-h-[124px] flex flex-col justify-between"
                >
                  <div className="flex items-center justify-between gap-2 text-[11px] text-[var(--text-muted)]">
                    <span className="truncate">{b.dimension}</span>
                    <span className="rounded px-1.5 py-0.5 bg-[var(--bg-elevated)] font-mono shrink-0">{b.benchmark_slug}</span>
                  </div>
                  <div className="mt-2 flex items-baseline justify-between gap-2">
                    <span className="text-xl font-bold tabular-nums text-[var(--best-cell)]">{fmtPct(b.score)}</span>
                    <span className="text-[11px] tabular-nums text-[var(--text-secondary)]">{b.coverage}</span>
                  </div>
                  <div className="mt-1.5 text-xs font-semibold text-[var(--text-primary)] truncate" title={b.model_canonical}>
                    {b.model_canonical}
                  </div>
                  <div className="mt-1 flex items-center justify-between text-[11px] text-[var(--text-muted)]">
                    <span className="truncate">
                      {b.harness} · {b.runner}
                    </span>
                    <span className="font-mono truncate max-w-[110px]" title={b.scoring_protocol}>
                      {b.denominator_kind}
                    </span>
                  </div>
                </SpotlightCard>
              ))}
            </div>
          )}
        </section>

        {/* 4. Recent Batches Feed */}
        <section>
          <div className="flex items-center justify-between mb-3.5">
            <div className="flex items-center gap-2">
              <Layers className="w-4 h-4 text-[var(--brand-primary)]" />
              <h2 className="m-0 text-base font-bold text-[var(--text-primary)]">最近批次动态</h2>
            </div>
            <Link to="/eval/reports" className="inline-flex items-center gap-1 text-xs font-medium text-[var(--brand-primary)] no-underline hover:underline">
              <span>全部评测报告</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          </div>

          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  <th className="py-2.5 px-3.5 font-semibold">批次名称</th>
                  <th className="py-2.5 px-3 font-semibold">基准 / 范围</th>
                  <th className="py-2.5 px-3 font-semibold">模型 / Harness</th>
                  <th className="py-2.5 px-3 font-semibold">覆盖率</th>
                  <th className="py-2.5 px-3 font-semibold text-right">有效均值</th>
                  <th className="py-2.5 px-3.5 font-semibold text-right">更新时间</th>
                  <th className="w-12 py-2.5 px-2 text-center">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {overviewQ.isLoading
                  ? Array.from({ length: 6 }).map((_, idx) => (
                      <tr key={idx} className="h-[52px]">
                        <td className="py-2.5 px-3.5"><div className="lrm-skeleton h-4 w-44" /></td>
                        <td className="py-2.5 px-3"><div className="lrm-skeleton h-4 w-24" /></td>
                        <td className="py-2.5 px-3"><div className="lrm-skeleton h-4 w-32" /></td>
                        <td className="py-2.5 px-3"><div className="lrm-skeleton h-4 w-14" /></td>
                        <td className="py-2.5 px-3"><div className="lrm-skeleton h-4 w-14 ml-auto" /></td>
                        <td className="py-2.5 px-3.5"><div className="lrm-skeleton h-4 w-20 ml-auto" /></td>
                        <td className="px-2"><div className="lrm-skeleton size-8 rounded-lg" /></td>
                      </tr>
                    ))
                  : recentBatches.map((b) => (
                  <tr
                    key={b.batch_id}
                    onClick={() => navigate(`/eval/batches/${b.batch_id}`)}
                    className="group/row-action hover:bg-[var(--bg-elevated)]/75 cursor-pointer transition-colors"
                  >
                    <td className="py-2.5 px-3.5 font-medium text-[var(--text-primary)] max-w-[260px] truncate">
                      <div className="flex items-center gap-2">
                        {b.is_running_placeholder && (
                          <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium bg-[var(--sem-running-bg)] text-[var(--sem-running-fg)] shrink-0">
                            <span className="w-1.5 h-1.5 rounded-full bg-[var(--sem-running-fg)] lrm-calm-pulse" />
                            运行中
                          </span>
                        )}
                        <span className="truncate">{b.batch_name}</span>
                      </div>
                    </td>
                    <td className="py-2.5 px-3 text-[var(--text-secondary)]">
                      <div>{b.benchmark_slug || b.raw_dataset_name || '—'}</div>
                      <div className="text-[11px] text-[var(--text-muted)]">{runScopeLabel(b.run_scope)}</div>
                    </td>
                    <td className="py-2.5 px-3 text-[var(--text-secondary)]">
                      <div className="text-[var(--text-primary)]">{b.model_canonical || b.model_route || '—'}</div>
                      <div className="text-[11px] text-[var(--text-muted)]">
                        {b.harness || b.runner} · {b.runner}
                      </div>
                    </td>
                    <td className="py-2.5 px-3 tabular-nums text-[var(--text-secondary)]">
                      {b.scored}/{b.planned || b.recorded}
                    </td>
                    <td className="py-2.5 px-3 text-right font-semibold tabular-nums text-[var(--text-primary)]">
                      {b.valid_mean !== null && b.valid_mean !== undefined
                        ? fmtPct(b.valid_mean <= 1.0 ? b.valid_mean * 100 : b.valid_mean)
                        : '—'}
                    </td>
                    <td className="py-2.5 px-3.5 text-right tabular-nums text-[var(--text-muted)] whitespace-nowrap">
                      <span className="inline-flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        {fmtDateShort(b.finished_at || b.indexed_at)}
                      </span>
                    </td>
                    <td className="px-2 text-center" onClick={(event) => event.stopPropagation()}>
                      <RowActionIcon
                        variant="details"
                        label={`查看批次详情：${b.batch_name}`}
                        to={`/eval/batches/${b.batch_id}`}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </PageTransition>
  );
}
