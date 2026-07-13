import AssignmentTurnedInOutlined from '@mui/icons-material/AssignmentTurnedInOutlined';
import DescriptionOutlined from '@mui/icons-material/DescriptionOutlined';
import DownloadOutlined from '@mui/icons-material/DownloadOutlined';
import RestartAltOutlined from '@mui/icons-material/RestartAltOutlined';
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Chip, CircularProgress, Divider, FormControl, Grid,
  InputLabel, MenuItem, Paper, Select, Stack, Step, StepLabel, Stepper, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { Title } from 'react-admin';
import { useSearchParams } from 'react-router-dom';
import { apiDownload, apiFetch, getSession } from './session';
import type { Asset, ChangeImpactPreview, ChangePackage } from './types';
import { useWorkspace } from './workspace';

type ChangeForm = {
  title: string;
  changeType: string;
  category: string;
  priority: string;
  riskLevel: string;
  outageExpected: string;
  plannedStart: string;
  plannedEnd: string;
  reason: string;
  businessImpact: string;
  implementationPlan: string;
  validationPlan: string;
  rollbackPlan: string;
  communicationStatus: string;
  communicationPlan: string;
  assignedTechnician: string;
  approver: string;
  notes: string;
};

const steps = ['Scope & schedule', 'Impact review', 'Execution plan', 'Review & PDF'];

function emptyForm(): ChangeForm {
  return {
    title: '', changeType: 'normal', category: 'infrastructure', priority: 'medium', riskLevel: 'suggested',
    outageExpected: 'no', plannedStart: '', plannedEnd: '', reason: '', businessImpact: '', implementationPlan: '',
    validationPlan: '', rollbackPlan: '', communicationStatus: 'required', communicationPlan: '',
    assignedTechnician: getSession()?.user.email || '', approver: '', notes: '',
  };
}

function sentence(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, match => match.toUpperCase());
}

function formatDate(value: string) {
  if (!value) return 'Not scheduled';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function downloadChange(change: ChangePackage) {
  const { blob, filename } = await apiDownload(`/api/changes/${encodeURIComponent(change.id)}/pdf`);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function ChangeControlPage() {
  const workspace = useWorkspace();
  const [searchParams] = useSearchParams();
  const [assets, setAssets] = useState<Asset[]>([]);
  const [changes, setChanges] = useState<ChangePackage[]>([]);
  const [scopeAssets, setScopeAssets] = useState<Asset[]>([]);
  const [form, setForm] = useState<ChangeForm>(emptyForm);
  const [activeStep, setActiveStep] = useState(0);
  const [preview, setPreview] = useState<ChangeImpactPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<ChangePackage | null>(null);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error' | 'info'; message: string } | null>(null);
  const canCreate = ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');

  const loadChanges = async () => {
    if (workspace.isRoot) return [];
    const records = await apiFetch<ChangePackage[]>(`/api/changes?companyId=${encodeURIComponent(workspace.companyId)}`);
    setChanges(records);
    return records;
  };

  useEffect(() => {
    setAssets([]); setChanges([]); setScopeAssets([]); setPreview(null); setSaved(null); setActiveStep(0); setForm(emptyForm()); setNotice(null);
    if (workspace.isRoot) return;
    Promise.all([
      apiFetch<Asset[]>(`/api/assets?companyId=${encodeURIComponent(workspace.companyId)}`),
      apiFetch<ChangePackage[]>(`/api/changes?companyId=${encodeURIComponent(workspace.companyId)}`),
    ]).then(([assetRecords, changeRecords]) => { setAssets(assetRecords); setChanges(changeRecords); })
      .catch(error => setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Change control could not be loaded.' }));
  }, [workspace.companyId, workspace.isRoot]);

  useEffect(() => {
    const assetId = searchParams.get('assetId');
    if (!assetId || !assets.length || scopeAssets.length) return;
    const selected = assets.find(asset => asset.id === assetId);
    if (selected) setScopeAssets([selected]);
  }, [assets, scopeAssets.length, searchParams]);

  useEffect(() => {
    let active = true;
    if (workspace.isRoot || !scopeAssets.length || !canCreate) { setPreview(null); return; }
    setPreviewLoading(true);
    apiFetch<ChangeImpactPreview>('/api/changes/impact-preview', {
      method: 'POST',
      body: JSON.stringify({ companyId: workspace.companyId, scopeAssetIds: scopeAssets.map(asset => asset.id), outageExpected: form.outageExpected === 'yes' }),
    }).then(value => { if (active) setPreview(value); })
      .catch(error => { if (active) setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Impact could not be calculated.' }); })
      .finally(() => { if (active) setPreviewLoading(false); });
    return () => { active = false; };
  }, [workspace.companyId, workspace.isRoot, scopeAssets, form.outageExpected, canCreate]);

  const update = (field: keyof ChangeForm, value: string) => {
    setSaved(null);
    setForm(current => ({ ...current, [field]: value }));
  };

  const reset = () => {
    setForm(emptyForm()); setScopeAssets([]); setPreview(null); setSaved(null); setActiveStep(0); setNotice(null);
  };

  const stepComplete = useMemo(() => {
    if (activeStep === 0) return Boolean(form.title.trim() && scopeAssets.length && (!form.plannedStart || !form.plannedEnd || form.plannedEnd > form.plannedStart));
    if (activeStep === 1) return Boolean(preview && form.businessImpact.trim());
    if (activeStep === 2) return Boolean(form.reason.trim() && form.implementationPlan.trim() && form.validationPlan.trim() && form.rollbackPlan.trim());
    return true;
  }, [activeStep, form, preview, scopeAssets.length]);

  const saveAndDownload = async () => {
    if (!preview) return;
    setSaving(true); setNotice(null);
    try {
      const change = await apiFetch<ChangePackage>('/api/changes', {
        method: 'POST',
        body: JSON.stringify({
          ...form,
          companyId: workspace.companyId,
          scopeAssetIds: scopeAssets.map(asset => asset.id),
          outageExpected: form.outageExpected === 'yes',
          riskLevel: form.riskLevel === 'suggested' ? '' : form.riskLevel,
        }),
      });
      setSaved(change);
      await loadChanges();
      await downloadChange(change);
      setNotice({ severity: 'success', message: `${change.number} was saved and its PDF was generated.` });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The change package could not be generated.' });
    } finally { setSaving(false); }
  };

  if (workspace.isRoot) return <Box><Title title="Change control" /><Typography variant="overline" color="primary">Operations</Typography><Typography variant="h3" sx={{ mb: 3 }}>Change control</Typography><Alert severity="info">Select a customer workspace to create or review customer change packages.</Alert></Box>;

  return <Box>
    <Title title={`${workspace.companyName} change control`} />
    <Stack direction={{ xs: 'column', md: 'row' }} justifyContent="space-between" alignItems={{ md: 'flex-end' }} spacing={2} sx={{ mb: 3 }}>
      <Box><Typography variant="overline" color="primary">Change enablement</Typography><Typography variant="h3">Change control</Typography><Typography color="text.secondary">Create an impact-aware, customer-ready change package from CMDB relationships.</Typography></Box>
      <Button variant="outlined" startIcon={<RestartAltOutlined />} onClick={reset}>New change</Button>
    </Stack>
    {notice && <Alert severity={notice.severity} onClose={() => setNotice(null)} sx={{ mb: 2 }}>{notice.message}</Alert>}
    {!canCreate && <Alert severity="info" sx={{ mb: 2 }}>You can review and download existing change packages. An MSP operator or platform administrator must generate new packages.</Alert>}

    {canCreate && <Card className="change-wizard">
      <CardContent>
        <Stepper activeStep={activeStep} alternativeLabel sx={{ mb: 4 }}>{steps.map(label => <Step key={label}><StepLabel>{label}</StepLabel></Step>)}</Stepper>

        {activeStep === 0 && <Grid container spacing={2.5}>
          <Grid size={{ xs: 12 }}><TextField fullWidth required label="Change title" value={form.title} onChange={event => update('title', event.target.value)} helperText="Use a short outcome-focused title that will also work as a future ConnectWise ticket summary." /></Grid>
          <Grid size={{ xs: 12 }}><Autocomplete multiple options={assets} value={scopeAssets} getOptionLabel={option => `${option.name} (${option.type})`} isOptionEqualToValue={(option, value) => option.id === value.id} onChange={(_, value) => { setSaved(null); setScopeAssets(value); }} renderInput={params => <TextField {...params} required label="Configuration items in scope" helperText="Downstream impact is calculated automatically from the relationship map." />} /></Grid>
          <Grid size={{ xs: 12, sm: 6, lg: 3 }}><FormControl fullWidth><InputLabel>Change type</InputLabel><Select label="Change type" value={form.changeType} onChange={event => update('changeType', event.target.value)}><MenuItem value="standard">Standard</MenuItem><MenuItem value="normal">Normal</MenuItem><MenuItem value="emergency">Emergency</MenuItem></Select></FormControl></Grid>
          <Grid size={{ xs: 12, sm: 6, lg: 3 }}><FormControl fullWidth><InputLabel>Category</InputLabel><Select label="Category" value={form.category} onChange={event => update('category', event.target.value)}>{['infrastructure', 'network', 'software', 'database', 'security', 'cloud', 'other'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}</Select></FormControl></Grid>
          <Grid size={{ xs: 12, sm: 6, lg: 3 }}><FormControl fullWidth><InputLabel>Priority</InputLabel><Select label="Priority" value={form.priority} onChange={event => update('priority', event.target.value)}>{['low', 'medium', 'high', 'critical'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}</Select></FormControl></Grid>
          <Grid size={{ xs: 12, sm: 6, lg: 3 }}><FormControl fullWidth><InputLabel>Expected outage</InputLabel><Select label="Expected outage" value={form.outageExpected} onChange={event => update('outageExpected', event.target.value)}><MenuItem value="no">No</MenuItem><MenuItem value="yes">Yes</MenuItem></Select></FormControl></Grid>
          <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Planned start" type="datetime-local" value={form.plannedStart} onChange={event => update('plannedStart', event.target.value)} slotProps={{ inputLabel: { shrink: true } }} /></Grid>
          <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Planned end" type="datetime-local" value={form.plannedEnd} onChange={event => update('plannedEnd', event.target.value)} slotProps={{ inputLabel: { shrink: true } }} error={Boolean(form.plannedStart && form.plannedEnd && form.plannedEnd <= form.plannedStart)} helperText={form.plannedStart && form.plannedEnd && form.plannedEnd <= form.plannedStart ? 'Planned end must be after planned start.' : 'Optional while timing is still being agreed.'} /></Grid>
        </Grid>}

        {activeStep === 1 && <Box>
          {previewLoading && <Stack alignItems="center" spacing={1} sx={{ py: 5 }}><CircularProgress /><Typography color="text.secondary">Calculating downstream impact…</Typography></Stack>}
          {!previewLoading && preview && <>
            <Grid container spacing={2} sx={{ mb: 2 }}>{[
              ['Scope', preview.summary.scopeCount], ['Direct impact', preview.summary.directCount], ['Downstream', preview.summary.downstreamCount], ['Missing owners', preview.summary.missingOwnerCount],
            ].map(([label, value]) => <Grid key={String(label)} size={{ xs: 6, md: 3 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography color="text.secondary" variant="body2">{label}</Typography><Typography variant="h4">{value}</Typography></Paper></Grid>)}</Grid>
            <Alert severity={preview.summary.missingOwnerCount ? 'warning' : 'success'} sx={{ mb: 2 }}>{preview.summary.missingOwnerCount ? `${preview.summary.missingOwnerCount} impacted item(s) have no owner recorded. The PDF will flag them.` : 'All impacted configuration items have an owner recorded.'}</Alert>
            <Grid container spacing={2.5}>
              <Grid size={{ xs: 12, lg: 8 }}><TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 390 }}><Table stickyHeader size="small"><TableHead><TableRow><TableCell>Impact</TableCell><TableCell>Configuration item</TableCell><TableCell>Criticality</TableCell><TableCell>Owner</TableCell><TableCell>Site</TableCell></TableRow></TableHead><TableBody>{preview.items.map(item => <TableRow key={item.assetId} hover><TableCell><Chip size="small" color={item.role === 'Scope' ? 'primary' : item.role === 'Direct impact' ? 'warning' : 'default'} label={item.role} /></TableCell><TableCell><Typography fontWeight={750}>{item.name}</Typography><Typography variant="caption" color="text.secondary">{item.type} · depth {item.depth}</Typography></TableCell><TableCell>{sentence(item.criticality)}</TableCell><TableCell>{item.owner}</TableCell><TableCell>{item.site || 'Not recorded'}</TableCell></TableRow>)}</TableBody></Table></TableContainer></Grid>
              <Grid size={{ xs: 12, lg: 4 }}><Stack spacing={2}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="overline" color="primary">Suggested risk</Typography><Stack direction="row" spacing={1} alignItems="center"><Typography variant="h4">{sentence(preview.summary.suggestedRisk.level)}</Typography><Chip label={`Score ${preview.summary.suggestedRisk.score}`} /></Stack><Divider sx={{ my: 1.5 }} />{preview.summary.suggestedRisk.factors.map(factor => <Typography key={factor} variant="body2" color="text.secondary" sx={{ mb: 0.75 }}>• {factor}</Typography>)}</Paper><FormControl fullWidth><InputLabel>Recorded risk</InputLabel><Select label="Recorded risk" value={form.riskLevel} onChange={event => update('riskLevel', event.target.value)}><MenuItem value="suggested">Use CMDB suggestion ({sentence(preview.summary.suggestedRisk.level)})</MenuItem>{['low', 'medium', 'high', 'critical'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}</Select></FormControl></Stack></Grid>
              <Grid size={{ xs: 12 }}><TextField fullWidth required multiline minRows={3} label="Business and user impact" value={form.businessImpact} onChange={event => update('businessImpact', event.target.value)} helperText="Add what the graph cannot know: affected users, business processes, service expectations and acceptable disruption." /></Grid>
            </Grid>
          </>}
        </Box>}

        {activeStep === 2 && <Grid container spacing={2.5}>
          <Grid size={{ xs: 12 }}><TextField fullWidth required multiline minRows={2} label="Reason for change" value={form.reason} onChange={event => update('reason', event.target.value)} /></Grid>
          <Grid size={{ xs: 12 }}><TextField fullWidth required multiline minRows={5} label="Implementation plan" value={form.implementationPlan} onChange={event => update('implementationPlan', event.target.value)} helperText="Use numbered, executable steps including pre-checks." /></Grid>
          <Grid size={{ xs: 12, lg: 6 }}><TextField fullWidth required multiline minRows={4} label="Validation and success criteria" value={form.validationPlan} onChange={event => update('validationPlan', event.target.value)} helperText="State how the technician will prove the service is healthy." /></Grid>
          <Grid size={{ xs: 12, lg: 6 }}><TextField fullWidth required multiline minRows={4} label="Rollback plan" value={form.rollbackPlan} onChange={event => update('rollbackPlan', event.target.value)} helperText="Include the trigger, recovery steps and expected recovery time." /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><FormControl fullWidth><InputLabel>Customer communication</InputLabel><Select label="Customer communication" value={form.communicationStatus} onChange={event => update('communicationStatus', event.target.value)}><MenuItem value="required">Required</MenuItem><MenuItem value="not_required">Not required</MenuItem><MenuItem value="completed">Completed</MenuItem></Select></FormControl></Grid>
          <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Assigned technician" value={form.assignedTechnician} onChange={event => update('assignedTechnician', event.target.value)} /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Approver / CAB owner" value={form.approver} onChange={event => update('approver', event.target.value)} /></Grid>
          <Grid size={{ xs: 12, lg: 6 }}><TextField fullWidth multiline minRows={3} label="Communication plan" value={form.communicationPlan} onChange={event => update('communicationPlan', event.target.value)} helperText="Audience, timing, method and responsible person." /></Grid>
          <Grid size={{ xs: 12, lg: 6 }}><TextField fullWidth multiline minRows={3} label="Additional notes" value={form.notes} onChange={event => update('notes', event.target.value)} /></Grid>
        </Grid>}

        {activeStep === 3 && preview && <Grid container spacing={2.5}>
          <Grid size={{ xs: 12, lg: 8 }}><Paper variant="outlined" sx={{ p: 3 }}><Stack direction="row" justifyContent="space-between" spacing={2}><Box><Typography variant="overline" color="primary">{saved?.number || 'Draft change package'}</Typography><Typography variant="h4">{form.title}</Typography></Box><Chip color="info" label={sentence(form.changeType)} /></Stack><Divider sx={{ my: 2 }} /><Grid container spacing={2}>{[
            ['Customer', workspace.companyName], ['Schedule', `${formatDate(form.plannedStart)} - ${formatDate(form.plannedEnd)}`], ['Scope', `${preview.summary.scopeCount} selected CI(s)`], ['Calculated impact', `${preview.summary.directCount + preview.summary.downstreamCount} downstream CI(s)`], ['Risk', form.riskLevel === 'suggested' ? sentence(preview.summary.suggestedRisk.level) : sentence(form.riskLevel)], ['Expected outage', form.outageExpected === 'yes' ? 'Yes' : 'No'], ['Technician', form.assignedTechnician || 'Not assigned'], ['Approver', form.approver || 'Not assigned'],
          ].map(([label, value]) => <Grid key={label} size={{ xs: 12, md: 6 }}><Typography variant="caption" color="text.secondary">{label}</Typography><Typography fontWeight={700}>{value}</Typography></Grid>)}</Grid><Divider sx={{ my: 2 }} /><Typography variant="subtitle2">Business impact</Typography><Typography color="text.secondary">{form.businessImpact}</Typography></Paper></Grid>
          <Grid size={{ xs: 12, lg: 4 }}><Paper variant="outlined" sx={{ p: 3, height: '100%' }}><DescriptionOutlined color="primary" sx={{ fontSize: 40 }} /><Typography variant="h5" sx={{ mt: 1 }}>PDF change package</Typography><Typography color="text.secondary" sx={{ my: 2 }}>The generated document freezes the impact list, owners, risk factors and technician plan. It also reserves a ConnectWise ticket reference for the future publisher.</Typography>{saved ? <Button fullWidth variant="contained" startIcon={<DownloadOutlined />} onClick={() => void downloadChange(saved)}>Download {saved.number}</Button> : <Button fullWidth variant="contained" startIcon={<AssignmentTurnedInOutlined />} disabled={saving} onClick={() => void saveAndDownload()}>{saving ? 'Generating…' : 'Save & generate PDF'}</Button>}</Paper></Grid>
        </Grid>}

        <Divider sx={{ my: 3 }} />
        <Stack direction="row" justifyContent="space-between"><Button disabled={activeStep === 0 || saving} onClick={() => setActiveStep(step => step - 1)}>Back</Button>{activeStep < steps.length - 1 && <Button variant="contained" disabled={!stepComplete || previewLoading} onClick={() => setActiveStep(step => step + 1)}>Continue</Button>}</Stack>
      </CardContent>
    </Card>}

    <Card sx={{ mt: 3 }}><CardContent><Typography variant="h5">Recent change packages</Typography><Typography color="text.secondary" sx={{ mb: 2 }}>Saved impact snapshots remain available even when CI names, owners or relationships later change.</Typography>{!changes.length ? <Alert severity="info">No change packages have been created for this customer.</Alert> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>Reference</TableCell><TableCell>Change</TableCell><TableCell>Schedule</TableCell><TableCell>Risk</TableCell><TableCell>Impact</TableCell><TableCell align="right">Document</TableCell></TableRow></TableHead><TableBody>{changes.slice(0, 20).map(change => <TableRow key={change.id} hover><TableCell><Typography fontWeight={800}>{change.number}</Typography><Typography variant="caption" color="text.secondary">{sentence(change.status)}</Typography></TableCell><TableCell><Typography fontWeight={700}>{change.title}</Typography><Typography variant="caption" color="text.secondary">{sentence(change.changeType)} · {sentence(change.category)}</Typography></TableCell><TableCell>{formatDate(change.plannedStart)}</TableCell><TableCell><Chip size="small" color={change.riskLevel === 'critical' ? 'error' : change.riskLevel === 'high' ? 'warning' : 'default'} label={sentence(change.riskLevel)} /></TableCell><TableCell>{change.impactSnapshot.length} CI(s)</TableCell><TableCell align="right"><Button size="small" startIcon={<DownloadOutlined />} onClick={() => void downloadChange(change)}>PDF</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer>}</CardContent></Card>
  </Box>;
}
