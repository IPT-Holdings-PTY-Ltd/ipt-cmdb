import AddBusinessOutlined from '@mui/icons-material/AddBusinessOutlined';
import BackupOutlined from '@mui/icons-material/BackupOutlined';
import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import CloudSyncOutlined from '@mui/icons-material/CloudSyncOutlined';
import CloudUploadOutlined from '@mui/icons-material/CloudUploadOutlined';
import DeleteOutlined from '@mui/icons-material/DeleteOutlined';
import EditOutlined from '@mui/icons-material/EditOutlined';
import GroupsOutlined from '@mui/icons-material/GroupsOutlined';
import OpenInNewOutlined from '@mui/icons-material/OpenInNewOutlined';
import PaletteOutlined from '@mui/icons-material/PaletteOutlined';
import PersonAddAltOutlined from '@mui/icons-material/PersonAddAltOutlined';
import RestoreOutlined from '@mui/icons-material/RestoreOutlined';
import SecurityOutlined from '@mui/icons-material/SecurityOutlined';
import StorageOutlined from '@mui/icons-material/StorageOutlined';
import InsertPhotoOutlined from '@mui/icons-material/InsertPhotoOutlined';
import {
  Alert, Box, Button, Card, CardContent, Checkbox, Chip, CircularProgress, Dialog, DialogActions, DialogContent,
  DialogTitle, Divider, FormControl, FormControlLabel, Grid, InputLabel, List, ListItem, ListItemIcon, ListItemText,
  MenuItem, Paper, Select, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Title, useNotify } from 'react-admin';
import { Navigate } from 'react-router-dom';
import { apiFetch, getSession } from './session';
import type { AccessGroup, Company, Integration, RoleTemplate, SyncRun, User } from './types';
import { useWorkspace } from './workspace';
import { DEFAULT_MSP_BRAND, useMspBranding, type Brand } from './branding';

type Notice = { severity: 'success' | 'error' | 'info' | 'warning'; message: string } | null;
type DatabaseStatus = { configured: boolean; available: boolean; mode: string; source: string; managedByEnvironment?: boolean; error?: string | null; diagnosticError?: string | null; settings: DatabaseSettings; database?: string; databaseUser?: string; server?: string; schemaVersion?: string | null; expectedSchemaVersion?: string; migrationsPending?: boolean; initialized?: boolean; schemaState?: string; canCreateSchemaObjects?: boolean };
type DatabaseSettings = { host?: string; port?: number | string; database?: string; username?: string; password?: string; sslmode?: string; seedMode?: 'current' | 'empty' | 'demo' };
type RestorePreview = { valid: boolean; version: number; createdAt?: string; companies: number; users: number; assets: number; relationships: number; changes: number; warning: string };
type EffectiveAccess = { user: User; role: RoleTemplate; customers: string[]; scope: string };

const titleSx = { mb: 3 };

function RootGuard({ adminOnly = false, children }: { adminOnly?: boolean; children: React.ReactNode }) {
  const workspace = useWorkspace();
  const role = getSession()?.user.role;
  if (!workspace.isRoot) return <Navigate to="/" replace />;
  if (!['platform_admin', 'msp_operator'].includes(role || '') || (adminOnly && role !== 'platform_admin')) {
    return <Alert severity="error">This page requires {adminOnly ? 'platform administrator' : 'MSP'} access.</Alert>;
  }
  return <>{children}</>;
}

function PageHeading({ eyebrow, title, copy, action }: { eyebrow: string; title: string; copy: string; action?: React.ReactNode }) {
  return (
    <Box sx={{ ...titleSx, display: 'flex', gap: 2, justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap' }}>
      <Box><Typography variant="overline" color="primary">{eyebrow}</Typography><Typography variant="h3">{title}</Typography><Typography
        sx={{
          color: "text.secondary",
          mt: 0.75,
          maxWidth: 780
        }}>{copy}</Typography></Box>
      {action}
    </Box>
  );
}

function LoadingCard() {
  return (
    <Card><CardContent><Stack direction="row" spacing={2} sx={{
      alignItems: "center"
    }}><CircularProgress size={22} /><Typography>Loading configuration…</Typography></Stack></CardContent></Card>
  );
}

function ErrorNotice({ error }: { error: unknown }) {
  return <Alert severity="error">{error instanceof Error ? error.message : 'The configuration could not be loaded.'}</Alert>;
}

export function CustomersPage() {
  const workspace = useWorkspace();
  const notify = useNotify();
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [saving, setSaving] = useState(false);

  async function createCustomer(event: FormEvent) {
    event.preventDefault(); setSaving(true);
    try {
      const created = await apiFetch<Company>('/api/companies', { method: 'POST', body: JSON.stringify({ name, slug }) });
      await workspace.refreshCompanies(); setName(''); setSlug('');
      notify(`${created.name} created`, { type: 'success' });
    } catch (error) { notify(error instanceof Error ? error.message : 'Customer creation failed', { type: 'error' }); }
    finally { setSaving(false); }
  }

  return (
    <RootGuard adminOnly><Title title="Customers" /><PageHeading eyebrow="Organisation" title="Customer management" copy="Create and open isolated customer workspaces. Customer deletion is intentionally not offered while CIs, users, or source mappings may still depend on the tenant." />
      <Grid container spacing={3}>
        <Grid size={{ xs: 12, lg: 5 }}><Card><CardContent><Stack component="form" spacing={2} onSubmit={createCustomer}>
          <Box><Typography variant="h5">Add customer</Typography><Typography sx={{
            color: "text.secondary"
          }}>A stable customer ID is generated from the name when left blank.</Typography></Box>
          <TextField label="Customer name" value={name} onChange={event => setName(event.target.value)} required placeholder="Contoso Ltd" />
          <TextField label="Customer ID (optional)" value={slug} onChange={event => setSlug(event.target.value)} helperText="Lowercase letters, numbers and hyphens" slotProps={{ htmlInput: { pattern: '[a-z0-9-]+' } }} placeholder="contoso" />
          <Button type="submit" variant="contained" startIcon={<AddBusinessOutlined />} disabled={saving}>{saving ? 'Creating…' : 'Create customer'}</Button>
        </Stack></CardContent></Card></Grid>
        <Grid size={{ xs: 12, lg: 7 }}><Card><CardContent><Typography variant="h5">Managed customers</Typography><Typography
          sx={{
            color: "text.secondary",
            mb: 2
          }}>{workspace.companies.length} customer workspaces</Typography>
          <Stack divider={<Divider flexItem />}>
            {workspace.companies.map(company => <Stack
              key={company.id}
              direction="row"
              spacing={2}
              sx={{
                alignItems: "center",
                justifyContent: "space-between",
                py: 1.5
              }}>
              <Box><Typography sx={{
                fontWeight: 750
              }}>{company.name}</Typography><Typography variant="caption" sx={{
                color: "text.secondary"
              }}>Tenant ID: {company.id}</Typography></Box>
              <Button size="small" endIcon={<OpenInNewOutlined />} onClick={() => workspace.setCompanyId(company.id)}>Open workspace</Button>
            </Stack>)}
          </Stack>
        </CardContent></Card></Grid>
      </Grid>
    </RootGuard>
  );
}

export function UsersPage() {
  const workspace = useWorkspace();
  const actor = getSession()?.user;
  const [users, setUsers] = useState<User[]>([]);
  const [groups, setGroups] = useState<AccessGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<Notice>(null);
  const [accountType, setAccountType] = useState<'root' | 'customer'>(actor?.role === 'platform_admin' ? 'root' : 'customer');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [companyIds, setCompanyIds] = useState<string[]>([]);
  const [companyId, setCompanyId] = useState('');
  const [groupId, setGroupId] = useState('');

  const load = async () => {
    setLoading(true);
    try {
      const [userData, groupData] = await Promise.all([apiFetch<User[]>('/api/users'), apiFetch<AccessGroup[]>('/api/access-groups')]);
      setUsers(userData); setGroups(groupData); setCompanyId(value => value || workspace.companies[0]?.id || '');
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Users could not be loaded.' }); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);

  async function createUser(event: FormEvent) {
    event.preventDefault(); setNotice(null);
    try {
      await apiFetch<User>('/api/users', { method: 'POST', body: JSON.stringify(accountType === 'root'
        ? { accountType, email, password, companyIds, groupIds: groupId ? [groupId] : [] }
        : { accountType, email, password, companyId }) });
      setEmail(''); setPassword(''); setCompanyIds([]); setGroupId('');
      setNotice({ severity: 'success', message: 'User created. Use Entra ID invitations instead of temporary app passwords in production.' });
      await load();
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'User creation failed.' }); }
  }

  const names = Object.fromEntries(workspace.companies.map(company => [company.id, company.name]));
  return (
    <RootGuard><Title title="Users" /><PageHeading eyebrow="Access" title="User management" copy="Root users receive explicit customer scope from a reusable access group plus optional direct customer access. Customer users remain bound to one tenant." />
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}
      <Grid container spacing={3}>
        <Grid size={{ xs: 12, xl: 5 }}><Card><CardContent><Stack component="form" spacing={2} onSubmit={createUser}>
          <Typography variant="h5">Add user</Typography>
          {actor?.role === 'platform_admin' && <FormControl><InputLabel>Account type</InputLabel><Select label="Account type" value={accountType} onChange={event => setAccountType(event.target.value as 'root' | 'customer')}>
            <MenuItem value="root">MSP user</MenuItem><MenuItem value="customer">Customer user</MenuItem>
          </Select></FormControl>}
          <TextField label="Email" type="email" value={email} onChange={event => setEmail(event.target.value)} required />
          <TextField label="Temporary password" type="password" value={password} onChange={event => setPassword(event.target.value)} slotProps={{ htmlInput: { minLength: 8 } }} helperText="At least 8 characters; local authentication only" required />
          {accountType === 'root' ? <>
            <FormControl><InputLabel>MSP access group</InputLabel><Select label="MSP access group" value={groupId} onChange={event => setGroupId(event.target.value)}><MenuItem value="">No access group</MenuItem>{groups.map(group => <MenuItem key={group.id} value={group.id}>{group.name} ({group.companyIds.length} customers)</MenuItem>)}</Select></FormControl>
            <Typography variant="subtitle2">Additional customer access</Typography>
            <Paper variant="outlined" sx={{ p: 1.5, maxHeight: 190, overflow: 'auto' }}>{workspace.companies.map(company => <FormControlLabel key={company.id} control={<Checkbox checked={companyIds.includes(company.id)} onChange={(_, checked) => setCompanyIds(items => checked ? [...items, company.id] : items.filter(id => id !== company.id))} />} label={company.name} />)}</Paper>
          </> : <FormControl><InputLabel>Customer</InputLabel><Select label="Customer" value={companyId} onChange={event => setCompanyId(event.target.value)} required>{workspace.companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}</Select></FormControl>}
          <Button variant="contained" type="submit" startIcon={<PersonAddAltOutlined />}>Create user</Button>
        </Stack></CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 7 }}><Card><CardContent><Typography variant="h5">Users in your scope</Typography><Typography
          sx={{
            color: "text.secondary",
            mb: 2
          }}>Server-side permissions determine which records are visible here.</Typography>
          {loading ? <CircularProgress size={24} /> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>User</TableCell><TableCell>Role</TableCell><TableCell>Customer scope</TableCell></TableRow></TableHead><TableBody>{users.map(user => <TableRow key={user.id}><TableCell>{user.email}</TableCell><TableCell><Chip size="small" label={user.role.replaceAll('_', ' ')} /></TableCell><TableCell>{user.role === 'platform_admin' ? 'All customers' : user.companyIds.map(id => names[id] || id).join(', ') || 'No access'}</TableCell></TableRow>)}</TableBody></Table></TableContainer>}
        </CardContent></Card></Grid>
      </Grid>
    </RootGuard>
  );
}

export function CustomerGroupsPage() {
  const workspace = useWorkspace();
  const [groups, setGroups] = useState<AccessGroup[]>([]);
  const [editing, setEditing] = useState<AccessGroup | null>(null);
  const [name, setName] = useState('');
  const [companyIds, setCompanyIds] = useState<string[]>([]);
  const [notice, setNotice] = useState<Notice>(null);
  const load = () => apiFetch<AccessGroup[]>('/api/access-groups').then(setGroups).catch(error => setNotice({ severity: 'error', message: error.message }));
  useEffect(() => { void load(); }, []);

  function reset() { setEditing(null); setName(''); setCompanyIds([]); }
  function edit(group: AccessGroup) { setEditing(group); setName(group.name); setCompanyIds(group.companyIds); }
  async function save(event: FormEvent) {
    event.preventDefault();
    try {
      await apiFetch(editing ? `/api/access-groups/${editing.id}` : '/api/access-groups', { method: editing ? 'PUT' : 'POST', body: JSON.stringify({ name, companyIds }) });
      setNotice({ severity: 'success', message: editing ? 'Customer group updated.' : 'Customer group created.' }); reset(); await load();
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Group could not be saved.' }); }
  }
  async function remove(group: AccessGroup) {
    if (!window.confirm(`Delete ${group.name}? Existing users keep their direct customer permissions.`)) return;
    try { await apiFetch(`/api/access-groups/${group.id}`, { method: 'DELETE' }); setNotice({ severity: 'success', message: 'Customer group deleted.' }); await load(); }
    catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Group could not be deleted.' }); }
  }
  const companyName = (id: string) => workspace.companies.find(company => company.id === id)?.name || id;
  return (
    <RootGuard adminOnly><Title title="Customer groups" /><PageHeading eyebrow="Organisation" title="Customer groups" copy="Bundle customer scopes into reusable access groups for MSP users. The dynamic All managed customers group follows the tenant list automatically." />
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}
      <Grid container spacing={3}>
        <Grid size={{ xs: 12, lg: 5 }}><Card><CardContent><Stack component="form" spacing={2} onSubmit={save}><Typography variant="h5">{editing ? `Edit ${editing.name}` : 'Create customer group'}</Typography><TextField label="Group name" value={name} onChange={event => setName(event.target.value)} required />
          <Typography variant="subtitle2">Customers</Typography><Paper variant="outlined" sx={{ p: 1.5 }}>{workspace.companies.map(company => <FormControlLabel key={company.id} control={<Checkbox checked={companyIds.includes(company.id)} onChange={(_, checked) => setCompanyIds(items => checked ? [...items, company.id] : items.filter(id => id !== company.id))} />} label={company.name} />)}</Paper>
          <Stack direction="row" spacing={1}><Button type="submit" variant="contained" startIcon={editing ? <EditOutlined /> : <GroupsOutlined />}>{editing ? 'Save group' : 'Create group'}</Button>{editing && <Button onClick={reset}>Cancel</Button>}</Stack>
        </Stack></CardContent></Card></Grid>
        <Grid size={{ xs: 12, lg: 7 }}><Stack spacing={2}>{groups.map(group => <Card key={group.id}><CardContent><Stack direction="row" spacing={2} sx={{
          justifyContent: "space-between"
        }}><Box><Stack direction="row" spacing={1} sx={{
          alignItems: "center"
        }}><Typography variant="h6">{group.name}</Typography>{group.system && <Chip size="small" label="Dynamic" color="primary" variant="outlined" />}</Stack><Typography sx={{
          color: "text.secondary"
        }}>{group.companyIds.includes('*') ? 'Every managed customer' : group.companyIds.map(companyName).join(', ')}</Typography></Box>{!group.system && <Stack direction="row"><Button size="small" startIcon={<EditOutlined />} onClick={() => edit(group)}>Edit</Button><Button size="small" color="error" startIcon={<DeleteOutlined />} onClick={() => remove(group)}>Delete</Button></Stack>}</Stack></CardContent></Card>)}</Stack></Grid>
      </Grid>
    </RootGuard>
  );
}

export function RbacPage() {
  const [roles, setRoles] = useState<RoleTemplate[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [selected, setSelected] = useState('');
  const [effective, setEffective] = useState<EffectiveAccess | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { Promise.all([apiFetch<RoleTemplate[]>('/api/rbac/roles'), apiFetch<User[]>('/api/users')]).then(([roleData, userData]) => { setRoles(roleData); setUsers(userData); setSelected(userData[0]?.id || ''); }).catch(setError); }, []);
  useEffect(() => { if (selected) apiFetch<EffectiveAccess>(`/api/rbac/effective?userId=${encodeURIComponent(selected)}`).then(setEffective).catch(setError); }, [selected]);
  return (
    <RootGuard adminOnly><Title title="Access control" /><PageHeading eyebrow="Access" title="Role-based access control" copy="Review the fixed role templates and preview the effective customer scope for any user. Custom roles are a future extension point; server permissions remain authoritative." />
      {error ? <ErrorNotice error={error} /> : null}
      <Grid container spacing={2} sx={{ mb: 3 }}>{roles.map(role => <Grid key={role.id} size={{ xs: 12, lg: 4 }}><Card sx={{ height: '100%' }}><CardContent><SecurityOutlined color="primary" /><Typography variant="h6" sx={{ mt: 1 }}>{role.name}</Typography><Chip size="small" label={role.scope} sx={{ my: 1 }} /><List dense>{role.permissions.map(permission => <ListItem key={permission} disableGutters><ListItemIcon sx={{ minWidth: 30 }}><CheckCircleOutlined color="success" fontSize="small" /></ListItemIcon><ListItemText primary={permission} /></ListItem>)}</List></CardContent></Card></Grid>)}</Grid>
      <Card><CardContent><Typography variant="h5">Effective access preview</Typography><Typography
        sx={{
          color: "text.secondary",
          mb: 2
        }}>Select a user to see the final role and tenant boundary resolved by the API.</Typography><FormControl fullWidth sx={{ maxWidth: 520, mb: 2 }}><InputLabel>User</InputLabel><Select label="User" value={selected} onChange={event => setSelected(event.target.value)}>{users.map(user => <MenuItem key={user.id} value={user.id}>{user.email} — {user.role.replaceAll('_', ' ')}</MenuItem>)}</Select></FormControl>{effective && <Alert severity="info"><Typography sx={{
        fontWeight: 750
      }}>{effective.user.email} · {effective.role.name}</Typography><Typography variant="body2">Customer scope: {effective.scope}</Typography></Alert>}</CardContent></Card>
    </RootGuard>
  );
}

export function IntegrationsPage() {
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [runs, setRuns] = useState<SyncRun[]>([]);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState<unknown>(null);
  const load = async () => { try { const [items, activity] = await Promise.all([apiFetch<Integration[]>('/api/integrations'), apiFetch<SyncRun[]>('/api/sync-runs')]); setIntegrations(items); setRuns(activity); } catch (value) { setError(value); } };
  useEffect(() => { void load(); }, []);
  async function sync(type: string) { setBusy(type); setError(null); try { await apiFetch(`/api/integrations/${type}/sync`, { method: 'POST' }); await load(); } catch (value) { setError(value); } finally { setBusy(''); } }
  return (
    <RootGuard><Title title="Integrations" /><PageHeading eyebrow="Operations" title="Third-party integrations" copy="MSP-wide source adapters live here. Connection settings are supplied securely by the API host; discovery and import remain review-gated while mapping rules are being built." action={<Chip label="MSP scope" color="primary" variant="outlined" />} />
      {error ? <Box sx={{ mb: 2 }}><ErrorNotice error={error} /></Box> : null}
      <Grid container spacing={2}>{integrations.map(integration => <Grid key={integration.id} size={{ xs: 12, md: 4 }}><Card sx={{ height: '100%' }}><CardContent><Stack direction="row" sx={{
        justifyContent: "space-between"
      }}><CloudSyncOutlined color="primary" /><Chip size="small" color={integration.enabled ? 'success' : 'default'} label={integration.status} /></Stack><Typography variant="h5" sx={{ mt: 2 }}>{integration.name}</Typography><Typography
        sx={{
          color: "text.secondary",
          my: 1
        }}>{integration.enabled ? 'Host configuration detected' : 'Awaiting secure host configuration'}</Typography><Typography variant="caption" sx={{
        display: "block"
      }}>{integration.lastSync ? `Last checked ${new Date(integration.lastSync).toLocaleString()}` : 'No sync attempts yet'}</Typography><Button sx={{ mt: 2 }} fullWidth variant="outlined" disabled={busy === integration.type} onClick={() => sync(integration.type)}>{busy === integration.type ? 'Checking…' : 'Test & sync'}</Button></CardContent></Card></Grid>)}</Grid>
      <Card sx={{ mt: 3 }}><CardContent><Typography variant="h5">Recent sync activity</Typography><Typography
        sx={{
          color: "text.secondary",
          mb: 2
        }}>Adapter checks and review-gated discovery results.</Typography>{!runs.length ? <Alert severity="info">No sync attempts yet.</Alert> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>Source</TableCell><TableCell>Status</TableCell><TableCell>Result</TableCell><TableCell>Finished</TableCell></TableRow></TableHead><TableBody>{runs.slice(0, 10).map(run => <TableRow key={run.id}><TableCell>{run.type}</TableCell><TableCell><Chip size="small" label={run.status} color={run.status === 'success' ? 'success' : run.status === 'failed' ? 'error' : 'warning'} /></TableCell><TableCell>{run.message}</TableCell><TableCell>{run.finishedAt ? new Date(run.finishedAt).toLocaleString() : 'In progress'}</TableCell></TableRow>)}</TableBody></Table></TableContainer>}</CardContent></Card>
    </RootGuard>
  );
}

export function BrandingPage() {
  const applied = useMspBranding();
  const [brand, setBrand] = useState<Brand>(applied.brand);
  const [notice, setNotice] = useState<Notice>(null);
  useEffect(() => { apiFetch<Partial<Brand>>('/api/branding?scope=msp').then(value => setBrand({ ...DEFAULT_MSP_BRAND, ...value })).catch(error => setNotice({ severity: 'error', message: error.message })); }, []);
  const update = (key: keyof Brand, value: string) => setBrand(current => ({ ...current, [key]: value }));
  async function selectLogo(file?: File) {
    if (!file) return;
    if (!['image/png', 'image/jpeg'].includes(file.type)) { setNotice({ severity: 'error', message: 'Choose a PNG or JPEG logo.' }); return; }
    if (file.size > 1_000_000) { setNotice({ severity: 'error', message: 'The logo must be 1 MB or smaller.' }); return; }
    const logoDataUrl = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(reader.error); reader.readAsDataURL(file); });
    setBrand(current => ({ ...current, logoDataUrl, logoFileName: file.name }));
    setNotice({ severity: 'info', message: 'Logo ready. Save MSP branding to publish it.' });
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    try {
      const stored = await apiFetch<Brand>('/api/branding', { method: 'PUT', body: JSON.stringify({ scope: 'msp', ...brand }) });
      setBrand(stored); window.dispatchEvent(new CustomEvent('cmdb-branding-change'));
      setNotice({ severity: 'success', message: 'MSP branding published to the application and reports.' });
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Branding could not be saved.' }); }
  }
  const previewLogo = brand.logoDataUrl
    ? <Box component="img" src={brand.logoDataUrl} alt={`${brand.name} logo preview`} sx={{ width: 72, height: 56, objectFit: 'contain' }} />
    : <Box sx={{ width: 54, height: 54, borderRadius: 2, background: `linear-gradient(135deg, ${brand.accent}, ${brand.secondaryAccent})`, color: '#07121d', display: 'grid', placeItems: 'center', fontWeight: 900, fontSize: 20 }}>{brand.logoText || 'C'}</Box>;
  return (
    <RootGuard><Title title="Branding" /><PageHeading eyebrow="Settings" title="MSP branding" copy="Publish one trusted MSP identity across sign-in, workspace screens and generated customer reports." action={<Chip label={brand.logoDataUrl ? 'Logo configured' : 'Fallback mark active'} color={brand.logoDataUrl ? 'success' : 'default'} variant="outlined" />} />
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}
      <Grid container spacing={3}><Grid size={{ xs: 12, xl: 7 }}><Card><CardContent><Stack component="form" spacing={3} onSubmit={save}>
        <Box><Typography variant="h5">Brand identity</Typography><Typography sx={{
          color: "text.secondary"
        }}>Use a horizontal or compact transparent PNG where possible. The fallback letters remain available if the image cannot load.</Typography></Box>
        <Grid container spacing={2}><Grid size={{ xs: 12, md: 8 }}><TextField fullWidth label="MSP display name" value={brand.name} onChange={event => update('name', event.target.value)} required /></Grid><Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Fallback letters" value={brand.logoText} onChange={event => update('logoText', event.target.value.toUpperCase().slice(0, 3))} slotProps={{ htmlInput: { maxLength: 3 } }} required /></Grid></Grid>
        <Paper variant="outlined" sx={{ p: 2 }}><Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{
          alignItems: { sm: 'center' }
        }}>{previewLogo}<Box sx={{ flex: 1 }}><Typography sx={{
          fontWeight: 750
        }}>{brand.logoFileName || 'No logo uploaded'}</Typography><Typography variant="body2" sx={{
          color: "text.secondary"
        }}>PNG or JPEG · maximum 1 MB · used on screens and PDF reports</Typography></Box><Stack direction="row" spacing={1}><Button component="label" variant="outlined" startIcon={<CloudUploadOutlined />}>Choose logo<input hidden type="file" accept="image/png,image/jpeg" onChange={event => void selectLogo(event.target.files?.[0])} /></Button>{brand.logoDataUrl && <Button color="error" startIcon={<DeleteOutlined />} onClick={() => setBrand(current => ({ ...current, logoDataUrl: '', logoFileName: '' }))}>Remove</Button>}</Stack></Stack></Paper>
        <Divider /><Box><Typography variant="h5">Colours and sign-in</Typography><Typography sx={{
        color: "text.secondary"
      }}>The colours update application controls and the report accent treatment.</Typography></Box>
        <Grid container spacing={2}><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Primary accent" type="color" value={brand.accent} onChange={event => update('accent', event.target.value)} /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Secondary accent" type="color" value={brand.secondaryAccent} onChange={event => update('secondaryAccent', event.target.value)} /></Grid></Grid>
        <TextField label="Sign-in welcome message" multiline minRows={2} value={brand.welcomeMessage} onChange={event => update('welcomeMessage', event.target.value)} helperText="Shown beneath the MSP name on the login page" />
        <Divider /><Box><Typography variant="h5">Support and report identity</Typography><Typography sx={{
        color: "text.secondary"
      }}>These details help technicians and customers identify the report owner.</Typography></Box>
        <Grid container spacing={2}><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Support email" type="email" value={brand.supportEmail} onChange={event => update('supportEmail', event.target.value)} /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Support phone" value={brand.supportPhone} onChange={event => update('supportPhone', event.target.value)} /></Grid></Grid>
        <TextField label="Support portal URL" type="url" value={brand.supportUrl} onChange={event => update('supportUrl', event.target.value)} />
        <TextField label="Report footer" value={brand.reportFooter} onChange={event => update('reportFooter', event.target.value)} placeholder={`${brand.name} | Controlled change record`} helperText="Change reference and page number are added automatically" />
        <TextField label="Confidentiality label" value={brand.confidentialityLabel} onChange={event => update('confidentialityLabel', event.target.value)} placeholder="Internal use only" />
        <Button type="submit" size="large" variant="contained" startIcon={<PaletteOutlined />}>Publish MSP branding</Button>
      </Stack></CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 5 }}><Stack spacing={3} sx={{ position: { xl: 'sticky' }, top: { xl: 88 } }}><Card><CardContent><Typography variant="overline" color="primary">Application preview</Typography><Paper variant="outlined" sx={{ mt: 1, p: 3, minHeight: 260, background: 'linear-gradient(135deg, #101a2d, #080f1d)' }}><Stack direction="row" spacing={2} sx={{
          alignItems: "center"
        }}>{previewLogo}<Box><Typography variant="caption" sx={{
          color: "text.secondary"
        }}>MSP PLATFORM</Typography><Typography variant="h5">{brand.name || 'CMDB Hub'}</Typography></Box></Stack><Divider sx={{ my: 3 }} /><Typography variant="h4">{brand.welcomeMessage || 'Asset intelligence for your organisation.'}</Typography><Typography
          sx={{
            color: "text.secondary",
            mt: 2
          }}>{brand.supportEmail || brand.supportPhone || brand.supportUrl || 'Support details not configured'}</Typography></Paper></CardContent></Card>
          <Card><CardContent><Stack direction="row" spacing={1} sx={{
            alignItems: "center"
          }}><InsertPhotoOutlined color="primary" /><Typography variant="overline" color="primary">Report preview</Typography></Stack><Paper variant="outlined" sx={{ mt: 1, p: 2.5, bgcolor: '#fff', color: '#172033', minHeight: 260 }}><Stack
            direction="row"
            spacing={2}
            sx={{
              alignItems: "center",
              bgcolor: '#0b1526',
              color: '#fff',
              p: 2
            }}>{previewLogo}<Box sx={{ flex: 1 }}><Typography variant="h6" sx={{
            color: "inherit"
          }}>Change control package</Typography><Typography variant="caption" sx={{ color: '#d7e5ea' }}>{brand.name}</Typography></Box><Typography variant="caption" sx={{ color: '#d7e5ea' }}>CHG-2026-0001</Typography></Stack><Box sx={{ height: 5, bgcolor: brand.accent, mt: 2, borderRadius: 4 }} /><Typography
            sx={{
              fontWeight: 800,
              mt: 2
            }}>Change summary</Typography><Typography variant="body2" sx={{ color: '#5e6b80' }}>Customer, schedule, risk and impacted configuration items</Typography><Divider sx={{ my: 3, borderColor: '#d8e1e8' }} /><Stack direction="row" sx={{
            justifyContent: "space-between"
          }}><Typography variant="caption" sx={{ color: '#5e6b80' }}>{brand.reportFooter || `${brand.name} | Controlled change record`}</Typography><Typography variant="caption" sx={{ color: '#5e6b80' }}>{brand.confidentialityLabel || 'Internal use only'}</Typography></Stack></Paper></CardContent></Card></Stack></Grid></Grid>
    </RootGuard>
  );
}

export function DatabasePage() {
  const [status, setStatus] = useState<DatabaseStatus | null>(null);
  const [settings, setSettings] = useState<DatabaseSettings>({ host: 'localhost', port: 5432, database: 'cmdb', username: 'cmdb', password: '', sslmode: 'prefer', seedMode: 'current' });
  const [notice, setNotice] = useState<Notice>(null);
  const [backup, setBackup] = useState<Record<string, unknown> | null>(null);
  const [restoreDocument, setRestoreDocument] = useState<Record<string, unknown> | null>(null);
  const [restoreFileName, setRestoreFileName] = useState('');
  const [restorePreview, setRestorePreview] = useState<RestorePreview | null>(null);
  const [restoreConfirmed, setRestoreConfirmed] = useState(false);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const load = async () => { try { const value = await apiFetch<DatabaseStatus>('/api/database/status'); setStatus(value); setSettings(current => ({ ...current, ...value.settings, password: '' })); } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Database status unavailable.' }); } };
  useEffect(() => { void load(); }, []);
  const update = (key: keyof DatabaseSettings, value: string | number) => setSettings(current => ({ ...current, [key]: value }));
  async function test() { setNotice({ severity: 'info', message: 'Testing connection and schema privileges…' }); try { const result = await apiFetch<{ database: string; server: string; schemaState: string; canCreateSchemaObjects: boolean }>('/api/database/test', { method: 'POST', body: JSON.stringify(settings) }); setNotice({ severity: result.canCreateSchemaObjects || result.schemaState === 'ready' ? 'success' : 'warning', message: `Connected to ${result.database} · ${result.schemaState} schema · ${result.canCreateSchemaObjects ? 'migration privileges available' : 'no CREATE privilege'}` }); } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Connection failed.' }); } }
  async function save(event: FormEvent) { event.preventDefault(); setNotice({ severity: 'info', message: 'Applying migrations and activating PostgreSQL…' }); try { const result = await apiFetch<{ message: string; repositoryMode: string }>('/api/database/config', { method: 'PUT', body: JSON.stringify(settings) }); setNotice({ severity: 'success', message: `${result.message} Repository: ${result.repositoryMode}.` }); update('password', ''); await load(); } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Database configuration was not saved.' }); } }
  async function downloadBackup() { try { const document = await apiFetch<Record<string, unknown> & { createdAt: string }>('/api/database/backup'); setBackup(document); const url = URL.createObjectURL(new Blob([JSON.stringify(document, null, 2)], { type: 'application/json' })); const link = documentRef().createElement('a'); link.href = url; link.download = `cmdb-hub-portable-export-${document.createdAt.slice(0, 10)}.json`; link.click(); URL.revokeObjectURL(url); setNotice({ severity: 'success', message: 'Portable export downloaded. It contains operational data and credential hashes; store it securely.' }); } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Export failed.' }); } }
  async function chooseRestoreFile(file?: File) { if (!file) return; setRestorePreview(null); setRestoreConfirmed(false); try { const document = JSON.parse(await file.text()) as Record<string, unknown>; const preview = await apiFetch<RestorePreview>('/api/database/restore/preview', { method: 'POST', body: JSON.stringify(document) }); setRestoreDocument(document); setRestoreFileName(file.name); setRestorePreview(preview); } catch (error) { setRestoreDocument(null); setRestoreFileName(file.name); setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Backup validation failed.' }); } }
  async function restore() { if (!restoreDocument || !restorePreview || !restoreConfirmed) return; try { const result = await apiFetch<{ message: string; companies: number; assets: number }>('/api/database/restore', { method: 'POST', body: JSON.stringify(restoreDocument) }); setNotice({ severity: 'success', message: `${result.message} ${result.companies} customers and ${result.assets} assets are now represented.` }); setRestoreOpen(false); setRestoreDocument(null); setRestorePreview(null); setRestoreFileName(''); setRestoreConfirmed(false); await load(); } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Portable import failed.' }); } }
  const managed = Boolean(status?.managedByEnvironment);
  return (
    <RootGuard adminOnly><Title title="Database" /><PageHeading eyebrow="Settings" title="Database and recovery" copy="Review PostgreSQL readiness, initialise an empty database, and manage portable operational exports. PostgreSQL point-in-time recovery remains an infrastructure responsibility." action={status && <Chip label={status.available && !status.migrationsPending ? 'PostgreSQL ready' : status.mode} color={status.available && !status.migrationsPending ? 'success' : 'warning'} />} />
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}
      {!status ? <LoadingCard /> : <Grid container spacing={3}>
        <Grid size={{ xs: 12 }}><Card><CardContent><Stack direction={{ xs: 'column', lg: 'row' }} spacing={3} sx={{
          justifyContent: "space-between"
        }}><Box><Typography variant="h5">Database readiness</Typography><Typography sx={{
          color: "text.secondary"
        }}>{status.database || settings.database || 'No database'} · {status.server || status.mode}</Typography></Box><Stack direction="row" spacing={1} useFlexGap sx={{
          flexWrap: "wrap"
        }}><Chip label={status.available ? 'Reachable' : 'Unavailable'} color={status.available ? 'success' : 'error'} /><Chip label={`Schema ${status.schemaVersion || 'not installed'}`} color={status.migrationsPending ? 'warning' : 'success'} variant="outlined" /><Chip label={status.initialized ? 'Operational state seeded' : 'Awaiting seed'} color={status.initialized ? 'success' : 'warning'} variant="outlined" /><Chip label={status.databaseUser || 'No database user'} variant="outlined" /></Stack></Stack>{(status.error || status.diagnosticError) && <Alert severity="warning" sx={{ mt: 2 }}>{status.error || status.diagnosticError}</Alert>}</CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 7 }}><Card><CardContent>{managed ? <Stack spacing={2}><Box><Typography variant="h5">Managed PostgreSQL connection</Typography><Typography sx={{
          color: "text.secondary"
        }}>Source: environment or Key Vault-backed application setting</Typography></Box><Alert severity="info">This connection is intentionally read-only here. Update the Azure Container App environment or Key Vault reference, then restart the service.</Alert><Grid container spacing={2}><Grid size={{ xs: 12, md: 8 }}><TextField fullWidth disabled label="Host" value={settings.host || ''} /></Grid><Grid size={{ xs: 12, md: 4 }}><TextField fullWidth disabled label="Port" value={settings.port || 5432} /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth disabled label="Database" value={settings.database || ''} /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth disabled label="Username" value={settings.username || ''} /></Grid></Grid><Button variant="outlined" onClick={() => void load()}>Refresh readiness</Button></Stack> : <Stack component="form" spacing={2} onSubmit={save}><Box><Typography variant="h5">PostgreSQL connection and initialisation</Typography><Typography sx={{
          color: "text.secondary"
        }}>Local setup only. Production credentials should be injected from Key Vault.</Typography></Box><Grid container spacing={2}><Grid size={{ xs: 12, md: 8 }}><TextField fullWidth label="Host" value={settings.host || ''} onChange={event => update('host', event.target.value)} required /></Grid><Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Port" type="number" value={settings.port || 5432} onChange={event => update('port', Number(event.target.value))} required /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Database" value={settings.database || ''} onChange={event => update('database', event.target.value)} required /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Username" value={settings.username || ''} onChange={event => update('username', event.target.value)} required /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Password" type="password" value={settings.password || ''} onChange={event => update('password', event.target.value)} /></Grid><Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth><InputLabel>SSL mode</InputLabel><Select label="SSL mode" value={settings.sslmode || 'prefer'} onChange={event => update('sslmode', event.target.value)}><MenuItem value="prefer">Prefer SSL</MenuItem><MenuItem value="require">Require SSL</MenuItem><MenuItem value="verify-full">Verify full certificate</MenuItem><MenuItem value="disable">Disable SSL (local only)</MenuItem></Select></FormControl></Grid></Grid><FormControl fullWidth><InputLabel>Blank database seed</InputLabel><Select label="Blank database seed" value={settings.seedMode || 'current'} onChange={event => update('seedMode', event.target.value)}><MenuItem value="current">Migrate this workspace</MenuItem><MenuItem value="empty">Platform setup only</MenuItem><MenuItem value="demo">Demo customers and assets</MenuItem></Select></FormControl><Typography variant="caption" sx={{
          color: "text.secondary"
        }}>The seed choice is used only when the target database has no CMDB operational state.</Typography><Stack direction="row" spacing={1}><Button variant="outlined" type="button" onClick={test}>Test connection</Button><Button variant="contained" type="submit" startIcon={<StorageOutlined />}>Migrate and activate</Button></Stack></Stack>}</CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 5 }}><Stack spacing={3}><Card><CardContent><Typography variant="h5">Portable export and import</Typography><Typography
          sx={{
            color: "text.secondary",
            mb: 2
          }}>Moves operational CMDB data between installations. It is checksum-protected, but it is not a PostgreSQL backup or point-in-time restore.</Typography><Stack spacing={1.5}><Button variant="contained" startIcon={<BackupOutlined />} onClick={downloadBackup}>Download portable export</Button><Button color="warning" variant="outlined" startIcon={<RestoreOutlined />} onClick={() => setRestoreOpen(true)}>Validate and import export</Button>{backup && <Typography variant="caption" sx={{
          color: "text.secondary"
        }}>A versioned portable export was prepared during this session.</Typography>}</Stack></CardContent></Card><Card><CardContent><Typography variant="h5">PostgreSQL recovery</Typography><Typography sx={{
          color: "text.secondary"
        }}>Use Azure Database for PostgreSQL point-in-time restore in production, or scheduled <code>pg_dump</code>/<code>pg_restore</code> outside the web process. Retain and test those backups independently from portable exports.</Typography></CardContent></Card></Stack></Grid>
      </Grid>}
      <Dialog open={restoreOpen} onClose={() => setRestoreOpen(false)} maxWidth="sm" fullWidth><DialogTitle>Import portable CMDB export</DialogTitle><DialogContent><Alert severity="info" sx={{ mb: 2 }}>This adds or updates operational records. It does not delete records absent from the file and does not replace PostgreSQL recovery.</Alert><Button component="label" variant="outlined" fullWidth>{restoreFileName || 'Choose portable CMDB export'}<input hidden type="file" accept="application/json,.json" onChange={event => void chooseRestoreFile(event.target.files?.[0])} /></Button>{restorePreview && <Paper variant="outlined" sx={{ mt: 2, p: 2 }}><Typography sx={{
        fontWeight: 800
      }}>Validated version {restorePreview.version}</Typography><Typography variant="body2" sx={{
        color: "text.secondary"
      }}>{restorePreview.companies} customers · {restorePreview.users} users · {restorePreview.assets} CIs · {restorePreview.relationships} relationships · {restorePreview.changes} changes</Typography></Paper>}<FormControlLabel sx={{ mt: 2 }} control={<Checkbox checked={restoreConfirmed} onChange={(_, checked) => setRestoreConfirmed(checked)} />} label="I understand this is an import/merge and have downloaded a fresh export first." /></DialogContent><DialogActions><Button onClick={() => setRestoreOpen(false)}>Cancel</Button><Button color="warning" variant="contained" disabled={!restoreDocument || !restorePreview || !restoreConfirmed} onClick={restore}>Import operational data</Button></DialogActions></Dialog>
    </RootGuard>
  );
}

function documentRef() { return window.document; }
