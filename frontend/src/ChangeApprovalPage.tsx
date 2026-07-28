import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import ReportProblemOutlined from '@mui/icons-material/ReportProblemOutlined';
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Container, CssBaseline,
  Divider, Grid, Stack, TextField, ThemeProvider, Typography, createTheme,
} from '@mui/material';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { BrandLogo, useMspBranding } from './branding';
import { apiFetch } from './session';

type ApprovalPackage = {
  requestId: string;
  status: string;
  expiresAt: string;
  approverName: string;
  scope: Array<{ type: string; id?: string; name?: string }>;
  branding: { name: string; logoText: string; logoDataUrl: string; accent: string; supportEmail: string };
  change: {
    number: string; title: string; companyName: string; status: string; changeType: string;
    priority: string; riskLevel: string; plannedStart: string; plannedEnd: string;
    outageExpected: boolean; reason: string; businessImpact: string; implementationPlan: string;
    validationPlan: string; rollbackPlan: string; requester: string;
    businessSystems: Array<{ id: string; name: string; criticality: string; impactSeverity: string; department: string; userPopulation: string }>;
  };
};

const sentence = (value: string) => value.replaceAll('_', ' ').replace(/\b\w/g, match => match.toUpperCase());
const displayDate = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

export function ChangeApprovalPage() {
  const { brand } = useMspBranding();
  const [searchParams] = useSearchParams();
  const [approval, setApproval] = useState<ApprovalPackage | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [comments, setComments] = useState('');
  const [error, setError] = useState('');
  const [complete, setComplete] = useState<{ decision: string; message: string } | null>(null);
  const token = searchParams.get('token') || '';
  const theme = useMemo(() => createTheme({
    palette: {
      mode: 'dark', primary: { main: brand.accent }, secondary: { main: brand.secondaryAccent },
      background: { default: '#080f1d', paper: '#111b2e' },
    },
    shape: { borderRadius: 12 },
    typography: { fontFamily: 'Inter, Segoe UI, system-ui, sans-serif', h3: { fontWeight: 800 } },
    components: {
      MuiPaper: { styleOverrides: { root: { backgroundImage: 'none', border: '1px solid #243552' } } },
      MuiButton: { styleOverrides: { root: { textTransform: 'none', fontWeight: 750 } } },
    },
  }), [brand.accent, brand.secondaryAccent]);

  useEffect(() => {
    if (!token) { setError('This approval link is incomplete.'); setLoading(false); return; }
    apiFetch<ApprovalPackage>('/api/change-approvals/validate', {
      method: 'POST', body: JSON.stringify({ token }),
    })
      .then(setApproval)
      .catch(value => setError(value instanceof Error ? value.message : 'This approval link is unavailable.'))
      .finally(() => setLoading(false));
  }, [token]);

  const respond = async (decision: 'approved' | 'declined') => {
    setSubmitting(true); setError('');
    try {
      const result = await apiFetch<{ decision: string; message: string }>('/api/change-approvals/respond', {
        method: 'POST', body: JSON.stringify({ token, decision, comments }),
      });
      setComplete(result);
    } catch (value) {
      setError(value instanceof Error ? value.message : 'Your decision could not be recorded.');
    } finally { setSubmitting(false); }
  };

  return <ThemeProvider theme={theme}><CssBaseline /><Box sx={{ minHeight: '100vh', py: { xs: 3, md: 7 }, background: 'radial-gradient(circle at 15% 0%, rgba(80,213,185,.13), transparent 34%)' }}>
    <Container maxWidth="md">
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', mb: 4 }}><BrandLogo size={46} /><Box><Typography sx={{ fontWeight: 850, fontSize: 18 }}>{brand.name}</Typography><Typography variant="caption" color="text.secondary">Secure change approval</Typography></Box></Stack>
      {loading && <Box sx={{ display: 'grid', minHeight: 360, placeItems: 'center' }}><CircularProgress aria-label="Loading approval" /></Box>}
      {!loading && complete && <Card><CardContent sx={{ p: { xs: 3, md: 5 }, textAlign: 'center' }}><CheckCircleOutlined color={complete.decision === 'approved' ? 'success' : 'warning'} sx={{ fontSize: 64 }} /><Typography variant="h4" sx={{ mt: 2 }}>{sentence(complete.decision)}</Typography><Typography color="text.secondary" sx={{ mt: 1 }}>{complete.message}</Typography><Typography variant="body2" color="text.secondary" sx={{ mt: 3 }}>You may close this window. This link cannot be used again.</Typography></CardContent></Card>}
      {!loading && !complete && error && !approval && <Alert severity="error" icon={<ReportProblemOutlined />}>{error}</Alert>}
      {!loading && !complete && approval && <Card><CardContent sx={{ p: { xs: 2.5, md: 4 } }}>
        <Typography variant="overline" color="primary">{approval.change.number} · Approval required</Typography>
        <Typography variant="h3" sx={{ fontSize: { xs: '1.8rem', md: '2.35rem' }, mt: .5 }}>{approval.change.title}</Typography>
        <Typography color="text.secondary" sx={{ mt: 1 }}>{approval.change.companyName} · Requested by {approval.change.requester || 'the change team'}</Typography>
        <Divider sx={{ my: 3 }} />
        <Grid container spacing={2}>{[
          ['Change type', sentence(approval.change.changeType)], ['Risk', sentence(approval.change.riskLevel)],
          ['Priority', sentence(approval.change.priority)], ['Expected outage', approval.change.outageExpected ? 'Yes' : 'No'],
          ['Planned start', displayDate(approval.change.plannedStart)], ['Planned end', displayDate(approval.change.plannedEnd)],
        ].map(([label, value]) => <Grid key={label} size={{ xs: 12, sm: 6 }}><Typography variant="caption" color="text.secondary">{label}</Typography><Typography sx={{ fontWeight: 700 }}>{value}</Typography></Grid>)}</Grid>
        <Divider sx={{ my: 3 }} />
        <Typography variant="h6">Reason and business impact</Typography><Typography sx={{ mt: 1, whiteSpace: 'pre-wrap' }}>{approval.change.reason}</Typography><Typography color="text.secondary" sx={{ mt: 1, whiteSpace: 'pre-wrap' }}>{approval.change.businessImpact || 'No additional business impact was recorded.'}</Typography>
        {!!approval.change.businessSystems.length && <Box sx={{ mt: 3 }}><Typography variant="h6">Business systems in your approval scope</Typography><Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap', mt: 1 }}>{approval.change.businessSystems.map(system => <Chip key={system.id} label={`${system.name} · ${sentence(system.criticality || 'unknown')}`} color={system.impactSeverity === 'outage' ? 'warning' : 'default'} />)}</Stack></Box>}
        <Grid container spacing={2} sx={{ mt: 1 }}>{[
          ['Implementation plan', approval.change.implementationPlan], ['Validation plan', approval.change.validationPlan], ['Rollback plan', approval.change.rollbackPlan],
        ].map(([label, value]) => <Grid key={label} size={{ xs: 12, md: 4 }}><Box sx={{ p: 2, height: '100%', border: '1px solid', borderColor: 'divider', borderRadius: 2 }}><Typography sx={{ fontWeight: 750 }}>{label}</Typography><Typography variant="body2" color="text.secondary" sx={{ mt: 1, whiteSpace: 'pre-wrap' }}>{value}</Typography></Box></Grid>)}</Grid>
        <Alert severity="info" sx={{ mt: 3 }}>You are approving only: {approval.scope.map(item => item.name).filter(Boolean).join(', ') || 'this change'}. The link expires {displayDate(approval.expiresAt)}.</Alert>
        {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
        <TextField fullWidth multiline minRows={3} label="Decision comments" value={comments} onChange={event => setComments(event.target.value)} sx={{ mt: 3 }} helperText="Required when declining; recommended when approving." />
        <Stack direction={{ xs: 'column-reverse', sm: 'row' }} spacing={1.5} sx={{ justifyContent: 'flex-end', mt: 3 }}><Button color="error" variant="outlined" disabled={submitting || comments.trim().length < 4} startIcon={<ReportProblemOutlined />} onClick={() => void respond('declined')}>Decline change</Button><Button variant="contained" disabled={submitting} startIcon={<CheckCircleOutlined />} onClick={() => void respond('approved')}>{submitting ? 'Recording…' : 'Approve change'}</Button></Stack>
      </CardContent></Card>}
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', textAlign: 'center', mt: 3 }}>This secure link is personal, time-limited and single-use.</Typography>
    </Container>
  </Box></ThemeProvider>;
}
