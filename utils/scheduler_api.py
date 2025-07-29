import logging
import random
import time
import threading
import asyncio
import os
import httpx
from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from utils.tgservice_api import execute_action
from tgservice.checkin_strategies import get_strategy_display_name
from utils.config import load_config
from utils.log import save_daily_checkin_log

jobstores = {
    'default': SQLAlchemyJobStore(url='sqlite:///data/jobs.sqlite')
}

executors = {
    'default': {'type': 'threadpool', 'max_workers': 20}
}

job_defaults = {
    'coalesce': False,
    'max_instances': 3
}

scheduler = BackgroundScheduler(
    jobstores=jobstores,
    executors=executors,
    job_defaults=job_defaults,
    timezone='Asia/Shanghai'
)

logger = logging.getLogger(__name__)

def get_random_time_in_range(start_h, start_m, end_h, end_m, start_s=0, end_s=0):
    start_total_seconds = start_h * 3600 + start_m * 60 + start_s
    end_total_seconds = end_h * 3600 + end_m * 60 + end_s

    if start_total_seconds >= end_total_seconds:
        day_end_seconds = 24 * 3600
        first_part_duration = day_end_seconds - start_total_seconds
        second_part_duration = end_total_seconds
        total_duration = first_part_duration + second_part_duration

        if total_duration <= 0:
            logger.warning(f"无效的时间范围: {start_h}:{start_m}:{start_s} - {end_h}:{end_m}:{end_s}. 总时长为0或负数。")
            return start_h, start_m, start_s

        random_point = random.randint(0, total_duration - 1)

        if random_point < first_part_duration:
            random_total_seconds = start_total_seconds + random_point
        else:
            random_total_seconds = random_point - first_part_duration
    else:
        random_total_seconds = random.randint(start_total_seconds, end_total_seconds)

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
    session_name_from_config = user_config.get('session_name')

    if not session_name_from_config:
        logger.error(f"计划任务: 用户 {user_nickname} (TGID: {user_telegram_id}) 缺少 session_name。")
        return
    
    session_name = os.path.basename(session_name_from_config)

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
            eff_strat_id = task_config.get('strategy_identifier') or \
                           (target_config_item.get('strategy') if target_config_item and 'strategy' in target_config_item else None) or \
                           (target_config_item.get('strategy_identifier') if target_config_item and 'strategy_identifier' in target_config_item else "未知")

            result = await execute_action(
                session_name=session_name,
                target_entity_identifier=target_identifier,
                strategy_id=eff_strat_id,
                task_config=task_config
            )

    eff_strat_id = task_config.get('strategy_identifier') or \
                   (target_config_item.get('strategy') if target_config_item and 'strategy' in target_config_item else "未知") if target_config_item else "未知"
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

def reconcile_tasks(force_reschedule_ids: list = None):
    logger.info("开始核对任务...")
    config = load_config()

    if not config.get('scheduler_enabled'):
        logger.info("调度器已禁用，跳过任务核对。")
        return {}

    def _get_new_cron_trigger(task_entry, cfg):
        scheduler_time_slots = cfg.get('scheduler_time_slots', [])
        if not scheduler_time_slots:
            logger.error("无法生成 CronTrigger，因为未配置任何时间段。")
            return None

        selected_slot_id = task_entry.get('selected_time_slot_id')
        time_slot = None
        if selected_slot_id:
            time_slot = next((s for s in scheduler_time_slots if s.get('id') == selected_slot_id), None)

        if not time_slot:
            time_slot = random.choice(scheduler_time_slots)

        if not time_slot:
            job_identifier = task_entry.get('bot_username') or task_entry.get('target_chat_id')
            logger.error(f"任务 {task_entry.get('user_telegram_id')}_{job_identifier} 无法找到有效的时间段。")
            return None

        start_h, start_m = time_slot.get('start_hour', 8), time_slot.get('start_minute', 0)
        start_s = time_slot.get('start_second', 0)
        end_h, end_m = time_slot.get('end_hour', 22), time_slot.get('end_minute', 0)
        end_s = time_slot.get('end_second', 0)

        rand_h, rand_m, rand_s = get_random_time_in_range(start_h, start_m, end_h, end_m, start_s, end_s)
        return CronTrigger(hour=rand_h, minute=rand_m, second=rand_s)

    if force_reschedule_ids is not None:
        logger.info(f"强制重调度指定的任务: {force_reschedule_ids}")
        rescheduled = []
        failed = []
        not_found = []

        for task_id in force_reschedule_ids:
            full_job_id = f"checkin_job_{task_id}"
            job = scheduler.get_job(full_job_id)
            if job:
                try:
                    parts = full_job_id.split('_')
                    user_id = int(parts[2])
                    identifier_str = "_".join(parts[3:])

                    fresh_task_entry = None
                    for task in config.get('checkin_tasks', []):
                        if task.get('user_telegram_id') != user_id:
                            continue
                        
                        current_identifier = str(task.get('bot_username') or task.get('target_chat_id'))
                        if current_identifier == identifier_str:
                            fresh_task_entry = task
                            break
                    
                    if not fresh_task_entry:
                        logger.warning(f"无法在当前配置中找到任务 {full_job_id} 的条目，跳过。")
                        failed.append({"task_id": task_id, "error": "Task entry not found in current config"})
                        continue

                    new_trigger = _get_new_cron_trigger(fresh_task_entry, config)
                    if new_trigger:
                        target_type = 'bot' if fresh_task_entry.get('bot_username') else 'chat'
                        identifier = fresh_task_entry.get('bot_username') or fresh_task_entry.get('target_chat_id')
                        new_args = [user_id, target_type, identifier, fresh_task_entry]

                        scheduler.add_job(
                            run_checkin_task_sync,
                            trigger=new_trigger,
                            args=new_args,
                            id=full_job_id,
                            name=job.name,
                            replace_existing=True
                        )
                        rescheduled.append(task_id)
                        logger.info(f"已成功为任务 {full_job_id} 生成新的执行计划。")
                    else:
                        failed.append({"task_id": task_id, "error": "无法生成新的触发器"})
                        logger.error(f"为任务 {full_job_id} 生成新触发器失败。")
                except Exception as e:
                    logger.error(f"修改任务 {full_job_id} 时出错: {e}", exc_info=True)
                    failed.append({"task_id": task_id, "error": str(e)})
            else:
                not_found.append(task_id)
                logger.warning(f"尝试重调度但未找到任务: {task_id} (构造的ID: {full_job_id})")
        
        result = {"rescheduled": rescheduled, "failed": failed, "not_found": not_found}
        logger.info(f"强制重调度完成: {result}")
        return result

    expected_job_ids = set()
    user_map_by_id = {user['telegram_id']: user for user in config.get('users', []) if 'telegram_id' in user}
    for task_entry in config.get('checkin_tasks', []):
        user_telegram_id = task_entry.get('user_telegram_id')
        user_config = user_map_by_id.get(user_telegram_id)
        if not user_config or user_config.get('status') != 'logged_in':
            continue

        identifier = task_entry.get('bot_username') or task_entry.get('target_chat_id')
        if identifier:
            job_id = f"checkin_job_{user_telegram_id}_{identifier}"
            expected_job_ids.add(job_id)

    existing_job_ids = {job.id for job in scheduler.get_jobs() if job.id and job.id.startswith("checkin_job_")}

    stale_job_ids = existing_job_ids - expected_job_ids
    for job_id in stale_job_ids:
        scheduler.remove_job(job_id)
        logger.info(f"已移除过时的任务: {job_id}")

    new_job_ids = expected_job_ids - existing_job_ids
    if not new_job_ids:
        logger.info("任务核对完成，没有需要新增的任务。")
        return {}
        
    logger.info(f"发现 {len(new_job_ids)} 个新任务，正在安排...")
    scheduler_time_slots = config.get('scheduler_time_slots', [])
    if not scheduler_time_slots:
        logger.error("无法安排新任务，因为未配置任何时间段。")
        return {}

    for task_entry in config.get('checkin_tasks', []):
        user_telegram_id = task_entry.get('user_telegram_id')
        
        target_type = None
        identifier = None
        if task_entry.get('bot_username'):
            target_type = 'bot'
            identifier = task_entry.get('bot_username')
        elif task_entry.get('target_chat_id'):
            target_type = 'chat'
            identifier = task_entry.get('target_chat_id')

        if not identifier: continue
        
        job_id = f"checkin_job_{user_telegram_id}_{identifier}"

        if job_id in new_job_ids:
            user_config_for_task = user_map_by_id.get(user_telegram_id)
            current_task_user_nickname = user_config_for_task.get('nickname', f"TGID_{user_telegram_id}")
            target_identifier = identifier
            
            chat_info = None
            if target_type == 'chat':
                 chat_info = next((c for c in config.get('chats', []) if c.get('chat_id') == target_identifier), None)
            display_target_name = chat_info.get('chat_title', str(target_identifier)) if chat_info else str(target_identifier)

            new_trigger = _get_new_cron_trigger(task_entry, config)

            if not new_trigger:
                logger.warning(f"无法为任务 {job_id} 生成执行计划，跳过。")
                continue
            
            try:
                scheduler.add_job(
                    run_checkin_task_sync,
                    trigger=new_trigger,
                    args=[user_telegram_id, target_type, target_identifier, task_entry],
                    id=job_id,
                    name=f"Task: {current_task_user_nickname} -> {display_target_name}",
                    replace_existing=True
                )
            except Exception as e:
                logger.error(f"为新任务 {job_id} 添加调度时发生错误: {e}", exc_info=True)
    return {}

def daily_reschedule_tasks():
    logger.info("开始每日重调度...")
    for job in scheduler.get_jobs():
        if job.id and job.id.startswith("checkin_job_"):
            scheduler.remove_job(job.id)
    logger.info("已移除所有昨日的任务作业。")
    reconcile_tasks()
    logger.info("每日重调度完成。")
    log_scheduled_jobs()

def run_scheduler():
    """在后台线程中运行调度器"""
    logger.info("启动调度器...")
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

    logger.info("调度器已成功启动并运行。")
    try:
        while True:
            time.sleep(2)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("调度器已关闭。")

SCHEDULER_HOST = os.environ.get("SCHEDULER_HOST", "localhost")
SCHEDULER_PORT = os.environ.get("SCHEDULER_PORT", "5057")
SCHEDULER_URL = f"http://{SCHEDULER_HOST}:{SCHEDULER_PORT}"

def _get_scheduler_url(endpoint):
    return f"{SCHEDULER_URL}{endpoint}"

def _send_reconcile_request(task_ids: list = None):
    reconcile_url = _get_scheduler_url("/reconcile")
    json_payload = {"task_ids": task_ids} if task_ids else {}
    
    try:
        with httpx.Client() as client:
            response = client.post(reconcile_url, json=json_payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"后台任务：成功通知调度器。Payload: {json_payload}")
        else:
            logger.error(f"后台任务：通知调度器失败，状态码: {response.status_code}, 响应: {response.text}")
    except httpx.ConnectError:
        logger.info(f"后台任务：无法连接到调度器服务(URL: {reconcile_url})，可能服务未运行。")
    except httpx.RequestError as e:
        logger.error(f"后台任务：请求调度器服务时发生未知网络错误: {e}")

def notify_scheduler_to_reconcile(task_ids: list = None):
    logger.info(f"正在创建后台线程以通知调度器... Task IDs: {task_ids}")
    thread = threading.Thread(target=_send_reconcile_request, args=(task_ids,), daemon=True)
    thread.start()
