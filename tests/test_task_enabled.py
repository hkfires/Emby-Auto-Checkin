import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import utils.config as config_module
import utils.log as task_log


class TaskEnabledTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(config_module, 'DATA_DIR', tmp))
        self.stack.enter_context(patch.object(config_module, 'CONFIG_FILE', str(Path(tmp) / 'config.json')))
        self.stack.enter_context(patch.object(task_log, 'DATA_DIR', tmp))
        self.stack.enter_context(patch.object(task_log, 'DB_FILE', str(Path(tmp) / 'log.db')))
        self.stack.enter_context(patch.object(task_log, '_db_initialized', False))
        self.bot_task = {'user_telegram_id': 1, 'bot_username': 'bot', 'selected_time_slot_id': 1}
        self.chat_task = {'user_telegram_id': 1, 'target_chat_id': -100, 'message_content': 'hello', 'selected_time_slot_id': 1}
        self.config = {
            'api_id': '1', 'api_hash': 'hash', 'scheduler_enabled': True,
            'users': [{'telegram_id': 1, 'nickname': 'User', 'session_name': 'session', 'status': 'logged_in'}],
            'bots': [{'bot_username': 'bot', 'strategy': 'checkin_text'}],
            'chats': [{'chat_id': -100, 'chat_title': 'Chat', 'strategy_identifier': 'send_custom_message'}],
            'checkin_tasks': [self.bot_task, self.chat_task],
            'scheduler_time_slots': [config_module._get_default_time_slot()],
        }
        config_module.save_config(self.config)
        import utils.scheduler_api as scheduler_module
        import webapp.api as api_module
        import webapp.views as views_module
        from webapp import create_app
        self.scheduler_module = scheduler_module
        self.api_module = api_module
        self.views_module = views_module
        self.app = create_app()
        self.app.config.update(TESTING=True, LOGIN_DISABLED=True)
        self.client = self.app.test_client()
        self.stack.enter_context(patch.object(api_module, 'current_user', Mock(is_authenticated=True)))
        self.notify = self.stack.enter_context(patch.object(api_module, 'notify_scheduler_to_reconcile'))
        self.execute = self.stack.enter_context(patch.object(api_module, 'execute_action', new_callable=AsyncMock,
                                                          return_value={'success': True, 'message': 'ok'}))
        self.scheduled_execute = self.stack.enter_context(patch.object(scheduler_module, 'execute_action', new_callable=AsyncMock,
                                                                    return_value={'success': True, 'message': 'ok'}))
        self.stack.enter_context(patch.object(views_module, 'get_scheduler_task_schedules',
                                             return_value={'available': False, 'tasks': []}))

    def set_enabled(self, enabled, tasks=None):
        if tasks is None:
            tasks = [{'user_telegram_id': 1, 'target_type': 'bot', 'identifier': 'bot'}]
        return self.client.post('/api/tasks/set_enabled', json={'enabled': enabled, 'tasks': tasks})

    def test_legacy_defaults_and_toggle_preserve_config_across_reload(self):
        self.assertTrue(all(task['enabled'] for task in config_module.load_config()['checkin_tasks']))
        self.assertEqual(self.set_enabled(False).status_code, 200)
        loaded = config_module.load_config()['checkin_tasks']
        self.assertFalse(loaded[0]['enabled'])
        self.assertTrue(loaded[1]['enabled'])
        self.assertEqual(loaded[0]['selected_time_slot_id'], 1)
        self.assertEqual(self.set_enabled(True).status_code, 200)
        self.assertTrue(config_module.load_config()['checkin_tasks'][0]['enabled'])
        self.notify.assert_called()

    def test_batch_updates_bots_and_chats_and_deduplicates(self):
        tasks = [
            {'user_telegram_id': '1', 'target_type': 'bot', 'identifier': 'bot'},
            {'user_telegram_id': 1, 'target_type': 'chat', 'identifier': '-100'},
        ]
        response = self.set_enabled(False, tasks + tasks)
        self.assertEqual(response.get_json()['updated_count'], 2)
        loaded = config_module.load_config()['checkin_tasks']
        self.assertTrue(all(not task['enabled'] for task in loaded))
        self.assertEqual(loaded[1]['message_content'], 'hello')

    def test_invalid_payloads_do_not_update_configuration(self):
        for payload in [None, [], {}, {'enabled': 'false', 'tasks': []},
                        {'enabled': False, 'tasks': []}, {'enabled': False, 'tasks': [None]},
                        {'enabled': False, 'tasks': [{'user_telegram_id': 'bad', 'target_type': 'bot', 'identifier': 'bot'}]},
                        {'enabled': False, 'tasks': [{'user_telegram_id': 1, 'target_type': 'chat', 'identifier': 'bad'}]}]:
            with self.subTest(payload=payload):
                response = self.client.post('/api/tasks/set_enabled', json=payload)
                self.assertEqual(response.status_code, 400)
        self.assertTrue(config_module.load_config()['checkin_tasks'][0]['enabled'])
        response = self.set_enabled(False, [{'user_telegram_id': 2, 'target_type': 'bot', 'identifier': 'bot'}])
        self.assertEqual(response.status_code, 404)
        self.notify.assert_not_called()

    def test_endpoint_requires_authentication(self):
        with patch.object(self.api_module, 'current_user', Mock(is_authenticated=False)):
            self.assertEqual(self.set_enabled(False).status_code, 401)
        self.assertTrue(config_module.load_config()['checkin_tasks'][0]['enabled'])

    def test_disabled_tasks_are_skipped_by_queue_and_daily_summary(self):
        self.set_enabled(False)
        cfg = config_module.load_config()
        batch, queued, skipped = task_log.queue_daily_tasks(cfg['checkin_tasks'])
        self.assertEqual((queued, skipped), (1, 1))
        summary = task_log.get_daily_task_counts(cfg)
        self.assertEqual(summary['total_count'], 2)
        self.assertEqual(summary['disabled_count'], 1)
        self.assertEqual(summary['pending_count'], 0)
        self.assertEqual(summary['queued_count'], 1)
        self.assertTrue(task_log.start_queued_task((1, 'chat', '-100'), batch)['started'])

    def test_disabling_cancels_waiting_work_and_clears_plan(self):
        identity = (1, 'bot', 'bot')
        batch, _, _ = task_log.queue_daily_tasks([self.bot_task])
        task_log.set_task_planned_time(identity, task_log.beijing_now() + timedelta(seconds=1))
        self.set_enabled(False)
        state = task_log.get_daily_task_states(config_module.load_config())[identity]
        self.assertEqual(state['status'], 'pending')
        self.assertIsNone(state['batch_id'])
        self.assertIsNone(state['planned_at'])
        self.assertEqual(state['attempt_count'], 0)
        self.assertFalse(task_log.start_queued_task(identity, batch)['started'])
        self.set_enabled(True)
        _, queued, _ = task_log.queue_daily_tasks([config_module.load_config()['checkin_tasks'][0]])
        self.assertEqual(queued, 1)

    def test_disabling_never_interrupts_running_or_resets_completed_history(self):
        identity = (1, 'bot', 'bot')
        claim = task_log.claim_daily_task(identity, 'scheduled')
        self.set_enabled(False)
        state = task_log.get_daily_task_states(config_module.load_config())[identity]
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['run_token'], claim['token'])
        self.assertTrue(task_log.complete_daily_task(identity, claim['token'], True, 'ok'))
        self.set_enabled(True)
        self.set_enabled(False)
        state = task_log.get_daily_task_states(config_module.load_config())[identity]
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['attempt_count'], 1)
        self.assertEqual(state['success'], 1)
        self.set_enabled(True)
        _, queued, skipped = task_log.queue_daily_tasks([self.bot_task])
        self.assertEqual((queued, skipped), (0, 1))

    async def test_scheduled_execution_checks_fresh_config_not_persisted_args(self):
        self.set_enabled(False)
        await self.scheduler_module.run_checkin_task(1, 'bot', 'bot', self.bot_task)
        self.scheduled_execute.assert_not_awaited()
        self.assertEqual(task_log.get_daily_task_counts(config_module.load_config())['executed_count'], 0)
        self.set_enabled(True)
        await self.scheduler_module.run_checkin_task(1, 'bot', 'bot', {'enabled': False})
        self.scheduled_execute.assert_awaited_once()

    async def test_quick_batch_rechecks_tasks_disabled_while_waiting(self):
        batch, _, _ = task_log.queue_daily_tasks(self.config['checkin_tasks'])

        async def execute_first(**kwargs):
            cfg = config_module.load_config()
            cfg['checkin_tasks'][1]['enabled'] = False
            config_module.save_config(cfg)
            return {'success': True, 'message': 'ok'}

        self.execute.side_effect = execute_first
        result = await self.api_module.execute_all_tasks_internal(task_entries=self.config['checkin_tasks'], batch_id=batch)
        self.assertEqual(len(result['all_tasks_results']), 1)
        self.execute.assert_awaited_once()
        states = task_log.get_daily_task_states(config_module.load_config())
        self.assertEqual(states[(1, 'chat', '-100')]['status'], 'pending')
        self.assertEqual(states[(1, 'chat', '-100')]['attempt_count'], 0)

    def test_manual_and_execute_all_do_not_execute_disabled_tasks(self):
        self.set_enabled(False)
        response = self.client.post('/api/checkin/manual', data={
            'user_telegram_id': '1', 'target_type': 'bot', 'identifier': 'bot'
        })
        self.assertEqual(response.status_code, 409)
        self.execute.assert_not_awaited()
        self.set_enabled(False, [{'user_telegram_id': 1, 'target_type': 'chat', 'identifier': '-100'}])
        with patch.object(self.api_module.threading, 'Thread') as thread:
            response = self.client.post('/api/tasks/execute_all')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['queued_count'], 0)
        self.assertEqual(response.get_json()['skipped_count'], 2)
        thread.assert_not_called()

    def test_scheduler_removes_disabled_jobs_and_recreates_on_enable(self):
        job = SimpleNamespace(id='checkin_job_1_bot', args=[1, 'bot', 'bot', self.bot_task], name='Bot')
        scheduler = Mock()
        scheduler.get_jobs.return_value = [job]
        scheduler.get_job.return_value = job
        self.set_enabled(False)
        with patch.object(self.scheduler_module, 'scheduler', scheduler), \
             patch.object(self.scheduler_module, '_record_job_schedule'):
            self.scheduler_module.reconcile_tasks()
            scheduler.remove_job.assert_called_once_with(job.id)
            self.assertEqual(scheduler.add_job.call_count, 1)
            self.assertEqual(scheduler.add_job.call_args.kwargs['id'], 'checkin_job_1_-100')
            self.set_enabled(True)
            scheduler.reset_mock()
            scheduler.get_jobs.return_value = []
            self.scheduler_module.reconcile_tasks()
            self.assertEqual(scheduler.add_job.call_count, 2)

    def test_force_and_daily_reschedule_cannot_revive_disabled_job(self):
        job = SimpleNamespace(id='checkin_job_1_bot', args=[1, 'bot', 'bot', self.bot_task], name='Bot')
        scheduler = Mock()
        scheduler.get_jobs.return_value = [job]
        scheduler.get_job.return_value = job
        self.set_enabled(False)
        with patch.object(self.scheduler_module, 'scheduler', scheduler):
            result = self.scheduler_module.reconcile_tasks(force_reschedule_ids=['1_bot'])
            self.assertEqual(len(result['failed']), 1)
            scheduler.add_job.assert_not_called()
            scheduler.remove_job.assert_called_once_with(job.id)
            scheduler.reset_mock()
            with patch.object(self.scheduler_module, 'reconcile_tasks'), \
                 patch.object(self.scheduler_module, 'log_scheduled_jobs'):
                self.scheduler_module.daily_reschedule_tasks()
            scheduler.reschedule_job.assert_not_called()
            scheduler.remove_job.assert_called_once_with(job.id)

    async def test_disable_cancels_previous_day_batch_even_after_reenable(self):
        previous_date = (task_log.beijing_now() - timedelta(days=1)).date().isoformat()
        batch, queued, _ = task_log.queue_daily_tasks([self.bot_task], state_date=previous_date)
        self.assertEqual(queued, 1)
        self.set_enabled(False)
        self.set_enabled(True)
        result = await self.api_module.execute_all_tasks_internal(
            task_entries=[self.bot_task], batch_id=batch, state_date=previous_date
        )
        self.assertEqual(result['all_tasks_results'], [])
        self.execute.assert_not_awaited()
        state = task_log.get_daily_task_states(config_module.load_config(), previous_date)[(1, 'bot', 'bot')]
        self.assertEqual(state['status'], 'pending')
        self.assertEqual(state['attempt_count'], 0)

    async def test_manual_refreshes_enabled_status_at_start_boundary(self):
        original_load = config_module.load_config
        calls = 0

        def load_then_disable():
            nonlocal calls
            snapshot = original_load()
            calls += 1
            if calls == 1:
                cfg = original_load()
                cfg['checkin_tasks'][0]['enabled'] = False
                config_module.save_config(cfg)
            return snapshot

        with patch.object(self.api_module, 'load_config', side_effect=load_then_disable), \
             self.app.test_request_context('/api/checkin/manual', method='POST', data={
                 'user_telegram_id': '1', 'target_type': 'bot', 'identifier': 'bot'
             }):
            response, status = await self.api_module.manual_action()
        self.assertEqual(status, 409)
        self.execute.assert_not_awaited()
        self.assertIn('已禁用', response.get_json()['message'])

    def test_disable_and_scheduled_start_are_serialized(self):
        before_claim = threading.Event()
        release_claim = threading.Event()
        disable_attempted = threading.Event()
        real_claim = task_log.claim_daily_task

        def paused_claim(*args, **kwargs):
            before_claim.set()
            if not release_claim.wait(5):
                raise AssertionError('claim barrier timed out')
            return real_claim(*args, **kwargs)

        def disable():
            disable_attempted.set()
            return self.set_enabled(False)

        with patch.object(self.scheduler_module, 'claim_daily_task', side_effect=paused_claim), \
             ThreadPoolExecutor(max_workers=2) as pool:
            started = pool.submit(self.scheduler_module.run_checkin_task_sync, 1, 'bot', 'bot', self.bot_task)
            try:
                self.assertTrue(before_claim.wait(5))
                disabled = pool.submit(disable)
                self.assertTrue(disable_attempted.wait(5))
                with self.assertRaises(TimeoutError):
                    disabled.result(timeout=0.1)
            finally:
                release_claim.set()
            self.assertTrue(started.result(timeout=5)['success'])
            self.assertEqual(disabled.result(timeout=5).status_code, 200)
        self.assertFalse(config_module.load_config()['checkin_tasks'][0]['enabled'])
        self.scheduled_execute.assert_awaited_once()

    def test_reenable_cannot_overtake_stale_disable_reconciliation(self):
        self.set_enabled(False)
        snapshot_read = threading.Event()
        release_reconcile = threading.Event()
        enable_attempted = threading.Event()
        real_load = config_module.load_config
        bot_job = SimpleNamespace(id='checkin_job_1_bot', args=[1, 'bot', 'bot', self.bot_task], name='Bot')
        chat_job = SimpleNamespace(id='checkin_job_1_-100', args=[1, 'chat', -100, self.chat_task], name='Chat')
        jobs = {job.id: job for job in [bot_job, chat_job]}
        scheduler = Mock()
        scheduler.get_jobs.side_effect = lambda: list(jobs.values())
        scheduler.get_job.side_effect = jobs.get
        scheduler.remove_job.side_effect = jobs.pop

        def add_job(func, **kwargs):
            jobs[kwargs['id']] = SimpleNamespace(id=kwargs['id'], args=kwargs['args'], name=kwargs['name'])

        scheduler.add_job.side_effect = add_job

        def paused_load():
            snapshot = real_load()
            snapshot_read.set()
            if not release_reconcile.wait(5):
                raise AssertionError('reconciliation barrier timed out')
            return snapshot

        def enable():
            enable_attempted.set()
            return self.set_enabled(True)

        with patch.object(self.scheduler_module, 'scheduler', scheduler), \
             patch.object(self.scheduler_module, '_record_job_schedule'), \
             patch.object(self.scheduler_module, 'load_config', side_effect=paused_load), \
             ThreadPoolExecutor(max_workers=2) as pool:
            reconciling = pool.submit(self.scheduler_module.reconcile_tasks)
            try:
                self.assertTrue(snapshot_read.wait(5))
                enabling = pool.submit(enable)
                self.assertTrue(enable_attempted.wait(5))
                with self.assertRaises(TimeoutError):
                    enabling.result(timeout=0.1)
            finally:
                release_reconcile.set()
            reconciling.result(timeout=5)
            self.assertEqual(enabling.result(timeout=5).status_code, 200)
            self.scheduler_module.reconcile_tasks()
        self.assertTrue(config_module.load_config()['checkin_tasks'][0]['enabled'])
        self.assertIn(bot_job.id, jobs)

    def test_task_page_displays_disabled_status_and_toggle(self):
        self.set_enabled(False)
        response = self.client.get('/tasks')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('已禁用', html)
        self.assertIn('启用', html)
        self.assertIn('批量禁用', html)
        self.assertIn('批量启用', html)
        self.assertIn('data-enabled="true"', html)


if __name__ == '__main__':
    unittest.main()
