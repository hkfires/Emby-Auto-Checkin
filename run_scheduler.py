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
        data = request.get_json()
        if data:
            task_ids = data.get('task_ids')

    try:
        result = reconcile_tasks(force_reschedule_ids=task_ids)
        log_scheduled_jobs()
        return jsonify({"success": True, "message": "任务重新调度成功。", "result": result}), 200
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
