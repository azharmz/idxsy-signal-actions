#!/usr/bin/env python3
"""
IDXSY Signal — Recompute trades[] (pure Python, no browser needed)
======================================================================
Port 1:1 dari 3 fungsi JS di idx_signal.html:
  - matchTrades(signals, results)
  - preserveXlsxMergeAcrossJsonUpdate(oldTrades, newTrades)
  - mergeWithXlsxData(trades, xlsxRows)

Kenapa di-port ke sini (bukan lanjut pakai headless-browser trigger):
  Sebelumnya (trigger_recompute.py) kita "membuka" idx_signal.html pakai
  Playwright buat mancing browser ngitung ulang trades[] terus nyimpen
  balik ke Supabase -- ini jalan, tapi muter jauh (butuh session injection,
  Chromium, timeout handling) padahal cuma buat 1 komputasi data yang
  sebenarnya bisa langsung dikerjain di backend. Setelah dipikir ulang,
  nge-port 3 fungsi ini (dan dites hati-hati biar match persis output JS)
  jauh lebih simpel & robust daripada muter lewat browser palsu.

PENTING: kalau fungsi-fungsi ini di JS (idx_signal.html) diubah lagi di
masa depan, port di sini HARUS ikut diupdate manual -- gak ada mekanisme
otomatis buat jaga sinkron. Simpan baik-baik kalau ada perubahan logic di
sisi JS.

Semantik tanggal: semua field `date` di signals[]/results[]/lastXlsxRows
itu string WITA NAIVE (tanpa offset, misal "2026-07-23T05:15:35"), PERSIS
representasi yang dipakai browser (yang timezone-nya WITA) waktu parsing
`new Date(dateString)`. Python `datetime.fromisoformat()` baca string ini
apa adanya (naive), jadi urutan waktu & selisihnya tetap konsisten dengan
JS selama gak ada yang nyampur timezone-aware datetime ke dalam sini.
"""

import os
import logging
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("recompute-trades")

RUNNING_FRESH_DAYS = 5
ENTRY_MATCH_TOLERANCE_PCT = 0.05


def parse_date(s):
    """Port dari `new Date(s)` buat string tanggal naive WITA yang dipakai di sini."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def count_business_days_between(start_dt, end_dt):
    """Port 1:1 dari countBusinessDaysBetween() JS."""
    start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    cur = start
    while cur < end:
        cur = cur + timedelta(days=1)
        dow = cur.weekday()  # Python: 0=Senin..6=Minggu (BEDA dari JS getDay() 0=Minggu!)
        if dow != 5 and dow != 6:  # exclude Sabtu(5)/Minggu(6) versi Python weekday()
            count += 1
    return count


def match_trades(signals, results):
    """Port 1:1 dari matchTrades() JS."""
    max_known_date = None
    for s in signals:
        d = parse_date(s.get("date"))
        if d and (max_known_date is None or d > max_known_date):
            max_known_date = d
    for r in results:
        d = parse_date(r.get("date"))
        if d and (max_known_date is None or d > max_known_date):
            max_known_date = d

    by_symbol = {}
    for s in signals:
        by_symbol.setdefault(s.get("symbol"), {"sig": [], "res": []})["sig"].append(s)
    for r in results:
        sym = r.get("symbol")
        if not sym:
            continue
        by_symbol.setdefault(sym, {"sig": [], "res": []})["res"].append(r)

    trades = []
    for symbol, group in by_symbol.items():
        sigs = sorted(group["sig"], key=lambda s: parse_date(s.get("date")) or datetime.min)
        res_arr = sorted(group["res"], key=lambda r: parse_date(r.get("date")) or datetime.min)

        for idx, s in enumerate(sigs):
            sig_date = parse_date(s.get("date"))
            next_sig_date = parse_date(sigs[idx + 1].get("date")) if idx + 1 < len(sigs) else None

            candidates = []
            for r in res_arr:
                rd = parse_date(r.get("date"))
                if rd is None or sig_date is None:
                    continue
                in_window = rd >= sig_date and (next_sig_date is None or rd < next_sig_date)
                if not in_window:
                    continue
                r_entry = r.get("entry")
                s_entry = s.get("entry_price")
                if r_entry is not None and s_entry not in (None, 0):
                    diff_pct = abs(r_entry - s_entry) / s_entry * 100
                    if diff_pct > ENTRY_MATCH_TOLERANCE_PCT:
                        continue
                candidates.append(r)

            status, return_pct, result_ref = "NO RESULT", None, None
            terminal = next((r for r in candidates if r.get("type") in ("TP_HIT", "PROFIT_LOCKED")), None)
            running = next((r for r in candidates if r.get("type") == "PROFIT_RUNNING"), None)

            if terminal:
                status = "TP HIT" if terminal.get("type") == "TP_HIT" else "CLOSED (LOCKED)"
                return_pct = terminal.get("profit_pct")
                result_ref = terminal
            elif running:
                status = "RUNNING"
                return_pct = running.get("profit_pct")
                result_ref = running
            elif next_sig_date is None and sig_date and max_known_date:
                age_business_days = count_business_days_between(sig_date, max_known_date)
                status = "RUNNING" if age_business_days <= RUNNING_FRESH_DAYS else "NO RESULT"

            duration_days, duration_source = None, None
            if terminal:
                if terminal.get("duration_days_confirm") is not None:
                    duration_days = round(terminal["duration_days_confirm"], 6)
                    duration_source = "confirm"
                else:
                    t_date = parse_date(terminal.get("date"))
                    if t_date and sig_date:
                        duration_days = round((t_date - sig_date).total_seconds() / 86400, 6)
                        duration_source = "result"

            trade = dict(s)
            trade.update({
                "status": status,
                "return_pct": return_pct,
                "result_ref": result_ref,
                "source": "JSON",
                "duration_days": duration_days,
                "duration_source": duration_source,
            })
            trades.append(trade)

    trades.sort(key=lambda t: parse_date(t.get("date")) or datetime.min, reverse=True)
    return trades


def preserve_xlsx_merge_across_json_update(old_trades, new_trades):
    """Port 1:1 dari preserveXlsxMergeAcrossJsonUpdate() JS."""
    def key_of(t):
        return (t.get("symbol") or "").upper() + "|" + str(t.get("date"))[:10]

    old_by_key = {}
    for t in old_trades:
        if t.get("source") in ("JSON+XLSX", "XLSX"):
            old_by_key[key_of(t)] = t

    used_keys = set()
    merged = []
    for t in new_trades:
        k = key_of(t)
        old = old_by_key.get(k)
        if not old:
            merged.append(t)
            continue
        used_keys.add(k)
        nt = dict(t)
        nt.update({"source": "JSON+XLSX", "status": old.get("status"), "return_pct": old.get("return_pct")})
        merged.append(nt)

    leftover_old = [t for k, t in old_by_key.items() if t.get("source") == "XLSX" and k not in used_keys]
    return merged + leftover_old


def merge_with_xlsx_data(trades, xlsx_rows):
    """Port 1:1 dari mergeWithXlsxData() JS (termasuk prioritas resolved_at v1.6)."""
    xlsx_by_key = {}
    for r in xlsx_rows:
        k = f"{r.get('symbol')}|{r.get('date')}"
        xlsx_by_key.setdefault(k, []).append(r)

    max_xlsx_date = None
    for r in xlsx_rows:
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d")
        except (ValueError, KeyError, TypeError):
            continue
        if max_xlsx_date is None or d > max_xlsx_date:
            max_xlsx_date = d
    xlsx_confirm_ts = max_xlsx_date.replace(hour=17, minute=30) if max_xlsx_date else None

    used_uid = set()
    existing_xlsx_only_keys = {
        (t.get("symbol") or "").upper() + "|" + str(t.get("date"))[:10]
        for t in trades if t.get("source") == "XLSX"
    }

    merged = []
    for t in trades:
        if t.get("source") == "XLSX":
            merged.append(t)
            continue
        k = (t.get("symbol") or "").upper() + "|" + str(t.get("date"))[:10]
        candidates = [r for r in xlsx_by_key.get(k, []) if r.get("_uid") not in used_uid]

        match = None
        if len(candidates) == 1:
            match = candidates[0]
        elif len(candidates) > 1:
            entry_price = t.get("entry_price") or 0
            match = sorted(candidates, key=lambda r: abs((r.get("entry") or 0) - entry_price))[0]

        if not match:
            nt = dict(t)
            nt["source"] = t.get("source") or "JSON"
            merged.append(nt)
            continue

        used_uid.add(match.get("_uid"))
        final_status = match.get("status") or t.get("status")

        json_duration_invalid = (
            t.get("duration_source") in ("confirm", "result")
            and final_status not in ("TP HIT", "CLOSED (LOCKED)")
        )
        duration_days = None if json_duration_invalid else t.get("duration_days")
        duration_source = None if json_duration_invalid else t.get("duration_source")

        t_date = parse_date(t.get("date"))
        if duration_days is None and match.get("resolved_at"):
            resolved_at = parse_date(match["resolved_at"])
            if resolved_at and t_date:
                duration_days = round((resolved_at - t_date).total_seconds() / 86400, 6)
                duration_source = "resolved_at"
        elif duration_days is None and xlsx_confirm_ts and t_date:
            duration_days = round((xlsx_confirm_ts - t_date).total_seconds() / 86400, 6)
            duration_source = "xlsx"

        nt = dict(t)
        nt.update({
            "source": "JSON+XLSX",
            "status": final_status,
            "return_pct": match.get("return_pct") or t.get("return_pct"),
            "duration_days": duration_days,
            "duration_source": duration_source,
        })
        merged.append(nt)

    leftover = []
    for r in xlsx_rows:
        if r.get("_uid") in used_uid:
            continue
        k = f"{r.get('symbol')}|{r.get('date')}"
        if k in existing_xlsx_only_keys:
            continue
        leftover.append({
            "date": r.get("date"), "symbol": r.get("symbol"), "signal_type": r.get("decision"),
            "entry_price": r.get("entry"), "stop_loss": r.get("sl"), "take_profit": r.get("tp"),
            "confidence_score": None, "status": r.get("status"), "return_pct": r.get("return_pct"),
            "source": "XLSX", "duration_days": None, "duration_source": None,
        })

    result = merged + leftover
    result.sort(key=lambda t: parse_date(t.get("date")) or datetime.min, reverse=True)
    return result


def main():
    from supabase import create_client, Client

    supabase_url = os.environ["SUPABASE_URL"]
    supabase_service_key = os.environ["SUPABASE_SERVICE_KEY"]
    supabase_user_id = os.environ["SUPABASE_USER_ID"]
    sb: Client = create_client(supabase_url, supabase_service_key)

    resp = (
        sb.table("trades_data")
        .select("payload")
        .eq("user_id", supabase_user_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows or not rows[0].get("payload"):
        log.warning("Belum ada trades_data buat user ini, gak ada yang di-recompute.")
        return

    payload = rows[0]["payload"]
    signals = payload.get("signals") or []
    results = payload.get("results") or []
    xlsx_rows = payload.get("lastXlsxRows") or []
    prev_trades = payload.get("trades") or []

    if not signals:
        log.warning("payload.signals kosong, gak ada yang di-recompute.")
        return

    trades = match_trades(signals, results)
    trades = preserve_xlsx_merge_across_json_update(prev_trades, trades)
    if xlsx_rows:
        trades = merge_with_xlsx_data(trades, xlsx_rows)

    payload["trades"] = trades

    sb.table("trades_data").upsert(
        {
            "user_id": supabase_user_id,
            "payload": payload,
            "updated_at": datetime.now().astimezone().isoformat(),
        },
        on_conflict="user_id",
    ).execute()

    status_counts = {}
    for t in trades:
        status_counts[t.get("status")] = status_counts.get(t.get("status"), 0) + 1
    log.info(f"Selesai. {len(trades)} trades di-recompute. Breakdown status: {status_counts}")


if __name__ == "__main__":
    main()
