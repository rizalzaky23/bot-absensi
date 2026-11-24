"""
Telegram Bot Daemon untuk memicu presensi SPADA UPN via chat Telegram.

Fitur:
- /absen       : Jalankan presensi untuk semua target di .env (dengan jeda waktu)
- /absen now   : Jalankan presensi langsung tanpa jeda waktu
- /absen <id>  : Jalankan presensi untuk ID modul tertentu
- /status /cek : Cek apakah ada sesi presensi yang sedang terbuka tanpa mengisi
- /list        : Tampilkan daftar ID mata kuliah yang terdaftar
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

import main
from main import (
    AttendanceResult,
    Config,
    SpadaBot,
    TelegramNotifier,
    build_summary,
    clean_text,
    guess_course_name,
    load_config,
    log,
    now_local,
    resolve_target,
    session_reason,
)

RUN_LOCK = asyncio.Lock()


class TelegramBotDaemon:
    """Long polling Telegram Bot listener."""

    def __init__(self) -> None:
        load_dotenv()
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.authorized_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.api_base = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        self.auto_enabled = os.getenv("AUTO_ABSEN", "true").lower() in {"1", "true", "yes"}
        self.check_interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
        if not self.token:
            raise SystemExit("TELEGRAM_BOT_TOKEN belum diset di .env")

    async def send_message(self, chat_id: str | int, text: str) -> bool:
        """Kirim pesan Markdown ke Telegram."""
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
                if res.status_code != 200:
                    log(f"! Gagal kirim pesan ke Telegram ({res.status_code}): {res.text[:120]}")
                    return False
                return True
        except Exception as exc:
            log(f"! HTTP error kirim Telegram: {exc}")
            return False

    async def handle_start(self, chat_id: str | int) -> None:
        """Balas perintah /start atau /help."""
        auto_status = "🟢 AKTIF" if self.auto_enabled else "🔴 NONAKTIF"
        msg = (
            "🤖 *Selamat Datang di Bot Presensi SPADA UPN!*\n\n"
            f"Status Auto-Absen: {auto_status} (cek tiap {self.check_interval_seconds // 60} menit)\n\n"
            "Perintah yang tersedia:\n"
            "• `/absen` — Cek & isi absen semua mata kuliah sekarang\n"
            "• `/absen now` — Cek & isi absen langsung tanpa jeda waktu\n"
            "• `/absen <id>` — Absen khusus satu ID mata kuliah\n"
            "• `/auto on` / `/auto off` — Aktifkan / matikan mode absen otomatis\n"
            "• `/cek` atau `/status` — Cek sesi terbuka hari ini tanpa mengisi\n"
            "• `/list` — Daftar ID mata kuliah yang terdaftar di konfigurasi\n"
        )
        await self.send_message(chat_id, msg)

    async def handle_auto(self, chat_id: str | int, text: str) -> None:
        """Ubah atau cek status auto-absen berkala."""
        parts = text.strip().split()
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub in {"on", "enable", "aktif"}:
                self.auto_enabled = True
                await self.send_message(
                    chat_id,
                    f"🟢 *Auto-Absen DIAKTIFKAN*\n"
                    f"Bot akan memeriksa SPADA otomatis setiap {self.check_interval_seconds // 60} menit "
                    f"pada jam kuliah (06:45 - 18:30 WIB, Senin-Jumat). "
                    f"Jika sesi dibuka dosen, bot otomatis absen dan melapor ke sini."
                )
                return
            elif sub in {"off", "disable", "matikan"}:
                self.auto_enabled = False
                await self.send_message(chat_id, "🔴 *Auto-Absen DIMATIKAN*\nBot hanya akan absen jika Anda kirim perintah `/absen`.")
                return

        status = "🟢 AKTIF" if self.auto_enabled else "🔴 NONAKTIF"
        await self.send_message(
            chat_id,
            f"ℹ️ *Status Auto-Absen:* {status}\n"
            f"• Interval periksa: tiap {self.check_interval_seconds // 60} menit\n"
            f"• Jam aktif: 06:45 - 18:30 WIB (Senin-Jumat)\n\n"
            f"Gunakan `/auto on` atau `/auto off` untuk mengubah."
        )

    async def auto_watcher_loop(self) -> None:
        """Loop latar belakang untuk memeriksa dan mengisi absensi otomatis saat sesi dibuka."""
        log(f"Auto-watcher berjalan (interval: {self.check_interval_seconds}s, auto={self.auto_enabled})")
        while True:
            await asyncio.sleep(self.check_interval_seconds)
            if not self.auto_enabled:
                continue

            now = now_local()
            # Senin (0) s/d Jumat (4), antara 06:45 dan 18:30 WIB
            minute_of_day = now.hour * 60 + now.minute
            if now.weekday() > 4 or not (6 * 60 + 45 <= minute_of_day <= 18 * 60 + 30):
                continue

            if RUN_LOCK.locked():
                continue

            async with RUN_LOCK:
                bot: SpadaBot | None = None
                try:
                    cfg = load_config()
                    bot = SpadaBot(cfg)
                    await bot.start()
                    await bot.login()

                    for target in cfg.attendance_targets:
                        url = resolve_target(target)
                        assert bot.page is not None
                        await bot.page.goto(url, wait_until="domcontentloaded")
                        await bot._settle()

                        link, row_text = await bot._find_active_session(bot.page)
                        if link is None:
                            continue

                        log(f"[Auto-Absen] Sesi aktif ditemukan untuk {target}! Menjalankan absensi...")
                        result = await bot.submit_attendance(target)

                        target_chat = self.authorized_chat_id or ""
                        if result.status == "success":
                            log(f"[Auto-Absen] Berhasil absen: {result.course}")
                            if target_chat:
                                msg = TelegramNotifier.format_success(
                                    result.course,
                                    result.timestamp,
                                    "Presensi tercatat otomatis saat sesi kelas dibuka (auto-watcher)"
                                )
                                await self.send_message(target_chat, msg)
                        elif result.status == "failed":
                            log(f"[Auto-Absen] Gagal submit: {result.course} ({result.reason})")
                            if target_chat:
                                await self.send_message(
                                    target_chat,
                                    TelegramNotifier.format_failure(result.course, result.timestamp, result.reason)
                                )
                except Exception as exc:
                    log(f"! Error pada auto-watcher cycle: {exc}")
                finally:
                    if bot is not None:
                        await bot.close()

    async def handle_list(self, chat_id: str | int) -> None:
        """Tampilkan daftar target attendance."""
        try:
            cfg = load_config()
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
        if RUN_LOCK.locked():
            await self.send_message(chat_id, "⚠️ Sedang ada proses absen yang berjalan. Mohon tunggu.")
            return

        async with RUN_LOCK:
            await self.send_message(chat_id, "🔍 *Mengecek sesi presensi SPADA hari ini...*")
            cfg = load_config(submit_delay_seconds=0)
            bot = SpadaBot(cfg)
            open_sessions: list[str] = []
            already_done: list[str] = []

            try:
                await bot.start()
                await bot.login()
                for target in cfg.attendance_targets:
                    url = resolve_target(target)
                    assert bot.page is not None
                    await bot.page.goto(url, wait_until="domcontentloaded")
                    await bot._settle()
                    cname = await guess_course_name(bot.page)

                    link, row_text = await bot._find_active_session(bot.page)
                    if link is not None:
                        open_sessions.append(f"🟢 *{cname}* (`{target}`): Sesi Terbuka!\n  _{row_text[:80]}_")
                    elif await bot._already_present(bot.page):
                        already_done.append(f"☑️ *{cname}* (`{target}`): Sudah Hadir")
            except Exception as exc:
                await self.send_message(chat_id, f"⚠️ Terjadi kesalahan saat cek sesi: {exc}")
                return
            finally:
                await bot.close()

            report = ["📊 *Hasil Pengecekan Sesi SPADA:*"]
            if open_sessions:
                report.append("\n*Sesi Siap Diisi:*")
                report.extend(open_sessions)
            else:
                report.append("\n_Tidak ada sesi yang sedang terbuka saat ini._")

            if already_done:
                report.append("\n*Sudah Terisi Hari Ini:*")
                report.extend(already_done)

            await self.send_message(chat_id, "\n".join(report))

    async def handle_absen(self, chat_id: str | int, text: str) -> None:
        """Proses perintah /absen."""
        if RUN_LOCK.locked():
            await self.send_message(chat_id, "⚠️ Sedang ada proses absen yang berjalan. Mohon tunggu.")
            return

        parts = text.strip().split()
        delay: int | None = None
        custom_urls: list[str] = []

        # Parse opsi: /absen, /absen now, /absen <id>, /absen <id> now
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
            cfg = load_config(cli_urls=custom_urls, submit_delay_seconds=delay)
        except Exception as exc:
            await self.send_message(chat_id, f"⚠️ Gagal memuat konfigurasi: {exc}")
            return

        async with RUN_LOCK:
            delay_info = f" (spare jeda: {cfg.submit_delay_seconds}s)" if cfg.submit_delay_seconds > 0 else " (tanpa jeda)"
            await self.send_message(
                chat_id,
                f"⏳ *Memulai proses presensi SPADA...*\n"
                f"• Target: {len(cfg.attendance_targets)} mata kuliah\n"
                f"• Mode: Headless{delay_info}\n"
                f"_Anda akan menerima notifikasi otomatis saat absensi berhasil diisi._"
            )

            bot = SpadaBot(cfg)
            results: list[AttendanceResult] = []
            notifier = TelegramNotifier(self.token, str(chat_id))

            try:
                await bot.start()
                await bot.login()

                for target in cfg.attendance_targets:
                    result = await bot.submit_attendance(target)
                    results.append(result)

                    # Kirim notifikasi seketika jika berhasil submit kehadiran baru
                    if result.status == "success":
                        await notifier.send(
                            TelegramNotifier.format_success(
                                result.course,
                                result.timestamp,
                                f"Berhasil diisi otomatis via Telegram{delay_info}"
                            )
                        )
                    elif result.status == "failed":
                        await notifier.send(
                            TelegramNotifier.format_failure(result.course, result.timestamp, result.reason)
                        )

            except Exception as exc:
                err_msg = f"! Error pada otomasi SPADA: {exc}"
                log(err_msg)
                await self.send_message(chat_id, f"⚠️ *Proses Terhenti Karena Kesalahan:*\n`{exc}`")
            finally:
                await bot.close()

            # Kirim ringkasan akhir
            if results:
                await self.send_message(chat_id, build_summary(results))

    async def process_update(self, update: dict[str, Any]) -> None:
        """Proses satu update pesan dari Telegram."""
        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        sender = message.get("from", {})
        username = sender.get("username", "") or sender.get("first_name", "User")
        text = (message.get("text") or "").strip()

        if not text:
            return

        log(f"Pesan diterima dari @{username} (ID: {chat_id}): '{text}'")

        # Cek otorisasi jika TELEGRAM_CHAT_ID ditentukan
        if self.authorized_chat_id and chat_id != self.authorized_chat_id:
            log(f"! Chat ID tidak cocok: {chat_id} != {self.authorized_chat_id}")
            await self.send_message(
                chat_id,
                f"⛔ *Akses Ditolak*\nBot ini hanya untuk pemilik terdaftar (Chat ID: `{chat_id}`)."
            )
            return

        cmd = text.split()[0].lower()
        if cmd in {"/start", "/help"}:
            await self.handle_start(chat_id)
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
                "❓ Perintah tidak dikenali. Gunakan `/absen`, `/auto`, `/cek`, atau `/help`."
            )

    async def run(self) -> None:
        """Loop long-polling getUpdates dan auto-watcher."""
        log(f"Bot Telegram aktif! Mendengarkan pesan (Authorized ID: {self.authorized_chat_id or 'semua'})...")
        # Jalankan loop watcher berkala di latar belakang
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
        log("Bot Telegram dimatikan.")
