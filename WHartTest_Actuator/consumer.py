"""
UI自动化执行器 - 任务消费者
"""

import asyncio
import copy
import difflib
import html
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional, Any
from urllib.parse import quote
import httpx

from models import (
    SocketDataModel, QueueModel, UiSocketEnum, 
    StepResultModel, CaseResultModel, ResponseCode, NoticeType
)
from websocket_client import WebSocketClient
from browser_installer import get_browser_executable_path
from executor import (
    PlaywrightExecutor, StepConfig, PageStepConfig, TestCaseConfig
)
from data_processor import reset_data_processor, DataProcessor

logger = logging.getLogger('actuator')


class TaskConsumer:
    """任务消费者 - 处理从服务器接收的执行任务"""
    
    def __init__(self, ws_client: WebSocketClient, api_base_url: str, 
                 config: Any = None,
                 api_username: str = 'admin', api_password: str = 'admin123456'):
        self.ws_client = ws_client
        self.api_base_url = api_base_url.rstrip('/')
        self.api_username = api_username
        self.api_password = api_password
        self._api_token: Optional[str] = None
        self.config = config
        
        # 从配置创建执行器
        executor_config = {}
        if config:
            # 超时配置：config中是秒，executor需要毫秒
            launch_timeout = getattr(config, 'launch_timeout', 30)
            action_timeout = getattr(config, 'action_timeout', 30)
            executor_config = {
                'browser_type': getattr(config, 'browser_type', 'chromium'),
                'headless': getattr(config, 'headless', False),
                'persistent': getattr(config, 'persistent', True),
                'user_data_dir': getattr(config, 'user_data_dir', './data/browser'),
                'ignore_https_errors': getattr(config, 'ignore_https_errors', True),
                'launch_timeout': launch_timeout * 1000,  # 转毫秒
                'action_timeout': action_timeout * 1000,  # 转毫秒
                'screenshot_dir': getattr(config, 'screenshot_dir', './data/screenshots'),
                # Trace 配置
                'trace_enabled': getattr(config, 'trace_enabled', False),
                'trace_dir': getattr(config, 'trace_dir', './data/traces'),
                'trace_screenshots': getattr(config, 'trace_screenshots', True),
                'trace_snapshots': getattr(config, 'trace_snapshots', True),
                'trace_sources': getattr(config, 'trace_sources', False),
                'mcp_provider': getattr(config, 'mcp_provider', 'playwright-native'),
                'mcp_transport': getattr(config, 'mcp_transport', 'stdio'),
                'mcp_command': getattr(config, 'mcp_command', 'npx'),
                'mcp_args': getattr(config, 'mcp_args', None),
                'mcp_cwd': getattr(config, 'mcp_cwd', None),
                'mcp_server_url': getattr(config, 'mcp_server_url', None),
            }
        self.executor = PlaywrightExecutor(**executor_config)
        self.task_queue: asyncio.Queue[QueueModel] = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._current_user: Optional[str] = None
        self._queued_ai_generation_task_ids: set[int] = set()
        self._active_ai_generation_task_ids: set[int] = set()
        self._pending_ai_poll_interval = 8.0
        self._manual_capture_inflight_keys: set[str] = set()
        self._manual_capture_completed_results: dict[str, dict[str, Any]] = {}
        self.typescript_spec_enabled = getattr(config, 'typescript_spec_enabled', True) if config else True
        self.typescript_spec_timeout = int(getattr(config, 'typescript_spec_timeout', 120) if config else 120)
        self.typescript_spec_allow_npx = getattr(config, 'typescript_spec_allow_npx', True) if config else True
        self.typescript_spec_command = getattr(config, 'typescript_spec_command', None) if config else None

        # 启动时清理过期文件（超过7天）
        self._cleanup_expired_files(
            getattr(config, 'screenshot_dir', './data/screenshots') if config else './data/screenshots',
            getattr(config, 'trace_dir', './data/traces') if config else './data/traces',
            max_age_days=7
        )

    def _cleanup_expired_files(self, screenshot_dir: str, trace_dir: str, max_age_days: int = 7):
        """清理超过指定天数的本地临时文件"""
        import os
        from pathlib import Path

        now = time.time()
        max_age_seconds = max_age_days * 24 * 60 * 60
        cleaned_count = 0

        for directory in [screenshot_dir, trace_dir]:
            dir_path = Path(directory)
            if not dir_path.exists():
                continue

            for file_path in dir_path.iterdir():
                if not file_path.is_file():
                    continue
                try:
                    file_age = now - file_path.stat().st_mtime
                    if file_age > max_age_seconds:
                        file_path.unlink()
                        cleaned_count += 1
                except Exception as e:
                    logger.warning(f"清理过期文件失败 {file_path}: {e}")

        if cleaned_count > 0:
            logger.info(f"已清理 {cleaned_count} 个超过 {max_age_days} 天的过期文件")
    
    async def _get_api_token(self) -> Optional[str]:
        """获取API认证token"""
        if self._api_token:
            return self._api_token
        
        url = f"{self.api_base_url}/api/token/"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json={
                    "username": self.api_username,
                    "password": self.api_password
                })
                if response.status_code == 200:
                    data = response.json()
                    if data.get('status') == 'success' and data.get('data'):
                        self._api_token = data['data'].get('access')
                    else:
                        self._api_token = data.get('access')
                    logger.info("获取API Token成功")
                    return self._api_token
                else:
                    logger.error(f"获取Token失败: {response.status_code}")
                    return None
        except Exception as e:
            logger.error(f"获取Token请求失败: {e}")
            return None
    
    async def _api_get(self, path: str) -> Optional[dict]:
        """带认证的API GET请求"""
        token = await self._get_api_token()
        if not token:
            return None
        
        url = f"{self.api_base_url}{path}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                if response.status_code == 200:
                    data = response.json()
                    # 提取data字段
                    if data.get('status') == 'success' and 'data' in data:
                        return data['data']
                    return data
                elif response.status_code == 401:
                    # Token过期，重新获取
                    self._api_token = None
                    return await self._api_get(path)
                else:
                    logger.error(f"API请求失败: {response.status_code} - {path}")
                    return None
        except Exception as e:
            logger.error(f"API请求异常: {e}")
            return None

    async def _api_post(self, path: str, payload: dict, timeout: float = 360.0) -> Optional[dict]:
        """带认证的API POST请求"""
        token = await self._get_api_token()
        if not token:
            return None

        url = f"{self.api_base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
                if response.status_code in (200, 201, 202):
                    data = response.json()
                    if data.get('status') == 'success' and 'data' in data:
                        return data['data']
                    return data
                if response.status_code == 401:
                    self._api_token = None
                    return await self._api_post(path, payload, timeout=timeout)
                logger.error(f"API POST请求失败: {response.status_code} - {path} - {response.text[:500]}")
                return None
        except Exception as e:
            logger.error(f"API POST请求异常: {type(e).__name__}: {e}")
            return None

    def _compact_llm_payload_value(self, value: Any, depth: int = 0) -> Any:
        """裁剪发给后端 LLM 规划接口的上下文，避免大报告拖慢同步请求。"""
        if depth >= 5:
            text = str(value)
            return text[:1000] if len(text) > 1000 else value
        if isinstance(value, str):
            return value[:4000]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, list):
            return [self._compact_llm_payload_value(item, depth + 1) for item in value[:40]]
        if isinstance(value, dict):
            compact = {}
            exploration_session_keys = {
                'auth_setup', 'auth_observations', 'storage_state', 'cookies',
                'local_storage', 'session_storage',
            }
            drop_keys = {
                'html', 'html_report', 'junit_xml', 'flow_junit_xml', 'typescript_junit_xml',
                'trace', 'trace_events', 'trace_payload', 'resources', 'screenshots',
                'screenshot_base64', 'runtime_video', 'attachment_content',
            }
            for index, (key, item) in enumerate(value.items()):
                if index >= 120:
                    compact['_truncated_keys'] = max(0, len(value) - 120)
                    break
                if key in exploration_session_keys:
                    continue
                if key in drop_keys:
                    compact[key] = '[omitted_for_llm_plan]'
                    continue
                compact[key] = self._compact_llm_payload_value(item, depth + 1)
            return compact
        text = str(value)
        return text[:1000] if len(text) > 1000 else text

    def _build_llm_plan_payload(self, result: dict) -> dict:
        verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
        flow_execution = verification.get('flow_execution') if isinstance(verification.get('flow_execution'), dict) else {}
        script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
        typescript_execution = script_artifacts.get('typescript_spec_execution') if isinstance(script_artifacts.get('typescript_spec_execution'), dict) else {}
        attempts = typescript_execution.get('attempts') if isinstance(typescript_execution.get('attempts'), list) else []
        latest_typescript_attempt = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
        execution_history = script_artifacts.get('typescript_execution_history') if isinstance(script_artifacts.get('typescript_execution_history'), list) else []
        flow_steps = [step for step in (flow_execution.get('steps') or []) if isinstance(step, dict)]
        failed_flow_steps = [
            {
                'step_sort': step.get('step_sort'),
                'operation': step.get('operation'),
                'page_key': step.get('page_key') or '',
                'element_key': step.get('element_key') or '',
                'target_name': step.get('target_name') or '',
                'description': step.get('description') or '',
                'message': step.get('message') or '',
                'failure_category': step.get('failure_category') or step.get('failure_category_before_repair') or '',
                'locator': step.get('locator') if isinstance(step.get('locator'), dict) else {},
                'current_url': step.get('current_url') or '',
                'runtime_state': step.get('runtime_state') if isinstance(step.get('runtime_state'), dict) else {},
            }
            for step in flow_steps
            if step.get('status') == 'failed'
        ]
        verification_summary = {
            'status': verification.get('status'),
            'checked_url': verification.get('checked_url'),
            'checked_page_key': verification.get('checked_page_key'),
            'failed_items': verification.get('failed_items') or [],
            'failed_elements': verification.get('failed_elements') or [],
            'review_queue': verification.get('review_queue') or [],
            'plan_validation': verification.get('plan_validation') or {},
            'failure_reobserve': verification.get('failure_reobserve') or {},
            'typescript_execution': {
                'status': typescript_execution.get('status'),
                'history': [
                    {
                        'status': item.get('status'),
                        'final_spec_hash': item.get('final_spec_hash'),
                        'failure_category': item.get('failure_category') or (item.get('failure_classification') or {}).get('category'),
                    }
                    for item in execution_history[-4:]
                    if isinstance(item, dict)
                ],
                'failure_classification': latest_typescript_attempt.get('failure_classification') or {},
                'failed_spec_step_marker': latest_typescript_attempt.get('failed_spec_step_marker') or {},
                'stdout': latest_typescript_attempt.get('stdout') or '',
                'stderr': latest_typescript_attempt.get('stderr') or '',
                'network_summary': latest_typescript_attempt.get('network_summary') or {},
            },
            'flow_execution': {
                'status': flow_execution.get('status'),
                'total_steps': flow_execution.get('total_steps'),
                'passed_steps': flow_execution.get('passed_steps'),
                'failed_steps': flow_execution.get('failed_steps'),
                'failed_step_records': failed_flow_steps[:5],
                'notes': flow_execution.get('notes') or [],
            },
        }
        compact_payload = self._compact_llm_payload_value({
            'mcp_observations': (result.get('mcp_observations') or [])[-12:],
            'verification_result': verification_summary,
            'repair_history': (result.get('repair_history') or [])[-12:],
            'generated_case': result.get('generated_case') or {},
        })
        compact_payload['element_map'] = result.get('element_map') or {}
        return compact_payload

    @staticmethod
    def _promote_failure_reobserve_into_element_map(
        result: dict,
        failure_reobserve: dict[str, Any],
    ) -> None:
        """让完整重规划可以合法绑定失败位置的最新页面元素。"""
        page_state = (
            failure_reobserve.get('page_state')
            if isinstance(failure_reobserve.get('page_state'), dict)
            else {}
        )
        if failure_reobserve.get('status') != 'success' or not page_state.get('elements'):
            return

        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        pages = [page for page in (element_map.get('pages') or []) if isinstance(page, dict)]
        runtime_page = {
            **page_state,
            'capture_source': 'failure_reobserve',
            'runtime_fresh': True,
        }
        runtime_page_key = str(runtime_page.get('page_key') or 'runtime_failure_state')
        runtime_url = str(runtime_page.get('url') or '')
        runtime_page['page_key'] = runtime_page_key

        # 运行时页面必须优先于同 URL 的探索快照。保留其他页面以便完整
        # 重放前置流程，但移除上一轮同 key 的运行时页面，避免状态堆积。
        retained_pages = [
            page for page in pages
            if not (
                str(page.get('page_key') or '') == runtime_page_key
                or (
                    page.get('runtime_fresh')
                    and runtime_url
                    and str(page.get('url') or '') == runtime_url
                )
            )
        ]
        element_map['pages'] = [runtime_page, *retained_pages]
        element_map['latest_runtime_page_key'] = runtime_page_key
        element_map['latest_runtime_url'] = runtime_url
        element_map['latest_runtime_capture_source'] = 'failure_reobserve'
        result['element_map'] = element_map
    
    async def _encode_screenshot_base64(self, file_path: str) -> Optional[str]:
        """将截图文件编码为 Base64 数据 URL"""
        import os
        import base64
        
        # 处理相对路径
        if file_path.startswith('./'):
            file_path = os.path.abspath(file_path)
        
        if not file_path or not os.path.exists(file_path):
            logger.warning(f"截图文件不存在: {file_path}")
            return None
        
        try:
            with open(file_path, 'rb') as f:
                data = base64.b64encode(f.read()).decode('utf-8')
            # 返回 data URL 格式
            return f"data:image/png;base64,{data}"
        except Exception as e:
            logger.warning(f"截图编码失败: {e}")
            return None
    
    async def _process_result_screenshots(self, result: CaseResultModel) -> CaseResultModel:
        """处理结果中的截图，转为 Base64 数据 URL，并清理本地文件"""
        import os
        for step in result.steps:
            if step.screenshot:
                local_path = step.screenshot
                base64_url = await self._encode_screenshot_base64(local_path)
                if base64_url:
                    step.screenshot = base64_url
                    # 清理本地截图文件
                    try:
                        abs_path = os.path.abspath(local_path) if local_path.startswith('./') else local_path
                        if os.path.exists(abs_path):
                            os.remove(abs_path)
                            logger.debug(f"已清理本地截图: {abs_path}")
                    except Exception as e:
                        logger.warning(f"清理截图失败: {e}")
                else:
                    step.screenshot = None
        return result
    
    async def _upload_trace_file(self, trace_path: str) -> Optional[str]:
        """上传 Trace 文件到服务器，成功后清理本地文件

        Returns:
            服务器返回的相对路径（用于存储到数据库）
        """
        import os

        if not trace_path or not os.path.exists(trace_path):
            logger.warning(f"Trace 文件不存在: {trace_path}")
            return None

        token = await self._get_api_token()
        if not token:
            logger.error("无法获取 API Token，跳过 Trace 上传")
            return None

        url = f"{self.api_base_url}/api/ui-automation/traces/upload/"
        try:
            async with httpx.AsyncClient() as client:
                with open(trace_path, 'rb') as f:
                    files = {'file': (os.path.basename(trace_path), f, 'application/zip')}
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        files=files,
                        timeout=60.0  # Trace 文件可能较大
                    )
                    if response.status_code == 201:
                        resp_data = response.json()
                        # 响应被中间件包装，path 在 data 字段中
                        inner_data = resp_data.get('data', resp_data)
                        server_path = inner_data.get('path')
                        logger.info(f"Trace 上传成功: {server_path}")
                        # 清理本地 Trace 文件
                        try:
                            os.remove(trace_path)
                            logger.debug(f"已清理本地 Trace: {trace_path}")
                        except Exception as e:
                            logger.warning(f"清理 Trace 失败: {e}")
                        return server_path
                    else:
                        logger.error(f"Trace 上传失败: {response.status_code}")
                        return None
        except Exception as e:
            logger.error(f"Trace 上传异常: {e}")
            return None

    @staticmethod
    def _is_local_artifact_path(path_value: str | None) -> bool:
        """Return true only for paths that still refer to files on the actuator host."""
        if not path_value:
            return False
        value = str(path_value)
        if value.startswith(('http://', 'https://', '/media/')):
            return False
        return os.path.exists(value)

    @staticmethod
    def _build_ai_artifact_step_results(
        generated_steps: list[dict[str, Any]],
        succeeded: bool,
        message: str,
        duration: float,
    ) -> list[StepResultModel]:
        """Convert generated-case contract steps into execution-record step rows."""
        if not generated_steps:
            return [
                StepResultModel(
                    step_id=0,
                    status='success' if succeeded else 'failed',
                    message=message,
                    description='AI TypeScript spec 执行合同',
                    duration=duration,
                    element_found=succeeded,
                )
            ]

        step_duration = duration / max(1, len(generated_steps))
        results: list[StepResultModel] = []
        for index, raw_step in enumerate(generated_steps, start=1):
            if not isinstance(raw_step, dict):
                continue
            description = (
                raw_step.get('description')
                or raw_step.get('target_name')
                or raw_step.get('target')
                or raw_step.get('action_id')
                or raw_step.get('operation')
                or f'AI step {index}'
            )
            results.append(
                StepResultModel(
                    step_id=index,
                    status='success' if succeeded else 'failed',
                    message=message,
                    description=str(description),
                    duration=step_duration,
                    element_found=succeeded,
                )
            )
        return results or [
            StepResultModel(
                step_id=0,
                status='success' if succeeded else 'failed',
                message=message,
                description='AI TypeScript spec 执行合同',
                duration=duration,
                element_found=succeeded,
            )
        ]

    async def _upload_ai_artifact_file(self, file_path: str, artifact_type: str = 'artifact') -> Optional[dict[str, Any]]:
        """上传 AI 生成任务执行产物，返回服务端 URL/path。"""
        if not file_path or not os.path.exists(file_path):
            return None

        token = await self._get_api_token()
        if not token:
            logger.error("无法获取 API Token，跳过 AI 产物上传")
            return None

        content_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
        url = f"{self.api_base_url}/api/ui-automation/artifacts/upload/"
        try:
            async with httpx.AsyncClient() as client:
                with open(file_path, 'rb') as f:
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        data={'type': artifact_type},
                        files={'file': (os.path.basename(file_path), f, content_type)},
                        timeout=120.0,
                    )
            if response.status_code == 201:
                resp_data = response.json()
                inner_data = resp_data.get('data', resp_data)
                return {
                    'type': artifact_type,
                    'name': os.path.basename(file_path),
                    'path': inner_data.get('path') or '',
                    'url': inner_data.get('url') or '',
                    'content_type': content_type,
                    'size': os.path.getsize(file_path),
                }
            logger.error(f"AI 产物上传失败: {response.status_code} - {response.text[:300]}")
            return None
        except Exception as e:
            logger.error(f"AI 产物上传异常: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _extract_trace_network_summary(trace_path: str) -> dict[str, Any]:
        """从 Playwright trace.zip 尽力抽取网络摘要，避免报告只给一个 trace 文件。"""
        summary = {
            'total': 0,
            'by_method': {},
            'by_status': {},
            'failed': [],
            'samples': [],
        }
        if not trace_path or not os.path.exists(trace_path):
            return summary

        pending: dict[str, dict[str, Any]] = {}
        try:
            with zipfile.ZipFile(trace_path, 'r') as zf:
                for name in zf.namelist():
                    if not name.endswith('.network'):
                        continue
                    content = zf.read(name).decode('utf-8', errors='replace')
                    for line in content.splitlines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        snapshot = event.get('snapshot') if isinstance(event.get('snapshot'), dict) else {}
                        event_type = event.get('type') or event.get('method') or ''
                        request_id = str(
                            event.get('requestId')
                            or event.get('request_id')
                            or event.get('id')
                            or snapshot.get('_requestId')
                            or snapshot.get('requestId')
                            or ''
                        )
                        request = event.get('request') if isinstance(event.get('request'), dict) else {}
                        response = event.get('response') if isinstance(event.get('response'), dict) else {}
                        if isinstance(snapshot.get('request'), dict):
                            request = snapshot['request']
                        if isinstance(snapshot.get('response'), dict):
                            response = snapshot['response']
                        url = event.get('url') or request.get('url') or response.get('url') or ''
                        method = event.get('method') or request.get('method') or ''
                        status = event.get('status') or response.get('status') or 0
                        if request_id:
                            item = pending.setdefault(request_id, {})
                        else:
                            item = {}
                        if url:
                            item['url'] = url
                        if method and event_type != 'method':
                            item['method'] = method
                        if status:
                            item['status'] = int(status)
                        if event.get('resourceType'):
                            item['resource_type'] = event.get('resourceType')
                        elif snapshot.get('_resourceType'):
                            item['resource_type'] = snapshot.get('_resourceType')
                        response_content = (
                            response.get('content') if isinstance(response.get('content'), dict) else {}
                        )
                        body_sha1 = str(response_content.get('_sha1') or '')
                        mime_type = str(response_content.get('mimeType') or '')
                        if body_sha1 and str(method or '').upper() != 'GET' and (
                            'json' in mime_type or mime_type.startswith('text/')
                        ):
                            try:
                                body = zf.read(f'resources/{body_sha1}').decode('utf-8', errors='replace')
                                item['response_body'] = body[:2000]
                            except KeyError:
                                pass
                        if not request_id and item:
                            pending[f"inline_{len(pending)}"] = item
        except Exception as exc:
            summary['error'] = f"{type(exc).__name__}: {str(exc)[:300]}"
            return summary

        requests = [
            item for item in pending.values()
            if item.get('url') or item.get('method') or item.get('status')
        ]
        summary['total'] = len(requests)
        for item in requests:
            method = str(item.get('method') or 'UNKNOWN').upper()
            status = int(item.get('status') or 0)
            summary['by_method'][method] = summary['by_method'].get(method, 0) + 1
            status_key = f"{status // 100}xx" if status else 'unknown'
            summary['by_status'][status_key] = summary['by_status'].get(status_key, 0) + 1
            sample = {
                'method': method,
                'url': str(item.get('url') or '')[:500],
                'status': status,
                'resource_type': item.get('resource_type') or '',
            }
            if item.get('response_body'):
                sample['response_body'] = item['response_body']
            if status >= 400 or status == 0:
                summary['failed'].append(sample)
            if len(summary['samples']) < 30:
                summary['samples'].append(sample)
        summary['failed'] = summary['failed'][:50]
        return summary

    async def _collect_typescript_runtime_artifacts(
        self,
        output_dir: Path,
        trace_files: list[Path],
        uploaded_trace: str | None,
        network_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """收集并上传 Playwright Test outputDir 中的运行证据。"""
        screenshots = sorted(
            [item for item in output_dir.rglob('*') if item.suffix.lower() in {'.png', '.jpg', '.jpeg'}],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        videos = sorted(
            [item for item in output_dir.rglob('*') if item.suffix.lower() in {'.webm', '.mp4'}],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        attachments = sorted(
            [
                item for item in output_dir.rglob('*')
                if item.is_file() and item.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webm', '.mp4', '.zip'}
            ],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        uploaded_screenshots = []
        for screenshot in screenshots[:8]:
            uploaded = await self._upload_ai_artifact_file(str(screenshot), 'screenshot')
            if uploaded:
                uploaded_screenshots.append(uploaded)
        uploaded_videos = []
        for video in videos[:4]:
            uploaded = await self._upload_ai_artifact_file(str(video), 'video')
            if uploaded:
                uploaded_videos.append(uploaded)
        return {
            'trace': {
                'path': uploaded_trace or (str(trace_files[0]) if trace_files else ''),
                'file_count': len(trace_files),
            },
            'screenshots': uploaded_screenshots,
            'videos': uploaded_videos,
            'attachments': [
                {
                    'name': item.name,
                    'path': str(item),
                    'size': item.stat().st_size,
                    'content_type': mimetypes.guess_type(str(item))[0] or 'application/octet-stream',
                }
                for item in attachments[:20]
            ],
            'network_summary': network_summary or {},
            'counts': {
                'screenshots': len(screenshots),
                'videos': len(videos),
                'attachments': len(attachments),
            },
        }

    @staticmethod
    def _promote_typescript_execution_trace(verification: dict, trace_path: str | None) -> None:
        """Make the real spec trace primary while retaining exploration evidence."""
        if not trace_path:
            return
        previous = str(verification.get('trace_path') or '')
        if previous and previous != trace_path:
            verification['exploration_trace_path'] = previous
        verification['trace_path'] = trace_path

    async def handle_message(self, socket_data: SocketDataModel):
        """处理接收到的消息"""
        if socket_data.code != ResponseCode.SUCCESS:
            logger.warning(f"收到错误消息: {socket_data.msg}")
            return
        
        if not socket_data.data:
            logger.debug(f"收到通知消息: {socket_data.msg}")
            return
        
        # 记录发起用户
        self._current_user = socket_data.user
        
        # 添加到任务队列
        await self.add_task(socket_data.data, source='websocket')
    
    def _ai_generation_task_id(self, task: QueueModel) -> Optional[int]:
        if task.func_name != UiSocketEnum.AI_GENERATION:
            return None
        try:
            return int(task.func_args.get('task_id'))
        except (TypeError, ValueError, AttributeError):
            return None

    async def _send_ai_generation_ack(self, args: dict, source: str, status: str = 'queued'):
        task_id = args.get('task_id')
        if not task_id:
            return
        await self.ws_client.send_result(
            UiSocketEnum.AI_GENERATION_ACK,
            {
                'task_id': task_id,
                'dispatch_id': args.get('dispatch_id') or '',
                'actuator_id': self.ws_client.actuator_id,
                'status': status,
                'source': source,
                'queued_at': time.time(),
            },
            self._current_user,
        )

    async def _submit_ai_generation_result(self, result: dict) -> bool:
        """Persist full AI generation results without depending on a huge WebSocket frame."""
        task_id = result.get('task_id')
        if task_id:
            saved = await self._api_post(
                f"/api/ui-automation/ai-generation-tasks/{task_id}/report-result/",
                result,
                timeout=90.0,
            )
            if saved is not None:
                logger.info(f"AI UI 生成任务结果已通过 HTTP 上报: task_id={task_id}")
                return True
            logger.warning(f"AI UI 生成任务 HTTP 结果上报失败，降级 WebSocket: task_id={task_id}")
        sent = await self.ws_client.send_result(
            UiSocketEnum.AI_GENERATION_RESULT,
            result,
            self._current_user,
        )
        return bool(sent)

    async def add_task(self, task: QueueModel, source: str = 'websocket') -> bool:
        """添加任务到队列"""
        ai_task_id = self._ai_generation_task_id(task)
        if ai_task_id is not None:
            if ai_task_id in self._queued_ai_generation_task_ids or ai_task_id in self._active_ai_generation_task_ids:
                logger.info(f"AI 生成任务已在队列或执行中，跳过重复入队: task_id={ai_task_id}, source={source}")
                await self._send_ai_generation_ack(task.func_args, source, status='duplicate')
                return False
            self._queued_ai_generation_task_ids.add(ai_task_id)

        await self.task_queue.put(task)
        logger.info(f"任务已入队: {task.func_name}")
        if ai_task_id is not None:
            await self._send_ai_generation_ack(task.func_args, source, status='queued')
        return True
    
    async def process_tasks(self):
        """处理任务队列"""
        while not self._stop_event.is_set():
            try:
                # 等待任务
                task = await asyncio.wait_for(
                    self.task_queue.get(),
                    timeout=1.0
                )
                
                ai_task_id = self._ai_generation_task_id(task)
                if ai_task_id is not None:
                    self._queued_ai_generation_task_ids.discard(ai_task_id)
                    self._active_ai_generation_task_ids.add(ai_task_id)

                try:
                    # 路由任务到对应处理器
                    await self._route_task(task)
                finally:
                    if ai_task_id is not None:
                        self._active_ai_generation_task_ids.discard(ai_task_id)
                    self.task_queue.task_done()
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"处理任务错误: {e}", exc_info=True)

    async def poll_pending_ai_generation_tasks(self):
        """轮询后端补偿拉取 AI 生成任务，防止 WebSocket 重连时任务丢失。"""
        actuator_id = self.ws_client.actuator_id
        if not actuator_id:
            logger.warning("执行器 ID 为空，跳过 AI 生成任务补偿轮询")
            return

        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self._pending_ai_poll_interval)
                if not self.ws_client.connected:
                    continue

                active_task_ids = ','.join(
                    str(task_id) for task_id in sorted(self._active_ai_generation_task_ids)
                )
                active_query = (
                    f"&active_task_ids={quote(active_task_ids, safe=',')}"
                    if active_task_ids else ''
                )
                result = await self._api_get(
                    f"/api/ui-automation/ai-generation-tasks/pending-for-actuator/?actuator_id={quote(actuator_id, safe='')}"
                    f"{active_query}"
                )
                if not isinstance(result, dict):
                    continue

                tasks = result.get('tasks') or []
                if not isinstance(tasks, list) or not tasks:
                    continue

                logger.info(f"拉取到 {len(tasks)} 个待补偿 AI 生成任务")
                for item in tasks:
                    if not isinstance(item, dict):
                        continue
                    func_args = item.get('func_args') or {}
                    if not isinstance(func_args, dict):
                        continue
                    queue_task = QueueModel(
                        func_name=item.get('func_name') or UiSocketEnum.AI_GENERATION,
                        func_args=func_args,
                    )
                    await self.add_task(queue_task, source='pull')
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"补偿拉取 AI 生成任务失败: {type(e).__name__}: {e}")
    
    async def _route_task(self, task: QueueModel):
        """路由任务到对应处理器"""
        handlers = {
            UiSocketEnum.PAGE_STEPS: self.execute_page_steps,
            UiSocketEnum.TEST_CASE: self.execute_test_case,
            UiSocketEnum.TEST_CASE_BATCH: self.execute_batch,
            UiSocketEnum.AI_GENERATION: self.execute_ai_generation,
            UiSocketEnum.ELEMENT_MAP_MANUAL_CAPTURE: self.execute_element_map_manual_capture,
            UiSocketEnum.STOP_EXECUTION: self.stop_execution,
        }
        
        handler = handlers.get(task.func_name)
        if handler:
            await handler(task.func_args)
        else:
            logger.warning(f"未知任务类型: {task.func_name}")

    async def execute_element_map_manual_capture(self, args: dict):
        """执行人工元素地图采集，并通过 HTTP 回填到后端元素地图记录。"""
        element_map_id = args.get('element_map_id')
        dispatch_key = self._manual_capture_dispatch_key(args)
        logger.info(f"开始人工采集元素地图: element_map_id={element_map_id}")
        if not element_map_id:
            logger.error("人工采集元素地图缺少 element_map_id")
            return
        if dispatch_key in self._manual_capture_inflight_keys:
            logger.info(f"人工采集元素地图重复入队，跳过 in-flight 任务: {dispatch_key}")
            return
        cached_result = self._manual_capture_completed_results.get(dispatch_key)
        if cached_result:
            logger.info(f"人工采集元素地图重复入队，重放已完成结果: {dispatch_key}")
            await self._api_post(
                f"/api/ui-automation/element-maps/{element_map_id}/manual-capture-result/",
                cached_result,
                timeout=90.0,
            )
            return
        self._manual_capture_inflight_keys.add(dispatch_key)
        try:
            result = await self.executor.run_manual_element_map_capture(args)
        except Exception as exc:
            logger.error(f"人工采集元素地图执行异常: element_map_id={element_map_id}, error={exc}", exc_info=True)
            result = {
                'element_map_id': element_map_id,
                'dispatch_id': args.get('dispatch_id') or '',
                'status': 'failed',
                'message': f"{type(exc).__name__}: {exc}",
                'failure_category': 'unknown',
            }
        try:
            self._manual_capture_completed_results[dispatch_key] = result
            if len(self._manual_capture_completed_results) > 20:
                oldest_key = next(iter(self._manual_capture_completed_results))
                if oldest_key != dispatch_key:
                    self._manual_capture_completed_results.pop(oldest_key, None)

            saved = await self._api_post(
                f"/api/ui-automation/element-maps/{element_map_id}/manual-capture-result/",
                result,
                timeout=90.0,
            )
            if saved is None:
                logger.error(f"人工采集元素地图结果上报失败: element_map_id={element_map_id}")
            logger.info(f"人工采集元素地图完成: element_map_id={element_map_id}, status={result.get('status')}")
        finally:
            self._manual_capture_inflight_keys.discard(dispatch_key)

    def _manual_capture_dispatch_key(self, args: dict) -> str:
        element_map_id = str(args.get('element_map_id') or '').strip()
        dispatch_id = str(args.get('dispatch_id') or '').strip()
        return f"{element_map_id}:{dispatch_id}" if dispatch_id else element_map_id
    
    async def execute_page_steps(self, args: dict):
        """执行页面步骤"""
        page_step_id = args.get('page_step_id')
        env_config_id = args.get('env_config_id')
        
        if not page_step_id:
            logger.error("缺少page_step_id参数")
            return
        
        # 从API获取页面步骤详情
        page_step_data = await self._fetch_page_step(page_step_id)
        if not page_step_data:
            return
        
        # 初始化数据处理器，加载项目公共变量
        project_id = page_step_data.get('project')
        data_processor = await self._init_data_processor(project_id)
        
        # 获取环境配置
        env_config = None
        base_url = ''
        if env_config_id:
            env_config = await self._fetch_env_config(env_config_id)
        else:
            # 尝试获取项目的默认环境配置
            if project_id:
                env_config = await self._fetch_default_env_config(project_id)
        
        if env_config:
            base_url = env_config.get('base_url', '') or ''
            logger.info(f"使用环境配置: {env_config.get('name')}, base_url: {base_url}")
        
        # 构建配置，传入 base_url 和数据处理器
        config = self._build_page_step_config(page_step_data, base_url, data_processor, env_config)
        
        # 执行（使用同一浏览器会话）
        logger.info(f"开始执行页面步骤: {config.page_name}")
        
        start_time = time.time()
        step_results = await self.executor.execute_page_step(config)
        
        # 统计结果
        passed_steps = sum(1 for r in step_results if r.status == 'success')
        failed_steps = len(step_results) - passed_steps
        
        # 处理截图为 Base64 并发送步骤结果
        import os
        for result in step_results:
            if result.screenshot:
                local_path = result.screenshot
                base64_url = await self._encode_screenshot_base64(local_path)
                if base64_url:
                    result.screenshot = base64_url
                    # 清理本地截图文件
                    try:
                        abs_path = os.path.abspath(local_path) if local_path.startswith('./') else local_path
                        if os.path.exists(abs_path):
                            os.remove(abs_path)
                            logger.debug(f"已清理本地截图: {abs_path}")
                    except Exception as e:
                        logger.warning(f"清理截图失败: {e}")

            # 发送步骤结果
            await self.ws_client.send_result(
                UiSocketEnum.STEP_RESULT,
                result.model_dump(),
                self._current_user
            )
        
        # 发送页面步骤执行汇总结果
        summary_result = {
            'page_step_id': page_step_id,
            'status': 'success' if failed_steps == 0 else 'failed',
            'message': '执行成功' if failed_steps == 0 else '执行失败',
            'total_steps': len(config.steps),
            'passed_steps': passed_steps,
            'failed_steps': failed_steps,
            'duration': time.time() - start_time,
            'steps': [r.model_dump() for r in step_results],
        }
        
        await self.ws_client.send_result(
            'u_page_step_result',  # 新增的结果类型
            summary_result,
            self._current_user
        )
        
        logger.info("页面步骤执行完成")
    
    async def execute_test_case(self, args: dict):
        """执行测试用例"""
        case_id = args.get('case_id')
        env_config_id = args.get('env_config_id')
        batch_id = args.get('batch_id')
        executor_id = args.get('executor_id')
        executor_name = args.get('executor_name')
        execution_record_id = args.get('execution_record_id')
        
        if not case_id:
            logger.error("缺少case_id参数")
            return

        # 从API获取用例详情
        case_data = await self._fetch_test_case(case_id)
        if not case_data:
            return

        # 获取环境配置
        env_config = None
        project_id = case_data.get('project')
        if env_config_id:
            env_config = await self._fetch_env_config(env_config_id)
        else:
            # 尝试获取项目的默认环境配置
            if project_id:
                env_config = await self._fetch_default_env_config(project_id)

        # 日志：确认环境配置
        if env_config:
            logger.info(f"环境配置已获取: name={env_config.get('name')}, base_url={env_config.get('base_url')}")
        else:
            logger.warning(f"未获取到环境配置 (env_config_id={env_config_id}, project_id={project_id})")

        # 初始化数据处理器，加载项目公共变量
        data_processor = await self._init_data_processor(project_id)

        # 构建配置（传入数据处理器进行变量替换）
        config = self._build_test_case_config(case_data, env_config, data_processor)

        # 执行
        logger.info(f"开始执行用例: {config.case_name}")
        if self._has_case_ai_execution_artifact(config):
            result = await self._execute_ai_artifact_test_case(config)
        else:
            result = await self.executor.execute_test_case(config)

        # 上传截图并替换路径
        result = await self._process_result_screenshots(result)

        # 上传 Trace 文件并替换路径。AI TypeScript artifact runner 可能已经
        # 返回服务端相对路径，此时不能再当成本地文件二次上传后清空。
        if result.trace_path and self._is_local_artifact_path(result.trace_path):
            server_trace_path = await self._upload_trace_file(result.trace_path)
            if server_trace_path:
                result.trace_path = server_trace_path
            else:
                result.trace_path = None  # 上传失败则清空

        # 发送用例结果（包含 batch_id 和执行人信息）
        result_data = result.model_dump()
        if batch_id:
            result_data['batch_id'] = batch_id
        # 添加执行人信息
        if executor_id:
            result_data['executor_id'] = executor_id
        if executor_name:
            result_data['executor_name'] = executor_name
        if execution_record_id:
            result_data['execution_record_id'] = execution_record_id
            
        await self.ws_client.send_result(
            UiSocketEnum.CASE_RESULT,
            result_data,
            self._current_user
        )

        logger.info(f"用例执行完成: {result.status}")

    @staticmethod
    def _extract_case_ai_execution_artifact(data: dict) -> dict[str, Any]:
        """Return the persisted AI execution artifact for an applied UI case."""
        if not isinstance(data, dict):
            return {}
        direct_artifact = data.get('ai_execution_artifact')
        if isinstance(direct_artifact, dict):
            artifact = direct_artifact
        else:
            result_data = data.get('result_data') if isinstance(data.get('result_data'), dict) else {}
            artifact = result_data.get('ai_execution_artifact') if isinstance(result_data.get('ai_execution_artifact'), dict) else {}
        if not artifact:
            return {}

        script_artifacts = artifact.get('script_artifacts') if isinstance(artifact.get('script_artifacts'), dict) else {}
        generated_case = artifact.get('generated_case') if isinstance(artifact.get('generated_case'), dict) else {}
        ts_spec = (
            artifact.get('playwright_ts_spec')
            or script_artifacts.get('playwright_ts_spec')
            or generated_case.get('playwright_ts_spec')
            or ''
        )
        ts_files = (
            artifact.get('playwright_ts_files')
            if isinstance(artifact.get('playwright_ts_files'), dict) else
            script_artifacts.get('playwright_ts_files')
            if isinstance(script_artifacts.get('playwright_ts_files'), dict) else
            generated_case.get('playwright_ts_files')
            if isinstance(generated_case.get('playwright_ts_files'), dict) else {}
        )
        if not str(ts_spec or '').strip() and not str(ts_files.get('generated.spec.ts') or '').strip():
            return {}
        return artifact

    @staticmethod
    def _has_case_ai_execution_artifact(config: TestCaseConfig) -> bool:
        artifact = config.ai_execution_artifact if isinstance(config.ai_execution_artifact, dict) else {}
        return bool(TaskConsumer._extract_case_ai_execution_artifact({'ai_execution_artifact': artifact}))

    async def _execute_ai_artifact_test_case(self, config: TestCaseConfig) -> CaseResultModel:
        """Execute an AI-applied UI case with the same TypeScript artifact runner used by generation."""
        artifact = self._extract_case_ai_execution_artifact({'ai_execution_artifact': config.ai_execution_artifact})
        script_artifacts = copy.deepcopy(
            artifact.get('script_artifacts') if isinstance(artifact.get('script_artifacts'), dict) else {}
        )
        generated_case = copy.deepcopy(
            artifact.get('generated_case') if isinstance(artifact.get('generated_case'), dict) else {}
        )
        verification = copy.deepcopy(
            artifact.get('verification_result') if isinstance(artifact.get('verification_result'), dict) else {}
        )
        ts_spec = (
            artifact.get('playwright_ts_spec')
            or script_artifacts.get('playwright_ts_spec')
            or generated_case.get('playwright_ts_spec')
            or ''
        )
        ts_files = (
            artifact.get('playwright_ts_files')
            if isinstance(artifact.get('playwright_ts_files'), dict) else
            script_artifacts.get('playwright_ts_files')
            if isinstance(script_artifacts.get('playwright_ts_files'), dict) else
            generated_case.get('playwright_ts_files')
            if isinstance(generated_case.get('playwright_ts_files'), dict) else {}
        )
        if ts_spec and 'generated.spec.ts' not in ts_files:
            ts_files = {**ts_files, 'generated.spec.ts': ts_spec}
        if ts_files and not ts_spec:
            ts_spec = str(ts_files.get('generated.spec.ts') or '')

        generated_case.update({
            'playwright_ts_spec': ts_spec,
            'playwright_ts_files': ts_files,
        })
        script_artifacts.update({
            'playwright_ts_spec': ts_spec,
            'playwright_ts_files': ts_files,
        })
        verification['script_artifacts'] = script_artifacts
        # Manual UI case execution is a replay of the applied artifact. It should not
        # silently rewrite the saved case with a new repair patch.
        result = {
            'task_id': artifact.get('source_task_id') or f'ui_case_{config.case_id}',
            'status': 'success',
            'generated_case': generated_case,
            'verification_result': verification,
            'safety_policy': {'max_repair_rounds': 0},
            'max_repair_rounds': 0,
        }
        logger.info(
            "用例 %s 使用 AI TypeScript artifact 执行: source_task_id=%s",
            config.case_id,
            artifact.get('source_task_id') or '',
        )
        started = time.time()
        await self._run_typescript_spec_artifact(result)
        verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
        script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
        execution = script_artifacts.get('typescript_spec_execution') if isinstance(script_artifacts.get('typescript_spec_execution'), dict) else {}
        steps = generated_case.get('steps') if isinstance(generated_case.get('steps'), list) else []
        total_steps = max(1, len(steps))
        succeeded = result.get('status') == 'success' and execution.get('status') == 'success'
        message = result.get('message') or execution.get('reason') or (
            'AI TypeScript spec 执行成功' if succeeded else 'AI TypeScript spec 执行失败'
        )
        duration = float(execution.get('duration') or (time.time() - started))
        step_results = self._build_ai_artifact_step_results(
            [step for step in steps if isinstance(step, dict)],
            succeeded,
            message,
            duration,
        )
        return CaseResultModel(
            case_id=config.case_id,
            status='success' if succeeded else 'failed',
            message=message,
            total_steps=total_steps,
            passed_steps=total_steps if succeeded else 0,
            failed_steps=0 if succeeded else 1,
            duration=duration,
            steps=step_results,
            trace_path=execution.get('trace_path') or verification.get('trace_path') or None,
        )
    
    async def execute_batch(self, args: dict):
        """批量执行用例（支持并发）"""
        case_ids = args.get('case_ids', [])
        env_config_id = args.get('env_config_id')
        batch_id = args.get('batch_id')
        executor_id = args.get('executor_id')
        executor_name = args.get('executor_name')
        # 从配置获取并发数
        max_concurrent = getattr(self.config, 'max_concurrent', 3) if self.config else 3
        if not case_ids:
            logger.error("缺少case_ids参数")
            return

        logger.info(f"开始批量执行 {len(case_ids)} 个用例, 并发数: {max_concurrent}")

        # 预先获取所有用例数据并构建配置
        configs = []
        config_batch_map = {}  # case_id -> batch_id 映射

        for case_id in case_ids:
            if self._stop_event.is_set():
                logger.info("批量执行准备阶段被停止")
                return

            case_data = await self._fetch_test_case(case_id)
            if not case_data:
                logger.warning(f"用例 {case_id} 数据获取失败，跳过")
                continue

            # 获取环境配置
            env_config = None
            project_id = case_data.get('project')
            if env_config_id:
                env_config = await self._fetch_env_config(env_config_id)
            elif project_id:
                env_config = await self._fetch_default_env_config(project_id)

            # 初始化数据处理器
            data_processor = await self._init_data_processor(project_id)

            # 构建配置
            config = self._build_test_case_config(case_data, env_config, data_processor)
            configs.append(config)
            config_batch_map[config.case_id] = batch_id

        if not configs:
            logger.warning("没有可执行的用例")
            return

        # 定义结果回调 - 每个用例完成后立即发送结果
        async def on_result(result):
            # 上传截图
            result = await self._process_result_screenshots(result)

            # 上传 Trace
            if result.trace_path and self._is_local_artifact_path(result.trace_path):
                server_trace_path = await self._upload_trace_file(result.trace_path)
                result.trace_path = server_trace_path if server_trace_path else None

            # 发送结果
            result_data = result.model_dump()
            if batch_id:
                result_data['batch_id'] = batch_id
            # 添加执行人信息
            if executor_id:
                result_data['executor_id'] = executor_id
            if executor_name:
                result_data['executor_name'] = executor_name
            await self.ws_client.send_result(
                UiSocketEnum.CASE_RESULT,
                result_data,
                self._current_user
            )
            logger.info(f"用例 {result.case_id} 执行完成: {result.status}")

        if any(self._has_case_ai_execution_artifact(config) for config in configs):
            semaphore = asyncio.Semaphore(max_concurrent)

            async def run_config(config: TestCaseConfig):
                async with semaphore:
                    if self._has_case_ai_execution_artifact(config):
                        result = await self._execute_ai_artifact_test_case(config)
                    else:
                        result = await self.executor.execute_test_case(config)
                    await on_result(result)

            await asyncio.gather(*(run_config(config) for config in configs))
            logger.info("批量执行完成")
            return

        # 并发执行
        await self.executor.execute_batch_concurrent(
            configs,
            max_concurrent=max_concurrent,
            on_result=on_result
        )

        logger.info("批量执行完成")

    async def _process_ai_generation_artifacts(self, args: dict, result: dict) -> dict:
        """处理 AI 生成任务中的截图和 Trace 产物，避免回传执行器本地路径。"""
        element_map = result.get('element_map') or {}
        if isinstance(element_map, dict):
            for page in element_map.get('pages') or []:
                if not isinstance(page, dict):
                    continue
                screenshot = page.get('screenshot')
                if screenshot and not str(screenshot).startswith('data:image/'):
                    encoded = await self._encode_screenshot_base64(screenshot)
                    if encoded:
                        page['screenshot'] = encoded

        verification = result.get('verification_result') or {}
        flow_execution = verification.get('flow_execution') if isinstance(verification, dict) else {}
        if isinstance(flow_execution, dict):
            for step in flow_execution.get('steps') or []:
                if not isinstance(step, dict):
                    continue
                screenshot = step.get('screenshot')
                if screenshot and not str(screenshot).startswith('data:image/'):
                    encoded = await self._encode_screenshot_base64(screenshot)
                    if encoded:
                        step['screenshot'] = encoded
        trace_path = verification.get('trace_path') if isinstance(verification, dict) else None
        if trace_path and os.path.exists(str(trace_path)):
            server_trace_path = await self._upload_trace_file(trace_path)
            verification['trace_path'] = server_trace_path if server_trace_path else None
            if isinstance(flow_execution, dict) and flow_execution.get('trace_path') == trace_path:
                flow_execution['trace_path'] = verification['trace_path']
            result['verification_result'] = verification
        if isinstance(args.get('safety_policy'), dict):
            result['safety_policy'] = args['safety_policy']
        if args.get('max_repair_rounds') is not None:
            result['max_repair_rounds'] = args.get('max_repair_rounds')
        await self._run_typescript_spec_artifact(result)
        self._attach_ai_generation_report_artifacts(result)
        return result

    def _resolve_playwright_test_command(self) -> list[str] | None:
        if self.typescript_spec_command:
            return [self.typescript_spec_command]

        base_dir = Path(__file__).resolve().parent
        local_candidates = [
            base_dir / 'node_modules/.bin/playwright',
            base_dir.parent / 'node_modules/.bin/playwright',
            base_dir.parent / 'WHartTest_Vue/node_modules/.bin/playwright',
            Path.cwd() / 'node_modules/.bin/playwright',
        ]
        for candidate in local_candidates:
            if candidate.exists():
                return [str(candidate)]

        playwright_cli = shutil.which('playwright')
        if playwright_cli:
            return [playwright_cli]
        if self.typescript_spec_allow_npx and shutil.which('npx'):
            return ['npx', '--yes', 'playwright']
        return None

    @staticmethod
    def _playwright_workspace_for_command(command: list[str] | None, task_id: Any) -> Path:
        """选择 Node 可解析 Playwright 依赖的工作目录。"""
        safe_task_id = ''.join(ch for ch in str(task_id or int(time.time())) if ch.isalnum() or ch in {'-', '_'}) or 'task'
        if command:
            command_path = Path(command[0])
            if command_path.exists():
                for parent in command_path.resolve().parents:
                    if (parent / 'package.json').exists() and (parent / 'node_modules').exists():
                        return parent / '.ai_ts_specs' / safe_task_id
        return Path('./data/ai_ts_specs') / safe_task_id

    @staticmethod
    def _ts_locator_expression(locator: dict[str, Any]) -> str:
        locator_type = locator.get('type')
        value = str(locator.get('value') or '')
        name = str(locator.get('name') or '')
        if locator_type == 'mcp_ref':
            return f"page.getByText({json.dumps(name or value, ensure_ascii=False)}, {{ exact: true }})"
        if locator_type == 'test_id':
            return f"page.getByTestId({json.dumps(value, ensure_ascii=False)})"
        if locator_type == 'role':
            if name:
                return f"page.getByRole({json.dumps(value, ensure_ascii=False)}, {{ name: {json.dumps(name, ensure_ascii=False)} }})"
            return f"page.getByRole({json.dumps(value, ensure_ascii=False)})"
        if locator_type == 'label':
            return f"page.getByLabel({json.dumps(value, ensure_ascii=False)})"
        if locator_type == 'placeholder':
            return f"page.getByPlaceholder({json.dumps(value, ensure_ascii=False)})"
        if locator_type == 'text':
            return f"page.getByText({json.dumps(value, ensure_ascii=False)}, {{ exact: true }})"
        if locator_type == 'id':
            return f"page.locator({json.dumps('#' + value, ensure_ascii=False)})"
        if locator_type == 'name':
            selector = f'[name="{value}"]'
            return f"page.locator({json.dumps(selector, ensure_ascii=False)})"
        if locator_type == 'xpath':
            return f"page.locator({json.dumps('xpath=' + value, ensure_ascii=False)})"
        return f"page.locator({json.dumps(value, ensure_ascii=False)})"

    @staticmethod
    def _dedupe_locator_dicts(locators: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for locator in locators:
            if not isinstance(locator, dict) or not locator.get('type'):
                continue
            key = json.dumps(locator, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            result.append(locator)
        return result

    @staticmethod
    def _locator_type_weight(locator_type: str) -> int:
        return {
            'test_id': 45,
            'role': 38,
            'label': 34,
            'placeholder': 30,
            'text': 24,
            'id': 22,
            'name': 20,
            'css': 10,
            'xpath': 6,
            'mcp_ref': 36,
        }.get(locator_type, 8)

    @staticmethod
    def _normalized_terms(*values: Any) -> list[str]:
        terms = []
        for value in values:
            text = re.sub(r'\s+', ' ', str(value or '').strip().lower())
            if not text:
                continue
            terms.append(text)
            terms.extend(part for part in re.split(r'[\s:/：,，()（）_\-]+', text) if len(part) >= 2)
        deduped = []
        seen = set()
        for term in terms:
            if term in seen:
                continue
            seen.add(term)
            deduped.append(term)
        return deduped

    @classmethod
    def _score_locator_candidate(
        cls,
        candidate: dict[str, Any],
        original: dict[str, Any],
        step: dict[str, Any],
        element: dict[str, Any],
    ) -> dict[str, Any]:
        locator_type = str(candidate.get('type') or '')
        confidence = 0.0
        try:
            confidence = float(candidate.get('confidence') or 0)
        except (TypeError, ValueError):
            confidence = 0.0

        score = cls._locator_type_weight(locator_type) + int(max(0.0, min(confidence, 1.0)) * 35)
        reasons = [f'type_weight:{locator_type or "unknown"}']
        if confidence:
            reasons.append(f'confidence:{confidence:.2f}')
        if locator_type == str(original.get('type') or ''):
            score += 8
            reasons.append('same_type_as_failed_locator')

        target_terms = cls._normalized_terms(
            step.get('target_name'),
            step.get('element_key'),
            step.get('description'),
            original.get('name'),
            original.get('value'),
        )
        candidate_text = ' '.join(cls._normalized_terms(candidate.get('name'), candidate.get('value')))
        element_text = ' '.join(cls._normalized_terms(
            element.get('name'),
            element.get('accessible_name'),
            element.get('text'),
            element.get('label'),
            element.get('placeholder'),
            element.get('element_key'),
        ))
        haystack = f'{candidate_text} {element_text}'.strip()
        for term in target_terms[:20]:
            if not term or not haystack:
                continue
            if term == haystack or term in candidate_text:
                score += 28
                reasons.append(f'exact_candidate_match:{term[:40]}')
                break
            if term in haystack:
                score += 18
                reasons.append(f'element_text_match:{term[:40]}')
                break
            ratio = difflib.SequenceMatcher(None, term, candidate_text or haystack).ratio()
            if ratio >= 0.72:
                score += int(ratio * 15)
                reasons.append(f'fuzzy_match:{ratio:.2f}')
                break

        operation = str(step.get('operation') or '').lower()
        role_or_type = ' '.join(cls._normalized_terms(element.get('role'), element.get('tag'), element.get('input_type')))
        if operation == 'fill' and any(token in role_or_type for token in ['textbox', 'input', 'textarea', 'combobox']):
            score += 16
            reasons.append('operation_role_fit:fill')
        elif operation in {'click', 'check', 'uncheck'} and any(token in role_or_type for token in ['button', 'link', 'checkbox', 'radio']):
            score += 16
            reasons.append(f'operation_role_fit:{operation}')
        elif operation == 'select_option' and any(token in role_or_type for token in ['combobox', 'select']):
            score += 16
            reasons.append('operation_role_fit:select')
        elif operation.startswith('assert_'):
            score += 8
            reasons.append('operation_role_fit:assert')

        if locator_type in {'css', 'xpath'} and score < 70:
            reasons.append('fallback_locator_requires_review')

        return {
            'candidate': candidate,
            'score': score,
            'confidence': round(min(0.99, max(0.1, score / 140)), 3),
            'reasons': reasons[:8],
        }

    @staticmethod
    def _find_element_in_map(element_map: dict[str, Any], page_key: str, element_key: str) -> dict[str, Any] | None:
        if not page_key or not element_key:
            return None
        for page in element_map.get('pages') or []:
            if not isinstance(page, dict) or str(page.get('page_key') or '') != page_key:
                continue
            for element in page.get('elements') or []:
                if isinstance(element, dict) and str(element.get('element_key') or '') == element_key:
                    return element
        return None

    @staticmethod
    def _classify_typescript_spec_failure(output: str) -> dict[str, Any]:
        text = output or ''
        lower = text.lower()
        signals: list[str] = []
        category = 'unknown'
        repairable = False

        if any(token in lower for token in [
            'cannot find package', 'module_not_found', 'err_module_not_found',
            'executable doesn', 'please run the following command to download new browsers',
            'browser has been closed', 'operation not permitted',
        ]):
            category = 'environment'
            repairable = 'cannot find package' in lower or 'err_module_not_found' in lower
            signals.append('node/playwright runtime environment error')
        elif any(token in lower for token in [
            'syntaxerror', 'is not assignable', 'unexpected token',
            'referenceerror', 'typeerror:',
        ]):
            category = 'llm'
            repairable = True
            signals.append('generated TypeScript syntax/runtime API error')
        elif any(token in lower for token in ['locator', 'strict mode violation', 'waiting for getby', 'waiting for locator']):
            category = 'locator'
            repairable = True
            signals.append('locator resolution failed')
        elif 'timeout' in lower or 'timed out' in lower:
            category = 'timeout'
            repairable = True
            signals.append('timeout while running spec')
        elif any(token in lower for token in ['expect(', 'expect.to', 'expected', 'received', 'tobevisible', 'tohavetext', 'tocontaintext']):
            category = 'assertion'
            repairable = True
            signals.append('web-first assertion failed')
        elif any(token in lower for token in ['403', '401', 'forbidden', 'unauthorized', 'permission']):
            category = 'permission'
            signals.append('permission/auth failure')
        elif any(token in lower for token in ['net::err', 'econnrefused', 'enotfound', 'certificate', 'ssl']):
            category = 'environment'
            signals.append('network or certificate failure')

        line_matches = [
            (int(match.group(1)), int(match.group(2)))
            for match in re.finditer(r'generated\.spec\.ts:(\d+):(\d+)', text)
        ]
        # Playwright prints the test declaration location before the actual
        # failing assertion. Use the deepest generated-spec location so
        # replay/reobserve stops at the failed business step.
        line_number, column_number = max(line_matches, default=(None, None))
        return {
            'category': category,
            'repairable': repairable,
            'signals': signals,
            'line': line_number,
            'column': column_number,
            'excerpt': text[-2000:],
        }

    def _apply_typescript_import_patch(self, ts_spec: str, config_text: str) -> tuple[str, str, dict[str, Any] | None]:
        patched_spec = ts_spec
        patched_config = config_text
        changed = False
        for needle in ["from '@playwright/test'", 'from "@playwright/test"']:
            if needle in patched_spec:
                patched_spec = patched_spec.replace(needle, needle.replace('@playwright/test', 'playwright/test'))
                changed = True
            if needle in patched_config:
                patched_config = patched_config.replace(needle, needle.replace('@playwright/test', 'playwright/test'))
                changed = True
        if not changed:
            return ts_spec, config_text, None
        return patched_spec, patched_config, {
            'type': 'typescript_spec_import_patch',
            'message': 'Playwright Test 模块解析失败后，已将 @playwright/test import 改为 playwright/test 兼容写法',
        }

    def _apply_typescript_locator_patch(
        self,
        ts_spec: str,
        result: dict,
        attempted_keys: set[str],
    ) -> tuple[str, dict[str, Any] | None]:
        generated_case = result.get('generated_case') if isinstance(result.get('generated_case'), dict) else {}
        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        steps = generated_case.get('steps') if isinstance(generated_case.get('steps'), list) else []

        for step in steps:
            if not isinstance(step, dict):
                continue
            original = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
            if not original:
                continue
            page_key = str(step.get('page_key') or '')
            element_key = str(step.get('element_key') or '')
            element = self._find_element_in_map(element_map, page_key, element_key)
            if not element:
                continue
            candidates = self._dedupe_locator_dicts(
                ([element.get('recommended_locator')] if isinstance(element.get('recommended_locator'), dict) else [])
                + (element.get('locator_candidates') or [])
            )
            original_base = self._ts_locator_expression(original)
            if original_base not in ts_spec:
                continue
            ranked_candidates = sorted(
                [
                    self._score_locator_candidate(candidate, original, step, element)
                    for candidate in candidates
                    if candidate != original
                ],
                key=lambda item: item['score'],
                reverse=True,
            )
            for ranked in ranked_candidates:
                candidate = dict(ranked['candidate'])
                if candidate == original:
                    continue
                candidate['healing_score'] = ranked['score']
                candidate['healing_confidence'] = ranked['confidence']
                candidate['healing_reasons'] = ranked['reasons']
                candidate_base = self._ts_locator_expression(candidate)
                patch_key = hashlib.sha256(json.dumps({
                    'step_sort': step.get('step_sort'),
                    'page_key': page_key,
                    'element_key': element_key,
                    'old': original,
                    'new': candidate,
                }, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
                if patch_key in attempted_keys:
                    continue
                attempted_keys.add(patch_key)
                step['locator_hint'] = candidate
                return ts_spec.replace(original_base, candidate_base, 1), {
                    'type': 'ai_plan_step_locator_patch',
                    'provider': 'typescript_spec_runner',
                    'step_sort': step.get('step_sort'),
                    'page_key': page_key,
                    'element_key': element_key,
                    'target_name': step.get('target_name') or element.get('name') or '',
                    'old_locator': original,
                    'new_locator': candidate,
                    'score': ranked['score'],
                    'confidence': ranked['confidence'],
                    'score_reasons': ranked['reasons'],
                    'message': 'TypeScript spec 真实执行失败后，已使用元素地图备用 locator patch 并重跑',
                }
        return ts_spec, None

    @staticmethod
    def _ensure_typescript_expect_import(ts_spec: str) -> str:
        if 'expect(' not in str(ts_spec or ''):
            return ts_spec
        import_pattern = re.compile(
            r"import\s*\{\s*([^}]+?)\s*\}\s*from\s*(['\"])(@playwright/test|playwright/test)\2\s*;"
        )

        def replace_import(match: re.Match) -> str:
            names = [name.strip() for name in match.group(1).split(',') if name.strip()]
            exported = {name.split()[0] for name in names}
            if 'expect' not in exported:
                names.append('expect')
            return f"import {{ {', '.join(names)} }} from {match.group(2)}{match.group(3)}{match.group(2)};"

        patched, count = import_pattern.subn(replace_import, ts_spec, count=1)
        if count:
            return patched
        return "import { test, expect } from '@playwright/test';\n" + ts_spec

    async def _request_llm_typescript_patch(
        self,
        result: dict,
        ts_spec: str,
        ts_files: dict[str, Any],
        failed_attempt: dict[str, Any],
        failure_reobserve: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        task_id = result.get('task_id')
        if not task_id:
            return ts_spec, ts_files, None
        payload = {
            'typescript_spec': ts_spec,
            'typescript_files': ts_files,
            'failure': failed_attempt,
            'runtime_artifacts': failed_attempt.get('runtime_artifacts') if isinstance(failed_attempt.get('runtime_artifacts'), dict) else {},
            'element_map': result.get('element_map') or {},
            'test_plan': result.get('test_plan') or {},
            'generated_case': result.get('generated_case') or {},
            'verification_result': result.get('verification_result') or {},
            'failure_reobserve': self._compact_llm_payload_value(failure_reobserve or {}),
        }
        response = await self._api_post(
            f"/api/ui-automation/ai-generation-tasks/{task_id}/llm-repair/",
            payload,
        )
        patch = None
        if isinstance(response, dict):
            patch = response.get('patch')
            if patch is None and isinstance(response.get('data'), dict):
                patch = response['data'].get('patch')
        if not isinstance(patch, dict) or not patch.get('patch_available'):
            return ts_spec, ts_files, {
                'type': 'llm_typescript_patch_unavailable',
                'provider': 'backend-llm-repair',
                'message': (patch or {}).get('llm_error') or (patch or {}).get('repair_summary') or 'LLM 未返回可用 TypeScript patch',
                'review_required': True,
                'llm_patch': patch or {},
            }

        patched_files = patch.get('patched_typescript_files') if isinstance(patch.get('patched_typescript_files'), dict) else {}
        patched_spec = str(patch.get('patched_typescript_spec') or patched_files.get('generated.spec.ts') or '').strip()
        if not patched_spec:
            return ts_spec, ts_files, {
                'type': 'llm_typescript_patch_unavailable',
                'provider': 'backend-llm-repair',
                'message': 'LLM patch 缺少 generated.spec.ts 内容',
                'review_required': True,
                'llm_patch': patch,
            }
        patched_spec = self._ensure_typescript_expect_import(patched_spec)
        merged_files = {**ts_files, **patched_files, 'generated.spec.ts': patched_spec}
        if patched_spec == ts_spec and merged_files == ts_files:
            return ts_spec, ts_files, None
        return patched_spec, merged_files, {
            'type': 'llm_typescript_spec_patch',
            'provider': 'backend-llm-repair',
            'message': patch.get('repair_summary') or 'LLM 基于失败证据生成 TypeScript spec patch 并重跑',
            'failure_category': patch.get('failure_category') or '',
            'review_required': bool(patch.get('review_required')),
            'changed_steps': patch.get('changed_steps') or [],
            'review_notes': patch.get('review_notes') or [],
            'llm_config': patch.get('llm_config') or {},
            'failure_reobserve_status': (failure_reobserve or {}).get('status') or '',
        }

    @staticmethod
    def _extract_failed_spec_step_marker(ts_spec: str, failed_attempt: dict[str, Any]) -> dict[str, Any]:
        """Find the nearest WHART_AI_STEP marker before the failed spec line."""
        if not ts_spec:
            return {}
        classification = (
            failed_attempt.get('failure_classification')
            if isinstance(failed_attempt.get('failure_classification'), dict)
            else {}
        )
        line_number = classification.get('line') or failed_attempt.get('line')
        try:
            line_number = int(line_number)
        except (TypeError, ValueError):
            line_number = 0

        lines = ts_spec.splitlines()
        if not lines:
            return {}
        start_index = min(max(line_number - 1, 0), len(lines) - 1) if line_number > 0 else len(lines) - 1
        marker_token = 'WHART_AI_STEP'
        for index in range(start_index, max(-1, start_index - 120), -1):
            text = lines[index]
            marker_index = text.find(marker_token)
            if marker_index < 0:
                continue
            marker_text = text[marker_index + len(marker_token):].strip()
            start = marker_text.find('{')
            end = marker_text.rfind('}')
            if start < 0 or end < start:
                continue
            try:
                marker = json.loads(marker_text[start:end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(marker, dict):
                marker['_spec_marker_line'] = index + 1
                marker['_failed_spec_line'] = line_number or None
                return marker
        return {}

    @staticmethod
    def _reobserve_replay_steps_before_marker(
        result: dict,
        marker: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int | None]:
        if not isinstance(marker, dict) or not marker:
            return [], None
        generated_case = result.get('generated_case') if isinstance(result.get('generated_case'), dict) else {}
        test_plan = result.get('test_plan') if isinstance(result.get('test_plan'), dict) else {}
        steps = generated_case.get('steps') if isinstance(generated_case.get('steps'), list) else None
        if steps is None:
            steps = test_plan.get('steps') if isinstance(test_plan.get('steps'), list) else []
        normalized_steps = [step for step in steps if isinstance(step, dict)]

        marker_sort = marker.get('step_sort')
        marker_page = str(marker.get('page_key') or '')
        marker_element = str(marker.get('element_key') or '')
        marker_operation = str(marker.get('operation') or '').strip().lower()
        marker_target = str(marker.get('target_name') or '')

        for index, step in enumerate(normalized_steps):
            same_sort = marker_sort is not None and str(step.get('step_sort')) == str(marker_sort)
            same_element = (
                marker_element
                and str(step.get('element_key') or '') == marker_element
                and (not marker_page or str(step.get('page_key') or '') == marker_page)
            )
            same_target = (
                marker_target
                and marker_operation
                and str(step.get('operation') or step.get('action') or '').strip().lower() == marker_operation
                and marker_target == str(step.get('target_name') or '')
            )
            if same_sort or same_element or same_target:
                return [dict(item) for item in normalized_steps[:index]], index
        return [], None

    async def _replay_ai_steps_for_failure_reobserve(
        self,
        page: Any,
        result: dict,
        marker: dict[str, Any],
        base_url: str,
        safety_policy: dict[str, Any],
    ) -> dict[str, Any]:
        replay_steps, failed_index = self._reobserve_replay_steps_before_marker(result, marker)
        if not replay_steps:
            return {
                'status': 'skipped',
                'reason': 'missing_failed_step_marker_or_prefix',
                'failed_step_index': failed_index,
                'target_marker': marker,
                'steps': [],
            }
        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        replay_records: list[dict[str, Any]] = []
        for index, step in enumerate(replay_steps):
            operation = self.executor._normalise_ai_plan_operation(step)
            record = {
                'index': index,
                'step_sort': step.get('step_sort'),
                'operation': operation,
                'page_key': step.get('page_key') or '',
                'element_key': step.get('element_key') or '',
                'target_name': step.get('target_name') or '',
                'status': 'running',
            }
            try:
                success, message, _ = await self.executor._execute_ai_plan_step(
                    page,
                    step,
                    base_url,
                    safety_policy,
                    element_map,
                )
                record['status'] = 'success' if success else 'failed'
                record['message'] = message
                replay_records.append(record)
                if not success:
                    return {
                        'status': 'partial',
                        'reason': message,
                        'failed_step_index': failed_index,
                        'target_marker': marker,
                        'steps': replay_records,
                    }
            except Exception as exc:
                record['status'] = 'failed'
                record['message'] = f'{type(exc).__name__}: {str(exc)[:500]}'
                replay_records.append(record)
                return {
                    'status': 'partial',
                    'reason': record['message'],
                    'failed_step_index': failed_index,
                    'target_marker': marker,
                    'steps': replay_records,
                }
        return {
            'status': 'success',
            'reason': 'replayed_steps_before_failed_spec_marker',
            'failed_step_index': failed_index,
            'target_marker': marker,
            'steps': replay_records,
        }

    async def _collect_failure_reobserve_snapshot(
        self,
        result: dict,
        failed_attempt: dict[str, Any],
        attempt_index: int,
        ts_spec: str = '',
    ) -> dict[str, Any]:
        """失败后重新采集当前页面状态，供 LLM healer 基于真实 UI 修复。"""
        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        test_plan = result.get('test_plan') if isinstance(result.get('test_plan'), dict) else {}
        base_url = element_map.get('base_url') or result.get('target_url') or test_plan.get('target_url') or ''
        if not base_url:
            return {'status': 'skipped', 'reason': 'missing_base_url'}

        task_id = result.get('task_id') or int(time.time())
        env_config = result.get('environment_config') if isinstance(result.get('environment_config'), dict) else {}
        safety_policy = result.get('safety_policy') if isinstance(result.get('safety_policy'), dict) else {}
        screenshot_path = str(
            Path(getattr(self.executor, 'screenshot_dir', './data/screenshots'))
            / f'ai_generation_{task_id}_repair_observe_{attempt_index + 1}.png'
        )
        try:
            async with self.executor.browser_session(
                ignore_https_errors=self.executor._ignore_https_errors_for_env(env_config)
            ) as page:
                self.executor._setup_page_listeners(page)
                auth_observations = await self.executor._ensure_authenticated_before_ai_flow(
                    page,
                    {
                        'target_url': base_url,
                        'environment_config': env_config,
                        'safety_policy': safety_policy,
                    },
                    base_url,
                    safety_policy,
                )
                failed_marker = self._extract_failed_spec_step_marker(ts_spec, failed_attempt)
                replay_result = await self._replay_ai_steps_for_failure_reobserve(
                    page,
                    result,
                    failed_marker,
                    base_url,
                    safety_policy,
                )
                if page.url == 'about:blank':
                    await page.goto(base_url, wait_until='networkidle', timeout=self.executor.action_timeout)
                await self.executor._wait_for_ui_interaction_ready(page)
                state = await self.executor._mcp_collect_page_state(
                    page,
                    f'repair_observe_{attempt_index + 1}',
                    screenshot_path,
                    max_elements=260,
                )
                return {
                    'status': 'success',
                    'reason': 'fresh_native_playwright_snapshot',
                    'failed_attempt': failed_attempt.get('attempt'),
                    'failure_category': (
                        failed_attempt.get('failure_classification') or {}
                    ).get('category') if isinstance(failed_attempt.get('failure_classification'), dict) else '',
                    'auth_observations': auth_observations,
                    'replay': replay_result,
                    'page_state': state,
                }
        except Exception as exc:
            return {
                'status': 'failed',
                'reason': f'{type(exc).__name__}: {str(exc)[:800]}',
                'failed_attempt': failed_attempt.get('attempt'),
            }

    @staticmethod
    def _apply_typescript_execution_status(
        result: dict,
        verification: dict,
        execution: dict,
        repairs: list[dict[str, Any]],
    ) -> None:
        """Use the real Playwright Test run as the authoritative generated-script verdict."""
        plan_validation = verification.get('plan_validation') if isinstance(verification.get('plan_validation'), dict) else {}
        if plan_validation.get('status') == 'failed':
            semantic_issues = plan_validation.get('semantic_issues') if isinstance(plan_validation.get('semantic_issues'), list) else []
            unresolved_steps = plan_validation.get('unresolved_steps') if isinstance(plan_validation.get('unresolved_steps'), list) else []
            issue_messages = [
                str(item.get('message') or item.get('reason') or '')
                for item in [*semantic_issues, *unresolved_steps]
                if isinstance(item, dict) and (item.get('message') or item.get('reason'))
            ]
            verification['status'] = 'failed'
            verification['typescript_spec_status'] = execution.get('status') or 'unknown'
            verification['final_status_source'] = 'plan_validation'
            result['status'] = 'failed'
            result['failure_category'] = 'llm_plan_validation'
            result['message'] = (
                'LLM 规划未覆盖用户需求或存在未绑定元素，TypeScript 执行结果不作为成功依据: '
                + '；'.join(issue_messages[:3])
                if issue_messages else
                'LLM 规划未通过静态校验，TypeScript 执行结果不作为成功依据'
            )
            return

        if execution.get('status') != 'success':
            classification = execution.get('failure_classification') if isinstance(execution.get('failure_classification'), dict) else {}
            category = classification.get('category') or execution.get('failure_category') or 'unknown'
            verification['status'] = 'failed'
            verification['typescript_spec_status'] = 'failed'
            result['status'] = 'failed'
            result['failure_category'] = result.get('failure_category') or category
            result['message'] = (
                execution.get('reason')
                or 'TypeScript Playwright spec 真实执行失败，已记录失败分类、trace、截图和修复历史'
            )
            return

        verification['typescript_spec_status'] = 'success'
        prior_flow = verification.get('flow_execution') if isinstance(verification.get('flow_execution'), dict) else {}
        if verification.get('status') != 'success' or result.get('status') != 'success':
            verification['status_before_typescript_execution'] = verification.get('status')
            verification['flow_status_before_typescript_execution'] = prior_flow.get('status') or ''
            verification['final_status_source'] = 'typescript_playwright_spec'
        verification['status'] = 'success'
        result['status'] = 'success'
        result['failure_category'] = ''
        if repairs:
            result['message'] = 'TypeScript Playwright spec 自动修复后执行成功'
        elif prior_flow.get('status') == 'failed':
            result['message'] = 'TypeScript Playwright spec 真实执行成功；native 预验证失败已保留为诊断证据'
        else:
            result['message'] = 'TypeScript Playwright spec 真实执行成功'

    async def _run_typescript_spec_artifact(self, result: dict) -> None:
        """真实运行生成的 TypeScript Playwright spec，并把结果写入 script_artifacts。"""
        verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
        script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
        generated_case = result.get('generated_case') if isinstance(result.get('generated_case'), dict) else {}
        local_runtime = result.get('_local_runtime') if isinstance(result.get('_local_runtime'), dict) else {}
        ts_spec = script_artifacts.get('playwright_ts_spec') or generated_case.get('playwright_ts_spec') or ''
        ts_files = (
            script_artifacts.get('playwright_ts_files')
            if isinstance(script_artifacts.get('playwright_ts_files'), dict) else
            generated_case.get('playwright_ts_files') if isinstance(generated_case.get('playwright_ts_files'), dict) else {}
        )
        if ts_spec and not ts_files:
            ts_files = {'generated.spec.ts': ts_spec}
        if ts_spec and 'generated.spec.ts' not in ts_files:
            ts_files = {**ts_files, 'generated.spec.ts': ts_spec}
        if ts_spec:
            ts_spec = self._ensure_typescript_expect_import(ts_spec)
            ts_files = {**ts_files, 'generated.spec.ts': ts_spec}

        if not ts_spec:
            result.pop('_local_runtime', None)
            return

        execution = {
            'status': 'skipped',
            'reason': '',
            'duration': 0,
        }
        script_artifacts['typescript_spec_execution'] = execution
        verification['script_artifacts'] = script_artifacts
        data_lifecycle = (
            (result.get('test_plan') or {}).get('data_lifecycle')
            if isinstance(result.get('test_plan'), dict) else None
        ) or (
            generated_case.get('data_lifecycle') if isinstance(generated_case.get('data_lifecycle'), dict) else {}
        )
        if isinstance(data_lifecycle, dict):
            verification['data_lifecycle'] = data_lifecycle
        result['verification_result'] = verification

        if not self.typescript_spec_enabled:
            execution['reason'] = 'typescript_spec.enabled=false'
            result.pop('_local_runtime', None)
            return

        command = self._resolve_playwright_test_command()
        if not command:
            execution['reason'] = '未找到 playwright/npx 命令，无法真实运行 TypeScript spec'
            result.pop('_local_runtime', None)
            return

        task_id = result.get('task_id') or int(time.time())
        work_dir = self._playwright_workspace_for_command(command, task_id)
        spec_path = work_dir / 'generated.spec.ts'
        config_path = work_dir / 'playwright.config.ts'
        work_dir.mkdir(parents=True, exist_ok=True)

        verification_storage = verification.get('storage_state') if isinstance(verification.get('storage_state'), dict) else {}
        storage_state_source = str(
            local_runtime.get('storage_state_path')
            or verification_storage.get('path')
            or verification_storage.get('source')
            or ''
        )
        storage_state_target = work_dir / 'storage-state.json'
        storage_state_config = ''
        candidate_storage_paths = []
        if storage_state_source:
            source_path = Path(storage_state_source).expanduser()
            if source_path.is_absolute():
                candidate_storage_paths.append(source_path)
            else:
                candidate_storage_paths.extend([
                    Path.cwd() / source_path,
                    Path(__file__).resolve().parent / source_path,
                    Path(__file__).resolve().parent.parent / source_path,
                ])
        resolved_storage_state = next((path for path in candidate_storage_paths if path.exists()), None)
        if resolved_storage_state and resolved_storage_state.exists():
            try:
                shutil.copyfile(str(resolved_storage_state), storage_state_target)
                storage_state_config = f"    storageState: {json.dumps(str(storage_state_target))},\n"
                execution['storage_state'] = {
                    'status': 'loaded',
                    'source': str(resolved_storage_state),
                    'target': str(storage_state_target),
                }
            except Exception as exc:
                execution['storage_state'] = {
                    'status': 'failed',
                    'source': str(resolved_storage_state),
                    'error': str(exc)[:500],
                }
        elif storage_state_source:
            execution['storage_state'] = {
                'status': 'missing',
                'source': storage_state_source,
            }

        chrome_path = get_browser_executable_path()
        executable_config = f"executablePath: {json.dumps(str(chrome_path))}," if chrome_path else ''
        config_text = (
            "import { defineConfig } from '@playwright/test';\n\n"
            "export default defineConfig({\n"
            "  timeout: 60000,\n"
            "  expect: { timeout: 10000 },\n"
            "  outputDir: __WHART_OUTPUT_DIR__,\n"
            "  reporter: [['line'], ['junit', { outputFile: 'junit.xml' }]],\n"
            "  use: {\n"
            "    headless: true,\n"
            "    ignoreHTTPSErrors: true,\n"
            "    trace: 'on',\n"
            "    screenshot: 'on',\n"
            "    video: 'retain-on-failure',\n"
            f"{storage_state_config}"
            f"    launchOptions: {{ {executable_config} }},\n"
            "  },\n"
            "});\n"
        )

        env = os.environ.copy()
        env['CI'] = '1'
        env.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(Path(__file__).resolve().parent / 'browsers'))
        ocr_script = Path(__file__).resolve().parent / 'runtime_input_resolver.py'
        ocr_artifact_dir = work_dir / 'runtime-inputs'
        ocr_artifact_dir.mkdir(parents=True, exist_ok=True)
        env['WHART_OCR_PYTHON'] = sys.executable
        env['WHART_OCR_SCRIPT'] = str(ocr_script)
        env['WHART_OCR_ARTIFACT_DIR'] = str(ocr_artifact_dir)

        safety_policy = result.get('safety_policy') if isinstance(result.get('safety_policy'), dict) else {}
        max_repair_rounds = int(
            safety_policy.get('max_repair_rounds')
            or result.get('max_repair_rounds')
            or 2
        )
        # 一个 spec 来源只真实运行一次。失败后的下一次尝试必须来自可审计的
        # LLM repair patch 或完整重规划，不能在执行器里静默改脚本。
        max_attempts = max(1, min(max_repair_rounds + 1, 3))
        attempts: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        review_queue = verification.get('review_queue') if isinstance(verification.get('review_queue'), list) else []

        for attempt_index in range(max_attempts):
            output_dir = work_dir / f'test-results-attempt-{attempt_index + 1}'
            junit_path = work_dir / f'junit-attempt-{attempt_index + 1}.xml'
            if output_dir.exists():
                shutil.rmtree(output_dir, ignore_errors=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            env['PLAYWRIGHT_JUNIT_OUTPUT_FILE'] = str(junit_path)
            config_attempt_text = config_text.replace('__WHART_OUTPUT_DIR__', json.dumps(str(output_dir)))
            for relative_name, content in ts_files.items():
                safe_parts = [
                    ''.join(ch if ch.isalnum() or ch in {'-', '_', '.'} else '_' for ch in part)
                    for part in str(relative_name).split('/')
                    if part and part not in {'.', '..'}
                ]
                if not safe_parts:
                    continue
                target_path = work_dir.joinpath(*safe_parts)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(str(content or ''), encoding='utf-8')
            spec_path.write_text(ts_spec, encoding='utf-8')
            config_path.write_text(config_attempt_text, encoding='utf-8')

            started = time.time()
            process = None
            attempt: dict[str, Any] = {
                'attempt': attempt_index + 1,
                'status': 'running',
                'duration': 0,
                'spec_hash': hashlib.sha256(ts_spec.encode('utf-8')).hexdigest(),
            }
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    'test',
                    str(spec_path.name),
                    '--config',
                    str(config_path.name),
                    cwd=str(work_dir),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(10, self.typescript_spec_timeout))
                duration = time.time() - started
                stdout_text = stdout.decode('utf-8', errors='replace')[-8000:]
                stderr_text = stderr.decode('utf-8', errors='replace')[-8000:]
                combined_output = f"{stdout_text}\n{stderr_text}"
                trace_files = sorted(output_dir.rglob('trace.zip'), key=lambda item: item.stat().st_mtime, reverse=True)
                network_summary = self._extract_trace_network_summary(str(trace_files[0])) if trace_files else {}
                uploaded_trace = None
                if trace_files:
                    uploaded_trace = await self._upload_trace_file(str(trace_files[0]))
                self._promote_typescript_execution_trace(verification, uploaded_trace)
                runtime_artifacts = await self._collect_typescript_runtime_artifacts(
                    output_dir,
                    trace_files,
                    uploaded_trace,
                    network_summary,
                )

                classification = (
                    self._classify_typescript_spec_failure(combined_output)
                    if process.returncode != 0 else
                    {'category': '', 'repairable': False, 'signals': []}
                )
                attempt.update({
                    'status': 'success' if process.returncode == 0 else 'failed',
                    'returncode': process.returncode,
                    'duration': duration,
                    'command': command + ['test', spec_path.name, '--config', config_path.name],
                    'work_dir': str(work_dir),
                    'spec_path': str(spec_path),
                    'stdout': stdout_text,
                    'stderr': stderr_text,
                    'junit_xml': junit_path.read_text(encoding='utf-8', errors='replace') if junit_path.exists() else '',
                    'trace_path': uploaded_trace or (str(trace_files[0]) if trace_files else ''),
                    'trace_file_count': len(trace_files),
                    'runtime_artifacts': runtime_artifacts,
                    'network_summary': network_summary,
                    'failure_classification': classification,
                    'runtime_input_artifacts': [
                        str(path)
                        for path in sorted(ocr_artifact_dir.glob('captcha-*.png'))
                    ][-20:],
                })
                attempts.append(attempt)

                if process.returncode == 0:
                    execution.update(attempt)
                    execution['status'] = 'success'
                    break

                # 失败证据先用于局部 LLM repair patch；patch 不可用时再交给
                # 外层完整重规划。每个重试轮次都有清晰、可审计的 spec 来源。
                if classification.get('repairable'):
                    attempt['failed_spec_step_marker'] = self._extract_failed_spec_step_marker(
                        ts_spec, attempt
                    )
                    failure_reobserve = await self._collect_failure_reobserve_snapshot(
                        result, attempt, attempt_index, ts_spec
                    )
                    reobserve_state = failure_reobserve.get('page_state') or {}
                    attempt['failure_reobserve'] = {
                        'status': failure_reobserve.get('status'),
                        'reason': failure_reobserve.get('reason'),
                        'page_url': reobserve_state.get('url') or '',
                        'element_count': len(reobserve_state.get('elements') or []),
                        'accessibility_node_count': (
                            (reobserve_state.get('accessibility_snapshot') or {}).get('node_count')
                            if isinstance(reobserve_state.get('accessibility_snapshot'), dict) else 0
                        ),
                    }
                    self._promote_failure_reobserve_into_element_map(result, failure_reobserve)
                    verification['failure_reobserve'] = failure_reobserve
                    if attempt_index + 1 < max_attempts:
                        patched_spec, patched_files, repair = await self._request_llm_typescript_patch(
                            result,
                            ts_spec,
                            ts_files,
                            attempt,
                            failure_reobserve,
                        )
                        if repair:
                            repairs.append({
                                'round': len(repairs) + 1,
                                **repair,
                                'attempt': attempt_index + 1,
                            })
                        if repair and repair.get('type') == 'llm_typescript_spec_patch':
                            ts_spec = patched_spec
                            ts_files = patched_files
                            script_artifacts['playwright_ts_spec'] = ts_spec
                            script_artifacts['playwright_ts_files'] = ts_files
                            script_artifacts['playwright_ts_spec_hash'] = hashlib.sha256(
                                ts_spec.encode('utf-8')
                            ).hexdigest()
                            if isinstance(result.get('generated_case'), dict):
                                result['generated_case']['playwright_ts_spec'] = ts_spec
                                result['generated_case']['playwright_ts_files'] = ts_files
                            verification['script_artifacts'] = script_artifacts
                            result['verification_result'] = verification
                            continue
                execution.update(attempt)
                execution['status'] = 'failed'
                break
            except asyncio.TimeoutError:
                if process and process.returncode is None:
                    process.kill()
                    try:
                        await process.wait()
                    except Exception:
                        pass
                attempt.update({
                    'status': 'failed',
                    'failure_category': 'timeout',
                    'duration': time.time() - started,
                    'reason': f'TypeScript spec 执行超过 {self.typescript_spec_timeout}s',
                    'failure_classification': {
                        'category': 'timeout',
                        'repairable': True,
                        'signals': ['process timeout'],
                    },
                })
                attempts.append(attempt)
                execution.update(attempt)
                break
            except Exception as exc:
                attempt.update({
                    'status': 'failed',
                    'failure_category': 'environment',
                    'duration': time.time() - started,
                    'reason': f'{type(exc).__name__}: {str(exc)[:1000]}',
                    'failure_classification': {
                        'category': 'environment',
                        'repairable': False,
                        'signals': [type(exc).__name__],
                    },
                })
                attempts.append(attempt)
                execution.update(attempt)
                break

        execution['attempts'] = attempts
        execution['repair_history'] = repairs
        execution['repair_round_count'] = len(repairs)
        execution['final_spec_hash'] = hashlib.sha256(ts_spec.encode('utf-8')).hexdigest()
        prior_execution_history = script_artifacts.get('typescript_execution_history')
        if not isinstance(prior_execution_history, list):
            prior_execution_history = []
        execution_history = [
            item for item in prior_execution_history
            if isinstance(item, dict)
        ]
        execution_history.append(execution)
        script_artifacts['typescript_execution_history'] = execution_history[-8:]
        script_artifacts['playwright_ts_spec'] = ts_spec
        script_artifacts['playwright_ts_spec_hash'] = execution['final_spec_hash']
        ts_files['generated.spec.ts'] = ts_spec
        script_artifacts['playwright_ts_files'] = ts_files
        script_artifacts['playwright_ts_file_hashes'] = {
            name: hashlib.sha256(str(content or '').encode('utf-8')).hexdigest()
            for name, content in ts_files.items()
        }
        generated_case['playwright_ts_spec'] = ts_spec
        generated_case['playwright_ts_spec_hash'] = execution['final_spec_hash']
        generated_case['playwright_ts_files'] = ts_files
        result['generated_case'] = generated_case

        if repairs:
            repair_history = result.get('repair_history') if isinstance(result.get('repair_history'), list) else []
            repair_history.extend(repairs)
            result['repair_history'] = repair_history
            result['current_repair_round'] = len(repair_history)
            verification['review_queue'] = review_queue

        self._apply_typescript_execution_status(result, verification, execution, repairs)

        script_artifacts['typescript_spec_execution'] = execution
        verification['script_artifacts'] = script_artifacts
        result['verification_result'] = verification
        result.pop('_local_runtime', None)

    def _attach_ai_generation_report_artifacts(self, result: dict):
        """生成 AI 生成任务的报告摘要、HTML 和 JUnit XML。"""
        verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
        flow_execution = verification.get('flow_execution') if isinstance(verification.get('flow_execution'), dict) else {}
        steps = flow_execution.get('steps') if isinstance(flow_execution.get('steps'), list) else []
        total_steps = int(flow_execution.get('total_steps') or len(steps))
        failed_steps = int(flow_execution.get('failed_steps') or 0)
        repaired_steps = int(flow_execution.get('repaired_steps') or 0)
        duration = float(flow_execution.get('duration') or result.get('duration') or 0)
        status = result.get('status') or verification.get('status') or 'unknown'
        task_id = result.get('task_id')
        intent = result.get('intent_analysis') if isinstance(result.get('intent_analysis'), dict) else {}
        repair_history = result.get('repair_history') if isinstance(result.get('repair_history'), list) else []
        plan_validation = verification.get('plan_validation') if isinstance(verification.get('plan_validation'), dict) else {}
        review_queue = verification.get('review_queue') if isinstance(verification.get('review_queue'), list) else []
        script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
        data_lifecycle = verification.get('data_lifecycle') if isinstance(verification.get('data_lifecycle'), dict) else {}
        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        mcp_protocol = element_map.get('mcp_protocol') if isinstance(element_map.get('mcp_protocol'), dict) else {}
        ts_spec = script_artifacts.get('playwright_ts_spec') or (result.get('generated_case') or {}).get('playwright_ts_spec') or ''
        ts_files = (
            script_artifacts.get('playwright_ts_files')
            if isinstance(script_artifacts.get('playwright_ts_files'), dict) else
            (result.get('generated_case') or {}).get('playwright_ts_files')
            if isinstance((result.get('generated_case') or {}).get('playwright_ts_files'), dict) else {}
        )
        if ts_spec and not ts_files:
            ts_files = {'generated.spec.ts': ts_spec}

        def validate_typescript_spec(spec: str, files: dict[str, Any] | None = None) -> dict:
            if not spec:
                return {
                    'status': 'missing',
                    'checks': [],
                    'warnings': ['未生成 TypeScript Playwright spec'],
                }
            file_map = files if isinstance(files, dict) else {}
            combined = '\n'.join(str(value or '') for value in file_map.values()) if file_map else spec
            checks = [
                {
                    'name': 'imports_playwright_test',
                    'passed': "from '@playwright/test'" in combined or "from 'playwright/test'" in combined
                    or 'from "@playwright/test"' in combined or 'from "playwright/test"' in combined,
                },
                {'name': 'declares_test', 'passed': 'test(' in spec or 'test.describe(' in spec},
                {'name': 'uses_expect', 'passed': 'expect(' in combined},
                {'name': 'uses_web_first_assertion', 'passed': any(token in combined for token in ['toBeVisible', 'toHaveText', 'toContainText', 'toHaveValue', 'toHaveURL'])},
                {'name': 'uses_stable_locator_api', 'passed': any(token in combined for token in ['getByTestId', 'getByRole', 'getByLabel', 'getByPlaceholder', 'getByText'])},
                {'name': 'uses_page_object_or_fixture', 'passed': 'WhartGeneratedPage' in combined or 'test.extend' in combined},
                {'name': 'not_python_async_api', 'passed': 'from playwright.async_api' not in combined and 'async def ' not in combined},
            ]
            warnings = []
            if 'waitForTimeout' in combined:
                warnings.append('包含 waitForTimeout，建议后续替换为 URL、response 或 locator state 等确定等待')
            if 'page.locator("xpath=' in combined or "page.locator('xpath=" in combined:
                warnings.append('包含 XPath fallback，建议通过元素地图基线继续沉淀更稳定 locator')
            status = 'success' if all(item['passed'] for item in checks) else 'warning'
            return {
                'status': status,
                'checks': checks,
                'warnings': warnings,
                'hash': hashlib.sha256(spec.encode('utf-8')).hexdigest(),
                'file_hashes': {
                    name: hashlib.sha256(str(content or '').encode('utf-8')).hexdigest()
                    for name, content in file_map.items()
                },
            }

        if ts_spec:
            script_artifacts['playwright_ts_spec'] = ts_spec
            script_artifacts['playwright_ts_spec_hash'] = hashlib.sha256(ts_spec.encode('utf-8')).hexdigest()
        if ts_files:
            script_artifacts['playwright_ts_files'] = ts_files
        script_artifacts['typescript_spec_validation'] = validate_typescript_spec(ts_spec, ts_files)
        verification['script_artifacts'] = script_artifacts
        ts_execution = script_artifacts.get('typescript_spec_execution') if isinstance(script_artifacts.get('typescript_spec_execution'), dict) else {}
        ts_attempts = ts_execution.get('attempts') if isinstance(ts_execution.get('attempts'), list) else []
        runtime_artifacts = ts_execution.get('runtime_artifacts') if isinstance(ts_execution.get('runtime_artifacts'), dict) else {}
        if not runtime_artifacts and ts_attempts and isinstance(ts_attempts[-1], dict):
            runtime_artifacts = ts_attempts[-1].get('runtime_artifacts') if isinstance(ts_attempts[-1].get('runtime_artifacts'), dict) else {}
        artifact_counts = runtime_artifacts.get('counts') if isinstance(runtime_artifacts.get('counts'), dict) else {}
        trace_artifact = runtime_artifacts.get('trace') if isinstance(runtime_artifacts.get('trace'), dict) else {}
        ts_network_summary = runtime_artifacts.get('network_summary') if isinstance(runtime_artifacts.get('network_summary'), dict) else {}

        def esc(value) -> str:
            return html.escape(str(value or ''), quote=True)

        rows = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            screenshot = step.get('screenshot') or ''
            screenshot_html = (
                f'<img alt="step screenshot" src="{esc(screenshot)}" style="max-width:220px;max-height:140px;border:1px solid #ddd">'
                if isinstance(screenshot, str) and screenshot.startswith('data:image/')
                else esc(step.get('mcp_screenshot') or '')
            )
            tool_context = step.get('tool_context') if isinstance(step.get('tool_context'), dict) else {}
            rows.append(
                "<tr>"
                f"<td>{esc(step.get('step_sort'))}</td>"
                f"<td>{esc(step.get('operation'))}</td>"
                f"<td>{esc(step.get('target_name'))}</td>"
                f"<td>{esc(step.get('status'))}</td>"
                f"<td>{esc(step.get('failure_category') or step.get('failure_category_before_repair'))}</td>"
                f"<td>{esc(step.get('message'))}</td>"
                f"<td>{esc(json.dumps(tool_context, ensure_ascii=False))}</td>"
                f"<td>{screenshot_html}</td>"
                "</tr>"
            )
        evidence_items = []
        for screenshot in runtime_artifacts.get('screenshots') or []:
            if not isinstance(screenshot, dict):
                continue
            src = screenshot.get('url') or screenshot.get('path') or ''
            if src and not str(src).startswith(('http://', 'https://', '/media/', 'data:')):
                src = f'/media/{str(src).lstrip("/")}'
            evidence_items.append(
                f'<figure><img alt="runtime screenshot" src="{esc(src)}" style="max-width:220px;max-height:140px;border:1px solid #ddd">'
                f'<figcaption>{esc(screenshot.get("name") or "screenshot")}</figcaption></figure>'
            )
        for video in runtime_artifacts.get('videos') or []:
            if not isinstance(video, dict):
                continue
            href = video.get('url') or video.get('path') or ''
            if href and not str(href).startswith(('http://', 'https://', '/media/')):
                href = f'/media/{str(href).lstrip("/")}'
            evidence_items.append(f'<p>Video: <a href="{esc(href)}">{esc(video.get("name") or href)}</a></p>')
        network_failed_rows = []
        for item in (ts_network_summary.get('failed') or [])[:80]:
            if not isinstance(item, dict):
                continue
            network_failed_rows.append(
                '<tr>'
                f'<td>{esc(item.get("method"))}</td>'
                f'<td>{esc(item.get("status"))}</td>'
                f'<td>{esc(item.get("resource_type"))}</td>'
                f'<td>{esc(item.get("url"))}</td>'
                '</tr>'
            )
        repair_diff_rows = []
        for repair in repair_history[-50:]:
            if not isinstance(repair, dict):
                continue
            changed_steps = repair.get('changed_steps') if isinstance(repair.get('changed_steps'), list) else []
            if changed_steps:
                for changed in changed_steps:
                    if not isinstance(changed, dict):
                        continue
                    repair_diff_rows.append(
                        '<tr>'
                        f'<td>{esc(repair.get("type"))}</td>'
                        f'<td>{esc(changed.get("step_sort"))}</td>'
                        f'<td>{esc(changed.get("reason") or repair.get("message"))}</td>'
                        f'<td>{esc(changed.get("old"))}</td>'
                        f'<td>{esc(changed.get("new"))}</td>'
                        '</tr>'
                    )
            elif repair.get('type') in {'ai_plan_step_locator_patch', 'llm_typescript_spec_patch'}:
                repair_diff_rows.append(
                    '<tr>'
                    f'<td>{esc(repair.get("type"))}</td>'
                    f'<td>{esc(repair.get("step_sort"))}</td>'
                    f'<td>{esc(repair.get("message"))}</td>'
                    f'<td>{esc(json.dumps(repair.get("old_locator") or {}, ensure_ascii=False))}</td>'
                    f'<td>{esc(json.dumps(repair.get("new_locator") or repair.get("review_notes") or {}, ensure_ascii=False))}</td>'
                    '</tr>'
                )

        failed_messages = [
            str(step.get('message') or '')
            for step in steps
            if isinstance(step, dict) and step.get('status') == 'failed'
        ]
        ts_validation = script_artifacts.get('typescript_spec_validation') if isinstance(script_artifacts.get('typescript_spec_validation'), dict) else {}
        unresolved_step_count = int(plan_validation.get('unresolved_step_count') or 0)
        review_queue_count = len(review_queue)
        map_pages = [page for page in (element_map.get('pages') or []) if isinstance(page, dict)]
        ax_snapshot_count = sum(
            1
            for page in map_pages
            if isinstance(page.get('accessibility_snapshot'), dict)
            and page.get('accessibility_snapshot', {}).get('status') == 'success'
        )
        repair_attempts = ts_execution.get('attempts') if isinstance(ts_execution.get('attempts'), list) else []
        has_failure_reobserve = any(
            isinstance(attempt, dict)
            and isinstance(attempt.get('failure_reobserve'), dict)
            and attempt.get('failure_reobserve', {}).get('status')
            for attempt in repair_attempts
        )
        recommended_architecture = {
            'document': 'D:/Project/docs/ai-ui-test-generation-research.md',
            'exploration_provider': 'playwright-native',
            'official_playwright_mcp_exploration': False,
            'checks': [
                {
                    'name': 'structured_llm_planner',
                    'passed': bool(verification.get('llm_planning', {}).get('status') == 'success') if isinstance(verification.get('llm_planning'), dict) else False,
                    'evidence': verification.get('llm_planning') if isinstance(verification.get('llm_planning'), dict) else {},
                },
                {
                    'name': 'page_state_element_map',
                    'passed': bool(map_pages and element_map.get('locator_baseline')),
                    'evidence': element_map.get('coverage_summary') or {},
                },
                {
                    'name': 'dom_accessibility_visual_context',
                    'passed': bool(map_pages and ax_snapshot_count > 0 and any((page.get('visual_som') or {}).get('enabled') for page in map_pages)),
                    'evidence': {'accessibility_snapshot_pages': ax_snapshot_count, 'page_count': len(map_pages)},
                },
                {
                    'name': 'typescript_playwright_spec_generator',
                    'passed': bool(ts_spec and ts_validation.get('status') in {'success', 'warning'}),
                    'evidence': {'validation_status': ts_validation.get('status') or ''},
                },
                {
                    'name': 'real_execution_with_trace_screenshot_network',
                    'passed': bool(ts_execution.get('status') in {'success', 'failed'} and runtime_artifacts),
                    'evidence': {
                        'status': ts_execution.get('status') or '',
                        'trace': trace_artifact.get('path') or ts_execution.get('trace_path') or '',
                        'screenshots': artifact_counts.get('screenshots') or 0,
                        'network_total': ts_network_summary.get('total') or 0,
                    },
                },
                {
                    'name': 'failure_classification',
                    'passed': bool(
                        ts_execution.get('status') == 'success'
                        or (isinstance(ts_execution.get('failure_classification'), dict) and ts_execution.get('failure_classification', {}).get('category'))
                        or (
                            repair_attempts
                            and isinstance(repair_attempts[-1], dict)
                            and isinstance(repair_attempts[-1].get('failure_classification'), dict)
                            and repair_attempts[-1].get('failure_classification', {}).get('category')
                        )
                    ),
                    'evidence': (
                        ts_execution.get('failure_classification')
                        if isinstance(ts_execution.get('failure_classification'), dict) else
                        (repair_attempts[-1].get('failure_classification') if repair_attempts and isinstance(repair_attempts[-1], dict) else {})
                    ),
                },
                {
                    'name': 'healer_patch_rerun_loop',
                    'passed': bool(ts_execution.get('status') == 'success' or ts_execution.get('repair_round_count') is not None),
                    'evidence': {'attempts': len(repair_attempts), 'repair_round_count': ts_execution.get('repair_round_count') or 0},
                },
                {
                    'name': 'fresh_state_reobserve_for_repair',
                    'passed': bool(ts_execution.get('status') == 'success'),
                    'evidence': {
                        'has_failure_reobserve': has_failure_reobserve,
                        'reobserve_is_diagnostic_only': True,
                    },
                },
                {
                    'name': 'human_review_queue_for_low_confidence_healing',
                    'passed': isinstance(review_queue, list),
                    'evidence': {'review_queue_count': len(review_queue)},
                },
                {
                    'name': 'human_and_machine_reports',
                    'passed': True,
                    'evidence': {'html': True, 'junit': True, 'json': True},
                },
            ],
        }
        recommended_architecture['status'] = (
            'passed'
            if all(item.get('passed') for item in recommended_architecture['checks'])
            else 'partial'
        )
        manual_cleanup_notes = (
            data_lifecycle.get('manual_cleanup_notes')
            if isinstance(data_lifecycle.get('manual_cleanup_notes'), list) else []
        )
        final_spec_success = bool(ts_spec and self.typescript_spec_enabled and ts_execution.get('status') == 'success')
        ci_gate_violations: list[str] = []
        ci_gate_warnings: list[str] = []
        if status != 'success':
            ci_gate_violations.append(f'任务状态不是 success: {status}')
        if failed_steps > 0 and not final_spec_success:
            ci_gate_violations.append(f'存在失败步骤: {failed_steps}')
        if unresolved_step_count > 0:
            ci_gate_violations.append(f'存在未覆盖/未解析计划步骤: {unresolved_step_count}')
        if review_queue_count > 0:
            if final_spec_success:
                ci_gate_warnings.append(f'存在待人审定位修复项: {review_queue_count}')
            else:
                ci_gate_violations.append(f'存在待人审定位修复项: {review_queue_count}')
        if ts_validation.get('status') in {'missing', 'failed'}:
            ci_gate_violations.append(f'TypeScript spec 校验未通过: {ts_validation.get("status")}')
        elif ts_validation.get('status') == 'warning':
            ci_gate_warnings.extend(ts_validation.get('warnings') or ['TypeScript spec 校验存在告警'])
        if ts_spec and self.typescript_spec_enabled and ts_execution.get('status') != 'success':
            ci_gate_violations.append(f'TypeScript spec 真实执行未通过: {ts_execution.get("status") or "unknown"}')
        if ts_spec and not self.typescript_spec_enabled:
            ci_gate_warnings.append('typescript_spec.enabled=false，当前未真实运行生成的 TypeScript spec')
        if manual_cleanup_notes:
            ci_gate_violations.append(f'存在人工清理要求: {len(manual_cleanup_notes)}')

        ci_quality_gate = {
            'status': 'passed' if not ci_gate_violations else 'failed',
            'requires_review': bool(review_queue_count or manual_cleanup_notes),
            'max_failed_steps': 0,
            'unresolved_step_count': unresolved_step_count,
            'review_queue_count': review_queue_count,
            'typescript_spec_validation_status': ts_validation.get('status') or '',
            'typescript_spec_execution_status': ts_execution.get('status') or '',
            'cleanup_required': bool(data_lifecycle.get('cleanup_required')),
            'violations': ci_gate_violations,
            'warnings': ci_gate_warnings,
            'checks': [
                {'name': 'task_success', 'passed': status == 'success', 'actual': status},
                {'name': 'no_failed_steps', 'passed': failed_steps <= 0 or final_spec_success, 'actual': failed_steps, 'threshold': 0},
                {'name': 'plan_fully_resolved', 'passed': unresolved_step_count <= 0, 'actual': unresolved_step_count, 'threshold': 0},
                {'name': 'no_pending_review', 'passed': review_queue_count <= 0 or final_spec_success, 'actual': review_queue_count, 'threshold': 0},
                {'name': 'typescript_spec_valid', 'passed': ts_validation.get('status') in {'success', 'warning'}, 'actual': ts_validation.get('status') or ''},
                {'name': 'typescript_spec_executed', 'passed': not ts_spec or not self.typescript_spec_enabled or ts_execution.get('status') == 'success', 'actual': ts_execution.get('status') or ''},
                {'name': 'no_manual_cleanup', 'passed': not manual_cleanup_notes, 'actual': len(manual_cleanup_notes), 'threshold': 0},
            ],
        }

        gate_failed_message = '; '.join(ci_gate_violations or failed_messages)
        failure_xml = ''
        if ci_quality_gate['status'] != 'passed':
            failure_xml = f'<failure message="{esc(gate_failed_message)}">{esc(gate_failed_message)}</failure>'
        junit_xml = (
            f'<testsuite name="AI UI Generation CI Gate {esc(task_id)}" tests="1" failures="{1 if ci_quality_gate["status"] != "passed" else 0}" time="{duration:.3f}">'
            f'<testcase classname="ai_ui_generation" name="task_{esc(task_id)}" time="{duration:.3f}">'
            f'{failure_xml}'
            '</testcase>'
            '</testsuite>'
        )
        primary_junit_xml = junit_xml

        html_report = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<title>AI UI Generation Report</title>'
            '<style>body{font-family:Arial,sans-serif;margin:24px;color:#202124}'
            'table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #ddd;padding:8px;text-align:left}'
            'th{background:#f5f5f5}.failed{color:#b00020}.success{color:#137333}</style>'
            '</head><body>'
            f'<h1>AI UI Generation Report - Task {esc(task_id)}</h1>'
            f'<p>Status: <strong class="{esc(status)}">{esc(status)}</strong></p>'
            f'<p>Total steps: {total_steps}, Failed: {failed_steps}, Repaired: {repaired_steps}, Duration: {duration:.2f}s</p>'
            f'<p>Trace: {esc(verification.get("trace_path") or flow_execution.get("trace_path") or "")}</p>'
            f'<h2>AI Decision Summary</h2>'
            f'<p>Goal: {esc(intent.get("goal") or "")}</p>'
            f'<p>Assumptions: {esc("; ".join(intent.get("assumptions") or []))}</p>'
            f'<p>Plan validation: {esc(plan_validation.get("status") or "")}, '
            f'unresolved steps: {esc(plan_validation.get("unresolved_step_count") or 0)}, '
            f'risky steps: {esc(plan_validation.get("risky_step_count") or 0)}</p>'
            f'<p>Review queue: {len(review_queue)} item(s), TypeScript spec: {"yes" if script_artifacts.get("playwright_ts_spec") else "no"}</p>'
            f'<p>Page state provider: {esc(mcp_protocol.get("provider") or "playwright-native")} - {esc(mcp_protocol.get("capability_note") or "")}</p>'
            f'<p>TypeScript spec validation: {esc((script_artifacts.get("typescript_spec_validation") or {}).get("status") or "")}</p>'
            f'<p>TypeScript spec execution: {esc(ts_execution.get("status") or "")}, '
            f'attempts: {len(ts_attempts)}, repairs: {esc(ts_execution.get("repair_round_count") or 0)}</p>'
            f'<p>Recommended architecture compliance: {esc(recommended_architecture.get("status"))}</p>'
            f'<p>CI quality gate: {esc(ci_quality_gate.get("status"))}, requires review: {esc(ci_quality_gate.get("requires_review"))}</p>'
            f'<h2>Recommended Architecture Compliance</h2><pre>{esc(json.dumps(recommended_architecture, ensure_ascii=False, indent=2))}</pre>'
            f'<h2>CI Quality Gate</h2><pre>{esc(json.dumps(ci_quality_gate, ensure_ascii=False, indent=2))}</pre>'
            f'<h2>Runtime Evidence</h2><div style="display:flex;gap:12px;flex-wrap:wrap">{"".join(evidence_items)}</div>'
            f'<h2>Network Failures</h2><table><thead><tr><th>Method</th><th>Status</th><th>Type</th><th>URL</th></tr></thead><tbody>{"".join(network_failed_rows)}</tbody></table>'
            f'<h2>Repair Diff</h2><table><thead><tr><th>Type</th><th>Step</th><th>Reason</th><th>Old</th><th>New</th></tr></thead><tbody>{"".join(repair_diff_rows)}</tbody></table>'
            f'<h2>Runtime Artifacts</h2><pre>{esc(json.dumps(runtime_artifacts, ensure_ascii=False, indent=2))}</pre>'
            f'<h2>Step Results</h2><table><thead><tr><th>#</th><th>Operation</th><th>Target</th><th>Status</th><th>Category</th><th>Message</th><th>Tool Context</th><th>Screenshot</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
            f'<h2>Data Lifecycle</h2><pre>{esc(json.dumps(data_lifecycle, ensure_ascii=False, indent=2))}</pre>'
            f'<h2>Network Summary</h2><pre>{esc(json.dumps({"typescript_execution": ts_network_summary, "element_map_pages": [page.get("network_summary") for page in (element_map.get("pages") or [])[:8] if isinstance(page, dict)]}, ensure_ascii=False, indent=2))}</pre>'
            f'<h2>TypeScript Spec Validation</h2><pre>{esc(json.dumps(script_artifacts.get("typescript_spec_validation") or {}, ensure_ascii=False, indent=2))}</pre>'
            f'<h2>TypeScript Spec Execution</h2><pre>{esc(json.dumps({key: value for key, value in ts_execution.items() if key != "junit_xml"}, ensure_ascii=False, indent=2))}</pre>'
            f'<h2>Repair History</h2><pre>{esc(json.dumps(repair_history[-20:], ensure_ascii=False, indent=2))}</pre>'
            f'<h2>Review Queue</h2><pre>{esc(json.dumps(review_queue[-20:], ensure_ascii=False, indent=2))}</pre>'
            '</body></html>'
        )

        verification['report_artifacts'] = {
            'summary': {
                'status': status,
                'total_steps': total_steps,
                'failed_steps': failed_steps,
                'repaired_steps': repaired_steps,
                'duration': duration,
                'trace_path': verification.get('trace_path') or flow_execution.get('trace_path'),
                'failure_category': result.get('failure_category') or '',
                'review_queue_count': len(review_queue),
                'has_typescript_spec': bool(script_artifacts.get('playwright_ts_spec')),
                'typescript_spec_validation_status': (script_artifacts.get('typescript_spec_validation') or {}).get('status') or '',
                'typescript_spec_execution_status': ts_execution.get('status') or '',
                'typescript_spec_trace_path': ts_execution.get('trace_path') or '',
                'typescript_runtime_trace_path': trace_artifact.get('path') or '',
                'typescript_runtime_screenshot_count': artifact_counts.get('screenshots') or 0,
                'typescript_runtime_video_count': artifact_counts.get('videos') or 0,
                'typescript_runtime_attachment_count': artifact_counts.get('attachments') or 0,
                'typescript_runtime_network_total': ts_network_summary.get('total') or 0,
                'typescript_runtime_network_failed': len(ts_network_summary.get('failed') or []),
                'typescript_spec_attempt_count': len(ts_attempts),
                'typescript_spec_repair_round_count': ts_execution.get('repair_round_count') or 0,
                'typescript_spec_failure_classification': (
                    (ts_attempts[-1].get('failure_classification') or {})
                    if ts_attempts and isinstance(ts_attempts[-1], dict) else {}
                ),
                'mcp_provider': mcp_protocol.get('provider') or '',
                'data_lifecycle_strategy': data_lifecycle.get('strategy') or '',
                'cleanup_required': bool(data_lifecycle.get('cleanup_required')),
                'ci_quality_gate': ci_quality_gate,
                'recommended_architecture': recommended_architecture,
                'runtime_artifacts': runtime_artifacts,
            },
            'html': html_report,
            'junit_xml': primary_junit_xml,
            'flow_junit_xml': junit_xml,
            'typescript_junit_xml': ts_execution.get('junit_xml') or '',
        }
        result['verification_result'] = verification

    @staticmethod
    def _structural_plan_steps(payload: dict) -> list[dict[str, Any]]:
        generated_case = payload.get('generated_case') if isinstance(payload.get('generated_case'), dict) else {}
        test_plan = payload.get('test_plan') if isinstance(payload.get('test_plan'), dict) else {}
        steps = generated_case.get('steps') if isinstance(generated_case.get('steps'), list) else None
        if steps is None:
            steps = test_plan.get('steps') if isinstance(test_plan.get('steps'), list) else []
        return [step for step in steps if isinstance(step, dict)]

    @staticmethod
    def _step_text(step: dict[str, Any]) -> str:
        return ' '.join(str(step.get(key) or '') for key in [
            'action', 'operation', 'description', 'target_name', 'value',
        ])

    @classmethod
    def _structural_plan_quality(cls, payload: dict) -> int:
        steps = cls._structural_plan_steps(payload)
        if not steps:
            return 0
        score = len(steps)
        has_goto = False
        has_navigation_click = False
        has_read_assertion = False
        has_form_action = False
        has_post_form_click = False
        first_business_operation = ''
        seen_form_action = False

        for step in steps:
            operation = str(step.get('operation') or step.get('action') or '').strip().lower()
            if operation in {'goto', 'navigate', 'open'}:
                has_goto = True
                continue
            if not first_business_operation:
                first_business_operation = operation
            if operation in {'fill', 'select_option', 'check', 'uncheck'}:
                has_form_action = True
                seen_form_action = True
            elif operation in {'assert_visible', 'assert_text', 'assert_contain_text', 'assert_value'}:
                has_read_assertion = True
            elif operation == 'click':
                if seen_form_action:
                    has_post_form_click = True
                else:
                    has_navigation_click = True

        if has_goto:
            score += 5
        if has_navigation_click:
            score += 30
        if has_read_assertion:
            score += 15
        if has_form_action:
            score += 10
        if has_post_form_click:
            score += 20
        if first_business_operation in {'fill', 'select_option', 'check', 'uncheck'} and not has_navigation_click:
            score -= 40
        return score

    @classmethod
    def _should_apply_llm_structural_plan(cls, current_result: dict, llm_plan: dict) -> tuple[bool, str]:
        """LLM 超时降级时，避免用较弱 fallback 覆盖自动探索得到的完整流程。"""
        if llm_plan.get('llm_enabled'):
            return True, ''

        current_steps = cls._structural_plan_steps(current_result)
        plan_steps = cls._structural_plan_steps(llm_plan)
        if not current_steps:
            return True, '当前无规则规划步骤，采用 LLM 降级规划'
        if not plan_steps:
            return False, 'LLM 降级规划缺少步骤，保留原生 Playwright 规则规划'

        current_quality = cls._structural_plan_quality(current_result)
        plan_quality = cls._structural_plan_quality(llm_plan)
        if plan_quality >= current_quality:
            return True, f'LLM 降级规划质量不低于当前规则规划: {plan_quality}>={current_quality}'
        return False, f'LLM 降级规划质量低于当前规则规划: {plan_quality}<{current_quality}'

    async def _plan_ai_generation_with_llm(self, args: dict, result: dict, phase: str = 'initial') -> dict:
        """采集完成后同步等待后端 LLM 规划，并把规划产物作为唯一执行来源。"""
        task_id = args.get('task_id') or result.get('task_id')
        if not task_id or not result.get('element_map'):
            return result

        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        coverage = element_map.get('exploration_coverage') if isinstance(element_map.get('exploration_coverage'), dict) else {}
        planning_allowed = (
            coverage.get('status') == 'success'
            or bool(coverage.get('planning_allowed'))
        )
        if phase == 'initial' and not planning_allowed:
            missing = coverage.get('missing_stages') if isinstance(coverage.get('missing_stages'), list) else []
            coverage_failure_category = str(coverage.get('failure_category') or '').strip()
            coverage_reason = str(coverage.get('reason') or '').strip()
            result['status'] = 'failed'
            result['failure_category'] = coverage_failure_category or 'exploration_coverage'
            result['message'] = (
                coverage_reason
                or '页面探索未覆盖 LLM 规划所需的业务目标阶段'
                + (f": {', '.join(str(item) for item in missing)}" if missing else '')
            )
            return result

        payload = {
            **self._build_llm_plan_payload(result),
            'background_executor': True,
        }
        if phase != 'initial':
            payload['planning_phase'] = phase
            previous_test_plan = result.get('test_plan') if isinstance(result.get('test_plan'), dict) else {}
            previous_generated_case = result.get('generated_case') if isinstance(result.get('generated_case'), dict) else {}
            payload['previous_candidate_plan'] = {
                'test_plan': {
                    key: value for key, value in previous_test_plan.items()
                    if key not in {'playwright_ts_spec', 'playwright_ts_files'}
                },
                'generated_case': {
                    key: value for key, value in previous_generated_case.items()
                    if key not in {
                        'playwright_ts_spec', 'playwright_ts_files', 'playwright_ts_spec_hash'
                    }
                },
                'plan_validation': (
                    (result.get('verification_result') or {}).get('plan_validation')
                    if isinstance(result.get('verification_result'), dict) else {}
                ) or {},
            }
            payload['repair_instruction'] = (
                '上一次 TypeScript 真实执行失败。请对照 previous_candidate_plan、'
                'verification_result.typescript_execution、failure_reobserve 和元素地图中的 '
                'runtime_fresh 页面重新规划完整步骤并生成全新 spec；不要局部 patch 旧步骤。'
            )
        plan_response = await self._api_post(
            f"/api/ui-automation/ai-generation-tasks/{task_id}/llm-plan/",
            payload,
            timeout=720.0,
        )
        repair_history = result.get('repair_history') or []

        if not isinstance(plan_response, dict):
            result['status'] = 'failed'
            result['failure_category'] = 'llm'
            result['message'] = '后端 LLM 规划接口未返回可用结果，已停止执行，未使用规则兜底脚本'
            repair_history.append({
                'round': len(repair_history),
                'type': 'llm_business_planning' if phase == 'initial' else 'llm_full_replanning',
                'status': 'failed',
                'message': result['message'],
            })
            result['repair_history'] = repair_history
            return result

        plan_response = plan_response.get('plan') if isinstance(plan_response.get('plan'), dict) else plan_response
        llm_enabled = bool(plan_response.get('llm_enabled'))
        generated_case = plan_response.get('generated_case') if isinstance(plan_response.get('generated_case'), dict) else {}
        test_plan = plan_response.get('test_plan') if isinstance(plan_response.get('test_plan'), dict) else {}
        steps = (
            generated_case.get('steps')
            if isinstance(generated_case.get('steps'), list)
            else test_plan.get('steps') if isinstance(test_plan.get('steps'), list) else []
        )
        verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
        verification['llm_planning'] = {
            'status': 'success' if llm_enabled and steps else 'failed',
            'llm_enabled': llm_enabled,
            'llm_error': plan_response.get('llm_error') or '',
            'llm_config': plan_response.get('llm_config') or None,
            'step_count': len(steps),
            'plan_validation': plan_response.get('plan_validation') or {},
            'planning_pipeline': (
                plan_response.get('planning_pipeline')
                if isinstance(plan_response.get('planning_pipeline'), dict)
                else {}
            ),
        }
        if isinstance(plan_response.get('intent'), dict):
            verification['llm_intent'] = plan_response['intent']
        if isinstance(plan_response.get('plan_validation'), dict):
            verification['plan_validation'] = plan_response['plan_validation']

        if self._is_exploration_coverage_gate_response(plan_response):
            return self._record_exploration_coverage_gate_failure(
                result,
                plan_response,
                verification,
                repair_history,
                phase,
            )

        if not llm_enabled or not steps:
            result['status'] = 'failed'
            result['failure_category'] = str(plan_response.get('failure_category') or '').strip() or 'llm'
            result['message'] = (
                plan_response.get('llm_error')
                or plan_response.get('error')
                or 'LLM 未生成可执行步骤，已停止执行，未使用规则兜底脚本'
            )
            result['verification_result'] = verification
            repair_history.append({
                'round': len(repair_history),
                'type': 'llm_business_planning' if phase == 'initial' else 'llm_full_replanning',
                'status': 'failed',
                'message': result['message'],
                'llm_enabled': llm_enabled,
                'llm_error': plan_response.get('llm_error') or '',
            })
            result['repair_history'] = repair_history
            return result

        plan_validation = plan_response.get('plan_validation') if isinstance(plan_response.get('plan_validation'), dict) else {}
        if plan_validation.get('status') == 'failed':
            semantic_issues = plan_validation.get('semantic_issues') if isinstance(plan_validation.get('semantic_issues'), list) else []
            unresolved_steps = plan_validation.get('unresolved_steps') if isinstance(plan_validation.get('unresolved_steps'), list) else []
            issue_messages = [
                str(item.get('message') or item.get('reason') or '')
                for item in [*semantic_issues, *unresolved_steps]
                if isinstance(item, dict) and (item.get('message') or item.get('reason'))
            ]
            result['status'] = 'failed'
            result['failure_category'] = 'llm_plan_validation'
            # 保留未通过校验的完整候选计划，供下一轮 LLM 全量重规划使用。
            result['test_plan'] = test_plan
            result['generated_case'] = generated_case
            result['message'] = (
                'LLM 规划未覆盖用户需求或存在未绑定元素，已停止执行: '
                + '；'.join(issue_messages[:3])
                if issue_messages else
                'LLM 规划未通过静态校验，已停止执行'
            )
            result['verification_result'] = verification
            repair_history.append({
                'round': len(repair_history),
                'type': 'llm_business_planning' if phase == 'initial' else 'llm_full_replanning',
                'status': 'failed',
                'message': result['message'],
                'plan_validation': plan_validation,
            })
            result['repair_history'] = repair_history
            return result

        result['test_plan'] = test_plan
        result['generated_case'] = generated_case
        result['target_url'] = args.get('target_url') or ''
        result['environment_config'] = args.get('environment_config') if isinstance(args.get('environment_config'), dict) else {}
        result['safety_policy'] = args.get('safety_policy') if isinstance(args.get('safety_policy'), dict) else {}
        result['max_repair_rounds'] = args.get('max_repair_rounds') if args.get('max_repair_rounds') is not None else result.get('max_repair_rounds')
        if isinstance(plan_response.get('generated_script'), str):
            result['generated_script'] = plan_response['generated_script']
            result['generated_script_hash'] = hashlib.sha256(result['generated_script'].encode('utf-8')).hexdigest()
        script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
        if isinstance(plan_response.get('generated_typescript_files'), dict):
            script_artifacts['playwright_ts_files'] = plan_response['generated_typescript_files']
        if isinstance(plan_response.get('generated_typescript_spec'), str) and plan_response['generated_typescript_spec'].strip():
            script_artifacts['playwright_ts_spec'] = plan_response['generated_typescript_spec']
            script_artifacts['playwright_ts_spec_hash'] = hashlib.sha256(
                plan_response['generated_typescript_spec'].encode('utf-8')
            ).hexdigest()
        verification['script_artifacts'] = script_artifacts
        result['verification_result'] = verification
        result['status'] = 'success'
        result['failure_category'] = ''
        result['message'] = 'LLM 已基于元素地图生成可执行 Playwright 测试计划和脚本'
        repair_history.append({
            'round': len(repair_history),
            'type': 'llm_business_planning' if phase == 'initial' else 'llm_full_replanning',
            'status': 'success',
            'message': result['message'],
            'llm_enabled': llm_enabled,
            'llm_config': plan_response.get('llm_config') or {},
            'step_count': len(steps),
        })
        result['repair_history'] = repair_history
        return result

    @staticmethod
    def _is_exploration_coverage_gate_response(plan_response: dict) -> bool:
        if not isinstance(plan_response, dict):
            return False
        planning_pipeline = (
            plan_response.get('planning_pipeline')
            if isinstance(plan_response.get('planning_pipeline'), dict)
            else {}
        )
        if str(plan_response.get('failure_category') or '').strip() == 'exploration_coverage':
            return True
        if str(planning_pipeline.get('blocked_stage') or '').strip() == 'exploration_coverage_gate':
            return True
        plan_validation = (
            plan_response.get('plan_validation')
            if isinstance(plan_response.get('plan_validation'), dict)
            else {}
        )
        for issue in plan_validation.get('semantic_issues') or []:
            if not isinstance(issue, dict):
                continue
            if str(issue.get('reason') or '').strip() == 'exploration_coverage_required_binding_unresolved':
                return True
        return False

    @staticmethod
    def _plan_response_exploration_coverage(plan_response: dict) -> dict:
        planning_pipeline = (
            plan_response.get('planning_pipeline')
            if isinstance(plan_response.get('planning_pipeline'), dict)
            else {}
        )
        coverage = (
            planning_pipeline.get('exploration_coverage')
            if isinstance(planning_pipeline.get('exploration_coverage'), dict)
            else {}
        )
        if coverage:
            return copy.deepcopy(coverage)
        plan_validation = (
            plan_response.get('plan_validation')
            if isinstance(plan_response.get('plan_validation'), dict)
            else {}
        )
        for issue in plan_validation.get('semantic_issues') or []:
            if not isinstance(issue, dict):
                continue
            missing = issue.get('missing_required_actions')
            if isinstance(missing, list):
                return {
                    'planning_allowed': False,
                    'reason': issue.get('reason') or 'exploration_coverage_required_binding_unresolved',
                    'missing_required_actions': copy.deepcopy(missing),
                    'missing_action_count': len(missing),
                }
        return {}

    def _record_exploration_coverage_gate_failure(
        self,
        result: dict,
        plan_response: dict,
        verification: dict,
        repair_history: list,
        phase: str,
    ) -> dict:
        coverage = self._plan_response_exploration_coverage(plan_response)
        plan_validation = (
            plan_response.get('plan_validation')
            if isinstance(plan_response.get('plan_validation'), dict)
            else {}
        )
        planning_pipeline = (
            plan_response.get('planning_pipeline')
            if isinstance(plan_response.get('planning_pipeline'), dict)
            else {}
        )
        missing = (
            coverage.get('missing_required_actions')
            if isinstance(coverage.get('missing_required_actions'), list)
            else []
        )
        issue_messages = []
        for item in [*(plan_validation.get('semantic_issues') or []), *(plan_validation.get('unresolved_steps') or [])]:
            if isinstance(item, dict) and (item.get('message') or item.get('reason')):
                issue_messages.append(str(item.get('message') or item.get('reason') or ''))
        message = (
            plan_response.get('error')
            or plan_response.get('llm_error')
            or planning_pipeline.get('failed_stage_error')
            or (
                '页面探索未覆盖必需动作的可绑定元素: '
                + '；'.join(
                    f"{item.get('action_id') or ''} {item.get('target') or item.get('target_name') or ''}".strip()
                    for item in missing[:6]
                    if isinstance(item, dict)
                )
                if missing else ''
            )
            or (
                '页面探索未覆盖 LLM 规划所需的业务目标，已停止进入最终规划'
            )
        )

        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        if coverage:
            element_map['llm_exploration_coverage'] = coverage
            result['element_map'] = element_map
        verification['plan_validation'] = plan_validation
        llm_planning = verification.get('llm_planning') if isinstance(verification.get('llm_planning'), dict) else {}
        llm_planning['status'] = 'blocked'
        llm_planning['failure_category'] = 'exploration_coverage'
        llm_planning['planning_pipeline'] = planning_pipeline
        llm_planning['exploration_coverage'] = coverage
        verification['llm_planning'] = llm_planning

        result['status'] = 'failed'
        result['failure_category'] = 'exploration_coverage'
        result['test_plan'] = plan_response.get('test_plan') if isinstance(plan_response.get('test_plan'), dict) else {}
        result['generated_case'] = plan_response.get('generated_case') if isinstance(plan_response.get('generated_case'), dict) else {}
        result['message'] = message
        result['verification_result'] = verification
        repair_history.append({
            'round': len(repair_history),
            'type': 'exploration_coverage_gate',
            'status': 'blocked',
            'message': message,
            'phase': phase,
            'plan_validation': plan_validation,
            'missing_required_actions': missing[:30],
        })
        result['repair_history'] = repair_history
        return result

    @staticmethod
    def _result_exploration_coverage_feedback(result: dict) -> dict:
        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        coverage = element_map.get('llm_exploration_coverage') if isinstance(element_map.get('llm_exploration_coverage'), dict) else {}
        if coverage:
            return coverage
        verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
        llm_planning = verification.get('llm_planning') if isinstance(verification.get('llm_planning'), dict) else {}
        coverage = llm_planning.get('exploration_coverage') if isinstance(llm_planning.get('exploration_coverage'), dict) else {}
        return coverage

    @staticmethod
    def _with_exploration_coverage_feedback(args: dict, coverage: dict, round_index: int) -> dict:
        updated = copy.deepcopy(args)
        updated['llm_exploration_coverage'] = copy.deepcopy(coverage)
        feedback = updated.get('planning_feedback') if isinstance(updated.get('planning_feedback'), dict) else {}
        history = feedback.get('exploration_coverage_rounds') if isinstance(feedback.get('exploration_coverage_rounds'), list) else []
        history.append({
            'round': round_index,
            'coverage': copy.deepcopy(coverage),
        })
        feedback['exploration_coverage_rounds'] = history[-4:]
        updated['planning_feedback'] = feedback
        return updated

    async def _plan_with_coverage_reexploration(self, args: dict, result: dict) -> tuple[dict, dict]:
        safety_policy = args.get('safety_policy') if isinstance(args.get('safety_policy'), dict) else {}
        selected_map = args.get('element_map') if isinstance(args.get('element_map'), dict) else {}
        manual_map_selected = (
            args.get('execution_mode') == 'manual_map'
            or args.get('element_map_source') == 'manual_capture'
            or selected_map.get('capture_mode') == 'manual_capture'
            or selected_map.get('source') == 'manual_capture'
        )
        max_rounds = int(
            args.get('coverage_reexploration_rounds')
            or safety_policy.get('coverage_reexploration_rounds')
            or args.get('max_repair_rounds')
            or safety_policy.get('max_repair_rounds')
            or 2
        )
        max_rounds = max(0, min(max_rounds, 3))
        active_args = args
        for round_index in range(max_rounds + 1):
            result = await self._plan_ai_generation_with_llm(active_args, result)
            if result.get('failure_category') != 'exploration_coverage':
                return active_args, result
            coverage = self._result_exploration_coverage_feedback(result)
            missing = coverage.get('missing_required_actions') if isinstance(coverage.get('missing_required_actions'), list) else []
            if manual_map_selected:
                result['message'] = (
                    'manual_element_map_missing_action_evidence: '
                    + (result.get('message') or '人工采集元素地图缺少 LLM 规划所需动作证据')
                )
                verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
                verification['manual_element_map_missing_action_evidence'] = {
                    'status': 'blocked',
                    'element_map_id': args.get('element_map_id'),
                    'missing_required_actions': missing[:30],
                    'reason': 'manual_capture_mode_does_not_fallback_to_auto_exploration',
                }
                result['verification_result'] = verification
                return active_args, result
            if round_index >= max_rounds or not missing:
                return active_args, result
            active_args = self._with_exploration_coverage_feedback(active_args, coverage, round_index + 1)
            result = await self.executor.run_ai_generation_task(active_args)
        return active_args, result

    @staticmethod
    def _ai_generation_flow_succeeded(result: dict) -> bool:
        verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
        flow_execution = verification.get('flow_execution') if isinstance(verification.get('flow_execution'), dict) else {}
        return result.get('status') == 'success' and flow_execution.get('status') == 'success'

    @staticmethod
    def _has_generated_typescript_spec(result: dict) -> bool:
        verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
        script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
        generated_case = result.get('generated_case') if isinstance(result.get('generated_case'), dict) else {}
        return bool(
            str(script_artifacts.get('playwright_ts_spec') or '').strip()
            or str(generated_case.get('playwright_ts_spec') or '').strip()
            or (
                isinstance(script_artifacts.get('playwright_ts_files'), dict)
                and str(script_artifacts.get('playwright_ts_files', {}).get('generated.spec.ts') or '').strip()
            )
            or (
                isinstance(generated_case.get('playwright_ts_files'), dict)
                and str(generated_case.get('playwright_ts_files', {}).get('generated.spec.ts') or '').strip()
            )
        )

    @staticmethod
    def _task_execution_mode(args: dict) -> str:
        safety_policy = args.get('safety_policy') if isinstance(args.get('safety_policy'), dict) else {}
        mode = str(
            args.get('execution_mode')
            or safety_policy.get('execution_mode')
            or 'contract_planner'
        ).strip().lower()
        if mode in {'runtime_agent', 'runtime', 'agent', 'runtime-mode'}:
            return 'runtime_agent'
        return 'contract_planner'

    def _runtime_authentication_block_reason(
        self,
        args: dict,
        current_state: dict[str, Any],
        auth_observations: list[dict[str, Any]],
    ) -> str:
        try:
            if not self.executor._looks_like_login_page(current_state):
                return ''
            authentication_mode = self.executor._authentication_mode(args, current_state)
        except Exception:
            return ''
        if authentication_mode in {'test_subject', 'authentication_then_business'}:
            return ''
        for item in reversed(auth_observations):
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'flow_auth_setup' and item.get('status') == 'failed':
                return str(item.get('reason') or item.get('error') or '')[:800]
            if item.get('type') == 'flow_auth_setup_skipped':
                return str(item.get('reason') or '')[:800]
        if authentication_mode == 'unknown':
            try:
                return self.executor._authentication_contract_unavailable_reason(args)
            except Exception:
                return '认证合同不可用，runtime_agent 停留在登录页'
        if authentication_mode == 'prerequisite':
            return '前置认证未完成，runtime_agent 当前仍停留在登录页'
        return ''

    @staticmethod
    def _is_runtime_reobserve_step(step: dict[str, Any]) -> bool:
        if not isinstance(step, dict):
            return False
        operation = str(step.get('operation') or step.get('action') or '').strip().lower()
        return bool(
            step.get('internal_runtime_primitive')
            or operation in {'wait_and_reobserve', 'reobserve', 'observe_current_page'}
        )

    @staticmethod
    def _runtime_wait_milliseconds(step: dict[str, Any]) -> int:
        raw_value = step.get('value') if isinstance(step, dict) else None
        try:
            wait_ms = int(float(raw_value))
        except (TypeError, ValueError):
            wait_ms = 1500
        return max(500, min(wait_ms, 5000))

    @staticmethod
    def _is_navigation_context_destroyed_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            'execution context was destroyed' in message
            or 'most likely because of a navigation' in message
            or 'navigation' in message and 'context' in message and 'destroyed' in message
        )

    @staticmethod
    def _is_locator_resolution_timeout_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            'timeout waiting for visible locator match' in message
            or ('matched=0' in message and 'scanned=0' in message and 'locator' in message)
        )

    async def _collect_runtime_page_state(
        self,
        page,
        page_key: str,
        screenshot_path: str,
        *,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                return await self.executor._mcp_collect_page_state(
                    page,
                    page_key,
                    screenshot_path,
                )
            except Exception as exc:
                last_error = exc
                if not self._is_navigation_context_destroyed_error(exc) or attempt >= max_attempts:
                    raise
                logger.info(
                    "runtime_agent 页面采集遇到导航上下文切换，等待后重试: page_key=%s, attempt=%s/%s",
                    page_key,
                    attempt,
                    max_attempts,
                )
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=8000)
                except Exception:
                    pass
                try:
                    await page.wait_for_load_state('networkidle', timeout=2000)
                except Exception:
                    pass
                try:
                    await page.wait_for_timeout(300)
                except Exception:
                    pass
        if last_error:
            raise last_error
        return {}

    @staticmethod
    def _runtime_observation_for_plan(
        current_state: dict[str, Any],
        current_candidates: list[dict[str, Any]],
        *,
        page_key: str,
        fallback_url: str,
    ) -> dict[str, Any]:
        def compact_locator(locator: Any) -> dict[str, Any]:
            if not isinstance(locator, dict):
                return {}
            return {
                key: locator.get(key)
                for key in ('type', 'value', 'name', 'confidence')
                if locator.get(key) not in (None, '')
            }

        def compact_element(element: Any) -> dict[str, Any]:
            if not isinstance(element, dict):
                return {}
            compact = {
                key: element.get(key)
                for key in (
                    'element_key', 'page_key', 'frame_key', 'frame_name', 'frame_url',
                    'name', 'accessible_name', 'text', 'label', 'form_label',
                    'placeholder', 'role', 'tag', 'type', 'input_type', 'actions',
                    'visible', 'enabled', 'context_type', 'source_element_key',
                    'selected_label', 'selected_text', 'selected_value', 'current_value',
                    'checked', 'selected', 'aria_checked', 'aria_selected', 'data_state',
                    'class_name', 'state_text',
                )
                if element.get(key) not in (None, '', [])
            }
            options = [
                {
                    key: option.get(key)
                    for key in ('value', 'label', 'text', 'name', 'disabled', 'selected')
                    if isinstance(option, dict) and option.get(key) not in (None, '')
                }
                for option in (element.get('options') or [])[:20]
                if isinstance(option, dict)
            ]
            if options:
                compact['options'] = options
            recommended = compact_locator(element.get('recommended_locator'))
            if recommended:
                compact['recommended_locator'] = recommended
            locators = [
                compact_locator(item)
                for item in (element.get('locator_candidates') or [])
                if compact_locator(item)
            ][:8]
            if locators:
                compact['locator_candidates'] = locators
            row_scope = element.get('row_scope') if isinstance(element.get('row_scope'), dict) else {}
            if row_scope:
                compact['row_scope'] = {
                    key: row_scope.get(key)
                    for key in ('row_text', 'row_index', 'column_name', 'column_text', 'row_cells')
                    if row_scope.get(key) not in (None, '', {})
                }
            return compact

        elements = [
            compact_element(item)
            for item in (current_state.get('elements') or [])
            if compact_element(item)
        ][:80]
        frames = []
        for frame in (current_state.get('frames') or [])[:12]:
            if not isinstance(frame, dict):
                continue
            frame_elements = [
                compact_element(item)
                for item in (frame.get('elements') or [])
                if compact_element(item)
            ][:80]
            frames.append({
                key: frame.get(key)
                for key in ('frame_key', 'frame_name', 'name', 'url', 'title')
                if frame.get(key) not in (None, '')
            } | {'elements': frame_elements})

        accessibility_snapshot = {}
        raw_ax = current_state.get('accessibility_snapshot')
        if isinstance(raw_ax, dict):
            accessibility_snapshot = {
                'status': raw_ax.get('status'),
                'node_count': raw_ax.get('node_count'),
                'nodes': [
                    {
                        key: node.get(key)
                        for key in ('ax_ref', 'role', 'name', 'value', 'ignored')
                        if isinstance(node, dict) and node.get(key) not in (None, '')
                    }
                    for node in (raw_ax.get('nodes') or [])[:160]
                    if isinstance(node, dict)
                ],
            }

        return {
            'page_key': current_state.get('page_key') or page_key,
            'url': current_state.get('url') or fallback_url,
            'title': current_state.get('title') or '',
            'source': current_state.get('source') or 'runtime_agent_observation',
            'element_count': len(current_state.get('elements') or []),
            'frame_count': len(current_state.get('frames') or []),
            'dialogs': current_state.get('dialogs') or [],
            'network_summary': current_state.get('network_summary') or {},
            'current_candidates': current_candidates,
            'elements': elements,
            'frames': frames,
            'accessibility_snapshot': accessibility_snapshot,
            'snapshot': current_state.get('snapshot') or {},
        }

    @staticmethod
    def _runtime_element_map_for_execution(
        current_state: dict[str, Any],
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        page_key = str(observation.get('page_key') or current_state.get('page_key') or '')
        elements: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(element: Any, *, frame: dict[str, Any] | None = None) -> None:
            if not isinstance(element, dict):
                return
            copied = dict(element)
            if page_key and not copied.get('page_key'):
                copied['page_key'] = page_key
            if frame:
                frame_key = str(frame.get('frame_key') or '').strip()
                source_key = str(copied.get('source_element_key') or copied.get('element_key') or '').strip()
                if frame_key:
                    copied.setdefault('context_type', 'frame')
                    copied.setdefault('frame_key', frame_key)
                    copied.setdefault('frame_name', frame.get('frame_name') or frame.get('name') or '')
                    copied.setdefault('frame_url', frame.get('frame_url') or frame.get('url') or '')
                    if source_key and not str(copied.get('element_key') or '').startswith(f'{frame_key}::'):
                        copied['source_element_key'] = source_key
                        copied['element_key'] = f'{frame_key}::{source_key}'
            element_key = str(copied.get('element_key') or '')
            if not element_key or element_key in seen:
                return
            seen.add(element_key)
            elements.append(copied)

        for element in observation.get('current_candidates') or []:
            add(element)
        for element in observation.get('elements') or []:
            add(element)
        for frame in observation.get('frames') or []:
            if not isinstance(frame, dict):
                continue
            for element in frame.get('elements') or []:
                add(element, frame=frame)
        if not elements:
            for element in current_state.get('elements') or []:
                add(element)
            for frame in current_state.get('frames') or []:
                if not isinstance(frame, dict):
                    continue
                for element in frame.get('elements') or []:
                    add(element, frame=frame)
        return {
            'pages': [{
                'page_key': page_key,
                'url': observation.get('url') or current_state.get('url') or '',
                'title': observation.get('title') or current_state.get('title') or '',
                'elements': elements,
            }],
        }

    async def _plan_runtime_agent_with_llm(
        self,
        args: dict,
        observation: dict,
        runtime_history: list[dict[str, Any]],
        *,
        last_step: dict[str, Any] | None = None,
        last_error: str = '',
        phase: str = 'initial',
    ) -> dict:
        task_id = args.get('task_id')
        if not task_id:
            return {
                'runtime_plan': {
                    'status': 'blocked',
                    'reason': '缺少 task_id',
                    'next_step': {},
                },
                'runtime_context': {'contract_artifacts': False},
            }

        completed_action_ids: set[str] = set()
        for item in runtime_history:
            if not isinstance(item, dict):
                continue
            status = str(item.get('status') or '').strip().lower()
            operation = str(item.get('operation') or item.get('action') or '').strip().lower()
            if status not in {'success', 'executed', 'completed', 'repaired'} or operation in {
                'wait_and_reobserve',
                'reobserve',
                'observe_current_page',
            }:
                continue
            action_id = str(item.get('action_id') or '').strip()
            if action_id:
                completed_action_ids.add(action_id)
            for covered_action_id in item.get('covered_action_ids') or []:
                covered = str(covered_action_id or '').strip()
                if covered:
                    completed_action_ids.add(covered)

        payload = {
            'execution_mode': 'runtime_agent',
            'task_id': task_id,
            'requirement_document': args.get('requirement_document') or {},
            'requirement': args.get('requirement') or '',
            'gherkin': args.get('gherkin') or '',
            'target_url': args.get('target_url') or '',
            'target_module': args.get('target_module') or '',
            'safety_policy': args.get('safety_policy') or {},
            'current_observation': observation or {},
            'current_candidates': observation.get('current_candidates') if isinstance(observation.get('current_candidates'), list) else [],
            'runtime_history': runtime_history[-12:],
            'completed_action_ids': sorted(completed_action_ids),
            'last_step': last_step or {},
            'last_error': last_error,
            'planning_phase': phase,
            'background_executor': True,
        }
        plan_response = await self._api_post(
            f"/api/ui-automation/ai-generation-tasks/{task_id}/llm-plan/",
            payload,
            timeout=720.0,
        )
        if not isinstance(plan_response, dict):
            return {
                'runtime_plan': {
                    'status': 'blocked',
                    'reason': 'runtime_agent 规划接口未返回可用结果',
                    'next_step': {},
                },
                'runtime_context': {'contract_artifacts': False},
            }
        plan_response = plan_response.get('plan') if isinstance(plan_response.get('plan'), dict) else plan_response
        runtime_plan = plan_response.get('runtime_plan') if isinstance(plan_response.get('runtime_plan'), dict) else {}
        runtime_response = {
            key: value
            for key, value in plan_response.items()
            if key not in {'generated_case', 'test_plan', 'plan_validation'}
        }
        return {
            **runtime_response,
            'runtime_plan': runtime_plan,
            'runtime_context': plan_response.get('runtime_context') if isinstance(plan_response.get('runtime_context'), dict) else {},
        }

    async def _run_runtime_agent_ai_generation(self, args: dict) -> dict:
        task_id = args.get('task_id')
        logger.info(f"开始执行 runtime_agent AI UI 生成任务: task_id={task_id}")
        start = time.time()
        safety_policy = args.get('safety_policy') if isinstance(args.get('safety_policy'), dict) else {}
        env_config = args.get('environment_config') if isinstance(args.get('environment_config'), dict) else {}
        base_url = args.get('target_url') or env_config.get('base_url') or ''
        max_steps = int(safety_policy.get('max_steps') or 24)
        max_reobserve_rounds = int(safety_policy.get('max_runtime_reobserve_rounds') or 4)
        observations: list[dict[str, Any]] = []
        runtime_history: list[dict[str, Any]] = []
        generated_steps: list[dict[str, Any]] = []
        repair_history: list[dict[str, Any]] = []
        verification_result: dict[str, Any] = {'runtime_agent_execution': {'status': 'running', 'steps': []}}
        failure_category = ''
        last_error = ''
        current_phase = 'initial'
        consecutive_reobserves = 0
        if not base_url:
            return {
                'task_id': task_id,
                'status': 'failed',
                'message': '缺少 target_url 或环境 base_url',
                'failure_category': 'environment',
                'mcp_observations': observations,
                'repair_history': repair_history,
                'verification_result': verification_result,
                'duration': time.time() - start,
            }

        try:
            async with self.executor.browser_session_with_trace(
                f"ai_generation_runtime_agent_{task_id}",
                ignore_https_errors=self.executor._ignore_https_errors_for_env(env_config),
            ) as page:
                self.executor._setup_page_listeners(page)
                auth_observations = await self.executor._ensure_authenticated_before_ai_flow(
                    page,
                    args,
                    base_url,
                    safety_policy,
                )
                if page.url == 'about:blank':
                    await self.executor._navigate_for_observation(page, base_url)
                elif base_url and page.url != base_url:
                    await self.executor._navigate_for_observation(page, base_url)

                current_state = await self._collect_runtime_page_state(
                    page,
                    'runtime_page_1',
                    f"{self.executor.screenshot_dir}/ai_generation_{task_id}_runtime_page_1.png",
                )
                current_candidates = self.executor._select_task_relevant_elements(args, current_state, limit=20)
                observation = self._runtime_observation_for_plan(
                    current_state,
                    current_candidates,
                    page_key='runtime_page_1',
                    fallback_url=base_url,
                )
                observations.append({
                    'type': 'runtime_agent_snapshot',
                    'page_key': observation['page_key'],
                    'url': observation['url'],
                    'title': observation['title'],
                    'candidate_count': len(current_candidates),
                })
                auth_block_reason = self._runtime_authentication_block_reason(args, current_state, auth_observations)
                if auth_block_reason:
                    trace_path = self.executor.get_current_trace_path()
                    verification_result['runtime_agent_execution'] = {
                        'status': 'failed',
                        'steps': runtime_history,
                        'generated_steps': generated_steps,
                        'auth_setup': auth_observations,
                        'reason': auth_block_reason,
                    }
                    if trace_path:
                        verification_result['trace_path'] = trace_path
                    verification_result['runtime_execution_artifact'] = {
                        'artifact_type': 'runtime_agent_execution',
                        'execution_mode': 'runtime_agent',
                        'objective': (
                            args.get('requirement_document', {}).get('source_requirement')
                            or args.get('requirement_document', {}).get('normalized_text')
                            or args.get('target_module')
                            or args.get('name')
                            or 'AI 生成 UI 用例'
                        ),
                        'steps': generated_steps,
                        'runtime_history': runtime_history,
                        'observation_count': len(observations),
                    }
                    repair_history.append({
                        'round': len(repair_history) + 1,
                        'type': 'runtime_agent_authentication_blocked',
                        'status': 'blocked',
                        'reason': auth_block_reason,
                        'phase': current_phase,
                    })
                    observations.append({
                        'type': 'runtime_agent_authentication_blocked',
                        'reason': auth_block_reason,
                        'url': observation['url'],
                    })
                    return {
                        'task_id': task_id,
                        'status': 'failed',
                        'message': auth_block_reason,
                        'execution_mode': 'runtime_agent',
                        'generated_script': '',
                        'generated_script_hash': '',
                        'mcp_observations': observations,
                        'verification_result': verification_result,
                        'repair_history': repair_history,
                        'failure_category': 'permission',
                        'current_repair_round': len(repair_history),
                        'duration': time.time() - start,
                    }

                for round_index in range(max_steps):
                    plan_response = await self._plan_runtime_agent_with_llm(
                        args,
                        self._compact_llm_payload_value(observation),
                        runtime_history,
                        last_step=runtime_history[-1] if runtime_history else {},
                        last_error=last_error,
                        phase=current_phase,
                    )
                    runtime_plan = plan_response.get('runtime_plan') if isinstance(plan_response.get('runtime_plan'), dict) else {}
                    next_step = runtime_plan.get('next_step') if isinstance(runtime_plan.get('next_step'), dict) else {}
                    runtime_status = str(runtime_plan.get('status') or '').strip().lower()
                    if runtime_status == 'completed':
                        if not generated_steps and consecutive_reobserves < max_reobserve_rounds:
                            next_step = {
                                'action_id': f'runtime_wait_{round_index + 1}',
                                'step_sort': round_index,
                                'operation': 'wait_and_reobserve',
                                'action': 'wait_and_reobserve',
                                'target_name': 'runtime_page_render',
                                'description': runtime_plan.get('reason') or '等待页面渲染后重新观察当前页面',
                                'value': 1500,
                                'internal_runtime_primitive': True,
                            }
                            runtime_status = 'continue'
                        elif not generated_steps:
                            failure_category = 'runtime_agent_plan_invalid'
                            last_error = 'runtime_agent LLM 标记完成但未产生任何可执行步骤'
                            repair_history.append({
                                'round': len(repair_history) + 1,
                                'type': 'runtime_agent_plan_invalid',
                                'status': 'failed',
                                'reason': last_error,
                                'phase': current_phase,
                            })
                            verification_result['runtime_agent_execution'] = {
                                'status': 'failed',
                                'steps': runtime_history,
                                'generated_steps': generated_steps,
                                'auth_setup': auth_observations,
                                'reason': last_error,
                            }
                            break
                        else:
                            verification_result['runtime_agent_execution'] = {
                                'status': 'success',
                                'steps': runtime_history,
                                'generated_steps': generated_steps,
                                'auth_setup': auth_observations,
                                'reason': runtime_plan.get('reason') or '所有必需动作已完成',
                            }
                            break
                    if runtime_status == 'blocked':
                        failure_category = 'runtime_agent_blocked'
                        last_error = str(runtime_plan.get('reason') or 'runtime_agent 规划被阻塞')
                        runtime_history.append({
                            'round': round_index + 1,
                            'status': 'blocked',
                            'reason': last_error,
                            'observation': observation,
                        })
                        repair_history.append({
                            'round': len(repair_history) + 1,
                            'type': 'runtime_agent_blocked',
                            'status': 'blocked',
                            'reason': last_error,
                            'phase': current_phase,
                        })
                        verification_result['runtime_agent_execution'] = {
                            'status': 'failed',
                            'steps': runtime_history,
                            'generated_steps': generated_steps,
                            'auth_setup': auth_observations,
                            'reason': last_error,
                        }
                        break
                    if not next_step:
                        if consecutive_reobserves < max_reobserve_rounds:
                            next_step = {
                                'action_id': f'runtime_wait_{round_index + 1}',
                                'step_sort': round_index,
                                'operation': 'wait_and_reobserve',
                                'action': 'wait_and_reobserve',
                                'target_name': 'runtime_page_render',
                                'description': runtime_plan.get('reason') or 'LLM 未返回下一步，等待后重新观察当前页面',
                                'value': 1500,
                                'internal_runtime_primitive': True,
                            }
                        else:
                            failure_category = 'runtime_agent_plan_invalid'
                            last_error = 'runtime_agent LLM 多次未返回可执行下一步'
                            repair_history.append({
                                'round': len(repair_history) + 1,
                                'type': 'runtime_agent_plan_invalid',
                                'status': 'failed',
                                'reason': last_error,
                                'phase': current_phase,
                            })
                            verification_result['runtime_agent_execution'] = {
                                'status': 'failed',
                                'steps': runtime_history,
                                'generated_steps': generated_steps,
                                'auth_setup': auth_observations,
                                'reason': last_error,
                            }
                            break

                    step = dict(next_step)
                    step_started = time.time()
                    is_reobserve_step = self._is_runtime_reobserve_step(step)
                    locator_timeout_failure = False
                    if is_reobserve_step:
                        wait_ms = self._runtime_wait_milliseconds(step)
                        try:
                            await page.wait_for_load_state('domcontentloaded', timeout=5000)
                        except Exception:
                            pass
                        try:
                            await page.wait_for_load_state('networkidle', timeout=wait_ms)
                        except Exception:
                            pass
                        await page.wait_for_timeout(wait_ms)
                        success, message, locator_used = True, f'等待 {wait_ms}ms 后重新观察页面', ''
                        consecutive_reobserves += 1
                    else:
                        try:
                            runtime_element_map = self._runtime_element_map_for_execution(current_state, observation)
                            success, message, locator_used = await self.executor._execute_ai_plan_step(
                                page,
                                step,
                                base_url,
                                safety_policy,
                                runtime_element_map,
                            )
                            consecutive_reobserves = 0
                        except Exception as exc:
                            if not self._is_locator_resolution_timeout_error(exc):
                                raise
                            success = False
                            locator_used = None
                            message = f'定位失败，当前页面未找到可见目标: {exc}'
                            locator_timeout_failure = True
                            failure_category = 'locator'
                            last_error = message
                            repair_history.append({
                                'round': len(repair_history) + 1,
                                'type': 'runtime_agent_locator_unresolved',
                                'status': 'failed',
                                'reason': message,
                                'phase': current_phase,
                                'step': {
                                    'action_id': step.get('action_id') or f'runtime_step_{round_index + 1}',
                                    'operation': self.executor._normalise_ai_plan_operation(step),
                                    'page_key': step.get('page_key') or observation.get('page_key') or '',
                                    'element_key': step.get('element_key') or '',
                                    'target_name': step.get('target_name') or '',
                                    'description': step.get('description') or '',
                                    'locator_hint': step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {},
                                },
                            })
                            logger.info(
                                "runtime_agent 定位失败，回到下一轮观察和重规划: page_key=%s, target=%s, locator=%s",
                                step.get('page_key') or observation.get('page_key') or '',
                                step.get('target_name') or step.get('element_key') or '',
                                step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {},
                            )
                    current_step_record = {
                        'round': round_index + 1,
                        'action_id': step.get('action_id') or f'runtime_step_{round_index + 1}',
                        'step_sort': step.get('step_sort', round_index),
                        'operation': self.executor._normalise_ai_plan_operation(step),
                        'page_key': step.get('page_key') or observation.get('page_key') or '',
                        'element_key': step.get('element_key') or '',
                        'target_name': step.get('target_name') or '',
                        'description': step.get('description') or '',
                        'locator_hint': step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {},
                        'status': 'success' if success else 'failed',
                        'message': message,
                        'duration': time.time() - step_started,
                    }
                    if locator_used:
                        current_step_record['locator'] = locator_used
                    runtime_history.append(current_step_record)
                    if success:
                        if not is_reobserve_step:
                            generated_steps.append({
                                **step,
                                'step_sort': len(generated_steps),
                                'action_id': current_step_record['action_id'],
                                'locator_hint': current_step_record['locator_hint'] or step.get('locator_hint') or {},
                            })
                        last_error = ''
                    else:
                        if not locator_timeout_failure:
                            failure_category = 'runtime_agent_step_failed'
                            last_error = message
                            repair_history.append({
                                'round': len(repair_history) + 1,
                                'type': 'runtime_agent_step_failed',
                                'status': 'failed',
                                'reason': message,
                                'phase': current_phase,
                                'step': current_step_record,
                            })

                    current_state = await self._collect_runtime_page_state(
                        page,
                        f'runtime_page_{round_index + 2}',
                        f"{self.executor.screenshot_dir}/ai_generation_{task_id}_runtime_page_{round_index + 2}.png",
                    )
                    current_candidates = self.executor._select_task_relevant_elements(args, current_state, limit=20)
                    observation = self._runtime_observation_for_plan(
                        current_state,
                        current_candidates,
                        page_key=f'runtime_page_{round_index + 2}',
                        fallback_url=page.url,
                    )
                    observations.append({
                        'type': 'runtime_agent_snapshot',
                        'page_key': observation['page_key'],
                        'url': observation['url'],
                        'title': observation['title'],
                        'candidate_count': len(current_candidates),
                    })
                    current_phase = f'runtime_step_{round_index + 1}'
                else:
                    failure_category = failure_category or 'runtime_agent_step_limit'
                    verification_result['runtime_agent_execution'] = {
                        'status': 'failed',
                        'steps': runtime_history,
                        'generated_steps': generated_steps,
                        'auth_setup': auth_observations,
                        'reason': 'runtime_agent 超过最大步骤数仍未完成',
                    }

                trace_path = self.executor.get_current_trace_path()
                if trace_path:
                    verification_result['trace_path'] = trace_path
                runtime_status = verification_result.get('runtime_agent_execution', {}).get('status') if isinstance(verification_result.get('runtime_agent_execution'), dict) else 'failed'
                result_status = 'success' if runtime_status == 'success' and generated_steps else 'failed'
                objective = (
                    args.get('requirement_document', {}).get('source_requirement')
                    or args.get('requirement_document', {}).get('normalized_text')
                    or args.get('target_module')
                    or args.get('name')
                    or 'AI 生成 UI 用例'
                )
                runtime_execution_artifact = {
                    'artifact_type': 'runtime_agent_execution',
                    'execution_mode': 'runtime_agent',
                    'objective': objective,
                    'steps': generated_steps,
                    'runtime_history': runtime_history,
                    'observation_count': len(observations),
                }
                verification_result['runtime_execution_artifact'] = runtime_execution_artifact
                generated_script = self.executor._build_mcp_script(args, {'base_url': base_url}, {'steps': generated_steps}) if generated_steps else ''
                return {
                    'task_id': task_id,
                    'status': result_status,
                    'message': (
                        'runtime_agent 逐步规划并执行成功'
                        if result_status == 'success'
                        else last_error or 'runtime_agent 逐步规划失败'
                    ),
                    'execution_mode': 'runtime_agent',
                    'generated_script': generated_script,
                    'generated_script_hash': hashlib.sha256(generated_script.encode('utf-8')).hexdigest() if generated_script else '',
                    'mcp_observations': observations,
                    'verification_result': verification_result,
                    'repair_history': repair_history,
                    'failure_category': '' if result_status == 'success' else failure_category or 'runtime_agent',
                    'current_repair_round': len(repair_history),
                    'duration': time.time() - start,
                }
        except Exception as exc:
            logger.error(f"runtime_agent AI UI 生成任务执行异常: task_id={task_id}, error={exc}", exc_info=True)
            return {
                'task_id': task_id,
                'status': 'failed',
                'message': f'{type(exc).__name__}: {exc}',
                'execution_mode': 'runtime_agent',
                'failure_category': 'unknown',
                'repair_history': [{
                    'round': 0,
                    'type': 'executor_exception',
                    'message': f'{type(exc).__name__}: {exc}',
                }],
                'mcp_observations': observations,
                'verification_result': verification_result,
                'duration': time.time() - start,
            }

    async def _verify_ai_generation_flow_with_llm_replanning(self, args: dict, result: dict) -> dict:
        """LLM 规划后先真实执行；失败时把失败证据回灌 LLM 重新规划完整流程。"""
        safety_policy = args.get('safety_policy') if isinstance(args.get('safety_policy'), dict) else {}
        max_repair_rounds = int(
            args.get('max_repair_rounds')
            or safety_policy.get('max_repair_rounds')
            or result.get('max_repair_rounds')
            or 2
        )
        max_attempts = max(1, min(max_repair_rounds + 1, 4))
        for attempt_index in range(max_attempts):
            result = await self.executor.verify_ai_generated_flow(args, result)
            if self._ai_generation_flow_succeeded(result):
                return result
            if attempt_index + 1 >= max_attempts:
                return result
            result = await self._plan_ai_generation_with_llm(
                args,
                result,
                phase=f'flow_replan_attempt_{attempt_index + 1}',
            )
            if result.get('status') != 'success':
                return result
        return result

    async def execute_ai_generation(self, args: dict):
        """执行 AI 生成、Playwright 原生探索、LLM 规划、真实验证与修复任务。"""
        task_id = args.get('task_id')
        logger.info(f"开始执行 AI UI 生成任务: task_id={task_id}")
        try:
            if self._task_execution_mode(args) == 'runtime_agent':
                result = await self._run_runtime_agent_ai_generation(args)
            else:
                result = await self.executor.run_ai_generation_task(args)
                args, result = await self._plan_with_coverage_reexploration(args, result)
            if self._task_execution_mode(args) != 'runtime_agent' and result.get('status') == 'success':
                safety_policy = args.get('safety_policy') if isinstance(args.get('safety_policy'), dict) else {}
                max_replan_rounds = int(
                    args.get('max_repair_rounds')
                    or safety_policy.get('max_repair_rounds')
                    or result.get('max_repair_rounds')
                    or 2
                )
                max_replan_rounds = max(0, min(max_replan_rounds, 4))

                for replan_round in range(max_replan_rounds + 1):
                    # 只有没有 TypeScript artifact 的兼容场景才走 native flow
                    # 验证；生成了 spec 后，TypeScript 真实执行是唯一裁决来源。
                    if not self._has_generated_typescript_spec(result):
                        result = await self._verify_ai_generation_flow_with_llm_replanning(args, result)
                    result = await self._process_ai_generation_artifacts(args, result)

                    verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
                    script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
                    ts_execution = script_artifacts.get('typescript_spec_execution') if isinstance(script_artifacts.get('typescript_spec_execution'), dict) else {}
                    if result.get('status') == 'success' and ts_execution.get('status') in {'success', 'skipped', ''}:
                        break
                    if replan_round >= max_replan_rounds:
                        break

                    # 失败后只回灌完整执行证据，重新生成 test_plan、generated_case
                    # 和 generated.spec.ts。严禁调用局部 TypeScript patch 链。
                    result = await self._plan_ai_generation_with_llm(
                        args,
                        result,
                        phase=f'full_replan_round_{replan_round + 1}',
                    )
                    if result.get('status') != 'success':
                        break
        except Exception as e:
            logger.error(f"AI UI 生成任务执行异常: task_id={task_id}, error={e}", exc_info=True)
            result = {
                'task_id': task_id,
                'status': 'failed',
                'message': f"{type(e).__name__}: {e}",
                'failure_category': 'unknown',
                'repair_history': [{
                    'round': 0,
                    'type': 'executor_exception',
                    'message': f"{type(e).__name__}: {e}",
                }],
            }
        submitted = await self._submit_ai_generation_result(result)
        if not submitted:
            logger.error(f"AI UI 生成任务结果上报失败: task_id={task_id}")
        logger.info(f"AI UI 生成任务完成: task_id={task_id}, status={result.get('status')}")
    
    async def stop_execution(self, args: dict):
        """停止执行"""
        logger.info("收到停止执行请求")
        self.executor.stop()
        # 清空任务队列
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
                self.task_queue.task_done()
            except asyncio.QueueEmpty:
                break
    
    async def _fetch_page_step(self, page_step_id: int) -> Optional[dict]:
        """从API获取页面步骤详情（含元素定位信息，用于执行）"""
        return await self._api_get(f"/api/ui-automation/page-steps/{page_step_id}/execute-data/")
    
    async def _fetch_test_case(self, case_id: int) -> Optional[dict]:
        """从API获取测试用例详情（含完整步骤详情）"""
        return await self._api_get(f"/api/ui-automation/testcases/{case_id}/execute-data/")
    
    async def _fetch_env_config(self, env_config_id: int) -> Optional[dict]:
        """从API获取环境配置"""
        return await self._api_get(f"/api/ui-automation/env-configs/{env_config_id}/")
    
    async def _fetch_default_env_config(self, project_id: int) -> Optional[dict]:
        """从API获取项目的默认环境配置"""
        result = await self._api_get(f"/api/ui-automation/env-configs/?project={project_id}&is_default=true")
        logger.debug(f"获取默认环境配置: project_id={project_id}, result={result}")

        if not result:
            logger.warning(f"未找到项目 {project_id} 的默认环境配置")
            return None

        # 处理不同的返回格式
        if isinstance(result, list):
            # API 直接返回列表
            if len(result) > 0:
                logger.info(f"使用默认环境配置: {result[0].get('name')}")
                return result[0]
        elif isinstance(result, dict):
            # API 返回分页格式 {"items": [...]} 或 {"results": [...]}
            items = result.get('items', result.get('results', []))
            if items and len(items) > 0:
                logger.info(f"使用默认环境配置: {items[0].get('name')}")
                return items[0]
            # 可能直接就是配置对象
            if result.get('id') and result.get('base_url'):
                logger.info(f"使用默认环境配置: {result.get('name')}")
                return result

        logger.warning(f"未找到项目 {project_id} 的默认环境配置")
        return None

    async def _fetch_public_data(self, project_id: int) -> list[dict]:
        """从API获取项目的公共数据（用于变量替换）"""
        result = await self._api_get(f"/api/ui-automation/public-data/by-project/{project_id}/")
        logger.debug(f"获取公共数据原始结果 (project_id={project_id}): type={type(result)}, value={result}")
        if result and isinstance(result, list):
            logger.debug(f"公共数据是列表，长度: {len(result)}")
            return result
        logger.warning(f"公共数据格式不正确: type={type(result)}")
        return []

    async def _init_data_processor(self, project_id: int) -> DataProcessor:
        """初始化数据处理器，加载项目公共变量"""
        data_processor = reset_data_processor()
        
        if project_id:
            public_data = await self._fetch_public_data(project_id)
            logger.info(f"已加载 {len(public_data)} 个公共变量")
            if public_data:
                data_processor.load_public_data(public_data)
                logger.debug(f"变量缓存: {data_processor.get_all()}")
        else:
            logger.warning("project_id 为空，无法加载公共变量")
        
        return data_processor

    def _build_page_step_config(
        self,
        data: dict,
        base_url: str = '',
        data_processor: Optional[DataProcessor] = None,
        case_data_override: Optional[dict] = None,
        env_config: Optional[dict] = None,
    ) -> PageStepConfig:
        """构建页面步骤配置
        
        Args:
            data: 页面步骤数据
            base_url: 环境基础URL
            data_processor: 变量处理器，用于替换 ${{变量名}} 语法
            case_data_override: 用例步骤的覆盖数据
            env_config: 执行环境配置，用于 SQL 步骤获取数据库连接
        """
        steps = []
        
        for detail in data.get('step_details', []):
            step_type = detail.get('step_type', 0)
            detail_id = str(detail.get('id', ''))
            
            # 从 ope_value 中提取输入值
            ope_value = detail.get('ope_value', {})
            sql_execute = detail.get('sql_execute') or {}
            
            # 应用用例步骤的覆盖数据
            if case_data_override and detail_id in case_data_override:
                val = case_data_override[detail_id]
                if isinstance(val, dict):
                    # 如果覆盖数据本身是字典，直接合并
                    if isinstance(ope_value, dict):
                        ope_value = {**ope_value, **val}
                    else:
                        ope_value = val
                else:
                    # 否则，如果是字符串等简单类型，覆盖已有的主要参数
                    if isinstance(ope_value, dict):
                        for key in ['text', 'value', 'timeout', 'url', 'key', 'expected']:
                            if key in ope_value:
                                ope_value[key] = val
                                break
                        else:
                            # 没找到已知键，且字典不为空，取第一个键
                            if ope_value:
                                first_key = next(iter(ope_value.keys()))
                                ope_value[first_key] = val
                            else:
                                ope_value = {'text': val}
                    else:
                        ope_value = val

            binding_mode = ''
            runtime_resolver = ''
            if isinstance(ope_value, dict):
                binding_mode = str(ope_value.get('binding_mode') or '').strip()
                runtime_resolver = str(ope_value.get('runtime_resolver') or '').strip()
            
            # 支持多种格式：{"text": "admin"}, {"value": "xxx"}, {"timeout": 3000}, 或纯字符串/数字
            if isinstance(ope_value, dict):
                # 按优先级尝试提取常见字段
                input_value = (
                    ope_value.get('text') or
                    ope_value.get('value') or
                    ope_value.get('timeout') or  # wait 操作使用 timeout
                    ope_value.get('url') or      # goto 操作可能使用 url
                    ope_value.get('key') or      # press 操作可能使用 key
                    ope_value.get('expected') or # 断言使用 expected
                    ''
                )
                # 如果所有已知字段都没有，且字典不为空，取第一个值
                if not input_value and ope_value:
                    input_value = next((
                        candidate
                        for key, candidate in ope_value.items()
                        if key not in {'binding_mode', 'runtime_resolver'} and candidate not in (None, '')
                    ), '')
                # 确保转换为字符串
                input_value = str(input_value) if input_value else ''
            else:
                input_value = str(ope_value) if ope_value else ''
            
            # 定位器值也可能包含变量
            locator_value = detail.get('locator_value', '')
            locator_value_2 = detail.get('locator_value_2', '')
            locator_value_3 = detail.get('locator_value_3', '')
            
            # 变量替换：替换 input_value 和 locator_value 中的 ${{变量名}}
            if data_processor:
                sql_execute = data_processor.replace(sql_execute)
                if not isinstance(sql_execute, (dict, str)):
                    sql_execute = {}

                original_input = input_value
                input_value = data_processor.replace(input_value)
                if original_input != input_value:
                    logger.info(f"变量替换: '{original_input}' -> '{input_value}'")
                
                original_locator = locator_value
                locator_value = data_processor.replace(locator_value)
                if original_locator != locator_value:
                    logger.info(f"变量替换 (定位器): '{original_locator}' -> '{locator_value}'")

                if locator_value_2:
                    original_locator_2 = locator_value_2
                    locator_value_2 = data_processor.replace(locator_value_2)
                    if original_locator_2 != locator_value_2:
                        logger.info(f"变量替换 (定位器2): '{original_locator_2}' -> '{locator_value_2}'")

                if locator_value_3:
                    original_locator_3 = locator_value_3
                    locator_value_3 = data_processor.replace(locator_value_3)
                    if original_locator_3 != locator_value_3:
                        logger.info(f"变量替换 (定位器3): '{original_locator_3}' -> '{locator_value_3}'")
                
                # 确保替换后的值是字符串类型
                if not isinstance(input_value, str):
                    input_value = str(input_value)
                if not isinstance(locator_value, str):
                    locator_value = str(locator_value)
                if locator_value_2 and not isinstance(locator_value_2, str):
                    locator_value_2 = str(locator_value_2)
                if locator_value_3 and not isinstance(locator_value_3, str):
                    locator_value_3 = str(locator_value_3)
            else:
                logger.warning(f"data_processor 为 None，跳过变量替换")

            def _parse_index(value):
                if value is None or value == '':
                    return None
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
            
            steps.append(StepConfig(
                step_id=detail.get('id', 0),
                operation_type=detail.get('ope_key') or '',  # 操作类型如 click, type
                locator_type=detail.get('locator_type') or 'xpath',  # 定位方式
                locator_value=locator_value or '',  # 定位表达式
                step_type=step_type,
                input_value=input_value,  # 输入值
                binding_mode=binding_mode,
                runtime_resolver=runtime_resolver,
                description=detail.get('description') or detail.get('element_name') or ('SQL操作' if step_type == 2 else ''),
                wait_time=detail.get('wait_time', 0),
                is_iframe=detail.get('is_iframe', False),
                iframe_locator=detail.get('iframe_locator') or '',
                locator_index=detail.get('locator_index'),
                locator_type_2=detail.get('locator_type_2'),
                locator_value_2=locator_value_2 or None,
                locator_index_2=_parse_index(detail.get('locator_index_2')),
                locator_type_3=detail.get('locator_type_3'),
                locator_value_3=locator_value_3 or None,
                locator_index_3=_parse_index(detail.get('locator_index_3')),
                sql_execute=sql_execute,
            ))
        
        # 页面URL处理：支持相对路径与 base_url 拼接
        page_url = data.get('page_url', '') or ''
        if page_url:
            # 如果是相对路径，与 base_url 拼接
            if page_url.startswith('/') and base_url:
                page_url = base_url.rstrip('/') + page_url
            elif not page_url.startswith(('http://', 'https://')) and base_url:
                # 既不是绝对路径也不是完整URL，与 base_url 拼接
                page_url = base_url.rstrip('/') + '/' + page_url.lstrip('/')
        else:
            # page_url 为空，使用 base_url
            page_url = base_url
        
        # URL也可能包含变量
        if data_processor and page_url:
            page_url = data_processor.replace(page_url)
            if not isinstance(page_url, str):
                page_url = str(page_url)
        
        return PageStepConfig(
            page_step_id=data.get('id', 0),
            page_url=page_url,
            page_name=data.get('name', ''),  # 页面步骤名称
            steps=steps,
            env_config=env_config,
        )
    
    def _build_test_case_config(self, data: dict, env_config: Optional[dict] = None, data_processor: Optional[DataProcessor] = None) -> TestCaseConfig:
        """构建测试用例配置
        
        Args:
            data: 用例数据
            env_config: 环境配置
            data_processor: 变量处理器
        """
        # 从环境配置获取base_url
        base_url = ''
        if env_config:
            base_url = env_config.get('base_url', '') or ''
            logger.info(f"使用环境配置: {env_config.get('name')}, base_url: {base_url}")
        
        page_steps = []
        
        for case_step in data.get('case_step_details', []):
            page_step_data = case_step.get('page_step', {})
            case_data_override = case_step.get('case_data') or {}
            if page_step_data:
                page_steps.append(self._build_page_step_config(page_step_data, base_url, data_processor, case_data_override, env_config))
        
        return TestCaseConfig(
            case_id=data.get('id', 0),
            case_name=data.get('name', ''),
            page_steps=page_steps,
            env_config=env_config,
            ai_execution_artifact=self._extract_case_ai_execution_artifact(data),
        )
    
    def stop(self):
        """停止消费者"""
        self._stop_event.set()
    
    async def run(self):
        """运行消费者"""
        self.ws_client.set_message_handler(self.handle_message)
        
        # 启动任务处理协程
        process_task = asyncio.create_task(self.process_tasks())
        pending_poll_task = asyncio.create_task(self.poll_pending_ai_generation_tasks())
        
        # 启动WebSocket客户端
        await self.ws_client.run()
        
        # 停止任务处理
        self.stop()
        pending_poll_task.cancel()
        try:
            await pending_poll_task
        except asyncio.CancelledError:
            pass
        await process_task
