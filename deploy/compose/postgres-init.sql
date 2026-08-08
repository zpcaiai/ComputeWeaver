DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'computeweaver_app') THEN
    CREATE ROLE computeweaver_app LOGIN PASSWORD 'local-app-only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'computeweaver_worker') THEN
    CREATE ROLE computeweaver_worker LOGIN PASSWORD 'local-worker-only' NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
  END IF;
END
$roles$;

GRANT CONNECT ON DATABASE computeweaver TO computeweaver_app, computeweaver_worker;
GRANT USAGE ON SCHEMA public TO computeweaver_app, computeweaver_worker;
