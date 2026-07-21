import AssignmentTurnedInOutlined from '@mui/icons-material/AssignmentTurnedInOutlined';
import CancelOutlined from '@mui/icons-material/CancelOutlined';
import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import DescriptionOutlined from '@mui/icons-material/DescriptionOutlined';
import DownloadOutlined from '@mui/icons-material/DownloadOutlined';
import EditOutlined from '@mui/icons-material/EditOutlined';
import PlayArrowOutlined from '@mui/icons-material/PlayArrowOutlined';
import RateReviewOutlined from '@mui/icons-material/RateReviewOutlined';
import ReportProblemOutlined from '@mui/icons-material/ReportProblemOutlined';
import RestartAltOutlined from '@mui/icons-material/RestartAltOutlined';
import ScheduleOutlined from '@mui/icons-material/ScheduleOutlined';
import SendOutlined from '@mui/icons-material/SendOutlined';
import UndoOutlined from '@mui/icons-material/UndoOutlined';
import VisibilityOutlined from '@mui/icons-material/VisibilityOutlined';
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Chip, CircularProgress, Divider, FormControl, Grid,
  Dialog, DialogActions, DialogContent, DialogTitle, InputLabel, MenuItem, Paper, Select, Stack, Step, StepLabel, Stepper, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { Title } from 'react-admin';
import { useSearchParams } from 'react-router-dom';
import { apiDownload, apiFetch, getSession } from './session';
import type { Asset, ChangeApprovalRequest, ChangeImpactPreview, ChangePackage } from './types';
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
const transitions: Record<string, string[]> = {
  draft: ['impact_review', 'cancelled'],
  impact_review: ['draft', 'awaiting_approval', 'cancelled'],
  awaiting_approval: ['impact_review', 'approved', 'declined', 'cancelled'],
  approved: ['impact_review', 'scheduled', 'cancelled'],
  declined: ['draft', 'cancelled'],
  scheduled: ['approved', 'implementing', 'cancelled'],
  implementing: ['completed', 'failed'],
  completed: ['post_implementation_review'],
  failed: ['backed_out', 'post_implementation_review'],
  backed_out: ['post_implementation_review'],
  post_implementation_review: ['closed'],
};
const transitionLabels: Record<string, string> = {
  draft: 'Return to draft', impact_review: 'Send to impact review', awaiting_approval: 'Request approval',
  approved: 'Approve', declined: 'Decline', scheduled: 'Mark scheduled', implementing: 'Start implementation',
  completed: 'Mark completed', failed: 'Mark failed', backed_out: 'Confirm back-out',
  post_implementation_review: 'Start final review', cancelled: 'Cancel change', closed: 'Close change',
};
const statusColors: Record<string, 'default' | 'primary' | 'info' | 'success' | 'warning' | 'error'> = {
  draft: 'default', impact_review: 'info', awaiting_approval: 'warning', approved: 'success', declined: 'error',
  scheduled: 'primary', implementing: 'warning', completed: 'success', failed: 'error', backed_out: 'warning',
  post_implementation_review: 'info', cancelled: 'default', closed: 'success',
};

function emptyForm(): ChangeForm {
  return {
    title: '', changeType: 'normal', category: 'infrastructure', priority: 'medium', riskLevel: 'suggested',
    outageExpected: 'no', plannedStart: '', plannedEnd: '', reason: '', businessImpact: '', implementationPlan: '',
    validationPlan: '', rollbackPlan: '', communicationStatus: 'required', communicationPlan: '',
    assignedTechnician: getSession()?.user.email || '', approver: '', notes: '',
  };
}

function formFromChange(change: ChangePackage): ChangeForm {
  return {
    title: change.title, changeType: change.changeType, category: change.category, priority: change.priority,
    riskLevel: change.riskSource === 'cmdb_suggestion' ? 'suggested' : change.riskLevel,
    outageExpected: change.outageExpected ? 'yes' : 'no', plannedStart: change.plannedStart || '', plannedEnd: change.plannedEnd || '',
    reason: change.reason || '', businessImpact: change.businessImpact || '', implementationPlan: change.implementationPlan || '',
    validationPlan: change.validationPlan || '', rollbackPlan: change.rollbackPlan || '',
    communicationStatus: change.communicationStatus || 'required', communicationPlan: change.communicationPlan || '',
    assignedTechnician: change.assignedTechnician || '', approver: change.approver || '', notes: change.notes || '',
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
  const [editing, setEditing] = useState<ChangePackage | null>(null);
  const [selectedChange, setSelectedChange] = useState<ChangePackage | null>(null);
  const [approvalRequests, setApprovalRequests] = useState<ChangeApprovalRequest[]>([]);
  const [approvalsLoading, setApprovalsLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState('active');
  const [transitionTarget, setTransitionTarget] = useState<string | null>(null);
  const [transitionData, setTransitionData] = useState({
    reason: '', actualStart: '', actualEnd: '', actualOutageMinutes: '0', validationResult: '', rollbackResult: '', closureNotes: '',
    plannedStart: '', plannedEnd: '', assignedTechnician: '', communicationStatus: 'required', communicationPlan: '', notes: '',
  });
  const [notice, setNotice] = useState<{ severity: 'success' | 'error' | 'info'; message: string } | null>(null);
  const canCreate = ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');

  const loadChanges = async () => {
    if (workspace.isRoot) return [];
    const records = await apiFetch<ChangePackage[]>(`/api/changes?companyId=${encodeURIComponent(workspace.companyId)}`);
    setChanges(records);
    setSelectedChange(current => current ? records.find(item => item.id === current.id) || null : null);
    return records;
  };

  useEffect(() => {
    setAssets([]); setChanges([]); setScopeAssets([]); setPreview(null); setSaved(null); setEditing(null); setSelectedChange(null); setTransitionTarget(null); setActiveStep(0); setForm(emptyForm()); setNotice(null);
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
    if (!selectedChange || !canCreate) { setApprovalRequests([]); return; }
    setApprovalsLoading(true);
    apiFetch<ChangeApprovalRequest[]>(`/api/changes/${encodeURIComponent(selectedChange.id)}/approval-requests`)
      .then(records => { if (active) setApprovalRequests(records); })
      .catch(error => { if (active) setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Approval history could not be loaded.' }); })
      .finally(() => { if (active) setApprovalsLoading(false); });
    return () => { active = false; };
  }, [selectedChange?.id, selectedChange?.status, canCreate]);

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
    setForm(emptyForm()); setScopeAssets([]); setPreview(null); setSaved(null); setEditing(null); setActiveStep(0); setNotice(null);
  };

  const beginEdit = (change: ChangePackage) => {
    setEditing(change); setSelectedChange(null); setSaved(null); setForm(formFromChange(change));
    setScopeAssets(assets.filter(asset => change.scopeAssetIds.includes(asset.id))); setActiveStep(0); setNotice(null);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const openTransition = (change: ChangePackage, target: string) => {
    setSelectedChange(change); setTransitionTarget(target);
    setTransitionData({
      reason: '', actualStart: '', actualEnd: '', actualOutageMinutes: String(change.actualOutageMinutes || 0),
      validationResult: change.validationResult || '', rollbackResult: change.rollbackResult || '', closureNotes: change.closureNotes || '',
      plannedStart: change.plannedStart || '', plannedEnd: change.plannedEnd || '', assignedTechnician: change.assignedTechnician || '',
      communicationStatus: change.communicationStatus || 'required', communicationPlan: change.communicationPlan || '', notes: change.notes || '',
    });
  };

  const applyTransition = async () => {
    if (!selectedChange || !transitionTarget) return;
    setSaving(true); setNotice(null);
    try {
      const scheduleEdit = transitionTarget === 'edit_schedule';
      const updated = await apiFetch<ChangePackage>(
        scheduleEdit ? `/api/changes/${encodeURIComponent(selectedChange.id)}` : `/api/changes/${encodeURIComponent(selectedChange.id)}/transition`,
        {
          method: scheduleEdit ? 'PATCH' : 'POST',
          body: JSON.stringify(scheduleEdit ? {
            expectedRevision: selectedChange.revision || 1,
            plannedStart: transitionData.plannedStart, plannedEnd: transitionData.plannedEnd,
            assignedTechnician: transitionData.assignedTechnician, communicationStatus: transitionData.communicationStatus,
            communicationPlan: transitionData.communicationPlan, notes: transitionData.notes,
          } : {
            status: transitionTarget, expectedRevision: selectedChange.revision || 1, ...transitionData,
            actualOutageMinutes: Number(transitionData.actualOutageMinutes || 0),
          }),
        },
      );
      setTransitionTarget(null); setSelectedChange(updated); await loadChanges();
      setNotice({ severity: 'success', message: scheduleEdit ? `${updated.number} schedule was updated.` : `${updated.number} is now ${sentence(updated.status)}.` });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The change could not be updated.' });
    } finally { setSaving(false); }
  };

  const sendApprovalRequests = async () => {
    if (!selectedChange) return;
    const replacing = approvalRequests.some(item => item.status === 'pending');
    if (replacing && !window.confirm('Replace the current approval batch? Existing pending links will stop working.')) return;
    setSaving(true); setNotice(null);
    try {
      const records = await apiFetch<ChangeApprovalRequest[]>(`/api/changes/${encodeURIComponent(selectedChange.id)}/approval-requests`, {
        method: 'POST', body: JSON.stringify({ expectedRevision: selectedChange.revision || 1, expiresInHours: 72 }),
      });
      setApprovalRequests(records);
      const accepted = records.filter(item => item.deliveryStatus === 'accepted' && item.batchId === records[0]?.batchId).length;
      const failed = records.filter(item => item.deliveryStatus === 'failed' && item.batchId === records[0]?.batchId).length;
      setNotice({ severity: failed ? 'info' : 'success', message: `${accepted} approval request(s) accepted by Microsoft 365${failed ? `; ${failed} delivery attempt(s) need attention.` : '.'}` });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Approval requests could not be sent.' });
    } finally { setSaving(false); }
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
      const change = await apiFetch<ChangePackage>(editing ? `/api/changes/${encodeURIComponent(editing.id)}` : '/api/changes', {
        method: editing ? 'PATCH' : 'POST',
        body: JSON.stringify({
          ...form,
          ...(editing ? { expectedRevision: editing.revision || 1 } : { companyId: workspace.companyId }),
          scopeAssetIds: scopeAssets.map(asset => asset.id),
          outageExpected: form.outageExpected === 'yes',
          riskLevel: form.riskLevel === 'suggested' ? '' : form.riskLevel,
        }),
      });
      setSaved(change);
      setEditing(change);
      await loadChanges();
      await downloadChange(change);
      setNotice({ severity: 'success', message: `${change.number} revision ${change.revision} was saved and its PDF was generated.` });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The change package could not be generated.' });
    } finally { setSaving(false); }
  };

  const visibleChanges = useMemo(() => changes.filter(change => {
    if (statusFilter === 'all') return true;
    if (statusFilter === 'active') return !['closed', 'cancelled'].includes(change.status);
    return change.status === statusFilter;
  }), [changes, statusFilter]);

  if (workspace.isRoot) return <Box><Title title="Change control" /><Typography variant="overline" color="primary">Operations</Typography><Typography variant="h3" sx={{ mb: 3 }}>Change control</Typography><Alert severity="info">Select a customer workspace to create or review customer change packages.</Alert></Box>;

  return (
    <Box>
      <Title title={`${workspace.companyName} change control`} />
      <Stack
        direction={{ xs: 'column', md: 'row' }}
        spacing={2}
        sx={{
          justifyContent: "space-between",
          alignItems: { md: 'flex-end' },
          mb: 3
        }}>
        <Box><Typography variant="overline" color="primary">Change enablement</Typography><Typography variant="h3">Change control</Typography><Typography sx={{
          color: "text.secondary"
        }}>Create an impact-aware, customer-ready change package from CMDB relationships.</Typography></Box>
        <Button variant="outlined" startIcon={<RestartAltOutlined />} onClick={reset}>New change</Button>
      </Stack>
      {notice && <Alert severity={notice.severity} onClose={() => setNotice(null)} sx={{ mb: 2 }}>{notice.message}</Alert>}
      {editing && <Alert severity="info" sx={{ mb: 2 }} action={<Button color="inherit" onClick={reset}>Stop editing</Button>}>
        Editing {editing.number} revision {editing.revision}. Saving creates a new immutable revision and refreshes its CMDB impact snapshot.
      </Alert>}
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
            {previewLoading && <Stack
              spacing={1}
              sx={{
                alignItems: "center",
                py: 5
              }}><CircularProgress /><Typography sx={{
              color: "text.secondary"
            }}>Calculating downstream impact…</Typography></Stack>}
            {!previewLoading && preview && <>
              <Grid container spacing={2} sx={{ mb: 2 }}>{[
                ['Scope', preview.summary.scopeCount], ['Direct impact', preview.summary.directCount], ['Downstream', preview.summary.downstreamCount], ['Missing owners', preview.summary.missingOwnerCount],
              ].map(([label, value]) => <Grid key={String(label)} size={{ xs: 6, md: 3 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="body2" sx={{
                color: "text.secondary"
              }}>{label}</Typography><Typography variant="h4">{value}</Typography></Paper></Grid>)}</Grid>
              <Alert severity={preview.summary.missingOwnerCount ? 'warning' : 'success'} sx={{ mb: 2 }}>{preview.summary.missingOwnerCount ? `${preview.summary.missingOwnerCount} impacted item(s) have no owner recorded. The PDF will flag them.` : 'All impacted configuration items have an owner recorded.'}</Alert>
              {preview.summary.businessSystems.length > 0 && <Paper variant="outlined" sx={{ p: 2, mb: 2, borderColor: 'primary.main', bgcolor: 'rgba(80,213,185,.04)' }}>
                <Stack
                  direction={{ xs: 'column', md: 'row' }}
                  spacing={1}
                  sx={{
                    justifyContent: "space-between",
                    mb: 1.5
                  }}><Box><Typography variant="overline" color="primary">Business impact and signoff</Typography><Typography variant="h6">{preview.summary.businessSystemCount} business system(s) assessed</Typography></Box>{preview.summary.missingBusinessOwnerCount > 0 && <Chip color="warning" label={`${preview.summary.missingBusinessOwnerCount} missing business owner`} />}</Stack>
                <Grid container spacing={1.5}>{preview.summary.businessSystems.map(system => <Grid key={system.assetId} size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 1.5 }}><Stack direction="row" spacing={1} sx={{
                  justifyContent: "space-between"
                }}><Box><Typography sx={{
                  fontWeight: 800
                }}>{system.name}</Typography><Typography variant="caption" sx={{
                  color: "text.secondary"
                }}>{system.department || 'Department not recorded'} · {system.userPopulation || 'User population not recorded'}</Typography></Box><Chip size="small" color={system.impactSeverity === 'outage' ? 'error' : system.impactSeverity === 'degraded' ? 'warning' : 'success'} label={sentence(system.impactSeverity)} /></Stack><Typography variant="body2" sx={{ mt: 1 }}>Owner: {system.businessOwner || 'Not recorded'}</Typography><Typography variant="body2">Signoff: {system.signoffRequired === 'no' ? 'Not required' : system.signoffDelegate || system.businessOwner || 'Not assigned'}</Typography><Typography variant="caption" sx={{
                  color: "text.secondary"
                }}>RTO {system.rtoHours || '?'}h · RPO {system.rpoHours || '?'}h</Typography></Paper></Grid>)}</Grid>
              </Paper>}
              {preview.summary.virtualizationAssessments.length > 0 && <Paper variant="outlined" sx={{ p: 2, mb: 2, borderColor: '#56b4e9', bgcolor: 'rgba(86,180,233,.04)' }}>
                <Stack
                  direction={{ xs: 'column', md: 'row' }}
                  spacing={1}
                  sx={{
                    justifyContent: "space-between",
                    mb: 1.5
                  }}><Box><Typography variant="overline" sx={{
                  color: "info.main"
                }}>Virtualization resilience</Typography><Typography variant="h6">{preview.summary.protectedVmCount} protected · {preview.summary.degradedVmCount} degraded · {preview.summary.outageVmCount} outage</Typography></Box></Stack>
                <Grid container spacing={1.5}>{preview.summary.virtualizationAssessments.map(vm => <Grid key={vm.assetId} size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 1.5 }}><Stack direction="row" spacing={1} sx={{
                  justifyContent: "space-between"
                }}><Box><Typography sx={{
                  fontWeight: 800
                }}>{vm.name}</Typography><Typography variant="caption" sx={{
                  color: "text.secondary"
                }}>{vm.virtualizationPlatform || 'Platform not recorded'} · {vm.clusterName || 'Cluster not recorded'}</Typography></Box><Chip size="small" color={vm.impactSeverity === 'outage' ? 'error' : vm.impactSeverity === 'degraded' ? 'warning' : 'success'} label={sentence(vm.impactSeverity)} /></Stack><Typography variant="body2" sx={{ mt: 1 }}>{vm.virtualizationDecision || 'No HA decision was required for this impact path.'}</Typography></Paper></Grid>)}</Grid>
              </Paper>}
              <Grid container spacing={2.5}>
                <Grid size={{ xs: 12, lg: 8 }}><TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 390 }}><Table stickyHeader size="small"><TableHead><TableRow><TableCell>Impact</TableCell><TableCell>Configuration item</TableCell><TableCell>Criticality</TableCell><TableCell>Owner</TableCell><TableCell>Site</TableCell></TableRow></TableHead><TableBody>{preview.items.map(item => <TableRow key={item.assetId} hover><TableCell><Chip size="small" color={item.role === 'Scope' ? 'primary' : item.impactSeverity === 'outage' ? 'error' : item.impactSeverity === 'degraded' ? 'warning' : item.impactSeverity === 'protected' ? 'success' : 'default'} label={item.role === 'Scope' ? item.role : `${item.role} · ${sentence(item.impactSeverity)}`} /></TableCell><TableCell><Typography sx={{
                  fontWeight: 750
                }}>{item.name}</Typography><Typography variant="caption" sx={{
                  color: "text.secondary"
                }}>{item.type} · depth {item.depth}</Typography></TableCell><TableCell>{sentence(item.criticality)}</TableCell><TableCell>{item.owner}</TableCell><TableCell>{item.site || 'Not recorded'}</TableCell></TableRow>)}</TableBody></Table></TableContainer></Grid>
                <Grid size={{ xs: 12, lg: 4 }}><Stack spacing={2}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="overline" color="primary">Suggested risk</Typography><Stack direction="row" spacing={1} sx={{
                  alignItems: "center"
                }}><Typography variant="h4">{sentence(preview.summary.suggestedRisk.level)}</Typography><Chip label={`Score ${preview.summary.suggestedRisk.score}`} /></Stack><Divider sx={{ my: 1.5 }} />{preview.summary.suggestedRisk.factors.map(factor => <Typography
                  key={factor}
                  variant="body2"
                  sx={{
                    color: "text.secondary",
                    mb: 0.75
                  }}>• {factor}</Typography>)}</Paper><FormControl fullWidth><InputLabel>Recorded risk</InputLabel><Select label="Recorded risk" value={form.riskLevel} onChange={event => update('riskLevel', event.target.value)}><MenuItem value="suggested">Use CMDB suggestion ({sentence(preview.summary.suggestedRisk.level)})</MenuItem>{['low', 'medium', 'high', 'critical'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}</Select></FormControl></Stack></Grid>
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
            <Grid size={{ xs: 12, lg: 8 }}><Paper variant="outlined" sx={{ p: 3 }}><Stack direction="row" spacing={2} sx={{
              justifyContent: "space-between"
            }}><Box><Typography variant="overline" color="primary">{saved?.number || 'Draft change package'}</Typography><Typography variant="h4">{form.title}</Typography></Box><Chip color="info" label={sentence(form.changeType)} /></Stack><Divider sx={{ my: 2 }} /><Grid container spacing={2}>{[
              ['Customer', workspace.companyName], ['Schedule', `${formatDate(form.plannedStart)} - ${formatDate(form.plannedEnd)}`], ['Scope', `${preview.summary.scopeCount} selected CI(s)`], ['Calculated impact', `${preview.summary.directCount + preview.summary.downstreamCount} downstream CI(s)`], ['Business systems', `${preview.summary.businessSystemCount} affected`], ['Business owners', preview.summary.businessOwners.join(', ') || 'None recorded'], ['Risk', form.riskLevel === 'suggested' ? sentence(preview.summary.suggestedRisk.level) : sentence(form.riskLevel)], ['Expected outage', form.outageExpected === 'yes' ? 'Yes' : 'No'], ['Technician', form.assignedTechnician || 'Not assigned'], ['Approver', form.approver || 'Not assigned'],
            ].map(([label, value]) => <Grid key={label} size={{ xs: 12, md: 6 }}><Typography variant="caption" sx={{
              color: "text.secondary"
            }}>{label}</Typography><Typography sx={{
              fontWeight: 700
            }}>{value}</Typography></Grid>)}</Grid><Divider sx={{ my: 2 }} /><Typography variant="subtitle2">Business impact</Typography><Typography sx={{
              color: "text.secondary"
            }}>{form.businessImpact}</Typography></Paper></Grid>
            <Grid size={{ xs: 12, lg: 4 }}><Paper variant="outlined" sx={{ p: 3, height: '100%' }}><DescriptionOutlined color="primary" sx={{ fontSize: 40 }} /><Typography variant="h5" sx={{ mt: 1 }}>PDF change package</Typography><Typography
              sx={{
                color: "text.secondary",
                my: 2
              }}>The generated document freezes the impact list, owners, risk factors and technician plan. It also reserves a ConnectWise ticket reference for the future publisher.</Typography>{saved ? <Button fullWidth variant="contained" startIcon={<DownloadOutlined />} onClick={() => void downloadChange(saved)}>Download {saved.number}</Button> : <Button fullWidth variant="contained" startIcon={<AssignmentTurnedInOutlined />} disabled={saving} onClick={() => void saveAndDownload()}>{saving ? 'Generating…' : editing ? 'Save revision & generate PDF' : 'Save & generate PDF'}</Button>}</Paper></Grid>
          </Grid>}

          <Divider sx={{ my: 3 }} />
          <Stack direction="row" sx={{
            justifyContent: "space-between"
          }}><Button disabled={activeStep === 0 || saving} onClick={() => setActiveStep(step => step - 1)}>Back</Button>{activeStep < steps.length - 1 && <Button variant="contained" disabled={!stepComplete || previewLoading} onClick={() => setActiveStep(step => step + 1)}>Continue</Button>}</Stack>
        </CardContent>
      </Card>}

      <Card sx={{ mt: 3 }}><CardContent>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'flex-end' }, mb: 2 }}>
          <Box><Typography variant="h5">Change register</Typography><Typography sx={{ color: 'text.secondary' }}>Open a record to edit its plan, manage approval, record execution and complete the final review.</Typography></Box>
          <FormControl size="small" sx={{ minWidth: 210 }}><InputLabel>Status</InputLabel><Select label="Status" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}>
            <MenuItem value="active">All active changes</MenuItem><MenuItem value="all">All changes</MenuItem>
            {Object.keys(statusColors).map(status => <MenuItem key={status} value={status}>{sentence(status)}</MenuItem>)}
          </Select></FormControl>
        </Stack>
        {!visibleChanges.length ? <Alert severity="info">No changes match this view.</Alert> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>Reference</TableCell><TableCell>Change</TableCell><TableCell>Status</TableCell><TableCell>Schedule</TableCell><TableCell>Risk</TableCell><TableCell>Impact</TableCell><TableCell align="right">Actions</TableCell></TableRow></TableHead><TableBody>{visibleChanges.slice(0, 50).map(change => <TableRow key={change.id} hover sx={{ cursor: 'pointer' }} onClick={() => setSelectedChange(change)}><TableCell><Typography sx={{ fontWeight: 800 }}>{change.number}</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>Revision {change.revision}</Typography></TableCell><TableCell><Typography sx={{ fontWeight: 700 }}>{change.title}</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>{sentence(change.changeType)} · {sentence(change.category)}</Typography></TableCell><TableCell><Chip size="small" color={statusColors[change.status] || 'default'} label={sentence(change.status)} /></TableCell><TableCell>{formatDate(change.plannedStart)}</TableCell><TableCell><Chip size="small" color={change.riskLevel === 'critical' ? 'error' : change.riskLevel === 'high' ? 'warning' : 'default'} label={sentence(change.riskLevel)} /></TableCell><TableCell>{change.impactSnapshot.length} CI(s)</TableCell><TableCell align="right"><Button size="small" startIcon={<VisibilityOutlined />} onClick={event => { event.stopPropagation(); setSelectedChange(change); }}>Open</Button><Button size="small" startIcon={<DownloadOutlined />} onClick={event => { event.stopPropagation(); void downloadChange(change); }}>PDF</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer>}
      </CardContent></Card>

      <Dialog open={Boolean(selectedChange)} onClose={() => { if (!transitionTarget) setSelectedChange(null); }} maxWidth="lg" fullWidth>
        {selectedChange && <>
          <DialogTitle><Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' } }}><Box><Typography variant="overline" color="primary">{selectedChange.number} · Revision {selectedChange.revision}</Typography><Typography variant="h5">{selectedChange.title}</Typography></Box><Chip color={statusColors[selectedChange.status] || 'default'} label={sentence(selectedChange.status)} /></Stack></DialogTitle>
          <DialogContent dividers>
            <Grid container spacing={2}>
              <Grid size={{ xs: 12, md: 8 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Plan and impact</Typography><Grid container spacing={2} sx={{ mt: 0.5 }}>{[
                ['Schedule', `${formatDate(selectedChange.plannedStart)} – ${formatDate(selectedChange.plannedEnd)}`],
                ['Technician', selectedChange.assignedTechnician || 'Not assigned'], ['Approver / CAB', selectedChange.approver || 'Not assigned'],
                ['Risk', `${sentence(selectedChange.riskLevel)} · score ${selectedChange.riskAssessment?.score ?? 0}`],
                ['Impact snapshot', `${selectedChange.impactSnapshot.length} CI(s), ${selectedChange.impactSummary?.businessSystemCount || 0} business system(s)`],
                ['Communication', sentence(selectedChange.communicationStatus)],
              ].map(([label, value]) => <Grid key={label} size={{ xs: 12, sm: 6 }}><Typography variant="caption" sx={{ color: 'text.secondary' }}>{label}</Typography><Typography sx={{ fontWeight: 700 }}>{value}</Typography></Grid>)}</Grid><Divider sx={{ my: 2 }} /><Typography variant="subtitle2">Reason and business impact</Typography><Typography sx={{ whiteSpace: 'pre-wrap', mb: 1 }}>{selectedChange.reason}</Typography><Typography sx={{ whiteSpace: 'pre-wrap', color: 'text.secondary' }}>{selectedChange.businessImpact || 'No additional business impact recorded.'}</Typography></Paper></Grid>
              <Grid size={{ xs: 12, md: 4 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Outcome</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>Current result</Typography><Typography sx={{ fontWeight: 800, mb: 1 }}>{sentence(selectedChange.outcome || 'pending')}</Typography><Typography variant="body2">Actual window: {selectedChange.actualStart || selectedChange.actualEnd ? `${formatDate(selectedChange.actualStart)} – ${formatDate(selectedChange.actualEnd)}` : 'Not started'}</Typography><Typography variant="body2">Actual outage: {selectedChange.actualOutageMinutes || 0} minute(s)</Typography>{selectedChange.failureReason && <Alert severity="error" sx={{ mt: 1 }}>{selectedChange.failureReason}</Alert>}{selectedChange.validationResult && <Alert severity="success" sx={{ mt: 1 }}>{selectedChange.validationResult}</Alert>}</Paper></Grid>
              <Grid size={{ xs: 12, md: 4 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Implementation plan</Typography><Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{selectedChange.implementationPlan}</Typography></Paper></Grid>
              <Grid size={{ xs: 12, md: 4 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Validation</Typography><Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{selectedChange.validationPlan}</Typography></Paper></Grid>
              <Grid size={{ xs: 12, md: 4 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Rollback</Typography><Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{selectedChange.rollbackPlan}</Typography>{selectedChange.rollbackResult && <Typography variant="body2" sx={{ mt: 1, fontWeight: 700 }}>Result: {selectedChange.rollbackResult}</Typography>}</Paper></Grid>
              {selectedChange.status === 'awaiting_approval' && <Grid size={{ xs: 12 }}><Paper variant="outlined" sx={{ p: 2 }}><Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'center' } }}><Box><Typography variant="h6">External sign-off workflow</Typography><Typography variant="body2" color="text.secondary">Approvers are resolved from active business-system sign-off delegates and owners. Links expire after 72 hours.</Typography></Box><Button variant="contained" startIcon={<SendOutlined />} disabled={saving || approvalsLoading} onClick={() => void sendApprovalRequests()}>{approvalRequests.some(item => item.status === 'pending') ? 'Replace approval batch' : 'Send approval requests'}</Button></Stack>{approvalsLoading ? <CircularProgress size={24} sx={{ mt: 2 }} /> : approvalRequests.length ? <Stack spacing={1} sx={{ mt: 2 }}>{approvalRequests.slice(0, 20).map(item => <Box key={item.id} sx={{ p: 1.5, border: '1px solid', borderColor: 'divider', borderRadius: 1.5 }}><Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' } }}><Box><Typography sx={{ fontWeight: 750 }}>{item.approverName} · {item.approverEmail}</Typography><Typography variant="caption" color="text.secondary">{item.scope.map(scope => scope.name).filter(Boolean).join(', ') || 'Entire change'} · expires {formatDate(item.expiresAt)}</Typography></Box><Stack direction="row" spacing={1}><Chip size="small" label={sentence(item.deliveryStatus)} color={item.deliveryStatus === 'accepted' ? 'success' : item.deliveryStatus === 'failed' ? 'error' : 'default'} /><Chip size="small" label={sentence(item.status)} color={item.status === 'approved' ? 'success' : item.status === 'declined' ? 'error' : item.status === 'pending' ? 'warning' : 'default'} /></Stack></Stack>{item.lastError && <Alert severity="error" sx={{ mt: 1 }}>{item.lastError}</Alert>}</Box>)}</Stack> : <Alert severity="info" sx={{ mt: 2 }}>No approval links have been sent yet.</Alert>}</Paper></Grid>}
              <Grid size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Approval evidence</Typography>{!(selectedChange.approvals || []).length ? <Typography sx={{ color: 'text.secondary' }}>No approval decision has been recorded.</Typography> : <Stack spacing={1} sx={{ mt: 1 }}>{(selectedChange.approvals || []).map(item => <Box key={item.id}><Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}><Chip size="small" color={item.decision === 'approved' ? 'success' : 'error'} label={sentence(item.decision)} /><Typography sx={{ fontWeight: 700 }}>{item.actorEmail}</Typography></Stack><Typography variant="body2">{item.comments || 'No comments'}</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>{formatDate(item.createdAt)}</Typography></Box>)}</Stack>}</Paper></Grid>
              <Grid size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Lifecycle history</Typography><Stack spacing={1} sx={{ mt: 1 }}>{[...(selectedChange.statusHistory || [])].reverse().map(item => <Box key={item.id} sx={{ borderLeft: '2px solid', borderColor: 'divider', pl: 1.5 }}><Typography sx={{ fontWeight: 700 }}>{sentence(item.toStatus)}</Typography><Typography variant="body2">{item.reason || 'No comment'}</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>{item.actorEmail || 'System'} · {formatDate(item.createdAt)}</Typography></Box>)}</Stack></Paper></Grid>
            </Grid>
          </DialogContent>
          <DialogActions sx={{ flexWrap: 'wrap', gap: 1, justifyContent: 'space-between' }}><Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}><Button startIcon={<DownloadOutlined />} onClick={() => void downloadChange(selectedChange)}>PDF</Button>{canCreate && ['draft', 'impact_review'].includes(selectedChange.status) && <Button startIcon={<EditOutlined />} onClick={() => beginEdit(selectedChange)}>Edit</Button>}{canCreate && ['approved', 'scheduled'].includes(selectedChange.status) && <Button startIcon={<ScheduleOutlined />} onClick={() => openTransition(selectedChange, 'edit_schedule')}>Edit schedule</Button>}</Stack><Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', justifyContent: 'flex-end' }}>{canCreate && (transitions[selectedChange.status] || []).filter(target => target !== 'approved' || !approvalRequests.length).map(target => <Button key={target} variant={['approved', 'completed', 'closed'].includes(target) ? 'contained' : 'outlined'} color={['declined', 'cancelled', 'failed'].includes(target) ? 'error' : target === 'backed_out' ? 'warning' : 'primary'} startIcon={target === 'approved' || target === 'completed' || target === 'closed' ? <CheckCircleOutlined /> : target === 'implementing' ? <PlayArrowOutlined /> : target === 'failed' || target === 'declined' ? <ReportProblemOutlined /> : target === 'backed_out' ? <UndoOutlined /> : target === 'cancelled' ? <CancelOutlined /> : <RateReviewOutlined />} onClick={() => openTransition(selectedChange, target)}>{transitionLabels[target]}</Button>)}<Button onClick={() => setSelectedChange(null)}>Close</Button></Stack></DialogActions>
        </>}
      </Dialog>

      <Dialog open={Boolean(transitionTarget)} onClose={() => { if (!saving) setTransitionTarget(null); }} maxWidth="sm" fullWidth>
        {selectedChange && transitionTarget && <><DialogTitle>{transitionTarget === 'edit_schedule' ? `Edit ${selectedChange.number} schedule` : transitionLabels[transitionTarget]}</DialogTitle><DialogContent><Stack spacing={2} sx={{ mt: 1 }}>
          {transitionTarget === 'edit_schedule' ? <>
            <TextField label="Planned start" type="datetime-local" value={transitionData.plannedStart} onChange={event => setTransitionData(value => ({ ...value, plannedStart: event.target.value }))} slotProps={{ inputLabel: { shrink: true } }} />
            <TextField label="Planned end" type="datetime-local" value={transitionData.plannedEnd} onChange={event => setTransitionData(value => ({ ...value, plannedEnd: event.target.value }))} slotProps={{ inputLabel: { shrink: true } }} />
            <TextField label="Assigned technician" value={transitionData.assignedTechnician} onChange={event => setTransitionData(value => ({ ...value, assignedTechnician: event.target.value }))} />
            <FormControl><InputLabel>Communication</InputLabel><Select label="Communication" value={transitionData.communicationStatus} onChange={event => setTransitionData(value => ({ ...value, communicationStatus: event.target.value }))}><MenuItem value="required">Required</MenuItem><MenuItem value="not_required">Not required</MenuItem><MenuItem value="completed">Completed</MenuItem></Select></FormControl>
            <TextField label="Communication plan" multiline minRows={2} value={transitionData.communicationPlan} onChange={event => setTransitionData(value => ({ ...value, communicationPlan: event.target.value }))} />
            <TextField label="Schedule notes" multiline minRows={2} value={transitionData.notes} onChange={event => setTransitionData(value => ({ ...value, notes: event.target.value }))} />
          </> : <>
            <Alert severity={['declined', 'cancelled', 'failed'].includes(transitionTarget) ? 'warning' : 'info'}>This action creates revision {(selectedChange.revision || 1) + 1} and an immutable audit event.</Alert>
            <TextField required={['declined', 'cancelled', 'failed', 'backed_out', 'closed'].includes(transitionTarget)} label={transitionTarget === 'approved' ? 'Approval comments' : 'Reason / execution notes'} multiline minRows={3} value={transitionData.reason} onChange={event => setTransitionData(value => ({ ...value, reason: event.target.value }))} />
            {transitionTarget === 'implementing' && <TextField label="Actual start" type="datetime-local" value={transitionData.actualStart} onChange={event => setTransitionData(value => ({ ...value, actualStart: event.target.value }))} slotProps={{ inputLabel: { shrink: true } }} helperText="Leave blank to use the current time." />}
            {['completed', 'failed'].includes(transitionTarget) && <><TextField label="Actual end" type="datetime-local" value={transitionData.actualEnd} onChange={event => setTransitionData(value => ({ ...value, actualEnd: event.target.value }))} slotProps={{ inputLabel: { shrink: true } }} helperText="Leave blank to use the current time." /><TextField label="Actual outage (minutes)" type="number" value={transitionData.actualOutageMinutes} onChange={event => setTransitionData(value => ({ ...value, actualOutageMinutes: event.target.value }))} slotProps={{ htmlInput: { min: 0 } }} /><TextField label="Validation result" multiline minRows={3} value={transitionData.validationResult} onChange={event => setTransitionData(value => ({ ...value, validationResult: event.target.value }))} /></>}
            {transitionTarget === 'backed_out' && <TextField required label="Rollback result" multiline minRows={3} value={transitionData.rollbackResult} onChange={event => setTransitionData(value => ({ ...value, rollbackResult: event.target.value }))} />}
            {transitionTarget === 'closed' && <TextField required label="Post-implementation review and closure notes" multiline minRows={4} value={transitionData.closureNotes} onChange={event => setTransitionData(value => ({ ...value, closureNotes: event.target.value }))} />}
          </>}
        </Stack></DialogContent><DialogActions><Button disabled={saving} onClick={() => setTransitionTarget(null)}>Cancel</Button><Button variant="contained" disabled={saving || (['declined', 'cancelled', 'failed', 'backed_out', 'closed'].includes(transitionTarget) && transitionData.reason.trim().length < 4)} onClick={() => void applyTransition()}>{saving ? 'Saving…' : transitionTarget === 'edit_schedule' ? 'Save schedule' : 'Confirm action'}</Button></DialogActions></>}
      </Dialog>
    </Box>
  );
}
