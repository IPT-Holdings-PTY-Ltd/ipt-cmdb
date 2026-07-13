import { Box, Chip, Grid, Typography } from '@mui/material';
import {
  Create,
  Datagrid,
  DateInput,
  Edit,
  EditButton,
  FunctionField,
  List,
  SearchInput,
  SelectInput,
  Show,
  SimpleForm,
  SimpleShowLayout,
  TextField,
  TextInput,
  useRecordContext,
} from 'react-admin';
import type { Asset } from './types';
import { useWorkspace } from './workspace';

const types = ['Device', 'Server', 'Workstation', 'Network device', 'Software', 'Licence', 'Service', 'Credential owner'].map(id => ({ id, name: id }));
const statuses = ['Active', 'Planned', 'Retired'].map(id => ({ id, name: id }));
const lifecycle = ['planned', 'ordered', 'received', 'in_stock', 'in_service', 'maintenance', 'retired', 'disposed'].map(id => ({ id, name: id.replaceAll('_', ' ') }));
const operational = ['unknown', 'healthy', 'warning', 'critical', 'offline'].map(id => ({ id, name: id }));
const criticality = ['low', 'medium', 'high', 'critical'].map(id => ({ id, name: id }));
const environments = ['production', 'pre_production', 'test', 'development', 'disaster_recovery', 'other'].map(id => ({ id, name: id.replaceAll('_', ' ') }));

const filters = [<SearchInput key="q" source="q" alwaysOn />, <SelectInput key="type" source="type" choices={types} />, <SelectInput key="lifecycle" source="lifecycle" choices={lifecycle} />];

function CompanyName() {
  const record = useRecordContext<Asset>();
  const { companies } = useWorkspace();
  return <Typography variant="body2">{companies.find(company => company.id === record?.companyId)?.name || record?.companyId}</Typography>;
}

export function AssetList() {
  return <List filters={filters} sort={{ field: 'name', order: 'ASC' }} perPage={25} title="Configuration items">
    <Datagrid rowClick="show" bulkActionButtons={false}>
      <TextField source="name" />
      <CompanyName />
      <TextField source="type" />
      <FunctionField label="Lifecycle" render={(record: Asset) => <Chip size="small" label={(record.metadata?.lifecycle || 'unknown').replaceAll('_', ' ')} />} />
      <FunctionField label="Health" render={(record: Asset) => <Chip size="small" color={record.metadata?.operationalStatus === 'healthy' ? 'success' : record.metadata?.operationalStatus === 'critical' || record.metadata?.operationalStatus === 'offline' ? 'error' : 'default'} label={record.metadata?.operationalStatus || 'unknown'} />} />
      <FunctionField label="Owner" render={(record: Asset) => record.metadata?.technicalOwner || record.metadata?.serviceOwner || 'Unassigned'} />
      <TextField source="source" />
      <EditButton />
    </Datagrid>
  </List>;
}

function AssetForm() {
  return <SimpleForm defaultValues={{ status: 'Active', type: 'Device', metadata: { lifecycle: 'in_service', operationalStatus: 'healthy', criticality: 'medium', environment: 'production' } }}>
    <Typography variant="h6" className="form-section">Identity and classification</Typography>
    <Grid container spacing={2} width="100%">
      <Grid size={{ xs: 12, md: 6 }}><TextInput source="name" fullWidth required /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><SelectInput source="type" choices={types} fullWidth required /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><SelectInput source="status" choices={statuses} fullWidth required /></Grid>
    </Grid>
    <Typography variant="h6" className="form-section">Lifecycle and service health</Typography>
    <Grid container spacing={2} width="100%">
      <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.lifecycle" choices={lifecycle} fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.operationalStatus" label="Operational status" choices={operational} fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.criticality" choices={criticality} fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><SelectInput source="metadata.environment" choices={environments} fullWidth /></Grid>
    </Grid>
    <Typography variant="h6" className="form-section">Ownership and location</Typography>
    <Grid container spacing={2} width="100%">
      <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.technicalOwner" label="Technical owner" fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.serviceOwner" label="Service owner" fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 4 }}><TextInput source="metadata.custodian" fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 6 }}><TextInput source="metadata.site" fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><TextInput source="metadata.vendor" fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><TextInput source="metadata.model" fullWidth /></Grid>
    </Grid>
    <Typography variant="h6" className="form-section">Commercial and review dates</Typography>
    <Grid container spacing={2} width="100%">
      <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.purchaseDate" label="Purchase date" fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.warrantyEnd" label="Warranty end" fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.renewalDate" label="Renewal date" fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.endOfLifeDate" label="End-of-life date" fullWidth /></Grid>
      <Grid size={{ xs: 12, md: 3 }}><DateInput source="metadata.reviewDate" label="Review date" fullWidth /></Grid>
    </Grid>
  </SimpleForm>;
}

export function AssetCreate() { return <Create redirect="list"><AssetForm /></Create>; }
export function AssetEdit() { return <Edit mutationMode="pessimistic"><AssetForm /></Edit>; }
export function AssetShow() {
  return <Show><SimpleShowLayout>
    <TextField source="name" />
    <TextField source="type" />
    <TextField source="status" />
    <TextField source="metadata.lifecycle" label="Lifecycle" />
    <TextField source="metadata.operationalStatus" label="Operational status" />
    <TextField source="metadata.criticality" label="Criticality" />
    <TextField source="metadata.technicalOwner" label="Technical owner" emptyText="Unassigned" />
    <TextField source="metadata.site" label="Site" emptyText="Not recorded" />
    <TextField source="source" />
    <TextField source="lastSeen" label="Last seen" />
  </SimpleShowLayout></Show>;
}
