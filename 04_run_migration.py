#!/usr/bin/env python3.11
"""
04_run_migration.py — Toplu migration orkestratörü.

CSV dosyasındaki (migration_users.csv) migrate edilmemiş kullanıcıları
10'ar kullanıcılık batch'ler halinde işler.

Her kullanıcı için sırayla şu adımları çalıştırır (migration_steps.py):
  1. step_01_create_kc_user    → Keycloak'ta kullanıcı yarat
  2. step_02_park_apic_email   → APIC e-postasını -old yap
  3. step_03_jit_provision     → APIC JIT provision (OIDC login)
  4. step_04_transfer_org      → Consumer Org sahipliğini Keycloak profiline devret

Her 10 kullanıcı tamamlandığında özet rapor ekrana basılır.
Batch içinde bir kullanıcı başarısız olursa o kullanıcı atlanır
(migrated=false kalır) ve bir sonrakiyle devam edilir.

Kullanım:
  python 04_run_migration.py                    # tüm pending kullanıcılar
  python 04_run_migration.py --batch-size 5     # özel batch boyutu
  python 04_run_migration.py --dry-run          # adımları yazdır, çalıştırma
"""

import os
import sys
import argparse

from migration_state import get_pending_users, write_status_report
from migration_steps import (
    step_01_create_kc_user,
    step_02_park_apic_email,
    step_03_jit_provision,
    step_04_transfer_org,
)

ENV_FILE   = "migration_env.sh"
BATCH_SIZE = 10


# ------------------------------------------------------------------------------
# YARDIMCI — Tek kullanıcı için tüm adımları çalıştır
# ------------------------------------------------------------------------------

def migrate_user(username, consumer_org="", dry_run=False):
    """
    Tek bir kullanıcı için 4 adımlı migration pipeline'ını sırasıyla çalıştırır.
    Her adım migration_steps.py'den doğrudan import edilip çağrılır (subprocess yok).
    dry_run=True ise adım adım hangi fonksiyonun çalışacağını listeler, değişiklik yapmaz.
    Başarıda True, herhangi bir adım başarısız olursa False döner.
    """
    steps = [
        ("1/4 KC_CREATE ", step_01_create_kc_user,  [username, consumer_org]),
        ("2/4 EMAIL_UPD ", step_02_park_apic_email, [username]),
        ("3/4 JIT_PROV  ", step_03_jit_provision,   [username]),
        ("4/4 ORG_XFER  ", step_04_transfer_org,    [username]),
    ]

    if dry_run:
        for label, fn, _ in steps:
            print(f"    [{label}] {fn.__name__}({username})  [DRY-RUN]")
        return True

    for label, fn, args in steps:
        print(f"    [{label}] {fn.__name__}({username}) ...", end=" ", flush=True)
        ok = fn(*args)
        if ok:
            print("OK")
        else:
            print("BAŞARISIZ")
            return False
    return True


# ------------------------------------------------------------------------------
# ANA BATCH DÖNGÜSÜ
# ------------------------------------------------------------------------------

def run_batch(batch_size=BATCH_SIZE, dry_run=False):
    pending = get_pending_users()

    if not pending:
        print("--> [BİLGİ] Migration gereken kullanıcı yok.")
        rpt = write_status_report()
        print(f"--> [BİLGİ] Güncel durum: {rpt}")
        return

    total      = len(pending)
    success_ct = 0
    fail_ct    = 0
    batch_no   = 0

    print(f"\n{'='*60}")
    print(f"  BATCH MİGRASYON BAŞLADI")
    print(f"  Toplam kullanıcı : {total}")
    print(f"  Batch boyutu     : {batch_size}")
    if dry_run:
        print("  MOD              : DRY-RUN (hiçbir şey değiştirilmez)")
    print(f"{'='*60}\n")

    # Kullanıcıları batch_size'lık gruplara böl
    for batch_start in range(0, total, batch_size):
        batch_no  += 1
        batch      = pending[batch_start : batch_start + batch_size]
        b_success  = 0
        b_fail     = 0
        b_failed_users = []

        print(f"--- BATCH {batch_no} ({len(batch)} kullanıcı) ---")

        for user in batch:
            username = user["username"]
            org      = user.get("consumer_org", "")
            print(f"  >> {username} [{org or '-'}]")

            ok = migrate_user(username, consumer_org=org, dry_run=dry_run)
            if ok:
                b_success += 1
                success_ct += 1
            else:
                b_fail += 1
                fail_ct += 1
                b_failed_users.append(username)

        # Her batch sonunda özet
        print(f"\n  Batch {batch_no} özeti: {b_success} başarılı / {b_fail} başarısız")
        if b_failed_users:
            print(f"  Başarısız: {', '.join(b_failed_users)}")

        # Canlı CSV durumunu dosyaya yaz
        rpt = write_status_report()
        print(f"\n  [SNAPSHOT] Batch {batch_no} sonu → {rpt}")

    # Genel özet
    print(f"\n{'='*60}")
    print(f"  BATCH MİGRASYON TAMAMLANDI")
    print(f"  Başarılı : {success_ct} / {total}")
    print(f"  Başarısız: {fail_ct} / {total}")
    print(f"{'='*60}\n")

    if fail_ct > 0:
        sys.exit(1)


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="migration_users.csv'deki kullanıcıları batch olarak migrate eder.",
        prog="04_run_migration.py"
    )
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE,
        help=f"Bir batch'teki kullanıcı sayısı (varsayılan: {BATCH_SIZE})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Adımları listele, gerçekten çalıştırma"
    )
    args = parser.parse_args()

    run_batch(batch_size=args.batch_size, dry_run=args.dry_run)