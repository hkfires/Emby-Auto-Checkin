from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from datetime import date, datetime
from config import load_config, save_config
from log import load_checkin_log_by_date
from utils.common import get_masked_api_credentials, get_processed_bots_list, update_api_credential
from utils.tg_service_api import resolve_chat_identifier
from checkin_strategies import STRATEGY_DISPLAY_NAMES, get_strategy_display_name
from utils.scheduler_api import notify_scheduler_to_reconcile
import logging

logger = logging.getLogger(__name__)
views = Blueprint('views', __name__)

@views.before_app_request
def require_api_setup():
    exempt_endpoints = ['auth.login', 'auth.logout', 'static', 'views.check_first_run_status', 'views.api_settings_page', 'views.chats', 'views.delete_chat']
    
    if current_user.is_authenticated and request.endpoint not in exempt_endpoints:
        config = load_config()
        if not config.get('api_id') or not config.get('api_hash'):
            if request.endpoint != 'views.api_settings_page':
                flash('请首先完成 Telegram API 设置以使用其他功能。', 'warning')
                return redirect(url_for('views.api_settings_page'))

@views.route('/check_first_run_status', methods=['GET'])
def check_first_run_status():
    config = load_config()
    web_users = config.get('web_users', [])
    return {'is_first_run': not web_users}

@views.route('/')
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

@views.route('/settings/api', methods=['GET', 'POST'])
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

@views.route('/settings/scheduler', methods=['GET', 'POST'])
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
        notify_scheduler_to_reconcile()
        flash("自动签到设置已保存。已在后台通知调度器更新任务，请稍后刷新查看状态。", "success")
        config = load_config()

    return render_template('scheduler_settings.html',
                           scheduler_enabled=config.get('scheduler_enabled'),
                           scheduler_time_slots=config.get('scheduler_time_slots', []),
                           root_mode=root_mode)

@views.route('/settings/llm', methods=['GET', 'POST'])
@login_required
def llm_settings_page():
   config = load_config()
   return render_template('llm_settings.html', llm_settings=config.get('llm_settings', {}))

@views.route('/users', methods=['GET'])
@login_required
def users_page():
    config = load_config()
    return render_template('users.html', users=config.get('users', []))

@views.route('/bots', methods=['GET'])
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

@views.route('/chats', methods=['GET', 'POST'])
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
        return redirect(url_for('views.chats'))
    
    chat_specific_strategies = {}
    for key, strategy_info in STRATEGY_DISPLAY_NAMES.items():
        if isinstance(strategy_info, dict) and strategy_info.get("target_type") in ["chat", "any"]:
            chat_specific_strategies[key] = strategy_info.get("name", key)

    return render_template('chats.html', 
                           users=config.get('users', []), 
                           config_chats=config.get('chats', []),
                           strategy_display_names=chat_specific_strategies)

@views.route('/chats/delete/<int:chat_idx>', methods=['GET'])
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
        notify_scheduler_to_reconcile()
        flash(f"群组 '{deleted_chat.get('chat_title')}' 已删除。", 'success')
        logger.info(f"群组 '{deleted_chat.get('chat_title')}' (ID: {deleted_chat.get('chat_id')}) 已删除。")
    else:
        flash('无效的群组索引，删除失败。', 'danger')
    return redirect(url_for('views.chats'))

@views.route('/tasks', methods=['GET'])
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