import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ChevronDown, Database, ServerCog, Wrench, type LucideIcon } from 'lucide-react';
import { HealthMeter, HEALTH_BUTTON, HEALTH_LINK, StatusTag } from './HealthVisuals';
import {
  dataRows, displayLabel, fmtBytes, fmtMetric, fmtTimestamp, isRecord, JOBS, numberValue, RUNNERS, SOURCE_STATUS, textValue,
  type AdminHealthResp, type ExecutionTelemetryResp, type HealthAction, type IsolationProbe, type SourceRow, type Tone,
} from './healthData';

const CELL = 'px-3 py-2 align-top';
const BLOCK_TITLE = 'm-0 mb-2 text-xs font-semibold text-[var(--text-primary)]';
const GRID = 'grid gap-x-6 gap-y-4 lg:grid-cols-2';

export const HEALTH_PANEL = { sources: 'health-sources', jobs: 'health-jobs', ops: 'health-ops' } as const;

export function showHealthPanel(id: string) {
  const panel = document.getElementById(id);
  if (panel instanceof HTMLDetailsElement) {
    panel.open = true;
    panel.querySelector('summary')?.focus({ preventScroll: true });
  }
}

function Panel({ id, icon: Icon, title, description, children }: {
  id: string; icon: LucideIcon; title: string; description: string; children: ReactNode;
}) {
  return (
    <details id={id} className="group scroll-mt-20 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)]">
      <summary className="flex min-h-15 cursor-pointer list-none items-center gap-3 rounded-xl px-4 py-2.5 hover:bg-[var(--bg-elevated)] [&::-webkit-details-marker]:hidden">
        <Icon className="size-4 shrink-0 text-[var(--brand-primary)]" aria-hidden="true" />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold text-[var(--text-primary)]">{title}</span>
          <span className="block text-xs text-[var(--text-secondary)]">{description}</span>
        </span>
        <ChevronDown className="size-4 shrink-0 text-[var(--text-muted)] motion-safe:transition-transform group-open:rotate-180" aria-hidden="true" />
      </summary>
      <div className="space-y-4 border-t border-[var(--border-subtle)] p-4 text-xs">{children}</div>
    </details>
  );
}

function Table({ label, headings, children, limitHeight = false }: {
  label: string; headings: string[]; children: ReactNode; limitHeight?: boolean;
}) {
  return (
    <div className={`overflow-auto rounded-lg border border-[var(--border-subtle)] ${limitHeight ? 'max-h-96' : ''}`}>
      <table aria-label={label} className="w-full text-left text-xs text-[var(--text-secondary)]">
        <thead className="bg-[var(--bg-elevated)] font-medium">
          <tr>{headings.map((heading) => <th key={heading} scope="col" className={CELL}>{heading}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-[var(--border-subtle)]">{children}</tbody>
      </table>
    </div>
  );
}

function EmptyDetail({ available, children }: { available: boolean; children: ReactNode }) {
  return <p className="m-0 text-xs text-[var(--text-muted)]">{available ? children : '接口未报告这项明细。'}</p>;
}

function SourceMetrics({ source }: { source: SourceRow }) {
  const counts = [
    ['发现目录', source.discovered_dirs], ['索引批次', source.indexed_batches], ['缓存文件', source.cached_files],
    ['索引条目', source.indexed_items], ['权限缺口', source.permission_gaps], ['失败文件', source.error_files],
  ] as const;
  const values = counts.filter(([, n]) => numberValue(n) !== null).map(([label, n]) => `${label} ${fmtMetric(n)}`);
  if (numberValue(source.size_mb) !== null) values.push(fmtMetric(source.size_mb, ' MB'));
  return <span className="tabular-nums">{values.join(' · ') || '未报告数量'}</span>;
}

function Sources({ data }: { data?: AdminHealthResp }) {
  const coverage = data?.source_coverage;
  const sources = dataRows(coverage?.by_source);
  const runners = dataRows(coverage?.by_runner);
  const cache = dataRows(coverage?.cold_cache);
  return (
    <Panel id={HEALTH_PANEL.sources} icon={Database} title="来源诊断" description="接入状态、批次覆盖、冷层缓存">
      {sources.length ? (
        <Table label="来源覆盖" headings={['来源', '状态', '覆盖明细']} limitHeight>
          {sources.map((source, index) => {
            const name = textValue(source.source ?? source.source_key, '未命名来源');
            return (
              <tr key={`${name}-${index}`}>
                <th scope="row" className={`${CELL} break-all font-medium text-[var(--text-primary)]`}>{name}</th>
                <td className={CELL}>{displayLabel(source.status, SOURCE_STATUS)}</td>
                <td className={CELL}><SourceMetrics source={source} /></td>
              </tr>
            );
          })}
        </Table>
      ) : <EmptyDetail available={Array.isArray(coverage?.by_source)}>暂无来源记录。</EmptyDetail>}
      <div className={GRID}>
        <section>
          <h3 className={BLOCK_TITLE}>运行器覆盖</h3>
          {runners.length ? (
            <Table label="运行器覆盖" headings={['运行器', '批次', 'Trial']}>
              {runners.map((runner, i) => (
                <tr key={`${runner.runner}-${i}`}>
                  <th scope="row" className={CELL}>{displayLabel(runner.runner, RUNNERS)}</th>
                  <td className={CELL}>{fmtMetric(runner.batches)}</td>
                  <td className={CELL}>{fmtMetric(runner.trials)}</td>
                </tr>
              ))}
            </Table>
          ) : <EmptyDetail available={Array.isArray(coverage?.by_runner)}>暂无已索引批次。</EmptyDetail>}
        </section>
        <section>
          <h3 className={BLOCK_TITLE}>冷层缓存</h3>
          {cache.length ? (
            <Table label="冷层缓存" headings={['类型', '状态', '文件 / 条目']} limitHeight>
              {cache.map((row, i) => (
                <tr key={`${row.cache_kind}-${row.status}-${i}`}>
                  <th scope="row" className={`${CELL} break-all font-mono`}>{textValue(row.cache_kind)}</th>
                  <td className={CELL}>{displayLabel(row.status, SOURCE_STATUS)}</td>
                  <td className={CELL}>{fmtMetric(row.files)} / {fmtMetric(row.items)}</td>
                </tr>
              ))}
            </Table>
          ) : <EmptyDetail available={Array.isArray(coverage?.cold_cache)}>暂无缓存记录。</EmptyDetail>}
        </section>
      </div>
    </Panel>
  );
}

function Jobs({ data }: { data?: AdminHealthResp }) {
  const runner = data?.runner_status;
  const jobs = isRecord(runner?.jobs_by_status) ? Object.entries(runner.jobs_by_status) : [];
  const placeholders = dataRows(data?.source_coverage?.running_placeholders);
  const profiles = dataRows(data?.gateway_profiles);
  return (
    <Panel id={HEALTH_PANEL.jobs} icon={ServerCog} title="作业诊断" description="作业状态、运行中占位批次、服务端 Gateway / Teacher / Judge Profiles">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[var(--text-secondary)]">
          Runner PID {fmtMetric(runner?.runner_pid, '', 0)} · IPC {runner?.runner_connected === true ? '已连接' : runner?.runner_connected === false ? '未连接' : '未报告'}
          {textValue(runner?.reason, '') && <p className="m-0 mt-1 break-words">{runner?.reason}</p>}
        </div>
        <Link className={HEALTH_LINK} to="/eval/execution">打开受控评测执行</Link>
      </div>
      <div className={GRID}>
        <section>
          <h3 className={BLOCK_TITLE}>作业状态</h3>
          {jobs.length ? (
            <dl className="m-0 grid grid-cols-2 gap-2 sm:grid-cols-3">
              {jobs.map(([status, count]) => (
                <div key={status} className="rounded-lg bg-[var(--bg-elevated)] px-2.5 py-2">
                  <dt className="text-[var(--text-secondary)]">{displayLabel(status, JOBS)}</dt>
                  <dd className="m-0 mt-0.5 font-semibold tabular-nums">{fmtMetric(count, '', 0)}</dd>
                </div>
              ))}
            </dl>
          ) : <EmptyDetail available={isRecord(runner?.jobs_by_status)}>暂无作业记录。</EmptyDetail>}
        </section>
        <section>
          <h3 className={BLOCK_TITLE}>服务端连接配置</h3>
          {profiles.length ? (
            <ul className="m-0 list-none space-y-1.5 p-0">
              {profiles.map((profile, i) => (
                <li key={`${profile.profile_id}-${i}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-[var(--bg-elevated)] px-2.5 py-2">
                  <span className="min-w-0 break-words">{textValue(profile.display_name ?? profile.profile_id, '未命名配置')} · {textValue(profile.kind)}</span>
                  <StatusTag tone={profile.key_configured === true ? 'ok' : profile.key_configured === false ? 'warning' : 'neutral'}>
                    {profile.key_configured === true ? '凭据已配置' : profile.key_configured === false ? '待配置凭据' : '状态未报告'}
                  </StatusTag>
                </li>
              ))}
            </ul>
          ) : <EmptyDetail available={Array.isArray(data?.gateway_profiles)}>暂无连接配置。</EmptyDetail>}
        </section>
      </div>
      <section>
        <h3 className={BLOCK_TITLE}>运行中占位批次</h3>
        {placeholders.length ? (
          <Table label="运行中占位批次" headings={['批次', '运行器', '已记录 / 计划', '最近索引']} limitHeight>
            {placeholders.map((batch, i) => (
              <tr key={`${batch.batch_id}-${i}`}>
                <th scope="row" className={`${CELL} break-all`}>{textValue(batch.batch_name ?? batch.batch_id, '未命名批次')}</th>
                <td className={CELL}>{displayLabel(batch.runner, RUNNERS)}</td>
                <td className={`${CELL} tabular-nums`}>
                  {fmtMetric(batch.recorded)} / {fmtMetric(batch.planned)}
                  {textValue(batch.batch_status, '') && <div className="mt-0.5">{displayLabel(batch.batch_status, JOBS)}</div>}
                </td>
                <td className={CELL}>{fmtTimestamp(batch.indexed_at)}</td>
              </tr>
            ))}
          </Table>
        ) : <EmptyDetail available={Array.isArray(data?.source_coverage?.running_placeholders)}>暂无运行中占位批次。</EmptyDetail>}
      </section>
    </Panel>
  );
}

function ProbeDetails({ data }: { data?: AdminHealthResp }) {
  const probes = data?.storage?.isolation_probes;
  const items = isRecord(probes)
    ? Object.entries(probes).filter((entry): entry is [string, IsolationProbe] => isRecord(entry[1])) : [];
  return (
    <section>
      <h3 className={BLOCK_TITLE}>存储隔离与受管框架探针</h3>
      {items.length ? (
        <Table label="隔离探针" headings={['探针', '状态', '明细']}>
          {items.map(([key, probe]) => {
            const tone: Tone = probe.ok === false ? 'error' : probe.configured === false ? 'warning' : probe.ok === true ? 'ok' : 'neutral';
            const label = { error: '异常', warning: '未配置', ok: '通过', neutral: '未报告' }[tone];
            const detail = [probe.code, probe.message ?? probe.note, probe.fstype, probe.mode].map((value) => textValue(value, '')).filter(Boolean);
            return (
              <tr key={key}>
                <th scope="row" className={`${CELL} break-all font-mono`}>{key}</th>
                <td className={CELL}><StatusTag tone={tone}>{label}</StatusTag></td>
                <td className={`${CELL} break-words`}>{detail.join(' · ') || '—'}</td>
              </tr>
            );
          })}
        </Table>
      ) : <EmptyDetail available={isRecord(probes)}>暂无探针记录。</EmptyDetail>}
      <p className="m-0 mt-2 text-[var(--text-secondary)]">配置盘类型 {textValue(probes?.config_dir_fstype)}</p>
    </section>
  );
}

function Logs({ telemetry }: { telemetry?: ExecutionTelemetryResp }) {
  const daemon = telemetry?.ops?.daemon_logs;
  const stdio = telemetry?.ops?.job_stdio_usage;
  const components = dataRows(daemon?.components);
  const cg = telemetry?.web_cgroup;
  const policy = telemetry?.stdio_policy;
  return (
    <div className={GRID}>
      <section className="space-y-2">
        <h3 className={BLOCK_TITLE}>守护进程日志</h3>
        <p className="m-0 tabular-nums">{fmtBytes(daemon?.total_bytes)} / {fmtBytes(daemon?.cap_bytes)}</p>
        {daemon?.logs_dir_present === false
          ? <p className="m-0 text-[var(--text-muted)]">日志目录尚未创建。</p>
          : <HealthMeter label="守护进程日志" value={daemon?.total_bytes} cap={daemon?.cap_bytes} tone={daemon?.within_cap === false ? 'error' : 'ok'} />}
        {components.length > 0 && (
          <dl className="m-0 space-y-1 text-[var(--text-secondary)]">
            {components.map((row, i) => (
              <div key={`${row.component}-${i}`} className="flex flex-wrap justify-between gap-x-2">
                <dt>{textValue(row.component)}</dt>
                <dd className="m-0 tabular-nums">{fmtBytes(row.current_bytes)} · {fmtMetric(row.files_present)} / {fmtMetric(row.max_files)} 份 · 单份 {fmtBytes(row.max_bytes_per_file)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>
      <section className="space-y-2">
        <h3 className={BLOCK_TITLE}>作业输出留存</h3>
        <p className="m-0 tabular-nums">{fmtBytes(stdio?.total_bytes)} / {fmtBytes(stdio?.cap_bytes)}</p>
        <HealthMeter label="作业日志" value={stdio?.total_bytes} cap={stdio?.cap_bytes} tone={stdio?.within_cap === false ? 'error' : 'ok'} />
        <p className="m-0 text-[var(--text-secondary)]">
          有输出作业 {fmtMetric(stdio?.jobs_with_stdio)} · 单作业上限 {fmtBytes(policy?.per_job_cap_bytes)} · 详情尾读 {fmtBytes(policy?.detail_read_bytes)}
        </p>
        <Link className={HEALTH_LINK} to="/eval/execution">在作业详情查看日志和 TTFT / TPS</Link>
      </section>
      <section className="lg:col-span-2">
        <h3 className={BLOCK_TITLE}>Web 进程遥测</h3>
        {cg?.source === 'cgroup_v2' ? (
          <p className="m-0 text-[var(--text-secondary)]">
            cgroup v2 · {textValue(cg.scope)} · 内存当前 {fmtBytes(cg.memory_current_bytes)} / 峰值 {fmtBytes(cg.memory_peak_bytes)} / 上限 {fmtBytes(cg.memory_max_bytes)} · CPU 累计 {fmtMetric(numberValue(cg.cpu_usage_usec) === null ? null : cg.cpu_usage_usec! / 1e6, ' 秒')}
          </p>
        ) : <p className="m-0 text-[var(--text-muted)]">{textValue(cg?.note, '未采集到 Web cgroup v2 数据。')}</p>}
      </section>
    </div>
  );
}

function Operations({ data, telemetry, busy, blocked, onAction }: {
  data?: AdminHealthResp; telemetry?: ExecutionTelemetryResp; busy: boolean; blocked: boolean; onAction: (action: HealthAction) => void;
}) {
  const [batchId, setBatchId] = useState('');
  const coldFiles = dataRows(data?.source_coverage?.cold_files).filter((row) =>
    row.cache_kind === 'legacy_jsonl' || row.cache_kind === 'legacy_json_dict' || row.cache_kind === 'bundle_tar');
  const disabled = busy || blocked;
  return (
    <Panel id={HEALTH_PANEL.ops} icon={Wrench} title="运维诊断" description="索引操作、隔离探针、日志限额和 Web 遥测">
      <section className="space-y-2">
        <h3 className={BLOCK_TITLE}>索引操作</h3>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className={HEALTH_BUTTON} disabled={disabled} onClick={() => onAction({ kind: 'hot' })}>热层增量扫描</button>
          <button type="button" className={HEALTH_BUTTON} disabled={disabled} onClick={() => onAction({ kind: 'full' })}>强制重扫热层与冷层</button>
          <button type="button" className={HEALTH_BUTTON} disabled={disabled} onClick={() => onAction({ kind: 'taxonomy' })}>重载 taxonomy.yaml</button>
          <Link className={`${HEALTH_LINK} px-1`} to="/toolkit/manuals">运维手册</Link>
        </div>
        <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => {
          event.preventDefault();
          if (batchId.trim() && !disabled) onAction({ kind: 'batch', batchId: batchId.trim() });
        }}>
          <label className="min-w-0 max-w-full text-[var(--text-secondary)]" htmlFor="health-batch-id">
            批次 ID
            <input id="health-batch-id" value={batchId} onChange={(event) => setBatchId(event.target.value)} disabled={disabled} className="mt-1 block h-8 max-w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-canvas)] px-2 font-mono text-[var(--text-primary)]" />
          </label>
          <button type="submit" className={HEALTH_BUTTON} disabled={disabled || !batchId.trim()}>单批次重索引</button>
          <Link className={`${HEALTH_LINK} px-1 pb-1.5`} to="/eval/reports">查找批次</Link>
        </form>
        {blocked && <p className="m-0 text-[var(--sem-err-fg)]">写入保护已触发。恢复磁盘后请刷新状态。</p>}
        {busy && <p className="m-0 text-[var(--text-secondary)]" role="status">正在执行运维操作…</p>}
      </section>
      <section>
        <h3 className={BLOCK_TITLE}>已登记冷文件</h3>
        {coldFiles.length ? (
          <Table label="已登记冷文件" headings={['文件', '状态', '体积 / 条目', '时间', '操作']} limitHeight>
            {coldFiles.map((file, index) => {
              const path = textValue(file.file_path, '');
              return (
                <tr key={`${path}-${index}`}>
                  <th scope="row" className={`${CELL} min-w-40 max-w-xs break-all font-mono`}>{path || '路径未报告'}</th>
                  <td className={`${CELL} min-w-20`}>{displayLabel(file.status, SOURCE_STATUS)}{textValue(file.error_message, '') && <p className="m-0 mt-1 break-words text-[var(--sem-err-fg)]">{file.error_message}</p>}</td>
                  <td className={`${CELL} min-w-24 tabular-nums`}>{fmtBytes(file.size_bytes)} / {fmtMetric(file.item_count)}</td>
                  <td className={`${CELL} min-w-28`}>{fmtTimestamp(file.updated_at)}</td>
                  <td className={`${CELL} whitespace-nowrap`}><button type="button" className={HEALTH_BUTTON} disabled={disabled || !path} onClick={() => onAction({ kind: 'cold-file', filePath: path })}>重建此文件</button></td>
                </tr>
              );
            })}
          </Table>
        ) : <EmptyDetail available={Array.isArray(data?.source_coverage?.cold_files)}>最近缓存记录中没有可重建的冷文件。</EmptyDetail>}
      </section>
      <ProbeDetails data={data} />
      <Logs telemetry={telemetry} />
    </Panel>
  );
}

export function HealthDiagnostics(props: {
  data?: AdminHealthResp; telemetry?: ExecutionTelemetryResp; busy: boolean; blocked: boolean; onAction: (action: HealthAction) => void;
}) {
  return (
    <section aria-label="诊断明细" className="space-y-2">
      <Sources data={props.data} />
      <Jobs data={props.data} />
      <Operations {...props} />
    </section>
  );
}
