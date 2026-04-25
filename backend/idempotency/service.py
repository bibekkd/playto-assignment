"""Idempotency service.

Three cases handled (see planning.md §5):
  (a) First request for (merchant, key) — claim the row in `in_flight`,
      caller does its work, calls `complete()`.
  (b) Replay after completion — return the cached response.
  (c) Replay while still in flight — return 409.

A fourth case is "same key, different body" — fingerprint mismatch — which
returns 422.

TTL: 24h, enforced as a read-side filter (authoritative). The periodic
cleanup is housekeeping, not the source of truth.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import IdempotencyKey, KeyState

TTL = timedelta(hours=24)


def fingerprint(body: dict[str, Any]) -> str:
    """Stable sha256 of the request body for "same key, different body"."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Replay:
    """A previously-completed request being served from cache.

    `body_text` is the raw JSON string that was returned the first time.
    Replays return it verbatim — byte-identical, including key order.
    """

    status: int
    body_text: str


@dataclass
class InFlight:
    """A request with this key is still being processed."""


@dataclass
class FingerprintMismatch:
    """Same key, different body."""


@dataclass
class Claimed:
    """Caller now owns this key; proceed with the work."""

    row: IdempotencyKey


def claim(merchant_id, key, body: dict[str, Any]):
    """Try to take ownership of (merchant, key).

    Returns one of: Replay, InFlight, FingerprintMismatch, Claimed.
    Caller is expected to dispatch on the type.
    """
    fp = fingerprint(body)
    cutoff = timezone.now() - TTL

    # Read-side TTL filter is authoritative. An expired-but-not-yet-swept
    # row is treated as if it doesn't exist — we'll INSERT over it after
    # cleaning it up.
    existing = (
        IdempotencyKey.objects.filter(
            merchant_id=merchant_id, key=key, created_at__gt=cutoff
        ).first()
    )
    if existing is not None:
        if existing.request_fingerprint != fp:
            return FingerprintMismatch()
        if existing.state == KeyState.IN_FLIGHT:
            return InFlight()
        return Replay(
            status=existing.response_status, body_text=existing.response_body
        )

    # No live row. If a stale (expired) row exists, drop it so we can insert.
    IdempotencyKey.objects.filter(
        merchant_id=merchant_id, key=key, created_at__lte=cutoff
    ).delete()

    try:
        row = IdempotencyKey.objects.create(
            merchant_id=merchant_id,
            key=key,
            request_fingerprint=fp,
            state=KeyState.IN_FLIGHT,
        )
    except IntegrityError:
        # Lost a race with a sibling request that inserted the same key
        # between our SELECT and INSERT. Re-read and dispatch.
        row = IdempotencyKey.objects.get(merchant_id=merchant_id, key=key)
        if row.request_fingerprint != fp:
            return FingerprintMismatch()
        if row.state == KeyState.IN_FLIGHT:
            return InFlight()
        return Replay(status=row.response_status, body_text=row.response_body)

    return Claimed(row=row)


def complete(row: IdempotencyKey, status: int, body_text: str, payout_id=None) -> None:
    """Mark the claimed row as completed and cache the response.

    `body_text` is the already-rendered JSON string we will replay verbatim.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("complete() must be called inside transaction.atomic()")
    row.state = KeyState.COMPLETED
    row.response_status = status
    row.response_body = body_text
    row.payout_id = payout_id
    row.save(
        update_fields=["state", "response_status", "response_body", "payout_id"]
    )
