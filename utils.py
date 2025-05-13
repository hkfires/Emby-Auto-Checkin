from datetime import datetime
from config import load_config

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
