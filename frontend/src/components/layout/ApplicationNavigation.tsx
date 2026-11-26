import { useEffect, useRef, useState } from 'react';
import { Link, matchPath, useLocation } from 'react-router-dom';
import {
  Activity, BarChart3, Compass, Cpu, Database, FileSpreadsheet,
  GitCompareArrows, Home, Menu, Trophy, Wrench, X,
  type LucideIcon,
} from 'lucide-react';

const NAV_GROUPS: { label: string; items: { to: string; label: string; icon: LucideIcon; related?: string }[] }[] = [
  {
    label: '浏览资源',
    items: [
      { to: '/', label: '首页', icon: Home },
      { to: '/models', label: '模型档案', icon: Cpu },
      { to: '/harnesses', label: 'Harness 档案', icon: Wrench },
      { to: '/datasets', label: '数据集', icon: Database },
      { to: '/benchmarks', label: '基准评测', icon: BarChart3 },
    ],
  },
  {
    label: '结果分析',
    items: [
      { to: '/leaderboard', label: '评测主榜', icon: Trophy },
      { to: '/eval/compare', label: '自由对比', icon: GitCompareArrows, related: '/compare' },
      { to: '/eval/reports', label: '评测报告', icon: FileSpreadsheet, related: '/eval/batches' },
      { to: '/eval/diagnostics', label: '失败诊断', icon: Activity },
      { to: '/recommend', label: '选型推荐', icon: Compass },
    ],
  },
];

export function ApplicationNavigation({ collapsed = false, onNavigate }: {
  collapsed?: boolean;
  onNavigate?: () => void;
}) {
  const { pathname } = useLocation();

  return (
    <nav aria-label="应用导航" className="space-y-3 px-2.5 py-3">
      {NAV_GROUPS.map((group) => (
        <div key={group.label} role="group" aria-label={group.label}
          className="border-t border-[var(--border-subtle)] pt-3 first:border-t-0 first:pt-0"
        >
          {!collapsed && (
            <p aria-hidden="true" className="mb-1.5 px-2 text-[11px] leading-5 font-medium tracking-wide text-[var(--text-secondary)]">
              {group.label}
            </p>
          )}
          <ul className="space-y-1">
            {group.items.map(({ icon: Icon, to, label, related }) => {
              const exact = matchPath(to, pathname);
              const active = exact || (to !== '/' && matchPath({ path: to, end: false }, pathname))
                || (related && matchPath({ path: related, end: false }, pathname));
              return (
                <li key={to}>
                  <Link
                    to={to}
                    onClick={onNavigate}
                    aria-label={collapsed ? label : undefined}
                    aria-current={exact ? 'page' : active ? 'location' : undefined}
                    title={collapsed ? label : undefined}
                    className={[
                      'group relative flex min-h-11 items-center rounded-lg border py-1 text-[13px] leading-5 no-underline transition-colors motion-reduce:transition-none md:min-h-10',
                      collapsed ? 'justify-center' : 'gap-2.5 px-2',
                      active
                        ? 'border-[var(--brand-primary)]/30 bg-[var(--brand-tint)] font-semibold text-[var(--text-primary)] before:absolute before:inset-y-2.5 before:-left-px before:w-[3px] before:rounded-full before:bg-[var(--brand-primary)]'
                        : 'border-transparent font-medium text-[var(--text-secondary)] hover:border-[var(--border-subtle)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)]',
                    ].join(' ')}
                  >
                    <span className={[
                      'grid size-7 shrink-0 place-items-center rounded-md',
                      active ? 'bg-[var(--brand-primary)]/15 text-[var(--brand-primary)]' : 'bg-[var(--bg-elevated)]',
                    ].join(' ')}>
                      <Icon aria-hidden="true" className="size-4" />
                    </span>
                    {!collapsed && <span className="min-w-0">{label}</span>}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

export function MobileNavigation({ pathname }: { pathname: string }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    dialog.current?.close();
  }, [pathname]);

  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 48rem)');
    function closeOnDesktop(event: MediaQueryListEvent) {
      if (event.matches) dialog.current?.close();
    }
    desktop.addEventListener('change', closeOnDesktop);
    return () => desktop.removeEventListener('change', closeOnDesktop);
  }, []);

  return (
    <>
      <button
        type="button"
        aria-label="打开导航菜单"
        aria-controls="mobile-navigation"
        aria-expanded={open}
        onClick={() => {
          dialog.current?.showModal();
          setOpen(true);
        }}
        className="grid size-9 shrink-0 cursor-pointer place-items-center rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] md:hidden"
      >
        <Menu aria-hidden="true" className="size-[18px]" />
      </button>
      <dialog
        id="mobile-navigation"
        ref={dialog}
        aria-labelledby="mobile-navigation-title"
        onClose={() => setOpen(false)}
        onClick={(event) => {
          if (event.target === event.currentTarget) dialog.current?.close();
        }}
        className="fixed inset-y-0 left-0 m-0 h-dvh max-h-none w-72 max-w-[calc(100vw-3rem)] overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-surface)] p-0 text-[var(--text-primary)] shadow-[var(--shadow-elevated)] backdrop:bg-[var(--bg-canvas)]/75 md:hidden"
      >
        <div className="flex h-full flex-col">
          <div className="flex h-14 shrink-0 items-center justify-between border-b border-[var(--border-subtle)] px-4">
            <h2 id="mobile-navigation-title" className="text-sm font-semibold">应用导航</h2>
            <button
              type="button"
              aria-label="关闭导航菜单"
              onClick={() => dialog.current?.close()}
              className="grid size-9 cursor-pointer place-items-center rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)]"
            >
              <X aria-hidden="true" className="size-[18px]" />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
            <ApplicationNavigation onNavigate={() => dialog.current?.close()} />
          </div>
        </div>
      </dialog>
    </>
  );
}
