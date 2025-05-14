import asyncio
from telethon import TelegramClient, errors, events
import re
import logging
from config import load_config

logger = logging.getLogger(__name__)

def get_session_name(nickname):
    sanitized_nickname = re.sub(r'\W+', '_', nickname)
    return f"session_{sanitized_nickname}"

async def telethon_check_in(api_id, api_hash, nickname_for_logging, session_name, bot_username):
    logger.info(f"Attempting check-in for {nickname_for_logging} with {bot_username} using session {session_name}")
    client = TelegramClient(session_name, api_id, api_hash)
    result = {"success": False, "message": "签到过程未启动或未完成。"}

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
            await client.disconnect()
            return {"success": False, "message": "用户未登录或会话无效。请尝试通过用户管理页面刷新登录状态（可能需要重新删除并添加用户）。"}

        logger.info(f"用户 {nickname_for_logging}: 已连接并授权。")
        bot_entity = await client.get_entity(bot_username)

        await client.send_message(bot_entity, '/start')
        logger.info(f"用户 {nickname_for_logging}: 已发送/start命令给 {bot_username}")

        timeout_seconds = 30
        check_in_clicked = asyncio.Event()
        response_message = ""

        async def message_handler(event):
            nonlocal response_message, result
            response_message = event.raw_text[:100]
            logger.info(f"用户 {nickname_for_logging}: 收到来自 {bot_username} 的消息: {response_message}...")
            if event.buttons:
                for row in event.buttons:
                    for button_in_row in row:
                        if '签到' in button_in_row.text:
                            logger.info(f"用户 {nickname_for_logging}: 检测到签到按钮: {button_in_row.text}")
                            try:
                                click_callback_result = await button_in_row.click()
                                logger.info(f"用户 {nickname_for_logging}: '签到'按钮已点击。click() 返回: {type(click_callback_result)}")

                                alert_message_text = None
                                proceed_to_check_chat_messages = False 

                                if hasattr(click_callback_result, 'message') and click_callback_result.message:
                                    alert_message_text = click_callback_result.message 
                                    logger.info(f"用户 {nickname_for_logging}: 按钮点击后收到弹框提示: {alert_message_text}")
                                    
                                    processed_alert = alert_message_text.strip()

                                    if "成功" in processed_alert or \
                                       ("已签到" in processed_alert and not ("失败" in processed_alert or "重复" in processed_alert or "已达上限" in processed_alert or "无法" in processed_alert)):
                                        result.update({"success": True, "message": alert_message_text})
                                    elif "已经签到过" in processed_alert or "今日已签" in processed_alert or "重复签到" in processed_alert:
                                        result.update({"success": False, "message": alert_message_text})
                                    elif "Done" in alert_message_text: 
                                        logger.info(f"用户 {nickname_for_logging}: 弹框消息 \"{alert_message_text}\" 包含 'Done' 但非明确成功/失败，将检查后续聊天消息。")
                                        proceed_to_check_chat_messages = True 
                                    else: 
                                        result.update({"success": False, "message": alert_message_text + " (弹框内容未知)"})
                                else: 
                                    proceed_to_check_chat_messages = True 

                                if proceed_to_check_chat_messages:
                                    logger.info(f"用户 {nickname_for_logging}: 等待后续聊天消息 (原因: 无弹框，或弹框为通用确认如 'Done' 且非明确结果)。")
                                    await asyncio.sleep(2.5) 
                                    messages_after_click = await client.get_messages(bot_entity, limit=1)
                                    if messages_after_click:
                                        chat_response_text = messages_after_click[0].text
                                        logger.info(f"用户 {nickname_for_logging}: 机器人后续聊天响应: {chat_response_text}")
                                        if "成功" in chat_response_text or \
                                           ("已签到" in chat_response_text and not ("失败" in chat_response_text or "重复" in chat_response_text)):
                                            result.update({"success": True, "message": chat_response_text})
                                        elif "重复签到" in chat_response_text or "今日已签" in chat_response_text:
                                            result.update({"success": False, "message": chat_response_text})
                                        else:
                                            result.update({"success": False, "message": chat_response_text + " (未知情况)"})
                                    else:
                                        logger.warning(f"用户 {nickname_for_logging}: 点击按钮后也未收到聊天响应。")
                                        if result.get("message") == "签到过程未启动或未完成。": 
                                            result.update({"success": False, "message": "按钮已点击，但未收到机器人后续响应（弹框或聊天消息）。"})
                            except Exception as e_click:
                                logger.error(f"用户 {nickname_for_logging}: 点击签到按钮或处理后续响应时失败: {e_click}")
                                result.update({"success": False, "message": f"点击签到按钮或处理后续响应失败: {e_click}"})
                            finally:
                                check_in_clicked.set()
                                return

        active_handler = client.add_event_handler(message_handler, events.NewMessage(from_users=bot_entity))
        logger.info(f"用户 {nickname_for_logging}: 等待机器人响应 (超时: {timeout_seconds} 秒)...")

        try:
            await asyncio.wait_for(check_in_clicked.wait(), timeout=timeout_seconds)
            if result["success"]:
                 logger.info(f"用户 {nickname_for_logging}: 签到事件已处理，结果成功。消息: {result['message']}")
            else:
                 logger.warning(f"用户 {nickname_for_logging}: 签到事件已处理或超时，结果非成功。消息: {result['message']}")
        except asyncio.TimeoutError:
            logger.warning(f"用户 {nickname_for_logging}: 等待机器人响应超时或未在消息中找到'签到'按钮。机器人最后消息(如有): '{response_message}'")
            if result["message"] == "签到过程未启动或未完成。":
                result.update({"success": False, "message": f"等待机器人响应超时或未找到签到按钮。机器人最后消息: '{response_message}'"})
        finally:
            if client and client.is_connected() and active_handler:
                 try:
                    client.remove_event_handler(active_handler, events.NewMessage)
                    logger.info(f"用户 {nickname_for_logging}: 事件处理器已移除。")
                 except Exception as e_remove_handler:
                    logger.error(f"用户 {nickname_for_logging}: 移除事件处理器失败: {e_remove_handler}")

    except ConnectionError as ce:
        logger.error(f"用户 {nickname_for_logging}: 连接到Telegram时发生 ConnectionError: {ce}")
        result.update({"success": False, "message": f"连接错误: {ce}"})
        if client and client.is_connected():
            await client.disconnect()
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
    except Exception as e_general:
        logger.error(f"用户 {nickname_for_logging}: 签到过程中发生未知错误: {e_general}")
        result.update({"success": False, "message": f"签到过程中发生未知错误: {e_general}"})
    finally:
        if client.is_connected():
            await client.disconnect()
    logger.info(f"用户 {nickname_for_logging}: 签到流程结束。结果: {result}")
    return result
