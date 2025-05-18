from telethon import TelegramClient, errors
import re, logging
from checkin_strategies import get_strategy_class

logger = logging.getLogger(__name__)

def get_session_name(nickname):
    sanitized_nickname = re.sub(r'\W+', '_', nickname)
    return f"session_{sanitized_nickname}"

async def telethon_check_in(api_id, api_hash, nickname_for_logging, session_name, bot_username, strategy_identifier="start_button_alert"):
    logger.info(f"尝试为用户 {nickname_for_logging} 使用会话 {session_name} 向机器人 {bot_username} 进行签到 (策略: {strategy_identifier})")
    client = TelegramClient(session_name, api_id, api_hash)
    result = {"success": False, "message": "签到过程未启动或未完成。"}

    StrategyClass = get_strategy_class(strategy_identifier)
    if not StrategyClass:
        logger.error(f"用户 {nickname_for_logging}: 未找到名为 '{strategy_identifier}' 的签到策略。")
        return {"success": False, "message": f"未知的签到策略: {strategy_identifier}"}

    try:
        logger.info(f"用户 {nickname_for_logging}: 调用 client.connect()")
        await client.connect()
        logger.info(f"用户 {nickname_for_logging}: client.connect() 调用完成。")

        if not client.is_connected():
            logger.error(f"用户 {nickname_for_logging}: client.connect() 后 client.is_connected() 仍为False。")
            return {"success": False, "message": "连接后状态仍为未连接。"}

        logger.info(f"用户 {nickname_for_logging}: client.is_connected() 为True。检查授权状态...")
        is_authorized = await client.is_user_authorized()
        logger.info(f"用户 {nickname_for_logging}: client.is_user_authorized() 返回: {is_authorized}")

        if not is_authorized:
            logger.warning(f"用户 {nickname_for_logging}: 未授权。会话可能已失效或需要重新登录。")
            return {"success": False, "message": "用户未登录或会话无效。请尝试通过用户管理页面刷新登录状态（可能需要重新删除并添加用户）。"}

        logger.info(f"用户 {nickname_for_logging}: 已连接并授权。")
        bot_entity = await client.get_entity(bot_username)
        
        strategy_instance = StrategyClass(client, bot_entity, logger, nickname_for_logging)
        result = await strategy_instance.check_in()

    except ConnectionError as ce:
        logger.error(f"用户 {nickname_for_logging}: 连接到Telegram时发生 ConnectionError: {ce}")
        result.update({"success": False, "message": f"连接错误: {ce}"})
        return result
    except errors.PhoneNumberInvalidError:
        logger.error(f"用户 {nickname_for_logging} (关联手机号可能无效): 无效的手机号码。")
        result.update({"success": False, "message": "提供的手机号码无效。"})
    except errors.PhoneCodeInvalidError:
        logger.error(f"用户 {nickname_for_logging}: 无效的验证码。")
        result.update({"success": False, "message": "提供的验证码无效。"})
    except errors.SessionPasswordNeededError:
        logger.error(f"用户 {nickname_for_logging}: 需要两步验证密码 (当前脚本不支持)。")
        result.update({"success": False, "message": "账户需要两步验证密码，当前不支持。"})
    except errors.UserDeactivatedBanError:
        logger.error(f"用户 {nickname_for_logging}: 账户已被封禁。")
        result.update({"success": False, "message": "Telegram账户已被封禁。"})
    except errors.AuthKeyUnregisteredError:
        logger.error(f"用户 {nickname_for_logging}: Auth key未注册或已吊销 (会话失效)。")
        result.update({"success": False, "message": "会话已失效 (Auth key unregistred)。请重新添加用户。"})
    except Exception as e_general:
        logger.error(f"用户 {nickname_for_logging}: 签到过程中发生未知错误: {type(e_general).__name__} - {e_general}")
        result.update({"success": False, "message": f"签到过程中发生未知错误: {type(e_general).__name__} - {e_general}"})
    finally:
        if client and client.is_connected():
            logger.info(f"用户 {nickname_for_logging}: 准备断开连接。")
            await client.disconnect()
            logger.info(f"用户 {nickname_for_logging}: 已断开连接。")
        else:
            logger.info(f"用户 {nickname_for_logging}: 无需断开连接 (客户端未连接或不存在)。")

    logger.info(f"用户 {nickname_for_logging}: 签到流程结束。结果: {result}")
    return result
