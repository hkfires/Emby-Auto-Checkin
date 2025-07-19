import logging
import random
import time
import asyncio
from apscheduler.triggers.cron import CronTrigger
from actions import execute_telegram_action_wrapper
from checkin_strategies import get_strategy_display_name
from config import load_config
from log import save_daily_checkin_log
from scheduler_instance import scheduler

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_random_time_in_range(start_h, start_m, end_h, end_m, start_s=0, end_s=0):
    start_total_seconds = start_h * 3600 + start_m * 60 + start_s
    end_total_seconds = end_h * 3600 + end_m * 60 + end_s

    if start_total_seconds >= end_total_seconds:
        logger.warning(f"无效的时间范围: {start_h}:{start_m}:{start_s} - {end_h}:{end_m}:{end_s}. 将使用开始时间。")
        return start_h, start_m, start_s

    random_total_seconds = random.randint(start_total_seconds, end_total_seconds - 1)

    rand_h = random_total_seconds // 3600
    rand_m = (random_total_seconds % 3600) // 60
    rand_s = random_total_seconds % 60
    return rand_h, rand_m, rand_s

async def run_checkin_task(user_telegram_id, target_type, target_identifier, task_config):
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
                target_config_item = None
        
        if not target_config_item:
            result = {"success": False, "message": f"目标 '{target_identifier}' 未在配置中找到。"}
        else:
            logger.info(f"计划任务: 开始执行 User: {user_nickname}, Type: {target_type}, Target: {log_target_display_name}")
            result = await execute_telegram_action_wrapper(api_id, api_hash, user_nickname, session_name, target_config_item, task_config)

    eff_strat_id = task_config.get('strategy_identifier') or \
                   (target_config_item.get('strategy') if target_config_item and 'strategy' in target_config_item else None) or \
                   (target_config_item.get('strategy_identifier') if target_config_item and 'strategy_identifier' in target_config_item else "未知")
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

def run_checkin_task_sync(user_telegram_id, target_type, target_identifier, task_config):
    try:
        asyncio.run(run_checkin_task(user_telegram_id, target_type, target_identifier, task_config))
    except Exception as e:
        logger.error(f"在同步包装器内执行任务 (User: {user_telegram_id}, Target: {target_identifier}) 时发生错误: {e}", exc_info=True)

def log_scheduled_jobs():
    logger.info("--- 当日任务计划总结 ---")
    checkin_jobs = [job for job in scheduler.get_jobs() if job.id and job.id.startswith("checkin_job_")]
    
    if not checkin_jobs:
        logger.info("没有已安排的签到任务。")
        return

    sorted_jobs = sorted(checkin_jobs, key=lambda j: j.next_run_time)

    for job in sorted_jobs:
        run_time_str = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')
        logger.info(f"任务: {job.name} | 计划时间: {run_time_str}")
    logger.info("--------------------------")


def reconcile_tasks():
    logger.info("开始核对任务...")
    config = load_config()
    
    if not config.get('scheduler_enabled'):
        logger.info("调度器已禁用，跳过任务核对。")
        return

    expected_job_ids = set()
    user_map_by_id = {user['telegram_id']: user for user in config.get('users', []) if 'telegram_id' in user}
    for task_entry in config.get('checkin_tasks', []):
        user_telegram_id = task_entry.get('user_telegram_id')
        user_config = user_map_by_id.get(user_telegram_id)
        if not user_config or user_config.get('status') != 'logged_in':
            continue

        job_id_suffix = None
        if task_entry.get('bot_username'):
            job_id_suffix = f"bot_{task_entry.get('bot_username')}"
        elif task_entry.get('target_chat_id'):
            job_id_suffix = f"chat_{task_entry.get('target_chat_id')}"
        
        if job_id_suffix:
            expected_job_ids.add(f"checkin_job_{user_telegram_id}_{job_id_suffix}")

    existing_job_ids = {job.id for job in scheduler.get_jobs() if job.id and job.id.startswith("checkin_job_")}

    stale_job_ids = existing_job_ids - expected_job_ids
    for job_id in stale_job_ids:
        scheduler.remove_job(job_id)
        logger.info(f"已移除过时的任务: {job_id}")

    new_job_ids = expected_job_ids - existing_job_ids
    if not new_job_ids:
        logger.info("任务核对完成，没有需要新增的任务。")
        return
        
    logger.info(f"发现 {len(new_job_ids)} 个新任务，正在安排...")
    scheduler_time_slots = config.get('scheduler_time_slots', [])
    if not scheduler_time_slots:
        logger.error("无法安排新任务，因为未配置任何时间段。")
        return

    for task_entry in config.get('checkin_tasks', []):
        user_telegram_id = task_entry.get('user_telegram_id')
        job_id_suffix = None
        if task_entry.get('bot_username'):
            job_id_suffix = f"bot_{task_entry.get('bot_username')}"
        elif task_entry.get('target_chat_id'):
            job_id_suffix = f"chat_{task_entry.get('target_chat_id')}"
        
        if not job_id_suffix: continue
        
        job_id = f"checkin_job_{user_telegram_id}_{job_id_suffix}"

        if job_id in new_job_ids:
            user_config_for_task = user_map_by_id.get(user_telegram_id)
            current_task_user_nickname = user_config_for_task.get('nickname', f"TGID_{user_telegram_id}")
            target_type = 'bot' if 'bot' in job_id_suffix else 'chat'
            target_identifier = task_entry.get('bot_username') or task_entry.get('target_chat_id')
            
            chat_info = None
            if target_type == 'chat':
                 chat_info = next((c for c in config.get('chats', []) if c.get('chat_id') == target_identifier), None)
            display_target_name = chat_info.get('chat_title', str(target_identifier)) if chat_info else str(target_identifier)

            selected_slot_id = task_entry.get('selected_time_slot_id')
            task_specific_time_slot = next((s for s in scheduler_time_slots if s.get('id') == selected_slot_id), None) or (random.choice(scheduler_time_slots) if scheduler_time_slots else None)

            if not task_specific_time_slot:
                logger.error(f"任务 {job_id} 无法找到有效的时间段，跳过调度。")
                continue

            slot_start_h, slot_start_m = task_specific_time_slot.get('start_hour', 8), task_specific_time_slot.get('start_minute', 0)
            slot_start_s = task_specific_time_slot.get('start_second', 0)
            slot_end_h, slot_end_m = task_specific_time_slot.get('end_hour', 22), task_specific_time_slot.get('end_minute', 0)
            slot_end_s = task_specific_time_slot.get('end_second', 0)
            rand_h, rand_m, rand_s = get_random_time_in_range(slot_start_h, slot_start_m, slot_end_h, slot_end_m, slot_start_s, slot_end_s)
            
            try:
                scheduler.add_job(
                    run_checkin_task_sync,
                    trigger=CronTrigger(hour=rand_h, minute=rand_m, second=rand_s),
                    args=[user_telegram_id, target_type, target_identifier, task_entry],
                    id=job_id,
                    name=f"Task: {current_task_user_nickname} -> {display_target_name}",
                    replace_existing=True
                )
            except Exception as e:
                logger.error(f"为新任务 {job_id} 添加调度时发生错误: {e}", exc_info=True)

def daily_reschedule_tasks():
    logger.info("开始每日重调度...")
    for job in scheduler.get_jobs():
        if job.id and job.id.startswith("checkin_job_"):
            scheduler.remove_job(job.id)
    logger.info("已移除所有昨日的任务作业。")
    reconcile_tasks()
    logger.info("每日重调度完成。")
    log_scheduled_jobs()

if __name__ == '__main__':
    logging.info("启动调度器...")
    scheduler.start()
    
    scheduler.add_job(
        daily_reschedule_tasks,
        trigger=CronTrigger(hour=1, minute=0, timezone='Asia/Shanghai'),
        id='daily_task_rescheduler',
        name='Daily Task Rescheduler',
        replace_existing=True
    )
    logger.info("已设置每日任务重调度作业 (01:00 Asia/Shanghai)。")

    logger.info("启动时执行任务核对...")
    reconcile_tasks()
    log_scheduled_jobs()

    logger.info("调度器已成功启动并运行。按 Ctrl+C 退出。")
    try:
        while True:
            time.sleep(2)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("调度器已关闭。")
