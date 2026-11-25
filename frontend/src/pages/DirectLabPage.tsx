import React, { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { CheckCircle2, GitCompareArrows, ImagePlus, Info, Play, ShieldCheck, Trash2, X, Zap } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDateShort, fmtDeltaPP, fmtPct } from '../lib/format';
import { useAppStore } from '../lib/store';
import { PageTransition } from '../components/motion/primitives';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

interface DirectCapsResp {
  active_adapter_variant: string;
  extended_adapter_status: {
    variant: string;
    enabled: boolean;
    pending_params: string[];
    disclosure: string;
  };
  native_case_schema: {
    required: string[];
    conditional_required: string[];
    optional: string[];
    forbidden_non_native: string[];
  };
  scorers: Array<{
    scorer_id: string;
    display_name: string;
    description: string;
    requires_answer: boolean;
    scorer_sha256: string;
  }>;
  gateway_profiles: Array<{
    profile_id: string;
    display_name: string;
    base_url: string;
    default_model: string;
    key_configured: boolean;
  }>;
}

interface DirectRunRow {
  run_id: string;
  project_id: string;
  suite_id: string;
  cases_sha256: string;
  scorer_id: string;
  scorer_sha256: string;
  adapter_variant: string;
  model_route: string;
  gateway_profile: string;
  summary: {
    report_py_verified?: boolean;
    case_count?: number;
    scored?: number;
    passed?: number;
    valid_mean?: number | null;
  };
  created_at: string;
}

interface DirectCompareResp {
  cases_sha256: string;
  scorer_id: string;
  run_a: { run_id: string; model_route: string; score_pct: number | null };
  run_b: { run_id: string; model_route: string; score_pct: number | null };
  delta_pp: number | null;
  rel_pct: number | null;
  win_tie_loss: { a_wins: number; both_pass: number; both_fail: number; b_wins: number; fault_excluded: number };
  items: Array<{
    task_id: string;
    diff_type: string;
    a: { status: string; reward: number | null; prediction?: string | null };
    b: { status: string; reward: number | null; prediction?: string | null };
  }>;
}

const SAMPLE_CASES = `{"id":"demo-1","prompt":"Compute 21 * 2 and reply with only the integer.","answer":"42"}\n{"id":"demo-2","prompt":"What is the capital of France? Reply with one word.","answer":"Paris"}\n`;

export function DirectLabPage() {
  const user = useAppStore((s) => s.user);
  const pushToast = useAppStore((s) => s.pushToast);
  const canWrite = Boolean(user && ['researcher', 'admin'].includes(user.role));

  const [suiteId, setSuiteId] = useState('direct-native-smoke');
  const [scorerId, setScorerId] = useState('exact_match');
  const [gatewayProfileId, setGatewayProfileId] = useState('qwen197-vllm');
  const [modelRoute, setModelRoute] = useState('Qwen3_8-27B');
  const [casesJsonl, setCasesJsonl] = useState(SAMPLE_CASES);
  const [uploadedImages, setUploadedImages] = useState<Array<{ relPath: string; b64: string; sizeBytes: number; mime: string }>>([]);
  const [valResult, setValResult] = useState<{
    valid: boolean;
    case_count: number;
    image_count?: number;
    cases_sha256: string;
    scorer_sha256: string;
  } | null>(null);
  const [compareIds, setCompareIds] = useState<[string, string]>(['', '']);
  const [abResult, setAbResult] = useState<DirectCompareResp | null>(null);

  const buildImagesB64Payload = (): Record<string, string> | undefined => {
    if (uploadedImages.length === 0) return undefined;
    const map: Record<string, string> = {};
    for (const img of uploadedImages) {
      const key = img.relPath.trim();
      if (key) map[key] = img.b64;
    }
    return Object.keys(map).length > 0 ? map : undefined;
  };

  const handleImageFilesSelected = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const next: Array<{ relPath: string; b64: string; sizeBytes: number; mime: string }> = [];
    for (const file of Array.from(files)) {
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => reject(new Error(`读取图像失败: ${file.name}`));
        reader.readAsDataURL(file);
      });
      const b64 = dataUrl.includes(',') ? dataUrl.split(',', 2)[1] : dataUrl;
      next.push({
        relPath: file.name,
        b64,
        sizeBytes: file.size,
        mime: file.type || 'image/png',
      });
    }
    setUploadedImages((prev) => {
      const byPath = new Map(prev.map((x) => [x.relPath, x]));
      for (const item of next) byPath.set(item.relPath, item);
      return Array.from(byPath.values());
    });
    setValResult(null);
  };

  const capsQ = useQuery({
    queryKey: ['direct-lab-caps'],
    queryFn: () => apiFetch<DirectCapsResp>('/api/v1/direct-lab/capabilities'),
  });

  const runsQ = useQuery({
    queryKey: ['direct-lab-runs'],
    queryFn: () => apiFetch<{ items: DirectRunRow[] }>('/api/v1/direct-lab/runs'),
  });

  const validateMut = useMutation({
    mutationFn: () =>
      apiFetch<{ valid: boolean; case_count: number; image_count?: number; cases_sha256: string; scorer_sha256: string }>('/api/v1/direct-lab/validate', {
        method: 'POST',
        body: JSON.stringify({
          cases_jsonl: casesJsonl,
          scorer_id: scorerId,
          images_b64: buildImagesB64Payload(),
        }),
      }),
    onSuccess: (res) => {
      setValResult(res);
      pushToast(
        'success',
        `通过原生 direct.py:33-37 格式校验（${res.case_count} 题，${res.image_count ?? 0} 图，SHA ${res.cases_sha256.slice(0, 10)}）`
      );
    },
    onError: (err: Error) => {
      setValResult(null);
      pushToast('error', err.message);
    },
  });

  const runMut = useMutation({
    mutationFn: () =>
      apiFetch<DirectRunRow>('/api/v1/direct-lab/runs', {
        method: 'POST',
        body: JSON.stringify({
          suite_id: suiteId.trim(),
          scorer_id: scorerId,
          gateway_profile_id: gatewayProfileId,
          model_route: modelRoute.trim(),
          cases_jsonl: casesJsonl,
          images_b64: buildImagesB64Payload(),
          adapter_variant: 'native_direct_v1',
        }),
      }),
    onSuccess: (res) => {
      pushToast('success', `Direct 试跑完成并经受管 report.py 验真：${res.run_id}`);
      runsQ.refetch();
    },
    onError: (err: Error) => pushToast('error', `试跑失败：${err.message}`),
  });

  const deleteMut = useMutation({
    mutationFn: (runId: string) => apiFetch(`/api/v1/direct-lab/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' }),
    onSuccess: () => {
      pushToast('success', '已删除 Direct 验证记录');
      runsQ.refetch();
    },
  });

  const compareMut = useMutation({
    mutationFn: () =>
      apiFetch<DirectCompareResp>('/api/v1/direct-lab/compare', {
        method: 'POST',
        body: JSON.stringify({ run_a_id: compareIds[0], run_b_id: compareIds[1] }),
      }),
    onSuccess: (res) => setAbResult(res),
    onError: (err: Error) => pushToast('error', err.message),
  });

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div>
        <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
          <Zap className="w-5 h-5 text-[var(--brand-primary)]" />
          <span>Direct Lab 原生评测验证工作台</span>
        </h1>
        <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
          严格遵循 direct.py:33-37 原生字段（id / prompt / 可选相对 images / 条件必填 answer）· 服务端白名单评分器与 0600 网关 Profile · 同指纹 A/B 对照
        </p>
      </div>

      {/* Explicit native_direct_v1 vs extended_adapter_pending Disclosure Banner */}
      {capsQ.data && (
        <div className="rounded-xl border border-[var(--sem-zero-border)] bg-[var(--sem-zero-bg)] p-3.5 text-xs flex items-start gap-2.5">
          <Info className="w-4 h-4 text-[var(--sem-zero-fg)] shrink-0 mt-0.5" />
          <div>
            <div className="font-semibold text-[var(--text-primary)]">
              当前激活契约：<code>{capsQ.data.active_adapter_variant}</code> · 待适配扩展项：<code>{capsQ.data.extended_adapter_status.variant}</code>
            </div>
            <div className="mt-1 text-[var(--text-secondary)]">{capsQ.data.extended_adapter_status.disclosure}</div>
          </div>
        </div>
      )}

      {canWrite && (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3 text-xs">
          <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
            <label className="space-y-1">
              <span className="text-[var(--text-secondary)]">套件 ID (suite_id)</span>
              <input
                type="text"
                value={suiteId}
                onChange={(e) => setSuiteId(e.target.value)}
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs"
              />
            </label>
            <label className="space-y-1">
              <span className="text-[var(--text-secondary)]">服务端白名单评分器</span>
              <select
                value={scorerId}
                onChange={(e) => setScorerId(e.target.value)}
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs"
              >
                {(capsQ.data?.scorers || []).map((s) => (
                  <option key={s.scorer_id} value={s.scorer_id}>
                    {s.display_name}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-[var(--text-secondary)]">服务端 Gateway Profile (0600)</span>
              <select
                value={gatewayProfileId}
                onChange={(e) => setGatewayProfileId(e.target.value)}
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs"
              >
                {(capsQ.data?.gateway_profiles || []).map((g) => (
                  <option key={g.profile_id} value={g.profile_id}>
                    {g.display_name} {g.key_configured ? '✓' : '(未配密钥)'}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-[var(--text-secondary)]">模型路由 (model_route)</span>
              <input
                type="text"
                value={modelRoute}
                onChange={(e) => setModelRoute(e.target.value)}
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs"
              />
            </label>
          </div>

          <label className="block space-y-1">
            <span className="text-[var(--text-secondary)]">
              原生 cases.jsonl（每行 JSON 须含唯一 id 与字符串 prompt，严禁使用非原生 messages / expected）
            </span>
            <textarea
              rows={5}
              value={casesJsonl}
              onChange={(e) => {
                setCasesJsonl(e.target.value);
                setValResult(null);
              }}
              className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-2.5 font-mono text-[11px]"
            />
          </label>

          {/* Companion Multimodal Images Upload (bound by SHA-256 into cases_sha256) */}
          <div className="rounded-lg border border-dashed border-[var(--border-strong)] bg-[var(--bg-elevated)]/55 p-3 space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-1.5 font-medium text-[var(--text-primary)]">
                <ImagePlus className="w-3.5 h-3.5 text-[var(--brand-primary)]" />
                <span>配套图像上传（当 cases.jsonl 含 <code>images</code> 相对路径时必填，实际图像字节摘要纳入 <code>cases_sha256</code>）</span>
              </div>
              <label className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1 text-[11px] font-medium hover:bg-[var(--bg-elevated)] cursor-pointer">
                <span>选择图像文件 (PNG/JPEG/WEBP/GIF)</span>
                <input
                  type="file"
                  aria-label="上传 Direct 配套图像"
                  multiple
                  accept="image/png,image/jpeg,image/webp,image/gif"
                  onChange={(e) => {
                    void handleImageFilesSelected(e.target.files);
                    e.target.value = '';
                  }}
                  className="hidden"
                />
              </label>
            </div>
            {uploadedImages.length > 0 && (
              <div className="space-y-1.5">
                {uploadedImages.map((img, idx) => (
                  <div
                    key={`${img.relPath}-${idx}`}
                    className="flex items-center gap-2 rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5"
                  >
                    <span className="text-[10px] text-[var(--text-muted)] shrink-0">相对路径:</span>
                    <input
                      type="text"
                      aria-label={`图像相对路径 ${idx + 1}`}
                      value={img.relPath}
                      onChange={(e) => {
                        const nextPath = e.target.value;
                        setUploadedImages((prev) => prev.map((it, i) => (i === idx ? { ...it, relPath: nextPath } : it)));
                        setValResult(null);
                      }}
                      className="flex-1 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-0.5 font-mono text-[11px]"
                    />
                    <span className="text-[10px] font-mono text-[var(--text-muted)] shrink-0">
                      {(img.sizeBytes / 1024).toFixed(1)} KB
                    </span>
                    <button
                      type="button"
                      aria-label={`移除图像 ${img.relPath}`}
                      onClick={() => {
                        setUploadedImages((prev) => prev.filter((_, i) => i !== idx));
                        setValResult(null);
                      }}
                      className="text-[var(--text-muted)] hover:text-[var(--sem-err-fg)] cursor-pointer"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={validateMut.isPending}
              onClick={() => validateMut.mutate()}
              className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-1.5 font-medium cursor-pointer"
            >
              <ShieldCheck className="w-3.5 h-3.5 text-[var(--brand-primary)]" /> 校验原生格式
            </button>
            <button
              type="button"
              disabled={runMut.isPending}
              onClick={() => runMut.mutate()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-3 py-1.5 font-semibold text-white cursor-pointer"
            >
              <Play className="w-3.5 h-3.5" /> 发起 Direct 试跑（含 report.py 验真）
            </button>
            {valResult && (
              <span className="inline-flex items-center gap-1 text-[11px] text-[var(--sem-pass-fg)] font-mono">
                <CheckCircle2 className="w-3.5 h-3.5" /> {valResult.case_count} 题 · {valResult.image_count ?? 0} 图 · cases_sha={valResult.cases_sha256.slice(0, 12)}
              </span>
            )}
          </div>
        </div>
      )}

      {/* Historical Direct Runs & Fingerprint-Locked A/B Diff */}
      <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
        <div className="px-4 py-3 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="font-bold">历史 Direct 验证记录（锁定 cases_sha256 + scorer_sha256）</span>
          <div className="flex items-center gap-2">
            <select
              aria-label="A/B Run A"
              value={compareIds[0]}
              onChange={(e) => setCompareIds([e.target.value, compareIds[1]])}
              className="rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2 py-1 text-[11px] font-mono"
            >
              <option value="">选择 Run A</option>
              {(runsQ.data?.items || []).map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {r.run_id} ({r.model_route})
                </option>
              ))}
            </select>
            <span>vs</span>
            <select
              aria-label="A/B Run B"
              value={compareIds[1]}
              onChange={(e) => setCompareIds([compareIds[0], e.target.value])}
              className="rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2 py-1 text-[11px] font-mono"
            >
              <option value="">选择 Run B</option>
              {(runsQ.data?.items || []).map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {r.run_id} ({r.model_route})
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={!compareIds[0] || !compareIds[1] || compareMut.isPending}
              onClick={() => compareMut.mutate()}
              className="inline-flex items-center gap-1 rounded-lg bg-[var(--brand-primary)] disabled:opacity-40 px-2.5 py-1 text-[11px] font-semibold text-white cursor-pointer"
            >
              <GitCompareArrows className="w-3.5 h-3.5" /> 同指纹 A/B 对照
            </button>
          </div>
        </div>

        {runsQ.isLoading ? (
          <TableSkeleton rows={4} cols={5} />
        ) : (runsQ.data?.items || []).length === 0 ? (
          <div className="p-6">
            <StateNoticeCard kind="empty" title="尚无 Direct 验证记录" description="可在上方校验并运行原生 cases.jsonl，或配置 0600 gateways.env 后试跑模型。" />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                  <th className="py-2 px-3">Run ID</th>
                  <th className="py-2 px-3">套件 / 模型</th>
                  <th className="py-2 px-3">评分器 & 指纹</th>
                  <th className="py-2 px-3 text-right">得分 / 通过</th>
                  <th className="py-2 px-3 text-right">时间 / 操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)] tabular-nums">
                {runsQ.data!.items.map((r) => (
                  <tr key={r.run_id}>
                    <td className="py-2 px-3 font-mono font-semibold">{r.run_id}</td>
                    <td className="py-2 px-3">
                      <div className="font-medium">{r.suite_id}</div>
                      <div className="text-[10px] text-[var(--text-muted)]">{r.model_route} · {r.gateway_profile}</div>
                    </td>
                    <td className="py-2 px-3 font-mono text-[10px] text-[var(--text-secondary)]">
                      {r.scorer_id} · cases={r.cases_sha256.slice(0, 8)}
                    </td>
                    <td className="py-2 px-3 text-right font-semibold">
                      {r.summary.valid_mean !== null && r.summary.valid_mean !== undefined
                        ? fmtPct(r.summary.valid_mean * 100)
                        : '—'}{' '}
                      <span className="text-[10px] text-[var(--text-muted)]">
                        ({r.summary.passed ?? 0}/{r.summary.scored ?? 0})
                      </span>
                    </td>
                    <td className="py-2 px-3 text-right">
                      <span className="text-[10px] text-[var(--text-muted)] mr-2">{fmtDateShort(r.created_at)}</span>
                      {canWrite && (
                        <button
                          type="button"
                          onClick={() => deleteMut.mutate(r.run_id)}
                          className="text-[var(--text-muted)] hover:text-[var(--sem-err-fg)] cursor-pointer"
                        >
                          <Trash2 className="w-3.5 h-3.5 inline" />
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {abResult && (
        <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 text-xs space-y-2">
          <div className="font-bold">
            同指纹 A/B 对比：{abResult.run_a.model_route} ({fmtPct(abResult.run_a.score_pct)}) vs {abResult.run_b.model_route} ({fmtPct(abResult.run_b.score_pct)}) · Δ {fmtDeltaPP(abResult.delta_pp)}
          </div>
          <div className="text-[11px] text-[var(--text-secondary)] tabular-nums">
            A 胜 {abResult.win_tie_loss.a_wins} · 同过 {abResult.win_tie_loss.both_pass} · 同错 {abResult.win_tie_loss.both_fail} · B 胜 {abResult.win_tie_loss.b_wins}
          </div>
        </div>
      )}
    </PageTransition>
  );
}
