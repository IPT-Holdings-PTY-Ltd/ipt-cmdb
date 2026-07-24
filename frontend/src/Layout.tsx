import AccountTreeOutlined from '@mui/icons-material/AccountTreeOutlined';
import AddBusinessOutlined from '@mui/icons-material/AddBusinessOutlined';
import AssignmentOutlined from '@mui/icons-material/AssignmentOutlined';
import CloudSyncOutlined from '@mui/icons-material/CloudSyncOutlined';
import GroupsOutlined from '@mui/icons-material/GroupsOutlined';
import ManageAccountsOutlined from '@mui/icons-material/ManageAccountsOutlined';
import PaletteOutlined from '@mui/icons-material/PaletteOutlined';
import SecurityOutlined from '@mui/icons-material/SecurityOutlined';
import StorageOutlined from '@mui/icons-material/StorageOutlined';
import BusinessCenterOutlined from '@mui/icons-material/BusinessCenterOutlined';
import AssessmentOutlined from '@mui/icons-material/AssessmentOutlined';
import HistoryOutlined from '@mui/icons-material/HistoryOutlined';
import FactCheckOutlined from '@mui/icons-material/FactCheckOutlined';
import ContactsOutlined from '@mui/icons-material/ContactsOutlined';
import EmailOutlined from '@mui/icons-material/EmailOutlined';
import NotificationsActiveOutlined from '@mui/icons-material/NotificationsActiveOutlined';
import CompareArrowsOutlined from '@mui/icons-material/CompareArrowsOutlined';
import LibraryBooksOutlined from '@mui/icons-material/LibraryBooksOutlined';
import { Box, FormControl, MenuItem, Select, Stack, Typography } from '@mui/material';
import { AppBar, Layout, Menu, useRefresh, type LayoutProps } from 'react-admin';
import { useWorkspace } from './workspace';
import { getSession } from './session';
import { BrandLogo, useMspBranding } from './branding';

function WorkspaceAppBar() {
  const workspace = useWorkspace();
  const { brand } = useMspBranding();
  const refresh = useRefresh();
  const rootAllowed = ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');
  return (
    <AppBar color="inherit" elevation={0}>
      <Stack
        direction="row"
        spacing={1.5}
        sx={{
          alignItems: "center",
          flex: 1,
          minWidth: 0
        }}><BrandLogo size={38} borderRadius={8} /><Box sx={{ minWidth: 0 }}>
        <Typography variant="caption" sx={{
          color: "text.secondary"
        }}>{brand.name.toUpperCase()} · {workspace.isRoot ? 'MSP WORKSPACE' : 'CUSTOMER WORKSPACE'}</Typography>
        <Typography variant="h6" noWrap>{workspace.companyName}</Typography>
      </Box></Stack>
      <FormControl size="small" sx={{ minWidth: 230 }}>
        <Select value={workspace.companyId} onChange={event => { workspace.setCompanyId(event.target.value); refresh(); }} aria-label="Workspace">
          {rootAllowed && <MenuItem value="__root__">MSP / Root level</MenuItem>}
          {workspace.companies.map(company => <MenuItem key={company.id} value={company.id}>{company.name}</MenuItem>)}
        </Select>
      </FormControl>
    </AppBar>
  );
}

function WorkspaceMenu() {
  const workspace = useWorkspace();
  const role = getSession()?.user.role;
  const platformAdmin = role === 'platform_admin';
  const rootRole = ['platform_admin', 'msp_operator'].includes(role || '');
  const section = (label: string) => <Typography
    key={label}
    variant="overline"
    sx={{
      color: "text.secondary",
      px: 2,
      pt: 2,
      pb: 0.5,
      display: 'block',
      fontSize: '0.65rem',
      letterSpacing: '0.12em'
    }}>{label}</Typography>;
  return <Menu>
    {section('Monitor')}
    <Menu.DashboardItem />
    {!workspace.isRoot && <>
      {section('CMDB')}
      <Menu.Item to="/business-systems" primaryText="Business systems" leftIcon={<BusinessCenterOutlined />} />
      <Menu.ResourceItem name="assets" />
      <Menu.Item to="/relationships" primaryText="Relationships" leftIcon={<AccountTreeOutlined />} />
      <Menu.Item to="/changes" primaryText="Change control" leftIcon={<AssignmentOutlined />} />
      <Menu.Item to="/contacts" primaryText="Contacts" leftIcon={<ContactsOutlined />} />
      {section('Governance')}
      <Menu.Item to="/governance/audit" primaryText="Audit activity" leftIcon={<HistoryOutlined />} />
      <Menu.Item to="/governance/data-quality" primaryText="Data quality" leftIcon={<FactCheckOutlined />} />
      <Menu.Item to="/governance/reports" primaryText="Reports" leftIcon={<AssessmentOutlined />} />
      {rootRole && <>{section('Access')}<Menu.Item to="/customer/users" primaryText="Users & permissions" leftIcon={<ManageAccountsOutlined />} /></>}
    </>}
    {workspace.isRoot && rootRole && <>
      {section('Organisation')}
      <Menu.Item to="/contacts" primaryText="Contact directory" leftIcon={<ContactsOutlined />} />
      {platformAdmin && <Menu.Item to="/admin/customers" primaryText="Customers" leftIcon={<AddBusinessOutlined />} />}
      {platformAdmin && <Menu.Item to="/admin/customer-groups" primaryText="Customer groups" leftIcon={<GroupsOutlined />} />}
      {section('Access')}
      <Menu.Item to="/admin/users" primaryText="Users" leftIcon={<ManageAccountsOutlined />} />
      {platformAdmin && <Menu.Item to="/admin/rbac" primaryText="Access control" leftIcon={<SecurityOutlined />} />}
      {section('Operations')}
      <Menu.Item to="/admin/change-templates" primaryText="Change templates" leftIcon={<LibraryBooksOutlined />} />
      <Menu.Item to="/admin/integrations" primaryText="Integrations" leftIcon={<CloudSyncOutlined />} />
      <Menu.Item to="/admin/reconciliation" primaryText="Reconciliation" leftIcon={<CompareArrowsOutlined />} />
      {platformAdmin && <Menu.Item to="/admin/email" primaryText="Email delivery" leftIcon={<EmailOutlined />} />}
      {platformAdmin && <Menu.Item to="/admin/notifications" primaryText="Notifications" leftIcon={<NotificationsActiveOutlined />} />}
      {section('Governance')}
      <Menu.Item to="/governance/audit" primaryText="Audit activity" leftIcon={<HistoryOutlined />} />
      <Menu.Item to="/governance/data-quality" primaryText="Data quality" leftIcon={<FactCheckOutlined />} />
      <Menu.Item to="/governance/reports" primaryText="Reports" leftIcon={<AssessmentOutlined />} />
      {section('Settings')}
      <Menu.Item to="/admin/branding" primaryText="Branding" leftIcon={<PaletteOutlined />} />
      {platformAdmin && <Menu.Item to="/admin/database" primaryText="Database & recovery" leftIcon={<StorageOutlined />} />}
    </>}
    {section('Account')}
    <Menu.Item to="/profile/security" primaryText="My security" leftIcon={<SecurityOutlined />} />
  </Menu>;
}

export function WorkspaceLayout(props: LayoutProps) {
  return <Layout {...props} appBar={WorkspaceAppBar} menu={WorkspaceMenu} />;
}
