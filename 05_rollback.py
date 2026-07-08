#!/usr/bin/env python3.11
import subprocess
import json
import os
import sys
import ssl
import argparse
import urllib.request
import urllib.parse
import urllib.error

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

from migration_state import get_user, get_pending_users, mark_rollback, load_users

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

APIC_SERVER       = os.environ.get("APIC_SERVER")
PROV_ORG          = os.environ.get("PROV_ORG")
LOCAL_REGISTRY    = os.environ.get("LOCAL_REGISTRY")
KEYCLOAK_REGISTRY = os.environ.get("KEYCLOAK_REGISTRY_NAME", "keycluk")
KEYCLOAK_URL      = os.environ.get("KEYCLOAK_URL")
KEYCLOAK_ADMIN_USER     = os.environ.get("KEYCLOAK_ADMIN_USER")
KEYCLOAK_ADMIN_PASSWORD = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
TARGET_REALM      = os.environ.get("KEYCLOAK_REALM_NAME", "apic-demo")
CATALOG           = os.environ.get("CATALOG", "")

# ------------------------------------------------------------------------------
# ADIM R0 — Tapuyu Geri Ver (DÜZELTİLDİ: Doğru YAML Payload Formatı)
# ------------------------------------------------------------------------------
def _get_local_user_url(username):
    cmd = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json", "--output", "-"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout).get("url")
    except Exception:
        return None

def _ensure_member_and_get_url(consumer_org, username, user_url):
    # Üye yap (zaten üyeyse idempotent'tir)
    yaml_content = f'name: "{username}-local"\ntitle: "{username}"\nuser:\n  url: "{user_url}"\n'
    cmd_add = [
        "apic", "members:create", "--scope", "consumer-org", "-",
        "-s", APIC_SERVER, "-o", PROV_ORG, "-c", CATALOG,
        "--consumer-org", consumer_org
    ]
    subprocess.run(cmd_add, input=yaml_content, capture_output=True, text=True)

    # Member URL'yi al
    cmd_list = [
        "apic", "members:list", "--scope", "consumer-org",
        "-s", APIC_SERVER, "-o", PROV_ORG, "-c", CATALOG,
        "--consumer-org", consumer_org,
        "--format", "json", "--output", "-"
    ]
    try:
        res = subprocess.run(cmd_list, capture_output=True, text=True, check=True)
        items = json.loads(res.stdout)
        items = items.get("results", items if isinstance(items, list) else [items])
        for m in items:
            if m.get("user", {}).get("url") == user_url:
                return m.get("url")
    except Exception:
        pass
    return None

def revert_org_ownership(consumer_org, local_username):
    user_url = _get_local_user_url(local_username)
    if not user_url:
        print(f"--> [HATA] Local kullanıcı URL'si alınamadı ({local_username}).")
        return False

    member_url = _ensure_member_and_get_url(consumer_org, local_username, user_url)
    if not member_url:
        print(f"--> [HATA] Local kullanıcı Member URL'si alınamadı.")
        return False

    yaml_content = f"new_owner_member_url: {member_url}\n"
    cmd = [
        "apic", "consumer-orgs:transfer-owner", consumer_org, "-",
        "-s", APIC_SERVER, "-o", PROV_ORG, "-c", CATALOG,
        "--cascade"
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        print(f"--> [ROLLBACK] '{consumer_org}' tapusu Local Registry'ye ({local_username}) geri devredildi.")
        return True
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() or e.stdout.strip()
        if "already the owner" in err.lower():
            print("--> [ROLLBACK] Local kullanıcı zaten organizasyonun sahibi, devir atlandı.")
            return True
        print(f"--> [UYARI] Tapu devri geri alınamadı: {err}")
        return False

# ------------------------------------------------------------------------------
# ADIM R1 — Nokta Atışı APIC shadow user silme
# ------------------------------------------------------------------------------
def _get_exact_apic_kc_username(target_email):
    cmd = [
        "apic", "users:list",
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", KEYCLOAK_REGISTRY,
        "--format", "json", "--output", "-"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        items = data.get("results", data if isinstance(data, list) else [data])
        for u in items:
            if u.get("email") == target_email:
                return u.get("name") or u.get("username")
        return None
    except Exception:
        return None

def rollback_apic_shadow_user(username, target_email):
    exact_username = _get_exact_apic_kc_username(target_email)
    if not exact_username:
        print(f"--> [ROLLBACK] '{target_email}' e-postasına sahip APIC shadow user zaten yok, atlanıyor.")
        return True

    cmd = [
        "apic", "users:delete", exact_username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", KEYCLOAK_REGISTRY,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"--> [ROLLBACK] APIC shadow user '{exact_username}' başarıyla silindi.")
        return True
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() or e.stdout.strip()
        if "read only" in err.lower():
            print("--> [BİLGİ] Kullanıcı read-only sistem hesabı, silme atlandı.")
            return True
        print(f"--> [HATA] APIC shadow user silinemedi: {err}")
        return False

# ------------------------------------------------------------------------------
# ADIM R2 — APIC e-postasını orijinaline geri al
# ------------------------------------------------------------------------------
def rollback_apic_email(username, target_email):
    cmd_get = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json", "--output", "-",
    ]
    first_name = ""
    last_name  = ""
    try:
        res = subprocess.run(cmd_get, capture_output=True, text=True, check=True)
        current = json.loads(res.stdout)
        first_name = current.get("first_name") or current.get("firstName") or ""
        last_name  = current.get("last_name")  or current.get("lastName")  or ""
    except Exception:
        pass

    yaml_content = f"email: {target_email}\ntitle: {username}\n"
    if first_name: yaml_content += f"first_name: {first_name}\n"
    if last_name: yaml_content += f"last_name: {last_name}\n"

    cmd_upd = [
        "apic", "users:update", username, "-",
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
    ]
    try:
        subprocess.run(cmd_upd, input=yaml_content, capture_output=True, text=True, check=True)
        print(f"--> [ROLLBACK] APIC e-postası '{target_email}' olarak geri alındı.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"--> [HATA] E-posta geri alınamadı: {e.stderr.strip() or e.stdout.strip()}")
        return False

# ------------------------------------------------------------------------------
# ADIM R3 — Keycloak kullanıcısını sil
# ------------------------------------------------------------------------------
def _get_kc_admin_token():
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
    except Exception:
        return None

def rollback_kc_user(username):
    token = _get_kc_admin_token()
    if not token: return False

    url_get = f"{KEYCLOAK_URL}/admin/realms/{TARGET_REALM}/users?username={username}&exact=true"
    try:
        req = urllib.request.Request(url_get)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            users = json.loads(resp.read().decode())
            if not users:
                print(f"--> [ROLLBACK] Keycloak'ta '{username}' zaten yok, atlanıyor.")
                return True
            kc_uuid = users[0]["id"]

            req_del = urllib.request.Request(f"{KEYCLOAK_URL}/admin/realms/{TARGET_REALM}/users/{kc_uuid}", method="DELETE")
            req_del.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req_del, context=_SSL_CTX) as resp_del:
                if resp_del.status in (200, 204):
                    print(f"--> [ROLLBACK] Keycloak kullanıcısı '{username}' silindi.")
                    return True
    except Exception as e:
        print(f"--> [HATA] KC kullanıcı silinemedi: {e}")
        return False

# ------------------------------------------------------------------------------
# DURUM TESPİTİ VE ORKESTRATÖR
# ------------------------------------------------------------------------------
def detect_apic_email_parked(username):
    cmd = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        email = json.loads(res.stdout).get("email", "")
        return "-old@" in email, email
    except Exception:
        return None, None

def derive_target_email(source_email):
    if source_email and "@" in source_email:
        parts = source_email.split("@")
        username_part = parts[0]
        while username_part.endswith("-old"):
            username_part = username_part[:-4]
        return f"{username_part}@{parts[1]}"
    return source_email

def rollback_user(csv_row, force=False):
    username     = csv_row["username"]
    target_email = csv_row.get("target_email", "")
    consumer_org = csv_row.get("consumer_org", "")

    print(f"\n{'='*50}")
    print(f"  ROLLBACK: {username}{'  [FORCE]' if force else ''}")
    print(f"{'='*50}")

    if force:
        parked, current_email = detect_apic_email_parked(username)
        target_email = derive_target_email(current_email or target_email)

    # R0: Tapuyu Kurtar (Eğer Org varsa)
    if consumer_org and (force or csv_row.get("org_owner_xfrd", "false").lower() == "true"):
        print("--> [R0] Consumer Org tapusu Local kullanıcıya geri devrediliyor...")
        revert_org_ownership(consumer_org, username)

    # R1: Gölgeyi Sil
    if force or csv_row.get("apic_jit_done", "false").lower() == "true":
        print("--> [R1] APIC shadow user aranıyor ve siliniyor...")
        if not rollback_apic_shadow_user(username, target_email):
            print(f"--> [DURDURULDU] '{username}' rollback R1'de başarısız oldu.")
            return False

    # R2: E-postayı İade Et
    if force or csv_row.get("apic_email_parked", "false").lower() == "true":
        print(f"--> [R2] APIC e-postası '{target_email}' olarak geri alınıyor...")
        if not rollback_apic_email(username, target_email):
            print(f"--> [DURDURULDU] '{username}' rollback R2'de başarısız oldu.")
            return False

    # R3: Keycloak'u Temizle
    if force or csv_row.get("kc_user_created", "false").lower() == "true":
        print("--> [R3] Keycloak kullanıcısı siliniyor...")
        if not rollback_kc_user(username):
            print(f"--> [DURDURULDU] '{username}' rollback R3'de başarısız oldu.")
            return False

    mark_rollback(username)
    print(f"--> [ROLLBACK TAMAMLANDI] '{username}' sanki hiç dokunulmamış gibi sıfırlandı.")
    return True

def main():
    parser = argparse.ArgumentParser(description="Migration adımlarını geri alır.")
    parser.add_argument("username", nargs="?", help="Tek kullanıcı rollback (opsiyonel)")
    parser.add_argument("--force", action="store_true", help="Başarılı olanlar dahil tüm listeyi geri al")
    args = parser.parse_args()

    if args.username:
        csv_row = get_user(args.username)
        if not csv_row:
            if args.force: csv_row = {"username": args.username, "target_email": ""}
            else:
                print(f"--> [HATA] '{args.username}' CSV'de bulunamadı.")
                sys.exit(1)
        success = rollback_user(csv_row, force=args.force)
        sys.exit(0 if success else 1)
    else:
        # --force ile başarılılar dahil bütün CSV'yi çeker
        to_rollback = load_users() if args.force else [
            u for u in get_pending_users() if any(u.get(f, "false").lower() == "true" for f in ("kc_user_created", "apic_email_parked", "apic_jit_done", "org_owner_xfrd"))
        ]

        if not to_rollback:
            print("--> [BİLGİ] Rollback gereken kullanıcı yok.")
            sys.exit(0)

        print(f"--> {len(to_rollback)} kullanıcı için rollback başlatılıyor...\n")
        failed = []
        for row in to_rollback:
            if not rollback_user(row, force=args.force):
                failed.append(row["username"])

        print("\n" + "="*50)
        if failed:
            print(f"[UYARI] Şu kullanıcılar rollback edilemedi: {', '.join(failed)}")
            sys.exit(1)
        else:
            print("[TAMAMLANDI] Tüm rollback işlemleri başarılı.")

if __name__ == "__main__":
    main()