import AddBusinessOutlined from '@mui/icons-material/AddBusinessOutlined';
import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import CloudSyncOutlined from '@mui/icons-material/CloudSyncOutlined';
import LinkOutlined from '@mui/icons-material/LinkOutlined';
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Checkbox, Chip, CircularProgress,
  Dialog, DialogActions, DialogContent, DialogTitle, FormControl, FormControlLabel,
  FormHelperText, Grid, InputLabel, LinearProgress, List, ListItem, ListItemIcon,
  ListItemText, MenuItem, Paper, Select, Stack, Step, StepLabel, Stepper, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';
import { canonicalAssetTypes } from './assetCatalog';
import { PageHeading, RootGuard } from './RootAdmin';
import { ApiError, apiFetch, getSession } from './session';
import { Title } from './ui';
import type {
  Asset, Company, ConfigurationReconciliationItem, ConfigurationReconciliationPreview,
  ConnectWiseCiPolicy, IntegrationProviderManifest, IntegrationTestResult, NcentralConnection,
  NcentralDeviceOptions, NcentralDiscoveryPreview, NcentralPreviewRun, ProviderCompany,
} from './types';
import { useWorkspace } from './workspace';

type Notice = { severity: 'success' | 'error' | 'info' | 'warning'; message: string } | null;

function emptyPolicy(companyId = '', providerParentId = ''): ConnectWiseCiPolicy {
  return {
    id: '', provider: 'ncentral', companyId, providerParentId, providerFilterId: '',
    typeMode: 'all', includedTypeIds: [], typeMappings: {}, blockUnmappedTypes: false,
    enrichmentMode: 'balanced',
    statusMode: 'all', includedStatusIds: [], excludedExternalIds: [],
    syncMode: 'manual', intervalMinutes: 360, enabled: false, revision: 0,
  };
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : 'The N-central operation could not be completed.';
}

function previewRunIsActive(run: NcentralPreviewRun | null): boolean {
  return Boolean(run && (run.status === 'queued' || run.status === 'running'));
}

export function NcentralPreviewProgressPanel({
  run,
  busy,
  onCancel,
  onRetry,
}: {
  run: NcentralPreviewRun;
  busy: boolean;
  onCancel: () => void;
  onRetry: () => void;
}) {
  const active = previewRunIsActive(run);
  const percent = Math.max(0, Math.min(100, Number(run.progress.percent) || 0));
  const determinate = run.progress.total > 0 || run.status === 'success';
  const statusColor = run.status === 'success'
    ? 'success'
    : run.status === 'failed'
      ? 'error'
      : run.status === 'cancelled'
        ? 'default'
        : 'info';
  const phase = (run.phase || run.status).replaceAll('_', ' ');

  return <Paper variant="outlined" sx={{ p: 2 }}>
    <Stack spacing={1.5}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}
        sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' } }}>
        <Box aria-live="polite">
          <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
            <Typography variant="h6">N-central preview run</Typography>
            <Chip size="small" color={statusColor}
              label={run.cancelRequested && active ? 'cancelling' : run.status} />
          </Stack>
          <Typography variant="body2" color="text.secondary">
            {phase.charAt(0).toUpperCase() + phase.slice(1)} · {run.message}
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          {active && run.canCancel && <Button size="small" color="inherit" variant="outlined"
            disabled={busy || run.cancelRequested} onClick={onCancel}>
            {run.cancelRequested ? 'Cancelling…' : 'Cancel run'}
          </Button>}
          {!active && run.canRetry && <Button size="small" variant="outlined"
            disabled={busy} onClick={onRetry}>Retry run</Button>}
        </Stack>
      </Stack>
      <LinearProgress
        aria-label="N-central preview progress"
        variant={determinate ? 'determinate' : 'indeterminate'}
        value={determinate ? (run.status === 'success' ? 100 : percent) : undefined}
      />
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
        {determinate && <Chip size="small" variant="outlined" label={`${Math.round(percent)}% complete`} />}
        <Chip size="small" variant="outlined" label={`${run.progress.discovered} discovered`} />
        <Chip size="small" variant="outlined" label={`${run.progress.enriched} enriched`} />
        <Chip size="small" variant="outlined" label={`${run.progress.reviewed} reviewed`} />
      </Stack>
      {run.status === 'failed' && <Alert severity="error">
        {run.error || run.message || 'The N-central preview failed.'}
      </Alert>}
      {run.status === 'cancelled' && <Alert severity="info">
        This preview was cancelled. Its partial observations were not published to the review queue.
      </Alert>}
    </Stack>
  </Paper>;
}

export function NcentralIntegrationPage() {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const isAdmin = getSession()?.user.role === 'platform_admin';
  const [connection, setConnection] = useState<NcentralConnection | null>(null);
  const [manifest, setManifest] = useState<IntegrationProviderManifest | null>(null);
  const [organizations, setOrganizations] = useState<ProviderCompany[]>([]);
  const [token, setToken] = useState('');
  const [testResult, setTestResult] = useState<IntegrationTestResult | null>(null);
  const [discoveryPreview, setDiscoveryPreview] = useState<NcentralDiscoveryPreview | null>(null);
  const [activeStep, setActiveStep] = useState(0);
  const [mappingChoices, setMappingChoices] = useState<Record<string, string>>({});
  const [search, setSearch] = useState('');
  const [selectedOrganizationId, setSelectedOrganizationId] = useState('');
  const [options, setOptions] = useState<NcentralDeviceOptions | null>(null);
  const [policy, setPolicy] = useState<ConnectWiseCiPolicy>(emptyPolicy());
  const [preview, setPreview] = useState<ConfigurationReconciliationPreview | null>(null);
  const [previewRun, setPreviewRun] = useState<NcentralPreviewRun | null>(null);
  const [selectedDeviceIds, setSelectedDeviceIds] = useState<string[]>([]);
  const [notice, setNotice] = useState<Notice>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [createOrganization, setCreateOrganization] = useState<ProviderCompany | null>(null);
  const [newCustomerName, setNewCustomerName] = useState('');
  const [newCustomerSlug, setNewCustomerSlug] = useState('');
  const [linkItem, setLinkItem] = useState<ConfigurationReconciliationItem | null>(null);
  const [linkAssets, setLinkAssets] = useState<Asset[]>([]);
  const [linkAssetId, setLinkAssetId] = useState('');
  const optionsCache = useRef(new Map<string, NcentralDeviceOptions>());
  const optionsRequest = useRef(0);
  const latestRunRequest = useRef(0);
  const previewScope = useRef('');

  const load = useCallback(async () => {
    try {
      const [stored, providerCatalogue, discovered] = await Promise.all([
        apiFetch<NcentralConnection>('/api/integrations/ncentral/config'),
        apiFetch<IntegrationProviderManifest[]>('/api/integration-providers'),
        apiFetch<ProviderCompany[]>('/api/integrations/ncentral/organizations'),
      ]);
      setConnection(stored);
      setManifest(providerCatalogue.find(item => item.key === 'ncentral') || null);
      setOrganizations(discovered);
    } catch (value) {
      setError(errorMessage(value));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const mappedOrganizations = useMemo(
    () => organizations.filter(item => item.active && item.mappedCompanyId),
    [organizations],
  );
  const filteredOrganizations = useMemo(
    () => organizations.filter(item => (
      `${item.name} ${item.identifier} ${item.externalId} ${item.mappedCompanyName}`
        .toLowerCase().includes(search.toLowerCase())
    )),
    [organizations, search],
  );
  const selectedMapping = mappedOrganizations.find(
    item => item.externalId === selectedOrganizationId,
  );
  const selectedCompanyId = selectedMapping?.mappedCompanyId || '';
  const previewScopeKey = selectedCompanyId && selectedOrganizationId
    ? `${selectedCompanyId}:${selectedOrganizationId}`
    : '';
  previewScope.current = previewScopeKey;
  const reviewableItems = (preview?.items || []).filter(
    item => ['create', 'update', 'link'].includes(item.action),
  );
  const typesInScope = (options?.availableTypes || []).filter(
    item => policy.typeMode === 'all' || policy.includedTypeIds.includes(item.id),
  );
  const connectionReady = Boolean(
    connection?.configured
    && connection.enabled
    && connection.lifecycleStatus === 'active',
  );
  const scopedPreviewRun = previewRun
    && previewRun.companyId === selectedCompanyId
    && previewRun.providerCompanyId === selectedOrganizationId
    ? previewRun
    : null;
  const previewRunActive = previewRunIsActive(scopedPreviewRun);
  const previewRunId = scopedPreviewRun?.id || '';

  const acceptPreviewRun = useCallback((run: NcentralPreviewRun, expectedScope: string) => {
    if (!expectedScope || previewScope.current !== expectedScope) return false;
    setPreviewRun(run);
    if (run.status === 'success') {
      if (run.result) setPreview(run.result);
      setSelectedDeviceIds([]);
      setNotice({
        severity: run.result?.counts.conflict ? 'warning' : 'success',
        message: run.message,
      });
    } else if (run.status === 'failed') {
      setNotice({ severity: 'error', message: run.error || run.message });
    } else if (run.status === 'cancelled') {
      setNotice({ severity: 'info', message: run.message });
    }
    return true;
  }, []);

  useEffect(() => {
    if (!previewScopeKey) {
      setPreviewRun(null);
      return undefined;
    }
    const requestNumber = ++latestRunRequest.current;
    const controller = new AbortController();
    const query = new URLSearchParams({
      companyId: selectedCompanyId,
      providerCompanyId: selectedOrganizationId,
    });
    void apiFetch<NcentralPreviewRun | null>(
      `/api/integrations/ncentral/devices/preview-runs/latest?${query.toString()}`,
      { signal: controller.signal },
    ).then(run => {
      if (requestNumber !== latestRunRequest.current || !run) return;
      acceptPreviewRun(run, previewScopeKey);
    }).catch(value => {
      if (controller.signal.aborted || requestNumber !== latestRunRequest.current) return;
      if (value instanceof ApiError && value.status === 404) return;
      setError(errorMessage(value));
    });
    return () => controller.abort();
  }, [acceptPreviewRun, previewScopeKey, selectedCompanyId, selectedOrganizationId]);

  useEffect(() => {
    if (!previewRunId || !previewRunActive) return undefined;
    const runId = previewRunId;
    const expectedScope = previewScopeKey;
    let stopped = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;

    const poll = async () => {
      controller = new AbortController();
      let pollAgain = true;
      try {
        const run = await apiFetch<NcentralPreviewRun>(
          `/api/integrations/ncentral/devices/preview-runs/${encodeURIComponent(runId)}`,
          { signal: controller.signal },
        );
        if (stopped || !acceptPreviewRun(run, expectedScope)) return;
        pollAgain = previewRunIsActive(run);
      } catch (value) {
        if (stopped || controller.signal.aborted) return;
        setNotice({
          severity: 'warning',
          message: `Progress could not be refreshed (${errorMessage(value)}). Retrying automatically…`,
        });
      } finally {
        controller = null;
      }
      if (!stopped && pollAgain) timer = window.setTimeout(() => void poll(), 1500);
    };

    timer = window.setTimeout(() => void poll(), 250);
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
      controller?.abort();
    };
  }, [acceptPreviewRun, previewRunActive, previewRunId, previewScopeKey]);

  async function saveConnection(event: FormEvent) {
    event.preventDefault();
    if (!connection) return;
    setBusy('save'); setError(''); setNotice(null);
    try {
      const stored = await apiFetch<NcentralConnection>('/api/integrations/ncentral/config', {
        method: 'PUT',
        body: JSON.stringify({
          // Saving from the setup wizard is an explicit request to enable the
          // connection. Lifecycle pause/disable controls remain on the
          // integrations overview.
          enabled: true,
          baseUrl: connection.baseUrl,
          pageSize: connection.pageSize,
          userApiToken: token,
          expectedRevision: connection.revision,
        }),
      });
      setConnection(stored); setToken(''); setTestResult(null);
      setNotice({
        severity: 'success',
        message: 'Connection saved and enabled. The permanent token is encrypted and will not be returned.',
      });
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function testConnection() {
    setBusy('test'); setError(''); setTestResult(null);
    setNotice({ severity: 'info', message: 'Exchanging and validating a temporary access token…' });
    try {
      const result = await apiFetch<IntegrationTestResult>(
        '/api/integrations/ncentral/test', { method: 'POST' },
      );
      setTestResult(result);
      setNotice({ severity: 'success', message: result.message });
      await load();
    } catch (value) { setError(errorMessage(value)); setNotice(null); await load(); } finally {
      setBusy('');
    }
  }

  async function previewOrganizations() {
    setBusy('preview-organizations'); setError('');
    try {
      const result = await apiFetch<NcentralDiscoveryPreview>(
        '/api/integrations/ncentral/discovery-preview', { method: 'POST' },
      );
      setDiscoveryPreview(result);
      setNotice({ severity: 'success', message: result.message });
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function discoverOrganizations() {
    setBusy('discover'); setError('');
    try {
      const result = await apiFetch<{ message: string }>(
        '/api/integrations/ncentral/discover', { method: 'POST' },
      );
      setNotice({ severity: 'success', message: result.message });
      await load();
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function mapOrganization(item: ProviderCompany) {
    const companyId = mappingChoices[item.externalId]
      || item.mappedCompanyId || item.suggestedCompanyId || '';
    if (!companyId) return;
    setBusy(`map-${item.externalId}`); setError('');
    try {
      await apiFetch(
        `/api/integrations/ncentral/organizations/${encodeURIComponent(item.externalId)}/mapping`,
        { method: 'PUT', body: JSON.stringify({ companyId }) },
      );
      setNotice({
        severity: 'success',
        message: `${item.name} is explicitly mapped. Nothing changed in N-central.`,
      });
      await load();
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function unmapOrganization(item: ProviderCompany) {
    setBusy(`map-${item.externalId}`); setError('');
    try {
      await apiFetch(
        `/api/integrations/ncentral/organizations/${encodeURIComponent(item.externalId)}/mapping`,
        { method: 'DELETE' },
      );
      setNotice({ severity: 'success', message: `${item.name} was unmapped; history is retained.` });
      await load();
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  function openCreateCustomer(item: ProviderCompany) {
    const slug = (item.identifier || item.name).toLowerCase()
      .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48);
    setCreateOrganization(item); setNewCustomerName(item.name); setNewCustomerSlug(slug);
  }

  async function createCustomerAndMap(event: FormEvent) {
    event.preventDefault();
    if (!createOrganization || !newCustomerName.trim()) return;
    setBusy('create'); setError('');
    try {
      const company = await apiFetch<Company>('/api/companies', {
        method: 'POST',
        body: JSON.stringify({ name: newCustomerName.trim(), slug: newCustomerSlug.trim() }),
      });
      await apiFetch(
        `/api/integrations/ncentral/organizations/${encodeURIComponent(createOrganization.externalId)}/mapping`,
        { method: 'PUT', body: JSON.stringify({ companyId: company.id }) },
      );
      setCreateOrganization(null);
      await Promise.all([workspace.refreshCompanies(), load()]);
      setNotice({
        severity: 'success',
        message: `${company.name} was created and mapped to ${createOrganization.name}.`,
      });
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  const loadDeviceOptions = useCallback(async (force = false) => {
    if (!selectedCompanyId || !selectedOrganizationId) return;
    const scope = `${selectedCompanyId}:${selectedOrganizationId}`;
    const cached = optionsCache.current.get(scope);
    if (cached && !force) {
      setOptions(cached); setPolicy(cached.policy);
      return;
    }
    const requestNumber = ++optionsRequest.current;
    setBusy('options'); setError(''); setPreview(null);
    try {
      const result = await apiFetch<NcentralDeviceOptions>(
        '/api/integrations/ncentral/devices/options',
        {
          method: 'POST',
          body: JSON.stringify({
            companyId: selectedCompanyId,
            providerCompanyId: selectedOrganizationId,
          }),
        },
      );
      optionsCache.current.set(scope, result);
      if (requestNumber === optionsRequest.current && previewScope.current === scope) {
        setOptions(result); setPolicy(result.policy);
      }
    } catch (value) {
      if (requestNumber === optionsRequest.current) setError(errorMessage(value));
    } finally {
      if (requestNumber === optionsRequest.current) setBusy('');
    }
  }, [selectedCompanyId, selectedOrganizationId]);

  useEffect(() => {
    if (activeStep === 3 && selectedOrganizationId) void loadDeviceOptions();
  }, [activeStep, selectedOrganizationId, loadDeviceOptions]);

  function updatePolicy(changes: Partial<ConnectWiseCiPolicy>) {
    setPolicy(current => ({ ...current, ...changes }));
    latestRunRequest.current += 1;
    setPreview(null); setPreviewRun(null); setSelectedDeviceIds([]);
  }

  function updateTypeMapping(providerTypeId: string, canonicalType: string) {
    const mappings = { ...policy.typeMappings };
    if (canonicalType) mappings[providerTypeId] = canonicalType;
    else delete mappings[providerTypeId];
    updatePolicy({ typeMappings: mappings });
  }

  async function savePolicy(showNotice = true): Promise<ConnectWiseCiPolicy | null> {
    if (!isAdmin || !selectedMapping?.mappedCompanyId) return policy;
    setBusy('save-policy'); setError('');
    try {
      const stored = await apiFetch<ConnectWiseCiPolicy>(
        '/api/integrations/ncentral/devices/policy',
        {
          method: 'PUT',
          body: JSON.stringify({
            companyId: selectedMapping.mappedCompanyId,
            providerCompanyId: selectedMapping.externalId,
            providerFilterId: policy.providerFilterId || '',
            typeMode: policy.typeMode,
            includedTypeIds: policy.includedTypeIds,
            typeMappings: policy.typeMappings,
            blockUnmappedTypes: policy.blockUnmappedTypes,
            enrichmentMode: policy.enrichmentMode || 'balanced',
            statusMode: policy.statusMode,
            includedStatusIds: policy.includedStatusIds,
            excludedExternalIds: policy.excludedExternalIds,
            syncMode: policy.syncMode,
            intervalMinutes: policy.intervalMinutes,
            enabled: policy.enabled,
            expectedRevision: policy.revision,
          }),
        },
      );
      setPolicy(stored);
      if (previewScopeKey) {
        const cached = optionsCache.current.get(previewScopeKey);
        if (cached) optionsCache.current.set(previewScopeKey, { ...cached, policy: stored });
      }
      if (showNotice) setNotice({
        severity: 'success',
        message: 'Device filter, type mappings and preview schedule saved with an audit revision.',
      });
      return stored;
    } catch (value) { setError(errorMessage(value)); return null; } finally { setBusy(''); }
  }

  async function previewDevices() {
    if (!selectedCompanyId || !selectedOrganizationId || !previewScopeKey) return;
    const expectedScope = previewScopeKey;
    setError('');
    const stored = isAdmin ? await savePolicy(false) : policy;
    if (!stored || previewScope.current !== expectedScope) return;
    latestRunRequest.current += 1;
    setBusy('enqueue-preview'); setPreview(null); setPreviewRun(null); setSelectedDeviceIds([]);
    try {
      const run = await apiFetch<NcentralPreviewRun>(
        '/api/integrations/ncentral/devices/preview-runs',
        {
          method: 'POST',
          body: JSON.stringify({
            companyId: selectedCompanyId,
            providerCompanyId: selectedOrganizationId,
          }),
        },
      );
      if (acceptPreviewRun(run, expectedScope) && previewRunIsActive(run)) {
        setNotice({
          severity: 'info',
          message: 'The read-only preview is running in the background. You may leave this page and return later.',
        });
      }
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function syncNow() {
    if (!policy.id || !previewScopeKey) return;
    const expectedScope = previewScopeKey;
    latestRunRequest.current += 1;
    setBusy('enqueue-sync'); setError(''); setPreview(null); setPreviewRun(null);
    setSelectedDeviceIds([]);
    try {
      const run = await apiFetch<NcentralPreviewRun>(
        `/api/integrations/ncentral/devices/policies/${encodeURIComponent(policy.id)}/preview-runs`,
        { method: 'POST' },
      );
      if (acceptPreviewRun(run, expectedScope) && previewRunIsActive(run)) {
        setNotice({
          severity: 'info',
          message: 'The saved policy is running in the background. Progress will update here.',
        });
      }
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function cancelPreviewRun() {
    if (!scopedPreviewRun || !previewScopeKey) return;
    const expectedScope = previewScopeKey;
    setBusy('cancel-preview'); setError('');
    try {
      const run = await apiFetch<NcentralPreviewRun>(
        `/api/integrations/ncentral/devices/preview-runs/${encodeURIComponent(scopedPreviewRun.id)}/cancel`,
        { method: 'POST' },
      );
      acceptPreviewRun(run, expectedScope);
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function retryPreviewRun() {
    if (!scopedPreviewRun || !previewScopeKey) return;
    const expectedScope = previewScopeKey;
    latestRunRequest.current += 1;
    setBusy('retry-preview'); setError(''); setPreview(null); setSelectedDeviceIds([]);
    try {
      const run = await apiFetch<NcentralPreviewRun>(
        `/api/integrations/ncentral/devices/preview-runs/${encodeURIComponent(scopedPreviewRun.id)}/retry`,
        { method: 'POST' },
      );
      if (acceptPreviewRun(run, expectedScope) && previewRunIsActive(run)) {
        setNotice({ severity: 'info', message: 'The preview retry has been queued.' });
      }
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function importDevices() {
    if (!selectedMapping?.mappedCompanyId || !selectedDeviceIds.length) return;
    const resolvedIds = [...selectedDeviceIds];
    setBusy('import'); setError('');
    try {
      const result = await apiFetch<{ message: string }>(
        '/api/integrations/ncentral/devices/import',
        {
          method: 'POST',
          body: JSON.stringify({
            companyId: selectedMapping.mappedCompanyId,
            providerCompanyId: selectedMapping.externalId,
            externalIds: selectedDeviceIds,
            decisionNotes: 'Selected and approved in the N-central reconciliation wizard',
          }),
        },
      );
      setNotice({ severity: 'success', message: result.message });
      const removeResolvedItems = (current: ConfigurationReconciliationPreview | null) => {
        if (!current) return current;
        const resolved = current.items.filter(item => resolvedIds.includes(item.externalId));
        if (!resolved.length) return current;
        const counts = { ...current.counts };
        for (const item of resolved) counts[item.action] = Math.max(0, counts[item.action] - 1);
        const queueSummary = current.queueSummary
          ? {
            ...current.queueSummary,
            pending: Math.max(0, current.queueSummary.pending - resolved.length),
            resolved: current.queueSummary.resolved + resolved.length,
          }
          : undefined;
        return {
          ...current,
          counts,
          queueSummary,
          items: current.items.filter(item => !resolvedIds.includes(item.externalId)),
        };
      };
      setPreview(removeResolvedItems);
      setPreviewRun(current => current?.result
        ? { ...current, result: removeResolvedItems(current.result) }
        : current);
      setSelectedDeviceIds([]);
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function openLink(item: ConfigurationReconciliationItem) {
    if (!selectedMapping?.mappedCompanyId) return;
    setBusy('link-options'); setError('');
    try {
      const assets = await apiFetch<Asset[]>(
        `/api/assets?companyId=${encodeURIComponent(selectedMapping.mappedCompanyId)}`,
      );
      setLinkAssets(assets); setLinkItem(item); setLinkAssetId('');
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  async function linkDevice() {
    if (!selectedMapping?.mappedCompanyId || !linkItem || !linkAssetId) return;
    const linkedExternalId = linkItem.externalId;
    setBusy('link'); setError('');
    try {
      const result = await apiFetch<{ message: string }>(
        '/api/integrations/ncentral/devices/link',
        {
          method: 'POST',
          body: JSON.stringify({
            companyId: selectedMapping.mappedCompanyId,
            providerCompanyId: selectedMapping.externalId,
            externalId: linkItem.externalId,
            assetId: linkAssetId,
          }),
        },
      );
      setLinkItem(null); setNotice({ severity: 'success', message: result.message });
      setPreview(current => {
        if (!current) return current;
        const linked = current.items.find(item => item.externalId === linkedExternalId);
        if (!linked) return current;
        const counts = { ...current.counts };
        counts[linked.action] = Math.max(0, counts[linked.action] - 1);
        const queueSummary = current.queueSummary
          ? {
            ...current.queueSummary,
            pending: Math.max(0, current.queueSummary.pending - 1),
            resolved: current.queueSummary.resolved + 1,
          }
          : undefined;
        return {
          ...current,
          counts,
          queueSummary,
          items: current.items.filter(item => item.externalId !== linkedExternalId),
        };
      });
      setSelectedDeviceIds(current => current.filter(id => id !== linkedExternalId));
    } catch (value) { setError(errorMessage(value)); } finally { setBusy(''); }
  }

  const steps = ['Provider', 'Connection', 'Customers', 'Devices & reconciliation'];
  return (
    <RootGuard>
      <Title title="N-central configuration" />
      <PageHeading
        eyebrow="Integration configuration"
        title="N-central"
        copy="Configure secure read-only access, map N-central customers, and reconcile devices through durable provider identities."
        action={<Button variant="outlined" onClick={() => navigate('/admin/integrations')}>Back to integrations</Button>}
      />
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {!connection ? <Card><CardContent><CircularProgress size={24} /></CardContent></Card> : (
        <Card><CardContent>
          <Stepper activeStep={activeStep} alternativeLabel sx={{ mb: 4 }}>
            {steps.map(label => <Step key={label}><StepLabel>{label}</StepLabel></Step>)}
          </Stepper>

          {activeStep === 0 && <Stack spacing={2}>
            <Box><Typography variant="overline" color="primary">Provider adapter</Typography>
              <Typography variant="h4">{manifest?.name || 'N-central'}</Typography>
              <Typography color="text.secondary">{manifest?.description}</Typography>
            </Box>
            <Grid container spacing={2}>
              <Grid size={{ xs: 12, lg: 5 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}>
                <Typography variant="h6">Prerequisites</Typography>
                <List dense>{manifest?.prerequisites.map(item => <ListItem key={item} disableGutters>
                  <ListItemIcon sx={{ minWidth: 30 }}><CheckCircleOutlined fontSize="small" color="primary" /></ListItemIcon>
                  <ListItemText primary={item} />
                </ListItem>)}</List>
              </Paper></Grid>
              <Grid size={{ xs: 12, lg: 7 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}>
                <Typography variant="h6">Declared operations</Typography>
                <List dense>{manifest?.operations.map(operation => <ListItem key={operation.key} disableGutters
                  secondaryAction={<Chip size="small" label={operation.status} color={operation.status === 'available' ? 'success' : 'default'} />}>
                  <ListItemText primary={`${operation.label} · ${operation.direction}`} secondary={operation.description} />
                </ListItem>)}</List>
              </Paper></Grid>
            </Grid>
            <Alert severity="info">This connector never calls N-central write or delete endpoints. Customer creation, device import and identity linking change only IPT CMDB after administrator review.</Alert>
          </Stack>}

          {activeStep === 1 && <Stack spacing={2.5}>
            <Stack component="form" spacing={2} onSubmit={saveConnection}>
              <Box><Typography variant="overline" color="primary">Secure token exchange</Typography>
                <Typography variant="h5">N-central connection</Typography>
                <Typography color="text.secondary">The permanent User-API token is write-only. Temporary access tokens remain in process memory and are never stored.</Typography>
              </Box>
              {connection.managedByEnvironment && <Alert severity={connection.lastError ? 'error' : 'info'}>
                {connection.lastError || 'Connection values are managed by NCENTRAL_* container variables or a Docker secret file.'}
              </Alert>}
              {connection.lifecycleStatus !== 'active' && <Alert severity="warning"
                action={<Button color="inherit" size="small"
                  onClick={() => navigate('/admin/integrations')}>Manage lifecycle</Button>}>
                N-central is {connection.lifecycleStatus.replaceAll('_', ' ')}. Re-enable it from
                Installed integrations before testing or discovery.
              </Alert>}
              {connection.lifecycleStatus === 'active' && connection.configured && !connection.enabled
                && <Alert severity="warning">
                  The saved connection is currently disabled. Saving this form will enable it so
                  testing, discovery and preview can continue.
                </Alert>}
              <Grid container spacing={2}>
                <Grid size={{ xs: 12, md: 8 }}><TextField fullWidth required label="N-central server URL"
                  value={connection.baseUrl}
                  onChange={event => setConnection({ ...connection, baseUrl: event.target.value })}
                  placeholder="https://ncentral.example.com"
                  disabled={connection.managedByEnvironment || !isAdmin}
                /></Grid>
                <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth type="number" label="Page size"
                  value={connection.pageSize}
                  onChange={event => setConnection({ ...connection, pageSize: Number(event.target.value) })}
                  slotProps={{ htmlInput: { min: 25, max: 1000 } }}
                  disabled={connection.managedByEnvironment || !isAdmin}
                /></Grid>
                {!connection.managedByEnvironment && isAdmin && <Grid size={{ xs: 12 }}>
                  <TextField fullWidth type="password" label="N-central User-API token" value={token}
                    onChange={event => setToken(event.target.value)}
                    helperText={connection.hasCredentials ? 'Leave blank to retain the encrypted token.' : 'Required for first setup. Do not use an interactive SSO token.'}
                  />
                </Grid>}
              </Grid>
              {!connection.managedByEnvironment && isAdmin && <Button type="submit" variant="contained"
                disabled={busy === 'save' || connection.lifecycleStatus !== 'active'}
                sx={{ alignSelf: 'flex-start' }}>
                {busy === 'save'
                  ? 'Saving…'
                  : connection.enabled ? 'Save encrypted connection' : 'Enable and save connection'}
              </Button>}
            </Stack>
            <Paper variant="outlined" sx={{ p: 2 }}><Stack spacing={1.5}>
              <Typography variant="h6">Progressive connection test</Typography>
              <Button variant="outlined" disabled={!connectionReady || busy === 'test'}
                onClick={() => void testConnection()} sx={{ alignSelf: 'flex-start' }}>
                {busy === 'test' ? 'Testing…' : 'Test token and organization read'}
              </Button>
              {testResult?.stages.map(stage => <Alert key={stage.key}
                severity={stage.status === 'passed' ? 'success' : stage.status === 'failed' ? 'error' : 'info'}>
                <Typography sx={{ fontWeight: 750 }}>{stage.label}</Typography>{stage.message}
              </Alert>)}
              {!testResult && <Alert severity={connection.connectionStatus === 'verified' ? 'success' : 'info'}>
                {connection.connectionStatus === 'verified' ? 'The last connection test passed.' : 'Run the test before customer discovery.'}
              </Alert>}
            </Stack></Paper>
          </Stack>}

          {activeStep === 2 && <Stack spacing={2}>
            <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5}
              sx={{ justifyContent: 'space-between', alignItems: { md: 'center' } }}>
              <Box><Typography variant="overline" color="primary">Tenant boundaries</Typography>
                <Typography variant="h5">Customer organization mapping</Typography>
                <Typography color="text.secondary">Only CUSTOMER organization units are retained. Every CMDB customer link requires an explicit administrator decision.</Typography>
              </Box>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                <Button variant="outlined" disabled={!connectionReady || Boolean(busy)}
                  onClick={() => void previewOrganizations()}>
                  {busy === 'preview-organizations' ? 'Previewing…' : 'Preview organizations'}
                </Button>
                <Button variant="contained" startIcon={busy === 'discover' ? <CircularProgress size={16} color="inherit" /> : <CloudSyncOutlined />}
                  disabled={!connectionReady || Boolean(busy)}
                  onClick={() => void discoverOrganizations()}>
                  {busy === 'discover' ? 'Discovering…' : 'Discover for mapping'}
                </Button>
              </Stack>
            </Stack>
            {discoveryPreview && <Alert severity="success">{discoveryPreview.included} of {discoveryPreview.discovered} accessible customer organizations are in scope. The preview persisted nothing.</Alert>}
            <TextField size="small" label="Search organizations" value={search}
              onChange={event => setSearch(event.target.value)} sx={{ maxWidth: 440 }} />
            {!organizations.length ? <Alert severity="info">Run discovery to create the mapping queue.</Alert>
              : <TableContainer><Table size="small"><TableHead><TableRow>
                <TableCell>N-central customer</TableCell><TableCell>Provider identity</TableCell>
                <TableCell>CMDB customer</TableCell><TableCell align="right">Action</TableCell>
              </TableRow></TableHead><TableBody>{filteredOrganizations.map(item => {
                const selected = mappingChoices[item.externalId] || item.mappedCompanyId
                  || item.suggestedCompanyId || '';
                return <TableRow key={item.externalId}>
                  <TableCell><Typography sx={{ fontWeight: 750 }}>{item.name}</Typography>
                    <Typography variant="caption" color="text.secondary">{item.type}</Typography>
                  </TableCell>
                  <TableCell><Typography variant="body2">Org unit {item.externalId}</Typography>
                    <Typography variant="caption" color="text.secondary">{item.identifier || 'No external reference'}</Typography>
                  </TableCell>
                  <TableCell><FormControl fullWidth size="small" sx={{ minWidth: 220 }}>
                    <InputLabel>CMDB customer</InputLabel>
                    <Select label="CMDB customer" value={selected} disabled={!isAdmin}
                      onChange={event => setMappingChoices(current => ({ ...current, [item.externalId]: event.target.value }))}>
                      <MenuItem value=""><em>Select customer</em></MenuItem>
                      {workspace.companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}
                    </Select>
                  </FormControl>
                    {!item.mappedCompanyId && item.suggestedCompanyName && <Typography variant="caption" color="warning.main">
                      Suggested: {item.suggestedCompanyName} · {item.suggestionReason}
                    </Typography>}
                  </TableCell>
                  <TableCell align="right"><Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
                    {!item.mappedCompanyId && <Button size="small" startIcon={<AddBusinessOutlined />}
                      disabled={!isAdmin || Boolean(busy)} onClick={() => openCreateCustomer(item)}>Create</Button>}
                    {item.mappedCompanyId && <Button size="small" color="inherit"
                      disabled={!isAdmin || Boolean(busy)} onClick={() => void unmapOrganization(item)}>Unmap</Button>}
                    <Button size="small" variant="outlined"
                      disabled={!isAdmin || !selected || selected === item.mappedCompanyId || Boolean(busy)}
                      onClick={() => void mapOrganization(item)}>{item.mappedCompanyId ? 'Change' : 'Map'}</Button>
                  </Stack></TableCell>
                </TableRow>;
              })}</TableBody></Table></TableContainer>}
          </Stack>}

          {activeStep === 3 && <Stack spacing={2}>
            <Box><Typography variant="overline" color="primary">Reviewed inventory</Typography>
              <Typography variant="h5">Device policy and reconciliation</Typography>
              <Typography color="text.secondary">Use a native N-central device filter, then refine by class and status. The filter ID and mappings persist for manual or scheduled previews.</Typography>
            </Box>
            {!mappedOrganizations.length ? <Alert severity="warning">Map at least one N-central customer first.</Alert>
              : <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                <FormControl fullWidth sx={{ maxWidth: 620 }}><InputLabel>Mapped N-central customer</InputLabel>
                  <Select label="Mapped N-central customer" value={selectedOrganizationId}
                    disabled={previewRunActive}
                    onChange={event => {
                      const externalId = event.target.value;
                      const mapped = mappedOrganizations.find(item => item.externalId === externalId);
                      optionsRequest.current += 1; latestRunRequest.current += 1;
                      setSelectedOrganizationId(externalId); setOptions(null); setPreview(null);
                      setPreviewRun(null); setSelectedDeviceIds([]);
                      setPolicy(emptyPolicy(mapped?.mappedCompanyId || '', externalId));
                    }}>
                    {mappedOrganizations.map(item => <MenuItem key={item.externalId} value={item.externalId}>
                      {item.name} → {item.mappedCompanyName}
                    </MenuItem>)}
                  </Select>
                </FormControl>
                <Button variant="outlined"
                  disabled={!selectedOrganizationId || busy === 'options' || previewRunActive}
                  onClick={() => void loadDeviceOptions(true)}>
                  {busy === 'options' ? 'Loading…' : 'Refresh choices'}
                </Button>
              </Stack>}
            {options && <Paper variant="outlined" sx={{ p: 2 }}><Stack spacing={2}>
              <Alert severity="info">Found {options.discovered} devices before the saved CMDB filters. N-central filtering occurs server-side when a native filter is selected.</Alert>
              <Grid container spacing={2}>
                <Grid size={{ xs: 12 }}><FormControl fullWidth><InputLabel>N-central device filter</InputLabel>
                  <Select label="N-central device filter" value={policy.providerFilterId || ''}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({ providerFilterId: event.target.value })}>
                    <MenuItem value=""><em>All devices visible to this customer</em></MenuItem>
                    {options.deviceFilters.map(item => <MenuItem key={item.id} value={item.id}>
                      {item.name}{item.description ? ` — ${item.description}` : ''}
                    </MenuItem>)}
                  </Select>
                </FormControl></Grid>
                <Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth><InputLabel>Device class scope</InputLabel>
                  <Select multiple label="Device class scope" value={policy.includedTypeIds}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({
                      typeMode: (event.target.value as string[]).length ? 'selected' : 'all',
                      includedTypeIds: event.target.value as string[],
                    })}>
                    {options.availableTypes.map(item => <MenuItem key={item.id} value={item.id}>
                      <Checkbox checked={policy.includedTypeIds.includes(item.id)} />
                      <ListItemText primary={item.name} secondary={`${item.count} device(s) · ID ${item.id}`} />
                    </MenuItem>)}
                  </Select>
                </FormControl></Grid>
                <Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth><InputLabel>License/status scope</InputLabel>
                  <Select multiple label="License/status scope" value={policy.includedStatusIds}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({
                      statusMode: (event.target.value as string[]).length ? 'selected' : 'all',
                      includedStatusIds: event.target.value as string[],
                    })}>
                    {options.availableStatuses.map(item => <MenuItem key={item.id} value={item.id}>
                      <Checkbox checked={policy.includedStatusIds.includes(item.id)} />
                      <ListItemText primary={item.name} secondary={`${item.count} device(s) · ID ${item.id}`} />
                    </MenuItem>)}
                  </Select>
                </FormControl></Grid>
                <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Excluded N-central device IDs"
                  value={policy.excludedExternalIds.join(', ')}
                  disabled={previewRunActive}
                  onChange={event => updatePolicy({ excludedExternalIds: event.target.value.split(',').map(value => value.trim()).filter(Boolean) })}
                  helperText="Optional immutable device IDs, separated by commas."
                /></Grid>
                <Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth>
                  <InputLabel>Enrichment depth</InputLabel>
                  <Select label="Enrichment depth" value={policy.enrichmentMode || 'balanced'}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({
                      enrichmentMode: event.target.value as NonNullable<ConnectWiseCiPolicy['enrichmentMode']>,
                    })}>
                    <MenuItem value="fast">Fast · no device detail calls</MenuItem>
                    <MenuItem value="balanced">Balanced · enrich up to 25 devices</MenuItem>
                    <MenuItem value="full">Full · enrich up to 250 devices</MenuItem>
                  </Select>
                  <FormHelperText>
                    Read-only. Greater enrichment adds identity evidence but takes longer.
                  </FormHelperText>
                </FormControl></Grid>
                <Grid size={{ xs: 12, md: 3 }}><FormControl fullWidth><InputLabel>Sync mode</InputLabel>
                  <Select label="Sync mode" value={policy.syncMode}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({
                      syncMode: event.target.value as ConnectWiseCiPolicy['syncMode'],
                      enabled: event.target.value === 'continuous_preview' ? policy.enabled : false,
                    })}>
                    <MenuItem value="manual">Manual reviewed import</MenuItem>
                    <MenuItem value="continuous_preview">Continuous preview</MenuItem>
                  </Select>
                </FormControl></Grid>
                <Grid size={{ xs: 12, md: 3 }}><TextField fullWidth type="number" label="Interval minutes"
                  value={policy.intervalMinutes}
                  disabled={policy.syncMode === 'manual' || previewRunActive}
                  onChange={event => updatePolicy({ intervalMinutes: Number(event.target.value) })}
                  slotProps={{ htmlInput: { min: 15, max: 10080 } }}
                /></Grid>
              </Grid>
              {policy.syncMode === 'continuous_preview' && <FormControlLabel
                control={<Checkbox checked={policy.enabled}
                  disabled={previewRunActive}
                  onChange={(_, checked) => updatePolicy({ enabled: checked })} />}
                label="Enable this saved policy for the integration worker"
              />}
              <Box><Typography variant="h6">Configuration type mapping</Typography>
                <Typography variant="body2" color="text.secondary">Map N-central device classes to canonical CMDB types. Unmapped classes can be blocked from import.</Typography>
              </Box>
              <FormControlLabel control={<Checkbox checked={policy.blockUnmappedTypes}
                disabled={previewRunActive}
                onChange={(_, checked) => updatePolicy({ blockUnmappedTypes: checked })} />}
                label="Block devices whose class has no canonical mapping"
              />
              <TableContainer><Table size="small"><TableHead><TableRow>
                <TableCell>N-central device class</TableCell><TableCell align="right">Devices</TableCell>
                <TableCell>Canonical CMDB type</TableCell>
              </TableRow></TableHead><TableBody>{typesInScope.map(item => <TableRow key={item.id}>
                <TableCell><Typography sx={{ fontWeight: 750 }}>{item.name}</Typography>
                  <Typography variant="caption" color="text.secondary">Immutable class ID {item.id}</Typography>
                </TableCell><TableCell align="right">{item.count}</TableCell>
                <TableCell><FormControl fullWidth size="small"><InputLabel>CMDB type</InputLabel>
                  <Select label="CMDB type" value={policy.typeMappings[item.id] || ''}
                    disabled={previewRunActive}
                    onChange={event => updateTypeMapping(item.id, event.target.value)}>
                    <MenuItem value=""><em>Use N-central class name</em></MenuItem>
                    {canonicalAssetTypes.map(type => <MenuItem key={type} value={type}>{type}</MenuItem>)}
                  </Select>
                </FormControl></TableCell>
              </TableRow>)}</TableBody></Table></TableContainer>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                <Button variant="outlined" disabled={!isAdmin || Boolean(busy) || previewRunActive}
                  onClick={() => void savePolicy()}>{busy === 'save-policy' ? 'Saving…' : 'Save policy'}</Button>
                <Button variant="contained" startIcon={<CloudSyncOutlined />}
                  disabled={Boolean(busy) || previewRunActive}
                  onClick={() => void previewDevices()}>
                  {busy === 'enqueue-preview' ? 'Starting…' : 'Preview device changes'}
                </Button>
                <Button variant="outlined" disabled={!policy.id || Boolean(busy) || previewRunActive}
                  onClick={() => void syncNow()}>
                  {busy === 'enqueue-sync' ? 'Starting…' : 'Sync saved policy now'}
                </Button>
              </Stack>
            </Stack></Paper>}
            {scopedPreviewRun && <NcentralPreviewProgressPanel
              run={scopedPreviewRun}
              busy={busy === 'cancel-preview' || busy === 'retry-preview'}
              onCancel={() => void cancelPreviewRun()}
              onRetry={() => void retryPreviewRun()}
            />}
            {preview && <Stack spacing={2}>
              <Alert severity={preview.counts.conflict ? 'warning' : 'success'}>{preview.message}</Alert>
              <Grid container spacing={1.5}>{([
                ['New', preview.counts.create, 'success'],
                ['Changed', preview.counts.update, 'warning'],
                ['Identity links', preview.counts.link, 'info'],
                ['Unchanged', preview.counts.unchanged, 'default'],
                ['Conflicts', preview.counts.conflict, 'error'],
              ] as const).map(([label, value, color]) => <Grid key={label} size={{ xs: 6, md: 2.4 }}>
                <Paper variant="outlined" sx={{ p: 1.5 }}><Typography variant="h5"
                  color={color === 'default' ? 'text.primary' : `${color}.main`}>{value}</Typography>
                  <Typography variant="caption">{label}</Typography></Paper>
              </Grid>)}</Grid>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}
                sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' } }}>
                <FormControlLabel control={<Checkbox
                  checked={reviewableItems.length > 0 && reviewableItems.every(item => selectedDeviceIds.includes(item.externalId))}
                  onChange={(_, checked) => setSelectedDeviceIds(
                    checked ? reviewableItems.map(item => item.externalId) : [],
                  )}
                  disabled={!isAdmin || !reviewableItems.length || Boolean(busy)}
                />} label={`${selectedDeviceIds.length} selected`} />
                <Button variant="contained" color="success" disabled={!isAdmin || !selectedDeviceIds.length || Boolean(busy)}
                  onClick={() => void importDevices()}>
                  {busy === 'import' ? 'Importing…' : `Import ${selectedDeviceIds.length} reviewed device(s)`}
                </Button>
              </Stack>
              <TableContainer><Table size="small"><TableHead><TableRow>
                <TableCell padding="checkbox">Import</TableCell><TableCell>N-central device</TableCell>
                <TableCell>Decision</TableCell><TableCell>Canonical match</TableCell>
                <TableCell>Evidence</TableCell><TableCell align="right">Identity</TableCell>
              </TableRow></TableHead><TableBody>{preview.items.map(item => {
                const selectable = ['create', 'update', 'link'].includes(item.action);
                const linkable = ['create', 'conflict'].includes(item.action);
                return <TableRow key={item.externalId} sx={{ opacity: item.action === 'unchanged' ? 0.6 : 1 }}>
                  <TableCell padding="checkbox"><Checkbox checked={selectedDeviceIds.includes(item.externalId)}
                    disabled={!selectable || !isAdmin || Boolean(busy)}
                    onChange={(_, checked) => setSelectedDeviceIds(current => (
                      checked ? [...current, item.externalId] : current.filter(id => id !== item.externalId)
                    ))}
                    aria-label={`Import ${item.name}`}
                  /></TableCell>
                  <TableCell><Typography sx={{ fontWeight: 750 }}>{item.name}</Typography>
                    <Typography variant="caption" color="text.secondary">{item.record.providerTypeName} · device ID {item.externalId}</Typography>
                  </TableCell>
                  <TableCell><Chip size="small" label={item.action}
                    color={item.action === 'create' ? 'success' : item.action === 'update' ? 'warning' : item.action === 'link' ? 'info' : item.action === 'conflict' ? 'error' : 'default'}
                  /></TableCell>
                  <TableCell>{item.assetName || `New ${item.record.type}`}</TableCell>
                  <TableCell><Typography variant="body2">{item.reason}</Typography>
                    {item.changedFields.length > 0 && <Typography variant="caption" color="text.secondary">Changes: {item.changedFields.join(', ')}</Typography>}
                  </TableCell>
                  <TableCell align="right">{linkable && <Button size="small" startIcon={<LinkOutlined />}
                    disabled={!isAdmin || Boolean(busy)} onClick={() => void openLink(item)}>Link existing</Button>}</TableCell>
                </TableRow>;
              })}</TableBody></Table></TableContainer>
              <Alert severity="info">Serial, MAC and model enrichment is limited to the first 25 devices and uses at most four concurrent read-only requests. Durable N-central device IDs remain the primary identity.</Alert>
            </Stack>}
          </Stack>}

          <Stack direction="row" spacing={1} sx={{ justifyContent: 'space-between', mt: 4 }}>
            <Button disabled={activeStep === 0} onClick={() => setActiveStep(step => step - 1)}>Back</Button>
            <Button variant="contained" disabled={
              activeStep === steps.length - 1
              || (activeStep === 1 && !connectionReady)
              || (activeStep === 2 && !organizations.length)
            } onClick={() => setActiveStep(step => step + 1)}>Continue</Button>
          </Stack>
        </CardContent></Card>
      )}

      <Dialog open={Boolean(createOrganization)} onClose={() => { if (busy !== 'create') setCreateOrganization(null); }} fullWidth maxWidth="sm">
        <Stack component="form" onSubmit={createCustomerAndMap}>
          <DialogTitle>Create and map customer</DialogTitle>
          <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
            {createOrganization && <Alert severity="info">Create an isolated CMDB customer for <strong>{createOrganization.name}</strong> and map immutable organization ID {createOrganization.externalId}.</Alert>}
            <TextField required label="Customer name" value={newCustomerName}
              onChange={event => setNewCustomerName(event.target.value)} />
            <TextField label="Customer ID" value={newCustomerSlug}
              onChange={event => setNewCustomerSlug(event.target.value)}
              slotProps={{ htmlInput: { pattern: '[a-z0-9-]*' } }}
            />
          </Stack></DialogContent>
          <DialogActions><Button onClick={() => setCreateOrganization(null)} disabled={busy === 'create'}>Cancel</Button>
            <Button type="submit" variant="contained" disabled={!newCustomerName.trim() || busy === 'create'}>
              {busy === 'create' ? 'Creating…' : 'Create and map'}
            </Button>
          </DialogActions>
        </Stack>
      </Dialog>

      <Dialog open={Boolean(linkItem)} onClose={() => { if (busy !== 'link') setLinkItem(null); }} fullWidth maxWidth="sm">
        <DialogTitle>Link N-central device identity</DialogTitle>
        <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
          {linkItem && <Alert severity="warning">Confirm that <strong>{linkItem.name}</strong> and the selected CMDB CI are the same real device. The durable mapping uses N-central device ID {linkItem.externalId}.</Alert>}
          <Autocomplete options={linkAssets}
            value={linkAssets.find(asset => asset.id === linkAssetId) || null}
            onChange={(_, asset) => setLinkAssetId(asset?.id || '')}
            getOptionLabel={asset => `${asset.name} · ${asset.type}`}
            isOptionEqualToValue={(option, value) => option.id === value.id}
            renderInput={params => <TextField {...params} label="Existing canonical CI" />}
          />
        </Stack></DialogContent>
        <DialogActions><Button onClick={() => setLinkItem(null)} disabled={busy === 'link'}>Cancel</Button>
          <Button variant="contained" startIcon={<LinkOutlined />} disabled={!linkAssetId || busy === 'link'}
            onClick={() => void linkDevice()}>{busy === 'link' ? 'Linking…' : 'Confirm identity link'}</Button>
        </DialogActions>
      </Dialog>
    </RootGuard>
  );
}
