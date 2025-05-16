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
        self.button_click_attempted_event = None

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

        PRIMARY_SUCCESS_KEYWORD = ["签到成功"]
        ALREADY_CHECKED_IN_KEYWORDS = ["已经签到", "已签到", "重复签到"]
        UNDETERMINED_KEYWORDS = ["Done"]

        for keyword in PRIMARY_SUCCESS_KEYWORD:
            if keyword in processed_text:
                return {"success": True, "message": processed_text}

        for keyword in ALREADY_CHECKED_IN_KEYWORDS:
            if keyword in processed_text:
                return {"success": False, "message": processed_text + " (重复签到)"}
        
        for keyword in UNDETERMINED_KEYWORDS:
            if keyword in processed_text:
                return {"success": False, "message": processed_text + " (待判断)"}
        
        return {"success": False, "message": processed_text + " (未知情况)"}

    async def check_in(self):
        """
        抽象方法，由子类实现具体的签到逻辑。
        应返回一个包含 {"success": bool, "message": str} 的字典。
        """
        raise NotImplementedError("子类必须实现 check_in 方法")

class StartCommandButtonAlertStrategy(CheckinStrategy):
    async def _find_and_click_checkin_button(self, event):
        """查找并点击包含'签到'文本的按钮。"""
        if event.buttons:
            for row in event.buttons:
                for button_in_row in row:
                    if '签到' in button_in_row.text:
                        self.logger.info(f"用户 {self.nickname_for_logging}: 检测到签到按钮: {button_in_row.text}")
                        try:
                            click_callback_result = await button_in_row.click()
                            self.logger.info(f"用户 {self.nickname_for_logging}: '签到'按钮已点击。click() 返回类型: {type(click_callback_result)}")
                            return True, click_callback_result
                        except Exception as e_click:
                            self.logger.error(f"用户 {self.nickname_for_logging}: 点击签到按钮时失败: {e_click}")
                            return True, e_click
        return False, None

    async def _handle_alert_or_prepare_follow_up(self, click_callback_result):
        """处理按钮点击后的弹窗消息，或准备检查后续聊天消息。"""
        parsed_result_from_alert = None
        needs_follow_up = True

        if isinstance(click_callback_result, Exception):
             return {"success": False, "message": f"点击签到按钮失败: {click_callback_result}"}, False

        if hasattr(click_callback_result, 'message') and click_callback_result.message:
            alert_message_text = click_callback_result.message
            self.logger.info(f"用户 {self.nickname_for_logging}: 按钮点击后收到弹框提示: {alert_message_text}")
            parsed_result_from_alert = await self._parse_response_text(alert_message_text)

            if "待判断" in parsed_result_from_alert["message"]:
                self.logger.info(f"用户 {self.nickname_for_logging}: 弹框消息 \"{alert_message_text}\" 非明确成功/失败，将检查后续聊天消息。")
                needs_follow_up = True
            elif parsed_result_from_alert["success"] or ("重复签到" in parsed_result_from_alert["message"]):
                needs_follow_up = False
            else:
                parsed_result_from_alert = {"success": False, "message": alert_message_text + " (弹框内容未知)"}
                needs_follow_up = False
            
            return parsed_result_from_alert, needs_follow_up
        else:
            self.logger.info(f"用户 {self.nickname_for_logging}: 按钮点击后未收到弹框提示，将检查后续聊天消息。")
            return {"success": False, "message": "按钮已点击，等待后续聊天消息。"}, True

    async def _process_follow_up_message(self):
        """等待并处理后续的聊天消息。"""
        self.logger.info(f"用户 {self.nickname_for_logging}: 等待后续聊天消息 (2.5秒)。")
        await asyncio.sleep(2.5)
        messages_after_click = await self.client.get_messages(self.bot_entity, limit=1)
        if messages_after_click:
            chat_response_text = messages_after_click[0].text
            self.logger.info(f"用户 {self.nickname_for_logging}: 机器人后续聊天响应: {chat_response_text}")
            return await self._parse_response_text(chat_response_text)
        else:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 点击按钮后也未收到聊天响应。")
            return {"success": False, "message": "按钮已点击，但未收到机器人后续响应（弹框或聊天消息）。"}

    async def check_in(self):
        self.logger.info(f"用户 {self.nickname_for_logging}: 使用 StartCommandButtonAlertStrategy 开始签到。")
        await self.send_command('/start')

        check_in_event = asyncio.Event()
        self.button_click_attempted_event = asyncio.Event()
        result_container = [{"success": False, "message": "签到过程未启动或未完成。"}]

        async def bot_response_handler(event):
            if check_in_event.is_set():
                self.logger.debug(f"用户 {self.nickname_for_logging}: 主签到事件已完成或超时，忽略新的消息事件。")
                return

            response_message_text_capture = event.raw_text[:100]
            self.logger.info(f"用户 {self.nickname_for_logging}: 收到来自 {self.bot_entity.username} 的消息: {response_message_text_capture}...")

            try:
                if not self.button_click_attempted_event.is_set():
                    clicked_button, click_obj = await self._find_and_click_checkin_button(event)

                    if clicked_button:
                        self.button_click_attempted_event.set()
                        interim_result, needs_follow_up = await self._handle_alert_or_prepare_follow_up(click_obj)
                        
                        if needs_follow_up:
                            final_result = await self._process_follow_up_message()
                            result_container[0] = final_result
                        else:
                            result_container[0] = interim_result
                        check_in_event.set()
                    else:
                        self.logger.info(f"用户 {self.nickname_for_logging}: 未找到'签到'按钮（或按钮点击已处理），解析当前收到的消息。")
                        result_container[0] = await self._parse_response_text(event.raw_text)
                        check_in_event.set()
                else:
                    self.logger.debug(f"用户 {self.nickname_for_logging}: 按钮点击已尝试/处理，此消息事件 '{response_message_text_capture}...' 将由主处理流程或后续步骤管理。")
                    if not check_in_event.is_set():
                        current_message_parsed_result = await self._parse_response_text(event.raw_text)
                        if current_message_parsed_result["success"] or "重复签到" in current_message_parsed_result["message"] or "待判断" not in current_message_parsed_result["message"]:
                            self.logger.info(f"用户 {self.nickname_for_logging}: 按钮点击已处理，将当前消息解析为最终结果: {current_message_parsed_result}")
                            result_container[0] = current_message_parsed_result
                            check_in_event.set()
                        else:
                            self.logger.debug(f"用户 {self.nickname_for_logging}: 按钮点击已处理，当前消息 '{response_message_text_capture}...' 不是明确的最终状态，等待主流程处理或超时。")
            
            except Exception as e_handler:
                self.logger.error(f"用户 {self.nickname_for_logging}: 处理机器人响应时发生意外错误: {e_handler}")
                result_container[0] = {"success": False, "message": f"处理机器人响应时发生意外错误: {e_handler}"}
                if not check_in_event.is_set():
                    check_in_event.set()

        active_handler = self.client.add_event_handler(bot_response_handler, events.NewMessage(from_users=self.bot_entity))
        self.logger.info(f"用户 {self.nickname_for_logging}: 等待机器人响应 (超时: {self.timeout_seconds} 秒)...")

        try:
            await asyncio.wait_for(check_in_event.wait(), timeout=self.timeout_seconds)
            if result_container[0]["success"]:
                self.logger.info(f"用户 {self.nickname_for_logging}: 签到成功。")
            else:
                self.logger.warning(f"用户 {self.nickname_for_logging}: 签到失败。")
        except asyncio.TimeoutError:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 等待机器人响应超时。")
            if result_container[0]["message"] == "签到过程未启动或未完成。":
                 result_container[0] = {"success": False, "message": "等待机器人响应超时（未收到任何可处理消息或按钮点击未完成）。"}
        finally:
            if self.client and self.client.is_connected() and active_handler:
                try:
                    self.client.remove_event_handler(active_handler)
                    self.logger.info(f"用户 {self.nickname_for_logging}: 事件处理器已移除。")
                except Exception as e_remove_handler:
                    self.logger.error(f"用户 {self.nickname_for_logging}: 移除事件处理器失败: {e_remove_handler}")
        
        return result_container[0]

class CheckinCommandTextStrategy(CheckinStrategy):
    async def check_in(self):
        self.logger.info(f"用户 {self.nickname_for_logging}: 使用 CheckinCommandTextStrategy 开始签到。")
        
        response_message_text = ""
        result = {"success": False, "message": "与机器人对话失败或响应超时。"}

        try:
            async with self.client.conversation(self.bot_entity, timeout=self.timeout_seconds) as conv:
                await conv.send_message('/checkin')
                self.logger.info(f"用户 {self.nickname_for_logging}: (对话内)已发送命令 '/checkin' 给 {self.bot_entity.username}")
                
                response = await conv.get_response()
                response_message_text = response.text
                self.logger.info(f"用户 {self.nickname_for_logging}: 收到来自 {self.bot_entity.username} 的响应: {response_message_text[:100]}...")
                result = await self._parse_response_text(response_message_text)
        except asyncio.TimeoutError:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 等待机器人响应超时。")
            result = {"success": False, "message": "等待机器人响应超时。"}
        except Exception as e:
            self.logger.error(f"用户 {self.nickname_for_logging}: 处理响应时发生错误: {e}")
            result = {"success": False, "message": f"处理响应时发生错误: {e}"}
            
        return result

STRATEGY_MAPPING = {
    "start_button_alert": StartCommandButtonAlertStrategy,
    "checkin_text": CheckinCommandTextStrategy,
}

STRATEGY_DISPLAY_NAMES = {
    "start_button_alert": "/start点击签到按钮",
    "checkin_text": "/checkin直接签到",
}

def get_strategy_class(strategy_identifier):
    return STRATEGY_MAPPING.get(strategy_identifier)

def get_strategy_display_name(strategy_identifier):
    return STRATEGY_DISPLAY_NAMES.get(strategy_identifier, strategy_identifier)
