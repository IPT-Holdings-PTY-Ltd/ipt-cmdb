import test from 'node:test';
import assert from 'node:assert/strict';

test('role scope rule documents client isolation', () => {
  const client = { role: 'client_reader', companyIds: ['acme'] };
  const allowed = (u, id) => u.role === 'platform_admin' || u.companyIds.includes('*') || u.companyIds.includes(id);
  assert.equal(allowed(client, 'acme'), true);
  assert.equal(allowed(client, 'northwind'), false);
});
