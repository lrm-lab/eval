import { useEffect, useRef, useState } from 'react';
import { NavLink } from 'react-router-dom';
import {
  Activity, BarChart3, Compass, Cpu, Database, FileCheck2, FileSpreadsheet, FlaskConical,
  GitCompareArrows, Home, Layers, Menu, Play, Sliders, TrendingUp, Trophy, Wrench, X, Zap,
  type LucideIcon,
} from 'lucide-react';

const NAV_GROUPS: { label: string; items: { to: string; label: string; icon: LucideIcon; end?: boolean }[] }[] = [
  {
    label: '浏览资源',
    items: [
      { to: '/', label: '平台首页', icon: Home, end: true },
      { to: '/datasets', label: 'Datasets 能力树', icon: Database },
      { to: '/benchmarks', label: 'Benchmark 档案', icon: BarChart3 },
      { to: '/models', label: '模型档案', icon: Cpu },
      { to: '/harnesses', label: 'Harness 档案', icon: Wrench },
    ],
  },
  {
    label: '执行与校验',
    items: [
      { to: '/eval/execution', label: '受控评测执行', icon: Play },
      { to: '/eval/direct-lab', label: 'Direct 原生验证台', icon: Zap },
      { to: '/toolkit/verify', label: '离线校验与算分', icon: FileCheck2 },
      { to: '/toolkit/params', label: '运行参数对照台账', icon: Sliders },
    ],
  },
  {
    label: '结果分析',
    items: [
      { to: '/leaderboard', label: '综合主榜 & 效率图', icon: Trophy },
      { to: '/eval/reports', label: '评测报告中心', icon: FileSpreadsheet },
      { to: '/eval/compare', label: '动态自由对比', icon: GitCompareArrows },
      { to: '/eval/diagnostics', label: '深度诊断与线索', icon: Activity },
      { to: '/recommend', label: '约束选型推荐', icon: Compass },
    ],
  },
  {
    label: '合成与训练',
    items: [
      { to: '/synth/datasets', label: '合成题库工坊', icon: FlaskConical },
      { to: '/synth/selection', label: 'RL/SFT 四通道选题', icon: Layers },
      { to: '/training/opd', label: '训练与 OPD 监控', icon: TrendingUp },
    ],
  },
];

export function ApplicationNavigation({ collapsed = false, onNavigate }: {
  collapsed?: boolean;
  onNavigate?: () => void;
}) {
  return (
    <nav aria-label="应用导航" className="space-y-2 px-2.5 py-3">
      {NAV_GROUPS.map((group) => (
        <div key={group.label} role="group" aria-label={group.label}
          className="border-t border-[var(--border-subtle)] pt-2 first:border-t-0 first:pt-0"
        >
          {!collapsed && (
            <p aria-hidden="true" className="mb-1 px-2.5 text-[11px] leading-5 font-semibold tracking-wider text-[var(--text-secondary)]">
              {group.label}
            </p>
          )}
          <ul className="space-y-0.5">
            {group.items.map(({ icon: Icon, ...item }) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end}
                  onClick={onNavigate}
                  aria-label={collapsed ? item.label : undefined}
                  title={collapsed ? item.label : undefined}
                  className={({ isActive }) => [
                    'group relative flex h-10 items-center rounded-lg border text-[13px] no-underline transition-colors motion-reduce:transition-none md:h-8',
                    collapsed ? 'justify-center' : 'gap-2.5 px-2.5',
                    isActive
                      ? 'border-[var(--brand-primary)]/25 bg-[var(--brand-tint)] font-semibold text-[var(--text-primary)] before:absolute before:inset-y-2 before:-left-px before:w-0.5 before:rounded-full before:bg-[var(--brand-primary)]'
                      : 'border-transparent font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)]',
                  ].join(' ')}
                >
                  <Icon aria-hidden="true" className="size-4 shrink-0 group-aria-[current=page]:text-[var(--brand-primary)]" />
                  {!collapsed && <span className="truncate">{item.label}</span>}
                </NavLink>
              </li>
            ))}
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
