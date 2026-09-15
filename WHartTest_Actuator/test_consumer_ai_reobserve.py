import json
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from consumer import TaskConsumer
from models import CaseResultModel, StepResultModel


class TaskConsumerAiReobserveTests(unittest.TestCase):
    def test_page_step_config_preserves_runtime_input_binding(self):
        consumer = TaskConsumer.__new__(TaskConsumer)

        config = consumer._build_page_step_config({
            'id': 7,
            'page_url': 'https://example.test/login',
            'page_name': '登录页',
            'step_details': [{
                'id': 667,
                'ope_key': 'fill',
                'locator_type': 'placeholder',
                'locator_value': '请输入验证码',
                'ope_value': {
                    'text': '',
                    'value': '',
                    'binding_mode': 'runtime_input',
                    'runtime_resolver': 'image_ocr',
                },
            }],
        })

        step = config.steps[0]
        self.assertEqual(step.input_value, '')
        self.assertEqual(step.binding_mode, 'runtime_input')
        self.assertEqual(step.runtime_resolver, 'image_ocr')

    def test_extracts_official_playwright_resource_snapshot_network_entries(self):
        event = {
            'type': 'resource-snapshot',
            'snapshot': {
                '_requestId': 'request-1',
                '_resourceType': 'fetch',
                'request': {'method': 'POST', 'url': 'https://example.test/api/tasks'},
                'response': {
                    'status': 400,
                    'content': {'mimeType': 'application/json', '_sha1': 'body-sha1'},
                },
            },
        }
        with tempfile.NamedTemporaryFile(suffix='.zip') as trace_file:
            with zipfile.ZipFile(trace_file.name, 'w') as archive:
                archive.writestr('trace.network', json.dumps(event))
                archive.writestr('resources/body-sha1', '{"message":"validation failed"}')

            summary = TaskConsumer._extract_trace_network_summary(trace_file.name)

        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['by_method'], {'POST': 1})
        self.assertEqual(summary['by_status'], {'4xx': 1})
        self.assertEqual(summary['failed'][0]['resource_type'], 'fetch')
        self.assertIn('validation failed', summary['failed'][0]['response_body'])

    def test_extracts_failed_step_marker_and_replay_prefix_from_spec_line(self):
        spec = "\n".join([
            "import { test } from '@playwright/test';",
            "// WHART_AI_STEP {\"step_sort\": 1, \"operation\": \"click\", \"page_key\": \"page_3\", \"element_key\": \"button_12\", \"target_name\": \"新建测试任务\"}",
            "await app.click(page => page.getByRole(\"button\", { name: \"新建测试任务\" }));",
            "// WHART_AI_STEP {\"step_sort\": 2, \"operation\": \"fill\", \"page_key\": \"page_4\", \"element_key\": \"input_3\", \"target_name\": \"任务名称\"}",
            "await app.fill(page => page.getByPlaceholder(\"请输入任务名称\"), \"测试任务\");",
        ])
        failed_attempt = {'failure_classification': {'line': 5, 'category': 'locator'}}
        result = {
            'generated_case': {
                'steps': [
                    {'step_sort': 1, 'operation': 'click', 'page_key': 'page_3', 'element_key': 'button_12'},
                    {'step_sort': 2, 'operation': 'fill', 'page_key': 'page_4', 'element_key': 'input_3'},
                ],
            },
        }

        marker = TaskConsumer._extract_failed_spec_step_marker(spec, failed_attempt)
        replay_steps, failed_index = TaskConsumer._reobserve_replay_steps_before_marker(result, marker)

        self.assertEqual(marker['element_key'], 'input_3')
        self.assertEqual(failed_index, 1)
        self.assertEqual(len(replay_steps), 1)
        self.assertEqual(replay_steps[0]['element_key'], 'button_12')

    def test_failure_classifier_prefers_deepest_generated_spec_location(self):
        output = """
generated.spec.ts:5:3 - test declaration
Error: strict mode violation
    at /tmp/generated.spec.ts:48:75
"""

        failure = TaskConsumer._classify_typescript_spec_failure(output)

        self.assertEqual(failure['line'], 48)
        self.assertEqual(failure['column'], 75)

    def test_ensure_typescript_expect_import_adds_missing_named_import(self):
        spec = "import { test } from 'playwright/test';\nawait expect(locator).toBeVisible();\n"

        normalized = TaskConsumer._ensure_typescript_expect_import(spec)

        self.assertIn("import { test, expect } from 'playwright/test';", normalized)

    def test_typescript_success_is_authoritative_when_native_flow_failed(self):
        result = {
            'status': 'failed',
            'failure_category': 'state_dependency',
            'message': 'AI 生成步骤真实执行失败: Locator.fill 指向非输入元素',
        }
        verification = {
            'status': 'failed',
            'flow_execution': {
                'status': 'failed',
                'failed_steps': 1,
                'steps': [
                    {
                        'step_sort': 1,
                        'operation': 'fill',
                        'status': 'failed',
                        'message': 'Locator.fill 指向非输入元素',
                    }
                ],
            },
        }
        execution = {
            'status': 'success',
            'attempt': 1,
        }

        TaskConsumer._apply_typescript_execution_status(result, verification, execution, repairs=[])

        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['failure_category'], '')
        self.assertEqual(verification['status'], 'success')
        self.assertEqual(verification['typescript_spec_status'], 'success')
        self.assertEqual(verification['final_status_source'], 'typescript_playwright_spec')
        self.assertEqual(verification['flow_status_before_typescript_execution'], 'failed')
        self.assertEqual(verification['flow_execution']['status'], 'failed')

    def test_plan_validation_failure_blocks_typescript_success_verdict(self):
        result = {
            'status': 'success',
            'failure_category': '',
            'message': 'LLM 已基于元素地图生成可执行 Playwright 测试计划和脚本',
        }
        verification = {
            'status': 'success',
            'plan_validation': {
                'status': 'failed',
                'semantic_issues': [{
                    'reason': 'missing_data_entry_operation',
                    'message': '用户需求包含填写/选择/勾选类动作，但 LLM 步骤没有生成数据录入操作',
                }],
            },
        }
        execution = {
            'status': 'success',
            'attempt': 1,
        }

        TaskConsumer._apply_typescript_execution_status(result, verification, execution, repairs=[])

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_category'], 'llm_plan_validation')
        self.assertEqual(verification['status'], 'failed')
        self.assertEqual(verification['typescript_spec_status'], 'success')
        self.assertEqual(verification['final_status_source'], 'plan_validation')

    def test_detects_generated_typescript_spec_from_script_artifacts(self):
        result = {
            'verification_result': {
                'script_artifacts': {
                    'playwright_ts_files': {
                        'generated.spec.ts': "import { test } from '@playwright/test';",
                    },
                },
            },
            'generated_case': {},
        }

        self.assertTrue(TaskConsumer._has_generated_typescript_spec(result))

    def test_missing_typescript_spec_allows_native_flow_fallback(self):
        result = {
            'verification_result': {'script_artifacts': {}},
            'generated_case': {'steps': [{'operation': 'goto'}]},
        }

        self.assertFalse(TaskConsumer._has_generated_typescript_spec(result))

    def test_llm_plan_payload_preserves_complete_element_map_for_backend_compaction(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        elements = [
            {
                'element_key': f'element_{index}',
                'name': f'Element {index}',
                'actions': ['click'],
            }
            for index in range(80)
        ]
        elements.append({
            'element_key': 'late_submit',
            'name': 'Late Submit',
            'actions': ['click'],
        })
        result = {
            'element_map': {
                'pages': [{'page_key': 'page_1', 'elements': elements}],
            },
            'verification_result': {},
        }

        payload = consumer._build_llm_plan_payload(result)

        preserved = payload['element_map']['pages'][0]['elements']
        self.assertEqual(len(preserved), 81)
        self.assertEqual(preserved[-1]['element_key'], 'late_submit')

    def test_failure_reobserve_becomes_high_priority_element_map_page(self):
        result = {
            'element_map': {
                'pages': [{
                    'page_key': 'page_old',
                    'url': 'https://example.test/business',
                    'elements': [{'element_key': 'old_button'}],
                }],
            },
        }
        failure_reobserve = {
            'status': 'success',
            'page_state': {
                'page_key': 'repair_observe_1',
                'url': 'https://example.test/business',
                'elements': [{'element_key': 'fresh_combobox'}],
            },
        }

        TaskConsumer._promote_failure_reobserve_into_element_map(result, failure_reobserve)

        element_map = result['element_map']
        self.assertEqual(element_map['latest_runtime_page_key'], 'repair_observe_1')
        self.assertTrue(element_map['pages'][0]['runtime_fresh'])
        self.assertEqual(element_map['pages'][0]['elements'][0]['element_key'], 'fresh_combobox')


class TaskConsumerFullReplanTests(unittest.IsolatedAsyncioTestCase):
    class _AsyncPageContext:
        def __init__(self, page):
            self.page = page

        async def __aenter__(self):
            return self.page

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def test_successful_step_screenshot_is_embedded_in_execution_result(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        with tempfile.TemporaryDirectory() as directory:
            screenshot_path = Path(directory) / 'success.png'
            screenshot_path.write_bytes(b'png-evidence')
            result = CaseResultModel(
                case_id=14,
                status='success',
                steps=[StepResultModel(step_id=623, status='success', screenshot=str(screenshot_path))],
            )

            processed = await consumer._process_result_screenshots(result)

            self.assertTrue(processed.steps[0].screenshot.startswith('data:image/png;base64,'))
            self.assertFalse(screenshot_path.exists())

    def test_build_test_case_config_extracts_ai_execution_artifact(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        artifact = {
            'playwright_ts_spec': "import { test } from '@playwright/test';",
            'generated_case': {'steps': [{'operation': 'click'}]},
        }

        config = consumer._build_test_case_config({
            'id': 22,
            'name': 'applied-ai-case',
            'result_data': {'ai_execution_artifact': artifact},
            'case_step_details': [],
        })

        self.assertEqual(config.ai_execution_artifact['playwright_ts_spec'], artifact['playwright_ts_spec'])

    async def test_ai_generation_result_prefers_http_report_over_websocket_payload(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._api_post = AsyncMock(return_value={'status': 'success'})
        consumer.ws_client = SimpleNamespace(send_result=AsyncMock())

        submitted = await consumer._submit_ai_generation_result({
            'task_id': 68,
            'status': 'success',
            'element_map': {'pages': [{'elements': [{'name': 'large'}]}]},
        })

        self.assertTrue(submitted)
        consumer._api_post.assert_awaited_once()
        consumer.ws_client.send_result.assert_not_awaited()

    async def test_ai_generation_result_falls_back_to_websocket_when_http_report_fails(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._current_user = 'tester'
        consumer._api_post = AsyncMock(return_value=None)
        consumer.ws_client = SimpleNamespace(send_result=AsyncMock(return_value=True))

        submitted = await consumer._submit_ai_generation_result({'task_id': 68, 'status': 'failed'})

        self.assertTrue(submitted)
        consumer._api_post.assert_awaited_once()
        consumer.ws_client.send_result.assert_awaited_once()

    async def test_runtime_agent_ai_generation_uses_runtime_branch(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer.executor = SimpleNamespace(run_ai_generation_task=AsyncMock())
        consumer._run_runtime_agent_ai_generation = AsyncMock(return_value={
            'task_id': 78,
            'status': 'success',
            'execution_mode': 'runtime_agent',
            'generated_case': {'steps': [{'operation': 'assert_visible'}]},
            'verification_result': {'runtime_agent_execution': {'status': 'success'}},
        })
        consumer._submit_ai_generation_result = AsyncMock(return_value=True)

        await consumer.execute_ai_generation({
            'task_id': 78,
            'execution_mode': 'runtime_agent',
            'safety_policy': {'execution_mode': 'runtime_agent'},
        })

        consumer._run_runtime_agent_ai_generation.assert_awaited_once()
        consumer.executor.run_ai_generation_task.assert_not_awaited()
        submitted = consumer._submit_ai_generation_result.await_args.args[0]
        self.assertEqual(submitted['execution_mode'], 'runtime_agent')

    async def test_runtime_agent_planner_drops_contract_artifacts(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._api_post = AsyncMock(return_value={
            'plan': {
                'runtime_plan': {
                    'status': 'continue',
                    'next_step': {'operation': 'click'},
                },
                'generated_case': {'steps': [{'operation': 'click'}]},
                'test_plan': {'steps': [{'operation': 'click'}]},
                'plan_validation': {'status': 'success'},
                'runtime_context': {'contract_artifacts': False},
            },
        })

        plan = await consumer._plan_runtime_agent_with_llm(
            {'task_id': 91},
            {'current_candidates': []},
            [],
        )

        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_context']['contract_artifacts'], False)
        self.assertNotIn('generated_case', plan)
        self.assertNotIn('test_plan', plan)
        self.assertNotIn('plan_validation', plan)

    def test_runtime_observation_for_plan_preserves_observed_candidates(self):
        observation = TaskConsumer._runtime_observation_for_plan(
            {
                'page_key': 'runtime_page_1',
                'url': 'https://example.test/home',
                'title': '首页',
                'elements': [{
                    'element_key': 'menu_task',
                    'name': '测试任务',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'text', 'value': '测试任务', 'confidence': 0.7},
                    'locator_candidates': [{'type': 'role', 'value': 'menuitem', 'name': '测试任务'}],
                }],
                'frames': [{
                    'frame_key': 'frame_1',
                    'url': 'https://example.test/frame.html',
                    'elements': [{
                        'element_key': 'frame_1::add',
                        'name': '添加',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'text', 'value': '添加'},
                    }, {
                        'element_key': 'frame_1::select_1',
                        'name': '主叫彩印 被叫彩印',
                        'form_label': '投递方式',
                        'tag': 'select',
                        'role': 'combobox',
                        'actions': ['select_option'],
                        'selected_label': '主叫彩印',
                        'selected_value': 'number:1',
                        'options': [
                            {'label': '主叫彩印', 'value': 'number:1', 'selected': True},
                            {'label': '被叫彩印', 'value': 'number:2', 'selected': False},
                        ],
                        'recommended_locator': {'type': 'role', 'value': 'combobox', 'name': '主叫彩印 被叫彩印'},
                    }],
                }],
                'accessibility_snapshot': {
                    'status': 'success',
                    'node_count': 1,
                    'nodes': [{'ax_ref': 'ax_1', 'role': 'menuitem', 'name': '测试任务'}],
                },
            },
            [],
            page_key='runtime_page_1',
            fallback_url='https://example.test/home',
        )

        self.assertEqual(observation['elements'][0]['name'], '测试任务')
        self.assertEqual(observation['frames'][0]['elements'][0]['name'], '添加')
        select_element = observation['frames'][0]['elements'][1]
        self.assertEqual(select_element['form_label'], '投递方式')
        self.assertEqual(select_element['selected_label'], '主叫彩印')
        self.assertEqual(select_element['options'][0]['value'], 'number:1')
        self.assertTrue(select_element['options'][0]['selected'])
        self.assertEqual(observation['accessibility_snapshot']['nodes'][0]['name'], '测试任务')

    def test_runtime_element_map_for_execution_flattens_frame_elements(self):
        observation = {
            'page_key': 'runtime_page_28',
            'url': 'https://example.test/menu.html',
            'title': '新增内容',
            'current_candidates': [],
            'elements': [],
            'frames': [{
                'frame_key': 'frame_1',
                'url': 'https://example.test/content.html',
                'elements': [{
                    'element_key': 'select_1',
                    'name': '主叫彩印 被叫彩印',
                    'form_label': '投递方式',
                    'tag': 'select',
                    'role': 'combobox',
                    'actions': ['select_option'],
                    'selected_label': '主叫彩印',
                    'options': [{'label': '主叫彩印', 'value': 'number:1', 'selected': True}],
                    'recommended_locator': {'type': 'role', 'value': 'combobox', 'name': '主叫彩印 被叫彩印'},
                }],
            }],
        }

        element_map = TaskConsumer._runtime_element_map_for_execution({}, observation)

        self.assertEqual(element_map['pages'][0]['page_key'], 'runtime_page_28')
        self.assertEqual(element_map['pages'][0]['elements'][0]['element_key'], 'frame_1::select_1')
        self.assertEqual(element_map['pages'][0]['elements'][0]['context_type'], 'frame')
        self.assertEqual(element_map['pages'][0]['elements'][0]['frame_key'], 'frame_1')
        self.assertEqual(element_map['pages'][0]['elements'][0]['frame_url'], 'https://example.test/content.html')
        self.assertEqual(element_map['pages'][0]['elements'][0]['options'][0]['value'], 'number:1')

    async def test_runtime_agent_planner_payload_keeps_completed_action_ids_outside_history_window(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._api_post = AsyncMock(return_value={
            'plan': {
                'runtime_plan': {'status': 'continue', 'next_step': {'operation': 'click'}},
                'runtime_context': {'contract_artifacts': True},
            },
        })
        runtime_history = [
            {'action_id': f'gherkin_step_{index}', 'operation': 'click', 'status': 'success'}
            for index in range(1, 21)
        ]

        await consumer._plan_runtime_agent_with_llm(
            {'task_id': 91},
            {'current_candidates': []},
            runtime_history,
        )

        payload = consumer._api_post.await_args.args[1]
        self.assertEqual(len(payload['runtime_history']), 12)
        self.assertIn('gherkin_step_1', payload['completed_action_ids'])
        self.assertIn('gherkin_step_20', payload['completed_action_ids'])

    async def test_runtime_agent_completed_without_generated_steps_fails_after_reobserve_limit(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        page = SimpleNamespace(
            url='https://example.test/login',
            wait_for_load_state=AsyncMock(),
            wait_for_timeout=AsyncMock(),
        )
        consumer.executor = SimpleNamespace(
            screenshot_dir='/tmp',
            browser_session_with_trace=lambda *args, **kwargs: self._AsyncPageContext(page),
            _setup_page_listeners=lambda page: None,
            _ensure_authenticated_before_ai_flow=AsyncMock(return_value=[]),
            _ignore_https_errors_for_env=lambda env: True,
            _mcp_collect_page_state=AsyncMock(return_value={
                'page_key': 'runtime_page_1',
                'url': 'https://example.test/login',
                'title': '登录',
                'elements': [{'element_key': 'sso_button', 'name': 'SSO登录', 'actions': ['click']}],
                'frames': [],
            }),
            _select_task_relevant_elements=lambda args, state, limit=20: [
                {'element_key': 'sso_button', 'name': 'SSO登录', 'actions': ['click']},
            ],
            _normalise_ai_plan_operation=lambda step: step.get('operation') or '',
            _build_mcp_script=lambda args, env, plan: '',
            get_current_trace_path=lambda: '',
        )
        consumer._compact_llm_payload_value = lambda value: value
        consumer._plan_runtime_agent_with_llm = AsyncMock(return_value={
            'runtime_plan': {
                'status': 'completed',
                'reason': '当前为登录页，需求首个待执行动作是点击“SSO登录”按钮。',
                'next_step': {},
            },
        })

        result = await consumer._run_runtime_agent_ai_generation({
            'task_id': 78,
            'target_url': 'https://example.test/login',
            'safety_policy': {
                'execution_mode': 'runtime_agent',
                'max_steps': 6,
                'max_runtime_reobserve_rounds': 2,
            },
            'requirement_document': {
                'flow': [{'action_id': 'action_1', 'operation': 'click', 'target': 'SSO登录'}],
            },
        })

        runtime_execution = result['verification_result']['runtime_agent_execution']
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_category'], 'runtime_agent_plan_invalid')
        self.assertEqual(runtime_execution['status'], 'failed')
        self.assertEqual(runtime_execution['generated_steps'], [])
        self.assertIn('未产生任何可执行步骤', runtime_execution['reason'])

    async def test_runtime_agent_blocks_when_prerequisite_authentication_still_on_login_page(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        page = SimpleNamespace(url='https://example.test/login')
        consumer.executor = SimpleNamespace(
            screenshot_dir='/tmp',
            browser_session_with_trace=lambda *args, **kwargs: self._AsyncPageContext(page),
            _setup_page_listeners=lambda page: None,
            _ensure_authenticated_before_ai_flow=AsyncMock(return_value=[{
                'type': 'flow_auth_setup',
                'status': 'failed',
                'reason': '前置认证未完成，当前仍停留在登录页',
            }]),
            _ignore_https_errors_for_env=lambda env: True,
            _navigate_for_observation=AsyncMock(),
            _mcp_collect_page_state=AsyncMock(return_value={
                'page_key': 'runtime_page_1',
                'url': 'https://example.test/login',
                'title': '登录',
                'elements': [{'element_key': 'login', 'name': '登录', 'actions': ['click']}],
                'frames': [],
            }),
            _select_task_relevant_elements=lambda args, state, limit=20: [
                {'element_key': 'login', 'name': '登录', 'actions': ['click']},
            ],
            _looks_like_login_page=lambda state: True,
            _authentication_mode=lambda args, state: 'prerequisite',
            _authentication_contract_unavailable_reason=lambda args: '认证合同不可用',
            get_current_trace_path=lambda: '/tmp/runtime.zip',
        )
        consumer._compact_llm_payload_value = lambda value: value
        consumer._plan_runtime_agent_with_llm = AsyncMock()

        result = await consumer._run_runtime_agent_ai_generation({
            'task_id': 78,
            'target_url': 'https://example.test/login',
            'safety_policy': {
                'execution_mode': 'runtime_agent',
                'authentication_contract': {'mode': 'prerequisite'},
            },
        })

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_category'], 'permission')
        self.assertEqual(result['execution_mode'], 'runtime_agent')
        self.assertIn('前置认证未完成', result['message'])
        consumer._plan_runtime_agent_with_llm.assert_not_awaited()
        self.assertEqual(
            result['repair_history'][0]['type'],
            'runtime_agent_authentication_blocked',
        )

    async def test_runtime_agent_retries_reobserve_when_navigation_destroys_context(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        page = SimpleNamespace(
            url='https://example.test/app',
            wait_for_load_state=AsyncMock(),
            wait_for_timeout=AsyncMock(),
        )
        initial_state = {
            'page_key': 'runtime_page_1',
            'url': 'https://example.test/app',
            'title': '登录',
            'elements': [{'element_key': 'sso_button', 'name': 'SSO登录', 'actions': ['click']}],
            'frames': [],
        }
        post_click_state = {
            'page_key': 'runtime_page_2',
            'url': 'https://example.test/home',
            'title': '首页',
            'elements': [{'element_key': 'home', 'name': '首页', 'actions': ['click']}],
            'frames': [],
        }
        collect_state = AsyncMock(side_effect=[
            initial_state,
            Exception('Page.evaluate: Execution context was destroyed, most likely because of a navigation'),
            post_click_state,
        ])
        consumer.executor = SimpleNamespace(
            screenshot_dir='/tmp',
            browser_session_with_trace=lambda *args, **kwargs: self._AsyncPageContext(page),
            _setup_page_listeners=lambda page: None,
            _ensure_authenticated_before_ai_flow=AsyncMock(return_value=[]),
            _ignore_https_errors_for_env=lambda env: True,
            _mcp_collect_page_state=collect_state,
            _select_task_relevant_elements=lambda args, state, limit=20: state.get('elements') or [],
            _looks_like_login_page=lambda state: False,
            _authentication_mode=lambda args, state: 'authentication_then_business',
            _normalise_ai_plan_operation=lambda step: step.get('operation') or '',
            _execute_ai_plan_step=AsyncMock(return_value=(True, 'clicked', 'role=button[name=SSO登录]')),
            _build_mcp_script=lambda args, env, plan: 'script',
            get_current_trace_path=lambda: '',
        )
        consumer._compact_llm_payload_value = lambda value: value
        consumer._plan_runtime_agent_with_llm = AsyncMock(side_effect=[
            {
                'runtime_plan': {
                    'status': 'continue',
                    'reason': '点击 SSO 登录',
                    'next_step': {
                        'action_id': 'gherkin_step_1',
                        'operation': 'click',
                        'element_key': 'sso_button',
                        'target_name': 'SSO登录',
                        'locator_hint': {'type': 'role', 'value': 'button', 'name': 'SSO登录'},
                    },
                },
            },
            {
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '已进入首页',
                    'next_step': {},
                },
            },
        ])

        result = await consumer._run_runtime_agent_ai_generation({
            'task_id': 78,
            'target_url': 'https://example.test/app',
            'safety_policy': {
                'execution_mode': 'runtime_agent',
                'max_steps': 3,
                'authentication_contract': {'mode': 'authentication_then_business'},
            },
            'requirement_document': {
                'flow': [{'action_id': 'gherkin_step_1', 'operation': 'click', 'target': 'SSO登录'}],
            },
        })

        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['execution_mode'], 'runtime_agent')
        self.assertEqual(len(result['verification_result']['runtime_agent_execution']['generated_steps']), 1)
        self.assertGreaterEqual(collect_state.await_count, 3)
        page.wait_for_load_state.assert_any_await('domcontentloaded', timeout=8000)
        page.wait_for_load_state.assert_any_await('networkidle', timeout=2000)

    async def test_runtime_agent_recovers_when_locator_resolution_times_out(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        page = SimpleNamespace(
            url='https://example.test/app',
            wait_for_load_state=AsyncMock(),
            wait_for_timeout=AsyncMock(),
        )
        initial_state = {
            'page_key': 'runtime_page_1',
            'url': 'https://example.test/app',
            'title': '登录',
            'elements': [{'element_key': 'sso_button', 'name': 'SSO登录', 'actions': ['click']}],
            'frames': [],
        }
        after_retry_state = {
            'page_key': 'runtime_page_2',
            'url': 'https://example.test/home',
            'title': '首页',
            'elements': [{'element_key': 'home', 'name': '首页', 'actions': ['click']}],
            'frames': [],
        }
        collect_state = AsyncMock(side_effect=[
            initial_state,
            initial_state,
            after_retry_state,
        ])
        consumer.executor = SimpleNamespace(
            screenshot_dir='/tmp',
            browser_session_with_trace=lambda *args, **kwargs: self._AsyncPageContext(page),
            _setup_page_listeners=lambda page: None,
            _ensure_authenticated_before_ai_flow=AsyncMock(return_value=[]),
            _ignore_https_errors_for_env=lambda env: True,
            _mcp_collect_page_state=collect_state,
            _select_task_relevant_elements=lambda args, state, limit=20: state.get('elements') or [],
            _looks_like_login_page=lambda state: False,
            _authentication_mode=lambda args, state: 'authentication_then_business',
            _normalise_ai_plan_operation=lambda step: step.get('operation') or '',
            _execute_ai_plan_step=AsyncMock(side_effect=[
                TimeoutError('Timeout waiting for visible locator match: matched=0, scanned=0, prefer=first, last_error='),
                (True, 'clicked', 'role=button[name=SSO登录]'),
            ]),
            _build_mcp_script=lambda args, env, plan: 'script',
            get_current_trace_path=lambda: '',
        )
        consumer._compact_llm_payload_value = lambda value: value
        consumer._plan_runtime_agent_with_llm = AsyncMock(side_effect=[
            {
                'runtime_plan': {
                    'status': 'continue',
                    'reason': '点击 SSO 登录',
                    'next_step': {
                        'action_id': 'gherkin_step_1',
                        'operation': 'click',
                        'element_key': 'sso_button',
                        'target_name': 'SSO登录',
                        'locator_hint': {'type': 'role', 'value': 'button', 'name': 'SSO登录'},
                    },
                },
            },
            {
                'runtime_plan': {
                    'status': 'continue',
                    'reason': '再次点击 SSO 登录',
                    'next_step': {
                        'action_id': 'gherkin_step_1_retry',
                        'operation': 'click',
                        'element_key': 'sso_button',
                        'target_name': 'SSO登录',
                        'locator_hint': {'type': 'role', 'value': 'button', 'name': 'SSO登录'},
                    },
                },
            },
            {
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '已进入首页',
                    'next_step': {},
                },
            },
        ])

        result = await consumer._run_runtime_agent_ai_generation({
            'task_id': 78,
            'target_url': 'https://example.test/app',
            'safety_policy': {
                'execution_mode': 'runtime_agent',
                'max_steps': 4,
                'authentication_contract': {'mode': 'authentication_then_business'},
            },
            'requirement_document': {
                'flow': [{'action_id': 'gherkin_step_1', 'operation': 'click', 'target': 'SSO登录'}],
            },
        })

        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['execution_mode'], 'runtime_agent')
        self.assertEqual(len(result['verification_result']['runtime_agent_execution']['generated_steps']), 1)
        self.assertTrue(
            any(item.get('type') == 'runtime_agent_locator_unresolved' for item in result['repair_history'])
        )
        self.assertEqual(consumer.executor._execute_ai_plan_step.await_count, 2)
        self.assertGreaterEqual(collect_state.await_count, 3)

    def test_runtime_reobserve_primitive_is_not_case_step(self):
        step = {
            'operation': 'wait_and_reobserve',
            'value': 20000,
            'internal_runtime_primitive': True,
        }

        self.assertTrue(TaskConsumer._is_runtime_reobserve_step(step))
        self.assertEqual(TaskConsumer._runtime_wait_milliseconds(step), 5000)
        self.assertFalse(TaskConsumer._is_runtime_reobserve_step({'operation': 'click'}))

    async def test_ai_applied_case_execution_uses_typescript_artifact_runner(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._current_user = 'tester'
        consumer.ws_client = SimpleNamespace(send_result=AsyncMock())
        consumer.executor = SimpleNamespace(execute_test_case=AsyncMock())
        consumer._fetch_test_case = AsyncMock(return_value={
            'id': 22,
            'name': 'applied-ai-case',
            'project': 4,
            'result_data': {
                'ai_execution_artifact': {
                    'source_task_id': 70,
                    'playwright_ts_spec': "import { test } from '@playwright/test';",
                    'generated_case': {
                        'steps': [
                            {'operation': 'click'},
                            {'operation': 'select_option'},
                        ],
                    },
                },
            },
            'case_step_details': [{
                'page_step': {
                    'id': 9,
                    'name': 'legacy page step',
                    'page_url': '/legacy',
                    'step_details': [{
                        'id': 701,
                        'ope_key': 'select',
                        'locator_type': 'label',
                        'locator_value': 'combobox label',
                        'ope_value': {'value': 'option'},
                    }],
                },
            }],
        })
        consumer._fetch_env_config = AsyncMock(return_value={'name': 'env', 'base_url': 'https://example.test'})
        consumer._fetch_default_env_config = AsyncMock()
        consumer._init_data_processor = AsyncMock(return_value=None)
        consumer._process_result_screenshots = AsyncMock(side_effect=lambda result: result)
        consumer._upload_trace_file = AsyncMock(return_value='ui_traces/typescript.zip')

        async def run_typescript_artifact(result):
            result['status'] = 'success'
            result['message'] = 'TypeScript Playwright spec 真实执行成功'
            result['verification_result']['script_artifacts']['typescript_spec_execution'] = {
                'status': 'success',
                'duration': 0.12,
                'trace_path': 'ui_traces/typescript.zip',
            }

        consumer._run_typescript_spec_artifact = AsyncMock(side_effect=run_typescript_artifact)

        await consumer.execute_test_case({'case_id': 22, 'env_config_id': 8})

        consumer.executor.execute_test_case.assert_not_awaited()
        consumer._run_typescript_spec_artifact.assert_awaited_once()
        consumer._upload_trace_file.assert_not_awaited()
        sent = consumer.ws_client.send_result.await_args.args[1]
        self.assertEqual(sent['status'], 'success')
        self.assertEqual(sent['total_steps'], 2)
        self.assertEqual(len(sent['steps']), 2)
        self.assertEqual(sent['steps'][0]['description'], 'click')
        self.assertEqual(sent['steps'][1]['description'], 'select_option')
        self.assertEqual(sent['trace_path'], 'ui_traces/typescript.zip')

    async def test_server_trace_path_is_preserved_when_sending_case_result(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._current_user = 'tester'
        consumer.ws_client = SimpleNamespace(send_result=AsyncMock())
        consumer.executor = SimpleNamespace(execute_test_case=AsyncMock(return_value=CaseResultModel(
            case_id=31,
            status='success',
            total_steps=1,
            passed_steps=1,
            steps=[StepResultModel(step_id=1, status='success', description='step')],
            trace_path='ui_traces/20260824/already-uploaded.zip',
        )))
        consumer._fetch_test_case = AsyncMock(return_value={
            'id': 31,
            'name': 'normal-case',
            'project': 4,
            'result_data': {},
            'case_step_details': [],
        })
        consumer._fetch_env_config = AsyncMock(return_value={'name': 'env', 'base_url': 'https://example.test'})
        consumer._fetch_default_env_config = AsyncMock()
        consumer._init_data_processor = AsyncMock(return_value=None)
        consumer._process_result_screenshots = AsyncMock(side_effect=lambda result: result)
        consumer._upload_trace_file = AsyncMock(return_value=None)

        await consumer.execute_test_case({'case_id': 31, 'env_config_id': 8, 'execution_record_id': 1234})

        consumer._upload_trace_file.assert_not_awaited()
        sent = consumer.ws_client.send_result.await_args.args[1]
        self.assertEqual(sent['trace_path'], 'ui_traces/20260824/already-uploaded.zip')
        self.assertEqual(sent['execution_record_id'], 1234)

    def test_typescript_trace_becomes_primary_and_exploration_trace_is_retained(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        verification = {'trace_path': 'ui_traces/exploration.zip'}

        consumer._promote_typescript_execution_trace(
            verification,
            'ui_traces/typescript-execution.zip',
        )

        self.assertEqual(verification['trace_path'], 'ui_traces/typescript-execution.zip')
        self.assertEqual(verification['exploration_trace_path'], 'ui_traces/exploration.zip')

    async def test_typescript_failure_uses_local_llm_patch_before_full_replan(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer.typescript_spec_enabled = True
        consumer.typescript_spec_timeout = 30
        consumer._resolve_playwright_test_command = lambda: ['npx', 'playwright']
        consumer._upload_trace_file = AsyncMock(return_value='')
        consumer._collect_typescript_runtime_artifacts = AsyncMock(return_value={})
        consumer._collect_failure_reobserve_snapshot = AsyncMock(return_value={
            'status': 'success',
            'page_state': {
                'url': 'https://example.test/current',
                'elements': [{'element_key': 'fresh_target', 'actions': ['assert_visible']}],
            },
        })

        first_spec = "\n".join([
            "import { test } from '@playwright/test';",
            "// WHART_AI_STEP {\"step_sort\": 1, \"operation\": \"assert_visible\", \"page_key\": \"page_1\", \"element_key\": \"stale_target\"}",
            "await expect(page.locator('#stale')).toBeVisible();",
        ])
        patched_spec = "\n".join([
            "import { test } from '@playwright/test';",
            "// repaired by llm patch",
            "await expect(page.locator('#fresh')).toBeVisible();",
        ])
        consumer._request_llm_typescript_patch = AsyncMock(return_value=(
            patched_spec,
            {'generated.spec.ts': patched_spec},
            {'type': 'llm_typescript_spec_patch', 'status': 'success'},
        ))

        class FakeProcess:
            def __init__(self, returncode, stdout_text):
                self.returncode = returncode
                self._stdout_text = stdout_text

            async def communicate(self):
                return self._stdout_text.encode('utf-8'), b''

        processes = [
            FakeProcess(1, 'Error: locator timeout\n    at /tmp/generated.spec.ts:3:7'),
            FakeProcess(0, '1 passed'),
        ]

        async def create_process(*_args, **_kwargs):
            return processes.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            consumer._playwright_workspace_for_command = lambda _command, _task_id: Path(directory)
            result = {
                'task_id': 51,
                'status': 'success',
                'max_repair_rounds': 1,
                'generated_case': {
                    'steps': [{'step_sort': 1, 'operation': 'assert_visible'}],
                    'playwright_ts_spec': first_spec,
                },
                'verification_result': {
                    'status': 'success',
                    'script_artifacts': {
                        'playwright_ts_spec': first_spec,
                        'playwright_ts_files': {'generated.spec.ts': first_spec},
                    },
                },
                'element_map': {'pages': [{'page_key': 'page_1', 'elements': []}]},
            }

            with patch('consumer.asyncio.create_subprocess_exec', side_effect=create_process):
                await consumer._run_typescript_spec_artifact(result)

        execution = result['verification_result']['script_artifacts']['typescript_spec_execution']
        self.assertEqual(execution['status'], 'success')
        self.assertEqual(len(execution['attempts']), 2)
        self.assertEqual(execution['repair_round_count'], 1)
        self.assertIn('fresh', result['generated_case']['playwright_ts_spec'])
        consumer._request_llm_typescript_patch.assert_awaited_once()
        consumer._collect_failure_reobserve_snapshot.assert_awaited_once()

    async def test_incomplete_exploration_blocks_initial_llm_planning(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._api_post = AsyncMock()
        result = {
            'task_id': 39,
            'status': 'failed',
            'element_map': {
                'pages': [{'page_key': 'page_1', 'elements': []}],
                'exploration_coverage': {
                    'status': 'incomplete',
                    'required_stages': ['target_module'],
                    'completed_stages': [],
                    'missing_stages': ['target_module'],
                },
            },
        }

        planned = await consumer._plan_ai_generation_with_llm({}, result)

        self.assertEqual(planned['status'], 'failed')
        self.assertEqual(planned['failure_category'], 'exploration_coverage')
        self.assertIn('target_module', planned['message'])
        consumer._api_post.assert_not_awaited()

    async def test_deferred_entry_transition_allows_initial_llm_planning(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._api_post = AsyncMock(return_value={
            'llm_enabled': True,
            'test_plan': {'steps': [{'step_sort': 0, 'operation': 'fill'}]},
            'generated_case': {'steps': [{'step_sort': 0, 'operation': 'fill'}]},
            'plan_validation': {'status': 'success'},
        })
        result = {
            'task_id': 40,
            'status': 'success',
            'element_map': {
                'pages': [{
                    'page_key': 'page_1',
                    'elements': [{'element_key': 'account', 'actions': ['fill']}],
                }],
                'exploration_coverage': {
                    'status': 'deferred',
                    'planning_allowed': True,
                    'required_stages': ['business_goal'],
                    'completed_stages': [],
                    'missing_stages': ['business_goal'],
                },
            },
        }

        planned = await consumer._plan_ai_generation_with_llm({}, result)

        self.assertEqual(planned['status'], 'success')
        self.assertEqual(planned['failure_category'], '')
        consumer._api_post.assert_awaited_once()

    async def test_backend_exploration_coverage_gate_keeps_structured_failure(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        missing = [{
            'action_id': 'action_2',
            'operation': 'click',
            'target': '目标入口',
            'phase': 'open_form',
            'reason': 'required_action_unresolved',
            'required': True,
        }]
        consumer._api_post = AsyncMock(return_value={
            'llm_enabled': True,
            'failure_category': 'exploration_coverage',
            'error': '页面探索未覆盖必需动作的可绑定元素: action_2 目标入口',
            'test_plan': {'steps': []},
            'generated_case': {'steps': []},
            'plan_validation': {
                'status': 'failed',
                'semantic_issues': [{
                    'reason': 'exploration_coverage_required_binding_unresolved',
                    'message': '页面探索未覆盖必需动作的可绑定元素，已停止生成可执行计划',
                    'missing_required_actions': missing,
                }],
                'unresolved_steps': [],
            },
            'planning_pipeline': {
                'status': 'blocked',
                'blocked_stage': 'exploration_coverage_gate',
                'exploration_coverage': {
                    'planning_allowed': False,
                    'reason': 'required_binding_unresolved',
                    'missing_required_actions': missing,
                    'missing_action_count': 1,
                },
            },
        })
        result = {
            'task_id': 41,
            'status': 'success',
            'element_map': {
                'pages': [{'page_key': 'page_1', 'elements': [{'element_key': 'root'}]}],
                'exploration_coverage': {'status': 'success', 'planning_allowed': True},
            },
        }

        planned = await consumer._plan_ai_generation_with_llm({}, result)

        self.assertEqual(planned['status'], 'failed')
        self.assertEqual(planned['failure_category'], 'exploration_coverage')
        self.assertIn('目标入口', planned['message'])
        self.assertNotEqual(planned['failure_category'], 'llm_plan_validation')
        self.assertEqual(
            planned['element_map']['llm_exploration_coverage']['missing_required_actions'],
            missing,
        )
        self.assertEqual(planned['repair_history'][-1]['type'], 'exploration_coverage_gate')

    async def test_coverage_gate_failure_triggers_bounded_reexploration(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer.ws_client = SimpleNamespace(send_result=AsyncMock())
        consumer._current_user = None
        async def process_artifacts(args, result):
            verification = result.setdefault('verification_result', {})
            script_artifacts = verification.setdefault('script_artifacts', {})
            script_artifacts['typescript_spec_execution'] = {'status': 'skipped'}
            return result

        consumer._process_ai_generation_artifacts = AsyncMock(side_effect=process_artifacts)
        consumer._api_post = AsyncMock(side_effect=[
            {
                'llm_enabled': True,
                'failure_category': 'exploration_coverage',
                'error': '页面探索未覆盖必需动作的可绑定元素',
                'test_plan': {'steps': []},
                'generated_case': {'steps': []},
                'plan_validation': {
                    'status': 'failed',
                    'semantic_issues': [{
                        'reason': 'exploration_coverage_required_binding_unresolved',
                        'missing_required_actions': [{
                            'action_id': 'action_1',
                            'operation': 'click',
                            'target': '目标入口',
                        }],
                    }],
                    'unresolved_steps': [],
                },
                'planning_pipeline': {
                    'status': 'blocked',
                    'blocked_stage': 'exploration_coverage_gate',
                    'exploration_coverage': {
                        'planning_allowed': False,
                        'missing_required_actions': [{
                            'action_id': 'action_1',
                            'operation': 'click',
                            'target': '目标入口',
                        }],
                    },
                },
            },
            {
                'llm_enabled': True,
                'test_plan': {'steps': [{'step_sort': 0, 'operation': 'click'}]},
                'generated_case': {
                    'steps': [{'step_sort': 0, 'operation': 'click'}],
                    'playwright_ts_spec': "import { test } from '@playwright/test';",
                },
                'plan_validation': {'status': 'success'},
            },
        ])
        first_result = {
            'task_id': 42,
            'status': 'success',
            'element_map': {
                'pages': [{'page_key': 'page_1', 'elements': [{'element_key': 'root'}]}],
                'exploration_coverage': {'status': 'success', 'planning_allowed': True},
            },
        }
        second_result = {
            'task_id': 42,
            'status': 'success',
            'element_map': {
                'pages': [{'page_key': 'page_2', 'elements': [{'element_key': 'target'}]}],
                'exploration_coverage': {'status': 'success', 'planning_allowed': True},
            },
        }
        consumer.executor = SimpleNamespace(run_ai_generation_task=AsyncMock(side_effect=[first_result, second_result]))

        await consumer.execute_ai_generation({
            'task_id': 42,
            'target_url': 'https://example.test',
            'coverage_reexploration_rounds': 1,
        })

        self.assertEqual(consumer.executor.run_ai_generation_task.await_count, 2)
        reexplore_args = consumer.executor.run_ai_generation_task.await_args_list[1].args[0]
        self.assertIn('llm_exploration_coverage', reexplore_args)
        self.assertEqual(
            reexplore_args['llm_exploration_coverage']['missing_required_actions'][0]['target'],
            '目标入口',
        )
        sent_result = consumer.ws_client.send_result.await_args.args[1]
        self.assertEqual(sent_result['status'], 'success')

    async def test_manual_capture_map_does_not_trigger_coverage_reexploration(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        missing = [{
            'action_id': 'action_1',
            'operation': 'click',
            'target': '目标入口',
        }]
        consumer._plan_ai_generation_with_llm = AsyncMock(return_value={
            'task_id': 43,
            'status': 'failed',
            'failure_category': 'exploration_coverage',
            'message': '页面探索未覆盖必需动作的可绑定元素',
            'element_map': {
                'capture_mode': 'manual_capture',
                'pages': [{'page_key': 'manual_page_1'}],
            },
            'verification_result': {
                'llm_planning': {
                    'exploration_coverage': {
                        'missing_required_actions': missing,
                    },
                },
            },
        })
        consumer.executor = SimpleNamespace(run_ai_generation_task=AsyncMock())

        _active_args, result = await consumer._plan_with_coverage_reexploration(
            {
                'task_id': 43,
                'execution_mode': 'manual_map',
                'element_map_id': 99,
                'element_map_source': 'manual_capture',
                'element_map': {'capture_mode': 'manual_capture'},
                'coverage_reexploration_rounds': 2,
            },
            {
                'task_id': 43,
                'status': 'success',
                'element_map': {'capture_mode': 'manual_capture', 'pages': [{'page_key': 'manual_page_1'}]},
            },
        )

        consumer.executor.run_ai_generation_task.assert_not_called()
        self.assertEqual(result['failure_category'], 'exploration_coverage')
        self.assertIn('manual_element_map_missing_action_evidence', result['message'])
        self.assertEqual(
            result['verification_result']['manual_element_map_missing_action_evidence']['reason'],
            'manual_capture_mode_does_not_fallback_to_auto_exploration',
        )

    async def test_full_replan_payload_contains_previous_plan_and_runtime_failure_evidence(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._api_post = AsyncMock(return_value={
            'llm_enabled': True,
            'test_plan': {'steps': [{'step_sort': 0, 'operation': 'click'}]},
            'generated_case': {'steps': [{'step_sort': 0, 'operation': 'click'}]},
            'plan_validation': {'status': 'success'},
            'generated_typescript_spec': "import { test } from '@playwright/test';",
        })
        result = {
            'task_id': 35,
            'status': 'failed',
            'element_map': {
                'latest_runtime_page_key': 'repair_observe_1',
                'pages': [{
                    'page_key': 'repair_observe_1',
                    'runtime_fresh': True,
                    'elements': [{'element_key': 'fresh_target'}],
                }],
            },
            'test_plan': {'steps': [{'step_sort': 0, 'operation': 'goto'}]},
            'generated_case': {
                'steps': [{'step_sort': 0, 'operation': 'goto'}],
                'playwright_ts_spec': 'old spec must not be sent',
            },
            'verification_result': {
                'failure_reobserve': {
                    'status': 'success',
                    'page_state': {
                        'page_key': 'repair_observe_1',
                        'elements': [{'element_key': 'fresh_target'}],
                    },
                },
                'script_artifacts': {
                    'typescript_spec_execution': {
                        'status': 'failed',
                        'attempts': [{
                            'failure_classification': {'category': 'locator', 'line': 12},
                            'failed_spec_step_marker': {'element_key': 'old_target'},
                        }],
                    },
                },
            },
        }

        await consumer._plan_ai_generation_with_llm({}, result, phase='full_replan_round_1')

        payload = consumer._api_post.await_args.args[1]
        self.assertEqual(payload['planning_phase'], 'full_replan_round_1')
        self.assertEqual(
            payload['previous_candidate_plan']['generated_case']['steps'][0]['operation'],
            'goto',
        )
        self.assertNotIn('playwright_ts_spec', payload['previous_candidate_plan']['generated_case'])
        self.assertEqual(
            payload['verification_result']['typescript_execution']['failed_spec_step_marker']['element_key'],
            'old_target',
        )
        self.assertTrue(payload['element_map']['pages'][0]['runtime_fresh'])

    async def test_typescript_failure_triggers_full_replan_and_new_spec_execution(self):
        consumer = TaskConsumer.__new__(TaskConsumer)
        consumer._current_user = 'tester'
        consumer.executor = SimpleNamespace(run_ai_generation_task=AsyncMock(return_value={
            'task_id': 35,
            'status': 'success',
            'element_map': {'pages': [{'page_key': 'page_1', 'elements': []}]},
            'verification_result': {},
        }))
        consumer.ws_client = SimpleNamespace(send_result=AsyncMock())

        planned_specs = [
            "import { test } from '@playwright/test';\n// first plan",
            "import { test } from '@playwright/test';\n// fully replanned",
        ]
        planning_phases = []

        async def plan(_args, result, phase='initial'):
            planning_phases.append(phase)
            planned = deepcopy(result)
            planned['status'] = 'success'
            planned['generated_case'] = {
                'steps': [{'step_sort': 1, 'operation': 'click'}],
                'playwright_ts_spec': planned_specs[len(planning_phases) - 1],
            }
            planned['verification_result'] = {
                **(planned.get('verification_result') or {}),
                'script_artifacts': {
                    'playwright_ts_spec': planned_specs[len(planning_phases) - 1],
                },
            }
            return planned

        execution_round = 0

        async def process(_args, result):
            nonlocal execution_round
            execution_round += 1
            processed = deepcopy(result)
            success = execution_round == 2
            processed['status'] = 'success' if success else 'failed'
            processed['verification_result']['script_artifacts']['typescript_spec_execution'] = {
                'status': 'success' if success else 'failed',
                'attempts': [{
                    'attempt': 1,
                    'spec_hash': str(execution_round),
                    'failure_classification': {} if success else {'category': 'locator'},
                }],
            }
            if not success:
                processed['verification_result']['failure_reobserve'] = {
                    'status': 'success',
                    'page_state': {'url': 'https://example.test/business'},
                }
            return processed

        consumer._plan_ai_generation_with_llm = AsyncMock(side_effect=plan)
        consumer._process_ai_generation_artifacts = AsyncMock(side_effect=process)
        consumer._verify_ai_generation_flow_with_llm_replanning = AsyncMock()

        await consumer.execute_ai_generation({'task_id': 35, 'max_repair_rounds': 1})

        self.assertEqual(planning_phases, ['initial', 'full_replan_round_1'])
        self.assertEqual(execution_round, 2)
        consumer._verify_ai_generation_flow_with_llm_replanning.assert_not_awaited()
        sent_result = consumer.ws_client.send_result.await_args.args[1]
        self.assertEqual(sent_result['status'], 'success')
        self.assertIn('fully replanned', sent_result['generated_case']['playwright_ts_spec'])


if __name__ == '__main__':
    unittest.main()
