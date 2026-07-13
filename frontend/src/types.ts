export type User = {
  id: string;
  email: string;
  role: 'platform_admin' | 'msp_operator' | 'client_reader';
  companyIds: string[];
  accountType?: 'root' | 'customer';
};

export type Company = {
  id: string;
  name: string;
  externalIds?: Record<string, string>;
};

export type AssetMetadata = {
  lifecycle: string;
  operationalStatus: string;
  criticality: string;
  environment: string;
  site: string;
  serviceOwner: string;
  technicalOwner: string;
  custodian: string;
  vendor: string;
  model: string;
  purchaseDate: string;
  warrantyEnd: string;
  renewalDate: string;
  endOfLifeDate: string;
  reviewDate: string;
};

export type Asset = {
  id: string;
  companyId: string;
  name: string;
  type: string;
  status: string;
  source: string;
  externalId?: string | null;
  lastSeen?: string;
  fields: Record<string, unknown>;
  metadata: AssetMetadata;
};

export type Relationship = {
  id: string;
  fromId: string;
  toId: string;
  type: string;
};

export type AccessGroup = {
  id: string;
  name: string;
  companyIds: string[];
  system: boolean;
};

export type RoleTemplate = {
  id: User['role'];
  name: string;
  scope: string;
  system: boolean;
  permissions: string[];
};

export type Integration = {
  id: string;
  name: string;
  type: 'connectwise' | 'ncentral' | 'passportal';
  enabled: boolean;
  mode: string;
  lastSync: string | null;
  status: string;
  scope: 'msp' | 'customer';
};

export type SyncRun = {
  id: string;
  type: string;
  status: string;
  message: string;
  startedAt?: string;
  finishedAt?: string;
  discovered?: number;
};

export type ChangeImpactItem = {
  assetId: string;
  name: string;
  type: string;
  role: 'Scope' | 'Direct impact' | 'Downstream impact';
  depth: number;
  pathAssetIds: string[];
  relationshipPath: string[];
  criticality: string;
  environment: string;
  site: string;
  lifecycle: string;
  operationalStatus: string;
  serviceOwner: string;
  technicalOwner: string;
  custodian: string;
  owner: string;
  source: string;
  externalId?: string | null;
};

export type ChangeImpactPreview = {
  items: ChangeImpactItem[];
  summary: {
    scopeCount: number;
    directCount: number;
    downstreamCount: number;
    criticalCount: number;
    missingOwnerCount: number;
    owners: string[];
    suggestedRisk: { score: number; level: string; factors: string[] };
  };
};

export type ChangePackage = {
  id: string;
  number: string;
  companyId: string;
  companyName: string;
  title: string;
  status: string;
  changeType: string;
  category: string;
  priority: string;
  riskLevel: string;
  outageExpected: boolean;
  plannedStart: string;
  plannedEnd: string;
  assignedTechnician: string;
  approver: string;
  createdAt: string;
  impactSnapshot: ChangeImpactItem[];
  impactSummary: ChangeImpactPreview['summary'];
  integrationState: { connectwise: { status: string; ticketId: string | null; ticketUrl: string | null } };
};
