#!/usr/bin/env python3.11
"""
migration_steps.py — Migration Pipeline: 4 Steps (Single File)
===============================================================
Called by : 04_run_migration.py (imported and called directly)
             For single-user testing: python migration_steps.py <username>

Steps included:
  step_01_create_kc_user(username, consumer_org)
      Creates the user in Keycloak. Writes the temporary password to migration_env.sh.
      CSV → kc_user_created = true

  step_02_park_apic_email(username)
      Parks the APIC email by appending -old@<domain> to the address.
      CSV → apic_email_parked = true, source_email = <parked address>

  step_03_jit_provision(username)
      Logs in to the APIC consumer token endpoint via password grant;
      APIC opens a Keycloak shadow user record in its own database (JIT).
      CSV → apic_jit_done = true, migrated = true
      ENV → KC_TEMP_PASSWORD is cleared

  step_04_transfer_org(username)
      Transfers Consumer Org ownership from the Local Registry user
      to the Keycloak shadow user (--cascade).
      CSV → org_owner_xfrd = true, migrated = true

Common Errors in Customer Environments (general):
  - 'apic' CLI not found → Add APIC Toolkit to PATH.
  - Could not obtain token / 401 → Re-run 00_setup_env.py.
  - SSL: CERTIFICATE_VERIFY_FAILED → CA bundle required in production.
    Update the _SSL_CTX block in this file.
"""

import subprocess
import json
import os
import sys
import ssl
import urllib.request
import urllib.error
import urllib.parse
import secrets
import string

# SSL verification disabled for lab/test environment.
# In production: _SSL_CTX = ssl.create_default_context(cafile="/path/to/ca-bundle.crt")
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE

from migration_state import get_user, add_user, update_flag, update_source_email, mark_migrated

ENV_FILE = "migration_env.sh"


# ==============================================================================
# COMMON HELPERS
# ==============================================================================

def load_env():
    """
    Reads migration_env.sh and loads 'export KEY="VALUE"' lines into os.environ.
    Each step must call this before reading its critical values
    (especially step_03, which needs KC_TEMP_PASSWORD).
    Exits via sys.exit(1) if the file is not found.
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


def _get_env(key, default=""):
    """
    Reads a value from os.environ. Must be called after load_env().
    Values are read at function call time, not at module load time;
    this keeps values up to date after load_env() is called inside 04_run_migration.py.
    """
    return os.environ.get(key, default)


def _http(url, *, data=None, method=None, headers=None):
    """
    Minimal urllib wrapper. Returns (status_code, parsed_body).
    Raises urllib.error.HTTPError on non-2xx responses.
    """
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
        body = resp.read().decode()
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body


# ==============================================================================
# STEP 1 — Create User in Keycloak
# ==============================================================================

class _ApicUser:
    """
    Takes raw JSON data from APIC and converts it into a simple object
    holding the fields required for migration.
    APIC returns snake_case in some versions and camelCase in others;
    both are supported.
    """
    def __init__(self, raw):
        self.username   = raw.get("username") or raw.get("name") or ""
        self.email      = raw.get("email") or ""
        self.first_name = raw.get("first_name") or raw.get("firstName") or ""
        self.last_name  = raw.get("last_name")  or raw.get("lastName")  or ""

    def is_valid(self):
        """At minimum, username and email are required to create an account in Keycloak."""
        return bool(self.username and self.email)


def _get_apic_user(username):
    """
    Fetches a user as JSON from the APIC Local Registry and returns an _ApicUser object.
    username comes from the CSV — 03_export_consumer_orgs.py writes user["name"]
    (case-preserved) to the CSV, so this value is passed directly to the APIC CLI.
    CLI errors sometimes appear in stderr and sometimes in stdout; both are checked.
    Returns None on error.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("LOCAL_REGISTRY"),
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return _ApicUser(json.loads(res.stdout))
    except subprocess.CalledProcessError as e:
        print("--> [ERROR] Failed to retrieve user from APIC!")
        print(f"--> [DETAIL] {e.stderr.strip() or e.stdout.strip()}")
        return None
    except json.JSONDecodeError:
        print("--> [ERROR] APIC response is not valid JSON!")
        return None


def _get_kc_admin_token():
    """
    Obtains a token from the Keycloak master realm via admin-cli.
    This token is used in all requests to the Keycloak Admin REST API.
    """
    kc_url = _get_env("KEYCLOAK_URL")
    url = f"{kc_url}/realms/master/protocol/openid-connect/token"
    data = urllib.parse.urlencode({
        "username":   _get_env("KEYCLOAK_ADMIN_USER"),
        "password":   _get_env("KEYCLOAK_ADMIN_PASSWORD"),
        "grant_type": "password",
        "client_id":  "admin-cli",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            return json.loads(resp.read().decode()).get("access_token")
    except Exception as e:
        print(f"--> [ERROR] Failed to obtain Keycloak admin token: {e}")
        return None


def _create_kc_user_api(token, user_obj):
    """
    Adds the user to the target realm (KEYCLOAK_REALM_NAME) via the Keycloak Admin API.
    Generates a 16-character cryptographically random temporary password and writes it to credentials.
    HTTP 409: warns if the user already exists (returns None; step_01 is considered failed).
    Returns the temporary password string on success, None on failure.
    """
    target_realm = _get_env("KEYCLOAK_REALM_NAME", "apic-demo")
    url = f"{_get_env('KEYCLOAK_URL')}/admin/realms/{target_realm}/users"

    alphabet  = string.ascii_letters + string.digits
    temp_pass = "".join(secrets.choice(alphabet) for _ in range(16))

    payload = {
        "username":      user_obj.username,
        "enabled":       True,
        "emailVerified": True,
        "email":         user_obj.email,
        "firstName":     user_obj.first_name,
        "lastName":      user_obj.last_name,
        # If temporary=True, the user is forced to change their password on first login
        "credentials": [{"type": "password", "value": temp_pass, "temporary": False}],
    }

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            if resp.status == 201:
                print(f"--> [SUCCESS] '{user_obj.username}' created in Keycloak.")
                return temp_pass
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print(f"--> [WARNING] '{user_obj.username}' already exists in Keycloak (HTTP 409).")
            print(f"    Solution: Delete the user from the Keycloak admin panel and try again.")
        else:
            print(f"--> [ERROR] Failed to create user (HTTP {e.code}): {e.read().decode()}")
    return None


def _save_temp_password(temp_pass):
    """
    Writes the temporary password to migration_env.sh; step_03 reads this value
    to log in to the APIC consumer token endpoint.
    Overwrites KC_TEMP_PASSWORD if it already exists in the file, otherwise appends it.
    Cleared by _clear_temp_password() after successful JIT provision.
    """
    key  = "KC_TEMP_PASSWORD"
    line = f'export {key}="{temp_pass}"\n'
    with open(ENV_FILE, "r") as f:
        lines = f.readlines()
    updated = False
    for i, l in enumerate(lines):
        if l.startswith(f"export {key}="):
            lines[i] = line
            updated   = True
            break
    if not updated:
        lines.append(line)
    with open(ENV_FILE, "w") as f:
        f.writelines(lines)
    print(f"--> [INFO] Temporary password saved to migration_env.sh (will be used by step_03).")


def step_01_create_kc_user(username, consumer_org=""):
    """
    Migration Step 1/4 — Create User in Keycloak

    1. Skips idempotently if kc_user_created=true in CSV.
    2. Fetches user details from the APIC Local Registry.
    3. Adds the user to the target realm via the Keycloak Admin API.
    4. Writes the generated temporary password to migration_env.sh (read by step_03).
    5. Marks kc_user_created=true in the CSV.

    Returns: True on success, False on error/skip.

    Common Errors:
      HTTP 409 → Same username already exists in Keycloak, delete it first.
      Could not obtain token → Verify Keycloak admin credentials.
    """
    load_env()
    csv_row = get_user(username)
    if csv_row and csv_row.get("kc_user_created", "false").lower() == "true":
        print(f"--> [INFO] '{username}' already exists in Keycloak (kc_user_created=true). Skipping.")
        return True

    print(f"\n--> [1/3] Reading user '{username}' from APIC...")
    user_obj = _get_apic_user(username)
    if not user_obj:
        return False

    if not user_obj.is_valid():
        print(f"--> [ERROR] User's email or username is empty!")
        print(f"    Both are required to create an account in Keycloak.")
        return False

    if not csv_row:
        add_user(username, consumer_org, user_obj.email)

    print("--> [2/3] Obtaining Keycloak admin token...")
    token = _get_kc_admin_token()
    if not token:
        return False

    print(f"--> [3/3] Writing '{username}' to Keycloak...")
    temp_pass = _create_kc_user_api(token, user_obj)
    if not temp_pass:
        return False

    update_flag(username, "kc_user_created", True)
    _save_temp_password(temp_pass)
    print("==================================================")
    return True


# ==============================================================================
# STEP 2 — Park APIC Email
# ==============================================================================

def _get_current_user_data(username):
    """
    Fetches the current details of a user from the APIC Local Registry (JSON).
    This is where we learn whether the email contains -old@ and what the name fields are.
    Returns None on error.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("LOCAL_REGISTRY"),
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout)
    except subprocess.CalledProcessError as e:
        print(f"--> [ERROR] Failed to read APIC user: {e.stderr.strip() or e.stdout.strip()}")
        return None
    except json.JSONDecodeError:
        print("--> [ERROR] APIC response is not valid JSON!")
        return None


def _update_user_email(username, current_data, new_email):
    """
    Updates the user's email in the APIC Local Registry.
    Current first_name and last_name values are preserved in the request;
    APIC may reset them if sent as empty.
    Returns True on success, False on CLI error.
    """
    first_name = current_data.get("first_name") or current_data.get("firstName") or ""
    last_name  = current_data.get("last_name")  or current_data.get("lastName")  or ""
    yaml_content = f"""email: {new_email}
first_name: {first_name}
last_name: {last_name}
title: {username}
"""
    cmd = [
        "apic", "users:update", username, "-",
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("LOCAL_REGISTRY"),
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print("--> [ERROR] APIC update was rejected!")
        print(f"--> [DETAIL] {e.stderr.strip() or e.stdout.strip()}")
        return False


def step_02_park_apic_email(username):
    """
    Migration Step 2/4 — Park APIC Email

    Converts the email in the APIC Local Registry to <address>-old@<domain> format.
    This prevents a collision between the Keycloak record and the APIC record
    sharing the same email during JIT provisioning.

    1. Skips if apic_email_parked=true in CSV (idempotent).
    2. Fetches the current email.
    3. Updates the email in -old@ format.
    4. Reads back from APIC to verify the change.
    5. Writes apic_email_parked=true, source_email=<parked address> to CSV.

    Returns: True on success, False on error/skip.

    Common Errors:
      APIC update rejected → User may be from an external source (LDAP).
      Verification failed → APIC validation constraint, consult an administrator.
    """
    load_env()
    csv_row = get_user(username)
    if not csv_row:
        print(f"--> [ERROR] '{username}' not found in CSV. Please run 03_export_consumer_orgs.py first.")
        return False

    if csv_row.get("apic_email_parked", "false").lower() == "true":
        print(f"--> [INFO] '{username}' email is already parked. Skipping.")
        return True

    print(f"\n--> [1/3] Reading APIC information for user '{username}'...")
    current_data = _get_current_user_data(username)
    if not current_data:
        return False

    old_email = current_data.get("email", "")
    if not old_email or "@" not in old_email:
        print(f"--> [ERROR] No valid email address found (current: '{old_email}').")
        return False

    if old_email.split("@")[0].endswith("-old"):
        print(f"--> [INFO] Email is already in parked format ({old_email}). Will not be appended again.")
        new_email = old_email
    else:
        parts     = old_email.split("@")
        new_email = f"{parts[0]}-old@{parts[1]}"

    print(f"--> Current email in APIC  : {old_email}")
    print(f"--> Email to be parked     : {new_email}")

    print("\n--> [2/3] Updating APIC Local Registry...")
    if not _update_user_email(username, current_data, new_email):
        return False

    print("--> [3/3] Verifying change by reading back from APIC...")
    verify = _get_current_user_data(username)
    if verify and verify.get("email") == new_email:
        print("--> [SUCCESS] Email change verified!")
        update_flag(username, "apic_email_parked", True)
        update_source_email(username, new_email)
    else:
        print("--> [WARNING] Update command ran but APIC verification failed.")
        print("    Please verify manually on APIC.")
        return False

    print("==================================================")
    return True


# ==============================================================================
# STEP 3 — APIC JIT Provision (JWT-Bearer Token Exchange)
# ==============================================================================

def _get_kc_access_token(username, password):
    """
    Obtains an Access Token from Keycloak using the user's temporary password.
    This token is then presented to APIC as a JWT-Bearer assertion in the next step.
    KEYCLOAK_CLIENT_ID: the OIDC client registered in Keycloak by APIC.
    Returns the access_token string on success, None on error.
    """
    target_realm = _get_env("KEYCLOAK_REALM_NAME", "apic-demo")
    kc_client_id = _get_env("KEYCLOAK_CLIENT_ID", "apic-client")
    kc_client_secret = _get_env("KEYCLOAK_CLIENT_SECRET", "")

    url = f"{_get_env('KEYCLOAK_URL')}/realms/{target_realm}/protocol/openid-connect/token"

    body_params = {
        "grant_type": "password",
        "client_id":  kc_client_id,
        "client_secret": kc_client_secret,
        "username":   username,
        "password":   password,
        "scope":      "openid email profile",
    }
    data = urllib.parse.urlencode(body_params).encode("utf-8")
    try:
        _, body = _http(url, data=data)
        token = body.get("access_token")
        if not token:
            print(f"--> [ERROR] Failed to obtain Access Token: {body}")
        return token
    except urllib.error.HTTPError as e:
        print(f"--> [ERROR] Keycloak token request failed (HTTP {e.code}): {e.read().decode()}")
        return None


def _trigger_apic_jwt_bearer(access_token):
    """
    Presents the Access Token obtained from Keycloak to APIC as a JWT-Bearer grant.

    Flow:
      1. The access_token from Keycloak is sent to APIC in the 'assertion' field.
      2. APIC validates the token against Keycloak.
      3. APIC opens a shadow user record in its own database (JIT provision).
         For this to work, the "Auto onboard" feature must be enabled
         in the Keycloak registry configured in APIC.

    Realm format: consumer:<prov_org>:<catalog>/<keycloak_registry_name>
    Returns True on success, False on error.
    """
    apic_server       = _get_env("APIC_SERVER")
    prov_org          = _get_env("PROV_ORG")
    catalog           = _get_env("CATALOG", "sandbox")
    kc_registry       = _get_env("KEYCLOAK_REGISTRY_NAME", "keycluk")
    creds_file        = _get_env("APIC_CLIENT_CREDS", "")

    url                = f"{apic_server}/api/token"
    apic_client_id     = ""
    apic_client_secret = ""

    if creds_file and os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                creds = json.load(f)
            # credentials.json structure: {"consumer_toolkit": {"endpoint": ..., "client_id": ...}}
            toolkit = creds.get("consumer_toolkit") or creds.get("toolkit", {})
            if "endpoint" in toolkit:
                url = f"{toolkit['endpoint']}/token"
            apic_client_id     = toolkit.get("client_id")     or creds.get("client_id", "")
            apic_client_secret = toolkit.get("client_secret") or creds.get("client_secret", "")
        except Exception as e:
            print(f"--> [WARNING] Could not read credentials.json: {e}")

    if not apic_client_id or not apic_client_secret:
        print(f"--> [ERROR] APIC client_id or client_secret not found!")
        print(f"    Check the credentials.json file and the APIC_CLIENT_CREDS path.")
        return False

    realm_str = f"consumer:{prov_org}:{catalog}/{kc_registry}"
    payload = json.dumps({
        "realm":         realm_str,
        "grant_type":    "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion":     access_token,
        "client_id":     apic_client_id,
        "client_secret": apic_client_secret,
    }).encode("utf-8")

    try:
        status, body = _http(
            url,
            data=payload,
            headers={
                "Content-Type":           "application/json",
                "Accept":                 "application/json",
                # X-IBM-Consumer-Context: tells APIC which org/catalog context to use
                "X-IBM-Consumer-Context": f"{prov_org}.{catalog}",
            },
        )
        if status == 200:
            print("--> [SUCCESS] APIC JIT-provisioning (Auto Onboard) completed.")
            return True
        else:
            print(f"--> [ERROR] APIC login failed (HTTP {status}): {body}")
            return False
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        if e.code == 400 and "already" in err_body.lower():
            print("--> [INFO] User already exists on APIC.")
            return True
        print(f"--> [ERROR] APIC login rejected (HTTP {e.code}): {err_body}")
        return False
    except Exception as e:
        print(f"--> [ERROR] Unexpected error during request: {e}")
        return False


def _clear_temp_password():
    """
    Removes the KC_TEMP_PASSWORD line from migration_env.sh.
    Must be called immediately after JIT provision completes; the temporary
    password does not linger on disk unnecessarily.
    If cleanup fails, migration does not stop.
    """
    key = "KC_TEMP_PASSWORD"
    try:
        with open(ENV_FILE, "r") as f:
            lines = f.readlines()
        filtered = [l for l in lines if not l.startswith(f"export {key}=")]
        if len(filtered) != len(lines):
            with open(ENV_FILE, "w") as f:
                f.writelines(filtered)
            print(f"--> [INFO] Temporary password cleared from migration_env.sh.")
    except Exception:
        pass


def step_03_jit_provision(username):
    """
    Migration Step 3/4 — APIC JIT Provision (JWT-Bearer Token Exchange)

    Registers the Keycloak-created user with APIC (opens a shadow user record).

    1. Skips if migrated/apic_jit_done=true in CSV (idempotent).
    2. Freshly reads KC_TEMP_PASSWORD from migration_env.sh
       (step_01 wrote it to disk during this run; a fresh read is mandatory).
    3. Obtains an Access Token from Keycloak via _get_kc_access_token().
    4. Presents the token to APIC as a JWT-Bearer via _trigger_apic_jwt_bearer().
    5. Writes apic_jit_done=true, migrated=true to CSV.
    6. Clears the temporary password via _clear_temp_password().

    Returns: True on success, False on error.

    Common Errors:
      KC_TEMP_PASSWORD not found → step_01 failed; set password manually from Keycloak.
      KC token 401 → client_id is wrong or user does not exist in Keycloak.
      APIC 401 → APIC client credentials are invalid.
      APIC 400 → Realm string format is wrong or Auto Onboard is disabled.
    """
    load_env()  # KC_TEMP_PASSWORD was just written by step_01; a fresh read is mandatory
    csv_row = get_user(username)
    if not csv_row:
        print(f"--> [ERROR] '{username}' not found in CSV.")
        return False

    if csv_row.get("migrated", "false").lower() == "true":
        print(f"--> [INFO] '{username}' already migrated. Skipping.")
        return True

    if csv_row.get("apic_jit_done", "false").lower() == "true":
        print(f"--> [INFO] '{username}' already provisioned in APIC. Skipping.")
        return True

    kc_temp_password = os.environ.get("KC_TEMP_PASSWORD", "")
    if not kc_temp_password:
        print("--> [ERROR] KC_TEMP_PASSWORD not found!")
        print("    step_01_create_kc_user must have completed successfully.")
        print("    If the user exists in Keycloak, you can set a password from the admin panel")
        print("    and add 'export KC_TEMP_PASSWORD=\"password\"' to migration_env.sh.")
        return False

    print(f"\n--> [1/2] Obtaining Access Token from Keycloak for '{username}'...")
    access_token = _get_kc_access_token(username, kc_temp_password)
    if not access_token:
        return False

    print(f"--> [2/2] Sending JWT-Bearer token to APIC (JIT Provision)...")
    success = _trigger_apic_jwt_bearer(access_token)
    if not success:
        return False

    update_flag(username, "apic_jit_done", True)

    _clear_temp_password()

    print("==================================================")
    print(f"[COMPLETE] '{username}' has been logged into APIC via Keycloak.")
    print("==================================================")
    return True

# ==============================================================================
# STEP 4 — Transfer Consumer Org Ownership
# ==============================================================================

def _get_target_email_for_org(username):
    """
    Reads the current email of the user from the APIC Local Registry.
    After step_02, the email may be in -old@ format; this function
    strips the -old@ suffix to return the real email address in Keycloak.
    This matching is critical because the shadow user was created in Keycloak
    using the original email address.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("LOCAL_REGISTRY"),
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        email = json.loads(res.stdout).get("email", "")
        if "-old@" in email:
            email = email.replace("-old@", "@")
        return email
    except Exception as e:
        print(f"--> [ERROR] Failed to retrieve email from APIC: {e}")
        return None


def _get_shadow_user_url(consumer_org, expected_email):
    """
    Searches the Keycloak registry in APIC for a shadow user by email address
    and returns their APIC URL. This URL is required for adding a member and
    transferring ownership.
    Returns None if not found.
    """
    cmd = [
        "apic", "users:list",
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("KEYCLOAK_REGISTRY_NAME", "keycluk"),
        "--format", "json", "--output", "-",
    ]
    try:
        res  = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        items = data.get("results", data if isinstance(data, list) else [data])
        for u in items:
            if u.get("email") == expected_email:
                return u.get("url")
        return None
    except Exception as e:
        print(f"--> [ERROR] Failed to read Keycloak registry list: {e}")
        return None


def _add_kc_user_as_member(consumer_org, username, user_url):
    """
    Adds the shadow user as a member of the consumer org.
    The user must be a member of the org before ownership can be transferred.
    Behaves idempotently if already a member (already exists check).
    Returns True on success, False on error.
    """
    yaml_content = f"""name: "{username}-kc"
title: "{username}"
user:
  url: "{user_url}"
"""
    cmd = [
        "apic", "members:create", "--scope", "consumer-org", "-",
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "-c", _get_env("CATALOG"),
        "--consumer-org", consumer_org,
    ]
    try:
        res = subprocess.run(cmd, input=yaml_content, capture_output=True, text=True)
        if res.returncode == 0:
            print(f"--> [SUCCESS] Shadow user added as member to org '{consumer_org}'.")
            return True
        if "already exists" in (res.stderr + res.stdout).lower():
            print("--> [INFO] Shadow user is already a member of this org.")
            return True
        print(f"--> [ERROR] Failed to add member:\n{res.stderr.strip() or res.stdout.strip()}")
        return False
    except Exception as e:
        print(f"--> [ERROR] Unexpected error while adding member: {e}")
        return False


def _get_member_url(consumer_org, expected_email):
    """
    Returns the member URL of the shadow user from the consumer org member list.
    The transfer-owner command expects a member URL, not a user URL.
    Returns None if not found.
    """
    cmd = [
        "apic", "members:list", "--scope", "consumer-org",
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "-c", _get_env("CATALOG"),
        "--consumer-org", consumer_org,
        "--format", "json", "--output", "-",
    ]
    try:
        res  = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        items = data.get("results", data if isinstance(data, list) else [data])
        for m in items:
            if m.get("user", {}).get("email") == expected_email:
                return m.get("url")
        return None
    except Exception as e:
        print(f"--> [ERROR] Failed to read member list: {e}")
        return None


def _transfer_ownership(consumer_org, member_url):
    """
    Transfers ownership using the consumer-orgs:transfer-owner --cascade command.
    --cascade: also updates the owner of App and Subscription records
    under the Consumer Org to the new user.
    WARNING: This is an irreversible operation; the rollback script does not reverse this step.
    Returns True on success, False on CLI error.
    """
    yaml_content = f"new_owner_member_url: {member_url}\n"
    cmd = [
        "apic", "consumer-orgs:transfer-owner", consumer_org, "-",
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "-c", _get_env("CATALOG"),
        "--cascade",
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        print(f"--> [SUCCESS] '{consumer_org}' ownership transferred to Keycloak profile.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"--> [ERROR] Ownership transfer failed:\n{e.stderr.strip() or e.stdout.strip()}")
        return False


def step_04_transfer_org(username):
    """
    Migration Step 4/4 — Transfer Consumer Org Ownership

    After the shadow user is opened in APIC via JIT provision,
    the consumer org ownership moves from the Local Registry user to the Keycloak profile.

    1. Skips if migrated=true in CSV (idempotent).
    2. Reads the consumer_org name from CSV.
    3. Derives the real email from the APIC Local Registry email (stripping -old@).
    4. Finds the shadow user URL in the Keycloak registry.
    5. Adds the shadow user as a member of the consumer org.
    6. Transfers ownership via consumer-orgs:transfer-owner --cascade.
    7. Writes org_owner_xfrd=true, migrated=true to CSV.

    Returns: True on success, False on error.

    Common Errors:
      Shadow user not found → step_03 has not completed.
      Ownership transfer failed → --cascade may not be supported in some versions.
    """
    load_env()
    csv_row = get_user(username)
    if not csv_row:
        print(f"--> [ERROR] '{username}' not found in CSV.")
        return False

    if csv_row.get("migrated", "false").lower() == "true":
        print(f"--> [INFO] '{username}' already migrated. Skipping.")
        return True

    consumer_org = csv_row.get("consumer_org", "")
    if not consumer_org:
        print(f"--> [ERROR] No consumer_org information in CSV for '{username}'.")
        return False

    print(f"\n--> [1/4] Calculating target email for '{username}'...")
    expected_email = _get_target_email_for_org(username)
    if not expected_email:
        return False
    print(f"--> [INFO] Email in Keycloak: {expected_email}")

    print(f"--> [2/4] Searching for shadow user in APIC Keycloak registry...")
    user_url = _get_shadow_user_url(consumer_org, expected_email)
    if not user_url:
        print(f"--> [ERROR] Shadow user not found for '{expected_email}'.")
        print(f"    Verify that step_03_jit_provision completed successfully.")
        return False

    print(f"--> [3/4] Adding shadow user as member to org '{consumer_org}'...")
    if not _add_kc_user_as_member(consumer_org, username, user_url):
        return False

    print(f"--> [4/4] Transferring consumer org ownership (--cascade)...")
    member_url = _get_member_url(consumer_org, expected_email)
    if not member_url:
        print("--> [ERROR] Could not retrieve Member URL. Please check the member list manually.")
        return False

    if not _transfer_ownership(consumer_org, member_url):
        return False

    update_flag(username, "org_owner_xfrd", True)
    mark_migrated(username)

    print("==================================================")
    print(f"[COMPLETE] '{consumer_org}' → owner is now the Keycloak profile.")
    print("==================================================")
    return True


# ==============================================================================
# STEP 5 — Keycloak Password Setup Email
# ==============================================================================

def _get_kc_user_id(token, username):
    """
    Returns the UUID of the user from the Keycloak Admin API.
    The execute-actions-email endpoint expects a UUID, not a username.
    Returns None if not found.
    """
    target_realm = _get_env("KEYCLOAK_REALM_NAME", "apic-demo")
    url = f"{_get_env('KEYCLOAK_URL')}/admin/realms/{target_realm}/users?username={username}&exact=true"
    try:
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            users = json.loads(resp.read().decode())
            if users:
                return users[0]["id"]
            print(f"--> [ERROR] '{username}' not found in Keycloak!")
            return None
    except Exception as e:
        print(f"--> [ERROR] User UUID query failed: {e}")
        return None


def _send_update_password_email(token, user_id, username):
    """
    Sends an UPDATE_PASSWORD action email via the Keycloak Admin API.
    The user receives an email containing a link to set their password.
    HTTP 200 or 204 is considered success. Returns False on error.
    """
    target_realm = _get_env("KEYCLOAK_REALM_NAME", "apic-demo")
    url = f"{_get_env('KEYCLOAK_URL')}/admin/realms/{target_realm}/users/{user_id}/execute-actions-email"
    payload = json.dumps(["UPDATE_PASSWORD"]).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, method="PUT")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            if resp.status in (200, 204):
                print(f"--> [SUCCESS] Password setup email sent for '{username}'.")
                return True
        return False
    except urllib.error.HTTPError as e:
        print(f"--> [ERROR] Failed to send email (HTTP {e.code}): {e.read().decode()}")
        return False
    except Exception as e:
        print(f"--> [ERROR] Unexpected error: {e}")
        return False


def step_05_send_password_email(username):
    """
    Migration Step 5/5 — Keycloak Password Setup Email

    After migration is complete (migrated=true), sends the user
    an UPDATE_PASSWORD email via Keycloak.

    1. Skips if migrated=true is not set in CSV — migration is not yet complete.
    2. Obtains a Keycloak admin token.
    3. Queries the user's Keycloak UUID.
    4. Triggers UPDATE_PASSWORD via the execute-actions-email endpoint.

    Returns: True on success, False on error.

    Common Errors:
      migrated=false → previous steps have not completed; finish migration first.
      UUID not found → user does not exist in Keycloak; check step_01.
      Email could not be sent → Keycloak SMTP settings may be missing or incorrect.
    """
    load_env()
    csv_row = get_user(username)
    if not csv_row:
        print(f"--> [ERROR] '{username}' not found in CSV.")
        return False

    if csv_row.get("migrated", "false").lower() != "true":
        print(f"--> [WARNING] '{username}' has not been migrated yet (migrated=false). Email skipped.")
        return False

    print(f"\n--> [1/3] Obtaining Keycloak admin token...")
    token = _get_kc_admin_token()
    if not token:
        return False

    print(f"--> [2/3] Querying Keycloak UUID for '{username}'...")
    user_id = _get_kc_user_id(token, username)
    if not user_id:
        return False
    print(f"--> [INFO] UUID: {user_id}")

    print(f"--> [3/3] Triggering UPDATE_PASSWORD email...")
    if not _send_update_password_email(token, user_id, username):
        return False

    print("==================================================")
    print(f"[COMPLETE] Password setup email sent for '{username}'.")
    print("==================================================")
    return True


# ==============================================================================
# Direct execution for single-user testing
# ==============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python migration_steps.py <username> [consumer_org]")
        sys.exit(1)

    _username = sys.argv[1]
    _org      = sys.argv[2] if len(sys.argv) > 2 else ""

    print(f"\n{'='*60}")
    print(f"  SINGLE USER MIGRATION: {_username}")
    print(f"{'='*60}")

    load_env()
    ok = (
        step_01_create_kc_user(_username, _org)
        and step_02_park_apic_email(_username)
        and step_03_jit_provision(_username)
        and step_04_transfer_org(_username)
        and step_05_send_password_email(_username)
    )
    sys.exit(0 if ok else 1)
