import type { Asset, Relationship } from './types';

export type DisplayLayer = 'business' | 'application' | 'compute' | 'virtualization' | 'storage' | 'network' | 'foundation';

export const displayLayerOrder: DisplayLayer[] = ['foundation', 'network', 'storage', 'virtualization', 'compute', 'application', 'business'];

export const displayLayerLabels: Record<DisplayLayer, string> = {
  foundation: 'Physical / cloud foundation',
  network: 'Network and security',
  storage: 'Storage and backup',
  virtualization: 'Virtualization',
  compute: 'Compute and operating systems',
  application: 'Applications and data',
  business: 'Business systems',
};

export const displayLayerColors: Record<DisplayLayer, string> = {
  foundation: '#8294b8',
  network: '#50d5b9',
  storage: '#f2c14e',
  virtualization: '#56b4e9',
  compute: '#6798e8',
  application: '#a68cf0',
  business: '#f1d372',
};

export function displayLayer(asset: Asset): DisplayLayer {
  const explicit = asset.metadata?.displayLayer as DisplayLayer;
  if (displayLayerOrder.includes(explicit)) return explicit;
  const type = asset.type.toLowerCase();
  const signal = `${type} ${asset.name} ${asset.metadata?.vendor || ''}`.toLowerCase();
  if (type === 'business system') return 'business';
  if (type.includes('credential') || type.includes('person')) return 'business';
  if (type.includes('software') || type.includes('service') || type.includes('licence') || type.includes('license')) return /backup|veeam|repository/.test(signal) ? 'storage' : 'application';
  if (type === 'datastore' || type === 'storage array') return 'storage';
  if (type.includes('virtualization') || type === 'hypervisor host' || type === 'virtual network') return 'virtualization';
  if (type.includes('network') || type.includes('vpn') || /firewall|switch|router|wan|internet/.test(signal)) return /wan|internet/.test(signal) ? 'foundation' : 'network';
  if (type === 'virtual machine' || type.includes('server') || type.includes('workstation') || type === 'device') return 'compute';
  return 'foundation';
}

const impactRules: Record<string, { reverseForImpact: boolean; traversesImpact: boolean }> = {
  connected_to: { reverseForImpact: false, traversesImpact: true },
  depends_on: { reverseForImpact: true, traversesImpact: true },
  installed_on: { reverseForImpact: true, traversesImpact: true },
  licensed_to: { reverseForImpact: false, traversesImpact: true },
  used_by: { reverseForImpact: false, traversesImpact: true },
  related_to: { reverseForImpact: false, traversesImpact: false },
  hosts: { reverseForImpact: false, traversesImpact: true },
  backs_up: { reverseForImpact: false, traversesImpact: false },
  managed_by: { reverseForImpact: true, traversesImpact: true },
  member_of: { reverseForImpact: false, traversesImpact: false },
  stored_on: { reverseForImpact: true, traversesImpact: true },
  provided_by: { reverseForImpact: true, traversesImpact: true },
  protected_by: { reverseForImpact: true, traversesImpact: true },
};

export function traverseImpact(startId: string, relationships: Relationship[], reverse = false) {
  const adjacency = new Map<string, string[]>();
  relationships.forEach(relationship => {
    const rule = impactRules[relationship.type] || impactRules.related_to;
    if (!rule.traversesImpact || relationship.impactPolicy === 'informational') return;
    const endpoints = rule.reverseForImpact
      ? { source: relationship.toId, target: relationship.fromId }
      : { source: relationship.fromId, target: relationship.toId };
    const from = reverse ? endpoints.target : endpoints.source;
    const to = reverse ? endpoints.source : endpoints.target;
    adjacency.set(from, [...(adjacency.get(from) || []), to]);
  });
  const visited = new Set<string>([startId]);
  const queue = [startId];
  while (queue.length) {
    const current = queue.shift()!;
    (adjacency.get(current) || []).forEach(next => {
      if (!visited.has(next)) { visited.add(next); queue.push(next); }
    });
  }
  visited.delete(startId);
  return visited;
}

export function businessApplicationMemberships(assets: Asset[], relationships: Relationship[]) {
  const memberships = new Map<string, Asset[]>();
  assets.filter(asset => asset.type === 'Business system').forEach(system => {
    const scope = new Set([system.id, ...traverseImpact(system.id, relationships, true)]);
    scope.forEach(assetId => memberships.set(assetId, [...(memberships.get(assetId) || []), system]));
  });
  memberships.forEach(systems => systems.sort((left, right) => left.name.localeCompare(right.name)));
  return memberships;
}
