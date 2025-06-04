import asyncio, re
from telethon import events, errors

class CheckinStrategy:
    def __init__(self, client, target_entity, logger, nickname_for_logging, task_config=None):
        self.client = client
        self.target_entity = target_entity
        self.logger = logger
        self.nickname_for_logging = nickname_for_logging
        self.task_config = task_config if task_config else {}
        self.timeout_seconds = 10

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
        if "请明天再来" in processed_text:
            return {"success": False, "message": processed_text + " (重复签到)"}
        if "Done" in processed_text or "开始签到验证" in processed_text:
            return {"success": False, "message": processed_text + " (待判断/验证流程)"}

        return {"success": False, "message": processed_text + " (未知情况/需策略特定解析)"}

    async def _click_button_in_message(self, message_obj, keywords, is_answer_logic=False):
        """
        通用方法：在给定的消息对象中查找并点击符合关键词的按钮。
        :param message_obj: telethon.tl.custom.message.Message 对象
        :param keywords: 字符串列表，用于匹配按钮文本
        :param is_answer_logic: 布尔值，True表示精确匹配 (通常用于答案按钮)，False表示包含匹配 (通常用于初始按钮)
        :return: button.click() 的结果 (通常是 BotCallbackAnswer), 或 Exception (如果点击失败), 或 None (如果未找到按钮)
        """
        if not message_obj or not hasattr(message_obj, 'buttons') or not message_obj.buttons:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 消息 (ID: {message_obj.id if message_obj else 'N/A'}) 中没有按钮可供点击 (关键词: {keywords})。")
            return None

        for row_idx, row in enumerate(message_obj.buttons):
            for btn_idx, button in enumerate(row):
                button_text_matches = False
                try:
                    current_button_text = button.text.strip()
                    if is_answer_logic:
                        button_text_matches = current_button_text == keywords[0]
                    else:
                        button_text_matches = any(kw in current_button_text for kw in keywords)
                except AttributeError:
                    self.logger.debug(f"用户 {self.nickname_for_logging}: 按钮对象 (行{row_idx},列{btn_idx}) 缺少 'text' 属性。")
                    continue

                if button_text_matches:
                    self.logger.info(f"用户 {self.nickname_for_logging}: 找到按钮 (行{row_idx},列{btn_idx}): '{current_button_text}' (匹配关键词: {keywords}) 在消息 ID {message_obj.id}")
                    try:
                        if message_obj.chat_id != self.target_entity.id:
                            self.logger.warning(f"用户 {self.nickname_for_logging}: 按钮所在消息的chat_id ({message_obj.chat_id}) 与目标实体ID ({self.target_entity.id}) 不匹配。不点击。")
                            return None
                        return await button.click()
                    except Exception as e:
                        self.logger.error(f"用户 {self.nickname_for_logging}: 点击按钮 '{current_button_text}' (消息 ID {message_obj.id}) 失败: {e}", exc_info=True)
                        return e
        self.logger.warning(f"用户 {self.nickname_for_logging}: 在消息 ID {message_obj.id} 中未找到符合关键词 '{keywords}' 的按钮。")
        return None

    async def _execute_initial_step(self, command_to_send, initial_button_keywords):
        """
        执行签到策略的初始步骤：发送命令，等待响应，并点击初始按钮。
        :param command_to_send: 要发送的初始命令 (例如 '/start')
        :param initial_button_keywords: 字符串列表，用于匹配初始按钮的关键词 (例如 ['签到'])
        :return: 元组 (click_obj, source_message, error_obj)
                 click_obj: 按钮点击的结果 (BotCallbackAnswer 或 Exception) 或 None
                 source_message: 包含按钮的消息对象或 None
                 error_obj: Exception (如 TimeoutError) 或 None
        """
        await self.send_command(command_to_send)
        
        action_taken_event = asyncio.Event()
        result_holder = {"value": (None, None, None)}


        async def temp_handler(event):
            if event.chat_id != self.target_entity.id:
                return
            
            if event.sender_id != self.target_entity.id:
                 return

            if action_taken_event.is_set():
                self.logger.debug(f"用户 {self.nickname_for_logging}: (_execute_initial_step) action_taken_event 已设置，忽略消息。")
                return

            self.logger.info(f"用户 {self.nickname_for_logging}: (_execute_initial_step) 收到来自机器人 {event.sender_id} 的消息 (ID: {event.message.id})，尝试寻找按钮 {initial_button_keywords}")
            message_obj = event.message
            click_obj = await self._click_button_in_message(message_obj, initial_button_keywords, is_answer_logic=False)
            
            result_holder["value"] = (click_obj, message_obj, None)
            if not action_taken_event.is_set():
                action_taken_event.set()

        handler_new_msg = self.client.add_event_handler(temp_handler, events.NewMessage(chats=self.target_entity.id, from_users=self.target_entity.id))
        handler_edit_msg = self.client.add_event_handler(temp_handler, events.MessageEdited(chats=self.target_entity.id, from_users=self.target_entity.id))
        
        target_display_name_log = getattr(self.target_entity, 'username', getattr(self.target_entity, 'title', str(self.target_entity.id)))
        self.logger.info(f"用户 {self.nickname_for_logging}: (_execute_initial_step) 等待来自 {target_display_name_log} 的初始响应 (超时: {self.timeout_seconds} 秒)...")

        try:
            await asyncio.wait_for(action_taken_event.wait(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            self.logger.warning(f"用户 {self.nickname_for_logging}: (_execute_initial_step) 等待初始响应超时。")
            result_holder["value"] = (None, None, asyncio.TimeoutError("等待初始响应超时"))
        finally:
            if self.client and self.client.is_connected():
                self.client.remove_event_handler(handler_new_msg)
                self.client.remove_event_handler(handler_edit_msg)
        
        return result_holder["value"]

    async def execute(self):
        raise NotImplementedError("子类必须实现 execute 方法")

class StartCommandButtonAlertStrategy(CheckinStrategy):

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
            if messages_after_click[0].sender_id == self.target_entity.id or \
               messages_after_click[0].sender_id == (await self.client.get_me()).id:
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
        
        click_obj, source_message, error = await self._execute_initial_step('/start', ['签到'])

        if error:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 初始步骤失败: {error}")
            if isinstance(error, asyncio.TimeoutError):
                 return {"success": False, "message": "等待机器人响应或按钮超时。"}
            return {"success": False, "message": f"初始步骤遇到错误: {error}"}

        if click_obj and isinstance(click_obj, Exception):
            self.logger.error(f"用户 {self.nickname_for_logging}: 点击初始签到按钮时发生错误: {click_obj}")
            return {"success": False, "message": f"点击初始签到按钮失败: {click_obj}"}
        
        if click_obj:
            self.logger.info(f"用户 {self.nickname_for_logging}: 初始按钮点击成功或尝试点击。")
            interim_result, needs_follow_up = await self._handle_alert_or_prepare_follow_up(click_obj)
            
            if needs_follow_up:
                return await self._process_follow_up_message()
            else:
                return interim_result
        elif source_message:
            self.logger.info(f"用户 {self.nickname_for_logging}: 初始步骤未点击按钮 (可能未找到)，但收到消息。解析消息文本: {source_message.raw_text[:70]}...")
            return await self._parse_response_text(source_message.raw_text)
        else:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 初始步骤未收到响应，也未能点击按钮，且无明确错误。")
            return {"success": False, "message": "操作未能完成：未收到机器人响应或未能点击初始按钮。"}

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

class MathCaptchaStrategy(CheckinStrategy):
    def __init__(self, client, target_entity, logger, nickname_for_logging, task_config=None):
        super().__init__(client, target_entity, logger, nickname_for_logging, task_config)
        self.initial_button_text_keywords = task_config.get("initial_button_keywords", ['签到'])
        self.action_event = None
        self.timeout_seconds = task_config.get("timeout", 30) 

    def _solve_math_problem(self, problem_text):
        self.logger.info(f"用户 {self.nickname_for_logging}: 尝试解析数学问题: '{problem_text}'")
        match = re.search(r'(\d+)\s*([+\-*\/])\s*(\d+)\s*=\s*\?', problem_text)
        if match:
            num1, operator, num2 = match.groups()
            num1, num2 = int(num1), int(num2)
            self.logger.info(f"用户 {self.nickname_for_logging}: 解析到数字1: {num1}, 运算符: {operator}, 数字2: {num2}")
            if operator == '+': return num1 + num2
            elif operator == '-': return num1 - num2
            elif operator == '*': return num1 * num2
            elif operator == '/': 
                if num2 == 0:
                    self.logger.error(f"用户 {self.nickname_for_logging}: 数学问题除数为零: {problem_text}")
                    return None
                return num1 / num2
        self.logger.warning(f"用户 {self.nickname_for_logging}: 无法从文本中解析数学问题: '{problem_text}'")
        return None

    async def _handle_captcha_message_and_click_answer(self, message_obj_from_event):
        self.logger.info(f"用户 {self.nickname_for_logging}: 处理验证码消息 (来自事件 ID: {message_obj_from_event.id}): {message_obj_from_event.raw_text[:70]}...")
        
        message_to_use_for_buttons = message_obj_from_event
        try:
            refetched_message = await self.client.get_messages(self.target_entity, ids=message_obj_from_event.id)
            if refetched_message:
                buttons_repr_refetch = "No buttons"
                if hasattr(refetched_message, 'buttons') and refetched_message.buttons:
                    buttons_list = []
                    for r_idx, r_val in enumerate(refetched_message.buttons):
                        row_buttons_list = []
                        if r_val:
                            for b_val in r_val:
                                button_text = getattr(b_val, 'text', '[NoTextAttr]')
                                row_buttons_list.append(f"Button(text='{button_text}')")
                        buttons_list.append(f"Row{r_idx}: [{', '.join(row_buttons_list)}]")
                    buttons_repr_refetch = "; ".join(buttons_list)
                self.logger.info(f"用户 {self.nickname_for_logging}: 重新获取消息 ID {refetched_message.id} 成功. 按钮: {buttons_repr_refetch}")
                
                if refetched_message.buttons:
                    message_to_use_for_buttons = refetched_message
                else:
                    self.logger.info(f"用户 {self.nickname_for_logging}: 重新获取的消息中无按钮，仍使用事件中的消息对象按钮。")
            else:
                self.logger.warning(f"用户 {self.nickname_for_logging}: 重新获取消息 ID {message_obj_from_event.id} 失败或返回空，将使用事件中的消息对象。")
        except Exception as e_refetch:
            self.logger.error(f"用户 {self.nickname_for_logging}: 重新获取消息 ID {message_obj_from_event.id} 时出错: {e_refetch}", exc_info=True)

        problem_text = message_to_use_for_buttons.raw_text
        answer = self._solve_math_problem(problem_text)

        if answer is None:
            return {"success": False, "message": "无法计算数学验证码答案。"}

        answer_str = str(int(answer))
        self.logger.info(f"用户 {self.nickname_for_logging}: 计算答案为: {answer_str}")

        click_obj_answer = await self._click_button_in_message(message_to_use_for_buttons, [answer_str], is_answer_logic=True)

        if click_obj_answer is None:
            return {"success": False, "message": f"未找到答案按钮 '{answer_str}'。"}
        if isinstance(click_obj_answer, Exception):
            return {"success": False, "message": f"点击答案按钮 '{answer_str}' 失败: {click_obj_answer}"}

        if hasattr(click_obj_answer, 'message') and click_obj_answer.message:
            alert_text_final = click_obj_answer.message
            self.logger.info(f"用户 {self.nickname_for_logging}: 点击答案后收到弹框: {alert_text_final}")
            return await self._parse_response_text(alert_text_final)
        else:
            self.logger.warning(f"用户 {self.nickname_for_logging}: 点击答案按钮后未收到预期的弹框确认。")
            return {"success": False, "message": "点击答案后未收到弹框确认。"}

    async def execute(self):
        self.logger.info(f"用户 {self.nickname_for_logging}: 使用 MathCaptchaStrategy (独立Execute) 开始执行操作。")
        self.action_event = asyncio.Event()
        current_result = {"success": False, "message": "操作过程未启动或未完成."}
        
        current_captcha_state = "INIT"
        initial_alert_text_for_timeout_msg = None

        click_obj_initial, source_message_initial, initial_step_error = await self._execute_initial_step(
            self.task_config.get("command", "/start"),
            self.initial_button_text_keywords
        )

        if initial_step_error:
            self.logger.warning(f"用户 {self.nickname_for_logging}: MathCaptcha 初始步骤失败: {initial_step_error}")
            if isinstance(initial_step_error, asyncio.TimeoutError):
                 return {"success": False, "message": "等待机器人响应或初始按钮超时。"}
            return {"success": False, "message": f"初始步骤遇到错误: {initial_step_error}"}

        if click_obj_initial and isinstance(click_obj_initial, Exception):
            self.logger.error(f"用户 {self.nickname_for_logging}: 点击初始签到按钮时发生错误: {click_obj_initial}")
            return {"success": False, "message": f"点击初始签到按钮失败: {click_obj_initial}"}
        
        if not click_obj_initial:
            msg_detail = f"收到的消息: {source_message_initial.raw_text[:70]}..." if source_message_initial else "未收到预期消息。"
            self.logger.warning(f"用户 {self.nickname_for_logging}: 未能点击初始按钮 (关键词: {self.initial_button_text_keywords})。{msg_detail}")
            return {"success": False, "message": f"未能点击初始按钮 (关键词: {self.initial_button_text_keywords})。"}

        if hasattr(click_obj_initial, 'message') and click_obj_initial.message:
            initial_alert_text = click_obj_initial.message
            initial_alert_text_for_timeout_msg = initial_alert_text
            self.logger.info(f"用户 {self.nickname_for_logging}: 初始按钮点击后弹框: '{initial_alert_text}'")
            parsed_alert = await self._parse_response_text(initial_alert_text)

            if parsed_alert["success"] or "重复签到" in parsed_alert["message"] or "已签到" in parsed_alert["message"]:
                self.logger.info(f"用户 {self.nickname_for_logging}: 初始弹框为最终结果: {parsed_alert['message']}")
                return parsed_alert
            elif "待判断/验证流程" in parsed_alert["message"] or "开始签到验证" in parsed_alert["message"]:
                self.logger.info(f"用户 {self.nickname_for_logging}: 弹框指示验证流程 ('{initial_alert_text}')，等待验证码消息。")
                current_captcha_state = "AWAITING_CAPTCHA_MESSAGE"
            else: 
                self.logger.warning(f"用户 {self.nickname_for_logging}: 初始弹框内容未知或非预期 ('{initial_alert_text}'), 解析为: {parsed_alert['message']}")
                return parsed_alert
        else: 
            self.logger.info(f"用户 {self.nickname_for_logging}: 初始按钮点击后无弹框，假定直接进入等待验证码消息流程。")
            current_captcha_state = "AWAITING_CAPTCHA_MESSAGE"
        
        if current_captcha_state != "AWAITING_CAPTCHA_MESSAGE":
            self.logger.error(f"用户 {self.nickname_for_logging}: MathCaptcha 逻辑错误，未进入验证码流程但未返回最终结果。")
            return {"success": False, "message": "内部逻辑错误，未能确定后续操作。"}

        active_captcha_handler = None
        try:
            @self.client.on(events.NewMessage(chats=self.target_entity.id, from_users=self.target_entity.id))
            @self.client.on(events.MessageEdited(chats=self.target_entity.id, from_users=self.target_entity.id))
            async def captcha_message_handler(event):
                nonlocal current_result, current_captcha_state
                
                if self.action_event.is_set():
                    self.logger.debug(f"用户 {self.nickname_for_logging} (MathCaptchaHandler): action_event 已设置，忽略消息。")
                    return

                message_text_for_log = "[非消息事件或无文本]"
                actual_message_obj = None
                if hasattr(event, 'message') and event.message:
                    actual_message_obj = event.message
                    if hasattr(actual_message_obj, 'raw_text'):
                        message_text_for_log = actual_message_obj.raw_text[:70] if actual_message_obj.raw_text else "[空消息]"
                    elif hasattr(actual_message_obj, 'text'):
                        message_text_for_log = actual_message_obj.text[:70] if actual_message_obj.text else "[空消息]"
                
                self.logger.info(f"用户 {self.nickname_for_logging} (MathCaptchaHandler): 状态 '{current_captcha_state}', 收到事件类型 '{type(event).__name__}', 消息: {message_text_for_log}...")

                if not actual_message_obj:
                    self.logger.debug(f"用户 {self.nickname_for_logging} (MathCaptchaHandler): 事件非预期类型或无 message 属性，忽略。")
                    return

                if current_captcha_state == "AWAITING_CAPTCHA_MESSAGE":
                    text_to_check = actual_message_obj.raw_text if actual_message_obj.raw_text else ""
                    is_likely_captcha = False
                    if re.search(r'\d+\s*[+\-*\/]\s*\d+\s*=\s*\?', text_to_check):
                        is_likely_captcha = True
                    elif actual_message_obj.buttons:
                        numerical_buttons_count = 0
                        for row in actual_message_obj.buttons:
                            for button_in_row in row:
                                if hasattr(button_in_row, 'text') and button_in_row.text.strip().isdigit():
                                    numerical_buttons_count += 1
                        if numerical_buttons_count >= 2:
                            is_likely_captcha = True
                    
                    if not is_likely_captcha:
                        self.logger.info(f"用户 {self.nickname_for_logging} (MathCaptchaHandler): 状态 'AWAITING_CAPTCHA_MESSAGE'，但收到消息 '{text_to_check[:50]}' 不像验证码，继续等待或依赖编辑。")
                        if isinstance(event, events.NewMessage.Event) and not is_likely_captcha:
                             return

                    self.logger.info(f"用户 {self.nickname_for_logging} (MathCaptchaHandler): 状态 'AWAITING_CAPTCHA_MESSAGE'，收到疑似验证码消息/编辑，开始处理。")
                    current_result = await self._handle_captcha_message_and_click_answer(actual_message_obj)
                    current_captcha_state = "DONE"
                
                if current_captcha_state == "DONE" and not self.action_event.is_set():
                    self.action_event.set()

            active_captcha_handler = captcha_message_handler 
            self.client.add_event_handler(active_captcha_handler, events.NewMessage(chats=self.target_entity.id, from_users=self.target_entity.id))
            self.client.add_event_handler(active_captcha_handler, events.MessageEdited(chats=self.target_entity.id, from_users=self.target_entity.id))

            self.logger.info(f"用户 {self.nickname_for_logging}: MathCaptcha等待验证码消息 (超时: {self.timeout_seconds} 秒)...")
            await asyncio.wait_for(self.action_event.wait(), timeout=self.timeout_seconds)
            self.logger.info(f"用户 {self.nickname_for_logging}: MathCaptchaStrategy 操作完成。最终结果: {current_result}")

        except asyncio.TimeoutError:
            self.logger.warning(f"用户 {self.nickname_for_logging}: MathCaptchaStrategy 超时 (验证码阶段状态: {current_captcha_state})。")
            if current_captcha_state == "AWAITING_CAPTCHA_MESSAGE":
                current_result = {"success": False, "message": f"等待验证码消息超时。初始弹框提示: '{initial_alert_text_for_timeout_msg}'"}
            else:
                 current_result = {"success": False, "message": f"操作超时，当前验证码阶段状态: {current_captcha_state}, 部分结果: {current_result.get('message')}"}
        except Exception as e_execute:
            self.logger.error(f"用户 {self.nickname_for_logging}: MathCaptchaStrategy execute 发生意外错误: {e_execute}", exc_info=True)
            current_result = {"success": False, "message": f"执行策略时发生意外错误: {e_execute}"}
        finally:
            if active_captcha_handler and self.client and self.client.is_connected():
                try:
                    self.client.remove_event_handler(active_captcha_handler, events.NewMessage)
                    self.client.remove_event_handler(active_captcha_handler, events.MessageEdited)
                    self.logger.info(f"用户 {self.nickname_for_logging}: MathCaptchaStrategy 事件处理器已移除。")
                except Exception as e_remove:
                    self.logger.error(f"用户 {self.nickname_for_logging}: 移除 MathCaptchaStrategy 事件处理器失败: {e_remove}")
            if not self.action_event.is_set(): 
                self.action_event.set() 
        
        return current_result

STRATEGY_MAPPING = {
    "start_button_alert": StartCommandButtonAlertStrategy,
    "checkin_text": CheckinCommandTextStrategy,
    "send_custom_message": SendMessageToChatStrategy,
    "math_captcha_checkin": MathCaptchaStrategy,
}

STRATEGY_DISPLAY_NAMES = {
    "start_button_alert": {"name": "点击签到按钮", "target_type": "bot", "config_params": ["timeout"]},
    "checkin_text": {"name": "发送/checkin", "target_type": "bot", "config_params": ["command", "timeout"]},
    "send_custom_message": {"name": "发送自定义消息", "target_type": "chat", "config_params": ["message_content"]},
    "math_captcha_checkin": {"name": "签到按钮+验证", "target_type": "bot", "config_params": ["command", "initial_button_keywords", "timeout"]},
}

def get_strategy_class(strategy_identifier):
    return STRATEGY_MAPPING.get(strategy_identifier)

def get_strategy_display_name(strategy_identifier):
    strategy_info = STRATEGY_DISPLAY_NAMES.get(strategy_identifier)
    if isinstance(strategy_info, dict):
        return strategy_info.get("name", strategy_identifier)
    return strategy_identifier
