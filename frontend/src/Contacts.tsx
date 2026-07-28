import AddOutlined from '@mui/icons-material/AddOutlined';
import BadgeOutlined from '@mui/icons-material/BadgeOutlined';
import EditOutlined from '@mui/icons-material/EditOutlined';
import KeyOutlined from '@mui/icons-material/KeyOutlined';
import OpenInNewOutlined from '@mui/icons-material/OpenInNewOutlined';
import PersonOffOutlined from '@mui/icons-material/PersonOffOutlined';
import SearchOutlined from '@mui/icons-material/SearchOutlined';
import SyncOutlined from '@mui/icons-material/SyncOutlined';
import SwapHorizOutlined from '@mui/icons-material/SwapHorizOutlined';
import {
  Alert, Avatar, Box, Button, Card, CardContent, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, FormControl, Grid, InputAdornment, InputLabel, MenuItem, Paper, Select,
  Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';
import { AuditTimeline } from './Governance';
import { Title } from './ui';
import { apiFetch, getSession } from './session';
import type { Contact, ContactResponsibility } from './types';
import { useWorkspace } from './workspace';

type ContactDetail = Contact & { responsibilities: ContactResponsibility[] };
type Notice = { severity: 'success' | 'error'; message: string } | null;
type ContactDraft = Pick<Contact, 'companyId' | 'displayName' | 'firstName' | 'lastName' | 'email' | 'phone' | 'mobile' | 'jobTitle' | 'department' | 'location' | 'timezone' | 'status'> & { id?: string; managerContactId?: string | null; reason: string };

const statuses: Contact['status'][] = ['active', 'on_leave', 'left_company', 'inactive'];
const roleLabel = (value: string) => value.replaceAll('_', ' ').replace(/\b\w/g, match => match.toUpperCase());
const initials = (value: string) => value.split(/\s+/).slice(0, 2).map(part => part[0]).join('').toUpperCase();
const dateTime = (value?: string | null) => value ? new Date(value).toLocaleString() : 'Not recorded';

function blankDraft(companyId: string): ContactDraft {
  return { companyId, displayName: '', firstName: '', lastName: '', email: '', phone: '', mobile: '', jobTitle: '', department: '', location: '', timezone: 'Africa/Johannesburg', status: 'active', managerContactId: null, reason: '' };
}

function statusColor(status: Contact['status']): 'success' | 'warning' | 'error' | 'default' {
  if (status === 'active') return 'success';
  if (status === 'on_leave') return 'warning';
  if (status === 'left_company') return 'error';
  return 'default';
}

export function ContactsPage() {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const canManage = ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('');
  const [source, setSource] = useState('');
  const [customer, setCustomer] = useState('');
  const [draft, setDraft] = useState<ContactDraft | null>(null);
  const [selected, setSelected] = useState<ContactDetail | null>(null);
  const [portalContact, setPortalContact] = useState<Contact | null>(null);
  const [temporaryPassword, setTemporaryPassword] = useState('');
  const [reassignContact, setReassignContact] = useState<ContactDetail | null>(null);
  const [replacementContactId, setReplacementContactId] = useState('');
  const [reassignmentReason, setReassignmentReason] = useState('');

  const companyNames = useMemo(() => new Map(workspace.companies.map(item => [item.id, item.name])), [workspace.companies]);
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
      setContacts(await apiFetch<Contact[]>(`/api/contacts${suffix}`));
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Contacts could not be loaded.' });
    } finally { setLoading(false); }
  }, [workspace.companyId, workspace.isRoot]);
  useEffect(() => { void load(); }, [load]);

  const visible = useMemo(() => contacts.filter(contact => {
    const search = query.trim().toLowerCase();
    return (!search || [contact.displayName, contact.email, contact.jobTitle, contact.department, contact.phone, contact.mobile].some(value => String(value || '').toLowerCase().includes(search)))
      && (!status || contact.status === status)
      && (!source || contact.source === source)
      && (!customer || contact.companyId === customer);
  }), [contacts, query, status, source, customer]);
  const sources = useMemo(() => [...new Set(contacts.map(item => item.source))].sort(), [contacts]);
  const summary = useMemo(() => ({
    active: contacts.filter(item => item.status === 'active').length,
    unlinked: contacts.filter(item => !item.portalUser).length,
    owners: contacts.filter(item => item.responsibilityCount > 0).length,
    attention: contacts.filter(item => ['left_company', 'inactive'].includes(item.status) && item.responsibilityCount > 0).length,
  }), [contacts]);

  const openCreate = () => setDraft(blankDraft(workspace.isRoot ? workspace.companies[0]?.id || '' : workspace.companyId));
  const openEdit = (contact: Contact) => setDraft({
    id: contact.id, companyId: contact.companyId, displayName: contact.displayName,
    firstName: contact.firstName, lastName: contact.lastName, email: contact.email,
    phone: contact.phone, mobile: contact.mobile, jobTitle: contact.jobTitle,
    department: contact.department, location: contact.location, timezone: contact.timezone,
    status: contact.status, managerContactId: contact.managerContactId, reason: '',
  });
  const openDetail = async (contact: Contact) => {
    try { setSelected(await apiFetch<ContactDetail>(`/api/contacts/${contact.id}`)); }
    catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Contact details could not be loaded.' }); }
  };

  async function saveContact(event: FormEvent) {
    event.preventDefault();
    if (!draft) return;
    setSaving(true); setNotice(null);
    try {
      await apiFetch<Contact>(draft.id ? `/api/contacts/${draft.id}` : '/api/contacts', {
        method: draft.id ? 'PATCH' : 'POST', body: JSON.stringify(draft),
      });
      setNotice({ severity: 'success', message: draft.id ? `${draft.displayName} was updated.` : `${draft.displayName} was added to the directory.` });
      setDraft(null); await load();
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The contact could not be saved.' }); }
    finally { setSaving(false); }
  }

  async function createPortalUser(event: FormEvent) {
    event.preventDefault();
    if (!portalContact) return;
    setSaving(true); setNotice(null);
    try {
      await apiFetch(`/api/contacts/${portalContact.id}/portal-user`, { method: 'POST', body: JSON.stringify({ password: temporaryPassword }) });
      setNotice({ severity: 'success', message: `Portal access was created for ${portalContact.displayName}.` });
      setPortalContact(null); setTemporaryPassword(''); await load();
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Portal access could not be created.' }); }
    finally { setSaving(false); }
  }

  async function reassignResponsibilities(event: FormEvent) {
    event.preventDefault();
    if (!reassignContact) return;
    setSaving(true); setNotice(null);
    try {
      const result = await apiFetch<{ transferred: number; assets: number }>(`/api/contacts/${reassignContact.id}/reassign`, {
        method: 'POST', body: JSON.stringify({ replacementContactId, reason: reassignmentReason }),
      });
      setNotice({ severity: 'success', message: `${result.transferred} responsibility assignment(s) across ${result.assets} CI(s) were transferred.` });
      setReassignContact(null); setReplacementContactId(''); setReassignmentReason(''); await load();
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Responsibilities could not be reassigned.' }); }
    finally { setSaving(false); }
  }

  return <Box className="contacts-page">
    <Title title={workspace.isRoot ? 'MSP contacts' : `${workspace.companyName} contacts`} />
    <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'flex-end' }, mb: 3 }}>
      <Box><Typography variant="overline" color="primary">People and accountability</Typography><Typography variant="h3">Contact directory</Typography><Typography sx={{ color: 'text.secondary', maxWidth: 820 }}>Maintain customer contacts once, then assign them as accountable owners, approvers and support contacts across the CMDB.</Typography></Box>
      {canManage && <Button variant="contained" startIcon={<AddOutlined />} onClick={openCreate}>Add contact</Button>}
    </Stack>
    {notice && <Alert severity={notice.severity} onClose={() => setNotice(null)} sx={{ mb: 2 }}>{notice.message}</Alert>}
    <Grid container spacing={1.5} sx={{ mb: 2 }}>
      {[
        ['Contacts', contacts.length, '#7997ff'], ['Active', summary.active, '#50d5b9'],
        ['With responsibilities', summary.owners, '#a68cf0'], ['Offboarded owners', summary.attention, '#ff7961'],
      ].map(([label, value, color]) => <Grid key={String(label)} size={{ xs: 6, md: 3 }}><Paper sx={{ p: 2, borderTop: `3px solid ${color}` }}><Typography variant="caption" color="text.secondary">{label}</Typography><Typography variant="h5">{value}</Typography></Paper></Grid>)}
    </Grid>
    <Paper variant="outlined" sx={{ p: 1.5, mb: 2 }}><Stack direction={{ xs: 'column', lg: 'row' }} spacing={1.5}>
      <TextField size="small" label="Search people" value={query} onChange={event => setQuery(event.target.value)} sx={{ minWidth: 260, flex: 1 }} slotProps={{ input: { startAdornment: <InputAdornment position="start"><SearchOutlined fontSize="small" /></InputAdornment> } }} />
      {workspace.isRoot && <FormControl size="small" sx={{ minWidth: 220 }}><InputLabel>Customer</InputLabel><Select label="Customer" value={customer} onChange={event => setCustomer(event.target.value)}><MenuItem value="">All customers</MenuItem>{workspace.companies.map(item => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</Select></FormControl>}
      <FormControl size="small" sx={{ minWidth: 170 }}><InputLabel>Status</InputLabel><Select label="Status" value={status} onChange={event => setStatus(event.target.value)}><MenuItem value="">All statuses</MenuItem>{statuses.map(value => <MenuItem key={value} value={value}>{roleLabel(value)}</MenuItem>)}</Select></FormControl>
      <FormControl size="small" sx={{ minWidth: 170 }}><InputLabel>Source</InputLabel><Select label="Source" value={source} onChange={event => setSource(event.target.value)}><MenuItem value="">All sources</MenuItem>{sources.map(value => <MenuItem key={value} value={value}>{roleLabel(value)}</MenuItem>)}</Select></FormControl>
      <Button color="inherit" onClick={() => { setQuery(''); setStatus(''); setSource(''); setCustomer(''); }}>Clear</Button>
    </Stack></Paper>
    {loading ? <Stack spacing={2} sx={{ py: 8, alignItems: 'center' }}><CircularProgress /><Typography color="text.secondary">Loading contacts…</Typography></Stack> : !visible.length ? <Alert severity="info">No contacts match these filters.</Alert> : <TableContainer component={Paper} variant="outlined"><Table size="small">
      <TableHead><TableRow><TableCell>Contact</TableCell>{workspace.isRoot && <TableCell>Customer</TableCell>}<TableCell>Department / role</TableCell><TableCell>Ownership</TableCell><TableCell>Source</TableCell><TableCell>Portal access</TableCell><TableCell align="right">Actions</TableCell></TableRow></TableHead>
      <TableBody>{visible.map(contact => <TableRow key={contact.id} hover>
        <TableCell><Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}><Avatar sx={{ bgcolor: 'primary.main', width: 34, height: 34, fontSize: 13 }}>{initials(contact.displayName)}</Avatar><Box><Typography sx={{ fontWeight: 800 }}>{contact.displayName}</Typography><Typography variant="caption" color="text.secondary">{contact.email || contact.mobile || 'No contact details'}</Typography></Box></Stack></TableCell>
        {workspace.isRoot && <TableCell>{companyNames.get(contact.companyId) || contact.companyId}</TableCell>}
        <TableCell><Typography variant="body2">{contact.jobTitle || 'Job title not recorded'}</Typography><Typography variant="caption" color="text.secondary">{contact.department || 'Department not recorded'}</Typography></TableCell>
        <TableCell><Chip size="small" label={`${contact.responsibilityCount} assignment${contact.responsibilityCount === 1 ? '' : 's'}`} color={contact.responsibilityCount ? 'primary' : 'default'} variant="outlined" /></TableCell>
        <TableCell><Chip size="small" icon={contact.source === 'manual' ? <BadgeOutlined /> : <SyncOutlined />} label={roleLabel(contact.source)} variant="outlined" /><Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>{roleLabel(contact.syncStatus)}</Typography></TableCell>
        <TableCell><Chip size="small" color={contact.portalUser?.status === 'active' ? 'success' : contact.portalUser ? 'warning' : 'default'} label={contact.portalUser ? roleLabel(contact.portalUser.status) : 'No login'} /></TableCell>
        <TableCell align="right"><Button size="small" endIcon={<OpenInNewOutlined />} onClick={() => void openDetail(contact)}>Open</Button>{canManage && <Button size="small" startIcon={<EditOutlined />} onClick={() => openEdit(contact)}>Edit</Button>}</TableCell>
      </TableRow>)}</TableBody>
    </Table></TableContainer>}

    <Dialog open={Boolean(draft)} onClose={() => !saving && setDraft(null)} maxWidth="md" fullWidth>
      <DialogTitle>{draft?.id ? 'Edit contact' : 'Add contact'}</DialogTitle>
      <Box component="form" onSubmit={saveContact}>{draft && <DialogContent dividers><Stack spacing={3}>
        {workspace.isRoot && !draft.id && <FormControl fullWidth required><InputLabel>Customer</InputLabel><Select label="Customer" value={draft.companyId} onChange={event => setDraft({ ...draft, companyId: event.target.value })}>{workspace.companies.map(item => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</Select></FormControl>}
        <Box><Typography variant="subtitle2" sx={{ mb: 1.5 }}>Identity</Typography><Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth required label="Display name" value={draft.displayName} onChange={event => setDraft({ ...draft, displayName: event.target.value })} /></Grid>
          <Grid size={{ xs: 6, md: 3 }}><TextField fullWidth label="First name" value={draft.firstName} onChange={event => setDraft({ ...draft, firstName: event.target.value })} /></Grid>
          <Grid size={{ xs: 6, md: 3 }}><TextField fullWidth label="Last name" value={draft.lastName} onChange={event => setDraft({ ...draft, lastName: event.target.value })} /></Grid>
          <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth type="email" label="Email" value={draft.email} onChange={event => setDraft({ ...draft, email: event.target.value })} /></Grid>
          <Grid size={{ xs: 6, md: 3 }}><TextField fullWidth label="Phone" value={draft.phone} onChange={event => setDraft({ ...draft, phone: event.target.value })} /></Grid>
          <Grid size={{ xs: 6, md: 3 }}><TextField fullWidth label="Mobile" value={draft.mobile} onChange={event => setDraft({ ...draft, mobile: event.target.value })} /></Grid>
        </Grid></Box>
        <Box><Typography variant="subtitle2" sx={{ mb: 1.5 }}>Organisation</Typography><Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Job title" value={draft.jobTitle} onChange={event => setDraft({ ...draft, jobTitle: event.target.value })} /></Grid>
          <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Department" value={draft.department} onChange={event => setDraft({ ...draft, department: event.target.value })} /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Location" value={draft.location} onChange={event => setDraft({ ...draft, location: event.target.value })} /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Timezone" value={draft.timezone} onChange={event => setDraft({ ...draft, timezone: event.target.value })} /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><FormControl fullWidth><InputLabel>Status</InputLabel><Select label="Status" value={draft.status} onChange={event => setDraft({ ...draft, status: event.target.value as Contact['status'] })}>{statuses.map(value => <MenuItem key={value} value={value}>{roleLabel(value)}</MenuItem>)}</Select></FormControl></Grid>
          <Grid size={{ xs: 12 }}><FormControl fullWidth><InputLabel>Manager</InputLabel><Select label="Manager" value={draft.managerContactId || ''} onChange={event => setDraft({ ...draft, managerContactId: event.target.value || null })}><MenuItem value="">Not recorded</MenuItem>{contacts.filter(item => item.companyId === draft.companyId && item.id !== draft.id && ['active', 'on_leave'].includes(item.status)).map(item => <MenuItem key={item.id} value={item.id}>{item.displayName}</MenuItem>)}</Select></FormControl></Grid>
          {['left_company', 'inactive'].includes(draft.status) && <Grid size={{ xs: 12 }}><TextField fullWidth required label="Offboarding reason" value={draft.reason} onChange={event => setDraft({ ...draft, reason: event.target.value })} helperText="Responsibilities remain historically attributable and should be reassigned before deactivation." /></Grid>}
        </Grid></Box>
      </Stack></DialogContent>}
      <DialogActions><Button onClick={() => setDraft(null)} disabled={saving}>Cancel</Button><Button type="submit" variant="contained" disabled={saving}>{saving ? 'Saving…' : 'Save contact'}</Button></DialogActions></Box>
    </Dialog>

    <Dialog open={Boolean(selected)} onClose={() => setSelected(null)} maxWidth="md" fullWidth>
      <DialogTitle>{selected?.displayName || 'Contact details'}</DialogTitle>
      <DialogContent dividers>{selected && <Stack spacing={2}>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ justifyContent: 'space-between' }}><Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}><Avatar sx={{ bgcolor: 'primary.main' }}>{initials(selected.displayName)}</Avatar><Box><Typography variant="h6">{selected.jobTitle || 'Job title not recorded'}</Typography><Typography color="text.secondary">{selected.department || 'Department not recorded'} · {selected.email || 'No email'}</Typography></Box></Stack><Chip color={statusColor(selected.status)} label={roleLabel(selected.status)} /></Stack>
        <Grid container spacing={1.5}>{[
          ['Phone', selected.phone || selected.mobile || 'Not recorded'], ['Location', selected.location || 'Not recorded'],
          ['Source', roleLabel(selected.source)], ['Last synchronized', dateTime(selected.lastSynced)],
        ].map(([label, value]) => <Grid key={label} size={{ xs: 6 }}><Card variant="outlined"><CardContent><Typography variant="caption" color="text.secondary">{label}</Typography><Typography>{value}</Typography></CardContent></Card></Grid>)}</Grid>
        <Typography variant="h6">Responsibilities</Typography>
        {!selected.responsibilities.length ? <Alert severity="info">This contact has no current or historical responsibilities.</Alert> : <TableContainer component={Paper} variant="outlined"><Table size="small"><TableHead><TableRow><TableCell>Configuration item</TableCell><TableCell>Responsibility</TableCell><TableCell>Effective period</TableCell><TableCell /></TableRow></TableHead><TableBody>{selected.responsibilities.map(item => <TableRow key={item.id}><TableCell><Typography sx={{ fontWeight: 750 }}>{item.assetName}</Typography><Typography variant="caption" color="text.secondary">{item.assetType}</Typography></TableCell><TableCell><Chip size="small" label={roleLabel(item.role)} color={item.effectiveUntil ? 'default' : 'primary'} variant="outlined" /></TableCell><TableCell>{dateTime(item.effectiveFrom)} – {item.effectiveUntil ? dateTime(item.effectiveUntil) : 'Current'}</TableCell><TableCell><Button size="small" onClick={() => navigate(`/assets/${item.assetId}/show`)}>Open CI</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer>}
        <AuditTimeline entityType="contact" entityId={selected.id} companyId={selected.companyId} />
      </Stack>}</DialogContent>
      <DialogActions>{selected && canManage && <><Button startIcon={<EditOutlined />} onClick={() => { openEdit(selected); setSelected(null); }}>Edit</Button>{selected.responsibilityCount > 0 && <Button startIcon={<SwapHorizOutlined />} onClick={() => { setReassignContact(selected); setSelected(null); }}>Reassign responsibilities</Button>}{!selected.portalUser && <Button startIcon={<KeyOutlined />} disabled={!selected.email} onClick={() => { setPortalContact(selected); setSelected(null); }}>Create portal access</Button>}{selected.responsibilityCount > 0 && ['left_company', 'inactive'].includes(selected.status) && <Chip icon={<PersonOffOutlined />} color="error" label="Reassignment required" />}</>}<Button onClick={() => setSelected(null)}>Close</Button></DialogActions>
    </Dialog>

    <Dialog open={Boolean(portalContact)} onClose={() => !saving && setPortalContact(null)} maxWidth="sm" fullWidth>
      <DialogTitle>Create portal access</DialogTitle><Box component="form" onSubmit={createPortalUser}><DialogContent dividers><Alert severity="info" sx={{ mb: 2 }}>This creates a customer-reader login for {portalContact?.email}. The contact record remains the source for ownership and communication details.</Alert><TextField fullWidth required type="password" label="Temporary password" value={temporaryPassword} onChange={event => setTemporaryPassword(event.target.value)} slotProps={{ htmlInput: { minLength: 8 } }} helperText="Use Entra ID invitation instead when external authentication is enabled." /></DialogContent><DialogActions><Button onClick={() => setPortalContact(null)} disabled={saving}>Cancel</Button><Button type="submit" variant="contained" disabled={saving}>{saving ? 'Creating…' : 'Create access'}</Button></DialogActions></Box>
    </Dialog>

    <Dialog open={Boolean(reassignContact)} onClose={() => !saving && setReassignContact(null)} maxWidth="sm" fullWidth>
      <DialogTitle>Reassign responsibilities</DialogTitle><Box component="form" onSubmit={reassignResponsibilities}><DialogContent dividers><Alert severity="warning" sx={{ mb: 2 }}>All current CI responsibilities held by {reassignContact?.displayName} will move to the selected replacement. Effective dates and the original owner remain in history.</Alert><Stack spacing={2}><FormControl fullWidth required><InputLabel>Replacement contact</InputLabel><Select label="Replacement contact" value={replacementContactId} onChange={event => setReplacementContactId(event.target.value)}>{contacts.filter(item => item.companyId === reassignContact?.companyId && item.id !== reassignContact?.id && ['active', 'on_leave'].includes(item.status)).map(item => <MenuItem key={item.id} value={item.id}>{item.displayName}{item.department ? ` · ${item.department}` : ''}</MenuItem>)}</Select></FormControl><TextField fullWidth required label="Reason" value={reassignmentReason} onChange={event => setReassignmentReason(event.target.value)} helperText="For example role change, employee departure or temporary delegation." /></Stack></DialogContent><DialogActions><Button onClick={() => setReassignContact(null)} disabled={saving}>Cancel</Button><Button type="submit" variant="contained" startIcon={<SwapHorizOutlined />} disabled={saving || !replacementContactId || reassignmentReason.trim().length < 4}>{saving ? 'Reassigning…' : 'Reassign all'}</Button></DialogActions></Box>
    </Dialog>
  </Box>;
}
