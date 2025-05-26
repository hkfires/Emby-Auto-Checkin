import asyncio
from telethon import events

class CheckinStrategy:
    def __init__(self, client, target_entity, logger, nickname_for_logging, task_config=None):
        self.client = client
        self.target_entity = target_entity
        self.logger = logger
        self.nickname_for_logging = nickname_for_logging
        self.task_config = task_config if task_config else {}
        self.timeout_seconds = 10
        self.button_click_attempted_event = None
        self.first_click_lock = None

    async def send_command(self, command_text):
        target_display_name = getattr(self.target_entity, 'username', getattr(self.target_entity, 'title', str(self.target_entity.id)))
        await self.client.send_message(self.target_entity, command_text)
        self.logger.info(f"用户 {self.nickname_for_logging}: 已发送命令 '{command_text}' 给 {target_display_name}")

    async def _parse_response_text(self, text_content):
        processed_text = text_content.strip()

        if "签到成功" in processed_text or "您获得了" in processed_text:
            return {"success": True, "message": processed_text}
        if "已经签到" in processed_text or "已签到" in processed_text or "重复签到" in processed_text:
            return {"success": False, "message": processed_text + " (重复签到)"}
        
        if "Done" in processed_text:
            return {"success": False, "message": processed_text + " (待判断)"}

        return {"success": False, "message": processed_text + " (未知情况/需策略特定解析)"}

    async def execute(self):
        raise NotImplementedError("子类必须实现 execute 方法")

class StartCommandButtonAlertStrategy(CheckinStrategy):
    async def _find_and_click_checkin_button(self, event):
        if event.buttons:
            for row in event.buttons:
                for button_in_row in row:
                    if '签到' in button_in_row.text:
                        self.logger.info(f"用户 {self.nickname_for_logging}: 检测到签到按钮: {button_in_row.text}")
                        try:
                            if event.chat_id == self.target_entity.id:
                                click_callback_result = await button_in_row.click()
                                self.logger.info(f"用户 {self.nickname_for_logging}: '签到'按钮已点击。click() 返回类型: {type(click_callback_result)}")
                                return True, click_callback_result
                            else:
                                self.logger.warning(f"用户 {self.nickname_for_logging}: 按钮所在消息的chat_id ({event.chat_id}) 与目标实体ID ({self.target_entity.id}) 不匹配。")
                                return False, None
                        except Exception as e_click:
                            self.logger.error(f"用户 {self.nickname_for_logging}: 点击签到按钮时失败: {e_click}")
                            return True, e_click
        return False, None

    async def _handle_alert_or_prepare_follow_up(self, click_callback_result):
        parsed_result_from_alert = None
        needs_follow_up = True

        if isinstance(click_callback_result, Exception):
             return {"success": False, "message": f"点击签到按钮失败: {click_callback_result}"}, False

        if hasattr(click_callback_result, 'message') and click_callback_result.message:
            alert_message_text = click_callback_result.message
            self.logger.info(f"用户 {self.nickname_for_logging}: 按钮点击后收到弹框提示: {alert_message_text}")
            parsed_result_from_alert = await self._parse_response_text(alert_message_text)

            if parsed_result_from_alert["success"] or ("重复签到" in parsed_result_from_alert["message"]):
                needs_follow_up = False
            elif "待判断" in parsed_result_from_alert["message"]:
                needs_follow_up = True
            else:
                parsed_result_from_alert = {"success": False, "message": alert_message_text + " (弹框内容未知/非决定性)"}
                needs_follow_up = False
            
            return parsed_result_from_alert, needs_follow_up
        else:
            self.logger.info(f"用户 {self.nickname_for_logging}: 按钮点击后未收到有效弹框提示 (click_callback_result类型: {type(click_callback_result)})，将检查后续聊天消息。")
            return {"success": False, "message": "按钮已点击，等待后续聊天消息。"}, True

    async def _process_follow_up_message(self):
        self.logger.info(f"用户 {self.nickname_for_logging}: 等待后续聊天消息 (默认2.5秒)。")
        await asyncio.sleep(2.5) 
        messages_after_click = await self.client.get_messages(self.target_entity, limit=1)
        if messages_after_click:
            if messages_after_click[0].sender_id == self.target_entity.id or messages_after_click[0].sender_id == (await self.client.get_me()).id :
                chat_response_text = messages_after_click[0].text
                self.logger.info(f"用户 {self.nickname_for_logging}: 机器人后续聊天响应: {chat_response_text}")
                return await self._parse_response_text(chat_response_text)
            else:
                self.logger.warning(f"用户 {self.nickname_for_logging}: 收到的最新消息并非来自目标机器人/实体。")
                return {"success": False, "message": "收到的最新消息并非来自目标机器人/实体。"}
        else:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 点击按钮后也未收到聊天响应。")
            return {"success": False, "message": "按钮已点击，但未收到机器人后续响应（弹框或聊天消息）。"}

    async def execute(self):
        self.logger.info(f"用户 {self.nickname_for_logging}: 使用 StartCommandButtonAlertStrategy 开始执行操作。")
        await self.send_command('/start')

        action_event = asyncio.Event()
        self.button_click_attempted_event = asyncio.Event()
        self.first_click_lock = asyncio.Lock()
        current_result = {"success": False, "message": "操作过程未启动或未完成."}

        async def bot_response_handler(event):
            nonlocal current_result
            if event.chat_id != self.target_entity.id:
                return

            if action_event.is_set():
                self.logger.debug(f"用户 {self.nickname_for_logging}: 主操作事件已完成或超时，忽略新的消息事件。")
                return

            response_message_text_capture = event.raw_text[:100]
            target_display_name = getattr(self.target_entity, 'username', getattr(self.target_entity, 'title', str(self.target_entity.id)))
            self.logger.info(f"用户 {self.nickname_for_logging}: 收到来自 {target_display_name} 的消息: {response_message_text_capture}...")

            is_responsible_for_first_click = False
            try:
                if not self.button_click_attempted_event.is_set():
                    async with self.first_click_lock:
                        if not self.button_click_attempted_event.is_set():
                            is_responsible_for_first_click = True
                            self.button_click_attempted_event.set()

                if is_responsible_for_first_click:
                    clicked_button, click_obj = await self._find_and_click_checkin_button(event)
                    if clicked_button:
                        interim_result, needs_follow_up = await self._handle_alert_or_prepare_follow_up(click_obj)
                        if needs_follow_up:
                            final_result_val = await self._process_follow_up_message()
                            current_result = final_result_val
                        else:
                            current_result = interim_result
                    else:
                        self.logger.info(f"用户 {self.nickname_for_logging}: 在首次处理的消息中未找到'签到'按钮，解析当前消息文本。")
                        current_result = await self._parse_response_text(event.raw_text)
                    
                    if not action_event.is_set():
                        action_event.set()
                else:
                    if not action_event.is_set():
                        self.logger.debug(f"用户 {self.nickname_for_logging}: 按钮点击已由其他handler尝试/处理，解析此后续消息: {response_message_text_capture}")
                        parsed_follow_up = await self._parse_response_text(event.raw_text)

                        is_initial_state = current_result["message"] == "操作过程未启动或未完成." or "等待后续聊天消息" in current_result["message"]
                        
                        if parsed_follow_up["success"]:
                            self.logger.info(f"用户 {self.nickname_for_logging}: 后续消息确认为成功: {parsed_follow_up}")
                            current_result = parsed_follow_up
                            action_event.set()
                        elif "重复签到" in parsed_follow_up["message"] and is_initial_state:
                            self.logger.info(f"用户 {self.nickname_for_logging}: 后续消息确认为已签到/重复: {parsed_follow_up}")
                            current_result = parsed_follow_up
                            action_event.set()
                        elif not parsed_follow_up["success"] and is_initial_state and "待判断" not in parsed_follow_up["message"]:
                             self.logger.info(f"用户 {self.nickname_for_logging}: 后续消息为明确失败状态: {parsed_follow_up}，更新结果。")
                             current_result = parsed_follow_up
                             action_event.set()
                        else:
                             self.logger.debug(f"用户 {self.nickname_for_logging}: 后续消息 '{response_message_text_capture}' 未立即更新结果或设置事件。当前结果: {current_result}")
            
            except Exception as e_handler:
                self.logger.error(f"用户 {self.nickname_for_logging}: 处理机器人响应时发生意外错误: {e_handler}", exc_info=True)
                current_result = {"success": False, "message": f"处理机器人响应时发生意外错误: {e_handler}"}
                if not action_event.is_set():
                    action_event.set()

        active_handler = self.client.add_event_handler(bot_response_handler, events.NewMessage(chats=self.target_entity.id))
        target_display_name_log = getattr(self.target_entity, 'username', getattr(self.target_entity, 'title', str(self.target_entity.id)))
        self.logger.info(f"用户 {self.nickname_for_logging}: 等待来自 {target_display_name_log} 的响应 (超时: {self.timeout_seconds} 秒)...")

        try:
            await asyncio.wait_for(action_event.wait(), timeout=self.timeout_seconds)
            if current_result["success"]:
                self.logger.info(f"用户 {self.nickname_for_logging}: 操作成功。")
            else:
                self.logger.warning(f"用户 {self.nickname_for_logging}: 操作失败或未明确成功。消息: {current_result.get('message')}")
        except asyncio.TimeoutError:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 等待响应超时。")
            if current_result["message"] == "操作过程未启动或未完成.": 
                 current_result = {"success": False, "message": "等待响应超时（未收到任何可处理消息或按钮点击未完成）。"}
        finally:
            if self.client and self.client.is_connected() and active_handler:
                try:
                    self.client.remove_event_handler(active_handler)
                    self.logger.info(f"用户 {self.nickname_for_logging}: 事件处理器已移除。")
                except Exception as e_remove_handler:
                    self.logger.error(f"用户 {self.nickname_for_logging}: 移除事件处理器失败: {e_remove_handler}")
        
        return current_result

class CheckinCommandTextStrategy(CheckinStrategy):
    async def execute(self):
        self.logger.info(f"用户 {self.nickname_for_logging}: 使用 CheckinCommandTextStrategy 开始执行操作。")
        
        response_message_text = ""
        result = {"success": False, "message": "与目标对话失败或响应超时。"}
        target_display_name = getattr(self.target_entity, 'username', getattr(self.target_entity, 'title', str(self.target_entity.id)))

        try:
            command_to_send = self.task_config.get("command", "/checkin")

            async with self.client.conversation(self.target_entity, timeout=self.timeout_seconds) as conv:
                await conv.send_message(command_to_send)
                self.logger.info(f"用户 {self.nickname_for_logging}: (对话内)已发送命令 '{command_to_send}' 给 {target_display_name}")
                
                response = await conv.get_response()
                response_message_text = response.text
                self.logger.info(f"用户 {self.nickname_for_logging}: 收到来自 {target_display_name} 的响应: {response_message_text[:100]}...")
                result = await self._parse_response_text(response_message_text)
        except asyncio.TimeoutError:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 等待响应超时。")
            result = {"success": False, "message": "等待响应超时。"}
        except Exception as e:
            self.logger.error(f"用户 {self.nickname_for_logging}: 处理响应时发生错误: {e}", exc_info=True)
            result = {"success": False, "message": f"处理响应时发生错误: {e}"}
            
        return result

class SendMessageToChatStrategy(CheckinStrategy):
    async def execute(self):
        self.logger.info(f"用户 {self.nickname_for_logging}: 使用 SendMessageToChatStrategy 发送消息。")
        
        message_content = self.task_config.get("message_content")
        if not message_content:
            self.logger.error(f"用户 {self.nickname_for_logging}: 任务配置中未找到 'message_content'。")
            return {"success": False, "message": "消息内容未在任务中配置。"}

        target_display_name = getattr(self.target_entity, 'title', str(self.target_entity.id))
        try:
            await self.client.send_message(self.target_entity, message_content)
            self.logger.info(f"用户 {self.nickname_for_logging}: 消息已成功发送到 {target_display_name}。")
            return {"success": True, "message": f"消息已成功发送到 {target_display_name}。"}
        except errors.ChatWriteForbiddenError:
            self.logger.error(f"用户 {self.nickname_for_logging}: 没有权限向 {target_display_name} 发送消息。")
            return {"success": False, "message": f"没有权限向 {target_display_name} 发送消息。"}
        except Exception as e:
            self.logger.error(f"用户 {self.nickname_for_logging}: 发送消息到 {target_display_name} 时发生错误: {e}", exc_info=True)
            return {"success": False, "message": f"发送消息时发生错误: {e}"}

STRATEGY_MAPPING = {
    "start_button_alert": StartCommandButtonAlertStrategy,
    "checkin_text": CheckinCommandTextStrategy,
    "send_custom_message": SendMessageToChatStrategy,
}

STRATEGY_DISPLAY_NAMES = {
    "start_button_alert": {"name": "/start+签到按钮", "target_type": "bot"},
    "checkin_text": {"name": "/checkin直接签到", "target_type": "bot"},
    "send_custom_message": {"name": "发送自定义消息", "target_type": "chat"},
}

def get_strategy_class(strategy_identifier):
    return STRATEGY_MAPPING.get(strategy_identifier)

def get_strategy_display_name(strategy_identifier):
    strategy_info = STRATEGY_DISPLAY_NAMES.get(strategy_identifier)
    if isinstance(strategy_info, dict):
        return strategy_info.get("name", strategy_identifier)
    return strategy_identifier
