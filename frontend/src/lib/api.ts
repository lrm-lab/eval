import type { ApiErrorPayload } from '../types/api';

export class ApiError extends Error {
  status: number;
  code: string;
  detail?: Record<string, unknown>;
  requestId?: string;

  constructor(status: number, code: string, message: string, detail?: Record<string, unknown>, requestId?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.requestId = requestId;
  }
}

export function getCookieValue(name: string): string | null {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const m = document.cookie.match(new RegExp(`(?:^|;\\s*)${escaped}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : null;
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit & { skipAuthRedirect?: boolean } = {}
): Promise<T> {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has('Content-Type') && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = getCookieValue('lrm_eval_csrf');
    if (csrf) {
      headers.set('X-CSRF-Token', csrf);
    }
  }

  const res = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: 'include',
  });

  if (res.status === 401 && !options.skipAuthRedirect && !window.location.pathname.startsWith('/login')) {
    const returnTo = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.assign(`/login?returnTo=${returnTo}${window.location.hash}`);
    throw new ApiError(401, 'UNAUTHORIZED', '会话已过期，请重新登录');
  }

  if (!res.ok) {
    let payload: ApiErrorPayload | null = null;
    try {
      payload = (await res.json()) as ApiErrorPayload;
    } catch {
      payload = null;
    }
    const err = payload?.error;
    throw new ApiError(
      res.status,
      err?.code || `HTTP_${res.status}`,
      err?.message || `请求失败 (${res.status})`,
      err?.detail,
      err?.request_id
    );
  }

  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

export async function apiDownload(path: string, body: unknown, fallbackFilename: string): Promise<void> {
  const headers = new Headers({ 'Content-Type': 'application/json' });
  const csrf = getCookieValue('lrm_eval_csrf');
  if (csrf) headers.set('X-CSRF-Token', csrf);

  const res = await fetch(path, {
    method: 'POST',
    headers,
    credentials: 'include',
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = `导出失败 (${res.status})`;
    try {
      const j = (await res.json()) as ApiErrorPayload;
      msg = j.error?.message || msg;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, 'EXPORT_FAILED', msg);
  }
  const blob = await res.blob();
  const disp = res.headers.get('content-disposition') || '';
  const match = disp.match(/filename="?([^"]+)"?/i);
  const filename = match ? match[1] : fallbackFilename;
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
