import logging
import os
import asyncio
import inspect
from telethon import TelegramClient

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')

import sys
sys.path.append(PROJECT_ROOT)
from utils.config import load_config

logger = logging.getLogger(__name__)

class ClientManager:
    def __init__(self):
        self._clients = {}
        self._temp_login_clients = {}
        self._session_locks = {}
        self.config = load_config()

    def _get_session_lock(self, session_name: str) -> asyncio.Lock:
        if session_name not in self._session_locks:
            self._session_locks[session_name] = asyncio.Lock()
        return self._session_locks[session_name]

    def _create_telegram_client(self, session_path: str, api_id: int, api_hash: str) -> TelegramClient:
        return TelegramClient(
            session_path,
            api_id,
            api_hash,
            timeout=15,
            connection_retries=10,
            retry_delay=2,
            auto_reconnect=True
        )

    async def _safe_disconnect(self, client):
        if not client:
            return
        try:
            if hasattr(client, 'disconnect'):
                dis = client.disconnect()
                if inspect.isawaitable(dis):
                    await dis
        except Exception as e:
            logger.debug(f"安全断开客户端时发生非关键异常: {e}")

    def create_temp_login_client(self, phone_number: str):
        if phone_number in self._temp_login_clients:
            return self._temp_login_clients[phone_number]

        api_id = self.config.get('api_id')
        api_hash = self.config.get('api_hash')
        
        temp_session_name = f"temp_login_{phone_number}_{os.urandom(4).hex()}"
        session_path = os.path.join(DATA_DIR, temp_session_name)
        client = self._create_telegram_client(session_path, api_id, api_hash)
        self._temp_login_clients[phone_number] = client
        return client

    def get_temp_login_client(self, phone_number: str):
        return self._temp_login_clients.get(phone_number)

    async def remove_temp_login_client(self, phone_number: str):
        client = self._temp_login_clients.pop(phone_number, None)
        if client:
            await self._safe_disconnect(client)
            
            session_file = getattr(getattr(client, 'session', None), 'filename', '')
            if session_file:
                if not session_file.endswith('.session'):
                    session_file_path = f"{session_file}.session"
                else:
                    session_file_path = session_file

                if os.path.exists(session_file_path):
                    try:
                        os.remove(session_file_path)
                        logger.info(f"已删除临时会话文件: {session_file_path}")
                    except OSError as e:
                        logger.error(f"删除临时会话文件 {session_file_path} 时出错: {e}")

    async def initialize_clients(self):
        logger.info("正在初始化所有Telegram客户端...")
        try:
            self.config = load_config()
        except Exception as e:
            logger.warning(f"加载配置失败，使用当前内存配置: {e}")

        api_id = self.config.get('api_id')
        api_hash = self.config.get('api_hash')

        if not api_id or not api_hash:
            logger.warning("API ID 或 API Hash 未配置，无法初始化客户端。")
            return

        for user in self.config.get('users', []):
            if user.get('status') == 'logged_in' and user.get('session_name'):
                session_name = user['session_name']
                nickname = user.get('nickname', '未知用户')
                await self.add_or_update_client(session_name, api_id, api_hash, nickname)

    async def add_or_update_client(self, session_name, api_id, api_hash, nickname, force_reconnect=False):
        async with self._get_session_lock(session_name):
            existing_data = self._clients.get(session_name)
            existing_client = existing_data.get('client') if existing_data else None

            # 1. 如果已有客户端且处于已连接状态（且未要求强制重连），无需重复创建
            if not force_reconnect and existing_client and getattr(existing_client, 'is_connected', lambda: False)():
                logger.info(f"用户 {nickname} (会话: {session_name}) 的客户端已存在且已连接，无需重复创建。")
                return

            # 如果是强制重连模式且已有客户端，先彻底释放旧的潜在僵死实例
            if force_reconnect and existing_client:
                logger.info(f"用户 {nickname} (会话: {session_name}): 强制重连模式，正在断开旧实例...")
                await self._safe_disconnect(existing_client)
                existing_client = None
                self._clients[session_name] = {"client": None, "nickname": nickname, "status": "reconnecting"}

            # 2. 如果已有客户端实例，优先在现有实例上尝试重连，避免重建造成的文件锁与任务泄露
            if existing_client:
                logger.info(f"用户 {nickname} (会话: {session_name}): 发现已有客户端实例，尝试直接重连...")
                try:
                    await existing_client.connect()
                    if await existing_client.is_user_authorized():
                        # 通过 get_me 验证网络探针真实可达
                        await asyncio.wait_for(existing_client.get_me(), timeout=10.0)
                        self._clients[session_name] = {"client": existing_client, "nickname": nickname, "status": "connected"}
                        logger.info(f"用户 {nickname} (会话: {session_name}): 已有客户端实例重连并授权成功。")
                        return
                    else:
                        await self._safe_disconnect(existing_client)
                        self._clients[session_name] = {"client": None, "nickname": nickname, "status": "auth_failed"}
                        logger.warning(f"用户 {nickname} (会话: {session_name}): 客户端连接后未授权，请刷新登录。")
                        return
                except Exception as e:
                    logger.warning(f"用户 {nickname} (会话: {session_name}): 已有客户端实例重连失败 ({e})，正在彻底释放旧资源并重建实例...")
                    await self._safe_disconnect(existing_client)
                    self._clients[session_name] = {"client": None, "nickname": nickname, "status": "reconnecting"}

            # 3. 创建全新客户端实例
            logger.info(f"用户 {nickname}: 正在为会话 {session_name} 创建新的客户端实例。")
            session_path = os.path.join(DATA_DIR, session_name)
            client = self._create_telegram_client(session_path, api_id, api_hash)

            try:
                await client.connect()
                if await client.is_user_authorized():
                    # 验证网络探针真实可达
                    await asyncio.wait_for(client.get_me(), timeout=10.0)
                    self._clients[session_name] = {"client": client, "nickname": nickname, "status": "connected"}
                    logger.info(f"用户 {nickname} (会话: {session_name}): 客户端已成功连接并授权。")
                else:
                    await self._safe_disconnect(client)
                    self._clients[session_name] = {"client": None, "nickname": nickname, "status": "auth_failed"}
                    logger.warning(f"用户 {nickname} (会话: {session_name}): 客户端连接后未授权，请刷新登录。")
            except Exception as e:
                await self._safe_disconnect(client)
                self._clients[session_name] = {"client": None, "nickname": nickname, "status": "connect_failed"}
                logger.error(f"用户 {nickname} (会话: {session_name}): 连接客户端时发生错误: {e}")

    async def disconnect_all(self):
        logger.info("正在断开所有客户端连接...")
        for session_name, data in self._clients.items():
            client = data.get("client")
            if client:
                await self._safe_disconnect(client)
                logger.info(f"会话 {session_name} 已成功断开。")
        self._clients.clear()
        logger.info("所有客户端连接已断开。")

    async def remove_client(self, session_name):
        logger.info(f"正在移除会话 {session_name}...")
        async with self._get_session_lock(session_name):
            if session_name in self._clients:
                data = self._clients.pop(session_name)
                client = data.get("client")
                if client:
                    await self._safe_disconnect(client)
                    logger.info(f"会话 {session_name} 已成功断开。")
                    
                session_file_path = os.path.join(DATA_DIR, f"{session_name}.session")
                if os.path.exists(session_file_path):
                    try:
                        os.remove(session_file_path)
                        logger.info(f"会话文件 {session_file_path} 已成功删除。")
                    except OSError as e:
                        logger.error(f"删除会话文件 {session_file_path} 时出错: {e}")
                logger.info(f"会话 {session_name} 已从管理器中移除。")
                return True
            else:
                logger.warning(f"尝试移除一个不存在的会话: {session_name}")
                return False

    def get_client(self, session_name):
        client_data = self._clients.get(session_name)
        if not client_data:
            return None
        client = client_data.get("client")
        if client and getattr(client, 'is_connected', lambda: True)():
            return client
        return None

    async def get_or_reconnect_client(self, session_name):
        client = self.get_client(session_name)
        if client:
            return client

        client_data = self._clients.get(session_name)
        if not client_data:
            return None

        # 客户端未连接，尝试按需紧急重连
        logger.info(f"会话 {session_name} 当前未连接，在执行前尝试按需重连...")
        api_id = self.config.get('api_id')
        api_hash = self.config.get('api_hash')
        nickname = client_data.get("nickname", "未知用户")
        if api_id and api_hash:
            await self.add_or_update_client(session_name, api_id, api_hash, nickname)
            return self.get_client(session_name)
        return None

    def get_all_clients_status(self):
        return {name: {"nickname": data["nickname"], "status": data["status"]} for name, data in self._clients.items()}

    def get_active_sessions_count(self):
        return sum(1 for data in self._clients.values() if data.get("status") == "connected" and data.get("client"))

    async def health_check_all_clients(self):
        logger.info("开始执行客户端健康检查...")
        try:
            self.config = load_config()
        except Exception as e:
            logger.warning(f"重新加载配置失败，使用当前内存配置: {e}")

        api_id = self.config.get('api_id')
        api_hash = self.config.get('api_hash')

        if not api_id or not api_hash:
            logger.warning("API ID 或 API Hash 未配置，无法执行健康检查。")
            return

        # 检查已在配置中但未初始化的用户
        for user in self.config.get('users', []):
            if user.get('status') == 'logged_in' and user.get('session_name'):
                session_name = user['session_name']
                if session_name not in self._clients:
                    nickname = user.get('nickname', '未知用户')
                    logger.info(f"发现新配置的登录用户 {nickname} (会话: {session_name})，尝试初始化...")
                    try:
                        await self.add_or_update_client(session_name, api_id, api_hash, nickname)
                    except Exception as e:
                        logger.error(f"初始化用户 {nickname} 时发生异常: {e}")

        for session_name, data in list(self._clients.items()):
            try:
                client = data.get("client")
                nickname = data.get("nickname", "未知用户")
                is_connected = False
                if client and getattr(client, 'is_connected', lambda: False)():
                    try:
                        await asyncio.wait_for(client.get_me(), timeout=10.0)
                        is_connected = True
                        data["status"] = "connected"
                    except Exception as e:
                        logger.warning(f"会话 {session_name} 的健康检查失败 (可能已断开): {e}")
                        is_connected = False
                else:
                    logger.warning(f"会话 {session_name} 处于未连接状态 (status={data.get('status')})。")
                    is_connected = False

                if not is_connected:
                    logger.warning(f"会话 {session_name} 未连接，尝试重新连接...")
                    self._clients[session_name]["status"] = "reconnecting"
                    await self.add_or_update_client(session_name, api_id, api_hash, nickname, force_reconnect=True)
            except Exception as e:
                logger.error(f"处理会话 {session_name} 健康检查时发生异常: {e}", exc_info=True)

        logger.info("客户端健康检查完成。")
