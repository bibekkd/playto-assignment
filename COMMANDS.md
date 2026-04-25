# Commands

Short cheat-sheet for running the service. All commands run from the repo root.

## One-time setup

```bash
make install            # install Python deps into .venv via uv
make migrate            # apply Django migrations
make seed               # populate 3 demo merchants
make frontend-install   # install JS deps for the dashboard
```

If you don't have `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Day-to-day

```bash
# Two terminals — backend + frontend
make dev                # T1: Django on :8000, eager Celery (no Redis needed)
make frontend-dev       # T2: Vite on :5173, proxies /api to Django
```

Open `http://localhost:5173/` for the dashboard.

```bash
make test               # run the two graded tests
make smoke              # Day 1 invariant + CHECK constraint checks
make frontend-build     # production build into frontend/dist
```

The Django dev server runs on `http://127.0.0.1:8000` by default. Override with `make dev PORT=8765`.

## Forcing the bank settlement outcome

`PAYOUT_SETTLEMENT_FORCE` controls the simulated bank roll. Threshold logic:
`< 0.7` → success, `< 0.9` → failure (writes REVERSAL), `>= 0.9` → hang (sweep retries).

```bash
make dev SETTLEMENT=0.0     # every payout succeeds
make dev SETTLEMENT=0.8     # every payout fails — verify REVERSAL restores balance
make dev SETTLEMENT=0.95    # every payout hangs — verify retry sweep
make test-fail-roll         # run tests with all rolls forced to fail
```

## Real worker mode (production-shaped)

Needs Redis running on `localhost:6379`.

```bash
make dev            # leave running in one terminal
make worker         # second terminal: real Celery worker
make beat           # third terminal: scheduled retry sweep
```

In dev mode (`make dev` alone), Celery runs eagerly inside the web process so you don't need a worker for the demo to work end to end.

## Try it via curl

```bash
# List merchants and grab one's id
curl -s localhost:8000/api/v1/merchants | python3 -m json.tool

MID=<paste a merchant uuid here>
KEY=$(uuidgen | tr A-Z a-z)
BANK=$(uuidgen | tr A-Z a-z)

# Create a payout
curl -s -X POST localhost:8000/api/v1/merchants/$MID/payouts \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d "{\"amount_paise\": 50000, \"bank_account_id\": \"$BANK\"}"

# Replay — same key + body — bytes are identical to the first response
curl -s -X POST localhost:8000/api/v1/merchants/$MID/payouts \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d "{\"amount_paise\": 50000, \"bank_account_id\": \"$BANK\"}"

# Same key + different body → 422
curl -s -i -X POST localhost:8000/api/v1/merchants/$MID/payouts \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d "{\"amount_paise\": 1, \"bank_account_id\": \"$BANK\"}" | head -1

# View the payout list
curl -s localhost:8000/api/v1/merchants/$MID/payouts/list | python3 -m json.tool
```

## Cleanup

```bash
make clean          # delete .venv and uv.lock
```

## Reference: full command list

```
make help               print this list with current SETTLEMENT/PORT values
make install            uv sync
make migrate            manage.py migrate
make seed               backend/seed.py
make smoke              backend/smoke_test.py
make dev                runserver, eager Celery
make worker             celery -A playto worker
make beat               celery -A playto beat
make test               manage.py test tests
make test-fail-roll     same as test, but every roll forced to failure
make shell              manage.py shell
make frontend-install   npm install in frontend/
make frontend-dev       vite dev server on :5173 (proxies /api → :8000)
make frontend-build     production build into frontend/dist
make clean              rm -rf .venv uv.lock node_modules dist
```
