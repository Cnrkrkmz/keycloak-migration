#!/usr/bin/env python3.11
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error

from migration_state import get_user, update_flag, mark_migrated

# ==============================================================================
# step_03_jit_provision.py — APIC'te shadow user JIT provision (OIDC login)
# Çalıştıran: 04_run_migration.py (adım 3/4)
#
# HOW IT WORKS:
#   1. step_01_create_kc_user.py Keycloak'ta kullanıcıyı yaratırken ürettiği
#      geçici şifreyi migration_env.sh'a (KC_TEMP_PASSWORD) kaydeder.
#   2. Bu script o şifreyi okuyarak kullanıcı adına gerçek bir Keycloak OIDC
#      token'ı alır.
#   3. Token'ı APIC'in /api/token endpoint'ine POST eder.
#      → APIC token'ı doğrular, 'sub' claim'ini çözer ve kendi veritabanında
#        gölge kullanıcıyı otomatik olarak JIT-provision eder.
# ==============================================================================

ENV_FILE = "migration_env.sh"


def load_env():
    """Loads environment variables from migration_env.sh."""
    if not os.path.exists(ENV_FILE):
        print(f"--> [HATA] '{ENV_FILE}' bulunamadı!")
        sys.exit(1)
    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                key_value = line[7:].split("=", 1)
                if len(key_value) == 2:
                    os.environ[key_value[0]] = key_value[1].strip('"\'')


load_env()

APIC_SERVER        = os.environ.get("APIC_SERVER")
PROV_ORG           = os.environ.get("PROV_ORG")
KEYCLOAK_REGISTRY  = os.environ.get("KEYCLOAK_REGISTRY_NAME", "keycluk")
KEYCLOAK_URL       = os.environ.get("KEYCLOAK_URL")
KEYCLOAK_ADMIN_USER     = os.environ.get("KEYCLOAK_ADMIN_USER")
KEYCLOAK_ADMIN_PASSWORD = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
TARGET_REALM       = os.environ.get("KEYCLOAK_REALM_NAME", "apic-demo")

# The Keycloak client that APIC uses for OIDC — must allow password grant & have
# "Direct Access Grants" enabled in KC.  Usually the same client registered in the
# APIC user-registry.  Override via env var if needed.
KEYCLOAK_CLIENT_ID     = os.environ.get("KEYCLOAK_CLIENT_ID", "apic-client")
KEYCLOAK_CLIENT_SECRET = os.environ.get("KEYCLOAK_CLIENT_SECRET", "")

# 03_migrate_user_to_keycloak.py tarafından kaydedilen geçici şifre
KC_TEMP_PASSWORD = os.environ.get("KC_TEMP_PASSWORD", "")

if len(sys.argv) > 1:
    TARGET_USERNAME = sys.argv[1]
else:
    TARGET_USERNAME = input("APIC'te gölge profili açılacak Keycloak Kullanıcı Adı: ").strip()


# ------------------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------------------

def _http(url, *, data=None, method=None, headers=None):
    """
    Minimal wrapper around urllib.request.  Returns (status_code, parsed_json_or_none).
    Raises urllib.error.HTTPError on non-2xx so callers can inspect .code.
    """
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode()
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body


# ------------------------------------------------------------------------------
# STEP 1 — Obtain a real Keycloak user token (OIDC password grant)
# ------------------------------------------------------------------------------

def get_kc_user_token(username, password):
    """
    Authenticates *username* against Keycloak using the password grant and returns
    the access_token.  This is the token APIC will receive and introspect.
    """
    url = f"{KEYCLOAK_URL}/realms/{TARGET_REALM}/protocol/openid-connect/token"
    body_params = {
        "grant_type": "password",
        "client_id":  KEYCLOAK_CLIENT_ID,
        "username":   username,
        "password":   password,
    }
    if KEYCLOAK_CLIENT_SECRET:
        body_params["client_secret"] = KEYCLOAK_CLIENT_SECRET

    data = urllib.parse.urlencode(body_params).encode("utf-8")
    try:
        _, body = _http(url, data=data)
        token = body.get("access_token")
        if not token:
            print(f"--> [HATA] Kullanıcı tokeni alınamadı. Yanıt: {body}")
        return token
    except urllib.error.HTTPError as e:
        print(f"--> [HATA] Kullanıcı token isteği başarısız (HTTP {e.code}): {e.read().decode()}")
        return None


# ------------------------------------------------------------------------------
# STEP 2 — Exchange the KC user token with APIC  →  triggers JIT provisioning
# ------------------------------------------------------------------------------

def trigger_apic_oidc_login(kc_user_token):
    """
    POSTs the Keycloak user token to APIC's /api/token endpoint.
    APIC validates it via OIDC, resolves the 'sub' claim to a username, and
    auto-creates (JIT-provisions) the shadow user in its own database.

    The request body follows the APIC Platform API spec:
        POST /api/token
        { "realm": "provider/<registry-name>",
          "access_token": "<keycloak-jwt>",
          "client_id": "...", "client_secret": "...",
          "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer" }
    """
    url = f"{APIC_SERVER}/api/token"

    # Load APIC client credentials from the file referenced in the env (if set).
    # The credentials.json must contain 'client_id' and 'client_secret'.
    creds_file = os.environ.get("APIC_CLIENT_CREDS", "")
    apic_client_id     = ""
    apic_client_secret = ""
    if creds_file and os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                creds = json.load(f)
            apic_client_id     = creds.get("client_id", "")
            apic_client_secret = creds.get("client_secret", "")
        except Exception:
            pass  # non-fatal; APIC may not require them for token exchange

    payload = json.dumps({
        "realm":        f"provider/{KEYCLOAK_REGISTRY}",
        "access_token": kc_user_token,
        "grant_type":   "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id":    apic_client_id,
        "client_secret": apic_client_secret,
    }).encode("utf-8")

    try:
        status, body = _http(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        if status == 200:
            print("--> [BAŞARILI] APIC OIDC login tetiklendi. Gölge kullanıcı JIT-provision edildi.")
            return True
        else:
            print(f"--> [HATA] APIC token endpoint beklenmedik yanıt döndü (HTTP {status})")
            print(f"--> [DETAY] {body}")
            return False
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        # 400 with "already exists" or similar → user was already provisioned, treat as success
        if e.code == 400 and "already" in err_body.lower():
            print("--> [BİLGİ] Kullanıcı APIC üzerinde zaten mevcut.")
            return True
        print(f"--> [HATA] APIC token endpoint başarısız (HTTP {e.code}): {err_body}")
        return False
    except Exception as e:
        print(f"--> [HATA] APIC OIDC login isteği sırasında hata: {e}")
        return False


# ------------------------------------------------------------------------------
# CLEANUP
# ------------------------------------------------------------------------------

def clear_temp_password():
    """
    migration_env.sh içindeki KC_TEMP_PASSWORD satırını siler.
    JIT provision başarılı olduğunda çağrılır — şifre dosyada kalıcı iz bırakmaz.
    """
    key = "KC_TEMP_PASSWORD"
    try:
        with open(ENV_FILE, "r") as f:
            lines = f.readlines()

        filtered = [l for l in lines if not l.startswith(f"export {key}=")]

        if len(filtered) == len(lines):
            return  # zaten yoktu, yapacak bir şey yok

        with open(ENV_FILE, "w") as f:
            f.writelines(filtered)

        print(f"--> [BİLGİ] Geçici şifre '{ENV_FILE}' dosyasından temizlendi.")
    except Exception as e:
        print(f"--> [UYARI] Geçici şifre temizlenemedi: {e}")


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

def main():
    csv_row = get_user(TARGET_USERNAME)
    if not csv_row:
        print(f"--> [HATA] '{TARGET_USERNAME}' CSV'de bulunamadı. Önce 02 ve 03 scriptlerini çalıştırın.")
        sys.exit(1)

    if csv_row.get("migrated", "false").lower() == "true":
        print(f"--> [BİLGİ] '{TARGET_USERNAME}' zaten migrate edilmiş (migrated=true). Atlanıyor.")
        print("==================================================")
        return

    if csv_row.get("apic_jit_done", "false").lower() == "true":
        print(f"--> [BİLGİ] '{TARGET_USERNAME}' APIC'te zaten JIT provision edilmiş (apic_jit_done=true). Atlanıyor.")
        print("==================================================")
        return

    if not KC_TEMP_PASSWORD:
        print("--> [HATA] 'KC_TEMP_PASSWORD' bulunamadı!")
        print("--> Lütfen önce 03_migrate_user_to_keycloak.py scriptini çalıştırın.")
        sys.exit(1)

    print(f"\n--> [1/2] '{TARGET_USERNAME}' kullanıcısı adına Keycloak'tan OIDC token alınıyor...")
    kc_user_token = get_kc_user_token(TARGET_USERNAME, KC_TEMP_PASSWORD)
    if not kc_user_token:
        sys.exit(1)

    print(f"--> [2/2] APIC'e OIDC login tetikleniyor (JIT provision)...")
    success = trigger_apic_oidc_login(kc_user_token)

    if not success:
        sys.exit(1)

    update_flag(TARGET_USERNAME, "apic_jit_done", True)
    mark_migrated(TARGET_USERNAME)
    clear_temp_password()

    print("==================================================")
    print(f"[TAMAMLANDI] '{TARGET_USERNAME}' APIC'e OIDC ile login edildi.")
    print("Geçici şifre 'temporary=True' — kullanıcı ilk girişte şifresini değiştirmek zorundadır.")
    print("==================================================")


if __name__ == "__main__":
    main()
