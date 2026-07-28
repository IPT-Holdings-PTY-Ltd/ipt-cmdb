import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import DeleteOutlined from '@mui/icons-material/DeleteOutlined';
import FactCheckOutlined from '@mui/icons-material/FactCheckOutlined';
import LinkOutlined from '@mui/icons-material/LinkOutlined';
import RuleOutlined from '@mui/icons-material/RuleOutlined';
import WarningAmberOutlined from '@mui/icons-material/WarningAmberOutlined';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControl, Grid, InputLabel, MenuItem, Paper, Select, Stack, Tab, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, Tabs, TextField, Typography,
} from '@mui/material';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiFetch, getSession } from './session';
import { Title, useRedirect } from './ui';
import type { DataQualityFinding, DataQualitySnapshot, FieldAuthorityRule, ReconciliationCandidate } from './types';
import { useWorkspace } from './workspace';

const words = (value: string) => value.replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase());
const blankAuthority: FieldAuthorityRule = { companyId: '', ciType: '*', fieldName: 'display_name', provider: 'connectwise', priority: 100 };

function ScoreCard({ label, value, copy, color = '#7997ff' }: { label: string; value: string | number; copy: string; color?: string }) {
  return (
    <Paper className="quality-metric" sx={{ '--quality-color': color } as React.CSSProperties}><Typography variant="overline" sx={{
      color: "text.secondary"
    }}>{label}</Typography><Typography variant="h3">{value}</Typography><Typography variant="body2" sx={{
      color: "text.secondary"
    }}>{copy}</Typography></Paper>
  );
}

export function DataQualityPage() {
  const workspace = useWorkspace(); const redirect = useRedirect();
  const canManage = ['platform_admin', 'msp_operator'].includes(getSession()?.user.role || '');
  const [snapshot, setSnapshot] = useState<DataQualitySnapshot | null>(null);
  const [loading, setLoading] = useState(true); const [error, setError] = useState('');
  const [tab, setTab] = useState(0); const [search, setSearch] = useState(''); const [severity, setSeverity] = useState('');
  const [waive, setWaive] = useState<DataQualityFinding | null>(null); const [reason, setReason] = useState(''); const [expiresAt, setExpiresAt] = useState('');
  const [decision, setDecision] = useState<ReconciliationCandidate | null>(null); const [decisionType, setDecisionType] = useState('use_existing'); const [decisionNotes, setDecisionNotes] = useState(''); const [targetAssetId, setTargetAssetId] = useState('');
  const [authority, setAuthority] = useState<FieldAuthorityRule | null>(null);
  const companyNames = useMemo(() => new Map(workspace.companies.map(item => [item.id, item.name])), [workspace.companies]);
  const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
  const load = useCallback(async () => { setLoading(true); setError(''); try { setSnapshot(await apiFetch<DataQualitySnapshot>(`/api/data-quality${suffix}`)); } catch (cause) { setError(cause instanceof Error ? cause.message : 'Data quality could not be evaluated.'); } finally { setLoading(false); } }, [suffix]);
  useEffect(() => { void load(); }, [load]);
  const findings = useMemo(() => (snapshot?.findings || []).filter(item => (!severity || item.severity === severity) && (!search || `${item.assetName} ${item.assetType} ${item.ruleLabel} ${item.evidence}`.toLowerCase().includes(search.toLowerCase()))), [snapshot, search, severity]);
  async function saveException() { if (!waive) return; try { await apiFetch('/api/data-quality/exceptions', { method: 'POST', body: JSON.stringify({ companyId: waive.companyId, ruleKey: waive.ruleKey, entityId: waive.assetId, reason, expiresAt: expiresAt || null }) }); setWaive(null); setReason(''); setExpiresAt(''); await load(); } catch (cause) { setError(cause instanceof Error ? cause.message : 'Exception could not be recorded.'); } }
  async function saveDecision() { if (!decision) return; try { await apiFetch(`/api/reconciliation-candidates/${decision.id}`, { method: 'PATCH', body: JSON.stringify({ decision: decisionType, notes: decisionNotes, targetAssetId: decisionType === 'use_existing' ? (targetAssetId || decision.candidateAssetId) : null }) }); setDecision(null); setDecisionNotes(''); await load(); } catch (cause) { setError(cause instanceof Error ? cause.message : 'Decision could not be recorded.'); } }
  async function saveAuthority() { if (!authority) return; try { await apiFetch('/api/field-authority', { method: 'PUT', body: JSON.stringify(authority) }); setAuthority(null); await load(); } catch (cause) { setError(cause instanceof Error ? cause.message : 'Source authority could not be saved.'); } }
  async function deleteAuthority(rule: FieldAuthorityRule) { const query = new URLSearchParams(rule as unknown as Record<string, string>); try { await apiFetch(`/api/field-authority?${query}`, { method: 'DELETE' }); await load(); } catch (cause) { setError(cause instanceof Error ? cause.message : 'Source authority could not be deleted.'); } }
  return (
    <Box className="governance-page"><Title title="Data quality" />
      <Stack
        direction="row"
        spacing={2}
        sx={{
          alignItems: "flex-start",
          mb: 3
        }}><Box className="governance-heading-icon"><FactCheckOutlined /></Box><Box><Typography variant="overline" color="primary">Governance</Typography><Typography variant="h3">{workspace.isRoot ? 'MSP data quality center' : `${workspace.companyName} data quality`}</Typography><Typography
        sx={{
          color: "text.secondary",
          mt: .75,
          maxWidth: 850
        }}>Turn incomplete, stale or conflicting CMDB records into a prioritized, auditable review queue before integration data is allowed to change canonical CIs.</Typography></Box></Stack>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {loading && !snapshot ? <Paper className="governance-loading"><CircularProgress /><Typography>Evaluating canonical records…</Typography></Paper> : snapshot && <>
        <Grid container spacing={2} sx={{ mb: 2.5 }}><Grid size={{ xs: 12, sm: 6, lg: 3 }}><ScoreCard label="Quality score" value={`${snapshot.summary.score}%`} copy={`${snapshot.summary.assetCount} CIs evaluated`} color={snapshot.summary.score >= 85 ? '#50d5b9' : '#f1d372'} /></Grid><Grid size={{ xs: 12, sm: 6, lg: 3 }}><ScoreCard label="Open findings" value={snapshot.summary.findingCount} copy={`${snapshot.summary.highCount} high priority`} color="#f1d372" /></Grid><Grid size={{ xs: 12, sm: 6, lg: 3 }}><ScoreCard label="Reconciliation" value={snapshot.summary.pendingReconciliationCount} copy="Source records awaiting a decision" color="#be8cff" /></Grid><Grid size={{ xs: 12, sm: 6, lg: 3 }}><ScoreCard label="Exceptions" value={snapshot.summary.exceptionCount} copy="Active, reasoned exceptions" color="#8cb4ff" /></Grid></Grid>
        {workspace.isRoot && <Paper className="quality-customers" sx={{ mb: 2.5 }}><Typography variant="h5" sx={{ p: 2, pb: 1 }}>Customer posture</Typography><TableContainer><Table size="small"><TableHead><TableRow><TableCell>Customer</TableCell><TableCell>Score</TableCell><TableCell>CIs</TableCell><TableCell>Findings</TableCell><TableCell>High priority</TableCell></TableRow></TableHead><TableBody>{snapshot.customers.map(item => <TableRow key={item.companyId}><TableCell>{item.companyName}</TableCell><TableCell><Chip size="small" color={item.score >= 85 ? 'success' : item.score >= 65 ? 'warning' : 'error'} label={`${item.score}%`} /></TableCell><TableCell>{item.assetCount}</TableCell><TableCell>{item.findingCount}</TableCell><TableCell>{item.highCount}</TableCell></TableRow>)}</TableBody></Table></TableContainer></Paper>}
        <Paper className="quality-workbench"><Tabs value={tab} onChange={(_, value) => setTab(value)}><Tab icon={<WarningAmberOutlined />} iconPosition="start" label={`Findings (${snapshot.summary.findingCount})`} /><Tab icon={<LinkOutlined />} iconPosition="start" label={`Reconciliation (${snapshot.candidates.length})`} /><Tab icon={<RuleOutlined />} iconPosition="start" label={`Source authority (${snapshot.fieldAuthority.length})`} /></Tabs>
          {tab === 0 && <><Box className="quality-filter"><TextField size="small" label="Search findings" value={search} onChange={event => setSearch(event.target.value)} /><FormControl size="small"><InputLabel>Severity</InputLabel><Select label="Severity" value={severity} onChange={event => setSeverity(event.target.value)}><MenuItem value="">All severities</MenuItem><MenuItem value="high">High</MenuItem><MenuItem value="medium">Medium</MenuItem><MenuItem value="low">Low</MenuItem></Select></FormControl></Box><TableContainer><Table size="small"><TableHead><TableRow>{workspace.isRoot && <TableCell>Customer</TableCell>}<TableCell>Configuration item</TableCell><TableCell>Finding</TableCell><TableCell>Evidence and action</TableCell><TableCell>Severity</TableCell><TableCell /></TableRow></TableHead><TableBody>{findings.map(item => <TableRow key={item.id} hover>{workspace.isRoot && <TableCell>{companyNames.get(item.companyId)}</TableCell>}<TableCell><Button className="quality-asset-link" onClick={() => redirect(`/assets/${item.assetId}/show`)}>{item.assetName}</Button><Typography
            variant="caption"
            sx={{
              display: "block",
              color: "text.secondary"
            }}>{item.assetType} · {words(item.source)}</Typography></TableCell><TableCell><Typography sx={{
            fontWeight: 800
          }}>{item.ruleLabel}</Typography><Typography variant="caption" sx={{
            color: "text.secondary"
          }}>{words(item.category)}</Typography></TableCell><TableCell><Typography variant="body2">{item.evidence}</Typography><Typography variant="caption" color="primary">{item.recommendation}</Typography></TableCell><TableCell><Chip size="small" color={item.severity === 'high' ? 'error' : 'warning'} label={words(item.severity)} /></TableCell><TableCell>{canManage && <Button size="small" onClick={() => { setWaive(item); setReason(''); }}>Exception</Button>}</TableCell></TableRow>)}</TableBody></Table></TableContainer>{!findings.length && <Alert severity="success" sx={{ m: 2 }}>No findings match these filters.</Alert>}</>}
          {tab === 1 && <TableContainer><Table size="small"><TableHead><TableRow>{workspace.isRoot && <TableCell>Customer</TableCell>}<TableCell>Source record</TableCell><TableCell>Suggested CI</TableCell><TableCell>Match evidence</TableCell><TableCell>Confidence</TableCell><TableCell /></TableRow></TableHead><TableBody>{snapshot.candidates.map(item => <TableRow key={item.id}>{workspace.isRoot && <TableCell>{companyNames.get(item.companyId)}</TableCell>}<TableCell><Typography sx={{
            fontWeight: 800
          }}>{item.externalName || item.externalId}</Typography><Typography variant="caption">{words(item.provider)} · {item.externalObjectType}</Typography></TableCell><TableCell>{item.candidateAssetName || 'Create new CI'}</TableCell><TableCell>{item.reason}</TableCell><TableCell>{Math.round(item.confidence * 100)}%</TableCell><TableCell>{canManage && <Button size="small" onClick={() => { setDecision(item); setDecisionType(item.candidateAssetId ? 'use_existing' : 'create_new'); setTargetAssetId(item.candidateAssetId || ''); }}>Review</Button>}</TableCell></TableRow>)}</TableBody></Table></TableContainer>}
          {tab === 1 && !snapshot.candidates.length && <Alert severity="success" sx={{ m: 2 }}>No ambiguous source records need review.</Alert>}
          {tab === 2 && <><Stack
            direction="row"
            sx={{
              justifyContent: "space-between",
              p: 2,
              pb: 1
            }}><Box><Typography variant="h5">Canonical field authority</Typography><Typography variant="body2" sx={{
            color: "text.secondary"
          }}>Lower priority numbers win when two sources propose different values.</Typography></Box>{canManage && <Button variant="contained" onClick={() => setAuthority({ ...blankAuthority, companyId: workspace.isRoot ? workspace.companies[0]?.id || '' : workspace.companyId })}>Add rule</Button>}</Stack><TableContainer><Table size="small"><TableHead><TableRow>{workspace.isRoot && <TableCell>Customer</TableCell>}<TableCell>CI type</TableCell><TableCell>Field</TableCell><TableCell>Authoritative source</TableCell><TableCell>Priority</TableCell><TableCell /></TableRow></TableHead><TableBody>{snapshot.fieldAuthority.map(rule => <TableRow key={`${rule.companyId}:${rule.ciType}:${rule.fieldName}:${rule.provider}`}>{workspace.isRoot && <TableCell>{companyNames.get(rule.companyId)}</TableCell>}<TableCell>{rule.ciType}</TableCell><TableCell>{words(rule.fieldName)}</TableCell><TableCell>{words(rule.provider)}</TableCell><TableCell>{rule.priority}</TableCell><TableCell>{canManage && <><Button size="small" onClick={() => setAuthority(rule)}>Edit</Button><Button size="small" color="error" startIcon={<DeleteOutlined />} onClick={() => void deleteAuthority(rule)}>Delete</Button></>}</TableCell></TableRow>)}</TableBody></Table></TableContainer>{!snapshot.fieldAuthority.length && <Alert severity="info" sx={{ m: 2 }}>No explicit source authority rules yet. Integration updates will remain conservative until rules are added.</Alert>}</>}
        </Paper>
      </>}
      <Dialog open={Boolean(waive)} onClose={() => setWaive(null)} maxWidth="sm" fullWidth><DialogTitle>Record a data-quality exception</DialogTitle><DialogContent><Alert severity="warning" sx={{ mb: 2 }}>This suppresses the finding; it does not change the CI. A reason is retained in the audit trail.</Alert><Stack spacing={2}><TextField label="Reason" multiline minRows={3} value={reason} onChange={event => setReason(event.target.value)} required /><TextField label="Expiry (optional)" type="date" value={expiresAt} onChange={event => setExpiresAt(event.target.value)} slotProps={{ inputLabel: { shrink: true } }} /></Stack></DialogContent><DialogActions><Button onClick={() => setWaive(null)}>Cancel</Button><Button variant="contained" disabled={reason.trim().length < 4} onClick={() => void saveException()}>Record exception</Button></DialogActions></Dialog>
      <Dialog open={Boolean(decision)} onClose={() => setDecision(null)} maxWidth="sm" fullWidth><DialogTitle>Review source record</DialogTitle><DialogContent><Stack spacing={2} sx={{ mt: 1 }}><FormControl><InputLabel>Decision</InputLabel><Select label="Decision" value={decisionType} onChange={event => setDecisionType(event.target.value)}><MenuItem value="use_existing">Use existing CI</MenuItem><MenuItem value="create_new">Create a new CI</MenuItem><MenuItem value="ignore">Ignore source record</MenuItem></Select></FormControl>{decisionType === 'use_existing' && <TextField label="Target CI ID" value={targetAssetId} onChange={event => setTargetAssetId(event.target.value)} helperText={decision?.candidateAssetName || 'Paste an accessible CI ID'} />}<TextField label="Decision notes" multiline minRows={3} value={decisionNotes} onChange={event => setDecisionNotes(event.target.value)} required /><Alert severity="info">The decision is recorded now. Connector execution will apply it when the integration worker is enabled.</Alert></Stack></DialogContent><DialogActions><Button onClick={() => setDecision(null)}>Cancel</Button><Button variant="contained" startIcon={<CheckCircleOutlined />} disabled={decisionNotes.trim().length < 4 || (decisionType === 'use_existing' && !targetAssetId)} onClick={() => void saveDecision()}>Record decision</Button></DialogActions></Dialog>
      <Dialog open={Boolean(authority)} onClose={() => setAuthority(null)} maxWidth="sm" fullWidth><DialogTitle>Source authority rule</DialogTitle><DialogContent>{authority && <Stack spacing={2} sx={{ mt: 1 }}>{workspace.isRoot && <FormControl><InputLabel>Customer</InputLabel><Select label="Customer" value={authority.companyId} onChange={event => setAuthority({ ...authority, companyId: event.target.value })}>{workspace.companies.map(item => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</Select></FormControl>}<TextField label="CI type" value={authority.ciType} onChange={event => setAuthority({ ...authority, ciType: event.target.value })} helperText="Use * for every CI type" /><TextField label="Canonical field" value={authority.fieldName} onChange={event => setAuthority({ ...authority, fieldName: event.target.value })} /><FormControl><InputLabel>Source</InputLabel><Select label="Source" value={authority.provider} onChange={event => setAuthority({ ...authority, provider: event.target.value })}>{['connectwise', 'ncentral', 'passportal', 'future'].map(value => <MenuItem key={value} value={value}>{words(value)}</MenuItem>)}</Select></FormControl><TextField label="Priority" type="number" value={authority.priority} onChange={event => setAuthority({ ...authority, priority: Number(event.target.value) })} helperText="Lower numbers are more authoritative" /></Stack>}</DialogContent><DialogActions><Button onClick={() => setAuthority(null)}>Cancel</Button><Button variant="contained" disabled={!authority?.companyId || !authority?.fieldName || !authority?.ciType} onClick={() => void saveAuthority()}>Save rule</Button></DialogActions></Dialog>
    </Box>
  );
}
