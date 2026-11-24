import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot_tele

class TestTelegramBotDaemon(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "123456:FAKE_TOKEN"
        os.environ["TELEGRAM_CHAT_ID"] = "123456789"
        os.environ["SPADA_USERNAME"] = "12345678"
        os.environ["SPADA_PASSWORD"] = "secret"
        os.environ["SPADA_ATTENDANCE_URLS"] = "813469,804475"
        self.daemon = bot_tele.TelegramBotDaemon()

    @patch.object(bot_tele.TelegramBotDaemon, "send_message", new_callable=AsyncMock)
    async def test_handle_start(self, mock_send):
        await self.daemon.handle_start("123456789")
        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][1]
        self.assertIn("/absen", text)

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

if __name__ == "__main__":
    unittest.main()
