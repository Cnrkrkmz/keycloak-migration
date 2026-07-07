#!/usr/bin/env python3.11
import subprocess
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error

from migration_state import get_user, get_pending_users, mark_rollback

# ==============================================================================
# ROLLBACK SCRİPTİ — Yarıda kalan veya başarısız migration'ı geri alır.
# ==============================================================================
# Çalıştırma sırası: 07 (hata durumunda — isteğe bağlı)
#
# Kullanım:
#   Tek kullanıcı     : python 07_rollback.py <username>
#   Tüm yarım kalanlar: python 07_rollback.py
#
# Her flag için ne geri alınır:
#   apic_provisioned=true → APIC'te Keycloak registry'sindeki shadow user'ı sil
#   email_updated=true    → APIC Local Registry'de e-postayı orijinaline geri al
#   kc_created=true       → Keycloak'tan kullanıcıyı sil
#
# Sıra intentionally ters: önce en son yapılan geri alınır.
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
# ADIM R1 — APIC shadow user sil (apic_provisioned geri al)
# ------------------------------------------------------------------------------

def rollback_apic_shadow_user(username):
    """
    APIC'teki Keycloak registry'sine ait shadow user kaydını siler.
    'username' burada Keycloak UUID'sidir (sub claim) — önce KC'den çekilir.
    """
    # Shadow user'ın APIC'teki adı = Keycloak UUID. Önce KC'den UUID'yi bul.
    kc_uuid = _get_kc_uuid(username)
    if not kc_uuid:
        print(f"--> [UYARI] Keycloak'ta '{username}' bulunamadı, APIC shadow user silme atlanıyor.")
        return True  # KC yoksa shadow user da yoktur

    cmd = [
        "apic", "users:delete", kc_uuid,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", KEYCLOAK_REGISTRY,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"--> [ROLLBACK] APIC shadow user '{kc_uuid}' silindi.")
        return True
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() or e.stdout.strip()
        if "not found" in err.lower() or "404" in err:
            print(f"--> [ROLLBACK] APIC shadow user zaten yok, atlanıyor.")
            return True
        print(f"--> [HATA] APIC shadow user silinemedi: {err}")
        return False


# ------------------------------------------------------------------------------
# ADIM R2 — APIC e-postasını orijinaline geri al (email_updated geri al)
# ------------------------------------------------------------------------------

def rollback_apic_email(username, original_email):
    """APIC Local Registry'de e-postayı orijinal değere geri yazar."""
    # Mevcut veriyi oku
    cmd_get = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd_get, capture_output=True, text=True, check=True)
        current = json.loads(res.stdout)
    except Exception as e:
        print(f"--> [HATA] APIC kullanıcı verisi okunamadı: {e}")
        return False

    first_name = current.get("first_name") or current.get("firstName") or ""
    last_name  = current.get("last_name")  or current.get("lastName")  or ""

    yaml_content = f"""email: {original_email}
first_name: {first_name}
last_name: {last_name}
title: {username}
"""
    cmd_upd = [
        "apic", "users:update", username, "-",
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
    ]
    try:
        subprocess.run(cmd_upd, input=yaml_content, capture_output=True, text=True, check=True)
        print(f"--> [ROLLBACK] APIC e-postası '{original_email}' olarak geri alındı.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"--> [HATA] E-posta geri alınamadı: {e.stderr.strip() or e.stdout.strip()}")
        return False


# ------------------------------------------------------------------------------
# ADIM R3 — Keycloak kullanıcısını sil (kc_created geri al)
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
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode()).get("access_token")
    except Exception as e:
        print(f"--> [HATA] KC admin token alınamadı: {e}")
        return None


def _get_kc_uuid(username):
    """Keycloak'tan kullanıcının UUID'sini döndürür."""
    token = _get_kc_admin_token()
    if not token:
        return None
    url = f"{KEYCLOAK_URL}/admin/realms/{TARGET_REALM}/users?username={username}&exact=true"
    try:
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req) as resp:
            users = json.loads(resp.read().decode())
            return users[0]["id"] if users else None
    except Exception as e:
        print(f"--> [HATA] KC kullanıcı sorgusu başarısız: {e}")
        return None


def rollback_kc_user(username):
    """Keycloak'taki kullanıcıyı UUID üzerinden siler."""
    token = _get_kc_admin_token()
    if not token:
        return False

    kc_uuid = _get_kc_uuid(username)
    if not kc_uuid:
        print(f"--> [ROLLBACK] Keycloak'ta '{username}' zaten yok, atlanıyor.")
        return True

    url = f"{KEYCLOAK_URL}/admin/realms/{TARGET_REALM}/users/{kc_uuid}"
    try:
        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req) as resp:
            if resp.status in (200, 204):
                print(f"--> [ROLLBACK] Keycloak kullanıcısı '{username}' silindi.")
                return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"--> [ROLLBACK] Keycloak kullanıcısı zaten yok, atlanıyor.")
            return True
        print(f"--> [HATA] KC kullanıcı silinemedi (HTTP {e.code}): {e.read().decode()}")
    return False


# ------------------------------------------------------------------------------
# ROLLBACK ORKESTRATÖRü
# ------------------------------------------------------------------------------

def rollback_user(csv_row):
    """
    Tek bir kullanıcı için tamamlanmış adımları ters sırayla geri alır.
    Herhangi bir adım başarısız olursa durur ve False döner.
    """
    username       = csv_row["username"]
    original_email = csv_row["src_email"]

    print(f"\n{'='*50}")
    print(f"  ROLLBACK: {username}")
    print(f"{'='*50}")

    # R1: apic_jit_done → shadow user sil
    if csv_row.get("apic_jit_done", "false").lower() == "true":
        print("--> [R1] APIC shadow user siliniyor...")
        if not rollback_apic_shadow_user(username):
            print(f"--> [DURDURULDU] '{username}' rollback R1'de başarısız oldu.")
            return False

    # R2: apic_email_parked → orijinal e-postaya dön
    if csv_row.get("apic_email_parked", "false").lower() == "true":
        print("--> [R2] APIC e-postası orijinaline geri alınıyor...")
        if not rollback_apic_email(username, original_email):
            print(f"--> [DURDURULDU] '{username}' rollback R2'de başarısız oldu.")
            return False

    # R3: kc_user_created → KC kullanıcısını sil
    if csv_row.get("kc_user_created", "false").lower() == "true":
        print("--> [R3] Keycloak kullanıcısı siliniyor...")
        if not rollback_kc_user(username):
            print(f"--> [DURDURULDU] '{username}' rollback R3'de başarısız oldu.")
            return False

    mark_rollback(username)
    print(f"--> [ROLLBACK TAMAMLANDI] '{username}' sanki hiç dokunulmamış gibi sıfırlandı.")
    return True


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1:
        # Tek kullanıcı rollback: python 05_rollback.py <username>
        target = sys.argv[1]
        csv_row = get_user(target)
        if not csv_row:
            print(f"--> [HATA] '{target}' CSV'de bulunamadı.")
            sys.exit(1)
        success = rollback_user(csv_row)
        sys.exit(0 if success else 1)
    else:
        # Tüm migrated=false (yarıda kalmış) kullanıcıları rollback et
        pending = get_pending_users()
        # Hiçbir flag set edilmemişleri (henüz dokunulmamışları) filtrele
        to_rollback = [
            u for u in pending
            if any(u.get(f, "false").lower() == "true"
                   for f in ("kc_user_created", "apic_email_parked", "apic_jit_done", "org_owner_xfrd"))
        ]

        if not to_rollback:
            print("--> [BİLGİ] Rollback gereken kullanıcı yok.")
            sys.exit(0)

        print(f"--> {len(to_rollback)} kullanıcı için rollback başlatılıyor...\n")
        failed = []
        for row in to_rollback:
            if not rollback_user(row):
                failed.append(row["username"])

        print("\n" + "="*50)
        if failed:
            print(f"[UYARI] Şu kullanıcılar rollback edilemedi: {', '.join(failed)}")
            sys.exit(1)
        else:
            print("[TAMAMLANDI] Tüm rollback işlemleri başarılı.")


if __name__ == "__main__":
    main()
