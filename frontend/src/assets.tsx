import AccountTreeOutlined from '@mui/icons-material/AccountTreeOutlined';
import AddOutlined from '@mui/icons-material/AddOutlined';
import EditOutlined from '@mui/icons-material/EditOutlined';
import FileDownloadOutlined from '@mui/icons-material/FileDownloadOutlined';
import Inventory2Outlined from '@mui/icons-material/Inventory2Outlined';
import SearchOutlined from '@mui/icons-material/SearchOutlined';
import VisibilityOutlined from '@mui/icons-material/VisibilityOutlined';
import WarningAmberOutlined from '@mui/icons-material/WarningAmberOutlined';
import {
  Alert, Box, Button, Chip, CircularProgress, Divider, FormControl, Grid, IconButton, InputAdornment, InputLabel,
  MenuItem, Paper, Select, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  TextField as MuiTextField, Tooltip, Typography,
} from '@mui/material';
import {
  Create,
  DateInput,
  Edit,
  FormDataConsumer,
  SelectInput,
  Show,
  SimpleForm,
  TextInput,
  Title,
  useRecordContext,
} from 'react-admin';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiFetch, getSession } from './session';
import { businessApplicationMemberships, displayLayer, displayLayerColors, displayLayerLabels, displayLayerOrder, type DisplayLayer } from './topology';
import type { Asset, Relationship } from './types';
import { useWorkspace } from './workspace';
import { AuditTimeline } from './Governance';

const types = [
  'Business system', 'Device', 'Server', 'Virtual machine', 'Hypervisor host', 'Virtualization cluster',
  'Datastore', 'Storage array', 'Virtual network', 'Virtualization manager', 'Workstation', 'Network device',
  'Network service', 'Network zone', 'VPN tunnel',
  'Software', 'Licence', 'Service', 'Credential owner',
].map(id => ({ id, name: id }));
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

function AssetForm() {
  return (
    <SimpleForm defaultValues={{ status: 'Active', type: 'Device', metadata: { lifecycle: 'in_service', operationalStatus: 'healthy', criticality: 'medium', environment: 'production' } }}>
      <Typography variant="h6" className="form-section">Identity and classification</Typography>
      <Grid container spacing={2} sx={{
        width: "100%"
      }}>
        <Grid size={{ xs: 12, md: 6 }}><TextInput source="name" fullWidth required /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><SelectInput source="type" choices={types} fullWidth required /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><SelectInput source="status" choices={statuses} fullWidth required /></Grid>
      </Grid>
      <Typography variant="h6" className="form-section">Lifecycle and service health</Typography>
      <Grid container spacing={2} sx={{
        width: "100%"
      }}>
        <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.lifecycle" choices={lifecycle} fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.operationalStatus" label="Operational status" choices={operational} fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.criticality" choices={criticality} fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.environment" choices={environments} fullWidth /></Grid>
      </Grid>
      <Typography variant="h6" className="form-section">Ownership and location</Typography>
      <Grid container spacing={2} sx={{
        width: "100%"
      }}>
        <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.technicalOwner" label="Technical owner" fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.serviceOwner" label="Service owner" fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.custodian" fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 6 }}><TextInput source="metadata.site" fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><TextInput source="metadata.vendor" fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><TextInput source="metadata.model" fullWidth /></Grid>
      </Grid>
      <Typography variant="h6" className="form-section">Relationship display</Typography>
      <Grid container spacing={2} sx={{
        width: "100%"
      }}>
        <Grid size={{ xs: 12, md: 4 }}><SelectInput source="metadata.displayLayer" label="Display layer" choices={displayLayers} helperText="Leave automatic unless this CI belongs in another full-stack lane." fullWidth /></Grid>
      </Grid>
      <FormDataConsumer>{({ formData }) => networkTypes.has(formData?.type) ? <Box sx={{
        width: "100%"
      }}>
        <Typography variant="h6" className="form-section">Network placement</Typography>
        <Grid container spacing={2} sx={{
          width: "100%"
        }}>
          <Grid size={{ xs: 12, md: 3 }}><TextInput source="metadata.networkZone" label="Zone" helperText="For example WAN, Edge, Core, Access or Finance." fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 3 }}><TextInput source="metadata.networkRole" label="Network role" fullWidth /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><TextInput source="metadata.vlanId" label="VLAN" fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.subnet" label="Subnet / prefix" fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.ipAddress" label="Management / primary IP" fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.redundancyGroup" label="Redundancy group" fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.redundancyRole" label="Redundancy role" helperText="For example active, passive or member 1." fullWidth /></Grid>
        </Grid>
      </Box> : null}</FormDataConsumer>
      <Typography variant="h6" className="form-section">Commercial and review dates</Typography>
      <Grid container spacing={2} sx={{
        width: "100%"
      }}>
        <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.purchaseDate" label="Purchase date" fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.warrantyEnd" label="Warranty end" fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.renewalDate" label="Renewal date" fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.endOfLifeDate" label="End-of-life date" fullWidth /></Grid>
        <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.reviewDate" label="Review date" fullWidth /></Grid>
      </Grid>
      <FormDataConsumer>{({ formData }) => virtualizationTypes.has(formData?.type) ? <Box sx={{
        width: "100%"
      }}>
        <Typography variant="h6" className="form-section">Virtualization and resilience</Typography>
        <Grid container spacing={2} sx={{
          width: "100%"
        }}>
          <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.virtualizationPlatform" label="Platform" helperText="For example VMware vSphere, Hyper-V or Azure." fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.clusterName" label="Cluster" fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><SelectInput source="metadata.powerState" label="Power state" choices={powerStates} fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.haEnabled" label="HA enabled" choices={[{ id: 'yes', name: 'Yes' }, { id: 'no', name: 'No' }]} fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.protectionStatus" label="Protection" choices={protectionStates} fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.mobility" label="Mobility" choices={mobilityStates} fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.capacityStatus" label="Failover capacity" choices={capacityStates} fullWidth /></Grid>
          <Grid size={{ xs: 6, md: 3 }}><TextInput source="metadata.minimumHosts" label="Minimum surviving hosts" fullWidth /></Grid>
          <Grid size={{ xs: 6, md: 3 }}><SelectInput source="metadata.maintenanceMode" label="Maintenance mode" choices={[{ id: 'yes', name: 'Yes' }, { id: 'no', name: 'No' }]} fullWidth /></Grid>
          <Grid size={{ xs: 12, md: 6 }}><TextInput source="metadata.guestOs" label="Guest operating system" fullWidth /></Grid>
          <Grid size={{ xs: 4 }}><TextInput source="metadata.cpuCount" label="vCPU" fullWidth /></Grid>
          <Grid size={{ xs: 4 }}><TextInput source="metadata.memoryGb" label="Memory (GB)" fullWidth /></Grid>
          <Grid size={{ xs: 4 }}><TextInput source="metadata.storageGb" label="Storage (GB)" fullWidth /></Grid>
        </Grid>
      </Box> : null}</FormDataConsumer>
    </SimpleForm>
  );
}

export function AssetCreate() { return <Create redirect="list"><AssetForm /></Create>; }
export function AssetEdit() { return <Edit mutationMode="pessimistic"><AssetForm /></Edit>; }

function AssetShowContent() {
  const asset = useRecordContext<Asset>();
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const canEdit = !workspace.isRoot && ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');
  const [applications, setApplications] = useState<Asset[]>([]);
  const [directRelationships, setDirectRelationships] = useState(0);
  useEffect(() => {
    if (!asset) return;
    let active = true;
    const loadContext = async () => {
      try {
        const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
        const [assets, relationships] = await Promise.all([apiFetch<Asset[]>(`/api/assets${suffix}`), apiFetch<Relationship[]>(`/api/relationships${suffix}`)]);
        if (!active) return;
        setApplications(businessApplicationMemberships(assets, relationships).get(asset.id) || []);
        setDirectRelationships(relationships.filter(item => item.fromId === asset.id || item.toId === asset.id).length);
      } catch { if (active) { setApplications([]); setDirectRelationships(0); } }
    };
    void loadContext();
    return () => { active = false; };
  }, [asset?.id, workspace.companyId, workspace.isRoot]);
  if (!asset) return null;
  const layer = displayLayer(asset); const health = asset.metadata?.operationalStatus || 'unknown'; const owner = primaryOwner(asset);
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
        <Stack direction="row" spacing={1}><Button variant="outlined" startIcon={<AccountTreeOutlined />} onClick={() => navigate(`/relationships?view=technical&assetId=${encodeURIComponent(asset.id)}`)}>Relationships ({directRelationships})</Button><Button variant="outlined" onClick={() => navigate(`/changes?assetId=${encodeURIComponent(asset.id)}`)}>Create change</Button>{canEdit && <Button variant="contained" startIcon={<EditOutlined />} onClick={() => navigate(`/assets/${asset.id}`)}>Edit</Button>}</Stack>
      </Stack>
      <Grid container spacing={2}>
        <Grid size={{ xs: 12, md: 6, lg: 3 }}><Paper className="asset-detail-card"><Typography variant="overline" color="primary">Ownership</Typography><Typography variant="h6">{owner || 'Unassigned'}</Typography><Typography variant="body2" sx={{
          color: "text.secondary"
        }}>{ownerRole(asset)}</Typography><Divider sx={{ my: 1.5 }} /><Typography variant="caption" sx={{
          color: "text.secondary"
        }}>Service owner</Typography><Typography variant="body2">{asset.metadata?.serviceOwner || 'Not recorded'}</Typography><Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block",
            mt: 1
          }}>Custodian</Typography><Typography variant="body2">{asset.metadata?.custodian || 'Not recorded'}</Typography></Paper></Grid>
        <Grid size={{ xs: 12, md: 6, lg: 3 }}><Paper className="asset-detail-card"><Typography variant="overline" color="primary">Placement</Typography><Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block"
          }}>Environment</Typography><Typography variant="body2">{asset.metadata?.environment || 'Not recorded'}</Typography><Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block",
            mt: 1
          }}>Site</Typography><Typography variant="body2">{asset.metadata?.site || 'Not recorded'}</Typography><Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block",
            mt: 1
          }}>Network</Typography><Typography variant="body2">{[asset.metadata?.networkZone, asset.metadata?.vlanId ? `VLAN ${asset.metadata.vlanId}` : '', asset.metadata?.ipAddress].filter(Boolean).join(' · ') || 'Not recorded'}</Typography></Paper></Grid>
        <Grid size={{ xs: 12, md: 6, lg: 3 }}><Paper className="asset-detail-card"><Typography variant="overline" color="primary">Lifecycle dates</Typography><Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block"
          }}>Purchased</Typography><Typography variant="body2">{asset.metadata?.purchaseDate || 'Not recorded'}</Typography><Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block",
            mt: 1
          }}>Warranty / renewal</Typography><Typography variant="body2">{asset.metadata?.warrantyEnd || '—'} / {asset.metadata?.renewalDate || '—'}</Typography><Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block",
            mt: 1
          }}>End of life</Typography><Typography variant="body2" color={dateAttention(asset) ? 'warning.main' : 'text.primary'}>{asset.metadata?.endOfLifeDate || 'Not recorded'}</Typography></Paper></Grid>
        <Grid size={{ xs: 12, md: 6, lg: 3 }}><Paper className="asset-detail-card"><Typography variant="overline" color="primary">Source and identity</Typography><Chip size="small" variant="outlined" label={asset.source || 'unknown'} /><Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block",
            mt: 1.5
          }}>External identifier</Typography><Typography variant="body2" sx={{ wordBreak: 'break-all' }}>{asset.externalId || 'Not recorded'}</Typography><Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block",
            mt: 1
          }}>Last seen</Typography><Typography variant="body2" color={isStale(asset) ? 'warning.main' : 'text.primary'}>{freshness(asset)}</Typography></Paper></Grid>
      </Grid>
      <AuditTimeline entityType="configuration_item" entityId={asset.id} companyId={asset.companyId} />
    </Box>
  );
}

export function AssetShow() { return <Show actions={false}><AssetShowContent /></Show>; }
