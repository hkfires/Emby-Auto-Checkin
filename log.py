import sqlite3, os
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, 'checkin_log.db')

def init_log_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS checkin_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    checkin_type TEXT,
                    user_nickname TEXT,
                    target_type TEXT,
                    target_name TEXT,
                    success INTEGER,
                    message TEXT
                )
            ''')
            conn.commit()
            logger.info(f"数据库 {DB_FILE} 初始化成功。")
    except sqlite3.Error as e:
        logger.error(f"初始化数据库 {DB_FILE} 时出错: {e}")

def load_checkin_log_by_date(target_date_str):
    try:
        datetime.strptime(target_date_str, '%Y-%m-%d')
    except ValueError:
        logger.error(f"无效的日期格式: {target_date_str}. 需要 YYYY-MM-DD 格式。")
        return []

    date_start_str = target_date_str + "T00:00:00"
    date_end_str = target_date_str + "T23:59:59.999999"
    
    logs_for_date = []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp, checkin_type, user_nickname, target_type, target_name, success, message 
                FROM checkin_records 
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp DESC
            ''', (date_start_str, date_end_str))
            
            rows = cursor.fetchall()
            for row in rows:
                logs_for_date.append(dict(row))
            
    except sqlite3.Error as e:
        logger.error(f"从 {DB_FILE} 加载日期 {target_date_str} 的签到日志时出错: {e}")
        return [] 
    
    return logs_for_date

def save_daily_checkin_log(log_entry):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            
            timestamp = log_entry.get("timestamp", datetime.now().isoformat())
            
            success_int = 1 if log_entry.get("success", False) else 0

            cursor.execute('''
                INSERT INTO checkin_records (timestamp, checkin_type, user_nickname, target_type, target_name, success, message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                timestamp,
                log_entry.get("checkin_type"),
                log_entry.get("user_nickname"),
                log_entry.get("target_type"),
                log_entry.get("target_name"),
                success_int,
                log_entry.get("message")
            ))
            conn.commit()
            logger.info(f"签到日志已保存到 {DB_FILE}: 用户 {log_entry.get('user_nickname')}, 类型 {log_entry.get('target_type')}, 目标 {log_entry.get('target_name')}")
    except sqlite3.Error as e:
        logger.error(f"保存每日签到日志到 {DB_FILE} 时出错: {e}")
