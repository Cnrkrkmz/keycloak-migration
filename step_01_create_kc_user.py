#!/usr/bin/env python3.11
"""
# ==============================================================================
# KULLANILMIYOR — Bu dosya artık referans/arşiv amaçlıdır.
# Tüm adımlar migration_steps.py içinde birleştirilmiştir.
# 04_run_migration.py artık bu dosyayı değil, migration_steps.py'yi kullanır.
# ==============================================================================

step_01_create_kc_user.py — Migration Adım 1/4: Keycloak'ta Kullanıcı Oluşturma
=================================================================================
Çalıştıran : 04_run_migration.py (subprocess olarak)
Girdi      : Komut satırından kullanıcı adı (sys.argv[1]) veya interaktif
Çıktı      : migration_users.csv → kc_user_created = true
             migration_env.sh   → KC_TEMP_PASSWORD = <rastgele 16 karakter>

Ne Yapar:
  1. APIC Local Registry'den kullanıcının adını, e-postasını ve isim bilgilerini çeker.
  2. Keycloak Admin API'si üzerinden hedef realm'e (ör. apic-demo) kullanıcıyı ekler.
     - E-posta adresi olarak kullanıcının orijinal adresi kullanılır.
     - 16 karakterlik kriptografik rastgele geçici şifre atanır.
  3. Geçici şifreyi migration_env.sh'a yazar; sonraki adım (step_03) bunu okur.
  4. CSV'de kc_user_created = true olarak işaretler.

Müşteri Ortamında Karşılaşılabilecek Hatalar:
  - "APIC komutu reddedildi / Not found":
      Kullanıcı APIC Local Registry'de değil; UUID gibi bir shadow user adı
      girilmiş olabilir. Keycloak registry'sine ait shadow user'lar APIC Local
      Registry'de bulunmaz.
  - "HTTP 409 / Kullanıcı zaten mevcut":
      Keycloak'ta aynı username ile kayıt var. Script bunu uyarı olarak geçer
      ama geçici şifre yazamaz; step_03 başarısız olur.
      Çözüm: Keycloak'taki mevcut kaydı sil veya step_03'ü atla.
  - "Token alınamadı":
      Keycloak admin credential'ları yanlış ya da Keycloak erişilemiyor.
  - "Keycloak için zorunlu alanlar eksik":
      APIC'teki kullanıcının e-posta adresi boş. Önce APIC'te tamamlanmalı.
"""

import subprocess
import json
import os
import sys
import ssl
import urllib.request
import urllib.parse
import secrets
import string

# Lab/test ortamlarında self-signed sertifika kullanıldığında SSL doğrulaması
# başarısız olur. Üretimde CA-signed sertifika kullanılmalı ve bu blok kaldırılmalı.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

from migration_state import get_user, add_user, update_flag

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

APIC_SERVER          = os.environ.get("APIC_SERVER")
PROV_ORG             = os.environ.get("PROV_ORG")
LOCAL_REGISTRY       = os.environ.get("LOCAL_REGISTRY")
KEYCLOAK_URL         = os.environ.get("KEYCLOAK_URL")
KEYCLOAK_ADMIN_USER  = os.environ.get("KEYCLOAK_ADMIN_USER")
KEYCLOAK_ADMIN_PASS  = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
# TARGET_REALM: Kullanıcıların oluşturulacağı Keycloak realm. migration_env.sh'dan okunur.
TARGET_REALM         = os.environ.get("KEYCLOAK_REALM_NAME", "apic-demo")

if len(sys.argv) > 1:
    DEMO_USERNAME = sys.argv[1]
else:
    DEMO_USERNAME = input("Migrate edilecek APIC Kullanıcı Adı: ").strip()

# Consumer Org bilgisi normalde CSV'den gelir. Script doğrudan çalıştırılırsa
# env'den okur; batch modunda her zaman CSV zaten dolu olacaktır.
CONSUMER_ORG = os.environ.get("CONSUMER_ORG", "")


# ------------------------------------------------------------------------------
# APIC kullanıcısını temsil eden hafif veri sınıfı
# ------------------------------------------------------------------------------

class ApicUser:
    """
    APIC'ten gelen ham JSON verisini alıp yalnızca migration için
    gerekli alanları tutan sade bir nesneye dönüştürür.
    """
    def __init__(self, raw):
        # APIC bazı versiyonlarda snake_case, bazılarında camelCase döndürür;
        # ikisini de destekliyoruz.
        self.username   = raw.get("username") or raw.get("name") or ""
        self.email      = raw.get("email") or ""
        self.first_name = raw.get("first_name") or raw.get("firstName") or ""
        self.last_name  = raw.get("last_name")  or raw.get("lastName")  or ""

    def is_valid(self):
        """Keycloak'ta hesap açmak için en az username ve email gereklidir."""
        return bool(self.username and self.email)


# ------------------------------------------------------------------------------
# APIC'ten kullanıcı verisi çekme
# ------------------------------------------------------------------------------

def get_apic_user(username):
    """
    APIC Local Registry'den kullanıcıyı JSON olarak çeker ve ApicUser nesnesi döndürür.
    Hata durumunda None döner; çağıran sys.exit(1) yapar.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return ApicUser(json.loads(res.stdout))
    except subprocess.CalledProcessError as e:
        # APIC CLI hataları bazen stderr'de, bazen stdout'ta gelir
        print("--> [HATA] APIC'ten kullanıcı alınamadı!")
        print(f"--> [DETAY] {e.stderr.strip() or e.stdout.strip()}")
        return None
    except json.JSONDecodeError:
        print("--> [HATA] APIC yanıtı geçerli JSON değil!")
        return None


# ------------------------------------------------------------------------------
# Keycloak işlemleri
# ------------------------------------------------------------------------------

def get_kc_admin_token():
    """
    Keycloak master realm üzerinden admin-cli ile token alır.
    Bu token Keycloak Admin REST API'sine erişmek için kullanılır.
    """
    url = f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token"
    data = urllib.parse.urlencode({
        "username":   KEYCLOAK_ADMIN_USER,
        "password":   KEYCLOAK_ADMIN_PASS,
        "grant_type": "password",
        "client_id":  "admin-cli",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            return json.loads(resp.read().decode()).get("access_token")
    except Exception as e:
        print(f"--> [HATA] Keycloak admin token alınamadı: {e}")
        return None


def create_kc_user(token, user_obj):
    """
    Keycloak Admin API'si ile kullanıcıyı hedef realm'e ekler.

    Geçici şifre: 16 karakterli, harf+rakam karışımı, kriptografik olarak üretilir.
    Şifre temporary=False olarak atanır; kullanıcı ilk girişte değiştirmeye
    zorlanmak isteniyorsa True yapılmalıdır (önerilen).

    Döndürür: Başarıda geçici şifre string'i, başarısızlıkta None.
    """
    url = f"{KEYCLOAK_URL}/admin/realms/{TARGET_REALM}/users"

    alphabet  = string.ascii_letters + string.digits
    temp_pass = "".join(secrets.choice(alphabet) for _ in range(16))

    payload = {
        "username":      user_obj.username,
        "enabled":       True,
        "emailVerified": True,
        "email":         user_obj.email,
        "firstName":     user_obj.first_name,
        "lastName":      user_obj.last_name,
        # temporary=True yapılırsa kullanıcı ilk girişte şifre değiştirmek zorunda kalır
        "credentials": [{"type": "password", "value": temp_pass, "temporary": False}],
    }

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            if resp.status == 201:
                print(f"--> [BAŞARILI] '{user_obj.username}' Keycloak'ta oluşturuldu.")
                return temp_pass
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # 409 Conflict: Keycloak'ta bu username zaten kayıtlı.
            # Bu genellikle önceki yarım kalan bir migration denemesinden kalır.
            # Script uyarı verir ama geçici şifre döndüremez; step_03 başarısız olur.
            print(f"--> [UYARI] '{user_obj.username}' Keycloak'ta zaten mevcut (HTTP 409).")
            print(f"    Çözüm: Keycloak admin panelinden kullanıcıyı silin, sonra tekrar deneyin.")
        else:
            print(f"--> [HATA] Kullanıcı oluşturulamadı (HTTP {e.code}): {e.read().decode()}")
    return None


def save_temp_password(username, temp_pass):
    """
    Geçici şifreyi migration_env.sh'a yazar; step_03_jit_provision.py bu
    değeri okuyarak APIC'e login eder.

    ÖNEMLİ: Bu değer başarılı JIT provision'ın ardından clear_temp_password()
    ile dosyadan silinmelidir. Başarısız senaryolarda dosyada kalabilir —
    bu bir güvenlik riskidir; migration_env.sh izinleri 600 olmalıdır.
    """
    key  = "KC_TEMP_PASSWORD"
    line = f'export {key}="{temp_pass}"\n'

    with open(ENV_FILE, "r") as f:
        lines = f.readlines()

    updated = False
    for i, l in enumerate(lines):
        if l.startswith(f"export {key}="):
            lines[i] = line
            updated   = True
            break
    if not updated:
        lines.append(line)

    with open(ENV_FILE, "w") as f:
        f.writelines(lines)

    print(f"--> [BİLGİ] Geçici şifre migration_env.sh'a kaydedildi (step_03 tarafından kullanılacak).")


# ------------------------------------------------------------------------------
# Ana akış
# ------------------------------------------------------------------------------

def main():
    # Idempotency kontrolü: bu adım daha önce başarıyla tamamlandıysa atla.
    csv_row = get_user(DEMO_USERNAME)
    if csv_row and csv_row.get("kc_user_created", "false").lower() == "true":
        print(f"--> [BİLGİ] '{DEMO_USERNAME}' zaten Keycloak'ta (kc_user_created=true). Atlanıyor.")
        return

    print(f"\n--> [1/3] APIC'ten '{DEMO_USERNAME}' kullanıcısı okunuyor...")
    user_obj = get_apic_user(DEMO_USERNAME)
    if not user_obj:
        sys.exit(1)

    if not user_obj.is_valid():
        print(f"--> [HATA] Kullanıcının e-posta veya kullanıcı adı boş!")
        print(f"    Keycloak'ta hesap açabilmek için her ikisi de zorunludur.")
        sys.exit(1)

    # Eğer CSV'de henüz bu kullanıcı yoksa (script ilk kez çalışıyor) kaydı oluştur.
    # target_email olarak APIC'teki mevcut e-posta kullanılır; bu adres Keycloak'a yazılır.
    if not csv_row:
        add_user(DEMO_USERNAME, CONSUMER_ORG, user_obj.email)

    print("--> [2/3] Keycloak admin token alınıyor...")
    token = get_kc_admin_token()
    if not token:
        sys.exit(1)

    print(f"--> [3/3] '{DEMO_USERNAME}' Keycloak'a yazılıyor...")
    temp_pass = create_kc_user(token, user_obj)
    if temp_pass:
        update_flag(DEMO_USERNAME, "kc_user_created", True)
        save_temp_password(DEMO_USERNAME, temp_pass)
    else:
        # create_kc_user başarısız oldu; hata mesajı zaten basıldı.
        sys.exit(1)

    print("==================================================")


if __name__ == "__main__":
    main()
