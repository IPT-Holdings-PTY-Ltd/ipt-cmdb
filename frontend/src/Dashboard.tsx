import ArrowForwardOutlined from '@mui/icons-material/ArrowForwardOutlined';
import BusinessOutlined from '@mui/icons-material/BusinessOutlined';
import HubOutlined from '@mui/icons-material/HubOutlined';
import Inventory2Outlined from '@mui/icons-material/Inventory2Outlined';
import OpenInNewOutlined from '@mui/icons-material/OpenInNewOutlined';
import ScheduleOutlined from '@mui/icons-material/ScheduleOutlined';
import SyncProblemOutlined from '@mui/icons-material/SyncProblemOutlined';
import WarningAmberOutlined from '@mui/icons-material/WarningAmberOutlined';
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Grid, LinearProgress, Paper, Stack,
  Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Tooltip, Typography,
} from '@mui/material';
import { useEffect, useState, type ReactNode } from 'react';
import { Title } from 'react-admin';
import { useNavigate } from 'react-router-dom';
import { apiFetch } from './session';
import { useWorkspace } from './workspace';

type DashboardAttention = { assetId: string; assetName: string; assetType: string; companyId: string; companyName: string; owner: string; reasons: string[]; score: number; severity: 'critical' | 'warning' | 'info' };
type CustomerRisk = { companyId: string; companyName: string; assetCount: number; businessSystemCount: number; unhealthyCount: number; attentionCount: number; overdueCount: number; missingOwnerCount: number; staleCount: number; riskScore: number };
type BusinessSystemHealth = { id: string; name: string; companyId: string; companyName: string; owner: string; serviceOwner: string; operationalStatus: string; criticality: string; rtoHours: string; rpoHours: string; supportCount: number; attentionCount: number };
type LayerCoverage = { id: string; label: string; count: number; attentionCount: number };
type IntegrationHealth = { id: string; name: string; enabled: boolean; status: string; lastRunStatus: string; lastRunAt: string | null };
type UpcomingChange = { id: string; number: string; title: string; companyId: string; companyName: string; plannedStart: string; riskLevel: string; impactCount: number };
type DashboardData = {
  scope: 'msp' | 'customer';
  summary: { customers: number; assets: number; businessSystems: number; unhealthy: number; attention: number; missingOwners: number; unlinked: number; stale: number; sharedDependencies: number; integrationIssues: number };
  attention: DashboardAttention[]; customers: CustomerRisk[]; businessSystems: BusinessSystemHealth[]; layers: LayerCoverage[]; integrations: IntegrationHealth[]; upcomingChanges: UpcomingChange[];
};

const sentence = (value: string) => value.replace(/[_-]+/g, ' ').replace(/^./, letter => letter.toUpperCase());
const dateTime = (value: string | null) => value ? new Date(value).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : 'Never';
const riskColor = (value: string) => value === 'critical' ? 'error' : value === 'high' ? 'warning' : 'default';

function MetricCard({ label, value, detail, color, icon }: { label: string; value: number; detail: string; color: string; icon: ReactNode }) {
  return (
    <Paper className="dashboard-metric" sx={{ '--metric-color': color }}>
      <Stack
        direction="row"
        sx={{
          justifyContent: "space-between",
          alignItems: "flex-start"
        }}><Box><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>{label}</Typography><Typography variant="h3">{value}</Typography></Box><Box className="dashboard-metric-icon">{icon}</Box></Stack>
      <Typography variant="caption" sx={{
        color: "text.secondary"
      }}>{detail}</Typography>
    </Paper>
  );
}

function SectionHeading({ eyebrow, title, copy, action }: { eyebrow: string; title: string; copy?: string; action?: ReactNode }) {
  return (
    <Stack
      direction={{ xs: 'column', sm: 'row' }}
      spacing={1.5}
      sx={{
        justifyContent: "space-between",
        alignItems: { sm: 'flex-start' },
        mb: 2
      }}>
      <Box><Typography variant="overline" color="primary">{eyebrow}</Typography><Typography variant="h5">{title}</Typography>{copy && <Typography variant="body2" sx={{
        color: "text.secondary"
      }}>{copy}</Typography>}</Box>{action}
    </Stack>
  );
}

function AttentionQueue({ items, showCustomer = false }: { items: DashboardAttention[]; showCustomer?: boolean }) {
  const navigate = useNavigate();
  if (!items.length) return <Alert severity="success">No operational, lifecycle or data-quality items require attention.</Alert>;
  return (
    <Stack divider={<Box className="dashboard-divider" />}>
      {items.slice(0, 10).map(item => <Stack
        key={item.assetId}
        className="dashboard-attention-row"
        direction={{ xs: 'column', sm: 'row' }}
        spacing={1.5}
        sx={{
          justifyContent: "space-between",
          alignItems: { sm: 'center' }
        }}>
        <Box sx={{ minWidth: 0 }}><Stack
          direction="row"
          spacing={1}
          useFlexGap
          sx={{
            alignItems: "center",
            flexWrap: "wrap"
          }}><Typography sx={{
          fontWeight: 800
        }}>{item.assetName}</Typography>{showCustomer && <Chip size="small" variant="outlined" label={item.companyName} />}</Stack><Typography variant="caption" sx={{
          color: "text.secondary"
        }}>{item.assetType} · {item.owner}</Typography><Typography variant="body2" color={item.severity === 'critical' ? 'error.light' : 'warning.light'}>{item.reasons.slice(0, 2).join(' · ')}</Typography></Box>
        <Stack direction="row" spacing={1} sx={{
          alignItems: "center"
        }}><Chip size="small" color={item.severity === 'critical' ? 'error' : item.severity === 'warning' ? 'warning' : 'info'} label={`Score ${item.score}`} /><Button size="small" endIcon={<ArrowForwardOutlined />} onClick={() => navigate(`/assets/${item.assetId}/show`)}>Open</Button></Stack>
      </Stack>)}
    </Stack>
  );
}

function UpcomingChanges({ items, showCustomer = false }: { items: UpcomingChange[]; showCustomer?: boolean }) {
  const navigate = useNavigate();
  return (
    <Card><CardContent><SectionHeading eyebrow="Change enablement" title="Upcoming changes" copy="Planned changes with saved CMDB impact snapshots." action={<Button size="small" onClick={() => navigate('/changes')}>Open change control</Button>} />
      {!items.length ? <Alert severity="info">No future change packages have a planned start date.</Alert> : <Stack spacing={1.25}>{items.map(item => <Paper variant="outlined" key={item.id} className="dashboard-change-row"><Stack direction="row" spacing={1} sx={{
        justifyContent: "space-between"
      }}><Box><Typography sx={{
        fontWeight: 800
      }}>{item.title}</Typography><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>{item.number}{showCustomer ? ` · ${item.companyName}` : ''}</Typography></Box><Chip size="small" color={riskColor(item.riskLevel)} label={sentence(item.riskLevel)} /></Stack><Stack
        direction="row"
        spacing={1}
        sx={{
          alignItems: "center",
          mt: 1
        }}><ScheduleOutlined fontSize="small" color="action" /><Typography variant="body2">{dateTime(item.plannedStart)}</Typography><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>· {item.impactCount} impacted CIs</Typography></Stack></Paper>)}</Stack>}
    </CardContent></Card>
  );
}

function MspDashboard({ data }: { data: DashboardData }) {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  return (
    <>
      <Grid container spacing={1.5} className="dashboard-metrics">
        <Grid size={{ xs: 6, lg: 2.4 }}><MetricCard label="Managed customers" value={data.summary.customers} detail="Accessible workspaces" color="#7997ff" icon={<BusinessOutlined />} /></Grid>
        <Grid size={{ xs: 6, lg: 2.4 }}><MetricCard label="Configuration items" value={data.summary.assets} detail={`${data.summary.businessSystems} business systems`} color="#50d5b9" icon={<Inventory2Outlined />} /></Grid>
        <Grid size={{ xs: 6, lg: 2.4 }}><MetricCard label="Offline / degraded" value={data.summary.unhealthy} detail="Operational intervention" color="#ef6c75" icon={<WarningAmberOutlined />} /></Grid>
        <Grid size={{ xs: 6, lg: 2.4 }}><MetricCard label="Attention items" value={data.summary.attention} detail={`${data.summary.missingOwners} missing owners`} color="#f0a45d" icon={<WarningAmberOutlined />} /></Grid>
        <Grid size={{ xs: 12, lg: 2.4 }}><MetricCard label="Integration issues" value={data.summary.integrationIssues} detail={`${data.summary.stale} stale CIs`} color="#a68cf0" icon={<SyncProblemOutlined />} /></Grid>
      </Grid>
      <Grid container spacing={2.5} sx={{ mt: 0.5 }}>
        <Grid size={{ xs: 12, xl: 8 }}><Card><CardContent><SectionHeading eyebrow="Customer operations" title="Customer attention queue" copy="Prioritised by operational state, overdue lifecycle events and CMDB completeness." />
          <TableContainer><Table size="small" className="dashboard-customer-table"><TableHead><TableRow><TableCell>Customer</TableCell><TableCell align="right">CIs</TableCell><TableCell align="right">Business systems</TableCell><TableCell align="right">Unhealthy</TableCell><TableCell align="right">Attention</TableCell><TableCell align="right">Missing owners</TableCell><TableCell align="right">Risk</TableCell><TableCell /></TableRow></TableHead><TableBody>{data.customers.map(customer => <TableRow key={customer.companyId} hover><TableCell><Typography sx={{
            fontWeight: 800
          }}>{customer.companyName}</Typography><Typography variant="caption" sx={{
            color: "text.secondary"
          }}>{customer.overdueCount ? `${customer.overdueCount} overdue · ` : ''}{customer.staleCount} stale</Typography></TableCell><TableCell align="right">{customer.assetCount}</TableCell><TableCell align="right">{customer.businessSystemCount}</TableCell><TableCell align="right"><Chip size="small" color={customer.unhealthyCount ? 'error' : 'success'} label={customer.unhealthyCount} /></TableCell><TableCell align="right">{customer.attentionCount}</TableCell><TableCell align="right">{customer.missingOwnerCount}</TableCell><TableCell align="right"><Chip size="small" color={customer.riskScore >= 20 ? 'error' : customer.riskScore ? 'warning' : 'success'} label={customer.riskScore} /></TableCell><TableCell align="right"><Tooltip title="Open customer workspace"><Button size="small" endIcon={<OpenInNewOutlined />} onClick={() => { workspace.setCompanyId(customer.companyId); navigate('/'); }}>Open</Button></Tooltip></TableCell></TableRow>)}</TableBody></Table></TableContainer>
        </CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 4 }}><Card sx={{ height: '100%' }}><CardContent><SectionHeading eyebrow="Data sources" title="Integration health" copy="MSP-level discovery and reconciliation services." action={<Button size="small" onClick={() => navigate('/admin/integrations')}>Manage</Button>} />
          <Stack spacing={1.25}>{data.integrations.map(item => { const healthy = ['ready', 'connected', 'success', 'healthy'].includes(item.status.toLowerCase()) && !['failed', 'blocked'].includes(item.lastRunStatus); return (
            <Paper variant="outlined" className="dashboard-integration-row" key={item.id}><Stack direction="row" spacing={1} sx={{
              justifyContent: "space-between"
            }}><Box><Typography sx={{
              fontWeight: 800
            }}>{item.name}</Typography><Typography variant="caption" sx={{
              color: "text.secondary"
            }}>Last run: {dateTime(item.lastRunAt)}</Typography></Box><Chip size="small" color={healthy ? 'success' : 'warning'} label={item.status} /></Stack><Typography variant="caption" color={item.lastRunStatus === 'failed' || item.lastRunStatus === 'blocked' ? 'error.light' : 'text.secondary'}>Run status: {sentence(item.lastRunStatus)}</Typography></Paper>
          ); })}{!data.integrations.length && <Alert severity="info">No MSP integrations are registered.</Alert>}</Stack>
        </CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 7 }}><Card><CardContent><SectionHeading eyebrow="Priority work" title="Cross-customer attention" copy="The highest-value items for technician review." /><AttentionQueue items={data.attention} showCustomer /></CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 5 }}><UpcomingChanges items={data.upcomingChanges} showCustomer /></Grid>
      </Grid>
    </>
  );
}

function CustomerDashboard({ data }: { data: DashboardData }) {
  const navigate = useNavigate();
  const maxLayer = Math.max(...data.layers.map(layer => layer.count), 1);
  return (
    <>
      <Grid container spacing={1.5} className="dashboard-metrics">
        <Grid size={{ xs: 6, lg: 2.4 }}><MetricCard label="Business systems" value={data.summary.businessSystems} detail="Business-facing services" color="#f1d372" icon={<BusinessOutlined />} /></Grid>
        <Grid size={{ xs: 6, lg: 2.4 }}><MetricCard label="Configuration items" value={data.summary.assets} detail={`${data.layers.filter(layer => layer.count).length} populated layers`} color="#7997ff" icon={<Inventory2Outlined />} /></Grid>
        <Grid size={{ xs: 6, lg: 2.4 }}><MetricCard label="Offline / degraded" value={data.summary.unhealthy} detail="Operational state" color="#ef6c75" icon={<WarningAmberOutlined />} /></Grid>
        <Grid size={{ xs: 6, lg: 2.4 }}><MetricCard label="Needs attention" value={data.summary.attention} detail="Prioritised CMDB actions" color="#f0a45d" icon={<WarningAmberOutlined />} /></Grid>
        <Grid size={{ xs: 12, lg: 2.4 }}><MetricCard label="Shared dependencies" value={data.summary.sharedDependencies} detail="Supporting multiple systems" color="#a68cf0" icon={<HubOutlined />} /></Grid>
      </Grid>
      <Card sx={{ mt: 2.5 }}><CardContent><SectionHeading eyebrow="Business context" title="Business-system health" copy="The technical estate translated into the services and owners the customer recognises." action={<Button size="small" onClick={() => navigate('/business-systems')}>Manage systems</Button>} />
        {!data.businessSystems.length ? <Alert severity="info">No business systems have been modelled for this customer.</Alert> : <Grid container spacing={1.5}>{data.businessSystems.map(system => <Grid key={system.id} size={{ xs: 12, md: 6, xl: 4 }}><Paper variant="outlined" className="dashboard-system-card"><Stack direction="row" spacing={1} sx={{
          justifyContent: "space-between"
        }}><Box sx={{ minWidth: 0 }}><Typography variant="h6" noWrap>{system.name}</Typography><Typography variant="caption" sx={{
          color: "text.secondary"
        }}>{system.owner} · {system.serviceOwner || 'No service owner'}</Typography></Box><Chip size="small" color={system.operationalStatus === 'healthy' ? 'success' : system.operationalStatus === 'offline' ? 'error' : 'warning'} label={sentence(system.operationalStatus)} /></Stack><Stack
          direction="row"
          spacing={1}
          useFlexGap
          sx={{
            flexWrap: "wrap",
            my: 1.5
          }}><Chip size="small" variant="outlined" label={`${system.supportCount} supporting CIs`} /><Chip size="small" color={system.attentionCount ? 'warning' : 'success'} variant="outlined" label={`${system.attentionCount} attention`} /><Chip size="small" variant="outlined" label={`RTO ${system.rtoHours || '?'}h · RPO ${system.rpoHours || '?'}h`} /></Stack><Button size="small" endIcon={<ArrowForwardOutlined />} onClick={() => navigate(`/relationships?view=stack&businessAppId=${encodeURIComponent(system.id)}&sharedImpact=1`)}>View full stack</Button></Paper></Grid>)}</Grid>}
      </CardContent></Card>
      <Grid container spacing={2.5} sx={{ mt: 0.5 }}>
        <Grid size={{ xs: 12, xl: 7 }}><Card sx={{ height: '100%' }}><CardContent><SectionHeading eyebrow="Priority work" title="Action queue" copy="Operational, lifecycle, ownership and relationship issues combined into one list." /><AttentionQueue items={data.attention} /></CardContent></Card></Grid>
        <Grid size={{ xs: 12, xl: 5 }}><Card sx={{ height: '100%' }}><CardContent><SectionHeading eyebrow="Topology coverage" title="CMDB layers" copy="Coverage and attention items across the full service stack." />
          <Stack spacing={1.6}>{data.layers.map(layer => <Box key={layer.id}><Stack direction="row" spacing={1} sx={{
            justifyContent: "space-between"
          }}><Typography variant="body2" sx={{
            fontWeight: 750
          }}>{layer.label}</Typography><Typography variant="caption" sx={{
            color: "text.secondary"
          }}>{layer.count} CIs · {layer.attentionCount} attention</Typography></Stack><LinearProgress variant="determinate" value={(layer.count / maxLayer) * 100} color={layer.attentionCount ? 'warning' : 'primary'} sx={{ mt: .6, height: 7, borderRadius: 6 }} /></Box>)}</Stack>
          <Stack
            direction="row"
            spacing={1}
            useFlexGap
            sx={{
              flexWrap: "wrap",
              mt: 2.5
            }}><Chip color={data.summary.missingOwners ? 'warning' : 'success'} label={`${data.summary.missingOwners} missing owners`} /><Chip color={data.summary.unlinked ? 'warning' : 'success'} label={`${data.summary.unlinked} unlinked CIs`} /><Chip color={data.summary.stale ? 'warning' : 'success'} label={`${data.summary.stale} stale records`} /></Stack>
          <Button sx={{ mt: 2 }} endIcon={<ArrowForwardOutlined />} onClick={() => navigate('/assets')}>Open asset inventory</Button>
        </CardContent></Card></Grid>
        <Grid size={{ xs: 12 }}><UpcomingChanges items={data.upcomingChanges} /></Grid>
      </Grid>
    </>
  );
}

export function Dashboard() {
  const workspace = useWorkspace();
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    setData(null); setError('');
    const path = workspace.isRoot ? '/api/dashboard' : `/api/dashboard?companyId=${encodeURIComponent(workspace.companyId)}`;
    apiFetch<DashboardData>(path).then(setData).catch(value => setError(value instanceof Error ? value.message : 'Dashboard data could not be loaded.'));
  }, [workspace.companyId, workspace.isRoot]);
  return (
    <Box className="dashboard-page">
      <Title title={workspace.isRoot ? 'MSP overview' : `${workspace.companyName} overview`} />
      <Stack
        direction={{ xs: 'column', md: 'row' }}
        spacing={2}
        sx={{
          justifyContent: "space-between",
          alignItems: { md: 'flex-end' },
          mb: 3
        }}><Box><Typography variant="overline" color="primary">{workspace.isRoot ? 'Managed service operations' : 'Customer service intelligence'}</Typography><Typography variant="h3">{workspace.isRoot ? 'MSP operational overview' : workspace.companyName}</Typography><Typography
        sx={{
          color: "text.secondary",
          mt: .5
        }}>{workspace.isRoot ? 'Customer risk, data-source health and priority work across the managed estate.' : 'Business systems, supporting technology and the actions that protect service continuity.'}</Typography></Box>{data && <Chip variant="outlined" color={data.summary.attention ? 'warning' : 'success'} label={data.summary.attention ? `${data.summary.attention} items need review` : 'Estate healthy'} />}</Stack>
      {error && <Alert severity="error">{error}</Alert>}
      {!data && !error && <Paper className="dashboard-loading"><CircularProgress size={28} /><Typography sx={{
        color: "text.secondary"
      }}>Building operational overview…</Typography></Paper>}
      {data && (workspace.isRoot ? <MspDashboard data={data} /> : <CustomerDashboard data={data} />)}
    </Box>
  );
}
