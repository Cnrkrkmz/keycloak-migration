#!/usr/bin/env python3.11
"""
04_run_migration.py — Toplu migration orkestratörü.

CSV dosyasındaki (migration_users.csv) migrate edilmemiş kullanıcıları
10'ar kullanıcılık batch'ler halinde işler.

Her kullanıcı için sırayla şu adımları çalıştırır:
  1. step_01_create_kc_user.py    → Keycloak'ta kullanıcı yarat
  2. step_02_park_apic_email.py   → APIC e-postasını -old yap
  3. step_03_jit_provision.py     → APIC JIT provision (OIDC login)
  4. step_04_transfer_org.py      → Consumer Org sahipliğini Keycloak profiline devret

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
import subprocess
import argparse

from migration_state import load_users, get_pending_users, print_status, write_status_report

ENV_FILE   = "migration_env.sh"
BATCH_SIZE = 10


# ------------------------------------------------------------------------------
# YARDIMCI — Tek kullanıcı için tüm adımları çalıştır
# ------------------------------------------------------------------------------

def _run_step(script, username, step_label):
    """
    Bir migration adımını subprocess olarak çalıştırır.
    Başarılıysa True, başarısızsa False döner.
    """
    print(f"    [{step_label}] python {script} {username} ...", end=" ", flush=True)
    result = subprocess.run(
        [sys.executable, script, username],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("OK")
        return True
    else:
        print("BAŞARISIZ")
        # Hata detayını girintili bas
        err = (result.stderr or result.stdout or "").strip()
        for line in err.splitlines():
            print(f"       {line}")
        return False


def migrate_user(username, dry_run=False):
    """
    Tek bir kullanıcı için 3 adımlı migration pipeline'ını çalıştırır.
    dry_run=True ise hiçbir şey çalıştırmadan sadece adımları listeler.
    Başarı True, herhangi bir adım başarısız olursa False döner.
    """
    steps = [
        ("step_01_create_kc_user.py",  "1/4 KC_CREATE "),
        ("step_02_park_apic_email.py", "2/4 EMAIL_UPD "),
        ("step_03_jit_provision.py",   "3/4 JIT_PROV  "),
        ("step_04_transfer_org.py",    "4/4 ORG_XFER  "),
    ]

    if dry_run:
        for script, label in steps:
            print(f"    [{label}] python {script} {username}  [DRY-RUN]")
        return True

    for script, label in steps:
        if not _run_step(script, username, label):
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
            org      = user.get("consumer_org", "-")
            print(f"  >> {username} [{org}]")

            ok = migrate_user(username, dry_run=dry_run)
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
