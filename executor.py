"""
UI自动化执行器 - Python Playwright执行引擎
使用Python原生Playwright库执行测试，无需Node.js依赖
"""

import asyncio
import copy
import hashlib
import importlib
import json
import logging
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any, Optional, Union
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, FrameLocator, Page, Playwright, expect

from models import StepResultModel, CaseResultModel
from runtime_input_resolver import resolve_image_captcha
from mcp_playwright_adapter import (
    PlaywrightMcpAdapter,
    PlaywrightMcpHttpAdapter,
    McpProtocolError,
)

logger = logging.getLogger('actuator')

FORM_FIELD_CONTAINER_SELECTOR = '.el-form-item, .arco-form-item, .ant-form-item, .form-item'
FORM_FIELD_CONTROL_SELECTOR = (
    'input, textarea, select, [role="combobox"], [contenteditable="true"], '
    'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"]'
)
FORM_FIELD_OPERATIONS = {'fill', 'select_option', 'check', 'uncheck'}


def _ensure_local_playwright_browsers_path() -> None:
    local_browsers = Path(__file__).parent / 'browsers'
    if 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ and local_browsers.exists():
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(local_browsers)
        logger.info(f"Playwright 浏览器路径: {local_browsers}")


@dataclass
class StepConfig:
    """步骤配置"""
    step_id: int
    operation_type: str      # click, fill, goto, wait, assert等
    locator_type: str        # xpath, css, id等
    locator_value: str
    step_type: int = 0       # 0元素操作, 1断言操作, 2 SQL操作
    input_value: str = ''
    binding_mode: str = ''
    runtime_resolver: str = ''
    description: str = ''
    wait_time: float = 0
    is_iframe: bool = False
    iframe_locator: str = ''
    locator_index: Optional[int] = None
    locator_type_2: Optional[str] = None
    locator_value_2: Optional[str] = None
    locator_index_2: Optional[int] = None
    locator_type_3: Optional[str] = None
    locator_value_3: Optional[str] = None
    locator_index_3: Optional[int] = None
    sql_execute: Any = None
    
    # 步骤详情(公共步骤)
    details: list['StepConfig'] = field(default_factory=list)


@dataclass 
class PageStepConfig:
    """页面步骤配置"""
    page_step_id: int
    page_url: str
    page_name: str
    steps: list[StepConfig] = field(default_factory=list)
    env_config: Optional[dict] = None


@dataclass
class TestCaseConfig:
    """测试用例配置"""
    case_id: int
    case_name: str
    page_steps: list[PageStepConfig] = field(default_factory=list)
    env_config: Optional[dict] = None


class PlaywrightExecutor:
    """Python原生Playwright执行器"""
    
    def __init__(
        self, 
        browser_type: str = 'chromium',
        headless: bool = False,
        persistent: bool = True,
        user_data_dir: str = './data/browser',
        launch_timeout: int = 30000,
        action_timeout: int = 30000,
        screenshot_dir: str = './data/screenshots',
        trace_enabled: bool = False,
        trace_dir: str = './data/traces',
        trace_screenshots: bool = True,
        trace_snapshots: bool = True,
        trace_sources: bool = False,
        ignore_https_errors: bool = True,
        mcp_provider: str = 'playwright-native',
        mcp_transport: str = 'stdio',
        mcp_command: str = 'npx',
        mcp_args: Optional[list[str]] = None,
        mcp_cwd: Optional[str] = None,
        mcp_server_url: Optional[str] = None,
    ):
        self.browser_type = browser_type
        self.headless = headless
        self.persistent = persistent
        self.user_data_dir = user_data_dir
        self.launch_timeout = launch_timeout
        self.action_timeout = action_timeout
        self.screenshot_dir = screenshot_dir
        
        # Trace 配置
        self.trace_enabled = trace_enabled
        self.trace_dir = trace_dir
        self.trace_screenshots = trace_screenshots
        self.trace_snapshots = trace_snapshots
        self.trace_sources = trace_sources
        self.ignore_https_errors = ignore_https_errors
        self.mcp_provider = mcp_provider or 'playwright-native'
        if self.mcp_provider == 'official-playwright-mcp':
            logger.warning('已按当前策略禁用官方 Playwright MCP，页面探索使用 Playwright 原生 API')
            self.mcp_provider = 'playwright-native'
        self.mcp_transport = mcp_transport or 'stdio'
        self.mcp_command = mcp_command or 'npx'
        self.mcp_args = mcp_args
        self.mcp_cwd = mcp_cwd
        self.mcp_server_url = mcp_server_url
        
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._stop_requested = False
        self._current_trace_path: Optional[str] = None
        self._page_errors: list[str] = []
        self._network_events: list[dict[str, Any]] = []
        
        Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.screenshot_dir).mkdir(parents=True, exist_ok=True)
        if self.trace_enabled:
            Path(self.trace_dir).mkdir(parents=True, exist_ok=True)
    
    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {'1', 'true', 'yes', 'y', 'on'}:
                return True
            if normalized in {'0', 'false', 'no', 'n', 'off'}:
                return False
        if value is None:
            return default
        return bool(value)

    def _ignore_https_errors_for_env(self, env_config: Optional[dict] = None) -> bool:
        """环境配置 extra_config.ignore_https_errors 可覆盖执行器全局证书策略。"""
        if isinstance(env_config, dict):
            extra_config = env_config.get('extra_config') if isinstance(env_config.get('extra_config'), dict) else {}
            if 'ignore_https_errors' in extra_config:
                return self._coerce_bool(extra_config.get('ignore_https_errors'), self.ignore_https_errors)
        return self.ignore_https_errors

    def _browser_launch_args(self) -> list[str]:
        """受限环境下补充 Chromium 启动参数。"""
        return [
            '--no-sandbox',
            '--disable-setuid-sandbox',
        ]

    async def init_browser(self, ignore_https_errors: Optional[bool] = None) -> None:
        """初始化浏览器"""
        _ensure_local_playwright_browsers_path()
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        
        browser_launcher = getattr(self._playwright, self.browser_type)
        
        context_options = {
            'ignore_https_errors': self.ignore_https_errors if ignore_https_errors is None else ignore_https_errors,
        }

        if self.persistent:
            self._context = await browser_launcher.launch_persistent_context(
                self.user_data_dir,
                headless=self.headless,
                timeout=self.launch_timeout,
                args=self._browser_launch_args(),
                **context_options,
            )
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
        else:
            self._browser = await browser_launcher.launch(
                headless=self.headless,
                timeout=self.launch_timeout,
                args=self._browser_launch_args(),
            )
            self._context = await self._browser.new_context(**context_options)
            self._page = await self._context.new_page()
        
        self._page.set_default_timeout(self.action_timeout)
        logger.info(
            f"浏览器已初始化: {self.browser_type}, headless={self.headless}, "
            f"ignore_https_errors={context_options['ignore_https_errors']}"
        )
    
    async def close(self) -> None:
        """关闭浏览器"""
        if self._context:
            await self._context.close()
            self._context = None
            self._page = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("浏览器已关闭")
    
    @asynccontextmanager
    async def browser_session(self, ignore_https_errors: Optional[bool] = None):
        """浏览器会话上下文管理器"""
        await self.init_browser(ignore_https_errors=ignore_https_errors)
        try:
            yield self._page
        finally:
            await self.close()
    
    @asynccontextmanager
    async def browser_session_with_trace(self, trace_name: str = 'trace', ignore_https_errors: Optional[bool] = None):
        """带 Trace 的浏览器会话上下文管理器
        
        Args:
            trace_name: trace 文件名前缀（不含扩展名）
            
        Yields:
            Page: 页面对象
            
        Returns:
            trace 文件路径（通过 self._current_trace_path 获取）
        """
        await self.init_browser(ignore_https_errors=ignore_https_errors)
        self._current_trace_path = None
        
        try:
            # 启动 Trace
            if self.trace_enabled and self._context:
                await self._context.tracing.start(
                    screenshots=self.trace_screenshots,
                    snapshots=self.trace_snapshots,
                    sources=self.trace_sources,
                )
                logger.debug(f"Trace 已启动: screenshots={self.trace_screenshots}, snapshots={self.trace_snapshots}")
            
            yield self._page
            
        finally:
            # 停止 Trace 并保存
            if self.trace_enabled and self._context:
                try:
                    timestamp = int(time.time() * 1000)
                    trace_path = f"{self.trace_dir}/{trace_name}_{timestamp}.zip"
                    await self._context.tracing.stop(path=trace_path)
                    self._current_trace_path = trace_path
                    logger.info(f"Trace 已保存: {trace_path}")
                except Exception as e:
                    logger.error(f"保存 Trace 失败: {e}")
            
            await self.close()
    
    def get_current_trace_path(self) -> Optional[str]:
        """获取当前执行的 trace 文件路径"""
        return self._current_trace_path

    async def _capture_ai_generation_storage_state(self, task_id: Any) -> str:
        """保存探索阶段的浏览器登录态，供后续 Playwright Test 真实执行复用。"""
        if not self._context:
            return ''
        try:
            base_dir = Path(self.trace_dir).parent if self.trace_dir else Path(self.screenshot_dir).parent
            storage_dir = base_dir / 'storage_states'
            storage_dir.mkdir(parents=True, exist_ok=True)
            storage_path = storage_dir / f"ai_generation_{task_id}_{int(time.time() * 1000)}.json"
            await self._context.storage_state(path=str(storage_path))
            return str(storage_path)
        except Exception as exc:
            logger.warning(f"保存 AI 生成浏览器 storageState 失败: {exc}")
            return ''

    def stop(self):
        """请求停止执行"""
        self._stop_requested = True

    def _setup_page_listeners(self, page: Page):
        """注册页面基础事件监听（自动处理弹窗、记录控制台 JS 错误）"""
        self._page_errors = []
        self._network_events = []

        async def handle_dialog(dialog):
            logger.warning(f"检测到浏览器弹窗 [{dialog.type}]: '{dialog.message}'，已自动 accept。")
            try:
                await dialog.accept()
            except Exception as e:
                logger.error(f"处理浏览器弹窗异常: {e}")

        def handle_pageerror(exception):
            logger.error(f"页面 JS 抛出未捕获异常: {exception}")
            if not hasattr(self, '_page_errors'):
                self._page_errors = []
            self._page_errors.append(str(exception))

        def handle_response(response):
            try:
                request = response.request
                resource_type = request.resource_type
                if resource_type not in {'xhr', 'fetch', 'document'}:
                    return
                self._network_events.append({
                    'url': response.url,
                    'method': request.method,
                    'resource_type': resource_type,
                    'status': response.status,
                    'ok': response.ok,
                    'request_headers': {
                        key: value
                        for key, value in (request.headers or {}).items()
                        if key.lower() in {'content-type', 'accept'}
                    },
                })
                self._network_events = self._network_events[-80:]
            except Exception as exc:
                logger.debug(f"记录网络响应摘要失败: {exc}")

        page.on("dialog", handle_dialog)
        page.on("pageerror", handle_pageerror)
        page.on("response", handle_response)

    def _network_summary(self) -> dict[str, Any]:
        events = list(getattr(self, '_network_events', []) or [])
        failed = [item for item in events if not item.get('ok')]
        api_events = [
            item for item in events
            if item.get('resource_type') in {'xhr', 'fetch'}
        ]
        return {
            'total_count': len(events),
            'api_count': len(api_events),
            'failed_count': len(failed),
            'recent': events[-20:],
            'failed': failed[-20:],
        }

    def _get_locator(self, container: Union[Page, FrameLocator], locator_type: str, locator_value: str):
        """根据定位类型获取元素定位器"""
        if locator_type == 'role':
            try:
                role_locator = json.loads(locator_value)
            except (TypeError, json.JSONDecodeError):
                role_locator = None
            if isinstance(role_locator, dict) and role_locator.get('role'):
                name = role_locator.get('name') or None
                exact = bool(role_locator.get('exact'))
                return container.get_by_role(
                    str(role_locator['role']),
                    name=str(name) if name is not None else None,
                    exact=exact,
                )
        if locator_type == 'text':
            try:
                text_locator = json.loads(locator_value)
            except (TypeError, json.JSONDecodeError):
                text_locator = None
            if isinstance(text_locator, dict) and 'value' in text_locator:
                scoped_container = container
                scope = text_locator.get('scope') if isinstance(text_locator.get('scope'), dict) else {}
                if scope.get('role'):
                    scoped_container = container.get_by_role(str(scope['role']))
                return scoped_container.get_by_text(
                    str(text_locator.get('value') or ''),
                    exact=bool(text_locator.get('exact')),
                )
        locator_map = {
            'xpath': lambda: container.locator(f"xpath={locator_value}"),
            'css': lambda: container.locator(locator_value),
            'id': lambda: container.locator(f"#{locator_value}"),
            'name': lambda: container.locator(f"[name='{locator_value}']"),
            'text': lambda: container.get_by_text(locator_value),
            'role': lambda: container.get_by_role(locator_value),
            'placeholder': lambda: container.get_by_placeholder(locator_value),
            'label': lambda: container.get_by_label(locator_value),
            'testid': lambda: container.get_by_test_id(locator_value),
            'test_id': lambda: container.get_by_test_id(locator_value),
        }
        return locator_map.get(locator_type, lambda: container.locator(locator_value))()

    @staticmethod
    def _is_url_allowed(url: str, safety_policy: dict, base_url: str = '') -> bool:
        """按安全策略限制探索 URL 范围。"""
        if not url:
            return False
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'}:
            return False

        allowlist = [item for item in safety_policy.get('url_allowlist') or [] if item]
        blocklist = [item for item in safety_policy.get('url_blocklist') or [] if item]
        if any(item in url for item in blocklist):
            return False
        if allowlist:
            return any(item in url for item in allowlist)
        if base_url:
            base = urlparse(base_url)
            return parsed.netloc == base.netloc
        return True

    @staticmethod
    def _looks_dangerous(text: str, safety_policy: dict) -> bool:
        lowered = (text or '').lower()
        keywords = safety_policy.get('dangerous_action_keywords') or []
        return any(str(keyword).lower() in lowered for keyword in keywords if keyword)

    @staticmethod
    def _requires_confirmation(text: str, safety_policy: dict) -> bool:
        lowered = (text or '').lower()
        keywords = safety_policy.get('require_confirmation_keywords') or []
        return any(str(keyword).lower() in lowered for keyword in keywords if keyword)

    def _ai_plan_step_requires_confirmation(self, step: dict[str, Any], safety_policy: dict[str, Any]) -> bool:
        text = self._clean_visible_text(' '.join(str(step.get(key) or '') for key in [
            'action',
            'description',
            'target_name',
            'expected',
            'value',
        ]))
        return self._requires_confirmation(text, safety_policy)

    @staticmethod
    def _clean_visible_text(value: Any) -> str:
        return re.sub(r'\s+', ' ', str(value or '')).strip()

    @staticmethod
    def _structured_dict(container: Any, key: str) -> dict[str, Any]:
        if not isinstance(container, dict):
            return {}
        value = container.get(key)
        return value if isinstance(value, dict) else {}

    def _structured_requirement_document(self, args: dict) -> dict[str, Any]:
        return self._structured_dict(args, 'requirement_document')

    def _structured_generated_case(self, args: dict) -> dict[str, Any]:
        return self._structured_dict(args, 'generated_case')

    def _structured_test_plan(self, args: dict) -> dict[str, Any]:
        return self._structured_dict(args, 'test_plan')

    def _structured_authentication_contract(self, args: dict) -> dict[str, Any]:
        contract = self._structured_dict(args, 'authentication_contract')
        if contract:
            return contract
        safety_policy = args.get('safety_policy') if isinstance(args.get('safety_policy'), dict) else {}
        return self._structured_dict(safety_policy, 'authentication_contract')

    def _structured_requirement_corpus(self, args: dict, include_raw_fallback: bool = True) -> str:
        pieces: list[str] = []
        seen: set[str] = set()

        def add_text(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    add_text(item)
                return
            text = self._clean_visible_text(value)
            if not text:
                return
            for raw_line in str(value).splitlines():
                line = self._clean_visible_text(raw_line)
                if line and line not in seen:
                    seen.add(line)
                    pieces.append(line)

        requirement_document = self._structured_requirement_document(args)
        generated_case = self._structured_generated_case(args)
        test_plan = self._structured_test_plan(args)

        for key in ['task_name', 'target_module', 'source_requirement', 'gherkin', 'normalized_text']:
            add_text(requirement_document.get(key))
        for source in requirement_document.get('sources') or []:
            if isinstance(source, dict):
                add_text(source.get('text'))

        for key in ['name', 'description']:
            add_text(generated_case.get(key))
        for key in ['objective']:
            add_text(test_plan.get(key))

        for container in [generated_case, test_plan]:
            for step in container.get('steps') or []:
                if not isinstance(step, dict):
                    continue
                for key in ['description', 'target_name', 'expected', 'value']:
                    add_text(step.get(key))

        if not pieces and include_raw_fallback:
            for key in ['requirement', 'gherkin', 'target_module']:
                add_text(args.get(key))

        return '\n'.join(pieces)

    def _element_visible_text(self, element: dict[str, Any]) -> str:
        values = [
            element.get('name'),
            element.get('accessible_name'),
            element.get('text'),
            element.get('label'),
            element.get('placeholder'),
            element.get('test_id'),
            element.get('href'),
        ]
        return self._clean_visible_text(' '.join(str(value or '') for value in values))

    def _extract_login_credentials(self, args: dict) -> dict[str, str]:
        """从环境配置 extra_config 和需求文本中提取登录账号，生产部署不依赖用户手工操作。"""
        env_config = args.get('environment_config') if isinstance(args.get('environment_config'), dict) else {}
        extra_config = env_config.get('extra_config') if isinstance(env_config.get('extra_config'), dict) else {}
        auth_config = extra_config.get('auth') if isinstance(extra_config.get('auth'), dict) else {}
        login_config = extra_config.get('login') if isinstance(extra_config.get('login'), dict) else {}
        containers = [auth_config, login_config, extra_config, env_config]

        username_keys = [
            'username', 'user_name', 'user', 'account', 'account_name', 'login_username',
            'login_user', 'mobile', 'phone', 'email',
        ]
        password_keys = ['password', 'passwd', 'pwd', 'login_password', 'login_pwd']

        credentials = {'username': '', 'password': ''}
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in username_keys:
                if not credentials['username'] and container.get(key):
                    credentials['username'] = str(container.get(key))
            for key in password_keys:
                if not credentials['password'] and container.get(key):
                    credentials['password'] = str(container.get(key))

        text = self._structured_requirement_corpus(args)
        if not credentials['username']:
            username_patterns = [
                r'(?:用户名|账号|账户|登录名|用户)\s*[：:为是=]\s*([A-Za-z0-9_@.\-]+)',
                r'(?:username|user|account)\s*[：:=]\s*([A-Za-z0-9_@.\-]+)',
            ]
            for pattern in username_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    credentials['username'] = match.group(1).strip()
                    break
        if not credentials['password']:
            password_patterns = [
                r'(?:密码|口令)\s*[：:为是=]\s*([^\s，,。；;]+)',
                r'(?:password|passwd|pwd)\s*[：:=]\s*([^\s,;]+)',
            ]
            for pattern in password_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    credentials['password'] = match.group(1).strip()
                    break
        return credentials

    def _extract_business_keywords(self, args: dict) -> list[str]:
        requirement_document = self._structured_requirement_document(args)
        generated_case = self._structured_generated_case(args)
        test_plan = self._structured_test_plan(args)
        text = self._structured_requirement_corpus(args)
        keywords: list[str] = []

        def add(value: Any) -> None:
            item = self._clean_visible_text(value)
            item = item.strip(' "\'“”‘’[]【】()（）')
            for marker in ['进入', '打开', '查看', '访问']:
                if marker in item:
                    item = item.split(marker)[-1]
            if '后' in item and len(item) > 8:
                item = item.split('后')[-1]
            if len(item) >= 2 and item not in keywords:
                keywords.append(item[:30])

        add(requirement_document.get('target_module') or args.get('target_module'))
        add(requirement_document.get('task_name'))
        add(generated_case.get('name'))
        add(generated_case.get('description'))
        add(test_plan.get('objective'))
        for pattern in [
            r'[“"\'【]([^”"\'】]{2,30})[”"\'】]',
            r'([\u4e00-\u9fa5A-Za-z0-9_]{2,20})(?:页面|模块|菜单|列表|管理)',
            r'(?:进入|打开|查看|访问)([\u4e00-\u9fa5A-Za-z0-9_]{2,20})',
            r'((?:新建|新增|添加|创建|查询|搜索|编辑)[\u4e00-\u9fa5A-Za-z0-9_]{0,16})',
            r'([\u4e00-\u9fa5A-Za-z0-9_]{0,12}任务)',
            r'([\u4e00-\u9fa5A-Za-z0-9_]{0,12}用例)',
        ]:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                add(match.group(1))

        generic_actions = ['新建', '新增', '添加', '创建', '查询', '搜索']
        for action in generic_actions:
            if action in text:
                add(action)

        return keywords[:20]

    def _page_signature(self, state: dict[str, Any]) -> str:
        element_names = [
            '|'.join([
                str(item.get('element_key') or ''),
                str(item.get('role') or ''),
                self._clean_visible_text(item.get('name') or item.get('accessible_name') or item.get('text'))[:80],
            ])
            for item in self._exploration_elements(state)[:440]
            if isinstance(item, dict)
        ]
        signature_text = '|'.join([str(state.get('url') or ''), str(state.get('title') or ''), *element_names])
        return hashlib.sha1(signature_text.encode('utf-8')).hexdigest()

    def _looks_like_login_page(self, state: dict[str, Any]) -> bool:
        elements = [item for item in (state.get('elements') or []) if isinstance(item, dict)]
        has_password = any(
            item.get('input_type') == 'password' or '密码' in self._element_visible_text(item).lower() or 'password' in self._element_visible_text(item).lower()
            for item in elements
        )
        has_login_action = any(
            'click' in (item.get('actions') or [])
            and any(keyword in self._element_visible_text(item).lower() for keyword in ['登录', '登陆', 'login', 'sign in'])
            for item in elements
        )
        return has_password and has_login_action

    def _authentication_mode(self, args: dict, first_state: dict[str, Any]) -> str:
        """消费后端下发的认证合同；执行器不理解自然语言需求。"""
        valid_modes = {'test_subject', 'prerequisite', 'authentication_then_business', 'unknown'}
        for candidate in [
            self._clean_visible_text(args.get('authentication_mode')).lower(),
            self._clean_visible_text(self._structured_authentication_contract(args).get('mode')).lower(),
        ]:
            if candidate in valid_modes:
                return candidate
        if not self._looks_like_login_page(first_state):
            return 'not_applicable'
        return 'unknown'

    def _authentication_contract_unavailable_reason(self, args: dict) -> str:
        contract = self._structured_authentication_contract(args)
        rationale = self._clean_visible_text(contract.get('rationale'))
        source = self._clean_visible_text(contract.get('source'))
        error_type = self._clean_visible_text(contract.get('error_type'))
        if rationale:
            return f'认证合同不可用: {rationale}'
        if source or error_type:
            detail = ' '.join(part for part in [source or 'unknown', error_type] if part)
            return f'认证合同不可用: {detail}'
        return '认证合同不可用，无法判断登录页是被测对象还是业务前置条件'

    def _append_unique_page_state(
        self,
        pages: list[dict[str, Any]],
        seen_signatures: set[str],
        state: dict[str, Any],
    ) -> bool:
        signature = self._page_signature(state)
        if signature in seen_signatures:
            return False
        state_score = len(self._exploration_elements(state)) + (600 if self._state_has_form_structure(state) else 0)
        for index, existing in enumerate(pages):
            if str(existing.get('url') or '') != str(state.get('url') or ''):
                continue
            existing_score = len(self._exploration_elements(existing)) + (600 if self._state_has_form_structure(existing) else 0)
            if state_score > existing_score:
                pages[index] = state
                seen_signatures.add(signature)
                return True
        seen_signatures.add(signature)
        pages.append(state)
        return True

    def _build_state_transition_record(
        self,
        from_state: dict[str, Any],
        to_state: dict[str, Any],
        element: dict[str, Any],
        stage: str = '',
        score: int = 0,
    ) -> dict[str, Any]:
        label = self._exploration_label(element)
        locator = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {}
        if not locator:
            candidates = self._dedupe_locators(element.get('locator_candidates') or [])
            locator = candidates[0] if candidates else {}
        return {
            'operation': 'click',
            'stage': stage,
            'score': score,
            'from_page_key': from_state.get('page_key') or '',
            'from_url': from_state.get('url') or '',
            'to_page_key': to_state.get('page_key') or '',
            'to_url': to_state.get('url') or '',
            'element_key': element.get('element_key') or '',
            'target_name': label,
            'locator_hint': locator,
            'context_type': element.get('context_type') or 'main',
            'frame_key': element.get('frame_key') or '',
            'frame_name': element.get('frame_name') or '',
            'frame_url': element.get('frame_url') or '',
        }

    def _rank_exploration_element(self, element: dict[str, Any], keywords: list[str], safety_policy: dict) -> int:
        if not isinstance(element, dict) or not element.get('enabled', True):
            return -1
        actions = element.get('actions') or []
        if 'click' not in actions:
            return -1

        label = self._element_visible_text(element)
        lowered = label.lower()
        if not label or self._looks_dangerous(label, safety_policy) or self._requires_confirmation(label, safety_policy):
            return -1

        score = 0
        for keyword in keywords:
            normalized = self._clean_visible_text(keyword)
            if not normalized:
                continue
            if normalized in label or label in normalized:
                score += 100 + min(len(normalized), 20)
            elif normalized.lower() in lowered:
                score += 80
        if any(word in label for word in ['新建', '新增', '添加', '创建', '查询', '搜索']):
            score += 12
        if score <= 0:
            return -1
        if element.get('href'):
            score += 8
        if element.get('role') in {'link', 'menuitem', 'button', 'tab'}:
            score += 6
        if any(word in label for word in ['菜单', '管理', '列表', '页面']):
            score += 4
        return score

    def _build_exploration_targets(self, args: dict) -> list[dict[str, Any]]:
        """按测试计划意图拆成阶段化探索目标，避免一次性按最高分乱点。"""
        requirement_document = self._structured_requirement_document(args)
        target_module = self._clean_visible_text(
            requirement_document.get('target_module') or args.get('target_module') or requirement_document.get('task_name') or ''
        )
        requirement = self._structured_requirement_corpus(args)
        llm_coverage_keywords = self._llm_exploration_coverage_keywords(args)
        coverage_keywords = self._merge_exploration_coverage_keywords(
            self._extract_exploration_coverage_keywords(args),
            llm_coverage_keywords,
        )
        form_coverage_keywords = self._merge_exploration_coverage_keywords(
            self._form_exploration_coverage_keywords(coverage_keywords),
            llm_coverage_keywords,
        )
        targets: list[dict[str, Any]] = []

        explicit_route = self._extract_requirement_exploration_route(args)
        if explicit_route and not target_module:
            route_clues: list[str] = []
            for route_item in explicit_route:
                route_clues.extend(self._route_label_clues(route_item.get('label')))
            for index, route_item in enumerate(explicit_route):
                label = route_item['label']
                opens_form = bool(route_item.get('opens_form'))
                route_path = [item['label'] for item in explicit_route[: index + 1]]
                targets.append({
                    'stage': 'open_business_form' if opens_form else f'requirement_route_{index + 1}',
                    'keywords': list(dict.fromkeys([
                        *self._route_label_clues(label),
                        *route_path,
                        *route_clues[:12],
                    ])),
                    'preferred_actions': [label],
                    'blocked_actions': (
                        ['保存', '提交', '确定', '确认', '删除', '移除']
                        if opens_form
                        else ['保存', '提交', '确定', '确认', '删除', '移除']
                    ),
                    'coverage_keywords': form_coverage_keywords if opens_form else {},
                    'route_path': route_path,
                    'route_tokens': self._route_label_clues(label),
                })
            return targets

        if target_module:
            targets.append({
                'stage': 'target_module',
                'keywords': [target_module, target_module.replace('模块', ''), target_module.replace('管理', '')],
                'preferred_actions': ['菜单', '管理', '列表', '页面', '任务', '用例'],
                'blocked_actions': ['新建', '新增', '添加', '创建', '保存', '提交', '确定', '确认', '删除', '移除'],
            })

        wants_create = any(keyword in requirement for keyword in ['新建', '新增', '添加', '创建'])
        if wants_create:
            create_keywords = ['新建', '新增', '添加', '创建']
            if target_module:
                create_keywords.extend([
                    f'新建{target_module}',
                    f'新增{target_module}',
                    f'创建{target_module}',
                ])
            targets.append({
                'stage': 'open_business_form',
                'keywords': create_keywords,
                'preferred_actions': ['新建', '新增', '添加', '创建', '弹窗'],
                'blocked_actions': ['保存', '提交', '确定', '确认', '删除', '移除'],
                'coverage_keywords': self._merge_exploration_coverage_keywords(
                    form_coverage_keywords,
                    {'page': [keyword for keyword in create_keywords if len(keyword) > 2]},
                ),
            })

        if not targets:
            targets.append({
                'stage': 'business_goal',
                'keywords': self._extract_business_keywords(args),
                'preferred_actions': ['菜单', '管理', '列表', '页面', '新建', '新增', '添加', '创建'],
                'blocked_actions': ['保存', '提交', '确定', '确认', '删除', '移除'],
            })
        return targets

    def _form_exploration_coverage_keywords(
        self,
        coverage_keywords: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        """仅保留可证明目标表单/弹窗的覆盖词，避免上游导航页误满足表单阶段。"""
        if not isinstance(coverage_keywords, dict):
            return {}
        page_markers = ['新增', '新建', '添加', '创建', '表单', '弹窗', '选择', '提交', '已上传', '审核']
        action_markers = ['添加', '新增', '新建', '创建', '提交', '确定', '已上传']

        def keep(values: list[str], markers: list[str]) -> list[str]:
            kept = []
            for value in values or []:
                item = self._clean_visible_text(value)
                if item and any(marker in item for marker in markers) and item not in kept:
                    kept.append(item)
            return kept

        result = {
            'field': list(coverage_keywords.get('field') or []),
            'page': keep(coverage_keywords.get('page') or [], page_markers),
            'action': keep(coverage_keywords.get('action') or [], action_markers),
        }
        return {key: values[:12] for key, values in result.items() if values}

    def _extract_exploration_coverage_keywords(self, args: dict) -> dict[str, list[str]]:
        """从结构化需求合同提取目标页面/字段/入口关键词，用于判定探索是否真的到达目标状态。"""
        text = self._structured_requirement_corpus(args)
        keywords: dict[str, list[str]] = {'page': [], 'field': [], 'action': []}
        generic = {
            '页面', '表单', '弹窗', '字段', '入口', '按钮', '控件', '模块', '菜单', '列表',
            '打开', '进入', '访问', '点击', '可见', '采集', '可采集', '填写', '选择',
            '勾选', '录入', '验证', '断言', '用户', '新增', '新建', '添加', '创建',
            '保存', '提交', '确定', '确认', '查询', '搜索', '应', '或', '和', '数据',
        }

        def clean_candidate(value: Any) -> str:
            item = self._clean_visible_text(value)
            item = item.strip(' "\'“”‘’[]【】()（）:：,，.。;；')
            item = re.sub(r'^(?:feature|scenario|given|when|then|and|but)\s*[:：]?\s*', '', item, flags=re.IGNORECASE)
            item = re.sub(r'^(?:应|可|或|和|及|且|用户|页面中应|页面中|可见或可采集)', '', item)
            item = re.sub(r'(?:应可见|可见|可采集|字段|入口|按钮|控件|页面|表单|弹窗)$', '', item)
            return self._clean_visible_text(item)

        def add(bucket: str, value: Any) -> None:
            item = clean_candidate(value)
            if len(item) < 2 or item in generic:
                return
            if item not in keywords[bucket]:
                keywords[bucket].append(item[:50])

        for raw_line in text.splitlines():
            line = self._clean_visible_text(raw_line)
            if not line or line.startswith('#') or line.startswith('//'):
                continue
            quoted = [
                clean_candidate(match)
                for match in re.findall(r'[“"\']([^”"\']{2,60})[”"\']', line)
            ]
            line_mentions_field = any(token in line for token in ['字段', '控件', '填写', '选择', '勾选', '录入', '采集'])
            line_mentions_page = any(token in line for token in ['页面', '表单', '弹窗', '标题'])
            line_mentions_action = any(token in line for token in ['入口', '按钮', '点击'])
            for item in quoted:
                if line_mentions_field:
                    add('field', item)
                elif line_mentions_action:
                    add('action', item)
                elif line_mentions_page:
                    add('page', item)

            if quoted:
                continue

            for pattern in [
                r'(?:打开|进入|访问|直达)([\u4e00-\u9fa5A-Za-z0-9_ -]{2,60}?)(?:页面|表单|弹窗)',
                r'([\u4e00-\u9fa5A-Za-z0-9_ -]{2,60}?)(?:页面|表单|弹窗)',
            ]:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    add('page', match.group(1))

            for pattern in [
                r'[“"\']?([^”"\'，,。；;]{2,40})[”"\']?(?:字段|控件)',
                r'(?:填写|选择|勾选|录入|采集)[^“"\']{0,12}[“"\']?([^”"\'，,。；;]{2,40})[”"\']?',
            ]:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    add('field', match.group(1))

            for pattern in [
                r'[“"\']?([^”"\'，,。；;]{2,40})[”"\']?(?:入口|按钮)',
                r'(?:点击|打开)[^“"\']{0,12}[“"\']?([^”"\'，,。；;]{2,40})[”"\']?',
            ]:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    add('action', match.group(1))

        return {
            key: values[:12]
            for key, values in keywords.items()
            if values
        }

    def _missing_required_actions_coverage_keywords(
        self,
        missing_actions: Any,
    ) -> dict[str, list[str]]:
        """把后端绑定缺口转换成下一轮探索提示，不解释具体业务词。"""
        keywords: dict[str, list[str]] = {'page': [], 'field': [], 'action': []}
        if not isinstance(missing_actions, list):
            return {}

        field_operations = {
            'fill', 'type', 'input', 'select', 'select_option', 'check',
            'uncheck', 'set_checked', 'set_input_files', 'upload',
        }
        action_operations = {
            'click', 'dblclick', 'tap', 'hover', 'press',
        }

        def add(bucket: str, value: Any) -> None:
            item = self._clean_visible_text(value)
            item = item.strip(' "\'“”‘’[]【】()（）:：,，.。;；')
            if len(item) < 2:
                return
            values = keywords.setdefault(bucket, [])
            if item not in values:
                values.append(item[:50])

        for missing in missing_actions:
            if not isinstance(missing, dict):
                continue
            operation = str(missing.get('operation') or '').strip().lower()
            target_values = [
                missing.get('target'),
                missing.get('target_name'),
                missing.get('name'),
            ]
            evidence_values = [
                missing.get('value'),
                missing.get('expected'),
                missing.get('phase'),
            ]

            if operation in field_operations:
                for value in target_values:
                    add('field', value)
                continue
            if operation in action_operations:
                for value in target_values:
                    add('action', value)
                continue
            if operation.startswith('assert_'):
                for value in [*target_values, *evidence_values]:
                    add('page', value)
                continue

            for value in target_values:
                add('page', value)

        return {
            key: values[:12]
            for key, values in keywords.items()
            if values
        }

    def _llm_exploration_coverage_keywords(self, args: dict) -> dict[str, list[str]]:
        coverage = (
            args.get('llm_exploration_coverage')
            if isinstance(args.get('llm_exploration_coverage'), dict)
            else {}
        )
        missing_actions = coverage.get('missing_required_actions') if isinstance(coverage.get('missing_required_actions'), list) else []
        return self._missing_required_actions_coverage_keywords(missing_actions)

    @staticmethod
    def _merge_exploration_coverage_keywords(
        base: dict[str, list[str]],
        extra: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        merged: dict[str, list[str]] = {}
        for source in [base, extra]:
            if not isinstance(source, dict):
                continue
            for key, values in source.items():
                if not isinstance(values, list):
                    continue
                bucket = merged.setdefault(str(key), [])
                for value in values:
                    item = str(value or '').strip()
                    if item and item not in bucket:
                        bucket.append(item)
        return merged

    def _extract_requirement_exploration_route(self, requirement: Any) -> list[dict[str, Any]]:
        """Extract explicit navigation/click targets without interpreting business-specific nouns."""
        route: list[dict[str, Any]] = []
        seen: set[str] = set()
        if isinstance(requirement, dict):
            text = self._structured_requirement_corpus(requirement)
        else:
            text = str(requirement or '')
        for raw_line in text.splitlines():
            line = self._clean_visible_text(raw_line)
            if not self._line_has_exploration_route_intent(line):
                continue
            labels = [
                self._clean_visible_text(match)
                for match in re.findall(r'[“"\']([^”"\']{1,80})[”"\']', line)
            ]
            if not labels:
                segments = re.split(r'(?:->|→|—>|＞|>|\||/|／|到|进入|打开|切换|点击)', line)
                labels = [self._clean_visible_text(item) for item in segments if self._clean_visible_text(item)]
            for label in labels:
                if not label or label in seen:
                    continue
                lowered_label = label.lower()
                lowered_line = line.lower()
                opens_form = (
                    any(word in lowered_label for word in ['新建', '新增', '添加', '创建', 'add', 'create', 'new'])
                    and any(word in lowered_line for word in ['按钮', 'button', '打开', 'open'])
                )
                route.append({'label': label, 'opens_form': opens_form})
                seen.add(label)
                if opens_form:
                    return route
        return route

    def _line_has_exploration_route_intent(self, line: str) -> bool:
        line = self._clean_visible_text(line)
        if not line or line.startswith('@') or line.startswith('#') or line.startswith('//'):
            return False
        bdd_match = re.match(r'^(given|when|then|and|but|background|scenario|feature)\b[:：]?\s*(.*)$', line, re.IGNORECASE)
        bdd_keyword = (bdd_match.group(1).lower() if bdd_match else '')
        content = bdd_match.group(2) if bdd_match else line
        if bdd_keyword in {'feature', 'scenario', 'background'}:
            return False
        if bdd_keyword == 'then':
            return False
        if re.search(r'\b(?:enter|open|visit|click)\b', content, re.IGNORECASE):
            return True
        return any(verb in content for verb in ['进入', '打开', '访问', '切换', '点击'])

    def _extract_list_filter_constraints(self, args: dict) -> list[dict[str, str]]:
        text = self._structured_requirement_corpus(args)
        constraints: list[dict[str, str]] = []

        def add(field: str, value: str) -> None:
            field = self._clean_visible_text(field)
            field = re.sub(r'^(?:用户|请|搜索|查找|查询|筛选|按照|按|根据)+', '', field)
            value = self._clean_visible_text(value).strip(' "\'“”‘’')
            if not field or not value:
                return
            if len(value) < 3 or len(value) > 40:
                return
            item = {'field': field[:40], 'value': value[:40]}
            if any(
                existing.get('value') == item['value']
                and (
                    existing.get('field') == item['field']
                    or str(existing.get('field') or '').endswith(item['field'])
                    or item['field'].endswith(str(existing.get('field') or ''))
                )
                for existing in constraints
            ):
                return
            if item not in constraints:
                constraints.append(item)

        patterns = [
            r'([\u4e00-\u9fa5A-Za-z0-9_]{0,16}(?:编号|编码|ID|Id|id|号码|账号|名称))\s*(?:为|是|=|:|：)\s*[“"\']?([A-Za-z0-9_\-]{3,40})[”"\']?',
            r'(?:搜索|查找|查询|筛选)[^“"\']{0,24}([\u4e00-\u9fa5A-Za-z0-9_]{0,16}(?:编号|编码|ID|Id|id|号码|账号|名称))[^“"\']{0,12}[“"\']([A-Za-z0-9_\-]{3,40})[”"\']',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                add(match.group(1), match.group(2))
        return constraints[:4]

    def _route_label_clues(self, label: Any) -> list[str]:
        text = self._clean_visible_text(label)
        if not text:
            return []
        clues: list[str] = []

        def add(value: Any) -> None:
            item = self._clean_visible_text(value)
            if len(item) >= 2 and item not in clues:
                clues.append(item[:40])

        add(text)
        for part in re.split(r'[\s,，。；;:】【：：/|\\>＜<>]+', text):
            add(part)
        for suffix in ['管理', '列表', '页面', '弹窗', '表单', '内容', '新增', '新建', '添加', '创建', '编辑', '详情']:
            if text.endswith(suffix) and len(text) > len(suffix):
                add(text[:-len(suffix)])
        return clues[:12]

    def _target_route_clues(self, target: Optional[dict[str, Any]]) -> list[str]:
        if not isinstance(target, dict):
            return []
        clues: list[str] = []

        def add(value: Any) -> None:
            item = self._clean_visible_text(value)
            if len(item) >= 2 and item not in clues:
                clues.append(item[:50])

        for key in ['keywords', 'route_path', 'route_parent', 'route_child']:
            value = target.get(key)
            if isinstance(value, list):
                for item in value:
                    add(item)
            else:
                add(value)
        for item in target.get('route_tokens') or []:
            add(item)
        return clues[:24]

    def _rank_exploration_element_for_target(
        self,
        element: dict[str, Any],
        target: dict[str, Any],
        safety_policy: dict,
    ) -> int:
        if not self._is_atomic_exploration_candidate(element):
            return -1
        label = self._element_visible_text(element)
        lowered = label.lower()
        if not label or self._looks_dangerous(label, safety_policy):
            return -1
        if any(keyword and keyword in label for keyword in target.get('blocked_actions') or []):
            return -1

        score = 0
        for keyword in target.get('keywords') or []:
            normalized = self._clean_visible_text(keyword)
            if not normalized:
                continue
            if normalized == label:
                score += 180 + min(len(normalized), 30)
            elif normalized in label or label in normalized:
                score += 120 + min(len(normalized), 30)
            elif normalized.lower() in lowered:
                score += 90
        if score <= 0:
            return -1
        for keyword in target.get('preferred_actions') or []:
            if keyword and keyword in label:
                score += 45
        if element.get('role') in {'link', 'menuitem', 'button', 'tab'}:
            score += 12
        if element.get('href'):
            score += 8
        return score if score > 0 else -1

    async def _perform_explicit_authentication_step(
        self,
        page: Page,
        first_state: dict[str, Any],
        args: dict,
        safety_policy: dict,
        observations: list[dict[str, Any]],
        task_id: Any,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        route = self._extract_requirement_exploration_route(args)
        if not route:
            return False, None, '结构化需求中没有可执行的显式认证入口步骤'
        label = self._clean_visible_text(route[0].get('label'))
        if not label:
            return False, None, '显式认证入口步骤缺少目标标签'
        target = {
            'stage': 'explicit_authentication',
            'keywords': self._route_label_clues(label),
            'preferred_actions': [label],
            'blocked_actions': ['保存', '提交', '确定', '确认', '删除', '移除'],
        }
        candidates = [
            element for element in self._exploration_elements(first_state)
            if self._rank_exploration_element_for_target(element, target, safety_policy) >= 0
        ]
        candidates.sort(
            key=lambda element: self._rank_exploration_element_for_target(element, target, safety_policy),
            reverse=True,
        )
        if not candidates:
            return False, None, f'入口页没有匹配显式认证步骤的可点击元素: {label}'

        before_url = page.url
        clicked, error = await self._click_collected_element(page, candidates[0])
        observations.append({
            'type': 'explicit_authentication_step',
            'status': 'clicked' if clicked else 'failed',
            'target': label,
            'element': self._exploration_label(candidates[0]),
            'error': error,
        })
        if not clicked:
            return False, None, f'显式认证步骤点击失败: {error}'

        try:
            await page.wait_for_url(
                lambda url: str(url) != before_url and '/login' not in str(url).lower(),
                timeout=max(8000, min(self.action_timeout, 20000)),
            )
        except Exception:
            await page.wait_for_timeout(1500)

        state = await self._mcp_collect_page_state(
            page,
            'explicit_authentication_check',
            f"{self.screenshot_dir}/ai_generation_explicit_auth_{task_id}_{int(time.time() * 1000)}.png",
            max_elements=120,
        )
        if self._looks_like_login_page(state):
            return False, state, '显式认证步骤执行后仍停留在认证入口页'
        return True, state, ''

    def _is_atomic_exploration_candidate(self, element: dict[str, Any]) -> bool:
        """Only allow one actionable navigation control, never an aggregate container."""
        if not isinstance(element, dict) or not element.get('enabled', True):
            return False
        if element.get('visible') is False:
            return False
        if 'click' not in (element.get('actions') or []):
            return False
        role = str(element.get('role') or '').strip().lower()
        if role in {
            'menubar', 'menu', 'list', 'listbox', 'group', 'navigation',
            'region', 'generic', 'main', 'document', 'dialog', 'form',
        }:
            return False
        label = self._exploration_label(element)
        if not label or len(label) > 80 or len(label.split()) > 10:
            return False
        return role in {'menuitem', 'button', 'link', 'tab', 'treeitem'} or bool(
            element.get('href') or element.get('recommended_locator') or element.get('locator_candidates')
        )

    def _exploration_label(self, element: dict[str, Any]) -> str:
        """Use the control's own accessible label, not text inherited from its container."""
        for key in ['name', 'accessible_name', 'label', 'placeholder', 'test_id', 'text']:
            value = self._clean_visible_text(element.get(key))
            if value:
                return value
        return ''

    def _navigation_target_affinity(self, label: str, target: dict[str, Any]) -> int:
        """Prefer a navigation parent that shares a meaningful suffix with the target."""
        best = 0
        generic_suffixes = {'管理', '页面', '列表', '表单', '弹窗', '内容', '业务', '系统', '平台'}
        for keyword in target.get('keywords') or []:
            normalized = self._clean_visible_text(keyword)
            limit = min(len(label), len(normalized))
            for size in range(limit, 2, -1):
                suffix = normalized[-size:]
                if suffix in generic_suffixes or any(suffix.endswith(item) for item in generic_suffixes):
                    continue
                if label.endswith(suffix):
                    best = max(best, size * 20)
                    break
        return best

    def _target_element_is_direct_match(self, element: dict[str, Any], target: dict[str, Any]) -> bool:
        label = self._exploration_label(element).lower()
        if not label:
            return False
        for keyword in target.get('keywords') or []:
            normalized = self._clean_visible_text(keyword).lower()
            if normalized and (normalized == label or normalized in label or label in normalized):
                return True
        return False

    def _target_state_contains_keyword(self, state: dict[str, Any], target: dict[str, Any]) -> bool:
        elements = self._exploration_elements(state)
        text = self._clean_visible_text(' '.join([
            str(state.get('title') or ''),
            str(state.get('name') or ''),
            str(state.get('url') or ''),
            *[self._element_visible_text(element) for element in elements[:260] if isinstance(element, dict)],
        ])).lower()
        for keyword in target.get('keywords') or []:
            normalized = self._clean_visible_text(keyword).lower()
            if normalized and normalized in text:
                return True
        return False

    def _target_state_text(self, state: dict[str, Any]) -> str:
        elements = self._exploration_elements(state)
        return self._clean_visible_text(' '.join([
            str(state.get('title') or ''),
            str(state.get('name') or ''),
            str(state.get('url') or ''),
            *[self._element_visible_text(element) for element in elements[:260] if isinstance(element, dict)],
        ])).lower()

    def _target_keywords_in_state(self, state: dict[str, Any], target: Optional[dict[str, Any]]) -> bool:
        if not isinstance(target, dict):
            return False
        text = self._target_state_text(state)
        for keyword in target.get('keywords') or []:
            normalized = self._clean_visible_text(keyword).lower()
            if normalized and normalized in text:
                return True
        return False

    @staticmethod
    def _is_requirement_route_stage(target: dict[str, Any]) -> bool:
        return str(target.get('stage') or '').startswith('requirement_route_')

    def _route_candidate_is_relevant(
        self,
        element: dict[str, Any],
        target: dict[str, Any],
        next_target: Optional[dict[str, Any]],
    ) -> bool:
        label = self._exploration_label(element).lower()
        if not label:
            return False
        relevant_targets = [item for item in [target, next_target] if isinstance(item, dict)]
        for relevant in relevant_targets:
            keywords = [
                *self._target_route_clues(relevant),
                *(relevant.get('keywords') or []),
            ]
            for keyword in keywords:
                normalized = self._clean_visible_text(keyword).lower()
                if normalized and (normalized == label or normalized in label or label in normalized):
                    return True
        return False

    def _route_candidate_relaxed_match(
        self,
        element: dict[str, Any],
        target: dict[str, Any],
        next_target: Optional[dict[str, Any]],
    ) -> bool:
        """Relaxed fallback for route probing when the strict route filter is too narrow.

        Keep the safety boundary, but recover candidates that belong to the same
        route family, same frame, or a close navigation parent.
        """
        label = self._exploration_label(element).lower()
        if not label:
            return False
        relevant_targets = [item for item in [target, next_target] if isinstance(item, dict)]
        if not relevant_targets:
            return False

        route_clues: list[str] = []
        for relevant in relevant_targets:
            route_clues.extend(self._target_route_clues(relevant))
            route_clues.extend(relevant.get('keywords') or [])

        label_text = self._clean_visible_text(label)
        if any(self._clean_visible_text(clue).lower() in label for clue in route_clues if self._clean_visible_text(clue)):
            return True

        frame_text = self._clean_visible_text(' '.join([
            str(element.get('frame_name') or ''),
            str(element.get('frame_url') or ''),
        ])).lower()
        frame_match = frame_text and any(self._clean_visible_text(clue).lower() in frame_text for clue in route_clues if self._clean_visible_text(clue))

        role = str(element.get('role') or '').lower()
        if role in {'menuitem', 'treeitem', 'tab', 'link'}:
            if frame_match and any(keyword in label for keyword in ['内容', '表单', '弹窗', '新增', '新建', '添加', '创建']):
                return True
            if self._navigation_target_affinity(label, target) >= 24:
                return True
            if element.get('context_type') == 'frame' and frame_match:
                return True
        if element.get('href') and frame_match and any(keyword in label for keyword in ['内容', '表单', '弹窗', '新增', '新建', '添加', '创建']):
            return True
        return False

    def _target_element_matches_target(self, element: dict[str, Any], target: Optional[dict[str, Any]]) -> bool:
        if not isinstance(target, dict):
            return False
        label = self._exploration_label(element).lower()
        if not label:
            return False
        for keyword in [*self._target_route_clues(target), *(target.get('keywords') or [])]:
            normalized = self._clean_visible_text(keyword).lower()
            if normalized and (normalized == label or normalized in label or label in normalized):
                return True
        return False

    def _reconcile_completed_exploration_stages(
        self,
        targets: list[dict[str, Any]],
        completed_stages: list[str],
        state_transitions: list[dict[str, Any]],
    ) -> tuple[list[str], list[str]]:
        """Reconcile coverage from observed transitions instead of loop bookkeeping only."""
        ordered = [stage for stage in completed_stages if stage]
        completed = set(ordered)
        added: list[str] = []

        def mark(stage: str) -> None:
            if stage and stage not in completed:
                completed.add(stage)
                ordered.append(stage)
                added.append(stage)

        for transition in state_transitions:
            if not isinstance(transition, dict):
                continue
            transition_element = {
                'name': transition.get('target_name') or '',
                'text': transition.get('target_name') or '',
                'role': 'button',
                'actions': ['click'],
                'context_type': transition.get('context_type') or '',
                'frame_name': transition.get('frame_name') or '',
                'frame_url': transition.get('frame_url') or '',
            }
            for target in targets:
                stage = str(target.get('stage') or '')
                if not stage or self._target_requires_form_coverage(target):
                    continue
                if self._target_element_matches_target(transition_element, target):
                    mark(stage)

        completed_targets = [
            target for target in targets
            if str(target.get('stage') or '') in completed
        ]
        for later_target in completed_targets:
            later_path = [
                self._clean_visible_text(item)
                for item in (later_target.get('route_path') or [])
                if self._clean_visible_text(item)
            ]
            if not later_path:
                continue
            for target in targets:
                stage = str(target.get('stage') or '')
                if not stage or stage in completed:
                    continue
                route_path = [
                    self._clean_visible_text(item)
                    for item in (target.get('route_path') or [])
                    if self._clean_visible_text(item)
                ]
                if route_path and len(route_path) <= len(later_path) and route_path == later_path[:len(route_path)]:
                    mark(stage)

        return ordered, added

    def _route_target_completed(
        self,
        before_state: dict[str, Any],
        after_state: dict[str, Any],
        target: dict[str, Any],
        next_target: Optional[dict[str, Any]],
        appended: bool,
        direct_match: bool,
        clicked_next_match: bool = False,
    ) -> bool:
        if not direct_match and not clicked_next_match:
            return False
        if clicked_next_match:
            return True
        if appended:
            return True
        if str(before_state.get('url') or '') != str(after_state.get('url') or ''):
            return True
        if self._page_signature(before_state) != self._page_signature(after_state):
            return True
        if self._target_keywords_in_state(after_state, next_target):
            return True
        target_keywords = [*self._target_route_clues(target), *(target.get('keywords') or [])]
        before_text = self._target_state_text(before_state)
        after_text = self._target_state_text(after_state)
        return any(
            self._clean_visible_text(keyword).lower() in after_text
            and self._clean_visible_text(keyword).lower() not in before_text
            for keyword in target_keywords
            if self._clean_visible_text(keyword)
        )

    def _non_form_target_completed(
        self,
        before_state: dict[str, Any],
        after_state: dict[str, Any],
        target: dict[str, Any],
        appended: bool,
        direct_match: bool,
    ) -> bool:
        if not direct_match:
            return False
        if appended:
            return True
        if str(before_state.get('url') or '') != str(after_state.get('url') or ''):
            return True
        if self._page_signature(before_state) != self._page_signature(after_state):
            return True
        return self._target_state_contains_keyword(after_state, target) and not self._target_state_contains_keyword(before_state, target)

    @staticmethod
    def _exploration_elements(state: dict[str, Any]) -> list[dict[str, Any]]:
        """Expose main-document and iframe controls through one observed-element model."""
        elements = [
            item for item in (state.get('elements') or [])
            if isinstance(item, dict)
        ]
        for frame_index, frame in enumerate(state.get('frames') or []):
            if not isinstance(frame, dict):
                continue
            frame_key = str(frame.get('frame_key') or f'frame_{frame_index + 1}')
            for item in frame.get('elements') or []:
                if not isinstance(item, dict):
                    continue
                element_key = str(item.get('element_key') or '')
                elements.append({
                    **item,
                    'element_key': f'{frame_key}::{element_key}' if element_key else frame_key,
                    'source_element_key': element_key,
                    'context_type': 'frame',
                    'frame_key': frame_key,
                    'frame_name': str(frame.get('name') or ''),
                    'frame_url': str(frame.get('url') or ''),
                    'frame_index': frame_index,
                })
        return elements

    def _rank_navigation_probe(
        self,
        element: dict[str, Any],
        target: dict[str, Any],
        safety_policy: dict,
    ) -> int:
        """Rank safe atomic controls that may reveal a hidden navigation level."""
        if not self._is_atomic_exploration_candidate(element):
            return -1
        label = self._exploration_label(element)
        if self._looks_dangerous(label, safety_policy) or self._requires_confirmation(label, safety_policy):
            return -1
        if any(keyword and keyword in label for keyword in target.get('blocked_actions') or []):
            return -1
        role = str(element.get('role') or '').lower()
        navigation_words = ['菜单', '管理', '资源', '系统', '工具', '业务']
        if role == 'button' and not element.get('href') and not any(word in label for word in navigation_words):
            return -1
        score = {'menuitem': 50, 'treeitem': 45, 'tab': 30, 'link': 25, 'button': 10}.get(role, 0)
        if any(keyword in label for keyword in navigation_words):
            score += 20
        score += self._navigation_target_affinity(label, target)
        if element.get('href'):
            score += 5
        return score if score > 0 else -1

    @staticmethod
    def _target_requires_form_coverage(target: dict[str, Any]) -> bool:
        return target.get('stage') == 'open_business_form'

    def _state_has_form_structure(self, state: dict[str, Any]) -> bool:
        elements = self._exploration_elements(state)
        has_field = any(
            isinstance(item, dict)
            and (
                'fill' in (item.get('actions') or [])
                or 'select_option' in (item.get('actions') or [])
                or self._looks_like_select_control(item)
            )
            for item in elements
        )
        has_form_context = any(
            isinstance(item, dict)
            and (
                item.get('required')
                or item.get('in_dialog')
                or item.get('dialog_name')
                or item.get('form_label')
                or item.get('in_form')
            )
            for item in elements
        )
        return has_field and has_form_context

    def _is_business_form_covered(self, state: dict[str, Any], safety_policy: dict) -> bool:
        elements = self._exploration_elements(state)
        has_required_field = any(item.get('required') for item in elements)
        has_fillable = any('fill' in (item.get('actions') or []) or 'select_option' in (item.get('actions') or []) for item in elements)
        has_confirmation = any(self._requires_confirmation(self._element_visible_text(item), safety_policy) for item in elements)
        has_dialog_context = any(
            item.get('in_dialog')
            or item.get('dialog_name')
            or str(item.get('role') or '').lower() == 'dialog'
            for item in elements
        )
        has_generic_submit = any(
            'click' in (item.get('actions') or [])
            and any(keyword in self._element_visible_text(item).lower() for keyword in ['保存', '提交', '确定', '确认', 'save', 'submit', 'confirm', 'ok'])
            for item in elements
        )
        return has_fillable and (has_confirmation or has_generic_submit or has_dialog_context or has_required_field)

    def _rank_form_entry_probe(
        self,
        element: dict[str, Any],
        target: dict[str, Any],
        safety_policy: dict,
    ) -> int:
        """Rank generic controls that may open a business form; completion is verified after click."""
        if not self._is_atomic_exploration_candidate(element):
            return -1
        label = self._exploration_label(element)
        if self._looks_dangerous(label, safety_policy) or self._requires_confirmation(label, safety_policy):
            return -1
        if any(keyword and keyword in label for keyword in target.get('blocked_actions') or []):
            return -1
        role = str(element.get('role') or '').lower()
        tag = str(element.get('tag') or '').lower()
        input_type = str(element.get('input_type') or '').lower()
        is_button_like = role == 'button' or tag == 'button' or (tag == 'input' and input_type in {'button', 'submit'})
        if not is_button_like:
            return -1
        if element.get('in_dialog') or element.get('dialog_name') or element.get('in_form'):
            return -1
        box = element.get('bounding_box') if isinstance(element.get('bounding_box'), dict) else {}
        width = float(box.get('width') or 0)
        height = float(box.get('height') or 0)
        if width <= 0 or height <= 0:
            return -1
        y = float(box.get('y') or 0)
        score = 120
        if role == 'button':
            score += 25
        score += max(0, 800 - int(max(y, 0))) // 10
        return score

    def _looks_like_search_or_list_page(self, state: dict[str, Any], safety_policy: dict) -> bool:
        elements = self._exploration_elements(state)
        text = self._clean_visible_text(' '.join([
            str(state.get('title') or ''),
            str(state.get('name') or ''),
            str(state.get('url') or ''),
            *[self._element_visible_text(element) for element in elements[:260] if isinstance(element, dict)],
        ]))
        has_search_action = any(
            'click' in (element.get('actions') or [])
            and any(keyword in self._element_visible_text(element) for keyword in ['查询', '搜索', '重置'])
            for element in elements
            if isinstance(element, dict)
        )
        has_table_or_pager = any(keyword in text for keyword in ['上一页', '下一页', '每页', '共', '列表'])
        has_dialog_or_submit = any(
            element.get('in_dialog')
            or element.get('dialog_name')
            or self._requires_confirmation(self._element_visible_text(element), safety_policy)
            for element in elements
            if isinstance(element, dict)
        )
        return (has_search_action or has_table_or_pager) and not has_dialog_or_submit

    def _is_target_business_form_covered(
        self,
        state: dict[str, Any],
        target: dict[str, Any],
        safety_policy: dict,
    ) -> bool:
        """判定探索是否到达目标表单页，而不是任意带输入框的列表/搜索页。"""
        if not self._is_business_form_covered(state, safety_policy):
            return False

        coverage_keywords = target.get('coverage_keywords') if isinstance(target.get('coverage_keywords'), dict) else {}
        target_words = [
            self._clean_visible_text(value)
            for values in coverage_keywords.values()
            if isinstance(values, list)
            for value in values
        ]
        target_words = [word for word in target_words if len(word) >= 2]
        if not target_words:
            return True

        elements = self._exploration_elements(state)
        title_text = self._clean_visible_text(' '.join([
            str(state.get('title') or ''),
            str(state.get('name') or ''),
            str(state.get('url') or ''),
        ]))
        page_text = self._clean_visible_text(' '.join([
            title_text,
            *[
                self._element_visible_text(element)
                for element in elements[:260]
                if isinstance(element, dict)
            ],
        ]))
        matched_words = [
            word for word in target_words
            if word and word in page_text
        ]
        if not matched_words:
            return False
        route_clues = [clue for clue in self._target_route_clues(target) if len(clue) >= 2]
        if route_clues and not any(clue in page_text for clue in route_clues):
            return False
        if self._looks_like_search_or_list_page(state, safety_policy):
            action_words_for_cover = [
                self._clean_visible_text(value)
                for value in coverage_keywords.get('action') or []
                if self._clean_visible_text(value)
            ]
            if not any(word and word in page_text for word in action_words_for_cover):
                return False

        field_words = [
            self._clean_visible_text(value)
            for value in coverage_keywords.get('field') or []
            if self._clean_visible_text(value)
        ]
        if not field_words:
            return True

        controls = [
            element for element in elements
            if isinstance(element, dict)
            and (
                'fill' in (element.get('actions') or [])
                or 'select_option' in (element.get('actions') or [])
                or self._looks_like_select_control(element)
            )
        ]
        matched_fields = [
            word for word in field_words
            if any(word in self._element_visible_text(element) for element in controls)
        ]
        if len(matched_fields) >= 2:
            return True

        page_words = [
            self._clean_visible_text(value)
            for value in coverage_keywords.get('page') or []
            if self._clean_visible_text(value)
        ]
        action_words = [
            self._clean_visible_text(value)
            for value in coverage_keywords.get('action') or []
            if self._clean_visible_text(value)
        ]
        return bool(
            len(matched_words) >= 2
            and (
                any(word in page_text for word in page_words)
                or any(word in page_text for word in action_words)
            )
        )

    async def _click_collected_element(self, page: Page, element: dict[str, Any]) -> tuple[bool, str]:
        candidates = self._dedupe_locators(
            ([element.get('recommended_locator')] if isinstance(element.get('recommended_locator'), dict) else [])
            + (element.get('locator_candidates') or [])
        )
        errors = []
        candidates.sort(key=self._locator_click_priority)
        context = self._locator_context(page, element)
        for candidate in candidates[:4]:
            try:
                locator = await self._first_visible_locator(self._candidate_locator(context, candidate), timeout=2500)
                await self._click_with_visibility_fallback(locator, page=page, timeout=3000, force_timeout=3000)
                try:
                    await page.wait_for_load_state('networkidle', timeout=6000)
                except Exception:
                    await page.wait_for_timeout(800)
                return True, ''
            except Exception as exc:
                errors.append(str(exc)[:200])
        return False, '; '.join(errors[:3])

    async def _fill_collected_element(self, page: Page, element: dict[str, Any], value: str) -> tuple[bool, str]:
        candidates = self._dedupe_locators(
            ([element.get('recommended_locator')] if isinstance(element.get('recommended_locator'), dict) else [])
            + (element.get('locator_candidates') or [])
        )
        errors = []
        candidates.sort(key=self._locator_fill_priority)
        context = self._locator_context(page, element)
        for candidate in candidates[:4]:
            try:
                locator = await self._first_visible_locator(self._candidate_locator(context, candidate), timeout=2500)
                await locator.fill(value, timeout=3000)
                return True, ''
            except Exception as exc:
                errors.append(str(exc)[:200])
        return False, '; '.join(errors[:3])

    async def _apply_list_filter_constraints(
        self,
        page: Page,
        state: dict[str, Any],
        constraints: list[dict[str, str]],
        applied_signatures: set[str],
        observations: list[dict[str, Any]],
        task_id: Any,
    ) -> dict[str, Any]:
        if not constraints:
            return state
        elements = self._exploration_elements(state)
        search_buttons = [
            element for element in elements
            if isinstance(element, dict)
            and 'click' in (element.get('actions') or [])
            and any(keyword in self._exploration_label(element) for keyword in ['搜索', '查询', '筛选'])
        ]
        if not search_buttons:
            return state

        for constraint in constraints:
            field = self._clean_visible_text(constraint.get('field'))
            value = self._clean_visible_text(constraint.get('value'))
            if not field or not value:
                continue
            field_clues = [field]
            for suffix in ['编号', '编码', '号码', '账号', '名称', 'ID', 'Id', 'id']:
                if field.endswith(suffix) and len(field) > len(suffix):
                    field_clues.append(field[:-len(suffix)])
            candidates = [
                element for element in elements
                if isinstance(element, dict)
                and 'fill' in (element.get('actions') or [])
                and any(clue and clue in self._element_visible_text(element) for clue in field_clues)
            ]
            if not candidates:
                continue
            candidates.sort(key=lambda item: (
                0 if field in self._element_visible_text(item) else 1,
                len(self._element_visible_text(item)),
            ))
            field_element = candidates[0]
            signature = '|'.join([
                str(state.get('page_key') or ''),
                str(field_element.get('context_type') or 'main'),
                str(field_element.get('frame_url') or ''),
                str(field_element.get('element_key') or ''),
                field,
                value,
            ])
            if signature in applied_signatures:
                continue
            filled, fill_error = await self._fill_collected_element(page, field_element, value)
            if not filled:
                observations.append({
                    'type': 'business_exploration_filter_failed',
                    'field': field,
                    'value': value,
                    'error': fill_error,
                })
                applied_signatures.add(signature)
                continue
            search_buttons.sort(key=lambda item: (
                0 if (item.get('context_type') or 'main') == (field_element.get('context_type') or 'main') else 1,
                0 if str(item.get('frame_url') or '') == str(field_element.get('frame_url') or '') else 1,
                len(self._exploration_label(item)),
            ))
            clicked, click_error = await self._click_collected_element(page, search_buttons[0])
            applied_signatures.add(signature)
            observations.append({
                'type': 'business_exploration_filter',
                'field': field,
                'value': value,
                'status': 'success' if clicked else 'failed',
                'search_target': self._exploration_label(search_buttons[0]),
                'error': click_error if not clicked else '',
            })
            if not clicked:
                return state
            await self._wait_for_business_state_stable(page)
            return await self._mcp_collect_page_state(
                page,
                f'page_filter_{len(applied_signatures)}',
                f"{self.screenshot_dir}/ai_generation_{task_id}_filter_{len(applied_signatures)}.png",
                max_elements=220,
            )
        return state

    async def _wait_for_business_state_stable(self, page: Page) -> None:
        """等待动态弹窗、表单和异步渲染稳定后再采集页面状态。"""
        try:
            await page.wait_for_load_state('networkidle', timeout=6000)
        except Exception:
            await page.wait_for_timeout(600)
        try:
            await page.wait_for_function(
                """
                () => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const dialogs = Array.from(document.querySelectorAll(
                    '[role="dialog"], .el-dialog, .arco-modal, .ant-modal, .arco-drawer, .ant-drawer, [class*="dialog"], [class*="modal"], [class*="drawer"]'
                  )).filter(visible);
                  if (!dialogs.length) return true;
                  return dialogs.some((dialog) => {
                    const fields = dialog.querySelectorAll('input, textarea, select, [role="combobox"], [contenteditable="true"]').length;
                    const buttons = dialog.querySelectorAll('button, [role="button"]').length;
                    return fields > 0 || buttons > 0;
                  });
                }
                """,
                timeout=4000,
            )
        except Exception:
            await page.wait_for_timeout(800)

    async def _navigate_for_observation(self, page: Page, url: str) -> None:
        """Navigate for observation without treating slow DOM events as fatal.

        Some intranet/VPN H5 pages commit the main document quickly but keep
        long-running resources or scripts that delay Playwright's
        ``domcontentloaded`` wait. For element discovery, a committed document
        plus bounded stabilization is a better signal than failing the whole
        task on a single lifecycle timeout.
        """
        navigation_timeout = max(self.action_timeout, self.launch_timeout, 45000)
        await page.goto(url, wait_until='commit', timeout=navigation_timeout)
        try:
            await page.wait_for_load_state('domcontentloaded', timeout=min(navigation_timeout, 15000))
        except Exception as exc:
            logger.warning(f"等待 DOMContentLoaded 超时，继续采集页面状态: {exc}")
        try:
            await page.wait_for_load_state('networkidle', timeout=min(self.action_timeout, 3000))
        except Exception:
            await page.wait_for_timeout(500)

    async def _collect_dynamic_options_for_element(self, page: Page, element: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = self._dedupe_locators(
            ([element.get('recommended_locator')] if isinstance(element.get('recommended_locator'), dict) else [])
            + (element.get('locator_candidates') or [])
        )
        candidates.sort(key=self._locator_click_priority)
        for candidate in candidates[:3]:
            try:
                locator = await self._first_visible_locator(self._candidate_locator(page, candidate), timeout=2000)
                await self._click_with_visibility_fallback(locator, page=page, timeout=2500, force_timeout=2500)
                await page.wait_for_timeout(500)
                options = await page.evaluate(
                    """
                    () => {
                      const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                      const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                      };
                      const selector = [
                        '[role="option"]',
                        '.el-select-dropdown__item',
                        '.el-picker-panel [role="button"]',
                        '.ant-select-item-option',
                        '.arco-select-option',
                        '[class*="option"], [class*="Option"]'
                      ].join(',');
                      return Array.from(document.querySelectorAll(selector))
                        .filter(visible)
                        .map((el) => ({label: textOf(el).slice(0, 120), disabled: el.getAttribute('aria-disabled') === 'true' || el.classList.contains('is-disabled')}))
                        .filter((item) => item.label && !item.disabled)
                        .slice(0, 20);
                    }
                    """
                )
                await page.keyboard.press('Escape')
                return options or []
            except Exception:
                try:
                    await page.keyboard.press('Escape')
                except Exception:
                    pass
        return []

    async def _enrich_dynamic_form_context(
        self,
        page: Page,
        state: dict[str, Any],
        observations: list[dict[str, Any]],
        coverage_keywords: Optional[dict[str, list[str]]] = None,
        max_controls: int = 6,
    ) -> dict[str, Any]:
        keyword_values = [
            str(value)
            for values in (coverage_keywords or {}).values()
            if isinstance(values, list)
            for value in values
        ]
        if keyword_values:
            refreshed = await self._mcp_collect_page_state(
                page,
                state.get('page_key') or 'form_enriched',
                state.get('screenshot') or f"{self.screenshot_dir}/ai_generation_form_enriched_{int(time.time() * 1000)}.png",
                max_elements=260,
                priority_keywords=keyword_values,
            )
            state.update({
                key: value
                for key, value in refreshed.items()
                if key not in {'page_key', 'screenshot'} or not state.get(key)
            })
            observations.append({
                'type': 'dynamic_form_priority_capture',
                'page_key': state.get('page_key'),
                'keywords': keyword_values[:16],
                'element_count': len(state.get('elements') or []),
                'frame_element_count': sum(
                    int(frame.get('element_count') or 0)
                    for frame in state.get('frames') or []
                    if isinstance(frame, dict)
                ),
            })

        elements = self._exploration_elements(state)
        controls = [
            element for element in elements
            if (
                element.get('in_dialog')
                and (
                    'select_option' in (element.get('actions') or [])
                    or element.get('role') in {'combobox', 'select'}
                    or '请选择' in self._element_visible_text(element)
                )
            )
        ][:max_controls]
        sampled = 0
        for element in controls:
            options = await self._collect_dynamic_options_for_element(page, element)
            if options:
                existing = element.get('options') if isinstance(element.get('options'), list) else []
                element['options'] = existing or options
                element['dynamic_options'] = options
                sampled += 1
        if sampled:
            observations.append({
                'type': 'dynamic_form_options_sampled',
                'page_key': state.get('page_key'),
                'control_count': sampled,
            })
        return state

    @staticmethod
    def _locator_fill_priority(candidate: dict[str, Any]) -> int:
        order = {
            'test_id': 0,
            'label': 1,
            'placeholder': 2,
            'name': 3,
            'id': 4,
            'css': 5,
            'xpath': 6,
            'role': 7,
            'text': 8,
        }
        locator_type = candidate.get('type')
        if locator_type == 'role' and candidate.get('name'):
            return 3
        return order.get(locator_type, 20)

    @staticmethod
    def _locator_click_priority(candidate: dict[str, Any]) -> int:
        order = {
            'test_id': 0,
            'role': 1,
            'text': 2,
            'label': 3,
            'id': 4,
            'css': 5,
            'xpath': 6,
        }
        return order.get(candidate.get('type'), 20)

    def _score_image_captcha_input(self, element: dict[str, Any]) -> int:
        if 'fill' not in (element.get('actions') or []):
            return -1
        text = self._element_visible_text(element).lower()
        if any(keyword in text for keyword in ['短信验证码', '手机验证码', 'sms code', 'otp']):
            return -1
        score = 0
        for keyword in ['图形验证码', '图片验证码', '验证码', 'captcha', 'verifycode', 'checkcode']:
            if keyword in text:
                score += 30
        if str(element.get('input_type') or '').lower() in {'code', 'captcha'}:
            score += 20
        return score if score > 0 else -1

    def _score_login_submit_element(self, element: dict[str, Any]) -> int:
        if 'click' not in (element.get('actions') or []):
            return -1
        label = self._element_visible_text(element).lower()
        normalized = re.sub(r'\s+', ' ', label).strip()
        exact_submit_labels = {
            '登录', '登陆', '立即登录', '登录系统', 'login', 'sign in', 'submit',
        }
        score = 100 if normalized in exact_submit_labels else 0
        if str(element.get('tag') or '').lower() in {'button', 'input'}:
            score += 10
        if str(element.get('input_type') or '').lower() == 'submit':
            score += 50
        if any(keyword in normalized for keyword in ['登录', '登陆', 'login', 'sign in', 'submit']):
            score += 25
        if any(keyword in normalized for keyword in [
            '账号密码登录', '密码登录', '手机验证码登录', '短信登录',
            '登录方式', '切换登录', 'password login', 'sms login',
        ]):
            score -= 80
        return score if score > 0 else -1

    async def _resolve_auto_login_captcha(
        self,
        page: Page,
        elements: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> bool:
        candidates = sorted(elements, key=self._score_image_captcha_input, reverse=True)
        captcha_element = (
            candidates[0]
            if candidates and self._score_image_captcha_input(candidates[0]) > 0
            else None
        )
        if not captcha_element:
            return False

        locator_candidates = self._dedupe_locators(
            ([captcha_element.get('recommended_locator')]
             if isinstance(captcha_element.get('recommended_locator'), dict) else [])
            + (captcha_element.get('locator_candidates') or [])
        )
        locator_candidates.sort(key=self._locator_fill_priority)
        last_error = ''
        for candidate in locator_candidates[:4]:
            try:
                locator = await self._first_visible_locator(
                    self._candidate_locator(page, candidate),
                    timeout=3000,
                )
                _, artifact_path = await self._resolve_runtime_input(
                    page,
                    locator,
                    'image_ocr',
                    'auto_login',
                )
                observations.append({
                    'type': 'auto_login_runtime_input',
                    'status': 'success',
                    'resolver': 'image_ocr',
                    'element_key': captcha_element.get('element_key') or '',
                    'target_name': captcha_element.get('name') or captcha_element.get('accessible_name') or '',
                    'artifact_path': artifact_path,
                })
                return True
            except Exception as exc:
                last_error = str(exc)[:500]

        observations.append({
            'type': 'auto_login_runtime_input',
            'status': 'failed',
            'resolver': 'image_ocr',
            'element_key': captcha_element.get('element_key') or '',
            'error': last_error or '验证码输入框没有可执行定位器',
        })
        raise RuntimeError(f'自动登录验证码 OCR 失败: {last_error or "验证码输入框没有可执行定位器"}')

    async def _check_auto_login_consents(
        self,
        page: Page,
        elements: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> bool:
        consent_elements = []
        for element in elements:
            if 'check' not in (element.get('actions') or []):
                continue
            label = self._element_visible_text(element).lower()
            has_consent_action = any(keyword in label for keyword in ['同意', '接受', 'agree', 'accept'])
            has_policy_subject = any(keyword in label for keyword in ['协议', '隐私', '条款', 'terms', 'privacy', 'policy'])
            if has_consent_action and has_policy_subject:
                consent_elements.append(element)
        if not consent_elements:
            return False

        last_error = ''
        for element in consent_elements:
            locator_candidates = self._dedupe_locators(
                ([element.get('recommended_locator')]
                 if isinstance(element.get('recommended_locator'), dict) else [])
                + (element.get('locator_candidates') or [])
            )
            locator_candidates.sort(key=self._locator_click_priority)
            for candidate in locator_candidates[:4]:
                try:
                    locator = await self._first_visible_locator(
                        self._candidate_locator(page, candidate),
                        timeout=3000,
                    )
                    if not await locator.is_checked():
                        await locator.check(timeout=3000)
                    observations.append({
                        'type': 'auto_login_consent',
                        'status': 'success',
                        'element_key': element.get('element_key') or '',
                        'target_name': element.get('name') or element.get('accessible_name') or '',
                    })
                    return True
                except Exception as exc:
                    last_error = str(exc)[:500]

        observations.append({
            'type': 'auto_login_consent',
            'status': 'failed',
            'error': last_error or '登录协议控件没有可执行定位器',
        })
        raise RuntimeError(f'自动登录协议勾选失败: {last_error or "登录协议控件没有可执行定位器"}')

    async def _auto_login_if_possible(
        self,
        page: Page,
        first_state: dict[str, Any],
        credentials: dict[str, str],
        safety_policy: dict,
        observations: list[dict[str, Any]],
    ) -> bool:
        username = credentials.get('username') or ''
        password = credentials.get('password') or ''
        if not username or not password:
            observations.append({
                'type': 'auto_login_skipped',
                'reason': '缺少可用登录账号或密码',
            })
            return False

        elements = [item for item in (first_state.get('elements') or []) if isinstance(item, dict)]

        def score_username(element: dict[str, Any]) -> int:
            if 'fill' not in (element.get('actions') or []):
                return -1
            text = self._element_visible_text(element).lower()
            if 'password' in text or '密码' in text or element.get('input_type') == 'password':
                return -1
            score = 0
            for keyword in ['用户名', '账号', '账户', '登录名', '用户', '手机', '邮箱', 'user', 'account', 'login', 'mobile', 'phone', 'email']:
                if keyword in text:
                    score += 20
            if element.get('tag') == 'input':
                score += 5
            return score

        def score_password(element: dict[str, Any]) -> int:
            if 'fill' not in (element.get('actions') or []):
                return -1
            text = self._element_visible_text(element).lower()
            score = 50 if element.get('input_type') == 'password' else 0
            for keyword in ['密码', '口令', 'password', 'passwd', 'pwd']:
                if keyword in text:
                    score += 25
            return score

        username_candidates = sorted(elements, key=score_username, reverse=True)
        password_candidates = sorted(elements, key=score_password, reverse=True)
        username_element = username_candidates[0] if username_candidates and score_username(username_candidates[0]) >= 0 else None
        password_element = password_candidates[0] if password_candidates and score_password(password_candidates[0]) > 0 else None

        if not username_element or not password_element:
            observations.append({
                'type': 'auto_login_skipped',
                'reason': '未能在入口页识别用户名或密码输入框',
            })
            return False

        try:
            login_url = page.url
            for element, value in [(username_element, username), (password_element, password)]:
                candidates = self._dedupe_locators(
                    ([element.get('recommended_locator')] if isinstance(element.get('recommended_locator'), dict) else [])
                    + (element.get('locator_candidates') or [])
                )
                candidates.sort(key=self._locator_fill_priority)
                filled = False
                last_error = ''
                for candidate in candidates[:4]:
                    try:
                        locator = await self._first_visible_locator(self._candidate_locator(page, candidate), timeout=3000)
                        await locator.fill(value, timeout=3000)
                        filled = True
                        break
                    except Exception as exc:
                        last_error = str(exc)[:300]
                if not filled:
                    raise RuntimeError(f"填写登录字段失败: {element.get('name')}, {last_error}")

            await self._resolve_auto_login_captcha(page, elements, observations)
            await self._check_auto_login_consents(page, elements, observations)

            login_buttons = [
                element for element in elements
                if self._score_login_submit_element(element) > 0
                and not self._looks_dangerous(self._element_visible_text(element), safety_policy)
            ]
            login_buttons.sort(key=self._score_login_submit_element, reverse=True)
            clicked = False
            if login_buttons:
                clicked, error = await self._click_collected_element(page, login_buttons[0])
                if not clicked:
                    logger.warning(f"点击登录按钮失败，尝试回车提交: {error}")
            if not clicked:
                await page.keyboard.press('Enter')
                try:
                    await page.wait_for_load_state('networkidle', timeout=6000)
                except Exception:
                    await page.wait_for_timeout(1200)

            try:
                await page.wait_for_url(
                    lambda url: str(url) != login_url and '/login' not in str(url).lower(),
                    timeout=max(8000, min(self.action_timeout, 15000)),
                )
            except Exception:
                try:
                    await self._first_visible_locator(page.get_by_text('退出登录'), timeout=5000)
                except Exception:
                    try:
                        await self._first_visible_locator(page.get_by_text(username), timeout=3000)
                    except Exception:
                        await page.wait_for_timeout(1500)

            check_state = await self._mcp_collect_page_state(
                page,
                'login_check',
                f"{self.screenshot_dir}/ai_generation_login_check_{int(time.time() * 1000)}.png",
                max_elements=40,
            )
            if self._looks_like_login_page(check_state):
                raise RuntimeError('登录后仍停留在登录页，未进入系统')

            observations.append({
                'type': 'auto_login',
                'status': 'success',
                'username_element': username_element.get('name'),
                'password_element': password_element.get('name'),
                'url': page.url,
            })
            return True
        except Exception as exc:
            observations.append({
                'type': 'auto_login',
                'status': 'failed',
                'error': str(exc)[:800],
            })
            return False

    async def _auto_explore_business_pages(
        self,
        page: Page,
        task_id: Any,
        first_state: dict[str, Any],
        args: dict,
        safety_policy: dict,
        base_url: str,
        max_pages: int,
        max_steps: int,
        observations: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pages: list[dict[str, Any]] = []
        state_transitions: list[dict[str, Any]] = []
        seen_signatures: set[str] = set()
        clicked_keys: set[str] = set()
        clicked_navigation_labels: set[str] = set()
        root_navigation_labels: set[str] = set()
        self._append_unique_page_state(pages, seen_signatures, first_state)

        exploration_targets = self._build_exploration_targets(args)
        list_filter_constraints = self._extract_list_filter_constraints(args)
        applied_filter_signatures: set[str] = set()
        required_stages = [str(target.get('stage') or '') for target in exploration_targets]
        observations.append({
            'type': 'business_exploration_plan',
            'targets': exploration_targets,
            'list_filter_constraints': list_filter_constraints,
        })

        authentication_mode = self._authentication_mode(args, first_state)
        observations.append({
            'type': 'authentication_strategy',
            'mode': authentication_mode,
            'entry_page_key': first_state.get('page_key') or '',
            'entry_url': first_state.get('url') or page.url,
        })
        if authentication_mode == 'unknown' and self._looks_like_login_page(first_state):
            reason = self._authentication_contract_unavailable_reason(args)
            observations.append({
                'type': 'business_exploration_stop',
                'reason': reason,
                'url': page.url,
            })
            observations.append({
                'type': 'business_exploration_coverage',
                'status': 'deferred',
                'planning_allowed': False,
                'failure_category': 'permission',
                'blocked_by': 'authentication',
                'required_stages': required_stages,
                'completed_stages': [],
                'missing_stages': required_stages,
                'steps_used': 0,
                'reason': reason,
            })
            return pages, state_transitions
        if authentication_mode == 'test_subject':
            observations.append({
                'type': 'business_exploration_coverage',
                'status': 'deferred',
                'planning_allowed': bool(first_state.get('elements')),
                'required_stages': ['authentication_subject'],
                'completed_stages': ['authentication_subject'],
                'missing_stages': [],
                'steps_used': 0,
                'reason': '认证是被测流程，登录页作为测试计划起点，不执行前置自动登录',
            })
            return pages, state_transitions

        explicit_authentication_success = False
        if authentication_mode == 'authentication_then_business' and self._looks_like_login_page(first_state):
            auth_success, auth_state, auth_error = await self._perform_explicit_authentication_step(
                page,
                first_state,
                args,
                safety_policy,
                observations,
                task_id,
            )
            if not auth_success:
                observations.append({
                    'type': 'business_exploration_stop',
                    'reason': auth_error or '显式认证阶段未能进入后续业务页面',
                    'url': page.url,
                })
                observations.append({
                    'type': 'business_exploration_coverage',
                    'status': 'deferred',
                    'planning_allowed': False,
                    'failure_category': 'permission',
                    'blocked_by': 'authentication',
                    'auth_error': auth_error[:800],
                    'required_stages': required_stages,
                    'completed_stages': [],
                    'missing_stages': required_stages,
                    'steps_used': 0,
                    'reason': auth_error or '显式认证阶段未能进入后续业务页面',
                })
                return pages, state_transitions
            if auth_state and self._append_unique_page_state(pages, seen_signatures, auth_state):
                observations.append({
                    'type': 'post_explicit_authentication_snapshot',
                    'url': auth_state.get('url'),
                    'title': auth_state.get('title'),
                    'element_count': len(auth_state.get('elements') or []),
                })
            if auth_state:
                first_state = auth_state
            explicit_authentication_success = True

        credentials = self._extract_login_credentials(args)
        login_success = explicit_authentication_success
        if not login_success:
            login_success = await self._auto_login_if_possible(page, first_state, credentials, safety_policy, observations)
        if not login_success and self._looks_like_login_page(first_state):
            auth_failure = next(
                (
                    item for item in reversed(observations)
                    if isinstance(item, dict) and item.get('type') == 'auto_login'
                ),
                {},
            )
            observations.append({
                'type': 'business_exploration_stop',
                'reason': '自动登录未成功，停止登录后业务页面探索',
                'url': page.url,
            })
            observations.append({
                'type': 'business_exploration_coverage',
                'status': 'deferred',
                'planning_allowed': False,
                'failure_category': 'permission',
                'blocked_by': 'authentication',
                'auth_error': str(auth_failure.get('error') or '')[:800],
                'required_stages': required_stages,
                'completed_stages': [],
                'missing_stages': required_stages,
                'steps_used': 0,
                'reason': '前置认证不可用，当前元素地图仍停留在登录页，不能用于业务 LLM 规划',
            })
            return pages, state_transitions
        if login_success and len(pages) < max_pages:
            screenshot_path = f"{self.screenshot_dir}/ai_generation_{task_id}_page_{len(pages) + 1}.png"
            state = await self._mcp_collect_page_state(page, f'page_{len(pages) + 1}', screenshot_path)
            if self._append_unique_page_state(pages, seen_signatures, state):
                observations.append({
                    'type': 'post_login_snapshot',
                    'url': state.get('url'),
                    'title': state.get('title'),
                    'element_count': len(state.get('elements') or []),
                    'screenshot': screenshot_path,
                })

        exploration_steps = 0
        completed_stages: list[str] = []

        for target in exploration_targets:
            stage = str(target.get('stage') or '')
            target_index = exploration_targets.index(target)
            next_target = exploration_targets[target_index + 1] if target_index + 1 < len(exploration_targets) else None
            target_budget = min(8, max_steps - exploration_steps)
            probes = 0
            while probes < target_budget and len(pages) < max_pages and exploration_steps < max_steps:
                current_index = len(pages) + 1
                await self._wait_for_business_state_stable(page)
                current_state = await self._mcp_collect_page_state(
                    page,
                    f'page_probe_{current_index}',
                    f"{self.screenshot_dir}/ai_generation_{task_id}_probe_{current_index}.png",
                    max_elements=220,
                    priority_keywords=[
                        str(value)
                        for values in (
                            target.get('coverage_keywords') if isinstance(target.get('coverage_keywords'), dict) else {}
                        ).values()
                        if isinstance(values, list)
                        for value in values
                    ] if self._target_requires_form_coverage(target) else None,
                )
                current_state = await self._apply_list_filter_constraints(
                    page,
                    current_state,
                    list_filter_constraints,
                    applied_filter_signatures,
                    observations,
                    task_id,
                )
                if not root_navigation_labels:
                    root_navigation_labels = {
                        self._exploration_label(element)
                        for element in current_state.get('elements') or []
                        if isinstance(element, dict)
                        and str(element.get('role') or '').lower() in {'menuitem', 'treeitem'}
                        and self._exploration_label(element)
                    }
                requires_form = self._target_requires_form_coverage(target)
                if requires_form and self._is_target_business_form_covered(current_state, target, safety_policy):
                    current_state = await self._enrich_dynamic_form_context(
                        page,
                        current_state,
                        observations,
                        target.get('coverage_keywords') if isinstance(target.get('coverage_keywords'), dict) else {},
                    )
                    self._append_unique_page_state(pages, seen_signatures, current_state)
                    completed_stages.append(stage)
                    break

                ranked: list[tuple[int, str, dict[str, Any], bool]] = []
                for element in self._exploration_elements(current_state):
                    if not isinstance(element, dict):
                        continue
                    click_key = '|'.join([
                        str(page.url),
                        str(element.get('context_type') or 'main'),
                        str(element.get('frame_url') or ''),
                        str(element.get('element_key') or ''),
                        self._exploration_label(element)[:120],
                    ])
                    if click_key in clicked_keys:
                        continue
                    direct_score = self._rank_exploration_element_for_target(element, target, safety_policy)
                    direct_match = self._target_element_is_direct_match(element, target)
                    next_match = self._target_element_matches_target(element, next_target)
                    label = self._exploration_label(element)
                    role = str(element.get('role') or '').lower()
                    if not direct_match and role in {'menuitem', 'treeitem', 'tab', 'link'} and label in clicked_navigation_labels:
                        continue
                    if self._is_requirement_route_stage(target):
                        if not self._route_candidate_is_relevant(element, target, next_target):
                            continue
                    if direct_score > 0:
                        ranked.append((direct_score + (200 if direct_match else 0) + (120 if next_match else 0), click_key, element, direct_match))
                        continue
                    if requires_form:
                        form_probe_score = self._rank_form_entry_probe(element, target, safety_policy)
                        if form_probe_score > 0:
                            ranked.append((form_probe_score, click_key, element, False))
                        continue
                    probe_score = self._rank_navigation_probe(element, target, safety_policy)
                    route_relevant = self._route_candidate_is_relevant(element, target, next_target)
                    if self._is_requirement_route_stage(target):
                        if not route_relevant and not self._route_candidate_relaxed_match(element, target, next_target):
                            continue
                        if not route_relevant and probe_score > 0:
                            probe_score = max(probe_score, 18)
                    if probe_score > 0:
                        if label in root_navigation_labels:
                            probe_score += 80
                        if next_match:
                            probe_score += 120
                        ranked.append((probe_score, click_key, element, False))

                if not ranked and self._is_requirement_route_stage(target):
                    relaxed_ranked: list[tuple[int, str, dict[str, Any], bool]] = []
                    for element in self._exploration_elements(current_state):
                        if not isinstance(element, dict):
                            continue
                        click_key = '|'.join([
                            str(page.url),
                            str(element.get('context_type') or 'main'),
                            str(element.get('frame_url') or ''),
                            str(element.get('element_key') or ''),
                            self._exploration_label(element)[:120],
                        ])
                        if click_key in clicked_keys:
                            continue
                        if not self._route_candidate_relaxed_match(element, target, next_target):
                            continue
                        label = self._exploration_label(element)
                        if not label or self._looks_dangerous(label, safety_policy):
                            continue
                        if any(keyword and keyword in label for keyword in target.get('blocked_actions') or []):
                            continue
                        probe_score = self._rank_navigation_probe(element, target, safety_policy)
                        if probe_score <= 0:
                            continue
                        if label in root_navigation_labels:
                            probe_score += 30
                        if self._target_element_matches_target(element, next_target):
                            probe_score += 50
                        relaxed_ranked.append((probe_score, click_key, element, False))
                    if relaxed_ranked:
                        ranked = relaxed_ranked

                if not ranked:
                    observations.append({
                        'type': 'business_exploration_target_incomplete',
                        'stage': stage,
                        'reason': '没有剩余的原子目标入口或安全导航入口可探测',
                        'url': page.url,
                    })
                    break
                ranked.sort(key=lambda item: item[0], reverse=True)
                score, click_key, element, direct_match = ranked[0]
                clicked_keys.add(click_key)
                before_url = page.url
                label = self._exploration_label(element)
                if str(element.get('role') or '').lower() in {'menuitem', 'treeitem', 'tab', 'link'}:
                    clicked_navigation_labels.add(label)
                success, error = await self._click_collected_element(page, element)
                exploration_steps += 1
                probes += 1
                if not success:
                    observations.append({
                        'type': 'business_exploration_click_failed',
                        'stage': stage,
                        'target': label,
                        'score': score,
                        'error': error,
                    })
                    continue
                if not self._is_url_allowed(page.url, safety_policy, base_url):
                    observations.append({
                        'type': 'business_exploration_blocked_url',
                        'stage': stage,
                        'target': label,
                        'url': page.url,
                    })
                    await page.goto(before_url, wait_until='networkidle', timeout=self.action_timeout)
                    continue

                await self._wait_for_business_state_stable(page)
                screenshot_path = f"{self.screenshot_dir}/ai_generation_{task_id}_page_{len(pages) + 1}.png"
                state = await self._mcp_collect_page_state(
                    page,
                    f'page_{len(pages) + 1}',
                    screenshot_path,
                    max_elements=220,
                    priority_keywords=[
                        str(value)
                        for values in (
                            target.get('coverage_keywords') if isinstance(target.get('coverage_keywords'), dict) else {}
                        ).values()
                        if isinstance(values, list)
                        for value in values
                    ] if self._target_requires_form_coverage(target) else None,
                )
                form_covered = requires_form and self._is_target_business_form_covered(state, target, safety_policy)
                if form_covered:
                    state = await self._enrich_dynamic_form_context(
                        page,
                        state,
                        observations,
                        target.get('coverage_keywords') if isinstance(target.get('coverage_keywords'), dict) else {},
                    )
                appended = self._append_unique_page_state(pages, seen_signatures, state)
                if appended:
                    state_transitions.append(
                        self._build_state_transition_record(current_state, state, element, stage, score)
                    )
                observations.append({
                    'type': 'business_exploration_click',
                    'stage': stage,
                    'target': label,
                    'direct_match': direct_match,
                    'score': score,
                    'from_url': before_url,
                    'to_url': state.get('url'),
                    'element_count': len(state.get('elements') or []),
                    'captured': appended,
                })
                target_completed = (
                    form_covered if requires_form
                    else (
                        self._route_target_completed(
                            current_state,
                            state,
                            target,
                            next_target,
                            appended,
                            direct_match,
                            self._target_element_matches_target(element, next_target),
                        )
                        if self._is_requirement_route_stage(target)
                        else self._non_form_target_completed(current_state, state, target, appended, direct_match)
                    )
                )
                if target_completed:
                    if stage not in completed_stages:
                        completed_stages.append(stage)
                    if (
                        self._is_requirement_route_stage(target)
                        and self._target_element_matches_target(element, next_target)
                        and isinstance(next_target, dict)
                    ):
                        next_stage = str(next_target.get('stage') or '')
                        if next_stage and next_stage not in completed_stages:
                            completed_stages.append(next_stage)
                    break

        completed_stages, reconciled_stages = self._reconcile_completed_exploration_stages(
            exploration_targets,
            completed_stages,
            state_transitions,
        )
        if reconciled_stages:
            observations.append({
                'type': 'business_exploration_coverage_reconciled',
                'added_stages': reconciled_stages,
                'reason': 'observed_state_transitions_and_route_prefixes',
            })
        missing_stages = [stage for stage in required_stages if stage not in completed_stages]
        observations.append({
            'type': 'business_exploration_coverage',
            'status': 'success' if not missing_stages else 'incomplete',
            'planning_allowed': not missing_stages,
            'required_stages': required_stages,
            'completed_stages': completed_stages,
            'missing_stages': missing_stages,
            'steps_used': exploration_steps,
        })

        return pages, state_transitions

    @staticmethod
    def _locator_expression(locator: dict[str, Any], operation: str = '') -> str:
        locator_type = locator.get('type')
        value = str(locator.get('value') or '').replace("'", "\\'")
        if locator_type == 'test_id':
            return f"page.get_by_test_id('{value}')"
        if locator_type == 'role':
            role = str(locator.get('role') or locator.get('value') or '').replace("'", "\\'")
            name = str(locator.get('name') or '').replace("'", "\\'")
            return f"page.get_by_role('{role}', name='{name}')" if name else f"page.get_by_role('{role}')"
        if locator_type == 'label':
            if operation in {'check', 'uncheck'}:
                return f"page.get_by_label('{value}')"
            if operation in FORM_FIELD_OPERATIONS:
                return (
                    f"page.locator({json.dumps(FORM_FIELD_CONTAINER_SELECTOR, ensure_ascii=False)})"
                    f".filter(has_text={json.dumps(value, ensure_ascii=False)})"
                    f".locator({json.dumps(FORM_FIELD_CONTROL_SELECTOR, ensure_ascii=False)})"
                )
            return f"page.get_by_label('{value}')"
        if locator_type == 'placeholder':
            return f"page.get_by_placeholder('{value}')"
        if locator_type == 'text':
            return f"page.get_by_text('{value}', exact=True)"
        if locator_type == 'id':
            return f"page.locator('#{value}')"
        if locator_type == 'name':
            return f"page.locator('[name=\"{value}\"]')"
        if locator_type == 'css':
            return f"page.locator('{value}')"
        if locator_type == 'xpath':
            return f"page.locator('xpath={value}')"
        return f"page.locator('{value}')"

    @staticmethod
    def _element_match_keys(element: dict[str, Any]) -> set[str]:
        values = [
            element.get('name'),
            element.get('accessible_name'),
            element.get('text'),
            element.get('label'),
            element.get('placeholder'),
            element.get('test_id'),
        ]
        keys = set()
        for value in values:
            normalized = re.sub(r'\s+', ' ', str(value or '')).strip().lower()
            if normalized:
                keys.add(normalized[:120])
        return keys

    @staticmethod
    def _dedupe_locators(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = set()
        result = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            locator_type = candidate.get('type')
            value = candidate.get('value')
            name = candidate.get('name')
            if not locator_type or (value in (None, '') and name in (None, '')):
                continue
            key = (str(locator_type), str(value or ''), str(name or ''))
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
        return result

    def _merge_existing_element_map_context(self, element_map: dict[str, Any], existing_map: dict[str, Any] | None) -> int:
        """把用户选择的已确认元素地图作为 locator 上下文合并到新采集结果。"""
        if not isinstance(existing_map, dict):
            return 0

        existing_index: dict[str, dict[str, Any]] = {}
        for page in existing_map.get('pages') or []:
            if not isinstance(page, dict):
                continue
            for element in page.get('elements') or []:
                if not isinstance(element, dict):
                    continue
                for key in self._element_match_keys(element):
                    existing_index.setdefault(key, element)

        merged_count = 0
        for page in element_map.get('pages') or []:
            if not isinstance(page, dict):
                continue
            for element in page.get('elements') or []:
                if not isinstance(element, dict):
                    continue
                matched = None
                for key in self._element_match_keys(element):
                    matched = existing_index.get(key)
                    if matched:
                        break
                if not matched:
                    continue

                confirmed_candidates = []
                recommended = matched.get('recommended_locator')
                if isinstance(recommended, dict):
                    confirmed_candidates.append({**recommended, 'source': 'confirmed_element_map'})
                for candidate in matched.get('locator_candidates') or []:
                    if isinstance(candidate, dict):
                        confirmed_candidates.append({**candidate, 'source': 'confirmed_element_map'})

                current_candidates = []
                current_recommended = element.get('recommended_locator')
                if isinstance(current_recommended, dict):
                    current_candidates.append({**current_recommended, 'source': 'live_playwright_observation'})
                current_candidates.extend(
                    candidate for candidate in (element.get('locator_candidates') or [])
                    if isinstance(candidate, dict)
                )
                # 当前页面的实时观察是本轮执行的事实；历史确认 locator 只能作为
                # 自愈候选，不能覆盖本轮新 DOM 产生的推荐定位。
                merged = self._dedupe_locators(current_candidates + confirmed_candidates)
                if merged:
                    element['locator_candidates'] = merged
                    element['recommended_locator'] = current_candidates[0] if current_candidates else merged[0]
                    element['context_match'] = {
                        'source': 'confirmed_element_map',
                        'matched_element_key': matched.get('element_key'),
                        'matched_name': matched.get('name'),
                    }
                    merged_count += 1
        return merged_count

    @staticmethod
    def _locator_context(page: Page, step: dict[str, Any]):
        if str(step.get('context_type') or '') != 'frame' and not (step.get('frame_name') or step.get('frame_url')):
            return page
        frame_name = str(step.get('frame_name') or '').strip()
        frame_url = str(step.get('frame_url') or '').strip()
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if frame_name and frame.name == frame_name:
                return frame
            if frame_url and (frame.url == frame_url or frame_url in frame.url or frame.url in frame_url):
                return frame
        return page

    @staticmethod
    def _candidate_locator(page: Any, locator: dict[str, Any], operation: str = ''):
        locator_type = locator.get('type')
        value = str(locator.get('value') or '')
        if locator_type == 'test_id':
            return page.get_by_test_id(value)
        if locator_type == 'role':
            role = str(locator.get('role') or locator.get('value') or '')
            name = locator.get('name')
            return page.get_by_role(role, name=str(name)) if name else page.get_by_role(role)
        if locator_type == 'label':
            if operation in {'check', 'uncheck'}:
                return page.get_by_label(value)
            if operation in FORM_FIELD_OPERATIONS:
                return page.get_by_label(value)
            return page.get_by_label(value)
        if locator_type == 'placeholder':
            return page.get_by_placeholder(value)
        if locator_type == 'text':
            return page.get_by_text(value, exact=True)
        if locator_type == 'id':
            return page.locator(f'#{value}')
        if locator_type == 'name':
            return page.locator(f'[name="{value}"]')
        if locator_type == 'css':
            return page.locator(value)
        if locator_type == 'xpath':
            return page.locator(f'xpath={value}')
        return page.locator(value)

    @staticmethod
    async def _first_visible_locator(locator: Any, timeout: int = 5000, prefer: str = 'first') -> Any:
        """Resolve a locator to the first currently visible match instead of a blind .first."""
        deadline = time.monotonic() + max(timeout, 0) / 1000
        last_error = ''
        last_count = 0
        scanned = 0
        while True:
            try:
                last_count = await locator.count()
                indexes = list(range(min(last_count, 80)))
                if prefer == 'last':
                    indexes.reverse()
                scanned = len(indexes)
                for index in indexes:
                    candidate = locator.nth(index)
                    try:
                        if await candidate.is_visible(timeout=200):
                            return candidate
                    except Exception as exc:
                        last_error = str(exc)[:300]
            except Exception as exc:
                last_error = str(exc)[:300]

            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.15)

        raise TimeoutError(
            f"Timeout waiting for visible locator match: matched={last_count}, "
            f"scanned={scanned}, prefer={prefer}, last_error={last_error}"
        )

    async def _prepare_locator_for_click(self, locator: Any, page: Page | None = None) -> None:
        """Ensure the locator is scrolled into a usable viewport before clicking."""
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        if page is not None:
            try:
                await page.wait_for_timeout(120)
            except Exception:
                pass

    async def _click_with_visibility_fallback(
        self,
        locator: Any,
        *,
        page: Page | None = None,
        timeout: int = 10000,
        force_timeout: int = 5000,
    ) -> None:
        """Click after scroll-into-view, with a force-click fallback."""
        await self._prepare_locator_for_click(locator, page)
        try:
            await locator.click(timeout=timeout)
            return
        except Exception as exc:
            message = str(exc)
            if 'outside of the viewport' not in message and 'element is not visible' not in message:
                raise
        await self._prepare_locator_for_click(locator, page)
        await locator.click(timeout=force_timeout, force=True)

    async def _verify_and_repair_element_map(
        self,
        page: Page,
        element_map: dict[str, Any],
        max_repair_rounds: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """真实校验生成脚本依赖的 locator，失败时用候选 locator 做基础自动修复。"""
        pages = element_map.get('pages') or []
        current_url = page.url
        target_page = {}
        for page_state in pages:
            if isinstance(page_state, dict) and page_state.get('url') == current_url:
                target_page = page_state
                break
        if not target_page:
            target_page = pages[-1] if pages and isinstance(pages[-1], dict) else {}
        elements = [item for item in (target_page.get('elements') or []) if isinstance(item, dict)]
        def target_score(element: dict[str, Any]) -> int:
            score = 0
            if element.get('required'):
                score += 100
            actions = element.get('actions') if isinstance(element.get('actions'), list) else []
            if 'fill' in actions:
                score += 40
            if 'select_option' in actions:
                score += 35
            if 'click' in actions:
                score += 25
            if element.get('in_dialog'):
                score += 20
            if element.get('visible'):
                score += 15
            locator = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {}
            score += int(float(locator.get('confidence') or 0) * 10)
            label = self._element_visible_text(element)
            for keyword in ['保存', '确认', '提交', '登录', '新建', '创建', 'save', 'confirm', 'submit', 'login', 'create', 'add']:
                if keyword in label:
                    score += 5
            return score

        targets = sorted(elements, key=target_score, reverse=True)[:8]
        checks: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        failed_items: list[dict[str, Any]] = []
        required_failed: list[dict[str, Any]] = []

        for element in targets:
            candidates = self._dedupe_locators(
                ([element.get('recommended_locator')] if isinstance(element.get('recommended_locator'), dict) else [])
                + (element.get('locator_candidates') or [])
            )
            if not candidates:
                failed_items.append({
                    'element_key': element.get('element_key'),
                    'name': element.get('name'),
                    'reason': 'no_locator_candidates',
                })
                continue

            verified_locator = None
            errors = []
            # 这里是元素地图的观测验证，不是 locator healing。只验证当前
            # 推荐 locator，并把失败证据交给后续完整规划链处理。
            for round_index, candidate in enumerate(candidates[:1]):
                try:
                    locator = await self._first_visible_locator(
                        self._candidate_locator(page, candidate),
                        timeout=3000 if round_index == 0 else 2000,
                    )
                    verified_locator = candidate
                    break
                except Exception as exc:
                    errors.append({
                        'locator': candidate,
                        'error': str(exc)[:500],
                    })

            if verified_locator:
                checks.append({
                    'element_key': element.get('element_key'),
                    'name': element.get('name'),
                    'status': 'success',
                    'locator': verified_locator,
                })
            else:
                failed_item = {
                    'element_key': element.get('element_key'),
                    'name': element.get('name'),
                    'required': bool(element.get('required')),
                    'reason': 'all_locator_candidates_failed',
                    'errors': errors[:5],
                }
                failed_items.append(failed_item)
                if element.get('required'):
                    required_failed.append(failed_item)

        verification_result = {
            'status': 'failed' if required_failed else 'success',
            'checked_url': current_url,
            'checked_page_key': target_page.get('page_key'),
            'checked_pages': len(pages),
            'checked_elements': len(targets),
            'passed_elements': len(checks),
            'failed_elements': len(failed_items),
            'failed_required_elements': len(required_failed),
            'failed_optional_elements': len(failed_items) - len(required_failed),
            'checks': checks,
            'failed_items': failed_items,
            'trace_path': None,
            'notes': [
                '已在真实 Playwright 页面中校验生成脚本依赖的关键 locator',
                '非关键元素校验失败不再直接判定整轮生成失败',
            ] if not required_failed else ['已在真实 Playwright 页面中校验生成脚本依赖的关键 locator'],
        }
        return verification_result, repairs, failed_items

    @staticmethod
    def _failure_category_from_error(error: str, operation: str = '') -> str:
        lowered = (error or '').lower()
        if any(keyword in lowered for keyword in ['not editable', 'not enabled', 'disabled', 'readonly', '不可编辑', '未启用']):
            return 'state_dependency'
        if any(keyword in lowered for keyword in ['timeout', 'waiting', '超时']):
            return 'timeout'
        if any(keyword in lowered for keyword in ['locator', 'strict mode', 'not found', 'not visible', '定位', '元素']):
            return 'locator'
        if operation.startswith('assert_') or any(keyword in lowered for keyword in ['expect', 'assert', '断言']):
            return 'assertion'
        if any(keyword in lowered for keyword in ['net::', 'navigation', 'connection', '网络']):
            return 'environment'
        return 'unknown'

    @staticmethod
    def _is_mcp_session_loss_error(error: str) -> bool:
        lowered = (error or '').lower()
        return any(keyword in lowered for keyword in [
            'session not found',
            'server not initialized',
            'session 丢失',
            '当前页面状态已失效',
        ])

    @staticmethod
    def _is_dynamic_element_plus_locator(locator: dict[str, Any]) -> bool:
        locator_type = str(locator.get('type') or '')
        value = str(locator.get('value') or '')
        if locator_type == 'id':
            return bool(re.fullmatch(r'el-id-\d+-\d+', value))
        if locator_type == 'css':
            return bool(re.fullmatch(r'#el-id-\d+-\d+', value))
        if locator_type == 'xpath':
            return bool(re.search(r'el-id-\d+-\d+', value))
        return False

    @classmethod
    def _stabilize_observed_locators(cls, state: dict[str, Any]) -> dict[str, Any]:
        """Prefer semantic locators over framework-generated runtime IDs."""
        for element in state.get('elements') or []:
            if not isinstance(element, dict):
                continue
            current = []
            recommended = element.get('recommended_locator')
            if isinstance(recommended, dict):
                current.append(recommended)
            current.extend(item for item in (element.get('locator_candidates') or []) if isinstance(item, dict))
            label = str(element.get('label') or element.get('form_label') or '').strip()
            actions = element.get('actions') or []
            role = str(element.get('role') or '').strip()
            semantic = []
            if label and any(action in actions for action in FORM_FIELD_OPERATIONS):
                if role in {'combobox', 'select'}:
                    semantic.append({'type': 'role', 'value': role, 'name': label, 'confidence': 0.88})
                else:
                    semantic.append({'type': 'label', 'value': label, 'confidence': 0.9})
            candidates = cls._dedupe_locators(semantic + current)
            if not candidates:
                continue
            stable = [item for item in candidates if not cls._is_dynamic_element_plus_locator(item)]
            dynamic = [item for item in candidates if cls._is_dynamic_element_plus_locator(item)]
            if stable:
                element['recommended_locator'] = stable[0]
                element['locator_candidates'] = stable + dynamic
            else:
                element['recommended_locator'] = dynamic[0]
                element['locator_candidates'] = dynamic
        return state

    def _plan_step_locator(self, step: dict[str, Any]) -> dict[str, Any]:
        locator = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
        operation = self._normalise_ai_plan_operation(step)
        target_name = self._clean_visible_text(step.get('target_name') or '')
        if (
            locator.get('type')
            and (
                operation in FORM_FIELD_OPERATIONS
                or operation in {'assert_visible', 'assert_text', 'assert_contain_text', 'assert_value'}
            )
            and target_name
            and self._is_dynamic_element_plus_locator(locator)
        ):
            return {'type': 'label', 'value': target_name, 'confidence': 0.9}
        if locator.get('type'):
            return locator
        return step.get('recommended_locator') if isinstance(step.get('recommended_locator'), dict) else {}

    def _locator_priority_for_operation(self, candidate: dict[str, Any], operation: str) -> tuple[int, int]:
        dynamic_penalty = 100 if self._is_dynamic_element_plus_locator(candidate) else 0
        if operation in FORM_FIELD_OPERATIONS:
            return dynamic_penalty + self._locator_fill_priority(candidate), 0
        return dynamic_penalty + self._locator_click_priority(candidate), 0

    @staticmethod
    def _normalise_ai_plan_operation(step: dict[str, Any]) -> str:
        operation = str(step.get('operation') or step.get('action') or '').strip().lower()
        aliases = {
            'navigate': 'goto',
            'navigation': 'goto',
            'open': 'goto',
            'open_url': 'goto',
            'page_goto': 'goto',
            'sleep': 'wait',
            'observe_page': 'observe',
            'snapshot': 'observe',
            'assert': 'assert_page',
            'assert_page_loaded': 'assert_page',
        }
        return aliases.get(operation, operation)

    @staticmethod
    def _find_mapped_element(element_map: dict[str, Any], page_key: str, element_key: str) -> dict[str, Any] | None:
        if not element_key:
            return None
        fallback = None
        for page_state in element_map.get('pages') or []:
            if not isinstance(page_state, dict):
                continue
            for element in page_state.get('elements') or []:
                if not isinstance(element, dict) or str(element.get('element_key') or '') != element_key:
                    continue
                if page_key and str(page_state.get('page_key') or '') == page_key:
                    return element
                fallback = fallback or element
        return fallback

    @staticmethod
    def _locator_stability_score(locator: dict[str, Any]) -> float:
        locator_type = locator.get('type')
        confidence = float(locator.get('confidence') or 0)
        type_weight = {
            'test_id': 1.0,
            'label': 0.88,
            'placeholder': 0.82,
            'role': 0.78,
            'id': 0.72,
            'name': 0.68,
            'text': 0.58,
            'css': 0.42,
            'xpath': 0.32,
        }.get(locator_type, 0.3)
        return round(min(1.0, max(0.0, (confidence * 0.65) + (type_weight * 0.35))), 3)

    def _build_locator_baseline(self, element_map: dict[str, Any]) -> list[dict[str, Any]]:
        baseline = []
        for page_state in element_map.get('pages') or []:
            if not isinstance(page_state, dict):
                continue
            page_key = str(page_state.get('page_key') or '')
            for element in page_state.get('elements') or []:
                if not isinstance(element, dict):
                    continue
                recommended = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {}
                candidates = [
                    item for item in (element.get('locator_candidates') or [])
                    if isinstance(item, dict)
                ]
                if not recommended and not candidates:
                    continue
                baseline.append({
                    'page_key': page_key,
                    'element_key': element.get('element_key'),
                    'name': element.get('name') or element.get('accessible_name') or '',
                    'tag': element.get('tag') or '',
                    'recommended_locator': recommended,
                    'candidate_count': len(candidates),
                    'stability_score': self._locator_stability_score(recommended) if recommended else 0,
                    'signature': hashlib.sha256(json.dumps({
                        'page_key': page_key,
                        'element_key': element.get('element_key'),
                        'name': element.get('name') or element.get('accessible_name') or '',
                        'tag': element.get('tag') or '',
                        'locator': recommended,
                    }, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest(),
                })
        return baseline

    @staticmethod
    def _build_healing_review_queue(step_results: list[dict[str, Any]], repairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        queue = []
        for repair in repairs:
            if not isinstance(repair, dict) or repair.get('type') != 'ai_plan_step_locator_patch':
                continue
            queue.append({
                'type': 'locator_patch_review',
                'status': 'pending',
                'step_sort': repair.get('step_sort'),
                'page_key': repair.get('page_key') or '',
                'element_key': repair.get('element_key') or '',
                'target_name': repair.get('target_name') or '',
                'old_locator': repair.get('old_locator') or {},
                'new_locator': repair.get('new_locator') or {},
                'reason': '自动修复成功，建议人工确认后固化到元素地图基线',
            })
        for step in step_results:
            if not isinstance(step, dict) or step.get('status') != 'failed':
                continue
            queue.append({
                'type': 'failed_step_review',
                'status': 'pending',
                'step_sort': step.get('step_sort'),
                'page_key': step.get('page_key') or '',
                'element_key': step.get('element_key') or '',
                'target_name': step.get('target_name') or '',
                'failure_category': step.get('failure_category') or '',
                'message': step.get('message') or '',
                'reason': '自动修复失败，需要人工确认元素或业务流程',
            })
        return queue

    async def _wait_for_ui_interaction_ready(self, page: Page, timeout: int = 12000) -> bool:
        """等待常见前端 loading 遮罩消失，避免点击被过渡动画层拦截。"""
        try:
            await page.wait_for_function(
                """
                () => {
                  const selectors = [
                    '.el-loading-mask',
                    '.v-loading-mask',
                    '.arco-spin-mask',
                    '.ant-spin-blur',
                    '.ant-spin-spinning',
                    '.n-spin-body',
                    '[class*="loading-mask"]',
                    '[class*="LoadingMask"]'
                  ];
                  const masks = Array.from(document.querySelectorAll(selectors.join(',')));
                  return !masks.some((el) => {
                    const style = window.getComputedStyle(el);
                    if (
                      style.display === 'none' ||
                      style.visibility === 'hidden' ||
                      style.pointerEvents === 'none' ||
                      Number(style.opacity || '1') <= 0.01
                    ) {
                      return false;
                    }
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 1 || rect.height <= 1) return false;
                    return true;
                  });
                }
                """,
                timeout=timeout,
            )
            return True
        except Exception:
            return False

    async def _select_custom_combobox_option(
        self,
        page: Page,
        step: dict[str, Any],
        locator_context: Any,
        locator_hint: dict[str, Any],
        value: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        label = self._clean_visible_text(
            step.get('target_name') or locator_hint.get('value') or locator_hint.get('name') or ''
        )
        if not label or not value:
            return False, None

        container = await self._first_visible_locator(
            locator_context.locator(FORM_FIELD_CONTAINER_SELECTOR).filter(has_text=label),
            timeout=5000,
        )
        trigger = await self._first_visible_locator(
            container.locator(
                '.el-select__wrapper, .el-select, .el-input__wrapper, '
                '.arco-select-view, .ant-select-selector, [role="combobox"]'
            ),
            timeout=5000,
        )
        try:
            await self._wait_for_ui_interaction_ready(page)
            try:
                await self._click_with_visibility_fallback(trigger, page=page, timeout=10000, force_timeout=10000)
            except Exception:
                await self._click_with_visibility_fallback(trigger, page=page, timeout=10000, force_timeout=10000)

            option_locators = [
                locator_context.get_by_role('option', name=value),
                locator_context.locator('.el-select-dropdown__item, .arco-select-option, .ant-select-item-option').filter(has_text=value),
                page.get_by_role('option', name=value),
                page.locator('.el-select-dropdown__item, .arco-select-option, .ant-select-item-option').filter(has_text=value),
                page.get_by_text(value, exact=True),
            ]
            for option in option_locators:
                try:
                    target = await self._first_visible_locator(option, timeout=1500, prefer='last')
                    await target.click(timeout=10000)
                    await self._wait_for_ui_interaction_ready(page)
                    return True, {'type': 'label', 'value': label, 'option': value, 'strategy': 'custom_combobox'}
                except Exception:
                    continue
            fallback_option_locators = [
                page.locator(
                    '.el-select-dropdown__item:not(.is-disabled), '
                    '.arco-select-option:not(.arco-select-option-disabled), '
                    '.ant-select-item-option:not(.ant-select-item-option-disabled), '
                    '[role="option"]:not([aria-disabled="true"])'
                ),
                locator_context.get_by_role('option'),
                page.get_by_role('option'),
            ]
            for option in fallback_option_locators:
                try:
                    target = await self._first_visible_locator(option, timeout=1200)
                    await target.click(timeout=10000)
                    await self._wait_for_ui_interaction_ready(page)
                    return True, {'type': 'label', 'value': label, 'option': value, 'strategy': 'custom_combobox_first_available'}
                except Exception:
                    continue
            await page.keyboard.press('ArrowDown')
            await page.keyboard.press('Enter')
            await self._wait_for_ui_interaction_ready(page)
            return True, {'type': 'label', 'value': label, 'option': value, 'strategy': 'custom_combobox_keyboard_first_available'}
        except Exception:
            return False, None
        return False, None

    async def _try_enable_disabled_form_target(
        self,
        page: Page,
        step: dict[str, Any],
        element_map: dict[str, Any] | None,
        locator_context: Any,
        target_locator: Any,
        safety_policy: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | None]:
        """基于元素地图上下文尝试解锁 disabled 表单字段，不写死具体业务字段。"""
        if not isinstance(element_map, dict):
            return False, None

        target = self._find_mapped_element(
            element_map,
            str(step.get('page_key') or ''),
            str(step.get('element_key') or ''),
        )
        if not target:
            return False, {'type': 'context_dependency', 'reason': 'target_element_not_found'}

        target_form_label = self._clean_visible_text(target.get('form_label') or target.get('label') or '')
        target_text = self._element_visible_text(target)
        page_key = str(step.get('page_key') or target.get('page_key') or '')
        page_state = next((
            item for item in element_map.get('pages') or []
            if isinstance(item, dict) and (not page_key or str(item.get('page_key') or '') == page_key)
        ), None)
        if not page_state:
            return False, {
                'type': 'context_dependency',
                'reason': 'target_page_state_not_found',
                'page_key': page_key,
                'target_page_key': target.get('page_key') or '',
            }

        target_locator_hint = target.get('recommended_locator') if isinstance(target.get('recommended_locator'), dict) else {}
        if not target_locator_hint:
            target_locator_hint = _locator_hint(target)
        resolved_target_locator = None
        if target_locator_hint.get('type'):
            try:
                resolved_target_locator = await self._first_visible_locator(
                    self._candidate_locator(
                        locator_context,
                        target_locator_hint,
                        str(step.get('operation') or step.get('action') or ''),
                    ),
                    timeout=3000,
                )
            except Exception:
                resolved_target_locator = None
        if resolved_target_locator is None and target_locator is not None:
            resolved_target_locator = target_locator

        candidates: list[tuple[int, dict[str, Any]]] = []
        for element in page_state.get('elements') or []:
            if not isinstance(element, dict):
                continue
            if str(element.get('element_key') or '') == str(target.get('element_key') or ''):
                continue
            actions = element.get('actions') if isinstance(element.get('actions'), list) else []
            if not any(action in actions for action in ['check', 'click', 'select_option']):
                continue
            if not element.get('enabled', True):
                continue
            label = self._element_visible_text(element)
            if not label or self._looks_dangerous(label, safety_policy):
                continue
            element_form_label = self._clean_visible_text(element.get('form_label') or element.get('label') or '')
            score = 0
            if target_form_label and element_form_label == target_form_label:
                score += 120
            if target_form_label and target_form_label in label:
                score += 70
            if element.get('dialog_name') and element.get('dialog_name') == target.get('dialog_name'):
                score += 20
            if element.get('in_dialog') and target.get('in_dialog'):
                score += 10
            if 'check' in actions:
                score += 50
            if 'select_option' in actions:
                score += 30
            if 'click' in actions:
                score += 10
            normalized_label = self._clean_visible_text(element.get('name') or element.get('label') or label)
            if normalized_label.startswith(('不', '无', '否', '禁用', '关闭')):
                score -= 30
            if score > 0:
                candidates.append((score, element))

        candidates.sort(key=lambda item: item[0], reverse=True)
        attempts: list[dict[str, Any]] = []
        for _, candidate in candidates[:8]:
            actions = candidate.get('actions') if isinstance(candidate.get('actions'), list) else []
            operation = 'check' if 'check' in actions else ('select_option' if 'select_option' in actions else 'click')
            label = self._element_visible_text(candidate)
            target_name = (
                candidate.get('name')
                or candidate.get('label')
                or candidate.get('accessible_name')
                or label
            )
            normalized_target_name = self._clean_visible_text(target_name)
            normalized_label = self._clean_visible_text(candidate.get('label') or '')
            role = str(candidate.get('role') or '').strip().lower()
            locator_hints: list[dict[str, Any]] = []
            if role in {'radio', 'checkbox', 'button', 'switch'} and normalized_target_name:
                locator_hints.append({
                    'type': 'role',
                    'value': role,
                    'name': normalized_target_name,
                    'confidence': 0.92,
                    'source': 'state_dependency_accessible_name',
                })
            if normalized_label:
                locator_hints.append({
                    'type': 'label',
                    'value': normalized_label,
                    'confidence': 0.9,
                    'source': 'state_dependency_label',
                })
            if normalized_target_name:
                locator_hints.append({
                    'type': 'label',
                    'value': normalized_target_name,
                    'confidence': 0.82,
                    'source': 'state_dependency_visible_name',
                })
            if role in {'radio', 'checkbox', 'button', 'switch'} and normalized_target_name:
                locator_hints.append({
                    'type': 'text',
                    'value': normalized_target_name,
                    'confidence': 0.78,
                    'source': 'state_dependency_visible_text',
                })
            locator_hints.extend(
                item for item in (candidate.get('locator_candidates') or [])
                if isinstance(item, dict)
            )
            if isinstance(candidate.get('recommended_locator'), dict):
                locator_hints.append(candidate['recommended_locator'])
            locator_hints = self._dedupe_locators(locator_hints)
            candidate_step = {
                'operation': operation,
                'page_key': page_key,
                'element_key': candidate.get('element_key') or '',
                'target_name': target_name,
                'locator_hint': locator_hints[0] if locator_hints else {},
                'value': self._default_script_option_for_element(candidate) if operation == 'select_option' else '',
            }
            locator_errors = []
            locator_resolved = False
            for locator_hint in locator_hints[:8]:
                try:
                    candidate_locator = await self._first_visible_locator(
                        self._candidate_locator(locator_context, locator_hint, operation),
                        timeout=2500,
                    )
                    locator_resolved = True
                except Exception as locator_exc:
                    locator_errors.append({
                        'locator': locator_hint,
                        'error': str(locator_exc)[:300],
                    })
                    continue
                try:
                    if operation == 'check':
                        try:
                            await candidate_locator.check(timeout=5000)
                        except Exception:
                            await candidate_locator.click(timeout=5000, force=True)
                    elif operation == 'select_option':
                        candidate_step['locator_hint'] = locator_hint
                        selected, selected_locator = await self._select_custom_combobox_option(
                            page,
                            candidate_step,
                            locator_context,
                            locator_hint,
                            str(candidate_step.get('value') or ''),
                        )
                        if not selected:
                            await candidate_locator.click(timeout=5000)
                            await page.keyboard.press('ArrowDown')
                            await page.keyboard.press('Enter')
                    else:
                        await candidate_locator.click(timeout=5000)
                    await self._wait_for_ui_interaction_ready(page)
                    for _ in range(6):
                        try:
                            if resolved_target_locator is not None and await resolved_target_locator.is_enabled(timeout=500):
                                return True, {
                                    'type': 'context_dependency',
                                    'element_key': candidate.get('element_key') or '',
                                    'target_name': candidate_step['target_name'],
                                    'operation': operation,
                                    'locator_hint': locator_hint or {},
                                    'reason': '同一表单上下文控件使目标字段可编辑',
                                }
                        except Exception:
                            pass
                        await page.wait_for_timeout(250)
                    attempts.append({
                        'element_key': candidate.get('element_key') or '',
                        'target_name': candidate_step['target_name'],
                        'operation': operation,
                        'status': 'target_still_disabled',
                        'locator_hint': locator_hint or {},
                    })
                except Exception as action_exc:
                    attempts.append({
                        'element_key': candidate.get('element_key') or '',
                        'target_name': candidate_step['target_name'],
                        'operation': operation,
                        'status': 'action_failed',
                        'locator_hint': locator_hint or {},
                        'error': str(action_exc)[:300],
                    })
                    continue
            if not locator_resolved:
                attempts.append({
                    'element_key': candidate.get('element_key') or '',
                    'target_name': candidate_step['target_name'],
                    'operation': operation,
                    'status': 'locator_not_visible',
                    'locator_errors': locator_errors[:3],
                })
        return False, {
            'type': 'context_dependency',
            'reason': 'no_context_candidate_unlocked_target',
            'target_element_key': target.get('element_key') or '',
            'target_name': target.get('name') or step.get('target_name') or '',
            'candidate_count': len(candidates),
            'attempts': attempts[:8],
        }

    async def _execute_ai_plan_step(
        self,
        page: Page,
        step: dict[str, Any],
        base_url: str,
        safety_policy: dict[str, Any],
        element_map: dict[str, Any] | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        operation = self._normalise_ai_plan_operation(step)
        value = str(step.get('value') or '')
        description = str(step.get('description') or step.get('target_name') or operation or '')

        if self._ai_plan_step_requires_confirmation(step, safety_policy):
            return False, '步骤需要人工确认，自动验证未执行', None

        if operation == 'goto':
            url = value or base_url
            if url.startswith('/') and base_url:
                parsed_base = urlparse(base_url)
                url = f"{parsed_base.scheme}://{parsed_base.netloc}{url}"
            if not self._is_url_allowed(url, safety_policy, base_url):
                return False, f'跳转 URL 不在安全边界允许范围内: {url}', None
            await self._navigate_for_observation(page, url)
            await self._wait_for_ui_interaction_ready(page)
            return True, '页面跳转成功', None

        if operation == 'wait':
            try:
                wait_ms = int(float(value or 1) * 1000)
            except (TypeError, ValueError):
                wait_ms = 1000
            await page.wait_for_timeout(max(0, min(wait_ms, 30000)))
            return True, '等待完成', None

        if operation == 'observe':
            try:
                await page.wait_for_load_state('networkidle', timeout=min(self.action_timeout, 10000))
            except Exception:
                await page.wait_for_timeout(500)
            return True, '页面状态采集点已通过', None

        if operation == 'assert_page':
            await self._first_visible_locator(page.locator('body'), timeout=5000)
            return True, '页面基础可见性验证通过', None

        locator_hint = self._plan_step_locator(step)
        if not locator_hint:
            return False, f'步骤缺少 locator_hint: {description}', None

        await self._wait_for_ui_interaction_ready(page)
        locator_context = self._locator_context(page, step)
        mapped_element = None
        if isinstance(element_map, dict):
            mapped_element = self._find_mapped_element(
                element_map,
                str(step.get('page_key') or ''),
                str(step.get('element_key') or ''),
            )
        if operation == 'fill' and mapped_element and self._looks_like_select_control(mapped_element):
            if value and value not in {'测试数据', '自动化测试数据'}:
                try:
                    locator = await self._first_visible_locator(
                        self._candidate_locator(locator_context, locator_hint, 'select_option'),
                        timeout=5000,
                    )
                    try:
                        await locator.select_option(value)
                    except Exception:
                        await self._click_with_visibility_fallback(locator, page=page, timeout=10000, force_timeout=10000)
                        await page.keyboard.press('ArrowDown')
                        await page.keyboard.press('Enter')
                    await self._wait_for_ui_interaction_ready(page)
                    return True, '步骤执行成功: select_option', locator_hint
                except Exception:
                    pass
            operation = 'select_option'
        if operation == 'select_option':
            selected, selected_locator = await self._select_custom_combobox_option(
                page,
                step,
                locator_context,
                locator_hint,
                value,
            )
            if selected:
                return True, f'步骤执行成功: {operation}', selected_locator

        locator = await self._first_visible_locator(
            self._candidate_locator(locator_context, locator_hint, operation),
            timeout=5000,
        )

        if operation == 'fill':
            try:
                await locator.fill(value)
            except Exception as exc:
                if 'not enabled' in str(exc) or 'disabled' in str(exc):
                    raise RuntimeError('目标字段当前不可编辑，需要 LLM 根据当前页面状态补充前置依赖步骤') from exc
                raise
        elif operation == 'click':
            await self._click_with_visibility_fallback(locator, page=page, timeout=5000, force_timeout=5000)
            try:
                await page.wait_for_load_state('networkidle', timeout=6000)
            except Exception:
                await page.wait_for_timeout(800)
            await self._wait_for_ui_interaction_ready(page)
        elif operation == 'select_option':
            try:
                if value:
                    await locator.select_option(value)
                else:
                    raise ValueError('没有可直接 select_option 的 value，改用下拉键盘选择')
            except Exception:
                await self._wait_for_ui_interaction_ready(page)
                try:
                    await self._click_with_visibility_fallback(locator, page=page, timeout=10000, force_timeout=10000)
                except Exception as click_exc:
                    if 'intercepts pointer events' in str(click_exc) or 'el-loading-mask' in str(click_exc):
                        await self._wait_for_ui_interaction_ready(page, timeout=20000)
                        await self._click_with_visibility_fallback(locator, page=page, timeout=10000, force_timeout=10000)
                    else:
                        raise
                await page.keyboard.press('ArrowDown')
                await page.keyboard.press('Enter')
            await self._wait_for_ui_interaction_ready(page)
        elif operation == 'check':
            await locator.check()
        elif operation == 'uncheck':
            await locator.uncheck()
        elif operation == 'assert_visible':
            await expect(locator).to_be_visible(timeout=5000)
        elif operation == 'assert_text':
            await expect(locator).to_have_text(value, timeout=5000)
        elif operation == 'assert_contain_text':
            await expect(locator).to_contain_text(value, timeout=5000)
        elif operation == 'assert_value':
            await expect(locator).to_have_value(value, timeout=5000)
        else:
            return False, f'未知或暂不支持的 AI 生成步骤操作: {operation}', None

        return True, f'步骤执行成功: {operation}', locator_hint

    async def _repair_and_execute_ai_plan_step(
        self,
        page: Page,
        step: dict[str, Any],
        element_map: dict[str, Any],
        base_url: str,
        safety_policy: dict[str, Any],
        max_repair_rounds: int,
    ) -> tuple[bool, str, dict[str, Any] | None, list[dict[str, Any]]]:
        page_key = str(step.get('page_key') or '')
        element_key = str(step.get('element_key') or '')
        element = self._find_mapped_element(element_map, page_key, element_key)
        repairs = []
        if not element or max_repair_rounds <= 0:
            return False, '未找到可用于自动修复的元素地图候选 locator', None, repairs

        original_locator = self._plan_step_locator(step)
        operation = self._normalise_ai_plan_operation(step)
        locator_context = self._locator_context(page, step)
        target_locator = None
        try:
            target_locator = await self._first_visible_locator(
                self._candidate_locator(locator_context, original_locator, operation),
                timeout=3000,
            )
        except Exception:
            target_locator = None

        if operation in FORM_FIELD_OPERATIONS | {'click'}:
            try:
                if target_locator is not None:
                    state_dependency_repaired, repair_context = await self._try_enable_disabled_form_target(
                        page,
                        step,
                        element_map,
                        locator_context,
                        target_locator,
                        safety_policy,
                    )
                else:
                    state_dependency_repaired, repair_context = False, None
                if state_dependency_repaired:
                    try:
                        success, message, locator_used = await self._execute_ai_plan_step(
                            page,
                            step,
                            base_url,
                            safety_policy,
                            element_map,
                        )
                        if success:
                            repairs.append({
                                'round': len(repairs) + 1,
                                'type': 'ai_plan_step_state_dependency_repair',
                                'page_key': step.get('page_key') or '',
                                'element_key': step.get('element_key') or '',
                                'step_sort': step.get('step_sort'),
                                'target_name': step.get('target_name') or element.get('name') or '',
                                'old_locator': original_locator,
                                'new_locator': self._plan_step_locator(step),
                                'repair_context': repair_context or {},
                                'message': '目标字段在当前状态下不可编辑，已依据同表单上下文控件尝试解锁并重试成功',
                            })
                            return True, message, locator_used, repairs
                    except Exception as retry_exc:
                        repairs.append({
                            'round': len(repairs) + 1,
                            'type': 'ai_plan_step_state_dependency_repair_failed',
                            'page_key': step.get('page_key') or '',
                            'element_key': step.get('element_key') or '',
                            'step_sort': step.get('step_sort'),
                            'target_name': step.get('target_name') or element.get('name') or '',
                            'old_locator': original_locator,
                            'repair_context': repair_context or {},
                            'error': str(retry_exc)[:500],
                        })
                elif repair_context:
                    repairs.append({
                        'round': len(repairs) + 1,
                        'type': 'ai_plan_step_state_dependency_repair_failed',
                        'page_key': step.get('page_key') or '',
                        'element_key': step.get('element_key') or '',
                        'step_sort': step.get('step_sort'),
                        'target_name': step.get('target_name') or element.get('name') or '',
                        'old_locator': original_locator,
                        'repair_context': repair_context or {},
                        'error': repair_context.get('reason') or 'context_dependency_repair_failed',
                    })
            except Exception:
                pass

        candidates = self._dedupe_locators(
            ([element.get('recommended_locator')] if isinstance(element.get('recommended_locator'), dict) else [])
            + (element.get('locator_candidates') or [])
        )
        operation = self._normalise_ai_plan_operation(step)
        candidates.sort(key=lambda item: self._locator_priority_for_operation(item, operation))
        for candidate in candidates:
            if candidate == original_locator:
                continue
            patched_step = {**step, 'locator_hint': candidate}
            try:
                success, message, locator_used = await self._execute_ai_plan_step(
                    page,
                    patched_step,
                    base_url,
                    safety_policy,
                    element_map,
                )
                if success:
                    step['locator_hint'] = candidate
                    repairs.append({
                        'round': len(repairs) + 1,
                        'type': 'ai_plan_step_locator_patch',
                        'page_key': page_key,
                        'element_key': element_key,
                        'step_sort': step.get('step_sort'),
                        'target_name': step.get('target_name') or element.get('name') or '',
                        'old_locator': original_locator,
                        'new_locator': candidate,
                        'message': '生成步骤执行失败后，已使用元素地图备用 locator 修复并重试成功',
                    })
                    return True, message, locator_used, repairs
            except Exception as exc:
                repairs.append({
                    'round': len(repairs) + 1,
                    'type': 'ai_plan_step_locator_patch_failed',
                    'page_key': page_key,
                    'element_key': element_key,
                    'step_sort': step.get('step_sort'),
                    'candidate_locator': candidate,
                    'error': str(exc)[:500],
                })
            if len([item for item in repairs if item.get('type') == 'ai_plan_step_locator_patch_failed']) >= max_repair_rounds:
                break
        return False, '所有备用 locator 修复尝试均失败', None, repairs

    async def _ensure_authenticated_before_ai_flow(
        self,
        page: Page,
        args: dict,
        base_url: str,
        safety_policy: dict[str, Any],
    ) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        credentials = self._extract_login_credentials(args)
        if not credentials.get('username') or not credentials.get('password') or not base_url:
            return observations
        if not self._is_url_allowed(base_url, safety_policy, base_url):
            observations.append({
                'type': 'flow_auth_setup_skipped',
                'reason': f'登录入口不在安全边界内: {base_url}',
            })
            return observations
        try:
            await page.goto(base_url, wait_until='networkidle', timeout=self.action_timeout)
            first_state = await self._mcp_collect_page_state(
                page,
                'flow_auth_setup',
                f"{self.screenshot_dir}/ai_generation_auth_setup_{int(time.time() * 1000)}.png",
                max_elements=80,
            )
            authentication_mode = self._authentication_mode(args, first_state)
            observations.append({
                'type': 'flow_auth_setup_strategy',
                'mode': authentication_mode,
                'entry_url': page.url,
            })
            if authentication_mode == 'unknown' and self._looks_like_login_page(first_state):
                observations.append({
                    'type': 'flow_auth_setup',
                    'status': 'failed',
                    'reason': self._authentication_contract_unavailable_reason(args),
                    'url': page.url,
                })
                return observations
            if authentication_mode == 'test_subject':
                observations.append({
                    'type': 'flow_auth_setup',
                    'status': 'skipped_authentication_is_test_subject',
                    'url': page.url,
                })
                return observations
            if self._looks_like_login_page(first_state):
                await self._auto_login_if_possible(page, first_state, credentials, safety_policy, observations)
            else:
                observations.append({
                    'type': 'flow_auth_setup',
                    'status': 'already_authenticated_or_public',
                    'url': page.url,
                })
        except Exception as exc:
            observations.append({
                'type': 'flow_auth_setup',
                'status': 'failed',
                'error': str(exc)[:800],
            })
        return observations

    def _compact_runtime_state_for_llm(self, state: dict[str, Any]) -> dict[str, Any]:
        elements = []
        for element in (state.get('elements') or [])[:120]:
            if not isinstance(element, dict):
                continue
            elements.append({
                'page_key': state.get('page_key') or '',
                'element_key': element.get('element_key') or '',
                'name': element.get('name') or element.get('accessible_name') or element.get('label') or '',
                'role': element.get('role') or '',
                'tag': element.get('tag') or '',
                'actions': element.get('actions') or [],
                'enabled': element.get('enabled'),
                'visible': element.get('visible'),
                'required': element.get('required'),
                'in_dialog': element.get('in_dialog'),
                'dialog_name': element.get('dialog_name') or '',
                'form_label': element.get('form_label') or '',
                'placeholder': element.get('placeholder') or '',
                'options': (element.get('dynamic_options') or element.get('options') or [])[:10],
                'recommended_locator': element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {},
            })
        return {
            'page_key': state.get('page_key') or '',
            'url': state.get('url') or '',
            'title': state.get('title') or '',
            'element_count': len(state.get('elements') or []),
            'elements': elements,
            'network_summary': state.get('network_summary') or {},
            'console_messages': (state.get('console_messages') or [])[-10:],
        }

    async def _capture_ai_flow_failure_context(
        self,
        page: Page,
        task_id: Any,
        step_index: int,
        step: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        try:
            screenshot_path = f"{self.screenshot_dir}/ai_generation_{task_id}_failure_state_{step_index + 1}.png"
            state = await self._mcp_collect_page_state(
                page,
                f"failure_step_{step_index + 1}",
                screenshot_path,
                max_elements=160,
            )
            record['current_url'] = state.get('url') or page.url
            record['runtime_state'] = self._compact_runtime_state_for_llm(state)
            record['runtime_state']['failed_step'] = {
                'operation': self._normalise_ai_plan_operation(step),
                'page_key': step.get('page_key') or '',
                'element_key': step.get('element_key') or '',
                'target_name': step.get('target_name') or '',
                'description': step.get('description') or '',
            }
        except Exception as exc:
            record['runtime_state_error'] = str(exc)[:500]

    async def verify_ai_generated_flow(self, args: dict, result: dict) -> dict:
        """真实执行 LLM 规划后的步骤，形成运行、失败分类、locator patch 和 trace 证据闭环。"""
        if False and self.mcp_provider == 'official-playwright-mcp':
            return await self._verify_ai_generated_flow_with_official_mcp(args, result)

        generated_case = result.get('generated_case') if isinstance(result.get('generated_case'), dict) else {}
        steps = generated_case.get('steps') or result.get('test_plan', {}).get('steps') or []
        if not isinstance(steps, list) or not steps:
            return result

        task_id = args.get('task_id') or result.get('task_id')
        env_config = args.get('environment_config') or {}
        safety_policy = args.get('safety_policy') or {}
        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        base_url = (
            element_map.get('base_url')
            or args.get('target_url')
            or env_config.get('base_url')
            or ''
        )
        patched_steps = self._ensure_state_transition_steps_for_plan(args, element_map, steps)
        if patched_steps is not steps:
            steps = patched_steps
            generated_case['steps'] = steps
            if isinstance(result.get('test_plan'), dict):
                result['test_plan']['steps'] = steps
        max_repair_rounds = int(args.get('max_repair_rounds') or safety_policy.get('max_repair_rounds') or 2)
        step_results: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        failed_category = ''
        started = time.time()

        try:
            async with self.browser_session_with_trace(
                f"ai_generation_flow_verify_{task_id}",
                ignore_https_errors=self._ignore_https_errors_for_env(env_config),
            ) as page:
                self._setup_page_listeners(page)
                auth_observations = await self._ensure_authenticated_before_ai_flow(
                    page,
                    args,
                    base_url,
                    safety_policy,
                )
                if base_url and not any(
                    isinstance(step, dict) and self._normalise_ai_plan_operation(step) == 'goto'
                    for step in steps
                ):
                    await page.goto(base_url, wait_until='networkidle', timeout=self.action_timeout)

                for index, step in enumerate(steps):
                    if not isinstance(step, dict):
                        continue
                    step_started = time.time()
                    operation = self._normalise_ai_plan_operation(step)
                    record = {
                        'step_sort': step.get('step_sort', index),
                        'operation': operation,
                        'description': step.get('description') or '',
                        'page_key': step.get('page_key') or '',
                        'element_key': step.get('element_key') or '',
                        'target_name': step.get('target_name') or '',
                        'status': 'success',
                        'message': '',
                        'duration': 0,
                    }
                    try:
                        success, message, locator_used = await self._execute_ai_plan_step(
                            page,
                            step,
                            base_url,
                            safety_policy,
                            element_map,
                        )
                        if not success:
                            raise RuntimeError(message)
                        record['message'] = message
                        if locator_used:
                            record['locator'] = locator_used
                    except Exception as exc:
                        category = self._failure_category_from_error(str(exc), operation)
                        repaired = False
                        repair_message = ''
                        locator_used = None
                        if category in {'locator', 'timeout', 'state_dependency'} and operation not in {'goto', 'wait'}:
                            repaired, repair_message, locator_used, step_repairs = await self._repair_and_execute_ai_plan_step(
                                page,
                                step,
                                element_map,
                                base_url,
                                safety_policy,
                                max_repair_rounds,
                            )
                            repairs.extend(step_repairs)
                        if repaired:
                            record['status'] = 'repaired'
                            record['message'] = repair_message
                            record['failure_category_before_repair'] = category
                            if locator_used:
                                record['locator'] = locator_used
                        else:
                            screenshot_path = f"{self.screenshot_dir}/ai_generation_{task_id}_verify_step_{index + 1}.png"
                            try:
                                await page.screenshot(path=screenshot_path, full_page=True, timeout=10000)
                                record['screenshot'] = screenshot_path
                            except Exception as screenshot_exc:
                                logger.warning(f"AI 生成步骤失败截图失败: {screenshot_exc}")
                            record['status'] = 'failed'
                            record['message'] = str(exc)[:1000]
                            record['failure_category'] = category
                            await self._capture_ai_flow_failure_context(page, task_id, index, step, record)
                            failed_category = failed_category or category
                            step_results.append(record)
                            break
                    finally:
                        record['duration'] = time.time() - step_started
                    step_results.append(record)

            trace_path = self.get_current_trace_path()
            verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
            failed_steps = [item for item in step_results if item.get('status') == 'failed']
            repaired_steps = [item for item in step_results if item.get('status') == 'repaired']
            verification['flow_execution'] = {
                'status': 'success' if not failed_steps else 'failed',
                'total_steps': len(step_results),
                'passed_steps': len([item for item in step_results if item.get('status') in {'success', 'repaired'}]),
                'failed_steps': len(failed_steps),
                'repaired_steps': len(repaired_steps),
                'duration': time.time() - started,
                'steps': step_results,
                'auth_setup': auth_observations,
                'trace_path': trace_path,
                'page_errors': getattr(self, '_page_errors', [])[-20:],
                'notes': ['已真实执行 LLM 生成步骤；定位失败会基于元素地图候选 locator 自动 patch 并重试'],
            }
            review_queue = self._build_healing_review_queue(step_results, repairs)
            if review_queue:
                verification['review_queue'] = review_queue
            if trace_path:
                verification['trace_path'] = trace_path
            plan_validation = verification.get('plan_validation') if isinstance(verification.get('plan_validation'), dict) else {}
            if failed_steps:
                verification['status'] = 'failed'
            else:
                verification['status'] = 'success'
                if plan_validation.get('status') == 'failed':
                    warnings = verification.get('warnings') if isinstance(verification.get('warnings'), list) else []
                    warnings.append('静态 plan_validation 未完全通过，但真实 Playwright 执行已通过，按真实执行结果判定成功')
                    verification['warnings'] = warnings
            result['verification_result'] = verification
            result['status'] = 'failed' if verification.get('status') == 'failed' else 'success'
            if failed_steps:
                result['failure_category'] = failed_category or result.get('failure_category') or 'unknown'
                result['message'] = f"AI 生成步骤真实执行失败: {failed_steps[0].get('message')}"
            else:
                result['failure_category'] = ''
                result['message'] = (
                    f"AI 生成步骤真实执行成功，自动修复 {len(repaired_steps)} 个步骤"
                    if repaired_steps else
                    'AI 生成步骤真实执行成功'
                )
            if repairs:
                repair_history = result.get('repair_history') if isinstance(result.get('repair_history'), list) else []
                repair_history.extend(repairs[:max_repair_rounds])
                result['repair_history'] = repair_history
                if isinstance(result.get('generated_case'), dict):
                    result['generated_case']['steps'] = steps
                if isinstance(result.get('test_plan'), dict):
                    result['test_plan']['steps'] = steps
            return result
        except Exception as exc:
            verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
            category = self._failure_category_from_error(str(exc))
            verification['flow_execution'] = {
                'status': 'failed',
                'total_steps': len(step_results),
                'passed_steps': len([item for item in step_results if item.get('status') in {'success', 'repaired'}]),
                'failed_steps': 1,
                'duration': time.time() - started,
                'steps': step_results,
                'error': str(exc)[:1000],
                'failure_category': category,
                'notes': ['生成步骤真实执行阶段发生框架级异常'],
            }
            review_queue = self._build_healing_review_queue(step_results, [])
            if review_queue:
                verification['review_queue'] = review_queue
            trace_path = self.get_current_trace_path()
            if trace_path:
                verification['flow_execution']['trace_path'] = trace_path
                verification['trace_path'] = trace_path
            result['verification_result'] = verification
            result['status'] = 'failed'
            result['failure_category'] = category
            result['message'] = f"AI 生成步骤真实执行异常: {exc}"
            return result

    @staticmethod
    def _official_mcp_resolve_ref(step: dict[str, Any], current_elements: list[dict[str, Any]]) -> tuple[str, str]:
        locator = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
        wanted_ref = str(locator.get('value') or '') if locator.get('type') == 'mcp_ref' else ''
        target_name = str(step.get('target_name') or locator.get('name') or '').strip().lower()
        if wanted_ref:
            for element in current_elements:
                if str(element.get('mcp_ref') or '') == wanted_ref:
                    return wanted_ref, element.get('name') or target_name or wanted_ref
        if target_name:
            for element in current_elements:
                name = str(element.get('name') or element.get('accessible_name') or '').strip().lower()
                if name and (name == target_name or target_name in name or name in target_name):
                    return str(element.get('mcp_ref') or ''), element.get('name') or target_name
        element_key = str(step.get('element_key') or '')
        if element_key:
            for element in current_elements:
                if str(element.get('element_key') or '') == element_key:
                    return str(element.get('mcp_ref') or ''), element.get('name') or element_key
        return '', target_name

    @staticmethod
    def _official_mcp_element_action_score(operation: str, element: dict[str, Any]) -> int:
        actions = element.get('actions') if isinstance(element.get('actions'), list) else []
        role = str(element.get('role') or '').lower()
        score = 0
        if operation == 'fill' and ('fill' in actions or role in {'textbox', 'input', 'searchbox', 'combobox'}):
            score += 20
        elif operation in {'click', 'check', 'uncheck'} and ('click' in actions or role in {'button', 'link', 'checkbox', 'radio'}):
            score += 20
        elif operation == 'select_option' and ('select_option' in actions or role in {'combobox', 'select'}):
            score += 20
        elif operation.startswith('assert_'):
            score += 10
        if element.get('mcp_ref'):
            score += 5
        return score

    @classmethod
    def _official_mcp_rank_repair_elements(
        cls,
        step: dict[str, Any],
        current_elements: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        operation = str(step.get('operation') or step.get('action') or '').strip().lower()
        target_values = [
            step.get('target_name'),
            step.get('element_key'),
            step.get('description'),
        ]
        target_terms = [
            re.sub(r'\s+', ' ', str(value or '').strip().lower())
            for value in target_values
            if str(value or '').strip()
        ]
        ranked: list[tuple[int, dict[str, Any]]] = []
        for element in current_elements:
            if not isinstance(element, dict) or not element.get('mcp_ref'):
                continue
            haystack = re.sub(
                r'\s+',
                ' ',
                ' '.join(str(element.get(key) or '') for key in ['name', 'accessible_name', 'text', 'role']).strip().lower(),
            )
            score = cls._official_mcp_element_action_score(operation, element)
            for term in target_terms:
                if not term:
                    continue
                if haystack == term:
                    score += 60
                elif term in haystack or haystack in term:
                    score += 35
                else:
                    parts = [part for part in re.split(r'[\s:/：,，()（）_-]+', term) if len(part) >= 2]
                    score += min(25, sum(8 for part in parts if part in haystack))
            if score > 0:
                ranked.append((score, element))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked

    async def _official_mcp_repair_and_execute_plan_step(
        self,
        adapter: PlaywrightMcpAdapter,
        step: dict[str, Any],
        base_url: str,
        safety_policy: dict[str, Any],
        max_repair_rounds: int,
    ) -> tuple[bool, str, dict[str, Any], list[dict[str, Any]]]:
        repairs: list[dict[str, Any]] = []
        if max_repair_rounds <= 0:
            return False, '官方 MCP 自愈未启用', {}, repairs

        original_locator = self._plan_step_locator(step)
        state = await self._official_mcp_collect_state(adapter, 'repair_snapshot', base_url)
        ranked = self._official_mcp_rank_repair_elements(step, state.get('elements') or [])
        original_ref = str(original_locator.get('value') or '') if original_locator.get('type') == 'mcp_ref' else ''
        attempts = 0

        for score, element in ranked:
            ref = str(element.get('mcp_ref') or '')
            if not ref or ref == original_ref:
                continue
            attempts += 1
            new_locator = {
                'type': 'mcp_ref',
                'value': ref,
                'name': element.get('name') or element.get('accessible_name') or ref,
                'confidence': min(0.98, max(0.50, score / 100)),
                'source': 'official_mcp_runtime_repair',
            }
            patched_step = {**step, 'locator_hint': new_locator}
            try:
                success, message, tool_context = await self._official_mcp_execute_plan_step(
                    adapter,
                    patched_step,
                    base_url,
                    safety_policy,
                )
                if success:
                    step['locator_hint'] = new_locator
                    repairs.append({
                        'round': attempts,
                        'type': 'ai_plan_step_locator_patch',
                        'provider': 'official-playwright-mcp',
                        'page_key': step.get('page_key') or '',
                        'element_key': step.get('element_key') or element.get('element_key') or '',
                        'step_sort': step.get('step_sort'),
                        'target_name': step.get('target_name') or element.get('name') or '',
                        'old_locator': original_locator,
                        'new_locator': new_locator,
                        'score': score,
                        'message': '官方 MCP 执行失败后，已重新 snapshot 并切换到新的元素 ref 重试成功',
                    })
                    return True, message, tool_context, repairs
            except Exception as exc:
                repairs.append({
                    'round': attempts,
                    'type': 'ai_plan_step_locator_patch_failed',
                    'provider': 'official-playwright-mcp',
                    'page_key': step.get('page_key') or '',
                    'element_key': step.get('element_key') or element.get('element_key') or '',
                    'step_sort': step.get('step_sort'),
                    'candidate_locator': new_locator,
                    'score': score,
                    'error': str(exc)[:500],
                })
            if attempts >= max_repair_rounds:
                break

        return False, '官方 MCP 当前 snapshot 中未找到可成功重试的替代 ref', {}, repairs

    async def _official_mcp_execute_plan_step(
        self,
        adapter: PlaywrightMcpAdapter,
        step: dict[str, Any],
        base_url: str,
        safety_policy: dict[str, Any],
    ) -> tuple[bool, str, dict[str, Any]]:
        operation = str(step.get('operation') or step.get('action') or '').strip().lower()
        value = str(step.get('value') or '')
        if step.get('requires_confirmation'):
            return False, '步骤需要人工确认，官方 MCP 自动验证未执行', {}
        if operation == 'goto':
            url = value or base_url
            if not self._is_url_allowed(url, safety_policy, base_url):
                return False, f'跳转 URL 不在安全边界允许范围内: {url}', {}
            await adapter.navigate(url)
            return True, 'MCP 页面跳转成功', {}
        if operation == 'wait':
            try:
                await adapter.wait_for(time_seconds=float(value or 1))
            except Exception:
                pass
            return True, 'MCP 等待完成', {}

        state = await self._official_mcp_collect_state(adapter, 'runtime_snapshot', base_url)
        current_elements = state.get('elements') or []
        ref, element_name = self._official_mcp_resolve_ref(step, current_elements)
        if not ref:
            return False, f'当前 MCP snapshot 中未找到步骤元素 ref: {step.get("target_name") or step.get("element_key")}', {}

        if operation == 'fill':
            await adapter.type_text(ref, value, element_name)
        elif operation == 'click':
            await adapter.click(ref, element_name)
            try:
                await adapter.wait_for(time_seconds=1)
            except Exception:
                pass
        elif operation == 'select_option':
            await adapter.select_option(ref, value, element_name)
        elif operation == 'check':
            await adapter.click(ref, element_name)
        elif operation == 'uncheck':
            await adapter.click(ref, element_name)
        elif operation in {'assert_visible', 'assert_text', 'assert_contain_text', 'assert_value'}:
            if operation in {'assert_text', 'assert_contain_text'} and value and value not in json.dumps(state, ensure_ascii=False):
                return False, f'MCP snapshot 未包含断言文本: {value}', {'mcp_ref': ref, 'element': element_name}
        else:
            return False, f'未知或暂不支持的 MCP 步骤操作: {operation}', {'mcp_ref': ref, 'element': element_name}
        return True, f'MCP tool 执行成功: {operation}', {'mcp_ref': ref, 'element': element_name}

    async def _verify_ai_generated_flow_with_official_mcp(self, args: dict, result: dict) -> dict:
        generated_case = result.get('generated_case') if isinstance(result.get('generated_case'), dict) else {}
        steps = generated_case.get('steps') or result.get('test_plan', {}).get('steps') or []
        if not isinstance(steps, list) or not steps:
            return result
        task_id = args.get('task_id') or result.get('task_id')
        env_config = args.get('environment_config') or {}
        safety_policy = args.get('safety_policy') or {}
        element_map = result.get('element_map') if isinstance(result.get('element_map'), dict) else {}
        base_url = element_map.get('base_url') or args.get('target_url') or env_config.get('base_url') or ''
        max_repair_rounds = int(args.get('max_repair_rounds') or safety_policy.get('max_repair_rounds') or 2)
        step_results: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        started = time.time()

        try:
            async with self._official_mcp_adapter() as adapter:
                auth_observations: list[dict[str, Any]] = []
                credentials = self._extract_login_credentials(args)
                if base_url and credentials.get('username') and credentials.get('password'):
                    try:
                        await adapter.navigate(base_url)
                        first_state = await self._official_mcp_collect_state(adapter, 'flow_auth_setup', base_url)
                        if self._looks_like_login_page(first_state):
                            await self._official_mcp_auto_login_if_possible(adapter, first_state, args, auth_observations)
                        else:
                            auth_observations.append({
                                'type': 'official_mcp_flow_auth_setup',
                                'status': 'already_authenticated_or_public',
                            })
                    except Exception as exc:
                        auth_observations.append({
                            'type': 'official_mcp_flow_auth_setup',
                            'status': 'failed',
                            'error': str(exc)[:800],
                        })
                if base_url and not any(
                    isinstance(step, dict) and str(step.get('operation') or '').lower() == 'goto'
                    for step in steps
                ):
                    await adapter.navigate(base_url)
                for index, step in enumerate(steps):
                    if not isinstance(step, dict):
                        continue
                    operation = str(step.get('operation') or step.get('action') or '').strip().lower()
                    record = {
                        'step_sort': step.get('step_sort', index),
                        'operation': operation,
                        'description': step.get('description') or '',
                        'page_key': step.get('page_key') or '',
                        'element_key': step.get('element_key') or '',
                        'target_name': step.get('target_name') or '',
                        'status': 'success',
                        'message': '',
                        'duration': 0,
                        'mcp_tool_protocol': True,
                    }
                    step_started = time.time()
                    try:
                        success, message, tool_context = await self._official_mcp_execute_plan_step(
                            adapter,
                            step,
                            base_url,
                            safety_policy,
                        )
                        if not success:
                            raise RuntimeError(message)
                        record['message'] = message
                        record['tool_context'] = tool_context
                    except Exception as exc:
                        category = self._failure_category_from_error(str(exc), operation)
                        repaired = False
                        repair_message = ''
                        tool_context = {}
                        if category in {'locator', 'timeout', 'assertion', 'state_dependency'} and operation not in {'goto', 'wait'}:
                            repaired, repair_message, tool_context, step_repairs = await self._official_mcp_repair_and_execute_plan_step(
                                adapter,
                                step,
                                base_url,
                                safety_policy,
                                max_repair_rounds,
                            )
                            repairs.extend(step_repairs)
                        if repaired:
                            record['status'] = 'repaired'
                            record['message'] = repair_message
                            record['failure_category_before_repair'] = category
                            record['tool_context'] = tool_context
                        else:
                            record['status'] = 'failed'
                            record['message'] = str(exc)[:1000]
                            record['failure_category'] = category
                            try:
                                screenshot = await adapter.screenshot()
                                record['mcp_screenshot'] = adapter._content_text(screenshot)[:1000]
                            except Exception:
                                pass
                            step_results.append(record)
                            break
                    finally:
                        record['duration'] = time.time() - step_started
                    step_results.append(record)

                failed_steps = [item for item in step_results if item.get('status') == 'failed']
                repaired_steps = [item for item in step_results if item.get('status') == 'repaired']
                if failed_steps and self._is_mcp_session_loss_error(str(failed_steps[0].get('message') or '')):
                    logger.warning('官方 MCP 生成步骤执行阶段会话丢失，降级使用原生 Playwright 重新执行生成步骤')
                    original_provider = self.mcp_provider
                    self.mcp_provider = 'native-playwright-fallback'
                    try:
                        fallback_result = await self.verify_ai_generated_flow(args, result)
                    finally:
                        self.mcp_provider = original_provider
                    fallback_obs = fallback_result.get('mcp_observations') if isinstance(fallback_result.get('mcp_observations'), list) else []
                    fallback_result['mcp_observations'] = [
                        {
                            'type': 'official_mcp_flow_fallback',
                            'reason': failed_steps[0].get('message') or 'official MCP session lost during flow execution',
                            'official_steps': len(step_results),
                        },
                        *fallback_obs,
                    ]
                    verification = fallback_result.get('verification_result') if isinstance(fallback_result.get('verification_result'), dict) else {}
                    verification['official_mcp_flow_execution'] = {
                        'status': 'failed',
                        'reason': failed_steps[0].get('message') or 'official MCP session lost during flow execution',
                        'steps': step_results,
                    }
                    fallback_result['verification_result'] = verification
                    fallback_flow = verification.get('flow_execution') if isinstance(verification.get('flow_execution'), dict) else {}
                    fallback_succeeded = (
                        fallback_result.get('status') == 'success'
                        and fallback_flow.get('status') == 'success'
                    )
                    if fallback_succeeded:
                        fallback_result['failure_category'] = ''
                        fallback_result['message'] = '官方 MCP 生成步骤执行阶段会话不稳定，已自动降级为原生 Playwright 验证成功'
                    else:
                        native_failure = str(
                            fallback_result.get('message')
                            or fallback_flow.get('error')
                            or '原生 Playwright 未完成生成步骤验证'
                        )
                        fallback_result['status'] = 'failed'
                        fallback_result['message'] = f'官方 MCP 会话丢失，原生 Playwright 降级验证失败: {native_failure}'
                    return fallback_result

                verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
                verification['flow_execution'] = {
                    'status': 'success' if not failed_steps else 'failed',
                    'provider': 'official-playwright-mcp',
                    'total_steps': len(step_results),
                    'passed_steps': len([item for item in step_results if item.get('status') in {'success', 'repaired'}]),
                    'failed_steps': len(failed_steps),
                    'repaired_steps': len(repaired_steps),
                    'duration': time.time() - started,
                    'steps': step_results,
                    'auth_setup': auth_observations,
                    'mcp_tools': sorted(adapter.tools),
                    'notes': ['已通过官方 Playwright MCP server refs 和 tools/call 真实执行生成步骤；失败时会重新 snapshot 并尝试 ref patch 重跑'],
                }
                review_queue = self._build_healing_review_queue(step_results, repairs)
                if review_queue:
                    verification['review_queue'] = review_queue
                plan_validation = verification.get('plan_validation') if isinstance(verification.get('plan_validation'), dict) else {}
                if failed_steps:
                    verification['status'] = 'failed'
                else:
                    verification['status'] = 'success'
                    if plan_validation.get('status') == 'failed':
                        warnings = verification.get('warnings') if isinstance(verification.get('warnings'), list) else []
                        warnings.append('静态 plan_validation 未完全通过，但真实 MCP/Playwright 执行已通过，按真实执行结果判定成功')
                        verification['warnings'] = warnings
                result['verification_result'] = verification
                result['status'] = 'failed' if verification.get('status') == 'failed' else 'success'
                if failed_steps:
                    result['failure_category'] = failed_steps[0].get('failure_category') or 'unknown'
                    result['message'] = f"官方 MCP 生成步骤执行失败: {failed_steps[0].get('message')}"
                else:
                    result['failure_category'] = ''
                    result['message'] = (
                        f"官方 MCP 生成步骤执行成功，自动修复 {len(repaired_steps)} 个步骤"
                        if repaired_steps else
                        '官方 MCP 生成步骤执行成功'
                    )
                if repairs:
                    repair_history = result.get('repair_history') if isinstance(result.get('repair_history'), list) else []
                    repair_history.extend(repairs[:max_repair_rounds])
                    result['repair_history'] = repair_history
                    if isinstance(result.get('generated_case'), dict):
                        result['generated_case']['steps'] = steps
                    if isinstance(result.get('test_plan'), dict):
                        result['test_plan']['steps'] = steps
                return result
        except Exception as exc:
            verification = result.get('verification_result') if isinstance(result.get('verification_result'), dict) else {}
            category = self._failure_category_from_error(str(exc))
            verification['flow_execution'] = {
                'status': 'failed',
                'provider': 'official-playwright-mcp',
                'total_steps': len(step_results),
                'passed_steps': len([item for item in step_results if item.get('status') == 'success']),
                'failed_steps': 1,
                'duration': time.time() - started,
                'steps': step_results,
                'error': str(exc)[:1000],
                'failure_category': category,
                'notes': ['官方 MCP 生成步骤真实执行阶段发生协议或工具异常'],
            }
            result['verification_result'] = verification
            result['status'] = 'failed'
            result['failure_category'] = category
            result['message'] = f"官方 MCP 生成步骤真实执行异常: {exc}"
            return result

    async def _mcp_collect_page_state(
        self,
        page: Page,
        page_key: str,
        screenshot_path: str,
        max_elements: int = 180,
        priority_keywords: Optional[list[str]] = None,
    ) -> dict:
        """采集 Playwright 原生页面状态：DOM 可交互元素、候选 locator、bounding box 和截图。"""
        try:
            await page.screenshot(path=screenshot_path, full_page=True, timeout=10000)
        except Exception as exc:
            logger.warning(f"页面截图失败，继续采集 DOM: {exc}")

        state = await page.evaluate(
            """
            (maxElements) => {
              const selector = [
                'a[href]', 'button', 'input', 'textarea', 'select',
                '[role]', '[aria-label]', '[contenteditable="true"]',
                '[data-testid]', '[data-test]', '[test-id]',
                '[ng-click]', '[data-ng-click]', '[x-ng-click]', '[onclick]',
                '[tabindex]:not([tabindex="-1"])'
              ].join(',');
              const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
              const attr = (el, name) => el.getAttribute(name) || '';
              const cssEscape = (value) => {
                if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
                return String(value).replace(/["'\\\\#.:\\[\\]>+~*^$|=\\s]/g, '\\\\$&');
              };
              const labelText = (el) => {
                if (el.labels && el.labels.length) return Array.from(el.labels).map(textOf).filter(Boolean).join(' ');
                const id = attr(el, 'id');
                if (id) {
                  const label = document.querySelector(`label[for="${cssEscape(id)}"]`);
                  if (label) return textOf(label);
                }
                const closest = el.closest('label');
                if (closest) return textOf(closest);
                const formItem = el.closest(
                  '.el-form-item, .arco-form-item, .ant-form-item, .form-item'
                );
                if (formItem) {
                  const label = formItem.querySelector(
                    '.el-form-item__label, .arco-form-item-label, .ant-form-item-label, label, [class*="label"], [class*="Label"]'
                  );
                  if (label) return textOf(label).replace(/[＊*]/g, '').trim();
                }
                const ariaLabelledBy = attr(el, 'aria-labelledby');
                if (ariaLabelledBy) {
                  return ariaLabelledBy
                    .split(/\\s+/)
                    .map((id) => textOf(document.getElementById(id)))
                    .filter(Boolean)
                    .join(' ');
                }
                return '';
              };
              const formContext = (el) => {
                const dialog = el.closest(
                  '[role="dialog"], .el-dialog, .arco-modal, .ant-modal, .arco-drawer, .ant-drawer, [class*="dialog"], [class*="modal"], [class*="drawer"]'
                );
                const formItem = el.closest(
                  '.el-form-item, .arco-form-item, .ant-form-item, .form-item'
                );
                const label = formItem ? textOf(formItem.querySelector(
                  '.el-form-item__label, .arco-form-item-label, .ant-form-item-label, label, [class*="label"], [class*="Label"]'
                )).replace(/[＊*]/g, '').trim() : '';
                const required = Boolean(
                  el.required ||
                  attr(el, 'aria-required') === 'true' ||
                  formItem?.classList?.contains('is-required') ||
                  formItem?.querySelector?.('.is-required, [aria-required="true"], .required, [class*="required"], [class*="Required"]') ||
                  /[＊*]/.test(textOf(formItem || null))
                );
                return {
                  label,
                  required,
                  in_dialog: Boolean(dialog),
                  dialog_name: dialog ? (attr(dialog, 'aria-label') || textOf(dialog.querySelector('.el-dialog__title, .arco-modal-title, .ant-modal-title, [class*="title"], [class*="Title"]')) || attr(dialog, 'role') || '') : '',
                };
              };
              const inferRole = (el) => {
                const explicit = attr(el, 'role');
                if (explicit) return explicit;
                const tag = el.tagName.toLowerCase();
                const type = (attr(el, 'type') || '').toLowerCase();
                if (tag === 'button') return 'button';
                if (tag === 'a') return 'link';
                if (tag === 'select') return 'combobox';
                if (tag === 'textarea') return 'textbox';
                if (tag === 'input') {
                  if (attr(el, 'role') === 'combobox' || attr(el, 'aria-haspopup') === 'listbox') return 'combobox';
                  if (['button', 'submit', 'reset'].includes(type)) return 'button';
                  if (type === 'checkbox') return 'checkbox';
                  if (type === 'radio') return 'radio';
                  return 'textbox';
                }
                return '';
              };
              const inferActions = (el) => {
                const tag = el.tagName.toLowerCase();
                const type = (attr(el, 'type') || '').toLowerCase();
                if (tag === 'select') return ['select_option'];
                if (tag === 'textarea' || el.isContentEditable) return ['fill'];
                if (tag === 'input') {
                  if (type === 'checkbox') return ['check', 'uncheck'];
                  if (type === 'radio') return ['check'];
                  if (['button', 'submit', 'reset'].includes(type)) return ['click'];
                  if (attr(el, 'role') === 'combobox' || attr(el, 'aria-haspopup') === 'listbox' || attr(el, 'readonly')) return ['select_option'];
                  return ['fill'];
                }
                return ['click'];
              };
              const absoluteXPath = (el) => {
                if (el.id) return `//*[@id="${el.id}"]`;
                const parts = [];
                while (el && el.nodeType === Node.ELEMENT_NODE) {
                  let index = 1;
                  let sibling = el.previousElementSibling;
                  while (sibling) {
                    if (sibling.tagName === el.tagName) index += 1;
                    sibling = sibling.previousElementSibling;
                  }
                  parts.unshift(`${el.tagName.toLowerCase()}[${index}]`);
                  el = el.parentElement;
                }
                return '/' + parts.join('/');
              };
              const uniqueCssPath = (el) => {
                const testId = attr(el, 'data-testid') || attr(el, 'data-test') || attr(el, 'test-id');
                if (testId) return `[data-testid="${testId}"], [data-test="${testId}"], [test-id="${testId}"]`;
                if (el.id) return `#${cssEscape(el.id)}`;
                const name = attr(el, 'name');
                if (name) return `${el.tagName.toLowerCase()}[name="${name}"]`;
                const parts = [];
                while (el && el.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
                  const tag = el.tagName.toLowerCase();
                  let index = 1;
                  let sibling = el.previousElementSibling;
                  while (sibling) {
                    if (sibling.tagName === el.tagName) index += 1;
                    sibling = sibling.previousElementSibling;
                  }
                  parts.unshift(`${tag}:nth-of-type(${index})`);
                  el = el.parentElement;
                }
                return parts.join(' > ');
              };
              const unique = (items) => {
                const seen = new Set();
                return items.filter((item) => {
                  const key = `${item.type}:${item.value || item.name || ''}`;
                  if ((!item.value && !item.name) || seen.has(key)) return false;
                  seen.add(key);
                  return true;
                });
              };
              const elementPriority = (el) => {
                const tag = el.tagName.toLowerCase();
                const type = (attr(el, 'type') || '').toLowerCase();
                const ctx = formContext(el);
                let score = ctx.in_dialog ? 1000 : 0;
                if (ctx.required) score += 220;
                if (['input', 'textarea', 'select'].includes(tag) || el.isContentEditable) score += 160;
                if (attr(el, 'role') === 'combobox' || attr(el, 'role') === 'textbox') score += 140;
                const text = textOf(el);
                if (['button', 'submit'].includes(type) || tag === 'button' || attr(el, 'role') === 'button') {
                  score += /保存|提交|确定|确认|save|submit|confirm|ok/i.test(text) ? 120 : 60;
                }
                return score;
              };
              const elements = Array.from(document.querySelectorAll(selector))
                .filter((el) => {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                })
                .sort((left, right) => elementPriority(right) - elementPriority(left))
                .slice(0, maxElements)
                .map((el, index) => {
                  const tag = el.tagName.toLowerCase();
                  const inputType = tag === 'input' ? (attr(el, 'type') || 'text').toLowerCase() : '';
                  const text = textOf(el);
                  const label = labelText(el);
                  const ctx = formContext(el);
                  const placeholder = attr(el, 'placeholder');
                  const ariaLabel = attr(el, 'aria-label');
                  const testId = attr(el, 'data-testid') || attr(el, 'data-test') || attr(el, 'test-id');
                  const role = inferRole(el);
                  const inForm = Boolean(el.closest('form'));
                  const roleName = ariaLabel || text || attr(el, 'value');
                  const isFieldControl = ['input', 'textarea', 'select'].includes(tag) || el.isContentEditable || role === 'textbox' || role === 'combobox';
                  const options = tag === 'select'
                    ? Array.from(el.options || []).map((option) => ({
                        value: attr(option, 'value'),
                        label: textOf(option),
                        disabled: option.disabled,
                      })).filter((option) => !option.disabled && (option.value || option.label)).slice(0, 20)
                    : [];
                  const candidates = unique(isFieldControl ? [
                    {type: 'test_id', value: testId, confidence: 0.98},
                    {type: 'placeholder', value: placeholder, confidence: 0.92},
                    {type: 'id', value: attr(el, 'id'), confidence: 0.82},
                    {type: 'name', value: attr(el, 'name'), confidence: 0.80},
                    {type: 'role', value: role, name: roleName, confidence: role && roleName ? 0.76 : 0},
                    {type: 'css', value: uniqueCssPath(el), confidence: 0.70},
                    {type: 'label', value: label, confidence: 0.62},
                    {type: 'xpath', value: absoluteXPath(el), confidence: 0.45},
                  ] : [
                    {type: 'test_id', value: testId, confidence: 0.98},
                    {type: 'label', value: label, confidence: 0.90},
                    {type: 'role', value: role, name: roleName, confidence: role && roleName ? 0.84 : 0},
                    {type: 'text', value: text && text.length <= 80 ? text : '', confidence: 0.72},
                    {type: 'id', value: attr(el, 'id'), confidence: 0.70},
                    {type: 'name', value: attr(el, 'name'), confidence: 0.68},
                    {type: 'css', value: uniqueCssPath(el), confidence: 0.45},
                    {type: 'xpath', value: absoluteXPath(el), confidence: 0.35},
                  ]).filter((item) => item.confidence > 0);
                  const rect = el.getBoundingClientRect();
                  const somIndex = index + 1;
                  return {
                    element_key: `${tag}_${index + 1}`,
                    som_index: somIndex,
                    som_label: `${somIndex}`,
                    name: (ariaLabel || label || placeholder || text || attr(el, 'name') || attr(el, 'id') || `${tag}_${index + 1}`).slice(0, 120),
                    tag,
                    input_type: inputType,
                    role,
                    accessible_name: ariaLabel || label || text || placeholder || '',
                    text: text.slice(0, 200),
                    label: label || ctx.label,
                    placeholder,
                    test_id: testId,
                    href: attr(el, 'href'),
                    required: ctx.required,
                    in_dialog: ctx.in_dialog,
                    dialog_name: ctx.dialog_name,
                    form_label: ctx.label,
                    in_form: inForm,
                    visible: true,
                    enabled: !el.disabled && attr(el, 'aria-disabled') !== 'true',
                    actions: inferActions(el),
                    options,
                    locator_candidates: candidates,
                    recommended_locator: candidates[0] || null,
                    bounding_box: {
                      x: Math.round(rect.x),
                      y: Math.round(rect.y),
                      width: Math.round(rect.width),
                      height: Math.round(rect.height)
                    },
                    screenshot_region: {
                      x: Math.max(0, Math.round(rect.x)),
                      y: Math.max(0, Math.round(rect.y)),
                      width: Math.round(rect.width),
                      height: Math.round(rect.height),
                      som_label: `${somIndex}`
                    },
                  };
                });
              return {url: window.location.href, title: document.title || '', elements};
            }
            """,
            max_elements,
        )
        state = self._stabilize_observed_locators(state)
        state['page_key'] = page_key
        state['name'] = state.get('title') or state.get('url') or page_key
        state['screenshot'] = screenshot_path
        state['source'] = 'playwright_native_observation'
        state['network_summary'] = self._network_summary()
        state['component_context'] = {
            'framework_hints': await self._collect_framework_hints(page),
            'viewport': await page.viewport_size() if callable(getattr(page, 'viewport_size', None)) else page.viewport_size,
        }
        state['accessibility_snapshot'] = await self._collect_accessibility_snapshot(page)
        self._attach_accessibility_refs(state)
        state['visual_som'] = {
            'enabled': True,
            'label_strategy': 'element.som_index',
            'element_count': len(state.get('elements') or []),
            'note': '元素已写入 som_index、som_label 和 screenshot_region，可用于视觉定位消歧',
        }
        state['virtual_lists'] = await self._collect_virtual_list_state(page)
        state['supplemental_collectors'] = {
            'scrapling': {
                'enabled': False,
                'status': 'not_configured',
                'note': '已预留 Scrapling 补充采集入口，可用于反爬或大规模页面结构采集',
            }
        }
        state['shadow_dom'] = await self._collect_shadow_dom_state(page, max_elements=60)
        state['frames'] = await self._collect_frame_states(
            page,
            max_elements_per_frame=120 if priority_keywords else 60,
            priority_keywords=priority_keywords,
        )
        return state

    async def _collect_accessibility_snapshot(self, page: Page, max_nodes: int = 260) -> dict[str, Any]:
        """通过 Playwright 原生 CDP 采集可访问性树摘要，给 LLM 规划提供 role/name 上下文。"""
        try:
            if not self._context:
                return {'source': 'playwright_native_cdp', 'status': 'skipped', 'reason': 'browser context not initialized'}
            cdp = await self._context.new_cdp_session(page)
            payload = await cdp.send('Accessibility.getFullAXTree')
            raw_nodes = payload.get('nodes') if isinstance(payload, dict) else []
            nodes = []
            for index, node in enumerate(raw_nodes or []):
                if len(nodes) >= max_nodes:
                    break
                if not isinstance(node, dict):
                    continue

                def ax_value(field: str) -> str:
                    value = node.get(field)
                    if isinstance(value, dict):
                        return str(value.get('value') or '')
                    return str(value or '')

                role = ax_value('role')
                name = ax_value('name')
                value = ax_value('value')
                if not role and not name and not value:
                    continue
                ignored = bool(node.get('ignored'))
                if ignored and not name:
                    continue
                nodes.append({
                    'ax_ref': f'ax_{len(nodes) + 1}',
                    'backend_node_id': node.get('backendDOMNodeId'),
                    'role': role,
                    'name': name[:180],
                    'value': value[:180],
                    'description': ax_value('description')[:180],
                    'ignored': ignored,
                })
            return {
                'source': 'playwright_native_cdp',
                'status': 'success',
                'node_count': len(nodes),
                'nodes': nodes,
                'note': '原生 Playwright CDP Accessibility.getFullAXTree 快照；ax_ref 为本次采集内稳定引用',
            }
        except Exception as exc:
            return {
                'source': 'playwright_native_cdp',
                'status': 'failed',
                'error': f'{type(exc).__name__}: {str(exc)[:500]}',
                'nodes': [],
                'node_count': 0,
            }

    @staticmethod
    def _attach_accessibility_refs(state: dict[str, Any]) -> None:
        snapshot = state.get('accessibility_snapshot') if isinstance(state.get('accessibility_snapshot'), dict) else {}
        nodes = [node for node in (snapshot.get('nodes') or []) if isinstance(node, dict)]
        if not nodes:
            return

        def normalize(value: Any) -> str:
            return re.sub(r'\s+', ' ', str(value or '').strip()).lower()

        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for node in nodes:
            key = (normalize(node.get('role')), normalize(node.get('name') or node.get('value')))
            if key[1]:
                buckets.setdefault(key, []).append(node)

        for element in state.get('elements') or []:
            if not isinstance(element, dict):
                continue
            role = normalize(element.get('role'))
            names = [
                element.get('accessible_name'),
                element.get('name'),
                element.get('label'),
                element.get('placeholder'),
                element.get('text'),
            ]
            matched = None
            for name in names:
                key = (role, normalize(name))
                if key in buckets:
                    matched = buckets[key][0]
                    break
            if matched:
                element['ax_ref'] = matched.get('ax_ref')
                element['accessibility'] = {
                    'ax_ref': matched.get('ax_ref'),
                    'role': matched.get('role'),
                    'name': matched.get('name'),
                    'description': matched.get('description'),
                }

    async def _collect_virtual_list_state(self, page: Page, max_containers: int = 20) -> dict[str, Any]:
        """识别疑似虚拟列表/滚动容器，给 LLM 和自愈提供滚动采样上下文。"""
        try:
            containers = await page.evaluate(
                """
                (maxContainers) => {
                  const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                  const attr = (el, name) => el.getAttribute(name) || '';
                  const candidates = Array.from(document.querySelectorAll('*')).filter((el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (rect.width < 120 || rect.height < 80) return false;
                    if (!['auto', 'scroll'].includes(style.overflowY)) return false;
                    return el.scrollHeight > rect.height * 1.5;
                  }).slice(0, maxContainers);
                  return candidates.map((el, index) => {
                    const rect = el.getBoundingClientRect();
                    const rows = Array.from(el.children || []).slice(0, 12).map((child, childIndex) => {
                      const childRect = child.getBoundingClientRect();
                      return {
                        index: childIndex,
                        tag: child.tagName.toLowerCase(),
                        text: textOf(child).slice(0, 160),
                        height: Math.round(childRect.height),
                      };
                    });
                    const avgRowHeight = rows.length
                      ? Math.round(rows.reduce((sum, item) => sum + item.height, 0) / rows.length)
                      : 0;
                    return {
                      container_key: `virtual_list_${index + 1}`,
                      tag: el.tagName.toLowerCase(),
                      id: attr(el, 'id'),
                      class_name: attr(el, 'class').slice(0, 200),
                      role: attr(el, 'role'),
                      text_sample: textOf(el).slice(0, 300),
                      scroll_top: Math.round(el.scrollTop),
                      scroll_height: Math.round(el.scrollHeight),
                      client_height: Math.round(el.clientHeight),
                      visible_ratio: Number((el.clientHeight / Math.max(el.scrollHeight, 1)).toFixed(3)),
                      estimated_total_rows: avgRowHeight > 0 ? Math.round(el.scrollHeight / avgRowHeight) : null,
                      visible_rows: rows,
                      bounding_box: {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                      },
                      detection_reason: 'scrollHeight significantly exceeds clientHeight',
                    };
                  });
                }
                """,
                max_containers,
            )
            return {
                'container_count': len(containers or []),
                'containers': containers or [],
                'sampling_strategy': 'top-visible-rows; future repair can scroll container and resample by container_key',
            }
        except Exception as exc:
            logger.debug(f"采集虚拟列表状态失败: {exc}")
            return {'error': str(exc)[:500], 'containers': [], 'container_count': 0}

    async def _collect_framework_hints(self, page: Page) -> dict[str, Any]:
        try:
            return await page.evaluate(
                """
                () => ({
                  vue: Boolean(window.__VUE__ || document.querySelector('[data-v-app], [ng-version]')?.hasAttribute('data-v-app')),
                  react: Boolean(window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || document.querySelector('[data-reactroot]')),
                  angular: Boolean(window.ng || document.querySelector('[ng-version]')),
                  elementPlus: Boolean(document.querySelector('.el-button, .el-dialog, .el-form, .el-table')),
                  antDesign: Boolean(document.querySelector('.ant-btn, .ant-modal, .ant-form, .ant-table')),
                })
                """
            )
        except Exception as exc:
            logger.debug(f"采集组件框架线索失败: {exc}")
            return {}

    async def _collect_shadow_dom_state(self, page: Page, max_elements: int = 60) -> dict[str, Any]:
        try:
            elements = await page.evaluate(
                """
                (maxElements) => {
                  const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                  const attr = (el, name) => el.getAttribute(name) || '';
                  const inferRole = (el) => {
                    const explicit = attr(el, 'role');
                    if (explicit) return explicit;
                    const tag = el.tagName.toLowerCase();
                    const type = (attr(el, 'type') || '').toLowerCase();
                    if (tag === 'button') return 'button';
                    if (tag === 'a') return 'link';
                    if (tag === 'select') return 'combobox';
                    if (tag === 'textarea') return 'textbox';
                    if (tag === 'input') {
                      if (['button', 'submit', 'reset'].includes(type)) return 'button';
                      if (type === 'checkbox') return 'checkbox';
                      if (type === 'radio') return 'radio';
                      return 'textbox';
                    }
                    return '';
                  };
                  const collect = [];
                  const interactive = [
                    'a[href]', 'button', 'input', 'textarea', 'select', '[role]', '[aria-label]',
                    '[data-testid]', '[data-test]', '[test-id]', '[ng-click]', '[data-ng-click]',
                    '[x-ng-click]', '[onclick]', '[tabindex]:not([tabindex="-1"])'
                  ].join(',');
                  const visit = (root, hostPath) => {
                    if (collect.length >= maxElements) return;
                    root.querySelectorAll(interactive).forEach((el) => {
                      if (collect.length >= maxElements) return;
                      const rect = el.getBoundingClientRect();
                      const style = window.getComputedStyle(el);
                      if (rect.width <= 0 || rect.height <= 0 || style.visibility === 'hidden' || style.display === 'none') return;
                      const text = textOf(el);
                      const ariaLabel = attr(el, 'aria-label');
                      const testId = attr(el, 'data-testid') || attr(el, 'data-test') || attr(el, 'test-id');
                      const role = inferRole(el);
                      const roleName = ariaLabel || text || attr(el, 'value');
                      const candidates = [
                        testId ? {type: 'test_id', value: testId, confidence: 0.95} : null,
                        ariaLabel ? {type: 'role', value: role || 'button', name: ariaLabel, confidence: 0.82} : null,
                        text && text.length <= 80 ? {type: 'text', value: text, confidence: 0.70} : null,
                      ].filter(Boolean);
                      collect.push({
                        element_key: `shadow_${collect.length + 1}`,
                        host_path: hostPath,
                        tag: el.tagName.toLowerCase(),
                        role,
                        name: (ariaLabel || text || testId || `shadow_${collect.length + 1}`).slice(0, 120),
                        text: text.slice(0, 200),
                        test_id: testId,
                        visible: true,
                        locator_candidates: candidates,
                        recommended_locator: candidates[0] || null,
                        bounding_box: {
                          x: Math.round(rect.x),
                          y: Math.round(rect.y),
                          width: Math.round(rect.width),
                          height: Math.round(rect.height),
                        },
                      });
                    });
                    root.querySelectorAll('*').forEach((el) => {
                      if (collect.length >= maxElements) return;
                      if (el.shadowRoot) {
                        const hostName = el.tagName.toLowerCase() + (el.id ? `#${el.id}` : '');
                        visit(el.shadowRoot, hostPath.concat(hostName));
                      }
                    });
                  };
                  visit(document, []);
                  return collect;
                }
                """,
                max_elements,
            )
            return {
                'open_shadow_root_count': len({tuple(item.get('host_path') or []) for item in elements}),
                'element_count': len(elements),
                'elements': elements,
            }
        except Exception as exc:
            logger.debug(f"采集 shadow DOM 失败: {exc}")
            return {'error': str(exc)[:500], 'elements': []}

    async def _collect_frame_states(
        self,
        page: Page,
        max_elements_per_frame: int = 60,
        priority_keywords: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        frames = []
        keywords = [
            self._clean_visible_text(keyword)
            for keyword in (priority_keywords or [])
            if self._clean_visible_text(keyword)
        ][:24]
        for index, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            frame_state = {
                'frame_key': f'frame_{index}',
                'name': frame.name,
                'url': frame.url,
                'elements': [],
                'element_count': 0,
            }
            try:
                elements = await frame.evaluate(
                    """
                    ({maxElements, priorityKeywords}) => {
                      const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                      const attr = (el, name) => el.getAttribute(name) || '';
                      const inferRole = (el) => {
                        const explicit = attr(el, 'role');
                        if (explicit) return explicit;
                        const tag = el.tagName.toLowerCase();
                        const type = attr(el, 'type').toLowerCase();
                        if (tag === 'button' || ['button', 'submit', 'reset'].includes(type)) return 'button';
                        if (tag === 'a') return 'link';
                        if (tag === 'select') return 'combobox';
                        if (tag === 'textarea') return 'textbox';
                        if (tag === 'input') {
                          if (type === 'checkbox') return 'checkbox';
                          if (type === 'radio') return 'radio';
                          return 'textbox';
                        }
                        if (attr(el, 'ng-click') || attr(el, 'data-ng-click') || attr(el, 'x-ng-click') || attr(el, 'onclick')) return 'button';
                        return '';
                      };
                      const inferActions = (el) => {
                        const tag = el.tagName.toLowerCase();
                        const type = attr(el, 'type').toLowerCase();
                        if (tag === 'select') return ['select_option'];
                        if (tag === 'textarea' || el.isContentEditable) return ['fill'];
                        if (tag === 'input') {
                          if (type === 'checkbox') return ['check', 'uncheck'];
                          if (type === 'radio') return ['check'];
                          if (['button', 'submit', 'reset'].includes(type)) return ['click'];
                          return ['fill'];
                        }
                        return ['click'];
                      };
                      const isRendered = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return (
                          rect.width > 0 &&
                          rect.height > 0 &&
                          style.visibility !== 'hidden' &&
                          style.display !== 'none' &&
                          attr(el, 'hidden') === '' &&
                          attr(el, 'aria-hidden') !== 'true'
                        );
                      };
                      const selector = [
                        'a[href]', 'button', 'input', 'textarea', 'select', '[role]', '[aria-label]',
                        '[data-testid]', '[data-test]', '[test-id]', '[ng-click]', '[data-ng-click]',
                        '[x-ng-click]', '[onclick]', '[tabindex]:not([tabindex="-1"])'
                      ].join(',');
                      const scoreElement = (el) => {
                        const tag = el.tagName.toLowerCase();
                        const type = attr(el, 'type').toLowerCase();
                        const text = textOf(el);
                        const label = [
                          attr(el, 'aria-label'),
                          text,
                          attr(el, 'placeholder'),
                          attr(el, 'name'),
                          attr(el, 'id')
                        ].filter(Boolean).join(' ');
                        let score = 0;
                        for (const keyword of priorityKeywords || []) {
                          if (keyword && label.includes(keyword)) score += 1200 + Math.min(keyword.length, 30);
                        }
                        if (tag === 'button' || ['button', 'submit', 'reset'].includes(type) || attr(el, 'role') === 'button') score += 180;
                        if (['input', 'textarea', 'select'].includes(tag) || attr(el, 'role') === 'combobox') score += 120;
                        const rect = el.getBoundingClientRect();
                        score += Math.max(0, 1000 - Math.round(rect.top));
                        return score;
                      };
                      return Array.from(document.querySelectorAll(selector))
                        .filter(isRendered)
                        .sort((left, right) => scoreElement(right) - scoreElement(left))
                        .slice(0, maxElements)
                        .map((el, i) => {
                        const rect = el.getBoundingClientRect();
                        const text = textOf(el);
                        const ariaLabel = attr(el, 'aria-label');
                        const testId = attr(el, 'data-testid') || attr(el, 'data-test') || attr(el, 'test-id');
                        const name = ariaLabel || text || attr(el, 'placeholder') || attr(el, 'name') || attr(el, 'id') || `${el.tagName.toLowerCase()}_${i + 1}`;
                        const role = inferRole(el);
                        const candidates = [
                          testId ? {type: 'test_id', value: testId, confidence: 0.95} : null,
                          attr(el, 'placeholder') ? {type: 'placeholder', value: attr(el, 'placeholder'), confidence: 0.84} : null,
                          role && name ? {type: 'role', value: role, name: name.slice(0, 120), confidence: 0.78} : null,
                          text && text.length <= 80 ? {type: 'text', value: text, confidence: 0.70} : null,
                          attr(el, 'id') ? {type: 'id', value: attr(el, 'id'), confidence: 0.68} : null,
                          attr(el, 'name') ? {type: 'name', value: attr(el, 'name'), confidence: 0.66} : null,
                        ].filter(Boolean);
                        return {
                          element_key: `frame_${i + 1}`,
                          name: name.slice(0, 120),
                          tag: el.tagName.toLowerCase(),
                          input_type: el.tagName.toLowerCase() === 'input' ? (attr(el, 'type') || 'text').toLowerCase() : '',
                          role,
                          accessible_name: name.slice(0, 120),
                          text: text.slice(0, 200),
                          placeholder: attr(el, 'placeholder'),
                          test_id: testId,
                          href: attr(el, 'href'),
                          required: el.required || attr(el, 'aria-required') === 'true' || !!attr(el, 'required'),
                          visible: rect.width > 0 && rect.height > 0,
                          enabled: !el.disabled && attr(el, 'aria-disabled') !== 'true',
                          in_form: Boolean(el.closest('form')),
                          actions: inferActions(el),
                          locator_candidates: candidates,
                          recommended_locator: candidates[0] || null,
                          bounding_box: {
                            x: Math.round(rect.x),
                            y: Math.round(rect.y),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                          },
                        };
                      });
                    }
                    """,
                    {
                        'maxElements': max_elements_per_frame,
                        'priorityKeywords': keywords,
                    },
                )
                frame_state['elements'] = elements
                frame_state['element_count'] = len(elements)
            except Exception as exc:
                frame_state['error'] = str(exc)[:500]
            frames.append(frame_state)
        return frames

    def _build_mcp_test_plan(self, args: dict, element_map: dict) -> dict:
        requirement_document = self._structured_requirement_document(args)
        provided_generated_case = self._structured_generated_case(args)
        provided_test_plan = self._structured_test_plan(args)
        structured_steps = provided_generated_case.get('steps') if isinstance(provided_generated_case.get('steps'), list) else None
        if structured_steps is None:
            structured_steps = provided_test_plan.get('steps') if isinstance(provided_test_plan.get('steps'), list) else None
        if structured_steps:
            steps = [
                step for step in structured_steps
                if isinstance(step, dict)
            ]
            steps = copy.deepcopy(steps)
            for index, step in enumerate(steps):
                step['step_sort'] = step.get('step_sort', index)
            objective = (
                provided_test_plan.get('objective')
                or provided_generated_case.get('description')
                or provided_generated_case.get('name')
                or requirement_document.get('source_requirement')
                or requirement_document.get('normalized_text')
                or requirement_document.get('target_module')
                or args.get('target_module')
                or ''
            )
            return {
                'objective': objective,
                'source_type': args.get('source_type') or 'structured_contract',
                'target_url': args.get('target_url') or element_map.get('base_url') or '',
                'preconditions': provided_test_plan.get('preconditions') or provided_generated_case.get('preconditions') or [],
                'test_data': provided_test_plan.get('test_data') or provided_generated_case.get('test_data') or {},
                'steps': steps,
                'expected_results': provided_test_plan.get('expected_results') or provided_generated_case.get('expected_results') or [],
                'cleanup': provided_test_plan.get('cleanup') or provided_generated_case.get('cleanup') or [],
            }

        requirement = self._structured_requirement_corpus(args)
        target_page = self._select_task_relevant_page(args, element_map)
        target_elements = self._select_task_relevant_elements(args, target_page, limit=30)
        page_key = target_page.get('page_key') or 'page_1'
        target_url = target_page.get('url') or element_map.get('base_url') or args.get('target_url') or ''
        safety_policy = args.get('safety_policy') or {}
        target_is_form_state = self._is_business_form_covered(target_page, safety_policy)

        if target_is_form_state and any(
            item.get('in_dialog') or item.get('dialog_name')
            for item in target_elements
            if isinstance(item, dict)
        ):
            target_elements = [
                item for item in target_elements
                if item.get('in_dialog') or item.get('dialog_name')
            ]

        def field_order(element: dict[str, Any]) -> tuple[int, str]:
            text = self._element_visible_text(element)
            if any(keyword in text for keyword in ['保存', '提交', '确定', '确认']):
                return (90, text)
            if element.get('required'):
                return (10, text)
            if any(action in (element.get('actions') or []) for action in ['fill', 'select_option', 'check']):
                return (30, text)
            return (60, text)

        def is_confirmation(element: dict[str, Any]) -> bool:
            role = str(element.get('role') or '').lower()
            tag = str(element.get('tag') or '').lower()
            if role and role not in {'button', 'link', 'menuitem'} and tag != 'button':
                return False
            return (
                'click' in (element.get('actions') or [])
                and any(keyword in self._element_visible_text(element) for keyword in ['保存', '提交', '确定', '确认'])
            )

        def is_form_control(element: dict[str, Any]) -> bool:
            actions = element.get('actions') or []
            if self._looks_like_select_control(element):
                return True
            return any(action in actions for action in ['fill', 'select_option', 'check', 'uncheck'])

        requirement_text = self._clean_visible_text(
            self._structured_requirement_corpus(args)
        )
        requested_field_names = [
            keyword for keyword in self._extract_business_keywords(args)
            if keyword and keyword in requirement_text
        ]
        if target_is_form_state and requested_field_names:
            target_elements = [
                element for element in target_elements
                if (
                    str(element.get('role') or '').lower() == 'dialog'
                    or is_confirmation(element)
                    or any(keyword in self._element_visible_text(element) for keyword in requested_field_names)
                )
            ]

        form_elements = [
            element for element in target_elements
            if is_form_control(element)
            and not is_confirmation(element)
            and isinstance(element.get('recommended_locator'), dict)
        ]
        confirmation_elements = [
            element for element in target_elements
            if is_confirmation(element) and isinstance(element.get('recommended_locator'), dict)
        ]
        assert_elements = [
            element for element in target_elements
            if element not in form_elements
            and element not in confirmation_elements
            and isinstance(element.get('recommended_locator'), dict)
        ]
        target_elements = [
            *sorted(form_elements, key=field_order)[:12],
            *sorted(assert_elements, key=field_order)[:4],
            *sorted(confirmation_elements, key=field_order)[:2],
        ]
        steps = [
            {
                'step_sort': 0,
                'action': 'navigate',
                'operation': 'goto',
                'description': f"打开目标页面：{target_url}",
                'page_key': page_key,
                'element_key': '',
                'target_name': '',
                'locator_hint': {},
                'value': target_url,
                'expected': '页面加载完成',
                'requires_confirmation': False,
                'risk_level': 'low',
                'fallback_generated': True,
            },
        ]
        steps.extend(self._build_state_transition_steps(args, element_map, target_page, len(steps)))
        for element in target_elements:
            actions = element.get('actions') or []
            name = element.get('name') or element.get('accessible_name') or element.get('label') or element.get('placeholder') or element.get('element_key')
            safe_name = self._clean_visible_text(name)
            locator = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {}
            if not locator:
                continue
            base_step = {
                'step_sort': len(steps),
                'page_key': element.get('page_key') or page_key,
                'element_key': element.get('element_key') or '',
                'target_name': safe_name,
                'locator_hint': locator,
                'requires_confirmation': False,
                'risk_level': 'low',
                'confidence': 0.72,
                'fallback_generated': True,
            }
            if 'fill' in actions:
                if self._looks_like_select_control(element):
                    steps.append({
                        **base_step,
                        'action': 'select',
                        'operation': 'select_option',
                        'description': f'选择字段：{safe_name}',
                        'value': self._default_script_option_for_element(element),
                        'expected': '字段已选择',
                    })
                else:
                    steps.append({
                        **base_step,
                        'action': 'fill',
                        'operation': 'fill',
                        'description': f'填写字段：{safe_name}',
                        'value': self._default_script_value_for_element(element),
                        'expected': '字段已填写',
                    })
            elif 'select_option' in actions:
                steps.append({
                    **base_step,
                    'action': 'select',
                    'operation': 'select_option',
                    'description': f'选择字段：{safe_name}',
                    'value': self._default_script_option_for_element(element),
                    'expected': '字段已选择',
                })
            elif is_confirmation(element):
                requirement_text = self._clean_visible_text(
                    ' '.join(str(args.get(key) or '') for key in ['requirement', 'gherkin', 'target_module'])
                )
                low_risk_create_submit = (
                    any(keyword in requirement_text for keyword in ['新建', '新增', '添加', '创建'])
                    and any(keyword in requirement_text for keyword in ['保存', '提交', '确定', '确认'])
                    and not self._looks_dangerous(requirement_text, safety_policy)
                )
                allow_submit = bool(safety_policy.get('allow_form_submit', True) or low_risk_create_submit)
                steps.append({
                    **base_step,
                    'action': 'click' if allow_submit else 'assert',
                    'operation': 'click' if allow_submit else 'assert_visible',
                    'description': f"{'点击保存入口' if allow_submit else '验证确认入口可见'}：{safe_name}",
                    'value': '',
                    'expected': '表单保存动作已触发' if allow_submit else '确认入口可见',
                    'requires_confirmation': False,
                    'risk_level': 'low' if allow_submit else 'medium',
                })
            else:
                steps.append({
                    **base_step,
                    'action': 'assert',
                    'operation': 'assert_visible',
                    'description': f'验证元素可见：{safe_name}',
                    'value': '',
                    'expected': '元素可见',
                })
        steps = self._ensure_state_transition_steps_for_plan(args, element_map, steps)
        return {
            'objective': requirement,
            'source_type': args.get('source_type') or 'natural_language',
            'target_url': args.get('target_url') or element_map.get('base_url') or '',
            'preconditions': ['使用目标环境配置和测试账号', '仅在安全边界允许的 URL 范围内执行'],
            'test_data': {'strategy': '优先使用测试数据或公共变量，不在生产数据上提交破坏性操作'},
            'steps': steps,
            'expected_results': ['页面可访问', '关键可交互元素可定位', '生成脚本可在真实浏览器中完成基础验证'],
            'cleanup': ['第一版不执行破坏性提交，无需清理业务数据'],
        }

    def _build_state_transition_steps(
        self,
        args: dict,
        element_map: dict,
        target_page: dict[str, Any],
        start_sort: int,
    ) -> list[dict[str, Any]]:
        safety_policy = args.get('safety_policy') or {}
        if not self._is_business_form_covered(target_page, safety_policy):
            return []

        page_key = str(target_page.get('page_key') or '')
        target_url = str(target_page.get('url') or '')
        transitions = [
            item for item in (element_map.get('state_transitions') or [])
            if isinstance(item, dict) and isinstance(item.get('locator_hint'), dict) and item.get('locator_hint', {}).get('type')
        ]
        candidates = []
        for transition in transitions:
            score = 0
            if page_key and str(transition.get('to_page_key') or '') == page_key:
                score += 200
            if target_url and str(transition.get('to_url') or '') == target_url:
                score += 80
            label = self._clean_visible_text(transition.get('target_name') or '')
            if any(keyword in label for keyword in ['新建', '新增', '添加', '创建', '打开']):
                score += 120
            if transition.get('stage') == 'open_business_form':
                score += 80
            if score > 0:
                candidates.append((score, transition))
        if not candidates:
            for element in target_page.get('elements') or []:
                if not isinstance(element, dict):
                    continue
                label = self._element_visible_text(element)
                locator = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {}
                if (
                    'click' in (element.get('actions') or [])
                    and not element.get('in_dialog')
                    and not element.get('dialog_name')
                    and any(keyword in label for keyword in ['新建', '新增', '添加', '创建'])
                    and locator.get('type')
                ):
                    candidates.append((
                        120,
                        {
                            'operation': 'click',
                            'stage': 'inferred_open_business_form',
                            'from_page_key': target_page.get('page_key') or '',
                            'from_url': target_page.get('url') or '',
                            'to_page_key': page_key,
                            'to_url': target_url,
                            'element_key': element.get('element_key') or '',
                            'target_name': label,
                            'locator_hint': locator,
                        },
                    ))
            if not candidates:
                return []

        candidates.sort(key=lambda item: item[0], reverse=True)
        transition = candidates[0][1]
        label = self._clean_visible_text(transition.get('target_name') or '打开业务表单')
        steps = [{
            'step_sort': start_sort,
            'action': 'click',
            'operation': 'click',
            'description': f'打开业务表单：{label}',
            'page_key': transition.get('from_page_key') or page_key,
            'element_key': transition.get('element_key') or '',
            'target_name': label,
            'locator_hint': transition.get('locator_hint') or {},
            'value': '',
            'expected': '业务表单或弹窗已打开',
            'requires_confirmation': False,
            'risk_level': 'low',
            'confidence': 0.82,
            'fallback_generated': False,
            'state_transition': {
                'to_page_key': transition.get('to_page_key') or '',
                'to_url': transition.get('to_url') or '',
            },
        }]

        dialog = next((
            item for item in target_page.get('elements') or []
            if isinstance(item, dict)
            and str(item.get('role') or '').lower() == 'dialog'
            and isinstance(item.get('recommended_locator'), dict)
        ), None)
        if dialog:
            steps.append({
                'step_sort': start_sort + len(steps),
                'action': 'assert',
                'operation': 'assert_visible',
                'description': f"验证弹窗可见：{dialog.get('name') or dialog.get('accessible_name') or dialog.get('element_key')}",
                'page_key': target_page.get('page_key') or page_key,
                'element_key': dialog.get('element_key') or '',
                'target_name': dialog.get('name') or dialog.get('accessible_name') or dialog.get('element_key') or '',
                'locator_hint': dialog.get('recommended_locator') or {},
                'value': '',
                'expected': '弹窗已打开并可见',
                'requires_confirmation': False,
                'risk_level': 'low',
                'confidence': 0.80,
                'fallback_generated': False,
            })
        return steps

    def _renumber_ai_plan_steps(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for index, step in enumerate(steps):
            if isinstance(step, dict):
                step['step_sort'] = index
        return steps

    def _ai_plan_click_identity(self, step: dict[str, Any]) -> tuple[str, str, str, str]:
        locator = self._plan_step_locator(step)
        return (
            self._clean_visible_text(step.get('element_key') or ''),
            self._clean_visible_text(locator.get('type') or ''),
            self._clean_visible_text(locator.get('value') or ''),
            self._clean_visible_text(locator.get('name') or step.get('target_name') or ''),
        )

    def _ai_plan_equivalent_click(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        if self._normalise_ai_plan_operation(left) != 'click' or self._normalise_ai_plan_operation(right) != 'click':
            return False
        left_key, left_type, left_value, left_name = self._ai_plan_click_identity(left)
        right_key, right_type, right_value, right_name = self._ai_plan_click_identity(right)
        if left_key and right_key and left_key == right_key:
            return True
        return bool(
            left_type
            and left_type == right_type
            and left_value == right_value
            and (not left_name or not right_name or left_name == right_name or left_name in right_name or right_name in left_name)
        )

    def _state_transition_step_from_record(self, transition: dict[str, Any], step_sort: int = 0) -> dict[str, Any]:
        label = self._clean_visible_text(transition.get('target_name') or '打开业务表单')
        return {
            'step_sort': step_sort,
            'action': 'click',
            'operation': 'click',
            'description': f'打开业务表单：{label}',
            'page_key': transition.get('from_page_key') or '',
            'element_key': transition.get('element_key') or '',
            'target_name': label,
            'locator_hint': transition.get('locator_hint') or {},
            'value': '',
            'expected': '业务表单或弹窗已打开',
            'requires_confirmation': False,
            'risk_level': 'low',
            'confidence': 0.82,
            'fallback_generated': False,
            'state_transition': {
                'to_page_key': transition.get('to_page_key') or '',
                'to_url': transition.get('to_url') or '',
            },
        }

    def _normalise_state_transition_step_order(
        self,
        steps: list[dict[str, Any]],
        element_map: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """按 state_transition 依赖重排迁移步骤，避免业务页点击排到登录前。"""
        if not isinstance(steps, list) or not steps:
            return steps

        page_rank: dict[str, int] = {}
        if isinstance(element_map, dict):
            for index, page_state in enumerate(element_map.get('pages') or []):
                if isinstance(page_state, dict) and page_state.get('page_key'):
                    page_rank[str(page_state.get('page_key'))] = index + 1

        def rank_for_page(page_key: str) -> int:
            if not page_key:
                return 0
            if page_key in page_rank:
                return page_rank[page_key]
            match = re.search(r'page_(\d+)', page_key)
            if match:
                return int(match.group(1))
            return 10_000

        def rank_for_step(step: Any) -> int:
            if not isinstance(step, dict):
                return 10_000
            transition = step.get('state_transition') if isinstance(step.get('state_transition'), dict) else {}
            if transition.get('to_page_key'):
                return rank_for_page(str(transition.get('to_page_key') or ''))
            return rank_for_page(str(step.get('page_key') or ''))

        existing_transitions: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for step in steps:
            if (
                isinstance(step, dict)
                and isinstance(step.get('state_transition'), dict)
                and step.get('state_transition', {}).get('to_page_key')
            ):
                existing_transitions.append(copy.deepcopy(step))
            else:
                if (
                    isinstance(step, dict)
                    and self._normalise_ai_plan_operation(step) == 'click'
                    and any(marker in self._clean_visible_text(step.get('description') or '') for marker in ['状态迁移', '打开业务表单'])
                ):
                    continue
                remaining.append(copy.deepcopy(step) if isinstance(step, dict) else step)

        needed_rank = 0
        for step in remaining:
            if isinstance(step, dict):
                needed_rank = max(needed_rank, rank_for_page(str(step.get('page_key') or '')))

        transitions: list[dict[str, Any]] = []
        if isinstance(element_map, dict):
            for transition in element_map.get('state_transitions') or []:
                if not isinstance(transition, dict) or not isinstance(transition.get('locator_hint'), dict):
                    continue
                to_page_key = str(transition.get('to_page_key') or '')
                if to_page_key and rank_for_page(to_page_key) <= needed_rank:
                    transitions.append(self._state_transition_step_from_record(transition))
        if not transitions:
            transitions = existing_transitions

        remaining = sorted(
            enumerate(remaining),
            key=lambda item: (
                rank_for_step(item[1]),
                item[0],
            ),
        )
        remaining = [item for _, item in remaining]

        for transition_step in transitions:
            to_page_key = str(transition_step.get('state_transition', {}).get('to_page_key') or '')
            to_rank = rank_for_page(to_page_key)
            target_index: int | None = None
            for index, step in enumerate(remaining):
                if not isinstance(step, dict):
                    continue
                step_rank = rank_for_step(step)
                if step_rank < to_rank or self._normalise_ai_plan_operation(step) in {'goto', 'wait', 'observe'}:
                    continue
                target_index = index
                break

            if target_index is None:
                remaining.append(transition_step)
                continue

            if any(
                isinstance(step, dict) and self._ai_plan_equivalent_click(transition_step, step)
                for step in remaining[:target_index]
            ):
                continue

            insert_at = target_index
            remaining.insert(insert_at, transition_step)

        return self._renumber_ai_plan_steps(remaining)

    def _ensure_state_transition_steps_for_plan(
        self,
        args: dict,
        element_map: dict,
        steps: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """确保业务表单字段操作前包含打开表单/弹窗的状态迁移步骤。"""
        if not isinstance(element_map, dict) or not isinstance(steps, list) or not steps:
            return steps

        safety_policy = args.get('safety_policy') or {}
        target_page = self._select_task_relevant_page(args, element_map)
        if not target_page or not self._is_business_form_covered(target_page, safety_policy):
            return self._normalise_state_transition_step_order(steps, element_map)

        transition_steps = self._build_state_transition_steps(args, element_map, target_page, 0)
        if not transition_steps:
            return self._normalise_state_transition_step_order(steps, element_map)

        target_page_key = str(target_page.get('page_key') or '')
        dialog_or_form_element_keys = {
            str(element.get('element_key') or '')
            for element in target_page.get('elements') or []
            if isinstance(element, dict)
            and (
                element.get('in_dialog')
                or element.get('dialog_name')
                or str(element.get('role') or '').lower() == 'dialog'
            )
            and element.get('element_key')
        }

        first_form_step_index: int | None = None
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            operation = self._normalise_ai_plan_operation(step)
            element_key = str(step.get('element_key') or '')
            step_page_key = str(step.get('page_key') or '')
            is_target_business_page_step = bool(target_page_key and step_page_key == target_page_key)
            if (
                element_key in dialog_or_form_element_keys
                or (is_target_business_page_step and operation not in {'goto', 'wait', 'observe'})
            ):
                first_form_step_index = index
                break

        if first_form_step_index is None:
            return self._renumber_ai_plan_steps(steps)

        transition_element_keys = {
            str(step.get('element_key') or '')
            for step in transition_steps
            if isinstance(step, dict) and step.get('element_key')
        }
        for step in steps[:first_form_step_index]:
            if not isinstance(step, dict):
                continue
            description = self._clean_visible_text(step.get('description') or step.get('target_name') or '')
            operation = self._normalise_ai_plan_operation(step)
            if step.get('state_transition') or str(step.get('element_key') or '') in transition_element_keys:
                return self._normalise_state_transition_step_order(steps, element_map)
            if operation == 'click' and any(keyword in description for keyword in ['打开业务表单', '新建', '新增', '添加', '创建']):
                return self._normalise_state_transition_step_order(steps, element_map)

        patched_steps = [copy.deepcopy(step) for step in steps]
        patched_transitions = [copy.deepcopy(step) for step in transition_steps]
        insert_at = first_form_step_index
        while insert_at > 0 and self._normalise_ai_plan_operation(patched_steps[insert_at - 1]) in {'wait', 'observe'}:
            insert_at -= 1
        patched_steps[insert_at:insert_at] = patched_transitions
        return self._normalise_state_transition_step_order(patched_steps, element_map)

    def _select_task_relevant_page(self, args: dict, element_map: dict) -> dict[str, Any]:
        pages = [page for page in (element_map.get('pages') or []) if isinstance(page, dict)]
        if not pages:
            return {}
        keywords = self._extract_business_keywords(args)
        safety_policy = args.get('safety_policy') or {}

        def page_score(page_state: dict[str, Any]) -> int:
            elements = self._exploration_elements(page_state)
            text = self._clean_visible_text(' '.join(self._element_visible_text(element) for element in elements[:220]))
            score = 0
            for keyword in keywords:
                normalized = self._clean_visible_text(keyword)
                if normalized and normalized in text:
                    score += 100 + min(len(normalized), 30)
            if self._is_business_form_covered(page_state, safety_policy):
                score += 400
            if any(element.get('required') for element in elements):
                score += 160
            if any(
                ('fill' in (element.get('actions') or []) or 'select_option' in (element.get('actions') or []))
                and any(token in self._element_visible_text(element) for token in ['任务', '名称', '环境', '报告', '方式'])
                for element in elements
            ):
                score += 180
            if any(
                'click' in (element.get('actions') or [])
                and any(token in self._element_visible_text(element) for token in ['保存', '提交', '确定', '确认'])
                for element in elements
            ):
                score += 160
            if self._looks_like_login_page(page_state):
                score -= 1000
            return score

        return max(pages, key=page_score)

    def _element_script_priority(self, args: dict, element: dict[str, Any]) -> int:
        keywords = self._extract_business_keywords(args)
        actions = element.get('actions') or []
        text = self._element_visible_text(element)
        score = 0
        if element.get('required'):
            score += 260
        if 'fill' in actions or 'select_option' in actions:
            score += 240
        if 'click' in actions and any(keyword in text for keyword in ['保存', '提交', '确定', '确认']):
            score += 220
        if any(action in actions for action in ['fill', 'select_option', 'check']) and (
            element.get('label') or element.get('form_label') or element.get('placeholder')
        ):
            score += 130
        for keyword in keywords:
            normalized = self._clean_visible_text(keyword)
            if normalized and (normalized in text or text in normalized):
                score += 120 + min(len(normalized), 20)
        locator = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {}
        if locator.get('type') in {'label', 'placeholder', 'test_id', 'role'}:
            score += 35
        return score

    def _select_task_relevant_elements(self, args: dict, page_state: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
        elements = self._exploration_elements(page_state)
        ranked = [
            (self._element_script_priority(args, element), index, element)
            for index, element in enumerate(elements)
            if isinstance(element.get('recommended_locator'), dict)
        ]
        ranked = [item for item in ranked if item[0] > 0]
        ranked.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected = []
        seen = set()
        for _, _, element in ranked:
            key = element.get('element_key') or self._element_visible_text(element)
            if key in seen:
                continue
            seen.add(key)
            selected.append(element)
            if len(selected) >= limit:
                break
        return selected

    def _looks_like_select_control(self, element: dict[str, Any]) -> bool:
        actions = element.get('actions') or []
        if 'select_option' in actions:
            return True
        if element.get('tag') == 'select':
            return True
        if element.get('role') in {'combobox', 'select'}:
            return True
        if element.get('tag') == 'textarea':
            return False
        if element.get('input_type') in {'textarea', 'email', 'password', 'text'}:
            text = self._element_visible_text(element)
            return any(marker in text for marker in ['请选择', '选择日期时间', '下拉选择'])
        text = self._element_visible_text(element)
        if any(marker in text for marker in ['请选择', '下拉选择']):
            return True
        return False

    def _build_mcp_script(self, args: dict, element_map: dict, test_plan: dict | None = None) -> str:
        base_url = element_map.get('base_url') or args.get('target_url') or ''
        if not isinstance(test_plan, dict):
            test_plan = self._build_mcp_test_plan(args, element_map)
        statements = []
        has_locator_step = False
        for step in test_plan.get('steps') or []:
            if not isinstance(step, dict):
                continue
            operation = self._normalise_ai_plan_operation(step)
            description = self._clean_visible_text(step.get('description') or step.get('target_name') or operation)
            if operation == 'goto':
                url = step.get('value') or base_url
                statements.append(f"    # {description}")
                statements.append(f"    await page.goto({json.dumps(url, ensure_ascii=False)}, wait_until='networkidle')")
                continue
            if operation == 'wait':
                try:
                    seconds = max(0, min(float(step.get('value') or 1), 30))
                except (TypeError, ValueError):
                    seconds = 1
                statements.append(f"    # {description}")
                statements.append(f"    await page.wait_for_timeout({int(seconds * 1000)})")
                continue

            locator = self._plan_step_locator(step)
            if not locator:
                continue
            has_locator_step = True
            expression = self._locator_expression(locator, operation)
            locator_expr = f"step_locator_{len(statements)}"
            value = str(step.get('value') or '')
            statements.append(f"    # {description}")
            statements.append(f"    {locator_expr} = await first_visible({expression})")
            if operation == 'fill':
                statements.append(f"    await expect({locator_expr}).to_be_visible(timeout=5000)")
                statements.append(f"    await {locator_expr}.fill({json.dumps(value, ensure_ascii=False)})")
            elif operation == 'click':
                statements.append(f"    await expect({locator_expr}).to_be_visible(timeout=5000)")
                statements.append(f"    await {locator_expr}.click()")
                statements.append("    try:")
                statements.append("        await page.wait_for_load_state('networkidle', timeout=6000)")
                statements.append("    except Exception:")
                statements.append("        await page.wait_for_timeout(800)")
            elif operation == 'select_option':
                statements.append(f"    await expect({locator_expr}).to_be_visible(timeout=5000)")
                if value:
                    statements.append("    try:")
                    statements.append(f"        await {locator_expr}.select_option({json.dumps(value, ensure_ascii=False)})")
                    statements.append("    except Exception:")
                    statements.append(f"        await {locator_expr}.click()")
                    statements.append("        await page.keyboard.press('ArrowDown')")
                    statements.append("        await page.keyboard.press('Enter')")
                else:
                    statements.append(f"    await {locator_expr}.click()")
                    statements.append("    await page.keyboard.press('ArrowDown')")
                    statements.append("    await page.keyboard.press('Enter')")
            elif operation == 'assert_text':
                statements.append(f"    await expect({locator_expr}).to_have_text({json.dumps(value, ensure_ascii=False)}, timeout=5000)")
            elif operation == 'assert_contain_text':
                statements.append(f"    await expect({locator_expr}).to_contain_text({json.dumps(value, ensure_ascii=False)}, timeout=5000)")
            elif operation == 'assert_value':
                statements.append(f"    await expect({locator_expr}).to_have_value({json.dumps(value, ensure_ascii=False)}, timeout=5000)")
            else:
                statements.append(f"    await expect({locator_expr}).to_be_visible(timeout=5000)")
        if statements and not has_locator_step:
            statements.append("    await expect(page.locator('body')).to_be_visible(timeout=5000)")
        statements_text = '\n'.join(statements) if statements else "    await expect(page.locator('body')).to_be_visible(timeout=5000)"
        return (
            "import asyncio\n"
            "import time\n\n"
            "from playwright.async_api import expect\n\n"
            "async def first_visible(locator, timeout=5000, prefer='first'):\n"
            "    deadline = time.monotonic() + max(timeout, 0) / 1000\n"
            "    last_count = 0\n"
            "    while True:\n"
            "        last_count = await locator.count()\n"
            "        indexes = list(range(min(last_count, 80)))\n"
            "        if prefer == 'last':\n"
            "            indexes.reverse()\n"
            "        for index in indexes:\n"
            "            candidate = locator.nth(index)\n"
            "            try:\n"
            "                if await candidate.is_visible(timeout=200):\n"
            "                    return candidate\n"
            "            except Exception:\n"
            "                continue\n"
            "        if time.monotonic() >= deadline:\n"
            "            break\n"
            "        await asyncio.sleep(0.15)\n"
            "    raise TimeoutError(f'Timeout waiting for visible locator match: matched={last_count}')\n\n"
            "async def test_generated_flow(page):\n"
            f"{statements_text}\n"
        )

    @staticmethod
    def _default_script_option_for_element(element: dict[str, Any]) -> str:
        for option in element.get('options') or []:
            if not isinstance(option, dict) or option.get('disabled'):
                continue
            label = str(option.get('label') or '').strip()
            value = str(option.get('value') or '').strip()
            if label and (label == '请选择' or label.startswith('请选择')):
                continue
            if value:
                return value
            if label:
                return label
        return ''

    @staticmethod
    def _default_script_value_for_element(element: dict[str, Any]) -> str:
        text = ' '.join(str(element.get(key) or '') for key in ['name', 'accessible_name', 'label', 'placeholder']).lower()
        if any(word in text for word in ['手机', '电话', 'mobile', 'phone']):
            return '13800138000'
        if any(word in text for word in ['邮箱', 'email', 'mail']):
            return 'test@example.com'
        if any(word in text for word in ['数量', '金额', '价格', 'number', 'amount', 'price']):
            return '1'
        if any(word in text for word in ['名称', '标题', '任务', 'name', 'title']):
            return '自动化测试数据'
        return '测试数据'

    def _official_mcp_adapter(self) -> PlaywrightMcpAdapter:
        if self.mcp_transport in {'http', 'sse', 'streamable-http'}:
            return PlaywrightMcpHttpAdapter(
                server_url=self.mcp_server_url or 'http://127.0.0.1:8931/mcp',
                command=self.mcp_command,
                args=self.mcp_args,
                cwd=self.mcp_cwd,
                request_timeout=max(60.0, self.action_timeout / 1000 * 2),
                auto_start=True,
            )
        if self.mcp_transport != 'stdio':
            raise McpProtocolError(f'不支持的官方 Playwright MCP transport: {self.mcp_transport}')
        return PlaywrightMcpAdapter(
            command=self.mcp_command,
            args=self.mcp_args,
            cwd=self.mcp_cwd,
            request_timeout=max(60.0, self.action_timeout / 1000 * 2),
        )

    async def _official_mcp_collect_state(
        self,
        adapter: PlaywrightMcpAdapter,
        page_key: str,
        fallback_url: str = '',
    ) -> dict[str, Any]:
        snapshot = await adapter.snapshot()
        snapshot_text = adapter._content_text(snapshot)
        if adapter.is_snapshot_error_text(snapshot_text):
            raise McpProtocolError(f'官方 Playwright MCP snapshot 失败: {snapshot_text[:1000]}')
        elements = adapter.parse_snapshot_elements(snapshot_text)
        if not elements and fallback_url and fallback_url not in snapshot_text:
            await adapter.navigate(fallback_url)
            await asyncio.sleep(1)
            snapshot = await adapter.snapshot()
            snapshot_text = adapter._content_text(snapshot)
            if adapter.is_snapshot_error_text(snapshot_text):
                raise McpProtocolError(f'官方 Playwright MCP snapshot 失败: {snapshot_text[:1000]}')
            elements = adapter.parse_snapshot_elements(snapshot_text)
        url_match = re.search(r'- Page URL:\s*(\S+)', snapshot_text)
        current_url = url_match.group(1).strip() if url_match else fallback_url
        for index, element in enumerate(elements):
            if not isinstance(element, dict):
                continue
            som_index = index + 1
            element.setdefault('som_index', som_index)
            element.setdefault('som_label', str(som_index))
            if element.get('bounding_box') and not element.get('screenshot_region'):
                element['screenshot_region'] = {
                    **element['bounding_box'],
                    'som_label': str(som_index),
                }
        network_summary = {}
        console_messages = []
        try:
            network_result = await adapter.network_requests()
            network_summary = {
                'raw': adapter._content_text(network_result),
                'tool': adapter.tool_aliases.get('network'),
            }
        except Exception as exc:
            network_summary = {'error': str(exc)[:500], 'tool': adapter.tool_aliases.get('network')}
        try:
            console_result = await adapter.console_messages()
            console_messages = [adapter._content_text(console_result)]
        except Exception as exc:
            console_messages = [f'console tool unavailable: {str(exc)[:300]}']

        state = {
            'page_key': page_key,
            'name': page_key,
            'url': current_url,
            'title': '',
            'source': 'official_playwright_mcp_snapshot',
            'snapshot': {
                'text': snapshot_text,
                'refs': [
                    {
                        'ref': item.get('mcp_ref'),
                        'role': item.get('role'),
                        'name': item.get('name'),
                        'element_key': item.get('element_key'),
                    }
                    for item in elements
                ],
            },
            'elements': elements,
            'network_summary': network_summary,
            'console_messages': console_messages,
            'component_context': {'source': 'official_playwright_mcp'},
            'visual_som': {
                'enabled': True,
                'label_strategy': 'mcp_ref + som_index',
                'element_count': len(elements),
                'note': '官方 MCP snapshot refs 已映射 som_index/som_label，若 snapshot 含 bounding box 则写入 screenshot_region',
            },
            'virtual_lists': {
                'container_count': 0,
                'containers': [],
                'sampling_strategy': 'official MCP snapshot only; native Playwright collector can provide scroll container sampling',
            },
            'supplemental_collectors': {
                'scrapling': {
                    'enabled': False,
                    'status': 'not_configured',
                    'note': '已预留 Scrapling 补充采集入口',
                }
            },
            'frames': [],
            'shadow_dom': {'elements': [], 'source': 'official_playwright_mcp_snapshot'},
        }
        return state

    async def _official_mcp_auto_login_if_possible(
        self,
        adapter: PlaywrightMcpAdapter,
        state: dict[str, Any],
        args: dict,
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        credentials = self._extract_login_credentials(args)
        if not credentials.get('username') or not credentials.get('password'):
            observations.append({
                'type': 'official_mcp_login_skipped',
                'reason': '未配置登录账号或密码',
            })
            return state

        elements = [item for item in state.get('elements') or [] if isinstance(item, dict)]
        password_element = None
        username_element = None
        login_element = None
        for element in elements:
            label = self._element_visible_text(element).lower()
            role = str(element.get('role') or '').lower()
            if not password_element and ('密码' in label or 'password' in label or 'pwd' in label):
                password_element = element
            if not username_element and role in {'textbox', 'input', 'searchbox'} and any(
                keyword in label for keyword in ['用户名', '账号', '账户', '手机', '邮箱', 'user', 'account', 'email', 'phone']
            ):
                username_element = element
            if not login_element and 'click' in (element.get('actions') or []) and any(
                keyword in label for keyword in ['登录', '登陆', 'login', 'sign in']
            ):
                login_element = element
        if not username_element:
            username_element = next((item for item in elements if 'fill' in (item.get('actions') or [])), None)
        if not password_element:
            fillables = [item for item in elements if 'fill' in (item.get('actions') or [])]
            if len(fillables) >= 2:
                password_element = fillables[1]
        if not login_element:
            login_element = next((item for item in elements if 'click' in (item.get('actions') or [])), None)

        if not username_element or not password_element or not login_element:
            observations.append({
                'type': 'official_mcp_login_skipped',
                'reason': 'snapshot 中未识别到完整登录控件',
            })
            return state

        if 'run_code' in adapter.tool_aliases:
            username_name = username_element.get('name') or username_element.get('accessible_name') or 'username'
            password_name = password_element.get('name') or password_element.get('accessible_name') or 'password'
            login_name = login_element.get('name') or login_element.get('accessible_name') or '登录'
            login_code = f"""async (page) => {{
  const username = {json.dumps(credentials['username'], ensure_ascii=False)};
  const password = {json.dumps(credentials['password'], ensure_ascii=False)};
  await page.getByPlaceholder({json.dumps(username_name, ensure_ascii=False)}).first().fill(username);
  await page.getByPlaceholder({json.dumps(password_name, ensure_ascii=False)}).first().fill(password);
  await Promise.allSettled([
    page.waitForLoadState('networkidle', {{ timeout: 8000 }}),
    page.getByRole('button', {{ name: {json.dumps(login_name, ensure_ascii=False)} }}).first().click()
  ]);
  await page.waitForTimeout(1500);
  return {{ url: page.url(), title: await page.title() }};
}}"""
            await adapter.run_code(login_code)
        else:
            await adapter.type_text(username_element['mcp_ref'], credentials['username'], username_element.get('name') or 'username')
            await adapter.type_text(password_element['mcp_ref'], credentials['password'], password_element.get('name') or 'password')
            await adapter.click(login_element['mcp_ref'], login_element.get('name') or 'login')
        observations.append({
            'type': 'official_mcp_auto_login',
            'username_element': username_element.get('name'),
            'password_element': password_element.get('name'),
            'login_element': login_element.get('name'),
            'tool': adapter.tool_aliases.get('run_code') or 'ref_tools',
        })
        last_state = state
        for attempt in range(1, 9):
            await asyncio.sleep(1)
            last_state = await self._official_mcp_collect_state(adapter, 'page_after_login', '')
            if not self._looks_like_login_page(last_state) and (last_state.get('elements') or []):
                observations.append({
                    'type': 'official_mcp_login_success',
                    'attempt': attempt,
                    'url': last_state.get('url') or '',
                    'element_count': len(last_state.get('elements') or []),
                })
                return last_state
        observations.append({
            'type': 'official_mcp_login_failed',
            'reason': '点击登录后仍停留在登录页或未采集到登录后元素',
            'url': last_state.get('url') or '',
            'element_count': len(last_state.get('elements') or []),
        })
        return last_state

    async def _official_mcp_explore_pages(
        self,
        adapter: PlaywrightMcpAdapter,
        first_state: dict[str, Any],
        args: dict,
        safety_policy: dict,
        base_url: str,
        observations: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pages = []
        state_transitions: list[dict[str, Any]] = []
        seen_signatures: set[str] = set()
        self._append_unique_page_state(pages, seen_signatures, first_state)
        authentication_mode = self._authentication_mode(args, first_state)
        observations.append({
            'type': 'authentication_strategy',
            'mode': authentication_mode,
            'entry_page_key': first_state.get('page_key') or '',
            'entry_url': first_state.get('url') or base_url,
        })
        if authentication_mode == 'test_subject':
            observations.append({
                'type': 'business_exploration_coverage',
                'status': 'deferred',
                'planning_allowed': bool(first_state.get('elements')),
                'required_stages': ['authentication_subject'],
                'completed_stages': ['authentication_subject'],
                'missing_stages': [],
                'steps_used': 0,
                'reason': '认证是被测流程，登录页作为测试计划起点，不执行前置自动登录',
            })
            return pages, state_transitions
        keywords = self._extract_business_keywords(args)
        max_pages = int(safety_policy.get('max_pages') or 8)
        max_steps = int(safety_policy.get('max_steps') or 24)
        targets = self._build_exploration_targets(args)
        required_stages = [str(target.get('stage') or '') for target in targets]
        completed_stages: list[str] = []

        for exploration_target in targets:
            if len(pages) >= max_pages:
                break
            current_state = pages[-1]
            requires_form = self._target_requires_form_coverage(exploration_target)
            stage = str(exploration_target.get('stage') or '')
            target_index = targets.index(exploration_target)
            next_target = targets[target_index + 1] if target_index + 1 < len(targets) else None
            if requires_form and self._is_target_business_form_covered(current_state, exploration_target, safety_policy):
                completed_stages.append(stage)
                break
            ranked = []
            for element in current_state.get('elements') or []:
                score = self._rank_exploration_element_for_target(element, exploration_target, safety_policy)
                if score > 0 and element.get('mcp_ref'):
                    if self._is_requirement_route_stage(exploration_target) and not self._route_candidate_is_relevant(element, exploration_target, next_target):
                        continue
                    if self._target_element_matches_target(element, next_target):
                        score += 120
                    ranked.append((score, element))
            ranked.sort(key=lambda item: item[0], reverse=True)
            if not ranked:
                observations.append({
                    'type': 'official_mcp_explore_target_skipped',
                    'stage': exploration_target.get('stage'),
                    'reason': '当前 snapshot 中没有匹配阶段目标的安全入口',
                })
                continue
            score, target = ranked[0]
            label = self._element_visible_text(target)
            if self._looks_dangerous(label, safety_policy):
                observations.append({
                    'type': 'official_mcp_explore_skip_guardrail',
                    'element': label,
                })
                break
            try:
                await adapter.click(target['mcp_ref'], target.get('name') or label or target['mcp_ref'])
                await asyncio.sleep(1)
                next_state = await self._official_mcp_collect_state(
                    adapter,
                    f'page_{len(pages) + 1}',
                    base_url,
                )
                added = self._append_unique_page_state(pages, seen_signatures, next_state)
                if added:
                    state_transitions.append(
                        self._build_state_transition_record(
                            current_state,
                            next_state,
                            target,
                            exploration_target.get('stage') or '',
                            score,
                        )
                    )
                observations.append({
                    'type': 'official_mcp_explore_click',
                    'element': label,
                    'stage': stage,
                    'ref': target.get('mcp_ref'),
                    'added_page': added,
                    'element_count': len(next_state.get('elements') or []),
                })
                target_completed = (
                    self._is_target_business_form_covered(next_state, exploration_target, safety_policy)
                    if requires_form
                    else (
                        self._route_target_completed(
                            current_state,
                            next_state,
                            exploration_target,
                            next_target,
                            added,
                            self._target_element_matches_target(target, exploration_target),
                            self._target_element_matches_target(target, next_target),
                        )
                        if self._is_requirement_route_stage(exploration_target)
                        else self._non_form_target_completed(
                            current_state,
                            next_state,
                            exploration_target,
                            added,
                            self._target_element_matches_target(target, exploration_target),
                        )
                    )
                )
                if target_completed:
                    if stage not in completed_stages:
                        completed_stages.append(stage)
                    if (
                        self._is_requirement_route_stage(exploration_target)
                        and self._target_element_matches_target(target, next_target)
                        and isinstance(next_target, dict)
                    ):
                        next_stage = str(next_target.get('stage') or '')
                        if next_stage and next_stage not in completed_stages:
                            completed_stages.append(next_stage)
                    break
            except Exception as exc:
                observations.append({
                    'type': 'official_mcp_explore_error',
                    'element': label,
                    'ref': target.get('mcp_ref'),
                    'error': str(exc)[:500],
                })
                break

        for _ in range(max_steps):
            if len(pages) >= max_pages:
                break
            current_state = pages[-1]
            covered_target = next((
                target for target in targets
                if self._target_requires_form_coverage(target)
                and str(target.get('stage') or '') not in completed_stages
                and self._is_target_business_form_covered(current_state, target, safety_policy)
            ), None)
            if covered_target:
                completed_stages.append(str(covered_target.get('stage') or ''))
                break
            ranked = []
            for element in current_state.get('elements') or []:
                score = self._rank_exploration_element(element, keywords, safety_policy)
                if score > 0 and element.get('mcp_ref'):
                    ranked.append((score, element))
            ranked.sort(key=lambda item: item[0], reverse=True)
            if not ranked:
                break
            _, target = ranked[0]
            label = self._element_visible_text(target)
            if self._looks_dangerous(label, safety_policy) or self._requires_confirmation(label, safety_policy):
                observations.append({
                    'type': 'official_mcp_explore_skip_guardrail',
                    'element': label,
                })
                break
            try:
                await adapter.click(target['mcp_ref'], target.get('name') or label or target['mcp_ref'])
                await asyncio.sleep(1)
                next_state = await self._official_mcp_collect_state(
                    adapter,
                    f'page_{len(pages) + 1}',
                    base_url,
                )
                added = self._append_unique_page_state(pages, seen_signatures, next_state)
                if added:
                    state_transitions.append(
                        self._build_state_transition_record(current_state, next_state, target, 'keyword_followup', 0)
                    )
                observations.append({
                    'type': 'official_mcp_explore_click',
                    'element': label,
                    'ref': target.get('mcp_ref'),
                    'added_page': added,
                    'element_count': len(next_state.get('elements') or []),
                })
                covered_target = next((
                    target for target in targets
                    if self._target_requires_form_coverage(target)
                    and str(target.get('stage') or '') not in completed_stages
                    and self._is_target_business_form_covered(next_state, target, safety_policy)
                ), None)
                if covered_target:
                    completed_stages.append(str(covered_target.get('stage') or ''))
                    break
            except Exception as exc:
                observations.append({
                    'type': 'official_mcp_explore_error',
                    'element': label,
                    'ref': target.get('mcp_ref'),
                    'error': str(exc)[:500],
                })
                break
        completed_stages, reconciled_stages = self._reconcile_completed_exploration_stages(
            targets,
            completed_stages,
            state_transitions,
        )
        if reconciled_stages:
            observations.append({
                'type': 'official_mcp_exploration_coverage_reconciled',
                'added_stages': reconciled_stages,
                'reason': 'observed_state_transitions_and_route_prefixes',
            })
        missing_stages = [stage for stage in required_stages if stage not in completed_stages]
        observations.append({
            'type': 'official_mcp_exploration_coverage',
            'status': 'success' if not missing_stages else 'incomplete',
            'planning_allowed': not missing_stages,
            'required_stages': required_stages,
            'completed_stages': completed_stages,
            'missing_stages': missing_stages,
        })
        return pages, state_transitions

    async def _run_official_mcp_ai_generation_task(self, args: dict) -> dict:
        start = time.time()
        task_id = args.get('task_id')
        safety_policy = args.get('safety_policy') or {}
        env_config = args.get('environment_config') or {}
        existing_element_map = args.get('element_map') if isinstance(args.get('element_map'), dict) else {}
        base_url = args.get('target_url') or env_config.get('base_url') or existing_element_map.get('base_url') or ''
        observations: list[dict[str, Any]] = []

        if not base_url:
            return {
                'task_id': task_id,
                'status': 'failed',
                'message': '缺少 target_url 或环境 base_url',
                'failure_category': 'environment',
                'duration': time.time() - start,
            }
        if not self._is_url_allowed(base_url, safety_policy, base_url):
            return {
                'task_id': task_id,
                'status': 'failed',
                'message': f'目标 URL 不在安全边界允许范围内: {base_url}',
                'failure_category': 'permission',
                'duration': time.time() - start,
            }

        try:
            async with self._official_mcp_adapter() as adapter:
                await adapter.navigate(base_url)
                await asyncio.sleep(1)
                first_state = await self._official_mcp_collect_state(adapter, 'page_1', base_url)
                observations.append({
                    'type': 'official_mcp_snapshot',
                    'tools': sorted(adapter.tools),
                    'element_count': len(first_state.get('elements') or []),
                })
                authentication_mode = self._authentication_mode(args, first_state)
                observations.append({
                    'type': 'authentication_strategy',
                    'mode': authentication_mode,
                    'entry_page_key': first_state.get('page_key') or '',
                    'entry_url': first_state.get('url') or base_url,
                })
                if authentication_mode == 'unknown' and self._looks_like_login_page(first_state):
                    return {
                        'task_id': task_id,
                        'status': 'failed',
                        'message': self._authentication_contract_unavailable_reason(args),
                        'mcp_observations': observations,
                        'verification_result': {
                            'status': 'failed',
                            'provider': 'official-playwright-mcp',
                            'error': 'authentication_contract_unavailable',
                            'checked_pages': 1,
                            'checked_elements': len(first_state.get('elements') or []),
                        },
                        'repair_history': [{
                            'round': 1,
                            'type': 'official_mcp_authentication_contract_unavailable',
                            'message': '认证合同不明确且当前页面仍为登录页',
                        }],
                        'failure_category': 'permission',
                        'current_repair_round': 1,
                        'duration': time.time() - start,
                    }
                if self._looks_like_login_page(first_state) and authentication_mode != 'test_subject':
                    first_state = await self._official_mcp_auto_login_if_possible(adapter, first_state, args, observations)
                    if self._looks_like_login_page(first_state):
                        return {
                            'task_id': task_id,
                            'status': 'failed',
                            'message': '官方 Playwright MCP 自动登录未成功，未进入登录后业务页面，已停止生成仅登录页脚本',
                            'mcp_observations': observations,
                            'verification_result': {
                                'status': 'failed',
                                'provider': 'official-playwright-mcp',
                                'error': 'auto_login_failed',
                                'checked_pages': 1,
                                'checked_elements': len(first_state.get('elements') or []),
                            },
                            'repair_history': [{
                                'round': 1,
                                'type': 'official_mcp_login_failed',
                                'message': '登录后页面仍匹配登录页特征',
                            }],
                            'failure_category': 'permission',
                            'current_repair_round': 1,
                            'duration': time.time() - start,
                        }
                elif authentication_mode == 'test_subject':
                    observations.append({
                        'type': 'business_exploration_coverage',
                        'status': 'deferred',
                        'planning_allowed': bool(first_state.get('elements')),
                        'required_stages': ['authentication_subject'],
                        'completed_stages': ['authentication_subject'],
                        'missing_stages': [],
                        'steps_used': 0,
                        'reason': '认证是被测流程，登录页作为测试计划起点，不执行前置自动登录',
                    })
                pages, state_transitions = await self._official_mcp_explore_pages(
                    adapter,
                    first_state,
                    args,
                    safety_policy,
                    base_url,
                    observations,
                )

                risk_items = []
                low_confidence_items = []
                for page_state in pages:
                    for element in page_state.get('elements') or []:
                        if not isinstance(element, dict):
                            continue
                        label = self._element_visible_text(element)
                        if self._looks_dangerous(label, safety_policy):
                            risk_items.append({
                                'page_key': page_state.get('page_key'),
                                'element_key': element.get('element_key'),
                                'mcp_ref': element.get('mcp_ref'),
                                'name': element.get('name'),
                                'reason': '命中危险动作关键词',
                            })
                        locator = element.get('recommended_locator') or {}
                        if locator and float(locator.get('confidence') or 0) < 0.7:
                            low_confidence_items.append({
                                'page_key': page_state.get('page_key'),
                                'element_key': element.get('element_key'),
                                'mcp_ref': element.get('mcp_ref'),
                                'name': element.get('name'),
                                'locator': locator,
                            })

                element_map = {
                    'base_url': base_url,
                    'generated_at': int(time.time()),
                    'capture_mode': 'official_playwright_mcp',
                    'mcp_protocol': {
                        'provider': self.mcp_provider,
                        'transport': self.mcp_transport,
                        'server_url': self.mcp_server_url or '',
                        'command': self.mcp_command,
                        'args': self.mcp_args or [],
                        'official_mcp_server_enabled': True,
                        'tools': sorted(adapter.tools),
                        'tool_aliases': adapter.tool_aliases,
                        'capability_note': '已通过官方 Playwright MCP server tools/list 和 tools/call 执行采集与动作',
                    },
                    'pages': pages,
                    'state_transitions': state_transitions,
                    'exploration_coverage': next(
                        (
                            item for item in reversed(observations)
                            if item.get('type') == 'business_exploration_coverage'
                        ),
                        {
                            'status': 'incomplete',
                            'required_stages': [],
                            'completed_stages': [],
                            'missing_stages': ['business_goal'],
                        },
                    ),
                    'coverage_summary': {
                        'page_count': len(pages),
                        'element_count': sum(len(item.get('elements') or []) for item in pages),
                        'state_transition_count': len(state_transitions),
                        'risk_count': len(risk_items),
                        'low_confidence_count': len(low_confidence_items),
                        'mcp_ref_count': sum(
                            1
                            for page_state in pages
                            for element in page_state.get('elements') or []
                            if isinstance(element, dict) and element.get('mcp_ref')
                        ),
                        'som_element_count': sum(
                            1
                            for page_state in pages
                            for element in page_state.get('elements') or []
                            if isinstance(element, dict) and element.get('som_index')
                        ),
                        'virtual_list_container_count': sum(
                            int((page_state.get('virtual_lists') or {}).get('container_count') or 0)
                            for page_state in pages
                            if isinstance(page_state, dict)
                        ),
                    },
                    'risk_items': risk_items,
                    'low_confidence_items': low_confidence_items,
                }
                element_map['locator_baseline'] = self._build_locator_baseline(element_map)
                merged_context_count = self._merge_existing_element_map_context(element_map, existing_element_map)
                if merged_context_count:
                    observations.append({
                        'type': 'confirmed_element_map_context',
                        'matched_element_count': merged_context_count,
                    })
                    element_map['confirmed_context'] = {'matched_element_count': merged_context_count}

                verification_result = {
                    'status': 'success' if pages else 'failed',
                    'provider': 'official-playwright-mcp',
                    'checked_pages': len(pages),
                    'checked_elements': sum(len(page_state.get('elements') or []) for page_state in pages),
                    'mcp_tools': sorted(adapter.tools),
                    'notes': ['已通过官方 Playwright MCP server snapshot refs 采集页面并构建元素地图'],
                }
                test_plan = self._build_mcp_test_plan(args, element_map)
                generated_script = self._build_mcp_script(args, element_map, test_plan)
                generated_script_hash = hashlib.sha256(generated_script.encode('utf-8')).hexdigest()
                return {
                    'task_id': task_id,
                    'status': 'success' if pages else 'failed',
                    'message': '官方 Playwright MCP 生成、观察与验证完成' if pages else '官方 Playwright MCP 未采集到页面',
                    'test_plan': test_plan,
                    'mcp_observations': observations,
                    'element_map': element_map,
                    'generated_case': {
                        'name': args.get('target_module') or 'AI 生成 UI 用例',
                        'steps': test_plan.get('steps') or [],
                    },
                    'generated_script': generated_script,
                    'generated_script_hash': generated_script_hash,
                    'verification_result': verification_result,
                    'repair_history': [],
                    'failure_category': '' if pages else 'environment',
                    'current_repair_round': 0,
                    'duration': time.time() - start,
                }
        except Exception as exc:
            failure_category = 'timeout' if 'timeout' in str(exc).lower() else 'environment'
            return {
                'task_id': task_id,
                'status': 'failed',
                'message': f'官方 Playwright MCP 生成任务失败: {exc}',
                'mcp_observations': observations,
                'verification_result': {
                    'status': 'failed',
                    'provider': 'official-playwright-mcp',
                    'error': str(exc)[:1000],
                },
                'repair_history': [{
                    'round': 1,
                    'type': 'official_mcp_error',
                    'message': str(exc)[:1000],
                }],
                'failure_category': failure_category,
                'current_repair_round': 1,
                'duration': time.time() - start,
            }

    async def run_ai_generation_task(self, args: dict) -> dict:
        """使用 Playwright 原生 API 探索页面，产出元素地图和运行证据。

        业务意图理解、步骤规划和脚本生成由后端 LLM 规划链负责。执行器在
        这一阶段不再生成规则兜底脚本，避免用采集启发式替代模型规划。
        """
        if False and self.mcp_provider == 'official-playwright-mcp':
            last_result: dict[str, Any] = {}
            for attempt in range(1, 4):
                last_result = await self._run_official_mcp_ai_generation_task(args)
                message = str(last_result.get('message') or '')
                verification = last_result.get('verification_result') if isinstance(last_result.get('verification_result'), dict) else {}
                error = str(verification.get('error') or '')
                if 'session 丢失' not in message and 'session 丢失' not in error and 'Session not found' not in message:
                    return last_result
                logger.warning(f"官方 Playwright MCP 会话丢失，重试整轮采集: {attempt}/3")
                await asyncio.sleep(1)
            observations = last_result.get('mcp_observations') if isinstance(last_result.get('mcp_observations'), list) else []
            observations.append({
                'type': 'official_mcp_session_lost_retries_exhausted',
                'attempts': 3,
            })
            last_result['mcp_observations'] = observations
            logger.warning("官方 Playwright MCP HTTP 会话持续丢失，降级使用原生 Playwright 采集器完成本轮生成")
            original_provider = self.mcp_provider
            self.mcp_provider = 'native-playwright-fallback'
            try:
                fallback_result = await self.run_ai_generation_task(args)
            finally:
                self.mcp_provider = original_provider
            fallback_observations = fallback_result.get('mcp_observations') if isinstance(fallback_result.get('mcp_observations'), list) else []
            fallback_result['mcp_observations'] = [
                {
                    'type': 'official_mcp_http_fallback',
                    'reason': last_result.get('message') or 'official MCP HTTP session lost',
                    'official_attempts': 3,
                },
                *fallback_observations,
            ]
            element_map = fallback_result.get('element_map') if isinstance(fallback_result.get('element_map'), dict) else {}
            element_map['official_mcp_fallback'] = {
                'enabled': True,
                'reason': last_result.get('message') or 'official MCP HTTP session lost',
                'fallback_provider': 'native-playwright',
            }
            fallback_result['element_map'] = element_map
            if fallback_result.get('status') == 'success':
                fallback_result['message'] = '官方 Playwright MCP HTTP 会话不稳定，已自动降级为原生 Playwright 完成登录后页面采集与脚本生成'
            return fallback_result

        start = time.time()
        task_id = args.get('task_id')
        safety_policy = args.get('safety_policy') or {}
        env_config = args.get('environment_config') or {}
        existing_element_map = args.get('element_map') if isinstance(args.get('element_map'), dict) else {}
        base_url = args.get('target_url') or env_config.get('base_url') or existing_element_map.get('base_url') or ''
        max_steps = int(safety_policy.get('max_steps') or 24)
        max_pages = int(safety_policy.get('max_pages') or 8)
        max_repair_rounds = int(args.get('max_repair_rounds') or safety_policy.get('max_repair_rounds') or 2)
        observations = []
        repair_history = []
        verification_result = {}
        failure_category = ''
        storage_state_path = ''

        if not base_url:
            return {
                'task_id': task_id,
                'status': 'failed',
                'message': '缺少 target_url 或环境 base_url',
                'failure_category': 'environment',
                'duration': time.time() - start,
            }
        if not self._is_url_allowed(base_url, safety_policy, base_url):
            return {
                'task_id': task_id,
                'status': 'failed',
                'message': f'目标 URL 不在安全边界允许范围内: {base_url}',
                'failure_category': 'permission',
                'duration': time.time() - start,
            }

        try:
            async with self.browser_session_with_trace(
                f"ai_generation_{task_id}",
                ignore_https_errors=self._ignore_https_errors_for_env(env_config),
            ) as page:
                self._setup_page_listeners(page)
                await self._navigate_for_observation(page, base_url)
                screenshot_path = f"{self.screenshot_dir}/ai_generation_{task_id}_page_1.png"
                first_state = await self._mcp_collect_page_state(page, 'page_1', screenshot_path)
                observations.append({
                    'type': 'browser_snapshot',
                    'url': first_state.get('url'),
                    'title': first_state.get('title'),
                    'element_count': len(first_state.get('elements') or []),
                    'screenshot': screenshot_path,
                })

                pages, state_transitions = await self._auto_explore_business_pages(
                    page=page,
                    task_id=task_id,
                    first_state=first_state,
                    args=args,
                    safety_policy=safety_policy,
                    base_url=base_url,
                    max_pages=max_pages,
                    max_steps=max_steps,
                    observations=observations,
                )

                risk_items = []
                low_confidence_items = []
                for page_state in pages:
                    for element in page_state.get('elements') or []:
                        if not isinstance(element, dict):
                            continue
                        label = self._element_visible_text(element)
                        if self._looks_dangerous(label, safety_policy):
                            risk_items.append({
                                'page_key': page_state.get('page_key'),
                                'element_key': element.get('element_key'),
                                'name': element.get('name'),
                                'reason': '命中危险动作关键词',
                            })
                        locator = element.get('recommended_locator') or {}
                        if locator and float(locator.get('confidence') or 0) < 0.7:
                            low_confidence_items.append({
                                'page_key': page_state.get('page_key'),
                                'element_key': element.get('element_key'),
                                'name': element.get('name'),
                                'locator': locator,
                            })

                element_map = {
                    'base_url': base_url,
                    'generated_at': int(time.time()),
                    'capture_mode': 'playwright_native_page_state',
                    'mcp_protocol': {
                        'provider': 'playwright-native',
                        'server_url': self.mcp_server_url or '',
                        'official_mcp_server_enabled': False,
                        'capability_note': '当前使用 Playwright 原生 API 采集页面状态元素地图',
                    },
                    'pages': pages,
                    'state_transitions': state_transitions,
                    'exploration_coverage': next(
                        (
                            item for item in reversed(observations)
                            if item.get('type') == 'business_exploration_coverage'
                        ),
                        {
                            'status': 'incomplete',
                            'required_stages': [],
                            'completed_stages': [],
                            'missing_stages': ['business_goal'],
                        },
                    ),
                    'coverage_summary': {
                        'page_count': len(pages),
                        'element_count': sum(len(item.get('elements') or []) for item in pages),
                        'state_transition_count': len(state_transitions),
                        'risk_count': len(risk_items),
                        'low_confidence_count': len(low_confidence_items),
                        'som_element_count': sum(
                            1
                            for page_state in pages
                            for element in page_state.get('elements') or []
                            if isinstance(element, dict) and element.get('som_index')
                        ),
                        'virtual_list_container_count': sum(
                            int((page_state.get('virtual_lists') or {}).get('container_count') or 0)
                            for page_state in pages
                            if isinstance(page_state, dict)
                        ),
                        'frame_count': sum(len(page_state.get('frames') or []) for page_state in pages if isinstance(page_state, dict)),
                        'shadow_element_count': sum(
                            int((page_state.get('shadow_dom') or {}).get('element_count') or 0)
                            for page_state in pages
                            if isinstance(page_state, dict)
                        ),
                    },
                    'risk_items': risk_items,
                    'low_confidence_items': low_confidence_items,
                }
                element_map['locator_baseline'] = self._build_locator_baseline(element_map)
                merged_context_count = self._merge_existing_element_map_context(element_map, existing_element_map)
                if merged_context_count:
                    observations.append({
                        'type': 'confirmed_element_map_context',
                        'matched_element_count': merged_context_count,
                    })
                    element_map['confirmed_context'] = {
                        'matched_element_count': merged_context_count,
                    }

                verification_result, locator_repairs, failed_locator_items = await self._verify_and_repair_element_map(
                    page,
                    element_map,
                    max_repair_rounds,
                )
                repair_history.extend(locator_repairs[:max_repair_rounds])
                if failed_locator_items:
                    failure_category = 'locator'
                    low_confidence_items.extend({
                        'page_key': 'page_1',
                        'element_key': item.get('element_key'),
                        'name': item.get('name'),
                        'reason': item.get('reason'),
                    } for item in failed_locator_items)
                element_map['low_confidence_items'] = low_confidence_items
                element_map['coverage_summary']['low_confidence_count'] = len(low_confidence_items)

                storage_state_path = await self._capture_ai_generation_storage_state(task_id)
                if storage_state_path:
                    verification_result['storage_state'] = {
                        'status': 'captured',
                        'path': storage_state_path,
                        'note': '探索阶段浏览器登录态已保存，后续 Playwright Test 真实执行可复用',
                    }

            trace_path = self.get_current_trace_path()
            if trace_path:
                verification_result['trace_path'] = trace_path

            if risk_items:
                repair_history.append({
                    'round': 0,
                    'type': 'guardrail',
                    'message': '发现高风险入口，已跳过执行，仅记录到风险清单',
                    'items': risk_items[:20],
                })

            exploration_coverage = element_map.get('exploration_coverage', {})
            exploration_complete = exploration_coverage.get('status') == 'success'
            planning_allowed = exploration_complete or bool(exploration_coverage.get('planning_allowed'))
            exploration_ready = bool(element_map.get('pages')) and planning_allowed
            coverage_failure_category = (
                str(exploration_coverage.get('failure_category') or '').strip()
                if isinstance(exploration_coverage, dict)
                else ''
            )
            return {
                'task_id': task_id,
                'status': 'success' if exploration_ready else 'failed',
                'message': (
                    'Playwright 原生页面探索完成，等待 LLM 计划生成可执行脚本'
                    if exploration_complete
                    else '入口页面已采集，后续状态转换交由 LLM 规划和真实执行'
                    if planning_allowed
                    else 'Playwright 原生页面探索未覆盖全部业务目标，已停止进入 LLM 规划'
                ),
                'test_plan': {},
                'mcp_observations': observations,
                'element_map': element_map,
                'generated_case': {
                    'name': args.get('target_module') or 'AI 生成 UI 用例',
                    'steps': [],
                },
                'generated_script': '',
                'generated_script_hash': '',
                'verification_result': verification_result,
                'repair_history': repair_history,
                'failure_category': failure_category or ('' if exploration_ready else coverage_failure_category or 'exploration_coverage'),
                'current_repair_round': min(len(repair_history), max_repair_rounds),
                '_local_runtime': {'storage_state_path': storage_state_path},
                'duration': time.time() - start,
            }
        except Exception as exc:
            failure_category = 'timeout' if 'timeout' in str(exc).lower() else 'unknown'
            traceback_text = traceback.format_exc()
            repair_history.append({
                'round': 1,
                'type': 'auto_repair_attempt',
                'message': '第一版修复策略记录失败上下文，后续版本将基于重新观察生成 locator patch',
                'error': str(exc),
                'traceback': traceback_text[-4000:],
            })
            logger.exception("Playwright 原生页面探索任务失败: task_id=%s", task_id)
            return {
                'task_id': task_id,
                'status': 'failed',
                'message': f'Playwright 原生页面探索任务失败: {exc}',
                'mcp_observations': observations,
                'verification_result': verification_result,
                'repair_history': repair_history[:max_repair_rounds],
                'failure_category': failure_category,
                'current_repair_round': min(len(repair_history), max_repair_rounds),
                'duration': time.time() - start,
            }

    @staticmethod
    def _first_sql_keyword(sql: str) -> str:
        sql = sql.strip()
        while sql.startswith('--'):
            _, _, sql = sql.partition('\n')
            sql = sql.strip()
        return sql.split(None, 1)[0].lower() if sql else ''

    def _normalize_sql_execute(self, step: StepConfig) -> dict[str, Any]:
        """解析 SQL 步骤配置，兼容前端 JSON 文本中的常见字段名。"""
        raw_config = step.sql_execute or {}
        if isinstance(raw_config, str):
            raw_config = {'sql': raw_config}
        if not isinstance(raw_config, dict):
            raise ValueError("SQL执行配置必须是对象或SQL字符串")

        sql = (
            raw_config.get('sql')
            or raw_config.get('statement')
            or raw_config.get('query')
        )
        if not sql or not str(sql).strip():
            raise ValueError("SQL执行配置缺少 sql 字段")
        sql = str(sql)

        configured_method = (
            raw_config.get('method')
            or raw_config.get('sql_method')
            or raw_config.get('action')
            or raw_config.get('execute_type')
        )
        first_keyword = self._first_sql_keyword(sql)
        supported_methods = {
            'fetchone', 'fetchmany', 'fetchall', 'select', 'query',
            'insert', 'update', 'delete', 'execute',
        }
        method = str(configured_method).lower() if configured_method else ''
        if method not in supported_methods:
            if first_keyword in {'select', 'with', 'values'}:
                method = 'fetchall'
            elif first_keyword in {'insert', 'update', 'delete'}:
                method = first_keyword
            else:
                method = 'execute'
        if method in {'select', 'query'}:
            method = 'fetchall'

        params = (
            raw_config.get('params')
            if 'params' in raw_config
            else raw_config.get('sql_params', raw_config.get('parameters'))
        )
        if params is None:
            params = {}
        if not isinstance(params, (dict, list, tuple)):
            raise ValueError("SQL参数必须是对象或数组")

        size = raw_config.get('size', raw_config.get('sql_size', raw_config.get('limit', 10)))
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 10

        return {
            'sql': sql,
            'method': method,
            'params': params,
            'size': max(size, 1),
            'first_keyword': first_keyword,
            'db_type': raw_config.get('db_type') or raw_config.get('database_type'),
            'connection': raw_config.get('connection'),
            'db_config': raw_config.get('db_config') or raw_config.get('connection_config'),
        }

    def _resolve_sql_connection_config(
        self,
        sql_config: dict[str, Any],
        env_config: Optional[dict],
    ) -> tuple[str, dict[str, Any]]:
        if not env_config:
            raise ValueError("执行SQL步骤需要选择包含数据库配置的执行环境")

        db_type = str(sql_config.get('db_type') or env_config.get('db_type') or 'mysql').lower()
        if db_type not in {'mysql', 'db2'}:
            raise ValueError(f"不支持的UI自动化数据库类型: {db_type}")

        direct_config = sql_config.get('connection') or sql_config.get('db_config')
        if direct_config is not None and not isinstance(direct_config, dict):
            raise ValueError("SQL连接配置必须是对象")

        db_config = direct_config or env_config.get(f'{db_type}_config') or {}
        if not isinstance(db_config, dict) or not db_config:
            raise ValueError(f"执行环境缺少 {db_type.upper()} 数据库配置")

        required_fields = ['host', 'port', 'database']
        missing = [field for field in required_fields if not db_config.get(field)]
        user = db_config.get('user') or db_config.get('username')
        if not user:
            missing.append('user')
        if not db_config.get('password'):
            missing.append('password')
        if missing:
            raise ValueError(f"{db_type.upper()} 数据库配置缺少字段: {', '.join(missing)}")

        resolved = dict(db_config)
        resolved['user'] = user
        return db_type, resolved

    def _validate_sql_permission(self, sql_config: dict[str, Any], env_config: dict) -> None:
        keyword = sql_config['first_keyword']
        method = sql_config['method']

        if keyword == 'insert' or method == 'insert':
            if not env_config.get('db_c_status', False):
                raise ValueError("当前环境未启用数据库新增操作")
            return

        if not env_config.get('db_rud_status', False):
            raise ValueError("当前环境未启用数据库查改删操作")

    @staticmethod
    def _execute_cursor(cursor: Any, sql: str, params: Any) -> None:
        if params in ({}, [], ()):
            cursor.execute(sql)
        else:
            cursor.execute(sql, params)

    @staticmethod
    def _rows_to_dicts(rows: Any, description: Any) -> list[Any]:
        if rows is None:
            return []
        if isinstance(rows, dict):
            return [rows]
        if not isinstance(rows, list):
            rows = [rows]

        columns = [desc[0] for desc in description] if description else []
        result = []
        for row in rows:
            if isinstance(row, dict):
                result.append(row)
            elif columns and isinstance(row, (list, tuple)):
                result.append(dict(zip(columns, row)))
            else:
                result.append(row)
        return result

    @staticmethod
    def _preview_rows(rows: list[Any]) -> str:
        if not rows:
            return ''
        preview = json.dumps(rows[:3], ensure_ascii=False, default=str)
        if len(preview) > 500:
            preview = preview[:500] + '...'
        return preview

    def _summarize_sql_result(self, method: str, rows: list[Any], affected_rows: int) -> str:
        if method in {'fetchone', 'fetchmany', 'fetchall'}:
            message = f"SQL操作执行成功: 返回 {len(rows)} 行"
            preview = self._preview_rows(rows)
            if preview:
                message += f"，预览: {preview}"
            return message

        if affected_rows is None or affected_rows < 0:
            return "SQL操作执行成功"
        return f"SQL操作执行成功: 影响 {affected_rows} 行"

    def _connect_mysql(self, config: dict[str, Any]):
        try:
            pymysql = importlib.import_module('pymysql')
        except ImportError as exc:
            raise RuntimeError("执行MySQL SQL步骤需要安装依赖 pymysql") from exc

        return pymysql.connect(
            host=config['host'],
            port=int(config['port']),
            user=config['user'],
            password=config['password'],
            database=config['database'],
            charset=config.get('charset') or 'utf8mb4',
            connect_timeout=int(config.get('connect_timeout') or 10),
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _connect_db2(self, config: dict[str, Any]):
        try:
            ibm_db_dbi = importlib.import_module('ibm_db_dbi')
        except ImportError as exc:
            raise RuntimeError("执行DB2 SQL步骤需要安装依赖 ibm_db") from exc

        dsn = (
            f"DATABASE={config['database']};"
            f"HOSTNAME={config['host']};"
            f"PORT={int(config['port'])};"
            "PROTOCOL=TCPIP;"
            f"UID={config['user']};"
            f"PWD={config['password']};"
        )
        return ibm_db_dbi.connect(dsn, '', '')

    def _execute_sql_step(
        self,
        step: StepConfig,
        env_config: Optional[dict],
    ) -> tuple[bool, str]:
        sql_config = self._normalize_sql_execute(step)
        if env_config is None:
            raise ValueError("执行SQL步骤需要执行环境")
        self._validate_sql_permission(sql_config, env_config)
        db_type, db_config = self._resolve_sql_connection_config(sql_config, env_config)

        conn = None
        cursor = None
        try:
            conn = self._connect_db2(db_config) if db_type == 'db2' else self._connect_mysql(db_config)
            cursor = conn.cursor()

            if db_type == 'db2' and db_config.get('schema'):
                schema = str(db_config['schema'])
                if not re.match(r'^[A-Za-z_][A-Za-z0-9_@$#]*$', schema):
                    raise ValueError("DB2 schema 只能包含字母、数字、下划线、@、$、#，且不能以数字开头")
                cursor.execute(f"SET CURRENT SCHEMA {schema}")

            self._execute_cursor(cursor, sql_config['sql'], sql_config['params'])

            method = sql_config['method']
            rows: list[Any] = []
            description = getattr(cursor, 'description', None)
            if method == 'fetchone':
                rows = self._rows_to_dicts(cursor.fetchone(), description)
            elif method == 'fetchmany':
                rows = self._rows_to_dicts(cursor.fetchmany(sql_config['size']), description)
            elif method == 'fetchall':
                rows = self._rows_to_dicts(cursor.fetchall(), description)
            else:
                conn.commit()

            return True, self._summarize_sql_result(method, rows, getattr(cursor, 'rowcount', -1))
        except Exception:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    
    async def _execute_step(
        self,
        page: Page,
        step: StepConfig,
        env_config: Optional[dict] = None,
    ) -> tuple[bool, str, str | None]:
        """执行单个步骤
        
        Returns:
            tuple: (成功与否, 消息, 截图路径(可选))
        """
        if step.step_type == 2:
            success, message = await asyncio.to_thread(self._execute_sql_step, step, env_config)
            return success, message, None

        operation = (step.operation_type or '').lower()
        screenshot_path: str | None = None
        
        # 等待时间（仅当用户明确设置 > 0 时才等待，用于特殊场景）
        # 注意：Playwright 自带 Auto-waiting，一般不需要手动等待
        if step.wait_time > 0:
            logger.debug(f"步骤 {step.step_id}: 强制等待 {step.wait_time}s（建议设为0让Playwright自动等待）")
            await page.wait_for_timeout(int(step.wait_time * 1000))
        
        # 记录开始时间
        op_start = time.time()
        
        # switch_tab 操作特殊处理
        if operation == 'switch_tab':
            if not page.context:
                return False, "浏览器上下文为空，无法切换页签", None
            
            pages = page.context.pages
            target_idx = None
            try:
                target_idx = int(step.input_value)
            except (ValueError, TypeError):
                pass
            
            if target_idx is not None:
                if 0 <= target_idx < len(pages):
                    self._page = pages[target_idx]
                    logger.info(f"成功切换到页签索引: {target_idx}, URL: {self._page.url}")
                    return True, f"成功切换到页签索引: {target_idx}", None
                else:
                    return False, f"切换页签失败，索引 {target_idx} 越界（当前共有 {len(pages)} 个页签）", None
            else:
                query = step.input_value.strip() if step.input_value else ''
                if not query:
                    return False, "切换页签参数为空，请输入索引、URL或页签标题", None
                
                for p in pages:
                    try:
                        title = await p.title()
                        if query in p.url or query in title:
                            self._page = p
                            logger.info(f"成功切换到页签: title='{title}', url='{p.url}'")
                            return True, f"成功切换到符合条件 '{query}' 的页签", None
                    except Exception as e:
                        logger.warning(f"获取页签属性失败: {e}")
                return False, f"未找到匹配 '{query}' 的页签", None

        # screenshot 操作特殊处理，保存路径
        if operation == 'screenshot':
            screenshot_path = step.input_value or f"{self.screenshot_dir}/step_{step.step_id}.png"
            await page.screenshot(path=screenshot_path)
            logger.debug(f"步骤 {step.step_id}: screenshot 耗时 {time.time() - op_start:.2f}s")
            return True, f"页面操作 {operation} 执行成功", screenshot_path
        
        # 页面操作（不需要定位器）
        def _parse_wait_timeout(value: str) -> int:
            """解析等待时间（毫秒）"""
            if not value:
                return 1000  # 默认 1 秒
            try:
                return int(float(value))
            except ValueError:
                return 1000

        page_operations = {
            'goto': lambda: page.goto(step.input_value),
            'reload': lambda: page.reload(),
            'go_back': lambda: page.go_back(),
            'go_forward': lambda: page.go_forward(),
            'wait': lambda: page.wait_for_timeout(_parse_wait_timeout(step.input_value)),
            'wait_load': lambda: page.wait_for_load_state("load"),
            'wait_network': lambda: page.wait_for_load_state("networkidle"),
        }
        
        if operation in page_operations:
            await page_operations[operation]()
            logger.debug(f"步骤 {step.step_id}: {operation} 耗时 {time.time() - op_start:.2f}s")
            return True, f"页面操作 {operation} 执行成功", None
        
        # 元素操作（需要定位器）- 先验证定位器是否有效
        if not step.locator_value or not step.locator_value.strip():
            return False, f"元素定位器为空，请在元素管理中配置定位表达式（步骤: {step.description or step.step_id}）", None
        
        locator_start = time.time()
        target = page
        if step.is_iframe and step.iframe_locator:
            logger.info(f"切换至 iframe 上下文, 表达式: {step.iframe_locator}")
            iframe_selectors = [step.iframe_locator]
            if ">>>" in step.iframe_locator:
                iframe_selectors = [s.strip() for s in step.iframe_locator.split(">>>") if s.strip()]
            elif ">>" in step.iframe_locator:
                iframe_selectors = [s.strip() for s in step.iframe_locator.split(">>") if s.strip()]

            for selector in iframe_selectors:
                target = target.frame_locator(selector)

        locators_to_try = [
            (step.locator_type, step.locator_value, step.locator_index),
        ]
        if step.locator_type_2 and step.locator_value_2:
            locators_to_try.append((step.locator_type_2, step.locator_value_2, step.locator_index_2))
        if step.locator_type_3 and step.locator_value_3:
            locators_to_try.append((step.locator_type_3, step.locator_value_3, step.locator_index_3))

        locator = None
        locator_type_used = step.locator_type
        locator_value_used = step.locator_value

        for idx, (locator_type, locator_value, locator_index) in enumerate(locators_to_try, start=1):
            if not locator_value or not locator_value.strip():
                continue

            logger.info(
                f"步骤 {step.step_id}: 尝试定位器 {idx} [{locator_type}={locator_value}]"
                + (f" 下标 {locator_index}" if locator_index is not None else "")
            )
            candidate = self._get_locator(target, locator_type, locator_value)
            if locator_index is not None:
                candidate = candidate.nth(locator_index)

            try:
                await candidate.wait_for(state="visible", timeout=5000 if idx == 1 else 2000)
                locator = candidate
                locator_type_used = locator_type
                locator_value_used = locator_value
                logger.info(f"步骤 {step.step_id}: 定位器 {idx} [{locator_type}={locator_value}] 可见并被成功选中")
                break
            except Exception as exc:
                logger.warning(f"步骤 {step.step_id}: 定位器 {idx} [{locator_type}={locator_value}] 尝试失败或不可见: {exc}")
                if idx == len(locators_to_try):
                    locator = candidate
                    locator_type_used = locator_type
                    locator_value_used = locator_value

        if locator is None:
            return False, f"所有定位器都失效（包含备用定位器，步骤: {step.description or step.step_id}）", None

        locator_time = time.time() - locator_start
        logger.debug(
            f"步骤 {step.step_id}: 定位元素 [{locator_type_used}={locator_value_used}] "
            f"耗时 {locator_time:.2f}s (iframe={step.is_iframe})"
        )
        
        if operation == 'fill' and step.binding_mode == 'runtime_input':
            value, artifact_path = await self._resolve_runtime_input(
                target,
                locator,
                step.runtime_resolver,
                step.step_id,
            )
            logger.info(
                "步骤 %s: 运行时输入解析器 %s 已识别并填写值 %s",
                step.step_id,
                step.runtime_resolver,
                value,
            )
            return (
                True,
                f"运行时输入 {step.runtime_resolver} 解析并填写成功: {value}",
                artifact_path,
            )

        if operation in {'fill', 'type'} and not step.input_value:
            return False, f"元素操作 {operation} 输入值为空，已拒绝执行", None

        element_operations = {
            'click': lambda: locator.click(),
            'dblclick': lambda: locator.dblclick(),
            'fill': lambda: locator.fill(step.input_value),
            'type': lambda: locator.type(step.input_value),
            'clear': lambda: locator.fill(""),
            'check': lambda: locator.check(),
            'uncheck': lambda: locator.uncheck(),
            'select': lambda: locator.select_option(step.input_value),
            'hover': lambda: locator.hover(),
            'focus': lambda: locator.focus(),
            'press': lambda: locator.press(step.input_value),
            'upload': lambda: locator.set_input_files(step.input_value),
        }
        
        if operation in element_operations:
            action_start = time.time()
            await element_operations[operation]()
            action_time = time.time() - action_start
            logger.debug(f"步骤 {step.step_id}: {operation} 操作耗时 {action_time:.2f}s (总计 {time.time() - op_start:.2f}s)")
            return True, f"元素操作 {operation} 执行成功", None
        
        # 断言操作
        if operation.startswith('assert_'):
            assert_type = operation.replace('assert_', '')
            assert_operations = {
                'visible': lambda: expect(locator).to_be_visible(),
                'hidden': lambda: expect(locator).to_be_hidden(),
                'enabled': lambda: expect(locator).to_be_enabled(),
                'disabled': lambda: expect(locator).to_be_disabled(),
                'checked': lambda: expect(locator).to_be_checked(),
                'text': lambda: expect(locator).to_have_text(step.input_value),
                'value': lambda: expect(locator).to_have_value(step.input_value),
                'contain_text': lambda: expect(locator).to_contain_text(step.input_value),
                'url': lambda: expect(page).to_have_url(step.input_value),
                'title': lambda: expect(page).to_have_title(step.input_value),
            }
            if assert_type in assert_operations:
                await assert_operations[assert_type]()
                logger.debug(f"步骤 {step.step_id}: assert_{assert_type} 耗时 {time.time() - op_start:.2f}s")
                return True, f"断言 {assert_type} 通过", None
        
        return False, f"未知操作类型: {operation}", None

    @staticmethod
    async def _nearest_runtime_image(container: Union[Page, FrameLocator], input_locator):
        input_box = await input_locator.bounding_box()
        if not input_box:
            raise RuntimeError('运行时输入框不可见，无法定位关联验证码图像')

        candidates = container.locator(
            "img, canvas, svg, [style*='background-image']"
        )
        best_candidate = None
        best_score = float('inf')
        for index in range(min(await candidates.count(), 120)):
            candidate = candidates.nth(index)
            if not await candidate.is_visible():
                continue
            box = await candidate.bounding_box()
            if (
                not box
                or box['width'] < 24
                or box['height'] < 12
                or box['width'] > 360
                or box['height'] > 180
            ):
                continue
            input_center_y = input_box['y'] + input_box['height'] / 2
            image_center_y = box['y'] + box['height'] / 2
            vertical_distance = abs(input_center_y - image_center_y)
            if vertical_distance > max(90, input_box['height'] * 2.5):
                continue
            if box['x'] >= input_box['x']:
                horizontal_gap = abs(box['x'] - (input_box['x'] + input_box['width']))
            else:
                horizontal_gap = abs(input_box['x'] - (box['x'] + box['width'])) + 240
            score = (
                vertical_distance * 4
                + horizontal_gap
                + abs(box['height'] - input_box['height'])
            )
            if score < best_score:
                best_score = score
                best_candidate = candidate

        if best_candidate is None:
            raise RuntimeError('未找到与运行时输入框关联的可见验证码图像')
        return best_candidate

    async def _resolve_runtime_input(
        self,
        container: Union[Page, FrameLocator],
        input_locator,
        resolver: str,
        step_id: int | str,
        max_attempts: int = 3,
    ) -> tuple[str, str]:
        resolver = str(resolver or '').strip()
        if resolver != 'image_ocr':
            raise ValueError(f'不支持的运行时输入解析器: {resolver or "未配置"}')

        artifact_dir = Path(self.screenshot_dir) / 'runtime-inputs'
        artifact_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            captcha_visual = await self._nearest_runtime_image(container, input_locator)
            artifact_path = artifact_dir / (
                f'image_ocr_step_{step_id}_{attempt}_{time.time_ns()}.png'
            )
            await captcha_visual.screenshot(path=str(artifact_path))
            try:
                result = await asyncio.to_thread(resolve_image_captcha, artifact_path)
                value = str(result.get('value') or '').strip()
                if not value:
                    raise ValueError('OCR 未识别出有效验证码')
                await input_locator.fill(value)
                return value, str(artifact_path)
            except Exception as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                try:
                    await captcha_visual.click()
                    await asyncio.sleep(0.25)
                except Exception:
                    logger.debug('验证码图像不支持点击刷新', exc_info=True)

        raise RuntimeError(
            f'image_ocr 在 {max_attempts} 次尝试后仍未解析出有效验证码: {last_error}'
        )
    
    async def _capture_runtime_step_screenshot(
        self,
        page: Page,
        case_id: int | str,
        step_id: int | str,
        status: str,
    ) -> Optional[str]:
        """Capture the visible page state after every executed step."""
        screenshot_dir = Path(self.screenshot_dir)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        safe_status = re.sub(r'[^a-zA-Z0-9_-]+', '_', str(status or 'unknown'))
        screenshot_path = screenshot_dir / (
            f'{safe_status}_{case_id}_{step_id}_{time.time_ns()}.png'
        )
        try:
            # Give SPA rendering and dialog transitions one event-loop turn to settle.
            await page.wait_for_timeout(100)
            await page.screenshot(
                path=str(screenshot_path),
                full_page=False,
                animations='disabled',
                timeout=min(max(int(self.action_timeout), 1000), 10000),
            )
            return str(screenshot_path)
        except Exception as exc:
            logger.warning(
                '步骤截图采集失败: case_id=%s, step_id=%s, error=%s',
                case_id,
                step_id,
                exc,
            )
            return None

    async def execute_step(self, step: StepConfig, page_url: str = '') -> StepResultModel:
        """执行单个步骤（独立浏览器会话）"""
        start_time = time.time()
        
        try:
            async with self.browser_session() as page:
                if page_url:
                    await page.goto(page_url)
                
                success, message, step_screenshot = await self._execute_step(page, step)
                if not step_screenshot:
                    step_screenshot = await self._capture_runtime_step_screenshot(
                        page,
                        case_id='single',
                        step_id=step.step_id,
                        status='success' if success else 'failed',
                    )
                duration = time.time() - start_time
                
                return StepResultModel(
                    step_id=step.step_id,
                    status='success' if success else 'failed',
                    message=message,
                    description=step.description or step.operation_type,
                    duration=duration,
                    element_found=success,
                    screenshot=step_screenshot
                )
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"步骤执行失败: {e}\n{traceback.format_exc()}")
            return StepResultModel(
                step_id=step.step_id,
                status='failed',
                message=str(e),
                description=step.description or step.operation_type,
                duration=duration,
                element_found=False
            )
    
    async def execute_test_case(self, config: TestCaseConfig) -> CaseResultModel:
        """执行测试用例（支持 Trace 记录）"""
        start_time = time.time()
        step_results = []
        passed_steps = 0
        failed_steps = 0
        total_steps = sum(len(ps.steps) for ps in config.page_steps)
        
        self._stop_requested = False
        trace_name = f"case_{config.case_id}"
        
        try:
            # 使用带 trace 的浏览器会话
            async with self.browser_session_with_trace(
                trace_name,
                ignore_https_errors=self._ignore_https_errors_for_env(config.env_config),
            ) as page:
                self._page = page
                logger.info(f"开始执行用例: {config.case_name}")
                self._page_errors = []
                self._setup_page_listeners(page)

                # 浏览器启动后，立即导航到环境配置的 base_url
                base_url = ''
                if config.env_config:
                    base_url = config.env_config.get('base_url', '') or ''
                if base_url:
                    logger.info(f"导航到环境 base_url: {base_url}")
                    await self._page.goto(base_url, wait_until="networkidle")

                for page_step in config.page_steps:
                    if self._stop_requested:
                        raise Exception("用例被手动停止")

                    logger.info(f"执行页面步骤: {page_step.page_name}")

                    # 确保使用最新的页签引用进行环境跳转检测
                    page = self._page

                    # 检测页面跳转：仅当下一个页面 URL 与当前不同时才等待
                    if page_step.page_url:
                        current_url = page.url
                        expected_url = page_step.page_url.rstrip('/')
                        
                        # 只有当期望的 URL 与当前 URL 不同时，才等待跳转
                        if expected_url not in current_url:
                            try:
                                # 短暂等待，检测是否有 URL 变化
                                await page.wait_for_url(
                                    lambda url: url != current_url,
                                    timeout=2000
                                )
                                logger.debug(f"检测到页面跳转: {current_url} -> {page.url}")
                            except Exception:
                                # 没有页面跳转是正常情况
                                pass
                    
                    # 执行页面内的步骤
                    for step in page_step.steps:
                        if self._stop_requested:
                            raise Exception("用例被手动停止")
                        
                        # 确保总是使用最新的活跃页签进行操作
                        page = self._page
                        
                        step_start = time.time()
                        try:
                            success, message, step_screenshot = await self._execute_step(
                                page,
                                step,
                                page_step.env_config or config.env_config,
                            )
                            
                            # 执行后重新同步页签引用，以防步骤内发生了页签切换
                            page = self._page
                            if not step_screenshot:
                                step_screenshot = await self._capture_runtime_step_screenshot(
                                    page,
                                    case_id=config.case_id,
                                    step_id=step.step_id,
                                    status='success' if success else 'failed',
                                )
                            step_duration = time.time() - step_start

                            step_result = StepResultModel(
                                step_id=step.step_id,
                                status='success' if success else 'failed',
                                message=message,
                                description=step.description or step.operation_type,
                                duration=step_duration,
                                element_found=success,
                                screenshot=step_screenshot  # 保存截图操作的路径
                            )

                            if success:
                                passed_steps += 1
                                logger.debug(f"  ✅ {step.description or step.operation_type}")
                            else:
                                failed_steps += 1
                                logger.warning(f"  ❌ {step.description or step.operation_type}: {message}")
                        except Exception as step_error:
                            step_duration = time.time() - step_start
                            failed_steps += 1
                            error_msg = str(step_error)
                            logger.error(f"  ❌ {step.description or step.operation_type}: {error_msg}")

                            screenshot_path = await self._capture_runtime_step_screenshot(
                                page,
                                case_id=config.case_id,
                                step_id=step.step_id,
                                status='error',
                            )

                            step_result = StepResultModel(
                                step_id=step.step_id,
                                status='failed',
                                message=error_msg,
                                description=step.description or step.operation_type,
                                duration=step_duration,
                                element_found=False,
                                screenshot=screenshot_path
                            )
                        
                        step_results.append(step_result)

                    # 页面步骤执行完毕后，等待页面稳定（处理可能的页面跳转）
                    try:
                        await page.wait_for_load_state("load", timeout=10000)
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        logger.debug(f"页面步骤 {page_step.page_name} 执行后等待页面稳定超时，继续执行")

                duration = time.time() - start_time
                status = 'success' if failed_steps == 0 else 'failed'
                message = f"用例执行{'成功' if status == 'success' else '失败'}: 通过 {passed_steps}/{total_steps}"
                if self._page_errors:
                    message += f" (捕获 {len(self._page_errors)} 个页面 JS 错误: {'; '.join(self._page_errors[:3])})"
                logger.info(f"✅ {message}" if status == 'success' else f"❌ {message}")
                
                # 获取 trace 文件路径（会在 browser_session_with_trace 结束时设置）
                trace_path = None
            
            # 会话结束后获取 trace 路径
            trace_path = self.get_current_trace_path()
            if trace_path:
                logger.info(f"用例执行 Trace 已记录: {trace_path}")
            
            return CaseResultModel(
                case_id=config.case_id,
                status=status,
                message=message,
                total_steps=total_steps,
                passed_steps=passed_steps,
                failed_steps=failed_steps,
                duration=duration,
                steps=step_results,
                trace_path=trace_path
            )
                
        except Exception as e:
            duration = time.time() - start_time
            error_msg = str(e)
            logger.error(f"用例执行异常: {error_msg}\n{traceback.format_exc()}")
            
            # 尝试获取 trace 路径（可能已保存）
            trace_path = self.get_current_trace_path()
            
            return CaseResultModel(
                case_id=config.case_id,
                status='failed',
                message=error_msg,
                total_steps=total_steps,
                passed_steps=passed_steps,
                failed_steps=failed_steps + (total_steps - passed_steps - failed_steps),
                duration=duration,
                steps=step_results,
                trace_path=trace_path
            )

    async def execute_page_step(self, config: PageStepConfig) -> list[StepResultModel]:
        """执行单个页面步骤（包含多个操作）- 使用同一个浏览器会话"""
        step_results = []
        
        try:
            async with self.browser_session(
                ignore_https_errors=self._ignore_https_errors_for_env(config.env_config),
            ) as page:
                logger.info(f"开始执行页面步骤: {config.page_name}")
                self._page_errors = []
                self._setup_page_listeners(page)
                
                # 导航到页面
                if config.page_url:
                    nav_start = time.time()
                    await page.goto(config.page_url)
                    await page.wait_for_load_state("domcontentloaded")
                    logger.debug(f"页面导航 {config.page_name} 耗时 {time.time() - nav_start:.2f}s")
                
                # 执行页面内的所有步骤
                for step in config.steps:
                    step_start = time.time()
                    try:
                        success, message, step_screenshot = await self._execute_step(
                            page,
                            step,
                            config.env_config,
                        )
                        if not step_screenshot:
                            step_screenshot = await self._capture_runtime_step_screenshot(
                                page,
                                case_id=f'page-{config.page_step_id}',
                                step_id=step.step_id,
                                status='success' if success else 'failed',
                            )
                        step_duration = time.time() - step_start

                        step_result = StepResultModel(
                            step_id=step.step_id,
                            status='success' if success else 'failed',
                            message=message,
                            description=step.description or step.operation_type,
                            duration=step_duration,
                            element_found=success,
                            screenshot=step_screenshot
                        )
                        step_results.append(step_result)

                        if success:
                            logger.debug(f"  ✅ {step.description or step.operation_type}")
                        else:
                            logger.warning(f"  ❌ {step.description or step.operation_type}: {message}")
                            break  # 步骤失败时停止执行后续步骤

                    except Exception as step_error:
                        step_duration = time.time() - step_start
                        error_msg = str(step_error)
                        logger.error(f"  ❌ {step.description or step.operation_type}: {error_msg}")

                        screenshot_path = await self._capture_runtime_step_screenshot(
                            page,
                            case_id=f'page-{config.page_step_id}',
                            step_id=step.step_id,
                            status='error',
                        )

                        step_result = StepResultModel(
                            step_id=step.step_id,
                            status='failed',
                            message=error_msg,
                            description=step.description or step.operation_type,
                            duration=step_duration,
                            element_found=False,
                            screenshot=screenshot_path
                        )
                        step_results.append(step_result)
                        break  # 步骤失败时停止执行后续步骤
                        
        except Exception as e:
            logger.error(f"页面步骤执行异常: {e}\n{traceback.format_exc()}")
            # 如果连浏览器都打不开，返回一个失败结果
            if not step_results:
                step_results.append(StepResultModel(
                    step_id=0,
                    status='failed',
                    message=str(e),
                    duration=0,
                    element_found=False
                ))

        return step_results

    async def _execute_case_on_context(
        self,
        context: BrowserContext,
        config: TestCaseConfig,
        trace_enabled: bool = False
    ) -> CaseResultModel:
        """在独立上下文中执行用例（用于并发执行）"""
        start_time = time.time()
        step_results = []
        passed_steps = 0
        failed_steps = 0
        total_steps = sum(len(ps.steps) for ps in config.page_steps)
        trace_path = None

        try:
            # 启动 Trace
            if trace_enabled:
                await context.tracing.start(
                    screenshots=self.trace_screenshots,
                    snapshots=self.trace_snapshots,
                    sources=self.trace_sources,
                )

            page = await context.new_page()
            page.set_default_timeout(self.action_timeout)
            self._page_errors = []
            self._setup_page_listeners(page)

            logger.info(f"[并发] 开始执行用例: {config.case_name}")

            # 浏览器启动后，立即导航到环境配置的 base_url
            base_url = ''
            if config.env_config:
                base_url = config.env_config.get('base_url', '') or ''
            if base_url:
                logger.info(f"[并发] 导航到环境 base_url: {base_url}")
                await page.goto(base_url, wait_until="networkidle")

            for page_step in config.page_steps:
                if self._stop_requested:
                    raise Exception("用例被手动停止")

                logger.info(f"[并发] 执行页面步骤: {page_step.page_name}")

                # 检测页面跳转：仅当下一个页面 URL 与当前不同时才等待
                if page_step.page_url:
                    current_url = page.url
                    expected_url = page_step.page_url.rstrip('/')

                    # 只有当期望的 URL 与当前 URL 不同时，才等待跳转
                    if expected_url not in current_url:
                        try:
                            # 短暂等待，检测是否有 URL 变化
                            await page.wait_for_url(
                                lambda url: url != current_url,
                                timeout=2000
                            )
                            logger.debug(f"[并发] 检测到页面跳转: {current_url} -> {page.url}")
                        except Exception:
                            # 没有页面跳转是正常情况
                            pass

                # 执行页面内的步骤
                for step in page_step.steps:
                    if self._stop_requested:
                        raise Exception("用例被手动停止")

                    step_start = time.time()
                    try:
                        success, message, step_screenshot = await self._execute_step(
                            page,
                            step,
                            page_step.env_config or config.env_config,
                        )
                        if not step_screenshot:
                            step_screenshot = await self._capture_runtime_step_screenshot(
                                page,
                                case_id=config.case_id,
                                step_id=step.step_id,
                                status='success' if success else 'failed',
                            )
                        step_duration = time.time() - step_start

                        step_result = StepResultModel(
                            step_id=step.step_id,
                            status='success' if success else 'failed',
                            message=message,
                            description=step.description or step.operation_type,
                            duration=step_duration,
                            element_found=success,
                            screenshot=step_screenshot
                        )

                        if success:
                            passed_steps += 1
                        else:
                            failed_steps += 1
                    except Exception as step_error:
                        step_duration = time.time() - step_start
                        failed_steps += 1
                        error_msg = str(step_error)

                        screenshot_path = await self._capture_runtime_step_screenshot(
                            page,
                            case_id=config.case_id,
                            step_id=step.step_id,
                            status='error',
                        )

                        step_result = StepResultModel(
                            step_id=step.step_id,
                            status='failed',
                            message=error_msg,
                            description=step.description or step.operation_type,
                            duration=step_duration,
                            element_found=False,
                            screenshot=screenshot_path
                        )

                    step_results.append(step_result)

                # 页面步骤执行完毕后，等待页面稳定（处理可能的页面跳转）
                try:
                    await page.wait_for_load_state("load", timeout=10000)
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    logger.debug(f"[并发] 页面步骤 {page_step.page_name} 执行后等待页面稳定超时，继续执行")

            duration = time.time() - start_time
            status = 'success' if failed_steps == 0 else 'failed'
            message = f"用例执行{'成功' if status == 'success' else '失败'}: 通过 {passed_steps}/{total_steps}"
            if self._page_errors:
                message += f" (捕获 {len(self._page_errors)} 个页面 JS 错误: {'; '.join(self._page_errors[:3])})"

            # 保存 Trace
            if trace_enabled:
                trace_path = f"{self.trace_dir}/case_{config.case_id}_{int(time.time())}.zip"
                await context.tracing.stop(path=trace_path)

            await page.close()

            logger.info(f"[并发] {'✅' if status == 'success' else '❌'} {message}")

            return CaseResultModel(
                case_id=config.case_id,
                status=status,
                message=message,
                total_steps=total_steps,
                passed_steps=passed_steps,
                failed_steps=failed_steps,
                duration=duration,
                steps=step_results,
                trace_path=trace_path
            )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = str(e)
            logger.error(f"[并发] 用例执行异常: {error_msg}")

            # 尝试保存 Trace
            if trace_enabled:
                try:
                    trace_path = f"{self.trace_dir}/case_{config.case_id}_{int(time.time())}.zip"
                    await context.tracing.stop(path=trace_path)
                except:
                    pass

            return CaseResultModel(
                case_id=config.case_id,
                status='failed',
                message=error_msg,
                total_steps=total_steps,
                passed_steps=passed_steps,
                failed_steps=failed_steps + (total_steps - passed_steps - failed_steps),
                duration=duration,
                steps=step_results,
                trace_path=trace_path
            )

    async def execute_batch_concurrent(
        self,
        configs: list[TestCaseConfig],
        max_concurrent: int = 3,
        on_result = None
    ) -> list[CaseResultModel]:
        """并发执行多个用例

        Args:
            configs: 用例配置列表
            max_concurrent: 最大并发数
            on_result: 单个用例完成时的回调函数 (可选)

        Returns:
            用例执行结果列表
        """
        if not configs:
            return []

        semaphore = asyncio.Semaphore(max_concurrent)

        # 确保浏览器已初始化（非持久化模式）
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        browser_launcher = getattr(self._playwright, self.browser_type)
        browser = await browser_launcher.launch(
            headless=self.headless,
            timeout=self.launch_timeout,
        )

        logger.info(f"[并发执行] 开始执行 {len(configs)} 个用例, 最大并发数: {max_concurrent}")

        async def run_with_limit(config: TestCaseConfig):
            async with semaphore:
                # 每个用例独立的浏览器上下文
                context = await browser.new_context(
                    ignore_https_errors=self._ignore_https_errors_for_env(config.env_config),
                )
                try:
                    result = await self._execute_case_on_context(
                        context,
                        config,
                        trace_enabled=self.trace_enabled
                    )
                    if on_result:
                        await on_result(result)
                    return result
                finally:
                    await context.close()

        try:
            # 并发执行所有用例
            results = await asyncio.gather(
                *[run_with_limit(c) for c in configs],
                return_exceptions=True
            )

            # 处理异常结果
            final_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    final_results.append(CaseResultModel(
                        case_id=configs[i].case_id,
                        status='failed',
                        message=str(result),
                        total_steps=0,
                        passed_steps=0,
                        failed_steps=0,
                        duration=0,
                        steps=[]
                    ))
                else:
                    final_results.append(result)

            logger.info(f"[并发执行] 完成, 成功: {sum(1 for r in final_results if r.status == 'success')}/{len(final_results)}")
            return final_results

        finally:
            await browser.close()
