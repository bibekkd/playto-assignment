import uuid

from django.db import models
from django.db.models import Case, IntegerField, Sum, Value, When
from django.db.models.functions import Coalesce


class EntryType(models.TextChoices):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"
    REVERSAL = "REVERSAL"


class Merchant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "merchant"

    def __str__(self) -> str:
        return self.name

    def balance_paise(self) -> int:
        # Single-query CASE aggregate. No Python arithmetic on rows.
        # Held funds are NOT separated here; DEBIT entries are written at
        # request time, so this number is the merchant's available balance.
        result = LedgerEntry.objects.filter(merchant_id=self.id).aggregate(
            total=Coalesce(
                Sum(
                    Case(
                        When(entry_type=EntryType.CREDIT, then=models.F("amount_paise")),
                        When(entry_type=EntryType.DEBIT, then=-models.F("amount_paise")),
                        When(entry_type=EntryType.REVERSAL, then=models.F("amount_paise")),
                        output_field=models.BigIntegerField(),
                    )
                ),
                Value(0, output_field=models.BigIntegerField()),
            )
        )
        return int(result["total"])

    def held_paise(self) -> int:
        # DEBITs whose payout is still pending or processing.
        result = LedgerEntry.objects.filter(
            merchant_id=self.id,
            entry_type=EntryType.DEBIT,
            payout__status__in=("pending", "processing"),
        ).aggregate(
            total=Coalesce(
                Sum("amount_paise"), Value(0, output_field=models.BigIntegerField())
            )
        )
        return int(result["total"])


class LedgerEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    entry_type = models.CharField(max_length=10, choices=EntryType.choices)
    amount_paise = models.BigIntegerField()
    payout = models.ForeignKey(
        "payouts.Payout",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_entry"
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount_paise__gt=0),
                name="ledger_amount_positive",
            ),
            models.CheckConstraint(
                check=models.Q(entry_type__in=["CREDIT", "DEBIT", "REVERSAL"]),
                name="ledger_entry_type_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["merchant", "-created_at"], name="ledger_merchant_created_idx"
            ),
            models.Index(fields=["payout"], name="ledger_payout_idx"),
        ]
