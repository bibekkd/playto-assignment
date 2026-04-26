"""Drain pending payouts and retry stuck ones.

Designed to be invoked by a scheduled job (Render Cron, GitHub Actions,
crontab — anything). Replaces a long-lived Celery worker on platforms
where workers cost money. Runs both phases in one shot:

  1. process_pending_payouts — picks up newly-created payouts in `pending`
  2. retry_stuck_payouts     — re-enqueues anything stuck in `processing`
                               past STUCK_AFTER_SECONDS

Idempotent and safe to run on overlapping schedules — both queries use
SELECT ... FOR UPDATE SKIP LOCKED.

Usage:
    python manage.py drain_payouts
    python manage.py drain_payouts --limit 200
"""

from django.core.management.base import BaseCommand

from payouts.tasks import process_pending_payouts, retry_stuck_payouts


class Command(BaseCommand):
    help = "Drain pending payouts and retry stuck ones (cron-style worker)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Max payouts to drain in this run (default 100).",
        )

    def handle(self, *args, **opts):
        drained = process_pending_payouts(limit=opts["limit"])
        requeued = retry_stuck_payouts()
        self.stdout.write(
            self.style.SUCCESS(
                f"drain_payouts: drained={drained} requeued={requeued}"
            )
        )
