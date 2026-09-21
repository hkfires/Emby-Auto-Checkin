import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from telethon import errors, events, types

from tgservice.checkin_strategies import (
    CheckinStrategy,
    StrategyConfigurationError,
    StartCommandButtonAlertStrategy,
    CheckinCommandTextStrategy,
    MathCaptchaStrategy,
    VisionCaptchaStrategy,
)

logger = logging.getLogger("test_checkin")
COMMAND_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class DummyButton:
    def __init__(self, text, click_result="ok"):
        self.text = text
        self._click_result = click_result
        self.clicked = False

    async def click(self):
        self.clicked = True
        return self._click_result


class DummyMessage:
    def __init__(self, msg_id, chat_id, text, buttons=None, reply_to=None, date=None, edit_date=None):
        self.id = msg_id
        self.chat_id = chat_id
        self.sender_id = chat_id
        self.peer_id = types.PeerUser(chat_id)
        self.post = False
        self.text = text
        self.raw_text = text
        self.buttons = buttons or []
        self.reply_to = reply_to
        self.date = date
        self.edit_date = edit_date


class DummyEntity:
    def __init__(self, entity_id, username="test_bot"):
        self.id = entity_id
        self.username = username
        self.title = username


class CheckinStrategiesTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def client_with_queued_events(*queued_events, history=()):
        client = MagicMock()
        client.handlers = []
        client.add_event_handler.side_effect = lambda h, builder: client.handlers.append((h, builder))

        def remove_handler(handler):
            client.handlers[:] = [(h, b) for h, b in client.handlers if h is not handler]

        async def send(*args):
            # Dispatch real Telethon event types while command submission is pending.
            for event in queued_events:
                for handler, builder in list(client.handlers):
                    if type(event) is builder.Event:
                        await handler(event)
            return DummyMessage(10, 12345, "/start", date=COMMAND_DATE)

        client.remove_event_handler.side_effect = remove_handler
        client.send_message = AsyncMock(side_effect=send)
        client.get_messages = AsyncMock(return_value=list(history))
        return client

    def test_default_timeout_is_15_seconds(self):
        client = MagicMock()
        target = DummyEntity(12345)
        strategy = CheckinStrategy(client, target, logger, "test_user")
        self.assertEqual(strategy.timeout_seconds, 15)

    def test_custom_timeout_from_config(self):
        for strategy_class in (CheckinStrategy, StartCommandButtonAlertStrategy,
                               CheckinCommandTextStrategy, MathCaptchaStrategy, VisionCaptchaStrategy):
            for timeout in (25, "25", 0.05, "0.05"):
                with self.subTest(strategy=strategy_class.__name__, timeout=timeout):
                    with patch("tgservice.checkin_strategies.load_config", return_value={}):
                        strategy = strategy_class(
                            MagicMock(), DummyEntity(12345), logger, "test_user", {"timeout": timeout})
                    self.assertEqual(strategy.timeout_seconds, float(timeout))

    def test_invalid_timeout_is_rejected(self):
        invalid_values = ("abc", None, "inf", float("inf"), "-inf", float("-inf"),
                          "nan", float("nan"), 0, "0", -1, "-1", True, False, [], {}, 10 ** 400)
        for strategy_class in (CheckinStrategy, StartCommandButtonAlertStrategy,
                               CheckinCommandTextStrategy, MathCaptchaStrategy, VisionCaptchaStrategy):
            for timeout in invalid_values:
                with self.subTest(strategy=strategy_class.__name__, timeout=timeout):
                    with patch("tgservice.checkin_strategies.load_config", return_value={}):
                        with self.assertRaises(StrategyConfigurationError):
                            strategy_class(
                                MagicMock(), DummyEntity(12345), logger, "test_user", {"timeout": timeout})

    def test_captcha_defaults_are_preserved(self):
        for strategy_class, default in ((MathCaptchaStrategy, 30), (VisionCaptchaStrategy, 60)):
            with self.subTest(strategy=strategy_class.__name__):
                with patch("tgservice.checkin_strategies.load_config", return_value={}):
                    strategy = strategy_class(MagicMock(), DummyEntity(12345), logger, "test_user", {})
                self.assertEqual(strategy.timeout_seconds, default)

    async def test_execute_initial_step_success(self):
        target = DummyEntity(12345)
        client = MagicMock()
        sent_msg = DummyMessage(10, 12345, "/start")
        client.send_message = AsyncMock(return_value=sent_msg)

        handlers = []

        def add_handler(h, filter_obj):
            handlers.append(h)

        def remove_handler(h):
            if h in handlers:
                handlers.remove(h)

        client.add_event_handler = MagicMock(side_effect=add_handler)
        client.remove_event_handler = MagicMock(side_effect=remove_handler)

        strategy = StartCommandButtonAlertStrategy(
            client, target, logger, "test_user",
            task_config={"initial_button_click_delay": 0, "timeout": 5}
        )

        async def emit_reply():
            await asyncio.sleep(0.05)
            btn = DummyButton("签到")
            reply_event = MagicMock()
            reply_event.chat_id = 12345
            reply_event.sender_id = 12345
            reply_event.message = DummyMessage(11, 12345, "欢迎", buttons=[[btn]])
            for h in list(handlers):
                await h(reply_event)

        task = asyncio.create_task(emit_reply())
        click_obj, source_msg, error = await strategy._execute_initial_step("/start", ["签到"])
        await task

        self.assertIsNone(error)
        self.assertEqual(click_obj, "ok")
        self.assertIsNotNone(source_msg)

    async def test_execute_initial_step_timeout_fallback_to_get_messages(self):
        target = DummyEntity(12345)
        client = MagicMock()
        sent_msg = DummyMessage(10, 12345, "/start")
        client.send_message = AsyncMock(return_value=sent_msg)

        handlers = []
        client.add_event_handler = MagicMock(side_effect=lambda h, f: handlers.append(h))
        client.remove_event_handler = MagicMock(side_effect=lambda h: handlers.remove(h) if h in handlers else None)

        btn = DummyButton("签到")
        bot_msg = DummyMessage(11, 12345, "欢迎点击", buttons=[[btn]])
        client.get_messages = AsyncMock(return_value=[bot_msg])

        # Timeout fast (0.1s), simulate event not pushed, fallback fetches message
        strategy = StartCommandButtonAlertStrategy(
            client, target, logger, "test_user",
            task_config={"timeout": 0.1, "initial_button_click_delay": 0}
        )

        click_obj, source_msg, error = await strategy._execute_initial_step("/start", ["签到"])
        self.assertIsNone(error)
        self.assertEqual(click_obj, "ok")
        self.assertEqual(source_msg.id, 11)
        self.assertTrue(btn.clicked)

    async def test_execute_initial_step_true_timeout_when_no_messages(self):
        target = DummyEntity(12345)
        client = MagicMock()
        sent_msg = DummyMessage(10, 12345, "/start")
        client.send_message = AsyncMock(return_value=sent_msg)
        client.add_event_handler = MagicMock()
        client.remove_event_handler = MagicMock()
        client.get_messages = AsyncMock(return_value=[])

        strategy = StartCommandButtonAlertStrategy(
            client, target, logger, "test_user",
            task_config={"timeout": 0.05}
        )

        click_obj, source_msg, error = await strategy._execute_initial_step("/start", ["签到"])
        self.assertIsInstance(error, asyncio.TimeoutError)
        self.assertIsNone(click_obj)
        self.assertIsNone(source_msg)

    async def test_send_timeout_propagates_without_reading_history(self):
        client = MagicMock()
        failure = asyncio.TimeoutError("send failed")
        client.send_message = AsyncMock(side_effect=failure)
        client.get_messages = AsyncMock(return_value=[DummyMessage(1, 12345, "签到成功")])
        strategy = StartCommandButtonAlertStrategy(client, DummyEntity(12345), logger, "test")
        with self.assertRaises(asyncio.TimeoutError) as caught:
            await strategy.execute()
        self.assertIs(caught.exception, failure)
        client.get_messages.assert_not_awaited()
        client.remove_event_handler.assert_called_once()

    async def test_history_rpc_errors_propagate_unchanged(self):
        for error_type in (errors.AuthKeyUnregisteredError, errors.ChatWriteForbiddenError,
                           errors.RPCError):
            with self.subTest(error_type=error_type):
                failure = (error_type(None, "rpc failure") if error_type is errors.RPCError
                           else error_type(None))
                client = MagicMock()
                client.send_message = AsyncMock(return_value=DummyMessage(10, 12345, "/start"))
                client.get_messages = AsyncMock(side_effect=failure)
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test", {"timeout": 0.01})
                with self.assertRaises(error_type) as caught:
                    await strategy.execute()
                self.assertIs(caught.exception, failure)
                client.remove_event_handler.assert_called_once()

    async def test_deadline_during_delay_or_rpc_clicks_only_once(self):
        for click_delay, rpc_delay in ((0.05, 0), (0, 0.05)):
            with self.subTest(click_delay=click_delay, rpc_delay=rpc_delay):
                client = MagicMock()
                handlers = []
                client.add_event_handler.side_effect = lambda h, f: handlers.append(h)
                client.remove_event_handler.side_effect = lambda h: handlers.clear()
                button = DummyButton("签到")

                async def click():
                    await asyncio.sleep(rpc_delay)
                    return "ok"

                button.click = AsyncMock(side_effect=click)
                event = MagicMock(chat_id=12345, sender_id=12345)
                event.message = DummyMessage(11, 12345, "签到", buttons=[[button]])

                async def send(*args):
                    # Queue both new-message and edited-message events before send returns.
                    for handler in list(handlers):
                        await handler(event)
                    return DummyMessage(10, 12345, "/start")

                client.send_message = AsyncMock(side_effect=send)
                client.get_messages = AsyncMock(return_value=[event.message])
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test",
                    {"timeout": 0.02, "initial_button_click_delay": click_delay})
                result = await strategy._execute_initial_step("/start", ["签到"])
                self.assertEqual(result[0], "ok")
                self.assertIsNone(result[2])
                await asyncio.sleep(0.06)
                button.click.assert_awaited_once()
                client.get_messages.assert_not_awaited()
                self.assertEqual(handlers, [])

    async def test_stalled_click_times_out_and_cleans_up(self):
        for source in ("event", "history"):
            with self.subTest(source=source):
                button = DummyButton("签到")
                queued_button = DummyButton("签到")
                message = DummyMessage(11, 12345, "菜单", buttons=[[button]])
                queued_message = DummyMessage(12, 12345, "新菜单", buttons=[[queued_button]])
                queued_events = (
                    events.NewMessage.Event(message), events.NewMessage.Event(queued_message)
                ) if source == "event" else ()
                client = self.client_with_queued_events(*queued_events, history=[message])
                rpc_cancelled = asyncio.Event()
                saved_handlers = []

                async def stalled_click():
                    saved_handlers.extend(client.handlers)
                    try:
                        await asyncio.Event().wait()
                    finally:
                        rpc_cancelled.set()

                button.click = AsyncMock(side_effect=stalled_click)
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test",
                    {"timeout": 0.02, "initial_button_click_delay": 0})
                strategy.BUTTON_CLICK_TIMEOUT_SECONDS = 0.05
                started_at = asyncio.get_running_loop().time()
                tasks_before = asyncio.all_tasks()
                # The click budget, not this watchdog, must terminate the RPC.
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(strategy._execute_initial_step("/start", ["签到"]), timeout=2)

                self.assertLess(asyncio.get_running_loop().time() - started_at, 1)
                self.assertTrue(rpc_cancelled.is_set())
                self.assertEqual(client.handlers, [])
                for handler, _ in saved_handlers:
                    await handler(events.NewMessage.Event(queued_message))
                await asyncio.sleep(0.01)
                button.click.assert_awaited_once()
                self.assertFalse(queued_button.clicked)
                self.assertEqual(asyncio.all_tasks(), tasks_before)
                if source == "event":
                    client.get_messages.assert_not_awaited()
                else:
                    client.get_messages.assert_awaited_once_with(strategy.target_entity, limit=3)

    async def test_click_delay_is_included_in_click_budget(self):
        button = DummyButton("签到")
        message = DummyMessage(11, 12345, "菜单", buttons=[[button]])
        client = self.client_with_queued_events(events.NewMessage.Event(message))
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test",
            {"timeout": 0.1, "initial_button_click_delay": 0.5})
        strategy.BUTTON_CLICK_TIMEOUT_SECONDS = 0.02

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(strategy._execute_initial_step("/start", ["签到"]), timeout=1)

        self.assertFalse(button.clicked)
        client.get_messages.assert_not_awaited()
        self.assertEqual(client.handlers, [])

    async def test_external_cancellation_during_click_cleans_up(self):
        button = DummyButton("签到")
        message = DummyMessage(11, 12345, "菜单", buttons=[[button]])
        client = self.client_with_queued_events(events.NewMessage.Event(message))
        click_started = asyncio.Event()
        click_cancelled = asyncio.Event()

        async def stalled_click():
            click_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                click_cancelled.set()

        button.click = AsyncMock(side_effect=stalled_click)
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"initial_button_click_delay": 0})
        task = asyncio.create_task(strategy._execute_initial_step("/start", ["签到"]))
        try:
            await asyncio.wait_for(click_started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(click_cancelled.is_set())
        button.click.assert_awaited_once()
        client.get_messages.assert_not_awaited()
        self.assertEqual(client.handlers, [])

    async def test_queued_success_predating_command_is_ignored(self):
        for message_id in (9, 10):
            with self.subTest(message_id=message_id):
                old_success = DummyMessage(message_id, 12345, "签到成功")
                client = self.client_with_queued_events(
                    events.NewMessage.Event(old_success), history=[old_success])
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test", {"timeout": 0.02})

                result = await strategy.execute()

                self.assertFalse(result["success"])
                client.get_messages.assert_awaited_once_with(strategy.target_entity, limit=3)
                self.assertEqual(client.handlers, [])

    async def test_queued_old_button_is_ignored_before_current_response(self):
        old_button = DummyButton("签到")
        current_button = DummyButton("签到")
        old_message = DummyMessage(9, 12345, "旧菜单", buttons=[[old_button]])
        current_message = DummyMessage(11, 12345, "新菜单", buttons=[[current_button]])
        client = self.client_with_queued_events(
            events.NewMessage.Event(old_message), events.NewMessage.Event(current_message))
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test",
            {"timeout": 0.1, "initial_button_click_delay": 0})

        click, source, error = await strategy._execute_initial_step("/start", ["签到"])

        self.assertIsNone(error)
        self.assertEqual(click, "ok")
        self.assertIs(source, current_message)
        self.assertFalse(old_button.clicked)
        self.assertTrue(current_button.clicked)
        client.get_messages.assert_not_awaited()
        self.assertEqual(client.handlers, [])

    async def test_refetch_deadline_cancels_rpc_and_finishes_with_saved_response(self):
        message = DummyMessage(11, 12345, "菜单加载中")
        client = self.client_with_queued_events(events.NewMessage.Event(message))
        rpc_cancelled = asyncio.Event()

        async def get_messages(target, *, ids=None, limit=None):
            if ids is None:
                self.assertEqual(limit, 3)
                return []
            try:
                await asyncio.Event().wait()
            finally:
                rpc_cancelled.set()

        client.get_messages.side_effect = get_messages
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.4})
        started_at = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(strategy.execute(), timeout=2)

        self.assertLess(asyncio.get_running_loop().time() - started_at, 1)
        self.assertFalse(result["success"])
        self.assertIn(message.raw_text, result["message"])
        client.get_messages.assert_has_awaits([
            call(strategy.target_entity, ids=11), call(strategy.target_entity, limit=3),
        ])
        self.assertEqual(client.get_messages.await_count, 2)
        self.assertTrue(rpc_cancelled.is_set())
        self.assertEqual(client.handlers, [])

    async def test_refetch_is_skipped_when_delay_exceeds_remaining_time(self):
        message = DummyMessage(11, 12345, "菜单加载中")
        client = self.client_with_queued_events(events.NewMessage.Event(message))
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.02})

        result = await asyncio.wait_for(strategy.execute(), timeout=1)

        self.assertFalse(result["success"])
        client.get_messages.assert_awaited_once_with(strategy.target_entity, limit=3)
        self.assertEqual(client.handlers, [])

    async def test_history_timeout_cancels_rpc_and_cleans_handlers(self):
        client = self.client_with_queued_events()
        rpc_cancelled = asyncio.Event()

        async def stalled_read(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                rpc_cancelled.set()

        client.get_messages.side_effect = stalled_read
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.05})
        started_at = asyncio.get_running_loop().time()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(strategy._execute_initial_step("/start", ["签到"]), timeout=2)

        self.assertLess(asyncio.get_running_loop().time() - started_at, 1)
        client.get_messages.assert_awaited_once_with(strategy.target_entity, limit=3)
        self.assertTrue(rpc_cancelled.is_set())
        self.assertEqual(client.handlers, [])

    async def test_refetch_rpc_error_propagates_without_history_recovery(self):
        message = DummyMessage(11, 12345, "菜单加载中")
        client = self.client_with_queued_events(events.NewMessage.Event(message))
        failure = errors.AuthKeyUnregisteredError(None)
        client.get_messages.side_effect = failure
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 1})
        with self.assertRaises(errors.AuthKeyUnregisteredError) as caught:
            await strategy.execute()

        self.assertIs(caught.exception, failure)
        client.get_messages.assert_awaited_once_with(strategy.target_entity, ids=11)
        self.assertEqual(client.handlers, [])

    async def test_rpc_timeout_is_not_treated_as_response_deadline(self):
        # Even saved success text must not hide a timeout raised by the RPC itself.
        message = DummyMessage(11, 12345, "签到成功")
        client = self.client_with_queued_events(events.NewMessage.Event(message))
        failure = asyncio.TimeoutError("RPC timeout")
        client.get_messages.side_effect = failure
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 1})

        with self.assertRaises(asyncio.TimeoutError) as caught:
            await strategy.execute()

        self.assertIs(caught.exception, failure)
        client.get_messages.assert_awaited_once_with(strategy.target_entity, ids=11)
        self.assertEqual(client.handlers, [])

    async def test_external_cancellation_during_refetch_is_not_recovered(self):
        message = DummyMessage(11, 12345, "签到成功")
        client = self.client_with_queued_events(events.NewMessage.Event(message))
        read_started = asyncio.Event()
        read_cancelled = asyncio.Event()

        async def stalled_read(*args, **kwargs):
            read_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                read_cancelled.set()

        client.get_messages.side_effect = stalled_read
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 2})
        task = asyncio.create_task(strategy.execute())
        try:
            await asyncio.wait_for(read_started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(read_cancelled.is_set())
        client.get_messages.assert_awaited_once_with(strategy.target_entity, ids=11)
        self.assertEqual(client.handlers, [])

    async def test_private_reply_to_previous_command_is_ignored(self):
        for event_type in (events.NewMessage.Event, events.MessageEdited.Event):
            with self.subTest(event_type=event_type):
                stale_button = DummyButton("签到")
                current_button = DummyButton("签到")
                stale_message = DummyMessage(
                    11, 12345, "旧命令回复", buttons=[[stale_button]],
                    reply_to=SimpleNamespace(reply_to_msg_id=8))
                current_message = DummyMessage(
                    12, 12345, "当前命令回复", buttons=[[current_button]],
                    reply_to=SimpleNamespace(reply_to_msg_id=10))
                client = self.client_with_queued_events(
                    event_type(stale_message), events.NewMessage.Event(current_message))
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test",
                    {"timeout": 0.1, "initial_button_click_delay": 0})

                click, source, error = await strategy._execute_initial_step("/start", ["签到"])

                self.assertIsNone(error)
                self.assertEqual(click, "ok")
                self.assertIs(source, current_message)
                self.assertFalse(stale_button.clicked)
                self.assertTrue(current_button.clicked)
                client.get_messages.assert_not_awaited()
                self.assertEqual(client.handlers, [])

    async def test_history_cannot_reintroduce_rejected_private_reply(self):
        button = DummyButton("签到")
        message = DummyMessage(
            11, 12345, "签到成功", buttons=[[button]], reply_to=SimpleNamespace(reply_to_msg_id=8))
        client = self.client_with_queued_events(events.NewMessage.Event(message), history=[message])
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.02, "initial_button_click_delay": 0})

        result = await strategy.execute()

        self.assertFalse(result["success"])
        self.assertFalse(button.clicked)
        client.get_messages.assert_awaited_once_with(strategy.target_entity, limit=3)
        self.assertEqual(client.handlers, [])

    async def test_edited_private_menu_can_be_reused(self):
        button = DummyButton("签到")
        message = DummyMessage(
            9, 12345, "更新菜单", buttons=[[button]], reply_to=SimpleNamespace(reply_to_msg_id=8),
            date=COMMAND_DATE - timedelta(days=1), edit_date=COMMAND_DATE + timedelta(seconds=1))
        client = self.client_with_queued_events(events.MessageEdited.Event(message))
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.1, "initial_button_click_delay": 0})

        click, source, error = await strategy._execute_initial_step("/start", ["签到"])

        self.assertIsNone(error)
        self.assertEqual(click, "ok")
        self.assertIs(source, message)
        self.assertTrue(button.clicked)
        client.get_messages.assert_not_awaited()
        self.assertEqual(client.handlers, [])

    async def test_stale_menu_edits_are_ignored_before_current_menu(self):
        for edit_date in (None, COMMAND_DATE - timedelta(days=1),
                          COMMAND_DATE - timedelta(seconds=1), COMMAND_DATE):
            with self.subTest(edit_date=edit_date):
                stale_button = DummyButton("签到")
                current_button = DummyButton("签到")
                stale_menu = DummyMessage(
                    9, 12345, "旧菜单", buttons=[[stale_button]],
                    reply_to=SimpleNamespace(reply_to_msg_id=8), edit_date=edit_date)
                current_menu = DummyMessage(
                    11, 12345, "当前菜单", buttons=[[current_button]],
                    reply_to=SimpleNamespace(reply_to_msg_id=10))
                client = self.client_with_queued_events(
                    events.MessageEdited.Event(stale_menu), events.NewMessage.Event(current_menu))
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test", {"initial_button_click_delay": 0})

                click, source, error = await strategy._execute_initial_step("/start", ["签到"])

                self.assertIsNone(error)
                self.assertEqual(click, "ok")
                self.assertIs(source, current_menu)
                self.assertFalse(stale_button.clicked)
                self.assertTrue(current_button.clicked)
                client.get_messages.assert_not_awaited()
                self.assertEqual(client.handlers, [])

    async def test_reused_menu_requires_command_timestamp(self):
        button = DummyButton("签到")
        message = DummyMessage(
            9, 12345, "旧菜单", buttons=[[button]], edit_date=COMMAND_DATE + timedelta(seconds=1))
        client = self.client_with_queued_events(events.MessageEdited.Event(message))
        original_send = client.send_message.side_effect

        async def send_without_date(*args):
            sent = await original_send(*args)
            sent.date = None
            return sent

        client.send_message.side_effect = send_without_date
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.02, "initial_button_click_delay": 0})

        result = await strategy.execute()

        self.assertFalse(result["success"])
        self.assertFalse(button.clicked)
        self.assertEqual(client.handlers, [])

    async def test_edited_old_text_is_not_a_reusable_menu(self):
        for buttons in ([], [[DummyButton("其他操作")]]):
            with self.subTest(buttons=bool(buttons)):
                message = DummyMessage(9, 12345, "签到成功", buttons=buttons)
                client = self.client_with_queued_events(events.MessageEdited.Event(message), history=[message])
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test", {"timeout": 0.02})

                result = await strategy.execute()

                self.assertFalse(result["success"])
                client.get_messages.assert_awaited_once_with(strategy.target_entity, limit=3)
                self.assertEqual(client.handlers, [])

    async def test_late_notice_does_not_replace_processed_success(self):
        for slow_refetch in (False, True):
            with self.subTest(slow_refetch=slow_refetch):
                confirmed = DummyMessage(11, 12345, "签到成功", buttons=[[DummyButton("帮助")]])
                notice = DummyMessage(12, 12345, "服务公告：欢迎加入交流群")
                client = self.client_with_queued_events(events.NewMessage.Event(confirmed))
                read_cancelled = asyncio.Event()

                async def get_messages(target, *, ids=None, limit=None):
                    if ids is None:
                        return [notice, confirmed]
                    self.assertEqual(ids, 12)
                    try:
                        await asyncio.sleep(0.2)
                    except asyncio.CancelledError:
                        read_cancelled.set()
                        raise
                    return notice

                client.get_messages.side_effect = get_messages
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test", {"timeout": 0.8})
                started_at = asyncio.get_running_loop().time()
                task = asyncio.create_task(strategy.execute())
                try:
                    await asyncio.sleep(0.4 if slow_refetch else 0.65)
                    event = events.NewMessage.Event(notice)
                    for handler, builder in list(client.handlers):
                        if type(event) is builder.Event:
                            await handler(event)
                    result = await asyncio.wait_for(task, timeout=2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

                self.assertGreaterEqual(asyncio.get_running_loop().time() - started_at, 0.75)
                self.assertEqual(result, {"success": True, "message": confirmed.raw_text})
                if slow_refetch:
                    self.assertTrue(read_cancelled.is_set())
                    client.get_messages.assert_awaited_once_with(strategy.target_entity, ids=12)
                else:
                    client.get_messages.assert_not_awaited()
                self.assertEqual(client.handlers, [])

    async def test_late_success_edit_remains_available_for_final_assessment(self):
        pending = DummyMessage(11, 12345, "签到中", buttons=[[DummyButton("帮助")]])
        confirmed = DummyMessage(11, 12345, "签到成功")
        client = self.client_with_queued_events(events.NewMessage.Event(pending))
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.4})
        task = asyncio.create_task(strategy.execute())
        try:
            await asyncio.sleep(0.2)
            event = events.MessageEdited.Event(confirmed)
            for handler, builder in list(client.handlers):
                if type(event) is builder.Event:
                    await handler(event)
            result = await asyncio.wait_for(task, timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(result, {"success": True, "message": confirmed.raw_text})
        client.get_messages.assert_not_awaited()
        self.assertEqual(client.handlers, [])

    async def test_late_edit_invalidates_previous_version_of_same_response(self):
        confirmed = DummyMessage(11, 12345, "签到成功", buttons=[[DummyButton("帮助")]])
        corrected = DummyMessage(11, 12345, "签到失败")
        client = self.client_with_queued_events(events.NewMessage.Event(confirmed), history=[corrected])
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.4})
        task = asyncio.create_task(strategy.execute())
        try:
            await asyncio.sleep(0.2)
            event = events.MessageEdited.Event(corrected)
            for handler, builder in list(client.handlers):
                if type(event) is builder.Event:
                    await handler(event)
            result = await asyncio.wait_for(task, timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertFalse(result["success"])
        self.assertIn(corrected.raw_text, result["message"])
        client.get_messages.assert_awaited_once_with(strategy.target_entity, limit=3)
        self.assertEqual(client.handlers, [])

    async def test_button_failure_overrides_previously_processed_success_text(self):
        confirmed = DummyMessage(11, 12345, "签到成功", buttons=[[DummyButton("帮助")]])
        notice = DummyMessage(12, 12345, "服务公告", buttons=[[DummyButton("帮助")]])
        button = DummyButton("签到")
        button.click = AsyncMock(return_value=SimpleNamespace(message="权限不足"))
        menu = DummyMessage(13, 12345, "菜单", buttons=[[button]])
        client = self.client_with_queued_events(
            events.NewMessage.Event(confirmed), events.NewMessage.Event(notice), events.NewMessage.Event(menu))
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.1, "initial_button_click_delay": 0})

        result = await strategy.execute()

        self.assertFalse(result["success"])
        button.click.assert_awaited_once()
        client.get_messages.assert_not_awaited()
        self.assertEqual(client.handlers, [])

    async def test_refetch_error_is_not_hidden_by_previously_processed_success(self):
        for failure in (errors.AuthKeyUnregisteredError(None), asyncio.TimeoutError("RPC timeout")):
            with self.subTest(error_type=type(failure)):
                confirmed = DummyMessage(11, 12345, "签到成功", buttons=[[DummyButton("帮助")]])
                notice = DummyMessage(12, 12345, "服务公告")
                client = self.client_with_queued_events(
                    events.NewMessage.Event(confirmed), events.NewMessage.Event(notice))
                client.get_messages.side_effect = failure
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test", {"timeout": 1})

                with self.assertRaises(type(failure)) as caught:
                    await strategy.execute()

                self.assertIs(caught.exception, failure)
                client.get_messages.assert_awaited_once_with(strategy.target_entity, ids=12)
                self.assertEqual(client.handlers, [])

    async def test_success_survives_optional_refetch_crossing_response_deadline(self):
        help_button = DummyButton("帮助")
        confirmed = DummyMessage(11, 12345, "签到成功", buttons=[[help_button]])
        edited = DummyMessage(
            11, 12345, "签到成功", edit_date=COMMAND_DATE + timedelta(seconds=1))
        client = self.client_with_queued_events(events.NewMessage.Event(confirmed))
        read_cancelled = asyncio.Event()

        async def slow_read(*args, **kwargs):
            try:
                # This RPC would succeed, but only after the response deadline.
                await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                read_cancelled.set()
                raise
            return edited

        client.get_messages.side_effect = slow_read
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.8, "initial_button_click_delay": 0})
        started_at = asyncio.get_running_loop().time()
        task = asyncio.create_task(strategy.execute())
        try:
            await asyncio.sleep(0.25)
            event = events.MessageEdited.Event(edited)
            for handler, builder in list(client.handlers):
                if type(event) is builder.Event:
                    await handler(event)
            result = await asyncio.wait_for(task, timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        elapsed = asyncio.get_running_loop().time() - started_at
        self.assertGreaterEqual(elapsed, 0.75)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(result, {"success": True, "message": edited.raw_text})
        self.assertTrue(read_cancelled.is_set())
        client.get_messages.assert_awaited_once_with(strategy.target_entity, ids=11)
        self.assertFalse(help_button.clicked)
        self.assertEqual(client.handlers, [])

    async def test_successful_buttonless_edit_is_parsed_after_response_wait(self):
        help_button = DummyButton("帮助")
        confirmed = DummyMessage(11, 12345, "签到成功", buttons=[[help_button]])
        edited = DummyMessage(
            11, 12345, "签到成功", edit_date=COMMAND_DATE + timedelta(seconds=1))
        client = self.client_with_queued_events(events.NewMessage.Event(confirmed))
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.4, "initial_button_click_delay": 0})
        started_at = asyncio.get_running_loop().time()
        task = asyncio.create_task(strategy.execute())
        try:
            # Keep waiting for buttons even when a late edit contains success text.
            await asyncio.sleep(0.2)
            event = events.MessageEdited.Event(edited)
            for handler, builder in list(client.handlers):
                if type(event) is builder.Event:
                    await handler(event)
            result = await asyncio.wait_for(task, timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertGreaterEqual(asyncio.get_running_loop().time() - started_at, 0.35)
        self.assertEqual(result, {"success": True, "message": edited.raw_text})
        client.get_messages.assert_not_awaited()
        self.assertFalse(help_button.clicked)
        self.assertEqual(client.handlers, [])

    async def test_buttonless_text_waits_for_delayed_checkin_button(self):
        for text in ("签到成功后可获得积分，请点击签到按钮。", "签到成功"):
            for callback_text, expected_success in (("签到成功", True), ("权限不足", False)):
                with self.subTest(text=text, callback_text=callback_text):
                    initial = DummyMessage(11, 12345, text)
                    button = DummyButton("签到")
                    button.click = AsyncMock(return_value=SimpleNamespace(message=callback_text))
                    menu = DummyMessage(11, 12345, text, buttons=[[button]])
                    client = self.client_with_queued_events(events.NewMessage.Event(initial))
                    client.get_messages.return_value = menu
                    strategy = StartCommandButtonAlertStrategy(
                        client, DummyEntity(12345), logger, "test",
                        {"timeout": 1, "initial_button_click_delay": 0})
                    task = asyncio.create_task(strategy.execute())
                    try:
                        await asyncio.sleep(0.05)
                        event = events.MessageEdited.Event(menu)
                        for handler, builder in list(client.handlers):
                            if type(event) is builder.Event:
                                await handler(event)
                        result = await asyncio.wait_for(task, timeout=2)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

                    button.click.assert_awaited_once()
                    self.assertEqual(result["success"], expected_success)
                    client.get_messages.assert_awaited_once_with(strategy.target_entity, ids=11)
                    self.assertEqual(client.handlers, [])

    async def test_math_strategy_still_waits_for_its_initial_button(self):
        text_response = DummyMessage(11, 12345, "签到成功")
        button = DummyButton("签到", click_result=SimpleNamespace(message="签到成功"))
        menu = DummyMessage(12, 12345, "菜单", buttons=[[button]])
        client = self.client_with_queued_events(
            events.NewMessage.Event(text_response), events.NewMessage.Event(menu))
        client.get_messages.return_value = text_response
        strategy = MathCaptchaStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 1, "initial_button_click_delay": 0})

        result = await strategy.execute()

        self.assertTrue(result["success"])
        self.assertTrue(button.clicked)
        client.get_messages.assert_awaited_once_with(strategy.target_entity, ids=11)
        self.assertEqual(client.handlers, [])

    async def test_success_edit_is_not_replaced_by_newer_informational_history(self):
        for has_other_buttons in (False, True):
            with self.subTest(has_other_buttons=has_other_buttons):
                help_button = DummyButton("帮助")
                buttons = [[help_button]] if has_other_buttons else []
                status = DummyMessage(11, 12345, "签到中", buttons=buttons)
                information = DummyMessage(12, 12345, "每日活动说明", buttons=buttons)
                confirmed = DummyMessage(
                    11, 12345, "签到成功，获得 1 积分", buttons=buttons,
                    edit_date=COMMAND_DATE + timedelta(seconds=1))
                client = self.client_with_queued_events(
                    events.NewMessage.Event(status), events.NewMessage.Event(information),
                    events.MessageEdited.Event(confirmed))

                async def get_messages(target, *, ids=None, limit=None):
                    if ids is not None:
                        return {11: confirmed, 12: information}[ids]
                    # History is ordered by creation ID, not by edit time.
                    self.assertEqual(limit, 3)
                    return [information, confirmed]

                client.get_messages.side_effect = get_messages
                strategy = StartCommandButtonAlertStrategy(
                    client, DummyEntity(12345), logger, "test",
                    {"timeout": 0.05 if has_other_buttons else 1.5, "initial_button_click_delay": 0})

                result = await strategy.execute()

                self.assertEqual(result, {"success": True, "message": confirmed.raw_text})
                self.assertFalse(help_button.clicked)
                self.assertEqual(client.handlers, [])
                # A confirmed result needs no optional history lookup.
                self.assertFalse(any("limit" in call.kwargs for call in client.get_messages.await_args_list))

    async def test_unconfirmed_response_still_uses_history_fallback(self):
        status = DummyMessage(11, 12345, "签到中", buttons=[[DummyButton("帮助")]])
        confirmed = DummyMessage(12, 12345, "签到成功")
        client = self.client_with_queued_events(events.NewMessage.Event(status), history=[confirmed])
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.02})

        result = await strategy.execute()

        self.assertEqual(result, {"success": True, "message": confirmed.raw_text})
        client.get_messages.assert_awaited_once_with(strategy.target_entity, limit=3)
        self.assertEqual(client.handlers, [])

    async def test_fallback_ignores_historical_success(self):
        client = MagicMock()
        client.send_message = AsyncMock(return_value=DummyMessage(10, 12345, "/start"))
        client.get_messages = AsyncMock(return_value=[DummyMessage(1, 12345, "签到成功")])
        strategy = StartCommandButtonAlertStrategy(
            client, DummyEntity(12345), logger, "test", {"timeout": 0.01})
        result = await strategy.execute()
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
