import asyncio
import logging
import random
from datetime import date, datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from config import load_config, save_config
from log import save_daily_checkin_log

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

def get_random_time_in_range(start_h, start_m, end_h, end_m):
    start_total_minutes = start_h * 60 + start_m
    end_total_minutes = end_h * 60 + end_m

    if start_total_minutes >= end_total_minutes:
        logger.warning(f"无效的时间范围: {start_h}:{start_m} - {end_h}:{end_m}. 将使用开始时间。")
        return start_h, start_m

    random_total_minutes = random.randint(start_total_minutes, end_total_minutes -1)

    rand_h = random_total_minutes // 60
    rand_m = random_total_minutes % 60
    return rand_h, rand_m

def run_scheduled_task_sync_wrapper(user_telegram_id, target_type, target_identifier, task_config_from_scheduler):
    from app import execute_telegram_action_wrapper
    from checkin_strategies import get_strategy_display_name

    async def async_task_execution():
        config = load_config()
        api_id = config.get('api_id')
        api_hash = config.get('api_hash')

        user_config = next((u for u in config.get('users', []) if u.get('telegram_id') == user_telegram_id), None)
        if not user_config:
            logger.error(f"计划任务: 未找到 TGID 为 {user_telegram_id} 的用户配置。")
            return
        
        user_nickname = user_config.get('nickname', f"TGID_{user_telegram_id}")
        session_name = user_config.get('session_name')

        if not session_name:
            logger.error(f"计划任务: 用户 {user_nickname} (TGID: {user_telegram_id}) 缺少 session_name。")
            return
        
        if not api_id or not api_hash:
            logger.error(f"计划任务 User: {user_nickname}, Target: {target_identifier} 失败: API ID/Hash 未配置。")
            result = {"success": False, "message": "API ID/Hash 未配置."}
        else:
            target_config_item = None
            log_target_display_name = str(target_identifier)

            if target_type == 'bot':
                target_config_item = next((b for b in config.get('bots', []) if isinstance(b, dict) and b.get('bot_username') == target_identifier), None)
            elif target_type == 'chat':
                try:
                    chat_id_int = int(target_identifier)
                    target_config_item = next((c for c in config.get('chats', []) if isinstance(c, dict) and c.get('chat_id') == chat_id_int), None)
                    if target_config_item:
                        log_target_display_name = target_config_item.get('chat_title', str(chat_id_int))
                except ValueError:
                    logger.error(f"计划任务: 群组ID '{target_identifier}' 无效。")
                    target_config_item = None
            
            if not target_config_item:
                logger.error(f"计划任务: 目标 '{target_identifier}' (类型: {target_type}) 未在配置中找到。")
                result = {"success": False, "message": f"目标 '{target_identifier}' 未在配置中找到。"}
            else:
                logger.info(f"计划任务: 开始执行 User: {user_nickname}, Type: {target_type}, Target: {log_target_display_name}, TaskConfig: {task_config_from_scheduler}")
                result = await execute_telegram_action_wrapper(api_id, api_hash, user_nickname, session_name, target_config_item, task_config_from_scheduler)

        eff_strat_id = task_config_from_scheduler.get('strategy_identifier') or \
                       target_config_item.get('strategy') if target_config_item and 'strategy' in target_config_item else \
                       target_config_item.get('strategy_identifier') if target_config_item and 'strategy_identifier' in target_config_item else "未知"
        strategy_display = get_strategy_display_name(eff_strat_id)

        log_entry = {
            "checkin_type": f"计划任务 ({strategy_display})",
            "user_nickname": user_nickname,
            "target_type": target_type,
            "target_name": log_target_display_name,
            "success": result.get("success"),
            "message": result.get("message")
        }
        save_daily_checkin_log(log_entry)
        logger.info(f"计划任务: User: {user_nickname}, Target: {log_target_display_name} 执行完毕. Result: {result.get('success')}")
        
        for task_in_conf in config.get('checkin_tasks', []):
            match = False
            if task_in_conf.get('user_telegram_id') == user_telegram_id:
                if target_type == 'bot' and task_in_conf.get('bot_username') == target_identifier:
                    match = True
                elif target_type == 'chat' and task_in_conf.get('target_chat_id') == int(target_identifier):
                    match = True
            
            if match:
                task_in_conf["last_auto_checkin_status"] = "成功" if result.get("success") else f"失败: {result.get('message', '')[:100]}"
                task_in_conf["last_auto_checkin_time"] = datetime.now().isoformat()
                save_config(config)
                break

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        logger.info(f"计划任务 (sync_wrapper): 准备执行 User: {user_telegram_id}, Type: {target_type}, Target: {target_identifier}")
        loop.run_until_complete(async_task_execution())
        logger.info(f"计划任务 (sync_wrapper): User: {user_telegram_id}, Type: {target_type}, Target: {target_identifier} 执行完毕。")
    except Exception as e:
        logger.error(f"计划任务 (sync_wrapper) User: {user_telegram_id}, Type: {target_type}, Target: {target_identifier} 执行时发生顶层错误: {e}", exc_info=True)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

def update_scheduler():
    global scheduler
    config = load_config()

    for job in scheduler.get_jobs():
        if job.id.startswith("checkin_job_"):
            scheduler.remove_job(job.id)
    logger.info("已移除所有旧的单个签到任务作业。")

    rescheduler_job_id = 'daily_config_rescheduler_job'
    if scheduler.get_job(rescheduler_job_id):
        scheduler.remove_job(rescheduler_job_id)
        logger.info("已移除旧的每日重调度作业。")
    scheduler.add_job(
        update_scheduler,
        trigger=CronTrigger(hour=0, minute=1, timezone='Asia/Shanghai'),
        id=rescheduler_job_id,
        name='Daily Rescheduler for Checkin Tasks',
        replace_existing=True
    )
    logger.info("已设置每日任务时间重调度作业 (00:01 Asia/Shanghai)。")

    if config.get('scheduler_enabled'):
        scheduler_time_slots = config.get('scheduler_time_slots', [])
        if not scheduler_time_slots:
            logger.error("调度器已启用，但未配置任何时间段 (scheduler_time_slots 为空)。自动签到将不会为任务创建调度。")
            return
        
        for slot in scheduler_time_slots:
            slot_name = slot.get('name', f"ID {slot.get('id')}")
            logger.info(f"可用调度时间段: {slot_name} ({slot.get('start_hour',0):02d}:{slot.get('start_minute',0):02d} - {slot.get('end_hour',23):02d}:{slot.get('end_minute',59):02d})")

        all_users = config.get('users', [])
        user_map_by_id = {user['telegram_id']: user for user in all_users if 'telegram_id' in user}
        
        today_str = date.today().isoformat()
        config_changed = False

        for task_entry in config.get('checkin_tasks', []):
            if not isinstance(task_entry, dict):
                logger.warning(f"发现无效的任务条目 (非字典类型): {task_entry}，已跳过。")
                continue

            user_telegram_id = task_entry.get('user_telegram_id')
            if not user_telegram_id:
                logger.warning(f"任务条目缺少 'user_telegram_id': {task_entry}，已跳过。")
                continue
            
            user_config_for_task = user_map_by_id.get(user_telegram_id)
            if not user_config_for_task:
                logger.warning(f"任务指定 TGID {user_telegram_id} 但在用户列表中未找到，任务 {task_entry} 已跳过。")
                continue
            
            current_task_user_nickname = user_config_for_task.get('nickname', f"TGID_{user_telegram_id}")
            if user_config_for_task.get('status') != 'logged_in':
                logger.info(f"用户 {current_task_user_nickname} (TGID: {user_telegram_id}) 未登录，跳过为其安排任务。")
                continue

            target_type = None
            target_identifier = None
            job_id_suffix = None
            display_target_name = "未知目标"

            if task_entry.get('bot_username'):
                target_type = 'bot'
                target_identifier = task_entry.get('bot_username')
                job_id_suffix = f"bot_{target_identifier}"
                display_target_name = target_identifier
            elif task_entry.get('target_chat_id'):
                target_type = 'chat'
                target_identifier = task_entry.get('target_chat_id')
                job_id_suffix = f"chat_{target_identifier}"
                chat_info = next((c for c in config.get('chats', []) if c.get('chat_id') == target_identifier), None)
                display_target_name = chat_info.get('chat_title', str(target_identifier)) if chat_info else str(target_identifier)
            else:
                logger.warning(f"任务条目既无 'bot_username' 也无 'target_chat_id': {task_entry}，已跳过。")
                continue
            
            job_id = f"checkin_job_{user_telegram_id}_{job_id_suffix}"
            
            task_last_scheduled_date = task_entry.get('last_scheduled_date')
            task_scheduled_h = task_entry.get('scheduled_hour')
            task_scheduled_m = task_entry.get('scheduled_minute')
            current_h, current_m = -1, -1

            selected_slot_id = task_entry.get('selected_time_slot_id')
            task_specific_time_slot = None
            if selected_slot_id is not None:
                task_specific_time_slot = next((s for s in scheduler_time_slots if s.get('id') == selected_slot_id), None)

            if not task_specific_time_slot:
                if scheduler_time_slots: 
                    task_specific_time_slot = scheduler_time_slots[0]
                    logger.warning(f"任务 {job_id} (用户: {current_task_user_nickname}, 目标: {display_target_name}) 的 selected_time_slot_id '{selected_slot_id}' 无效或未设置。将使用第一个可用时间段: ID {task_specific_time_slot.get('id')}")
                else:
                    logger.error(f"任务 {job_id} (用户: {current_task_user_nickname}, 目标: {display_target_name}) 无法找到有效的时间段，且全局时间段列表为空。跳过此任务的调度。")
                    continue
            
            slot_start_h = task_specific_time_slot.get('start_hour', 8)
            slot_start_m = task_specific_time_slot.get('start_minute', 0)
            slot_end_h = task_specific_time_slot.get('end_hour', 22)
            slot_end_m = task_specific_time_slot.get('end_minute', 0)
            slot_name_for_log = task_specific_time_slot.get('name', f"ID {task_specific_time_slot.get('id')}")

            if not (0 <= slot_start_h <= 23 and 0 <= slot_start_m <= 59 and \
                    0 <= slot_end_h <= 23 and 0 <= slot_end_m <= 59 and \
                    (slot_start_h * 60 + slot_start_m < slot_end_h * 60 + slot_end_m)):
                logger.error(f"任务 {job_id} (用户: {current_task_user_nickname}, 目标: {display_target_name}) 使用的时间段 '{slot_name_for_log}' ({slot_start_h:02d}:{slot_start_m:02d} - {slot_end_h:02d}:{slot_end_m:02d}) 无效。跳过此任务的调度。")
                continue

            if task_last_scheduled_date == today_str and \
               isinstance(task_scheduled_h, int) and isinstance(task_scheduled_m, int) and \
               0 <= task_scheduled_h <= 23 and 0 <= task_scheduled_m <= 59:
                saved_total_minutes = task_scheduled_h * 60 + task_scheduled_m
                slot_start_total_minutes = slot_start_h * 60 + slot_start_m
                slot_end_total_minutes = slot_end_h * 60 + slot_end_m
                if slot_start_total_minutes <= saved_total_minutes < slot_end_total_minutes:
                    current_h, current_m = task_scheduled_h, task_scheduled_m
                    logger.info(f"任务 {job_id} (用户: {current_task_user_nickname}, 目标: {display_target_name}) 今天已被安排在 {current_h:02d}:{current_m:02d} (在时段 '{slot_name_for_log}' 内)，将使用此时间。")
                else:
                    logger.info(f"任务 {job_id} (用户: {current_task_user_nickname}, 目标: {display_target_name}) 今天已安排的时间 {task_scheduled_h:02d}:{task_scheduled_m:02d} 不在当前所选时段 '{slot_name_for_log}' ({slot_start_h:02d}:{slot_start_m:02d}-{slot_end_h:02d}:{slot_end_m:02d}) 内。将重新生成随机时间。")
            
            if current_h == -1 and current_m == -1:
                rand_h, rand_m = get_random_time_in_range(slot_start_h, slot_start_m, slot_end_h, slot_end_m)
                if rand_h is None: 
                    logger.warning(f"无法为任务 {job_id} (用户: {current_task_user_nickname}, 目标: {display_target_name}) 在时段 '{slot_name_for_log}' 内计算随机时间，因时间范围无效。")
                    continue
                current_h, current_m = rand_h, rand_m
                task_entry['last_scheduled_date'] = today_str
                task_entry['scheduled_hour'] = current_h
                task_entry['scheduled_minute'] = current_m
                config_changed = True
                logger.info(f"任务 {job_id} (用户: {current_task_user_nickname}, 目标: {display_target_name}) 今天首次安排 (或重新安排) 在时段 '{slot_name_for_log}' 内，随机时间: {current_h:02d}:{current_m:02d}。")
            
            try:
                scheduler.add_job(
                    run_scheduled_task_sync_wrapper,
                    trigger=CronTrigger(hour=current_h, minute=current_m, timezone='Asia/Shanghai'),
                    args=[user_telegram_id, target_type, target_identifier, task_entry],
                    id=job_id,
                    name=f"Task: {current_task_user_nickname} -> {target_type} {display_target_name}",
                    replace_existing=True
                )
                logger.info(f"已为任务 (用户: {current_task_user_nickname}, 目标: {display_target_name}) 设置/更新签到时间: {current_h:02d}:{current_m:02d} (Job ID: {job_id})")
            except Exception as e_add_job:
                 logger.error(f"为任务 (用户: {current_task_user_nickname}, 目标: {display_target_name}, Job ID: {job_id}) 添加调度时发生错误: {e_add_job}", exc_info=True)
        
        if config_changed:
            save_config(config)
            logger.info("签到任务配置已更新。")
    else:
        logger.info("每日自动签到任务已禁用。所有单个任务作业已被移除。")
