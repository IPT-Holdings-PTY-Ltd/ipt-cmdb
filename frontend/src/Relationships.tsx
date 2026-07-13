import AccountTreeOutlined from '@mui/icons-material/AccountTreeOutlined';
import AppsOutlined from '@mui/icons-material/AppsOutlined';
import AutoAwesomeOutlined from '@mui/icons-material/AutoAwesomeOutlined';
import CloseOutlined from '@mui/icons-material/CloseOutlined';
import ComputerOutlined from '@mui/icons-material/ComputerOutlined';
import DeleteOutline from '@mui/icons-material/DeleteOutline';
import FitScreenOutlined from '@mui/icons-material/FitScreenOutlined';
import GridViewOutlined from '@mui/icons-material/GridViewOutlined';
import KeyOutlined from '@mui/icons-material/KeyOutlined';
import LockOpenOutlined from '@mui/icons-material/LockOpenOutlined';
import LockOutlined from '@mui/icons-material/LockOutlined';
import PersonOutline from '@mui/icons-material/PersonOutline';
import PostAddOutlined from '@mui/icons-material/PostAddOutlined';
import RouterOutlined from '@mui/icons-material/RouterOutlined';
import SearchOutlined from '@mui/icons-material/SearchOutlined';
import StorageOutlined from '@mui/icons-material/StorageOutlined';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle, Divider,
  FormControl, IconButton, InputAdornment, InputLabel, MenuItem, Paper, Select, Stack, TextField, Tooltip, Typography,
} from '@mui/material';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Background, Controls, Handle, MarkerType, MiniMap, Panel, Position, ReactFlow, useEdgesState, useNodesState,
  type Connection, type Edge, type Node, type NodeProps, type ReactFlowInstance,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Title } from 'react-admin';
import { useNavigate } from 'react-router-dom';
import { apiFetch, getSession } from './session';
import type { Asset, Relationship } from './types';
import { useWorkspace } from './workspace';

type RelationshipType = 'connected_to' | 'depends_on' | 'installed_on' | 'licensed_to' | 'used_by' | 'related_to';
type ImpactRole = 'selected' | 'upstream' | 'downstream' | 'neutral';
type LayoutDirection = 'RIGHT' | 'DOWN';
type SavedPositions = Record<string, { x: number; y: number }>;
type CiNodeData = {
  asset: Asset;
  connectable: boolean;
  dimmed?: boolean;
  impactRole?: ImpactRole;
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
  installed_on: { label: 'installed on', impactLabel: 'hosts', description: 'The host supports installed software', color: '#a68cf0', dash: '4 5', directed: true, reverseForImpact: true, traversesImpact: true },
  licensed_to: { label: 'licensed to', impactLabel: 'licensed to', description: 'Licence allocation or entitlement', color: '#efc46b', dash: '2 5', directed: true, reverseForImpact: false, traversesImpact: true },
  used_by: { label: 'used by', impactLabel: 'used by', description: 'Consumer or owner association', color: '#74c89e', directed: true, reverseForImpact: false, traversesImpact: true },
  related_to: { label: 'related to', impactLabel: 'related', description: 'Contextual association without impact propagation', color: '#9aaac5', dash: '2 6', directed: false, reverseForImpact: false, traversesImpact: false },
};

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
  if (kind === 'network') return <RouterOutlined fontSize="small" />;
  if (kind === 'server') return <StorageOutlined fontSize="small" />;
  if (kind === 'compute') return <ComputerOutlined fontSize="small" />;
  if (kind === 'software') return <AppsOutlined fontSize="small" />;
  if (kind === 'licence') return <KeyOutlined fontSize="small" />;
  if (kind === 'person') return <PersonOutline fontSize="small" />;
  return <AccountTreeOutlined fontSize="small" />;
}

function CiNode({ data, selected }: NodeProps<Node<CiNodeData>>) {
  const asset = data.asset;
  const mark = productMark(asset);
  const owner = asset.metadata?.technicalOwner || asset.metadata?.serviceOwner || 'No owner recorded';
  const classes = ['ci-react-node', `kind-${assetKind(asset)}`, `impact-${data.impactRole || 'neutral'}`, selected ? 'selected' : '', data.dimmed ? 'dimmed' : ''].filter(Boolean).join(' ');
  return <Tooltip arrow placement="top" title={<Box sx={{ p: 0.5 }}><Typography fontWeight={800}>{asset.name}</Typography><Typography variant="caption" display="block">{asset.type} · {asset.metadata?.operationalStatus || 'unknown'}</Typography><Typography variant="caption" display="block">Owner: {owner}</Typography>{asset.metadata?.site && <Typography variant="caption" display="block">Site: {asset.metadata.site}</Typography>}</Box>}>
    <Box className={classes}>
      <Handle type="target" position={Position.Left} isConnectable={data.connectable} />
      <Stack className="ci-node-heading" direction="row" spacing={1} alignItems="center">
        <Box className="ci-node-icon"><AssetIcon asset={asset} /></Box>
        <Box className="ci-node-label"><Typography variant="caption" color="text.secondary">{asset.type}</Typography><Typography fontWeight={800} noWrap>{asset.name}</Typography></Box>
        {mark && <span className="ci-vendor-mark">{mark}</span>}
      </Stack>
      <Stack direction="row" spacing={0.5} sx={{ mt: 1 }}><Chip size="small" label={asset.metadata?.operationalStatus || 'unknown'} /><Chip size="small" label={asset.metadata?.criticality || 'medium'} /></Stack>
      <Handle type="source" position={Position.Right} isConnectable={data.connectable} />
    </Box>
  </Tooltip>;
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
      label: config.impactLabel,
      type: 'smoothstep',
      data: { relationship, relationshipType: type },
      style: { stroke: config.color, strokeWidth: 2, strokeDasharray: config.dash },
      labelStyle: { fill: '#d9e4f6', fontSize: 10, fontWeight: 700 },
      labelBgStyle: { fill: '#0b1424', fillOpacity: 0.88 },
      markerEnd: config.directed ? { type: MarkerType.ArrowClosed, color: config.color, width: 16, height: 16 } : undefined,
    };
  });
}

function layoutStorageKey(companyId: string) {
  return `cmdb.relationship.positions.${companyId}`;
}

function savedPositions(companyId: string): SavedPositions {
  try { return JSON.parse(localStorage.getItem(layoutStorageKey(companyId)) || '{}') as SavedPositions; }
  catch { return {}; }
}

function persistPositions(companyId: string, nodes: Node<CiNodeData>[]) {
  const positions = Object.fromEntries(nodes.map(node => [node.id, { x: Math.round(node.position.x), y: Math.round(node.position.y) }]));
  localStorage.setItem(layoutStorageKey(companyId), JSON.stringify(positions));
}

function initialNodes(assets: Asset[], companyId: string, connectable: boolean): Node<CiNodeData>[] {
  const stored = savedPositions(companyId);
  return assets.map((asset, index) => ({
    id: asset.id,
    type: 'ci',
    position: stored[asset.id] || { x: (index % 4) * 270, y: Math.floor(index / 4) * 145 },
    data: { asset, connectable, impactRole: 'neutral' },
  }));
}

async function elkLayout(nodes: Node<CiNodeData>[], edges: Edge[], direction: LayoutDirection) {
  const { default: ELK } = await import('elkjs/lib/elk.bundled.js');
  const elk = new ELK();
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
}

function gridLayout(nodes: Node<CiNodeData>[]) {
  const columns = Math.max(2, Math.ceil(Math.sqrt(nodes.length)));
  return nodes.map((node, index) => ({ ...node, position: { x: (index % columns) * 280, y: Math.floor(index / columns) * 150 } }));
}

function traverse(startId: string, relationships: Relationship[], reverse = false) {
  const adjacency = new Map<string, string[]>();
  relationships.forEach(relationship => {
    const config = relationshipTypes[relationshipType(relationship.type)];
    if (!config.traversesImpact) return;
    const endpoints = impactEndpoints(relationship);
    const from = reverse ? endpoints.target : endpoints.source;
    const to = reverse ? endpoints.source : endpoints.target;
    adjacency.set(from, [...(adjacency.get(from) || []), to]);
  });
  const visited = new Set<string>([startId]);
  const queue = [startId];
  while (queue.length) {
    const current = queue.shift()!;
    (adjacency.get(current) || []).forEach(next => { if (!visited.has(next)) { visited.add(next); queue.push(next); } });
  }
  visited.delete(startId);
  return visited;
}

function storedRelationship(connection: Connection, type: RelationshipType) {
  const config = relationshipTypes[type];
  if (!connection.source || !connection.target) throw new Error('Choose two configuration items');
  return config.reverseForImpact
    ? { fromId: connection.target, toId: connection.source, type }
    : { fromId: connection.source, toId: connection.target, type };
}

export function Relationships() {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const role = getSession()?.user.role;
  const canEdit = !workspace.isRoot && ['platform_admin', 'msp_operator'].includes(role || '');
  const [assets, setAssets] = useState<Asset[]>([]);
  const [relationships, setRelationships] = useState<Relationship[]>([]);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<CiNodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [flow, setFlow] = useState<ReactFlowInstance<Node<CiNodeData>, Edge> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [layoutBusy, setLayoutBusy] = useState(false);
  const [layoutDirection, setLayoutDirection] = useState<LayoutDirection>('RIGHT');
  const [locked, setLocked] = useState(false);
  const [search, setSearch] = useState('');
  const [visibleTypes, setVisibleTypes] = useState<Set<RelationshipType>>(() => new Set(allRelationshipTypes));
  const [selectedAssetId, setSelectedAssetId] = useState('');
  const [selectedRelationshipId, setSelectedRelationshipId] = useState('');
  const [pendingConnection, setPendingConnection] = useState<Connection | null>(null);
  const [pendingType, setPendingType] = useState<RelationshipType>('connected_to');
  const [savingRelationship, setSavingRelationship] = useState(false);

  const reloadRelationships = useCallback(async () => {
    const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
    const records = await apiFetch<Relationship[]>(`/api/relationships${suffix}`);
    setRelationships(records); setEdges(makeEdges(records));
  }, [workspace.companyId, workspace.isRoot, setEdges]);

  useEffect(() => {
    let active = true;
    const load = async () => {
      setLoading(true); setError(''); setSelectedAssetId(''); setSelectedRelationshipId('');
      try {
        const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
        const [assetData, relationshipData] = await Promise.all([apiFetch<Asset[]>(`/api/assets${suffix}`), apiFetch<Relationship[]>(`/api/relationships${suffix}`)]);
        if (!active) return;
        setAssets(assetData); setRelationships(relationshipData);
        const nextEdges = makeEdges(relationshipData);
        let nextNodes = initialNodes(assetData, workspace.companyId, canEdit);
        if (!Object.keys(savedPositions(workspace.companyId)).length && nextNodes.length) nextNodes = await elkLayout(nextNodes, nextEdges, layoutDirection);
        if (!active) return;
        setNodes(nextNodes); setEdges(nextEdges);
      } catch (value) { if (active) setError(value instanceof Error ? value.message : 'The relationship map could not be loaded.'); }
      finally { if (active) setLoading(false); }
    };
    void load();
    return () => { active = false; };
  }, [workspace.companyId, workspace.isRoot, canEdit, setNodes, setEdges]);

  const assetById = useMemo(() => new Map(assets.map(asset => [asset.id, asset])), [assets]);
  const selectedAsset = assetById.get(selectedAssetId);
  const selectedRelationship = relationships.find(item => item.id === selectedRelationshipId);
  const downstream = useMemo(() => selectedAssetId ? traverse(selectedAssetId, relationships) : new Set<string>(), [selectedAssetId, relationships]);
  const upstream = useMemo(() => selectedAssetId ? traverse(selectedAssetId, relationships, true) : new Set<string>(), [selectedAssetId, relationships]);
  const relatedIds = useMemo(() => new Set([selectedAssetId, ...upstream, ...downstream].filter(Boolean)), [selectedAssetId, upstream, downstream]);

  const shownNodes = useMemo(() => {
    const query = search.trim().toLowerCase();
    return nodes.map(node => {
      const asset = node.data.asset;
      const matchesSearch = !query || [asset.name, asset.type, asset.metadata?.vendor, asset.metadata?.site, asset.metadata?.technicalOwner].some(value => String(value || '').toLowerCase().includes(query));
      const impactRole: ImpactRole = node.id === selectedAssetId ? 'selected' : upstream.has(node.id) ? 'upstream' : downstream.has(node.id) ? 'downstream' : 'neutral';
      return { ...node, data: { ...node.data, connectable: canEdit, impactRole, dimmed: !matchesSearch || (Boolean(selectedAssetId) && !relatedIds.has(node.id)) } };
    });
  }, [nodes, search, selectedAssetId, upstream, downstream, relatedIds, canEdit]);

  const shownEdges = useMemo(() => edges.filter(edge => visibleTypes.has(relationshipType(String(edge.data?.relationshipType || 'related_to')))).map(edge => {
    const inImpact = !selectedAssetId || (relatedIds.has(edge.source) && relatedIds.has(edge.target));
    const selected = edge.id === selectedRelationshipId;
    return { ...edge, animated: selected, style: { ...edge.style, opacity: inImpact ? 0.95 : 0.13, strokeWidth: selected ? 4 : 2 } };
  }), [edges, visibleTypes, selectedAssetId, selectedRelationshipId, relatedIds]);

  const applyAutoLayout = useCallback(async () => {
    setLayoutBusy(true);
    try {
      const layouted = await elkLayout(nodes, edges, layoutDirection);
      setNodes(layouted); persistPositions(workspace.companyId, layouted);
      window.setTimeout(() => void flow?.fitView({ padding: 0.22, duration: 350 }), 0);
    } finally { setLayoutBusy(false); }
  }, [nodes, edges, layoutDirection, workspace.companyId, flow, setNodes]);

  const applyGridLayout = () => {
    const layouted = gridLayout(nodes); setNodes(layouted); persistPositions(workspace.companyId, layouted);
    window.setTimeout(() => void flow?.fitView({ padding: 0.22, duration: 300 }), 0);
  };

  const resetLayout = async () => {
    localStorage.removeItem(layoutStorageKey(workspace.companyId));
    await applyAutoLayout();
  };

  const saveDragPosition = (_event: unknown, moved: Node<CiNodeData>) => {
    setNodes(current => {
      const updated = current.map(node => node.id === moved.id ? { ...node, position: moved.position } : node);
      persistPositions(workspace.companyId, updated); return updated;
    });
  };

  const toggleRelationshipType = (type: RelationshipType) => setVisibleTypes(current => {
    const next = new Set(current); if (next.has(type)) next.delete(type); else next.add(type); return next;
  });

  const createRelationship = async () => {
    if (!pendingConnection) return;
    setSavingRelationship(true); setError('');
    try {
      await apiFetch('/api/relationships', { method: 'POST', body: JSON.stringify(storedRelationship(pendingConnection, pendingType)) });
      setPendingConnection(null); await reloadRelationships();
    } catch (value) { setError(value instanceof Error ? value.message : 'The relationship could not be created.'); }
    finally { setSavingRelationship(false); }
  };

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

  return <Box>
    <Title title="Relationships" />
    <Typography variant="overline" color="primary">Impact and dependency map</Typography>
    <Typography variant="h3">CI topology</Typography>
    <Typography color="text.secondary" sx={{ mb: 2 }}>Drag CIs to build a useful view, or auto-arrange them. Select any CI to trace its upstream dependencies and downstream outage impact.</Typography>
    {error && <Alert severity="error" onClose={() => setError('')} sx={{ mb: 2 }}>{error}</Alert>}

    <Paper className="relationship-toolbar" variant="outlined">
      <TextField size="small" placeholder="Search CIs" value={search} onChange={event => setSearch(event.target.value)} InputProps={{ startAdornment: <InputAdornment position="start"><SearchOutlined fontSize="small" /></InputAdornment> }} />
      <FormControl size="small" sx={{ minWidth: 145 }}><InputLabel>Auto-layout</InputLabel><Select label="Auto-layout" value={layoutDirection} onChange={event => setLayoutDirection(event.target.value as LayoutDirection)}><MenuItem value="RIGHT">Left to right</MenuItem><MenuItem value="DOWN">Top to bottom</MenuItem></Select></FormControl>
      <Button variant="outlined" startIcon={layoutBusy ? <CircularProgress size={16} /> : <AutoAwesomeOutlined />} disabled={layoutBusy || !nodes.length} onClick={() => void applyAutoLayout()}>Auto-arrange</Button>
      <Button variant="outlined" startIcon={<GridViewOutlined />} disabled={!nodes.length} onClick={applyGridLayout}>Grid</Button>
      <Button variant="outlined" startIcon={<FitScreenOutlined />} disabled={!nodes.length} onClick={() => void flow?.fitView({ padding: 0.22, duration: 300 })}>Fit</Button>
      <Button variant="outlined" startIcon={locked ? <LockOutlined /> : <LockOpenOutlined />} onClick={() => setLocked(value => !value)}>{locked ? 'Unlock' : 'Lock'}</Button>
      <Button color="inherit" onClick={() => void resetLayout()}>Reset layout</Button>
    </Paper>

    <Stack className="relationship-filter-row" direction="row" spacing={1} useFlexGap flexWrap="wrap">
      <Typography variant="caption" color="text.secondary" sx={{ alignSelf: 'center', mr: 0.5 }}>Relationship lines</Typography>
      {allRelationshipTypes.map(type => <Chip key={type} size="small" clickable variant={visibleTypes.has(type) ? 'filled' : 'outlined'} label={relationshipTypes[type].impactLabel} onClick={() => toggleRelationshipType(type)} sx={{ borderColor: relationshipTypes[type].color, color: visibleTypes.has(type) ? '#07131d' : relationshipTypes[type].color, bgcolor: visibleTypes.has(type) ? relationshipTypes[type].color : 'transparent' }} />)}
    </Stack>

    {loading ? <Box className="relationship-loading"><CircularProgress /><Typography>Arranging configuration items…</Typography></Box> : !assets.length ? <Alert severity="info">No configuration items are visible in this workspace.</Alert> : <Box className="relationship-canvas">
      <ReactFlow<Node<CiNodeData>, Edge>
        nodes={shownNodes}
        edges={shownEdges}
        nodeTypes={nodeTypes}
        onInit={setFlow}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeDragStop={saveDragPosition}
        onNodeClick={(_event, node) => { setSelectedAssetId(node.id); setSelectedRelationshipId(''); }}
        onEdgeClick={(_event, edge) => { setSelectedRelationshipId(edge.id); setSelectedAssetId(''); }}
        onPaneClick={() => { setSelectedAssetId(''); setSelectedRelationshipId(''); }}
        onConnect={connection => { if (canEdit) setPendingConnection(connection); }}
        isValidConnection={connection => Boolean(canEdit && connection.source && connection.target && connection.source !== connection.target)}
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
        {!canEdit && <Panel position="bottom-left"><Chip size="small" icon={<LockOutlined />} label="Read-only relationships" /></Panel>}
        {(selectedAsset || selectedRelationship) && <Panel position="top-right"><Paper className="relationship-inspector" elevation={8}>
          <Stack direction="row" justifyContent="space-between" alignItems="flex-start"><Box><Typography variant="overline" color="primary">{selectedAsset ? 'Impact inspection' : 'Relationship'}</Typography><Typography variant="h6">{selectedAsset?.name || relationshipTypes[relationshipType(selectedRelationship!.type)].label}</Typography></Box><IconButton size="small" aria-label="Close inspector" onClick={() => { setSelectedAssetId(''); setSelectedRelationshipId(''); }}><CloseOutlined /></IconButton></Stack>
          {selectedAsset && <><Typography color="text.secondary" variant="body2">{selectedAsset.type} · {selectedAsset.metadata?.criticality || 'medium'} criticality · {selectedAsset.metadata?.operationalStatus || 'unknown'}</Typography><Typography variant="caption" display="block" sx={{ mt: 1 }}>Owner: {selectedAsset.metadata?.technicalOwner || selectedAsset.metadata?.serviceOwner || 'No owner recorded'}</Typography>{canEdit && <Button fullWidth variant="contained" sx={{ mt: 2 }} startIcon={<PostAddOutlined />} onClick={() => navigate(`/changes?assetId=${encodeURIComponent(selectedAsset.id)}`)}>Create change package</Button>}<Divider sx={{ my: 2 }} /><Stack direction="row" spacing={1}><Chip size="small" color="info" label={`${upstream.size} upstream`} /><Chip size="small" color="warning" label={`${downstream.size} downstream`} /></Stack>{upstream.size > 0 && <Box sx={{ mt: 2 }}><Typography variant="subtitle2">Upstream dependencies</Typography><Typography variant="body2" color="text.secondary">{[...upstream].map(id => assetById.get(id)?.name || id).join(', ')}</Typography></Box>}{downstream.size > 0 && <Box sx={{ mt: 2 }}><Typography variant="subtitle2">Downstream impact</Typography><Typography variant="body2" color="text.secondary">{[...downstream].map(id => assetById.get(id)?.name || id).join(', ')}</Typography></Box>}<Divider sx={{ my: 2 }} /><Typography variant="subtitle2">Direct relationships</Typography><Stack spacing={1} sx={{ mt: 1 }}>{relationships.filter(item => item.fromId === selectedAsset.id || item.toId === selectedAsset.id).map(item => <Paper variant="outlined" key={item.id} sx={{ p: 1 }}><Typography variant="caption">{assetById.get(item.fromId)?.name || item.fromId} {relationshipTypes[relationshipType(item.type)].label} {assetById.get(item.toId)?.name || item.toId}</Typography>{canEdit && <Button color="error" size="small" startIcon={<DeleteOutline />} onClick={() => void disconnectRelationship(item)}>Disconnect</Button>}</Paper>)}</Stack></>}
          {selectedRelationship && <><Typography color="text.secondary" sx={{ mt: 1 }}>{assetById.get(selectedRelationship.fromId)?.name || selectedRelationship.fromId} {relationshipTypes[relationshipType(selectedRelationship.type)].label} {assetById.get(selectedRelationship.toId)?.name || selectedRelationship.toId}</Typography><Typography variant="body2" sx={{ mt: 2 }}>{relationshipTypes[relationshipType(selectedRelationship.type)].description}</Typography>{canEdit && <Button fullWidth color="error" variant="outlined" sx={{ mt: 2 }} startIcon={<DeleteOutline />} onClick={() => void disconnectRelationship(selectedRelationship)}>Disconnect relationship</Button>}</>}
        </Paper></Panel>}
      </ReactFlow>
    </Box>}

    <Dialog open={Boolean(pendingConnection)} onClose={() => setPendingConnection(null)} maxWidth="sm" fullWidth>
      <DialogTitle>Create relationship</DialogTitle>
      <DialogContent><Typography color="text.secondary" sx={{ mb: 2 }}>The connection is drawn in outage-impact direction: from the supporting CI to the affected CI.</Typography><FormControl fullWidth><InputLabel>Relationship type</InputLabel><Select label="Relationship type" value={pendingType} onChange={event => setPendingType(event.target.value as RelationshipType)}>{allRelationshipTypes.map(type => <MenuItem key={type} value={type}>{relationshipTypes[type].label}</MenuItem>)}</Select></FormControl>{pendingSentence && <Alert severity="info" sx={{ mt: 2 }}>{pendingSentence}</Alert>}</DialogContent>
      <DialogActions><Button onClick={() => setPendingConnection(null)}>Cancel</Button><Button variant="contained" disabled={savingRelationship} onClick={() => void createRelationship()}>{savingRelationship ? 'Saving…' : 'Create relationship'}</Button></DialogActions>
    </Dialog>
  </Box>;
}
