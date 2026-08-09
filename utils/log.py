import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "checkin_log.db")
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
STALE_TASK_STATE_SECONDS = 10 * 60

TASK_STATE_PENDING = "pending"
TASK_STATE_QUEUED = "queued"
TASK_STATE_RUNNING = "running"
TASK_STATE_COMPLETED = "completed"
TASK_STATE_INTERRUPTED = "interrupted"
TASK_EXECUTED_STATES = {TASK_STATE_COMPLETED, TASK_STATE_INTERRUPTED}
_db_initialized = False
_state_lock = threading.RLock()


def beijing_now():
    return datetime.now(BEIJING_TZ)


def beijing_today_str():
    return beijing_now().date().isoformat()


def _beijing_naive_iso(value=None):
    if value is None:
        value = beijing_now()
    if value.tzinfo is not None:
        value = value.astimezone(BEIJING_TZ).replace(tzinfo=None)
    return value.isoformat()


def task_identity(user_telegram_id, target_type, target_identifier):
    if user_telegram_id is None or target_type is None or target_identifier is None:
        return None
    try:
        user_id = int(user_telegram_id)
    except (TypeError, ValueError):
        return None
    target_type = str(target_type).strip().lower()
    if target_type not in {"bot", "chat"}:
        return None
    if target_type == "chat":
        try:
            target_identifier = str(int(target_identifier))
        except (TypeError, ValueError):
            return None
    else:
        target_identifier = str(target_identifier).strip()
        if not target_identifier:
            return None
    return user_id, target_type, target_identifier


def task_identity_from_config(task_entry):
    if not isinstance(task_entry, dict):
        return None
    if task_entry.get("bot_username"):
        return task_identity(
            task_entry.get("user_telegram_id"), "bot", task_entry.get("bot_username")
        )
    if task_entry.get("target_chat_id") is not None:
        return task_identity(
            task_entry.get("user_telegram_id"), "chat", task_entry.get("target_chat_id")
        )
    return None


def task_identity_key(identity):
    return tuple(identity) if identity else None


@contextmanager
def _connect():
    if not DB_FILE.startswith("file:"):
        os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10,
        uri=DB_FILE.startswith("file:"),
    )
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(cursor, table_name):
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()}


def init_log_db():
    global _db_initialized
    if _db_initialized:
        return

    try:
        with _connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS checkin_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    checkin_type TEXT,
                    user_nickname TEXT,
                    target_type TEXT,
                    target_name TEXT,
                    success INTEGER,
                    message TEXT,
                    user_telegram_id INTEGER,
                    target_identifier TEXT,
                    execution_source TEXT
                )
                """
            )

            columns = _table_columns(cursor, "checkin_records")
            migrations = {
                "user_telegram_id": "INTEGER",
                "target_identifier": "TEXT",
                "execution_source": "TEXT",
            }
            for column_name, column_type in migrations.items():
                if column_name not in columns:
                    cursor.execute(
                        f"ALTER TABLE checkin_records ADD COLUMN {column_name} {column_type}"
                    )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_task_state (
                    state_date TEXT NOT NULL,
                    user_telegram_id INTEGER NOT NULL,
                    target_type TEXT NOT NULL,
                    target_identifier TEXT NOT NULL,
                    planned_at TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    batch_id TEXT,
                    run_token TEXT,
                    source TEXT,
                    queued_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    success INTEGER,
                    message TEXT,
                    PRIMARY KEY (
                        state_date,
                        user_telegram_id,
                        target_type,
                        target_identifier
                    )
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_checkin_records_date_task
                ON checkin_records (timestamp, user_telegram_id, target_type, target_identifier)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_daily_task_state_date_status
                ON daily_task_state (state_date, status)
                """
            )
            conn.commit()
        _db_initialized = True
        logger.info(f"数据库 {DB_FILE} 初始化成功。")
    except sqlite3.Error as exc:
        logger.error(f"初始化数据库 {DB_FILE} 时出错: {exc}")


def _ensure_initialized():
    if not _db_initialized:
        init_log_db()


def _date_bounds(target_date_str):
    try:
        datetime.strptime(target_date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return target_date_str + "T00:00:00", target_date_str + "T23:59:59.999999"


def load_checkin_log_by_date(target_date_str):
    bounds = _date_bounds(target_date_str)
    if not bounds:
        logger.error(f"无效的日期格式: {target_date_str}. 需要 YYYY-MM-DD 格式。")
        return []

    _ensure_initialized()
    logs_for_date = []
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, timestamp, checkin_type, user_nickname, target_type,
                       target_name, success, message, user_telegram_id,
                       target_identifier, execution_source
                FROM checkin_records
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp DESC
                """,
                bounds,
            ).fetchall()
            logs_for_date = [dict(row) for row in rows]
    except sqlite3.Error as exc:
        logger.error(f"从 {DB_FILE} 加载日期 {target_date_str} 的签到日志时出错: {exc}")
    return logs_for_date


def save_daily_checkin_log(log_entry):
    _ensure_initialized()
    try:
        timestamp = log_entry.get("timestamp") or _beijing_naive_iso()
        success_int = 1 if log_entry.get("success", False) else 0
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO checkin_records (
                    timestamp, checkin_type, user_nickname, target_type,
                    target_name, success, message, user_telegram_id,
                    target_identifier, execution_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    log_entry.get("checkin_type"),
                    log_entry.get("user_nickname"),
                    log_entry.get("target_type"),
                    log_entry.get("target_name"),
                    success_int,
                    log_entry.get("message"),
                    log_entry.get("user_telegram_id"),
                    log_entry.get("target_identifier"),
                    log_entry.get("execution_source"),
                ),
            )
            conn.commit()
        logger.info(
            f"签到日志已保存到 {DB_FILE}: 用户 {log_entry.get('user_nickname')}, "
            f"类型 {log_entry.get('target_type')}, 目标 {log_entry.get('target_name')}"
        )
    except sqlite3.Error as exc:
        logger.error(f"保存每日签到日志到 {DB_FILE} 时出错: {exc}")


def _descriptor_for_task(task_entry, config):
    identity = task_identity_from_config(task_entry)
    if not identity:
        return None
    user_id, target_type, target_identifier = identity
    users = config.get("users", []) if isinstance(config, dict) else []
    user = next((item for item in users if item.get("telegram_id") == user_id), None)
    user_nickname = user.get("nickname", f"TGID_{user_id}") if user else f"TGID_{user_id}"
    target_name = target_identifier
    if target_type == "chat":
        chats = config.get("chats", []) if isinstance(config, dict) else []
        chat = next(
            (item for item in chats if str(item.get("chat_id")) == target_identifier), None
        )
        if chat:
            target_name = chat.get("chat_title", target_identifier)
    return {
        "identity": identity,
        "user_telegram_id": user_id,
        "user_nickname": user_nickname,
        "target_type": target_type,
        "target_identifier": target_identifier,
        "target_name": target_name,
    }


def _task_descriptors(config):
    return [
        descriptor
        for task_entry in config.get("checkin_tasks", [])
        if (descriptor := _descriptor_for_task(task_entry, config)) is not None
    ]


def recover_stale_task_states(state_date=None):
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    cutoff = _beijing_naive_iso(beijing_now() - timedelta(seconds=STALE_TASK_STATE_SECONDS))
    now_value = _beijing_naive_iso()
    try:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE daily_task_state
                SET status = 'pending', batch_id = NULL, queued_at = NULL,
                    source = NULL, message = NULL
                WHERE state_date = ? AND status = 'queued' AND queued_at < ?
                """,
                (state_date, cutoff),
            )
            conn.execute(
                """
                UPDATE daily_task_state
                SET status = 'interrupted', finished_at = ?, success = 0,
                    run_token = NULL, batch_id = NULL,
                    message = '执行超过10分钟或进程中断'
                WHERE state_date = ? AND status = 'running' AND started_at < ?
                """,
                (now_value, state_date, cutoff),
            )
            conn.commit()
    except sqlite3.Error as exc:
        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
            logger.debug(f"恢复过期任务状态时数据库暂时被占用: {exc}")
        else:
            logger.error(f"恢复过期任务状态失败: {exc}")


def backfill_legacy_task_states(config, state_date=None):
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    bounds = _date_bounds(state_date)
    if not bounds:
        return
    descriptors = _task_descriptors(config)
    if not descriptors:
        return
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            legacy_rows = conn.execute(
                """
                SELECT id, timestamp, user_nickname, target_type, target_name, success
                FROM checkin_records
                WHERE timestamp >= ? AND timestamp <= ?
                  AND (user_telegram_id IS NULL OR target_identifier IS NULL)
                """,
                bounds,
            ).fetchall()
            for row in legacy_rows:
                matches = [
                    descriptor
                    for descriptor in descriptors
                    if descriptor["user_nickname"] == row["user_nickname"]
                    and descriptor["target_type"] == row["target_type"]
                    and descriptor["target_name"] == row["target_name"]
                ]
                if len(matches) != 1:
                    continue
                descriptor = matches[0]
                identity = descriptor["identity"]
                conn.execute(
                    """
                    INSERT INTO daily_task_state (
                        state_date, user_telegram_id, target_type, target_identifier,
                        status, attempt_count, source, finished_at, success, message
                    ) VALUES (?, ?, ?, ?, 'completed', 1, 'legacy', ?, ?,
                              '由升级前日志回填')
                    ON CONFLICT(state_date, user_telegram_id, target_type, target_identifier)
                    DO UPDATE SET
                        status = CASE WHEN daily_task_state.attempt_count = 0
                                      THEN 'completed' ELSE daily_task_state.status END,
                        attempt_count = CASE WHEN daily_task_state.attempt_count = 0
                                             THEN 1 ELSE daily_task_state.attempt_count END,
                        source = CASE WHEN daily_task_state.attempt_count = 0
                                      THEN 'legacy' ELSE daily_task_state.source END,
                        finished_at = CASE WHEN daily_task_state.attempt_count = 0
                                           THEN excluded.finished_at ELSE daily_task_state.finished_at END,
                        success = CASE WHEN daily_task_state.attempt_count = 0
                                       THEN excluded.success ELSE daily_task_state.success END,
                        message = CASE WHEN daily_task_state.attempt_count = 0
                                       THEN excluded.message ELSE daily_task_state.message END
                    """,
                    (
                        state_date,
                        identity[0],
                        identity[1],
                        identity[2],
                        row["timestamp"],
                        1 if row["success"] else 0,
                    ),
                )
                conn.execute(
                    """
                    UPDATE checkin_records
                    SET user_telegram_id = ?, target_identifier = ?,
                        execution_source = COALESCE(execution_source, 'legacy')
                    WHERE id = ?
                    """,
                    (identity[0], identity[2], row["id"]),
                )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"回填升级前日志状态失败: {exc}")


def ensure_daily_task_rows(config, state_date=None):
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    backfill_legacy_task_states(config, state_date)
    recover_stale_task_states(state_date)
    descriptors = _task_descriptors(config)
    try:
        with _connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO daily_task_state (
                    state_date, user_telegram_id, target_type, target_identifier,
                    status, attempt_count
                ) VALUES (?, ?, ?, ?, 'pending', 0)
                """,
                [
                    (state_date, *descriptor["identity"])
                    for descriptor in descriptors
                ],
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"初始化每日任务状态失败: {exc}")


def get_daily_task_states(config, state_date=None):
    state_date = state_date or beijing_today_str()
    ensure_daily_task_rows(config, state_date)
    descriptors = _task_descriptors(config)
    identities = {descriptor["identity"] for descriptor in descriptors}
    if not identities:
        return {}
    _ensure_initialized()
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM daily_task_state WHERE state_date = ?
                """,
                (state_date,),
            ).fetchall()
        return {
            (row["user_telegram_id"], row["target_type"], row["target_identifier"]): dict(row)
            for row in rows
            if (row["user_telegram_id"], row["target_type"], row["target_identifier"])
            in identities
        }
    except sqlite3.Error as exc:
        logger.error(f"读取每日任务状态失败: {exc}")
        return {}


def get_daily_task_counts(config, state_date=None):
    states = get_daily_task_states(config, state_date)
    descriptors = _task_descriptors(config)
    executed_count = sum(
        1
        for descriptor in descriptors
        if states.get(descriptor["identity"], {}).get("status") in TASK_EXECUTED_STATES
    )
    pending_count = sum(
        1
        for descriptor in descriptors
        if states.get(descriptor["identity"], {}).get("status") == TASK_STATE_PENDING
    )
    return {
        "total_count": len(config.get("checkin_tasks", [])),
        "executed_count": executed_count,
        "pending_count": pending_count,
        "queued_count": sum(
            1
            for descriptor in descriptors
            if states.get(descriptor["identity"], {}).get("status") == TASK_STATE_QUEUED
        ),
        "running_count": sum(
            1
            for descriptor in descriptors
            if states.get(descriptor["identity"], {}).get("status") == TASK_STATE_RUNNING
        ),
    }


def _insert_state_if_missing(conn, state_date, identity):
    conn.execute(
        """
        INSERT OR IGNORE INTO daily_task_state (
            state_date, user_telegram_id, target_type, target_identifier,
            status, attempt_count
        ) VALUES (?, ?, ?, ?, 'pending', 0)
        """,
        (state_date, *identity),
    )


def _claim_daily_task(identity, source, allow_retry=False, state_date=None):
    if not identity:
        return {"claimed": False, "reason": "invalid_identity", "token": None}
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    recover_stale_task_states(state_date)
    token = uuid.uuid4().hex
    now_value = _beijing_naive_iso()
    try:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _insert_state_if_missing(conn, state_date, identity)
            row = conn.execute(
                """
                SELECT status FROM daily_task_state
                WHERE state_date = ? AND user_telegram_id = ?
                  AND target_type = ? AND target_identifier = ?
                """,
                (state_date, *identity),
            ).fetchone()
            status = row[0] if row else TASK_STATE_PENDING
            if status in {TASK_STATE_QUEUED, TASK_STATE_RUNNING}:
                conn.commit()
                return {"claimed": False, "reason": "in_progress", "token": None}
            if status in TASK_EXECUTED_STATES and not allow_retry:
                conn.commit()
                return {"claimed": False, "reason": "already_executed", "token": None}
            conn.execute(
                """
                UPDATE daily_task_state
                SET status = 'running', attempt_count = attempt_count + 1,
                    source = ?, run_token = ?, started_at = ?, finished_at = NULL,
                    success = NULL, message = NULL, batch_id = NULL, queued_at = NULL
                WHERE state_date = ? AND user_telegram_id = ?
                  AND target_type = ? AND target_identifier = ?
                """,
                (source, token, now_value, state_date, *identity),
            )
            conn.commit()
        return {"claimed": True, "reason": "claimed", "token": token}
    except sqlite3.Error as exc:
        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
            logger.debug(f"抢占每日任务时数据库暂时被占用 {identity}: {exc}")
        else:
            logger.error(f"抢占每日任务失败 {identity}: {exc}")
        return {"claimed": False, "reason": "database_error", "token": None}


def claim_daily_task(identity, source, allow_retry=False, state_date=None):
    with _state_lock:
        return _claim_daily_task(identity, source, allow_retry, state_date)


def _queue_daily_tasks(task_entries, batch_id=None, state_date=None):
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    ensure_date = state_date
    recover_stale_task_states(ensure_date)
    batch_id = batch_id or uuid.uuid4().hex
    now_value = _beijing_naive_iso()
    queued_count = 0
    skipped_count = 0
    try:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for task_entry in task_entries:
                identity = task_identity_from_config(task_entry)
                if not identity:
                    skipped_count += 1
                    continue
                _insert_state_if_missing(conn, state_date, identity)
                row = conn.execute(
                    """
                    SELECT status FROM daily_task_state
                    WHERE state_date = ? AND user_telegram_id = ?
                      AND target_type = ? AND target_identifier = ?
                    """,
                    (state_date, *identity),
                ).fetchone()
                if row and row[0] == TASK_STATE_PENDING:
                    conn.execute(
                        """
                        UPDATE daily_task_state
                        SET status = 'queued', batch_id = ?, queued_at = ?,
                            source = 'quick', message = NULL
                        WHERE state_date = ? AND user_telegram_id = ?
                          AND target_type = ? AND target_identifier = ?
                        """,
                        (batch_id, now_value, state_date, *identity),
                    )
                    queued_count += 1
                else:
                    skipped_count += 1
            conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"批量排队每日任务失败: {exc}")
        raise
    return batch_id, queued_count, skipped_count


def queue_daily_tasks(task_entries, batch_id=None, state_date=None):
    with _state_lock:
        return _queue_daily_tasks(task_entries, batch_id, state_date)


def refresh_queued_batch(batch_id, state_date=None):
    if not batch_id:
        return
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    try:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE daily_task_state SET queued_at = ?
                WHERE state_date = ? AND status = 'queued' AND batch_id = ?
                """,
                (_beijing_naive_iso(), state_date, batch_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"刷新任务批次状态失败: {exc}")


def start_queued_task(identity, batch_id, state_date=None):
    if not identity:
        return {"started": False, "reason": "invalid_identity", "token": None}
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    token = uuid.uuid4().hex
    try:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status FROM daily_task_state
                WHERE state_date = ? AND user_telegram_id = ?
                  AND target_type = ? AND target_identifier = ? AND batch_id = ?
                """,
                (state_date, *identity, batch_id),
            ).fetchone()
            if not row or row[0] != TASK_STATE_QUEUED:
                conn.commit()
                return {"started": False, "reason": "not_queued", "token": None}
            conn.execute(
                """
                UPDATE daily_task_state
                SET status = 'running', attempt_count = attempt_count + 1,
                    source = 'quick', run_token = ?, started_at = ?,
                    finished_at = NULL, success = NULL, message = NULL,
                    queued_at = NULL
                WHERE state_date = ? AND user_telegram_id = ?
                  AND target_type = ? AND target_identifier = ? AND batch_id = ?
                """,
                (token, _beijing_naive_iso(), state_date, *identity, batch_id),
            )
            conn.commit()
        return {"started": True, "reason": "started", "token": token}
    except sqlite3.Error as exc:
        logger.error(f"启动排队任务失败 {identity}: {exc}")
        return {"started": False, "reason": "database_error", "token": None}


def complete_daily_task(identity, token, success, message=None, interrupted=False, state_date=None):
    if not identity or not token:
        return False
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    status = TASK_STATE_INTERRUPTED if interrupted else TASK_STATE_COMPLETED
    try:
        with _connect() as conn:
            cursor = conn.execute(
                """
                UPDATE daily_task_state
                SET status = ?, finished_at = ?, success = ?, message = ?,
                    run_token = NULL, batch_id = NULL, queued_at = NULL
                WHERE state_date = ? AND user_telegram_id = ?
                  AND target_type = ? AND target_identifier = ?
                  AND run_token = ? AND status = 'running'
                """,
                (
                    status,
                    _beijing_naive_iso(),
                    1 if success else 0,
                    message,
                    state_date,
                    *identity,
                    token,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
    except sqlite3.Error as exc:
        logger.error(f"完成每日任务状态失败 {identity}: {exc}")
        return False


def set_task_planned_time(identity, planned_at, state_date=None):
    if not identity or planned_at is None:
        return
    _ensure_initialized()
    if planned_at.tzinfo is None:
        planned_at = planned_at.replace(tzinfo=BEIJING_TZ)
    planned_at = planned_at.astimezone(BEIJING_TZ)
    state_date = state_date or planned_at.date().isoformat()
    planned_value = planned_at.isoformat()
    try:
        with _connect() as conn:
            _insert_state_if_missing(conn, state_date, identity)
            conn.execute(
                """
                UPDATE daily_task_state
                SET planned_at = ?
                WHERE state_date = ? AND user_telegram_id = ?
                  AND target_type = ? AND target_identifier = ?
                  AND (status IN ('pending', 'queued', 'running') OR planned_at IS NULL)
                """,
                (planned_value, state_date, *identity),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"保存任务计划时间失败 {identity}: {exc}")


def clear_task_planned_time(identity, state_date=None):
    if not identity:
        return
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    try:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE daily_task_state SET planned_at = NULL
                WHERE state_date = ? AND user_telegram_id = ?
                  AND target_type = ? AND target_identifier = ?
                  AND status IN ('pending', 'queued')
                """,
                (state_date, *identity),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"清理任务计划时间失败 {identity}: {exc}")


def get_planned_times_for_date(state_date=None, identities=None):
    _ensure_initialized()
    state_date = state_date or beijing_today_str()
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT user_telegram_id, target_type, target_identifier, planned_at
                FROM daily_task_state
                WHERE state_date = ? AND planned_at IS NOT NULL
                """,
                (state_date,),
            ).fetchall()
        result = {}
        allowed = {task_identity_key(identity) for identity in identities} if identities else None
        for row in rows:
            identity = (row["user_telegram_id"], row["target_type"], row["target_identifier"])
            if allowed is None or identity in allowed:
                result[identity] = row["planned_at"]
        return result
    except sqlite3.Error as exc:
        logger.error(f"读取任务计划时间失败: {exc}")
        return {}
