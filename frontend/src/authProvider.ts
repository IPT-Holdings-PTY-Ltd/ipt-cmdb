import type { AuthProvider } from 'react-admin';
import { apiFetch, getSession, setSession } from './session';
import type { User } from './types';

export const authProvider: AuthProvider = {
  async login({ username, password }) {
    const session = await apiFetch<{ token: string; expiresAt: string; user: User }>('/api/login', {
      method: 'POST',
      body: JSON.stringify({ email: username, password }),
    });
    setSession(session);
  },
  async logout() {
    const session = getSession();
    if (session) await apiFetch('/api/logout', { method: 'POST' }).catch(() => undefined);
    setSession(null);
  },
  async checkAuth() {
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
    const user = getSession()?.user;
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
