#!/usr/bin/env python3.11
import json
import os
import sys
import ssl
import urllib.request
import urllib.parse
import urllib.error

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# ==============================================================================
# KEYCLOAK UPDATE_PASSWORD MAİL GÖNDERİCİ
# ==============================================================================
# Çalıştırma sırası: 07 (migration tamamlandıktan sonra)
#
# Keycloak'ta belirtilen kullanıcıya "Şifreni belirle" (UPDATE_PASSWORD)
# e-postası gönderir.
#
# Kullanım:
#   python 07_send_keycloak_mail.py <username>
#   python 07_send_keycloak_mail.py          # interaktif
# ==============================================================================

ENV_FILE = "migration_env.sh"


def load_env():
    if not os.path.exists(ENV_FILE):
        print(f"--> [HATA] '{ENV_FILE}' bulunamadı!")
        sys.exit(1)
    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                kv = line[7:].split("=", 1)
                if len(kv) == 2:
                    os.environ[kv[0]] = kv[1].strip('"\'')


load_env()

KEYCLOAK_URL            = os.environ.get("KEYCLOAK_URL")
KEYCLOAK_ADMIN_USER     = os.environ.get("KEYCLOAK_ADMIN_USER")
KEYCLOAK_ADMIN_PASSWORD = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
TARGET_REALM            = os.environ.get("KEYCLOAK_REALM_NAME", "apic-demo")

if len(sys.argv) > 1:
    TARGET_USERNAME = sys.argv[1]
else:
    print("\n==================================================")
    realm_input = input(f"Hedef Realm [{TARGET_REALM}]: ").strip()
    if realm_input:
        TARGET_REALM = realm_input
    TARGET_USERNAME = input("Kullanıcı Adı (Username): ").strip()
    if not TARGET_USERNAME:
        print("--> [HATA] Kullanıcı adı zorunludur!")
        sys.exit(1)


# ------------------------------------------------------------------------------
# ADIMLAR
# ------------------------------------------------------------------------------

def get_admin_token():
    """Keycloak master realm'den admin access token alır."""
    url = f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token"
    data = urllib.parse.urlencode({
        "username":   KEYCLOAK_ADMIN_USER,
        "password":   KEYCLOAK_ADMIN_PASSWORD,
        "grant_type": "password",
        "client_id":  "admin-cli",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            return json.loads(resp.read().decode()).get("access_token")
    except Exception as e:
        print(f"--> [HATA] Admin token alınamadı: {e}")
        return None


def get_user_id(token, username):
    """Kullanıcının Keycloak UUID'sini döndürür."""
    url = f"{KEYCLOAK_URL}/admin/realms/{TARGET_REALM}/users?username={username}&exact=true"
    try:
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            users = json.loads(resp.read().decode())
            if users:
                return users[0]["id"]
            print(f"--> [HATA] '{username}' Keycloak'ta bulunamadı!")
            return None
    except Exception as e:
        print(f"--> [HATA] Kullanıcı sorgusu başarısız: {e}")
        return None


def send_update_password_email(token, user_id):
    """
    UPDATE_PASSWORD action'ını tetikler — Keycloak kullanıcıya
    şifre belirleme bağlantısı içeren e-posta gönderir.
    """
    url = f"{KEYCLOAK_URL}/admin/realms/{TARGET_REALM}/users/{user_id}/execute-actions-email"
    payload = json.dumps(["UPDATE_PASSWORD"]).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, method="PUT")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            if resp.status in (200, 204):
                return True
        return False
    except urllib.error.HTTPError as e:
        print(f"--> [HATA] Mail gönderilemedi (HTTP {e.code}): {e.read().decode()}")
        return False
    except Exception as e:
        print(f"--> [HATA] Beklenmeyen hata: {e}")
        return False


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

def main():
    print(f"\n--> [1/3] Admin token alınıyor...")
    token = get_admin_token()
    if not token:
        sys.exit(1)

    print(f"--> [2/3] '{TARGET_USERNAME}' için Keycloak UUID sorgulanıyor...")
    user_id = get_user_id(token, TARGET_USERNAME)
    if not user_id:
        sys.exit(1)
    print(f"--> [BİLGİ] UUID: {user_id}")

    print(f"--> [3/3] UPDATE_PASSWORD e-postası tetikleniyor...")
    if send_update_password_email(token, user_id):
        print("--> [BAŞARILI] Şifre belirleme e-postası Keycloak kuyruğuna iletildi.")
    else:
        sys.exit(1)

    print("==================================================")


if __name__ == "__main__":
    main()
