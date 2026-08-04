import type {
  Asset,
  AssetClassificationEvidence,
  AssetInventoryPayload,
  AssetInventoryRecord,
  AssetInventorySnapshot,
  AssetSourceEvidence,
} from './types';

export type InventoryFact = {
  label: string;
  value: unknown;
};

export type InventoryCollection = {
  key: string;
  label: string;
  records: AssetInventoryRecord[];
};

export type InventoryCoverageItem = {
  key: InventorySectionKey;
  label: string;
  available: boolean;
  count?: number;
};

export type InventorySectionKey =
  | 'hardware'
  | 'network'
  | 'operatingSystem'
  | 'software'
  | 'virtualization'
  | 'monitoring'
  | 'lifecycle';

export type AssetTechnicalInventory = {
  hardware: {
    facts: InventoryFact[];
    collections: InventoryCollection[];
  };
  network: {
    facts: InventoryFact[];
    interfaces: AssetInventoryRecord[];
  };
  operatingSystem: {
    facts: InventoryFact[];
    capabilities: AssetInventoryRecord[];
  };
  software: {
    summary: InventoryFact[];
    applications: AssetInventoryRecord[];
    roles: AssetInventoryRecord[];
    features: AssetInventoryRecord[];
    patches: AssetInventoryRecord[];
  };
  virtualization: {
    facts: InventoryFact[];
    classification: AssetClassificationEvidence;
  };
  monitoring: {
    facts: InventoryFact[];
    serviceSummary: InventoryFact[];
    activeIssueSummary: InventoryFact[];
    maintenanceWindows: AssetInventoryRecord[];
    observations: AssetInventoryRecord[];
  };
  lifecycle: {
    facts: InventoryFact[];
  };
  sourceEvidence: AssetSourceEvidence;
  coverage: InventoryCoverageItem[];
  observedAt: string;
};

const sectionLabels: Record<InventorySectionKey, string> = {
  hardware: 'Hardware',
  network: 'Network',
  operatingSystem: 'Operating system',
  software: 'Software',
  virtualization: 'Virtualization',
  monitoring: 'Monitoring',
  lifecycle: 'Lifecycle',
};

function asRecord(value: unknown): AssetInventoryRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as AssetInventoryRecord
    : {};
}

function hasValue(value: unknown): boolean {
  if (value === null || value === undefined || value === '') return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value as object).length > 0;
  return true;
}

function firstValue(record: AssetInventoryRecord, keys: string[]): unknown {
  return keys.map(key => record[key]).find(hasValue);
}

function recordList(...values: unknown[]): AssetInventoryRecord[] {
  const value = values.find(item => Array.isArray(item));
  if (!Array.isArray(value)) return [];
  return value.flatMap(item => {
    if (item && typeof item === 'object' && !Array.isArray(item)) {
      return [item as AssetInventoryRecord];
    }
    return hasValue(item) ? [{ name: item }] : [];
  });
}

function facts(
  record: AssetInventoryRecord,
  definitions: Array<[label: string, keys: string[]]>,
): InventoryFact[] {
  return definitions.flatMap(([label, keys]) => {
    const value = firstValue(record, keys);
    return hasValue(value) ? [{ label, value }] : [];
  });
}

function genericFacts(record: AssetInventoryRecord): InventoryFact[] {
  return Object.entries(record)
    .filter(([, value]) => hasValue(value) && typeof value !== 'object')
    .map(([key, value]) => ({ label: humanizeInventoryKey(key), value }));
}

function collection(
  key: string,
  label: string,
  ...values: unknown[]
): InventoryCollection {
  return { key, label, records: recordList(...values) };
}

function isAvailable(factList: InventoryFact[], recordLists: AssetInventoryRecord[][] = []) {
  return factList.length > 0 || recordLists.some(records => records.length > 0);
}

function confidenceScore(value: unknown): number {
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric;
  const label = String(value || '').toLowerCase();
  return { high: 0.9, medium: 0.7, low: 0.4 }[label as 'high' | 'medium' | 'low'] || 0;
}

/** Convert provider field names into compact, user-facing labels. */
export function humanizeInventoryKey(value: string): string {
  return value
    .replaceAll(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replaceAll(/[_-]+/g, ' ')
    .replaceAll(/\b\w/g, match => match.toUpperCase());
}

/**
 * Normalize current and future provider field shapes into a stable asset-detail
 * presentation model. Provider-specific keys are accepted as fallbacks, while
 * the provider-neutral nested contract remains the preferred source.
 */
export function buildAssetTechnicalInventory(asset: Asset): AssetTechnicalInventory {
  const root = asRecord(asset.fields);
  const hardware = asRecord(root.hardware);
  const network = asRecord(root.network);
  const operatingSystemValue = root.operatingSystem;
  const operatingSystem = typeof operatingSystemValue === 'string'
    ? { name: operatingSystemValue }
    : asRecord(operatingSystemValue);
  const software = asRecord(root.software);
  const virtualization = asRecord(root.virtualization);
  const classification = asRecord(virtualization.classification);
  const monitoring = asRecord(root.monitoring);
  const lifecycle = asRecord(root.lifecycle);
  const sourceEvidence = asRecord(root.sourceEvidence);

  const hardwareFacts = facts(
    {
      manufacturer: asset.metadata?.vendor,
      model: asset.metadata?.model,
      ...root,
      ...hardware,
    },
    [
      ['Manufacturer', ['manufacturer', 'vendor']],
      ['Model', ['model']],
      ['Serial number', ['serialNumber', 'serialnumber']],
      ['System UUID', ['systemUuid', 'systemUUID', 'uuid', 'biosUuid']],
      ['Asset tag', ['assetTag']],
    ],
  );
  const hardwareCollections = [
    collection('processors', 'Processors', hardware.processors, root.processors),
    collection('memoryModules', 'Memory modules', hardware.memoryModules, root.memoryModules),
    collection('physicalDisks', 'Physical disks', hardware.physicalDisks, root.physicalDisks),
    collection('logicalVolumes', 'Logical volumes', hardware.logicalVolumes, root.logicalVolumes),
  ];
  const bios = asRecord(hardware.bios);
  if (Object.keys(bios).length) {
    hardwareCollections.push({ key: 'bios', label: 'BIOS', records: [bios] });
  }

  let interfaces = recordList(
    network.interfaces,
    root.networkInterfaces,
    root.networkAdapters,
  );
  const flatIp = firstValue(root, ['ipAddress', 'ip']);
  const flatMac = firstValue(root, ['macAddress', 'mac']);
  if (!interfaces.length && (hasValue(flatIp) || hasValue(flatMac))) {
    interfaces = [{ name: 'Primary interface', ipAddress: flatIp, macAddress: flatMac }];
  }
  const networkFacts = facts(
    {
      ipAddress: asset.metadata?.ipAddress,
      networkZone: asset.metadata?.networkZone,
      vlanId: asset.metadata?.vlanId,
      subnet: asset.metadata?.subnet,
      ...root,
      ...network,
    },
    [
      ['Hostname', ['hostname', 'hostName', 'fqdn']],
      ['Domain', ['domain', 'domainName']],
      ['Primary IP', ['ipAddress', 'primaryIpAddress', 'ip']],
      ['Network zone', ['networkZone']],
      ['VLAN', ['vlanId', 'vlan']],
      ['Subnet', ['subnet']],
    ],
  );

  const operatingSystemFacts = facts(
    { operatingSystem: asset.metadata?.guestOs, ...root, ...operatingSystem },
    [
      ['Operating system', ['name', 'caption', 'operatingSystem', 'osName']],
      ['Version', ['version', 'osVersion']],
      ['Architecture', ['architecture', 'osArchitecture']],
      ['Installed', ['installDate', 'installedAt']],
      ['Last boot', ['lastBoot', 'lastBootTime']],
    ],
  );
  const operatingSystemCapabilities = recordList(
    operatingSystem.capabilities,
    root.osCapabilities,
  );

  const applications = recordList(
    software.applications,
    root.applications,
    root.installedSoftware,
  );
  const roles = recordList(software.roles, root.serverRoles);
  const features = recordList(software.features, root.serverFeatures);
  const patches = recordList(software.patches, root.patches);
  const softwareSummaryRecord = asRecord(software.summary);
  const softwareSummary = genericFacts(softwareSummaryRecord);

  const virtualizationFacts = facts(
    {
      platform: asset.metadata?.virtualizationPlatform,
      clusterName: asset.metadata?.clusterName,
      powerState: asset.metadata?.powerState,
      ...root,
      ...virtualization,
    },
    [
      ['Role', ['role', 'kind', 'virtualizationRole']],
      ['Platform', ['platform', 'virtualizationPlatform']],
      ['Host', ['hostName', 'host']],
      ['Cluster', ['clusterName', 'cluster']],
      ['Power state', ['powerState']],
    ],
  );
  const normalizedClassification: AssetClassificationEvidence = {
    proposedType: String(
      firstValue(classification, ['proposedType', 'type'])
        || firstValue(virtualization, ['proposedType'])
        || firstValue(virtualization, ['kind'])
        || '',
    ),
    confidence: confidenceScore(
      firstValue(classification, ['confidence'])
        || firstValue(virtualization, ['classificationConfidence'])
        || firstValue(virtualization, ['confidence'])
        || 0,
    ),
    evidence: recordList(
      classification.evidence,
      virtualization.classificationEvidence,
      virtualization.evidence,
    ).map(record => String(record.name || record.label || record.value || '')).filter(Boolean),
  };

  const monitoringFacts = facts(
    { operationalStatus: asset.metadata?.operationalStatus, ...root, ...monitoring },
    [
      ['Last agent check-in', ['lastAgentCheckIn', 'lastCheckIn']],
      ['Inventory collected', ['inventoryCollectedAt', 'collectedAt']],
      ['Monitoring state', ['state', 'status', 'operationalStatus']],
      ['Last transition', ['lastTransitionAt']],
      ['Stale after', ['staleAfter']],
    ],
  );
  const serviceSummary = genericFacts(asRecord(monitoring.serviceSummary));
  const activeIssueSummary = genericFacts(asRecord(monitoring.activeIssueSummary));
  const maintenanceWindows = recordList(
    monitoring.maintenanceWindows,
    root.maintenanceWindows,
  );
  const monitoringObservations = recordList(monitoring.observations);

  const lifecycleFacts = facts(
    {
      purchaseDate: asset.metadata?.purchaseDate,
      warrantyExpiryDate: asset.metadata?.warrantyEnd,
      expectedReplacementDate: asset.metadata?.endOfLifeDate,
      ...root,
      ...lifecycle,
    },
    [
      ['Purchased', ['purchaseDate']],
      ['Warranty expires', ['warrantyExpiryDate', 'warrantyEnd']],
      ['Lease expires', ['leaseExpiryDate']],
      ['Expected replacement', ['expectedReplacementDate', 'replacementDate']],
      ['Cost', ['cost', 'purchaseCost']],
      ['Location', ['location']],
      ['Asset tag', ['assetTag']],
    ],
  );

  const normalizedEvidence: AssetSourceEvidence = {
    provider: String(sourceEvidence.provider || asset.source || ''),
    coverage: asRecord(sourceEvidence.coverage),
    observedAt: String(
      sourceEvidence.observedAt
        || monitoring.inventoryCollectedAt
        || asset.lastSeen
        || '',
    ),
    fingerprint: String(sourceEvidence.fingerprint || ''),
    partialErrors: recordList(sourceEvidence.partialErrors)
      .map(record => String(record.name || record.message || record.error || ''))
      .filter(Boolean),
    snapshots: recordList(sourceEvidence.snapshots),
  };

  const sectionState: Record<InventorySectionKey, { available: boolean; count?: number }> = {
    hardware: {
      available: isAvailable(
        hardwareFacts,
        hardwareCollections.map(item => item.records),
      ),
      count: hardwareCollections.reduce((total, item) => total + item.records.length, 0),
    },
    network: { available: isAvailable(networkFacts, [interfaces]), count: interfaces.length },
    operatingSystem: {
      available: isAvailable(operatingSystemFacts, [operatingSystemCapabilities]),
      count: operatingSystemCapabilities.length,
    },
    software: {
      available: isAvailable(softwareSummary, [applications, roles, features, patches]),
      count: applications.length + roles.length + features.length + patches.length,
    },
    virtualization: {
      available: virtualizationFacts.length > 0
        || hasValue(normalizedClassification.proposedType)
        || normalizedClassification.evidence!.length > 0,
    },
    monitoring: {
      available: isAvailable(
        [...monitoringFacts, ...serviceSummary, ...activeIssueSummary],
        [maintenanceWindows, monitoringObservations],
      ),
      count: maintenanceWindows.length + monitoringObservations.length,
    },
    lifecycle: { available: lifecycleFacts.length > 0 },
  };
  const coverage = (Object.keys(sectionLabels) as InventorySectionKey[]).map(key => ({
    key,
    label: sectionLabels[key],
    ...sectionState[key],
  }));

  return {
    hardware: { facts: hardwareFacts, collections: hardwareCollections },
    network: { facts: networkFacts, interfaces },
    operatingSystem: { facts: operatingSystemFacts, capabilities: operatingSystemCapabilities },
    software: { summary: softwareSummary, applications, roles, features, patches },
    virtualization: {
      facts: virtualizationFacts,
      classification: normalizedClassification,
    },
    monitoring: {
      facts: monitoringFacts,
      serviceSummary,
      activeIssueSummary,
      maintenanceWindows,
      observations: monitoringObservations,
    },
    lifecycle: { facts: lifecycleFacts },
    sourceEvidence: normalizedEvidence,
    coverage,
    observedAt: normalizedEvidence.observedAt || '',
  };
}

function inventoryCollectionPayload(
  collections: AssetInventoryRecord,
  keys: string[],
): unknown {
  const key = keys.find(candidate => candidate in collections);
  if (!key) return undefined;
  const value = collections[key];
  if (!Array.isArray(value)) return value;
  const entries = recordList(value);
  const snapshots = entries.filter(isInventorySnapshot);
  const snapshot = snapshots.find(entry => !entry.supersededAt) || snapshots[0];
  return snapshot ? snapshot.payload : value;
}

function isInventorySnapshot(value: unknown): value is AssetInventorySnapshot {
  const record = asRecord(value);
  return Object.prototype.hasOwnProperty.call(record, 'payload')
    && typeof record.collectionType === 'string'
    && typeof record.fingerprint === 'string';
}

function inventoryCollectionRecords(
  collections: AssetInventoryRecord,
  keys: string[],
): AssetInventoryRecord[] {
  return recordList(inventoryCollectionPayload(collections, keys));
}

function preferInventoryCollection(
  collections: AssetInventoryRecord,
  keys: string[],
  fallback: unknown,
): AssetInventoryRecord[] | undefined {
  const records = inventoryCollectionRecords(collections, keys);
  return records.length ? records : (Array.isArray(fallback) ? recordList(fallback) : undefined);
}

function inventorySnapshotSummaries(collections: AssetInventoryRecord): AssetInventoryRecord[] {
  return Object.entries(collections).flatMap(([collectionType, value]) =>
    recordList(value)
      .filter(isInventorySnapshot)
      .map(({ payload: _payload, ...record }) => ({
        ...record,
        collectionType: String(record.collectionType || collectionType),
      }) as AssetInventoryRecord));
}

function inventoryCoverage(collections: AssetInventoryRecord) {
  return Object.fromEntries(Object.entries(collections).map(([key, value]) => {
    const entries = recordList(value);
    const snapshots = entries.filter(isInventorySnapshot);
    const current = snapshots.find(entry => !entry.supersededAt) || snapshots[0];
    const direct = asRecord(value);
    const directCount = Number(direct.count);
    const count = current && Number.isFinite(Number(current.itemCount))
      ? Number(current.itemCount)
      : snapshots.length
        ? inventoryCollectionRecords(collections, [key]).length
        : Array.isArray(value)
          ? entries.length
          : Number.isFinite(directCount)
            ? directCount
            : Object.keys(direct).length
              ? 1
              : 0;
    return [key, count];
  }));
}

/**
 * Layer durable inventory collections over the summary asset response without
 * mutating either payload. Empty optional collections do not erase summary
 * evidence retained on the asset.
 */
export function mergeAssetInventoryPayload(
  asset: Asset,
  payload: AssetInventoryPayload | null | undefined,
): Asset {
  if (!payload || (payload.assetId && payload.assetId !== asset.id)) return asset;
  const fields = asRecord(asset.fields);
  const hardware = asRecord(fields.hardware);
  const network = asRecord(fields.network);
  const software = asRecord(fields.software);
  const monitoring = asRecord(fields.monitoring);
  const sourceEvidence = asRecord(fields.sourceEvidence);
  const collections = asRecord(payload.collections);
  const persistedHardware = asRecord(inventoryCollectionPayload(collections, ['hardware']));
  const persistedOperatingSystem = asRecord(
    inventoryCollectionPayload(collections, ['operating_system', 'operatingSystem']),
  );
  const persistedSoftware = asRecord(inventoryCollectionPayload(collections, ['software']));
  const persistedMonitoring = asRecord(inventoryCollectionPayload(collections, ['monitoring']));
  const persistedLifecycle = asRecord(inventoryCollectionPayload(collections, ['lifecycle']));
  const persistedVirtualization = asRecord(
    inventoryCollectionPayload(collections, ['virtualization']),
  );
  const repositorySnapshots = inventorySnapshotSummaries(collections);
  const networkInterfaces = recordList(payload.networkInterfaces);
  const snapshotObservedAt = [...repositorySnapshots, ...networkInterfaces]
    .map(record => String(record['lastObservedAt'] || ''))
    .filter(Boolean)
    .sort()
    .at(-1);

  return {
    ...asset,
    fields: {
      ...fields,
      hardware: {
        ...hardware,
        ...persistedHardware,
        processors: preferInventoryCollection(
          collections,
          ['processors'],
          persistedHardware.processors || hardware.processors,
        ),
        memoryModules: preferInventoryCollection(
          collections,
          ['memory_modules'],
          persistedHardware.memoryModules || hardware.memoryModules,
        ),
        physicalDisks: preferInventoryCollection(
          collections,
          ['physical_disks'],
          persistedHardware.physicalDisks || hardware.physicalDisks,
        ),
        logicalVolumes: preferInventoryCollection(
          collections,
          ['logical_volumes', 'volumes'],
          persistedHardware.logicalVolumes || hardware.logicalVolumes,
        ),
      },
      operatingSystem: {
        ...(typeof fields.operatingSystem === 'string'
          ? { name: fields.operatingSystem }
          : asRecord(fields.operatingSystem)),
        ...persistedOperatingSystem,
        capabilities: preferInventoryCollection(
          collections,
          ['os_capabilities'],
          persistedOperatingSystem.capabilities
            || asRecord(fields.operatingSystem).capabilities,
        ),
      },
      network: {
        ...network,
        interfaces: networkInterfaces.length
          ? networkInterfaces
          : preferInventoryCollection(
            collections,
            ['network_interfaces'],
            network.interfaces,
          ),
      },
      software: {
        ...software,
        ...persistedSoftware,
        summary: {
          ...asRecord(software.summary),
          ...Object.fromEntries(
            Object.entries(persistedSoftware)
              .filter(([key]) => !['items', 'applications'].includes(key)),
          ),
        },
        applications: recordList(
          persistedSoftware.items || persistedSoftware.applications,
        ).length
          ? recordList(persistedSoftware.items || persistedSoftware.applications)
          : preferInventoryCollection(
            collections,
            ['applications'],
            software.applications,
          ),
        roles: preferInventoryCollection(
          collections,
          ['server_roles'],
          persistedSoftware.roles || software.roles,
        ),
        features: preferInventoryCollection(
          collections,
          ['server_features'],
          persistedSoftware.features || software.features,
        ),
        patches: preferInventoryCollection(
          collections,
          ['patches'],
          persistedSoftware.patches || software.patches,
        ),
      },
      monitoring: {
        ...monitoring,
        ...persistedMonitoring,
        maintenanceWindows: preferInventoryCollection(
          collections,
          ['maintenance_windows'],
          persistedMonitoring.maintenanceWindows || monitoring.maintenanceWindows,
        ),
        observations: preferInventoryCollection(
          collections,
          ['monitoring'],
          persistedMonitoring.observations || monitoring.observations,
        ),
      },
      lifecycle: {
        ...asRecord(fields.lifecycle),
        ...persistedLifecycle,
      },
      virtualization: {
        ...asRecord(fields.virtualization),
        ...persistedVirtualization,
      },
      sourceEvidence: {
        ...sourceEvidence,
        observedAt: String(
          payload.observedAt
            || snapshotObservedAt
            || sourceEvidence.observedAt
            || '',
        ),
        coverage: {
          ...asRecord(sourceEvidence.coverage),
          ...inventoryCoverage(collections),
          ...(networkInterfaces.length ? { network_interfaces: networkInterfaces.length } : {}),
        },
        snapshots: Array.isArray(payload.snapshots) && payload.snapshots.length
          ? payload.snapshots
          : repositorySnapshots.length
            ? repositorySnapshots
            : recordList(sourceEvidence.snapshots),
      },
    },
  };
}
