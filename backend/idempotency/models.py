from django.db import models


class KeyState(models.TextChoices):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"


class IdempotencyKey(models.Model):
    # Composite uniqueness on (merchant, key) is what enforces "same key
    # returns same response" at the DB level. We keep an auto PK for ORM
    # convenience and put the unique constraint on the pair.
    merchant = models.ForeignKey(
        "ledger.Merchant", on_delete=models.CASCADE, related_name="idempotency_keys"
    )
    key = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    state = models.CharField(max_length=10, choices=KeyState.choices)
    response_status = models.IntegerField(null=True, blank=True)
    # Stored as raw JSON text (not jsonb) so replays return byte-identical
    # responses. jsonb hashes keys and loses insertion order, which would
    # break the "same response if called twice" guarantee at the byte level.
    response_body = models.TextField(null=True, blank=True)
    payout = models.ForeignKey(
        "payouts.Payout",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="idempotency_keys",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "idempotency_key"
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "key"], name="idempotency_merchant_key_unique"
            ),
            models.CheckConstraint(
                check=models.Q(state__in=["in_flight", "completed"]),
                name="idempotency_state_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["created_at"], name="idempotency_created_idx"),
        ]
