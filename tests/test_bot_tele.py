import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bot_tele
from main import AttendanceResult, now_local


class TestTelegramBotDaemon(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["TELEGRAM_BOT_TOKEN"] = "123456:FAKE_TOKEN"
        os.environ["TELEGRAM_CHAT_ID"] = "123456789"
        os.environ["SPADA_USERNAME"] = "12345678"
        os.environ["SPADA_PASSWORD"] = "secret"
        os.environ["SPADA_ATTENDANCE_URLS"] = "813469,804475"
        os.environ["DB_PATH"] = str(Path(self.tmpdir.name) / "test.db")
        os.environ["CREDENTIAL_KEY"] = ""
        os.environ["USER_GAP_MIN_SECONDS"] = "0"
        os.environ["USER_GAP_MAX_SECONDS"] = "0"
        os.environ["TARGET_GAP_MIN_SECONDS"] = "0"
        os.environ["TARGET_GAP_MAX_SECONDS"] = "0"
        os.environ["SUBMIT_DELAY_SECONDS"] = "0"
        os.environ["SUBMIT_JITTER_SECONDS"] = "0"
        self.daemon = bot_tele.TelegramBotDaemon()

    async def asyncTearDown(self):
        self.daemon.store.close()
        self.tmpdir.cleanup()

    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_handle_start(self, mock_send):
        await self.daemon.handle_start("123456789")
        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][1]
        self.assertIn("/absen", text)
        self.assertIn("Mode Pemilik", text)

    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_authorization_rejection(self, mock_send):
        update = {
            "message": {
                "chat": {"id": "999999999"},
                "from": {"username": "stranger"},
                "text": "/absen",
            }
        }
        await self.daemon.process_update(update)
        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][1]
        self.assertIn("Akses Ditolak", text)
        self.assertIn("/daftar", text)

    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_handle_list(self, mock_send):
        await self.daemon.handle_list("123456789")
        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][1]
        self.assertIn("813469", text)

    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_handle_auto_toggle(self, mock_send):
        await self.daemon.handle_auto("123456789", "/auto off")
        self.assertFalse(self.daemon.auto_enabled)
        text = mock_send.call_args[0][1]
        self.assertIn("DIMATIKAN", text)

        await self.daemon.handle_auto("123456789", "/auto on")
        self.assertTrue(self.daemon.auto_enabled)
        text = mock_send.call_args[0][1]
        self.assertIn("DIAKTIFKAN", text)

    @patch.object(bot_tele, "guess_course_name", new_callable=AsyncMock)
    @patch.object(bot_tele, "SpadaBot")
    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_handle_cek_shows_only_today_courses(
        self, mock_send, mock_bot_cls, mock_guess
    ):
        mock_bot = MagicMock()
        mock_bot.page = MagicMock()
        mock_bot.page.goto = AsyncMock()
        mock_bot.start = AsyncMock()
        mock_bot.login = AsyncMock()
        mock_bot.close = AsyncMock()
        mock_bot._settle = AsyncMock()
        mock_guess.side_effect = [
            "Matkul Terbuka",
            "Matkul Belum Buka",
            "Matkul Sudah Hadir",
            "Matkul Besok",
        ]
        mock_bot.get_today_sessions = AsyncMock(
            side_effect=[
                [("open", "Tue 24 Nov 2026 12:30PM - 2:15PM")],
                [("not_open", "Tue 24 Nov 2026 3PM - 6PM")],
                [("already", "Tue 24 Nov 2026 08:00AM - 09:40AM")],
                [],
            ]
        )
        mock_bot_cls.return_value = mock_bot

        os.environ["SPADA_ATTENDANCE_URLS"] = "101,102,103,104"
        await self.daemon.handle_cek("123456789")

        report = mock_send.call_args_list[-1][0][1]
        self.assertIn("*Sesi Siap Diisi:*", report)
        self.assertIn("Matkul Terbuka", report)
        self.assertIn("*Belum Dibuka / Ditutup:*", report)
        self.assertIn("Matkul Belum Buka", report)
        self.assertIn("*Sudah Terisi Hari Ini:*", report)
        self.assertIn("Matkul Sudah Hadir", report)
        self.assertNotIn("Matkul Besok", report)

    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_owner_exempt_from_registration(self, mock_send):
        await self.daemon.process_update(
            {
                "message": {
                    "chat": {"id": "123456789"},
                    "from": {"username": "owner"},
                    "text": "/daftar",
                }
            }
        )
        self.assertNotIn("123456789", self.daemon._pending_reg)
        self.assertIn("tidak perlu mendaftar", mock_send.call_args[0][1])

    @patch.object(bot_tele.TelegramBotDaemon, "delete_message", new_callable=AsyncMock)
    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_registration_deletes_password_and_encrypts_db(self, mock_send, mock_del):
        await self.daemon.process_update(
            {"message": {"chat": {"id": "777"}, "from": {"username": "budi"}, "text": "/daftar"}}
        )
        await self.daemon.process_update(
            {"message": {"chat": {"id": "777"}, "from": {"username": "budi"}, "text": "123210001"}}
        )
        await self.daemon.process_update(
            {
                "message": {
                    "message_id": 42,
                    "chat": {"id": "777"},
                    "from": {"username": "budi"},
                    "text": "rahasia123",
                }
            }
        )
        mock_del.assert_awaited_once_with("777", 42)
        user = self.daemon.store.get_user("777")
        self.assertIsNotNone(user)
        self.assertEqual(user.spada_username, "123210001")
        self.assertEqual(user.spada_password, "rahasia123")
        self.assertEqual(user.status, "pending_payment")

        raw_conn = sqlite3.connect(os.environ["DB_PATH"])
        raw_enc = raw_conn.execute(
            "SELECT spada_password_enc FROM users WHERE chat_id = '777'"
        ).fetchone()[0]
        raw_conn.close()
        self.assertNotIn("rahasia123", raw_enc)
        self.assertIn("/bayar mingguan", mock_send.call_args[0][1])

    @patch.object(bot_tele.TelegramBotDaemon, "send_photo", new_callable=AsyncMock)
    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_bayar_and_proof_forwards_to_owner(self, mock_send, mock_photo):
        self.daemon.store.upsert_credentials("777", "budi", "123210001", "pw", now_local())
        await self.daemon.process_update(
            {"message": {"chat": {"id": "777"}, "from": {"username": "budi"}, "text": "/bayar mingguan"}}
        )
        self.assertIn("Tagihan #", mock_send.call_args[0][1])
        mock_photo.assert_awaited_once()
        self.assertEqual(mock_photo.call_args[0][0], "777")
        self.assertEqual(mock_photo.call_args[0][1].name, "qris.jpg")
        self.assertIn("RZF STORE, KLTN SLT", mock_photo.call_args[0][2])

        await self.daemon.process_update(
            {
                "message": {
                    "chat": {"id": "777"},
                    "from": {"username": "budi"},
                    "photo": [{"file_id": "PHOTO_SMALL"}, {"file_id": "PHOTO_123"}],
                }
            }
        )
        self.assertEqual(mock_photo.await_count, 2)
        self.assertEqual(mock_photo.call_args[0][0], "123456789")
        self.assertEqual(mock_photo.call_args[0][1], "PHOTO_123")
        self.assertIn("/approve 777", mock_photo.call_args[0][2])

    @patch.object(bot_tele, "TelegramNotifier")
    @patch.object(bot_tele, "SpadaBot")
    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_approve_activates_packages_and_onboards(
        self, mock_send, mock_bot_cls, mock_notifier_cls
    ):
        now = now_local()
        self.daemon.store.upsert_credentials("777", "budi", "123210001", "pw", now)
        self.daemon.store.create_payment("777", "mingguan", now)

        mock_bot = MagicMock()
        mock_bot.start = AsyncMock()
        mock_bot.login = AsyncMock()
        mock_bot.close = AsyncMock()
        mock_bot.discover_attendance_targets = AsyncMock(return_value=[("555", "Basis Data")])
        mock_bot.submit_attendance = AsyncMock(
            return_value=AttendanceResult("Basis Data", "success", "Dikonfirmasi")
        )
        mock_bot_cls.return_value = mock_bot

        mock_notifier = MagicMock()
        mock_notifier.send = AsyncMock(return_value=True)
        mock_notifier_cls.return_value = mock_notifier
        mock_notifier_cls.format_success = MagicMock(return_value="OK")

        await self.daemon.process_update(
            {"message": {"chat": {"id": "123456789"}, "from": {"username": "owner"}, "text": "/approve 777"}}
        )
        await asyncio.sleep(0.05)

        user = self.daemon.store.get_user("777")
        self.assertIsNotNone(user)
        self.assertEqual(user.status, "active")
        self.assertEqual(user.attendance_targets, ["555"])
        days_left = (user.expired_at - now).total_seconds() / 86400
        self.assertTrue(6.9 <= days_left <= 7.1)
        mock_bot.discover_attendance_targets.assert_awaited_once()
        mock_bot.submit_attendance.assert_awaited_once_with("555")

        # Perpanjangan lebih awal: bulanan (30d) + semester (180d) memperpanjang dari expired_at lama
        prev_exp = user.expired_at
        u_month, _, _ = self.daemon.store.approve_payment("777", now, package_override="bulanan")
        self.assertEqual(u_month.expired_at, prev_exp + timedelta(days=30))
        u_sem, _, _ = self.daemon.store.approve_payment("777", now, package_override="semester")
        self.assertEqual(u_sem.expired_at, prev_exp + timedelta(days=210))

    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_reject_payment(self, mock_send):
        now = now_local()
        self.daemon.store.upsert_credentials("777", "budi", "123210001", "pw", now)
        self.daemon.store.create_payment("777", "bulanan", now)
        await self.daemon.process_update(
            {
                "message": {
                    "chat": {"id": "123456789"},
                    "from": {"username": "owner"},
                    "text": "/reject 777 bukti buram",
                }
            }
        )
        self.assertIsNone(self.daemon.store.get_pending_payment("777"))
        self.assertIn("Ditolak", mock_send.call_args_list[-1][0][1])

    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_expired_user_denied_and_excluded(self, mock_send):
        now = now_local()
        self.daemon.store.upsert_credentials("777", "budi", "123210001", "pw", now)
        self.daemon.store.approve_payment("777", now - timedelta(days=10), package_override="mingguan")
        self.assertEqual(self.daemon.store.list_active_users(now), [])

        await self.daemon.process_update(
            {"message": {"chat": {"id": "777"}, "from": {"username": "budi"}, "text": "/absen"}}
        )
        self.assertIn("Langganan Tidak Aktif", mock_send.call_args[0][1])

    @patch.object(bot_tele.TelegramBotDaemon, "delete_message", new_callable=AsyncMock)
    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_unregistered_user_start_allows_registration(self, mock_send, mock_del):
        await self.daemon.process_update(
            {"message": {"chat": {"id": "888"}, "from": {"username": "teman"}, "text": "/start"}}
        )
        first_reply = mock_send.call_args[0][1]
        self.assertNotIn("Akses Ditolak", first_reply)
        self.assertIn("/daftar", first_reply)

        self.daemon._pending_reg["888"] = {"step": "nim"}
        await self.daemon.process_update(
            {
                "message": {
                    "chat": {"id": "888"},
                    "from": {"username": "teman"},
                    "text": "/start@SpadaBot",
                }
            }
        )
        second_reply = mock_send.call_args[0][1]
        self.assertNotIn("Perintah tidak dikenali", second_reply)
        self.assertIn("/daftar", second_reply)
        self.assertNotIn("888", self.daemon._pending_reg)

        await self.daemon.process_update(
            {"message": {"chat": {"id": "888"}, "from": {"username": "teman"}, "text": "/daftar"}}
        )
        await self.daemon.process_update(
            {"message": {"chat": {"id": "888"}, "from": {"username": "teman"}, "text": "123210099"}}
        )
        await self.daemon.process_update(
            {
                "message": {
                    "message_id": 99,
                    "chat": {"id": "888"},
                    "from": {"username": "teman"},
                    "text": "pass123",
                }
            }
        )
        mock_del.assert_awaited_once_with("888", 99)
        user = self.daemon.store.get_user("888")
        self.assertIsNotNone(user)
        self.assertEqual(user.status, "pending_payment")

    async def test_send_message_falls_back_on_markdown_400(self):
        res_400 = MagicMock(status_code=400, text="Bad Request: can't parse entities")
        res_200 = MagicMock(status_code=200, text="ok")
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=[res_400, res_200])
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch.object(bot_tele.httpx, "AsyncClient", return_value=mock_cm):
            ok = await self.daemon.send_message("888", "Halo @budi_123")

        self.assertTrue(ok)
        self.assertEqual(mock_client.post.await_count, 2)
        first_payload = mock_client.post.await_args_list[0].kwargs["json"]
        second_payload = mock_client.post.await_args_list[1].kwargs["json"]
        self.assertEqual(first_payload.get("parse_mode"), "Markdown")
        self.assertNotIn("parse_mode", second_payload)


if __name__ == "__main__":
    unittest.main()
