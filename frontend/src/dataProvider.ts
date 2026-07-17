import type { DataProvider, GetListParams } from 'react-admin';
import { apiFetch } from './session';
import { currentCompanyId } from './workspace';
import type { Asset } from './types';

const assetUrl = () => {
  const companyId = currentCompanyId();
  return companyId && companyId !== '__root__' ? `/api/assets?companyId=${encodeURIComponent(companyId)}` : '/api/assets';
};

function filteredAssets(records: Asset[], params: GetListParams): Asset[] {
  const filter = params.filter || {};
  let result = records.filter(record => {
    const search = String(filter.q || '').toLowerCase();
    const matchesSearch = !search || [record.name, record.type, record.source, record.metadata?.site, record.metadata?.technicalOwner]
      .some(value => String(value || '').toLowerCase().includes(search));
    return matchesSearch && (!filter.type || record.type === filter.type) && (!filter.lifecycle || record.metadata?.lifecycle === filter.lifecycle);
  });
  const field = params.sort?.field || 'name';
  const order = params.sort?.order === 'DESC' ? -1 : 1;
  result = result.sort((left, right) => String((left as unknown as Record<string, unknown>)[field] || '').localeCompare(String((right as unknown as Record<string, unknown>)[field] || '')) * order);
  return result;
}

function withOwnerSelections(record: Asset): Asset & { ownerSelections: Record<string, string> } {
  const ownerSelections = Object.fromEntries(
    (record.responsibilities || []).filter(item => !item.effectiveUntil && item.isPrimary).map(item => [item.role, item.contactId]),
  );
  return { ...record, ownerSelections };
}

function assetPayload(data: Record<string, unknown>) {
  const payload = { ...data };
  const selections = payload.ownerSelections as Record<string, string> | undefined;
  delete payload.ownerSelections;
  if (selections) {
    payload.responsibilities = Object.entries(selections)
      .filter(([, contactId]) => Boolean(contactId))
      .map(([role, contactId]) => ({ role, contactId, isPrimary: true, escalationOrder: 1 }));
  }
  return payload;
}

const provider = {
  async getList(resource: string, params: GetListParams) {
    if (resource !== 'assets') throw new Error(`Unsupported resource: ${resource}`);
    const filtered = filteredAssets(await apiFetch<Asset[]>(assetUrl()), params);
    const pagination = params.pagination || { page: 1, perPage: 25 };
    const start = (pagination.page - 1) * pagination.perPage;
    return { data: filtered.slice(start, start + pagination.perPage), total: filtered.length };
  },
  async getOne(resource: string, params: { id: string | number }) {
    if (resource !== 'assets') throw new Error(`Unsupported resource: ${resource}`);
    return { data: withOwnerSelections(await apiFetch<Asset>(`/api/v2/assets/${params.id}`)) };
  },
  async create(resource: string, params: { data: Record<string, unknown> }) {
    if (resource !== 'assets') throw new Error(`Unsupported resource: ${resource}`);
    const companyId = currentCompanyId();
    if (!companyId || companyId === '__root__') throw new Error('Select a customer before adding an asset');
    return { data: await apiFetch<Asset>('/api/assets', { method: 'POST', body: JSON.stringify({ ...assetPayload(params.data), companyId }) }) };
  },
  async update(resource: string, params: { id: string | number; data: Record<string, unknown> }) {
    if (resource !== 'assets') throw new Error(`Unsupported resource: ${resource}`);
    return { data: await apiFetch<Asset>(`/api/assets/${params.id}`, { method: 'PATCH', body: JSON.stringify(assetPayload(params.data)) }) };
  },
  async delete() { throw new Error('Asset deletion is intentionally disabled; retire the CI instead.'); },
  async getMany(resource: string, params: { ids: Array<string | number> }) {
    const records = await apiFetch<Asset[]>(assetUrl());
    return { data: records.filter(record => params.ids.includes(record.id)) };
  },
  async getManyReference(resource: string, params: GetListParams & { target: string; id: string | number }) {
    return provider.getList(resource, { ...params, filter: { ...params.filter, [params.target]: params.id } });
  },
  async updateMany(resource: string, params: { ids: Array<string | number>; data: Record<string, unknown> }) {
    const results = await Promise.all(params.ids.map(id => provider.update(resource, { id, data: params.data })));
    return { data: results.map(result => result.data.id) };
  },
  async deleteMany() { throw new Error('Asset deletion is intentionally disabled; retire the CIs instead.'); },
};

export const dataProvider = provider as unknown as DataProvider;
