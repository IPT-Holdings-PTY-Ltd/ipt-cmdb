import AddCircleOutlineOutlined from '@mui/icons-material/AddCircleOutlineOutlined';
import BlockOutlined from '@mui/icons-material/BlockOutlined';
import CloudSyncOutlined from '@mui/icons-material/CloudSyncOutlined';
import DeleteOutlineOutlined from '@mui/icons-material/DeleteOutlineOutlined';
import ExtensionOutlined from '@mui/icons-material/ExtensionOutlined';
import HistoryOutlined from '@mui/icons-material/HistoryOutlined';
import PauseCircleOutlineOutlined from '@mui/icons-material/PauseCircleOutlineOutlined';
import PlayCircleOutlineOutlined from '@mui/icons-material/PlayCircleOutlineOutlined';
import SettingsOutlined from '@mui/icons-material/SettingsOutlined';
import WarningAmberOutlined from '@mui/icons-material/WarningAmberOutlined';
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, Grid, Paper, Stack, Tab, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, Tabs, TextField, Typography,
  type ChipProps,
} from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { Navigate, useNavigate } from 'react-router';
import { apiFetch, getSession } from './session';
import { Title } from './ui';
import type {
  ConnectWiseConnection,
  Integration,
  IntegrationLifecycleImpact,
  IntegrationPreviewStatus,
  IntegrationProviderManifest,
  NcentralConnection,
  SyncRun,
} from './types';
import { useWorkspace } from './workspace';

type DirectoryTab = 'installed' | 'available' | 'activity';

type DirectoryEntry = {
  key: string;
  name: string;
  description: string;
  installed: boolean;
  integration?: Integration;
  manifest?: IntegrationProviderManifest;
};

type LifecycleAction = 'pause' | 'resume' | 'disable' | 'reenable' | 'restore';

const providerDescriptions: Record<string, string> = {
  connectwise: 'Customer and configuration-item discovery from ConnectWise PSA, with reviewed canonical imports.',
  ncentral: 'Device inventory, operational state and monitoring evidence from N-central.',
  passportal: 'Approved owner, folder and asset metadata associations from Passportal. Secret content is never ingested.',
};

function RootIntegrationGuard({ children }: { children: React.ReactNode }) {
  const workspace = useWorkspace();
  const role = getSession()?.user.role;
  if (!workspace.isRoot) return <Navigate to="/" replace />;
  if (!['platform_admin', 'msp_operator'].includes(role || '')) {
    return <Alert severity="error">This page requires MSP access.</Alert>;
  }
  return <>{children}</>;
}

function Metric({ label, value, detail, warning = false }: { label: string; value: number; detail: string; warning?: boolean }) {
  return (
    <Paper variant="outlined" sx={{ p: 2, height: '100%' }}>
      <Typography variant="overline" color={warning ? 'warning.main' : 'text.secondary'}>{label}</Typography>
      <Typography variant="h4" sx={{ mt: 0.25 }}>{value}</Typography>
      <Typography variant="body2" color="text.secondary">{detail}</Typography>
    </Paper>
  );
}

function statusPresentation(
  entry: DirectoryEntry,
  connections: Record<string, ConnectWiseConnection | NcentralConnection | null>,
): { label: string; color: ChipProps['color'] } {
  const lifecycle = entry.integration?.lifecycleStatus || 'active';
  if (lifecycle === 'paused') return { label: 'Paused', color: 'warning' };
  if (lifecycle === 'disabled') return { label: 'Disabled', color: 'default' };
  if (lifecycle === 'removed') return { label: 'Removed', color: 'default' };
  if (entry.key === 'connectwise' || entry.key === 'ncentral') {
    const connection = connections[entry.key];
    if (connection?.connectionStatus === 'verified') return { label: 'Healthy', color: 'success' };
    if (connection?.connectionStatus === 'error') return { label: 'Connection error', color: 'error' };
    return { label: 'Review required', color: 'warning' };
  }
  if (entry.integration?.connectionStatus === 'error') return { label: 'Connection error', color: 'error' };
  return { label: entry.integration?.status || 'Configured', color: 'success' };
}

function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString() : 'No activity yet';
}

export function IntegrationsPage() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<DirectoryTab>('installed');
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [providers, setProviders] = useState<IntegrationProviderManifest[]>([]);
  const [runs, setRuns] = useState<SyncRun[]>([]);
  const [connectWise, setConnectWise] = useState<ConnectWiseConnection | null>(null);
  const [ncentral, setNcentral] = useState<NcentralConnection | null>(null);
  const [previewStatus, setPreviewStatus] = useState<IntegrationPreviewStatus | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [lifecycleEntry, setLifecycleEntry] = useState<DirectoryEntry | null>(null);
  const [lifecycleImpact, setLifecycleImpact] = useState<IntegrationLifecycleImpact | null>(null);
  const [lifecycleReason, setLifecycleReason] = useState('');
  const [removalConfirmation, setRemovalConfirmation] = useState('');
  const [lifecycleBusy, setLifecycleBusy] = useState('');
  const isAdmin = getSession()?.user.role === 'platform_admin';

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [configured, catalogue, activity, connection, ncentralConnection, workerStatus] = await Promise.all([
        apiFetch<Integration[]>('/api/integrations'),
        apiFetch<IntegrationProviderManifest[]>('/api/integration-providers'),
        apiFetch<SyncRun[]>('/api/sync-runs'),
        apiFetch<ConnectWiseConnection>('/api/integrations/connectwise/config'),
        apiFetch<NcentralConnection>('/api/integrations/ncentral/config'),
        apiFetch<IntegrationPreviewStatus>('/api/integrations/continuous-preview/status'),
      ]);
      setIntegrations(configured);
      setProviders(catalogue);
      setRuns(activity);
      setConnectWise(connection);
      setNcentral(ncentralConnection);
      setPreviewStatus(workerStatus);
    } catch (value) {
      setError(value instanceof Error ? value.message : 'The integration directory could not be loaded.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const entries = useMemo<DirectoryEntry[]>(() => integrations.map(integration => {
    const manifest = providers.find(provider => provider.key === integration.type);
    return {
      key: integration.type,
      name: manifest?.name || integration.name,
      description: manifest?.description || providerDescriptions[integration.type] || 'A governed CMDB provider integration.',
      installed: integration.lifecycleStatus !== 'removed' && (
        ['connectwise', 'ncentral'].includes(integration.type)
          ? Boolean(
            (integration.type === 'connectwise' ? connectWise?.configured : ncentral?.configured)
            || integration.enabled || ['paused', 'disabled'].includes(integration.lifecycleStatus),
          )
          : integration.enabled
      ),
      integration,
      manifest,
    };
  }), [connectWise?.configured, ncentral?.configured, integrations, providers]);
  const installed = entries.filter(entry => entry.installed);
  const available = entries.filter(entry => !entry.installed);
  const connectionMap = { connectwise: connectWise, ncentral };
  const attention = installed.filter(
    entry => statusPresentation(entry, connectionMap).color !== 'success',
  ).length;
  const removalConfirmationName = lifecycleEntry?.integration?.name || '';

  const openConfiguration = (entry: DirectoryEntry) => {
    if (entry.key === 'connectwise') navigate('/admin/integrations/connectwise');
    if (entry.key === 'ncentral') navigate('/admin/integrations/ncentral');
  };

  const openLifecycle = async (entry: DirectoryEntry) => {
    setLifecycleEntry(entry);
    setLifecycleImpact(null);
    setLifecycleReason('');
    setRemovalConfirmation('');
    setError('');
    try {
      setLifecycleImpact(await apiFetch<IntegrationLifecycleImpact>(`/api/integrations/${entry.key}/lifecycle-impact`));
    } catch (value) {
      setError(value instanceof Error ? value.message : 'Lifecycle impact could not be loaded.');
    }
  };

  const closeLifecycle = (force = false) => {
    if (lifecycleBusy && !force) return;
    setLifecycleEntry(null);
    setLifecycleImpact(null);
    setLifecycleReason('');
    setRemovalConfirmation('');
  };

  const applyLifecycle = async (action: LifecycleAction) => {
    if (!lifecycleEntry?.integration || lifecycleReason.trim().length < 4) return;
    setLifecycleBusy(action);
    setError('');
    try {
      await apiFetch(`/api/integrations/${lifecycleEntry.key}/lifecycle`, {
        method: 'POST',
        body: JSON.stringify({
          action,
          reason: lifecycleReason.trim(),
          expectedRevision: lifecycleEntry.integration.revision,
        }),
      });
      closeLifecycle(true);
      await load();
    } catch (value) {
      setError(value instanceof Error ? value.message : 'Integration lifecycle could not be changed.');
    } finally {
      setLifecycleBusy('');
    }
  };

  const removeIntegration = async () => {
    if (!lifecycleEntry?.integration || lifecycleReason.trim().length < 4) return;
    setLifecycleBusy('remove');
    setError('');
    try {
      await apiFetch(`/api/integrations/${lifecycleEntry.key}/remove`, {
        method: 'POST',
        body: JSON.stringify({
          confirmation: removalConfirmation,
          reason: lifecycleReason.trim(),
          expectedRevision: lifecycleEntry.integration.revision,
        }),
      });
      closeLifecycle(true);
      setTab('available');
      await load();
    } catch (value) {
      setError(value instanceof Error ? value.message : 'Integration could not be removed.');
    } finally {
      setLifecycleBusy('');
    }
  };

  return (
    <RootIntegrationGuard>
      <Title title="Integrations" />
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ mb: 3, justifyContent: 'space-between', alignItems: { md: 'flex-start' } }}>
        <Box>
          <Typography variant="overline" color="primary">MSP operations</Typography>
          <Typography variant="h3">Integrations</Typography>
          <Typography color="text.secondary" sx={{ mt: 0.75, maxWidth: 780 }}>
            Monitor installed provider connections, add reviewed adapters and inspect sync evidence without mixing configuration into the overview.
          </Typography>
        </Box>
        <Button variant="contained" startIcon={<AddCircleOutlineOutlined />} onClick={() => setTab('available')}>Add integration</Button>
      </Stack>

      {error ? <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert> : null}
      {previewStatus?.workerConfigured && !previewStatus.workerHealthy ? <Alert severity="warning" sx={{ mb: 2 }}>The {previewStatus.executionMode.replaceAll('_', ' ')} integration worker is configured but has no fresh heartbeat. Review the worker container or Container App logs before relying on scheduled discovery.</Alert> : null}
      {loading ? <Card><CardContent><Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}><CircularProgress size={22} /><Typography>Loading integration directory…</Typography></Stack></CardContent></Card> : <>
        <Grid container spacing={2} sx={{ mb: 3 }}>
          <Grid size={{ xs: 12, sm: 4 }}><Metric label="Installed" value={installed.length} detail="Configured provider connections" /></Grid>
          <Grid size={{ xs: 12, sm: 4 }}><Metric label="Needs attention" value={attention} detail="Connections requiring review" warning={attention > 0} /></Grid>
          <Grid size={{ xs: 12, sm: 4 }}><Metric label="Pending reconciliation" value={previewStatus?.pendingReviews || 0} detail="Provider observations awaiting a decision" warning={Boolean(previewStatus?.pendingReviews)} /></Grid>
        </Grid>

        <Card>
          <Tabs
            value={tab}
            onChange={(_, value: DirectoryTab) => setTab(value)}
            variant="scrollable"
            scrollButtons="auto"
            aria-label="Integration directory sections"
            sx={{ px: 2, borderBottom: 1, borderColor: 'divider' }}
          >
            <Tab value="installed" label={`Installed (${installed.length})`} />
            <Tab value="available" label={`Available (${available.length})`} />
            <Tab value="activity" label={`Activity (${runs.length})`} />
          </Tabs>
          <CardContent sx={{ p: { xs: 2, md: 3 } }}>
            {tab === 'installed' && <>
              <Stack direction="row" spacing={1.25} sx={{ mb: 2, alignItems: 'center' }}>
                <CloudSyncOutlined color="primary" />
                <Box><Typography variant="h5">Installed integrations</Typography><Typography variant="body2" color="text.secondary">Health and operational controls for active provider connections.</Typography></Box>
              </Stack>
              {!installed.length ? <Alert severity="info" action={<Button color="inherit" size="small" onClick={() => setTab('available')}>Browse providers</Button>}>No provider integrations have been installed.</Alert> : <Grid container spacing={2}>{installed.map(entry => {
                const state = statusPresentation(entry, connectionMap);
                const availableOperations = entry.manifest?.operations.filter(operation => operation.status === 'available') || [];
                return <Grid key={entry.key} size={{ xs: 12, xl: 6 }}><Paper variant="outlined" sx={{ p: 2.5, height: '100%' }}>
                  <Stack spacing={2} sx={{ height: '100%' }}>
                    <Stack direction="row" spacing={2} sx={{ alignItems: 'flex-start', justifyContent: 'space-between' }}>
                      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}><Box sx={{ display: 'grid', placeItems: 'center', width: 44, height: 44, borderRadius: 2, bgcolor: 'primary.main', color: 'primary.contrastText' }}><ExtensionOutlined /></Box><Box><Typography variant="h5">{entry.name}</Typography><Typography variant="caption" color="text.secondary">MSP connection</Typography></Box></Stack>
                      <Chip size="small" color={state.color} label={state.label} />
                    </Stack>
                    <Typography color="text.secondary">{entry.description}</Typography>
                    <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
                      {availableOperations.length ? availableOperations.map(operation => <Chip key={operation.key} size="small" variant="outlined" label={`${operation.label} · ${operation.direction}`} />) : <Chip size="small" variant="outlined" label="Provider capability" />}
                      {['connectwise', 'ncentral'].includes(entry.key) && previewStatus?.workerConfigured && <Chip size="small" color={previewStatus.workerHealthy ? 'success' : 'warning'} variant="outlined" label={`${previewStatus.executionMode.replaceAll('_', ' ')} worker ${previewStatus.workerHealthy ? 'healthy' : 'needs attention'}`} />}
                      {previewStatus?.providerRateLimits?.[entry.key] && <Chip size="small" color={previewStatus.providerRateLimits[entry.key]?.limited ? 'error' : 'info'} variant="outlined" label={previewStatus.providerRateLimits[entry.key]?.limited ? 'Provider throttling observed' : `Last API response ${previewStatus.providerRateLimits[entry.key]?.httpStatus || 'observed'}`} />}
                    </Stack>
                    <Grid container spacing={1.5}>
                      <Grid size={{ xs: 12, sm: 6 }}><Typography variant="caption" color="text.secondary">Last activity</Typography><Typography variant="body2">{formatDate(entry.integration?.lastSync)}</Typography></Grid>
                      <Grid size={{ xs: 12, sm: 6 }}><Typography variant="caption" color="text.secondary">Credential source</Typography><Typography variant="body2">{entry.key === 'connectwise' ? (connectWise?.credentialSource || 'Not configured').replaceAll('_', ' ') : entry.key === 'ncentral' ? (ncentral?.credentialSource || 'Not configured').replaceAll('_', ' ') : 'Installation configuration'}</Typography></Grid>
                    </Grid>
                    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 'auto' }}>
                      <Button variant="contained" startIcon={<SettingsOutlined />} onClick={() => openConfiguration(entry)} disabled={!['connectwise', 'ncentral'].includes(entry.key)}>Change configuration</Button>
                      <Button variant="outlined" startIcon={<PauseCircleOutlineOutlined />} onClick={() => void openLifecycle(entry)} disabled={!isAdmin}>Manage lifecycle</Button>
                      <Button variant="outlined" onClick={() => navigate('/admin/reconciliation')}>Open reconciliation</Button>
                    </Stack>
                  </Stack>
                </Paper></Grid>;
              })}</Grid>}
            </>}

            {tab === 'available' && <>
              <Stack direction="row" spacing={1.25} sx={{ mb: 2, alignItems: 'center' }}>
                <AddCircleOutlineOutlined color="primary" />
                <Box><Typography variant="h5">Available integrations</Typography><Typography variant="body2" color="text.secondary">Install a supported adapter or review providers on the roadmap.</Typography></Box>
              </Stack>
              {!available.length ? <Alert severity="success">Every available integration has been installed.</Alert> : <Grid container spacing={2}>{available.map(entry => {
                const removed = entry.integration?.lifecycleStatus === 'removed';
                const canInstall = ['connectwise', 'ncentral'].includes(entry.key);
                return <Grid key={entry.key} size={{ xs: 12, md: 6, xl: 4 }}><Paper variant="outlined" sx={{ p: 2.5, height: '100%' }}>
                  <Stack spacing={1.5} sx={{ height: '100%' }}>
                    <Stack direction="row" sx={{ justifyContent: 'space-between', alignItems: 'center' }}><ExtensionOutlined color="primary" /><Chip size="small" label={removed ? 'Removed' : canInstall ? 'Ready to install' : 'Planned'} color={canInstall && !removed ? 'info' : 'default'} /></Stack>
                    <Typography variant="h5">{entry.name}</Typography>
                    <Typography color="text.secondary" sx={{ flex: 1 }}>{entry.description}</Typography>
                    {entry.manifest ? <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>{entry.manifest.authentication_modes.map(mode => <Chip key={mode} size="small" variant="outlined" label={mode.replaceAll('_', ' ')} />)}</Stack> : <Typography variant="caption" color="text.secondary">The provider contract and secure setup workflow will be added before installation is enabled.</Typography>}
                    <Button fullWidth variant={canInstall ? 'contained' : 'outlined'} disabled={!canInstall} onClick={() => removed ? void openLifecycle(entry) : openConfiguration(entry)}>{removed ? 'Review reinstall options' : canInstall ? 'Install integration' : 'Coming soon'}</Button>
                  </Stack>
                </Paper></Grid>;
              })}</Grid>}
            </>}

            {tab === 'activity' && <>
              <Stack direction="row" spacing={1.25} sx={{ mb: 2, alignItems: 'center' }}>
                <HistoryOutlined color="primary" />
                <Box><Typography variant="h5">Integration activity</Typography><Typography variant="body2" color="text.secondary">Read-only adapter checks, discovery and reconciliation evidence across providers.</Typography></Box>
              </Stack>
              {!runs.length ? <Alert severity="info">No integration activity has been recorded.</Alert> : <TableContainer component={Paper} variant="outlined"><Table size="small"><TableHead><TableRow><TableCell>Provider</TableCell><TableCell>Status</TableCell><TableCell>Result</TableCell><TableCell align="right">Discovered</TableCell><TableCell>Finished</TableCell></TableRow></TableHead><TableBody>{runs.slice(0, 50).map(run => <TableRow key={run.id} hover><TableCell sx={{ textTransform: 'capitalize', fontWeight: 700 }}>{run.type.replaceAll('_', ' ')}</TableCell><TableCell><Chip size="small" label={run.status.replaceAll('_', ' ')} color={run.status === 'success' ? 'success' : run.status === 'failed' ? 'error' : 'warning'} /></TableCell><TableCell sx={{ maxWidth: 540 }}>{run.message}</TableCell><TableCell align="right">{run.discovered || 0}</TableCell><TableCell>{formatDate(run.finishedAt)}</TableCell></TableRow>)}</TableBody></Table></TableContainer>}
            </>}
          </CardContent>
        </Card>
        {attention > 0 && <Alert severity="warning" icon={<WarningAmberOutlined />} sx={{ mt: 2 }}>One or more installed connections need review. Open the integration configuration to retest credentials or finish setup.</Alert>}
      </>}
      <Dialog open={Boolean(lifecycleEntry)} onClose={() => closeLifecycle()} maxWidth="md" fullWidth>
        <DialogTitle>Manage {lifecycleEntry?.name}</DialogTitle>
        <DialogContent>
          <Stack spacing={2.5} sx={{ mt: 1 }}>
            <Alert severity={lifecycleEntry?.integration?.lifecycleStatus === 'active' ? 'success' : 'warning'}>
              Current lifecycle: <strong>{(lifecycleEntry?.integration?.lifecycleStatus || 'active').replaceAll('_', ' ')}</strong>
              {lifecycleEntry?.integration?.lifecycleReason ? ` — ${lifecycleEntry.integration.lifecycleReason}` : ''}
            </Alert>
            <Box>
              <Typography variant="h6">Retained data impact</Typography>
              <Typography variant="body2" color="text.secondary">Pausing or disabling changes provider access only. Removal erases stored credentials and editable connection configuration while retaining these records.</Typography>
            </Box>
            {!lifecycleImpact ? <Alert severity="info" icon={<CircularProgress size={18} />}>Calculating integration impact…</Alert> : <Grid container spacing={1.5}>
              {[
                ['Customer mappings', lifecycleImpact.customerMappings],
                ['CI policies', lifecycleImpact.ciPolicies],
                ['Pending reviews', lifecycleImpact.pendingReviews],
                ['Imported CIs', lifecycleImpact.importedCis],
                ['CI identity mappings', lifecycleImpact.ciMappings],
                ['Sync runs', lifecycleImpact.syncRuns],
              ].map(([label, value]) => <Grid key={String(label)} size={{ xs: 6, md: 4 }}><Paper variant="outlined" sx={{ p: 1.5 }}><Typography variant="h5">{value}</Typography><Typography variant="caption" color="text.secondary">{label}</Typography></Paper></Grid>)}
            </Grid>}
            {lifecycleImpact?.managedByEnvironment && <Alert severity="info">Credentials are supplied by container environment variables. IPT CMDB’s lifecycle switch still blocks their use, but removing the integration cannot delete those external variables.</Alert>}
            <TextField
              label="Reason"
              value={lifecycleReason}
              onChange={event => setLifecycleReason(event.target.value)}
              multiline
              minRows={2}
              required
              helperText="Required and stored in the audit trail"
            />
            {lifecycleEntry?.integration?.lifecycleStatus === 'active' && <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
              <Button variant="contained" color="warning" startIcon={<PauseCircleOutlineOutlined />} disabled={lifecycleReason.trim().length < 4 || Boolean(lifecycleBusy)} onClick={() => void applyLifecycle('pause')}>Pause provider access</Button>
              <Button variant="outlined" color="warning" startIcon={<BlockOutlined />} disabled={lifecycleReason.trim().length < 4 || Boolean(lifecycleBusy)} onClick={() => void applyLifecycle('disable')}>Disable integration</Button>
            </Stack>}
            {lifecycleEntry?.integration?.lifecycleStatus === 'paused' && <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
              <Button variant="contained" startIcon={<PlayCircleOutlineOutlined />} disabled={lifecycleReason.trim().length < 4 || Boolean(lifecycleBusy)} onClick={() => void applyLifecycle('resume')}>Resume integration</Button>
              <Button variant="outlined" color="warning" startIcon={<BlockOutlined />} disabled={lifecycleReason.trim().length < 4 || Boolean(lifecycleBusy)} onClick={() => void applyLifecycle('disable')}>Disable integration</Button>
            </Stack>}
            {lifecycleEntry?.integration?.lifecycleStatus === 'disabled' && <Button variant="contained" startIcon={<PlayCircleOutlineOutlined />} disabled={lifecycleReason.trim().length < 4 || Boolean(lifecycleBusy)} onClick={() => void applyLifecycle('reenable')} sx={{ alignSelf: 'flex-start' }}>Re-enable integration</Button>}
            {lifecycleEntry?.integration?.lifecycleStatus === 'removed' && (lifecycleImpact?.managedByEnvironment
              ? <Button variant="contained" startIcon={<PlayCircleOutlineOutlined />} disabled={lifecycleReason.trim().length < 4 || Boolean(lifecycleBusy)} onClick={() => void applyLifecycle('restore')} sx={{ alignSelf: 'flex-start' }}>Restore environment-managed integration</Button>
              : <Alert severity="info" action={<Button color="inherit" onClick={() => { closeLifecycle(true); if (lifecycleEntry) openConfiguration(lifecycleEntry); }}>Enter credentials</Button>}>Install the integration again by entering a new credential set. Historical mappings will be reused.</Alert>)}
            {lifecycleEntry?.integration?.lifecycleStatus !== 'removed' && <>
              <Divider />
              <Paper variant="outlined" sx={{ p: 2, borderColor: 'error.main' }}>
                <Stack spacing={1.5}>
                  <Box><Typography variant="h6" color="error.main">Danger zone</Typography><Typography variant="body2" color="text.secondary">Removal is credential-destructive. Canonical CIs, mappings, sync history and audit evidence are deliberately retained.</Typography></Box>
                  <TextField label={`Type “${removalConfirmationName}” to confirm`} value={removalConfirmation} onChange={event => setRemovalConfirmation(event.target.value)} />
                  <Button color="error" variant="contained" startIcon={<DeleteOutlineOutlined />} disabled={!lifecycleImpact || lifecycleReason.trim().length < 4 || removalConfirmation !== removalConfirmationName || Boolean(lifecycleBusy)} onClick={() => void removeIntegration()} sx={{ alignSelf: 'flex-start' }}>{lifecycleBusy === 'remove' ? 'Removing…' : 'Remove integration'}</Button>
                </Stack>
              </Paper>
            </>}
          </Stack>
        </DialogContent>
        <DialogActions><Button onClick={() => closeLifecycle()} disabled={Boolean(lifecycleBusy)}>Close</Button></DialogActions>
      </Dialog>
    </RootIntegrationGuard>
  );
}
