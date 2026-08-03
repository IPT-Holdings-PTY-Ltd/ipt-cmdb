import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import {
  MissingDeviceLifecycleWorkbench,
  type MissingDeviceLifecycleRequest,
} from './Reconciliation';
import type {
  MissingDeviceLifecycleCandidate,
  MissingDeviceLifecycleQueue,
} from './types';

function candidate(
  changes: Partial<MissingDeviceLifecycleCandidate> = {},
): MissingDeviceLifecycleCandidate {
  return {
    id: 'candidate-1',
    mappingId: 'mapping-1',
    provider: 'ncentral',
    companyId: 'company-1',
    companyName: 'Northwind',
    providerParentId: 'customer-42',
    externalId: '1001',
    externalName: 'SERVER-01',
    assetId: 'asset-1',
    assetName: 'Server 01',
    state: 'eligible',
    consecutiveCompleteAbsences: 3,
    requiredAbsences: 3,
    firstAbsentAt: '2026-08-01T08:00:00Z',
    lastObservedAt: '2026-07-31T08:00:00Z',
    lastEvaluatedAt: '2026-08-03T08:00:00Z',
    reappearedAt: null,
    retiredAt: null,
    retiredByName: null,
    retirementNotes: null,
    revision: 4,
    actionAllowed: true,
    actionReason: 'Three complete absences and the minimum age threshold are satisfied.',
    ...changes,
  };
}

function lifecycleQueue(
  changes: Partial<MissingDeviceLifecycleQueue> = {},
): MissingDeviceLifecycleQueue {
  const base: MissingDeviceLifecycleQueue = {
    summary: {
      observed: 12,
      monitoring: 4,
      eligible: 1,
      notEvaluated: 2,
      retired: 3,
      restoreReady: 1,
      total: 23,
    },
    items: [candidate()],
    total: 52,
  };
  return {
    ...base,
    ...changes,
    summary: { ...base.summary, ...changes.summary },
  };
}

describe('missing-device lifecycle workbench', () => {
  it('loads a bounded customer scope, shows evidence, and paginates server-side', async () => {
    const loadCandidates = vi.fn<(
      request: MissingDeviceLifecycleRequest,
      signal?: AbortSignal,
    ) => Promise<MissingDeviceLifecycleQueue>>(async () => lifecycleQueue());
    render(<MissingDeviceLifecycleWorkbench
      companies={[{ id: 'company-1', name: 'Northwind' }]}
      canManage={false}
      initialCompanyId="company-1"
      initialProviderParentId="customer-42"
      loadCandidates={loadCandidates}
    />);

    await waitFor(() => expect(loadCandidates).toHaveBeenCalledWith({
      provider: 'ncentral',
      companyId: 'company-1',
      providerParentId: 'customer-42',
      state: '',
      search: '',
      limit: 25,
      offset: 0,
    }, expect.any(AbortSignal)));
    expect(screen.getByRole('table', { name: 'Missing-device lifecycle candidates' }))
      .toBeInTheDocument();
    expect(screen.getByText('Ready to retire source')).toBeInTheDocument();
    expect(screen.getByText('Complete absences 3/3')).toBeInTheDocument();
    expect(screen.getByText('Retire N-central source').closest('button')).toBeDisabled();
    expect(screen.getByText('Platform administrator approval required')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Go to next page' }));
    await waitFor(() => expect(loadCandidates).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit: 25, offset: 25 }), expect.any(AbortSignal),
    ));

    await userEvent.type(screen.getByLabelText('Search device, provider ID or reason'), 'server');
    await userEvent.click(screen.getByText('Apply search').closest('button')!);
    await waitFor(() => expect(loadCandidates).toHaveBeenLastCalledWith(
      expect.objectContaining({ search: 'server', offset: 0 }), expect.any(AbortSignal),
    ));
  });

  it('requires notes and confirmation before retiring only the N-central source link', async () => {
    const loadCandidates = vi.fn<(
      request: MissingDeviceLifecycleRequest,
      signal?: AbortSignal,
    ) => Promise<MissingDeviceLifecycleQueue>>(async () => lifecycleQueue({ total: 1 }));
    const decideCandidate = vi.fn<(
      candidateId: string,
      action: 'retire' | 'restore',
      request: { expectedRevision: number; notes: string },
    ) => Promise<MissingDeviceLifecycleCandidate>>(
      async () => candidate({ state: 'retired', revision: 5 }),
    );
    render(<MissingDeviceLifecycleWorkbench
      companies={[{ id: 'company-1', name: 'Northwind' }]}
      canManage
      loadCandidates={loadCandidates}
      decideCandidate={decideCandidate}
    />);
    await screen.findByText('SERVER-01');

    await userEvent.click(screen.getByText('Retire N-central source').closest('button')!);
    expect(screen.getByText(/canonical CI, its relationships and other source identities remain/i))
      .toBeInTheDocument();
    const confirm = screen.getByText('Confirm retirement').closest('button')!;
    expect(confirm).toBeDisabled();
    const notes = screen.getByText('Decision notes', { selector: 'label' })
      .parentElement!.querySelector('textarea')!;
    fireEvent.change(notes, { target: { value: 'Device removed from N-central.' } });
    const acknowledgement = screen.getByText(/changes only the N-central source link/i)
      .closest('label')!.querySelector('input')!;
    fireEvent.click(acknowledgement);
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);

    await waitFor(() => expect(decideCandidate).toHaveBeenCalledWith(
      'candidate-1',
      'retire',
      { expectedRevision: 4, notes: 'Device removed from N-central.' },
    ));
    expect(await screen.findByText(/source link was retired with audited decision notes/i))
      .toBeInTheDocument();
  });

  it('shows provider-filter exclusions as neutral and honours action blockers', async () => {
    const excluded = candidate({
      id: 'candidate-filtered',
      state: 'not_evaluated',
      actionAllowed: false,
      actionReason: 'The saved native provider filter intentionally excludes this identity.',
    });
    render(<MissingDeviceLifecycleWorkbench
      companies={[{ id: 'company-1', name: 'Northwind' }]}
      canManage
      loadCandidates={async () => lifecycleQueue({ items: [excluded], total: 1 })}
    />);

    const stateChip = await screen.findByText('Not Evaluated');
    expect(stateChip.closest('.MuiChip-root')).toHaveClass('MuiChip-colorDefault');
    expect(screen.getByText(/provider filter intentionally excludes/i)).toBeInTheDocument();
    expect(screen.queryByText('Retire N-central source')).not.toBeInTheDocument();
  });

  it('makes restore a source-link decision without changing the canonical CI', async () => {
    const restoreReady = candidate({
      id: 'candidate-restored',
      state: 'restore_ready',
      reappearedAt: '2026-08-03T09:00:00Z',
      actionReason: 'The N-central device reappeared in a complete observation.',
    });
    render(<MissingDeviceLifecycleWorkbench
      companies={[{ id: 'company-1', name: 'Northwind' }]}
      canManage
      loadCandidates={async () => lifecycleQueue({ items: [restoreReady], total: 1 })}
    />);

    await userEvent.click((await screen.findByText('Restore source link')).closest('button')!);
    expect(screen.getByText(/canonical CI is not changed/i)).toBeInTheDocument();
    expect(screen.getByText('Confirm restore').closest('button')).toBeDisabled();
  });
});
