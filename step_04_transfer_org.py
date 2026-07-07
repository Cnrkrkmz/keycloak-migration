#!/usr/bin/env python3.11
import subprocess
import json
import os
import sys

from migration_state import get_user, update_flag, mark_migrated

# ==============================================================================
# step_04_transfer_org.py — Consumer Org sahipliğini Keycloak profiline devret
# Çalıştıran: 04_run_migration.py (adım 4/4)
#
# Bir Consumer Org'un sahipliğini (ve --cascade ile tüm Apps + Subscriptions'larını)
# eski Local Registry kullanıcısından yeni Keycloak kullanıcısına devreder.
#
# Adımlar:
#   1. Keycloak registry'sinde kullanıcının shadow profilini e-posta ile bul
#   2. Shadow profili Consumer Org'a üye olarak ekle
#   3. consumer-orgs:transfer-owner --cascade ile sahipliği devret
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
CATALOG           = os.environ.get("CATALOG")
LOCAL_REGISTRY    = os.environ.get("LOCAL_REGISTRY", "sandbox-catalog")
KEYCLOAK_REGISTRY = os.environ.get("KEYCLOAK_REGISTRY_NAME", "keycluk")

if len(sys.argv) > 1:
    TARGET_USERNAME = sys.argv[1]
else:
    TARGET_USERNAME = input("Sahipliği devredilecek APIC Kullanıcı Adı: ").strip()


# ------------------------------------------------------------------------------
# YARDIMCI FONKSİYONLAR
# ------------------------------------------------------------------------------

def get_consumer_org_for_user(username):
    """CSV'den kullanıcının consumer_org değerini okur."""
    row = get_user(username)
    if row:
        return row.get("consumer_org", "")
    return ""


def get_target_email(username):
    """
    APIC Local Registry'den kullanıcının mevcut e-postasını okur.
    -old suffix'i varsa çıkarıp hedef e-postayı döndürür.
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
        if "-old@" in email:
            email = email.replace("-old@", "@")
        return email
    except Exception as e:
        print(f"--> [HATA] Local registry'den email alınamadı: {e}")
        return None


def get_shadow_user_url(consumer_org, expected_email):
    """
    APIC'teki Keycloak registry'sinde e-posta ile shadow user'ı bulur.
    Kullanıcının URL'sini döndürür.
    """
    cmd = [
        "apic", "users:list",
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", KEYCLOAK_REGISTRY,
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        items = data.get("results", data if isinstance(data, list) else [data])
        for u in items:
            if u.get("email") == expected_email:
                return u.get("url")
        return None
    except Exception as e:
        print(f"--> [HATA] Keycloak registry okunamadı: {e}")
        return None


def add_kc_user_as_member(consumer_org, username, user_url):
    """Shadow user'ı Consumer Org'a üye olarak ekler."""
    yaml_content = f"""name: "{username}-kc"
title: "{username}"
user:
  url: "{user_url}"
"""
    cmd = [
        "apic", "members:create", "--scope", "consumer-org", "-",
        "-s", APIC_SERVER, "-o", PROV_ORG, "-c", CATALOG,
        "--consumer-org", consumer_org,
    ]
    try:
        res = subprocess.run(cmd, input=yaml_content, capture_output=True, text=True)
        if res.returncode == 0:
            print(f"--> [BAŞARILI] Shadow user Consumer Org'a üye eklendi.")
            return True
        if "already exists" in (res.stderr + res.stdout).lower():
            print("--> [BİLGİ] Shadow user zaten bu org'da üye.")
            return True
        print(f"--> [HATA] Üye ekleme başarısız:\n{res.stderr.strip() or res.stdout.strip()}")
        return False
    except Exception as e:
        print(f"--> [HATA] Beklenmeyen hata: {e}")
        return False


def get_member_url(consumer_org, expected_email):
    """Consumer Org içindeki shadow user'ın member URL'sini döndürür."""
    cmd = [
        "apic", "members:list", "--scope", "consumer-org",
        "-s", APIC_SERVER, "-o", PROV_ORG, "-c", CATALOG,
        "--consumer-org", consumer_org,
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        items = data.get("results", data if isinstance(data, list) else [data])
        for m in items:
            if m.get("user", {}).get("email") == expected_email:
                return m.get("url")
        return None
    except Exception as e:
        print(f"--> [HATA] Member listesi okunamadı: {e}")
        return None


def transfer_ownership(consumer_org, member_url):
    """
    --cascade ile Consumer Org sahipliğini (Apps + Subscriptions dahil)
    yeni Keycloak üyesine devreder.
    """
    yaml_content = f"new_owner_member_url: {member_url}\n"
    cmd = [
        "apic", "consumer-orgs:transfer-owner", consumer_org, "-",
        "-s", APIC_SERVER, "-o", PROV_ORG, "-c", CATALOG,
        "--cascade",
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        print(f"--> [BAŞARILI] '{consumer_org}' sahipliği (Apps + Subscriptions ile) Keycloak profiline devredildi.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"--> [HATA] Sahiplik devri başarısız:\n{e.stderr.strip() or e.stdout.strip()}")
        return False


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

def main():
    csv_row = get_user(TARGET_USERNAME)
    if not csv_row:
        print(f"--> [HATA] '{TARGET_USERNAME}' CSV'de bulunamadı. Önce 05 scriptini çalıştırın.")
        sys.exit(1)

    if csv_row.get("migrated", "false").lower() != "true":
        print(f"--> [HATA] '{TARGET_USERNAME}' henüz migrate edilmemiş (migrated=false).")
        print("--> Önce 03→04→05 adımlarını tamamlayın.")
        sys.exit(1)

    consumer_org = csv_row.get("consumer_org", "")
    if not consumer_org:
        print(f"--> [HATA] CSV'de '{TARGET_USERNAME}' için consumer_org bilgisi yok.")
        sys.exit(1)

    print(f"\n--> [1/4] '{TARGET_USERNAME}' için hedef e-posta hesaplanıyor...")
    expected_email = get_target_email(TARGET_USERNAME)
    if not expected_email:
        sys.exit(1)
    print(f"--> [BİLGİ] Hedef e-posta: {expected_email}")

    print(f"--> [2/4] Keycloak registry'de shadow user aranıyor...")
    user_url = get_shadow_user_url(consumer_org, expected_email)
    if not user_url:
        print(f"--> [HATA] '{expected_email}' için shadow user bulunamadı. 05 scriptini kontrol edin.")
        sys.exit(1)

    print(f"--> [3/4] Shadow user '{consumer_org}' org'una üye ekleniyor...")
    if not add_kc_user_as_member(consumer_org, TARGET_USERNAME, user_url):
        sys.exit(1)

    print(f"--> [4/4] Sahiplik devrediliyor (--cascade)...")
    member_url = get_member_url(consumer_org, expected_email)
    if not member_url:
        print("--> [HATA] Member URL alınamadı, sahiplik devri yapılamadı.")
        sys.exit(1)

    if not transfer_ownership(consumer_org, member_url):
        sys.exit(1)

    update_flag(TARGET_USERNAME, "org_owner_xfrd", True)
    mark_migrated(TARGET_USERNAME)

    print("==================================================")
    print(f"[TAMAMLANDI] '{consumer_org}' sahipliği '{TARGET_USERNAME}' → Keycloak profiline devredildi.")
    print("==================================================")


if __name__ == "__main__":
    main()
