import { useId, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronLeft, ChevronRight, Database, Search, X } from 'lucide-react';
import type { EvaluationChoice } from '../../lib/homeEvaluation';

const tracks = [
  ['agentic', 'Agentic'],
  ['llm-text', '文本'],
  ['vision-image', '视觉'],
  ['synthetic', '合成'],
  ['extended', '扩展'],
  ['custom', '自定义'],
] as const;
const trackNames = new Map<string, string>(tracks);
const recentKey = 'lrm-eval-recent-home-datasets';
const iconButton = 'grid min-h-10 w-10 shrink-0 cursor-pointer place-items-center rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-[var(--brand-primary)] disabled:cursor-default disabled:opacity-35';

function readRecent(): string[] {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(recentKey) ?? '[]');
    return Array.isArray(saved) ? saved.filter((key): key is string => typeof key === 'string').slice(0, 3) : [];
  } catch {
    return [];
  }
}

export function HomeDatasetSwitcher({ choices, subject, label, pending, failed, onRetry, onChange }: {
  choices: EvaluationChoice[];
  subject: string;
  label: string;
  pending: boolean;
  failed: boolean;
  onRetry: () => void;
  onChange: (key: string) => void;
}) {
  const titleId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const [recent, setRecent] = useState(readRecent);
  const groups = new Map<string, EvaluationChoice[]>([...tracks.map(([key]) => [key, []] as [string, EvaluationChoice[]]), ['other', []]]);
  for (const choice of choices) {
    const track = choice.track && trackNames.has(choice.track) ? choice.track : 'other';
    groups.get(track)?.push(choice);
  }
  const ordered = [...groups.values()].flat();
  const index = ordered.findIndex((choice) => choice.key === subject);
  const previous = index > 0 ? ordered[index - 1] : undefined;
  const next = ordered[index + 1];
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const visible = new Set(ordered.filter((choice) => {
    const haystack = [choice.search, choice.label, choice.track, trackNames.get(choice.track ?? '')].join(' ').toLowerCase();
    return terms.every((term) => haystack.includes(term));
  }).map((choice) => choice.key));
  const recentChoices = terms.length ? [] : recent.flatMap((key) => {
    const choice = ordered.find((item) => item.key === key);
    return choice ? [choice] : [];
  });

  function select(key: string) {
    const remembered = [key, ...recent.filter((item) => item !== key)].slice(0, 3);
    setRecent(remembered);
    try {
      localStorage.setItem(recentKey, JSON.stringify(remembered));
    } catch {
      // The picker also works with storage disabled.
    }
    dialog.current?.close();
    if (key !== subject) onChange(key);
  }

  function option(choice: EvaluationChoice) {
    return (
      <li key={choice.key}>
        <button type="button" aria-pressed={choice.key === subject} onClick={() => select(choice.key)}
          className="flex min-h-10 w-full min-w-0 cursor-pointer items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm text-[var(--text-primary)] hover:bg-[var(--bg-elevated)] focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-[var(--brand-primary)]">
          <span className="min-w-0 [overflow-wrap:anywhere]">{choice.label}</span>
          {choice.key === subject && <Check aria-hidden="true" className="size-4 shrink-0 text-[var(--brand-primary)]" />}
        </button>
      </li>
    );
  }

  return (
    <div className="inline-flex min-w-0 items-center gap-0.5 rounded-xl border border-[var(--border-subtle)] p-0.5">
      <button type="button" aria-label="上一个数据集" title={previous?.label} disabled={!previous}
        onClick={() => { if (previous) select(previous.key); }} className={iconButton}>
        <ChevronLeft aria-hidden="true" className="size-4" />
      </button>
      <button ref={trigger} type="button" aria-haspopup="dialog" aria-label={'切换数据集' + (subject ? '，当前 ' + label : '')}
        onClick={() => {
          setQuery('');
          dialog.current?.showModal();
          searchInput.current?.focus();
        }}
        className="inline-flex min-h-10 min-w-0 cursor-pointer items-center gap-1.5 rounded-lg px-2 text-sm font-bold text-[var(--text-primary)] hover:bg-[var(--bg-elevated)] focus-visible:outline-2 focus-visible:outline-[var(--brand-primary)] sm:gap-2 sm:px-3">
        <Database aria-hidden="true" className="hidden size-4 shrink-0 text-[var(--brand-primary)] sm:block" />
        <span className="truncate">{subject ? label : '切换数据集'}</span>
        <ChevronDown aria-hidden="true" className="size-4 shrink-0 text-[var(--text-secondary)]" />
      </button>
      <button type="button" aria-label="下一个数据集" title={next?.label} disabled={!next}
        onClick={() => { if (next) select(next.key); }} className={iconButton}>
        <ChevronRight aria-hidden="true" className="size-4" />
      </button>
      <dialog ref={dialog} aria-labelledby={titleId} onClose={() => trigger.current?.focus()}
        onClick={(event) => { if (event.target === event.currentTarget) dialog.current?.close(); }}
        className="fixed inset-x-0 bottom-0 top-auto m-0 w-full max-w-none overflow-hidden rounded-t-2xl border border-[var(--border-strong)] bg-[var(--bg-surface)] p-0 text-[var(--text-primary)] shadow-2xl backdrop:bg-black/55 sm:inset-0 sm:m-auto sm:max-w-md sm:rounded-2xl">
        <div className="flex max-h-[min(82dvh,36rem)] flex-col p-3 sm:p-4">
          <div className="flex items-center justify-between gap-2">
            <h3 id={titleId} className="m-0 pl-1 text-base font-bold">切换数据集</h3>
            <button type="button" aria-label="关闭数据集选择" className={iconButton} onClick={() => dialog.current?.close()}>
              <X aria-hidden="true" className="size-4" />
            </button>
          </div>
          <form className="relative mb-2" onSubmit={(event) => {
            event.preventDefault();
            const first = ordered.find((choice) => visible.has(choice.key));
            if (terms.length && first) select(first.key);
          }}>
            <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[var(--text-secondary)]" />
            <input ref={searchInput} type="search" aria-label="搜索数据集" placeholder="搜索数据集"
              value={query} onChange={(event) => setQuery(event.target.value)}
              className="w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] py-2.5 pl-9 pr-3 text-base text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-[var(--brand-primary)] sm:text-sm" />
          </form>
          <div className="min-h-0 space-y-3 overflow-y-auto overscroll-contain">
            {failed ? <button type="button" onClick={onRetry} className="px-3 py-2 text-sm text-[var(--brand-primary)]">目录加载失败，重试</button>
              : pending ? <p role="status" className="m-0 px-3 py-2 text-sm text-[var(--text-secondary)]">正在加载目录…</p>
                : visible.size === 0 ? <p role="status" className="m-0 px-3 py-2 text-sm text-[var(--text-secondary)]">没有匹配的数据集。</p>
                  : <>
                    {recentChoices.length > 0 && <div>
                      <h4 className="m-0 px-3 pb-1 text-xs font-semibold text-[var(--text-secondary)]">最近使用</h4>
                      <ul aria-label="最近使用的数据集" className="m-0 list-none p-0">{recentChoices.map(option)}</ul>
                    </div>}
                    {[...groups].map(([track, items]) => {
                      const matching = items.filter((choice) => visible.has(choice.key));
                      if (!matching.length) return null;
                      const title = trackNames.get(track) ?? '其他';
                      return <div key={track}>
                        <h4 className="m-0 px-3 pb-1 text-xs font-semibold text-[var(--text-secondary)]">{title}</h4>
                        <ul aria-label={title + '数据集'} className="m-0 list-none p-0">{matching.map(option)}</ul>
                      </div>;
                    })}
                  </>}
          </div>
        </div>
      </dialog>
    </div>
  );
}
