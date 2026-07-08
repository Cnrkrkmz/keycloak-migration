#!/usr/bin/env python3.11
import subprocess
import json
import os
import sys

# ==============================================================================
# APIC TEST KULLANICISI OLUŞTURMA SCRİPTİ (LOCAL REGISTRY)
# ==============================================================================
# Çalıştırma sırası: 01 (test ortamı hazırlığı — migration öncesi)
#
# Kullanıcıyı sadece Local Registry'ye (global havuz) ekler.
# Consumer Org oluşturmak ve bu kullanıcıyı owner olarak atamak için
# 00_create_consumer_org.py'i kullanın.
# ==============================================================================

ENV_FILE = "migration_env.sh"

def load_env():
    """
    migration_env.sh dosyasını okuyarak içindeki 'export KEY="VALUE"'
    satırlarını os.environ'a yükler. Dosya yoksa hata verip çıkar.
    Bu sayede 00_setup_env.py'nin kaydettiği tüm değişkenler
    bu scriptte kullanılabilir hale gelir.
    """
    if not os.path.exists(ENV_FILE):
        print(f"--> [HATA] '{ENV_FILE}' bulunamadı! Önce 00_setup_env.py'i çalıştırın.")
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
    Yeni kullanıcı için username, email, ad, soyad ve şifre bilgilerini
    interaktif olarak kullanıcıdan alır ve tuple olarak döndürür.
    Bu değerler create_global_user() fonksiyonuna aktarılır.
    """
    print("\n==================================================")
    print("      YENİ APIC TEST KULLANICISI OLUŞTURMA        ")
    print("==================================================")
    username   = input("Kullanıcı Adı (Username - Boşluksuz): ").strip()
    email      = input("E-posta Adresi (Email): ").strip()
    first_name = input("Adı (First Name): ").strip()
    last_name  = input("Soyadı (Last Name): ").strip()
    password   = input("Şifre (Password): ").strip()
    return username, email, first_name, last_name, password


def create_global_user(username, email, first_name, last_name, password):
    """
    Verilen bilgilerle APIC Local Registry'de yeni bir kullanıcı oluşturur.
    APIC CLI'ye YAML formatında girdi verilir (stdin üzerinden).
    Kullanıcı sadece global kullanıcı havuzuna eklenir; herhangi bir
    Consumer Org'a atanmaz. Org ataması için 02_create_consumer_org.py kullanılır.
    Başarılı olursa True, hata olursa False döner.
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
        print(f"--> [BAŞARILI] '{username}' kullanıcısı '{LOCAL_REGISTRY}' registry'sinde oluşturuldu.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"--> [HATA] Kullanıcı oluşturulamadı:\n{e.stderr.strip() or e.stdout.strip()}")
        return False


def main():
    """
    prompt_user_details() ile bilgileri toplar,
    ardından create_global_user() ile kullanıcıyı oluşturur.
    Hata olursa çıkış kodu 1 ile sonlanır.
    """
    username, email, first_name, last_name, password = prompt_user_details()

    print("\n--> İşlem başlatılıyor...")
    if not create_global_user(username, email, first_name, last_name, password):
        sys.exit(1)

    print("==================================================")

if __name__ == "__main__":
    main()