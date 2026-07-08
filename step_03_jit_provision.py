#!/usr/bin/env python3.11
import json
import os
import sys
import ssl
import urllib.request
import urllib.error

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

from migration_state import get_user, update_flag, mark_migrated

# ==============================================================================
# step_03_jit_provision.py — APIC Üzerinden Gerçek Login (Password Grant)
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

APIC_SERVER        = os.environ.get("APIC_SERVER")
PROV_ORG           = os.environ.get("PROV_ORG")
CATALOG            = os.environ.get("CATALOG", "sandbox")
KEYCLOAK_REGISTRY  = os.environ.get("KEYCLOAK_REGISTRY_NAME", "keycluk")

if len(sys.argv) > 1:
    TARGET_USERNAME = sys.argv[1]
else:
    TARGET_USERNAME = input("APIC'te gölge profili açılacak Kullanıcı Adı: ").strip()

def _http(url, *, data=None, method=None, headers=None):
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

def trigger_real_apic_login(username, password):
    url = f"{APIC_SERVER}/api/token" # Fallback
    creds_file = os.environ.get("APIC_CLIENT_CREDS", "")
    apic_client_id     = ""
    apic_client_secret = ""

    if creds_file and os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                creds = json.load(f)
            toolkit_creds = creds.get("consumer_toolkit", {})
            if "endpoint" in toolkit_creds:
                url = f"{toolkit_creds['endpoint']}/token"
            apic_client_id     = toolkit_creds.get("client_id") or creds.get("client_id", "")
            apic_client_secret = toolkit_creds.get("client_secret") or creds.get("client_secret", "")
        except Exception as e:
            print(f"--> [UYARI] Kimlik dosyası okunamadı: {e}")

    if not apic_client_id or not apic_client_secret:
        print(f"--> [HATA] '{creds_file}' içinden client_id veya client_secret okunamadı!")
        return False

    realm_str = f"consumer:{PROV_ORG}:{CATALOG}/{KEYCLOAK_REGISTRY}"
    
    # BÜYÜK DEĞİŞİKLİK: Araya girmek yok. Doğrudan APIC üzerinden "password" grant ile login oluyoruz!
    payload = json.dumps({
        "realm":         realm_str,
        "grant_type":    "password",
        "username":      username,
        "password":      password,
        "client_id":     apic_client_id,
        "client_secret": apic_client_secret,
    }).encode("utf-8")

    try:
        status, body = _http(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-IBM-Consumer-Context": f"{PROV_ORG}.{CATALOG}"
            },
        )
        if status == 200:
            print("--> [BAŞARILI] APIC üzerinden gerçek login yapıldı. Tüm iç bağlantılar kurularak gölge kullanıcı oluşturuldu.")
            return True
        else:
            print(f"--> [HATA] APIC login başarısız (HTTP {status}): {body}")
            return False
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"--> [HATA] APIC login reddedildi (HTTP {e.code}): {err_body}")
        return False
    except Exception as e:
        print(f"--> [HATA] İstek sırasında hata: {e}")
        return False

def clear_temp_password():
    key = "KC_TEMP_PASSWORD"
    try:
        with open(ENV_FILE, "r") as f:
            lines = f.readlines()
        filtered = [l for l in lines if not l.startswith(f"export {key}=")]
        if len(filtered) != len(lines):
            with open(ENV_FILE, "w") as f:
                f.writelines(filtered)
    except Exception:
        pass

def main():
    csv_row = get_user(TARGET_USERNAME)
    if not csv_row:
        print(f"--> [HATA] '{TARGET_USERNAME}' CSV'de bulunamadı.")
        sys.exit(1)

    if csv_row.get("migrated", "false").lower() == "true":
        print(f"--> [BİLGİ] '{TARGET_USERNAME}' zaten migrate edilmiş. Atlanıyor.")
        return

    if csv_row.get("apic_jit_done", "false").lower() == "true":
        print(f"--> [BİLGİ] '{TARGET_USERNAME}' APIC'te zaten provision edilmiş. Atlanıyor.")
        return

    load_env()
    kc_temp_password = os.environ.get("KC_TEMP_PASSWORD", "")

    if not kc_temp_password:
        print("--> [HATA] 'KC_TEMP_PASSWORD' bulunamadı! 1. Adım tekrar edilmeli.")
        sys.exit(1)

    print(f"\n--> [1/1] '{TARGET_USERNAME}' için APIC üzerinden gerçek login (Password Grant) simüle ediliyor...")
    success = trigger_real_apic_login(TARGET_USERNAME, kc_temp_password)

    if not success:
        sys.exit(1)

    update_flag(TARGET_USERNAME, "apic_jit_done", True)
    mark_migrated(TARGET_USERNAME)
    clear_temp_password()

    print("==================================================")
    print(f"[TAMAMLANDI] '{TARGET_USERNAME}' gerçek OIDC akışıyla login edildi.")
    print("==================================================")

if __name__ == "__main__":
    main()