CREATE TABLE IF NOT EXISTS resource_versions (
  tenant_id text NOT NULL,
  kind text NOT NULL,
  resource_id text NOT NULL,
  version bigint NOT NULL CHECK (version > 0),
  etag text NOT NULL,
  body jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, kind, resource_id, version)
);

INSERT INTO resource_versions(tenant_id, kind, resource_id, version, etag, body, created_at)
SELECT tenant_id, kind, resource_id, version, etag, body, created_at
FROM resources
ON CONFLICT (tenant_id, kind, resource_id, version) DO NOTHING;

ALTER TABLE resource_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE resource_versions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON resource_versions;
CREATE POLICY tenant_isolation ON resource_versions
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

DO $computeweaver_resource_history_grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'computeweaver_app') THEN
    GRANT SELECT, INSERT ON resource_versions TO computeweaver_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'computeweaver_worker') THEN
    GRANT SELECT, INSERT ON resource_versions TO computeweaver_worker;
  END IF;
END
$computeweaver_resource_history_grants$;
