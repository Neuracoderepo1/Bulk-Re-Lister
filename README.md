# Bulk Re-Lister

Autonomous listing rewriter: CSV in -> Stripe payment -> writer + checker -> CSV out. Nothing runs until payment clears.

## Run locally
1. `pip install -r requirements.txt`; copy `.env.example` to `.env` and fill it in (use Stripe **test** keys; set `MODEL`).
2. `python tests/test_core.py` (offline; no keys needed)
3. `set -a; . ./.env; set +a; uvicorn main:app --workers 1 --proxy-headers`
4. `stripe listen --forward-to localhost:8000/webhook`, paste the `whsec_` secret into `.env`, order with test card 4242 4242 4242 4242.

## Operate
- Kill switch: `POST /admin/intake?open=0` with header `X-Admin-Token`.
- Review flow: `GET /admin/jobs` lists `needs_owner` jobs; `GET /admin/job/{id}`; `POST /admin/job/{id}/release` or `/refund`.
- Spend is reserved before each model call; stuck jobs are requeued (max 3 attempts); customer data is purged after `RETENTION_DAYS`.
- Lock dependencies before deploy: `pip-compile --generate-hashes requirements.txt -o requirements.lock`; run `pip-audit`.

## Database
Current code uses SQLite (`DB_PATH`). The Supabase schema (jobs, ledger, settings, atomic `reserve_budget`) is applied; the Postgres port of `core.py` is the next step.

## Before taking real money
Verify Stripe/Anthropic/SMTP end to end in test mode, run 10 manual jobs to validate demand, publish terms/privacy/refund policy, and register the business and tax details.
