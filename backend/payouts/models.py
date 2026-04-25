import uuid

from django.db import models


class PayoutStatus(models.TextChoices):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Payout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        "ledger.Merchant", on_delete=models.PROTECT, related_name="payouts"
    )
    amount_paise = models.BigIntegerField()
    bank_account_id = models.UUIDField()
    status = models.CharField(
        max_length=12, choices=PayoutStatus.choices, default=PayoutStatus.PENDING
    )
    attempts = models.IntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payout"
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount_paise__gt=0),
                name="payout_amount_positive",
            ),
            models.CheckConstraint(
                check=models.Q(
                    status__in=["pending", "processing", "completed", "failed"]
                ),
                name="payout_status_valid",
            ),
            models.CheckConstraint(
                check=models.Q(attempts__gte=0),
                name="payout_attempts_nonneg",
            ),
        ]
        indexes = [
            models.Index(fields=["merchant", "-created_at"], name="payout_merchant_idx"),
            models.Index(
                fields=["status", "last_attempt_at"], name="payout_stuck_sweep_idx"
            ),
        ]
