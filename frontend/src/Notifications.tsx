import AddOutlined from '@mui/icons-material/AddOutlined';
import AutorenewOutlined from '@mui/icons-material/AutorenewOutlined';
import SendOutlined from '@mui/icons-material/SendOutlined';
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Chip, Dialog, DialogActions,
  DialogContent, DialogTitle, FormControl, FormControlLabel, Grid, InputLabel, MenuItem,
  Select, Stack, Switch, Tab, Table, TableBody, TableCell, TableContainer, TableHead,
  TableRow, Tabs, TextField, Typography,
} from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { Title } from 'react-admin';
import { Navigate } from 'react-router-dom';
import { apiFetch, getSession } from './session';
import { useWorkspace } from './workspace';

type Notice = { severity: 'success' | 'error' | 'info' | 'warning'; message: string } | null;
type NotificationStatus = {
  workerEnabled: boolean;
  workerIntervalSeconds: number;
  queued: number;
  failed: number;
  deadLetter: number;
  missingRecipients: number;
  email: { enabled: boolean; status: string; senderAddress: string };
};
type NotificationRule = {
  id: string;
  companyId?: string | null;
  key: string;
  name: string;
  eventType: string;
  enabled: boolean;
  leadDays: number;
  cadence: string;
  recipientRoles: string[];
  fallbackAddresses: string[];
  templateKey: string;
  maxAttempts: number;
  lastRunAt?: string | null;
  revision: number;
};
type NotificationTemplate = {
  id: string;
  companyId?: string | null;
  key: string;
  name: string;
  subjectTemplate: string;
  htmlTemplate: string;
  textTemplate: string;
  enabled: boolean;
  version: number;
};
type NotificationEvent = {
  id: string;
  companyId: string;
  ruleName?: string;
  eventType: string;
  entityName: string;
  status: string;
  recipients: string[];
  missingRoles: string[];
  createdAt: string;
};
type Preference = {
  id: string;
  companyId: string;
  contactId?: string | null;
  userId?: string | null;
  emailEnabled: boolean;
  eventTypes: string[];
  digestMode: string;
};
type Contact = { id: string; companyId: string; displayName: string; primaryEmail?: string; status: string };
type Company = { id: string; name: string };

const roleOptions = [
  'business_owner', 'service_owner', 'technical_owner', 'custodian',
  'change_approver', 'signoff_delegate', 'support_contact',
];
const eventOptions = ['*', 'asset_renewal', 'asset_eol', 'change_approval', 'missing_owner'];
const displayLabel = (value: string) => value.replaceAll('_', ' ');
const displayDate = (value?: string | null) => value ? new Date(value).toLocaleString() : 'Never';

function RootAdminGuard({ children }: { children: React.ReactNode }) {
  const workspace = useWorkspace();
  if (!workspace.isRoot) return <Navigate to="/" replace />;
  if (getSession()?.user.role !== 'platform_admin') {
    return <Alert severity="error">This page requires platform administrator access.</Alert>;
  }
  return <>{children}</>;
}

function SummaryCard({ label, value, tone = 'text.primary' }: { label: string; value: string | number; tone?: string }) {
  return <Card variant="outlined"><CardContent><Typography variant="overline" color="text.secondary">{label}</Typography><Typography variant="h4" sx={{ color: tone, fontWeight: 800 }}>{value}</Typography></CardContent></Card>;
}

export function NotificationsPage() {
  const [status, setStatus] = useState<NotificationStatus | null>(null);
  const [rules, setRules] = useState<NotificationRule[]>([]);
  const [templates, setTemplates] = useState<NotificationTemplate[]>([]);
  const [events, setEvents] = useState<NotificationEvent[]>([]);
  const [preferences, setPreferences] = useState<Preference[]>([]);
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [tab, setTab] = useState(0);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState('');
  const [editingRule, setEditingRule] = useState<NotificationRule | null>(null);
  const [editingTemplate, setEditingTemplate] = useState<NotificationTemplate | null>(null);
  const [preferenceOpen, setPreferenceOpen] = useState(false);
  const [preferenceDraft, setPreferenceDraft] = useState({ companyId: '', contactId: '', emailEnabled: true, eventTypes: ['*'], digestMode: 'instant' });

  const load = async () => {
    try {
      const [health, loadedRules, loadedTemplates, loadedEvents, loadedPreferences, loadedContacts, loadedCompanies] = await Promise.all([
        apiFetch<NotificationStatus>('/api/notifications/status'),
        apiFetch<NotificationRule[]>('/api/notifications/rules'),
        apiFetch<NotificationTemplate[]>('/api/notifications/templates'),
        apiFetch<NotificationEvent[]>('/api/notifications/events?limit=200'),
        apiFetch<Preference[]>('/api/notifications/preferences'),
        apiFetch<Contact[]>('/api/contacts'),
        apiFetch<Company[]>('/api/companies'),
      ]);
      setStatus(health); setRules(loadedRules); setTemplates(loadedTemplates); setEvents(loadedEvents);
      setPreferences(loadedPreferences); setContacts(loadedContacts); setCompanies(loadedCompanies);
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Notifications could not be loaded.' });
    }
  };
  useEffect(() => { void load(); }, []);

  const contactById = useMemo(() => new Map(contacts.map(contact => [contact.id, contact])), [contacts]);
  const companyById = useMemo(() => new Map(companies.map(company => [company.id, company.name])), [companies]);
  const scopedContacts = contacts.filter(contact => contact.companyId === preferenceDraft.companyId && contact.status === 'active');

  const evaluate = async () => {
    setBusy('evaluate'); setNotice(null);
    try {
      const result = await apiFetch<{ queued: number; missingRecipients: number; duplicates: number }>('/api/notifications/run', { method: 'POST' });
      setNotice({ severity: 'success', message: `Evaluation completed: ${result.queued} queued, ${result.missingRecipients} missing recipients, ${result.duplicates} already recorded.` });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Rules could not be evaluated.' });
    } finally { setBusy(''); }
  };

  const processQueue = async () => {
    if (!window.confirm('Send all due queued email now? This can contact real customer recipients.')) return;
    setBusy('process'); setNotice(null);
    try {
      const result = await apiFetch<{ processed: number; accepted: number; failed: number; deadLetter: number }>('/api/notifications/process?limit=50', { method: 'POST' });
      setNotice({ severity: result.failed || result.deadLetter ? 'warning' : 'success', message: `Delivery completed: ${result.accepted} accepted, ${result.failed} retrying, ${result.deadLetter} moved to dead letter.` });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The delivery queue could not be processed.' });
    } finally { setBusy(''); }
  };

  const saveRule = async () => {
    if (!editingRule) return;
    setBusy('rule');
    try {
      await apiFetch(`/api/notifications/rules/${editingRule.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ ...editingRule, expectedRevision: editingRule.revision }),
      });
      setEditingRule(null); setNotice({ severity: 'success', message: 'Notification rule updated.' }); await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Rule could not be saved.' });
    } finally { setBusy(''); }
  };

  const saveTemplate = async () => {
    if (!editingTemplate) return;
    setBusy('template');
    try {
      await apiFetch(`/api/notifications/templates/${editingTemplate.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ ...editingTemplate, expectedVersion: editingTemplate.version }),
      });
      setEditingTemplate(null); setNotice({ severity: 'success', message: 'Notification template version created.' }); await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Template could not be saved.' });
    } finally { setBusy(''); }
  };

  const savePreference = async () => {
    if (!preferenceDraft.companyId || !preferenceDraft.contactId) {
      setNotice({ severity: 'warning', message: 'Choose a customer and contact.' }); return;
    }
    setBusy('preference');
    try {
      await apiFetch('/api/notifications/preferences', { method: 'PUT', body: JSON.stringify(preferenceDraft) });
      setPreferenceOpen(false); setNotice({ severity: 'success', message: 'Recipient preference saved.' }); await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Preference could not be saved.' });
    } finally { setBusy(''); }
  };

  return <RootAdminGuard><Title title="Notification center" /><Box sx={{ p: { xs: 2, md: 3 }, maxWidth: 1500, mx: 'auto' }}>
    <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', mb: 3 }}>
      <Box><Typography variant="overline" color="primary">Operations</Typography><Typography variant="h3">Notification center</Typography><Typography color="text.secondary" sx={{ mt: 0.75, maxWidth: 850 }}>Turn CMDB lifecycle, ownership and change events into traceable Microsoft 365 messages. Evaluation only queues messages; delivery is a separate controlled action.</Typography></Box>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <Button variant="outlined" startIcon={<AutorenewOutlined />} disabled={Boolean(busy)} onClick={() => void evaluate()}>Evaluate rules</Button>
        <Button variant="contained" startIcon={<SendOutlined />} disabled={Boolean(busy) || !status?.email.enabled} onClick={() => void processQueue()}>Send due email</Button>
      </Stack>
    </Stack>
    {notice && <Alert severity={notice.severity} onClose={() => setNotice(null)} sx={{ mb: 2 }}>{notice.message}</Alert>}
    {status && !status.email.enabled && <Alert severity="warning" sx={{ mb: 2 }}>Microsoft 365 email delivery is disabled. Rules can be evaluated safely, but queued messages will not be sent.</Alert>}
    <Grid container spacing={2} sx={{ mb: 3 }}>
      <Grid size={{ xs: 6, md: 2.4 }}><SummaryCard label="Worker" value={status?.workerEnabled ? 'Running' : 'Manual'} tone={status?.workerEnabled ? 'success.main' : 'warning.main'} /></Grid>
      <Grid size={{ xs: 6, md: 2.4 }}><SummaryCard label="Queued" value={status?.queued ?? '—'} /></Grid>
      <Grid size={{ xs: 6, md: 2.4 }}><SummaryCard label="Retrying" value={status?.failed ?? '—'} tone="warning.main" /></Grid>
      <Grid size={{ xs: 6, md: 2.4 }}><SummaryCard label="Dead letter" value={status?.deadLetter ?? '—'} tone="error.main" /></Grid>
      <Grid size={{ xs: 12, md: 2.4 }}><SummaryCard label="Missing recipient" value={status?.missingRecipients ?? '—'} tone="warning.main" /></Grid>
    </Grid>

    <Tabs value={tab} onChange={(_event, value) => setTab(value)} sx={{ mb: 2 }}>
      <Tab label="Rules" /><Tab label="Templates" /><Tab label="Evidence" /><Tab label="Recipient preferences" />
    </Tabs>
    {tab === 0 && <TableContainer component={Card}><Table><TableHead><TableRow><TableCell>Rule</TableCell><TableCell>Event</TableCell><TableCell>Timing</TableCell><TableCell>Recipients</TableCell><TableCell>Last evaluated</TableCell><TableCell /></TableRow></TableHead><TableBody>{rules.map(rule => <TableRow key={rule.id} hover><TableCell><Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}><Chip size="small" color={rule.enabled ? 'success' : 'default'} label={rule.enabled ? 'On' : 'Off'} /><Box><Typography sx={{ fontWeight: 700 }}>{rule.name}</Typography><Typography variant="caption" color="text.secondary">{rule.companyId ? companyById.get(rule.companyId) || rule.companyId : 'All customers'}</Typography></Box></Stack></TableCell><TableCell>{displayLabel(rule.eventType)}</TableCell><TableCell>{rule.cadence} · {rule.leadDays} days</TableCell><TableCell><Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap', gap: 0.5 }}>{rule.recipientRoles.map(role => <Chip key={role} size="small" variant="outlined" label={displayLabel(role)} />)}</Stack></TableCell><TableCell>{displayDate(rule.lastRunAt)}</TableCell><TableCell><Button size="small" onClick={() => setEditingRule({ ...rule })}>Edit</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer>}
    {tab === 1 && <Grid container spacing={2}>{templates.map(template => <Grid key={template.id} size={{ xs: 12, md: 6 }}><Card variant="outlined"><CardContent><Stack direction="row" sx={{ justifyContent: 'space-between' }}><Box><Typography variant="h6">{template.name}</Typography><Typography variant="caption" color="text.secondary">{template.companyId ? companyById.get(template.companyId) : 'Global'} · version {template.version}</Typography></Box><Chip size="small" label={template.enabled ? 'Enabled' : 'Disabled'} color={template.enabled ? 'success' : 'default'} /></Stack><Typography sx={{ mt: 2, fontWeight: 700 }}>{template.subjectTemplate}</Typography><Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{template.textTemplate}</Typography><Button size="small" sx={{ mt: 2 }} onClick={() => setEditingTemplate({ ...template })}>Edit template</Button></CardContent></Card></Grid>)}</Grid>}
    {tab === 2 && <TableContainer component={Card}><Table><TableHead><TableRow><TableCell>Created</TableCell><TableCell>Customer</TableCell><TableCell>Event</TableCell><TableCell>Configuration item / change</TableCell><TableCell>Status</TableCell><TableCell>Recipients / gap</TableCell></TableRow></TableHead><TableBody>{events.map(event => <TableRow key={event.id} hover><TableCell>{displayDate(event.createdAt)}</TableCell><TableCell>{companyById.get(event.companyId) || event.companyId}</TableCell><TableCell>{displayLabel(event.eventType)}</TableCell><TableCell>{event.entityName}</TableCell><TableCell><Chip size="small" label={displayLabel(event.status)} color={event.status === 'accepted' ? 'success' : event.status === 'dead_letter' ? 'error' : event.status === 'missing_recipient' ? 'warning' : 'default'} /></TableCell><TableCell>{event.recipients.length ? event.recipients.join(', ') : event.missingRoles.map(displayLabel).join(', ') || 'No recipient'}</TableCell></TableRow>)}</TableBody></Table></TableContainer>}
    {tab === 3 && <><Stack direction="row" sx={{ justifyContent: 'space-between', mb: 2 }}><Box><Typography variant="h6">Recipient preferences</Typography><Typography variant="body2" color="text.secondary">Opt contacts into selected event types or suppress email without changing CMDB ownership.</Typography></Box><Button startIcon={<AddOutlined />} onClick={() => setPreferenceOpen(true)}>Add preference</Button></Stack><TableContainer component={Card}><Table><TableHead><TableRow><TableCell>Customer</TableCell><TableCell>Contact</TableCell><TableCell>Email</TableCell><TableCell>Events</TableCell><TableCell>Delivery</TableCell></TableRow></TableHead><TableBody>{preferences.map(preference => { const contact = preference.contactId ? contactById.get(preference.contactId) : undefined; return <TableRow key={preference.id}><TableCell>{companyById.get(preference.companyId) || preference.companyId}</TableCell><TableCell>{contact?.displayName || preference.userId || 'Unknown'}</TableCell><TableCell><Chip size="small" color={preference.emailEnabled ? 'success' : 'default'} label={preference.emailEnabled ? 'Enabled' : 'Suppressed'} /></TableCell><TableCell>{preference.eventTypes.map(displayLabel).join(', ')}</TableCell><TableCell>{preference.digestMode}</TableCell></TableRow>; })}{!preferences.length && <TableRow><TableCell colSpan={5}><Alert severity="info">No overrides exist. Active owners receive notifications according to each rule.</Alert></TableCell></TableRow>}</TableBody></Table></TableContainer></>}

    <Dialog open={Boolean(editingRule)} onClose={() => setEditingRule(null)} fullWidth maxWidth="md"><DialogTitle>Edit notification rule</DialogTitle>{editingRule && <DialogContent><Stack spacing={2} sx={{ pt: 1 }}><TextField label="Rule name" value={editingRule.name} onChange={event => setEditingRule({ ...editingRule, name: event.target.value })} /><FormControlLabel control={<Switch checked={editingRule.enabled} onChange={event => setEditingRule({ ...editingRule, enabled: event.target.checked })} />} label="Rule enabled" /><Grid container spacing={2}><Grid size={{ xs: 12, sm: 4 }}><TextField fullWidth type="number" label="Lead days" value={editingRule.leadDays} onChange={event => setEditingRule({ ...editingRule, leadDays: Number(event.target.value) })} /></Grid><Grid size={{ xs: 12, sm: 4 }}><FormControl fullWidth><InputLabel>Cadence</InputLabel><Select label="Cadence" value={editingRule.cadence} onChange={event => setEditingRule({ ...editingRule, cadence: event.target.value })}><MenuItem value="immediate">Immediate</MenuItem><MenuItem value="daily">Daily</MenuItem><MenuItem value="weekly">Weekly</MenuItem></Select></FormControl></Grid><Grid size={{ xs: 12, sm: 4 }}><TextField fullWidth type="number" label="Maximum attempts" value={editingRule.maxAttempts} onChange={event => setEditingRule({ ...editingRule, maxAttempts: Number(event.target.value) })} /></Grid></Grid><Autocomplete multiple options={roleOptions} value={editingRule.recipientRoles} onChange={(_event, value) => setEditingRule({ ...editingRule, recipientRoles: value })} renderInput={params => <TextField {...params} label="Recipient responsibility roles" />} /><TextField label="Fallback email addresses" helperText="Comma-separated; used when CMDB ownership is incomplete." value={editingRule.fallbackAddresses.join(', ')} onChange={event => setEditingRule({ ...editingRule, fallbackAddresses: event.target.value.split(',').map(value => value.trim()).filter(Boolean) })} /><FormControl fullWidth><InputLabel>Template</InputLabel><Select label="Template" value={editingRule.templateKey} onChange={event => setEditingRule({ ...editingRule, templateKey: event.target.value })}>{templates.map(template => <MenuItem key={template.id} value={template.key}>{template.name}</MenuItem>)}</Select></FormControl></Stack></DialogContent>}<DialogActions><Button onClick={() => setEditingRule(null)}>Cancel</Button><Button variant="contained" disabled={busy === 'rule'} onClick={() => void saveRule()}>Save rule</Button></DialogActions></Dialog>
    <Dialog open={Boolean(editingTemplate)} onClose={() => setEditingTemplate(null)} fullWidth maxWidth="md"><DialogTitle>Edit versioned template</DialogTitle>{editingTemplate && <DialogContent><Stack spacing={2} sx={{ pt: 1 }}><Alert severity="info">Variables use mustache syntax, for example <code>{'{{asset_name}}'}</code>. Values are escaped before rendering.</Alert><TextField label="Template name" value={editingTemplate.name} onChange={event => setEditingTemplate({ ...editingTemplate, name: event.target.value })} /><FormControlLabel control={<Switch checked={editingTemplate.enabled} onChange={event => setEditingTemplate({ ...editingTemplate, enabled: event.target.checked })} />} label="Template enabled" /><TextField label="Subject" value={editingTemplate.subjectTemplate} onChange={event => setEditingTemplate({ ...editingTemplate, subjectTemplate: event.target.value })} /><TextField label="HTML body" multiline minRows={7} value={editingTemplate.htmlTemplate} onChange={event => setEditingTemplate({ ...editingTemplate, htmlTemplate: event.target.value })} /><TextField label="Plain-text body" multiline minRows={4} value={editingTemplate.textTemplate} onChange={event => setEditingTemplate({ ...editingTemplate, textTemplate: event.target.value })} /></Stack></DialogContent>}<DialogActions><Button onClick={() => setEditingTemplate(null)}>Cancel</Button><Button variant="contained" disabled={busy === 'template'} onClick={() => void saveTemplate()}>Save new version</Button></DialogActions></Dialog>
    <Dialog open={preferenceOpen} onClose={() => setPreferenceOpen(false)} fullWidth maxWidth="sm"><DialogTitle>Add recipient preference</DialogTitle><DialogContent><Stack spacing={2} sx={{ pt: 1 }}><FormControl fullWidth><InputLabel>Customer</InputLabel><Select label="Customer" value={preferenceDraft.companyId} onChange={event => setPreferenceDraft({ ...preferenceDraft, companyId: event.target.value, contactId: '' })}>{companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}</Select></FormControl><Autocomplete options={scopedContacts} getOptionLabel={option => `${option.displayName}${option.primaryEmail ? ` · ${option.primaryEmail}` : ''}`} value={scopedContacts.find(item => item.id === preferenceDraft.contactId) || null} onChange={(_event, value) => setPreferenceDraft({ ...preferenceDraft, contactId: value?.id || '' })} renderInput={params => <TextField {...params} label="Contact" />} /><FormControlLabel control={<Switch checked={preferenceDraft.emailEnabled} onChange={event => setPreferenceDraft({ ...preferenceDraft, emailEnabled: event.target.checked })} />} label="Email enabled" /><Autocomplete multiple options={eventOptions} value={preferenceDraft.eventTypes} onChange={(_event, value) => setPreferenceDraft({ ...preferenceDraft, eventTypes: value.length ? value : ['*'] })} renderInput={params => <TextField {...params} label="Event types" />} /><FormControl fullWidth><InputLabel>Delivery</InputLabel><Select label="Delivery" value={preferenceDraft.digestMode} onChange={event => setPreferenceDraft({ ...preferenceDraft, digestMode: event.target.value })}><MenuItem value="instant">Instant</MenuItem><MenuItem value="daily">Daily digest (reserved)</MenuItem><MenuItem value="weekly">Weekly digest (reserved)</MenuItem></Select></FormControl></Stack></DialogContent><DialogActions><Button onClick={() => setPreferenceOpen(false)}>Cancel</Button><Button variant="contained" disabled={busy === 'preference'} onClick={() => void savePreference()}>Save preference</Button></DialogActions></Dialog>
  </Box></RootAdminGuard>;
}
