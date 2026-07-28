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
import LinkOutlined from '@mui/icons-material/LinkOutlined';
import EmailOutlined from '@mui/icons-material/EmailOutlined';
import SendOutlined from '@mui/icons-material/SendOutlined';
import DownloadOutlined from '@mui/icons-material/DownloadOutlined';
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Checkbox, Chip, CircularProgress, Dialog, DialogActions, DialogContent,
  DialogTitle, Divider, FormControl, FormControlLabel, Grid, InputLabel, List, ListItem, ListItemIcon, ListItemText,
  MenuItem, Paper, Select, Stack, Step, StepLabel, Stepper, Table, TableBody, TableCell, TableContainer, TableHead, TablePagination, TableRow, TextField, Typography,
} from '@mui/material';
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { Navigate, useNavigate } from 'react-router';
import { apiDownload, apiFetch, getSession } from './session';
import { Title, useNotify } from './ui';
import type { AccessGroup, AccessGroupImpact, ApiToken, Asset, CiReviewItem, CiReviewQueue, Company, ConfigurationReconciliationItem, ConfigurationReconciliationPreview, ConnectWiseCiOptions, ConnectWiseCiPolicy, ConnectWiseConnection, ConnectWiseDiscoveryOptions, DiscoveryPreview, IntegrationPreviewStatus, IntegrationProviderManifest, IntegrationTestResult, ProviderCompany, RoleTemplate, SyncRun, User } from './types';
import { useWorkspace } from './workspace';
import { DEFAULT_MSP_BRAND, useMspBranding, type Brand } from './branding';
import { CustomerScopeSelector } from './CustomerScopeSelector';
import { canonicalAssetTypes } from './assetCatalog';

type Notice = { severity: 'success' | 'error' | 'info' | 'warning'; message: string } | null;
const emptyCiPolicy = (companyId = '', providerParentId = ''): ConnectWiseCiPolicy => ({
  id: '', provider: 'connectwise', companyId, providerParentId,
  typeMode: 'all', includedTypeIds: [], statusMode: 'all', includedStatusIds: [],
  typeMappings: {}, blockUnmappedTypes: false,
  excludedExternalIds: [], syncMode: 'manual', intervalMinutes: 360,
  enabled: false, revision: 0, updatedAt: null,
});
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

export function RootGuard({ adminOnly = false, children }: { adminOnly?: boolean; children: React.ReactNode }) {
  const workspace = useWorkspace();
  const role = getSession()?.user.role;
  if (!workspace.isRoot) return <Navigate to="/" replace />;
  if (!['platform_admin', 'msp_operator'].includes(role || '') || (adminOnly && role !== 'platform_admin')) {
    return <Alert severity="error">This page requires {adminOnly ? 'platform administrator' : 'MSP'} access.</Alert>;
  }
  return <>{children}</>;
}

export function PageHeading({ eyebrow, title, copy, action }: { eyebrow: string; title: string; copy: string; action?: React.ReactNode }) {
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

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [userData, groupData] = await Promise.all([apiFetch<User[]>('/api/users'), apiFetch<AccessGroup[]>('/api/access-groups')]);
      setUsers(userData); setGroups(groupData); setCompanyId(value => value || workspace.companies[0]?.id || '');
    } catch (error) { setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Users could not be loaded.' }); }
    finally { setLoading(false); }
  }, [workspace.companies]);
  useEffect(() => { void load(); }, [load]);

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
        <Stack component="form" onSubmit={resetPassword}><DialogTitle>Change local password</DialogTitle><DialogContent><Stack spacing={2} sx={{ pt: 1 }}><Typography color="text.secondary">Set a new password for {passwordUser?.email}.</Typography><TextField label="New password" type="password" value={newPassword} onChange={event => setNewPassword(event.target.value)} slotProps={{ htmlInput: { minLength: 12 } }} helperText="At least 12 characters" required /><Alert severity="warning">All active browser sessions for this user will be revoked.</Alert></Stack></DialogContent><DialogActions><Button onClick={() => setPasswordUser(null)}>Cancel</Button><Button type="submit" variant="contained">Change password</Button></DialogActions></Stack>
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

export function ConnectWiseIntegrationPage() {
  const [connectWise, setConnectWise] = useState<ConnectWiseConnection | null>(null);
  const [providers, setProviders] = useState<IntegrationProviderManifest[]>([]);
  const [providerCompanies, setProviderCompanies] = useState<ProviderCompany[]>([]);
  const [testResult, setTestResult] = useState<IntegrationTestResult | null>(null);
  const [preview, setPreview] = useState<DiscoveryPreview | null>(null);
  const [previewStale, setPreviewStale] = useState(false);
  const [discoveryOptions, setDiscoveryOptions] = useState<ConnectWiseDiscoveryOptions | null>(null);
  const [filterChoicesLoading, setFilterChoicesLoading] = useState(false);
  const [activeStep, setActiveStep] = useState(0);
  const [publicKey, setPublicKey] = useState('');
  const [privateKey, setPrivateKey] = useState('');
  const [companySearch, setCompanySearch] = useState('');
  const [mappingChoices, setMappingChoices] = useState<Record<string, string>>({});
  const [createCustomerItem, setCreateCustomerItem] = useState<ProviderCompany | null>(null);
  const [createCustomerName, setCreateCustomerName] = useState('');
  const [createCustomerSlug, setCreateCustomerSlug] = useState('');
  const [configurationCompanyId, setConfigurationCompanyId] = useState('');
  const [configurationOptions, setConfigurationOptions] = useState<ConnectWiseCiOptions | null>(null);
  const [configurationPolicy, setConfigurationPolicy] = useState<ConnectWiseCiPolicy>(emptyCiPolicy());
  const [configurationOptionsLoading, setConfigurationOptionsLoading] = useState(false);
  const [configurationPreview, setConfigurationPreview] = useState<ConfigurationReconciliationPreview | null>(null);
  const [selectedConfigurationIds, setSelectedConfigurationIds] = useState<string[]>([]);
  const [configurationSearch, setConfigurationSearch] = useState('');
  const [configurationActionFilter, setConfigurationActionFilter] = useState('reviewable');
  const [configurationPage, setConfigurationPage] = useState(0);
  const [configurationRowsPerPage, setConfigurationRowsPerPage] = useState(25);
  const [linkConfigurationItem, setLinkConfigurationItem] = useState<ConfigurationReconciliationItem | null>(null);
  const [linkAssets, setLinkAssets] = useState<Asset[]>([]);
  const [linkAssetId, setLinkAssetId] = useState('');
  const [previewStatus, setPreviewStatus] = useState<IntegrationPreviewStatus | null>(null);
  const [syncRuns, setSyncRuns] = useState<SyncRun[]>([]);
  const [ciReviewItems, setCiReviewItems] = useState<CiReviewItem[]>([]);
  const [ciReviewTotal, setCiReviewTotal] = useState(0);
  const [dismissReviewItem, setDismissReviewItem] = useState<CiReviewItem | null>(null);
  const [dismissReviewNotes, setDismissReviewNotes] = useState('');
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState<unknown>(null);
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const isAdmin = getSession()?.user.role === 'platform_admin';
  const load = async () => {
    try {
      const [connection, discovered, catalogue, workerStatus, recentRuns] = await Promise.all([
        apiFetch<ConnectWiseConnection>('/api/integrations/connectwise/config'),
        apiFetch<ProviderCompany[]>('/api/integrations/connectwise/companies'),
        apiFetch<IntegrationProviderManifest[]>('/api/integration-providers'),
        apiFetch<IntegrationPreviewStatus>('/api/integrations/continuous-preview/status'),
        apiFetch<SyncRun[]>('/api/sync-runs?provider=connectwise&limit=25'),
      ]);
      setConnectWise(connection); setProviderCompanies(discovered); setProviders(catalogue); setPreviewStatus(workerStatus); setSyncRuns(recentRuns);
    } catch (value) { setError(value); }
  };
  useEffect(() => { void load(); }, []);
  const connectWiseManifest = providers.find(item => item.key === 'connectwise');
  const filteredCompanies = useMemo(() => providerCompanies.filter(item => `${item.name} ${item.identifier} ${item.externalId} ${item.mappedCompanyName}`.toLowerCase().includes(companySearch.toLowerCase())), [providerCompanies, companySearch]);
  const mappedCompanies = useMemo(() => providerCompanies.filter(item => item.active && item.mappedCompanyId), [providerCompanies]);
  const filteredConfigurationItems = useMemo(() => (configurationPreview?.items || []).filter(item => {
    const matchesSearch = !configurationSearch || `${item.name} ${item.type} ${item.externalId} ${item.assetName}`.toLowerCase().includes(configurationSearch.toLowerCase());
    const matchesAction = configurationActionFilter === 'all' || (configurationActionFilter === 'reviewable' ? ['create', 'update', 'link'].includes(item.action) : item.action === configurationActionFilter);
    return matchesSearch && matchesAction;
  }), [configurationPreview, configurationSearch, configurationActionFilter]);
  const selectableConfigurationItems = useMemo(() => filteredConfigurationItems.filter(item => ['create', 'update', 'link'].includes(item.action)), [filteredConfigurationItems]);
  const visibleConfigurationItems = useMemo(() => filteredConfigurationItems.slice(configurationPage * configurationRowsPerPage, (configurationPage + 1) * configurationRowsPerPage), [filteredConfigurationItems, configurationPage, configurationRowsPerPage]);
  const configurationTypesInScope = useMemo(() => (configurationOptions?.availableTypes || []).filter(item => configurationPolicy.typeMode === 'all' || configurationPolicy.includedTypeIds.includes(item.id)), [configurationOptions, configurationPolicy.typeMode, configurationPolicy.includedTypeIds]);
  const filterOptions = (kind: 'Statuses' | 'Types' | 'Sites') => [...new Set([...(connectWise?.discoveryPolicy[`included${kind}`] || []), ...(discoveryOptions?.[`available${kind}`] || []), ...(preview?.[`available${kind}`] || [])])].sort();
  const updatePolicy = (changes: Partial<ConnectWiseConnection['discoveryPolicy']>) => {
    setConnectWise(current => current ? ({ ...current, discoveryPolicy: { ...current.discoveryPolicy, ...changes } }) : current);
    setPreviewStale(Boolean(preview));
  };
  const loadDiscoveryOptions = useCallback(async () => {
    setFilterChoicesLoading(true); setError(null);
    try {
      setDiscoveryOptions(await apiFetch<ConnectWiseDiscoveryOptions>('/api/integrations/connectwise/discovery-options'));
    } catch (value) { setError(value); } finally { setFilterChoicesLoading(false); }
  }, []);
  async function sync(type: string) {
    setBusy(type); setError(null); setNotice({ severity: 'info', message: 'Reading ConnectWise companies. Nothing will be written to ConnectWise or mapped automatically.' });
    try {
      const run = await apiFetch<SyncRun>(`/api/integrations/${type}/sync`, { method: 'POST' });
      await load();
      setNotice({ severity: run.status === 'failed' ? 'error' : 'success', message: run.message });
    } catch (value) { setError(value); setNotice(null); } finally { setBusy(''); }
  }
  async function saveConnectWise(event: FormEvent) {
    event.preventDefault(); if (!connectWise) return;
    setBusy('save-cw'); setNotice(null); setError(null);
    try {
      const stored = await apiFetch<ConnectWiseConnection>('/api/integrations/connectwise/config', {
        method: 'PUT',
        body: JSON.stringify({
          enabled: connectWise.enabled, baseUrl: connectWise.baseUrl, companyId: connectWise.companyId,
          clientId: connectWise.clientId, pageSize: connectWise.pageSize, publicKey, privateKey,
          expectedRevision: connectWise.revision,
        }),
      });
      setConnectWise(stored); setPublicKey(''); setPrivateKey(''); setTestResult(null); setDiscoveryOptions(null); setPreview(null); setPreviewStale(false);
      setNotice({ severity: 'success', message: 'ConnectWise settings saved with write-only encrypted credentials. Test the connection before discovery.' });
      await load();
    } catch (value) { setError(value); } finally { setBusy(''); }
  }
  async function testConnectWise() {
    setBusy('test-cw'); setTestResult(null); setNotice({ severity: 'info', message: 'Testing authentication and company-read permission…' }); setError(null);
    try {
      const result = await apiFetch<IntegrationTestResult>('/api/integrations/connectwise/test', { method: 'POST' });
      setTestResult(result); setNotice({ severity: 'success', message: result.message }); await load();
    } catch (value) { setError(value); setNotice(null); await load(); } finally { setBusy(''); }
  }
  async function savePolicy() {
    if (!connectWise) return;
    setBusy('save-policy'); setError(null); setNotice(null);
    try {
      const stored = await apiFetch<ConnectWiseConnection>('/api/integrations/connectwise/policy', { method: 'PUT', body: JSON.stringify({ ...connectWise.discoveryPolicy, expectedRevision: connectWise.revision }) });
      setConnectWise(stored); setNotice({ severity: 'success', message: 'Discovery filters saved as an audited integration policy.' });
    } catch (value) { setError(value); } finally { setBusy(''); }
  }
  async function previewDiscovery() {
    if (!connectWise) return;
    setBusy('preview-cw'); setError(null); setNotice({ severity: 'info', message: 'Running a read-only preview. No observations or mappings will be saved.' });
    try {
      const stored = await apiFetch<ConnectWiseConnection>('/api/integrations/connectwise/policy', { method: 'PUT', body: JSON.stringify({ ...connectWise.discoveryPolicy, expectedRevision: connectWise.revision }) });
      setConnectWise(stored);
      const result = await apiFetch<DiscoveryPreview>('/api/integrations/connectwise/discovery-preview', { method: 'POST' });
      setPreview(result);
      setPreviewStale(false);
      setDiscoveryOptions({ readOnly: result.readOnly, writesAttempted: result.writesAttempted, sampled: result.discovered, truncated: result.truncated, availableStatuses: result.availableStatuses, availableTypes: result.availableTypes, availableSites: result.availableSites, credentialSource: connectWise.credentialSource });
      setNotice({ severity: 'success', message: result.message }); await load();
    } catch (value) { setError(value); setNotice(null); } finally { setBusy(''); }
  }
  async function mapCompany(item: ProviderCompany) {
    const companyId = mappingChoices[item.externalId] || item.mappedCompanyId || item.suggestedCompanyId || '';
    if (!companyId) return;
    setBusy(`map-${item.externalId}`); setError(null);
    try {
      await apiFetch(`/api/integrations/connectwise/companies/${encodeURIComponent(item.externalId)}/mapping`, { method: 'PUT', body: JSON.stringify({ companyId }) });
      setNotice({ severity: 'success', message: `${item.name} is now explicitly mapped. No ConnectWise data was changed.` }); await load();
    } catch (value) { setError(value); } finally { setBusy(''); }
  }
  async function unmapCompany(item: ProviderCompany) {
    setBusy(`map-${item.externalId}`); setError(null);
    try {
      await apiFetch(`/api/integrations/connectwise/companies/${encodeURIComponent(item.externalId)}/mapping`, { method: 'DELETE' });
      setNotice({ severity: 'success', message: `${item.name} was unmapped; its discovery and audit history remain available.` }); await load();
    } catch (value) { setError(value); } finally { setBusy(''); }
  }
  function openCreateCustomer(item: ProviderCompany) {
    const suggestedSlug = (item.identifier || item.name).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48);
    setCreateCustomerItem(item); setCreateCustomerName(item.name); setCreateCustomerSlug(suggestedSlug); setError(null);
  }
  async function createCustomerAndMap(event: FormEvent) {
    event.preventDefault();
    if (!createCustomerItem || !createCustomerName.trim()) return;
    const item = createCustomerItem;
    setBusy(`create-${item.externalId}`); setError(null);
    try {
      const created = await apiFetch<Company>('/api/companies', {
        method: 'POST',
        body: JSON.stringify({ name: createCustomerName.trim(), slug: createCustomerSlug.trim() }),
      });
      await apiFetch(`/api/integrations/connectwise/companies/${encodeURIComponent(item.externalId)}/mapping`, {
        method: 'PUT', body: JSON.stringify({ companyId: created.id }),
      });
      setMappingChoices(current => ({ ...current, [item.externalId]: created.id }));
      setCreateCustomerItem(null); setCreateCustomerName(''); setCreateCustomerSlug('');
      await Promise.all([workspace.refreshCompanies(), load()]);
      setNotice({ severity: 'success', message: `${created.name} was created and explicitly mapped to ${item.name}. No data was written to ConnectWise.` });
    } catch (value) { setError(value); } finally { setBusy(''); }
  }
  const updateConfigurationPolicy = (changes: Partial<ConnectWiseCiPolicy>) => {
    setConfigurationPolicy(current => ({ ...current, ...changes }));
    setConfigurationPreview(null); setSelectedConfigurationIds([]); setConfigurationPage(0);
  };
  const updateConfigurationTypeMapping = (providerTypeId: string, canonicalType: string) => {
    const mappings = { ...configurationPolicy.typeMappings };
    if (canonicalType) mappings[providerTypeId] = canonicalType; else delete mappings[providerTypeId];
    updateConfigurationPolicy({ typeMappings: mappings });
  };
  const autoMapConfigurationTypes = () => {
    const canonicalByName = new Map(canonicalAssetTypes.map(type => [type.toLocaleLowerCase(), type]));
    const mappings = { ...configurationPolicy.typeMappings };
    configurationTypesInScope.forEach(item => {
      const exact = canonicalByName.get(item.name.toLocaleLowerCase());
      if (exact && !mappings[item.id]) mappings[item.id] = exact;
    });
    updateConfigurationPolicy({ typeMappings: mappings });
  };
  const loadCiReviewQueue = useCallback(async (companyId?: string) => {
    const selectedCompanyId = companyId || configurationPolicy.companyId;
    if (!selectedCompanyId) { setCiReviewItems([]); setCiReviewTotal(0); return; }
    try {
      const queue = await apiFetch<CiReviewQueue>(`/api/integrations/connectwise/configurations/review-queue?companyId=${encodeURIComponent(selectedCompanyId)}&state=pending&limit=500`);
      setCiReviewItems(queue.items); setCiReviewTotal(queue.total);
    } catch (value) { setError(value); }
  }, [configurationPolicy.companyId]);
  const loadConfigurationOptions = useCallback(async () => {
    const mapped = mappedCompanies.find(item => item.externalId === configurationCompanyId);
    if (!mapped?.mappedCompanyId) return;
    setConfigurationOptionsLoading(true); setError(null);
    try {
      const options = await apiFetch<ConnectWiseCiOptions>('/api/integrations/connectwise/configurations/options', {
        method: 'POST', body: JSON.stringify({ companyId: mapped.mappedCompanyId, providerCompanyId: mapped.externalId }),
      });
      setConfigurationOptions(options); setConfigurationPolicy(options.policy);
      await loadCiReviewQueue(mapped.mappedCompanyId);
    } catch (value) { setError(value); } finally { setConfigurationOptionsLoading(false); }
  }, [configurationCompanyId, loadCiReviewQueue, mappedCompanies]);
  useEffect(() => {
    if (activeStep === 3 && connectWise?.configured && !discoveryOptions) void loadDiscoveryOptions();
  }, [activeStep, connectWise?.configured, discoveryOptions, loadDiscoveryOptions]);
  useEffect(() => {
    if (activeStep === 5 && configurationCompanyId) void loadConfigurationOptions();
  }, [activeStep, configurationCompanyId, loadConfigurationOptions]);
  async function saveConfigurationPolicy(showNotice = true): Promise<ConnectWiseCiPolicy | null> {
    if (!isAdmin || !configurationPolicy.companyId || !configurationPolicy.providerParentId) return configurationPolicy;
    setBusy('save-ci-policy'); setError(null);
    try {
      const stored = await apiFetch<ConnectWiseCiPolicy>('/api/integrations/connectwise/configurations/policy', {
        method: 'PUT', body: JSON.stringify({
          companyId: configurationPolicy.companyId,
          providerCompanyId: configurationPolicy.providerParentId,
          typeMode: configurationPolicy.typeMode,
          includedTypeIds: configurationPolicy.includedTypeIds,
          typeMappings: configurationPolicy.typeMappings,
          blockUnmappedTypes: configurationPolicy.blockUnmappedTypes,
          statusMode: configurationPolicy.statusMode,
          includedStatusIds: configurationPolicy.includedStatusIds,
          excludedExternalIds: configurationPolicy.excludedExternalIds,
          syncMode: configurationPolicy.syncMode,
          intervalMinutes: configurationPolicy.intervalMinutes,
          enabled: configurationPolicy.enabled,
          expectedRevision: configurationPolicy.revision,
        }),
      });
      setConfigurationPolicy(stored);
      if (showNotice) setNotice({ severity: 'success', message: 'CI filters, type mappings and continuous-preview settings were saved with an audit revision.' });
      return stored;
    } catch (value) { setError(value); return null; } finally { setBusy(''); }
  }
  async function previewConfigurations() {
    const mapped = mappedCompanies.find(item => item.externalId === configurationCompanyId);
    if (!mapped?.mappedCompanyId) return;
    setBusy('preview-configurations'); setError(null); setNotice({ severity: 'info', message: 'Reading mapped-customer configuration items and calculating reconciliation actions. No records will be changed.' });
    try {
      if (isAdmin) {
        const stored = await apiFetch<ConnectWiseCiPolicy>('/api/integrations/connectwise/configurations/policy', {
          method: 'PUT', body: JSON.stringify({
            companyId: mapped.mappedCompanyId, providerCompanyId: mapped.externalId,
            typeMode: configurationPolicy.typeMode, includedTypeIds: configurationPolicy.includedTypeIds,
            typeMappings: configurationPolicy.typeMappings, blockUnmappedTypes: configurationPolicy.blockUnmappedTypes,
            statusMode: configurationPolicy.statusMode, includedStatusIds: configurationPolicy.includedStatusIds,
            excludedExternalIds: configurationPolicy.excludedExternalIds,
            syncMode: configurationPolicy.syncMode, intervalMinutes: configurationPolicy.intervalMinutes,
            enabled: configurationPolicy.enabled, expectedRevision: configurationPolicy.revision,
          }),
        });
        setConfigurationPolicy(stored);
      }
      const result = await apiFetch<ConfigurationReconciliationPreview>('/api/integrations/connectwise/configurations/preview', {
        method: 'POST', body: JSON.stringify({ companyId: mapped.mappedCompanyId, providerCompanyId: mapped.externalId }),
      });
      setConfigurationPreview(result);
      setSelectedConfigurationIds([]); setConfigurationPage(0);
      setNotice({ severity: result.counts.conflict ? 'warning' : 'success', message: result.message });
      await Promise.all([load(), loadCiReviewQueue(mapped.mappedCompanyId)]);
    } catch (value) { setError(value); setNotice(null); } finally { setBusy(''); }
  }
  async function syncSavedPolicyNow() {
    if (!configurationPolicy.id) return;
    setBusy('sync-policy-now'); setError(null);
    setNotice({ severity: 'info', message: 'Running the saved read-only policy under an exclusive sync lease…' });
    try {
      const result = await apiFetch<ConfigurationReconciliationPreview>(
        `/api/integrations/connectwise/configurations/policies/${encodeURIComponent(configurationPolicy.id)}/sync-now`,
        { method: 'POST' },
      );
      setConfigurationPreview(result);
      setSelectedConfigurationIds([]); setConfigurationPage(0);
      setNotice({ severity: result.counts.conflict ? 'warning' : 'success', message: result.message });
      await Promise.all([load(), loadCiReviewQueue(result.companyId), loadConfigurationOptions()]);
    } catch (value) {
      setError(value); setNotice(null); await load();
    } finally {
      setBusy('');
    }
  }
  async function openLinkConfiguration(item: ConfigurationReconciliationItem) {
    if (!configurationPreview) return;
    setBusy('load-link-assets'); setError(null); setLinkAssetId('');
    try {
      const assets = await apiFetch<Asset[]>(`/api/assets?companyId=${encodeURIComponent(configurationPreview.companyId)}`);
      setLinkAssets(assets); setLinkConfigurationItem(item);
    } catch (value) { setError(value); } finally { setBusy(''); }
  }
  async function linkConfiguration() {
    if (!configurationPreview || !linkConfigurationItem || !linkAssetId) return;
    setBusy('link-configuration'); setError(null);
    try {
      const result = await apiFetch<{ message: string }>('/api/integrations/connectwise/configurations/link', {
        method: 'POST', body: JSON.stringify({
          companyId: configurationPreview.companyId,
          providerCompanyId: configurationPreview.providerCompanyId,
          externalId: linkConfigurationItem.externalId,
          assetId: linkAssetId,
        }),
      });
      setLinkConfigurationItem(null); setLinkAssetId(''); setNotice({ severity: 'success', message: result.message });
      await previewConfigurations();
    } catch (value) { setError(value); } finally { setBusy(''); }
  }
  async function importConfigurations() {
    if (!configurationPreview || !selectedConfigurationIds.length) return;
    setBusy('import-configurations'); setError(null); setNotice({ severity: 'info', message: `Applying ${selectedConfigurationIds.length} reviewed CMDB change(s). ConnectWise remains read-only.` });
    try {
      const run = await apiFetch<SyncRun>('/api/integrations/connectwise/configurations/import', {
        method: 'POST',
        body: JSON.stringify({
          companyId: configurationPreview.companyId,
          providerCompanyId: configurationPreview.providerCompanyId,
          externalIds: selectedConfigurationIds,
        }),
      });
      setConfigurationPreview(null); setSelectedConfigurationIds([]);
      setNotice({ severity: 'success', message: run.message }); await Promise.all([load(), loadCiReviewQueue(configurationPreview.companyId)]);
    } catch (value) { setError(value); setNotice(null); } finally { setBusy(''); }
  }
  async function dismissCiReview() {
    if (!dismissReviewItem || dismissReviewNotes.trim().length < 4) return;
    const item = dismissReviewItem;
    setBusy(`dismiss-review-${item.id}`); setError(null);
    try {
      await apiFetch(`/api/integrations/connectwise/configurations/review-queue/${encodeURIComponent(item.id)}/dismiss`, {
        method: 'POST', body: JSON.stringify({ notes: dismissReviewNotes.trim() }),
      });
      setDismissReviewItem(null); setDismissReviewNotes('');
      setNotice({ severity: 'success', message: `${item.externalName} was dismissed. New or changed provider evidence will reopen it automatically.` });
      await Promise.all([load(), loadCiReviewQueue(item.companyId)]);
    } catch (value) { setError(value); } finally { setBusy(''); }
  }
  const steps = ['Provider', 'Connection', 'Tests', 'Filters & preview', 'Customer mapping', 'CI reconciliation'];
  return (
    <RootGuard><Title title="ConnectWise configuration" /><PageHeading eyebrow="Integration configuration" title="ConnectWise PSA" copy="Manage the connection, discovery scope, customer mappings and governed CI reconciliation policy for this installed integration." action={<Stack direction="row" spacing={1}><Chip label="MSP scope" color="primary" variant="outlined" /><Button variant="outlined" onClick={() => navigate('/admin/integrations')}>Back to integrations</Button></Stack>} />
      {notice ? <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert> : null}
      {error ? <Box sx={{ mb: 2 }}><ErrorNotice error={error} /></Box> : null}
      {connectWise && <Card><CardContent><Stepper activeStep={activeStep} alternativeLabel sx={{ mb: 4 }}>{steps.map(label => <Step key={label}><StepLabel>{label}</StepLabel></Step>)}</Stepper>
        {activeStep === 0 && <Stack spacing={2}><Box><Typography variant="overline" color="primary">Provider adapter</Typography><Typography variant="h4">{connectWiseManifest?.name || 'ConnectWise PSA'}</Typography><Typography color="text.secondary">{connectWiseManifest?.description}</Typography></Box><Grid container spacing={2}><Grid size={{ xs: 12, lg: 5 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Prerequisites</Typography><List dense>{connectWiseManifest?.prerequisites.map(item => <ListItem key={item} disableGutters><ListItemIcon sx={{ minWidth: 30 }}><CheckCircleOutlined fontSize="small" color="primary" /></ListItemIcon><ListItemText primary={item} /></ListItem>)}</List></Paper></Grid><Grid size={{ xs: 12, lg: 7 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Typography variant="h6">Declared operations</Typography><List dense>{connectWiseManifest?.operations.map(operation => <ListItem key={operation.key} disableGutters secondaryAction={<Chip size="small" label={operation.status} color={operation.status === 'available' ? 'success' : 'default'} />}><ListItemText primary={`${operation.label} · ${operation.direction}`} secondary={`${operation.description}${operation.writes_provider ? ' Provider write; approval required.' : ''}`} /></ListItem>)}</List></Paper></Grid></Grid><Alert severity="info">The wizard enables review-gated company mapping and configuration-item import. Both ConnectWise inputs are read-only; future ticket output remains disabled until an approval-gated workflow executor is implemented.</Alert></Stack>}

        {activeStep === 1 && <Stack spacing={2.5} component="form" onSubmit={saveConnectWise}><Box><Typography variant="overline" color="primary">Secure connection</Typography><Typography variant="h5">API member credentials</Typography><Typography color="text.secondary">Keys are write-only and encrypted with the installation key. Container-managed environment values take precedence.</Typography></Box>{connectWise.managedByEnvironment && <Alert severity={connectWise.lastError ? 'error' : 'info'}>{connectWise.lastError || 'Credentials are managed by CW_* environment variables. Discovery filters remain editable in this wizard.'}</Alert>}<Grid container spacing={2}><Grid size={{ xs: 12, lg: 6 }}><TextField fullWidth label="Regional API base URL" value={connectWise.baseUrl} onChange={event => setConnectWise({ ...connectWise, baseUrl: event.target.value })} placeholder="https://api-eu.myconnectwise.net/v4_6_release/apis/3.0" disabled={connectWise.managedByEnvironment || !isAdmin} required /></Grid><Grid size={{ xs: 12, md: 6, lg: 3 }}><TextField fullWidth label="ConnectWise company ID" value={connectWise.companyId} onChange={event => setConnectWise({ ...connectWise, companyId: event.target.value })} disabled={connectWise.managedByEnvironment || !isAdmin} required /></Grid><Grid size={{ xs: 12, md: 6, lg: 3 }}><TextField fullWidth label="Integration client ID" value={connectWise.clientId} onChange={event => setConnectWise({ ...connectWise, clientId: event.target.value })} disabled={connectWise.managedByEnvironment || !isAdmin} required /></Grid>{!connectWise.managedByEnvironment && isAdmin && <><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Public API key" value={publicKey} onChange={event => setPublicKey(event.target.value)} type="password" helperText={connectWise.hasCredentials ? 'Leave both keys blank to retain the stored credential pair.' : 'Required for the first save.'} /></Grid><Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Private API key" value={privateKey} onChange={event => setPrivateKey(event.target.value)} type="password" helperText="Never returned to the browser or audit log." /></Grid></>}<Grid size={{ xs: 12, sm: 4 }}><TextField fullWidth label="Page size" type="number" value={connectWise.pageSize} onChange={event => setConnectWise({ ...connectWise, pageSize: Number(event.target.value) })} slotProps={{ htmlInput: { min: 25, max: 1000 } }} disabled={connectWise.managedByEnvironment || !isAdmin} /></Grid></Grid>{!connectWise.managedByEnvironment && isAdmin && <Button type="submit" variant="contained" disabled={busy === 'save-cw'} sx={{ alignSelf: 'flex-start' }}>{busy === 'save-cw' ? 'Saving…' : 'Save encrypted connection'}</Button>}</Stack>}

        {activeStep === 2 && <Stack spacing={2}><Box><Typography variant="overline" color="primary">Progressive verification</Typography><Typography variant="h5">Connection tests</Typography><Typography color="text.secondary">Validate configuration, authentication and the least-privilege company read. No provider write is attempted.</Typography></Box><Button variant="contained" onClick={() => void testConnectWise()} disabled={!connectWise.configured || busy === 'test-cw'} sx={{ alignSelf: 'flex-start' }}>{busy === 'test-cw' ? 'Testing…' : 'Run connection tests'}</Button>{testResult?.stages.map(stage => <Alert key={stage.key} severity={stage.status === 'passed' ? 'success' : stage.status === 'failed' ? 'error' : 'info'}><Typography sx={{ fontWeight: 750 }}>{stage.label}</Typography>{stage.message}</Alert>)}{!testResult && <Alert severity={connectWise.connectionStatus === 'verified' ? 'success' : 'info'}>{connectWise.connectionStatus === 'verified' ? `Verified ${connectWise.lastTestAt ? new Date(connectWise.lastTestAt).toLocaleString() : ''}` : 'Run the tests before selecting discovery scope.'}</Alert>}</Stack>}

        {activeStep === 3 && <Stack spacing={2}><Box><Typography variant="overline" color="primary">Read scope</Typography><Typography variant="h5">Filters and dry-run preview</Typography><Typography color="text.secondary">Filter choices load automatically from a bounded ConnectWise company sample. Values selected in the same menu match any value; different menus are combined, so a company must satisfy every populated menu.</Typography></Box>{filterChoicesLoading ? <Alert severity="info" icon={<CircularProgress size={18} />}>Loading filter choices from ConnectWise…</Alert> : discoveryOptions ? <Alert severity={discoveryOptions.truncated ? 'warning' : 'success'}>Loaded filter choices from {discoveryOptions.sampled} company records.{discoveryOptions.truncated ? ' The source contains more than 1,000 records, so the choices and preview are sampled.' : ''}</Alert> : <Alert severity="warning">Filter choices have not loaded. Use Refresh filter choices to retry.</Alert>}<Grid container spacing={2}>{([{ kind: 'Statuses', label: 'Statuses' }, { kind: 'Types', label: 'Company types' }, { kind: 'Sites', label: 'Territories' }] as const).map(({ kind, label }) => { const options = filterOptions(kind); const labelId = `connectwise-${kind.toLowerCase()}-label`; return <Grid key={kind} size={{ xs: 12, md: 4 }}><FormControl fullWidth><InputLabel id={labelId}>{label}</InputLabel><Select id={`connectwise-${kind.toLowerCase()}`} labelId={labelId} multiple label={label} value={connectWise.discoveryPolicy[`included${kind}`]} onChange={event => updatePolicy({ [`included${kind}`]: event.target.value as string[] })}>{filterChoicesLoading ? <MenuItem disabled>Loading choices…</MenuItem> : options.length ? options.map(value => <MenuItem key={value} value={value}>{value}</MenuItem>) : <MenuItem disabled>No values returned by ConnectWise</MenuItem>}</Select></FormControl></Grid>; })}<Grid size={{ xs: 12, md: 8 }}><TextField fullWidth label="Excluded provider company IDs" value={connectWise.discoveryPolicy.excludedExternalIds.join(', ')} onChange={event => updatePolicy({ excludedExternalIds: event.target.value.split(',').map(value => value.trim()).filter(Boolean) })} helperText="Comma-separated immutable ConnectWise company IDs." /></Grid><Grid size={{ xs: 12, md: 4 }}><FormControlLabel control={<Checkbox checked={connectWise.discoveryPolicy.includeDeleted} onChange={(_, checked) => updatePolicy({ includeDeleted: checked })} />} label="Include deleted companies" /></Grid></Grid><Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}><Button variant="outlined" disabled={filterChoicesLoading} onClick={() => void loadDiscoveryOptions()}>{filterChoicesLoading ? 'Loading choices…' : 'Refresh filter choices'}</Button><Button variant="outlined" disabled={!isAdmin || busy === 'save-policy'} onClick={() => void savePolicy()}>{busy === 'save-policy' ? 'Saving…' : 'Save filter policy'}</Button><Button variant="contained" disabled={!connectWise.configured || busy === 'preview-cw'} onClick={() => void previewDiscovery()}>{busy === 'preview-cw' ? 'Previewing…' : previewStale ? 'Recalculate preview' : 'Run read-only preview'}</Button></Stack>{previewStale && <Alert severity="warning">The filters have changed since this preview was generated. Recalculate the preview before continuing.</Alert>}{preview && <><Alert severity={previewStale ? 'warning' : 'success'}><Stack spacing={1}><Typography sx={{ fontWeight: 750 }}>{previewStale ? 'Previously applied filters' : 'Applied filters'}</Typography><Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>{preview.appliedPolicy.includedStatuses.map(value => <Chip key={`status-${value}`} size="small" label={`Status: ${value}`} />)}{preview.appliedPolicy.includedTypes.map(value => <Chip key={`type-${value}`} size="small" label={`Type: ${value}`} />)}{preview.appliedPolicy.includedSites.map(value => <Chip key={`territory-${value}`} size="small" label={`Territory: ${value}`} />)}{preview.appliedPolicy.excludedExternalIds.length > 0 && <Chip size="small" label={`${preview.appliedPolicy.excludedExternalIds.length} provider IDs excluded`} />}{!preview.appliedPolicy.includeDeleted && <Chip size="small" label="Deleted companies excluded" />}</Stack></Stack></Alert><Grid container spacing={2}>{[{ label: 'Companies evaluated', value: preview.discovered }, { label: `Matched (${preview.discovered ? Math.round((preview.included / preview.discovered) * 1000) / 10 : 0}%)`, value: preview.included }, { label: 'Filtered out', value: preview.excluded }].map(metric => <Grid key={metric.label} size={{ xs: 12, sm: 4 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h4">{metric.value}</Typography><Typography color="text.secondary">{metric.label}</Typography></Paper></Grid>)}</Grid>{preview.truncated && <Alert severity="warning">The preview evaluates only the first 1,000 accessible company records. Full discovery remains separately review-gated.</Alert>}{Object.keys(preview.exclusionReasons).length > 0 && <Alert severity="info"><Typography sx={{ fontWeight: 750 }}>Why companies were filtered out</Typography>{Object.entries(preview.exclusionReasons).map(([reason, count]) => `${reason}: ${count}`).join(' · ')}</Alert>}</>}</Stack>}

        {activeStep === 4 && <Stack spacing={2}>
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'center' } }}>
            <Box><Typography variant="overline" color="primary">Identity mapping</Typography><Typography variant="h5">ConnectWise companies</Typography><Typography color="text.secondary">Discovery reads optimized pages of up to 1,000 companies, persists only the included observations and never creates or maps customers automatically.</Typography></Box>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}><TextField size="small" label="Search companies" value={companySearch} onChange={event => setCompanySearch(event.target.value)} /><Button variant="contained" startIcon={busy === 'connectwise' ? <CircularProgress size={18} color="inherit" /> : <CloudSyncOutlined />} disabled={!connectWise.configured || busy === 'connectwise'} onClick={() => void sync('connectwise')}>{busy === 'connectwise' ? 'Reading optimized pages…' : 'Discover included companies'}</Button></Stack>
          </Stack>
          {!providerCompanies.length ? <Alert severity="info">Run filtered discovery to build the mapping queue.</Alert> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>ConnectWise company</TableCell><TableCell>Provider details</TableCell><TableCell>CMDB customer</TableCell><TableCell align="right">Action</TableCell></TableRow></TableHead><TableBody>{filteredCompanies.map(item => {
            const selected = mappingChoices[item.externalId] || item.mappedCompanyId || item.suggestedCompanyId || '';
            return <TableRow key={item.externalId} sx={{ opacity: item.active ? 1 : 0.55 }}>
              <TableCell><Typography sx={{ fontWeight: 750 }}>{item.name}</Typography><Typography variant="caption" color="text.secondary">ID {item.externalId}{item.identifier ? ` · ${item.identifier}` : ''}</Typography></TableCell>
              <TableCell><Typography variant="body2">{item.type || 'Unclassified'} · {item.status || 'No status'}</Typography><Typography variant="caption" color="text.secondary">{item.site || 'No territory'}</Typography></TableCell>
              <TableCell><FormControl size="small" fullWidth sx={{ minWidth: 220 }}><InputLabel>CMDB customer</InputLabel><Select label="CMDB customer" value={selected} onChange={event => setMappingChoices(current => ({ ...current, [item.externalId]: event.target.value }))} disabled={!isAdmin}><MenuItem value=""><em>Select customer</em></MenuItem>{workspace.companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}</Select></FormControl>{!item.mappedCompanyId && item.suggestedCompanyName && <Typography variant="caption" color="warning.main">Suggested: {item.suggestedCompanyName} · {item.suggestionReason}</Typography>}{item.mappedCompanyId && <Typography variant="caption" color="success.main">Mapped by explicit provider ID</Typography>}</TableCell>
              <TableCell align="right"><Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end', flexWrap: 'wrap' }}>{!item.mappedCompanyId && <Button size="small" startIcon={<AddBusinessOutlined />} disabled={!isAdmin || Boolean(busy)} onClick={() => openCreateCustomer(item)}>Create customer</Button>}{item.mappedCompanyId && <Button size="small" color="inherit" disabled={!isAdmin || busy === `map-${item.externalId}`} onClick={() => void unmapCompany(item)}>Unmap</Button>}<Button size="small" variant="outlined" disabled={!isAdmin || !selected || selected === item.mappedCompanyId || busy === `map-${item.externalId}`} onClick={() => void mapCompany(item)}>{item.mappedCompanyId ? 'Change' : 'Map'}</Button></Stack></TableCell>
            </TableRow>;
          })}</TableBody></Table></TableContainer>}
          <Typography variant="caption" color="text.secondary">The term ConnectWise is a trademark of ConnectWise, LLC. This application uses the ConnectWise API but is not endorsed or certified by ConnectWise.</Typography>
        </Stack>}

        {activeStep === 5 && <Stack spacing={2}>
          <Box><Typography variant="overline" color="primary">Reviewed import</Typography><Typography variant="h5">Configuration-item reconciliation</Typography><Typography color="text.secondary">Save immutable type and status filters per mapped customer, then preview provider-ID-first matching. ConnectWise remains read-only.</Typography></Box>
          {previewStatus && <Paper variant="outlined" sx={{ p: 2 }}><Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ justifyContent: 'space-between', alignItems: { md: 'center' } }}><Box><Typography variant="h6">Continuous discovery</Typography><Typography variant="body2" color="text.secondary">Multi-replica-safe policy leases refresh the review queue only. Failures retry with exponential backoff; canonical imports always require a separate administrator decision.</Typography>{previewStatus.runtime?.lastError && <Typography variant="caption" color="error.main">Last worker error: {previewStatus.runtime.lastError}</Typography>}</Box><Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}><Chip size="small" color={previewStatus.workerHealthy ? 'success' : previewStatus.workerConfigured ? 'warning' : 'default'} label={previewStatus.workerHealthy ? `${previewStatus.executionMode.replaceAll('_', ' ')} worker healthy · ${previewStatus.heartbeatAgeSeconds ?? 0}s ago` : previewStatus.workerConfigured ? `${previewStatus.executionMode.replaceAll('_', ' ')} worker awaiting heartbeat` : 'Worker disabled by deployment'} /><Chip size="small" label={`${previewStatus.enabledPolicies} enabled policies`} /><Chip size="small" color={previewStatus.pendingReviews ? 'warning' : 'default'} label={`${previewStatus.pendingReviews} pending reviews`} />{previewStatus.providerRateLimit && <Chip size="small" color={previewStatus.providerRateLimit.limited ? 'error' : 'info'} variant="outlined" label={previewStatus.providerRateLimit.remaining != null ? `ConnectWise quota ${previewStatus.providerRateLimit.remaining}${previewStatus.providerRateLimit.limit != null ? ` / ${previewStatus.providerRateLimit.limit}` : ''}` : `ConnectWise HTTP ${previewStatus.providerRateLimit.httpStatus || 'observed'}`} />}<Chip size="small" color={previewStatus.alertDeliveryConfigured ? 'success' : 'warning'} label={previewStatus.alertDeliveryConfigured ? `Failure alerts ready · ${previewStatus.alertRecipientCount} recipient(s)` : 'Failure alerts need email + notification worker'} /></Stack></Stack></Paper>}
          {!mappedCompanies.length ? <Alert severity="warning">Map at least one ConnectWise company to a CMDB customer before discovering configuration items.</Alert> : <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} sx={{ alignItems: { md: 'center' } }}><FormControl fullWidth sx={{ maxWidth: 620 }}><InputLabel id="connectwise-config-company-label">Mapped ConnectWise company</InputLabel><Select id="connectwise-config-company" labelId="connectwise-config-company-label" label="Mapped ConnectWise company" value={configurationCompanyId} onChange={event => { const externalId = event.target.value; const mapped = mappedCompanies.find(item => item.externalId === externalId); setConfigurationCompanyId(externalId); setConfigurationOptions(null); setConfigurationPolicy(emptyCiPolicy(mapped?.mappedCompanyId || '', externalId)); setConfigurationPreview(null); setCiReviewItems([]); setSelectedConfigurationIds([]); setConfigurationPage(0); }}>{mappedCompanies.map(item => <MenuItem key={item.externalId} value={item.externalId}>{item.name} → {item.mappedCompanyName}</MenuItem>)}</Select></FormControl><Button variant="outlined" disabled={!configurationCompanyId || configurationOptionsLoading || Boolean(busy)} onClick={() => void loadConfigurationOptions()}>{configurationOptionsLoading ? 'Loading choices…' : 'Refresh type & status choices'}</Button></Stack>}
          {configurationOptionsLoading && <Alert severity="info" icon={<CircularProgress size={18} />}>Reading the mapped company’s configuration catalogue from ConnectWise…</Alert>}
          {configurationOptions && !configurationOptionsLoading && <Paper variant="outlined" sx={{ p: 2 }}><Stack spacing={2}>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' } }}><Box><Typography variant="h6">Saved discovery policy</Typography><Typography variant="body2" color="text.secondary">{configurationOptions.discovered} configurations available · policy revision {configurationPolicy.revision}{configurationPolicy.nextRunAt ? ` · next run ${new Date(configurationPolicy.nextRunAt).toLocaleString()}` : ''}{configurationPolicy.lastRunAt ? ` · last run ${new Date(configurationPolicy.lastRunAt).toLocaleString()}` : ''}</Typography></Box><Stack direction="row" spacing={1}><Chip size="small" label="Immutable provider IDs" color="info" variant="outlined" />{Boolean(configurationPolicy.consecutiveFailures) && <Chip size="small" label={`${configurationPolicy.consecutiveFailures} failure(s) · ${configurationPolicy.retryDelayMinutes || 15}m backoff`} color="error" />}</Stack></Stack>
            {configurationPolicy.backoffActive && configurationPolicy.lastError && <Alert severity="warning">The policy is in retry backoff. {configurationPolicy.lastError}</Alert>}
            <Grid container spacing={1.5}>
              <Grid size={{ xs: 12, md: 3 }}><FormControl fullWidth><InputLabel id="connectwise-ci-type-mode-label">Type scope</InputLabel><Select id="connectwise-ci-type-mode" labelId="connectwise-ci-type-mode-label" label="Type scope" value={configurationPolicy.typeMode} onChange={event => updateConfigurationPolicy({ typeMode: event.target.value as ConnectWiseCiPolicy['typeMode'] })}><MenuItem value="all">All types</MenuItem><MenuItem value="selected">Selected types</MenuItem></Select></FormControl></Grid>
              <Grid size={{ xs: 12, md: 9 }}><FormControl fullWidth disabled={configurationPolicy.typeMode === 'all'}><InputLabel id="connectwise-ci-types-label">Included types</InputLabel><Select id="connectwise-ci-types" labelId="connectwise-ci-types-label" multiple label="Included types" value={configurationPolicy.includedTypeIds} onChange={event => updateConfigurationPolicy({ includedTypeIds: typeof event.target.value === 'string' ? event.target.value.split(',') : event.target.value })} renderValue={selected => configurationOptions.availableTypes.filter(item => selected.includes(item.id)).map(item => item.name).join(', ')}>{configurationOptions.availableTypes.map(item => <MenuItem key={item.id} value={item.id}><Checkbox checked={configurationPolicy.includedTypeIds.includes(item.id)} /><ListItemText primary={item.name} secondary={`${item.count} configuration(s) · ID ${item.id}`} /></MenuItem>)}</Select></FormControl></Grid>
              <Grid size={{ xs: 12 }}><Paper variant="outlined" sx={{ p: 2 }}><Stack spacing={1.5}>
                <Stack direction={{ xs: 'column', md: 'row' }} spacing={1} sx={{ justifyContent: 'space-between', alignItems: { md: 'center' } }}><Box><Typography variant="h6">Configuration type mapping</Typography><Typography variant="body2" color="text.secondary">Map immutable ConnectWise type IDs to the canonical CMDB types used by inventory, relationships and reporting.</Typography></Box><Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}><Chip size="small" color={configurationTypesInScope.every(item => configurationPolicy.typeMappings[item.id]) ? 'success' : 'warning'} label={`${configurationTypesInScope.filter(item => configurationPolicy.typeMappings[item.id]).length} of ${configurationTypesInScope.length} mapped`} /><Button size="small" variant="outlined" onClick={autoMapConfigurationTypes}>Auto-map exact names</Button></Stack></Stack>
                {!configurationTypesInScope.length ? <Alert severity="info">Select at least one ConnectWise configuration type to configure mappings.</Alert> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>ConnectWise configuration type</TableCell><TableCell align="right">Records</TableCell><TableCell sx={{ width: '48%' }}>Canonical CMDB type</TableCell></TableRow></TableHead><TableBody>{configurationTypesInScope.map(item => <TableRow key={item.id}><TableCell><Typography sx={{ fontWeight: 750 }}>{item.name}</Typography><Typography variant="caption" color="text.secondary">Immutable type ID {item.id}</Typography></TableCell><TableCell align="right">{item.count}</TableCell><TableCell><FormControl fullWidth size="small"><InputLabel id={`connectwise-type-map-${item.id}-label`}>CMDB type</InputLabel><Select labelId={`connectwise-type-map-${item.id}-label`} label="CMDB type" value={configurationPolicy.typeMappings[item.id] || ''} onChange={event => updateConfigurationTypeMapping(item.id, event.target.value)}><MenuItem value=""><em>Use ConnectWise type name</em></MenuItem>{canonicalAssetTypes.map(type => <MenuItem key={type} value={type}>{type}</MenuItem>)}</Select></FormControl></TableCell></TableRow>)}</TableBody></Table></TableContainer>}
                <FormControlLabel control={<Checkbox checked={configurationPolicy.blockUnmappedTypes} onChange={(_, checked) => updateConfigurationPolicy({ blockUnmappedTypes: checked })} />} label="Block unmapped configuration types from import" />
                {configurationTypesInScope.some(item => !configurationPolicy.typeMappings[item.id]) && <Alert severity={configurationPolicy.blockUnmappedTypes ? 'warning' : 'info'}>{configurationPolicy.blockUnmappedTypes ? 'Unmapped types will appear as conflicts and cannot be imported until a mapping is saved.' : 'Unmapped types retain their ConnectWise type name for compatibility. Enable blocking if every imported CI must use the canonical catalogue.'}</Alert>}
              </Stack></Paper></Grid>
              <Grid size={{ xs: 12, md: 3 }}><FormControl fullWidth><InputLabel id="connectwise-ci-status-mode-label">Status scope</InputLabel><Select id="connectwise-ci-status-mode" labelId="connectwise-ci-status-mode-label" label="Status scope" value={configurationPolicy.statusMode} onChange={event => updateConfigurationPolicy({ statusMode: event.target.value as ConnectWiseCiPolicy['statusMode'] })}><MenuItem value="all">All statuses</MenuItem><MenuItem value="selected">Selected statuses</MenuItem></Select></FormControl></Grid>
              <Grid size={{ xs: 12, md: 9 }}><FormControl fullWidth disabled={configurationPolicy.statusMode === 'all'}><InputLabel id="connectwise-ci-statuses-label">Included statuses</InputLabel><Select id="connectwise-ci-statuses" labelId="connectwise-ci-statuses-label" multiple label="Included statuses" value={configurationPolicy.includedStatusIds} onChange={event => updateConfigurationPolicy({ includedStatusIds: typeof event.target.value === 'string' ? event.target.value.split(',') : event.target.value })} renderValue={selected => configurationOptions.availableStatuses.filter(item => selected.includes(item.id)).map(item => item.name).join(', ')}>{configurationOptions.availableStatuses.map(item => <MenuItem key={item.id} value={item.id}><Checkbox checked={configurationPolicy.includedStatusIds.includes(item.id)} /><ListItemText primary={item.name} secondary={`${item.count} configuration(s) · ID ${item.id}`} /></MenuItem>)}</Select></FormControl></Grid>
              <Grid size={{ xs: 12, md: 6 }}><TextField fullWidth label="Excluded ConnectWise configuration IDs" value={configurationPolicy.excludedExternalIds.join(', ')} onChange={event => updateConfigurationPolicy({ excludedExternalIds: event.target.value.split(',').map(value => value.trim()).filter(Boolean) })} helperText="Optional immutable IDs, separated by commas." /></Grid>
              <Grid size={{ xs: 12, md: 3 }}><FormControl fullWidth><InputLabel id="connectwise-ci-sync-mode-label">Sync mode</InputLabel><Select id="connectwise-ci-sync-mode" labelId="connectwise-ci-sync-mode-label" label="Sync mode" value={configurationPolicy.syncMode} onChange={event => updateConfigurationPolicy({ syncMode: event.target.value as ConnectWiseCiPolicy['syncMode'], enabled: event.target.value === 'continuous_preview' ? configurationPolicy.enabled : false })}><MenuItem value="manual">Manual reviewed import</MenuItem><MenuItem value="continuous_preview">Continuous preview</MenuItem></Select></FormControl></Grid>
              <Grid size={{ xs: 12, md: 3 }}><TextField fullWidth type="number" label="Preview interval (minutes)" value={configurationPolicy.intervalMinutes} onChange={event => updateConfigurationPolicy({ intervalMinutes: Number(event.target.value) })} disabled={configurationPolicy.syncMode === 'manual'} slotProps={{ htmlInput: { min: 15, max: 10080 } }} /></Grid>
            </Grid>
            {configurationPolicy.syncMode === 'continuous_preview' && <FormControlLabel control={<Checkbox checked={configurationPolicy.enabled} onChange={(_, checked) => updateConfigurationPolicy({ enabled: checked })} />} label="Enable this policy for the continuous-preview worker" />}
            <Alert severity="info">Continuous preview stores a repeatable discovery scope for the worker and review queue. It never enables automatic canonical imports or provider writes.</Alert>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}><Button variant="outlined" disabled={!isAdmin || Boolean(busy)} onClick={() => void saveConfigurationPolicy()}>{busy === 'save-ci-policy' ? 'Saving…' : 'Save CI policy'}</Button><Button variant="contained" startIcon={busy === 'preview-configurations' ? <CircularProgress size={18} color="inherit" /> : <CloudSyncOutlined />} disabled={!configurationCompanyId || Boolean(busy) || (configurationPolicy.typeMode === 'selected' && !configurationPolicy.includedTypeIds.length) || (configurationPolicy.statusMode === 'selected' && !configurationPolicy.includedStatusIds.length)} onClick={() => void previewConfigurations()}>{busy === 'preview-configurations' ? 'Reconciling…' : 'Preview filtered CI changes'}</Button></Stack>
          </Stack></Paper>}
          {configurationOptions && <Paper variant="outlined" sx={{ p: 2 }}><Stack spacing={1.5}>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' } }}><Box><Typography variant="h6">Persistent review queue</Typography><Typography variant="body2" color="text.secondary">Current changes for this customer survive restarts. Dismissed evidence reopens automatically if the provider record changes.</Typography></Box><Stack direction="row" spacing={1}><Button size="small" variant="outlined" disabled={Boolean(busy)} onClick={() => void loadCiReviewQueue()}>Refresh queue</Button><Button size="small" variant="contained" startIcon={busy === 'sync-policy-now' ? <CircularProgress size={16} color="inherit" /> : <CloudSyncOutlined />} disabled={!configurationPolicy.id || Boolean(busy)} onClick={() => void syncSavedPolicyNow()}>{busy === 'sync-policy-now' ? 'Syncing…' : 'Sync now'}</Button></Stack></Stack>
          {!ciReviewItems.length ? <Alert severity="info">No persisted items are waiting for review. Run the saved policy to create or refresh the queue.</Alert> : <><Alert severity="warning">{ciReviewTotal} current item(s) require review. The first 50 are shown here; use the live preview below for controlled imports and identity linking.</Alert><TableContainer><Table size="small"><TableHead><TableRow><TableCell>Provider CI</TableCell><TableCell>Decision</TableCell><TableCell>Canonical evidence</TableCell><TableCell>Last observed</TableCell><TableCell align="right">Action</TableCell></TableRow></TableHead><TableBody>{ciReviewItems.slice(0, 50).map(item => <TableRow key={item.id}><TableCell><Typography sx={{ fontWeight: 750 }}>{item.externalName}</Typography><Typography variant="caption" color="text.secondary">{item.providerTypeName} · {item.providerStatusName} · ID {item.externalId}</Typography></TableCell><TableCell><Chip size="small" label={item.action} color={item.action === 'conflict' ? 'error' : item.action === 'update' ? 'warning' : item.action === 'link' ? 'info' : 'success'} /></TableCell><TableCell><Typography variant="body2">{item.assetName || 'No canonical target yet'}</Typography><Typography variant="caption" color="text.secondary">{item.reason}</Typography></TableCell><TableCell>{new Date(item.lastSeenAt).toLocaleString()}</TableCell><TableCell align="right"><Button size="small" color="inherit" disabled={!isAdmin || Boolean(busy)} onClick={() => { setDismissReviewItem(item); setDismissReviewNotes(''); }}>Dismiss</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer></>}
          </Stack></Paper>}
          {configurationPreview && <>
            <Alert severity={configurationPreview.counts.conflict ? 'warning' : 'success'}>{configurationPreview.message}</Alert>
            <Alert severity={configurationPreview.typeMappingSummary.blocked ? 'warning' : configurationPreview.typeMappingSummary.unmapped ? 'info' : 'success'}>{configurationPreview.typeMappingSummary.mapped} included record(s) use an explicit canonical type mapping · {configurationPreview.typeMappingSummary.unmapped} use no mapping{configurationPreview.typeMappingSummary.blocked ? ` · ${configurationPreview.typeMappingSummary.blocked} blocked from import` : ''}.</Alert>
            <Grid container spacing={1.5}>{([
              ['New', configurationPreview.counts.create, 'success'],
              ['Changed', configurationPreview.counts.update, 'warning'],
              ['Identity links', configurationPreview.counts.link, 'info'],
              ['Unchanged', configurationPreview.counts.unchanged, 'default'],
              ['Conflicts', configurationPreview.counts.conflict, 'error'],
            ] as const).map(([label, value, color]) => <Grid key={label} size={{ xs: 6, md: 2.4 }}><Paper variant="outlined" sx={{ p: 1.5 }}><Typography variant="h5" color={color === 'default' ? 'text.primary' : `${color}.main`}>{value}</Typography><Typography variant="caption" color="text.secondary">{label}</Typography></Paper></Grid>)}</Grid>
            <Grid container spacing={1.5}><Grid size={{ xs: 12, md: 7 }}><TextField fullWidth size="small" label="Search configurations" value={configurationSearch} onChange={event => { setConfigurationSearch(event.target.value); setConfigurationPage(0); }} /></Grid><Grid size={{ xs: 12, md: 5 }}><FormControl fullWidth size="small"><InputLabel>Decision filter</InputLabel><Select label="Decision filter" value={configurationActionFilter} onChange={event => { setConfigurationActionFilter(event.target.value); setConfigurationPage(0); }}><MenuItem value="reviewable">Reviewable changes</MenuItem><MenuItem value="all">All decisions</MenuItem><MenuItem value="create">New</MenuItem><MenuItem value="update">Changed</MenuItem><MenuItem value="link">Identity links</MenuItem><MenuItem value="unchanged">Unchanged</MenuItem><MenuItem value="conflict">Conflicts</MenuItem></Select></FormControl></Grid></Grid>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' } }}><FormControlLabel control={<Checkbox disabled={!selectableConfigurationItems.length || !isAdmin || Boolean(busy)} checked={selectableConfigurationItems.length > 0 && selectableConfigurationItems.every(item => selectedConfigurationIds.includes(item.externalId))} onChange={(_, checked) => { const visibleIds = new Set(selectableConfigurationItems.map(item => item.externalId)); setSelectedConfigurationIds(current => checked ? [...new Set([...current, ...visibleIds])] : current.filter(value => !visibleIds.has(value))); }} />} label={`${selectedConfigurationIds.length} approved · ${filteredConfigurationItems.length} filtered result(s)`} /><Button variant="contained" color="success" disabled={!isAdmin || !selectedConfigurationIds.length || Boolean(busy)} onClick={() => void importConfigurations()}>{busy === 'import-configurations' ? 'Importing reviewed items…' : `Import ${selectedConfigurationIds.length} selected`}</Button></Stack>
            <TableContainer><Table size="small"><TableHead><TableRow><TableCell padding="checkbox">Import</TableCell><TableCell>ConnectWise configuration</TableCell><TableCell>Decision</TableCell><TableCell>Canonical match</TableCell><TableCell>Evidence</TableCell><TableCell align="right">Identity</TableCell></TableRow></TableHead><TableBody>{visibleConfigurationItems.map(item => { const selectable = ['create', 'update', 'link'].includes(item.action); const manuallyLinkable = ['create', 'conflict'].includes(item.action); return <TableRow key={item.externalId} sx={{ opacity: item.action === 'unchanged' ? 0.65 : 1 }}><TableCell padding="checkbox"><Checkbox checked={selectedConfigurationIds.includes(item.externalId)} disabled={!selectable || !isAdmin || Boolean(busy)} onChange={(_, checked) => setSelectedConfigurationIds(current => checked ? [...current, item.externalId] : current.filter(value => value !== item.externalId))} aria-label={`Import ${item.name}`} /></TableCell><TableCell><Typography sx={{ fontWeight: 750 }}>{item.name}</Typography><Typography variant="caption" color="text.secondary">{item.record.providerTypeName} → {item.record.type} · {item.record.providerStatusName} · provider ID {item.externalId}</Typography></TableCell><TableCell><Chip size="small" label={item.action} color={item.action === 'create' ? 'success' : item.action === 'update' ? 'warning' : item.action === 'link' ? 'info' : item.action === 'conflict' ? 'error' : 'default'} /></TableCell><TableCell>{item.assetName || `New ${item.record.type}`}</TableCell><TableCell><Typography variant="body2">{item.reason}</Typography>{item.changedFields.length > 0 && <Typography variant="caption" color="text.secondary">Changes: {item.changedFields.join(', ')}</Typography>}</TableCell><TableCell align="right">{manuallyLinkable && <Button size="small" startIcon={<LinkOutlined />} disabled={!isAdmin || Boolean(busy)} onClick={() => void openLinkConfiguration(item)}>Link existing</Button>}</TableCell></TableRow>; })}</TableBody></Table></TableContainer>
            <TablePagination component="div" count={filteredConfigurationItems.length} page={configurationPage} onPageChange={(_, page) => setConfigurationPage(page)} rowsPerPage={configurationRowsPerPage} onRowsPerPageChange={event => { setConfigurationRowsPerPage(Number(event.target.value)); setConfigurationPage(0); }} rowsPerPageOptions={[10, 25, 50, 100]} />
            {configurationPreview.counts.conflict > 0 && <Alert severity="info">Conflicts are deliberately excluded from import. Use “Link existing” to confirm the canonical CI by immutable provider ID, then refresh the preview.</Alert>}
          </>}
          <Paper variant="outlined" sx={{ overflow: 'hidden' }}>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ justifyContent: 'space-between', alignItems: { sm: 'center' }, p: 2 }}>
              <Box><Typography variant="h6">ConnectWise sync history</Typography><Typography variant="body2" color="text.secondary">Recent company discovery, continuous previews, manual runs and reviewed imports.</Typography></Box>
              <Button size="small" variant="outlined" disabled={Boolean(busy)} onClick={() => void load()}>Refresh history</Button>
            </Stack>
            {!syncRuns.length ? <Alert severity="info" sx={{ m: 2, mt: 0 }}>No ConnectWise sync runs have been recorded.</Alert> : <TableContainer><Table size="small"><TableHead><TableRow><TableCell>Started</TableCell><TableCell>Customer</TableCell><TableCell>Operation</TableCell><TableCell>Status</TableCell><TableCell>Summary</TableCell></TableRow></TableHead><TableBody>{syncRuns.map(run => <TableRow key={run.id}><TableCell>{run.startedAt ? new Date(run.startedAt).toLocaleString() : 'Unknown'}</TableCell><TableCell>{workspace.companies.find(company => company.id === run.attributes?.companyId)?.name || run.attributes?.companyId || 'MSP scope'}</TableCell><TableCell>{String(run.attributes?.operation || 'provider_sync').replaceAll('_', ' ')}</TableCell><TableCell><Chip size="small" label={run.status.replaceAll('_', ' ')} color={run.status === 'success' ? 'success' : run.status === 'failed' ? 'error' : run.status === 'review_required' ? 'warning' : 'default'} /></TableCell><TableCell><Typography variant="body2">{run.message}</Typography><Typography variant="caption" color="text.secondary">{run.discovered || 0} discovered · {run.review || 0} review · {run.imported || 0} created · {run.updated || 0} updated</Typography></TableCell></TableRow>)}</TableBody></Table></TableContainer>}
          </Paper>
          <Typography variant="caption" color="text.secondary">Preview and import re-read ConnectWise so stale selections cannot silently apply. Only CMDB records and immutable mapping evidence are changed.</Typography>
        </Stack>}

        <Divider sx={{ my: 3 }} /><Stack direction="row" spacing={1} sx={{ justifyContent: 'space-between' }}><Button disabled={activeStep === 0} onClick={() => setActiveStep(step => step - 1)}>Back</Button><Button variant="contained" disabled={activeStep === steps.length - 1 || (activeStep === 1 && !connectWise.configured) || (activeStep === 2 && connectWise.connectionStatus !== 'verified' && !testResult) || (activeStep === 3 && (!preview || previewStale))} onClick={() => setActiveStep(step => step + 1)}>Continue</Button></Stack>
      </CardContent></Card>}

      <Dialog
        open={Boolean(createCustomerItem)}
        onClose={() => {
          if (!busy.startsWith('create-')) setCreateCustomerItem(null);
        }}
        fullWidth
        maxWidth="sm"
      >
        <Stack component="form" onSubmit={createCustomerAndMap}>
          <DialogTitle>Create customer from ConnectWise</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ pt: 1 }}>
              {createCustomerItem && (
                <Alert severity="info">
                  Create an isolated CMDB customer for <strong>{createCustomerItem.name}</strong> and explicitly
                  map provider ID {createCustomerItem.externalId}. Nothing will be written to ConnectWise.
                </Alert>
              )}
              <TextField
                fullWidth
                required
                label="Customer name"
                value={createCustomerName}
                onChange={event => setCreateCustomerName(event.target.value)}
              />
              <TextField
                fullWidth
                label="Customer ID"
                value={createCustomerSlug}
                onChange={event => setCreateCustomerSlug(event.target.value)}
                helperText="Lowercase letters, numbers and hyphens. Leave blank to generate it from the name."
                slotProps={{ htmlInput: { pattern: '[a-z0-9-]*' } }}
              />
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setCreateCustomerItem(null)} disabled={busy.startsWith('create-')}>Cancel</Button>
            <Button
              type="submit"
              variant="contained"
              startIcon={<AddBusinessOutlined />}
              disabled={!createCustomerName.trim() || busy.startsWith('create-')}
            >
              {busy.startsWith('create-') ? 'Creating and mapping…' : 'Create and map'}
            </Button>
          </DialogActions>
        </Stack>
      </Dialog>

      <Dialog open={Boolean(linkConfigurationItem)} onClose={() => { if (busy !== 'link-configuration') setLinkConfigurationItem(null); }} fullWidth maxWidth="sm">
        <DialogTitle>Link ConnectWise identity to an existing CI</DialogTitle>
        <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
          {linkConfigurationItem && <Alert severity="warning">Confirm that <strong>{linkConfigurationItem.name}</strong> and the selected CMDB item are the same real object. Names are supporting evidence only; the durable link will use ConnectWise ID {linkConfigurationItem.externalId}.</Alert>}
          <Autocomplete
            options={linkAssets}
            value={linkAssets.find(asset => asset.id === linkAssetId) || null}
            onChange={(_, asset) => setLinkAssetId(asset?.id || '')}
            getOptionLabel={asset => `${asset.name} · ${asset.type}`}
            isOptionEqualToValue={(option, value) => option.id === value.id}
            renderInput={params => <TextField {...params} label="Existing canonical CI" placeholder="Search by name or type" />}
          />
          <Typography variant="caption" color="text.secondary">The link is customer-scoped, audited and can later be joined by N-central or Passportal identities. No record is changed in ConnectWise.</Typography>
        </Stack></DialogContent>
        <DialogActions><Button onClick={() => setLinkConfigurationItem(null)} disabled={busy === 'link-configuration'}>Cancel</Button><Button variant="contained" startIcon={<LinkOutlined />} disabled={!linkAssetId || busy === 'link-configuration'} onClick={() => void linkConfiguration()}>{busy === 'link-configuration' ? 'Linking…' : 'Confirm identity link'}</Button></DialogActions>
      </Dialog>

      <Dialog open={Boolean(dismissReviewItem)} onClose={() => { if (!busy.startsWith('dismiss-review-')) setDismissReviewItem(null); }} fullWidth maxWidth="sm">
        <DialogTitle>Dismiss CI review observation</DialogTitle>
        <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
          {dismissReviewItem && <Alert severity="warning">Dismiss <strong>{dismissReviewItem.externalName}</strong> only when this observation does not require a CMDB change. A changed provider record will automatically return it to the pending queue.</Alert>}
          <TextField fullWidth required multiline minRows={3} label="Decision notes" value={dismissReviewNotes} onChange={event => setDismissReviewNotes(event.target.value)} helperText="Record the operational reason for the audit trail." />
        </Stack></DialogContent>
        <DialogActions><Button onClick={() => setDismissReviewItem(null)} disabled={busy.startsWith('dismiss-review-')}>Cancel</Button><Button variant="contained" color="warning" disabled={dismissReviewNotes.trim().length < 4 || busy.startsWith('dismiss-review-')} onClick={() => void dismissCiReview()}>{busy.startsWith('dismiss-review-') ? 'Dismissing…' : 'Dismiss observation'}</Button></DialogActions>
      </Dialog>

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
