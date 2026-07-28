# Background worker deployment and monitoring

IPT CMDB uses the same image and PostgreSQL repository for web requests and background
work. The default remains one low-cost application process. Larger installations can
move enabled jobs into a dedicated process without changing policies, review queues,
email outbox records or provider policy leases.

## Process roles

`CMDB_PROCESS_ROLE` accepts:

| Role | Behaviour | Recommended use |
|---|---|---|
| `combined` | FastAPI plus enabled background loops | Small MSP or dedicated customer instance |
| `web` | FastAPI only; never starts embedded loops | Web replica beside a dedicated worker |
| `worker` | Background process started with `python -m backend.worker` | Dedicated Compose or Container Apps worker |

The default is `combined`, so existing deployments remain compatible. Worker feature
flags remain explicit:

```text
INTEGRATION_WORKER_ENABLED=true
NOTIFICATION_WORKER_ENABLED=true
```

The integration worker performs read-only ConnectWise and N-central previews and takes
the existing PostgreSQL policy lease. The notification worker evaluates rules and
claims durable outbox messages. Neither process role enables provider writes or
bypasses review.

## Safe cutover sequence

On a blank database, start the web service first and require `/api/ready` to pass. This
lets one process apply forward-only migrations and seed the first administrator. Then
enable the dedicated worker topology. Do not start several cold processes against an
uninitialized database.

To return to a single process, stop the dedicated worker first, set the web process to
`combined`, and recreate it. Existing policy leases expire safely; do not delete lease,
review, outbox or telemetry rows.

## Docker Compose

For an external database deployment:

```powershell
docker compose --env-file .env.production `
  -f compose.production.yml -f compose.worker.yml config --quiet
docker compose --env-file .env.production `
  -f compose.production.yml -f compose.worker.yml up -d
docker compose --env-file .env.production `
  -f compose.production.yml -f compose.worker.yml ps
```

For the compact appliance, use the same overlay after the initial web/database startup:

```powershell
docker compose --env-file .appliance\customer-acme\.env.appliance `
  -f compose.appliance.yml -f compose.worker.yml up -d
```

The overlay changes the `cmdb` service to `web`, creates a non-ingress `worker` service,
and applies the same read-only filesystem, dropped capabilities and secret mounts. Both
services must use the same immutable `CMDB_IMAGE` tag or digest.

Run one controlled cycle without leaving a worker running:

```powershell
docker compose --env-file .env.production `
  -f compose.production.yml -f compose.worker.yml `
  run --rm worker python -m backend.worker --once
```

The command exits `0` when every enabled job succeeds, `1` when a job fails, and `2`
when no jobs are enabled. This is suitable for an orchestrator-scheduled job.

## Azure Container Apps

The Bicep deployment always provisions a non-ingress worker Container App. It scales to
zero by default. Set the following azd value before reprovisioning an initialized
environment:

```powershell
azd env set CMDB_WORKER_DEPLOYMENT_MODE dedicated
azd env set CMDB_ENABLE_NOTIFICATION_WORKER true
azd env set CMDB_ENABLE_INTEGRATION_WORKER true
azd provision
azd deploy api
azd deploy worker
```

`workerDeploymentMode=dedicated` changes the API process to `web`, enables one worker
replica, and routes enabled jobs only to the worker. `embedded` leaves the worker at
zero replicas and retains the original single-process topology.

For a scheduled Container Apps Job instead of a continuously warm worker, use the same
image with command `python -m backend.worker --once`, a schedule longer than the worst
case cycle, and concurrency `1`. Keep the continuous worker scaled to zero in that
topology.

## Heartbeats and rate limits

Every worker start, cycle, success, failure and stop updates `worker_runtime_status`.
The integration and notification screens show:

- embedded, dedicated or one-shot execution mode;
- heartbeat freshness and last success;
- completed cycles and processed-item count;
- the latest sanitized failure.

ConnectWise and N-central requests update `provider_rate_limit_status` with HTTP status,
quota limit, remaining quota, reset/retry headers and URL path. Query strings, response
bodies and credentials are never stored. Providers that omit quota headers still expose
their latest HTTP status; the UI does not invent a limit.

Alert when a continuously configured worker has no heartbeat for the larger of two
minutes or three polling intervals. A one-shot worker is healthy only when its most
recent fresh run succeeded.

## Operational checks

```powershell
docker compose -f compose.production.yml -f compose.worker.yml logs --tail 200 worker
Invoke-RestMethod https://cmdb.example.com/api/health
```

Also review Administration > Integrations and Administration > Notifications. A
configured worker without a fresh heartbeat is an operational fault even when the web
readiness endpoint remains healthy.
