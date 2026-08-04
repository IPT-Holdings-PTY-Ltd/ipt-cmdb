import { describe, expect, it } from 'vitest';
import {
  buildAssetTechnicalInventory,
  mergeAssetInventoryPayload,
} from './assetInventory';
import type { Asset, AssetInventoryCollections, AssetInventoryPayload } from './types';

function asset(changes: Partial<Asset> = {}): Asset {
  return {
    id: 'asset-1',
    companyId: 'company-1',
    name: 'HV-01',
    type: 'Hypervisor host',
    status: 'Active',
    source: 'ncentral',
    externalId: 'nc-101',
    lastSeen: '2026-07-31T08:00:00Z',
    fields: {},
    metadata: {} as Asset['metadata'],
    ...changes,
  };
}

describe('asset technical inventory', () => {
  it('normalizes rich provider-neutral sections and classification evidence', () => {
    const inventory = buildAssetTechnicalInventory(asset({
      fields: {
        hardware: {
          manufacturer: 'Dell',
          model: 'PowerEdge R750',
          serialNumber: 'ABC123',
          systemUuid: 'uuid-1',
          processors: [{ name: 'Xeon Gold', cores: 16 }],
          memoryModules: [{ bank: 'A1', capacityBytes: 34_359_738_368 }],
          physicalDisks: [{ model: 'PERC disk', sizeBytes: 1_000_000_000_000 }],
        },
        network: {
          hostname: 'hv-01.example.test',
          interfaces: [
            { name: 'NIC 1', ipAddresses: ['10.0.0.10'], macAddress: '00:11:22:33:44:55' },
            { name: 'NIC 2', ipAddresses: ['10.0.1.10'], macAddress: '00:11:22:33:44:66' },
          ],
        },
        operatingSystem: {
          name: 'Windows Server 2025',
          version: '24H2',
          architecture: '64-bit',
        },
        software: {
          applications: [{ name: 'Backup agent', version: '4.2' }],
          roles: [{ name: 'Hyper-V' }],
          features: [{ name: 'Failover clustering' }],
          summary: { applicationCount: 1 },
        },
        virtualization: {
          role: 'host',
          platform: 'Hyper-V',
          classification: {
            proposedType: 'Hypervisor host',
            confidence: 0.98,
            evidence: ['Hyper-V role installed', 'Microsoft virtual-machine services detected'],
          },
        },
        monitoring: {
          lastAgentCheckIn: '2026-07-31T07:55:00Z',
          serviceSummary: { healthy: 12, failed: 1 },
          activeIssueSummary: { critical: 1 },
          inventoryCollectedAt: '2026-07-31T07:50:00Z',
        },
        sourceEvidence: {
          provider: 'ncentral',
          coverage: { hardware: 'complete', software: 'complete' },
          observedAt: '2026-07-31T07:50:00Z',
          fingerprint: 'sha256:test',
        },
      },
    }));

    expect(inventory.hardware.facts).toContainEqual({ label: 'Manufacturer', value: 'Dell' });
    expect(inventory.hardware.collections.find(item => item.key === 'processors')?.records).toHaveLength(1);
    expect(inventory.network.interfaces).toHaveLength(2);
    expect(inventory.software.applications[0]).toMatchObject({ name: 'Backup agent' });
    expect(inventory.virtualization.classification).toMatchObject({
      proposedType: 'Hypervisor host',
      confidence: 0.98,
      evidence: ['Hyper-V role installed', 'Microsoft virtual-machine services detected'],
    });
    expect(inventory.sourceEvidence.observedAt).toBe('2026-07-31T07:50:00Z');
    expect(inventory.coverage.find(section => section.key === 'hardware')?.available).toBe(true);
    expect(inventory.coverage.find(section => section.key === 'lifecycle')?.available).toBe(false);
  });

  it('uses flat legacy fields without inventing missing sections', () => {
    const inventory = buildAssetTechnicalInventory(asset({
      fields: {
        manufacturer: 'Microsoft Corporation',
        model: 'Virtual Machine',
        serialNumber: 'legacy-serial',
        ipAddress: '192.0.2.15',
        macAddress: '00:AA:BB:CC:DD:EE',
        operatingSystem: 'Windows Server',
        lastAgentCheckIn: '2026-07-31T07:40:00Z',
      },
    }));

    expect(inventory.hardware.facts).toContainEqual({
      label: 'Serial number',
      value: 'legacy-serial',
    });
    expect(inventory.network.interfaces).toEqual([
      {
        name: 'Primary interface',
        ipAddress: '192.0.2.15',
        macAddress: '00:AA:BB:CC:DD:EE',
      },
    ]);
    expect(inventory.operatingSystem.facts).toContainEqual({
      label: 'Operating system',
      value: 'Windows Server',
    });
    expect(inventory.software.applications).toEqual([]);
    expect(inventory.coverage.find(section => section.key === 'software')?.available).toBe(false);
  });

  it('shows provider kind/evidence and keeps OS capabilities separate from server roles', () => {
    const original = asset({
      fields: {
        virtualization: {
          kind: 'hypervisor_host',
          platform: 'Hyper-V',
          confidence: 'medium',
          evidence: ['hyperv_virtual_switch_adapter'],
        },
      },
    });
    const merged = mergeAssetInventoryPayload(original, {
      assetId: original.id,
      collections: {
        os_capabilities: [
          { name: 'PowerShellVersion', value: '5.1' },
        ],
      },
    });
    const inventory = buildAssetTechnicalInventory(merged);

    expect(inventory.operatingSystem.capabilities).toEqual([
      { name: 'PowerShellVersion', value: '5.1' },
    ]);
    expect(inventory.software.roles).toEqual([]);
    expect(inventory.virtualization.facts).toContainEqual({
      label: 'Role',
      value: 'hypervisor_host',
    });
    expect(inventory.virtualization.classification).toMatchObject({
      proposedType: 'hypervisor_host',
      confidence: 0.7,
      evidence: ['hyperv_virtual_switch_adapter'],
    });
  });

  it('merges durable collection payloads and preserves non-empty summary fallbacks', () => {
    const original = asset({
      fields: {
        hardware: { memoryModules: [{ bank: 'summary-A1' }] },
        network: { interfaces: [{ name: 'summary NIC' }] },
        software: { roles: [{ name: 'Summary role' }] },
        sourceEvidence: { provider: 'ncentral' },
      },
    });
    const payload: AssetInventoryPayload = {
      assetId: original.id,
      observedAt: '2026-07-31T08:10:00Z',
      collections: {
        network_interfaces: [{ name: 'NIC 1' }, { name: 'NIC 2' }],
        memory_modules: [],
        applications: [{ name: 'SQL Server', version: '2025' }],
        server_roles: [{ name: 'Hyper-V' }],
      },
      snapshots: [{ collection: 'applications', fingerprint: 'sha256:applications' }],
    };

    const merged = mergeAssetInventoryPayload(original, payload);
    const inventory = buildAssetTechnicalInventory(merged);

    expect(inventory.network.interfaces).toHaveLength(2);
    expect(inventory.hardware.collections.find(item => item.key === 'memoryModules')?.records)
      .toEqual([{ bank: 'summary-A1' }]);
    expect(inventory.software.applications).toEqual([{ name: 'SQL Server', version: '2025' }]);
    expect(inventory.software.roles).toEqual([{ name: 'Hyper-V' }]);
    expect(inventory.sourceEvidence.coverage).toMatchObject({
      network_interfaces: 2,
      memory_modules: 0,
      applications: 1,
      server_roles: 1,
    });
    expect(inventory.sourceEvidence.snapshots).toEqual(payload.snapshots);
  });

  it('accepts object-shaped provider collections alongside record lists', () => {
    const collections: AssetInventoryCollections = {
      hardware: { manufacturer: 'Dell', model: 'PowerEdge R750' },
      operating_system: {
        name: 'Windows Server 2025',
        capabilities: [{ name: 'PowerShellVersion', value: '5.1' }],
      },
      software: {
        count: 1,
        items: [{ name: 'Windows Admin Center', version: '2.0' }],
        roles: [{ name: 'Hyper-V' }],
        features: [{ name: 'Failover Clustering' }],
        patches: [{ name: 'KB5000001' }],
      },
      monitoring: {
        maintenanceWindows: [{ name: 'Sunday maintenance' }],
        observations: [{ name: 'Agent healthy' }],
      },
      lifecycle: { warrantyExpiryDate: '2028-08-04' },
      virtualization: { kind: 'hypervisor_host', platform: 'Hyper-V' },
      network_interfaces: [{ name: 'vEthernet (Management)' }],
    };

    const merged = mergeAssetInventoryPayload(asset(), {
      assetId: 'asset-1',
      collections,
    });

    expect(merged.fields.hardware).toMatchObject({
      manufacturer: 'Dell',
      model: 'PowerEdge R750',
    });
    expect(merged.fields.operatingSystem).toMatchObject({ name: 'Windows Server 2025' });
    expect(merged.fields.software).toMatchObject({
      applications: [{ name: 'Windows Admin Center', version: '2.0' }],
      roles: [{ name: 'Hyper-V' }],
      features: [{ name: 'Failover Clustering' }],
      patches: [{ name: 'KB5000001' }],
    });
    expect(merged.fields.monitoring).toMatchObject({
      maintenanceWindows: [{ name: 'Sunday maintenance' }],
      observations: [{ name: 'Agent healthy' }],
    });
    expect(merged.fields.lifecycle).toMatchObject({ warrantyExpiryDate: '2028-08-04' });
    expect(merged.fields.virtualization).toMatchObject({
      kind: 'hypervisor_host',
      platform: 'Hyper-V',
    });
    expect(merged.fields.sourceEvidence).toMatchObject({
      coverage: {
        hardware: 1,
        operating_system: 1,
        software: 1,
        monitoring: 1,
        lifecycle: 1,
        virtualization: 1,
        network_interfaces: 1,
      },
    });
  });

  it('does not mistake a direct inventory row payload for a repository snapshot', () => {
    const merged = mergeAssetInventoryPayload(asset(), {
      assetId: 'asset-1',
      collections: {
        applications: [
          {
            name: 'Collector agent',
            version: '4.0',
            payload: { channel: 'stable' },
          },
        ],
      },
    });

    expect(merged.fields.software).toMatchObject({
      applications: [
        {
          name: 'Collector agent',
          version: '4.0',
          payload: { channel: 'stable' },
        },
      ],
    });
  });

  it('leaves the asset unchanged for an inventory payload belonging to another asset', () => {
    const original = asset();
    expect(mergeAssetInventoryPayload(original, {
      assetId: 'asset-2',
      collections: { applications: [{ name: 'Wrong asset' }] },
    })).toBe(original);
  });

  it('unwraps repository snapshot collections and uses canonical network-interface rows', () => {
    const merged = mergeAssetInventoryPayload(asset(), {
      assetId: 'asset-1',
      collections: {
        hardware: [{
          collectionType: 'hardware',
          payload: { manufacturer: 'Dell', model: 'PowerEdge R750', serialNumber: 'server-1' },
          itemCount: 1,
          fingerprint: 'sha256:hardware',
          lastObservedAt: '2026-07-31T09:04:00Z',
        }],
        operating_system: [{
          collectionType: 'operating_system',
          payload: { name: 'Windows Server 2025', architecture: '64-bit' },
          itemCount: 1,
          fingerprint: 'sha256:os',
          lastObservedAt: '2026-07-31T09:04:00Z',
        }],
        applications: [{
          collectionType: 'applications',
          payload: [
            { name: 'Sage 200', version: '2026' },
            { name: 'Microsoft SQL Server', version: '2025' },
          ],
          fingerprint: 'sha256:apps',
          itemCount: 2,
          lastObservedAt: '2026-07-31T09:00:00Z',
          supersededAt: null,
          provider: 'ncentral',
        }],
        software: [{
          collectionType: 'software',
          payload: {
            count: 2,
            publishers: ['Microsoft', 'Sage'],
            truncated: false,
            items: [
              { name: 'Sage 200', version: '2026' },
              { name: 'Microsoft SQL Server', version: '2025' },
            ],
          },
          fingerprint: 'sha256:software',
          itemCount: 2,
          lastObservedAt: '2026-07-31T09:00:00Z',
          supersededAt: null,
        }],
        virtualization: [{
          collectionType: 'virtualization',
          payload: {
            role: 'host',
            platform: 'Hyper-V',
            classification: {
              proposedType: 'Hypervisor host',
              confidence: 0.96,
              evidence: ['Hyper-V server feature detected'],
            },
          },
          fingerprint: 'sha256:virtualization',
          itemCount: 1,
          lastObservedAt: '2026-07-31T09:03:00Z',
        }],
        volumes: [{
          collectionType: 'volumes',
          payload: [{ name: 'C:', sizeBytes: 500_000_000_000 }],
          fingerprint: 'sha256:volumes',
          itemCount: 1,
          lastObservedAt: '2026-07-31T08:55:00Z',
          supersededAt: null,
        }],
      },
      networkInterfaces: [{
        name: 'Production NIC',
        ipAddresses: ['10.0.0.20'],
        lastObservedAt: '2026-07-31T09:05:00Z',
      }],
    });
    const inventory = buildAssetTechnicalInventory(merged);

    expect(inventory.software.applications).toHaveLength(2);
    expect(inventory.hardware.facts).toContainEqual({ label: 'Manufacturer', value: 'Dell' });
    expect(inventory.operatingSystem.facts).toContainEqual({
      label: 'Operating system',
      value: 'Windows Server 2025',
    });
    expect(inventory.virtualization.classification.proposedType).toBe('Hypervisor host');
    expect(inventory.hardware.collections.find(item => item.key === 'logicalVolumes')?.records)
      .toEqual([{ name: 'C:', sizeBytes: 500_000_000_000 }]);
    expect(inventory.network.interfaces).toHaveLength(1);
    expect(inventory.sourceEvidence.coverage).toMatchObject({
      applications: 2,
      volumes: 1,
      network_interfaces: 1,
    });
    expect(inventory.sourceEvidence.observedAt).toBe('2026-07-31T09:05:00Z');
    expect(inventory.sourceEvidence.snapshots?.[0]).not.toHaveProperty('payload');
  });
});
