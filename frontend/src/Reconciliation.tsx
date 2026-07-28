import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import CompareArrowsOutlined from '@mui/icons-material/CompareArrowsOutlined';
import DeleteOutlined from '@mui/icons-material/DeleteOutlined';
import LinkOutlined from '@mui/icons-material/LinkOutlined';
import RestoreOutlined from '@mui/icons-material/RestoreOutlined';
import RuleOutlined from '@mui/icons-material/RuleOutlined';
import VisibilityOffOutlined from '@mui/icons-material/VisibilityOffOutlined';
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Checkbox, Chip, CircularProgress,
  Dialog, DialogActions, DialogContent, DialogTitle, FormControl, Grid, InputLabel,
  MenuItem, Paper, Select, Stack, Tab, Table, TableBody, TableCell, TableContainer,
  TableHead, TablePagination, TableRow, Tabs, TextField, Typography,
} from '@mui/material';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Navigate } from 'react-router';
import { apiFetch, getSession } from './session';
import { Title } from './ui';
import type {
  Asset, CiReviewItem, FieldAuthorityCatalogue, FieldAuthorityRule,
  IntegrationObjectSuppression, IntegrationObjectSuppressionQueue,
  IntegrationReconciliationQueue,
} from './types';
import { useWorkspace } from './workspace';

const emptyQueue: IntegrationReconciliationQueue = {
  items: [], total: 0, summary: { create: 0, update: 0, link: 0, conflict: 0 },
};
const emptySuppressions: IntegrationObjectSuppressionQueue = { items: [], total: 0 };
const words = (value: string) => value.replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase());
const jsonValue = (value: unknown) => value == null || value === '' ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);

function valueAt(source: Record<string, unknown> | Asset | null, path: string): unknown {
  if (!source) return undefined;
  return path.split('.').reduce<unknown>((value, key) => {
    if (!value || typeof value !== 'object') return undefined;
    return (value as Record<string, unknown>)[key];
  }, source);
}

function Metric({ label, value, color }: { label: string; value: number; color: string }) {
  return <Card sx={{ height: '100%', borderTop: `3px solid ${color}` }}><CardContent>
    <Typography variant="overline" color="text.secondary">{label}</Typography>
    <Typography variant="h3" sx={{ mt: .5 }}>{value}</Typography>
  </CardContent></Card>;
}

export function ReconciliationPage() {
  const workspace = useWorkspace();
  const role = getSession()?.user.role;
  const canManage = role === 'platform_admin';
  const [tab, setTab] = useState(0);
  const [queue, setQueue] = useState(emptyQueue);
  const [suppressions, setSuppressions] = useState(emptySuppressions);
  const [catalogue, setCatalogue] = useState<FieldAuthorityCatalogue | null>(null);
  const [rules, setRules] = useState<FieldAuthorityRule[]>([]);
  const [companyId, setCompanyId] = useState('');
  const [provider, setProvider] = useState('');
  const [action, setAction] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(0);
  const [suppressionPage, setSuppressionPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [selected, setSelected] = useState<string[]>([]);
  const [detail, setDetail] = useState<CiReviewItem | null>(null);
  const [detailAsset, setDetailAsset] = useState<Asset | null>(null);
  const [bulkAction, setBulkAction] = useState<'import' | 'dismiss' | 'ignore' | null>(null);
  const [decisionNotes, setDecisionNotes] = useState('');
  const [restoreItem, setRestoreItem] = useState<IntegrationObjectSuppression | null>(null);
  const [linkItem, setLinkItem] = useState<CiReviewItem | null>(null);
  const [linkAssets, setLinkAssets] = useState<Asset[]>([]);
  const [linkAsset, setLinkAsset] = useState<Asset | null>(null);
  const [authority, setAuthority] = useState<FieldAuthorityRule | null>(null);
  const [presetKey, setPresetKey] = useState('');
  const [presetCompanyId, setPresetCompanyId] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const companyNames = useMemo(
    () => new Map(workspace.companies.map(item => [item.id, item.name])),
    [workspace.companies],
  );
  const selectedItems = useMemo(
    () => queue.items.filter(item => selected.includes(item.id)),
    [queue.items, selected],
  );
  const importableSelection = selectedItems.length > 0
    && selectedItems.every(item => item.provider === 'connectwise' && item.action !== 'conflict')
    && new Set(selectedItems.map(item => item.policyId)).size === 1;
  const ignorableSelection = selectedItems.length > 0
    && new Set(selectedItems.map(item => item.policyId)).size === 1;
  const displayedRules = useMemo(
    () => rules.filter(rule => !companyId || rule.companyId === companyId),
    [rules, companyId],
  );

  const loadReferences = useCallback(async () => {
    try {
      const [authorityCatalogue, storedRules] = await Promise.all([
        apiFetch<FieldAuthorityCatalogue>('/api/field-authority/catalogue'),
        apiFetch<FieldAuthorityRule[]>('/api/field-authority'),
      ]);
      setCatalogue(authorityCatalogue);
      setRules(storedRules);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Reconciliation controls could not be loaded.');
    }
  }, []);

  const loadQueue = useCallback(async () => {
    setBusy('queue'); setError('');
    try {
      const params = new URLSearchParams({
        state: 'pending', limit: String(rowsPerPage), offset: String(page * rowsPerPage),
      });
      if (companyId) params.set('companyId', companyId);
      if (provider) params.set('provider', provider);
      if (action) params.set('action', action);
      if (search.trim()) params.set('search', search.trim());
      setQueue(await apiFetch<IntegrationReconciliationQueue>(`/api/integration-reconciliation?${params}`));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The reconciliation queue could not be loaded.');
    } finally {
      setBusy('');
    }
  }, [action, companyId, page, provider, rowsPerPage, search]);

  const loadSuppressions = useCallback(async () => {
    setBusy('suppressions'); setError('');
    try {
      const params = new URLSearchParams({
        active: 'true',
        limit: String(rowsPerPage),
        offset: String(suppressionPage * rowsPerPage),
      });
      if (companyId) params.set('companyId', companyId);
      if (provider) params.set('provider', provider);
      if (search.trim()) params.set('search', search.trim());
      setSuppressions(
        await apiFetch<IntegrationObjectSuppressionQueue>(
          `/api/integration-reconciliation/ignored?${params}`,
        ),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Ignored configurations could not be loaded.');
    } finally {
      setBusy('');
    }
  }, [companyId, provider, rowsPerPage, search, suppressionPage]);

  useEffect(() => { void loadReferences(); }, [loadReferences]);
  useEffect(() => { void loadQueue(); }, [loadQueue]);
  useEffect(() => {
    if (tab === 1) void loadSuppressions();
  }, [tab, loadSuppressions]);

  async function openDetail(item: CiReviewItem) {
    setDetail(item); setDetailAsset(null); setError('');
    if (!item.assetId) return;
    try { setDetailAsset(await apiFetch<Asset>(`/api/assets/${encodeURIComponent(item.assetId)}`)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'The canonical CI could not be loaded.'); }
  }

  async function runBulkAction() {
    if (!bulkAction || decisionNotes.trim().length < 4 || !selectedItems.length) return;
    setBusy(`bulk-${bulkAction}`); setError(''); setNotice('');
    try {
      if (bulkAction === 'dismiss') {
        await apiFetch('/api/integration-reconciliation/dismiss', {
          method: 'POST', body: JSON.stringify({ itemIds: selectedItems.map(item => item.id), notes: decisionNotes.trim() }),
        });
        setNotice(`${selectedItems.length} review observation(s) were dismissed with audit notes.`);
      } else if (bulkAction === 'ignore') {
        if (!ignorableSelection) throw new Error('Ignore selections must belong to one customer integration policy.');
        await apiFetch('/api/integration-reconciliation/ignore', {
          method: 'POST', body: JSON.stringify({ itemIds: selectedItems.map(item => item.id), notes: decisionNotes.trim() }),
        });
        setNotice(`${selectedItems.length} provider configuration(s) will remain ignored until restored.`);
      } else {
        if (!importableSelection) throw new Error('Import selections must be non-conflicting ConnectWise items from one customer policy.');
        const first = selectedItems[0];
        await apiFetch('/api/integrations/connectwise/configurations/import', {
          method: 'POST',
          body: JSON.stringify({
            companyId: first.companyId,
            providerCompanyId: first.providerParentId,
            externalIds: selectedItems.map(item => item.externalId),
            decisionNotes: decisionNotes.trim(),
          }),
        });
        setNotice(`${selectedItems.length} reviewed ConnectWise item(s) were applied to the canonical CMDB.`);
      }
      setSelected([]); setBulkAction(null); setDecisionNotes('');
      await Promise.all([loadQueue(), loadSuppressions()]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The reviewed action could not be completed.');
    } finally { setBusy(''); }
  }

  async function restoreSuppression() {
    if (!restoreItem || decisionNotes.trim().length < 4) return;
    setBusy('restore'); setError(''); setNotice('');
    try {
      await apiFetch(
        `/api/integration-reconciliation/ignored/${encodeURIComponent(restoreItem.id)}/restore`,
        { method: 'POST', body: JSON.stringify({ notes: decisionNotes.trim() }) },
      );
      setNotice(`${restoreItem.externalName} can enter reconciliation again.`);
      setRestoreItem(null); setDecisionNotes('');
      await loadSuppressions();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The ignored configuration could not be restored.');
    } finally { setBusy(''); }
  }

  async function openLink(item: CiReviewItem) {
    setBusy('link-assets'); setError(''); setLinkAsset(null);
    try {
      setLinkAssets(await apiFetch<Asset[]>(`/api/assets?companyId=${encodeURIComponent(item.companyId)}`));
      setLinkItem(item);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Customer CIs could not be loaded.');
    } finally { setBusy(''); }
  }

  async function saveLink() {
    if (!linkItem || !linkAsset || linkItem.provider !== 'connectwise') return;
    setBusy('link'); setError('');
    try {
      await apiFetch('/api/integrations/connectwise/configurations/link', {
        method: 'POST',
        body: JSON.stringify({
          companyId: linkItem.companyId,
          providerCompanyId: linkItem.providerParentId,
          externalId: linkItem.externalId,
          assetId: linkAsset.id,
        }),
      });
      setNotice(`${linkItem.externalName} was linked to ${linkAsset.name} by immutable provider ID.`);
      setLinkItem(null); setLinkAsset(null); await loadQueue();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The identity link could not be recorded.');
    } finally { setBusy(''); }
  }

  async function saveAuthority() {
    if (!authority) return;
    setBusy('authority'); setError('');
    try {
      await apiFetch('/api/field-authority', { method: 'PUT', body: JSON.stringify(authority) });
      setAuthority(null); setNotice('Field authority was saved and will be enforced by future previews and imports.');
      await Promise.all([loadReferences(), loadQueue()]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Field authority could not be saved.');
    } finally { setBusy(''); }
  }

  async function deleteAuthority(rule: FieldAuthorityRule) {
    const params = new URLSearchParams({
      companyId: rule.companyId, ciType: rule.ciType, fieldName: rule.fieldName, provider: rule.provider,
    });
    setBusy('authority'); setError('');
    try {
      await apiFetch(`/api/field-authority?${params}`, { method: 'DELETE' });
      setNotice('Field authority rule deleted.'); await Promise.all([loadReferences(), loadQueue()]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Field authority could not be deleted.');
    } finally { setBusy(''); }
  }

  async function applyPreset() {
    if (!presetKey || !presetCompanyId) return;
    setBusy('preset'); setError('');
    try {
      const result = await apiFetch<{ applied: number }>('/api/field-authority/presets', {
        method: 'POST', body: JSON.stringify({ companyId: presetCompanyId, presetKey }),
      });
      setNotice(`${result.applied} audited field-authority rules were applied.`);
      setPresetKey(''); await Promise.all([loadReferences(), loadQueue()]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The authority preset could not be applied.');
    } finally { setBusy(''); }
  }

  if (!workspace.isRoot || !['platform_admin', 'msp_operator'].includes(role || '')) return <Navigate to="/" replace />;

  const currentPageIds = queue.items.map(item => item.id);
  const pageSelected = currentPageIds.length > 0 && currentPageIds.every(id => selected.includes(id));
  const comparisonFields = detail
    ? [...new Set([...(detail.changedFields || []), ...(detail.blockedFields || [])])]
    : [];

  return <Box className="governance-page"><Title title="Reconciliation" />
    <Stack direction="row" spacing={2} sx={{ alignItems: 'flex-start', mb: 3 }}>
      <Box className="governance-heading-icon"><CompareArrowsOutlined /></Box>
      <Box><Typography variant="overline" color="primary">Integration governance</Typography>
        <Typography variant="h3">Reconciliation workbench</Typography>
        <Typography color="text.secondary" sx={{ mt: .75, maxWidth: 900 }}>
          Resolve provider identities, review proposed canonical changes and define which source owns each field. Names never create an automatic identity match.
        </Typography></Box>
    </Stack>
    {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
    {notice && <Alert severity="success" sx={{ mb: 2 }} onClose={() => setNotice('')}>{notice}</Alert>}
    <Paper className="quality-workbench">
      <Tabs value={tab} onChange={(_, value) => { setTab(value); setSelected([]); }}>
        <Tab icon={<CompareArrowsOutlined />} iconPosition="start" label={`Review queue (${queue.total})`} />
        <Tab icon={<VisibilityOffOutlined />} iconPosition="start" label={`Ignored (${suppressions.total})`} />
        <Tab icon={<RuleOutlined />} iconPosition="start" label={`Field authority (${displayedRules.length})`} />
      </Tabs>
      {tab === 0 && <>
        <Grid container spacing={2} sx={{ p: 2, pb: 1 }}>
          <Grid size={{ xs: 6, md: 3 }}><Metric label="New CIs" value={queue.summary.create} color="#50d5b9" /></Grid>
          <Grid size={{ xs: 6, md: 3 }}><Metric label="Updates" value={queue.summary.update} color="#f1d372" /></Grid>
          <Grid size={{ xs: 6, md: 3 }}><Metric label="Identity links" value={queue.summary.link} color="#7997ff" /></Grid>
          <Grid size={{ xs: 6, md: 3 }}><Metric label="Conflicts" value={queue.summary.conflict} color="#ef7f8d" /></Grid>
        </Grid>
        <Box className="reconciliation-filter">
          <TextField size="small" label="Search provider CI, ID or canonical target" value={search} onChange={event => { setSearch(event.target.value); setPage(0); setSelected([]); }} />
          <FormControl size="small"><InputLabel>Customer</InputLabel><Select label="Customer" value={companyId} onChange={event => { setCompanyId(event.target.value); setPage(0); setSelected([]); }}><MenuItem value="">All customers</MenuItem>{workspace.companies.map(item => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</Select></FormControl>
          <FormControl size="small"><InputLabel>Provider</InputLabel><Select label="Provider" value={provider} onChange={event => { setProvider(event.target.value); setPage(0); setSelected([]); }}><MenuItem value="">All providers</MenuItem>{catalogue?.providers.filter(item => item.key !== 'cmdb').map(item => <MenuItem key={item.key} value={item.key}>{item.name}</MenuItem>)}</Select></FormControl>
          <FormControl size="small"><InputLabel>Decision</InputLabel><Select label="Decision" value={action} onChange={event => { setAction(event.target.value); setPage(0); setSelected([]); }}><MenuItem value="">All decisions</MenuItem>{['create', 'update', 'link', 'conflict'].map(value => <MenuItem key={value} value={value}>{words(value)}</MenuItem>)}</Select></FormControl>
          <Button variant="outlined" disabled={!selected.length || !canManage} onClick={() => { setBulkAction('dismiss'); setDecisionNotes(''); }}>Dismiss selected</Button>
          <Button variant="outlined" color="warning" startIcon={<VisibilityOffOutlined />} disabled={!ignorableSelection || !canManage} onClick={() => { setBulkAction('ignore'); setDecisionNotes(''); }}>Ignore selected</Button>
          <Button variant="contained" disabled={!importableSelection || !canManage} onClick={() => { setBulkAction('import'); setDecisionNotes(''); }}>Import selected</Button>
        </Box>
        {busy === 'queue' && !queue.items.length ? <Stack sx={{ p: 5, alignItems: 'center' }} spacing={2}><CircularProgress /><Typography>Loading review evidence…</Typography></Stack> : <TableContainer><Table size="small" aria-label="Integration reconciliation queue"><TableHead><TableRow>
          <TableCell padding="checkbox"><Checkbox slotProps={{ input: { 'aria-label': 'Select current page' } }} checked={pageSelected} indeterminate={selected.some(id => currentPageIds.includes(id)) && !pageSelected} onChange={event => setSelected(current => event.target.checked ? [...new Set([...current, ...currentPageIds])] : current.filter(id => !currentPageIds.includes(id)))} /></TableCell>
          <TableCell>Customer</TableCell><TableCell>Provider record</TableCell><TableCell>Decision</TableCell><TableCell>Canonical target</TableCell><TableCell>Field control</TableCell><TableCell>Observed</TableCell><TableCell />
        </TableRow></TableHead><TableBody>{queue.items.map(item => <TableRow key={item.id} hover selected={selected.includes(item.id)}>
          <TableCell padding="checkbox"><Checkbox slotProps={{ input: { 'aria-label': `Select ${item.externalName}` } }} checked={selected.includes(item.id)} onChange={event => setSelected(current => event.target.checked ? [...current, item.id] : current.filter(id => id !== item.id))} /></TableCell>
          <TableCell>{item.companyName || companyNames.get(item.companyId) || item.companyId}</TableCell>
          <TableCell><Typography sx={{ fontWeight: 800 }}>{item.externalName}</Typography><Typography variant="caption" color="text.secondary">{words(item.provider)} · {item.providerTypeName || 'Unclassified'} · ID {item.externalId}</Typography></TableCell>
          <TableCell><Chip size="small" label={words(item.action)} color={item.action === 'conflict' ? 'error' : item.action === 'update' ? 'warning' : item.action === 'link' ? 'info' : 'success'} /><Typography variant="caption" sx={{ display: 'block', mt: .5, maxWidth: 250 }}>{item.reason}</Typography></TableCell>
          <TableCell>{item.assetName || 'New canonical CI'}</TableCell>
          <TableCell><Stack direction="row" spacing={.5} useFlexGap sx={{ flexWrap: 'wrap' }}>{item.changedFields?.slice(0, 3).map(field => <Chip key={field} size="small" variant="outlined" color="success" label={field} />)}{item.blockedFields?.slice(0, 2).map(field => <Chip key={field} size="small" variant="outlined" color="warning" label={`${field} protected`} />)}{!item.changedFields?.length && !item.blockedFields?.length && <Typography variant="caption" color="text.secondary">Identity decision only</Typography>}</Stack></TableCell>
          <TableCell>{new Date(item.lastSeenAt).toLocaleString()}</TableCell>
          <TableCell align="right"><Stack direction="row" spacing={.5} sx={{ justifyContent: 'flex-end' }}><Button size="small" onClick={() => void openDetail(item)}>Compare</Button>{canManage && item.provider === 'connectwise' && <Button size="small" startIcon={<LinkOutlined />} onClick={() => void openLink(item)}>Link CI</Button>}</Stack></TableCell>
        </TableRow>)}</TableBody></Table></TableContainer>}
        {!queue.items.length && busy !== 'queue' && <Alert severity="success" sx={{ m: 2 }}>No review observations match these filters.</Alert>}
        <TablePagination component="div" count={queue.total} page={page} rowsPerPage={rowsPerPage} rowsPerPageOptions={[10, 25, 50, 100]} onPageChange={(_, value) => { setPage(value); setSelected([]); }} onRowsPerPageChange={event => { setRowsPerPage(Number(event.target.value)); setPage(0); setSelected([]); }} />
      </>}
      {tab === 1 && <>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} sx={{ p: 2, alignItems: { md: 'center' }, justifyContent: 'space-between' }}>
          <Box><Typography variant="h5">Ignored provider configurations</Typography><Typography variant="body2" color="text.secondary">These immutable provider IDs are excluded from discovery previews and continuous reconciliation. Canonical CIs and provider records are not deleted.</Typography></Box>
          <Chip color="warning" variant="outlined" icon={<VisibilityOffOutlined />} label={`${suppressions.total} active exclusion${suppressions.total === 1 ? '' : 's'}`} />
        </Stack>
        <Box className="reconciliation-filter">
          <TextField size="small" label="Search name, provider ID or reason" value={search} onChange={event => { setSearch(event.target.value); setSuppressionPage(0); }} />
          <FormControl size="small"><InputLabel>Customer</InputLabel><Select label="Customer" value={companyId} onChange={event => { setCompanyId(event.target.value); setSuppressionPage(0); }}><MenuItem value="">All customers</MenuItem>{workspace.companies.map(item => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</Select></FormControl>
          <FormControl size="small"><InputLabel>Provider</InputLabel><Select label="Provider" value={provider} onChange={event => { setProvider(event.target.value); setSuppressionPage(0); }}><MenuItem value="">All providers</MenuItem>{catalogue?.providers.filter(item => item.key !== 'cmdb').map(item => <MenuItem key={item.key} value={item.key}>{item.name}</MenuItem>)}</Select></FormControl>
        </Box>
        {busy === 'suppressions' && !suppressions.items.length ? <Stack sx={{ p: 5, alignItems: 'center' }} spacing={2}><CircularProgress /><Typography>Loading ignored configurations…</Typography></Stack> : <TableContainer><Table size="small" aria-label="Ignored integration configurations"><TableHead><TableRow>
          <TableCell>Customer</TableCell><TableCell>Provider record</TableCell><TableCell>Reason</TableCell><TableCell>Ignored by</TableCell><TableCell>Ignored</TableCell><TableCell />
        </TableRow></TableHead><TableBody>{suppressions.items.map(item => <TableRow key={item.id} hover>
          <TableCell>{item.companyName || companyNames.get(item.companyId) || item.companyId}</TableCell>
          <TableCell><Typography sx={{ fontWeight: 800 }}>{item.externalName}</Typography><Typography variant="caption" color="text.secondary">{words(item.provider)} · immutable ID {item.externalId}</Typography></TableCell>
          <TableCell sx={{ maxWidth: 360 }}>{item.reason}</TableCell>
          <TableCell>{item.ignoredByName || 'System administrator'}</TableCell>
          <TableCell>{new Date(item.ignoredAt).toLocaleString()}</TableCell>
          <TableCell align="right">{canManage && <Button size="small" startIcon={<RestoreOutlined />} onClick={() => { setRestoreItem(item); setDecisionNotes(''); }}>Restore</Button>}</TableCell>
        </TableRow>)}</TableBody></Table></TableContainer>}
        {!suppressions.items.length && busy !== 'suppressions' && <Alert severity="success" sx={{ m: 2 }}>No ignored configurations match these filters.</Alert>}
        <TablePagination component="div" count={suppressions.total} page={suppressionPage} rowsPerPage={rowsPerPage} rowsPerPageOptions={[10, 25, 50, 100]} onPageChange={(_, value) => setSuppressionPage(value)} onRowsPerPageChange={event => { setRowsPerPage(Number(event.target.value)); setSuppressionPage(0); }} />
      </>}
      {tab === 2 && <>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} sx={{ p: 2, alignItems: { md: 'center' }, justifyContent: 'space-between' }}>
          <Box><Typography variant="h5">Canonical field authority</Typography><Typography variant="body2" color="text.secondary">Rules are evaluated in previews and enforced again during import. Lower priority numbers win.</Typography></Box>
          {canManage && <Stack direction="row" spacing={1}><Button variant="outlined" onClick={() => { setPresetCompanyId(companyId || workspace.companies[0]?.id || ''); setPresetKey('balanced_msp'); }}>Apply baseline</Button><Button variant="contained" onClick={() => setAuthority({ companyId: companyId || workspace.companies[0]?.id || '', ciType: '*', fieldName: 'name', provider: 'connectwise', priority: 20 })}>Add rule</Button></Stack>}
        </Stack>
        <TableContainer><Table size="small"><TableHead><TableRow><TableCell>Customer</TableCell><TableCell>CI type</TableCell><TableCell>Canonical field</TableCell><TableCell>Source</TableCell><TableCell>Priority</TableCell><TableCell /></TableRow></TableHead><TableBody>{displayedRules.map(rule => {
          const peerPriorities = displayedRules.filter(item => item.companyId === rule.companyId && item.ciType === rule.ciType && item.fieldName === rule.fieldName).map(item => item.priority);
          const winner = rule.priority === Math.min(...peerPriorities);
          return <TableRow key={`${rule.companyId}:${rule.ciType}:${rule.fieldName}:${rule.provider}`}><TableCell>{companyNames.get(rule.companyId) || rule.companyId}</TableCell><TableCell>{rule.ciType === '*' ? 'All CI types' : rule.ciType}</TableCell><TableCell>{catalogue?.fields.find(item => item.key === rule.fieldName)?.label || words(rule.fieldName)}</TableCell><TableCell><Chip size="small" color={winner ? 'success' : 'default'} label={catalogue?.providers.find(item => item.key === rule.provider)?.name || words(rule.provider)} /></TableCell><TableCell>{rule.priority}{winner && <Typography variant="caption" color="success.main" sx={{ display: 'block' }}>Highest authority</Typography>}</TableCell><TableCell align="right">{canManage && <><Button size="small" onClick={() => setAuthority(rule)}>Edit</Button><Button size="small" color="error" startIcon={<DeleteOutlined />} disabled={busy === 'authority'} onClick={() => void deleteAuthority(rule)}>Delete</Button></>}</TableCell></TableRow>;
        })}</TableBody></Table></TableContainer>
        {!displayedRules.length && <Alert severity="info" sx={{ m: 2 }}>No explicit authority rules match this customer filter. Until configured, existing provider update behaviour remains unchanged.</Alert>}
      </>}
    </Paper>

    <Dialog open={Boolean(detail)} onClose={() => setDetail(null)} maxWidth="md" fullWidth><DialogTitle>Compare provider and canonical evidence</DialogTitle><DialogContent>{detail && <Stack spacing={2} sx={{ mt: 1 }}>
      <Alert severity={detail.action === 'conflict' ? 'warning' : 'info'}>{detail.reason}</Alert>
      <Grid container spacing={2}><Grid size={{ xs: 12, md: 6 }}><Paper sx={{ p: 2, height: '100%' }}><Typography variant="overline" color="primary">Provider observation</Typography><Typography variant="h6">{detail.externalName}</Typography><Typography color="text.secondary">{words(detail.provider)} · {detail.providerTypeName} · {detail.providerStatusName}</Typography></Paper></Grid><Grid size={{ xs: 12, md: 6 }}><Paper sx={{ p: 2, height: '100%' }}><Typography variant="overline" color="secondary">Canonical CMDB</Typography><Typography variant="h6">{detailAsset?.name || detail.assetName || 'New CI required'}</Typography><Typography color="text.secondary">{detailAsset ? `${detailAsset.type} · ${detailAsset.status} · ${words(detailAsset.source)}` : 'No canonical identity is linked'}</Typography></Paper></Grid></Grid>
      {comparisonFields.length ? <TableContainer component={Paper}><Table size="small"><TableHead><TableRow><TableCell>Field</TableCell><TableCell>Provider value</TableCell><TableCell>Canonical value</TableCell><TableCell>Authority</TableCell></TableRow></TableHead><TableBody>{comparisonFields.map(field => {
        const decision = detail.fieldDecisions?.find(item => item.field === field);
        return <TableRow key={field}><TableCell>{catalogue?.fields.find(item => item.key === field)?.label || field}</TableCell><TableCell>{jsonValue(valueAt(detail.providerRecord || {}, field))}</TableCell><TableCell>{jsonValue(valueAt(detailAsset, field))}</TableCell><TableCell><Chip size="small" color={detail.blockedFields?.includes(field) ? 'warning' : 'success'} label={detail.blockedFields?.includes(field) ? 'Protected' : 'Will apply'} />{decision && <Typography variant="caption" sx={{ display: 'block', mt: .5 }}>{decision.reason}</Typography>}</TableCell></TableRow>;
      })}</TableBody></Table></TableContainer> : <Alert severity="info">This row requires an identity decision rather than a field update.</Alert>}
    </Stack>}</DialogContent><DialogActions><Button onClick={() => setDetail(null)}>Close</Button></DialogActions></Dialog>

    <Dialog open={Boolean(bulkAction)} onClose={() => setBulkAction(null)} maxWidth="sm" fullWidth><DialogTitle>{bulkAction === 'import' ? 'Approve canonical import' : bulkAction === 'ignore' ? 'Ignore provider configurations' : 'Dismiss review observations'}</DialogTitle><DialogContent><Stack spacing={2} sx={{ mt: 1 }}><Alert severity={bulkAction === 'import' || bulkAction === 'ignore' ? 'warning' : 'info'}>{bulkAction === 'import' ? `The provider will be re-read before ${selectedItems.length} selected change(s) are applied. ConnectWise remains read-only.` : bulkAction === 'ignore' ? `${selectedItems.length} immutable provider ID(s) will be excluded from future previews and reconciliation until restored. Existing canonical CIs and provider records will not be deleted or unlinked.` : `Dismiss ${selectedItems.length} observation(s). Changed provider evidence will reopen them automatically.`}</Alert><TextField label={bulkAction === 'ignore' ? 'Ignore reason' : 'Decision notes'} required multiline minRows={3} value={decisionNotes} onChange={event => setDecisionNotes(event.target.value)} helperText="Stored with the governed decision evidence" /></Stack></DialogContent><DialogActions><Button onClick={() => setBulkAction(null)}>Cancel</Button><Button variant="contained" color={bulkAction === 'ignore' ? 'warning' : 'primary'} startIcon={bulkAction === 'ignore' ? <VisibilityOffOutlined /> : <CheckCircleOutlined />} disabled={decisionNotes.trim().length < 4 || busy.startsWith('bulk-')} onClick={() => void runBulkAction()}>{bulkAction === 'import' ? 'Approve and import' : bulkAction === 'ignore' ? 'Ignore configurations' : 'Dismiss selected'}</Button></DialogActions></Dialog>

    <Dialog open={Boolean(restoreItem)} onClose={() => setRestoreItem(null)} maxWidth="sm" fullWidth><DialogTitle>Restore provider configuration</DialogTitle><DialogContent><Stack spacing={2} sx={{ mt: 1 }}><Alert severity="info">{restoreItem?.externalName} will be allowed into the next reconciliation preview. Restoring it does not immediately import or change a canonical CI.</Alert><TextField label="Restore reason" required multiline minRows={3} value={decisionNotes} onChange={event => setDecisionNotes(event.target.value)} helperText="Stored in the audit trail" /></Stack></DialogContent><DialogActions><Button onClick={() => setRestoreItem(null)}>Cancel</Button><Button variant="contained" startIcon={<RestoreOutlined />} disabled={decisionNotes.trim().length < 4 || busy === 'restore'} onClick={() => void restoreSuppression()}>Restore configuration</Button></DialogActions></Dialog>

    <Dialog open={Boolean(linkItem)} onClose={() => setLinkItem(null)} maxWidth="sm" fullWidth><DialogTitle>Link immutable provider identity</DialogTitle><DialogContent><Stack spacing={2} sx={{ mt: 1 }}><Alert severity="warning">Link {linkItem?.externalName} only after confirming it represents the same real configuration item. Names are supporting evidence, never identity.</Alert><Autocomplete options={linkAssets} value={linkAsset} onChange={(_, value) => setLinkAsset(value)} getOptionLabel={option => `${option.name} · ${option.type}`} isOptionEqualToValue={(option, value) => option.id === value.id} renderInput={params => <TextField {...params} label="Canonical CI" required />} /></Stack></DialogContent><DialogActions><Button onClick={() => setLinkItem(null)}>Cancel</Button><Button variant="contained" disabled={!linkAsset || busy === 'link'} onClick={() => void saveLink()}>Link by provider ID</Button></DialogActions></Dialog>

    <Dialog open={Boolean(authority)} onClose={() => setAuthority(null)} maxWidth="sm" fullWidth><DialogTitle>Field authority rule</DialogTitle><DialogContent>{authority && <Stack spacing={2} sx={{ mt: 1 }}><FormControl><InputLabel>Customer</InputLabel><Select label="Customer" value={authority.companyId} onChange={event => setAuthority({ ...authority, companyId: event.target.value })}>{workspace.companies.map(item => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</Select></FormControl><FormControl><InputLabel>CI type</InputLabel><Select label="CI type" value={authority.ciType} onChange={event => setAuthority({ ...authority, ciType: event.target.value })}>{catalogue?.ciTypes.map(value => <MenuItem key={value} value={value}>{value === '*' ? 'All CI types' : value}</MenuItem>)}</Select></FormControl><FormControl><InputLabel>Canonical field</InputLabel><Select label="Canonical field" value={authority.fieldName} onChange={event => setAuthority({ ...authority, fieldName: event.target.value })}>{catalogue?.fields.map(field => <MenuItem key={field.key} value={field.key}>{field.group} · {field.label}</MenuItem>)}</Select></FormControl><FormControl><InputLabel>Authoritative source</InputLabel><Select label="Authoritative source" value={authority.provider} onChange={event => setAuthority({ ...authority, provider: event.target.value })}>{catalogue?.providers.map(item => <MenuItem key={item.key} value={item.key}>{item.name}</MenuItem>)}</Select></FormControl><TextField label="Priority" type="number" value={authority.priority} onChange={event => setAuthority({ ...authority, priority: Number(event.target.value) })} helperText="Lower numbers have greater authority. Use the same value for an intentional tie." /></Stack>}</DialogContent><DialogActions><Button onClick={() => setAuthority(null)}>Cancel</Button><Button variant="contained" disabled={!authority?.companyId || !authority?.fieldName || busy === 'authority'} onClick={() => void saveAuthority()}>Save rule</Button></DialogActions></Dialog>

    <Dialog open={Boolean(presetKey)} onClose={() => setPresetKey('')} maxWidth="sm" fullWidth><DialogTitle>Apply field-authority baseline</DialogTitle><DialogContent><Stack spacing={2} sx={{ mt: 1 }}><Alert severity="info">Presets add or update individual audited rules. Existing unrelated rules are retained.</Alert><FormControl><InputLabel>Customer</InputLabel><Select label="Customer" value={presetCompanyId} onChange={event => setPresetCompanyId(event.target.value)}>{workspace.companies.map(item => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</Select></FormControl><FormControl><InputLabel>Baseline</InputLabel><Select label="Baseline" value={presetKey} onChange={event => setPresetKey(event.target.value)}>{catalogue?.presets.map(item => <MenuItem key={item.key} value={item.key}>{item.name}</MenuItem>)}</Select></FormControl><Typography variant="body2" color="text.secondary">{catalogue?.presets.find(item => item.key === presetKey)?.description}</Typography></Stack></DialogContent><DialogActions><Button onClick={() => setPresetKey('')}>Cancel</Button><Button variant="contained" disabled={!presetCompanyId || !presetKey || busy === 'preset'} onClick={() => void applyPreset()}>Apply baseline</Button></DialogActions></Dialog>
  </Box>;
}
