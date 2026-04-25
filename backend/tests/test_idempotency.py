"""GRADED TEST 2 — idempotency.

Three things this test proves:
  1. Replay with the SAME key and SAME body returns BYTE-IDENTICAL response,
     and only one payout exists in the database.
  2. Same key, DIFFERENT body returns 422 (fingerprint mismatch).
  3. Different keys produce different payouts (sanity check that scoping is
     not too aggressive).
"""

from __future__ import annotations

import os
import uuid

from django.test import Client, TransactionTestCase

os.environ.setdefault("PAYOUT_SETTLEMENT_FORCE", "0.0")
os.environ.setdefault("CELERY_EAGER", "1")

from ledger.models import EntryType, LedgerEntry, Merchant  # noqa: E402
from payouts.models import Payout  # noqa: E402


class IdempotencyTest(TransactionTestCase):
    def setUp(self) -> None:
        self.merchant = Merchant.objects.create(name="IdempotencyTestMerchant")
        LedgerEntry.objects.create(
            merchant=self.merchant,
            entry_type=EntryType.CREDIT,
            amount_paise=1_000_000,
        )
        self.bank = uuid.uuid4()
        self.client = Client()
        self.url = f"/api/v1/merchants/{self.merchant.id}/payouts"

    def _post(self, key, body):
        return self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=str(key),
        )

    def test_same_key_same_body_returns_byte_identical_response(self) -> None:
        key = uuid.uuid4()
        body = {"amount_paise": 5000, "bank_account_id": str(self.bank)}

        r1 = self._post(key, body)
        r2 = self._post(key, body)

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        # The bytes returned must be exactly the same — same id, same
        # timestamps, same key order. This is the "no duplicate work"
        # contract at the byte level.
        self.assertEqual(r1.content, r2.content)
        # And only one payout was actually created.
        self.assertEqual(
            Payout.objects.filter(merchant=self.merchant).count(),
            1,
            "second request must NOT have created a duplicate payout",
        )

    def test_same_key_different_body_is_rejected(self) -> None:
        key = uuid.uuid4()
        body1 = {"amount_paise": 5000, "bank_account_id": str(self.bank)}
        body2 = {"amount_paise": 9999, "bank_account_id": str(self.bank)}

        r1 = self._post(key, body1)
        r2 = self._post(key, body2)

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(
            r2.status_code,
            422,
            "reusing a key with a different body must be rejected",
        )
        self.assertIn(
            b"idempotency_key_reused_with_different_body", r2.content
        )

    def test_different_keys_create_different_payouts(self) -> None:
        body = {"amount_paise": 1000, "bank_account_id": str(self.bank)}
        r1 = self._post(uuid.uuid4(), body)
        r2 = self._post(uuid.uuid4(), body)

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(
            Payout.objects.filter(merchant=self.merchant).count(),
            2,
            "two different keys must produce two distinct payouts",
        )
