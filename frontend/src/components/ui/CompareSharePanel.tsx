import { DismissibleDetails } from './DismissibleDetails';
import { useId, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { ChevronDown, Copy, Link2, Share2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { apiFetch } from '../../lib/api';

type ShareMode = 'snapshot' | 'live';

const shareModes = [
  { id: 'snapshot', label: '冻结链接', description: '保留当前结果，适合评审、复盘或记录版本。' },
  { id: 'live', label: '动态链接', description: '保留所选对象，每次打开时用它们的最新数据重新计算。' },
] as const;

export function compareShareModeLabel(mode: string) {
  return shareModes.find((item) => item.id === mode)?.label ?? mode;
}

function projectLabel(project: string) {
  if (project.startsWith('personal:')) return '个人项目';
  if (project === 'default') return '默认项目';
  return project;
}

export function CompareSharePanel({
  comparisonId,
  defaultName,
  projects,
  frozenOnly,
  onCreated,
}: {
  comparisonId: string;
  defaultName: string;
  projects: string[];
  frozenOnly: boolean;
  onCreated: () => void;
}) {
  const modeId = useId();
  const linkInput = useRef<HTMLInputElement>(null);
  const [name, setName] = useState(defaultName);
  const [project, setProject] = useState('');
  const [mode, setMode] = useState<ShareMode>('snapshot');
  const [copyStatus, setCopyStatus] = useState('');
  const projectId = projects.includes(project) ? project : projects[0];
  const selectedMode = frozenOnly ? 'snapshot' : mode;

  const save = useMutation({
    mutationFn: () => apiFetch<{ short_id: string; mode: ShareMode }>('/api/v1/compare/saved', {
      method: 'POST',
      body: JSON.stringify({ name: name.trim(), mode: selectedMode, project_id: projectId, comparison_id: comparisonId }),
    }),
    onSuccess: () => {
      setCopyStatus('');
      onCreated();
    },
  });

  const sharePath = save.data ? `/compare/s/${encodeURIComponent(save.data.short_id)}` : '';
  const shareUrl = sharePath ? new URL(sharePath, window.location.origin).href : '';

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopyStatus('链接已复制');
    } catch {
      linkInput.current?.focus();
      linkInput.current?.select();
      setCopyStatus('链接已选中，请手动复制。');
    }
  }

  const control = 'w-full min-w-0 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-[var(--text-primary)]';

  return (
    <DismissibleDetails className="group/share min-w-0" summary={(
      <summary className="inline-flex cursor-pointer list-none items-center gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--brand-tint)] px-3 py-2 font-semibold text-[var(--brand-primary)] hover:border-[var(--brand-primary)] [&::-webkit-details-marker]:hidden">
        <Share2 aria-hidden="true" className="size-4" />
        分享对比
        <ChevronDown aria-hidden="true" className="size-3.5 group-open/share:rotate-180" />
      </summary>
    )}>
      <div className="mt-3 border-t border-[var(--border-subtle)] pt-3">
        <p className="mb-3 text-[var(--text-secondary)]">生成链接以重开这组对比。打开者需拥有所选项目及来源数据的访问权限。</p>
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (name.trim() && projectId && !save.isPending) save.mutate();
          }}
        >
          <fieldset className="min-w-0">
            <legend className="mb-1.5 font-semibold text-[var(--text-primary)]">链接类型</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              {shareModes.filter(({ id }) => !frozenOnly || id === 'snapshot').map(({ id, label, description }) => (
                <label key={id} className="flex cursor-pointer items-start gap-2 rounded-lg border border-[var(--border-subtle)] px-3 py-2.5 has-checked:border-[var(--brand-primary)] has-checked:bg-[var(--brand-tint)]">
                  <input
                    type="radio"
                    name={modeId}
                    value={id}
                    checked={selectedMode === id}
                    onChange={() => setMode(id)}
                    className="mt-0.5 shrink-0 accent-[var(--brand-primary)]"
                  />
                  <span>
                    <span className="block font-semibold text-[var(--text-primary)]">{label}</span>
                    <span className="mt-0.5 block leading-relaxed text-[var(--text-secondary)]">{description}</span>
                  </span>
                </label>
              ))}
            </div>
            {frozenOnly && <p className="mt-1.5 text-[var(--text-secondary)]">此对比已固定数据集范围，仅可生成冻结链接。</p>}
          </fieldset>
          <div className="grid items-end gap-2.5 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_auto]">
            <label className="grid min-w-0 gap-1.5 font-semibold text-[var(--text-primary)]">
              链接名称
              <input type="text" value={name} onChange={(event) => setName(event.target.value)} required className={control + ' font-normal'} />
            </label>
            <label className="grid min-w-0 gap-1.5 font-semibold text-[var(--text-primary)]">
              存放项目
              <select value={projectId} onChange={(event) => setProject(event.target.value)} className={control + ' font-normal'}>
                {projects.map((id) => <option key={id} value={id}>{projectLabel(id)}</option>)}
              </select>
            </label>
            <button type="submit" disabled={!name.trim() || !projectId || save.isPending} className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-[var(--brand-primary)] px-3 py-2 font-semibold text-white cursor-pointer hover:bg-[var(--brand-hover)] disabled:cursor-not-allowed disabled:opacity-50">
              <Link2 aria-hidden="true" className="size-4" />
              {save.isPending ? '正在生成…' : '生成链接'}
            </button>
          </div>
          {save.isError && <p role="alert" className="text-[var(--sem-err-fg)]">{save.error.message}</p>}
        </form>
        {save.data && (
          <div className="mt-3 space-y-2 rounded-lg border border-[var(--sem-pass-border)] bg-[var(--sem-pass-bg)] p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span role="status" className="font-semibold text-[var(--sem-pass-fg)]">已生成{compareShareModeLabel(save.data.mode)}</span>
              <Link to={sharePath} className="font-semibold text-[var(--brand-primary)] hover:underline">打开链接</Link>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <input ref={linkInput} aria-label="对比链接" readOnly value={shareUrl} className={control + ' flex-1'} />
              <button type="button" onClick={copyLink} className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-2 font-semibold text-[var(--brand-primary)] hover:border-[var(--brand-primary)]">
                <Copy aria-hidden="true" className="size-3.5" /> 复制链接
              </button>
            </div>
            <p role="status" className="text-[var(--text-secondary)] empty:hidden">{copyStatus}</p>
          </div>
        )}
      </div>
    </DismissibleDetails>
  );
}
