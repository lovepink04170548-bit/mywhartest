"""
Official Playwright MCP server adapter.

The adapter speaks MCP over stdio JSON-RPC directly so the actuator can use the
official Playwright MCP server without depending on the Python MCP SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from browser_installer import get_browser_executable_path

logger = logging.getLogger('actuator')


def _default_executable_path_arg() -> list[str]:
    chrome = get_browser_executable_path()
    return ['--executable-path', str(chrome)] if chrome else []


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


_MCP_SNAPSHOT_ERROR_PATTERNS = [
    '### Error',
    "Chromium distribution",
    "Executable doesn't exist",
    'browserType.launch',
    'is not found',
]


class McpProtocolError(RuntimeError):
    """Raised when the MCP server returns a JSON-RPC error or invalid response."""


class McpSessionLostError(McpProtocolError):
    """Raised when streamable HTTP MCP session state is lost."""


@dataclass
class McpTool:
    name: str
    description: str = ''
    input_schema: dict[str, Any] | None = None

    @property
    def properties(self) -> dict[str, Any]:
        schema = self.input_schema if isinstance(self.input_schema, dict) else {}
        properties = schema.get('properties')
        return properties if isinstance(properties, dict) else {}


class PlaywrightMcpAdapter:
    """MCP stdio client tailored for the official Playwright MCP server."""

    def __init__(
        self,
        command: str = 'npx',
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        request_timeout: float = 60.0,
    ):
        self.command = command
        self.args = args or [
            '-y',
            '@playwright/mcp@latest',
            '--caps=vision,pdf,devtools',
            '--headless',
            '--ignore-https-errors',
            *_default_executable_path_arg(),
        ]
        self.cwd = cwd
        self.env = env or {}
        self.request_timeout = request_timeout
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self.tools: dict[str, McpTool] = {}
        self.tool_aliases: dict[str, str] = {}

    async def __aenter__(self) -> 'PlaywrightMcpAdapter':
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def start(self):
        if self.process:
            return
        env = os.environ.copy()
        env.update(self.env)
        self.process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        try:
            await self.initialize()
            await self.refresh_tools()
        except Exception:
            await self.close()
            raise

    async def close(self):
        for task in [self._reader_task, self._stderr_task]:
            if task:
                task.cancel()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None

    async def _read_stdout(self):
        assert self.process and self.process.stdout
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            if line.lower().startswith(b'content-length:'):
                try:
                    content_length = int(line.decode('ascii').split(':', 1)[1].strip())
                    while True:
                        header_line = await self.process.stdout.readline()
                        if header_line in {b'\r\n', b'\n', b''}:
                            break
                    body = await self.process.stdout.readexactly(content_length)
                    message = json.loads(body.decode('utf-8'))
                except Exception as exc:
                    logger.warning(f"MCP stdout 帧解析失败: {exc}")
                    continue
            else:
                try:
                    message = json.loads(line.decode('utf-8'))
                except json.JSONDecodeError:
                    logger.debug(f"MCP stdout 非 JSON 行: {line[:500]!r}")
                    continue
            message_id = message.get('id')
            if message_id is None:
                logger.debug(f"MCP notification: {message}")
                continue
            future = self._pending.pop(int(message_id), None)
            if future and not future.done():
                future.set_result(message)

    async def _read_stderr(self):
        assert self.process and self.process.stderr
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            logger.info(f"Playwright MCP: {line.decode('utf-8', errors='replace').rstrip()}")

    async def _send(self, payload: dict[str, Any]):
        if not self.process or not self.process.stdin:
            raise McpProtocolError('MCP server is not running')
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        header = f'Content-Length: {len(body)}\r\n\r\n'.encode('ascii')
        self.process.stdin.write(header + body)
        await self.process.stdin.drain()

    async def request(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        await self._send({
            'jsonrpc': '2.0',
            'id': request_id,
            'method': method,
            'params': params or {},
        })
        try:
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise McpProtocolError(f'MCP request timeout: {method}') from exc
        if response.get('error'):
            raise McpProtocolError(f"MCP {method} error: {response['error']}")
        result = response.get('result')
        if not isinstance(result, dict):
            return {}
        return result

    async def notify(self, method: str, params: Optional[dict[str, Any]] = None):
        await self._send({
            'jsonrpc': '2.0',
            'method': method,
            'params': params or {},
        })

    async def initialize(self):
        await self.request('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {
                'name': 'WHartTest Actuator',
                'version': '1.0.0',
            },
        })
        await self.notify('notifications/initialized')

    async def refresh_tools(self):
        result = await self.request('tools/list')
        tools = result.get('tools') if isinstance(result.get('tools'), list) else []
        self.tools = {}
        for raw_tool in tools:
            if not isinstance(raw_tool, dict) or not raw_tool.get('name'):
                continue
            tool = McpTool(
                name=raw_tool['name'],
                description=raw_tool.get('description') or '',
                input_schema=raw_tool.get('inputSchema') or raw_tool.get('input_schema') or {},
            )
            self.tools[tool.name] = tool
        self.tool_aliases = self._build_tool_aliases()
        logger.info(f"Playwright MCP tools loaded: {sorted(self.tools)}")

    def _build_tool_aliases(self) -> dict[str, str]:
        aliases = {}
        specs = {
            'navigate': ['browser_navigate', 'navigate'],
            'snapshot': ['browser_snapshot', 'snapshot', 'accessibility_snapshot'],
            'click': ['browser_click', 'click'],
            'type': ['browser_type', 'browser_fill', 'fill', 'type'],
            'select': ['browser_select_option', 'select_option', 'select'],
            'wait': ['browser_wait_for', 'wait_for', 'wait'],
            'screenshot': ['browser_take_screenshot', 'take_screenshot', 'screenshot'],
            'network': ['browser_network_requests', 'network_requests'],
            'console': ['browser_console_messages', 'console_messages'],
            'evaluate': ['browser_evaluate', 'evaluate'],
            'run_code': ['browser_run_code_unsafe', 'run_code_unsafe', 'run_code'],
        }
        names = set(self.tools)
        for semantic, candidates in specs.items():
            for candidate in candidates:
                if candidate in names:
                    aliases[semantic] = candidate
                    break
            if semantic not in aliases:
                for name in names:
                    normalized = name.lower()
                    if any(candidate in normalized for candidate in candidates):
                        aliases[semantic] = name
                        break
        return aliases

    def _tool(self, semantic_name: str) -> McpTool:
        tool_name = self.tool_aliases.get(semantic_name)
        if not tool_name or tool_name not in self.tools:
            raise McpProtocolError(f'MCP tool not available: {semantic_name}')
        return self.tools[tool_name]

    @staticmethod
    def _content_text(result: dict[str, Any]) -> str:
        chunks = []
        for item in result.get('content') or []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'text':
                chunks.append(str(item.get('text') or ''))
            elif item.get('text'):
                chunks.append(str(item.get('text')))
        structured = result.get('structuredContent') or result.get('structured_content')
        if structured:
            chunks.append(json.dumps(structured, ensure_ascii=False))
        return '\n'.join(chunks).strip()

    @staticmethod
    def is_snapshot_error_text(snapshot_text: str) -> bool:
        text = (snapshot_text or '').strip()
        if not text:
            return False
        return any(pattern in text for pattern in _MCP_SNAPSHOT_ERROR_PATTERNS)

    def _schema_args(self, tool: McpTool, values: dict[str, Any]) -> dict[str, Any]:
        properties = tool.properties
        if not properties:
            return {key: value for key, value in values.items() if value not in (None, '')}

        args: dict[str, Any] = {}
        for key in properties:
            if key in values and values[key] not in (None, ''):
                args[key] = values[key]
        if 'ref' in properties and 'ref' not in args and values.get('mcp_ref'):
            args['ref'] = values['mcp_ref']
        if 'target' in properties and 'target' not in args and values.get('mcp_ref'):
            args['target'] = values['mcp_ref']
        if 'element' in properties and 'element' not in args and values.get('element'):
            args['element'] = values['element']
        if 'text' in properties and 'text' not in args and values.get('text') is not None:
            args['text'] = values['text']
        if 'value' in properties and 'value' not in args and values.get('text') is not None:
            args['value'] = values['text']
        if 'values' in properties and 'values' not in args and values.get('text') is not None:
            args['values'] = [values['text']]
        if 'url' in properties and 'url' not in args and values.get('url'):
            args['url'] = values['url']
        if 'type' in properties and 'type' not in args and values.get('screenshot_type'):
            args['type'] = values['screenshot_type']
        if 'scale' in properties and 'scale' not in args and values.get('scale'):
            args['scale'] = values['scale']
        if 'boxes' in properties and 'boxes' not in args and 'boxes' in values:
            args['boxes'] = bool(values['boxes'])
        if 'static' in properties and 'static' not in args:
            args['static'] = False
        if 'level' in properties and 'level' not in args:
            args['level'] = 'info'
        return args

    async def call_tool(self, semantic_name: str, values: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        tool = self._tool(semantic_name)
        args = self._schema_args(tool, values or {})
        return await self.request('tools/call', {'name': tool.name, 'arguments': args})

    async def navigate(self, url: str) -> dict[str, Any]:
        return await self.call_tool('navigate', {'url': url})

    async def snapshot(self) -> dict[str, Any]:
        return await self.call_tool('snapshot', {'boxes': True})

    async def click(self, ref: str, element: str = '') -> dict[str, Any]:
        return await self.call_tool('click', {'mcp_ref': ref, 'ref': ref, 'element': element or ref})

    async def type_text(self, ref: str, text: str, element: str = '') -> dict[str, Any]:
        return await self.call_tool('type', {'mcp_ref': ref, 'ref': ref, 'element': element or ref, 'text': text})

    async def select_option(self, ref: str, value: str, element: str = '') -> dict[str, Any]:
        return await self.call_tool('select', {'mcp_ref': ref, 'ref': ref, 'element': element or ref, 'text': value})

    async def wait_for(self, text: str = '', time_seconds: Optional[float] = None) -> dict[str, Any]:
        return await self.call_tool('wait', {'text': text, 'time': time_seconds, 'timeout': time_seconds})

    async def screenshot(self, filename: str = '') -> dict[str, Any]:
        return await self.call_tool('screenshot', {
            'filename': filename,
            'path': filename,
            'screenshot_type': 'png',
            'scale': 'css',
        })

    async def network_requests(self) -> dict[str, Any]:
        return await self.call_tool('network', {})

    async def console_messages(self) -> dict[str, Any]:
        return await self.call_tool('console', {})

    async def run_code(self, code: str) -> dict[str, Any]:
        return await self.call_tool('run_code', {'code': code})

    @staticmethod
    def parse_snapshot_elements(snapshot_text: str) -> list[dict[str, Any]]:
        elements = []
        pattern = re.compile(
            r'(?P<prefix>[-*]\s*)?(?P<role>[A-Za-z_ -]+)\s+"(?P<name>[^"]*)"(?:[^\n]*?)\[ref=(?P<ref>[^\]\s]+)\]',
            re.IGNORECASE,
        )
        for index, match in enumerate(pattern.finditer(snapshot_text), start=1):
            role = ' '.join(match.group('role').split()).lower().lstrip('-* ').strip()
            name = match.group('name').strip()
            ref = match.group('ref').strip()
            actions = ['click']
            if role in {'textbox', 'input', 'searchbox', 'combobox'}:
                actions = ['fill']
            if role in {'checkbox', 'radio'}:
                actions = ['check']
            if role in {'combobox', 'select'}:
                actions = ['select_option']
            semantic_locator = None
            if role in {'textbox', 'input', 'searchbox'} and name:
                semantic_locator = {'type': 'placeholder', 'value': name, 'name': name, 'confidence': 0.88}
            elif role == 'button' and name:
                semantic_locator = {'type': 'role', 'value': 'button', 'role': 'button', 'name': name, 'confidence': 0.86}
            elif role and name:
                semantic_locator = {'type': 'role', 'value': role, 'role': role, 'name': name, 'confidence': 0.80}
            elements.append({
                'element_key': f'mcp_ref_{index}',
                'mcp_ref': ref,
                'name': name or ref,
                'role': role,
                'accessible_name': name,
                'text': name,
                'visible': True,
                'enabled': True,
                'actions': actions,
                'locator_candidates': [
                    {'type': 'mcp_ref', 'value': ref, 'name': name, 'confidence': 0.98},
                    semantic_locator,
                    {'type': 'role', 'value': role, 'name': name, 'confidence': 0.84} if role and name else None,
                    {'type': 'text', 'value': name, 'confidence': 0.70} if name else None,
                ],
                'recommended_locator': semantic_locator or {'type': 'mcp_ref', 'value': ref, 'name': name, 'confidence': 0.98},
                'source': 'official_playwright_mcp_snapshot',
            })
        for element in elements:
            element['locator_candidates'] = [item for item in element['locator_candidates'] if item]
        return elements


class PlaywrightMcpHttpAdapter(PlaywrightMcpAdapter):
    """Streamable HTTP transport for the official Playwright MCP server."""

    def __init__(
        self,
        server_url: str = 'http://127.0.0.1:8931/mcp',
        command: str = 'npx',
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        request_timeout: float = 60.0,
        auto_start: bool = True,
    ):
        super().__init__(
            command=command,
            args=args or [
                '-y',
                '@playwright/mcp@latest',
                '--port',
                '8931',
                '--host',
                '127.0.0.1',
                '--caps=vision,pdf,devtools',
                '--headless',
                '--ignore-https-errors',
                *_default_executable_path_arg(),
            ],
            cwd=cwd,
            env=env,
            request_timeout=request_timeout,
        )
        self.server_url = server_url
        self.auto_start = auto_start
        self.session_id: str | None = None
        self._client: httpx.AsyncClient | None = None

    def _server_tcp_reachable(self) -> bool:
        parsed = urlparse(self.server_url)
        host = parsed.hostname or '127.0.0.1'
        port = parsed.port
        if not port:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    async def _wait_until_tcp_reachable(self, timeout_seconds: float = 20.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if self._server_tcp_reachable():
                return True
            if self.process and self.process.returncode is not None:
                return False
            await asyncio.sleep(0.25)
        return False

    def _host_header(self) -> str | None:
        parsed = urlparse(self.server_url)
        if parsed.hostname == '127.0.0.1':
            return f"localhost:{parsed.port}" if parsed.port else 'localhost'
        return None

    async def start(self):
        if self.auto_start and not self.process:
            if self.server_url.endswith(':8931/mcp'):
                port = _find_free_local_port()
                self.server_url = f'http://127.0.0.1:{port}/mcp'
                rewritten_args = []
                skip_next = False
                for index, item in enumerate(self.args):
                    if skip_next:
                        skip_next = False
                        continue
                    if item == '--port' and index + 1 < len(self.args):
                        rewritten_args.extend(['--port', str(port)])
                        skip_next = True
                    else:
                        rewritten_args.append(item)
                if '--port' not in rewritten_args:
                    rewritten_args.extend(['--port', str(port)])
                self.args = rewritten_args
            env = os.environ.copy()
            env.update(self.env)
            self.process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                cwd=self.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._stderr_task = asyncio.create_task(self._read_stderr())
            if not await self._wait_until_tcp_reachable(timeout_seconds=min(self.request_timeout, 30.0)):
                await self.close()
                raise McpProtocolError(f'Playwright MCP server did not become reachable: {self.server_url}')
        self._client = httpx.AsyncClient(timeout=self.request_timeout, trust_env=False)
        try:
            await self.initialize()
            await self.refresh_tools()
        except Exception:
            await self.close()
            raise

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._stderr_task:
            self._stderr_task.cancel()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None

    @staticmethod
    def _parse_http_mcp_response(response: httpx.Response) -> dict[str, Any]:
        text = response.text or ''
        content_type = response.headers.get('content-type', '')
        if 'text/event-stream' in content_type:
            data_lines = []
            for line in text.splitlines():
                if line.startswith('data:'):
                    data_lines.append(line.split(':', 1)[1].strip())
            if not data_lines:
                return {}
            return json.loads(data_lines[-1])
        if text.strip():
            return response.json()
        return {}

    async def request(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if not self._client:
            raise McpProtocolError('MCP HTTP client is not running')
        request_id = self._next_id
        self._next_id += 1
        payload = {
            'jsonrpc': '2.0',
            'id': request_id,
            'method': method,
            'params': params or {},
        }
        last_error: Exception | None = None
        for attempt in range(20):
            try:
                headers = {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/event-stream',
                }
                host_header = self._host_header()
                if host_header:
                    headers['Host'] = host_header
                if self.session_id:
                    headers['mcp-session-id'] = self.session_id
                async with self._client.stream('POST', self.server_url, headers=headers, json=payload) as response:
                    if response.status_code in {404, 409} and not self.session_id:
                        await asyncio.sleep(0.5)
                        continue
                    if response.is_error:
                        body = await response.aread()
                        detail = body.decode('utf-8', errors='replace')[:1000] if body else ''
                        if (
                            response.status_code in {400, 404}
                            and ('Session not found' in detail or 'Server not initialized' in detail)
                            and method != 'initialize'
                        ):
                            self.session_id = None
                            raise McpSessionLostError('MCP HTTP session 丢失，当前页面状态已失效，需要重启本轮采集')
                        raise McpProtocolError(
                            f'MCP HTTP {method} failed: status={response.status_code}, body={detail}'
                        )
                    if response.headers.get('mcp-session-id'):
                        self.session_id = response.headers['mcp-session-id']
                    message: dict[str, Any] = {}
                    content_type = response.headers.get('content-type', '')
                    if 'text/event-stream' in content_type:
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            if line.startswith('data:'):
                                event = json.loads(line.split(':', 1)[1].strip())
                                if event.get('id') == request_id:
                                    message = event
                                    break
                                logger.debug(f"MCP HTTP 忽略非当前请求事件: {event}")
                    else:
                        body = await response.aread()
                        message = json.loads(body.decode('utf-8')) if body else {}
                if not message:
                    raise McpProtocolError(f'MCP HTTP {method} returned no JSON-RPC result for request id {request_id}')
                if message.get('error'):
                    raise McpProtocolError(f"MCP {method} error: {message['error']}")
                result = message.get('result')
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                last_error = exc
                if attempt < 19:
                    await asyncio.sleep(0.5)
                    continue
                raise
        raise McpProtocolError(f'MCP HTTP request failed: {method}: {last_error}')

    async def notify(self, method: str, params: Optional[dict[str, Any]] = None):
        if not self._client:
            raise McpProtocolError('MCP HTTP client is not running')
        payload = {
            'jsonrpc': '2.0',
            'method': method,
            'params': params or {},
        }
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }
        host_header = self._host_header()
        if host_header:
            headers['Host'] = host_header
        if self.session_id:
            headers['mcp-session-id'] = self.session_id
        await self._client.post(self.server_url, headers=headers, json=payload)
