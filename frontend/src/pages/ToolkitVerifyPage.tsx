import React, { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Award, CheckCircle2, FileCheck2, FileUp, FolderKanban, ShieldCheck, Trash2 } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDateShort, fmtPct } from '../lib/format';
import { useAppStore } from '../lib/store';
import { PageTransition, SlidingTabs } from '../components/motion/primitives';
import { RowActionIcon } from '../components/ui/RowActionIcon';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

type VerifyTab = 'calc' | 'report' | 'manage';

interface BatchOpt {
  batch_id: string;
  batch_name: string;
  runner: string;
  run_scope: string;
  official_status: string;
  source_namespace: string;
}

interface OfflineReportRow {
  report_id: string;
  project_id: string;
  report_name: string;
  upload_format: 'shared_batch_or_episodes' | 'native_harbor_job' | 'summary_only';
  adapter_used: string;
  model_name: string | null;
  dataset_name: string | null;
  status: string;
  created_at: string;
  summary: Record<string, unknown>;
}

export function ToolkitVerifyPage() {
  const user = useAppStore((s) => s.user);
  const pushToast = useAppStore((s) => s.pushToast);
  const canWrite = Boolean(user && ['researcher', 'admin'].includes(user.role));
  const isAdmin = user?.role === 'admin';

  const [tab, setTab] = useState<VerifyTab>('calc');
  const [selectedBatchId, setSelectedBatchId] = useState('');
  const [checkSmoke, setCheckSmoke] = useState(false);
  const [checkGw, setCheckGw] = useState(false);
  const [verifyOutput, setVerifyOutput] = useState<Record<string, unknown> | null>(null);

  const [reportName, setReportName] = useState('离线验证报告');
  const [uploadFilename, setUploadFilename] = useState('official-result.json');
  const [uploadContent, setUploadContent] = useState(
    '{"dataset":"CMMMU","metric":"accuracy","score":0.6667,"denominator":6,"source":"vlmevalkit/metrics.json"}'
  );
  const [activeReport, setActiveReport] = useState<OfflineReportRow | null>(null);

  // Admin official result attach
  const [offDataset, setOffDataset] = useState('');
  const [offMetric, setOffMetric] = useState('accuracy');
  const [offScore, setOffScore] = useState('0.5');
  const [offDenom, setOffDenom] = useState('10');
  const [offSource, setOffSource] = useState('official-scorer.json');

  const batchesQ = useQuery({
    queryKey: ['toolkit-verify-batches'],
    queryFn: () => apiFetch<{ items: BatchOpt[] }>('/api/v1/batches?page_size=100'),
  });

  const reportsQ = useQuery({
    queryKey: ['toolkit-offline-reports'],
    queryFn: () => apiFetch<{ items: OfflineReportRow[] }>('/api/v1/toolkit/offline-reports'),
  });

  const harborBatches = (batchesQ.data?.items || []).filter((b) => b.source_namespace === 'harbor_runs');
  const effectiveBatchId = selectedBatchId || harborBatches[0]?.batch_id || '';

  const verifyMut = useMutation({
    mutationFn: () =>
      apiFetch<Record<string, unknown>>(`/api/v1/toolkit/verify/${encodeURIComponent(effectiveBatchId)}`, {
        method: 'POST',
        body: JSON.stringify({ check_smoke: checkSmoke, check_gateway_capture: checkGw }),
      }),
    onSuccess: (res) => {
      setVerifyOutput(res);
      pushToast('success', '已完成 report.py 只读验收');
    },
    onError: (err: Error) => pushToast('error', err.message),
  });

  const offlineCalcMut = useMutation({
    mutationFn: () =>
      apiFetch<OfflineReportRow>('/api/v1/toolkit/offline-calc', {
        method: 'POST',
        body: JSON.stringify({
          report_name: reportName.trim(),
          filename: uploadFilename.trim(),
          content: uploadContent,
          project_id: 'default',
        }),
      }),
    onSuccess: (res) => {
      setActiveReport(res);
      setTab('report');
      pushToast('success', `离线算分完成（分流格式: ${res.upload_format} · 适配器: ${res.adapter_used}）`);
      reportsQ.refetch();
    },
    onError: (err: Error) => pushToast('error', `离线算分失败：${err.message}`),
  });

  const deleteOffMut = useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/toolkit/offline-reports/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    onSuccess: () => {
      pushToast('success', '已删除离线报告并清理 0700 待审目录');
      reportsQ.refetch();
    },
  });

  const attachOfficialMut = useMutation({
    mutationFn: () =>
      apiFetch<{ run_scope: string; official_status: string }>(`/api/v1/toolkit/official-result/${encodeURIComponent(effectiveBatchId)}`, {
        method: 'PUT',
        body: JSON.stringify({
          dataset: offDataset.trim() || undefined,
          metric: offMetric.trim(),
          score: Number(offScore),
          denominator: Number(offDenom),
          source: offSource.trim(),
        }),
      }),
    onSuccess: (res) => {
      pushToast('success', `官方分挂载成功（official_status=${res.official_status}，run_scope=${res.run_scope} 保持不变）`);
      batchesQ.refetch();
    },
    onError: (err: Error) => pushToast('error', err.message),
  });

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <FileCheck2 className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>Offline Verify 离线校验与三格式分流算分</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            共享批次 report.py 只读验真 · 0700 隔离待审区三格式路由（shared / native Harbor / summary_only）· 管理员官方分挂载
          </p>
        </div>
        <SlidingTabs
          layoutId="toolkit-verify-tabs"
          size="sm"
          value={tab}
          onChange={(v) => setTab(v as VerifyTab)}
          options={[
            { id: 'calc', label: 'Benchmark Calculation', icon: <FileUp className="w-3.5 h-3.5" /> },
            { id: 'report', label: 'Offline Report', icon: <CheckCircle2 className="w-3.5 h-3.5" /> },
            { id: 'manage', label: 'Report Management', icon: <FolderKanban className="w-3.5 h-3.5" /> },
          ]}
        />
      </div>

      {tab === 'calc' && (
        <div className="space-y-4 text-xs">
          {/* 1. Existing Batch Verification */}
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3">
            <div className="font-bold text-sm flex items-center gap-1.5">
              <ShieldCheck className="w-4 h-4 text-[var(--brand-primary)]" />
              现有共享批次一键 report.py 验收
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <select
                aria-label="选择共享批次"
                value={effectiveBatchId}
                onChange={(e) => setSelectedBatchId(e.target.value)}
                className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono text-xs min-w-[280px]"
              >
                {harborBatches.map((b) => (
                  <option key={b.batch_id} value={b.batch_id}>
                    {b.batch_name} ({b.runner} · {b.run_scope})
                  </option>
                ))}
              </select>
              <label className="inline-flex items-center gap-1.5">
                <input type="checkbox" checked={checkSmoke} onChange={(e) => setCheckSmoke(e.target.checked)} />
                --check-smoke
              </label>
              <label className="inline-flex items-center gap-1.5">
                <input type="checkbox" checked={checkGw} onChange={(e) => setCheckGw(e.target.checked)} />
                --check-gateway-capture
              </label>
              {canWrite && (
                <button
                  type="button"
                  disabled={!effectiveBatchId || verifyMut.isPending}
                  onClick={() => verifyMut.mutate()}
                  className="rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-3 py-1.5 text-white font-semibold cursor-pointer"
                >
                  运行 report.py 验收
                </button>
              )}
            </div>
            {verifyOutput && (
              <pre className="m-0 rounded-lg bg-[var(--bg-elevated)] p-3 font-mono text-[11px] max-h-64 overflow-y-auto">
                {JSON.stringify(verifyOutput, null, 2)}
              </pre>
            )}
          </div>

          {/* 2. Offline 3-Format Calculation */}
          {canWrite && (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3">
              <div className="font-bold text-sm flex items-center gap-1.5">
                <FileUp className="w-4 h-4 text-[var(--brand-primary)]" />
                离线结果包三格式分流算分（落 0700 隔离待审区，上限 50 MB）
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <label className="space-y-1">
                  <span className="text-[var(--text-secondary)]">报告名称</span>
                  <input
                    type="text"
                    value={reportName}
                    onChange={(e) => setReportName(e.target.value)}
                    className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5"
                  />
                </label>
                <label className="space-y-1">
                  <span className="text-[var(--text-secondary)]">文件名（如 episodes.jsonl / result.json / official-result.json）</span>
                  <input
                    type="text"
                    value={uploadFilename}
                    onChange={(e) => setUploadFilename(e.target.value)}
                    className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 font-mono"
                  />
                </label>
              </div>
              <textarea
                rows={5}
                value={uploadContent}
                onChange={(e) => setUploadContent(e.target.value)}
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-2.5 font-mono text-[11px]"
              />
              <button
                type="button"
                disabled={offlineCalcMut.isPending}
                onClick={() => offlineCalcMut.mutate()}
                className="rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] px-3 py-1.5 text-white font-semibold cursor-pointer"
              >
                提交离线算分并入库
              </button>
            </div>
          )}

          {/* 3. Admin Official Result Attachment */}
          {isAdmin && (
            <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3">
              <div className="font-bold text-sm flex items-center gap-1.5">
                <Award className="w-4 h-4 text-[var(--sem-zero-fg)]" />
                管理员官方分挂载（仅写 SQLite official_results，严禁改写 run_scope）
              </div>
              <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
                <input value={offDataset} onChange={(e) => setOffDataset(e.target.value)} placeholder="Dataset 名(可选)" className="rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1" />
                <input value={offMetric} onChange={(e) => setOffMetric(e.target.value)} placeholder="Metric" className="rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1 font-mono" />
                <input value={offScore} onChange={(e) => setOffScore(e.target.value)} placeholder="Score" className="rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1 font-mono" />
                <input value={offDenom} onChange={(e) => setOffDenom(e.target.value)} placeholder="Denominator" className="rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1 font-mono" />
                <input value={offSource} onChange={(e) => setOffSource(e.target.value)} placeholder="Source" className="rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1 font-mono" />
              </div>
              <button
                type="button"
                disabled={!effectiveBatchId || attachOfficialMut.isPending}
                onClick={() => attachOfficialMut.mutate()}
                className="rounded-lg border border-[var(--brand-primary)] text-[var(--brand-primary)] px-3 py-1.5 font-semibold cursor-pointer"
              >
                挂载官方正式分至当前所选批次
              </button>
            </div>
          )}
        </div>
      )}

      {tab === 'report' && (
        activeReport ? (
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 space-y-3 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-bold text-sm">{activeReport.report_name}</div>
              <div className="flex items-center gap-2 font-mono text-[11px]">
                <span className="rounded px-2 py-0.5 bg-[var(--brand-tint)] text-[var(--brand-primary)]">{activeReport.upload_format}</span>
                <span className="rounded px-2 py-0.5 bg-[var(--bg-elevated)] text-[var(--text-secondary)]">{activeReport.adapter_used}</span>
              </div>
            </div>
            <pre className="m-0 rounded-lg bg-[var(--bg-elevated)] p-3 font-mono text-[11px] max-h-[60vh] overflow-y-auto">
              {JSON.stringify(activeReport.summary, null, 2)}
            </pre>
          </div>
        ) : (
          <StateNoticeCard kind="empty" title="请先在 Benchmark Calculation 提交离线包或在 Report Management 选择报告" />
        )
      )}

      {tab === 'manage' && (
        reportsQ.isLoading ? (
          <TableSkeleton rows={4} cols={5} />
        ) : (reportsQ.data?.items || []).length === 0 ? (
          <StateNoticeCard kind="empty" title="暂无已保存的离线校验报告" />
        ) : (
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] overflow-hidden">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)]">
                  <th className="py-2 px-3">报告名</th>
                  <th className="py-2 px-3">格式分流</th>
                  <th className="py-2 px-3">适配器</th>
                  <th className="py-2 px-3">创建时间</th>
                  <th className="py-2 px-3 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {reportsQ.data!.items.map((r) => (
                  <tr key={r.report_id} className="group/row-action hover:bg-[var(--bg-elevated)]/60">
                    <td className="py-2 px-3 font-medium">{r.report_name}</td>
                    <td className="py-2 px-3 font-mono text-[11px]">{r.upload_format}</td>
                    <td className="py-2 px-3 font-mono text-[11px]">{r.adapter_used}</td>
                    <td className="py-2 px-3 text-[var(--text-muted)]">{fmtDateShort(r.created_at)}</td>
                    <td className="py-2 px-3 text-right whitespace-nowrap">
                      <RowActionIcon
                        variant="details"
                        label={`查看离线报告：${r.report_name}`}
                        onClick={() => {
                          setActiveReport(r);
                          setTab('report');
                        }}
                      />
                      {canWrite && (
                        <button
                          type="button"
                          aria-label={`删除离线报告：${r.report_name}`}
                          onClick={() => deleteOffMut.mutate(r.report_id)}
                          className="ml-2 text-[var(--text-muted)] hover:text-[var(--sem-err-fg)] cursor-pointer"
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
        )
      )}
    </PageTransition>
  );
}
