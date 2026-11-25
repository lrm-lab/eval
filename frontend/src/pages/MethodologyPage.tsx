import React, { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowRight, BookText, CircleHelp, Search } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { PageTransition } from '../components/motion/primitives';

const TOPICS = [
  {
    id: 'orthogonal-dimensions',
    title: '哪些批次适合放在一起比较？',
    summary: '先对齐题库发行版、评分协议和评测范围，再比较同一批共同覆盖的题。',
    steps: [
      '在主榜选中候选批次，打开批次详情核对题库版本和 split。',
      '比较分数前核对评分协议和 Full、Subset、Smoke 或 Reference 范围。故障策略和官方认证状态也要单独查看。',
      '如果这些口径不一致，把批次放进自由对比后先查看覆盖和共同题。',
    ],
    example: '同一个模型跑了 2 题 Smoke 和 100 题 Full，两行即使都显示 50%，分数也对应不同的题集。',
    link: ['/leaderboard', '去主榜核对批次'],
  },
  {
    id: 'macro-avg',
    title: '为什么可比宏观均分会随对比对象变化？',
    summary: '可比宏观均分只纳入参与对象都具备可比分数的基准，每个基准等权。',
    steps: [
      '从对比矩阵的共同基准列表查看参与平均的基准和范围。',
      '需要了解单个对象的测评覆盖时，另看自身覆盖均分和已测基准数。',
      '分数旁显示「—」表示缺测或无有效分。有效零分会显示 0%。',
    ],
    example: 'A 有 GAIA 和 DeepSWE 的成绩，B 只有可比的 GAIA 成绩。A 与 B 的可比宏观均分均使用 GAIA。',
    link: ['/eval/compare', '去自由对比'],
  },
  {
    id: 'three-denominators',
    title: '对比时该选哪个分母？',
    summary: '先决定要观察解题能力、实际运行效果，还是单个批次的整体成绩。',
    steps: [
      '共同有效题（common_valid）适合看有有效评分的共同题上谁答得更好。',
      '共同覆盖题、故障计零（common_covered_faults_as_zero）适合把运行失败纳入实际表现。',
      '独立全批（self_full_batch_valid）用于查看每个批次自己的有效均值和分母。',
    ],
    example: '两批都跑过 10 题，其中有 2 题存在运行故障。若只比两批都有效的 8 题，分母是 8。若采用故障计零的共同覆盖口径，分母是 10，各批次按自己的有效结果算分。',
    link: ['/eval/compare', '去查看共同题'],
  },
  {
    id: 'gain-vs-guardrail',
    title: '怎么看提分和防退化结果？',
    summary: 'Agentic 关注相对基线提升。文本与视觉基准按设定阈值判断退化。',
    steps: [
      '先选择一个基线，再查看 Agentic 赛道中的百分点差和相对变化。',
      '检查文本和视觉护栏的可比覆盖，区分护栏维持、触及阈值和缺测无法判定。',
      '点进有变化的基准查看关联批次与差异试题。',
    ],
    example: '40% 到 45% 是提高 5 个百分点，相对提高 12.5%。如果视觉基准缺测，视觉护栏显示无法判定。',
    link: ['/', '打开评测首页'],
  },
  {
    id: 'efficiency-scatter',
    title: '效率图上的点和前沿表示什么？',
    summary: '纵轴显示分数，横轴显示实际观测的资源用量。前沿是当前口径下的非支配点。',
    steps: [
      '先选耗时、Token、工具步数或已记录成本，查看点上标出的覆盖。',
      '调整横轴后留意共同题策略。固定纵轴与重算横纵轴可能采用不同的题集。',
      '横轴未覆盖全部比较样本的点会标注覆盖，前沿使用满覆盖的点。',
    ],
    example: '两个同口径批次同样得 70%。耗时更短的批次在耗时横轴上更靠左，若没有其他批次同时更快且分数更高，它位于前沿。',
    link: ['/eval/diagnostics', '去效率诊断'],
  },
  {
    id: 'token-semantics',
    title: '怎么看 Token 和耗时？',
    summary: '优先看实际观测字段及覆盖。阶段时间、首 Token 时间和整题耗时各自有独立口径。',
    steps: [
      '打开批次的用量说明。只有确认互斥的类别才使用堆叠，总量与其中的已缓存 Token 可能重叠。',
      '历史记录缺少阶段时间戳时，使用整题总耗时。',
      'TTFT 和 TPS 来自真实流式观测。视图留白时可切回已记录的耗时或 Token 轴。',
    ],
    example: '总耗时包含多个阶段。若这条运行只记录了总耗时 42 秒，页面只能据此展示总耗时。',
    link: ['/leaderboard', '打开主榜'],
  },
  {
    id: 'security-and-auth',
    title: '为什么试题详情有些标签为空？',
    summary: '详情显示已采集、可读取且当前账号有权限查看的证据。',
    steps: [
      '先看置灰标签的提示。原生 Harbor 轨迹、网关抓包、图文问答和训练采集是四类证据。',
      '如果提示授权限制，请记录批次名和证据类型，向来源负责人核对审批和访问权限。',
      '如果系统提示发布代已变化，刷新详情后再打开差异试题。',
    ],
    example: '纯文本原生 Harbor 作业可以有 ATIF 轨迹，同时没有图文问答、网关抓包或训练采集。',
    link: ['/eval/reports', '去报告中心选批次'],
  },
];

export function helpTopicForPath(path: string) {
  if (path.startsWith('/eval/compare')) return 'three-denominators';
  if (path.startsWith('/eval/diagnostics')) return 'efficiency-scatter';
  if (/^\/eval\/(batches|reports|direct-lab|execution)/.test(path)) return 'security-and-auth';
  if (path.startsWith('/leaderboard')) return 'macro-avg';
  if (path === '/') return 'gain-vs-guardrail';
  return 'orthogonal-dimensions';
}

interface MethodologySection {
  title?: string;
  summary?: string;
  rules?: string[];
}
interface MethodologyResp {
  methodology_version: string;
  sections: Record<string, MethodologySection>;
}

export function MethodologyPage() {
  const { hash } = useLocation();
  const [query, setQuery] = useState('');
  const current = TOPICS.find((topic) => `#${topic.id}` === hash) ?? TOPICS[0];
  const filter = query.trim().toLowerCase();
  const visible = TOPICS.filter((topic) =>
    [topic.title, topic.summary, ...topic.steps].some((entry) => entry.toLowerCase().includes(filter)),
  );
  const registry = useQuery({
    queryKey: ['methodology'],
    queryFn: () => apiFetch<MethodologyResp>('/api/v1/toolkit/methodology'),
  });

  return (
    <PageTransition className="mx-auto max-w-6xl space-y-5 px-4 py-6 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <CircleHelp className="h-6 w-6 text-[var(--brand-primary)]" aria-hidden="true" />
          <h1 className="m-0 text-xl font-bold">评测帮助</h1>
        </div>
        <Link to="/toolkit/manuals" className="inline-flex items-center gap-2 text-sm text-[var(--brand-primary)]">
          <BookText size={14} aria-hidden="true" /> 框架与平台手册
        </Link>
      </header>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(15rem,1fr)_minmax(0,2.5fr)]">
        <nav aria-label="帮助主题" className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3">
          <label className="flex items-center gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2">
            <Search size={15} aria-hidden="true" />
            <input
              aria-label="搜索帮助"
              placeholder="搜索分母、Token、证据…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className="w-full min-w-0 bg-transparent text-sm outline-none"
            />
          </label>
          <ul className="m-0 mt-3 grid list-none gap-1 p-0">
            {visible.map((topic) => (
              <li key={topic.id}>
                <Link
                  to={`#${topic.id}`}
                  aria-current={current.id === topic.id ? 'location' : undefined}
                  className={`block rounded-lg px-3 py-2 text-sm no-underline motion-safe:transition-colors ${
                    current.id === topic.id
                      ? 'bg-[var(--brand-tint)] font-semibold text-[var(--brand-primary)]'
                      : 'text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)]'
                  }`}
                >
                  {topic.title}
                </Link>
              </li>
            ))}
          </ul>
          {visible.length === 0 && <p className="px-3 text-xs text-[var(--text-muted)]">没有找到匹配的主题。</p>}
        </nav>

        <PageTransition key={current.id} className="min-w-0 overflow-hidden rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-surface)]">
          <article id={current.id} className="scroll-mt-20">
            <header className="border-b border-[var(--border-subtle)] bg-[var(--brand-tint)] px-5 py-5 sm:px-7">
              <h2 className="m-0 text-xl font-bold">{current.title}</h2>
              <p className="mb-0 mt-2 text-sm leading-relaxed text-[var(--text-secondary)]">{current.summary}</p>
            </header>
            <div className="space-y-5 p-5 sm:p-7">
              <ol className="m-0 space-y-3 pl-5 text-sm leading-relaxed text-[var(--text-primary)] marker:font-semibold marker:text-[var(--brand-primary)]">
                {current.steps.map((step) => <li key={step} className="pl-1">{step}</li>)}
              </ol>
              <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-4 py-3">
                <h3 className="m-0 text-sm font-semibold">示例</h3>
                <p className="mb-0 mt-1 text-sm leading-relaxed text-[var(--text-secondary)]">{current.example}</p>
              </div>
              <Link to={current.link[0]} className="inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--brand-primary)] hover:underline">
                {current.link[1]} <ArrowRight size={15} aria-hidden="true" />
              </Link>
            </div>
          </article>
          {registry.data && (
            <details className="border-t border-[var(--border-subtle)] px-5 py-3 text-xs text-[var(--text-secondary)] sm:px-7">
              <summary className="cursor-pointer">当前生效的口径注册表 · {registry.data.methodology_version}</summary>
              <dl className="mb-1 mt-3 grid gap-3">
                {Object.entries(registry.data.sections).map(([id, section]) => (
                  <div key={id}>
                    <dt className="font-semibold text-[var(--text-primary)]">{section.title || id}</dt>
                    {section.summary && <dd className="ml-0 mt-1 leading-relaxed">{section.summary}</dd>}
                    {section.rules?.map((rule) => <dd key={rule} className="ml-0 mt-1 leading-relaxed">{rule}</dd>)}
                  </div>
                ))}
              </dl>
            </details>
          )}
        </PageTransition>
      </div>
    </PageTransition>
  );
}
