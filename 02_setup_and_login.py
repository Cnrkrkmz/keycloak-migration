#!/usr/bin/env python3.11
import os
import subprocess
import getpass

ENV_FILE = "migration_env.sh"

def get_input_with_default(prompt_text, default_value, is_password=False):
    """
    Kullanıcıdan veri alır. Eğer kullanıcı boş bırakıp Enter'a basarsa,
    default_value değerini kullanır. is_password True ise girdiyi gizler.
    """
    if is_password:
        user_input = getpass.getpass(f"{prompt_text} [{default_value}]: ").strip()
    else:
        user_input = input(f"{prompt_text} [{default_value}]: ").strip()

    return user_input if user_input else default_value

def setup_environment():
    """
    Global altyapı değişkenlerini ve login bilgilerini alır,
    diğer scriptlerin kullanabilmesi için dosyaya kaydeder.
    """
    print("--------------------------------------------------")
    print("ADIM 1: Global Ortam Değişkenleri Ayarlanıyor...")
    print("(Mevcut değeri kullanmak için sadece Enter'a basın)")
    print("--------------------------------------------------")

    env_vars = {}
    env_vars["APIC_SERVER"] = get_input_with_default("APIC Server URL", "https://api.apic.apps.ocpinstall.gym.lan")
    env_vars["APIC_REALM"] = get_input_with_default("APIC Realm", "provider/default-idp-2")
    env_vars["PROV_ORG"] = get_input_with_default("Provider Organization", "caner-script-provider")
    env_vars["CATALOG"] = get_input_with_default("Catalog", "sandbox")
    env_vars["LOCAL_REGISTRY"] = get_input_with_default("Local Registry", "sandbox-catalog")
    env_vars["ROOT_DIR"] = get_input_with_default("Root Directory", "/home/admin/caner-script-deneme")

    print("\n--- APIC Kimlik ve Client Bilgileri ---")
    env_vars["APIC_CLIENT_CREDS"] = get_input_with_default("Client Credentials File", "/home/admin/credentials.json")
    env_vars["APIC_USERNAME"] = get_input_with_default("APIC Admin Username", "canerkorkmaz")
    env_vars["APIC_PASSWORD"] = get_input_with_default("APIC Admin Password", "Passw0rd", is_password=True)

    print("\n--- KEYCLOAK Bilgileri ---")
    env_vars["KEYCLOAK_URL"] = get_input_with_default("KEYCLOAK URL", "http://keycloak-keycloak-demo.apps.ocpinstall.gym.lan")
    env_vars["KEYCLOAK_ADMIN_USER"] = get_input_with_default("KEYCLOAK Admin Username", "admin")
    env_vars["KEYCLOAK_ADMIN_PASSWORD"] = get_input_with_default("KEYCLOAK Admin Password", "Admin123!", is_password=True)
    env_vars["KEYCLOAK_REGISTRY_NAME"] = get_input_with_default("KEYCLOAK Registry Name", "keycluk")
    env_vars["KEYCLOAK_REALM_NAME"] = get_input_with_default("KEYCLOAK Target Realm", "apic-demo")
    env_vars["KEYCLOAK_CLIENT_ID"] = get_input_with_default("KEYCLOAK OIDC Client ID (APIC için)", "apic-client")
    env_vars["KEYCLOAK_CLIENT_SECRET"] = get_input_with_default("KEYCLOAK OIDC Client Secret (boşsa Enter)", "", is_password=True)
    try:
        with open(ENV_FILE, "w") as f:
            f.write("#!/bin/bash\n")
            f.write("# Bu dosya 02_setup_and_login.py scripti tarafından otomatik oluşturulmuştur.\n\n")
            for key, value in env_vars.items():
                f.write(f'export {key}="{value}"\n')
                os.environ[key] = value

        print(f"\n--> [BİLGİ] Altyapı değişkenleri belleğe alındı ve '{ENV_FILE}' dosyasına kaydedildi.")

        os.makedirs(env_vars["ROOT_DIR"], exist_ok=True)
        print(f"--> [BİLGİ] Çalışma dizini hazır: {env_vars['ROOT_DIR']}")

    except Exception as e:
        print(f"--> [HATA] Ortam değişkenleri ayarlanırken bir sorun oluştu: {str(e)}")
        exit(1)

def apic_login():
    """
    Ortam değişkenlerindeki bilgileri kullanarak önce Client Credentials ayarlar,
    ardından APIC CLI üzerinden otomatik sisteme giriş yapar.
    """
    print("\n--------------------------------------------------")
    print("ADIM 2: API Connect (APIC) Sistemine Otomatik Giriş")
    print("--------------------------------------------------")

    server = os.environ.get("APIC_SERVER")
    realm = os.environ.get("APIC_REALM")
    username = os.environ.get("APIC_USERNAME")
    password = os.environ.get("APIC_PASSWORD")
    creds_file = os.environ.get("APIC_CLIENT_CREDS")

    print(f"Hedef Sunucu : {server}")
    print(f"Kullanıcı    : {username}")
    print(f"Client Creds : {creds_file}")

    # 1. Client Credentials Set Etme Adımı
    print("\n--> [BİLGİ] Client credentials ayarlanıyor...")
    creds_command = ["apic", "client-creds:set", creds_file]

    try:
        subprocess.run(creds_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=True)
        print("--> [BAŞARILI] Client credentials başarıyla yüklendi.")
    except subprocess.CalledProcessError as e:
        print("--> [HATA] Client credentials ayarlanamadı!")
        error_details = e.stderr.strip() if e.stderr else e.stdout.strip()
        print(f"Detay: {error_details}")
        exit(1)

    # 2. Login Adımı
    print("--> [BİLGİ] APIC sistemine otomatik login olunuyor, lütfen bekleyin...")
    login_command = [
        "apic", "login",
        "--server", server,
        "--realm", realm,
        "--username", username,
        "--password", password
    ]

    try:
        result = subprocess.run(login_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=True)
        print(f"--> [BAŞARILI] {result.stdout.strip()}")

    except subprocess.CalledProcessError as e:
        print(f"--> [HATA] APIC Login başarısız oldu!")
        # Eğer stderr boş gelirse stdout'u kontrol et (CLI hataları bazen stdout'a atabiliyor)
        error_details = e.stderr.strip() if e.stderr else e.stdout.strip()
        print(f"Detay: {error_details}")
        exit(1)
    except FileNotFoundError:
        print("--> [HATA] 'apic' CLI komutu bulunamadı! APIC Toolkit'in PATH üzerinde kurulu olduğundan emin olun.")
        exit(1)

if __name__ == "__main__":
    setup_environment()
    apic_login()
    print("\n==================================================")
    print("Bölüm 1 Başarıyla Tamamlandı!")
    print("==================================================")