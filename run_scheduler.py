import logging
import threading
from datetime import datetime
from flask import Flask, jsonify, request
from utils.config import load_config
from utils.log import (
    BEIJING_TZ,
    beijing_today_str,
    get_planned_times_for_date,
    set_task_planned_time,
)
from utils.scheduler_api import (
    _get_checkin_jobs,
    _job_identity,
    reconcile_tasks,
    run_scheduler,
    log_scheduled_jobs,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/reconcile', methods=['POST', 'GET'])
def trigger_reconciliation():
    logger.info("收到 API 请求，开始执行任务核对...")
    task_ids = None
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "无效的 JSON 请求。"}), 400
        task_ids = data.get('task_ids')
        if task_ids is not None and (
            not isinstance(task_ids, list) or not task_ids
            or any(not isinstance(task_id, str) or not task_id.strip() for task_id in task_ids)
        ):
            return jsonify({"success": False, "message": "无效的 task_ids 参数。"}), 400

    try:
        result = reconcile_tasks(force_reschedule_ids=task_ids)
        log_scheduled_jobs()
        success = not result.get("failed") and not result.get("not_found")
        message = "任务重新调度成功。"
        if task_ids is not None:
            scheduled_count = len(result.get("rescheduled", []))
            failed_count = len(result.get("failed", [])) + len(result.get("not_found", []))
            message = f"重新调度完成：成功 {scheduled_count} 个，失败 {failed_count} 个。"
            errors = list(dict.fromkeys(item["error"] for item in result.get("failed", [])))
            if result.get("not_found"):
                errors.append("部分任务在当前配置中不存在")
            if errors:
                message += "原因：" + "；".join(errors) + "。"
        return jsonify({"success": success, "message": message, "result": result}), 200
    except Exception as e:
        logger.error(f"执行任务核对时发生错误: {e}")
        return jsonify({"success": False, "message": f"内部错误: {e}"}), 500


@app.route('/tasks/schedules', methods=['GET'])
def task_schedules():
    target_date = request.args.get('date') or beijing_today_str()
    try:
        datetime.strptime(target_date, '%Y-%m-%d')
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "日期格式必须为 YYYY-MM-DD。"}), 400

    config = load_config()
    if not config.get('scheduler_enabled'):
        return jsonify({
            "success": True,
            "date": target_date,
            "timezone": "Asia/Shanghai",
            "tasks": [],
            "message": "调度器未启用",
        })

    jobs = _get_checkin_jobs()
    identities = [identity for identity in (_job_identity(job) for job in jobs) if identity]
    planned_times = get_planned_times_for_date(target_date, identities)
    tasks = []
    for job in jobs:
        identity = _job_identity(job)
        if not identity:
            continue
        planned_at = planned_times.get(identity)
        next_run_time = job.next_run_time
        if planned_at is None and next_run_time:
            next_run_beijing = next_run_time.astimezone(BEIJING_TZ)
            if next_run_beijing.date().isoformat() == target_date:
                planned_at = next_run_beijing.isoformat()
                set_task_planned_time(identity, next_run_beijing)
        if planned_at is None:
            continue
        tasks.append({
            "user_telegram_id": identity[0],
            "target_type": identity[1],
            "target_identifier": identity[2],
            "planned_at": planned_at,
        })

    return jsonify({
        "success": True,
        "date": target_date,
        "timezone": "Asia/Shanghai",
        "tasks": tasks,
    })

def start_scheduler_thread():
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("调度器线程已启动。")

start_scheduler_thread()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5057)
