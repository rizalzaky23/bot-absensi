# Bot Absen SPADA UPN

Otomasi pengisian presensi di [SPADA UPN](https://spada.upnyk.ac.id) (Moodle) memakai
Playwright (Chromium headless), lalu mengirim notifikasi real-time ke Telegram.

Fitur:
- Login otomatis ke SPADA (kredensial dari `.env`, tidak pernah di-hardcode).
- Deteksi sesi attendance yang masih terbuka pada tanggal hari ini.
- Klik "Submit attendance" → pilih "Hadir/Present" → "Save changes" → verifikasi status.
- Aman dijalankan berulang: bila sudah absen, bot melapor "sudah absen", tidak dobel.
- Notifikasi Telegram per mata kuliah + satu ringkasan di akhir.
- Screenshot otomatis (`artifacts/*.png`) saat gagal, untuk debugging.
- Keluar dengan exit code `0` (aman) / `1` (ada kegagalan) — cocok untuk cronjob.

## 1. Instalasi

Butuh Python 3.10+.

```bash
cd bot-absen
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium   # unduh browser (~150 MB, sekali saja)
```

Di server Linux tanpa dependensi sistem, tambahkan:

```bash
python -m playwright install --with-deps chromium
```

## 2. Konfigurasi

```bash
cp .env.example .env
```

Isi `.env`:

| Variabel | Keterangan |
| --- | --- |
| `SPADA_USERNAME` | NIM / username Moodle |
| `SPADA_PASSWORD` | Password Moodle |
| `TELEGRAM_BOT_TOKEN` | Token dari [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Chat ID tujuan (grup = id diawali `-100`) |
| `HEADLESS_MODE` | `true` (default) / `false` untuk melihat browser |
| `SPADA_ATTENDANCE_URLS` | cmid atau URL attendance, dipisah koma |
| `SKIP_COURSES` | Nama mata kuliah yang diabaikan (opsional) |
| `STEP_TIMEOUT_MS` | Timeout tiap langkah (default 20000) |
| `USER_AGENT` | UA custom (opsional) |
| `TELEGRAM_API_BASE` | Base URL Bot API (default `https://api.telegram.org`, untuk testing) |
| `ARTIFACT_DIR` | Folder screenshot (default `artifacts`) |

Cara mendapatkan `TELEGRAM_CHAT_ID`: kirim `/start` ke bot, lalu buka
`https://api.telegram.org/bot<TOKEN>/getUpdates` dan ambil `message.chat.id`.

## 3. Menjalankan

```bash
python main.py                                   # semua target dari .env
python main.py --headed                           # tampilkan browser (debug)
python main.py --url 12345 --url https://.../id=67890   # override target
```

Contoh keluaran:

```
[07:31:02] Browser siap (headless=True)
[07:31:05] Membuka https://spada.upnyk.ac.id/login/index.php ...
[07:31:09] Login berhasil
[07:31:10] Membuka https://spada.upnyk.ac.id/mod/attendance/view.php?id=12345
[07:31:14] Sesi aktif: 29 Sep 2026 16:00 Needs your attention
[07:31:18] Memilih status Hadir/Present
[07:31:20] Browser ditutup
```

## 4. Cronjob

Semua jam di server sebaiknya WIB (`Asia/Jakarta`).

**Linux / macOS** — `crontab -e`:

```cron
# setiap hari kerja (Senin-Jumat) jam 07:30 WIB
30 7 * * 1-5 cd /path/ke/bot-absen && .venv/bin/python main.py >> logs/absen.log 2>&1
```

**Windows** — Task Scheduler:

```bat
:: run_absen.bat
cd /d C:\path\ke\bot-absen
.venv\Scripts\python.exe main.py >> logs\absen.log 2>&1
```

Buat task: Trigger *Daily 07:30*, Action *Run a program* → `C:\path\ke\bot-absen\run_absen.bat`,
centang **Run whether user is logged on or not**.

> Pastikan server tidak tidur saat jadwal berjalan (macOS: `caffeinate -i`; Windows: disable sleep di Power settings).

## 5. Struktur kode

```
main.py
├── Config / AttendanceResult        # model data
├── load_config()                    # baca .env + argumen CLI
├── resolve_target() / session_reason() / build_summary()   # utilitas
├── TelegramNotifier                 # kirim pesan, format sukses & gagal
└── SpadaBot
    ├── start() / close()            # lifecycle Chromium
    ├── login() / _is_logged_in()    # langkah 3
    └── submit_attendance()          # langkah 4-5
        ├── _find_active_session()   # deteksi sesi hari ini yang terbuka
        ├── _already_present()       # sudah absen?
        ├── _fill_present()          # pilih radio Hadir
        ├── _save_changes()          # klik tombol simpan
        └── _verify()                # pastikan status benar-benar berubah
```

## 6. Test

```bash
python tests/smoke_test.py
```

Test memakai mock Moodle via Playwright route dan server Telegram HTTP lokal — tidak ada
request ke SPADA asli, tidak butuh kredensial. Cakupannya: submit berhasil, sudah absen,
sesi tertutup, login gagal, fallback selector, format pesan, dan payload Telegram.

## 7. Troubleshooting

| Gejala | Penyebab & solusi |
| --- | --- |
| `Login gagal: Invalid login` | Username harus NIM (bukan email), atau akun terkunci. |
| `No active session found` | Sesi belum dibuka / sudah ditutup. Atur `SKIP_COURSES` untuk mata kuliah tanpa absensi. |
| `Opsi 'Hadir/Present' tidak ditemukan` | Plugin absensi non-standar. Lihat `artifacts/no-radio-*.png`, lalu sesuaikan `SEL_PRESENCE_RADIO`. |
| `Status tidak berubah setelah submit` | Sesi ditutup di detik terakhir. Bot sengaja tidak melapor sukses. |
| Chromium gagal start | Jalankan ulang `python -m playwright install chromium`. |
| Telegram tidak terkirim | Cek `TELEGRAM_CHAT_ID` (harus chat, bukan username seperti `@namagrup`). |

Selector Moodle dikumpulkan sebagai daftar kandidat di bagian atas `main.py`
(`SEL_*`), sehingga mudah disesuaikan bila markup SPADA berubah.
