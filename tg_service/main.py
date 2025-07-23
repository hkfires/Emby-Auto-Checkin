import logging
import asyncio
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .client_manager import ClientManager
from .checkin_strategies import get_strategy_class
from telethon import errors

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Telegram Service",
    description="一个用于管理Telegram客户端会话并执行操作的独立服务。",
    version="1.0.0"
)

client_manager = ClientManager()

class ActionRequest(BaseModel):
    session_name: str
    target_entity_identifier: str
    strategy_id: str
    task_config: dict = {}

class SessionManageRequest(BaseModel):
    action: str
    session_name: str
    nickname: str

class HealthCheckResponse(BaseModel):
    status: str = "ok"
    active_sessions: int

@app.get("/", tags=["通用"])
async def root():
    return {"message": "欢迎使用 Telegram 服务!"}

@app.get("/health", response_model=HealthCheckResponse, tags=["健康检查"])
async def health_check():
    active_sessions = client_manager.get_active_sessions_count()
    return HealthCheckResponse(status="ok", active_sessions=active_sessions)

@app.post("/actions/execute", tags=["核心操作"])
async def execute_action(request: ActionRequest):
    client = client_manager.get_client(request.session_name)
    if not client:
        logger.error(f"动作请求失败: 未找到或未连接会话 {request.session_name}")
        raise HTTPException(status_code=404, detail=f"Session '{request.session_name}' not found or not connected.")

    StrategyClass = get_strategy_class(request.strategy_id)
    if not StrategyClass:
        logger.error(f"动作请求失败: 未知的策略ID {request.strategy_id}")
        raise HTTPException(status_code=400, detail=f"Unknown strategy ID: {request.strategy_id}")

    try:
        target_entity = await client.get_entity(request.target_entity_identifier)
        
        client_data = client_manager._clients.get(request.session_name, {})
        nickname_for_logging = client_data.get("nickname", "未知用户")

        strategy_instance = StrategyClass(client, target_entity, logger, nickname_for_logging, task_config=request.task_config)
        
        if hasattr(strategy_instance, 'execute') and callable(getattr(strategy_instance, 'execute')):
            result = await strategy_instance.execute()
            logger.info(f"动作执行成功: {request.dict()}, 结果: {result}")
            return result
        else:
            logger.error(f"策略 {request.strategy_id} 没有 'execute' 方法。")
            raise HTTPException(status_code=500, detail=f"Strategy '{request.strategy_id}' is not executable.")

    except errors.UserDeactivatedBanError as e:
        logger.error(f"会话 {request.session_name} 未授权或账户问题: {e}")
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as ve:
        logger.error(f"无法找到实体 {request.target_entity_identifier}: {ve}")
        raise HTTPException(status_code=404, detail=f"Could not find entity: {request.target_entity_identifier}")
    except Exception as e:
        logger.error(f"执行动作时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {type(e).__name__}")

@app.post("/sessions/manage", tags=["会话管理"])
async def manage_session(request: SessionManageRequest):
    api_id = client_manager.config.get('api_id')
    api_hash = client_manager.config.get('api_hash')

    if not api_id or not api_hash:
        raise HTTPException(status_code=500, detail="API ID or API Hash is not configured in the service.")

    if request.action == "add":
        await client_manager.add_or_update_client(request.session_name, api_id, api_hash, request.nickname)
        return {"success": True, "message": f"Session '{request.session_name}' is being added."}
    elif request.action == "remove":
        removed = await client_manager.remove_client(request.session_name)
        if removed:
            return {"success": True, "message": f"Session '{request.session_name}' has been removed."}
        else:
            return {"success": False, "message": f"Session '{request.session_name}' not found."}
    else:
        raise HTTPException(status_code=400, detail="Invalid action. Must be 'add' or 'remove'.")


async def periodic_health_check():
    while True:
        await asyncio.sleep(300)
        await client_manager.health_check_all_clients()

@app.on_event("startup")
async def startup_event():
    logger.info("Telegram 服务正在启动...")
    os.makedirs('data', exist_ok=True)
    await client_manager.initialize_clients()
    asyncio.create_task(periodic_health_check())
    logger.info("Telegram 服务已成功启动，并已启动后台健康检查任务。")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Telegram 服务正在关闭...")
    await client_manager.disconnect_all()
    logger.info("Telegram 服务已成功关闭。")
