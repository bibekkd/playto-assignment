# Playto Payout Engine — Planning Document

This document captures the architecture and execution plan **before** any code is written. The goal is to make every non-trivial decision deliberate, because the rubric is explicitly graded on *thinking*, not features.

---

## 1. Reading the brief carefully

The brief tells us what is and isn't being graded. Re-stating it so we don't drift:

**Graded heavily:**
- Money integrity (paise as BigInteger, DB-level aggregation, invariant: `sum(credits) - sum(debits) == balance`)
- Concurrency correctness (two simultaneous payouts that together overdraw → exactly one succeeds)
- Idempotency (per-merchant key, 24h TTL, identical response on replay, in-flight handling)
- State machine (only legal transitions; failed→completed must be impossible at the code level)
- EXPLAINER.md quality (must be able to defend every line)
- Honest AI audit

**Not graded:**
- Pixel-perfect UI
- High test coverage (just 2 *meaningful* tests: concurrency + idempotency)
- Feature breadth beyond what's listed
- Auth, multi-tenancy beyond seeded merchants

**Implication:** spend time on the ledger schema, the lock, the idempotency table, and the state-transition guard. Do not over-build the frontend.

---

## 2. Architecture at a glance

```
┌──────────────┐      ┌─────────────────────┐      ┌──────────────┐
│ React + Tail │─────▶│ Django + DRF        │◀────▶│ PostgreSQL   │
│ Dashboard    │      │  /api/v1/payouts    │      │  ledger,     │
└──────────────┘      │  /api/v1/merchants… │      │  payouts,    │
                      └──────────┬──────────┘      │  idempotency │
                                 │                 └──────────────┘
                                 │  enqueue                ▲
                                 ▼                         │
                      ┌─────────────────────┐              │
                      │ Celery worker       │──────────────┘
                      │  process_payout     │   SELECT … FOR UPDATE
                      │  retry_stuck_payouts│
                      └──────────┬──────────┘
                                 │
                      ┌──────────▼──────────┐
                      │ Redis (broker +     │
                      │ celery-beat sched)  │
                      └─────────────────────┘
```

- **Django + DRF** for the API. It's specified.
- **PostgreSQL** for `SELECT … FOR UPDATE` row locks and `SERIALIZABLE`-capable transactions. SQLite would not let us demonstrate the lock primitive honestly.
- **Celery + Redis** for the background worker and **celery-beat** for the periodic "retry stuck payouts" sweep. Celery is the most defensible choice; the brief says "do not fake it with sync code."
- **React + Vite + Tailwind** for the dashboard. Polled status updates (every 2s) — WebSockets are out of scope.

---

## 3. Data model

All amounts are `BigIntegerField` storing **paise**. No floats, no decimals.

### `Merchant`
| field | type | notes |
|---|---|---|
| id | UUID PK | |
| name | str | |
| created_at | timestamp | |

Balance is **never** stored on the merchant row. It's derived. (See §4.)

### `LedgerEntry`
The single source of truth for money movement. Append-only.

**Exactly three entry types — locked, will not be expanded:**

| type | sign | when written |
|---|---|---|
| `CREDIT` | + | simulated customer payment |
| `DEBIT` | − | payout requested (the "hold" — funds leave available immediately) |
| `REVERSAL` | + | payout failed; counter-entry to a prior DEBIT |

| field | type | notes |
|---|---|---|
| id | UUID PK | |
| merchant_id | FK → Merchant | indexed |
| entry_type | enum: `CREDIT`, `DEBIT`, `REVERSAL` | DB CHECK constraint enforces enum |
| amount_paise | BigInteger | **DB CHECK: `amount_paise > 0`** — sign comes from `entry_type`, never from the amount |
| payout_id | FK → Payout, nullable | links DEBIT/REVERSAL entries to their payout |
| created_at | timestamp | |

**Indexes:**
- Composite `(merchant_id, created_at DESC)` — covers both balance aggregation and the "recent ledger" dashboard query.
- `(payout_id)` — for joining REVERSAL back to its DEBIT.

**Balance** = `sum(CREDIT) − sum(DEBIT) + sum(REVERSAL)`, computed in **one** SQL query using a `CASE` expression (see §4), not three separate aggregates.

For the dashboard's "held balance", sum DEBITs whose linked payout is still `pending` or `processing`. For "available", use the full ledger sum (which already excludes held funds, because the DEBIT was written at request time).

**Why this model, one sentence for the EXPLAINER:** the ledger is append-only and balance is a pure function of it, so there is no mutable balance column to race on, and the invariant `sum(entries) == displayed_balance` is structurally guaranteed rather than enforced by application code.

### `Payout`
| field | type | notes |
|---|---|---|
| id | UUID PK | |
| merchant_id | FK | indexed |
| amount_paise | BigInteger | |
| bank_account_id | UUID (just an opaque ref; we don't model bank accounts) | |
| status | enum: `pending`, `processing`, `completed`, `failed` | |
| attempts | int, default 0 | for retry cap |
| last_attempt_at | timestamp, nullable | for stuck detection |
| created_at, updated_at | timestamp | |

### `IdempotencyKey`
| field | type | notes |
|---|---|---|
| key | UUID | part of composite PK |
| merchant_id | FK | part of composite PK (keys are scoped per merchant) |
| request_fingerprint | sha256 of (amount, bank_account_id) | to detect "same key, different body" abuse |
| response_status | int | cached HTTP status |
| response_body | JSON | cached response body |
| payout_id | FK, nullable | |
| state | enum: `in_flight`, `completed` | for in-flight handling |
| created_at | timestamp | for 24h expiry |

Composite unique constraint on `(merchant_id, key)` — this is what makes "same key returns same response" enforceable at the DB level.

---

## 4. The Lock — concurrency design

The classic bug: `if balance >= amount: deduct(amount)` evaluated by two workers concurrently, both pass the check, both deduct, balance goes negative.

**Our fix:** in the payout-creation transaction, in **this exact order**:

1. `BEGIN` (`transaction.atomic()`)
2. `SELECT id FROM merchant WHERE id = %s FOR UPDATE` — row-level exclusive lock. Concurrent requests for the same merchant block here.
3. **Only after the lock is held**, compute balance via aggregate query. This ordering is non-negotiable: computing balance before the lock would re-introduce the race we're trying to kill.
4. If `available >= amount`: insert `Payout(status=pending)` + `LedgerEntry(type=DEBIT)` + complete the idempotency row.
5. Else: cache a 422 response on the idempotency row and raise.
6. `COMMIT`.

**Single-query balance** (one round-trip, no Python arithmetic):

```sql
SELECT COALESCE(SUM(
  CASE entry_type
    WHEN 'CREDIT'   THEN  amount_paise
    WHEN 'DEBIT'    THEN -amount_paise
    WHEN 'REVERSAL' THEN  amount_paise
  END
), 0) AS balance_paise
FROM ledger_entry
WHERE merchant_id = %s;
```

Django ORM equivalent uses `Sum(Case(When(...)))`. The reviewer can paste either into psql.

Two simultaneous 60-rupee requests against a 100-rupee balance: the second blocks at step 2, and when it proceeds, its step-3 aggregate sees the first request's DEBIT entry, so `available = 40`, the check fails, it's rejected. **Exactly one succeeds.**

Why `SELECT FOR UPDATE` on the merchant row and not on ledger entries: the merchant row is the natural serialization point for "all money decisions for merchant X." Locking ledger entries doesn't help — new ones are being inserted, not the existing ones that would be locked.

Alternative considered: `SERIALIZABLE` isolation level. Would also work, but requires retry-on-serialization-failure logic everywhere and is harder to explain. Row lock is simpler and sufficient.

---

## 5. Idempotency design

Three cases to handle:

**(a) First request:** insert `IdempotencyKey(state=in_flight)` early in the transaction. If insert succeeds, we own this key. Process normally. Before committing, update to `state=completed` with cached response.

**(b) Replay after completion:** the `INSERT` fails on the unique constraint. We `SELECT` the existing row, check `state=completed`, return the cached response verbatim.

**(c) Replay while first is in-flight:** the `INSERT` fails, we `SELECT` and find `state=in_flight`. Two options:
- Return `409 Conflict` with `Retry-After: 1` — simpler, honest.
- Block waiting on the row — risks tying up a worker.

**Decision:** return 409. Document this in the EXPLAINER. Real systems (Stripe) do something similar.

**Same key, different body:** if `request_fingerprint` doesn't match, return `422` with an explicit error. This is also Stripe's behavior and prevents a class of bugs where a client reuses keys.

**Expiry — two-layer:**
1. **Read-side filter (authoritative):** every idempotency lookup filters `WHERE created_at > now() - interval '24 hours'`. An expired-but-not-yet-swept row is treated as if it doesn't exist. This is the rule that actually enforces TTL — the periodic cleanup is just housekeeping.
2. **Periodic cleanup:** celery-beat task deletes rows older than 24h, hourly. Pure storage hygiene.

---

## 6. State machine

Legal transitions:
```
pending  → processing
processing → completed
processing → failed
processing → processing  (retry; increments attempts)
```

Everything else is illegal, including `pending → completed` (must go through processing) and any backward move.

**Where the guard lives:** a single `Payout.transition_to(new_state)` method that:
1. Takes a `SELECT … FOR UPDATE` on the payout row.
2. Checks `(self.status, new_state)` is in the legal-transitions set.
3. Raises `IllegalTransition` if not.
4. Updates and saves.

Critically, the **failure path** (`processing → failed` + REVERSAL ledger entry) happens in **one transaction**. Either both the state change and the reversal entry commit, or neither does. This is what the brief calls out as "atomically with the state transition."

We also add a Postgres `CHECK` constraint on the status column for the enum values, and rely on the legal-transition set in code for the transition logic. The DB constraint catches typos; the Python check catches illegal flows.

---

## 7. Retry logic

A celery-beat task `retry_stuck_payouts` runs every 10 seconds:

```sql
SELECT id FROM payout
WHERE status = 'processing'
  AND last_attempt_at < now() - interval '30 seconds'
  AND attempts < 3
FOR UPDATE SKIP LOCKED
```

`SKIP LOCKED` means concurrent beat firings don't fight over the same rows.

For each, enqueue `process_payout` with backoff: attempt 2 waits 2s, attempt 3 waits 8s (exponential, base 2). After attempt 3 fails or hangs, transition to `failed` and write the REVERSAL entry.

---

## 8. Simulated bank settlement

Inside `process_payout`:
- `random.random()` < 0.7 → success (transition to completed)
- < 0.9 → failure (transition to failed, write REVERSAL)
- else → "hang" — sleep beyond the 30s threshold without completing, so the retry sweep picks it up

Use a *seedable* RNG so tests are deterministic.

---

## 9. Frontend (deliberately minimal)

One page (`/dashboard/:merchantId`):
- Balance card (available, held)
- Recent ledger entries (last 10)
- Payout request form (amount, bank_account_id dropdown with two seeded accounts)
- Payout history table with status badges
- Polls the API every 2 seconds for live status

Tailwind for styling. No router beyond a single dynamic route. No global state library — `useEffect` + `fetch` is enough.

The form generates a UUID v4 client-side as the idempotency key on submit. Resubmits within 24h with the same key are deduplicated by the server.

---

## 10. Test plan

The brief asks for **2 meaningful tests**. Quality > quantity.

**Test 1 — Concurrency:** seed merchant with 100 rupees. Spawn 2 threads, each POSTing a 60-rupee payout. Assert: exactly one returns 201, the other returns 422 with "insufficient balance." Assert the ledger sum equals the displayed balance.

Realism rules:
- **Real Postgres**, not SQLite (SQLite has no row-level `FOR UPDATE` semantics — the test would lie).
- **Real `transaction.atomic()`**, no patches or mocks on the lock path.
- Use `threading.Barrier(2)` so both requests hit the view at the same instant, not staggered.
- `pytest-django` with `--reuse-db` is fine, but the test class needs `TransactionTestCase` (not `TestCase`) so each thread sees committed data — `TestCase` wraps everything in a single rolled-back transaction and would mask the bug.

**Test 2 — Idempotency:** POST a payout with key K. POST again with the same key and same body. Assert both responses are byte-identical and only one payout exists in the DB. Then POST again with key K but a different amount; assert 422 with "key already used with different request."

Bonus if time: a state machine test that asserts `transition_to('completed')` from `pending` raises.

---

## 11. Deployment

- **Render** for both web service and worker (free tier supports both, and a managed Postgres + Redis are easy).
- One `render.yaml` with: web (Django + gunicorn), worker (celery), beat (celery-beat), postgres, redis.
- Frontend: build static, serve from Django via `whitenoise`. Saves a separate Vercel deploy.
- Seed runs as a one-off Render job after first deploy.

Fallback: Railway, also fine. Avoid Fly.io for this — more setup overhead.

---

## 12. Repo layout

```
playto-payout/
├── backend/
│   ├── manage.py
│   ├── playto/             # django project
│   ├── ledger/             # app: Merchant, LedgerEntry, balance queries
│   ├── payouts/            # app: Payout model, state machine, API views
│   │   ├── models.py
│   │   ├── views.py        # POST /payouts, GET /payouts
│   │   ├── tasks.py        # celery: process_payout, retry_stuck
│   │   ├── idempotency.py  # the middleware/decorator
│   │   └── state.py        # transition table + transition_to()
│   ├── tests/
│   │   ├── test_concurrency.py
│   │   └── test_idempotency.py
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   └── components/
│   └── package.json
├── seed.py
├── docker-compose.yml      # bonus
├── render.yaml
├── README.md
└── EXPLAINER.md
```

---

## 13. Execution plan (5 days, ~12 hours)

**Day 1 (3h) — Bones**
- Django project, Postgres, Celery, Redis wired up
- Models: Merchant, LedgerEntry, Payout, IdempotencyKey with all constraints
- Migrations applied
- `seed.py` populates 3 merchants with credit history
- Smoke test: `Merchant.balance()` returns expected paise

**Day 2 (3h) — The hard part**
- Payout POST endpoint with the lock, idempotency, ledger DEBIT
- `process_payout` Celery task with simulated settlement + state transitions + REVERSAL on failure
- `retry_stuck_payouts` beat task
- Both critical tests written and passing

**Day 3 (2h) — Frontend**
- Vite + React + Tailwind scaffold
- Dashboard page: balance, ledger, form, history table, polling
- Wired to API

**Day 4 (2h) — Deploy + EXPLAINER**
- Render deployment, seed remote DB
- README with setup
- EXPLAINER.md drafted answering all 5 questions with real code paste-ins

**Day 5 (2h) — Polish**
- Manual end-to-end test on the live URL
- AI audit section: keep a running notes file from day 1 of any AI suggestions I had to correct. Pick the sharpest one.
- Final commit hygiene pass; submit.

**Buffer:** ~2h. If something blocks, it's almost certainly the Celery+Render combo or row-lock testing on a fresh Postgres. Both are known unknowns.

---

## 14. Risks and what I'm watching for

- **Celery on Render free tier**: workers may sleep. Mitigation: document as a known free-tier limitation in README; the worker wakes on enqueue.
- **`SELECT FOR UPDATE` interaction with Django's transaction.atomic**: must use `select_for_update()` on the queryset *and* be inside `transaction.atomic()`. Easy to forget the wrapper. Will write the concurrency test first to catch this.
- **Idempotency in-flight insert race**: two requests arriving in the same millisecond both try to `INSERT` — the second fails on the unique constraint, which is exactly what we want, but the code must catch `IntegrityError` cleanly.
- **REVERSAL atomicity on failure**: if the worker dies between transitioning to `failed` and writing the REVERSAL, the merchant is short money. Mitigation: both in the same `transaction.atomic()`. The retry sweep won't pick it up because it's already `failed`. But: if the worker dies *during* `processing` before either, the sweep correctly retries.
- **AI audit — capturing it honestly**: I'll keep `ai-audit-notes.md` as a scratch file from the start. The temptation is to fabricate a clean story at the end; a real running log is more credible.

---

## 15. Bonuses — what I'll consider, what I'll skip

- **docker-compose.yml** — yes, ~30 min, makes local setup one command. Worth it.
- **Audit log** — partially free, since the ledger *is* the audit log for money. Will mention this framing in EXPLAINER.
- **Webhook delivery with retries** — skip. High effort, no merchant in this challenge to receive them.
- **Event sourcing** — skip as a separate thing. The ledger is already event-sourced for money movement; calling it "event sourcing" would be marketing.

---

## 16. Scope guardrails — what we will NOT build

These are tempting but explicitly out of scope. If I catch myself drafting any of these, stop:

- ❌ Authentication / login (the seed merchant id is passed in the URL; that's enough for the demo)
- ❌ Real bank API integration (settlement is simulated with `random.random()`)
- ❌ Wallet / multi-account abstraction (one ledger per merchant, period)
- ❌ Multi-currency (paise only)
- ❌ Email or SMS notifications
- ❌ Microservices / service split
- ❌ Kafka / event bus (Celery + Redis is sufficient)
- ❌ GraphQL (DRF JSON only)
- ❌ WebSockets for live updates (2s polling is fine and the brief doesn't ask)
- ❌ Re-introducing more ledger entry types beyond CREDIT / DEBIT / REVERSAL

---

## 17. Safety & correctness checklist

A pre-merge checklist I will run through before declaring done. Each item is a known way these systems fail.

**Money integrity**
- [ ] All money fields are `BigIntegerField` (paise). Grep the codebase for `FloatField` and `DecimalField` — should return zero hits in models.
- [ ] DB `CHECK (amount_paise > 0)` on `ledger_entry`. Verified via migration SQL.
- [ ] DB `CHECK` on `payout.amount_paise > 0` and on `payout.status IN (...)`.
- [ ] Balance computed in **one** SQL query with `CASE`, not three separate `Sum()` calls and Python addition.
- [ ] No code path reads balance into Python, then writes it back. The merchant has no balance column.

**Concurrency**
- [ ] `select_for_update()` is **inside** `transaction.atomic()` in the payout view. Both present, in that order.
- [ ] Balance aggregate runs **after** the `FOR UPDATE` returns, not before.
- [ ] Test 1 uses `TransactionTestCase` and `threading.Barrier`, against real Postgres.

**Idempotency**
- [ ] Composite unique constraint on `(merchant_id, key)`.
- [ ] Lookup query filters `created_at > now() - interval '24 hours'`.
- [ ] Same key + different body returns 422 (fingerprint mismatch).
- [ ] In-flight replay returns 409, not a duplicate payout.

**State machine**
- [ ] `transition_to()` is the only path that mutates `payout.status`. Grep confirms no direct `payout.status = ...` writes elsewhere.
- [ ] Failure transition + REVERSAL ledger entry happen in one `transaction.atomic()`. Worker crash between them is impossible.
- [ ] DB CHECK constraint enumerates the four valid statuses.

**Retry**
- [ ] Stuck-payout sweep uses `FOR UPDATE SKIP LOCKED` so two beat firings don't double-process.
- [ ] After 3 attempts, payout transitions to `failed` and writes REVERSAL — verified by manual test.

---

## 18. What success looks like

A reviewer clones the repo, runs `docker-compose up`, runs `python seed.py`, opens localhost, sees three merchants with balances, requests a payout, watches it move through statuses, opens two terminal tabs and fires curl with the same idempotency key — gets identical responses. Reads EXPLAINER.md and finds direct answers with real code, including one place where I caught the AI being wrong.

That's the bar.
