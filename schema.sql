-- ListProof schema (Postgres / Supabase). core.init() runs the same statements idempotently on startup.
-- Tables are prefixed rl_ so they cannot collide with anything already in the project.
CREATE TABLE IF NOT EXISTS rl_jobs(id TEXT PRIMARY KEY, email TEXT, status TEXT, input TEXT,
    output TEXT, failed INTEGER DEFAULT 0, total INTEGER, payment TEXT, created DOUBLE PRECISION,
    updated DOUBLE PRECISION, attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS rl_ledger(ts DOUBLE PRECISION, job_id TEXT, kind TEXT, amount DOUBLE PRECISION, note TEXT);
CREATE TABLE IF NOT EXISTS rl_settings(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS rl_ledger_kind_ts ON rl_ledger(kind, ts);
CREATE INDEX IF NOT EXISTS rl_jobs_status ON rl_jobs(status);

-- Customer data lives in these tables. They are server-side only: lock them away from the Supabase Data API.
ALTER TABLE rl_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE rl_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE rl_settings ENABLE ROW LEVEL SECURITY;
