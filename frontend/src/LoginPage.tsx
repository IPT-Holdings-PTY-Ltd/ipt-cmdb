import { useEffect, useState, type FormEvent } from 'react';
import { Alert, Box, Button, Paper, Stack, TextField, Typography } from '@mui/material';
import { useLogin } from 'react-admin';
import { BrandLogo, useMspBranding } from './branding';
import {
  completeLoginMfa,
  getAuthConfiguration,
  MfaRequiredError,
  startLoginMfaEnrollment,
  type AuthConfiguration,
  type MfaChallenge,
  type MfaEnrollment,
  type MfaLoginResult,
} from './authProvider';
import { apiFetch, setSession } from './session';

type LoginMode = 'login' | 'request-reset' | 'reset-password';

function loginQuery() {
  return new URLSearchParams(window.location.hash.split('?', 2)[1] || '');
}

function clearLoginQuery() {
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#/login`);
}

export function LoginPage() {
  const login = useLogin();
  const { brand } = useMspBranding();
  const initialToken = loginQuery().get('resetToken') || '';
  const [mode, setMode] = useState<LoginMode>(initialToken ? 'reset-password' : 'login');
  const [username, setUsername] = useState('admin@example.com');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState(loginQuery().has('passwordChanged') ? 'Password changed. Sign in again with your new password.' : '');
  const [busy, setBusy] = useState(false);
  const [configuration, setConfiguration] = useState<AuthConfiguration | null>(null);
  const [challenge, setChallenge] = useState<MfaChallenge | null>(null);
  const [enrollment, setEnrollment] = useState<MfaEnrollment | null>(null);
  const [code, setCode] = useState('');
  const [pendingSession, setPendingSession] = useState<MfaLoginResult | null>(null);
  const [resetToken, setResetToken] = useState(initialToken);
  const [resetValid, setResetValid] = useState<boolean | null>(initialToken ? null : false);
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [revokeApiTokens, setRevokeApiTokens] = useState(true);

  useEffect(() => {
    getAuthConfiguration().then(setConfiguration).catch(reason => setError(reason instanceof Error ? reason.message : 'Unable to load sign-in configuration'));
  }, []);

  useEffect(() => {
    const syncRoute = () => {
      const token = loginQuery().get('resetToken') || '';
      if (!token) return;
      setResetToken(token);
      setResetValid(null);
      setMode('reset-password');
      setError('');
      setNotice('');
    };
    window.addEventListener('hashchange', syncRoute);
    return () => window.removeEventListener('hashchange', syncRoute);
  }, []);

  useEffect(() => {
    if (mode !== 'reset-password' || !resetToken) return;
    apiFetch<{ valid: boolean }>(`/api/password-reset/validate?token=${encodeURIComponent(resetToken)}`)
      .then(result => setResetValid(result.valid))
      .catch(() => setResetValid(false));
  }, [mode, resetToken]);

  function returnToLogin(message = '') {
    clearLoginQuery();
    setMode('login');
    setChallenge(null);
    setEnrollment(null);
    setError('');
    setNotice(message);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    setNotice('');
    try { await login({ username, password }); }
    catch (reason) {
      if (reason instanceof MfaRequiredError) {
        setChallenge(reason.challenge);
        if (reason.challenge.mfaEnrollmentRequired) {
          try { setEnrollment(await startLoginMfaEnrollment(reason.challenge.challengeToken)); }
          catch (enrollmentError) { setError(enrollmentError instanceof Error ? enrollmentError.message : 'Unable to start authenticator setup'); }
        }
      } else setError(reason instanceof Error ? reason.message : 'Unable to sign in');
    }
    finally { setBusy(false); }
  }

  async function requestReset(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await apiFetch<{ message: string }>('/api/password-reset/request', {
        method: 'POST', body: JSON.stringify({ email: username }),
      });
      setNotice(result.message);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Password reset could not be requested'); }
    finally { setBusy(false); }
  }

  async function completeReset(event: FormEvent) {
    event.preventDefault();
    setError(''); setNotice('');
    if (newPassword !== confirmPassword) { setError('The new passwords do not match.'); return; }
    setBusy(true);
    try {
      const result = await apiFetch<{ message: string }>('/api/password-reset/complete', {
        method: 'POST',
        body: JSON.stringify({ token: resetToken, newPassword, revokeApiTokens }),
      });
      setPassword(''); setNewPassword(''); setConfirmPassword('');
      returnToLogin(result.message);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Password could not be reset'); }
    finally { setBusy(false); }
  }

  async function submitMfa(event: FormEvent) {
    event.preventDefault();
    if (!challenge) return;
    setBusy(true); setError('');
    try {
      const session = await completeLoginMfa(challenge.challengeToken, code);
      if (session.recoveryCodes?.length) setPendingSession(session);
      else { setSession(session); window.location.assign('/#/'); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Authenticator verification failed'); }
    finally { setBusy(false); }
  }

  function finishEnrollment() {
    if (!pendingSession) return;
    setSession(pendingSession);
    window.location.assign('/#/');
  }

  const formSubmit = challenge ? submitMfa : mode === 'request-reset' ? requestReset : mode === 'reset-password' ? completeReset : submit;

  return (
    <Box className="login-page">
      <Paper component="form" onSubmit={formSubmit} className="login-card" elevation={18}>
        <BrandLogo size={58} borderRadius={14} />
        <Typography variant="overline" color="primary">MSP asset intelligence</Typography>
        <Typography variant="h3" component="h1">{brand.name}</Typography>
        <Typography color="text.secondary">{brand.welcomeMessage || 'Sign in to the workspace assigned to your account.'}</Typography>
        <Stack spacing={2} sx={{ mt: 4 }}>
          {error && <Alert severity="error">{error}</Alert>}
          {notice && <Alert severity="success">{notice}</Alert>}
          {pendingSession?.recoveryCodes ? <>
            <Alert severity="warning">Store these one-use recovery codes securely. They will not be shown again.</Alert>
            <Box component="pre" sx={{ m: 0, p: 2, borderRadius: 1, bgcolor: 'background.default', columns: 2, fontFamily: 'monospace' }}>{pendingSession.recoveryCodes.join('\n')}</Box>
            <Button type="button" variant="contained" size="large" onClick={finishEnrollment}>I saved these codes</Button>
          </> : challenge ? <>
            {challenge.mfaEnrollmentRequired && enrollment && <>
              <Typography variant="h6">Set up an authenticator</Typography>
              <Typography color="text.secondary">Scan this QR code with Microsoft Authenticator, Google Authenticator, 1Password or another TOTP app.</Typography>
              <Box component="img" src={enrollment.qrCodeDataUri} alt="Authenticator enrollment QR code" sx={{ width: 220, height: 220, alignSelf: 'center', bgcolor: 'white', p: 1, borderRadius: 1 }} />
              <TextField label="Manual setup key" value={enrollment.manualKey} slotProps={{ htmlInput: { readOnly: true } }} />
            </>}
            {!challenge.mfaEnrollmentRequired && <><Typography variant="h6">Authenticator verification</Typography><Typography color="text.secondary">Enter the current six-digit code or a recovery code.</Typography></>}
            <TextField label="Authenticator or recovery code" value={code} onChange={event => setCode(event.target.value)} autoComplete="one-time-code" autoFocus required />
            <Button type="submit" variant="contained" size="large" disabled={busy || code.length < 6}>{busy ? 'Verifying…' : challenge.mfaEnrollmentRequired ? 'Complete setup' : 'Verify and sign in'}</Button>
            <Button type="button" onClick={() => { setChallenge(null); setEnrollment(null); setCode(''); }}>Back to password</Button>
          </> : mode === 'request-reset' ? <>
            <Typography variant="h5">Reset local password</Typography>
            <Typography color="text.secondary">Enter your local account email. If it is eligible, we will send a single-use link that expires in 30 minutes.</Typography>
            <TextField label="Email address" type="email" value={username} onChange={event => setUsername(event.target.value)} autoComplete="email" required autoFocus />
            <Button type="submit" variant="contained" size="large" disabled={busy}>{busy ? 'Sending…' : 'Send reset link'}</Button>
            <Button type="button" onClick={() => returnToLogin()}>Back to sign in</Button>
          </> : mode === 'reset-password' ? <>
            <Typography variant="h5">Choose a new password</Typography>
            {resetValid === null && <Alert severity="info">Checking this reset link…</Alert>}
            {resetValid === false && <Alert severity="error">This reset link is invalid or has expired. Request a new link.</Alert>}
            <TextField label="New password" type="password" value={newPassword} onChange={event => setNewPassword(event.target.value)} autoComplete="new-password" required slotProps={{ htmlInput: { minLength: 12 } }} disabled={!resetValid} />
            <TextField label="Confirm new password" type="password" value={confirmPassword} onChange={event => setConfirmPassword(event.target.value)} autoComplete="new-password" required disabled={!resetValid} />
            <Box component="label" sx={{ display: 'flex', gap: 1, alignItems: 'center', cursor: 'pointer' }}>
              <Box component="input" type="checkbox" checked={revokeApiTokens} onChange={event => setRevokeApiTokens(event.target.checked)} sx={{ width: 18, height: 18, accentColor: 'primary.main' }} />
              <Typography variant="body2">Also revoke my personal API tokens</Typography>
            </Box>
            <Alert severity="info">Completing the reset signs out every active browser session. MFA remains enabled.</Alert>
            <Button type="submit" variant="contained" size="large" disabled={busy || !resetValid || newPassword.length < 12 || newPassword !== confirmPassword}>{busy ? 'Updating…' : 'Update password'}</Button>
            <Button type="button" onClick={() => returnToLogin()}>Back to sign in</Button>
          </> : <>
            {configuration?.external && <Button type="button" variant="contained" size="large" onClick={() => window.location.assign(configuration.externalLoginUrl)}>Sign in with Microsoft</Button>}
            {configuration?.localLoginEnabled && <>
              {configuration.external && <Typography variant="caption" sx={{ color: 'text.secondary', textAlign: 'center' }}>Break-glass administrator</Typography>}
              <TextField label="Email address" value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" required />
              <TextField label="Password" type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" required />
              <Button type="submit" variant={configuration.external ? 'outlined' : 'contained'} size="large" disabled={busy}>{busy ? 'Signing in…' : 'Open CMDB'}</Button>
              {configuration.passwordResetAvailable && <Button type="button" onClick={() => { setMode('request-reset'); setError(''); setNotice(''); }}>Forgot password?</Button>}
            </>}
          </>}
        </Stack>
        {!challenge && !pendingSession && mode === 'login' && configuration?.mode === 'local' && <Typography variant="caption" sx={{ color: 'text.secondary', mt: 3, display: 'block' }}>Local development account: admin@example.com / ChangeMe!</Typography>}
      </Paper>
    </Box>
  );
}
