import httpx
import logging
import os

logger = logging.getLogger(__name__)

TG_SERVICE_HOST = os.environ.get("TG_SERVICE_HOST", "localhost")
TG_SERVICE_PORT = os.environ.get("TG_SERVICE_PORT", "5056")
TG_SERVICE_URL = f"http://{TG_SERVICE_HOST}:{TG_SERVICE_PORT}"

async def execute_action(session_name: str, target_entity_identifier: str, strategy_id: str, task_config: dict = None):
    if task_config is None:
        task_config = {}
    
    url = f"{TG_SERVICE_URL}/actions/execute"
    payload = {
        "session_name": session_name,
        "target_entity_identifier": target_entity_identifier,
        "strategy_id": strategy_id,
        "task_config": task_config
    }
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"调用 TG 服务执行动作失败 (HTTP {e.response.status_code}): {e.response.text}")
        return {"success": False, "message": f"服务内部错误: {e.response.text}"}
    except httpx.RequestError as e:
        logger.error(f"调用 TG 服务时发生网络请求错误: {e}")
        return {"success": False, "message": f"无法连接到TG服务: {e}"}
    except Exception as e:
        logger.error(f"调用 TG 服务时发生未知错误: {e}", exc_info=True)
        return {"success": False, "message": f"未知错误: {e}"}

async def manage_session(action: str, session_name: str, nickname: str):
    url = f"{TG_SERVICE_URL}/sessions/manage"
    payload = {
        "action": action,
        "session_name": session_name,
        "nickname": nickname
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"调用 TG 服务管理会话失败 (HTTP {e.response.status_code}): {e.response.text}")
        return {"success": False, "message": f"服务内部错误: {e.response.text}"}
    except httpx.RequestError as e:
        logger.error(f"调用 TG 服务时发生网络请求错误: {e}")
        return {"success": False, "message": f"无法连接到TG服务: {e}"}
    except Exception as e:
        logger.error(f"调用 TG 服务时发生未知错误: {e}", exc_info=True)
        return {"success": False, "message": f"未知错误: {e}"}
