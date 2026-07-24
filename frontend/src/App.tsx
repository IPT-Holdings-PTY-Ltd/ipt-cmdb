import Inventory2Outlined from '@mui/icons-material/Inventory2Outlined';
import { Box, CircularProgress } from '@mui/material';
import { createTheme } from '@mui/material/styles';
import { Admin, CustomRoutes, Resource } from 'react-admin';
import { Route } from 'react-router-dom';
import { authProvider } from './authProvider';
import { dataProvider } from './dataProvider';
import { WorkspaceLayout } from './Layout';
import { LoginPage } from './LoginPage';
import { WorkspaceProvider } from './workspace';
import { BrandingProvider, useMspBranding } from './branding';
import { lazy, Suspense, useEffect, useMemo, useState } from 'react';

const AssetList = lazy(() => import('./assets').then(module => ({ default: module.AssetList })));
const AssetShow = lazy(() => import('./assets').then(module => ({ default: module.AssetShow })));
const AssetEdit = lazy(() => import('./assets').then(module => ({ default: module.AssetEdit })));
const AssetCreate = lazy(() => import('./assets').then(module => ({ default: module.AssetCreate })));
const Dashboard = lazy(() => import('./Dashboard').then(module => ({ default: module.Dashboard })));
const BusinessSystemsPage = lazy(() => import('./BusinessSystems').then(module => ({ default: module.BusinessSystemsPage })));
const Relationships = lazy(() => import('./Relationships').then(module => ({ default: module.Relationships })));
const ChangeControlPage = lazy(() => import('./ChangeControl').then(module => ({ default: module.ChangeControlPage })));
const ContactsPage = lazy(() => import('./Contacts').then(module => ({ default: module.ContactsPage })));
const AuditCenterPage = lazy(() => import('./Governance').then(module => ({ default: module.AuditCenterPage })));
const ReportsPage = lazy(() => import('./Governance').then(module => ({ default: module.ReportsPage })));
const DataQualityPage = lazy(() => import('./DataQuality').then(module => ({ default: module.DataQualityPage })));
const ReconciliationPage = lazy(() => import('./Reconciliation').then(module => ({ default: module.ReconciliationPage })));
const CustomerUsersPage = lazy(() => import('./CustomerUsers').then(module => ({ default: module.CustomerUsersPage })));
const CustomersPage = lazy(() => import('./RootAdmin').then(module => ({ default: module.CustomersPage })));
const CustomerGroupsPage = lazy(() => import('./RootAdmin').then(module => ({ default: module.CustomerGroupsPage })));
const UsersPage = lazy(() => import('./RootAdmin').then(module => ({ default: module.UsersPage })));
const RbacPage = lazy(() => import('./RootAdmin').then(module => ({ default: module.RbacPage })));
const IntegrationsPage = lazy(() => import('./Integrations').then(module => ({ default: module.IntegrationsPage })));
const ConnectWiseIntegrationPage = lazy(() => import('./RootAdmin').then(module => ({ default: module.ConnectWiseIntegrationPage })));
const EmailPage = lazy(() => import('./EmailSetup').then(module => ({ default: module.EmailPage })));
const NotificationsPage = lazy(() => import('./Notifications').then(module => ({ default: module.NotificationsPage })));
const BrandingPage = lazy(() => import('./RootAdmin').then(module => ({ default: module.BrandingPage })));
const DatabasePage = lazy(() => import('./RootAdmin').then(module => ({ default: module.DatabasePage })));
const SecurityPage = lazy(() => import('./SecurityPage').then(module => ({ default: module.SecurityPage })));
const ChangeApprovalPage = lazy(() => import('./ChangeApprovalPage').then(module => ({ default: module.ChangeApprovalPage })));
const ChangeTemplatesPage = lazy(() => import('./ChangeTemplates').then(module => ({ default: module.ChangeTemplatesPage })));

function RouteLoadingFallback() {
  return <Box sx={{ display: 'grid', minHeight: '45vh', placeItems: 'center' }}><CircularProgress aria-label="Loading workspace" /></Box>;
}

function BrandedApplication() {
  const { brand } = useMspBranding();
  const theme = useMemo(() => createTheme({
    palette: {
      mode: 'dark',
      primary: { main: brand.accent },
      secondary: { main: brand.secondaryAccent },
      background: { default: '#080f1d', paper: '#111b2e' },
    },
    shape: { borderRadius: 10 },
    typography: { fontFamily: 'Inter, Segoe UI, system-ui, sans-serif', h3: { fontWeight: 800, fontSize: '2rem' }, h6: { fontWeight: 750 } },
    components: {
      MuiPaper: { styleOverrides: { root: { backgroundImage: 'none', border: '1px solid #243552' } } },
      MuiButton: { styleOverrides: { root: { textTransform: 'none', fontWeight: 750 } } },
    },
  }), [brand.accent, brand.secondaryAccent]);
  return <WorkspaceProvider><Suspense fallback={<RouteLoadingFallback />}>
      <Admin dataProvider={dataProvider} authProvider={authProvider} dashboard={Dashboard} layout={WorkspaceLayout} loginPage={LoginPage} theme={theme} requireAuth>
        <Resource name="assets" list={AssetList} show={AssetShow} edit={AssetEdit} create={AssetCreate} icon={Inventory2Outlined} recordRepresentation="name" />
        <CustomRoutes>
          <Route path="/business-systems" element={<BusinessSystemsPage />} />
          <Route path="/relationships" element={<Relationships />} />
          <Route path="/changes" element={<ChangeControlPage />} />
          <Route path="/contacts" element={<ContactsPage />} />
          <Route path="/governance/audit" element={<AuditCenterPage />} />
          <Route path="/governance/reports" element={<ReportsPage />} />
          <Route path="/governance/data-quality" element={<DataQualityPage />} />
          <Route path="/customer/users" element={<CustomerUsersPage />} />
          <Route path="/admin/customers" element={<CustomersPage />} />
          <Route path="/admin/customer-groups" element={<CustomerGroupsPage />} />
          <Route path="/admin/users" element={<UsersPage />} />
          <Route path="/admin/rbac" element={<RbacPage />} />
          <Route path="/admin/integrations" element={<IntegrationsPage />} />
          <Route path="/admin/integrations/connectwise" element={<ConnectWiseIntegrationPage />} />
          <Route path="/admin/reconciliation" element={<ReconciliationPage />} />
          <Route path="/admin/email" element={<EmailPage />} />
          <Route path="/admin/notifications" element={<NotificationsPage />} />
          <Route path="/admin/change-templates" element={<ChangeTemplatesPage />} />
          <Route path="/admin/branding" element={<BrandingPage />} />
          <Route path="/admin/database" element={<DatabasePage />} />
          <Route path="/profile/security" element={<SecurityPage />} />
        </CustomRoutes>
      </Admin>
    </Suspense></WorkspaceProvider>;
}

export function App() {
  const [publicApproval, setPublicApproval] = useState(window.location.hash.split('?')[0] === '#/approve');
  useEffect(() => {
    const routeChanged = () => setPublicApproval(window.location.hash.split('?')[0] === '#/approve');
    window.addEventListener('hashchange', routeChanged);
    return () => window.removeEventListener('hashchange', routeChanged);
  }, []);
  return <BrandingProvider><Suspense fallback={<RouteLoadingFallback />}>{publicApproval ? <ChangeApprovalPage /> : <BrandedApplication />}</Suspense></BrandingProvider>;
}
