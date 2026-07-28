-- Durable worker liveness and sanitized provider rate-limit observations.
CREATE TABLE IF NOT EXISTS worker_runtime_status (
    worker_name text PRIMARY KEY,
    worker_id text NOT NULL,
    deployment_mode text NOT NULL
        CHECK (deployment_mode IN ('embedded', 'dedicated', 'one_shot')),
    status text NOT NULL
        CHECK (status IN ('starting', 'running', 'degraded', 'stopped')),
    interval_seconds integer NOT NULL CHECK (interval_seconds BETWEEN 1 AND 86400),
    last_started_at timestamptz,
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    last_cycle_started_at timestamptz,
    last_cycle_finished_at timestamptz,
    last_success_at timestamptz,
    last_error_at timestamptz,
    last_error text,
    cycles_completed bigint NOT NULL DEFAULT 0 CHECK (cycles_completed >= 0),
    items_processed bigint NOT NULL DEFAULT 0 CHECK (items_processed >= 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS worker_runtime_heartbeat_idx
    ON worker_runtime_status(last_heartbeat_at DESC);

CREATE TABLE IF NOT EXISTS provider_rate_limit_status (
    provider text PRIMARY KEY,
    observed_at timestamptz NOT NULL DEFAULT now(),
    http_status integer CHECK (http_status BETWEEN 100 AND 599),
    limit_value bigint CHECK (limit_value IS NULL OR limit_value >= 0),
    remaining_value bigint CHECK (remaining_value IS NULL OR remaining_value >= 0),
    reset_at text,
    retry_after_seconds integer
        CHECK (retry_after_seconds IS NULL OR retry_after_seconds >= 0),
    limited boolean NOT NULL DEFAULT false,
    request_path text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
