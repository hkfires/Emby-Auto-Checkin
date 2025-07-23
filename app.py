from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import logging, os, threading, asyncio, httpx, base64, json
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime
from config import load_config, save_config, migrate_session_names
from log import save_daily_checkin_log, init_log_db, load_checkin_log_by_date
from apscheduler.triggers.cron import CronTrigger
from scheduler_instance import scheduler
from run_scheduler import get_random_time_in_range
from utils.common import format_datetime_filter, get_masked_api_credentials, get_processed_bots_list, update_api_credential
from utils.tg_service_api import execute_action, manage_session, resolve_chat_identifier
from checkin_strategies import STRATEGY_DISPLAY_NAMES, get_strategy_display_name

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

migrate_session_names()

app = Flask(__name__)
config = load_config()
if 'secret_key' not in config or not config['secret_key']:
    config['secret_key'] = os.urandom(24).hex()
    save_config(config)
    logger.info("Generated and saved a new secret key.")
app.secret_key = bytes.fromhex(config['secret_key'])

with app.app_context():
    init_log_db()

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = ""

temp_otp_store = {}

class User(UserMixin):
    def __init__(self, id, username, password_hash=None):
        self.id = id
        self.username = username
        self.password_hash = password_hash

    @staticmethod
    def get(user_id):
        config = load_config()
        try:
            uid_to_check = int(user_id)
        except ValueError:
            return None

        for user_data in config.get('web_users', []):
            if user_data.get('id') == uid_to_check:
                return User(user_data['id'], user_data['username'], user_data.get('password_hash'))
        return None

    @staticmethod
    def get_by_username(username):
        config = load_config()
        for user_data in config.get('web_users', []):
            if user_data.get('username') == username:
                return User(user_data['id'], user_data['username'], user_data.get('password_hash'))
        return None

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

@app.before_request
def require_api_setup():
    exempt_endpoints = ['login', 'logout', 'static', 'check_first_run_status', 'api_settings_page', 'chats', 'delete_chat']
    
    if current_user.is_authenticated and request.endpoint not in exempt_endpoints:
        config = load_config()
        if not config.get('api_id') or not config.get('api_hash'):
            if request.endpoint != 'api_settings_page':
                flash('请首先完成 Telegram API 设置以使用其他功能。', 'warning')
                return redirect(url_for('api_settings_page'))

@app.route('/check_first_run_status', methods=['GET'])
def check_first_run_status():
    config = load_config()
    web_users = config.get('web_users', [])
    return jsonify({'is_first_run': not web_users})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        config = load_config()
        web_users = config.get('web_users', [])
        
        user_data = next((u for u in web_users if u.get('username') == username), None)

        if not web_users:
            if not password or len(password) < 6:
                flash('密码长度至少为6位。', 'danger')
                return render_template('login.html')
            if password != confirm_password:
                flash('两次输入的密码不一致。', 'danger')
                return render_template('login.html')

            new_user_id = 1
            hashed_password = generate_password_hash(password)
            new_user_data = {'id': new_user_id, 'username': username, 'password_hash': hashed_password}
            web_users.append(new_user_data)
            config['web_users'] = web_users
            save_config(config)
            
            new_user = User(new_user_id, username, hashed_password)
            login_user(new_user)
            flash('管理员账户注册成功并已登录！', 'success')
            return redirect(url_for('index'))
        
        elif user_data:
            user = User(user_data['id'], user_data['username'], user_data.get('password_hash'))
            if user.password_hash and check_password_hash(user.password_hash, password):
                login_user(user)
                flash('登录成功！', 'success')
                return redirect(url_for('index'))
            else:
                flash('用户名或密码错误。', 'danger')
        else:
            flash('用户名不存在或不允许注册新用户。', 'danger')
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('您已成功登出。', 'info')
    return redirect(url_for('login'))

@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_new_password = request.form.get('confirm_new_password')

        if not current_user.password_hash or not check_password_hash(current_user.password_hash, current_password):
            flash('当前密码不正确。', 'danger')
        elif new_password != confirm_new_password:
            flash('新密码和确认密码不匹配。', 'danger')
        elif len(new_password) < 6:
            flash('新密码长度至少为6位。', 'danger')
        else:
            config = load_config()
            user_updated = False
            for user_data in config.get('web_users', []):
                if user_data.get('id') == current_user.id:
                    user_data['password_hash'] = generate_password_hash(new_password)
                    user_updated = True
                    break
            
            if user_updated:
                save_config(config)
                flash('密码修改成功！请使用新密码重新登录。', 'success')
                logout_user()
                return redirect(url_for('login'))
            else:
                flash('更新密码时发生错误。', 'danger')

    return render_template('change_password.html')

@app.route('/')
@login_required
def index():
    config = load_config()
    selected_date_str = date.today().isoformat()
    display_date_label = "今日"
    
    requested_date_str = request.args.get('date')

    if requested_date_str:
        try:
            datetime.strptime(requested_date_str, '%Y-%m-%d')
            selected_date_str = requested_date_str
            display_date_label = requested_date_str
        except ValueError:
            flash(f"提供的日期格式无效: {requested_date_str}。请使用 YYYY-MM-DD 格式。", "warning")
            
    checkin_log_for_display = load_checkin_log_by_date(selected_date_str)
        
    return render_template('index.html', 
                           config=config, 
                           checkin_log=checkin_log_for_display, 
                           selected_date=selected_date_str,
                           display_date_label=display_date_label)

app.jinja_env.filters['format_datetime'] = format_datetime_filter

@app.route('/settings/api', methods=['GET', 'POST'])
@login_required
def api_settings_page():
    config = load_config()
    if request.method == 'POST':
        submitted_api_id = request.form.get('api_id')
        submitted_api_hash = request.form.get('api_hash')
        current_api_id_display_val, current_api_hash_display_val = get_masked_api_credentials(config)

        update_api_credential(config, submitted_api_id, current_api_id_display_val, 'api_id')
        update_api_credential(config, submitted_api_hash, current_api_hash_display_val, 'api_hash')
        
        save_config(config)
        flash("API 设置已成功保存。", "success")
        config = load_config()


    api_id_display_val, api_hash_display_val = get_masked_api_credentials(config)
    return render_template('api_settings.html',
                           api_id_display=api_id_display_val,
                           api_hash_display=api_hash_display_val,
                           original_api_id=config.get('api_id'),
                           original_api_hash=config.get('api_hash'))

@app.route('/settings/scheduler', methods=['GET', 'POST'])
@login_required
def scheduler_settings_page():
    config = load_config()
    root_mode = 'root' in request.args or request.form.get('root_mode') == 'true'

    if request.method == 'POST':
        form_data = request.form.to_dict()
        
        new_scheduler_time_slots = []
        for i in range(1, 4):
            slot_name = form_data.get(f'slot_{i}_name')
            if not slot_name or not slot_name.strip():
                fields_to_check = ['start_hour', 'start_minute', 'end_hour', 'end_minute']
                if root_mode:
                    fields_to_check.extend(['start_second', 'end_second'])
                
                if any(form_data.get(f'slot_{i}_{field}') for field in fields_to_check):
                    flash(f"时间段 {i} 的名称不能为空，如果不想使用该时段，请清空所有相关字段。", "warning")
                    return render_template('scheduler_settings.html',
                                           scheduler_enabled=config.get('scheduler_enabled'),
                                           scheduler_time_slots=config.get('scheduler_time_slots', []),
                                           root_mode=root_mode)
                continue

            try:
                start_hour = int(form_data.get(f'slot_{i}_start_hour'))
                start_minute = int(form_data.get(f'slot_{i}_start_minute'))
                start_second = int(form_data.get(f'slot_{i}_start_second', 0)) if root_mode else 0
                end_hour = int(form_data.get(f'slot_{i}_end_hour'))
                end_minute = int(form_data.get(f'slot_{i}_end_minute'))
                end_second = int(form_data.get(f'slot_{i}_end_second', 0)) if root_mode else 0

                if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59 and 0 <= start_second <= 59 and \
                        0 <= end_hour <= 24 and 0 <= end_minute <= 59 and 0 <= end_second <= 59):
                    flash(f"时间段 {i} ('{slot_name}') 的时间值超出有效范围 (小时 0-23, 分钟/秒 0-59)。", "danger")
                    return render_template('scheduler_settings.html',
                                           scheduler_enabled=config.get('scheduler_enabled'),
                                           scheduler_time_slots=config.get('scheduler_time_slots', []),
                                           root_mode=root_mode)

                start_total_seconds = start_hour * 3600 + start_minute * 60 + start_second
                end_total_seconds = end_hour * 3600 + end_minute * 60 + end_second

                
                slot_data = {
                    "id": len(new_scheduler_time_slots) + 1,
                    "name": slot_name.strip(),
                    "start_hour": start_hour,
                    "start_minute": start_minute,
                    "end_hour": end_hour,
                    "end_minute": end_minute
                }
                if root_mode:
                    slot_data["start_second"] = start_second
                    slot_data["end_second"] = end_second
                new_scheduler_time_slots.append(slot_data)

            except (ValueError, TypeError):
                flash(f"时间段 {i} ('{slot_name}') 的时间格式无效。请输入有效的数字。", "danger")
                return render_template('scheduler_settings.html',
                                       scheduler_enabled=config.get('scheduler_enabled'),
                                       scheduler_time_slots=config.get('scheduler_time_slots', []),
                                       root_mode=root_mode)
        
        if not new_scheduler_time_slots and form_data.get('scheduler_enabled') == 'on':
            flash("调度器已启用，但未配置任何有效的时间段。请至少配置一个时间段。", "warning")
            return render_template('scheduler_settings.html',
                                   scheduler_enabled=True,
                                   scheduler_time_slots=[],
                                   root_mode=root_mode)

        config['scheduler_enabled'] = True if form_data.get('scheduler_enabled') == 'on' else False
        config['scheduler_time_slots'] = new_scheduler_time_slots
        
        save_config(config)

        flash("自动签到设置已成功保存。更改将在每日重调度（凌晨1点）或重启调度器服务后生效。", "warning")
        config = load_config()

    return render_template('scheduler_settings.html',
                           scheduler_enabled=config.get('scheduler_enabled'),
                           scheduler_time_slots=config.get('scheduler_time_slots', []),
                           root_mode=root_mode)

@app.route('/settings/llm', methods=['GET', 'POST'])
@login_required
def llm_settings_page():
   config = load_config()
   return render_template('llm_settings.html', llm_settings=config.get('llm_settings', {}))

@app.route('/api/llm/test', methods=['POST'])
@login_required
async def api_test_llm_connection():
    base_api_url = request.form.get('api_url', '').strip().rstrip('/')
    api_key = request.form.get('api_key')
    model_name = request.form.get('model_name')
    if not all([base_api_url, api_key, model_name]):
        return jsonify({"success": False, "message": "API URL, API Key 和模型名称均不能为空。"}), 400

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    chat_url = f"{base_api_url}/v1/chat/completions"

    image_path = os.path.join(app.static_folder, 'test_image.png')
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

    json_data = {
        "model": model_name,
        "messages": messages,
        "stream": True
    }

    try:
        full_content = ""
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", chat_url, headers=headers, json=json_data, timeout=60) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    return jsonify({"success": False, "message": f"连接失败 (状态码: {response.status_code})。URL: {chat_url}, 错误信息: {error_text.decode()}"})

                try:
                    async for line in response.aiter_lines():
                        if line.startswith('data: '):
                            data_str = line[len('data: '):]
                            if data_str.strip() == '[DONE]':
                                break
                            try:
                                chunk = json.loads(data_str)
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                content_piece = delta.get("content")
                                if content_piece:
                                    full_content += content_piece
                            except json.JSONDecodeError:
                                logger.warning(f"无法解析SSE中的JSON数据: {data_str}")
                                continue
                except Exception as e:
                    logger.error(f"处理LLM API流式响应时出错: {e}", exc_info=True)
                    return jsonify({"success": False, "message": f"处理流式响应时出错: {e}"})

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

    except httpx.RequestError as e:
        logger.error(f"测试LLM API连接时发生请求错误: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"请求失败: {e}"}), 500
    except Exception as e:
        logger.error(f"测试LLM API连接时发生未知错误: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"发生未知错误: {e}"}), 500

@app.route('/api/llm/models', methods=['POST'])
@login_required
async def api_get_llm_models():
    base_api_url = request.form.get('api_url', '').strip().rstrip('/')
    api_key = request.form.get('api_key')

    if not all([base_api_url, api_key]):
        return jsonify({"success": False, "message": "API URL 和 API Key 均不能为空。"}), 400

    headers = {"Authorization": f"Bearer {api_key}"}
    models_url = f"{base_api_url}/v1/models"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(models_url, headers=headers, timeout=20)
            if response.status_code == 200:
                models_data = response.json().get('data', [])
                return jsonify({"success": True, "models": models_data})
            else:
                return jsonify({"success": False, "message": f"获取模型列表失败 (状态码: {response.status_code})。URL: {models_url}, 错误: {response.text}"})
    except httpx.RequestError as e:
        logger.error(f"获取LLM模型列表时发生请求错误: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"请求失败: {e}"}), 500
    except Exception as e:
        logger.error(f"获取LLM模型列表时发生未知错误: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"发生未知错误: {e}"}), 500

@app.route('/users', methods=['GET'])
@login_required
def users_page():
    config = load_config()
    return render_template('users.html', users=config.get('users', []))

@app.route('/api/users/add', methods=['POST'])
async def api_add_user():
    config = load_config()
    if not config.get('api_id') or not config.get('api_hash'):
        return jsonify({"success": False, "message": "请先在API设置中配置API ID和Hash。"}), 400

    phone = request.form.get('phone')
    if not phone:
        return jsonify({"success": False, "message": "未提供手机号码。"}), 400

    if any(u.get('phone') == phone for u in config.get('users', [])):
        return jsonify({"success": False, "message": "该手机号码已经添加过。"}), 400

    from utils.tg_service_api import send_code
    result = await send_code(phone)

    if result.get("success"):
        temp_otp_store[phone] = {"hash": result.get("phone_code_hash")}
        return jsonify({"success": True, "message": "验证码已发送，请输入验证码。", "needs_otp": True, "phone": phone})
    else:
        return jsonify({"success": False, "message": result.get("message", "发送验证码失败。")}), 500

@app.route('/api/users/submit_otp', methods=['POST'])
async def api_submit_otp():
    phone = request.form.get('phone')
    otp_code = request.form.get('otp_code')
    password = request.form.get('password') 

    if not phone or not otp_code:
        return jsonify({"success": False, "message": "未提供手机号或验证码。"}), 400

    otp_data = temp_otp_store.get(phone)
    if not otp_data or 'hash' not in otp_data:
        return jsonify({"success": False, "message": "无法验证OTP：未找到之前的请求或请求已超时。"}), 400
    
    phone_code_hash = otp_data['hash']

    from utils.tg_service_api import sign_in
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

@app.route('/api/users/delete', methods=['POST'])
async def api_delete_user():
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
        if user_telegram_id_to_delete:
            for job in scheduler.get_jobs():
                if job.id and job.id.startswith(f"checkin_job_{user_telegram_id_to_delete}_"):
                    scheduler.remove_job(job.id)
                    logger.info(f"已从调度器中移除任务: {job.id}")
        logger.info(f"用户 {nickname_to_delete} 已删除。")
        return jsonify({"success": True, "message": f"用户 {nickname_to_delete} 已删除。"})
    else:
        return jsonify({"success": False, "message": "删除用户时发生错误或用户未找到。"}), 404

@app.route('/bots', methods=['GET'])
@login_required
def bots_page():
    config = load_config()
    
    available_strategies_for_template = []
    for key, strategy_info in STRATEGY_DISPLAY_NAMES.items():
        if isinstance(strategy_info, dict) and strategy_info.get("target_type") in ["bot", "any"]:
            available_strategies_for_template.append({
                "key": key,
                "name": strategy_info.get("name", key)
            })
    
    raw_bots_list = config.get('bots', [])
    processed_bots = get_processed_bots_list(raw_bots_list)

    return render_template('bots.html', bots=processed_bots, users=config.get('users', []), available_strategies=available_strategies_for_template)

@app.route('/chats', methods=['GET', 'POST'])
@login_required
async def chats():
    config = load_config()
    if request.method == 'POST':
        chat_identifier = request.form.get('chat_identifier')
        user_nickname_for_解析 = request.form.get('user_nickname')
        strategy_identifier = request.form.get('strategy_identifier')
        custom_chat_title = request.form.get('custom_chat_title', '').strip()

        if not chat_identifier or not user_nickname_for_解析 or not strategy_identifier:
            flash('群组标识符、解析用户和策略均不能为空。', 'danger')
        else:
            user_for_解析 = next((u for u in config.get('users', []) if u.get('nickname') == user_nickname_for_解析), None)
            if not user_for_解析 or user_for_解析.get('status') != 'logged_in':
                flash(f"选择的解析用户 '{user_nickname_for_解析}' 无效或未登录。", 'danger')
            else:
                api_id = config.get('api_id')
                api_hash = config.get('api_hash')
                user_session_name = user_for_解析.get('session_name')

                if not api_id or not api_hash:
                    flash('请先在API设置中配置API ID和Hash。', 'danger')
                elif not user_session_name:
                    flash(f"用户 '{user_nickname_for_解析}' 的会话名称未找到，可能需要重新登录该用户。", 'danger')
                else:
                    logger.info(f"尝试解析群组: ID/Link='{chat_identifier}', 使用用户='{user_nickname_for_解析}' (会话: {user_session_name})")
                    resolved_data = await resolve_chat_identifier(user_session_name, chat_identifier)
                    
                    if resolved_data.get("success"):
                        chat_id = resolved_data["id"]
                        chat_name_from_telegram = resolved_data["name"]
                        
                        final_chat_title = custom_chat_title if custom_chat_title else chat_name_from_telegram

                        if 'chats' not in config or not isinstance(config['chats'], list):
                            config['chats'] = []
                        
                        existing_chat = next((c for c in config['chats'] if c.get('chat_id') == chat_id), None)
                        if existing_chat:
                            flash(f"群组 '{final_chat_title}' (ID: {chat_id}) 已经存在。", 'warning')
                        else:
                            new_chat_entry = {
                                "chat_id": chat_id,
                                "chat_title": final_chat_title,
                                "strategy_identifier": strategy_identifier
                            }
                            config['chats'].append(new_chat_entry)
                            save_config(config)
                            flash(f"群组 '{final_chat_title}' 添加成功！", 'success')
                            logger.info(f"群组 '{final_chat_title}' (ID: {chat_id}) 已添加。")
                    else:
                        flash(f"添加群组失败: {resolved_data.get('message', '未知错误')}", 'danger')
                        logger.error(f"解析群组 '{chat_identifier}' 失败: {resolved_data.get('message')}")
        return redirect(url_for('chats'))
    
    chat_specific_strategies = {}
    for key, strategy_info in STRATEGY_DISPLAY_NAMES.items():
        if isinstance(strategy_info, dict) and strategy_info.get("target_type") in ["chat", "any"]:
            chat_specific_strategies[key] = strategy_info.get("name", key)

    return render_template('chats.html', 
                           users=config.get('users', []), 
                           config_chats=config.get('chats', []),
                           strategy_display_names=chat_specific_strategies)

@app.route('/chats/delete/<int:chat_idx>', methods=['GET'])
@login_required
def delete_chat(chat_idx):
    config = load_config()
    if 'chats' in config and 0 <= chat_idx < len(config['chats']):
        deleted_chat = config['chats'].pop(chat_idx)
        
        if 'checkin_tasks' in config:
            config['checkin_tasks'] = [
                task for task in config['checkin_tasks']
                if task.get('target_chat_id') != deleted_chat.get('chat_id')
            ]
        
        save_config(config)
        deleted_chat_id = deleted_chat.get('chat_id')
        if deleted_chat_id:
            for job in scheduler.get_jobs():
                if job.id and job.id.endswith(f"_chat_{deleted_chat_id}"):
                    scheduler.remove_job(job.id)
                    logger.info(f"已从调度器中移除任务: {job.id}")
        flash(f"群组 '{deleted_chat.get('chat_title')}' 已删除。", 'success')
        logger.info(f"群组 '{deleted_chat.get('chat_title')}' (ID: {deleted_chat.get('chat_id')}) 已删除。")
    else:
        flash('无效的群组索引，删除失败。', 'danger')
    return redirect(url_for('chats'))

@app.route('/api/bots/add', methods=['POST'])
def api_add_bot():
    config = load_config()
    bot_username = request.form.get('bot_username')
    strategy = request.form.get('strategy', 'start_button_alert')

    if not bot_username:
        return jsonify({"success": False, "message": "未提供机器人用户名。"}), 400

    from checkin_strategies import STRATEGY_MAPPING
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

@app.route('/api/bots/delete', methods=['POST'])
def api_delete_bot():
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
        for job in scheduler.get_jobs():
            if job.id and job.id.endswith(f"_bot_{bot_to_delete_username}"):
                scheduler.remove_job(job.id)
                logger.info(f"已从调度器中移除任务: {job.id}")
        logger.info(f"机器人 {bot_to_delete_username} 已删除。")
        return jsonify({"success": True, "message": "机器人已删除。"})
    else:
        return jsonify({"success": False, "message": "未找到该机器人。"}), 404

@app.route('/tasks', methods=['GET'])
@login_required
def tasks_page():
    config = load_config()

    processed_bots_data = get_processed_bots_list(config.get('bots', []))
    configured_chats_data = config.get('chats', [])

    logged_in_users = [u for u in config.get('users', []) if u.get('status') == 'logged_in' and u.get('telegram_id')]
    
    valid_tasks = []
    user_map_by_id = {user['telegram_id']: user for user in config.get('users', []) if 'telegram_id' in user}
    bot_strategy_map = {b['bot_username']: b.get('strategy', 'start_button_alert') for b in processed_bots_data}
    chat_strategy_map = {c['chat_id']: c.get('strategy_identifier', 'send_custom_message') for c in configured_chats_data}
    
    scheduler_time_slots_map = {slot['id']: slot for slot in config.get('scheduler_time_slots', []) if isinstance(slot, dict) and 'id' in slot}

    for task_data in config.get('checkin_tasks', []):
        user_for_task = user_map_by_id.get(task_data.get('user_telegram_id'))
        if user_for_task:
            task_data['display_nickname'] = user_for_task.get('nickname', f"TGID: {task_data['user_telegram_id']}")
        else:
            task_data['display_nickname'] = f"未知用户 (TGID: {task_data.get('user_telegram_id')})"

        if task_data.get('bot_username'):
            task_data['target_type'] = 'bot'
            task_data['target_name'] = task_data['bot_username']
            task_data['strategy_used'] = bot_strategy_map.get(task_data['bot_username'], 'start_button_alert')
        elif task_data.get('target_chat_id'):
            task_data['target_type'] = 'chat'
            chat_info = next((c for c in configured_chats_data if c.get('chat_id') == task_data['target_chat_id']), None)
            task_data['target_name'] = chat_info.get('chat_title', str(task_data['target_chat_id'])) if chat_info else str(task_data['target_chat_id'])
            task_data['strategy_used'] = chat_strategy_map.get(task_data['target_chat_id'], 'send_custom_message')
            task_data['message_content_display'] = task_data.get('message_content', '')

        task_data['strategy_display_name'] = get_strategy_display_name(task_data.get('strategy_used'))
        
        selected_slot_id = task_data.get('selected_time_slot_id')
        selected_slot_info = scheduler_time_slots_map.get(selected_slot_id)
        if selected_slot_info:
            task_data['selected_time_slot_name'] = selected_slot_info.get('name', f"时段ID: {selected_slot_id}")
        else:
            first_slot = next(iter(scheduler_time_slots_map.values()), None)
            if first_slot:
                 task_data['selected_time_slot_name'] = f"默认为: {first_slot.get('name', f'时段ID: {first_slot.get_id}')} (原ID {selected_slot_id} 无效)"
                 task_data['selected_time_slot_id'] = first_slot.get('id')
            else:
                 task_data['selected_time_slot_name'] = "未分配或时段无效"

        valid_tasks.append(task_data)
    
    all_strategy_display_names_for_tasks = {
        key: info.get("name", key) if isinstance(info, dict) else info
        for key, info in STRATEGY_DISPLAY_NAMES.items()
    }

    return render_template('tasks.html', 
                           tasks=valid_tasks, 
                           users=logged_in_users, 
                           bots=processed_bots_data,
                           chats=configured_chats_data, 
                           strategy_display_names=all_strategy_display_names_for_tasks,
                           scheduler_time_slots=config.get('scheduler_time_slots', []),
                           app_config=config)

@app.route('/api/tasks/add', methods=['POST'])
def api_add_task():
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
                     return jsonify({"success": False, "message": "无法分配任务到时间段：系统中未配置任何时间段。"}), 400
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
        try:
            scheduler_time_slots = config.get('scheduler_time_slots', [])
            selected_slot_id = new_task.get('selected_time_slot_id')
            task_specific_time_slot = next((s for s in scheduler_time_slots if s.get('id') == selected_slot_id), scheduler_time_slots if scheduler_time_slots else None)

            if task_specific_time_slot:
                slot_start_h = task_specific_time_slot.get('start_hour', 8)
                slot_start_m = task_specific_time_slot.get('start_minute', 0)
                slot_start_s = task_specific_time_slot.get('start_second', 0)
                slot_end_h = task_specific_time_slot.get('end_hour', 22)
                slot_end_m = task_specific_time_slot.get('end_minute', 0)
                slot_end_s = task_specific_time_slot.get('end_second', 0)
                rand_h, rand_m, rand_s = get_random_time_in_range(slot_start_h, slot_start_m, slot_end_h, slot_end_m, slot_start_s, slot_end_s)

                target_identifier = new_task.get('bot_username') or new_task.get('target_chat_id')
                job_id_suffix = f"bot_{target_identifier}" if target_type == 'bot' else f"chat_{target_identifier}"
                job_id = f"checkin_job_{user_telegram_id}_{job_id_suffix}"

                scheduler.add_job(
                    'run_scheduler:run_checkin_task_sync',
                    trigger=CronTrigger(hour=rand_h, minute=rand_m, second=rand_s),
                    args=[user_telegram_id, target_type, target_identifier, new_task],
                    id=job_id,
                    name=f"Task: {user_nickname} -> {log_target_name}",
                    replace_existing=True
                )
                logger.info(f"任务已添加并调度: 用户 {user_nickname} -> {log_target_name} at {rand_h:02d}:{rand_m:02d}:{rand_s:02d}")
                return jsonify({"success": True, "message": "任务已添加并成功调度。"})
            else:
                logger.error(f"无法为新任务 {log_target_name} 找到调度时间段。")
                return jsonify({"success": True, "message": "任务已添加，但无法调度（未配置时间段）。它将在下次调度器重载时尝试调度。"})

        except Exception as e:
            logger.error(f"添加任务到调度器时出错: {e}", exc_info=True)
            return jsonify({"success": True, "message": "任务已添加，但调度失败。请检查调度器服务状态。"})
    else:
        return jsonify({"success": False, "message": "该任务已存在。"}), 400

@app.route('/api/tasks/delete', methods=['POST'])
def api_delete_task():
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
        try:
            job_id_suffix = f"bot_{identifier}" if target_type == 'bot' else f"chat_{identifier}"
            job_id = f"checkin_job_{user_telegram_id}_{job_id_suffix}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job.id)
                logger.info(f"已从调度器中移除任务: {job.id}")
            else:
                logger.warning(f"尝试删除任务 {job_id}，但在调度器中未找到。")
        except Exception as e:
            logger.error(f"从调度器删除任务 {job_id} 时出错: {e}", exc_info=True)

        logger.info(f"任务已删除: 用户 {log_identifier_user} -> {target_type.upper()} {log_target_name}")
        return jsonify({"success": True, "message": "任务已删除。"})
    else:
        return jsonify({"success": False, "message": "未找到该任务。"}), 404

@app.route('/api/checkin/manual', methods=['POST'])
async def api_manual_action():
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
    if not session_name_from_config:
        return jsonify({"success": False, "message": f"用户 {user_nickname} 缺少 session_name 配置。"}), 400
    user_nickname = user_config.get('nickname', f"TGID_{user_telegram_id}")
    
    target_config_item = None
    log_target_display_name = identifier
    task_for_manual_action = {"user_telegram_id": user_telegram_id}

    if target_type == 'bot':
        target_config_item = next((b for b in config.get('bots', []) if isinstance(b, dict) and b.get('bot_username') == identifier), None)
        if target_config_item:
            task_for_manual_action['bot_username'] = identifier
            if task_strategy_manual: task_for_manual_action['strategy_identifier'] = task_strategy_manual
    elif target_type == 'chat':
        try:
            chat_id_int = int(identifier)
            target_config_item = next((c for c in config.get('chats', []) if isinstance(c, dict) and c.get('chat_id') == chat_id_int), None)
            if target_config_item:
                task_for_manual_action['target_chat_id'] = chat_id_int
                log_target_display_name = target_config_item.get('chat_title', identifier)
                if message_content_manual is not None:
                    task_for_manual_action['message_content'] = message_content_manual
                if task_strategy_manual: task_for_manual_action['strategy_identifier'] = task_strategy_manual
        except ValueError:
             return jsonify({"success": False, "message": "群组ID必须是数字。"}), 400
    
    if not target_config_item:
        return jsonify({"success": False, "message": f"目标 '{identifier}' 未在配置中找到。"}), 400

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

    result = await execute_action(
        session_name=session_name_from_config,
        target_entity_identifier=target_entity_identifier,
        strategy_id=effective_strategy_id,
        task_config=task_for_manual_action
    )

    log_entry = {
        "checkin_type": f"手动操作 ({strategy_display})",
        "user_nickname": user_nickname, 
        "target_type": target_type,
        "target_name": log_target_display_name,
        "success": result.get("success"),
        "message": result.get("message")
    }
    save_daily_checkin_log(log_entry)

    return jsonify(result)

def run_async_tasks_in_background(app):
    with app.app_context():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.info("后台线程：开始执行所有任务...")
        try:
            loop.run_until_complete(api_execute_all_tasks_internal(source="background_thread"))
            logger.info("后台线程：所有任务执行完毕。")
        except Exception as e:
            logger.error(f"后台线程执行任务时发生错误: {e}", exc_info=True)
        finally:
            loop.close()

async def api_execute_all_tasks_internal(source="http_manual_all"):
    config = load_config()
    api_id = config.get('api_id')
    api_hash = config.get('api_hash')

    if not api_id or not api_hash:
        message = "请先设置API ID和API Hash。"
        if source.startswith("http"): return jsonify({"success": False, "message": message}), 400
        else: logger.warning(f"内部调用所有任务失败: {message}"); return {"success": False, "message": message, "all_tasks_results": []}

    tasks_to_run = config.get('checkin_tasks', [])
    if not tasks_to_run:
        message = "没有配置任务。"
        if source.startswith("http"): return jsonify({"success": True, "message": message, "all_tasks_results": []}), 200
        else: logger.info(f"内部调用所有任务: {message}"); return {"success": True, "message": message, "all_tasks_results": []}


    results_list = []
    user_map_by_id = {user['telegram_id']: user for user in config.get('users', []) if 'telegram_id' in user}
    bot_map_by_username = {bot['bot_username']: bot for bot in config.get('bots', []) if 'bot_username' in bot}
    chat_map_by_id = {chat['chat_id']: chat for chat in config.get('chats', []) if 'chat_id' in chat}

    for task_config_entry in tasks_to_run:
        user_telegram_id = task_config_entry.get('user_telegram_id')
        user_config = user_map_by_id.get(user_telegram_id)
        user_nickname = user_config.get('nickname', f"TGID_{user_telegram_id}") if user_config else f"TGID_{user_telegram_id}_(未知用户)"

        target_config_item = None
        target_type = "未知目标"
        log_target_name = "未知"

        if task_config_entry.get('bot_username'):
            target_config_item = bot_map_by_username.get(task_config_entry['bot_username'])
            target_type = "bot"
            log_target_name = task_config_entry['bot_username']
        elif task_config_entry.get('target_chat_id'):
            target_config_item = chat_map_by_id.get(task_config_entry['target_chat_id'])
            target_type = "chat"
            log_target_name = target_config_item.get('chat_title', str(task_config_entry['target_chat_id'])) if target_config_item else str(task_config_entry['target_chat_id'])
        
        eff_strat_id = "未知"
        if target_config_item:
            eff_strat_id = task_config_entry.get('strategy_identifier') or \
                           target_config_item.get('strategy') or \
                           target_config_item.get('strategy_identifier') or \
                           "未知"
        strategy_display = get_strategy_display_name(eff_strat_id)

        if not user_config or user_config.get('status') != 'logged_in':
            current_task_result = {"success": False, "message": f"用户 {user_nickname} 未登录或配置不正确。"}
        elif not target_config_item:
            current_task_result = {"success": False, "message": f"目标 {log_target_name} ({target_type}) 未在配置中找到。"}
        else:
            session_name_from_config = user_config.get('session_name')
            if not session_name_from_config:
                current_task_result = {"success": False, "message": f"用户 {user_nickname} 缺少 session_name 配置。"}
            else:
                target_entity_identifier = task_config_entry.get('bot_username') or task_config_entry.get('target_chat_id')
                current_task_result = await execute_action(
                    session_name=session_name_from_config,
                    target_entity_identifier=target_entity_identifier,
                    strategy_id=eff_strat_id,
                    task_config=task_config_entry
                )

        results_list.append({
            "task": {"user_nickname": user_nickname, "target_type": target_type, "target_name": log_target_name, "strategy_used_display": strategy_display},
            "result": current_task_result
        })
    
        log_entry = {
            "checkin_type": f"批量手动操作 ({strategy_display})",
            "user_nickname": user_nickname,
            "target_type": target_type,
            "target_name": log_target_name,
            "success": current_task_result.get("success"),
            "message": current_task_result.get("message")
        }
        save_daily_checkin_log(log_entry)
        
    final_response = {"all_tasks_results": results_list, "message": "所有任务执行完毕。"}
    if source.startswith("http"):
        return jsonify(final_response)
    else:
        return final_response

@app.route('/api/tasks/execute_all', methods=['POST'])
def api_execute_all_tasks_http():
    thread = threading.Thread(target=run_async_tasks_in_background, args=(app,))
    thread.start()
    flash("所有任务已在后台启动。请稍后在日志中查看结果。", "info")
    return jsonify({"success": True})

if __name__ == '__main__':
    logger.info("启动Flask应用...")
    app.run(debug=True, host='0.0.0.0', port=5055, use_reloader=False)
