#!/usr/bin/env python3.11
"""
00_export_consumer_orgs.py — APIC Consumer Org & Owner Dışa Aktarma

Çalıştırma sırası: 00  (02_setup_and_login.py'den SONRA, diğer her şeyden ÖNCE)

Tüm consumer org'ları APIC API'sinden sayfalama (paging) ile çeker,
her org'un owner kullanıcısını ayrıca sorgular ve sonuçları
migration_users.csv dosyasına yazar.

Bu CSV, 06_run_migration_batch.py tarafından migration input'u olarak kullanılır.

Kullanım:
  python 00_export_consumer_orgs.py              # tüm org'lar
  python 00_export_consumer_orgs.py --page-size 25   # sayfa boyutunu özelleştir
  python 00_export_consumer_orgs.py --dry-run    # sadece ekrana yaz, CSV'ye yazma
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
    "username",
    "consumer_org",
    "email_source",
    "email_target",
    "kc_created",
    "email_updated",
    "apic_provisioned",
    "migrated",
    "migrated_at",
]


# ------------------------------------------------------------------------------
# ORTAM
# ------------------------------------------------------------------------------

def load_env():
    if not os.path.exists(ENV_FILE):
        print(f"--> [HATA] '{ENV_FILE}' bulunamadı! Önce 02_setup_and_login.py'i çalıştırın.")
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


def fetch_owner_info(server, prov_org, catalog, consumer_org_name):
    """
    Bir consumer org'un owner kullanıcısını çeker.
    APIC, consumer-orgs:get yanıtında owner_url alanı verir; bu URL'den kullanıcı
    bilgisi alınır.
    Returns: {"username": ..., "email": ...} veya None
    """
    # 1. Consumer org detayını al → owner_url
    cmd_org = [
        "apic", "consumer-orgs:get", consumer_org_name,
        "-s", server, "-o", prov_org, "-c", catalog,
        "--format", "json", "--output", "-",
    ]
    org_data = _apic_run(cmd_org)
    if not org_data:
        return None

    owner_url = org_data.get("owner_url", "")
    if not owner_url:
        print(f"--> [UYARI] '{consumer_org_name}' için owner_url boş.")
        return None

    # 2. owner_url'den username ve email çek (members:list içinden owner'ı bul)
    cmd_members = [
        "apic", "members:list",
        "-s", server, "-o", prov_org, "-c", catalog,
        "--consumer-org", consumer_org_name,
        "--format", "json", "--output", "-",
    ]
    members_data = _apic_run(cmd_members)
    if not members_data:
        return None

    for member in members_data.get("results", []):
        user = member.get("user", {})
        if user.get("url") == owner_url or member.get("role", "") == "owner":
            return {
                "username": user.get("username", ""),
                "email":    user.get("email", ""),
            }

    print(f"--> [UYARI] '{consumer_org_name}' için owner üye bulunamadı.")
    return None


# ------------------------------------------------------------------------------
# CSV
# ------------------------------------------------------------------------------

def load_existing_csv():
    """Mevcut CSV'yi {username: row} dict olarak döndürür."""
    if not os.path.exists(CSV_FILE):
        return {}
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return {row["username"]: row for row in csv.DictReader(f)}


def write_csv(rows):
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

    existing = load_existing_csv()
    new_rows  = []
    skipped   = 0
    added     = 0
    failed    = 0

    print(f"\n--> [BİLGİ] {len(orgs)} org için owner bilgisi alınıyor...\n")

    for org in orgs:
        org_name = org.get("name", "")
        if not org_name:
            continue

        owner = fetch_owner_info(server, prov_org, catalog, org_name)
        if not owner or not owner["username"]:
            print(f"--> [UYARI] '{org_name}' owner'ı alınamadı, atlanıyor.")
            failed += 1
            continue

        username = owner["username"]
        email    = owner["email"]

        if username in existing:
            print(f"--> [ATLA] '{username}' ({org_name}) zaten CSV'de kayıtlı.")
            new_rows.append(existing[username])
            skipped += 1
            continue

        row = {
            "username":         username,
            "consumer_org":     org_name,
            "email_source":     email,
            "email_target":     "",
            "kc_created":       "false",
            "email_updated":    "false",
            "apic_provisioned": "false",
            "migrated":         "false",
            "migrated_at":      "",
        }
        new_rows.append(row)
        added += 1
        print(f"--> [+] {username:<30} | {org_name:<40} | {email}")

    print(f"\n--------------------------------------------------")
    print(f"  Toplam org    : {len(orgs)}")
    print(f"  Yeni kayıt    : {added}")
    print(f"  Zaten vardı   : {skipped}")
    print(f"  Başarısız     : {failed}")
    print(f"--------------------------------------------------")

    if args.dry_run:
        print("\n--> [DRY-RUN] CSV'ye yazılmadı.")
    else:
        write_csv(new_rows)
        print(f"\n--> [BAŞARILI] {len(new_rows)} satır '{CSV_FILE}' dosyasına yazıldı.")
        print(f"    Tarih: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    print("==================================================\n")


if __name__ == "__main__":
    main()
