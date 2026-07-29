import type { User } from './types';

const sessionKey = 'cmdb.react.session';

export type Session = { token: string; expiresAt: string; user: User };

/** HTTP error with the response status and structured API response body. */
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public body: unknown,
    public retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

function responseRetryAfterSeconds(response: Response): number | null {
  const value = response.headers.get('retry-after')?.trim();
  if (!value) return null;
  if (/^\d+$/.test(value)) return Math.max(1, Number.parseInt(value, 10));
  const retryAt = Date.parse(value);
  if (Number.isNaN(retryAt)) return null;
  return Math.max(1, Math.ceil((retryAt - Date.now()) / 1000));
}

function isJsonText(body: BodyInit | null | undefined): body is string {
  if (typeof body !== 'string') return false;
  try {
    JSON.parse(body);
    return true;
  } catch {
    return false;
  }
}

function requestHeaders(init: RequestInit, session: Session | null): Headers {
  const headers = new Headers(init.headers);
  if (isJsonText(init.body) && !headers.has('content-type')) {
    headers.set('content-type', 'application/json');
  }
  if (session && !headers.has('authorization')) {
    headers.set('authorization', `Bearer ${session.token}`);
  }
  return headers;
}

export function getSession(): Session | null {
  try {
    const session = JSON.parse(sessionStorage.getItem(sessionKey) || 'null') as Session | null;
    if (!session || new Date(session.expiresAt) <= new Date()) {
      sessionStorage.removeItem(sessionKey);
      return null;
    }
    return session;
  } catch {
    sessionStorage.removeItem(sessionKey);
    return null;
  }
}

export function setSession(session: Session | null) {
  if (session) sessionStorage.setItem(sessionKey, JSON.stringify(session));
  else sessionStorage.removeItem(sessionKey);
  window.dispatchEvent(new CustomEvent('cmdb-auth-change'));
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const session = getSession();
  const response = await fetch(path, {
    ...init,
    headers: requestHeaders(init, session),
  });
  const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) setSession(null);
    throw new ApiError(
      payload.error || payload.detail || response.statusText,
      response.status,
      payload,
      responseRetryAfterSeconds(response),
    );
  }
  return payload as T;
}

export async function apiDownload(path: string, init: RequestInit = {}): Promise<{ blob: Blob; filename: string }> {
  const session = getSession();
  const response = await fetch(path, {
    ...init,
    headers: requestHeaders(init, session),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) setSession(null);
    throw new ApiError(
      payload.error || payload.detail || response.statusText,
      response.status,
      payload,
      responseRetryAfterSeconds(response),
    );
  }
  const disposition = response.headers.get('content-disposition') || '';
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return { blob: await response.blob(), filename: match?.[1] || 'change-control.pdf' };
}
