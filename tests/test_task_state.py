import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch
import uuid

import utils.log as task_log


class TaskStateTestCase(unittest.TestCase):
    def setUp(self):
        self.db_uri = f"file:taskstate_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(self.db_uri, uri=True, check_same_thread=False)
        self.old_db_file = task_log.DB_FILE
        self.old_data_dir = task_log.DATA_DIR
        self.old_initialized = task_log._db_initialized
        task_log.DB_FILE = self.db_uri
        task_log.DATA_DIR = "."
        task_log._db_initialized = False
        task_log.init_log_db()

    def tearDown(self):
        task_log.DB_FILE = self.old_db_file
        task_log.DATA_DIR = self.old_data_dir
        task_log._db_initialized = self.old_initialized
        self.keeper.close()

    @staticmethod
    def identity():
        return task_log.task_identity(1001, "bot", "checkin_bot")

    def test_legacy_schema_is_migrated_without_losing_records(self):
        self.keeper.execute("DROP TABLE daily_task_state")
        self.keeper.execute("DROP TABLE checkin_records")
        self.keeper.execute(
            """
            CREATE TABLE checkin_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                checkin_type TEXT,
                user_nickname TEXT,
                target_type TEXT,
                target_name TEXT,
                success INTEGER,
                message TEXT
            )
            """
        )
        self.keeper.execute(
            """
            INSERT INTO checkin_records
            (timestamp, checkin_type, user_nickname, target_type, target_name, success, message)
            VALUES ('2026-08-08T08:00:00', '旧任务', 'Alice', 'bot', 'checkin_bot', 1, 'ok')
            """
        )
        self.keeper.commit()
        task_log._db_initialized = False
        task_log.init_log_db()

        with sqlite3.connect(self.db_uri, uri=True) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(checkin_records)")}
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue({"user_telegram_id", "target_identifier", "execution_source"}.issubset(columns))
            self.assertIn("daily_task_state", tables)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM checkin_records").fetchone()[0], 1)

    def test_concurrent_database_claim_only_allows_one_non_manual_execution(self):
        identity = self.identity()

        def claim():
            # Bypass the process-local lock to exercise SQLite arbitration directly.
            return task_log._claim_daily_task(identity, "quick")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: claim(), range(8)))

        self.assertEqual(sum(result["claimed"] for result in results), 1)
        token = next(result["token"] for result in results if result["claimed"])
        self.assertTrue(task_log.complete_daily_task(identity, token, False, "failed"))
        self.assertFalse(task_log.claim_daily_task(identity, "scheduled")["claimed"])

    def test_queue_database_error_is_propagated(self):
        task = {"user_telegram_id": 1001, "bot_username": "checkin_bot"}
        with patch.object(task_log, "recover_stale_task_states"):
            with patch.object(
                task_log,
                "_connect",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    task_log.queue_daily_tasks([task])

    def test_manual_retry_is_allowed_after_failure_but_not_while_running(self):
        identity = self.identity()
        first = task_log.claim_daily_task(identity, "manual", allow_retry=True)
        self.assertTrue(first["claimed"])
        self.assertFalse(task_log.claim_daily_task(identity, "manual", allow_retry=True)["claimed"])
        task_log.complete_daily_task(identity, first["token"], False, "network error")

        retry = task_log.claim_daily_task(identity, "manual", allow_retry=True)
        self.assertTrue(retry["claimed"])
        self.assertTrue(task_log.complete_daily_task(identity, retry["token"], True, "ok"))

        states = task_log.get_daily_task_states(
            {"checkin_tasks": [{"user_telegram_id": 1001, "bot_username": "checkin_bot"}]}
        )
        state = states[identity]
        self.assertEqual(state["status"], task_log.TASK_STATE_COMPLETED)
        self.assertEqual(state["attempt_count"], 2)
        self.assertEqual(state["success"], 1)

    def test_quick_queue_is_idempotent_and_completed_tasks_are_skipped(self):
        task = {"user_telegram_id": 1001, "bot_username": "checkin_bot"}
        batch_id, queued, skipped = task_log.queue_daily_tasks([task])
        self.assertEqual((queued, skipped), (1, 0))
        second_batch, queued_again, skipped_again = task_log.queue_daily_tasks([task])
        self.assertNotEqual(batch_id, second_batch)
        self.assertEqual((queued_again, skipped_again), (0, 1))

        started = task_log.start_queued_task(self.identity(), batch_id)
        self.assertTrue(started["started"])
        task_log.complete_daily_task(self.identity(), started["token"], True, "ok")
        _, queued_after, skipped_after = task_log.queue_daily_tasks([task])
        self.assertEqual((queued_after, skipped_after), (0, 1))

    def test_batch_heartbeat_keeps_later_tasks_queued(self):
        state_date = task_log.beijing_today_str()
        task = {"user_telegram_id": 1001, "bot_username": "checkin_bot"}
        batch_id, queued, skipped = task_log.queue_daily_tasks(
            [task], state_date=state_date
        )
        self.assertEqual((queued, skipped), (1, 0))

        old_queued_at = (
            task_log.beijing_now() - timedelta(minutes=11)
        ).replace(tzinfo=None).isoformat()
        self.keeper.execute(
            """
            UPDATE daily_task_state SET queued_at = ?
            WHERE state_date = ? AND user_telegram_id = 1001
              AND target_type = 'bot' AND target_identifier = 'checkin_bot'
            """,
            (old_queued_at, state_date),
        )
        self.keeper.commit()

        task_log.refresh_queued_batch(batch_id, state_date)
        task_log.recover_stale_task_states(state_date)
        states = task_log.get_daily_task_states(
            {"checkin_tasks": [task]}, state_date
        )
        self.assertEqual(states[self.identity()]["status"], task_log.TASK_STATE_QUEUED)

    def test_legacy_log_is_backfilled_only_when_match_is_unique(self):
        task_log.save_daily_checkin_log(
            {
                "timestamp": "2026-08-08T08:00:00",
                "checkin_type": "旧任务",
                "user_nickname": "Alice",
                "target_type": "bot",
                "target_name": "checkin_bot",
                "success": True,
                "message": "ok",
            }
        )
        config = {
            "users": [{"telegram_id": 1001, "nickname": "Alice"}],
            "bots": [{"bot_username": "checkin_bot"}],
            "chats": [],
            "checkin_tasks": [{"user_telegram_id": 1001, "bot_username": "checkin_bot"}],
        }
        task_log.backfill_legacy_task_states(config, "2026-08-08")
        states = task_log.get_daily_task_states(config, "2026-08-08")
        self.assertEqual(states[self.identity()]["status"], task_log.TASK_STATE_COMPLETED)

    def test_planned_time_is_persisted_by_beijing_date(self):
        planned_at = datetime.fromisoformat("2026-08-08T10:23:15+08:00")
        task_log.set_task_planned_time(self.identity(), planned_at)
        planned = task_log.get_planned_times_for_date("2026-08-08")
        self.assertEqual(planned[self.identity()], planned_at.isoformat())

    def test_stale_running_task_becomes_interrupted_and_can_be_retried(self):
        claim = task_log.claim_daily_task(self.identity(), "quick", state_date="2026-08-08")
        old_started_at = (task_log.beijing_now() - timedelta(minutes=11)).replace(tzinfo=None).isoformat()
        self.keeper.execute(
            """
            UPDATE daily_task_state SET started_at = ?
            WHERE state_date = '2026-08-08' AND user_telegram_id = 1001
              AND target_type = 'bot' AND target_identifier = 'checkin_bot'
            """,
            (old_started_at,),
        )
        self.keeper.commit()
        task_log.recover_stale_task_states("2026-08-08")
        config = {"checkin_tasks": [{"user_telegram_id": 1001, "bot_username": "checkin_bot"}]}
        self.assertEqual(
            task_log.get_daily_task_states(config, "2026-08-08")[self.identity()]["status"],
            task_log.TASK_STATE_INTERRUPTED,
        )
        self.assertTrue(task_log.claim_daily_task(self.identity(), "manual", allow_retry=True, state_date="2026-08-08")["claimed"])

    def test_daily_summary_counts_current_tasks_and_resets_on_new_date(self):
        second = task_log.task_identity(1001, "bot", "second_bot")
        third = task_log.task_identity(1001, "bot", "third_bot")
        config = {
            "checkin_tasks": [
                {"user_telegram_id": 1001, "bot_username": "checkin_bot"},
                {"user_telegram_id": 1001, "bot_username": "second_bot"},
                {"user_telegram_id": 1001, "bot_username": "third_bot"},
            ]
        }
        first_claim = task_log.claim_daily_task(self.identity(), "scheduled", state_date="2026-08-08")
        second_claim = task_log.claim_daily_task(second, "manual", state_date="2026-08-08")
        task_log.complete_daily_task(self.identity(), first_claim["token"], True, state_date="2026-08-08")
        task_log.complete_daily_task(second, second_claim["token"], False, state_date="2026-08-08")

        summary = task_log.get_daily_task_counts(config, "2026-08-08")
        self.assertEqual(summary["total_count"], 3)
        self.assertEqual(summary["executed_count"], 2)
        self.assertEqual(summary["pending_count"], 1)
        next_day = task_log.get_daily_task_counts(config, "2026-08-09")
        self.assertEqual(next_day["executed_count"], 0)
        self.assertEqual(next_day["pending_count"], 3)

if __name__ == "__main__":
    unittest.main()
