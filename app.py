from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import logging
import os
import re
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from telethon import TelegramClient, errors

from config import load_config, save_config
from telegram_client import telethon_check_in, get_session_name
from log import load_daily_checkin_log, save_daily_checkin_log
from scheduler import update_scheduler, scheduler, run_scheduled_task_sync, get_random_time_in_range
from utils import format_datetime_filter, get_masked_api_credentials

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.urandom(24)

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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        config = load_config()
        web_users = config.get('web_users', [])
        
        user_data = next((u for u in web_users if u.get('username') == username), None)

        if not web_users:
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
    if not config.get('api_id') or not config.get('api_hash'):
        return redirect(url_for('setup_page'))

    todays_checkin_log = load_daily_checkin_log()

    return render_template('index.html', config=config, todays_checkin_log=todays_checkin_log)

app.jinja_env.filters['format_datetime'] = format_datetime_filter

@app.route('/setup', methods=['GET', 'POST'])
@login_required
def setup_page():
    config = load_config()

    if request.method == 'POST':
        form_data = request.form.to_dict()

        render_context = {
            'original_api_id': config.get('api_id'),
            'original_api_hash': config.get('api_hash'),
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
                current_api_id_disp, current_api_hash_disp = get_masked_api_credentials(config)
                render_context['api_id_display'] = current_api_id_disp
                render_context['api_hash_display'] = current_api_hash_disp
                return render_template('setup.html', **render_context)

            start_total_minutes = s_start_h * 60 + s_start_m
            end_total_minutes = s_end_h * 60 + s_end_m
            if start_total_minutes >= end_total_minutes:
                flash("调度结束时间必须晚于开始时间。请重新输入。", "danger")
                current_api_id_disp, current_api_hash_disp = get_masked_api_credentials(config)
                render_context['api_id_display'] = current_api_id_disp
                render_context['api_hash_display'] = current_api_hash_disp
                return render_template('setup.html', **render_context)

            validated_scheduler_settings = {
                'scheduler_enabled': True if form_data.get('scheduler_enabled') == 'on' else False,
                'scheduler_range_start_hour': s_start_h,
                'scheduler_range_start_minute': s_start_m,
                'scheduler_range_end_hour': s_end_h,
                'scheduler_range_end_minute': s_end_m,
            }
        except (ValueError, TypeError):
            flash("调度时间范围格式无效。请输入有效的数字。", "danger")
            current_api_id_disp, current_api_hash_disp = get_masked_api_credentials(config)
            render_context['api_id_display'] = current_api_id_disp
            render_context['api_hash_display'] = current_api_hash_disp
            return render_template('setup.html', **render_context)

        submitted_api_id = form_data.get('api_id')
        submitted_api_hash = form_data.get('api_hash')
        current_api_id_display_val, current_api_hash_display_val = get_masked_api_credentials(config)

        if submitted_api_id == current_api_id_display_val and submitted_api_id is not None:
            pass
        elif submitted_api_id == "":
            config['api_id'] = None
        else:
            config['api_id'] = submitted_api_id

        if submitted_api_hash == current_api_hash_display_val and submitted_api_hash is not None:
            pass
        elif submitted_api_hash == "":
            config['api_hash'] = None
        else:
            config['api_hash'] = submitted_api_hash

        config.update(validated_scheduler_settings)

        save_config(config)
        update_scheduler()
        flash("设置已成功保存。", "success")

        config = load_config()

    api_id_display_val, api_hash_display_val = get_masked_api_credentials(config)

    return render_template('setup.html',
                           api_id_display=api_id_display_val,
                           api_hash_display=api_hash_display_val,
                           original_api_id=config.get('api_id'),
                           original_api_hash=config.get('api_hash'),
                           scheduler_enabled=config.get('scheduler_enabled'),
                           scheduler_range_start_hour=config.get('scheduler_range_start_hour'),
                           scheduler_range_start_minute=config.get('scheduler_range_start_minute'),
                           scheduler_range_end_hour=config.get('scheduler_range_end_hour'),
                           scheduler_range_end_minute=config.get('scheduler_range_end_minute'))

@app.route('/users', methods=['GET'])
@login_required
def users_page():
    config = load_config()
    if not config.get('api_id') or not config.get('api_hash'):
        return redirect(url_for('setup_page'))
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

            old_session_path = f"{otp_flow_session_name}.session" # otp_flow_session_name already includes DATA_DIR
            new_session_path = f"{final_session_name_for_system}.session" # final_session_name_for_system now includes DATA_DIR

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

    config['users'] = [u for u in config.get('users', []) if u.get('nickname') != nickname_to_delete]
    
    if 'checkin_tasks' in config:
        config['checkin_tasks'] = [t for t in config['checkin_tasks'] if t.get('user_nickname') != nickname_to_delete]

    session_file_to_delete = os.path.join(DATA_DIR, f"{get_session_name(nickname_to_delete)}.session")
    if os.path.exists(session_file_to_delete):
        try:
            os.remove(session_file_to_delete)
            logger.info(f"已删除会话文件: {session_file_to_delete}")
        except OSError as e:
            logger.error(f"删除会话文件 {session_file_to_delete} 失败: {e}")

    if len(config.get('users', [])) < original_user_count:
        save_config(config)
        logger.info(f"用户 {nickname_to_delete} 已删除。")
        return jsonify({"success": True, "message": f"用户 {nickname_to_delete} 已删除。"})
    else:
        return jsonify({"success": False, "message": "删除用户时发生错误或用户未找到。"}), 404

@app.route('/bots', methods=['GET'])
@login_required
def bots_page():
    config = load_config()
    if not config.get('api_id') or not config.get('api_hash'):
        return redirect(url_for('setup_page'))
    return render_template('bots.html', bots=config.get('bots', []), users=config.get('users', []))

@app.route('/api/bots/add', methods=['POST'])
def api_add_bot():
    config = load_config()
    bot_username = request.form.get('bot_username')
    if not bot_username:
        return jsonify({"success": False, "message": "未提供机器人用户名。"}), 400

    if bot_username not in config['bots']:
        config['bots'].append(bot_username)
        save_config(config)
        logger.info(f"机器人 {bot_username} 已添加。")
        return jsonify({"success": True, "message": "机器人已添加。"})
    else:
        return jsonify({"success": False, "message": "该机器人已存在。"}), 400

@app.route('/api/bots/delete', methods=['POST'])
def api_delete_bot():
    config = load_config()
    bot_to_delete = request.form.get('bot_username')
    if not bot_to_delete:
        return jsonify({"success": False, "message": "未提供机器人用户名。"}), 400

    if bot_to_delete in config['bots']:
        config['bots'].remove(bot_to_delete)
        config['checkin_tasks'] = [t for t in config['checkin_tasks'] if t['bot_username'] != bot_to_delete]
        save_config(config)
        logger.info(f"机器人 {bot_to_delete} 已删除。")
        return jsonify({"success": True, "message": "机器人已删除。"})
    else:
        return jsonify({"success": False, "message": "未找到该机器人。"}), 404

@app.route('/tasks', methods=['GET'])
@login_required
def tasks_page():
    config = load_config()
    if not config.get('api_id') or not config.get('api_hash'):
        return redirect(url_for('setup_page'))
    logged_in_users = [u for u in config.get('users', []) if u.get('status') == 'logged_in']
    valid_tasks = []
    if 'checkin_tasks' in config:
        for task in config['checkin_tasks']:
            if 'user_nickname' in task and 'bot_username' in task:
                valid_tasks.append(task)
            elif 'user_phone' in task:
                user_obj = next((u for u in config.get('users', []) if u.get('phone') == task['user_phone']), None)
                if user_obj and 'nickname' in user_obj:
                    task['user_nickname'] = user_obj['nickname']
                    del task['user_phone']
                    valid_tasks.append(task)
    
    return render_template('tasks.html', tasks=valid_tasks, users=logged_in_users, bots=config.get('bots', []))

@app.route('/api/tasks/add', methods=['POST'])
def api_add_task():
    config = load_config()
    user_nickname = request.form.get('user_nickname')
    bot_username = request.form.get('bot_username')

    if not user_nickname or not bot_username:
        return jsonify({"success": False, "message": "未选择用户或机器人。"}), 400

    new_task = {
        "user_nickname": user_nickname,
        "bot_username": bot_username,
        "last_auto_checkin_status": None,
        "last_auto_checkin_time": None
    }
    
    if 'checkin_tasks' not in config or not isinstance(config['checkin_tasks'], list):
        config['checkin_tasks'] = []

    task_exists = any(
        t.get('user_nickname') == new_task['user_nickname'] and t.get('bot_username') == new_task['bot_username']
        for t in config['checkin_tasks']
    )

    if not task_exists:
        config['checkin_tasks'].append(new_task)
        save_config(config)
        update_scheduler() 
        logger.info(f"签到任务已添加: 用户 {user_nickname} -> 机器人 {bot_username}")
        return jsonify({"success": True, "message": "签到任务已添加。"})
    else:
        return jsonify({"success": False, "message": "该签到任务已存在。"}), 400

@app.route('/api/tasks/delete', methods=['POST'])
def api_delete_task():
    config = load_config()
    user_nickname = request.form.get('user_nickname')
    bot_username = request.form.get('bot_username')

    if 'checkin_tasks' not in config or not isinstance(config['checkin_tasks'], list):
        config['checkin_tasks'] = []

    task_to_find_dict = {"user_nickname": user_nickname, "bot_username": bot_username}
    
    original_task_count = len(config['checkin_tasks'])
    
    config['checkin_tasks'] = [
        t for t in config['checkin_tasks']
        if not (t.get('user_nickname') == user_nickname and t.get('bot_username') == bot_username)
    ]

    if len(config['checkin_tasks']) < original_task_count:
        save_config(config)
        update_scheduler()
        logger.info(f"签到任务已删除: 用户 {user_nickname} -> 机器人 {bot_username}")
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

    user_nickname = request.form.get('user_nickname')
    bot_username = request.form.get('bot_username')

    if not user_nickname or not bot_username:
        return jsonify({"success": False, "message": "未选择用户或机器人。"}), 400

    user_config = next((u for u in config.get('users', []) if u.get('nickname') == user_nickname and u.get('status') == 'logged_in'), None)
    if not user_config:
        return jsonify({"success": False, "message": f"用户昵称 '{user_nickname}' 未找到或未登录。"}), 400

    session_name = user_config.get('session_name')
    result = await telethon_check_in(api_id, api_hash, user_nickname, session_name, bot_username)

    log_entry = {
        "type": "manual",
        "user_nickname": user_nickname,
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
    users_config = config.get('users', [])

    for task_config_entry in tasks_to_run:
        user_nickname = task_config_entry.get('user_nickname')
        bot_username = task_config_entry.get('bot_username')

        if not user_nickname or not bot_username:
            logger.warning(f"跳过格式不正确的任务: {task_config_entry}")
            continue

        user_config = next((u for u in users_config if u.get('nickname') == user_nickname and u.get('status') == 'logged_in'), None)

        current_task_result = {"success": False, "message": f"用户 {user_nickname} 未登录或配置不正确。"}
        if user_config:
            session_name = user_config.get('session_name')
            try:
                current_task_result = await telethon_check_in(api_id, api_hash, user_nickname, session_name, bot_username)
            except Exception as e_checkin:
                logger.error(f"执行全部签到任务中，用户 {user_nickname}->{bot_username} 时发生异常: {e_checkin}")
                current_task_result = {"success": False, "message": f"签到时发生内部错误: {str(e_checkin)}"}
        else:
            logger.warning(f"全部签到跳过任务: 用户 {user_nickname} -> {bot_username}，因为用户未登录或配置错误。")

        results_list.append({"task": {"user_nickname": user_nickname, "bot_username": bot_username}, "result": current_task_result})

        log_entry = {
            "type": source,
            "user_nickname": user_nickname,
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

update_scheduler()

if __name__ == '__main__':
    logger.info("启动Flask应用...")
    app.run(debug=True, host='127.0.0.1', port=5055, use_reloader=False)
