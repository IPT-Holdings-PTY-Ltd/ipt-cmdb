import type { AuthProvider } from 'react-admin';
import { apiFetch, getSession, setSession } from './session';
import type { User } from './types';

export type AuthConfiguration = {
  mode: string;
  external: boolean;
  localLoginEnabled: boolean;
  externalLoginUrl: string;
  externalLogoutUrl: string;
};

let authConfiguration: AuthConfiguration | null = null;

export async function getAuthConfiguration(): Promise<AuthConfiguration> {
  if (!authConfiguration) authConfiguration = await apiFetch<AuthConfiguration>('/api/auth/config');
  return authConfiguration;
}

export const authProvider: AuthProvider = {
  async login({ username, password }) {
    const configuration = await getAuthConfiguration();
    if (configuration.external && !configuration.localLoginEnabled) {
      window.location.assign(configuration.externalLoginUrl);
      return;
    }
    const session = await apiFetch<{ token: string; expiresAt: string; user: User }>('/api/login', {
      method: 'POST',
      body: JSON.stringify({ email: username, password }),
    });
    setSession(session);
  },
  async logout() {
    const configuration = await getAuthConfiguration();
    const session = getSession();
    if (session) await apiFetch('/api/logout', { method: 'POST' }).catch(() => undefined);
    setSession(null);
    if (configuration.external) window.location.assign(configuration.externalLogoutUrl);
  },
  async checkAuth() {
    const configuration = await getAuthConfiguration();
    if (configuration.external) {
      const user = await apiFetch<User>('/api/me');
      setSession({ token: '', expiresAt: new Date(Date.now() + 60 * 60 * 1000).toISOString(), user });
      return;
    }
    const session = getSession();
    if (!session) throw new Error('Sign in required');
    const user = await apiFetch<User>('/api/me');
    setSession({ ...session, user });
  },
  async checkError(error) {
    if (error?.status === 401) {
      setSession(null);
      throw error;
    }
  },
  async getIdentity() {
    let user = getSession()?.user;
    if (!user && (await getAuthConfiguration()).external) user = await apiFetch<User>('/api/me');
    if (!user) throw new Error('Sign in required');
    return { id: user.id, fullName: user.email };
  },
  async getPermissions() {
    return getSession()?.user.role || 'anonymous';
  },
  async canAccess({ action }) {
    const role = getSession()?.user.role;
    if (role === 'platform_admin') return true;
    if (role === 'msp_operator') return action !== 'delete';
    return ['list', 'show', 'read'].includes(action);
  },
};
