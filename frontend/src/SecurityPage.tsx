import { useEffect, useState, type FormEvent } from 'react';
import SecurityOutlined from '@mui/icons-material/SecurityOutlined';
import {
  Alert, Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent,
  DialogTitle, Grid, Stack, TextField, Typography,
} from '@mui/material';
import { Title } from 'react-admin';
import { apiFetch, setSession } from './session';
import type { MfaEnrollment } from './authProvider';

type MfaStatus = {
  available: boolean;
  authSource: 'local' | 'entra' | 'none';
  required: boolean;
  enabled: boolean;
  recoveryCodesRemaining: number;
};

export function SecurityPage() {
  const [status, setStatus] = useState<MfaStatus | null>(null);
  const [enrollment, setEnrollment] = useState<MfaEnrollment | null>(null);
  const [code, setCode] = useState('');
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [disableOpen, setDisableOpen] = useState(false);
  const [password, setPassword] = useState('');
  const [disableCode, setDisableCode] = useState('');

  const load = async () => setStatus(await apiFetch<MfaStatus>('/api/me/mfa'));
  useEffect(() => { void load().catch(reason => setError(reason instanceof Error ? reason.message : 'Security settings could not be loaded')); }, []);

  async function startEnrollment() {
    setBusy(true); setError(''); setNotice(''); setRecoveryCodes([]);
    try { setEnrollment(await apiFetch<MfaEnrollment>('/api/me/mfa/enrollment', { method: 'POST' })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Authenticator setup could not be started'); }
    finally { setBusy(false); }
  }

  async function confirmEnrollment(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const result = await apiFetch<{ enabled: boolean; recoveryCodes: string[] }>('/api/me/mfa/confirm', { method: 'POST', body: JSON.stringify({ code }) });
      setRecoveryCodes(result.recoveryCodes); setEnrollment(null); setCode('');
      setNotice('Authenticator enabled. Save the recovery codes before leaving this page.');
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Authenticator code was not accepted'); }
    finally { setBusy(false); }
  }

  async function regenerateCodes(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const result = await apiFetch<{ recoveryCodes: string[] }>('/api/me/mfa/recovery-codes', { method: 'POST', body: JSON.stringify({ code }) });
      setRecoveryCodes(result.recoveryCodes); setCode('');
      setNotice('Previous recovery codes were revoked. Store this replacement set securely.');
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Recovery codes could not be regenerated'); }
    finally { setBusy(false); }
  }

  async function disableMfa(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      await apiFetch('/api/me/mfa/disable', { method: 'POST', body: JSON.stringify({ password, code: disableCode }) });
      setSession(null); window.location.assign('/#/login');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'MFA could not be disabled'); }
    finally { setBusy(false); }
  }

  return <Box>
    <Title title="My security" />
    <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'center' }, mb: 3 }}>
      <Box><Typography variant="overline" color="primary">Account protection</Typography><Typography variant="h3">My security</Typography><Typography color="text.secondary">Manage the second factor and recovery path for your local CMDB account.</Typography></Box>
      {status && <Chip icon={<SecurityOutlined />} label={status.enabled ? 'Authenticator enabled' : status.required ? 'Setup required' : 'Optional'} color={status.enabled ? 'success' : status.required ? 'warning' : 'default'} />}
    </Stack>
    {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
    {notice && <Alert severity="success" sx={{ mb: 2 }}>{notice}</Alert>}
    {recoveryCodes.length > 0 && <Alert severity="warning" sx={{ mb: 3 }}><Typography sx={{ fontWeight: 750 }}>Copy these one-use recovery codes now. They will not be shown again.</Typography><Box component="pre" sx={{ columns: { sm: 2 }, whiteSpace: 'pre-wrap', fontFamily: 'monospace' }}>{recoveryCodes.join('\n')}</Box></Alert>}
    {status?.authSource === 'entra' ? <Alert severity="info">Your sign-in and MFA methods are managed by Microsoft Entra ID and its Conditional Access policies.</Alert> : <Grid container spacing={3}>
      <Grid size={{ xs: 12, lg: 7 }}><Card><CardContent><Stack spacing={2}>
        <Typography variant="h5">Authenticator app</Typography>
        <Typography color="text.secondary">Use any RFC 6238-compatible app, including Microsoft Authenticator, Google Authenticator, 1Password or Bitwarden.</Typography>
        {!status?.available && <Alert severity="warning">The server encryption key has not been configured. An administrator must set MFA_ENCRYPTION_KEY before enrollment.</Alert>}
        {!status?.enabled && !enrollment && <Button variant="contained" onClick={() => void startEnrollment()} disabled={busy || !status?.available}>Set up authenticator</Button>}
        {enrollment && <Stack component="form" spacing={2} onSubmit={confirmEnrollment}>
          <Alert severity="info">Scan the QR code, then enter the current six-digit code to prove the authenticator was saved correctly.</Alert>
          <Box component="img" src={enrollment.qrCodeDataUri} alt="Authenticator enrollment QR code" sx={{ width: 240, height: 240, bgcolor: 'white', p: 1, borderRadius: 1, alignSelf: 'center' }} />
          <TextField label="Manual setup key" value={enrollment.manualKey} slotProps={{ htmlInput: { readOnly: true } }} />
          <TextField label="Current authenticator code" value={code} onChange={event => setCode(event.target.value)} autoComplete="one-time-code" required />
          <Stack direction="row" spacing={1}><Button onClick={() => { setEnrollment(null); setCode(''); }}>Cancel</Button><Button type="submit" variant="contained" disabled={busy || code.length < 6}>Verify and enable</Button></Stack>
        </Stack>}
        {status?.enabled && <><Alert severity="success">TOTP is active. {status.recoveryCodesRemaining} recovery code{status.recoveryCodesRemaining === 1 ? '' : 's'} remain.</Alert><Button color="error" variant="outlined" onClick={() => setDisableOpen(true)} disabled={status.required}>Disable authenticator</Button>{status.required && <Typography variant="caption" color="text.secondary">Your account or MSP policy requires MFA, so it cannot be disabled here.</Typography>}</>}
      </Stack></CardContent></Card></Grid>
      <Grid size={{ xs: 12, lg: 5 }}><Card><CardContent><Stack component="form" spacing={2} onSubmit={regenerateCodes}>
        <Typography variant="h5">Recovery codes</Typography><Typography color="text.secondary">Generate a replacement set after verifying your current authenticator. Every previous code is revoked immediately.</Typography>
        <TextField label="Current authenticator or recovery code" value={code} onChange={event => setCode(event.target.value)} disabled={!status?.enabled} />
        <Button type="submit" variant="outlined" disabled={busy || !status?.enabled || code.length < 6}>Replace recovery codes</Button>
      </Stack></CardContent></Card></Grid>
    </Grid>}
    <Dialog open={disableOpen} onClose={() => setDisableOpen(false)} fullWidth maxWidth="xs"><Stack component="form" onSubmit={disableMfa}><DialogTitle>Disable authenticator</DialogTitle><DialogContent><Stack spacing={2} sx={{ pt: 1 }}><Alert severity="warning">This lowers account protection and signs out every active browser session.</Alert><TextField label="Current password" type="password" value={password} onChange={event => setPassword(event.target.value)} required /><TextField label="Authenticator or recovery code" value={disableCode} onChange={event => setDisableCode(event.target.value)} required /></Stack></DialogContent><DialogActions><Button onClick={() => setDisableOpen(false)}>Cancel</Button><Button type="submit" color="error" variant="contained" disabled={busy}>Disable and sign out</Button></DialogActions></Stack></Dialog>
  </Box>;
}
