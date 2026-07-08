#!/usr/bin/env python3.11
"""
migration_state.py — CSV tabanlı migration durum yöneticisi.

Her satır = 1 consumer org + o org'un owner kullanıcısı.

  ── SOURCE (APIC Local Registry — eski sistem) ────────────────────
  username          — APIC kullanıcı adı
  consumer_org      — Kullanıcının sahibi olduğu Consumer Org
  source_email      — Migration süresince APIC'e yazılan geçici -old@
                      e-posta. Rollback'te kaldırılır, target_email geri yazılır.

  ── TARGET (Keycloak — yeni sistem) ──────────────────────────────
  target_email      — Kullanıcının gerçek e-postası. Keycloak'a bu yazılır.
  kc_user_created   — [ADIM 1] Keycloak'ta kullanıcı oluşturuldu mu?
  apic_email_parked — [ADIM 2] APIC'te e-posta -old@ yapıldı mı?
  apic_jit_done     — [ADIM 3] Keycloak token'ı APIC'e POST edildi mi?
                       (APIC bu POST ile kendi içinde shadow user'ı JIT-provision eder)
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


def get_users_by_org(consumer_org):
    """
    Belirli bir consumer org adına ait tüm kullanıcıları döndürür.
    Büyük/küçük harf duyarsız karşılaştırma yapar.
    Org CSV'de yoksa boş liste döner.
    """
    return [u for u in load_users() if u.get("consumer_org", "").lower() == consumer_org.lower()]


# ------------------------------------------------------------------------------
# YAZMA
# ------------------------------------------------------------------------------

def _write_all(rows):
    """Tüm satırları CSV'ye geri yazar (sırayı korur)."""
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def add_user(username, consumer_org, target_email):
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
        "source_email":      "",
        "target_email":      target_email,
        "kc_user_created":   "false",
        "apic_email_parked": "false",
        "apic_jit_done":     "false",
        "org_owner_xfrd":    "false",
        "migrated":          "false",
        "migrated_at":       "",
    }
    rows.append(new_row)
    _write_all(rows)
    print(f"--> [CSV] '{username}' kaydı oluşturuldu (target_email: {target_email}).")
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


def update_source_email(username, source_email):
    """
    APIC'e yazılan -old@ park e-postasını CSV'ye yazar
    (step_02_park_apic_email.py çağırır).
    """
    rows = load_users()
    for row in rows:
        if row["username"] == username:
            row["source_email"] = source_email
            break
    _write_all(rows)
    print(f"--> [CSV] '{username}' source_email güncellendi → {source_email}")


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
    Rollback tamamlandığında tüm flag ve source_email'i sıfırlar.
    target_email korunur — tekrar deneme için.
    """
    rows = load_users()
    for row in rows:
        if row["username"] == username:
            row["source_email"]      = ""
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
      SOURCE  : username  consumer_org  source_email
      TARGET  : target_email  kc  email_p  jit  xfrd
      DURUM   : migrated  migrated_at
    Flag değerleri okunabilirlik için kısaltılır: true→Y  false→-
    """
    rows = load_users()
    if not rows:
        print(f"--> [BİLGİ] '{CSV_FILE}' boş veya mevcut değil.")
        return

    # Sütun tanımları  (başlık_kısa, alan_adı)
    src_cols = [
        ("username",      "username"),
        ("consumer_org",  "consumer_org"),
        ("source_email",  "source_email"),
    ]
    tgt_cols = [
        ("target_email", "target_email"),
        ("kc",             "kc_user_created"),
        ("email_p",        "apic_email_parked"),
        ("jit",            "apic_jit_done"),
        ("xfrd",           "org_owner_xfrd"),
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


def write_status_report(filepath=None):
    """
    Migration durumunu okunabilir bir metin dosyasına yazar.
    Her bölüm (SOURCE / TARGET / DURUM) alt alta gelir — terminal genişliğinden bağımsız.

    filepath verilmezse 'migration_report.txt' kullanılır.
    Dosya her çağrıda üzerine yazılır (son durum her zaman güncel).
    """
    if filepath is None:
        filepath = "migration_report.txt"

    rows = load_users()
    from datetime import datetime as _dt
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = []
    lines.append(f"Migration Durum Raporu — {now}")
    lines.append(f"Toplam kayıt: {len(rows)}")
    lines.append("")

    done    = sum(1 for r in rows if r.get("migrated") == "true")
    pending = len(rows) - done
    lines.append(f"  Tamamlanan : {done}")
    lines.append(f"  Bekleyen   : {pending}")
    lines.append("")

    # Sütun etiketleri ve CSV alan adları
    col_defs = [
        ("username",      "username"),
        ("consumer_org",  "consumer_org"),
        ("source_email",  "source_email"),
        ("target_email",  "target_email"),
        ("kc",             "kc_user_created"),
        ("email_p",        "apic_email_parked"),
        ("jit",            "apic_jit_done"),
        ("xfrd",           "org_owner_xfrd"),
        ("ok",             "migrated"),
        ("migrated_at",    "migrated_at"),
    ]

    def _val(row, field):
        v = row.get(field, "")
        if v == "true":  return "Y"
        if v == "false": return "-"
        return v

    widths = [
        max(len(hdr), max((len(_val(r, fld)) for r in rows), default=0))
        for hdr, fld in col_defs
    ]

    sep   = "  ".join("-" * w for w in widths)
    hdr   = "  ".join(hdr.ljust(widths[i]) for i, (hdr, _) in enumerate(col_defs))
    lines.append(hdr)
    lines.append(sep)

    for row in rows:
        line = "  ".join(_val(row, fld).ljust(widths[i]) for i, (_, fld) in enumerate(col_defs))
        lines.append(line)

    lines.append("")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return filepath


if __name__ == "__main__":
    print_status()
