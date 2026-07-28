import AccountTreeOutlined from '@mui/icons-material/AccountTreeOutlined';
import AppsOutlined from '@mui/icons-material/AppsOutlined';
import AutoAwesomeOutlined from '@mui/icons-material/AutoAwesomeOutlined';
import CloseOutlined from '@mui/icons-material/CloseOutlined';
import ComputerOutlined from '@mui/icons-material/ComputerOutlined';
import DeleteOutlined from '@mui/icons-material/DeleteOutlined';
import FitScreenOutlined from '@mui/icons-material/FitScreenOutlined';
import GridViewOutlined from '@mui/icons-material/GridViewOutlined';
import KeyOutlined from '@mui/icons-material/KeyOutlined';
import LockOpenOutlined from '@mui/icons-material/LockOpenOutlined';
import LockOutlined from '@mui/icons-material/LockOutlined';
import PersonOutlined from '@mui/icons-material/PersonOutlined';
import PostAddOutlined from '@mui/icons-material/PostAddOutlined';
import RouterOutlined from '@mui/icons-material/RouterOutlined';
import SearchOutlined from '@mui/icons-material/SearchOutlined';
import StorageOutlined from '@mui/icons-material/StorageOutlined';
import BusinessCenterOutlined from '@mui/icons-material/BusinessCenterOutlined';
import {
  Alert, Autocomplete, Box, Button, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle, Divider,
  FormControl, FormControlLabel, IconButton, InputAdornment, InputLabel, MenuItem, Paper, Select, Stack, Switch, TextField, Tooltip, Typography,
} from '@mui/material';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Background, Controls, Handle, MarkerType, MiniMap, Panel, Position, ReactFlow, useEdgesState, useNodesState,
  type Connection, type Edge, type FinalConnectionState, type Node, type NodeProps, type ReactFlowInstance,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useNavigate, useSearchParams } from 'react-router';
import { createRelationshipSearchParams, readRelationshipRouteState } from './routeState';
import { apiFetch, getSession } from './session';
import { Title } from './ui';
import { displayLayer, displayLayerLabels, displayLayerOrder, traverseImpact as traverse, type DisplayLayer } from './topology';
import type { Asset, Relationship } from './types';
import { useWorkspace } from './workspace';

type RelationshipType = 'connected_to' | 'depends_on' | 'installed_on' | 'licensed_to' | 'used_by' | 'related_to' | 'hosts' | 'backs_up' | 'managed_by' | 'member_of' | 'stored_on' | 'provided_by' | 'protected_by';
type ImpactRole = 'selected' | 'upstream' | 'downstream' | 'neutral';
type LayoutDirection = 'RIGHT' | 'DOWN';
type TopologyView = 'BUSINESS' | 'APPLICATION' | 'VIRTUALIZATION' | 'NETWORK' | 'STORAGE' | 'STACK' | 'TECHNICAL';
type ImpactPolicy = Relationship['impactPolicy'];
type SavedPositions = Record<string, { x: number; y: number }>;
type CiNodeData = {
  asset: Asset;
  connectable: boolean;
  dimmed?: boolean;
  impactRole?: ImpactRole;
  sharedBusinessAppCount?: number;
};

const relationshipTypes: Record<RelationshipType, {
  label: string;
  impactLabel: string;
  description: string;
  color: string;
  dash?: string;
  directed: boolean;
  reverseForImpact: boolean;
  traversesImpact: boolean;
}> = {
  connected_to: { label: 'connected to', impactLabel: 'connected', description: 'Physical or logical connectivity', color: '#50d5b9', directed: false, reverseForImpact: false, traversesImpact: true },
  depends_on: { label: 'depends on', impactLabel: 'supports', description: 'The target supports the dependent CI', color: '#f0a45d', dash: '9 5', directed: true, reverseForImpact: true, traversesImpact: true },
  installed_on: { label: 'installed on', impactLabel: 'software installed', description: 'The host supports installed software', color: '#a68cf0', dash: '4 5', directed: true, reverseForImpact: true, traversesImpact: true },
  licensed_to: { label: 'licensed to', impactLabel: 'licensed to', description: 'Licence allocation or entitlement', color: '#efc46b', dash: '2 5', directed: true, reverseForImpact: false, traversesImpact: true },
  used_by: { label: 'used by', impactLabel: 'used by', description: 'Consumer or owner association', color: '#74c89e', directed: true, reverseForImpact: false, traversesImpact: true },
  related_to: { label: 'related to', impactLabel: 'related', description: 'Contextual association without impact propagation', color: '#9aaac5', dash: '2 6', directed: false, reverseForImpact: false, traversesImpact: false },
  hosts: { label: 'hosts', impactLabel: 'hosts', description: 'Current runtime placement of a virtual machine on a hypervisor host', color: '#56b4e9', directed: true, reverseForImpact: false, traversesImpact: true },
  backs_up: { label: 'backs up', impactLabel: 'backup', description: 'Backup coverage; the relationship does not imply immediate workload outage', color: '#69c17d', dash: '3 6', directed: true, reverseForImpact: false, traversesImpact: false },
  managed_by: { label: 'managed by', impactLabel: 'management', description: 'The target provides the management plane for the subject', color: '#be8cff', dash: '6 4', directed: true, reverseForImpact: true, traversesImpact: true },
  member_of: { label: 'member of', impactLabel: 'membership', description: 'Logical cluster or resource-pool membership without direct outage propagation', color: '#8294b8', dash: '2 5', directed: true, reverseForImpact: false, traversesImpact: false },
  stored_on: { label: 'stored on', impactLabel: 'storage', description: 'The target datastore stores the virtual machine disks', color: '#f2c14e', directed: true, reverseForImpact: true, traversesImpact: true },
  provided_by: { label: 'provided by', impactLabel: 'provided by', description: 'The target storage or platform provides the subject resource', color: '#e39d4d', directed: true, reverseForImpact: true, traversesImpact: true },
  protected_by: { label: 'protected by', impactLabel: 'HA protection', description: 'The target cluster or resilience service protects the workload', color: '#50d5b9', dash: '8 4', directed: true, reverseForImpact: true, traversesImpact: true },
};

const virtualizationCiTypes = new Set(['virtual machine', 'hypervisor host', 'virtualization cluster', 'datastore', 'storage array', 'virtual network', 'virtualization manager']);
const perspectiveLabels: Record<TopologyView, string> = {
  BUSINESS: 'Business impact', APPLICATION: 'Application stack', VIRTUALIZATION: 'Virtualization',
  NETWORK: 'Network', STORAGE: 'Storage & backup', STACK: 'Full stack', TECHNICAL: 'All technical CIs',
};

function isVirtualizationCi(asset: Asset) { return virtualizationCiTypes.has(asset.type.toLowerCase()); }

function isNetworkPerspectiveAsset(asset: Asset) {
  const signal = `${asset.type} ${asset.name} ${asset.metadata?.networkRole || ''} ${asset.metadata?.networkZone || ''}`.toLowerCase();
  return displayLayer(asset) === 'network' || /network|firewall|switch|router|vpn|wan|internet|mpls/.test(signal) || Boolean(asset.metadata?.vlanId || asset.metadata?.subnet);
}

function isStoragePerspectiveAsset(asset: Asset) {
  return displayLayer(asset) === 'storage' || asset.type === 'Virtual machine' || asset.type === 'Virtualization cluster';
}

function perspectiveFromQuery(value: string | null): TopologyView {
  const match = Object.keys(perspectiveLabels).find(item => item.toLowerCase() === String(value || '').toLowerCase());
  return (match as TopologyView) || (value === 'business' ? 'BUSINESS' : value === 'virtualization' ? 'VIRTUALIZATION' : 'TECHNICAL');
}

const nodeTypes = { ci: CiNode };
const allRelationshipTypes = Object.keys(relationshipTypes) as RelationshipType[];

function relationshipType(value: string): RelationshipType {
  return value in relationshipTypes ? value as RelationshipType : 'related_to';
}

function productMark(asset: Asset) {
  const signal = `${asset.metadata?.vendor || ''} ${asset.metadata?.model || ''} ${asset.name}`.toLowerCase();
  if (/microsoft|windows/.test(signal)) return 'MS';
  if (/fortinet|fortigate/.test(signal)) return 'F';
  if (/vmware|vsphere/.test(signal)) return 'VM';
  if (/veeam/.test(signal)) return 'V';
  if (/postgres|sql/.test(signal)) return 'SQL';
  return '';
}

function assetKind(asset: Asset) {
  const type = asset.type.toLowerCase();
  if (type === 'business system') return 'business';
  if (type === 'virtualization cluster') return 'virtual-cluster';
  if (type === 'hypervisor host') return 'hypervisor';
  if (type === 'virtual machine') return 'virtual-machine';
  if (type === 'datastore' || type === 'storage array') return 'virtual-storage';
  if (type === 'virtual network' || type === 'virtualization manager') return 'virtual-platform';
  if (type.includes('network') || type.includes('switch') || type.includes('firewall')) return 'network';
  if (type.includes('server')) return 'server';
  if (type.includes('workstation') || type.includes('device')) return 'compute';
  if (type.includes('software') || type.includes('service')) return 'software';
  if (type.includes('licence') || type.includes('license')) return 'licence';
  if (type.includes('credential') || type.includes('person')) return 'person';
  return 'other';
}

function AssetIcon({ asset }: { asset: Asset }) {
  const kind = assetKind(asset);
  if (kind === 'business') return <BusinessCenterOutlined fontSize="small" />;
  if (kind === 'network') return <RouterOutlined fontSize="small" />;
  if (kind === 'server') return <StorageOutlined fontSize="small" />;
  if (kind === 'compute') return <ComputerOutlined fontSize="small" />;
  if (kind === 'software') return <AppsOutlined fontSize="small" />;
  if (kind === 'licence') return <KeyOutlined fontSize="small" />;
  if (kind === 'person') return <PersonOutlined fontSize="small" />;
  return <AccountTreeOutlined fontSize="small" />;
}

function CiNode({ data, selected }: NodeProps<Node<CiNodeData>>) {
  const asset = data.asset;
  const mark = productMark(asset);
  const owner = asset.metadata?.businessOwner || asset.metadata?.technicalOwner || asset.metadata?.serviceOwner || 'No owner recorded';
  const classes = ['ci-react-node', `kind-${assetKind(asset)}`, `impact-${data.impactRole || 'neutral'}`, selected ? 'selected' : '', data.dimmed ? 'dimmed' : ''].filter(Boolean).join(' ');
  return (
    <Tooltip arrow placement="top" title={<Box sx={{ p: 0.5 }}><Typography sx={{
      fontWeight: 800
    }}>{asset.name}</Typography><Typography variant="caption" sx={{
      display: "block"
    }}>{asset.type} · {asset.metadata?.operationalStatus || 'unknown'}</Typography><Typography variant="caption" sx={{
      display: "block"
    }}>Owner: {owner}</Typography>{asset.metadata?.site && <Typography variant="caption" sx={{
      display: "block"
    }}>Site: {asset.metadata.site}</Typography>}</Box>}>
      <Box className={classes}>
        <Handle type="target" position={Position.Left} isConnectable={data.connectable} />
        <Stack className="ci-node-heading" direction="row" spacing={1} sx={{
          alignItems: "center"
        }}>
          <Box className="ci-node-icon"><AssetIcon asset={asset} /></Box>
          <Box className="ci-node-label"><Typography variant="caption" sx={{
            color: "text.secondary"
          }}>{asset.type}</Typography><Typography noWrap sx={{
            fontWeight: 800
          }}>{asset.name}</Typography></Box>
          {mark && <span className="ci-vendor-mark">{mark}</span>}
        </Stack>
        <Stack direction="row" spacing={0.5} sx={{ mt: 1 }}><Chip size="small" label={asset.metadata?.operationalStatus || 'unknown'} /><Chip size="small" label={asset.metadata?.criticality || 'medium'} /></Stack>
        {Boolean(data.sharedBusinessAppCount) && <Chip className="ci-shared-app-chip" size="small" color="warning" variant="outlined" label={`Shared · ${data.sharedBusinessAppCount} other app${data.sharedBusinessAppCount === 1 ? '' : 's'}`} />}
        {asset.type === 'Virtualization cluster' && <Typography variant="caption" sx={{
          color: "text.secondary"
        }}>HA {asset.metadata?.haEnabled === 'yes' ? 'enabled' : 'disabled'} · {asset.metadata?.capacityStatus || 'unknown'} capacity</Typography>}
        {asset.type === 'Virtual machine' && <Typography variant="caption" sx={{
          color: "text.secondary"
        }}>{asset.metadata?.powerState || 'unknown'} · {asset.metadata?.protectionStatus || 'unknown'}</Typography>}
        {(displayLayer(asset) === 'network' || asset.metadata?.networkZone || asset.metadata?.vlanId || asset.metadata?.subnet) && <Typography variant="caption" sx={{
          color: "text.secondary"
        }}>{asset.metadata?.networkZone || 'Unzoned'}{asset.metadata?.vlanId ? ` · VLAN ${asset.metadata.vlanId}` : ''}{asset.metadata?.ipAddress ? ` · ${asset.metadata.ipAddress}` : ''}</Typography>}
        <Handle type="source" position={Position.Right} isConnectable={data.connectable} />
      </Box>
    </Tooltip>
  );
}

function impactEndpoints(relationship: Relationship) {
  const config = relationshipTypes[relationshipType(relationship.type)];
  return config.reverseForImpact
    ? { source: relationship.toId, target: relationship.fromId }
    : { source: relationship.fromId, target: relationship.toId };
}

function makeEdges(relationships: Relationship[]): Edge[] {
  return relationships.map(relationship => {
    const type = relationshipType(relationship.type);
    const config = relationshipTypes[type];
    const endpoints = impactEndpoints(relationship);
    return {
      id: relationship.id,
      source: endpoints.source,
      target: endpoints.target,
      label: `${config.impactLabel} · ${relationship.impactPolicy || 'required'}`,
      type: 'smoothstep',
      data: { relationship, relationshipType: type },
      style: { stroke: config.color, strokeWidth: relationship.impactPolicy === 'required' ? 2.4 : 1.8, strokeDasharray: relationship.impactPolicy === 'redundant' ? '3 5' : config.dash, opacity: relationship.impactPolicy === 'informational' ? 0.55 : 1 },
      labelStyle: { fill: '#d9e4f6', fontSize: 10, fontWeight: 700 },
      labelBgStyle: { fill: '#0b1424', fillOpacity: 0.88 },
      markerEnd: config.directed ? { type: MarkerType.ArrowClosed, color: config.color, width: 16, height: 16 } : undefined,
    };
  });
}

function layoutStorageKey(companyId: string, view: TopologyView) {
  return `cmdb.relationship.positions.${companyId}.${view.toLowerCase()}`;
}

function savedPositions(companyId: string, view: TopologyView): SavedPositions {
  try {
    const value = localStorage.getItem(layoutStorageKey(companyId, view)) || (view === 'TECHNICAL' ? localStorage.getItem(`cmdb.relationship.positions.${companyId}`) : null) || '{}';
    return JSON.parse(value) as SavedPositions;
  }
  catch { return {}; }
}

function persistPositions(companyId: string, view: TopologyView, nodes: Node<CiNodeData>[]) {
  const positions = Object.fromEntries(nodes.map(node => [node.id, { x: Math.round(node.position.x), y: Math.round(node.position.y) }]));
  localStorage.setItem(layoutStorageKey(companyId, view), JSON.stringify(positions));
}

function perspectiveDefaultPositions(assets: Asset[], view: TopologyView): SavedPositions {
  const positions: SavedPositions = {};
  if (view === 'STACK') {
    displayLayerOrder.forEach((layer, row) => assets.filter(asset => displayLayer(asset) === layer).sort((a, b) => a.name.localeCompare(b.name)).forEach((asset, index) => {
      positions[asset.id] = { x: 210 + index * 270, y: 35 + row * 180 };
    }));
  } else if (view === 'NETWORK') {
    const rank = (asset: Asset) => {
      const signal = `${asset.metadata?.networkRole || ''} ${asset.metadata?.networkZone || ''} ${asset.name}`.toLowerCase();
      if (displayLayer(asset) === 'foundation' || /internet|wan|mpls/.test(signal)) return 0;
      if (/firewall|edge|vpn/.test(signal)) return 1;
      if (/core|router/.test(signal)) return 2;
      if (/access|switch|wireless/.test(signal)) return 3;
      if (displayLayer(asset) === 'network') return 4;
      return 5;
    };
    const networkAssets = assets.filter(isNetworkPerspectiveAsset);
    for (let column = 0; column <= 5; column += 1) networkAssets.filter(asset => rank(asset) === column).sort((a, b) => a.name.localeCompare(b.name)).forEach((asset, index) => {
      positions[asset.id] = { x: column * 300, y: 45 + index * 150 };
    });
  } else if (view === 'APPLICATION') {
    const ranks: Partial<Record<DisplayLayer, number>> = { compute: 0, application: 1, business: 2 };
    Object.entries(ranks).forEach(([layer, column]) => assets.filter(asset => displayLayer(asset) === layer).sort((a, b) => a.name.localeCompare(b.name)).forEach((asset, index) => {
      positions[asset.id] = { x: Number(column) * 330, y: 45 + index * 150 };
    }));
  } else if (view === 'STORAGE') {
    const rank = (asset: Asset) => asset.type === 'Storage array' ? 0 : asset.type === 'Datastore' ? 1 : displayLayer(asset) === 'storage' ? 2 : displayLayer(asset) === 'virtualization' ? 3 : 4;
    const storageAssets = assets.filter(isStoragePerspectiveAsset);
    for (let column = 0; column <= 4; column += 1) storageAssets.filter(asset => rank(asset) === column).sort((a, b) => a.name.localeCompare(b.name)).forEach((asset, index) => {
      positions[asset.id] = { x: column * 300, y: 45 + index * 150 };
    });
  }
  return positions;
}

function initialNodes(assets: Asset[], companyId: string, connectable: boolean, view: TopologyView): Node<CiNodeData>[] {
  const stored = savedPositions(companyId, view);
  const defaults = perspectiveDefaultPositions(assets, view);
  return assets.map((asset, index) => ({
    id: asset.id,
    type: 'ci',
    position: stored[asset.id] || defaults[asset.id] || { x: (index % 4) * 270, y: Math.floor(index / 4) * 145 },
    data: { asset, connectable, impactRole: 'neutral' },
  }));
}

async function elkLayout(nodes: Node<CiNodeData>[], edges: Edge[], direction: LayoutDirection) {
  const [{ default: ELK }, { default: workerUrl }] = await Promise.all([
    import('elkjs/lib/elk-api.js'),
    import('elkjs/lib/elk-worker.min.js?url'),
  ]);
  const elk = new ELK({ workerUrl });
  try {
    const graph = await elk.layout({
      id: 'root',
      layoutOptions: {
        'elk.algorithm': 'layered',
        'elk.direction': direction,
        'elk.spacing.nodeNode': '72',
        'elk.layered.spacing.nodeNodeBetweenLayers': '120',
        'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
        'elk.edgeRouting': 'ORTHOGONAL',
      },
      children: nodes.map(node => ({ id: node.id, width: 230, height: 94 })),
      edges: edges.map(edge => ({ id: edge.id, sources: [edge.source], targets: [edge.target] })),
    });
    const positions = new Map((graph.children || []).map(node => [node.id, { x: node.x || 0, y: node.y || 0 }]));
    return nodes.map(node => ({ ...node, position: positions.get(node.id) || node.position }));
  } finally {
    elk.terminateWorker();
  }
}

function gridLayout(nodes: Node<CiNodeData>[]) {
  const columns = Math.max(2, Math.ceil(Math.sqrt(nodes.length)));
  return nodes.map((node, index) => ({ ...node, position: { x: (index % columns) * 280, y: Math.floor(index / columns) * 150 } }));
}

function storedRelationship(connection: Connection, type: RelationshipType, impactPolicy: ImpactPolicy) {
  const config = relationshipTypes[type];
  if (!connection.source || !connection.target) throw new Error('Choose two configuration items');
  return config.reverseForImpact
    ? { fromId: connection.target, toId: connection.source, type, impactPolicy }
    : { fromId: connection.source, toId: connection.target, type, impactPolicy };
}

export function Relationships() {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const routeState = readRelationshipRouteState(searchParams);
  const requestedTopologyView = perspectiveFromQuery(routeState.view);
  const role = getSession()?.user.role;
  const canEdit = !workspace.isRoot && ['platform_admin', 'msp_operator'].includes(role || '');
  const workspaceKey = workspace.isRoot ? '__root__' : workspace.companyId;
  const [assets, setAssets] = useState<Asset[]>([]);
  const [relationships, setRelationships] = useState<Relationship[]>([]);
  const relationshipsRef = useRef<Relationship[]>([]);
  const [loadedWorkspaceKey, setLoadedWorkspaceKey] = useState('');
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<CiNodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [flow, setFlow] = useState<ReactFlowInstance<Node<CiNodeData>, Edge> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [layoutBusy, setLayoutBusy] = useState(false);
  const [layoutDirection, setLayoutDirection] = useState<LayoutDirection>('RIGHT');
  const [topologyView, setTopologyView] = useState<TopologyView>(requestedTopologyView);
  const [focusedBusinessSystemId, setFocusedBusinessSystemId] = useState(routeState.businessAppId);
  const [showSharedImpact, setShowSharedImpact] = useState(routeState.showSharedImpact);
  const [expandedClusters, setExpandedClusters] = useState<Set<string>>(new Set());
  const [locked, setLocked] = useState(false);
  const [search, setSearch] = useState('');
  const [visibleTypes, setVisibleTypes] = useState<Set<RelationshipType>>(() => new Set(allRelationshipTypes));
  const [selectedAssetId, setSelectedAssetId] = useState(routeState.assetId);
  const [selectedRelationshipId, setSelectedRelationshipId] = useState('');
  const [pendingConnection, setPendingConnection] = useState<Connection | null>(null);
  const [pendingType, setPendingType] = useState<RelationshipType>('connected_to');
  const [pendingImpactPolicy, setPendingImpactPolicy] = useState<ImpactPolicy>('required');
  const [savingRelationship, setSavingRelationship] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const syncRelationshipQuery = useCallback((
    view: TopologyView,
    businessAppId: string,
    sharedImpact: boolean,
  ) => {
    setSearchParams(createRelationshipSearchParams(view, businessAppId, sharedImpact), { replace: true });
  }, [setSearchParams]);

  const reloadRelationships = useCallback(async () => {
    const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
    const records = await apiFetch<Relationship[]>(`/api/relationships${suffix}`);
    relationshipsRef.current = records;
    setRelationships(records); setEdges(makeEdges(records));
  }, [workspace.companyId, workspace.isRoot, setEdges]);

  // Remote topology data changes with the workspace, not with the selected
  // perspective or layout direction.
  useEffect(() => {
    let active = true;
    const load = async () => {
      setLoading(true); setError(''); setSelectedRelationshipId('');
      setLoadedWorkspaceKey('');
      try {
        const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
        const [assetData, relationshipData] = await Promise.all([apiFetch<Asset[]>(`/api/assets${suffix}`), apiFetch<Relationship[]>(`/api/relationships${suffix}`)]);
        if (!active) return;
        relationshipsRef.current = relationshipData;
        setAssets(assetData); setRelationships(relationshipData); setEdges(makeEdges(relationshipData));
        setLoadedWorkspaceKey(workspaceKey);
      } catch (value) {
        if (active) {
          setError(value instanceof Error ? value.message : 'The relationship map could not be loaded.');
          setLoading(false);
        }
      }
    };
    void load();
    return () => { active = false; };
  }, [workspace.companyId, workspace.isRoot, workspaceKey, setEdges]);

  // Rebuild the graph from cached API data when its presentation changes.
  useEffect(() => {
    if (loadedWorkspaceKey !== workspaceKey) return;
    let active = true;
    const arrange = async () => {
      setLayoutBusy(true);
      try {
        const nextEdges = makeEdges(relationshipsRef.current);
        let nextNodes = initialNodes(assets, workspace.companyId, canEdit, topologyView);
        if (!Object.keys(savedPositions(workspace.companyId, topologyView)).length && !['APPLICATION', 'NETWORK', 'STORAGE', 'STACK'].includes(topologyView) && nextNodes.length) {
          nextNodes = await elkLayout(nextNodes, nextEdges, layoutDirection);
        }
        if (!active) return;
        setNodes(nextNodes); setError('');
      } catch (value) {
        if (active) setError(value instanceof Error ? value.message : 'The relationship map could not be arranged.');
      } finally {
        if (active) {
          setLoading(false);
          setLayoutBusy(false);
        }
      }
    };
    void arrange();
    return () => { active = false; };
  }, [assets, loadedWorkspaceKey, workspaceKey, workspace.companyId, canEdit, topologyView, layoutDirection, setNodes]);

  const assetById = useMemo(() => new Map(assets.map(asset => [asset.id, asset])), [assets]);
  const selectedAsset = assetById.get(selectedAssetId);
  const selectedRelationship = relationships.find(item => item.id === selectedRelationshipId);
  const downstream = useMemo(() => selectedAssetId ? traverse(selectedAssetId, relationships) : new Set<string>(), [selectedAssetId, relationships]);
  const upstream = useMemo(() => selectedAssetId ? traverse(selectedAssetId, relationships, true) : new Set<string>(), [selectedAssetId, relationships]);
  const relatedIds = useMemo(() => new Set([selectedAssetId, ...upstream, ...downstream].filter(Boolean)), [selectedAssetId, upstream, downstream]);
  const businessSystems = useMemo(() => assets.filter(asset => asset.type === 'Business system').sort((a, b) => a.name.localeCompare(b.name)), [assets]);
  const businessSystemIds = useMemo(() => new Set(businessSystems.map(asset => asset.id)), [businessSystems]);
  const focusedBusinessSystem = useMemo(() => businessSystems.find(asset => asset.id === focusedBusinessSystemId), [businessSystems, focusedBusinessSystemId]);
  const focusedSupportIds = useMemo(() => focusedBusinessSystem
    ? new Set([focusedBusinessSystem.id, ...traverse(focusedBusinessSystem.id, relationships, true)])
    : new Set<string>(), [focusedBusinessSystem, relationships]);
  const sharedBusinessSystemIds = useMemo(() => {
    const shared = new Set<string>();
    if (!focusedBusinessSystem) return shared;
    focusedSupportIds.forEach(assetId => traverse(assetId, relationships).forEach(affectedId => {
      if (affectedId !== focusedBusinessSystem.id && businessSystemIds.has(affectedId)) shared.add(affectedId);
    }));
    return shared;
  }, [focusedBusinessSystem, focusedSupportIds, relationships, businessSystemIds]);
  const businessScopeIds = useMemo(() => {
    const scoped = new Set(focusedSupportIds);
    if (showSharedImpact) sharedBusinessSystemIds.forEach(systemId => {
      scoped.add(systemId);
      traverse(systemId, relationships, true).forEach(id => scoped.add(id));
    });
    return scoped;
  }, [focusedSupportIds, showSharedImpact, sharedBusinessSystemIds, relationships]);
  const sharedBusinessAppCounts = useMemo(() => {
    const counts = new Map<string, number>();
    if (!focusedBusinessSystem) return counts;
    focusedSupportIds.forEach(assetId => {
      if (businessSystemIds.has(assetId)) return;
      const otherApps = new Set([...traverse(assetId, relationships)].filter(id => businessSystemIds.has(id) && id !== focusedBusinessSystem.id));
      if (otherApps.size) counts.set(assetId, otherApps.size);
    });
    return counts;
  }, [focusedBusinessSystem, focusedSupportIds, relationships, businessSystemIds]);
  const scopeLayerCounts = useMemo(() => {
    const counts = new Map<DisplayLayer, number>();
    if (!focusedBusinessSystem) return counts;
    assets.forEach(asset => {
      if (!businessScopeIds.has(asset.id)) return;
      const layer = displayLayer(asset);
      counts.set(layer, (counts.get(layer) || 0) + 1);
    });
    return counts;
  }, [assets, businessScopeIds, focusedBusinessSystem]);
  const businessVisibleIds = useMemo(() => {
    const visible = new Set(businessSystemIds);
    assets.forEach(asset => {
      if (businessSystemIds.has(asset.id)) return;
      const affectedSystems = [...traverse(asset.id, relationships)].filter(id => businessSystemIds.has(id));
      if (affectedSystems.length >= 2) visible.add(asset.id);
    });
    return visible;
  }, [assets, relationships, businessSystemIds]);

  const clusterMemberships = useMemo(() => {
    const result = new Map<string, string>();
    relationships.filter(item => item.type === 'member_of').forEach(item => result.set(item.fromId, item.toId));
    return result;
  }, [relationships]);
  const clusterCounts = useMemo(() => {
    const result = new Map<string, { hosts: number; vms: number }>();
    clusterMemberships.forEach((clusterId, memberId) => {
      const value = result.get(clusterId) || { hosts: 0, vms: 0 };
      const member = assetById.get(memberId);
      if (member?.type === 'Hypervisor host') value.hosts += 1;
      if (member?.type === 'Virtual machine') value.vms += 1;
      result.set(clusterId, value);
    });
    return result;
  }, [clusterMemberships, assetById]);
  const virtualizationVisibleIds = useMemo(() => {
    const visible = new Set(assets.filter(isVirtualizationCi).map(asset => asset.id));
    return visible;
  }, [assets]);
  const perspectiveVisibleIds = useMemo(() => {
    let visible: Set<string>;
    if (topologyView === 'BUSINESS') visible = new Set(businessVisibleIds);
    else if (topologyView === 'VIRTUALIZATION') visible = new Set(virtualizationVisibleIds);
    else if (topologyView === 'TECHNICAL' || topologyView === 'STACK') visible = new Set(assets.map(asset => asset.id));
    else {
      visible = new Set<string>();
      assets.forEach(asset => {
        const layer = displayLayer(asset);
        if (topologyView === 'APPLICATION' && ['business', 'application', 'compute'].includes(layer)) visible.add(asset.id);
        if (topologyView === 'STORAGE' && isStoragePerspectiveAsset(asset)) visible.add(asset.id);
        if (topologyView === 'NETWORK' && isNetworkPerspectiveAsset(asset)) visible.add(asset.id);
      });
    }
    if (!focusedBusinessSystem && selectedAssetId) relatedIds.forEach(id => visible.add(id));
    if (focusedBusinessSystem) visible = new Set([...visible].filter(id => businessScopeIds.has(id)));
    return visible;
  }, [topologyView, assets, businessVisibleIds, virtualizationVisibleIds, selectedAssetId, relatedIds, focusedBusinessSystem, businessScopeIds]);
  const networkZones = useMemo(() => [...new Set(assets.filter(asset => perspectiveVisibleIds.has(asset.id) && topologyView === 'NETWORK').map(asset => asset.metadata?.networkZone).filter(Boolean))].sort(), [assets, perspectiveVisibleIds, topologyView]);
  useEffect(() => {
    setTopologyView(requestedTopologyView);
    setFocusedBusinessSystemId(routeState.businessAppId);
    setShowSharedImpact(routeState.showSharedImpact);
    setSelectedRelationshipId('');
  }, [requestedTopologyView, routeState.businessAppId, routeState.showSharedImpact]);
  useEffect(() => {
    if (!loading && focusedBusinessSystemId && !focusedBusinessSystem) {
      setFocusedBusinessSystemId(''); setShowSharedImpact(false);
      syncRelationshipQuery(topologyView, '', false);
    }
  }, [loading, focusedBusinessSystemId, focusedBusinessSystem, topologyView, syncRelationshipQuery]);
  const toggleCluster = (clusterId: string) => setExpandedClusters(current => {
    const next = new Set(current); if (next.has(clusterId)) next.delete(clusterId); else next.add(clusterId); return next;
  });
  const changePerspective = (view: TopologyView) => {
    const stored = savedPositions(workspace.companyId, view);
    const layoutAssets = focusedBusinessSystem ? assets.filter(asset => businessScopeIds.has(asset.id)) : assets;
    const defaults = perspectiveDefaultPositions(layoutAssets, view);
    setNodes(current => current.map(node => ({ ...node, position: stored[node.id] || defaults[node.id] || node.position })));
    setTopologyView(view); setSelectedAssetId(''); setSelectedRelationshipId('');
    syncRelationshipQuery(view, focusedBusinessSystemId, showSharedImpact);
    window.setTimeout(() => void flow?.fitView({ padding: view === 'STACK' ? 0.12 : 0.22, duration: 350 }), 30);
  };
  const changeBusinessApplication = (businessAppId: string) => {
    setFocusedBusinessSystemId(businessAppId); setShowSharedImpact(false); setSelectedAssetId(''); setSelectedRelationshipId('');
    syncRelationshipQuery(topologyView, businessAppId, false);
    window.setTimeout(() => void flow?.fitView({ padding: topologyView === 'STACK' ? 0.12 : 0.22, duration: 350 }), 30);
  };
  const changeSharedImpact = (enabled: boolean) => {
    setShowSharedImpact(enabled); setSelectedAssetId(''); setSelectedRelationshipId('');
    syncRelationshipQuery(topologyView, focusedBusinessSystemId, enabled);
    window.setTimeout(() => void flow?.fitView({ padding: topologyView === 'STACK' ? 0.12 : 0.22, duration: 350 }), 30);
  };
  const inspectNode = (node: Node<CiNodeData>) => {
    if (node.data.asset.type === 'Business system' && node.id !== focusedBusinessSystemId) {
      setFocusedBusinessSystemId(node.id); setShowSharedImpact(false);
      syncRelationshipQuery(topologyView, node.id, false);
      window.setTimeout(() => void flow?.fitView({ padding: topologyView === 'STACK' ? 0.12 : 0.22, duration: 350 }), 30);
    }
    setSelectedAssetId(node.id); setSelectedRelationshipId('');
  };

  const shownNodes = useMemo(() => {
    const query = search.trim().toLowerCase();
    return nodes.map(node => {
      const asset = node.data.asset;
      const matchesSearch = !query || [asset.name, asset.type, asset.metadata?.vendor, asset.metadata?.site, asset.metadata?.technicalOwner, asset.metadata?.networkZone, asset.metadata?.vlanId, asset.metadata?.subnet, asset.metadata?.ipAddress].some(value => String(value || '').toLowerCase().includes(query));
      const impactRole: ImpactRole = node.id === selectedAssetId ? 'selected' : upstream.has(node.id) ? 'upstream' : downstream.has(node.id) ? 'downstream' : 'neutral';
      const clusterId = clusterMemberships.get(node.id);
      const collapsedByCluster = topologyView === 'VIRTUALIZATION' && Boolean(clusterId) && !expandedClusters.has(clusterId!) && node.id !== selectedAssetId;
      return {
        ...node,
        hidden: !perspectiveVisibleIds.has(node.id) || collapsedByCluster,
        data: { ...node.data, connectable: canEdit, impactRole, sharedBusinessAppCount: sharedBusinessAppCounts.get(node.id), dimmed: !connecting && (!matchesSearch || (Boolean(selectedAssetId) && !relatedIds.has(node.id))) },
      };
    });
  }, [nodes, search, selectedAssetId, upstream, downstream, relatedIds, canEdit, connecting, topologyView, perspectiveVisibleIds, clusterMemberships, expandedClusters, sharedBusinessAppCounts]);

  const shownEdges = useMemo(() => edges.filter(edge => visibleTypes.has(relationshipType(String(edge.data?.relationshipType || 'related_to'))) && perspectiveVisibleIds.has(edge.source) && perspectiveVisibleIds.has(edge.target) && (topologyView !== 'VIRTUALIZATION' || ((!clusterMemberships.get(edge.source) || expandedClusters.has(clusterMemberships.get(edge.source)!)) && (!clusterMemberships.get(edge.target) || expandedClusters.has(clusterMemberships.get(edge.target)!))))).map(edge => {
    const inImpact = !selectedAssetId || (relatedIds.has(edge.source) && relatedIds.has(edge.target));
    const selected = edge.id === selectedRelationshipId;
    return { ...edge, label: topologyView === 'STACK' ? undefined : edge.label, animated: selected, style: { ...edge.style, opacity: inImpact ? 0.95 : 0.13, strokeWidth: selected ? 4 : 2 } };
  }), [edges, visibleTypes, selectedAssetId, selectedRelationshipId, relatedIds, topologyView, perspectiveVisibleIds, clusterMemberships, expandedClusters]);

  const applyAutoLayout = useCallback(async () => {
    setLayoutBusy(true);
    try {
      const layoutNodes = nodes.filter(node => perspectiveVisibleIds.has(node.id));
      const layoutEdges = edges.filter(edge => perspectiveVisibleIds.has(edge.source) && perspectiveVisibleIds.has(edge.target));
      if (!layoutNodes.length) return;
      let visibleLayout: Node<CiNodeData>[];
      if (['APPLICATION', 'NETWORK', 'STORAGE', 'STACK'].includes(topologyView)) {
        const layoutAssets = assets.filter(asset => perspectiveVisibleIds.has(asset.id));
        const positions = perspectiveDefaultPositions(layoutAssets, topologyView);
        visibleLayout = layoutNodes.map(node => ({ ...node, position: positions[node.id] || node.position }));
      } else visibleLayout = await elkLayout(layoutNodes, layoutEdges, layoutDirection);
      const positionsById = new Map(visibleLayout.map(node => [node.id, node.position]));
      const layouted = nodes.map(node => positionsById.has(node.id) ? { ...node, position: positionsById.get(node.id)! } : node);
      setNodes(layouted); persistPositions(workspace.companyId, topologyView, layouted);
      window.setTimeout(() => void flow?.fitView({ padding: 0.22, duration: 350 }), 0);
    } finally { setLayoutBusy(false); }
  }, [nodes, edges, layoutDirection, workspace.companyId, flow, setNodes, topologyView, assets, perspectiveVisibleIds]);

  const applyGridLayout = () => {
    const visibleLayout = gridLayout(nodes.filter(node => perspectiveVisibleIds.has(node.id)));
    const positionsById = new Map(visibleLayout.map(node => [node.id, node.position]));
    const layouted = nodes.map(node => positionsById.has(node.id) ? { ...node, position: positionsById.get(node.id)! } : node);
    setNodes(layouted); persistPositions(workspace.companyId, topologyView, layouted);
    window.setTimeout(() => void flow?.fitView({ padding: 0.22, duration: 300 }), 0);
  };

  const resetLayout = async () => {
    localStorage.removeItem(layoutStorageKey(workspace.companyId, topologyView));
    await applyAutoLayout();
  };

  const saveDragPosition = (_event: unknown, moved: Node<CiNodeData>) => {
    setNodes(current => {
      const updated = current.map(node => node.id === moved.id ? { ...node, position: moved.position } : node);
      persistPositions(workspace.companyId, topologyView, updated); return updated;
    });
  };

  const toggleRelationshipType = (type: RelationshipType) => setVisibleTypes(current => {
    const next = new Set(current); if (next.has(type)) next.delete(type); else next.add(type); return next;
  });

  const createRelationship = async () => {
    if (!pendingConnection) return;
    setSavingRelationship(true); setError('');
    try {
      await apiFetch('/api/relationships', { method: 'POST', body: JSON.stringify(storedRelationship(pendingConnection, pendingType, pendingImpactPolicy)) });
      setPendingConnection(null); await reloadRelationships();
    } catch (value) { setError(value instanceof Error ? value.message : 'The relationship could not be created.'); }
    finally { setSavingRelationship(false); }
  };

  const finishConnection = useCallback((event: MouseEvent | TouchEvent, state: FinalConnectionState) => {
    setConnecting(false);
    if (!canEdit || state.isValid) return;

    // React Flow normally requires an exact handle-to-handle drop. Treat the
    // destination CI card as a larger drop target so a visually correct drag
    // is not discarded just because the pointer missed the small handle.
    const targetElement = event.target instanceof Element ? event.target.closest('.react-flow__node') : null;
    const sourceId = state.fromNode?.id;
    const targetId = targetElement?.getAttribute('data-id') || '';
    if (sourceId && targetId && sourceId !== targetId) {
      const startedFromTargetHandle = state.fromHandle?.type === 'target';
      setError('');
      setPendingConnection({
        source: startedFromTargetHandle ? targetId : sourceId,
        target: startedFromTargetHandle ? sourceId : targetId,
        sourceHandle: state.fromHandle?.id || null,
        targetHandle: null,
      });
      return;
    }
    if (sourceId) setError('Drop the connector onto another CI card or connector dot.');
  }, [canEdit]);

  const disconnectRelationship = async (relationship: Relationship) => {
    const from = assetById.get(relationship.fromId)?.name || relationship.fromId;
    const to = assetById.get(relationship.toId)?.name || relationship.toId;
    if (!window.confirm(`Disconnect ${from} ${relationshipTypes[relationshipType(relationship.type)].label} ${to}?`)) return;
    try {
      await apiFetch(`/api/relationships/${relationship.id}`, { method: 'DELETE' });
      setSelectedRelationshipId(''); await reloadRelationships();
    } catch (value) { setError(value instanceof Error ? value.message : 'The relationship could not be disconnected.'); }
  };

  const pendingSource = pendingConnection?.source ? assetById.get(pendingConnection.source) : undefined;
  const pendingTarget = pendingConnection?.target ? assetById.get(pendingConnection.target) : undefined;
  const pendingSentence = pendingSource && pendingTarget
    ? relationshipTypes[pendingType].reverseForImpact
      ? `${pendingTarget.name} ${relationshipTypes[pendingType].label} ${pendingSource.name}`
      : `${pendingSource.name} ${relationshipTypes[pendingType].label} ${pendingTarget.name}`
    : '';

  return (
    <Box>
      <Title title="Relationships" />
      <Typography variant="overline" color="primary">Impact and dependency map</Typography>
      <Typography variant="h3">CI topology</Typography>
      <Typography
        sx={{
          color: "text.secondary",
          mb: 2
        }}>Choose a perspective for the task at hand. Every view uses the same canonical CIs and relationships, so a change made in one layer is immediately reflected everywhere.</Typography>
      {error && <Alert severity="error" onClose={() => setError('')} sx={{ mb: 2 }}>{error}</Alert>}

      <Paper className="relationship-toolbar" variant="outlined">
        <TextField size="small" placeholder="Search CIs" value={search} onChange={event => setSearch(event.target.value)} slotProps={{ input: { startAdornment: <InputAdornment position="start"><SearchOutlined fontSize="small" /></InputAdornment> } }} />
        <FormControl size="small" sx={{ minWidth: 190 }}><InputLabel>Perspective</InputLabel><Select label="Perspective" value={topologyView} onChange={event => changePerspective(event.target.value as TopologyView)}>{(Object.keys(perspectiveLabels) as TopologyView[]).map(view => <MenuItem key={view} value={view}>{perspectiveLabels[view]}</MenuItem>)}</Select></FormControl>
        <Autocomplete
          className="business-application-filter"
          size="small"
          options={businessSystems}
          value={focusedBusinessSystem || null}
          onChange={(_event, option) => changeBusinessApplication(option?.id || '')}
          getOptionLabel={option => option.name}
          isOptionEqualToValue={(option, value) => option.id === value.id}
          noOptionsText="No business applications found"
          renderInput={params => <TextField {...params} label="Business application" placeholder="All business applications" />}
        />
        <FormControl size="small" sx={{ minWidth: 145 }}><InputLabel>Auto-layout</InputLabel><Select label="Auto-layout" value={layoutDirection} onChange={event => setLayoutDirection(event.target.value as LayoutDirection)}><MenuItem value="RIGHT">Left to right</MenuItem><MenuItem value="DOWN">Top to bottom</MenuItem></Select></FormControl>
        <Button variant="outlined" startIcon={layoutBusy ? <CircularProgress size={16} /> : <AutoAwesomeOutlined />} disabled={layoutBusy || !nodes.length} onClick={() => void applyAutoLayout()}>Auto-arrange</Button>
        <Button variant="outlined" startIcon={<GridViewOutlined />} disabled={!nodes.length} onClick={applyGridLayout}>Grid</Button>
        <Button variant="outlined" startIcon={<FitScreenOutlined />} disabled={!nodes.length} onClick={() => void flow?.fitView({ padding: 0.22, duration: 300 })}>Fit</Button>
        <Button variant="outlined" startIcon={locked ? <LockOutlined /> : <LockOpenOutlined />} onClick={() => setLocked(value => !value)}>{locked ? 'Unlock' : 'Lock'}</Button>
        <Button color="inherit" onClick={() => void resetLayout()}>Reset layout</Button>
      </Paper>

      {focusedBusinessSystem && <Paper className="business-application-scope" variant="outlined">
        <Stack
          direction={{ xs: 'column', md: 'row' }}
          spacing={2}
          sx={{
            justifyContent: "space-between",
            alignItems: { xs: 'flex-start', md: 'center' }
          }}>
          <Stack direction="row" spacing={1.25} sx={{
            alignItems: "center"
          }}>
            <Box className="business-application-scope-icon"><AppsOutlined /></Box>
            <Box><Typography variant="overline" color="primary">Business application scope</Typography><Typography variant="h6">{focusedBusinessSystem.name}</Typography><Typography variant="caption" sx={{
              color: "text.secondary"
            }}>{focusedBusinessSystem.metadata?.businessOwner || focusedBusinessSystem.metadata?.serviceOwner || 'No business owner recorded'} · {businessScopeIds.size} related CIs</Typography></Box>
          </Stack>
          <FormControlLabel
            control={<Switch checked={showSharedImpact} disabled={!sharedBusinessSystemIds.size} onChange={event => changeSharedImpact(event.target.checked)} />}
            label={sharedBusinessSystemIds.size ? `Show shared impact (${sharedBusinessSystemIds.size})` : 'No shared application impact'}
          />
        </Stack>
        <Stack
          direction="row"
          spacing={0.75}
          useFlexGap
          sx={{
            flexWrap: "wrap",
            mt: 1.5
          }}>
          {displayLayerOrder.filter(layer => Boolean(scopeLayerCounts.get(layer))).map(layer => <Chip key={layer} size="small" variant="outlined" label={`${displayLayerLabels[layer]} · ${scopeLayerCounts.get(layer)}`} />)}
        </Stack>
      </Paper>}
      {focusedBusinessSystem && focusedSupportIds.size <= 1 && <Alert severity="warning" sx={{ mb: 1.5 }}>{focusedBusinessSystem.name} has no traversable supporting relationships. Add its application and infrastructure dependencies to complete this map.</Alert>}
      {focusedBusinessSystem && focusedSupportIds.size > 1 && perspectiveVisibleIds.size === 0 && <Alert severity="warning" sx={{ mb: 1.5 }}>No {perspectiveLabels[topologyView].toLowerCase()} CIs are related to {focusedBusinessSystem.name}. Check its relationships or choose another perspective.</Alert>}

      {topologyView === 'BUSINESS' && <Alert severity="info" sx={{ mb: 1.5 }}>{focusedBusinessSystem ? `Showing the business-impact path for ${focusedBusinessSystem.name}${showSharedImpact ? ' and applications sharing its supporting CIs' : ''}.` : `Showing ${businessSystemIds.size} business system(s) and shared supporting CIs. Select a business system to expand its complete technical dependency path.`}</Alert>}
      {topologyView === 'APPLICATION' && <Alert severity="info" sx={{ mb: 1.5 }}>Applications, databases and business systems are arranged from compute foundations to business consumers. Select an item to reveal its complete dependency path.</Alert>}
      {topologyView === 'VIRTUALIZATION' && <Alert severity="info" sx={{ mb: 1.5 }}>Clusters are collapsed by default. Double-click a cluster, or use its inspector, to show hosts and virtual machines. HA capacity is evaluated during change impact analysis.</Alert>}
      {topologyView === 'NETWORK' && <Alert severity="info" sx={{ mb: 1.5 }}>Network path view · {networkZones.length ? `${networkZones.length} zone(s): ${networkZones.join(', ')}` : 'add site, zone, VLAN and subnet metadata to improve grouping'}. Select a network CI to overlay its complete business-impact path.</Alert>}
      {topologyView === 'STORAGE' && <Alert severity="info" sx={{ mb: 1.5 }}>Storage arrays, datastores, backup components and protected workloads are shown from provider to consumer.</Alert>}
      {topologyView === 'STACK' && <Alert severity="info" sx={{ mb: 1.5 }}>Full-stack lanes run from physical and cloud foundations through network, storage, virtualization, compute and applications to business systems.</Alert>}

      <Stack className="relationship-filter-row" direction="row" spacing={1} useFlexGap sx={{
        flexWrap: "wrap"
      }}>
        <Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            alignSelf: 'center',
            mr: 0.5
          }}>Relationship lines</Typography>
        {allRelationshipTypes.map(type => <Chip key={type} size="small" clickable variant={visibleTypes.has(type) ? 'filled' : 'outlined'} label={relationshipTypes[type].impactLabel} onClick={() => toggleRelationshipType(type)} sx={{ borderColor: relationshipTypes[type].color, color: visibleTypes.has(type) ? '#07131d' : relationshipTypes[type].color, bgcolor: visibleTypes.has(type) ? relationshipTypes[type].color : 'transparent' }} />)}
      </Stack>

      {loading ? <Box className="relationship-loading"><CircularProgress /><Typography>Arranging configuration items…</Typography></Box> : !assets.length ? <Alert severity="info">No configuration items are visible in this workspace.</Alert> : <Box className={`relationship-canvas view-${topologyView.toLowerCase()}`}>
        <ReactFlow<Node<CiNodeData>, Edge>
          className={`${connecting ? 'relationship-flow is-connecting' : 'relationship-flow'} perspective-${topologyView.toLowerCase()}`}
          nodes={shownNodes}
          edges={shownEdges}
          nodeTypes={nodeTypes}
          onInit={setFlow}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeDragStop={saveDragPosition}
          onNodeClick={(_event, node) => inspectNode(node)}
          onNodeDoubleClick={(_event, node) => { if (node.data.asset.type === 'Virtualization cluster') toggleCluster(node.id); }}
          onEdgeClick={(_event, edge) => { setSelectedRelationshipId(edge.id); setSelectedAssetId(''); }}
          onPaneClick={() => { setSelectedAssetId(''); setSelectedRelationshipId(''); }}
          onConnectStart={() => { if (canEdit) { setConnecting(true); setError(''); } }}
          onConnect={connection => { if (canEdit) { setError(''); setPendingConnection(connection); } }}
          onConnectEnd={finishConnection}
          isValidConnection={connection => Boolean(canEdit && connection.source && connection.target && connection.source !== connection.target)}
          connectionRadius={42}
          connectOnClick
          nodesDraggable={!locked}
          nodesConnectable={canEdit}
          edgesReconnectable={false}
          deleteKeyCode={null}
          snapToGrid
          snapGrid={[16, 16]}
          selectionOnDrag
          fitView
          minZoom={0.2}
          maxZoom={1.8}
          onlyRenderVisibleElements
        >
          <Background gap={22} color="#2d3c55" />
          <MiniMap nodeColor={node => node.data?.impactRole === 'downstream' ? '#f0a45d' : node.data?.impactRole === 'upstream' ? '#7997ff' : '#4cc7b1'} maskColor="rgba(7, 13, 24, .72)" pannable zoomable />
          <Controls />
          {topologyView === 'STACK' && <Panel position="top-right"><Paper className="stack-layer-legend" elevation={8}><Typography variant="overline" color="primary">Full stack layers</Typography>{displayLayerOrder.map(layer => <Stack key={layer} direction="row" spacing={1} sx={{
            justifyContent: "space-between"
          }}><Typography variant="caption">{displayLayerLabels[layer]}</Typography><Chip size="small" label={focusedBusinessSystem ? (scopeLayerCounts.get(layer) || 0) : assets.filter(asset => displayLayer(asset) === layer).length} /></Stack>)}</Paper></Panel>}
          {!canEdit && <Panel position="bottom-left"><Chip size="small" icon={<LockOutlined />} label="Read-only relationships" /></Panel>}
          {canEdit && <Panel position="bottom-left"><Chip className="relationship-connect-help" size="small" label="Drag or click a connector dot, then choose another CI" /></Panel>}
          {(selectedAsset || selectedRelationship) && <Panel position="top-right"><Paper className="relationship-inspector" elevation={8}>
            <Stack
              direction="row"
              sx={{
                justifyContent: "space-between",
                alignItems: "flex-start"
              }}><Box><Typography variant="overline" color="primary">{selectedAsset ? 'Impact inspection' : 'Relationship'}</Typography><Typography variant="h6">{selectedAsset?.name || relationshipTypes[relationshipType(selectedRelationship!.type)].label}</Typography></Box><IconButton size="small" aria-label="Close inspector" onClick={() => { setSelectedAssetId(''); setSelectedRelationshipId(''); }}><CloseOutlined /></IconButton></Stack>
            {selectedAsset && <><Typography variant="body2" sx={{
              color: "text.secondary"
            }}>{selectedAsset.type} · {selectedAsset.metadata?.criticality || 'medium'} criticality · {selectedAsset.metadata?.operationalStatus || 'unknown'}</Typography><Typography
              variant="caption"
              sx={{
                display: "block",
                mt: 1
              }}>Layer: {displayLayerLabels[displayLayer(selectedAsset)]}</Typography><Typography variant="caption" sx={{
              display: "block"
            }}>Owner: {selectedAsset.metadata?.businessOwner || selectedAsset.metadata?.technicalOwner || selectedAsset.metadata?.serviceOwner || 'No owner recorded'}</Typography>{(displayLayer(selectedAsset) === 'network' || selectedAsset.metadata?.networkZone) && <Typography variant="caption" sx={{
              display: "block"
            }}>Network: {selectedAsset.metadata?.site || 'No site'} · {selectedAsset.metadata?.networkZone || 'Unzoned'}{selectedAsset.metadata?.vlanId ? ` · VLAN ${selectedAsset.metadata.vlanId}` : ''}{selectedAsset.metadata?.subnet ? ` · ${selectedAsset.metadata.subnet}` : ''}</Typography>}{selectedAsset.type === 'Business system' && <Typography variant="caption" sx={{
              display: "block"
            }}>Signoff: {selectedAsset.metadata?.signoffRequired === 'no' ? 'Not required' : selectedAsset.metadata?.signoffDelegate || selectedAsset.metadata?.businessOwner || 'Not assigned'}</Typography>}{selectedAsset.type === 'Virtualization cluster' && <><Typography variant="caption" sx={{
              display: "block"
            }}>{clusterCounts.get(selectedAsset.id)?.hosts || 0} host(s) · {clusterCounts.get(selectedAsset.id)?.vms || 0} VM(s) · {selectedAsset.metadata?.capacityStatus || 'unknown'} failover capacity</Typography><Button fullWidth variant="outlined" sx={{ mt: 1 }} onClick={() => toggleCluster(selectedAsset.id)}>{expandedClusters.has(selectedAsset.id) ? 'Collapse cluster' : 'Expand cluster'}</Button></>}{canEdit && <Button fullWidth variant="contained" sx={{ mt: 2 }} startIcon={<PostAddOutlined />} onClick={() => navigate(`/changes?assetId=${encodeURIComponent(selectedAsset.id)}`)}>Create change package</Button>}<Divider sx={{ my: 2 }} /><Stack direction="row" spacing={1}><Chip size="small" color="info" label={`${upstream.size} upstream`} /><Chip size="small" color="warning" label={`${downstream.size} downstream`} /></Stack>{upstream.size > 0 && <Box sx={{ mt: 2 }}><Typography variant="subtitle2">Upstream dependencies</Typography><Typography variant="body2" sx={{
              color: "text.secondary"
            }}>{[...upstream].map(id => assetById.get(id)?.name || id).join(', ')}</Typography></Box>}{downstream.size > 0 && <Box sx={{ mt: 2 }}><Typography variant="subtitle2">Downstream impact</Typography><Typography variant="body2" sx={{
              color: "text.secondary"
            }}>{[...downstream].map(id => assetById.get(id)?.name || id).join(', ')}</Typography></Box>}<Divider sx={{ my: 2 }} /><Typography variant="subtitle2">Direct relationships</Typography><Stack spacing={1} sx={{ mt: 1 }}>{relationships.filter(item => item.fromId === selectedAsset.id || item.toId === selectedAsset.id).map(item => <Paper variant="outlined" key={item.id} sx={{ p: 1 }}><Typography variant="caption">{assetById.get(item.fromId)?.name || item.fromId} {relationshipTypes[relationshipType(item.type)].label} {assetById.get(item.toId)?.name || item.toId}</Typography><Chip size="small" sx={{ ml: 1 }} label={item.impactPolicy || 'required'} />{canEdit && <Button color="error" size="small" startIcon={<DeleteOutlined />} onClick={() => void disconnectRelationship(item)}>Disconnect</Button>}</Paper>)}</Stack></>}
            {selectedRelationship && <><Typography
              sx={{
                color: "text.secondary",
                mt: 1
              }}>{assetById.get(selectedRelationship.fromId)?.name || selectedRelationship.fromId} {relationshipTypes[relationshipType(selectedRelationship.type)].label} {assetById.get(selectedRelationship.toId)?.name || selectedRelationship.toId}</Typography><Chip size="small" sx={{ mt: 1 }} label={`${selectedRelationship.impactPolicy || 'required'} impact`} /><Typography variant="body2" sx={{ mt: 2 }}>{relationshipTypes[relationshipType(selectedRelationship.type)].description}</Typography>{canEdit && <Button fullWidth color="error" variant="outlined" sx={{ mt: 2 }} startIcon={<DeleteOutlined />} onClick={() => void disconnectRelationship(selectedRelationship)}>Disconnect relationship</Button>}</>}
          </Paper></Panel>}
        </ReactFlow>
      </Box>}

      <Dialog open={Boolean(pendingConnection)} onClose={() => setPendingConnection(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Create relationship</DialogTitle>
        <DialogContent><Typography
          sx={{
            color: "text.secondary",
            mb: 2
          }}>The connection is drawn in outage-impact direction: from the supporting CI to the affected CI.</Typography><Stack spacing={2}><FormControl fullWidth><InputLabel>Relationship type</InputLabel><Select label="Relationship type" value={pendingType} onChange={event => setPendingType(event.target.value as RelationshipType)}>{allRelationshipTypes.map(type => <MenuItem key={type} value={type}>{relationshipTypes[type].label}</MenuItem>)}</Select></FormControl><FormControl fullWidth><InputLabel>Failure impact</InputLabel><Select label="Failure impact" value={pendingImpactPolicy} onChange={event => setPendingImpactPolicy(event.target.value as ImpactPolicy)}><MenuItem value="required">Required — dependent system is unavailable</MenuItem><MenuItem value="degraded">Degraded — reduced function or performance</MenuItem><MenuItem value="redundant">Redundant — alternate component should carry service</MenuItem><MenuItem value="informational">Informational — do not propagate outage impact</MenuItem></Select></FormControl></Stack>{pendingSentence && <Alert severity="info" sx={{ mt: 2 }}>{pendingSentence}</Alert>}</DialogContent>
        <DialogActions><Button onClick={() => setPendingConnection(null)}>Cancel</Button><Button variant="contained" disabled={savingRelationship} onClick={() => void createRelationship()}>{savingRelationship ? 'Saving…' : 'Create relationship'}</Button></DialogActions>
      </Dialog>
    </Box>
  );
}
