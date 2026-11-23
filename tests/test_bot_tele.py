import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

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

if __name__ == "__main__":
    unittest.main()
