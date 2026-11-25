"""Bounded, lease-safe Stripe webhook reconciliation worker.

This module is intentionally opt-in. It is not started by the web application and
must be scheduled/run by an operator only after the non-production billing gates
are complete.
"""
from __future__ import annotations

import argparse

from . import config
from .identity import IdentityStore


def process_pending_stripe_events(
    store: IdentityStore, *, limit: int = 50, max_attempts: int = 8
) -> dict[str, int]:
    """Process a bounded batch without exposing event bodies or provider details."""
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("Worker batch limit must be between 1 and 500.")
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 12:
        raise ValueError("Worker attempts must be between 1 and 12.")

    with store.connect() as db:
        rows = db.execute(
            """SELECT stripe_event_id FROM billing_webhook_events
               WHERE processing_state IN ('received', 'failed')
                 AND encrypted_payload IS NOT NULL AND attempt_count < ?
               ORDER BY received_at, stripe_event_id LIMIT ?""",
            (max_attempts, limit),
        ).fetchall()

    totals = {
        "claimed": 0, "created": 0, "updated": 0, "stale": 0,
        "ignored": 0, "failed": 0, "lease_lost": 0,
    }
    for row in rows:
        event_id = row["stripe_event_id"]
        lease = store.claim_stripe_webhook_event(
            event_id, lease_seconds=60, max_attempts=max_attempts
        )
        if lease is None:
            continue
        totals["claimed"] += 1
        try:
            result = store.process_stripe_subscription_event(event_id, lease)
            if result in {"created", "updated", "stale", "ignored"}:
                totals[result] += 1
            else:
                totals["lease_lost"] += 1
        except LookupError:
            if store.fail_stripe_webhook_event(event_id, lease, "customer_unmapped"):
                totals["failed"] += 1
            else:
                totals["lease_lost"] += 1
        except ValueError:
            if store.fail_stripe_webhook_event(event_id, lease, "invalid_subscription_event"):
                totals["failed"] += 1
            else:
                totals["lease_lost"] += 1
        except RuntimeError:
            if store.fail_stripe_webhook_event(event_id, lease, "processing_error"):
                totals["failed"] += 1
            else:
                totals["lease_lost"] += 1
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description="Process verified Stripe webhook inbox events.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-attempts", type=int, default=8)
    args = parser.parse_args()
    store = IdentityStore(config.ACCOUNTS_DATABASE_PATH)
    totals = process_pending_stripe_events(
        store, limit=args.limit, max_attempts=args.max_attempts
    )
    print("Stripe webhook worker:", " ".join(f"{key}={value}" for key, value in totals.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
