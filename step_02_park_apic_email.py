#!/usr/bin/env python3.11
import subprocess
import json
import os
import sys

from migration_state import get_user, update_flag, update_source_email

# ==============================================================================
# step_02_park_apic_email.py — APIC e-postasını -old suffix ile park et
# Çalıştıran: 04_run_migration.py (adım 2/4)
# ==============================================================================

ENV_FILE = "migration_env.sh"

def load_env():
    """migration_env.sh dosyasını okuyup ortama yükler."""
    if not os.path.exists(ENV_FILE):
        print(f"--> [HATA] '{ENV_FILE}' bulunamadı! Lütfen önce kurulum scriptini çalıştırın.")
        sys.exit(1)

    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                key_value = line[7:].split("=", 1)
                if len(key_value) == 2:
                    os.environ[key_value[0]] = key_value[1].strip('"\'')

load_env()

APIC_SERVER = os.environ.get("APIC_SERVER")
PROV_ORG = os.environ.get("PROV_ORG")
LOCAL_REGISTRY = os.environ.get("LOCAL_REGISTRY")

if len(sys.argv) > 1:
    TARGET_USERNAME = sys.argv[1]
else:
    TARGET_USERNAME = input("E-postası güncellenecek APIC Kullanıcı Adı: ").strip()


def get_current_user_data(username):
    """Kullanıcının mevcut verilerini okur."""
    cmd = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json", "--output", "-"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout)
    except subprocess.CalledProcessError as e:
        print(f"--> [HATA] APIC kullanıcısı okunamadı! {e.stderr.strip() if e.stderr else e.stdout.strip()}")
        return None
    except json.JSONDecodeError:
        print("--> [HATA] APIC'ten dönen yanıt geçerli JSON değil!")
        return None

def update_user_email(username, current_data, new_email):
    """APIC üzerindeki kullanıcının e-postasını STDIN üzerinden günceller."""
    first_name = current_data.get("first_name") or current_data.get("firstName") or ""
    last_name = current_data.get("last_name") or current_data.get("lastName") or ""

    yaml_content = f"""email: {new_email}
first_name: {first_name}
last_name: {last_name}
title: {username}
"""

    cmd = [
        "apic", "users:update", username, "-",
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY
    ]

    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print("--> [HATA] APIC güncelleme reddedildi!")
        print(f"--> [DETAY] {e.stderr.strip() if e.stderr else e.stdout.strip()}")
        return False

def main():
    # email_updated=true ise bu adım zaten tamamlanmış demektir
    csv_row = get_user(TARGET_USERNAME)
    if not csv_row:
        print(f"--> [HATA] '{TARGET_USERNAME}' CSV'de bulunamadı. Önce 03 scriptini çalıştırın.")
        sys.exit(1)

    if csv_row.get("apic_email_parked", "false").lower() == "true":
        print(f"--> [BİLGİ] '{TARGET_USERNAME}' e-posta zaten güncellendi (apic_email_parked=true). Atlanıyor.")
        print("==================================================")
        return

    print(f"\n--> [1/3] '{TARGET_USERNAME}' kullanıcısının mevcut bilgileri çekiliyor...")
    current_data = get_current_user_data(TARGET_USERNAME)

    if not current_data:
        sys.exit(1)

    old_email = current_data.get("email", "")

    if not old_email or "@" not in old_email:
        print(f"--> [HATA] Kullanıcının geçerli bir e-posta adresi bulunamadı! (Mevcut değer: '{old_email}')")
        sys.exit(1)

    # Otomatik '-old' ekleme mantığı — zaten park edildiyse tekrar ekleme
    if old_email.split("@")[0].endswith("-old"):
        print(f"--> [BİLGİ] E-posta zaten park edilmiş durumda. Tekrar eklenmeyecek.")
        new_email = old_email
    else:
        email_parts = old_email.split("@")
        new_email = f"{email_parts[0]}-old@{email_parts[1]}"

    print(f"--> E-posta (source) : {old_email}")
    print(f"--> E-posta (target) : {new_email}")

    print("\n--> [2/3] APIC Local Registry (in-memory) güncelleniyor...")
    success = update_user_email(TARGET_USERNAME, current_data, new_email)

    if success:
        print("--> [3/3] Değişiklik doğrudan STDOUT üzerinden doğrulanıyor...")
        verify_data = get_current_user_data(TARGET_USERNAME)
        if verify_data and verify_data.get("email") == new_email:
            print("--> [BAŞARILI] E-posta değişimi APIC üzerinden %100 doğrulandı!")
            update_flag(TARGET_USERNAME, "apic_email_parked", True)
            update_source_email(TARGET_USERNAME, new_email)   # canlı CSV güncelleme
        else:
            print("--> [UYARI] Güncelleme komutu çalıştı ancak doğrulama başarısız oldu.")

    print("==================================================")

if __name__ == "__main__":
    main()