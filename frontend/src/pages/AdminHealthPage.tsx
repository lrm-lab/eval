import { type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, AlertTriangle, Archive, CheckCircle2, Cpu, Database, HardDrive, RefreshCw, type LucideIcon } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { useAppStore } from '../lib/store';
import { PageTransition } from '../components/motion/primitives';
import { CardGridSkeleton, StateNoticeCard } from '../components/ui/Skeletons';
import { HealthDiagnostics, HEALTH_PANEL, showHealthPanel } from '../components/admin-health/HealthDiagnostics';
import { HealthMeter, IndexSamples, HEALTH_BUTTON, HEALTH_LINK, StatusTag } from '../components/admin-health/HealthVisuals';
import {
  BACKUPS, dataRows, displayLabel, fmtBytes, fmtFreshness, fmtMetric, fmtTimestamp, isRecord, numberValue, RUNNERS, textValue,
  type AdminHealthResp, type BackupStatus, type ExecutionTelemetryResp, type HealthAction, type IndexStatus, type RunnerStatus, type StorageStatus, type Tone,
} from '../components/admin-health/healthData';

const HEALTH_QUERY = ['admin-health'];
const TELEMETRY_QUERY = ['admin-execution-telemetry'];
const REFRESH_INTERVAL_MS = 30_000;
const CARD = 'min-w-0 space-y-2.5 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5 shadow-[var(--shadow-card)]';
const DETAIL = 'm-0 text-xs leading-relaxed text-[var(--text-secondary)]';
const VALUE = 'm-0 text-2xl font-semibold leading-none tracking-tight tabular-nums text-[var(--text-primary)]';

function SummaryCard({ title, icon: Icon, status, children }: {
  title: string; icon: LucideIcon; status: { label: string; tone: Tone }; children: ReactNode;
}) {
  return (
    <section aria-label={title} className={CARD}>
      <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
        <h2 className="m-0 inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--text-primary)]">
          <Icon className="size-4 text-[var(--brand-primary)]" aria-hidden="true" /> {title}
        </h2>
        <StatusTag tone={status.tone}>{status.label}</StatusTag>
      </div>
      {children}
    </section>
  );
}

function coldIndicator(indexer?: IndexStatus): { label: string; tone: Tone } {
  const status = textValue(indexer?.cold_status, '');
  if (status === 'ready') return { label: '冷层就绪', tone: 'ok' };
  if (status === 'running') return { label: '冷层扫描中', tone: 'neutral' };
  if (status === 'idle') return { label: '冷层未运行', tone: 'neutral' };
  if (status === 'write_protected_disk_low') return { label: '冷层写入暂停', tone: 'error' };
  if (status.startsWith('error')) return { label: '冷层扫描异常', tone: 'error' };
  return { label: '冷层状态未知', tone: 'neutral' };
}

function IndexSummary({ indexer, asOf }: { indexer?: IndexStatus; asOf: number }) {
  const hotTimestamp = fmtTimestamp(indexer?.last_hot_sync_at);
  const coldTimestamp = fmtTimestamp(indexer?.last_cold_sync_at);
  const duration = numberValue(indexer?.last_hot_stats?.elapsed_ms);
  const cold = coldIndicator(indexer);
  return (
    <SummaryCard title="索引" icon={Database} status={cold}>
      <p className={VALUE}>{duration === null ? '—' : fmtMetric(duration)} <span className="text-xs font-medium text-[var(--text-secondary)]">{duration === null ? '最近热扫描耗时未报告' : 'ms · 最近热扫描'}</span></p>
      <p className={DETAIL} title={hotTimestamp}>热层同步 {fmtFreshness(indexer?.last_hot_sync_at, asOf)}</p>
      <IndexSamples value={indexer?.recent_poll_ms} />
      <p className={DETAIL} title={coldTimestamp}>
        冷层同步 {fmtFreshness(indexer?.last_cold_sync_at, asOf)}
        {numberValue(indexer?.last_cold_stats?.elapsed_ms) !== null && ` · ${fmtMetric(indexer?.last_cold_stats?.elapsed_ms, ' ms')}`}
      </p>
    </SummaryCard>
  );
}

function JobSummary({ runner }: { runner?: RunnerStatus }) {
  const ready = runner?.isolated_runner_ready;
  const counts = runner?.jobs_by_status;
  const jobs = isRecord(counts)
    ? ['queued', 'running', 'cancelling'].map((status) => Object.hasOwn(counts, status) ? numberValue(counts[status]) : 0)
    : [null, null, null];
  const active = jobs.includes(null) ? null : jobs.reduce<number>((total, n) => total + (n ?? 0), 0);
  const supported: unknown[] = Array.isArray(runner?.supported_runners) ? runner.supported_runners : [];
  const modes = [...new Set(supported.map((mode) => textValue(mode, '')).filter(Boolean))];
  const status = ready === true ? { label: '执行就绪', tone: 'ok' as const }
    : ready === false ? { label: runner?.runner_connected === true ? '已连接 · 未就绪' : '未就绪', tone: 'warning' as const }
      : { label: '状态未报告', tone: 'neutral' as const };
  return (
    <SummaryCard title="隔离 Runner" icon={Cpu} status={status}>
      <p className={VALUE}>{fmtMetric(active, '', 0)} <span className="text-xs font-medium text-[var(--text-secondary)]">活跃作业</span></p>
      <p className={DETAIL}>排队 {fmtMetric(jobs[0], '', 0)} · 运行 {fmtMetric(jobs[1], '', 0)} · 停止中 {fmtMetric(jobs[2], '', 0)}</p>
      {ready === true && modes.length ? (
        <ul className="m-0 flex list-none flex-wrap gap-1 p-0" aria-label="可用运行器">
          {modes.map((mode) => <li key={mode} className="rounded-md bg-[var(--brand-tint)] px-1.5 py-0.5 text-[11px] text-[var(--brand-primary)]">{displayLabel(mode, RUNNERS)}</li>)}
        </ul>
      ) : <p className={DETAIL}>{ready === false && runner?.runner_mode === 'disconnected_readonly' ? '只读与预检可用' : '未报告可用运行器'}</p>}
      <p className={DETAIL}>Web Docker 组 · {runner?.docker_group_attached === false ? '已隔离 (无 docker 组)' : runner?.docker_group_attached === true ? '隔离异常' : '未报告'}</p>
    </SummaryCard>
  );
}

function StorageSummary({ storage, blocked }: { storage?: StorageStatus; blocked: boolean }) {
  const tone: Tone = blocked ? 'error' : storage?.budget_alert === true || storage?.root_disk_alert === true ? 'warning'
    : storage?.write_guard_ok === true ? 'ok' : 'neutral';
  const status = { label: { error: '写入受保护', warning: '磁盘告警', ok: '允许写入', neutral: '状态未报告' }[tone], tone };
  return (
    <SummaryCard title="本地存储" icon={HardDrive} status={status}>
      <p className={VALUE}>
        {fmtMetric(storage?.local_runtime_mb)} <span className="text-xs font-medium text-[var(--text-secondary)]">/ {fmtMetric(storage?.local_budget_mb)} MB</span>
      </p>
      <HealthMeter label="本地存储预算" value={storage?.local_runtime_mb} cap={storage?.local_budget_mb} tone={tone} />
      <p className={DETAIL}>根盘剩余 {fmtMetric(storage?.root_free_gb, ' GB')} · 已用 {fmtMetric(storage?.root_used_pct, '%')}</p>
      <p className={DETAIL}>状态盘剩余 {fmtMetric(storage?.state_fs_free_gb, ' GB')}</p>
    </SummaryCard>
  );
}

function backupIndicator(backup: BackupStatus | undefined): { label: string; tone: Tone } {
  const status = textValue(backup?.status, '');
  if (Object.hasOwn(BACKUPS, status)) return BACKUPS[status];
  return { label: '状态未报告', tone: 'neutral' };
}

function BackupSummary({ backup }: { backup?: BackupStatus }) {
  const status = backupIndicator(backup);
  const latest = textValue(backup?.latest_name, '');
  return (
    <SummaryCard title="SQLite 备份" icon={Archive} status={status}>
      <p className={VALUE}>{fmtMetric(backup?.retained_count, '', 0)} <span className="text-xs font-medium text-[var(--text-secondary)]">/ {fmtMetric(backup?.retention_limit, '', 0)} 份</span></p>
      <p className={DETAIL}>最近备份 {fmtTimestamp(backup?.latest_at)}{numberValue(backup?.age_hours) !== null && ` · ${fmtMetric(backup?.age_hours, ' 小时前', 2)}`}</p>
      {latest && <p className={`${DETAIL} break-all font-mono`} title={latest}>{latest}</p>}
      <p className={DETAIL}>文件体积 {fmtBytes(backup?.latest_size_bytes)} / {fmtBytes(backup?.size_limit_bytes)}</p>
    </SummaryCard>
  );
}

type HealthAlert = {
  key: string; title: string; detail?: string; tone: Exclude<Tone, 'ok'>; time?: string; target?: 'jobs' | 'sources' | 'ops' | 'manuals'; count?: number;
};

function collectAlerts(data: AdminHealthResp | undefined, telemetry: ExecutionTelemetryResp | undefined, healthError: string, telemetryError: string): HealthAlert[] {
  const alerts: HealthAlert[] = [];
  const st = data?.storage;
  const index = data?.indexer;
  const runner = data?.runner_status;
  const ops = telemetry?.ops;
  if (healthError) alerts.push({ key: 'health', tone: 'error', title: '监控数据更新失败', detail: healthError });
  if (telemetryError || (telemetry && !ops)) alerts.push({ key: 'telemetry', tone: 'warning', title: '日志与备份状态不可用', detail: telemetryError });
  if (st?.write_guard_ok === false || index?.write_protected === true) {
    alerts.push({ key: 'write', tone: 'error', title: '写入保护已触发', detail: textValue(st?.write_guard_error ?? index?.write_block_reason, '重索引与配置重载已暂停。'), target: 'ops' });
  } else if (st?.root_disk_alert === true || st?.budget_alert === true) {
    alerts.push({ key: 'disk', tone: 'warning', title: st?.root_disk_alert === true ? '根盘剩余空间告警' : '本地占用达到警戒线', detail: `根盘剩余 ${fmtMetric(st.root_free_gb, ' GB')} · 本地占用 ${fmtMetric(st.local_runtime_mb, ' MB')}`, target: 'ops' });
  }
  const probes = st?.isolation_probes;
  const failures = isRecord(probes) ? Object.entries(probes).filter(([, probe]) => isRecord(probe) && probe.ok === false).map(([name]) => name) : [];
  if (failures.length) alerts.push({ key: 'isolation', tone: 'error', title: '隔离或受管框架探针异常', detail: failures.join('、'), target: 'ops' });
  if (runner?.docker_group_attached === true) alerts.push({ key: 'docker', tone: 'error', title: 'Web 进程加入了 Docker 组', target: 'ops' });
  if (runner?.isolated_runner_ready === false) alerts.push({ key: 'runner', tone: 'warning', title: 'Runner 未就绪', detail: textValue(runner.reason, '暂无法在线提交作业。'), target: 'jobs' });
  if (textValue(index?.cold_status, '').startsWith('error')) alerts.push({ key: 'cold', tone: 'error', title: '冷层扫描异常', detail: index?.cold_status, target: 'ops' });
  const backup = backupIndicator(ops?.backup);
  if (backup.tone === 'warning' || backup.tone === 'error') alerts.push({ key: 'backup', tone: backup.tone, title: backup.label, detail: textValue(ops?.backup?.note, ''), target: 'manuals' });
  if (ops?.daemon_logs?.within_cap === false || ops?.job_stdio_usage?.within_cap === false) {
    alerts.push({ key: 'logs', tone: 'warning', title: '日志占用超过留存上限', target: 'ops' });
  }
  const warnings = new Map<string, HealthAlert>();
  for (const row of dataRows(data?.source_coverage?.warnings)) {
    const severity = textValue(row.severity, '');
    const title = textValue(row.code, '索引告警');
    const detail = [row.message, row.target_path].map((field) => textValue(field, '')).filter(Boolean).join(' · ');
    const key = JSON.stringify([severity, row.tier, title, detail]);
    const existing = warnings.get(key);
    if (existing) existing.count = (existing.count ?? 1) + 1;
    else warnings.set(key, {
      key, title, detail, time: row.detected_at, target: 'sources',
      tone: severity === 'error' || severity === 'critical' ? 'error' : severity === 'info' ? 'neutral' : 'warning',
    });
  }
  const rank = { error: 0, warning: 1, neutral: 2 };
  return [...alerts, ...warnings.values()].sort((a, b) => rank[a.tone] - rank[b.tone]);
}

const ALERT_ACTIONS = { jobs: '查看作业', ops: '查看运维', sources: '查看来源' };

function AlertRow({ alert }: { alert: HealthAlert }) {
  const target = alert.target;
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1 px-3.5 py-2">
      <StatusTag tone={alert.tone}>{alert.tone === 'error' ? '异常' : alert.tone === 'warning' ? '警告' : '信息'}</StatusTag>
      <div className="min-w-0 grow basis-36 text-xs">
        <p className="m-0 break-words font-semibold text-[var(--text-primary)]">{alert.title}{alert.count && alert.count > 1 ? ` ×${alert.count}` : ''}</p>
        {alert.detail && <p className={`${DETAIL} break-words`}>{alert.detail}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 text-xs">
        {alert.time && <span className="text-[11px] tabular-nums text-[var(--text-muted)]">{fmtTimestamp(alert.time)}</span>}
        {target === 'manuals' ? <Link className={HEALTH_LINK} to="/toolkit/manuals">查看运维手册</Link>
          : target && <a className={HEALTH_LINK} href={`#${HEALTH_PANEL[target]}`} onClick={() => showHealthPanel(HEALTH_PANEL[target])}>{ALERT_ACTIONS[target]}</a>}
      </div>
    </li>
  );
}

function AlertList({ alerts, haveWarnings, onRefresh, refreshing }: {
  alerts: HealthAlert[]; haveWarnings: boolean; onRefresh: () => void; refreshing: boolean;
}) {
  const primary = alerts.slice(0, 4);
  const remaining = alerts.slice(4);
  return (
    <section aria-labelledby="health-alerts-title" className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] shadow-[var(--shadow-card)]">
      <div className="flex items-center justify-between gap-2 px-3.5 py-2.5">
        <h2 id="health-alerts-title" className="m-0 flex items-center gap-2 text-sm font-semibold">
          <AlertTriangle className={`size-4 ${alerts.length ? 'text-[var(--sem-zero-fg)]' : 'text-[var(--text-secondary)]'}`} aria-hidden="true" />
          告警清单 {alerts.length > 0 && <span className="font-normal tabular-nums text-[var(--text-secondary)]">{alerts.length} 项</span>}
        </h2>
        {alerts.length > 0 && <button type="button" onClick={onRefresh} disabled={refreshing} className={HEALTH_BUTTON}>复查告警</button>}
      </div>
      {alerts.length ? (
        <ul className="m-0 list-none divide-y divide-[var(--border-subtle)] border-t border-[var(--border-subtle)] p-0">
          {primary.map((alert) => <AlertRow key={alert.key} alert={alert} />)}
        </ul>
      ) : (
        <p className="m-0 flex items-center gap-2 border-t border-[var(--border-subtle)] px-3.5 py-2.5 text-xs text-[var(--text-secondary)]">
          {haveWarnings && <CheckCircle2 className="size-4 text-[var(--sem-pass-fg)]" aria-hidden="true" />}
          {haveWarnings ? '当前未报告告警' : '告警数据未报告'}
        </p>
      )}
      {remaining.length > 0 && (
        <details className="border-t border-[var(--border-subtle)]">
          <summary className="cursor-pointer px-3.5 py-2 text-xs text-[var(--brand-primary)]">其余 {remaining.length} 项告警</summary>
          <ul className="m-0 max-h-52 list-none divide-y divide-[var(--border-subtle)] overflow-y-auto p-0">
            {remaining.map((alert) => <AlertRow key={alert.key} alert={alert} />)}
          </ul>
        </details>
      )}
    </section>
  );
}

async function runHealthAction(action: HealthAction) {
  let path = '/api/v1/admin/';
  let body: string | undefined;
  switch (action.kind) {
    case 'hot': path += 'reindex?force=false&include_cold=false'; break;
    case 'full': path += 'reindex?force=true&include_cold=true'; break;
    case 'batch': path += `reindex/${encodeURIComponent(action.batchId)}`; break;
    case 'cold-file':
      path += 'reindex-cold-file';
      body = JSON.stringify({ file_path: action.filePath });
      break;
    case 'taxonomy': path += 'taxonomy/reload'; break;
  }
  const result = await apiFetch<{ ok?: boolean; status?: string }>(path, { method: 'POST', body });
  if (!result?.ok || result.status === 'rejected' || result.status === 'error') {
    throw new Error(result?.status ? `索引返回 ${result.status}，请查看告警清单。` : '服务端未报告操作成功。');
  }
}

export function AdminHealthPage() {
  const user = useAppStore((s) => s.user);
  const pushToast = useAppStore((s) => s.pushToast);
  const qc = useQueryClient();
  const isAdmin = user?.role === 'admin';
  const healthQ = useQuery({
    queryKey: HEALTH_QUERY, enabled: isAdmin, refetchInterval: REFRESH_INTERVAL_MS,
    queryFn: () => apiFetch<AdminHealthResp>('/api/v1/admin/health'),
  });
  const telemetryQ = useQuery({
    queryKey: TELEMETRY_QUERY, enabled: isAdmin, refetchInterval: REFRESH_INTERVAL_MS,
    queryFn: () => apiFetch<ExecutionTelemetryResp>('/api/v1/execution/telemetry'),
  });
  const mutation = useMutation({
    mutationFn: runHealthAction,
    onSuccess: (_, action) => {
      pushToast('success', action.kind === 'taxonomy' ? '分类配置已重载' : '索引操作已完成');
      void qc.invalidateQueries({ queryKey: ['system-overview'] });
      if (action.kind === 'taxonomy') void qc.invalidateQueries({ queryKey: ['datasets-tree'] });
    },
    onError: (error: Error) => pushToast('error', `运维操作失败：${error.message}`),
    onSettled: () => Promise.all([qc.invalidateQueries({ queryKey: HEALTH_QUERY }), qc.invalidateQueries({ queryKey: TELEMETRY_QUERY })]),
  });

  if (!isAdmin) {
    return <div className="mx-auto max-w-4xl px-5 py-10"><StateNoticeCard kind="forbidden" title="仅管理员可访问系统监控" description="管理员可查看探针与索引告警，并触发索引维护。" /></div>;
  }

  const data = healthQ.data;
  const telemetry = telemetryQ.data;
  const blocked = data?.storage?.write_guard_ok === false || data?.indexer?.write_protected === true;
  const loading = healthQ.isPending || telemetryQ.isPending;
  const refreshing = healthQ.isFetching || telemetryQ.isFetching;
  const asOf = healthQ.dataUpdatedAt;
  const refresh = () => { void Promise.all([healthQ.refetch(), telemetryQ.refetch()]); };
  const alerts = collectAlerts(data, telemetry, healthQ.error?.message ?? '', telemetryQ.error?.message ?? '');
  const framework = data?.storage?.isolation_probes?.managed_framework;
  const frameworkOk = isRecord(framework) && framework.ok === true;
  const frameworkFailed = isRecord(framework) && framework.ok === false;
  const mode = data?.storage?.production_mode;

  return (
    <PageTransition className="mx-auto max-w-7xl space-y-3.5 px-4 py-5 sm:px-5 sm:py-6">
      <header className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] bg-linear-to-r from-[var(--brand-tint)]/65 via-transparent to-transparent px-4 py-3.5">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg border border-[var(--brand-primary)]/25 bg-[var(--brand-tint)] text-[var(--brand-primary)]"><Activity className="size-5" aria-hidden="true" /></span>
          <div>
            <h1 className="m-0 text-xl font-bold tracking-tight text-[var(--text-primary)]">系统监控</h1>
            <p className="m-0 mt-0.5 text-xs text-[var(--text-secondary)]">系统健康、磁盘预算与数据源覆盖</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-xs tabular-nums text-[var(--text-secondary)]" role="status">
            {refreshing ? '正在刷新…' : asOf ? `状态采集 ${fmtTimestamp(new Date(asOf).toISOString())}` : '等待状态采集'}
          </span>
          <button type="button" className={HEALTH_BUTTON} disabled={refreshing} onClick={refresh}>
            <RefreshCw className={`size-3.5 ${refreshing ? 'motion-safe:animate-spin' : ''}`} aria-hidden="true" /> 刷新状态
          </button>
        </div>
      </header>

      <div className="flex min-h-6 flex-wrap items-center gap-2 text-xs">
        <StatusTag tone={healthQ.isError ? 'error' : isRecord(data) ? 'ok' : 'neutral'}>
          {healthQ.isError ? '监控接口异常' : isRecord(data) ? '监控接口可用' : healthQ.isPending ? '正在读取状态' : '监控接口未返回状态'}
        </StatusTag>
        <StatusTag tone={frameworkOk ? 'ok' : frameworkFailed ? 'error' : 'neutral'}><span className="font-mono">managed_framework</span> {frameworkOk ? '通过' : frameworkFailed ? '异常' : '未报告'}</StatusTag>
        <span className="text-[var(--text-secondary)]">{mode === true ? '生产模式' : mode === false ? '开发模式' : '运行模式未报告'}</span>
      </div>

      {loading ? (
        <div className="space-y-3.5" aria-label="正在读取监控概览" aria-busy="true">
          <div className="h-20 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5">
            <div className="lrm-skeleton h-4 w-32" />
          </div>
          <CardGridSkeleton />
        </div>
      ) : (
        <>
          <AlertList
            alerts={alerts}
            haveWarnings={Array.isArray(data?.source_coverage?.warnings) && data.source_coverage.warnings.every(isRecord)}
            onRefresh={refresh}
            refreshing={refreshing}
          />
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <IndexSummary indexer={data?.indexer} asOf={asOf} />
            <JobSummary runner={data?.runner_status} />
            <StorageSummary storage={data?.storage} blocked={blocked} />
            <BackupSummary backup={telemetry?.ops?.backup} />
          </div>
          <HealthDiagnostics data={data} telemetry={telemetry} busy={mutation.isPending} blocked={blocked} onAction={mutation.mutate} />
        </>
      )}
    </PageTransition>
  );
}
