## What changed

Describe the user-facing outcome and the reason for the change.

## Scope and safety

- [ ] API tenant and role checks remain enforced.
- [ ] No credentials, customer data, exports or generated build output are included.
- [ ] Provider writes are absent or explicitly previewed, confirmed, idempotent and audited.
- [ ] Database changes use a new forward-only migration; applied migrations were not edited.
- [ ] Documentation and changelog were updated where appropriate.

## Validation

- [ ] `python -m pre_commit run --all-files`
- [ ] `python -m coverage run -m unittest discover -s test -v` and `python -m coverage report`
- [ ] `npm test`
- [ ] `npm run typecheck`
- [ ] `npm run build`
- [ ] PostgreSQL blank bootstrap/upgrade checks when schema or repository code changed
- [ ] Manual tenant-boundary and UI verification where relevant

## Screenshots or API examples

Use demo data only. Remove internal names, tokens, headers and customer information.
