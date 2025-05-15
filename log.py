import sqlite3
import os
from datetime import date, datetime
import logging

logger = logging.getLogger(__name__)

DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, 'checkin_log.db')

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS checkin_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                checkin_type TEXT,
                user_nickname TEXT,
                bot_username TEXT,
                success INTEGER,
                message TEXT
            )
        ''')
        conn.commit()
        logger.info(f"Database {DB_FILE} initialized successfully.")
    except sqlite3.Error as e:
        logger.error(f"Error initializing database {DB_FILE}: {e}")
    finally:
        if conn:
            conn.close()

def load_daily_checkin_log():
    today_str_start = date.today().isoformat() + "T00:00:00"
    today_str_end = date.today().isoformat() + "T23:59:59.999999"
    
    daily_logs = []
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, checkin_type, user_nickname, bot_username, success, message 
            FROM checkin_records 
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp DESC
        ''', (today_str_start, today_str_end))
        
        rows = cursor.fetchall()
        for row in rows:
            daily_logs.append(dict(row))
            
    except sqlite3.Error as e:
        logger.error(f"Error loading daily checkin log from {DB_FILE}: {e}")
        return [] 
    finally:
        if conn:
            conn.close()
    
    return daily_logs

def save_daily_checkin_log(log_entry):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        timestamp = log_entry.get("timestamp", datetime.now().isoformat())
        
        success_int = 1 if log_entry.get("success", False) else 0

        cursor.execute('''
            INSERT INTO checkin_records (timestamp, checkin_type, user_nickname, bot_username, success, message)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            timestamp,
            log_entry.get("checkin_type"),
            log_entry.get("user_nickname"),
            log_entry.get("bot_username"),
            success_int,
            log_entry.get("message")
        ))
        conn.commit()
        logger.info(f"Saved checkin log to {DB_FILE}: User {log_entry.get('user_nickname')}, Bot {log_entry.get('bot_username')}")
    except sqlite3.Error as e:
        logger.error(f"Error saving daily checkin log to {DB_FILE}: {e}")
    finally:
        if conn:
            conn.close()
