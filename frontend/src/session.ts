import { HttpError } from 'react-admin';
import type { User } from './types';

const sessionKey = 'cmdb.react.session';

export type Session = { token: string; expiresAt: string; user: User };

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
    headers: {
      ...(init.body ? { 'content-type': 'application/json' } : {}),
      ...(session ? { Authorization: `Bearer ${session.token}` } : {}),
      ...init.headers,
    },
  });
  const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) setSession(null);
    throw new HttpError(payload.error || payload.detail || response.statusText, response.status, payload);
  }
  return payload as T;
}

export async function apiDownload(path: string): Promise<{ blob: Blob; filename: string }> {
  const session = getSession();
  const response = await fetch(path, { headers: session ? { Authorization: `Bearer ${session.token}` } : {} });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) setSession(null);
    throw new HttpError(payload.error || payload.detail || response.statusText, response.status, payload);
  }
  const disposition = response.headers.get('content-disposition') || '';
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return { blob: await response.blob(), filename: match?.[1] || 'change-control.pdf' };
}
