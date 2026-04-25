"""GRADED TEST 1 — concurrency.

Two simultaneous payout requests against a merchant whose balance can
satisfy ONE of them but not BOTH. Exactly one must succeed (201) and the
other must be cleanly rejected (422 insufficient_balance). The ledger
invariant `sum(entries) == displayed_balance` must hold throughout.

Realism rules (planning.md §10):
  - Real Postgres (we cannot use SQLite — no row-level FOR UPDATE).
  - Real transaction.atomic() in the view path; nothing mocked.
  - threading.Barrier(2) so both requests hit the lock at the same instant.
  - TransactionTestCase, NOT TestCase. TestCase wraps everything in one
    transaction and rolls it back, which would mean the second thread
    cannot see the first thread's committed DEBIT row, masking the bug.
"""

from __future__ import annotations

import os
import threading
import uuid

from django.db import connections
from django.test import Client, TransactionTestCase

# Force every settlement roll to land on `success` so that if the worker
# is invoked synchronously by Celery EAGER mode, it doesn't introduce
# extra ledger entries that would muddy the invariant assertion.
os.environ.setdefault("PAYOUT_SETTLEMENT_FORCE", "0.0")
os.environ.setdefault("CELERY_EAGER", "1")

from ledger.models import EntryType, LedgerEntry, Merchant  # noqa: E402
from payouts.models import Payout  # noqa: E402


class ConcurrentPayoutTest(TransactionTestCase):
    # Reset DB after the test so seeded merchants outside the test aren't
    # polluted. TransactionTestCase already truncates by default.
    reset_sequences = False

    def setUp(self) -> None:
        # 100 rupees == 10000 paise. Two 60-rupee (6000-paise) requests
        # cannot both win.
        self.merchant = Merchant.objects.create(name="ConcurrencyTestMerchant")
        LedgerEntry.objects.create(
            merchant=self.merchant, entry_type=EntryType.CREDIT, amount_paise=10000
        )
        self.bank = uuid.uuid4()

    def _post_payout(self, key: uuid.UUID, barrier: threading.Barrier, results: list):
        client = Client()
        # Wait until both threads are ready, then both fire at the same
        # instant. This makes the race actually race.
        barrier.wait(timeout=5)
        try:
            resp = client.post(
                f"/api/v1/merchants/{self.merchant.id}/payouts",
                data={
                    "amount_paise": 6000,
                    "bank_account_id": str(self.bank),
                },
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY=str(key),
            )
            results.append((resp.status_code, resp.content.decode("utf-8")))
        finally:
            # Each thread opens its own DB connection; close it so the
            # test runner's teardown doesn't see leaks.
            connections.close_all()

    def test_two_concurrent_payouts_only_one_succeeds(self) -> None:
        barrier = threading.Barrier(2)
        results: list = []

        t1 = threading.Thread(
            target=self._post_payout, args=(uuid.uuid4(), barrier, results)
        )
        t2 = threading.Thread(
            target=self._post_payout, args=(uuid.uuid4(), barrier, results)
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(len(results), 2, "both threads must complete")
        statuses = sorted(r[0] for r in results)
        self.assertEqual(
            statuses, [201, 422],
            f"exactly one 201 and one 422 expected; got {statuses}: {results}",
        )

        # Exactly ONE Payout row exists.
        self.assertEqual(
            Payout.objects.filter(merchant=self.merchant).count(),
            1,
            "exactly one payout should have been created",
        )

        # Ledger invariant: sum(entries) == displayed balance, and the
        # balance is exactly the original 10000 minus the one successful
        # 6000 DEBIT (= 4000). The worker may have run in eager mode and
        # marked the payout completed, but completion does NOT add a
        # ledger entry — the DEBIT at request time is the only money move.
        merchant = Merchant.objects.get(pk=self.merchant.id)
        self.assertEqual(merchant.balance_paise(), 4000)

        # And the 422 response carries the right error code.
        rejected = next(r for r in results if r[0] == 422)
        self.assertIn("insufficient_balance", rejected[1])
