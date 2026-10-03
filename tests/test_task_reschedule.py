import importlib
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import utils.config as config_module
import utils.log as task_log
import utils.scheduler_api as scheduler_module


class TaskRescheduleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(config_module, 'DATA_DIR', tmp))
        self.stack.enter_context(patch.object(config_module, 'CONFIG_FILE', str(Path(tmp) / 'config.json')))
        self.stack.enter_context(patch.object(task_log, 'DATA_DIR', tmp))
        self.stack.enter_context(patch.object(task_log, 'DB_FILE', str(Path(tmp) / 'log.db')))
        self.stack.enter_context(patch.object(task_log, '_db_initialized', False))
        self.now = datetime(2026, 8, 8, 12, 0, 0, 500000, tzinfo=task_log.BEIJING_TZ)
        self.stack.enter_context(patch.object(task_log, 'beijing_now', return_value=self.now))
        self.stack.enter_context(patch.object(scheduler_module, 'beijing_now', return_value=self.now, create=True))
        scheduler_datetime = self.stack.enter_context(patch('apscheduler.schedulers.base.datetime', wraps=datetime))
        scheduler_datetime.now.side_effect = lambda tz=None: self.now.astimezone(tz) if tz else self.now.replace(tzinfo=None)
        self.tasks = [
            {'user_telegram_id': 1, 'bot_username': 'test_bot', 'selected_time_slot_id': 1},
            {'user_telegram_id': 1, 'target_chat_id': -100, 'selected_time_slot_id': 1},
            {'user_telegram_id': 2, 'bot_username': 'other_bot', 'selected_time_slot_id': 1},
        ]
        self.ids = ['1_test_bot', '1_-100', '2_other_bot']
        self.config = {
            'scheduler_enabled': True,
            'users': [
                {'telegram_id': 1, 'nickname': 'One', 'status': 'logged_in'},
                {'telegram_id': 2, 'nickname': 'Two', 'status': 'logged_in'},
            ],
            'chats': [{'chat_id': -100, 'chat_title': 'Chat'}],
            'checkin_tasks': self.tasks,
            'scheduler_time_slots': [{
                'id': 1, 'start_hour': 8, 'start_minute': 0,
                'end_hour': 22, 'end_minute': 0,
            }],
        }
        config_module.save_config(self.config)
        self.scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
        # A paused in-memory scheduler exercises real replacement and next-run calculation.
        self.scheduler.start(paused=True)
        self.addCleanup(self.scheduler.shutdown)
        self.stack.enter_context(patch.object(scheduler_module, 'scheduler', self.scheduler))

    def seed_jobs(self, ids=None):
        for task_id, task in zip(self.ids, self.tasks):
            if ids is not None and task_id not in ids:
                continue
            target_type = 'bot' if task.get('bot_username') else 'chat'
            identifier = task.get('bot_username') or task.get('target_chat_id')
            self.scheduler.add_job(
                scheduler_module.run_checkin_task_sync,
                trigger=CronTrigger(hour=20, timezone='Asia/Shanghai'),
                args=[task['user_telegram_id'], target_type, identifier, task],
                id=f'checkin_job_{task_id}', name=task_id,
            )

    def assert_scheduled_today(self, ids):
        for task_id in ids:
            job = self.scheduler.get_job(f'checkin_job_{task_id}')
            self.assertIsNotNone(job)
            planned_at = job.next_run_time
            self.assertEqual(planned_at.date(), self.now.date())
            self.assertGreater(planned_at, self.now)
            self.assertLessEqual(planned_at.hour, 22)
            identity = scheduler_module._job_identity(job)
            self.assertEqual(task_log.get_planned_times_for_date('2026-08-08')[identity], planned_at.isoformat())

    def test_single_and_multiple_missing_jobs_are_created(self):
        for ids in [self.ids[:1], self.ids[:2], self.ids]:
            with self.subTest(ids=ids):
                self.scheduler.remove_all_jobs()
                result = scheduler_module.reconcile_tasks(force_reschedule_ids=ids)
                self.assertEqual(result, {'rescheduled': ids, 'failed': [], 'not_found': []})
                self.assert_scheduled_today(ids)
                self.assertEqual(len(self.scheduler.get_jobs()), len(ids))

    def test_existing_jobs_are_all_rescheduled_into_remaining_today_window(self):
        self.seed_jobs()
        with patch.object(scheduler_module.random, 'randint', side_effect=lambda low, high: low):
            result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids[:2])
        self.assertEqual(result['rescheduled'], self.ids[:2])
        self.assert_scheduled_today(self.ids[:2])
        self.assertEqual(len(self.scheduler.get_jobs()), 3)
        self.assertEqual(self.scheduler.get_job('checkin_job_2_other_bot').name, '2_other_bot')

    def test_mixed_existing_and_missing_jobs_and_duplicate_ids(self):
        self.seed_jobs(self.ids[:1])
        result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids + self.ids[:1])
        self.assertEqual(result['rescheduled'], self.ids)
        self.assert_scheduled_today(self.ids)

    def test_expired_window_reports_failure_and_keeps_existing_jobs(self):
        self.seed_jobs()
        old_times = {job.id: job.next_run_time for job in self.scheduler.get_jobs()}
        self.config['scheduler_time_slots'][0]['end_hour'] = 10
        config_module.save_config(self.config)
        result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids)
        self.assertEqual(result['rescheduled'], [])
        self.assertEqual(len(result['failed']), 3)
        self.assertIn('剩余', result['failed'][0]['error'])
        self.assertEqual({job.id: job.next_run_time for job in self.scheduler.get_jobs()}, old_times)

    def test_failure_for_one_task_does_not_stop_other_tasks(self):
        self.config['checkin_tasks'][0]['enabled'] = False
        config_module.save_config(self.config)
        result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids + ['9_missing'])
        self.assertEqual(result['rescheduled'], self.ids[1:])
        self.assertEqual(result['failed'][0]['task_id'], self.ids[0])
        self.assertEqual(result['not_found'], ['9_missing'])
        self.assert_scheduled_today(self.ids[1:])
        self.assertIsNone(self.scheduler.get_job('checkin_job_1_test_bot'))

    def test_logged_out_user_is_not_scheduled(self):
        self.config['users'][0]['status'] = 'logged_out'
        config_module.save_config(self.config)
        result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids)
        self.assertEqual(result['rescheduled'], self.ids[2:])
        self.assertEqual(len(result['failed']), 2)

    def test_overnight_window_uses_only_remaining_segment_today(self):
        self.config['scheduler_time_slots'][0].update(start_hour=22, end_hour=2)
        config_module.save_config(self.config)
        with patch.object(scheduler_module.random, 'randint', side_effect=lambda low, high: low):
            result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids)
        self.assertEqual(result['rescheduled'], self.ids)
        for job in self.scheduler.get_jobs():
            self.assertEqual(job.next_run_time.date(), self.now.date())
            self.assertEqual(job.next_run_time.hour, 22)

    def test_clock_advancing_past_sample_preserves_existing_job(self):
        self.seed_jobs(self.ids[:1])
        old_job = self.scheduler.get_job('checkin_job_1_test_bot')
        later = self.now.replace(second=2)
        with patch.object(scheduler_module, 'beijing_now', side_effect=[self.now, later]), \
             patch.object(scheduler_module.random, 'randint', side_effect=lambda low, high: low):
            result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids[:1])
        self.assertEqual(result['rescheduled'], [])
        self.assertIn('时间已过', result['failed'][0]['error'])
        self.assertIs(self.scheduler.get_job(old_job.id), old_job)

    def test_midnight_clock_rollover_preserves_existing_job(self):
        self.seed_jobs(self.ids[:1])
        old_job = self.scheduler.get_job('checkin_job_1_test_bot')
        self.config['scheduler_time_slots'][0].update(start_hour=22, end_hour=2)
        config_module.save_config(self.config)
        sampled_at = self.now.replace(hour=23, minute=59, second=58)
        rechecked_at = sampled_at.replace(day=9, hour=0, minute=0, second=0)
        with patch.object(scheduler_module, 'beijing_now', side_effect=[sampled_at, rechecked_at]), \
             patch.object(scheduler_module.random, 'randint', side_effect=lambda low, high: low):
            result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids[:1])
        self.assertEqual(result['rescheduled'], [])
        self.assertEqual(len(result['failed']), 1)
        self.assertIs(self.scheduler.get_job(old_job.id), old_job)

    def test_late_add_uses_explicit_today_run_time(self):
        later = self.now.replace(hour=13)
        with patch('apscheduler.schedulers.base.datetime', wraps=datetime) as later_clock, \
             patch.object(scheduler_module.random, 'randint', side_effect=lambda low, high: low):
            later_clock.now.side_effect = lambda tz=None: later.astimezone(tz) if tz else later.replace(tzinfo=None)
            result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids[:1])
        self.assertEqual(result['rescheduled'], self.ids[:1])
        self.assert_scheduled_today(self.ids[:1])
        self.assertEqual(self.scheduler.get_job('checkin_job_1_test_bot').next_run_time.second, 1)

    def test_job_storage_error_does_not_cancel_other_jobs(self):
        self.seed_jobs()
        old_job = self.scheduler.get_job('checkin_job_1_test_bot')
        real_add = self.scheduler.add_job

        def fail_first(func, **kwargs):
            if kwargs['id'] == old_job.id:
                raise RuntimeError('storage unavailable')
            return real_add(func, **kwargs)

        with patch.object(self.scheduler, 'add_job', side_effect=fail_first):
            result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids)
        self.assertEqual(result['rescheduled'], self.ids[1:])
        self.assertEqual(result['failed'][0]['error'], 'storage unavailable')
        self.assertIs(self.scheduler.get_job(old_job.id), old_job)
        self.assert_scheduled_today(self.ids[1:])

    def test_remaining_window_boundaries_and_unassigned_slots(self):
        for now, start_hour, end_hour, expected_hour in [
            (self.now.replace(hour=1), 22, 2, 1),
            (self.now.replace(hour=23, minute=59, second=58), 22, 2, 23),
            (self.now.replace(hour=7), 8, 22, 8),
        ]:
            with self.subTest(now=now):
                self.config['scheduler_time_slots'][0].update(start_hour=start_hour, end_hour=end_hour)
                with patch.object(scheduler_module, 'beijing_now', return_value=now), \
                     patch.object(scheduler_module.random, 'choice', side_effect=lambda ranges: ranges[-1]), \
                     patch.object(scheduler_module.random, 'randint', side_effect=lambda low, high: low):
                    trigger = scheduler_module._get_new_cron_trigger(self.tasks[0], self.config, remaining_today=True)
                planned_at = trigger.get_next_fire_time(None, now)
                self.assertEqual(planned_at.date(), now.date())
                self.assertGreater(planned_at, now)
                self.assertEqual(planned_at.hour, expected_hour)
        self.config['scheduler_time_slots'][0].update(start_hour=8, end_hour=10)
        self.config['scheduler_time_slots'].append({'id': 2, 'start_hour': 20, 'end_hour': 22})
        # A configured, expired slot must not silently change to a different slot.
        self.assertIsNone(scheduler_module._get_new_cron_trigger(self.tasks[0], self.config, remaining_today=True))
        with patch.object(scheduler_module.random, 'randint', side_effect=lambda low, high: low):
            trigger = scheduler_module._get_new_cron_trigger({}, self.config, remaining_today=True)
        self.assertEqual(trigger.get_next_fire_time(None, self.now).hour, 20)
        with patch.object(scheduler_module, 'beijing_now', return_value=self.now.replace(hour=23, minute=59, second=59)):
            self.assertIsNone(scheduler_module._get_new_cron_trigger({}, self.config, remaining_today=True))

    def test_disabled_scheduler_reports_failure_for_each_selected_task(self):
        self.config['scheduler_enabled'] = False
        config_module.save_config(self.config)
        result = scheduler_module.reconcile_tasks(force_reschedule_ids=self.ids)
        self.assertEqual(result['rescheduled'], [])
        self.assertEqual([item['task_id'] for item in result['failed']], self.ids)

    def test_web_endpoint_validates_ids_and_forwards_batch_results(self):
        import webapp.api as api_module
        from webapp import create_app
        app = create_app()
        app.config.update(TESTING=True, LOGIN_DISABLED=True)
        client = app.test_client()
        with patch.object(api_module, 'current_user', Mock(is_authenticated=True)), \
             patch.object(api_module.httpx, 'Client') as http_client:
            for payload in [[], {}, {'task_ids': []}, {'task_ids': [1]}, {'task_ids': [{}]}, {'task_ids': ['']}]:
                with self.subTest(payload=payload):
                    self.assertEqual(client.post('/api/scheduler/reconcile', json=payload).status_code, 400)
            http_client.assert_not_called()
            expected = {
                'success': False, 'message': '部分任务调度失败',
                'result': {'rescheduled': self.ids[1:], 'failed': [{'task_id': self.ids[0], 'error': '禁用'}]},
            }
            response_mock = http_client.return_value.__enter__.return_value.post.return_value
            response_mock.status_code = 200
            response_mock.json.return_value = expected
            response = client.post('/api/scheduler/reconcile', json={'task_ids': self.ids})
            self.assertEqual(response.get_json(), expected)
            self.assertEqual(http_client.return_value.__enter__.return_value.post.call_args.kwargs['json'],
                             {'task_ids': self.ids})

    def test_scheduler_endpoint_reports_partial_and_total_failures(self):
        # Import without starting the service thread or touching production job storage.
        with patch('threading.Thread'):
            service = importlib.import_module('run_scheduler')
        client = service.app.test_client()
        self.config['checkin_tasks'][0]['enabled'] = False
        config_module.save_config(self.config)
        response = client.post('/reconcile', json={'task_ids': self.ids})
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['success'])
        self.assertIn('2', data['message'])
        self.assertEqual(data['result']['rescheduled'], self.ids[1:])
        response = client.post('/reconcile', json={'task_ids': ['9_missing']})
        self.assertFalse(response.get_json()['success'])
        for task_ids in [[], '1_test_bot', [None], [1], [{}], ['']]:
            with self.subTest(task_ids=task_ids):
                self.assertEqual(client.post('/reconcile', json={'task_ids': task_ids}).status_code, 400)
        self.assertEqual(client.post('/reconcile', json=[]).status_code, 400)
        self.assertTrue(client.get('/reconcile').get_json()['success'])


if __name__ == '__main__':
    unittest.main()
