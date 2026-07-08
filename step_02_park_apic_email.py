#!/usr/bin/env python3.11
"""
# ==============================================================================
# KULLANILMIYOR — Bu dosya artık referans/arşiv amaçlıdır.
# Tüm adımlar migration_steps.py içinde birleştirilmiştir.
# 04_run_migration.py artık bu dosyayı değil, migration_steps.py'yi kullanır.
# ==============================================================================

step_02_park_apic_email.py — Migration Adım 2/4: APIC E-postasını Geçici Olarak Park Et
=========================================================================================
Çalıştıran : 04_run_migration.py (subprocess olarak)
Girdi      : Komut satırından kullanıcı adı (sys.argv[1]) veya interaktif
Çıktı      : APIC Local Registry → kullanıcının e-postası <adres>-old@<domain> yapılır
             migration_users.csv → apic_email_parked = true
                                   source_email = <yeni -old@ adresi>

Neden Bu Adım Gerekli:
  Keycloak'ta orijinal e-posta ile yeni hesap açıldı (adım 1). APIC'te eski
  Local Registry kullanıcısı hâlâ aynı e-posta ile kayıtlı. APIC JIT provision
  sırasında (adım 3) iki farklı kayıt aynı e-postaya sahip olursa çakışma olur.
  Bu adım APIC'teki eski kaydın e-postasını geçici olarak "<adres>-old@<domain>"
  yaparak çakışmayı önler.

  Migration tamamlandıktan sonra eski Local Registry kullanıcısı silinmeli ya da
  bu "park" hali kalıcı bırakılmalıdır. Rollback durumunda e-posta geri alınır.

Müşteri Ortamında Karşılaşılabilecek Hatalar:
  - "APIC kullanıcısı okunamadı":
      Kullanıcı APIC Local Registry'de yok veya erişim yetkisi yok.
      APIC CLI session'ının aktif olduğunu doğrulayın (00_setup_env.py'yi tekrar çalıştırın).
  - "APIC güncelleme reddedildi":
      E-posta formatı geçersiz, kullanıcı başka bir registry'de tanımlı,
      veya kullanıcı "read-only" (LDAP sync gibi dış kaynaktan geliyor).
      Dış kaynaklı kullanıcılar bu adımla güncellenemez.
  - "Doğrulama başarısız":
      Güncelleme komutu hata vermedi ama APIC e-postayı yazmadı.
      Genellikle APIC'in içeride tuttuğu bir kısıtlama veya validation hatası.
      APIC yöneticisiyle iletişime geçin.
  - "E-posta zaten park edilmiş":
      Önceki bir denemeden kalan durum. Script bunu atlar; idempotent davranış.
"""

import subprocess
import json
import os
import sys

from migration_state import get_user, update_flag, update_source_email

ENV_FILE = "migration_env.sh"


def load_env():
    """migration_env.sh dosyasını okuyup os.environ'a yükler."""
    if not os.path.exists(ENV_FILE):
        print(f"--> [HATA] '{ENV_FILE}' bulunamadı! Lütfen önce 00_setup_env.py'yi çalıştırın.")
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
LOCAL_REGISTRY = os.environ.get("LOCAL_REGISTRY")

if len(sys.argv) > 1:
    TARGET_USERNAME = sys.argv[1]
else:
    TARGET_USERNAME = input("E-postası park edilecek APIC Kullanıcı Adı: ").strip()


# ------------------------------------------------------------------------------
# APIC işlemleri
# ------------------------------------------------------------------------------

def get_current_user_data(username):
    """
    APIC Local Registry'den kullanıcının güncel bilgilerini çeker.
    E-posta zaten -old@ içerip içermediğini ve first/last name'i buradan öğreniriz.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout)
    except subprocess.CalledProcessError as e:
        print(f"--> [HATA] APIC kullanıcısı okunamadı: {e.stderr.strip() or e.stdout.strip()}")
        return None
    except json.JSONDecodeError:
        print("--> [HATA] APIC yanıtı geçerli JSON değil!")
        return None


def update_user_email(username, current_data, new_email):
    """
    APIC Local Registry'de kullanıcının e-postasını günceller.
    first_name ve last_name zorunlu olmasa da mevcut değerler korunarak
    gönderilir; APIC bu alanları boş gönderince sıfırlayabilir.
    """
    first_name = current_data.get("first_name") or current_data.get("firstName") or ""
    last_name  = current_data.get("last_name")  or current_data.get("lastName")  or ""

    yaml_content = f"""email: {new_email}
first_name: {first_name}
last_name: {last_name}
title: {username}
"""
    cmd = [
        "apic", "users:update", username, "-",
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print("--> [HATA] APIC güncelleme reddedildi!")
        print(f"--> [DETAY] {e.stderr.strip() or e.stdout.strip()}")
        return False


# ------------------------------------------------------------------------------
# Ana akış
# ------------------------------------------------------------------------------

def main():
    # Idempotency kontrolü: bu adım daha önce başarıyla tamamlandıysa atla.
    csv_row = get_user(TARGET_USERNAME)
    if not csv_row:
        print(f"--> [HATA] '{TARGET_USERNAME}' CSV'de bulunamadı. Önce 03_export_consumer_orgs.py'yi çalıştırın.")
        sys.exit(1)

    if csv_row.get("apic_email_parked", "false").lower() == "true":
        print(f"--> [BİLGİ] '{TARGET_USERNAME}' e-postası zaten park edilmiş. Atlanıyor.")
        return

    print(f"\n--> [1/3] '{TARGET_USERNAME}' kullanıcısının APIC bilgileri okunuyor...")
    current_data = get_current_user_data(TARGET_USERNAME)
    if not current_data:
        sys.exit(1)

    old_email = current_data.get("email", "")
    if not old_email or "@" not in old_email:
        print(f"--> [HATA] Geçerli e-posta adresi bulunamadı (mevcut: '{old_email}').")
        sys.exit(1)

    # Güvenlik: e-posta zaten -old@ ile bitiyorsa tekrar ekleme.
    # Bu durum normalde yaşanmaz (flag kontrolü yukarıda); ancak CSV bozuk olduğunda
    # veya --force ile rollback sonrası tekrar çalıştırılırsa karşılaşılabilir.
    if old_email.split("@")[0].endswith("-old"):
        print(f"--> [BİLGİ] E-posta zaten park edilmiş durumda ({old_email}). Tekrar eklenmeyecek.")
        new_email = old_email
    else:
        parts     = old_email.split("@")
        new_email = f"{parts[0]}-old@{parts[1]}"

    print(f"--> APIC'teki mevcut e-posta : {old_email}")
    print(f"--> Park edilecek e-posta    : {new_email}")

    print("\n--> [2/3] APIC Local Registry güncelleniyor...")
    if not update_user_email(TARGET_USERNAME, current_data, new_email):
        sys.exit(1)

    print("--> [3/3] Değişiklik APIC'ten okunarak doğrulanıyor...")
    verify = get_current_user_data(TARGET_USERNAME)
    if verify and verify.get("email") == new_email:
        print("--> [BAŞARILI] E-posta değişimi doğrulandı!")
        update_flag(TARGET_USERNAME, "apic_email_parked", True)
        # source_email sütununa park edilmiş adresi yaz (rollback için referans)
        update_source_email(TARGET_USERNAME, new_email)
    else:
        print("--> [UYARI] Güncelleme komutu çalıştı ancak APIC doğrulaması başarısız oldu.")
        print("    APIC üzerinde manuel kontrol yapın.")

    print("==================================================")


if __name__ == "__main__":
    main()
