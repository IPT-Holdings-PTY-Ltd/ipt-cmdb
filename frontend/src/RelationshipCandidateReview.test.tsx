import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  RelationshipCandidateReviewDialog,
  candidateFreshness,
  candidateFreshnessState,
  confidencePercent,
  filterRelationshipCandidates,
  type RelationshipCandidate,
  type RelationshipCandidateDecision,
  type RelationshipCandidateFilters,
} from './RelationshipCandidateReview';

beforeEach(() => {
  // jsdom needs a concrete root size when resolving MUI's rem-based dialog
  // typography during accessible-role queries.
  document.documentElement.style.fontSize = '16px';
  document.body.style.fontSize = '16px';
});

function suggestion(changes: Partial<RelationshipCandidate> = {}): RelationshipCandidate {
  return {
    id: 'candidate-1',
    companyId: 'company-1',
    provider: 'ncentral',
    fromCiId: 'host-1',
    toCiId: 'vm-1',
    fromName: 'HV-HOST-01',
    toName: 'SAGE-APP-01',
    relationshipType: 'hosts',
    confidence: 0.94,
    evidence: {
      messages: ['N-central reports the VM host immutable device ID.'],
      impactPolicy: 'required',
    },
    firstObservedAt: '2026-07-30T08:00:00Z',
    lastSeenAt: '2026-07-31T08:00:00Z',
    state: 'pending',
    ...changes,
  };
}

function renderReview({
  candidates = [suggestion()],
  canManage = true,
  loading = false,
  error = '',
  showCompany = false,
  onFocusCandidate,
  onDecision = vi.fn<
    (
      candidate: RelationshipCandidate,
      decision: RelationshipCandidateDecision,
      notes: string,
    ) => Promise<void>
  >(async () => undefined),
}: {
  candidates?: RelationshipCandidate[];
  canManage?: boolean;
  loading?: boolean;
  error?: string;
  showCompany?: boolean;
  onFocusCandidate?: (candidate: RelationshipCandidate) => void;
  onDecision?: (
    candidate: RelationshipCandidate,
    decision: RelationshipCandidateDecision,
    notes: string,
  ) => Promise<void>;
} = {}) {
  render(
    <RelationshipCandidateReviewDialog
      open
      candidates={candidates}
      loading={loading}
      error={error}
      canManage={canManage}
      showCompany={showCompany}
      onClose={() => undefined}
      onRefresh={async () => undefined}
      onFocusCandidate={onFocusCandidate}
      onDecision={onDecision}
    />,
  );
  return { onDecision };
}

describe('relationship suggestion review', () => {
  it('shows provider evidence and keeps scoped readers in review-only mode', () => {
    renderReview({ canManage: false });

    expect(document.querySelector('table[aria-label="Pending relationship suggestions"]')).toBeInTheDocument();
    expect(screen.getByText('HV-HOST-01')).toBeInTheDocument();
    expect(screen.getByText('SAGE-APP-01')).toBeInTheDocument();
    expect(screen.getAllByText(/^N-central/).length).toBeGreaterThan(0);
    expect(screen.getByText('N-central reports the VM host immutable device ID.')).toBeInTheDocument();
    expect(screen.getByLabelText('94 percent confidence')).toBeInTheDocument();
    expect(screen.getByText('Review only')).toBeInTheDocument();
    expect(screen.queryByText('Approve')).not.toBeInTheDocument();
    expect(screen.getByText(/manually created relationships are never changed/i)).toBeInTheDocument();
  });

  it('requires notes before explicitly approving a suggestion', async () => {
    const onDecision = vi.fn<
      (
        candidate: RelationshipCandidate,
        decision: RelationshipCandidateDecision,
        notes: string,
      ) => Promise<void>
    >(async () => undefined);
    renderReview({ onDecision });

    fireEvent.click(screen.getByText('Approve').closest('button')!);
    expect(screen.getByText('Approve relationship suggestion')).toBeInTheDocument();
    expect(screen.getByText('Approve suggestion').closest('button')).toBeDisabled();
    expect(screen.getByText(/does not write to the source provider/i)).toBeInTheDocument();

    const notes = document.querySelector('textarea[name="relationship-decision-notes"]');
    expect(notes).toBeInTheDocument();
    fireEvent.change(notes!, {
      target: { value: 'Verified immutable host evidence' },
    });
    fireEvent.click(screen.getByText('Approve suggestion').closest('button')!);

    await waitFor(() => expect(onDecision).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'candidate-1' }),
      'approve',
      'Verified immutable host evidence',
    ));
  });

  it('explains the difference between reject and ignore', () => {
    renderReview();

    fireEvent.click(screen.getByText('Reject').closest('button')!);
    expect(screen.getByText(/evidence is incorrect or identifies the wrong/i)).toBeInTheDocument();
    expect(screen.getByText('Reject bad evidence').closest('button')).toBeDisabled();

    fireEvent.click(screen.getByText('Cancel').closest('button')!);
    fireEvent.click(screen.getByText('Ignore').closest('button')!);
    expect(screen.getByText(/evidence is valid but the relationship is not useful/i)).toBeInTheDocument();
    expect(screen.getByText('Ignore suggestion').closest('button')).toBeDisabled();
  });

  it('provides accessible loading, error and empty states', () => {
    const { rerender } = render(
      <RelationshipCandidateReviewDialog
        open
        candidates={[]}
        loading
        error=""
        canManage
        onClose={() => undefined}
        onRefresh={async () => undefined}
        onDecision={async () => undefined}
      />,
    );
    expect(document.querySelector('output[aria-label="Loading relationship suggestions"]')).toBeInTheDocument();

    rerender(
      <RelationshipCandidateReviewDialog
        open
        candidates={[]}
        loading={false}
        error="Suggestion endpoint unavailable"
        canManage
        onClose={() => undefined}
        onRefresh={async () => undefined}
        onDecision={async () => undefined}
      />,
    );
    expect(screen.getByText('Suggestion endpoint unavailable')).toBeInTheDocument();
    expect(screen.getByText(/No pending relationship suggestions in this workspace/i)).toBeInTheDocument();
  });

  it('shows the customer in an MSP queue and can focus an item on the map', () => {
    const onFocusCandidate = vi.fn<(candidate: RelationshipCandidate) => void>();
    renderReview({
      showCompany: true,
      onFocusCandidate,
      candidates: [suggestion({ companyName: 'Northwind Traders' })],
    });

    expect([...document.querySelectorAll('th')].some(cell => cell.textContent === 'Customer')).toBe(true);
    expect(screen.getByText('Northwind Traders')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Show on map').closest('button')!);
    expect(onFocusCandidate).toHaveBeenCalledWith(expect.objectContaining({ id: 'candidate-1' }));
  });
});

describe('relationship suggestion formatting', () => {
  it('normalizes fractional and percentage confidence values', () => {
    expect(confidencePercent(0.875)).toBe(88);
    expect(confidencePercent(76)).toBe(76);
    expect(confidencePercent(250)).toBe(100);
  });

  it('uses last-seen evidence before older observation timestamps', () => {
    const candidate = suggestion({
      lastSeenAt: 'current evidence',
      observedAt: 'older evidence',
    });
    expect(candidateFreshness(candidate)).toBe('current evidence');
  });

  it('classifies freshness against explicit review windows', () => {
    const now = new Date('2026-07-31T12:00:00Z');
    expect(candidateFreshnessState(suggestion({ lastSeenAt: '2026-07-31T08:00:00Z' }), now)).toBe('current');
    expect(candidateFreshnessState(suggestion({ lastSeenAt: '2026-07-28T08:00:00Z' }), now)).toBe('aging');
    expect(candidateFreshnessState(suggestion({ lastSeenAt: '2026-07-20T08:00:00Z' }), now)).toBe('stale');
    expect(candidateFreshnessState(suggestion({ lastSeenAt: '', firstObservedAt: '' }), now)).toBe('unreported');
  });

  it('filters a provider-neutral queue by search, customer, type, confidence and freshness', () => {
    const candidates = [
      suggestion({ companyName: 'Northwind Traders' }),
      suggestion({
        id: 'candidate-2',
        companyId: 'company-2',
        companyName: 'Contoso',
        provider: 'connectwise',
        fromName: 'SQL-02',
        relationshipType: 'depends_on',
        confidence: 0.62,
        lastSeenAt: '2026-07-20T08:00:00Z',
      }),
    ];
    const filters: RelationshipCandidateFilters = {
      search: 'SQL',
      companyId: 'company-2',
      provider: 'connectwise',
      relationshipType: 'depends_on',
      confidence: 'low',
      freshness: 'stale',
    };

    expect(filterRelationshipCandidates(candidates, filters, new Date('2026-07-31T12:00:00Z')))
      .toEqual([expect.objectContaining({ id: 'candidate-2' })]);
  });
});
