import { describe, expect, it } from 'vitest';
import { makeSuggestedEdges } from './Relationships';
import type { RelationshipCandidate } from './RelationshipCandidateReview';

function candidate(changes: Partial<RelationshipCandidate> = {}): RelationshipCandidate {
  return {
    id: 'suggestion-1',
    companyId: 'company-1',
    provider: 'ncentral',
    fromCiId: 'vm-1',
    toCiId: 'host-1',
    fromName: 'VM-01',
    toName: 'HV-01',
    relationshipType: 'installed_on',
    confidence: 0.91,
    state: 'pending',
    ...changes,
  };
}

describe('relationship suggestion graph overlay', () => {
  it('creates separately identified, non-canonical dashed edges', () => {
    const [edge] = makeSuggestedEdges([candidate()]);

    expect(edge.id).toBe('suggestion:suggestion-1');
    expect(edge.source).toBe('host-1');
    expect(edge.target).toBe('vm-1');
    expect(edge.label).toBe('Suggested · 91%');
    expect(edge.data).toMatchObject({
      candidateId: 'suggestion-1',
      proposed: true,
      relationshipType: 'installed_on',
    });
    expect(edge.style).toMatchObject({ strokeDasharray: '7 6', opacity: 0.58 });
  });

  it('omits unresolved suggestions instead of drawing unsafe guessed endpoints', () => {
    expect(makeSuggestedEdges([candidate({ toCiId: undefined })])).toEqual([]);
  });
});
