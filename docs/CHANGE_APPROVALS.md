# Change approval and business-owner sign-off

IPT CMDB can collect accountable approval from the people who own affected business systems. The workflow turns the frozen CMDB impact snapshot into a review package, sends a personal link through the configured Microsoft 365 mailbox, and records the decision against the change.

## Technician workflow

1. Create the change, review its impact, and move it to **Awaiting approval**.
2. Open the change and select **Send approval requests**.
3. Review delivery and decision status in the change dialog. A failed Microsoft Graph submission is visible and does not pretend that the message was delivered.
4. The change becomes **Approved** only when every request in the current batch is approved. One decline ends the batch and moves the change to **Declined**.
5. Download the branded PDF to retain the resulting decision evidence with the change package.

Selecting **Replace approval batch** revokes every outstanding link before new links are generated. Moving the change back to impact review, declining it internally, or cancelling it also revokes outstanding links.

## How approvers are selected

For every impacted business system where sign-off is required, the frozen snapshot resolves:

1. the active primary `signoff_delegate` responsibility;
2. otherwise an active `business_owner` responsibility;
3. otherwise a legacy sign-off or owner field only when that field is itself a valid email address.

The optional change-level **Approver / CAB owner** is included when it contains a valid email address. A person responsible for several systems receives one request listing all of those systems. Sending is blocked when a required system has no email-enabled delegate or owner; correct the contact responsibility and refresh the change impact before requesting approval.

## Security properties

- Approval links use cryptographically random bearer tokens. Only a SHA-256 verifier is stored.
- Raw tokens are sent directly through Microsoft Graph and never stored in PostgreSQL, the durable email outbox, portable backups, logs, or audit values.
- Links are personal, single-use, and expire after 1 to 168 hours (72 hours by default).
- The public page exposes only the requested change context and the business systems in that approver's scope. It does not expose the approver email address, CMDB navigation, other customers, or arbitrary asset records.
- A lifecycle change or replacement batch invalidates pending links.
- Every creation, delivery-state update, revocation, lifecycle transition, and external decision is attributable in the audit ledger. The immutable change revision records decision email, name, role, scope, timestamp, and comments.

Treat approval URLs like password-reset links: require HTTPS outside localhost, do not forward them, and use a short expiry appropriate to the planned change window.

## Deployment prerequisites

- Configure and verify the Microsoft Graph sender under **MSP workspace > Email delivery**.
- Set `PUBLIC_BASE_URL` to the exact external origin, for example `https://cmdb.example.com`. HTTP is accepted only for `localhost`, `127.0.0.1`, or `::1` development origins.
- Ensure the reverse proxy sends HTTPS traffic to the application and does not log query strings containing sensitive links.
- Keep all container replicas on the same canonical PostgreSQL database. Approval state is repository-backed and safe for a scaled FastAPI deployment.

The public route is `/#/approve?token=...`; the API validates the token before returning any change detail.
