#!/usr/bin/env python3.11
import subprocess
import json
import os
import sys

# ==============================================================================
# APIC CONSUMER ORG OLUŞTURMA SCRİPTİ
# ==============================================================================
# Çalıştırma sırası: 00 (test ortamı hazırlığı — migration öncesi)
#
# Verilen isimde bir Consumer Org oluşturur ve owner olarak belirtilen
# kullanıcıyı (Local Registry'den) atar.
# ==============================================================================

ENV_FILE = "migration_env.sh"


def load_env():
    if not os.path.exists(ENV_FILE):
        print(f"--> [HATA] '{ENV_FILE}' bulunamadı! Önce 02_setup_and_login.py'i çalıştırın.")
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
    """Local Registry'deki kullanıcının APIC URL'sini döndürür."""
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
        print(f"--> [HATA] Kullanıcı URL'si alınamadı: {e.stderr.strip() or e.stdout.strip()}")
        return None
    except json.JSONDecodeError:
        print("--> [HATA] APIC yanıtı geçerli JSON değil.")
        return None


def create_consumer_org(org_name, owner_username, owner_url):
    """
    Belirtilen isimde bir Consumer Org oluşturur ve owner'ı atar.
    APIC, consumer org yaratılırken owner'ı otomatik olarak org'a üye yapar.
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
        print(f"--> [BAŞARILI] Consumer Org '{org_name}' oluşturuldu.")
        print(f"--> [BİLGİ]   Owner: '{owner_username}'")
        return True
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() or e.stdout.strip()
        if "already exists" in err.lower():
            print(f"--> [BİLGİ] Consumer Org '{org_name}' zaten mevcut.")
            return True
        print(f"--> [HATA] Consumer Org oluşturulamadı:\n{err}")
        return False


def main():
    print("\n==================================================")
    print("        CONSUMER ORG OLUŞTURMA                   ")
    print("==================================================")

    org_name       = input("Consumer Org Adı: ").strip()
    owner_username = input("Owner Kullanıcı Adı (Local Registry'deki): ").strip()

    print(f"\n--> [1/2] '{owner_username}' kullanıcısının URL'si alınıyor...")
    owner_url = get_user_url(owner_username)
    if not owner_url:
        print(f"--> [HATA] '{owner_username}' Local Registry'de bulunamadı. Önce 01_create_test_user.py'i çalıştırın.")
        sys.exit(1)

    print(f"--> [2/2] Consumer Org oluşturuluyor...")
    if not create_consumer_org(org_name, owner_username, owner_url):
        sys.exit(1)

    print("==================================================")


if __name__ == "__main__":
    main()
