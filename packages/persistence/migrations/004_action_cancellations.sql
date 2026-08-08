CREATE TABLE IF NOT EXISTS action_cancellations (
  tenant_id text NOT NULL,
  action_id text NOT NULL,
  actor_id text NOT NULL,
  reason text NOT NULL CHECK (length(reason) > 0),
  cancelled_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, action_id)
);

ALTER TABLE action_cancellations ENABLE ROW LEVEL SECURITY;
ALTER TABLE action_cancellations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON action_cancellations;
CREATE POLICY tenant_isolation ON action_cancellations
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

DO $computeweaver_action_cancellation_grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'computeweaver_app') THEN
    GRANT SELECT, INSERT ON action_cancellations TO computeweaver_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'computeweaver_worker') THEN
    GRANT SELECT, INSERT ON action_cancellations TO computeweaver_worker;
  END IF;
END
$computeweaver_action_cancellation_grants$;
