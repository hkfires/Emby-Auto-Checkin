import logging
import asyncio
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Union
from .client_manager import ClientManager, DATA_DIR
from .checkin_strategies import get_strategy_class
from telethon import errors

import sys
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(PROJECT_ROOT)
from utils.config import migrate_session_names

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

migrate_session_names()

app = FastAPI(
    title="Telegram Service",
    version="1.0.0"
)

client_manager = ClientManager()

class ActionRequest(BaseModel):
    session_name: str
    target_entity_identifier: Union[int, str]
    strategy_id: str
    task_config: dict = {}

class SessionManageRequest(BaseModel):
    action: str
    session_name: str
    nickname: str

class HealthCheckResponse(BaseModel):
    status: str = "ok"
    active_sessions: int

class SendCodeRequest(BaseModel):
    phone: str

class SignInRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: str
    password: str = None

class ResolveEntityRequest(BaseModel):
    session_name: str
    entity_identifier: Union[int, str]

@app.post("/entities/resolve", tags=["实体解析"])
async def resolve_entity(request: ResolveEntityRequest):
    client = client_manager.get_client(request.session_name)
    if not client:
        raise HTTPException(status_code=404, detail=f"Session '{request.session_name}' not found or not connected.")
    
    try:
        entity = await client.get_entity(request.entity_identifier)
        return {
            "success": True,
            "id": entity.id,
            "name": getattr(entity, 'title', getattr(entity, 'username', str(entity.id)))
        }
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Could not find entity: {request.entity_identifier}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", tags=["通用"])
async def root():
    return {"message": "欢迎使用 Telegram 服务!"}

@app.get("/health", response_model=HealthCheckResponse, tags=["健康检查"])
async def health_check():
    active_sessions = client_manager.get_active_sessions_count()
    return HealthCheckResponse(status="ok", active_sessions=active_sessions)

@app.post("/login/send_code", tags=["登录管理"])
async def send_code(request: SendCodeRequest):
    api_id = client_manager.config.get('api_id')
    api_hash = client_manager.config.get('api_hash')

    if not api_id or not api_hash:
        raise HTTPException(status_code=500, detail="API ID or API Hash is not configured in the service.")

    temp_client = client_manager.create_temp_login_client(request.phone)
    
    try:
        if not temp_client.is_connected():
            await temp_client.connect()
        sent_code = await temp_client.send_code_request(request.phone)
        return {"success": True, "phone_code_hash": sent_code.phone_code_hash}
    except Exception as e:
        logger.error(f"发送验证码到 {request.phone} 时发生错误: {e}", exc_info=True)
        await client_manager.remove_temp_login_client(request.phone)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/login/signin", tags=["登录管理"])
async def sign_in(request: SignInRequest):
    api_id = client_manager.config.get('api_id')
    api_hash = client_manager.config.get('api_hash')

    if not api_id or not api_hash:
        raise HTTPException(status_code=500, detail="API ID or API Hash is not configured in the service.")

    temp_client = client_manager.get_temp_login_client(request.phone)
    if not temp_client:
        raise HTTPException(status_code=400, detail="No active login process found for this phone number. Please request a code first.")

    try:
        user = await temp_client.sign_in(
            phone=request.phone,
            code=request.code,
            phone_code_hash=request.phone_code_hash,
            password=request.password
        )
        
        if not user:
            raise HTTPException(status_code=401, detail="Failed to sign in, user object not returned.")
        
        session_name = f"session_{user.id}"
        temp_session_filename = temp_client.session.filename
        
        await temp_client.disconnect()
        logger.info(f"临时客户端 ({temp_session_filename}) 已断开连接，准备迁移会话。")

        temp_session_path = os.path.join(DATA_DIR, temp_session_filename)
        permanent_session_path = os.path.join(DATA_DIR, f"{session_name}.session")

        if os.path.exists(temp_session_path):
            try:
                with open(temp_session_path, 'rb') as f_temp:
                    session_data = f_temp.read()
                
                with open(permanent_session_path, 'wb') as f_perm:
                    f_perm.write(session_data)
                
                logger.info(f"会话文件内容已从 {temp_session_path} 成功复制到 {permanent_session_path}")
                
            except IOError as e:
                logger.error(f"复制会话文件时发生IO错误: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail="Failed to migrate session file.")
            finally:
                os.remove(temp_session_path)
        else:
            logger.warning(f"未找到预期的临时会话文件: {temp_session_path}")

        await client_manager.add_or_update_client(session_name, api_id, api_hash, user.first_name or user.username)

        return {
            "success": True,
            "status": "logged_in",
            "user_info": {
                "telegram_id": user.id,
                "nickname": user.first_name or user.username,
                "phone": request.phone,
                "session_name": session_name
            }
        }
    except errors.SessionPasswordNeededError:
        return {"success": False, "status": "2fa_needed", "message": "Two-factor authentication is required."}
    
    except errors.PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="Invalid code.")
        
    except Exception as e:
        logger.error(f"登录 {request.phone} 时发生错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await client_manager.remove_temp_login_client(request.phone)

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
