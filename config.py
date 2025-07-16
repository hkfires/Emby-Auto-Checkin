import json, os

DATA_DIR = "data"
CONFIG_FILE = os.path.join(DATA_DIR, 'config_data.json')

def _get_default_time_slot():
    """Returns the default time slot configuration."""
    return {"id": 1, "name": "默认时段", "start_hour": 8, "start_minute": 0, "end_hour": 22, "end_minute": 0}

def _get_default_config():
    """Returns the default configuration structure."""
    cfg = {
        "api_id": None,
        "api_hash": None,
        "users": [],
        "bots": [],
        "chats": [],
        "checkin_tasks": [],
        "scheduler_enabled": False,
        "scheduler_time_slots": [_get_default_time_slot()],
        "web_users": [],
        "llm_settings": {
            "api_url": "",
            "api_key": "",
            "model_name": "",
            "enabled": False
        }
    }
    default_slot_id = 1
    if cfg["scheduler_time_slots"] and isinstance(cfg["scheduler_time_slots"][0], dict):
        default_slot_id = cfg["scheduler_time_slots"][0].get("id", 1)
    
    for task in cfg["checkin_tasks"]:
        task.setdefault("selected_time_slot_id", default_slot_id)
    return cfg

def load_config():
    """Loads the configuration from the JSON file, handling migration from older formats."""
    if not os.path.exists(CONFIG_FILE):
        default_config = _get_default_config()
        save_config(default_config)
        return default_config
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _get_default_config()

    config.setdefault("api_id", None)
    config.setdefault("api_hash", None)
    config.setdefault("users", [])
    config.setdefault("bots", [])
    config.setdefault("chats", [])
    config.setdefault("checkin_tasks", [])
    config.setdefault("scheduler_enabled", False)
    config.setdefault("web_users", [])
    config.setdefault("llm_settings", {
        "api_url": "",
        "api_key": "",
        "model_name": "",
        "enabled": False
    })

    migrated_to_slots_this_run = False

    if not config.get("scheduler_time_slots") or not isinstance(config.get("scheduler_time_slots"), list):
        if "scheduler_range_start_hour" in config:
            start_h = config.pop("scheduler_range_start_hour")
            start_m = config.pop("scheduler_range_start_minute", 0)
            end_h = config.pop("scheduler_range_end_hour", 22)
            end_m = config.pop("scheduler_range_end_minute", 0)
            config["scheduler_time_slots"] = [{
                "id": 1, "name": "迁移时段 (范围)",
                "start_hour": start_h, "start_minute": start_m,
                "end_hour": end_h, "end_minute": end_m
            }]
            migrated_to_slots_this_run = True
        elif "scheduler_time_hour" in config:
            start_h = config.pop("scheduler_time_hour")
            start_m = config.pop("scheduler_time_minute", 0)
            config["scheduler_time_slots"] = [{
                "id": 1, "name": "迁移时段 (旧)",
                "start_hour": start_h, "start_minute": start_m,
                "end_hour": 22, "end_minute": 0
            }]
            migrated_to_slots_this_run = True
        else:
            config["scheduler_time_slots"] = [_get_default_time_slot()]
        
        if config.get("checkin_tasks"):
             migrated_to_slots_this_run = True


    if not config.get("scheduler_time_slots") or not isinstance(config.get("scheduler_time_slots"), list) or not config["scheduler_time_slots"]:
        config["scheduler_time_slots"] = [_get_default_time_slot()]
        if config.get("checkin_tasks"):
            migrated_to_slots_this_run = True

    default_slot_id_for_tasks = 1
    if config["scheduler_time_slots"] and isinstance(config["scheduler_time_slots"][0], dict):
        first_slot_id = config["scheduler_time_slots"][0].get("id")
        if isinstance(first_slot_id, int):
             default_slot_id_for_tasks = first_slot_id
        
    for task in config.get("checkin_tasks", []):
        task.setdefault("last_auto_checkin_status", None)
        task.setdefault("last_auto_checkin_time", None)
        task.setdefault("last_scheduled_date", None)
        task.setdefault("scheduled_hour", None) 
        task.setdefault("scheduled_minute", None)
        
        if "selected_time_slot_id" not in task or migrated_to_slots_this_run:
            task["selected_time_slot_id"] = default_slot_id_for_tasks
            
    config.pop("scheduler_range_start_hour", None)
    config.pop("scheduler_range_start_minute", None)
    config.pop("scheduler_range_end_hour", None)
    config.pop("scheduler_range_end_minute", None)
    config.pop("scheduler_time_hour", None)
    config.pop("scheduler_time_minute", None)
            
    return config

def save_config(config_data):
    """Saves the configuration data to the JSON file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)
