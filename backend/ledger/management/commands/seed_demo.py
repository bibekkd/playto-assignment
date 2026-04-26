"""Seed the demo merchants. Idempotent — safe to re-run.

Usage:
    python manage.py seed_demo         # writes 3 merchants if absent
    python manage.py seed_demo --reset # delete everything first (DEV ONLY)

This replaces piping `seed.py` into a remote shell — Render's Shell tab
makes `python manage.py seed_demo` the cleanest path.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from ledger.models import EntryType, LedgerEntry, Merchant


SEED = [
    ("Acme Studios", [50_000_00, 12_345_67, 7_890_12]),       # 70,235.79
    ("Bluegrass Agency", [200_000_00, 33_333_33]),             # 233,333.33
    ("Coral Freelancer", [9_999_99, 1_00, 5_000_00, 250_75]),  # 15,251.74
]


class Command(BaseCommand):
    help = "Seed 3 demo merchants with credit history. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="DEV ONLY: delete all merchants and their ledger entries first.",
        )

    def handle(self, *args, **opts):
        if opts["reset"]:
            self.stdout.write(self.style.WARNING("--reset: deleting everything"))
            with transaction.atomic():
                # Order matters — payouts → ledger → merchants
                from payouts.models import Payout
                from idempotency.models import IdempotencyKey
                IdempotencyKey.objects.all().delete()
                LedgerEntry.objects.all().delete()
                Payout.objects.all().delete()
                Merchant.objects.all().delete()

        with transaction.atomic():
            for name, credits in SEED:
                merchant, created = Merchant.objects.get_or_create(name=name)
                if not created:
                    self.stdout.write(f"[skip] {name} exists ({merchant.id})")
                    continue
                for amount in credits:
                    LedgerEntry.objects.create(
                        merchant=merchant,
                        entry_type=EntryType.CREDIT,
                        amount_paise=amount,
                    )
                self.stdout.write(self.style.SUCCESS(
                    f"[ok]   {name} ({merchant.id}) "
                    f"balance={merchant.balance_paise()} paise"
                ))

        self.stdout.write("\nFinal balances:")
        for m in Merchant.objects.all().order_by("name"):
            self.stdout.write(
                f"  {m.name:25s} {m.id} balance={m.balance_paise():>12d} paise"
            )
