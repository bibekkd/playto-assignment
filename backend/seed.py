"""Seed script: 3 merchants with credit history.

Idempotent — running twice does not double-credit, because we look up by name
and skip if the merchant already exists.

Usage: python manage.py shell < seed.py
   or: python -c "import django; django.setup(); ..." (use the wrapper below)
"""

import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "playto.settings")
django.setup()

from django.db import transaction  # noqa: E402

from ledger.models import EntryType, LedgerEntry, Merchant  # noqa: E402

# (name, [credit_amounts_in_paise])
# 1 rupee = 100 paise. Amounts chosen to be obviously non-round so a
# floating-point bug would show up immediately.
SEED = [
    ("Acme Studios", [50_000_00, 12_345_67, 7_890_12]),       # 70,235.79
    ("Bluegrass Agency", [200_000_00, 33_333_33]),             # 233,333.33
    ("Coral Freelancer", [9_999_99, 1_00, 5_000_00, 250_75]),  # 15,251.74
]


def run() -> None:
    with transaction.atomic():
        for name, credits in SEED:
            merchant, created = Merchant.objects.get_or_create(name=name)
            if not created:
                print(f"[skip] {name} already exists ({merchant.id})")
                continue
            for amount in credits:
                LedgerEntry.objects.create(
                    merchant=merchant,
                    entry_type=EntryType.CREDIT,
                    amount_paise=amount,
                )
            print(
                f"[ok]   {name} ({merchant.id}) "
                f"balance={merchant.balance_paise()} paise"
            )

    print("\nFinal balances:")
    for m in Merchant.objects.all().order_by("name"):
        print(f"  {m.name:25s} {m.id} balance={m.balance_paise():>12d} paise")


if __name__ == "__main__":
    run()
