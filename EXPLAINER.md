# EXPLAINER

Answers to the 5 graded questions in the brief. Each answer pastes the
exact code that does the work, with file paths and line numbers, plus a
short explanation of WHY it works the way it does.

---

## 1. The Ledger

### The balance calculation query

[backend/ledger/models.py:25-42](backend/ledger/models.py#L25-L42):

```python
def balance_paise(self) -> int:
    # Single-query CASE aggregate. No Python arithmetic on rows.
    # Held funds are NOT separated here; DEBIT entries are written at
    # request time, so this number is the merchant's available balance.
    result = LedgerEntry.objects.filter(merchant_id=self.id).aggregate(
        total=Coalesce(
            Sum(
                Case(
                    When(entry_type=EntryType.CREDIT,   then= models.F("amount_paise")),
                    When(entry_type=EntryType.DEBIT,    then=-models.F("amount_paise")),
                    When(entry_type=EntryType.REVERSAL, then= models.F("amount_paise")),
                    output_field=models.BigIntegerField(),
                )
            ),
            Value(0, output_field=models.BigIntegerField()),
        )
    )
    return int(result["total"])
```

Equivalent raw SQL the reviewer can paste into psql:

```sql
SELECT COALESCE(SUM(
  CASE entry_type
    WHEN 'CREDIT'   THEN  amount_paise
    WHEN 'DEBIT'    THEN -amount_paise
    WHEN 'REVERSAL' THEN  amount_paise
  END
), 0)::bigint AS balance_paise
FROM ledger_entry
WHERE merchant_id = %s;
```

### Why credits and debits are modeled this way

**Three ledger entry types, locked**: `CREDIT` (+), `DEBIT` (−), `REVERSAL` (+). No separate "hold" / "settle" / "release" types. The `DEBIT` is written at payout-request time, so funds leave the available balance the moment the request is accepted. On failure, a `REVERSAL` is written as a positive counter-entry. On success, nothing else happens — the original `DEBIT` is the final money movement.

The choices that matter:

1. **Append-only.** Once written, ledger rows are never updated or deleted. The audit trail IS the data model.
2. **No mutable balance column on `Merchant`.** Balance is derived from the ledger via the query above. The invariant *"sum of credits minus debits equals displayed balance"* is **structurally guaranteed** — there's no number to drift, because there's no number stored.
3. **`amount_paise` is always positive.** The sign comes from `entry_type`, never from the amount. The DB enforces this with `CHECK (amount_paise > 0)` ([backend/ledger/models.py:79-82](backend/ledger/models.py#L79-L82)). A negative-amount bug is impossible at the database, not just in Python.
4. **Single-query `CASE` aggregate.** Not three separate `Sum()` calls and Python addition. One round-trip, one consistent snapshot, no chance of a partial read between queries.
5. **All money fields are `BigIntegerField` storing paise.** No floats. No Decimals. The smoke test ([backend/smoke_test.py](backend/smoke_test.py)) asserts the ORM result matches a raw-SQL `CASE` aggregate, for every seeded merchant.

---

## 2. The Lock

### The exact code that prevents two concurrent payouts from overdrawing

[backend/payouts/views.py:112-149](backend/payouts/views.py#L112-L149):

```python
amount = body["amount_paise"]

# Steps 3-7: the money-moving transaction.
with transaction.atomic():
    # Step 4: lock the merchant row. Concurrent requests for the same
    # merchant block here. This is the serialization point.
    Merchant.objects.select_for_update().get(pk=merchant_id)

    # Step 5: compute balance AFTER the lock is held.
    merchant = Merchant.objects.get(pk=merchant_id)
    available = merchant.balance_paise()

    if available < amount:
        response_dict = {
            "error": "insufficient_balance",
            "available_paise": available,
            "requested_paise": amount,
        }
        body_text = _render_json(response_dict)
        row = IdempotencyKey.objects.get(pk=idem_row.pk)
        idem.complete(row, status=422, body_text=body_text)
        return _replay(body_text, 422)

    # Step 6: create the payout and the DEBIT in the same transaction
    # as the balance check. This is what makes "exactly one wins" true.
    payout = Payout.objects.create(
        merchant=merchant,
        amount_paise=amount,
        bank_account_id=body["bank_account_id"],
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        entry_type=EntryType.DEBIT,
        amount_paise=amount,
        payout=payout,
    )
    # ... cache the 201 on the idempotency row ...
```

### What database primitive it relies on

**PostgreSQL's `SELECT ... FOR UPDATE` row-level exclusive lock**, taken inside a `BEGIN`/`COMMIT` (Django's `transaction.atomic()`).

The Django ORM call:
```python
Merchant.objects.select_for_update().get(pk=merchant_id)
```
issues:
```sql
SELECT … FROM merchant WHERE id = %s FOR UPDATE
```

Postgres holds an exclusive row lock on that one merchant row for the rest of the transaction. Any other transaction that calls `SELECT ... FOR UPDATE` on the same row blocks until the first one commits or rolls back.

### Why this kills the race

Two requests A and B, each trying to debit ₹60 from a ₹100 balance:

```
T0:  A: BEGIN
T1:  A: SELECT merchant FOR UPDATE   ← acquires lock
T2:  B: BEGIN
T3:  B: SELECT merchant FOR UPDATE   ← BLOCKS, waits for A
T4:  A: SUM(...)  → 100  (≥ 60, ok)
T5:  A: INSERT payout, INSERT ledger DEBIT
T6:  A: COMMIT                        ← releases lock
T7:  B: SELECT returns                ← lock now held by B
T8:  B: SUM(...)  → 40   (sees A's committed DEBIT, < 60, REJECTED)
T9:  B: cache 422 on idempotency row
T10: B: COMMIT
```

Exactly one succeeds. The second sees A's committed DEBIT in its balance aggregate and is rejected with 422 cleanly.

### Why the order matters

Notice the lock acquisition (`select_for_update()`) **precedes** the balance computation in the source. **This ordering is non-negotiable.** Reading balance before taking the lock would be a TOCTOU bug — both requests could read 100, both then acquire the lock in sequence, and both pass the check. The check has to read state *that exists under the lock*. Code review for this codebase rejects any PR that puts a balance read above a `select_for_update()`.

### Why lock the merchant row, not the ledger entries

The merchant row is the natural serialization point for "all money decisions for merchant X." Locking ledger entries doesn't help — new ones are being inserted, not the existing ones that would be locked. There's no row-level lock you can take on an "as-yet-unwritten row." The merchant row is the immutable serialization handle.

### Tested

[backend/tests/test_concurrency.py](backend/tests/test_concurrency.py) seeds a merchant with 10000 paise, fires two 6000-paise requests via `threading.Barrier(2)` against real Postgres in a `TransactionTestCase`. Asserts: exactly one 201 + one 422, exactly one Payout row, balance ends at 4000.

---

## 3. The Idempotency

### How the system knows it has seen a key before

A composite-unique constraint on `(merchant_id, key)` and a tiny state-machine row in the `idempotency_key` table.

The lookup-then-insert dance lives in [backend/idempotency/service.py:67-115](backend/idempotency/service.py#L67-L115):

```python
def claim(merchant_id, key, body: dict[str, Any]):
    fp = fingerprint(body)
    cutoff = timezone.now() - TTL

    # Read-side TTL filter is authoritative. An expired-but-not-yet-swept
    # row is treated as if it doesn't exist — we'll INSERT over it after
    # cleaning it up.
    existing = (
        IdempotencyKey.objects.filter(
            merchant_id=merchant_id, key=key, created_at__gt=cutoff
        ).first()
    )
    if existing is not None:
        if existing.request_fingerprint != fp:
            return FingerprintMismatch()           # 422
        if existing.state == KeyState.IN_FLIGHT:
            return InFlight()                       # 409
        return Replay(status=existing.response_status,
                      body_text=existing.response_body)   # cached 201

    # No live row. Drop a stale row if any, then INSERT.
    IdempotencyKey.objects.filter(
        merchant_id=merchant_id, key=key, created_at__lte=cutoff
    ).delete()

    try:
        row = IdempotencyKey.objects.create(
            merchant_id=merchant_id, key=key,
            request_fingerprint=fp, state=KeyState.IN_FLIGHT,
        )
    except IntegrityError:
        # Lost a race with a sibling request that inserted the same key
        # between our SELECT and INSERT. Re-read and dispatch.
        row = IdempotencyKey.objects.get(merchant_id=merchant_id, key=key)
        if row.request_fingerprint != fp:
            return FingerprintMismatch()
        if row.state == KeyState.IN_FLIGHT:
            return InFlight()
        return Replay(status=row.response_status, body_text=row.response_body)

    return Claimed(row=row)
```

### What the constraint looks like at the DB level

[backend/idempotency/models.py:31-34](backend/idempotency/models.py#L31-L34):

```python
constraints = [
    models.UniqueConstraint(
        fields=["merchant", "key"], name="idempotency_merchant_key_unique"
    ),
    ...
]
```

In Postgres:

```sql
ALTER TABLE idempotency_key
  ADD CONSTRAINT idempotency_merchant_key_unique UNIQUE (merchant_id, key);
```

This is what enforces "same key returns same response" — the database physically refuses a second `INSERT` of `(merchant, key)`. Application code can't bypass it.

### TTL — 24 hours, two-layer

1. **Read-side filter (authoritative):** every lookup filters `WHERE created_at > now() - interval '24 hours'`. An expired-but-not-yet-swept row is treated as if it doesn't exist.
2. **Periodic cleanup:** housekeeping only. The read-side filter is the rule of record.

### What happens if the first request is in flight when the second arrives

The first request writes `state=in_flight` early in its transaction (the `IdempotencyKey.objects.create(...)` line above). The second request:

1. Looks up `(merchant, key)` — finds the row with `state=in_flight`.
2. Returns `InFlight()`, which the view converts to **`409 Conflict` with `Retry-After: 1`**.

The view code at [backend/payouts/views.py:96-101](backend/payouts/views.py#L96-L101):
```python
if isinstance(outcome, idem.InFlight):
    return Response(
        {"error": "request_in_flight"},
        status=status.HTTP_409_CONFLICT,
        headers={"Retry-After": "1"},
    )
```

Why 409 and not "block waiting for the first to finish": blocking would tie up a worker thread, and a stuck first request would cascade into stuck second/third requests. Returning 409 lets the client decide its own retry policy. Stripe's API does the same.

### Same key, different body

If the lookup returns an existing row whose `request_fingerprint` doesn't match the current body's fingerprint, we return `FingerprintMismatch()` → **`422 Unprocessable Entity`** with `error: idempotency_key_reused_with_different_body`. This catches client bugs where a key is reused for an unrelated request.

The fingerprint is sha256 of canonical-JSON (sorted keys, no whitespace). [backend/idempotency/service.py:33-37](backend/idempotency/service.py#L33-L37):

```python
def fingerprint(body: dict[str, Any]) -> str:
    """Stable sha256 of the request body for "same key, different body"."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

### Tested

[backend/tests/test_idempotency.py](backend/tests/test_idempotency.py) covers all three paths:

- `test_same_key_same_body_returns_byte_identical_response` — `r1.content == r2.content` (byte equality)
- `test_same_key_different_body_is_rejected` — second POST → 422 with `idempotency_key_reused_with_different_body`
- `test_different_keys_create_different_payouts` — sanity that scoping isn't too aggressive

---

## 4. The State Machine

### Where failed-to-completed is blocked

[backend/payouts/state.py:15-22](backend/payouts/state.py#L15-L22):

```python
LEGAL_TRANSITIONS: set[tuple[str, str]] = {
    (PayoutStatus.PENDING,    PayoutStatus.PROCESSING),
    (PayoutStatus.PROCESSING, PayoutStatus.COMPLETED),
    (PayoutStatus.PROCESSING, PayoutStatus.FAILED),
    # Retry path: the worker re-enters processing on a fresh attempt.
    (PayoutStatus.PROCESSING, PayoutStatus.PROCESSING),
}
```

`(FAILED, COMPLETED)` is **deliberately absent** from this set. The check is in `transition()` at [backend/payouts/state.py:36-55](backend/payouts/state.py#L36-L55):

```python
def transition(payout_id, new_status: str) -> Payout:
    """Atomically move a payout to `new_status`."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "transition() must be called inside transaction.atomic()"
        )

    payout = Payout.objects.select_for_update().get(pk=payout_id)
    if (payout.status, new_status) not in LEGAL_TRANSITIONS:
        raise IllegalTransition(payout.status, new_status)

    payout.status = new_status
    payout.save(update_fields=["status", "updated_at"])
    return payout
```

A call like `transition(payout_id, "completed")` on a payout whose current `status` is `"failed"` evaluates `("failed", "completed") not in LEGAL_TRANSITIONS` → True → raises `IllegalTransition`. The save never happens. Nothing in the database changes.

### Three things that make this guard solid

**(a) `select_for_update()` re-reads the current status under a lock.** A stale `payout.status` from before some other worker transitioned the row would lead to a wrong decision. Re-reading inside the lock guarantees the `(current, new)` pair is evaluated against the actually-current status.

**(b) `in_atomic_block` assertion.** If the caller forgot the `transaction.atomic()` wrapper, the function refuses to run. This matters most for the failure path:

[backend/payouts/tasks.py:84-99](backend/payouts/tasks.py#L84-L99):

```python
def _fail_and_reverse(payout: Payout, reason: str) -> None:
    """processing → failed + write REVERSAL ledger entry, atomically.

    Caller must already hold a SELECT FOR UPDATE on the payout row and be
    inside transaction.atomic(). Both side effects commit together or
    neither does — this is what makes the "merchant is never short money
    on a worker crash" guarantee true.
    """
    log.warning("failing payout %s: %s", payout.id, reason)
    state.transition(payout.id, PayoutStatus.FAILED)
    LedgerEntry.objects.create(
        merchant_id=payout.merchant_id,
        entry_type=EntryType.REVERSAL,
        amount_paise=payout.amount_paise,
        payout=payout,
    )
```

The state transition and the REVERSAL ledger entry are inside the same `transaction.atomic()` (the caller's). If the worker dies between line `state.transition(...)` and the `LedgerEntry.objects.create(...)`, the open transaction is rolled back by Postgres on connection loss. Neither side effect is persisted. The merchant is never short money.

**(c) Single source of truth.** `payouts.state.transition()` is the only function that writes `payout.status`. A `grep -rn 'payout.status =' backend/` returns this file and nothing else. The view, the worker, the retry sweep — all go through this function.

### Belt and braces: also a DB CHECK constraint

[backend/payouts/models.py:30-34](backend/payouts/models.py#L30-L34):

```python
models.CheckConstraint(
    check=models.Q(
        status__in=["pending", "processing", "completed", "failed"]
    ),
    name="payout_status_valid",
),
```

The Python check catches illegal *transitions*. The DB CHECK catches illegal *values* (typos, schema drift, raw-SQL accidents). Both layers are needed; neither alone is sufficient.

---

## 5. The AI Audit

A real running log of two specific cases where AI gave me code that was almost-right but quietly wrong, what I caught, and what I replaced it with. These are not invented — both bugs were caught while building Day 2 ([DAY2.md §5](DAY2.md#5-day-2-issues-and-how-they-were-caught)).

### Case A — the byte-identical replay bug (the subtle one)

**What AI suggested.** When I asked for the idempotency caching code, the suggestion was the obvious thing: store the response on the row as a JSON dict, and on replay, return that dict via DRF's `Response`.

```python
# AI version (subtly wrong):
class IdempotencyKey(models.Model):
    response_body = models.JSONField(null=True, blank=True)   # jsonb under the hood

# In the view, on replay:
return Response(row.response_body, status=row.response_status)
```

**Why it's wrong.** Two compounding issues:

1. `models.JSONField` on Postgres uses the `jsonb` column type. `jsonb` **stores normalized**: it hashes keys and does not preserve insertion order. So `r1.content == r2.content` (byte equality) fails on the second request. The keys come back in a different order. The brief and the planning doc both promise byte-identical replay; this breaks that contract.

2. DRF's `Response(...)` runs the renderer again on replay. Even if the dict round-trips through `jsonb` cleanly (it doesn't), the second `Response` would re-serialize from Python objects, which includes UUIDs and datetimes — there's no guarantee the rendered bytes match what was sent the first time.

**What I caught it with.** The very first manual replay test:

```bash
KEY=$(uuidgen); BANK=$(uuidgen)
R1=$(curl -s -X POST .../payouts -H "Idempotency-Key: $KEY" -d "...")
R2=$(curl -s -X POST .../payouts -H "Idempotency-Key: $KEY" -d "...")
[ "$R1" = "$R2" ] && echo BYTE-IDENTICAL || echo DIFFER
```

Got `DIFFER`. The two responses had the same payout id but the keys were in different orders.

**What I replaced it with.** Two changes:

1. `response_body` field type: `JSONField` → `TextField`. Storage is raw bytes; nothing reorders. [backend/idempotency/models.py:13-17](backend/idempotency/models.py#L13-L17):

   ```python
   # Stored as raw JSON text (not jsonb) so replays return byte-identical
   # responses. jsonb hashes keys and loses insertion order, which would
   # break the "same response if called twice" guarantee at the byte level.
   response_body = models.TextField(null=True, blank=True)
   ```

2. The view renders the response **once** through DRF's `JSONRenderer`, stores the resulting bytes, and replays via raw `HttpResponse` (not `Response`) so DRF can't re-render. [backend/payouts/views.py:25-44](backend/payouts/views.py#L25-L44):

   ```python
   def _render_json(data) -> str:
       """Render a serializer dict to a stable JSON string.

       The bytes we return on the first response are the same bytes we will
       replay on every subsequent same-key request — that's what makes the
       idempotency contract byte-identical, not just semantically equal.
       """
       return JSONRenderer().render(data).decode("utf-8")


   def _replay(body_text: str, status_code: int) -> HttpResponse:
       return HttpResponse(
           body_text, status=status_code, content_type="application/json"
       )
   ```

After the fix, the `R1 == R2` test passes. Migration `0003_alter_idempotencykey_response_body.py` was added to roll the schema change forward.

The graded test [backend/tests/test_idempotency.py](backend/tests/test_idempotency.py) asserts `r1.content == r2.content` (byte equality, not JSON equality) precisely because of this incident.

### Case B — the JSON serialization 500 (the obvious one, hit first)

Same area of code, different bug, caught earlier.

**What AI suggested.** Inside the view, after creating a payout:

```python
# AI version:
response_body = PayoutReadSerializer(payout).data    # OrderedDict with UUID/datetime
row.response_body = response_body                    # write to JSONField
row.save()
```

**Why it's wrong.** `PayoutReadSerializer(...).data` is an `OrderedDict` containing `UUID` and `datetime.datetime` objects. Writing that to a `JSONField` blows up with `TypeError: Object of type UUID is not JSON serializable` because Django's JSON encoder doesn't know about UUIDs.

**What I caught it with.** The first end-to-end POST returned a 500 with a Django debug page reading exactly that. Easy to spot.

**What I replaced it with.** Render through DRF's `JSONRenderer` first — that DOES know about UUIDs and datetimes — and store the resulting string. This is the same `_render_json()` helper from Case A. So Case B's fix turned out to be the prerequisite for Case A's fix; the right shape was render-once-store-bytes-replay-bytes, and getting there required both insights.

### What this audit demonstrates

The brief asks "are you senior enough not to trust the machine blindly?" The answer is: I trust AI to draft, and I distrust it on the boundary cases that matter for correctness — serialization, locking, ordering, atomicity. Both of these bugs would have shipped if I'd trusted the AI suggestion verbatim. Both were caught because I wrote a manual replay test before writing automated tests, and the manual test asserted byte equality — which is what the brief's idempotency contract actually requires, not what AI defaults assume ("semantically equal JSON" is good enough; it isn't).

The graded test asserts the strong property (`r1.content == r2.content`) precisely so that any future AI-suggested refactor that re-introduces a `jsonb` column or a re-rendered `Response` will fail loudly.
