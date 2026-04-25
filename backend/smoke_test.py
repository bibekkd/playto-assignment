"""Day 1 smoke test.

Asserts that for every seeded merchant:
  1. The ORM `balance_paise()` matches a raw-SQL CASE aggregate, and
  2. CHECK constraints reject negative ledger amounts.

Run: python smoke_test.py
"""

import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "playto.settings")
django.setup()

from django.db import IntegrityError, connection, transaction  # noqa: E402

from ledger.models import EntryType, LedgerEntry, Merchant  # noqa: E402


RAW_BALANCE_SQL = """
SELECT COALESCE(SUM(
  CASE entry_type
    WHEN 'CREDIT'   THEN  amount_paise
    WHEN 'DEBIT'    THEN -amount_paise
    WHEN 'REVERSAL' THEN  amount_paise
  END
), 0)::bigint AS balance_paise
FROM ledger_entry
WHERE merchant_id = %s;
"""


def check_balances() -> None:
    for m in Merchant.objects.all().order_by("name"):
        orm_value = m.balance_paise()
        with connection.cursor() as cur:
            cur.execute(RAW_BALANCE_SQL, [str(m.id)])
            (raw_value,) = cur.fetchone()
        assert orm_value == raw_value, (
            f"BALANCE MISMATCH for {m.name}: ORM={orm_value} RAW={raw_value}"
        )
        print(f"[ok] {m.name:25s} ORM == RAW == {orm_value} paise")


def check_negative_amount_rejected() -> None:
    m = Merchant.objects.first()
    try:
        with transaction.atomic():
            LedgerEntry.objects.create(
                merchant=m, entry_type=EntryType.CREDIT, amount_paise=-1
            )
    except IntegrityError as e:
        assert "ledger_amount_positive" in str(e), (
            f"Wrong constraint fired: {e}"
        )
        print("[ok] CHECK (amount_paise > 0) rejected negative amount")
        return
    raise AssertionError("Negative amount was accepted — CHECK constraint missing!")


def check_bad_entry_type_rejected() -> None:
    m = Merchant.objects.first()
    try:
        with transaction.atomic(), connection.cursor() as cur:
            # Bypass Django enum validation, hit the DB CHECK directly.
            cur.execute(
                "INSERT INTO ledger_entry (id, merchant_id, entry_type, "
                "amount_paise, created_at) VALUES (gen_random_uuid(), %s, "
                "'BOGUS', 1, now())",
                [str(m.id)],
            )
    except IntegrityError as e:
        assert "ledger_entry_type_valid" in str(e), (
            f"Wrong constraint fired: {e}"
        )
        print("[ok] CHECK entry_type IN (...) rejected 'BOGUS'")
        return
    raise AssertionError("Bad entry_type was accepted — CHECK constraint missing!")


if __name__ == "__main__":
    check_balances()
    check_negative_amount_rejected()
    check_bad_entry_type_rejected()
    print("\nAll Day 1 smoke checks passed.")
