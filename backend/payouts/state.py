"""Payout state machine.

The single source of truth for legal status transitions. The view and the
worker MUST go through `transition_to()` — never assign `payout.status`
directly. A grep for `payout.status =` outside this file should return zero
hits.
"""

from __future__ import annotations

from django.db import transaction

from .models import Payout, PayoutStatus

# Legal transitions. Anything not in this set is rejected.
LEGAL_TRANSITIONS: set[tuple[str, str]] = {
    (PayoutStatus.PENDING, PayoutStatus.PROCESSING),
    (PayoutStatus.PROCESSING, PayoutStatus.COMPLETED),
    (PayoutStatus.PROCESSING, PayoutStatus.FAILED),
    # Retry path: the worker re-enters processing on a fresh attempt.
    (PayoutStatus.PROCESSING, PayoutStatus.PROCESSING),
}


class IllegalTransition(Exception):
    """Attempted a status change that the state machine forbids."""

    def __init__(self, current: str, attempted: str) -> None:
        super().__init__(
            f"Illegal payout transition: {current} → {attempted}"
        )
        self.current = current
        self.attempted = attempted


def transition(payout_id, new_status: str) -> Payout:
    """Atomically move a payout to `new_status`.

    Re-reads the payout under `SELECT … FOR UPDATE` so concurrent workers
    see a consistent current status. Caller must already be inside a
    `transaction.atomic()` block when side effects (e.g. writing a
    REVERSAL) need to commit together with the status change.
    """
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
