from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import logging, os, re
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from telethon import TelegramClient, errors
from datetime import date, datetime
from config import load_config, save_config
from telegram_client import telethon_check_in, get_session_name
from log import save_daily_checkin_log, init_log_db, load_checkin_log_by_date
from scheduler import update_scheduler
from utils import format_datetime_filter, get_masked_api_credentials, get_processed_bots_list, update_api_credential
from checkin_strategies import STRATEGY_MAPPING, STRATEGY_DISPLAY_NAMES

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.urandom(24)

with app.app_context():
    init_log_db()
    update_scheduler()

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
    exempt_endpoints = ['login', 'logout', 'static', 'check_first_run_status', 'api_settings_page']
    
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
    if request.method == 'POST':
        form_data = request.form.to_dict()
        render_context = {
            'scheduler_enabled': config.get('scheduler_enabled'),
            'scheduler_range_start_hour': config.get('scheduler_range_start_hour'),
            'scheduler_range_start_minute': config.get('scheduler_range_start_minute'),
            'scheduler_range_end_hour': config.get('scheduler_range_end_hour'),
            'scheduler_range_end_minute': config.get('scheduler_range_end_minute'),
        }
        render_context.update({
            'scheduler_enabled': True if form_data.get('scheduler_enabled') == 'on' else False,
            'scheduler_range_start_hour': form_data.get('scheduler_range_start_hour'),
            'scheduler_range_start_minute': form_data.get('scheduler_range_start_minute'),
            'scheduler_range_end_hour': form_data.get('scheduler_range_end_hour'),
            'scheduler_range_end_minute': form_data.get('scheduler_range_end_minute'),
        })

        try:
            s_start_h = int(form_data.get('scheduler_range_start_hour', config.get('scheduler_range_start_hour')))
            s_start_m = int(form_data.get('scheduler_range_start_minute', config.get('scheduler_range_start_minute')))
            s_end_h = int(form_data.get('scheduler_range_end_hour', config.get('scheduler_range_end_hour')))
            s_end_m = int(form_data.get('scheduler_range_end_minute', config.get('scheduler_range_end_minute')))

            if not (0 <= s_start_h <= 23 and 0 <= s_start_m <= 59 and \
                    0 <= s_end_h <= 23 and 0 <= s_end_m <= 59):
                flash("时间值超出有效范围 (小时 0-23, 分钟 0-59)。请重新输入。", "danger")
                return render_template('scheduler_settings.html', **render_context)

            start_total_minutes = s_start_h * 60 + s_start_m
            end_total_minutes = s_end_h * 60 + s_end_m
            if start_total_minutes >= end_total_minutes:
                flash("调度结束时间必须晚于开始时间。请重新输入。", "danger")
                return render_template('scheduler_settings.html', **render_context)

            validated_scheduler_settings = {
                'scheduler_enabled': True if form_data.get('scheduler_enabled') == 'on' else False,
                'scheduler_range_start_hour': s_start_h,
                'scheduler_range_start_minute': s_start_m,
                'scheduler_range_end_hour': s_end_h,
                'scheduler_range_end_minute': s_end_m,
            }
            config.update(validated_scheduler_settings)
            save_config(config)
            update_scheduler()
            flash("自动签到设置已成功保存。", "success")
        except (ValueError, TypeError):
            flash("调度时间范围格式无效。请输入有效的数字。", "danger")
            return render_template('scheduler_settings.html', **render_context)

    return render_template('scheduler_settings.html',
                           scheduler_enabled=config.get('scheduler_enabled'),
                           scheduler_range_start_hour=config.get('scheduler_range_start_hour'),
                           scheduler_range_start_minute=config.get('scheduler_range_start_minute'),
                           scheduler_range_end_hour=config.get('scheduler_range_end_hour'),
                           scheduler_range_end_minute=config.get('scheduler_range_end_minute'))

@app.route('/users', methods=['GET'])
@login_required
def users_page():
    config = load_config()
    return render_template('users.html', users=config.get('users', []))

@app.route('/api/users/add', methods=['POST'])
async def api_add_user():
    config = load_config()
    api_id = config.get('api_id')
    api_hash = config.get('api_hash')

    if not api_id or not api_hash:
        return jsonify({"success": False, "message": "请先设置API ID和API Hash。"}), 400

    phone = request.form.get('phone')
    if not phone:
        return jsonify({"success": False, "message": "未提供手机号码。"}), 400

    if any(u.get('phone') == phone for u in config.get('users', [])):
        return jsonify({"success": False, "message": "该手机号码已经添加过。"}), 400
    
    if phone in temp_otp_store and temp_otp_store[phone].get('hash'):
        return jsonify({"success": True, "message": "该手机号已发送验证码，请直接输入验证码。", "needs_otp": True, "phone": phone, "status": "requires_otp"})

    temp_otp_flow_session_name = os.path.join(DATA_DIR, f"otp_flow_{re.sub(r'[^0-9a-zA-Z]', '', phone)}")
    client = TelegramClient(temp_otp_flow_session_name, api_id, api_hash)
    message = "发送验证码时发生未知错误。"
    needs_otp = False
    
    try:
        logger.info(f"尝试为手机号 {phone} 发送验证码 (使用临时会话: {temp_otp_flow_session_name})。")
        await client.connect()
        
        formatted_phone = phone if phone.startswith('+') else '+' + phone.lstrip('0')

        sent_code = await client.send_code_request(formatted_phone)
        temp_otp_store[phone] = {
            "hash": sent_code.phone_code_hash,
            "session_name": temp_otp_flow_session_name 
        }
        needs_otp = True
        message = "验证码已发送，请输入验证码。"
        logger.info(f"手机号 {phone}: 验证码已发送。Phone code hash: {sent_code.phone_code_hash}, 会话: {temp_otp_flow_session_name}")
        
        return jsonify({"success": True, "message": message, "needs_otp": needs_otp, "phone": phone, "status": "requires_otp"})

    except errors.PhoneNumberInvalidError:
        message = "无效的手机号码。"
        logger.error(f"手机号 {phone}: {message}")
        return jsonify({"success": False, "message": message}), 400
    except errors.ApiIdInvalidError: 
        message = "无效的 API ID / API Hash。"
        logger.error(f"手机号 {phone}: {message}")
        return jsonify({"success": False, "message": message}), 500
    except Exception as e:
        message = f"发送验证码时出错: {str(e)}"
        logger.error(f"手机号 {phone}: {message}")
        if os.path.exists(f"{temp_otp_flow_session_name}.session"):
            try:
                os.remove(f"{temp_otp_flow_session_name}.session")
                logger.info(f"已清理OTP流程中出错的临时会话文件: {temp_otp_flow_session_name}.session")
            except Exception as e_clean:
                logger.error(f"清理OTP流程中出错的临时会话文件失败: {e_clean}")
        return jsonify({"success": False, "message": message}), 500
    finally:
        if client and client.is_connected():
            await client.disconnect()

@app.route('/api/users/submit_otp', methods=['POST'])
async def api_submit_otp():
    config = load_config()
    api_id = config.get('api_id')
    api_hash = config.get('api_hash')

    phone = request.form.get('phone')
    otp_code = request.form.get('otp_code')

    if not phone or not otp_code:
        return jsonify({"success": False, "message": "未提供手机号或验证码。"}), 400

    otp_data = temp_otp_store.get(phone)
    if not otp_data or 'hash' not in otp_data or 'session_name' not in otp_data:
        return jsonify({"success": False, "message": "无法验证OTP：未找到之前的请求、已超时或请求数据不完整。请重新尝试添加用户。"}), 400
    
    phone_code_hash = otp_data['hash']
    otp_flow_session_name = otp_data['session_name']

    client = TelegramClient(otp_flow_session_name, api_id, api_hash)
    user_status = "otp_failed"
    message = "OTP验证失败。"
    final_nickname_for_response = None

    try:
        logger.info(f"尝试为手机号 {phone} 提交OTP (使用会话: {otp_flow_session_name})。")
        await client.connect()
        
        formatted_phone = phone if phone.startswith('+') else '+' + phone.lstrip('0')
        
        await client.sign_in(phone=formatted_phone, code=otp_code, phone_code_hash=phone_code_hash)

        if await client.is_user_authorized():
            me = await client.get_me()
            
            telegram_id = me.id
            telegram_username = me.username
            telegram_first_name = me.first_name
            telegram_last_name = me.last_name
            actual_phone = me.phone

            if telegram_username:
                base_nickname = telegram_username
            elif telegram_first_name and telegram_last_name:
                base_nickname = f"{telegram_first_name}_{telegram_last_name}"
            elif telegram_first_name:
                base_nickname = telegram_first_name
            else:
                base_nickname = f"user_{telegram_id}"
            
            final_nickname = base_nickname
            count = 1
            if 'users' not in config or not isinstance(config['users'], list):
                config['users'] = []
            while any(u.get('nickname') == final_nickname for u in config['users']):
                final_nickname = f"{base_nickname}_{count}"
                count += 1
            
            final_nickname_for_response = final_nickname
            final_session_name_for_system = os.path.join(DATA_DIR, get_session_name(final_nickname))
            
            await client.disconnect()
            client = None 

            old_session_path = f"{otp_flow_session_name}.session"
            new_session_path = f"{final_session_name_for_system}.session"

            if os.path.exists(old_session_path):
                os.rename(old_session_path, new_session_path)
                logger.info(f"会话文件已从 {old_session_path} 重命名为 {new_session_path}")
            else:
                logger.warning(f"OTP流程的临时会话文件 {old_session_path} 未找到，无法重命名。")

            new_user_entry = {
                "nickname": final_nickname,
                "telegram_id": telegram_id,
                "telegram_username": telegram_username,
                "telegram_first_name": telegram_first_name,
                "telegram_last_name": telegram_last_name,
                "phone": actual_phone, 
                "session_name": final_session_name_for_system,
                "status": "logged_in"
            }
            
            config['users'].append(new_user_entry)
            save_config(config)
            
            user_status = "logged_in"
            message = f"用户 {final_nickname} 添加并登录成功！"
            logger.info(f"用户 {final_nickname} (手机号: {actual_phone}) 添加成功。")

        else: 
            message = "OTP已提交，但用户仍未授权。"
            logger.warning(f"手机号 {phone}: {message}")

    except errors.PhoneCodeInvalidError:
        message = "无效的验证码。"
        logger.error(f"手机号 {phone}: {message}")
    except errors.SessionPasswordNeededError:
        message = "此账户需要两步验证密码，当前不支持。"
        user_status = "2fa_needed" 
        logger.error(f"手机号 {phone}: {message}")
    except errors.PhoneCodeExpiredError:
        message = "验证码已过期，请重新尝试添加用户以获取新的验证码。"
        logger.error(f"手机号 {phone}: {message}")
        user_status = "otp_expired"
    except Exception as e:
        message = f"OTP验证或用户添加时出错: {str(e)}"
        logger.error(f"手机号 {phone}: {message} ({type(e)})")
    finally:
        if client and client.is_connected():
            await client.disconnect()
        
        otp_session_file_path = f"{otp_flow_session_name}.session"
        if user_status != "logged_in" and os.path.exists(otp_session_file_path):
            try:
                os.remove(otp_session_file_path)
                logger.info(f"已清理OTP失败/过期的临时会话文件: {otp_session_file_path}")
            except Exception as e_clean:
                logger.error(f"清理OTP失败/过期的临时会话文件失败: {e_clean}")
        
        if phone in temp_otp_store:
            del temp_otp_store[phone]

    return jsonify({"success": user_status == "logged_in", "message": message, "status": user_status, "nickname": final_nickname_for_response if user_status == "logged_in" else None})

@app.route('/api/users/delete', methods=['POST'])
def api_delete_user():
    config = load_config()
    nickname_to_delete = request.form.get('nickname')
    if not nickname_to_delete:
        return jsonify({"success": False, "message": "未提供用户昵称。"}), 400

    original_user_count = len(config.get('users', []))
    
    user_to_delete_obj = next((u for u in config.get('users', []) if u.get('nickname') == nickname_to_delete), None)

    if not user_to_delete_obj:
        return jsonify({"success": False, "message": "未找到该昵称的用户。"}), 404

    user_telegram_id_to_delete = user_to_delete_obj.get('telegram_id')

    config['users'] = [u for u in config.get('users', []) if u.get('nickname') != nickname_to_delete]
    
    if 'checkin_tasks' in config and user_telegram_id_to_delete is not None:
        config['checkin_tasks'] = [
            task for task in config.get('checkin_tasks', [])
            if task.get('user_telegram_id') != user_telegram_id_to_delete
        ]

    session_file_to_delete = os.path.join(DATA_DIR, f"{get_session_name(nickname_to_delete)}.session")
    if os.path.exists(session_file_to_delete):
        try:
            os.remove(session_file_to_delete)
            logger.info(f"已删除会话文件: {session_file_to_delete}")
        except OSError as e:
            logger.error(f"删除会话文件 {session_file_to_delete} 失败: {e}")

    if len(config.get('users', [])) < original_user_count:
        save_config(config)
        update_scheduler()
        logger.info(f"用户 {nickname_to_delete} 已删除，并更新了调度器。")
        return jsonify({"success": True, "message": f"用户 {nickname_to_delete} 已删除。"})
    else:
        return jsonify({"success": False, "message": "删除用户时发生错误或用户未找到。"}), 404

@app.route('/bots', methods=['GET'])
@login_required
def bots_page():
    config = load_config()
    
    available_strategies_for_template = []
    for key in STRATEGY_MAPPING.keys():
        available_strategies_for_template.append({
            "key": key,
            "name": STRATEGY_DISPLAY_NAMES.get(key, key)
        })
    
    raw_bots_list = config.get('bots', [])
    processed_bots = get_processed_bots_list(raw_bots_list)

    return render_template('bots.html', bots=processed_bots, users=config.get('users', []), available_strategies=available_strategies_for_template)

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
        update_scheduler()
        logger.info(f"机器人 {bot_to_delete_username} 已删除，并更新了调度器。")
        return jsonify({"success": True, "message": "机器人已删除。"})
    else:
        return jsonify({"success": False, "message": "未找到该机器人。"}), 404

@app.route('/tasks', methods=['GET'])
@login_required
def tasks_page():
    config = load_config()

    processed_bots_data = get_processed_bots_list(config.get('bots', []))

    logged_in_users = [u for u in config.get('users', []) if u.get('status') == 'logged_in' and u.get('telegram_id')]
    valid_tasks = []
    
    bot_strategy_map = {b['bot_username']: b['strategy'] for b in processed_bots_data}
    bot_strategy_display_map = {b['bot_username']: b['strategy_display_name'] for b in processed_bots_data}

    if 'checkin_tasks' in config:
        all_users_list = config.get('users', [])
        user_map_by_id = {user['telegram_id']: user for user in all_users_list if 'telegram_id' in user}

        for task_data in config.get('checkin_tasks', []):
            if 'strategy' in task_data:
                del task_data['strategy'] 

            if 'user_telegram_id' in task_data and 'bot_username' in task_data:
                user_for_task = user_map_by_id.get(task_data['user_telegram_id'])
                if user_for_task:
                    task_data['display_nickname'] = user_for_task.get('nickname', f"TGID: {task_data['user_telegram_id']}")
                else:
                    task_data['display_nickname'] = f"未知用户 (TGID: {task_data['user_telegram_id']})"
                
                task_data['bot_strategy'] = bot_strategy_map.get(task_data['bot_username'], 'start_button_alert')
                task_data['bot_strategy_display_name'] = bot_strategy_display_map.get(task_data['bot_username'], task_data['bot_strategy'])
                valid_tasks.append(task_data)
    
    return render_template('tasks.html', tasks=valid_tasks, users=logged_in_users, bots=processed_bots_data)


@app.route('/api/tasks/add', methods=['POST'])
def api_add_task():
    config = load_config()
    user_telegram_id_str = request.form.get('user_telegram_id')
    bot_username = request.form.get('bot_username')

    if not user_telegram_id_str or not bot_username:
        return jsonify({"success": False, "message": "未选择用户或机器人。"}), 400
    
    try:
        user_telegram_id = int(user_telegram_id_str)
    except ValueError:
        return jsonify({"success": False, "message": "无效的用户TG ID格式。"}), 400

    user_for_task = next((u for u in config.get('users', []) if u.get('telegram_id') == user_telegram_id and u.get('status') == 'logged_in'), None)
    if not user_for_task:
        return jsonify({"success": False, "message": "选择的用户无效、未登录或缺少TG ID。"}), 400
    
    user_nickname_for_log = user_for_task.get('nickname', f"TGID_{user_telegram_id}")

    new_task = {
        "user_telegram_id": user_telegram_id, 
        "bot_username": bot_username,
        "last_auto_checkin_status": None,
        "last_auto_checkin_time": None,
        "last_scheduled_date": None
    }
    
    if 'checkin_tasks' not in config or not isinstance(config['checkin_tasks'], list):
        config['checkin_tasks'] = []

    task_exists = any(
        t.get('user_telegram_id') == new_task['user_telegram_id'] and t.get('bot_username') == new_task['bot_username']
        for t in config['checkin_tasks']
    )

    if not task_exists:
        config['checkin_tasks'].append(new_task)
        save_config(config)
        update_scheduler() 
        logger.info(f"签到任务已添加: 用户 {user_nickname_for_log} (TGID: {user_telegram_id}) -> 机器人 {bot_username}")
        return jsonify({"success": True, "message": "签到任务已添加。"})
    else:
        return jsonify({"success": False, "message": "该签到任务已存在。"}), 400

@app.route('/api/tasks/delete', methods=['POST'])
def api_delete_task():
    config = load_config()
    user_telegram_id_str = request.form.get('user_telegram_id')
    bot_username = request.form.get('bot_username')

    if not user_telegram_id_str or not bot_username:
        return jsonify({"success": False, "message": "未提供用户TG ID或机器人名称。"}), 400

    try:
        user_telegram_id = int(user_telegram_id_str)
    except ValueError:
        return jsonify({"success": False, "message": "无效的用户TG ID格式。"}), 400

    if 'checkin_tasks' not in config or not isinstance(config['checkin_tasks'], list):
        config['checkin_tasks'] = []
    
    user_for_log = next((u for u in config.get('users', []) if u.get('telegram_id') == user_telegram_id), None)
    log_identifier = user_for_log.get('nickname') if user_for_log else f"TGID_{user_telegram_id}"

    original_task_count = len(config['checkin_tasks'])
    
    config['checkin_tasks'] = [
        t for t in config['checkin_tasks']
        if not (t.get('user_telegram_id') == user_telegram_id and t.get('bot_username') == bot_username)
    ]

    if len(config['checkin_tasks']) < original_task_count:
        save_config(config)
        update_scheduler()
        logger.info(f"签到任务已删除: 用户 {log_identifier} (TGID: {user_telegram_id}) -> 机器人 {bot_username}")
        return jsonify({"success": True, "message": "签到任务已删除。"})
    else:
        return jsonify({"success": False, "message": "未找到该签到任务。"}), 404

@app.route('/api/checkin/manual', methods=['POST'])
async def api_manual_checkin():
    config = load_config()
    api_id = config.get('api_id')
    api_hash = config.get('api_hash')

    if not api_id or not api_hash:
        return jsonify({"success": False, "message": "请先设置API ID和API Hash。"}), 400

    user_telegram_id_str = request.form.get('user_telegram_id') 
    bot_username = request.form.get('bot_username')

    if not user_telegram_id_str or not bot_username: 
        return jsonify({"success": False, "message": "未选择用户或机器人。"}), 400
    
    try:
        user_telegram_id = int(user_telegram_id_str)
    except ValueError:
        return jsonify({"success": False, "message": "无效的用户TG ID格式。"}), 400

    user_config = next((u for u in config.get('users', []) if u.get('telegram_id') == user_telegram_id and u.get('status') == 'logged_in'), None)
    if not user_config:
        return jsonify({"success": False, "message": f"用户TG ID '{user_telegram_id}' 未找到或未登录。"}), 400

    session_name = user_config.get('session_name')
    user_nickname_for_operation = user_config.get('nickname', f"TGID_{user_telegram_id}")
    bots_list = config.get('bots', [])
    bot_config = next((b for b in bots_list if isinstance(b, dict) and b.get('bot_username') == bot_username), None)
    
    strategy_identifier = "start_button_alert"
    if bot_config:
        strategy_identifier = bot_config.get('strategy', "start_button_alert")
    else:
        return jsonify({"success": False, "message": f"机器人 '{bot_username}' 未在配置中找到。"}), 400

    from checkin_strategies import STRATEGY_MAPPING, get_strategy_display_name
    if strategy_identifier not in STRATEGY_MAPPING:
        logger.warning(f"手动签到: 机器人 {bot_username} 配置了无效策略 '{strategy_identifier}'，将使用默认策略 'start_button_alert'。")
        strategy_identifier = "start_button_alert"
        
    strategy_display = get_strategy_display_name(strategy_identifier)

    result = await telethon_check_in(api_id, api_hash, user_nickname_for_operation, session_name, bot_username, strategy_identifier)

    log_entry = {
        "checkin_type": f"手动签到 ({strategy_display})",
        "user_nickname": user_nickname_for_operation, 
        "bot_username": bot_username,
        "success": result.get("success"),
        "message": result.get("message")
    }
    save_daily_checkin_log(log_entry)

    return jsonify(result)

async def api_checkin_all_tasks_internal(source="http_manual_all"):
    config = load_config()
    api_id = config.get('api_id')
    api_hash = config.get('api_hash')

    if not api_id or not api_hash:
        message = "请先设置API ID和API Hash。"
        if source.startswith("http"):
            return jsonify({"success": False, "message": message}), 400
        else:
            logger.warning(f"内部调用签到所有任务失败: {message}")
            return {"success": False, "message": message, "all_tasks_results": []}
    tasks_to_run = config.get('checkin_tasks', [])
    if not tasks_to_run:
        message = "没有配置签到任务。"
        if source.startswith("http"):
            return jsonify({"success": True, "message": message, "all_tasks_results": []}), 200
        else:
            logger.info(f"内部调用签到所有任务: {message}")
            return {"success": True, "message": message, "all_tasks_results": []}

    results_list = []
    all_users_list = config.get('users', [])
    user_map_by_id = {user['telegram_id']: user for user in all_users_list if 'telegram_id' in user}

    processed_bots = get_processed_bots_list(config.get('bots', []))
    bot_strategy_map_all = {bot['bot_username']: bot['strategy'] for bot in processed_bots if bot.get('bot_username')}

    for task_config_entry in tasks_to_run:
        bot_username = task_config_entry.get('bot_username')
        user_telegram_id = task_config_entry.get('user_telegram_id')

        user_config = None
        log_nickname = "未知用户"

        if user_telegram_id:
            user_config = user_map_by_id.get(user_telegram_id)
            if user_config:
                log_nickname = user_config.get('nickname', f"TGID_{user_telegram_id}")
            else: 
                log_nickname = f"TGID_{user_telegram_id} (用户数据不存在)"
        
        if not bot_username or not user_telegram_id:
            logger.warning(f"跳过格式不正确的任务 (缺少TGID或机器人名): {task_config_entry}")
            continue

        current_task_result = {"success": False, "message": f"用户 {log_nickname} 未登录、配置不正确或用户数据不存在。"}
        
        if user_config and user_config.get('status') == 'logged_in':
            session_name = user_config.get('session_name')
            strategy_identifier = bot_strategy_map_all.get(bot_username, "start_button_alert")
            strategy_display = STRATEGY_DISPLAY_NAMES.get(strategy_identifier, strategy_identifier)
            try:
                current_task_result = await telethon_check_in(api_id, api_hash, log_nickname, session_name, bot_username, strategy_identifier)
            except Exception as e_checkin:
                logger.error(f"执行全部签到任务中，用户 {log_nickname}->{bot_username} (策略: {strategy_display}) 时发生异常: {e_checkin}")
                current_task_result = {"success": False, "message": f"签到时发生内部错误: {str(e_checkin)}"}
        elif user_config: 
             current_task_result = {"success": False, "message": f"用户 {log_nickname} 未登录。"}

        results_list.append({"task": {"user_nickname": log_nickname, "bot_username": bot_username, "strategy_used": strategy_identifier}, "result": current_task_result})
    
        log_entry = {
            "checkin_type": f"手动批量签到 ({strategy_display})",
            "user_nickname": log_nickname, 
            "bot_username": bot_username,
            "success": current_task_result.get("success"),
            "message": current_task_result.get("message")
        }
        save_daily_checkin_log(log_entry)
        
        task_config_entry["last_auto_checkin_status"] = "成功" if current_task_result.get("success") else "失败: " + current_task_result.get("message", "")[:50]
        task_config_entry["last_auto_checkin_time"] = format_datetime_filter(None)

    save_config(config)

    final_response = {"all_tasks_results": results_list, "message": "所有任务执行完毕。"}
    if source.startswith("http"):
        return jsonify(final_response)
    else:
        return final_response

@app.route('/api/checkin/all', methods=['POST'])
async def api_checkin_all_tasks():
    return await api_checkin_all_tasks_internal(source="http_manual_all")

if __name__ == '__main__':
    logger.info("启动Flask应用...")
    app.run(debug=True, host='0.0.0.0', port=5055, use_reloader=False)
