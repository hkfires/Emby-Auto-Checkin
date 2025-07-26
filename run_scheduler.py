import logging
import os
import threading
from flask import Flask, jsonify
from utils.scheduler_api import reconcile_tasks, run_scheduler, log_scheduled_jobs

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/reconcile', methods=['POST'])
def trigger_reconciliation():
    logger.info("收到 API 请求，开始执行任务核对...")
    try:
        reconcile_tasks()
        log_scheduled_jobs()
        logger.info("任务核对完成。")
        return jsonify({"success": True, "message": "任务核对成功完成。"}), 200
    except Exception as e:
        logger.error(f"执行任务核对时发生错误: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"内部错误: {e}"}), 500

def start_scheduler_thread():
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("调度器线程已启动。")

start_scheduler_thread()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5057)
