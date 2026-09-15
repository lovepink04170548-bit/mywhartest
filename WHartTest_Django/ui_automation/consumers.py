"""
UI自动化 WebSocket Consumer


提供两个WebSocket端点：
- /ws/ui/web/ - 前端连接，用于接收执行结果和状态更新
- /ws/ui/actuator/ - 执行器连接，用于接收执行任务和返回结果
"""

import json
import logging
import datetime as dt
from typing import Optional
from urllib.parse import parse_qs
from django.conf import settings
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from django.utils import timezone

from .socket_models import (
    SocketDataModel, QueueModel, NoticeType, ResponseCode,
    UiSocketEnum, ExecutionTaskModel, StepResultModel, CaseResultModel
)
from wharttest_django.i18n import translate_app_text

logger = logging.getLogger('ui_automation')


def _element_map_field_values(element_map_payload: dict, coverage: dict, task, previous_map=None) -> dict:
    from .element_map_governance import annotate_map_json

    previous_json = previous_map.map_json if previous_map is not None and isinstance(previous_map.map_json, dict) else None
    annotated = annotate_map_json(
        element_map_payload,
        project_id=task.project_id,
        environment_config=task.environment_config,
        base_url=element_map_payload.get('base_url') or task.target_url or '',
        previous_map_json=previous_json,
    )
    governance = annotated.get('governance') if isinstance(annotated.get('governance'), dict) else {}
    scope = annotated.get('scope') if isinstance(annotated.get('scope'), dict) else {}
    return {
        'map_json': annotated,
        'coverage_summary': coverage,
        'low_confidence_items': annotated.get('low_confidence_items') or [],
        'risk_items': annotated.get('risk_items') or [],
        'version_group': governance.get('version_group') or scope.get('version_group') or '',
        'role_key': scope.get('role_key') or 'default',
        'permission_key': scope.get('permission_key') or 'default',
        'map_hash': governance.get('map_hash') or '',
        'baseline_hash': governance.get('baseline_hash') or '',
        'diff_summary': governance.get('diff_summary') or {},
        'stale_status': governance.get('stale_status') or 'current',
        'stale_reason': '',
    }


def _is_manual_capture_map_payload(value) -> bool:
    if not isinstance(value, dict):
        return False
    return value.get('capture_mode') == 'manual_capture' or value.get('source') == 'manual_capture'


def _is_manual_capture_map_result(args: dict, element_map_payload: dict, element_map) -> bool:
    if args.get('execution_mode') == 'manual_map' or args.get('element_map_source') == 'manual_capture':
        return True
    if _is_manual_capture_map_payload(element_map_payload):
        return True
    map_json = element_map.map_json if element_map is not None and isinstance(element_map.map_json, dict) else {}
    return _is_manual_capture_map_payload(map_json)


def save_ai_generation_result_sync(args: dict):
    """Persist Playwright MCP generation results from WebSocket or REST transport."""
    from .models import UiAiGenerationTask, UiElementMap
    from django.utils import timezone

    task_id = args.get('task_id')
    if not task_id:
        logger.warning("AI 生成任务结果缺少 task_id")
        return

    try:
        task = UiAiGenerationTask.objects.select_related('project', 'environment_config', 'creator').get(id=task_id)
    except UiAiGenerationTask.DoesNotExist:
        logger.warning(f"AI 生成任务不存在: task_id={task_id}")
        return

    status_str = args.get('status') or 'failed'
    existing_repair_history = task.repair_history if isinstance(task.repair_history, list) else []
    existing_preserved_history = [
        item for item in existing_repair_history
        if isinstance(item, dict) and item.get('type') in {'dispatch_ack', 'async_llm_business_planning'}
    ]
    incoming_repair_history = args.get('repair_history') or []
    if not isinstance(incoming_repair_history, list):
        incoming_repair_history = []
    incoming_history_keys = {
        (
            item.get('type'),
            item.get('dispatch_id') or item.get('status') or '',
            item.get('queued_at') or item.get('started_at') or item.get('completed_at') or '',
        )
        for item in incoming_repair_history
        if isinstance(item, dict)
    }
    merged_repair_history = [
        *incoming_repair_history,
        *[
            item for item in existing_preserved_history
            if (
                item.get('type'),
                item.get('dispatch_id') or item.get('status') or '',
                item.get('queued_at') or item.get('started_at') or item.get('completed_at') or '',
            ) not in incoming_history_keys
        ],
    ]
    existing_verification = task.verification_result if isinstance(task.verification_result, dict) else {}

    task.status = 'success' if status_str == 'success' else 'failed'
    task.dispatch_status = 'completed'
    task.completed_at = timezone.now()
    task.test_plan = args.get('test_plan') or {}
    task.mcp_observations = args.get('mcp_observations') or []
    task.element_map_snapshot = args.get('element_map') or {}
    generated_case = args.get('generated_case') or {}
    if isinstance(generated_case, dict) and args.get('intent_analysis'):
        generated_case['intent_analysis'] = args.get('intent_analysis')
    task.generated_case = generated_case
    task.generated_script = args.get('generated_script') or ''
    task.generated_script_hash = args.get('generated_script_hash') or ''
    incoming_verification = args.get('verification_result') or {}
    if not isinstance(incoming_verification, dict):
        incoming_verification = {}
    if (
        isinstance(existing_verification.get('async_llm_planning'), dict)
        and 'async_llm_planning' not in incoming_verification
    ):
        incoming_verification['async_llm_planning'] = existing_verification['async_llm_planning']
    if (
        isinstance(existing_verification.get('async_llm_intent'), dict)
        and 'async_llm_intent' not in incoming_verification
    ):
        incoming_verification['async_llm_intent'] = existing_verification['async_llm_intent']
    existing_script_artifacts = existing_verification.get('script_artifacts')
    if isinstance(existing_script_artifacts, dict):
        incoming_script_artifacts = incoming_verification.get('script_artifacts')
        if not isinstance(incoming_script_artifacts, dict):
            incoming_script_artifacts = {}
        for key, value in existing_script_artifacts.items():
            if str(key).startswith('async_') and key not in incoming_script_artifacts:
                incoming_script_artifacts[key] = value
        if incoming_script_artifacts:
            incoming_verification['script_artifacts'] = incoming_script_artifacts
    task.verification_result = incoming_verification
    task.repair_history = merged_repair_history
    task.failure_category = args.get('failure_category') or ''
    task.current_repair_round = int(args.get('current_repair_round') or len(task.repair_history or []))
    task.error_message = args.get('message') if task.status == 'failed' else ''

    element_map_payload = args.get('element_map') or {}
    pages = element_map_payload.get('pages') if isinstance(element_map_payload, dict) else None
    if pages:
        coverage = element_map_payload.get('coverage_summary') or {
            'page_count': len(pages),
            'element_count': sum(len(page.get('elements') or []) for page in pages if isinstance(page, dict)),
        }
        element_map = task.element_map
        if _is_manual_capture_map_result(args, element_map_payload, element_map):
            governance_note = incoming_verification.get('element_map_governance')
            if not isinstance(governance_note, dict):
                governance_note = {}
            incoming_verification['element_map_governance'] = {
                **governance_note,
                'status': 'skipped',
                'reason': 'manual_capture_map_is_immutable_for_ai_result',
                'element_map_id': task.element_map_id,
                'received_page_count': len(pages),
                'received_element_count': coverage.get('element_count'),
            }
            task.verification_result = incoming_verification
        else:
            field_values = _element_map_field_values(element_map_payload, coverage, task, element_map)
            if element_map is None:
                element_map = UiElementMap.objects.create(
                    project=task.project,
                    environment_config=task.environment_config,
                    name=f"{task.name}-元素地图",
                    base_url=element_map_payload.get('base_url') or task.target_url,
                    status='draft',
                    creator=task.creator,
                    **field_values,
                )
            elif element_map.map_hash and field_values['map_hash'] and element_map.map_hash == field_values['map_hash']:
                for field, value in field_values.items():
                    setattr(element_map, field, value)
                element_map.save(update_fields=[
                    'map_json', 'coverage_summary', 'low_confidence_items', 'risk_items',
                    'version_group', 'role_key', 'permission_key', 'map_hash',
                    'baseline_hash', 'diff_summary', 'stale_status', 'stale_reason', 'updated_at'
                ])
            elif element_map.status == 'confirmed':
                element_map.stale_status = 'superseded'
                element_map.stale_reason = 'AI 重新采集发现元素地图内容变化，已生成新的 draft 版本'
                element_map.save(update_fields=['stale_status', 'stale_reason', 'updated_at'])
                latest_version = UiElementMap.objects.filter(
                    project=task.project,
                    environment_config=task.environment_config,
                    version_group=field_values['version_group'],
                ).order_by('-version', '-id').first()
                next_version = (latest_version.version if latest_version else element_map.version) + 1
                element_map = UiElementMap.objects.create(
                    project=task.project,
                    environment_config=task.environment_config,
                    parent_map=task.element_map,
                    name=task.element_map.name,
                    version=next_version,
                    base_url=element_map_payload.get('base_url') or task.target_url,
                    status='draft',
                    creator=task.creator,
                    **field_values,
                )
            else:
                for field, value in field_values.items():
                    setattr(element_map, field, value)
                element_map.save(update_fields=[
                    'map_json', 'coverage_summary', 'low_confidence_items', 'risk_items',
                    'version_group', 'role_key', 'permission_key', 'map_hash',
                    'baseline_hash', 'diff_summary', 'stale_status', 'stale_reason', 'updated_at'
                ])
            task.element_map = element_map
            task.element_map_snapshot = element_map.map_json

    task.save(update_fields=[
        'status', 'completed_at', 'test_plan', 'mcp_observations',
        'element_map_snapshot', 'generated_case', 'generated_script',
        'generated_script_hash', 'verification_result', 'repair_history',
        'failure_category', 'current_repair_round', 'error_message',
        'element_map', 'dispatch_status', 'updated_at'
    ])
    logger.info(f"AI 生成任务结果已保存: task_id={task.id}, status={task.status}")


class SocketUserManager:
    """WebSocket用户管理器"""
    
    _web_users: dict[str, 'UiAutomationConsumer'] = {}      # 前端用户连接
    _actuator_users: dict[str, 'UiAutomationConsumer'] = {} # 执行器连接
    
    @classmethod
    def _online_ttl_seconds(cls) -> int:
        return int(getattr(settings, 'UI_ACTUATOR_ONLINE_TTL_SECONDS', 90))

    @classmethod
    def _parse_datetime(cls, value) -> Optional[dt.datetime]:
        if not value:
            return None
        try:
            text = str(value).strip().replace('Z', '+00:00')
            parsed = dt.datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed
        except Exception:
            return None

    @classmethod
    def _actuator_last_seen(cls, consumer: 'UiAutomationConsumer') -> Optional[dt.datetime]:
        info = getattr(consumer, 'actuator_info', {}) or {}
        return cls._parse_datetime(info.get('last_seen_at') or info.get('connected_at'))

    @classmethod
    def _actuator_status(cls, consumer: 'UiAutomationConsumer') -> str:
        info = getattr(consumer, 'actuator_info', {}) or {}
        if not info.get('authenticated', False):
            return 'unauthenticated'
        if not getattr(consumer, 'connected', True):
            return 'offline'
        last_seen = cls._actuator_last_seen(consumer)
        if not last_seen:
            return 'unknown'
        age = timezone.now() - last_seen
        if age.total_seconds() > cls._online_ttl_seconds():
            return 'expired'
        return 'online'

    @classmethod
    def _available_actuators(cls) -> list[tuple[str, 'UiAutomationConsumer']]:
        now = timezone.now()
        items: list[tuple[str, 'UiAutomationConsumer', dt.datetime, str]] = []
        for actuator_id, consumer in cls._actuator_users.items():
            status = cls._actuator_status(consumer)
            if status != 'online':
                continue
            last_seen = cls._actuator_last_seen(consumer) or now
            items.append((actuator_id, consumer, last_seen, status))
        items.sort(key=lambda item: item[2], reverse=True)
        return [(actuator_id, consumer) for actuator_id, consumer, _, _ in items]

    @classmethod
    def describe_actuator(cls, actuator_id: str, consumer: 'UiAutomationConsumer') -> dict:
        info = getattr(consumer, 'actuator_info', {}) or {}
        status = cls._actuator_status(consumer)
        return {
            'id': actuator_id,
            'name': info.get('name', actuator_id),
            'ip': info.get('ip', 'unknown'),
            'type': info.get('type', 'web_ui'),
            'is_open': info.get('is_open', True),
            'debug': info.get('debug', False),
            'browser_type': info.get('browser_type', 'chromium'),
            'headless': info.get('headless', False),
            'version': info.get('version', ''),
            'connected_at': info.get('connected_at'),
            'last_seen_at': info.get('last_seen_at'),
            'authenticated': bool(info.get('authenticated', False)),
            'status': status,
            'online_ttl_seconds': cls._online_ttl_seconds(),
        }

    @classmethod
    def add_web_user(cls, user_id: str, consumer: 'UiAutomationConsumer'):
        cls._web_users[user_id] = consumer
        logger.info(f"Web用户连接: {user_id}, 当前连接数: {len(cls._web_users)}")
    
    @classmethod
    def remove_web_user(cls, user_id: str):
        if user_id in cls._web_users:
            del cls._web_users[user_id]
            logger.info(f"Web用户断开: {user_id}, 当前连接数: {len(cls._web_users)}")
    
    @classmethod
    def add_actuator(cls, actuator_id: str, consumer: 'UiAutomationConsumer'):
        cls._actuator_users[actuator_id] = consumer
        logger.info(f"执行器连接: {actuator_id}, 当前执行器数: {len(cls._actuator_users)}")
    
    @classmethod
    def remove_actuator(cls, actuator_id: str):
        if actuator_id in cls._actuator_users:
            del cls._actuator_users[actuator_id]
            logger.info(f"执行器断开: {actuator_id}, 当前执行器数: {len(cls._actuator_users)}")
    
    @classmethod
    def get_actuator(cls, actuator_id: Optional[str] = None) -> Optional['UiAutomationConsumer']:
        """获取执行器，如果不指定则返回第一个可用的"""
        if actuator_id and actuator_id in cls._actuator_users:
            consumer = cls._actuator_users[actuator_id]
            return consumer if cls._actuator_status(consumer) == 'online' else None
        available = cls._available_actuators()
        if available:
            return available[0][1]
        return None
    
    @classmethod
    def get_actuator_by_id(cls, actuator_id: str) -> Optional['UiAutomationConsumer']:
        """根据ID获取指定执行器"""
        consumer = cls._actuator_users.get(actuator_id)
        if consumer and cls._actuator_status(consumer) == 'online':
            return consumer
        return None
    
    @classmethod
    def get_web_user(cls, user_id: str) -> Optional['UiAutomationConsumer']:
        return cls._web_users.get(user_id)
    
    @classmethod
    def has_actuator(cls) -> bool:
        return bool(cls._available_actuators())
    
    @classmethod
    def get_actuator_count(cls) -> int:
        return len(cls._actuator_users)

    @classmethod
    def get_available_actuator_count(cls) -> int:
        return len(cls._available_actuators())
    
    @classmethod
    def get_all_actuators(cls) -> list['UiAutomationConsumer']:
        return list(cls._actuator_users.values())

    @classmethod
    def get_all_actuator_descriptions(cls) -> list[dict]:
        return [
            cls.describe_actuator(actuator_id, consumer)
            for actuator_id, consumer in cls._actuator_users.items()
        ]

    @classmethod
    def update_actuator_info(cls, actuator_id: str, consumer: 'UiAutomationConsumer', args: dict, *, authenticated: bool = True):
        now = timezone.now().isoformat()
        info = getattr(consumer, 'actuator_info', {}) or {}
        info.update({
            'id': actuator_id,
            'name': args.get('name') or info.get('name') or actuator_id,
            'type': args.get('type') or info.get('type') or 'web_ui',
            'is_open': args.get('is_open', info.get('is_open', True)),
            'debug': args.get('debug', info.get('debug', False)),
            'browser_type': args.get('browser_type') or info.get('browser_type') or 'chromium',
            'headless': args.get('headless', info.get('headless', False)),
            'version': args.get('version') or info.get('version') or '',
            'last_seen_at': now,
            'authenticated': authenticated or bool(info.get('authenticated', False)),
        })
        if not info.get('connected_at'):
            info['connected_at'] = now
        consumer.actuator_info = info
        return info
    
    @classmethod
    def get_actuator_info(cls, actuator_id: str) -> dict:
        """获取执行器详细信息"""
        if actuator_id in cls._actuator_users:
            consumer = cls._actuator_users[actuator_id]
            return getattr(consumer, 'actuator_info', {})
        return {}


class UiAutomationConsumer(AsyncWebsocketConsumer):
    """UI自动化WebSocket消费者"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_id: Optional[str] = None
        self.is_actuator: bool = False
        self.group_name: str = 'ui_automation'
        self.actuator_info: dict = {}  # 执行器信息
        self.language: str = 'zh-Hans'

    def _get_query_params(self) -> dict[str, list[str]]:
        query_string = self.scope.get('query_string', b'').decode('utf-8')
        if not query_string:
            return {}
        if '=' not in query_string:
            return {'id': [query_string]}
        return parse_qs(query_string)

    def _localize(self, message: str) -> str:
        return translate_app_text(message, self.language)
    
    async def connect(self):
        """建立连接"""
        path = self.scope.get('path', '')
        query_params = self._get_query_params()
        self.language = query_params.get('lang', ['zh-Hans'])[0]

        # 获取客户端IP
        client = self.scope.get('client', ['unknown', 0])
        client_ip = client[0] if client else 'unknown'
        
        # 根据路径判断是前端还是执行器
        if '/actuator/' in path:
            self.is_actuator = True
            self.user_id = query_params.get('id', [None])[0] or query_params.get('user_id', [None])[0]
            if not self.user_id:
                self.user_id = f"actuator_{id(self)}"
            registration_token = query_params.get('token', [None])[0] or query_params.get('registration_token', [None])[0]
            token_ok, token_reason = self._validate_actuator_registration_token(registration_token)
            if not token_ok:
                logger.warning(f"执行器连接被拒绝: {self.user_id}, reason={token_reason}")
                await self.close(code=4403)
                return
            
            # 初始化执行器信息
            self.actuator_info = {
                'id': self.user_id,
                'name': self.user_id,
                'ip': client_ip,
                'type': 'web_ui',
                'is_open': True,
                'debug': False,
                'browser_type': 'chromium',
                'headless': False,
                'version': '',
                'connected_at': timezone.now().isoformat(),
                'last_seen_at': timezone.now().isoformat(),
                'authenticated': True,
                'connection_status': 'online',
                'registration_token_required': bool(getattr(settings, 'UI_ACTUATOR_REGISTRATION_TOKEN_REQUIRED', False)),
            }
            SocketUserManager.add_actuator(self.user_id, self)
        else:
            self.is_actuator = False
            # 从用户认证获取ID
            user = self.scope.get('user')
            if user and hasattr(user, 'username'):
                self.user_id = user.username
            else:
                self.user_id = f"web_{id(self)}"
            SocketUserManager.add_web_user(self.user_id, self)
        
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        
        # 发送连接成功消息
        await self.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg=self._localize(f"{'执行器' if self.is_actuator else 'Web客户端'}连接成功"),
            user=self.user_id
        ))
        
        logger.info(f"{'执行器' if self.is_actuator else 'Web'}连接: {self.user_id}")
    
    async def disconnect(self, close_code):
        """断开连接"""
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        
        if self.is_actuator:
            SocketUserManager.remove_actuator(self.user_id)
        else:
            SocketUserManager.remove_web_user(self.user_id)
        
        logger.info(f"{'执行器' if self.is_actuator else 'Web'}断开: {self.user_id}, code: {close_code}")
    
    async def receive(self, text_data=None, bytes_data=None):
        """接收消息"""
        if not text_data:
            return
        
        try:
            data = json.loads(text_data)
            socket_data = SocketDataModel(**data)
            
            # 如果有func_name，进行路由处理
            if socket_data.data and socket_data.data.func_name:
                await self.route_message(socket_data)
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析错误: {e}")
            await self.send_json(SocketDataModel(
                code=ResponseCode.ERROR,
                msg=self._localize(f"消息格式错误: {str(e)}")
            ))
        except Exception as e:
            logger.error(f"处理消息错误: {e}", exc_info=True)
            await self.send_json(SocketDataModel(
                code=ResponseCode.ERROR,
                msg=self._localize(f"处理错误: {str(e)}")
            ))
    
    async def route_message(self, socket_data: SocketDataModel):
        """路由消息到对应处理器"""
        func_name = socket_data.data.func_name
        func_args = socket_data.data.func_args
        
        # 前端发送的执行请求 -> 转发给执行器
        if self.is_actuator:
            # 执行器返回的结果 -> 转发给前端
            handler_map = {
                UiSocketEnum.STEP_RESULT: self.handle_step_result,
                UiSocketEnum.PAGE_STEP_RESULT: self.handle_page_step_result,
                UiSocketEnum.CASE_RESULT: self.handle_case_result,
                UiSocketEnum.AI_GENERATION_ACK: self.handle_ai_generation_ack,
                UiSocketEnum.AI_GENERATION_RESULT: self.handle_ai_generation_result,
                UiSocketEnum.SET_ACTUATOR_INFO: self.handle_set_actuator_info,
                UiSocketEnum.ACTUATOR_HEARTBEAT: self.handle_actuator_heartbeat,
            }
        else:
            # 前端发送执行请求 -> 转发给执行器
            handler_map = {
                UiSocketEnum.PAGE_STEPS: self.handle_execute_page_steps,
                UiSocketEnum.TEST_CASE: self.handle_execute_test_case,
                UiSocketEnum.TEST_CASE_BATCH: self.handle_execute_batch,
                UiSocketEnum.STOP_EXECUTION: self.handle_stop_execution,
            }
        
        handler = handler_map.get(func_name)
        if handler:
            await handler(func_args, socket_data.user)
        else:
            logger.warning(f"未知的func_name: {func_name}")
            await self.send_json(SocketDataModel(
                code=ResponseCode.ERROR,
                msg=self._localize(f"未知的操作: {func_name}")
            ))
    
    async def handle_execute_page_steps(self, args: dict, user: str):
        """处理执行页面步骤请求"""
        actuator_id = args.get('actuator_id')
        if actuator_id:
            actuator = SocketUserManager.get_actuator_by_id(actuator_id)
        else:
            actuator = SocketUserManager.get_actuator()
        
        if not actuator:
            await self.send_json(SocketDataModel(
                code=ResponseCode.ERROR,
                msg=self._localize("没有可用的执行器，请先启动执行器服务" if not actuator_id else f"执行器 {actuator_id} 不在线")
            ))
            return
        
        # 转发给执行器，使用服务端分配的user_id而非客户端传入的user
        await actuator.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg="execute",
            user=self.user_id,
            is_notice=NoticeType.ACTUATOR,
            data=QueueModel(
                func_name=UiSocketEnum.PAGE_STEPS,
                func_args=args
            )
        ))
        
        await self.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg=self._localize("任务已发送给执行器")
        ))
    
    async def handle_execute_test_case(self, args: dict, user: str):
        """处理执行测试用例请求"""
        actuator_id = args.get('actuator_id')
        if actuator_id:
            actuator = SocketUserManager.get_actuator_by_id(actuator_id)
        else:
            actuator = SocketUserManager.get_actuator()
        if not actuator:
            await self.send_json(SocketDataModel(
                code=ResponseCode.ERROR,
                msg=self._localize("没有可用的执行器，请先启动执行器服务" if not actuator_id else f"执行器 {actuator_id} 不在线")
            ))
            return
        
        # 持久化执行启动状态，避免前端刷新后仍显示未执行。
        case_id = args.get('case_id')
        if case_id:
            execution_record_id = await self.start_execution_record(case_id, args)
            if execution_record_id:
                args['execution_record_id'] = execution_record_id
        
        # 转发给执行器，使用服务端分配的user_id而非客户端传入的user
        await actuator.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg="execute",
            user=self.user_id,
            is_notice=NoticeType.ACTUATOR,
            data=QueueModel(
                func_name=UiSocketEnum.TEST_CASE,
                func_args=args
            )
        ))
        
        await self.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg=self._localize("任务已发送给执行器")
        ))
    
    async def handle_execute_batch(self, args: dict, user: str):
        """处理批量执行请求"""
        actuator_id = args.get('actuator_id')
        if actuator_id:
            actuator = SocketUserManager.get_actuator_by_id(actuator_id)
        else:
            actuator = SocketUserManager.get_actuator()
        if not actuator:
            await self.send_json(SocketDataModel(
                code=ResponseCode.ERROR,
                msg=self._localize("没有可用的执行器，请先启动执行器服务" if not actuator_id else f"执行器 {actuator_id} 不在线")
            ))
            return

        case_ids = args.get('case_ids', [])
        if not case_ids:
            await self.send_json(SocketDataModel(
                code=ResponseCode.ERROR,
                msg=self._localize("没有选择要执行的用例")
            ))
            return

        # 创建批量执行记录
        batch_id = await self.create_batch_record(case_ids)
        if not batch_id:
            await self.send_json(SocketDataModel(
                code=ResponseCode.ERROR,
                msg=self._localize("创建批量执行记录失败")
            ))
            return

        # 将 batch_id 加入参数传递给执行器
        args['batch_id'] = batch_id

        # 转发给执行器，使用服务端分配的user_id而非客户端传入的user
        await actuator.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg="execute_batch",
            user=self.user_id,
            is_notice=NoticeType.ACTUATOR,
            data=QueueModel(
                func_name=UiSocketEnum.TEST_CASE_BATCH,
                func_args=args
            )
        ))

        await self.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg=self._localize("批量任务已发送给执行器"),
            data=QueueModel(
                func_name='batch_created',
                func_args={'batch_id': batch_id, 'total_cases': len(case_ids)}
            )
        ))
    
    @sync_to_async
    def start_execution_record(self, case_id: int, args: dict) -> int | None:
        """创建执行中的用例记录，并同步用例当前状态。"""
        from .models import UiExecutionRecord, UiTestCase
        from django.contrib.auth.models import User
        from django.utils import timezone

        executor = None
        executor_id = args.get('executor_id')
        if executor_id:
            try:
                executor = User.objects.get(id=executor_id)
            except User.DoesNotExist:
                logger.warning(f"执行人不存在: id={executor_id}")

        try:
            record = UiExecutionRecord.objects.create(
                test_case_id=case_id,
                executor=executor,
                status=1,
                trigger_type='manual',
                environment={'env_config_id': args.get('env_config_id')} if args.get('env_config_id') else None,
                start_time=timezone.now(),
            )
            UiTestCase.objects.filter(id=case_id).update(
                status=1,
                result_data={'last_execution': record.id, 'steps': []},
                error_message=None,
            )
            logger.info(f"测试用例执行已启动: case_id={case_id}, record_id={record.id}")
            return record.id
        except Exception as e:
            logger.error(f"创建执行中记录失败: case_id={case_id}, error={e}", exc_info=True)
            return None

    async def handle_stop_execution(self, args: dict, user: str):
        """处理停止执行请求"""
        # 广播给所有执行器
        for actuator in SocketUserManager.get_all_actuators():
            await actuator.send_json(SocketDataModel(
                code=ResponseCode.SUCCESS,
                msg="stop",
                user=self.user_id,
                is_notice=NoticeType.ACTUATOR,
                data=QueueModel(
                    func_name=UiSocketEnum.STOP_EXECUTION,
                    func_args=args
                )
            ))
        
        await self.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg=self._localize("停止信号已发送")
        ))
    
    async def handle_step_result(self, args: dict, user: str):
        """处理步骤执行结果（来自执行器）"""
        logger.info(f"收到步骤结果, 目标用户: {user}, 当前Web用户: {list(SocketUserManager._web_users.keys())}")
        
        # 转发给对应的前端用户
        web_user = SocketUserManager.get_web_user(user)
        if web_user:
            await web_user.send_json(SocketDataModel(
                code=ResponseCode.SUCCESS,
                msg="step_result",
                user=user,
                is_notice=NoticeType.WEB,
                data=QueueModel(
                    func_name=UiSocketEnum.STEP_RESULT,
                    func_args=args
                )
            ))
            logger.info(f"步骤结果已发送给用户: {user}")
        else:
            logger.warning(f"找不到Web用户: {user}")
        
        # 同时广播给所有前端（用于多人协作）
        await self.channel_layer.group_send(
            self.group_name,
            {
                'type': 'broadcast_result',
                'data': {
                    'func_name': UiSocketEnum.STEP_RESULT,
                    'args': args
                }
            }
        )
    
    async def handle_page_step_result(self, args: dict, user: str):
        """处理页面步骤执行结果（来自执行器）"""
        logger.info(f"收到页面步骤结果, 执行用户: {user}")

        # 更新页面步骤状态到数据库
        page_step_id = args.get('page_step_id')
        if page_step_id:
            status_str = args.get('status', 'unknown')
            status = 2 if status_str == 'success' else 3  # 2=成功, 3=失败
            await self.update_page_step_status(page_step_id, status, args)

        # 广播给所有前端
        await self.channel_layer.group_send(
            self.group_name,
            {
                'type': 'broadcast_result',
                'data': {
                    'func_name': UiSocketEnum.PAGE_STEP_RESULT,
                    'args': args,
                    'user': user
                }
            }
        )
    
    async def handle_case_result(self, args: dict, user: str):
        """处理用例执行结果（来自执行器）"""
        logger.info(f"收到用例结果, 执行用户: {user}")
        
        # 保存执行结果到数据库
        await self.save_execution_result(args)
        
        # 广播给所有前端（避免重复发送）
        await self.channel_layer.group_send(
            self.group_name,
            {
                'type': 'broadcast_result',
                'data': {
                    'func_name': UiSocketEnum.CASE_RESULT,
                    'args': args,
                    'user': user  # 携带执行用户信息
                }
            }
        )

    async def handle_ai_generation_result(self, args: dict, user: str):
        """处理 Playwright MCP AI 生成任务结果（来自执行器）"""
        logger.info(f"收到 AI 生成任务结果, 执行用户: {user}, task_id={args.get('task_id')}")
        await self.save_ai_generation_result(args)
        await self.channel_layer.group_send(
            self.group_name,
            {
                'type': 'broadcast_result',
                'data': {
                    'func_name': UiSocketEnum.AI_GENERATION_RESULT,
                    'args': args,
                    'user': user,
                }
            }
        )

    async def handle_ai_generation_ack(self, args: dict, user: str):
        """处理 AI 生成任务入队确认（来自执行器）。"""
        logger.info(
            f"收到 AI 生成任务入队确认, 执行用户: {user}, "
            f"task_id={args.get('task_id')}, dispatch_id={args.get('dispatch_id')}"
        )
        await self.save_ai_generation_ack(args)
        await self.channel_layer.group_send(
            self.group_name,
            {
                'type': 'broadcast_result',
                'data': {
                    'func_name': UiSocketEnum.AI_GENERATION_ACK,
                    'args': args,
                    'user': user,
                }
            }
        )
    
    async def broadcast_result(self, event):
        """广播结果给所有前端"""
        if not self.is_actuator:
            await self.send_json(SocketDataModel(
                code=ResponseCode.SUCCESS,
                msg="broadcast",
                is_notice=NoticeType.WEB,
                data=QueueModel(
                    func_name=event['data']['func_name'],
                    func_args=event['data']['args']
                )
            ))
    
    @sync_to_async
    def update_testcase_status(self, case_id: int, status: int):
        """更新测试用例状态"""
        from .models import UiTestCase
        try:
            UiTestCase.objects.filter(id=case_id).update(status=status)
            logger.info(f"测试用例状态更新: case_id={case_id}, status={status}")
        except Exception as e:
            logger.error(f"更新测试用例状态失败: {e}")

    @sync_to_async
    def update_page_step_status(self, page_step_id: int, status: int, result_data: dict):
        """更新页面步骤状态"""
        from .models import UiPageSteps
        try:
            UiPageSteps.objects.filter(id=page_step_id).update(
                status=status,
                result_data=result_data
            )
            logger.info(f"页面步骤状态更新: page_step_id={page_step_id}, status={status}")
        except Exception as e:
            logger.error(f"更新页面步骤状态失败: {e}")
    
    @sync_to_async
    def save_execution_result(self, args: dict):
        """保存执行结果到数据库"""
        from .models import UiExecutionRecord, UiTestCase, UiBatchExecutionRecord
        from django.contrib.auth.models import User
        from datetime import timedelta
        from django.utils import timezone

        logger.info(f">>> save_execution_result 被调用, args: {args}")

        # 状态映射: string -> int
        status_map = {'success': 2, 'failed': 3, 'skipped': 4}
        status_str = args.get('status', 'unknown')
        status = status_map.get(status_str, 3)  # 默认失败

        duration = args.get('duration', 0)
        end_time = timezone.now()
        start_time = end_time - timedelta(seconds=duration) if duration else end_time

        # 提取步骤结果
        steps = args.get('steps', [])
        screenshots = []
        for step in steps:
            if step.get('screenshot'):
                screenshots.append(step['screenshot'])

        # 提取 trace 路径
        trace_path = args.get('trace_path')

        # 提取 batch_id
        batch_id = args.get('batch_id')
        
        # 提取执行人信息
        executor_id = args.get('executor_id')
        executor = None
        if executor_id:
            try:
                executor = User.objects.get(id=executor_id)
                logger.info(f"找到执行人: id={executor_id}, username={executor.username}")
            except User.DoesNotExist:
                logger.warning(f"执行人不存在: id={executor_id}")

        case_id = args.get('case_id')
        execution_record_id = args.get('execution_record_id')
        try:
            record = None
            if execution_record_id:
                record = UiExecutionRecord.objects.filter(id=execution_record_id, test_case_id=case_id).first()
            if record:
                record.batch_id = batch_id
                record.executor = executor or record.executor
                record.status = status
                record.step_results = steps
                record.screenshots = screenshots
                record.trace_path = trace_path
                record.log = args.get('message', '')
                record.error_message = args.get('message') if status == 3 else None
                if not record.start_time:
                    record.start_time = start_time
                record.end_time = end_time
                record.duration = duration
                record.save(update_fields=[
                    'batch', 'executor', 'status', 'step_results', 'screenshots',
                    'trace_path', 'log', 'error_message', 'start_time', 'end_time', 'duration'
                ])
            else:
                record = UiExecutionRecord.objects.create(
                    test_case_id=case_id,
                    batch_id=batch_id,
                    executor=executor,
                    status=status,
                    trigger_type='manual',
                    step_results=steps,
                    screenshots=screenshots,
                    trace_path=trace_path,
                    log=args.get('message', ''),
                    error_message=args.get('message') if status == 3 else None,
                    start_time=start_time,
                    end_time=end_time,
                    duration=duration
                )
            logger.info(f"执行记录已保存: id={record.id}, case_id={case_id}, batch_id={batch_id}, status={status}")

            # 同时更新测试用例的状态
            if case_id:
                UiTestCase.objects.filter(id=case_id).update(
                    status=status,
                    result_data={'last_execution': record.id, 'steps': steps},
                    error_message=args.get('message') if status == 3 else None
                )
                logger.info(f"测试用例状态已更新: case_id={case_id}, status={status}")

            # 更新批量执行记录统计
            if batch_id:
                try:
                    batch = UiBatchExecutionRecord.objects.get(id=batch_id)
                    batch.update_statistics()
                    logger.info(f"批量执行记录统计已更新: batch_id={batch_id}")
                except UiBatchExecutionRecord.DoesNotExist:
                    logger.warning(f"批量执行记录不存在: batch_id={batch_id}")
        except Exception as e:
            logger.error(f"保存执行结果失败: {e}", exc_info=True)

    @sync_to_async
    def save_ai_generation_result(self, args: dict):
        """保存 Playwright MCP 生成、验证和修复结果。"""
        return save_ai_generation_result_sync(args)
        from .models import UiAiGenerationTask, UiElementMap
        from django.utils import timezone

        task_id = args.get('task_id')
        if not task_id:
            logger.warning("AI 生成任务结果缺少 task_id")
            return

        try:
            task = UiAiGenerationTask.objects.select_related('project', 'environment_config', 'creator').get(id=task_id)
        except UiAiGenerationTask.DoesNotExist:
            logger.warning(f"AI 生成任务不存在: task_id={task_id}")
            return

        status_str = args.get('status') or 'failed'
        existing_repair_history = task.repair_history if isinstance(task.repair_history, list) else []
        existing_preserved_history = [
            item for item in existing_repair_history
            if isinstance(item, dict) and item.get('type') in {'dispatch_ack', 'async_llm_business_planning'}
        ]
        incoming_repair_history = args.get('repair_history') or []
        if not isinstance(incoming_repair_history, list):
            incoming_repair_history = []
        incoming_history_keys = {
            (
                item.get('type'),
                item.get('dispatch_id') or item.get('status') or '',
                item.get('queued_at') or item.get('started_at') or item.get('completed_at') or '',
            )
            for item in incoming_repair_history
            if isinstance(item, dict)
        }
        merged_repair_history = [
            *incoming_repair_history,
            *[
                item for item in existing_preserved_history
                if (
                    item.get('type'),
                    item.get('dispatch_id') or item.get('status') or '',
                    item.get('queued_at') or item.get('started_at') or item.get('completed_at') or '',
                ) not in incoming_history_keys
            ],
        ]
        existing_verification = task.verification_result if isinstance(task.verification_result, dict) else {}

        task.status = 'success' if status_str == 'success' else 'failed'
        # Reaching this handler means the actuator returned a result. Keep
        # dispatch status about delivery, and expose execution failure via
        # task.status/failure_category/error_message.
        task.dispatch_status = 'completed'
        task.completed_at = timezone.now()
        task.test_plan = args.get('test_plan') or {}
        task.mcp_observations = args.get('mcp_observations') or []
        task.element_map_snapshot = args.get('element_map') or {}
        generated_case = args.get('generated_case') or {}
        if isinstance(generated_case, dict) and args.get('intent_analysis'):
            generated_case['intent_analysis'] = args.get('intent_analysis')
        task.generated_case = generated_case
        task.generated_script = args.get('generated_script') or ''
        task.generated_script_hash = args.get('generated_script_hash') or ''
        incoming_verification = args.get('verification_result') or {}
        if not isinstance(incoming_verification, dict):
            incoming_verification = {}
        if (
            isinstance(existing_verification.get('async_llm_planning'), dict)
            and 'async_llm_planning' not in incoming_verification
        ):
            incoming_verification['async_llm_planning'] = existing_verification['async_llm_planning']
        if (
            isinstance(existing_verification.get('async_llm_intent'), dict)
            and 'async_llm_intent' not in incoming_verification
        ):
            incoming_verification['async_llm_intent'] = existing_verification['async_llm_intent']
        existing_script_artifacts = existing_verification.get('script_artifacts')
        if isinstance(existing_script_artifacts, dict):
            incoming_script_artifacts = incoming_verification.get('script_artifacts')
            if not isinstance(incoming_script_artifacts, dict):
                incoming_script_artifacts = {}
            for key, value in existing_script_artifacts.items():
                if str(key).startswith('async_') and key not in incoming_script_artifacts:
                    incoming_script_artifacts[key] = value
            if incoming_script_artifacts:
                incoming_verification['script_artifacts'] = incoming_script_artifacts
        task.verification_result = incoming_verification
        task.repair_history = merged_repair_history
        task.failure_category = args.get('failure_category') or ''
        task.current_repair_round = int(args.get('current_repair_round') or len(task.repair_history or []))
        task.error_message = args.get('message') if task.status == 'failed' else ''

        element_map_payload = args.get('element_map') or {}
        pages = element_map_payload.get('pages') if isinstance(element_map_payload, dict) else None
        if pages:
            coverage = element_map_payload.get('coverage_summary') or {
                'page_count': len(pages),
                'element_count': sum(len(page.get('elements') or []) for page in pages if isinstance(page, dict)),
            }
            element_map = task.element_map
            field_values = _element_map_field_values(element_map_payload, coverage, task, element_map)
            if element_map is None:
                element_map = UiElementMap.objects.create(
                    project=task.project,
                    environment_config=task.environment_config,
                    name=f"{task.name}-元素地图",
                    base_url=element_map_payload.get('base_url') or task.target_url,
                    status='draft',
                    creator=task.creator,
                    **field_values,
                )
            elif element_map.map_hash and field_values['map_hash'] and element_map.map_hash == field_values['map_hash']:
                for field, value in field_values.items():
                    setattr(element_map, field, value)
                element_map.save(update_fields=[
                    'map_json', 'coverage_summary', 'low_confidence_items', 'risk_items',
                    'version_group', 'role_key', 'permission_key', 'map_hash',
                    'baseline_hash', 'diff_summary', 'stale_status', 'stale_reason', 'updated_at'
                ])
            elif element_map.status == 'confirmed':
                element_map.stale_status = 'superseded'
                element_map.stale_reason = 'AI 重新采集发现元素地图内容变化，已生成新的 draft 版本'
                element_map.save(update_fields=['stale_status', 'stale_reason', 'updated_at'])
                latest_version = UiElementMap.objects.filter(
                    project=task.project,
                    environment_config=task.environment_config,
                    version_group=field_values['version_group'],
                ).order_by('-version', '-id').first()
                next_version = (latest_version.version if latest_version else element_map.version) + 1
                element_map = UiElementMap.objects.create(
                    project=task.project,
                    environment_config=task.environment_config,
                    parent_map=task.element_map,
                    name=task.element_map.name,
                    version=next_version,
                    base_url=element_map_payload.get('base_url') or task.target_url,
                    status='draft',
                    creator=task.creator,
                    **field_values,
                )
            else:
                for field, value in field_values.items():
                    setattr(element_map, field, value)
                element_map.save(update_fields=[
                    'map_json', 'coverage_summary', 'low_confidence_items', 'risk_items',
                    'version_group', 'role_key', 'permission_key', 'map_hash',
                    'baseline_hash', 'diff_summary', 'stale_status', 'stale_reason', 'updated_at'
                ])
            task.element_map = element_map
            task.element_map_snapshot = element_map.map_json

        task.save(update_fields=[
            'status', 'completed_at', 'test_plan', 'mcp_observations',
            'element_map_snapshot', 'generated_case', 'generated_script',
            'generated_script_hash', 'verification_result', 'repair_history',
            'failure_category', 'current_repair_round', 'error_message',
            'element_map', 'dispatch_status', 'updated_at'
        ])
        logger.info(f"AI 生成任务结果已保存: task_id={task.id}, status={task.status}")

    @sync_to_async
    def save_ai_generation_ack(self, args: dict):
        """保存执行器入队 ACK，用于诊断 WebSocket 投递是否成功。"""
        from .models import UiAiGenerationTask
        from django.utils import timezone

        task_id = args.get('task_id')
        if not task_id:
            logger.warning("AI 生成任务 ACK 缺少 task_id")
            return

        try:
            task = UiAiGenerationTask.objects.get(id=task_id)
        except UiAiGenerationTask.DoesNotExist:
            logger.warning(f"AI 生成任务 ACK 对应任务不存在: task_id={task_id}")
            return

        ack_item = {
            'type': 'dispatch_ack',
            'task_id': task.id,
            'dispatch_id': args.get('dispatch_id') or '',
            'actuator_id': args.get('actuator_id') or '',
            'status': args.get('status') or 'queued',
            'source': args.get('source') or 'websocket',
            'received_at': timezone.now().isoformat(),
        }
        repair_history = task.repair_history if isinstance(task.repair_history, list) else []
        if not any(
            item.get('type') == 'dispatch_ack'
            and item.get('dispatch_id') == ack_item['dispatch_id']
            for item in repair_history
            if isinstance(item, dict)
        ):
            repair_history.append(ack_item)
            task.repair_history = repair_history
        ack_status = ack_item['status']
        if ack_status == 'queued':
            task.dispatch_status = 'acked'
        elif ack_status == 'duplicate':
            # Duplicate ACK normally means the actuator is already processing
            # the task and a compensation pull saw it again. Keep the
            # user-facing delivery state as queued instead of surfacing this as
            # a warning state.
            if task.dispatch_status in {'pending', 'sent', 'failed', 'duplicate'}:
                task.dispatch_status = 'acked'
        else:
            task.dispatch_status = ack_status[:20] or task.dispatch_status
        task.last_ack_at = timezone.now()
        if ack_item['actuator_id']:
            task.actuator_id = ack_item['actuator_id']
        task.save(update_fields=[
            'repair_history', 'dispatch_status', 'last_ack_at', 'actuator_id', 'updated_at'
        ])
        logger.info(f"AI 生成任务 ACK 已保存: task_id={task.id}, dispatch_id={ack_item['dispatch_id']}")

    @sync_to_async
    def create_batch_record(self, case_ids: list) -> int:
        """创建批量执行记录"""
        from .models import UiBatchExecutionRecord, UiTestCase
        from django.utils import timezone

        try:
            # 获取用例名称用于批次命名
            case_names = list(UiTestCase.objects.filter(id__in=case_ids).values_list('name', flat=True)[:3])
            batch_name = f"批量执行: {', '.join(case_names)}"
            if len(case_ids) > 3:
                batch_name += f" 等{len(case_ids)}个用例"

            batch = UiBatchExecutionRecord.objects.create(
                name=batch_name,
                total_cases=len(case_ids),
                status=1,  # 执行中
                start_time=timezone.now()
            )
            logger.info(f"批量执行记录已创建: id={batch.id}, total={len(case_ids)}")
            return batch.id
        except Exception as e:
            logger.error(f"创建批量执行记录失败: {e}", exc_info=True)
            return None
    
    async def handle_set_actuator_info(self, args: dict, user: str):
        """处理执行器信息更新（仅执行器可调用）"""
        if not self.is_actuator:
            return
        self.actuator_info = SocketUserManager.update_actuator_info(self.user_id, self, args, authenticated=True)
        logger.info(f"执行器 {self.user_id} 信息已更新: {self.actuator_info}")
        
        await self.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg="执行器信息已更新"
        ))

    async def handle_actuator_heartbeat(self, args: dict, user: str):
        """处理执行器心跳"""
        if not self.is_actuator:
            return
        self.actuator_info = SocketUserManager.update_actuator_info(self.user_id, self, args, authenticated=True)
        logger.debug(f"执行器 {self.user_id} 心跳更新: last_seen_at={self.actuator_info.get('last_seen_at')}")
        await self.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg="心跳已更新"
        ))

    def _validate_actuator_registration_token(self, token: Optional[str]) -> tuple[bool, str]:
        required = bool(getattr(settings, 'UI_ACTUATOR_REGISTRATION_TOKEN_REQUIRED', not settings.DEBUG))
        expected = str(getattr(settings, 'UI_ACTUATOR_REGISTRATION_TOKEN', '') or '').strip()
        provided = str(token or '').strip()
        if not required and not expected:
            return True, 'optional'
        if not provided:
            return (False, 'missing_registration_token') if required or expected else (True, 'optional')
        if expected and provided != expected:
            return False, 'invalid_registration_token'
        if required and not expected:
            return False, 'registration_token_not_configured'
        return True, 'ok'
    
    async def send_json(self, data: SocketDataModel):
        """发送JSON消息"""
        await self.send(text_data=data.model_dump_json())
    
    @classmethod
    async def send_to_actuator(cls, task: ExecutionTaskModel, user: str) -> bool:
        """发送任务给执行器（供视图调用）"""
        actuator = SocketUserManager.get_actuator()
        if not actuator:
            return False
        
        func_name = UiSocketEnum.TEST_CASE
        if task.task_type == 'page_steps':
            func_name = UiSocketEnum.PAGE_STEPS
        elif task.task_type == 'batch':
            func_name = UiSocketEnum.TEST_CASE_BATCH
        
        await actuator.send_json(SocketDataModel(
            code=ResponseCode.SUCCESS,
            msg="execute",
            user=user,
            is_notice=NoticeType.ACTUATOR,
            data=QueueModel(
                func_name=func_name,
                func_args=task.model_dump()
            )
        ))
        return True
