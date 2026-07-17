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
import { setSession } from './session';

export function LoginPage() {
  const login = useLogin();
  const { brand } = useMspBranding();
  const [username, setUsername] = useState('admin@example.com');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [configuration, setConfiguration] = useState<AuthConfiguration | null>(null);
  const [challenge, setChallenge] = useState<MfaChallenge | null>(null);
  const [enrollment, setEnrollment] = useState<MfaEnrollment | null>(null);
  const [code, setCode] = useState('');
  const [pendingSession, setPendingSession] = useState<MfaLoginResult | null>(null);

  useEffect(() => {
    getAuthConfiguration().then(setConfiguration).catch(reason => setError(reason instanceof Error ? reason.message : 'Unable to load sign-in configuration'));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
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

  return (
    <Box className="login-page">
      <Paper component="form" onSubmit={challenge ? submitMfa : submit} className="login-card" elevation={18}>
        <BrandLogo size={58} borderRadius={14} />
        <Typography variant="overline" color="primary">MSP asset intelligence</Typography>
        <Typography variant="h3" component="h1">{brand.name}</Typography>
        <Typography sx={{
          color: "text.secondary"
        }}>{brand.welcomeMessage || 'Sign in to the workspace assigned to your account.'}</Typography>
        <Stack spacing={2} sx={{ mt: 4 }}>
          {error && <Alert severity="error">{error}</Alert>}
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
          </> : <>
          {configuration?.external && <Button type="button" variant="contained" size="large" onClick={() => window.location.assign(configuration.externalLoginUrl)}>Sign in with Microsoft</Button>}
          {configuration?.localLoginEnabled && <>
            {configuration.external && <Typography
              variant="caption"
              sx={{
                color: "text.secondary",
                textAlign: "center"
              }}>Break-glass administrator</Typography>}
            <TextField label="Email address" value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" required />
            <TextField label="Password" type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" required />
            <Button type="submit" variant={configuration.external ? 'outlined' : 'contained'} size="large" disabled={busy}>{busy ? 'Signing in…' : 'Open CMDB'}</Button>
          </>}
          </>}
        </Stack>
        {!challenge && !pendingSession && configuration?.mode === 'local' && <Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            mt: 3,
            display: 'block'
          }}>Local development account: admin@example.com / ChangeMe!</Typography>}
      </Paper>
    </Box>
  );
}
