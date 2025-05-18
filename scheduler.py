import asyncio
import logging
import random
from datetime import date
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from config import load_config, save_config
from telegram_client import telethon_check_in
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

def run_scheduled_task_sync(user_telegram_id, bot_username):
    async def async_task_logic():
        config = load_config()
        api_id = config.get('api_id')
        api_hash = config.get('api_hash')

        user_nickname = f"TGID_{user_telegram_id}"
        session_name = None
        user_found = False
        for user_conf in config.get('users', []):
            if user_conf.get('telegram_id') == user_telegram_id:
                user_nickname = user_conf.get('nickname', user_nickname)
                session_name = user_conf.get('session_name')
                user_found = True
                break
        
        if not user_found:
            logger.error(f"计划任务 (async_task_logic): 未找到 TGID 为 {user_telegram_id} 的用户配置。")
        elif not session_name:
            logger.error(f"计划任务 (async_task_logic): 用户 {user_nickname} (TGID: {user_telegram_id}) 缺少 session_name。")

        log_identifier = f"{user_nickname} (TGID: {user_telegram_id})" if user_telegram_id else user_nickname

        strategy_identifier = "start_button_alert"
        bots_list = config.get('bots', [])
        found_strategy_for_bot = False
        for bot_config in bots_list:
            if isinstance(bot_config, dict) and bot_config.get('bot_username') == bot_username:
                configured_strategy = bot_config.get('strategy')
                if configured_strategy:
                    strategy_identifier = configured_strategy
                    logger.info(f"计划任务 (async_task_logic): 为 Bot: {bot_username} 从 'bots' 配置中找到策略: {strategy_identifier}")
                else:
                    logger.warning(f"计划任务 (async_task_logic): Bot: {bot_username} 在 'bots' 配置中未指定 'strategy'，将使用默认策略: {strategy_identifier}")
                found_strategy_for_bot = True
                break
        
        if not found_strategy_for_bot:
            logger.warning(f"计划任务 (async_task_logic): 未在 'bots' 配置列表中找到 Bot: {bot_username} 的策略定义，将使用默认策略: {strategy_identifier}")

        if not api_id or not api_hash:
            logger.error(f"计划任务 User: {log_identifier}, Bot: {bot_username} 失败: API ID/Hash 未配置。")
            result = {"success": False, "message": "API ID/Hash 未配置."}
        elif not session_name:
             logger.error(f"计划任务 User: {log_identifier}, Bot: {bot_username} 失败: Session name 未找到或无效。")
             result = {"success": False, "message": "Session name 未找到或无效."}
        elif not user_found:
            logger.error(f"计划任务 User: {log_identifier}, Bot: {bot_username} 失败: 用户配置未找到。")
            result = {"success": False, "message": "用户配置未找到。"}
        else:
            logger.info(f"计划任务 (async_task_logic): 开始执行签到任务 User: {log_identifier}, Bot: {bot_username}, Strategy: {strategy_identifier}")
            result = await telethon_check_in(api_id, api_hash, user_nickname, session_name, bot_username, strategy_identifier)

        log_entry = {
            "checkin_type": "自动签到",
            "user_nickname": user_nickname,
            "bot_username": bot_username,
            "success": result.get("success"),
            "message": result.get("message"),
            "strategy_used": strategy_identifier 
        }
        save_daily_checkin_log(log_entry)
        logger.info(f"计划任务 (async_task_logic): 任务 User: {log_identifier}, Bot: {bot_username} 执行完毕. Result: {result.get('success')}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        logger.info(f"计划任务 (sync_wrapper): 准备在独立事件循环中执行任务 for TGID: {user_telegram_id}, Bot: {bot_username}")
        loop.run_until_complete(async_task_logic())
        logger.info(f"计划任务 (sync_wrapper): TGID: {user_telegram_id}, Bot: {bot_username} 的异步逻辑执行完毕。")
    except Exception as e:
        logger.error(f"计划任务 (sync_wrapper) TGID: {user_telegram_id}, Bot: {bot_username} 执行时发生顶层错误: {e}")
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
        start_h = config.get('scheduler_range_start_hour', 8)
        start_m = config.get('scheduler_range_start_minute', 0)
        end_h = config.get('scheduler_range_end_hour', 22)
        end_m = config.get('scheduler_range_end_minute', 0)

        if not (0 <= start_h <= 23 and 0 <= start_m <= 59 and \
                0 <= end_h <= 23 and 0 <= end_m <= 59 and \
                (start_h * 60 + start_m < end_h * 60 + end_m)):
            logger.error(f"无效的调度时间范围: {start_h}:{start_m} - {end_h}:{end_m}. 自动签到将不会为任务创建调度。")
            return

        logger.info(f"自动签到已启用。时间范围: {start_h:02d}:{start_m:02d} - {end_h:02d}:{end_m:02d}")

        all_users = config.get('users', [])
        user_map_by_id = {user['telegram_id']: user for user in all_users if 'telegram_id' in user}
        
        today_str = date.today().isoformat()
        config_changed = False

        for task in config.get('checkin_tasks', []):
            if not isinstance(task, dict):
                logger.warning(f"发现无效的任务条目 (非字典类型): {task}，已跳过。")
                continue

            bot_username = task.get('bot_username')
            if not bot_username:
                logger.warning(f"任务条目缺少 'bot_username': {task}，已跳过。")
                continue

            user_telegram_id = task.get('user_telegram_id')
            if not user_telegram_id:
                logger.warning(f"任务条目缺少 'user_telegram_id': {task}，已跳过。")
                continue

            user_config_for_task = user_map_by_id.get(user_telegram_id)
            if not user_config_for_task:
                logger.warning(f"任务指定 TGID {user_telegram_id} 但在用户列表中未找到，任务 {task} 已跳过。")
                continue
            
            current_task_user_nickname = user_config_for_task.get('nickname', f"TGID_{user_telegram_id}")
            current_task_session_name = user_config_for_task.get('session_name')

            if not current_task_session_name:
                logger.warning(f"用户 {current_task_user_nickname} (TGID: {user_telegram_id}) 缺少 session_name，任务 {task} 已跳过。")
                continue
            
            if user_config_for_task.get('status') != 'logged_in':
                logger.info(f"用户 {current_task_user_nickname} (TGID: {user_telegram_id}) 未登录，跳过为其安排任务。")
                continue

            job_id = f"checkin_job_{user_telegram_id}_{bot_username}"
            
            task_last_scheduled_date = task.get('last_scheduled_date')
            task_scheduled_h = task.get('scheduled_hour')
            task_scheduled_m = task.get('scheduled_minute')

            current_h, current_m = -1, -1

            if task_last_scheduled_date == today_str and \
               isinstance(task_scheduled_h, int) and isinstance(task_scheduled_m, int) and \
               0 <= task_scheduled_h <= 23 and 0 <= task_scheduled_m <= 59:
                current_h, current_m = task_scheduled_h, task_scheduled_m
                logger.info(f"任务 {job_id} 今天已被安排在 {current_h:02d}:{current_m:02d}，将使用此时间。")
            else:
                rand_h, rand_m = get_random_time_in_range(start_h, start_m, end_h, end_m)
                if rand_h is None: 
                    logger.warning(f"无法为任务 {job_id} (用户: {current_task_user_nickname}, 机器人: {bot_username}) 计算随机时间，因时间范围无效。")
                    continue
                current_h, current_m = rand_h, rand_m
                task['last_scheduled_date'] = today_str
                task['scheduled_hour'] = current_h
                task['scheduled_minute'] = current_m
                config_changed = True
                logger.info(f"任务 {job_id} 今天首次安排，随机时间: {current_h:02d}:{current_m:02d}。")
            
            try:
                scheduler.add_job(
                    run_scheduled_task_sync,
                    trigger=CronTrigger(hour=current_h, minute=current_m, timezone='Asia/Shanghai'),
                    args=[user_telegram_id, bot_username],
                    id=job_id,
                    name=f"Checkin {current_task_user_nickname} (TGID:{user_telegram_id}) with {bot_username}",
                    replace_existing=True
                )
                logger.info(f"已为任务 (用户: {current_task_user_nickname} TGID:{user_telegram_id}, 机器人: {bot_username}) 设置/更新签到时间: {current_h:02d}:{current_m:02d} (Job ID: {job_id})")
            except Exception as e_add_job:
                 logger.error(f"为任务 (用户: {current_task_user_nickname} TGID:{user_telegram_id}, 机器人: {bot_username}, Job ID: {job_id}) 添加调度时发生错误: {e_add_job}")
        
        if config_changed:
            save_config(config)
            logger.info("签到任务配置已更新。")
    else:
        logger.info("每日自动签到任务已禁用。所有单个任务作业已被移除。")
