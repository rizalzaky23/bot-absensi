"""
Telegram Bot Daemon multi-user untuk presensi SPADA UPN.

Fitur:
- Pemilik (TELEGRAM_CHAT_ID di .env) bebas registrasi & pembayaran, serta bertindak sebagai admin.
- User lain mendaftar (/daftar NIM + password), memilih paket (/bayar), upload bukti,
  lalu setelah /approve oleh admin, bot otomatis scan mata kuliah SPADA dan absen.
"""

from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from main import (
    DEFAULT_USER_AGENT,
    AttendanceResult,
    Config,
    SpadaBot,
    SpadaError,
    TelegramNotifier,
    _env_bool,
    _split_list,
    build_summary,
    guess_course_name,
    load_config,
    log,
    now_local,
    resolve_target,
)
from user_store import UserRecord, UserStore, format_packages_text, get_package


class TelegramBotDaemon:
    """Long polling Telegram Bot listener multi-user."""

    def __init__(self) -> None:
        load_dotenv()
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.authorized_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.api_base = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        self.auto_enabled = os.getenv("AUTO_ABSEN", "true").lower() in {"1", "true", "yes"}
        self.check_interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
        if not self.token:
            raise SystemExit("TELEGRAM_BOT_TOKEN belum diset di .env")

        self.store = UserStore(Path(os.getenv("DB_PATH", "bot_users.db")))
        self._pending_reg: dict[str, dict[str, str]] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._browser_sem = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_BROWSERS", "2")))

        default_qr = Path(__file__).resolve().parent / "assets" / "qris.jpg"
        self.payment_info = (
            os.getenv("PAYMENT_INFO", "").strip()
            or "QRIS *RZF STORE, KLTN SLT* (NMID: `ID1024339160201`)\n"
            "Terima pembayaran QRIS dari e-wallet & m-banking mana saja."
        )
        self.payment_qr_path = os.getenv("PAYMENT_QR_PATH", "").strip() or (
            str(default_qr) if default_qr.is_file() else ""
        )
        self.user_gap_min = float(os.getenv("USER_GAP_MIN_SECONDS", "15"))
        self.user_gap_max = float(os.getenv("USER_GAP_MAX_SECONDS", "45"))
        self.target_gap_min = float(os.getenv("TARGET_GAP_MIN_SECONDS", "2"))
        self.target_gap_max = float(os.getenv("TARGET_GAP_MAX_SECONDS", "5"))
        self.submit_jitter = int(os.getenv("SUBMIT_JITTER_SECONDS", "25"))

    def _is_owner(self, chat_id: str | int) -> bool:
        return bool(self.authorized_chat_id) and str(chat_id) == self.authorized_chat_id

    def _user_lock(self, chat_id: str | int) -> asyncio.Lock:
        return self._user_locks.setdefault(str(chat_id), asyncio.Lock())

    async def send_message(self, chat_id: str | int, text: str) -> bool:
        """Kirim pesan Markdown ke Telegram (fallback plain text bila Markdown ditolak)."""
        url = f"{self.api_base}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, json=payload)
                if res.status_code == 400:
                    fallback = {k: v for k, v in payload.items() if k != "parse_mode"}
                    res = await client.post(url, json=fallback)
                if res.status_code != 200:
                    log(f"! Gagal kirim pesan ke Telegram ({res.status_code}): {res.text[:120]}")
                    return False
                return True
        except Exception as exc:
            log(f"! HTTP error kirim Telegram: {exc}")
            return False

    async def delete_message(self, chat_id: str | int, message_id: int | None) -> bool:
        """Hapus pesan di chat Telegram (best-effort, misalnya pesan password)."""
        if not message_id:
            return False
        url = f"{self.api_base}/bot{self.token}/deleteMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    url, json={"chat_id": str(chat_id), "message_id": message_id}
                )
                return res.status_code == 200
        except Exception:
            return False

    async def send_photo(self, chat_id: str | int, photo: str | Path, caption: str = "") -> bool:
        """Kirim foto via file_id Telegram atau file lokal."""
        url = f"{self.api_base}/bot{self.token}/sendPhoto"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                photo_path = Path(photo) if isinstance(photo, Path) else None
                if photo_path and photo_path.is_file():
                    photo_bytes = photo_path.read_bytes()
                    res = await client.post(
                        url,
                        data={"chat_id": str(chat_id), "caption": caption, "parse_mode": "Markdown"},
                        files={"photo": (photo_path.name, photo_bytes)},
                    )
                    if res.status_code == 400:
                        res = await client.post(
                            url,
                            data={"chat_id": str(chat_id), "caption": caption},
                            files={"photo": (photo_path.name, photo_bytes)},
                        )
                else:
                    res = await client.post(
                        url,
                        json={
                            "chat_id": str(chat_id),
                            "photo": str(photo),
                            "caption": caption,
                            "parse_mode": "Markdown",
                        },
                    )
                    if res.status_code == 400:
                        res = await client.post(
                            url,
                            json={
                                "chat_id": str(chat_id),
                                "photo": str(photo),
                                "caption": caption,
                            },
                        )
                if res.status_code != 200:
                    log(f"! Gagal kirim foto ke Telegram ({res.status_code}): {res.text[:120]}")
                    return False
                return True
        except Exception as exc:
            log(f"! HTTP error kirim foto Telegram: {exc}")
            return False

    def _build_config_for_chat(
        self,
        chat_id: str,
        cli_urls: list[str] | None = None,
        submit_delay_seconds: int | None = None,
        allow_empty_targets: bool = False,
    ) -> Config:
        if self._is_owner(chat_id):
            cfg = load_config(
                cli_urls=cli_urls or (),
                submit_delay_seconds=submit_delay_seconds,
            )
            cfg.submit_jitter_seconds = self.submit_jitter
            cfg.target_gap_range = (self.target_gap_min, self.target_gap_max)
            return cfg

        user = self.store.get_user(chat_id)
        if user is None:
            raise ValueError("User belum terdaftar.")
        targets = list(cli_urls) if cli_urls else list(user.attendance_targets)
        if not targets and not allow_empty_targets:
            raise ValueError("Belum ada mata kuliah attendance tersimpan. Jalankan `/sync`.")

        delay_env = int(os.getenv("SUBMIT_DELAY_SECONDS", "60"))
        delay = submit_delay_seconds if submit_delay_seconds is not None else delay_env
        return Config(
            username=user.spada_username,
            password=user.spada_password,
            telegram_token=self.token,
            telegram_chat_id=str(chat_id),
            headless=_env_bool("HEADLESS_MODE", True),
            attendance_targets=targets,
            skip_courses=[item.lower() for item in _split_list(os.getenv("SKIP_COURSES"))],
            timeout_ms=int(os.getenv("STEP_TIMEOUT_MS", "20000")),
            user_agent=os.getenv("USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
            artifact_dir=Path(os.getenv("ARTIFACT_DIR", "artifacts")),
            submit_delay_seconds=delay,
            submit_jitter_seconds=self.submit_jitter,
            target_gap_range=(self.target_gap_min, self.target_gap_max),
        )

    def _check_active_access(self, chat_id: str) -> str | None:
        """Kembalikan pesan penolakan bila user belum terdaftar / belum aktif."""
        if self._is_owner(chat_id):
            return None
        user = self.store.get_user(chat_id)
        if user is None:
            return (
                "⛔ *Akses Ditolak*\n"
                f"Anda belum terdaftar (Chat ID: `{chat_id}`).\n"
                "Gunakan /daftar untuk mendaftarkan akun SPADA Anda."
            )
        now = now_local()
        if not user.is_active(now):
            if user.status == "active":
                self.store.list_active_users(now)
            status_label = (
                "kadaluarsa"
                if user.expired_at and user.expired_at <= now
                else "belum aktif (menunggu pembayaran)"
            )
            return (
                "⛔ *Langganan Tidak Aktif*\n"
                f"Status akun Anda: *{status_label}*.\n"
                "Gunakan /bayar untuk memilih paket dan mengaktifkan bot."
            )
        return None

    async def _sleep_target_gap(self) -> None:
        if self.target_gap_max > 0:
            await asyncio.sleep(random.uniform(self.target_gap_min, self.target_gap_max))

    async def handle_start(self, chat_id: str | int) -> None:
        """Balas perintah /start atau /help."""
        cid = str(chat_id)
        self._pending_reg.pop(cid, None)
        if self._is_owner(cid):
            auto_status = "🟢 AKTIF" if self.auto_enabled else "🔴 NONAKTIF"
            header = f"👑 *Mode Pemilik (Tanpa Registrasi/Bayar)*\nStatus Auto-Absen: {auto_status}"
        else:
            user = self.store.get_user(cid)
            if user and user.is_active(now_local()):
                auto_status = "🟢 AKTIF" if user.auto_enabled else "🔴 NONAKTIF"
                assert user.expired_at is not None
                header = (
                    f"🟢 *Status Akun:* Aktif s/d {user.expired_at:%d %b %Y %H:%M} WIB\n"
                    f"Status Auto-Absen: {auto_status}"
                )
            elif user:
                header = "🟡 *Status Akun:* Terdaftar (menunggu pembayaran/aktivasi). Gunakan /bayar."
            else:
                header = "⚪ *Belum Terdaftar.* Klik atau ketik /daftar untuk mendaftarkan akun SPADA Anda."

        msg = (
            "🤖 *Selamat Datang di Bot Presensi SPADA UPN!*\n\n"
            f"{header}\n"
            f"(Cek otomatis tiap ~{self.check_interval_seconds // 60} menit)\n\n"
            "Perintah Akun & Pembayaran:\n"
            "• `/daftar` — Daftar / perbarui NIM & password SPADA\n"
            "• `/bayar` — Lihat paket (mingguan/bulanan/semester) & cara bayar\n"
            "• `/akun` — Cek status langganan & jumlah mata kuliah\n"
            "• `/sync` — Scan ulang daftar mata kuliah SPADA\n"
            "• `/batal` — Batalkan proses input pendaftaran\n\n"
            "Perintah Presensi:\n"
            "• `/absen` — Cek & isi absen semua mata kuliah sekarang\n"
            "• `/absen now` — Cek & isi absen langsung tanpa jeda waktu\n"
            "• `/absen <id>` — Absen khusus satu ID mata kuliah\n"
            "• `/auto on` / `/auto off` — Aktifkan / matikan mode absen otomatis\n"
            "• `/cek` atau `/status` — Cek sesi terbuka hari ini tanpa mengisi\n"
            "• `/list` — Daftar ID mata kuliah Anda\n"
        )
        if self._is_owner(cid):
            msg += (
                "\nPerintah Admin:\n"
                "• `/approve <chat_id> [paket]` — Setujui pembayaran & jalankan bot user\n"
                "• `/reject <chat_id> [alasan]` — Tolak pembayaran pending\n"
                "• `/users` — Daftar seluruh user terdaftar\n"
            )
        await self.send_message(chat_id, msg)

    async def handle_daftar(self, chat_id: str) -> None:
        if self._is_owner(chat_id):
            await self.send_message(
                chat_id,
                "ℹ️ ID Anda adalah pemilik bot — tidak perlu mendaftar atau membayar.",
            )
            return
        self._pending_reg[chat_id] = {"step": "nim"}
        await self.send_message(
            chat_id,
            "📝 *Pendaftaran Akun SPADA*\n"
            "Silakan kirim *NIM / Username SPADA* Anda sekarang.\n"
            "_(Ketik `/batal` untuk membatalkan.)_",
        )

    async def handle_batal(self, chat_id: str) -> None:
        if self._pending_reg.pop(chat_id, None) is not None:
            await self.send_message(chat_id, "❌ Pendaftaran dibatalkan.")
        else:
            await self.send_message(chat_id, "ℹ️ Tidak ada proses pendaftaran yang aktif.")

    async def _handle_reg_step(
        self, chat_id: str, username: str, text: str, message_id: int | None
    ) -> None:
        state = self._pending_reg[chat_id]
        if state["step"] == "nim":
            if not text or any(ch.isspace() for ch in text):
                await self.send_message(
                    chat_id,
                    "⚠️ NIM/username tidak valid (tidak boleh mengandung spasi). Kirim ulang atau ketik `/batal`.",
                )
                return
            state["nim"] = text
            state["step"] = "password"
            await self.send_message(
                chat_id,
                "🔐 Sekarang kirim *password SPADA* Anda.\n"
                "_(Pesan password akan langsung dihapus setelah diterima dan disimpan terenkripsi.)_",
            )
            return

        if state["step"] == "password":
            await self.delete_message(chat_id, message_id)
            nim = state["nim"]
            self._pending_reg.pop(chat_id, None)
            user = self.store.upsert_credentials(chat_id, username, nim, text, now_local())
            if user.is_active(now_local()):
                await self.send_message(
                    chat_id,
                    f"✅ Kredensial SPADA untuk NIM `{nim}` berhasil diperbarui.\n"
                    "Jalankan `/sync` bila ingin memindai ulang mata kuliah.",
                )
            else:
                await self.send_message(
                    chat_id,
                    f"✅ *Akun SPADA (`{nim}`) Tersimpan!*\n\n"
                    "Pilih paket langganan untuk mengaktifkan bot:\n"
                    f"{format_packages_text()}\n\n"
                    "Ketik `/bayar mingguan`, `/bayar bulanan`, atau `/bayar semester` untuk mendapatkan info pembayaran.",
                )

    async def handle_bayar(self, chat_id: str, text: str) -> None:
        if self._is_owner(chat_id):
            await self.send_message(chat_id, "ℹ️ Akun pemilik tidak memerlukan pembayaran.")
            return
        user = self.store.get_user(chat_id)
        if user is None:
            await self.send_message(
                chat_id, "⚠️ Anda belum terdaftar. Ketik `/daftar` terlebih dahulu."
            )
            return

        parts = text.strip().split()
        if len(parts) < 2:
            await self.send_message(
                chat_id,
                "💳 *Paket Langganan Bot Absen*\n\n"
                f"{format_packages_text()}\n\n"
                f"*Tujuan Pembayaran:*\n{self.payment_info}\n\n"
                "Ketik `/bayar mingguan`, `/bayar bulanan`, atau `/bayar semester` untuk membuat tagihan.",
            )
            return

        resolved = get_package(parts[1])
        if resolved is None:
            await self.send_message(
                chat_id,
                "⚠️ Paket tidak dikenali. Pilih: `mingguan`, `bulanan`, atau `semester`.",
            )
            return

        pkg_key, _, _, _ = resolved
        payment = self.store.create_payment(chat_id, pkg_key, now_local())
        amount_str = f"Rp {payment['amount']:,}".replace(",", ".")
        await self.send_message(
            chat_id,
            f"🧾 *Tagihan #{payment['id']}*\n"
            f"• *Paket:* {payment['label']}\n"
            f"• *Nominal:* *{amount_str}*\n"
            f"• *Tujuan Pembayaran:*\n{self.payment_info}\n\n"
            "Setelah transfer, kirim *foto/screenshot bukti pembayaran* langsung ke chat ini.",
        )
        if self.payment_qr_path and Path(self.payment_qr_path).is_file():
            await self.send_photo(
                chat_id,
                Path(self.payment_qr_path),
                f"QRIS Pembayaran Bot Absen — RZF STORE, KLTN SLT\nNominal: *{amount_str}* ({payment['label']})",
            )

    async def _handle_payment_proof(
        self, chat_id: str, username: str, file_id: str
    ) -> None:
        user = self.store.get_user(chat_id)
        if user is None:
            await self.send_message(chat_id, "⚠️ Anda belum terdaftar. Gunakan `/daftar` dulu.")
            return
        pending = self.store.attach_payment_proof(chat_id, file_id)
        if pending is None:
            await self.send_message(
                chat_id,
                "⚠️ Anda belum memilih paket pembayaran. Ketik `/bayar` untuk memilih paket.",
            )
            return

        await self.send_message(
            chat_id,
            "✅ *Bukti pembayaran diterima!*\n"
            "Mohon tunggu verifikasi admin. Begitu disetujui, bot langsung mengecek mata kuliah SPADA Anda dan mulai bekerja.",
        )
        if self.authorized_chat_id:
            pkg = get_package(pending["package"])
            pkg_label = pkg[1] if pkg else pending["package"]
            amount_str = f"Rp {pending['amount']:,}".replace(",", ".")
            caption = (
                "💰 *Bukti Pembayaran Baru*\n"
                f"• *User:* @{username} (`{chat_id}`)\n"
                f"• *NIM:* `{user.spada_username}`\n"
                f"• *Paket:* {pkg_label}\n"
                f"• *Nominal:* {amount_str}\n\n"
                f"Setujui: `/approve {chat_id}`\n"
                f"Tolak: `/reject {chat_id} <alasan>`"
            )
            await self.send_photo(self.authorized_chat_id, file_id, caption)

    async def handle_akun(self, chat_id: str) -> None:
        if self._is_owner(chat_id):
            await self.send_message(
                chat_id,
                "👑 *Akun Pemilik (Exempt)*\n"
                "Tidak perlu registrasi/pembayaran. Konfigurasi diambil dari `.env`.",
            )
            return
        now = now_local()
        self.store.list_active_users(now)
        user = self.store.get_user(chat_id)
        if user is None:
            await self.send_message(chat_id, "ℹ️ Anda belum terdaftar. Ketik `/daftar` untuk mendaftar.")
            return

        exp_str = f"{user.expired_at:%d %b %Y %H:%M} WIB" if user.expired_at else "-"
        auto_str = "🟢 AKTIF" if user.auto_enabled else "🔴 NONAKTIF"
        await self.send_message(
            chat_id,
            "👤 *Informasi Akun*\n"
            f"• *NIM SPADA:* `{user.spada_username}`\n"
            f"• *Status:* `{user.status}`\n"
            f"• *Aktif Hingga:* {exp_str}\n"
            f"• *Mata Kuliah Terdeteksi:* {len(user.attendance_targets)} modul\n"
            f"• *Auto-Absen:* {auto_str}",
        )

    async def handle_approve(self, chat_id: str, text: str) -> None:
        if not self._is_owner(chat_id):
            await self.send_message(chat_id, "⛔ Perintah ini hanya untuk admin.")
            return
        parts = text.strip().split()
        if len(parts) < 2:
            await self.send_message(chat_id, "Format: `/approve <chat_id> [mingguan|bulanan|semester]`")
            return

        target_id = parts[1].strip()
        pkg_override = parts[2].strip() if len(parts) > 2 else None
        try:
            user, label, amount = self.store.approve_payment(
                target_id, now_local(), package_override=pkg_override
            )
        except ValueError as exc:
            await self.send_message(chat_id, f"⚠️ {exc}")
            return

        assert user.expired_at is not None
        exp_str = f"{user.expired_at:%d %b %Y %H:%M} WIB"
        amount_str = f"Rp {amount:,}".replace(",", ".")
        await self.send_message(
            chat_id,
            f"✅ User `{target_id}` (@{user.telegram_username or '-'}) diaktifkan.\n"
            f"• Paket: {label} ({amount_str})\n"
            f"• Aktif hingga: {exp_str}\n"
            "Memulai scan mata kuliah & presensi otomatis...",
        )
        await self.send_message(
            target_id,
            "🎉 *Pembayaran Disetujui!*\n"
            f"• *Paket:* {label}\n"
            f"• *Aktif Hingga:* {exp_str}\n\n"
            "Bot sekarang mengecek daftar mata kuliah SPADA Anda dan langsung memproses absensi yang terbuka...",
        )
        asyncio.create_task(self._onboard_and_run_user(target_id))

    async def handle_reject(self, chat_id: str, text: str) -> None:
        if not self._is_owner(chat_id):
            await self.send_message(chat_id, "⛔ Perintah ini hanya untuk admin.")
            return
        parts = text.strip().split(maxsplit=2)
        if len(parts) < 2:
            await self.send_message(chat_id, "Format: `/reject <chat_id> [alasan]`")
            return
        target_id = parts[1].strip()
        reason = parts[2].strip() if len(parts) > 2 else "Bukti pembayaran tidak valid"
        if not self.store.reject_payment(target_id, now_local(), reason=reason):
            await self.send_message(chat_id, f"⚠️ Tidak ada pembayaran pending untuk `{target_id}`.")
            return
        await self.send_message(chat_id, f"❌ Pembayaran user `{target_id}` ditolak ({reason}).")
        await self.send_message(
            target_id,
            f"❌ *Pembayaran Ditolak*\nAlasan: {reason}\nSilakan kirim ulang `/bayar <paket>` dan bukti transfer yang valid.",
        )

    async def handle_users(self, chat_id: str) -> None:
        if not self._is_owner(chat_id):
            await self.send_message(chat_id, "⛔ Perintah ini hanya untuk admin.")
            return
        now = now_local()
        self.store.list_active_users(now)
        users = self.store.list_users()
        if not users:
            await self.send_message(chat_id, "ℹ️ Belum ada user terdaftar selain pemilik.")
            return
        lines = [f"👥 *Daftar User ({len(users)}):*"]
        for u in users:
            exp = f"{u.expired_at:%d/%m/%y}" if u.expired_at else "-"
            lines.append(
                f"• `{u.chat_id}` (@{u.telegram_username or '-'}) | NIM `{u.spada_username}` "
                f"| `{u.status}` | exp {exp} | {len(u.attendance_targets)} matkul"
            )
        await self.send_message(chat_id, "\n".join(lines))

    async def _onboard_and_run_user(self, chat_id: str) -> None:
        """Login ke SPADA user, scan matkul berabsensi, simpan target, dan langsung absen."""
        lock = self._user_lock(chat_id)
        async with lock, self._browser_sem:
            try:
                cfg = self._build_config_for_chat(chat_id, allow_empty_targets=True)
            except Exception as exc:
                await self.send_message(chat_id, f"⚠️ Gagal memuat akun: {exc}")
                return

            bot = SpadaBot(cfg)
            results: list[AttendanceResult] = []
            notifier = TelegramNotifier(self.token, chat_id)
            try:
                await bot.start()
                await bot.login()
                discovered = await bot.discover_attendance_targets()
                cmids = [cmid for cmid, _ in discovered]
                self.store.set_targets(chat_id, cmids, now_local())

                if not discovered:
                    await self.send_message(
                        chat_id,
                        "⚠️ Tidak ditemukan modul attendance pada mata kuliah SPADA Anda.\n"
                        "Coba jalankan `/sync` nanti jika dosen baru menambahkan absensi.",
                    )
                    return

                matkul_lines = [f"• *{name}* (`{cmid}`)" for cmid, name in discovered]
                await self.send_message(
                    chat_id,
                    f"📚 *Ditemukan {len(discovered)} modul attendance:*\n"
                    + "\n".join(matkul_lines)
                    + "\n\n⏳ Mengecek & mengisi presensi yang sedang dibuka...",
                )

                for idx, target in enumerate(cmids):
                    if idx > 0:
                        await self._sleep_target_gap()
                    result = await bot.submit_attendance(target)
                    results.append(result)
                    if result.status == "success":
                        await notifier.send(
                            TelegramNotifier.format_success(
                                result.course,
                                result.timestamp,
                                "Berhasil diisi otomatis setelah aktivasi akun",
                            )
                        )
                    elif result.status == "failed":
                        await notifier.send(
                            TelegramNotifier.format_failure(
                                result.course, result.timestamp, result.reason
                            )
                        )
            except SpadaError as exc:
                await self.send_message(
                    chat_id,
                    f"⚠️ *Login SPADA gagal:* `{exc}`\n"
                    "Perbarui NIM & password Anda dengan `/daftar`, lalu jalankan `/sync`.",
                )
                if self.authorized_chat_id and not self._is_owner(chat_id):
                    await self.send_message(
                        self.authorized_chat_id,
                        f"⚠️ Onboarding user `{chat_id}` gagal login SPADA: `{exc}`",
                    )
            except Exception as exc:
                log(f"! Error onboarding user {chat_id}: {exc}")
                await self.send_message(chat_id, f"⚠️ Terjadi kesalahan saat scan mata kuliah: `{exc}`")
            finally:
                await bot.close()

            if results:
                await self.send_message(chat_id, build_summary(results))

    async def handle_sync(self, chat_id: str) -> None:
        """Scan ulang daftar mata kuliah & modul attendance dari akun SPADA."""
        if denial := self._check_active_access(chat_id):
            await self.send_message(chat_id, denial)
            return

        lock = self._user_lock(chat_id)
        if lock.locked():
            await self.send_message(chat_id, "⚠️ Sedang ada proses absen yang berjalan. Mohon tunggu.")
            return

        async with lock, self._browser_sem:
            await self.send_message(chat_id, "🔍 *Memindai daftar mata kuliah SPADA Anda...*")
            try:
                cfg = self._build_config_for_chat(chat_id, allow_empty_targets=True)
            except Exception as exc:
                await self.send_message(chat_id, f"⚠️ {exc}")
                return

            bot = SpadaBot(cfg)
            try:
                await bot.start()
                await bot.login()
                discovered = await bot.discover_attendance_targets()
            except Exception as exc:
                await self.send_message(chat_id, f"⚠️ Gagal scan mata kuliah: `{exc}`")
                return
            finally:
                await bot.close()

            if not discovered:
                await self.send_message(chat_id, "ℹ️ Tidak ditemukan modul attendance di SPADA Anda.")
                return

            if not self._is_owner(chat_id):
                self.store.set_targets(chat_id, [c for c, _ in discovered], now_local())
            lines = [f"• *{name}* (`{cmid}`)" for cmid, name in discovered]
            note = (
                "\n\n_Mode pemilik: daftar `.env` tidak diubah otomatis._"
                if self._is_owner(chat_id)
                else "\n\n_Daftar ini telah disimpan untuk auto-absen & `/absen`._"
            )
            await self.send_message(
                chat_id,
                f"📚 *Ditemukan {len(discovered)} modul attendance:*\n" + "\n".join(lines) + note,
            )

    async def handle_auto(self, chat_id: str | int, text: str) -> None:
        """Ubah atau cek status auto-absen berkala."""
        cid = str(chat_id)
        if denial := self._check_active_access(cid):
            await self.send_message(chat_id, denial)
            return

        is_owner = self._is_owner(cid)
        parts = text.strip().split()
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub in {"on", "enable", "aktif"}:
                if is_owner:
                    self.auto_enabled = True
                else:
                    self.store.set_auto(cid, True, now_local())
                await self.send_message(
                    chat_id,
                    f"🟢 *Auto-Absen DIAKTIFKAN*\n"
                    f"Bot akan memeriksa SPADA otomatis setiap ~{self.check_interval_seconds // 60} menit "
                    f"pada jam kuliah (06:45 - 18:30 WIB, Senin-Jumat). "
                    f"Jika sesi dibuka dosen, bot otomatis absen dan melapor ke sini.",
                )
                return
            elif sub in {"off", "disable", "matikan"}:
                if is_owner:
                    self.auto_enabled = False
                else:
                    self.store.set_auto(cid, False, now_local())
                await self.send_message(
                    chat_id,
                    "🔴 *Auto-Absen DIMATIKAN*\nBot hanya akan absen jika Anda kirim perintah `/absen`.",
                )
                return

        current = (
            self.auto_enabled
            if is_owner
            else bool((u := self.store.get_user(cid)) and u.auto_enabled)
        )
        status = "🟢 AKTIF" if current else "🔴 NONAKTIF"
        await self.send_message(
            chat_id,
            f"ℹ️ *Status Auto-Absen:* {status}\n"
            f"• Interval periksa: tiap ~{self.check_interval_seconds // 60} menit\n"
            f"• Jam aktif: 06:45 - 18:30 WIB (Senin-Jumat)\n\n"
            f"Gunakan `/auto on` atau `/auto off` untuk mengubah.",
        )

    async def _run_auto_watcher_for_user(self, chat_id: str) -> None:
        lock = self._user_lock(chat_id)
        if lock.locked():
            return
        async with lock, self._browser_sem:
            bot: SpadaBot | None = None
            try:
                cfg = self._build_config_for_chat(chat_id)
                bot = SpadaBot(cfg)
                await bot.start()
                await bot.login()

                for idx, target in enumerate(cfg.attendance_targets):
                    if idx > 0:
                        await self._sleep_target_gap()
                    url = resolve_target(target)
                    assert bot.page is not None
                    await bot.page.goto(url, wait_until="domcontentloaded")
                    await bot._settle()

                    link, _ = await bot._find_active_session(bot.page)
                    if link is None:
                        continue

                    log(f"[Auto-Absen][{chat_id}] Sesi aktif ditemukan untuk {target}!")
                    result = await bot.submit_attendance(target)
                    if result.status == "success":
                        log(f"[Auto-Absen][{chat_id}] Berhasil absen: {result.course}")
                        await self.send_message(
                            chat_id,
                            TelegramNotifier.format_success(
                                result.course,
                                result.timestamp,
                                "Presensi tercatat otomatis saat sesi kelas dibuka (auto-watcher)",
                            ),
                        )
                    elif result.status == "failed":
                        log(f"[Auto-Absen][{chat_id}] Gagal submit: {result.course} ({result.reason})")
                        await self.send_message(
                            chat_id,
                            TelegramNotifier.format_failure(
                                result.course, result.timestamp, result.reason
                            ),
                        )
            except Exception as exc:
                log(f"! Error pada auto-watcher user {chat_id}: {exc}")
            finally:
                if bot is not None:
                    await bot.close()

    async def auto_watcher_loop(self) -> None:
        """Loop latar belakang untuk memeriksa dan mengisi absensi otomatis dengan jeda antar-user."""
        log(f"Auto-watcher berjalan (interval: ~{self.check_interval_seconds}s, auto={self.auto_enabled})")
        # shortcut: sequential staggered auto-watcher (one user at a time); add worker pool if >30 active users
        while True:
            jitter_wait = max(30.0, self.check_interval_seconds + random.uniform(-30, 60))
            await asyncio.sleep(jitter_wait)

            now = now_local()
            minute_of_day = now.hour * 60 + now.minute
            if now.weekday() > 4 or not (6 * 60 + 45 <= minute_of_day <= 18 * 60 + 30):
                continue

            candidate_ids: list[str] = []
            if self.authorized_chat_id and self.auto_enabled:
                candidate_ids.append(self.authorized_chat_id)
            for u in self.store.list_active_users(now):
                if u.chat_id != self.authorized_chat_id and u.auto_enabled and u.attendance_targets:
                    candidate_ids.append(u.chat_id)

            random.shuffle(candidate_ids)
            for idx, uid in enumerate(candidate_ids):
                if idx > 0 and self.user_gap_max > 0:
                    await asyncio.sleep(random.uniform(self.user_gap_min, self.user_gap_max))
                await self._run_auto_watcher_for_user(uid)

    async def handle_list(self, chat_id: str | int) -> None:
        """Tampilkan daftar target attendance."""
        cid = str(chat_id)
        if denial := self._check_active_access(cid):
            await self.send_message(chat_id, denial)
            return
        try:
            cfg = self._build_config_for_chat(cid)
            targets = cfg.attendance_targets
            lines = [f"• `{t}`" for t in targets]
            msg = (
                f"📋 *Daftar Target Attendance ({len(targets)} modul):*\n"
                + "\n".join(lines)
                + "\n\n_Untuk absen satuan: `/absen <id>`_"
            )
        except Exception as exc:
            msg = f"⚠️ Gagal memuat daftar: {exc}"
        await self.send_message(chat_id, msg)

    async def handle_cek(self, chat_id: str | int) -> None:
        """Cek sesi tanpa submit absensi."""
        cid = str(chat_id)
        if denial := self._check_active_access(cid):
            await self.send_message(chat_id, denial)
            return

        lock = self._user_lock(cid)
        if lock.locked():
            await self.send_message(chat_id, "⚠️ Sedang ada proses absen yang berjalan. Mohon tunggu.")
            return

        try:
            cfg = self._build_config_for_chat(cid, submit_delay_seconds=0)
        except Exception as exc:
            await self.send_message(chat_id, f"⚠️ {exc}")
            return

        async with lock, self._browser_sem:
            await self.send_message(chat_id, "🔍 *Mengecek jadwal presensi SPADA hari ini...*")
            bot = SpadaBot(cfg)
            open_sessions: list[str] = []
            upcoming: list[str] = []
            already_done: list[str] = []

            try:
                await bot.start()
                await bot.login()
                for idx, target in enumerate(cfg.attendance_targets):
                    if idx > 0:
                        await self._sleep_target_gap()
                    url = resolve_target(target)
                    assert bot.page is not None
                    await bot.page.goto(url, wait_until="domcontentloaded")
                    await bot._settle()
                    cname = await guess_course_name(bot.page)
                    if any(skip in cname.lower() for skip in cfg.skip_courses):
                        continue

                    for status, schedule in await bot.get_today_sessions(bot.page):
                        entry_head = f"*{cname}* (`{target}`)\n  _{schedule}_"
                        if status == "open":
                            open_sessions.append(f"🟢 {entry_head}")
                        elif status == "already":
                            already_done.append(f"☑️ {entry_head}")
                        else:
                            upcoming.append(f"⏳ {entry_head}")
            except Exception as exc:
                await self.send_message(chat_id, f"⚠️ Terjadi kesalahan saat cek sesi: {exc}")
                return
            finally:
                await bot.close()

            report = [
                "📊 *Jadwal Presensi SPADA Hari Ini*",
                f"• *Tanggal:* {now_local():%d %B %Y} (WIB)",
            ]
            if not (open_sessions or upcoming or already_done):
                report.append("\n_Tidak ada jadwal presensi pada hari ini._")
            else:
                if open_sessions:
                    report.append("\n*Sesi Siap Diisi:*")
                    report.extend(open_sessions)
                if upcoming:
                    report.append("\n*Belum Dibuka / Ditutup:*")
                    report.extend(upcoming)
                if already_done:
                    report.append("\n*Sudah Terisi Hari Ini:*")
                    report.extend(already_done)

            await self.send_message(chat_id, "\n".join(report))

    async def handle_absen(self, chat_id: str | int, text: str) -> None:
        """Proses perintah /absen."""
        cid = str(chat_id)
        if denial := self._check_active_access(cid):
            await self.send_message(chat_id, denial)
            return

        lock = self._user_lock(cid)
        if lock.locked():
            await self.send_message(chat_id, "⚠️ Sedang ada proses absen yang berjalan. Mohon tunggu.")
            return

        parts = text.strip().split()
        delay: int | None = None
        custom_urls: list[str] = []

        for arg in parts[1:]:
            arg_lower = arg.lower()
            if arg_lower == "now":
                delay = 0
            elif arg.isdigit() or arg.startswith("http"):
                custom_urls.append(arg)
            elif arg_lower.startswith("delay="):
                try:
                    delay = int(arg_lower.split("=")[1])
                except ValueError:
                    pass

        try:
            cfg = self._build_config_for_chat(
                cid, cli_urls=custom_urls, submit_delay_seconds=delay
            )
        except Exception as exc:
            await self.send_message(chat_id, f"⚠️ Gagal memuat konfigurasi: {exc}")
            return

        async with lock, self._browser_sem:
            delay_info = (
                f" (spare jeda: {cfg.submit_delay_seconds}s + acak)"
                if cfg.submit_delay_seconds > 0
                else " (tanpa jeda)"
            )
            await self.send_message(
                chat_id,
                f"⏳ *Memulai proses presensi SPADA...*\n"
                f"• Target: {len(cfg.attendance_targets)} mata kuliah\n"
                f"• Mode: Headless{delay_info}\n"
                f"_Anda akan menerima notifikasi otomatis saat absensi berhasil diisi._",
            )

            bot = SpadaBot(cfg)
            results: list[AttendanceResult] = []
            notifier = TelegramNotifier(self.token, cid)

            try:
                await bot.start()
                await bot.login()

                for idx, target in enumerate(cfg.attendance_targets):
                    if idx > 0:
                        await self._sleep_target_gap()
                    result = await bot.submit_attendance(target)
                    results.append(result)

                    if result.status == "success":
                        await notifier.send(
                            TelegramNotifier.format_success(
                                result.course,
                                result.timestamp,
                                f"Berhasil diisi otomatis via Telegram{delay_info}",
                            )
                        )
                    elif result.status == "failed":
                        await notifier.send(
                            TelegramNotifier.format_failure(
                                result.course, result.timestamp, result.reason
                            )
                        )
            except Exception as exc:
                err_msg = f"! Error pada otomasi SPADA: {exc}"
                log(err_msg)
                await self.send_message(chat_id, f"⚠️ *Proses Terhenti Karena Kesalahan:*\n`{exc}`")
            finally:
                await bot.close()

            if results:
                await self.send_message(chat_id, build_summary(results))

    async def process_update(self, update: dict[str, Any]) -> None:
        """Proses satu update pesan dari Telegram."""
        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        message_id = message.get("message_id")
        sender = message.get("from", {})
        username = sender.get("username", "") or sender.get("first_name", "User")
        text = (message.get("text") or "").strip()

        is_password_step = self._pending_reg.get(chat_id, {}).get("step") == "password"
        logged_text = "[REDACTED]" if is_password_step else text

        photo_file_id: str | None = None
        if message.get("photo"):
            photo_file_id = message["photo"][-1].get("file_id")
        elif (doc := message.get("document")) and (doc.get("mime_type") or "").startswith("image/"):
            photo_file_id = doc.get("file_id")

        if photo_file_id:
            log(f"Bukti gambar diterima dari @{username} (ID: {chat_id})")
            await self._handle_payment_proof(chat_id, username, photo_file_id)
            return

        if not text:
            return

        log(f"Pesan diterima dari @{username} (ID: {chat_id}): '{logged_text}'")

        cmd = text.split()[0].split("@", 1)[0].lower()
        if cmd == "/batal":
            await self.handle_batal(chat_id)
            return

        if chat_id in self._pending_reg and not text.startswith("/"):
            await self._handle_reg_step(chat_id, username, text, message_id)
            return

        if cmd in {"/start", "/help"}:
            await self.handle_start(chat_id)
        elif cmd in {"/daftar", "/register"}:
            await self.handle_daftar(chat_id)
        elif cmd in {"/bayar", "/paket"}:
            await self.handle_bayar(chat_id, text)
        elif cmd in {"/akun", "/profil"}:
            await self.handle_akun(chat_id)
        elif cmd == "/approve":
            await self.handle_approve(chat_id, text)
        elif cmd == "/reject":
            await self.handle_reject(chat_id, text)
        elif cmd == "/users":
            await self.handle_users(chat_id)
        elif cmd == "/sync":
            await self.handle_sync(chat_id)
        elif cmd.startswith("/auto"):
            await self.handle_auto(chat_id, text)
        elif cmd in {"/list", "/target"}:
            await self.handle_list(chat_id)
        elif cmd in {"/cek", "/status"}:
            await self.handle_cek(chat_id)
        elif cmd.startswith("/absen"):
            await self.handle_absen(chat_id, text)
        else:
            await self.send_message(
                chat_id,
                "❓ Perintah tidak dikenali. Gunakan `/daftar`, `/bayar`, `/absen`, `/cek`, atau `/help`.",
            )

    async def run(self) -> None:
        """Loop long-polling getUpdates dan auto-watcher."""
        log(f"Bot Telegram aktif! Pemilik (exempt): {self.authorized_chat_id or '-'}")
        asyncio.create_task(self.auto_watcher_loop())
        offset = 0
        timeout = 25

        async with httpx.AsyncClient(timeout=timeout + 10.0) as client:
            while True:
                url = f"{self.api_base}/bot{self.token}/getUpdates?offset={offset}&timeout={timeout}"
                try:
                    res = await client.get(url)
                    if res.status_code == 200:
                        data = res.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            asyncio.create_task(self.process_update(update))
                    else:
                        log(f"! getUpdates HTTP {res.status_code}: {res.text[:100]}")
                        await asyncio.sleep(3)
                except httpx.TimeoutException:
                    continue
                except Exception as exc:
                    log(f"! Polling error: {exc}")
                    await asyncio.sleep(5)


if __name__ == "__main__":
    daemon = TelegramBotDaemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        daemon.store.close()
        log("Bot Telegram dimatikan.")
