# Notification rules and delivery

The MSP notification center turns canonical CMDB events into traceable Microsoft
365 email without coupling business rules directly to Graph. It is available to
`platform_admin` users in **MSP workspace > Operations > Notifications**.

## Operational flow

1. A rule evaluates lifecycle, ownership, or change-control data.
2. Active contact responsibilities resolve the intended recipient roles.
3. Contact preferences can suppress email or limit selected event types.
4. A versioned template renders escaped CMDB values.
5. A deduplicated event and provider-neutral outbox message are committed.
6. An explicit admin action or the optional worker claims and sends due messages.
7. Acceptance, retry, missing-recipient, and dead-letter states remain as evidence.

Evaluation and delivery are deliberately separate in the UI. **Evaluate rules**
does not contact anyone. **Send due email** displays a confirmation because it can
send to real customer recipients.

## Included rules

| Rule | Default cadence | Default recipients |
|---|---|---|
| Asset/subscription renewal | Daily, 90-day lead | Business, service, technical owner or custodian |
| Asset end of life | Daily, 180-day lead | Business, service, technical owner or custodian |
| Change approval required | Immediate | Change approver, sign-off delegate or business owner |
| Missing CI owner | Weekly | Support contact |

Global rules apply to every customer. The schema also supports customer-specific
rule and template overrides for a later delegated-administration release.

Fallback addresses are an exception path, not a substitute for structured owner
data. Missing-recipient events stay visible on the notification dashboard so the
MSP can correct contact or responsibility records.

## Retry and dead-letter handling

Workers claim rows with PostgreSQL `FOR UPDATE SKIP LOCKED`, so multiple application
replicas cannot send the same due row concurrently. A stale `sending` claim can be
recovered after 15 minutes. Failed sends use exponential backoff, capped at six
hours, until the rule's maximum attempt count is reached. The row then moves to
`dead_letter` and requires an administrator to requeue it after resolving the cause.

Microsoft Graph `202 Accepted` is recorded as `accepted`; it is not a mailbox
delivery receipt. Provider request IDs are retained for support correlation.

## Enabling background delivery

Automatic sending is off by default for every deployment:

```text
NOTIFICATION_WORKER_ENABLED=false
NOTIFICATION_WORKER_INTERVAL_SECONDS=60
```

Before enabling it:

- complete and verify the Microsoft 365 sender setup;
- review all enabled rules, templates, lifecycle dates and fallback addresses;
- resolve the notification center's missing-recipient findings;
- test with a controlled customer and recipient;
- take a database backup.

Then set `NOTIFICATION_WORKER_ENABLED=true` and restart the application container.
The interval is constrained to 15-3600 seconds. Cadence controls prevent daily and
weekly rules from being evaluated every worker cycle.

## Recipient preferences

Preferences are stored separately from contacts so future ConnectWise or other
contact syncs do not overwrite local communication choices. `emailEnabled=false`
suppresses the contact without removing ownership. `eventTypes` can contain `*` or
selected event keys. Daily and weekly digest modes are reserved in the schema and
UI; current delivery is immediate once an event is queued and processed.

Portable backups include rules, templates, and preferences, but exclude event
evidence, email bodies, the outbox, and installation-bound credentials.
