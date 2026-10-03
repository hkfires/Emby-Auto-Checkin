import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tgservice.client_manager import ClientManager


class ClientManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_config = {
            "api_id": 12345,
            "api_hash": "mock_hash",
            "users": [
                {"status": "logged_in", "session_name": "session_1", "nickname": "User1"}
            ]
        }
        self.enterContext(patch("tgservice.client_manager.load_config", return_value=self.mock_config))
        self.manager = ClientManager()

    async def test_safe_disconnect_awaits_future_like_telethon_shield(self):
        """Verify that _safe_disconnect awaits Futures returned by Telethon's disconnect (e.g. asyncio.shield)."""
        client = MagicMock()
        future_completed = False

        async def coro_action():
            nonlocal future_completed
            await asyncio.sleep(0.01)
            future_completed = True

        task = asyncio.create_task(coro_action())
        shielded = asyncio.shield(task)
        client.disconnect.return_value = shielded

        await self.manager._safe_disconnect(client)
        self.assertTrue(future_completed)

    async def test_add_or_update_client_does_not_crash_when_existing_client_is_none(self):
        """Verify that AttributeError is not raised when existing client is None (the original bug)."""
        self.manager._clients["session_1"] = {
            "client": None,
            "nickname": "User1",
            "status": "connect_failed"
        }

        mock_new_client = MagicMock()
        mock_new_client.connect = AsyncMock()
        mock_new_client.is_user_authorized = AsyncMock(return_value=True)
        mock_new_client.get_me = AsyncMock(return_value=MagicMock())

        with patch.object(self.manager, "_create_telegram_client", return_value=mock_new_client):
            await self.manager.add_or_update_client("session_1", 12345, "mock_hash", "User1")

        self.assertEqual(self.manager._clients["session_1"]["status"], "connected")
        self.assertIs(self.manager._clients["session_1"]["client"], mock_new_client)
        mock_new_client.connect.assert_awaited_once()
        mock_new_client.get_me.assert_awaited_once()

    async def test_add_or_update_client_skips_if_already_connected(self):
        """If client exists and is_connected() is True (and not force_reconnect), it should not reconnect."""
        existing_client = MagicMock()
        existing_client.is_connected.return_value = True

        self.manager._clients["session_1"] = {
            "client": existing_client,
            "nickname": "User1",
            "status": "connected"
        }

        with patch.object(self.manager, "_create_telegram_client") as mock_create:
            await self.manager.add_or_update_client("session_1", 12345, "mock_hash", "User1")
            mock_create.assert_not_called()

        existing_client.connect.assert_not_called()

    async def test_add_or_update_client_reuses_existing_client_when_reconnect_succeeds(self):
        """Disconnected client should first be reconnected on the same instance."""
        existing_client = MagicMock()
        existing_client.is_connected.return_value = False
        existing_client.connect = AsyncMock()
        existing_client.is_user_authorized = AsyncMock(return_value=True)
        existing_client.get_me = AsyncMock(return_value=MagicMock())

        self.manager._clients["session_1"] = {
            "client": existing_client,
            "nickname": "User1",
            "status": "connect_failed"
        }

        with patch.object(self.manager, "_create_telegram_client") as mock_create:
            await self.manager.add_or_update_client("session_1", 12345, "mock_hash", "User1")
            mock_create.assert_not_called()

        existing_client.connect.assert_awaited_once()
        existing_client.get_me.assert_awaited_once()
        self.assertEqual(self.manager._clients["session_1"]["status"], "connected")
        self.assertIs(self.manager._clients["session_1"]["client"], existing_client)

    async def test_add_or_update_client_disconnects_existing_before_recreating_on_failure(self):
        """If reconnecting existing client fails, it must be safely disconnected before creating new instance."""
        existing_client = MagicMock()
        existing_client.is_connected.return_value = False
        existing_client.connect = AsyncMock(side_effect=ConnectionError("reconnect failed"))
        existing_client.disconnect = AsyncMock()

        self.manager._clients["session_1"] = {
            "client": existing_client,
            "nickname": "User1",
            "status": "reconnecting"
        }

        mock_new_client = MagicMock()
        mock_new_client.connect = AsyncMock()
        mock_new_client.is_user_authorized = AsyncMock(return_value=True)
        mock_new_client.get_me = AsyncMock(return_value=MagicMock())

        with patch.object(self.manager, "_create_telegram_client", return_value=mock_new_client):
            await self.manager.add_or_update_client("session_1", 12345, "mock_hash", "User1")

        existing_client.disconnect.assert_awaited_once()
        mock_new_client.connect.assert_awaited_once()
        mock_new_client.get_me.assert_awaited_once()
        self.assertEqual(self.manager._clients["session_1"]["status"], "connected")
        self.assertIs(self.manager._clients["session_1"]["client"], mock_new_client)

    async def test_add_or_update_client_cleans_up_new_client_on_connect_failure(self):
        """If newly created client fails to connect, its resources must be disconnected to avoid leaks."""
        mock_new_client = MagicMock()
        mock_new_client.connect = AsyncMock(side_effect=TimeoutError("connect timeout"))
        mock_new_client.disconnect = AsyncMock()

        with patch.object(self.manager, "_create_telegram_client", return_value=mock_new_client):
            await self.manager.add_or_update_client("session_1", 12345, "mock_hash", "User1")

        mock_new_client.disconnect.assert_awaited_once()
        self.assertEqual(self.manager._clients["session_1"]["status"], "connect_failed")
        self.assertIsNone(self.manager._clients["session_1"]["client"])

    async def test_health_check_forces_reconnect_disconnects_stalled_client(self):
        """When health probe get_me fails, force_reconnect must disconnect the stalled client first and rebuild."""
        broken_client = MagicMock()
        broken_client.is_connected.return_value = True  # Transport still claims connected
        broken_client.get_me = AsyncMock(side_effect=ConnectionError("connection dropped"))
        broken_client.disconnect = AsyncMock()

        self.manager._clients["session_1"] = {
            "client": broken_client,
            "nickname": "User1",
            "status": "connected"
        }

        fresh_client = MagicMock()
        fresh_client.connect = AsyncMock()
        fresh_client.is_user_authorized = AsyncMock(return_value=True)
        fresh_client.get_me = AsyncMock(return_value=MagicMock())

        with patch.object(self.manager, "_create_telegram_client", return_value=fresh_client):
            await self.manager.health_check_all_clients()

        broken_client.get_me.assert_awaited_once()
        broken_client.disconnect.assert_awaited_once()
        fresh_client.connect.assert_awaited_once()
        fresh_client.get_me.assert_awaited_once()
        self.assertEqual(self.manager._clients["session_1"]["status"], "connected")
        self.assertIs(self.manager._clients["session_1"]["client"], fresh_client)

    async def test_concurrent_reconnects_are_serialized(self):
        """Multiple concurrent reconnect requests for the same session must not race or create duplicates."""
        self.manager._clients["session_1"] = {
            "client": None,
            "nickname": "User1",
            "status": "connect_failed"
        }

        created_clients = []

        def create_client(*args, **kwargs):
            client = MagicMock()
            client.is_connected.return_value = False
            async def slow_connect():
                await asyncio.sleep(0.05)
                client.is_connected.return_value = True
            client.connect = slow_connect
            client.is_user_authorized = AsyncMock(return_value=True)
            client.get_me = AsyncMock(return_value=MagicMock())
            created_clients.append(client)
            return client

        with patch.object(self.manager, "_create_telegram_client", side_effect=create_client):
            t1 = asyncio.create_task(self.manager.get_or_reconnect_client("session_1"))
            t2 = asyncio.create_task(self.manager.get_or_reconnect_client("session_1"))
            res1, res2 = await asyncio.gather(t1, t2)

        self.assertEqual(len(created_clients), 1)
        self.assertIs(res1, res2)
        self.assertIs(res1, created_clients[0])

    async def test_concurrent_removal_and_reconnect_serialized(self):
        """Removal acquiring session lock prevents reconnect from re-installing removed session."""
        existing_client = MagicMock()
        existing_client.is_connected.return_value = False
        existing_client.disconnect = AsyncMock()

        self.manager._clients["session_1"] = {
            "client": existing_client,
            "nickname": "User1",
            "status": "connected"
        }

        async def slow_remove():
            await self.manager.remove_client("session_1")

        # After removal, session_1 is gone
        await slow_remove()
        self.assertNotIn("session_1", self.manager._clients)
        res = await self.manager.get_or_reconnect_client("session_1")
        self.assertIsNone(res)

    async def test_get_or_reconnect_client(self):
        """get_or_reconnect_client should return connected client or attempt reconnect."""
        connected_client = MagicMock()
        connected_client.is_connected.return_value = True
        self.manager._clients["session_1"] = {
            "client": connected_client,
            "nickname": "User1",
            "status": "connected"
        }
        res = await self.manager.get_or_reconnect_client("session_1")
        self.assertIs(res, connected_client)

        disconnected_client = MagicMock()
        disconnected_client.is_connected.return_value = False
        disconnected_client.connect = AsyncMock()
        disconnected_client.is_user_authorized = AsyncMock(return_value=True)
        disconnected_client.get_me = AsyncMock(return_value=MagicMock())

        self.manager._clients["session_2"] = {
            "client": disconnected_client,
            "nickname": "User2",
            "status": "connect_failed"
        }
        def side_effect():
            disconnected_client.is_connected.return_value = True
        disconnected_client.connect.side_effect = side_effect

        res2 = await self.manager.get_or_reconnect_client("session_2")
        self.assertIs(res2, disconnected_client)


if __name__ == "__main__":
    unittest.main()
