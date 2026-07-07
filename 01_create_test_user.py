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

APIC_SERVER    = os.environ.get("APIC_SERVER")
PROV_ORG       = os.environ.get("PROV_ORG")
LOCAL_REGISTRY = os.environ.get("LOCAL_REGISTRY", "sandbox-catalog")


def prompt_user_details():
    """Yeni kullanıcı bilgilerini interaktif olarak toplar."""
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
    """Kullanıcıyı Local Registry üzerinde yaratır."""
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
    username, email, first_name, last_name, password = prompt_user_details()

    print("\n--> İşlem başlatılıyor...")
    if not create_global_user(username, email, first_name, last_name, password):
        sys.exit(1)

    print("==================================================")

if __name__ == "__main__":
    main()
