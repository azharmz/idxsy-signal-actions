# Panduan Setup — Telegram Ingestion Automation (TG-ING-01)

**Tujuan automasi ini: MENGGANTIKAN upload JSON manual ke IDXSY Signal.**
Data yang ditarik dari grup Telegram langsung masuk ke `trades_data.payload`
(struktur yang sama persis dibaca `idx_signal.html`) — begitu app dibuka di
browser, sinyal/konfirmasi baru otomatis muncul, gak perlu export+upload JSON
manual lagi.

Semua kode sudah saya siapkan & sudah dites syntax-nya. Bagian yang tersisa
di bawah ini **harus dikerjain manual oleh kamu sendiri** — gak bisa
diwakilkan ke AI manapun (saya atau agen lain), karena butuh akses langsung
ke akun Telegram & GitHub kamu.

## ⚠️ Ada 1 file TAMBAHAN yang harus kamu deploy juga

Selain repo automasi ini, **`idx_signal.html` juga perlu diupdate** (sudah
saya patch versi terbarunya, v1.5) dan di-upload ulang ke Cloudflare Pages
kamu. Tanpa update ini, sinyal baru dari automasi TETAP gak akan muncul di
tabel trade journal — karena app-nya baru dipatch supaya menghitung ulang
`trades[]` dari `signals[]`/`results[]` setiap kali load dari cloud (sebelum
ini app cuma trust data `trades[]` yang tersimpan apa adanya, yang gak pernah
diupdate otomatis sama automasi).

## Automasi ke-2: Zeta Journal Sync (gantiin scrape XLSX manual)

Selain automasi Telegram (`ingest.py`), ada 1 automasi lagi: **`sync_journal.py`**
— gantiin proses manual "scrape dashboard pakai Instant Data Scraper → export
XLSX → upload ke IDXSY Signal". Ini yang ngisi status **SL HIT** & **EXPIRED**
(yang emang gak pernah dipost di Telegram).

### 2 sumber data, PREFER yang lebih lengkap

Ternyata endpoint publik (`idx-journal.zeta-ai.pro`) **gak lengkap** — ada
banyak record yang ke-drop, terutama status **SL HIT** (berhenti muncul
total sejak 2026-06-26, padahal transaksi terus jalan). Ketauan setelah
bandingin langsung sama `member.zeta-ai.pro` (versi lengkap yang butuh
login) — contoh nyata: **PACK (SL HIT, 21 Juli)** ada di member, TIDAK ADA
di publik.

Makanya script ini sekarang punya 2 sumber, **prefer member** (lebih
lengkap), fallback ke publik kalau member gagal/belum di-setup:

1. **Member** (`member.zeta-ai.pro/api/signals`) — butuh cookie
   `zeta_member=...`. **Bukan username/password akun kamu** — cuma
   passphrase/kode akses statis (kelihatan dari namanya, `zetamemberjuly2026`,
   kemungkinan besar **rotate tiap bulan**).
2. **Publik** (`idx-journal.zeta-ai.pro/api/idx-signals.json`) — fallback,
   tanpa login, tapi data gak lengkap.

### Secret tambahan: `ZETA_MEMBER_COOKIE`

Cara dapetin value-nya:
1. Buka https://member.zeta-ai.pro/ di browser (sambil login)
2. DevTools (F12) → tab **Network** → filter **Fetch/XHR**
3. Refresh halaman, cari request **`signals`**
4. Tab **Headers** → **Request Headers** → cari baris **`Cookie`**
5. Copy **seluruh value**-nya (format: `zeta_member=xxxxxxxxxxxxx`)
6. Simpan sebagai GitHub Secret **`ZETA_MEMBER_COOKIE`**

**Penting**: kemungkinan besar perlu di-**update manual tiap awal bulan**
(cookie-nya kelihatan encode nama bulan). Kalau automasi mendadak gagal
dengan error "Auth gagal" di log, itu tandanya — ulangi langkah di atas buat
dapetin cookie baru.

Kalau secret ini **gak diisi sama sekali**, script otomatis fallback ke
sumber publik (tetap jalan, cuma datanya kurang lengkap seperti sebelumnya).

### Cara kerja

Fetch data status final trade, convert ke format yang sama persis dengan
yang diharapkan `mergeWithXlsxData()` di `idx_signal.html`, lalu **replace
total** `trades_data.payload.lastXlsxRows` (bukan append/dedupe — sama
seperti upload XLSX manual, yang juga selalu replace total tiap upload).
**Gak perlu update `idx_signal.html` lagi** — patch yang sudah ada (v1.5)
otomatis re-merge pakai `lastXlsxRows` apa pun isinya, setiap kali app
di-load dari cloud.

Jadwalnya disesuaikan sama waktu Zeta AI ngaku sistem audit mereka jalan
(22:00, 01:00, 08:00 WIB) — asumsinya itu pas status SL HIT/EXPIRED di-update
di sisi mereka, jadi kita fetch beberapa menit sesudahnya.

File tambahan yang perlu di-drop ke repo (folder sama, `.github/workflows/`
buat yang `.yml`):
- `sync_journal.py`
- `.github/workflows/zeta-journal-sync.yml`

Testing-nya sama: **Actions → zeta-journal-sync → Run workflow** (manual),
cek hasilnya di Supabase, baru biarin jadwal otomatis jalan.

## File yang sudah disiapkan

| File | Isi |
|---|---|
| `ingest.py` | Script utama — login Telegram, fetch pesan baru, parsing (di-port 1:1 dari `idx_signal.html`), APPEND ke `trades_data.payload` (bukan tabel terpisah) |
| `requirements.txt` | Dependency Python (`telethon`, `supabase`) |
| `telegram-ingest.yml` | GitHub Actions workflow (jadwal cron + concurrency guard) |
| `generate_session.py` | Script sekali-pakai buat generate session string Telegram |

## Langkah 1 — Bikin repo GitHub (kalau belum ada)

Bikin repo baru (**private**, penting — ini nyimpen secrets & logic akses akun Telegram kamu).

Struktur folder:
```
repo/
├── .github/
│   └── workflows/
│       └── telegram-ingest.yml
├── ingest.py
└── requirements.txt
```
(`generate_session.py` JANGAN di-commit ke repo — itu cuma dijalankan lokal sekali, lalu buang/simpan sendiri di luar repo)

## Langkah 2 — Generate session string (WAJIB interaktif, gak bisa diwakilkan)

Di komputer kamu sendiri (bukan GitHub, bukan sandbox AI manapun):

```bash
pip install telethon
python generate_session.py
```

Kamu akan diminta:
1. `api_id` — bikin dulu di https://my.telegram.org kalau belum ada
2. `api_hash` — dari halaman yang sama
3. Nomor HP Telegram kamu
4. **Kode OTP** yang dikirim ke Telegram/SMS kamu — ini sebabnya harus kamu
   sendiri yang jalanin, gak ada AI yang bisa terima OTP ke HP kamu.

Setelah itu bakal muncul **session string** yang panjang. Simpan itu
sementara (jangan taruh di file/notes yang gampang bocor) — bakal dipakai
di Langkah 3.

## Langkah 3 — Isi GitHub Secrets

Di repo → Settings → Secrets and variables → Actions → New repository secret,
tambahin semua ini:

| Nama Secret | Isi |
|---|---|
| `TG_API_ID` | dari my.telegram.org |
| `TG_API_HASH` | dari my.telegram.org |
| `TG_SESSION_STRING` | hasil dari Langkah 2 |
| `TG_GROUP_ID` | username grup (`@namagrup`) atau numeric ID grup Zeta AI |
| `TG_TOPIC_ID` | ID topic (forum topics) tempat Zeta AI kirim sinyal — cari pakai `find_group_id.ipynb` (Cell 3) |
| `SUPABASE_URL` | URL project Supabase kamu |
| `SUPABASE_SERVICE_KEY` | service role key Supabase (bukan anon key — butuh akses insert langsung) |
| `SUPABASE_USER_ID` | **`1c744f4e-b811-44a9-a9a1-1977ec508700`** (sudah saya ambil dari data kamu yang ada sekarang, supaya data ingestion baru nyambung ke data lama) |

### Cara dapetin `TG_GROUP_ID` dan `TG_TOPIC_ID`

Karena grup Zeta AI kamu pakai fitur **Topics** (forum topics — sinyal
dikirim di topic khusus, bukan di chat umum), butuh 2 informasi:
`TG_GROUP_ID` (ID grupnya) dan `TG_TOPIC_ID` (ID topic spesifik tempat
sinyal dikirim).

Pakai notebook `find_group_id.ipynb` (sudah saya siapkan):
1. Cell 2 → nampilin daftar semua grup/channel kamu, cari grup Zeta AI, catat `id=...` → `TG_GROUP_ID`.
2. Cell 3 → masukin `GROUP_ID` yang barusan didapat, nanti nampilin daftar topic di dalam grup itu, cari topic yang isinya sinyal → catat `topic_id=...` → `TG_TOPIC_ID`.

Kalau grup kamu **ternyata gak pakai Topics** (cuma chat biasa), `TG_TOPIC_ID`
boleh dikosongin / gak usah diisi sama sekali — `ingest.py` sudah didesain
buat handle 2 kasus ini (kalau `TG_TOPIC_ID` gak di-set, ambil dari seluruh
grup seperti biasa).

## Langkah 4 — Test manual dulu (JANGAN langsung andelin jadwal otomatis)

1. Push semua file ke repo.
2. Di tab **Actions** GitHub → pilih workflow **telegram-ingest** → **Run workflow** (trigger manual, ini yang `workflow_dispatch: {}` di YAML).
3. Lihat log run-nya — pastikan:
   - Gak ada error auth Telegram
   - Muncul log `"Selesai: X signal, Y result, Z regime, ..."`
   - Gak ada `"Pesan yang gagal di-parse"` (kalau ada, itu perlu dicek — kemungkinan format pesan baru yang belum ke-cover parser)
4. Cek ke Supabase (kabari saya, saya bisa bantu verifikasi langsung) — pastikan `trades_data.payload.signals[]`/`.results[]`/`.regimes[]` nambah entry baru dengan `msg_id` yang valid, gak ada duplikat.
5. **Buka `idx_signal.html` di browser** (yang sudah di-deploy versi v1.5) — pastikan sinyal barunya kelihatan di tabel trade journal TANPA perlu upload JSON manual.
6. Ulangi `Run workflow` manual 2-3 kali berturut-turut — pastikan gak numpuk duplikat.
7. Kalau semua aman, baru biarin jadwal `schedule` (cron) jalan otomatis.

**Catatan cursor**: saya sudah reset `ingest_cursor` ke `7202` (titik terakhir yang sudah ke-cover data manual JSON kamu), jadi run pertama abis ini bakal cepat — cuma narik pesan yang bener-bener baru sejak upload manual terakhir, bukan re-backfill semua histori. Kamu gak perlu isi `backfill_days` lagi kecuali mau testing ulang secara sengaja.

## Catatan soal backfill (run pertama)

Karena `ingest_cursor` sekarang sudah di-reset ke `7202` (bukan `0`), run
pertama **gak bakal** full-backfill semua histori — cuma narik pesan baru
sejak titik itu. Kalau suatu saat kamu perlu full re-backfill (misal ganti
akun/pindah grup), tetap aman dilakukan — insert-nya pakai `msg_id` **asli**
Telegram sebagai key dedupe (`dedupeByMsgId`), jadi pesan yang sudah ada di
`trades_data.payload` gak bakal dobel, cuma di-overwrite/update di tempat
yang sama (idempotent).

## Soal data dari testing sebelumnya (tabel `signals`/`confirmations`)

Run testing kita sebelumnya (pas arsitektur masih salah sasaran) sempat
nulis 16 signal + 25 confirmation + 2 market_regime ke tabel ternormalisasi
(`signals`/`confirmations`/`market_regime` — punya IDXSY Screener). Data itu
**valid dan benar** (sudah kita verifikasi kualitasnya), jadi **gak perlu
dihapus** — anggap aja itu bonus data buat IDXSY Screener, gak mengganggu
apa pun di alur `trades_data` yang baru ini.

## Kalau ada error

Kirim log error-nya ke saya (dari tab Actions → run yang gagal → buka step
`python ingest.py`), saya bantu debug. Saya juga bisa langsung cek ke
Supabase kalau perlu verifikasi data masuk dengan benar atau ada masalah
duplikat/matching.
