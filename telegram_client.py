from telethon import TelegramClient, errors, functions, types
import re, logging
from checkin_strategies import get_strategy_class

logger = logging.getLogger(__name__)

def get_session_name(nickname):
    if not nickname or not re.search(r'[a-zA-Z0-9]', nickname):
        base_name = "default_user"
    else:
        base_name = nickname
    sanitized_nickname = re.sub(r'\W+', '_', base_name)
    return f"session_{sanitized_nickname}"

async def _connect_and_authorize_client(api_id, api_hash, session_name, nickname_for_logging):
    client = TelegramClient(session_name, api_id, api_hash)
    logger.info(f"用户 {nickname_for_logging}: 尝试连接会话 {session_name}")
    await client.connect()
    if not client.is_connected():
        logger.error(f"用户 {nickname_for_logging}: 连接失败。")
        raise ConnectionError("连接后状态仍为未连接。")
    
    logger.info(f"用户 {nickname_for_logging}: 已连接。检查授权状态...")
    if not await client.is_user_authorized():
        logger.warning(f"用户 {nickname_for_logging}: 未授权。会话可能已失效或需要重新登录。")
        await client.disconnect()
        raise errors.UserDeactivatedBanError("用户未登录或会话无效。请尝试通过用户管理页面刷新登录状态。")
    logger.info(f"用户 {nickname_for_logging}: 已授权。")
    return client

async def resolve_chat_identifier(api_id, api_hash, user_session_name, chat_identifier, nickname_for_logging=""):
    logger.info(f"用户 {nickname_for_logging} (会话: {user_session_name}): 尝试解析群组标识符 '{chat_identifier}'")
    client = None
    try:
        client = await _connect_and_authorize_client(api_id, api_hash, user_session_name, nickname_for_logging)
        
        entity = await client.get_entity(chat_identifier)
        
        if not isinstance(entity, (types.Chat, types.Channel)):
            logger.warning(f"用户 {nickname_for_logging}: 标识符 '{chat_identifier}' 解析到的实体不是群组或频道，类型为: {type(entity)}")
            if isinstance(chat_identifier, int):
                 pass
            else:
                async for dialog in client.iter_dialogs():
                    if dialog.name == chat_identifier or dialog.entity.username == chat_identifier:
                        entity = dialog.entity
                        break
                if not isinstance(entity, (types.Chat, types.Channel)):
                     raise ValueError(f"标识符 '{chat_identifier}' 解析到的不是有效的群组或频道。")

        chat_id = entity.id
        title = getattr(entity, 'title', None) or getattr(entity, 'username', None) or f"Chat/Channel {entity.id}"
        
        logger.info(f"用户 {nickname_for_logging}: 标识符 '{chat_identifier}' 解析为 ID: {chat_id}, 名称: {title}")
        return {"id": chat_id, "name": title, "success": True}

    except errors.UserDeactivatedBanError as e:
        logger.error(f"用户 {nickname_for_logging}: 解析群组时用户会话 {user_session_name} 未授权: {e}")
        return {"success": False, "message": f"用户会话未授权: {e}"}
    except ValueError as ve:
        logger.error(f"用户 {nickname_for_logging}: 解析群组标识符 '{chat_identifier}' 失败: {ve}")
        return {"success": False, "message": f"无法解析群组标识符 '{chat_identifier}': {ve}"}
    except Exception as e:
        logger.error(f"用户 {nickname_for_logging}: 解析群组标识符 '{chat_identifier}' 时发生未知错误: {type(e).__name__} - {e}")
        return {"success": False, "message": f"解析时发生未知错误: {type(e).__name__} - {e}"}
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info(f"用户 {nickname_for_logging}: (解析群组) 已断开连接 {user_session_name}。")

async def send_message_to_chat_id(api_id, api_hash, user_session_name, chat_id: int, message_text: str, nickname_for_logging=""):
    logger.info(f"用户 {nickname_for_logging} (会话: {user_session_name}): 尝试向群组ID {chat_id} 发送消息")
    client = None
    result = {"success": False, "message": "消息发送过程未启动或未完成。"}
    try:
        client = await _connect_and_authorize_client(api_id, api_hash, user_session_name, nickname_for_logging)
        
        target_entity = await client.get_entity(chat_id)
        
        await client.send_message(target_entity, message_text)
        logger.info(f"用户 {nickname_for_logging}: 消息已成功发送到群组ID {chat_id}")
        result = {"success": True, "message": "消息已成功发送。"}

    except errors.UserDeactivatedBanError as e:
        logger.error(f"用户 {nickname_for_logging}: 发送消息时用户会话 {user_session_name} 未授权: {e}")
        result.update({"success": False, "message": f"用户会话未授权: {e}"})
    except errors.ChatWriteForbiddenError:
        logger.error(f"用户 {nickname_for_logging}: 没有权限向群组ID {chat_id} 发送消息。")
        result.update({"success": False, "message": "没有权限向此群组发送消息。"})
    except ValueError as ve:
        logger.error(f"用户 {nickname_for_logging}: 无法找到群组ID {chat_id} 对应的实体: {ve}")
        result.update({"success": False, "message": f"无效的群组ID: {chat_id}。"})
    except Exception as e:
        logger.error(f"用户 {nickname_for_logging}: 向群组ID {chat_id} 发送消息时发生未知错误: {type(e).__name__} - {e}")
        result.update({"success": False, "message": f"发送消息时发生未知错误: {type(e).__name__} - {e}"})
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info(f"用户 {nickname_for_logging}: (发送消息) 已断开连接 {user_session_name}。")
    return result

async def telethon_check_in(api_id, api_hash, nickname_for_logging, session_name, bot_username, strategy_identifier="start_button_alert"):
    logger.info(f"用户 {nickname_for_logging} (会话 {session_name}): 尝试向机器人 {bot_username} 进行签到 (策略: {strategy_identifier})")
    client = None
    result = {"success": False, "message": "签到过程未启动或未完成。"}

    StrategyClass = get_strategy_class(strategy_identifier)
    if not StrategyClass:
        logger.error(f"用户 {nickname_for_logging}: 未找到名为 '{strategy_identifier}' 的签到策略。")
        return {"success": False, "message": f"未知的签到策略: {strategy_identifier}"}

    try:
        client = await _connect_and_authorize_client(api_id, api_hash, session_name, nickname_for_logging)
        
        bot_entity = await client.get_entity(bot_username)
        
        strategy_instance = StrategyClass(client, bot_entity, logger, nickname_for_logging, task_config=None)
        if hasattr(strategy_instance, 'check_in') and callable(getattr(strategy_instance, 'check_in')):
            result = await strategy_instance.check_in()
        elif hasattr(strategy_instance, 'execute') and callable(getattr(strategy_instance, 'execute')):
            result = await strategy_instance.execute()
        else:
            logger.error(f"用户 {nickname_for_logging}: 策略 {strategy_identifier} 既没有 'check_in' 方法也没有 'execute' 方法。")
            return {"success": False, "message": f"策略 {strategy_identifier} 无法执行。"}


    except errors.UserDeactivatedBanError as e:
        logger.error(f"用户 {nickname_for_logging}: 签到时用户会话 {session_name} 未授权或账户问题: {e}")
        result.update({"success": False, "message": str(e)})
    except ConnectionError as ce:
        logger.error(f"用户 {nickname_for_logging}: 连接到Telegram时发生 ConnectionError: {ce}")
        result.update({"success": False, "message": f"连接错误: {ce}"})
    except (errors.PhoneNumberInvalidError, errors.PhoneCodeInvalidError, errors.SessionPasswordNeededError, 
              errors.AuthKeyUnregisteredError) as specific_auth_error:
        logger.error(f"用户 {nickname_for_logging}: 签到过程中发生授权或会话相关错误: {type(specific_auth_error).__name__} - {specific_auth_error}")
        result.update({"success": False, "message": f"授权/会话错误: {specific_auth_error}"})
    except ValueError as ve:
        logger.error(f"用户 {nickname_for_logging}: 无法找到机器人 {bot_username} 对应的实体: {ve}")
        result.update({"success": False, "message": f"无效的机器人用户名: {bot_username}。"})
    except Exception as e_general:
        logger.error(f"用户 {nickname_for_logging}: 签到过程中发生未知错误: {type(e_general).__name__} - {e_general}")
        result.update({"success": False, "message": f"签到过程中发生未知错误: {type(e_general).__name__} - {e_general}"})
    finally:
        if client and client.is_connected():
            logger.info(f"用户 {nickname_for_logging}: (签到) 准备断开连接 {session_name}。")
            await client.disconnect()
            logger.info(f"用户 {nickname_for_logging}: (签到) 已断开连接 {session_name}。")
        else:
            logger.info(f"用户 {nickname_for_logging}: (签到) 无需断开连接 (客户端未连接或不存在)。")

    logger.info(f"用户 {nickname_for_logging}: 签到流程结束。结果: {result}")
    return result
