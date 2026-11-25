import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { BookOpen, CheckCircle2, Shield } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { PageTransition } from '../components/motion/primitives';
import { CardGridSkeleton, StateNoticeCard } from '../components/ui/Skeletons';

interface MethodologySection {
  id: string;
  title: string;
  summary: string;
  rules?: string[];
  details?: string;
}

interface MethodologyResp {
  methodology_version: string;
  sections: MethodologySection[] | Record<string, { title?: string; summary?: string; rules?: string[] }>;
}

const STATIC_ANCHORS: Array<{ id: string; title: string; points: string[] }> = [
  {
    id: 'orthogonal-dimensions',
    title: '五大正交评测维度隔离',
    points: [
      'dataset_release_id（数据集发行版与 split）、scoring_protocol（评分协议）、run_scope（full_split / subset / smoke / reference）、fault_policy（故障处理策略）、official_status（官方校验态）彼此独立建模。',
      '任何跨协议或跨范围的成绩严禁自动合并排名；Reference（Oracle / NOP）批次默认与正式主榜隔离。',
    ],
  },
  {
    id: 'macro-avg',
    title: '可比宏观均分（Comparative Macro Avg）与反缺测偏置',
    points: [
      '主榜与对比矩阵的 Comparative Macro Avg 仅在参与对象拥有完全相同协议与范围的「共同基准交集」上等权平均。',
      '缺测基准在表格中统一展示为「—」，严禁填 0；Self-Coverage Avg 仅作单对象自身覆盖度参考。',
    ],
  },
  {
    id: 'three-denominators',
    title: '三大独立分母口径（绝对禁止混算）',
    points: [
      '1) common_valid：仅统计所有对比对象均有效评分（reward_valid=1）的共同题交集。',
      '2) common_covered_faults_as_zero：在所有共同覆盖题（含故障题）上，各对象按自身实际通过数除以共同覆盖数，故障按 0 分计（严禁用 common_valid 分子除以 common_covered 分母）。',
      '3) self_full_batch_valid：各批次独立全批有效均值，展示实际覆盖分母，不可替代交集比较。',
    ],
  },
  {
    id: 'gain-vs-guardrail',
    title: '三线提分主线 vs 防退化护栏（Gain vs Guardrail）',
    points: [
      'Agentic 轨道（optimize_gain）：关注相对基线的绝对提分（Δpp）与未舍入浮点相对变化率（rel_pct）。',
      'LLM-Text 与 Vision-Image 轨道（non_regression_guardrail）：严格三态判定（护栏维持 / 触及退步阈值 / 缺测无法判定），缺测基准如 MMLU-Pro、BFCL-v4、Global-MMLU-Lite 必须明确标为 undeterminable。',
    ],
  },
  {
    id: 'efficiency-scatter',
    title: 'ARC Prize 效率前沿散点口径',
    points: [
      '严格基于真实 Harbor/历史评测字段（耗时 P50、均总 Tokens、均工具步数、已记录原生成本 recorded_cost_usd），绝不伪造商业定价或未实测硬件字段。',
      '提供 fixed_y_common 与 recompute_xy_common 两种切轴策略；X 轴未满覆盖的批次明确标注并禁止进入 Pareto 前沿连线。',
    ],
  },
  {
    id: 'token-semantics',
    title: 'Token 互斥语义与 4 阶段耗时诚实展示',
    points: [
      '仅当批次明确标记 token_semantics = mutually_exclusive_verified 时绘制 4 段互斥堆叠条；否则以并排独立胶囊展示。',
      '严禁将整题总耗时伪装为 TTFT 或 TPS；无 4 阶段时间戳的历史批次明确标注「仅总耗时」。',
    ],
  },
  {
    id: 'security-and-auth',
    title: '权限、证据分级与跨代一致性保障',
    points: [
      '全站会话认证 + 项目成员 RBAC + 6 类细粒度 Scope（trace:read、gateway:read、verifier_log:read、direct_vlm_qa:read、dataset:answer_read、dataset:holdout_read）。',
      '所有主榜与档案聚合先鉴权再按 canonical_trial_key 去重（Auth-Before-Dedup）；逐题 Diff 严格绑定发布代 generation_id，跨代修改直接返回 409 GENERATION_MISMATCH。',
    ],
  },
];

export function MethodologyPage() {
  const q = useQuery({
    queryKey: ['methodology'],
    queryFn: () => apiFetch<MethodologyResp>('/api/v1/toolkit/methodology'),
  });

  const backendSections: MethodologySection[] = Array.isArray(q.data?.sections)
    ? q.data!.sections
    : q.data?.sections
      ? Object.entries(q.data.sections).map(([id, v]) => ({
          id,
          title: v.title || id,
          summary: v.summary || '',
          rules: v.rules,
        }))
      : [];

  return (
    <PageTransition className="max-w-5xl mx-auto px-5 py-7 space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-subtle)] pb-4">
        <div>
          <div className="flex items-center gap-2">
            <BookOpen className="w-5 h-5 text-[var(--brand-primary)]" />
            <h1 className="m-0 text-xl font-bold text-[var(--text-primary)]">评测方法学与指标口径说明</h1>
          </div>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            单一权威口径来源 · 所有页面 `?` 提示气泡均直链本页对应章节
          </p>
        </div>
        {q.data && (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-1 text-xs font-mono text-[var(--text-secondary)]">
            Version: {q.data.methodology_version}
          </span>
        )}
      </div>

      {/* Quick Anchor TOC */}
      <div className="flex flex-wrap gap-2">
        {STATIC_ANCHORS.map((a) => (
          <a
            key={a.id}
            href={`#${a.id}`}
            className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-1 text-xs text-[var(--text-secondary)] hover:border-[var(--brand-primary)] hover:text-[var(--brand-primary)] no-underline"
          >
            #{a.title.split('（')[0]}
          </a>
        ))}
      </div>

      {q.isLoading ? (
        <CardGridSkeleton count={4} />
      ) : q.isError ? (
        <StateNoticeCard kind="error" title="方法学注册表读取失败" description={(q.error as Error).message} onRetry={() => q.refetch()} />
      ) : (
        <div className="space-y-4">
          {STATIC_ANCHORS.map((sec) => (
            <section
              key={sec.id}
              id={sec.id}
              className="scroll-mt-20 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-5"
            >
              <h2 className="m-0 text-sm font-bold text-[var(--text-primary)] flex items-center gap-2">
                <CheckCircle2 className="w-4 h-4 text-[var(--brand-primary)] shrink-0" />
                <span>{sec.title}</span>
              </h2>
              <ul className="mt-3 mb-0 pl-5 space-y-2 text-xs text-[var(--text-secondary)] leading-relaxed">
                {sec.points.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            </section>
          ))}

          {backendSections.length > 0 && (
            <section className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-5">
              <h2 className="m-0 text-sm font-bold text-[var(--text-primary)] flex items-center gap-2 mb-3">
                <Shield className="w-4 h-4 text-[var(--sem-pass-fg)]" />
                <span>后端运行期方法学注册表（实时读取）</span>
              </h2>
              <div className="space-y-3">
                {backendSections.map((s) => (
                  <div key={s.id} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 text-xs">
                    <div className="font-semibold text-[var(--text-primary)]">
                      {s.title} <span className="font-mono text-[11px] text-[var(--text-muted)]">({s.id})</span>
                    </div>
                    {s.summary && <p className="m-0 mt-1 text-[var(--text-secondary)]">{s.summary}</p>}
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </PageTransition>
  );
}
