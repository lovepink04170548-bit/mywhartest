import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import async_playwright

from browser_installer import setup_playwright_env
from executor import PlaywrightExecutor, StepConfig, resolve_image_captcha
from mcp_playwright_adapter import McpProtocolError, PlaywrightMcpHttpAdapter


class PlaywrightExecutorMcpCollectionTest(unittest.IsolatedAsyncioTestCase):
    def test_requirement_route_ignores_gherkin_metadata_comments_and_then_assertions(self):
        executor = PlaywrightExecutor(headless=True)
        route = executor._extract_requirement_exploration_route(
            '@platform @enterprise-management @color-printing\n'
            'Feature: 子企业彩印内容管理\n'
            '  Background:\n'
            '    And 用户已进入“合作管理”->“子企业管理”页面\n'
            '  Scenario: 为指定子企业成功添加彩印内容\n'
            '    # 步骤1：进入指定子企业的彩印内容管理\n'
            '    And 用户在该子企业的操作列点击“名片彩印”按钮\n'
            '    Then 用户成功进入分组管理页面\n'
            '    When 用户点击“彩印内容”标签页\n'
            '    When 用户点击“添加”按钮\n'
        )

        self.assertEqual(
            [item['label'] for item in route],
            ['合作管理', '子企业管理', '名片彩印', '彩印内容', '添加'],
        )
        self.assertTrue(route[-1]['opens_form'])

    def test_requirement_route_prefers_structured_requirement_document_over_raw_requirement(self):
        executor = PlaywrightExecutor(headless=True)
        route = executor._extract_requirement_exploration_route({
            'requirement': '这段噪声文本应该被忽略',
            'requirement_document': {
                'source_requirement': 'When 用户点击“系统管理”按钮\nAnd 用户点击“账号管理”菜单\nWhen 用户点击“添加”按钮',
                'gherkin': 'Feature: ignored',
                'sources': [{'kind': 'natural_language', 'text': '同样应该被忽略'}],
            },
        })

        self.assertEqual([item['label'] for item in route], ['系统管理', '账号管理', '添加'])
        self.assertTrue(route[-1]['opens_form'])

    def test_list_filter_constraints_extracts_identifier_search_value(self):
        executor = PlaywrightExecutor(headless=True)
        constraints = executor._extract_list_filter_constraints({
            'gherkin': 'When 用户搜索查找企业编号为 "30002620" 的子企业\nAnd 用户点击“搜索”按钮',
        })

        self.assertEqual(constraints, [{'field': '企业编号', 'value': '30002620'}])

    def test_list_filter_constraints_prefers_structured_requirement_document(self):
        executor = PlaywrightExecutor(headless=True)
        constraints = executor._extract_list_filter_constraints({
            'requirement': '这段噪声文本应该被忽略',
            'requirement_document': {
                'source_requirement': 'When 用户搜索查找企业编号为 "30002620" 的子企业',
                'sources': [{'kind': 'gherkin', 'text': 'Then 结果列表只显示企业编号为 "30002620" 的记录'}],
            },
        })

        self.assertEqual(constraints, [{'field': '企业编号', 'value': '30002620'}])

    def test_exploration_coverage_keywords_prefers_structured_requirement_document(self):
        executor = PlaywrightExecutor(headless=True)
        keywords = executor._extract_exploration_coverage_keywords({
            'requirement': '点击“噪声入口”按钮',
            'requirement_document': {
                'source_requirement': '进入“账号管理”页面\n填写“账号名称”字段\n点击“高级筛选”按钮',
            },
        })

        self.assertIn('账号管理', keywords['page'])
        self.assertIn('账号名称', keywords['field'])
        self.assertIn('高级筛选', keywords['action'])
        self.assertNotIn('噪声入口', keywords.get('action', []))

    def test_backend_missing_required_actions_feed_next_exploration_keywords(self):
        executor = PlaywrightExecutor(headless=True)
        keywords = executor._llm_exploration_coverage_keywords({
            'llm_exploration_coverage': {
                'missing_required_actions': [
                    {'operation': 'fill', 'target': '对象编号'},
                    {'operation': 'click', 'target': '目标入口'},
                    {'operation': 'assert_visible', 'target': '结果列表', 'value': '30002620'},
                ],
            },
        })

        self.assertEqual(keywords['field'], ['对象编号'])
        self.assertEqual(keywords['action'], ['目标入口'])
        self.assertEqual(keywords['page'], ['结果列表', '30002620'])

    def test_extract_login_credentials_prefers_structured_requirement_document(self):
        executor = PlaywrightExecutor(headless=True)
        credentials = executor._extract_login_credentials({
            'requirement': '这段噪声文本应该被忽略',
            'requirement_document': {
                'source_requirement': '用户名：alice\n密码：s3cr3t',
                'sources': [{'kind': 'natural_language', 'text': '用户名：alice\n密码：s3cr3t'}],
            },
        })

        self.assertEqual(credentials, {'username': 'alice', 'password': 's3cr3t'})

    def test_exploration_targets_merge_backend_coverage_keywords_without_business_rules(self):
        executor = PlaywrightExecutor(headless=True)
        targets = executor._build_exploration_targets({
            'gherkin': 'When user click "Menu"\nAnd user click "Create" button',
            'llm_exploration_coverage': {
                'missing_required_actions': [
                    {'operation': 'fill', 'target': '对象编号'},
                    {'operation': 'click', 'target': '目标入口'},
                ],
            },
        })

        form_target = targets[-1]
        self.assertEqual(form_target['stage'], 'open_business_form')
        self.assertIn('对象编号', form_target['coverage_keywords']['field'])
        self.assertIn('目标入口', form_target['coverage_keywords']['action'])

    def test_authentication_mode_prefers_contract_over_login_page_guess(self):
        executor = PlaywrightExecutor(headless=True)
        mode = executor._authentication_mode(
            {
                'authentication_contract': {
                    'mode': 'authentication_then_business',
                    'source': 'explicit',
                },
            },
            {
                'elements': [
                    {'name': '用户名', 'actions': ['fill']},
                    {'name': '密码', 'input_type': 'password', 'actions': ['fill']},
                    {'name': '登录', 'actions': ['click']},
                ],
            },
        )

        self.assertEqual(mode, 'authentication_then_business')

    async def test_explicit_authentication_step_uses_first_structured_route_action(self):
        executor = PlaywrightExecutor(headless=True)
        first_state = {
            'page_key': 'page_1',
            'url': 'https://example.test/login',
            'elements': [
                {
                    'element_key': 'username',
                    'name': '账号',
                    'tag': 'input',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'password',
                    'name': '密码',
                    'tag': 'input',
                    'input_type': 'password',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'sso_login',
                    'name': 'SSO登录',
                    'role': 'button',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'SSO登录'},
                },
            ],
        }
        post_login_state = {
            'page_key': 'page_2',
            'url': 'https://example.test/home',
            'elements': [{'element_key': 'menu', 'name': '系统管理', 'role': 'menuitem', 'actions': ['click']}],
        }
        observations = []
        page = MagicMock()
        page.url = first_state['url']
        page.wait_for_url = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        with (
            patch.object(executor, '_click_collected_element', new=AsyncMock(return_value=(True, ''))) as click_element,
            patch.object(executor, '_mcp_collect_page_state', new=AsyncMock(return_value=post_login_state)),
        ):
            success, state, error = await executor._perform_explicit_authentication_step(
                page,
                first_state,
                {
                    'requirement_document': {
                        'sources': [{
                            'kind': 'gherkin',
                            'steps': [{'keyword': 'When', 'text': '点击"SSO登录"按钮'}],
                        }],
                    },
                    'gherkin': 'When 点击"SSO登录"按钮',
                },
                {},
                observations,
                67,
            )

        self.assertTrue(success, error)
        self.assertEqual(state, post_login_state)
        click_element.assert_awaited_once()
        self.assertEqual(click_element.await_args.args[1]['element_key'], 'sso_login')

    async def test_business_exploration_blocks_when_authentication_contract_is_unknown(self):
        executor = PlaywrightExecutor(headless=True)
        first_state = {
            'page_key': 'page_1',
            'url': 'https://example.test/login',
            'elements': [
                {
                    'element_key': 'username',
                    'name': '账号',
                    'tag': 'input',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'password',
                    'name': '密码',
                    'tag': 'input',
                    'input_type': 'password',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'login',
                    'name': '登录',
                    'role': 'button',
                    'actions': ['click'],
                },
            ],
        }
        observations = []
        page = MagicMock()
        page.url = first_state['url']

        with patch.object(executor, '_auto_login_if_possible', new=AsyncMock()) as auto_login:
            pages, transitions = await executor._auto_explore_business_pages(
                page=page,
                task_id=55,
                first_state=first_state,
                args={
                    'authentication_mode': 'unknown',
                    'authentication_contract': {
                        'mode': 'unknown',
                        'status': 'degraded',
                        'source': 'llm_error_default',
                        'rationale': 'LLM 分类失败，认证阶段角色不确定',
                    },
                    'target_module': '业务对象管理',
                    'requirement': '新增业务对象配置',
                },
                safety_policy={},
                base_url=first_state['url'],
                max_pages=20,
                max_steps=50,
                observations=observations,
            )

        auto_login.assert_not_awaited()
        self.assertEqual(pages, [first_state])
        self.assertEqual(transitions, [])
        coverage = next(item for item in observations if item['type'] == 'business_exploration_coverage')
        self.assertEqual(coverage['status'], 'deferred')
        self.assertFalse(coverage['planning_allowed'])
        self.assertEqual(coverage['failure_category'], 'permission')
        self.assertEqual(coverage['blocked_by'], 'authentication')
        self.assertIn('认证合同不可用', coverage['reason'])

    def test_navigation_affinity_ignores_generic_management_suffix(self):
        executor = PlaywrightExecutor(headless=True)

        score = executor._navigation_target_affinity('分省企业管理', {
            'keywords': ['彩印内容', '添加', '子企业管理'],
        })

        self.assertEqual(score, 0)

    def test_coverage_reconciliation_marks_route_prefixes_from_later_form_stage(self):
        executor = PlaywrightExecutor(headless=True)
        targets = [
            {'stage': 'requirement_route_1', 'keywords': ['Admin'], 'route_path': ['Admin']},
            {'stage': 'requirement_route_2', 'keywords': ['Users'], 'route_path': ['Admin', 'Users']},
            {'stage': 'requirement_route_3', 'keywords': ['Profile'], 'route_path': ['Admin', 'Users', 'Profile']},
            {'stage': 'open_business_form', 'keywords': ['Create'], 'route_path': ['Admin', 'Users', 'Profile', 'Create']},
        ]

        completed, added = executor._reconcile_completed_exploration_stages(
            targets,
            ['requirement_route_1', 'open_business_form'],
            [],
        )

        self.assertEqual(
            completed,
            ['requirement_route_1', 'open_business_form', 'requirement_route_2', 'requirement_route_3'],
        )
        self.assertEqual(added, ['requirement_route_2', 'requirement_route_3'])

    def test_coverage_reconciliation_uses_observed_transition_target_for_route_stage(self):
        executor = PlaywrightExecutor(headless=True)
        targets = [
            {'stage': 'requirement_route_1', 'keywords': ['Admin'], 'route_path': ['Admin']},
            {'stage': 'requirement_route_2', 'keywords': ['Users'], 'route_path': ['Admin', 'Users']},
            {'stage': 'open_business_form', 'keywords': ['Create'], 'route_path': ['Admin', 'Users', 'Create']},
        ]

        completed, added = executor._reconcile_completed_exploration_stages(
            targets,
            ['requirement_route_1'],
            [{'stage': 'open_business_form', 'target_name': 'Users'}],
        )

        self.assertEqual(completed, ['requirement_route_1', 'requirement_route_2'])
        self.assertEqual(added, ['requirement_route_2'])

    def test_exploration_elements_include_frame_controls_with_context(self):
        executor = PlaywrightExecutor(headless=True)
        state = {
            'elements': [{'element_key': 'main_1', 'name': 'Main', 'actions': ['click']}],
            'frames': [{
                'frame_key': 'frame_1',
                'name': 'business-frame',
                'url': 'https://example.test/business',
                'elements': [{
                    'element_key': 'button_1',
                    'name': 'Add item',
                    'actions': ['click'],
                }],
            }],
        }

        elements = executor._exploration_elements(state)

        self.assertEqual(len(elements), 2)
        frame_element = elements[1]
        self.assertEqual(frame_element['context_type'], 'frame')
        self.assertEqual(frame_element['frame_key'], 'frame_1')
        self.assertEqual(frame_element['frame_name'], 'business-frame')
        self.assertEqual(frame_element['frame_url'], 'https://example.test/business')
        self.assertEqual(frame_element['element_key'], 'frame_1::button_1')

    async def test_click_collected_element_uses_observed_frame_context(self):
        executor = PlaywrightExecutor(headless=True)
        page = MagicMock()
        page.main_frame = MagicMock()
        frame = MagicMock()
        frame.name = 'business-frame'
        frame.url = 'https://example.test/business'
        page.frames = [page.main_frame, frame]
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        visible_locator = MagicMock()
        visible_locator.is_visible = AsyncMock(return_value=True)
        visible_locator.click = AsyncMock()
        locator.nth.return_value = visible_locator
        frame.get_by_role.return_value = locator
        page.wait_for_load_state = AsyncMock()

        success, error = await executor._click_collected_element(page, {
            'context_type': 'frame',
            'frame_name': 'business-frame',
            'frame_url': frame.url,
            'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Add item'},
        })

        self.assertTrue(success, error)
        frame.get_by_role.assert_called_once_with('button', name='Add item')
        visible_locator.click.assert_awaited_once()

    async def test_navigation_ready_does_not_require_network_idle(self):
        executor = PlaywrightExecutor(headless=True)
        page = MagicMock()
        page.goto = AsyncMock(return_value=None)
        page.wait_for_load_state = AsyncMock(side_effect=TimeoutError('polling never becomes idle'))
        page.wait_for_timeout = AsyncMock()

        await executor._navigate_for_observation(page, 'https://example.test/app')

        page.goto.assert_awaited_once_with(
            'https://example.test/app',
            wait_until='domcontentloaded',
            timeout=max(executor.action_timeout, executor.launch_timeout, 45000),
        )
        page.wait_for_load_state.assert_awaited_once_with('networkidle', timeout=3000)
        page.wait_for_timeout.assert_awaited_once()

    def test_frame_form_controls_satisfy_business_form_coverage(self):
        executor = PlaywrightExecutor(headless=True)
        state = {
            'elements': [],
            'frames': [{
                'frame_key': 'frame_1',
                'name': 'business-frame',
                'url': 'https://example.test/business',
                'elements': [
                    {'name': 'Item name', 'required': True, 'actions': ['fill']},
                    {'name': 'Save', 'role': 'button', 'actions': ['click']},
                ],
            }],
        }

        self.assertTrue(executor._is_business_form_covered(state, {}))

    async def test_native_collection_includes_angular_click_controls(self):
        setup_playwright_env()
        executor = PlaywrightExecutor(headless=True)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content('<div ng-click="openCooperation()">合作管理</div>')
            with tempfile.NamedTemporaryFile(suffix='.png') as screenshot:
                state = await executor._mcp_collect_page_state(
                    page,
                    'angular_menu',
                    screenshot.name,
                    max_elements=20,
                )
            await browser.close()

        cooperation = next(
            (item for item in state['elements'] if item.get('name') == '合作管理'),
            None,
        )
        self.assertIsNotNone(cooperation)
        self.assertEqual(cooperation['actions'], ['click'])
        self.assertEqual(cooperation['recommended_locator']['type'], 'text')

    async def test_frame_collection_filters_hidden_controls_before_limit(self):
        setup_playwright_env()
        executor = PlaywrightExecutor(headless=True)
        hidden_buttons = ''.join(
            f'<button style="display:none">Hidden {index}</button>'
            for index in range(90)
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content('<iframe id="business"></iframe>')
            frame = page.frames[1]
            await frame.set_content(
                f'''
                <form>
                  <input placeholder="Filter value">
                  <button type="submit">Filter</button>
                </form>
                {hidden_buttons}
                <div class="toolbar">
                  <button type="button">Primary action</button>
                </div>
                '''
            )
            with tempfile.NamedTemporaryFile(suffix='.png') as screenshot:
                state = await executor._mcp_collect_page_state(
                    page,
                    'iframe_toolbar',
                    screenshot.name,
                    max_elements=20,
                )
            await browser.close()

        frame_elements = state['frames'][0]['elements']
        names = [item.get('name') for item in frame_elements]
        self.assertIn('Primary action', names)
        self.assertNotIn('Hidden 1', names)

    def test_form_entry_probe_prefers_visible_button_outside_filter_form(self):
        executor = PlaywrightExecutor(headless=True)
        target = {'stage': 'open_business_form', 'blocked_actions': []}
        outside_form_button = {
            'tag': 'button',
            'role': 'button',
            'name': 'Primary action',
            'visible': True,
            'enabled': True,
            'actions': ['click'],
            'bounding_box': {'x': 20, 'y': 240, 'width': 120, 'height': 32},
        }
        inside_form_button = {
            **outside_form_button,
            'name': 'Filter',
            'in_form': True,
            'bounding_box': {'x': 20, 'y': 120, 'width': 120, 'height': 32},
        }

        self.assertGreater(
            executor._rank_form_entry_probe(outside_form_button, target, {}),
            0,
        )
        self.assertEqual(
            executor._rank_form_entry_probe(inside_form_button, target, {}),
            -1,
        )

    async def test_auto_login_checks_visible_consent_control(self):
        executor = PlaywrightExecutor(headless=True)
        page = MagicMock()
        consent_locator = MagicMock()
        consent_locator.is_checked = AsyncMock(return_value=False)
        consent_locator.check = AsyncMock()
        observations = []
        elements = [{
            'element_key': 'agreement',
            'name': '同意用户服务协议和用户隐私政策',
            'role': 'checkbox',
            'actions': ['check', 'uncheck'],
            'recommended_locator': {'type': 'label', 'value': '同意用户服务协议和用户隐私政策'},
        }]

        with (
            patch.object(executor, '_candidate_locator', return_value=MagicMock()),
            patch.object(
                executor,
                '_first_visible_locator',
                new=AsyncMock(return_value=consent_locator),
            ),
        ):
            checked = await executor._check_auto_login_consents(page, elements, observations)

        self.assertTrue(checked)
        consent_locator.check.assert_awaited_once()
        self.assertEqual(observations[0]['type'], 'auto_login_consent')
        self.assertEqual(observations[0]['status'], 'success')

    def test_auto_login_prefers_submit_button_over_login_mode_tabs(self):
        executor = PlaywrightExecutor(headless=True)
        submit = {'tag': 'button', 'role': 'button', 'name': '登录', 'actions': ['click']}
        account_tab = {'tag': 'button', 'role': 'button', 'name': '账号密码登录', 'actions': ['click']}
        sms_tab = {'tag': 'button', 'role': 'button', 'name': '手机验证码登录', 'actions': ['click']}

        self.assertGreater(
            executor._score_login_submit_element(submit),
            executor._score_login_submit_element(account_tab),
        )
        self.assertGreater(
            executor._score_login_submit_element(submit),
            executor._score_login_submit_element(sms_tab),
        )

    async def test_auto_login_runtime_captcha_uses_image_ocr(self):
        executor = PlaywrightExecutor(headless=True)
        page = MagicMock()
        captcha_locator = MagicMock()
        observations = []
        elements = [{
            'element_key': 'input_3',
            'name': '填写验证码',
            'input_type': 'code',
            'actions': ['fill'],
            'recommended_locator': {'type': 'placeholder', 'value': '填写验证码'},
        }]

        with (
            patch.object(executor, '_candidate_locator', return_value=MagicMock()),
            patch.object(
                executor,
                '_first_visible_locator',
                new=AsyncMock(return_value=captcha_locator),
            ),
            patch.object(
                executor,
                '_resolve_runtime_input',
                new=AsyncMock(return_value=('A7K9', '/tmp/captcha.png')),
            ) as resolver,
        ):
            resolved = await executor._resolve_auto_login_captcha(page, elements, observations)

        self.assertTrue(resolved)
        resolver.assert_awaited_once_with(page, captcha_locator, 'image_ocr', 'auto_login')
        self.assertEqual(observations[0]['type'], 'auto_login_runtime_input')
        self.assertEqual(observations[0]['status'], 'success')
        self.assertNotIn('value', observations[0])

    async def test_auto_login_without_image_captcha_skips_ocr(self):
        executor = PlaywrightExecutor(headless=True)
        page = MagicMock()
        observations = []
        elements = [{
            'element_key': 'input_1',
            'name': '用户名',
            'input_type': 'text',
            'actions': ['fill'],
        }]

        with patch.object(executor, '_resolve_runtime_input', new=AsyncMock()) as resolver:
            resolved = await executor._resolve_auto_login_captcha(page, elements, observations)

        self.assertFalse(resolved)
        resolver.assert_not_awaited()
        self.assertEqual(observations, [])

    async def test_runtime_image_input_is_resolved_before_fill(self):
        executor = PlaywrightExecutor(headless=True)
        page = MagicMock()
        locator = MagicMock()
        locator.wait_for = AsyncMock()

        step = StepConfig(
            step_id=667,
            operation_type='fill',
            locator_type='placeholder',
            locator_value='请输入验证码',
            binding_mode='runtime_input',
            runtime_resolver='image_ocr',
        )

        with (
            patch.object(executor, '_get_locator', return_value=locator),
            patch.object(
                executor,
                '_resolve_runtime_input',
                new=AsyncMock(return_value=('A7K9', '/tmp/captcha.png')),
            ) as resolver,
        ):
            success, message, artifact = await executor._execute_step(page, step)

        self.assertTrue(success)
        self.assertIn('image_ocr', message)
        self.assertEqual(artifact, '/tmp/captcha.png')
        resolver.assert_awaited_once_with(page, locator, 'image_ocr', step.step_id)

    async def test_static_empty_fill_is_rejected(self):
        executor = PlaywrightExecutor(headless=True)
        page = MagicMock()
        locator = MagicMock()
        locator.wait_for = AsyncMock()
        step = StepConfig(
            step_id=10,
            operation_type='fill',
            locator_type='placeholder',
            locator_value='请输入内容',
            input_value='',
        )

        with patch.object(executor, '_get_locator', return_value=locator):
            success, message, _ = await executor._execute_step(page, step)

        self.assertFalse(success)
        self.assertIn('输入值为空', message)

    async def test_runtime_image_resolver_screenshots_ocr_and_fills(self):
        with tempfile.TemporaryDirectory() as screenshot_dir:
            executor = PlaywrightExecutor(headless=True, screenshot_dir=screenshot_dir)
            container = MagicMock()
            input_locator = MagicMock()
            input_locator.fill = AsyncMock()
            captcha_visual = MagicMock()
            captcha_visual.screenshot = AsyncMock()

            with (
                patch.object(
                    executor,
                    '_nearest_runtime_image',
                    new=AsyncMock(return_value=captcha_visual),
                ),
                patch(
                    'executor.asyncio.to_thread',
                    new=AsyncMock(return_value={'status': 'success', 'value': 'A7K9'}),
                ) as to_thread,
            ):
                value, artifact = await executor._resolve_runtime_input(
                    container,
                    input_locator,
                    'image_ocr',
                    667,
                )

        self.assertEqual(value, 'A7K9')
        self.assertIn('runtime-inputs/image_ocr_step_667_1_', artifact)
        captcha_visual.screenshot.assert_awaited_once()
        to_thread.assert_awaited_once()
        self.assertIs(to_thread.await_args.args[0], resolve_image_captcha)
        input_locator.fill.assert_awaited_once_with('A7K9')

    def test_scoped_text_locator_limits_dialog_assertion(self):
        executor = PlaywrightExecutor(headless=True)
        container = MagicMock()
        dialog = MagicMock()
        container.get_by_role.return_value = dialog

        executor._get_locator(
            container,
            'text',
            '{"exact":true,"scope":{"role":"dialog"},"value":"使用指南"}',
        )

        container.get_by_role.assert_called_once_with('dialog')
        dialog.get_by_text.assert_called_once_with('使用指南', exact=True)

    def test_structured_role_locator_preserves_accessible_name(self):
        executor = PlaywrightExecutor(headless=True)
        container = MagicMock()

        executor._get_locator(
            container,
            'role',
            '{"role":"button","name":"登录","exact":true}',
        )

        container.get_by_role.assert_called_once_with('button', name='登录', exact=True)

    async def test_successful_step_captures_runtime_screenshot(self):
        with tempfile.TemporaryDirectory() as screenshot_dir:
            executor = PlaywrightExecutor(headless=True, screenshot_dir=screenshot_dir)
            page = MagicMock()
            page.wait_for_timeout = AsyncMock()
            page.screenshot = AsyncMock()

            screenshot = await executor._capture_runtime_step_screenshot(
                page,
                case_id=14,
                step_id=623,
                status='success',
            )

            self.assertTrue(screenshot.startswith(screenshot_dir))
            self.assertIn('success_14_623_', screenshot)
            page.screenshot.assert_awaited_once()

    def test_live_locator_remains_primary_when_merging_confirmed_baseline(self):
        executor = PlaywrightExecutor(headless=True)
        current = {
            'pages': [{
                'page_key': 'page_4',
                'elements': [{
                    'element_key': 'input_3',
                    'tag': 'input',
                    'name': '任务名称',
                    'placeholder': '请输入任务名称',
                    'recommended_locator': {
                        'type': 'placeholder',
                        'value': '请输入任务名称',
                        'confidence': 0.92,
                    },
                    'locator_candidates': [],
                }],
            }],
        }
        confirmed = {
            'pages': [{
                'page_key': 'page_4',
                'elements': [{
                    'element_key': 'input_3',
                    'tag': 'input',
                    'name': '任务名称',
                    'placeholder': '结束日期',
                    'recommended_locator': {
                        'type': 'placeholder',
                        'value': '结束日期',
                        'confidence': 0.92,
                    },
                }],
            }],
        }

        merged_count = executor._merge_existing_element_map_context(current, confirmed)

        self.assertEqual(merged_count, 1)
        element = current['pages'][0]['elements'][0]
        self.assertEqual(element['recommended_locator']['value'], '请输入任务名称')
        self.assertEqual(element['locator_candidates'][0]['value'], '请输入任务名称')

    def test_observed_form_locator_demotes_framework_generated_id(self):
        state = {
            'elements': [{
                'element_key': 'input_1',
                'role': 'combobox',
                'label': '环境',
                'actions': ['select_option'],
                'recommended_locator': {'type': 'id', 'value': 'el-id-1234-637', 'confidence': 0.82},
                'locator_candidates': [
                    {'type': 'id', 'value': 'el-id-1234-637', 'confidence': 0.82},
                ],
            }],
        }

        PlaywrightExecutor._stabilize_observed_locators(state)

        element = state['elements'][0]
        self.assertEqual(element['recommended_locator']['type'], 'role')
        self.assertEqual(element['recommended_locator']['name'], '环境')
        self.assertEqual(element['locator_candidates'][-1]['value'], 'el-id-1234-637')

    def test_page_local_element_key_cannot_cross_merge_into_another_control(self):
        executor = PlaywrightExecutor(headless=True)
        current = {
            'pages': [{
                'page_key': 'page_dialog',
                'elements': [{
                    'element_key': 'input_1',
                    'name': '测试环境',
                    'role': 'combobox',
                    'recommended_locator': {'type': 'role', 'value': 'combobox', 'name': '测试环境'},
                }],
            }],
        }
        confirmed = {
            'pages': [{
                'page_key': 'page_login',
                'elements': [{
                    'element_key': 'input_1',
                    'name': '请输入用户名',
                    'role': 'textbox',
                    'recommended_locator': {'type': 'placeholder', 'value': '请输入用户名'},
                }],
            }],
        }

        merged_count = executor._merge_existing_element_map_context(current, confirmed)

        self.assertEqual(merged_count, 0)
        self.assertNotIn('context_match', current['pages'][0]['elements'][0])

    async def test_http_mcp_adapter_starts_private_server_when_default_port_is_occupied(self):
        adapter = PlaywrightMcpHttpAdapter(
            server_url="http://127.0.0.1:8931/mcp",
            auto_start=True,
            request_timeout=5,
        )
        fake_process = MagicMock()
        fake_process.returncode = None
        fake_process.stderr.readline = AsyncMock(return_value=b'')
        fake_process.terminate = MagicMock()
        fake_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(adapter, "_server_tcp_reachable", return_value=True),
            patch("mcp_playwright_adapter._find_free_local_port", return_value=19001),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_process)) as create_process,
            patch.object(adapter, "_wait_until_tcp_reachable", new=AsyncMock(return_value=True)),
            patch.object(adapter, "initialize", new=AsyncMock()),
            patch.object(adapter, "refresh_tools", new=AsyncMock()),
        ):
            await adapter.start()
            self.assertEqual(adapter.server_url, "http://127.0.0.1:19001/mcp")
            self.assertIs(adapter.process, fake_process)
            create_process.assert_awaited()
            await adapter.close()

    async def test_official_mcp_collect_state_fails_on_snapshot_error_text(self):
        executor = PlaywrightExecutor(headless=True)

        class FakeAdapter:
            tools = {}
            tool_aliases = {}
            _content_text = staticmethod(PlaywrightMcpHttpAdapter._content_text)
            parse_snapshot_elements = staticmethod(PlaywrightMcpHttpAdapter.parse_snapshot_elements)
            is_snapshot_error_text = staticmethod(PlaywrightMcpHttpAdapter.is_snapshot_error_text)

            async def snapshot(self):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "### Error\nError: async initializeServer: Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome",
                        }
                    ]
                }

        with self.assertRaisesRegex(McpProtocolError, "snapshot 失败"):
            await executor._official_mcp_collect_state(FakeAdapter(), "page_1", "https://example.test")

    def test_mcp_snapshot_parser_strips_yaml_bullet_from_role(self):
        snapshot_text = """
### Snapshot
```yaml
- generic [ref=e2]:
  - textbox "请输入用户名" [ref=e15]
  - textbox "请输入密码" [ref=e25]
  - button "登录" [ref=e34]
```
"""

        elements = PlaywrightMcpHttpAdapter.parse_snapshot_elements(snapshot_text)

        self.assertEqual([item["role"] for item in elements], ["textbox", "textbox", "button"])
        self.assertEqual(elements[0]["actions"], ["fill"])
        self.assertEqual(elements[1]["actions"], ["fill"])
        self.assertEqual(elements[2]["actions"], ["click"])

    async def test_official_mcp_flow_session_loss_falls_back_to_native(self):
        executor = PlaywrightExecutor(headless=True, mcp_provider="official-playwright-mcp", mcp_transport="http")
        args = {"task_id": "task-1", "target_url": "https://example.test"}
        result = {
            "task_id": "task-1",
            "generated_case": {
                "steps": [
                    {
                        "step_sort": 0,
                        "operation": "goto",
                        "value": "https://example.test",
                        "description": "打开目标页面",
                    }
                ]
            },
            "test_plan": {
                "steps": [
                    {
                        "step_sort": 0,
                        "operation": "goto",
                        "value": "https://example.test",
                        "description": "打开目标页面",
                    }
                ]
            },
            "verification_result": {
                "status": "failed",
                "provider": "official-playwright-mcp",
                "flow_execution": {
                    "status": "failed",
                    "provider": "official-playwright-mcp",
                },
            },
            "mcp_observations": [],
            "element_map": {"base_url": "https://example.test"},
            "repair_history": [],
        }

        class FakeOfficialAdapter:
            tools = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def navigate(self, url):
                return None

        class FakePage:
            url = "https://example.test"

            async def goto(self, url, wait_until=None, timeout=None):
                return None

        @asynccontextmanager
        async def fake_browser_session_with_trace(*args, **kwargs):
            yield FakePage()

        with (
            patch.object(executor, "_official_mcp_adapter", return_value=FakeOfficialAdapter()),
            patch.object(
                executor,
                "_official_mcp_execute_plan_step",
                new=AsyncMock(return_value=(False, "MCP HTTP session 丢失，当前页面状态已失效，需要重启本轮采集", {})),
            ),
            patch.object(executor, "browser_session_with_trace", new=fake_browser_session_with_trace),
            patch.object(executor, "_setup_page_listeners", new=lambda page: None),
            patch.object(executor, "_ensure_authenticated_before_ai_flow", new=AsyncMock(return_value=[])),
            patch.object(executor, "_execute_ai_plan_step", new=AsyncMock(return_value=(True, "步骤执行成功: goto", None))),
            patch.object(executor, "_repair_and_execute_ai_plan_step", new=AsyncMock(return_value=(False, "", None, []))),
        ):
            fallback_result = await executor.verify_ai_generated_flow(args, result)

        self.assertEqual(fallback_result["status"], "success")
        self.assertIn("自动降级为原生 Playwright", fallback_result["message"])
        self.assertTrue(
            any(
                isinstance(item, dict) and item.get("type") == "official_mcp_flow_fallback"
                for item in fallback_result.get("mcp_observations") or []
            )
        )

    async def test_optional_element_map_verify_failures_do_not_fail_generation(self):
        executor = PlaywrightExecutor(headless=True)

        class FakeLocator:
            def __init__(self, should_fail: bool = False):
                self.should_fail = should_fail

            @property
            def first(self):
                return self

            async def wait_for(self, **kwargs):
                if self.should_fail:
                    raise TimeoutError("Timeout 3000ms exceeded.")
                return None

        class FakePage:
            url = "https://example.test/home/testingTask"

            def get_by_label(self, value):
                return FakeLocator(should_fail=value == "Page")

            def locator(self, selector):
                return FakeLocator(should_fail="Page" in selector)

        element_map = {
            "pages": [
                {
                    "url": "https://example.test/home/testingTask",
                    "page_key": "page_1",
                    "elements": [
                        {
                            "element_key": "input_1",
                            "name": "任务名称",
                            "required": True,
                            "visible": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "任务名称", "confidence": 0.9},
                            "locator_candidates": [{"type": "label", "value": "任务名称", "confidence": 0.9}],
                        },
                        {
                            "element_key": "input_2",
                            "name": "Page",
                            "required": False,
                            "visible": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "Page", "confidence": 0.9},
                            "locator_candidates": [{"type": "label", "value": "Page", "confidence": 0.9}],
                        },
                    ],
                }
            ]
        }

        verification_result, repairs, failed_items = await executor._verify_and_repair_element_map(
            FakePage(),
            element_map,
            max_repair_rounds=1,
        )

        self.assertEqual(verification_result["status"], "success")
        self.assertEqual(verification_result["failed_optional_elements"], 1)
        self.assertEqual(verification_result["failed_required_elements"], 0)
        self.assertEqual(len(failed_items), 1)
        self.assertEqual(repairs, [])

    async def test_mcp_flow_fallback_failure_preserves_native_failure_reason(self):
        executor = PlaywrightExecutor(headless=True, mcp_provider="official-playwright-mcp", mcp_transport="http")
        args = {"task_id": "task-2", "target_url": "https://example.test"}
        result = {
            "task_id": "task-2",
            "generated_case": {
                "steps": [
                    {
                        "step_sort": 0,
                        "operation": "click",
                        "target_name": "测试环境",
                        "description": "选择测试环境",
                    }
                ]
            },
            "test_plan": {
                "steps": [
                    {
                        "step_sort": 0,
                        "operation": "click",
                        "target_name": "测试环境",
                        "description": "选择测试环境",
                    }
                ]
            },
            "verification_result": {},
            "mcp_observations": [],
            "element_map": {"base_url": "https://example.test"},
            "repair_history": [],
        }

        class FakeOfficialAdapter:
            tools = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def navigate(self, url):
                return None

        class FakePage:
            url = "https://example.test"

            async def goto(self, url, wait_until=None, timeout=None):
                return None

        @asynccontextmanager
        async def fake_browser_session_with_trace(*args, **kwargs):
            yield FakePage()

        with (
            patch.object(executor, "_official_mcp_adapter", return_value=FakeOfficialAdapter()),
            patch.object(
                executor,
                "_official_mcp_execute_plan_step",
                new=AsyncMock(return_value=(False, "MCP HTTP session 丢失，当前页面状态已失效，需要重启本轮采集", {})),
            ),
            patch.object(executor, "browser_session_with_trace", new=fake_browser_session_with_trace),
            patch.object(executor, "_setup_page_listeners", new=lambda page: None),
            patch.object(executor, "_ensure_authenticated_before_ai_flow", new=AsyncMock(return_value=[])),
            patch.object(
                executor,
                "_execute_ai_plan_step",
                new=AsyncMock(return_value=(False, "原生步骤失败: 测试环境下拉框不可见", None)),
            ),
            patch.object(executor, "_repair_and_execute_ai_plan_step", new=AsyncMock(return_value=(False, "", None, []))),
        ):
            fallback_result = await executor.verify_ai_generated_flow(args, result)

        self.assertEqual(fallback_result["status"], "failed")
        self.assertIn("原生 Playwright 降级验证失败", fallback_result["message"])
        self.assertIn("测试环境下拉框不可见", fallback_result["message"])

    async def test_plan_navigation_observe_and_page_assert_do_not_require_locator_hint(self):
        executor = PlaywrightExecutor(headless=True)

        class FakeLocator:
            @property
            def first(self):
                return self

            async def wait_for(self, **kwargs):
                return None

        class FakePage:
            def __init__(self):
                self.goto_calls = []
                self.wait_for_load_state_calls = []
                self.wait_for_timeout_calls = []

            async def goto(self, url, wait_until=None, timeout=None):
                self.goto_calls.append((url, wait_until, timeout))

            async def wait_for_load_state(self, state, timeout=None):
                self.wait_for_load_state_calls.append((state, timeout))

            async def wait_for_timeout(self, timeout):
                self.wait_for_timeout_calls.append(timeout)

            def locator(self, selector):
                self.last_selector = selector
                return FakeLocator()

        page = FakePage()
        base_url = "https://example.test/login"
        safety_policy = {"url_allowlist": [], "url_blocklist": []}

        for step in [
            {"action": "navigate", "description": "打开目标页面"},
            {"action": "observe", "description": "采集页面状态"},
            {"action": "assert", "description": "验证目标页面可访问"},
        ]:
            success, message, locator = await executor._execute_ai_plan_step(
                page,
                step,
                base_url,
                safety_policy,
            )
            self.assertTrue(success, message)
            self.assertIsNone(locator)

        self.assertEqual(page.goto_calls[0][0], base_url)
        self.assertEqual(page.wait_for_load_state_calls[0][0], "networkidle")
        self.assertEqual(page.last_selector, "body")

    async def test_state_dependency_repair_unlocks_disabled_form_target_before_locator_patch(self):
        executor = PlaywrightExecutor(headless=True)
        step = {
            "step_sort": 0,
            "operation": "fill",
            "page_key": "page_1",
            "element_key": "textarea_8",
            "target_name": "报告方式",
            "description": "填写报告方式",
            "locator_hint": {"type": "label", "value": "报告方式", "confidence": 0.9},
            "value": "多邮箱换行输入",
        }
        element_map = {
            "pages": [
                {
                    "page_key": "page_1",
                    "elements": [
                        {
                            "element_key": "textarea_8",
                            "name": "报告方式",
                            "label": "报告方式",
                            "enabled": False,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "报告方式", "confidence": 0.9},
                        }
                    ],
                }
            ]
        }

        with (
            patch.object(
                executor,
                "_first_visible_locator",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch.object(
                executor,
                "_try_enable_disabled_form_target",
                new=AsyncMock(return_value=(True, {"type": "context_dependency", "element_key": "radio_7"})),
            ) as unlock_mock,
            patch.object(
                executor,
                "_execute_ai_plan_step",
                new=AsyncMock(return_value=(True, "步骤执行成功: fill", {"type": "label", "value": "报告方式"})),
            ) as execute_mock,
        ):
            success, message, locator_used, repairs = await executor._repair_and_execute_ai_plan_step(
                page=MagicMock(),
                step=step,
                element_map=element_map,
                base_url="https://example.test",
                safety_policy={},
                max_repair_rounds=1,
            )

        self.assertTrue(success)
        self.assertIn("步骤执行成功", message)
        self.assertIsNotNone(locator_used)
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["type"], "ai_plan_step_state_dependency_repair")
        unlock_mock.assert_awaited_once()
        execute_mock.assert_awaited_once()

    def test_exploration_targets_prefer_module_before_create_entry(self):
        executor = PlaywrightExecutor(headless=True)
        args = {
            "target_module": "测试任务",
            "requirement": "新建弹窗仅填写最简必填项",
        }
        targets = executor._build_exploration_targets(args)
        module_target = targets[0]
        create_target = targets[1]
        safety_policy = {"dangerous_action_keywords": [], "require_confirmation_keywords": ["保存", "提交", "确定", "确认"]}
        module_element = {
            "name": "测试任务",
            "text": "测试任务",
            "role": "menuitem",
            "actions": ["click"],
            "enabled": True,
        }
        create_element = {
            "name": "新建测试任务",
            "text": "新建测试任务",
            "role": "button",
            "actions": ["click"],
            "enabled": True,
        }

        self.assertGreater(
            executor._rank_exploration_element_for_target(module_element, module_target, safety_policy),
            0,
        )
        self.assertEqual(
            executor._rank_exploration_element_for_target(create_element, module_target, safety_policy),
            -1,
        )
        self.assertGreater(
            executor._rank_exploration_element_for_target(create_element, create_target, safety_policy),
            0,
        )

    def test_gherkin_navigation_targets_preserve_explicit_route_order(self):
        executor = PlaywrightExecutor(headless=True)
        targets = executor._build_exploration_targets({
            'gherkin': (
                'Given 用户已进入“Section A”->“Item B”页面\n'
                'When 用户搜索编号为 "10001" 的记录\n'
                'And 用户在该记录点击“Details”按钮\n'
                'When 用户点击“Content”标签页\n'
                'When 用户点击“Add”按钮\n'
                'Then 添加表单页面打开\n'
                'When 用户点击“Submit”按钮'
            ),
        })

        self.assertEqual(
            [target['keywords'][0] for target in targets],
            ['Section A', 'Item B', 'Details', 'Content', 'Add'],
        )
        self.assertEqual(targets[-1]['stage'], 'open_business_form')
        self.assertNotIn('10001', [target['keywords'][0] for target in targets])
        self.assertNotIn('Submit', [target['keywords'][0] for target in targets])

    async def test_authentication_scenario_defers_exploration_to_test_plan(self):
        executor = PlaywrightExecutor(headless=True)
        first_state = {
            'page_key': 'page_1',
            'url': 'https://example.test/login',
            'title': '登录',
            'elements': [
                {
                    'element_key': 'account',
                    'name': '请输入账号',
                    'input_type': 'account',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'password',
                    'name': '请输入密码',
                    'input_type': 'password',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'login',
                    'name': '登录',
                    'role': 'button',
                    'actions': ['click'],
                },
            ],
        }
        observations = []
        page = MagicMock()
        page.url = first_state['url']

        with patch.object(
            executor,
            '_auto_login_if_possible',
            new=AsyncMock(return_value=True),
        ) as auto_login:
            pages, transitions = await executor._auto_explore_business_pages(
                page=page,
                task_id=40,
                first_state=first_state,
                args={
                    'authentication_mode': 'test_subject',
                    'gherkin': (
                        'Scenario: 成功登录系统\n'
                        'When 输入账号 "sysAdmin"\n'
                        'And 输入密码 "secret"\n'
                        'And 点击登录\n'
                        'Then 成功进入网站首页'
                    ),
                },
                safety_policy={},
                base_url=first_state['url'],
                max_pages=20,
                max_steps=50,
                observations=observations,
            )

        auto_login.assert_not_awaited()
        self.assertEqual(pages, [first_state])
        self.assertEqual(transitions, [])
        strategy = next(item for item in observations if item['type'] == 'authentication_strategy')
        self.assertEqual(strategy['mode'], 'test_subject')
        coverage = next(item for item in observations if item['type'] == 'business_exploration_coverage')
        self.assertEqual(coverage['status'], 'deferred')
        self.assertTrue(coverage['planning_allowed'])

    async def test_sso_login_validation_task_skips_prerequisite_auto_login(self):
        executor = PlaywrightExecutor(headless=True)
        first_state = {
            'page_key': 'page_1',
            'url': 'https://example.test/login',
            'elements': [
                {
                    'element_key': 'username',
                    'name': '用户名',
                    'tag': 'input',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'password',
                    'name': '密码',
                    'tag': 'input',
                    'input_type': 'password',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'login',
                    'name': '登录',
                    'role': 'button',
                    'actions': ['click'],
                },
                {
                    'element_key': 'sso_login',
                    'name': 'SSO登录',
                    'role': 'button',
                    'actions': ['click'],
                },
            ],
        }
        observations = []
        page = MagicMock()
        page.url = first_state['url']

        with patch.object(executor, '_auto_login_if_possible', new=AsyncMock(return_value=False)) as auto_login:
            pages, transitions = await executor._auto_explore_business_pages(
                page=page,
                task_id=41,
                first_state=first_state,
                args={
                    'authentication_mode': 'test_subject',
                    'requirement': '验证网站使用sso登录是否登录成功',
                },
                safety_policy={},
                base_url=first_state['url'],
                max_pages=20,
                max_steps=50,
                observations=observations,
            )

        auto_login.assert_not_awaited()
        self.assertEqual(pages, [first_state])
        self.assertEqual(transitions, [])
        strategy = next(item for item in observations if item['type'] == 'authentication_strategy')
        self.assertEqual(strategy['mode'], 'test_subject')
        coverage = next(item for item in observations if item['type'] == 'business_exploration_coverage')
        self.assertEqual(coverage['status'], 'deferred')
        self.assertTrue(coverage['planning_allowed'])
        self.assertEqual(coverage['missing_stages'], [])

    async def test_authentication_then_business_flow_continues_past_login_snapshot(self):
        executor = PlaywrightExecutor(headless=True)
        first_state = {
            'page_key': 'page_1',
            'url': 'https://example.test/login',
            'elements': [
                {
                    'element_key': 'username',
                    'name': '账号',
                    'tag': 'input',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'password',
                    'name': '密码',
                    'tag': 'input',
                    'input_type': 'password',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'login',
                    'name': 'SSO登录',
                    'role': 'button',
                    'actions': ['click'],
                },
            ],
        }
        post_login_state = {
            'page_key': 'page_2',
            'url': 'https://example.test/home',
            'title': '管理平台首页',
            'elements': [
                {
                    'element_key': 'menu_system',
                    'name': '系统管理',
                    'role': 'menuitem',
                    'actions': ['click'],
                },
            ],
        }
        observations = []
        page = MagicMock()
        page.url = first_state['url']

        with (
            patch.object(executor, '_auto_login_if_possible', new=AsyncMock(return_value=False)) as auto_login,
            patch.object(
                executor,
                '_perform_explicit_authentication_step',
                new=AsyncMock(return_value=(True, post_login_state, '')),
            ) as explicit_auth,
            patch.object(executor, '_wait_for_business_state_stable', new=AsyncMock()),
        ):
            pages, transitions = await executor._auto_explore_business_pages(
                page=page,
                task_id=42,
                first_state=first_state,
                args={
                    'authentication_mode': 'authentication_then_business',
                    'gherkin': (
                        'Feature: 账号管理 - 冻结账号查看\n'
                        'Scenario: 通过SSO登录并查看已冻结账号详情\n'
                        'When 点击"SSO登录"按钮\n'
                        'Then 成功登录管理平台\n'
                        'When 点击菜单"系统管理" -> "账号管理"\n'
                        'Then 进入账号管理页面\n'
                        'When 搜索账号名称为 "zhangwenwen@ebupt.com"\n'
                        'And 点击列表中第一个账号名称\n'
                        'Then 进入账号详情页面\n'
                    ),
                },
                safety_policy={},
                base_url=first_state['url'],
                max_pages=2,
                max_steps=0,
                observations=observations,
            )

        explicit_auth.assert_awaited_once()
        auto_login.assert_not_awaited()
        self.assertGreaterEqual(len(pages), 2)
        self.assertEqual(pages[1]['page_key'], 'page_2')
        self.assertTrue(any(item['type'] == 'post_explicit_authentication_snapshot' for item in observations))
        strategy = next(item for item in observations if item['type'] == 'authentication_strategy')
        self.assertEqual(strategy['mode'], 'authentication_then_business')
        self.assertEqual(transitions, [])

    async def test_business_exploration_blocks_llm_planning_when_prerequisite_login_fails(self):
        executor = PlaywrightExecutor(headless=True)
        first_state = {
            'page_key': 'page_1',
            'url': 'https://example.test/login',
            'elements': [
                {
                    'element_key': 'username',
                    'name': '账号',
                    'tag': 'input',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'password',
                    'name': '密码',
                    'tag': 'input',
                    'input_type': 'password',
                    'actions': ['fill'],
                },
                {
                    'element_key': 'login',
                    'name': '登录',
                    'role': 'button',
                    'actions': ['click'],
                },
            ],
        }
        observations = [{
            'type': 'auto_login',
            'status': 'failed',
            'error': '登录后仍停留在登录页，未进入系统',
        }]
        page = MagicMock()
        page.url = first_state['url']

        with patch.object(executor, '_auto_login_if_possible', new=AsyncMock(return_value=False)):
            pages, transitions = await executor._auto_explore_business_pages(
                page=page,
                task_id=55,
                first_state=first_state,
                args={
                    'authentication_mode': 'prerequisite',
                    'target_module': '业务对象管理',
                    'requirement': '新增业务对象配置',
                },
                safety_policy={},
                base_url=first_state['url'],
                max_pages=20,
                max_steps=50,
                observations=observations,
            )

        self.assertEqual(pages, [first_state])
        self.assertEqual(transitions, [])
        coverage = next(item for item in observations if item['type'] == 'business_exploration_coverage')
        self.assertEqual(coverage['status'], 'deferred')
        self.assertFalse(coverage['planning_allowed'])
        self.assertEqual(coverage['failure_category'], 'permission')
        self.assertEqual(coverage['blocked_by'], 'authentication')
        self.assertIn('登录页', coverage['reason'])

    def test_exploration_rejects_aggregate_menubar_candidate(self):
        executor = PlaywrightExecutor(headless=True)
        target = executor._build_exploration_targets({
            'target_module': '目标管理',
            'requirement': '查看目标列表',
        })[0]
        aggregate = {
            'element_key': 'ul_21',
            'role': 'menubar',
            'name': '测试脚本 测试资源 测试任务 测试报告 系统管理 辅助工具',
            'actions': ['click'],
            'enabled': True,
        }

        self.assertEqual(
            executor._rank_exploration_element_for_target(aggregate, target, {}),
            -1,
        )
        self.assertEqual(executor._rank_navigation_probe(aggregate, target, {}), -1)

    def test_read_only_target_does_not_use_form_completion_condition(self):
        executor = PlaywrightExecutor(headless=True)
        target = executor._build_exploration_targets({
            'target_module': '目标管理',
            'requirement': '查看目标列表并查询名称',
        })[0]

        self.assertEqual(target['stage'], 'target_module')
        self.assertFalse(executor._target_requires_form_coverage(target))

    def test_dialog_assertion_does_not_create_a_form_exploration_stage(self):
        executor = PlaywrightExecutor(headless=True)

        targets = executor._build_exploration_targets({
            'target_module': '目标管理',
            'requirement': '点击使用指南后验证弹窗成功打开',
        })

        self.assertEqual([target['stage'] for target in targets], ['target_module'])

    def test_exploration_uses_control_label_not_inherited_container_text(self):
        executor = PlaywrightExecutor(headless=True)
        target = executor._build_exploration_targets({
            'target_module': '目标管理',
            'requirement': '查看目标列表',
        })[0]
        noisy_button = {
            'role': 'button',
            'name': '用户菜单',
            'text': '用户菜单 目标管理 新建目标',
            'actions': ['click'],
            'enabled': True,
            'recommended_locator': {'type': 'role', 'role': 'button', 'name': '用户菜单'},
        }

        self.assertEqual(executor._exploration_label(noisy_button), '用户菜单')
        self.assertEqual(executor._rank_exploration_element_for_target(noisy_button, target, {}), -1)

    def test_unrelated_atomic_control_never_becomes_a_direct_target(self):
        executor = PlaywrightExecutor(headless=True)
        target = executor._build_exploration_targets({
            'target_module': '目标管理',
            'requirement': '查看目标列表',
        })[0]
        unrelated = {
            'role': 'menuitem',
            'name': '测试任务',
            'actions': ['click'],
            'enabled': True,
            'recommended_locator': {'type': 'role', 'role': 'menuitem', 'name': '测试任务'},
        }

        self.assertEqual(executor._rank_exploration_element_for_target(unrelated, target, {}), -1)
        self.assertGreater(executor._rank_navigation_probe(unrelated, target, {}), 0)

    def test_page_signature_detects_submenu_added_after_first_thirty_elements(self):
        executor = PlaywrightExecutor(headless=True)
        base_elements = [
            {'element_key': f'button_{index}', 'role': 'button', 'name': f'Action {index}'}
            for index in range(31)
        ]
        before = {'url': 'https://example.test/home', 'title': 'Home', 'elements': base_elements}
        after = {
            'url': 'https://example.test/home',
            'title': 'Home',
            'elements': [
                *base_elements,
                {'element_key': 'submenu_32', 'role': 'menuitem', 'name': '目标管理'},
            ],
        }

        self.assertNotEqual(executor._page_signature(before), executor._page_signature(after))

    async def test_hidden_target_is_reached_by_bounded_atomic_menu_probing(self):
        executor = PlaywrightExecutor(headless=True)
        aggregate = {
            'element_key': 'menu_root',
            'role': 'menubar',
            'name': '系统管理 目标管理',
            'actions': ['click'],
            'enabled': True,
        }
        parent = {
            'element_key': 'menu_system',
            'role': 'menuitem',
            'name': '系统管理',
            'actions': ['click'],
            'enabled': True,
            'recommended_locator': {'type': 'role', 'role': 'menuitem', 'name': '系统管理'},
        }
        target = {
            'element_key': 'menu_target',
            'role': 'menuitem',
            'name': '目标管理',
            'actions': ['click'],
            'enabled': True,
            'recommended_locator': {'type': 'role', 'role': 'menuitem', 'name': '目标管理'},
        }
        states = [
            {'page_key': 'probe_home', 'url': 'https://example.test/home', 'elements': [aggregate, parent]},
            {'page_key': 'submenu', 'url': 'https://example.test/home', 'elements': [aggregate, parent, target]},
            {'page_key': 'probe_submenu', 'url': 'https://example.test/home', 'elements': [aggregate, parent, target]},
            {'page_key': 'target_page', 'url': 'https://example.test/targets', 'elements': [
                {'element_key': 'table', 'role': 'table', 'name': '目标列表', 'actions': []},
            ]},
        ]
        page = MagicMock()
        page.url = 'https://example.test/home'
        page.goto = AsyncMock()
        clicked = []

        async def click_element(_page, element):
            clicked.append(element['element_key'])
            if element['element_key'] == 'menu_target':
                page.url = 'https://example.test/targets'
            return True, ''

        observations = []
        with (
            patch.object(executor, '_auto_login_if_possible', new=AsyncMock(return_value=False)),
            patch.object(executor, '_wait_for_business_state_stable', new=AsyncMock()),
            patch.object(executor, '_mcp_collect_page_state', new=AsyncMock(side_effect=states)),
            patch.object(executor, '_click_collected_element', new=AsyncMock(side_effect=click_element)),
        ):
            pages, transitions = await executor._auto_explore_business_pages(
                page=page,
                task_id=1,
                first_state={'page_key': 'home', 'url': page.url, 'elements': [aggregate, parent]},
                args={'target_module': '目标管理', 'requirement': '查看目标列表'},
                safety_policy={},
                base_url='https://example.test',
                max_pages=10,
                max_steps=10,
                observations=observations,
            )

        self.assertEqual(clicked, ['menu_system', 'menu_target'])
        self.assertNotIn('menu_root', clicked)
        self.assertEqual(len(transitions), 2)
        coverage = next(item for item in observations if item['type'] == 'business_exploration_coverage')
        self.assertEqual(coverage['status'], 'success')
        self.assertEqual(coverage['missing_stages'], [])
        self.assertEqual(pages[-1]['url'], 'https://example.test/targets')

    def test_mcp_script_prefers_business_dialog_over_login_page(self):
        executor = PlaywrightExecutor(headless=True)
        args = {
            "target_url": "https://10.1.70.29:8444/login",
            "target_module": "测试任务",
            "requirement": "新建弹窗仅填写最简必填项",
            "safety_policy": {
                "require_confirmation_keywords": ["保存", "提交", "确定", "确认"],
            },
        }
        element_map = {
            "base_url": "https://10.1.70.29:8444/login",
            "pages": [
                {
                    "page_key": "page_1",
                    "url": "https://10.1.70.29:8444/login",
                    "title": "登录",
                    "elements": [
                        {
                            "element_key": "input_username",
                            "name": "请输入用户名",
                            "placeholder": "请输入用户名",
                            "actions": ["fill"],
                            "recommended_locator": {"type": "placeholder", "value": "请输入用户名"},
                        },
                        {
                            "element_key": "input_password",
                            "name": "请输入密码",
                            "placeholder": "请输入密码",
                            "input_type": "password",
                            "actions": ["fill"],
                            "recommended_locator": {"type": "placeholder", "value": "请输入密码"},
                        },
                        {
                            "element_key": "button_login",
                            "name": "登录",
                            "text": "登录",
                            "actions": ["click"],
                            "recommended_locator": {"type": "role", "role": "button", "name": "登录"},
                        },
                    ],
                },
                {
                    "page_key": "page_4",
                    "url": "https://10.1.70.29:8444/task",
                    "title": "创建任务",
                    "elements": [
                        {
                            "element_key": "dialog_create_task",
                            "name": "创建任务",
                            "text": "创建任务",
                            "actions": [],
                            "recommended_locator": {"type": "role", "role": "dialog", "name": "创建任务"},
                        },
                        {
                            "element_key": "input_101",
                            "name": "任务名称",
                            "label": "任务名称",
                            "placeholder": "请输入任务名称",
                            "required": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "任务名称"},
                        },
                        {
                            "element_key": "select_102",
                            "name": "测试环境",
                            "label": "测试环境",
                            "placeholder": "请选择测试环境",
                            "required": True,
                            "actions": ["select_option"],
                            "recommended_locator": {"type": "label", "value": "测试环境"},
                        },
                        {
                            "element_key": "select_103",
                            "name": "报告方式",
                            "label": "报告方式",
                            "required": True,
                            "actions": ["select_option"],
                            "recommended_locator": {"type": "label", "value": "报告方式"},
                        },
                        {
                            "element_key": "select_104",
                            "name": "执行方式",
                            "label": "执行方式",
                            "required": True,
                            "actions": ["select_option"],
                            "recommended_locator": {"type": "label", "value": "执行方式"},
                        },
                        {
                            "element_key": "button_136",
                            "name": "保存任务",
                            "text": "保存任务",
                            "actions": ["click"],
                            "recommended_locator": {"type": "role", "role": "button", "name": "保存任务"},
                        },
                    ],
                },
            ],
        }

        script = executor._build_mcp_script(args, element_map)

        self.assertIn('page.goto("https://10.1.70.29:8444/task"', script)
        self.assertIn("任务名称", script)
        self.assertIn(".first.fill", script)
        self.assertIn("测试环境", script)
        self.assertIn("报告方式", script)
        self.assertIn("执行方式", script)
        self.assertIn("保存任务", script)
        self.assertIn("page.keyboard.press('ArrowDown')", script)
        self.assertIn("page.keyboard.press('Enter')", script)
        self.assertNotIn("请输入用户名", script)
        self.assertNotIn("请输入密码", script)
        self.assertNotIn("登录", script)
        compile(script, "<generated_script>", "exec")

    def test_mcp_test_plan_binds_required_form_steps_to_element_map(self):
        executor = PlaywrightExecutor(headless=True)
        args = {
            "target_url": "https://10.1.70.29:8444/login",
            "target_module": "测试任务",
            "requirement": "新建测试任务，新建弹窗仅填写必填项，保存任务",
            "safety_policy": {"allow_form_submit": False, "dangerous_action_keywords": ["删除", "支付"]},
        }
        element_map = {
            "base_url": "https://10.1.70.29:8444/login",
            "pages": [
                {
                    "page_key": "page_4",
                    "url": "https://10.1.70.29:8444/home/testingTask",
                    "elements": [
                        {
                            "element_key": "input_1",
                            "name": "测试环境",
                            "label": "测试环境",
                            "required": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "测试环境"},
                        },
                        {
                            "element_key": "input_2",
                            "name": "执行方式",
                            "label": "执行方式",
                            "required": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "执行方式"},
                        },
                        {
                            "element_key": "input_3",
                            "name": "任务名称",
                            "label": "任务名称",
                            "required": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "任务名称"},
                        },
                        {
                            "element_key": "textarea_8",
                            "name": "报告方式",
                            "label": "报告方式",
                            "required": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "报告方式"},
                        },
                        {
                            "element_key": "button_25",
                            "name": "保存任务",
                            "text": "保存任务",
                            "actions": ["click"],
                            "recommended_locator": {"type": "role", "value": "button", "name": "保存任务"},
                        },
                    ],
                },
            ],
        }

        plan = executor._build_mcp_test_plan(args, element_map)
        steps = plan["steps"]
        element_steps = [step for step in steps if step.get("operation") not in {"goto", "wait"}]

        self.assertTrue(element_steps)
        self.assertTrue(all(step.get("locator_hint", {}).get("type") for step in element_steps))
        self.assertTrue(any(step.get("operation") == "fill" and step.get("target_name") == "任务名称" for step in steps))
        self.assertTrue(any(step.get("operation") == "select_option" and step.get("target_name") == "测试环境" for step in steps))
        self.assertTrue(any(step.get("operation") == "select_option" and step.get("target_name") == "执行方式" for step in steps))
        self.assertTrue(any(step.get("operation") == "fill" and step.get("target_name") == "报告方式" for step in steps))
        self.assertTrue(any(step.get("operation") == "click" and step.get("target_name") == "保存任务" for step in steps))

    def test_mcp_test_plan_prefers_structured_contract_over_requirement_text(self):
        executor = PlaywrightExecutor(headless=True)
        args = {
            "target_url": "https://example.test/task",
            "target_module": "测试任务",
            "requirement": "请忽略这段自然语言，不要重新编计划",
            "gherkin": "Feature: ignored",
            "generated_case": {
                "name": "结构化任务",
                "description": "已有结构化合同",
                "steps": [
                    {
                        "action": "click",
                        "operation": "click",
                        "description": "点击保存任务",
                        "page_key": "page_1",
                        "element_key": "button_25",
                        "target_name": "保存任务",
                        "locator_hint": {"type": "role", "value": "button", "name": "保存任务"},
                        "value": "",
                        "expected": "已保存",
                    }
                ],
            },
            "test_plan": {
                "objective": "结构化目标",
                "preconditions": ["已有登录态"],
                "test_data": {"task_name": "自动化任务"},
                "expected_results": ["保存成功"],
                "cleanup": ["删除测试任务"],
                "steps": [
                    {
                        "action": "click",
                        "operation": "click",
                        "description": "点击保存任务",
                        "page_key": "page_1",
                        "element_key": "button_25",
                        "target_name": "保存任务",
                        "locator_hint": {"type": "role", "value": "button", "name": "保存任务"},
                        "value": "",
                        "expected": "已保存",
                    }
                ],
            },
        }

        plan = executor._build_mcp_test_plan(args, {"base_url": "https://example.test/task", "pages": []})

        self.assertEqual(plan["objective"], "结构化目标")
        self.assertEqual(plan["preconditions"], ["已有登录态"])
        self.assertEqual(plan["test_data"], {"task_name": "自动化任务"})
        self.assertEqual(plan["expected_results"], ["保存成功"])
        self.assertEqual(plan["cleanup"], ["删除测试任务"])
        self.assertEqual(plan["steps"][0]["step_sort"], 0)
        self.assertEqual(plan["steps"][0]["target_name"], "保存任务")
        self.assertEqual(plan["steps"][0]["locator_hint"]["name"], "保存任务")

    def test_mcp_test_plan_uses_generated_case_metadata_when_test_plan_is_absent(self):
        executor = PlaywrightExecutor(headless=True)
        args = {
            "target_url": "https://example.test/task",
            "target_module": "测试任务",
            "requirement": "噪声文本",
            "generated_case": {
                "name": "结构化用例",
                "description": "结构化目标",
                "preconditions": ["已有登录态"],
                "test_data": {"task_name": "自动化任务"},
                "expected_results": ["保存成功"],
                "cleanup": ["删除测试任务"],
                "steps": [
                    {
                        "action": "click",
                        "operation": "click",
                        "description": "点击保存任务",
                        "page_key": "page_1",
                        "element_key": "button_25",
                        "target_name": "保存任务",
                        "locator_hint": {"type": "role", "value": "button", "name": "保存任务"},
                        "value": "",
                        "expected": "已保存",
                    }
                ],
            },
            "test_plan": {},
        }

        plan = executor._build_mcp_test_plan(args, {"base_url": "https://example.test/task", "pages": []})

        self.assertEqual(plan["objective"], "结构化目标")
        self.assertEqual(plan["preconditions"], ["已有登录态"])
        self.assertEqual(plan["test_data"], {"task_name": "自动化任务"})
        self.assertEqual(plan["expected_results"], ["保存成功"])
        self.assertEqual(plan["cleanup"], ["删除测试任务"])

    def test_mcp_test_plan_preserves_transition_into_dialog_state(self):
        executor = PlaywrightExecutor(headless=True)
        args = {
            "target_url": "https://10.1.70.29:8444/login",
            "target_module": "测试任务",
            "requirement": "新建测试任务，新建弹窗仅填写必填项任务名称、测试环境、报告方式、执行方式，保存任务",
            "safety_policy": {"allow_form_submit": True, "dangerous_action_keywords": ["删除", "支付"]},
        }
        element_map = {
            "base_url": "https://10.1.70.29:8444/login",
            "state_transitions": [
                {
                    "operation": "click",
                    "stage": "open_business_form",
                    "from_page_key": "page_3",
                    "from_url": "https://10.1.70.29:8444/home/testingTask",
                    "to_page_key": "page_4",
                    "to_url": "https://10.1.70.29:8444/home/testingTask",
                    "element_key": "button_12",
                    "target_name": "新建测试任务",
                    "locator_hint": {"type": "role", "value": "button", "name": "新建测试任务"},
                }
            ],
            "pages": [
                {
                    "page_key": "page_3",
                    "url": "https://10.1.70.29:8444/home/testingTask",
                    "elements": [
                        {
                            "element_key": "input_41",
                            "name": "任务名称",
                            "placeholder": "任务名称",
                            "actions": ["fill"],
                            "recommended_locator": {"type": "placeholder", "value": "任务名称"},
                        },
                        {
                            "element_key": "button_12",
                            "name": "新建测试任务",
                            "text": "新建测试任务",
                            "actions": ["click"],
                            "recommended_locator": {"type": "role", "value": "button", "name": "新建测试任务"},
                        },
                    ],
                },
                {
                    "page_key": "page_4",
                    "url": "https://10.1.70.29:8444/home/testingTask",
                    "elements": [
                        {
                            "element_key": "dialog_1",
                            "name": "创建任务",
                            "role": "dialog",
                            "text": "创建任务 任务名称 测试环境 报告方式 执行方式 保存任务",
                            "in_dialog": True,
                            "actions": ["click"],
                            "recommended_locator": {"type": "role", "value": "dialog", "name": "创建任务"},
                        },
                        {
                            "element_key": "input_3",
                            "name": "任务名称",
                            "label": "任务名称",
                            "placeholder": "请输入任务名称",
                            "required": True,
                            "in_dialog": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "任务名称"},
                        },
                        {
                            "element_key": "input_1",
                            "name": "测试环境",
                            "label": "测试环境",
                            "required": True,
                            "in_dialog": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "测试环境"},
                        },
                        {
                            "element_key": "textarea_8",
                            "name": "报告方式",
                            "label": "报告方式",
                            "required": True,
                            "in_dialog": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "报告方式"},
                        },
                        {
                            "element_key": "input_2",
                            "name": "执行方式",
                            "label": "执行方式",
                            "required": True,
                            "in_dialog": True,
                            "actions": ["fill"],
                            "recommended_locator": {"type": "label", "value": "执行方式"},
                        },
                        {
                            "element_key": "button_25",
                            "name": "保存任务",
                            "text": "保存任务",
                            "in_dialog": True,
                            "actions": ["click"],
                            "recommended_locator": {"type": "role", "value": "button", "name": "保存任务"},
                        },
                    ],
                },
            ],
        }

        plan = executor._build_mcp_test_plan(args, element_map)
        steps = plan["steps"]
        transition_index = next(i for i, step in enumerate(steps) if step.get("target_name") == "新建测试任务")
        fill_index = next(i for i, step in enumerate(steps) if step.get("operation") == "fill" and step.get("target_name") == "任务名称")

        self.assertLess(transition_index, fill_index)
        self.assertEqual(steps[transition_index]["operation"], "click")
        self.assertEqual(steps[fill_index]["element_key"], "input_3")
        self.assertFalse(any(step.get("element_key") == "input_41" for step in steps))

        script = executor._build_mcp_script(args, element_map, plan)
        self.assertLess(script.index("新建测试任务"), script.index("填写字段：任务名称"))
        self.assertIn("get_by_label('任务名称')", script)
        compile(script, "<generated_script>", "exec")

    def test_mcp_script_empty_element_fallback_is_valid_python(self):
        executor = PlaywrightExecutor(headless=True)
        script = executor._build_mcp_script(
            {"target_url": "https://example.test"},
            {"base_url": "https://example.test", "pages": [{"url": "https://example.test", "elements": []}]},
        )

        self.assertIn("page.locator('body')", script)
        compile(script, "<generated_script>", "exec")

    async def test_dialog_required_fields_are_prioritized_when_element_budget_is_small(self):
        setup_playwright_env()
        executor = PlaywrightExecutor(headless=True)

        html = """
        <html>
          <body>
            <main>
              <button>列表按钮01</button><button>列表按钮02</button><button>列表按钮03</button>
              <button>列表按钮04</button><button>列表按钮05</button><button>列表按钮06</button>
              <button>列表按钮07</button><button>列表按钮08</button><button>列表按钮09</button>
              <button>列表按钮10</button><button>列表按钮11</button><button>列表按钮12</button>
              <section class="el-dialog" role="dialog" aria-label="创建任务">
                <h2 class="el-dialog__title">创建任务</h2>
                <form class="el-form">
                  <div class="el-form-item is-required">
                    <label class="el-form-item__label">任务名称</label>
                    <div class="el-form-item__content"><input placeholder="请输入任务名称" /></div>
                  </div>
                  <div class="el-form-item is-required">
                    <label class="el-form-item__label">测试环境</label>
                    <div class="el-form-item__content"><input role="combobox" placeholder="请选择测试环境" /></div>
                  </div>
                  <button type="button">保存任务</button>
                </form>
              </section>
            </main>
          </body>
        </html>
        """

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            with tempfile.NamedTemporaryFile(suffix=".png") as screenshot:
                state = await executor._mcp_collect_page_state(
                    page,
                    "dialog_probe",
                    screenshot.name,
                    max_elements=8,
                )
            await browser.close()

        elements = state.get("elements") or []
        required_names = {
            item.get("name")
            for item in elements
            if isinstance(item, dict) and item.get("required")
        }
        self.assertIn("任务名称", required_names)
        self.assertIn("测试环境", required_names)
        self.assertTrue(any(item.get("name") == "保存任务" for item in elements if isinstance(item, dict)))


if __name__ == "__main__":
    unittest.main()
