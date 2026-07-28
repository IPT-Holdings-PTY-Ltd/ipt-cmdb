import { CssBaseline } from '@mui/material';
import { createTheme, ThemeProvider } from '@mui/material/styles';
import { lazy, Suspense, useEffect, useMemo, useState, type ReactNode } from 'react';
import { HashRouter, Navigate, Route, Routes, useNavigate } from 'react-router';
import { checkAuth } from './authProvider';
import { BrandingProvider, useMspBranding } from './branding';
import { WorkspaceLayout } from './Layout';
import { LoginPage } from './LoginPage';
import { RouteErrorBoundary, RouteLoadingFallback } from './RouteFeedback';
import { getSession } from './session';
import { NotificationProvider } from './ui';
import { WorkspaceProvider } from './workspace';

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

function PublicLazyRoute({ children }: { children: ReactNode }) {
  return (
    <RouteErrorBoundary>
      <Suspense fallback={<RouteLoadingFallback />}>{children}</Suspense>
    </RouteErrorBoundary>
  );
}

/**
 * Verifies the server-side session before protected routes render and responds
 * immediately when an API request clears an expired browser session.
 */
function AuthenticationBoundary({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let active = true;
    checkAuth()
      .then(() => { if (active) setReady(true); })
      .catch(() => { if (active) navigate('/login', { replace: true }); });
    const sessionChanged = () => {
      if (!getSession()) navigate('/login', { replace: true });
    };
    window.addEventListener('cmdb-auth-change', sessionChanged);
    return () => {
      active = false;
      window.removeEventListener('cmdb-auth-change', sessionChanged);
    };
  }, [navigate]);

  return ready ? children : <RouteLoadingFallback />;
}

function ApplicationRoutes() {
  return (
    <HashRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/approve" element={<PublicLazyRoute><ChangeApprovalPage /></PublicLazyRoute>} />
        <Route
          element={(
            <AuthenticationBoundary>
              <WorkspaceProvider>
                <WorkspaceLayout />
              </WorkspaceProvider>
            </AuthenticationBoundary>
          )}
        >
          <Route index element={<Dashboard />} />
          <Route path="/assets" element={<AssetList />} />
          <Route path="/assets/create" element={<AssetCreate />} />
          <Route path="/assets/:assetId" element={<AssetEdit />} />
          <Route path="/assets/:assetId/show" element={<AssetShow />} />
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
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </HashRouter>
  );
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
    typography: {
      fontFamily: 'Inter, Segoe UI, system-ui, sans-serif',
      h3: { fontWeight: 800, fontSize: '2rem' },
      h6: { fontWeight: 750 },
    },
    components: {
      MuiPaper: { styleOverrides: { root: { backgroundImage: 'none', border: '1px solid #243552' } } },
      MuiButton: { styleOverrides: { root: { textTransform: 'none', fontWeight: 750 } } },
    },
  }), [brand.accent, brand.secondaryAccent]);

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <NotificationProvider>
        <ApplicationRoutes />
      </NotificationProvider>
    </ThemeProvider>
  );
}

/** Root application with public branding and a React Router 8 route graph. */
export function App() {
  return <BrandingProvider><BrandedApplication /></BrandingProvider>;
}
