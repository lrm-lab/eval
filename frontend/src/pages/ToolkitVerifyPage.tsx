import { DismissibleDetails } from '../components/ui/DismissibleDetails';
import { useDisclosureLayer } from '../lib/dismissibleLayer';
import { useDeferredValue, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Award, ChevronDown, FileCheck2, FileUp, FolderKanban, Trash2 } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { fmtDateShort, fmtPct, runScopeLabel } from '../lib/format';
import { useAppStore } from '../lib/store';
import { CollapsiblePanel, PageTransition } from '../components/motion/primitives';
import { RowActionIcon } from '../components/ui/RowActionIcon';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

type Task = 'verify' | 'import' | 'reports' | 'official';
type JsonRecord = Record<string, unknown>;

interface BatchOption {
  batch_id: string;
  batch_name: string;
  runner: string;
  run_scope: string;
  official_status: string;
  raw_dataset_name: string | null;
}

interface Verification {
  batch_id: string;
  batch_name: string;
  exit_code: number;
  passed_checks: boolean;
  report: JsonRecord;
  stderr: string;
}

const FORMAT_LABELS = {
  shared_batch_or_episodes: '共享批次 / Episodes',
  native_harbor_job: '原生 Harbor',
  summary_only: '仅汇总指标',
};

interface OfflineReport {
  report_id: string;
  project_id: string;
  report_name: string;
  upload_format: keyof typeof FORMAT_LABELS;
  adapter_used: string;
  model_name: string | null;
  dataset_name: string | null;
  created_at: string;
  summary: JsonRecord;
}

interface SavedOfficialScore {
  batch_id: string;
  run_scope: string;
  official_status: string;
}

const TASKS = [
  { id: 'verify', title: '校验已有批次', description: '检查已发布的共享批次，查看校验结果。', icon: FileCheck2 },
  { id: 'import', title: '导入离线结果', description: '选择结果文件或粘贴内容，生成离线报告。', icon: FileUp },
  { id: 'reports', title: '报告', description: '查看和管理已保存的离线报告。', icon: FolderKanban },
] as const;

const fieldStyle = 'w-full min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-sm text-[var(--text-primary)] disabled:opacity-60';
const primaryButton = 'inline-flex w-fit items-center gap-1.5 rounded-lg bg-[var(--brand-primary)] px-4 py-2 text-sm font-semibold text-white hover:bg-[var(--brand-hover)] disabled:cursor-not-allowed disabled:opacity-45';
const secondaryButton = 'inline-flex w-fit items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm font-medium text-[var(--text-primary)] hover:bg-[var(--bg-elevated)] disabled:opacity-45';
const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {};
}

function valueLabel(value: unknown): string {
  if (typeof value === 'number') return String(value);
  return typeof value === 'string' && value ? value : '—';
}

function Feedback({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return (
    <div
      role={error ? 'alert' : 'status'}
      className={`break-words rounded-lg border px-3 py-2 text-sm ${
        error
          ? 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]'
          : 'border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] text-[var(--sem-pass-fg)]'
      }`}
    >
      {children}
    </div>
  );
}

function RawJson({ data }: { data: JsonRecord }) {
  return (
    <DismissibleDetails className="group rounded-lg border border-[var(--border-subtle)]" summary={(
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 rounded-lg px-3 py-2 text-sm font-medium hover:bg-[var(--bg-elevated)]">
        原始 JSON
        <ChevronDown aria-hidden="true" className="size-4 text-[var(--text-secondary)] group-open:rotate-180" />
      </summary>
    )}>
      {() => (
        <pre className="m-0 max-h-[55vh] overflow-auto border-t border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 font-mono text-xs">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </DismissibleDetails>
  );
}

function TotalsSummary({ data }: { data: unknown }) {
  const totals = record(data);
  const metrics = ([
    ['planned', '计划'],
    ['recorded', '已记录'],
    ['scored', '有效计分'],
    ['passed', '通过'],
    ['valid_mean', '有效均分'],
  ] as const).flatMap(([key, label]) => {
    const value = totals[key];
    if (typeof value !== 'number' || !Number.isFinite(value)) return [];
    return [{ label, value: key === 'valid_mean' ? fmtPct(value * 100) : value }];
  });

  return metrics.length > 0 && (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
      {metrics.map(({ label, value }) => (
        <div key={label} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
          <dt className="text-xs text-[var(--text-secondary)]">{label}</dt>
          <dd className="mt-1 text-lg font-semibold tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function BatchPicker({
  userId, verify = false, selected, onChange, disabled,
}: {
  userId: string | undefined;
  verify?: boolean;
  selected: BatchOption | null;
  onChange: (batch: BatchOption | null) => void;
  disabled: boolean;
}) {
  const [search, setSearch] = useState('');
  const query = useDeferredValue(search.trim());
  const batches = useQuery({
    queryKey: ['toolkit-verify-batches', userId, verify, query],
    queryFn: async ({ signal }) => {
      const namespaces = verify ? ['harbor_runs', 'runner_published'] : [''];
      const pages = await Promise.all(namespaces.map((namespace) => {
        const params = new URLSearchParams({ page_size: '100' });
        if (namespace) params.set('source_namespace', namespace);
        if (query) params.set('q', query);
        return apiFetch<{ items: BatchOption[]; total: number }>(`/api/v1/batches?${params}`, { signal });
      }));
      return { items: pages.flatMap((page) => page.items), total: pages.reduce((sum, page) => sum + page.total, 0) };
    },
  });
  const items = batches.data?.items ?? [];
  const options = selected && !items.some((batch) => batch.batch_id === selected.batch_id) ? [selected, ...items] : items;
  const label = verify ? '选择共享批次' : '选择官方分目标批次';

  return (
    <div className="space-y-2">
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <label className="space-y-1 text-sm">
          <span className="block font-medium">搜索批次</span>
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') event.preventDefault(); }}
            disabled={disabled}
            placeholder="输入批次名"
            className={fieldStyle}
          />
        </label>
        <label className="space-y-1 text-sm">
          <span className="block font-medium">{label}</span>
          <select
            aria-label={label}
            value={selected?.batch_id ?? ''}
            onChange={(event) => onChange(options.find((batch) => batch.batch_id === event.target.value) ?? null)}
            disabled={disabled}
            required
            className={fieldStyle}
          >
            <option value="">{batches.isPending ? '正在加载批次…' : '请选择批次'}</option>
            {options.map((batch) => (
              <option key={batch.batch_id} value={batch.batch_id}>
                {batch.batch_name} · {batch.runner} · {runScopeLabel(batch.run_scope)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {batches.error && <Feedback error>加载批次失败。{batches.error.message}</Feedback>}
      {batches.data && items.length === 0 && <p className="text-sm text-[var(--text-secondary)]">没有匹配的批次。</p>}
      {batches.data && batches.data.total > items.length && <p className="text-xs text-[var(--text-secondary)]">可以按批次名查找更多结果。</p>}
    </div>
  );
}

export function ToolkitVerifyPage() {
  const user = useAppStore((store) => store.user);
  const queryClient = useQueryClient();
  const canWrite = user?.role === 'researcher' || user?.role === 'admin';
  const isAdmin = user?.role === 'admin';
  const userId = user?.user_id;
  const reportsKey = ['toolkit-offline-reports', userId];
  const reportKey = (id: string) => ['toolkit-offline-report', userId, id];

  const [task, setTask] = useState<Task | null>(null);
  const officialDisclosure = useDisclosureLayer<HTMLElement>(task === 'official', () => setTask(null));
  const [verifyBatch, setVerifyBatch] = useState<BatchOption | null>(null);
  const [checkSmoke, setCheckSmoke] = useState(false);
  const [checkGateway, setCheckGateway] = useState(false);
  const [reportName, setReportName] = useState('');
  const [project, setProject] = useState('');
  const [importMethod, setImportMethod] = useState<'file' | 'paste'>('file');
  const [file, setFile] = useState<File | null>(null);
  const [pasteFilename, setPasteFilename] = useState('');
  const [pasteContent, setPasteContent] = useState('');
  const [reportId, setReportId] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const deleteDisclosure = useDisclosureLayer<HTMLDivElement>(confirmDelete, () => setConfirmDelete(false));
  const [deletedName, setDeletedName] = useState('');
  const [officialBatch, setOfficialBatch] = useState<BatchOption | null>(null);
  const [official, setOfficial] = useState({ dataset: '', metric: '', score: '', denominator: '', source: '' });

  const projects = [...new Set([...(user?.projects ?? []), ...(userId ? [`personal:${userId}`] : []), ...(isAdmin ? ['default'] : [])])];
  const projectId = projects.includes(project) ? project : projects.includes('default') ? 'default' : projects[0];
  const reportsQ = useQuery({
    queryKey: reportsKey,
    queryFn: () => apiFetch<{ items: OfflineReport[] }>('/api/v1/toolkit/offline-reports'),
    enabled: task === 'reports' && !reportId,
  });
  const reportQ = useQuery({
    queryKey: reportKey(reportId),
    queryFn: () => apiFetch<OfflineReport>(`/api/v1/toolkit/offline-reports/${encodeURIComponent(reportId)}`),
    enabled: task === 'reports' && Boolean(reportId),
  });

  const verifyMut = useMutation({
    mutationFn: (batch: BatchOption) => apiFetch<Verification>(`/api/v1/toolkit/verify/${encodeURIComponent(batch.batch_id)}`, {
      method: 'POST',
      body: JSON.stringify({ check_smoke: checkSmoke, check_gateway_capture: checkGateway }),
    }),
  });
  const importMut = useMutation({
    mutationFn: () => {
      if (importMethod === 'file' && file) {
        if (file.size === 0 || file.size > MAX_UPLOAD_BYTES) throw new Error('请选择非空且不超过 50 MB 的文件。');
        const form = new FormData();
        form.set('file', file);
        form.set('report_name', reportName.trim());
        form.set('project_id', projectId);
        return apiFetch<OfflineReport>('/api/v1/toolkit/offline-calc', { method: 'POST', body: form });
      }
      if (new Blob([pasteContent]).size > MAX_UPLOAD_BYTES) throw new Error('粘贴内容超过 50 MB。');
      return apiFetch<OfflineReport>('/api/v1/toolkit/offline-calc', {
        method: 'POST',
        body: JSON.stringify({
          report_name: reportName.trim(), project_id: projectId, filename: pasteFilename.trim(), content: pasteContent,
        }),
      });
    },
    onSuccess: (report) => {
      queryClient.setQueryData(reportKey(report.report_id), report);
      void queryClient.invalidateQueries({ queryKey: reportsKey });
      setReportName('');
      setFile(null);
      setPasteFilename('');
      setPasteContent('');
      setDeletedName('');
      setReportId(report.report_id);
      setTask('reports');
    },
  });
  const deleteMut = useMutation({
    mutationFn: (report: OfflineReport) => apiFetch(`/api/v1/toolkit/offline-reports/${encodeURIComponent(report.report_id)}`, { method: 'DELETE' }),
    onSuccess: (_, report) => {
      queryClient.setQueryData<{ items: OfflineReport[] }>(reportsKey, (cached) => cached && {
        ...cached, items: cached.items.filter((item) => item.report_id !== report.report_id),
      });
      void queryClient.invalidateQueries({ queryKey: reportsKey });
      queryClient.removeQueries({ queryKey: reportKey(report.report_id), exact: true });
      setConfirmDelete(false);
      setDeletedName(report.report_name);
      setReportId('');
    },
  });
  const officialMut = useMutation({
    mutationFn: (batch: BatchOption) => apiFetch<SavedOfficialScore>(`/api/v1/toolkit/official-result/${encodeURIComponent(batch.batch_id)}`, {
      method: 'PUT',
      body: JSON.stringify({
        dataset: official.dataset.trim() || undefined,
        metric: official.metric.trim(),
        score: Number(official.score),
        denominator: Number(official.denominator),
        source: official.source.trim(),
      }),
    }),
    onSuccess: (saved) => {
      setOfficialBatch((batch) => batch?.batch_id === saved.batch_id ? { ...batch, ...saved } : batch);
      void queryClient.invalidateQueries({ queryKey: ['toolkit-verify-batches', userId] });
    },
  });

  const verification = verifyMut.data;
  const report = reportQ.data;
  const reportOfficial = record(report?.summary.official_result);
  const hasOfficialSummary = Object.keys(reportOfficial).length > 0;
  const pending = verifyMut.isPending || importMut.isPending || deleteMut.isPending || officialMut.isPending;
  const canImport = reportName.trim() && projectId && (importMethod === 'file' ? file : pasteFilename.trim() && pasteContent.trim());
  const canSave = officialBatch && official.metric.trim() && official.source.trim() && official.score.trim() &&
    Number.isFinite(Number(official.score)) && Number.isSafeInteger(Number(official.denominator)) && Number(official.denominator) > 0;

  function openReport(id: string) {
    deleteMut.reset();
    setConfirmDelete(false);
    setDeletedName('');
    setReportId(id);
  }

  function submit(event: FormEvent<HTMLFormElement>, run: () => void) {
    event.preventDefault();
    run();
  }

  return (
    <PageTransition className="mx-auto max-w-7xl space-y-5 px-5 py-6">
      <header>
        <h1 className="flex items-center gap-2 text-xl font-bold text-[var(--text-primary)]">
          <FileCheck2 aria-hidden="true" className="size-5 shrink-0 text-[var(--brand-primary)]" />
          离线校验与算分
        </h1>
      </header>

      <section aria-label="离线校验任务" className="grid gap-3 sm:grid-cols-3">
        {TASKS.map(({ id, title, description, icon: Icon }) => {
          const locked = id !== 'reports' && !canWrite;
          return (
            <button
              key={id}
              type="button"
              aria-label={title}
              aria-pressed={task === id}
              disabled={locked || pending}
              onClick={() => {
                if (id === 'reports') openReport('');
                setTask(id);
              }}
              className={`flex min-h-22 items-center gap-3 rounded-xl border p-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                task === id
                  ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)]'
                  : 'border-[var(--border-subtle)] bg-[var(--bg-surface)] hover:border-[var(--brand-primary)]'
              }`}
            >
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-[var(--brand-tint)] text-[var(--brand-primary)]">
                <Icon aria-hidden="true" className="size-4" />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-semibold text-[var(--text-primary)]">{title}</span>
                <span className="block text-xs leading-5 text-[var(--text-secondary)]">{locked ? '需研究员或管理员权限' : description}</span>
              </span>
            </button>
          );
        })}
      </section>

      {task === 'verify' && canWrite && (
        <section aria-labelledby="verify-batch-heading" className="space-y-4 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 sm:p-5">
          <h2 id="verify-batch-heading" className="text-base font-bold">校验已有批次</h2>
          <form className="space-y-4" onSubmit={(event) => submit(event, () => verifyBatch && verifyMut.mutate(verifyBatch))}>
            <BatchPicker userId={userId} verify selected={verifyBatch} disabled={verifyMut.isPending} onChange={(batch) => {
              setVerifyBatch(batch);
              verifyMut.reset();
            }} />
            <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm text-[var(--text-secondary)]">
              <label className="inline-flex items-center gap-2">
                <input type="checkbox" checked={checkSmoke} disabled={verifyMut.isPending} onChange={(event) => {
                  setCheckSmoke(event.target.checked);
                  verifyMut.reset();
                }} />
                检查冒烟结果
              </label>
              <label className="inline-flex items-center gap-2">
                <input type="checkbox" checked={checkGateway} disabled={verifyMut.isPending} onChange={(event) => {
                  setCheckGateway(event.target.checked);
                  verifyMut.reset();
                }} />
                检查网关记录
              </label>
            </div>
            <button type="submit" disabled={!verifyBatch || verifyMut.isPending} className={primaryButton}>
              {verifyMut.isPending ? '正在校验…' : '运行批次校验'}
            </button>
            {verifyMut.error && <Feedback error>校验请求失败。{verifyMut.error.message}</Feedback>}
          </form>
          {verification && (
            <div className="space-y-3 border-t border-[var(--border-subtle)] pt-4">
              <Feedback error={!verification.passed_checks}>
                {verification.passed_checks ? '批次校验通过' : '批次校验未通过'} · {verification.batch_name}
              </Feedback>
              <TotalsSummary data={verification.report.totals} />
              <RawJson data={{ ...verification }} />
            </div>
          )}
        </section>
      )}

      {task === 'import' && canWrite && (
        <section aria-labelledby="offline-import-heading" className="space-y-4 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 sm:p-5">
          <h2 id="offline-import-heading" className="text-base font-bold">导入离线结果</h2>
          <form onSubmit={(event) => submit(event, () => canImport && importMut.mutate())}>
            <fieldset disabled={importMut.isPending} className="min-w-0 space-y-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="space-y-1 text-sm">
                  <span className="block font-medium">报告名称</span>
                  <input value={reportName} onChange={(event) => setReportName(event.target.value)} required className={fieldStyle} />
                </label>
                <label className="space-y-1 text-sm">
                  <span className="block font-medium">保存到项目</span>
                  <select aria-label="保存到项目" value={projectId ?? ''} onChange={(event) => setProject(event.target.value)} className={fieldStyle}>
                    {projects.map((id) => <option key={id} value={id}>{id === `personal:${userId}` ? '个人项目' : id}</option>)}
                  </select>
                </label>
              </div>
              <fieldset className="flex flex-wrap gap-4 text-sm">
                <legend className="mb-2 font-medium">结果来源</legend>
                <label className="inline-flex items-center gap-2">
                  <input type="radio" name="offline-import-source" checked={importMethod === 'file'} onChange={() => setImportMethod('file')} />
                  选择本地文件
                </label>
                <label className="inline-flex items-center gap-2">
                  <input type="radio" name="offline-import-source" checked={importMethod === 'paste'} onChange={() => setImportMethod('paste')} />
                  粘贴内容
                </label>
              </fieldset>
              {importMethod === 'file' ? (
                <div className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
                  <input
                    id="offline-result-file"
                    type="file"
                    accept=".json,.jsonl,.zip,.tar,.gz,.tgz"
                    onChange={(event) => {
                      const selected = event.target.files?.[0];
                      if (selected) setFile(selected);
                      event.target.value = '';
                      importMut.reset();
                    }}
                    className="peer sr-only"
                  />
                  <label htmlFor="offline-result-file" className={`${secondaryButton} cursor-pointer peer-focus-visible:outline-2 peer-focus-visible:outline-[var(--brand-primary)]`}>
                    <FileUp aria-hidden="true" className="size-4 text-[var(--brand-primary)]" /> 离线结果文件
                  </label>
                  <span className="min-w-0 flex-1 break-all text-sm text-[var(--text-secondary)]">{file?.name ?? '尚未选择文件'}</span>
                  {file && (
                    <button type="button" className={secondaryButton} onClick={() => {
                      setFile(null);
                      importMut.reset();
                    }}>移除文件</button>
                  )}
                </div>
              ) : (
                <div className="space-y-3">
                  <label className="block space-y-1 text-sm">
                    <span className="block font-medium">原始文件名</span>
                    <input value={pasteFilename} onChange={(event) => setPasteFilename(event.target.value)} placeholder="例如 episodes.jsonl" required className={fieldStyle} />
                  </label>
                  <label className="block space-y-1 text-sm">
                    <span className="block font-medium">JSON 或 JSONL 内容</span>
                    <textarea
                      aria-label="JSON 或 JSONL 内容"
                      rows={6}
                      value={pasteContent}
                      onChange={(event) => setPasteContent(event.target.value)}
                      required
                      className={`${fieldStyle} font-mono`}
                    />
                  </label>
                </div>
              )}
              <p className="text-xs leading-relaxed text-[var(--text-secondary)]">
                自动识别共享批次（episodes.jsonl）、原生 Harbor（result.json）和汇总结果（如 official-result.json）。
                支持 JSON、JSONL、ZIP 和 TAR 归档，上限 50 MB。导入内容保存在隔离待审区。
              </p>
              <button type="submit" disabled={!canImport || importMut.isPending} className={primaryButton}>
                {importMut.isPending ? '正在导入…' : '导入并生成报告'}
              </button>
              {importMut.error && <Feedback error>导入失败。{importMut.error.message}</Feedback>}
            </fieldset>
          </form>
        </section>
      )}

      {task === 'reports' && (
        <section aria-labelledby="offline-reports-heading" className="space-y-4 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 sm:p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 id="offline-reports-heading" className="text-base font-bold">{reportId ? '报告详情' : '离线报告'}</h2>
            {reportId && (
              <button type="button" onClick={() => openReport('')} disabled={deleteMut.isPending} className={secondaryButton}>
                <ArrowLeft aria-hidden="true" className="size-4" /> 返回报告列表
              </button>
            )}
          </div>
          {deletedName && <Feedback>已删除报告 {deletedName}</Feedback>}
          {reportId ? (
            reportQ.isPending ? <TableSkeleton rows={3} cols={3} />
              : reportQ.error ? <Feedback error>加载报告失败。{reportQ.error.message}</Feedback>
                : report && (
                  <div className="space-y-4">
                    <div className="space-y-1">
                      <h3 className="break-words text-lg font-semibold">{report.report_name}</h3>
                      <p className="break-words text-sm text-[var(--text-secondary)]">
                        {FORMAT_LABELS[report.upload_format] ?? report.upload_format} · {report.project_id} · {fmtDateShort(report.created_at)}
                      </p>
                      <p className="break-words text-xs text-[var(--text-secondary)]">
                        适配器 <code>{report.adapter_used}</code>
                        {report.dataset_name && ` · 数据集 ${report.dataset_name}`}
                        {report.model_name && ` · 模型 ${report.model_name}`}
                      </p>
                    </div>
                    <TotalsSummary data={report.summary.totals} />
                    {report.upload_format === 'summary_only' && <p className="text-sm text-[var(--text-secondary)]">这份文件只包含汇总指标。</p>}
                    {hasOfficialSummary && (
                      <dl className="grid gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
                        {([['指标', 'metric'], ['得分', 'score'], ['分母', 'denominator'], ['来源', 'source']] as const).map(([name, key]) => (
                          <div key={key} className="min-w-0">
                            <dt className="text-xs text-[var(--text-secondary)]">{name}</dt>
                            <dd className="break-words font-medium">{valueLabel(reportOfficial[key])}</dd>
                          </div>
                        ))}
                      </dl>
                    )}
                    <RawJson key={report.report_id} data={report.summary} />
                    {canWrite && (
                      <div {...deleteDisclosure.layerProps} className="space-y-2 border-t border-[var(--border-subtle)] pt-3">
                        <button ref={deleteDisclosure.triggerRef} type="button" aria-expanded={confirmDelete}
                          aria-controls="verify-delete-panel" disabled={deleteMut.isPending}
                          onClick={() => setConfirmDelete((open) => !open)} className={secondaryButton}>
                          <Trash2 aria-hidden="true" className="size-4 text-[var(--sem-err-fg)]" /> 删除报告
                        </button>
                        <CollapsiblePanel id="verify-delete-panel" open={confirmDelete}>
                          <div className="space-y-2">
                            <p className="text-sm text-[var(--text-secondary)]">删除报告及其隔离待审文件？</p>
                            <div className="flex flex-wrap gap-2">
                              <button
                                type="button"
                                disabled={deleteMut.isPending}
                                onClick={() => deleteMut.mutate(report)}
                                className="rounded-lg border border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] px-3 py-2 text-sm font-semibold text-[var(--sem-err-fg)] disabled:opacity-45"
                              >
                                {deleteMut.isPending ? '正在删除…' : '确认删除'}
                              </button>
                              <button type="button" disabled={deleteMut.isPending} onClick={() => {
                                setConfirmDelete(false);
                                deleteMut.reset();
                              }} className={secondaryButton}>取消</button>
                            </div>
                          </div>
                        </CollapsiblePanel>
                        {deleteMut.error && <Feedback error>删除报告失败。{deleteMut.error.message}</Feedback>}
                      </div>
                    )}
                  </div>
                )
          ) : reportsQ.isPending ? <TableSkeleton rows={4} cols={3} />
            : reportsQ.error ? <Feedback error>加载报告失败。{reportsQ.error.message}</Feedback>
              : !reportsQ.data?.items.length ? <StateNoticeCard kind="empty" title="暂无离线报告" />
                : (
                  <ul className="divide-y divide-[var(--border-subtle)] rounded-lg border border-[var(--border-subtle)]">
                    {reportsQ.data.items.map((item) => (
                      <li key={item.report_id} className="group/row-action flex items-center justify-between gap-3 p-3 hover:bg-[var(--bg-elevated)]">
                        <div className="min-w-0">
                          <p className="break-words text-sm font-semibold">{item.report_name}</p>
                          <p className="break-words text-xs text-[var(--text-secondary)]">
                            {FORMAT_LABELS[item.upload_format] ?? item.upload_format} · {item.project_id} · {fmtDateShort(item.created_at)}
                          </p>
                        </div>
                        <RowActionIcon variant="details" label={`查看离线报告：${item.report_name}`} onClick={() => openReport(item.report_id)} />
                      </li>
                    ))}
                  </ul>
                )}
        </section>
      )}

      {isAdmin && (
        <section {...officialDisclosure.layerProps} aria-label="管理员操作" className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)]">
          <button
            ref={officialDisclosure.triggerRef}
            type="button"
            onClick={() => setTask(task === 'official' ? null : 'official')}
            aria-expanded={task === 'official'}
            aria-controls="official-score-panel"
            disabled={pending}
            className="flex w-full items-center justify-between gap-3 rounded-xl p-4 text-left hover:bg-[var(--bg-elevated)] disabled:opacity-60 sm:px-5"
          >
            <span className="flex items-center gap-2 text-sm font-semibold">
              <Award aria-hidden="true" className="size-4 text-[var(--sem-zero-fg)]" /> 管理员 · 保存官方分
            </span>
            <ChevronDown aria-hidden="true" className={`size-4 text-[var(--text-secondary)] ${task === 'official' ? 'rotate-180' : ''}`} />
          </button>
          <CollapsiblePanel id="official-score-panel" open={task === 'official'}>
            <form
              aria-label="保存官方分"
              className="space-y-4 border-t border-[var(--border-subtle)] p-4 sm:p-5"
              onSubmit={(event) => submit(event, () => canSave && officialBatch && officialMut.mutate(officialBatch))}
            >
              <BatchPicker userId={userId} selected={officialBatch} disabled={officialMut.isPending} onChange={(batch) => {
                setOfficialBatch(batch);
                setOfficial({ dataset: '', metric: '', score: '', denominator: '', source: '' });
                officialMut.reset();
              }} />
              {officialBatch && (
                <dl className="grid gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 text-sm sm:grid-cols-3">
                  <div className="min-w-0">
                    <dt className="text-xs text-[var(--text-secondary)]">目标批次</dt>
                    <dd className="break-words font-semibold">{officialBatch.batch_name}</dd>
                  </div>
                  <div className="min-w-0">
                    <dt className="text-xs text-[var(--text-secondary)]">批次 ID</dt>
                    <dd className="break-all font-mono text-xs">{officialBatch.batch_id}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-[var(--text-secondary)]">当前运行范围</dt>
                    <dd className="font-semibold">{runScopeLabel(officialBatch.run_scope)}</dd>
                  </div>
                </dl>
              )}
              <p className="text-sm text-[var(--text-secondary)]">
                保存会更新目标批次在平台上的官方分及其状态。同一批次原有的官方分会被替换，运行范围和原始结果文件保持不变。
              </p>
              <fieldset disabled={!officialBatch || officialMut.isPending} className="grid min-w-0 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {([
                  ['dataset', '数据集（可选）', 'text'],
                  ['metric', '指标名称', 'text'],
                  ['score', '得分', 'number'],
                  ['denominator', '分母', 'number'],
                  ['source', '评分来源', 'text'],
                ] as const).map(([key, label, type]) => (
                  <label key={key} className="min-w-0 space-y-1 text-sm">
                    <span className="block font-medium">{label}</span>
                    <input
                      value={official[key]}
                      onChange={(event) => {
                        setOfficial({ ...official, [key]: event.target.value });
                        officialMut.reset();
                      }}
                      type={type}
                      min={key === 'denominator' ? 1 : undefined}
                      step={key === 'score' ? 'any' : undefined}
                      required={key !== 'dataset'}
                      placeholder={key === 'dataset' ? officialBatch?.raw_dataset_name ?? '默认使用批次数据集' : undefined}
                      className={fieldStyle}
                    />
                  </label>
                ))}
              </fieldset>
              <button type="submit" disabled={!canSave || officialMut.isPending} className={primaryButton}>
                {officialMut.isPending ? '正在保存…' : '保存到所选批次'}
              </button>
              {officialMut.error && <Feedback error>保存官方分失败。{officialMut.error.message}</Feedback>}
              {officialMut.data && (
                <Feedback>官方分已保存。运行范围仍为 {runScopeLabel(officialMut.data.run_scope)}。</Feedback>
              )}
            </form>
          </CollapsiblePanel>
        </section>
      )}
    </PageTransition>
  );
}
