#!/usr/bin/env python3.11
import subprocess
import json
import os
import sys

# ==============================================================================
# 02_create_consumer_org.py — Consumer Org creation for test purposes
# Execution order: 02 (optional, test environment setup)
#
# Creates a Consumer Org with the given name and assigns the specified
# user (from the Local Registry) as owner.
# Prerequisite: 00_setup_env.py + 01_create_test_user.py must have run first.
# ==============================================================================

ENV_FILE = "migration_env.sh"


def load_env():
    """
    Reads migration_env.sh and loads the 'export KEY="VALUE"'
    lines into os.environ. Exits with an error if the file is not found.
    This makes all variables saved by 00_setup_env.py available
    in this script.
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


load_env()

APIC_SERVER    = os.environ.get("APIC_SERVER")
PROV_ORG       = os.environ.get("PROV_ORG")
CATALOG        = os.environ.get("CATALOG")
LOCAL_REGISTRY = os.environ.get("LOCAL_REGISTRY", "sandbox-catalog")


def get_user_url(username):
    """
    Queries the Local Registry record for the given username via the APIC CLI
    and returns the user's full APIC URL.
    This URL is required in the 'owner_url' field when creating a Consumer Org.
    Returns None if the user is not found.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        return data.get("url")
    except subprocess.CalledProcessError as e:
        print(f"--> [ERROR] Could not retrieve user URL: {e.stderr.strip() or e.stdout.strip()}")
        return None
    except json.JSONDecodeError:
        print("--> [ERROR] APIC response is not valid JSON.")
        return None


def create_consumer_org(org_name, owner_username, owner_url):
    """
    Creates a Consumer Org with the given name and assigns owner_url as the owner.
    Input is provided to the APIC CLI in YAML format (via stdin).
    Behaves idempotently — does not error if the org already exists.
    Returns True on success, False on error.
    """
    yaml_content = f"""name: "{org_name}"
title: "{org_name}"
owner_url: "{owner_url}"
"""
    cmd = [
        "apic", "consumer-orgs:create", "-",
        "-s", APIC_SERVER, "-o", PROV_ORG, "-c", CATALOG,
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        print(f"--> [SUCCESS] Consumer Org '{org_name}' created.")
        print(f"--> [INFO]    Owner: '{owner_username}'")
        return True
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() or e.stdout.strip()
        if "already exists" in err.lower():
            print(f"--> [INFO] Consumer Org '{org_name}' already exists.")
            return True
        print(f"--> [ERROR] Failed to create Consumer Org:\n{err}")
        return False


def main():
    """
    Collects org name and owner username from the user,
    then calls get_user_url() → create_consumer_org() in sequence.
    Stops if the owner user is not found in the Local Registry.
    """
    print("\n==================================================")
    print("          CREATE CONSUMER ORG                     ")
    print("==================================================")

    org_name       = input("Consumer Org Name: ").strip()
    owner_username = input("Owner Username (from Local Registry): ").strip()

    print(f"\n--> [1/2] Retrieving URL for user '{owner_username}'...")
    owner_url = get_user_url(owner_username)
    if not owner_url:
        print(f"--> [ERROR] '{owner_username}' not found in Local Registry. Please run 01_create_test_user.py first.")
        sys.exit(1)

    print(f"--> [2/2] Creating Consumer Org...")
    if not create_consumer_org(org_name, owner_username, owner_url):
        sys.exit(1)

    print("==================================================")


if __name__ == "__main__":
    main()
