import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import CloudOutlined from '@mui/icons-material/CloudOutlined';
import DownloadOutlined from '@mui/icons-material/DownloadOutlined';
import EmailOutlined from '@mui/icons-material/EmailOutlined';
import KeyOutlined from '@mui/icons-material/KeyOutlined';
import OpenInNewOutlined from '@mui/icons-material/OpenInNewOutlined';
import ReplayOutlined from '@mui/icons-material/ReplayOutlined';
import SendOutlined from '@mui/icons-material/SendOutlined';
import {
  Alert, Box, Button, Card, CardContent, Checkbox, Chip, CircularProgress, Divider, FormControl,
  FormControlLabel, Grid, InputLabel, List, ListItem, ListItemIcon, ListItemText, MenuItem, Paper,
  Radio, RadioGroup, Select, Stack, Step, StepLabel, Stepper, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { Title } from 'react-admin';
import { Navigate } from 'react-router-dom';
import { apiDownload, apiFetch, getSession } from './session';
import { useWorkspace } from './workspace';

type Notice = { severity: 'success' | 'error' | 'info' | 'warning'; message: string } | null;
type AuthMode = 'managed_identity' | 'client_secret' | 'certificate';
type EmailConnection = {
  id: string;
  enabled: boolean;
  authMode: AuthMode;
  tenantId: string;
  clientId: string;
  servicePrincipalObjectId: string;
  managedIdentityClientId: string;
  senderAddress: string;
  senderName: string;
  replyTo: string;
  status: string;
  lastTestAt?: string | null;
  lastError?: string;
  revision: number;
  hasClientSecret: boolean;
  certificateConfigured: boolean;
  deploymentHints?: {
    managedIdentityAvailable: boolean;
    certificateConfigured: boolean;
    secretStorageConfigured: boolean;
  };
};
type EmailOutboxItem = {
  id: string;
  to: string[];
  subject: string;
  status: string;
  attempts: number;
  createdAt: string;
};

const steps = ['Choose sign-in', 'Sender mailbox', 'Identity details', 'Review and save', 'Run setup', 'Verify'];
const entraApplicationsUrl = 'https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade';
const enterpriseApplicationsUrl = 'https://portal.azure.com/#view/Microsoft_AAD_IAM/StartboardApplicationsMenuBlade/~/AppAppsPreview/menuId~/null';
const sharedMailboxesUrl = 'https://admin.microsoft.com/Adminportal/Home#/SharedMailbox';
const azurePortalUrl = 'https://portal.azure.com/';

function validEmail(value: string) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

function RootAdminGuard({ children }: { children: React.ReactNode }) {
  const workspace = useWorkspace();
  const role = getSession()?.user.role;
  if (!workspace.isRoot) return <Navigate to="/" replace />;
  if (role !== 'platform_admin') return <Alert severity="error">This page requires platform administrator access.</Alert>;
  return <>{children}</>;
}

function PageHeading({ connection, restart }: { connection: EmailConnection | null; restart: () => void }) {
  return <Box sx={{ mb: 3, display: 'flex', gap: 2, justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap' }}>
    <Box><Typography variant="overline" color="primary">Operations</Typography><Typography variant="h3">Microsoft 365 email setup</Typography><Typography sx={{ color: 'text.secondary', mt: 0.75, maxWidth: 820 }}>A guided setup for a least-privilege shared sender mailbox using Azure managed identity, a certificate, or a client secret.</Typography></Box>
    <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
      {connection && <Chip label={connection.status.replaceAll('_', ' ')} color={connection.status === 'verified' ? 'success' : connection.status === 'error' ? 'error' : 'warning'} />}
      <Button size="small" startIcon={<ReplayOutlined />} onClick={restart}>Restart wizard</Button>
    </Stack>
  </Box>;
}

function ChoiceCard({ selected, icon, title, copy, recommended, onClick }: { selected: boolean; icon: React.ReactNode; title: string; copy: string; recommended?: boolean; onClick: () => void }) {
  return <Paper variant="outlined" role="radio" aria-checked={selected} aria-label={title} tabIndex={0} onClick={onClick} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onClick(); } }} sx={{ p: 2.5, height: '100%', cursor: 'pointer', borderColor: selected ? 'primary.main' : undefined, bgcolor: selected ? 'action.selected' : undefined }}>
    <Stack direction="row" spacing={1.5} sx={{ alignItems: 'flex-start' }}><Radio checked={selected} tabIndex={-1} aria-hidden /><Box sx={{ flex: 1 }}>{icon}<Stack direction="row" spacing={1} sx={{ mt: 1, alignItems: 'center', flexWrap: 'wrap' }}><Typography variant="h6">{title}</Typography>{recommended && <Chip size="small" color="success" label="Recommended" />}</Stack><Typography color="text.secondary" variant="body2" sx={{ mt: 0.75 }}>{copy}</Typography></Box></Stack>
  </Paper>;
}

export function EmailPage() {
  const [connection, setConnection] = useState<EmailConnection | null>(null);
  const [activeStep, setActiveStep] = useState(0);
  const [mailboxMode, setMailboxMode] = useState<'create' | 'existing'>('create');
  const [managedIdentityType, setManagedIdentityType] = useState<'system' | 'user'>('system');
  const [clientSecret, setClientSecret] = useState('');
  const [recipient, setRecipient] = useState(getSession()?.user.email || '');
  const [outbox, setOutbox] = useState<EmailOutboxItem[]>([]);
  const [objectIdConfirmed, setObjectIdConfirmed] = useState(false);
  const [scriptConfirmed, setScriptConfirmed] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState('');

  const load = async (positionWizard = true) => {
    try {
      const [configured, messages] = await Promise.all([
        apiFetch<EmailConnection>('/api/email/config'),
        apiFetch<EmailOutboxItem[]>('/api/email/outbox?limit=50'),
      ]);
      setConnection(configured);
      setManagedIdentityType(configured.managedIdentityClientId ? 'user' : 'system');
      setOutbox(messages);
      if (positionWizard) {
        if (configured.status === 'verified') setActiveStep(5);
        else if (configured.status === 'configured') setActiveStep(4);
      }
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Email configuration could not be loaded.' });
    }
  };
  useEffect(() => { void load(); }, []);

  const update = (key: keyof EmailConnection, value: string | boolean) => setConnection(current => current ? { ...current, [key]: value } : current);
  const stepError = useMemo(() => {
    if (!connection) return 'Configuration is still loading.';
    if (activeStep === 1 && !validEmail(connection.senderAddress)) return 'Enter a valid sender mailbox address.';
    if (activeStep === 2) {
      if (!connection.tenantId.trim()) return 'Enter the Microsoft Entra tenant ID.';
      if (!connection.clientId.trim()) return 'Enter the application or managed identity client ID.';
      if (!connection.servicePrincipalObjectId.trim()) return 'Enter the enterprise application object ID.';
      if (!objectIdConfirmed) return 'Confirm that the Object ID came from Enterprise applications.';
      if (connection.authMode === 'client_secret' && !connection.hasClientSecret && !clientSecret) return 'Enter the new application client secret.';
    }
    if (activeStep === 4 && !objectIdConfirmed) return 'Review and confirm the Enterprise applications Object ID before generating another script.';
    if (activeStep === 4 && !scriptConfirmed) return 'Confirm that the administrator script completed before continuing.';
    if (activeStep === 5 && !validEmail(recipient)) return 'Enter a valid test recipient.';
    return '';
  }, [activeStep, clientSecret, connection, objectIdConfirmed, recipient, scriptConfirmed]);

  function chooseAuth(authMode: AuthMode) {
    setConnection(current => current ? { ...current, authMode, enabled: true } : current);
    setNotice(null);
  }

  function changeIdentityType(kind: 'system' | 'user') {
    setManagedIdentityType(kind);
    if (kind === 'system') update('managedIdentityClientId', '');
    else if (connection?.clientId) update('managedIdentityClientId', connection.clientId);
  }

  function changeClientId(value: string) {
    setConnection(current => current ? {
      ...current,
      clientId: value,
      managedIdentityClientId: current.authMode === 'managed_identity' && managedIdentityType === 'user' ? value : current.managedIdentityClientId,
    } : current);
  }

  function changeServicePrincipalObjectId(value: string) {
    setObjectIdConfirmed(false);
    update('servicePrincipalObjectId', value);
  }

  async function save() {
    if (!connection) return false;
    setBusy('save'); setNotice(null);
    try {
      const stored = await apiFetch<EmailConnection>('/api/email/config', {
        method: 'PUT',
        body: JSON.stringify({
          enabled: connection.enabled,
          authMode: connection.authMode,
          tenantId: connection.tenantId,
          clientId: connection.clientId,
          servicePrincipalObjectId: connection.servicePrincipalObjectId,
          managedIdentityClientId: connection.managedIdentityClientId,
          senderAddress: connection.senderAddress,
          senderName: connection.senderName,
          replyTo: connection.replyTo,
          clientSecret,
          expectedRevision: connection.revision,
        }),
      });
      setConnection(stored); setClientSecret('');
      setNotice({ severity: 'success', message: 'Connection settings saved. The next step generates the tenant administrator package.' });
      return true;
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Email settings could not be saved.' });
      return false;
    } finally { setBusy(''); }
  }

  async function next() {
    if (stepError) { setNotice({ severity: 'warning', message: stepError }); return; }
    if (activeStep === 3 && !(await save())) return;
    setNotice(null);
    setActiveStep(step => Math.min(step + 1, steps.length - 1));
  }

  async function downloadSetupScript() {
    if (!connection) return;
    setBusy('script'); setNotice(null);
    try {
      const result = await apiDownload('/api/email/setup-script', {
        method: 'POST',
        body: JSON.stringify({
          authMode: connection.authMode,
          tenantId: connection.tenantId,
          clientId: connection.clientId,
          servicePrincipalObjectId: connection.servicePrincipalObjectId,
          senderAddress: connection.senderAddress,
          senderName: connection.senderName,
          createSharedMailbox: mailboxMode === 'create',
        }),
      });
      const url = URL.createObjectURL(result.blob);
      const link = window.document.createElement('a'); link.href = url; link.download = result.filename; link.click(); URL.revokeObjectURL(url);
      setNotice({ severity: 'info', message: 'Setup package downloaded. Review it, run it as an Exchange administrator, and confirm the result below.' });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Setup script could not be generated.' });
    } finally { setBusy(''); }
  }

  async function sendTest() {
    setBusy('test'); setNotice({ severity: 'info', message: 'Submitting a test message to Microsoft Graph…' });
    try {
      await apiFetch('/api/email/test', { method: 'POST', body: JSON.stringify({ recipient }) });
      setNotice({ severity: 'success', message: 'Microsoft Graph accepted the message. Exchange policy and transport still determine final delivery.' });
      await load(false);
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The test message failed.' });
      await load(false);
    } finally { setBusy(''); }
  }

  async function retry(item: EmailOutboxItem) {
    setBusy(item.id); setNotice(null);
    try {
      const action = item.status === 'dead_letter' ? 'requeue' : 'retry';
      await apiFetch(`/api/email/outbox/${item.id}/${action}`, { method: 'POST' });
      setNotice({
        severity: 'success',
        message: action === 'requeue'
          ? 'Message returned to the delivery queue. Send it from the notification center when ready.'
          : 'Microsoft Graph accepted the retried message.',
      });
      await load(false);
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Retry failed.' });
      await load(false);
    } finally { setBusy(''); }
  }

  function restart() {
    setActiveStep(0); setScriptConfirmed(false); setNotice(null);
  }

  function stepContent() {
    if (!connection) return <Card><CardContent><Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}><CircularProgress size={22} /><Typography>Loading configuration…</Typography></Stack></CardContent></Card>;
    if (activeStep === 0) return <Grid container spacing={2}>
      <Grid size={{ xs: 12, md: 4 }}><ChoiceCard selected={connection.authMode === 'managed_identity'} icon={<CloudOutlined color="primary" />} title="Azure managed identity" copy="No stored credentials. Use a system- or user-assigned identity on Azure Container Apps or another supported Azure host." recommended onClick={() => chooseAuth('managed_identity')} /></Grid>
      <Grid size={{ xs: 12, md: 4 }}><ChoiceCard selected={connection.authMode === 'certificate'} icon={<KeyOutlined color="primary" />} title="Application certificate" copy="Best portable option for Docker. The public certificate goes to Entra; the private PFX remains a mounted container secret." recommended onClick={() => chooseAuth('certificate')} /></Grid>
      <Grid size={{ xs: 12, md: 4 }}><ChoiceCard selected={connection.authMode === 'client_secret'} icon={<KeyOutlined color="warning" />} title="Client secret" copy="Fastest for evaluation. Use a short expiry and move to a certificate or managed identity for production." onClick={() => chooseAuth('client_secret')} /></Grid>
      <Grid size={{ xs: 12 }}><Alert severity={connection.authMode === 'managed_identity' && !connection.deploymentHints?.managedIdentityAvailable ? 'info' : 'success'}>{connection.authMode === 'managed_identity' ? (connection.deploymentHints?.managedIdentityAvailable ? 'This host exposes a managed identity endpoint.' : 'The endpoint is not visible in this development container. It will be checked again after deployment to Azure.') : connection.authMode === 'certificate' ? (connection.certificateConfigured ? 'A certificate path is configured on this host.' : 'The wizard will show the required Docker secret mounts before activation.') : (connection.deploymentHints?.secretStorageConfigured ? 'Encrypted application-secret storage is configured.' : 'Configure MFA_ENCRYPTION_KEY before saving a client secret.')}</Alert></Grid>
    </Grid>;

    if (activeStep === 1) return <Grid container spacing={3}>
      <Grid size={{ xs: 12, lg: 7 }}><Card><CardContent><Stack spacing={2}><Typography variant="h5">Choose the sender mailbox</Typography><FormControl fullWidth><InputLabel>Mailbox setup</InputLabel><Select label="Mailbox setup" value={mailboxMode} onChange={event => setMailboxMode(event.target.value as 'create' | 'existing')}><MenuItem value="create">Create a dedicated shared mailbox with the script</MenuItem><MenuItem value="existing">Use an existing shared or service mailbox</MenuItem></Select></FormControl><TextField label="Sender mailbox" type="email" value={connection.senderAddress} onChange={event => update('senderAddress', event.target.value)} placeholder="cmdb@contoso.com" autoFocus /><TextField label="Sender display name" value={connection.senderName} onChange={event => update('senderName', event.target.value)} placeholder="Contoso CMDB" /><TextField label="Reply-to address (optional)" type="email" value={connection.replyTo} onChange={event => update('replyTo', event.target.value)} placeholder="support@contoso.com" /></Stack></CardContent></Card></Grid>
      <Grid size={{ xs: 12, lg: 5 }}><Card><CardContent><Typography variant="h5">Administrator guidance</Typography><List dense><ListItem><ListItemIcon><CheckCircleOutlined color="success" /></ListItemIcon><ListItemText primary={mailboxMode === 'create' ? 'The generated script creates the shared mailbox' : 'Confirm the existing mailbox address'} secondary="A dedicated shared mailbox keeps the CMDB sender separate from human accounts." /></ListItem><ListItem><ListItemIcon><CheckCircleOutlined color="success" /></ListItemIcon><ListItemText primary="No mailbox password is used" secondary="Microsoft Graph authenticates the application identity, not a mailbox user." /></ListItem></List><Button href={sharedMailboxesUrl} target="_blank" rel="noreferrer" endIcon={<OpenInNewOutlined />}>Open shared mailboxes</Button></CardContent></Card></Grid>
    </Grid>;

    if (activeStep === 2) return <Grid container spacing={3}>
      <Grid size={{ xs: 12, lg: 7 }}><Card><CardContent><Stack spacing={2}><Typography variant="h5">{connection.authMode === 'managed_identity' ? 'Managed identity details' : 'Enterprise application details'}</Typography>
        {connection.authMode === 'managed_identity' && <><RadioGroup row value={managedIdentityType} onChange={event => changeIdentityType(event.target.value as 'system' | 'user')}><FormControlLabel value="system" control={<Radio />} label="System-assigned" /><FormControlLabel value="user" control={<Radio />} label="User-assigned" /></RadioGroup><Alert severity="info">Enable the identity on the Azure host first. Then open its enterprise application to copy the client ID and object/principal ID. No app secret is required.</Alert></>}
        {connection.authMode !== 'managed_identity' && <Alert severity="info">Create a single-tenant app registration. Creating it also creates the enterprise application used by Exchange.</Alert>}
        <TextField label="Microsoft Entra tenant ID" value={connection.tenantId} onChange={event => update('tenantId', event.target.value)} placeholder="00000000-0000-0000-0000-000000000000" />
        <TextField label={connection.authMode === 'managed_identity' ? 'Managed identity application (client) ID' : 'Application (client) ID'} value={connection.clientId} onChange={event => changeClientId(event.target.value)} placeholder="00000000-0000-0000-0000-000000000000" helperText={connection.authMode === 'managed_identity' && managedIdentityType === 'user' ? 'This is also used by the container to select the user-assigned identity.' : undefined} />
        <TextField label="Enterprise application (service principal) Object ID" value={connection.servicePrincipalObjectId} onChange={event => changeServicePrincipalObjectId(event.target.value)} placeholder="00000000-0000-0000-0000-000000000000" helperText="Entra ID > Enterprise applications > your app > Object ID. Never use the Object ID shown under App registrations." />
        <Alert severity="warning"><strong>Important:</strong> search Enterprise applications using the Application ID above, open that result, and copy its Object ID. The app-registration Object ID is a different object and Exchange will reject it.</Alert>
        <FormControlLabel control={<Checkbox checked={objectIdConfirmed} onChange={(_, checked) => setObjectIdConfirmed(checked)} />} label="I copied the Object ID from Enterprise applications, not App registrations" />
        {connection.authMode === 'client_secret' && <TextField type="password" label={connection.hasClientSecret ? 'Replace client secret (optional)' : 'Client secret'} value={clientSecret} onChange={event => setClientSecret(event.target.value)} helperText={connection.hasClientSecret ? 'Leave blank to retain the encrypted secret.' : 'The value is encrypted immediately and never returned.'} />}
        {connection.authMode === 'certificate' && <Alert severity={connection.certificateConfigured ? 'success' : 'warning'}>{connection.certificateConfigured ? 'The container has a certificate path configured.' : 'Upload only the public certificate to Entra. Mount the private PFX with EMAIL_CERTIFICATE_PATH and its password with EMAIL_CERTIFICATE_PASSWORD_FILE.'}</Alert>}
      </Stack></CardContent></Card></Grid>
      <Grid size={{ xs: 12, lg: 5 }}><Card><CardContent><Typography variant="h5">Where to find the IDs</Typography><List dense><ListItem><ListItemText primary="Application ID and Object ID for Exchange" secondary="Entra ID > Enterprise applications > search using the Application ID > Overview" /></ListItem><ListItem><ListItemText primary="Tenant ID" secondary="Entra ID > Overview, or the app-registration overview" /></ListItem><ListItem><ListItemText primary="Do not grant broad Graph Mail.Send" secondary="The generated Exchange assignment restricts sending to this mailbox; grants are additive." /></ListItem></List><Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}><Button href={enterpriseApplicationsUrl} target="_blank" rel="noreferrer" endIcon={<OpenInNewOutlined />}>Open enterprise applications</Button>{connection.authMode !== 'managed_identity' && <Button href={entraApplicationsUrl} target="_blank" rel="noreferrer" endIcon={<OpenInNewOutlined />}>App registrations</Button>}{connection.authMode === 'managed_identity' && <Button href={azurePortalUrl} target="_blank" rel="noreferrer" endIcon={<OpenInNewOutlined />}>Open Azure</Button>}</Stack></CardContent></Card></Grid>
    </Grid>;

    if (activeStep === 3) return <Grid container spacing={3}>
      <Grid size={{ xs: 12, lg: 7 }}><Card><CardContent><Typography variant="h5">Review configuration</Typography><List><ListItem divider><ListItemText primary="Authentication" secondary={connection.authMode.replaceAll('_', ' ')} /></ListItem><ListItem divider><ListItemText primary="Sender" secondary={`${connection.senderName || 'IPT CMDB'} <${connection.senderAddress}>`} /></ListItem><ListItem divider><ListItemText primary="Tenant ID" secondary={connection.tenantId} /></ListItem><ListItem divider><ListItemText primary="Application client ID" secondary={connection.clientId} /></ListItem><ListItem><ListItemText primary="Enterprise application object ID" secondary={connection.servicePrincipalObjectId} /></ListItem></List><Divider sx={{ my: 2 }} /><FormControlLabel control={<Checkbox checked={connection.enabled} onChange={(_, checked) => update('enabled', checked)} />} label="Enable outbound platform email after saving" /></CardContent></Card></Grid>
      <Grid size={{ xs: 12, lg: 5 }}><Stack spacing={2}><Alert severity="success">The generated package contains identifiers and mailbox settings only. It never contains the client secret, certificate private key, or PFX password.</Alert><Alert severity="info">Saving is audited. Portable database exports omit email credentials and email bodies.</Alert></Stack></Grid>
    </Grid>;

    if (activeStep === 4) return <Grid container spacing={3}>
      <Grid size={{ xs: 12, lg: 7 }}><Card><CardContent><Stack spacing={2}><Typography variant="h5">Run the administrator package</Typography><Typography color="text.secondary">The tailored PowerShell script installs no permissions in Entra. It connects to Exchange Online, optionally creates the shared mailbox, registers the service principal, scopes <code>Application Mail.Send</code> to that mailbox, and verifies the assignment.</Typography>{!objectIdConfirmed && <Alert severity="error" action={<Button color="inherit" size="small" onClick={() => setActiveStep(2)}>Review IDs</Button>}>Confirm the Object ID from Enterprise applications before downloading another script.</Alert>}<Alert severity="success"><strong>Do not add Microsoft Graph Mail.Send under API permissions.</strong> The Exchange Online <code>Application Mail.Send</code> role created by this package is the permission. An <code>InScope = True</code> result confirms that Exchange authorizes this app for the selected mailbox.</Alert><Alert severity="info">Run with PowerShell 7.2 or later as an Exchange administrator. Install the ExchangeOnlineManagement module if it is not already available.</Alert><Alert severity="warning">Review the downloaded file, then unblock only that file. Do not change the computer-wide execution policy to Unrestricted.</Alert><Paper component="pre" variant="outlined" sx={{ p: 2, m: 0, overflowX: 'auto', fontSize: '0.82rem' }}>{`cd $HOME\\Downloads\nGet-Content .\\setup-ipt-cmdb-email.ps1\nUnblock-File -LiteralPath .\\setup-ipt-cmdb-email.ps1\n.\\setup-ipt-cmdb-email.ps1`}</Paper><Button size="large" variant="contained" startIcon={<DownloadOutlined />} disabled={Boolean(busy) || !objectIdConfirmed} onClick={downloadSetupScript}>{busy === 'script' ? 'Generating…' : 'Download corrected setup package'}</Button><FormControlLabel control={<Checkbox checked={scriptConfirmed} onChange={(_, checked) => setScriptConfirmed(checked)} />} label="The script completed and reported that the sender mailbox is in scope" /></Stack></CardContent></Card></Grid>
      <Grid size={{ xs: 12, lg: 5 }}><Card><CardContent><Typography variant="h5">What the script returns</Typography><List dense><ListItem><ListItemIcon><CheckCircleOutlined color="success" /></ListItemIcon><ListItemText primary="Idempotent configuration" secondary="Existing mailbox, scope, service principal and role assignment are reused." /></ListItem><ListItem><ListItemIcon><CheckCircleOutlined color="success" /></ListItemIcon><ListItemText primary="Setup result JSON" secondary="A non-sensitive result file records the IDs, mailbox, role and scope verification." /></ListItem><ListItem><ListItemIcon><CheckCircleOutlined color="success" /></ListItemIcon><ListItemText primary="Clean disconnect" secondary="The Exchange Online session is closed even when a command fails." /></ListItem></List></CardContent></Card></Grid>
    </Grid>;

    return <Grid container spacing={3}>
      <Grid size={{ xs: 12, lg: 7 }}><Card><CardContent><Stack spacing={2}><Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}><EmailOutlined color="primary" /><Typography variant="h5">Send verification message</Typography></Stack><Typography color="text.secondary">This creates an audited outbox item and submits it once. A successful response means Microsoft Graph accepted it, not that final mailbox delivery is guaranteed.</Typography><Alert severity="info">No Graph <code>Mail.Send</code> API permission is required in Entra for this setup. Exchange validates the scoped application role created by the administrator package.</Alert><TextField type="email" label="Test recipient" value={recipient} onChange={event => setRecipient(event.target.value)} /><Button size="large" variant="contained" startIcon={<SendOutlined />} disabled={Boolean(busy) || !connection.enabled || !validEmail(recipient)} onClick={sendTest}>{busy === 'test' ? 'Sending…' : 'Send test email'}</Button>{connection.lastTestAt && <Alert severity="success">Last accepted {new Date(connection.lastTestAt).toLocaleString()}</Alert>}</Stack></CardContent></Card></Grid>
      <Grid size={{ xs: 12, lg: 5 }}><Card><CardContent><Typography variant="h5">Setup summary</Typography><List dense><ListItem><ListItemText primary="Authentication" secondary={connection.authMode.replaceAll('_', ' ')} /></ListItem><ListItem><ListItemText primary="Sender mailbox" secondary={connection.senderAddress} /></ListItem><ListItem><ListItemText primary="Connection state" secondary={connection.status.replaceAll('_', ' ')} /></ListItem></List>{connection.lastError && <Alert severity="warning">{connection.lastError}</Alert>}</CardContent></Card></Grid>
    </Grid>;
  }

  return <RootAdminGuard><Title title="Microsoft 365 email" /><PageHeading connection={connection} restart={restart} />
    {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}
    <Card sx={{ mb: 3 }}><CardContent><Stepper activeStep={activeStep} alternativeLabel sx={{ mb: 4 }}>{steps.map((label, index) => <Step key={label} completed={index < activeStep || (index === 5 && connection?.status === 'verified')}><StepLabel>{label}</StepLabel></Step>)}</Stepper>{stepContent()}<Divider sx={{ my: 3 }} /><Stack direction="row" spacing={1} sx={{ justifyContent: 'space-between' }}><Button disabled={activeStep === 0 || Boolean(busy)} onClick={() => { setNotice(null); setActiveStep(step => Math.max(0, step - 1)); }}>Back</Button>{activeStep < steps.length - 1 && <Button variant="contained" disabled={Boolean(busy)} onClick={() => void next()}>{activeStep === 3 ? (busy === 'save' ? 'Saving…' : 'Save and continue') : 'Continue'}</Button>}</Stack></CardContent></Card>
    {outbox.length > 0 && <Card><CardContent><Typography variant="h5">Recent delivery activity</Typography><Typography color="text.secondary" sx={{ mb: 2 }}>Provider submissions and sanitized failures.</Typography><TableContainer><Table size="small"><TableHead><TableRow><TableCell>Created</TableCell><TableCell>Recipient</TableCell><TableCell>Subject</TableCell><TableCell>Status</TableCell><TableCell>Attempts</TableCell><TableCell align="right">Action</TableCell></TableRow></TableHead><TableBody>{outbox.map(item => <TableRow key={item.id}><TableCell>{new Date(item.createdAt).toLocaleString()}</TableCell><TableCell>{item.to.join(', ')}</TableCell><TableCell>{item.subject}</TableCell><TableCell><Chip size="small" label={item.status.replaceAll('_', ' ')} color={item.status === 'accepted' ? 'success' : ['failed', 'dead_letter'].includes(item.status) ? 'error' : 'warning'} /></TableCell><TableCell>{item.attempts}</TableCell><TableCell align="right">{['failed', 'dead_letter'].includes(item.status) && <Button size="small" disabled={Boolean(busy)} onClick={() => void retry(item)}>{item.status === 'dead_letter' ? 'Requeue' : 'Retry'}</Button>}</TableCell></TableRow>)}</TableBody></Table></TableContainer></CardContent></Card>}
  </RootAdminGuard>;
}
