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

test('container publishing is reusable only through the guarded release workflow', () => {
  const workflow = readFileSync(
    new URL('../.github/workflows/container-image.yml', import.meta.url),
    'utf8',
  );

  assert.match(workflow, /^on:\r?\n  workflow_call:/m);
  assert.doesNotMatch(workflow, /^  (push|workflow_dispatch):/m);
  assert.match(workflow, /environment: release/);
  assert.match(workflow, /type=raw,value=\$\{\{ inputs\.version \}\}/);
  assert.match(workflow, /type=raw,value=sha-\$\{\{ inputs\.source_sha \}\}/);
  assert.match(workflow, /org\.opencontainers\.image\.revision=\$\{\{ inputs\.source_sha \}\}/);
  assert.match(workflow, /CMDB_VERSION=\$\{\{ inputs\.version \}\}/);
  assert.match(workflow, /CMDB_SOURCE_COMMIT=\$\{\{ inputs\.source_sha \}\}/);
  assert.match(workflow, /Refuse to overwrite immutable release tags/);
  assert.match(workflow, /docker buildx imagetools inspect "\$reference"/);
  assert.doesNotMatch(workflow, /type=(semver|raw),[^\r\n]*(latest|\{\{major\}\}\.\{\{minor\}\})/);
});

test('release workflow records deployment compatibility and packages verifiable bundles', () => {
  const workflow = readFileSync(
    new URL('../.github/workflows/release.yml', import.meta.url),
    'utf8',
  );

  assert.match(workflow, /^      database_change:\r?$/m);
  assert.match(workflow, /^      rollback_policy:\r?$/m);
  assert.match(workflow, /root_version="\$\(tr -d '\[:space:\]' < VERSION\)"/);
  assert.match(workflow, /require\('\.\/package-lock\.json'\)\.version/);
  assert.match(workflow, /require\('\.\/package-lock\.json'\)\.packages\[''\]\.version/);
  assert.match(workflow, /needs: \[prepare, quality\]\r?\n    uses: \.\/\.github\/workflows\/container-image\.yml/);
  assert.match(workflow, /"schemaVersion": plan\[-1\]\.version/);
  assert.match(workflow, /history_hash = schema_history_sha256\(root\)/);
  assert.match(workflow, /"schemaHistorySha256": history_hash/);
  assert.match(workflow, /"postgresqlMajor": int\(next\(iter\(postgres_majors\)\)\)/);
  assert.match(workflow, /"prerelease": os\.environ\["PRERELEASE"\] == "true"/);
  assert.match(workflow, /"architectures": \["linux\/amd64", "linux\/arm64"\]/);
  assert.match(workflow, /"backupRequired": os\.environ\["DATABASE_CHANGE"\] != "none"/);
  assert.match(workflow, /"changeClassification": os\.environ\["DATABASE_CHANGE"\]/);
  assert.match(workflow, /"rollbackPolicy": os\.environ\["ROLLBACK_POLICY"\]/);
  assert.match(
    workflow,
    /"rollbackBoundary": "schema-version-change-requires-database-restore"/,
  );
  assert.match(
    workflow,
    /\[\[ "\$DATABASE_CHANGE" == "expand-only-compatible" && "\$ROLLBACK_POLICY" == "previous-image-compatible" \]\]/,
  );
  assert.match(workflow, /N-1 compatibility is exercised in CI/);
  assert.match(workflow, /f"\{image_name\}@\{digest\}"/);
  assert.match(workflow, /release-manifest\.json/);
  assert.match(workflow, /f"Container: \{image_name\}:\{os\.environ\['VERSION'\]\}"/);
  assert.match(workflow, /f"Digest: \{digest\}"/);
  assert.match(workflow, /Authoritative manifest: release-manifest\.json/);
  assert.match(workflow, /archive_base="\$ARTIFACT_DIRECTORY\/\$bundle_name-self-hosted"/);
  assert.match(workflow, /"\$archive_base\.tar\.gz"/);
  assert.match(workflow, /"\$GITHUB_WORKSPACE\/\$archive_base\.zip"/);
  assert.match(workflow, /sha256sum[\s\S]*release-manifest\.json[\s\S]*release-manifest\.txt[\s\S]*"\$bundle_name-self-hosted\.tar\.gz"[\s\S]*"\$bundle_name-self-hosted\.zip"/);
  assert.match(workflow, /"compose\.appliance\.yml"/);
  assert.match(workflow, /"compose\.production\.yml"/);
  assert.match(workflow, /"VERSION"/);
  assert.match(workflow, /"scripts\/Initialize-Appliance\.ps1"/);
  assert.match(workflow, /"scripts\/Install-Cmdb\.ps1"/);
  assert.match(workflow, /"scripts\/Update-Cmdb\.ps1"/);
  assert.match(workflow, /"scripts\/install-cmdb\.sh"/);
  assert.match(workflow, /"scripts\/update-cmdb\.sh"/);
  assert.match(
    workflow,
    /chmod 0755[\s\S]*"\$bundle_root\/scripts\/update-cmdb\.sh"/,
  );
  assert.doesNotMatch(workflow, /"azure\.yaml"/);
  assert.doesNotMatch(workflow, /"infra\/main\.bicep"/);
  assert.match(workflow, /install -m 0644[\s\S]*release-manifest\.json[\s\S]*bundle_root\/release-manifest\.json/);
  assert.match(workflow, /install -m 0644[\s\S]*release-manifest\.txt[\s\S]*bundle_root\/release-manifest\.txt/);
});

test('stable image channels move only after a successful GitHub release', () => {
  const workflow = readFileSync(
    new URL('../.github/workflows/release.yml', import.meta.url),
    'utf8',
  );
  const publishRelease = workflow.indexOf('  publish-release:');
  const createRelease = workflow.indexOf('gh release create');
  const promoteStable = workflow.indexOf('  promote-stable:');
  const promoteDigest = workflow.indexOf('docker buildx imagetools create');

  assert.ok(publishRelease >= 0);
  assert.ok(createRelease > publishRelease);
  assert.ok(promoteStable > createRelease);
  assert.ok(promoteDigest > promoteStable);
  assert.match(
    workflow.slice(promoteStable),
    /needs: \[prepare, publish-image, publish-release\]/,
  );
  assert.match(
    workflow.slice(promoteStable),
    /if: needs\.prepare\.outputs\.prerelease == 'false'/,
  );
  assert.match(workflow.slice(promoteStable), /exact_image="\$IMAGE_NAME@\$IMAGE_DIGEST"/);
  assert.match(workflow.slice(promoteStable), /for tag in "\$MINOR_VERSION" latest/);
});
