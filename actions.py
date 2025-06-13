import logging
from telethon import errors
from checkin_strategies import get_strategy_class
from telegram_client import _connect_and_authorize_client

logger = logging.getLogger(__name__)

async def execute_telegram_action_wrapper(api_id, api_hash, user_nickname, session_name, target_config_item, task_specific_config):
    client = None
    result = {"success": False, "message": "操作未启动。"}
    
    target_entity_identifier = None
    if 'bot_username' in target_config_item:
        target_entity_identifier = target_config_item['bot_username']
        effective_strategy_id = task_specific_config.get('strategy_identifier') or target_config_item.get('strategy')
    elif 'chat_id' in target_config_item:
        target_entity_identifier = target_config_item['chat_id']
        effective_strategy_id = task_specific_config.get('strategy_identifier') or target_config_item.get('strategy_identifier')
    else:
        return {"success": False, "message": "无效的目标配置项。"}

    if not effective_strategy_id:
        return {"success": False, "message": "未能确定操作策略。"}

    StrategyClass = get_strategy_class(effective_strategy_id)
    if not StrategyClass:
        return {"success": False, "message": f"未知的策略: {effective_strategy_id}"}

    try:
        client = await _connect_and_authorize_client(api_id, api_hash, session_name, user_nickname)
        target_entity = await client.get_entity(target_entity_identifier)
        
        strategy_instance = StrategyClass(client, target_entity, logger, user_nickname, task_config=task_specific_config)
        
        if hasattr(strategy_instance, 'execute') and callable(getattr(strategy_instance, 'execute')):
            result = await strategy_instance.execute()
        else:
            logger.error(f"用户 {user_nickname}: 策略 {effective_strategy_id} 没有 'execute' 方法。")
            result = {"success": False, "message": f"策略 {effective_strategy_id} 无法执行。"}

    except errors.UserDeactivatedBanError as e:
        logger.error(f"用户 {user_nickname}: 会话 {session_name} 未授权或账户问题: {e}")
        result.update({"success": False, "message": str(e)})
    except ConnectionError as ce:
        logger.error(f"用户 {user_nickname}: 连接Telegram时发生 ConnectionError: {ce}")
        result.update({"success": False, "message": f"连接错误: {ce}"})
    except ValueError as ve:
        logger.error(f"用户 {user_nickname}: 无法找到实体 {target_entity_identifier}: {ve}")
        result.update({"success": False, "message": f"无法找到目标实体: {target_entity_identifier}。"})
    except Exception as e_general:
        logger.error(f"用户 {user_nickname}: 执行操作时发生未知错误: {type(e_general).__name__} - {e_general}", exc_info=True)
        result.update({"success": False, "message": f"执行操作时发生未知错误: {type(e_general).__name__} - {e_general}"})
    finally:
        if client and client.is_connected():
            await client.disconnect()
    return result
