import { useEffect, useState } from 'react';
import { Alert, Box, Card, CardContent, Chip, Grid, Stack, Typography } from '@mui/material';
import { Title } from 'react-admin';
import { apiFetch } from './session';
import { useWorkspace } from './workspace';
import type { Asset } from './types';

type Attention = { assetId: string; assetName: string; kind: string; date: string; days: number; owner: string; companyName: string };

function localAttention(assets: Asset[], companyName: string): Attention[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return assets.flatMap(asset => ([['Renewal', asset.metadata?.renewalDate], ['End of life', asset.metadata?.endOfLifeDate]] as const)
    .filter(([, value]) => Boolean(value))
    .map(([kind, value]) => ({
      assetId: asset.id,
      assetName: asset.name,
      kind,
      date: value!,
      days: Math.ceil((new Date(`${value}T00:00:00`).getTime() - today.getTime()) / 86400000),
      owner: asset.metadata?.technicalOwner || asset.metadata?.serviceOwner || 'No owner recorded',
      companyName,
    }))
    .filter(item => item.days <= 90));
}

export function Dashboard() {
  const workspace = useWorkspace();
  const [assets, setAssets] = useState<Asset[]>([]);
  const [attention, setAttention] = useState<Attention[]>([]);

  useEffect(() => {
    const path = workspace.isRoot ? '/api/assets' : `/api/assets?companyId=${encodeURIComponent(workspace.companyId)}`;
    Promise.all([apiFetch<Asset[]>(path), apiFetch<Attention[]>('/api/root-attention').catch(() => [])]).then(([assetData, attentionData]) => {
      setAssets(assetData);
      const visibleAttention = attentionData.filter(item => workspace.isRoot || assetData.some(asset => asset.id === item.assetId));
      setAttention(visibleAttention.length || workspace.isRoot ? visibleAttention : localAttention(assetData, workspace.companyName));
    });
  }, [workspace.companyId, workspace.isRoot]);

  const critical = assets.filter(asset => asset.metadata?.criticality === 'critical').length;
  const offline = assets.filter(asset => asset.metadata?.operationalStatus === 'offline').length;
  return <Box>
    <Title title={workspace.isRoot ? 'MSP overview' : `${workspace.companyName} overview`} />
    <Typography variant="overline" color="primary">{workspace.isRoot ? 'All managed customers' : 'Customer estate'}</Typography>
    <Typography variant="h3" sx={{ mb: 3 }}>{workspace.isRoot ? 'MSP overview' : workspace.companyName}</Typography>
    <Grid container spacing={2}>
      {[['Configuration items', assets.length], ['Critical CIs', critical], ['Offline', offline], ['Attention items', attention.length]].map(([label, value]) =>
        <Grid key={String(label)} size={{ xs: 6, md: 3 }}><Card><CardContent><Typography color="text.secondary">{label}</Typography><Typography variant="h3">{value}</Typography></CardContent></Card></Grid>)}
    </Grid>
    <Card sx={{ mt: 3 }}>
      <CardContent>
        <Typography variant="h5">Lifecycle attention</Typography>
        <Typography color="text.secondary" sx={{ mb: 2 }}>Renewals and vendor end-of-life dates inside the 90-day watch window.</Typography>
        {!attention.length && <Alert severity="success">No renewal or end-of-life items require attention.</Alert>}
        <Stack spacing={1}>{attention.slice(0, 8).map(item => <Box className="attention-row" key={`${item.assetId}-${item.kind}`}>
          <Box><Typography fontWeight={700}>{item.assetName}</Typography><Typography variant="caption" color="text.secondary">{item.companyName} · {item.owner}</Typography></Box>
          <Chip color={item.days < 0 ? 'error' : 'warning'} label={`${item.kind}: ${item.days < 0 ? `${Math.abs(item.days)} days overdue` : `${item.days} days`}`} />
        </Box>)}</Stack>
      </CardContent>
    </Card>
  </Box>;
}
