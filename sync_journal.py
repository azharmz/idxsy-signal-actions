#!/usr/bin/env python3
"""
IDXSY Zeta Journal Sync (replaces manual "scrape dashboard -> XLSX -> upload" step)
====================================================================================
Fetch data status final trade (TP HIT / SL HIT / EXPIRED) dari Zeta AI, lalu
replace `trades_data.payload.lastXlsxRows` dengan itu.

Dua sumber, PREFER yang member (lebih lengkap):

1. MEMBER (https://member.zeta-ai.pro/api/signals) -- butuh cookie
   `zeta_member=...` (secret ZETA_MEMBER_COOKIE). INI LEBIH LENGKAP --
   ditemukan lewat perbandingan langsung: endpoint publik ternyata SUKA
   nge-drop sebagian record (terutama SL_HIT, berhenti total sejak
   2026-06-26 di versi publik, padahal di member ada terus). Contoh nyata:
   PACK (SL_HIT, 2026-07-21) ada di member, TIDAK ADA di publik.

2. PUBLIK (https://idx-journal.zeta-ai.pro/api/idx-signals.json) -- fallback
   kalau ZETA_MEMBER_COOKIE belum di-set / gagal auth. TANPA login, tapi
   datanya gak lengkap (lihat poin 1).

PENTING soal cookie member: namanya mengandung bulan ("zetamemberjuly2026"),
kemungkinan besar ROTATE tiap bulan. Kalau tiba-tiba gagal auth, cek dulu
apakah cookie ini perlu di-update manual (buka member.zeta-ai.pro di browser,
DevTools > Network > cari request /api/signals > copy value cookie
`zeta_member` yang baru > update GitHub Secret ZETA_MEMBER_COOKIE).

Kenapa ini yang punya status SL HIT/EXPIRED (bukan Telegram):
  Grup Telegram Zeta AI cuma post notifikasi kalau profit (TP HIT/PROFIT
  LOCKED/PROFIT RUNNING) -- SL HIT dan EXPIRED gak pernah dipost publik.

Kenapa REPLACE (bukan APPEND/dedupe kayak signals[]/results[]):
  Ini snapshot LENGKAP tiap kali di-fetch (bukan incremental), sama seperti
  upload XLSX manual yang juga selalu replace `STATE.lastXlsxRows` secara
  keseluruhan (lihat handleXlsxFile() di idx_signal.html).

Gak perlu ubah idx_signal.html lagi -- checkAndSyncFromCloud() sudah
dipatch sebelumnya untuk otomatis re-merge pakai payload.lastXlsxRows yang
mana pun, setiap kali app di-load dari cloud.
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta

import requests
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("journal-sync")

MEMBER_API_URL = "https://member.zeta-ai.pro/api/signals"
PUBLIC_API_URL = "https://idx-journal.zeta-ai.pro/api/idx-signals.json"

ZETA_MEMBER_COOKIE = os.environ.get("ZETA_MEMBER_COOKIE")  # opsional, format: "zeta_member=xxxxx"

# Konversi status dari format API (underscore) ke format yang dipakai
# mergeWithXlsxData() di idx_signal.html (spasi) -- PENTING, kalau salah
# format, perbandingan string `finalStatus !== 'TP HIT'` di JS bakal gagal
# match dan status gak akan pernah dianggap final/otoritatif.
STATUS_MAP = {
    "TP_HIT": "TP HIT",
    "SL_HIT": "SL HIT",
    "EXPIRED": "EXPIRED",
}


def wib_string_to_wita_date(date_str):
    """Convert timestamp mentah dari API Zeta (WIB -- terverifikasi lewat cross-check
    PACK msg_id 6954: member API bilang 12:27, verified true UTC+7/WIB = 12:29, MATCH;
    kalau WITA harusnya 13:29) ke tanggal WITA (+1 jam), biar konsisten sama konvensi
    yang dipakai trades_data.payload.signals[]/results[] (WITA, dari upload manual
    browser user sendiri). PENTING: cuma date-part yang dipakai di xlsxRow, tapi tetap
    harus dikonversi dulu SEBELUM diambil tanggalnya, biar gak salah hari buat sinyal
    yang deket tengah malam."""
    if not date_str:
        return None
    try:
        naive = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return date_str[:10] if len(date_str) >= 10 else None
    wita = naive + timedelta(hours=1)
    return wita.strftime("%Y-%m-%d")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_USER_ID = os.environ["SUPABASE_USER_ID"]

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def fetch_member_signals():
    """Fetch dari member.zeta-ai.pro/api/signals -- lebih lengkap, butuh cookie."""
    cookie_value = ZETA_MEMBER_COOKIE
    if cookie_value and "=" not in cookie_value:
        # Jaga-jaga kalau secret cuma isi value-nya doang (tanpa "zeta_member=" prefix)
        cookie_value = f"zeta_member={cookie_value}"
    resp = requests.get(
        MEMBER_API_URL,
        headers={"Cookie": cookie_value},
        timeout=30,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"Auth gagal ({resp.status_code}) ke member API. Cookie ZETA_MEMBER_COOKIE "
            f"kemungkinan sudah expired (namanya mengandung nama bulan -- kemungkinan "
            f"rotate tiap bulan). Cek ulang value cookie terbaru lewat DevTools di "
            f"member.zeta-ai.pro, lalu update GitHub Secret ZETA_MEMBER_COOKIE."
        )
    resp.raise_for_status()
    return resp.json()


def fetch_public_journal():
    """Fetch dari idx-journal.zeta-ai.pro -- fallback, publik, TANPA login, tapi
    datanya gak selengkap versi member (banyak record ke-drop, terutama SL_HIT)."""
    resp = requests.get(PUBLIC_API_URL, timeout=30)
    resp.raise_for_status()
    return resp.json().get("completed", [])


def to_xlsx_row_member(entry, uid):
    """Konversi 1 entry dari member API ke bentuk row yang dikonsumsi
    mergeWithXlsxData(): {_uid, date, symbol, decision, entry, sl, tp, status, return_pct}."""
    raw_status = (entry.get("status") or "").strip()
    status = STATUS_MAP.get(raw_status, raw_status)

    profit_pct = entry.get("profit_pct")
    return_pct = f"{profit_pct:+.1f}%" if profit_pct is not None else None

    date_raw = entry.get("timestamp") or ""
    date_only = wib_string_to_wita_date(date_raw)

    return {
        "_uid": uid,
        "date": date_only,
        "symbol": (entry.get("symbol") or "").strip().upper(),
        "decision": entry.get("decision"),
        "entry": entry.get("close_price"),  # field ini isinya harga ENTRY, penamaan agak menyesatkan
        "sl": entry.get("stop_loss"),
        "tp": entry.get("take_profit"),
        "status": status,
        "return_pct": return_pct,
    }


def to_xlsx_row_public(entry, uid):
    """Konversi 1 entry dari public API's completed[] (struktur field beda dikit
    dari member API: 'date' bukan 'timestamp', 'entry'/'sl'/'tp' bukan
    'close_price'/'stop_loss'/'take_profit')."""
    raw_status = (entry.get("status") or "").strip()
    status = STATUS_MAP.get(raw_status, raw_status)

    profit_pct = entry.get("profit_pct")
    return_pct = f"{profit_pct:+.1f}%" if profit_pct is not None else None

    date_raw = entry.get("date") or ""
    date_only = wib_string_to_wita_date(date_raw)

    return {
        "_uid": uid,
        "date": date_only,
        "symbol": (entry.get("symbol") or "").strip().upper(),
        "decision": entry.get("decision"),
        "entry": entry.get("entry"),
        "sl": entry.get("sl"),
        "tp": entry.get("tp"),
        "status": status,
        "return_pct": return_pct,
    }


def load_current_payload():
    resp = (
        sb.table("trades_data")
        .select("payload")
        .eq("user_id", SUPABASE_USER_ID)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    p = rows[0]["payload"] if (rows and rows[0].get("payload")) else {}
    p.setdefault("signals", [])
    p.setdefault("results", [])
    p.setdefault("regimes", [])
    p.setdefault("others", [])
    p.setdefault("trades", [])
    p.setdefault("filename", "telegram-auto-ingest")
    p.setdefault("lastXlsxRows", [])
    return p


def save_payload(payload):
    sb.table("trades_data").upsert(
        {
            "user_id": SUPABASE_USER_ID,
            "payload": payload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_id",
    ).execute()


def main():
    xlsx_rows = None
    source_used = None

    if ZETA_MEMBER_COOKIE:
        try:
            log.info(f"Fetch {MEMBER_API_URL} (sumber utama, lebih lengkap)")
            raw = fetch_member_signals()
            # PENTING: endpoint ini ngasih SEMUA sinyal (aktif + selesai) dalam 1 list,
            # dibedain lewat field `status`. Sinyal yang MASIH AKTIF status-nya "OPEN"
            # (bukan null/kosong!) -- jadi filter "asal ada status" itu SALAH, malah
            # ikut nangkep yang masih jalan juga. Cuma terima status yang genuinely
            # FINAL (ada di STATUS_MAP) -- yang lain (termasuk "OPEN") di-skip total,
            # karena kalau "OPEN" ke-passthrough ke lastXlsxRows, dia bakal NIMPA status
            # RUNNING yang benar (dari Telegram) jadi "OPEN" mentah yang gak dikenal
            # badge manapun di idx_signal.html.
            completed = [e for e in raw if e.get("status") in STATUS_MAP]
            xlsx_rows = [to_xlsx_row_member(e, i) for i, e in enumerate(completed)
                         if e.get("symbol") and e.get("timestamp")]
            source_used = "member"
            log.info(f"Berhasil dari member API: {len(xlsx_rows)} baris resolved (dari {len(raw)} total)")
        except Exception as e:
            log.warning(f"Gagal fetch member API ({e}), fallback ke publik.")

    if xlsx_rows is None:
        log.info(f"Fetch {PUBLIC_API_URL} (fallback, TANPA login tapi kurang lengkap)")
        completed = fetch_public_journal()
        # Defensif: walau endpoint publik ini SUDAH namanya "completed" (kemungkinan
        # sudah pre-filtered oleh Zeta), tetap jaga-jaga cuma terima status FINAL yang
        # dikenal -- sama alasannya kayak jalur member (lihat komentar di atas).
        completed = [e for e in completed if e.get("status") in STATUS_MAP]
        xlsx_rows = [to_xlsx_row_public(e, i) for i, e in enumerate(completed)
                     if e.get("symbol") and e.get("date")]
        source_used = "public"
        log.info(f"Berhasil dari public API: {len(xlsx_rows)} baris")

    if not xlsx_rows:
        log.warning("Hasil kosong -- SKIP, gak replace lastXlsxRows dengan data kosong "
                    "(jaga-jaga API lagi error/maintenance sesaat).")
        sys.exit(1)

    payload = load_current_payload()
    prev_count = len(payload.get("lastXlsxRows") or [])
    payload["lastXlsxRows"] = xlsx_rows
    save_payload(payload)

    log.info(f"Selesai (sumber: {source_used}): lastXlsxRows diganti dari "
              f"{prev_count} baris -> {len(xlsx_rows)} baris")


if __name__ == "__main__":
    main()
