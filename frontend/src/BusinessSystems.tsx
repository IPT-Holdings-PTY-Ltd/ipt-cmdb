import AccountTreeOutlined from '@mui/icons-material/AccountTreeOutlined';
import AddOutlined from '@mui/icons-material/AddOutlined';
import BusinessCenterOutlined from '@mui/icons-material/BusinessCenterOutlined';
import EditOutlined from '@mui/icons-material/EditOutlined';
import {
  Alert, Box, Button, Card, CardActions, CardContent, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, FormControl, Grid, InputLabel, MenuItem, Select, Stack, TextField, Typography,
} from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { Title } from 'react-admin';
import { useNavigate } from 'react-router-dom';
import { apiFetch, getSession } from './session';
import type { Asset, AssetMetadata, Relationship } from './types';
import { useWorkspace } from './workspace';

type BusinessSystemDraft = {
  id?: string;
  name: string;
  status: string;
  metadata: Partial<AssetMetadata>;
};

const blankDraft = (): BusinessSystemDraft => ({
  name: '', status: 'Active', metadata: {
    lifecycle: 'in_service', operationalStatus: 'healthy', criticality: 'high', environment: 'production',
    dataClassification: 'internal', customerFacing: 'no', signoffRequired: 'yes',
  },
});

const selectOptions = {
  criticality: ['low', 'medium', 'high', 'critical'],
  operationalStatus: ['unknown', 'healthy', 'warning', 'critical', 'offline'],
  dataClassification: ['public', 'internal', 'confidential', 'restricted'],
};

function dependencyCount(assetId: string, relationships: Relationship[]) {
  return relationships.filter(item => item.fromId === assetId && ['depends_on', 'installed_on'].includes(item.type)).length;
}

export function BusinessSystemsPage() {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const canEdit = ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');
  const [assets, setAssets] = useState<Asset[]>([]);
  const [relationships, setRelationships] = useState<Relationship[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState<BusinessSystemDraft | null>(null);

  const load = async () => {
    setLoading(true); setError('');
    try {
      const suffix = `?companyId=${encodeURIComponent(workspace.companyId)}`;
      const [assetData, relationshipData] = await Promise.all([
        apiFetch<Asset[]>(`/api/assets${suffix}`), apiFetch<Relationship[]>(`/api/relationships${suffix}`),
      ]);
      setAssets(assetData); setRelationships(relationshipData);
    } catch (value) { setError(value instanceof Error ? value.message : 'Business systems could not be loaded.'); }
    finally { setLoading(false); }
  };

  useEffect(() => { if (!workspace.isRoot) void load(); }, [workspace.companyId, workspace.isRoot]);
  const systems = useMemo(() => assets.filter(asset => asset.type === 'Business system').sort((a, b) => a.name.localeCompare(b.name)), [assets]);

  const editSystem = (asset: Asset) => setDraft({ id: asset.id, name: asset.name, status: asset.status, metadata: { ...asset.metadata } });
  const setMetadata = (field: keyof AssetMetadata, value: string) => setDraft(current => current ? ({ ...current, metadata: { ...current.metadata, [field]: value } }) : current);
  const save = async () => {
    if (!draft?.name.trim()) { setError('Enter a business system name.'); return; }
    setSaving(true); setError('');
    try {
      const body = JSON.stringify({ name: draft.name.trim(), type: 'Business system', status: draft.status, metadata: draft.metadata, ...(draft.id ? {} : { companyId: workspace.companyId, fields: {} }) });
      await apiFetch(draft.id ? `/api/assets/${draft.id}` : '/api/assets', { method: draft.id ? 'PATCH' : 'POST', body });
      setDraft(null); await load();
    } catch (value) { setError(value instanceof Error ? value.message : 'The business system could not be saved.'); }
    finally { setSaving(false); }
  };

  if (workspace.isRoot) return <Alert severity="info">Choose a customer workspace to manage its business systems.</Alert>;
  return (
    <Box>
      <Title title="Business systems" />
      <Stack
        direction={{ xs: 'column', sm: 'row' }}
        spacing={2}
        sx={{
          justifyContent: "space-between",
          alignItems: { xs: 'stretch', sm: 'flex-end' },
          mb: 3
        }}>
        <Box><Typography variant="overline" color="primary">Business service portfolio</Typography><Typography variant="h3">Business systems</Typography><Typography sx={{
          color: "text.secondary"
        }}>Connect systems people recognise to the technical CIs that keep them running.</Typography></Box>
        {canEdit && <Button variant="contained" startIcon={<AddOutlined />} onClick={() => setDraft(blankDraft())}>Add business system</Button>}
      </Stack>
      {error && <Alert severity="error" onClose={() => setError('')} sx={{ mb: 2 }}>{error}</Alert>}
      {loading ? <Stack
        spacing={2}
        sx={{
          alignItems: "center",
          py: 8
        }}><CircularProgress /><Typography sx={{
        color: "text.secondary"
      }}>Loading the service portfolio…</Typography></Stack> : !systems.length ? <Alert severity="info" action={canEdit ? <Button color="inherit" onClick={() => setDraft(blankDraft())}>Add the first one</Button> : undefined}>No business systems are recorded for this customer yet.</Alert> : <Grid container spacing={2}>
        {systems.map(system => {
          const health = system.metadata?.operationalStatus || 'unknown';
          const dependencies = dependencyCount(system.id, relationships);
          return (
            <Grid key={system.id} size={{ xs: 12, lg: 6 }}><Card variant="outlined" sx={{ height: '100%', borderTop: `3px solid ${system.metadata?.criticality === 'critical' ? '#ff7961' : '#50d5b9'}` }}>
              <CardContent>
                <Stack direction="row" spacing={2} sx={{
                  justifyContent: "space-between"
                }}><Stack direction="row" spacing={1.5}><BusinessCenterOutlined color="primary" /><Box><Typography variant="h6">{system.name}</Typography><Typography variant="body2" sx={{
                  color: "text.secondary"
                }}>{system.metadata?.aliases || system.metadata?.department || 'Business service'}</Typography></Box></Stack><Chip size="small" color={health === 'healthy' ? 'success' : health === 'critical' || health === 'offline' ? 'error' : 'warning'} label={health} /></Stack>
                <Typography variant="body2" sx={{ mt: 2, minHeight: 40 }}>{system.metadata?.businessDescription || 'No business description recorded.'}</Typography>
                <Grid container spacing={1.5} sx={{ mt: 1 }}>
                  <Grid size={{ xs: 6, sm: 3 }}><Typography variant="caption" sx={{
                    color: "text.secondary"
                  }}>Criticality</Typography><Typography variant="body2">{system.metadata?.criticality || 'medium'}</Typography></Grid>
                  <Grid size={{ xs: 6, sm: 3 }}><Typography variant="caption" sx={{
                    color: "text.secondary"
                  }}>Dependencies</Typography><Typography variant="body2">{dependencies || 'None linked'}</Typography></Grid>
                  <Grid size={{ xs: 6, sm: 3 }}><Typography variant="caption" sx={{
                    color: "text.secondary"
                  }}>RTO / RPO</Typography><Typography variant="body2">{system.metadata?.rtoHours || '?'}h / {system.metadata?.rpoHours || '?'}h</Typography></Grid>
                  <Grid size={{ xs: 6, sm: 3 }}><Typography variant="caption" sx={{
                    color: "text.secondary"
                  }}>Users</Typography><Typography variant="body2">{system.metadata?.userPopulation || 'Not recorded'}</Typography></Grid>
                </Grid>
                <Box sx={{ mt: 2, p: 1.5, bgcolor: 'rgba(121,151,255,.08)', borderRadius: 1 }}><Typography variant="caption" sx={{
                  color: "text.secondary"
                }}>Business owner / signoff</Typography><Typography variant="body2">{system.metadata?.businessOwner || 'Business owner missing'} · {system.metadata?.signoffRequired === 'no' ? 'Signoff not required' : system.metadata?.signoffDelegate || 'Owner signs off'}</Typography></Box>
              </CardContent>
              <CardActions sx={{ px: 2, pb: 2 }}><Button startIcon={<AccountTreeOutlined />} onClick={() => navigate(`/relationships?assetId=${encodeURIComponent(system.id)}&view=business`)}>View dependencies</Button>{canEdit && <Button startIcon={<EditOutlined />} onClick={() => editSystem(system)}>Edit</Button>}</CardActions>
            </Card></Grid>
          );
        })}
      </Grid>}

      <Dialog open={Boolean(draft)} onClose={() => !saving && setDraft(null)} maxWidth="md" fullWidth>
        <DialogTitle>{draft?.id ? 'Edit business system' : 'Add business system'}</DialogTitle>
        <DialogContent dividers>{draft && <Stack spacing={3}>
          <Box><Typography variant="subtitle2" sx={{ mb: 1.5 }}>Identity and purpose</Typography><Grid container spacing={2}>
            <Grid size={{ xs: 12, md: 8 }}><TextField fullWidth required label="Business system name" value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} helperText="Use the name employees and approvers recognise, for example Sage 200." /></Grid>
            <Grid size={{ xs: 12, md: 4 }}><FormControl fullWidth><InputLabel>Status</InputLabel><Select label="Status" value={draft.status} onChange={event => setDraft({ ...draft, status: event.target.value })}>{['Active', 'Planned', 'Retired'].map(value => <MenuItem key={value} value={value}>{value}</MenuItem>)}</Select></FormControl></Grid>
            <Grid size={{ xs: 12 }}><TextField fullWidth multiline minRows={2} label="Business description" value={draft.metadata.businessDescription || ''} onChange={event => setMetadata('businessDescription', event.target.value)} /></Grid>
            <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Aliases" value={draft.metadata.aliases || ''} onChange={event => setMetadata('aliases', event.target.value)} helperText="Other names, abbreviations or legacy names." /></Grid>
            <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Department" value={draft.metadata.department || ''} onChange={event => setMetadata('department', event.target.value)} /></Grid>
          </Grid></Box>
          <Box><Typography variant="subtitle2" sx={{ mb: 1.5 }}>Ownership and approval</Typography><Grid container spacing={2}>
            <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth required label="Business owner" value={draft.metadata.businessOwner || ''} onChange={event => setMetadata('businessOwner', event.target.value)} /></Grid>
            <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Service owner" value={draft.metadata.serviceOwner || ''} onChange={event => setMetadata('serviceOwner', event.target.value)} /></Grid>
            <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Technical owner" value={draft.metadata.technicalOwner || ''} onChange={event => setMetadata('technicalOwner', event.target.value)} /></Grid>
            <Grid size={{ xs: 12, md: 8 }}><TextField fullWidth label="Signoff delegate" value={draft.metadata.signoffDelegate || ''} onChange={event => setMetadata('signoffDelegate', event.target.value)} helperText="Optional person who can approve changes for the business owner." /></Grid>
            <Grid size={{ xs: 12, md: 4 }}><FormControl fullWidth><InputLabel>Business signoff</InputLabel><Select label="Business signoff" value={draft.metadata.signoffRequired || 'yes'} onChange={event => setMetadata('signoffRequired', event.target.value)}><MenuItem value="yes">Required</MenuItem><MenuItem value="no">Not required</MenuItem></Select></FormControl></Grid>
          </Grid></Box>
          <Box><Typography variant="subtitle2" sx={{ mb: 1.5 }}>Impact and recovery</Typography><Grid container spacing={2}>
            <Grid size={{ xs: 12, md: 3 }}><FormControl fullWidth><InputLabel>Criticality</InputLabel><Select label="Criticality" value={draft.metadata.criticality || 'high'} onChange={event => setMetadata('criticality', event.target.value)}>{selectOptions.criticality.map(value => <MenuItem key={value} value={value}>{value}</MenuItem>)}</Select></FormControl></Grid>
            <Grid size={{ xs: 12, md: 3 }}><FormControl fullWidth><InputLabel>Health</InputLabel><Select label="Health" value={draft.metadata.operationalStatus || 'healthy'} onChange={event => setMetadata('operationalStatus', event.target.value)}>{selectOptions.operationalStatus.map(value => <MenuItem key={value} value={value}>{value}</MenuItem>)}</Select></FormControl></Grid>
            <Grid size={{ xs: 6, md: 3 }}><TextField fullWidth label="RTO (hours)" value={draft.metadata.rtoHours || ''} onChange={event => setMetadata('rtoHours', event.target.value)} /></Grid>
            <Grid size={{ xs: 6, md: 3 }}><TextField fullWidth label="RPO (hours)" value={draft.metadata.rpoHours || ''} onChange={event => setMetadata('rpoHours', event.target.value)} /></Grid>
            <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="User population" value={draft.metadata.userPopulation || ''} onChange={event => setMetadata('userPopulation', event.target.value)} /></Grid>
            <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Support hours" value={draft.metadata.supportHours || ''} onChange={event => setMetadata('supportHours', event.target.value)} /></Grid>
            <Grid size={{ xs: 12, md: 2 }}><FormControl fullWidth><InputLabel>Data</InputLabel><Select label="Data" value={draft.metadata.dataClassification || 'internal'} onChange={event => setMetadata('dataClassification', event.target.value)}>{selectOptions.dataClassification.map(value => <MenuItem key={value} value={value}>{value}</MenuItem>)}</Select></FormControl></Grid>
            <Grid size={{ xs: 12, md: 2 }}><FormControl fullWidth><InputLabel>Customer-facing</InputLabel><Select label="Customer-facing" value={draft.metadata.customerFacing || 'no'} onChange={event => setMetadata('customerFacing', event.target.value)}><MenuItem value="yes">Yes</MenuItem><MenuItem value="no">No</MenuItem></Select></FormControl></Grid>
          </Grid></Box>
        </Stack>}</DialogContent>
        <DialogActions><Button onClick={() => setDraft(null)} disabled={saving}>Cancel</Button><Button variant="contained" onClick={() => void save()} disabled={saving}>{saving ? 'Saving…' : 'Save business system'}</Button></DialogActions>
      </Dialog>
    </Box>
  );
}
