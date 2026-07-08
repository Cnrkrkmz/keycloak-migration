#!/usr/bin/env python3.11
"""
migration_steps.py — Migration Pipeline: 4 Adım (Tek Dosya)
=============================================================
Çalıştıran : 04_run_migration.py (import ederek doğrudan çağırır)
             Tek kullanıcı testi için: python migration_steps.py <username>

İçerdiği Adımlar:
  step_01_create_kc_user(username, consumer_org)
      Keycloak'ta kullanıcıyı oluşturur. Geçici şifreyi migration_env.sh'a yazar.
      CSV → kc_user_created = true

  step_02_park_apic_email(username)
      APIC'teki e-postayı <adres>-old@<domain> yaparak park eder.
      CSV → apic_email_parked = true, source_email = <park adresi>

  step_03_jit_provision(username)
      APIC consumer token endpoint'e password grant ile login atar;
      APIC kendi veritabanında Keycloak shadow user kaydını açar (JIT).
      CSV → apic_jit_done = true, migrated = true
      ENV → KC_TEMP_PASSWORD temizlenir

  step_04_transfer_org(username)
      Consumer Org sahipliğini Local Registry kullanıcısından
      Keycloak shadow user'a devret (--cascade).
      CSV → org_owner_xfrd = true, migrated = true

Müşteri Ortamında Sık Karşılaşılan Hatalar (genel):
  - 'apic' CLI bulunamadı → PATH'e APIC Toolkit ekleyin.
  - Token alınamadı / 401 → 00_setup_env.py'yi yeniden çalıştırın.
  - SSL: CERTIFICATE_VERIFY_FAILED → Prod ortamda CA bundle gerekli.
    Bu dosyadaki _SSL_CTX bloğunu güncelleyin.
"""

import subprocess
import json
import os
import sys
import ssl
import urllib.request
import urllib.error
import urllib.parse
import secrets
import string

# Lab/test ortamı için SSL doğrulaması devre dışı.
# Üretimde: _SSL_CTX = ssl.create_default_context(cafile="/path/to/ca-bundle.crt")
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE

from migration_state import get_user, add_user, update_flag, update_source_email, mark_migrated

ENV_FILE = "migration_env.sh"


# ==============================================================================
# ORTAK YARDIMCILAR
# ==============================================================================

def load_env():
    """
    migration_env.sh dosyasını okuyarak 'export KEY="VALUE"' satırlarını
    os.environ'a yükler. Her adım kendi kritik değerini okumadan önce
    bu fonksiyonu çağırmalıdır (özellikle step_03, KC_TEMP_PASSWORD için).
    Dosya yoksa hata verip sys.exit(1) yapar.
    """
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


def _get_env(key, default=""):
    """
    os.environ'dan değer okur. load_env() sonrası çağrılmalıdır.
    Modül yükleme zamanında değil, fonksiyon çağrısı zamanında okunur;
    böylece 04_run_migration.py içinde load_env() sonrası değerler güncel kalır.
    """
    return os.environ.get(key, default)


def _http(url, *, data=None, method=None, headers=None):
    """
    Minimal urllib wrapper. (status_code, parsed_body) döndürür.
    Non-2xx durumlarda urllib.error.HTTPError fırlatır.
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


# ==============================================================================
# ADIM 1 — Keycloak'ta Kullanıcı Oluşturma
# ==============================================================================

class _ApicUser:
    """
    APIC'ten gelen ham JSON verisini alıp migration için gerekli
    alanları tutan sade bir nesneye dönüştürür.
    APIC bazı versiyonlarda snake_case, bazılarında camelCase döndürür;
    her ikisini de destekler.
    """
    def __init__(self, raw):
        self.username   = raw.get("username") or raw.get("name") or ""
        self.email      = raw.get("email") or ""
        self.first_name = raw.get("first_name") or raw.get("firstName") or ""
        self.last_name  = raw.get("last_name")  or raw.get("lastName")  or ""

    def is_valid(self):
        """Keycloak'ta hesap açmak için en az username ve email zorunludur."""
        return bool(self.username and self.email)


def _get_apic_user(username):
    """
    APIC Local Registry'den kullanıcıyı JSON olarak çeker ve _ApicUser nesnesi döndürür.
    username, CSV'deki değerden gelir — 03_export_consumer_orgs.py user["name"] alanını
    (case-preserved) CSV'ye yazarından bu değer doğrudan APIC CLI'ye geçilir.
    CLI hataları bazen stderr'de, bazen stdout'ta gelir; her ikisi de kontrol edilir.
    Hata durumunda None döner.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("LOCAL_REGISTRY"),
        "--format", "json", "--output", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return _ApicUser(json.loads(res.stdout))
    except subprocess.CalledProcessError as e:
        print("--> [HATA] APIC'ten kullanıcı alınamadı!")
        print(f"--> [DETAY] {e.stderr.strip() or e.stdout.strip()}")
        return None
    except json.JSONDecodeError:
        print("--> [HATA] APIC yanıtı geçerli JSON değil!")
        return None


def _get_kc_admin_token():
    """
    Keycloak master realm üzerinden admin-cli ile token alır.
    Bu token Keycloak Admin REST API'sine yapılacak tüm isteklerde kullanılır.
    """
    kc_url = _get_env("KEYCLOAK_URL")
    url = f"{kc_url}/realms/master/protocol/openid-connect/token"
    data = urllib.parse.urlencode({
        "username":   _get_env("KEYCLOAK_ADMIN_USER"),
        "password":   _get_env("KEYCLOAK_ADMIN_PASSWORD"),
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


def _create_kc_user_api(token, user_obj):
    """
    Keycloak Admin API'si ile kullanıcıyı hedef realm'e (KEYCLOAK_REALM_NAME) ekler.
    16 karakterli kriptografik rastgele geçici şifre üretir ve credentials'a yazar.
    HTTP 409: kullanıcı zaten mevcutsa uyarı verir (None döner; step_01 başarısız sayılır).
    Başarıda geçici şifre string'i, başarısızlıkta None döner.
    """
    target_realm = _get_env("KEYCLOAK_REALM_NAME", "apic-demo")
    url = f"{_get_env('KEYCLOAK_URL')}/admin/realms/{target_realm}/users"

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
            print(f"--> [UYARI] '{user_obj.username}' Keycloak'ta zaten mevcut (HTTP 409).")
            print(f"    Çözüm: Keycloak admin panelinden kullanıcıyı silin, sonra tekrar deneyin.")
        else:
            print(f"--> [HATA] Kullanıcı oluşturulamadı (HTTP {e.code}): {e.read().decode()}")
    return None


def _save_temp_password(temp_pass):
    """
    Geçici şifreyi migration_env.sh'a yazar; step_03 bu değeri okuyarak
    APIC consumer token endpoint'ine login eder.
    Dosyada KC_TEMP_PASSWORD zaten varsa üzerine yazar, yoksa sona ekler.
    Başarılı JIT provision sonrası _clear_temp_password() ile temizlenir.
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


def step_01_create_kc_user(username, consumer_org=""):
    """
    Migration Adım 1/4 — Keycloak'ta Kullanıcı Oluşturma

    1. CSV'de kc_user_created=true ise idempotent olarak atlar.
    2. APIC Local Registry'den kullanıcı bilgilerini çeker.
    3. Keycloak Admin API ile hedef realm'e kullanıcıyı ekler.
    4. Üretilen geçici şifreyi migration_env.sh'a yazar (step_03 okur).
    5. CSV'de kc_user_created=true olarak işaretler.

    Döner: başarıda True, hata/atlanmada False.

    Müşteri Hataları:
      HTTP 409 → Keycloak'ta aynı username zaten var, önce silin.
      Token alınamadı → Keycloak admin bilgilerini doğrulayın.
    """
    load_env()
    csv_row = get_user(username)
    if csv_row and csv_row.get("kc_user_created", "false").lower() == "true":
        print(f"--> [BİLGİ] '{username}' zaten Keycloak'ta (kc_user_created=true). Atlanıyor.")
        return True

    print(f"\n--> [1/3] APIC'ten '{username}' kullanıcısı okunuyor...")
    user_obj = _get_apic_user(username)
    if not user_obj:
        return False

    if not user_obj.is_valid():
        print(f"--> [HATA] Kullanıcının e-posta veya kullanıcı adı boş!")
        print(f"    Keycloak'ta hesap açabilmek için her ikisi de zorunludur.")
        return False

    if not csv_row:
        add_user(username, consumer_org, user_obj.email)

    print("--> [2/3] Keycloak admin token alınıyor...")
    token = _get_kc_admin_token()
    if not token:
        return False

    print(f"--> [3/3] '{username}' Keycloak'a yazılıyor...")
    temp_pass = _create_kc_user_api(token, user_obj)
    if not temp_pass:
        return False

    update_flag(username, "kc_user_created", True)
    _save_temp_password(temp_pass)
    print("==================================================")
    return True


# ==============================================================================
# ADIM 2 — APIC E-postasını Park Et
# ==============================================================================

def _get_current_user_data(username):
    """
    APIC Local Registry'den kullanıcının güncel bilgilerini çeker (JSON).
    E-postanın -old@ içerip içermediğini ve isim alanlarını buradan öğreniriz.
    Hata durumunda None döner.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("LOCAL_REGISTRY"),
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


def _update_user_email(username, current_data, new_email):
    """
    APIC Local Registry'de kullanıcının e-postasını günceller.
    first_name ve last_name mevcut değerleri korunarak gönderilir;
    APIC bu alanları boş gönderince sıfırlayabilir.
    Başarıda True, CLI hatasında False döner.
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
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("LOCAL_REGISTRY"),
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print("--> [HATA] APIC güncelleme reddedildi!")
        print(f"--> [DETAY] {e.stderr.strip() or e.stdout.strip()}")
        return False


def step_02_park_apic_email(username):
    """
    Migration Adım 2/4 — APIC E-postasını Park Et

    APIC Local Registry'deki e-postayı <adres>-old@<domain> formatına çevirir.
    Bu, JIT provision sırasında Keycloak kaydı ile APIC kaydının aynı e-postaya
    sahip olmasından doğacak çakışmayı engeller.

    1. CSV'de apic_email_parked=true ise atlar (idempotent).
    2. Mevcut e-postayı çeker.
    3. E-postayı -old@ formatında günceller.
    4. Değişikliği APIC'ten okuyarak doğrular.
    5. CSV'de apic_email_parked=true, source_email=<park adresi> yazar.

    Döner: başarıda True, hata/atlanmada False.

    Müşteri Hataları:
      APIC güncelleme reddedildi → Kullanıcı dış kaynaklı (LDAP) olabilir.
      Doğrulama başarısız → APIC validation kısıtlaması, yöneticiyle görüşün.
    """
    load_env()
    csv_row = get_user(username)
    if not csv_row:
        print(f"--> [HATA] '{username}' CSV'de bulunamadı. Önce 03_export_consumer_orgs.py'yi çalıştırın.")
        return False

    if csv_row.get("apic_email_parked", "false").lower() == "true":
        print(f"--> [BİLGİ] '{username}' e-postası zaten park edilmiş. Atlanıyor.")
        return True

    print(f"\n--> [1/3] '{username}' kullanıcısının APIC bilgileri okunuyor...")
    current_data = _get_current_user_data(username)
    if not current_data:
        return False

    old_email = current_data.get("email", "")
    if not old_email or "@" not in old_email:
        print(f"--> [HATA] Geçerli e-posta adresi bulunamadı (mevcut: '{old_email}').")
        return False

    if old_email.split("@")[0].endswith("-old"):
        print(f"--> [BİLGİ] E-posta zaten park edilmiş durumda ({old_email}). Tekrar eklenmeyecek.")
        new_email = old_email
    else:
        parts     = old_email.split("@")
        new_email = f"{parts[0]}-old@{parts[1]}"

    print(f"--> APIC'teki mevcut e-posta : {old_email}")
    print(f"--> Park edilecek e-posta    : {new_email}")

    print("\n--> [2/3] APIC Local Registry güncelleniyor...")
    if not _update_user_email(username, current_data, new_email):
        return False

    print("--> [3/3] Değişiklik APIC'ten okunarak doğrulanıyor...")
    verify = _get_current_user_data(username)
    if verify and verify.get("email") == new_email:
        print("--> [BAŞARILI] E-posta değişimi doğrulandı!")
        update_flag(username, "apic_email_parked", True)
        update_source_email(username, new_email)
    else:
        print("--> [UYARI] Güncelleme komutu çalıştı ancak APIC doğrulaması başarısız oldu.")
        print("    APIC üzerinde manuel kontrol yapın.")
        return False

    print("==================================================")
    return True


# ==============================================================================
# ADIM 3 — APIC JIT Provision (JWT-Bearer Token Exchange)
# ==============================================================================

def _get_kc_access_token(username, password):
    """
    Kullanıcının geçici şifresiyle Keycloak'tan Access Token alır.
    Bu token sonraki adımda APIC'e JWT-Bearer assertion olarak sunulur.
    KEYCLOAK_CLIENT_ID: APIC'in Keycloak'ta kayıtlı OIDC client'ı.
    Başarıda access_token string'i, hata durumunda None döner.
    """
    target_realm = _get_env("KEYCLOAK_REALM_NAME", "apic-demo")
    kc_client_id = _get_env("KEYCLOAK_CLIENT_ID", "apic-client")
    url = f"{_get_env('KEYCLOAK_URL')}/realms/{target_realm}/protocol/openid-connect/token"

    body_params = {
        "grant_type": "password",
        "client_id":  kc_client_id,
        "username":   username,
        "password":   password,
        "scope":      "openid email profile",
    }
    data = urllib.parse.urlencode(body_params).encode("utf-8")
    try:
        _, body = _http(url, data=data)
        token = body.get("access_token")
        if not token:
            print(f"--> [HATA] Access Token alınamadı: {body}")
        return token
    except urllib.error.HTTPError as e:
        print(f"--> [HATA] KC token isteği başarısız (HTTP {e.code}): {e.read().decode()}")
        return None


def _trigger_apic_jwt_bearer(access_token):
    """
    Keycloak'tan alınan Access Token'ı APIC'e JWT-Bearer grant olarak sunar.

    Akış:
      1. Keycloak'tan alınan access_token, APIC'e 'assertion' alanında gönderilir.
      2. APIC token'ı Keycloak'a doğrulattırır.
      3. APIC kendi veritabanında shadow user kaydını açar (JIT provision).
         Bunun çalışması için APIC'teki Keycloak registry'sinde
         "Auto onboard" (otomatik kayıt) özelliğinin açık olması gerekir.

    Realm formatı: consumer:<prov_org>:<catalog>/<keycloak_registry_adi>
    Başarıda True, hata durumunda False döner.
    """
    apic_server       = _get_env("APIC_SERVER")
    prov_org          = _get_env("PROV_ORG")
    catalog           = _get_env("CATALOG", "sandbox")
    kc_registry       = _get_env("KEYCLOAK_REGISTRY_NAME", "keycluk")
    creds_file        = _get_env("APIC_CLIENT_CREDS", "")

    url                = f"{apic_server}/api/token"
    apic_client_id     = ""
    apic_client_secret = ""

    if creds_file and os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                creds = json.load(f)
            # credentials.json yapısı: {"consumer_toolkit": {"endpoint": ..., "client_id": ...}}
            toolkit = creds.get("consumer_toolkit") or creds.get("toolkit", {})
            if "endpoint" in toolkit:
                url = f"{toolkit['endpoint']}/token"
            apic_client_id     = toolkit.get("client_id")     or creds.get("client_id", "")
            apic_client_secret = toolkit.get("client_secret") or creds.get("client_secret", "")
        except Exception as e:
            print(f"--> [UYARI] credentials.json okunamadı: {e}")

    if not apic_client_id or not apic_client_secret:
        print(f"--> [HATA] APIC client_id veya client_secret bulunamadı!")
        print(f"    credentials.json dosyasını ve APIC_CLIENT_CREDS yolunu kontrol edin.")
        return False

    realm_str = f"consumer:{prov_org}:{catalog}/{kc_registry}"
    payload = json.dumps({
        "realm":         realm_str,
        "grant_type":    "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion":     access_token,
        "client_id":     apic_client_id,
        "client_secret": apic_client_secret,
    }).encode("utf-8")

    try:
        status, body = _http(
            url,
            data=payload,
            headers={
                "Content-Type":           "application/json",
                "Accept":                 "application/json",
                # X-IBM-Consumer-Context: APIC'e hangi org/catalog context'inin
                # kullanılacağını belirtir
                "X-IBM-Consumer-Context": f"{prov_org}.{catalog}",
            },
        )
        if status == 200:
            print("--> [BAŞARILI] APIC JIT-provisioning (Auto Onboard) tamamlandı.")
            return True
        else:
            print(f"--> [HATA] APIC login başarısız (HTTP {status}): {body}")
            return False
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        if e.code == 400 and "already" in err_body.lower():
            print("--> [BİLGİ] Kullanıcı APIC üzerinde zaten mevcut.")
            return True
        print(f"--> [HATA] APIC login reddedildi (HTTP {e.code}): {err_body}")
        return False
    except Exception as e:
        print(f"--> [HATA] İstek sırasında beklenmeyen hata: {e}")
        return False


def _clear_temp_password():
    """
    migration_env.sh'dan KC_TEMP_PASSWORD satırını siler.
    JIT provision tamamlanır tamamlanmaz çağrılmalıdır; geçici şifre
    diskte gereksiz yere kalmaz. Temizlik başarısız olursa migration durmaz.
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
        pass


def step_03_jit_provision(username):
    """
    Migration Adım 3/4 — APIC JIT Provision (JWT-Bearer Token Exchange)

    Keycloak'ta oluşturulmuş kullanıcıyı APIC'e tanıtır (shadow user kaydı açar).

    1. CSV'de migrated/apic_jit_done=true ise atlar (idempotent).
    2. migration_env.sh'dan KC_TEMP_PASSWORD'ü taze okur
       (step_01 bu süreçte diske yazdı; taze okuma şart).
    3. _get_kc_access_token() ile Keycloak'tan Access Token alır.
    4. _trigger_apic_jwt_bearer() ile token'ı APIC'e JWT-Bearer olarak sunar.
    5. CSV → apic_jit_done=true, migrated=true yazar.
    6. _clear_temp_password() ile geçici şifreyi temizler.

    Döner: başarıda True, hata durumunda False.

    Müşteri Hataları:
      KC_TEMP_PASSWORD bulunamadı → step_01 başarısız olmuş, Keycloak'tan elle şifre belirleyin.
      KC token 401 → client_id yanlış veya kullanıcı Keycloak'ta yok.
      APIC 401 → APIC client credentials geçersiz.
      APIC 400 → Realm string formatı yanlış veya Auto Onboard kapalı.
    """
    load_env()  # KC_TEMP_PASSWORD step_01 tarafından az önce yazıldı; taze okuma şart
    csv_row = get_user(username)
    if not csv_row:
        print(f"--> [HATA] '{username}' CSV'de bulunamadı.")
        return False

    if csv_row.get("migrated", "false").lower() == "true":
        print(f"--> [BİLGİ] '{username}' zaten migrate edilmiş. Atlanıyor.")
        return True

    if csv_row.get("apic_jit_done", "false").lower() == "true":
        print(f"--> [BİLGİ] '{username}' APIC'te zaten provision edilmiş. Atlanıyor.")
        return True

    kc_temp_password = os.environ.get("KC_TEMP_PASSWORD", "")
    if not kc_temp_password:
        print("--> [HATA] KC_TEMP_PASSWORD bulunamadı!")
        print("    step_01_create_kc_user başarıyla tamamlanmış olmalı.")
        print("    Keycloak'ta kullanıcı varsa admin panelinden şifre belirleyip")
        print("    migration_env.sh'a 'export KC_TEMP_PASSWORD=\"şifre\"' ekleyebilirsiniz.")
        return False

    print(f"\n--> [1/2] '{username}' için Keycloak'tan Access Token alınıyor...")
    access_token = _get_kc_access_token(username, kc_temp_password)
    if not access_token:
        return False

    print(f"--> [2/2] APIC'e JWT-Bearer token gönderiliyor (JIT Provision)...")
    success = _trigger_apic_jwt_bearer(access_token)
    if not success:
        return False

    update_flag(username, "apic_jit_done", True)
    
    _clear_temp_password()

    print("==================================================")
    print(f"[TAMAMLANDI] '{username}' APIC'e Keycloak üzerinden login edildi.")
    print("==================================================")
    return True

# ==============================================================================
# ADIM 4 — Consumer Org Sahipliğini Devret
# ==============================================================================

def _get_target_email_for_org(username):
    """
    APIC Local Registry'den kullanıcının güncel e-postasını okur.
    step_02 sonrasında e-posta -old@ formatında olabilir; bu fonksiyon
    -old@ kısmını temizleyerek Keycloak'taki gerçek e-postayı döndürür.
    Shadow user Keycloak'ta orijinal e-posta ile oluşturulduğundan
    bu eşleştirme kritiktir.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("LOCAL_REGISTRY"),
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


def _get_shadow_user_url(consumer_org, expected_email):
    """
    APIC'teki Keycloak registry'sinde e-posta adresiyle shadow user'ı arar
    ve APIC URL'sini döndürür. Bu URL üye ekleme ve sahiplik devri için gereklidir.
    Bulunamazsa None döner.
    """
    cmd = [
        "apic", "users:list",
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "--user-registry", _get_env("KEYCLOAK_REGISTRY_NAME", "keycluk"),
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


def _add_kc_user_as_member(consumer_org, username, user_url):
    """
    Shadow user'ı consumer org'a üye olarak ekler.
    Sahiplik devri yapılabilmesi için kullanıcının önce org'un üyesi olması şarttır.
    Zaten üyeyse idempotent davranır (already exists kontrolü).
    Başarıda True, hata durumunda False döner.
    """
    yaml_content = f"""name: "{username}-kc"
title: "{username}"
user:
  url: "{user_url}"
"""
    cmd = [
        "apic", "members:create", "--scope", "consumer-org", "-",
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "-c", _get_env("CATALOG"),
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


def _get_member_url(consumer_org, expected_email):
    """
    Consumer org üye listesinden shadow user'ın member URL'sini döndürür.
    transfer-owner komutu kullanıcı URL'sini değil, member URL'sini bekler.
    Bulunamazsa None döner.
    """
    cmd = [
        "apic", "members:list", "--scope", "consumer-org",
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "-c", _get_env("CATALOG"),
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


def _transfer_ownership(consumer_org, member_url):
    """
    consumer-orgs:transfer-owner --cascade komutu ile sahipliği devreder.
    --cascade: Consumer Org altındaki App ve Subscription kayıtlarının owner'ı
    da yeni kullanıcıya güncellenir.
    DİKKAT: Bu geri alınamaz bir işlemdir; rollback scripti bu adımı tersine çevirmez.
    Başarıda True, CLI hatasında False döner.
    """
    yaml_content = f"new_owner_member_url: {member_url}\n"
    cmd = [
        "apic", "consumer-orgs:transfer-owner", consumer_org, "-",
        "-s", _get_env("APIC_SERVER"), "-o", _get_env("PROV_ORG"),
        "-c", _get_env("CATALOG"),
        "--cascade",
    ]
    try:
        subprocess.run(cmd, input=yaml_content, capture_output=True, text=True, check=True)
        print(f"--> [BAŞARILI] '{consumer_org}' sahipliği Keycloak profiline devredildi.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"--> [HATA] Sahiplik devri başarısız:\n{e.stderr.strip() or e.stdout.strip()}")
        return False


def step_04_transfer_org(username):
    """
    Migration Adım 4/4 — Consumer Org Sahipliğini Devret

    JIT provision ile APIC'te shadow user açıldıktan sonra
    consumer org'un sahipliği Local Registry kullanıcısından Keycloak profiline geçer.

    1. CSV'de migrated=true ise atlar (idempotent).
    2. CSV'den consumer_org adını okur.
    3. APIC Local Registry'deki e-postadan (-old@ temizleyerek) gerçek e-postayı hesaplar.
    4. Keycloak registry'sinde shadow user URL'sini bulur.
    5. Shadow user'ı consumer org'a üye olarak ekler.
    6. consumer-orgs:transfer-owner --cascade ile sahipliği devreder.
    7. CSV → org_owner_xfrd=true, migrated=true yazar.

    Döner: başarıda True, hata durumunda False.

    Müşteri Hataları:
      Shadow user bulunamadı → step_03 tamamlanmamış.
      Sahiplik devri başarısız → --cascade bazı versiyonlarda desteklenmeyebilir.
    """
    load_env()
    csv_row = get_user(username)
    if not csv_row:
        print(f"--> [HATA] '{username}' CSV'de bulunamadı.")
        return False

    if csv_row.get("migrated", "false").lower() == "true":
        print(f"--> [BİLGİ] '{username}' zaten migrate edilmiş. Atlanıyor.")
        return True

    consumer_org = csv_row.get("consumer_org", "")
    if not consumer_org:
        print(f"--> [HATA] '{username}' için CSV'de consumer_org bilgisi yok.")
        return False

    print(f"\n--> [1/4] '{username}' için hedef e-posta hesaplanıyor...")
    expected_email = _get_target_email_for_org(username)
    if not expected_email:
        return False
    print(f"--> [BİLGİ] Keycloak'taki e-posta: {expected_email}")

    print(f"--> [2/4] APIC Keycloak registry'sinde shadow user aranıyor...")
    user_url = _get_shadow_user_url(consumer_org, expected_email)
    if not user_url:
        print(f"--> [HATA] '{expected_email}' için shadow user bulunamadı.")
        print(f"    step_03_jit_provision'ın başarıyla tamamlandığını doğrulayın.")
        return False

    print(f"--> [3/4] Shadow user '{consumer_org}' org'una üye ekleniyor...")
    if not _add_kc_user_as_member(consumer_org, username, user_url):
        return False

    print(f"--> [4/4] Consumer org sahipliği devrediliyor (--cascade)...")
    member_url = _get_member_url(consumer_org, expected_email)
    if not member_url:
        print("--> [HATA] Member URL alınamadı. Üye listesini manuel kontrol edin.")
        return False

    if not _transfer_ownership(consumer_org, member_url):
        return False

    update_flag(username, "org_owner_xfrd", True)
    mark_migrated(username)

    print("==================================================")
    print(f"[TAMAMLANDI] '{consumer_org}' → owner artık Keycloak profili.")
    print("==================================================")
    return True


# ==============================================================================
# Tek kullanıcı testi için doğrudan çalıştırma
# ==============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Kullanım: python migration_steps.py <username> [consumer_org]")
        sys.exit(1)

    _username = sys.argv[1]
    _org      = sys.argv[2] if len(sys.argv) > 2 else ""

    print(f"\n{'='*60}")
    print(f"  TEK KULLANICI MİGRASYON: {_username}")
    print(f"{'='*60}")

    load_env()
    ok = (
        step_01_create_kc_user(_username, _org)
        and step_02_park_apic_email(_username)
        and step_03_jit_provision(_username)
        and step_04_transfer_org(_username)
    )
    sys.exit(0 if ok else 1)
