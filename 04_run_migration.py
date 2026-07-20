#!/usr/bin/env python3.11
"""
04_run_migration.py — Bulk migration orchestrator.

Processes users from migration_users.csv who have not yet been migrated,
in batches of 10.

For each user, runs the following steps in order (migration_steps.py):
  1. step_01_create_kc_user      → Create user in Keycloak
  2. step_02_park_apic_email     → Append -old to the APIC email
  3. step_03_jit_provision       → APIC JIT provision (JWT-Bearer)
  4. step_04_transfer_org        → Transfer Consumer Org ownership to Keycloak profile
  5. step_05_send_password_email → Send Keycloak UPDATE_PASSWORD email

A summary report is printed after every 10 users.
If a user fails within a batch, that user is skipped
(migrated=false remains) and processing continues with the next one.

Usage:
  python 04_run_migration.py                               # all pending users
  python 04_run_migration.py --consumer-org Musti          # single consumer org
  python 04_run_migration.py --consumer-org Musti Trend    # multiple consumer orgs
  python 04_run_migration.py --limit 2                     # migrate first 2 users
  python 04_run_migration.py --username Mustafa            # by specific username
  python 04_run_migration.py --dry-run                     # print steps, do not run
  python 04_run_migration.py --batch-size 5                # custom batch size
"""

import os
import sys
import argparse

from migration_state import get_pending_users, get_users_by_org, write_status_report
from migration_steps import (
    step_01_create_kc_user,
    step_02_park_apic_email,
    step_03_jit_provision,
    step_04_transfer_org,
    step_05_send_password_email,
)

ENV_FILE   = "migration_env.sh"
BATCH_SIZE = 10


# ------------------------------------------------------------------------------
# HELPER — Run all steps for a single user
# ------------------------------------------------------------------------------

def migrate_user(username, consumer_org="", dry_run=False):
    """
    Runs the 5-step migration pipeline for a single user in sequence.
    Each step is directly imported from migration_steps.py and called (no subprocess).
    If dry_run=True, lists which function would run step by step without making changes.
    Returns True on success, False if any step fails.
    """
    steps = [
        ("1/5 KC_CREATE  ", step_01_create_kc_user,        [username, consumer_org]),
        ("2/5 EMAIL_UPD  ", step_02_park_apic_email,       [username]),
        ("3/5 JIT_PROV   ", step_03_jit_provision,         [username]),
        ("4/5 ORG_XFER   ", step_04_transfer_org,          [username]),
        ("5/5 SEND_MAIL  ", step_05_send_password_email,   [username]),
    ]

    if dry_run:
        for label, fn, _ in steps:
            print(f"    [{label}] {fn.__name__}({username})  [DRY-RUN]")
        return True

    for label, fn, args in steps:
        print(f"    [{label}] {fn.__name__}({username}) ...", end=" ", flush=True)
        ok = fn(*args)
        if ok:
            print("OK")
        else:
            print("FAILED")
            return False
    return True


# ------------------------------------------------------------------------------
# MAIN BATCH LOOP
# ------------------------------------------------------------------------------

def run_batch(batch_size=BATCH_SIZE, dry_run=False, limit=None, usernames=None, consumer_orgs=None):
    """
    Migrates pending users from the CSV in batches.

    consumer_orgs: only users belonging to these consumer orgs are processed (recommended filter)
    usernames    : only these usernames are processed
    limit        : maximum number of users to process (None = unlimited)
    Filters can be combined; order: consumer_org → username → limit.
    """
    pending = get_pending_users()

    if not pending:
        print("--> [INFO] No users pending migration.")
        rpt = write_status_report()
        print(f"--> [INFO] Current status: {rpt}")
        return

    # --consumer-org filter
    if consumer_orgs:
        org_set   = {o.lower() for o in consumer_orgs}
        not_found = org_set - {u["consumer_org"].lower() for u in pending}
        if not_found:
            print(f"--> [WARNING] The following orgs are not in the CSV or already migrated: {', '.join(sorted(not_found))}")
        pending = [u for u in pending if u.get("consumer_org", "").lower() in org_set]

    # --username filter
    if usernames:
        username_set = set(usernames)
        not_found    = username_set - {u["username"] for u in pending}
        if not_found:
            print(f"--> [WARNING] The following users are not in the CSV or already migrated: {', '.join(sorted(not_found))}")
        pending = [u for u in pending if u["username"] in username_set]

    # --limit filter
    if limit is not None and limit > 0:
        pending = pending[:limit]

    if not pending:
        print("--> [INFO] No users remaining to process after filtering.")
        return

    total      = len(pending)
    success_ct = 0
    fail_ct    = 0
    batch_no   = 0

    print(f"\n{'='*60}")
    print(f"  BATCH MIGRATION STARTED")
    print(f"  Total users  : {total}")
    print(f"  Batch size   : {batch_size}")
    if consumer_orgs:
        print(f"  Org filter   : {', '.join(consumer_orgs)}")
    if usernames:
        print(f"  User filter  : {', '.join(usernames)}")
    if limit is not None:
        print(f"  Limit        : {limit}")
    if dry_run:
        print("  MODE             : DRY-RUN (no changes will be made)")
    print(f"{'='*60}\n")

    # Split users into groups of batch_size
    for batch_start in range(0, total, batch_size):
        batch_no  += 1
        batch      = pending[batch_start : batch_start + batch_size]
        b_success  = 0
        b_fail     = 0
        b_failed_users = []

        print(f"--- BATCH {batch_no} ({len(batch)} users) ---")

        for user in batch:
            username = user["username"]
            org      = user.get("consumer_org", "")
            print(f"  >> {username} [{org or '-'}]")

            ok = migrate_user(username, consumer_org=org, dry_run=dry_run)
            if ok:
                b_success += 1
                success_ct += 1
            else:
                b_fail += 1
                fail_ct += 1
                b_failed_users.append(username)

        # Summary after each batch
        print(f"\n  Batch {batch_no} summary: {b_success} succeeded / {b_fail} failed")
        if b_failed_users:
            print(f"  Failed: {', '.join(b_failed_users)}")

        # Write live CSV status to file
        rpt = write_status_report()
        print(f"\n  [SNAPSHOT] End of batch {batch_no} → {rpt}")

    # Overall summary
    print(f"\n{'='*60}")
    print(f"  BATCH MIGRATION COMPLETED")
    print(f"  Succeeded : {success_ct} / {total}")
    print(f"  Failed    : {fail_ct} / {total}")
    print(f"{'='*60}\n")

    if fail_ct > 0:
        sys.exit(1)


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrates users from migration_users.csv in batches.",
        prog="04_run_migration.py"
    )
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE,
        help=f"Number of users per batch (default: {BATCH_SIZE})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List steps without executing them"
    )
    parser.add_argument(
        "--consumer-org", nargs="+", dest="consumer_orgs", default=None,
        metavar="ORG",
        help="Migrate only the specified consumer orgs (space-separated for multiple)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of users to process (default: unlimited)"
    )
    parser.add_argument(
        "--username", nargs="+", dest="usernames", default=None,
        metavar="USERNAME",
        help="Migrate only the specified users (space-separated for multiple)"
    )
    args = parser.parse_args()

    run_batch(
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        limit=args.limit,
        usernames=args.usernames,
        consumer_orgs=args.consumer_orgs,
    )
