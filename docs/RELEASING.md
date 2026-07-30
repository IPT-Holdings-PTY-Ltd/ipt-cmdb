# Releasing IPT CMDB

Releases are created deliberately from the latest commit on the default branch. The
**Create release** workflow validates a single release commit, runs the normal CI suite,
waits at the protected `release` environment, publishes immutable container references,
attaches verifiable self-hosted deployment bundles, and only then promotes stable image
channels.

## One-time repository setup

1. In **Settings → Environments**, open or create the `release` environment.
2. Add at least one required reviewer and restrict deployment branches to the default
   branch.
3. In **Settings → Actions → General**, leave workflow permissions read-only by default.
   The release workflows request only their explicit `contents`, `packages`, `id-token`
   and `attestations` permissions.
4. Optionally enable immutable GitHub releases after the first successful rehearsal.

No registry password or personal access token is required. Publishing uses the
workflow-scoped `GITHUB_TOKEN` and GitHub artifact attestations.

The container publishing workflow has no push or manual trigger. It is reusable only,
and its publishing job is protected by the `release` environment. The caller additionally
refuses to run unless `release.yml` itself came from the default branch.

## Prepare a release pull request

1. Choose a semantic version such as `0.4.0`. Use a suffix such as `0.4.0-rc.1` for a
   prerelease.
2. Update the root `VERSION`, `package.json` and both root version fields in
   `package-lock.json` together:

   ```powershell
   npm version 0.4.0 --no-git-tag-version
   Set-Content -LiteralPath VERSION -Value '0.4.0'
   ```

3. Move the relevant entries from `## [Unreleased]` in `CHANGELOG.md` into a dated
   heading:

   ```markdown
   ## [0.4.0] - 2026-07-30
   ```

4. Keep a new empty `## [Unreleased]` section for subsequent work.
5. Review every migration in `db/migrations`. Decide which database classification and
   rollback policy accurately describe this release.
6. Merge the release pull request only after CI succeeds.

The workflow rejects a mismatch between the requested version, `VERSION`,
`package.json`, either package-lock version, or the changelog heading.

## Database and rollback classifications

The release operator must explicitly select both fields; the default
`review-required` value deliberately fails validation.

Database change:

- `none`: no schema change is included;
- `expand-only-compatible`: the change is additive for forward rollout, but the current
  exact-history runtime does not yet permit an older image after the schema version
  advances;
- `restore-required`: the previous application cannot safely use the migrated schema.

Rollback policy:

- `previous-image-compatible`: application traffic may return to the previous digest
  while retaining the migrated database;
- `database-restore-required`: rollback requires a reviewed PostgreSQL restore to a new
  server and connection cutover.

`previous-image-compatible` is currently valid only with `none`. The workflow rejects
it for both `expand-only-compatible` and `restore-required`: any schema-version change
requires restoring the pre-update database before starting an older image until N-1
compatibility is explicitly exercised in CI. The manifest records this as
`rollbackBoundary=schema-version-change-requires-database-restore` and records
`backupRequired=true` for every classification except `none`. This metadata is a release
contract, not a substitute for actually taking and testing a backup.

## Create the release

1. Open **Actions → Create release → Run workflow** on the default branch.
2. Enter the version with or without its leading `v`.
3. Select the reviewed database change and rollback policy.
4. Review the validation and CI jobs.
5. Approve the `release` environment when satisfied.

The workflow then performs these operations in order:

1. build one multi-architecture image for `linux/amd64` and `linux/arm64`;
2. publish only the exact version and full commit-SHA tags, with OCI version and commit
   metadata, SBOM, provenance and GitHub attestation;
3. refuse to overwrite either immutable reference;
4. create `release-manifest.json`, its compatibility text form, deterministic ZIP and
   tar.gz self-hosted bundles, and SHA-256 checksums;
5. create the GitHub tag and release with those artifacts attached;
6. for a stable release only, move the minor and `latest` tags to the already released
   digest without rebuilding it.

Prereleases never update minor or `latest` channels. All release operations are
serialized so two versions cannot race to update a stable channel.

## Release artifacts

Each GitHub release contains:

- `release-manifest.json`, the authoritative machine-readable contract;
- `release-manifest.txt`, a small backward-compatible `Container:` and `Digest:`
  contract for installers that do not have a JSON parser;
- `ipt-cmdb-<version>-self-hosted.zip`;
- `ipt-cmdb-<version>-self-hosted.tar.gz`;
- `SHA256SUMS` for the two bundles and both attached manifests.

Each bundle has its own internal `SHA256SUMS` and contains both manifests, the exact
digest in `.env.production.example`, Compose profiles, appliance installers, Windows
and POSIX host-side updaters, backup/restore helpers, `VERSION`, and the self-hosted
operations documentation. These are appliance and external-PostgreSQL self-hosting
bundles; they are not Azure source bundles.

The JSON manifest records:

- release version, tag, commit, prerelease state and source timestamp;
- image name, digest, exact `image@sha256` reference, version/SHA tags and architectures;
- latest schema version and a deterministic hash of the complete ordered migration
  history;
- supported PostgreSQL major;
- database change, backup requirement, rollback policy, explicit rollback boundary and
  forward-only migration policy.

After downloading a bundle and `SHA256SUMS`, verify it before extraction:

```bash
sha256sum --check SHA256SUMS
```

Inside the extracted directory, run the same command again to verify every bundled
file. On Windows, compare each recorded value with `Get-FileHash -Algorithm SHA256`.

The bundled installers read `release-manifest.txt` when no image argument is supplied,
combine `Container:` and `Digest:`, and deploy the exact immutable reference:

```powershell
.\scripts\Install-Cmdb.ps1 `
  -AdminEmail admin@example.com `
  -PublicBaseUrl https://cmdb.example.com
```

```bash
./scripts/install-cmdb.sh admin@example.com cmdb https://cmdb.example.com
```

The bundled updaters use the JSON manifest as the source of truth and verify it against
`SHA256SUMS`. Their default action is check-only; a deployment change requires the
operator to repeat both the exact target version and exact manifest SHA-256 reported by
that check. Both Windows and POSIX updaters support appliance and external-PostgreSQL
profiles. They inspect the existing worker topology and fail closed unless the explicit
worker-split flag matches, preserve an intentionally stopped worker, and require exact
readiness including `schemaHistorySha256`.

External-database update history hashes the structured recovery reference rather than
storing it. The host stops the running worker and web writers, establishes the required
recovery point, rejects schema downgrade, runs a one-shot migration, and validates the
target runtime. The application container is never given the Docker socket.

## Rehearsal and recovery

Use a prerelease for the first end-to-end rehearsal. For production, deploy the exact
`image.reference` from `release-manifest.json`; do not use `latest`.

If image publishing succeeded but a later job failed, use **Re-run failed jobs**. Do not
start a new full run: the workflow intentionally refuses to overwrite the existing
version and commit tags. If GitHub release creation succeeded but stable promotion
failed, re-run only the failed promotion job; it copies from the manifest digest and
does not rebuild the image.

Never delete or repoint an exact version or commit-SHA tag as routine recovery. If a
published candidate is wrong, correct the source and issue a new patch or prerelease
version. An image-only rollback remains valid only when the release has no database
change and exact schema history is unchanged. Every schema-version change currently
requires the database restore path in the separate operations runbook. PostgreSQL major
upgrades are never part of an application release.
