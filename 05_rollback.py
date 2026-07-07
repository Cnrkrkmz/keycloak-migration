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

def rollback_apic_email(username, target_email):
    """APIC Local Registry'de e-postayı hedef değere geri yazar."""
    # Mevcut first_name/last_name'i almaya çalış — başarısız olursa boş bırak,
    # users:update email-only güncellemeyi kabul eder.
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
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() or e.stdout.strip()
        print(f"--> [UYARI] Kullanıcı detayı alınamadı, güncelleme yine de denenecek: {err}")
    except Exception as e:
        print(f"--> [UYARI] Kullanıcı detayı alınamadı, güncelleme yine de denenecek: {e}")

    yaml_content = f"""email: {target_email}
title: {username}
"""
    if first_name:
        yaml_content += f"first_name: {first_name}\n"
    if last_name:
        yaml_content += f"last_name: {last_name}\n"

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
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
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
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
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
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
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
# DURUM TESPİTİ — APIC ve KC'ye bakarak gerçek durumu öğren
# ------------------------------------------------------------------------------

def detect_apic_email_parked(username):
    """
    APIC'ten kullanıcının mevcut e-postasını okur.
    E-posta '-old@' içeriyorsa email park edilmiş demektir → True döner.
    Kullanıcı bulunamazsa veya e-posta okunamazsa None döner.
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
        email = data.get("email", "")
        return "-old@" in email, email
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() or e.stdout.strip()
        print(f"--> [UYARI] APIC'ten e-posta okunamadı ({username}): {err}")
        return None, None
    except Exception as e:
        print(f"--> [UYARI] APIC'ten e-posta okunamadı ({username}): {e}")
        return None, None


def derive_target_email(source_email):
    """Ne kadar '-old' suffix'i varsa hepsini temizleyerek orijinal hedef e-postayı hesaplar."""
    if source_email and "@" in source_email:
        parts = source_email.split("@")
        username_part = parts[0]
        while username_part.endswith("-old"):
            username_part = username_part[:-4]
        return f"{username_part}@{parts[1]}"
    return source_email


# ------------------------------------------------------------------------------
# ROLLBACK ORKESTRATÖRü
# ------------------------------------------------------------------------------

def rollback_user(csv_row, force=False):
    """
    Tek bir kullanıcı için tamamlanmış adımları ters sırayla geri alır.
    Herhangi bir adım başarısız olursa durur ve False döner.

    force=True: CSV flag'lerine bakmadan APIC ve KC'ye bakarak gerçek
                durumu tespit eder. CSV'nin eski/eksik olduğu durumlarda kullan.
    """
    username     = csv_row["username"]
    target_email = csv_row.get("target_email", "")

    print(f"\n{'='*50}")
    print(f"  ROLLBACK: {username}{'  [FORCE]' if force else ''}")
    print(f"{'='*50}")

    # Force modunda flag'leri gerçek durumdan hesapla
    if force:
        parked, current_email = detect_apic_email_parked(username)
        kc_exists = bool(_get_kc_uuid(username))
        do_r2 = (parked is True)
        do_r3 = kc_exists
        # target_email: CSV'de -old@ ile yazılmışsa gerçek e-postayı hesapla
        if not target_email or "-old@" in target_email:
            target_email = derive_target_email(current_email or target_email)
        print(f"--> [FORCE] APIC e-posta parked={parked}  KC exists={kc_exists}")
        print(f"--> [FORCE] Geri alınacak hedef e-posta: {target_email}")
    else:
        do_r2 = csv_row.get("apic_email_parked", "false").lower() == "true"
        do_r3 = csv_row.get("kc_user_created",   "false").lower() == "true"

    # R1: apic_jit_done → shadow user sil (force modda her zaman dene)
    if force or csv_row.get("apic_jit_done", "false").lower() == "true":
        print("--> [R1] APIC shadow user siliniyor...")
        if not rollback_apic_shadow_user(username):
            print(f"--> [DURDURULDU] '{username}' rollback R1'de başarısız oldu.")
            return False

    # R2: apic_email_parked → hedef e-postaya dön
    if do_r2:
        print(f"--> [R2] APIC e-postası '{target_email}' olarak geri alınıyor...")
        if not rollback_apic_email(username, target_email):
            print(f"--> [DURDURULDU] '{username}' rollback R2'de başarısız oldu.")
            return False

    # R3: kc_user_created → KC kullanıcısını sil
    if do_r3:
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
    parser = argparse.ArgumentParser(
        description="Migration adımlarını geri alır."
    )
    parser.add_argument("username", nargs="?", help="Tek kullanıcı rollback (opsiyonel)")
    parser.add_argument(
        "--force", action="store_true",
        help="CSV flag'lerine bakmadan APIC ve KC'ye bakarak gerçek durumu tespit et ve geri al"
    )
    args = parser.parse_args()

    if args.username:
        csv_row = get_user(args.username)
        if not csv_row:
            # --force ile CSV'de olmayan kullanıcı da rollback edilebilir
            if args.force:
                csv_row = {"username": args.username, "target_email": ""}
            else:
                print(f"--> [HATA] '{args.username}' CSV'de bulunamadı.")
                sys.exit(1)
        success = rollback_user(csv_row, force=args.force)
        sys.exit(0 if success else 1)
    else:
        pending = get_pending_users()

        if args.force:
            # Force modda tüm pending kullanıcıları işle (flag durumuna bakma)
            to_rollback = pending
        else:
            # Normal modda sadece en az bir flag'i true olanları al
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
