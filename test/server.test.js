import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('role scope rule documents client isolation', () => {
  const client = { role: 'client_reader', companyIds: ['acme'] };
  const allowed = (u, id) => u.role === 'platform_admin' || u.companyIds.includes('*') || u.companyIds.includes(id);
  assert.equal(allowed(client, 'acme'), true);
  assert.equal(allowed(client, 'northwind'), false);
});

test('Node runtime floor stays aligned across package, lockfile, CI and Docker', () => {
  const packageJson = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
  const packageLock = JSON.parse(readFileSync(new URL('../package-lock.json', import.meta.url), 'utf8'));
  const ci = readFileSync(new URL('../.github/workflows/ci.yml', import.meta.url), 'utf8');
  const dockerfile = readFileSync(new URL('../Dockerfile', import.meta.url), 'utf8');
  const minimumNode = packageJson.engines.node.replace(/^>=/, '');

  assert.equal(packageLock.packages[''].engines.node, packageJson.engines.node);
  assert.match(ci, new RegExp(`node-version:\\s*['"]${minimumNode.replaceAll('.', '\\.')}['"]`));
  assert.match(dockerfile, new RegExp(`^FROM node:${minimumNode.replaceAll('.', '\\.')}-alpine AS frontend-build$`, 'm'));
});
