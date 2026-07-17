import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  Grid,
  Paper,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import { useMemo, useState } from 'react';
import type { Company } from './types';

type CustomerScopeSelectorProps = {
  companies: Company[];
  directIds: string[];
  inheritedIds?: string[];
  inheritedLabel?: string;
  onDirectIdsChange: (ids: string[]) => void;
  disabled?: boolean;
  warningThreshold?: number;
};

const DISPLAY_LIMIT = 100;

function matchesCompany(company: Company, query: string) {
  const normalized = query.trim().toLowerCase();
  return !normalized || `${company.name} ${company.id}`.toLowerCase().includes(normalized);
}

/** Select direct customer exceptions while keeping group-derived access visible. */
export function CustomerScopeSelector({
  companies,
  directIds,
  inheritedIds = [],
  inheritedLabel = 'Access group',
  onDirectIdsChange,
  disabled = false,
  warningThreshold = 10,
}: CustomerScopeSelectorProps) {
  const [managerOpen, setManagerOpen] = useState(false);
  const [availableSearch, setAvailableSearch] = useState('');
  const [selectedSearch, setSelectedSearch] = useState('');
  const [effectiveSearch, setEffectiveSearch] = useState('');
  const inheritedSet = useMemo(() => new Set(inheritedIds), [inheritedIds]);
  const directSet = useMemo(() => new Set(directIds), [directIds]);
  const selectableCompanies = companies.filter(company => !inheritedSet.has(company.id));
  const directCompanies = companies.filter(company => directSet.has(company.id) && !inheritedSet.has(company.id));
  const availableCompanies = selectableCompanies
    .filter(company => !directSet.has(company.id) && matchesCompany(company, availableSearch))
    .slice(0, DISPLAY_LIMIT);
  const selectedCompanies = directCompanies
    .filter(company => matchesCompany(company, selectedSearch))
    .slice(0, DISPLAY_LIMIT);
  const effectiveCompanies = companies
    .filter(company => (inheritedSet.has(company.id) || directSet.has(company.id)) && matchesCompany(company, effectiveSearch))
    .slice(0, DISPLAY_LIMIT);
  const effectiveCount = new Set([...inheritedIds, ...directCompanies.map(company => company.id)]).size;

  function changeDirect(companiesToSelect: Company[]) {
    onDirectIdsChange(companiesToSelect.map(company => company.id));
  }

  function addFiltered() {
    onDirectIdsChange([...new Set([...directCompanies.map(company => company.id), ...availableCompanies.map(company => company.id)])]);
  }

  function removeFiltered() {
    const removed = new Set(selectedCompanies.map(company => company.id));
    onDirectIdsChange(directCompanies.filter(company => !removed.has(company.id)).map(company => company.id));
  }

  return (
    <Stack spacing={1.5}>
      <Autocomplete
        multiple
        filterSelectedOptions
        limitTags={3}
        disabled={disabled}
        options={selectableCompanies}
        value={directCompanies}
        getOptionLabel={company => company.name}
        isOptionEqualToValue={(option, value) => option.id === value.id}
        filterOptions={(options, state) => options
          .filter(company => matchesCompany(company, state.inputValue))
          .slice(0, DISPLAY_LIMIT)}
        onChange={(_, value) => changeDirect(value)}
        renderInput={params => <TextField {...params} label="Direct customer additions" helperText="Search by customer name or ID. Use direct access for exceptions; prefer customer groups for recurring scopes." />}
      />
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ alignItems: { sm: 'center' }, justifyContent: 'space-between' }}>
        <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}>
          <Chip size="small" variant="outlined" label={`${inheritedLabel}: ${inheritedSet.size}`} />
          <Chip size="small" variant="outlined" color={directCompanies.length ? 'primary' : 'default'} label={`Direct: ${directCompanies.length}`} />
          <Chip size="small" color={effectiveCount ? 'success' : 'default'} label={`Effective: ${effectiveCount}`} />
        </Stack>
        <Button size="small" variant="outlined" onClick={() => setManagerOpen(true)} disabled={disabled}>Manage selection</Button>
      </Stack>
      {directCompanies.length >= warningThreshold && <Alert severity="warning">This user has {directCompanies.length} direct customer additions. Consider creating or updating a customer group so this scope remains easier to govern.</Alert>}

      <Dialog open={managerOpen} onClose={() => setManagerOpen(false)} fullWidth maxWidth="lg">
        <DialogTitle>Manage direct customer additions</DialogTitle>
        <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
          <Alert severity="info">{inheritedSet.size ? `Customers inherited from ${inheritedLabel} are shown in effective access and cannot be duplicated as direct additions.` : 'No access group is selected. Every effective customer below comes from a direct addition.'}</Alert>
          <Grid container spacing={2}>
            <Grid size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Stack spacing={1.5}>
              <Stack direction="row" spacing={1} sx={{ alignItems: 'center', justifyContent: 'space-between' }}><Box><Typography variant="h6">Available customers</Typography><Typography variant="caption" color="text.secondary">Showing up to {DISPLAY_LIMIT} matches</Typography></Box><Button size="small" onClick={addFiltered} disabled={!availableCompanies.length}>Add filtered</Button></Stack>
              <TextField size="small" label="Search available customers" value={availableSearch} onChange={event => setAvailableSearch(event.target.value)} />
              <Divider />
              <Stack divider={<Divider flexItem />} sx={{ maxHeight: 300, overflow: 'auto' }}>{availableCompanies.map(company => <Stack key={company.id} direction="row" spacing={1} sx={{ alignItems: 'center', justifyContent: 'space-between', py: 1 }}><Box><Typography>{company.name}</Typography><Typography variant="caption" color="text.secondary">{company.id}</Typography></Box><Button size="small" onClick={() => onDirectIdsChange([...directCompanies.map(item => item.id), company.id])}>Add</Button></Stack>)}{!availableCompanies.length && <Typography color="text.secondary" sx={{ py: 2 }}>No available customers match this search.</Typography>}</Stack>
            </Stack></Paper></Grid>
            <Grid size={{ xs: 12, md: 6 }}><Paper variant="outlined" sx={{ p: 2, height: '100%' }}><Stack spacing={1.5}>
              <Stack direction="row" spacing={1} sx={{ alignItems: 'center', justifyContent: 'space-between' }}><Box><Typography variant="h6">Direct additions</Typography><Typography variant="caption" color="text.secondary">{directCompanies.length} selected</Typography></Box><Button size="small" color="warning" onClick={removeFiltered} disabled={!selectedCompanies.length}>Remove filtered</Button></Stack>
              <TextField size="small" label="Search direct additions" value={selectedSearch} onChange={event => setSelectedSearch(event.target.value)} />
              <Divider />
              <Stack divider={<Divider flexItem />} sx={{ maxHeight: 300, overflow: 'auto' }}>{selectedCompanies.map(company => <Stack key={company.id} direction="row" spacing={1} sx={{ alignItems: 'center', justifyContent: 'space-between', py: 1 }}><Box><Typography>{company.name}</Typography><Typography variant="caption" color="text.secondary">{company.id}</Typography></Box><Button size="small" color="warning" onClick={() => onDirectIdsChange(directCompanies.filter(item => item.id !== company.id).map(item => item.id))}>Remove</Button></Stack>)}{!selectedCompanies.length && <Typography color="text.secondary" sx={{ py: 2 }}>No direct customer additions match this search.</Typography>}</Stack>
            </Stack></Paper></Grid>
          </Grid>
          <Paper variant="outlined" sx={{ p: 2 }}><Stack spacing={1.5}>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ alignItems: { sm: 'center' }, justifyContent: 'space-between' }}><Box><Typography variant="h6">Effective customer access</Typography><Typography variant="caption" color="text.secondary">{effectiveCount} unique customers from group and direct assignments</Typography></Box><TextField size="small" label="Search effective access" value={effectiveSearch} onChange={event => setEffectiveSearch(event.target.value)} /></Stack>
            <Divider />
            <Grid container spacing={1}>{effectiveCompanies.map(company => <Grid key={company.id} size={{ xs: 12, sm: 6, md: 4 }}><Stack direction="row" spacing={1} sx={{ alignItems: 'center', justifyContent: 'space-between' }}><Typography variant="body2" noWrap title={company.name}>{company.name}</Typography><Chip size="small" variant="outlined" color={inheritedSet.has(company.id) ? 'default' : 'primary'} label={inheritedSet.has(company.id) && directSet.has(company.id) ? 'Group + direct' : inheritedSet.has(company.id) ? 'Group' : 'Direct'} /></Stack></Grid>)}{!effectiveCompanies.length && <Grid size={{ xs: 12 }}><Typography color="text.secondary">This user currently has no effective customer access.</Typography></Grid>}</Grid>
          </Stack></Paper>
        </Stack></DialogContent>
        <DialogActions><Button onClick={() => setManagerOpen(false)} variant="contained">Done</Button></DialogActions>
      </Dialog>
    </Stack>
  );
}
