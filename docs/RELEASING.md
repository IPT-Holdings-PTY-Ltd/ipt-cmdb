# Releasing IPT CMDB

Releases are created deliberately from the latest commit on the repository's default branch. The **Create release** workflow reuses the normal CI suite, waits at the protected `release` environment, publishes the multi-architecture GHCR image, attaches provenance, and then creates the GitHub tag and release.

## One-time repository setup

1. In **Settings → Environments**, open or create the `release` environment.
2. Add at least one required reviewer. Limit deployment branches to `main` if the repository policy permits it.
3. In **Settings → Actions → General**, leave workflow permissions read-only by default. The release workflow requests only its explicit `contents`, `packages`, `id-token`, and `attestations` permissions.
4. Optionally enable immutable releases in the repository release settings after the first successful rehearsal.

No registry password or personal access token is required. Publishing uses the workflow-scoped `GITHUB_TOKEN` and GitHub's OIDC-backed artifact attestation.

## Prepare a release pull request

1. Choose a semantic version such as `0.3.0`. Use a suffix such as `0.3.0-rc.1` for a prerelease.
2. Update `package.json` and `package-lock.json` to that exact version:

   ```powershell
   npm version 0.3.0 --no-git-tag-version
   ```

3. Move the relevant entries from `## [Unreleased]` in `CHANGELOG.md` into a dated heading such as:

   ```markdown
   ## [0.3.0] - 2026-07-21
   ```

4. Keep a new empty `## [Unreleased]` section for subsequent work.
5. Open and merge the release pull request after CI succeeds.

## Create the release

1. Open **Actions → Create release → Run workflow**.
2. Enter the version without or with the leading `v` (`0.3.0` and `v0.3.0` are equivalent).
3. Review the validation and CI jobs.
4. Approve the `release` environment deployment when satisfied.

The workflow refuses malformed versions, existing tags, package-version mismatches, and missing changelog sections. It publishes:

- `ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:0.3.0`;
- `ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:0.3`;
- `ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:latest` for stable releases only;
- a commit-SHA image tag;
- an SBOM, registry provenance, GitHub artifact attestation and release manifest;
- a GitHub release with categorized generated notes.

Prerelease versions such as `0.3.0-rc.1` are marked as GitHub prereleases and never replace `latest`.

## Rehearsal and recovery

Use a prerelease version for the first end-to-end rehearsal. The release workflow is intentionally not retryable with the same tag once the GitHub release exists. If image publishing succeeds but release creation fails, inspect the failed job before retrying; delete or retag published artifacts only after confirming they are not in use.

Consumers can pin either the semantic version or the immutable digest recorded in the attached `release-manifest.txt`. Production deployments should prefer the digest.
