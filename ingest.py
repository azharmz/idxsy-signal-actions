#!/usr/bin/env python3
"""
IDXSY Telegram Group Ingestion (TG-ING-01)
===========================================
Scheduled polling job (dijalankan via GitHub Actions cron) yang narik pesan
baru dari grup Telegram Zeta AI, parsing pakai logic yang di-port 1:1 dari
`idx_signal.html` (parseSignal / parseResult / parseRegime), lalu insert ke
Supabase (tabel signals / confirmations / market_regime).

Kenapa Python + Telethon (bukan Deno/TypeScript):
  Telethon adalah library MTProto paling matang yang tersedia, sudah
  terbukti jalan terhadap 948 pesan real project ini. Alternatif Deno-native
  (MTKruto dkk) masih pre-1.0 dan secara eksplisit belum direkomendasikan
  untuk production oleh maintainer-nya sendiri — risiko terlalu besar untuk
  pipeline data trading real.

State: `last_processed_msg_id` disimpan di tabel Supabase `ingest_cursor`
(BUKAN di file/repo) — stateless, aman kalau job pindah runner.

Anti-duplicate: pakai msg_id ASLI dari Telegram (message.id) langsung sebagai
primary matching key ke constraint unique (msg_id, user_id) yang sudah ada di
tabel `signals` dan `confirmations`. TIDAK perlu synthetic hash sama sekali
di jalur ini — synthetic ID cuma dibutuhkan dulu untuk data lama yang asalnya
dari XLSX (yang emang gak punya msg_id Telegram).
"""

import os
import re
import sys
import time
import logging
from datetime import datetime, timezone

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

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_USER_ID = os.environ["SUPABASE_USER_ID"]  # user_id (uuid) pemilik data di semua tabel

ENTRY_MATCH_TOLERANCE_PCT = 0.05  # sama persis dengan konstanta di idx_signal.html

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
# parse_signal — port 1:1 dari parseSignal() JS
# ---------------------------------------------------------------------------
def parse_signal(text, msg_id, date):
    row = {"msg_id": msg_id, "date": date, "type": "SIGNAL"}
    lines = text.split("\n")

    row["market_warning"] = match1(r"⚠️\s*(.+)", text)
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

    # Durasi eksplisit di pesan konfirmasi (best-guess pattern, sama seperti JS)
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
# Mapping row hasil parse -> kolom tabel Supabase
# ---------------------------------------------------------------------------
def signal_to_db_row(sig):
    detail_keys = [
        "market_warning", "confidence_reasons", "chart_pattern", "candle_pattern",
        "bandar_signal", "smart_money_net", "top_buyer_1", "top_buyer_2", "top_buyer_3",
        "top_seller_1", "top_seller_2", "top_seller_3", "macd", "macd_signal_val",
        "macd_trend", "rsi", "rsi_label", "ema20", "ema50", "ema_trend", "vwap",
        "vwap_position", "bb_lower", "bb_upper", "adx", "adx_label", "atr",
        "foreign_status", "net_asing", "net_asing_lot", "foreign_buy_lot",
        "foreign_sell_lot", "partisipasi_asing_pct", "beta", "beta_label",
        "volatilitas_pct", "analyst_opinion", "news_1", "news_2", "news_3",
        "news_4", "news_5", "target2_price", "sl_moderat", "sl_konservatif",
        "bot_version",
    ]
    detail = {k: sig[k] for k in detail_keys if k in sig}
    return {
        "msg_id": sig["msg_id"],
        "ticker": sig.get("symbol"),
        "signal_type": sig.get("signal_type"),
        "signal_timestamp": sig["date"],
        "entry_price": sig.get("entry_price"),
        "tp1_price": sig.get("take_profit"),
        "tp1_pct": sig.get("take_profit_pct"),
        "tp2_price": sig.get("target2_price"),
        "sl_default_price": sig.get("stop_loss"),
        "sl_default_pct": sig.get("stop_loss_pct"),
        "sl_moderat_price": sig.get("sl_moderat"),
        "sl_konservatif_price": sig.get("sl_konservatif"),
        "confidence_score": sig.get("confidence_score"),
        "confidence_label": sig.get("confidence_label"),
        "detail": detail,
        "raw_text": sig.get("raw_text"),
    }


def result_to_confirmation_row(res, signal_id):
    confirmation_type_map = {
        "TP_HIT": "tp_hit",
        "PROFIT_LOCKED": "closed",
        "PROFIT_RUNNING": "ongoing",
    }
    return {
        "msg_id": res["msg_id"],
        "signal_id": signal_id,
        "ticker": res.get("symbol"),
        "confirmation_type": confirmation_type_map.get(res.get("type")),
        "status": res.get("status_text"),
        "entry_price": res.get("entry"),
        "exit_price": res.get("exit_price"),
        "profit_pct": res.get("profit_pct"),
        "peak_price": res.get("peak_price"),
        "peak_pct": res.get("peak_pct"),
        "durasi": str(res["duration_days_confirm"]) if "duration_days_confirm" in res else None,
        "raw_text": res.get("raw_text"),
    }


def regime_to_db_row(reg):
    return {
        "date": reg["date"][:10],  # ambil bagian tanggal (YYYY-MM-DD) dari ISO timestamp
        "prediction": reg.get("prediction"),
        "score": reg.get("score"),
        "summary": reg.get("commentary"),
        "raw_text": reg.get("raw_text"),
        # `index_scoring` / `wall_street` / `komoditas` / `suspended` / `uma` / `strength_emoji`
        # belum ada regex parser-nya di idx_signal.html (regime message punya bagian lain yang
        # belum di-cover) — dibiarkan NULL, JANGAN ditebak isinya.
    }


# ---------------------------------------------------------------------------
# Matching confirmation -> signal (simplified live version dari matchTrades() JS)
# ---------------------------------------------------------------------------
def find_matching_signal_id(res):
    """Cari signal_id yang cocok buat sebuah result/confirmation, berdasarkan
    ticker + entry_price (toleransi ENTRY_MATCH_TOLERANCE_PCT), sama seperti
    matchTrades() di idx_signal.html. Ambil kandidat dengan signal_timestamp
    PALING BARU yang <= waktu confirmation (confirmation harus datang SETELAH
    sinyalnya)."""
    ticker = res.get("symbol")
    entry = res.get("entry")
    if not ticker or entry is None:
        return None

    lo = entry * (1 - ENTRY_MATCH_TOLERANCE_PCT / 100)
    hi = entry * (1 + ENTRY_MATCH_TOLERANCE_PCT / 100)

    resp = (
        sb.table("signals")
        .select("id, signal_timestamp, entry_price")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("ticker", ticker)
        .gte("entry_price", lo)
        .lte("entry_price", hi)
        .lte("signal_timestamp", res["date"])
        .order("signal_timestamp", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0]["id"] if rows else None


def is_halal(ticker):
    resp = (
        sb.table("issi_list")
        .select("ticker")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("ticker", ticker)
        .limit(1)
        .execute()
    )
    return bool(resp.data)


# ---------------------------------------------------------------------------
# Cursor (state) helpers
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
# Main
# ---------------------------------------------------------------------------
def main():
    last_id = get_last_processed_id()
    log.info(f"Mulai dari msg_id > {last_id}")

    with TelegramClient(StringSession(TG_SESSION_STRING), TG_API_ID, TG_API_HASH) as client:
        # PENTING: StringSession gak nyimpen cache entity (ID<->access_hash) dari sesi
        # sebelumnya. Kalau TG_GROUP_ID numeric ID mentah langsung dipanggil tanpa ini,
        # Telethon bakal gagal "Cannot find any entity corresponding to ...". Panggil
        # get_dialogs() dulu supaya entity grup ke-cache buat run ini.
        client.get_dialogs()

        iter_kwargs = dict(min_id=last_id, reverse=True)
        if TG_TOPIC_ID is not None:
            # Filter cuma pesan di dalam topic tertentu (forum topics) — kalau gak di-set,
            # ambil dari SELURUH grup (termasuk topic lain / general), yang keliru kalau
            # grupnya emang pakai topics dan sinyal cuma ada di 1 topic spesifik.
            iter_kwargs["reply_to"] = TG_TOPIC_ID
        messages = list(client.iter_messages(TG_GROUP_ID, **iter_kwargs))

    log.info(f"Ambil {len(messages)} pesan baru")
    if not messages:
        return

    max_id_seen = last_id
    n_signal = n_result = n_regime = n_other = 0
    failed_msg_ids = []

    for m in messages:
        if not m.text:
            max_id_seen = max(max_id_seen, m.id)
            continue
        text = m.text
        date_iso = m.date.astimezone(timezone.utc).isoformat()

        try:
            cat = classify(text)

            if cat == "signal":
                sig = parse_signal(text, m.id, date_iso)
                db_row = signal_to_db_row(sig)
                db_row["user_id"] = SUPABASE_USER_ID
                if db_row.get("ticker"):
                    db_row["is_halal"] = is_halal(db_row["ticker"])
                sb.table("signals").upsert(db_row, on_conflict="msg_id,user_id").execute()
                n_signal += 1

            elif cat == "result":
                res = parse_result(text, m.id, date_iso)
                signal_id = find_matching_signal_id(res)
                conf_row = result_to_confirmation_row(res, signal_id)
                conf_row["user_id"] = SUPABASE_USER_ID
                sb.table("confirmations").upsert(conf_row, on_conflict="msg_id,user_id").execute()
                n_result += 1
                if signal_id is None:
                    log.warning(f"msg #{m.id}: confirmation {res.get('symbol')} gak ketemu signal pasangannya")

            elif cat == "regime":
                reg = parse_regime(text, m.id, date_iso)
                if reg.get("regime_date"):
                    reg_row = regime_to_db_row(reg)
                    reg_row["user_id"] = SUPABASE_USER_ID
                    sb.table("market_regime").upsert(reg_row, on_conflict="date,user_id").execute()
                    n_regime += 1
                else:
                    log.warning(f"msg #{m.id}: regime message tanpa tanggal jelas, di-skip")

            else:
                n_other += 1  # gak diinsert kemana pun, cuma dihitung buat logging

            # Cursor maju SETIAP pesan berhasil diproses (bukan cuma di akhir batch),
            # supaya kalau job crash di tengah jalan, reprocessing minimal.
            max_id_seen = max(max_id_seen, m.id)
            set_last_processed_id(max_id_seen)

        except Exception as e:
            # PENTING: jangan `break`/stop total di sini. Satu pesan format aneh/gak
            # terduga TIDAK BOLEH nge-block semua sinyal baru selanjutnya selamanya
            # (poison message problem). Skip pesan ini, catat buat dicek manual, tetap
            # majukan cursor biar pipeline jalan terus.
            failed_msg_ids.append(m.id)
            log.error(f"Gagal proses msg #{m.id}, DI-SKIP (bukan diblokir): {e}")
            max_id_seen = max(max_id_seen, m.id)
            set_last_processed_id(max_id_seen)

    log.info(
        f"Selesai: {n_signal} signal, {n_result} result, {n_regime} regime, "
        f"{n_other} other (skip kategori), {len(failed_msg_ids)} gagal parse"
    )
    if failed_msg_ids:
        log.error(
            f"Pesan yang gagal di-parse (PERLU DICEK MANUAL, kemungkinan format baru "
            f"dari bot yang belum ke-cover parser): {failed_msg_ids}"
        )
        sys.exit(1)  # exit non-zero supaya GitHub Actions run kelihatan "failed" di UI,
                      # walau datanya sendiri tetap masuk (skip cuma yang error doang)


if __name__ == "__main__":
    main()
