import AddOutlined from '@mui/icons-material/AddOutlined';
import AssignmentTurnedInOutlined from '@mui/icons-material/AssignmentTurnedInOutlined';
import AutoAwesomeOutlined from '@mui/icons-material/AutoAwesomeOutlined';
import CancelOutlined from '@mui/icons-material/CancelOutlined';
import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import DescriptionOutlined from '@mui/icons-material/DescriptionOutlined';
import DeleteOutlined from '@mui/icons-material/DeleteOutlined';
import DownloadOutlined from '@mui/icons-material/DownloadOutlined';
import EditOutlined from '@mui/icons-material/EditOutlined';
import PlayArrowOutlined from '@mui/icons-material/PlayArrowOutlined';
import PersonAddAltOutlined from '@mui/icons-material/PersonAddAltOutlined';
import RateReviewOutlined from '@mui/icons-material/RateReviewOutlined';
import ReportProblemOutlined from '@mui/icons-material/ReportProblemOutlined';
import RestartAltOutlined from '@mui/icons-material/RestartAltOutlined';
import ScheduleOutlined from '@mui/icons-material/ScheduleOutlined';
import SendOutlined from '@mui/icons-material/SendOutlined';
import UndoOutlined from '@mui/icons-material/UndoOutlined';
import VisibilityOutlined from '@mui/icons-material/VisibilityOutlined';
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Checkbox, Chip, CircularProgress, Divider, FormControl, FormControlLabel, Grid,
  Dialog, DialogActions, DialogContent, DialogTitle, IconButton, InputLabel, MenuItem, Paper, Select, Stack, Step, StepLabel, Stepper, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { apiDownload, apiFetch, getSession } from './session';
import { Title } from './ui';
import type { Asset, ChangeApprovalRequest, ChangeClosureAssessment, ChangeClosureFollowUp, ChangeClosureTest, ChangeImpactItem, ChangeImpactPreview, ChangePackage, ChangeTemplate, ChangeTemplateParameter, User } from './types';
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
  assignedUserId: string;
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
    assignedUserId: getSession()?.user.id || '', approver: '', notes: '',
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
    assignedUserId: change.assignedUserId || '', approver: change.approver || '', notes: change.notes || '',
  };
}

function sentence(value: string | null | undefined, fallback = 'Not recorded') {
  if (!value) return fallback;
  return value.replaceAll('_', ' ').replace(/\b\w/g, match => match.toUpperCase());
}

function formatDate(value: string) {
  if (!value) return 'Not scheduled';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function sortedImpactItems(change: ChangePackage) {
  const roleOrder: Record<ChangeImpactItem['role'], number> = {
    Scope: 0,
    'Direct impact': 1,
    'Downstream impact': 2,
  };
  return [...change.impactSnapshot].sort((left, right) =>
    roleOrder[left.role] - roleOrder[right.role]
    || left.depth - right.depth
    || left.name.localeCompare(right.name));
}

function impactColor(severity: ChangeImpactItem['impactSeverity'] | undefined) {
  if (severity === 'outage') return 'error';
  if (severity === 'degraded') return 'warning';
  if (severity === 'protected') return 'success';
  return 'default';
}

function technicianLabel(user: User) {
  const displayName = user.displayName || user.email;
  return displayName === user.email ? user.email : `${displayName} · ${user.email}`;
}

function renderTemplateText(template: string, values: Record<string, string | number | boolean>) {
  return template.replace(/\{\{\s*([a-z][a-z0-9_]*)\s*\}\}/gi, (_, key: string) => {
    const value = values[key.toLowerCase()];
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    return value === undefined || value === null ? '' : String(value);
  });
}

function parameterHasValue(parameter: ChangeTemplateParameter, value: string | number | boolean | undefined) {
  if (parameter.type === 'boolean') return typeof value === 'boolean';
  return value !== undefined && value !== null && String(value).trim() !== '';
}

function isBusinessSystemType(value: string) {
  const normalized = value.trim().toLowerCase().replace(/[_-]+/g, ' ');
  return ['business system', 'business application', 'business service'].includes(normalized);
}

function closureRequiresPir(change: ChangePackage, assessment: ChangeClosureAssessment) {
  return change.changeType === 'emergency'
    || ['high', 'critical'].includes(change.riskLevel)
    || change.rollbackExecuted
    || assessment.implementationResult !== 'successful'
    || assessment.serviceStatus !== 'restored'
    || Boolean(assessment.unexpectedImpact.trim())
    || assessment.tests.some(test => test.result === 'failed');
}

function closureDraft(change: ChangePackage): ChangeClosureAssessment {
  if (change.closureAssessment?.tests?.length) {
    const draft = structuredClone(change.closureAssessment);
    draft.pirRequired = closureRequiresPir(change, draft);
    return draft;
  }
  const scope = sortedImpactItems(change);
  const primary = scope.find(item => item.role === 'Scope') || scope[0];
  const businessSystem = scope.find(item => isBusinessSystemType(item.type));
  const context = {
    company_name: change.companyName,
    asset_name: primary?.name || '',
    business_system_name: businessSystem?.name || '',
    ...(change.templateParameters || {}),
  };
  const definitions = change.templateSnapshot?.content.closureTests || [];
  const tests: ChangeClosureTest[] = definitions.length ? definitions.map(test => ({
    key: test.key,
    label: renderTemplateText(test.label, context),
    expectedResult: renderTemplateText(test.expectedResultTemplate, context),
    required: test.required,
    evidenceRequired: test.evidenceRequired,
    result: 'pending',
    actualResult: '',
    evidence: '',
    testedBy: '',
    testedAt: '',
  })) : [{
    key: 'validation_plan',
    label: 'Validation plan',
    expectedResult: change.validationPlan,
    required: true,
    evidenceRequired: false,
    result: 'pending',
    actualResult: '',
    evidence: '',
    testedBy: '',
    testedAt: '',
  }];
  const outcome = ['successful', 'successful_with_issues', 'partially_implemented', 'failed', 'backed_out'].includes(change.outcome)
    ? change.outcome as ChangeClosureAssessment['implementationResult']
    : 'successful';
  const draft: ChangeClosureAssessment = {
    implementationResult: outcome,
    serviceStatus: 'restored',
    deviations: '',
    unexpectedImpact: '',
    tests,
    pirRequired: false,
    pirCompleted: false,
    lessonsLearned: '',
    followUpActions: [],
    stakeholderConfirmation: 'not_required',
    closureSummary: '',
    closedBy: '',
    closedAt: '',
  };
  draft.pirRequired = closureRequiresPir(change, draft);
  return draft;
}

function closureReadiness(change: ChangePackage, assessment: ChangeClosureAssessment) {
  const issues: string[] = [];
  assessment.tests.forEach(test => {
    if (test.required && ['pending', 'not_run'].includes(test.result)) issues.push(`Complete ${test.label}.`);
    if (['passed', 'failed'].includes(test.result) && test.actualResult.trim().length < 4) issues.push(`Record the actual result for ${test.label}.`);
    if (test.evidenceRequired && ['passed', 'failed'].includes(test.result) && test.evidence.trim().length < 3) issues.push(`Record evidence for ${test.label}.`);
  });
  if (assessment.implementationResult === 'successful' && assessment.tests.some(test => test.required && test.result !== 'passed')) issues.push('Every required test must pass for a successful result.');
  if (['successful', 'successful_with_issues'].includes(assessment.implementationResult) && assessment.serviceStatus !== 'restored') issues.push('A successful result requires restored service.');
  if (assessment.serviceStatus !== 'restored' && assessment.unexpectedImpact.trim().length < 4) issues.push('Describe the remaining service impact.');
  const pirRequired = closureRequiresPir(change, assessment);
  if (pirRequired && !assessment.pirCompleted) issues.push('Complete the required post-implementation review.');
  if (pirRequired && assessment.lessonsLearned.trim().length < 4) issues.push('Record lessons learned.');
  assessment.followUpActions.filter(action => action.description.trim()).forEach(action => {
    if (action.status === 'open' && (!action.owner.trim() || !action.dueDate)) issues.push(`Add an owner and due date for “${action.description}”.`);
  });
  if ((change.impactSummary?.businessSystemCount || 0) > 0 && ['high', 'critical'].includes(change.riskLevel) && assessment.stakeholderConfirmation !== 'confirmed') issues.push('Confirm stakeholder acceptance for this high-risk business impact.');
  if (assessment.closureSummary.trim().length < 4) issues.push('Enter a concise closure summary.');
  return [...new Set(issues)];
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
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const affectedAssetId = searchParams.get('affectedAssetId') || '';
  const requestedChangeId = searchParams.get('changeId') || '';
  const [assets, setAssets] = useState<Asset[]>([]);
  const [changes, setChanges] = useState<ChangePackage[]>([]);
  const [technicians, setTechnicians] = useState<User[]>([]);
  const [templates, setTemplates] = useState<ChangeTemplate[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState<ChangeTemplate | null>(null);
  const [templateParameters, setTemplateParameters] = useState<Record<string, string | number | boolean>>({});
  const [templateApplied, setTemplateApplied] = useState(false);
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
  const [assignmentFilter, setAssignmentFilter] = useState('all');
  const [transitionTarget, setTransitionTarget] = useState<string | null>(null);
  const [transitionData, setTransitionData] = useState({
    reason: '', actualStart: '', actualEnd: '', actualOutageMinutes: '0', validationResult: '', rollbackResult: '', closureNotes: '',
    plannedStart: '', plannedEnd: '', communicationStatus: 'required', communicationPlan: '', notes: '',
  });
  const [closureStep, setClosureStep] = useState(0);
  const [closureData, setClosureData] = useState<ChangeClosureAssessment | null>(null);
  const [assignmentOpen, setAssignmentOpen] = useState(false);
  const [assignmentTargetId, setAssignmentTargetId] = useState('');
  const [assignmentReason, setAssignmentReason] = useState('');
  const [assignmentNotify, setAssignmentNotify] = useState(true);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error' | 'info'; message: string } | null>(null);
  const canCreate = ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');

  const changesUrl = useCallback(() => {
    const query = new URLSearchParams({ companyId: workspace.companyId });
    if (affectedAssetId) query.set('assetId', affectedAssetId);
    return `/api/changes?${query.toString()}`;
  }, [affectedAssetId, workspace.companyId]);

  const loadChanges = useCallback(async () => {
    if (workspace.isRoot) return [];
    const records = await apiFetch<ChangePackage[]>(changesUrl());
    setChanges(records);
    setSelectedChange(current => current ? records.find(item => item.id === current.id) || null : null);
    return records;
  }, [changesUrl, workspace.isRoot]);

  useEffect(() => {
    setAssets([]); setChanges([]); setTechnicians([]); setTemplates([]); setScopeAssets([]); setSelectedTemplate(null); setTemplateParameters({}); setTemplateApplied(false); setPreview(null); setSaved(null); setEditing(null); setSelectedChange(null); setTransitionTarget(null); setClosureData(null); setClosureStep(0); setAssignmentOpen(false); setAssignmentFilter('all'); setActiveStep(0); setForm(emptyForm()); setNotice(null);
    if (workspace.isRoot) return;
    Promise.all([
      apiFetch<Asset[]>(`/api/assets?companyId=${encodeURIComponent(workspace.companyId)}`),
      apiFetch<ChangePackage[]>(changesUrl()),
      canCreate ? apiFetch<User[]>(`/api/change-assignees?companyId=${encodeURIComponent(workspace.companyId)}`) : Promise.resolve([] as User[]),
      canCreate ? apiFetch<ChangeTemplate[]>(`/api/change-templates?companyId=${encodeURIComponent(workspace.companyId)}`) : Promise.resolve([] as ChangeTemplate[]),
    ]).then(([assetRecords, changeRecords, technicianRecords, templateRecords]) => { setAssets(assetRecords); setChanges(changeRecords); setTechnicians(technicianRecords); setTemplates(templateRecords); })
      .catch(error => setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Change control could not be loaded.' }));
  }, [workspace.companyId, workspace.isRoot, canCreate, changesUrl]);

  useEffect(() => {
    const assetId = searchParams.get('assetId');
    if (!assetId || !assets.length || scopeAssets.length) return;
    const selected = assets.find(asset => asset.id === assetId);
    if (selected) setScopeAssets([selected]);
  }, [assets, scopeAssets.length, searchParams]);

  useEffect(() => {
    if (!selectedTemplate || templateApplied) return;
    const primaryName = scopeAssets[0]?.name || '';
    setTemplateParameters(current => {
      const next = { ...current };
      let changed = false;
      selectedTemplate.content.parameters.forEach(parameter => {
        if (parameter.source === 'scope_primary_name' && next[parameter.key] !== primaryName) {
          next[parameter.key] = primaryName;
          changed = true;
        }
      });
      return changed ? next : current;
    });
  }, [scopeAssets, selectedTemplate, templateApplied]);

  useEffect(() => {
    if (!requestedChangeId || !changes.length) return;
    const selected = changes.find(change => change.id === requestedChangeId);
    if (selected) setSelectedChange(selected);
  }, [changes, requestedChangeId]);

  useEffect(() => {
    let active = true;
    if (!selectedChange || !canCreate) { setApprovalRequests([]); return; }
    setApprovalsLoading(true);
    apiFetch<ChangeApprovalRequest[]>(`/api/changes/${encodeURIComponent(selectedChange.id)}/approval-requests`)
      .then(records => { if (active) setApprovalRequests(records); })
      .catch(error => { if (active) setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Approval history could not be loaded.' }); })
      .finally(() => { if (active) setApprovalsLoading(false); });
    return () => { active = false; };
  }, [selectedChange, canCreate]);

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
    setForm(emptyForm()); setScopeAssets([]); setSelectedTemplate(null); setTemplateParameters({}); setTemplateApplied(false); setPreview(null); setSaved(null); setEditing(null); setActiveStep(0); setNotice(null);
  };

  const beginEdit = (change: ChangePackage) => {
    setEditing(change); setSelectedChange(null); setSaved(null); setSelectedTemplate(null); setTemplateParameters({}); setTemplateApplied(false); setForm(formFromChange(change));
    setScopeAssets(assets.filter(asset => change.scopeAssetIds.includes(asset.id))); setActiveStep(0); setNotice(null);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const templateContext = (parameters = templateParameters) => {
    const primaryAsset = scopeAssets[0];
    const businessSystem = scopeAssets.find(asset => isBusinessSystemType(asset.type));
    return {
      company_name: workspace.companyName,
      asset_name: primaryAsset?.name || '',
      business_system_name: businessSystem?.name || '',
      ...parameters,
    };
  };

  const chooseTemplate = (template: ChangeTemplate | null) => {
    setSelectedTemplate(template);
    setTemplateApplied(false);
    setSaved(null);
    if (!template) {
      setTemplateParameters({});
      return;
    }
    const context = templateContext({});
    setTemplateParameters(Object.fromEntries(template.content.parameters.map(parameter => {
      const automatic = parameter.source === 'scope_primary_name' ? context.asset_name : '';
      return [parameter.key, parameter.type === 'boolean' ? false : automatic];
    })));
  };

  const setTemplateParameter = (parameter: ChangeTemplateParameter, value: string | number | boolean) => {
    setTemplateParameters(current => ({ ...current, [parameter.key]: value }));
    setTemplateApplied(false);
    setSaved(null);
  };

  const applyTemplate = () => {
    if (!selectedTemplate) return;
    const missing = selectedTemplate.content.parameters.filter(parameter =>
      parameter.required && !parameterHasValue(parameter, templateParameters[parameter.key]));
    if (missing.length) {
      setNotice({ severity: 'error', message: `Complete the required template input${missing.length === 1 ? '' : 's'}: ${missing.map(item => item.label).join(', ')}.` });
      return;
    }
    const context = templateContext();
    const content = selectedTemplate.content;
    setForm(current => ({
      ...current,
      title: renderTemplateText(content.titleTemplate, context),
      changeType: content.changeType,
      category: content.category,
      priority: content.priority,
      outageExpected: content.outageExpected ? 'yes' : 'no',
      reason: renderTemplateText(content.reasonTemplate, context),
      businessImpact: renderTemplateText(content.businessImpactTemplate, context),
      implementationPlan: renderTemplateText(content.implementationPlanTemplate, context),
      validationPlan: renderTemplateText(content.validationPlanTemplate, context),
      rollbackPlan: renderTemplateText(content.rollbackPlanTemplate, context),
      communicationStatus: content.communicationStatus,
      communicationPlan: renderTemplateText(content.communicationPlanTemplate, context),
    }));
    setTemplateApplied(true);
    setNotice({ severity: 'success', message: `${selectedTemplate.name} version ${selectedTemplate.version} was applied. Review and adapt the procedure before saving.` });
  };

  const openTransition = (change: ChangePackage, target: string) => {
    setSelectedChange(change); setTransitionTarget(target);
    setClosureStep(0);
    setClosureData(target === 'closed' ? closureDraft(change) : null);
    setTransitionData({
      reason: '', actualStart: '', actualEnd: '', actualOutageMinutes: String(change.actualOutageMinutes || 0),
      validationResult: change.validationResult || '', rollbackResult: change.rollbackResult || '', closureNotes: change.closureNotes || '',
      plannedStart: change.plannedStart || '', plannedEnd: change.plannedEnd || '',
      communicationStatus: change.communicationStatus || 'required', communicationPlan: change.communicationPlan || '', notes: change.notes || '',
    });
  };

  const applyTransition = async () => {
    if (!selectedChange || !transitionTarget) return;
    if (transitionTarget === 'closed' && !closureData) return;
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
            communicationStatus: transitionData.communicationStatus, communicationPlan: transitionData.communicationPlan,
            notes: transitionData.notes,
          } : transitionTarget === 'closed' ? {
            status: transitionTarget,
            expectedRevision: selectedChange.revision || 1,
            reason: closureData?.closureSummary || '',
            closureNotes: closureData?.closureSummary || '',
            closureAssessment: closureData,
          } : {
            status: transitionTarget, expectedRevision: selectedChange.revision || 1, ...transitionData,
            actualOutageMinutes: Number(transitionData.actualOutageMinutes || 0),
          }),
        },
      );
      setTransitionTarget(null); setClosureData(null); setClosureStep(0); setSelectedChange(updated); await loadChanges();
      setNotice({ severity: 'success', message: scheduleEdit ? `${updated.number} schedule was updated.` : `${updated.number} is now ${sentence(updated.status)}.` });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The change could not be updated.' });
    } finally { setSaving(false); }
  };

  const updateClosure = <K extends keyof ChangeClosureAssessment>(key: K, value: ChangeClosureAssessment[K]) => {
    setClosureData(current => current ? { ...current, [key]: value } : current);
  };

  const updateClosureTest = (index: number, updates: Partial<ChangeClosureTest>) => {
    setClosureData(current => current ? {
      ...current,
      tests: current.tests.map((test, position) => position === index ? { ...test, ...updates } : test),
    } : current);
  };

  const updateFollowUp = (index: number, updates: Partial<ChangeClosureFollowUp>) => {
    setClosureData(current => current ? {
      ...current,
      followUpActions: current.followUpActions.map((action, position) => position === index ? { ...action, ...updates } : action),
    } : current);
  };

  const addFollowUp = () => {
    setClosureData(current => current ? {
      ...current,
      followUpActions: [...current.followUpActions, {
        id: crypto.randomUUID(),
        description: '',
        owner: '',
        dueDate: '',
        status: 'open',
      }],
    } : current);
  };

  const removeFollowUp = (index: number) => {
    setClosureData(current => current ? {
      ...current,
      followUpActions: current.followUpActions.filter((_, position) => position !== index),
    } : current);
  };

  const openAssignment = (change: ChangePackage) => {
    setSelectedChange(change);
    setAssignmentTargetId(change.assignedUserId || '');
    setAssignmentReason('');
    setAssignmentNotify(true);
    setAssignmentOpen(true);
  };

  const applyAssignment = async () => {
    if (!selectedChange || assignmentReason.trim().length < 4) return;
    setSaving(true); setNotice(null);
    try {
      const updated = await apiFetch<ChangePackage>(`/api/changes/${encodeURIComponent(selectedChange.id)}/assignment`, {
        method: 'POST',
        body: JSON.stringify({
          expectedRevision: selectedChange.revision || 1,
          assignedUserId: assignmentTargetId || null,
          reason: assignmentReason.trim(),
          notify: assignmentNotify,
        }),
      });
      setAssignmentOpen(false);
      setSelectedChange(updated);
      await loadChanges();
      const assignee = updated.assignedTechnician || 'Unassigned';
      const delivery = updated.assignmentNotification?.queued
        ? ` An email was queued for ${updated.assignmentNotification.recipient}.`
        : updated.assignmentNotification?.requested
          ? ' Email delivery is not currently configured, so no message was queued.'
          : '';
      setNotice({ severity: updated.assignmentNotification?.requested && !updated.assignmentNotification.queued ? 'info' : 'success', message: `${updated.number} is now assigned to ${assignee}.${delivery}` });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The change could not be reassigned.' });
    } finally {
      setSaving(false);
    }
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
    if (activeStep === 0) return Boolean(form.title.trim() && scopeAssets.length && (!selectedTemplate || templateApplied) && (!form.plannedStart || !form.plannedEnd || form.plannedEnd > form.plannedStart));
    if (activeStep === 1) return Boolean(preview && form.businessImpact.trim());
    if (activeStep === 2) return Boolean(form.reason.trim() && form.implementationPlan.trim() && form.validationPlan.trim() && form.rollbackPlan.trim());
    return true;
  }, [activeStep, form, preview, scopeAssets.length, selectedTemplate, templateApplied]);

  const saveAndDownload = async () => {
    if (!preview) return;
    setSaving(true); setNotice(null);
    try {
      const { assignedUserId, ...editableForm } = form;
      const change = await apiFetch<ChangePackage>(editing ? `/api/changes/${encodeURIComponent(editing.id)}` : '/api/changes', {
        method: editing ? 'PATCH' : 'POST',
        body: JSON.stringify({
          ...editableForm,
          ...(editing ? { expectedRevision: editing.revision || 1 } : { companyId: workspace.companyId, assignedUserId: assignedUserId || null }),
          ...(!editing && selectedTemplate && templateApplied ? {
            templateId: selectedTemplate.id,
            templateVersion: selectedTemplate.version,
            templateParameters,
          } : {}),
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

  const assignmentFilterOptions = useMemo(() => [
    { id: 'all', label: 'All assignees' },
    { id: 'mine', label: 'My changes' },
    { id: 'unassigned', label: 'Unassigned' },
    ...technicians.map(user => ({ id: `user:${user.id}`, label: technicianLabel(user) })),
  ], [technicians]);
  const selectedFormTechnician = technicians.find(item => item.id === form.assignedUserId) || null;
  const formTechnicianName = selectedFormTechnician?.displayName || selectedFormTechnician?.email || editing?.assignedTechnician || 'Not assigned';

  const visibleChanges = useMemo(() => {
    const sessionUser = getSession()?.user;
    return changes.filter(change => {
      const matchesStatus = statusFilter === 'all'
        || (statusFilter === 'active' && !['closed', 'cancelled'].includes(change.status))
        || change.status === statusFilter;
      if (!matchesStatus || assignmentFilter === 'all') return matchesStatus;
      if (assignmentFilter === 'unassigned') {
        return !change.assignedUserId && !change.assignedTechnician;
      }
      if (assignmentFilter === 'mine') {
        return Boolean(sessionUser && (
          change.assignedUserId === sessionUser.id
          || (!change.assignedUserId && (change.assignedTechnician || '').toLowerCase() === sessionUser.email.toLowerCase())
        ));
      }
      const userId = assignmentFilter.startsWith('user:') ? assignmentFilter.slice(5) : '';
      const technician = technicians.find(item => item.id === userId);
      return change.assignedUserId === userId
        || Boolean(technician && !change.assignedUserId && (change.assignedTechnician || '').toLowerCase() === technician.email.toLowerCase());
    });
  }, [assignmentFilter, changes, statusFilter, technicians]);
  const closureIssues = selectedChange && closureData ? closureReadiness(selectedChange, closureData) : [];
  const closurePirRequired = Boolean(selectedChange && closureData && closureRequiresPir(selectedChange, closureData));

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
      {affectedAssetId && <Alert
        severity="info"
        sx={{ mb: 2 }}
        action={<Button color="inherit" onClick={() => setSearchParams({})}>Clear asset filter</Button>}
      >
        Showing changes involving {assets.find(asset => asset.id === affectedAssetId)?.name || 'the selected asset'}, including calculated downstream impact.
      </Alert>}
      {editing && <Alert severity="info" sx={{ mb: 2 }} action={<Button color="inherit" onClick={reset}>Stop editing</Button>}>
        Editing {editing.number} revision {editing.revision}. Saving creates a new immutable revision and refreshes its CMDB impact snapshot.
      </Alert>}
      {!canCreate && <Alert severity="info" sx={{ mb: 2 }}>You can review and download existing change packages. An MSP operator or platform administrator must generate new packages.</Alert>}

      {canCreate && <Card className="change-wizard">
        <CardContent>
          <Stepper activeStep={activeStep} alternativeLabel sx={{ mb: 4 }}>{steps.map(label => <Step key={label}><StepLabel>{label}</StepLabel></Step>)}</Stepper>

          {activeStep === 0 && <Grid container spacing={2.5}>
            {!editing && <Grid size={{ xs: 12 }}><Paper variant="outlined" sx={{ p: 2.5, borderColor: selectedTemplate ? 'primary.main' : 'divider', bgcolor: selectedTemplate ? 'rgba(80,213,185,.035)' : undefined }}>
              <Stack direction={{ xs: 'column', lg: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { lg: 'flex-start' } }}>
                <Box sx={{ flex: 1 }}><Typography variant="overline" color="primary">Start faster</Typography><Typography variant="h6">Use a governed procedure</Typography><Typography color="text.secondary">Choose a standard or customer template, answer its prompts, then review the generated plan. You can also continue with a blank change.</Typography></Box>
                <Autocomplete
                  sx={{ minWidth: { lg: 410 } }}
                  options={templates}
                  value={selectedTemplate}
                  groupBy={option => option.companyId ? `${workspace.companyName} procedures` : 'Global standards'}
                  getOptionLabel={option => option.name}
                  isOptionEqualToValue={(option, value) => option.id === value.id}
                  onChange={(_, value) => chooseTemplate(value)}
                  renderOption={(props, option) => <li {...props} key={option.id}><Box><Typography sx={{ fontWeight: 750 }}>{option.name}</Typography><Typography variant="caption" color="text.secondary">{sentence(option.content.changeType)} · {sentence(option.content.category)} · v{option.version}</Typography></Box></li>}
                  renderInput={params => <TextField {...params} label="Change template (optional)" placeholder="Search procedures…" />}
                />
              </Stack>
              {selectedTemplate && <Box sx={{ mt: 2 }}>
                <Divider sx={{ mb: 2 }} />
                <Stack direction={{ xs: 'column', md: 'row' }} spacing={1} sx={{ justifyContent: 'space-between', mb: 2 }}>
                  <Box><Typography sx={{ fontWeight: 800 }}>{selectedTemplate.name}</Typography><Typography variant="body2" color="text.secondary">{selectedTemplate.description}</Typography></Box>
                  <Stack direction="row" spacing={0.75}><Chip size="small" label={`v${selectedTemplate.version}`} /><Chip size="small" variant="outlined" label={`${selectedTemplate.content.expectedDurationMinutes} min`} />{templateApplied && <Chip size="small" color="success" label="Applied" />}</Stack>
                </Stack>
                {selectedTemplate.content.parameters.length > 0 && <Grid container spacing={1.5} sx={{ mb: 2 }}>
                  {selectedTemplate.content.parameters.map(parameter => <Grid key={parameter.key} size={{ xs: 12, md: parameter.type === 'multiline' ? 12 : 6, lg: parameter.type === 'multiline' ? 12 : 4 }}>
                    {parameter.type === 'select' ? <FormControl fullWidth required={parameter.required}><InputLabel>{parameter.label}</InputLabel><Select label={parameter.label} value={String(templateParameters[parameter.key] ?? '')} onChange={event => setTemplateParameter(parameter, event.target.value)}>
                      {parameter.options.map(option => <MenuItem key={option} value={option}>{option}</MenuItem>)}
                    </Select></FormControl> : parameter.type === 'boolean' ? <FormControlLabel control={<Checkbox checked={Boolean(templateParameters[parameter.key])} onChange={event => setTemplateParameter(parameter, event.target.checked)} />} label={parameter.label} /> : <TextField
                      fullWidth required={parameter.required} multiline={parameter.type === 'multiline'} minRows={parameter.type === 'multiline' ? 3 : undefined}
                      type={parameter.type === 'number' ? 'number' : 'text'} label={parameter.label}
                      value={String(templateParameters[parameter.key] ?? '')}
                      onChange={event => setTemplateParameter(parameter, parameter.type === 'number' && event.target.value !== '' ? Number(event.target.value) : event.target.value)}
                      helperText={parameter.helpText || (parameter.source === 'scope_primary_name' ? 'Select a CI below to auto-fill this value, or enter it manually.' : undefined)}
                    />}
                  </Grid>)}
                </Grid>}
                <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ alignItems: { sm: 'center' } }}>
                  <Button variant="contained" startIcon={<AutoAwesomeOutlined />} onClick={applyTemplate}>{templateApplied ? 'Reapply procedure' : 'Apply procedure'}</Button>
                  <Typography variant="caption" color="text.secondary">Applying replaces the procedure fields below but never changes scope, schedule, calculated risk, technician or approval identities.</Typography>
                </Stack>
              </Box>}
            </Paper></Grid>}
            <Grid size={{ xs: 12 }}><TextField fullWidth required label="Change title" value={form.title} onChange={event => update('title', event.target.value)} helperText="Use a short outcome-focused title that will also work as a future ConnectWise ticket summary." /></Grid>
            <Grid size={{ xs: 12 }}><Autocomplete multiple options={assets} value={scopeAssets} getOptionLabel={option => `${option.name} (${option.type})`} isOptionEqualToValue={(option, value) => option.id === value.id} onChange={(_, value) => { setSaved(null); setScopeAssets(value); }} renderInput={params => <TextField {...params} required label="Configuration items in scope" helperText="Downstream impact is calculated automatically from the relationship map." />} /></Grid>
            <Grid size={{ xs: 12, sm: 6, lg: 3 }}><FormControl fullWidth><InputLabel>Change type</InputLabel><Select label="Change type" value={form.changeType} onChange={event => update('changeType', event.target.value)}><MenuItem value="standard">Standard</MenuItem><MenuItem value="normal">Normal</MenuItem><MenuItem value="emergency">Emergency</MenuItem></Select></FormControl></Grid>
            <Grid size={{ xs: 12, sm: 6, lg: 3 }}><FormControl fullWidth><InputLabel>Category</InputLabel><Select label="Category" value={form.category} onChange={event => update('category', event.target.value)}>{['infrastructure', 'network', 'software', 'application', 'database', 'security', 'cloud', 'other'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}</Select></FormControl></Grid>
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
            <Grid size={{ xs: 12, md: 4 }}><Autocomplete options={technicians} value={selectedFormTechnician} disabled={Boolean(editing)} getOptionLabel={technicianLabel} isOptionEqualToValue={(option, value) => option.id === value.id} onChange={(_, value) => update('assignedUserId', value?.id || '')} renderInput={params => <TextField {...params} label="Assigned technician" helperText={editing ? `Current: ${editing.assignedTechnician || 'Unassigned'}. Use Reassign from the change record.` : 'Only active MSP technicians with customer access are available.'} />} /></Grid>
            <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Approver / CAB owner" value={form.approver} onChange={event => update('approver', event.target.value)} /></Grid>
            <Grid size={{ xs: 12, lg: 6 }}><TextField fullWidth multiline minRows={3} label="Communication plan" value={form.communicationPlan} onChange={event => update('communicationPlan', event.target.value)} helperText="Audience, timing, method and responsible person." /></Grid>
            <Grid size={{ xs: 12, lg: 6 }}><TextField fullWidth multiline minRows={3} label="Additional notes" value={form.notes} onChange={event => update('notes', event.target.value)} /></Grid>
          </Grid>}

          {activeStep === 3 && preview && <Grid container spacing={2.5}>
            <Grid size={{ xs: 12, lg: 8 }}><Paper variant="outlined" sx={{ p: 3 }}><Stack direction="row" spacing={2} sx={{
              justifyContent: "space-between"
            }}><Box><Typography variant="overline" color="primary">{saved?.number || 'Draft change package'}</Typography><Typography variant="h4">{form.title}</Typography></Box><Chip color="info" label={sentence(form.changeType)} /></Stack><Divider sx={{ my: 2 }} /><Grid container spacing={2}>{[
              ['Customer', workspace.companyName], ['Schedule', `${formatDate(form.plannedStart)} - ${formatDate(form.plannedEnd)}`], ['Scope', `${preview.summary.scopeCount} selected CI(s)`], ['Calculated impact', `${preview.summary.directCount + preview.summary.downstreamCount} downstream CI(s)`], ['Business systems', `${preview.summary.businessSystemCount} affected`], ['Business owners', preview.summary.businessOwners.join(', ') || 'None recorded'], ['Risk', form.riskLevel === 'suggested' ? sentence(preview.summary.suggestedRisk.level) : sentence(form.riskLevel)], ['Expected outage', form.outageExpected === 'yes' ? 'Yes' : 'No'], ['Technician', formTechnicianName], ['Approver', form.approver || 'Not assigned'],
              ['Procedure', selectedTemplate && templateApplied ? `${selectedTemplate.name} · version ${selectedTemplate.version}` : editing?.templateSnapshot ? `${editing.templateSnapshot.name} · version ${editing.templateSnapshot.version}` : 'Blank change'],
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
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
            <FormControl size="small" sx={{ minWidth: 210 }}><InputLabel>Status</InputLabel><Select label="Status" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}>
              <MenuItem value="active">All active changes</MenuItem><MenuItem value="all">All changes</MenuItem>
              {Object.keys(statusColors).map(status => <MenuItem key={status} value={status}>{sentence(status)}</MenuItem>)}
            </Select></FormControl>
            <Autocomplete size="small" sx={{ minWidth: 290 }} options={assignmentFilterOptions} value={assignmentFilterOptions.find(item => item.id === assignmentFilter) || assignmentFilterOptions[0]} getOptionLabel={option => option.label} isOptionEqualToValue={(option, value) => option.id === value.id} onChange={(_, value) => setAssignmentFilter(value?.id || 'all')} renderInput={params => <TextField {...params} label="Assignment" />} />
          </Stack>
        </Stack>
        {!visibleChanges.length ? <Alert severity="info">No changes match this view.</Alert> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>Reference</TableCell><TableCell>Change</TableCell><TableCell>Status</TableCell><TableCell>Assigned to</TableCell><TableCell>Schedule</TableCell><TableCell>Risk</TableCell><TableCell>Affected assets</TableCell><TableCell align="right">Actions</TableCell></TableRow></TableHead><TableBody>{visibleChanges.slice(0, 50).map(change => {
          const impacted = sortedImpactItems(change);
          return <TableRow key={change.id} hover sx={{ cursor: 'pointer' }} onClick={() => setSelectedChange(change)}><TableCell><Typography sx={{ fontWeight: 800 }}>{change.number}</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>Revision {change.revision}</Typography></TableCell><TableCell><Typography sx={{ fontWeight: 700 }}>{change.title}</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>{sentence(change.changeType)} · {sentence(change.category)}</Typography></TableCell><TableCell><Chip size="small" color={statusColors[change.status] || 'default'} label={sentence(change.status)} /></TableCell><TableCell sx={{ minWidth: 160 }}><Typography variant="body2" sx={{ fontWeight: 650 }}>{change.assignedTechnician || 'Unassigned'}</Typography></TableCell><TableCell>{formatDate(change.plannedStart)}</TableCell><TableCell><Chip size="small" color={change.riskLevel === 'critical' ? 'error' : change.riskLevel === 'high' ? 'warning' : 'default'} label={sentence(change.riskLevel)} /></TableCell><TableCell sx={{ minWidth: 260 }}><Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap' }}>{impacted.slice(0, 3).map(item => <Chip key={item.assetId} size="small" variant={item.role === 'Scope' ? 'filled' : 'outlined'} color={impactColor(item.impactSeverity)} label={item.name} onClick={event => { event.stopPropagation(); navigate(`/assets/${encodeURIComponent(item.assetId)}/show`); }} />)}{impacted.length > 3 && <Chip size="small" variant="outlined" label={`+${impacted.length - 3} more`} />}</Stack><Typography variant="caption" color="text.secondary">{impacted.length} affected CI(s)</Typography></TableCell><TableCell align="right"><Button size="small" startIcon={<VisibilityOutlined />} onClick={event => { event.stopPropagation(); setSelectedChange(change); }}>Open</Button><Button size="small" startIcon={<DownloadOutlined />} onClick={event => { event.stopPropagation(); void downloadChange(change); }}>PDF</Button></TableCell></TableRow>;
        })}</TableBody></Table></TableContainer>}
      </CardContent></Card>

      <Dialog open={Boolean(selectedChange)} onClose={() => { if (!transitionTarget && !assignmentOpen) setSelectedChange(null); }} maxWidth="lg" fullWidth>
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
                ['Procedure', selectedChange.templateSnapshot ? `${selectedChange.templateSnapshot.name} · version ${selectedChange.templateSnapshot.version}` : 'Blank change'],
              ].map(([label, value]) => <Grid key={label} size={{ xs: 12, sm: 6 }}><Typography variant="caption" sx={{ color: 'text.secondary' }}>{label}</Typography><Typography sx={{ fontWeight: 700 }}>{value}</Typography></Grid>)}</Grid><Divider sx={{ my: 2 }} /><Typography variant="subtitle2">Reason and business impact</Typography><Typography sx={{ whiteSpace: 'pre-wrap', mb: 1 }}>{selectedChange.reason}</Typography><Typography sx={{ whiteSpace: 'pre-wrap', color: 'text.secondary' }}>{selectedChange.businessImpact || 'No additional business impact recorded.'}</Typography></Paper></Grid>
              <Grid size={{ xs: 12, md: 4 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Outcome</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>Current result</Typography><Typography sx={{ fontWeight: 800, mb: 1 }}>{sentence(selectedChange.outcome || 'pending')}</Typography><Typography variant="body2">Actual window: {selectedChange.actualStart || selectedChange.actualEnd ? `${formatDate(selectedChange.actualStart)} – ${formatDate(selectedChange.actualEnd)}` : 'Not started'}</Typography><Typography variant="body2">Actual outage: {selectedChange.actualOutageMinutes || 0} minute(s)</Typography>{selectedChange.failureReason && <Alert severity="error" sx={{ mt: 1 }}>{selectedChange.failureReason}</Alert>}{selectedChange.validationResult && <Alert severity="success" sx={{ mt: 1 }}>{selectedChange.validationResult}</Alert>}</Paper></Grid>
              {selectedChange.closureAssessment?.tests?.length ? <Grid size={{ xs: 12 }}><Paper variant="outlined" sx={{ p: 2 }}><Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'center' } }}><Box><Typography variant="h6">Post-change review</Typography><Typography variant="body2" color="text.secondary">{sentence(selectedChange.closureAssessment.implementationResult)} · service {sentence(selectedChange.closureAssessment.serviceStatus)}</Typography></Box><Stack direction="row" spacing={1}><Chip size="small" color="success" label={`${selectedChange.closureAssessment.tests.filter(test => test.result === 'passed').length} passed`} /><Chip size="small" color={selectedChange.closureAssessment.tests.some(test => test.result === 'failed') ? 'error' : 'default'} label={`${selectedChange.closureAssessment.tests.filter(test => test.result === 'failed').length} failed`} />{selectedChange.closureAssessment.pirRequired && <Chip size="small" color={selectedChange.closureAssessment.pirCompleted ? 'success' : 'warning'} label={selectedChange.closureAssessment.pirCompleted ? 'PIR completed' : 'PIR required'} />}</Stack></Stack><TableContainer sx={{ mt: 1.5 }}><Table size="small"><TableHead><TableRow><TableCell>Validation test</TableCell><TableCell>Result</TableCell><TableCell>Actual result and evidence</TableCell></TableRow></TableHead><TableBody>{selectedChange.closureAssessment.tests.map(test => <TableRow key={test.key}><TableCell><Typography sx={{ fontWeight: 700 }}>{test.label}</Typography><Typography variant="caption" color="text.secondary">{test.expectedResult}</Typography></TableCell><TableCell><Chip size="small" color={test.result === 'passed' ? 'success' : test.result === 'failed' ? 'error' : 'default'} label={sentence(test.result)} /></TableCell><TableCell><Typography variant="body2">{test.actualResult || 'Not recorded'}</Typography>{test.evidence && <Typography variant="caption" color="text.secondary">Evidence: {test.evidence}</Typography>}</TableCell></TableRow>)}</TableBody></Table></TableContainer>{selectedChange.closureAssessment.closureSummary && <Alert severity="success" sx={{ mt: 2 }}>{selectedChange.closureAssessment.closureSummary}</Alert>}</Paper></Grid> : null}
              <Grid size={{ xs: 12, md: 4 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Implementation plan</Typography><Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{selectedChange.implementationPlan}</Typography></Paper></Grid>
              <Grid size={{ xs: 12, md: 4 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Validation</Typography><Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{selectedChange.validationPlan}</Typography></Paper></Grid>
              <Grid size={{ xs: 12, md: 4 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Rollback</Typography><Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{selectedChange.rollbackPlan}</Typography>{selectedChange.rollbackResult && <Typography variant="body2" sx={{ mt: 1, fontWeight: 700 }}>Result: {selectedChange.rollbackResult}</Typography>}</Paper></Grid>
              <Grid size={{ xs: 12 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Affected assets</Typography><Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>Names and ownership reflect the immutable CMDB snapshot captured with this change revision.</Typography><TableContainer><Table size="small"><TableHead><TableRow><TableCell>Asset</TableCell><TableCell>Type</TableCell><TableCell>Relationship to change</TableCell><TableCell>Impact</TableCell><TableCell>Owner</TableCell></TableRow></TableHead><TableBody>{sortedImpactItems(selectedChange).map(item => <TableRow key={item.assetId} hover><TableCell><Button size="small" sx={{ justifyContent: 'flex-start', px: 0, fontWeight: 750 }} onClick={() => navigate(`/assets/${encodeURIComponent(item.assetId)}/show`)}>{item.name}</Button></TableCell><TableCell>{item.type}</TableCell><TableCell><Chip size="small" variant={item.role === 'Scope' ? 'filled' : 'outlined'} label={item.role} /></TableCell><TableCell><Chip size="small" color={impactColor(item.impactSeverity)} label={sentence(item.impactSeverity, 'Impacted')} /></TableCell><TableCell>{item.owner || 'No owner recorded'}</TableCell></TableRow>)}</TableBody></Table></TableContainer></Paper></Grid>
              {selectedChange.status === 'awaiting_approval' && <Grid size={{ xs: 12 }}><Paper variant="outlined" sx={{ p: 2 }}><Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'center' } }}><Box><Typography variant="h6">External sign-off workflow</Typography><Typography variant="body2" color="text.secondary">Approvers are resolved from active business-system sign-off delegates and owners. Links expire after 72 hours.</Typography></Box><Button variant="contained" startIcon={<SendOutlined />} disabled={saving || approvalsLoading} onClick={() => void sendApprovalRequests()}>{approvalRequests.some(item => item.status === 'pending') ? 'Replace approval batch' : 'Send approval requests'}</Button></Stack>{approvalsLoading ? <CircularProgress size={24} sx={{ mt: 2 }} /> : approvalRequests.length ? <Stack spacing={1} sx={{ mt: 2 }}>{approvalRequests.slice(0, 20).map(item => <Box key={item.id} sx={{ p: 1.5, border: '1px solid', borderColor: 'divider', borderRadius: 1.5 }}><Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' } }}><Box><Typography sx={{ fontWeight: 750 }}>{item.approverName} · {item.approverEmail}</Typography><Typography variant="caption" color="text.secondary">{item.scope.map(scope => scope.name).filter(Boolean).join(', ') || 'Entire change'} · expires {formatDate(item.expiresAt)}</Typography></Box><Stack direction="row" spacing={1}><Chip size="small" label={sentence(item.deliveryStatus)} color={item.deliveryStatus === 'accepted' ? 'success' : item.deliveryStatus === 'failed' ? 'error' : 'default'} /><Chip size="small" label={sentence(item.status)} color={item.status === 'approved' ? 'success' : item.status === 'declined' ? 'error' : item.status === 'pending' ? 'warning' : 'default'} /></Stack></Stack>{item.lastError && <Alert severity="error" sx={{ mt: 1 }}>{item.lastError}</Alert>}</Box>)}</Stack> : <Alert severity="info" sx={{ mt: 2 }}>No approval links have been sent yet.</Alert>}</Paper></Grid>}
              <Grid size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Approval evidence</Typography>{!(selectedChange.approvals || []).length ? <Typography sx={{ color: 'text.secondary' }}>No approval decision has been recorded.</Typography> : <Stack spacing={1} sx={{ mt: 1 }}>{(selectedChange.approvals || []).map(item => <Box key={item.id}><Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}><Chip size="small" color={item.decision === 'approved' ? 'success' : 'error'} label={sentence(item.decision)} /><Typography sx={{ fontWeight: 700 }}>{item.actorEmail}</Typography></Stack><Typography variant="body2">{item.comments || 'No comments'}</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>{formatDate(item.createdAt)}</Typography></Box>)}</Stack>}</Paper></Grid>
              <Grid size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Assignment history</Typography>{!(selectedChange.assignmentHistory || []).length ? <Typography sx={{ color: 'text.secondary' }}>No structured assignment changes have been recorded yet.</Typography> : <Stack spacing={1} sx={{ mt: 1 }}>{[...(selectedChange.assignmentHistory || [])].reverse().map(item => <Box key={item.id} sx={{ borderLeft: '2px solid', borderColor: 'primary.main', pl: 1.5 }}><Typography sx={{ fontWeight: 700 }}>{item.assignedDisplayName ? `Assigned to ${item.assignedDisplayName}` : 'Unassigned'}</Typography>{item.previousDisplayName && <Typography variant="caption" color="text.secondary">Previously {item.previousDisplayName}</Typography>}<Typography variant="body2">{item.reason}</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>{item.actorEmail || 'System'} · {formatDate(item.createdAt)}</Typography></Box>)}</Stack>}</Paper></Grid>
              <Grid size={{ xs: 12 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Lifecycle history</Typography><Stack spacing={1} sx={{ mt: 1 }}>{[...(selectedChange.statusHistory || [])].reverse().map(item => <Box key={item.id} sx={{ borderLeft: '2px solid', borderColor: 'divider', pl: 1.5 }}><Typography sx={{ fontWeight: 700 }}>{sentence(item.toStatus)}</Typography><Typography variant="body2">{item.reason || 'No comment'}</Typography><Typography variant="caption" sx={{ color: 'text.secondary' }}>{item.actorEmail || 'System'} · {formatDate(item.createdAt)}</Typography></Box>)}</Stack></Paper></Grid>
            </Grid>
          </DialogContent>
          <DialogActions sx={{ flexWrap: 'wrap', gap: 1, justifyContent: 'space-between' }}><Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}><Button startIcon={<DownloadOutlined />} onClick={() => void downloadChange(selectedChange)}>PDF</Button>{canCreate && !['closed', 'cancelled'].includes(selectedChange.status) && <Button startIcon={<PersonAddAltOutlined />} onClick={() => openAssignment(selectedChange)}>Reassign</Button>}{canCreate && ['draft', 'impact_review'].includes(selectedChange.status) && <Button startIcon={<EditOutlined />} onClick={() => beginEdit(selectedChange)}>Edit</Button>}{canCreate && ['approved', 'scheduled'].includes(selectedChange.status) && <Button startIcon={<ScheduleOutlined />} onClick={() => openTransition(selectedChange, 'edit_schedule')}>Edit schedule</Button>}</Stack><Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', justifyContent: 'flex-end' }}>{canCreate && (transitions[selectedChange.status] || []).filter(target => target !== 'approved' || !approvalRequests.length).map(target => <Button key={target} variant={['approved', 'completed', 'closed'].includes(target) ? 'contained' : 'outlined'} color={['declined', 'cancelled', 'failed'].includes(target) ? 'error' : target === 'backed_out' ? 'warning' : 'primary'} startIcon={target === 'approved' || target === 'completed' || target === 'closed' ? <CheckCircleOutlined /> : target === 'implementing' ? <PlayArrowOutlined /> : target === 'failed' || target === 'declined' ? <ReportProblemOutlined /> : target === 'backed_out' ? <UndoOutlined /> : target === 'cancelled' ? <CancelOutlined /> : <RateReviewOutlined />} onClick={() => openTransition(selectedChange, target)}>{transitionLabels[target]}</Button>)}<Button onClick={() => setSelectedChange(null)}>Close</Button></Stack></DialogActions>
        </>}
      </Dialog>

      <Dialog open={assignmentOpen} onClose={() => { if (!saving) setAssignmentOpen(false); }} maxWidth="sm" fullWidth>
        {selectedChange && <><DialogTitle>Reassign {selectedChange.number}</DialogTitle><DialogContent><Stack spacing={2} sx={{ mt: 1 }}>
          <Alert severity="info">Current technician: {selectedChange.assignedTechnician || 'Unassigned'}. Reassignment creates revision {(selectedChange.revision || 1) + 1} and immutable audit evidence.</Alert>
          <Autocomplete options={technicians} value={technicians.find(item => item.id === assignmentTargetId) || null} getOptionLabel={technicianLabel} isOptionEqualToValue={(option, value) => option.id === value.id} onChange={(_, value) => setAssignmentTargetId(value?.id || '')} renderInput={params => <TextField {...params} label="New technician" helperText="Clear the selection to explicitly unassign this change." />} />
          <TextField required label="Reason for reassignment" multiline minRows={3} value={assignmentReason} onChange={event => setAssignmentReason(event.target.value)} helperText="This reason is shown in assignment history and the audit trail." />
          <FormControlLabel control={<Checkbox checked={assignmentNotify} disabled={!assignmentTargetId} onChange={event => setAssignmentNotify(event.target.checked)} />} label="Email the new technician" />
        </Stack></DialogContent><DialogActions><Button disabled={saving} onClick={() => setAssignmentOpen(false)}>Cancel</Button><Button variant="contained" color={assignmentTargetId ? 'primary' : 'warning'} disabled={saving || assignmentReason.trim().length < 4 || (Boolean(assignmentTargetId) && assignmentTargetId === selectedChange.assignedUserId) || (!assignmentTargetId && !selectedChange.assignedTechnician)} onClick={() => void applyAssignment()}>{saving ? 'Saving…' : assignmentTargetId ? 'Confirm reassignment' : 'Confirm unassign'}</Button></DialogActions></>}
      </Dialog>

      <Dialog open={Boolean(transitionTarget)} onClose={() => { if (!saving) { setTransitionTarget(null); setClosureData(null); } }} maxWidth={transitionTarget === 'closed' ? 'lg' : 'sm'} fullWidth>
        {selectedChange && transitionTarget && <>
          <DialogTitle>{transitionTarget === 'edit_schedule' ? `Edit ${selectedChange.number} schedule` : transitionTarget === 'closed' ? `Post-change review · ${selectedChange.number}` : transitionLabels[transitionTarget]}</DialogTitle>
          <DialogContent dividers={transitionTarget === 'closed'}>
            {transitionTarget === 'closed' && closureData ? <Stack spacing={2}>
              <Stepper activeStep={closureStep} alternativeLabel>
                {['Outcome', 'Validation tests', 'PIR & closure'].map(label => <Step key={label}><StepLabel>{label}</StepLabel></Step>)}
              </Stepper>
              <Alert severity="info">Closing creates revision {(selectedChange.revision || 1) + 1}, freezes this evidence in the audit trail and updates the change PDF.</Alert>
              {closureStep === 0 && <Grid container spacing={2}>
                <Grid size={{ xs: 12 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="subtitle2">Recorded execution</Typography><Typography variant="body2">Actual window: {formatDate(selectedChange.actualStart)} – {formatDate(selectedChange.actualEnd)} · outage {selectedChange.actualOutageMinutes || 0} minute(s)</Typography></Paper></Grid>
                <Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth><InputLabel>Implementation result</InputLabel><Select label="Implementation result" value={closureData.implementationResult} onChange={event => updateClosure('implementationResult', event.target.value as ChangeClosureAssessment['implementationResult'])}>{['successful', 'successful_with_issues', 'partially_implemented', 'failed', 'backed_out'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}</Select></FormControl></Grid>
                <Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth><InputLabel>Current service state</InputLabel><Select label="Current service state" value={closureData.serviceStatus} onChange={event => updateClosure('serviceStatus', event.target.value as ChangeClosureAssessment['serviceStatus'])}>{['restored', 'degraded', 'unavailable'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}</Select></FormControl></Grid>
                <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth multiline minRows={3} label="Deviations from the approved plan" value={closureData.deviations} onChange={event => updateClosure('deviations', event.target.value)} helperText="Record what changed during implementation, or state that there were no deviations." /></Grid>
                <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth required={closureData.serviceStatus !== 'restored'} multiline minRows={3} label="Unexpected impact or remaining degradation" value={closureData.unexpectedImpact} onChange={event => updateClosure('unexpectedImpact', event.target.value)} /></Grid>
              </Grid>}
              {closureStep === 1 && <Stack spacing={2}>
                <Typography color="text.secondary">Record an observable result for every required test. Template requirements cannot be weakened during closure.</Typography>
                {closureData.tests.map((test, index) => <Paper key={test.key} variant="outlined" sx={{ p: 2 }}><Grid container spacing={2}>
                  <Grid size={{ xs: 12, md: 8 }}><Typography sx={{ fontWeight: 800 }}>{test.label}{test.required ? ' *' : ''}</Typography><Typography variant="body2" color="text.secondary">Expected: {test.expectedResult}</Typography>{test.evidenceRequired && <Chip size="small" color="info" sx={{ mt: 1 }} label="Evidence required" />}</Grid>
                  <Grid size={{ xs: 12, md: 4 }}><FormControl fullWidth><InputLabel>Result</InputLabel><Select label="Result" value={test.result} onChange={event => updateClosureTest(index, { result: event.target.value as ChangeClosureTest['result'] })}>{['pending', 'passed', 'failed', 'not_run'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}</Select></FormControl></Grid>
                  <Grid size={{ xs: 12, md: 7 }}><TextField fullWidth required={['passed', 'failed'].includes(test.result)} multiline minRows={2} label="Actual result" value={test.actualResult} onChange={event => updateClosureTest(index, { actualResult: event.target.value })} /></Grid>
                  <Grid size={{ xs: 12, md: 5 }}><TextField fullWidth required={test.evidenceRequired && ['passed', 'failed'].includes(test.result)} multiline minRows={2} label="Evidence or reference" value={test.evidence} onChange={event => updateClosureTest(index, { evidence: event.target.value })} helperText="Log excerpt, screenshot location, monitoring event or ticket reference." /></Grid>
                </Grid></Paper>)}
              </Stack>}
              {closureStep === 2 && <Stack spacing={2}>
                <Alert severity={closurePirRequired ? 'warning' : 'success'}>{closurePirRequired ? 'A post-implementation review is required by risk, outcome or impact rules.' : 'A formal PIR is not required, but lessons and follow-ups may still be recorded.'}</Alert>
                <FormControlLabel control={<Checkbox checked={closureData.pirCompleted} disabled={!closurePirRequired} onChange={event => updateClosure('pirCompleted', event.target.checked)} />} label={closurePirRequired ? 'Post-implementation review completed' : 'PIR not required'} />
                <TextField fullWidth required={closurePirRequired} multiline minRows={3} label="Lessons learned / PIR findings" value={closureData.lessonsLearned} onChange={event => updateClosure('lessonsLearned', event.target.value)} />
                <FormControl fullWidth><InputLabel>Stakeholder acceptance</InputLabel><Select label="Stakeholder acceptance" value={closureData.stakeholderConfirmation} onChange={event => updateClosure('stakeholderConfirmation', event.target.value as ChangeClosureAssessment['stakeholderConfirmation'])}><MenuItem value="not_required">Not required</MenuItem><MenuItem value="confirmed">Confirmed</MenuItem></Select></FormControl>
                <Divider><Chip label="Follow-up actions" /></Divider>
                <Stack direction="row" sx={{ justifyContent: 'space-between', alignItems: 'center' }}><Typography color="text.secondary">Open actions need an accountable owner and due date.</Typography><Button startIcon={<AddOutlined />} onClick={addFollowUp}>Add action</Button></Stack>
                {closureData.followUpActions.map((action, index) => <Paper key={action.id} variant="outlined" sx={{ p: 2 }}><Grid container spacing={1.5}>
                  <Grid size={{ xs: 12, md: 5 }}><TextField fullWidth label="Action" value={action.description} onChange={event => updateFollowUp(index, { description: event.target.value })} /></Grid>
                  <Grid size={{ xs: 12, md: 3 }}><TextField fullWidth label="Owner" value={action.owner} onChange={event => updateFollowUp(index, { owner: event.target.value })} /></Grid>
                  <Grid size={{ xs: 10, md: 2 }}><TextField fullWidth type="date" label="Due date" value={action.dueDate} onChange={event => updateFollowUp(index, { dueDate: event.target.value })} slotProps={{ inputLabel: { shrink: true } }} /></Grid>
                  <Grid size={{ xs: 2, md: 1 }}><IconButton color="error" aria-label="Remove follow-up action" onClick={() => removeFollowUp(index)}><DeleteOutlined /></IconButton></Grid>
                  <Grid size={{ xs: 12, md: 1 }}><FormControl fullWidth><InputLabel>Status</InputLabel><Select label="Status" value={action.status} onChange={event => updateFollowUp(index, { status: event.target.value as ChangeClosureFollowUp['status'] })}><MenuItem value="open">Open</MenuItem><MenuItem value="completed">Done</MenuItem></Select></FormControl></Grid>
                </Grid></Paper>)}
                <TextField fullWidth required multiline minRows={4} label="Closure summary" value={closureData.closureSummary} onChange={event => updateClosure('closureSummary', event.target.value)} helperText="Summarize the implemented result, validation and any residual risk." />
                {closureIssues.length ? <Alert severity="warning"><Typography sx={{ fontWeight: 700 }}>Complete before closing:</Typography><Box component="ul" sx={{ my: 0.5, pl: 2.5 }}>{closureIssues.map(issue => <li key={issue}>{issue}</li>)}</Box></Alert> : <Alert severity="success">All closure controls are complete.</Alert>}
              </Stack>}
            </Stack> : <Stack spacing={2} sx={{ mt: 1 }}>
              {transitionTarget === 'edit_schedule' ? <>
                <TextField label="Planned start" type="datetime-local" value={transitionData.plannedStart} onChange={event => setTransitionData(value => ({ ...value, plannedStart: event.target.value }))} slotProps={{ inputLabel: { shrink: true } }} />
                <TextField label="Planned end" type="datetime-local" value={transitionData.plannedEnd} onChange={event => setTransitionData(value => ({ ...value, plannedEnd: event.target.value }))} slotProps={{ inputLabel: { shrink: true } }} />
                <FormControl><InputLabel>Communication</InputLabel><Select label="Communication" value={transitionData.communicationStatus} onChange={event => setTransitionData(value => ({ ...value, communicationStatus: event.target.value }))}><MenuItem value="required">Required</MenuItem><MenuItem value="not_required">Not required</MenuItem><MenuItem value="completed">Completed</MenuItem></Select></FormControl>
                <TextField label="Communication plan" multiline minRows={2} value={transitionData.communicationPlan} onChange={event => setTransitionData(value => ({ ...value, communicationPlan: event.target.value }))} />
                <TextField label="Schedule notes" multiline minRows={2} value={transitionData.notes} onChange={event => setTransitionData(value => ({ ...value, notes: event.target.value }))} />
              </> : <>
                <Alert severity={['declined', 'cancelled', 'failed'].includes(transitionTarget) ? 'warning' : 'info'}>This action creates revision {(selectedChange.revision || 1) + 1} and an immutable audit event.</Alert>
                <TextField required={['declined', 'cancelled', 'failed', 'backed_out'].includes(transitionTarget)} label={transitionTarget === 'approved' ? 'Approval comments' : 'Reason / execution notes'} multiline minRows={3} value={transitionData.reason} onChange={event => setTransitionData(value => ({ ...value, reason: event.target.value }))} />
                {transitionTarget === 'implementing' && <TextField label="Actual start" type="datetime-local" value={transitionData.actualStart} onChange={event => setTransitionData(value => ({ ...value, actualStart: event.target.value }))} slotProps={{ inputLabel: { shrink: true } }} helperText="Leave blank to use the current time." />}
                {['completed', 'failed'].includes(transitionTarget) && <><TextField label="Actual end" type="datetime-local" value={transitionData.actualEnd} onChange={event => setTransitionData(value => ({ ...value, actualEnd: event.target.value }))} slotProps={{ inputLabel: { shrink: true } }} helperText="Leave blank to use the current time." /><TextField label="Actual outage (minutes)" type="number" value={transitionData.actualOutageMinutes} onChange={event => setTransitionData(value => ({ ...value, actualOutageMinutes: event.target.value }))} slotProps={{ htmlInput: { min: 0 } }} /><TextField label="Validation result" multiline minRows={3} value={transitionData.validationResult} onChange={event => setTransitionData(value => ({ ...value, validationResult: event.target.value }))} /></>}
                {transitionTarget === 'backed_out' && <TextField required label="Rollback result" multiline minRows={3} value={transitionData.rollbackResult} onChange={event => setTransitionData(value => ({ ...value, rollbackResult: event.target.value }))} />}
              </>}
            </Stack>}
          </DialogContent>
          {transitionTarget === 'closed' ? <DialogActions sx={{ justifyContent: 'space-between' }}>
            <Button disabled={saving} onClick={() => { setTransitionTarget(null); setClosureData(null); }}>Cancel</Button>
            <Stack direction="row" spacing={1}>{closureStep > 0 && <Button disabled={saving} onClick={() => setClosureStep(step => step - 1)}>Back</Button>}{closureStep < 2 ? <Button variant="contained" onClick={() => setClosureStep(step => step + 1)}>Next</Button> : <Button variant="contained" color="success" disabled={saving || closureIssues.length > 0} onClick={() => void applyTransition()}>{saving ? 'Closing…' : 'Close change'}</Button>}</Stack>
          </DialogActions> : <DialogActions><Button disabled={saving} onClick={() => setTransitionTarget(null)}>Cancel</Button><Button variant="contained" disabled={saving || (['declined', 'cancelled', 'failed', 'backed_out'].includes(transitionTarget) && transitionData.reason.trim().length < 4)} onClick={() => void applyTransition()}>{saving ? 'Saving…' : transitionTarget === 'edit_schedule' ? 'Save schedule' : 'Confirm action'}</Button></DialogActions>}
        </>}
      </Dialog>
    </Box>
  );
}
