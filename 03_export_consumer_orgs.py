#!/usr/bin/env python3.11
"""
03_export_consumer_orgs.py — APIC Consumer Org & Owner Dışa Aktarma

Çalıştırma sırası: 03  (00_setup_env.py'den SONRA, 04_run_migration.py'den ÖNCE)

Tüm consumer org'ları APIC API'sinden sayfalama (paging) ile çeker,
her org'un owner kullanıcısını ayrıca sorgular ve sonuçları
migration_users.csv dosyasına yazar.

Bu CSV, 04_run_migration.py tarafından migration input'u olarak kullanılır.

Ne Zaman Tekrar Çalıştırılır:
  - Migration'dan önce ilk kez mutlaka çalıştırılmalıdır (CSV oluşturur).
  - APIC'e yeni consumer org eklendiyse tekrar çalıştırılabilir;
    mevcut CSV'deki kayıtları korur, sadece yeni org'ları ekler.
  - Zaten migrate edilmiş (migrated=true) kullanıcılar CSV'de kalır,
    bir sonraki çalıştırmada tekrar eklenmez (username kontrolü).

Kullanım:
  python 03_export_consumer_orgs.py              # tüm org'lar
  python 03_export_consumer_orgs.py --page-size 25   # sayfa boyutunu özelleştir
  python 03_export_consumer_orgs.py --dry-run    # sadece ekrana yaz, CSV'ye yazma
"""

import subprocess
import json
import os
import sys
import csv
import argparse
from datetime import datetime

ENV_FILE   = "migration_env.sh"
CSV_FILE   = "migration_users.csv"
PAGE_SIZE  = 50   # APIC'e gönderilen --limit değeri

CSV_FIELDS = [
    # SOURCE — APIC Local Registry (eski sistem)
    "username",
    "consumer_org",
    "source_email",
    # TARGET — Keycloak (yeni sistem)
    "target_email",
    "kc_user_created",
    "apic_email_parked",
    "apic_jit_done",
    "org_owner_xfrd",
    # DURUM
    "migrated",
    "migrated_at",
]


# ------------------------------------------------------------------------------
# ORTAM
# ------------------------------------------------------------------------------

def load_env():
    """
    migration_env.sh dosyasını okuyarak 'export KEY="VALUE"' satırlarını
    os.environ'a yükler. Dosya yoksa hata verip çıkar.
    """
    if not os.path.exists(ENV_FILE):
        print(f"--> [HATA] '{ENV_FILE}' bulunamadı! Önce 00_setup_env.py'i çalıştırın.")
        sys.exit(1)
    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                kv = line[7:].split("=", 1)
                if len(kv) == 2:
                    os.environ[kv[0]] = kv[1].strip('"\'')


# ------------------------------------------------------------------------------
# APIC SORGULARI
# ------------------------------------------------------------------------------

def _apic_run(cmd):
    """APIC CLI komutunu çalıştırır, JSON parse eder. Hata durumunda None döner."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        print(f"--> [HATA] APIC komutu başarısız: {' '.join(cmd)}\n    {err}")
        return None
    except json.JSONDecodeError:
        print(f"--> [HATA] APIC yanıtı JSON değil: {' '.join(cmd)}")
        return None


def fetch_consumer_orgs_page(server, prov_org, catalog, limit, offset):
    """
    Belirtilen limit/offset ile bir sayfa consumer org döndürür.
    Yanıt: {"total_results": N, "results": [...]} şeklinde beklenir.
    """
    cmd = [
        "apic", "consumer-orgs:list",
        "-s", server, "-o", prov_org, "-c", catalog,
        "--format", "json", "--output", "-",
        "--limit",  str(limit),
        "--offset", str(offset),
    ]
    return _apic_run(cmd)


def fetch_all_consumer_orgs(server, prov_org, catalog, page_size):
    """
    Tüm consumer org'ları paging ile çeker, birleşik liste döndürür.
    Her sayfa sonunda ilerleme ekrana basılır.
    """
    all_orgs  = []
    offset    = 0
    page_no   = 0
    total     = None

    while True:
        page_no += 1
        print(f"--> [APIC] Sayfa {page_no} alınıyor (offset={offset}, limit={page_size})...")

        data = fetch_consumer_orgs_page(server, prov_org, catalog, page_size, offset)
        if data is None:
            print("--> [HATA] Sayfa alınamadı, durduruluyor.")
            break

        results = data.get("results", [])
        if total is None:
            total = data.get("total_results", 0)
            print(f"--> [BİLGİ] Toplam consumer org sayısı: {total}")

        all_orgs.extend(results)
        print(f"--> [BİLGİ] Bu sayfada {len(results)} org alındı. Toplam alınan: {len(all_orgs)}/{total}")

        # Son sayfaya ulaştık mı?
        offset += page_size
        if offset >= total or not results:
            break

    return all_orgs


def fetch_owner_info(server, prov_org, catalog, consumer_org_name, owner_url):
    """
    Bir consumer org'un owner kullanıcısını members:list ile çeker.
    owner_url, consumer-orgs:list yanıtından gelen değerdir — eşleştirme
    için kullanılır.
    Returns: {"username": ..., "email": ...} veya None
    """
    cmd_members = [
        "apic", "members:list", "--scope", "consumer-org",
        "-s", server, "-o", prov_org, "-c", catalog,
        "--consumer-org", consumer_org_name,
        "--format", "json", "--output", "-",
    ]
    members_data = _apic_run(cmd_members)
    if not members_data:
        return None

    items = members_data.get("results", [])

    keycloak_registry = os.environ.get("KEYCLOAK_REGISTRY_NAME", "keycluk")

    # Önce owner_url ile tam eşleştir, bulamazsan role=owner'ı dene.
    # Owner zaten Keycloak kullanıcısıysa bu org daha önce migrate edilmiş — atla.
    for member in items:
        user = member.get("user", {})
        if user.get("identity_provider") == keycloak_registry:
            print(f"--> [BİLGİ] '{consumer_org_name}' zaten Keycloak kullanıcısına ait. Atlanıyor.")
            return None
        if owner_url and user.get("url") == owner_url:
            # APIC'in "name" alanı users:get komutuna verilmesi gereken case-preserved
            # değerdir. "username" küçük harfe normalize edilmiş olabilir ve
            # users:get ile sorgulandığında "Not found" hatasına neden olur.
            return {"username": user.get("name") or user.get("username", ""), "email": user.get("email", "")}

    for member in items:
        user = member.get("user", {})
        if member.get("role", "") == "owner":
            return {"username": user.get("name") or user.get("username", ""), "email": user.get("email", "")}

    print(f"--> [UYARI] '{consumer_org_name}' için owner üye bulunamadı.")
    return None


# ------------------------------------------------------------------------------
# CSV
# ------------------------------------------------------------------------------

def load_existing_csv():
    """
    Mevcut migration_users.csv dosyasını {username: row} dict olarak döndürür.
    Dosya yoksa boş dict döner. Yeniden çalıştırmalarda mevcut kayıtları
    korumak için kullanılır.
    """
    if not os.path.exists(CSV_FILE):
        return {}
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return {row["username"]: row for row in csv.DictReader(f)}


def write_csv(rows):
    """
    Tüm satırları (mevcut + yeni) migration_users.csv'ye yazar.
    Her çalıştırmada dosyanın tamamını yeniden yazar; bu nedenle
    load_existing_csv() ile eski kayıtlar önce belleğe alınıp
    new_rows listesine dahil edilir.
    """
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="APIC consumer org'larını ve owner kullanıcılarını CSV'ye aktarır."
    )
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE,
                        help=f"APIC paging limit (varsayılan: {PAGE_SIZE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Sadece ekrana yaz, CSV'ye kaydetme")
    args = parser.parse_args()

    load_env()

    server   = os.environ["APIC_SERVER"]
    prov_org = os.environ["PROV_ORG"]
    catalog  = os.environ["CATALOG"]

    print("\n==================================================")
    print("  CONSUMER ORG & OWNER DIŞA AKTARMA              ")
    print(f"  Sunucu  : {server}")
    print(f"  Org     : {prov_org}  |  Catalog: {catalog}")
    print(f"  Sayfa   : {args.page_size} org/sayfa")
    if args.dry_run:
        print("  MOD     : DRY-RUN (CSV'ye yazılmayacak)")
    print("==================================================\n")

    # Tüm consumer org'ları çek
    orgs = fetch_all_consumer_orgs(server, prov_org, catalog, args.page_size)
    if not orgs:
        print("--> [HATA] Hiç consumer org bulunamadı, çıkılıyor.")
        sys.exit(1)

    # MEVCUT CSV'Yİ HAFIZAYA AL (Sözlük Formatında)
    existing_users = load_existing_csv()

    skipped   = 0
    added     = 0
    failed    = 0
    already   = 0   # zaten Keycloak'ta olan org sayısı

    print(f"\n--> [BİLGİ] {len(orgs)} org için owner bilgisi alınıyor...\n")

    for org in orgs:
        org_name  = org.get("name", "")
        owner_url = org.get("owner_url", "")
        if not org_name:
            continue

        owner = fetch_owner_info(server, prov_org, catalog, org_name, owner_url)
        if owner is None:
            already += 1
            continue
        if not owner.get("username"):
            print(f"--> [UYARI] '{org_name}' owner username boş, atlanıyor.")
            failed += 1
            continue

        username = owner["username"]
        email    = owner["email"]

        # EĞER KULLANICI ZATEN HAFIZADAKİ CSV'DE VARSA HİÇ DOKUNMA
        if username in existing_users:
            print(f"--> [ATLA] '{username}' ({org_name}) zaten CSV'de kayıtlı.")
            skipped += 1
            continue

        # EĞER YENİ BİR KULLANICIYSA, ONU DA MEVCUT HAFIZAYA (SÖZLÜĞE) EKLE
        row = {
            "username":          username,
            "consumer_org":      org_name,
            "source_email":      "",
            "target_email":      email,
            "kc_user_created":   "false",
            "apic_email_parked": "false",
            "apic_jit_done":     "false",
            "org_owner_xfrd":    "false",
            "migrated":          "false",
            "migrated_at":       "",
        }
        existing_users[username] = row
        added += 1
        print(f"--> [+] {username:<30} | {org_name:<40} | {email}")

    print(f"\n--------------------------------------------------")
    print(f"  Toplam org      : {len(orgs)}")
    print(f"  Yeni kayıt      : {added}")
    print(f"  Zaten CSV'de    : {skipped}")
    print(f"  Zaten KC'de     : {already}")
    print(f"  Başarısız       : {failed}")
    print(f"--------------------------------------------------")

    if args.dry_run:
        print("\n--> [DRY-RUN] CSV'ye yazılmadı.")
    else:
        # MEVCUT + YENİ EKLENEN HER ŞEYİ (TÜM SÖZLÜĞÜ) DOSYAYA YAZ
        final_rows = list(existing_users.values())
        write_csv(final_rows)
        print(f"\n--> [BAŞARILI] {len(final_rows)} satır '{CSV_FILE}' dosyasına yazıldı.")
        print(f"    Tarih: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    print("==================================================\n")

if __name__ == "__main__":
    main()