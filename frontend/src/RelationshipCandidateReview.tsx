import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import CloseOutlined from '@mui/icons-material/CloseOutlined';
import CenterFocusStrongOutlined from '@mui/icons-material/CenterFocusStrongOutlined';
import RefreshOutlined from '@mui/icons-material/RefreshOutlined';
import ReportProblemOutlined from '@mui/icons-material/ReportProblemOutlined';
import SearchOutlined from '@mui/icons-material/SearchOutlined';
import VisibilityOffOutlined from '@mui/icons-material/VisibilityOffOutlined';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputAdornment,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from '@mui/material';
import { useEffect, useMemo, useState } from 'react';

export type RelationshipCandidateDecision = 'approve' | 'reject' | 'ignore';

export type RelationshipCandidate = {
  id: string;
  revision?: number;
  observationCount?: number;
  companyId: string;
  companyName?: string;
  provider: string;
  fromCiId?: string;
  toCiId?: string;
  fromName: string;
  toName: string;
  relationshipType: string;
  confidence: number;
  evidence?: {
    messages?: string[];
    impactPolicy?: string;
    freshness?: string;
    observedAt?: string;
    providerEvidence?: Record<string, unknown>;
    [key: string]: unknown;
  };
  firstObservedAt?: string;
  lastSeenAt?: string;
  observedAt?: string;
  retiredAt?: string | null;
  state: string;
};

type Props = {
  open: boolean;
  candidates: RelationshipCandidate[];
  loading: boolean;
  error: string;
  canManage: boolean;
  showCompany?: boolean;
  onClose: () => void;
  onRefresh: () => Promise<void>;
  onFocusCandidate?: (candidate: RelationshipCandidate) => void;
  onDecision: (
    candidate: RelationshipCandidate,
    decision: RelationshipCandidateDecision,
    notes: string,
  ) => Promise<void>;
};

export type RelationshipCandidateFilters = {
  search: string;
  companyId: string;
  provider: string;
  relationshipType: string;
  confidence: 'all' | 'high' | 'medium' | 'low';
  freshness: 'all' | CandidateFreshnessState;
};

export type CandidateFreshnessState = 'current' | 'aging' | 'stale' | 'unreported';

const emptyFilters: RelationshipCandidateFilters = {
  search: '',
  companyId: '',
  provider: '',
  relationshipType: '',
  confidence: 'all',
  freshness: 'all',
};

const decisionCopy: Record<RelationshipCandidateDecision, {
  title: string;
  action: string;
  detail: string;
  severity: 'info' | 'warning' | 'error';
}> = {
  approve: {
    title: 'Approve relationship suggestion',
    action: 'Approve suggestion',
    detail: 'This creates this canonical relationship after review. It does not write to the source provider or change any manually created relationship.',
    severity: 'info',
  },
  reject: {
    title: 'Reject relationship suggestion',
    action: 'Reject bad evidence',
    detail: 'Reject when the evidence is incorrect or identifies the wrong configuration items. The decision is retained with the provider evidence.',
    severity: 'error',
  },
  ignore: {
    title: 'Ignore relationship suggestion',
    action: 'Ignore suggestion',
    detail: 'Ignore when the evidence is valid but the relationship is not useful in this CMDB. The suggestion leaves the pending queue without changing either CI.',
    severity: 'warning',
  },
};

export function relationshipTypeLabel(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, character => character.toUpperCase());
}

export function confidencePercent(value: number) {
  const percentage = value <= 1 ? value * 100 : value;
  return Math.max(0, Math.min(100, Math.round(percentage)));
}

export function candidateFreshness(candidate: RelationshipCandidate) {
  const value = candidateTimestamp(candidate);
  if (!value) return 'Not reported';
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

export function candidateTimestamp(candidate: RelationshipCandidate) {
  return candidate.lastSeenAt
    || candidate.evidence?.freshness
    || candidate.evidence?.observedAt
    || candidate.observedAt
    || candidate.firstObservedAt;
}

export function candidateFreshnessState(
  candidate: RelationshipCandidate,
  now: Date = new Date(),
): CandidateFreshnessState {
  const value = candidateTimestamp(candidate);
  if (!value) return 'unreported';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return 'unreported';
  const age = Math.max(0, now.valueOf() - parsed.valueOf());
  if (age <= 24 * 60 * 60 * 1000) return 'current';
  if (age <= 7 * 24 * 60 * 60 * 1000) return 'aging';
  return 'stale';
}

export function providerLabel(value: string) {
  const normalized = value.toLowerCase();
  if (normalized === 'ncentral') return 'N-central';
  if (normalized === 'connectwise') return 'ConnectWise Manage';
  if (normalized === 'passportal') return 'Passportal';
  return value || 'Unknown provider';
}

export function filterRelationshipCandidates(
  candidates: RelationshipCandidate[],
  filters: RelationshipCandidateFilters,
  now: Date = new Date(),
) {
  const query = filters.search.trim().toLowerCase();
  return candidates.filter(candidate => {
    const percentage = confidencePercent(candidate.confidence);
    const messages = candidate.evidence?.messages || [];
    const matchesSearch = !query || [
      candidate.fromName,
      candidate.toName,
      candidate.companyName,
      candidate.provider,
      relationshipTypeLabel(candidate.relationshipType),
      ...messages,
    ].some(value => String(value || '').toLowerCase().includes(query));
    const matchesConfidence = filters.confidence === 'all'
      || (filters.confidence === 'high' && percentage >= 85)
      || (filters.confidence === 'medium' && percentage >= 65 && percentage < 85)
      || (filters.confidence === 'low' && percentage < 65);
    return matchesSearch
      && (!filters.companyId || candidate.companyId === filters.companyId)
      && (!filters.provider || candidate.provider === filters.provider)
      && (!filters.relationshipType || candidate.relationshipType === filters.relationshipType)
      && matchesConfidence
      && (filters.freshness === 'all' || candidateFreshnessState(candidate, now) === filters.freshness);
  });
}

export function RelationshipCandidateReviewDialog({
  open,
  candidates,
  loading,
  error,
  canManage,
  showCompany = false,
  onClose,
  onRefresh,
  onFocusCandidate,
  onDecision,
}: Props) {
  const [candidate, setCandidate] = useState<RelationshipCandidate | null>(null);
  const [decision, setDecision] = useState<RelationshipCandidateDecision | null>(null);
  const [notes, setNotes] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [decisionError, setDecisionError] = useState('');
  const [filters, setFilters] = useState<RelationshipCandidateFilters>(emptyFilters);

  useEffect(() => {
    if (!open) setFilters(emptyFilters);
  }, [open]);

  const companies = useMemo(() => [...new Map(candidates.map(item => [
    item.companyId,
    item.companyName || item.companyId,
  ])).entries()].sort((left, right) => left[1].localeCompare(right[1])), [candidates]);
  const providers = useMemo(() => [...new Set(candidates.map(item => item.provider))]
    .sort((left, right) => providerLabel(left).localeCompare(providerLabel(right))), [candidates]);
  const candidateTypes = useMemo(() => [...new Set(candidates.map(item => item.relationshipType))]
    .sort((left, right) => relationshipTypeLabel(left).localeCompare(relationshipTypeLabel(right))), [candidates]);
  const filteredCandidates = useMemo(
    () => filterRelationshipCandidates(candidates, filters),
    [candidates, filters],
  );
  const filtersActive = Object.entries(filters).some(([, value]) => value && value !== 'all');

  const beginDecision = (
    selected: RelationshipCandidate,
    selectedDecision: RelationshipCandidateDecision,
  ) => {
    setCandidate(selected);
    setDecision(selectedDecision);
    setNotes('');
    setDecisionError('');
  };

  const closeDecision = () => {
    if (submitting) return;
    setCandidate(null);
    setDecision(null);
    setNotes('');
    setDecisionError('');
  };

  const submitDecision = async () => {
    if (!candidate || !decision || notes.trim().length < 4) return;
    setSubmitting(true);
    setDecisionError('');
    try {
      await onDecision(candidate, decision, notes.trim());
      setCandidate(null);
      setDecision(null);
      setNotes('');
    } catch (value) {
      setDecisionError(
        value instanceof Error ? value.message : 'The relationship decision could not be saved.',
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Dialog open={open} onClose={onClose} maxWidth="xl" fullWidth>
        <DialogTitle>
          <Stack direction="row" spacing={2} sx={{ alignItems: 'center', justifyContent: 'space-between' }}>
            <Box>
              <Typography variant="overline" color="primary">Provider evidence review</Typography>
              <Typography variant="h5">Relationship suggestions</Typography>
              <Typography variant="caption" color="text.secondary">
                {filteredCandidates.length} of {candidates.length} pending suggestion{candidates.length === 1 ? '' : 's'}
              </Typography>
            </Box>
            <IconButton aria-label="Close relationship suggestions" onClick={onClose}>
              <CloseOutlined />
            </IconButton>
          </Stack>
        </DialogTitle>
        <DialogContent dividers>
          <Alert severity="info" sx={{ mb: 2 }}>
            Suggestions are read-only evidence from connected providers until an authorised user approves one.
            Provider data and manually created relationships are never changed by this review.
          </Alert>
          {!canManage && (
            <Alert severity="warning" sx={{ mb: 2 }}>
              You can inspect the evidence, but your role cannot manage relationships.
            </Alert>
          )}
          {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
          {!loading && candidates.length > 0 && (
            <Paper className="relationship-suggestion-filters" variant="outlined">
              <TextField
                size="small"
                label="Search suggestions"
                value={filters.search}
                onChange={event => setFilters(current => ({ ...current, search: event.target.value }))}
                slotProps={{
                  input: {
                    startAdornment: <InputAdornment position="start"><SearchOutlined fontSize="small" /></InputAdornment>,
                  },
                }}
              />
              {showCompany && (
                <FormControl size="small">
                  <InputLabel>Customer</InputLabel>
                  <Select
                    label="Customer"
                    value={filters.companyId}
                    onChange={event => setFilters(current => ({ ...current, companyId: event.target.value }))}
                  >
                    <MenuItem value="">All customers</MenuItem>
                    {companies.map(([id, name]) => <MenuItem key={id} value={id}>{name}</MenuItem>)}
                  </Select>
                </FormControl>
              )}
              <FormControl size="small">
                <InputLabel>Provider</InputLabel>
                <Select
                  label="Provider"
                  value={filters.provider}
                  onChange={event => setFilters(current => ({ ...current, provider: event.target.value }))}
                >
                  <MenuItem value="">All providers</MenuItem>
                  {providers.map(provider => <MenuItem key={provider} value={provider}>{providerLabel(provider)}</MenuItem>)}
                </Select>
              </FormControl>
              <FormControl size="small">
                <InputLabel>Relationship type</InputLabel>
                <Select
                  label="Relationship type"
                  value={filters.relationshipType}
                  onChange={event => setFilters(current => ({ ...current, relationshipType: event.target.value }))}
                >
                  <MenuItem value="">All relationship types</MenuItem>
                  {candidateTypes.map(type => <MenuItem key={type} value={type}>{relationshipTypeLabel(type)}</MenuItem>)}
                </Select>
              </FormControl>
              <FormControl size="small">
                <InputLabel>Confidence</InputLabel>
                <Select
                  label="Confidence"
                  value={filters.confidence}
                  onChange={event => setFilters(current => ({
                    ...current,
                    confidence: event.target.value as RelationshipCandidateFilters['confidence'],
                  }))}
                >
                  <MenuItem value="all">All confidence</MenuItem>
                  <MenuItem value="high">High · 85%+</MenuItem>
                  <MenuItem value="medium">Medium · 65–84%</MenuItem>
                  <MenuItem value="low">Low · below 65%</MenuItem>
                </Select>
              </FormControl>
              <FormControl size="small">
                <InputLabel>Freshness</InputLabel>
                <Select
                  label="Freshness"
                  value={filters.freshness}
                  onChange={event => setFilters(current => ({
                    ...current,
                    freshness: event.target.value as RelationshipCandidateFilters['freshness'],
                  }))}
                >
                  <MenuItem value="all">All freshness</MenuItem>
                  <MenuItem value="current">Current · 24 hours</MenuItem>
                  <MenuItem value="aging">Aging · 1–7 days</MenuItem>
                  <MenuItem value="stale">Stale · over 7 days</MenuItem>
                  <MenuItem value="unreported">Not reported</MenuItem>
                </Select>
              </FormControl>
              <Button disabled={!filtersActive} onClick={() => setFilters(emptyFilters)}>Clear filters</Button>
            </Paper>
          )}
          {loading ? (
            <Stack
              component="output"
              aria-label="Loading relationship suggestions"
              spacing={1.5}
              sx={{ minHeight: 180, alignItems: 'center', justifyContent: 'center' }}
            >
              <CircularProgress size={30} />
              <Typography color="text.secondary">Loading pending suggestions…</Typography>
            </Stack>
          ) : !candidates.length ? (
            <Alert severity="success">No pending relationship suggestions in this workspace.</Alert>
          ) : !filteredCandidates.length ? (
            <Alert severity="info">No suggestions match the current filters.</Alert>
          ) : (
            <TableContainer>
              <Table size="small" aria-label="Pending relationship suggestions">
                <caption>
                  Provider relationship evidence awaiting an explicit CMDB decision.
                </caption>
                <TableHead>
                  <TableRow>
                    <TableCell>From CI</TableCell>
                    {showCompany && <TableCell>Customer</TableCell>}
                    <TableCell>Relationship</TableCell>
                    <TableCell>To CI</TableCell>
                    <TableCell>Confidence</TableCell>
                    <TableCell>Evidence</TableCell>
                    <TableCell>Freshness</TableCell>
                    <TableCell align="right">Decision</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {filteredCandidates.map(item => {
                    const percentage = confidencePercent(item.confidence);
                    const messages = item.evidence?.messages || [];
                    const freshness = candidateFreshnessState(item);
                    return (
                      <TableRow key={item.id} hover>
                        <TableCell>
                          <Typography variant="body2" sx={{ fontWeight: 700 }}>{item.fromName || 'Unknown CI'}</Typography>
                        </TableCell>
                        {showCompany && (
                          <TableCell>
                            <Typography variant="body2" sx={{ fontWeight: 700 }}>
                              {item.companyName || item.companyId}
                            </Typography>
                          </TableCell>
                        )}
                        <TableCell>
                          <Stack spacing={0.5} sx={{ alignItems: 'flex-start' }}>
                            <Chip size="small" label={relationshipTypeLabel(item.relationshipType)} />
                            <Typography variant="caption" color="text.secondary">
                              {providerLabel(item.provider)}
                              {item.evidence?.impactPolicy ? ` · ${item.evidence.impactPolicy} impact` : ''}
                            </Typography>
                          </Stack>
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2" sx={{ fontWeight: 700 }}>{item.toName || 'Unknown CI'}</Typography>
                        </TableCell>
                        <TableCell>
                          <Chip
                            size="small"
                            color={percentage >= 85 ? 'success' : percentage >= 65 ? 'warning' : 'default'}
                            label={`${percentage}%`}
                            aria-label={`${percentage} percent confidence`}
                          />
                        </TableCell>
                        <TableCell sx={{ minWidth: 240 }}>
                          {messages.length ? (
                            <Box component="ul" sx={{ m: 0, pl: 2 }}>
                              {messages.map((message, index) => (
                                <Typography component="li" variant="caption" key={`${item.id}-${index}`}>
                                  {message}
                                </Typography>
                              ))}
                            </Box>
                          ) : (
                            <Typography variant="caption" color="text.secondary">
                              No supporting explanation supplied.
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell>
                          <Stack spacing={0.5} sx={{ alignItems: 'flex-start' }}>
                            <Chip
                              size="small"
                              variant="outlined"
                              color={freshness === 'current' ? 'success' : freshness === 'aging' ? 'warning' : freshness === 'stale' ? 'error' : 'default'}
                              label={freshness === 'unreported' ? 'Not reported' : relationshipTypeLabel(freshness)}
                            />
                            <Typography variant="caption">{candidateFreshness(item)}</Typography>
                          </Stack>
                        </TableCell>
                        <TableCell align="right">
                          {canManage ? (
                            <Stack spacing={0.5} sx={{ alignItems: 'flex-end' }}>
                              <Button
                                size="small"
                                variant="contained"
                                startIcon={<CheckCircleOutlined />}
                                onClick={() => beginDecision(item, 'approve')}
                              >
                                Approve
                              </Button>
                              <Stack direction="row" spacing={0.5}>
                                <Button
                                  size="small"
                                  color="error"
                                  startIcon={<ReportProblemOutlined />}
                                  onClick={() => beginDecision(item, 'reject')}
                                >
                                  Reject
                                </Button>
                                <Button
                                  size="small"
                                  color="warning"
                                  startIcon={<VisibilityOffOutlined />}
                                  onClick={() => beginDecision(item, 'ignore')}
                                >
                                  Ignore
                                </Button>
                              </Stack>
                              {onFocusCandidate && item.fromCiId && item.toCiId && (
                                <Button
                                  size="small"
                                  color="inherit"
                                  startIcon={<CenterFocusStrongOutlined />}
                                  onClick={() => onFocusCandidate(item)}
                                >
                                  Show on map
                                </Button>
                              )}
                            </Stack>
                          ) : (
                            <Stack spacing={0.5} sx={{ alignItems: 'flex-end' }}>
                              <Chip size="small" variant="outlined" label="Review only" />
                              {onFocusCandidate && item.fromCiId && item.toCiId && (
                                <Button
                                  size="small"
                                  color="inherit"
                                  startIcon={<CenterFocusStrongOutlined />}
                                  onClick={() => onFocusCandidate(item)}
                                >
                                  Show on map
                                </Button>
                              )}
                            </Stack>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </DialogContent>
        <DialogActions sx={{ justifyContent: 'space-between' }}>
          <Button
            startIcon={loading ? <CircularProgress size={15} /> : <RefreshOutlined />}
            disabled={loading}
            onClick={() => void onRefresh()}
          >
            Refresh
          </Button>
          <Button onClick={onClose}>Close</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={Boolean(candidate && decision)} onClose={closeDecision} maxWidth="sm" fullWidth>
        {candidate && decision && (
          <>
            <DialogTitle>{decisionCopy[decision].title}</DialogTitle>
            <DialogContent>
              <Stack spacing={2} sx={{ pt: 1 }}>
                <Alert severity={decisionCopy[decision].severity}>
                  {decisionCopy[decision].detail}
                </Alert>
                <Typography>
                  <strong>{candidate.fromName}</strong>{' '}
                  {relationshipTypeLabel(candidate.relationshipType).toLowerCase()}{' '}
                  <strong>{candidate.toName}</strong>
                </Typography>
                {decisionError && <Alert severity="error">{decisionError}</Alert>}
                <TextField
                  required
                  fullWidth
                  multiline
                  minRows={3}
                  label="Decision notes"
                  name="relationship-decision-notes"
                  value={notes}
                  onChange={event => setNotes(event.target.value)}
                  helperText="Enter at least 4 characters. Notes are retained with the review evidence."
                  slotProps={{ htmlInput: { minLength: 4 } }}
                />
              </Stack>
            </DialogContent>
            <DialogActions>
              <Button disabled={submitting} onClick={closeDecision}>Cancel</Button>
              <Button
                variant="contained"
                color={decision === 'reject' ? 'error' : decision === 'ignore' ? 'warning' : 'primary'}
                disabled={submitting || notes.trim().length < 4}
                onClick={() => void submitDecision()}
              >
                {submitting ? 'Saving…' : decisionCopy[decision].action}
              </Button>
            </DialogActions>
          </>
        )}
      </Dialog>
    </>
  );
}
