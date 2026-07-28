import AssessmentOutlined from '@mui/icons-material/AssessmentOutlined';
import DownloadOutlined from '@mui/icons-material/DownloadOutlined';
import FilterAltOutlined from '@mui/icons-material/FilterAltOutlined';
import HistoryOutlined from '@mui/icons-material/HistoryOutlined';
import OpenInNewOutlined from '@mui/icons-material/OpenInNewOutlined';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogContent, DialogTitle, FormControl,
  Grid, InputLabel, MenuItem, Paper, Select, Stack, Table, TableBody, TableCell, TableContainer,
  TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiDownload, apiFetch } from './session';
import { Title } from './ui';
import type { AuditEvent, ReportDefinition, ReportPreview } from './types';
import { useWorkspace } from './workspace';

function heading(eyebrow: string, title: string, copy: string, icon: React.ReactNode) {
  return (
    <Stack
      direction="row"
      spacing={2}
      sx={{
        alignItems: "flex-start",
        mb: 3
      }}><Box className="governance-heading-icon">{icon}</Box><Box><Typography variant="overline" color="primary">{eyebrow}</Typography><Typography variant="h3">{title}</Typography><Typography
      sx={{
        color: "text.secondary",
        mt: .75,
        maxWidth: 820
      }}>{copy}</Typography></Box></Stack>
  );
}

function words(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase());
}

function dateTime(value: string) {
  return value ? new Date(value).toLocaleString() : 'Not recorded';
}

function displayValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

function outcomeColor(value: AuditEvent['outcome']): 'success' | 'warning' | 'error' {
  return value === 'success' ? 'success' : value === 'denied' ? 'warning' : 'error';
}

export function AuditTimeline({ entityType, entityId, companyId }: { entityType: string; entityId: string; companyId: string }) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    const query = new URLSearchParams({ companyId, entityType, entityId, limit: '12' });
    apiFetch<AuditEvent[]>(`/api/audit-events?${query}`).then(records => { if (active) setEvents(records); }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : 'Activity could not be loaded.'); });
    return () => { active = false; };
  }, [companyId, entityId, entityType]);
  return (
    <Paper className="audit-timeline" sx={{ mt: 2 }}><Stack
      direction="row"
      spacing={1}
      sx={{
        alignItems: "center",
        mb: 1.5
      }}><HistoryOutlined color="primary" /><Typography variant="h5">Activity</Typography></Stack>
      {error && <Alert severity="error">{error}</Alert>}
      {!error && !events.length && <Typography sx={{
        color: "text.secondary"
      }}>No recorded activity is available for this item yet.</Typography>}
      <Stack>{events.map(event => <Box key={event.id} className="audit-timeline-row"><Stack direction="row" spacing={1} sx={{
        justifyContent: "space-between"
      }}><Typography sx={{
        fontWeight: 800
      }}>{words(event.action)}</Typography><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>{dateTime(event.createdAt)}</Typography></Stack><Typography variant="body2" sx={{
        color: "text.secondary"
      }}>{event.actorLabel} · {words(event.sourceSystem)}</Typography>{event.changes?.length > 0 && <Typography variant="caption" color="primary">{event.changes.length} field{event.changes.length === 1 ? '' : 's'} changed</Typography>}</Box>)}</Stack>
    </Paper>
  );
}

export function AuditCenterPage() {
  const workspace = useWorkspace();
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [selected, setSelected] = useState<AuditEvent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [outcome, setOutcome] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const searchRef = useRef(search);
  searchRef.current = search;
  const companyNames = useMemo(() => new Map(workspace.companies.map(item => [item.id, item.name])), [workspace.companies]);
  const load = useCallback(async () => {
    setLoading(true); setError('');
    const query = new URLSearchParams({ limit: '500' });
    if (!workspace.isRoot) query.set('companyId', workspace.companyId);
    if (searchRef.current) query.set('search', searchRef.current);
    if (category) query.set('category', category);
    if (outcome) query.set('outcome', outcome);
    if (dateFrom) query.set('dateFrom', dateFrom);
    if (dateTo) query.set('dateTo', dateTo);
    try { setEvents(await apiFetch<AuditEvent[]>(`/api/audit-events?${query}`)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Audit events could not be loaded.'); }
    finally { setLoading(false); }
  }, [workspace.companyId, workspace.isRoot, category, outcome, dateFrom, dateTo]);
  useEffect(() => { void load(); }, [load]);
  return (
    <Box className="governance-page"><Title title="Audit activity" />
      {heading('Governance', workspace.isRoot ? 'MSP audit center' : `${workspace.companyName} audit`, 'Trace security, access, configuration and CMDB changes with attributable actors, outcomes and correlation references.', <HistoryOutlined />)}
      <Paper className="governance-filter"><TextField size="small" label="Search activity" value={search} onChange={event => setSearch(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void load(); }} /><FormControl size="small"><InputLabel>Category</InputLabel><Select label="Category" value={category} onChange={event => setCategory(event.target.value)}><MenuItem value="">All categories</MenuItem>{['authentication', 'access', 'configuration', 'data', 'integration', 'report', 'recovery'].map(value => <MenuItem key={value} value={value}>{words(value)}</MenuItem>)}</Select></FormControl><FormControl size="small"><InputLabel>Outcome</InputLabel><Select label="Outcome" value={outcome} onChange={event => setOutcome(event.target.value)}><MenuItem value="">All outcomes</MenuItem>{['success', 'denied', 'failed'].map(value => <MenuItem key={value} value={value}>{words(value)}</MenuItem>)}</Select></FormControl><TextField size="small" label="From" type="date" value={dateFrom} onChange={event => setDateFrom(event.target.value)} slotProps={{ inputLabel: { shrink: true } }} /><TextField size="small" label="To" type="date" value={dateTo} onChange={event => setDateTo(event.target.value)} slotProps={{ inputLabel: { shrink: true } }} /><Button variant="outlined" startIcon={<FilterAltOutlined />} onClick={() => void load()}>Apply</Button></Paper>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {loading ? <Paper className="governance-loading"><CircularProgress size={28} /><Typography>Loading attributable activity…</Typography></Paper> : <Paper className="governance-table"><TableContainer><Table size="small"><TableHead><TableRow><TableCell>Time</TableCell>{workspace.isRoot && <TableCell>Customer</TableCell>}<TableCell>Actor</TableCell><TableCell>Event</TableCell><TableCell>Entity</TableCell><TableCell>Source</TableCell><TableCell>Outcome</TableCell><TableCell /></TableRow></TableHead><TableBody>{events.map(event => <TableRow key={event.id} hover><TableCell sx={{ whiteSpace: 'nowrap' }}>{dateTime(event.createdAt)}</TableCell>{workspace.isRoot && <TableCell>{event.companyId ? companyNames.get(event.companyId) || event.companyId : 'MSP'}</TableCell>}<TableCell><Typography sx={{
        fontWeight: 750
      }}>{event.actorLabel}</Typography><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>{words(event.actorType)}</Typography></TableCell><TableCell><Typography sx={{
        fontWeight: 750
      }}>{words(event.action)}</Typography><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>{words(event.category)}</Typography></TableCell><TableCell>{event.entityName || words(event.entityType)}</TableCell><TableCell>{words(event.sourceSystem)}</TableCell><TableCell><Chip size="small" color={outcomeColor(event.outcome)} label={words(event.outcome)} /></TableCell><TableCell><Button size="small" endIcon={<OpenInNewOutlined />} onClick={() => setSelected(event)}>Details</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer>{!events.length && <Alert severity="info" sx={{ m: 2 }}>No activity matches these filters.</Alert>}</Paper>}
      <Dialog open={Boolean(selected)} onClose={() => setSelected(null)} maxWidth="md" fullWidth><DialogTitle>{selected ? `${words(selected.action)} · ${selected.entityName || words(selected.entityType)}` : 'Audit event'}</DialogTitle><DialogContent>{selected && <Stack spacing={2}><Grid container spacing={1.5}><Grid size={{ xs: 12, sm: 6 }}><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>Actor</Typography><Typography>{selected.actorLabel} ({words(selected.actorType)})</Typography></Grid><Grid size={{ xs: 12, sm: 6 }}><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>Occurred</Typography><Typography>{dateTime(selected.createdAt)}</Typography></Grid><Grid size={{ xs: 12, sm: 6 }}><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>Request ID</Typography><Typography sx={{ wordBreak: 'break-all' }}>{selected.requestId || 'Not available'}</Typography></Grid><Grid size={{ xs: 12, sm: 6 }}><Typography variant="caption" sx={{
        color: "text.secondary"
      }}>Correlation ID</Typography><Typography sx={{ wordBreak: 'break-all' }}>{selected.correlationId || 'Not available'}</Typography></Grid></Grid><Typography variant="h6">Field changes</Typography>{selected.changes?.length ? <TableContainer component={Paper} variant="outlined"><Table size="small"><TableHead><TableRow><TableCell>Field</TableCell><TableCell>Before</TableCell><TableCell>After</TableCell></TableRow></TableHead><TableBody>{selected.changes.map((change, index) => <TableRow key={`${change.field}-${index}`}><TableCell>{words(change.field)}</TableCell><TableCell><pre className="audit-value">{displayValue(change.before)}</pre></TableCell><TableCell><pre className="audit-value">{displayValue(change.after)}</pre></TableCell></TableRow>)}</TableBody></Table></TableContainer> : <Alert severity="info">This event does not contain a field-level change.</Alert>}<Typography variant="h6">Context</Typography><pre className="audit-metadata">{JSON.stringify(selected.metadata || {}, null, 2)}</pre></Stack>}</DialogContent></Dialog>
    </Box>
  );
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = filename; link.click(); URL.revokeObjectURL(url);
}

export function ReportsPage() {
  const workspace = useWorkspace();
  const [catalog, setCatalog] = useState<ReportDefinition[]>([]);
  const [reportId, setReportId] = useState('');
  const [preview, setPreview] = useState<ReportPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const suffix = workspace.isRoot ? '' : `?companyId=${encodeURIComponent(workspace.companyId)}`;
  useEffect(() => { let active = true; setLoading(true); setError(''); apiFetch<ReportDefinition[]>(`/api/reports/catalog${suffix}`).then(records => { if (!active) return; setCatalog(records); setReportId(current => records.some(item => item.id === current) ? current : records[0]?.id || ''); }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : 'Report catalogue could not be loaded.'); }).finally(() => { if (active) setLoading(false); }); return () => { active = false; }; }, [suffix]);
  useEffect(() => { if (!reportId) { setPreview(null); return; } let active = true; setLoading(true); apiFetch<ReportPreview>(`/api/reports/${encodeURIComponent(reportId)}/preview${suffix}`).then(value => { if (active) { setPreview(value); setError(''); } }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : 'Report could not be generated.'); }).finally(() => { if (active) setLoading(false); }); return () => { active = false; }; }, [reportId, suffix]);
  async function download(format: 'pdf' | 'xlsx' | 'csv') { try { const separator = suffix ? '&' : '?'; const result = await apiDownload(`/api/reports/${encodeURIComponent(reportId)}/download${suffix}${separator}format=${format}`); downloadBlob(result.blob, result.filename); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Report download failed.'); } }
  return (
    <Box className="governance-page"><Title title="Reports" />{heading('Governance', workspace.isRoot ? 'MSP reports center' : `${workspace.companyName} reports`, 'Generate consistent point-in-time operational, lifecycle, access and governance evidence in presentation and analysis formats.', <AssessmentOutlined />)}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      <Grid container spacing={2.5}><Grid size={{ xs: 12, lg: 3 }}><Paper className="report-catalog"><Typography variant="h5">Report catalogue</Typography><Typography
        variant="body2"
        sx={{
          color: "text.secondary",
          mb: 2
        }}>Select a controlled report template.</Typography><Stack spacing={1}>{catalog.map(item => <Button key={item.id} variant={reportId === item.id ? 'contained' : 'text'} sx={{ justifyContent: 'flex-start', textAlign: 'left', py: 1.2 }} onClick={() => setReportId(item.id)}>{item.title}</Button>)}</Stack></Paper></Grid><Grid size={{ xs: 12, lg: 9 }}>{loading && !preview ? <Paper className="governance-loading"><CircularProgress size={28} /><Typography>Building report…</Typography></Paper> : preview && <Stack spacing={2}><Paper className="report-heading"><Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{
        justifyContent: "space-between"
      }}><Box><Typography variant="overline" color="primary">Point-in-time report</Typography><Typography variant="h4">{preview.title}</Typography><Typography sx={{
        color: "text.secondary"
      }}>{preview.description}</Typography><Stack direction="row" spacing={1} sx={{ mt: 1.5 }}><Chip size="small" label={`${preview.summary.rowCount} rows`} /><Chip size="small" variant="outlined" label={`Generated ${dateTime(preview.generatedAt)}`} /></Stack></Box><Stack direction="row" spacing={1} sx={{
        alignItems: "flex-start"
      }}><Button variant="contained" startIcon={<DownloadOutlined />} onClick={() => void download('pdf')}>PDF</Button><Button variant="outlined" onClick={() => void download('xlsx')}>XLSX</Button><Button variant="outlined" onClick={() => void download('csv')}>CSV</Button></Stack></Stack></Paper><Paper className="governance-table"><TableContainer><Table size="small"><TableHead><TableRow>{preview.columns.map(column => <TableCell key={column.key}>{column.label}</TableCell>)}</TableRow></TableHead><TableBody>{preview.rows.map((row, index) => <TableRow key={index} hover>{preview.columns.map(column => <TableCell key={column.key}>{displayValue(row[column.key])}</TableCell>)}</TableRow>)}</TableBody></Table></TableContainer>{!preview.rows.length && <Alert severity="info" sx={{ m: 2 }}>This report currently has no matching records.</Alert>}{preview.previewLimited && <Alert severity="info" sx={{ m: 2 }}>Preview limited to 100 rows. Downloads contain the complete report.</Alert>}</Paper></Stack>}</Grid></Grid>
    </Box>
  );
}
