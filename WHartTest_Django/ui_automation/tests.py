import json
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
import zipfile

try:
    import httpx
except ImportError:  # pragma: no cover - test environment may not have optional deps
    httpx = None
from django.contrib.auth.models import User
from django.test import TestCase, SimpleTestCase, override_settings
from asgiref.sync import async_to_sync
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory
from unittest.mock import AsyncMock, MagicMock, patch

from projects.models import Project, ProjectMember
from ui_automation.models import (
    UiAiGenerationTask,
    UiElementMap,
    UiElement,
    UiEnvironmentConfig,
    UiModule,
    UiPage,
    UiPageSteps,
    UiPageStepsDetailed,
    UiCaseStepsDetailed,
    UiTestCase,
    UiExecutionRecord,
)
from ui_automation.consumers import SocketUserManager, UiAutomationConsumer, save_ai_generation_result_sync
from ui_automation.serializers import UiEnvironmentConfigSerializer, UiPageStepsExecuteSerializer
from ui_automation.ai_planning import (
    _StagedUiGenerationPlanner,
    _apply_llm_patch_operations,
    _build_budgeted_generation_prompt,
    _build_data_lifecycle,
    _build_script_from_steps,
    _build_typescript_runtime_helper,
    _build_typescript_spec_from_steps,
    _build_runtime_agent_plan,
    _compact_element_map,
    _complete_staged_requirement_contract,
    _compile_final_contract_step_order,
    _compile_flow_transition_steps,
    _compile_selected_route_steps,
    _compile_runtime_state_evidence_bindings,
    _compile_missing_explicit_actions,
    _extract_explicit_action_requirements,
    _extract_repair_spec_context,
    _fallback_plan,
    _normalize_llm_plan,
    _planning_precondition_failure,
    _planning_action_binding_candidates,
    _planning_binding_candidates,
    _planning_page_summaries,
    _semantic_replan_payload,
    _standardize_staged_contract_steps,
    _standardize_unresolved_gap_steps,
    _staged_binding_contract_issues,
    _staged_plan_contract_issues,
    _validate_generated_steps,
    build_llm_ui_generation_plan,
    classify_task_authentication_contract,
    _task_authentication_mode,
)
from ui_automation.planning_contract import (
    BindingContract,
    RequirementContract,
    RouteContract,
    build_requirement_contract,
    build_requirement_document,
    exploration_coverage_from_bindings,
    is_assertion_operation,
    staged_binding_contract_issues,
    staged_plan_contract_issues,
    standardize_staged_contract_steps,
)
from ui_automation.views import ActuatorViewSet, UiAiGenerationTaskViewSet, UiTestCaseViewSet, _should_apply_async_llm_plan


class UiEnvironmentAuthConfigTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='env-admin', password='secret')
        self.project = Project.objects.create(name='Auth Environment Project', creator=self.user)
        self.environment = UiEnvironmentConfig.objects.create(
            project=self.project,
            name='测试环境',
            base_url='https://example.test/login',
            extra_config={
                'ignore_https_errors': True,
                'auth': {'username': 'tester', 'password': 'stored-secret'},
            },
            creator=self.user,
        )

    def test_environment_api_masks_login_password(self):
        data = UiEnvironmentConfigSerializer(self.environment).data

        self.assertTrue(data['login_enabled'])
        self.assertEqual(data['login_username'], 'tester')
        self.assertTrue(data['has_login_password'])
        self.assertNotIn('password', data['extra_config']['auth'])

    def test_blank_password_preserves_existing_login_secret(self):
        serializer = UiEnvironmentConfigSerializer(
            self.environment,
            data={
                'login_username': 'updated-user',
                'login_password': '',
                'extra_config': {'ignore_https_errors': False},
            },
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        environment = serializer.save()
        self.assertEqual(environment.extra_config['auth']['username'], 'updated-user')
        self.assertEqual(environment.extra_config['auth']['password'], 'stored-secret')
        self.assertFalse(environment.extra_config['ignore_https_errors'])

    def test_ai_dispatch_includes_environment_login_secret(self):
        task = UiAiGenerationTask.objects.create(
            project=self.project,
            environment_config=self.environment,
            name='认证后探索',
            target_url='https://example.test/login',
            creator=self.user,
        )

        args = UiAiGenerationTaskViewSet._build_ai_generation_args(task)

        self.assertEqual(
            args['environment_config']['extra_config']['auth'],
            {'username': 'tester', 'password': 'stored-secret'},
        )


class ActuatorConnectionPolicyTests(SimpleTestCase):
    def setUp(self):
        self._original_actuators = SocketUserManager._actuator_users
        SocketUserManager._actuator_users = {}

    def tearDown(self):
        SocketUserManager._actuator_users = self._original_actuators

    @override_settings(UI_ACTUATOR_REGISTRATION_TOKEN_REQUIRED=True, UI_ACTUATOR_REGISTRATION_TOKEN='token-1')
    def test_registration_token_validation_rejects_mismatch(self):
        consumer = UiAutomationConsumer()

        self.assertFalse(consumer._validate_actuator_registration_token('wrong')[0])
        self.assertTrue(consumer._validate_actuator_registration_token('token-1')[0])

    @override_settings(UI_ACTUATOR_ONLINE_TTL_SECONDS=60)
    def test_available_actuator_selection_skips_unauthenticated_and_expired(self):
        now = timezone.now().isoformat()
        expired = (timezone.now() - timedelta(seconds=120)).isoformat()

        live = SimpleNamespace(
            connected=True,
            actuator_info={
                'authenticated': True,
                'connected_at': now,
                'last_seen_at': now,
            },
        )
        unauthenticated = SimpleNamespace(
            connected=True,
            actuator_info={
                'authenticated': False,
                'connected_at': now,
                'last_seen_at': now,
            },
        )
        stale = SimpleNamespace(
            connected=True,
            actuator_info={
                'authenticated': True,
                'connected_at': expired,
                'last_seen_at': expired,
            },
        )

        SocketUserManager._actuator_users = {
            'live': live,
            'unauthenticated': unauthenticated,
            'stale': stale,
        }

        self.assertIs(SocketUserManager.get_actuator(), live)
        self.assertIsNone(SocketUserManager.get_actuator_by_id('stale'))
        self.assertEqual(SocketUserManager.get_available_actuator_count(), 1)
        self.assertTrue(SocketUserManager.has_actuator())

    def test_heartbeat_refreshes_last_seen_timestamp(self):
        consumer = UiAutomationConsumer()
        consumer.is_actuator = True
        consumer.user_id = 'actuator-1'
        consumer.actuator_info = {
            'id': 'actuator-1',
            'authenticated': True,
            'connected_at': '2026-01-01T00:00:00+00:00',
            'last_seen_at': '2026-01-01T00:00:00+00:00',
        }
        consumer.send_json = AsyncMock(return_value=None)

        async_to_sync(consumer.handle_actuator_heartbeat)({
            'browser_type': 'chromium',
            'headless': True,
            'version': '1.0.0',
        }, None)

        self.assertEqual(consumer.actuator_info['browser_type'], 'chromium')
        self.assertTrue(consumer.actuator_info['headless'])
        self.assertEqual(consumer.actuator_info['version'], '1.0.0')
        self.assertNotEqual(consumer.actuator_info['last_seen_at'], '2026-01-01T00:00:00+00:00')

    def test_ai_dispatch_includes_requirement_document(self):
        task = UiAiGenerationTask.objects.create(
            project=self.project,
            environment_config=self.environment,
            name='任务编排',
            target_url='https://example.test/task',
            source_requirement='用户点击保存任务',
            gherkin='''
Feature: 任务编排
  Scenario: 保存任务
    When 用户在表单中填写任务名称
''',
            creator=self.user,
        )

        args = UiAiGenerationTaskViewSet._build_ai_generation_args(task)

        self.assertTrue(args['requirement_document']['has_natural_language'])
        self.assertTrue(args['requirement_document']['has_gherkin'])
        self.assertEqual(args['requirement_document']['source_requirement'], '用户点击保存任务')

    def test_ai_dispatch_preserves_unknown_authentication_mode(self):
        task = UiAiGenerationTask.objects.create(
            project=self.project,
            environment_config=self.environment,
            name='认证合同未知',
            target_url='https://example.test/login',
            creator=self.user,
        )

        with patch('ui_automation.views.classify_task_authentication_contract') as classify_mock:
            classify_mock.return_value = {
                'mode': 'unknown',
                'status': 'degraded',
                'source': 'llm_error_default',
                'rationale': 'LLM 分类失败，认证阶段角色不确定',
            }
            args = UiAiGenerationTaskViewSet._build_ai_generation_args(task)

        self.assertEqual(args['authentication_mode'], 'unknown')
        self.assertEqual(args['authentication_contract']['mode'], 'unknown')
        self.assertEqual(args['authentication_contract']['status'], 'degraded')

    def test_ai_dispatch_marks_manual_capture_element_map_mode(self):
        element_map = UiElementMap.objects.create(
            project=self.project,
            environment_config=self.environment,
            name='人工采集地图',
            base_url='https://example.test/app',
            status='confirmed',
            map_json={
                'capture_mode': 'manual_capture',
                'source': 'manual_capture',
                'base_url': 'https://example.test/app',
                'pages': [{'page_key': 'manual_page_1', 'elements': []}],
            },
            creator=self.user,
        )
        task = UiAiGenerationTask.objects.create(
            project=self.project,
            environment_config=self.environment,
            element_map=element_map,
            name='使用人工地图',
            target_url='https://example.test/app',
            creator=self.user,
        )

        args = UiAiGenerationTaskViewSet._build_ai_generation_args(task)

        self.assertEqual(args['execution_mode'], 'manual_map')
        self.assertEqual(args['element_map_source'], 'manual_capture')
        self.assertEqual(args['element_map_id'], element_map.id)

    def test_ai_dispatch_preserves_runtime_agent_mode(self):
        task = UiAiGenerationTask.objects.create(
            project=self.project,
            environment_config=self.environment,
            name='智能模式任务',
            target_url='https://example.test/app',
            safety_policy={'execution_mode': 'runtime_agent'},
            creator=self.user,
        )

        args = UiAiGenerationTaskViewSet._build_ai_generation_args(task)

        self.assertEqual(args['execution_mode'], 'runtime_agent')
        self.assertEqual(args['safety_policy']['execution_mode'], 'runtime_agent')

    def test_manual_capture_map_is_not_superseded_by_ai_generation_result(self):
        element_map = UiElementMap.objects.create(
            project=self.project,
            environment_config=self.environment,
            name='人工采集地图',
            base_url='https://example.test/app',
            status='confirmed',
            stale_status='current',
            map_hash='manual-baseline-hash',
            map_json={
                'capture_mode': 'manual_capture',
                'source': 'manual_capture',
                'base_url': 'https://example.test/app',
                'pages': [{
                    'page_key': 'manual_page_1',
                    'elements': [{
                        'element_key': 'manual_button',
                        'name': 'Manual Button',
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Manual Button'},
                    }],
                }],
            },
            creator=self.user,
        )
        task = UiAiGenerationTask.objects.create(
            project=self.project,
            environment_config=self.environment,
            element_map=element_map,
            name='使用人工地图生成',
            target_url='https://example.test/app',
            status='running',
            creator=self.user,
        )
        incoming_map = {
            'capture_mode': 'manual_capture',
            'source': 'manual_capture',
            'base_url': 'https://example.test/app',
            'pages': [{
                'page_key': 'runtime_page_1',
                'elements': [{
                    'element_key': 'runtime_button',
                    'name': 'Runtime Button',
                    'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Runtime Button'},
                }],
            }],
            'coverage_summary': {'page_count': 1, 'element_count': 1},
        }

        save_ai_generation_result_sync({
            'task_id': task.id,
            'status': 'success',
            'element_map': incoming_map,
            'generated_case': {'steps': []},
            'verification_result': {},
        })

        task.refresh_from_db()
        element_map.refresh_from_db()
        self.assertEqual(UiElementMap.objects.filter(project=self.project).count(), 1)
        self.assertEqual(task.element_map_id, element_map.id)
        self.assertEqual(task.element_map_snapshot, incoming_map)
        self.assertEqual(element_map.status, 'confirmed')
        self.assertEqual(element_map.stale_status, 'current')
        self.assertEqual(element_map.stale_reason, '')
        self.assertEqual(element_map.map_json['pages'][0]['page_key'], 'manual_page_1')
        self.assertEqual(task.verification_result['element_map_governance']['status'], 'skipped')
        self.assertEqual(
            task.verification_result['element_map_governance']['reason'],
            'manual_capture_map_is_immutable_for_ai_result',
        )

    def test_testcase_execute_creates_running_record_before_dispatch(self):
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        ProjectMember.objects.get_or_create(project=self.project, user=self.user, defaults={'role': 'admin'})
        module = UiModule.objects.create(
            project=self.project,
            name='执行合同',
            creator=self.user,
        )
        test_case = UiTestCase.objects.create(
            project=self.project,
            module=module,
            name='执行状态持久化',
            creator=self.user,
        )
        sent_messages = []

        class FakeActuator:
            async def send_json(self, payload):
                sent_messages.append(payload)

        client = APIClient()
        client.force_authenticate(self.user)
        with patch('ui_automation.consumers.SocketUserManager.get_actuator_by_id', return_value=FakeActuator()):
            response = client.post(
                f'/api/ui-automation/testcases/{test_case.id}/execute/',
                {'actuator_id': 'actuator-test', 'env_config_id': self.environment.id},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        test_case.refresh_from_db()
        record = UiExecutionRecord.objects.get(test_case=test_case)
        self.assertEqual(test_case.status, 1)
        self.assertEqual(record.status, 1)
        self.assertEqual(response.data['record_id'], record.id)
        self.assertEqual(len(sent_messages), 1)
        self.assertEqual(sent_messages[0].data.func_args['execution_record_id'], record.id)

    def test_save_execution_result_updates_started_record(self):
        module = UiModule.objects.create(
            project=self.project,
            name='结果合同',
            creator=self.user,
        )
        test_case = UiTestCase.objects.create(
            project=self.project,
            module=module,
            name='执行结果更新',
            creator=self.user,
            status=1,
        )
        record = UiExecutionRecord.objects.create(
            test_case=test_case,
            executor=self.user,
            status=1,
            trigger_type='manual',
        )

        consumer = UiAutomationConsumer()
        async_to_sync(consumer.save_execution_result)({
            'case_id': test_case.id,
            'execution_record_id': record.id,
            'executor_id': self.user.id,
            'status': 'success',
            'message': 'ok',
            'duration': 1.5,
            'steps': [{'step_id': 1, 'status': 'success'}],
        })

        test_case.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(UiExecutionRecord.objects.filter(test_case=test_case).count(), 1)
        self.assertEqual(record.status, 2)
        self.assertEqual(test_case.status, 2)
        self.assertEqual(test_case.result_data['last_execution'], record.id)


class ActuatorOnboardingTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @override_settings(
        UI_ACTUATOR_PACKAGE_WINDOWS_URL='',
        UI_ACTUATOR_PACKAGE_LINUX_URL='',
        UI_ACTUATOR_PACKAGE_MACOS_URL='',
    )
    def test_onboarding_does_not_advertise_missing_local_package(self):
        request = self.factory.get('/api/ui-automation/actuators/onboarding/')
        response = ActuatorViewSet.as_view({'get': 'onboarding'})(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        packages = response.data['data']['packages']
        windows = next(item for item in packages if item['platform'] == 'windows')

        self.assertFalse(windows['available'])
        self.assertEqual(windows['url'], '')

    @override_settings(
        UI_ACTUATOR_PACKAGE_WINDOWS_URL='',
        UI_ACTUATOR_PACKAGE_LINUX_URL='',
        UI_ACTUATOR_PACKAGE_MACOS_URL='',
    )
    def test_onboarding_uses_request_host_for_download_urls(self):
        request = self.factory.get(
            '/api/ui-automation/actuators/onboarding/',
            HTTP_HOST='10.4.4.69:8913',
        )
        with TemporaryDirectory() as tmpdir:
            installer = Path(tmpdir) / 'WHartTest_Actuator_Installer.exe'
            installer.write_bytes(b'MZ-test-installer')
            with patch.object(ActuatorViewSet, '_package_installer_path', return_value=installer):
                response = ActuatorViewSet.as_view({'get': 'onboarding'})(request)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            packages = response.data['data']['packages']
            windows = next(item for item in packages if item['platform'] == 'windows')

            parsed = urlparse(windows['url'])
            self.assertEqual(parsed.netloc, '10.4.4.69:8913')
            self.assertIn('/api/ui-automation/actuators/download-package/windows/', parsed.path)

    def test_download_package_returns_installer_when_available(self):
        request = self.factory.get('/api/ui-automation/actuators/download-package/windows/')
        with TemporaryDirectory() as tmpdir:
            installer = Path(tmpdir) / 'WHartTest_Actuator_Installer.exe'
            installer.write_bytes(b'MZ-test-installer')
            with patch.object(ActuatorViewSet, '_package_installer_path', return_value=installer):
                response = ActuatorViewSet.as_view({'get': 'download_package'})(request, platform='windows')

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn(
                'attachment; filename="WHartTest_Actuator_Installer.exe"',
                response['Content-Disposition'],
            )
            self.assertEqual(response['Content-Type'], 'application/vnd.microsoft.portable-executable')
            body = b''.join(response.streaming_content)
            self.assertTrue(body.startswith(b'MZ'))

    def test_download_package_prefers_packaged_dist_when_available(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'WHartTest_Actuator'
            dist_dir = root / 'dist' / 'WHartTest_Actuator'
            dist_dir.mkdir(parents=True)
            (dist_dir / 'WHartTest_Actuator.exe').write_bytes(b'MZ')
            (dist_dir / 'config.toml').write_text('api_url = "http://example.test"\n', encoding='utf-8')

            request = self.factory.get('/api/ui-automation/actuators/download-package/windows/')
            with patch.object(ActuatorViewSet, '_package_source_dir', return_value=root):
                response = ActuatorViewSet.as_view({'get': 'download_package'})(request, platform='windows')

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            archive = zipfile.ZipFile(BytesIO(b''.join(response.streaming_content)))
            names = archive.namelist()
            self.assertIn('WHartTest_Actuator/WHartTest_Actuator.exe', names)
            self.assertIn('WHartTest_Actuator/config.toml', names)

    def test_download_package_prefers_macos_app_bundle_when_available(self):
        with TemporaryDirectory() as tmpdir:
            dist_dir = Path(tmpdir) / 'dist'
            release_dir = dist_dir / 'WHartTest_Actuator_MacOS'
            app_binary = release_dir / 'WHartTest_Actuator.app' / 'Contents' / 'MacOS'
            app_binary.mkdir(parents=True)
            (app_binary / 'WHartTest_Actuator').write_bytes(b'MACH-O')
            (release_dir / 'config.toml').write_text(
                'api_url = "http://example.test"\n',
                encoding='utf-8',
            )

            request = self.factory.get('/api/ui-automation/actuators/download-package/macos/')
            with patch.object(ActuatorViewSet, '_package_dist_dir', return_value=dist_dir), \
                    patch.object(ActuatorViewSet, '_package_source_dir', return_value=Path(tmpdir) / 'missing'):
                response = ActuatorViewSet.as_view({'get': 'download_package'})(
                    request,
                    platform='macos',
                )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn(
                'attachment; filename="WHartTest_Actuator_MacOS.zip"',
                response['Content-Disposition'],
            )
            archive = zipfile.ZipFile(BytesIO(b''.join(response.streaming_content)))
            self.assertIn(
                'WHartTest_Actuator_MacOS/WHartTest_Actuator.app/Contents/MacOS/WHartTest_Actuator',
                archive.namelist(),
            )

    def test_download_package_returns_prebuilt_macos_archive_when_only_archive_is_available(self):
        with TemporaryDirectory() as tmpdir:
            dist_dir = Path(tmpdir) / 'dist'
            dist_dir.mkdir(parents=True)
            archive_path = dist_dir / 'WHartTest_Actuator_MacOS.zip'
            archive_path.write_bytes(b'PK-macos-package')

            request = self.factory.get('/api/ui-automation/actuators/download-package/macos/')
            with patch.object(ActuatorViewSet, '_package_dist_dir', return_value=dist_dir), \
                    patch.object(ActuatorViewSet, '_package_source_dir', return_value=Path(tmpdir) / 'missing'):
                response = ActuatorViewSet.as_view({'get': 'download_package'})(
                    request,
                    platform='macos',
                )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn(
                'attachment; filename="WHartTest_Actuator_MacOS.zip"',
                response['Content-Disposition'],
            )
            self.assertEqual(b''.join(response.streaming_content), b'PK-macos-package')

    def test_download_package_returns_macos_pkg_when_available(self):
        with TemporaryDirectory() as tmpdir:
            dist_dir = Path(tmpdir) / 'dist'
            dist_dir.mkdir(parents=True)
            pkg_path = dist_dir / 'WHartTest_Actuator_MacOS.pkg'
            pkg_path.write_bytes(b'xar-macos-package')

            request = self.factory.get('/api/ui-automation/actuators/download-package/macos/')
            with patch.object(ActuatorViewSet, '_package_dist_dir', return_value=dist_dir), \
                    patch.object(ActuatorViewSet, '_package_source_dir', return_value=Path(tmpdir) / 'missing'):
                response = ActuatorViewSet.as_view({'get': 'download_package'})(
                    request,
                    platform='macos',
                )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn(
                'attachment; filename="WHartTest_Actuator_MacOS.pkg"',
                response['Content-Disposition'],
            )
            self.assertEqual(response['Content-Type'], 'application/octet-stream')
            self.assertEqual(b''.join(response.streaming_content), b'xar-macos-package')


class UiPlanningIframeCandidateTests(TestCase):
    def setUp(self):
        self.element_map = {
            'pages': [{
                'page_key': 'business_page',
                'url': 'https://example.test/home',
                'elements': [],
                'frames': [{
                    'frame_key': 'frame_1',
                    'name': 'business-frame',
                    'url': 'https://example.test/business',
                    'elements': [{
                        'element_key': 'input_1',
                        'name': 'Company code',
                        'actions': ['fill'],
                        'recommended_locator': {'type': 'placeholder', 'value': 'Company code'},
                    }],
                }],
            }],
        }

    def test_route_selection_summary_includes_frame_elements(self):
        summaries = _planning_page_summaries(self.element_map)

        self.assertEqual(summaries[0]['elements'][0]['name'], 'Company code')
        self.assertEqual(summaries[0]['elements'][0]['element_key'], 'frame_1::input_1')

    def test_element_binding_candidates_preserve_frame_context(self):
        candidates = _planning_binding_candidates(
            self.element_map,
            {'ordered_page_keys': ['business_page']},
        )

        element = candidates[0]['elements'][0]
        self.assertEqual(element['context_type'], 'frame')
        self.assertEqual(element['frame_key'], 'frame_1')
        self.assertEqual(element['frame_name'], 'business-frame')
        self.assertEqual(element['frame_url'], 'https://example.test/business')

    def test_element_binding_candidates_preserve_table_row_context(self):
        element_map = {
            'pages': [{
                'page_key': 'business_page',
                'url': 'https://example.test/entities',
                'elements': [
                    {
                        'element_key': 'button_source',
                        'name': 'Row action',
                        'role': 'button',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Row action'},
                        'row_text': 'Source entity Enabled Row action',
                        'row_index': 0,
                        'row_cells': [
                            {'column_index': 0, 'column_name': 'Entity name', 'text': 'Source entity'},
                            {'column_index': 1, 'column_name': 'State', 'text': 'Enabled'},
                            {'column_index': 2, 'column_name': 'Operation', 'text': 'Row action'},
                        ],
                        'column_index': 2,
                        'column_name': 'Operation',
                        'column_text': 'Row action',
                        'table_name': 'Entity permissions',
                    },
                ],
            }],
        }

        candidates = _planning_binding_candidates(
            element_map,
            {'ordered_page_keys': ['business_page']},
        )

        element = candidates[0]['elements'][0]
        self.assertEqual(element['row_text'], 'Source entity Enabled Row action')
        self.assertEqual(element['row_index'], 0)
        self.assertEqual(element['column_name'], 'Operation')
        self.assertEqual(element['table_name'], 'Entity permissions')
        self.assertEqual(element['row_cells'][0]['text'], 'Source entity')

    def test_action_scoped_candidates_preserve_row_scope_after_compaction(self):
        class TaskStub:
            name = 'Row scoped action'
            source_requirement = ''
            gherkin = 'When Click "Row action" in the row whose entity name is "Target entity"'
            target_module = ''
            target_url = 'https://example.test/entities'
            safety_policy = {}

        element_map = {
            'pages': [{
                'page_key': 'business_page',
                'url': 'https://example.test/entities',
                'elements': [
                    {
                        'element_key': 'button_source',
                        'name': 'Row action',
                        'text': 'Row action',
                        'role': 'button',
                        'tag': 'button',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Row action'},
                        'row_text': 'Source entity Enabled Row action',
                        'row_index': 0,
                        'row_cells': [
                            {'column_index': 0, 'column_name': 'Entity name', 'text': 'Source entity'},
                            {'column_index': 1, 'column_name': 'State', 'text': 'Enabled'},
                            {'column_index': 2, 'column_name': 'Operation', 'text': 'Row action'},
                        ],
                        'column_index': 2,
                        'column_name': 'Operation',
                        'column_text': 'Row action',
                        'table_name': 'Entity permissions',
                    },
                    {
                        'element_key': 'button_target',
                        'name': 'Row action',
                        'text': 'Row action',
                        'role': 'button',
                        'tag': 'button',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Row action'},
                        'row_text': 'Target entity Enabled Row action',
                        'row_index': 1,
                        'row_cells': [
                            {'column_index': 0, 'column_name': 'Entity name', 'text': 'Target entity'},
                            {'column_index': 1, 'column_name': 'State', 'text': 'Enabled'},
                            {'column_index': 2, 'column_name': 'Operation', 'text': 'Row action'},
                        ],
                        'column_index': 2,
                        'column_name': 'Operation',
                        'column_text': 'Row action',
                        'table_name': 'Entity permissions',
                    },
                ],
            }],
        }
        requirements = {
            'flow': [{
                'action_id': 'action_5',
                'operation': 'click',
                'target': 'Click Row action in the row whose entity name is Target entity',
                'expected': 'Target entity detail page is shown',
                'required': True,
            }],
        }

        compact = _compact_element_map(element_map, task=TaskStub())
        candidates = _planning_action_binding_candidates(
            compact,
            {'ordered_page_keys': ['business_page']},
            requirements,
        )
        action = candidates['actions'][0]
        target_candidate = next(
            item for item in action['candidates']
            if item.get('element_key') == 'button_target'
        )

        self.assertEqual(action['candidates'][0]['element_key'], 'button_target')
        self.assertEqual(target_candidate['row_text'], 'Target entity Enabled Row action')
        self.assertEqual(target_candidate['row_scope']['row_text'], 'Target entity Enabled Row action')
        self.assertEqual(target_candidate['row_scope']['row_cells'][0]['text'], 'Target entity')
        self.assertEqual(target_candidate['column_name'], 'Operation')


class UiRequirementDocumentTests(TestCase):
    class TaskStub:
        name = '任务创建'
        target_module = '任务创建'
        target_url = 'https://example.test/task'
        source_requirement = '用户点击保存任务'
        gherkin = '''
Feature: 测试任务管理
  Scenario: 新建测试任务
    When 用户在新建弹窗中填写必填项：
      | 字段名称 | 值 |
      | 测试任务 | 自动化测试任务 |
    Then 页面显示“保存成功”
'''

    def test_requirement_document_merges_natural_language_and_gherkin_sources(self):
        document = build_requirement_document(self.TaskStub())

        self.assertTrue(document['has_natural_language'])
        self.assertTrue(document['has_gherkin'])
        self.assertEqual([item['kind'] for item in document['sources']], ['natural_language', 'gherkin'])
        self.assertIn('用户点击保存任务', document['sources'][0]['text'])
        self.assertEqual(document['sources'][1]['tables'][0]['cells'], ['字段名称', '值'])
        self.assertEqual(document['sources'][1]['steps'][0]['keyword'], 'When')
        self.assertIn('测试任务', document['normalized_text'])
        self.assertEqual([item['action_id'] for item in document['flow']], ['gherkin_step_1', 'gherkin_step_2'])
        self.assertEqual(document['flow'][0]['target'], '用户在新建弹窗中填写必填项：')
        self.assertEqual(document['flow'][1]['operation'], 'assert_visible')


class UiAiGenerationApplyCrossPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='tester', password='secret', is_staff=True, is_superuser=True,
        )
        self.project = Project.objects.create(name='Cross Page Project', creator=self.user)
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.task = UiAiGenerationTask.objects.create(
            project=self.project,
            name='关键字管理',
            target_module='关键字管理',
            target_url='https://example.test/login',
            status='success',
            generated_script='async def test_generated_flow(page): pass',
            generated_case={
                'steps': [
                    {
                        'operation': 'goto',
                        'page_key': 'page_1',
                        'value': 'https://example.test/login',
                        'description': '打开登录页',
                    },
                    {
                        'operation': 'fill',
                        'page_key': 'page_1',
                        'element_key': 'input_2',
                        'value': 'secret',
                        'target_name': '密码',
                        'locator_hint': {'type': 'placeholder', 'value': '密码'},
                        'description': '填写密码',
                    },
                    {
                        'operation': 'click',
                        'page_key': 'page_probe_4',
                        'element_key': 'li_stale',
                        'target_name': '关键字管理',
                        'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '关键字管理'},
                        'description': '进入关键字管理',
                    },
                    {
                        'operation': 'fill',
                        'page_key': 'page_5',
                        'element_key': 'input_2',
                        'value': 'CompareWithTolerance',
                        'target_name': '请输入编号/名称/说明',
                        'locator_hint': {'type': 'placeholder', 'value': '请输入编号/名称/说明'},
                        'description': '填写关键字搜索框',
                    },
                    {
                        'operation': 'assert_visible',
                        'page_key': 'page_5',
                        'element_key': '',
                        'target_name': '使用指南弹窗',
                        'locator_hint': {'type': 'text', 'value': '使用指南'},
                        'description': '验证使用指南文本',
                    },
                ],
            },
            element_map_snapshot={
                'pages': [
                    {
                        'page_key': 'page_1',
                        'name': '登录页',
                        'url': 'https://example.test/login',
                        'elements': [{
                            'element_key': 'input_2',
                            'name': '密码',
                            'placeholder': '密码',
                            'recommended_locator': {'type': 'placeholder', 'value': '密码'},
                        }],
                    },
                    {
                        'page_key': 'page_4',
                        'name': '首页菜单',
                        'url': 'https://example.test/home',
                        'elements': [{
                            'element_key': 'li_40',
                            'name': '关键字管理',
                            'role': 'menuitem',
                            'recommended_locator': {
                                'type': 'role', 'value': 'menuitem', 'name': '关键字管理',
                            },
                        }],
                    },
                    {
                        'page_key': 'page_5',
                        'name': '关键字管理',
                        'url': 'https://example.test/keywords',
                        'elements': [
                            {
                                'element_key': 'input_2',
                                'name': '请输入编号/名称/说明',
                                'placeholder': '请输入编号/名称/说明',
                                'recommended_locator': {
                                    'type': 'placeholder', 'value': '请输入编号/名称/说明',
                                },
                            },
                            {
                                'element_key': 'button_10',
                                'name': '使用指南',
                                'role': 'button',
                                'recommended_locator': {
                                    'type': 'role', 'value': 'button', 'name': '使用指南',
                                },
                            },
                        ],
                    },
                ],
            },
            creator=self.user,
        )

    def test_apply_preserves_page_scoped_identity_and_semantic_fallback(self):
        response = self.client.post(
            f'/api/ui-automation/ai-generation-tasks/{self.task.id}/apply-to-ui-case/',
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        test_case = UiTestCase.objects.get(id=response.data['test_case_id'])
        details = list(
            UiPageStepsDetailed.objects.filter(
                page_step__case_usages__test_case=test_case,
            ).select_related('element__page').order_by('step_sort')
        )
        self.assertEqual(len(details), 5)
        self.assertEqual(details[1].element.page.url, 'https://example.test/login')
        self.assertEqual(details[1].element.locator_value, '密码')
        self.assertEqual(details[2].element.page.url, 'https://example.test/home')
        self.assertEqual(details[2].element.locator_type, 'role')
        self.assertEqual(
            details[2].element.locator_value,
            '{"exact":true,"name":"关键字管理","role":"menuitem"}',
        )
        self.assertEqual(details[3].element.page.url, 'https://example.test/keywords')
        self.assertEqual(details[3].element.locator_value, '请输入编号/名称/说明')
        self.assertEqual(details[4].element.locator_type, 'text')
        self.assertEqual(
            details[4].element.locator_value,
            '{"exact":true,"scope":{"role":"dialog"},"value":"使用指南"}',
        )

    def test_retrieve_uses_lightweight_payload_and_lazy_detail_field(self):
        element_map = dict(self.task.element_map_snapshot)
        element_map['pages'] = [dict(page) for page in element_map['pages']]
        element_map['pages'][0]['elements'] = [
            {
                'element_key': f'button_{index}',
                'name': f'Button {index}',
                'recommended_locator': {'type': 'text', 'value': f'Button {index}'},
            }
            for index in range(100)
        ]
        self.task.element_map_snapshot = element_map
        self.task.verification_result = {
            'large_trace': [{'index': index, 'payload': 'x' * 100} for index in range(200)],
            'report_artifacts': {'summary': {'status': 'success'}},
        }
        self.task.save(update_fields=['element_map_snapshot', 'verification_result'])

        response = self.client.get(
            f'/api/ui-automation/ai-generation-tasks/{self.task.id}/',
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        payload = response.data
        self.assertTrue(payload['payload_summary']['lazy'])
        self.assertTrue(payload['element_map_snapshot']['_lazy_summary'])
        self.assertNotIn('pages', payload['element_map_snapshot'])
        self.assertTrue(payload['verification_result']['_lazy_summary'])
        self.assertNotIn('large_trace', payload['verification_result'])

        field_response = self.client.get(
            f'/api/ui-automation/ai-generation-tasks/{self.task.id}/detail-payload/',
            {'field': 'element_map_snapshot'},
            format='json',
        )

        self.assertEqual(field_response.status_code, status.HTTP_200_OK, field_response.data)
        self.assertEqual(field_response.data['field'], 'element_map_snapshot')
        self.assertEqual(len(field_response.data['value']['pages'][0]['elements']), 100)

    def test_apply_preserves_runtime_input_binding_metadata(self):
        element_map = dict(self.task.element_map_snapshot)
        element_map['pages'] = [dict(page) for page in element_map['pages']]
        login_page = element_map['pages'][0]
        login_page['elements'] = [{
            'element_key': 'input_2',
            'name': '验证码',
            'placeholder': '请输入验证码',
            'recommended_locator': {'type': 'placeholder', 'value': '请输入验证码'},
        }]
        self.task.element_map_snapshot = element_map
        self.task.generated_case = {
            'steps': [{
                'operation': 'fill',
                'binding_mode': 'runtime_input',
                'runtime_resolver': 'image_ocr',
                'page_key': 'page_1',
                'element_key': 'input_2',
                'value': '',
                'target_name': '验证码',
                'description': '识别并填写验证码',
            }],
        }
        self.task.save(update_fields=['element_map_snapshot', 'generated_case'])

        response = self.client.post(
            f'/api/ui-automation/ai-generation-tasks/{self.task.id}/apply-to-ui-case/',
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        detail = UiPageStepsDetailed.objects.get(
            page_step__case_usages__test_case_id=response.data['test_case_id'],
        )
        self.assertEqual(detail.ope_value['text'], '')
        self.assertEqual(detail.ope_value['binding_mode'], 'runtime_input')
        self.assertEqual(detail.ope_value['runtime_resolver'], 'image_ocr')

    def test_apply_falls_back_to_runtime_execution_artifact_steps_when_generated_case_is_empty(self):
        task = UiAiGenerationTask.objects.create(
            project=self.project,
            name='Runtime AI task',
            target_module='产品发布',
            target_url='https://example.test/login',
            status='success',
            generated_script='import { test } from \'@playwright/test\';',
            generated_case={},
            verification_result={
                'runtime_execution_artifact': {
                    'artifact_type': 'runtime_agent_execution',
                    'steps': [{
                        'operation': 'fill',
                        'page_key': 'page_1',
                        'element_key': 'input_2',
                        'value': 'secret',
                        'target_name': '密码',
                        'locator_hint': {'type': 'placeholder', 'value': '密码'},
                        'description': '填写密码',
                    }],
                },
            },
            element_map_snapshot=self.task.element_map_snapshot,
            creator=self.user,
        )

        response = self.client.post(
            f'/api/ui-automation/ai-generation-tasks/{task.id}/apply-to-ui-case/',
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        task.refresh_from_db()
        self.assertIn('steps', task.generated_case)
        self.assertEqual(task.generated_case['steps'][0]['operation'], 'fill')
        test_case = UiTestCase.objects.get(id=response.data['test_case_id'])
        details = list(
            UiPageStepsDetailed.objects.filter(
                page_step__case_usages__test_case=test_case,
            ).select_related('element__page').order_by('step_sort')
        )
        self.assertEqual(len(details), 2)
        self.assertEqual(details[1].element.locator_type, 'placeholder')
        self.assertEqual(details[1].element.locator_value, '密码')

    def test_apply_preserves_runtime_state_assertion_without_static_element(self):
        element_map = dict(self.task.element_map_snapshot)
        element_map['pages'] = [dict(page) for page in element_map['pages']]
        target_page = element_map['pages'][-1]
        target_page['elements'] = [{
            'element_key': 'button_1',
            'name': '状态切换',
            'role': 'button',
            'recommended_locator': {'type': 'role', 'value': 'button', 'name': '状态切换'},
        }]
        self.task.element_map_snapshot = element_map
        self.task.generated_case = {
            'steps': [
                {
                    'operation': 'click',
                    'page_key': target_page['page_key'],
                    'element_key': 'button_1',
                    'target_name': '状态切换',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': '状态切换'},
                    'description': '触发状态变化',
                },
                {
                    'operation': 'assert_value',
                    'binding_mode': 'runtime_state',
                    'runtime_resolver': 'state_evidence',
                    'page_key': target_page['page_key'],
                    'element_key': '',
                    'target_name': '集合状态',
                    'state_assertion': {
                        'scope': '当前页面集合',
                        'expected_state': '已完成',
                    },
                    'description': '验证运行时状态证据',
                },
            ],
        }
        self.task.save(update_fields=['element_map_snapshot', 'generated_case'])

        response = self.client.post(
            f'/api/ui-automation/ai-generation-tasks/{self.task.id}/apply-to-ui-case/',
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        details = list(UiPageStepsDetailed.objects.filter(
            page_step__case_usages__test_case_id=response.data['test_case_id'],
        ).order_by('step_sort'))
        self.assertEqual(len(details), 2)
        self.assertEqual(details[1].step_type, 1)
        self.assertIsNone(details[1].element)
        self.assertEqual(details[1].ope_key, 'assert_value')
        self.assertEqual(details[1].ope_value['binding_mode'], 'runtime_state')
        self.assertEqual(details[1].ope_value['runtime_resolver'], 'state_evidence')
        self.assertEqual(details[1].ope_value['state_assertion']['expected_state'], '已完成')

    def test_apply_compiles_ai_select_option_to_legacy_select_operation(self):
        element_map = dict(self.task.element_map_snapshot)
        element_map['pages'] = [dict(page) for page in element_map['pages']]
        target_page = element_map['pages'][-1]
        target_page['elements'] = [{
            'element_key': 'select_1',
            'name': '状态',
            'role': 'combobox',
            'recommended_locator': {'type': 'role', 'value': 'combobox', 'name': '状态'},
        }]
        self.task.element_map_snapshot = element_map
        self.task.generated_case = {
            'steps': [{
                'operation': 'select_option',
                'page_key': target_page['page_key'],
                'element_key': 'select_1',
                'value': '已冻结',
                'target_name': '状态',
                'description': '选择状态',
            }],
        }
        self.task.save(update_fields=['element_map_snapshot', 'generated_case'])

        response = self.client.post(
            f'/api/ui-automation/ai-generation-tasks/{self.task.id}/apply-to-ui-case/',
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        detail = UiPageStepsDetailed.objects.get(
            page_step__case_usages__test_case_id=response.data['test_case_id'],
        )
        self.assertEqual(detail.ope_key, 'select')
        self.assertEqual(detail.ope_value['value'], '已冻结')


class UiAiGenerationDispatchAckTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='secret')
        self.project = Project.objects.create(name='Demo Project', creator=self.user)
        self.task = UiAiGenerationTask.objects.create(
            project=self.project,
            name='AI 生成用例',
            source_requirement='新建测试任务',
            target_url='https://example.test/login',
            status='running',
            dispatch_id='dispatch-1',
            dispatch_status='acked',
            actuator_id='actuator-1',
            creator=self.user,
        )

    def test_duplicate_ack_does_not_overwrite_user_visible_dispatch_status(self):
        consumer = UiAutomationConsumer()

        async_to_sync(consumer.save_ai_generation_ack)({
            'task_id': self.task.id,
            'dispatch_id': 'dispatch-1',
            'actuator_id': 'actuator-1',
            'status': 'duplicate',
            'source': 'pull',
        })

        self.task.refresh_from_db()
        self.assertEqual(self.task.dispatch_status, 'acked')
        self.assertIsNotNone(self.task.last_ack_at)
        self.assertTrue(any(
            item.get('type') == 'dispatch_ack' and item.get('status') == 'duplicate'
            for item in self.task.repair_history
            if isinstance(item, dict)
        ))

    def test_active_task_heartbeat_renews_lease_without_redispatch(self):
        request = APIRequestFactory().get(
            '/api/ui-automation/ai-generation-tasks/pending-for-actuator/',
            {
                'actuator_id': 'actuator-1',
                'active_task_ids': str(self.task.id),
            },
        )
        request.user = self.user
        request.query_params = request.GET
        view = UiAiGenerationTaskViewSet()
        view.request = request
        view.kwargs = {}

        response = view.pending_for_actuator(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.dispatch_status, 'acked')
        self.assertIsNotNone(self.task.last_ack_at)


class UiPageStepsExecuteDataTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='secret')
        self.project = Project.objects.create(name='Demo Project')
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')
        self.module = UiModule.objects.create(
            project=self.project,
            name='Module A',
            creator=self.user,
        )
        self.page = UiPage.objects.create(
            project=self.project,
            module=self.module,
            name='Login Page',
            url='/login',
            creator=self.user,
        )
        self.element = UiElement.objects.create(
            page=self.page,
            name='Submit Button',
            locator_type='css',
            locator_value='button[type="submit"]',
            locator_index=2,
            locator_type_2='xpath',
            locator_value_2='//button[@type="submit"]',
            locator_index_2=1,
            locator_type_3='text',
            locator_value_3='Submit',
            is_iframe=True,
            iframe_locator='iframe.login-frame',
            creator=self.user,
        )
        self.page_step = UiPageSteps.objects.create(
            project=self.project,
            page=self.page,
            module=self.module,
            name='Submit Login',
            creator=self.user,
        )
        UiPageStepsDetailed.objects.create(
            page_step=self.page_step,
            element=self.element,
            ope_key='click',
            step_sort=0,
        )

    def test_execute_data_includes_iframe_fields(self):
        response = UiPageStepsExecuteSerializer(self.page_step).data
        self.assertEqual(len(response['step_details']), 1)
        detail = response['step_details'][0]
        self.assertEqual(detail['locator_index'], 2)
        self.assertEqual(detail['locator_type_2'], 'xpath')
        self.assertEqual(detail['locator_value_2'], '//button[@type="submit"]')
        self.assertEqual(detail['locator_index_2'], 1)
        self.assertEqual(detail['locator_type_3'], 'text')
        self.assertEqual(detail['locator_value_3'], 'Submit')
        self.assertTrue(detail['is_iframe'])
        self.assertEqual(detail['iframe_locator'], 'iframe.login-frame')

    def test_testcase_execute_data_backfills_ai_execution_artifact_from_source_task(self):
        ts_spec = "import { test } from '@playwright/test';\ntest('generated', async () => {});"
        task = UiAiGenerationTask.objects.create(
            project=self.project,
            name='AI generated case',
            status='success',
            generated_case={
                'steps': [{'operation': 'click'}],
                'playwright_ts_spec': ts_spec,
                'playwright_ts_files': {'generated.spec.ts': ts_spec},
            },
            verification_result={
                'script_artifacts': {
                    'playwright_ts_spec': ts_spec,
                    'playwright_ts_files': {'generated.spec.ts': ts_spec},
                },
            },
        )
        self.page_step.flow_data = {
            'source': 'ai_generation_task',
            'task_id': task.id,
        }
        self.page_step.save(update_fields=['flow_data', 'updated_at'])
        test_case = UiTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='Applied AI case',
            creator=self.user,
        )
        UiCaseStepsDetailed.objects.create(
            test_case=test_case,
            page_step=self.page_step,
            case_sort=0,
        )
        request = APIRequestFactory().get(f'/api/ui-automation/testcases/{test_case.id}/execute-data/')
        request.user = self.user
        request.query_params = request.GET
        view = UiTestCaseViewSet()
        view.request = request
        view.kwargs = {'pk': test_case.id}
        view.action = 'execute_data'
        view.format_kwarg = None

        response = view.execute_data(request, pk=test_case.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        artifact = response.data['result_data']['ai_execution_artifact']
        self.assertEqual(artifact['source_task_id'], task.id)
        self.assertEqual(artifact['playwright_ts_files']['generated.spec.ts'], ts_spec)
        self.assertEqual(response.data['result_data']['ai_execution_mode'], 'typescript_spec')


class UiAiPlanningElementMapCompactionTests(TestCase):
    class TaskStub:
        id = 1
        name = '新建测试任务'
        source_type = 'natural_language'
        source_requirement = '新建弹窗仅填写最简必填项'
        gherkin = ''
        target_module = '测试任务'
        target_url = 'https://example.test/login'
        safety_policy = {
            'dangerous_action_keywords': ['删除', '支付', '审批'],
            'require_confirmation_keywords': ['保存', '提交', '确定', '确认'],
            'allow_form_submit': False,
            'allow_destructive_actions': False,
        }

    def test_compaction_prioritizes_fresh_runtime_page(self):
        element_map = {
            'latest_runtime_page_key': 'repair_observe_1',
            'latest_runtime_url': 'https://example.test/business',
            'pages': [
                {
                    'page_key': 'page_old',
                    'url': 'https://example.test/business',
                    'elements': [{
                        'element_key': f'old_{index}',
                        'name': f'普通元素{index}',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'text', 'value': f'普通元素{index}'},
                    } for index in range(30)],
                },
                {
                    'page_key': 'repair_observe_1',
                    'url': 'https://example.test/business',
                    'runtime_fresh': True,
                    'capture_source': 'failure_reobserve',
                    'elements': [{
                        'element_key': 'fresh_combobox',
                        'name': '运行时下拉框',
                        'role': 'combobox',
                        'actions': ['click'],
                        'recommended_locator': {
                            'type': 'role', 'value': 'combobox', 'name': '运行时下拉框'
                        },
                    }],
                },
            ],
        }

        compact = _compact_element_map(element_map, max_pages=1, max_elements=10, task=self.TaskStub())

        self.assertEqual(compact['latest_runtime_page_key'], 'repair_observe_1')
        self.assertEqual(compact['pages'][0]['page_key'], 'repair_observe_1')
        self.assertTrue(compact['pages'][0]['runtime_fresh'])

    def test_compaction_keeps_late_dialog_form_fields_and_save_button(self):
        filler_page = {
            'page_key': 'page_1',
            'elements': [
                {
                    'element_key': f'button_{index}',
                    'name': f'列表按钮{index}',
                    'text': f'列表按钮{index}',
                    'tag': 'button',
                    'role': 'button',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'text', 'value': f'列表按钮{index}'},
                }
                for index in range(60)
            ],
        }
        dialog_elements = [
            {
                'element_key': f'div_{index}',
                'name': f'普通弹窗文本{index}',
                'text': f'普通弹窗文本{index}',
                'tag': 'div',
                'role': 'group',
                'actions': ['click'],
                'recommended_locator': {'type': 'text', 'value': f'普通弹窗文本{index}'},
            }
            for index in range(100)
        ]
        dialog_elements.extend([
            {
                'element_key': 'input_101',
                'name': '任务名称',
                'label': '任务名称',
                'placeholder': '请输入任务名称',
                'tag': 'input',
                'role': 'textbox',
                'actions': ['fill'],
                'in_dialog': True,
                'dialog_name': '创建任务',
                'recommended_locator': {'type': 'label', 'value': '任务名称'},
            },
            {
                'element_key': 'button_136',
                'name': '保存任务',
                'text': '保存任务',
                'tag': 'button',
                'role': 'button',
                'actions': ['click'],
                'in_dialog': True,
                'dialog_name': '创建任务',
                'recommended_locator': {'type': 'role', 'value': 'button', 'name': '保存任务'},
            },
        ])
        element_map = {
            'base_url': 'https://example.test',
            'pages': [
                filler_page,
                {
                    'page_key': 'page_4',
                    'elements': dialog_elements,
                },
            ],
        }

        compact = _compact_element_map(element_map, max_pages=5, max_elements=160, task=self.TaskStub())
        compact_page = next(page for page in compact['pages'] if page['page_key'] == 'page_4')
        element_keys = {element['element_key'] for element in compact_page['elements']}
        required_keys = {field['element_key'] for field in compact['required_fields']}

        self.assertIn('input_101', element_keys)
        self.assertIn('button_136', element_keys)
        self.assertNotIn('input_101', required_keys)
        self.assertEqual(compact['compaction']['strategy'], 'task_flow_priority')

    def test_default_compaction_limit_is_larger_than_old_160_element_budget(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'page_1',
                    'elements': [
                        {
                            'element_key': f'button_{index}',
                            'name': f'流程相关任务元素{index}',
                            'text': f'流程相关任务元素{index}',
                            'tag': 'button',
                            'role': 'button',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'text', 'value': f'流程相关任务元素{index}'},
                        }
                        for index in range(300)
                    ],
                }
            ],
        }

        compact = _compact_element_map(element_map, task=self.TaskStub())

        self.assertGreaterEqual(compact['compaction']['max_elements'], 500)
        self.assertEqual(compact['compaction']['included_elements'], 300)

    def test_staged_planner_keeps_all_observed_elements_for_binding(self):
        element_map = {
            'pages': [{
                'page_key': 'business_page',
                'url': 'https://example.test/business',
                'elements': [
                    {
                        'element_key': f'element_{index}',
                        'name': f'业务元素 {index}',
                        'role': 'button',
                        'actions': ['click'],
                        'recommended_locator': {
                            'type': 'role',
                            'value': 'button',
                            'name': f'业务元素 {index}',
                        },
                    }
                    for index in range(1200)
                ],
            }],
            'state_transitions': [],
        }

        planner = _StagedUiGenerationPlanner(
            self.TaskStub(),
            {},
            element_map,
            MagicMock(
                id=1,
                config_name='test-config',
                name='test-model',
                request_timeout=60,
                max_retries=1,
                enable_streaming=False,
            ),
            async_mode=True,
            request_timeout=60,
            max_retries=1,
            total_budget=240,
        )

        self.assertEqual(len(planner.compact_map['pages'][0]['elements']), 1200)
        self.assertEqual(len(planner.planning_map['pages'][0]['elements']), 1200)

    def test_selected_route_compiler_inserts_missing_parent_transition(self):
        transitions = [
            {
                'from_page_key': 'home',
                'to_page_key': 'submenu',
                'element_key': 'parent_menu',
                'target_name': 'Parent menu',
                'operation': 'click',
                'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': 'Parent menu'},
            },
            {
                'from_page_key': 'submenu',
                'to_page_key': 'target',
                'element_key': 'target_menu',
                'target_name': 'Target menu',
                'operation': 'click',
                'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': 'Target menu'},
            },
        ]
        steps = [
            {'step_sort': 0, 'operation': 'click', 'element_key': 'login', 'target_name': 'Login'},
            {
                'step_sort': 1,
                'operation': 'click',
                'element_key': 'target_menu',
                'target_name': 'Target menu',
                'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': 'Target menu'},
            },
        ]

        compiled = _compile_selected_route_steps(
            steps,
            {'transition_keys': ['parent_menu', 'target_menu']},
            {'state_transitions': transitions},
            max_steps=10,
        )

        self.assertEqual(
            [step.get('element_key') for step in compiled],
            ['login', 'parent_menu', 'target_menu'],
        )
        self.assertEqual(compiled[1]['state_transition']['to_page_key'], 'submenu')

    def test_flow_transition_compiler_replaces_unobserved_edge_and_restores_missing_action(self):
        requirements = {
            'flow': [
                {'action_id': 'action_3', 'operation': 'click', 'target': '系统管理', 'required': True},
                {'action_id': 'action_4', 'operation': 'click', 'target': '账号管理', 'required': True},
                {'action_id': 'action_6', 'operation': 'click', 'target': 'zhangwenwen@ebupt.com', 'required': True},
            ],
        }
        element_map = {
            'pages': [
                {'page_key': 'page_filter_1', 'elements': [{
                    'element_key': 'li_86',
                    'name': '系统管理账号管理角色管理 登录日志 操作日志',
                    'role': 'menuitem',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'role', 'value': 'menuitem', 'name': '系统管理账号管理角色管理 登录日志 操作日志'},
                }]},
            ],
            'state_transitions': [
                {
                    'stage': 'requirement_route_1',
                    'from_page_key': 'page_probe_4',
                    'to_page_key': 'page_4',
                    'to_url': 'https://example.test/operation-dashboard/index',
                    'element_key': 'li_64',
                    'target_name': '系统管理',
                    'operation': 'click',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '系统管理'},
                },
                {
                    'stage': 'requirement_route_2',
                    'from_page_key': 'page_probe_4',
                    'to_page_key': 'page_filter_1',
                    'to_url': 'https://example.test/system/account',
                    'element_key': 'li_39',
                    'target_name': '账号管理',
                    'operation': 'click',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '账号管理'},
                },
                {
                    'stage': 'coverage_action_1',
                    'from_page_key': 'page_filter_1',
                    'to_page_key': 'page_5',
                    'to_url': 'https://example.test/system/account/detail/481',
                    'element_key': 'span_43',
                    'target_name': 'zhangwenwen@ebupt.com',
                    'operation': 'click',
                    'locator_hint': {'type': 'text', 'value': 'zhangwenwen@ebupt.com'},
                },
            ],
        }
        llm_steps = [
            {
                'step_sort': 0,
                'action_id': 'action_3',
                'operation': 'click',
                'page_key': 'page_filter_1',
                'element_key': 'li_86',
                'target_name': '系统管理账号管理角色管理 登录日志 操作日志',
                'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '系统管理账号管理角色管理 登录日志 操作日志'},
                'state_transition': {'to_page_key': 'page_4', 'to_url': 'https://example.test/system/account'},
            },
            {
                'step_sort': 1,
                'action_id': 'action_6',
                'operation': 'click',
                'page_key': 'page_filter_1',
                'element_key': 'span_43',
                'target_name': 'zhangwenwen@ebupt.com',
                'locator_hint': {'type': 'text', 'value': 'zhangwenwen@ebupt.com'},
                'state_transition': {'to_page_key': 'page_5', 'to_url': 'https://example.test/system/account/detail/481'},
            },
        ]

        compiled = _compile_flow_transition_steps(llm_steps, requirements, element_map, max_steps=10)

        self.assertEqual(
            [(step.get('action_id'), step.get('element_key')) for step in compiled],
            [('action_3', 'li_64'), ('action_4', 'li_39'), ('action_6', 'span_43')],
        )
        validation = _validate_generated_steps(
            compiled,
            {'pages': [], 'state_transitions': element_map['state_transitions']},
            {},
        )
        self.assertEqual(validation['semantic_issues'], [])

    def test_flow_transition_compiler_runs_after_binding_standardization(self):
        requirements = {
            'flow': [
                {'action_id': 'action_3', 'operation': 'click', 'target': '系统管理', 'required': True},
                {'action_id': 'action_4', 'operation': 'click', 'target': '账号管理', 'required': True},
            ],
        }
        element_map = {
            'state_transitions': [
                {
                    'from_page_key': 'page_home',
                    'to_page_key': 'page_system',
                    'to_url': 'https://example.test/system',
                    'element_key': 'li_64',
                    'target_name': '系统管理',
                    'operation': 'click',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '系统管理'},
                },
                {
                    'from_page_key': 'page_system',
                    'to_page_key': 'page_account',
                    'to_url': 'https://example.test/system/account',
                    'element_key': 'li_39',
                    'target_name': '账号管理',
                    'operation': 'click',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '账号管理'},
                },
            ],
        }
        parsed = {
            'generated_case': {
                'steps': [{
                    'step_sort': 0,
                    'action_id': 'action_3',
                    'operation': 'click',
                    'page_key': 'page_home',
                    'element_key': 'li_64',
                    'target_name': '系统管理',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '系统管理'},
                    'state_transition': {'to_page_key': 'page_system', 'to_url': 'https://example.test/system'},
                }],
            },
        }
        bindings = {
            'bindings': [
                {
                    'action_id': 'action_3',
                    'operation': 'click',
                    'page_key': 'page_account',
                    'element_key': 'li_86',
                    'target_name': '系统管理账号管理角色管理 登录日志 操作日志',
                    'locator_hint': {
                        'type': 'role',
                        'value': 'menuitem',
                        'name': '系统管理账号管理角色管理 登录日志 操作日志',
                    },
                },
            ],
        }

        standardized = _standardize_staged_contract_steps(parsed, requirements, bindings)
        standardized_steps = standardized['generated_case']['steps']
        self.assertEqual(standardized_steps[0]['element_key'], 'li_86')

        compiled = _compile_flow_transition_steps(standardized_steps, requirements, element_map, max_steps=10)

        self.assertEqual(
            [(step.get('action_id'), step.get('element_key')) for step in compiled],
            [('action_3', 'li_64'), ('action_4', 'li_39')],
        )
        validation = _validate_generated_steps(
            compiled,
            {'pages': [], 'state_transitions': element_map['state_transitions']},
            {},
        )
        self.assertEqual(validation['semantic_issues'], [])

    def test_final_contract_order_compiler_restores_flow_order_and_covers_duplicate_click(self):
        requirements = {
            'flow': [
                {'action_id': 'action_1', 'operation': 'goto', 'target': '入口页', 'required': True},
                {'action_id': 'action_2', 'operation': 'click', 'target': '认证入口', 'required': True},
                {'action_id': 'action_3', 'operation': 'click', 'target': '一级业务路由', 'required': True},
                {'action_id': 'action_4', 'operation': 'click', 'target': '二级业务路由', 'required': True},
                {'action_id': 'action_5', 'operation': 'fill', 'target': '筛选字段', 'value': 'zhangwenwen@ebupt.com', 'required': True},
                {'action_id': 'action_6', 'operation': 'click', 'target': '筛选结果行', 'required': True},
                {'action_id': 'assert_4', 'operation': 'assert_text', 'target': '详情状态', 'value': '已冻结', 'required': True},
                {'action_id': 'explicit_action_1', 'operation': 'click', 'target': '认证入口', 'required': True},
            ],
        }
        bad_steps = [
            {'step_sort': 0, 'action_id': 'action_1', 'operation': 'goto', 'target_url': 'https://example.test/'},
            {
                'step_sort': 1,
                'action_id': 'action_2',
                'operation': 'click',
                'page_key': 'page_login',
                'element_key': 'button_sso',
                'locator_hint': {'type': 'role', 'value': 'button', 'name': '认证入口'},
            },
            {
                'step_sort': 2,
                'action_id': 'action_3',
                'operation': 'click',
                'page_key': 'page_home',
                'element_key': 'menu_1',
                'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '一级业务路由'},
                'state_transition': {'to_page_key': 'page_system', 'to_url': 'https://example.test/system'},
            },
            {
                'step_sort': 3,
                'action_id': 'action_4',
                'operation': 'click',
                'page_key': 'page_system',
                'element_key': 'menu_2',
                'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '二级业务路由'},
                'state_transition': {'to_page_key': 'page_filter', 'to_url': 'https://example.test/system/account'},
            },
            {
                'step_sort': 4,
                'action_id': 'action_5',
                'operation': 'fill',
                'page_key': 'page_filter',
                'element_key': 'input_account',
                'locator_hint': {'type': 'placeholder', 'value': '请输入账号'},
                'value': 'zhangwenwen@ebupt.com',
            },
            {
                'step_sort': 5,
                'action_id': 'assert_4',
                'operation': 'assert_text',
                'binding_mode': 'runtime_text',
                'value': '已冻结',
            },
            {
                'step_sort': 6,
                'action_id': 'explicit_action_1',
                'operation': 'click',
                'page_key': 'page_login',
                'element_key': 'button_sso',
                'locator_hint': {'type': 'role', 'value': 'button', 'name': '认证入口'},
            },
            {
                'step_sort': 7,
                'action_id': 'action_6',
                'operation': 'click',
                'page_key': 'page_filter',
                'element_key': 'row_account',
                'locator_hint': {'type': 'text', 'value': 'zhangwenwen@ebupt.com'},
                'state_transition': {'to_page_key': 'page_detail', 'to_url': 'https://example.test/system/account/detail/481'},
            },
        ]
        bindings = {
            'bindings': [
                {'action_id': 'action_2', 'operation': 'click', 'page_key': 'page_login', 'element_key': 'button_sso'},
                {'action_id': 'action_3', 'operation': 'click', 'page_key': 'page_home', 'element_key': 'menu_1'},
                {'action_id': 'action_4', 'operation': 'click', 'page_key': 'page_system', 'element_key': 'menu_2'},
                {'action_id': 'action_5', 'operation': 'fill', 'page_key': 'page_filter', 'element_key': 'input_account'},
                {'action_id': 'action_6', 'operation': 'click', 'page_key': 'page_filter', 'element_key': 'row_account'},
                {'action_id': 'assert_4', 'operation': 'assert_text', 'binding_mode': 'runtime_text', 'value': '已冻结'},
                {'action_id': 'explicit_action_1', 'operation': 'click', 'page_key': 'page_login', 'element_key': 'button_sso'},
            ],
        }

        compiled = _compile_final_contract_step_order(bad_steps, requirements, max_steps=20)

        self.assertEqual(
            [step.get('action_id') for step in compiled],
            ['action_1', 'action_2', 'action_3', 'action_4', 'action_5', 'action_6', 'assert_4'],
        )
        self.assertEqual(compiled[1].get('covered_action_ids'), ['explicit_action_1'])
        self.assertEqual(
            _staged_plan_contract_issues(requirements, bindings, {'generated_case': {'steps': compiled}}),
            [],
        )

    def test_flow_transition_compiler_respects_binding_contract_when_auth_transition_is_not_observed(self):
        requirements = {
            'flow': [
                {'action_id': 'action_1', 'operation': 'goto', 'target': '管理平台', 'required': True},
                {'action_id': 'action_2', 'operation': 'click', 'target': 'SSO登录', 'required': True},
                {'action_id': 'action_3', 'operation': 'click', 'target': '系统管理菜单', 'required': True},
                {'action_id': 'action_4', 'operation': 'click', 'target': '账号管理菜单', 'required': True},
                {'action_id': 'explicit_action_1', 'operation': 'click', 'target': 'SSO登录', 'required': True},
            ],
        }
        element_map = {
            'state_transitions': [
                {
                    'stage': 'requirement_route_1',
                    'from_page_key': 'page_probe_4',
                    'to_page_key': 'page_4',
                    'to_url': 'https://example.test/operation-dashboard/index',
                    'element_key': 'li_64',
                    'target_name': '系统管理',
                    'operation': 'click',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '系统管理'},
                },
                {
                    'stage': 'requirement_route_2',
                    'from_page_key': 'page_probe_4',
                    'to_page_key': 'page_4',
                    'to_url': 'https://example.test/system/account',
                    'element_key': 'li_39',
                    'target_name': '账号管理',
                    'operation': 'click',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '账号管理'},
                },
            ],
        }
        bindings = {
            'bindings': [
                {
                    'action_id': 'action_2',
                    'operation': 'click',
                    'page_key': 'page_1',
                    'element_key': 'button_3',
                    'target_name': 'SSO登录',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': 'SSO登录'},
                },
                {
                    'action_id': 'action_3',
                    'operation': 'click',
                    'page_key': 'page_probe_4',
                    'element_key': 'li_64',
                    'target_name': '系统管理',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '系统管理'},
                },
                {
                    'action_id': 'action_4',
                    'operation': 'click',
                    'page_key': 'page_probe_4',
                    'element_key': 'li_39',
                    'target_name': '账号管理',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '账号管理'},
                },
                {
                    'action_id': 'explicit_action_1',
                    'operation': 'click',
                    'page_key': 'page_1',
                    'element_key': 'button_3',
                    'target_name': 'SSO登录',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': 'SSO登录'},
                },
            ],
        }
        parsed = {
            'generated_case': {
                'steps': [
                    {'action_id': 'action_1', 'operation': 'goto', 'value': 'https://example.test/'},
                    {
                        'action_id': 'action_2',
                        'operation': 'click',
                        'page_key': 'page_probe_4',
                        'element_key': 'li_64',
                        'target_name': '系统管理',
                        'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '系统管理'},
                    },
                    {
                        'action_id': 'action_3',
                        'operation': 'click',
                        'page_key': 'page_probe_4',
                        'element_key': 'li_39',
                        'target_name': '账号管理',
                        'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '账号管理'},
                    },
                    {
                        'action_id': 'explicit_action_1',
                        'operation': 'click',
                        'page_key': 'page_1',
                        'element_key': 'button_3',
                        'target_name': 'SSO登录',
                        'locator_hint': {'type': 'role', 'value': 'button', 'name': 'SSO登录'},
                    },
                ],
            },
        }

        standardized = _standardize_staged_contract_steps(parsed, requirements, bindings)
        compiled_steps = _compile_flow_transition_steps(
            standardized['generated_case']['steps'],
            requirements,
            element_map,
            max_steps=10,
            bindings=bindings,
        )
        compiled = {
            'generated_case': {
                'steps': _compile_final_contract_step_order(compiled_steps, requirements, max_steps=10),
            },
        }

        self.assertEqual(
            [(step.get('action_id'), step.get('page_key'), step.get('element_key')) for step in compiled['generated_case']['steps']],
            [
                ('action_1', None, None),
                ('action_2', 'page_1', 'button_3'),
                ('action_3', 'page_probe_4', 'li_64'),
                ('action_4', 'page_probe_4', 'li_39'),
            ],
        )
        self.assertEqual(compiled['generated_case']['steps'][1].get('covered_action_ids'), ['explicit_action_1'])
        self.assertEqual(_staged_plan_contract_issues(requirements, bindings, compiled), [])

    def test_staged_planner_recovers_route_timeout_with_deterministic_fallback(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'login_page',
                    'url': 'https://example.test/login',
                    'elements': [{
                        'element_key': 'login_button',
                        'name': '登录',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': '登录'},
                    }],
                },
                {
                    'page_key': 'business_page',
                    'url': 'https://example.test/business',
                    'elements': [{
                        'element_key': 'search_input',
                        'name': '关键字',
                        'actions': ['fill'],
                        'recommended_locator': {'type': 'placeholder', 'value': '关键字'},
                    }],
                },
            ],
            'state_transitions': [{
                'from_page_key': 'login_page',
                'to_page_key': 'business_page',
                'element_key': 'login_button',
                'target_name': '登录',
                'operation': 'click',
                'locator_hint': {'type': 'role', 'value': 'button', 'name': '登录'},
            }],
        }
        planner = _StagedUiGenerationPlanner(
            self.TaskStub(),
            {},
            element_map,
            MagicMock(
                id=1,
                config_name='test-config',
                name='test-model',
                request_timeout=60,
                max_retries=1,
                enable_streaming=False,
            ),
            async_mode=True,
            request_timeout=60,
            max_retries=1,
            total_budget=240,
        )
        requirements = {
            'goal': '查看业务信息',
            'flow': [{
                'action_id': 'action_1',
                'operation': 'click',
                'target': '登录',
                'required': True,
            }],
            'actions': [{
                'action_id': 'action_1',
                'operation': 'click',
                'target': '登录',
                'required': True,
            }],
            'assertions': [],
        }
        bindings = {
            'bindings': [{
                'action_id': 'action_1',
                'operation': 'click',
                'page_key': 'login_page',
                'element_key': 'login_button',
                'target_name': '登录',
            }],
            'unresolved': [],
        }
        plan = {
            'generated_case': {
                'steps': [{
                    'step_sort': 0,
                    'operation': 'click',
                    'action_id': 'action_1',
                    'page_key': 'login_page',
                    'element_key': 'login_button',
                    'target_name': '登录',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': '登录'},
                }],
            },
            'test_plan': {
                'steps': [{
                    'step_sort': 0,
                    'operation': 'click',
                    'action_id': 'action_1',
                    'page_key': 'login_page',
                    'element_key': 'login_button',
                    'target_name': '登录',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': '登录'},
                }],
            },
        }

        with patch('ui_automation.ai_planning._invoke_planning_stage_json') as invoke:
            invoke.side_effect = [
                (requirements, {'stage': 'requirement_analysis', 'input_chars': 128, 'output_chars': 256, 'elapsed_seconds': 1.0}),
                httpx.ReadTimeout(
                    'The read operation timed out',
                    request=httpx.Request('POST', 'https://example.test/chat/completions'),
                ),
                (bindings, {'stage': 'element_binding', 'input_chars': 512, 'output_chars': 128, 'elapsed_seconds': 1.0}),
                (plan, {'stage': 'executable_plan_generation', 'input_chars': 768, 'output_chars': 192, 'elapsed_seconds': 1.0}),
            ]
            result = planner.run()

        self.assertEqual(
            [call.args[1] for call in invoke.call_args_list],
            [
                'requirement_analysis',
                'state_route_selection',
                'element_binding',
                'executable_plan_generation',
            ],
        )
        self.assertEqual(result['planning_pipeline']['state_route']['fallback_mode'], 'deterministic_page_order')
        self.assertEqual(result['planning_pipeline']['stages'][1]['status'], 'degraded')
        self.assertTrue(result['planning_pipeline']['stages'][1]['fallback_used'])
        self.assertEqual(result['planning_pipeline']['state_route']['ordered_page_keys'], ['login_page', 'business_page'])
        self.assertTrue(result['generated_case']['steps'])

    def test_staged_planner_route_selection_uses_deterministic_seed_when_llm_returns_empty(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'login_page',
                    'url': 'https://example.test/login',
                    'elements': [{
                        'element_key': 'login_button',
                        'name': '登录',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': '登录'},
                    }],
                },
                {
                    'page_key': 'business_page',
                    'url': 'https://example.test/business',
                    'elements': [{
                        'element_key': 'search_input',
                        'name': '关键字',
                        'actions': ['fill'],
                        'recommended_locator': {'type': 'placeholder', 'value': '关键字'},
                    }],
                },
            ],
            'state_transitions': [{
                'from_page_key': 'login_page',
                'to_page_key': 'business_page',
                'element_key': 'login_button',
                'target_name': '登录',
                'operation': 'click',
                'locator_hint': {'type': 'role', 'value': 'button', 'name': '登录'},
            }],
        }
        planner = _StagedUiGenerationPlanner(
            self.TaskStub(),
            {},
            element_map,
            MagicMock(
                id=1,
                config_name='test-config',
                name='test-model',
                request_timeout=60,
                max_retries=1,
                enable_streaming=False,
            ),
            async_mode=True,
            request_timeout=60,
            max_retries=1,
            total_budget=240,
        )
        requirements = {
            'goal': '查看业务信息',
            'flow': [{
                'action_id': 'action_1',
                'operation': 'click',
                'target': '登录',
                'required': True,
            }],
            'actions': [{
                'action_id': 'action_1',
                'operation': 'click',
                'target': '登录',
                'required': True,
            }],
            'assertions': [],
        }

        with patch('ui_automation.ai_planning._invoke_planning_stage_json') as invoke:
            invoke.side_effect = [
                (requirements, {'stage': 'requirement_analysis', 'input_chars': 128, 'output_chars': 256, 'elapsed_seconds': 1.0}),
                ({}, {'stage': 'state_route_selection', 'input_chars': 256, 'output_chars': 0, 'elapsed_seconds': 1.0}),
            ]
            analyzed = planner.analyze_requirements()
            route = planner.select_route(analyzed)

        self.assertEqual(route['ordered_page_keys'][0], 'login_page')
        self.assertEqual(route['route_seed']['ordered_page_keys'][0], 'login_page')
        self.assertEqual(route['transition_keys'], ['login_button'])

    def test_budgeted_prompt_shrinks_large_element_map(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'page_1',
                    'elements': [
                        {
                            'element_key': f'field_{index}',
                            'name': f'业务字段{index}',
                            'text': '很长的页面文本' * 80,
                            'label': f'业务字段{index}',
                            'tag': 'input',
                            'role': 'textbox',
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': f'业务字段{index}'},
                            'locator_candidates': [
                                {'type': 'label', 'value': f'业务字段{index}', 'confidence': 0.9},
                                {'type': 'css', 'value': f'[data-index="{index}"]', 'confidence': 0.5},
                            ],
                        }
                        for index in range(900)
                    ],
                }
            ],
        }

        with patch.dict('os.environ', {'AI_UI_LLM_PROMPT_CHAR_BUDGET': '12000'}):
            _, compact, metrics = _build_budgeted_generation_prompt(self.TaskStub(), {}, element_map)

        self.assertGreater(metrics['prompt_shrink_rounds'], 0)
        self.assertLessEqual(compact['compaction']['max_elements'], 180)
        self.assertLessEqual(compact['compaction']['included_elements'], 180)
        self.assertLessEqual(metrics['prompt_chars'], metrics['prompt_budget_chars'])

    def test_compaction_keeps_state_transitions_for_business_flow(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'page_4',
                    'elements': [
                        {
                            'element_key': 'button_12',
                            'name': '新建测试任务',
                            'text': '新建测试任务',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'role', 'value': 'button', 'name': '新建测试任务'},
                        },
                        {
                            'element_key': 'input_101',
                            'name': '任务名称',
                            'label': '任务名称',
                            'placeholder': '请输入任务名称',
                            'required': True,
                            'in_dialog': True,
                            'dialog_name': '创建任务',
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': '任务名称'},
                        },
                    ],
                }
            ],
            'state_transitions': [
                {
                    'stage': 'open_business_form',
                    'from_page_key': 'page_3',
                    'to_page_key': 'page_4',
                    'element_key': 'button_12',
                    'target_name': '新建测试任务',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': '新建测试任务'},
                }
            ],
        }

        compact = _compact_element_map(element_map, task=self.TaskStub())

        self.assertTrue(compact['state_transitions'])
        self.assertEqual(compact['state_transitions'][0]['target_name'], '新建测试任务')

    def test_llm_plan_does_not_inject_missing_state_transition(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'page_4',
                    'elements': [
                        {
                            'element_key': 'button_12',
                            'name': '新建测试任务',
                            'text': '新建测试任务',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'role', 'value': 'button', 'name': '新建测试任务'},
                        },
                        {
                            'element_key': 'input_101',
                            'name': '任务名称',
                            'label': '任务名称',
                            'placeholder': '请输入任务名称',
                            'required': True,
                            'in_dialog': True,
                            'dialog_name': '创建任务',
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': '任务名称'},
                        },
                    ],
                }
            ],
            'state_transitions': [
                {
                    'stage': 'open_business_form',
                    'from_page_key': 'page_3',
                    'to_page_key': 'page_4',
                    'element_key': 'button_12',
                    'target_name': '新建测试任务',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': '新建测试任务'},
                }
            ],
        }
        parsed = {
            'generated_case': {
                'steps': [
                    {
                        'operation': 'fill',
                        'page_key': 'page_4',
                        'element_key': 'input_101',
                        'target_name': '任务名称',
                        'description': '填写任务名称',
                        'locator_hint': {'type': 'label', 'value': '任务名称'},
                        'value': '自动化测试数据',
                    },
                ],
            },
            'test_plan': {},
        }

        normalized = _normalize_llm_plan(self.TaskStub(), parsed, element_map)
        steps = normalized['generated_case']['steps']

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['target_name'], '任务名称')
        self.assertEqual(steps[0]['operation'], 'fill')

    def test_plan_validation_treats_state_transition_as_bound_element(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'page_4',
                    'elements': [
                        {
                            'element_key': 'input_101',
                            'name': '任务名称',
                            'label': '任务名称',
                            'required': True,
                            'in_dialog': True,
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': '任务名称'},
                        },
                    ],
                },
            ],
            'state_transitions': [
                {
                    'stage': 'open_business_form',
                    'from_page_key': 'page_3',
                    'to_page_key': 'page_4',
                    'element_key': 'button_12',
                    'target_name': '新建测试任务',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': '新建测试任务'},
                },
            ],
        }
        steps = [
            {
                'operation': 'click',
                'page_key': 'page_3',
                'element_key': 'button_12',
                'target_name': '新建测试任务',
                'locator_hint': {'type': 'role', 'value': 'button', 'name': '新建测试任务'},
            },
            {
                'operation': 'fill',
                'page_key': 'page_4',
                'element_key': 'input_101',
                'target_name': '任务名称',
                'locator_hint': {'type': 'label', 'value': '任务名称'},
                'value': '自动化测试数据',
            },
        ]

        validation = _validate_generated_steps(steps, element_map, self.TaskStub.safety_policy)

        self.assertEqual(validation['status'], 'success')
        self.assertEqual(validation['unresolved_step_count'], 0)
        self.assertEqual(validation['bound_element_steps'], 2)

    def test_unresolved_required_gap_is_not_counted_as_executable_binding(self):
        element_map = {
            'pages': [{
                'page_key': 'page_1',
                'elements': [{
                    'element_key': 'search_button',
                    'name': '搜索',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'role', 'value': 'button', 'name': '搜索'},
                }],
            }],
        }
        steps = [{
            'action_id': 'search_by_id',
            'action': 'unresolved_required_gap',
            'operation': 'fill',
            'page_key': 'page_1',
            'element_key': 'search_button',
            'target_name': '企业编号',
            'description': '未观察到支持填写企业编号的输入元素',
        }]

        validation = _validate_generated_steps(steps, element_map, self.TaskStub.safety_policy)

        self.assertEqual(validation['status'], 'failed')
        self.assertEqual(validation['executable_steps'], 0)
        self.assertEqual(validation['bound_element_steps'], 0)
        self.assertEqual(validation['unresolved_steps'][0]['action_id'], 'search_by_id')

    def test_unexecutable_gap_alias_is_not_counted_as_executable_binding(self):
        element_map = {
            'pages': [{
                'page_key': 'page_1',
                'elements': [{
                    'element_key': 'button_1',
                    'name': 'Open',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Open'},
                }],
            }],
        }
        steps = [{
            'action_id': 'missing_input',
            'action': 'unexecutable_gap',
            'operation': 'fill',
            'page_key': 'page_1',
            'element_key': 'button_1',
            'target_name': 'Record id',
            'description': 'Required input was not observed',
        }]

        validation = _validate_generated_steps(steps, element_map, self.TaskStub.safety_policy)

        self.assertEqual(validation['status'], 'failed')
        self.assertEqual(validation['executable_steps'], 0)
        self.assertEqual(validation['bound_element_steps'], 0)
        self.assertEqual(validation['unresolved_steps'][0]['action_id'], 'missing_input')

    def test_goto_does_not_require_element_binding_even_when_binding_stage_marked_unresolved(self):
        steps = [{
            'action_id': 'open_initial_url',
            'action': 'unresolved_required_gap',
            'operation': 'goto',
            'value': 'https://example.test/login',
            'target_name': '登录页',
        }]

        validation = _validate_generated_steps(steps, {'pages': []}, self.TaskStub.safety_policy)

        self.assertEqual(validation['status'], 'success')
        self.assertEqual(validation['executable_steps'], 1)
        self.assertEqual(validation['unresolved_step_count'], 0)

    def test_unobserved_explicit_click_does_not_force_text_only_coverage(self):
        class ExplicitClickTask(self.TaskStub):
            gherkin = 'When 用户点击“结果状态”按钮'

        element_map = {
            'pages': [{
                'page_key': 'page_1',
                'elements': [{
                    'element_key': 'submit_button',
                    'name': '提交',
                    'text': '提交',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'role', 'value': 'button', 'name': '提交'},
                }],
            }],
        }
        steps = [{
            'operation': 'click',
            'page_key': 'page_1',
            'element_key': 'submit_button',
            'target_name': '提交',
        }]

        validation = _validate_generated_steps(steps, element_map, self.TaskStub.safety_policy, ExplicitClickTask())

        self.assertNotIn(
            'missing_explicit_click_action',
            {issue['reason'] for issue in validation['semantic_issues']},
        )

    def test_staged_requirement_contract_merges_missing_flow_items_and_matched_explicit_actions(self):
        requirements = {
            'flow': [
                {'action_id': 'action_1', 'operation': 'goto', 'target': 'Entry', 'required': True},
            ],
            'actions': [
                {'action_id': 'action_1', 'operation': 'goto', 'target': 'Entry', 'required': True},
                {'action_id': 'action_2', 'operation': 'click', 'target': 'Open panel', 'required': True},
            ],
            'assertions': [
                {'action_id': 'assert_1', 'operation': 'assert_visible', 'target': 'Panel', 'required': True},
            ],
        }
        explicit = {
            'fields': [],
            'clicks': [{'name': 'Confirm flow', 'operation': 'click', 'source': 'quoted_click'}],
            'assertions': [],
        }
        compact_map = {
            'explicit_action_requirements': {
                **explicit,
                'matched_elements': [{
                    'requirement': explicit['clicks'][0],
                    'page_key': 'page_2',
                    'element_key': 'button_confirm',
                    'name': 'Confirm flow',
                    'actions': ['click'],
                }],
            },
        }

        completed = _complete_staged_requirement_contract(requirements, explicit, compact_map)

        self.assertEqual(
            [item['action_id'] for item in completed['flow']],
            ['action_1', 'action_2', 'assert_1', 'explicit_action_1'],
        )
        self.assertEqual(completed['flow'][-1]['operation'], 'click')
        self.assertEqual(completed['flow'][-1]['matched_element_key'], 'button_confirm')

    def test_staged_requirement_contract_does_not_treat_short_action_as_covering_specific_action(self):
        requirements = {
            'flow': [
                {'action_id': 'action_1', 'operation': 'click', 'target': 'Open', 'required': True},
            ],
            'actions': [
                {'action_id': 'action_1', 'operation': 'click', 'target': 'Open', 'required': True},
            ],
        }
        explicit = {
            'fields': [],
            'clicks': [{'name': 'Open advanced panel', 'operation': 'click', 'source': 'quoted_click'}],
            'assertions': [],
        }
        compact_map = {
            'explicit_action_requirements': {
                **explicit,
                'matched_elements': [{
                    'requirement': explicit['clicks'][0],
                    'page_key': 'page_2',
                    'element_key': 'button_advanced',
                    'name': 'Open advanced panel',
                    'actions': ['click'],
                }],
            },
        }

        completed = _complete_staged_requirement_contract(requirements, explicit, compact_map)

        self.assertEqual(
            [item['action_id'] for item in completed['flow']],
            ['action_1', 'explicit_action_1'],
        )
        self.assertEqual(completed['flow'][-1]['target'], 'Open advanced panel')

    def test_unresolved_binding_contract_standardizes_llm_gap_steps_and_removes_fake_binding(self):
        parsed = {'test_plan': {'steps': [{
            'action_id': 'missing_required_field',
            'action': 'cannot continue because field is missing',
            'operation': 'fill',
            'page_key': 'page_1',
            'element_key': 'similar_button',
            'target_name': 'Required field',
            'locator_hint': {'type': 'role', 'value': 'button', 'name': 'Similar'},
        }]}}
        bindings = {'unresolved': [{
            'action_id': 'missing_required_field',
            'required': True,
            'reason': 'No observed editable field matches this required action',
        }]}

        normalized = _standardize_unresolved_gap_steps(parsed, bindings)
        step = normalized['test_plan']['steps'][0]

        self.assertEqual(step['action'], 'unresolved_required_gap')
        self.assertEqual(step['page_key'], '')
        self.assertEqual(step['element_key'], '')
        self.assertEqual(step['binding_mode'], 'unresolved')
        self.assertFalse(step['requires_confirmation'])
        self.assertEqual(step['unresolved_reason'], 'No observed editable field matches this required action')

    def test_staged_contract_standardization_applies_binding_keys_and_flow_order(self):
        requirements = {'flow': [
            {'action_id': 'fill_name', 'operation': 'fill', 'required': True},
            {'action_id': 'save_form', 'operation': 'click', 'required': True},
        ]}
        bindings = {'bindings': [
            {
                'action_id': 'fill_name',
                'operation': 'fill',
                'page_key': 'dialog',
                'element_key': 'name_input',
                'target_name': 'Name',
                'locator_hint': {'type': 'label', 'value': 'Name'},
            },
            {
                'action_id': 'save_form',
                'operation': 'click',
                'page_key': 'dialog',
                'element_key': 'save_button',
                'target_name': 'Save',
                'locator_hint': {'type': 'role', 'value': 'button', 'name': 'Save'},
            },
        ]}
        parsed = {'test_plan': {'steps': [
            {
                'action_id': 'save_form',
                'operation': 'click',
                'page_key': 'wrong',
                'element_key': 'wrong_save',
                'target_name': 'Save',
            },
            {
                'action_id': 'fill_name',
                'operation': 'fill',
                'page_key': 'wrong',
                'element_key': 'similar_label',
                'target_name': 'Name',
                'value': 'case data',
            },
        ]}}

        normalized = _standardize_staged_contract_steps(parsed, requirements, bindings)
        steps = normalized['test_plan']['steps']

        self.assertEqual([step['action_id'] for step in steps], ['fill_name', 'save_form'])
        self.assertEqual(steps[0]['page_key'], 'dialog')
        self.assertEqual(steps[0]['element_key'], 'name_input')
        self.assertEqual(steps[0]['value'], 'case data')
        self.assertEqual(steps[1]['page_key'], 'dialog')
        self.assertEqual(steps[1]['element_key'], 'save_button')

    def test_planning_contract_module_exposes_stable_contract_interfaces(self):
        requirements = {
            'flow': [{'action_id': 'open', 'operation': 'click', 'target': 'Open', 'required': True}],
            'actions': [{'action_id': 'fill_name', 'operation': 'fill', 'target': 'Name', 'required': True}],
        }
        route_result = {
            'ordered_page_keys': ['home'],
            'transition_keys': ['open_button'],
            'route_actions': [{'action_id': 'open', 'page_key': 'home'}],
            'assumptions': ['seed'],
            'route_seed': {'ordered_page_keys': ['home'], 'transition_keys': ['open_button']},
        }
        compact_map = {'explicit_action_requirements': {'matched_elements': [{
            'requirement': {'name': 'Save', 'operation': 'click', 'source': 'quoted_click'},
            'page_key': 'dialog',
            'element_key': 'save_button',
            'name': 'Save',
            'actions': ['click'],
        }]}}
        binding_result = {'bindings': [
            {'action_id': 'open', 'operation': 'click', 'page_key': 'home', 'element_key': 'open_button'},
            {'action_id': 'fill_name', 'operation': 'fill', 'page_key': 'dialog', 'element_key': 'name_input'},
        ]}
        parsed = {'test_plan': {'steps': [
            {'action_id': 'fill_name', 'operation': 'fill', 'page_key': 'wrong', 'element_key': 'wrong'},
            {'action_id': 'open', 'operation': 'click', 'page_key': 'wrong', 'element_key': 'wrong'},
        ]}}

        contract = build_requirement_contract(requirements, {}, compact_map)
        route_contract = RouteContract.from_result(route_result)
        binding_contract = BindingContract.from_result(binding_result)
        compiled = standardize_staged_contract_steps(parsed, contract.as_dict(), binding_result)
        steps = compiled['test_plan']['steps']

        self.assertEqual(contract.action_ids, ('open', 'fill_name', 'explicit_action_1'))
        self.assertEqual(route_contract.as_dict()['page_keys'], ['home'])
        self.assertEqual(route_contract.as_dict()['transition_keys'], ['open_button'])
        self.assertIn('fill_name', binding_contract.bindings)
        self.assertEqual([step['action_id'] for step in steps], ['open', 'fill_name'])
        self.assertEqual(steps[0]['page_key'], 'home')
        self.assertEqual(steps[1]['element_key'], 'name_input')

    def test_planning_contract_coverage_gate_blocks_required_unresolved_bindings(self):
        requirements = {'flow': [
            {'action_id': 'fill_required', 'operation': 'fill', 'target': 'Required field', 'required': True},
            {'action_id': 'optional_assert', 'operation': 'assert_visible', 'target': 'Optional result', 'required': False},
        ]}
        bindings = {'unresolved': [
            {'action_id': 'fill_required', 'required': True, 'reason': 'No editable element observed'},
            {'action_id': 'optional_assert', 'required': False, 'reason': 'Optional assertion not observed'},
        ]}

        coverage = exploration_coverage_from_bindings(requirements, bindings)

        self.assertFalse(coverage['planning_allowed'])
        self.assertEqual(coverage['missing_action_count'], 1)
        self.assertEqual(coverage['missing_required_actions'][0]['action_id'], 'fill_required')

    def test_planning_contract_coverage_gate_defers_post_action_assertions(self):
        requirements = {'flow': [
            {'action_id': 'open_target_state', 'operation': 'click', 'target': 'Entry Control', 'required': True},
            {'action_id': 'fill_required', 'operation': 'fill', 'target': 'Required Field', 'required': True},
            {'action_id': 'assert_after_action', 'operation': 'assert_text', 'target': 'Runtime Result', 'required': True},
        ]}
        bindings = {
            'bindings': [
                {
                    'action_id': 'open_target_state',
                    'operation': 'click',
                    'page_key': 'page_1',
                    'element_key': 'button_1',
                },
                {
                    'action_id': 'fill_required',
                    'operation': 'fill',
                    'page_key': 'page_2',
                    'element_key': 'input_1',
                },
            ],
            'unresolved': [
                {
                    'action_id': 'assert_after_action',
                    'required': True,
                    'reason': 'Runtime result appears after prior actions execute',
                },
            ],
        }

        coverage = exploration_coverage_from_bindings(requirements, bindings)

        self.assertTrue(coverage['planning_allowed'])
        self.assertEqual(coverage['missing_action_count'], 0)
        self.assertEqual(coverage['deferred_assertion_count'], 1)
        self.assertEqual(coverage['deferred_assertions'][0]['action_id'], 'assert_after_action')

    def test_contract_validation_module_matches_legacy_wrappers(self):
        requirements = {'flow': [{
            'action_id': 'open',
            'operation': 'click',
            'required': True,
        }]}
        bindings = {'bindings': [{
            'action_id': 'open',
            'operation': 'click',
            'page_key': 'home',
            'element_key': 'open_button',
        }]}
        parsed = {'test_plan': {'steps': [{
            'action_id': 'open',
            'operation': 'click',
            'page_key': 'home',
            'element_key': 'open_button',
        }]}}

        self.assertEqual(staged_binding_contract_issues(requirements, bindings), [])
        self.assertEqual(staged_plan_contract_issues(requirements, bindings, parsed), [])
        self.assertEqual(_staged_binding_contract_issues(requirements, bindings), [])
        self.assertEqual(_staged_plan_contract_issues(requirements, bindings, parsed), [])

    def test_staged_planner_stops_before_plan_generation_when_binding_coverage_is_missing(self):
        element_map = {'pages': [], 'state_transitions': []}
        planner = _StagedUiGenerationPlanner(
            self.TaskStub(),
            {},
            element_map,
            object(),
            async_mode=True,
            request_timeout=60,
            max_retries=1,
            total_budget=240,
        )
        requirements = {'flow': [{
            'action_id': 'fill_required',
            'operation': 'fill',
            'target': 'Required field',
            'required': True,
        }]}
        route = {'ordered_page_keys': [], 'transition_keys': []}
        bindings = {'bindings': [], 'unresolved': [{
            'action_id': 'fill_required',
            'required': True,
            'reason': 'No editable element observed',
        }]}

        with patch('ui_automation.ai_planning._invoke_planning_stage_json') as invoke:
            invoke.side_effect = [
                (requirements, {'stage': 'requirement_analysis'}),
                (route, {'stage': 'state_route_selection'}),
                (bindings, {'stage': 'element_binding'}),
            ]
            result = planner.run()

        invoked_stage_names = [call.args[1] for call in invoke.call_args_list]
        self.assertEqual(invoked_stage_names, [
            'requirement_analysis',
            'state_route_selection',
            'element_binding',
        ])
        self.assertEqual(result['planning_pipeline']['status'], 'blocked')
        self.assertEqual(result['planning_pipeline']['blocked_stage'], 'exploration_coverage_gate')
        self.assertEqual(result['plan_validation']['semantic_issues'][0]['reason'], 'exploration_coverage_required_binding_unresolved')

    def test_llm_plan_does_not_reorder_or_inject_observed_transitions(self):
        element_map = {
            'pages': [{
                'page_key': 'page_4',
                'elements': [{
                    'element_key': 'input_101',
                    'name': '任务名称',
                    'label': '任务名称',
                    'in_dialog': True,
                    'actions': ['fill'],
                    'recommended_locator': {'type': 'label', 'value': '任务名称'},
                }],
            }],
            'state_transitions': [
                {
                    'from_page_key': 'page_probe_3',
                    'to_page_key': 'page_3',
                    'to_url': 'https://example.test/home/testingTask',
                    'element_key': 'li_24',
                    'target_name': '测试任务',
                    'stage': 'keyword_followup',
                    'locator_hint': {'type': 'role', 'value': 'menuitem', 'name': '测试任务'},
                },
                {
                    'from_page_key': 'page_probe_4',
                    'to_page_key': 'page_4',
                    'to_url': 'https://example.test/home/testingTask',
                    'element_key': 'button_12',
                    'target_name': '新建测试任务',
                    'stage': 'open_business_form',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': '新建测试任务'},
                },
            ],
        }
        parsed = {'generated_case': {'steps': [{
            'operation': 'fill',
            'page_key': 'page_4',
            'element_key': 'input_101',
            'target_name': '任务名称',
            'locator_hint': {'type': 'label', 'value': '任务名称'},
            'value': '测试数据',
        }]}}

        steps = _normalize_llm_plan(self.TaskStub(), parsed, element_map)['generated_case']['steps']

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['element_key'], 'input_101')
        self.assertFalse(any(step.get('state_transition') for step in steps))

    def test_staged_contract_rejects_reordered_and_rebound_actions(self):
        requirements = {
            'flow': [
                {'action_id': 'login', 'operation': 'click', 'required': True},
                {'action_id': 'open_form', 'operation': 'click', 'required': True},
            ],
        }
        bindings = {
            'bindings': [
                {'action_id': 'login', 'page_key': 'login', 'element_key': 'submit'},
                {'action_id': 'open_form', 'page_key': 'list', 'element_key': 'create'},
            ],
        }
        parsed = {
            'test_plan': {
                'steps': [
                    {'action_id': 'open_form', 'operation': 'click', 'page_key': 'list', 'element_key': 'create'},
                    {'action_id': 'login', 'operation': 'click', 'page_key': 'login', 'element_key': 'wrong'},
                ],
            },
        }

        reasons = {
            issue['reason']
            for issue in _staged_plan_contract_issues(requirements, bindings, parsed)
        }

        self.assertIn('staged_action_order_mismatch', reasons)
        self.assertIn('staged_binding_contract_mismatch', reasons)

    def test_staged_contract_accepts_declared_unresolved_gap_step(self):
        requirements = {
            'flow': [
                {'action_id': 'search_by_id', 'operation': 'fill', 'required': True},
            ],
        }
        bindings = {
            'bindings': [],
            'unresolved': [{
                'action_id': 'search_by_id',
                'required': True,
                'reason': '没有观察到可填写的搜索输入',
            }],
        }
        parsed = {
            'test_plan': {
                'steps': [{
                    'action_id': 'search_by_id',
                    'action': 'unresolved_required_gap',
                    'operation': 'fill',
                    'page_key': 'page_1',
                    'element_key': '',
                }],
            },
        }

        self.assertEqual(_staged_binding_contract_issues(requirements, bindings), [])
        self.assertEqual(_staged_plan_contract_issues(requirements, bindings, parsed), [])

    def test_runtime_text_assertion_is_readonly_and_does_not_require_element_map_binding(self):
        parsed = {
            'test_plan': {'steps': [{
                'action_id': 'assert_created',
                'operation': 'assert_visible',
                'binding_mode': 'runtime_text',
                'value': '新建后的唯一名称',
                'target_name': '新建后的列表项',
            }]},
        }
        normalized = _normalize_llm_plan(self.TaskStub(), parsed, {'pages': []})
        step = normalized['test_plan']['steps'][0]

        validation = _validate_generated_steps(
            normalized['test_plan']['steps'],
            {'pages': []},
            self.TaskStub.safety_policy,
        )
        spec = _build_typescript_spec_from_steps(
            self.TaskStub(),
            normalized['test_plan']['steps'],
            {'base_url': 'https://example.test'},
            {},
        )

        self.assertEqual(validation['status'], 'success')
        self.assertFalse(step['requires_confirmation'])
        self.assertEqual(step['locator_hint'], {'type': 'text', 'value': '新建后的唯一名称'})
        self.assertIn("page.locator('body')", spec)
        self.assertIn('新建后的唯一名称', spec)

    def test_runtime_text_assertion_must_match_fresh_runtime_page_when_available(self):
        steps = [{
            'step_sort': 1,
            'action_id': 'assert_runtime_text',
            'operation': 'assert_text',
            'binding_mode': 'runtime_text',
            'target_name': 'Alpha status',
            'value': 'Alpha detail',
            'locator_hint': {'type': 'text', 'value': 'Alpha detail'},
        }]
        element_map = {'pages': [{
            'page_key': 'runtime_state',
            'runtime_fresh': True,
            'elements': [{'element_key': 'notice', 'text': 'Alpha summary'}],
        }]}

        validation = _validate_generated_steps(steps, element_map, self.TaskStub.safety_policy)

        self.assertEqual(validation['status'], 'failed')
        self.assertIn(
            'runtime_assertion_text_not_observed',
            {item.get('reason') for item in validation['semantic_issues']},
        )

    def test_runtime_text_assertion_allows_structural_reveal_candidate(self):
        steps = [
            {
                'step_sort': 1,
                'action_id': 'fill_anchor',
                'operation': 'fill',
                'page_key': 'runtime_state',
                'element_key': 'input_anchor',
                'target_name': 'Alpha field',
                'value': 'sample',
                'locator_hint': {'type': 'label', 'value': 'Alpha field'},
            },
            {
                'step_sort': 2,
                'action_id': 'assert_runtime_text',
                'operation': 'assert_text',
                'binding_mode': 'runtime_text',
                'target_name': 'Alpha guidance',
                'value': 'Alpha detailed requirement',
                'locator_hint': {'type': 'text', 'value': 'Alpha detailed requirement'},
            },
        ]
        element_map = {'pages': [{
            'page_key': 'runtime_state',
            'runtime_fresh': True,
            'elements': [
                {
                    'element_key': 'input_anchor',
                    'role': 'textbox',
                    'tag': 'input',
                    'label': 'Alpha field',
                    'actions': ['fill'],
                    'visible': True,
                    'enabled': True,
                    'bounding_box': {'x': 100, 'y': 100, 'width': 280, 'height': 30},
                    'recommended_locator': {'type': 'label', 'value': 'Alpha field', 'confidence': 0.9},
                },
                {
                    'element_key': 'reveal_control',
                    'role': 'link',
                    'tag': 'a',
                    'text': 'Alpha guidance',
                    'actions': ['click'],
                    'visible': True,
                    'enabled': True,
                    'in_form': True,
                    'href': 'javascript:void(0)',
                    'bounding_box': {'x': 120, 'y': 148, 'width': 80, 'height': 18},
                    'recommended_locator': {'type': 'role', 'value': 'link', 'name': 'Alpha guidance', 'confidence': 0.84},
                    'locator_candidates': [{'type': 'text', 'value': 'Alpha guidance', 'confidence': 0.72}],
                },
            ],
        }]}

        validation = _validate_generated_steps(steps, element_map, self.TaskStub.safety_policy)
        spec = _build_typescript_spec_from_steps(
            self.TaskStub(),
            steps,
            {'base_url': 'https://example.test', **element_map},
            {},
        )

        self.assertEqual(validation['status'], 'success')
        self.assertEqual(validation['semantic_issues'], [])
        self.assertIn('revealRuntimeText', spec)
        self.assertIn("page.locator('body')", spec)
        self.assertIn('Alpha detailed requirement', spec)

    def test_unbound_state_assertions_compile_to_runtime_state_evidence_contract(self):
        requirements = {'flow': [
            {'action_id': 'toggle_on', 'operation': 'click', 'target': 'Primary toggle', 'required': True},
            {
                'action_id': 'assert_on',
                'operation': 'assert_value',
                'target': 'Collection state',
                'expected': 'all items selected',
                'required': True,
            },
            {'action_id': 'toggle_off', 'operation': 'click', 'target': 'Secondary toggle', 'required': True},
            {
                'action_id': 'assert_off',
                'operation': 'assert_value',
                'target': 'Collection state',
                'expected': 'all items unselected',
                'required': True,
            },
        ]}
        element_map = {'pages': [{
            'page_key': 'collection_page',
            'title': 'Collection page',
            'elements': [
                {
                    'element_key': 'toggle_on_button',
                    'name': 'Primary toggle',
                    'role': 'button',
                    'tag': 'button',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Primary toggle'},
                },
                {
                    'element_key': 'toggle_off_button',
                    'name': 'Secondary toggle',
                    'role': 'button',
                    'tag': 'button',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Secondary toggle'},
                },
                {'element_key': 'item_a', 'text': 'Item A', 'tag': 'span', 'actions': []},
                {'element_key': 'item_b', 'text': 'Item B', 'tag': 'span', 'actions': []},
            ],
        }]}
        bindings = {
            'bindings': [
                {
                    'action_id': 'toggle_on',
                    'operation': 'click',
                    'page_key': 'collection_page',
                    'element_key': 'toggle_on_button',
                    'target_name': 'Primary toggle',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': 'Primary toggle'},
                },
                {
                    'action_id': 'toggle_off',
                    'operation': 'click',
                    'page_key': 'collection_page',
                    'element_key': 'toggle_off_button',
                    'target_name': 'Secondary toggle',
                    'locator_hint': {'type': 'role', 'value': 'button', 'name': 'Secondary toggle'},
                },
            ],
            'unresolved': [
                {'action_id': 'assert_on', 'required': True, 'reason': 'no static state control observed'},
                {'action_id': 'assert_off', 'required': True, 'reason': 'no static state control observed'},
            ],
        }

        compiled = _compile_runtime_state_evidence_bindings(
            requirements,
            bindings,
            element_map,
            {'ordered_page_keys': ['collection_page']},
        )
        binding_by_action = {
            item['action_id']: item
            for item in compiled['bindings']
        }

        self.assertEqual(compiled['unresolved'], [])
        self.assertEqual(binding_by_action['assert_on']['binding_mode'], 'runtime_state')
        self.assertEqual(binding_by_action['assert_on']['runtime_resolver'], 'state_evidence')
        self.assertEqual(binding_by_action['assert_on']['state_assertion']['previous_action_id'], 'toggle_on')
        self.assertEqual(binding_by_action['assert_off']['state_assertion']['previous_action_id'], 'toggle_off')
        self.assertEqual(staged_binding_contract_issues(requirements, compiled), [])
        self.assertTrue(is_assertion_operation(binding_by_action['assert_on']['operation']))

    def test_runtime_state_assertion_does_not_require_static_element_binding_and_generates_ts_evidence_call(self):
        steps = [
            {
                'action_id': 'toggle_on',
                'operation': 'click',
                'page_key': 'collection_page',
                'element_key': 'toggle_on_button',
                'target_name': 'Primary toggle',
                'locator_hint': {'type': 'role', 'value': 'button', 'name': 'Primary toggle'},
            },
            {
                'action_id': 'assert_on',
                'operation': 'assert_value',
                'binding_mode': 'runtime_state',
                'runtime_resolver': 'state_evidence',
                'page_key': 'collection_page',
                'element_key': '',
                'target_name': 'Collection state',
                'expected': 'all items selected',
                'state_assertion': {
                    'target': 'Collection state',
                    'expected': 'all items selected',
                    'previous_action_id': 'toggle_on',
                    'page_key': 'collection_page',
                },
            },
        ]
        element_map = {'pages': [{
            'page_key': 'collection_page',
            'title': 'Collection page',
            'elements': [{
                'element_key': 'toggle_on_button',
                'name': 'Primary toggle',
                'role': 'button',
                'tag': 'button',
                'actions': ['click'],
                'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Primary toggle'},
            }],
        }]}

        validation = _validate_generated_steps(steps, element_map, self.TaskStub.safety_policy)
        spec = _build_typescript_spec_from_steps(
            self.TaskStub(),
            steps,
            {'base_url': 'https://example.test', **element_map},
            {},
        )

        self.assertEqual(validation['status'], 'success')
        self.assertEqual(validation['unresolved_step_count'], 0)
        self.assertIn('assertRuntimeStateChange', spec)
        self.assertIn('"binding_mode": "runtime_state"', spec)

    def test_runtime_state_assertion_includes_expected_text_gate_in_ts_helper(self):
        steps = [
            {
                'action_id': 'assert_detail',
                'operation': 'assert_visible',
                'binding_mode': 'runtime_state',
                'runtime_resolver': 'state_evidence',
                'page_key': 'detail_page',
                'element_key': '',
                'target_name': '发布概览',
                'expected': '发布状态 已生效',
                'state_assertion': {
                    'target': '发布概览',
                    'expected': '发布状态 已生效',
                    'previous_action_id': 'open_detail',
                    'page_key': 'detail_page',
                },
            },
        ]
        element_map = {'pages': [{
            'page_key': 'detail_page',
            'elements': [],
        }]}

        spec = _build_typescript_spec_from_steps(
            self.TaskStub(),
            steps,
            {'base_url': 'https://example.test', **element_map},
            {},
        )

        self.assertIn('expectedText', spec)
        self.assertIn('toContain(expectedText)', spec)
        self.assertIn('运行时状态证据未包含期望文本', spec)

    def test_staged_planner_retries_runtime_text_assertion_semantic_failure(self):
        class PlannerStub(_StagedUiGenerationPlanner):
            def __init__(self):
                self.task = UiAiPlanningElementMapCompactionTests.TaskStub()
                self.payload = {}
                self.element_map = {'pages': [{
                    'page_key': 'runtime_state',
                    'runtime_fresh': True,
                    'elements': [{'element_key': 'notice', 'text': 'Visible runtime fact'}],
                }]}
                self.active_config = MagicMock(id=1, name='stub-llm', model_name='stub-llm')
                self.async_mode = True
                self.request_timeout = 30
                self.max_retries = 1
                self.total_budget = 120
                self.started_at = 0
                self.deadline = 999999
                self.stage_metrics = []
                self.requirement_document = {}
                self.requirement_contract = build_requirement_contract({
                    'flow': [{
                        'action_id': 'assert_1',
                        'operation': 'assert_text',
                        'target': 'runtime notice',
                        'value': 'Visible runtime fact',
                        'required': True,
                    }],
                })
                self.route_contract = RouteContract.from_result({'page_keys': ['runtime_state']})
                self.binding_contract = BindingContract.from_result({
                    'bindings': [{
                        'action_id': 'assert_1',
                        'operation': 'assert_text',
                        'target_name': 'runtime notice',
                        'value': 'Visible runtime fact',
                        'binding_mode': 'runtime_text',
                        'locator_hint': {'type': 'text', 'value': 'Visible runtime fact'},
                        'confidence': 0.95,
                    }],
                })
                self.compact_map = self.element_map
                self.planning_map = self.element_map
                self.generated_attempts = 0

            def analyze_requirements(self):
                return self.requirement_contract.as_dict()

            def select_route(self, requirements):
                return self.route_contract.as_dict()

            def bind_elements(self, requirements, route, validation_gap=None, focus_action_ids=None):
                return self.binding_contract.as_dict()

            def generate_plan(self, requirements, route, bindings, validation_gap=None):
                self.generated_attempts += 1
                value = 'Missing runtime fact' if self.generated_attempts == 1 else 'Visible runtime fact'
                return {
                    'intent': {'goal': 'assert runtime fact'},
                    'test_plan': {'steps': [{
                        'step_sort': 0,
                        'action_id': 'assert_1',
                        'operation': 'assert_text',
                        'target_name': 'runtime notice',
                        'value': value,
                        'binding_mode': 'runtime_text',
                        'locator_hint': {'type': 'text', 'value': value},
                        'confidence': 0.95,
                    }]},
                    'generated_case': {'name': 'runtime', 'steps': [{
                        'step_sort': 0,
                        'action_id': 'assert_1',
                        'operation': 'assert_text',
                        'target_name': 'runtime notice',
                        'value': value,
                        'binding_mode': 'runtime_text',
                        'locator_hint': {'type': 'text', 'value': value},
                        'confidence': 0.95,
                    }]},
                }, 'stub_input'

        result = PlannerStub().run()

        self.assertEqual(result['plan_validation']['status'], 'success')
        self.assertEqual(result['generated_case']['steps'][0]['value'], 'Visible runtime fact')

    def test_runtime_image_ocr_input_is_preserved_without_fake_static_value(self):
        requirements = {'flow': [{
            'action_id': 'captcha',
            'operation': 'choose',
            'target': '验证码',
            'required': True,
        }]}
        bindings = {'bindings': [{
            'action_id': 'captcha',
            'operation': 'fill',
            'binding_mode': 'runtime_input',
            'runtime_resolver': 'image_ocr',
            'page_key': 'login',
            'element_key': 'captcha_input',
        }]}
        parsed = {'test_plan': {'steps': [{
            'action_id': 'captcha',
            'operation': 'fill',
            'binding_mode': 'runtime_input',
            'runtime_resolver': 'image_ocr',
            'page_key': 'login',
            'element_key': 'captcha_input',
            'target_name': '填写验证码',
            'value': '',
        }]}}
        element_map = {'pages': [{
            'page_key': 'login',
            'elements': [{
                'element_key': 'captcha_input',
                'name': '填写验证码',
                'actions': ['fill'],
                'recommended_locator': {'type': 'placeholder', 'value': '填写验证码'},
            }],
        }]}

        normalized = _normalize_llm_plan(self.TaskStub(), parsed, element_map)
        steps = normalized['test_plan']['steps']
        lifecycle = _build_data_lifecycle(self.TaskStub(), steps, normalized['test_plan'])
        spec = _build_typescript_spec_from_steps(
            self.TaskStub(), steps, element_map, lifecycle,
        )

        self.assertEqual(_staged_binding_contract_issues(requirements, bindings), [])
        self.assertEqual(
            _staged_plan_contract_issues(requirements, bindings, normalized),
            [],
        )
        self.assertEqual(steps[0]['value'], '')
        self.assertEqual(lifecycle['test_data'], {})
        self.assertEqual(lifecycle['runtime_inputs'][0]['resolver'], 'image_ocr')
        self.assertIn('app.fillRuntimeInput', spec)
        self.assertIn('"resolver": "image_ocr"', spec)

    def test_staged_contract_accepts_choose_resolved_to_observed_control_operation(self):
        requirements = {'flow': [{
            'action_id': 'choose_mode',
            'operation': 'choose',
            'required': True,
        }]}
        bindings = {'bindings': [{
            'action_id': 'choose_mode',
            'operation': 'check',
            'page_key': 'dialog',
            'element_key': 'radio_now',
        }]}
        parsed = {'test_plan': {'steps': [{
            'action_id': 'choose_mode',
            'operation': 'check',
            'page_key': 'dialog',
            'element_key': 'radio_now',
        }]}}

        self.assertEqual(_staged_plan_contract_issues(requirements, bindings, parsed), [])

    def test_binding_contract_requires_runtime_binding_for_post_submit_exact_text(self):
        requirements = {'flow': [{
            'action_id': 'assert_created',
            'operation': 'assert_visible',
            'value': '新建后的唯一名称',
            'required': True,
        }]}

        missing = _staged_binding_contract_issues(requirements, {'bindings': []})
        resolved = _staged_binding_contract_issues(requirements, {'bindings': [{
            'action_id': 'assert_created',
            'operation': 'assert_visible',
            'binding_mode': 'runtime_text',
            'value': '新建后的唯一名称',
            'locator_hint': {'type': 'text', 'value': '新建后的唯一名称'},
        }]})

        self.assertEqual(missing[0]['reason'], 'staged_required_binding_missing')
        self.assertEqual(resolved, [])

    def test_typescript_step_uses_all_observed_locator_candidates(self):
        element_map = {'pages': [{
            'page_key': 'dialog',
            'elements': [{
                'element_key': 'environment',
                'name': '测试环境',
                'role': 'combobox',
                'actions': ['select_option'],
                'recommended_locator': {
                    'type': 'role', 'value': 'combobox', 'name': '测试环境', 'confidence': 0.88,
                },
                'locator_candidates': [{
                    'type': 'label', 'value': '测试环境', 'confidence': 0.62,
                }],
            }],
        }]}
        parsed = {'test_plan': {'steps': [{
            'action_id': 'choose_environment',
            'operation': 'select_option',
            'page_key': 'dialog',
            'element_key': 'environment',
            'locator_hint': {'type': 'role', 'value': 'combobox', 'name': '测试环境'},
            'value': '第一个选项',
        }]}}

        normalized = _normalize_llm_plan(self.TaskStub(), parsed, element_map)
        steps = normalized['test_plan']['steps']
        spec = _build_typescript_spec_from_steps(self.TaskStub(), steps, element_map, {})

        self.assertEqual(
            [candidate['type'] for candidate in steps[0]['locator_candidates']],
            ['role', 'label'],
        )
        self.assertIn('await app.selectOption([page => page.getByRole', spec)
        self.assertIn('{ name: "测试环境", exact: true }', spec)
        self.assertIn('.el-form-item', spec)

    def test_repair_context_uses_failure_line_window(self):
        spec = '\n'.join(f'line {index}' for index in range(1, 301))
        context = _extract_repair_spec_context(spec, {'failure_classification': {'line': 150}})

        self.assertEqual(context['mode'], 'line_window')
        self.assertIn('150: line 150', context['text'])
        self.assertFalse(any(line == '1: line 1' for line in context['text'].splitlines()))

    def test_llm_patch_operations_apply_to_full_spec(self):
        patched, applied = _apply_llm_patch_operations(
            'await app.click(page => page.getByText("旧按钮"));',
            [{
                'type': 'replace_text',
                'old': 'page.getByText("旧按钮")',
                'new': 'page.getByRole("button", { name: "新按钮" })',
            }],
        )

        self.assertIn('getByRole("button"', patched)
        self.assertEqual(applied[0]['status'], 'applied')

    def test_llm_normalization_does_not_inject_min_required_fill_steps(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'page_4',
                    'elements': [
                        {
                            'element_key': 'div_98',
                            'name': '创建任务',
                            'text': '创建任务 任务名称 保存任务',
                            'tag': 'div',
                            'role': 'dialog',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'role', 'value': 'dialog', 'name': '创建任务'},
                        },
                        {
                            'element_key': 'input_101',
                            'name': '任务名称',
                            'label': '任务名称',
                            'placeholder': '请输入任务名称',
                            'tag': 'input',
                            'role': 'textbox',
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': '任务名称'},
                        },
                    ],
                },
            ],
        }
        parsed = {
            'generated_case': {
                'steps': [
                    {
                        'step_sort': 0,
                        'operation': 'assert_visible',
                        'page_key': 'page_4',
                        'element_key': 'div_98',
                        'target_name': '创建任务',
                        'description': '验证创建任务弹窗可见',
                        'locator_hint': {'type': 'role', 'value': 'dialog', 'name': '创建任务'},
                    },
                ],
            },
            'test_plan': {},
        }

        normalized = _normalize_llm_plan(self.TaskStub(), parsed, element_map)
        steps = normalized['generated_case']['steps']

        self.assertFalse(any(step.get('operation') == 'fill' for step in steps))

    def test_plan_validation_fails_when_steps_do_not_cover_user_action_intent(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'page_4',
                    'elements': [
                        {
                            'element_key': 'div_98',
                            'name': '创建任务',
                            'text': '创建任务 任务名称 保存任务',
                            'tag': 'div',
                            'role': 'dialog',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'role', 'value': 'dialog', 'name': '创建任务'},
                        },
                    ],
                },
            ],
        }
        steps = [
            {
                'step_sort': 0,
                'operation': 'assert_visible',
                'page_key': 'page_4',
                'element_key': 'div_98',
                'target_name': '创建任务',
                'description': '验证创建任务弹窗可见',
                'locator_hint': {'type': 'role', 'value': 'dialog', 'name': '创建任务'},
            },
        ]

        safety_policy = {
            **self.TaskStub.safety_policy,
            'requirement_contract': {
                'flow': [
                    {'action_id': 'fill_task_name', 'operation': 'fill', 'target': '任务名称', 'required': True},
                    {'action_id': 'submit_task', 'operation': 'click', 'target': '保存任务', 'required': True},
                ],
            },
        }
        validation = _validate_generated_steps(steps, element_map, safety_policy, self.TaskStub())

        self.assertEqual(validation['status'], 'failed')
        self.assertEqual(validation['semantic_issue_count'], 2)
        self.assertEqual(
            {issue['reason'] for issue in validation['semantic_issues']},
            {'missing_required_contract_action'},
        )

    def test_plan_validation_requires_explicit_authentication_flow(self):
        task = self.TaskStub()
        task.source_requirement = '用户输入用户名 "tester" 和密码 "secret"，点击登录后进入业务页面'
        element_map = {
            'pages': [
                {
                    'page_key': 'business_page',
                    'url': 'https://example.test/home',
                    'elements': [{
                        'element_key': 'logout', 'name': '退出登录', 'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': '退出登录'},
                    }],
                },
                {
                    'page_key': 'login_page',
                    'url': 'https://example.test/login',
                    'elements': [
                    {
                        'element_key': 'username', 'name': '用户名', 'actions': ['fill'],
                        'recommended_locator': {'type': 'placeholder', 'value': '请输入用户名'},
                    },
                    {
                        'element_key': 'password', 'name': '密码', 'actions': ['fill'],
                        'recommended_locator': {'type': 'placeholder', 'value': '请输入密码'},
                    },
                    {
                        'element_key': 'login', 'name': '登录', 'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': '登录'},
                    },
                ],
                },
            ],
        }
        steps = [{
            'operation': 'click', 'page_key': 'login_page', 'element_key': 'login',
            'target_name': '登录', 'locator_hint': {'type': 'role', 'value': 'button', 'name': '登录'},
        }]

        safety_policy = {
            **task.safety_policy,
            'requirement_contract': {
                'flow': [
                    {'action_id': 'fill_username', 'operation': 'fill', 'target': '用户名', 'value': 'tester', 'phase': 'authentication', 'required': True},
                    {'action_id': 'fill_password', 'operation': 'fill', 'target': '密码', 'value': 'secret', 'phase': 'authentication', 'required': True},
                    {'action_id': 'submit_login', 'operation': 'click', 'target': '登录', 'phase': 'authentication', 'required': True},
                ],
            },
        }
        validation = _validate_generated_steps(steps, element_map, safety_policy, task)

        self.assertEqual(validation['status'], 'failed')
        self.assertIn('missing_required_contract_action', {item['reason'] for item in validation['semantic_issues']})

        compiled = _compile_missing_explicit_actions(task, {
            'generated_case': {'steps': [
                {'operation': 'goto', 'value': 'https://example.test/login'},
                steps[0],
            ]},
            'test_plan': {},
        }, element_map)
        compiled_steps = compiled['generated_case']['steps']
        self.assertEqual([step['operation'] for step in compiled_steps[:4]], ['goto', 'fill', 'fill', 'click'])
        self.assertEqual([step.get('value') for step in compiled_steps[1:3]], ['tester', 'secret'])
        self.assertEqual(compiled_steps[3]['element_key'], 'login')

    def test_plan_validation_does_not_infer_password_login_for_sso_business_flow(self):
        task = self.TaskStub()
        task.source_requirement = '通过 SSO 完成认证后校验业务详情'
        task.safety_policy = {
            **self.TaskStub.safety_policy,
            'authentication_contract': {'mode': 'authentication_then_business', 'source': 'llm'},
            'requirement_contract': {
                'flow': [
                    {'action_id': 'sso_auth', 'operation': 'click', 'target': 'SSO', 'phase': 'authentication', 'required': True},
                    {'action_id': 'assert_business_detail', 'operation': 'assert_visible', 'target': '业务详情', 'phase': 'business', 'required': True},
                ],
            },
        }
        element_map = {
            'pages': [{
                'page_key': 'page_sso',
                'elements': [
                    {
                        'element_key': 'sso_button', 'name': 'SSO',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'SSO'},
                    },
                    {
                        'element_key': 'business_detail', 'name': '业务详情',
                        'actions': [],
                        'recommended_locator': {'type': 'text', 'value': '业务详情'},
                    },
                ],
            }],
        }
        steps = [
            {'action_id': 'sso_auth', 'operation': 'click', 'page_key': 'page_sso', 'element_key': 'sso_button', 'target_name': 'SSO'},
            {'action_id': 'assert_business_detail', 'operation': 'assert_visible', 'page_key': 'page_sso', 'element_key': 'business_detail', 'target_name': '业务详情'},
        ]

        validation = _validate_generated_steps(steps, element_map, task.safety_policy, task)

        self.assertEqual(validation['status'], 'success')
        self.assertNotIn('missing_authentication_flow', {item['reason'] for item in validation['semantic_issues']})

    def test_runtime_agent_converts_empty_completed_without_rendered_candidates_to_reobserve(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '查询后校验结果'},
                'current_observation': {'url': 'https://example.test/list'},
                'current_candidates': [],
                'runtime_history': [],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '页面仍在渲染',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['operation'], 'wait_and_reobserve')
        self.assertTrue(plan['runtime_plan']['next_step']['internal_runtime_primitive'])
        self.assertEqual(plan['runtime_context']['contract_artifacts'], False)
        self.assertNotIn('generated_case', plan)
        self.assertNotIn('test_plan', plan)
        self.assertNotIn('plan_validation', plan)

    def test_runtime_agent_converts_empty_completed_with_pending_candidate_to_step(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {
                    'source_requirement': 'When 点击"SSO登录"按钮\nThen 成功进入网站',
                    'flow': [{'action_id': 'action_1', 'operation': 'click', 'target': 'SSO登录'}],
                },
                'requirement_document': {
                    'flow': [{'action_id': 'action_1', 'operation': 'click', 'target': 'SSO登录'}],
                },
                'current_observation': {
                    'page_key': 'runtime_page_1',
                    'url': 'https://example.test/login',
                    'title': '登录',
                },
                'current_candidates': [
                    {
                        'element_key': 'sso_button',
                        'name': 'SSO登录',
                        'actions': ['click'],
                        'page_key': 'runtime_page_1',
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'SSO登录'},
                    },
                ],
                'runtime_history': [
                    {'operation': 'wait_and_reobserve', 'status': 'success'},
                    {'operation': 'wait_and_reobserve', 'status': 'success'},
                ],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '当前为登录页，需求首个待执行动作是点击“SSO登录”按钮。',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['operation'], 'click')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'action_1')
        self.assertEqual(plan['runtime_plan']['next_step']['element_key'], 'sso_button')
        self.assertEqual(
            plan['runtime_plan']['next_step']['runtime_resolver'],
            'pending_action_candidate_match',
        )
        self.assertFalse(plan['runtime_plan']['next_step'].get('internal_runtime_primitive'))
        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)
        self.assertFalse(plan['runtime_context']['has_successful_business_history'])

    def test_runtime_agent_skips_obsolete_authentication_actions_after_login(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {
                    'source_requirement': '登录后查询测试任务',
                    'flow': [
                        {'action_id': 'auth_username', 'operation': 'fill', 'target': '用户名', 'phase': 'authentication'},
                        {'action_id': 'auth_password', 'operation': 'fill', 'target': '密码', 'phase': 'authentication'},
                        {'action_id': 'auth_login', 'operation': 'click', 'target': '登录', 'phase': 'authentication'},
                        {'action_id': 'business_query', 'operation': 'click', 'target': '测试任务', 'phase': 'business'},
                    ],
                },
                'requirement_document': {
                    'flow': [
                        {'action_id': 'auth_username', 'operation': 'fill', 'target': '用户名', 'phase': 'authentication', 'required': True},
                        {'action_id': 'auth_password', 'operation': 'fill', 'target': '密码', 'phase': 'authentication', 'required': True},
                        {'action_id': 'auth_login', 'operation': 'click', 'target': '登录', 'phase': 'authentication', 'required': True},
                        {'action_id': 'business_query', 'operation': 'click', 'target': '测试任务', 'phase': 'business', 'required': True},
                    ],
                },
                'authentication_contract': {'mode': 'authentication_then_business'},
                'current_observation': {
                    'page_key': 'runtime_page_1',
                    'url': 'https://example.test/report',
                    'title': '报表首页',
                    'elements': [
                        {'element_key': 'user_button', 'name': 'superadmin', 'actions': ['click']},
                    ],
                },
                'current_candidates': [
                    {
                        'element_key': 'business_query',
                        'name': '测试任务',
                        'actions': ['click'],
                        'page_key': 'runtime_page_1',
                        'recommended_locator': {'type': 'text', 'value': '测试任务'},
                    },
                ],
                'runtime_history': [
                    {'operation': 'wait_and_reobserve', 'status': 'success'},
                    {'operation': 'wait_and_reobserve', 'status': 'success'},
                ],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '登录态已稳定，继续业务查询',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)
        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'business_query')
        self.assertEqual(plan['runtime_plan']['next_step']['element_key'], 'business_query')
        self.assertEqual(plan['runtime_plan']['next_step']['operation'], 'click')
        self.assertEqual(plan['runtime_plan']['next_step']['runtime_resolver'], 'pending_action_candidate_match')

    def test_runtime_agent_does_not_match_pending_action_to_unrelated_candidate(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {
                    'source_requirement': 'When 点击"SSO登录"按钮',
                    'flow': [{'action_id': 'action_1', 'operation': 'click', 'target': 'SSO登录'}],
                },
                'requirement_document': {
                    'flow': [{'action_id': 'action_1', 'operation': 'click', 'target': 'SSO登录'}],
                },
                'current_observation': {'page_key': 'runtime_page_1', 'url': 'https://example.test/login'},
                'current_candidates': [
                    {
                        'element_key': 'forgot_password',
                        'name': '忘记密码',
                        'actions': ['click'],
                        'page_key': 'runtime_page_1',
                        'recommended_locator': {'type': 'text', 'value': '忘记密码'},
                    },
                ],
                'runtime_history': [],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '错误标记完成',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['operation'], 'wait_and_reobserve')
        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)

    def test_runtime_agent_falls_back_to_observed_elements_when_rendered_candidates_are_empty(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {
                    'source_requirement': '进入测试任务菜单',
                    'flow': [{'action_id': 'business_menu', 'operation': 'click', 'target': '测试任务'}],
                },
                'requirement_document': {
                    'flow': [{'action_id': 'business_menu', 'operation': 'click', 'target': '测试任务'}],
                },
                'current_observation': {
                    'page_key': 'runtime_page_1',
                    'url': 'https://example.test/home',
                    'title': '首页',
                    'elements': [
                        {
                            'element_key': 'menu_business',
                            'name': '测试任务',
                            'text': '测试任务',
                            'actions': ['click'],
                            'page_key': 'runtime_page_1',
                            'recommended_locator': {'type': 'role', 'value': 'menuitem', 'name': '测试任务'},
                        },
                    ],
                },
                'current_candidates': [],
                'runtime_history': [],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '页面已渲染完成，继续业务菜单点击。',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['operation'], 'click')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'business_menu')
        self.assertEqual(plan['runtime_plan']['next_step']['element_key'], 'menu_business')
        self.assertEqual(plan['runtime_plan']['next_step']['runtime_resolver'], 'pending_action_candidate_match')

    def test_runtime_agent_preserves_primary_locator_in_locator_candidates(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {
                    'source_requirement': '点击表格操作列的名片彩印',
                    'flow': [{'action_id': 'open_card_print', 'operation': 'click', 'target': '名片彩印'}],
                },
                'requirement_document': {
                    'flow': [{'action_id': 'open_card_print', 'operation': 'click', 'target': '名片彩印'}],
                },
                'current_observation': {
                    'page_key': 'runtime_page_10',
                    'url': 'https://example.test/menu.html',
                    'title': '子企业管理',
                    'elements': [{
                        'element_key': 'row_action_card_print',
                        'name': '名片彩印',
                        'text': '名片彩印',
                        'role': 'button',
                        'tag': 'a',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': '名片彩印', 'confidence': 0.78},
                        'locator_candidates': [{'type': 'text', 'value': '名片彩印', 'confidence': 0.70}],
                    }],
                },
                'current_candidates': [],
                'runtime_history': [],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '继续点击当前页面候选。',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        next_step = plan['runtime_plan']['next_step']
        self.assertEqual(next_step['action_id'], 'open_card_print')
        self.assertEqual(next_step['locator_hint'], {'type': 'role', 'value': 'button', 'name': '名片彩印', 'confidence': 0.78})
        self.assertEqual(
            next_step['locator_candidates'],
            [
                {'type': 'role', 'value': 'button', 'name': '名片彩印', 'confidence': 0.78},
                {'type': 'text', 'value': '名片彩印', 'confidence': 0.70},
            ],
        )

    def test_runtime_agent_extracts_quoted_value_for_deterministic_fill_step(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {
                    'source_requirement': '输入子企业编号',
                    'flow': [{'action_id': 'fill_child_enterprise_id', 'operation': '', 'target': '我在搜索框中输入子企业编号 "30002620"'}],
                },
                'requirement_document': {
                    'flow': [{'action_id': 'fill_child_enterprise_id', 'operation': '', 'target': '我在搜索框中输入子企业编号 "30002620"'}],
                },
                'current_observation': {
                    'page_key': 'runtime_page_7',
                    'url': 'https://example.test/menu.html',
                    'title': '子企业管理',
                    'elements': [{
                        'element_key': 'input_child_enterprise_id',
                        'name': '子企业编号',
                        'placeholder': '请输入子企业编号',
                        'role': 'textbox',
                        'tag': 'input',
                        'actions': ['fill'],
                        'recommended_locator': {'type': 'placeholder', 'value': '请输入子企业编号', 'confidence': 0.84},
                    }],
                },
                'current_candidates': [],
                'runtime_history': [],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '继续输入当前页面候选。',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        next_step = plan['runtime_plan']['next_step']
        self.assertEqual(next_step['action_id'], 'fill_child_enterprise_id')
        self.assertEqual(next_step['operation'], 'fill')
        self.assertEqual(next_step['value'], '30002620')
        self.assertEqual(next_step['element_key'], 'input_child_enterprise_id')

    def test_runtime_agent_uses_completed_action_ids_beyond_history_window(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [
            {'action_id': f'gherkin_step_{index}', 'operation': '', 'target': f'已完成动作{index}', 'required': True}
            for index in range(1, 12)
        ] + [
            {'action_id': 'gherkin_step_12', 'operation': '', 'target': '我点击 "添加" 按钮', 'required': True}
        ]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '长流程创建彩印内容', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_33',
                    'url': 'https://example.test/menu.html',
                    'title': '彩印内容',
                    'elements': [{
                        'element_key': 'add_button',
                        'name': '添加',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'text', 'value': '添加'},
                    }],
                },
                'current_candidates': [{
                    'element_key': 'add_button',
                    'name': '添加',
                    'actions': ['click'],
                    'page_key': 'runtime_page_33',
                    'recommended_locator': {'type': 'text', 'value': '添加'},
                }],
                'completed_action_ids': [f'gherkin_step_{index}' for index in range(1, 12)],
                'runtime_history': [
                    {'action_id': f'runtime_action_{index}', 'operation': 'click', 'status': 'failed'}
                    for index in range(20, 32)
                ],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '错误标记完成',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'gherkin_step_12')
        self.assertEqual(plan['runtime_plan']['next_step']['target_name'], '添加')
        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)

    def test_runtime_agent_treats_current_page_given_state_as_completed(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [
            {
                'action_id': 'gherkin_step_10',
                'operation': 'assert_visible',
                'target': '我应该进入彩印内容管理页面',
                'expected': '我应该进入彩印内容管理页面',
                'phase': 'Then',
                'required': True,
            },
            {
                'action_id': 'gherkin_step_11',
                'operation': '',
                'target': '我处于彩印内容管理页面',
                'phase': 'Given',
                'required': True,
            },
            {
                'action_id': 'gherkin_step_12',
                'operation': '',
                'target': '我点击 "添加" 按钮',
                'phase': 'When',
                'required': True,
            },
        ]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '彩印内容管理页面添加', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_21',
                    'url': 'https://example.test/queryContentInfoList.html',
                    'title': '彩印内容',
                    'elements': [
                        {'element_key': 'tab_content', 'name': '彩印内容', 'actions': ['click'],
                         'recommended_locator': {'type': 'text', 'value': '彩印内容'}},
                        {'element_key': 'add_button', 'name': '添加', 'actions': ['click'],
                         'recommended_locator': {'type': 'text', 'value': '添加'}},
                    ],
                },
                'current_candidates': [{
                    'element_key': 'add_button',
                    'name': '添加',
                    'actions': ['click'],
                    'page_key': 'runtime_page_21',
                    'recommended_locator': {'type': 'text', 'value': '添加'},
                }],
                'completed_action_ids': ['gherkin_step_10'],
                'runtime_history': [
                    {
                        'action_id': 'gherkin_step_10',
                        'operation': 'assert_visible',
                        'status': 'success',
                        'target_name': '彩印内容管理页面',
                        'description': '断言当前已进入彩印内容管理页面',
                    },
                ],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '错误标记完成',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'gherkin_step_12')
        self.assertEqual(plan['runtime_plan']['next_step']['target_name'], '添加')
        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)

    def test_runtime_agent_treats_form_page_state_assertion_as_completed(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [
            {
                'action_id': 'gherkin_step_13',
                'operation': 'assert_visible',
                'target': '系统应弹出/跳转至彩印内容表单页面',
                'expected': '系统应弹出/跳转至彩印内容表单页面',
                'phase': 'Then',
                'required': True,
            },
            {
                'action_id': 'gherkin_step_14',
                'operation': '',
                'target': '我在 "投递方式" 字段中选择 "主叫彩印"',
                'phase': 'When',
                'required': True,
            },
        ]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '进入彩印内容表单并选择投递方式', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_16',
                    'url': 'https://example.test/menu.html',
                    'title': '新增内容',
                    'elements': [
                        {'element_key': 'breadcrumb', 'text': '二级企业管理 > 内容管理 > 新增内容'},
                        {
                            'element_key': 'select_delivery',
                            'name': '主叫彩印 被叫彩印',
                            'form_label': '投递方式',
                            'role': 'combobox',
                            'tag': 'select',
                            'actions': ['select_option'],
                            'selected_label': '主叫彩印',
                            'recommended_locator': {'type': 'label', 'value': '投递方式'},
                        },
                        {'element_key': 'textarea_content', 'label': '彩印内容', 'actions': ['fill']},
                    ],
                },
                'current_candidates': [],
                'completed_action_ids': ['gherkin_step_12'],
                'runtime_history': [
                    {'action_id': 'gherkin_step_12', 'operation': 'click', 'target_name': '添加', 'status': 'success'},
                ],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '错误标记完成',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'gherkin_step_14')
        self.assertNotEqual(plan['runtime_plan']['next_step']['action_id'], 'gherkin_step_13')

    def test_runtime_agent_skips_obsolete_state_assertions_before_later_completed_action(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [
            {
                'action_id': 'gherkin_step_8',
                'operation': 'assert_visible',
                'target': '我应该进入分组管理页面',
                'expected': '我应该进入分组管理页面',
                'phase': 'Then',
                'required': True,
            },
            {
                'action_id': 'gherkin_step_12',
                'operation': '',
                'target': '我点击 "添加" 按钮',
                'phase': 'When',
                'required': True,
            },
            {
                'action_id': 'gherkin_step_13',
                'operation': 'assert_visible',
                'target': '系统应弹出/跳转至彩印内容表单页面',
                'expected': '系统应弹出/跳转至彩印内容表单页面',
                'phase': 'Then',
                'required': True,
            },
            {
                'action_id': 'gherkin_step_14',
                'operation': '',
                'target': '我在 "投递方式" 字段中选择 "主叫彩印"',
                'phase': 'When',
                'required': True,
            },
        ]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '进入表单后继续填写彩印内容', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_13',
                    'url': 'https://example.test/menu.html',
                    'title': '新增内容',
                    'elements': [
                        {'element_key': 'breadcrumb', 'text': '二级企业管理 > 内容管理 > 新增内容'},
                        {
                            'element_key': 'select_delivery',
                            'form_label': '投递方式',
                            'name': '主叫彩印 被叫彩印',
                            'role': 'combobox',
                            'tag': 'select',
                            'actions': ['select_option'],
                            'recommended_locator': {'type': 'label', 'value': '投递方式'},
                        },
                        {'element_key': 'textarea_content', 'label': '彩印内容', 'actions': ['fill']},
                    ],
                },
                'current_candidates': [],
                'completed_action_ids': ['gherkin_step_12'],
                'runtime_history': [
                    {'action_id': 'gherkin_step_12', 'operation': 'click', 'target_name': '添加', 'status': 'success'},
                ],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '错误标记完成',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'gherkin_step_14')
        self.assertNotEqual(plan['runtime_plan']['next_step']['action_id'], 'gherkin_step_8')

    def test_runtime_agent_treats_selected_option_value_as_completed(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [
            {
                'action_id': 'select_delivery',
                'operation': 'select_option',
                'target': '投递方式',
                'value': '主叫彩印',
                'phase': 'When',
                'required': True,
            },
            {
                'action_id': 'fill_content',
                'operation': 'fill',
                'target': '彩印内容',
                'value': '测试内容',
                'phase': 'And',
                'required': True,
            },
        ]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '新增彩印内容', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_28',
                    'url': 'https://example.test/menu.html',
                    'title': '新增内容',
                    'frames': [{
                        'frame_key': 'frame_1',
                        'url': 'https://example.test/content.html',
                        'elements': [
                            {
                                'element_key': 'frame_1::select_1',
                                'source_element_key': 'select_1',
                                'context_type': 'frame',
                                'frame_key': 'frame_1',
                                'frame_url': 'https://example.test/content.html',
                                'name': '主叫彩印 被叫彩印',
                                'label': '投递方式',
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
                                'locator_candidates': [{'type': 'css', 'value': 'select:nth-of-type(1)'}],
                            },
                            {
                                'element_key': 'frame_1::textarea_1',
                                'source_element_key': 'textarea_1',
                                'context_type': 'frame',
                                'frame_key': 'frame_1',
                                'frame_url': 'https://example.test/content.html',
                                'name': '彩印内容',
                                'label': '彩印内容',
                                'tag': 'textarea',
                                'actions': ['fill'],
                                'recommended_locator': {'type': 'label', 'value': '彩印内容'},
                            },
                        ],
                    }],
                },
                'current_candidates': [],
                'runtime_history': [{'action_id': 'open_form', 'operation': 'click', 'status': 'success'}],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {'status': 'completed', 'reason': '错误标记完成', 'next_step': {}},
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)
        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'fill_content')
        self.assertEqual(plan['runtime_plan']['next_step']['element_key'], 'frame_1::textarea_1')
        self.assertEqual(plan['runtime_plan']['next_step']['frame_key'], 'frame_1')

    def test_runtime_agent_maps_select_current_value_to_option_label(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [
            {
                'action_id': 'select_delivery',
                'operation': 'select_option',
                'target': '投递方式',
                'value': '主叫彩印',
                'phase': 'When',
                'required': True,
            },
            {
                'action_id': 'fill_signature',
                'operation': 'fill',
                'target': '签名',
                'value': '测试签名',
                'phase': 'And',
                'required': True,
            },
        ]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '新增彩印内容', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_28',
                    'url': 'https://example.test/menu.html',
                    'title': '新增内容',
                    'elements': [
                        {
                            'element_key': 'select_delivery',
                            'name': '主叫彩印 被叫彩印 主被叫彩印',
                            'form_label': '投递方式',
                            'tag': 'select',
                            'role': 'combobox',
                            'actions': ['select_option'],
                            'current_value': 'number:1',
                            'options': [
                                {'label': '主叫彩印', 'value': 'number:1'},
                                {'label': '被叫彩印', 'value': 'number:2'},
                            ],
                            'recommended_locator': {'type': 'name', 'value': 'deliveryWay'},
                        },
                        {
                            'element_key': 'signature',
                            'label': '签名',
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': '签名'},
                        },
                    ],
                },
                'current_candidates': [],
                'runtime_history': [{'action_id': 'open_form', 'operation': 'click', 'status': 'success'}],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {'status': 'completed', 'reason': '错误标记完成', 'next_step': {}},
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)
        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'fill_signature')
        self.assertEqual(plan['runtime_plan']['next_step']['target_name'], '签名')

    def test_runtime_agent_skips_select_when_selected_value_matches_even_if_label_mismatch(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [
            {
                'action_id': 'select_delivery',
                'operation': 'select_option',
                'target': '投递方式',
                'value': '主叫彩印',
                'phase': 'When',
                'required': True,
            },
            {
                'action_id': 'fill_signature',
                'operation': 'fill',
                'target': '签名',
                'value': '测试签名',
                'phase': 'And',
                'required': True,
            },
        ]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '新增彩印内容', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_28',
                    'url': 'https://example.test/menu.html',
                    'title': '新增内容',
                    'elements': [
                        {
                            'element_key': 'select_delivery',
                            'name': '主叫彩印 被叫彩印 主被叫彩印',
                            'tag': 'select',
                            'role': 'combobox',
                            'actions': ['select_option'],
                            'current_value': 'number:1',
                            'selected_label': '主叫彩印',
                            'selected_value': 'number:1',
                            'options': [
                                {'label': '主叫彩印', 'value': 'number:1', 'selected': True},
                                {'label': '被叫彩印', 'value': 'number:2', 'selected': False},
                            ],
                            'recommended_locator': {'type': 'name', 'value': 'deliveryWay'},
                        },
                        {
                            'element_key': 'signature',
                            'label': '签名',
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': '签名'},
                        },
                    ],
                },
                'current_candidates': [],
                'runtime_history': [{'action_id': 'open_form', 'operation': 'click', 'status': 'success'}],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {'status': 'completed', 'reason': '错误标记完成', 'next_step': {}},
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)
        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'fill_signature')
        self.assertEqual(plan['runtime_plan']['next_step']['target_name'], '签名')

    def test_runtime_agent_treats_checked_candidate_as_completed(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [
            {
                'action_id': 'check_group',
                'operation': 'check',
                'target': '我在搜索结果中勾选 "主叫彩印分组001"',
                'value': '主叫彩印分组001',
                'phase': 'And',
                'required': True,
            },
            {
                'action_id': 'fill_push_time',
                'operation': 'fill',
                'target': '推送时间',
                'value': '00:00:00',
                'phase': 'When',
                'required': True,
            },
        ]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '选择配置对象后填写推送时间', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_31',
                    'url': 'https://example.test/menu.html',
                    'title': '新增内容',
                    'elements': [
                        {
                            'element_key': 'group_result',
                            'name': '主叫彩印分组001(0)人',
                            'text': '主叫彩印分组001(0)人',
                            'tag': 'li',
                            'actions': ['click'],
                            'class_name': 'check-li-checked ng-binding ng-scope',
                            'state_text': 'check-li-checked ng-binding ng-scope',
                            'recommended_locator': {'type': 'text', 'value': '主叫彩印分组001(0)人'},
                        },
                        {
                            'element_key': 'push_time',
                            'label': '推送时间',
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': '推送时间'},
                        },
                    ],
                },
                'current_candidates': [],
                'runtime_history': [{'action_id': 'search_group', 'operation': 'click', 'status': 'success'}],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {'status': 'completed', 'reason': '错误标记完成', 'next_step': {}},
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_context']['pending_action_count'], 1)
        self.assertEqual(plan['runtime_plan']['status'], 'continue')
        self.assertEqual(plan['runtime_plan']['next_step']['action_id'], 'fill_push_time')
        self.assertEqual(plan['runtime_plan']['next_step']['target_name'], '推送时间')

    def test_runtime_agent_select_step_preserves_frame_context_and_options(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [{
            'action_id': 'select_delivery',
            'operation': 'select_option',
            'target': '投递方式',
            'value': '主叫彩印',
            'phase': 'When',
            'required': True,
        }]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '选择投递方式', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_28',
                    'url': 'https://example.test/menu.html',
                    'title': '新增内容',
                    'frames': [{
                        'frame_key': 'frame_1',
                        'url': 'https://example.test/content.html',
                        'elements': [{
                            'element_key': 'frame_1::select_1',
                            'source_element_key': 'select_1',
                            'context_type': 'frame',
                            'frame_key': 'frame_1',
                            'frame_url': 'https://example.test/content.html',
                            'name': '主叫彩印 被叫彩印',
                            'form_label': '投递方式',
                            'tag': 'select',
                            'role': 'combobox',
                            'actions': ['select_option'],
                            'selected_label': '被叫彩印',
                            'options': [
                                {'label': '主叫彩印', 'value': 'number:1', 'selected': False},
                                {'label': '被叫彩印', 'value': 'number:2', 'selected': True},
                            ],
                            'recommended_locator': {'type': 'role', 'value': 'combobox', 'name': '主叫彩印 被叫彩印'},
                            'locator_candidates': [{'type': 'css', 'value': 'select:nth-of-type(1)'}],
                        }],
                    }],
                },
                'current_candidates': [],
                'runtime_history': [{'action_id': 'open_form', 'operation': 'click', 'status': 'success'}],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {'status': 'completed', 'reason': '错误标记完成', 'next_step': {}},
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        step = plan['runtime_plan']['next_step']
        self.assertEqual(step['operation'], 'select_option')
        self.assertEqual(step['action_id'], 'select_delivery')
        self.assertEqual(step['target_name'], '投递方式')
        self.assertEqual(step['locator_hint'], {'type': 'label', 'value': '投递方式', 'confidence': 0.9})
        self.assertEqual(step['context_type'], 'frame')
        self.assertEqual(step['frame_key'], 'frame_1')
        self.assertEqual(step['options'][0]['value'], 'number:1')

    def test_runtime_agent_skips_select_when_operation_is_implicit_but_current_value_matches(self):
        active_config = MagicMock()
        active_config.id = 1
        active_config.config_name = 'runtime-test'
        active_config.name = 'runtime-model'
        active_config.request_timeout = 30
        active_config.max_retries = 1

        flow = [
            {
                'action_id': 'select_delivery',
                'operation': '',
                'target': '我在 "投递方式" 字段中选择 "主叫彩印"',
                'phase': 'When',
                'required': True,
            },
        ]

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, \
                patch('ui_automation.ai_planning._runtime_step_prompt_context') as context_mock, \
                patch('ui_automation.ai_planning._invoke_openai_compatible_chat') as invoke_mock:
            filter_mock.return_value.first.return_value = active_config
            context_mock.return_value = {
                'runtime_goal': {'source_requirement': '进入彩印内容表单并选择投递方式', 'flow': flow},
                'requirement_document': {'flow': flow},
                'current_observation': {
                    'page_key': 'runtime_page_28',
                    'url': 'https://example.test/menu.html',
                    'title': '新增内容',
                    'elements': [
                        {'element_key': 'breadcrumb', 'text': '二级企业管理 > 内容管理 > 新增内容'},
                        {
                            'element_key': 'select_delivery',
                            'name': '主叫彩印 被叫彩印 主被叫彩印',
                            'form_label': '投递方式',
                            'tag': 'select',
                            'role': 'combobox',
                            'actions': ['select_option'],
                            'current_value': 'number:1',
                            'selected_label': '主叫彩印',
                            'selected_value': 'number:1',
                            'options': [
                                {'label': '主叫彩印', 'value': 'number:1', 'selected': True},
                                {'label': '被叫彩印', 'value': 'number:2', 'selected': False},
                            ],
                            'recommended_locator': {'type': 'label', 'value': '投递方式'},
                        },
                    ],
                },
                'current_candidates': [],
                'runtime_history': [
                    {'action_id': 'open_form', 'operation': 'click', 'status': 'success'},
                ],
            }
            invoke_mock.return_value = json.dumps({
                'runtime_plan': {
                    'status': 'completed',
                    'reason': '错误标记完成',
                    'next_step': {},
                },
            })

            plan = _build_runtime_agent_plan(self.TaskStub(), {'execution_mode': 'runtime_agent'})

        self.assertEqual(plan['runtime_context']['pending_action_count'], 0)
        self.assertEqual(plan['runtime_plan']['status'], 'completed')
        self.assertEqual(plan['runtime_plan']['next_step'], {})

    def test_llm_normalization_uses_observed_checkable_semantics(self):
        element_map = {
            'pages': [{
                'page_key': 'dialog_page',
                'elements': [{
                    'element_key': 'radio_1',
                    'name': '不发送邮件',
                    'label': '不发送邮件',
                    'tag': 'input',
                    'type': 'radio',
                    'role': 'radio',
                    'actions': ['click', 'check'],
                    'recommended_locator': {'type': 'label', 'value': '不发送邮件'},
                }],
            }],
        }
        task = self.TaskStub()
        parsed = {'generated_case': {'steps': [{
            'operation': 'click',
            'page_key': 'dialog_page',
            'element_key': 'radio_1',
            'target_name': '不发送邮件',
            'locator_hint': {'type': 'label', 'value': '不发送邮件'},
        }]}}

        normalized = _normalize_llm_plan(task, parsed, element_map)

        self.assertEqual(normalized['generated_case']['steps'][0]['operation'], 'check')
        self.assertEqual(normalized['test_plan']['steps'][0]['operation'], 'check')

    def test_explicit_action_compilation_merges_existing_element_step(self):
        element_map = {
            'pages': [{
                'page_key': 'dialog_page',
                'elements': [{
                    'element_key': 'radio_1',
                    'name': '报告方式',
                    'label': '不发送邮件',
                    'tag': 'input',
                    'type': 'radio',
                    'role': 'radio',
                    'actions': ['click', 'check'],
                    'recommended_locator': {'type': 'label', 'value': '不发送邮件'},
                }],
            }],
        }
        task = self.TaskStub()
        task.source_requirement = '填写报告方式\n| 报告方式 | 不发送邮件 |'
        parsed = {
            'generated_case': {'steps': [{
                'operation': 'click',
                'page_key': 'dialog_page',
                'element_key': 'radio_1',
                'target_name': '报告方式',
                'locator_hint': {'type': 'label', 'value': '不发送邮件'},
            }]},
            'test_plan': {},
        }

        compiled = _compile_missing_explicit_actions(task, parsed, element_map)
        steps = compiled['generated_case']['steps']

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['operation'], 'check')
        self.assertEqual(compiled['explicit_action_compilation']['compiled_count'], 1)

    def test_async_llm_plan_validation_failure_is_not_applied(self):
        task = UiAiGenerationTask(
            generated_case={'steps': [{'operation': 'goto'}]},
            test_plan={},
        )
        plan = {
            'generated_case': {
                'steps': [
                    {'operation': 'goto'},
                    {'operation': 'assert_visible', 'element_key': 'dialog_1'},
                ],
            },
            'plan_validation': {
                'status': 'failed',
                'semantic_issues': [{
                    'reason': 'missing_data_entry_operation',
                    'message': '用户需求包含填写/选择/勾选类动作，但 LLM 步骤没有生成数据录入操作',
                }],
            },
        }

        apply_plan, reason = _should_apply_async_llm_plan(task, plan)

        self.assertFalse(apply_plan)
        self.assertIn('拒绝写回', reason)

    def test_llm_generation_plan_blocks_when_exploration_disallows_planning(self):
        task = UiAiGenerationTask(
            name='新增业务对象配置',
            target_module='业务对象管理',
            source_requirement='新增业务对象配置',
            generated_case={'steps': []},
            test_plan={},
        )
        element_map = {
            'pages': [{
                'page_key': 'page_1',
                'url': 'https://example.test/login',
                'elements': [{
                    'element_key': 'login',
                    'name': '登录',
                    'actions': ['click'],
                }],
            }],
            'exploration_coverage': {
                'status': 'deferred',
                'planning_allowed': False,
                'failure_category': 'permission',
                'blocked_by': 'authentication',
                'missing_stages': ['业务页面'],
                'reason': '前置认证不可用，当前元素地图仍停留在登录页，不能用于业务 LLM 规划',
            },
        }

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock:
            plan = build_llm_ui_generation_plan(task, {'element_map': element_map})

        filter_mock.assert_not_called()
        self.assertFalse(plan['llm_enabled'])
        self.assertEqual(plan['failure_category'], 'permission')
        self.assertIn('前置认证不可用', plan['llm_error'])
        validation = plan['plan_validation']
        self.assertEqual(validation['status'], 'failed')
        self.assertEqual(validation['semantic_issues'][0]['reason'], 'planning_precondition_failed')

    def test_llm_generation_plan_allows_authentication_subject_tasks_to_bypass_login_guard(self):
        task = UiAiGenerationTask(
            name='验证登录',
            target_module='登录验证',
            safety_policy={'authentication_contract': {'mode': 'test_subject', 'source': 'explicit'}},
            generated_case={'steps': []},
            test_plan={},
        )
        element_map = {
            'pages': [{
                'page_key': 'page_1',
                'url': 'https://example.test/login',
                'elements': [{
                    'element_key': 'login',
                    'name': '登录',
                    'actions': ['click'],
                }],
            }],
            'exploration_coverage': {
                'status': 'deferred',
                'planning_allowed': False,
                'failure_category': 'permission',
                'blocked_by': 'authentication',
                'missing_stages': ['business_goal'],
                'reason': '前置认证不可用，当前元素地图仍停留在登录页，不能用于业务 LLM 规划',
            },
        }

        self.assertEqual(_task_authentication_mode(task), 'test_subject')
        self.assertIsNone(_planning_precondition_failure(task, element_map))

    def test_task_authentication_contract_detects_authentication_then_business_flow(self):
        task = UiAiGenerationTask(
            name='查看账户信息',
            target_module='账号管理',
            gherkin=(
                'Feature: 账号管理 - 冻结账号查看\n'
                '  Scenario: 通过SSO登录并查看已冻结账号详情\n'
                '    When 点击"SSO登录"按钮\n'
                '    Then 成功登录管理平台\n'
                '    When 点击菜单"系统管理" -> "账号管理"\n'
                '    Then 进入账号管理页面\n'
                '    When 搜索账号名称为 "zhangwenwen@ebupt.com"\n'
                '    And 点击列表中第一个账号名称\n'
                '    Then 进入账号详情页面\n'
            ),
            generated_case={'steps': []},
            test_plan={},
        )

        fake_llm = MagicMock(return_value=json.dumps({
            'mode': 'authentication_then_business',
            'confidence': 0.97,
            'rationale': '认证是显式步骤，后面还有独立业务查询流程。',
        }, ensure_ascii=False))

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, patch(
            'ui_automation.ai_planning._invoke_openai_compatible_chat',
            fake_llm,
        ):
            filter_mock.return_value.first.return_value = object()
            contract = classify_task_authentication_contract(task, {})

        self.assertEqual(contract['mode'], 'authentication_then_business')
        self.assertEqual(_task_authentication_mode(
            UiAiGenerationTask(
                name=task.name,
                target_module=task.target_module,
                safety_policy={'authentication_contract': contract},
            )
        ), 'authentication_then_business')

    def test_task_authentication_contract_llm_failure_returns_unknown_degraded_contract(self):
        task = UiAiGenerationTask(
            name='查看账户信息',
            target_module='账号管理',
            gherkin=(
                'Feature: 账号管理 - 冻结账号查看\n'
                '  Scenario: 通过SSO登录并查看已冻结账号详情\n'
                '    When 点击"SSO登录"按钮\n'
                '    Then 成功登录管理平台\n'
            ),
            generated_case={'steps': []},
            test_plan={},
        )

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, patch(
            'ui_automation.ai_planning._invoke_openai_compatible_chat',
            side_effect=httpx.ReadTimeout(
                'The read operation timed out',
                request=httpx.Request('POST', 'https://example.test/chat/completions'),
            ),
        ):
            filter_mock.return_value.first.return_value = object()
            contract = classify_task_authentication_contract(task, {})

        self.assertEqual(contract['mode'], 'unknown')
        self.assertEqual(contract['status'], 'degraded')
        self.assertEqual(contract['source'], 'llm_error_default')
        self.assertEqual(contract['error_type'], 'ReadTimeout')

    def test_task_authentication_contract_ignores_legacy_llm_error_cache(self):
        task = UiAiGenerationTask(
            name='查看账户信息',
            target_module='账号管理',
            gherkin='Scenario: 查看账户信息',
            safety_policy={
                'authentication_contract': {
                    'mode': 'prerequisite',
                    'source': 'llm_error_default',
                    'rationale': 'LLM 分类失败，按业务探索前置认证处理: ReadTimeout',
                },
            },
            generated_case={'steps': []},
            test_plan={},
        )

        with patch('ui_automation.ai_planning.LLMConfig.objects.filter') as filter_mock, patch(
            'ui_automation.ai_planning._invoke_openai_compatible_chat',
            side_effect=httpx.ReadTimeout(
                'The read operation timed out',
                request=httpx.Request('POST', 'https://example.test/chat/completions'),
            ),
        ):
            filter_mock.return_value.first.return_value = object()
            contract = classify_task_authentication_contract(task, task.safety_policy)

        self.assertEqual(contract['mode'], 'unknown')
        self.assertEqual(contract['status'], 'degraded')
        self.assertEqual(contract['source'], 'llm_error_default')

    def test_semantic_replan_carries_complete_candidate_and_validation_gap(self):
        candidate = {
            'intent': {'goal': '创建业务对象'},
            'test_plan': {'steps': [{'operation': 'click', 'element_key': 'open_1'}]},
            'generated_case': {'steps': [{'operation': 'click', 'element_key': 'open_1'}]},
        }
        validation = {
            'status': 'failed',
            'semantic_issues': [{
                'reason': 'missing_explicit_click_action',
                'message': '用户明确要求的提交动作未覆盖',
            }],
            'unresolved_steps': [],
        }

        payload = _semantic_replan_payload({'verification_result': {}}, candidate, validation, 2)

        self.assertEqual(payload['semantic_validation_attempt'], 2)
        self.assertEqual(payload['previous_candidate_plan']['generated_case'], candidate['generated_case'])
        self.assertEqual(payload['previous_candidate_plan']['plan_validation'], validation)
        self.assertIn('提交动作未覆盖', payload['repair_instruction'])

    def test_budgeted_prompt_exposes_previous_candidate_to_replanner(self):
        messages = _build_budgeted_generation_prompt(
            self.TaskStub(),
            {
                'previous_candidate_plan': {
                    'generated_case': {'steps': [{'operation': 'click', 'element_key': 'save_1'}]},
                    'plan_validation': {'status': 'failed'},
                },
            },
            {'pages': [], 'state_transitions': []},
        )[0]

        self.assertIn('previous_candidate_plan', messages[-1].content)
        self.assertIn('save_1', messages[-1].content)

    def test_staged_planner_passes_only_structured_results_between_roles(self):
        element_map = {
            'pages': [{
                'page_key': 'login_page',
                'url': 'https://example.test/login',
                'elements': [{
                    'element_key': 'login_button',
                    'name': '登录',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'role', 'value': 'button', 'name': '登录'},
                }],
            }],
            'state_transitions': [{
                'from_page_key': 'login_page',
                'to_page_key': 'home_page',
                'element_key': 'login_button',
                'operation': 'click',
                'locator_hint': {'type': 'role', 'value': 'button', 'name': '登录'},
            }],
        }
        planner = _StagedUiGenerationPlanner(
            self.TaskStub(),
            {},
            element_map,
            object(),
            async_mode=True,
            request_timeout=60,
            max_retries=1,
            total_budget=240,
        )
        requirements = {'goal': '登录', 'actions': [{'action_id': 'a1', 'operation': 'click', 'target': '登录'}]}
        route = {'ordered_page_keys': ['login_page'], 'transition_keys': ['login_button']}
        bindings = {'bindings': [{'action_id': 'a1', 'page_key': 'login_page', 'element_key': 'login_button'}]}
        plan = {'test_plan': {'steps': [{'operation': 'click', 'page_key': 'login_page', 'element_key': 'login_button'}]}}

        with patch('ui_automation.ai_planning._invoke_planning_stage_json') as invoke:
            invoke.side_effect = [
                (requirements, {'stage': 'requirement_analysis'}),
                (route, {'stage': 'state_route_selection'}),
                (bindings, {'stage': 'element_binding'}),
                (plan, {'stage': 'executable_plan_generation'}),
            ]
            analyzed = planner.analyze_requirements()
            selected_route = planner.select_route(analyzed)
            validation_gap = {
                'issues': [{
                    'reason': 'staged_required_binding_missing',
                    'action_id': 'assert_created',
                }],
            }
            bound = planner.bind_elements(analyzed, planner.route_contract, validation_gap)
            generated, _ = planner.generate_plan(analyzed, planner.route_contract, planner.binding_contract)

        completed_requirements = {
            **requirements,
            'flow': [{'action_id': 'a1', 'operation': 'click', 'target': '登录'}],
        }
        requirement_input = invoke.call_args_list[0].args[3]
        route_input = invoke.call_args_list[1].args[3]
        binding_input = invoke.call_args_list[2].args[3]
        plan_input = invoke.call_args_list[3].args[3]
        self.assertNotIn('element_map', requirement_input)
        self.assertNotIn('pages', requirement_input)
        self.assertEqual(analyzed, completed_requirements)
        self.assertEqual(selected_route['ordered_page_keys'], ['login_page'])
        self.assertEqual(route_input['requirements'], completed_requirements)
        self.assertEqual(route_input['requirement_contract'], completed_requirements)
        self.assertNotIn('requirement', route_input.get('task', {}))
        self.assertEqual(route_input['state_transitions'][0]['transition_key'], 'login_button')
        self.assertEqual(binding_input['requirement_contract'], completed_requirements)
        self.assertEqual(binding_input['route'], planner.route_contract.as_dict())
        self.assertEqual(binding_input['validation_gap'], validation_gap)
        self.assertIn('candidates', binding_input)
        self.assertEqual(binding_input['route_transitions'][0]['element_key'], 'login_button')
        self.assertEqual(plan_input['requirements'], completed_requirements)
        self.assertEqual(plan_input['requirement_contract'], completed_requirements)
        self.assertEqual(plan_input['route'], planner.route_contract.as_dict())
        self.assertEqual(plan_input['bindings'], planner.binding_contract.as_dict())
        self.assertNotIn('element_map', plan_input)
        self.assertIsInstance(planner.requirement_contract, RequirementContract)
        self.assertEqual(planner.requirement_contract.action_ids, ('a1',))
        self.assertIsInstance(planner.route_contract, RouteContract)
        self.assertEqual(planner.route_contract.ordered_page_keys, ('login_page',))
        self.assertEqual(planner.binding_contract.bindings['a1']['element_key'], 'login_button')
        self.assertEqual(bound['bindings'][0]['element_key'], 'login_button')
        self.assertEqual(generated['test_plan']['steps'][0]['element_key'], 'login_button')
        self.assertEqual(generated, plan)

    def test_staged_planner_run_keeps_route_and_binding_contracts_through_final_compile(self):
        element_map = {
            'pages': [{
                'page_key': 'login_page',
                'url': 'https://example.test/login',
                'runtime_fresh': True,
                'elements': [{
                    'element_key': 'login_button',
                    'name': '登录',
                    'actions': ['click'],
                    'recommended_locator': {'type': 'role', 'value': 'button', 'name': '登录'},
                }],
            }, {
                'page_key': 'home_page',
                'url': 'https://example.test/home',
                'elements': [],
            }],
            'state_transitions': [{
                'from_page_key': 'login_page',
                'to_page_key': 'home_page',
                'element_key': 'login_button',
                'locator_hint': {'type': 'role', 'value': 'button', 'name': '登录'},
            }],
        }
        planner = _StagedUiGenerationPlanner(
            self.TaskStub(),
            {},
            element_map,
            MagicMock(
                id=1,
                config_name='test-config',
                name='test-model',
                request_timeout=60,
                max_retries=1,
                enable_streaming=False,
            ),
            async_mode=True,
            request_timeout=60,
            max_retries=1,
            total_budget=240,
        )
        requirements = {'flow': [{'action_id': 'a1', 'operation': 'click', 'target': '登录', 'required': True}]}
        bindings = {'bindings': [{'action_id': 'a1', 'operation': 'click', 'page_key': 'login_page', 'element_key': 'login_button'}], 'unresolved': []}
        plan = {
            'generated_case': {'steps': [{'action_id': 'a1', 'operation': 'click', 'page_key': 'login_page', 'element_key': 'login_button'}]},
            'test_plan': {'steps': [{'action_id': 'a1', 'operation': 'click', 'page_key': 'login_page', 'element_key': 'login_button'}]},
        }

        responses = [
            (requirements, {'stage': 'requirement_analysis'}),
            ({}, {'stage': 'state_route_selection'}),
            (bindings, {'stage': 'element_binding'}),
            (bindings, {'stage': 'element_binding'}),
            (plan, {'stage': 'executable_plan_generation'}),
            (bindings, {'stage': 'element_binding'}),
            (plan, {'stage': 'executable_plan_generation'}),
        ]
        calls = []

        planner.invoke = lambda stage, instruction, stage_input: (calls.append((stage, stage_input)) or True) and responses.pop(0)[0]
        result = planner.run()

        route_calls = [call for call in calls if call[0] == 'state_route_selection']
        binding_calls = [call for call in calls if call[0] == 'element_binding']
        plan_calls = [call for call in calls if call[0] == 'executable_plan_generation']
        self.assertEqual(result['planning_pipeline']['state_route']['ordered_page_keys'][0], 'login_page')
        self.assertEqual(result['planning_pipeline']['requirement_contract']['flow'][0]['action_id'], 'a1')
        self.assertEqual(result['planning_pipeline']['route_contract']['transition_keys'], ['login_button'])
        self.assertEqual(result['planning_pipeline']['binding_contract']['bindings'][0]['element_key'], 'login_button')
        self.assertEqual(result['planning_pipeline']['element_bindings']['bindings'][0]['element_key'], 'login_button')
        self.assertEqual(planner.requirement_contract.action_ids, ('a1',))
        self.assertEqual(planner.route_contract.ordered_page_keys, ('login_page', 'home_page'))
        self.assertEqual(planner.route_contract.transition_keys, ('login_button',))
        self.assertEqual(planner.binding_contract.bindings['a1']['element_key'], 'login_button')
        self.assertGreaterEqual(len(route_calls), 1)
        self.assertGreaterEqual(len(binding_calls), 2)
        self.assertGreaterEqual(len(plan_calls), 2)
        self.assertEqual(binding_calls[0][1]['route'], planner.route_contract.as_dict())
        self.assertEqual(plan_calls[0][1]['bindings'], planner.binding_contract.as_dict())

    def test_compiler_adds_only_explicit_actions_with_element_bindings(self):
        class ExplicitTask(self.TaskStub):
            source_requirement = (
                'When 用户填写表单\n'
                '| 字段名称 | 值 |\n'
                '| 对象名称 | 自动化对象 |\n'
                'And 用户点击"提交对象"按钮'
            )
            safety_policy = {
                **self.TaskStub.safety_policy,
                'allow_form_submit': True,
            }

        element_map = {
            'base_url': 'https://example.test',
            'pages': [{
                'page_key': 'form_state',
                'elements': [
                    {
                        'element_key': 'name_input',
                        'name': '对象名称',
                        'label': '对象名称',
                        'actions': ['fill'],
                        'recommended_locator': {'type': 'label', 'value': '对象名称', 'confidence': 0.9},
                    },
                    {
                        'element_key': 'submit_button',
                        'name': '提交对象',
                        'actions': ['click'],
                        'recommended_locator': {'type': 'role', 'value': 'button', 'name': '提交对象', 'confidence': 0.9},
                    },
                    {
                        'element_key': 'unrequested_input',
                        'name': '未请求字段',
                        'actions': ['fill'],
                        'recommended_locator': {'type': 'label', 'value': '未请求字段', 'confidence': 0.9},
                    },
                ],
            }],
        }
        parsed = {
            'generated_case': {'steps': [{'operation': 'goto', 'value': 'https://example.test'}]},
            'test_plan': {},
        }

        compiled = _compile_missing_explicit_actions(ExplicitTask(), parsed, element_map)
        steps = compiled['generated_case']['steps']

        self.assertTrue(any(step.get('operation') == 'fill' and step.get('element_key') == 'name_input' for step in steps))
        self.assertTrue(any(step.get('operation') == 'click' and step.get('element_key') == 'submit_button' for step in steps))
        self.assertFalse(any(step.get('element_key') == 'unrequested_input' for step in steps))

    def test_min_required_augment_helper_is_not_used_by_normalization(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'task_dialog',
                    'elements': [
                        {
                            'element_key': 'dialog_task',
                            'name': '创建任务',
                            'text': '创建任务 任务名称 测试环境 报告方式 执行方式 保存任务',
                            'tag': 'div',
                            'role': 'dialog',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'role', 'value': 'dialog', 'name': '创建任务'},
                        },
                        {
                            'element_key': 'input_task_name',
                            'name': '任务名称',
                            'label': '任务名称',
                            'placeholder': '请输入任务名称',
                            'tag': 'input',
                            'role': 'textbox',
                            'actions': ['fill'],
                            'in_dialog': True,
                            'dialog_name': '创建任务',
                            'recommended_locator': {'type': 'label', 'value': '任务名称'},
                        },
                        {
                            'element_key': 'select_env',
                            'name': '测试环境',
                            'label': '测试环境',
                            'tag': 'select',
                            'role': 'combobox',
                            'actions': ['select_option'],
                            'in_dialog': True,
                            'dialog_name': '创建任务',
                            'options': [{'label': '测试环境A', 'value': 'env-a'}],
                            'recommended_locator': {'type': 'label', 'value': '测试环境'},
                        },
                    ],
                },
                {
                    'page_key': 'repository_dialog',
                    'elements': [
                        {
                            'element_key': 'input_repository',
                            'name': '关联仓库',
                            'label': '关联仓库',
                            'placeholder': '关联仓库',
                            'tag': 'input',
                            'role': 'textbox',
                            'actions': ['fill'],
                            'required': True,
                            'in_dialog': True,
                            'dialog_name': '创建仓库配置',
                            'recommended_locator': {'type': 'label', 'value': '关联仓库'},
                        },
                        {
                            'element_key': 'input_branch',
                            'name': '代码分支版本',
                            'label': '代码分支版本',
                            'placeholder': '代码分支版本',
                            'tag': 'input',
                            'role': 'textbox',
                            'actions': ['fill'],
                            'required': True,
                            'in_dialog': True,
                            'dialog_name': '创建仓库配置',
                            'recommended_locator': {'type': 'label', 'value': '代码分支版本'},
                        },
                    ],
                },
            ],
        }
        parsed = {
            'generated_case': {
                'steps': [
                    {
                        'step_sort': 0,
                        'operation': 'assert_visible',
                        'page_key': 'task_dialog',
                        'element_key': 'dialog_task',
                        'target_name': '创建任务',
                        'description': '验证创建任务弹窗可见',
                        'locator_hint': {'type': 'role', 'value': 'dialog', 'name': '创建任务'},
                    },
                ],
            },
            'test_plan': {},
        }

        normalized = _normalize_llm_plan(self.TaskStub(), parsed, element_map)
        normalized_keys = {step.get('element_key') for step in normalized['generated_case']['steps']}

        self.assertIn('dialog_task', normalized_keys)
        self.assertNotIn('input_task_name', normalized_keys)
        self.assertNotIn('select_env', normalized_keys)
        self.assertNotIn('input_repository', normalized_keys)
        self.assertNotIn('input_branch', normalized_keys)

    def test_compaction_required_fields_are_scoped_to_relevant_business_page(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'repository_dialog',
                    'elements': [
                        {
                            'element_key': 'input_repository',
                            'name': '关联仓库',
                            'label': '关联仓库',
                            'required': True,
                            'in_dialog': True,
                            'dialog_name': '创建仓库配置',
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': '关联仓库'},
                        },
                    ],
                },
                {
                    'page_key': 'task_dialog',
                    'elements': [
                        {
                            'element_key': 'input_task_name',
                            'name': '任务名称',
                            'label': '任务名称',
                            'required': True,
                            'in_dialog': True,
                            'dialog_name': '创建任务',
                            'actions': ['fill'],
                            'recommended_locator': {'type': 'label', 'value': '任务名称'},
                        },
                        {
                            'element_key': 'select_env',
                            'name': '测试环境',
                            'label': '测试环境',
                            'required': True,
                            'in_dialog': True,
                            'dialog_name': '创建任务',
                            'actions': ['select_option'],
                            'recommended_locator': {'type': 'label', 'value': '测试环境'},
                        },
                    ],
                },
            ],
        }

        compact = _compact_element_map(element_map, task=self.TaskStub())
        required_keys = {field['element_key'] for field in compact['required_fields']}

        self.assertIn('input_task_name', required_keys)
        self.assertIn('select_env', required_keys)
        self.assertNotIn('input_repository', required_keys)

    def test_gherkin_table_requirements_are_exposed_as_matched_action_evidence(self):
        class GherkinTask(self.TaskStub):
            source_requirement = ''
            gherkin = '''
Feature: 测试任务管理
  Scenario: 新建测试任务
    And 用户在新建弹窗中填写必填项：
      | 字段名称 | 值 /(操作提示)|
      | 测试任务 | 自动化测试任务_20260804 |
      | 测试环境 | (下拉选择第一个选项) |
      | 报告方式 | 不发送邮件 |
      | 执行方式 | 立即执行 |
    And 用户点击"保存任务"按钮
'''

        element_map = {
            'pages': [
                {
                    'page_key': 'page_4',
                    'elements': [
                        {
                            'element_key': 'input_name',
                            'name': '测试任务',
                            'label': '测试任务',
                            'actions': ['fill'],
                            'required': True,
                            'recommended_locator': {'type': 'label', 'value': '测试任务'},
                        },
                        {
                            'element_key': 'select_env',
                            'name': '测试环境',
                            'label': '测试环境',
                            'actions': ['select_option'],
                            'required': True,
                            'recommended_locator': {'type': 'label', 'value': '测试环境'},
                        },
                        {
                            'element_key': 'radio_no_mail',
                            'name': '不发送邮件',
                            'label': '不发送邮件',
                            'form_label': '报告方式',
                            'actions': ['check'],
                            'required': True,
                            'recommended_locator': {'type': 'label', 'value': '不发送邮件'},
                        },
                        {
                            'element_key': 'radio_now',
                            'name': '立即执行',
                            'label': '立即执行',
                            'form_label': '执行方式',
                            'actions': ['check'],
                            'required': True,
                            'recommended_locator': {'type': 'label', 'value': '立即执行'},
                        },
                        {
                            'element_key': 'button_save',
                            'name': '保存任务',
                            'text': '保存任务',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'role', 'value': 'button', 'name': '保存任务'},
                        },
                        {
                            'element_key': 'input_repo',
                            'name': '关联仓库',
                            'label': '关联仓库',
                            'actions': ['fill'],
                            'required': False,
                            'recommended_locator': {'type': 'label', 'value': '关联仓库'},
                        },
                    ],
                },
            ],
        }

        requirements = _extract_explicit_action_requirements(GherkinTask())
        compact = _compact_element_map(element_map, task=GherkinTask())
        matched_keys = {
            item['element_key']
            for item in compact['explicit_action_requirements']['matched_elements']
        }
        required_keys = {field['element_key'] for field in compact['required_fields']}

        self.assertEqual([item['name'] for item in requirements['fields']], ['测试任务', '测试环境', '报告方式', '执行方式'])
        self.assertIn('input_name', matched_keys)
        self.assertIn('select_env', matched_keys)
        self.assertIn('radio_no_mail', matched_keys)
        self.assertIn('radio_now', matched_keys)
        self.assertIn('button_save', matched_keys)
        self.assertNotIn('input_repo', required_keys)

    def test_generated_label_fill_uses_form_field_scope_not_raw_get_by_label(self):
        element_map = {'base_url': 'https://example.test/task', 'pages': []}
        steps = [
            {
                'step_sort': 0,
                'operation': 'fill',
                'page_key': 'page_4',
                'element_key': 'input_101',
                'target_name': '任务名称',
                'description': '填写任务名称',
                'locator_hint': {'type': 'label', 'value': '任务名称'},
                'value': '自动化测试数据',
            },
        ]

        script = _build_script_from_steps(self.TaskStub(), steps, element_map)
        ts_spec = _build_typescript_spec_from_steps(self.TaskStub(), steps, element_map, {})

        self.assertIn('.el-form-item', script)
        self.assertIn('has_text="任务名称"', script)
        self.assertIn('.locator("input, textarea, select', script)
        self.assertNotIn('get_by_label("任务名称")', script)
        self.assertIn('.el-form-item', ts_spec)
        self.assertIn('hasText: "任务名称"', ts_spec)
        self.assertIn('.locator("input, textarea, select', ts_spec)
        self.assertNotIn('getByLabel("任务名称")', ts_spec)

    def test_generated_click_uses_row_scope_when_action_name_is_duplicated(self):
        element_map = {
            'base_url': 'https://example.test/entities',
            'pages': [
                {
                    'page_key': 'page_1',
                    'elements': [
                        {
                            'element_key': 'button_source',
                            'name': 'Row action',
                            'role': 'button',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Row action'},
                            'row_text': 'Source entity Enabled Row action',
                            'row_index': 0,
                            'row_cells': [
                                {'column_index': 0, 'column_name': 'Entity name', 'text': 'Source entity'},
                                {'column_index': 1, 'column_name': 'State', 'text': 'Enabled'},
                                {'column_index': 2, 'column_name': 'Operation', 'text': 'Row action'},
                            ],
                            'column_index': 2,
                            'column_name': 'Operation',
                            'column_text': 'Row action',
                            'table_name': 'Entity permissions',
                        },
                        {
                            'element_key': 'button_target',
                            'name': 'Row action',
                            'role': 'button',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'role', 'value': 'button', 'name': 'Row action'},
                            'row_text': 'Target entity Enabled Row action',
                            'row_index': 1,
                            'row_cells': [
                                {'column_index': 0, 'column_name': 'Entity name', 'text': 'Target entity'},
                                {'column_index': 1, 'column_name': 'State', 'text': 'Enabled'},
                                {'column_index': 2, 'column_name': 'Operation', 'text': 'Row action'},
                            ],
                            'column_index': 2,
                            'column_name': 'Operation',
                            'column_text': 'Row action',
                            'table_name': 'Entity permissions',
                        },
                    ],
                },
            ],
        }
        parsed = {
            'generated_case': {
                'steps': [
                    {
                        'operation': 'click',
                        'page_key': 'page_1',
                        'element_key': 'button_target',
                        'target_name': 'Target entity row action',
                        'description': 'Click the target entity row action',
                    },
                ],
            },
            'test_plan': {},
        }

        normalized = _normalize_llm_plan(self.TaskStub(), parsed, element_map)
        step = normalized['generated_case']['steps'][0]
        ts_spec = _build_typescript_spec_from_steps(self.TaskStub(), [step], element_map, {})

        self.assertEqual(step['row_scope']['row_text'], 'Target entity Enabled Row action')
        self.assertIn('page.locator("tbody tr, .el-table__row', ts_spec)
        self.assertIn('filter({ hasText: "Target entity Enabled Row action" })', ts_spec)
        self.assertIn('getByRole("button", { name: "Row action", exact: true })', ts_spec)
        self.assertLess(
            ts_spec.index('filter({ hasText: "Target entity Enabled Row action" })'),
            ts_spec.index('getByRole("button", { name: "Row action", exact: true })'),
        )

    def test_typescript_spec_imports_expect_for_repair_compatibility(self):
        ts_spec = _build_typescript_spec_from_steps(self.TaskStub(), [], {'base_url': 'https://example.test'}, {})

        self.assertIn("import { test, expect } from '@playwright/test';", ts_spec)

    def test_typescript_runtime_waits_for_async_rendered_locator(self):
        helper = _build_typescript_runtime_helper()

        self.assertIn('const deadline = Date.now()', helper)
        self.assertIn("await candidate.waitFor({ state: 'visible', timeout: 200 });", helper)
        self.assertIn('await new Promise((resolve) => setTimeout(resolve, 150));', helper)
        self.assertIn('matched=${lastCount}', helper)
        self.assertIn("await locator.press('ArrowDown', { timeout: 5000 });", helper)
        self.assertIn("this.page.getByRole('option').filter({", helper)
        self.assertIn("new RegExp(`^\\\\s*${escapeRegExp(value)}\\\\s*$`, 'i')", helper)
        self.assertIn("await locator.check({ timeout: 5000 });", helper)
        self.assertIn("await locator.check({ force: true, timeout: 5000 });", helper)
        self.assertIn("private async captureUiFeedback()", helper)
        self.assertIn("[role=\"alert\"], .el-message, .el-notification", helper)
        self.assertIn("recent UI feedback:", helper)
        self.assertIn("await this.captureUiFeedback();", helper)

    def test_low_risk_create_save_is_not_downgraded_to_assert_visible(self):
        element_map = {
            'pages': [
                {
                    'page_key': 'page_4',
                    'elements': [
                        {
                            'element_key': 'button_136',
                            'name': '保存任务',
                            'text': '保存任务',
                            'actions': ['click'],
                            'recommended_locator': {'type': 'role', 'value': 'button', 'name': '保存任务'},
                        },
                    ],
                },
            ],
        }
        parsed = {
            'generated_case': {
                'steps': [
                    {
                        'operation': 'click',
                        'page_key': 'page_4',
                        'element_key': 'button_136',
                        'target_name': '保存任务',
                        'description': '点击保存任务',
                    },
                ],
            },
            'test_plan': {},
        }

        normalized = _normalize_llm_plan(self.TaskStub(), parsed, element_map)
        step = normalized['generated_case']['steps'][0]

        self.assertEqual(step['operation'], 'click')
        self.assertFalse(step['requires_confirmation'])
        self.assertNotEqual(step.get('original_operation'), 'click')


class UiModuleSortingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='testuser',
            password='password',
            email='test@example.com',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Test Project',
            description='Test Description',
            creator=self.user,
        )
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')

        self.root1 = UiModule.objects.create(
            project=self.project,
            name='Root 1',
            creator=self.user,
            order=1,
        )
        self.child1_1 = UiModule.objects.create(
            project=self.project,
            name='Child 1-1',
            parent=self.root1,
            creator=self.user,
            order=1,
        )
        self.child1_1_1 = UiModule.objects.create(
            project=self.project,
            name='Child 1-1-1',
            parent=self.child1_1,
            creator=self.user,
            order=1,
        )
        self.child1_2 = UiModule.objects.create(
            project=self.project,
            name='Child 1-2',
            parent=self.root1,
            creator=self.user,
            order=2,
        )
        self.root2 = UiModule.objects.create(
            project=self.project,
            name='Root 2',
            creator=self.user,
            order=2,
        )

    def test_get_max_depth(self):
        self.assertEqual(self.root1.get_max_depth(), 3)
        self.assertEqual(self.child1_1.get_max_depth(), 2)
        self.assertEqual(self.child1_1_1.get_max_depth(), 1)

    def test_move_api_sibling_reorder_before(self):
        url = f'/api/ui-automation/modules/{self.child1_2.id}/move/'
        data = {
            'target_id': self.child1_1.id,
            'drop_position': -1,
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.child1_1.refresh_from_db()
        self.child1_2.refresh_from_db()

        self.assertEqual(self.child1_2.order, 1)
        self.assertEqual(self.child1_1.order, 2)
        self.assertEqual(self.child1_2.parent, self.root1)
        self.assertEqual(self.child1_1.parent, self.root1)

    def test_move_api_into_parent(self):
        url = f'/api/ui-automation/modules/{self.child1_2.id}/move/'
        data = {
            'target_id': self.root2.id,
            'drop_position': 0,
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.child1_2.refresh_from_db()
        self.assertEqual(self.child1_2.parent, self.root2)
        self.assertEqual(self.child1_2.level, 2)

    def test_move_api_circular_reference_protection(self):
        url = f'/api/ui-automation/modules/{self.root1.id}/move/'
        data = {
            'target_id': self.child1_1_1.id,
            'drop_position': 0,
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('无法移动模块到自身或其子模块下', response.data['error'])

    def test_move_api_depth_limit_protection(self):
        child4 = UiModule.objects.create(
            project=self.project,
            name='Child 4',
            parent=self.child1_1_1,
            creator=self.user,
        )
        UiModule.objects.create(
            project=self.project,
            name='Child 5',
            parent=child4,
            creator=self.user,
        )

        url = f'/api/ui-automation/modules/{self.root1.id}/move/'
        data = {
            'target_id': self.root2.id,
            'drop_position': 0,
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('超过5级限制', response.data['error'])
