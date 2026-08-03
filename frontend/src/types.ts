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

export type AssetInventoryRecord = Record<string, unknown>;

export type AssetHardwareInventory = {
  manufacturer?: string;
  model?: string;
  serialNumber?: string;
  systemUuid?: string;
  bios?: AssetInventoryRecord;
  processors?: AssetInventoryRecord[];
  memoryModules?: AssetInventoryRecord[];
  physicalDisks?: AssetInventoryRecord[];
  logicalVolumes?: AssetInventoryRecord[];
};

export type AssetNetworkInterface = AssetInventoryRecord & {
  name?: string;
  description?: string;
  macAddress?: string;
  ipAddresses?: string[];
  gateway?: string;
  gateways?: string[];
  dnsServers?: string[];
  dhcpEnabled?: boolean;
};

export type AssetNetworkInventory = {
  hostname?: string;
  interfaces?: AssetNetworkInterface[];
};

export type AssetOperatingSystemInventory = {
  name?: string;
  version?: string;
  architecture?: string;
  installDate?: string;
  lastBoot?: string;
  capabilities?: AssetInventoryRecord[];
};

export type AssetSoftwareItem = AssetInventoryRecord & {
  name?: string;
  displayName?: string;
  publisher?: string;
  version?: string;
  installDate?: string;
};

export type AssetSoftwareInventory = {
  applications?: AssetSoftwareItem[];
  roles?: AssetSoftwareItem[];
  features?: AssetSoftwareItem[];
  patches?: AssetSoftwareItem[];
  summary?: AssetInventoryRecord;
};

export type AssetClassificationEvidence = {
  proposedType?: string;
  confidence?: number;
  evidence?: string[];
};

export type AssetVirtualizationInventory = {
  role?: string;
  kind?: string;
  platform?: string;
  hostName?: string;
  clusterName?: string;
  powerState?: string;
  classification?: AssetClassificationEvidence;
  confidence?: number | string;
  evidence?: string[];
};

export type AssetMonitoringInventory = {
  lastAgentCheckIn?: string;
  serviceSummary?: AssetInventoryRecord;
  activeIssueSummary?: AssetInventoryRecord;
  inventoryCollectedAt?: string;
  maintenanceWindows?: AssetInventoryRecord[];
  observations?: AssetInventoryRecord[];
};

export type AssetLifecycleInventory = {
  warrantyExpiryDate?: string;
  leaseExpiryDate?: string;
  expectedReplacementDate?: string;
  purchaseDate?: string;
  cost?: string | number;
  location?: string;
  assetTag?: string;
};

export type AssetSourceEvidence = {
  provider?: string;
  coverage?: Record<string, unknown>;
  observedAt?: string;
  fingerprint?: string;
  partialErrors?: string[];
  snapshots?: AssetInventoryRecord[];
};

export type AssetFields = Record<string, unknown> & {
  hardware?: AssetHardwareInventory;
  network?: AssetNetworkInventory;
  operatingSystem?: string | AssetOperatingSystemInventory;
  software?: AssetSoftwareInventory;
  virtualization?: AssetVirtualizationInventory;
  monitoring?: AssetMonitoringInventory;
  lifecycle?: AssetLifecycleInventory;
  sourceEvidence?: AssetSourceEvidence;
};

export type AssetInventoryCollections = Record<string, AssetInventoryRecord[]> & {
  network_interfaces?: AssetNetworkInterface[];
  applications?: AssetSoftwareItem[];
  memory_modules?: AssetInventoryRecord[];
  physical_disks?: AssetInventoryRecord[];
  logical_volumes?: AssetInventoryRecord[];
  processors?: AssetInventoryRecord[];
  server_roles?: AssetSoftwareItem[];
  server_features?: AssetSoftwareItem[];
  os_capabilities?: AssetInventoryRecord[];
  maintenance_windows?: AssetInventoryRecord[];
};

export type AssetInventoryPayload = {
  assetId: string;
  companyId?: string;
  observedAt?: string;
  collections?: AssetInventoryCollections;
  networkInterfaces?: AssetNetworkInterface[];
  snapshots?: AssetInventoryRecord[];
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
  fields: AssetFields;
  inventoryCollections?: AssetInventoryCollections;
  metadata: AssetMetadata;
  responsibilities?: ContactResponsibility[];
};

export type Relationship = {
  id: string;
  fromId: string;
  toId: string;
  type: string;
  impactPolicy: 'required' | 'degraded' | 'redundant' | 'informational';
  provenance?: 'manual' | 'provider';
  sourceMappingId?: string | null;
  confidence?: number;
  evidence?: {
    provider?: string;
    candidateId?: string;
    messages?: string[];
    impactPolicy?: string;
    freshness?: string;
    observedAt?: string;
    [key: string]: unknown;
  };
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
  connectionStatus?: string;
  lastTestAt?: string | null;
  lastError?: string;
  revision?: number;
  hasCredentials?: boolean;
  lifecycleStatus: 'active' | 'paused' | 'disabled' | 'removed';
  lifecycleReason?: string;
  lifecycleChangedAt?: string | null;
  lifecycleChangedBy?: string | null;
};

export type ConnectWiseConnection = {
  id: string;
  enabled: boolean;
  baseUrl: string;
  companyId: string;
  clientId: string;
  pageSize: number;
  configured: boolean;
  hasCredentials: boolean;
  credentialSource: 'environment' | 'encrypted_database' | 'not_configured';
  managedByEnvironment: boolean;
  connectionStatus: 'not_configured' | 'configured' | 'verified' | 'error';
  lastTestAt?: string | null;
  lastError?: string;
  revision: number;
  lifecycleStatus: 'active' | 'paused' | 'disabled' | 'removed';
  lifecycleReason?: string;
  lifecycleChangedAt?: string | null;
  lifecycleChangedBy?: string | null;
  discoveryPolicy: ConnectWiseDiscoveryPolicy;
};

export type NcentralConnection = {
  id: string;
  enabled: boolean;
  baseUrl: string;
  pageSize: number;
  configured: boolean;
  hasCredentials: boolean;
  credentialSource: 'environment' | 'encrypted_database' | 'not_configured';
  managedByEnvironment: boolean;
  connectionStatus: 'not_configured' | 'configured' | 'verified' | 'error';
  lastTestAt?: string | null;
  lastError?: string;
  revision: number;
  lifecycleStatus: 'active' | 'paused' | 'disabled' | 'removed';
  lifecycleReason?: string;
  lifecycleChangedAt?: string | null;
  lifecycleChangedBy?: string | null;
  discoveryPolicy: { excludedExternalIds: string[] };
};

export type NcentralGraphqlCapabilityStatus =
  | 'available'
  | 'unavailable'
  | 'forbidden'
  | 'schema_gated'
  | 'not_tested'
  | 'error';

export type NcentralGraphqlCapability = {
  key: string;
  label?: string;
  status: NcentralGraphqlCapabilityStatus;
  summary?: string;
  checkedAt?: string | null;
  expiresAt?: string | null;
  stale?: boolean;
};

export type NcentralGraphqlCacheState = {
  status: 'empty' | 'fresh' | 'stale' | 'refreshing' | 'partial' | 'scope_changed'
    | 'configuration_changed' | 'error';
  providerAssetCount?: number;
  eligibleDeviceCount?: number;
  deviceCount?: number;
  unmatchedDeviceCount?: number;
  pagesRead?: number;
  complete?: boolean;
  lastRefreshedAt?: string | null;
  expiresAt?: string | null;
  message?: string;
};

/**
 * Public GraphQL configuration. The API must never populate graphqlApiToken;
 * that field exists only in the write request sent from the setup form.
 */
export type NcentralGraphqlConfig = {
  graphqlEnabled: boolean;
  graphqlEndpoint: string;
  graphqlPageSize: number;
  graphqlServerId: string;
  configured: boolean;
  credentialSource: 'environment' | 'encrypted_database' | 'not_configured';
  managedByEnvironment: boolean;
  hasCredentials?: boolean;
  connectionStatus?: 'disabled' | 'not_configured' | 'configured' | 'verified' | 'error';
  lastTestAt?: string | null;
  lastError?: string;
  revision?: number;
  capabilities?: NcentralGraphqlCapability[];
  capability?: {
    reachable?: boolean;
    candidateCount?: number;
    truncated?: boolean;
    testedAt?: string | null;
    status?: 'supported' | 'unsupported' | 'unavailable' | 'error';
    errorCategory?: string;
    checkedAt?: string | null;
    expiresAt?: string | null;
    stale?: boolean;
  };
  queries?: Array<{
    key: string;
    label: string;
    description: string;
    paginated: boolean;
    customerScoped: boolean;
    readOnly: boolean;
  }>;
  cache?: NcentralGraphqlCacheState;
};

export type NcentralGraphqlTestResult = {
  reachable: boolean;
  credentialSource: string;
  customerCandidates: Array<{ id: string; name: string; typeName: string }>;
  candidateCount: number;
  truncated: boolean;
  readOnly: boolean;
  writesAttempted: boolean;
};

export type NcentralGraphqlFieldGroup = {
  key: string;
  label?: string;
  devicesWithData?: number;
  fieldCount?: number;
  status?: NcentralGraphqlCapabilityStatus;
};

export type NcentralGraphqlPreview = {
  companyId: string;
  providerCompanyId: string;
  queryKey: 'asset_identity' | 'asset_inventory';
  organizationIds: string[];
  totalCount: number;
  items: Array<{
    graphqlAssetId: string;
    name: string;
    customer: { id: string; name: string };
    site: { id: string; name: string } | null;
    serviceOrganization: { id: string; name: string } | null;
    sourceIdentity: {
      provider: 'ncentral';
      namespace: 'nable_graphql_asset';
      externalId: string;
    };
    restIdentity: {
      provider: 'ncentral';
      namespace: 'ncentral_rest_device';
      serverId: string;
      deviceId: string;
      crosswalkKey: string;
    } | null;
    summary: {
      description?: string;
      operatingSystem?: {
        name?: string;
        version?: string;
        type?: string;
        architecture?: string;
        buildNumber?: string;
        installedOn?: string;
      };
      system?: {
        hostname?: string;
        manufacturer?: string;
        model?: string;
        serialNumber?: string;
        memoryTotalSizeBytes?: number;
      };
      cpu?: Array<{ name: string; cores: number }>;
      agent?: { status?: string; statusChangedAt?: string };
      lastBootedAt?: string;
      externalIpAddress?: string;
    };
  }>;
  truncated: boolean;
  refreshed?: boolean;
  credentialSource: string;
  customerOnly?: boolean;
  readOnly?: boolean;
  writesAttempted?: boolean;
  message?: string;
  cache?: NcentralGraphqlCacheState;
};

export type NcentralDiscoveryPreview = {
  readOnly: boolean;
  writesAttempted: boolean;
  appliedPolicy: { excludedExternalIds: string[] };
  discovered: number;
  included: number;
  excluded: number;
  truncated: boolean;
  sampleIncluded: ProviderCompany[];
  sampleExcluded: Array<{ externalId: string; name: string; reason: string }>;
  credentialSource: string;
  message: string;
};

export type NcentralDeviceFilter = {
  id: string;
  name: string;
  description: string;
};

export type NcentralDeviceOptions = {
  companyId: string;
  providerCompanyId: string;
  providerCompanyName: string;
  credentialSource: string;
  readOnly: boolean;
  writesAttempted: boolean;
  discovered: number;
  deviceFilters: NcentralDeviceFilter[];
  availableTypes: ProviderFilterOption[];
  availableStatuses: ProviderFilterOption[];
  policy: ConnectWiseCiPolicy;
};

export type IntegrationLifecycleImpact = {
  provider: Integration['type'];
  lifecycleStatus: Integration['lifecycleStatus'];
  customerMappings: number;
  companyObservations: number;
  ciPolicies: number;
  enabledPolicies: number;
  pendingReviews: number;
  ciMappings: number;
  importedCis: number;
  syncRuns: number;
  managedByEnvironment: boolean;
  credentialSource: string;
};

export type ConnectWiseDiscoveryPolicy = {
  includedStatuses: string[];
  includedTypes: string[];
  includedSites: string[];
  includeDeleted: boolean;
  excludedExternalIds: string[];
};

export type IntegrationProviderManifest = {
  key: string;
  name: string;
  vendor: string;
  version: string;
  description: string;
  scopes: Array<'msp' | 'customer'>;
  authentication_modes: string[];
  prerequisites: string[];
  operations: Array<{
    key: string;
    label: string;
    direction: 'input' | 'output' | 'bidirectional' | 'trigger' | 'action';
    entity_type: string;
    status: 'available' | 'planned' | 'disabled';
    description: string;
    requires_approval: boolean;
    writes_provider: boolean;
  }>;
  filters: Array<{ key: string; label: string; kind: string; description: string }>;
  documentation_path: string;
};

export type IntegrationTestResult = {
  reachable: boolean;
  message: string;
  credentialSource: string;
  writesAttempted: boolean;
  stages: Array<{ key: string; label: string; status: 'passed' | 'failed' | 'skipped'; message: string }>;
};

export type DiscoveryPreview = {
  readOnly: boolean;
  writesAttempted: boolean;
  appliedPolicy: ConnectWiseDiscoveryPolicy;
  discovered: number;
  included: number;
  excluded: number;
  truncated: boolean;
  exclusionReasons: Record<string, number>;
  availableStatuses: string[];
  availableTypes: string[];
  availableSites: string[];
  sampleIncluded: ProviderCompany[];
  sampleExcluded: Array<{ externalId: string; name: string; reason: string }>;
  message: string;
};

export type ConnectWiseDiscoveryOptions = {
  readOnly: boolean;
  writesAttempted: boolean;
  sampled: number;
  truncated: boolean;
  availableStatuses: string[];
  availableTypes: string[];
  availableSites: string[];
  credentialSource: string;
};

export type ConfigurationReconciliationItem = {
  externalId: string;
  name: string;
  type: string;
  status: string;
  action: 'create' | 'update' | 'link' | 'unchanged' | 'conflict';
  reason: string;
  confidence: number;
  assetId?: string | null;
  assetName: string;
  changedFields: string[];
  record: {
    externalId: string;
    name: string;
    type: string;
    status: string;
    providerTypeId: string;
    providerTypeName: string;
    providerStatusId: string;
    providerStatusName: string;
    fields: Record<string, unknown>;
    metadata: Record<string, unknown>;
  };
};

export type ConfigurationReconciliationPreview = {
  companyId: string;
  companyName: string;
  providerCompanyId: string;
  providerCompanyName: string;
  credentialSource: string;
  readOnly: boolean;
  writesAttempted: boolean;
  discovered: number;
  included: number;
  excluded: number;
  exclusionReasons: Record<string, number>;
  availableTypes: ProviderFilterOption[];
  availableStatuses: ProviderFilterOption[];
  typeMappingSummary: {
    mapped: number;
    unmapped: number;
    blocked: number;
    unmappedTypes: ProviderFilterOption[];
  };
  appliedPolicy: ConnectWiseCiPolicy;
  counts: Record<'create' | 'update' | 'link' | 'unchanged' | 'conflict', number>;
  items: ConfigurationReconciliationItem[];
  message: string;
  syncRunId?: string;
  queueSummary?: { pending: number; created: number; updated: number; resolved: number };
};

export type NcentralPreviewRun = {
  id: string;
  companyId: string;
  providerCompanyId: string;
  policyId?: string | null;
  policyRevision?: number;
  status: 'queued' | 'running' | 'success' | 'failed' | 'cancelled';
  phase: string;
  progress: {
    current: number;
    total: number;
    percent: number;
    discovered: number;
    enriched: number;
    reviewed: number;
  };
  message: string;
  error?: string;
  startedAt?: string | null;
  updatedAt?: string | null;
  finishedAt?: string | null;
  canCancel: boolean;
  canRetry: boolean;
  cancelRequested: boolean;
  result?: ConfigurationReconciliationPreview | null;
};

export type ProviderFilterOption = { id: string; name: string; count: number };

export type ConnectWiseCiPolicy = {
  id: string;
  provider: string;
  companyId: string;
  providerParentId: string;
  typeMode: 'all' | 'selected';
  includedTypeIds: string[];
  typeMappings: Record<string, string>;
  blockUnmappedTypes: boolean;
  enrichmentMode?: 'fast' | 'balanced' | 'full';
  relationshipAutomationMode?: 'review' | 'auto_explicit';
  relationshipAutoApproveTypes?: string[];
  relationshipMinConfidence?: number;
  relationshipMinObservations?: number;
  relationshipMaxEvidenceAgeHours?: number;
  missingDeviceRequiredSnapshots?: number;
  missingDeviceMinimumHours?: number;
  graphqlOrganizationIds?: string[];
  statusMode: 'all' | 'selected';
  includedStatusIds: string[];
  excludedExternalIds: string[];
  providerFilterId?: string;
  syncMode: 'manual' | 'continuous_preview';
  intervalMinutes: number;
  enabled: boolean;
  revision: number;
  updatedAt?: string | null;
  nextRunAt?: string | null;
  lastRunAt?: string | null;
  lastSuccessAt?: string | null;
  lastError?: string;
  consecutiveFailures?: number;
  backoffActive?: boolean;
  retryDelayMinutes?: number;
  companyName?: string;
  leaseOwner?: string | null;
  leaseUntil?: string | null;
};

export type IntegrationPreviewStatus = {
  workerConfigured: boolean;
  workerEnabled: boolean;
  workerHealthy: boolean;
  workerIntervalSeconds: number;
  scheduledPoliciesEnabled: boolean;
  executionMode: 'embedded' | 'dedicated' | 'one_shot';
  heartbeatAgeSeconds?: number | null;
  runtime?: WorkerRuntimeStatus | null;
  providerRateLimit?: ProviderRateLimitStatus | null;
  providerRateLimits?: Record<string, ProviderRateLimitStatus | null>;
  notificationWorkerEnabled: boolean;
  alertDeliveryConfigured: boolean;
  alertRecipientCount: number;
  enabledPolicies: number;
  pendingReviews: number;
  policies: ConnectWiseCiPolicy[];
};

export type WorkerRuntimeStatus = {
  workerName: string;
  workerId: string;
  deploymentMode: 'embedded' | 'dedicated' | 'one_shot';
  status: 'starting' | 'running' | 'degraded' | 'stopped';
  intervalSeconds: number;
  lastStartedAt?: string | null;
  lastHeartbeatAt: string;
  lastCycleStartedAt?: string | null;
  lastCycleFinishedAt?: string | null;
  lastSuccessAt?: string | null;
  lastErrorAt?: string | null;
  lastError?: string;
  cyclesCompleted: number;
  itemsProcessed: number;
  metadata?: Record<string, unknown>;
};

export type ProviderRateLimitStatus = {
  provider: string;
  observedAt: string;
  httpStatus?: number | null;
  limit?: number | null;
  remaining?: number | null;
  resetAt?: string | null;
  retryAfterSeconds?: number | null;
  limited: boolean;
  requestPath?: string;
};

export type CiReviewItem = {
  id: string;
  provider: string;
  policyId: string;
  companyId: string;
  companyName?: string;
  providerParentId: string;
  externalId: string;
  externalName: string;
  action: 'create' | 'update' | 'link' | 'conflict';
  assetId?: string | null;
  assetName?: string;
  reason: string;
  providerTypeName: string;
  providerStatusName: string;
  changedFields: string[];
  blockedFields?: string[];
  fieldDecisions?: Array<{
    field: string;
    provider: string;
    currentProvider: string;
    incomingPriority?: number;
    currentPriority?: number | null;
    allowed: boolean;
    reason: string;
  }>;
  providerRecord?: Record<string, unknown>;
  evidence?: Record<string, unknown>;
  state: 'pending' | 'dismissed' | 'resolved';
  firstSeenAt: string;
  lastSeenAt: string;
  reviewedAt?: string | null;
  reviewNotes?: string;
};

export type CiReviewQueue = {
  items: CiReviewItem[];
  total: number;
};

export type IntegrationReconciliationQueue = CiReviewQueue & {
  summary: Record<'create' | 'update' | 'link' | 'conflict', number>;
};

export type IntegrationObjectSuppression = {
  id: string;
  provider: string;
  policyId: string;
  companyId: string;
  companyName?: string;
  providerParentId: string;
  externalObjectType: string;
  externalId: string;
  externalName: string;
  providerRecord?: Record<string, unknown>;
  reason: string;
  active: boolean;
  ignoredBy?: string | null;
  ignoredByName?: string;
  ignoredAt: string;
  restoredBy?: string | null;
  restoredByName?: string;
  restoredAt?: string | null;
  restoreReason?: string;
  createdAt: string;
  updatedAt: string;
};

export type IntegrationObjectSuppressionQueue = {
  items: IntegrationObjectSuppression[];
  total: number;
};

export type MissingDeviceLifecycleState =
  | 'observed'
  | 'monitoring'
  | 'eligible'
  | 'not_evaluated'
  | 'retired'
  | 'restore_ready';

export type MissingDeviceLifecycleCandidate = {
  id: string;
  mappingId: string;
  provider: string;
  companyId: string;
  companyName: string;
  providerParentId: string;
  externalId: string;
  externalName: string;
  assetId: string;
  assetName: string;
  state: MissingDeviceLifecycleState;
  consecutiveCompleteAbsences: number;
  requiredAbsences: number;
  firstAbsentAt?: string | null;
  lastObservedAt?: string | null;
  lastEvaluatedAt?: string | null;
  reappearedAt?: string | null;
  retiredAt?: string | null;
  retiredByName?: string | null;
  retirementNotes?: string | null;
  revision: number;
  actionAllowed: boolean;
  actionReason: string;
};

export type MissingDeviceLifecycleSummary = {
  observed: number;
  monitoring: number;
  eligible: number;
  notEvaluated: number;
  retired: number;
  restoreReady: number;
  total: number;
};

export type MissingDeviceLifecycleQueue = {
  summary: MissingDeviceLifecycleSummary;
  items: MissingDeviceLifecycleCandidate[];
  total: number;
};

export type ConnectWiseCiOptions = {
  companyId: string;
  providerCompanyId: string;
  providerCompanyName: string;
  credentialSource: string;
  readOnly: boolean;
  writesAttempted: boolean;
  discovered: number;
  availableTypes: ProviderFilterOption[];
  availableStatuses: ProviderFilterOption[];
  policy: ConnectWiseCiPolicy;
};

export type ProviderCompany = {
  id: string;
  provider: string;
  externalId: string;
  identifier: string;
  name: string;
  status: string;
  type: string;
  site: string;
  deleted: boolean;
  active: boolean;
  lastUpdated?: string;
  lastSeenAt?: string;
  mappingId?: string | null;
  mappedCompanyId?: string | null;
  mappedCompanyName: string;
  suggestedCompanyId?: string | null;
  suggestedCompanyName: string;
  suggestionReason: string;
};

export type SyncRun = {
  id: string;
  type: string;
  status: string;
  message: string;
  startedAt?: string;
  finishedAt?: string;
  discovered?: number;
  imported?: number;
  updated?: number;
  review?: number;
  attributes?: {
    operation?: string;
    trigger?: string;
    companyId?: string;
    providerCompanyId?: string;
    policyId?: string;
    [key: string]: unknown;
  };
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

export type ChangeTemplateParameter = {
  key: string;
  label: string;
  type: 'text' | 'multiline' | 'number' | 'select' | 'boolean';
  required: boolean;
  source: string;
  helpText: string;
  options: string[];
};

export type ChangeTemplateClosureTest = {
  key: string;
  label: string;
  expectedResultTemplate: string;
  required: boolean;
  evidenceRequired: boolean;
};

export type ChangeTemplateContent = {
  titleTemplate: string;
  changeType: string;
  category: string;
  priority: string;
  outageExpected: boolean;
  expectedDurationMinutes: number;
  reasonTemplate: string;
  businessImpactTemplate: string;
  implementationPlanTemplate: string;
  validationPlanTemplate: string;
  rollbackPlanTemplate: string;
  communicationStatus: string;
  communicationPlanTemplate: string;
  suggestedApproverRole: string;
  closureTests: ChangeTemplateClosureTest[];
  parameters: ChangeTemplateParameter[];
};

export type ChangeClosureTest = {
  key: string;
  label: string;
  expectedResult: string;
  required: boolean;
  evidenceRequired: boolean;
  result: 'pending' | 'passed' | 'failed' | 'not_run';
  actualResult: string;
  evidence: string;
  testedBy: string;
  testedAt: string;
};

export type ChangeClosureFollowUp = {
  id: string;
  description: string;
  owner: string;
  dueDate: string;
  status: 'open' | 'completed';
};

export type ChangeClosureAssessment = {
  implementationResult: 'successful' | 'successful_with_issues' | 'partially_implemented' | 'failed' | 'backed_out';
  serviceStatus: 'restored' | 'degraded' | 'unavailable';
  deviations: string;
  unexpectedImpact: string;
  tests: ChangeClosureTest[];
  pirRequired: boolean;
  pirCompleted: boolean;
  lessonsLearned: string;
  followUpActions: ChangeClosureFollowUp[];
  stakeholderConfirmation: 'not_required' | 'confirmed';
  closureSummary: string;
  closedBy: string;
  closedAt: string;
};

export type ChangeTemplate = {
  id: string;
  companyId?: string | null;
  key: string;
  name: string;
  description: string;
  tags: string[];
  status: 'draft' | 'published' | 'retired';
  system: boolean;
  version: number;
  ownerUserId?: string | null;
  reviewDueDate?: string | null;
  content: ChangeTemplateContent;
  createdAt: string;
  updatedAt: string;
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
  assignedUserId?: string | null;
  assignedTechnician: string;
  approver: string;
  notes: string;
  scopeAssetIds: string[];
  revision: number;
  updatedAt: string;
  actualStart: string;
  actualEnd: string;
  actualOutageMinutes: number;
  outcome: 'pending' | 'successful' | 'successful_with_issues' | 'partially_implemented' | 'failed' | 'backed_out' | 'cancelled';
  failureReason: string;
  validationResult: string;
  rollbackExecuted: boolean;
  rollbackResult: string;
  closureNotes: string;
  closureAssessment?: ChangeClosureAssessment;
  approvals: Array<{ id: string; decision: 'approved' | 'declined'; comments: string; actorId?: string | null; actorEmail: string; approverName?: string; responsibilityRole?: string; scope?: Array<{ type: string; id?: string; name?: string }>; createdAt: string }>;
  assignmentHistory?: Array<{ id: string; previousUserId?: string | null; previousDisplayName: string; assignedUserId?: string | null; assignedDisplayName: string; reason: string; actorId?: string | null; actorEmail: string; createdAt: string }>;
  assignmentNotification?: { requested: boolean; queued: boolean; recipient?: string | null };
  templateId?: string | null;
  templateVersion?: number | null;
  templateSnapshot?: { id: string; key: string; name: string; companyId?: string | null; version: number; content: ChangeTemplateContent } | null;
  templateParameters?: Record<string, string | number | boolean>;
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
export type FieldAuthorityCatalogue = {
  fields: Array<{ key: string; label: string; group: string }>;
  providers: Array<{ key: string; name: string }>;
  ciTypes: string[];
  presets: Array<{ key: string; name: string; description: string }>;
};
export type DataQualitySnapshot = {
  summary: { score: number; assetCount: number; findingCount: number; highCount: number; exceptionCount: number; staleDays: number; pendingReconciliationCount: number; byCategory: Record<string, number> };
  customers: DataQualityCustomer[]; findings: DataQualityFinding[]; candidates: ReconciliationCandidate[];
  fieldAuthority: FieldAuthorityRule[];
};
