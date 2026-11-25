import React, { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Boxes, Lock, LogIn, Moon, Sun, User } from 'lucide-react';
import { apiFetch, ApiError } from '../lib/api';
import { useAppStore } from '../lib/store';
import type { AuthUser } from '../types/api';

export function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const setUser = useAppStore((s) => s.setUser);
  const pushToast = useAppStore((s) => s.pushToast);
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const returnToRaw = searchParams.get('returnTo') || '/';
  const safeReturnTo = returnToRaw.startsWith('/') && !returnToRaw.startsWith('//') ? returnToRaw : '/';

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg(null);
    if (!username.trim() || !password) {
      setErrorMsg('请输入用户名和密码');
      return;
    }
    setSubmitting(true);
    try {
      const res = await apiFetch<{ user: AuthUser }>('/api/v1/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username: username.trim(), password }),
        skipAuthRedirect: true,
      });
      setUser(res.user);
      pushToast('success', `欢迎回来，${res.user.display_name}`);
      navigate(safeReturnTo, { replace: true });
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '登录失败，请检查账号或网络';
      setErrorMsg(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-4 py-12 bg-[var(--bg-canvas)] relative overflow-hidden">
      <div className="lrm-hero-ambient pointer-events-none" />

      <button
        type="button"
        onClick={toggleTheme}
        aria-label="切换主题"
        className="fixed top-4 right-4 z-20 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-2 text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
      >
        {theme === 'light' ? <Moon className="w-4 h-4" /> : <Sun className="w-4 h-4" />}
      </button>

      <div className="relative z-10 w-full max-w-md rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-7 shadow-xl">
        <div className="flex items-center gap-3 mb-6">
          <div className="inline-flex items-center justify-center w-10 h-10 rounded-xl bg-[var(--brand-primary)] text-white shadow-sm">
            <Boxes className="w-5 h-5" />
          </div>
          <div>
            <h1 className="m-0 text-lg font-bold tracking-tight text-[var(--text-primary)]">LRM Eval 分析平台</h1>
            <p className="m-0 text-xs text-[var(--text-secondary)]">
              Harbor · Direct · VLMEvalKit · 2026-11-20 三线评测平台
            </p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="login-username" className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">
              用户名
            </label>
            <div className="relative">
              <User className="w-4 h-4 text-[var(--text-muted)] absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                id="login-username"
                type="text"
                autoComplete="username"
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="输入内网账号"
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] pl-9 pr-3 py-2 text-sm text-[var(--text-primary)] focus:border-[var(--brand-primary)] outline-none"
              />
            </div>
          </div>

          <div>
            <label htmlFor="login-password" className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">
              密码
            </label>
            <div className="relative">
              <Lock className="w-4 h-4 text-[var(--text-muted)] absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                id="login-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="输入登录密码"
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] pl-9 pr-3 py-2 text-sm text-[var(--text-primary)] focus:border-[var(--brand-primary)] outline-none"
              />
            </div>
          </div>

          {errorMsg && (
            <div
              role="alert"
              className="rounded-lg border border-[var(--sem-err-border)] bg-[var(--sem-err-bg)] px-3 py-2 text-xs text-[var(--sem-err-fg)]"
            >
              {errorMsg}
            </div>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="w-full inline-flex items-center justify-center gap-2 rounded-lg bg-[var(--brand-primary)] hover:bg-[var(--brand-hover)] disabled:opacity-60 px-4 py-2.5 text-sm font-semibold text-white lrm-press cursor-pointer"
          >
            <LogIn className="w-4 h-4" />
            <span>{submitting ? '正在验证会话...' : '登录平台'}</span>
          </button>
        </form>

        <div className="mt-5 pt-4 border-t border-[var(--border-subtle)] text-[11px] text-[var(--text-muted)] leading-relaxed">
          本平台采用全站认证 + 项目级授权（RBAC）+ 细粒度证据 Scope（ATIF 轨迹、网关抓包、Holdout 题面、标准答案按角色隔离）。
        </div>
      </div>
    </div>
  );
}
