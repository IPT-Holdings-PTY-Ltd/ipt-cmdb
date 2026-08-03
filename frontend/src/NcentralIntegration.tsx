import AddBusinessOutlined from '@mui/icons-material/AddBusinessOutlined';
import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import CloudSyncOutlined from '@mui/icons-material/CloudSyncOutlined';
import FactCheckOutlined from '@mui/icons-material/FactCheckOutlined';
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
  NcentralDeviceOptions, NcentralDiscoveryPreview, NcentralGraphqlConfig,
  NcentralGraphqlPreview, NcentralGraphqlTestResult, NcentralPreviewRun,
  MissingDeviceLifecycleQueue, ProviderCompany,
} from './types';
import { useWorkspace } from './workspace';

type Notice = { severity: 'success' | 'error' | 'info' | 'warning'; message: string } | null;

type NcentralSetupStatus = {
  overallStatus: 'ready' | 'needs_attention' | 'blocked';
  blockers: Array<{ gate: string; message: string }>;
  warnings: Array<{ gate: string; message: string }>;
  recommendedNextAction: { key: string; label: string; path: string };
};

type NcentralCapabilityShapeField = {
  name: string;
  type: string;
};

type NcentralCapabilityShape = {
  type?: string;
  count?: number;
  itemTypes?: Record<string, number>;
  valueTypes?: Record<string, number>;
  fields?: NcentralCapabilityShapeField[];
  itemFields?: NcentralCapabilityShapeField[];
  sections?: Array<{
    name: string;
    type?: string;
    count?: number;
    fields?: NcentralCapabilityShapeField[];
    itemFields?: NcentralCapabilityShapeField[];
  }>;
};

type NcentralCapabilityEndpoint = {
  accessStatus: string;
  httpStatus?: number;
  shape?: NcentralCapabilityShape;
};

export type NcentralCapabilityResult = {
  provider: 'ncentral';
  readOnly: boolean;
  requestedDevice: boolean;
  sampleCount: number;
  maximumSampleCount: number;
  samples: Array<{
    sample: number;
    endpoints: Record<string, NcentralCapabilityEndpoint>;
  }>;
  organizationEndpoints: Record<string, NcentralCapabilityEndpoint>;
};

export type NcentralCapabilityRequest = {
  companyId: string;
  providerCompanyId: string;
  externalId: string;
  sampleLimit: number;
};

type CapabilityDeviceChoice = {
  externalId: string;
  name: string;
};

const CAPABILITY_ENDPOINT_LABELS: Record<string, string> = {
  device_details: 'Device details',
  assets: 'Technical asset inventory',
  lifecycle: 'Lifecycle information',
  custom_properties: 'Custom-property schema',
  service_monitor_status: 'Service monitor status',
  maintenance_windows: 'Maintenance windows',
  active_issues: 'Active issues',
};

function words(value: string): string {
  return value.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replaceAll('_', ' ')
    .replace(/\b\w/g, character => character.toUpperCase());
}

function endpointColor(
  status: string,
): 'success' | 'warning' | 'error' | 'default' {
  if (status === 'available') return 'success';
  if (status === 'rate_limited' || status === 'unavailable') return 'warning';
  if (status === 'unauthorized' || status === 'forbidden') return 'error';
  return 'default';
}

function CapabilityShapeEvidence({ shape }: { shape?: NcentralCapabilityShape }) {
  if (!shape) {
    return <Typography variant="caption" color="text.secondary">
      No response shape was available.
    </Typography>;
  }
  const fields = shape.fields || shape.itemFields || [];
  const typeCounts = shape.itemTypes || shape.valueTypes || {};
  return <Stack spacing={0.75}>
    <Stack direction="row" spacing={0.75} useFlexGap sx={{ flexWrap: 'wrap' }}>
      <Chip size="small" variant="outlined" label={`Shape: ${shape.type || 'unknown'}`} />
      {typeof shape.count === 'number'
        && <Chip size="small" variant="outlined" label={`${shape.count} top-level item(s)`} />}
      {Object.entries(typeCounts).map(([type, count]) => (
        <Chip key={type} size="small" variant="outlined" label={`${count} ${type}`} />
      ))}
    </Stack>
    {fields.length > 0 && <Typography variant="caption" color="text.secondary">
      Fields: {fields.slice(0, 18).map(field => `${field.name} (${field.type})`).join(', ')}
      {fields.length > 18 ? `, +${fields.length - 18} more` : ''}
    </Typography>}
    {(shape.sections || []).length > 0 && <Typography variant="caption" color="text.secondary">
      Sections: {(shape.sections || []).slice(0, 12).map(section => (
        `${section.name}${typeof section.count === 'number' ? ` (${section.count})` : ''}`
      )).join(', ')}
      {(shape.sections || []).length > 12 ? `, +${(shape.sections || []).length - 12} more` : ''}
    </Typography>}
  </Stack>;
}

function CapabilityEndpointRow({
  name,
  endpoint,
}: {
  name: string;
  endpoint: NcentralCapabilityEndpoint;
}) {
  return <TableRow>
    <TableCell sx={{ minWidth: 190 }}>
      <Typography variant="body2" sx={{ fontWeight: 750 }}>
        {CAPABILITY_ENDPOINT_LABELS[name] || words(name)}
      </Typography>
      <Typography variant="caption" color="text.secondary">Read endpoint</Typography>
    </TableCell>
    <TableCell sx={{ width: 170 }}>
      <Chip
        size="small"
        color={endpointColor(endpoint.accessStatus)}
        label={words(endpoint.accessStatus)}
      />
      {endpoint.httpStatus
        ? <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
          HTTP {endpoint.httpStatus}
        </Typography>
        : null}
    </TableCell>
    <TableCell><CapabilityShapeEvidence shape={endpoint.shape} /></TableCell>
  </TableRow>;
}

export function NcentralCapabilityResults({ result }: { result: NcentralCapabilityResult }) {
  const available = [
    ...result.samples.flatMap(sample => Object.values(sample.endpoints)),
    ...Object.values(result.organizationEndpoints),
  ].filter(endpoint => endpoint.accessStatus === 'available').length;
  const total = result.samples.reduce(
    (count, sample) => count + Object.keys(sample.endpoints).length,
    Object.keys(result.organizationEndpoints).length,
  );
  return <Stack spacing={1.5} aria-live="polite">
    <Alert severity={available === total ? 'success' : available > 0 ? 'warning' : 'error'}>
      {available} of {total} read endpoint checks are available across {result.sampleCount} sampled
      device{result.sampleCount === 1 ? '' : 's'}.
    </Alert>
    {result.sampleCount === 0 && <Alert severity="info">
      No device was returned for this customer. Organization-level endpoint access is shown below.
    </Alert>}
    {result.samples.map(sample => <Box key={sample.sample}>
      <Typography variant="subtitle2" sx={{ mb: 0.75 }}>
        Device sample {sample.sample}
      </Typography>
      <TableContainer component={Paper} variant="outlined">
        <Table size="small" aria-label={`N-central device sample ${sample.sample} capabilities`}>
          <TableHead><TableRow>
            <TableCell>Capability</TableCell><TableCell>Access</TableCell>
            <TableCell>Shape-only evidence</TableCell>
          </TableRow></TableHead>
          <TableBody>{Object.entries(sample.endpoints).map(([name, endpoint]) => (
            <CapabilityEndpointRow key={name} name={name} endpoint={endpoint} />
          ))}</TableBody>
        </Table>
      </TableContainer>
    </Box>)}
    <Box>
      <Typography variant="subtitle2" sx={{ mb: 0.75 }}>Customer-level capabilities</Typography>
      <TableContainer component={Paper} variant="outlined">
        <Table size="small" aria-label="N-central customer capabilities">
          <TableHead><TableRow>
            <TableCell>Capability</TableCell><TableCell>Access</TableCell>
            <TableCell>Shape-only evidence</TableCell>
          </TableRow></TableHead>
          <TableBody>{Object.entries(result.organizationEndpoints).map(([name, endpoint]) => (
            <CapabilityEndpointRow key={name} name={name} endpoint={endpoint} />
          ))}</TableBody>
        </Table>
      </TableContainer>
    </Box>
  </Stack>;
}

async function requestNcentralCapabilities(
  request: NcentralCapabilityRequest,
): Promise<NcentralCapabilityResult> {
  return apiFetch<NcentralCapabilityResult>('/api/integrations/ncentral/capabilities', {
    method: 'POST',
    body: JSON.stringify(request),
  });
}

export function NcentralCapabilityCheck({
  companyId,
  providerCompanyId,
  providerCompanyName,
  deviceChoices = [],
  disabled = false,
  probe = requestNcentralCapabilities,
}: {
  companyId: string;
  providerCompanyId: string;
  providerCompanyName?: string;
  deviceChoices?: CapabilityDeviceChoice[];
  disabled?: boolean;
  probe?: (request: NcentralCapabilityRequest) => Promise<NcentralCapabilityResult>;
}) {
  const [externalId, setExternalId] = useState('');
  const [sampleLimit, setSampleLimit] = useState(1);
  const [result, setResult] = useState<NcentralCapabilityResult | null>(null);
  const [probeError, setProbeError] = useState('');
  const [probing, setProbing] = useState(false);
  const validExternalId = !externalId.trim() || /^\d+$/.test(externalId.trim());

  async function runProbe() {
    if (!companyId || !providerCompanyId || !validExternalId) return;
    setProbing(true);
    setProbeError('');
    setResult(null);
    try {
      setResult(await probe({
        companyId,
        providerCompanyId,
        externalId: externalId.trim(),
        sampleLimit,
      }));
    } catch (value) {
      setProbeError(errorMessage(value));
    } finally {
      setProbing(false);
    }
  }

  return <Card variant="outlined">
    <CardContent>
      <Stack spacing={2}>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={1}
          sx={{ justifyContent: 'space-between', alignItems: { md: 'flex-start' } }}>
          <Box>
            <Typography variant="overline" color="primary">Read-only verification</Typography>
            <Typography variant="h6">Inventory capability check</Typography>
            <Typography variant="body2" color="text.secondary">
              Verify which technical inventory endpoints are accessible for
              {providerCompanyName ? ` ${providerCompanyName}` : ' this mapped customer'}.
            </Typography>
          </Box>
          <Stack direction="row" spacing={0.75} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Chip size="small" color="success" variant="outlined" label="No writes" />
            <Chip size="small" color="success" variant="outlined" label="No provider values" />
          </Stack>
        </Stack>
        <Alert severity="info">
          The check reads at most three devices and returns endpoint access plus response shape only.
          Names, serial numbers, custom-property values and credentials are never returned here.
        </Alert>
        <Grid container spacing={1.5} sx={{ alignItems: 'flex-start' }}>
          <Grid size={{ xs: 12, md: 7 }}>
            <TextField
              fullWidth
              label="Device ID (optional)"
              value={externalId}
              disabled={disabled || probing}
              onChange={event => setExternalId(event.target.value)}
              error={!validExternalId}
              helperText={!validExternalId
                ? 'Use the immutable numeric N-central device ID.'
                : 'Leave blank to sample devices, or select an ID from the current preview.'}
              slotProps={{
                htmlInput: {
                  inputMode: 'numeric',
                  pattern: '[0-9]*',
                  list: `ncentral-capability-devices-${providerCompanyId}`,
                },
              }}
            />
            <datalist id={`ncentral-capability-devices-${providerCompanyId}`}>
              {deviceChoices.map(choice => <option
                key={choice.externalId}
                value={choice.externalId}
              >{choice.name}</option>)}
            </datalist>
          </Grid>
          <Grid size={{ xs: 12, sm: 5, md: 2 }}>
            <FormControl fullWidth disabled={disabled || probing}>
              <InputLabel>Sample size</InputLabel>
              <Select
                label="Sample size"
                value={sampleLimit}
                onChange={event => setSampleLimit(Number(event.target.value))}
              >
                {[1, 2, 3].map(value => <MenuItem key={value} value={value}>{value}</MenuItem>)}
              </Select>
            </FormControl>
          </Grid>
          <Grid size={{ xs: 12, sm: 7, md: 3 }}>
            <Button
              fullWidth
              variant="outlined"
              startIcon={probing ? <CircularProgress size={18} /> : <FactCheckOutlined />}
              disabled={disabled || probing || !companyId || !providerCompanyId || !validExternalId}
              onClick={() => void runProbe()}
              sx={{ minHeight: 56 }}
            >
              {probing ? 'Checking…' : 'Check capabilities'}
            </Button>
          </Grid>
        </Grid>
        {probeError && <Alert severity="error">{probeError}</Alert>}
        {!result && !probeError && !probing && <Alert severity="info" variant="outlined">
          Run the check to see safe capability evidence. This does not import or update any CI.
        </Alert>}
        {probing && <Box aria-live="polite">
          <LinearProgress aria-label="N-central capability check" />
          <Typography variant="caption" color="text.secondary">
            Reading bounded endpoint metadata…
          </Typography>
        </Box>}
        {result && <NcentralCapabilityResults result={result} />}
      </Stack>
    </CardContent>
  </Card>;
}

export type NcentralGraphqlConfigInput = {
  graphqlEnabled: boolean;
  graphqlEndpoint: string;
  graphqlApiToken: string;
  graphqlPageSize: number;
  graphqlServerId: string;
  expectedRevision?: number;
};

/** Keep the REST and GraphQL editors on the same shared N-central connection revision. */
export function syncNcentralSharedRevision<T extends { revision?: number }>(
  current: T | null,
  revision: number | undefined,
): T | null {
  if (!current || typeof revision !== 'number' || current.revision === revision) return current;
  return { ...current, revision };
}

export type NcentralGraphqlCustomerCandidate =
  NcentralGraphqlTestResult['customerCandidates'][number];

export type NcentralGraphqlPreviewRequest = {
  companyId: string;
  providerCompanyId: string;
  queryKey: 'asset_identity' | 'asset_inventory';
  limit: number;
};

export type NcentralGraphqlServerCandidate = {
  serverId: string;
  graphqlDeviceCount: number;
  restDeviceMatchCount: number;
};

export type NcentralGraphqlServerDetection = {
  companyId: string;
  providerCompanyId: string;
  candidates: NcentralGraphqlServerCandidate[];
  recommendedServerId: string | null;
  confidence: 'exact' | 'ambiguous' | 'none';
  truncated: boolean;
  readOnly: boolean;
  writesAttempted: boolean;
};

const DEFAULT_GRAPHQL_ENDPOINT = 'https://api.n-able.com/graphql';

async function saveNcentralGraphqlConfig(
  input: NcentralGraphqlConfigInput,
): Promise<NcentralGraphqlConfig> {
  return apiFetch<NcentralGraphqlConfig>('/api/integrations/ncentral/graphql/config', {
    method: 'PUT',
    body: JSON.stringify(input),
  });
}

async function testNcentralGraphqlConnection(): Promise<NcentralGraphqlTestResult> {
  return apiFetch<NcentralGraphqlTestResult>('/api/integrations/ncentral/graphql/test', {
    method: 'POST',
  });
}

async function detectNcentralGraphqlServer(
  request: Pick<NcentralGraphqlPreviewRequest, 'companyId' | 'providerCompanyId' | 'limit'>,
): Promise<NcentralGraphqlServerDetection> {
  return apiFetch<NcentralGraphqlServerDetection>(
    '/api/integrations/ncentral/graphql/server-candidates',
    {
      method: 'POST',
      body: JSON.stringify(request),
    },
  );
}

async function previewNcentralGraphql(
  request: NcentralGraphqlPreviewRequest,
): Promise<NcentralGraphqlPreview> {
  return apiFetch<NcentralGraphqlPreview>('/api/integrations/ncentral/graphql/preview', {
    method: 'POST',
    body: JSON.stringify(request),
  });
}

async function refreshNcentralGraphql(
  request: NcentralGraphqlPreviewRequest,
): Promise<NcentralGraphqlPreview> {
  return apiFetch<NcentralGraphqlPreview>('/api/integrations/ncentral/graphql/refresh', {
    method: 'POST',
    body: JSON.stringify(request),
  });
}

function readableTimestamp(value?: string | null): string {
  if (!value) return 'Not reported';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return 'Not reported';
  return parsed.toLocaleString();
}

function credentialLabel(value: string): string {
  return value === 'not_configured' ? 'No credential' : words(value);
}

/**
 * Resolve the token-safe credential facts returned by old and current APIs.
 *
 * `configured` was the original public flag, while newer responses also expose
 * a credential source and may expose `hasCredentials`. Treat any affirmative
 * server fact as authoritative so a successful write-only token save cannot be
 * rendered as unconfigured merely because one compatibility field is stale.
 */
export function ncentralGraphqlHasCredentials(config: NcentralGraphqlConfig): boolean {
  return Boolean(
    config.configured
    || config.hasCredentials
    || config.credentialSource === 'environment'
    || config.credentialSource === 'encrypted_database',
  );
}

type NcentralGraphqlSetupState =
  | 'not_configured'
  | 'credential_saved_disabled'
  | 'server_id_required'
  | 'ready';

export function ncentralGraphqlSetupState(
  config: NcentralGraphqlConfig,
): NcentralGraphqlSetupState {
  if (!ncentralGraphqlHasCredentials(config)) return 'not_configured';
  if (!config.graphqlEnabled) return 'credential_saved_disabled';
  if (!config.graphqlServerId.trim()) return 'server_id_required';
  return 'ready';
}

function graphqlSetupLabel(state: NcentralGraphqlSetupState): string {
  if (state === 'credential_saved_disabled') return 'credentials saved · disabled';
  if (state === 'server_id_required') return 'server identity setup';
  if (state === 'ready') return 'optional ready';
  return 'not configured';
}

function validNcentralServerId(value: string): boolean {
  const identifier = value.trim();
  return identifier.length >= 1
    && identifier.length <= 160
    && [...identifier].every(character => (
      !/\s/.test(character)
      && character.charCodeAt(0) >= 32
      && character.charCodeAt(0) !== 127
    ));
}

export function NcentralGraphqlConnectionPanel({
  config,
  restReady,
  isAdmin,
  disabled = false,
  saveConfig = saveNcentralGraphqlConfig,
  testConnection = testNcentralGraphqlConnection,
  onConfigChange,
  onTestResult,
}: {
  config: NcentralGraphqlConfig;
  restReady: boolean;
  isAdmin: boolean;
  disabled?: boolean;
  saveConfig?: (input: NcentralGraphqlConfigInput) => Promise<NcentralGraphqlConfig>;
  testConnection?: () => Promise<NcentralGraphqlTestResult>;
  onConfigChange: (config: NcentralGraphqlConfig) => void;
  onTestResult?: (result: NcentralGraphqlTestResult) => void;
}) {
  const [graphqlEnabled, setGraphqlEnabled] = useState(config.graphqlEnabled);
  const [graphqlEndpoint, setGraphqlEndpoint] = useState(
    config.graphqlEndpoint || DEFAULT_GRAPHQL_ENDPOINT,
  );
  const [graphqlPageSize, setGraphqlPageSize] = useState(config.graphqlPageSize || 25);
  const [graphqlApiToken, setGraphqlApiToken] = useState('');
  const [testResult, setTestResult] = useState<NcentralGraphqlTestResult | null>(null);
  const [panelError, setPanelError] = useState('');
  const [panelNotice, setPanelNotice] = useState('');
  const [panelBusy, setPanelBusy] = useState<'save' | 'test' | ''>('');

  useEffect(() => {
    setGraphqlEnabled(config.graphqlEnabled);
    setGraphqlEndpoint(config.graphqlEndpoint || DEFAULT_GRAPHQL_ENDPOINT);
    setGraphqlPageSize(config.graphqlPageSize || 25);
  }, [config]);

  async function submitGraphqlConfig(event: FormEvent) {
    event.preventDefault();
    setPanelBusy('save');
    setPanelError('');
    setPanelNotice('');
    try {
      const stored = await saveConfig({
        graphqlEnabled,
        graphqlEndpoint: graphqlEndpoint.trim(),
        graphqlApiToken,
        graphqlPageSize,
        graphqlServerId: config.graphqlServerId,
        expectedRevision: config.revision,
      });
      // The secret is write-only: clear the only browser-held copy immediately
      // after the server accepts it and never hydrate this field from a response.
      setGraphqlApiToken('');
      setTestResult(null);
      onConfigChange(stored);
      setPanelNotice(
        stored.graphqlEnabled
          ? stored.graphqlServerId
            ? 'GraphQL settings saved. The API token is encrypted and was not returned.'
            : 'GraphQL settings saved. Select an exact GraphQL Customer in Devices & reconciliation, then detect and confirm its N-central server identity.'
          : 'GraphQL enrichment is disabled. The REST connector remains available.',
      );
    } catch (value) {
      setPanelError(errorMessage(value));
    } finally {
      setPanelBusy('');
    }
  }

  async function runGraphqlTest() {
    setPanelBusy('test');
    setPanelError('');
    setPanelNotice('');
    setTestResult(null);
    try {
      const result = await testConnection();
      setTestResult(result);
      onTestResult?.(result);
    } catch (value) {
      setPanelError(errorMessage(value));
    } finally {
      setPanelBusy('');
    }
  }

  const validPageSize = Number.isInteger(graphqlPageSize)
    && graphqlPageSize >= 1 && graphqlPageSize <= 100;
  const hasCredentials = ncentralGraphqlHasCredentials(config);
  const graphqlSetupState = ncentralGraphqlSetupState(config);
  const graphqlReady = graphqlSetupState === 'ready';
  const canTest = isAdmin && hasCredentials && !disabled;
  const configurationLocked = config.managedByEnvironment || !isAdmin;
  const needsFirstToken = graphqlEnabled && !hasCredentials;
  const capabilityReachable = testResult
    ? testResult.reachable
    : Boolean(config.capability?.reachable
      && !config.capability.stale
      && config.capability.status === 'supported');
  const capabilityStale = !testResult && Boolean(config.capability?.stale);
  const patchQueryAvailable = Boolean(
    config.queries?.some(query => query.key === 'patch_installations'),
  );

  return <Paper variant="outlined" sx={{ p: 2 }}>
    <Stack spacing={2}>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5}
        sx={{ justifyContent: 'space-between', alignItems: { md: 'flex-start' } }}>
        <Box>
          <Typography variant="overline" color="primary">Optional enrichment channel</Typography>
          <Typography variant="h6">N-able GraphQL rich inventory</Typography>
          <Typography variant="body2" color="text.secondary">
            GraphQL adds read-only inventory detail. It does not replace REST customer discovery,
            device identity, mapping, reconciliation, or imports.
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
          <Chip size="small" color={restReady ? 'success' : 'default'}
            label={`REST · ${restReady ? 'core ready' : 'core setup required'}`} />
          <Chip size="small"
            color={graphqlReady ? 'success'
              : graphqlSetupState === 'server_id_required' ? 'warning' : 'default'}
            label={`GraphQL · ${graphqlSetupLabel(graphqlSetupState)}`} />
        </Stack>
      </Stack>

      <Alert severity="info">
        REST remains the durable source for N-central Customer and device IDs. GraphQL queries are
        permitted only after an administrator explicitly selects immutable GraphQL Customer IDs for
        a mapped CMDB customer.
      </Alert>
      {config.managedByEnvironment && <Alert severity="info">
        GraphQL connection values are managed by the deployment environment. Test and Customer
        scope controls remain available here.
      </Alert>}
      {(config.connectionStatus === 'error' || config.lastError) && <Alert severity="error">
        {config.lastError || 'GraphQL deployment configuration is invalid. Review the container settings.'}
      </Alert>}
      {hasCredentials && !config.graphqlEnabled && <Alert severity="info">
        The GraphQL credential is saved. You can test it and detect the N-central server identity
        after selecting an exact GraphQL Customer. Enable enrichment when you are ready to use the cache.
      </Alert>}
      {graphqlEnabled && !config.graphqlServerId.trim() && <Alert severity="info">
        Server identity is completed in Devices & reconciliation after an exact GraphQL Customer is
        selected. It is a stable provider ID—not the N-central URL, hostname, or customer name.
      </Alert>}
      {panelError && <Alert severity="error">{panelError}</Alert>}
      {panelNotice && <Alert severity="success">{panelNotice}</Alert>}

      <Stack component="form" spacing={2} onSubmit={submitGraphqlConfig}>
        <FormControlLabel
          control={<Checkbox checked={graphqlEnabled}
            onChange={(_, checked) => setGraphqlEnabled(checked)} />}
          label="Enable optional GraphQL enrichment"
          disabled={configurationLocked || disabled}
        />
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, lg: 8 }}><TextField
            fullWidth
            required={graphqlEnabled}
            label="N-able GraphQL endpoint"
            value={graphqlEndpoint}
            onChange={event => setGraphqlEndpoint(event.target.value)}
            placeholder={DEFAULT_GRAPHQL_ENDPOINT}
            disabled={configurationLocked || disabled}
          /></Grid>
          <Grid size={{ xs: 12, sm: 6, lg: 4 }}><TextField
            fullWidth
            type="number"
            label="Page size"
            value={graphqlPageSize}
            onChange={event => setGraphqlPageSize(Number(event.target.value))}
            slotProps={{ htmlInput: { min: 1, max: 100 } }}
            error={!validPageSize}
            helperText={validPageSize ? '1-100 rows per provider page.' : 'Enter a whole number from 1 to 100.'}
            disabled={configurationLocked || disabled}
          /></Grid>
          {!config.managedByEnvironment && isAdmin && <Grid size={{ xs: 12 }}><TextField
            fullWidth
            type="password"
            autoComplete="new-password"
            label={hasCredentials
              ? 'Replace GraphQL API token (optional)'
              : 'GraphQL API token'}
            value={graphqlApiToken}
            onChange={event => setGraphqlApiToken(event.target.value)}
            required={needsFirstToken}
            helperText={hasCredentials
              ? 'Leave blank to retain the encrypted token. Stored values are never returned.'
              : 'Required for first setup. The value is encrypted immediately and never returned.'}
            disabled={disabled}
          /></Grid>}
        </Grid>
        {!configurationLocked && <Button type="submit" variant="contained"
          disabled={disabled || Boolean(panelBusy) || !validPageSize
            || (needsFirstToken && !graphqlApiToken)}
          sx={{ alignSelf: 'flex-start' }}>
          {panelBusy === 'save' ? 'Saving…' : 'Save GraphQL settings'}
        </Button>}
      </Stack>

      <Paper variant="outlined" sx={{ p: 1.5 }}><Stack spacing={1.25}>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}
          sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' } }}>
          <Box>
            <Typography variant="subtitle1" sx={{ fontWeight: 750 }}>
              GraphQL connection and catalogue test
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Authenticates and performs a bounded Customer catalogue read; no asset values or
              provider writes are requested.
            </Typography>
          </Box>
          <Button variant="outlined" startIcon={panelBusy === 'test'
            ? <CircularProgress size={18} /> : <FactCheckOutlined />}
            disabled={!canTest || Boolean(panelBusy)}
            onClick={() => void runGraphqlTest()}>
            {panelBusy === 'test' ? 'Testing…' : 'Test GraphQL access'}
          </Button>
        </Stack>
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
          <Chip size="small" variant="outlined"
            label={`Credential · ${credentialLabel(config.credentialSource)}`} />
          <Chip size="small" color={capabilityReachable ? 'success' : 'default'}
            label={`GraphQL auth · ${capabilityReachable ? 'available' : capabilityStale ? 'retest required' : 'not tested'}`} />
          <Chip size="small" color={capabilityReachable ? 'success' : 'default'}
            label={`Customer catalogue · ${capabilityReachable ? 'available' : capabilityStale ? 'stale' : 'not tested'}`} />
          <Chip size="small" color="warning" label="Rich inventory · Customer scope required" />
          <Chip size="small" variant="outlined"
            label={`Patch installations · ${patchQueryAvailable ? 'live API ready' : 'unavailable'}`} />
        </Stack>
        {testResult && <Alert severity={testResult.reachable ? 'success' : 'error'}>
          {testResult.reachable
            ? `GraphQL access passed. ${testResult.candidateCount} Customer candidates were returned${testResult.truncated ? ' in a bounded result' : ''}.`
            : 'GraphQL access failed.'}
          {' '}No asset query or provider write was attempted. A successful test verifies the
          credential and Customer catalogue only; it does not enable GraphQL enrichment or refresh
          the asset cache.
        </Alert>}
        {config.lastTestAt && !testResult && <Typography variant="caption" color="text.secondary">
          Last server-recorded test: {readableTimestamp(config.lastTestAt)}
        </Typography>}
      </Stack></Paper>
    </Stack>
  </Paper>;
}

function nonEmptyRecord(value: object | null | undefined): boolean {
  return Boolean(value && Object.keys(value).length);
}

type OptionalGraphqlCacheCoverage = {
  cachedDeviceCount?: unknown;
  matchedDeviceCount?: unknown;
  eligibleDeviceCount?: unknown;
  totalDeviceCount?: unknown;
  providerAssetCount?: unknown;
  pagesRead?: unknown;
  complete?: unknown;
};

function optionalCount(...values: unknown[]): number | undefined {
  const value = values.find(candidate => (
    typeof candidate === 'number' && Number.isFinite(candidate) && candidate >= 0
  ));
  return typeof value === 'number' ? Math.floor(value) : undefined;
}

function graphqlCacheCoverage(
  cache: NcentralGraphqlConfig['cache'] | NcentralGraphqlPreview['cache'],
) {
  const optional = (cache || {}) as OptionalGraphqlCacheCoverage;
  return {
    cached: optionalCount(
      cache?.deviceCount,
      optional.cachedDeviceCount,
      optional.matchedDeviceCount,
    ),
    eligible: optionalCount(cache?.eligibleDeviceCount, optional.eligibleDeviceCount),
    total: optionalCount(cache?.providerAssetCount, optional.providerAssetCount,
      optional.totalDeviceCount),
    unmatched: optionalCount(cache?.unmatchedDeviceCount),
    pagesRead: optionalCount(cache?.pagesRead, optional.pagesRead),
    complete: typeof cache?.complete === 'boolean'
      ? cache.complete
      : typeof optional.complete === 'boolean' ? optional.complete : undefined,
  };
}

export function ncentralScopedGraphqlCache(
  diagnostics: NcentralEnrichmentDiagnostics | null | undefined,
): NonNullable<NcentralGraphqlConfig['cache']> | undefined {
  if (!diagnostics) return undefined;
  const sourceStatus = diagnostics.freshness.status;
  const status: NonNullable<NcentralGraphqlConfig['cache']>['status'] = sourceStatus === 'fresh'
    ? 'fresh'
    : sourceStatus === 'partial' || sourceStatus === 'incomplete'
      ? 'partial'
      : sourceStatus === 'stale'
        ? 'stale'
        : sourceStatus === 'scope_changed'
          ? 'scope_changed'
          : sourceStatus === 'error'
            ? 'error'
            : 'empty';
  return {
    status,
    providerAssetCount: diagnostics.freshness.providerAssetCount,
    eligibleDeviceCount: diagnostics.coverage.eligibleDevices,
    deviceCount: diagnostics.coverage.enrichedDevices,
    unmatchedDeviceCount: diagnostics.freshness.unmatchedDeviceCount,
    complete: diagnostics.freshness.complete,
    lastRefreshedAt: diagnostics.freshness.lastRefreshedAt,
    expiresAt: diagnostics.freshness.expiresAt,
    message: diagnostics.freshness.reason,
  };
}

function graphqlSummaryGroups(preview: NcentralGraphqlPreview): Array<[string, number]> {
  const sensitive = /token|secret|password|authorization|credential|api.?key/i;
  const counts = new Map<string, number>();
  preview.items.forEach(item => {
    Object.entries(item.summary || {}).forEach(([key, value]) => {
      if (!sensitive.test(key) && value !== null && value !== undefined) {
        counts.set(key, (counts.get(key) || 0) + 1);
      }
    });
  });
  return [...counts.entries()].sort((left, right) => right[1] - left[1]);
}

export function NcentralGraphqlCustomerEnrichmentPanel({
  config,
  companyId,
  providerCompanyId,
  providerCompanyName,
  organizationIds,
  customerCandidates,
  isAdmin,
  disabled = false,
  scopedDiagnostics,
  onOrganizationIdsChange,
  persistScope,
  detectServer = detectNcentralGraphqlServer,
  saveConfig = saveNcentralGraphqlConfig,
  onConfigChange = () => undefined,
  previewGraphql = previewNcentralGraphql,
  refreshGraphql = refreshNcentralGraphql,
}: {
  config: NcentralGraphqlConfig;
  companyId: string;
  providerCompanyId: string;
  providerCompanyName: string;
  organizationIds: string[];
  customerCandidates: NcentralGraphqlCustomerCandidate[];
  isAdmin: boolean;
  disabled?: boolean;
  scopedDiagnostics?: NcentralEnrichmentDiagnostics | null;
  onOrganizationIdsChange: (ids: string[]) => void;
  persistScope: () => Promise<boolean>;
  detectServer?: (
    request: Pick<NcentralGraphqlPreviewRequest, 'companyId' | 'providerCompanyId' | 'limit'>,
  ) => Promise<NcentralGraphqlServerDetection>;
  saveConfig?: (input: NcentralGraphqlConfigInput) => Promise<NcentralGraphqlConfig>;
  onConfigChange?: (config: NcentralGraphqlConfig) => void;
  previewGraphql?: (request: NcentralGraphqlPreviewRequest) => Promise<NcentralGraphqlPreview>;
  refreshGraphql?: (request: NcentralGraphqlPreviewRequest) => Promise<NcentralGraphqlPreview>;
}) {
  const [queryKey, setQueryKey] = useState<'asset_identity' | 'asset_inventory'>('asset_inventory');
  const [sampleLimit, setSampleLimit] = useState(Math.min(25, config.graphqlPageSize || 25));
  const [result, setResult] = useState<NcentralGraphqlPreview | null>(null);
  const [serverDetection, setServerDetection] = useState<NcentralGraphqlServerDetection | null>(null);
  const [pendingServerId, setPendingServerId] = useState(config.graphqlServerId || '');
  const [advancedServerSetup, setAdvancedServerSetup] = useState(false);
  const [panelError, setPanelError] = useState('');
  const [panelWarning, setPanelWarning] = useState('');
  const [panelNotice, setPanelNotice] = useState('');
  const [panelBusy, setPanelBusy] = useState<
    'save' | 'detect' | 'save-server' | 'preview' | 'refresh' | ''
  >('');
  const [lastActionAt, setLastActionAt] = useState<string | null>(null);
  const [lastActionKind, setLastActionKind] = useState<'preview' | 'identity' | 'refresh' | null>(null);

  useEffect(() => {
    setResult(null);
    setServerDetection(null);
    setPanelError('');
    setPanelWarning('');
    setPanelNotice('');
    setLastActionAt(null);
    setLastActionKind(null);
  }, [companyId, providerCompanyId]);

  useEffect(() => {
    setPendingServerId(config.graphqlServerId || '');
  }, [config.graphqlServerId]);

  const candidateOptions = useMemo(
    () => customerCandidates.filter(candidate => candidate.typeName.toUpperCase() === 'CUSTOMER'),
    [customerCandidates],
  );
  const selectedOptions = organizationIds.map(id => (
    candidateOptions.find(candidate => candidate.id === id)
      || { id, name: 'Manually entered immutable ID', typeName: 'CUSTOMER' }
  ));
  const invalidIds = organizationIds.filter(id => !/^\S{1,160}$/.test(id));
  const tooManyOrganizationIds = organizationIds.length > 100;
  const graphqlSetupState = ncentralGraphqlSetupState(config);
  const graphqlReady = graphqlSetupState === 'ready';
  const graphqlCredentialReady = ncentralGraphqlHasCredentials(config);
  const confirmedServerId = config.graphqlServerId.trim();
  const normalizedPendingServerId = pendingServerId.trim();
  const pendingServerIdValid = validNcentralServerId(normalizedPendingServerId);
  const serverIdentityChanged = normalizedPendingServerId !== confirmedServerId;
  const selectedServerCandidate = serverDetection?.candidates.find(
    candidate => candidate.serverId === normalizedPendingServerId,
  );
  const unambiguousDetectedCandidate = serverDetection?.confidence === 'exact'
    && serverDetection.candidates.length === 1
    && serverDetection.candidates[0]?.restDeviceMatchCount > 0
    && serverDetection.recommendedServerId === serverDetection.candidates[0]?.serverId
    ? serverDetection.candidates[0]
    : null;
  const environmentSetting = normalizedPendingServerId
    ? `NCENTRAL_GRAPHQL_SERVER_ID=${normalizedPendingServerId}`
    : '';
  const summaryGroups = result ? graphqlSummaryGroups(result) : [];
  const sourceIdentityCount = result?.items.filter(item => nonEmptyRecord(item.sourceIdentity)).length || 0;
  const restIdentityCount = result?.items.filter(item => nonEmptyRecord(item.restIdentity)).length || 0;
  const eligibleCrosswalkCount = result?.items.filter(item => (
    nonEmptyRecord(item.restIdentity)
    && Boolean(config.graphqlServerId)
    && item.restIdentity?.serverId === config.graphqlServerId
  )).length || 0;
  const observedServerIds = [...new Set((result?.items || [])
    .map(item => item.restIdentity?.serverId)
    .filter((value): value is string => Boolean(value)))];
  const cacheableQuery = queryKey === 'asset_inventory';
  const maximumSample = Math.min(100, config.graphqlPageSize || 25);
  const validSampleLimit = Number.isInteger(sampleLimit)
    && sampleLimit >= 1 && sampleLimit <= maximumSample;
  const reportedCache = result?.cache
    || ncentralScopedGraphqlCache(scopedDiagnostics)
    || config.cache;
  const cacheCoverage = graphqlCacheCoverage(reportedCache);
  const cacheStatus = panelBusy === 'refresh'
    ? 'refreshing'
    : reportedCache?.status || 'empty';
  const cacheColor = cacheStatus === 'fresh'
    ? 'success'
    : cacheStatus === 'error'
      ? 'error'
      : cacheStatus === 'stale' || cacheStatus === 'partial'
        || cacheStatus === 'scope_changed' || cacheStatus === 'configuration_changed'
      ? 'warning'
      : 'default';
  const cacheStateMessage = reportedCache?.message || (
    cacheStatus === 'stale'
      ? 'Cached GraphQL inventory is expired. Refresh it before the next N-central import.'
      : cacheStatus === 'fresh'
        ? 'Current cached GraphQL inventory is available to the next N-central import.'
        : cacheStatus === 'error'
          ? 'The last cache operation failed. Review the error and retry the full-scope refresh.'
          : 'No cached GraphQL inventory is available for this mapped Customer. Run a full-scope refresh to populate it.'
  );

  function changeOrganizations(values: Array<NcentralGraphqlCustomerCandidate | string>) {
    const ids = values.map(value => (typeof value === 'string' ? value : value.id).trim())
      .filter(Boolean);
    const uniqueIds = [...new Set(ids)];
    if (uniqueIds.length > 100) {
      setPanelError('Select no more than 100 GraphQL Customer IDs.');
      return;
    }
    setPanelError('');
    setPanelWarning('');
    onOrganizationIdsChange(uniqueIds);
    setResult(null);
    setServerDetection(null);
    setPendingServerId(config.graphqlServerId || '');
    setPanelNotice('GraphQL Customer scope changed. Save it before previewing inventory.');
  }

  async function saveScope() {
    if (!organizationIds.length || invalidIds.length || tooManyOrganizationIds) return;
    setPanelBusy('save');
    setPanelError('');
    setPanelWarning('');
    try {
      if (await persistScope()) {
        setPanelNotice('GraphQL Customer IDs saved with the mapped customer policy and audit revision.');
      }
    } catch (value) {
      setPanelError(errorMessage(value));
    } finally {
      setPanelBusy('');
    }
  }

  async function detectServerIdentity() {
    if (!organizationIds.length || invalidIds.length || tooManyOrganizationIds
      || !graphqlCredentialReady) return;
    setPanelBusy('detect');
    setPanelError('');
    setPanelWarning('');
    setPanelNotice('');
    setServerDetection(null);
    try {
      if (!await persistScope()) return;
      const detection = await detectServer({
        companyId,
        providerCompanyId,
        limit: Math.min(100, config.graphqlPageSize || 25),
      });
      setServerDetection(detection);
      const exactCandidate = detection.confidence === 'exact'
        && detection.candidates.length === 1
        && detection.candidates[0]?.restDeviceMatchCount > 0
        && detection.recommendedServerId === detection.candidates[0]?.serverId
        ? detection.candidates[0]
        : null;
      if (!confirmedServerId && exactCandidate) {
        setPendingServerId(exactCandidate.serverId);
        setPanelNotice(
          `Detected ${exactCandidate.serverId} from ${exactCandidate.restDeviceMatchCount} exact GraphQL-to-REST device matches. Review and save this binding.`,
        );
      } else if (!detection.candidates.length) {
        setPanelNotice(
          'No N-central server identity was returned for this Customer scope. Try another mapped Customer or use Advanced setup.',
        );
      } else if (confirmedServerId) {
        setPanelNotice(
          detection.candidates.some(candidate => candidate.serverId === confirmedServerId)
            ? 'Detection completed. The saved server identity is present in this Customer scope.'
            : 'Detection completed, but the saved server identity was not observed. Review the evidence before changing it.',
        );
      } else {
        setPanelNotice(
          'Multiple or unverified N-central server identities were found. Select one using the exact REST match evidence; the app will not guess across servers.',
        );
      }
    } catch (value) {
      setPanelError(errorMessage(value));
    } finally {
      setPanelBusy('');
    }
  }

  async function saveServerIdentity() {
    if (!pendingServerIdValid || !serverIdentityChanged || config.managedByEnvironment) return;
    setPanelBusy('save-server');
    setPanelError('');
    setPanelWarning('');
    setPanelNotice('');
    try {
      const stored = await saveConfig({
        graphqlEnabled: config.graphqlEnabled,
        graphqlEndpoint: config.graphqlEndpoint,
        graphqlApiToken: '',
        graphqlPageSize: config.graphqlPageSize,
        graphqlServerId: normalizedPendingServerId,
        expectedRevision: config.revision,
      });
      onConfigChange(stored);
      setPanelNotice(
        stored.graphqlEnabled
          ? `N-central server identity ${normalizedPendingServerId} saved. GraphQL cache and enrichment controls are now available.`
          : `N-central server identity ${normalizedPendingServerId} saved. Enable GraphQL enrichment in Connection when you are ready to use the cache.`,
      );
    } catch (value) {
      setPanelError(errorMessage(value));
    } finally {
      setPanelBusy('');
    }
  }

  async function runGraphqlOperation(operation: 'preview' | 'identity' | 'refresh') {
    const refresh = operation === 'refresh';
    const liveRead = refresh || operation === 'identity';
    if (!organizationIds.length || invalidIds.length || tooManyOrganizationIds
      || (!refresh && !validSampleLimit) || !graphqlReady) return;
    setPanelBusy(refresh ? 'refresh' : 'preview');
    setPanelError('');
    setPanelWarning('');
    setPanelNotice('');
    try {
      if (!await persistScope()) return;
      const request = {
        companyId,
        providerCompanyId,
        queryKey: refresh ? 'asset_inventory' as const : queryKey,
        // Keep the existing request contract. For asset_inventory refreshes,
        // the backend intentionally ignores this compatibility limit and
        // follows the complete saved scope within its server-side safety cap.
        limit: refresh ? 500 : sampleLimit,
      };
      const preview = await (liveRead ? refreshGraphql(request) : previewGraphql(request));
      setResult(preview);
      setLastActionAt(new Date().toISOString());
      setLastActionKind(operation);
      const generationPublished = preview.refreshed !== false
        && preview.cache?.complete !== false;
      if (refresh && !generationPublished) {
        const message = preview.cache?.message
          || 'The full-scope read completed, but no cache generation was published.';
        if (preview.cache?.status === 'error') setPanelError(message);
        else setPanelWarning(message);
      } else {
        setPanelNotice(
          refresh
            ? 'Full-scope GraphQL cache refresh completed for this mapped Customer. Review the coverage counts below.'
            : operation === 'identity'
              ? 'Bounded live identity sample completed. Identity-only results are not stored in the enrichment cache.'
              : 'Bounded sample loaded from the existing GraphQL inventory cache.',
        );
      }
    } catch (value) {
      setPanelError(errorMessage(value));
    } finally {
      setPanelBusy('');
    }
  }

  return <Paper variant="outlined" sx={{ p: 2 }}><Stack spacing={2}>
    <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5}
      sx={{ justifyContent: 'space-between', alignItems: { md: 'flex-start' } }}>
      <Box>
        <Typography variant="overline" color="primary">Customer-only GraphQL scope</Typography>
        <Typography variant="h6">Rich inventory for {providerCompanyName}</Typography>
        <Typography variant="body2" color="text.secondary">
          Select immutable GraphQL Customer IDs explicitly. Names and REST organization IDs are
          never used as an automatic cross-tenant match.
        </Typography>
      </Box>
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
        <Chip size="small" color={graphqlReady ? 'success' : 'default'}
          label={`GraphQL · ${graphqlSetupLabel(graphqlSetupState)}`} />
        <Chip size="small" color={cacheColor} label={`Cache · ${words(cacheStatus)}`} />
      </Stack>
    </Stack>
    <Alert severity="warning">
      Customer-only guard: these reads are limited to the saved GraphQL Customer ID selection for
      this mapped CMDB customer. MSP-wide, all-customer, and name-inferred queries are not available.
    </Alert>
    {!graphqlCredentialReady && <Alert severity="info">
      Save and test the optional GraphQL credential in the Connection step. REST previews and imports
      continue to work without it.
    </Alert>}
    {graphqlSetupState === 'credential_saved_disabled' && <Alert severity="warning">
      GraphQL credentials are saved, but enrichment is disabled. A successful connection test or
      server-identity detection does not enable it. Enable GraphQL in Connection before previewing,
      refreshing, or enriching N-central imports.
    </Alert>}
    {graphqlSetupState === 'server_id_required' && <Alert severity="info">
      GraphQL is enabled, but the immutable N-central server identity is not saved. Select the exact
      Customer scope, detect the server identity below, and save it before using the cache.
    </Alert>}
    {graphqlReady && cacheStatus === 'empty' && <Alert severity="info">
      GraphQL is enabled, but this mapped Customer has no cached rich inventory. Run the full-scope
      cache refresh below; a connection test alone does not populate asset data.
    </Alert>}
    {graphqlReady && cacheStatus === 'stale' && <Alert severity="warning">
      This mapped Customer's GraphQL cache is stale. N-central imports use only current cached
      evidence, so refresh the full scope before the next import.
    </Alert>}
    {panelError && <Alert severity="error">{panelError}</Alert>}
    {panelWarning && <Alert severity="warning">{panelWarning}</Alert>}
    {panelNotice && <Alert severity="info">{panelNotice}</Alert>}

    <Autocomplete<NcentralGraphqlCustomerCandidate | string, true, false, true>
      multiple
      freeSolo
      filterSelectedOptions
      options={candidateOptions}
      value={selectedOptions}
      disabled={!isAdmin || disabled}
      getOptionLabel={option => typeof option === 'string'
        ? option
        : `${option.name} · ${option.id}`}
      isOptionEqualToValue={(option, value) => (
        (typeof option === 'string' ? option : option.id)
        === (typeof value === 'string' ? value : value.id)
      )}
      onChange={(_, values) => changeOrganizations(values)}
      renderInput={params => <TextField {...params}
        label="GraphQL Customer IDs"
        error={Boolean(invalidIds.length) || tooManyOrganizationIds}
        helperText={candidateOptions.length
          ? 'Search tested CUSTOMER records or enter an exact immutable ID and press Enter. Save before previewing.'
          : 'Run the GraphQL catalogue test, or enter an exact immutable Customer ID and press Enter. IDs cannot contain whitespace.'}
      />}
    />
    {invalidIds.length > 0 && <Alert severity="error">
      Customer IDs must be 1-160 character opaque identifiers without whitespace.
    </Alert>}
    {tooManyOrganizationIds && <Alert severity="error">
      Select no more than 100 GraphQL Customer IDs for one mapped customer.
    </Alert>}

    <Paper variant="outlined" sx={{ p: 1.5 }}><Stack spacing={1.5}>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={1}
        sx={{ justifyContent: 'space-between', alignItems: { md: 'center' } }}>
        <Box>
          <Typography variant="subtitle1" sx={{ fontWeight: 750 }}>
            N-central server identity
          </Typography>
          <Typography variant="body2" color="text.secondary">
            This stable provider ID distinguishes the same device number on different N-central
            servers. It is detected from ncentralDevice.server.id—not from a URL, hostname, or name.
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
          <Chip size="small" color={confirmedServerId ? 'success' : 'default'}
            label={confirmedServerId ? 'Saved identity' : 'Not detected yet'} />
          {confirmedServerId && <Chip size="small" variant="outlined"
            label={confirmedServerId} />}
        </Stack>
      </Stack>

      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
        <Button variant="outlined" startIcon={panelBusy === 'detect'
          ? <CircularProgress size={18} /> : <FactCheckOutlined />}
          disabled={!isAdmin || disabled || !graphqlCredentialReady || !organizationIds.length
            || Boolean(invalidIds.length) || tooManyOrganizationIds || Boolean(panelBusy)}
          onClick={() => void detectServerIdentity()}>
          {panelBusy === 'detect' ? 'Detecting…' : 'Detect server identity'}
        </Button>
        <Button size="small" color="inherit" aria-expanded={advancedServerSetup}
          disabled={!isAdmin || disabled}
          onClick={() => setAdvancedServerSetup(current => !current)}>
          {advancedServerSetup ? 'Hide advanced setup' : 'Advanced: enter server ID manually'}
        </Button>
      </Stack>

      {serverDetection?.truncated && <Alert severity="warning">
        Detection used a bounded sample. Review the exact REST match counts before saving.
      </Alert>}
      {serverDetection && serverDetection.candidates.length === 0 && <Alert severity="warning">
        No N-central device identity was returned for this Customer scope. Try another mapped
        Customer or use Advanced setup to enter the exact provider ID.
      </Alert>}
      {unambiguousDetectedCandidate && <Alert severity="success" variant="outlined">
        <Typography sx={{ fontWeight: 750 }}>{unambiguousDetectedCandidate.serverId}</Typography>
        Corroborated by {unambiguousDetectedCandidate.restDeviceMatchCount} exact REST device ID
        matches across {unambiguousDetectedCandidate.graphqlDeviceCount} GraphQL devices.
      </Alert>}
      {serverDetection && serverDetection.candidates.length > 0 && !unambiguousDetectedCandidate
        && <FormControl fullWidth size="small">
        <InputLabel>N-central server identity</InputLabel>
        <Select label="N-central server identity" value={normalizedPendingServerId}
          displayEmpty
          renderValue={value => {
            if (!value) return <em>Select from detected identities</em>;
            const candidate = serverDetection.candidates.find(item => item.serverId === value);
            return candidate
              ? `${candidate.serverId} · ${candidate.restDeviceMatchCount} exact REST matches · ${candidate.graphqlDeviceCount} GraphQL devices`
              : String(value);
          }}
          disabled={!isAdmin || disabled}
          onChange={event => setPendingServerId(String(event.target.value))}>
          <MenuItem value=""><em>Select from detected identities</em></MenuItem>
          {serverDetection.candidates.map(candidate => <MenuItem
            key={candidate.serverId} value={candidate.serverId}>
            {candidate.serverId} · {candidate.restDeviceMatchCount} exact REST matches ·
            {' '}{candidate.graphqlDeviceCount} GraphQL devices
          </MenuItem>)}
        </Select>
        <FormHelperText>
          {serverDetection.confidence === 'exact' && serverDetection.candidates.length === 1
            ? 'One identity was corroborated by exact REST device IDs and has been preselected for review.'
            : 'Select using exact REST match evidence. The application will not guess across servers.'}
        </FormHelperText>
      </FormControl>}

      {selectedServerCandidate && <Stack direction="row" spacing={1} useFlexGap
        sx={{ flexWrap: 'wrap' }}>
        <Chip size="small" color={selectedServerCandidate.restDeviceMatchCount > 0 ? 'success' : 'warning'}
          label={`${selectedServerCandidate.restDeviceMatchCount} exact REST matches`} />
        <Chip size="small" variant="outlined"
          label={`${selectedServerCandidate.graphqlDeviceCount} GraphQL devices`} />
      </Stack>}

      {advancedServerSetup && <Stack spacing={1}>
        <Alert severity="warning" variant="outlined">
          Manual entry bypasses automatic corroboration. Copy only the exact opaque
          {' '}ncentralDevice.server.id value returned by N-able GraphQL.
        </Alert>
        <TextField fullWidth label="Exact ncentralDevice.server.id"
          value={pendingServerId}
          onChange={event => setPendingServerId(event.target.value)}
          error={Boolean(normalizedPendingServerId) && !pendingServerIdValid}
          helperText={Boolean(normalizedPendingServerId) && !pendingServerIdValid
            ? 'Enter 1-160 characters without whitespace or control characters.'
            : 'Do not enter the N-central URL, hostname, Customer ID, site, or device ID.'}
          disabled={!isAdmin || disabled}
        />
      </Stack>}

      {config.managedByEnvironment && normalizedPendingServerId && serverIdentityChanged
        && <Stack spacing={1}>
          <Alert severity="info">
            This GraphQL connection is managed by the deployment environment. Copy the setting
            below into the container configuration and restart the application; it cannot be saved here.
          </Alert>
          <TextField fullWidth label="Container setting to copy" value={environmentSetting}
            slotProps={{ htmlInput: { readOnly: true } }} />
        </Stack>}
      {!config.managedByEnvironment && <Button variant="contained"
        disabled={!isAdmin || disabled || !pendingServerIdValid || !serverIdentityChanged
          || Boolean(panelBusy)}
        onClick={() => void saveServerIdentity()} sx={{ alignSelf: 'flex-start' }}>
        {panelBusy === 'save-server' ? 'Saving…' : 'Save server identity'}
      </Button>}
      {!confirmedServerId && normalizedPendingServerId && <Typography variant="caption"
        color="text.secondary">
        Selecting or detecting an identity does not activate enrichment. Save it explicitly first.
      </Typography>}
    </Stack></Paper>

    <Button variant="outlined" disabled={!isAdmin || disabled || !organizationIds.length
      || Boolean(invalidIds.length) || tooManyOrganizationIds || Boolean(panelBusy)}
      onClick={() => void saveScope()} sx={{ alignSelf: 'flex-start' }}>
      {panelBusy === 'save' ? 'Saving…' : 'Save GraphQL Customer scope'}
    </Button>

    <Stack direction={{ xs: 'column', lg: 'row' }} spacing={1.5}>
      <Paper component="section" aria-label="Bounded GraphQL sample"
        variant="outlined" sx={{ p: 1.5, flex: 7 }}>
        <Stack spacing={1.25}>
          <Box>
            <Typography variant="subtitle1" sx={{ fontWeight: 750 }}>Bounded sample</Typography>
            <Typography variant="body2" color="text.secondary">
              Inspect a small cached inventory sample or run a bounded identity-only read. This
              sample size never limits the full-scope cache refresh.
            </Typography>
          </Box>
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={1}>
            <FormControl size="small" sx={{ minWidth: 210 }} disabled={!graphqlReady || disabled}>
              <InputLabel>Preview query</InputLabel>
              <Select label="Preview query" value={queryKey}
                onChange={event => setQueryKey(event.target.value as typeof queryKey)}>
                <MenuItem value="asset_inventory">Rich asset inventory cache</MenuItem>
                <MenuItem value="asset_identity">Live asset identity crosswalk</MenuItem>
              </Select>
            </FormControl>
            <TextField size="small" type="number" label="Sample size" value={sampleLimit}
              onChange={event => setSampleLimit(Number(event.target.value))}
              slotProps={{ htmlInput: { min: 1, max: maximumSample } }}
              error={!validSampleLimit}
              helperText={validSampleLimit
                ? `Maximum ${maximumSample}; affects this sample only.`
                : `Enter 1-${maximumSample}.`}
              disabled={!graphqlReady || disabled} sx={{ width: 190 }} />
            <Button variant="outlined"
              disabled={!graphqlReady || disabled || !organizationIds.length
                || Boolean(invalidIds.length) || tooManyOrganizationIds
                || !validSampleLimit || Boolean(panelBusy)}
              onClick={() => void runGraphqlOperation(
                cacheableQuery ? 'preview' : 'identity',
              )}>
              {panelBusy === 'preview'
                ? 'Loading…'
                : cacheableQuery ? 'Preview cached sample' : 'Run live identity sample'}
            </Button>
          </Stack>
        </Stack>
      </Paper>

      <Paper component="section" aria-label="Full-scope GraphQL cache"
        variant="outlined" sx={{ p: 1.5, flex: 5 }}>
        <Stack spacing={1.25}>
          <Box>
            <Typography variant="subtitle1" sx={{ fontWeight: 750 }}>
              Full-scope asset cache
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Read the saved Customer scope and cache every eligible asset returned within the
              backend safety bound. This action is independent of the sample size.
            </Typography>
          </Box>
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Chip size="small" variant="outlined" label="Rich asset inventory" />
            <Chip size="small" variant="outlined" label="Backend paginated" />
          </Stack>
          <Button variant="contained" startIcon={panelBusy === 'refresh'
            ? <CircularProgress size={18} color="inherit" /> : <CloudSyncOutlined />}
            disabled={!graphqlReady || disabled || !organizationIds.length
              || Boolean(invalidIds.length) || tooManyOrganizationIds || Boolean(panelBusy)}
            onClick={() => void runGraphqlOperation('refresh')} sx={{ alignSelf: 'flex-start' }}>
            {panelBusy === 'refresh' ? 'Refreshing full scope…' : 'Refresh full-scope cache'}
          </Button>
        </Stack>
      </Paper>
    </Stack>

    {result && !config.graphqlServerId && <Alert severity="warning">
      No source server ID is configured, so none of these results can enrich REST devices.
      Observed immutable server IDs: {observedServerIds.join(', ') || 'none reported'}.
    </Alert>}
    {result && config.graphqlServerId && restIdentityCount > 0 && eligibleCrosswalkCount === 0
      && <Alert severity="warning">
        The configured source server ID does not match any returned REST crosswalk. Observed IDs:
        {' '}{observedServerIds.join(', ') || 'none reported'}.
      </Alert>}

    <Paper component="section" aria-label="GraphQL cache state" aria-live="polite"
      variant="outlined" sx={{ p: 1.5 }}><Stack spacing={1}>
      <Typography variant="subtitle2">Cache and refresh state</Typography>
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
        <Chip size="small" color={cacheColor} label={words(cacheStatus)} />
        {lastActionAt && <Chip size="small" variant="outlined"
          label={`Last action · ${readableTimestamp(lastActionAt)}`} />}
        {reportedCache?.lastRefreshedAt && <Chip size="small" variant="outlined"
          label={`Refreshed · ${readableTimestamp(reportedCache.lastRefreshedAt)}`} />}
        {reportedCache?.expiresAt && <Chip size="small" variant="outlined"
          label={`Expires · ${readableTimestamp(reportedCache.expiresAt)}`} />}
        {cacheCoverage.total !== undefined && <Chip size="small" variant="outlined"
          label={`Provider assets · ${cacheCoverage.total}`} />}
        {cacheCoverage.eligible !== undefined && <Chip size="small" variant="outlined"
          label={`Eligible · ${cacheCoverage.eligible}`} />}
        {cacheCoverage.cached !== undefined && <Chip size="small" variant="outlined"
          label={`Cached · ${cacheCoverage.cached}`} />}
        {cacheCoverage.unmatched !== undefined && <Chip size="small" variant="outlined"
          label={`Unmatched · ${cacheCoverage.unmatched}`} />}
        {cacheCoverage.pagesRead !== undefined && <Chip size="small" variant="outlined"
          label={`Pages read · ${cacheCoverage.pagesRead}`} />}
        {cacheCoverage.complete !== undefined && <Chip size="small"
          color={cacheCoverage.complete ? 'success' : 'warning'}
          label={cacheCoverage.complete ? 'Full scope completed' : 'Partial scope'} />}
      </Stack>
      <Typography variant="caption" color="text.secondary">
        {cacheStateMessage}
      </Typography>
    </Stack></Paper>

    {result && <Stack spacing={1.5} aria-live="polite">
      <Typography variant="subtitle1" sx={{ fontWeight: 750 }}>Rich-inventory preview summary</Typography>
      <Grid container spacing={1.5}>
        {[
          ['Matching assets', result.totalCount],
          [lastActionKind === 'refresh' ? 'Refresh result' : 'Bounded sample', result.items.length],
          ['Source identities', sourceIdentityCount],
          ['Eligible REST crosswalks', eligibleCrosswalkCount],
          ['Other / unmatched', result.items.length - eligibleCrosswalkCount],
        ].map(([label, value]) => <Grid key={label} size={{ xs: 6, md: 3 }}>
          <Paper variant="outlined" sx={{ p: 1.5 }}>
            <Typography variant="h6">{value}</Typography>
            <Typography variant="caption" color="text.secondary">{label}</Typography>
          </Paper>
        </Grid>)}
      </Grid>
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
        {summaryGroups.length ? summaryGroups.slice(0, 16).map(([key, count]) => (
          <Chip key={key} size="small" variant="outlined"
            label={`${words(key)} · ${count}/${result.items.length}`} />
        )) : <Chip size="small" variant="outlined" label="No rich summary groups returned" />}
      </Stack>
      {restIdentityCount < result.items.length && <Alert severity="warning">
        {result.items.length - restIdentityCount} GraphQL asset(s) have no N-central REST device
        identity crosswalk. They remain unmatched preview evidence and are not assigned a fabricated
        REST device ID.
      </Alert>}
      {result.truncated && <Alert severity="info">
        {lastActionKind === 'refresh'
          ? 'The provider reports more assets than this safely bounded refresh returned. The cache coverage above is partial; run another refresh after reviewing the configured scope and backend limits.'
          : 'This is a bounded sample. More matching assets are available; no automatic all-customer expansion was performed.'}
      </Alert>}
    </Stack>}
  </Stack></Paper>;
}

function emptyPolicy(companyId = '', providerParentId = ''): ConnectWiseCiPolicy {
  return {
    id: '', provider: 'ncentral', companyId, providerParentId, providerFilterId: '',
    typeMode: 'all', includedTypeIds: [], typeMappings: {}, blockUnmappedTypes: false,
    enrichmentMode: 'balanced', graphqlOrganizationIds: [],
    relationshipAutomationMode: 'review', relationshipAutoApproveTypes: [],
    relationshipMinConfidence: 0.98, relationshipMinObservations: 2,
    relationshipMaxEvidenceAgeHours: 72,
    missingDeviceRequiredSnapshots: 3, missingDeviceMinimumHours: 24,
    statusMode: 'all', includedStatusIds: [], excludedExternalIds: [],
    syncMode: 'manual', intervalMinutes: 360, enabled: false, revision: 0,
  };
}

export function withNcentralLifecyclePolicyDefaults(
  policy: ConnectWiseCiPolicy,
): ConnectWiseCiPolicy {
  return {
    ...policy,
    missingDeviceRequiredSnapshots: policy.missingDeviceRequiredSnapshots ?? 3,
    missingDeviceMinimumHours: policy.missingDeviceMinimumHours ?? 24,
  };
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : 'The N-central operation could not be completed.';
}

function previewRunIsActive(run: NcentralPreviewRun | null): boolean {
  return Boolean(run && (run.status === 'queued' || run.status === 'running'));
}

type NcentralEnrichmentMode = NonNullable<ConnectWiseCiPolicy['enrichmentMode']>;

export function ncentralRestDetailEnrichmentMessage(
  mode: NcentralEnrichmentMode,
  graphqlConfig: NcentralGraphqlConfig | null,
): string {
  const restMessage = mode === 'fast'
    ? 'REST detail enrichment is off, so the preview makes no per-device detail reads.'
    : mode === 'full'
      ? 'REST detail enrichment reads serial, MAC and model evidence for up to 250 devices, using at most four concurrent read-only requests.'
      : 'REST detail enrichment reads up to 25 devices per run, advances through the saved fleet window after each successful preview, and uses at most four concurrent read-only requests.';
  const graphqlMessage = graphqlConfig?.graphqlEnabled
    ? `The separate GraphQL rich-inventory cache is ${words(graphqlConfig.cache?.status || 'empty')}`
      + ` and currently contains ${graphqlConfig.cache?.deviceCount || 0} cached asset(s); its coverage is not controlled by this REST limit.`
    : 'The separate GraphQL rich-inventory cache is disabled and is not controlled by this REST limit.';
  return `${restMessage} ${graphqlMessage} Durable N-central device IDs remain the primary identity.`;
}

export function ncentralWizardContinueDisabled({
  activeStep,
  lastStep,
  connectionReady,
  mappedOrganizationCount,
}: {
  activeStep: number;
  lastStep: number;
  connectionReady: boolean;
  mappedOrganizationCount: number;
}): boolean {
  return activeStep === lastStep
    || (activeStep === 1 && !connectionReady)
    || (activeStep === 2 && mappedOrganizationCount === 0);
}

export type NcentralDeviceWorkflowReadinessProps = {
  mappedOrganizationCount: number;
  customerSelected: boolean;
  optionsLoaded: boolean;
  optionsLoading: boolean;
  graphqlConfig: NcentralGraphqlConfig | null;
  scopedDiagnostics?: NcentralEnrichmentDiagnostics | null;
  policySaved: boolean;
  previewRunStatus?: NcentralPreviewRun['status'];
  previewAvailable: boolean;
  reviewableCount: number;
  conflictCount: number;
  selectedCount: number;
};

export function NcentralDeviceWorkflowReadiness({
  mappedOrganizationCount,
  customerSelected,
  optionsLoaded,
  optionsLoading,
  graphqlConfig,
  scopedDiagnostics,
  policySaved,
  previewRunStatus,
  previewAvailable,
  reviewableCount,
  conflictCount,
  selectedCount,
}: NcentralDeviceWorkflowReadinessProps) {
  const previewActive = previewRunStatus === 'queued' || previewRunStatus === 'running';
  const scopedCache = ncentralScopedGraphqlCache(scopedDiagnostics);
  const graphqlCacheStatus = scopedCache?.status || graphqlConfig?.cache?.status || 'empty';
  const graphqlStatus = !graphqlConfig?.graphqlEnabled
    ? 'optional, off'
    : !graphqlConfig.graphqlServerId
      ? 'setup needed'
      : `${words(graphqlCacheStatus)} cache`;
  const previewStatus = previewActive
    ? 'running'
    : previewAvailable
      ? 'ready'
      : previewRunStatus === 'failed'
        ? 'failed'
        : previewRunStatus === 'cancelled'
          ? 'cancelled'
          : 'not run';
  const reviewStatus = !previewAvailable
    ? 'waiting for preview'
    : selectedCount > 0
      ? `${selectedCount} selected`
      : reviewableCount > 0
        ? `${reviewableCount} to review`
        : conflictCount > 0
          ? `${conflictCount} conflict(s)`
          : 'no changes';
  const phases = [
    {
      label: 'Customer',
      status: customerSelected ? 'selected' : mappedOrganizationCount ? 'select one' : 'mapping required',
      ready: customerSelected,
      warning: !customerSelected,
    },
    {
      label: 'Choices',
      status: optionsLoading ? 'loading' : optionsLoaded ? 'loaded' : 'not loaded',
      ready: optionsLoaded,
      warning: customerSelected && !optionsLoaded,
    },
    {
      label: 'GraphQL',
      status: graphqlStatus,
      ready: Boolean(
        graphqlConfig?.graphqlEnabled
        && graphqlConfig.graphqlServerId
        && graphqlCacheStatus === 'fresh',
      ),
      warning: Boolean(graphqlConfig?.graphqlEnabled && (
        !graphqlConfig.graphqlServerId
        || ['stale', 'partial', 'scope_changed', 'configuration_changed', 'error']
          .includes(graphqlCacheStatus)
      )),
    },
    {
      label: 'Policy',
      status: policySaved ? 'saved' : optionsLoaded ? 'review before preview' : 'waiting for choices',
      ready: policySaved,
      warning: false,
    },
    {
      label: 'Preview',
      status: previewStatus,
      ready: previewAvailable,
      warning: previewRunStatus === 'failed',
    },
    {
      label: 'Review',
      status: reviewStatus,
      ready: previewAvailable && reviewableCount === 0 && conflictCount === 0,
      warning: previewAvailable && conflictCount > 0,
    },
  ];

  let nextAction = 'Select a mapped N-central customer to load its device choices.';
  if (mappedOrganizationCount === 0) {
    nextAction = 'Go back to Customers and map at least one N-central customer.';
  } else if (!customerSelected) {
    nextAction = 'Select a mapped N-central customer to load its device choices.';
  } else if (optionsLoading) {
    nextAction = 'Wait while device choices and the saved policy load.';
  } else if (!optionsLoaded) {
    nextAction = 'Refresh device choices before configuring the policy.';
  } else if (previewActive) {
    nextAction = 'Wait for the background preview to finish, or leave and return later.';
  } else if (previewRunStatus === 'failed' || previewRunStatus === 'cancelled') {
    nextAction = 'Retry the preview after reviewing the failure or cancellation details.';
  } else if (!previewAvailable) {
    nextAction = policySaved
      ? 'Run Preview device changes to refresh the reconciliation queue.'
      : 'Review the device policy, then run Preview device changes; the policy is saved first.';
  } else if (selectedCount > 0) {
    nextAction = `Import the ${selectedCount} selected reviewed device(s).`;
  } else if (reviewableCount > 0) {
    nextAction = 'Select the reviewed creates, updates or identity links you want to import.';
  } else if (conflictCount > 0) {
    nextAction = 'Resolve or link the remaining identity conflicts before importing.';
  } else {
    nextAction = 'No device changes need action; refresh the preview when the source or policy changes.';
  }

  return <Paper component="section" aria-labelledby="ncentral-workflow-readiness-title"
    variant="outlined" sx={{
      p: 1.5,
      position: { lg: 'sticky' },
      top: { lg: 12 },
      zIndex: 2,
      bgcolor: 'background.paper',
    }}>
    <Stack spacing={1}>
      <Typography id="ncentral-workflow-readiness-title" variant="subtitle2">
        Device workflow readiness
      </Typography>
      <Box component="ul" aria-label="Device reconciliation phases" sx={{
        display: 'flex', flexWrap: 'wrap', gap: 0.75, p: 0, m: 0, listStyle: 'none',
      }}>
        {phases.map(phase => <Box component="li" key={phase.label}>
          <Chip size="small"
            icon={phase.ready ? <CheckCircleOutlined /> : undefined}
            color={phase.ready ? 'success' : phase.warning ? 'warning' : 'default'}
            variant={phase.ready ? 'filled' : 'outlined'}
            label={`${phase.label} · ${phase.status}`}
          />
        </Box>)}
      </Box>
      <Typography variant="body2" color="text.secondary" aria-live="polite">
        <Box component="span" sx={{ fontWeight: 750, color: 'text.primary' }}>Next: </Box>
        {nextAction}
      </Typography>
    </Stack>
  </Paper>;
}

export type NcentralEnrichmentDiagnosticItem = {
  externalId: string;
  name: string;
  type: string;
  status: string;
  assetId?: string | null;
  assetName: string;
  reason: string;
  reasonLabel: string;
  detail: string;
  mapped: boolean;
  reviewState: string;
  reviewAction: string;
  nextAction?: { key: string; label: string };
  lastSeenAt?: string | null;
  cacheObservedAt?: string | null;
  cacheExpiresAt?: string | null;
};

export type NcentralEnrichmentDiagnostics = {
  provider: 'ncentral';
  company: { id: string; name: string };
  providerCompany: { id: string; name: string };
  readOnly: boolean;
  generatedAt: string;
  summary: {
    knownDevices: number;
    returnedDevices: number;
    mappedDevices?: number;
    reviewedDevices?: number;
    reasonCounts: Record<string, number>;
    unmatchedIdentities?: number;
  };
  freshness: {
    status: string;
    reason: string;
    enabled: boolean;
    configured: boolean;
    scopeConfigured: boolean;
    serverConfigured: boolean;
    complete: boolean;
    scopeMatches: boolean;
    lastRefreshedAt?: string | null;
    expiresAt?: string | null;
    providerAssetCount: number;
    eligibleDeviceCount: number;
    unmatchedDeviceCount: number;
  };
  coverage: {
    eligibleDevices: number;
    evaluatedDevices: number;
    enrichedDevices: number;
    enrichedPercent: number;
  };
  filters: {
    reason: string;
    status?: string;
    search: string;
    limit: number;
    offset: number;
  };
  total: number;
  items: NcentralEnrichmentDiagnosticItem[];
  latestPreview?: {
    id: string;
    status: string;
    finishedAt?: string | null;
    discovered?: number;
    included?: number;
    excluded?: number;
    exclusionReasons?: Record<string, number>;
    counts?: Record<string, number>;
  } | null;
  limitations?: string[];
};

export type NcentralEnrichmentDiagnosticsRequest = {
  companyId: string;
  providerCompanyId: string;
  status: string;
  search: string;
  limit: number;
  offset: number;
};

type LoadNcentralEnrichmentDiagnostics = (
  request: NcentralEnrichmentDiagnosticsRequest,
  signal?: AbortSignal,
) => Promise<NcentralEnrichmentDiagnostics>;

const NCENTRAL_DIAGNOSTIC_REASON_LABELS: Record<string, string> = {
  enriched: 'Enriched',
  graphql_missing: 'GraphQL missing',
  stale_cache_evidence: 'Stale cache evidence',
  cache_unavailable: 'Cache unavailable',
  filtered_excluded: 'Filtered or excluded',
  ignored: 'Ignored',
  graphql_only: 'GraphQL only · may be outside saved REST filter',
  rest_only: 'REST only',
  not_yet_evaluated: 'Not yet evaluated',
};

function ncentralDiagnosticLabel(value: string): string {
  return NCENTRAL_DIAGNOSTIC_REASON_LABELS[value] || words(value || 'unknown');
}

function ncentralDiagnosticColor(
  reason: string,
): 'success' | 'warning' | 'error' | 'info' | 'default' {
  if (reason === 'enriched') return 'success';
  if (['stale_cache_evidence', 'cache_unavailable', 'graphql_missing'].includes(reason)) {
    return 'warning';
  }
  if (['graphql_only', 'rest_only', 'not_yet_evaluated'].includes(reason)) return 'info';
  return 'default';
}

function ncentralFreshnessColor(
  status: string,
): 'success' | 'warning' | 'error' | 'info' | 'default' {
  if (status === 'fresh') return 'success';
  if (status === 'rest_only') return 'info';
  if (['partial', 'stale', 'scope_changed', 'incomplete'].includes(status)) return 'warning';
  if (['not_configured', 'server_missing'].includes(status)) return 'error';
  return 'default';
}

function ncentralDiagnosticFallbackAction(reason: string): string {
  const actions: Record<string, string> = {
    enriched: 'No enrichment action is required.',
    graphql_missing: 'Verify the GraphQL Customer and server scope, then refresh the full-scope cache.',
    stale_cache_evidence: 'Refresh the full-scope GraphQL cache before relying on this evidence.',
    cache_unavailable: 'Complete GraphQL setup or publish a current complete cache generation.',
    filtered_excluded: 'Review the saved device exclusions if this device should be managed.',
    ignored: 'Open Reconciliation ignored items to restore this device if it should be reviewed.',
    graphql_only: 'Run a reviewed REST preview to confirm this device is inside the saved REST filter.',
    rest_only: 'Enable GraphQL only if this customer needs richer inventory evidence.',
    not_yet_evaluated: 'Run a reviewed device preview after current enrichment evidence is available.',
  };
  return actions[reason] || 'Review this device and its persisted evidence.';
}

async function defaultLoadNcentralEnrichmentDiagnostics(
  request: NcentralEnrichmentDiagnosticsRequest,
  signal?: AbortSignal,
): Promise<NcentralEnrichmentDiagnostics> {
  const query = new URLSearchParams({
    companyId: request.companyId,
    providerCompanyId: request.providerCompanyId,
    limit: String(request.limit),
    offset: String(request.offset),
  });
  if (request.status) query.set('status', request.status);
  if (request.search) query.set('search', request.search);
  return apiFetch<NcentralEnrichmentDiagnostics>(
    `/api/integrations/ncentral/enrichment-diagnostics?${query.toString()}`,
    { signal },
  );
}

export function NcentralEnrichmentDiagnosticsWorkbench({
  companyId,
  providerCompanyId,
  customerName,
  refreshKey = '',
  onDiagnosticsChange,
  loadDiagnostics = defaultLoadNcentralEnrichmentDiagnostics,
}: {
  companyId: string;
  providerCompanyId: string;
  customerName: string;
  refreshKey?: string;
  onDiagnosticsChange?: (diagnostics: NcentralEnrichmentDiagnostics | null) => void;
  loadDiagnostics?: LoadNcentralEnrichmentDiagnostics;
}) {
  const pageSize = 50;
  const [diagnostics, setDiagnostics] = useState<NcentralEnrichmentDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [deviceStatusFilter, setDeviceStatusFilter] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [detailsExpanded, setDetailsExpanded] = useState(false);
  const requestNumber = useRef(0);
  const requestController = useRef<AbortController | null>(null);
  const scopeRef = useRef('');
  const appliedFilters = useRef({ status: '', search: '', offset: 0 });

  const runDiagnostics = useCallback(async (
    filters: { status: string; search: string; offset: number },
  ) => {
    if (!companyId || !providerCompanyId) return;
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    const currentRequest = ++requestNumber.current;
    const currentScope = `${companyId}:${providerCompanyId}`;
    setLoading(true);
    setLoadError('');
    try {
      const result = await loadDiagnostics({
        companyId,
        providerCompanyId,
        status: filters.status,
        search: filters.search.trim(),
        limit: pageSize,
        offset: filters.offset,
      }, controller.signal);
      if (controller.signal.aborted
        || currentRequest !== requestNumber.current
        || scopeRef.current !== currentScope) return;
      setDiagnostics(result);
      onDiagnosticsChange?.(result);
      setOffset(result.filters.offset);
      const availableDeviceStatuses = new Set(result.items.map(item => item.status).filter(Boolean));
      setDeviceStatusFilter(current => (
        current && !availableDeviceStatuses.has(current) ? '' : current
      ));
    } catch (value) {
      if (controller.signal.aborted || currentRequest !== requestNumber.current) return;
      setLoadError(errorMessage(value));
      setDetailsExpanded(true);
    } finally {
      if (currentRequest === requestNumber.current) setLoading(false);
    }
  }, [companyId, loadDiagnostics, onDiagnosticsChange, providerCompanyId]);

  useEffect(() => {
    const scope = `${companyId}:${providerCompanyId}`;
    if (scopeRef.current !== scope) {
      scopeRef.current = scope;
      appliedFilters.current = { status: '', search: '', offset: 0 };
      setDiagnostics(null);
      onDiagnosticsChange?.(null);
      setStatusFilter('');
      setDeviceStatusFilter('');
      setSearch('');
      setOffset(0);
      setDetailsExpanded(false);
    }
    void runDiagnostics(appliedFilters.current);
    return () => requestController.current?.abort();
  }, [companyId, onDiagnosticsChange, providerCompanyId, refreshKey, runDiagnostics]);

  const reasonOptions = useMemo(() => Object.entries(diagnostics?.summary.reasonCounts || {})
    .filter(([, count]) => count > 0)
    .map(([value, count]) => ({ value, count, label: ncentralDiagnosticLabel(value) })),
  [diagnostics]);
  const deviceStatusOptions = useMemo(() => Array.from(new Set(
    (diagnostics?.items || []).map(item => item.status).filter(Boolean),
  )).sort((left, right) => left.localeCompare(right)), [diagnostics]);
  const visibleItems = useMemo(() => (diagnostics?.items || []).filter(item => (
    !deviceStatusFilter || item.status === deviceStatusFilter
  )), [deviceStatusFilter, diagnostics]);
  const nextActions = useMemo(() => Object.entries(diagnostics?.summary.reasonCounts || {})
    .filter(([reason, count]) => count > 0 && reason !== 'enriched')
    .map(([reason, count]) => ({
      reason,
      count,
      action: diagnostics?.items.find(item => (
        item.reason === reason && item.nextAction?.label
      ))?.nextAction?.label || ncentralDiagnosticFallbackAction(reason),
    })), [diagnostics]);

  const applyFilters = () => {
    const filters = { status: statusFilter, search, offset: 0 };
    appliedFilters.current = filters;
    setOffset(0);
    void runDiagnostics(filters);
  };
  const clearFilters = () => {
    setStatusFilter('');
    setDeviceStatusFilter('');
    setSearch('');
    const filters = { status: '', search: '', offset: 0 };
    appliedFilters.current = filters;
    setOffset(0);
    void runDiagnostics(filters);
  };
  const movePage = (nextOffset: number) => {
    const filters = { ...appliedFilters.current, offset: nextOffset };
    appliedFilters.current = filters;
    setOffset(nextOffset);
    void runDiagnostics(filters);
  };

  return <Paper component="section" aria-labelledby="ncentral-enrichment-diagnostics-title"
    variant="outlined" sx={{ p: 2 }}>
    <Stack spacing={2}>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={1}
        sx={{ justifyContent: 'space-between', alignItems: { md: 'flex-start' } }}>
        <Box>
          <Typography variant="overline" color="primary">Persisted evidence</Typography>
          <Typography id="ncentral-enrichment-diagnostics-title" variant="h6">
            Enrichment diagnostics
          </Typography>
          <Typography id="ncentral-enrichment-diagnostics-safety" variant="body2"
            color="text.secondary">
            Customer-scoped to {customerName}. This reads stored CMDB and cache evidence only; it
            never calls N-central, refreshes the provider cache, starts a preview or imports data.
          </Typography>
        </Box>
        <Button variant="outlined" startIcon={<FactCheckOutlined />}
          aria-describedby="ncentral-enrichment-diagnostics-safety"
          disabled={loading}
          onClick={() => void runDiagnostics(appliedFilters.current)}>
          {loading ? 'Loading diagnostics…' : 'Reload diagnostics'}
        </Button>
      </Stack>

      {loading && !diagnostics && <Stack component="output" direction="row" spacing={1}
        aria-live="polite" sx={{ alignItems: 'center' }}>
        <CircularProgress size={20} />
        <Typography variant="body2">Loading stored enrichment evidence…</Typography>
      </Stack>}
      {loadError && <Alert severity="error" action={<Button color="inherit" size="small"
        onClick={() => void runDiagnostics(appliedFilters.current)}>Retry</Button>}>
        Diagnostics could not be loaded: {loadError}
      </Alert>}

      {diagnostics && <>
        <Grid container spacing={1.25} aria-label="Enrichment coverage summary">
          {([
            ['Known devices', diagnostics.summary.knownDevices],
            ['Eligible', diagnostics.coverage.eligibleDevices],
            ['Evaluated', diagnostics.coverage.evaluatedDevices],
            ['Enriched', diagnostics.coverage.enrichedDevices],
            ['Coverage', `${diagnostics.coverage.enrichedPercent}%`],
          ] as Array<[string, string | number]>).map(([label, value]) => <Grid
            key={label} size={{ xs: 6, sm: 4, lg: 2.4 }}>
            <Paper variant="outlined" sx={{ p: 1.25, height: '100%' }}>
              <Typography variant="h6">{value}</Typography>
              <Typography variant="caption" color="text.secondary">{label}</Typography>
            </Paper>
          </Grid>)}
        </Grid>

        <Stack spacing={1}>
          <Stack direction="row" spacing={0.75} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Chip size="small" color={ncentralFreshnessColor(diagnostics.freshness.status)}
              label={`Cache · ${ncentralDiagnosticLabel(diagnostics.freshness.status)}`} />
            <Chip size="small" variant="outlined"
              label={`Generated · ${readableTimestamp(diagnostics.generatedAt)}`} />
            {diagnostics.freshness.lastRefreshedAt && <Chip size="small" variant="outlined"
              label={`Cache observed · ${readableTimestamp(diagnostics.freshness.lastRefreshedAt)}`} />}
            <Chip size="small" variant="outlined"
              label={`Unmatched aggregate · ${diagnostics.freshness.unmatchedDeviceCount}`} />
            {diagnostics.latestPreview && <Chip size="small" variant="outlined"
              label={`Latest preview · ${ncentralDiagnosticLabel(diagnostics.latestPreview.status)}`} />}
          </Stack>
          <Alert severity={diagnostics.freshness.status === 'fresh' ? 'success'
            : ['rest_only', 'empty'].includes(diagnostics.freshness.status) ? 'info' : 'warning'}>
            {diagnostics.freshness.reason}
            {diagnostics.freshness.status === 'partial'
              && ' The published cache is usable, but coverage is incomplete; review aggregate unmatched identities.'}
          </Alert>
        </Stack>

        <Button size="small" variant="outlined" sx={{ alignSelf: 'flex-start' }}
          aria-expanded={detailsExpanded}
          aria-controls="ncentral-enrichment-diagnostic-details"
          onClick={() => setDetailsExpanded(current => !current)}>
          {detailsExpanded ? 'Hide device diagnostics' : 'Show device diagnostics'}
        </Button>

        {detailsExpanded && <Stack id="ncentral-enrichment-diagnostic-details" spacing={2}>
        {diagnostics.latestPreview && <Paper variant="outlined" sx={{ p: 1.5 }}>
          <Stack spacing={1}>
            <Typography variant="subtitle2">Latest preview evidence</Typography>
            <Stack direction="row" spacing={0.75} useFlexGap sx={{ flexWrap: 'wrap' }}>
              <Chip size="small" variant="outlined"
                label={`Discovered · ${diagnostics.latestPreview.discovered || 0}`} />
              <Chip size="small" variant="outlined"
                label={`Included · ${diagnostics.latestPreview.included || 0}`} />
              <Chip size="small" variant="outlined"
                label={`Excluded · ${diagnostics.latestPreview.excluded || 0}`} />
              {diagnostics.latestPreview.finishedAt && <Chip size="small" variant="outlined"
                label={`Finished · ${readableTimestamp(diagnostics.latestPreview.finishedAt)}`} />}
            </Stack>
            {Boolean(Object.keys(diagnostics.latestPreview.exclusionReasons || {}).length)
              && <Stack direction="row" spacing={0.75} useFlexGap sx={{ flexWrap: 'wrap' }}>
                <Typography variant="caption" color="text.secondary">Aggregate exclusions:</Typography>
                {Object.entries(diagnostics.latestPreview.exclusionReasons || {})
                  .map(([reason, count]) => <Chip key={reason} size="small" color="warning"
                    variant="outlined" label={`${ncentralDiagnosticLabel(reason)} · ${count}`} />)}
              </Stack>}
            <Typography variant="caption" color="text.secondary">
              These aggregates are the only persisted view of native/type/status filtering;
              per-device identities hidden by native filters are not available to diagnostics.
            </Typography>
          </Stack>
        </Paper>}

        <Stack component="form" direction={{ xs: 'column', lg: 'row' }} spacing={1}
          onSubmit={event => { event.preventDefault(); applyFilters(); }}
          aria-label="Enrichment diagnostic filters">
          <FormControl size="small" sx={{ minWidth: 210 }}>
            <InputLabel>Enrichment reason</InputLabel>
            <Select label="Enrichment reason" value={statusFilter}
              onChange={event => setStatusFilter(event.target.value)}>
              <MenuItem value=""><em>All reasons</em></MenuItem>
              {reasonOptions.map(option => <MenuItem key={option.value} value={option.value}>
                {option.label} ({option.count})
              </MenuItem>)}
            </Select>
          </FormControl>
          <FormControl size="small" sx={{ minWidth: 210 }} disabled={!deviceStatusOptions.length}>
            <InputLabel>N-central device status</InputLabel>
            <Select label="N-central device status" value={deviceStatusFilter}
              onChange={event => setDeviceStatusFilter(event.target.value)}>
              <MenuItem value=""><em>All loaded statuses</em></MenuItem>
              {deviceStatusOptions.map(status => <MenuItem key={status} value={status}>
                {status}
              </MenuItem>)}
            </Select>
            <FormHelperText>Filters the current bounded page.</FormHelperText>
          </FormControl>
          <TextField size="small" label="Search devices or assets" value={search}
            onChange={event => setSearch(event.target.value)} sx={{ flex: 1, minWidth: 220 }} />
          <Stack direction="row" spacing={1} sx={{ alignItems: 'flex-start' }}>
            <Button type="submit" variant="contained" disabled={loading}>Apply filters</Button>
            <Button type="button" color="inherit" disabled={loading
              || (!statusFilter && !deviceStatusFilter && !search)} onClick={clearFilters}>
              Clear
            </Button>
          </Stack>
        </Stack>

        <Box aria-live="polite">
          <Typography variant="body2" color="text.secondary">
            {diagnostics.total
              ? `Showing ${diagnostics.filters.offset + 1}–${diagnostics.filters.offset + diagnostics.items.length} of ${diagnostics.total} matching persisted device(s).`
              : 'No persisted devices match the current filters.'}
            {' '}Device-status filtering may reduce the visible rows on this bounded page.
          </Typography>
        </Box>

        {!diagnostics.items.length ? <Alert severity="info">
          {diagnostics.summary.knownDevices
            ? 'No diagnostics match the current enrichment reason or search. Clear the filters or review another page.'
            : 'No persisted N-central device evidence exists for this mapped customer yet. Run a reviewed device preview to establish evidence.'}
        </Alert> : !visibleItems.length ? <Alert severity="info">
          No rows on this bounded page match the selected N-central device status. Choose another
          status, clear the filter or move to another page.
        </Alert> : <TableContainer>
          <Typography id="ncentral-enrichment-diagnostics-table-description" variant="caption"
            color="text.secondary" component="p">
            Bounded, customer-scoped N-central enrichment evidence. No provider call is made.
          </Typography>
          <Table size="small" aria-label="Enrichment diagnostic rows"
            aria-describedby="ncentral-enrichment-diagnostics-table-description">
            <TableHead><TableRow>
              <TableCell>Device / asset</TableCell>
              <TableCell>N-central status</TableCell>
              <TableCell>Enrichment reason</TableCell>
              <TableCell>Persisted evidence</TableCell>
              <TableCell>Next action</TableCell>
            </TableRow></TableHead>
            <TableBody>{visibleItems.map(item => <TableRow key={item.externalId}>
              <TableCell><Typography sx={{ fontWeight: 750 }}>{item.name}</Typography>
                <Typography variant="caption" color="text.secondary">
                  {item.assetName || 'No canonical asset'} · ID {item.externalId}
                </Typography>
              </TableCell>
              <TableCell>{item.status || 'Not recorded'}</TableCell>
              <TableCell><Chip size="small" color={ncentralDiagnosticColor(item.reason)}
                variant={item.reason === 'enriched' ? 'filled' : 'outlined'}
                label={item.reasonLabel || ncentralDiagnosticLabel(item.reason)} /></TableCell>
              <TableCell><Typography variant="body2">{item.detail}</Typography>
                <Typography variant="caption" color="text.secondary">
                  Last REST evidence: {readableTimestamp(item.lastSeenAt)} · Cache evidence: {readableTimestamp(item.cacheObservedAt)}
                </Typography>
              </TableCell>
              <TableCell><Typography variant="body2">
                {item.nextAction?.label || ncentralDiagnosticFallbackAction(item.reason)}
              </Typography>
                {item.reviewAction && <Chip size="small" variant="outlined"
                  label={`Review queue · ${ncentralDiagnosticLabel(item.reviewAction)}`} sx={{ mt: 0.5 }} />}
              </TableCell>
            </TableRow>)}</TableBody>
          </Table>
        </TableContainer>}

        <Stack direction="row" spacing={1}
          sx={{ justifyContent: 'space-between', alignItems: 'center' }}>
          <Button size="small" disabled={loading || offset === 0}
            onClick={() => movePage(Math.max(0, offset - pageSize))}>Previous page</Button>
          <Typography variant="caption" color="text.secondary">
            Bounded to {diagnostics.filters.limit} rows per request
          </Typography>
          <Button size="small" disabled={loading
            || diagnostics.filters.offset + diagnostics.items.length >= diagnostics.total}
            onClick={() => movePage(offset + pageSize)}>Next page</Button>
        </Stack>

        <Box component="section" aria-labelledby="ncentral-enrichment-next-actions-title">
          <Typography id="ncentral-enrichment-next-actions-title" variant="subtitle2">
            Recommended next actions
          </Typography>
          {!nextActions.length ? <Typography variant="body2" color="success.main">
            No enrichment remediation is required for the current persisted evidence.
          </Typography> : <List dense aria-label="Recommended enrichment actions">
            {nextActions.map(item => <ListItem key={item.reason} disableGutters>
              <ListItemIcon sx={{ minWidth: 34 }}>
                <Chip size="small" label={item.count} color={ncentralDiagnosticColor(item.reason)} />
              </ListItemIcon>
              <ListItemText primary={item.action}
                secondary={ncentralDiagnosticLabel(item.reason)} />
            </ListItem>)}
          </List>}
        </Box>

        {Boolean(diagnostics.limitations?.length) && <Alert severity="info">
          <Typography sx={{ fontWeight: 750 }}>Evidence limits</Typography>
          <List dense aria-label="Enrichment diagnostic limitations" sx={{ py: 0 }}>
            {diagnostics.limitations?.map(limit => <ListItem key={limit} disableGutters
              sx={{ display: 'list-item', ml: 2, listStyleType: 'disc' }}>
              <ListItemText primary={limit} />
            </ListItem>)}
          </List>
        </Alert>}
        </Stack>}
      </>}
    </Stack>
  </Paper>;
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

type LoadNcentralLifecycleSummary = (
  request: { companyId: string; providerParentId: string },
  signal?: AbortSignal,
) => Promise<MissingDeviceLifecycleQueue>;

async function loadNcentralLifecycleSummary(
  request: { companyId: string; providerParentId: string },
  signal?: AbortSignal,
): Promise<MissingDeviceLifecycleQueue> {
  const params = new URLSearchParams({
    provider: 'ncentral',
    companyId: request.companyId,
    providerParentId: request.providerParentId,
    limit: '5',
    offset: '0',
  });
  return apiFetch<MissingDeviceLifecycleQueue>(
    `/api/integration-reconciliation/lifecycle-candidates?${params.toString()}`,
    { signal },
  );
}

export function NcentralMissingDeviceLifecycleSummary({
  companyId,
  providerParentId,
  customerName,
  refreshKey = '',
  onOpen,
  loadSummary = loadNcentralLifecycleSummary,
}: {
  companyId: string;
  providerParentId: string;
  customerName: string;
  refreshKey?: string;
  onOpen: () => void;
  loadSummary?: LoadNcentralLifecycleSummary;
}) {
  const [queue, setQueue] = useState<MissingDeviceLifecycleQueue | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setLoadError('');
    try {
      setQueue(await loadSummary({ companyId, providerParentId }, signal));
    } catch (cause) {
      if (!signal?.aborted) setLoadError(errorMessage(cause));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [companyId, loadSummary, providerParentId]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, refreshKey]);

  const actionable = (queue?.summary.eligible || 0) + (queue?.summary.restoreReady || 0);
  return <Paper component="section" aria-label="N-central missing-device lifecycle"
    variant="outlined" sx={{ p: 2 }}>
    <Stack spacing={1.5}>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={1}
        sx={{ alignItems: { md: 'center' }, justifyContent: 'space-between' }}>
        <Box><Typography variant="overline" color="primary">Source lifecycle</Typography>
          <Typography variant="h6">Missing-device evidence</Typography>
          <Typography variant="body2" color="text.secondary">
            Customer-scoped to {customerName}. Complete observations can flag an N-central source
            link for review; the canonical CI remains untouched.
          </Typography>
        </Box>
        <Button variant="outlined" onClick={onOpen} disabled={loading && !queue}>
          Review missing devices
        </Button>
      </Stack>
      {loadError && <Alert severity="warning">
        Missing-device lifecycle summary could not be loaded: {loadError}
      </Alert>}
      {loading && !queue ? <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <CircularProgress size={20} /><Typography variant="body2">Loading lifecycle evidence…</Typography>
      </Stack> : queue && <Stack direction="row" spacing={.75} useFlexGap sx={{ flexWrap: 'wrap' }}>
        <Chip size="small" variant="outlined" label={`Tracked · ${queue.summary.total}`} />
        <Chip size="small" color="success" variant="outlined"
          label={`Observed · ${queue.summary.observed}`} />
        <Chip size="small" color="info" label={`Monitoring · ${queue.summary.monitoring}`} />
        <Chip size="small" color={queue.summary.eligible ? 'warning' : 'default'}
          label={`Ready to retire source · ${queue.summary.eligible}`} />
        <Chip size="small" color={queue.summary.restoreReady ? 'success' : 'default'}
          label={`Ready to restore source · ${queue.summary.restoreReady}`} />
        <Chip size="small" variant="outlined"
          label={`Not evaluated · ${queue.summary.notEvaluated}`} />
        <Chip size="small" variant="outlined"
          label={`Sources retired · ${queue.summary.retired}`} />
        {actionable > 0 && <Chip size="small" color="warning"
          label={`${actionable} administrator decision${actionable === 1 ? '' : 's'}`} />}
      </Stack>}
    </Stack>
  </Paper>;
}

export function NcentralIntegrationPage() {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const isAdmin = getSession()?.user.role === 'platform_admin';
  const [connection, setConnection] = useState<NcentralConnection | null>(null);
  const [graphqlConfig, setGraphqlConfig] = useState<NcentralGraphqlConfig | null>(null);
  const [graphqlConfigError, setGraphqlConfigError] = useState('');
  const [graphqlTestResult, setGraphqlTestResult] = useState<NcentralGraphqlTestResult | null>(null);
  const [manifest, setManifest] = useState<IntegrationProviderManifest | null>(null);
  const [setupStatus, setSetupStatus] = useState<NcentralSetupStatus | null>(null);
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
  const [diagnosticsRevision, setDiagnosticsRevision] = useState(0);
  const [scopedDiagnostics, setScopedDiagnostics]
    = useState<NcentralEnrichmentDiagnostics | null>(null);
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
      if (isAdmin) {
        void apiFetch<NcentralSetupStatus>('/api/integrations/ncentral/setup-status')
          .then(setSetupStatus)
          .catch(() => setSetupStatus(null));
      }
    } catch (value) {
      setError(errorMessage(value));
    }
  }, [isAdmin]);

  const loadGraphqlConfig = useCallback(async () => {
    setGraphqlConfigError('');
    try {
      setGraphqlConfig(await apiFetch<NcentralGraphqlConfig>(
        '/api/integrations/ncentral/graphql/config',
      ));
    } catch (value) {
      // GraphQL is an optional side channel. Its route or configuration may be
      // unavailable without taking the established REST wizard offline.
      setGraphqlConfigError(errorMessage(value));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { void loadGraphqlConfig(); }, [loadGraphqlConfig]);

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
      setGraphqlConfig(current => syncNcentralSharedRevision(current, stored.revision));
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
      setOptions(cached); setPolicy(withNcentralLifecyclePolicyDefaults(cached.policy));
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
      const normalizedResult = {
        ...result,
        policy: withNcentralLifecyclePolicyDefaults(result.policy),
      };
      optionsCache.current.set(scope, normalizedResult);
      if (requestNumber === optionsRequest.current && previewScope.current === scope) {
        setOptions(normalizedResult); setPolicy(normalizedResult.policy);
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
            relationshipAutomationMode: policy.relationshipAutomationMode || 'review',
            relationshipAutoApproveTypes: policy.relationshipAutoApproveTypes || [],
            relationshipMinConfidence: policy.relationshipMinConfidence ?? 0.98,
            relationshipMinObservations: policy.relationshipMinObservations ?? 2,
            relationshipMaxEvidenceAgeHours: policy.relationshipMaxEvidenceAgeHours ?? 72,
            missingDeviceRequiredSnapshots: Math.min(
              10, Math.max(2, policy.missingDeviceRequiredSnapshots ?? 3),
            ),
            missingDeviceMinimumHours: Math.min(
              720, Math.max(1, policy.missingDeviceMinimumHours ?? 24),
            ),
            graphqlOrganizationIds: policy.graphqlOrganizationIds || [],
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
      const normalized = withNcentralLifecyclePolicyDefaults(stored);
      setPolicy(normalized);
      if (previewScopeKey) {
        const cached = optionsCache.current.get(previewScopeKey);
        if (cached) optionsCache.current.set(previewScopeKey, { ...cached, policy: normalized });
      }
      if (showNotice) setNotice({
        severity: 'success',
        message: 'Device filter, type mappings and preview schedule saved with an audit revision.',
      });
      return normalized;
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
      setDiagnosticsRevision(current => current + 1);
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
      setDiagnosticsRevision(current => current + 1);
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
          {setupStatus && <Paper component="section" aria-label="N-central setup readiness"
            variant="outlined" sx={{ p: 1.5, mb: 3 }}>
            <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5}
              sx={{ alignItems: { md: 'center' }, justifyContent: 'space-between' }}>
              <Box>
                <Typography variant="subtitle2">Setup readiness</Typography>
                <Typography variant="body2" color="text.secondary">
                  Next: {setupStatus.recommendedNextAction.label}
                </Typography>
              </Box>
              <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
                <Chip size="small"
                  color={setupStatus.overallStatus === 'ready'
                    ? 'success'
                    : setupStatus.overallStatus === 'blocked' ? 'error' : 'warning'}
                  label={words(setupStatus.overallStatus)} />
                {setupStatus.blockers.length > 0 && <Chip size="small" variant="outlined"
                  color="error" label={`${setupStatus.blockers.length} blocker(s)`} />}
                {setupStatus.warnings.length > 0 && <Chip size="small" variant="outlined"
                  color="warning" label={`${setupStatus.warnings.length} warning(s)`} />}
              </Stack>
            </Stack>
          </Paper>}
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
            {graphqlConfig && <NcentralGraphqlConnectionPanel
              config={graphqlConfig}
              restReady={connectionReady}
              isAdmin={isAdmin}
              disabled={connection.lifecycleStatus !== 'active'}
              onConfigChange={stored => {
                setGraphqlConfig(stored);
                setConnection(current => syncNcentralSharedRevision(current, stored.revision));
                setGraphqlTestResult(null);
              }}
              onTestResult={setGraphqlTestResult}
            />}
            {!graphqlConfig && graphqlConfigError && <Alert severity="warning">
              Optional GraphQL enrichment is unavailable ({graphqlConfigError}). The REST
              connection, customer discovery, and reconciliation workflow remain available.
            </Alert>}
            {!graphqlConfig && !graphqlConfigError && <Paper variant="outlined" sx={{ p: 2 }}>
              <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
                <CircularProgress size={20} />
                <Typography variant="body2">Loading optional GraphQL status…</Typography>
              </Stack>
            </Paper>}
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
            <NcentralDeviceWorkflowReadiness
              mappedOrganizationCount={mappedOrganizations.length}
              customerSelected={Boolean(selectedMapping)}
              optionsLoaded={Boolean(options)}
              optionsLoading={busy === 'options'}
              graphqlConfig={graphqlConfig}
              scopedDiagnostics={scopedDiagnostics}
              policySaved={Boolean(policy.id)}
              previewRunStatus={scopedPreviewRun?.status}
              previewAvailable={Boolean(preview)}
              reviewableCount={reviewableItems.length}
              conflictCount={preview?.counts.conflict || 0}
              selectedCount={selectedDeviceIds.length}
            />
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
                      setScopedDiagnostics(null);
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
            {selectedMapping && <NcentralCapabilityCheck
              key={previewScopeKey}
              companyId={selectedCompanyId}
              providerCompanyId={selectedOrganizationId}
              providerCompanyName={selectedMapping.name}
              deviceChoices={(preview?.items || []).map(item => ({
                externalId: item.externalId,
                name: item.name,
              }))}
              disabled={!connectionReady || previewRunActive}
            />}
            {selectedMapping && options && graphqlConfig && <NcentralGraphqlCustomerEnrichmentPanel
              key={`graphql:${previewScopeKey}`}
              config={graphqlConfig}
              companyId={selectedCompanyId}
              providerCompanyId={selectedOrganizationId}
              providerCompanyName={selectedMapping.name}
              organizationIds={policy.graphqlOrganizationIds || []}
              customerCandidates={graphqlTestResult?.customerCandidates || []}
              isAdmin={isAdmin}
              disabled={previewRunActive}
              scopedDiagnostics={scopedDiagnostics}
              onOrganizationIdsChange={ids => updatePolicy({ graphqlOrganizationIds: ids })}
              persistScope={async () => {
                if (!isAdmin) return Boolean(policy.id && policy.graphqlOrganizationIds?.length);
                return Boolean(await savePolicy(false));
              }}
              onConfigChange={stored => {
                setGraphqlConfig(stored);
                setConnection(current => syncNcentralSharedRevision(current, stored.revision));
              }}
            />}
            {selectedMapping && <NcentralEnrichmentDiagnosticsWorkbench
              key={`diagnostics:${previewScopeKey}`}
              companyId={selectedCompanyId}
              providerCompanyId={selectedOrganizationId}
              customerName={selectedMapping.mappedCompanyName || selectedMapping.name}
              onDiagnosticsChange={setScopedDiagnostics}
              refreshKey={[
                scopedPreviewRun?.finishedAt || '',
                preview?.syncRunId || '',
                diagnosticsRevision,
              ].join(':')}
            />}
            {selectedMapping && <NcentralMissingDeviceLifecycleSummary
              key={`missing-lifecycle:${previewScopeKey}`}
              companyId={selectedCompanyId}
              providerParentId={selectedOrganizationId}
              customerName={selectedMapping.mappedCompanyName || selectedMapping.name}
              refreshKey={[
                scopedPreviewRun?.finishedAt || '',
                preview?.syncRunId || '',
                diagnosticsRevision,
              ].join(':')}
              onOpen={() => navigate(
                `/admin/reconciliation?tab=missing&companyId=${encodeURIComponent(selectedCompanyId)}`
                + `&provider=ncentral&providerParentId=${encodeURIComponent(selectedOrganizationId)}`,
              )}
            />}
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
                  <InputLabel>REST detail enrichment</InputLabel>
                  <Select label="REST detail enrichment" value={policy.enrichmentMode || 'balanced'}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({
                      enrichmentMode: event.target.value as NonNullable<ConnectWiseCiPolicy['enrichmentMode']>,
                    })}>
                    <MenuItem value="fast">Fast · no device detail calls</MenuItem>
                    <MenuItem value="balanced">Balanced · enrich up to 25 devices</MenuItem>
                    <MenuItem value="full">Full · enrich up to 250 devices</MenuItem>
                  </Select>
                  <FormHelperText>
                    Controls per-device REST detail reads only. The separate GraphQL cache has its own scope and refresh.
                  </FormHelperText>
                </FormControl></Grid>
                <Grid size={{ xs: 12 }}><Paper variant="outlined" sx={{ p: 1.5 }}>
                  <Typography variant="subtitle2">Missing-device evidence threshold</Typography>
                  <Typography variant="body2" color="text.secondary">
                    A source identity becomes reviewable only after complete, in-scope observations
                    meet both thresholds. The app never deactivates a source link automatically,
                    and the canonical CI remains untouched.
                  </Typography>
                </Paper></Grid>
                <Grid size={{ xs: 12, md: 3 }}><TextField fullWidth type="number"
                  label="Required complete snapshots"
                  value={policy.missingDeviceRequiredSnapshots ?? 3}
                  disabled={previewRunActive}
                  onChange={event => updatePolicy({
                    missingDeviceRequiredSnapshots: Number(event.target.value),
                  })}
                  slotProps={{ htmlInput: { min: 2, max: 10, step: 1 } }}
                  helperText="2–10 complete observations; default 3"
                /></Grid>
                <Grid size={{ xs: 12, md: 3 }}><TextField fullWidth type="number"
                  label="Minimum missing time (hours)"
                  value={policy.missingDeviceMinimumHours ?? 24}
                  disabled={previewRunActive}
                  onChange={event => updatePolicy({
                    missingDeviceMinimumHours: Number(event.target.value),
                  })}
                  slotProps={{ htmlInput: { min: 1, max: 720, step: 1 } }}
                  helperText="1–720 hours; default 24"
                /></Grid>
                <Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth>
                  <InputLabel>Relationship automation</InputLabel>
                  <Select label="Relationship automation"
                    value={policy.relationshipAutomationMode || 'review'}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({
                      relationshipAutomationMode: event.target.value as NonNullable<ConnectWiseCiPolicy['relationshipAutomationMode']>,
                    })}>
                    <MenuItem value="review">Review every suggestion</MenuItem>
                    <MenuItem value="auto_explicit">Auto-approve explicit provider identities</MenuItem>
                  </Select>
                  <FormHelperText>
                    Auto-approval never uses names, sites, gateways or subnet proximity.
                  </FormHelperText>
                </FormControl></Grid>
                {policy.relationshipAutomationMode === 'auto_explicit' && <>
                  <Grid size={{ xs: 12 }}>
                    <Alert severity="warning">
                      This can add CMDB impact relationships after repeated fresh observations. It never writes to N-central or changes manually maintained links.
                    </Alert>
                  </Grid>
                  <Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth>
                    <InputLabel>Automatically approved relationship types</InputLabel>
                    <Select multiple label="Automatically approved relationship types"
                      value={policy.relationshipAutoApproveTypes || []}
                      disabled={previewRunActive}
                      onChange={event => updatePolicy({
                        relationshipAutoApproveTypes: event.target.value as string[],
                      })}>
                      {[
                        { value: 'hosts', label: 'Hypervisor hosts virtual machine' },
                        { value: 'member_of', label: 'Cluster membership' },
                        { value: 'stored_on', label: 'Virtual machine stored on datastore' },
                      ].map(item => <MenuItem key={item.value} value={item.value}>
                        <Checkbox checked={(policy.relationshipAutoApproveTypes || []).includes(item.value)} />
                        <ListItemText primary={item.label} />
                      </MenuItem>)}
                    </Select>
                    <FormHelperText>An empty selection keeps every suggestion in review.</FormHelperText>
                  </FormControl></Grid>
                  <Grid size={{ xs: 12, sm: 4, md: 2 }}><TextField fullWidth type="number"
                    label="Minimum confidence" value={policy.relationshipMinConfidence ?? 0.98}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({ relationshipMinConfidence: Number(event.target.value) })}
                    slotProps={{ htmlInput: { min: 0.5, max: 1, step: 0.01 } }}
                  /></Grid>
                  <Grid size={{ xs: 12, sm: 4, md: 2 }}><TextField fullWidth type="number"
                    label="Observations" value={policy.relationshipMinObservations ?? 2}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({ relationshipMinObservations: Number(event.target.value) })}
                    slotProps={{ htmlInput: { min: 2, max: 10, step: 1 } }}
                  /></Grid>
                  <Grid size={{ xs: 12, sm: 4, md: 2 }}><TextField fullWidth type="number"
                    label="Max age (hours)" value={policy.relationshipMaxEvidenceAgeHours ?? 72}
                    disabled={previewRunActive}
                    onChange={event => updatePolicy({ relationshipMaxEvidenceAgeHours: Number(event.target.value) })}
                    slotProps={{ htmlInput: { min: 1, max: 720, step: 1 } }}
                  /></Grid>
                </>}
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
              <Alert severity="info">
                {ncentralRestDetailEnrichmentMessage(
                  policy.enrichmentMode || 'balanced',
                  graphqlConfig,
                )}
              </Alert>
            </Stack>}
          </Stack>}

          <Stack direction="row" spacing={1} sx={{ justifyContent: 'space-between', mt: 4 }}>
            <Button disabled={activeStep === 0} onClick={() => setActiveStep(step => step - 1)}>Back</Button>
            <Button variant="contained" disabled={ncentralWizardContinueDisabled({
              activeStep,
              lastStep: steps.length - 1,
              connectionReady,
              mappedOrganizationCount: mappedOrganizations.length,
            })} onClick={() => setActiveStep(step => step + 1)}>Continue</Button>
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
