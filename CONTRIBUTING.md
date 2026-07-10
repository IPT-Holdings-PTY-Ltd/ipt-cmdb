# Contributing to IPT CMDB

## Before opening a pull request

- Keep provider integrations read-only by default.
- Never commit customer data, API keys, database backups, `.env` files, or screenshots containing sensitive details.
- Preserve company scoping on every API read and write.
- Add or update tests for Python behaviour changes.
- Run `python -m unittest discover -s test -v`.

## Design principles

- The canonical CMDB record owns the stable internal ID.
- Provider IDs are mappings, not primary keys.
- Names are mutable; match on stable identifiers where possible.
- Sync changes must be reviewable, idempotent, and auditable.
- Secret systems such as Passportal contribute metadata only.

## Pull requests

Use a focused branch and explain the user impact, security implications, and validation performed. Changes that introduce external writes need an explicit approval/review control and a safe default.
