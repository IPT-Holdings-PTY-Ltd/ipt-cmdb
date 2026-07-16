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
  businessOwner: string;
  signoffDelegate: string;
  department: string;
  userPopulation: string;
  rtoHours: string;
  rpoHours: string;
  supportHours: string;
  dataClassification: string;
  customerFacing: string;
  signoffRequired: string;
  aliases: string;
  businessDescription: string;
  virtualizationPlatform: string;
  clusterName: string;
  haEnabled: string;
  minimumHosts: string;
  capacityStatus: string;
  mobility: string;
  powerState: string;
  guestOs: string;
  cpuCount: string;
  memoryGb: string;
  storageGb: string;
  maintenanceMode: string;
  protectionStatus: string;
  displayLayer: string;
  networkZone: string;
  vlanId: string;
  subnet: string;
  ipAddress: string;
  networkRole: string;
  redundancyGroup: string;
  redundancyRole: string;
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
  impactPolicy: 'required' | 'degraded' | 'redundant' | 'informational';
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
  impactPolicyPath: string[];
  impactSeverity: 'scope' | 'outage' | 'degraded' | 'protected';
  impactNotes: string[];
  criticality: string;
  environment: string;
  site: string;
  lifecycle: string;
  operationalStatus: string;
  serviceOwner: string;
  technicalOwner: string;
  custodian: string;
  businessOwner: string;
  signoffDelegate: string;
  signoffRequired: string;
  department: string;
  userPopulation: string;
  rtoHours: string;
  rpoHours: string;
  virtualizationPlatform: string;
  clusterName: string;
  haEnabled: string;
  capacityStatus: string;
  mobility: string;
  powerState: string;
  protectionStatus: string;
  virtualizationDecision: string;
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
    businessSystemCount: number;
    businessSystems: Array<Pick<ChangeImpactItem, 'assetId' | 'name' | 'criticality' | 'operationalStatus' | 'businessOwner' | 'serviceOwner' | 'signoffDelegate' | 'signoffRequired' | 'department' | 'userPopulation' | 'rtoHours' | 'rpoHours' | 'impactSeverity'>>;
    businessOwners: string[];
    missingBusinessOwnerCount: number;
    virtualizationAssessments: Array<Pick<ChangeImpactItem, 'assetId' | 'name' | 'role' | 'impactSeverity' | 'virtualizationPlatform' | 'clusterName' | 'haEnabled' | 'powerState' | 'protectionStatus' | 'virtualizationDecision'>>;
    protectedVmCount: number;
    degradedVmCount: number;
    outageVmCount: number;
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

export type AuditChange = { field: string; before: unknown; after: unknown };

export type AuditEvent = {
  id: string;
  companyId?: string | null;
  actorUserId?: string | null;
  actorLabel: string;
  actorType: string;
  sourceSystem: string;
  category: string;
  entityType: string;
  entityId?: string | null;
  entityName: string;
  action: string;
  outcome: 'success' | 'denied' | 'failed';
  severity: 'informational' | 'warning' | 'critical';
  requestId: string;
  correlationId: string;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  changes: AuditChange[];
  reason?: string;
  metadata: Record<string, unknown>;
  createdAt: string;
};

export type ReportDefinition = { id: string; title: string; description: string };
export type ReportColumn = { key: string; label: string };
export type ReportPreview = {
  id: string;
  title: string;
  description: string;
  generatedAt: string;
  companyId?: string | null;
  columns: ReportColumn[];
  rows: Array<Record<string, unknown>>;
  summary: { rowCount: number; customerCount: number };
  previewLimited: boolean;
};
