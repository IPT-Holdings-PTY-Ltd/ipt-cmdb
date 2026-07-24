import AddOutlined from '@mui/icons-material/AddOutlined';
import ArchiveOutlined from '@mui/icons-material/ArchiveOutlined';
import ContentCopyOutlined from '@mui/icons-material/ContentCopyOutlined';
import EditOutlined from '@mui/icons-material/EditOutlined';
import LibraryBooksOutlined from '@mui/icons-material/LibraryBooksOutlined';
import PublishOutlined from '@mui/icons-material/PublishOutlined';
import DeleteOutlined from '@mui/icons-material/DeleteOutlined';
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  Divider, FormControl, FormControlLabel, Grid, IconButton, InputLabel, MenuItem, Paper, Select, Stack,
  Switch, TextField, Typography,
} from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { Title } from 'react-admin';
import { apiFetch, getSession } from './session';
import type { ChangeTemplate, ChangeTemplateClosureTest, ChangeTemplateContent, ChangeTemplateParameter, Company, User } from './types';
import { PageHeading, RootGuard } from './RootAdmin';
import { useWorkspace } from './workspace';

type TemplateEditor = {
  id: string;
  expectedVersion: number;
  companyId: string;
  key: string;
  name: string;
  description: string;
  tags: string;
  status: ChangeTemplate['status'];
  ownerUserId: string;
  reviewDueDate: string;
  content: ChangeTemplateContent;
};

const parameterTypes: ChangeTemplateParameter['type'][] = ['text', 'multiline', 'number', 'select', 'boolean'];
const stableKeyPattern = /^[a-z][a-z0-9_]{1,63}$/;
const contentFields: Array<{ key: keyof ChangeTemplateContent; label: string; rows: number; help: string }> = [
  { key: 'titleTemplate', label: 'Change title', rows: 1, help: 'Short, outcome-focused summary.' },
  { key: 'reasonTemplate', label: 'Reason', rows: 3, help: 'Why this work is necessary.' },
  { key: 'businessImpactTemplate', label: 'Business impact', rows: 3, help: 'Customer-facing consequence and benefit.' },
  { key: 'implementationPlanTemplate', label: 'Implementation plan', rows: 7, help: 'Numbered, repeatable technician steps.' },
  { key: 'validationPlanTemplate', label: 'Validation plan', rows: 5, help: 'Objective success checks and evidence.' },
  { key: 'rollbackPlanTemplate', label: 'Rollback plan', rows: 5, help: 'Trigger, steps and validation for recovery.' },
  { key: 'communicationPlanTemplate', label: 'Communication plan', rows: 4, help: 'Audience, timing, channel and owner.' },
];

function blankContent(): ChangeTemplateContent {
  return {
    titleTemplate: '', changeType: 'normal', category: 'infrastructure', priority: 'medium',
    outageExpected: false, expectedDurationMinutes: 60, reasonTemplate: '', businessImpactTemplate: '',
    implementationPlanTemplate: '', validationPlanTemplate: '', rollbackPlanTemplate: '',
    communicationStatus: 'required', communicationPlanTemplate: '', suggestedApproverRole: 'change_approver',
    closureTests: [], parameters: [],
  };
}

function blankEditor(companyId = ''): TemplateEditor {
  return {
    id: '', expectedVersion: 1, companyId, key: '', name: '', description: '', tags: '',
    status: 'draft', ownerUserId: '', reviewDueDate: '', content: blankContent(),
  };
}

function editorFromTemplate(template: ChangeTemplate): TemplateEditor {
  return {
    id: template.id,
    expectedVersion: template.version,
    companyId: template.companyId || '',
    key: template.key,
    name: template.name,
    description: template.description,
    tags: template.tags.join(', '),
    status: template.status,
    ownerUserId: template.ownerUserId || '',
    reviewDueDate: template.reviewDueDate || '',
    content: {
      ...blankContent(),
      ...structuredClone(template.content),
      closureTests: structuredClone(template.content.closureTests || []),
    },
  };
}

function copyEditor(template: ChangeTemplate): TemplateEditor {
  const editor = editorFromTemplate(template);
  return {
    ...editor,
    id: '',
    expectedVersion: 1,
    key: `${template.key.replace(/_copy(?:_\d+)?$/, '').slice(0, 54)}_copy`,
    name: `Copy of ${template.name}`.slice(0, 160),
    status: 'draft',
    ownerUserId: '',
  };
}

function sentence(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, match => match.toUpperCase());
}

function ownerLabel(user: User) {
  return user.displayName && user.displayName !== user.email ? `${user.displayName} · ${user.email}` : user.email;
}

function normalizeStableKey(value: string) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9_]/g, '_')
    .replace(/^[^a-z]+/, '')
    .slice(0, 64);
}

function editorKeysAreValid(editor: TemplateEditor) {
  const closureKeys = (editor.content.closureTests || []).map(test => test.key);
  const parameterKeys = editor.content.parameters.map(parameter => parameter.key);
  const keySetIsValid = (keys: string[]) =>
    keys.every(key => stableKeyPattern.test(key)) && new Set(keys).size === keys.length;
  return stableKeyPattern.test(editor.key)
    && keySetIsValid(closureKeys)
    && keySetIsValid(parameterKeys);
}

export function ChangeTemplatesPage() {
  const workspace = useWorkspace();
  const role = getSession()?.user.role;
  const platformAdmin = role === 'platform_admin';
  const [templates, setTemplates] = useState<ChangeTemplate[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [editor, setEditor] = useState<TemplateEditor | null>(null);
  const [search, setSearch] = useState('');
  const [scopeFilter, setScopeFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('active');
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error' | 'info'; message: string } | null>(null);

  const load = async () => {
    const [templateRecords, companyRecords, userRecords] = await Promise.all([
      apiFetch<ChangeTemplate[]>('/api/change-templates?includeDraft=true'),
      apiFetch<Company[]>('/api/companies'),
      apiFetch<User[]>('/api/users'),
    ]);
    setTemplates(templateRecords);
    setCompanies(companyRecords);
    setUsers(userRecords.filter(user => user.status === 'active'));
  };

  useEffect(() => {
    if (!workspace.isRoot) return;
    void load().catch(error => setNotice({
      severity: 'error',
      message: error instanceof Error ? error.message : 'The template library could not be loaded.',
    }));
  }, [workspace.isRoot]);

  const visibleTemplates = useMemo(() => templates.filter(template => {
    const haystack = `${template.name} ${template.description} ${template.key} ${template.tags.join(' ')}`.toLowerCase();
    const matchesSearch = !search.trim() || haystack.includes(search.trim().toLowerCase());
    const matchesScope = scopeFilter === 'all'
      || (scopeFilter === 'global' && !template.companyId)
      || template.companyId === scopeFilter;
    const matchesStatus = statusFilter === 'all'
      || (statusFilter === 'active' && template.status !== 'retired')
      || template.status === statusFilter;
    return matchesSearch && matchesScope && matchesStatus;
  }), [scopeFilter, search, statusFilter, templates]);

  const updateEditor = <K extends keyof TemplateEditor>(key: K, value: TemplateEditor[K]) => {
    setEditor(current => current ? { ...current, [key]: value } : current);
  };

  const updateContent = <K extends keyof ChangeTemplateContent>(key: K, value: ChangeTemplateContent[K]) => {
    setEditor(current => current ? { ...current, content: { ...current.content, [key]: value } } : current);
  };

  const updateParameter = (index: number, changes: Partial<ChangeTemplateParameter>) => {
    setEditor(current => {
      if (!current) return current;
      const parameters = current.content.parameters.map((parameter, position) =>
        position === index ? { ...parameter, ...changes } : parameter);
      return { ...current, content: { ...current.content, parameters } };
    });
  };

  const addParameter = () => {
    const index = (editor?.content.parameters.length || 0) + 1;
    updateContent('parameters', [
      ...(editor?.content.parameters || []),
      { key: `input_${index}`, label: `Input ${index}`, type: 'text', required: true, source: '', helpText: '', options: [] },
    ]);
  };

  const removeParameter = (index: number) => {
    updateContent('parameters', (editor?.content.parameters || []).filter((_, position) => position !== index));
  };

  const updateClosureTest = (index: number, changes: Partial<ChangeTemplateClosureTest>) => {
    setEditor(current => {
      if (!current) return current;
      const closureTests = (current.content.closureTests || []).map((test, position) =>
        position === index ? { ...test, ...changes } : test);
      return { ...current, content: { ...current.content, closureTests } };
    });
  };

  const addClosureTest = () => {
    const index = (editor?.content.closureTests?.length || 0) + 1;
    updateContent('closureTests', [
      ...(editor?.content.closureTests || []),
      {
        key: `validation_${index}`,
        label: `Validation check ${index}`,
        expectedResultTemplate: '',
        required: true,
        evidenceRequired: false,
      },
    ]);
  };

  const removeClosureTest = (index: number) => {
    updateContent('closureTests', (editor?.content.closureTests || []).filter((_, position) => position !== index));
  };

  const save = async () => {
    if (!editor) return;
    setSaving(true);
    setNotice(null);
    const payload = {
      ...(editor.id ? { expectedVersion: editor.expectedVersion } : { companyId: editor.companyId || null, key: editor.key }),
      name: editor.name,
      description: editor.description,
      tags: editor.tags.split(',').map(tag => tag.trim()).filter(Boolean),
      status: editor.status,
      ownerUserId: editor.ownerUserId || null,
      reviewDueDate: editor.reviewDueDate || null,
      content: editor.content,
    };
    try {
      const saved = await apiFetch<ChangeTemplate>(
        editor.id ? `/api/change-templates/${encodeURIComponent(editor.id)}` : '/api/change-templates',
        { method: editor.id ? 'PUT' : 'POST', body: JSON.stringify(payload) },
      );
      setEditor(null);
      await load();
      setNotice({
        severity: 'success',
        message: `${saved.name} ${editor.id ? `version ${saved.version}` : 'was created'} and is ${saved.status}.`,
      });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The template could not be saved.' });
    } finally {
      setSaving(false);
    }
  };

  const setLifecycle = async (template: ChangeTemplate, status: ChangeTemplate['status']) => {
    setSaving(true);
    setNotice(null);
    try {
      const saved = await apiFetch<ChangeTemplate>(`/api/change-templates/${encodeURIComponent(template.id)}`, {
        method: 'PUT',
        body: JSON.stringify({
          expectedVersion: template.version,
          name: template.name,
          description: template.description,
          tags: template.tags,
          status,
          ownerUserId: template.ownerUserId || null,
          reviewDueDate: template.reviewDueDate || null,
          content: template.content,
        }),
      });
      await load();
      setNotice({ severity: 'success', message: `${saved.name} is now ${saved.status} at version ${saved.version}.` });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The lifecycle could not be updated.' });
    } finally {
      setSaving(false);
    }
  };

  const selectedOwner = users.find(user => user.id === editor?.ownerUserId) || null;
  const selectedCompany = companies.find(company => company.id === editor?.companyId);

  return (
    <RootGuard>
      <Box>
        <Title title="Change templates" />
        <PageHeading
          eyebrow="Change enablement"
          title="Change template library"
          copy="Govern reusable technician procedures with customer scope, immutable versions, ownership and review dates. Templates accelerate authoring; CMDB impact, risk and approvals remain calculated per change."
          action={<Button variant="contained" startIcon={<AddOutlined />} onClick={() => setEditor(blankEditor())}>New template</Button>}
        />
        {notice && <Alert severity={notice.severity} onClose={() => setNotice(null)} sx={{ mb: 2 }}>{notice.message}</Alert>}
        <Alert severity="info" icon={<LibraryBooksOutlined />} sx={{ mb: 2 }}>
          Global templates are available to every customer. Customer templates can only be used inside their customer workspace. Editing or changing lifecycle creates a new immutable version.
        </Alert>
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
            <TextField fullWidth label="Search templates" value={search} onChange={event => setSearch(event.target.value)} />
            <FormControl sx={{ minWidth: 230 }}><InputLabel>Scope</InputLabel><Select label="Scope" value={scopeFilter} onChange={event => setScopeFilter(event.target.value)}>
              <MenuItem value="all">All scopes</MenuItem><MenuItem value="global">Global standards</MenuItem>
              {companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}
            </Select></FormControl>
            <FormControl sx={{ minWidth: 180 }}><InputLabel>Status</InputLabel><Select label="Status" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}>
              <MenuItem value="active">Active</MenuItem><MenuItem value="all">All</MenuItem>
              {['draft', 'published', 'retired'].map(status => <MenuItem key={status} value={status}>{sentence(status)}</MenuItem>)}
            </Select></FormControl>
          </Stack>
        </Paper>
        {!visibleTemplates.length ? <Alert severity="info">No templates match this view.</Alert> : <Grid container spacing={2}>
          {visibleTemplates.map(template => {
            const company = companies.find(item => item.id === template.companyId);
            return <Grid key={template.id} size={{ xs: 12, md: 6, xl: 4 }}>
              <Card sx={{ height: '100%' }}><CardContent sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
                <Stack direction="row" spacing={1} sx={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <Box><Typography variant="overline" color="primary">{company?.name || 'Global standard'}</Typography><Typography variant="h6">{template.name}</Typography></Box>
                  <Chip size="small" color={template.status === 'published' ? 'success' : template.status === 'draft' ? 'warning' : 'default'} label={sentence(template.status)} />
                </Stack>
                <Typography sx={{ color: 'text.secondary', my: 1.5, flex: 1 }}>{template.description}</Typography>
                <Stack direction="row" spacing={0.75} useFlexGap sx={{ flexWrap: 'wrap', mb: 1.5 }}>
                  <Chip size="small" variant="outlined" label={`v${template.version}`} />
                  <Chip size="small" variant="outlined" label={sentence(template.content.changeType)} />
                  <Chip size="small" variant="outlined" label={`${template.content.expectedDurationMinutes} min`} />
                  {template.tags.slice(0, 3).map(tag => <Chip key={tag} size="small" label={tag} />)}
                </Stack>
                <Typography variant="caption" color="text.secondary">
                  {template.system ? 'Managed standard' : 'Custom procedure'}{template.reviewDueDate ? ` · review ${template.reviewDueDate}` : ' · no review date'}
                </Typography>
                <Divider sx={{ my: 1.5 }} />
                <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}>
                  <Button size="small" startIcon={<EditOutlined />} onClick={() => setEditor(editorFromTemplate(template))}>Edit</Button>
                  <Button size="small" startIcon={<ContentCopyOutlined />} onClick={() => setEditor(copyEditor(template))}>Duplicate</Button>
                  {template.status !== 'published' && <Button size="small" color="success" startIcon={<PublishOutlined />} disabled={saving} onClick={() => void setLifecycle(template, 'published')}>Publish</Button>}
                  {template.status !== 'retired' && <Button size="small" color="warning" startIcon={<ArchiveOutlined />} disabled={saving} onClick={() => void setLifecycle(template, 'retired')}>Retire</Button>}
                </Stack>
              </CardContent></Card>
            </Grid>;
          })}
        </Grid>}

        <Dialog open={Boolean(editor)} onClose={() => { if (!saving) setEditor(null); }} maxWidth="lg" fullWidth>
          {editor && <>
            <DialogTitle>{editor.id ? `Edit ${editor.name} · create version ${editor.expectedVersion + 1}` : 'Create change template'}</DialogTitle>
            <DialogContent dividers>
              <Grid container spacing={2}>
                <Grid size={{ xs: 12 }}><Alert severity="info">Use tokens such as <code>{'{{server_name}}'}</code> in procedure fields. Define every custom token as an input below. Names, scope, schedule, owners, calculated impact, risk and approvals are added when the change is logged.</Alert></Grid>
                <Grid size={{ xs: 12, md: 4 }}><FormControl fullWidth disabled={Boolean(editor.id)}><InputLabel>Scope</InputLabel><Select label="Scope" value={editor.companyId} onChange={event => updateEditor('companyId', event.target.value)}>
                  {platformAdmin && <MenuItem value="">Global · all customers</MenuItem>}
                  {companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}
                </Select></FormControl></Grid>
                <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth required disabled={Boolean(editor.id)} label="Stable key" value={editor.key} error={Boolean(editor.key) && !stableKeyPattern.test(editor.key)} onChange={event => updateEditor('key', normalizeStableKey(event.target.value))} helperText={editor.key && !stableKeyPattern.test(editor.key) ? 'Use 2–64 characters, beginning with a letter.' : 'Lowercase ID used by integrations and audit history.'} /></Grid>
                <Grid size={{ xs: 12, md: 4 }}><FormControl fullWidth><InputLabel>Status</InputLabel><Select label="Status" value={editor.status} onChange={event => updateEditor('status', event.target.value as ChangeTemplate['status'])}>
                  <MenuItem value="draft">Draft</MenuItem><MenuItem value="published">Published</MenuItem>{editor.id && <MenuItem value="retired">Retired</MenuItem>}
                </Select></FormControl></Grid>
                <Grid size={{ xs: 12, md: 7 }}><TextField fullWidth required label="Template name" value={editor.name} onChange={event => updateEditor('name', event.target.value)} /></Grid>
                <Grid size={{ xs: 12, md: 5 }}><TextField fullWidth label="Tags" value={editor.tags} onChange={event => updateEditor('tags', event.target.value)} helperText="Comma-separated search and reporting tags." /></Grid>
                <Grid size={{ xs: 12 }}><TextField fullWidth multiline minRows={2} label="Purpose and when to use it" value={editor.description} onChange={event => updateEditor('description', event.target.value)} /></Grid>
                <Grid size={{ xs: 12, md: 6 }}><Autocomplete options={users} value={selectedOwner} getOptionLabel={ownerLabel} isOptionEqualToValue={(option, value) => option.id === value.id} onChange={(_, value) => updateEditor('ownerUserId', value?.id || '')} renderInput={params => <TextField {...params} label="Procedure owner" helperText="Responsible for review and accuracy." />} /></Grid>
                <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth type="date" label="Next review date" value={editor.reviewDueDate} onChange={event => updateEditor('reviewDueDate', event.target.value)} slotProps={{ inputLabel: { shrink: true } }} /></Grid>
                <Grid size={{ xs: 12 }}><Divider><Chip label="Change defaults" /></Divider></Grid>
                <Grid size={{ xs: 12, sm: 6, lg: 3 }}><FormControl fullWidth><InputLabel>Type</InputLabel><Select label="Type" value={editor.content.changeType} onChange={event => updateContent('changeType', event.target.value)}>
                  {['standard', 'normal', 'emergency'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}
                </Select></FormControl></Grid>
                <Grid size={{ xs: 12, sm: 6, lg: 3 }}><FormControl fullWidth><InputLabel>Category</InputLabel><Select label="Category" value={editor.content.category} onChange={event => updateContent('category', event.target.value)}>
                  {['infrastructure', 'network', 'software', 'application', 'database', 'security', 'cloud', 'other'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}
                </Select></FormControl></Grid>
                <Grid size={{ xs: 12, sm: 6, lg: 3 }}><FormControl fullWidth><InputLabel>Priority</InputLabel><Select label="Priority" value={editor.content.priority} onChange={event => updateContent('priority', event.target.value)}>
                  {['low', 'medium', 'high', 'critical'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}
                </Select></FormControl></Grid>
                <Grid size={{ xs: 12, sm: 6, lg: 3 }}><TextField fullWidth type="number" label="Expected duration (minutes)" value={editor.content.expectedDurationMinutes} onChange={event => updateContent('expectedDurationMinutes', Number(event.target.value))} slotProps={{ htmlInput: { min: 1, max: 10080 } }} /></Grid>
                <Grid size={{ xs: 12, md: 6 }}><FormControlLabel control={<Switch checked={editor.content.outageExpected} onChange={event => updateContent('outageExpected', event.target.checked)} />} label="Outage expected by default" /></Grid>
                <Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth><InputLabel>Suggested approver responsibility</InputLabel><Select label="Suggested approver responsibility" value={editor.content.suggestedApproverRole} onChange={event => updateContent('suggestedApproverRole', event.target.value)}>
                  <MenuItem value="">No suggestion</MenuItem>{['change_approver', 'business_owner', 'service_owner', 'technical_owner', 'signoff_delegate', 'custodian'].map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}
                </Select></FormControl></Grid>
                {contentFields.map(field => <Grid key={field.key} size={{ xs: 12, lg: field.rows > 4 ? 12 : 6 }}>
                  <TextField fullWidth required={field.key !== 'communicationPlanTemplate'} multiline={field.rows > 1} minRows={field.rows} label={field.label} value={String(editor.content[field.key] || '')} onChange={event => updateContent(field.key, event.target.value as never)} helperText={`${field.help} Tokens use {{input_key}}.`} />
                </Grid>)}
                <Grid size={{ xs: 12 }}><Divider><Chip label="Post-change validation" /></Divider></Grid>
                <Grid size={{ xs: 12 }}><Stack direction="row" spacing={2} sx={{ justifyContent: 'space-between', alignItems: 'center' }}><Box><Typography variant="h6">Closure tests</Typography><Typography color="text.secondary">Define the objective checks a technician must record before closing a change.</Typography></Box><Button startIcon={<AddOutlined />} onClick={addClosureTest}>Add test</Button></Stack></Grid>
                {!(editor.content.closureTests || []).length && <Grid size={{ xs: 12 }}><Alert severity="info">Without template tests, changes will use the free-form validation plan as one required closure check.</Alert></Grid>}
                {(editor.content.closureTests || []).map((test, index) => <Grid key={`${test.key}-${index}`} size={{ xs: 12 }}>
                  <Paper variant="outlined" sx={{ p: 2 }}><Grid container spacing={1.5}>
                    <Grid size={{ xs: 12, md: 3 }}><TextField fullWidth required label="Stable test key" value={test.key} error={Boolean(test.key) && !stableKeyPattern.test(test.key)} onChange={event => updateClosureTest(index, { key: normalizeStableKey(event.target.value) })} helperText={test.key && !stableKeyPattern.test(test.key) ? 'Use 2–64 characters, beginning with a letter.' : 'Used in immutable closure evidence.'} /></Grid>
                    <Grid size={{ xs: 10, md: 4 }}><TextField fullWidth required label="Test name" value={test.label} onChange={event => updateClosureTest(index, { label: event.target.value })} helperText="Tokens are supported." /></Grid>
                    <Grid size={{ xs: 2, md: 1 }}><IconButton color="error" aria-label={`Remove ${test.label}`} onClick={() => removeClosureTest(index)}><DeleteOutlined /></IconButton></Grid>
                    <Grid size={{ xs: 12, md: 2 }}><FormControlLabel control={<Switch checked={test.required} onChange={event => updateClosureTest(index, { required: event.target.checked })} />} label="Required" /></Grid>
                    <Grid size={{ xs: 12, md: 2 }}><FormControlLabel control={<Switch checked={test.evidenceRequired} onChange={event => updateClosureTest(index, { evidenceRequired: event.target.checked })} />} label="Evidence required" /></Grid>
                    <Grid size={{ xs: 12 }}><TextField fullWidth required multiline minRows={2} label="Expected result" value={test.expectedResultTemplate} onChange={event => updateClosureTest(index, { expectedResultTemplate: event.target.value })} helperText="State the observable success condition. Tokens use {{input_key}}." /></Grid>
                  </Grid></Paper>
                </Grid>)}
                <Grid size={{ xs: 12 }}><Divider><Chip label="Technician inputs" /></Divider></Grid>
                <Grid size={{ xs: 12 }}><Stack direction="row" spacing={2} sx={{ justifyContent: 'space-between', alignItems: 'center' }}><Box><Typography variant="h6">Parameters</Typography><Typography color="text.secondary">Prompt only for values the CMDB cannot determine.</Typography></Box><Button startIcon={<AddOutlined />} onClick={addParameter}>Add input</Button></Stack></Grid>
                {!editor.content.parameters.length && <Grid size={{ xs: 12 }}><Alert severity="info">This template has no custom inputs. You can still use built-in <code>{'{{company_name}}'}</code>, <code>{'{{asset_name}}'}</code> and <code>{'{{business_system_name}}'}</code> tokens.</Alert></Grid>}
                {editor.content.parameters.map((parameter, index) => <Grid key={`${parameter.key}-${index}`} size={{ xs: 12 }}>
                  <Paper variant="outlined" sx={{ p: 2 }}><Grid container spacing={1.5}>
                    <Grid size={{ xs: 12, md: 3 }}><TextField fullWidth required label="Input key" value={parameter.key} error={Boolean(parameter.key) && !stableKeyPattern.test(parameter.key)} onChange={event => updateParameter(index, { key: normalizeStableKey(event.target.value) })} helperText={parameter.key && !stableKeyPattern.test(parameter.key) ? 'Use 2–64 characters, beginning with a letter.' : 'Use this key in template tokens.'} /></Grid>
                    <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Technician prompt" value={parameter.label} onChange={event => updateParameter(index, { label: event.target.value })} /></Grid>
                    <Grid size={{ xs: 10, md: 2 }}><FormControl fullWidth><InputLabel>Type</InputLabel><Select label="Type" value={parameter.type} onChange={event => updateParameter(index, { type: event.target.value as ChangeTemplateParameter['type'] })}>
                      {parameterTypes.map(value => <MenuItem key={value} value={value}>{sentence(value)}</MenuItem>)}
                    </Select></FormControl></Grid>
                    <Grid size={{ xs: 2, md: 1 }}><IconButton color="error" aria-label={`Remove ${parameter.label}`} onClick={() => removeParameter(index)}><DeleteOutlined /></IconButton></Grid>
                    <Grid size={{ xs: 12, md: 2 }}><FormControlLabel control={<Switch checked={parameter.required} onChange={event => updateParameter(index, { required: event.target.checked })} />} label="Required" /></Grid>
                    <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Help text" value={parameter.helpText} onChange={event => updateParameter(index, { helpText: event.target.value })} /></Grid>
                    <Grid size={{ xs: 12, md: 4 }}><FormControl fullWidth><InputLabel>Auto-fill source</InputLabel><Select label="Auto-fill source" value={parameter.source} onChange={event => updateParameter(index, { source: event.target.value })}><MenuItem value="">None</MenuItem><MenuItem value="scope_primary_name">First selected CI</MenuItem></Select></FormControl></Grid>
                    <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth disabled={parameter.type !== 'select'} label="Select options" value={parameter.options.join(', ')} onChange={event => updateParameter(index, { options: event.target.value.split(',').map(value => value.trim()).filter(Boolean) })} helperText="Comma-separated." /></Grid>
                  </Grid></Paper>
                </Grid>)}
              </Grid>
            </DialogContent>
            <DialogActions sx={{ justifyContent: 'space-between' }}>
              <Typography variant="caption" color="text.secondary">{selectedCompany?.name || 'Global scope'} · {editor.id ? `based on version ${editor.expectedVersion}` : 'new procedure'}</Typography>
              <Stack direction="row" spacing={1}><Button onClick={() => setEditor(null)} disabled={saving}>Cancel</Button><Button variant="contained" disabled={saving || !editor.name.trim() || !editorKeysAreValid(editor) || (!platformAdmin && !editor.companyId)} onClick={() => void save()}>{saving ? 'Saving…' : editor.id ? 'Save new version' : 'Create template'}</Button></Stack>
            </DialogActions>
          </>}
        </Dialog>
      </Box>
    </RootGuard>
  );
}
