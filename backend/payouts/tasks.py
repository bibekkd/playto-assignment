"""Celery tasks: payout processor and stuck-payout retry sweep."""

from __future__ import annotations

import logging
import os
import random
import time

from celery import shared_task
from django.db import connection, transaction
from django.utils import timezone

from ledger.models import EntryType, LedgerEntry

from . import state
from .models import Payout, PayoutStatus

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
STUCK_AFTER_SECONDS = 30

# Simulated bank settlement probabilities (planning.md §8).
SUCCESS_THRESHOLD = 0.7   # < 0.7 → success
FAILURE_THRESHOLD = 0.9   # 0.7 ≤ x < 0.9 → failure; ≥ 0.9 → hang


def _roll() -> float:
    """Seedable random for tests. PAYOUT_SETTLEMENT_FORCE overrides."""
    forced = os.environ.get("PAYOUT_SETTLEMENT_FORCE")
    if forced is not None:
        return float(forced)
    return random.random()


@shared_task(bind=True, max_retries=0)
def process_payout(self, payout_id: str) -> str:
    """Move a payout pending → processing → (completed | failed | hang).

    Each invocation is one attempt. Retries are scheduled by the
    `retry_stuck_payouts` beat sweep, not by Celery's own retry, because
    the brief defines stuck-detection in terms of wall-clock time spent
    in `processing`, not in terms of task exceptions.
    """
    # Step 1: pending → processing (or processing → processing for retry).
    # Increment attempts and stamp last_attempt_at in the same txn as the
    # transition so the sweep query sees consistent values.
    with transaction.atomic():
        payout = Payout.objects.select_for_update().get(pk=payout_id)
        if payout.status == PayoutStatus.COMPLETED or payout.status == PayoutStatus.FAILED:
            log.info("payout %s already terminal (%s); skip", payout_id, payout.status)
            return payout.status
        if payout.attempts >= MAX_ATTEMPTS:
            # Out of attempts and still not terminal — fail it and refund.
            _fail_and_reverse(payout, reason="max_attempts_exceeded")
            return PayoutStatus.FAILED

        state.transition(payout.id, PayoutStatus.PROCESSING)
        Payout.objects.filter(pk=payout.id).update(
            attempts=payout.attempts + 1,
            last_attempt_at=timezone.now(),
        )

    # Step 2: simulate bank settlement. Outside the txn — we don't want
    # to hold a row lock during the simulated wall-clock delay.
    roll = _roll()

    if roll < SUCCESS_THRESHOLD:
        with transaction.atomic():
            state.transition(payout_id, PayoutStatus.COMPLETED)
        log.info("payout %s completed (roll=%.3f)", payout_id, roll)
        return PayoutStatus.COMPLETED

    if roll < FAILURE_THRESHOLD:
        with transaction.atomic():
            payout = Payout.objects.select_for_update().get(pk=payout_id)
            _fail_and_reverse(payout, reason=f"simulated_bank_failure roll={roll:.3f}")
        return PayoutStatus.FAILED

    # Hang: sleep past the stuck threshold without transitioning.
    # The beat sweep will pick this up and re-enqueue.
    log.info("payout %s hanging (roll=%.3f)", payout_id, roll)
    time.sleep(STUCK_AFTER_SECONDS + 5)
    return PayoutStatus.PROCESSING


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


@shared_task
def process_pending_payouts(limit: int = 100) -> int:
    """Drain `pending` payouts from the database.

    On Render's free tier we can't run a long-lived Celery worker, so a
    scheduled Cron Job invokes this every minute. SKIP LOCKED means two
    overlapping cron firings (e.g. a slow run still going when the next
    one starts) don't fight over the same rows.

    Returns the count of payouts kicked off.
    """
    picked: list[str] = []
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM payout
            WHERE status = 'pending'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            [limit],
        )
        picked = [str(row[0]) for row in cur.fetchall()]

    # Process each one in its own transaction. Run inline (not via .delay)
    # because in cron-mode we ARE the worker — there's nothing else to run
    # the queued task.
    for pid in picked:
        try:
            process_payout.run(pid)
        except Exception as e:
            log.exception("process_payout(%s) failed: %s", pid, e)
    log.info("process_pending_payouts: drained=%d", len(picked))
    return len(picked)


@shared_task
def retry_stuck_payouts() -> int:
    """Re-enqueue payouts that have been stuck in `processing` too long.

    Uses FOR UPDATE SKIP LOCKED so two beat firings cannot grab the same
    row. Bounded by MAX_ATTEMPTS — once exceeded, the payout is failed
    and reversed in-place.
    """
    cutoff = timezone.now() - timezone.timedelta(seconds=STUCK_AFTER_SECONDS)
    requeued: list[str] = []

    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, attempts FROM payout
            WHERE status = 'processing'
              AND last_attempt_at < %s
            FOR UPDATE SKIP LOCKED
            LIMIT 100
            """,
            [cutoff],
        )
        rows = cur.fetchall()

        for payout_id, attempts in rows:
            if attempts >= MAX_ATTEMPTS:
                payout = Payout.objects.select_for_update().get(pk=payout_id)
                _fail_and_reverse(
                    payout, reason=f"stuck and out of retries (attempts={attempts})"
                )
                continue
            requeued.append(str(payout_id))

    # Enqueue with exponential backoff (countdown in seconds): 2, 8.
    for pid in requeued:
        # We don't know exact attempt count post-commit without a re-read,
        # but the worker stamps last_attempt_at fresh on every run, so the
        # backoff here is best-effort. Use a constant short delay.
        process_payout.apply_async(args=[pid], countdown=2)

    log.info("retry_stuck_payouts: requeued=%d failed=%d", len(requeued), len(rows) - len(requeued))
    return len(requeued)
