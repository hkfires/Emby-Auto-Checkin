import json
import os
from datetime import date, datetime

DATA_DIR = "data"
DAILY_CHECKIN_LOG_FILE = os.path.join(DATA_DIR, 'daily_checkin_log.json')

def load_daily_checkin_log():
    today_str = date.today().isoformat()
    if not os.path.exists(DAILY_CHECKIN_LOG_FILE):
        return {}
    try:
        with open(DAILY_CHECKIN_LOG_FILE, 'r') as f:
            logs = json.load(f)
            return logs.get(today_str, {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_daily_checkin_log(log_entry):
    today_str = date.today().isoformat()
    logs = {}
    if os.path.exists(DAILY_CHECKIN_LOG_FILE):
        try:
            with open(DAILY_CHECKIN_LOG_FILE, 'r') as f:
                logs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logs = {}

    if today_str not in logs:
        logs[today_str] = []

    log_entry_with_time = {
        "timestamp": datetime.now().isoformat(),
        **log_entry
    }
    logs[today_str].append(log_entry_with_time)

    with open(DAILY_CHECKIN_LOG_FILE, 'w') as f:
        json.dump(logs, f, indent=2)
