import asyncio
import ipaddress
import json
import os
import re
import socket
import html
import logging
from typing import Optional, Union
import httpx
from utils.config import load_config
from utils.log import beijing_now

logger = logging.getLogger(__name__)

# Suppress HTTPX/HTTPCORE info logs to avoid leaking bot tokens embedded in request URLs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DEFAULT_API_BASE_URL = "https://api.telegram.org"
MAX_ERROR_MESSAGE_LENGTH = 1200
MAX_FIELD_LENGTH = 200

SOURCE_DISPLAY_NAMES = {
    "scheduled": "计划任务",
    "quick": "批量快速任务",
    "manual": "手动执行",
}

TARGET_TYPE_NAMES = {
    "bot": "机器人",
    "chat": "群组/频道",
}


def _truncate_text(text: str, max_length: int) -> str:
    if not text:
        return ""
    text_str = str(text)
    if len(text_str) > max_length:
        return text_str[:max_length] + "... (已截断)"
    return text_str


def build_failure_notification_text(
    user_nickname: str,
    user_telegram_id: Union[int, str],
    target_name: str,
    target_type: str,
    strategy_display: str,
    message: str,
    execution_source: str = "scheduled",
    timestamp: Optional[str] = None,
) -> str:
    """Format failure notification text into HTML for Telegram with safe bounds."""
    if not timestamp:
        timestamp = beijing_now().strftime("%Y-%m-%d %H:%M:%S")

    source_text = SOURCE_DISPLAY_NAMES.get(execution_source, execution_source)
    target_type_text = TARGET_TYPE_NAMES.get(target_type, target_type)

    safe_user = _truncate_text(str(user_nickname or f"TGID_{user_telegram_id}"), MAX_FIELD_LENGTH)
    safe_target = _truncate_text(str(target_name or ""), MAX_FIELD_LENGTH)
    safe_strategy = _truncate_text(str(strategy_display or "未知"), MAX_FIELD_LENGTH)
    safe_source = _truncate_text(str(source_text or ""), MAX_FIELD_LENGTH)
    safe_message = _truncate_text(str(message or "未知原因"), MAX_ERROR_MESSAGE_LENGTH)

    escaped_user = html.escape(safe_user)
    escaped_user_id = html.escape(str(user_telegram_id if user_telegram_id is not None else ""))
    escaped_target = html.escape(safe_target)
    escaped_target_type = html.escape(str(target_type_text or ""))
    escaped_strategy = html.escape(safe_strategy)
    escaped_source = html.escape(safe_source)
    escaped_message = html.escape(safe_message)
    escaped_time = html.escape(str(timestamp))

    text = (
        "⚠️ <b>Emby 签到失败通知</b>\n\n"
        f"• <b>执行账号</b>: {escaped_user} (ID: <code>{escaped_user_id}</code>)\n"
        f"• <b>签到目标</b>: {escaped_target} ({escaped_target_type})\n"
        f"• <b>签到策略</b>: {escaped_strategy}\n"
        f"• <b>执行方式</b>: {escaped_source}\n"
        f"• <b>失败原因</b>: {escaped_message}\n"
        f"• <b>发生时间</b>: {escaped_time}"
    )
    return text


class NotificationError(Exception):
    """Stable, credential-free failure exposed at notification boundaries."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _prepare_request(bot_token, chat_id, text, api_base_url, parse_mode):
    """Allow trusted HTTPS origins only and pin the connection to a public IP."""
    if not bot_token or not re.fullmatch(r"[A-Za-z0-9_:-]+", str(bot_token).strip()) or not str(chat_id or "").strip():
        raise NotificationError("configuration_error", "Bot Token 或 Chat ID 无效。")
    try:
        base = httpx.URL((api_base_url or DEFAULT_API_BASE_URL).strip())
    except httpx.InvalidURL as exc:
        raise NotificationError("destination_blocked", "通知 API 地址无效。") from exc
    trusted = {DEFAULT_API_BASE_URL}
    trusted.update(value.strip().rstrip("/") for value in os.environ.get("NOTIFICATION_TRUSTED_ORIGINS", "").split(",") if value.strip())
    if (base.scheme != "https" or not base.host or base.userinfo or base.query or base.fragment
            or base.path != "/" or str(base).rstrip("/") not in trusted):
        raise NotificationError("destination_blocked", "通知 API 必须是管理员允许的 HTTPS 基础地址。")
    try:
        addresses = socket.getaddrinfo(base.host, base.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NotificationError("network_error", "通知 API DNS 解析失败。") from exc
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or getattr(ip, "ipv4_mapped", None) is not None for ip in ips):
        raise NotificationError("destination_blocked", "通知 API 解析到禁止访问的网络地址。")
    # HTTPX connects to this numeric IP; Host and TLS SNI retain the trusted hostname.
    # Disable redirects and environment proxies in both clients to prevent bypasses.
    url = base.copy_with(host=str(ips[0]), path=f"/bot{str(bot_token).strip()}/sendMessage")
    return url, {"Host": base.netloc.decode("ascii")}, {"sni_hostname": base.host}, {
        "chat_id": str(chat_id).strip(), "text": text, "parse_mode": parse_mode,
    }


def _check_response(response):
    """Translate malformed upstream data explicitly, never return raw response bodies."""
    try:
        data = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotificationError("protocol_error", "通知 API 返回了无效 JSON。") from exc
    if not isinstance(data, dict) or type(data.get("ok")) is not bool:
        raise NotificationError("protocol_error", "通知 API 响应结构无效。")
    if not response.is_success or not data["ok"]:
        raise NotificationError("api_rejected", f"通知 API 拒绝发送（HTTP {response.status_code}）。")
    return True, "发送成功"


async def send_telegram_message(bot_token, chat_id, text, api_base_url=DEFAULT_API_BASE_URL, parse_mode="HTML"):
    url, headers, extensions, payload = await asyncio.to_thread(
        _prepare_request, bot_token, chat_id, text, api_base_url, parse_mode
    )
    # Transport failures gain stable context; callers must handle the raised error.
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False, follow_redirects=False) as client:
            response = await client.post(url, headers=headers, extensions=extensions, json=payload)
    except httpx.RequestError as exc:
        raise NotificationError("network_error", "发送通知时网络请求失败。") from exc
    return _check_response(response)


def send_telegram_message_sync(bot_token, chat_id, text, api_base_url=DEFAULT_API_BASE_URL, parse_mode="HTML"):
    url, headers, extensions, payload = _prepare_request(bot_token, chat_id, text, api_base_url, parse_mode)
    try:
        with httpx.Client(timeout=15.0, trust_env=False, follow_redirects=False) as client:
            response = client.post(url, headers=headers, extensions=extensions, json=payload)
    except httpx.RequestError as exc:
        raise NotificationError("network_error", "发送通知时网络请求失败。") from exc
    return _check_response(response)


async def notify_checkin_failure(
    user_nickname, user_telegram_id, target_name, target_type, strategy_display,
    message, execution_source="scheduled", config=None,
):
    if config is None:
        config = load_config()
    settings = config.get("notification_settings", {})
    if not settings.get("enabled"):
        return False
    text = build_failure_notification_text(
        user_nickname, user_telegram_id, target_name, target_type,
        strategy_display, message, execution_source,
    )
    await send_telegram_message(
        settings.get("bot_token"), settings.get("chat_id"), text,
        settings.get("api_base_url") or DEFAULT_API_BASE_URL,
    )
    return True
