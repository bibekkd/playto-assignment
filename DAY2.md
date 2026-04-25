# Day 2 — What was built and how

Day 2 turned the foundation from Day 1 into a working money-moving service. Every concurrency, idempotency, state-machine, and retry concern from the brief is now exercised by code that runs and tests that pass.

What's new in one sentence: **a `POST /api/v1/.../payouts` endpoint that takes a row lock before computing balance, an idempotency layer that survives same-key replays byte-identically, a Celery worker that simulates bank settlement and writes REVERSAL atomically with the failure transition, a beat sweep for stuck payouts, and the two graded tests (concurrency + idempotency) passing against real Postgres.**

---

## 1. What "done" means for Day 2

From the planning doc, Day 2's exit criteria:

- [x] Payout POST endpoint with the lock, idempotency, ledger DEBIT
- [x] `process_payout` Celery task with simulated settlement + state transitions + REVERSAL on failure
- [x] `retry_stuck_payouts` beat task
- [x] Both critical tests written and passing

All four green. Every error path was also verified manually via curl (insufficient balance, missing header, fingerprint mismatch, replay byte-equality, failure → REVERSAL → balance restored).

---

## 2. Folder structure after Day 2

New and changed files marked with `★`. Files unchanged from Day 1 are listed but unmarked.

```
assignment-playtopay/
├── .venv/                                     # unchanged
├── planning.md                                # unchanged
├── DAY1.md                                    # unchanged
├── DAY2.md                                    # ★ this file
└── backend/
    ├── manage.py
    ├── requirements.txt
    ├── seed.py
    ├── smoke_test.py
    │
    ├── playto/
    │   ├── __init__.py
    │   ├── settings.py                        # ★ added DRF auth/permission disable
    │   ├── celery.py
    │   ├── urls.py                            # ★ now includes payouts.urls
    │   ├── wsgi.py / asgi.py
    │
    ├── ledger/
    │   ├── __init__.py
    │   ├── apps.py
    │   ├── models.py                          # unchanged from Day 1
    │   └── migrations/0001_initial.py, 0002_initial.py
    │
    ├── payouts/
    │   ├── __init__.py
    │   ├── apps.py
    │   ├── models.py                          # unchanged from Day 1
    │   ├── serializers.py                     # ★ NEW — DRF serializers
    │   ├── state.py                           # ★ NEW — state machine guard
    │   ├── tasks.py                           # ★ NEW — Celery worker + sweep
    │   ├── views.py                           # ★ NEW — API endpoints
    │   ├── urls.py                            # ★ NEW — route table
    │   └── migrations/0001_initial.py
    │
    ├── idempotency/
    │   ├── __init__.py
    │   ├── apps.py
    │   ├── models.py                          # ★ response_body: JSONField → TextField
    │   ├── service.py                         # ★ NEW — claim/complete/replay logic
    │   └── migrations/
    │       ├── 0001_initial.py
    │       ├── 0002_initial.py
    │       └── 0003_alter_idempotencykey_response_body.py    # ★ NEW
    │
    └── tests/
        ├── __init__.py
        ├── test_concurrency.py                # ★ NEW — graded test 1
        └── test_idempotency.py                # ★ NEW — graded test 2
```

Three apps now have meaningful logic, not just models. `payouts` is the heart.

---

## 3. File-by-file breakdown of new code

### `payouts/state.py` — the state machine guard

The single source of truth for "what status changes are legal." Two things live here:

**The legal-transition set** as a Python `set[tuple]`:

```python
LEGAL_TRANSITIONS = {
    (PENDING, PROCESSING),
    (PROCESSING, COMPLETED),
    (PROCESSING, FAILED),
    (PROCESSING, PROCESSING),   # retry
}
```

Anything not in the set raises `IllegalTransition`. This is where `failed → completed` is blocked — explicitly absent from the set, so the explicit check returns false and the call raises. The brief asks for exactly this in EXPLAINER question 4.

**The `transition(payout_id, new_status)` function**, which:
1. Asserts the caller is inside `transaction.atomic()` — fails fast if someone forgot the wrapper.
2. Re-fetches the payout under `select_for_update()` so the current status is read under a lock (no TOCTOU on the status field).
3. Checks the pair against the legal set. Raises if not legal.
4. Updates and saves only the status and updated_at columns.

The reason the function takes a `payout_id` rather than a `Payout` instance: the caller might have a stale `Payout` object from before the lock was held. Re-fetching inside this function guarantees we transition from the actually-current status, not whatever was true when the caller loaded it.

### `idempotency/service.py` — the idempotency layer

Pure logic module. No views, no models. Exposes two functions and four small dataclass result types:

**`fingerprint(body) -> str`** — sha256 of canonical JSON (sorted keys, no whitespace). Used to detect "same key, different body" abuse. We hash the body separately rather than store it raw because (a) it's smaller and (b) the comparison is cheap and constant-time-ish.

**`claim(merchant_id, key, body) -> Replay | InFlight | FingerprintMismatch | Claimed`** — the workhorse. Handles all four cases the brief cares about:
- New key → insert `IdempotencyKey(state=in_flight)`, return `Claimed(row)`. Caller proceeds.
- Existing completed row, same fingerprint → return `Replay(status, body_text)`. Caller returns the cached bytes verbatim.
- Existing in_flight row → return `InFlight()`. Caller returns 409.
- Existing row, different fingerprint → return `FingerprintMismatch()`. Caller returns 422.

The TTL is enforced inside this function: the existing-row lookup filters `created_at__gt=now() - 24h`. **An expired-but-not-yet-swept row is treated as if it does not exist** — that's the authoritative TTL rule from the planning doc. The hourly cleanup is just storage hygiene.

There's also a `try/except IntegrityError` around the `INSERT`. This catches the rare case where two requests with the same key arrive in the same millisecond and both clear the SELECT-doesn't-exist check; the second's INSERT fails on the unique constraint, we re-read, and dispatch as if it had been a normal replay/in-flight/mismatch.

**`complete(row, status, body_text, payout_id) -> None`** — flips the row to `state=completed` and caches the response. Asserts it's inside `transaction.atomic()` so the cache and the work it represents commit together.

A subtle but important choice: `body_text` is a string, not a dict. The caller renders the response once, hands us the bytes, and we replay those exact bytes. This is what makes byte-identical replay possible (see the Day 2 issues section below for the bug this fixed).

### `payouts/serializers.py` — DRF serializers

Two serializers:
- **`PayoutCreateSerializer`** — input validation: `amount_paise` must be a positive integer, `bank_account_id` must be a UUID. DRF rejects malformed bodies with a 400 before our code even runs.
- **`PayoutReadSerializer`** — `ModelSerializer` over `Payout`, surfacing exactly the fields we want in API responses. No id leaks (UUIDs only), no internal-only fields.

### `payouts/views.py` — the API endpoints

Four `@api_view` functions. The first is the load-bearing one in the entire service.

#### `create_payout(request, merchant_id)` — POST /api/v1/merchants/<id>/payouts

This view follows the exact 8-step ordering from planning.md §4. The ordering is non-negotiable; doing them in any other order re-introduces a race.

```
1. parse + validate Idempotency-Key header (400 if missing/malformed)
2. validate body via PayoutCreateSerializer
3. confirm merchant exists (404 if not)
4. claim idempotency key in a small transaction
   ├─ Replay        → return cached bytes verbatim
   ├─ InFlight      → return 409 with Retry-After: 1
   ├─ FingerprintMismatch → return 422
   └─ Claimed(row)  → continue
5. BEGIN money-moving transaction
6. SELECT merchant FOR UPDATE     ← serialization point
7. compute balance via Merchant.balance_paise()  ← AFTER the lock
8. if available < amount:
       cache 422 response on idempotency row
       return 422
   else:
       Payout.objects.create(...)
       LedgerEntry.objects.create(entry_type=DEBIT, ...)
       cache 201 response on idempotency row
9. COMMIT
10. process_payout.delay(payout.id)   ← AFTER commit so worker sees the row
11. return 201
```

Steps 6–10 all live inside one `with transaction.atomic():` block. Two important details:

**Why balance is computed only AFTER the lock is held.** If you compute the balance first, then `select_for_update`, you've solved nothing — two concurrent requests can both read the pre-lock balance, both then acquire the lock in sequence, and both pass the check. The check has to read state that exists *under the lock*. The view enforces this by ordering: the `select_for_update().get(...)` call literally precedes the `merchant.balance_paise()` call in the source.

**Why enqueue happens after commit.** If we called `process_payout.delay(...)` inside the transaction, Celery would put the task on the queue immediately, and the worker could pick it up and try to read the payout row before our transaction has committed. The worker would either see no such row (race) or, with high enough isolation, would block waiting for our commit. Enqueueing after the `with` block exits keeps that whole class of bug out of the picture.

There's a helper at the top of the file called `_render_json(data) -> str` that uses DRF's `JSONRenderer` to produce the bytes that go both into the response *and* into the idempotency cache. The same bytes are used for both, which is what makes the replay test pass byte-equality.

And `_replay(body_text, status)` returns a raw `HttpResponse` rather than a DRF `Response` — because `Response` would re-render the body, and re-rendering on a UUID/datetime payload could produce different bytes. Bypassing the renderer guarantees verbatim replay.

#### `merchant_detail(request, merchant_id)` — GET

Returns merchant id/name, `available_paise`, `held_paise`, and the ten most recent ledger entries. The dashboard reads from this on every poll.

#### `list_merchants(request)` — GET

Index endpoint. Returns all merchants with their available balances.

#### `list_payouts(request, merchant_id)` — GET

Returns the 50 most recent payouts for a merchant. Used by the dashboard's history table.

### `payouts/urls.py` — route table

Four routes, all under `/api/v1/`:

| method | path                                       | view             |
|--------|--------------------------------------------|------------------|
| GET    | /api/v1/merchants                          | list_merchants   |
| GET    | /api/v1/merchants/`<uuid>`                 | merchant_detail  |
| POST   | /api/v1/merchants/`<uuid>`/payouts         | create_payout    |
| GET    | /api/v1/merchants/`<uuid>`/payouts/list    | list_payouts     |

The collection endpoint is `/payouts/list` instead of `/payouts` because the same path is used for POST and we wanted to keep the route definitions explicit and easy to read. A future cleanup could collapse them into a single function dispatching on method.

### `payouts/tasks.py` — the Celery worker

Two `@shared_task` functions plus one private helper.

#### `process_payout(payout_id)` — one attempt at processing a payout

Three phases, in this order:

**Phase 1 (in a transaction):** lock the payout row, check if it's already terminal (skip if so) or out of attempts (fail+reverse if so), then transition pending→processing and increment attempts. The `last_attempt_at` timestamp is stamped here so the sweep query has fresh data.

**Phase 2 (no transaction):** the simulated bank settlement. We deliberately drop out of the transaction here because the simulated work involves `time.sleep(35)` for the hang case, and we don't want to hold a row lock for that long.

The roll uses `_roll()` which prefers the `PAYOUT_SETTLEMENT_FORCE` env var if set. This makes the settlement deterministic for tests and for manual demo (e.g. `PAYOUT_SETTLEMENT_FORCE=0.0` to force every payout to succeed; `0.8` to force every one to fail).

**Phase 3 (in a transaction):** based on the roll, call `state.transition(...)` to move to completed, or call `_fail_and_reverse(...)`. The hang case sleeps past the stuck threshold and returns; no transition happens, the beat sweep will pick it up.

#### `_fail_and_reverse(payout, reason)` — the atomic failure path

This is the function that satisfies the brief's "A failed payout returning funds must do so atomically with the state transition." Two operations:
1. `state.transition(payout.id, FAILED)` — flips the status under the payout's row lock.
2. `LedgerEntry.objects.create(entry_type=REVERSAL, ...)` — writes the counter-entry that returns the funds.

Both happen inside the caller's `transaction.atomic()`. If the worker dies between the two lines, neither commits. If the worker dies during phase 2 before this is called, the payout stays in `processing`, `last_attempt_at` is stale, and the beat sweep retries it. The merchant is never short money, regardless of where the worker died.

#### `retry_stuck_payouts()` — the beat sweep

Runs (in a real deployment) every 10 seconds via celery-beat. The query is hand-written SQL because Django's ORM doesn't expose `FOR UPDATE SKIP LOCKED` cleanly:

```sql
SELECT id, attempts FROM payout
WHERE status = 'processing'
  AND last_attempt_at < (now - 30s)
FOR UPDATE SKIP LOCKED
LIMIT 100
```

`SKIP LOCKED` is the key: if two beat firings overlap, the second skips rows the first has already locked rather than blocking. No double-processing.

For each row:
- If `attempts >= 3`, fail-and-reverse in place. No more retries.
- Otherwise, schedule `process_payout` with a 2-second countdown (a simple backoff).

The function returns the number of payouts requeued, which is handy for logging and for a future status endpoint.

### `idempotency/models.py` — schema change

One field changed: `response_body` went from `JSONField` (which Postgres backs with `jsonb`) to `TextField`. The reason is in the Day 2 issues section below — `jsonb` reorders keys, breaking byte-identical replay. Migration `0003_alter_idempotencykey_response_body.py` performs this change.

### `tests/test_concurrency.py` — graded test 1

Subclasses `TransactionTestCase`, not `TestCase`. The difference matters:
- `TestCase` wraps every test in a single transaction and rolls it back at the end. Threads in a `TestCase` share a transaction, so the second thread can never see the first thread's committed DEBIT row — the bug we're testing for would be invisible.
- `TransactionTestCase` truncates between tests instead of using a wrapping transaction, so each thread's commit is actually visible to the others.

The test seeds a merchant with 10000 paise (100 rupees), then spawns two threads each POSTing a 6000-paise (60-rupee) payout. A `threading.Barrier(2)` makes both threads wait until both are ready, then proceed simultaneously — without it the requests would arrive serially and the test wouldn't actually race.

Each thread uses Django's `Client` (not the live HTTP server), but the lock path is real `transaction.atomic()` + `select_for_update()` against real Postgres, so the concurrency primitive is exercised honestly.

Assertions:
- Exactly one 201 and one 422 (sorted statuses must equal `[201, 422]`).
- Exactly one Payout row in the database.
- `merchant.balance_paise() == 4000` (10000 starting − 6000 successful debit).
- The 422 body contains `insufficient_balance`.

The merchant state is also asserted via `Merchant.balance_paise()`, which uses the same `CASE` aggregate that the API uses. So the assertion proves the rubric's invariant — sum of ledger entries equals displayed balance — through the exact same code path the merchant sees.

### `tests/test_idempotency.py` — graded test 2

Three test methods on a `TransactionTestCase`:

- **`test_same_key_same_body_returns_byte_identical_response`** — POSTs twice with identical body and key. Asserts both return 201, `r1.content == r2.content` (byte equality, not just JSON equality), and that only one Payout row was created.
- **`test_same_key_different_body_is_rejected`** — POSTs with the same key but a different `amount_paise`. Asserts the second returns 422 with the `idempotency_key_reused_with_different_body` error.
- **`test_different_keys_create_different_payouts`** — sanity check that the deduplication isn't too aggressive: two different keys for the same merchant + same body produce two distinct payouts.

### `playto/settings.py` — DRF auth disabled explicitly

Added:

```python
"DEFAULT_AUTHENTICATION_CLASSES": [],
"DEFAULT_PERMISSION_CLASSES": [],
"UNAUTHENTICATED_USER": None,
```

Without this, DRF tries to authenticate every request and falls back to anonymous user resolution, which without `django.contrib.auth` middleware throws errors. Disabling it is the explicit "we have no auth in this challenge" statement.

### `playto/urls.py` — root URLconf

Added one line: `path("api/v1/", include("payouts.urls"))`. The whole API lives under `/api/v1/`.

---

## 4. The lifecycle of one payout, end to end

This is the easiest way to see how the new files fit together.

```
Client                Django view              Postgres                    Celery worker
  │                       │                        │                             │
  │ POST /payouts         │                        │                             │
  │ Idempotency-Key=K     │                        │                             │
  │──────────────────────▶│                        │                             │
  │                       │ idem.claim(merch, K)   │                             │
  │                       │───────────────────────▶│ INSERT idempotency_key      │
  │                       │                        │   (state=in_flight)         │
  │                       │ Claimed(row)           │                             │
  │                       │                        │                             │
  │                       │  BEGIN                 │                             │
  │                       │───────────────────────▶│                             │
  │                       │  SELECT merchant       │                             │
  │                       │  WHERE id=M FOR UPDATE │                             │
  │                       │───────────────────────▶│ row lock acquired           │
  │                       │  SUM(CASE …)           │                             │
  │                       │───────────────────────▶│ available = N paise         │
  │                       │  if N >= amount:       │                             │
  │                       │    INSERT payout       │                             │
  │                       │    INSERT ledger DEBIT │                             │
  │                       │  UPDATE idem state=    │                             │
  │                       │    completed, body=…   │                             │
  │                       │  COMMIT                │                             │
  │                       │───────────────────────▶│ row lock released           │
  │                       │                        │                             │
  │                       │ process_payout.delay(p_id)                           │
  │                       │──────────────────────────────────────────────────────▶ enqueued
  │ 201 + body            │                        │                             │
  │◀──────────────────────│                        │                             │
  │                       │                        │                             │
  │                       │                        │ ◀───────────────────────────│ pull task
  │                       │                        │                             │
  │                       │                        │ BEGIN                       │
  │                       │                        │ SELECT payout FOR UPDATE    │
  │                       │                        │ transition pending→processing
  │                       │                        │ UPDATE attempts, last_attempt_at
  │                       │                        │ COMMIT                      │
  │                       │                        │                             │
  │                       │                        │ roll = random()             │
  │                       │                        │                             │
  │                       │                        │ < 0.7 → BEGIN; transition   │
  │                       │                        │   processing→completed;     │
  │                       │                        │   COMMIT                    │
  │                       │                        │                             │
  │                       │                        │ < 0.9 → BEGIN; SELECT FOR   │
  │                       │                        │   UPDATE; transition→failed │
  │                       │                        │   INSERT ledger REVERSAL    │
  │                       │                        │   COMMIT  (atomic together) │
  │                       │                        │                             │
  │                       │                        │ ≥ 0.9 → sleep(35); return.  │
  │                       │                        │   beat sweep will retry.    │
```

One replay of the same key is just: client POSTs again → view calls `idem.claim(...)` → database returns the existing completed row → view returns the cached bytes verbatim. No transaction, no lock, no work.

---

## 5. Day 2 issues and how they were caught

I want to be honest about what didn't work the first time, because the brief grades on this. (Will be folded into EXPLAINER.md's AI Audit section on Day 4.)

### Issue 1: 500 on first POST — `Object of type UUID is not JSON serializable`

The first version of the view stored the response on the idempotency row as a plain dict. DRF serializers return `OrderedDict` instances containing `UUID` and `datetime` objects, and Django's `JSONField` chokes on non-native JSON types when writing. The fix was to render once via `JSONRenderer().render(...)` and store the resulting bytes (decoded to string) — that gets us JSON-native types and the bytes for replay in one step.

### Issue 2: Replay was semantically equal but not byte-identical

After fixing issue 1, the response stored as dict-in-jsonb still came back with reordered keys on the second request — Postgres `jsonb` hashes keys and does not preserve insertion order. The brief and the planning doc both promise byte-identical replay, so this was a real correctness gap.

The fix had two parts:
1. Schema: `response_body` field changed from `JSONField` (jsonb) to `TextField`. Migration `0003`.
2. Code: store the rendered JSON string, replay it via `HttpResponse(body, content_type="application/json")` — bypassing DRF's renderer on the replay path so the bytes go out exactly as they came in.

This kind of thing is exactly what the EXPLAINER's AI audit question is asking for.

---

## 6. What's NOT in Day 2 (intentionally)

- ❌ React frontend (Day 3)
- ❌ docker-compose, render.yaml (Day 4)
- ❌ Live deployment (Day 4)
- ❌ README.md, EXPLAINER.md (Day 4)
- ❌ celery-beat actually scheduled (only the task definition exists; scheduling lives in `render.yaml` for prod, optional for local dev where the sweep can be triggered manually for the demo)
- ❌ Cleanup task for expired idempotency rows (housekeeping, can be added on Day 4 with the rest of the deploy config)
- ❌ Authentication, audit log table, webhook delivery — explicit out-of-scope items from §16 of planning.md

---

## 7. How to verify Day 2 yourself

```bash
cd backend

# Run the graded tests against real Postgres
CELERY_EAGER=1 PAYOUT_SETTLEMENT_FORCE=0.0 \
  ../.venv/bin/python manage.py test tests --verbosity 2

# Expected:
#   test_two_concurrent_payouts_only_one_succeeds ... ok
#   test_different_keys_create_different_payouts ... ok
#   test_same_key_different_body_is_rejected ... ok
#   test_same_key_same_body_returns_byte_identical_response ... ok
#   Ran 4 tests in ~0.3s

# Spin up the dev server
CELERY_EAGER=1 PAYOUT_SETTLEMENT_FORCE=0.0 \
  ../.venv/bin/python manage.py runserver 127.0.0.1:8000

# In another terminal — list merchants
curl -s http://127.0.0.1:8000/api/v1/merchants

# Create a payout (replace MID with one of the merchant ids)
MID=<merchant-uuid>
KEY=$(uuidgen | tr A-Z a-z)
BANK=$(uuidgen | tr A-Z a-z)
curl -s -X POST "http://127.0.0.1:8000/api/v1/merchants/$MID/payouts" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d "{\"amount_paise\": 50000, \"bank_account_id\": \"$BANK\"}"

# Replay — same key, same body. Bytes are identical to the first response.
curl -s -X POST "http://127.0.0.1:8000/api/v1/merchants/$MID/payouts" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d "{\"amount_paise\": 50000, \"bank_account_id\": \"$BANK\"}"

# Failure path — set the env to force every roll into the failure band
# and watch the balance return to its original value via REVERSAL.
PAYOUT_SETTLEMENT_FORCE=0.8  ./manage.py runserver  # restart with this
```

---

## 8. Day 2 in one sentence

The lock, the idempotency layer, the state machine, the worker, the failure-path REVERSAL, and the retry sweep are all in place; two graded tests prove the behaviors that the brief grades on; and every error path was manually exercised end-to-end.

Day 3 builds the React dashboard on top of these endpoints.
