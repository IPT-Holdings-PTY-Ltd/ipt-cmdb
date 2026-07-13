import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { apiFetch, getSession } from './session';
import type { Company } from './types';

type WorkspaceState = {
  companies: Company[];
  companyId: string;
  setCompanyId: (value: string) => void;
  companyName: string;
  isRoot: boolean;
  refreshCompanies: () => Promise<Company[]>;
};

const WorkspaceContext = createContext<WorkspaceState | null>(null);
let selectedCompany = localStorage.getItem('cmdb.workspace') || '__root__';

export function currentCompanyId() {
  return selectedCompany;
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [companyId, updateCompanyId] = useState(selectedCompany);
  const [authVersion, setAuthVersion] = useState(0);
  const authenticated = Boolean(getSession());

  const refreshCompanies = async () => {
    const items = await apiFetch<Company[]>('/api/companies');
    setCompanies(items);
    const user = getSession()?.user;
    const rootAllowed = ['platform_admin', 'msp_operator'].includes(user?.role || '');
    if (selectedCompany === '__root__' && !rootAllowed) {
      selectedCompany = items[0]?.id || '';
      updateCompanyId(selectedCompany);
    }
    return items;
  };

  useEffect(() => {
    const changed = () => setAuthVersion(value => value + 1);
    window.addEventListener('cmdb-auth-change', changed);
    return () => window.removeEventListener('cmdb-auth-change', changed);
  }, []);

  useEffect(() => {
    if (!authenticated) return;
    refreshCompanies().catch(() => setCompanies([]));
  }, [authenticated, authVersion]);

  const setCompanyId = (value: string) => {
    selectedCompany = value;
    localStorage.setItem('cmdb.workspace', value);
    updateCompanyId(value);
    window.dispatchEvent(new CustomEvent('cmdb-workspace-change'));
  };

  const value = useMemo<WorkspaceState>(() => ({
    companies,
    companyId,
    setCompanyId,
    isRoot: companyId === '__root__',
    companyName: companyId === '__root__' ? 'MSP workspace' : companies.find(item => item.id === companyId)?.name || 'Customer workspace',
    refreshCompanies,
  }), [companies, companyId]);

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace() {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error('WorkspaceProvider is missing');
  return value;
}
