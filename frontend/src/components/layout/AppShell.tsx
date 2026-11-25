import React, { useState } from 'react';
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom';
import {
  Activity,
  BarChart3,
  BookText,
  Boxes,
  CircleHelp,
  Compass,
  Cpu,
  Database,
  FileCheck2,
  FileSpreadsheet,
  FlaskConical,
  GitCompareArrows,
  Home,
  Layers,
  LogOut,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Play,
  Search,
  Sliders,
  Sun,
  TrendingUp,
  Trophy,
  Wrench,
  Zap,
} from 'lucide-react';
import { apiFetch } from '../../lib/api';
import { useAppStore } from '../../lib/store';
import { FloatingCompareDock } from '../ui/FloatingCompareDock';
import { ToastContainer } from '../ui/Skeletons';
import { CommandPalette } from './CommandPalette';
import { helpTopicForPath } from '../../pages/MethodologyPage';

const NAV_ITEMS = [
  { to: '/', label: '平台首页', icon: Home, end: true },
  { to: '/leaderboard', label: '综合主榜 & 效率图', icon: Trophy },
  { to: '/eval/compare', label: '动态自由对比', icon: GitCompareArrows },
  { to: '/eval/diagnostics', label: '深度诊断与线索', icon: Activity },
  { to: '/datasets', label: 'Datasets 能力树', icon: Database },
  { to: '/eval/reports', label: '评测报告中心', icon: FileSpreadsheet },
  { to: '/eval/execution', label: '受控评测执行', icon: Play },
  { to: '/eval/direct-lab', label: 'Direct 原生验证台', icon: Zap },
  { to: '/synth/datasets', label: '合成题库工坊', icon: FlaskConical },
  { to: '/synth/selection', label: 'RL/SFT 四通道选题', icon: Layers },
  { to: '/training/opd', label: '训练与 OPD 监控', icon: TrendingUp },
  { to: '/models', label: '模型档案', icon: Cpu },
  { to: '/harnesses', label: 'Harness 档案', icon: Wrench },
  { to: '/benchmarks', label: 'Benchmark 档案', icon: BarChart3 },
  { to: '/recommend', label: '约束选型推荐', icon: Compass },
  { to: '/toolkit/verify', label: '离线校验与算分', icon: FileCheck2 },
  { to: '/toolkit/params', label: '运行参数对照台账', icon: Sliders },
];

function ThemeToggleButton() {
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);
  return (
    <button
      type="button"
      onClick={toggleTheme}
      title={theme === 'light' ? '切换深色主题' : '切换浅色主题'}
      aria-label="切换明暗主题"
      className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-1.5 text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
    >
      {theme === 'light' ? <Moon className="w-4 h-4" /> : <Sun className="w-4 h-4" />}
    </button>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const isHome = location.pathname === '/';

  const user = useAppStore((s) => s.user);
  const setUser = useAppStore((s) => s.setUser);
  const collapsed = useAppStore((s) => s.sidebarCollapsed);
  const setCollapsed = useAppStore((s) => s.setSidebarCollapsed);
  const setCmdOpen = useAppStore((s) => s.setCommandPaletteOpen);
  const pushToast = useAppStore((s) => s.pushToast);

  const [userMenuOpen, setUserMenuOpen] = useState(false);

  const handleLogout = async () => {
    try {
      await apiFetch('/api/v1/auth/logout', { method: 'POST', skipAuthRedirect: true });
    } catch {
      /* ignore */
    }
    setUser(null);
    pushToast('info', '已安全退出登录');
    navigate('/login');
  };

  return (
    <div className="min-h-screen flex flex-col bg-[var(--bg-canvas)] text-[var(--text-primary)]">
      {/* 56px Fixed Topbar */}
      <header className="sticky top-0 z-30 h-14 border-b border-[var(--border-subtle)] bg-[var(--bg-surface)]/92 backdrop-blur-md px-4 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          {!isHome && (
            <button
              type="button"
              onClick={() => setCollapsed(!collapsed)}
              aria-label={collapsed ? '展开侧边栏' : '折叠侧边栏'}
              className="rounded-lg p-1.5 text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)] cursor-pointer"
            >
              {collapsed ? <PanelLeftOpen className="w-4 h-4" /> : <PanelLeftClose className="w-4 h-4" />}
            </button>
          )}
          <Link to="/" className="flex items-center gap-2 no-underline text-[var(--text-primary)] shrink-0">
            <span className="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-[var(--brand-primary)] text-white">
              <Boxes className="w-4 h-4" />
            </span>
            <span className="font-bold text-sm tracking-tight">LRM Eval</span>
            <span className="hidden sm:inline-block rounded-full px-2 py-0.5 text-[10px] font-mono bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-secondary)]">
              v2.2.1
            </span>
          </Link>

          {isHome && (
            <nav className="hidden md:flex items-center gap-1 ml-3">
              {[
                { to: '/leaderboard', label: 'Leaderboard' },
                { to: '/datasets', label: 'Datasets' },
                { to: '/eval/compare', label: 'Compare' },
                { to: '/eval/reports', label: 'Reports' },
                { to: '/models', label: 'Models' },
                { to: '/benchmarks', label: 'Benchmarks' },
              ].map((l) => (
                <Link
                  key={l.to}
                  to={l.to}
                  className="rounded-lg px-2.5 py-1.5 text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-elevated)] no-underline transition-colors"
                >
                  {l.label}
                </Link>
              ))}
            </nav>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setCmdOpen(true)}
            className="inline-flex items-center gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)] cursor-pointer"
          >
            <Search className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">检索模型 / 批次 / 基准...</span>
            <kbd className="rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-1.5 py-0.5 text-[10px] font-mono text-[var(--text-muted)]">
              ⌘K
            </kbd>
          </button>

          <Link
            to={`/toolkit/methodology#${helpTopicForPath(location.pathname)}`}
            aria-label="评测帮助"
            title="评测帮助"
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-1.5 text-[var(--text-secondary)] hover:text-[var(--brand-primary)]"
          >
            <CircleHelp className="h-4 w-4" aria-hidden="true" />
          </Link>

          <ThemeToggleButton />

          {user && (
            <div className="relative">
              <button
                type="button"
                onClick={() => setUserMenuOpen((v) => !v)}
                aria-expanded={userMenuOpen}
                aria-label="用户菜单"
                className="inline-flex items-center gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs font-medium text-[var(--text-primary)] hover:border-[var(--border-strong)] cursor-pointer"
              >
                <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-[var(--brand-tint)] text-[var(--brand-primary)] text-[11px] font-bold uppercase">
                  {user.display_name.slice(0, 1)}
                </span>
                <span className="hidden sm:inline max-w-[100px] truncate">{user.display_name}</span>
                <span className="rounded px-1 py-0.2 text-[10px] uppercase bg-[var(--bg-surface)] text-[var(--text-muted)]">
                  {user.role}
                </span>
              </button>

              {userMenuOpen && (
                <div
                  className="lrm-glass absolute right-0 top-11 z-40 w-60 rounded-xl p-2 text-xs"
                >
                  <div className="px-2.5 py-2 border-b border-[var(--border-subtle)]">
                    <div className="font-semibold text-[var(--text-primary)]">{user.display_name}</div>
                    <div className="text-[11px] text-[var(--text-muted)]">
                      @{user.username} · 项目: {user.projects.join(', ') || '无'}
                    </div>
                  </div>
                  <div className="py-1.5 space-y-1">
                    <Link
                      to="/toolkit/manuals"
                      onClick={() => setUserMenuOpen(false)}
                      className="w-full flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)] no-underline"
                    >
                      <BookText className="w-3.5 h-3.5" /> 框架与平台手册
                    </Link>

                    {user.role === 'admin' && (
                      <Link
                        to="/admin/health"
                        onClick={() => setUserMenuOpen(false)}
                        className="w-full flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)] no-underline"
                      >
                        <Activity className="w-3.5 h-3.5" /> 系统监控
                      </Link>
                    )}

                    <button
                      type="button"
                      onClick={handleLogout}
                      className="w-full flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[var(--sem-err-fg)] hover:bg-[var(--sem-err-bg)] cursor-pointer"
                    >
                      <LogOut className="w-3.5 h-3.5" /> 退出登录
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </header>

      <div className="flex-1 flex min-h-0">
        {!isHome && (
          <aside
            className={`shrink-0 border-r border-[var(--border-subtle)] bg-[var(--bg-surface)] transition-[width] duration-200 hidden md:flex flex-col py-3 ${
              collapsed ? 'w-16 px-2' : 'w-56 px-3'
            }`}
          >
            <nav className="space-y-1 flex-1">
              {NAV_ITEMS.map((item) => {
                const Icon = item.icon;
                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    title={collapsed ? item.label : undefined}
                    className={({ isActive }) =>
                      `flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs font-medium no-underline transition-colors ${
                        isActive
                          ? 'bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                          : 'text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)]'
                      }`
                    }
                  >
                    <Icon className="w-4 h-4 shrink-0" />
                    {!collapsed && <span className="truncate">{item.label}</span>}
                  </NavLink>
                );
              })}

            </nav>
          </aside>
        )}

        <main className="flex-1 min-w-0 pb-20">{children}</main>
      </div>

      <CommandPalette />
      <FloatingCompareDock />
      <ToastContainer />
    </div>
  );
}
