#!/usr/bin/env python3
"""
IDXSY Telegram Group Ingestion (TG-ING-01) — v2
=================================================
Scheduled polling job yang narik pesan baru dari grup Telegram Zeta AI,
parsing pakai logic yang di-port 1:1 dari `idx_signal.html`, lalu APPEND ke
`trades_data.payload.signals[]` / `.results[]` / `.regimes[]` / `.others[]`
— TUJUANNYA MENGGANTIKAN UPLOAD JSON MANUAL sepenuhnya.

PENTING soal arsitektur:
  Script ini SENGAJA TIDAK menghitung ulang `payload.trades[]` (hasil
  matching sinyal<->konfirmasi + merge status XLSX). Logic itu (matchTrades,
  mergeWithXlsxData, preserveXlsxMergeAcrossJsonUpdate) TETAP tinggal di
  idx_signal.html (JS, client-side) sebagai SATU-SATUNYA sumber kebenaran —
  supaya gak ada 2 implementasi (Python vs JS) yang bisa diam-diam divergen.
  idx_signal.html sudah dipatch (checkAndSyncFromCloud) supaya recompute
  trades[] setiap kali load dari cloud, bukan trust `p.trades` mentah-mentah.
  Jadi begitu app dibuka di browser, sinyal/konfirmasi baru dari automasi ini
  otomatis ke-render, TANPA perlu upload JSON manual lagi.

State: `last_processed_msg_id` disimpan di tabel Supabase `ingest_cursor`
(BUKAN di file/repo) — stateless, aman kalau job pindah runner.

Dedup: pakai `msg_id` asli Telegram, semantiknya identik dengan
`dedupeByMsgId()` di idx_signal.html (map keyed by msg_id, item baru
menang/overwrite kalau ada duplikat).
"""

import os
import re
import sys
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tg-ingest")

# ---------------------------------------------------------------------------
# Konfigurasi dari environment (GitHub Secrets)
# ---------------------------------------------------------------------------
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION_STRING = os.environ["TG_SESSION_STRING"]

TG_GROUP_ID = os.environ["TG_GROUP_ID"]  # username (@grup) atau numeric ID grup
if re.fullmatch(r"-?\d+", TG_GROUP_ID):
    TG_GROUP_ID = int(TG_GROUP_ID)  # numeric ID lebih reliable di-resolve Telethon sebagai int, bukan string
TG_TOPIC_ID = os.environ.get("TG_TOPIC_ID")  # opsional: ID topic (forum topics) tempat Zeta AI kirim sinyal
TG_TOPIC_ID = int(TG_TOPIC_ID) if TG_TOPIC_ID else None

# Opsional, buat TESTING doang: kalau di-set (misal "7"), run ini CUMA narik pesan
# dari N hari terakhir (pakai filter tanggal langsung ke Telegram), bukan dari cursor
# tersimpan. Cursor tetap ke-update normal di akhir run, jadi run BERIKUTNYA (tanpa
# env var ini) otomatis lanjut incremental dari situ -- gak perlu reset manual lagi.
BACKFILL_SINCE_DAYS = os.environ.get("BACKFILL_SINCE_DAYS")
BACKFILL_SINCE_DAYS = int(BACKFILL_SINCE_DAYS) if BACKFILL_SINCE_DAYS else None

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_USER_ID = os.environ["SUPABASE_USER_ID"]  # user_id (uuid) pemilik data di trades_data

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ---------------------------------------------------------------------------
# Helper — di-port 1:1 dari idx_signal.html (match1, parseNum)
# ---------------------------------------------------------------------------
def match1(pattern, text, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def parse_num(s):
    """Port dari parseNum() JS — parsing angka Rupiah dengan format lokal
    (titik ATAU koma sebagai desimal/ribuan, tergantung konteks)."""
    if s is None:
        return None
    s = re.sub(r"Rp", "", str(s), flags=re.IGNORECASE).strip()
    s = re.sub(r"[^\d.,\-+]", "", s)
    if s in ("", "-", "+"):
        return None
    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        lc, ld = s.rfind(","), s.rfind(".")
        if lc > ld:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        parts = s.split(",")
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            s = "".join(parts)
        else:
            s = s.replace(",", ".", 1)
    elif has_dot:
        parts = s.split(".")
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            s = "".join(parts)
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Classify — port dari classify() JS
# ---------------------------------------------------------------------------
def classify(text):
    if "ZETA IDX STOCK SIGNAL" in text:
        return "signal"
    if "SIGNAL CONFIRMED" in text or "PROFIT TERKUNCI" in text or "PROFIT TERUS NAIK" in text:
        return "result"
    if "Regime Prediction" in text:
        return "regime"
    return "other"


# ---------------------------------------------------------------------------
# parse_signal — port 1:1 dari parseSignal() JS. Output dict ini SENGAJA
# pakai nama key yang PERSIS SAMA dengan object `row` di JS, karena ini
# langsung di-append ke payload.signals[] apa adanya (harus bentuknya identik
# dengan yang dikonsumsi matchTrades()/renderAll() di idx_signal.html).
# ---------------------------------------------------------------------------
def parse_signal(text, msg_id, date):
    row = {"msg_id": msg_id, "date": date, "type": "SIGNAL"}
    lines = text.split("\n")

    row["market_warning"] = match1(r"^⚠️\s*(.+)$", text, flags=re.MULTILINE)
    row["symbol"] = match1(r"Saham:\s*(\S+)", text)

    sig_m = re.search(r"Signal:\s*.*?\b(BUY|WATCHLIST|SELL)\b", text)
    row["signal_type"] = sig_m.group(1) if sig_m else None

    conf_m = re.search(r"Confidence Score:\s*[^\d]*(\d+)/10\s*\(([^)]+)\)", text)
    if conf_m:
        row["confidence_score"] = int(conf_m.group(1))
        row["confidence_label"] = conf_m.group(2).strip()

    reason_lines = [l.strip() for l in lines if re.match(r"^\s*[+\-]\d+\s+\S", l)]
    row["confidence_reasons"] = "; ".join(reason_lines) if reason_lines else None

    row["entry_price"] = parse_num(match1(r"Entry Price:\s*(Rp?[\d.,]+)", text))

    tp_m = re.search(r"Take Profit:\s*(Rp?[\d.,]+)", text)
    if tp_m:
        row["take_profit"] = parse_num(tp_m.group(1))
    else:
        tp_m = re.search(r"TP1:\s*(Rp?[\d.,]+)\s*\(([+\-\d.]+%)\)", text)
        if tp_m:
            row["take_profit"] = parse_num(tp_m.group(1))
            row["take_profit_pct"] = tp_m.group(2)

    row["target2_price"] = parse_num(match1(r"Target 2[^:]*:\s*(Rp?[\d.,]+)", text))

    sl_single = re.search(r"Stop Loss:\s*(Rp?[\d.,]+)", text)
    if sl_single:
        row["stop_loss"] = parse_num(sl_single.group(1))
    sl_default = re.search(r"Default \(ATR\):\s*(Rp?[\d.,]+)\s*\(([+\-\d.]+%)\)", text)
    if sl_default:
        row["stop_loss"] = parse_num(sl_default.group(1))
        row["stop_loss_pct"] = sl_default.group(2)
    sl_mod = re.search(r"Moderat \(-5%\):\s*(Rp?[\d.,]+)", text)
    if sl_mod:
        row["sl_moderat"] = parse_num(sl_mod.group(1))
    sl_kons = re.search(r"Konservatif \(-3%\):\s*(Rp?[\d.,]+)", text)
    if sl_kons:
        row["sl_konservatif"] = parse_num(sl_kons.group(1))

    macd_m = re.search(r"MACD:\s*([\-\d.]+)\s*\(Sig:\s*([\-\d.]+)\)\s*\S*\s*(Bullish|Bearish)?", text)
    if macd_m:
        row["macd"] = float(macd_m.group(1))
        row["macd_signal_val"] = float(macd_m.group(2))
        row["macd_trend"] = macd_m.group(3)

    rsi_m = re.search(r"RSI \(14\):\s*([\d.]+)\s*\S*\s*\(?(Overbought|Oversold)?\)?", text)
    if rsi_m:
        row["rsi"] = float(rsi_m.group(1))
        row["rsi_label"] = rsi_m.group(2)

    ema_m = re.search(r"EMA 20/50:\s*(Rp?[\d.,]+)\s*/\s*(Rp?[\d.,]+)\s*\S*\s*(Bullish|Bearish)?", text)
    if ema_m:
        row["ema20"] = parse_num(ema_m.group(1))
        row["ema50"] = parse_num(ema_m.group(2))
        row["ema_trend"] = ema_m.group(3)

    vwap_m = re.search(r"VWAP:\s*(Rp?[\d.,]+)\s*\S*\s*(Above|Below)?", text)
    if vwap_m:
        row["vwap"] = parse_num(vwap_m.group(1))
        row["vwap_position"] = vwap_m.group(2)

    bb_m = re.search(r"(?:Bollinger Bands|BB):\s*\[(Rp?[\d.,]+)\s*-\s*(Rp?[\d.,]+)\]", text)
    if bb_m:
        row["bb_lower"] = parse_num(bb_m.group(1))
        row["bb_upper"] = parse_num(bb_m.group(2))

    adx_m = re.search(r"ADX:\s*([\d.]+)\s*(?:\(([^)]+)\))?\s*\S*\s*(Strong|Weak)?", text)
    if adx_m:
        row["adx"] = float(adx_m.group(1))
        row["adx_label"] = adx_m.group(3) or adx_m.group(2)

    atr_m = re.search(r"ATR(?:\s*\(Volatilitas\))?:\s*(Rp?[\d.,]+)", text)
    if atr_m:
        row["atr"] = parse_num(atr_m.group(1))

    row["chart_pattern"] = match1(r"Chart:\s*(.+)", text)
    row["candle_pattern"] = match1(r"Candle:\s*(.+)", text)
    row["bandar_signal"] = match1(r"Sinyal Bandar:\s*\S*\s*(\S+)", text)
    row["smart_money_net"] = match1(r"Smart Money Net:\s*([+\-][\w.,]+\s*\w*)", text)

    buyer_block = re.search(r"Top Buyer:\s*\n([\s\S]*?)(?:\n\s*\n|🔴|Top Seller|📈|💡|$)", text)
    if buyer_block:
        for i, l in enumerate([x.strip() for x in buyer_block.group(1).split("\n") if x.strip()][:3]):
            row[f"top_buyer_{i+1}"] = l

    seller_block = re.search(r"Top Seller:\s*\n([\s\S]*?)(?:\n\s*\n|📈|💡|$)", text)
    if seller_block:
        for i, l in enumerate([x.strip() for x in seller_block.group(1).split("\n") if x.strip()][:3]):
            row[f"top_seller_{i+1}"] = l

    beta_m = re.search(r"Beta:\s*([\d.]+)\s*\(([^)]+)\)\s*\|\s*Volatilitas:\s*(\d+)%", text)
    if beta_m:
        row["beta"] = float(beta_m.group(1))
        row["beta_label"] = beta_m.group(2)
        row["volatilitas_pct"] = float(beta_m.group(3))

    f_status = match1(r"Status:\s*\S*\s*(NET BUY ASING|NET SELL ASING|NEUTRAL)", text)
    if f_status:
        row["foreign_status"] = f_status

    net_asing_m = re.search(r"Net Asing:\s*([+\-][\w.]+)\s*\(([+\-\d,]+)\s*lot\)", text)
    if net_asing_m:
        row["net_asing"] = net_asing_m.group(1)
        row["net_asing_lot"] = parse_num(net_asing_m.group(2))

    buy_sell_m = re.search(r"Buy:\s*([\d,]+)\s*lot\s*\|\s*Sell:\s*([\d,]+)\s*lot", text)
    if buy_sell_m:
        row["foreign_buy_lot"] = parse_num(buy_sell_m.group(1))
        row["foreign_sell_lot"] = parse_num(buy_sell_m.group(2))

    part_m = re.search(r"Partisipasi Asing:\s*(\d+)%", text)
    if part_m:
        row["partisipasi_asing_pct"] = float(part_m.group(1))

    opinion_m = re.search(r"Analyst Opinion:\s*\n([\s\S]*?)(?:\n\s*\n|📰|🤖|$)", text)
    if opinion_m:
        row["analyst_opinion"] = opinion_m.group(1).strip()

    news_block = re.search(r"Berita Terkait:\s*\n([\s\S]*?)(?:🤖|$)", text)
    if news_block:
        cleaned = [re.sub(r"^•\s*", "", x).strip() for x in news_block.group(1).split("\n")]
        for i, l in enumerate([x for x in cleaned if x][:5]):
            row[f"news_{i+1}"] = l

    ver_m = re.search(r"Powered by Zeta AI\s*(v[\d.]+)?", text)
    row["bot_version"] = ver_m.group(1) if (ver_m and ver_m.group(1)) else "v1"

    row["raw_text"] = text
    return row


# ---------------------------------------------------------------------------
# parse_result — port 1:1 dari parseResult() JS
# ---------------------------------------------------------------------------
def parse_result(text, msg_id, date):
    row = {"msg_id": msg_id, "date": date}

    if "SIGNAL CONFIRMED" in text:
        row["type"] = "TP_HIT"
        row["symbol"] = match1(r"Symbol:\s*(\S+)", text)
        row["status_text"] = match1(r"Status:\s*\S*\s*(.+)", text)
        em = re.search(r"Entry:\s*(Rp?[\d.,]+)\s*→\s*TP1?:\s*(Rp?[\d.,]+)", text)
        if em:
            row["entry"] = parse_num(em.group(1))
            row["exit_price"] = parse_num(em.group(2))
        row["day_high"] = parse_num(match1(r"Day High:\s*(Rp?[\d.,]+)", text))
        peak_m = re.search(r"Peak Tertinggi:\s*(Rp?[\d.,]+)\s*\(([+\-\d.]+%)\)", text)
        if peak_m:
            row["peak_price"] = parse_num(peak_m.group(1))
            row["peak_pct"] = peak_m.group(2)
        row["profit_pct"] = (
            match1(r"Profit:\s*([+\-\d.]+%)", text)
            or match1(r"Profit Terkunci:\s*([+\-\d.]+%)", text)
        )

    elif "PROFIT TERKUNCI" in text:
        row["type"] = "PROFIT_LOCKED"
        row["symbol"] = match1(r"Symbol:\s*(\S+)", text)
        row["status_text"] = match1(r"Status:\s*\S*\s*(.+)", text)
        em = re.search(r"Entry:\s*(Rp?[\d.,]+)\s*→\s*Exit:\s*(Rp?[\d.,]+)", text)
        if em:
            row["entry"] = parse_num(em.group(1))
            row["exit_price"] = parse_num(em.group(2))
        row["profit_pct"] = match1(r"Profit Terkunci:\s*([+\-\d.]+%)", text)
        peak_m = re.search(r"Peak Tertinggi:\s*(Rp?[\d.,]+)\s*\(([+\-\d.]+%)\)", text)
        if peak_m:
            row["peak_price"] = parse_num(peak_m.group(1))
            row["peak_pct"] = peak_m.group(2)

    elif "PROFIT TERUS NAIK" in text:
        row["type"] = "PROFIT_RUNNING"
        row["symbol"] = match1(r"Symbol:\s*(\S+)", text)
        row["entry"] = parse_num(match1(r"Entry:\s*(Rp?[\d.,]+)", text))
        row["day_high"] = parse_num(match1(r"Day High:\s*(Rp?[\d.,]+)", text))
        row["profit_pct"] = match1(r"Profit Sekarang:\s*([+\-\d.]+%)", text)

    dur_m = re.search(r"Durasi(?:\s*Sinyal)?:\s*([\d.]+)\s*(hari|jam)", text, re.IGNORECASE)
    if dur_m:
        dur_val = float(dur_m.group(1))
        row["duration_days_confirm"] = dur_val / 24 if dur_m.group(2).lower() == "jam" else dur_val

    row["raw_text"] = text
    return row


# ---------------------------------------------------------------------------
# parse_regime — port 1:1 dari parseRegime() JS
# ---------------------------------------------------------------------------
def parse_regime(text, msg_id, date):
    row = {"msg_id": msg_id, "date": date, "type": "REGIME"}
    dm = re.search(r"Regime Prediction\s*—\s*(.+)", text)
    row["regime_date"] = dm.group(1).strip() if dm else None
    pm = re.search(r"Prediksi:\s*(BULLISH|BEARISH|NEUTRAL)\s*\(Score:\s*([+\-\d.]+)\)", text)
    if pm:
        row["prediction"] = pm.group(1)
        row["score"] = float(pm.group(2))
    components = []
    for cm in re.finditer(r"[🔴🟢⚪]\s*([\w &()/]+):\s*([+\-\d.]+%)\s*\((\d+)%\)", text):
        components.append(f"{cm.group(1).strip()}: {cm.group(2)} (bobot {cm.group(3)}%)")
    row["components"] = " | ".join(components) if components else None
    comment_m = re.search(r"💬\s*(.+)", text)
    row["commentary"] = comment_m.group(1).strip() if comment_m else None
    row["raw_text"] = text
    return row


# ---------------------------------------------------------------------------
# dedupe_by_msg_id — port 1:1 dari dedupeByMsgId() JS. Item BARU menang
# (overwrite) kalau ada msg_id yang sama dengan yang lama.
# ---------------------------------------------------------------------------
def dedupe_by_msg_id(existing, new):
    merged = {item["msg_id"]: item for item in existing}
    merged.update({item["msg_id"]: item for item in new})
    return list(merged.values())


# ---------------------------------------------------------------------------
# Cursor (state) helpers — cuma soal progress baca Telegram, gak ada
# hubungannya sama sekali dengan skema trades_data.
# ---------------------------------------------------------------------------
def get_last_processed_id():
    resp = (
        sb.table("ingest_cursor")
        .select("last_processed_msg_id")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source", "telegram_group")
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0]["last_processed_msg_id"] if rows else 0


def set_last_processed_id(msg_id):
    sb.table("ingest_cursor").upsert(
        {
            "user_id": SUPABASE_USER_ID,
            "source": "telegram_group",
            "last_processed_msg_id": msg_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_id,source",
    ).execute()


# ---------------------------------------------------------------------------
# trades_data helpers
# ---------------------------------------------------------------------------
def load_current_payload():
    resp = (
        sb.table("trades_data")
        .select("payload")
        .eq("user_id", SUPABASE_USER_ID)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if rows and rows[0].get("payload"):
        p = rows[0]["payload"]
    else:
        p = {}
    # Default aman kalau row/field belum ada sama sekali (user baru, belum pernah
    # upload JSON manual sekalipun) — biar automasi ini bisa jadi cara PERTAMA data masuk.
    p.setdefault("signals", [])
    p.setdefault("results", [])
    p.setdefault("regimes", [])
    p.setdefault("others", [])
    p.setdefault("trades", [])          # SENGAJA gak disentuh di sini, lihat catatan modul.
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    last_id = get_last_processed_id()
    log.info(f"Mulai dari msg_id > {last_id}")

    new_signals, new_results, new_regimes, new_others = [], [], [], []
    failed_msg_ids = []
    max_id_seen = last_id

    with TelegramClient(StringSession(TG_SESSION_STRING), TG_API_ID, TG_API_HASH) as client:
        # PENTING: StringSession gak nyimpen cache entity (ID<->access_hash) dari sesi
        # sebelumnya. Kalau TG_GROUP_ID numeric ID mentah langsung dipanggil tanpa ini,
        # Telethon bakal gagal "Cannot find any entity corresponding to ...". Panggil
        # get_dialogs() dulu supaya entity grup ke-cache buat run ini.
        client.get_dialogs()

        iter_kwargs = dict(reverse=True)
        if BACKFILL_SINCE_DAYS is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=BACKFILL_SINCE_DAYS)
            iter_kwargs["offset_date"] = cutoff
            log.info(f"Mode testing: cuma narik pesan sejak {cutoff.isoformat()} ({BACKFILL_SINCE_DAYS} hari terakhir)")
        else:
            iter_kwargs["min_id"] = last_id
        if TG_TOPIC_ID is not None:
            iter_kwargs["reply_to"] = TG_TOPIC_ID
        messages = list(client.iter_messages(TG_GROUP_ID, **iter_kwargs))

    log.info(f"Ambil {len(messages)} pesan baru")
    if not messages:
        return

    for m in messages:
        # PENTING: pakai raw_text, BUKAN m.text. Telethon `.text` merender ulang pesan
        # sebagai Markdown (bold jadi **Symbol**, dst) sesuai entity formatting yang
        # dipakai bot. Semua regex parser di sini didesain buat teks POLOS (persis
        # seperti JSON export Telegram Desktop, yang gak nyisipin tanda ** literal).
        raw = m.raw_text
        if not raw:
            max_id_seen = max(max_id_seen, m.id)
            continue
        text = raw
        # PENTING: simpan sebagai string WITA NAIVE (tanpa offset sama sekali), PERSIS
        # format yang dipakai 654 dari 731 entry lama (hasil upload JSON manual, browser
        # user sendiri yang WITA) -- BUKAN WIB, BUKAN UTC. idx_signal.html matching
        # (keyOf, mergeWithXlsxData, dll) semua pakai String(date).slice(0,10) MENTAH,
        # gak ada konversi timezone -- jadi kalau disimpan beda zona dari yang sudah
        # ada, sinyal yang deket tengah malam bisa ke-slice jadi tanggal beda ->
        # gagal matching -> data duplikat. Instant absolutnya tetap presisi selama
        # browser yang baca ini juga di WITA (asumsi valid, app ini single-user
        # milik orang Makassar).
        date_iso = m.date.astimezone(ZoneInfo("Asia/Makassar")).replace(tzinfo=None).isoformat()

        try:
            cat = classify(text)
            if cat == "signal":
                new_signals.append(parse_signal(text, m.id, date_iso))
            elif cat == "result":
                new_results.append(parse_result(text, m.id, date_iso))
            elif cat == "regime":
                new_regimes.append(parse_regime(text, m.id, date_iso))
            else:
                new_others.append({"msg_id": m.id, "date": date_iso, "type": "OTHER", "raw_text": text})
            max_id_seen = max(max_id_seen, m.id)
        except Exception as e:
            # Poison-message safety: 1 pesan format aneh TIDAK BOLEH nge-block semua
            # sinyal baru selanjutnya selamanya. Skip, catat buat dicek manual, tetap lanjut.
            failed_msg_ids.append(m.id)
            log.error(f"Gagal proses msg #{m.id}, DI-SKIP (bukan diblokir): {e}")
            max_id_seen = max(max_id_seen, m.id)

    # Merge SATU KALI ke payload trades_data (bukan per-pesan) — payload ini adalah
    # blob gemuk (seluruh state app), jadi baca-ubah-simpan per pesan bakal boros +
    # lambat. dedupe_by_msg_id() semantiknya identik dengan dedupeByMsgId() di JS.
    payload = load_current_payload()
    payload["signals"] = dedupe_by_msg_id(payload["signals"], new_signals)
    payload["results"] = dedupe_by_msg_id(payload["results"], new_results)
    payload["regimes"] = dedupe_by_msg_id(payload["regimes"], new_regimes)
    payload["others"] = dedupe_by_msg_id(payload["others"], new_others)
    # payload["trades"] SENGAJA TIDAK diubah di sini — idx_signal.html yang recompute
    # dari signals[]/results[] setiap kali load dari cloud (lihat catatan di modul docstring).
    save_payload(payload)

    # Cursor cuma dimajukan SETELAH payload berhasil tersimpan -- kalau save_payload()
    # gagal (network/permission/dll), cursor TETAP di posisi lama, jadi run berikutnya
    # otomatis retry batch yang sama (aman/idempotent karena dedupe by msg_id).
    set_last_processed_id(max_id_seen)

    log.info(
        f"Selesai: {len(new_signals)} signal, {len(new_results)} result, "
        f"{len(new_regimes)} regime, {len(new_others)} other (skip kategori), "
        f"{len(failed_msg_ids)} gagal parse. Total di payload sekarang: "
        f"{len(payload['signals'])} signal, {len(payload['results'])} result."
    )
    if failed_msg_ids:
        log.error(
            f"Pesan yang gagal di-parse (PERLU DICEK MANUAL, kemungkinan format baru "
            f"dari bot yang belum ke-cover parser): {failed_msg_ids}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
