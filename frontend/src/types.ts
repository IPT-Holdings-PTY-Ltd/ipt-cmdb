export type User = {
  id: string;
  email: string;
  displayName: string;
  status: 'active' | 'invited' | 'disabled' | 'archived';
  role: 'platform_admin' | 'msp_operator' | 'client_reader';
  companyIds: string[];
  directCompanyIds?: string[];
  groupIds?: string[];
  accountType?: 'root' | 'customer';
  authSource?: 'local' | 'entra' | 'none';
  apiAccessEnabled?: boolean;
  mfaRequired?: boolean;
  mfaEnabled?: boolean;
  mfaRecoveryCodesRemaining?: number;
  apiTokenCount?: number;
  lastLoginAt?: string | null;
  lastApiUsedAt?: string | null;
  archivedAt?: string | null;
};

export type ApiToken = {
  id: string;
  userId: string;
  name: string;
  tokenPrefix: string;
  scopes: string[];
  companyIds: string[];
  expiresAt: string;
  lastUsedAt?: string | null;
  revokedAt?: string | null;
  createdAt: string;
  token?: string;
};

export type Contact = {
  id: string;
  companyId: string;
  linkedUserId?: string | null;
  displayName: string;
  firstName: string;
  lastName: string;
  email: string;
  phone: string;
  mobile: string;
  jobTitle: string;
  department: string;
  location: string;
  timezone: string;
  managerContactId?: string | null;
  status: 'active' | 'on_leave' | 'left_company' | 'inactive';
  source: string;
  syncStatus: 'not_synced' | 'current' | 'stale' | 'conflict' | 'error';
  lastSeen?: string | null;
  lastSynced?: string | null;
  attributes: Record<string, unknown>;
  responsibilityCount: number;
  portalUser?: { id: string; email: string; role: User['role']; status: 'active' | 'invited' | 'disabled' } | null;
  createdAt: string;
  updatedAt: string;
};

export type ContactResponsibility = {
  id: string;
  companyId: string;
  assetId: string;
  assetName: string;
  assetType: string;
  contactId: string;
  contactName: string;
  contactEmail: string;
  role: 'business_owner' | 'service_owner' | 'technical_owner' | 'custodian' | 'change_approver' | 'signoff_delegate' | 'support_contact';
  isPrimary: boolean;
  effectiveFrom: string;
  effectiveUntil?: string | null;
  escalationOrder: number;
  notes: string;
  source: string;
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
  responsibilities?: ContactResponsibility[];
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
  description: string;
  companyIds: string[];
  system: boolean;
  membershipMode: 'manual' | 'dynamic';
  membershipRules: Record<string, unknown>;
  ownerUserId?: string | null;
  ownerLabel: string;
  assignedUserCount: number;
  revision: number;
  updatedAt?: string | null;
};

export type AccessGroupImpact = {
  groupId: string;
  groupName: string;
  assignedUserCount: number;
  activeAssignedUserCount: number;
  usersLosingAccess: number;
  lostCustomerAssignments: number;
  users: Array<{
    id: string;
    displayName: string;
    email: string;
    status: User['status'];
    lostCompanyIds: string[];
    lostCustomers: string[];
  }>;
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
  responsibilities: ContactResponsibility[];
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
  riskSource: string;
  riskAssessment: { score: number; level: string; factors: string[] };
  outageExpected: boolean;
  plannedStart: string;
  plannedEnd: string;
  reason: string;
  businessImpact: string;
  implementationPlan: string;
  validationPlan: string;
  rollbackPlan: string;
  communicationStatus: string;
  communicationPlan: string;
  assignedTechnician: string;
  approver: string;
  notes: string;
  scopeAssetIds: string[];
  revision: number;
  updatedAt: string;
  actualStart: string;
  actualEnd: string;
  actualOutageMinutes: number;
  outcome: 'pending' | 'successful' | 'failed' | 'backed_out' | 'cancelled';
  failureReason: string;
  validationResult: string;
  rollbackExecuted: boolean;
  rollbackResult: string;
  closureNotes: string;
  approvals: Array<{ id: string; decision: 'approved' | 'declined'; comments: string; actorId?: string | null; actorEmail: string; approverName?: string; responsibilityRole?: string; scope?: Array<{ type: string; id?: string; name?: string }>; createdAt: string }>;
  statusHistory: Array<{ id: string; fromStatus?: string | null; toStatus: string; reason: string; actorId?: string | null; actorEmail: string; createdAt: string }>;
  createdBy: { id?: string | null; email: string };
  createdAt: string;
  impactSnapshot: ChangeImpactItem[];
  impactSummary: ChangeImpactPreview['summary'];
  integrationState: { connectwise: { status: string; ticketId: string | null; ticketUrl: string | null } };
};

export type ChangeApprovalRequest = {
  id: string;
  changeId: string;
  batchId: string;
  changeRevision: number;
  approverName: string;
  approverEmail: string;
  responsibilityRole: string;
  scope: Array<{ type: string; id?: string; name?: string }>;
  status: 'pending' | 'approved' | 'declined' | 'expired' | 'revoked';
  expiresAt: string;
  deliveryStatus: 'pending' | 'accepted' | 'failed';
  providerRequestId: string;
  lastError: string;
  decisionComments: string;
  decidedAt: string;
  createdAt: string;
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

export type DataQualityFinding = {
  id: string; ruleKey: string; ruleLabel: string; category: string; severity: 'high' | 'medium' | 'low';
  companyId: string; assetId: string; assetName: string; assetType: string; source: string;
  evidence: string; recommendation: string;
};
export type DataQualityCustomer = { companyId: string; companyName: string; assetCount: number; findingCount: number; highCount: number; score: number };
export type ReconciliationCandidate = {
  id: string; companyId: string; provider: string; externalObjectType: string; externalId: string; externalName: string;
  candidateAssetId?: string | null; candidateAssetName: string; reason: string; confidence: number; state: string;
  conflictDetails: Record<string, unknown>;
};
export type FieldAuthorityRule = { companyId: string; ciType: string; fieldName: string; provider: string; priority: number };
export type DataQualitySnapshot = {
  summary: { score: number; assetCount: number; findingCount: number; highCount: number; exceptionCount: number; staleDays: number; pendingReconciliationCount: number; byCategory: Record<string, number> };
  customers: DataQualityCustomer[]; findings: DataQualityFinding[]; candidates: ReconciliationCandidate[];
  fieldAuthority: FieldAuthorityRule[];
};
