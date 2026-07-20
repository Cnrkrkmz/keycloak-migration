#!/usr/bin/env python3.11
"""
03_export_consumer_orgs.py — APIC Consumer Org & Owner Export

Execution order: 03  (AFTER 00_setup_env.py, BEFORE 04_run_migration.py)

Fetches all consumer orgs from the APIC API using pagination,
queries the owner user for each org separately, and writes the
results to migration_users.csv.

This CSV is used as migration input by 04_run_migration.py.

When to re-run:
  - Must be run at least once before migration (creates the CSV).
  - Can be re-run if new consumer orgs are added to APIC;
    preserves existing CSV records and only appends new orgs.
  - Already migrated (migrated=true) users remain in the CSV
    and will not be re-added on the next run (username check).

Usage:
  python 03_export_consumer_orgs.py              # all orgs
  python 03_export_consumer_orgs.py --page-size 25   # custom page size
  python 03_export_consumer_orgs.py --dry-run    # print only, do not write to CSV
"""

import subprocess
import json
import os
import sys
import csv
import argparse
from datetime import datetime

ENV_FILE   = "migration_env.sh"
CSV_FILE   = "migration_users.csv"
PAGE_SIZE  = 50   # --limit value sent to APIC

CSV_FIELDS = [
    # SOURCE — APIC Local Registry (legacy system)
    "username",
    "consumer_org",
    "source_email",
    # TARGET — Keycloak (new system)
    "target_email",
    "kc_user_created",
    "apic_email_parked",
    "apic_jit_done",
    "org_owner_xfrd",
    # STATUS
    "migrated",
    "migrated_at",
]


# ------------------------------------------------------------------------------
# ENVIRONMENT
# ------------------------------------------------------------------------------

def load_env():
    """
    Reads migration_env.sh and loads 'export KEY="VALUE"' lines
    into os.environ. Exits with an error if the file is not found.
    """
    if not os.path.exists(ENV_FILE):
        print(f"--> [ERROR] '{ENV_FILE}' not found! Please run 00_setup_env.py first.")
        sys.exit(1)
    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                kv = line[7:].split("=", 1)
                if len(kv) == 2:
                    os.environ[kv[0]] = kv[1].strip('"\'')


# ------------------------------------------------------------------------------
# APIC QUERIES
# ------------------------------------------------------------------------------

def _apic_run(cmd):
    """Runs an APIC CLI command and parses the JSON output. Returns None on error."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        print(f"--> [ERROR] APIC command failed: {' '.join(cmd)}\n    {err}")
        return None
    except json.JSONDecodeError:
        print(f"--> [ERROR] APIC response is not valid JSON: {' '.join(cmd)}")
        return None


def fetch_consumer_orgs_page(server, prov_org, catalog, limit, offset):
    """
    Returns one page of consumer orgs for the given limit/offset.
    Expected response format: {"total_results": N, "results": [...]}
    """
    cmd = [
        "apic", "consumer-orgs:list",
        "-s", server, "-o", prov_org, "-c", catalog,
        "--format", "json", "--output", "-",
        "--limit",  str(limit),
        "--offset", str(offset),
    ]
    return _apic_run(cmd)


def fetch_all_consumer_orgs(server, prov_org, catalog, page_size):
    """
    Fetches all consumer orgs using pagination and returns a combined list.
    Progress is printed to the screen after each page.
    """
    all_orgs  = []
    offset    = 0
    page_no   = 0
    total     = None

    while True:
        page_no += 1
        print(f"--> [APIC] Fetching page {page_no} (offset={offset}, limit={page_size})...")

        data = fetch_consumer_orgs_page(server, prov_org, catalog, page_size, offset)
        if data is None:
            print("--> [ERROR] Failed to fetch page, stopping.")
            break

        results = data.get("results", [])
        if total is None:
            total = data.get("total_results", 0)
            print(f"--> [INFO] Total consumer org count: {total}")

        all_orgs.extend(results)
        print(f"--> [INFO] {len(results)} orgs retrieved on this page. Total fetched: {len(all_orgs)}/{total}")

        # Have we reached the last page?
        offset += page_size
        if offset >= total or not results:
            break

    return all_orgs


def fetch_owner_info(server, prov_org, catalog, consumer_org_name, owner_url):
    """
    Fetches the owner user of a consumer org via members:list.
    owner_url is the value from the consumer-orgs:list response — used for matching.
    Returns: {"username": ..., "email": ...} or None
    """
    cmd_members = [
        "apic", "members:list", "--scope", "consumer-org",
        "-s", server, "-o", prov_org, "-c", catalog,
        "--consumer-org", consumer_org_name,
        "--format", "json", "--output", "-",
    ]
    members_data = _apic_run(cmd_members)
    if not members_data:
        return None

    items = members_data.get("results", [])

    keycloak_registry = os.environ.get("KEYCLOAK_REGISTRY_NAME", "keycluk")

    # First try exact match by owner_url; fall back to role=owner if not found.
    # If the owner is already a Keycloak user, this org was previously migrated — skip.
    for member in items:
        user = member.get("user", {})
        if user.get("identity_provider") == keycloak_registry:
            print(f"--> [INFO] '{consumer_org_name}' already belongs to a Keycloak user. Skipping.")
            return None
        if owner_url and user.get("url") == owner_url:
            # The APIC "name" field is the case-preserved value to pass to users:get.
            # "username" may be normalised to lowercase and cause "Not found" errors
            # when queried with users:get.
            return {"username": user.get("name") or user.get("username", ""), "email": user.get("email", "")}

    for member in items:
        user = member.get("user", {})
        if member.get("role", "") == "owner":
            return {"username": user.get("name") or user.get("username", ""), "email": user.get("email", "")}

    print(f"--> [WARNING] No owner member found for '{consumer_org_name}'.")
    return None


# ------------------------------------------------------------------------------
# CSV
# ------------------------------------------------------------------------------

def load_existing_csv():
    """
    Returns the existing migration_users.csv as a {username: row} dict.
    Returns an empty dict if the file does not exist. Used to preserve
    existing records on re-runs.
    """
    if not os.path.exists(CSV_FILE):
        return {}
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return {row["username"]: row for row in csv.DictReader(f)}


def write_csv(rows):
    """
    Writes all rows (existing + new) to migration_users.csv.
    Rewrites the entire file on each run; therefore existing records
    are loaded into memory via load_existing_csv() and included in
    the new_rows list before writing.
    """
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Exports APIC consumer orgs and their owner users to CSV."
    )
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE,
                        help=f"APIC paging limit (default: {PAGE_SIZE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print only, do not write to CSV")
    args = parser.parse_args()

    load_env()

    server   = os.environ["APIC_SERVER"]
    prov_org = os.environ["PROV_ORG"]
    catalog  = os.environ["CATALOG"]

    print("\n==================================================")
    print("  CONSUMER ORG & OWNER EXPORT                    ")
    print(f"  Server  : {server}")
    print(f"  Org     : {prov_org}  |  Catalog: {catalog}")
    print(f"  Page    : {args.page_size} orgs/page")
    if args.dry_run:
        print("  MODE    : DRY-RUN (will not write to CSV)")
    print("==================================================\n")

    # Fetch all consumer orgs
    orgs = fetch_all_consumer_orgs(server, prov_org, catalog, args.page_size)
    if not orgs:
        print("--> [ERROR] No consumer orgs found, exiting.")
        sys.exit(1)

    # LOAD EXISTING CSV INTO MEMORY (as a dict)
    existing_users = load_existing_csv()

    skipped   = 0
    added     = 0
    failed    = 0
    already   = 0   # number of orgs already owned by a Keycloak user

    print(f"\n--> [INFO] Fetching owner information for {len(orgs)} orgs...\n")

    for org in orgs:
        org_name  = org.get("name", "")
        owner_url = org.get("owner_url", "")
        if not org_name:
            continue

        owner = fetch_owner_info(server, prov_org, catalog, org_name, owner_url)
        if owner is None:
            already += 1
            continue
        if not owner.get("username"):
            print(f"--> [WARNING] Owner username is empty for '{org_name}', skipping.")
            failed += 1
            continue

        username = owner["username"]
        email    = owner["email"]

        # IF THE USER IS ALREADY IN THE IN-MEMORY CSV, DO NOT TOUCH IT
        if username in existing_users:
            print(f"--> [SKIP] '{username}' ({org_name}) already recorded in CSV.")
            skipped += 1
            continue

        # IF IT IS A NEW USER, ADD IT TO THE IN-MEMORY DICT AS WELL
        row = {
            "username":          username,
            "consumer_org":      org_name,
            "source_email":      "",
            "target_email":      email,
            "kc_user_created":   "false",
            "apic_email_parked": "false",
            "apic_jit_done":     "false",
            "org_owner_xfrd":    "false",
            "migrated":          "false",
            "migrated_at":       "",
        }
        existing_users[username] = row
        added += 1
        print(f"--> [+] {username:<30} | {org_name:<40} | {email}")

    print(f"\n--------------------------------------------------")
    print(f"  Total orgs      : {len(orgs)}")
    print(f"  New records     : {added}")
    print(f"  Already in CSV  : {skipped}")
    print(f"  Already in KC   : {already}")
    print(f"  Failed          : {failed}")
    print(f"--------------------------------------------------")

    if args.dry_run:
        print("\n--> [DRY-RUN] Not written to CSV.")
    else:
        final_rows = list(existing_users.values())
        write_csv(final_rows)
        print(f"\n--> [SUCCESS] {len(final_rows)} rows written to '{CSV_FILE}'.")
        print(f"    Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    print("==================================================\n")

if __name__ == "__main__":
    main()
