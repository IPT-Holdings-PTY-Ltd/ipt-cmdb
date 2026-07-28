import { describe, expect, it } from 'vitest';
import { createRelationshipSearchParams, readRelationshipRouteState } from './routeState';

describe('relationship route state', () => {
  it('round-trips a scoped business application perspective', () => {
    const query = createRelationshipSearchParams('STACK', 'sage-200', true);

    expect(query.toString()).toBe('view=stack&businessAppId=sage-200&sharedImpact=1');
    expect(readRelationshipRouteState(query)).toEqual({
      view: 'stack',
      businessAppId: 'sage-200',
      showSharedImpact: true,
      assetId: '',
    });
  });

  it('does not retain shared impact without a business application', () => {
    const query = createRelationshipSearchParams('NETWORK', '', true);

    expect(query.toString()).toBe('view=network');
    expect(readRelationshipRouteState(new URLSearchParams('view=network&sharedImpact=1'))).toMatchObject({
      businessAppId: '',
      showSharedImpact: false,
    });
  });

  it('reads deep-linked assets without changing the selected perspective', () => {
    expect(readRelationshipRouteState(new URLSearchParams('view=technical&assetId=server-1'))).toEqual({
      view: 'technical',
      businessAppId: '',
      showSharedImpact: false,
      assetId: 'server-1',
    });
  });
});
