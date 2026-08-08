-- Canonical runtime migration: packages/persistence/migrations/001_initial.sql
-- This copy is kept for operators and schema review. The migration runner executes
-- the packaged file and records its SHA-256 checksum in schema_migrations.
\ir ../packages/persistence/migrations/001_initial.sql
