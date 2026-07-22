#!/usr/bin/env python3
"""
IDXSY Zeta Journal Sync (replaces manual "scrape dashboard -> XLSX -> upload" step)
====================================================================================
Fetch `https://idx-journal.zeta-ai.pro/api/idx-signals.json` (endpoint publik,
statis, TANPA login — ditemukan lewat DevTools Network tab, bukan hasil
scraping HTML) dan replace `trades_data.payload.lastXlsxRows` dengan data
`completed[]` dari situ.

Kenapa ini yang punya status SL HIT/EXPIRED (bukan Telegram):
  Grup Telegram Zeta AI cuma post notifikasi kalau profit (TP HIT/PROFIT
  LOCKED/PROFIT RUNNING) — SL HIT dan EXPIRED gak pernah dipost publik.
  Dashboard/journal JSON ini punya status FINAL yang sebenarnya (otoritatif),
  makanya ini pengganti proses manual "scrape dashboard pakai Instant Data
  Scraper -> export XLSX -> upload manual ke IDXSY Signal".

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
from datetime import datetime, timezone

import requests
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("journal-sync")

JOURNAL_API_URL = "https://idx-journal.zeta-ai.pro/api/idx-signals.json"

# Konversi status dari format API (underscore) ke format yang dipakai
# mergeWithXlsxData() di idx_signal.html (spasi) -- PENTING, kalau salah
# format, perbandingan string `finalStatus !== 'TP HIT'` di JS bakal gagal
# match dan status gak akan pernah dianggap final/otoritatif.
STATUS_MAP = {
    "TP_HIT": "TP HIT",
    "SL_HIT": "SL HIT",
    "EXPIRED": "EXPIRED",
}

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_USER_ID = os.environ["SUPABASE_USER_ID"]

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def fetch_journal():
    resp = requests.get(JOURNAL_API_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def to_xlsx_row(entry, uid):
    """Konversi 1 entry dari completed[] (API) ke bentuk row yang dikonsumsi
    mergeWithXlsxData() di idx_signal.html: {_uid, date, symbol, decision,
    entry, sl, tp, status, return_pct}."""
    raw_status = (entry.get("status") or "").strip()
    status = STATUS_MAP.get(raw_status, raw_status)  # fallback: pass-through kalau ada status baru yang belum ke-map

    profit_pct = entry.get("profit_pct")
    return_pct = f"{profit_pct:+.1f}%" if profit_pct is not None else None

    date_raw = entry.get("date") or ""
    date_only = date_raw[:10] if date_raw else None  # "2026-07-22 12:51:13" -> "2026-07-22"

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
    log.info(f"Fetch {JOURNAL_API_URL}")
    data = fetch_journal()
    completed = data.get("completed", [])
    log.info(f"API updated_at: {data.get('updated')}, {len(completed)} baris 'completed'")

    if not completed:
        log.warning("Response API kosong (completed=[]) -- SKIP, gak replace lastXlsxRows "
                    "dengan data kosong (jaga-jaga API lagi error/maintenance sesaat).")
        sys.exit(1)

    xlsx_rows = [to_xlsx_row(e, i) for i, e in enumerate(completed) if e.get("symbol") and e.get("date")]
    skipped = len(completed) - len(xlsx_rows)
    if skipped:
        log.warning(f"{skipped} entry di-skip (symbol/date kosong)")

    payload = load_current_payload()
    prev_count = len(payload.get("lastXlsxRows") or [])
    payload["lastXlsxRows"] = xlsx_rows
    save_payload(payload)

    log.info(f"Selesai: lastXlsxRows diganti dari {prev_count} baris -> {len(xlsx_rows)} baris")


if __name__ == "__main__":
    main()
