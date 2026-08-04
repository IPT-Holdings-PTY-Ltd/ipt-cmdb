import AccountTreeOutlined from '@mui/icons-material/AccountTreeOutlined';
import AddOutlined from '@mui/icons-material/AddOutlined';
import AssignmentOutlined from '@mui/icons-material/AssignmentOutlined';
import EditOutlined from '@mui/icons-material/EditOutlined';
import FileDownloadOutlined from '@mui/icons-material/FileDownloadOutlined';
import Inventory2Outlined from '@mui/icons-material/Inventory2Outlined';
import SearchOutlined from '@mui/icons-material/SearchOutlined';
import VisibilityOutlined from '@mui/icons-material/VisibilityOutlined';
import WarningAmberOutlined from '@mui/icons-material/WarningAmberOutlined';
import {
  Alert, Box, Button, Chip, CircularProgress, Divider, FormControl, Grid, IconButton, InputAdornment, InputLabel,
  LinearProgress, MenuItem, Paper, Select, Stack, Tab, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Tabs,
  TextField as MuiTextField, Tooltip, Typography,
} from '@mui/material';
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ApiError, apiFetch, getSession } from './session';
import { businessApplicationMemberships, displayLayer, displayLayerColors, displayLayerLabels, displayLayerOrder, type DisplayLayer } from './topology';
import type { Asset, AssetInventoryPayload, AssetInventoryRecord, AssetMetadata, ChangePackage, Contact, Relationship } from './types';
import { useWorkspace } from './workspace';
import { AuditTimeline } from './Governance';
import { canonicalAssetTypes } from './assetCatalog';
import { Title } from './ui';
import {
  buildAssetTechnicalInventory,
  humanizeInventoryKey,
  mergeAssetInventoryPayload,
  type InventoryFact,
} from './assetInventory';

const types = canonicalAssetTypes.map(id => ({ id, name: id }));
const statuses = ['Active', 'Planned', 'Retired'].map(id => ({ id, name: id }));
const lifecycle = ['planned', 'ordered', 'received', 'in_stock', 'in_service', 'maintenance', 'retired', 'disposed'].map(id => ({ id, name: id.replaceAll('_', ' ') }));
const operational = ['unknown', 'healthy', 'warning', 'critical', 'offline'].map(id => ({ id, name: id }));
const criticality = ['low', 'medium', 'high', 'critical'].map(id => ({ id, name: id }));
const environments = ['production', 'pre_production', 'test', 'development', 'disaster_recovery', 'other'].map(id => ({ id, name: id.replaceAll('_', ' ') }));
const capacityStates = ['unknown', 'sufficient', 'constrained', 'insufficient'].map(id => ({ id, name: id }));
const mobilityStates = ['automatic', 'manual', 'pinned'].map(id => ({ id, name: id }));
const powerStates = ['unknown', 'running', 'stopped', 'suspended', 'offline'].map(id => ({ id, name: id }));
const protectionStates = ['unknown', 'protected', 'degraded', 'unprotected'].map(id => ({ id, name: id }));
const virtualizationTypes = new Set(['Virtual machine', 'Hypervisor host', 'Virtualization cluster', 'Datastore', 'Storage array', 'Virtual network', 'Virtualization manager']);
const networkTypes = new Set(['Network device', 'Network service', 'Network zone', 'VPN tunnel', 'Virtual network']);
const displayLayers = ['', 'business', 'application', 'compute', 'virtualization', 'storage', 'network', 'foundation'].map(id => ({ id, name: id ? id.replaceAll('_', ' ') : 'Automatic from CI type' }));

type AssetGroup = 'layer' | 'business' | 'type' | 'site' | 'none';
type AttentionFilter = 'all' | 'attention' | 'unassigned' | 'renewal' | 'unlinked' | 'stale' | 'manual';

const groupLabels: Record<AssetGroup, string> = { layer: 'Layer', business: 'Business application', type: 'CI type', site: 'Site', none: 'No grouping' };
const attentionLabels: Record<AttentionFilter, string> = { all: 'All assets', attention: 'Needs attention', unassigned: 'Unassigned owner', renewal: 'Renewal / EOL', unlinked: 'No relationships', stale: 'Stale integration data', manual: 'Manually created' };

function primaryOwner(asset: Asset) {
  if (asset.type === 'Business system') return asset.metadata?.businessOwner || asset.metadata?.serviceOwner || asset.metadata?.technicalOwner || '';
  return asset.metadata?.technicalOwner || asset.metadata?.serviceOwner || asset.metadata?.custodian || '';
}

function ownerRole(asset: Asset) {
  if (asset.type === 'Business system' && asset.metadata?.businessOwner) return 'Business owner';
  if (asset.metadata?.technicalOwner) return 'Technical owner';
  if (asset.metadata?.serviceOwner) return 'Service owner';
  if (asset.metadata?.custodian) return 'Custodian';
  return 'Owner missing';
}

function daysFromToday(value?: string) {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? Math.ceil((parsed - Date.now()) / 86_400_000) : null;
}

function dateAttention(asset: Asset) {
  const renewal = daysFromToday(asset.metadata?.renewalDate);
  const endOfLife = daysFromToday(asset.metadata?.endOfLifeDate);
  return (renewal !== null && renewal <= 90) || (endOfLife !== null && endOfLife <= 90);
}

function isStale(asset: Asset) {
  if (asset.source === 'manual' || !asset.lastSeen) return false;
  const seen = Date.parse(asset.lastSeen);
  return Number.isFinite(seen) && Date.now() - seen > 7 * 86_400_000;
}

const inactiveChangeStatuses = new Set(['closed', 'cancelled']);

function changeImpactForAsset(change: ChangePackage, assetId: string) {
  return change.impactSnapshot.find(item => item.assetId === assetId);
}

function changeStatusColor(status: string) {
  if (['completed', 'closed', 'approved'].includes(status)) return 'success';
  if (['failed', 'declined'].includes(status)) return 'error';
  if (['scheduled', 'implementing', 'awaiting_approval'].includes(status)) return 'warning';
  if (['impact_review', 'post_implementation_review'].includes(status)) return 'info';
  return 'default';
}

function attentionReasons(asset: Asset, relationshipCount: number) {
  const reasons: string[] = [];
  const health = asset.metadata?.operationalStatus || 'unknown';
  if (['warning', 'critical', 'offline'].includes(health)) reasons.push(`${health} operational status`);
  if (!primaryOwner(asset)) reasons.push('Owner not assigned');
  if (dateAttention(asset)) reasons.push('Renewal or end-of-life date is due within 90 days');
  if (!relationshipCount && asset.type !== 'Credential owner') reasons.push('No relationships recorded');
  if (isStale(asset)) reasons.push('Integration data is older than 7 days');
  return reasons;
}

function freshness(asset: Asset) {
  if (asset.source === 'manual') return 'Manual record';
  if (!asset.lastSeen) return 'Never seen';
  const days = Math.max(0, Math.floor((Date.now() - Date.parse(asset.lastSeen)) / 86_400_000));
  return days === 0 ? 'Seen today' : `Seen ${days}d ago`;
}

function csvCell(value: unknown) { return `"${String(value ?? '').replaceAll('"', '""')}"`; }

export function AssetList() {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const canEdit = !workspace.isRoot && ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');
  const [assets, setAssets] = useState<Asset[]>([]);
  const [relationships, setRelationships] = useState<Relationship[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [layerFilter, setLayerFilter] = useState<DisplayLayer | ''>('');
  const [typeFilter, setTypeFilter] = useState('');
  const [businessFilter, setBusinessFilter] = useState('');
  const [attentionFilter, setAttentionFilter] = useState<AttentionFilter>('all');
  const [groupBy, setGroupBy] = useState<AssetGroup>('layer');

  useEffect(() => {
    let active = true;
    const load = async () => {
      setLoading(true); setError('');
      try {
        const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
        const [assetData, relationshipData] = await Promise.all([
          apiFetch<Asset[]>(`/api/assets${suffix}`), apiFetch<Relationship[]>(`/api/relationships${suffix}`),
        ]);
        if (active) { setAssets(assetData); setRelationships(relationshipData); }
      } catch (value) { if (active) setError(value instanceof Error ? value.message : 'The asset inventory could not be loaded.'); }
      finally { if (active) setLoading(false); }
    };
    void load();
    return () => { active = false; };
  }, [workspace.companyId, workspace.isRoot]);

  const memberships = useMemo(() => businessApplicationMemberships(assets, relationships), [assets, relationships]);
  const businessSystems = useMemo(() => assets.filter(asset => asset.type === 'Business system').sort((left, right) => left.name.localeCompare(right.name)), [assets]);
  const relationshipCounts = useMemo(() => {
    const counts = new Map<string, number>();
    relationships.forEach(item => { counts.set(item.fromId, (counts.get(item.fromId) || 0) + 1); counts.set(item.toId, (counts.get(item.toId) || 0) + 1); });
    return counts;
  }, [relationships]);
  const visibleTypes = useMemo(() => [...new Set(assets.map(asset => asset.type))].sort(), [assets]);
  const filteredAssets = useMemo(() => assets.filter(asset => {
    const applications = memberships.get(asset.id) || [];
    const query = search.trim().toLowerCase();
    const matchesSearch = !query || [asset.name, asset.type, asset.source, asset.metadata?.site, primaryOwner(asset), ...applications.map(system => system.name)].some(value => String(value || '').toLowerCase().includes(query));
    if (!matchesSearch || (layerFilter && displayLayer(asset) !== layerFilter) || (typeFilter && asset.type !== typeFilter) || (businessFilter && !applications.some(system => system.id === businessFilter))) return false;
    const count = relationshipCounts.get(asset.id) || 0;
    if (attentionFilter === 'attention') return attentionReasons(asset, count).length > 0;
    if (attentionFilter === 'unassigned') return !primaryOwner(asset);
    if (attentionFilter === 'renewal') return dateAttention(asset);
    if (attentionFilter === 'unlinked') return count === 0;
    if (attentionFilter === 'stale') return isStale(asset);
    if (attentionFilter === 'manual') return asset.source === 'manual';
    return true;
  }).sort((left, right) => left.name.localeCompare(right.name)), [assets, memberships, search, layerFilter, typeFilter, businessFilter, attentionFilter, relationshipCounts]);

  const groups = useMemo(() => {
    const values = new Map<string, Asset[]>();
    const add = (key: string, asset: Asset) => values.set(key, [...(values.get(key) || []), asset]);
    filteredAssets.forEach(asset => {
      if (groupBy === 'business') {
        const systems = (memberships.get(asset.id) || []).filter(system => !businessFilter || system.id === businessFilter);
        if (systems.length) systems.forEach(system => add(system.name, asset)); else add('No business application', asset);
      } else if (groupBy === 'layer') add(displayLayerLabels[displayLayer(asset)], asset);
      else if (groupBy === 'type') add(asset.type || 'Unknown type', asset);
      else if (groupBy === 'site') add(asset.metadata?.site || 'Site not recorded', asset);
      else add('', asset);
    });
    return [...values.entries()].sort(([left], [right]) => {
      if (groupBy === 'layer') return displayLayerOrder.findIndex(layer => displayLayerLabels[layer] === left) - displayLayerOrder.findIndex(layer => displayLayerLabels[layer] === right);
      if (left.startsWith('No ') || left.endsWith('not recorded')) return 1;
      if (right.startsWith('No ') || right.endsWith('not recorded')) return -1;
      return left.localeCompare(right);
    });
  }, [filteredAssets, groupBy, memberships, businessFilter]);

  const summary = useMemo(() => ({
    attention: filteredAssets.filter(asset => attentionReasons(asset, relationshipCounts.get(asset.id) || 0).length).length,
    unassigned: filteredAssets.filter(asset => !primaryOwner(asset)).length,
    shared: filteredAssets.filter(asset => (memberships.get(asset.id) || []).length > 1).length,
    layers: new Set(filteredAssets.map(displayLayer)).size,
  }), [filteredAssets, relationshipCounts, memberships]);

  const clearFilters = () => { setSearch(''); setLayerFilter(''); setTypeFilter(''); setBusinessFilter(''); setAttentionFilter('all'); };
  const exportCsv = () => {
    const rows = [['Name', 'Customer', 'Type', 'Layer', 'Business applications', 'Lifecycle', 'Health', 'Criticality', 'Owner', 'Source', 'Last seen'], ...filteredAssets.map(asset => [asset.name, workspace.companies.find(company => company.id === asset.companyId)?.name || asset.companyId, asset.type, displayLayerLabels[displayLayer(asset)], (memberships.get(asset.id) || []).map(system => system.name).join('; '), asset.metadata?.lifecycle, asset.metadata?.operationalStatus, asset.metadata?.criticality, primaryOwner(asset), asset.source, asset.lastSeen])];
    const blob = new Blob([rows.map(row => row.map(csvCell).join(',')).join('\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `cmdb-assets-${workspace.companyId}.csv`; link.click(); URL.revokeObjectURL(url);
  };

  return (
    <Box className="asset-inventory">
      <Title title="Configuration items" />
      <Stack
        direction={{ xs: 'column', lg: 'row' }}
        spacing={2}
        sx={{
          justifyContent: "space-between",
          alignItems: { xs: 'stretch', lg: 'flex-end' },
          mb: 2.5
        }}>
        <Box><Typography variant="overline" color="primary">Configuration management</Typography><Typography variant="h3">Asset inventory</Typography><Typography sx={{
          color: "text.secondary"
        }}>Find CIs by full-stack layer, business application, ownership and operational attention.</Typography></Box>
        <Stack direction="row" spacing={1}>{canEdit && <Button variant="contained" startIcon={<AddOutlined />} onClick={() => navigate('/assets/create')}>Add asset</Button>}<Button variant="outlined" startIcon={<FileDownloadOutlined />} disabled={!filteredAssets.length} onClick={exportCsv}>Export view</Button></Stack>
      </Stack>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      <Grid container spacing={1.5} sx={{ mb: 2 }}>
        {[['Visible CIs', filteredAssets.length, '#7997ff'], ['Layers', summary.layers, '#50d5b9'], ['Needs attention', summary.attention, '#f0a45d'], ['Unassigned', summary.unassigned, '#efc46b'], ['Shared across apps', summary.shared, '#a68cf0']].map(([label, value, color]) => <Grid key={String(label)} size={{ xs: 6, md: 2.4 }}><Paper className="asset-summary-card" sx={{ borderTopColor: color }}><Typography variant="caption" sx={{
          color: "text.secondary"
        }}>{label}</Typography><Typography variant="h5">{value}</Typography></Paper></Grid>)}
      </Grid>
      <Paper className="asset-filter-toolbar" variant="outlined">
        <MuiTextField size="small" label="Search" value={search} onChange={event => setSearch(event.target.value)} slotProps={{ input: { startAdornment: <InputAdornment position="start"><SearchOutlined fontSize="small" /></InputAdornment> } }} />
        <FormControl size="small"><InputLabel>Layer</InputLabel><Select label="Layer" value={layerFilter} onChange={event => setLayerFilter(event.target.value as DisplayLayer | '')}><MenuItem value="">All layers</MenuItem>{displayLayerOrder.map(layer => <MenuItem key={layer} value={layer}>{displayLayerLabels[layer]}</MenuItem>)}</Select></FormControl>
        <FormControl size="small"><InputLabel>Business application</InputLabel><Select label="Business application" value={businessFilter} onChange={event => setBusinessFilter(event.target.value)}><MenuItem value="">All applications</MenuItem>{businessSystems.map(system => <MenuItem key={system.id} value={system.id}>{system.name}</MenuItem>)}</Select></FormControl>
        <FormControl size="small"><InputLabel>CI type</InputLabel><Select label="CI type" value={typeFilter} onChange={event => setTypeFilter(event.target.value)}><MenuItem value="">All types</MenuItem>{visibleTypes.map(type => <MenuItem key={type} value={type}>{type}</MenuItem>)}</Select></FormControl>
        <FormControl size="small"><InputLabel>Attention</InputLabel><Select label="Attention" value={attentionFilter} onChange={event => setAttentionFilter(event.target.value as AttentionFilter)}>{(Object.keys(attentionLabels) as AttentionFilter[]).map(value => <MenuItem key={value} value={value}>{attentionLabels[value]}</MenuItem>)}</Select></FormControl>
        <FormControl size="small"><InputLabel>Group by</InputLabel><Select label="Group by" value={groupBy} onChange={event => setGroupBy(event.target.value as AssetGroup)}>{(Object.keys(groupLabels) as AssetGroup[]).map(value => <MenuItem key={value} value={value}>{groupLabels[value]}</MenuItem>)}</Select></FormControl>
        <Button color="inherit" onClick={clearFilters}>Clear</Button>
      </Paper>
      {loading ? <Stack
        spacing={2}
        sx={{
          alignItems: "center",
          py: 8
        }}><CircularProgress /><Typography sx={{
        color: "text.secondary"
      }}>Loading configuration items and relationships…</Typography></Stack> : !filteredAssets.length ? <Alert severity="info">No configuration items match these filters.</Alert> : <Stack spacing={1.5}>
        {groups.map(([group, records]) => <Paper key={group || 'all'} className="asset-group" variant="outlined">
          {group && <Stack
            className="asset-group-heading"
            direction="row"
            sx={{
              justifyContent: "space-between",
              alignItems: "center"
            }}><Stack direction="row" spacing={1} sx={{
            alignItems: "center"
          }}><Inventory2Outlined sx={{ color: groupBy === 'layer' ? displayLayerColors[displayLayerOrder.find(layer => displayLayerLabels[layer] === group) || 'foundation'] : 'primary.main' }} /><Typography variant="h6">{group}</Typography></Stack><Chip size="small" label={`${records.length} CI${records.length === 1 ? '' : 's'}`} /></Stack>}
          <TableContainer><Table size="small"><TableHead><TableRow><TableCell>Name / type</TableCell>{workspace.isRoot && <TableCell>Customer</TableCell>}<TableCell>Layer</TableCell><TableCell>Business applications</TableCell><TableCell>State</TableCell><TableCell>Accountable owner</TableCell><TableCell>Source / freshness</TableCell><TableCell align="right">Actions</TableCell></TableRow></TableHead><TableBody>
            {records.map(asset => {
              const layer = displayLayer(asset); const applications = memberships.get(asset.id) || []; const reasons = attentionReasons(asset, relationshipCounts.get(asset.id) || 0); const health = asset.metadata?.operationalStatus || 'unknown';
              return (
                <TableRow key={`${group}-${asset.id}`} hover className="asset-inventory-row">
                  <TableCell><Button className="asset-name-button" color="inherit" onClick={() => navigate(`/assets/${asset.id}/show`)}><Box sx={{
                    textAlign: "left"
                  }}><Typography sx={{
                    fontWeight: 800
                  }}>{asset.name}</Typography><Typography variant="caption" sx={{
                    color: "text.secondary"
                  }}>{asset.type}{asset.metadata?.site ? ` · ${asset.metadata.site}` : ''}{asset.metadata?.ipAddress ? ` · ${asset.metadata.ipAddress}` : ''}</Typography></Box></Button></TableCell>
                  {workspace.isRoot && <TableCell>{workspace.companies.find(company => company.id === asset.companyId)?.name || asset.companyId}</TableCell>}
                  <TableCell><Chip size="small" variant="outlined" label={displayLayerLabels[layer]} sx={{ color: displayLayerColors[layer], borderColor: `${displayLayerColors[layer]}99` }} /></TableCell>
                  <TableCell><Stack direction="row" spacing={0.5} useFlexGap sx={{
                    flexWrap: "wrap"
                  }}>{applications.slice(0, 2).map(system => <Chip key={system.id} size="small" clickable label={system.name} onClick={() => navigate(`/relationships?view=stack&businessAppId=${encodeURIComponent(system.id)}`)} />)}{applications.length > 2 && <Chip size="small" label={`+${applications.length - 2}`} />}{!applications.length && <Typography variant="caption" sx={{
                    color: "text.secondary"
                  }}>Not mapped</Typography>}</Stack></TableCell>
                  <TableCell><Stack direction="row" spacing={0.5} sx={{
                    alignItems: "center"
                  }}><Chip size="small" color={health === 'healthy' ? 'success' : ['critical', 'offline'].includes(health) ? 'error' : health === 'warning' ? 'warning' : 'default'} label={health} /><Chip size="small" variant="outlined" label={asset.metadata?.criticality || 'medium'} />{reasons.length > 0 && <Tooltip title={reasons.join(' · ')}><Chip size="small" color="warning" variant="outlined" icon={<WarningAmberOutlined />} label={reasons.length} /></Tooltip>}</Stack><Typography variant="caption" sx={{
                    color: "text.secondary"
                  }}>{(asset.metadata?.lifecycle || 'unknown').replaceAll('_', ' ')}</Typography></TableCell>
                  <TableCell><Typography variant="body2">{primaryOwner(asset) || 'Unassigned'}</Typography><Typography variant="caption" color={primaryOwner(asset) ? 'text.secondary' : 'warning.main'}>{ownerRole(asset)}</Typography></TableCell>
                  <TableCell><Chip size="small" variant="outlined" label={asset.source || 'unknown'} /><Typography variant="caption" color={isStale(asset) ? 'warning.main' : 'text.secondary'} sx={{
                    display: "block"
                  }}>{freshness(asset)}</Typography></TableCell>
                  <TableCell align="right"><Tooltip title="View details"><IconButton size="small" onClick={() => navigate(`/assets/${asset.id}/show`)}><VisibilityOutlined fontSize="small" /></IconButton></Tooltip><Tooltip title="View relationships"><IconButton size="small" onClick={() => navigate(`/relationships?view=technical&assetId=${encodeURIComponent(asset.id)}`)}><AccountTreeOutlined fontSize="small" /></IconButton></Tooltip>{canEdit && <Tooltip title="Edit asset"><IconButton size="small" onClick={() => navigate(`/assets/${asset.id}`)}><EditOutlined fontSize="small" /></IconButton></Tooltip>}</TableCell>
                </TableRow>
              );
            })}
          </TableBody></Table></TableContainer>
        </Paper>)}
      </Stack>}
    </Box>
  );
}

type AssetFormData = {
  name: string;
  type: string;
  status: string;
  fields: Record<string, unknown>;
  metadata: Partial<AssetMetadata>;
  ownerSelections: Record<string, string>;
};

const defaultAssetForm: AssetFormData = {
  name: '',
  type: 'Device',
  status: 'Active',
  fields: {},
  metadata: {
    lifecycle: 'in_service',
    operationalStatus: 'healthy',
    criticality: 'medium',
    environment: 'production',
  },
  ownerSelections: {},
};

function assetFormData(asset?: Asset): AssetFormData {
  if (!asset) return { ...defaultAssetForm, fields: {}, metadata: { ...defaultAssetForm.metadata }, ownerSelections: {} };
  return {
    name: asset.name,
    type: asset.type,
    status: asset.status,
    fields: { ...asset.fields },
    metadata: { ...asset.metadata },
    ownerSelections: Object.fromEntries(
      (asset.responsibilities || [])
        .filter(item => !item.effectiveUntil && item.isPrimary)
        .map(item => [item.role, item.contactId]),
    ),
  };
}

function assetPayload(data: AssetFormData) {
  return {
    name: data.name.trim(),
    type: data.type,
    status: data.status,
    fields: data.fields,
    metadata: data.metadata,
    responsibilities: Object.entries(data.ownerSelections)
      .filter(([, contactId]) => Boolean(contactId))
      .map(([role, contactId]) => ({ role, contactId, isPrimary: true, escalationOrder: 1 })),
  };
}

function fieldValue(data: AssetFormData, source: string) {
  if (source.startsWith('metadata.')) return String(data.metadata[source.slice(9) as keyof AssetMetadata] || '');
  if (source.startsWith('ownerSelections.')) return data.ownerSelections[source.slice(16)] || '';
  return String(data[source as keyof Pick<AssetFormData, 'name' | 'type' | 'status'>] || '');
}

function updateField(data: AssetFormData, source: string, value: string): AssetFormData {
  if (source.startsWith('metadata.')) {
    return { ...data, metadata: { ...data.metadata, [source.slice(9)]: value } };
  }
  if (source.startsWith('ownerSelections.')) {
    return { ...data, ownerSelections: { ...data.ownerSelections, [source.slice(16)]: value } };
  }
  return { ...data, [source]: value };
}

function AssetTextInput({
  data,
  setData,
  source,
  label,
  type = 'text',
  helperText,
  required = false,
}: {
  data: AssetFormData;
  setData: (value: AssetFormData) => void;
  source: string;
  label: string;
  type?: string;
  helperText?: string;
  required?: boolean;
}) {
  return (
    <MuiTextField
      label={label}
      type={type}
      value={fieldValue(data, source)}
      onChange={event => setData(updateField(data, source, event.target.value))}
      helperText={helperText}
      required={required}
      fullWidth
    />
  );
}

function AssetSelectInput({
  data,
  setData,
  source,
  label,
  choices,
  helperText,
  emptyText,
  required = false,
}: {
  data: AssetFormData;
  setData: (value: AssetFormData) => void;
  source: string;
  label: string;
  choices: Array<{ id: string; name: string }>;
  helperText?: string;
  emptyText?: string;
  required?: boolean;
}) {
  return (
    <MuiTextField
      select
      label={label}
      value={fieldValue(data, source)}
      onChange={event => setData(updateField(data, source, event.target.value))}
      helperText={helperText}
      required={required}
      fullWidth
    >
      {emptyText !== undefined && <MenuItem value="">{emptyText}</MenuItem>}
      {choices.map(choice => <MenuItem key={choice.id} value={choice.id}>{choice.name}</MenuItem>)}
    </MuiTextField>
  );
}

function ContactOwnershipInputs({
  businessSystem,
  data,
  setData,
}: {
  businessSystem: boolean;
  data: AssetFormData;
  setData: (value: AssetFormData) => void;
}) {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const [contacts, setContacts] = useState<Contact[]>([]);
  useEffect(() => {
    if (!workspace.isRoot) {
      apiFetch<Contact[]>(`/api/contacts?companyId=${encodeURIComponent(workspace.companyId)}`)
        .then(setContacts)
        .catch(() => setContacts([]));
    }
  }, [workspace.companyId, workspace.isRoot]);
  const choices = contacts.filter(item => ['active', 'on_leave'].includes(item.status)).map(item => ({
    id: item.id,
    name: `${item.displayName}${item.department ? ` · ${item.department}` : ''}`,
  }));
  const owner = (source: string, label: string, emptyText = 'Unassigned') => (
    <AssetSelectInput data={data} setData={setData} source={`ownerSelections.${source}`} label={label} choices={choices} emptyText={emptyText} />
  );
  return <>
    {businessSystem && <Grid size={{ xs: 12, md: 4 }}>{owner('business_owner', 'Business owner')}</Grid>}
    <Grid size={{ xs: 12, md: 4 }}>{owner('service_owner', 'Service owner')}</Grid>
    <Grid size={{ xs: 12, md: 4 }}>{owner('technical_owner', 'Technical owner')}</Grid>
    {!businessSystem && <Grid size={{ xs: 12, md: 4 }}>{owner('custodian', 'Custodian')}</Grid>}
    {businessSystem && <Grid size={{ xs: 12, md: 4 }}>{owner('signoff_delegate', 'Signoff delegate', 'Owner signs off')}</Grid>}
    <Grid size={{ xs: 12 }}><Button size="small" startIcon={<AddOutlined />} onClick={() => navigate('/contacts')}>Create or update contacts</Button></Grid>
  </>;
}

function AssetForm({
  initial,
  saving,
  error,
  submitLabel,
  onSubmit,
}: {
  initial: AssetFormData;
  saving: boolean;
  error: string;
  submitLabel: string;
  onSubmit: (data: AssetFormData) => Promise<void>;
}) {
  const navigate = useNavigate();
  const [data, setData] = useState(initial);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void onSubmit(data);
  };
  const text = (source: string, label: string, helperText?: string, type?: string) => (
    <AssetTextInput data={data} setData={setData} source={source} label={label} helperText={helperText} type={type} />
  );
  const select = (source: string, label: string, choices: Array<{ id: string; name: string }>, helperText?: string) => (
    <AssetSelectInput data={data} setData={setData} source={source} label={label} choices={choices} helperText={helperText} />
  );

  return (
    <Box component="form" onSubmit={submit}>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      <Typography variant="h6" className="form-section">Identity and classification</Typography>
      <Grid container spacing={2} sx={{ width: '100%' }}>
        <Grid size={{ xs: 12, md: 6 }}><AssetTextInput data={data} setData={setData} source="name" label="Name" required /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><AssetSelectInput data={data} setData={setData} source="type" label="Type" choices={types} required /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><AssetSelectInput data={data} setData={setData} source="status" label="Status" choices={statuses} required /></Grid>
      </Grid>
      <Typography variant="h6" className="form-section">Lifecycle and service health</Typography>
      <Grid container spacing={2} sx={{ width: '100%' }}>
        <Grid size={{ xs: 12, md: 3 }}>{select('metadata.lifecycle', 'Lifecycle', lifecycle)}</Grid>
        <Grid size={{ xs: 12, md: 3 }}>{select('metadata.operationalStatus', 'Operational status', operational)}</Grid>
        <Grid size={{ xs: 12, md: 3 }}>{select('metadata.criticality', 'Criticality', criticality)}</Grid>
        <Grid size={{ xs: 12, md: 3 }}>{select('metadata.environment', 'Environment', environments)}</Grid>
      </Grid>
      <Typography variant="h6" className="form-section">Ownership and location</Typography>
      <Grid container spacing={2} sx={{ width: '100%' }}>
        <ContactOwnershipInputs businessSystem={data.type === 'Business system'} data={data} setData={setData} />
        <Grid size={{ xs: 12, md: 6 }}>{text('metadata.site', 'Site')}</Grid>
        <Grid size={{ xs: 12, md: 3 }}>{text('metadata.vendor', 'Vendor')}</Grid>
        <Grid size={{ xs: 12, md: 3 }}>{text('metadata.model', 'Model')}</Grid>
      </Grid>
      <Typography variant="h6" className="form-section">Relationship display</Typography>
      <Grid container spacing={2} sx={{ width: '100%' }}>
        <Grid size={{ xs: 12, md: 4 }}>{select('metadata.displayLayer', 'Display layer', displayLayers, 'Leave automatic unless this CI belongs in another full-stack lane.')}</Grid>
      </Grid>
      {networkTypes.has(data.type) && <Box sx={{ width: '100%' }}>
        <Typography variant="h6" className="form-section">Network placement</Typography>
        <Grid container spacing={2} sx={{ width: '100%' }}>
          <Grid size={{ xs: 12, md: 3 }}>{text('metadata.networkZone', 'Zone', 'For example WAN, Edge, Core, Access or Finance.')}</Grid>
          <Grid size={{ xs: 12, md: 3 }}>{text('metadata.networkRole', 'Network role')}</Grid>
          <Grid size={{ xs: 6, md: 2 }}>{text('metadata.vlanId', 'VLAN')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{text('metadata.subnet', 'Subnet / prefix')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{text('metadata.ipAddress', 'Management / primary IP')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{text('metadata.redundancyGroup', 'Redundancy group')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{text('metadata.redundancyRole', 'Redundancy role', 'For example active, passive or member 1.')}</Grid>
        </Grid>
      </Box>}
      <Typography variant="h6" className="form-section">Commercial and review dates</Typography>
      <Grid container spacing={2} sx={{ width: '100%' }}>
        <Grid size={{ xs: 12, md: 3 }}>{text('metadata.purchaseDate', 'Purchase date', undefined, 'date')}</Grid>
        <Grid size={{ xs: 12, md: 3 }}>{text('metadata.warrantyEnd', 'Warranty end', undefined, 'date')}</Grid>
        <Grid size={{ xs: 12, md: 3 }}>{text('metadata.renewalDate', 'Renewal date', undefined, 'date')}</Grid>
        <Grid size={{ xs: 12, md: 3 }}>{text('metadata.endOfLifeDate', 'End-of-life date', undefined, 'date')}</Grid>
        <Grid size={{ xs: 12, md: 3 }}>{text('metadata.reviewDate', 'Review date', undefined, 'date')}</Grid>
      </Grid>
      {virtualizationTypes.has(data.type) && <Box sx={{ width: '100%' }}>
        <Typography variant="h6" className="form-section">Virtualization and resilience</Typography>
        <Grid container spacing={2} sx={{ width: '100%' }}>
          <Grid size={{ xs: 12, md: 4 }}>{text('metadata.virtualizationPlatform', 'Platform', 'For example VMware vSphere, Hyper-V or Azure.')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{text('metadata.clusterName', 'Cluster')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{select('metadata.powerState', 'Power state', powerStates)}</Grid>
          <Grid size={{ xs: 12, md: 3 }}>{select('metadata.haEnabled', 'HA enabled', [{ id: 'yes', name: 'Yes' }, { id: 'no', name: 'No' }])}</Grid>
          <Grid size={{ xs: 12, md: 3 }}>{select('metadata.protectionStatus', 'Protection', protectionStates)}</Grid>
          <Grid size={{ xs: 12, md: 3 }}>{select('metadata.mobility', 'Mobility', mobilityStates)}</Grid>
          <Grid size={{ xs: 12, md: 3 }}>{select('metadata.capacityStatus', 'Failover capacity', capacityStates)}</Grid>
          <Grid size={{ xs: 6, md: 3 }}>{text('metadata.minimumHosts', 'Minimum surviving hosts')}</Grid>
          <Grid size={{ xs: 6, md: 3 }}>{select('metadata.maintenanceMode', 'Maintenance mode', [{ id: 'yes', name: 'Yes' }, { id: 'no', name: 'No' }])}</Grid>
          <Grid size={{ xs: 12, md: 6 }}>{text('metadata.guestOs', 'Guest operating system')}</Grid>
          <Grid size={{ xs: 4 }}>{text('metadata.cpuCount', 'vCPU')}</Grid>
          <Grid size={{ xs: 4 }}>{text('metadata.memoryGb', 'Memory (GB)')}</Grid>
          <Grid size={{ xs: 4 }}>{text('metadata.storageGb', 'Storage (GB)')}</Grid>
        </Grid>
      </Box>}
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end', mt: 3 }}>
        <Button type="button" color="inherit" onClick={() => navigate(-1)}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={saving || !data.name.trim()}>{saving ? 'Saving…' : submitLabel}</Button>
      </Stack>
    </Box>
  );
}

export function AssetCreate() {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const canEdit = !workspace.isRoot && ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');
  if (!canEdit) return <Alert severity="warning">Select a customer workspace with asset management access before creating a configuration item.</Alert>;
  const save = async (data: AssetFormData) => {
    setSaving(true);
    setError('');
    try {
      const created = await apiFetch<Asset>('/api/assets', {
        method: 'POST',
        body: JSON.stringify({ ...assetPayload(data), companyId: workspace.companyId }),
      });
      navigate(`/assets/${created.id}/show`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The asset could not be created.');
    } finally {
      setSaving(false);
    }
  };
  return <Box><Title title="Add configuration item" /><Typography variant="overline" color="primary">Configuration management</Typography><Typography variant="h3">Add asset</Typography><AssetForm initial={assetFormData()} saving={saving} error={error} submitLabel="Create asset" onSubmit={save} /></Box>;
}

function assetResourcePath(assetId: string) {
  return `/api/assets/${encodeURIComponent(assetId)}`;
}

export function AssetEdit() {
  const { assetId = '' } = useParams();
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const [asset, setAsset] = useState<Asset | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const canEdit = !workspace.isRoot && ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');
  useEffect(() => {
    setLoading(true);
    apiFetch<Asset>(assetResourcePath(assetId))
      .then(setAsset)
      .catch(reason => setError(reason instanceof Error ? reason.message : 'The asset could not be loaded.'))
      .finally(() => setLoading(false));
  }, [assetId]);
  if (!canEdit) return <Alert severity="warning">Asset editing requires a customer workspace and asset management access.</Alert>;
  if (loading) return <Stack sx={{ alignItems: 'center', py: 8 }}><CircularProgress /></Stack>;
  if (!asset) return <Alert severity="error">{error || 'Asset not found.'}</Alert>;
  const save = async (data: AssetFormData) => {
    setSaving(true);
    setError('');
    try {
      await apiFetch<Asset>(assetResourcePath(asset.id), {
        method: 'PATCH',
        body: JSON.stringify(assetPayload(data)),
      });
      navigate(`/assets/${asset.id}/show`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The asset could not be updated.');
    } finally {
      setSaving(false);
    }
  };
  return <Box><Title title={`Edit ${asset.name}`} /><Typography variant="overline" color="primary">Configuration management</Typography><Typography variant="h3">Edit {asset.name}</Typography><AssetForm initial={assetFormData(asset)} saving={saving} error={error} submitLabel="Save changes" onSubmit={save} /></Box>;
}

type AssetDetailTab = 'overview' | 'hardware' | 'network' | 'software' | 'virtualization' | 'monitoring' | 'source';

const assetDetailTabs: Array<{ key: AssetDetailTab; label: string }> = [
  { key: 'overview', label: 'Overview' },
  { key: 'hardware', label: 'Hardware' },
  { key: 'network', label: 'Network' },
  { key: 'software', label: 'Software' },
  { key: 'virtualization', label: 'Virtualization' },
  { key: 'monitoring', label: 'Monitoring' },
  { key: 'source', label: 'Source evidence' },
];

function formatInventoryDate(value: unknown) {
  const parsed = Date.parse(String(value || ''));
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleString() : String(value || '');
}

function formatInventoryValue(value: unknown, key = ''): string {
  if (value === null || value === undefined || value === '') return 'Not recorded';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value)) return value.map(item => formatInventoryValue(item)).join(', ');
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== null && item !== undefined && item !== '')
      .map(([itemKey, item]) => `${humanizeInventoryKey(itemKey)}: ${formatInventoryValue(item, itemKey)}`)
      .join(' · ') || 'Not recorded';
  }
  if (typeof value === 'number' && /(bytes|capacitybytes|sizebytes)/i.test(key)) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = value; let unit = 0;
    while (Math.abs(size) >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size.toLocaleString(undefined, { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
  }
  if (/(date|time|boot|check.?in|observed|collected|created|updated|expires)/i.test(key) && !Number.isNaN(Date.parse(String(value)))) {
    return formatInventoryDate(value);
  }
  return String(value);
}

function InventoryFacts({ facts, empty = 'No details were supplied by the source.' }: { facts: InventoryFact[]; empty?: string }) {
  if (!facts.length) return <Alert severity="info">{empty}</Alert>;
  return <Grid container spacing={1.25}>{facts.map(fact => <Grid key={fact.label} size={{ xs: 12, sm: 6, lg: 4 }}><Paper className="inventory-fact" variant="outlined"><Typography variant="caption" color="text.secondary">{fact.label}</Typography><Typography variant="body2">{formatInventoryValue(fact.value, fact.label)}</Typography></Paper></Grid>)}</Grid>;
}

const inventoryColumnPriority = [
  'name', 'displayName', 'description', 'manufacturer', 'model', 'version', 'publisher',
  'status', 'state', 'ipAddress', 'ipAddresses', 'macAddress', 'gateway', 'gateways',
  'dnsServers', 'capacityBytes', 'sizeBytes', 'capacity', 'size', 'installDate',
];

function recordColumns(records: AssetInventoryRecord[]) {
  const available = new Set<string>();
  records.forEach(record => Object.entries(record).forEach(([key, value]) => {
    const displayable = typeof value !== 'object' || (Array.isArray(value) && value.length > 0);
    if (value !== null && value !== undefined && value !== '' && displayable) available.add(key);
  }));
  const ordered = [...inventoryColumnPriority.filter(key => available.has(key)), ...[...available].filter(key => !inventoryColumnPriority.includes(key)).sort()];
  if (ordered.length) return ordered.slice(0, 7);
  return [...new Set(records.flatMap(record => Object.keys(record)))].slice(0, 7);
}

function InventoryRecordTable({
  title,
  records,
  empty,
  maxRows = 50,
}: {
  title: string;
  records: AssetInventoryRecord[];
  empty?: string;
  maxRows?: number;
}) {
  if (!records.length) return <Box><Typography variant="h6" sx={{ mb: 1 }}>{title}</Typography><Alert severity="info">{empty || `No ${title.toLowerCase()} were supplied by the source.`}</Alert></Box>;
  const columns = recordColumns(records);
  const visible = records.slice(0, maxRows);
  return <Box className="inventory-collection"><Stack direction="row" spacing={1} sx={{ alignItems: 'center', justifyContent: 'space-between', mb: 1 }}><Typography variant="h6">{title}</Typography><Chip size="small" variant="outlined" label={`${records.length} record${records.length === 1 ? '' : 's'}`} /></Stack><TableContainer><Table size="small" className="inventory-table"><TableHead><TableRow>{columns.map(column => <TableCell key={column}>{humanizeInventoryKey(column)}</TableCell>)}</TableRow></TableHead><TableBody>{visible.map((record, index) => <TableRow key={`${String(record.id || record.name || record.displayName || title)}-${index}`} hover>{columns.map(column => <TableCell key={column}>{formatInventoryValue(record[column], column)}</TableCell>)}</TableRow>)}</TableBody></Table></TableContainer>{records.length > visible.length && <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>Showing the first {visible.length.toLocaleString()} of {records.length.toLocaleString()} records.</Typography>}</Box>;
}

function InventorySection({
  eyebrow,
  title,
  copy,
  children,
}: {
  eyebrow: string;
  title: string;
  copy: string;
  children: ReactNode;
}) {
  return <Paper className="inventory-section" variant="outlined"><Typography variant="overline" color="primary">{eyebrow}</Typography><Typography variant="h5">{title}</Typography><Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{copy}</Typography>{children}</Paper>;
}

function AssetShowContent({ asset, inventoryError = '' }: { asset: Asset; inventoryError?: string }) {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const canEdit = !workspace.isRoot && ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');
  const [applications, setApplications] = useState<Asset[]>([]);
  const [directRelationships, setDirectRelationships] = useState(0);
  const [changes, setChanges] = useState<ChangePackage[]>([]);
  const [changesLoading, setChangesLoading] = useState(false);
  const [changesError, setChangesError] = useState('');
  const [detailTab, setDetailTab] = useState<AssetDetailTab>('overview');
  useEffect(() => {
    let active = true;
    const loadContext = async () => {
      setChangesLoading(true);
      setChangesError('');
      try {
        const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
        const [assets, relationships, relatedChanges] = await Promise.all([
          apiFetch<Asset[]>(`/api/assets${suffix}`),
          apiFetch<Relationship[]>(`/api/relationships${suffix}`),
          apiFetch<ChangePackage[]>(`/api/changes?assetId=${encodeURIComponent(asset.id)}`),
        ]);
        if (!active) return;
        setApplications(businessApplicationMemberships(assets, relationships).get(asset.id) || []);
        setDirectRelationships(relationships.filter(item => item.fromId === asset.id || item.toId === asset.id).length);
        setChanges(relatedChanges);
      } catch (error) {
        if (active) {
          setApplications([]);
          setDirectRelationships(0);
          setChanges([]);
          setChangesError(error instanceof Error ? error.message : 'Change activity could not be loaded.');
        }
      } finally {
        if (active) setChangesLoading(false);
      }
    };
    void loadContext();
    return () => { active = false; };
  }, [asset.id, workspace.companyId, workspace.isRoot]);
  const inventory = useMemo(() => buildAssetTechnicalInventory(asset), [asset]);
  const availableInventorySections = inventory.coverage.filter(section => section.available).length;
  const inventoryCoveragePercent = Math.round((availableInventorySections / inventory.coverage.length) * 100);
  const layer = displayLayer(asset); const health = asset.metadata?.operationalStatus || 'unknown'; const owner = primaryOwner(asset);
  const orderedChanges = [...changes].sort((left, right) => {
    const leftInactive = inactiveChangeStatuses.has(left.status) ? 1 : 0;
    const rightInactive = inactiveChangeStatuses.has(right.status) ? 1 : 0;
    return leftInactive - rightInactive || (right.plannedStart || right.createdAt).localeCompare(left.plannedStart || left.createdAt);
  });
  const openChanges = (query: string) => {
    workspace.setCompanyId(asset.companyId);
    navigate(`/changes?${query}`);
  };
  return (
    <Box className="asset-detail">
      <Stack
        direction={{ xs: 'column', md: 'row' }}
        spacing={2}
        sx={{
          justifyContent: "space-between",
          alignItems: { xs: 'stretch', md: 'flex-start' },
          mb: 2
        }}>
        <Box><Typography variant="overline" sx={{ color: displayLayerColors[layer] }}>{displayLayerLabels[layer]}</Typography><Typography variant="h3">{asset.name}</Typography><Typography sx={{
          color: "text.secondary"
        }}>{asset.type}{asset.metadata?.vendor ? ` · ${asset.metadata.vendor}` : ''}{asset.metadata?.model ? ` ${asset.metadata.model}` : ''}</Typography><Stack
          direction="row"
          spacing={0.75}
          useFlexGap
          sx={{
            flexWrap: "wrap",
            mt: 1.5
          }}><Chip size="small" color={health === 'healthy' ? 'success' : ['critical', 'offline'].includes(health) ? 'error' : 'warning'} label={health} /><Chip size="small" variant="outlined" label={`${asset.metadata?.criticality || 'medium'} criticality`} /><Chip size="small" variant="outlined" label={(asset.metadata?.lifecycle || 'unknown').replaceAll('_', ' ')} />{applications.map(system => <Chip key={system.id} size="small" clickable label={system.name} onClick={() => navigate(`/relationships?view=stack&businessAppId=${encodeURIComponent(system.id)}`)} />)}</Stack></Box>
        <Stack direction="row" spacing={1}><Button variant="outlined" startIcon={<AccountTreeOutlined />} onClick={() => navigate(`/relationships?view=technical&assetId=${encodeURIComponent(asset.id)}`)}>Relationships ({directRelationships})</Button><Button variant="outlined" startIcon={<AssignmentOutlined />} onClick={() => openChanges(`assetId=${encodeURIComponent(asset.id)}`)}>Create change</Button>{canEdit && <Button variant="contained" startIcon={<EditOutlined />} onClick={() => navigate(`/assets/${asset.id}`)}>Edit</Button>}</Stack>
      </Stack>
      <Paper className="inventory-navigation" variant="outlined">
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ alignItems: { md: 'center' }, justifyContent: 'space-between', px: 2, pt: 1.75 }}>
          <Box sx={{ minWidth: 220 }}><Stack direction="row" spacing={1} sx={{ alignItems: 'baseline', justifyContent: 'space-between' }}><Typography variant="overline" color="primary">Technical inventory coverage</Typography><Typography variant="caption" color="text.secondary">{availableInventorySections} of {inventory.coverage.length} sections</Typography></Stack><LinearProgress variant="determinate" value={inventoryCoveragePercent} aria-label="Technical inventory coverage" sx={{ mt: 0.5 }} /></Box>
          <Stack direction="row" spacing={0.75} useFlexGap sx={{ flexWrap: 'wrap', justifyContent: { md: 'flex-end' } }}>{inventory.coverage.map(section => <Chip key={section.key} size="small" color={section.available ? 'success' : 'default'} variant={section.available ? 'filled' : 'outlined'} label={`${section.label}${section.count ? ` (${section.count})` : ''}`} />)}{inventory.observedAt && <Chip size="small" variant="outlined" label={`Observed ${formatInventoryDate(inventory.observedAt)}`} />}</Stack>
        </Stack>
        <Tabs value={detailTab} onChange={(_, value: AssetDetailTab) => setDetailTab(value)} variant="scrollable" scrollButtons="auto" aria-label="Asset detail sections">{assetDetailTabs.map(tab => <Tab key={tab.key} value={tab.key} label={tab.label} />)}</Tabs>
      </Paper>
      {inventoryError && <Alert severity="warning" sx={{ mb: 2 }}>{inventoryError} Summary data remains available.</Alert>}
      <Box role="tabpanel" aria-label={`${assetDetailTabs.find(tab => tab.key === detailTab)?.label} asset details`} className="inventory-tab-panel">
        {detailTab === 'overview' && <Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 6, lg: 3 }}><Paper className="asset-detail-card"><Typography variant="overline" color="primary">Ownership</Typography><Typography variant="h6">{owner || 'Unassigned'}</Typography><Typography variant="body2" color="text.secondary">{ownerRole(asset)}</Typography><Divider sx={{ my: 1.5 }} /><Typography variant="caption" color="text.secondary">Service owner</Typography><Typography variant="body2">{asset.metadata?.serviceOwner || 'Not recorded'}</Typography><Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>Custodian</Typography><Typography variant="body2">{asset.metadata?.custodian || 'Not recorded'}</Typography></Paper></Grid>
          <Grid size={{ xs: 12, md: 6, lg: 3 }}><Paper className="asset-detail-card"><Typography variant="overline" color="primary">Placement</Typography><Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>Environment</Typography><Typography variant="body2">{asset.metadata?.environment || 'Not recorded'}</Typography><Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>Site</Typography><Typography variant="body2">{asset.metadata?.site || 'Not recorded'}</Typography><Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>Network</Typography><Typography variant="body2">{[asset.metadata?.networkZone, asset.metadata?.vlanId ? `VLAN ${asset.metadata.vlanId}` : '', asset.metadata?.ipAddress].filter(Boolean).join(' · ') || 'Not recorded'}</Typography></Paper></Grid>
          <Grid size={{ xs: 12, md: 6, lg: 3 }}><Paper className="asset-detail-card"><Typography variant="overline" color="primary">Lifecycle dates</Typography><Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>Purchased</Typography><Typography variant="body2">{asset.metadata?.purchaseDate || 'Not recorded'}</Typography><Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>Warranty / renewal</Typography><Typography variant="body2">{asset.metadata?.warrantyEnd || '—'} / {asset.metadata?.renewalDate || '—'}</Typography><Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>End of life</Typography><Typography variant="body2" color={dateAttention(asset) ? 'warning.main' : 'text.primary'}>{asset.metadata?.endOfLifeDate || 'Not recorded'}</Typography></Paper></Grid>
          <Grid size={{ xs: 12, md: 6, lg: 3 }}><Paper className="asset-detail-card"><Typography variant="overline" color="primary">Source and identity</Typography><Chip size="small" variant="outlined" label={asset.source || 'unknown'} /><Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.5 }}>External identifier</Typography><Typography variant="body2" sx={{ wordBreak: 'break-all' }}>{asset.externalId || 'Not recorded'}</Typography><Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>Last seen</Typography><Typography variant="body2" color={isStale(asset) ? 'warning.main' : 'text.primary'}>{freshness(asset)}</Typography></Paper></Grid>
        </Grid>}
        {detailTab === 'hardware' && <Stack spacing={2}>
          <InventorySection eyebrow="Device inventory" title="Hardware identity" copy="Provider-observed system identity and physical components."><InventoryFacts facts={inventory.hardware.facts} empty="No hardware identity has been collected for this asset." /></InventorySection>
          <InventorySection eyebrow="Operating system" title="Platform details" copy="The installed operating system and boot evidence reported by the source."><Stack spacing={2}><InventoryFacts facts={inventory.operatingSystem.facts} empty="No operating-system inventory has been collected." />{inventory.operatingSystem.capabilities.length ? <InventoryRecordTable title="OS capabilities" records={inventory.operatingSystem.capabilities} /> : null}</Stack></InventorySection>
          <InventorySection eyebrow="Component inventory" title="Physical components" copy="Detailed processor, memory, disk, volume and BIOS records reported for this configuration item.">{inventory.hardware.collections.some(item => item.records.length) ? <Stack spacing={2}>{inventory.hardware.collections.filter(item => item.records.length).map(item => <InventoryRecordTable key={item.key} title={item.label} records={item.records} />)}</Stack> : <Alert severity="info">No detailed hardware components have been supplied.</Alert>}</InventorySection>
        </Stack>}
        {detailTab === 'network' && <Stack spacing={2}>
          <InventorySection eyebrow="Network identity" title="Addressing and placement" copy="Hostname and provider-observed network placement."><InventoryFacts facts={inventory.network.facts} empty="No network identity has been collected for this asset." /></InventorySection>
          <InventorySection eyebrow="Interface inventory" title="Network adapters" copy="All reported interfaces are retained; an interface does not imply a physical topology link."><InventoryRecordTable title="Network interfaces" records={inventory.network.interfaces} empty="No network adapters have been reported." /></InventorySection>
        </Stack>}
        {detailTab === 'software' && <Stack spacing={2}>
          <InventorySection eyebrow="Software inventory" title="Inventory summary" copy="Counts and collection metadata supplied by the provider."><InventoryFacts facts={inventory.software.summary} empty="No software summary has been supplied." /></InventorySection>
          <InventorySection eyebrow="Installed applications" title="Applications" copy="Detected applications and versions. Large inventories are capped on screen to keep this page responsive."><InventoryRecordTable title="Applications" records={inventory.software.applications} /></InventorySection>
          <InventorySection eyebrow="Server capabilities" title="Roles and features" copy="Only explicit Windows Server role and feature inventory is shown here. General N-central OS properties appear under Hardware as OS capabilities."><Stack spacing={2}><InventoryRecordTable title="Server roles" records={inventory.software.roles} empty="The current provider response did not report Windows Server roles." /><InventoryRecordTable title="Server features" records={inventory.software.features} empty="The current provider response did not report Windows Server features." /></Stack></InventorySection>
          {inventory.software.patches.length ? <InventorySection eyebrow="Update inventory" title="Detected patches" copy="Provider-observed operating-system updates retained as technical evidence."><InventoryRecordTable title="Patches" records={inventory.software.patches} /></InventorySection> : null}
        </Stack>}
        {detailTab === 'virtualization' && <Stack spacing={2}>
          <InventorySection eyebrow="Virtualization" title="Workload placement" copy="Provider-observed virtualization role, platform and placement evidence."><InventoryFacts facts={inventory.virtualization.facts} empty="No explicit virtualization placement has been collected." /></InventorySection>
          <InventorySection eyebrow="Explainable classification" title="Classification evidence" copy="Suggestions remain evidence-backed so technicians can validate ambiguous hosts and guests.">
            {inventory.virtualization.classification.proposedType || inventory.virtualization.classification.evidence?.length ? <Stack spacing={1.5}><Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ alignItems: { sm: 'center' } }}><Chip color="info" label={inventory.virtualization.classification.proposedType || 'Classification candidate'} /><Typography variant="body2" color="text.secondary">{inventory.virtualization.classification.confidence ? `${Math.round(inventory.virtualization.classification.confidence * (inventory.virtualization.classification.confidence <= 1 ? 100 : 1))}% confidence` : 'Confidence not scored'}</Typography></Stack>{Boolean(inventory.virtualization.classification.confidence) && <LinearProgress variant="determinate" value={Math.min(100, inventory.virtualization.classification.confidence! * (inventory.virtualization.classification.confidence! <= 1 ? 100 : 1))} aria-label="Virtualization classification confidence" />}{inventory.virtualization.classification.evidence?.map((evidence, index) => <Alert key={`${evidence}-${index}`} severity="info" icon={false}>{evidence}</Alert>)}</Stack> : <Alert severity="info">No virtualization classification evidence is available for this asset.</Alert>}
          </InventorySection>
        </Stack>}
        {detailTab === 'monitoring' && <Stack spacing={2}>
          <InventorySection eyebrow="Monitoring evidence" title="Collection freshness" copy="Operational observations are shown separately from the ITIL lifecycle state."><InventoryFacts facts={inventory.monitoring.facts} empty="No monitoring freshness evidence has been collected." /></InventorySection>
          <Grid container spacing={2}><Grid size={{ xs: 12, md: 6 }}><InventorySection eyebrow="Services" title="Service health" copy="Summary of provider monitoring services."><InventoryFacts facts={inventory.monitoring.serviceSummary} empty="No service-health summary has been supplied." /></InventorySection></Grid><Grid size={{ xs: 12, md: 6 }}><InventorySection eyebrow="Active issues" title="Attention summary" copy="Current issue counts and severity evidence from the monitoring source."><InventoryFacts facts={inventory.monitoring.activeIssueSummary} empty="No active-issue summary has been supplied." /></InventorySection></Grid></Grid>
          {inventory.monitoring.observations.length ? <InventorySection eyebrow="Provider observations" title="Monitoring records" copy="Detailed provider monitoring evidence associated with this configuration item."><InventoryRecordTable title="Monitoring records" records={inventory.monitoring.observations} /></InventorySection> : null}
          <InventorySection eyebrow="Maintenance" title="Maintenance windows" copy="Known maintenance windows affecting monitoring and operational interpretation."><InventoryRecordTable title="Maintenance windows" records={inventory.monitoring.maintenanceWindows} /></InventorySection>
        </Stack>}
        {detailTab === 'source' && <Stack spacing={2}>
          <InventorySection eyebrow="Source provenance" title="Observation evidence" copy="Stable source identity, collection time and fingerprint for reconciliation and audit."><InventoryFacts facts={[
            { label: 'Provider', value: inventory.sourceEvidence.provider || asset.source },
            { label: 'Observed at', value: inventory.sourceEvidence.observedAt || asset.lastSeen },
            { label: 'External identifier', value: asset.externalId },
            { label: 'Fingerprint', value: inventory.sourceEvidence.fingerprint },
          ].filter(fact => fact.value)} empty="No source provenance is recorded." /></InventorySection>
          <InventorySection eyebrow="Coverage" title="Provider sections" copy="Coverage indicates what the source observed; absent sections are not treated as confirmed-empty data."><Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>{Object.keys(inventory.sourceEvidence.coverage || {}).length ? Object.entries(inventory.sourceEvidence.coverage || {}).map(([key, value]) => <Chip key={key} variant="outlined" label={`${humanizeInventoryKey(key)}: ${formatInventoryValue(value, key)}`} />) : inventory.coverage.map(section => <Chip key={section.key} color={section.available ? 'success' : 'default'} variant="outlined" label={`${section.label}: ${section.available ? 'observed' : 'not supplied'}`} />)}</Stack></InventorySection>
          {inventory.sourceEvidence.snapshots?.length ? <InventorySection eyebrow="Snapshot history" title="Inventory snapshots" copy="Collection fingerprints and observation windows retained for reconciliation."><InventoryRecordTable title="Inventory snapshots" records={inventory.sourceEvidence.snapshots} /></InventorySection> : null}
          {inventory.sourceEvidence.partialErrors?.length ? <InventorySection eyebrow="Partial collection" title="Collection notes" copy="Some source sections could not be collected; retained data remains visible."><Stack spacing={1}>{inventory.sourceEvidence.partialErrors.map((error, index) => <Alert key={`${error}-${index}`} severity="warning">{error}</Alert>)}</Stack></InventorySection> : <Alert severity="success">No partial collection errors were reported.</Alert>}
        </Stack>}
      </Box>
      <Paper variant="outlined" sx={{ mt: 2, p: 2.5 }}>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'center' }, mb: 2 }}>
          <Box><Typography variant="overline" color="primary">Change enablement</Typography><Typography variant="h5">Change activity</Typography><Typography variant="body2" color="text.secondary">Planned and historical changes where this asset was explicitly selected or calculated as affected.</Typography></Box>
          <Stack direction="row" spacing={1}><Button variant="outlined" onClick={() => openChanges(`affectedAssetId=${encodeURIComponent(asset.id)}`)}>View all history</Button><Button variant="contained" startIcon={<AssignmentOutlined />} onClick={() => openChanges(`assetId=${encodeURIComponent(asset.id)}`)}>Create change</Button></Stack>
        </Stack>
        {changesLoading ? <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}><CircularProgress size={22} /><Typography color="text.secondary">Loading change activity…</Typography></Stack>
          : changesError ? <Alert severity="error">{changesError}</Alert>
            : !orderedChanges.length ? <Alert severity="info">No changes are currently linked to this asset.</Alert>
              : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>Change</TableCell><TableCell>Status</TableCell><TableCell>Schedule</TableCell><TableCell>Asset involvement</TableCell><TableCell>Impact</TableCell><TableCell align="right">Action</TableCell></TableRow></TableHead><TableBody>{orderedChanges.slice(0, 8).map(change => {
                const impact = changeImpactForAsset(change, asset.id);
                return <TableRow key={change.id} hover><TableCell><Typography sx={{ fontWeight: 800 }}>{change.number}</Typography><Typography variant="body2">{change.title}</Typography></TableCell><TableCell><Chip size="small" color={changeStatusColor(change.status)} label={change.status.replaceAll('_', ' ')} /></TableCell><TableCell>{change.plannedStart ? new Date(change.plannedStart).toLocaleString() : 'Not scheduled'}</TableCell><TableCell><Chip size="small" variant={impact?.role === 'Scope' ? 'filled' : 'outlined'} label={impact?.role || (change.scopeAssetIds.includes(asset.id) ? 'Scope' : 'Affected')} /></TableCell><TableCell><Chip size="small" color={impact?.impactSeverity === 'outage' ? 'error' : impact?.impactSeverity === 'degraded' ? 'warning' : impact?.impactSeverity === 'protected' ? 'success' : 'default'} label={(impact?.impactSeverity || 'scope').replaceAll('_', ' ')} /></TableCell><TableCell align="right"><Button size="small" startIcon={<VisibilityOutlined />} onClick={() => openChanges(`changeId=${encodeURIComponent(change.id)}`)}>Open change</Button></TableCell></TableRow>;
              })}</TableBody></Table></TableContainer>}
      </Paper>
      <AuditTimeline entityType="configuration_item" entityId={asset.id} companyId={asset.companyId} />
    </Box>
  );
}

export function AssetShow() {
  const { assetId = '' } = useParams();
  const [asset, setAsset] = useState<Asset | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [inventoryError, setInventoryError] = useState('');
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    setInventoryError('');
    const inventoryRequest = apiFetch<AssetInventoryPayload>(`${assetResourcePath(assetId)}/inventory`)
      .then(payload => ({ payload, error: '' }))
      .catch(reason => {
        if (reason instanceof ApiError && reason.status === 404) return { payload: null, error: '' };
        return {
          payload: null,
          error: reason instanceof Error
            ? reason.message
            : 'Detailed technical inventory could not be loaded.',
        };
      });
    Promise.all([apiFetch<Asset>(assetResourcePath(assetId)), inventoryRequest])
      .then(([record, inventoryResult]) => {
        if (!active) return;
        const inlineInventory = record.inventoryCollections
          ? { assetId: record.id, collections: record.inventoryCollections }
          : null;
        setAsset(mergeAssetInventoryPayload(record, inventoryResult.payload || inlineInventory));
        setInventoryError(inventoryResult.error);
      })
      .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : 'The asset could not be loaded.'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [assetId]);
  if (loading) return <Stack sx={{ alignItems: 'center', py: 8 }}><CircularProgress /><Typography color="text.secondary">Loading asset…</Typography></Stack>;
  if (!asset) return <Alert severity="error">{error || 'Asset not found.'}</Alert>;
  return <><Title title={asset.name} /><AssetShowContent asset={asset} inventoryError={inventoryError} /></>;
}
