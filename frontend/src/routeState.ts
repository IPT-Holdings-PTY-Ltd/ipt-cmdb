export type RelationshipRouteState = {
  view: string | null;
  businessAppId: string;
  showSharedImpact: boolean;
  assetId: string;
};

/** Reads the relationship perspective and business-system scope from Router state. */
export function readRelationshipRouteState(searchParams: URLSearchParams): RelationshipRouteState {
  const businessAppId = searchParams.get('businessAppId') || '';
  return {
    view: searchParams.get('view'),
    businessAppId,
    showSharedImpact: Boolean(businessAppId) && searchParams.get('sharedImpact') === '1',
    assetId: searchParams.get('assetId') || '',
  };
}

/** Produces the canonical relationship query without retaining obsolete filters. */
export function createRelationshipSearchParams(
  view: string,
  businessAppId: string,
  showSharedImpact: boolean,
) {
  const searchParams = new URLSearchParams({ view: view.toLowerCase() });
  if (businessAppId) searchParams.set('businessAppId', businessAppId);
  if (businessAppId && showSharedImpact) searchParams.set('sharedImpact', '1');
  return searchParams;
}
