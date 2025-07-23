from datetime import datetime
import logging
from checkin_strategies import STRATEGY_MAPPING, STRATEGY_DISPLAY_NAMES

logger = logging.getLogger(__name__)

def format_datetime_filter(value, format='%Y-%m-%d %H:%M:%S'):
    if value is None:
        return ""
    try:
        dt_object = datetime.fromisoformat(value)
        return dt_object.strftime(format)
    except (ValueError, TypeError):
        return value

def get_masked_api_credentials(config_data):
    api_id = config_data.get('api_id')
    api_hash = config_data.get('api_hash')
    api_id_display = None
    api_hash_display = None
    if api_id:
        api_id_display = api_id[:3] + '****' + api_id[-3:] if len(api_id) > 6 else '******'
    if api_hash:
        api_hash_display = api_hash[:3] + '****' + api_hash[-3:] if len(api_hash) > 6 else '******'
    return api_id_display, api_hash_display

def get_processed_bots_list(raw_bots_list):
    processed_bots = []
    if not isinstance(raw_bots_list, list):
        logger.warning(f"get_processed_bots_list 传入参数应为列表，实际得到 {type(raw_bots_list)}。将返回空列表。")
        return []
        
    for bot_entry in raw_bots_list:
        if isinstance(bot_entry, dict) and bot_entry.get('bot_username'):
            bot_dict = dict(bot_entry) 
            bot_username = bot_dict['bot_username']
            
            current_strategy = bot_dict.get('strategy')
            if not current_strategy or current_strategy not in STRATEGY_MAPPING:
                logger.warning(f"机器人 '{bot_username}' 配置了无效或缺失的策略 '{current_strategy}'。将默认为 'start_button_alert'。")
                current_strategy = "start_button_alert"
            
            bot_dict["strategy"] = current_strategy
            strategy_info = STRATEGY_DISPLAY_NAMES.get(current_strategy)
            if isinstance(strategy_info, dict):
                bot_dict["strategy_display_name"] = strategy_info.get("name", current_strategy)
            else:
                bot_dict["strategy_display_name"] = current_strategy
            processed_bots.append(bot_dict)
        else:
            logger.warning(f"处理过程中跳过无效或格式错误的机器人: {bot_entry}")
    return processed_bots

def update_api_credential(config_obj, submitted_value, current_display_value, config_key):
    if submitted_value == current_display_value and submitted_value is not None:
        pass
    elif submitted_value == "":
        config_obj[config_key] = None
    else:
        config_obj[config_key] = submitted_value
