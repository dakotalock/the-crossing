"""Outbox drain worker. HTTP to Stripe lives here, not in the ledger transaction."""

from __future__ import annotations

import argparse
import os
import time

from crossing import auth, billing, crypto, db


def run_once(*, limit: int = 50) -> int:
    rows = billing.drain_outbox(limit=limit)
    return len(rows)


def run_loop(*, interval: float = 2.0, limit: int = 50) -> None:
    while True:
        n = run_once(limit=limit)
        if n == 0:
            time.sleep(interval)


def attach_customer(account_id: str, stripe_customer_id: str) -> None:
    with db.session_scope() as s:
        billing.attach_stripe_customer(s, account_id, stripe_customer_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m crossing.worker")
    parser.add_argument("--once", action="store_true", help="claim and drain one batch then exit")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--attach-customer",
        nargs=2,
        metavar=("ACCOUNT_ID", "CUS_ID"),
        help="admin: attach a Stripe customer id to an account, then exit",
    )
    parser.add_argument(
        "--requeue-dead",
        metavar="OUTBOX_ID",
        nargs="?",
        const="*",
        help="admin: requeue dead outbox to pending with attempts=0 (omit id for all)",
    )
    args = parser.parse_args(argv)
    if os.environ.get("CROSSING_ALLOW_DEV") != "1":
        crypto.require_production_secrets()
        auth.key_pepper()
    db.init_db()
    billing.warn_unsent_fees()
    if args.attach_customer:
        attach_customer(args.attach_customer[0], args.attach_customer[1])
        return 0
    if args.requeue_dead:
        with db.session_scope() as s:
            oid = None if args.requeue_dead == "*" else args.requeue_dead
            billing.requeue_dead(s, oid)
        return 0
    if args.once:
        run_once(limit=args.limit)
        return 0
    run_loop(interval=args.interval, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
