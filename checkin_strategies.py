import asyncio
import logging
from telethon import events

class CheckinStrategy:
    def __init__(self, client, bot_entity, logger, nickname_for_logging):
        self.client = client
        self.bot_entity = bot_entity
        self.logger = logger
        self.nickname_for_logging = nickname_for_logging
        self.timeout_seconds = 10 

    async def send_command(self, command_text):
        await self.client.send_message(self.bot_entity, command_text)
        self.logger.info(f"用户 {self.nickname_for_logging}: 已发送命令 '{command_text}' 给 {self.bot_entity.username}")

    async def _parse_response_text(self, text_content):
        """
        通用文本解析逻辑，判断签到是否成功。
        子类可以覆盖此方法以实现更复杂的解析。
        返回: {"success": bool, "message": str}
        """
        processed_text = text_content.strip()
        if "成功" in processed_text or \
           ("已签到" in processed_text and not ("失败" in processed_text or "重复" in processed_text or "已达上限" in processed_text or "无法" in processed_text)):
            return {"success": True, "message": processed_text}
        elif "已经签到过" in processed_text or "今日已签" in processed_text or "重复签到" in processed_text:
            return {"success": False, "message": processed_text}
        else:
            return {"success": False, "message": processed_text + " (未知情况)"}

    async def check_in(self):
        """
        抽象方法，由子类实现具体的签到逻辑。
        应返回一个包含 {"success": bool, "message": str} 的字典。
        """
        raise NotImplementedError("子类必须实现 check_in 方法")

class StartCommandButtonAlertStrategy(CheckinStrategy):
    async def check_in(self):
        self.logger.info(f"用户 {self.nickname_for_logging}: 使用 StartCommandButtonAlertStrategy 开始签到。")
        await self.send_command('/start')

        check_in_event = asyncio.Event()
        result = {"success": False, "message": "签到过程未启动或未完成。"}
        response_message_text_capture = ""

        async def message_handler(event):
            nonlocal result, response_message_text_capture
            response_message_text_capture = event.raw_text[:100]
            self.logger.info(f"用户 {self.nickname_for_logging}: 收到来自 {self.bot_entity.username} 的消息: {response_message_text_capture}...")
            
            clicked_button = False
            if event.buttons:
                for row in event.buttons:
                    for button_in_row in row:
                        if '签到' in button_in_row.text:
                            self.logger.info(f"用户 {self.nickname_for_logging}: 检测到签到按钮: {button_in_row.text}")
                            try:
                                click_callback_result = await button_in_row.click()
                                clicked_button = True
                                self.logger.info(f"用户 {self.nickname_for_logging}: '签到'按钮已点击。click() 返回类型: {type(click_callback_result)}")

                                alert_message_text = None
                                proceed_to_check_chat_messages = False

                                if hasattr(click_callback_result, 'message') and click_callback_result.message:
                                    alert_message_text = click_callback_result.message
                                    self.logger.info(f"用户 {self.nickname_for_logging}: 按钮点击后收到弹框提示: {alert_message_text}")
                                    result = await self._parse_response_text(alert_message_text)
                                    if "Done" in alert_message_text and not result["success"] and "未知情况" in result["message"]:
                                         self.logger.info(f"用户 {self.nickname_for_logging}: 弹框消息 \"{alert_message_text}\" 包含 'Done' 但非明确成功/失败，将检查后续聊天消息。")
                                         proceed_to_check_chat_messages = True
                                    elif result["success"] or ("重复签到" in result["message"] or "今日已签" in result["message"]):
                                        pass
                                    else:
                                        result = {"success": False, "message": alert_message_text + " (弹框内容未知)"}

                                else:
                                    proceed_to_check_chat_messages = True

                                if proceed_to_check_chat_messages:
                                    self.logger.info(f"用户 {self.nickname_for_logging}: 等待后续聊天消息 (原因: 无弹框，或弹框为通用确认如 'Done' 且非明确结果)。")
                                    await asyncio.sleep(2.5)
                                    messages_after_click = await self.client.get_messages(self.bot_entity, limit=1)
                                    if messages_after_click:
                                        chat_response_text = messages_after_click[0].text
                                        self.logger.info(f"用户 {self.nickname_for_logging}: 机器人后续聊天响应: {chat_response_text}")
                                        result = await self._parse_response_text(chat_response_text)
                                    else:
                                        self.logger.warning(f"用户 {self.nickname_for_logging}: 点击按钮后也未收到聊天响应。")
                                        if result.get("message") == "签到过程未启动或未完成。":
                                            result = {"success": False, "message": "按钮已点击，但未收到机器人后续响应（弹框或聊天消息）。"}
                            except Exception as e_click:
                                self.logger.error(f"用户 {self.nickname_for_logging}: 点击签到按钮或处理后续响应时失败: {e_click}")
                                result = {"success": False, "message": f"点击签到按钮或处理后续响应失败: {e_click}"}
                            finally:
                                check_in_event.set()
                                return

            if not clicked_button and not event.buttons:
                self.logger.info(f"用户 {self.nickname_for_logging}: 消息无按钮，直接解析文本内容。")
                result = await self._parse_response_text(event.raw_text)
                check_in_event.set()


        active_handler = self.client.add_event_handler(message_handler, events.NewMessage(from_users=self.bot_entity))
        self.logger.info(f"用户 {self.nickname_for_logging}: 等待机器人响应 (超时: {self.timeout_seconds} 秒)...")

        try:
            await asyncio.wait_for(check_in_event.wait(), timeout=self.timeout_seconds)
            if result["success"]:
                self.logger.info(f"用户 {self.nickname_for_logging}: 签到事件已处理，结果成功。消息: {result['message']}")
            else:
                self.logger.warning(f"用户 {self.nickname_for_logging}: 签到事件已处理或超时，结果非成功。消息: {result['message']}")
        except asyncio.TimeoutError:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 等待机器人响应超时或未在消息中找到'签到'按钮。机器人最后消息(如有): '{response_message_text_capture}'")
            if result["message"] == "签到过程未启动或未完成。":
                result = {"success": False, "message": f"等待机器人响应超时或未找到签到按钮。机器人最后消息: '{response_message_text_capture}'"}
        finally:
            if self.client and self.client.is_connected() and active_handler:
                try:
                    self.client.remove_event_handler(active_handler)
                    self.logger.info(f"用户 {self.nickname_for_logging}: 事件处理器已移除。")
                except Exception as e_remove_handler:
                    self.logger.error(f"用户 {self.nickname_for_logging}: 移除事件处理器失败: {e_remove_handler}")
        
        return result

class CheckinCommandTextStrategy(CheckinStrategy):
    async def check_in(self):
        self.logger.info(f"用户 {self.nickname_for_logging}: 使用 CheckinCommandTextStrategy 开始签到。")
        
        response_message_text = ""
        final_result = {"success": False, "message": "与机器人对话失败或响应超时。"}

        try:
            async with self.client.conversation(self.bot_entity, timeout=self.timeout_seconds) as conv:
                await conv.send_message('/checkin')
                self.logger.info(f"用户 {self.nickname_for_logging}: (对话内)已发送命令 '/checkin' 给 {self.bot_entity.username}")
                
                response = await conv.get_response()
                response_message_text = response.text
                self.logger.info(f"用户 {self.nickname_for_logging}: 收到来自 {self.bot_entity.username} 的响应: {response_message_text[:100]}...")
                final_result = await self._parse_response_text(response_message_text)
        except asyncio.TimeoutError:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 等待机器人响应超时。")
            final_result = {"success": False, "message": "等待机器人响应超时。"}
        except Exception as e:
            self.logger.error(f"用户 {self.nickname_for_logging}: 处理响应时发生错误: {e}")
            final_result = {"success": False, "message": f"处理响应时发生错误: {e}"}
            
        return final_result

STRATEGY_MAPPING = {
    "start_button_alert": StartCommandButtonAlertStrategy,
    "checkin_text": CheckinCommandTextStrategy,
}

STRATEGY_DISPLAY_NAMES = {
    "start_button_alert": "默认策略",
    "checkin_text": "69云策略",
}

def get_strategy_class(strategy_identifier):
    return STRATEGY_MAPPING.get(strategy_identifier)

def get_strategy_display_name(strategy_identifier):
    return STRATEGY_DISPLAY_NAMES.get(strategy_identifier, strategy_identifier)
