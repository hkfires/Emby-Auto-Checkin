import asyncio
import logging
import os
from telethon import TelegramClient, errors

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')

import sys
sys.path.append(PROJECT_ROOT)
from config import load_config

logger = logging.getLogger(__name__)

class ClientManager:
    def __init__(self):
        self._clients = {}
        self._temp_login_clients = {}
        self.config = load_config()

    def create_temp_login_client(self, phone_number: str):
        if phone_number in self._temp_login_clients:
            return self._temp_login_clients[phone_number]

        api_id = self.config.get('api_id')
        api_hash = self.config.get('api_hash')
        
        original_cwd = os.getcwd()
        try:
            os.chdir(DATA_DIR)
            temp_session_name = f"temp_login_{phone_number}_{os.urandom(4).hex()}"
            client = TelegramClient(temp_session_name, api_id, api_hash)
            self._temp_login_clients[phone_number] = client
            return client
        finally:
            os.chdir(original_cwd)

    def get_temp_login_client(self, phone_number: str):
        return self._temp_login_clients.get(phone_number)

    async def remove_temp_login_client(self, phone_number: str):
        client = self._temp_login_clients.pop(phone_number, None)
        if client:
            if client.is_connected():
                await client.disconnect()
            
            session_file = f"{client.session.filename}"
            if os.path.exists(session_file):
                try:
                    os.remove(session_file)
                    logger.info(f"已删除临时会话文件: {session_file}")
                except OSError as e:
                    logger.error(f"删除临时会话文件 {session_file} 时出错: {e}")

    async def initialize_clients(self):
        logger.info("正在初始化所有Telegram客户端...")
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

    async def add_or_update_client(self, session_name, api_id, api_hash, nickname):
        if session_name in self._clients and self._clients[session_name]['client'].is_connected():
            logger.info(f"用户 {nickname} (会话: {session_name}) 的客户端已存在且已连接，无需重复创建。")
            return

        logger.info(f"用户 {nickname}: 正在为会话 {session_name} 创建新的客户端实例。")
        original_cwd = os.getcwd()
        try:
            os.chdir(DATA_DIR)
            client = TelegramClient(session_name, api_id, api_hash)
        finally:
            os.chdir(original_cwd)

        try:
            await client.connect()
            if await client.is_user_authorized():
                self._clients[session_name] = {"client": client, "nickname": nickname, "status": "connected"}
                logger.info(f"用户 {nickname} (会话: {session_name}): 客户端已成功连接并授权。")
            else:
                await client.disconnect()
                self._clients[session_name] = {"client": None, "nickname": nickname, "status": "auth_failed"}
                logger.warning(f"用户 {nickname} (会话: {session_name}): 客户端连接后未授权，请刷新登录。")
        except Exception as e:
            self._clients[session_name] = {"client": None, "nickname": nickname, "status": "connect_failed"}
            logger.error(f"用户 {nickname} (会话: {session_name}): 连接客户端时发生错误: {e}", exc_info=True)

    async def disconnect_all(self):
        logger.info("正在断开所有客户端连接...")
        for session_name, data in self._clients.items():
            client = data.get("client")
            if client and client.is_connected():
                try:
                    await client.disconnect()
                    logger.info(f"会话 {session_name} 已成功断开。")
                except Exception as e:
                    logger.error(f"断开会话 {session_name} 时发生错误: {e}")
        self._clients.clear()
        logger.info("所有客户端连接已断开。")

    async def remove_client(self, session_name):
        logger.info(f"正在移除会话 {session_name}...")
        if session_name in self._clients:
            data = self._clients.pop(session_name)
            client = data.get("client")
            if client:
                if client.is_connected():
                    try:
                        await client.disconnect()
                        logger.info(f"会话 {session_name} 已成功断开。")
                    except Exception as e:
                        logger.error(f"断开会话 {session_name} 时发生错误: {e}")
                
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
        if client_data and client_data.get("status") == "connected":
            return client_data["client"]
        return None

    def get_all_clients_status(self):
        return {name: {"nickname": data["nickname"], "status": data["status"]} for name, data in self._clients.items()}

    def get_active_sessions_count(self):
        return sum(1 for data in self._clients.values() if data.get("status") == "connected")

    async def health_check_all_clients(self):
        logger.info("开始执行客户端健康检查...")
        api_id = self.config.get('api_id')
        api_hash = self.config.get('api_hash')

        if not api_id or not api_hash:
            logger.warning("API ID 或 API Hash 未配置，无法执行健康检查。")
            return

        for session_name, data in list(self._clients.items()):
            client = data.get("client")
            is_connected = False
            try:
                if client and client.is_connected():
                    await client.get_me()
                    is_connected = True
            except Exception as e:
                logger.warning(f"会话 {session_name} 的健康检查失败 (可能已断开): {e}")
                is_connected = False

            if not is_connected:
                logger.warning(f"会话 {session_name} 未连接，尝试重新连接...")
                self._clients[session_name]["status"] = "reconnecting"
                await self.add_or_update_client(session_name, api_id, api_hash, data["nickname"])
        logger.info("客户端健康检查完成。")
