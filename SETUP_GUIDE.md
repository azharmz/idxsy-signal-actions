# Panduan Setup — Telegram Ingestion Automation (TG-ING-01)

Semua kode sudah saya siapkan & sudah dites syntax-nya. Bagian yang tersisa
di bawah ini **harus dikerjain manual oleh kamu sendiri** — gak bisa
diwakilkan ke AI manapun (saya atau agen lain), karena butuh akses langsung
ke akun Telegram & GitHub kamu.

## File yang sudah disiapkan

| File | Isi |
|---|---|
| `ingest.py` | Script utama — login Telegram, fetch pesan baru, parsing (di-port 1:1 dari `idx_signal.html`), insert ke Supabase |
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
| `SUPABASE_URL` | URL project Supabase kamu |
| `SUPABASE_SERVICE_KEY` | service role key Supabase (bukan anon key — butuh akses insert langsung) |
| `SUPABASE_USER_ID` | **`1c744f4e-b811-44a9-a9a1-1977ec508700`** (sudah saya ambil dari data kamu yang ada sekarang, supaya data ingestion baru nyambung ke data lama) |

### Cara dapetin `TG_GROUP_ID`

Kalau grupnya punya username publik (`t.me/namagrup`), tinggal pakai
`@namagrup`. Kalau grup private tanpa username, perlu numeric ID — cara
paling gampang: jalanin script kecil pakai session string yang sama
(`client.get_dialogs()`), cari nama grupnya, catat `.id`-nya. Kalau butuh,
saya bisa buatin script kecil ini juga.

## Langkah 4 — Test manual dulu (JANGAN langsung andelin jadwal otomatis)

1. Push semua file ke repo.
2. Di tab **Actions** GitHub → pilih workflow **telegram-ingest** → **Run workflow** (trigger manual, ini yang `workflow_dispatch: {}` di YAML).
3. Lihat log run-nya — pastikan:
   - Gak ada error auth Telegram
   - Muncul log `"Selesai: X signal, Y result, Z regime, ..."`
   - Gak ada `"Pesan yang gagal di-parse"` (kalau ada, itu perlu dicek — kemungkinan format pesan baru yang belum ke-cover parser)
4. Cek ke Supabase (kabari saya, saya bisa bantu verifikasi langsung) — pastikan baris baru masuk ke `signals`/`confirmations`/`market_regime` dengan `msg_id` yang valid, gak ada duplikat.
5. Ulangi `Run workflow` manual 2-3 kali berturut-turut — pastikan gak numpuk duplikat (ini persis testing yang kita rencanain sebelumnya).
6. Kalau semua aman, baru biarin jadwal `schedule` (cron) jalan otomatis.

## Catatan soal backfill (run pertama)

Karena `ingest_cursor` mulai dari `0`, run pertama bakal narik **SEMUA histori
grup dari awal** (bukan cuma pesan baru). Ini **aman**, bukan masalah —
karena insert-nya pakai `msg_id` **asli** Telegram sebagai key upsert,
jadi kalau pesan itu kebetulan sudah pernah masuk ke `signals`/`confirmations`
lewat cara lain (JSON export manual), run ini cuma **update** baris yang sama
(idempotent), bukan bikin duplikat baru.

Yang perlu diantisipasi cuma soal **durasi run pertama** — narik 900+ pesan
lama makanya bisa makan waktu lebih lama dari run rutin (yang cuma beberapa
pesan baru). GitHub Actions kasih waktu sampai 6 jam per run, jadi ini masih
jauh dari limit — gak perlu tindakan khusus, cukup sabar nunggu run pertama
kelar (mungkin beberapa menit), baru pantau hasilnya.

## Kalau ada error

Kirim log error-nya ke saya (dari tab Actions → run yang gagal → buka step
`python ingest.py`), saya bantu debug. Saya juga bisa langsung cek ke
Supabase kalau perlu verifikasi data masuk dengan benar atau ada masalah
duplikat/matching.
