import asyncio
import os
import socket
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from utils import notification as n


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dns = patch.object(socket, 'getaddrinfo', return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('149.154.167.220', 443))
        ])
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.env = patch.dict(os.environ, {'NOTIFICATION_TRUSTED_ORIGINS': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_both_senders_pin_ip_and_preserve_tls_hostname(self):
        response = httpx.Response(200, json={'ok': True})
        with patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=response) as post:
            self.assertTrue((await n.send_telegram_message('123:token', '1', 'hello'))[0])
            args, kwargs = post.call_args
            self.assertEqual(args[0].host, '149.154.167.220')
            self.assertEqual(kwargs['headers']['Host'], 'api.telegram.org')
            self.assertEqual(kwargs['extensions']['sni_hostname'], 'api.telegram.org')
        with patch('httpx.Client.post', return_value=response) as post:
            n.send_telegram_message_sync('123:token', '1', 'hello')
            self.assertEqual(post.call_args.args[0].host, '149.154.167.220')

    async def test_transport_connects_only_to_checked_ip(self):
        import httpcore
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:8080'}):
            with patch('httpcore._backends.anyio.AnyIOBackend.connect_tcp',
                       side_effect=httpcore.ConnectError('blocked in test')) as connect:
                with self.assertRaises(n.NotificationError):
                    await n.send_telegram_message('token', '1', 'hello')
                self.assertEqual(connect.call_args.args[0], '149.154.167.220')
            with patch('httpcore._backends.sync.SyncBackend.connect_tcp',
                       side_effect=httpcore.ConnectError('blocked in test')) as connect:
                with self.assertRaises(n.NotificationError):
                    n.send_telegram_message_sync('token', '1', 'hello')
                self.assertEqual(connect.call_args.kwargs['host'], '149.154.167.220')

    async def test_mixed_dns_answers_are_rejected(self):
        with patch.object(socket, 'getaddrinfo', return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('149.154.167.220', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
        ]):
            with self.assertRaises(n.NotificationError) as ctx:
                await n.send_telegram_message('token', '1', 'hello')
            self.assertEqual(ctx.exception.code, 'destination_blocked')

    async def test_redirect_is_not_followed(self):
        response = httpx.Response(302, headers={'Location': 'https://127.0.0.1'}, json={'ok': False})
        with patch('httpx.AsyncClient.post', return_value=response) as post:
            with self.assertRaises(n.NotificationError):
                await n.send_telegram_message('token', '1', 'hello')
            post.assert_called_once()

    async def test_message_format_and_default_config(self):
        from utils.config import _get_default_config
        self.assertFalse(_get_default_config()['notification_settings']['enabled'])
        text = n.build_failure_notification_text('User<admin>', 1, 'Bot&Name', 'bot', 'strategy', 'A' * 5000)
        self.assertIn('User&lt;admin&gt;', text)
        self.assertIn('Bot&amp;Name', text)
        self.assertIn('已截断', text)
        self.assertLess(len(text), 2000)

    async def test_prohibited_origins_and_credentials(self):
        for url in ['http://api.telegram.org', 'https://127.0.0.1',
                    'https://api.telegram.org@localhost', 'https://api.telegram.org/path',
                    'https://api.telegram.org?x=1', 'https://evil.example',
                    'https://api.telegram.org:444']:
            with self.subTest(url=url), self.assertRaises(n.NotificationError) as ctx:
                await n.send_telegram_message('123:token', '1', 'hello', url)
            self.assertEqual(ctx.exception.code, 'destination_blocked')
        with self.assertRaises(n.NotificationError):
            await n.send_telegram_message('', '1', 'hello')

    async def test_allowlist_cannot_bypass_address_checks(self):
        with patch.dict(os.environ, {'NOTIFICATION_TRUSTED_ORIGINS': 'https://proxy.example'}):
            for ip in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1',
                       'fe80::1', '::ffff:127.0.0.1', '0.0.0.0', '224.0.0.1']:
                with self.subTest(ip=ip), patch.object(socket, 'getaddrinfo', return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))
                ]), patch('httpx.Client.post') as post:
                    with self.assertRaises(n.NotificationError) as ctx:
                        n.send_telegram_message_sync('123:token', '1', 'hello', 'https://proxy.example')
                    self.assertEqual(ctx.exception.code, 'destination_blocked')
                    post.assert_not_called()

    async def test_explicit_protocol_errors_in_both_senders(self):
        for body in [b'<html>secret_token</html>', b'{', b'[]', b'{"ok": "true"}']:
            response = httpx.Response(502, content=body)
            with self.subTest(body=body), patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=response):
                with self.assertRaises(n.NotificationError) as ctx:
                    await n.send_telegram_message('secret_token', '1', 'hello')
                self.assertEqual(ctx.exception.code, 'protocol_error')
                self.assertNotIn('secret_token', str(ctx.exception))
            with patch('httpx.Client.post', return_value=response):
                with self.assertRaises(n.NotificationError) as ctx:
                    n.send_telegram_message_sync('secret_token', '1', 'hello')
                self.assertEqual(ctx.exception.code, 'protocol_error')

    async def test_network_and_api_errors_propagate(self):
        with patch('httpx.AsyncClient.post', side_effect=httpx.ConnectError('secret_token')):
            with self.assertRaises(n.NotificationError) as ctx:
                await n.send_telegram_message('secret_token', '1', 'hello')
            self.assertEqual(ctx.exception.code, 'network_error')
            self.assertNotIn('secret_token', str(ctx.exception))
        with patch('httpx.Client.post', return_value=httpx.Response(403, json={'ok': False})):
            with self.assertRaises(n.NotificationError) as ctx:
                n.send_telegram_message_sync('token', '1', 'hello')
            self.assertEqual(ctx.exception.code, 'api_rejected')

    async def test_disabled_is_distinct_from_delivery_failure(self):
        args = ('User', 1, 'bot', 'bot', 'strategy', 'failed')
        self.assertFalse(await n.notify_checkin_failure(*args, config={}))
        with self.assertRaises(n.NotificationError) as ctx:
            await n.notify_checkin_failure(*args, config={'notification_settings': {'enabled': True}})
        self.assertEqual(ctx.exception.code, 'configuration_error')
        with patch.object(n, 'send_telegram_message', side_effect=RuntimeError('bug')):
            with self.assertRaises(RuntimeError):
                await n.notify_checkin_failure(*args, config={'notification_settings': {'enabled': True}})

    async def test_task_boundaries_preserve_checkin_and_record_delivery_error(self):
        # Isolate package import side effects from real configuration and databases.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            import utils.config as config_module
            import utils.log as log
            stack.enter_context(patch.object(config_module, 'DATA_DIR', tmp))
            stack.enter_context(patch.object(config_module, 'CONFIG_FILE', str(Path(tmp) / 'config.json')))
            stack.enter_context(patch.object(log, 'DATA_DIR', tmp))
            stack.enter_context(patch.object(log, 'DB_FILE', str(Path(tmp) / 'log.db')))
            stack.enter_context(patch.object(log, '_db_initialized', False))
            import utils.scheduler_api as scheduler
            import webapp.api as api
            from flask import Flask
            app = Flask(__name__)
            app.config.update(TESTING=True, LOGIN_DISABLED=True)
            app.register_blueprint(api.api, url_prefix='/api')
            stack.enter_context(patch.object(api, 'current_user'))
            cfg = {'api_id': '1', 'api_hash': 'hash',
                   'users': [{'telegram_id': 1, 'nickname': 'User', 'session_name': 'session', 'status': 'logged_in'}],
                   'bots': [{'bot_username': 'bot', 'strategy': 'checkin_text'}],
                   'checkin_tasks': [{'user_telegram_id': 1, 'bot_username': 'bot'}]}
            for module in [scheduler, api]:
                stack.enter_context(patch.object(module, 'load_config', return_value=cfg))
                stack.enter_context(patch.object(module, 'claim_daily_task', return_value={'claimed': True, 'token': 'token'}))
                stack.enter_context(patch.object(module, 'save_daily_checkin_log'))
                stack.enter_context(patch.object(module, 'execute_action', new_callable=AsyncMock,
                                                 side_effect=lambda **kw: {'success': False, 'message': 'checkin failed'}))
            for source in ['scheduled', 'quick', 'manual']:
                module = scheduler if source == 'scheduled' else api
                events = []
                async def fail(**kwargs):
                    events.append('notify')
                    raise n.NotificationError('network_error', '网络错误')
                with patch.object(module, 'complete_daily_task', side_effect=lambda *a, **k: events.append('complete')), \
                     patch.object(module, 'notify_checkin_failure', side_effect=fail), \
                     patch.object(module, 'record_notification_failure') as record:
                    if source == 'scheduled':
                        result = await scheduler.run_checkin_task(1, 'bot', 'bot', {})
                    elif source == 'quick':
                        result = (await api.execute_all_tasks_internal())['all_tasks_results'][0]['result']
                    else:
                        with app.test_request_context('/api/checkin/manual', method='POST', data={
                            'user_telegram_id': '1', 'target_type': 'bot', 'identifier': 'bot'
                        }):
                            result = (await api.manual_action()).get_json()
                    self.assertEqual(events, ['complete', 'notify'])
                    self.assertFalse(result['success'])
                    self.assertEqual(result['message'], 'checkin failed')
                    self.assertEqual(result['notification_error'], 'network_error')
                    record.assert_called_once_with((1, 'bot', 'bot'), source, 'network_error')
            with patch.object(api, 'send_telegram_message', side_effect=n.NotificationError('protocol_error', '无效 JSON')):
                with app.test_request_context('/api/notification/test', method='POST', data={'bot_token': 'token', 'chat_id': '1'}):
                    response, status = await api.test_notification.__wrapped__()
                    self.assertEqual(status, 502)
                    self.assertEqual(response.get_json()['error_code'], 'protocol_error')
            log.record_notification_failure((1, 'bot', 'bot'), 'scheduled', 'network_error')
            with log._connect() as conn:
                self.assertEqual(conn.execute('SELECT error_code FROM notification_failures').fetchone()[0], 'network_error')


if __name__ == '__main__':
    unittest.main()
