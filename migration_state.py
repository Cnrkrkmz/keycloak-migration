#!/usr/bin/env python3.11
"""
migration_state.py — CSV tabanlı migration durum yöneticisi.

Her consumer org için tek bir satır tutulur (1 org = 1 owner kullanıcısı).
CSV dosyası hiçbir zaman sıfırlanmaz — her gün yeni kayıtlar eklenir,
tamamlananlar migrated=true + migrated_at tarih damgasıyla arşivlenir.

CSV sütunları:
  username          — APIC kaynak kullanıcı adı
  consumer_org      — Kullanıcının ait olduğu Consumer Org
  email_source      — Migrasyon öncesi orijinal e-posta (SOURCE, rollback için)
  email_target      — Migrasyon sonrası -old suffix'li e-posta (TARGET, 04 yazar)
  kc_created        — 03: Keycloak'ta kullanıcı yaratıldı mı?
  email_updated     — 04: APIC'te e-posta -old suffix'iyle güncellendi mi?
  apic_provisioned  — 05: APIC'te OIDC login tetiklendi / shadow user oluşturuldu mu?
  migrated          — Tüm adımlar başarıyla tamamlandı mı?
  migrated_at       — Tamamlanma tarihi/saati (YYYY-MM-DD HH:MM:SS), boş = henüz bitmedi
"""

import csv
import os
from datetime import datetime

CSV_FILE = "migration_users.csv"

FIELDS = [
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
# OKUMA
# ------------------------------------------------------------------------------

def load_users():
    """CSV dosyasındaki tüm kullanıcıları dict listesi olarak döndürür."""
    if not os.path.exists(CSV_FILE):
        return []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def get_user(username):
    """Belirli bir kullanıcıyı CSV'den bulup döndürür. Yoksa None."""
    for row in load_users():
        if row["username"] == username:
            return row
    return None


def get_pending_users():
    """migrated=false olan kullanıcıları döndürür."""
    return [u for u in load_users() if u.get("migrated", "false").lower() != "true"]


# ------------------------------------------------------------------------------
# YAZMA
# ------------------------------------------------------------------------------

def _write_all(rows):
    """Tüm satırları CSV'ye geri yazar (sırayı korur)."""
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def add_user(username, consumer_org, email_source):
    """
    Yeni bir kullanıcıyı CSV'ye ekler.
    Kullanıcı zaten varsa ekleme yapmaz, mevcut satırı döndürür.
    """
    rows = load_users()
    for row in rows:
        if row["username"] == username:
            print(f"--> [BİLGİ] '{username}' zaten CSV'de kayıtlı, atlanıyor.")
            return row

    new_row = {
        "username":         username,
        "consumer_org":     consumer_org,
        "email_source":     email_source,
        "email_target":     "",
        "kc_created":       "false",
        "email_updated":    "false",
        "apic_provisioned": "false",
        "migrated":         "false",
        "migrated_at":      "",
    }
    rows.append(new_row)
    _write_all(rows)
    print(f"--> [CSV] '{username}' kaydı oluşturuldu (source: {email_source}).")
    return new_row


def update_flag(username, flag, value=True):
    """
    Belirli bir flag'i (kc_created, email_updated, apic_provisioned, migrated)
    günceller.
    """
    if flag not in FIELDS:
        raise ValueError(f"Geçersiz flag: '{flag}'. Geçerli değerler: {FIELDS}")

    rows = load_users()
    found = False
    for row in rows:
        if row["username"] == username:
            row[flag] = "true" if value else "false"
            found = True
            break

    if not found:
        print(f"--> [UYARI] CSV'de '{username}' bulunamadı, flag güncellenemedi.")
        return

    _write_all(rows)


def update_email_target(username, email_target):
    """
    Migration sırasında -old suffix'iyle oluşturulan hedef e-postayı
    canlı olarak CSV'ye yazar (04_update_apic_email.py çağırır).
    """
    rows = load_users()
    for row in rows:
        if row["username"] == username:
            row["email_target"] = email_target
            break
    _write_all(rows)
    print(f"--> [CSV] '{username}' email_target güncellendi → {email_target}")


def mark_migrated(username):
    """Tüm adımlar tamamlandığında kullanıcıyı migrated=true + tarih damgası yapar."""
    rows = load_users()
    for row in rows:
        if row["username"] == username:
            row["migrated"]    = "true"
            row["migrated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            break
    _write_all(rows)
    print(f"--> [CSV] '{username}' → migrated=true [{row['migrated_at']}]")


def mark_rollback(username):
    """
    Rollback tamamlandığında tüm flag ve email_target'ı sıfırlar.
    Kullanıcı CSV'de kalır — tekrar deneme için.
    """
    rows = load_users()
    for row in rows:
        if row["username"] == username:
            row["email_target"]     = ""
            row["kc_created"]       = "false"
            row["email_updated"]    = "false"
            row["apic_provisioned"] = "false"
            row["migrated"]         = "false"
            row["migrated_at"]      = ""
            break
    _write_all(rows)
    print(f"--> [CSV] '{username}' rollback tamamlandı, tüm flag'ler sıfırlandı.")


# ------------------------------------------------------------------------------
# DURUM RAPORU
# ------------------------------------------------------------------------------

def print_status():
    """Tüm kullanıcıların migration durumunu tablo olarak ekrana basar."""
    rows = load_users()
    if not rows:
        print(f"--> [BİLGİ] '{CSV_FILE}' boş veya mevcut değil.")
        return

    col_w = [max(len(f), max((len(r.get(f, "")) for r in rows), default=0)) for f in FIELDS]
    header = "  ".join(f.ljust(col_w[i]) for i, f in enumerate(FIELDS))
    sep    = "  ".join("-" * w for w in col_w)
    print("\n" + header)
    print(sep)
    for row in rows:
        print("  ".join(row.get(f, "").ljust(col_w[i]) for i, f in enumerate(FIELDS)))
    print()


if __name__ == "__main__":
    print_status()
