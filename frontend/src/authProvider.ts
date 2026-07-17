import type { AuthProvider } from 'react-admin';
import { apiFetch, getSession, setSession, type Session } from './session';
import type { User } from './types';

export type AuthConfiguration = {
  mode: string;
  external: boolean;
  localLoginEnabled: boolean;
  externalLoginUrl: string;
  externalLogoutUrl: string;
  mfaAvailable: boolean;
  localMfaPolicy: string;
};

export type MfaChallenge = {
  mfaRequired: true;
  mfaEnrollmentRequired: boolean;
  challengeToken: string;
  expiresAt: string;
};

export type MfaEnrollment = {
  status: 'pending';
  manualKey: string;
  provisioningUri: string;
  qrCodeDataUri: string;
};

export type MfaLoginResult = Session & { recoveryCodes?: string[] };

export class MfaRequiredError extends Error {
  constructor(public challenge: MfaChallenge) {
    super(challenge.mfaEnrollmentRequired ? 'Authenticator setup required' : 'Authenticator code required');
  }
}

export function startLoginMfaEnrollment(challengeToken: string) {
  return apiFetch<MfaEnrollment>('/api/login/mfa/enrollment', {
    method: 'POST', body: JSON.stringify({ challengeToken }),
  });
}

export function completeLoginMfa(challengeToken: string, code: string) {
  return apiFetch<MfaLoginResult>('/api/login/mfa', {
    method: 'POST', body: JSON.stringify({ challengeToken, code }),
  });
}

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
    const session = await apiFetch<Session | MfaChallenge>('/api/login', {
      method: 'POST',
      body: JSON.stringify({ email: username, password }),
    });
    if ('mfaRequired' in session) throw new MfaRequiredError(session);
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
