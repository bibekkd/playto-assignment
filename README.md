# Playto Payout Engine

Submission for the Playto Founding Engineer challenge. A merchant payout
service that handles the things real money systems get wrong: race
conditions on balance checks, duplicate requests on flaky networks,
illegal state transitions, and worker crashes mid-payout.

- **Backend:** Django + DRF + Postgres + Celery + Redis
- **Frontend:** React + Vite + Tailwind v4 (single page, polled every 2s)
- **Deploy:** Render (web + Postgres + Redis), Vercel (frontend), GitHub Actions cron (free worker substitute)

The four documents that matter:

| File | What's in it |
|---|---|
| **[planning.md](planning.md)** | Architecture decisions made BEFORE writing code. Read this first. |
| **[EXPLAINER.md](EXPLAINER.md)** | Answers the 5 graded questions with real code paste-ins. |
| **[DEPLOYMENT.md](DEPLOYMENT.md)** | Manual click-through deploy guide for Render + Vercel + UptimeRobot. |
| **[COMMANDS.md](COMMANDS.md)** | One-liner cheat sheet for local dev. |

The day-by-day build log is in [DAY1.md](DAY1.md), [DAY2.md](DAY2.md), [DAY3.md](DAY3.md), and the deploy phase is captured in [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Quick start (local)

Prereqs: Python 3.11+, Postgres, Redis, Node 20+, [`uv`](https://github.com/astral-sh/uv).

```bash
# 1. Install Python deps + create venv
make install

# 2. Create the database (one-time)
createdb playto_payout

# 3. Apply migrations
make migrate

# 4. Seed 3 demo merchants with credit history
make seed

# 5. Run the Day 1 invariant + DB CHECK constraint smoke tests
make smoke

# 6. Run the two graded tests (concurrency + idempotency, real Postgres)
make test
```

Two terminals to run the dashboard:

```bash
make dev               # T1: Django on :8000 with eager Celery
make frontend-dev      # T2: Vite on :5173 (proxies /api → :8000)
```

Open `http://localhost:5173/`.

---

## Tour of the repo

```
assignment-playtopay/
├── pyproject.toml           # uv-managed Python deps
├── uv.lock
├── Makefile                 # short commands (make dev, make test, etc.)
├── .env.example             # all backend env vars documented
├── frontend/.env.example    # frontend env vars (VITE_API_BASE_URL)
│
├── backend/
│   ├── manage.py
│   ├── seed.py              # standalone seed (dev)
│   ├── smoke_test.py        # Day 1 invariant + CHECK constraint checks
│   │
│   ├── playto/
│   │   ├── settings.py      # env-driven, prod-ready
│   │   ├── celery.py
│   │   └── urls.py
│   │
│   ├── ledger/              # Merchant + LedgerEntry + balance query
│   │   ├── models.py        # the heart of money integrity
│   │   └── management/commands/seed_demo.py
│   │
│   ├── payouts/             # Payout + lock + state machine + worker
│   │   ├── models.py
│   │   ├── views.py         # POST /payouts (the load-bearing endpoint)
│   │   ├── state.py         # transition() guard
│   │   ├── tasks.py         # process_payout, retry_stuck, drain
│   │   ├── serializers.py
│   │   ├── urls.py
│   │   └── management/commands/drain_payouts.py
│   │
│   ├── idempotency/         # token replay + 24h TTL
│   │   ├── models.py
│   │   └── service.py       # claim() + complete()
│   │
│   └── tests/
│       ├── test_concurrency.py    # GRADED TEST 1
│       └── test_idempotency.py    # GRADED TEST 2
│
└── frontend/
    ├── vite.config.ts        # /api proxy to Django, Tailwind v4 plugin
    └── src/
        ├── App.tsx           # the single-page dashboard
        ├── api.ts            # typed API client
        └── format.ts         # paise → ₹ display helpers
```

---

## Architecture in one diagram

```
┌──────────────────┐   HTTP    ┌─────────────────────┐    ┌──────────────┐
│  React + Vite    │──────────▶│  Django + DRF       │◀──▶│  PostgreSQL  │
│  Tailwind        │           │  /api/v1/...        │    │  ledger,     │
│  polls every 2s  │           │                     │    │  payouts,    │
└──────────────────┘           └──────────┬──────────┘    │  idempotency │
                                          │               └──────────────┘
                                          │ enqueue              ▲
                                          ▼                      │
                               ┌─────────────────────┐           │
                               │  Celery worker      │───────────┘
                               │   process_payout    │  SELECT … FOR UPDATE
                               │   retry_stuck       │  in transaction.atomic()
                               └──────────┬──────────┘
                                          │
                               ┌──────────▼──────────┐
                               │  Redis (broker)     │
                               └─────────────────────┘
```

Key invariants enforced at the database, not in Python:

- `CHECK (amount_paise > 0)` on `ledger_entry` and `payout`
- `CHECK (entry_type IN ('CREDIT', 'DEBIT', 'REVERSAL'))`
- `CHECK (status IN ('pending', 'processing', 'completed', 'failed'))`
- `UNIQUE (merchant_id, key)` on `idempotency_key`

Read [EXPLAINER.md](EXPLAINER.md) for the rest.

---

## Money correctness

- **All amounts are paise (BigInteger).** No floats, no Decimals. Math is integer; rendering to rupees is render-only and never feeds back into computation.
- **Balance is derived, not stored.** A `Merchant` has no `balance` column — it's a single-query `CASE` aggregate over the append-only ledger. The invariant `sum(entries) == displayed_balance` is structural, not enforced by application code.
- **The ledger has exactly 3 entry types**: `CREDIT` (customer payment), `DEBIT` (payout request — funds leave available immediately), `REVERSAL` (counter-entry on payout failure). Locked. Will not be expanded.
- **Concurrency:** `SELECT ... FOR UPDATE` on the merchant row inside `transaction.atomic()`. Balance is computed AFTER the lock is held. Two simultaneous overdraw requests result in exactly one success and one clean 422.
- **Idempotency:** composite-unique `(merchant_id, key)`. 24-hour TTL enforced as a read-side filter. Replay returns byte-identical bytes (response stored as `TextField`, not `jsonb`, so key order is preserved). Same key + different body → 422.
- **State machine:** `pending → processing → (completed | failed)`. Anything else is rejected. Failure transition + REVERSAL ledger entry happen in one `transaction.atomic()` — a worker crash between them is impossible.

---

## Tests

Two graded tests, both against real Postgres:

```bash
make test
```

```
test_two_concurrent_payouts_only_one_succeeds ... ok
test_different_keys_create_different_payouts ... ok
test_same_key_different_body_is_rejected ... ok
test_same_key_same_body_returns_byte_identical_response ... ok
```

The concurrency test uses `TransactionTestCase` (not `TestCase`) and a
`threading.Barrier(2)` so both requests arrive at the lock at the same
instant — a `TestCase` would mask the bug under one wrapping transaction.
Details in [tests/test_concurrency.py](backend/tests/test_concurrency.py).

---

## Demo: how to watch it work

```bash
make dev SETTLEMENT=0.0     # force every payout to succeed
make dev SETTLEMENT=0.8     # force every payout to fail (demos REVERSAL)
make dev SETTLEMENT=0.95    # force every payout to hang (demos retry sweep)
```

The simulated bank settlement uses a single env knob:
- `< 0.7` → success
- `0.7 ≤ x < 0.9` → failure (writes REVERSAL atomically with the failed transition)
- `≥ 0.9` → hang (the retry sweep picks it up after 30s)

---

## Deployment

Production is on **Render free tier** (web + Postgres + Redis) plus
**Vercel** for the frontend, with a **GitHub Actions cron** workflow
(`.github/workflows/drain.yml`) hitting a token-protected
`/api/v1/internal/drain` endpoint every 5 minutes to substitute for the
paid-only Background Worker. Full click-by-click guide in
[DEPLOYMENT.md](DEPLOYMENT.md).

The trade-off: payout latency is up to 5 minutes in production (the time
between cron runs). Mention this if asked. To demo without the wait, fire
the workflow manually from the GitHub Actions tab → "Run workflow", or
curl the endpoint directly with the drain token.

---

## What's deliberately out of scope

Per the brief, these were not built:

- Authentication / login (the merchant id is in the URL)
- Real bank API integration (settlement is simulated with a seedable RNG)
- Multi-currency, wallets, multi-account abstractions
- Notifications / emails / webhooks
- Microservices, Kafka, GraphQL, WebSockets

[planning.md §16](planning.md) lists the full guardrails.

---

## Bonuses

- **`docker-compose.yml`** — not added (the Make + uv setup is faster to onboard).
- **Audit log** — implicit. The append-only ledger IS the audit log for money movement.
- **Webhook delivery / event sourcing** — skipped per planning §15.

---

## Submission

- GitHub: this repo
- Live frontend: `https://<your-vercel-url>`
- Live API: `https://<your-render-url>.onrender.com/api/v1/`
- Most proud of: the discipline of computing balance only AFTER the lock is held, and storing the idempotency response as raw `TextField` (not `jsonb`) so replays are byte-identical. Both are subtle and both came from catching the AI being wrong — see EXPLAINER.md §5.
