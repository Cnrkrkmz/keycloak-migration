#!/usr/bin/env python3.11
"""
# ==============================================================================
# KULLANILMIYOR — Bu dosya artık referans/arşiv amaçlıdır.
# Tüm adımlar migration_steps.py içinde birleştirilmiştir.
# 04_run_migration.py artık bu dosyayı değil, migration_steps.py'yi kullanır.
# ==============================================================================

step_03_jit_provision.py — Migration Adım 3/4: APIC'te Shadow User JIT Provision
==================================================================================
Çalıştıran : 04_run_migration.py (subprocess olarak)
Girdi      : Komut satırından kullanıcı adı (sys.argv[1]) veya interaktif
             migration_env.sh → KC_TEMP_PASSWORD (step_01 tarafından yazılır)
Çıktı      : APIC Keycloak Registry → shadow user kaydı oluşur (JIT)
             migration_users.csv → apic_jit_done = true, migrated = true
             migration_env.sh   → KC_TEMP_PASSWORD satırı silinir

Ne Yapar — JIT Provision Mekanizması:
  APIC, Keycloak ile OIDC federation kurulmuş bir user registry'ye sahipse,
  o registry üzerinden ilk kez giriş yapan kullanıcıyı otomatik olarak kendi
  veritabanına "shadow user" olarak ekler. Bu işlem JIT (Just-In-Time)
  Provisioning olarak adlandırılır.

  Bu adımda:
  1. Keycloak'ta (adım 1'de oluşturulmuş) kullanıcı adına APIC'in consumer
     token endpoint'ine doğrudan "password" grant ile login isteği atılır.
  2. APIC bu isteği doğrular, Keycloak'tan kullanıcı bilgilerini alır ve kendi
     veritabanında shadow user kaydını açar.
  3. Bu noktadan itibaren kullanıcı APIC'e Keycloak credentials ile giriş yapabilir.

  NOT: Bu adım, APIC'in "consumer" endpoint'ini kullandığı için provider org /
  catalog bilgisi de realm string'ine dahil edilir:
  consumer:<prov_org>:<catalog>/<keycloak_registry>

Müşteri Ortamında Karşılaşılabilecek Hatalar:
  - "KC_TEMP_PASSWORD bulunamadı":
      step_01 bu çalıştırmada başarısız olmuş ya da daha önce 409 ile atlanmış.
      Keycloak'ta kullanıcı var ama şifre kaydedilmemiş. Keycloak admin
      panelinden yeni şifre belirleyip migration_env.sh'a elle yazabilirsiniz.
  - "client_id / client_secret okunamadı":
      credentials.json dosyasında 'consumer_toolkit' bölümü yok veya dosya yolu
      migration_env.sh'ta yanlış. Dosyayı kontrol edin.
  - "HTTP 401 / Unauthorized":
      APIC client credential'ları geçersiz veya süresi dolmuş.
  - "HTTP 400":
      Realm string formatı yanlış (consumer:<org>:<catalog>/<registry>).
      PROV_ORG, CATALOG, KEYCLOAK_REGISTRY_NAME değerlerini doğrulayın.
  - "SSL: CERTIFICATE_VERIFY_FAILED":
      Prod ortamda self-signed sertifika var. CA bundle'ı güncelleyin.
  - "İstek sırasında hata / connection refused":
      APIC sunucusu erişilemiyor ya da URL yanlış.
"""

import json
import os
import sys
import ssl
import urllib.request
import urllib.error

# Lab/test ortamı için SSL doğrulaması devre dışı.
# Üretimde: _SSL_CTX = ssl.create_default_context(cafile="/path/to/ca-bundle.crt")
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE

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
CATALOG           = os.environ.get("CATALOG", "sandbox")
# KEYCLOAK_REGISTRY: APIC'teki Keycloak user registry'nin adı (APIC tarafında tanımlı)
KEYCLOAK_REGISTRY = os.environ.get("KEYCLOAK_REGISTRY_NAME", "keycluk")

if len(sys.argv) > 1:
    TARGET_USERNAME = sys.argv[1]
else:
    TARGET_USERNAME = input("APIC'te shadow user açılacak kullanıcı adı: ").strip()


# ------------------------------------------------------------------------------
# HTTP yardımcısı
# ------------------------------------------------------------------------------

def _http(url, *, data=None, method=None, headers=None):
    """
    Minimal urllib wrapper. (status_code, parsed_body) döndürür.
    Non-2xx için urllib.error.HTTPError fırlatır.
    """
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
        body = resp.read().decode()
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body


# ------------------------------------------------------------------------------
# APIC consumer token endpoint'e password grant ile login
# ------------------------------------------------------------------------------

def trigger_real_apic_login(username, password):
    """
    APIC'in /api/token (veya consumer_toolkit endpoint'i) üzerine
    "password" grant type ile login isteği atar.

    APIC bu isteği aldığında:
      1. Keycloak'a username/password ile doğrulama yapar.
      2. Kullanıcının Keycloak'ta var olduğunu onaylar.
      3. Kendi veritabanında bu kullanıcı için shadow user kaydını açar (JIT).
      4. Bir APIC access token döndürür.

    Realm formatı: consumer:<prov_org>:<catalog>/<keycloak_registry_adi>
    Bu format APIC'e hangi catalog'daki hangi OIDC registry'nin kullanılacağını söyler.
    """
    # Varsayılan endpoint; credentials.json'da consumer_toolkit.endpoint varsa o kullanılır
    url = f"{APIC_SERVER}/api/token"

    creds_file         = os.environ.get("APIC_CLIENT_CREDS", "")
    apic_client_id     = ""
    apic_client_secret = ""

    if creds_file and os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                creds = json.load(f)

            # credentials.json yapısı: {"consumer_toolkit": {"endpoint": ..., "client_id": ..., ...}}
            # Bazı APIC versiyonlarında "toolkit" anahtarı kullanılır
            toolkit = creds.get("consumer_toolkit") or creds.get("toolkit", {})

            if "endpoint" in toolkit:
                # consumer_toolkit'te özel bir endpoint tanımlanmışsa onu kullan
                url = f"{toolkit['endpoint']}/token"

            apic_client_id     = toolkit.get("client_id")     or creds.get("client_id", "")
            apic_client_secret = toolkit.get("client_secret") or creds.get("client_secret", "")

        except Exception as e:
            print(f"--> [UYARI] credentials.json okunamadı: {e}")

    if not apic_client_id or not apic_client_secret:
        print(f"--> [HATA] client_id veya client_secret bulunamadı!")
        print(f"    credentials.json dosyasını ve APIC_CLIENT_CREDS yolunu kontrol edin.")
        return False

    # consumer: prefix'i APIC'e bunun bir consumer (developer portal) isteği olduğunu bildirir
    realm_str = f"consumer:{PROV_ORG}:{CATALOG}/{KEYCLOAK_REGISTRY}"

    payload = json.dumps({
        "realm":         realm_str,
        "grant_type":    "password",
        "username":      username,
        "password":      password,
        "client_id":     apic_client_id,
        "client_secret": apic_client_secret,
    }).encode("utf-8")

    try:
        status, body = _http(
            url,
            data=payload,
            headers={
                "Content-Type":            "application/json",
                "Accept":                  "application/json",
                # X-IBM-Consumer-Context: APIC'in hangi org/catalog context'inde
                # işlem yapılacağını belirler
                "X-IBM-Consumer-Context":  f"{PROV_ORG}.{CATALOG}",
            },
        )
        if status == 200:
            print("--> [BAŞARILI] APIC login tamamlandı. Shadow user oluşturuldu (JIT provision).")
            return True
        else:
            print(f"--> [HATA] APIC login başarısız (HTTP {status}): {body}")
            return False

    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"--> [HATA] APIC login reddedildi (HTTP {e.code}): {err}")
        return False
    except Exception as e:
        print(f"--> [HATA] İstek sırasında beklenmeyen hata: {e}")
        return False


# ------------------------------------------------------------------------------
# Temizlik
# ------------------------------------------------------------------------------

def clear_temp_password():
    """
    migration_env.sh'dan KC_TEMP_PASSWORD satırını siler.
    Bu satır kullanıcının geçici şifresini içerdiğinden, JIT provision
    tamamlanır tamamlanmaz dosyadan kaldırılmalıdır.
    """
    key = "KC_TEMP_PASSWORD"
    try:
        with open(ENV_FILE, "r") as f:
            lines = f.readlines()
        filtered = [l for l in lines if not l.startswith(f"export {key}=")]
        if len(filtered) != len(lines):
            with open(ENV_FILE, "w") as f:
                f.writelines(filtered)
            print(f"--> [BİLGİ] Geçici şifre migration_env.sh'dan temizlendi.")
    except Exception:
        # Temizlik başarısız olursa migration durmuyor; sadece uyarı
        pass


# ------------------------------------------------------------------------------
# Ana akış
# ------------------------------------------------------------------------------

def main():
    csv_row = get_user(TARGET_USERNAME)
    if not csv_row:
        print(f"--> [HATA] '{TARGET_USERNAME}' CSV'de bulunamadı.")
        print(f"    Önce 03_export_consumer_orgs.py'yi çalıştırın.")
        sys.exit(1)

    # Idempotency kontrolleri
    if csv_row.get("migrated", "false").lower() == "true":
        print(f"--> [BİLGİ] '{TARGET_USERNAME}' zaten migrate edilmiş. Atlanıyor.")
        return

    if csv_row.get("apic_jit_done", "false").lower() == "true":
        print(f"--> [BİLGİ] '{TARGET_USERNAME}' APIC'te zaten provision edilmiş. Atlanıyor.")
        return

    # load_env() modül yüklenirken çalıştı; ama step_01 şifreyi o andan SONRA
    # dosyaya yazdı. Bu subprocess yeni bir process olduğu için tekrar okumamız gerekiyor.
    load_env()
    kc_temp_password = os.environ.get("KC_TEMP_PASSWORD", "")

    if not kc_temp_password:
        print("--> [HATA] KC_TEMP_PASSWORD bulunamadı!")
        print("    step_01_create_kc_user.py başarıyla tamamlanmış olmalı.")
        print("    Keycloak'ta kullanıcı varsa admin panelinden şifre belirleyip")
        print("    migration_env.sh'a 'export KC_TEMP_PASSWORD=\"şifre\"' ekleyebilirsiniz.")
        sys.exit(1)

    print(f"\n--> [1/1] '{TARGET_USERNAME}' için APIC consumer login tetikleniyor (JIT provision)...")
    success = trigger_real_apic_login(TARGET_USERNAME, kc_temp_password)

    if not success:
        sys.exit(1)

    update_flag(TARGET_USERNAME, "apic_jit_done", True)
    mark_migrated(TARGET_USERNAME)
    clear_temp_password()

    print("==================================================")
    print(f"[TAMAMLANDI] '{TARGET_USERNAME}' APIC'e Keycloak üzerinden login edildi.")
    print("==================================================")


if __name__ == "__main__":
    main()
