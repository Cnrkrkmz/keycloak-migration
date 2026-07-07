#!/usr/bin/env python3.11
"""
migration_state.py — CSV tabanlı migration durum yöneticisi.

Her satır = 1 consumer org + o org'un owner kullanıcısı.
CSV ikiye bölünmüş gibi okunabilir:

  ── SOURCE (geçiş öncesi, APIC Local Registry) ──────────────────────
  username          — APIC kullanıcı adı
  consumer_org      — Kullanıcının sahibi olduğu Consumer Org
  src_email         — Geçiş öncesi orijinal e-posta (rollback için saklanır)

  ── TARGET (geçiş sonrası, Keycloak registry) ───────────────────────
  tgt_email         — Geçiş için APIC'e yazılan -old suffix'li e-posta
  kc_user_created   — [ADIM 1] Keycloak'ta kullanıcı oluşturuldu mu?
  apic_email_parked — [ADIM 2] APIC Local Registry'de e-posta -old yapıldı mı?
                       (Keycloak kullanıcısının orijinal adresle çakışmaması için)
  apic_jit_done     — [ADIM 3] Keycloak token'ı APIC'e POST edildi mi?
                       (APIC bu POST ile kendi içinde shadow user'ı JIT-provision eder;
                        kullanıcı "otomatik giriş yapmış" sayılır ve APIC kaydı açılır)
  org_owner_xfrd    — [ADIM 4] Consumer Org sahipliği Keycloak profiline devredildi mi?

  ── DURUM ────────────────────────────────────────────────────────────
  migrated          — Tüm 4 adım başarıyla tamamlandı mı?
  migrated_at       — Tamamlanma zaman damgası (YYYY-MM-DD HH:MM:SS), boş = henüz bitmedi
"""

import csv
import os
from datetime import datetime

CSV_FILE = "migration_users.csv"

FIELDS = [
    # SOURCE
    "username",
    "consumer_org",
    "src_email",
    # TARGET
    "tgt_email",
    "kc_user_created",
    "apic_email_parked",
    "apic_jit_done",
    "org_owner_xfrd",
    # DURUM
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


def add_user(username, consumer_org, src_email):
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
        "username":          username,
        "consumer_org":      consumer_org,
        "src_email":         src_email,
        "tgt_email":         "",
        "kc_user_created":   "false",
        "apic_email_parked": "false",
        "apic_jit_done":     "false",
        "org_owner_xfrd":    "false",
        "migrated":          "false",
        "migrated_at":       "",
    }
    rows.append(new_row)
    _write_all(rows)
    print(f"--> [CSV] '{username}' kaydı oluşturuldu (src_email: {src_email}).")
    return new_row


def update_flag(username, flag, value=True):
    """
    Belirli bir flag'i günceller.
    Kabul edilen flag'ler: kc_user_created, apic_email_parked,
                           apic_jit_done, org_owner_xfrd, migrated
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


def update_email_target(username, tgt_email):
    """
    -old suffix'iyle oluşturulan hedef e-postayı CSV'ye yazar
    (04_update_apic_email.py çağırır).
    """
    rows = load_users()
    for row in rows:
        if row["username"] == username:
            row["tgt_email"] = tgt_email
            break
    _write_all(rows)
    print(f"--> [CSV] '{username}' tgt_email güncellendi → {tgt_email}")


def mark_migrated(username):
    """Tüm adımlar tamamlandığında kullanıcıyı migrated=true + zaman damgası yapar."""
    rows = load_users()
    for row in rows:
        if row["username"] == username:
            row["migrated"]    = "true"
            row["migrated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _write_all(rows)
            print(f"--> [CSV] '{username}' → migrated=true [{row['migrated_at']}]")
            return
    print(f"--> [UYARI] CSV'de '{username}' bulunamadı, migrated işaretlenemedi.")


def mark_rollback(username):
    """
    Rollback tamamlandığında tüm flag ve tgt_email'i sıfırlar.
    src_email korunur — tekrar deneme için.
    """
    rows = load_users()
    for row in rows:
        if row["username"] == username:
            row["tgt_email"]         = ""
            row["kc_user_created"]   = "false"
            row["apic_email_parked"] = "false"
            row["apic_jit_done"]     = "false"
            row["org_owner_xfrd"]    = "false"
            row["migrated"]          = "false"
            row["migrated_at"]       = ""
            break
    _write_all(rows)
    print(f"--> [CSV] '{username}' rollback tamamlandı, tüm flag'ler sıfırlandı.")


# ------------------------------------------------------------------------------
# DURUM RAPORU
# ------------------------------------------------------------------------------

def print_status():
    """
    Tüm kullanıcıların migration durumunu SOURCE | TARGET formatında
    tablo olarak ekrana basar.

    Gösterim:
      SOURCE  : username  consumer_org  src_email
      TARGET  : tgt_email  kc  email  jit  xfrd
      DURUM   : migrated  migrated_at
    Flag değerleri okunabilirlik için kısaltılır: true→Y  false→-
    """
    rows = load_users()
    if not rows:
        print(f"--> [BİLGİ] '{CSV_FILE}' boş veya mevcut değil.")
        return

    # Sütun tanımları  (başlık_kısa, alan_adı)
    src_cols = [
        ("username",     "username"),
        ("consumer_org", "consumer_org"),
        ("src_email",    "src_email"),
    ]
    tgt_cols = [
        ("tgt_email",    "tgt_email"),
        ("kc",           "kc_user_created"),
        ("email_p",      "apic_email_parked"),
        ("jit",          "apic_jit_done"),
        ("xfrd",         "org_owner_xfrd"),
    ]
    st_cols = [
        ("ok",           "migrated"),
        ("migrated_at",  "migrated_at"),
    ]

    def _val(row, field):
        v = row.get(field, "")
        if v == "true":  return "Y"
        if v == "false": return "-"
        return v

    def _colw(cols):
        return [
            max(len(hdr), max((len(_val(r, fld)) for r in rows), default=0))
            for hdr, fld in cols
        ]

    sw = _colw(src_cols)
    tw = _colw(tgt_cols)
    dw = _colw(st_cols)

    def _row_str(cols, widths, row):
        return "  ".join(_val(row, fld).ljust(widths[i]) for i, (_, fld) in enumerate(cols))

    def _hdr_str(cols, widths):
        return "  ".join(hdr.ljust(widths[i]) for i, (hdr, _) in enumerate(cols))

    src_w = sum(sw) + 2 * (len(sw) - 1)
    tgt_w = sum(tw) + 2 * (len(tw) - 1)
    st_w  = sum(dw) + 2 * (len(dw) - 1)
    total = src_w + 4 + tgt_w + 4 + st_w

    print()
    # Grup başlıkları
    print(f"  {'SOURCE':<{src_w}}  |  {'TARGET':<{tgt_w}}  |  {'DURUM':<{st_w}}")
    print(f"  {'-' * src_w}  |  {'-' * tgt_w}  |  {'-' * st_w}")
    # Sütun başlıkları
    print(f"  {_hdr_str(src_cols, sw)}  |  {_hdr_str(tgt_cols, tw)}  |  {_hdr_str(st_cols, dw)}")
    print(f"  {'=' * total}")
    # Satırlar
    for row in rows:
        print(f"  {_row_str(src_cols, sw, row)}  |  {_row_str(tgt_cols, tw, row)}  |  {_row_str(st_cols, dw, row)}")
    print()


if __name__ == "__main__":
    print_status()
