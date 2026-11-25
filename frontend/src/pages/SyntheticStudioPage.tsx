import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { CheckCircle2, FlaskConical, Lock, Play, ShieldCheck, Terminal } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { useAppStore } from '../lib/store';
import { PageTransition } from '../components/motion/primitives';
import { CardGridSkeleton, StateNoticeCard } from '../components/ui/Skeletons';

interface SynthDatasetsResp {
  available_methods: Record<string, Record<string, unknown>>;
  teacher_profiles: Array<{ profile_id: string; display_name: string; default_model: string; key_configured: boolean }>;
  items: Array<{
    dataset_id: string;
    tier: string;
    slug: string;
    seed: number | null;
    methods: string[];
    train_count: number;
    holdout_count: number;
    holdout_redacted: boolean;
    train_tasks: Array<{ name: string; method?: string; split?: string }>;
    holdout_tasks: Array<{ name: string; method?: string; split?: string }>;
  }>;
}

interface SynthCheckResp {
  dataset_id: string;
  scope_checked: string;
  checked: number;
  passed: number;
  failed: Record<string, unknown> | number;
  results?: Array<{ task: string; ok: boolean; errors?: string[] }>;
}

interface SynthGenResp {
  mode: string;
  slug: string;
  planned_train?: number;
  planned_holdout?: number;
  train_count?: number;
  holdout_count?: number;
  cli_command: string;
  dataset_id?: string;
}

export function SyntheticStudioPage() {
  const user = useAppStore((s) => s.user);
  const pushToast = useAppStore((s) => s.pushToast);
  const canWrite = Boolean(user && ['researcher', 'admin'].includes(user.role));

  const [slug, setSlug] = useState('synth-demo-pack');
  const [selectedMethods, setSelectedMethods] = useState<string[]>(['log-rollup']);
  const [trainPer, setTrainPer] = useState(2);
  const [holdoutPer, setHoldoutPer] = useState(1);
  const [checkResult, setCheckResult] = useState<SynthCheckResp | null>(null);
  const [genResult, setGenResult] = useState<SynthGenResp | null>(null);

  const dsQ = useQuery({
    queryKey: ['synth-datasets'],
    queryFn: () => apiFetch<SynthDatasetsResp>('/api/v1/synth/datasets'),
  });

  const checkMut = useMutation({
    mutationFn: (payload: { dataset_id: string; include_holdout: boolean }) =>
      apiFetch<SynthCheckResp>('/api/v1/synth/check', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    onSuccess: (res) => {
      setCheckResult(res);
      pushToast('success', `结构校验完成：${res.passed}/${res.checked} 通过`);
    },
    onError: (err: Error) => pushToast('error', `结构校验失败：${err.message}`),
  });

  const genMut = useMutation({
    mutationFn: (execute: boolean) =>
      apiFetch<SynthGenResp>('/api/v1/synth/generate', {
        method: 'POST',
        body: JSON.stringify({
          name: slug.trim(),
          methods: selectedMethods,
          train_per_method: trainPer,
          holdout_per_method: holdoutPer,
          execute,
        }),
      }),
    onSuccess: (res) => {
      setGenResult(res);
      pushToast('success', res.mode === 'plan' ? '已生成 synthesize.py 命令预检' : `已生成至 0700 隔离区：${res.dataset_id}`);
      if (res.mode !== 'plan') dsQ.refetch();
    },
    onError: (err: Error) => pushToast('error', `合成生成失败：${err.message}`),
  });

  const methodNames = Object.keys(dsQ.data?.available_methods || {});

  const toggleMethod = (m: string) => {
    setSelectedMethods((prev) => (prev.includes(m) ? prev.filter((x) => x !== m) : [...prev, m]));
  };

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <FlaskConical className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>Synthetic Studio 合成题库工坊</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            对接 197 真实 framework/synthesize.py 与 tianxi-synth-v1（40 train / 12 holdout）· Holdout 按权限严格脱敏
          </p>
        </div>
        <Link
          to="/synth/selection"
          className="rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-3 py-1.5 text-xs font-semibold text-white no-underline"
        >
          进入 RL/SFT 四通道选题板 →
        </Link>
      </div>

      {/* Dataset Inventory */}
      {dsQ.isLoading ? (
        <CardGridSkeleton count={3} />
      ) : dsQ.isError ? (
        <StateNoticeCard kind="error" title="合成题包加载失败" description={(dsQ.error as Error).message} onRetry={() => dsQ.refetch()} />
      ) : dsQ.data ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {dsQ.data.items.map((ds) => (
            <div key={ds.dataset_id} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3 text-xs">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <div className="font-bold text-sm text-[var(--text-primary)] font-mono">{ds.dataset_id}</div>
                  <div className="text-[11px] text-[var(--text-muted)]">
                    tier: {ds.tier} · seed: {ds.seed ?? '—'} · methods: {ds.methods.join(', ') || '—'}
                  </div>
                </div>
                {canWrite && (
                  <button
                    type="button"
                    disabled={checkMut.isPending}
                    onClick={() => checkMut.mutate({ dataset_id: ds.dataset_id, include_holdout: false })}
                    className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] hover:border-[var(--brand-primary)] px-2.5 py-1 text-[11px] font-medium cursor-pointer"
                  >
                    <ShieldCheck className="w-3.5 h-3.5 text-[var(--brand-primary)]" /> 运行 synthesize.py check
                  </button>
                )}
              </div>

              <div className="flex flex-wrap gap-2 tabular-nums text-[11px]">
                <span className="rounded px-2 py-0.5 bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)] font-semibold">
                  Train: {ds.train_count} 题
                </span>
                <span className="rounded px-2 py-0.5 bg-[var(--sem-zero-bg)] text-[var(--sem-zero-fg)] font-semibold inline-flex items-center gap-1">
                  {ds.holdout_redacted && <Lock className="w-3 h-3" />}
                  Holdout: {ds.holdout_count} 题{ds.holdout_redacted ? '（受保护）' : ''}
                </span>
              </div>

              {ds.train_tasks.length > 0 && (
                <div className="flex flex-wrap gap-1 max-h-24 overflow-y-auto">
                  {ds.train_tasks.slice(0, 12).map((t) => (
                    <span key={t.name} className="rounded bg-[var(--bg-elevated)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--text-secondary)]">
                      {t.name}
                    </span>
                  ))}
                  {ds.train_tasks.length > 12 && (
                    <span className="text-[10px] text-[var(--text-muted)]">... 共 {ds.train_tasks.length} 题</span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      ) : null}

      {checkResult && (
        <div className="rounded-xl border border-[var(--sem-pass-border)] bg-[var(--bg-surface)] p-4 text-xs space-y-2">
          <div className="font-bold flex items-center gap-1.5 text-[var(--sem-pass-fg)]">
            <CheckCircle2 className="w-4 h-4" />
            结构校验结果 · {checkResult.dataset_id}（{checkResult.passed}/{checkResult.checked} 通过，失败{' '}
            {typeof checkResult.failed === 'number' ? checkResult.failed : Object.keys(checkResult.failed || {}).length}）
          </div>
          <div className="text-[11px] text-[var(--text-muted)]">校验范围：{checkResult.scope_checked}</div>
        </div>
      )}

      {/* Generator & Plan Preview */}
      {canWrite && (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3 text-xs">
          <div className="font-bold text-sm text-[var(--text-primary)] flex items-center gap-1.5">
            <Terminal className="w-4 h-4 text-[var(--brand-primary)]" />
            受控题包生成与预检（仅写入 0700 隔离待审区，不改写共享盘只读目录）
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <label className="space-y-1">
              <span className="text-[var(--text-secondary)]">题包 Slug (kebab-case)</span>
              <input
                type="text"
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs"
              />
            </label>
            <label className="space-y-1">
              <span className="text-[var(--text-secondary)]">每方法 Train 题数</span>
              <input
                type="number"
                min={1}
                max={20}
                value={trainPer}
                onChange={(e) => setTrainPer(Number(e.target.value) || 1)}
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs"
              />
            </label>
            <label className="space-y-1">
              <span className="text-[var(--text-secondary)]">每方法 Holdout 题数</span>
              <input
                type="number"
                min={0}
                max={10}
                value={holdoutPer}
                onChange={(e) => setHoldoutPer(Number(e.target.value) || 0)}
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs"
              />
            </label>
          </div>

          {methodNames.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {methodNames.map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => toggleMethod(m)}
                  className={`rounded-full px-2.5 py-1 text-[11px] font-mono border cursor-pointer ${
                    selectedMethods.includes(m)
                      ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                      : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[var(--text-secondary)]'
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>
          )}

          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={genMut.isPending || selectedMethods.length === 0}
              onClick={() => genMut.mutate(false)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-1.5 font-medium cursor-pointer"
            >
              <Play className="w-3.5 h-3.5" /> 生成 CLI 预检 (--plan)
            </button>
          </div>

          {genResult && (
            <pre className="m-0 rounded-lg bg-[var(--bg-elevated)] p-3 font-mono text-[11px] overflow-x-auto">
              {genResult.cli_command}
            </pre>
          )}
        </div>
      )}
    </PageTransition>
  );
}
