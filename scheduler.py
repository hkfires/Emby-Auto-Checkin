import asyncio
import logging
import random
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from config import load_config
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

def run_scheduled_task_sync(user_phone, user_nickname, bot_username):
    async def async_task_logic():
        config = load_config()
        api_id = config.get('api_id')
        api_hash = config.get('api_hash')

        if not api_id or not api_hash:
            logger.error(f"计划任务 User: {user_nickname} (Phone: {user_phone}), Bot: {bot_username} 失败: API ID/Hash 未配置。")
            result = {"success": False, "message": "API ID/Hash 未配置."}
        else:
            logger.info(f"计划任务 (async_task_logic): 开始执行签到任务 User: {user_nickname} (Phone: {user_phone}), Bot: {bot_username}")
            user_config = next((u for u in config['users'] if u['phone'] == user_phone and u.get('status') == 'logged_in'), None)
            if not user_config:
                logger.warning(f"计划任务: 用户 {user_nickname} (Phone: {user_phone}) 未登录或不存在，跳过任务。")
                result = {"success": False, "message": "用户未登录或不存在"}
            else:
                session_name = user_config['session_name']
                result = await telethon_check_in(api_id, api_hash, user_nickname, session_name, bot_username)

        log_entry = {
            "type": "scheduler_single_task",
            "user_nickname": user_nickname,
            "bot_username": bot_username,
            "success": result.get("success"),
            "message": result.get("message")
        }
        save_daily_checkin_log(log_entry)
        logger.info(f"计划任务 (async_task_logic): 任务 User: {user_nickname} (Phone: {user_phone}), Bot: {bot_username} 执行完毕. Result: {result.get('success')}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        logger.info(f"计划任务 (sync_wrapper): 准备在独立事件循环中执行 User: {user_nickname} (Phone: {user_phone}), Bot: {bot_username}")
        loop.run_until_complete(async_task_logic())
        logger.info(f"计划任务 (sync_wrapper): User: {user_nickname} (Phone: {user_phone}), Bot: {bot_username} 的异步逻辑执行完毕。")
    except Exception as e:
        logger.error(f"计划任务 (sync_wrapper) User: {user_nickname} (Phone: {user_phone}), Bot: {bot_username} 执行时发生顶层错误: {e}")
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

        for task in config.get('checkin_tasks', []):
            if not isinstance(task, dict):
                logger.warning(f"发现无效的任务条目 (非字典类型): {task}，已跳过。")
                continue

            task_user_nickname = task.get('user_nickname')
            bot_username = task.get('bot_username')

            if not task_user_nickname or not bot_username:
                logger.warning(f"任务条目缺少 'user_nickname' 或 'bot_username': {task}，已跳过。")
                continue

            user_phone = None
            user_found = False
            for user_config in config.get('users', []):
                if user_config.get('nickname') == task_user_nickname:
                    user_phone = user_config.get('phone')
                    user_found = True
                    break
            
            if not user_found:
                logger.warning(f"在用户列表中未找到昵称为 '{task_user_nickname}' 的用户，任务 {task} 已跳过。")
                continue

            if not user_phone:
                logger.warning(f"找到用户 '{task_user_nickname}' 但其电话号码未配置，任务 {task} 已跳过。")
                continue

            job_id = f"checkin_job_{user_phone}_{bot_username}" # Use actual user_phone for job_id
            
            rand_h, rand_m = get_random_time_in_range(start_h, start_m, end_h, end_m)

            if rand_h is None: # This check remains, in case get_random_time_in_range returns None
                logger.warning(f"无法为任务 {job_id} (用户: {task_user_nickname}, 机器人: {bot_username}) 计算随机时间，因时间范围无效。")
                continue
            
            try:
                scheduler.add_job(
                    run_scheduled_task_sync,
                    trigger=CronTrigger(hour=rand_h, minute=rand_m, timezone='Asia/Shanghai'),
                    args=[user_phone, task_user_nickname, bot_username], # Add task_user_nickname
                    id=job_id,
                    name=f"Checkin {task_user_nickname} ({user_phone}) with {bot_username}",
                    replace_existing=True
                )
                logger.info(f"已为任务 (用户: {task_user_nickname}, 机器人: {bot_username}) 设置签到时间: {rand_h:02d}:{rand_m:02d} (Job ID: {job_id})")
            except Exception as e_add_job:
                 logger.error(f"为任务 (用户: {task_user_nickname}, 机器人: {bot_username}, Job ID: {job_id}) 添加调度时发生错误: {e_add_job}")
    else:
        logger.info("每日自动签到任务已禁用。所有单个任务作业已被移除。")
