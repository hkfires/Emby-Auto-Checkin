import logging, os, asyncio, httpx, base64, json, sqlite3, threading
from openai import AsyncOpenAI
from flask import Blueprint, request, jsonify, current_app, flash
from flask_login import login_required
from utils.config import load_config, save_config
from utils.log import (
    STALE_TASK_STATE_SECONDS,
    beijing_today_str,
    claim_daily_task,
    complete_daily_task,
    ensure_daily_task_rows,
    get_daily_task_counts,
    queue_daily_tasks,
    refresh_queued_batch,
    save_daily_checkin_log,
    start_queued_task,
    task_identity,
    task_identity_from_config,
)
from utils.tgservice_api import execute_action, manage_session
from utils.scheduler_api import notify_scheduler_to_reconcile
from tgservice.checkin_strategies import STRATEGY_MAPPING, get_strategy_display_name

api = Blueprint('api', __name__)
logger = logging.getLogger(__name__)
temp_otp_store = {}
QUICK_QUEUE_HEARTBEAT_SECONDS = max(1, min(60, STALE_TASK_STATE_SECONDS // 3))

@api.route('/llm/test', methods=['POST'])
@login_required
async def test_llm_connection():
    base_api_url = request.form.get('api_url', '').strip().rstrip('/')
    api_key = request.form.get('api_key')
    model_name = request.form.get('model_name')
    if not all([base_api_url, api_key, model_name]):
        return jsonify({"success": False, "message": "API URL, API Key 和模型名称均不能为空。"}), 400

    client = AsyncOpenAI(
        base_url=f"{base_api_url}/v1",
        api_key=api_key,
    )

    image_path = os.path.join(current_app.static_folder, 'test_image.png')
    if not os.path.exists(image_path):
        return jsonify({"success": False, "message": "测试失败：未找到测试图片 static/test_image.png。请先放置一张图片用于测试。"}), 400
    
    with open(image_path, "rb") as image_file:
        test_image_base64 = base64.b64encode(image_file.read()).decode('utf-8')

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这是一张图片问答截图。请仔细观察图片上半部分的主要物体，然后在下方的几个文字选项按钮中，选择一个最能准确描述该物体的词语。请直接返回你选择的那个词语，不要包含任何其他文字、解释或标点符号。如果你不支持读取图片并识别，则明确说明当前模型不支持图片识别。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{test_image_base64}"}}
            ]
        }
    ]

    try:
        full_content = ""
        stream = await client.chat.completions.create(
            model=model_name,
            messages=messages,
            stream=True,
            timeout=60
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                full_content += chunk.choices[0].delta.content

        if full_content:
            if full_content.strip() == "路由器":
                config = load_config()
                llm_settings = config.get('llm_settings', {})
                llm_settings['api_url'] = base_api_url
                llm_settings['api_key'] = api_key
                llm_settings['model_name'] = model_name
                config['llm_settings'] = llm_settings
                save_config(config)
                
                message = f"测试成功。模型正确识别出了图片内容为：{full_content}"
                return jsonify({"success": True, "message": message})
            elif "不支持图片识别" in full_content:
                message = f"测试失败。模型不支持图片识别，返回结果：{full_content}"
                return jsonify({"success": False, "message": message})
            else:
                message = f"测试失败。模型返回了非预期的结果：{full_content}"
                return jsonify({"success": False, "message": message})
        else:
            message = "连接成功，但未能从API响应中解析出任何有效内容。"
            return jsonify({"success": False, "message": message})

    except Exception as e:
        logger.error(f"测试LLM API连接时发生未知错误: {e}")
        return jsonify({"success": False, "message": f"发生未知错误: {e}"}), 500

@api.route('/llm/models', methods=['POST'])
@login_required
async def get_llm_models():
    base_api_url = request.form.get('api_url', '').strip().rstrip('/')
    api_key = request.form.get('api_key')

    if not all([base_api_url, api_key]):
        return jsonify({"success": False, "message": "API URL 和 API Key 均不能为空。"}), 400

    client = AsyncOpenAI(
        base_url=f"{base_api_url}/v1",
        api_key=api_key,
    )

    try:
        models = await client.models.list()
        models_data = [model.dict() for model in models.data]
        return jsonify({"success": True, "models": models_data})
    except Exception as e:
        logger.error(f"获取LLM模型列表时发生错误: {e}")
        return jsonify({"success": False, "message": f"发生错误: {e}"}), 500

@api.route('/users/add', methods=['POST'])
async def add_user():
    config = load_config()
    if not config.get('api_id') or not config.get('api_hash'):
        return jsonify({"success": False, "message": "请先在API���置中配置API ID和Hash。"}), 400

    phone = request.form.get('phone')
    if not phone:
        return jsonify({"success": False, "message": "未提供手机号码。"}), 400

    if any(u.get('phone') == phone for u in config.get('users', [])):
        return jsonify({"success": False, "message": "该手机号码已经添加过。"}), 400

    from utils.tgservice_api import send_code
    result = await send_code(phone)

    if result.get("success"):
        temp_otp_store[phone] = {"hash": result.get("phone_code_hash")}
        return jsonify({"success": True, "message": "验证码已发送，请输入验证码。", "needs_otp": True, "phone": phone})
    else:
        return jsonify({"success": False, "message": result.get("message", "发送验证码失败。")}), 500

@api.route('/users/submit_otp', methods=['POST'])
async def submit_otp():
    phone = request.form.get('phone')
    otp_code = request.form.get('otp_code')
    password = request.form.get('password') 

    if not phone or not otp_code:
        return jsonify({"success": False, "message": "未提供手机号或验证码。"}), 400

    otp_data = temp_otp_store.get(phone)
    if not otp_data or 'hash' not in otp_data:
        return jsonify({"success": False, "message": "无法验证OTP：未找到之前的请求或请求已超时。"}), 400
    
    phone_code_hash = otp_data['hash']

    from utils.tgservice_api import sign_in
    result = await sign_in(phone, otp_code, phone_code_hash, password)

    if result.get("success"):
        config = load_config()
        user_info = result.get("user_info")
        
        if not any(u.get('telegram_id') == user_info.get('telegram_id') for u in config.get('users', [])):
            new_user_data = {
                "telegram_id": user_info.get("telegram_id"),
                "nickname": user_info.get("nickname"),
                "phone": user_info.get("phone"),
                "session_name": user_info.get("session_name"),
                "status": "logged_in"
            }
            config['users'].append(new_user_data)
            save_config(config)
            
        if phone in temp_otp_store:
            del temp_otp_store[phone]
            
        return jsonify({"success": True, "message": "登录成功！"})
        
    elif result.get("status") == "2fa_needed":
        return jsonify({"success": False, "status": "2fa_needed", "message": "需要两步验证密码。"})
        
    else:
        return jsonify({"success": False, "message": result.get("message", "登录失败。")}), 500

@api.route('/users/delete', methods=['POST'])
async def delete_user():
    config = load_config()
    nickname_to_delete = request.form.get('nickname')
    if not nickname_to_delete:
        return jsonify({"success": False, "message": "未提供用户昵称。"}), 400

    original_user_count = len(config.get('users', []))
    
    user_to_delete_obj = next((u for u in config.get('users', []) if u.get('nickname') == nickname_to_delete), None)

    if not user_to_delete_obj:
        return jsonify({"success": False, "message": "未找到该昵称的用户。"}), 404

    user_telegram_id_to_delete = user_to_delete_obj.get('telegram_id')
    session_name_to_delete = user_to_delete_obj.get('session_name')

    config['users'] = [u for u in config.get('users', []) if u.get('nickname') != nickname_to_delete]
    
    if 'checkin_tasks' in config and user_telegram_id_to_delete is not None:
        config['checkin_tasks'] = [
            task for task in config.get('checkin_tasks', [])
            if task.get('user_telegram_id') != user_telegram_id_to_delete
        ]

    if session_name_to_delete:
        result = await manage_session(action="remove", session_name=session_name_to_delete, nickname=nickname_to_delete)
        if not result.get("success"):
            logger.warning(f"调用TG服务移除会话 {session_name_to_delete} 失败: {result.get('message')}")

    if len(config.get('users', [])) < original_user_count:
        save_config(config)
        notify_scheduler_to_reconcile()
        logger.info(f"用户 {nickname_to_delete} 已删除。")
        return jsonify({"success": True, "message": f"用户 {nickname_to_delete} 已删除。"})
    else:
        return jsonify({"success": False, "message": "删除用户时发生错误或用户未找到。"}), 404

@api.route('/bots/add', methods=['POST'])
def add_bot():
    config = load_config()
    bot_username_raw = request.form.get('bot_username', '').strip()
    strategy = request.form.get('strategy', 'start_button_alert')

    if not bot_username_raw:
        return jsonify({"success": False, "message": "未提供机器人用户名。"}), 400

    bot_username = bot_username_raw.lstrip('@')

    if not bot_username:
        return jsonify({"success": False, "message": "未提供机器人用户名。"}), 400

    if strategy not in STRATEGY_MAPPING:
        return jsonify({"success": False, "message": f"无效的签到策略: {strategy}。"}), 400

    if 'bots' not in config or not isinstance(config['bots'], list):
        config['bots'] = []
    
    existing_bot = next((b for b in config.get('bots', []) if isinstance(b, dict) and b.get('bot_username') == bot_username), None)

    if not existing_bot:
        new_bot_entry = {"bot_username": bot_username, "strategy": strategy}
        config['bots'].append(new_bot_entry)
        save_config(config)
        logger.info(f"机器人 {bot_username} (策略: {strategy}) 已添加。")
        return jsonify({"success": True, "message": "机器人已添加。"})
    else:
        if existing_bot.get('strategy') != strategy:
             existing_bot['strategy'] = strategy
             save_config(config)
             logger.info(f"机器人 {bot_username} 的策略已更新为: {strategy}。")
             return jsonify({"success": True, "message": f"机器人 {bot_username} 的策略已更新。"})
        else:
            return jsonify({"success": False, "message": "该机器人已存在且策略相同。"}), 400

@api.route('/bots/delete', methods=['POST'])
def delete_bot():
    config = load_config()
    bot_to_delete_username = request.form.get('bot_username')
    if not bot_to_delete_username:
        return jsonify({"success": False, "message": "未提供机器人用户名。"}), 400

    original_bot_count = len(config.get('bots', []))
    config['bots'] = [b for b in config.get('bots', []) if not (isinstance(b, dict) and b.get('bot_username') == bot_to_delete_username)]
    
    if len(config.get('bots',[])) < original_bot_count:
        if 'checkin_tasks' in config:
            config['checkin_tasks'] = [t for t in config['checkin_tasks'] if t.get('bot_username') != bot_to_delete_username]
        save_config(config)
        notify_scheduler_to_reconcile()
        logger.info(f"机器人 {bot_to_delete_username} 已删除。")
        return jsonify({"success": True, "message": "机器人已删除。"})
    else:
        return jsonify({"success": False, "message": "未找到该机器人。"}), 404

@api.route('/tasks/add', methods=['POST'])
def add_task():
    config = load_config()
    user_telegram_id_str = request.form.get('user_telegram_id')
    target_type = request.form.get('target_type')
    selected_time_slot_id_str = request.form.get('selected_time_slot_id')
    
    if not user_telegram_id_str or not target_type:
        return jsonify({"success": False, "message": "未选择用户或目标类型。"}), 400
    
    try:
        user_telegram_id = int(user_telegram_id_str)
    except ValueError:
        return jsonify({"success": False, "message": "无效的用户TG ID格式。"}), 400

    user_for_task = next((u for u in config.get('users', []) if u.get('telegram_id') == user_telegram_id and u.get('status') == 'logged_in'), None)
    if not user_for_task:
        return jsonify({"success": False, "message": "选择的用户无效、未登录或缺少TG ID。"}), 400
    
    user_nickname = user_for_task.get('nickname', f"TGID_{user_telegram_id}")

    new_task = {
        "user_telegram_id": user_telegram_id,
        "selected_time_slot_id": None
    }

    if selected_time_slot_id_str:
        try:
            selected_time_slot_id = int(selected_time_slot_id_str)
            available_slot_ids = [s['id'] for s in config.get('scheduler_time_slots', []) if isinstance(s, dict) and 'id' in s]
            if selected_time_slot_id in available_slot_ids:
                new_task["selected_time_slot_id"] = selected_time_slot_id
            else:
                flash_msg = f"选择的时间段ID '{selected_time_slot_id_str}' 无效。"
                logger.warning(flash_msg)
                if available_slot_ids:
                    new_task["selected_time_slot_id"] = available_slot_ids[0]
                    flash_msg += f" 已自动分配到第一个可用时段 (ID: {available_slot_ids[0]})。"
                else:
                     return jsonify({"success": False, "message": "无法分配任务到时间段：系���中未配置任何时间段。"}), 400
        except ValueError:
            return jsonify({"success": False, "message": "时间段ID格式无效。"}), 400
    else:
        available_slots = config.get('scheduler_time_slots', [])
        if available_slots and isinstance(available_slots[0], dict) and 'id' in available_slots[0]:
            new_task["selected_time_slot_id"] = available_slots[0]['id']
        else:
            return jsonify({"success": False, "message": "无法分配任务到时间段：系统中未配置任何时间段。"}), 400

    if target_type == 'bot':
        bot_username = request.form.get('bot_username')
        if not bot_username:
            return jsonify({"success": False, "message": "未选择机器人。"}), 400
        new_task["bot_username"] = bot_username
        log_target_name = bot_username
    elif target_type == 'chat':
        target_chat_id_str = request.form.get('target_chat_id')
        message_content = request.form.get('message_content', '')

        if not target_chat_id_str:
            return jsonify({"success": False, "message": "未选择群组。"}), 400
        try:
            target_chat_id = int(target_chat_id_str)
        except ValueError:
            return jsonify({"success": False, "message": "无效的群组ID格式。"}), 400
        
        chat_info = next((c for c in config.get('chats', []) if c.get('chat_id') == target_chat_id), None)
        if not chat_info:
            return jsonify({"success": False, "message": "选择的群组未在配置中找到。"}), 400

        new_task["target_chat_id"] = target_chat_id
        new_task["message_content"] = message_content
        log_target_name = chat_info.get('chat_title', str(target_chat_id))
    else:
        return jsonify({"success": False, "message": "无效的目标类型。"}), 400
    
    if 'checkin_tasks' not in config or not isinstance(config['checkin_tasks'], list):
        config['checkin_tasks'] = []

    task_exists = False
    for t in config['checkin_tasks']:
        if t.get('user_telegram_id') == new_task['user_telegram_id']:
            if target_type == 'bot' and t.get('bot_username') == new_task.get('bot_username'):
                task_exists = True
                break
            if target_type == 'chat' and t.get('target_chat_id') == new_task.get('target_chat_id'):
                task_exists = True 
                break
    
    if not task_exists:
        config['checkin_tasks'].append(new_task)
        save_config(config)
        notify_scheduler_to_reconcile()
        logger.info(f"任务已添加: 用户 {user_nickname} -> {log_target_name}")
        return jsonify({"success": True, "message": "任务已添加，调度器将很快进行同步。"})
    else:
        return jsonify({"success": False, "message": "该任务已存在。"}), 400

@api.route('/tasks/add_batch', methods=['POST'])
@login_required
def add_tasks_batch():
    config = load_config()
    user_telegram_id_strs = request.form.getlist('user_telegram_ids[]')
    target_identifiers = request.form.getlist('targets[]')
    selected_time_slot_id_str = request.form.get('selected_time_slot_id')
    message_content = request.form.get('message_content', '')
 
    if not user_telegram_id_strs or not target_identifiers:
        return jsonify({"success": False, "message": "未选择用户或目标。"}), 400

    try:
        user_telegram_ids = [int(id_str) for id_str in user_telegram_id_strs]
    except ValueError:
        return jsonify({"success": False, "message": "无效的用户TG ID格式。"}), 400

    if 'checkin_tasks' not in config or not isinstance(config['checkin_tasks'], list):
        config['checkin_tasks'] = []

    added_count = 0
    existing_count = 0
    
    for user_id in user_telegram_ids:
        for identifier in target_identifiers:
            try:
                target_type, target_id_str = identifier.split(':', 1)
            except ValueError:
                logger.warning(f"批量添加任务时跳过无效的目标标识符: {identifier}")
                continue

            new_task = {
                "user_telegram_id": user_id,
                "selected_time_slot_id": None
            }

            if selected_time_slot_id_str:
                try:
                    selected_time_slot_id = int(selected_time_slot_id_str)
                    available_slot_ids = [s['id'] for s in config.get('scheduler_time_slots', [])]
                    if selected_time_slot_id in available_slot_ids:
                        new_task["selected_time_slot_id"] = selected_time_slot_id
                    elif available_slot_ids:
                        new_task["selected_time_slot_id"] = available_slot_ids[0]
                except (ValueError, IndexError):
                    pass
            else:
                available_slots = config.get('scheduler_time_slots', [])
                if available_slots:
                    new_task["selected_time_slot_id"] = available_slots[0]['id']

            task_exists = False
            if target_type == 'bot':
                new_task["bot_username"] = target_id_str
                task_exists = any(
                    t.get('user_telegram_id') == user_id and t.get('bot_username') == target_id_str
                    for t in config['checkin_tasks']
                )
            elif target_type == 'chat':
                try:
                    target_id = int(target_id_str)
                    new_task["target_chat_id"] = target_id
                    new_task["message_content"] = message_content
                    task_exists = any(
                        t.get('user_telegram_id') == user_id and t.get('target_chat_id') == target_id
                        for t in config['checkin_tasks']
                    )
                except ValueError:
                    logger.warning(f"批量添加任务时跳过无效的群组ID: {target_id_str}")
                    continue
            else:
                logger.warning(f"批量添加任务时跳过无效的目标类型: {target_type}")
                continue

            if not task_exists:
                config['checkin_tasks'].append(new_task)
                added_count += 1
            else:
                existing_count += 1

    if added_count > 0:
        save_config(config)
        notify_scheduler_to_reconcile()
        message = f"成功添加 {added_count} 个新任务。"
        if existing_count > 0:
            message += f" {existing_count} 个任务已存在，已跳过。"
        return jsonify({"success": True, "message": message})
    else:
        return jsonify({"success": False, "message": "所有指定的任务都已存在或提供了无效的目标。"}), 400

@api.route('/tasks/delete_batch', methods=['POST'])
@login_required
def delete_tasks_batch():
    config = load_config()
    tasks_to_delete = request.json.get('tasks', [])

    if not tasks_to_delete:
        return jsonify({"success": False, "message": "未选择要删除的任务。"}), 400

    original_task_count = len(config.get('checkin_tasks', []))
    
    tasks_to_keep = []
    deleted_count = 0

    for task in config.get('checkin_tasks', []):
        task_key = (
            task.get('user_telegram_id'),
            task.get('bot_username') or task.get('target_chat_id')
        )
        
        should_delete = False
        for task_to_delete in tasks_to_delete:
            delete_key = (
                task_to_delete.get('user_telegram_id'),
                task_to_delete.get('identifier')
            )
            if task_key[0] == delete_key[0]:
                if str(task_key[1]) == str(delete_key[1]):
                    should_delete = True
                    break
        
        if not should_delete:
            tasks_to_keep.append(task)
        else:
            deleted_count += 1

    if deleted_count > 0:
        config['checkin_tasks'] = tasks_to_keep
        save_config(config)
        notify_scheduler_to_reconcile()
        return jsonify({"success": True, "message": f"成功删除 {deleted_count} 个任务。"})
    else:
        return jsonify({"success": False, "message": "未找到要删除的任务。"}), 404

@api.route('/tasks/update_slot', methods=['POST'])
@login_required
def update_task_slot():
    config = load_config()
    user_telegram_id_str = request.form.get('user_telegram_id')
    identifier = request.form.get('identifier')
    new_slot_id_str = request.form.get('selected_time_slot_id')

    if not all([user_telegram_id_str, identifier, new_slot_id_str]):
        return jsonify({"success": False, "message": "缺少必要参数。"}), 400

    try:
        user_telegram_id = int(user_telegram_id_str)
        new_slot_id = int(new_slot_id_str)
    except ValueError:
        return jsonify({"success": False, "message": "无效的ID格式。"}), 400

    task_found = False
    for task in config.get('checkin_tasks', []):
        task_key_user = task.get('user_telegram_id')
        task_key_target = str(task.get('bot_username') or task.get('target_chat_id'))

        if task_key_user == user_telegram_id and task_key_target == identifier:
            task['selected_time_slot_id'] = new_slot_id
            task_found = True
            break
    
    if task_found:
        save_config(config)
        notify_scheduler_to_reconcile()
        return jsonify({"success": True, "message": "任务的时间段已更新。"})
    else:
        return jsonify({"success": False, "message": "未找到指定的任务。"}), 404

@api.route('/tasks/update_message', methods=['POST'])
@login_required
def update_task_message():
   config = load_config()
   user_telegram_id_str = request.form.get('user_telegram_id')
   identifier = request.form.get('identifier')
   message_content = request.form.get('message_content', '')

   if not all([user_telegram_id_str, identifier]):
       return jsonify({"success": False, "message": "缺少必要参数。"}), 400

   try:
       user_telegram_id = int(user_telegram_id_str)
       target_chat_id = int(identifier)
   except ValueError:
       return jsonify({"success": False, "message": "无效的ID格式。"}), 400

   task_found = False
   for task in config.get('checkin_tasks', []):
       if task.get('user_telegram_id') == user_telegram_id and task.get('target_chat_id') == target_chat_id:
           task['message_content'] = message_content
           task_found = True
           break
   
   if task_found:
       save_config(config)
       notify_scheduler_to_reconcile()
       return jsonify({"success": True, "message": "任务的消息内容已更新。"})
   else:
       return jsonify({"success": False, "message": "未找到指定的任务。"}), 404

@api.route('/tasks/delete', methods=['POST'])
def delete_task():
    config = load_config()
    user_telegram_id_str = request.form.get('user_telegram_id')
    target_type = request.form.get('target_type')
    identifier = request.form.get('identifier')

    if not user_telegram_id_str or not target_type or not identifier:
        return jsonify({"success": False, "message": "缺少必要参数。"}), 400

    try:
        user_telegram_id = int(user_telegram_id_str)
    except ValueError:
        return jsonify({"success": False, "message": "无效的用户TG ID格式。"}), 400

    if 'checkin_tasks' not in config or not isinstance(config['checkin_tasks'], list):
        config['checkin_tasks'] = []
    
    user_for_log = next((u for u in config.get('users', []) if u.get('telegram_id') == user_telegram_id), None)
    log_identifier_user = user_for_log.get('nickname') if user_for_log else f"TGID_{user_telegram_id}"
    log_target_name = identifier

    original_task_count = len(config['checkin_tasks'])
    
    tasks_to_keep = []
    for t in config['checkin_tasks']:
        keep_task = True
        if t.get('user_telegram_id') == user_telegram_id:
            if target_type == 'bot' and t.get('bot_username') == identifier:
                keep_task = False
            elif target_type == 'chat':
                try:
                    chat_id_to_check = int(identifier)
                    if t.get('target_chat_id') == chat_id_to_check:
                        keep_task = False
                except ValueError:
                    pass
        if keep_task:
            tasks_to_keep.append(t)
            
    config['checkin_tasks'] = tasks_to_keep

    if len(config['checkin_tasks']) < original_task_count:
        save_config(config)
        notify_scheduler_to_reconcile()
        logger.info(f"任务已删除: 用户 {log_identifier_user} -> {target_type.upper()} {log_target_name}")
        return jsonify({"success": True, "message": "任务已删除。"})
    else:
        return jsonify({"success": False, "message": "未找到该任务。"}), 404

@api.route('/scheduler/reconcile', methods=['POST'])
@login_required
def reconcile_scheduler_tasks():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "无效的请求：缺少JSON body。"}), 400

    task_ids = data.get('task_ids')
    if not task_ids or not isinstance(task_ids, list):
        return jsonify({"success": False, "message": "缺少或无效的 'task_ids' 参数。"}), 400

    SCHEDULER_HOST = os.environ.get("SCHEDULER_HOST", "localhost")
    SCHEDULER_PORT = os.environ.get("SCHEDULER_PORT", "5057")
    url = f"http://{SCHEDULER_HOST}:{SCHEDULER_PORT}/reconcile"
    payload = {"task_ids": task_ids}

    try:
        with httpx.Client() as client:
            response = client.post(url, json=payload, timeout=15)
        
        if response.status_code == 200:
            logger.info(f"成功请求重新调度任务: {task_ids}")
            return jsonify(response.json())
        else:
            logger.error(f"重新调度任务失败，状态码: {response.status_code}, 响应: {response.text}")
            return jsonify({"success": False, "message": f"Error: {response.status_code} - {response.text}"}), response.status_code
            
    except httpx.ConnectError:
        logger.error(f"无法连接到调度器服务 ({url})")
        return jsonify({"success": False, "message": "无法连接到调度器服务"}), 503
    except httpx.RequestError as e:
        logger.error(f"请求调度器服务时发生错误: {e}")
        return jsonify({"success": False, "message": f"请求错误: {e}"}), 500


@api.route('/checkin/manual', methods=['POST'])
async def manual_action():
    config = load_config()
    api_id = config.get('api_id')
    api_hash = config.get('api_hash')

    if not api_id or not api_hash:
        return jsonify({"success": False, "message": "请先设置API ID和API Hash。"}), 400

    user_telegram_id_str = request.form.get('user_telegram_id') 
    target_type = request.form.get('target_type')
    identifier = request.form.get('identifier')
    message_content_manual = request.form.get('message_content_manual', None)
    task_strategy_manual = request.form.get('task_strategy_manual', None)


    if not user_telegram_id_str or not target_type or not identifier: 
        return jsonify({"success": False, "message": "未选择用户、目标类型或目标标识符。"}), 400
    
    try:
        user_telegram_id = int(user_telegram_id_str)
    except ValueError:
        return jsonify({"success": False, "message": "无效的用户TG ID格式。"}), 400

    user_config = next((u for u in config.get('users', []) if u.get('telegram_id') == user_telegram_id and u.get('status') == 'logged_in'), None)
    if not user_config:
        return jsonify({"success": False, "message": f"用户TG ID '{user_telegram_id}' 未找到或未登录。"}), 400

    session_name_from_config = user_config.get('session_name')
    user_nickname = user_config.get('nickname', f"TGID_{user_telegram_id}")
    if not session_name_from_config:
        return jsonify({"success": False, "message": f"用户 {user_nickname} 缺少 session_name 配置。"}), 400
    
    target_config_item = None
    log_target_display_name = identifier
    target_chat_id_int = None

    if target_type == 'bot':
        target_config_item = next((b for b in config.get('bots', []) if isinstance(b, dict) and b.get('bot_username') == identifier), None)
    elif target_type == 'chat':
        try:
            target_chat_id_int = int(identifier)
            target_config_item = next((c for c in config.get('chats', []) if isinstance(c, dict) and c.get('chat_id') == target_chat_id_int), None)
            if target_config_item:
                log_target_display_name = target_config_item.get('chat_title', identifier)
        except ValueError:
            return jsonify({"success": False, "message": "群组ID必须是数字。"}), 400
    else:
        return jsonify({"success": False, "message": "无效的目标类型。"}), 400
    
    if not target_config_item:
        return jsonify({"success": False, "message": f"目标 '{identifier}' 未在配置中找到。"}), 400

    task_for_manual_action = {
        "user_telegram_id": user_telegram_id,
        "selected_time_slot_id": None
    }
    if target_type == 'bot':
        task_for_manual_action["bot_username"] = identifier
    else:
        task_for_manual_action["target_chat_id"] = target_chat_id_int

    if target_type == 'chat' and message_content_manual is not None:
        task_for_manual_action['message_content'] = message_content_manual
    if task_strategy_manual:
        task_for_manual_action['strategy_identifier'] = task_strategy_manual

    effective_strategy_id = task_for_manual_action.get('strategy_identifier') or \
                            target_config_item.get('strategy') or \
                            target_config_item.get('strategy_identifier', '未知')
    strategy_display = get_strategy_display_name(effective_strategy_id)
    
    target_entity_identifier = identifier
    if target_type == 'chat':
        try:
            target_entity_identifier = int(identifier)
        except ValueError:
            return jsonify({"success": False, "message": "群组ID必须是数字。"}), 400

    identity = task_identity(user_telegram_id, target_type, identifier)
    state_date = beijing_today_str()
    configured_task = next(
        (
            task
            for task in config.get("checkin_tasks", [])
            if task_identity_from_config(task) == identity
        ),
        None,
    )
    claim = {"claimed": False, "token": None, "reason": "not_configured"}
    if configured_task is not None:
        claim = claim_daily_task(identity, "manual", allow_retry=True, state_date=state_date)
        if not claim["claimed"]:
            if claim["reason"] == "in_progress":
                return jsonify({"success": False, "message": "该任务正在排队或执行中，请稍后再试。"}), 409
            return jsonify({"success": False, "message": "该任务今日状态暂时无法更新。"}), 503

    try:
        result = await execute_action(
            session_name=session_name_from_config,
            target_entity_identifier=target_entity_identifier,
            strategy_id=effective_strategy_id,
            task_config=task_for_manual_action,
        )
    except Exception as exc:
        logger.exception(f"手动执行任务异常: {identity}")
        result = {"success": False, "message": f"执行异常: {exc}"}

    log_entry = {
        "checkin_type": f"手动操作 ({strategy_display})",
        "user_nickname": user_nickname,
        "target_type": target_type,
        "target_name": log_target_display_name,
        "target_identifier": identity[2] if identity else identifier,
        "user_telegram_id": user_telegram_id,
        "execution_source": "manual",
        "success": result.get("success"),
        "message": result.get("message"),
    }
    save_daily_checkin_log(log_entry)
    if claim["claimed"]:
        complete_daily_task(
            identity,
            claim["token"],
            result.get("success"),
            result.get("message"),
            state_date=state_date,
        )

    return jsonify(result)

def _keep_queued_batch_alive(batch_id, state_date, stop_event):
    while not stop_event.is_set():
        refresh_queued_batch(batch_id, state_date)
        if stop_event.wait(QUICK_QUEUE_HEARTBEAT_SECONDS):
            break


def run_async_tasks_in_background(app, task_entries=None, batch_id=None, state_date=None):
    with app.app_context():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        heartbeat_stop = threading.Event()
        heartbeat_thread = None
        if batch_id:
            heartbeat_thread = threading.Thread(
                target=_keep_queued_batch_alive,
                args=(batch_id, state_date, heartbeat_stop),
                daemon=True,
                name=f"quick-queue-heartbeat-{batch_id[:8]}",
            )
            heartbeat_thread.start()
        logger.info("后台线程：开始执行今日未执行任务...")
        try:
            loop.run_until_complete(
                execute_all_tasks_internal(
                    source="background_thread",
                    task_entries=task_entries,
                    batch_id=batch_id,
                    state_date=state_date,
                )
            )
            logger.info("后台线程：今日未执行任务处理完毕。")
        except Exception as exc:
            logger.exception(f"后台线程执行任务时发生错误: {exc}")
        finally:
            heartbeat_stop.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=2)
            loop.close()


async def execute_all_tasks_internal(
    source="http_manual_all", task_entries=None, batch_id=None, state_date=None
):
    config = load_config()
    state_date = state_date or beijing_today_str()
    tasks_to_run = task_entries if task_entries is not None else config.get("checkin_tasks", [])
    if not tasks_to_run:
        return {"all_tasks_results": [], "message": "没有配置任务。"}

    user_map_by_id = {
        user["telegram_id"]: user
        for user in config.get("users", [])
        if "telegram_id" in user
    }
    bot_map_by_username = {
        bot["bot_username"]: bot
        for bot in config.get("bots", [])
        if "bot_username" in bot
    }
    chat_map_by_id = {
        chat["chat_id"]: chat
        for chat in config.get("chats", [])
        if "chat_id" in chat
    }
    results_list = []

    for task_config_entry in tasks_to_run:
        identity = task_identity_from_config(task_config_entry)
        if not identity:
            continue

        if batch_id:
            refresh_queued_batch(batch_id, state_date)
            start_result = start_queued_task(identity, batch_id, state_date)
            if not start_result["started"]:
                continue
            run_token = start_result["token"]
        else:
            claim = claim_daily_task(identity, "quick", allow_retry=False, state_date=state_date)
            if not claim["claimed"]:
                continue
            run_token = claim["token"]

        user_telegram_id, target_type, target_identifier = identity
        user_config = user_map_by_id.get(user_telegram_id)
        user_nickname = (
            user_config.get("nickname", f"TGID_{user_telegram_id}")
            if user_config
            else f"TGID_{user_telegram_id}_(未知用户)"
        )
        target_config_item = None
        log_target_name = target_identifier

        if target_type == "bot":
            target_config_item = bot_map_by_username.get(target_identifier)
        elif target_type == "chat":
            target_config_item = chat_map_by_id.get(int(target_identifier))
            if target_config_item:
                log_target_name = target_config_item.get("chat_title", target_identifier)

        effective_strategy_id = "未知"
        if target_config_item:
            effective_strategy_id = (
                task_config_entry.get("strategy_identifier")
                or target_config_item.get("strategy")
                or target_config_item.get("strategy_identifier")
                or "未知"
            )
        strategy_display = get_strategy_display_name(effective_strategy_id)

        try:
            if not user_config or user_config.get("status") != "logged_in":
                current_task_result = {
                    "success": False,
                    "message": f"用户 {user_nickname} 未登录或配置不正确。",
                }
            elif not target_config_item:
                current_task_result = {
                    "success": False,
                    "message": f"目标 {log_target_name} ({target_type}) 未在配置中找到。",
                }
            elif not user_config.get("session_name"):
                current_task_result = {
                    "success": False,
                    "message": f"用户 {user_nickname} 缺少 session_name 配置。",
                }
            elif not config.get("api_id") or not config.get("api_hash"):
                current_task_result = {
                    "success": False,
                    "message": "API ID/Hash 未配置。",
                }
            else:
                target_entity_identifier = (
                    int(target_identifier) if target_type == "chat" else target_identifier
                )
                current_task_result = await execute_action(
                    session_name=user_config.get("session_name"),
                    target_entity_identifier=target_entity_identifier,
                    strategy_id=effective_strategy_id,
                    task_config=task_config_entry,
                )
        except Exception as exc:
            logger.exception(f"批量执行任务异常: {identity}")
            current_task_result = {"success": False, "message": f"执行异常: {exc}"}

        results_list.append(
            {
                "task": {
                    "user_nickname": user_nickname,
                    "target_type": target_type,
                    "target_name": log_target_name,
                    "strategy_used_display": strategy_display,
                },
                "result": current_task_result,
            }
        )
        log_entry = {
            "checkin_type": f"批量快速操作 ({strategy_display})",
            "user_nickname": user_nickname,
            "target_type": target_type,
            "target_name": log_target_name,
            "user_telegram_id": user_telegram_id,
            "target_identifier": target_identifier,
            "execution_source": "quick",
            "success": current_task_result.get("success"),
            "message": current_task_result.get("message"),
        }
        save_daily_checkin_log(log_entry)
        complete_daily_task(
            identity,
            run_token,
            current_task_result.get("success"),
            current_task_result.get("message"),
            state_date=state_date,
        )

    return {"all_tasks_results": results_list, "message": "所有任务执行完毕。"}


@api.route('/tasks/execute_all', methods=['POST'])
def execute_all_tasks_http():
    config = load_config()
    if not config.get("api_id") or not config.get("api_hash"):
        return jsonify({"success": False, "message": "请先设置API ID和API Hash。"}), 400

    tasks_to_queue = list(config.get("checkin_tasks", []))
    if not tasks_to_queue:
        return jsonify({
            "success": True,
            "queued_count": 0,
            "skipped_count": 0,
            "message": "没有配置任务。",
        })

    state_date = beijing_today_str()
    ensure_daily_task_rows(config, state_date)
    try:
        batch_id, queued_count, skipped_count = queue_daily_tasks(
            tasks_to_queue, state_date=state_date
        )
    except sqlite3.Error as exc:
        logger.error(f"今日任务排队失败: {exc}")
        return jsonify({
            "success": False,
            "queued_count": 0,
            "skipped_count": 0,
            "message": "任务状态数据库暂时不可用，请稍后重试。",
        }), 503

    if queued_count > 0:
        thread = threading.Thread(
            target=run_async_tasks_in_background,
            args=(current_app._get_current_object(), tasks_to_queue, batch_id, state_date),
            daemon=True,
        )
        thread.start()
        message = f"已在后台排队执行 {queued_count} 个今日未执行任务。"
    else:
        today_task_summary = get_daily_task_counts(config, state_date)
        if (
            today_task_summary["total_count"] > 0
            and today_task_summary["executed_count"] == today_task_summary["total_count"]
        ):
            message = "今日任务均已执行，无需重复执行。"
        elif today_task_summary["queued_count"] or today_task_summary["running_count"]:
            message = "今日任务已在排队或执行中，无需重复提交。"
        else:
            message = "今日没有可排队执行的任务。"
    flash(message, "info")
    return jsonify({
        "success": True,
        "queued_count": queued_count,
        "skipped_count": skipped_count,
        "message": message,
    })
