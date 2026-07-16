import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import PersonAddAltOutlined from '@mui/icons-material/PersonAddAltOutlined';
import SecurityOutlined from '@mui/icons-material/SecurityOutlined';
import { Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Grid, List, ListItem, ListItemIcon, ListItemText, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useEffect, useState, type FormEvent } from 'react';
import { Title } from 'react-admin';
import { Navigate } from 'react-router-dom';
import { apiFetch, getSession } from './session';
import type { User } from './types';
import { useWorkspace } from './workspace';

type Notice = { severity: 'success' | 'error'; message: string } | null;

export function CustomerUsersPage() {
  const workspace = useWorkspace();
  const role = getSession()?.user.role;
  const canManageUsers = ['platform_admin', 'msp_operator'].includes(role || '');
  const [users, setUsers] = useState<User[]>([]);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);

  const loadUsers = async () => {
    setLoading(true);
    try {
      const records = await apiFetch<User[]>(`/api/users?companyId=${encodeURIComponent(workspace.companyId)}`);
      setUsers(records);
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Customer users could not be loaded.' });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { if (!workspace.isRoot && canManageUsers) void loadUsers(); }, [workspace.companyId, workspace.isRoot]);

  async function createCustomerUser(event: FormEvent) {
    event.preventDefault(); setSaving(true); setNotice(null);
    try {
      await apiFetch<User>('/api/users', {
        method: 'POST',
        body: JSON.stringify({ accountType: 'customer', companyId: workspace.companyId, email, password }),
      });
      setEmail(''); setPassword('');
      setNotice({ severity: 'success', message: `${email} now has reader access to ${workspace.companyName}.` });
      await loadUsers();
    } catch (error) {
      setNotice({ severity: 'error', message: error instanceof Error ? error.message : 'Customer user could not be created.' });
    } finally {
      setSaving(false);
    }
  }

  if (workspace.isRoot) return <Navigate to="/admin/users" replace />;
  if (!canManageUsers) return <Navigate to="/" replace />;

  return (
    <Box>
      <Title title={`${workspace.companyName} users`} />
      <Typography variant="overline" color="primary">Customer access</Typography>
      <Typography variant="h3">Users &amp; permissions</Typography>
      <Typography
        sx={{
          color: "text.secondary",
          mt: 0.75,
          mb: 3,
          maxWidth: 800
        }}>
        Manage access only for {workspace.companyName}. This page never exposes MSP access groups or other customer tenants.
      </Typography>
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.message}</Alert>}

      <Grid container spacing={3}>
        <Grid size={{ xs: 12, xl: 5 }}>
          <Card><CardContent>
            <Stack component="form" spacing={2} onSubmit={createCustomerUser}>
              <Box><Typography variant="h5">Add customer user</Typography><Typography sx={{
                color: "text.secondary"
              }}>The account is permanently scoped to this selected customer.</Typography></Box>
              <TextField label="Customer" value={workspace.companyName} disabled />
              <TextField label="Permission level" value="Customer reader" disabled helperText="Custom customer roles are planned for a later RBAC phase." />
              <TextField label="Email" type="email" value={email} onChange={event => setEmail(event.target.value)} required />
              <TextField label="Temporary password" type="password" value={password} onChange={event => setPassword(event.target.value)} slotProps={{ htmlInput: { minLength: 8 } }} helperText="At least 8 characters; local authentication only" required />
              <Button type="submit" variant="contained" startIcon={<PersonAddAltOutlined />} disabled={saving}>{saving ? 'Creating…' : 'Create customer user'}</Button>
            </Stack>
          </CardContent></Card>
        </Grid>

        <Grid size={{ xs: 12, xl: 7 }}>
          <Stack spacing={3}>
            <Card><CardContent>
              <Typography variant="h5">Users with access</Typography>
              <Typography
                sx={{
                  color: "text.secondary",
                  mb: 2
                }}>Includes MSP operators and platform administrators who can enter this customer workspace.</Typography>
              {loading ? <CircularProgress size={24} /> : <TableContainer><Table size="small">
                <TableHead><TableRow><TableCell>User</TableCell><TableCell>Role</TableCell><TableCell>Scope</TableCell></TableRow></TableHead>
                <TableBody>{users.map(user => <TableRow key={user.id}><TableCell>{user.email}</TableCell><TableCell><Chip size="small" label={user.role.replaceAll('_', ' ')} /></TableCell><TableCell>{user.role === 'platform_admin' ? 'All customers' : user.role === 'msp_operator' ? 'Assigned MSP customers' : workspace.companyName}</TableCell></TableRow>)}</TableBody>
              </Table></TableContainer>}
            </CardContent></Card>

            <Card><CardContent>
              <Stack direction="row" spacing={1.5} sx={{
                alignItems: "center"
              }}><SecurityOutlined color="primary" /><Typography variant="h5">Customer reader permissions</Typography></Stack>
              <List dense>{['View this customer’s configuration items', 'View this customer’s relationships and impact map', 'No access to another customer or the MSP workspace', 'No asset, integration, user, or platform configuration changes'].map(permission => <ListItem key={permission} disableGutters><ListItemIcon sx={{ minWidth: 30 }}><CheckCircleOutlined color="success" fontSize="small" /></ListItemIcon><ListItemText primary={permission} /></ListItem>)}</List>
            </CardContent></Card>
          </Stack>
        </Grid>
      </Grid>
    </Box>
  );
}
