#!/usr/bin/env python3.11
import subprocess
import json
import os
import sys

# ==============================================================================
# APIC TEST USER CREATION SCRIPT (LOCAL REGISTRY)
# ==============================================================================
# Execution order: 01 (test environment setup — before migration)
#
# Adds the user only to the Local Registry (global pool).
# To create a Consumer Org and assign this user as owner, use
# 02_create_consumer_org.py.
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
                key_value = line[7:].split("=", 1)
                if len(key_value) == 2:
                    os.environ[key_value[0]] = key_value[1].strip('"\'')

load_env()

APIC_SERVER    = os.environ.get("APIC_SERVER")
PROV_ORG       = os.environ.get("PROV_ORG")
LOCAL_REGISTRY = os.environ.get("LOCAL_REGISTRY", "sandbox-catalog")


def prompt_user_details():
    """
    Interactively collects username, email, first name, last name,
    and password for a new user, then returns them as a tuple.
    These values are passed to create_global_user().
    """
    print("\n==================================================")
    print("        CREATE NEW APIC TEST USER                 ")
    print("==================================================")
    username   = input("Username (no spaces): ").strip()
    email      = input("Email Address: ").strip()
    first_name = input("First Name: ").strip()
    last_name  = input("Last Name: ").strip()
    password   = input("Password: ").strip()
    return username, email, first_name, last_name, password


def create_global_user(username, email, first_name, last_name, password):
    """
    Creates a new user in the APIC Local Registry with the given details.
    Input is provided to the APIC CLI in YAML format (via stdin).
    The user is added only to the global user pool; they are not
    assigned to any Consumer Org. Use 02_create_consumer_org.py for org assignment.
    Returns True on success, False on error.
    """
    yaml_content = f"""username: "{username}"
email: "{email}"
first_name: "{first_name}"
last_name: "{last_name}"
title: "{username}"
password: "{password}"
"""
    cmd = [
        "apic", "users:create", "-",
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        print(f"--> [SUCCESS] User '{username}' created in '{LOCAL_REGISTRY}' registry.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"--> [ERROR] Failed to create user:\n{e.stderr.strip() or e.stdout.strip()}")
        return False


def main():
    """
    Collects details via prompt_user_details(),
    then creates the user via create_global_user().
    Exits with code 1 on error.
    """
    username, email, first_name, last_name, password = prompt_user_details()

    print("\n--> Starting operation...")
    if not create_global_user(username, email, first_name, last_name, password):
        sys.exit(1)

    print("==================================================")

if __name__ == "__main__":
    main()
