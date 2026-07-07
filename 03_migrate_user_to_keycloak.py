#!/usr/bin/env python3.11
import subprocess
import json
import os
import sys
import urllib.request
import urllib.parse
import secrets
import string

from migration_state import get_user, add_user, update_flag
# add_user imzası: add_user(username, consumer_org, email_source)

# ==============================================================================
# APIC TO KEYCLOAK MIGRATION (OBJECT-ORIENTED & IN-MEMORY)
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
                    key = key_value[0]
                    value = key_value[1].strip('"\'')
                    os.environ[key] = value

# Ortam değişkenlerini belleğe yükle
load_env()

APIC_SERVER = os.environ.get("APIC_SERVER")
PROV_ORG = os.environ.get("PROV_ORG")
LOCAL_REGISTRY = os.environ.get("LOCAL_REGISTRY")
KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL")
KEYCLOAK_ADMIN_USER = os.environ.get("KEYCLOAK_ADMIN_USER")
KEYCLOAK_ADMIN_PASSWORD = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
TARGET_REALM = "apic-demo"

if len(sys.argv) > 1:
    DEMO_USERNAME = sys.argv[1]
else:
    DEMO_USERNAME = input("Migrate edilecek APIC Kullanıcı Adı: ").strip()

# Consumer Org bilgisi CSV'den gelir (add_user çağrısında); fallback olarak env'den okunur
CONSUMER_ORG = os.environ.get("CONSUMER_ORG", "")

# ------------------------------------------------------------------------------
# KULLANICI OBJESİ (CLASS)
# ------------------------------------------------------------------------------
class ApicUser:
    """
    APIC'ten gelen karmaşık JSON verisini alıp, sadece ihtiyacımız olan
    verileri barındıran temiz bir Python objesine dönüştürür.
    """
    def __init__(self, raw_json_data):
        self.username = raw_json_data.get("username") or raw_json_data.get("name") or ""
        self.email = raw_json_data.get("email") or ""
        self.first_name = raw_json_data.get("first_name") or raw_json_data.get("firstName") or ""
        self.last_name = raw_json_data.get("last_name") or raw_json_data.get("lastName") or ""

    def is_valid(self):
        """Keycloak için zorunlu alanların dolu olup olmadığını kontrol eder."""
        return bool(self.username and self.email)

# ------------------------------------------------------------------------------
# FONKSİYONLAR
# ------------------------------------------------------------------------------
def get_apic_user_as_object(username):
    """
    APIC'ten kullanıcı datasını doğrudan STDOUT üzerinden (dosya yaratmadan)
    çeker ve ApicUser objesi olarak döndürür.
    """
    cmd = [
        "apic", "users:get", username,
        "-s", APIC_SERVER, "-o", PROV_ORG,
        "--user-registry", LOCAL_REGISTRY,
        "--format", "json",
        "--output", "-"  # Veriyi dosyaya değil, doğrudan terminale basması için
    ]

    try:
        # capture_output=True ile ekrana basılacak olan JSON'ı doğrudan yakalıyoruz
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)

        # Yakalanan saf metni JSON olarak parçalıyoruz
        raw_data = json.loads(res.stdout)

        # Saf veriyi ApicUser objesine (Class) bağlayıp geri döndürüyoruz
        return ApicUser(raw_data)

    except subprocess.CalledProcessError as e:
        print("--> [HATA] APIC komutu sunucu tarafından reddedildi!")
        print(f"--> [DETAY] {e.stderr.strip() if e.stderr else e.stdout.strip()}")
        return None
    except json.JSONDecodeError:
        print("--> [HATA] APIC'ten dönen yanıt geçerli bir JSON değil!")
        print(f"--> [GELEN YANIT] {res.stdout}")
        return None
    except Exception as e:
        print(f"--> [HATA] Beklenmeyen hata: {e}")
        return None

def get_kc_token():
    """Keycloak Admin Access Token alır."""
    url = f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token"
    data = urllib.parse.urlencode({
        "username": KEYCLOAK_ADMIN_USER,
        "password": KEYCLOAK_ADMIN_PASSWORD,
        "grant_type": "password",
        "client_id": "admin-cli"
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            return res_data.get("access_token")
    except Exception as e:
        print(f"--> [HATA] Token alınamadı: {e}")
        return None

def create_kc_user(token, user_obj):
    """
    Keycloak üzerinde kullanıcıyı yaratır.
    Başarılı olursa üretilen geçici şifreyi döndürür, aksi halde None döner.
    """
    url = f"{KEYCLOAK_URL}/admin/realms/{TARGET_REALM}/users"

    # 16 haneli güvenli rastgele şifre üretimi
    alphabet = string.ascii_letters + string.digits
    temp_pass = ''.join(secrets.choice(alphabet) for _ in range(16))

    payload = {
        "username": user_obj.username,
        "enabled": True,
        "emailVerified": True,
        "email": user_obj.email,
        "firstName": user_obj.first_name,
        "lastName": user_obj.last_name,
        "credentials": [{"type": "password", "value": temp_pass, "temporary": False}]
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 201:
                print(f"--> [BAŞARILI] '{user_obj.username}' Keycloak'ta yaratıldı.")
                return temp_pass
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print(f"--> [UYARI] Kullanıcı '{user_obj.username}' zaten mevcut!")
        else:
            print(f"--> [HATA] Kullanıcı yaratılamadı (HTTP {e.code})")
    return None

# ------------------------------------------------------------------------------
# ANA ÇALIŞMA BLOĞU
# ------------------------------------------------------------------------------
def save_temp_password(username, temp_pass):
    """
    Üretilen geçici şifreyi migration_env.sh'a yazar.
    04_pre_provision_shadow_user.py bu değeri oradan okuyacak.
    """
    key = "KC_TEMP_PASSWORD"
    line = f'export {key}="{temp_pass}"\n'

    with open(ENV_FILE, "r") as f:
        lines = f.readlines()

    # Varsa eski satırı güncelle, yoksa ekle
    updated = False
    for i, l in enumerate(lines):
        if l.startswith(f"export {key}="):
            lines[i] = line
            updated = True
            break
    if not updated:
        lines.append(line)

    with open(ENV_FILE, "w") as f:
        f.writelines(lines)

    print(f"--> [BİLGİ] Geçici şifre '{ENV_FILE}' dosyasına kaydedildi (05. script tarafından kullanılacak).")


def main():
    # CSV kaydı yoksa oluştur; kc_user_created=true ise bu adım zaten geçilmiş demektir
    csv_row = get_user(DEMO_USERNAME)
    if csv_row and csv_row.get("kc_user_created", "false").lower() == "true":
        print(f"--> [BİLGİ] '{DEMO_USERNAME}' zaten Keycloak'ta mevcut (kc_user_created=true). Atlanıyor.")
        print("==================================================")
        return

    print(f"\n--> [1/3] APIC'ten '{DEMO_USERNAME}' okunup objeye dönüştürülüyor...")
    user_obj = get_apic_user_as_object(DEMO_USERNAME)

    if not user_obj:
        sys.exit(1)

    if not user_obj.is_valid():
        print(f"--> [HATA] Kullanıcının Keycloak için zorunlu alanları (Email/Username) eksik!")
        sys.exit(1)

    # CSV'de kayıt yoksa şimdi oluştur (orijinal e-posta SOURCE olarak kaydediliyor)
    if not csv_row:
        add_user(DEMO_USERNAME, CONSUMER_ORG, user_obj.email)

    print("--> [2/3] Keycloak Token alınıyor...")
    token = get_kc_token()
    if not token:
        sys.exit(1)

    print("--> [3/3] Keycloak'a yazılıyor...")
    temp_pass = create_kc_user(token, user_obj)
    if temp_pass:
        update_flag(DEMO_USERNAME, "kc_user_created", True)
        save_temp_password(user_obj.username, temp_pass)

    print("==================================================")

if __name__ == "__main__":
    main()