import { apiFetch, getSession, setSession, type Session } from './session';
import type { User } from './types';

export type AuthConfiguration = {
  mode: string;
  external: boolean;
  localLoginEnabled: boolean;
  passwordResetAvailable: boolean;
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

/** Authenticates a local account and stores its short-lived browser session. */
export async function login(username: string, password: string) {
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
}

/** Ends the local session and, when configured, the external identity session. */
export async function logout() {
  const configuration = await getAuthConfiguration();
  const session = getSession();
  if (session) await apiFetch('/api/logout', { method: 'POST' }).catch(() => undefined);
  setSession(null);
  if (configuration.external) {
    window.location.assign(configuration.externalLogoutUrl);
    return;
  }
  window.location.assign('/#/login');
}

/**
 * Validates the current identity against the API before rendering protected
 * routes. External sessions are represented locally without persisting a token.
 */
export async function checkAuth() {
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
}
