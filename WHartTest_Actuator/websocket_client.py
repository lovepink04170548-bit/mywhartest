"""
UI自动化执行器 - WebSocket客户端

"""

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Optional, Callable, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import websockets
from websockets.client import WebSocketClientProtocol

from models import SocketDataModel, QueueModel, ResponseCode, NoticeType, UiSocketEnum

logger = logging.getLogger('actuator')


def build_websocket_connect_url(
    url: str,
    actuator_id: str | None = None,
    registration_token: str | None = None,
) -> str:
    """构建带执行器 ID 的 WebSocket 连接地址。"""
    parsed_url = urlsplit(url)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
        if key not in {'id', 'user_id', 'token', 'registration_token'}
    ]
    if actuator_id:
        query_items.append(('id', actuator_id))
    if registration_token:
        query_items.append(('token', registration_token))
    return urlunsplit(parsed_url._replace(query=urlencode(query_items)))


def build_websocket_origin(url: str) -> str | None:
    """根据 WebSocket 地址推导握手所需的 HTTP Origin。"""
    parsed_url = urlsplit(url)
    if not parsed_url.scheme or not parsed_url.netloc:
        return None

    origin_scheme = 'https' if parsed_url.scheme == 'wss' else 'http'
    return urlunsplit((origin_scheme, parsed_url.netloc, '', '', ''))


class WebSocketClient:
    """WebSocket客户端 - 连接Django后端"""
    
    VERSION = '1.0.0'
    
    def __init__(self, url: str, actuator_id: str = 'default', config: Any = None):
        self.url = url
        self.actuator_id = actuator_id
        self.config = config
        self.registration_token = str(getattr(config, 'registration_token', '') or '').strip() if config else ''
        self.websocket: Optional[WebSocketClientProtocol] = None
        self.connected = False
        self.reconnect_interval = 5  # 重连间隔(秒)
        self.max_reconnect_attempts = 0  # 0表示无限重试
        self.send_timeout = float(getattr(config, 'websocket_send_timeout', 30.0) if config else 30.0)
        self.heartbeat_interval = float(getattr(config, 'heartbeat_interval', 30.0) if config else 30.0)
        self._message_handler: Optional[Callable] = None
        self._stop_event = asyncio.Event()
        self._last_heartbeat_at = 0.0
        self._connected_at: str | None = None
    
    def set_message_handler(self, handler: Callable[[SocketDataModel], Any]):
        """设置消息处理器"""
        self._message_handler = handler
    
    async def connect(self) -> bool:
        """建立WebSocket连接"""
        try:
            connect_url = build_websocket_connect_url(self.url, self.actuator_id, self.registration_token)
            origin = build_websocket_origin(self.url)
            connect_kwargs = {
                'ping_interval': 30,
                'ping_timeout': 10,
                'close_timeout': 10,
            }
            if origin:
                connect_kwargs['origin'] = origin

            self.websocket = await websockets.connect(
                connect_url,
                **connect_kwargs,
            )
            self.connected = True
            self._connected_at = dt.datetime.now(dt.timezone.utc).isoformat()
            self._last_heartbeat_at = time.monotonic()
            logger.info(f"已连接到服务器: {connect_url}")
            
            # 连接成功后发送执行器信息
            await self._send_actuator_info()
            return True
        except Exception as e:
            logger.error(f"连接失败: {e}")
            self.connected = False
            return False
    
    def _build_actuator_info_payload(self) -> dict[str, Any]:
        """构建执行器注册/心跳 payload。"""
        actuator_info = {
            'name': getattr(self.config, 'actuator_name', None) or self.actuator_id,
            'type': 'web_ui',
            'is_open': True,
            'debug': False,
            'version': self.VERSION,
            'actuator_id': self.actuator_id,
        }
        
        # 从配置中获取浏览器相关设置
        if self.config:
            actuator_info['browser_type'] = getattr(self.config, 'browser_type', 'chromium')
            actuator_info['headless'] = getattr(self.config, 'headless', False)
        if self._connected_at:
            actuator_info['connected_at'] = self._connected_at
        actuator_info['sent_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        return actuator_info

    async def _send_actuator_info(self):
        """发送执行器信息到服务端"""
        actuator_info = self._build_actuator_info_payload()
        
        await self.send(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg='设置执行器信息',
            data=QueueModel(
                func_name=UiSocketEnum.SET_ACTUATOR_INFO,
                func_args=actuator_info
            )
        ))
        logger.info(f"已发送执行器信息: {actuator_info}")

    async def _send_heartbeat(self):
        """发送执行器心跳到服务端。"""
        heartbeat = self._build_actuator_info_payload()
        heartbeat['heartbeat'] = True
        await self.send(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg='执行器心跳',
            data=QueueModel(
                func_name=UiSocketEnum.ACTUATOR_HEARTBEAT,
                func_args=heartbeat,
            )
        ))
        self._last_heartbeat_at = time.monotonic()
        logger.debug("已发送执行器心跳")
    
    async def disconnect(self):
        """断开连接"""
        self._stop_event.set()
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        self.connected = False
        logger.info("已断开连接")
    
    async def send(self, data: SocketDataModel):
        """发送消息"""
        if not self.websocket or not self.connected:
            logger.error("未连接，无法发送消息")
            return False
        
        try:
            await asyncio.wait_for(
                self.websocket.send(data.model_dump_json()),
                timeout=max(1.0, self.send_timeout),
            )
            return True
        except asyncio.TimeoutError:
            logger.error(f"发送消息超时: timeout={self.send_timeout}s")
            self.connected = False
            return False
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            self.connected = False
            return False
    
    async def send_result(self, func_name: str, args: dict, user: Optional[str] = None):
        """发送执行结果"""
        return await self.send(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg='result',
            user=user,
            is_notice=NoticeType.WEB,
            data=QueueModel(func_name=func_name, func_args=args)
        ))
    
    async def receive_loop(self):
        """接收消息循环"""
        while not self._stop_event.is_set():
            if not self.connected or not self.websocket:
                # 尝试重连
                await self._reconnect()
                continue
            
            try:
                if (
                    self.connected
                    and self.websocket
                    and self.heartbeat_interval > 0
                    and time.monotonic() - self._last_heartbeat_at >= self.heartbeat_interval
                ):
                    await self._send_heartbeat()
                message = await asyncio.wait_for(
                    self.websocket.recv(),
                    timeout=1.0
                )
                await self._handle_message(message)
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                logger.warning("连接已关闭，准备重连")
                self.connected = False
            except Exception as e:
                logger.error(f"接收消息错误: {e}")
                self.connected = False
    
    async def _reconnect(self):
        """重连逻辑"""
        attempts = 0
        while not self._stop_event.is_set():
            attempts += 1
            logger.info(f"尝试重连 ({attempts})...")
            
            if await self.connect():
                return
            
            if self.max_reconnect_attempts > 0 and attempts >= self.max_reconnect_attempts:
                logger.error("达到最大重连次数，停止重连")
                self._stop_event.set()
                return
            
            await asyncio.sleep(self.reconnect_interval)
    
    async def _handle_message(self, message: str):
        """处理接收到的消息"""
        try:
            data = json.loads(message)
            socket_data = SocketDataModel(**data)
            
            if self._message_handler:
                await self._message_handler(socket_data)
            else:
                logger.warning(f"收到消息但没有处理器: {socket_data.msg}")
        except json.JSONDecodeError as e:
            logger.error(f"消息JSON解析错误: {e}")
        except Exception as e:
            logger.error(f"处理消息错误: {e}")
    
    async def run(self):
        """运行客户端"""
        if not await self.connect():
            logger.error("初始连接失败")
        
        await self.receive_loop()
