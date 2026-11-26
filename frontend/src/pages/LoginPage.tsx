import React, { useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowRight, Boxes, Lock, Moon, Sun, User } from 'lucide-react';
import { apiFetch, ApiError } from '../lib/api';
import { useAppStore } from '../lib/store';
import type { AuthUser } from '../types/api';
import './login-page.css';

const backdrop = new URL('./assets/login-datacenter.webp', import.meta.url).href;
const mobileBackdrop = new URL('./assets/login-datacenter-mobile.webp', import.meta.url).href;

export function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { hash } = useLocation();
  const setUser = useAppStore((s) => s.setUser);
  const pushToast = useAppStore((s) => s.pushToast);
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [focusUsername] = useState(() => window.matchMedia('(pointer: fine)').matches);

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
      navigate(safeReturnTo.includes('#') ? safeReturnTo : safeReturnTo + hash, { replace: true });
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '登录失败，请检查账号或网络';
      setErrorMsg(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-page" aria-labelledby="login-title">
      <picture className="login-backdrop" aria-hidden="true">
        <source media="(max-width: 760px)" srcSet={mobileBackdrop} />
        <img src={backdrop} alt="" width={1672} height={941} fetchPriority="high" />
      </picture>

      <header className="login-header">
        <div className="login-brand">
          <span className="login-brand-icon"><Boxes size={21} aria-hidden="true" /></span>
          <span>LRM Eval</span>
        </div>
        <button type="button" onClick={toggleTheme} aria-label="切换主题" className="login-theme">
          {theme === 'light' ? <Moon size={19} aria-hidden="true" /> : <Sun size={19} aria-hidden="true" />}
        </button>
      </header>

      <div className="login-layout">
        <div className="login-intro">
          <h1 id="login-title">大模型与 Agent<br />评测平台</h1>
        </div>

        <section className="login-panel" aria-labelledby="login-form-title">
          <div className="login-panel-heading">
            <h2 id="login-form-title">登录</h2>
            <p>使用内网账号继续</p>
          </div>

          <form onSubmit={handleSubmit} aria-busy={submitting} className="login-form">
            <div className="login-field">
              <label htmlFor="login-username">用户名</label>
              <div className="login-input">
                <User size={17} aria-hidden="true" />
                <input
                  id="login-username"
                  name="username"
                  type="text"
                  autoComplete="username"
                  autoCapitalize="none"
                  spellCheck={false}
                  autoFocus={focusUsername}
                  aria-describedby={errorMsg ? 'login-error' : undefined}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </div>
            </div>

            <div className="login-field">
              <label htmlFor="login-password">密码</label>
              <div className="login-input">
                <Lock size={17} aria-hidden="true" />
                <input
                  id="login-password"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  aria-describedby={errorMsg ? 'login-error' : undefined}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
            </div>

            {errorMsg && (
              <p id="login-error" role="alert" className="login-error">{errorMsg}</p>
            )}

            <button type="submit" disabled={submitting} className="login-submit">
              <span>{submitting ? '正在验证会话…' : '登录平台'}</span>
              <ArrowRight size={18} aria-hidden="true" />
            </button>
          </form>
        </section>
      </div>
    </main>
  );
}
