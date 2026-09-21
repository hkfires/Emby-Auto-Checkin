import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from tgservice.checkin_strategies import StartCommandButtonAlertStrategy


class ExecuteActionApiTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Import without reading or migrating real account/session configuration.
        with patch("utils.config.migrate_session_names"), \
             patch("tgservice.client_manager.load_config", return_value={}):
            cls.service = importlib.import_module("tgservice.main")

    def setUp(self):
        self.client = MagicMock()
        self.client.get_entity = AsyncMock(return_value=SimpleNamespace(id=12345, username="test_bot"))
        self.client.send_message = AsyncMock()
        manager = MagicMock()
        manager.get_client.return_value = self.client
        manager._clients = {"test_session": {"nickname": "test_user"}}
        self.enterContext(patch.object(self.service, "client_manager", manager))
        self.execute = self.enterContext(patch.object(
            StartCommandButtonAlertStrategy, "execute", new_callable=AsyncMock))
        self.execute.return_value = {"success": True, "message": "签到成功"}
        self.payload = {
            "session_name": "test_session",
            "target_entity_identifier": "test_bot",
            "strategy_id": "start_button_alert",
            "task_config": {"timeout": 15},
        }

    async def test_invalid_timeout_returns_configuration_error_after_entity_resolution(self):
        # ASGITransport does not run lifespan hooks or connect Telegram sessions.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.service.app), base_url="http://testserver"
        ) as api:
            for timeout in (None, "abc", "inf", "nan", 0, -1, True):
                with self.subTest(timeout=timeout):
                    self.client.get_entity.reset_mock()
                    self.payload["task_config"] = {"timeout": timeout}
                    response = await api.post("/actions/execute", json=self.payload)

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json(), {"detail": "任务配置 timeout 必须为有限的正数（秒）。"})
                    self.client.get_entity.assert_awaited_once_with("test_bot")
                    self.client.send_message.assert_not_awaited()
                    self.execute.assert_not_awaited()

    async def test_entity_lookup_failure_remains_not_found(self):
        self.client.get_entity.side_effect = ValueError("missing entity")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.service.app), base_url="http://testserver"
        ) as api:
            response = await api.post("/actions/execute", json=self.payload)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Could not find entity: test_bot"})
        self.execute.assert_not_awaited()

    async def test_valid_timeout_executes_strategy(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.service.app), base_url="http://testserver"
        ) as api:
            response = await api.post("/actions/execute", json=self.payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.execute.return_value)
        self.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
