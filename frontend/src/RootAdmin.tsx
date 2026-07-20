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
import BlockOutlined from '@mui/icons-material/BlockOutlined';
import ContentCopyOutlined from '@mui/icons-material/ContentCopyOutlined';
import KeyOutlined from '@mui/icons-material/KeyOutlined';
import EmailOutlined from '@mui/icons-material/EmailOutlined';
import SendOutlined from '@mui/icons-material/SendOutlined';
import DownloadOutlined from '@mui/icons-material/DownloadOutlined';
import {
  Alert, Box, Button, Card, CardContent, Checkbox, Chip, CircularProgress, Dialog, DialogActions, DialogContent,
  DialogTitle, Divider, FormControl, FormControlLabel, Grid, InputLabel, List, ListItem, ListItemIcon, ListItemText,
  MenuItem, Paper, Select, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Title, useNotify } from 'react-admin';
import { Navigate } from 'react-router-dom';
import { apiDownload, apiFetch, getSession } from './session';
import type { AccessGroup, AccessGroupImpact, ApiToken, Company, Integration, RoleTemplate, SyncRun, User } from './types';
import { useWorkspace } from './workspace';
import { DEFAULT_MSP_BRAND, useMspBranding, type Brand } from './branding';
import { CustomerScopeSelector } from './CustomerScopeSelector';

type Notice = { severity: 'success' | 'error' | 'info' | 'warning'; message: string } | null;
type DatabaseStatus = { configured: boolean; available: boolean; mode: string; source: string; managedByEnvironment?: boolean; uiConfigPersistenceReady?: boolean; error?: string | null; diagnosticError?: string | null; settings: DatabaseSettings; database?: string; databaseUser?: string; server?: string; schemaVersion?: string | null; expectedSchemaVersion?: string; migrationsPending?: boolean; initialized?: boolean; schemaState?: string; canCreateSchemaObjects?: boolean };
type DatabaseSettings = { host?: string; port?: number | string; database?: string; username?: string; password?: string; sslmode?: string; seedMode?: 'current' | 'empty' | 'demo' };
type RestorePreview = { valid: boolean; version: number; createdAt?: string; companies: number; users: number; assets: number; relationships: number; changes: number; warning: string };
type EffectiveAccess = { user: User; role: RoleTemplate; customers: string[]; scope: string };
type EmailConnection = {
  id: string; enabled: boolean; authMode: 'managed_identity' | 'client_secret' | 'certificate';
  tenantId: string; clientId: string; managedIdentityClientId: string; senderAddress: string;
  senderName: string; replyTo: string; status: string; lastTestAt?: string | null;
  lastError?: string; revision: number; hasClientSecret: boolean; certificateConfigured: boolean;
};
type EmailOutboxItem = {
  id: string; to: string[]; subject: string; status: string; attempts: number;
  createdAt: string; acceptedAt?: string | null; lastError?: string;
};

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
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('current');
  const [editing, setEditing] = useState<User | null>(null);
  const [editEmail, setEditEmail] = useState('');
  const [editName, setEditName] = useState('');
  const [editRole, setEditRole] = useState<User['role']>('client_reader');
  const [editCompanyId, setEditCompanyId] = useState('');
  const [editCompanyIds, setEditCompanyIds] = useState<string[]>([]);
  const [editGroupId, setEditGroupId] = useState('');
  const [editApiEnabled, setEditApiEnabled] = useState(false);
  const [editMfaRequired, setEditMfaRequired] = useState(false);
  const [mfaResetUser, setMfaResetUser] = useState<User | null>(null);
  const [mfaResetReason, setMfaResetReason] = useState('Lost or replaced authenticator device');
  const [mfaResetTicket, setMfaResetTicket] = useState('');
  const [mfaResetConfirmation, setMfaResetConfirmation] = useState('');
  const [mfaResetAdminPassword, setMfaResetAdminPassword] = useState('');
  const [mfaResetAdminCode, setMfaResetAdminCode] = useState('');
  const [passwordUser, setPasswordUser] = useState<User | null>(null);
  const [newPassword, setNewPassword] = useState('');
  const [tokenUser, setTokenUser] = useState<User | null>(null);
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [tokenName, setTokenName] = useState('');
  const [tokenDays, setTokenDays] = useState(90);
  const [tokenScopes, setTokenScopes] = useState<string[]>(['cmdb:read']);
  const [tokenCompanyIds, setTokenCompanyIds] = useState<string[]>([]);
  const [revealedToken, setRevealedToken] = useState('');

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
  const createAccessGroup = groups.find(group => group.id === groupId);
  const editAccessGroup = groups.find(group => group.id === editGroupId);
  const dateLabel = (value?: string | null) => value ? new Date(value).toLocaleString() : 'Never';
  const visibleUsers = users.filter(user => {
    const matchesSearch = `${user.displayName} ${user.email} ${user.role}`.toLowerCase().includes(search.toLowerCase());
    const matchesStatus = statusFilter === 'all' || (statusFilter === 'current'
      ? user.status !== 'archived'
      : user.status === statusFilter);
    return matchesSearch && matchesStatus;
  });

  function openEdit(user: User) {
    setEditing(user);
    setEditEmail(user.email);
    setEditName(user.displayName || user.email.split('@')[0]);
    setEditRole(user.role);
    setEditCompanyId(user.directCompanyIds?.[0] || user.companyIds[0] || '');
    setEditCompanyIds(user.directCompanyIds?.filter(id => id !== '*') || []);
    setEditGroupId(user.groupIds?.[0] || '');
    setEditApiEnabled(Boolean(user.apiAccessEnabled));
    setEditMfaRequired(Boolean(user.mfaRequired));
  }

  async function saveUser(event: FormEvent) {
    event.preventDefault();
    if (!editing) return;
    try {
      await apiFetch<User>(`/api/users/${editing.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          email: editEmail,
          displayName: editName,
          role: editRole,
          companyId: editRole === 'client_reader' ? editCompanyId : undefined,
          companyIds: editRole === 'msp_operator' ? editCompanyIds : [],
          groupIds: editRole === 'msp_operator' && editGroupId ? [editGroupId] : [],
          apiAccessEnabled: editApiEnabled,
          mfaRequired: editMfaRequired,
          reason: 'Updated from user management',
        }),
      });
      setEditing(null);
      setNotice({ severity: 'success', message: 'User profile and access updated.' });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'User update failed.' });
    }
  }

  async function changeStatus(user: User) {
    const status = user.status === 'active' ? 'disabled' : 'active';
    const verb = status === 'disabled' ? 'Disable' : 'Enable';
    if (!window.confirm(`${verb} ${user.email}?${status === 'disabled' ? ' Active sessions and API tokens will be revoked.' : ''}`)) return;
    try {
      await apiFetch<User>(`/api/users/${user.id}/status`, {
        method: 'POST',
        body: JSON.stringify({ status, reason: `${verb}d from user management` }),
      });
      setNotice({ severity: 'success', message: `${user.email} ${status === 'active' ? 'enabled' : 'disabled'}.` });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Status update failed.' });
    }
  }

  async function archive(user: User) {
    if (!window.confirm(`Archive ${user.email}? Their historical audit and ownership references will be retained.`)) return;
    try {
      await apiFetch(`/api/users/${user.id}`, { method: 'DELETE' });
      setNotice({ severity: 'success', message: `${user.email} archived.` });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'User could not be archived.' });
    }
  }

  async function resetPassword(event: FormEvent) {
    event.preventDefault();
    if (!passwordUser) return;
    try {
      await apiFetch(`/api/users/${passwordUser.id}/password`, {
        method: 'PUT',
        body: JSON.stringify({ password: newPassword }),
      });
      setPasswordUser(null);
      setNewPassword('');
      setNotice({ severity: 'success', message: 'Local password changed and active sessions revoked.' });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Password reset failed.' });
    }
  }

  async function resetMfa(event: FormEvent) {
    event.preventDefault();
    if (!mfaResetUser) return;
    try {
      await apiFetch(`/api/users/${mfaResetUser.id}/mfa/reset`, {
        method: 'POST', body: JSON.stringify({
          reason: mfaResetReason,
          ticketReference: mfaResetTicket,
          confirmation: mfaResetConfirmation,
          administratorPassword: mfaResetAdminPassword,
          administratorCode: mfaResetAdminCode,
        }),
      });
      setNotice({ severity: 'success', message: `Authenticator reset for ${mfaResetUser.email}. Active sessions were revoked and enrollment will be required at the next sign-in when policy requires it.` });
      closeMfaReset();
      await load();
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Authenticator reset failed.' }); }
  }

  function openMfaReset(user: User) {
    setMfaResetUser(user);
    setMfaResetReason('Lost or replaced authenticator device');
    setMfaResetTicket('');
    setMfaResetConfirmation('');
    setMfaResetAdminPassword('');
    setMfaResetAdminCode('');
  }

  function closeMfaReset() {
    setMfaResetUser(null);
    setMfaResetReason('Lost or replaced authenticator device');
    setMfaResetTicket('');
    setMfaResetConfirmation('');
    setMfaResetAdminPassword('');
    setMfaResetAdminCode('');
  }

  async function openTokens(user: User) {
    setTokenUser(user);
    setRevealedToken('');
    setTokenName('');
    setTokenCompanyIds([]);
    try {
      setTokens(await apiFetch<ApiToken[]>(`/api/users/${user.id}/tokens`));
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'API tokens could not be loaded.' });
    }
  }

  async function createToken(event: FormEvent) {
    event.preventDefault();
    if (!tokenUser) return;
    try {
      const created = await apiFetch<ApiToken>(`/api/users/${tokenUser.id}/tokens`, {
        method: 'POST',
        body: JSON.stringify({
          name: tokenName,
          scopes: tokenScopes,
          companyIds: tokenCompanyIds,
          expiresInDays: tokenDays,
        }),
      });
      setRevealedToken(created.token || '');
      setTokenName('');
      setTokens(await apiFetch<ApiToken[]>(`/api/users/${tokenUser.id}/tokens`));
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'API token could not be created.' });
    }
  }

  async function revokeToken(token: ApiToken) {
    if (!tokenUser || !window.confirm(`Revoke ${token.name}? This cannot be undone.`)) return;
    try {
      await apiFetch(`/api/users/${tokenUser.id}/tokens/${token.id}`, { method: 'DELETE' });
      setTokens(await apiFetch<ApiToken[]>(`/api/users/${tokenUser.id}/tokens`));
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'API token could not be revoked.' });
    }
  }

  return (
    <RootGuard><Title title="Users" /><PageHeading eyebrow="Identity lifecycle" title="User management" copy="Create, edit, secure and retire human or automation identities. Server-side role, tenant and token policy remains authoritative." action={<Chip label={`${users.filter(user => user.status === 'active').length} active`} color="success" variant="outlined" />} />
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}
      <Grid container spacing={3}>
        <Grid size={{ xs: 12, xl: 4 }}><Card><CardContent><Stack component="form" spacing={2} onSubmit={createUser}>
          <Typography variant="h5">Add user</Typography>
          {actor?.role === 'platform_admin' && <FormControl><InputLabel>Account type</InputLabel><Select label="Account type" value={accountType} onChange={event => setAccountType(event.target.value as 'root' | 'customer')}>
            <MenuItem value="root">MSP user</MenuItem><MenuItem value="customer">Customer user</MenuItem>
          </Select></FormControl>}
          <TextField label="Email" type="email" value={email} onChange={event => setEmail(event.target.value)} required />
          <TextField label="Temporary password" type="password" value={password} onChange={event => setPassword(event.target.value)} slotProps={{ htmlInput: { minLength: 12 } }} helperText="At least 12 characters; local authentication only" required />
          {accountType === 'root' ? <>
            <FormControl><InputLabel>MSP access group</InputLabel><Select label="MSP access group" value={groupId} onChange={event => { const nextGroupId = event.target.value; const inherited = new Set(groups.find(group => group.id === nextGroupId)?.companyIds || []); setGroupId(nextGroupId); setCompanyIds(items => items.filter(id => !inherited.has(id))); }}><MenuItem value="">No access group</MenuItem>{groups.map(group => <MenuItem key={group.id} value={group.id}>{group.name} ({group.companyIds.length} customers)</MenuItem>)}</Select></FormControl>
            <CustomerScopeSelector companies={workspace.companies} directIds={companyIds} inheritedIds={createAccessGroup?.companyIds || []} inheritedLabel={createAccessGroup?.name || 'No access group'} onDirectIdsChange={setCompanyIds} />
          </> : <FormControl><InputLabel>Customer</InputLabel><Select label="Customer" value={companyId} onChange={event => setCompanyId(event.target.value)} required>{workspace.companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}</Select></FormControl>}
          <Button variant="contained" type="submit" startIcon={<PersonAddAltOutlined />}>Create user</Button>
        </Stack></CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 8 }}><Card><CardContent>
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', mb: 2 }}>
            <Box><Typography variant="h5">Identity directory</Typography><Typography sx={{ color: 'text.secondary' }}>Disabled and archived accounts remain visible for governance.</Typography></Box>
            <Stack direction="row" spacing={1}><TextField size="small" label="Search users" value={search} onChange={event => setSearch(event.target.value)} /><FormControl size="small" sx={{ minWidth: 130 }}><InputLabel>Status</InputLabel><Select label="Status" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><MenuItem value="current">Current</MenuItem><MenuItem value="active">Active</MenuItem><MenuItem value="disabled">Disabled</MenuItem><MenuItem value="archived">Archived</MenuItem><MenuItem value="all">All</MenuItem></Select></FormControl></Stack>
          </Stack>
          {loading ? <CircularProgress size={24} /> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>User</TableCell><TableCell>Status</TableCell><TableCell>Access</TableCell><TableCell>MFA</TableCell><TableCell>API</TableCell><TableCell>Activity</TableCell><TableCell align="right">Actions</TableCell></TableRow></TableHead><TableBody>{visibleUsers.map(user => <TableRow key={user.id} hover sx={{ opacity: user.status === 'archived' ? 0.62 : 1 }}>
            <TableCell><Typography sx={{ fontWeight: 750 }}>{user.displayName || user.email}</Typography><Typography variant="caption" color="text.secondary">{user.email} · {user.authSource || 'local'}</Typography></TableCell>
            <TableCell><Chip size="small" label={user.status} color={user.status === 'active' ? 'success' : user.status === 'disabled' ? 'warning' : 'default'} /></TableCell>
            <TableCell><Typography variant="body2">{user.role.replaceAll('_', ' ')}</Typography><Typography variant="caption" color="text.secondary">{user.role === 'platform_admin' ? 'All customers' : user.companyIds.map(id => names[id] || id).join(', ') || 'No access'}</Typography></TableCell>
            <TableCell><Chip size="small" variant="outlined" color={user.mfaEnabled ? 'success' : user.mfaRequired ? 'warning' : 'default'} label={user.mfaEnabled ? `Enabled · ${user.mfaRecoveryCodesRemaining || 0} recovery` : user.mfaRequired ? 'Setup required' : 'Optional'} /></TableCell>
            <TableCell><Chip size="small" variant="outlined" color={user.apiAccessEnabled ? 'primary' : 'default'} label={user.apiAccessEnabled ? `${user.apiTokenCount || 0} token${user.apiTokenCount === 1 ? '' : 's'}` : 'Disabled'} /></TableCell>
            <TableCell><Typography variant="caption">Login: {dateLabel(user.lastLoginAt)}</Typography><Typography variant="caption" sx={{ display: 'block' }}>API: {dateLabel(user.lastApiUsedAt)}</Typography></TableCell>
            <TableCell align="right"><Stack direction="row" spacing={0.5} sx={{ justifyContent: 'flex-end', flexWrap: 'wrap' }}><Button size="small" startIcon={<EditOutlined />} onClick={() => openEdit(user)} disabled={user.status === 'archived'}>Edit</Button><Button size="small" startIcon={<KeyOutlined />} onClick={() => void openTokens(user)} disabled={user.status === 'archived'}>API</Button><Button size="small" onClick={() => setPasswordUser(user)} disabled={user.authSource === 'entra' || user.status === 'archived'}>Password</Button><Box component="span" title={actor?.role !== 'platform_admin' ? 'Only platform administrators can reset MFA' : user.authSource === 'entra' ? 'MFA is managed by Microsoft Entra ID' : !user.mfaEnabled ? 'No authenticator is currently enrolled' : 'Remove the authenticator and recovery codes'}><Button size="small" color="warning" onClick={() => openMfaReset(user)} disabled={actor?.role !== 'platform_admin' || user.authSource === 'entra' || !user.mfaEnabled || user.status === 'archived'}>Reset MFA</Button></Box><Button size="small" color={user.status === 'active' ? 'warning' : 'success'} onClick={() => void changeStatus(user)} disabled={user.status === 'archived' || user.id === actor?.id}>{user.status === 'active' ? 'Disable' : 'Enable'}</Button><Button size="small" color="error" startIcon={<DeleteOutlined />} onClick={() => void archive(user)} disabled={user.status === 'archived' || user.id === actor?.id}>Archive</Button></Stack></TableCell>
          </TableRow>)}</TableBody></Table></TableContainer>}
        </CardContent></Card></Grid>
      </Grid>
      <Dialog open={Boolean(editing)} onClose={() => setEditing(null)} fullWidth maxWidth="sm">
        <Stack component="form" onSubmit={saveUser}>
          <DialogTitle>Edit user and access</DialogTitle>
          <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
            <TextField label="Display name" value={editName} onChange={event => setEditName(event.target.value)} required />
            <TextField label="Email" type="email" value={editEmail} onChange={event => setEditEmail(event.target.value)} required />
            <FormControl><InputLabel>Role</InputLabel><Select label="Role" value={editRole} onChange={event => setEditRole(event.target.value as User['role'])} disabled={actor?.role !== 'platform_admin'}>
              <MenuItem value="platform_admin">Platform administrator</MenuItem><MenuItem value="msp_operator">MSP operator</MenuItem><MenuItem value="client_reader">Customer user</MenuItem>
            </Select></FormControl>
            {editRole === 'client_reader' && <FormControl><InputLabel>Customer</InputLabel><Select label="Customer" value={editCompanyId} onChange={event => setEditCompanyId(event.target.value)} required>{workspace.companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}</Select></FormControl>}
            {editRole === 'msp_operator' && <><FormControl><InputLabel>MSP access group</InputLabel><Select label="MSP access group" value={editGroupId} onChange={event => { const nextGroupId = event.target.value; const inherited = new Set(groups.find(group => group.id === nextGroupId)?.companyIds || []); setEditGroupId(nextGroupId); setEditCompanyIds(items => items.filter(id => !inherited.has(id))); }}><MenuItem value="">No access group</MenuItem>{groups.map(group => <MenuItem key={group.id} value={group.id}>{group.name}</MenuItem>)}</Select></FormControl><CustomerScopeSelector companies={workspace.companies} directIds={editCompanyIds} inheritedIds={editAccessGroup?.companyIds || []} inheritedLabel={editAccessGroup?.name || 'No access group'} onDirectIdsChange={setEditCompanyIds} /></>}
            <FormControlLabel control={<Checkbox checked={editApiEnabled} onChange={(_, checked) => setEditApiEnabled(checked)} />} label="Allow personal API tokens" />
            <FormControlLabel control={<Checkbox checked={editMfaRequired} onChange={(_, checked) => setEditMfaRequired(checked)} disabled={editing?.authSource === 'entra'} />} label="Require TOTP MFA at local sign-in" />
            <Alert severity="info">Role and customer scope changes take effect immediately. Turning API access off revokes every active token for this user.</Alert>
          </Stack></DialogContent>
          <DialogActions><Button onClick={() => setEditing(null)}>Cancel</Button><Button type="submit" variant="contained">Save changes</Button></DialogActions>
        </Stack>
      </Dialog>
      <Dialog open={Boolean(passwordUser)} onClose={() => setPasswordUser(null)} fullWidth maxWidth="xs">
        <Stack component="form" onSubmit={resetPassword}><DialogTitle>Change local password</DialogTitle><DialogContent><Stack spacing={2} sx={{ pt: 1 }}><Typography color="text.secondary">Set a new password for {passwordUser?.email}.</Typography><TextField autoFocus label="New password" type="password" value={newPassword} onChange={event => setNewPassword(event.target.value)} slotProps={{ htmlInput: { minLength: 12 } }} helperText="At least 12 characters" required /><Alert severity="warning">All active browser sessions for this user will be revoked.</Alert></Stack></DialogContent><DialogActions><Button onClick={() => setPasswordUser(null)}>Cancel</Button><Button type="submit" variant="contained">Change password</Button></DialogActions></Stack>
      </Dialog>
      <Dialog open={Boolean(mfaResetUser)} onClose={closeMfaReset} fullWidth maxWidth="sm">
        <Stack component="form" onSubmit={resetMfa}><DialogTitle>Reset authenticator</DialogTitle><DialogContent><Stack spacing={2} sx={{ pt: 1 }}><Alert severity="warning">This removes the enrolled authenticator and every recovery code for {mfaResetUser?.email}. All browser sessions will be revoked, while the account’s MFA-required policy remains unchanged.</Alert><TextField label="Reset reason" value={mfaResetReason} onChange={event => setMfaResetReason(event.target.value)} required slotProps={{ htmlInput: { minLength: 4, maxLength: 500 } }} /><TextField label="Ticket or change reference (optional)" value={mfaResetTicket} onChange={event => setMfaResetTicket(event.target.value)} slotProps={{ htmlInput: { maxLength: 120 } }} />{actor?.authSource === 'local' ? <><Divider>Administrator verification</Divider><TextField label="Your current password" type="password" value={mfaResetAdminPassword} onChange={event => setMfaResetAdminPassword(event.target.value)} required />{actor.mfaEnabled && <TextField label="Your authenticator or recovery code" value={mfaResetAdminCode} onChange={event => setMfaResetAdminCode(event.target.value)} required slotProps={{ htmlInput: { autoComplete: 'one-time-code', maxLength: 64 } }} />}</> : <Alert severity="info">Your Microsoft Entra session authorizes this administrative action. Conditional Access remains responsible for Entra step-up authentication.</Alert>}<Divider>Destructive action confirmation</Divider><TextField label={`Type ${mfaResetUser?.email || 'the user email'} to confirm`} value={mfaResetConfirmation} onChange={event => setMfaResetConfirmation(event.target.value)} required autoComplete="off" /></Stack></DialogContent><DialogActions><Button onClick={closeMfaReset}>Cancel</Button><Button type="submit" color="warning" variant="contained" disabled={mfaResetConfirmation.trim().toLowerCase() !== (mfaResetUser?.email || '').toLowerCase()}>Reset MFA and revoke sessions</Button></DialogActions></Stack>
      </Dialog>
      <Dialog open={Boolean(tokenUser)} onClose={() => setTokenUser(null)} fullWidth maxWidth="md">
        <DialogTitle>Personal API access · {tokenUser?.displayName || tokenUser?.email}</DialogTitle>
        <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
          {!tokenUser?.apiAccessEnabled && <Alert severity="warning" icon={<BlockOutlined />}>API access is disabled for this user. Enable it in Edit user before creating a token.</Alert>}
          {revealedToken && <Alert severity="success"><Typography sx={{ fontWeight: 750 }}>Copy this token now. It will not be shown again.</Typography><Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }}><TextField fullWidth value={revealedToken} slotProps={{ htmlInput: { readOnly: true } }} /><Button startIcon={<ContentCopyOutlined />} onClick={() => void navigator.clipboard.writeText(revealedToken)}>Copy</Button></Stack></Alert>}
          <Paper component="form" variant="outlined" onSubmit={createToken} sx={{ p: 2 }}><Stack spacing={2}><Typography variant="h6">Create token</Typography><Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}><TextField fullWidth label="Token name" value={tokenName} onChange={event => setTokenName(event.target.value)} placeholder="Automation or integration name" required /><TextField label="Expires in days" type="number" value={tokenDays} onChange={event => setTokenDays(Number(event.target.value))} slotProps={{ htmlInput: { min: 1, max: 365 } }} sx={{ width: { sm: 180 } }} required /></Stack><Stack direction="row" spacing={2}><FormControlLabel control={<Checkbox checked={tokenScopes.includes('cmdb:read')} onChange={(_, checked) => setTokenScopes(scopes => checked ? [...new Set([...scopes, 'cmdb:read'])] : scopes.filter(scope => scope !== 'cmdb:read'))} />} label="Read CMDB" /><FormControlLabel control={<Checkbox checked={tokenScopes.includes('cmdb:write')} onChange={(_, checked) => setTokenScopes(scopes => checked ? [...new Set([...scopes, 'cmdb:write'])] : scopes.filter(scope => scope !== 'cmdb:write'))} />} label="Write CMDB" /></Stack><Box><Typography variant="subtitle2">Restrict to customers (optional)</Typography><Typography variant="caption" color="text.secondary">No selection uses the user’s full effective customer scope.</Typography><Paper variant="outlined" sx={{ p: 1, mt: 1, maxHeight: 150, overflow: 'auto' }}>{workspace.companies.filter(company => tokenUser?.companyIds.includes('*') || tokenUser?.companyIds.includes(company.id)).map(company => <FormControlLabel key={company.id} control={<Checkbox checked={tokenCompanyIds.includes(company.id)} onChange={(_, checked) => setTokenCompanyIds(items => checked ? [...items, company.id] : items.filter(id => id !== company.id))} />} label={company.name} />)}</Paper></Box><Button type="submit" variant="contained" startIcon={<KeyOutlined />} disabled={!tokenUser?.apiAccessEnabled || tokenScopes.length === 0}>Create token</Button></Stack></Paper>
          <Box><Typography variant="h6" sx={{ mb: 1 }}>Issued tokens</Typography>{tokens.length === 0 ? <Typography color="text.secondary">No tokens have been issued.</Typography> : <TableContainer component={Paper} variant="outlined"><Table size="small"><TableHead><TableRow><TableCell>Name</TableCell><TableCell>Prefix</TableCell><TableCell>Scopes</TableCell><TableCell>Expires</TableCell><TableCell>Last used</TableCell><TableCell>Status</TableCell><TableCell align="right">Action</TableCell></TableRow></TableHead><TableBody>{tokens.map(token => <TableRow key={token.id}><TableCell>{token.name}</TableCell><TableCell><code>{token.tokenPrefix}</code></TableCell><TableCell>{token.scopes.join(', ')}</TableCell><TableCell>{dateLabel(token.expiresAt)}</TableCell><TableCell>{dateLabel(token.lastUsedAt)}</TableCell><TableCell><Chip size="small" label={token.revokedAt ? 'Revoked' : 'Active'} color={token.revokedAt ? 'default' : 'success'} /></TableCell><TableCell align="right"><Button size="small" color="error" onClick={() => void revokeToken(token)} disabled={Boolean(token.revokedAt)}>Revoke</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer>}</Box>
        </Stack></DialogContent><DialogActions><Button onClick={() => setTokenUser(null)}>Close</Button></DialogActions>
      </Dialog>
    </RootGuard>
  );
}

export function CustomerGroupsPage() {
  const workspace = useWorkspace();
  const actor = getSession()?.user;
  const [groups, setGroups] = useState<AccessGroup[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState('');
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<AccessGroup | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [ownerUserId, setOwnerUserId] = useState('');
  const [companyIds, setCompanyIds] = useState<string[]>([]);
  const [availableSearch, setAvailableSearch] = useState('');
  const [selectedSearch, setSelectedSearch] = useState('');
  const [deleteImpact, setDeleteImpact] = useState<AccessGroupImpact | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState('');
  const [notice, setNotice] = useState<Notice>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [groupData, userData] = await Promise.all([
        apiFetch<AccessGroup[]>('/api/access-groups'),
        apiFetch<User[]>('/api/users'),
      ]);
      setGroups(groupData);
      setUsers(userData);
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Customer groups could not be loaded.' });
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void load(); }, []);

  const ownerOptions = users.filter(user => user.status === 'active' && ['platform_admin', 'msp_operator'].includes(user.role));
  const companyName = (id: string) => workspace.companies.find(company => company.id === id)?.name || id;
  const normalizedAvailableSearch = availableSearch.trim().toLowerCase();
  const normalizedSelectedSearch = selectedSearch.trim().toLowerCase();
  const availableCompanies = workspace.companies.filter(company => !companyIds.includes(company.id) && company.name.toLowerCase().includes(normalizedAvailableSearch));
  const selectedCompanies = workspace.companies.filter(company => companyIds.includes(company.id) && company.name.toLowerCase().includes(normalizedSelectedSearch));
  const visibleGroups = groups.filter(group => {
    const haystack = [group.name, group.description, group.ownerLabel, ...group.companyIds.map(companyName)].join(' ').toLowerCase();
    return haystack.includes(query.trim().toLowerCase());
  });

  function closeEditor() {
    setEditorOpen(false);
    setEditing(null);
    setName('');
    setDescription('');
    setOwnerUserId('');
    setCompanyIds([]);
    setAvailableSearch('');
    setSelectedSearch('');
  }

  function createGroup() {
    setEditing(null);
    setName('');
    setDescription('');
    setOwnerUserId(actor?.id || '');
    setCompanyIds([]);
    setAvailableSearch('');
    setSelectedSearch('');
    setEditorOpen(true);
  }

  function edit(group: AccessGroup) {
    setEditing(group);
    setName(group.name);
    setDescription(group.description || '');
    setOwnerUserId(group.ownerUserId || actor?.id || '');
    setCompanyIds(group.companyIds);
    setAvailableSearch('');
    setSelectedSearch('');
    setEditorOpen(true);
  }

  async function save(event: FormEvent) {
    event.preventDefault(); setNotice(null);
    try {
      await apiFetch(editing ? '/api/access-groups/' + editing.id : '/api/access-groups', {
        method: editing ? 'PUT' : 'POST',
        body: JSON.stringify({
          name,
          description,
          companyIds,
          ownerUserId,
          membershipMode: 'manual',
          membershipRules: {},
          expectedRevision: editing?.revision,
        }),
      });
      setNotice({ severity: 'success', message: editing ? 'Customer group updated.' : 'Customer group created.' });
      closeEditor();
      await load();
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Group could not be saved.' }); }
  }

  async function reviewDelete(group: AccessGroup) {
    setNotice(null);
    try {
      setDeleteImpact(await apiFetch<AccessGroupImpact>('/api/access-groups/' + group.id + '/impact'));
      setDeleteConfirmation('');
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Deletion impact could not be calculated.' });
    }
  }

  async function remove() {
    if (!deleteImpact || deleteConfirmation !== deleteImpact.groupName) return;
    try {
      await apiFetch('/api/access-groups/' + deleteImpact.groupId, { method: 'DELETE' });
      setNotice({ severity: 'success', message: 'Customer group deleted after impact review.' });
      setDeleteImpact(null);
      setDeleteConfirmation('');
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Group could not be deleted.' });
    }
  }

  const membershipSummary = (group: AccessGroup) => {
    if (group.system) return 'Every managed customer';
    const names = group.companyIds.slice(0, 3).map(companyName);
    const remainder = group.companyIds.length - names.length;
    return names.join(', ') + (remainder > 0 ? ' +' + remainder + ' more' : '');
  };

  return (
    <RootGuard adminOnly><Title title="Customer groups" /><PageHeading eyebrow="Organisation" title="Customer groups" copy="Build governed, reusable customer scopes without losing sight of their owners, users or downstream access impact." action={<Button variant="contained" startIcon={<GroupsOutlined />} onClick={createGroup}>Create group</Button>} />
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}
      <Card><CardContent>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'center' }, mb: 2 }}><Box><Typography variant="h5">Group directory</Typography><Typography color="text.secondary">Search and review membership, ownership and usage before making a change.</Typography></Box><TextField size="small" label="Search groups or customers" value={query} onChange={event => setQuery(event.target.value)} sx={{ minWidth: { md: 300 } }} /></Stack>
        {loading ? <CircularProgress size={24} /> : <TableContainer><Table><TableHead><TableRow><TableCell>Group</TableCell><TableCell>Type</TableCell><TableCell>Customer scope</TableCell><TableCell>Assigned users</TableCell><TableCell>Owner</TableCell><TableCell>Updated</TableCell><TableCell align="right">Actions</TableCell></TableRow></TableHead><TableBody>{visibleGroups.map(group => <TableRow key={group.id} hover>
          <TableCell><Typography sx={{ fontWeight: 750 }}>{group.name}</Typography><Typography variant="caption" color="text.secondary">{group.description || 'No description provided'}</Typography></TableCell>
          <TableCell><Chip size="small" label={group.membershipMode === 'dynamic' ? 'Dynamic' : 'Manual'} color={group.membershipMode === 'dynamic' ? 'primary' : 'default'} variant="outlined" /></TableCell>
          <TableCell><Typography variant="body2">{membershipSummary(group)}</Typography><Typography variant="caption" color="text.secondary">{group.companyIds.length} customers</Typography></TableCell>
          <TableCell><Chip size="small" label={group.assignedUserCount + ' users'} color={group.assignedUserCount ? 'success' : 'default'} variant="outlined" /></TableCell>
          <TableCell>{group.ownerLabel}</TableCell><TableCell>{group.updatedAt ? new Date(group.updatedAt).toLocaleDateString() : 'Seeded'}</TableCell>
          <TableCell align="right">{group.system ? <Typography variant="caption" color="text.secondary">System managed</Typography> : <Stack direction="row" spacing={0.5} sx={{ justifyContent: 'flex-end' }}><Button size="small" startIcon={<EditOutlined />} onClick={() => edit(group)}>Edit</Button><Button size="small" color="error" startIcon={<DeleteOutlined />} onClick={() => void reviewDelete(group)}>Delete</Button></Stack>}</TableCell>
        </TableRow>)}</TableBody></Table></TableContainer>}
      </CardContent></Card>

      <Dialog open={editorOpen} onClose={closeEditor} fullWidth maxWidth="lg"><Stack component="form" onSubmit={save}><DialogTitle>{editing ? 'Edit customer group' : 'Create customer group'}</DialogTitle><DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
        <Grid container spacing={2}><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Group name" value={name} onChange={event => setName(event.target.value)} required /></Grid><Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth><InputLabel>Accountable owner</InputLabel><Select label="Accountable owner" value={ownerUserId} onChange={event => setOwnerUserId(event.target.value)} required>{ownerOptions.map(user => <MenuItem key={user.id} value={user.id}>{user.displayName || user.email} — {user.role.replaceAll('_', ' ')}</MenuItem>)}</Select></FormControl></Grid></Grid>
        <TextField label="Purpose and usage" value={description} onChange={event => setDescription(event.target.value)} multiline minRows={2} helperText="Explain why this scope exists and which MSP team should use it." />
        <Alert severity="info"><Typography sx={{ fontWeight: 750 }}>Manual membership</Typography>Rule-based dynamic groups are prepared in the data model. They will be enabled later with reviewed customer tags and rules.</Alert>
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Stack spacing={1.5}><Stack direction="row" spacing={1} sx={{ justifyContent: 'space-between', alignItems: 'center' }}><Box><Typography variant="h6">Available customers</Typography><Typography variant="caption" color="text.secondary">{availableCompanies.length} matching</Typography></Box><Button size="small" onClick={() => setCompanyIds(items => [...new Set([...items, ...availableCompanies.map(company => company.id)])])} disabled={!availableCompanies.length}>Add all filtered</Button></Stack><TextField size="small" label="Search available customers" value={availableSearch} onChange={event => setAvailableSearch(event.target.value)} /><Divider /><Stack divider={<Divider flexItem />} sx={{ maxHeight: 300, overflow: 'auto' }}>{availableCompanies.map(company => <Stack key={company.id} direction="row" spacing={1} sx={{ justifyContent: 'space-between', alignItems: 'center', py: 1 }}><Box><Typography>{company.name}</Typography><Typography variant="caption" color="text.secondary">{company.id}</Typography></Box><Button size="small" onClick={() => setCompanyIds(items => [...items, company.id])}>Add</Button></Stack>)}{!availableCompanies.length && <Typography color="text.secondary" sx={{ py: 2 }}>No available customers match this search.</Typography>}</Stack></Stack></Paper></Grid>
          <Grid size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Stack spacing={1.5}><Stack direction="row" spacing={1} sx={{ justifyContent: 'space-between', alignItems: 'center' }}><Box><Typography variant="h6">Customers in group</Typography><Typography variant="caption" color="text.secondary">{companyIds.length} selected</Typography></Box><Button size="small" color="warning" onClick={() => setCompanyIds(items => items.filter(id => !selectedCompanies.some(company => company.id === id)))} disabled={!selectedCompanies.length}>Remove filtered</Button></Stack><TextField size="small" label="Search selected customers" value={selectedSearch} onChange={event => setSelectedSearch(event.target.value)} /><Divider /><Stack divider={<Divider flexItem />} sx={{ maxHeight: 300, overflow: 'auto' }}>{selectedCompanies.map(company => <Stack key={company.id} direction="row" spacing={1} sx={{ justifyContent: 'space-between', alignItems: 'center', py: 1 }}><Box><Typography>{company.name}</Typography><Typography variant="caption" color="text.secondary">{company.id}</Typography></Box><Button size="small" color="warning" onClick={() => setCompanyIds(items => items.filter(id => id !== company.id))}>Remove</Button></Stack>)}{!selectedCompanies.length && <Typography color="text.secondary" sx={{ py: 2 }}>Add customers from the available list.</Typography>}</Stack></Stack></Paper></Grid>
        </Grid>
      </Stack></DialogContent><DialogActions><Button onClick={closeEditor}>Cancel</Button><Button type="submit" variant="contained" disabled={!name.trim() || !ownerUserId || companyIds.length === 0}>{editing ? 'Save group' : 'Create group'}</Button></DialogActions></Stack></Dialog>

      <Dialog open={Boolean(deleteImpact)} onClose={() => setDeleteImpact(null)} fullWidth maxWidth="sm"><DialogTitle>Delete customer group?</DialogTitle><DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
        {deleteImpact && <><Alert severity={deleteImpact.usersLosingAccess ? 'warning' : 'info'}><Typography sx={{ fontWeight: 750 }}>{deleteImpact.usersLosingAccess ? deleteImpact.usersLosingAccess + ' users will lose customer access' : 'No users will lose effective customer access'}</Typography>{deleteImpact.lostCustomerAssignments} customer assignments will be removed across {deleteImpact.assignedUserCount} assigned users.</Alert>{deleteImpact.users.length > 0 && <Paper variant="outlined" sx={{ maxHeight: 240, overflow: 'auto' }}><List dense>{deleteImpact.users.map(user => <ListItem key={user.id} divider><ListItemText primary={user.displayName + ' · ' + user.email} secondary={user.lostCustomers.length ? 'Loses: ' + user.lostCustomers.join(', ') : 'Retains access through direct permissions or another group'} /></ListItem>)}</List></Paper>}<TextField label={'Type “' + deleteImpact.groupName + '” to confirm'} value={deleteConfirmation} onChange={event => setDeleteConfirmation(event.target.value)} fullWidth /></>}
      </Stack></DialogContent><DialogActions><Button onClick={() => setDeleteImpact(null)}>Cancel</Button><Button color="error" variant="contained" onClick={() => void remove()} disabled={!deleteImpact || deleteConfirmation !== deleteImpact.groupName}>Delete group</Button></DialogActions></Dialog>
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
    <RootGuard adminOnly><Title title="Access control" /><PageHeading eyebrow="Access governance" title="Role-based access control" copy="Review role capabilities, effective customer boundaries and the personal API policy. All permissions are enforced by the server." />
      {error ? <ErrorNotice error={error} /> : null}
      <Grid container spacing={2} sx={{ mb: 3 }}>{roles.map(role => <Grid key={role.id} size={{ xs: 12, lg: 4 }}><Card sx={{ height: '100%' }}><CardContent><SecurityOutlined color="primary" /><Typography variant="h6" sx={{ mt: 1 }}>{role.name}</Typography><Chip size="small" label={role.scope} sx={{ my: 1 }} /><List dense>{role.permissions.map(permission => <ListItem key={permission} disableGutters><ListItemIcon sx={{ minWidth: 30 }}><CheckCircleOutlined color="success" fontSize="small" /></ListItemIcon><ListItemText primary={permission} /></ListItem>)}</List></CardContent></Card></Grid>)}</Grid>
      <Grid container spacing={2}><Grid size={{ xs: 12, lg: 8 }}><Card sx={{ height: '100%' }}><CardContent><Typography variant="h5">Effective access preview</Typography><Typography
        sx={{
          color: "text.secondary",
          mb: 2
        }}>Select a user to see the final role and tenant boundary resolved by the API.</Typography><FormControl fullWidth sx={{ maxWidth: 520, mb: 2 }}><InputLabel>User</InputLabel><Select label="User" value={selected} onChange={event => setSelected(event.target.value)}>{users.map(user => <MenuItem key={user.id} value={user.id}>{user.email} — {user.role.replaceAll('_', ' ')}</MenuItem>)}</Select></FormControl>{effective && <Alert severity="info"><Typography sx={{
        fontWeight: 750
      }}>{effective.user.email} · {effective.role.name}</Typography><Typography variant="body2">Account: {effective.user.status} · Authentication: {effective.user.authSource || 'local'}</Typography><Typography variant="body2">Customer scope: {effective.scope}</Typography><Typography variant="body2">Personal API access: {effective.user.apiAccessEnabled ? `Enabled (${effective.user.apiTokenCount || 0} issued)` : 'Disabled'}</Typography></Alert>}</CardContent></Card></Grid>
      <Grid size={{ xs: 12, lg: 4 }}><Card sx={{ height: '100%' }}><CardContent><KeyOutlined color="primary" /><Typography variant="h5" sx={{ mt: 1 }}>Personal API policy</Typography><List dense><ListItem disableGutters><ListItemText primary="Explicit per-user enablement" secondary="Disabled by default and revoked automatically when an account is disabled." /></ListItem><ListItem disableGutters><ListItemText primary="Least-privilege scopes" secondary="Separate read and write scopes with optional customer restrictions." /></ListItem><ListItem disableGutters><ListItemText primary="Short-lived secrets" secondary="Maximum 365-day lifetime; raw tokens are displayed once and only hashes are stored." /></ListItem><ListItem disableGutters><ListItemText primary="No administration by token" secondary="Personal tokens cannot call users, RBAC, branding, database or integration administration APIs." /></ListItem></List></CardContent></Card></Grid></Grid>
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

export function EmailPage() {
  const [connection, setConnection] = useState<EmailConnection | null>(null);
  const [clientSecret, setClientSecret] = useState('');
  const [recipient, setRecipient] = useState(getSession()?.user.email || '');
  const [outbox, setOutbox] = useState<EmailOutboxItem[]>([]);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState('');
  const load = async () => {
    try {
      const [configured, messages] = await Promise.all([
        apiFetch<EmailConnection>('/api/email/config'),
        apiFetch<EmailOutboxItem[]>('/api/email/outbox?limit=50'),
      ]);
      setConnection(configured);
      setOutbox(messages);
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Email configuration could not be loaded.' });
    }
  };
  useEffect(() => { void load(); }, []);
  const update = (key: keyof EmailConnection, value: string | boolean) => setConnection(current => current ? { ...current, [key]: value } : current);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!connection) return;
    setBusy('save'); setNotice(null);
    try {
      const stored = await apiFetch<EmailConnection>('/api/email/config', {
        method: 'PUT',
        body: JSON.stringify({
          enabled: connection.enabled, authMode: connection.authMode, tenantId: connection.tenantId,
          clientId: connection.clientId, managedIdentityClientId: connection.managedIdentityClientId,
          senderAddress: connection.senderAddress, senderName: connection.senderName,
          replyTo: connection.replyTo, clientSecret, expectedRevision: connection.revision,
        }),
      });
      setConnection(stored); setClientSecret('');
      setNotice({ severity: 'success', message: 'Microsoft 365 email settings saved. Send a test to verify Graph and mailbox scope.' });
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Email settings could not be saved.' });
    } finally { setBusy(''); }
  }
  async function sendTest() {
    setBusy('test'); setNotice({ severity: 'info', message: 'Submitting a test message to Microsoft Graph…' });
    try {
      await apiFetch('/api/email/test', { method: 'POST', body: JSON.stringify({ recipient }) });
      setNotice({ severity: 'success', message: 'Microsoft Graph accepted the test message. Delivery can still be subject to Exchange processing and policy.' });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'The test message failed.' });
      await load();
    } finally { setBusy(''); }
  }
  async function retry(item: EmailOutboxItem) {
    setBusy(item.id); setNotice(null);
    try {
      await apiFetch(`/api/email/outbox/${item.id}/retry`, { method: 'POST' });
      setNotice({ severity: 'success', message: 'Microsoft Graph accepted the retried message.' });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Retry failed.' });
      await load();
    } finally { setBusy(''); }
  }
  async function downloadScript() {
    try {
      const result = await apiDownload('/api/email/exchange-rbac-script');
      const url = URL.createObjectURL(result.blob);
      const link = documentRef().createElement('a'); link.href = url; link.download = result.filename; link.click(); URL.revokeObjectURL(url);
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Setup script could not be generated.' });
    }
  }
  return (
    <RootGuard adminOnly><Title title="Microsoft 365 email" /><PageHeading eyebrow="Operations" title="Email delivery" copy="Configure one MSP-wide Microsoft Graph sender for platform notifications. Credentials are write-only, delivery is audited, and messages enter a durable outbox before sending." action={connection && <Chip label={connection.status.replaceAll('_', ' ')} color={connection.status === 'verified' ? 'success' : connection.status === 'error' ? 'error' : 'warning'} />} />
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}
      {!connection ? <LoadingCard /> : <Grid container spacing={3}>
        <Grid size={{ xs: 12, xl: 7 }}><Card><CardContent><Stack component="form" spacing={2.5} onSubmit={save}>
          <Box><Typography variant="h5">Microsoft Graph connection</Typography><Typography color="text.secondary">Managed identity is recommended in Azure. Certificate credentials suit portable Docker deployments; client secrets are available for simpler initial setup.</Typography></Box>
          <FormControlLabel control={<Checkbox checked={connection.enabled} onChange={(_, checked) => update('enabled', checked)} />} label="Enable outbound platform email" />
          <FormControl fullWidth><InputLabel>Authentication</InputLabel><Select label="Authentication" value={connection.authMode} onChange={event => update('authMode', event.target.value)}><MenuItem value="managed_identity">Azure managed identity</MenuItem><MenuItem value="certificate">Application certificate</MenuItem><MenuItem value="client_secret">Application client secret</MenuItem></Select></FormControl>
          {connection.authMode !== 'managed_identity' && <Grid container spacing={2}><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Tenant ID" value={connection.tenantId} onChange={event => update('tenantId', event.target.value)} required /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Application client ID" value={connection.clientId} onChange={event => update('clientId', event.target.value)} required /></Grid></Grid>}
          {connection.authMode === 'managed_identity' && <TextField fullWidth label="User-assigned managed identity client ID (optional)" value={connection.managedIdentityClientId} onChange={event => update('managedIdentityClientId', event.target.value)} helperText="Leave blank to use the system-assigned identity." />}
          {connection.authMode === 'client_secret' && <TextField fullWidth type="password" label={connection.hasClientSecret ? 'Replace client secret (optional)' : 'Client secret'} value={clientSecret} onChange={event => setClientSecret(event.target.value)} helperText={connection.hasClientSecret ? 'A secret is stored encrypted. Leave blank to retain it.' : 'Stored encrypted using the application secret key.'} required={!connection.hasClientSecret} />}
          {connection.authMode === 'certificate' && <Alert severity={connection.certificateConfigured ? 'success' : 'warning'}>Certificate material is never uploaded through the browser. Mount it into the container and configure EMAIL_CERTIFICATE_PATH plus EMAIL_CERTIFICATE_PASSWORD_FILE. {connection.certificateConfigured ? 'A certificate path is present on this host.' : 'This host has no certificate path configured yet.'}</Alert>}
          <Grid container spacing={2}><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Sender mailbox" type="email" value={connection.senderAddress} onChange={event => update('senderAddress', event.target.value)} required={connection.enabled} /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Sender display name" value={connection.senderName} onChange={event => update('senderName', event.target.value)} /></Grid><Grid size={{ xs: 12 }}><TextField fullWidth label="Reply-to address (optional)" type="email" value={connection.replyTo} onChange={event => update('replyTo', event.target.value)} /></Grid></Grid>
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}><Button type="submit" variant="contained" startIcon={<EmailOutlined />} disabled={Boolean(busy)}>{busy === 'save' ? 'Saving…' : 'Save settings'}</Button><Button type="button" variant="outlined" startIcon={<DownloadOutlined />} onClick={downloadScript}>Exchange RBAC script</Button></Stack>
        </Stack></CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 5 }}><Stack spacing={3}><Card><CardContent><Typography variant="h5">Send verification message</Typography><Typography color="text.secondary" sx={{ mb: 2 }}>This creates an audited outbox item and submits it once. A 202 response confirms provider acceptance, not final mailbox delivery.</Typography><TextField fullWidth type="email" label="Test recipient" value={recipient} onChange={event => setRecipient(event.target.value)} /><Button fullWidth sx={{ mt: 2 }} variant="contained" startIcon={<SendOutlined />} disabled={Boolean(busy) || !connection.enabled || !recipient} onClick={sendTest}>{busy === 'test' ? 'Sending…' : 'Send test email'}</Button>{connection.lastTestAt && <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.5 }}>Last accepted {new Date(connection.lastTestAt).toLocaleString()}</Typography>}</CardContent></Card><Card><CardContent><Typography variant="h5">Least-privilege setup</Typography><List dense><ListItem disableGutters><ListItemText primary="Application Mail.Send" secondary="Use Exchange Online Application RBAC to scope the app to the configured sender mailbox." /></ListItem><ListItem disableGutters><ListItemText primary="Do not add an unscoped duplicate grant" secondary="Exchange and Entra permission grants are additive." /></ListItem><ListItem disableGutters><ListItemText primary="Secrets stay outside backups" secondary="Portable exports omit credentials and email bodies." /></ListItem></List></CardContent></Card></Stack></Grid>
        <Grid size={{ xs: 12 }}><Card><CardContent><Typography variant="h5">Delivery outbox</Typography><Typography color="text.secondary" sx={{ mb: 2 }}>Recent provider submissions and sanitized failures.</Typography>{!outbox.length ? <Alert severity="info">No email attempts yet.</Alert> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>Created</TableCell><TableCell>Recipient</TableCell><TableCell>Subject</TableCell><TableCell>Status</TableCell><TableCell>Attempts</TableCell><TableCell align="right">Action</TableCell></TableRow></TableHead><TableBody>{outbox.map(item => <TableRow key={item.id}><TableCell>{new Date(item.createdAt).toLocaleString()}</TableCell><TableCell>{item.to.join(', ')}</TableCell><TableCell>{item.subject}</TableCell><TableCell><Chip size="small" label={item.status} color={item.status === 'accepted' ? 'success' : item.status === 'failed' ? 'error' : 'warning'} /></TableCell><TableCell>{item.attempts}</TableCell><TableCell align="right">{item.status === 'failed' && <Button size="small" disabled={Boolean(busy)} onClick={() => retry(item)}>Retry</Button>}</TableCell></TableRow>)}</TableBody></Table></TableContainer>}</CardContent></Card></Grid>
      </Grid>}
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
        }}>Local setup only. Production credentials should be injected from Key Vault.</Typography></Box>{status.uiConfigPersistenceReady ? <Alert severity="success">Saved connection settings will be encrypted with the configured database-configuration key.</Alert> : <Alert severity="warning">Set DATABASE_CONFIG_ENCRYPTION_KEY or DATABASE_CONFIG_ENCRYPTION_KEY_FILE on the container before activating this connection. The application will not store a database password in clear text.</Alert>}<Grid container spacing={2}><Grid size={{ xs: 12, md: 8 }}><TextField fullWidth label="Host" value={settings.host || ''} onChange={event => update('host', event.target.value)} required /></Grid><Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Port" type="number" value={settings.port || 5432} onChange={event => update('port', Number(event.target.value))} required /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Database" value={settings.database || ''} onChange={event => update('database', event.target.value)} required /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Username" value={settings.username || ''} onChange={event => update('username', event.target.value)} required /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Password" type="password" value={settings.password || ''} onChange={event => update('password', event.target.value)} /></Grid><Grid size={{ xs: 12, md: 6 }}><FormControl fullWidth><InputLabel>SSL mode</InputLabel><Select label="SSL mode" value={settings.sslmode || 'prefer'} onChange={event => update('sslmode', event.target.value)}><MenuItem value="prefer">Prefer SSL</MenuItem><MenuItem value="require">Require SSL</MenuItem><MenuItem value="verify-full">Verify full certificate</MenuItem><MenuItem value="disable">Disable SSL (local only)</MenuItem></Select></FormControl></Grid></Grid><FormControl fullWidth><InputLabel>Blank database seed</InputLabel><Select label="Blank database seed" value={settings.seedMode || 'current'} onChange={event => update('seedMode', event.target.value)}><MenuItem value="current">Migrate this workspace</MenuItem><MenuItem value="empty">Platform setup only</MenuItem><MenuItem value="demo">Demo customers and assets</MenuItem></Select></FormControl><Typography variant="caption" sx={{
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
