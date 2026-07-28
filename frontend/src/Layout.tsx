import AccountTreeOutlined from '@mui/icons-material/AccountTreeOutlined';
import AddBusinessOutlined from '@mui/icons-material/AddBusinessOutlined';
import AssessmentOutlined from '@mui/icons-material/AssessmentOutlined';
import AssignmentOutlined from '@mui/icons-material/AssignmentOutlined';
import BusinessCenterOutlined from '@mui/icons-material/BusinessCenterOutlined';
import CloudSyncOutlined from '@mui/icons-material/CloudSyncOutlined';
import CompareArrowsOutlined from '@mui/icons-material/CompareArrowsOutlined';
import ContactsOutlined from '@mui/icons-material/ContactsOutlined';
import DashboardOutlined from '@mui/icons-material/DashboardOutlined';
import EmailOutlined from '@mui/icons-material/EmailOutlined';
import FactCheckOutlined from '@mui/icons-material/FactCheckOutlined';
import GroupsOutlined from '@mui/icons-material/GroupsOutlined';
import HistoryOutlined from '@mui/icons-material/HistoryOutlined';
import Inventory2Outlined from '@mui/icons-material/Inventory2Outlined';
import LibraryBooksOutlined from '@mui/icons-material/LibraryBooksOutlined';
import LogoutOutlined from '@mui/icons-material/LogoutOutlined';
import ManageAccountsOutlined from '@mui/icons-material/ManageAccountsOutlined';
import MenuOutlined from '@mui/icons-material/MenuOutlined';
import NotificationsActiveOutlined from '@mui/icons-material/NotificationsActiveOutlined';
import PaletteOutlined from '@mui/icons-material/PaletteOutlined';
import SecurityOutlined from '@mui/icons-material/SecurityOutlined';
import StorageOutlined from '@mui/icons-material/StorageOutlined';
import {
  AppBar,
  Avatar,
  Box,
  Divider,
  Drawer,
  FormControl,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  ListSubheader,
  MenuItem,
  Select,
  Stack,
  Toolbar,
  Tooltip,
  Typography,
} from '@mui/material';
import { Suspense, useState, type ReactNode } from 'react';
import { Link, Outlet, useLocation } from 'react-router';
import { logout } from './authProvider';
import { BrandLogo, useMspBranding } from './branding';
import { RouteErrorBoundary, RouteLoadingFallback } from './RouteFeedback';
import { getSession } from './session';
import { useWorkspace } from './workspace';

const drawerWidth = 272;

export type NavigationItem = {
  path: string;
  label: string;
  icon: ReactNode;
  exact?: boolean;
};

export function NavigationLink({ item, close }: { item: NavigationItem; close: () => void }) {
  const location = useLocation();
  const active = item.exact ? location.pathname === item.path : location.pathname.startsWith(item.path);
  return (
    <ListItemButton
      component={Link}
      to={item.path}
      selected={active}
      onClick={close}
      sx={{ mx: 1, my: 0.25, borderRadius: 1.5 }}
    >
      <ListItemIcon sx={{ minWidth: 38, color: active ? 'primary.main' : 'text.secondary' }}>{item.icon}</ListItemIcon>
      <ListItemText primary={item.label} slotProps={{ primary: { sx: { fontSize: '.88rem', fontWeight: active ? 800 : 650 } } }} />
    </ListItemButton>
  );
}

function NavigationSection({ label, items, close }: { label: string; items: NavigationItem[]; close: () => void }) {
  if (!items.length) return null;
  return (
    <List
      dense
      subheader={(
        <ListSubheader disableSticky sx={{ bgcolor: 'transparent', color: 'text.secondary', fontSize: '.65rem', fontWeight: 850, letterSpacing: '.12em', lineHeight: '34px' }}>
          {label.toUpperCase()}
        </ListSubheader>
      )}
    >
      {items.map(item => <NavigationLink key={item.path} item={item} close={close} />)}
    </List>
  );
}

function WorkspaceNavigation({ close }: { close: () => void }) {
  const workspace = useWorkspace();
  const { brand } = useMspBranding();
  const user = getSession()?.user;
  const platformAdmin = user?.role === 'platform_admin';
  const rootRole = ['platform_admin', 'msp_operator'].includes(user?.role || '');

  const monitor: NavigationItem[] = [{ path: '/', label: workspace.isRoot ? 'MSP overview' : 'Customer overview', icon: <DashboardOutlined />, exact: true }];
  const cmdb: NavigationItem[] = !workspace.isRoot ? [
    { path: '/business-systems', label: 'Business systems', icon: <BusinessCenterOutlined /> },
    { path: '/assets', label: 'Assets', icon: <Inventory2Outlined /> },
    { path: '/relationships', label: 'Relationships', icon: <AccountTreeOutlined /> },
    { path: '/changes', label: 'Change control', icon: <AssignmentOutlined /> },
    { path: '/contacts', label: 'Contacts', icon: <ContactsOutlined /> },
  ] : [];
  const organisation: NavigationItem[] = workspace.isRoot && rootRole ? [
    { path: '/contacts', label: 'Contact directory', icon: <ContactsOutlined /> },
    ...(platformAdmin ? [{ path: '/admin/customers', label: 'Customers', icon: <AddBusinessOutlined /> }] : []),
    ...(platformAdmin ? [{ path: '/admin/customer-groups', label: 'Customer groups', icon: <GroupsOutlined /> }] : []),
  ] : [];
  const access: NavigationItem[] = workspace.isRoot && rootRole ? [
    { path: '/admin/users', label: 'Users', icon: <ManageAccountsOutlined /> },
    ...(platformAdmin ? [{ path: '/admin/rbac', label: 'Access control', icon: <SecurityOutlined /> }] : []),
  ] : !workspace.isRoot && rootRole ? [
    { path: '/customer/users', label: 'Users & permissions', icon: <ManageAccountsOutlined /> },
  ] : [];
  const operations: NavigationItem[] = workspace.isRoot && rootRole ? [
    { path: '/admin/change-templates', label: 'Change templates', icon: <LibraryBooksOutlined /> },
    { path: '/admin/integrations', label: 'Integrations', icon: <CloudSyncOutlined /> },
    { path: '/admin/reconciliation', label: 'Reconciliation', icon: <CompareArrowsOutlined /> },
    ...(platformAdmin ? [{ path: '/admin/email', label: 'Email delivery', icon: <EmailOutlined /> }] : []),
    ...(platformAdmin ? [{ path: '/admin/notifications', label: 'Notifications', icon: <NotificationsActiveOutlined /> }] : []),
  ] : [];
  const governance: NavigationItem[] = [
    { path: '/governance/audit', label: 'Audit activity', icon: <HistoryOutlined /> },
    { path: '/governance/data-quality', label: 'Data quality', icon: <FactCheckOutlined /> },
    { path: '/governance/reports', label: 'Reports', icon: <AssessmentOutlined /> },
  ];
  const settings: NavigationItem[] = workspace.isRoot && rootRole ? [
    { path: '/admin/branding', label: 'Branding', icon: <PaletteOutlined /> },
    ...(platformAdmin ? [{ path: '/admin/database', label: 'Database & recovery', icon: <StorageOutlined /> }] : []),
  ] : [];

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Box sx={{ p: 2, pb: 1.5 }}>
        <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center', mb: 1.75 }}>
          <BrandLogo size={40} borderRadius={10} />
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{ fontWeight: 850 }} noWrap>{brand.name}</Typography>
            <Typography variant="caption" color="text.secondary">Asset intelligence</Typography>
          </Box>
        </Stack>
        <FormControl size="small" fullWidth>
          <Select
            value={workspace.companyId}
            onChange={event => workspace.setCompanyId(event.target.value)}
            aria-label="Workspace"
            sx={{ bgcolor: 'background.default', fontWeight: 750 }}
          >
            {rootRole && <MenuItem value="__root__">MSP / Root level</MenuItem>}
            {workspace.companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}
          </Select>
        </FormControl>
      </Box>
      <Divider />
      <Box sx={{ flex: 1, overflowY: 'auto', py: 0.5 }}>
        <NavigationSection label="Monitor" items={monitor} close={close} />
        <NavigationSection label="CMDB" items={cmdb} close={close} />
        <NavigationSection label="Organisation" items={organisation} close={close} />
        <NavigationSection label="Access" items={access} close={close} />
        <NavigationSection label="Operations" items={operations} close={close} />
        <NavigationSection label="Governance" items={governance} close={close} />
        <NavigationSection label="Settings" items={settings} close={close} />
        <NavigationSection label="Account" items={[{ path: '/profile/security', label: 'My security', icon: <SecurityOutlined /> }]} close={close} />
      </Box>
    </Box>
  );
}

/**
 * Responsive MSP workspace shell with tenant selection, role-aware navigation,
 * and an explicit sign-out action.
 */
export function WorkspaceLayout() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const workspace = useWorkspace();
  const user = getSession()?.user;
  const drawer = <WorkspaceNavigation close={() => setMobileOpen(false)} />;

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh' }}>
      <AppBar
        position="fixed"
        color="inherit"
        elevation={0}
        sx={{
          width: { md: `calc(100% - ${drawerWidth}px)` },
          ml: { md: `${drawerWidth}px` },
          borderBottom: '1px solid',
          borderColor: 'divider',
          bgcolor: '#0d1728e8',
          backdropFilter: 'blur(14px)',
        }}
      >
        <Toolbar sx={{ minHeight: '72px !important', gap: 1.5 }}>
          <IconButton onClick={() => setMobileOpen(true)} sx={{ display: { md: 'none' } }} aria-label="Open navigation"><MenuOutlined /></IconButton>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography variant="caption" color="text.secondary">{workspace.isRoot ? 'MSP WORKSPACE' : 'CUSTOMER WORKSPACE'}</Typography>
            <Typography variant="h6" noWrap>{workspace.companyName}</Typography>
          </Box>
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <Avatar sx={{ width: 34, height: 34, bgcolor: 'primary.main', color: 'primary.contrastText', fontSize: '.8rem', fontWeight: 850 }}>
              {(user?.displayName || user?.email || 'U').slice(0, 2).toUpperCase()}
            </Avatar>
            <Box sx={{ display: { xs: 'none', sm: 'block' }, maxWidth: 210 }}>
              <Typography variant="body2" noWrap sx={{ fontWeight: 750 }}>{user?.displayName || user?.email}</Typography>
              <Typography variant="caption" color="text.secondary">{user?.role.replaceAll('_', ' ')}</Typography>
            </Box>
            <Tooltip title="Sign out">
              <IconButton aria-label="Sign out" onClick={() => void logout()}><LogoutOutlined /></IconButton>
            </Tooltip>
          </Stack>
        </Toolbar>
      </AppBar>
      <Box component="nav" sx={{ width: { md: drawerWidth }, flexShrink: { md: 0 } }} aria-label="Workspace navigation">
        <Drawer
          variant="temporary"
          open={mobileOpen}
          onClose={() => setMobileOpen(false)}
          ModalProps={{ keepMounted: true }}
          sx={{ display: { xs: 'block', md: 'none' }, '& .MuiDrawer-paper': { width: drawerWidth } }}
        >
          {drawer}
        </Drawer>
        <Drawer
          variant="permanent"
          open
          sx={{
            display: { xs: 'none', md: 'block' },
            '& .MuiDrawer-paper': { width: drawerWidth, borderRightColor: 'divider', bgcolor: '#0d1728' },
          }}
        >
          {drawer}
        </Drawer>
      </Box>
      <Box
        component="main"
        sx={{
          flexGrow: 1,
          width: { xs: '100%', md: `calc(100% - ${drawerWidth}px)` },
          minWidth: 0,
          pt: '72px',
        }}
      >
        <Box sx={{ width: '100%', maxWidth: 1760, mx: 'auto', p: { xs: 2, sm: 2.5, lg: 3.5 } }}>
          <RouteErrorBoundary key={`${location.pathname}${location.search}`}>
            <Suspense fallback={<RouteLoadingFallback />}>
              <Outlet />
            </Suspense>
          </RouteErrorBoundary>
        </Box>
      </Box>
    </Box>
  );
}
