# ListPilot (Bulk Re-Lister)

Autonomous listing rewriter: CSV in -> Stripe payment -> writer + checker -> CSV out. Nothing runs until payment clears.

## Run locally
1. `pip install -r requirements.txt`; copy `.env.example` to `.env` and fill it in (use Stripe **test** keys; set `MODEL`).
2. `python tests/test_core.py` (offline; no keys needed)
3. `set -a; . ./.env; set +a; uvicorn main:app --workers 1 --proxy-headers`
4. `stripe listen --forward-to localhost:8000/webhook`, paste the `whsec_` secret into `.env`, order with test card 4242 4242 4242 4242.

## Operate
- Kill switch: `POST /admin/intake?open=0` with header `X-Admin-Token`.
- Review flow: `GET /healthz` for uptime checks (503 if the DB is down). `GET /admin/jobs` lists `needs_owner` jobs; `GET /admin/job/{id}`; `POST /admin/job/{id}/release` or `/refund`.
- Spend is reserved before each model call; stuck jobs are requeued (max 3 attempts); customer data is purged after `RETENTION_DAYS`.
- Lock dependencies before deploy: `pip-compile --generate-hashes requirements.txt -o requirements.lock`; run `pip-audit`.

## Database
Set `DATABASE_URL` to use Postgres (use Supabase's **transaction pooler** URI, port 6543); leave it empty to use local SQLite at `DB_PATH`. Tables are `rl_jobs`, `rl_ledger`, `rl_settings` (prefixed so they can't collide with anything already in the project); `core.init()` creates them idempotently on startup, and `schema.sql` is the same DDL plus row-level security (these tables are server-side only; don't expose them through the Data API). The spend cap is enforced with a Postgres advisory lock, verified under 40-thread contention.

Run tests on either backend: `python tests/test_core.py && python tests/test_web.py` (set `DATABASE_URL` to a throwaway Postgres to test that path).

## Before taking real money
Verify Stripe/Anthropic/SMTP end to end in test mode and run 10 manual jobs to validate demand. Set `BUSINESS_NAME` and `SUPPORT_EMAIL`; `/terms`, `/privacy` and `/refund-policy` are plain-language **drafts** that need review by a lawyer for your jurisdiction. Register the business and tax details. Deploy with the hashed lockfile (`pip install --require-hashes -r requirements.lock`), a single worker, and a persistent database (Supabase).
