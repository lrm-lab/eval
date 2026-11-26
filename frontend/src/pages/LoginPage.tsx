import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowRight, Boxes, Lock, Moon, Sun, User } from 'lucide-react';
import { apiFetch, ApiError } from '../lib/api';
import { useAppStore } from '../lib/store';
import type { AuthUser } from '../types/api';
import './login-page.css';

const backdrop = new URL('./assets/login-datacenter.webp', import.meta.url).href;
const mobileBackdrop = new URL('./assets/login-datacenter-mobile.webp', import.meta.url).href;
const home = { pathname: '/', search: '', hash: '' };

function authDestination(returnTo: string | null, hash: string) {
  const target = returnTo || '/';
  if (!target.startsWith('/') || target.startsWith('//')) return home;

  try {
    const url = new URL(target, window.location.origin);
    const decodedPath = decodeURIComponent(url.pathname);
    // URL normalization can introduce a leading double slash after removing dot segments.
    if (url.origin !== window.location.origin || /^\/[\\/]/.test(decodedPath)
        || /^\/(login|register)\/*$/i.test(decodedPath)) return home;
    return { pathname: url.pathname, search: url.search, hash: url.hash || hash };
  } catch {
    return home;
  }
}

export function LoginPage({ mode = 'login' }: { mode?: 'login' | 'register' }) {
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
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => request.current?.abort(), []);

  const registering = mode === 'register';
  const submitLabel = registering ? '注册并进入评测' : '登录平台';
  const pendingLabel = registering ? '正在注册…' : '正在验证会话…';
  const destination = authDestination(searchParams.get('returnTo'), hash);
  const alternate = {
    pathname: registering ? '/login' : '/register',
    search: destination.pathname === '/' && !destination.search
      ? '' : `?${new URLSearchParams({ returnTo: destination.pathname + destination.search })}`,
    hash: destination.hash,
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (request.current) return;
    setErrorMsg(null);
    const enteredUsername = username.trim();
    if (!enteredUsername || !password) {
      setErrorMsg('请输入用户名和密码');
      return;
    }
    if (registering && Array.from(enteredUsername).length > 64) {
      setErrorMsg('用户名最多 64 个字符');
      return;
    }
    const passwordLength = Array.from(password).length;
    if (registering && (passwordLength < 8 || passwordLength > 128)) {
      setErrorMsg('密码须为 8–128 个字符');
      return;
    }

    const controller = new AbortController();
    request.current = controller;
    setSubmitting(true);
    try {
      const res = await apiFetch<{ user: AuthUser }>(`/api/v1/auth/${mode}`, {
        method: 'POST',
        body: JSON.stringify({ username: enteredUsername, password }),
        signal: controller.signal,
        skipAuthRedirect: true,
      });
      if (controller.signal.aborted) return;
      setUser(res.user);
      pushToast('success', registering ? '注册成功' : `欢迎回来，${res.user.display_name}`);
      navigate(destination, { replace: true });
    } catch (err) {
      if (controller.signal.aborted) return;
      if (registering && err instanceof ApiError && err.status === 409) {
        setErrorMsg('用户名已被使用，请换一个用户名');
      } else {
        setErrorMsg(err instanceof ApiError ? err.message : `${registering ? '注册' : '登录'}失败，请检查网络后重试`);
      }
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setSubmitting(false);
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
            <h2 id="login-form-title">{registering ? '注册账号' : '登录'}</h2>
            {!registering && <p>使用已有账号继续</p>}
          </div>

          <form onSubmit={handleSubmit} aria-busy={submitting} className="login-form">
            <div className="login-field">
              <label htmlFor={`${mode}-username`}>用户名</label>
              <div className="login-input">
                <User size={17} aria-hidden="true" />
                <input
                  id={`${mode}-username`}
                  name="username"
                  type="text"
                  autoComplete="username"
                  autoCapitalize="none"
                  spellCheck={false}
                  autoFocus={focusUsername}
                  aria-describedby={errorMsg ? 'login-error' : undefined}
                  readOnly={submitting}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </div>
            </div>

            <div className="login-field">
              <label htmlFor={`${mode}-password`}>密码</label>
              <div className="login-input">
                <Lock size={17} aria-hidden="true" />
                <input
                  id={`${mode}-password`}
                  name="password"
                  type="password"
                  autoComplete={registering ? 'new-password' : 'current-password'}
                  aria-describedby={[registering && 'register-password-hint', errorMsg && 'login-error'].filter(Boolean).join(' ') || undefined}
                  readOnly={submitting}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
              {registering && <p id="register-password-hint" className="login-hint">8–128 个字符</p>}
            </div>

            {errorMsg && (
              <p id="login-error" role="alert" className="login-error">{errorMsg}</p>
            )}

            <button type="submit" disabled={submitting} className="login-submit">
              <span>{submitting ? pendingLabel : submitLabel}</span>
              <ArrowRight size={18} aria-hidden="true" />
            </button>
          </form>
          <p className="login-alternate">
            {registering ? '已有账号？' : '还没有账号？'}
            <Link to={alternate}>{registering ? '返回登录' : '注册账号'}</Link>
          </p>
        </section>
      </div>
    </main>
  );
}
