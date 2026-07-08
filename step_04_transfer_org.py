#!/usr/bin/env python3.11
"""
# ==============================================================================
# KULLANILMIYOR — Bu dosya artık referans/arşiv amaçlıdır.
# Tüm adımlar migration_steps.py içinde birleştirilmiştir.
# 04_run_migration.py artık bu dosyayı değil, migration_steps.py'yi kullanır.
# ==============================================================================

step_04_transfer_org.py — Migration Adım 4/4: Consumer Org Sahipliğini Devret
==============================================================================
Çalıştıran : 04_run_migration.py (subprocess olarak)
Girdi      : Komut satırından kullanıcı adı (sys.argv[1]) veya interaktif
Çıktı      : APIC Consumer Org → owner, Local Registry kullanıcısından
             Keycloak shadow user'a devredilir (--cascade ile App ve
             Subscription'lar da yeni owner'a geçer)
             migration_users.csv → org_owner_xfrd = true, migrated = true

Ne Yapar:
  adım 3'te APIC'te oluşturulan Keycloak shadow user'ı, consumer org'a üye
  olarak eklenir ve ardından org'un owner'ı yapılır.

  Adımlar:
    1. CSV'den consumer_org adını okur.
    2. APIC'teki Local Registry kullanıcısının mevcut e-postasından
       (varsa -old@ temizleyerek) asıl e-postayı hesaplar.
    3. Bu e-postayı kullanarak Keycloak registry'sinde shadow user'ı bulur.
    4. Shadow user'ı consumer org'a üye olarak ekler.
    5. consumer-orgs:transfer-owner --cascade komutu ile sahipliği devreder.

Müşteri Ortamında Karşılaşılabilecek Hatalar:
  - "consumer_org CSV'de yok":
      03_export_consumer_orgs.py düzgün çalışmamış veya CSV elle düzenlenmiş.
  - "Shadow user bulunamadı":
      step_03 başarısız olmuş; APIC'te Keycloak shadow user kaydı açılmamış.
      Önce step_03'ü tamamlayın.
  - "Üye ekleme başarısız":
      Shadow user URL'si geçersiz ya da org erişim kısıtlaması var.
  - "Member URL alınamadı":
      Üye eklendi ama listede görünmüyor; e-posta eşleşmesi başarısız.
      get_member_url'in beklediği e-posta ile shadow user'ın e-postasını karşılaştırın.
  - "Sahiplik devri başarısız":
      --cascade parametresi bazı APIC versiyonlarında desteklenmeyebilir.
      APIC sürümünü ve kullanıcı yetkilerini kontrol edin.

ÖNEMLİ — Geri Alınamaz İşlem:
  --cascade ile yapılan sahiplik devri şu anda rollback scripti tarafından
  geri alınmamaktadır. Bu adım başarısız olan senaryolarda APIC admin
  panelinden manuel devir gerekebilir.
"""

import subprocess
import json
import os
import sys

from migration_state import get_user, update_flag, mark_migrated

ENV_FILE = "migration_env.sh"


def load_env():
    """migration_env.sh dosyasını okuyup os.environ'a yükler."""
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
# KEYCLOAK_REGISTRY: APIC tarafında tanımlı Keycloak user registry'nin adı
KEYCLOAK_REGISTRY = os.environ.get("KEYCLOAK_REGISTRY_NAME", "keycluk")

if len(sys.argv) > 1:
    TARGET_USERNAME = sys.argv[1]
else:
    TARGET_USERNAME = input("Sahipliği devredilecek kullanıcı adı: ").strip()


# ------------------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ------------------------------------------------------------------------------

def get_consumer_org_for_user(username):
    """CSV'den kullanıcının bağlı olduğu consumer org adını okur."""
    row = get_user(username)
    return row.get("consumer_org", "") if row else ""


def get_target_email(username):
    """
    APIC Local Registry'den kullanıcının e-postasını okur.
    Bu aşamada e-posta -old@ formatında olabilir (step_02 tarafından park edildi);
    -old@ kısmını kaldırarak Keycloak'taki gerçek e-postayı döndürür.
    Shadow user Keycloak'ta orijinal e-posta ile oluşturulduğundan bu eşleştirme kritiktir.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        email = json.loads(res.stdout).get("email", "")
        if "-old@" in email:
            email = email.replace("-old@", "@")
        return email
    except Exception as e:
        print(f"--> [HATA] APIC'ten e-posta alınamadı: {e}")
        return None


def get_shadow_user_url(consumer_org, expected_email):
    """
    APIC'teki Keycloak registry'sinde e-posta adresiyle shadow user'ı arar
    ve APIC URL'sini döndürür. Bu URL, üye ekleme ve sahiplik devri için gereklidir.
    """
    cmd = [
        "apic", "users:list",
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", KEYCLOAK_REGISTRY,
        "--format", "json", "--output", "-",
    ]
    try:
        res  = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        items = data.get("results", data if isinstance(data, list) else [data])
        for u in items:
            if u.get("email") == expected_email:
                return u.get("url")
        return None
    except Exception as e:
        print(f"--> [HATA] Keycloak registry listesi okunamadı: {e}")
        return None


def add_kc_user_as_member(consumer_org, username, user_url):
    """
    Shadow user'ı consumer org'a üye olarak ekler.
    Sahiplik devri yapılabilmesi için kullanıcının önce org'un üyesi olması şarttır.
    Zaten üyeyse idempotent davranır.
    """
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
            print(f"--> [BAŞARILI] Shadow user '{consumer_org}' org'una üye eklendi.")
            return True
        if "already exists" in (res.stderr + res.stdout).lower():
            print("--> [BİLGİ] Shadow user zaten bu org'un üyesi.")
            return True
        print(f"--> [HATA] Üye ekleme başarısız:\n{res.stderr.strip() or res.stdout.strip()}")
        return False
    except Exception as e:
        print(f"--> [HATA] Üye ekleme sırasında beklenmeyen hata: {e}")
        return False


def get_member_url(consumer_org, expected_email):
    """
    Consumer org üye listesinden shadow user'ın member URL'sini döndürür.
    transfer-owner komutu kullanıcı URL'sini değil member URL'sini bekler.
    """
    cmd = [
        "apic", "members:list", "--scope", "consumer-org",
        "-s", APIC_SERVER, "-o", PROV_ORG, "-c", CATALOG,
        "--consumer-org", consumer_org,
        "--format", "json", "--output", "-",
    ]
    try:
        res  = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        items = data.get("results", data if isinstance(data, list) else [data])
        for m in items:
            if m.get("user", {}).get("email") == expected_email:
                return m.get("url")
        return None
    except Exception as e:
        print(f"--> [HATA] Üye listesi okunamadı: {e}")
        return None


def transfer_ownership(consumer_org, member_url):
    """
    consumer-orgs:transfer-owner --cascade komutu ile sahipliği devreder.
    --cascade: Consumer Org altındaki tüm App ve Subscription kayıtlarının
    owner'ı da yeni kullanıcıya güncellenir. Geri alınamaz bir işlemdir.
    """
    yaml_content = f"new_owner_member_url: {member_url}\n"
    cmd = [
        "apic", "consumer-orgs:transfer-owner", consumer_org, "-",
        "-s", APIC_SERVER, "-o", PROV_ORG, "-c", CATALOG,
        "--cascade",
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        print(f"--> [BAŞARILI] '{consumer_org}' sahipliği Keycloak profiline devredildi.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"--> [HATA] Sahiplik devri başarısız:\n{e.stderr.strip() or e.stdout.strip()}")
        return False


# ------------------------------------------------------------------------------
# Ana akış
# ------------------------------------------------------------------------------

def main():
    csv_row = get_user(TARGET_USERNAME)
    if not csv_row:
        print(f"--> [HATA] '{TARGET_USERNAME}' CSV'de bulunamadı.")
        sys.exit(1)

    # Idempotency: bu kullanıcı daha önce tamamen tamamlandıysa atla
    if csv_row.get("migrated", "false").lower() == "true":
        print(f"--> [BİLGİ] '{TARGET_USERNAME}' zaten migrate edilmiş. Atlanıyor.")
        return

    consumer_org = csv_row.get("consumer_org", "")
    if not consumer_org:
        print(f"--> [HATA] '{TARGET_USERNAME}' için CSV'de consumer_org bilgisi yok.")
        sys.exit(1)

    print(f"\n--> [1/4] '{TARGET_USERNAME}' için hedef e-posta hesaplanıyor...")
    expected_email = get_target_email(TARGET_USERNAME)
    if not expected_email:
        sys.exit(1)
    print(f"--> [BİLGİ] Keycloak'taki e-posta: {expected_email}")

    print(f"--> [2/4] APIC Keycloak registry'sinde shadow user aranıyor...")
    user_url = get_shadow_user_url(consumer_org, expected_email)
    if not user_url:
        print(f"--> [HATA] '{expected_email}' için shadow user bulunamadı.")
        print(f"    step_03_jit_provision.py'nin başarıyla tamamlandığını doğrulayın.")
        sys.exit(1)

    print(f"--> [3/4] Shadow user '{consumer_org}' org'una üye ekleniyor...")
    if not add_kc_user_as_member(consumer_org, TARGET_USERNAME, user_url):
        sys.exit(1)

    print(f"--> [4/4] Consumer org sahipliği devrediliyor (--cascade)...")
    member_url = get_member_url(consumer_org, expected_email)
    if not member_url:
        print("--> [HATA] Member URL alınamadı. Üye listesini manuel kontrol edin.")
        sys.exit(1)

    if not transfer_ownership(consumer_org, member_url):
        sys.exit(1)

    update_flag(TARGET_USERNAME, "org_owner_xfrd", True)
    mark_migrated(TARGET_USERNAME)

    print("==================================================")
    print(f"[TAMAMLANDI] '{consumer_org}' → owner artık Keycloak profili.")
    print("==================================================")


if __name__ == "__main__":
    main()
