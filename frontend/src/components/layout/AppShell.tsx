import React, { useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import {
  Activity,
  BookText,
  Boxes,
  CircleHelp,
  LogOut,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Sun,
} from 'lucide-react';
import { apiFetch } from '../../lib/api';
import { helpTopicForPath } from '../../lib/helpTopics';
import { useAppStore } from '../../lib/store';
import { FloatingCompareDock } from '../ui/FloatingCompareDock';
import { ToastContainer } from '../ui/Skeletons';
import { CommandPalette } from './CommandPalette';
import { ApplicationNavigation, MobileNavigation } from './ApplicationNavigation';

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
      <header className="sticky top-0 z-30 h-14 border-b border-[var(--border-subtle)] bg-[var(--bg-surface)]/92 backdrop-blur-md px-2.5 flex items-center justify-between gap-2 sm:gap-4 sm:px-4">
        <div className="flex items-center gap-1.5 min-w-0 sm:gap-3">
          <MobileNavigation pathname={location.pathname} />
          {!isHome && (
            <button
              type="button"
              onClick={() => setCollapsed(!collapsed)}
              aria-label={collapsed ? '展开侧边栏' : '折叠侧边栏'}
              aria-controls="desktop-navigation"
              aria-expanded={!collapsed}
              className="hidden rounded-lg p-1.5 text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] hover:text-[var(--text-primary)] cursor-pointer md:inline-flex"
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

        <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
          <button
            type="button"
            onClick={() => setCmdOpen(true)}
            aria-label="检索模型、批次和基准"
            className="inline-flex items-center gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)] cursor-pointer"
          >
            <Search className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">检索模型 / 批次 / 基准...</span>
            <kbd className="hidden rounded border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-1.5 py-0.5 text-[10px] font-mono text-[var(--text-muted)] sm:inline">
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
            id="desktop-navigation"
            className={`sticky top-14 hidden h-[calc(100dvh-3.5rem)] shrink-0 self-start overflow-x-hidden overflow-y-auto overscroll-contain border-r border-[var(--border-subtle)] bg-[var(--bg-surface)] transition-[width] duration-200 motion-reduce:transition-none md:block ${
              collapsed ? 'w-16' : 'w-[232px]'
            }`}
          >
            <ApplicationNavigation collapsed={collapsed} />
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
