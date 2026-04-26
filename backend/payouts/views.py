"""Payout API views.

The POST view is the load-bearing one in this service. Keep it readable.
Anything that can be moved out without harming clarity has been moved out:
  - state transitions live in `payouts.state`
  - idempotency lives in `idempotency.service`
  - balance computation lives on `Merchant.balance_paise()`
"""

from __future__ import annotations

import uuid

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from idempotency import service as idem
from idempotency.models import IdempotencyKey
from ledger.models import EntryType, LedgerEntry, Merchant

from .models import Payout
from .serializers import PayoutCreateSerializer, PayoutReadSerializer


def _render_json(data) -> str:
    """Render a serializer dict to a stable JSON string.

    The bytes we return on the first response are the same bytes we will
    replay on every subsequent same-key request — that's what makes the
    idempotency contract byte-identical, not just semantically equal.
    """
    return JSONRenderer().render(data).decode("utf-8")


def _replay(body_text: str, status_code: int) -> HttpResponse:
    return HttpResponse(
        body_text, status=status_code, content_type="application/json"
    )


def _parse_idempotency_header(request) -> uuid.UUID | None:
    raw = request.headers.get("Idempotency-Key")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


@api_view(["POST"])
def create_payout(request, merchant_id):
    """POST /api/v1/merchants/<merchant_id>/payouts.

    Order of operations is non-negotiable (planning.md §4):
      1. validate input
      2. claim idempotency key (or replay)
      3. BEGIN
      4. SELECT merchant FOR UPDATE  ← serialization point
      5. compute balance with single-CASE aggregate (AFTER the lock)
      6. if sufficient: insert Payout(pending) + LedgerEntry(DEBIT)
         else: cache 422 on idempotency row and return
      7. COMMIT
      8. enqueue process_payout
    """
    idem_key = _parse_idempotency_header(request)
    if idem_key is None:
        return Response(
            {"error": "missing_or_invalid_idempotency_key"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = PayoutCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    body = serializer.validated_data
    body_for_fingerprint = {
        "amount_paise": body["amount_paise"],
        "bank_account_id": str(body["bank_account_id"]),
    }

    # Confirm merchant exists outside the lock — fast-fail on bad path.
    get_object_or_404(Merchant, pk=merchant_id)

    # Step 2: claim idempotency. Tiny txn so the IN_FLIGHT row is visible
    # to sibling requests immediately.
    with transaction.atomic():
        outcome = idem.claim(merchant_id, idem_key, body_for_fingerprint)

    if isinstance(outcome, idem.Replay):
        return _replay(outcome.body_text, outcome.status)
    if isinstance(outcome, idem.InFlight):
        return Response(
            {"error": "request_in_flight"},
            status=status.HTTP_409_CONFLICT,
            headers={"Retry-After": "1"},
        )
    if isinstance(outcome, idem.FingerprintMismatch):
        return Response(
            {"error": "idempotency_key_reused_with_different_body"},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    assert isinstance(outcome, idem.Claimed)
    idem_row = outcome.row

    amount = body["amount_paise"]

    # Steps 3-7: the money-moving transaction.
    with transaction.atomic():
        # Step 4: lock the merchant row. Concurrent requests for the same
        # merchant block here. This is the serialization point.
        Merchant.objects.select_for_update().get(pk=merchant_id)

        # Step 5: compute balance AFTER the lock is held.
        merchant = Merchant.objects.get(pk=merchant_id)
        available = merchant.balance_paise()

        if available < amount:
            response_dict = {
                "error": "insufficient_balance",
                "available_paise": available,
                "requested_paise": amount,
            }
            body_text = _render_json(response_dict)
            row = IdempotencyKey.objects.get(pk=idem_row.pk)
            idem.complete(
                row,
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                body_text=body_text,
            )
            return _replay(body_text, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Step 6: create the payout and the DEBIT in the same transaction
        # as the balance check. This is what makes "exactly one wins" true.
        payout = Payout.objects.create(
            merchant=merchant,
            amount_paise=amount,
            bank_account_id=body["bank_account_id"],
        )
        LedgerEntry.objects.create(
            merchant=merchant,
            entry_type=EntryType.DEBIT,
            amount_paise=amount,
            payout=payout,
        )

        body_text = _render_json(PayoutReadSerializer(payout).data)
        row = IdempotencyKey.objects.get(pk=idem_row.pk)
        idem.complete(
            row,
            status=status.HTTP_201_CREATED,
            body_text=body_text,
            payout_id=payout.id,
        )

    # Step 8: enqueue work AFTER commit so the worker can't read a row
    # that hasn't been committed yet.
    #
    # In `cron` mode (Render free tier) there's no long-lived Celery
    # worker — a scheduled `manage.py drain_payouts` picks up pending
    # rows directly from Postgres. We skip the .delay() round-trip so
    # we don't pile messages onto a queue nobody is reading.
    from django.conf import settings as _settings
    if _settings.PAYOUT_WORKER_MODE != "cron":
        from .tasks import process_payout
        process_payout.delay(str(payout.id))

    return _replay(body_text, status.HTTP_201_CREATED)


@api_view(["GET"])
def list_payouts(request, merchant_id):
    get_object_or_404(Merchant, pk=merchant_id)
    qs = Payout.objects.filter(merchant_id=merchant_id).order_by("-created_at")[:50]
    return Response(PayoutReadSerializer(qs, many=True).data)


@api_view(["GET"])
def merchant_detail(request, merchant_id):
    merchant = get_object_or_404(Merchant, pk=merchant_id)
    recent = (
        LedgerEntry.objects.filter(merchant=merchant)
        .order_by("-created_at")[:10]
        .values("id", "entry_type", "amount_paise", "payout_id", "created_at")
    )
    return Response(
        {
            "id": str(merchant.id),
            "name": merchant.name,
            "available_paise": merchant.balance_paise(),
            "held_paise": merchant.held_paise(),
            "recent_entries": list(recent),
        }
    )


@api_view(["POST"])
def drain(request):
    """Internal endpoint hit by an external scheduler (UptimeRobot, etc).

    Drains `pending` payouts and retries `processing` payouts that are
    stuck. Protected by a shared secret header so a random scanner can't
    trigger it.

    Auth: header `X-Drain-Token` must equal the `DRAIN_TOKEN` env var.
    Returns 503 if no token is configured (i.e. this endpoint is disabled).
    """
    from django.conf import settings as _settings
    from .tasks import process_pending_payouts, retry_stuck_payouts

    expected = _settings.DRAIN_TOKEN
    if not expected:
        return Response(
            {"error": "drain_disabled"}, status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    presented = request.headers.get("X-Drain-Token", "")
    # Constant-time comparison to avoid timing oracle.
    import hmac
    if not hmac.compare_digest(presented, expected):
        return Response(
            {"error": "unauthorized"}, status=status.HTTP_401_UNAUTHORIZED
        )

    drained = process_pending_payouts()
    requeued = retry_stuck_payouts()
    return Response({"drained": drained, "requeued": requeued})


@api_view(["GET"])
def list_merchants(request):
    out = [
        {
            "id": str(m.id),
            "name": m.name,
            "available_paise": m.balance_paise(),
        }
        for m in Merchant.objects.all().order_by("name")
    ]
    return Response(out)
