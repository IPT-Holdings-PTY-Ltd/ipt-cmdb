import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import {
  NcentralCapabilityCheck,
  NcentralDeviceWorkflowReadiness,
  NcentralEnrichmentDiagnosticsWorkbench,
  NcentralGraphqlConnectionPanel,
  NcentralGraphqlCustomerEnrichmentPanel,
  NcentralMissingDeviceLifecycleSummary,
  NcentralPreviewProgressPanel,
  ncentralRestDetailEnrichmentMessage,
  ncentralWizardContinueDisabled,
  syncNcentralSharedRevision,
  withNcentralLifecyclePolicyDefaults,
  type NcentralCapabilityRequest,
  type NcentralCapabilityResult,
  type NcentralEnrichmentDiagnostics,
  type NcentralEnrichmentDiagnosticsRequest,
  type NcentralGraphqlConfigInput,
  type NcentralGraphqlPreviewRequest,
  type NcentralGraphqlServerDetection,
} from './NcentralIntegration';
import type {
  ConnectWiseCiPolicy,
  MissingDeviceLifecycleQueue,
  NcentralGraphqlConfig,
  NcentralGraphqlPreview,
  NcentralGraphqlTestResult,
  NcentralPreviewRun,
} from './types';

type DetectGraphqlServer = (
  request: Pick<NcentralGraphqlPreviewRequest, 'companyId' | 'providerCompanyId' | 'limit'>,
) => Promise<NcentralGraphqlServerDetection>;

function previewRun(
  changes: Partial<NcentralPreviewRun> = {},
): NcentralPreviewRun {
  return {
    id: 'run-1',
    companyId: 'acme',
    providerCompanyId: '101',
    policyId: 'policy-1',
    policyRevision: 2,
    status: 'running',
    phase: 'enriching_devices',
    progress: {
      current: 10,
      total: 25,
      percent: 40,
      discovered: 125,
      enriched: 10,
      reviewed: 0,
    },
    message: 'Enriching device evidence.',
    canCancel: true,
    canRetry: false,
    cancelRequested: false,
    ...changes,
  };
}

describe('N-central preview progress', () => {
  it('shows live progress and exposes cancellation without blocking the page', async () => {
    const cancel = vi.fn<() => void>();
    render(<NcentralPreviewProgressPanel
      run={previewRun()}
      busy={false}
      onCancel={cancel}
      onRetry={() => undefined}
    />);

    expect(screen.getByRole('progressbar', { name: 'N-central preview progress' }))
      .toHaveAttribute('aria-valuenow', '40');
    expect(screen.getByText('125 discovered')).toBeVisible();
    expect(screen.getByText('10 enriched')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
    expect(cancel).toHaveBeenCalledOnce();
  });

  it('stops cancellation and offers retry after a sanitized failure', async () => {
    const retry = vi.fn<() => void>();
    render(<NcentralPreviewProgressPanel
      run={previewRun({
        status: 'failed',
        phase: 'failed',
        message: 'Preview failed.',
        error: 'N-central returned a temporary provider error.',
        canCancel: false,
        canRetry: true,
      })}
      busy={false}
      onCancel={() => undefined}
      onRetry={retry}
    />);

    expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('temporary provider error');
    await userEvent.click(screen.getByRole('button', { name: 'Retry run' }));
    expect(retry).toHaveBeenCalledOnce();
  });
});

function capabilityResult(): NcentralCapabilityResult {
  return {
    provider: 'ncentral',
    readOnly: true,
    requestedDevice: false,
    sampleCount: 1,
    maximumSampleCount: 3,
    samples: [{
      sample: 1,
      endpoints: {
        assets: {
          accessStatus: 'available',
          httpStatus: 200,
          shape: {
            type: 'object',
            count: 2,
            fields: [
              { name: 'computerSystem', type: 'object' },
              { name: 'networkAdapter', type: 'object' },
            ],
            sections: [{ name: 'networkAdapter', type: 'object', count: 4 }],
          },
        },
        custom_properties: {
          accessStatus: 'forbidden',
          httpStatus: 403,
        },
      },
    }],
    organizationEndpoints: {
      active_issues: {
        accessStatus: 'available',
        httpStatus: 200,
        shape: { type: 'object', count: 1, fields: [{ name: 'data', type: 'array' }] },
      },
    },
  };
}

describe('N-central inventory capability check', () => {
  it('submits a bounded read-only request and renders shape-only evidence', async () => {
    const probe = vi.fn<
      (request: NcentralCapabilityRequest) => Promise<NcentralCapabilityResult>
    >(async () => capabilityResult());
    render(<NcentralCapabilityCheck
      companyId="company-1"
      providerCompanyId="101"
      providerCompanyName="Northwind"
      probe={probe}
    />);

    expect(screen.getByText('No writes')).toBeInTheDocument();
    expect(screen.getByText('No provider values')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Check capabilities').closest('button')!);

    await waitFor(() => expect(probe).toHaveBeenCalledWith({
      companyId: 'company-1',
      providerCompanyId: '101',
      externalId: '',
      sampleLimit: 1,
    }));
    expect(await screen.findByText('Technical asset inventory')).toBeInTheDocument();
    expect(screen.getByText('Custom-property schema')).toBeInTheDocument();
    expect(screen.getByText('Fields: computerSystem (object), networkAdapter (object)'))
      .toBeInTheDocument();
    expect(screen.getByText('Sections: networkAdapter (4)')).toBeInTheDocument();
    expect(screen.queryByText(/SECRET/)).not.toBeInTheDocument();
  });

  it('rejects a non-numeric immutable device identity before making a request', async () => {
    const probe = vi.fn<
      (request: NcentralCapabilityRequest) => Promise<NcentralCapabilityResult>
    >(async () => capabilityResult());
    render(<NcentralCapabilityCheck
      companyId="company-1"
      providerCompanyId="101"
      probe={probe}
    />);

    fireEvent.change(screen.getByLabelText('Device ID (optional)'), {
      target: { value: 'device-42' },
    });
    expect(screen.getByText('Use the immutable numeric N-central device ID.')).toBeInTheDocument();
    expect(screen.getByText('Check capabilities').closest('button')).toBeDisabled();
    expect(probe).not.toHaveBeenCalled();
  });
});

function graphqlConfig(
  changes: Partial<NcentralGraphqlConfig> = {},
): NcentralGraphqlConfig {
  return {
    graphqlEnabled: true,
    graphqlEndpoint: 'https://api.n-able.com/graphql',
    graphqlPageSize: 25,
    graphqlServerId: 'server-za-1',
    configured: true,
    credentialSource: 'encrypted_database',
    managedByEnvironment: false,
    revision: 4,
    ...changes,
  };
}

function graphqlTestResult(): NcentralGraphqlTestResult {
  return {
    reachable: true,
    candidateCount: 12,
    truncated: true,
    customerCandidates: [
      { id: 'customer-immutable-101', name: 'Northwind', typeName: 'Customer' },
    ],
    credentialSource: 'encrypted_database',
    readOnly: true,
    writesAttempted: false,
  };
}

function graphqlServerDetection(
  changes: Partial<NcentralGraphqlServerDetection> = {},
): NcentralGraphqlServerDetection {
  return {
    companyId: 'cmdb-company-1',
    providerCompanyId: 'rest-customer-42',
    candidates: [{
      serverId: 'server-za-1',
      graphqlDeviceCount: 9,
      restDeviceMatchCount: 7,
    }],
    recommendedServerId: 'server-za-1',
    confidence: 'exact',
    truncated: false,
    readOnly: true,
    writesAttempted: false,
    ...changes,
  };
}

describe('N-central shared connection revision', () => {
  it('propagates either transport save revision to its sibling editor', () => {
    const restEditor = { channel: 'rest', revision: 4 };
    const graphqlEditor = { channel: 'graphql', revision: 4 };

    expect(syncNcentralSharedRevision(graphqlEditor, 5)).toEqual({
      channel: 'graphql',
      revision: 5,
    });
    expect(syncNcentralSharedRevision(restEditor, 6)).toEqual({
      channel: 'rest',
      revision: 6,
    });
    expect(syncNcentralSharedRevision(restEditor, undefined)).toBe(restEditor);
    expect(syncNcentralSharedRevision(null, 7)).toBeNull();
  });
});

describe('N-central optional GraphQL connection', () => {
  it('keeps REST and GraphQL capabilities distinct and clears a replacement token after save', async () => {
    const saveConfig = vi.fn<
      (input: NcentralGraphqlConfigInput) => Promise<NcentralGraphqlConfig>
    >(async () => graphqlConfig({ revision: 5 }));
    render(<NcentralGraphqlConnectionPanel
      config={graphqlConfig()}
      restReady
      isAdmin
      saveConfig={saveConfig}
      onConfigChange={() => undefined}
    />);

    expect(screen.getByText('REST · core ready')).toBeVisible();
    expect(screen.getByText('GraphQL · optional ready')).toBeVisible();
    expect(screen.getByText('Patch installations · unavailable')).toBeVisible();

    const token = screen.getByLabelText('Replace GraphQL API token (optional)');
    await userEvent.type(token, 'replacement-token-value');
    await userEvent.click(screen.getByRole('button', { name: 'Save GraphQL settings' }));

    await waitFor(() => expect(saveConfig).toHaveBeenCalledWith({
      graphqlEnabled: true,
      graphqlEndpoint: 'https://api.n-able.com/graphql',
      graphqlApiToken: 'replacement-token-value',
      graphqlPageSize: 25,
      graphqlServerId: 'server-za-1',
      expectedRevision: 4,
    }));
    await waitFor(() => expect(token).toHaveValue(''));
    expect(screen.queryByText('replacement-token-value')).not.toBeInTheDocument();
    expect(screen.getByText(/token is encrypted and was not returned/i)).toBeVisible();
  });

  it('shows saved credentials as ready when the response credential source is authoritative', async () => {
    const saveConfig = vi.fn<
      (input: NcentralGraphqlConfigInput) => Promise<NcentralGraphqlConfig>
    >(async input => graphqlConfig({
      graphqlEnabled: input.graphqlEnabled,
      configured: false,
      hasCredentials: undefined,
      credentialSource: 'encrypted_database',
      revision: 5,
    }));

    function StatefulPanel() {
      const [config, setConfig] = useState(graphqlConfig({
        graphqlEnabled: false,
        configured: false,
        hasCredentials: false,
        credentialSource: 'not_configured',
      }));
      return <NcentralGraphqlConnectionPanel
        config={config}
        restReady
        isAdmin
        saveConfig={saveConfig}
        onConfigChange={setConfig}
      />;
    }

    render(<StatefulPanel />);
    expect(screen.getByText('GraphQL · not configured')).toBeVisible();

    await userEvent.click(screen.getByRole('checkbox', {
      name: 'Enable optional GraphQL enrichment',
    }));
    const token = screen.getByLabelText(/GraphQL API token/);
    await userEvent.type(token, 'write-only-token-value');
    await userEvent.click(screen.getByRole('button', { name: 'Save GraphQL settings' }));

    await waitFor(() => expect(screen.getByText('GraphQL · optional ready')).toBeVisible());
    expect(screen.getByLabelText('Replace GraphQL API token (optional)')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Test GraphQL access' })).toBeEnabled();
    expect(screen.queryByText('write-only-token-value')).not.toBeInTheDocument();
  });

  it('distinguishes a saved credential from disabled enrichment and a missing server ID', () => {
    const { rerender } = render(<NcentralGraphqlConnectionPanel
      config={graphqlConfig({
        graphqlEnabled: false,
        graphqlServerId: '',
        configured: true,
        credentialSource: 'encrypted_database',
      })}
      restReady
      isAdmin
      onConfigChange={() => undefined}
    />);

    expect(screen.getByText('GraphQL · credentials saved · disabled')).toBeVisible();
    expect(screen.getByText(/The GraphQL credential is saved/i)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Test GraphQL access' })).toBeEnabled();
    expect(screen.getByLabelText('Replace GraphQL API token (optional)')).toBeInTheDocument();

    rerender(<NcentralGraphqlConnectionPanel
      config={graphqlConfig({
        graphqlEnabled: true,
        graphqlServerId: '',
        configured: true,
        credentialSource: 'encrypted_database',
      })}
      restReady
      isAdmin
      onConfigChange={() => undefined}
    />);

    expect(screen.getByText('GraphQL · server identity setup')).toBeVisible();
    expect(screen.getByText(/Server identity is completed in Devices & reconciliation/i)).toBeVisible();
    expect(screen.queryByLabelText('Source server ID')).not.toBeInTheDocument();
  });

  it('runs a bounded Customer catalogue test without reading asset values', async () => {
    const testConnection = vi.fn<() => Promise<NcentralGraphqlTestResult>>(
      async () => graphqlTestResult(),
    );
    const onTestResult = vi.fn<(result: NcentralGraphqlTestResult) => void>();
    render(<NcentralGraphqlConnectionPanel
      config={graphqlConfig()}
      restReady
      isAdmin
      testConnection={testConnection}
      onConfigChange={() => undefined}
      onTestResult={onTestResult}
    />);

    await userEvent.click(screen.getByRole('button', { name: 'Test GraphQL access' }));

    await waitFor(() => expect(testConnection).toHaveBeenCalledOnce());
    expect(onTestResult).toHaveBeenCalledWith(graphqlTestResult());
    expect(screen.getByText(/12 Customer candidates were returned in a bounded result/i)).toBeVisible();
    expect(screen.getByText(/No asset query or provider write was attempted/i)).toBeVisible();
    expect(screen.getByText(/does not enable GraphQL enrichment or refresh the asset cache/i))
      .toBeVisible();
  });

  it('does not offer the token-wide Customer catalogue test to an MSP operator', () => {
    const testConnection = vi.fn<() => Promise<NcentralGraphqlTestResult>>(
      async () => graphqlTestResult(),
    );
    render(<NcentralGraphqlConnectionPanel
      config={graphqlConfig()}
      restReady
      isAdmin={false}
      testConnection={testConnection}
      onConfigChange={() => undefined}
    />);

    expect(screen.getByRole('button', { name: 'Test GraphQL access' })).toBeDisabled();
    expect(testConnection).not.toHaveBeenCalled();
  });

  it('marks an expired capability stale and surfaces deployment-safe configuration errors', () => {
    render(<NcentralGraphqlConnectionPanel
      config={graphqlConfig({
        managedByEnvironment: true,
        connectionStatus: 'error',
        lastError: 'The mounted GraphQL token file cannot be read.',
        capability: { reachable: true, status: 'supported', stale: true },
      })}
      restReady
      isAdmin
      onConfigChange={() => undefined}
    />);

    expect(screen.getByText('GraphQL auth · retest required')).toBeVisible();
    expect(screen.getByText('Customer catalogue · stale')).toBeVisible();
    expect(screen.getByText('The mounted GraphQL token file cannot be read.')).toBeVisible();
    expect(screen.queryByText('GraphQL auth · available')).not.toBeInTheDocument();
  });
});

function graphqlPreview(
  changes: Partial<NcentralGraphqlPreview> = {},
): NcentralGraphqlPreview {
  return {
    companyId: 'cmdb-company-1',
    providerCompanyId: 'rest-customer-42',
    queryKey: 'asset_inventory',
    organizationIds: ['customer-immutable-101'],
    totalCount: 48,
    items: [
      {
        graphqlAssetId: 'graphql-asset-1',
        name: 'SERVER-01',
        customer: { id: 'customer-immutable-101', name: 'Northwind' },
        site: { id: 'site-1', name: 'Johannesburg' },
        serviceOrganization: null,
        sourceIdentity: {
          provider: 'ncentral',
          namespace: 'nable_graphql_asset',
          externalId: 'graphql-asset-1',
        },
        restIdentity: {
          provider: 'ncentral',
          namespace: 'ncentral_rest_device',
          serverId: 'server-za-1',
          deviceId: '1001',
          crosswalkKey: 'server-za-1:1001',
        },
        summary: {
          system: { manufacturer: 'Dell', model: 'PowerEdge' },
          operatingSystem: { name: 'Windows Server', version: '2022' },
        },
      },
      {
        graphqlAssetId: 'graphql-asset-2',
        name: 'LAPTOP-02',
        customer: { id: 'customer-immutable-101', name: 'Northwind' },
        site: null,
        serviceOrganization: null,
        sourceIdentity: {
          provider: 'ncentral',
          namespace: 'nable_graphql_asset',
          externalId: 'graphql-asset-2',
        },
        restIdentity: null,
        summary: {
          system: { manufacturer: 'Lenovo', model: 'ThinkPad' },
          agent: { status: 'CONNECTED' },
        },
      },
    ],
    truncated: true,
    credentialSource: 'encrypted_database',
    cache: {
      status: 'fresh',
      deviceCount: 2,
      unmatchedDeviceCount: 0,
      lastRefreshedAt: '2026-07-31T15:00:00Z',
      expiresAt: '2026-07-31T15:15:00Z',
      message: 'Showing current cached asset enrichment.',
    },
    ...changes,
  };
}

describe('N-central Customer-only GraphQL enrichment', () => {
  it('persists explicit Customer IDs before a bounded rich-inventory preview', async () => {
    const persistScope = vi.fn<() => Promise<boolean>>(async () => true);
    const previewGraphql = vi.fn<
      (request: NcentralGraphqlPreviewRequest) => Promise<NcentralGraphqlPreview>
    >(async () => graphqlPreview());
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig()}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={persistScope}
      previewGraphql={previewGraphql}
    />);

    expect(screen.getByText(/Customer-only guard/i)).toHaveTextContent(
      /MSP-wide, all-customer, and name-inferred queries are not available/i,
    );
    expect(screen.getByRole('combobox', { name: 'GraphQL Customer IDs' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Preview cached sample' }));

    await waitFor(() => expect(persistScope).toHaveBeenCalledOnce());
    expect(previewGraphql).toHaveBeenCalledWith({
      companyId: 'cmdb-company-1',
      providerCompanyId: 'rest-customer-42',
      queryKey: 'asset_inventory',
      limit: 25,
    });
    expect(await screen.findByText('Rich-inventory preview summary')).toBeInTheDocument();
    expect(screen.getByText('48')).toBeInTheDocument();
    expect(screen.getAllByText('2', { selector: 'h6' })).toHaveLength(2);
    expect(screen.getByText('System · 2/2')).toBeInTheDocument();
    expect(screen.getByText(/This is a bounded sample/i)).toBeInTheDocument();
    expect(screen.getAllByText('Fresh').length).toBeGreaterThan(0);
  });

  it('keeps the bounded preview size separate from the full-scope cache refresh', async () => {
    const persistScope = vi.fn<() => Promise<boolean>>(async () => true);
    const previewGraphql = vi.fn<
      (request: NcentralGraphqlPreviewRequest) => Promise<NcentralGraphqlPreview>
    >(async () => graphqlPreview());
    const refreshGraphql = vi.fn<
      (request: NcentralGraphqlPreviewRequest) => Promise<NcentralGraphqlPreview>
    >(async () => graphqlPreview({ truncated: false }));
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig()}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={persistScope}
      previewGraphql={previewGraphql}
      refreshGraphql={refreshGraphql}
    />);

    const sampleSize = screen.getByLabelText('Sample size');
    expect(screen.getByRole('region', { name: 'Bounded GraphQL sample' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Full-scope GraphQL cache' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'GraphQL cache state' })).toBeVisible();
    await userEvent.clear(sampleSize);
    await userEvent.type(sampleSize, '7');
    await userEvent.click(screen.getByRole('button', { name: 'Preview cached sample' }));
    await waitFor(() => expect(previewGraphql).toHaveBeenCalledWith({
      companyId: 'cmdb-company-1',
      providerCompanyId: 'rest-customer-42',
      queryKey: 'asset_inventory',
      limit: 7,
    }));

    await userEvent.click(screen.getByRole('button', { name: 'Refresh full-scope cache' }));
    await waitFor(() => expect(refreshGraphql).toHaveBeenCalledWith({
      companyId: 'cmdb-company-1',
      providerCompanyId: 'rest-customer-42',
      queryKey: 'asset_inventory',
      limit: 500,
    }));
    expect(persistScope).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/independent of the sample size/i)).toBeVisible();
  });

  it('does not report success when a complete cache generation was not published', async () => {
    const cacheMessage = 'The provider safety limit was reached; the current published cache was retained.';
    const refreshGraphql = vi.fn<
      (request: NcentralGraphqlPreviewRequest) => Promise<NcentralGraphqlPreview>
    >(async () => graphqlPreview({
      refreshed: false,
      truncated: true,
      cache: {
        status: 'partial',
        providerAssetCount: 10_000,
        eligibleDeviceCount: 0,
        deviceCount: 0,
        unmatchedDeviceCount: 0,
        pagesRead: 100,
        complete: false,
        message: cacheMessage,
      },
    }));
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig()}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={async () => true}
      refreshGraphql={refreshGraphql}
    />);

    await userEvent.click(screen.getByRole('button', { name: 'Refresh full-scope cache' }));
    await waitFor(() => expect(refreshGraphql).toHaveBeenCalledOnce());
    const messages = await screen.findAllByText(cacheMessage);
    expect(messages.some(message => message.closest('[role="alert"]')
      ?.classList.contains('MuiAlert-colorWarning'))).toBe(true);
    expect(screen.queryByText(/Full-scope GraphQL cache refresh completed/i))
      .not.toBeInTheDocument();
    expect(screen.getByText('Provider assets · 10000')).toBeVisible();
    expect(screen.getByText('Cached · 0')).toBeVisible();
    expect(screen.getByText('Pages read · 100')).toBeVisible();
    expect(screen.getByText('Partial scope')).toBeVisible();
  });

  it('makes empty and stale cache states explicit and renders optional coverage counts', () => {
    const common = {
      companyId: 'cmdb-company-1',
      providerCompanyId: 'rest-customer-42',
      providerCompanyName: 'Northwind',
      organizationIds: ['customer-immutable-101'],
      customerCandidates: graphqlTestResult().customerCandidates,
      isAdmin: true,
      onOrganizationIdsChange: () => undefined,
      persistScope: async () => true,
    };
    const { rerender } = render(<NcentralGraphqlCustomerEnrichmentPanel
      {...common}
      config={graphqlConfig({
        cache: {
          status: 'empty',
          deviceCount: 0,
          message: 'No eligible cached asset enrichment is available.',
        },
      })}
    />);

    expect(screen.getByText(/has no cached rich inventory/i)).toBeVisible();
    expect(screen.getByText('Cached · 0')).toBeVisible();
    expect(screen.getByText('No eligible cached asset enrichment is available.')).toBeVisible();

    rerender(<NcentralGraphqlCustomerEnrichmentPanel
      {...common}
      config={graphqlConfig({
        cache: {
          status: 'stale',
          deviceCount: 42,
          unmatchedDeviceCount: 6,
          providerAssetCount: 50,
          pagesRead: 3,
          complete: false,
          lastRefreshedAt: '2026-07-31T15:00:00Z',
          expiresAt: '2026-07-31T15:15:00Z',
        },
      })}
    />);

    expect(screen.getByText(/GraphQL cache is stale/i)).toBeVisible();
    expect(screen.getByText('Provider assets · 50')).toBeVisible();
    expect(screen.getByText('Cached · 42')).toBeVisible();
    expect(screen.getByText('Unmatched · 6')).toBeVisible();
    expect(screen.getByText('Pages read · 3')).toBeVisible();
    expect(screen.getByText('Partial scope')).toBeVisible();
  });

  it('prefers persisted customer diagnostics over an empty global cache snapshot', () => {
    const scopedDiagnostics = freshEnrichmentDiagnostics();
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig({
        cache: {
          status: 'empty',
          deviceCount: 0,
          message: 'No eligible cached asset enrichment is available.',
        },
      })}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      scopedDiagnostics={scopedDiagnostics}
      onOrganizationIdsChange={() => undefined}
      persistScope={async () => true}
    />);

    expect(screen.getByText('Cache · Fresh')).toBeInTheDocument();
    expect(screen.getByText('Provider assets · 100')).toBeInTheDocument();
    expect(screen.getByText('Cached · 91')).toBeInTheDocument();
    expect(screen.getByText('The current complete customer cache is ready for enrichment.'))
      .toBeInTheDocument();
    expect(screen.queryByText('Cache · Empty')).not.toBeInTheDocument();
    expect(screen.queryByText(/has no cached rich inventory/i)).not.toBeInTheDocument();
  });

  it('lets a new preview result supersede persisted customer diagnostics', async () => {
    const previewGraphql = vi.fn<
      (request: NcentralGraphqlPreviewRequest) => Promise<NcentralGraphqlPreview>
    >(async () => graphqlPreview({
      cache: {
        status: 'stale',
        deviceCount: 3,
        providerAssetCount: 4,
        unmatchedDeviceCount: 1,
        message: 'The bounded preview observed an expired cache generation.',
      },
    }));
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig({ cache: { status: 'empty', deviceCount: 0 } })}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      scopedDiagnostics={freshEnrichmentDiagnostics()}
      onOrganizationIdsChange={() => undefined}
      persistScope={async () => true}
      previewGraphql={previewGraphql}
    />);

    expect(screen.getByText('Cached · 91')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Preview cached sample' }));
    await waitFor(() => expect(previewGraphql).toHaveBeenCalledOnce());
    expect(screen.getByText('Cache · Stale')).toBeInTheDocument();
    expect(screen.getByText('Cached · 3')).toBeInTheDocument();
    expect(screen.queryByText('Cached · 91')).not.toBeInTheDocument();
  });

  it('offers tested Customer candidates but never auto-selects by REST name', async () => {
    const onOrganizationIdsChange = vi.fn<(ids: string[]) => void>();
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig()}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={[]}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={onOrganizationIdsChange}
      persistScope={async () => true}
    />);

    const customerInput = screen.getByRole('combobox', { name: 'GraphQL Customer IDs' });
    expect(customerInput).toHaveValue('');
    const previewButton = screen.getByRole('button', { name: 'Preview cached sample' });
    expect(previewButton).toBeDisabled();
    await userEvent.click(customerInput);
    await userEvent.type(customerInput, 'Northwind');
    await userEvent.click(await screen.findByText('Northwind · customer-immutable-101'));
    expect(onOrganizationIdsChange).toHaveBeenCalledWith(['customer-immutable-101']);
  });

  it('distinguishes exact eligible crosswalks from identities on another server', async () => {
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig({ graphqlServerId: 'different-server' })}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={async () => true}
      previewGraphql={async () => graphqlPreview()}
    />);

    await userEvent.click(screen.getByRole('button', { name: 'Preview cached sample' }));
    expect(await screen.findByText(/does not match any returned REST crosswalk/i)).toBeInTheDocument();
    expect(screen.getByText(/Observed IDs: server-za-1/i)).toBeInTheDocument();
    expect(screen.getByText('Eligible REST crosswalks')).toBeInTheDocument();
  });

  it('detects one corroborated server, prefills it, and waits for explicit save', async () => {
    const persistScope = vi.fn<() => Promise<boolean>>(async () => true);
    const detectServer = vi.fn<DetectGraphqlServer>(async () => graphqlServerDetection());
    const saveConfig = vi.fn<
      (input: NcentralGraphqlConfigInput) => Promise<NcentralGraphqlConfig>
    >(async () => graphqlConfig({ revision: 5 }));
    const onConfigChange = vi.fn<(config: NcentralGraphqlConfig) => void>();
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig({ graphqlServerId: '' })}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={persistScope}
      detectServer={detectServer}
      saveConfig={saveConfig}
      onConfigChange={onConfigChange}
    />);

    const previewButton = screen.getByText('Preview cached sample').closest('button');
    expect(previewButton).toBeDisabled();
    const detectButton = screen.getByText('Detect server identity').closest('button');
    await userEvent.click(detectButton!);

    await waitFor(() => expect(detectServer).toHaveBeenCalledWith({
      companyId: 'cmdb-company-1',
      providerCompanyId: 'rest-customer-42',
      limit: 25,
    }));
    expect(persistScope).toHaveBeenCalledOnce();
    expect(saveConfig).not.toHaveBeenCalled();
    expect(screen.getByText('server-za-1'))
      .toBeInTheDocument();
    expect(screen.getByText(/Corroborated by 7 exact REST device ID matches across 9 GraphQL devices/i))
      .toBeInTheDocument();
    expect(screen.getByText(/Detected server-za-1 from 7 exact GraphQL-to-REST device matches/i))
      .toBeInTheDocument();
    expect(previewButton).toBeDisabled();

    const saveServerButton = screen.getByText('Save server identity').closest('button');
    expect(saveServerButton).toBeEnabled();
    await userEvent.click(saveServerButton!);
    await waitFor(() => expect(saveConfig).toHaveBeenCalledWith({
      graphqlEnabled: true,
      graphqlEndpoint: 'https://api.n-able.com/graphql',
      graphqlApiToken: '',
      graphqlPageSize: 25,
      graphqlServerId: 'server-za-1',
      expectedRevision: 4,
    }));
    expect(onConfigChange).toHaveBeenCalledWith(graphqlConfig({ revision: 5 }));
  });

  it('allows identity detection with a saved credential while enrichment is disabled', async () => {
    const detectServer = vi.fn<DetectGraphqlServer>(async () => graphqlServerDetection());
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig({ graphqlEnabled: false, graphqlServerId: '' })}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={async () => true}
      detectServer={detectServer}
    />);

    const detectButton = screen.getByText('Detect server identity').closest('button');
    expect(detectButton).toBeEnabled();
    expect(screen.getByText('Preview cached sample').closest('button')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Refresh full-scope cache' })).toBeDisabled();
    expect(screen.getByText(/credentials are saved, but enrichment is disabled/i)).toBeVisible();
    expect(screen.getByText(/successful connection test or server-identity detection does not enable it/i))
      .toBeVisible();
    await userEvent.click(detectButton!);
    await waitFor(() => expect(detectServer).toHaveBeenCalledOnce());
    expect(screen.getByText(/Detected server-za-1 from 7 exact GraphQL-to-REST device matches/i))
      .toBeInTheDocument();
  });

  it('does not guess when multiple server identities are detected', async () => {
    const detectServer = vi.fn<DetectGraphqlServer>(async () => graphqlServerDetection({
      candidates: [
        { serverId: 'server-za-1', graphqlDeviceCount: 9, restDeviceMatchCount: 7 },
        { serverId: 'server-za-2', graphqlDeviceCount: 5, restDeviceMatchCount: 0 },
      ],
      recommendedServerId: 'server-za-1',
      confidence: 'ambiguous',
    }));
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig({ graphqlServerId: '' })}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={async () => true}
      detectServer={detectServer}
    />);

    await userEvent.click(screen.getByRole('button', { name: 'Detect server identity' }));
    expect(await screen.findByText('Select from detected identities')).toBeVisible();
    expect(screen.getByText('Save server identity').closest('button')).toBeDisabled();
    expect(screen.getByText(/The app will not guess across servers/i)).toBeVisible();
  });

  it('does not prefill a single server without an exact REST device match', async () => {
    const detectServer = vi.fn<DetectGraphqlServer>(async () => graphqlServerDetection({
      candidates: [{
        serverId: 'server-unverified',
        graphqlDeviceCount: 4,
        restDeviceMatchCount: 0,
      }],
      recommendedServerId: null,
      confidence: 'none',
    }));
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig({ graphqlServerId: '' })}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={async () => true}
      detectServer={detectServer}
    />);

    await userEvent.click(screen.getByText('Detect server identity').closest('button')!);
    expect(await screen.findByText('Select from detected identities')).toBeInTheDocument();
    expect(screen.getByText('Save server identity').closest('button')).toBeDisabled();
  });

  it('keeps validated manual server entry behind Advanced setup and requires explicit save', async () => {
    const saveConfig = vi.fn<
      (input: NcentralGraphqlConfigInput) => Promise<NcentralGraphqlConfig>
    >(async input => graphqlConfig({ graphqlServerId: input.graphqlServerId, revision: 5 }));
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig({ graphqlServerId: '' })}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={async () => true}
      saveConfig={saveConfig}
    />);

    expect(screen.queryByLabelText('Exact ncentralDevice.server.id')).not.toBeInTheDocument();
    await userEvent.click(screen.getByText('Advanced: enter server ID manually'));
    const manualId = screen.getByLabelText('Exact ncentralDevice.server.id');
    await userEvent.type(manualId, 'bad server id');
    expect(screen.getByText(/without whitespace or control characters/i)).toBeInTheDocument();
    expect(screen.getByText('Save server identity').closest('button')).toBeDisabled();

    await userEvent.clear(manualId);
    await userEvent.type(manualId, 'server-manual-1');
    const saveButton = screen.getByText('Save server identity').closest('button');
    expect(saveButton).toBeEnabled();
    expect(saveConfig).not.toHaveBeenCalled();
    await userEvent.click(saveButton!);
    await waitFor(() => expect(saveConfig).toHaveBeenCalledWith(expect.objectContaining({
      graphqlServerId: 'server-manual-1',
    })));
  });

  it('shows an environment setting instead of an in-app save for managed configuration', async () => {
    render(<NcentralGraphqlCustomerEnrichmentPanel
      config={graphqlConfig({
        graphqlServerId: '',
        managedByEnvironment: true,
        credentialSource: 'environment',
      })}
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      providerCompanyName="Northwind"
      organizationIds={['customer-immutable-101']}
      customerCandidates={graphqlTestResult().customerCandidates}
      isAdmin
      onOrganizationIdsChange={() => undefined}
      persistScope={async () => true}
      detectServer={async () => graphqlServerDetection()}
    />);

    await userEvent.click(screen.getByRole('button', { name: 'Detect server identity' }));
    expect(await screen.findByLabelText('Container setting to copy'))
      .toHaveValue('NCENTRAL_GRAPHQL_SERVER_ID=server-za-1');
    expect(screen.queryByText('Save server identity')).not.toBeInTheDocument();
  });

});

describe('N-central device workflow guidance', () => {
  it('requires a real mapped customer before leaving the Customers step', () => {
    expect(ncentralWizardContinueDisabled({
      activeStep: 2,
      lastStep: 3,
      connectionReady: true,
      mappedOrganizationCount: 0,
    })).toBe(true);
    expect(ncentralWizardContinueDisabled({
      activeStep: 2,
      lastStep: 3,
      connectionReady: true,
      mappedOrganizationCount: 1,
    })).toBe(false);
    expect(ncentralWizardContinueDisabled({
      activeStep: 1,
      lastStep: 3,
      connectionReady: false,
      mappedOrganizationCount: 1,
    })).toBe(true);
  });

  it('describes REST detail limits separately from the GraphQL cache', () => {
    expect(ncentralRestDetailEnrichmentMessage('fast', null)).toContain(
      'REST detail enrichment is off',
    );
    expect(ncentralRestDetailEnrichmentMessage('balanced', null)).toContain(
      'up to 25 devices',
    );
    const fullMessage = ncentralRestDetailEnrichmentMessage('full', graphqlConfig({
      cache: {
        status: 'fresh',
        deviceCount: 42,
      },
    }));
    expect(fullMessage).toContain('up to 250 devices');
    expect(fullMessage).toContain('separate GraphQL rich-inventory cache is Fresh');
    expect(fullMessage).toContain('42 cached asset(s)');
    expect(fullMessage).toContain('not controlled by this REST limit');
  });

  it('shows accessible phase readiness with exactly one current next action', () => {
    const baseProps = {
      mappedOrganizationCount: 1,
      customerSelected: false,
      optionsLoaded: false,
      optionsLoading: false,
      graphqlConfig: graphqlConfig({ graphqlEnabled: false }),
      policySaved: false,
      previewAvailable: false,
      reviewableCount: 0,
      conflictCount: 0,
      selectedCount: 0,
    };
    const { rerender } = render(<NcentralDeviceWorkflowReadiness {...baseProps} />);

    expect(screen.getByRole('region', { name: 'Device workflow readiness' })).toBeVisible();
    expect(screen.getByRole('list', { name: 'Device reconciliation phases' })).toBeVisible();
    expect(screen.getByText('Customer · select one')).toBeVisible();
    expect(screen.getByText('GraphQL · optional, off')).toBeVisible();
    expect(screen.getAllByText(/^Next:/)).toHaveLength(1);
    expect(screen.getByText(/Select a mapped N-central customer/i)).toBeVisible();

    rerender(<NcentralDeviceWorkflowReadiness
      {...baseProps}
      customerSelected
      optionsLoaded
    />);
    expect(screen.getByText('Customer · selected')).toBeVisible();
    expect(screen.getByText('Choices · loaded')).toBeVisible();
    expect(screen.getByText(/policy is saved first/i)).toBeVisible();

    rerender(<NcentralDeviceWorkflowReadiness
      {...baseProps}
      customerSelected
      optionsLoaded
      policySaved
      previewAvailable
      reviewableCount={3}
      selectedCount={2}
    />);
    expect(screen.getByText('Review · 2 selected')).toBeVisible();
    expect(screen.getByText(/Import the 2 selected reviewed device/i)).toBeVisible();
    expect(screen.getAllByText(/^Next:/)).toHaveLength(1);
  });

  it('uses persisted customer freshness instead of an empty global readiness snapshot', () => {
    render(<NcentralDeviceWorkflowReadiness
      mappedOrganizationCount={1}
      customerSelected
      optionsLoaded
      optionsLoading={false}
      graphqlConfig={graphqlConfig({ cache: { status: 'empty', deviceCount: 0 } })}
      scopedDiagnostics={freshEnrichmentDiagnostics()}
      policySaved
      previewAvailable={false}
      reviewableCount={0}
      conflictCount={0}
      selectedCount={0}
    />);

    expect(screen.getByText('GraphQL · Fresh cache')).toBeInTheDocument();
    expect(screen.queryByText('GraphQL · Empty cache')).not.toBeInTheDocument();
  });
});

describe('N-central missing-device lifecycle summary', () => {
  it('loads only the selected customer scope and opens the governed workbench', async () => {
    const queue: MissingDeviceLifecycleQueue = {
      summary: {
        observed: 18,
        monitoring: 4,
        eligible: 2,
        notEvaluated: 3,
        retired: 1,
        restoreReady: 1,
        total: 29,
      },
      items: [],
      total: 0,
    };
    const loadSummary = vi.fn<(
      request: { companyId: string; providerParentId: string },
      signal?: AbortSignal,
    ) => Promise<MissingDeviceLifecycleQueue>>(async () => queue);
    const onOpen = vi.fn<() => void>();
    render(<NcentralMissingDeviceLifecycleSummary
      companyId="cmdb-company-1"
      providerParentId="rest-customer-42"
      customerName="Northwind"
      onOpen={onOpen}
      loadSummary={loadSummary}
    />);

    await waitFor(() => expect(loadSummary).toHaveBeenCalledWith({
      companyId: 'cmdb-company-1',
      providerParentId: 'rest-customer-42',
    }, expect.any(AbortSignal)));
    expect(screen.getByText('Tracked · 29')).toBeInTheDocument();
    expect(screen.getByText('Ready to retire source · 2')).toBeInTheDocument();
    expect(screen.getByText('Ready to restore source · 1')).toBeInTheDocument();
    expect(screen.getByText('3 administrator decisions')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Review missing devices' }));
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it('defaults conservative lifecycle thresholds without replacing saved values', () => {
    const policy: ConnectWiseCiPolicy = {
      id: 'policy-1',
      provider: 'ncentral',
      companyId: 'cmdb-company-1',
      providerParentId: 'rest-customer-42',
      typeMode: 'all',
      includedTypeIds: [],
      typeMappings: {},
      blockUnmappedTypes: false,
      statusMode: 'all',
      includedStatusIds: [],
      excludedExternalIds: [],
      syncMode: 'manual',
      intervalMinutes: 360,
      enabled: false,
      revision: 1,
    };

    expect(withNcentralLifecyclePolicyDefaults(policy)).toEqual(expect.objectContaining({
      missingDeviceRequiredSnapshots: 3,
      missingDeviceMinimumHours: 24,
    }));
    expect(withNcentralLifecyclePolicyDefaults({
      ...policy,
      missingDeviceRequiredSnapshots: 6,
      missingDeviceMinimumHours: 72,
    })).toEqual(expect.objectContaining({
      missingDeviceRequiredSnapshots: 6,
      missingDeviceMinimumHours: 72,
    }));
  });
});

function enrichmentDiagnostics(
  changes: Partial<NcentralEnrichmentDiagnostics> = {},
): NcentralEnrichmentDiagnostics {
  const base: NcentralEnrichmentDiagnostics = {
    provider: 'ncentral',
    company: { id: 'cmdb-company-1', name: 'Northwind' },
    providerCompany: { id: 'rest-customer-42', name: 'Northwind N-central' },
    readOnly: true,
    generatedAt: '2026-08-03T08:00:00Z',
    summary: {
      knownDevices: 4,
      returnedDevices: 2,
      mappedDevices: 2,
      reviewedDevices: 2,
      reasonCounts: {
        enriched: 1,
        graphql_missing: 1,
        graphql_only: 1,
        ignored: 1,
        rest_only: 1,
      },
      unmatchedIdentities: 2,
    },
    freshness: {
      status: 'partial',
      reason: 'The current GraphQL generation is usable but has aggregate unmatched identities.',
      enabled: true,
      configured: true,
      scopeConfigured: true,
      serverConfigured: true,
      complete: true,
      scopeMatches: true,
      lastRefreshedAt: '2026-08-03T07:55:00Z',
      expiresAt: '2026-08-03T08:10:00Z',
      providerAssetCount: 6,
      eligibleDeviceCount: 4,
      unmatchedDeviceCount: 2,
    },
    coverage: {
      eligibleDevices: 4,
      evaluatedDevices: 2,
      enrichedDevices: 1,
      enrichedPercent: 25,
    },
    filters: { reason: '', status: '', search: '', limit: 50, offset: 0 },
    total: 2,
    items: [
      {
        externalId: 'device-1',
        name: 'HV-01',
        type: 'Server',
        status: 'Online',
        assetId: 'asset-1',
        assetName: 'HV-01',
        reason: 'enriched',
        reasonLabel: 'Enriched',
        detail: 'A current GraphQL row matches this REST identity.',
        mapped: true,
        reviewState: 'resolved',
        reviewAction: '',
        nextAction: { key: 'none', label: 'No enrichment action is required.' },
        lastSeenAt: '2026-08-03T07:50:00Z',
        cacheObservedAt: '2026-08-03T07:55:00Z',
        cacheExpiresAt: '2026-08-03T08:10:00Z',
      },
      {
        externalId: 'device-2',
        name: 'SAGE-APP-01',
        type: 'Server',
        status: 'Offline',
        assetId: 'asset-2',
        assetName: 'Sage application server',
        reason: 'graphql_missing',
        reasonLabel: 'GraphQL missing',
        detail: 'The current complete GraphQL generation has no row for this REST identity.',
        mapped: true,
        reviewState: 'pending',
        reviewAction: 'update',
        nextAction: {
          key: 'review_scope',
          label: 'Verify the GraphQL Customer scope, then refresh the full-scope cache.',
        },
        lastSeenAt: '2026-08-03T07:45:00Z',
        cacheObservedAt: null,
        cacheExpiresAt: null,
      },
    ],
    latestPreview: {
      id: 'preview-1',
      status: 'success',
      finishedAt: '2026-08-03T07:58:00Z',
      discovered: 4,
      included: 4,
      excluded: 0,
      exclusionReasons: {},
      counts: { create: 0, update: 1, link: 0, unchanged: 3, conflict: 0 },
    },
    limitations: [
      'Unmatched GraphQL identities are retained only as an aggregate count.',
      'REST deep-detail reads record the requested count, not per-device success.',
    ],
  };
  return {
    ...base,
    ...changes,
    summary: { ...base.summary, ...changes.summary },
    freshness: { ...base.freshness, ...changes.freshness },
    coverage: { ...base.coverage, ...changes.coverage },
    filters: { ...base.filters, ...changes.filters },
  };
}

function freshEnrichmentDiagnostics(): NcentralEnrichmentDiagnostics {
  const base = enrichmentDiagnostics();
  return enrichmentDiagnostics({
    freshness: {
      ...base.freshness,
      status: 'fresh',
      reason: 'The current complete customer cache is ready for enrichment.',
      complete: true,
      providerAssetCount: 100,
      eligibleDeviceCount: 91,
      unmatchedDeviceCount: 9,
    },
    coverage: {
      eligibleDevices: 91,
      evaluatedDevices: 91,
      enrichedDevices: 91,
      enrichedPercent: 100,
    },
  });
}

describe('N-central enrichment diagnostics workbench', () => {
  it('loads only persisted customer evidence and presents bounded coverage and actions', async () => {
    const loadDiagnostics = vi.fn<(
      request: NcentralEnrichmentDiagnosticsRequest,
      signal?: AbortSignal,
    ) => Promise<NcentralEnrichmentDiagnostics>>(async () => enrichmentDiagnostics());
    const onDiagnosticsChange = vi.fn<
      (diagnostics: NcentralEnrichmentDiagnostics | null) => void
    >();
    render(<NcentralEnrichmentDiagnosticsWorkbench
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      customerName="Northwind"
      onDiagnosticsChange={onDiagnosticsChange}
      loadDiagnostics={loadDiagnostics}
    />);

    await waitFor(() => expect(loadDiagnostics).toHaveBeenCalledWith({
      companyId: 'cmdb-company-1',
      providerCompanyId: 'rest-customer-42',
      status: '',
      search: '',
      limit: 50,
      offset: 0,
    }, expect.any(AbortSignal)));
    expect(onDiagnosticsChange).toHaveBeenLastCalledWith(enrichmentDiagnostics());
    expect(screen.getByRole('region', { name: 'Enrichment diagnostics' })).toBeInTheDocument();
    expect(screen.getByText(/reads stored CMDB and cache evidence only/i)).toBeInTheDocument();
    expect(screen.getByText(/never calls N-central/i)).toBeInTheDocument();
    expect(screen.getByText('Known devices')).toBeInTheDocument();
    expect(screen.getByText('Coverage')).toBeInTheDocument();
    expect(screen.getByText('25%')).toBeInTheDocument();
    expect(screen.getByText('Cache · Partial')).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: 'Enrichment diagnostic rows' }))
      .not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show device diagnostics' }));
    expect(screen.getByRole('table', { name: 'Enrichment diagnostic rows' })).toBeInTheDocument();
    expect(screen.getByText('SAGE-APP-01')).toBeInTheDocument();
    expect(screen.getAllByText(/Verify the GraphQL Customer scope/i)).toHaveLength(2);
    expect(screen.getByText('Discovered · 4')).toBeInTheDocument();
    expect(screen.getByText('Included · 4')).toBeInTheDocument();
    expect(screen.getByText('Excluded · 0')).toBeInTheDocument();
    expect(screen.getByText('GraphQL only · may be outside saved REST filter'))
      .toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Enrichment diagnostic limitations' }))
      .toHaveTextContent(/aggregate count/i);
  });

  it('applies server reason/search filters explicitly and device status locally', async () => {
    const loadDiagnostics = vi.fn<(
      request: NcentralEnrichmentDiagnosticsRequest,
      signal?: AbortSignal,
    ) => Promise<NcentralEnrichmentDiagnostics>>(async request => enrichmentDiagnostics({
      filters: {
        reason: request.status,
        status: request.status,
        search: request.search,
        limit: request.limit,
        offset: request.offset,
      },
    }));
    render(<NcentralEnrichmentDiagnosticsWorkbench
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      customerName="Northwind"
      loadDiagnostics={loadDiagnostics}
    />);
    await waitFor(() => expect(loadDiagnostics).toHaveBeenCalledOnce());
    await userEvent.click(screen.getByRole('button', { name: 'Show device diagnostics' }));

    fireEvent.mouseDown(screen.getByText('Enrichment reason', { selector: 'label' })
      .parentElement!.querySelector('[role="combobox"]')!);
    await userEvent.click(screen.getByText('GraphQL missing (1)'));
    await userEvent.type(screen.getByLabelText('Search devices or assets'), 'sage');
    expect(loadDiagnostics).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(loadDiagnostics).toHaveBeenCalledTimes(2));
    expect(loadDiagnostics.mock.calls[1]?.[0]).toEqual(expect.objectContaining({
      status: 'graphql_missing',
      search: 'sage',
      offset: 0,
    }));

    fireEvent.mouseDown(screen.getByText('N-central device status', { selector: 'label' })
      .parentElement!.querySelector('[role="combobox"]')!);
    await userEvent.click(screen.getByText('Offline', { selector: '[role="option"]' }));
    expect(screen.queryByText('HV-01')).not.toBeInTheDocument();
    expect(screen.getByText('SAGE-APP-01')).toBeInTheDocument();
    expect(loadDiagnostics).toHaveBeenCalledTimes(2);
  });

  it('shows an empty state and supports explicit retry after an error', async () => {
    const loadDiagnostics = vi.fn<(
      request: NcentralEnrichmentDiagnosticsRequest,
      signal?: AbortSignal,
    ) => Promise<NcentralEnrichmentDiagnostics>>()
      .mockRejectedValueOnce(new Error('Stored diagnostics are temporarily unavailable.'))
      .mockResolvedValueOnce(enrichmentDiagnostics({
        summary: { knownDevices: 0, returnedDevices: 0, reasonCounts: {} },
        coverage: {
          eligibleDevices: 0,
          evaluatedDevices: 0,
          enrichedDevices: 0,
          enrichedPercent: 0,
        },
        total: 0,
        items: [],
      }));
    render(<NcentralEnrichmentDiagnosticsWorkbench
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      customerName="Northwind"
      loadDiagnostics={loadDiagnostics}
    />);

    expect(await screen.findByText(/Diagnostics could not be loaded/i)).toHaveTextContent(
      /temporarily unavailable/i,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText(/No persisted N-central device evidence exists/i))
      .toBeInTheDocument();
    expect(loadDiagnostics).toHaveBeenCalledTimes(2);
  });

  it('aborts stale requests on customer switches and reloads after evidence changes', async () => {
    const signals: AbortSignal[] = [];
    const loadDiagnostics = vi.fn<(
      request: NcentralEnrichmentDiagnosticsRequest,
      signal?: AbortSignal,
    ) => Promise<NcentralEnrichmentDiagnostics>>((_request, signal) => {
      if (signal) signals.push(signal);
      return new Promise(() => undefined);
    });
    const { rerender, unmount } = render(<NcentralEnrichmentDiagnosticsWorkbench
      companyId="cmdb-company-1"
      providerCompanyId="rest-customer-42"
      customerName="Northwind"
      refreshKey="preview-1"
      loadDiagnostics={loadDiagnostics}
    />);
    await waitFor(() => expect(loadDiagnostics).toHaveBeenCalledOnce());

    rerender(<NcentralEnrichmentDiagnosticsWorkbench
      companyId="cmdb-company-2"
      providerCompanyId="rest-customer-99"
      customerName="Contoso"
      refreshKey="preview-2"
      loadDiagnostics={loadDiagnostics}
    />);
    await waitFor(() => expect(loadDiagnostics).toHaveBeenCalledTimes(2));
    expect(signals[0]?.aborted).toBe(true);
    expect(loadDiagnostics.mock.calls[1]?.[0]).toEqual(expect.objectContaining({
      companyId: 'cmdb-company-2',
      providerCompanyId: 'rest-customer-99',
    }));

    rerender(<NcentralEnrichmentDiagnosticsWorkbench
      companyId="cmdb-company-2"
      providerCompanyId="rest-customer-99"
      customerName="Contoso"
      refreshKey="preview-3"
      loadDiagnostics={loadDiagnostics}
    />);
    await waitFor(() => expect(loadDiagnostics).toHaveBeenCalledTimes(3));
    expect(signals[1]?.aborted).toBe(true);
    unmount();
    expect(signals[2]?.aborted).toBe(true);
  });
});
