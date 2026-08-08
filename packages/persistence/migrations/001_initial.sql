CREATE TABLE IF NOT EXISTS resources (
  tenant_id text NOT NULL,
  kind text NOT NULL,
  resource_id text NOT NULL,
  version bigint NOT NULL CHECK (version > 0),
  etag text NOT NULL,
  body jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, kind, resource_id)
);

CREATE TABLE IF NOT EXISTS events (
  event_id text PRIMARY KEY,
  tenant_id text NOT NULL,
  site_id text NOT NULL,
  event_type text NOT NULL,
  schema_version text NOT NULL,
  occurred_at timestamptz NOT NULL,
  observed_at timestamptz NOT NULL,
  trace_id text NOT NULL,
  payload jsonb NOT NULL,
  payload_hash text NOT NULL
);
CREATE INDEX IF NOT EXISTS events_tenant_time ON events (tenant_id, occurred_at);

CREATE TABLE IF NOT EXISTS raw_events (
  tenant_id text NOT NULL,
  event_id text NOT NULL,
  source text NOT NULL,
  received_at timestamptz NOT NULL,
  payload jsonb NOT NULL,
  payload_hash text NOT NULL,
  PRIMARY KEY (tenant_id, event_id)
);

CREATE TABLE IF NOT EXISTS timeseries_points (
  tenant_id text NOT NULL,
  metric text NOT NULL,
  observed_at timestamptz NOT NULL,
  value numeric NOT NULL,
  unit text NOT NULL,
  source text NOT NULL,
  source_event_id text NOT NULL,
  raw_payload_hash text NOT NULL,
  transformation text NOT NULL,
  PRIMARY KEY (tenant_id, metric, observed_at)
);
CREATE INDEX IF NOT EXISTS timeseries_tenant_metric_time
  ON timeseries_points (tenant_id, metric, observed_at);

CREATE TABLE IF NOT EXISTS audit_records (
  sequence bigserial PRIMARY KEY,
  tenant_id text NOT NULL,
  actor_id text NOT NULL,
  action text NOT NULL,
  resource text NOT NULL,
  outcome text NOT NULL,
  correlation_id text NOT NULL,
  previous_hash text NOT NULL,
  record_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_tenant_sequence ON audit_records (tenant_id, sequence);

CREATE TABLE IF NOT EXISTS idempotency_records (
  tenant_id text NOT NULL,
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  response_status integer NOT NULL,
  response_body jsonb NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS durable_jobs (
  id bigserial PRIMARY KEY,
  tenant_id text NOT NULL,
  kind text NOT NULL,
  payload jsonb NOT NULL,
  idempotency_key text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'dead_letter')),
  attempt integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
  available_at timestamptz NOT NULL DEFAULT now(),
  leased_by text,
  lease_expires_at timestamptz,
  last_error text,
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS durable_jobs_claim
  ON durable_jobs (status, available_at, lease_expires_at, id);

CREATE TABLE IF NOT EXISTS approvals (
  id text NOT NULL,
  tenant_id text NOT NULL,
  plan_id text NOT NULL,
  risk integer NOT NULL,
  requested_by text NOT NULL,
  expires_at timestamptz NOT NULL,
  required_roles text[] NOT NULL,
  required_count integer NOT NULL,
  status text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
  version bigint NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
  ,PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS approval_votes (
  tenant_id text NOT NULL,
  approval_id text NOT NULL,
  actor_id text NOT NULL,
  role text NOT NULL,
  decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
  decided_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, approval_id, actor_id),
  FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS action_executions (
  tenant_id text NOT NULL,
  idempotency_key text NOT NULL,
  action_id text NOT NULL,
  intent_hash text NOT NULL,
  status text NOT NULL CHECK (status IN ('started', 'succeeded', 'failed', 'compensated')),
  result jsonb,
  error text,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS api_operations (
  tenant_id text NOT NULL,
  idempotency_key text NOT NULL,
  operation text NOT NULL,
  intent_hash text NOT NULL,
  status text NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
  response jsonb,
  error text,
  lease_expires_at timestamptz NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS connector_offsets (
  tenant_id text NOT NULL,
  connector_id text NOT NULL,
  stream text NOT NULL,
  cursor_value text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, connector_id, stream)
);

CREATE TABLE IF NOT EXISTS topology_drafts (
  tenant_id text PRIMARY KEY,
  revision bigint NOT NULL CHECK (revision > 0),
  assets jsonb NOT NULL,
  relationships jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS topology_versions (
  tenant_id text NOT NULL,
  version bigint NOT NULL CHECK (version > 0),
  assets jsonb NOT NULL,
  relationships jsonb NOT NULL,
  published_at timestamptz NOT NULL,
  etag text NOT NULL,
  PRIMARY KEY (tenant_id, version)
);

CREATE TABLE IF NOT EXISTS policy_versions (
  tenant_id text NOT NULL,
  policy_id text NOT NULL,
  version integer NOT NULL,
  site_ids text[] NOT NULL,
  rule jsonb NOT NULL,
  enforcement text NOT NULL CHECK (enforcement IN ('hard', 'soft')),
  priority integer NOT NULL,
  owner_id text NOT NULL,
  published boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, policy_id, version)
);

CREATE TABLE IF NOT EXISTS model_versions (
  tenant_id text NOT NULL,
  name text NOT NULL,
  version text NOT NULL,
  artifact_hash text NOT NULL,
  dataset_hash text NOT NULL,
  stage text NOT NULL CHECK (stage IN ('registered', 'staging', 'production', 'archived')),
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, name, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS model_one_production_per_tenant
  ON model_versions (tenant_id, name) WHERE stage = 'production';

CREATE TABLE IF NOT EXISTS quota_ledgers (
  tenant_id text PRIMARY KEY,
  max_gpus integer NOT NULL CHECK (max_gpus >= 0),
  max_gpu_hours numeric NOT NULL CHECK (max_gpu_hours >= 0),
  max_concurrent_jobs integer NOT NULL CHECK (max_concurrent_jobs >= 0),
  used_gpus integer NOT NULL DEFAULT 0 CHECK (used_gpus >= 0),
  used_gpu_hours numeric NOT NULL DEFAULT 0 CHECK (used_gpu_hours >= 0),
  active_jobs integer NOT NULL DEFAULT 0 CHECK (active_jobs >= 0),
  version bigint NOT NULL DEFAULT 1,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quota_reservations (
  tenant_id text NOT NULL,
  reservation_key text NOT NULL,
  gpus integer NOT NULL CHECK (gpus >= 0),
  gpu_hours numeric NOT NULL CHECK (gpu_hours >= 0),
  released_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, reservation_key),
  FOREIGN KEY (tenant_id) REFERENCES quota_ledgers(tenant_id) ON DELETE RESTRICT
);

DO $computeweaver_rls$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'resources', 'events', 'raw_events', 'timeseries_points', 'audit_records',
    'idempotency_records', 'durable_jobs', 'approvals', 'approval_votes',
    'action_executions', 'api_operations', 'connector_offsets', 'topology_drafts', 'topology_versions',
    'policy_versions', 'model_versions', 'quota_ledgers', 'quota_reservations'
  ]
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', table_name);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
      table_name
    );
  END LOOP;
END
$computeweaver_rls$;

DO $computeweaver_grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'computeweaver_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO computeweaver_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO computeweaver_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'computeweaver_worker') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO computeweaver_worker;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO computeweaver_worker;
  END IF;
END
$computeweaver_grants$;
