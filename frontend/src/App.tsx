import Inventory2Outlined from '@mui/icons-material/Inventory2Outlined';
import { createTheme } from '@mui/material/styles';
import { Admin, CustomRoutes, Resource } from 'react-admin';
import { Route } from 'react-router-dom';
import { AssetCreate, AssetEdit, AssetList, AssetShow } from './assets';
import { authProvider } from './authProvider';
import { Dashboard } from './Dashboard';
import { CustomerUsersPage } from './CustomerUsers';
import { ChangeControlPage } from './ChangeControl';
import { dataProvider } from './dataProvider';
import { WorkspaceLayout } from './Layout';
import { LoginPage } from './LoginPage';
import { Relationships } from './Relationships';
import { BrandingPage, CustomerGroupsPage, CustomersPage, DatabasePage, IntegrationsPage, RbacPage, UsersPage } from './RootAdmin';
import { WorkspaceProvider } from './workspace';
import { BrandingProvider, useMspBranding } from './branding';
import { useMemo } from 'react';

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
  return <WorkspaceProvider>
    <Admin dataProvider={dataProvider} authProvider={authProvider} dashboard={Dashboard} layout={WorkspaceLayout} loginPage={LoginPage} theme={theme} requireAuth>
      <Resource name="assets" list={AssetList} show={AssetShow} edit={AssetEdit} create={AssetCreate} icon={Inventory2Outlined} recordRepresentation="name" />
      <CustomRoutes>
        <Route path="/relationships" element={<Relationships />} />
        <Route path="/changes" element={<ChangeControlPage />} />
        <Route path="/customer/users" element={<CustomerUsersPage />} />
        <Route path="/admin/customers" element={<CustomersPage />} />
        <Route path="/admin/customer-groups" element={<CustomerGroupsPage />} />
        <Route path="/admin/users" element={<UsersPage />} />
        <Route path="/admin/rbac" element={<RbacPage />} />
        <Route path="/admin/integrations" element={<IntegrationsPage />} />
        <Route path="/admin/branding" element={<BrandingPage />} />
        <Route path="/admin/database" element={<DatabasePage />} />
      </CustomRoutes>
    </Admin>
  </WorkspaceProvider>;
}

export function App() {
  return <BrandingProvider><BrandedApplication /></BrandingProvider>;
}
