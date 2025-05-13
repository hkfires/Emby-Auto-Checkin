import json
import os

CONFIG_FILE = 'config_data.json'

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default_config = {
            "api_id": None,
            "api_hash": None,
            "users": [],
            "bots": [],
            "checkin_tasks": [],
            "scheduler_enabled": False,
            "scheduler_range_start_hour": 8,
            "scheduler_range_start_minute": 0,
            "scheduler_range_end_hour": 22,
            "scheduler_range_end_minute": 0,
            "web_users": []
        }
        save_config(default_config)
        return default_config
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            config.setdefault("scheduler_enabled", False)
            config.setdefault("web_users", [])

            config.setdefault("scheduler_range_start_hour", config.pop("scheduler_time_hour", 8))
            config.setdefault("scheduler_range_start_minute", config.pop("scheduler_time_minute", 0))
            config.setdefault("scheduler_range_end_hour", 22)
            config.setdefault("scheduler_range_end_minute", 0)

            if "scheduler_time_hour" in config:
                del config["scheduler_time_hour"]
            if "scheduler_time_minute" in config:
                del config["scheduler_time_minute"]

            for task in config.get("checkin_tasks", []):
                task.setdefault("last_auto_checkin_status", None)
                task.setdefault("last_auto_checkin_time", None)
            return config
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "api_id": None, "api_hash": None, "users": [], "bots": [], "checkin_tasks": [],
            "scheduler_enabled": False,
            "scheduler_range_start_hour": 8, "scheduler_range_start_minute": 0,
            "scheduler_range_end_hour": 22, "scheduler_range_end_minute": 0,
            "web_users": []
        }

def save_config(config_data):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config_data, f, indent=2)
