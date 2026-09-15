# -*- coding: utf-8 -*-
"""UI 自动化序列化器"""

import json

from rest_framework import serializers
from .models import (
    UiModule, UiPage, UiElement, UiPageSteps, UiPageStepsDetailed,
    UiTestCase, UiCaseStepsDetailed, UiExecutionRecord, UiPublicData, UiEnvironmentConfig,
    UiBatchExecutionRecord, UiElementMap, UiAiGenerationTask
)


class UiModuleSerializer(serializers.ModelSerializer):
    """模块序列化器"""
    children = serializers.SerializerMethodField()
    creator_name = serializers.CharField(source='creator.username', read_only=True)

    class Meta:
        model = UiModule
        fields = ['id', 'project', 'name', 'parent', 'level', 'order', 'children', 'creator', 'creator_name', 'created_at', 'updated_at']
        read_only_fields = ['level', 'creator', 'created_at', 'updated_at', 'order']

    def get_children(self, obj):
        children = obj.children.all()
        return UiModuleSerializer(children, many=True).data if children else []


class UiElementSerializer(serializers.ModelSerializer):
    """元素序列化器"""
    creator_name = serializers.CharField(source='creator.username', read_only=True)

    class Meta:
        model = UiElement
        fields = '__all__'
        read_only_fields = ['creator', 'created_at', 'updated_at']


class UiPageSerializer(serializers.ModelSerializer):
    """页面序列化器"""
    module_name = serializers.CharField(source='module.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    element_count = serializers.SerializerMethodField()

    class Meta:
        model = UiPage
        fields = '__all__'
        read_only_fields = ['creator', 'created_at', 'updated_at']

    def get_element_count(self, obj):
        return obj.elements.count()


class UiPageDetailSerializer(UiPageSerializer):
    """页面详情序列化器（含元素列表）"""
    elements = UiElementSerializer(many=True, read_only=True)

    class Meta(UiPageSerializer.Meta):
        fields = '__all__'


class UiPageStepsDetailedSerializer(serializers.ModelSerializer):
    """步骤详情序列化器"""
    element_name = serializers.CharField(source='element.name', read_only=True)
    page_name = serializers.SerializerMethodField()
    module_name = serializers.SerializerMethodField()

    class Meta:
        model = UiPageStepsDetailed
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']

    def get_page_name(self, obj):
        if obj.element and obj.element.page:
            return obj.element.page.name
        return None

    def get_module_name(self, obj):
        if obj.element and obj.element.page and obj.element.page.module:
            return obj.element.page.module.name
        return None


class UiPageStepsDetailedExecuteSerializer(serializers.ModelSerializer):
    """步骤详情序列化器（含元素定位信息，用于执行器）"""
    element_name = serializers.CharField(source='element.name', read_only=True)
    locator_type = serializers.CharField(source='element.locator_type', read_only=True)
    locator_value = serializers.CharField(source='element.locator_value', read_only=True)
    locator_index = serializers.IntegerField(source='element.locator_index', read_only=True)
    locator_type_2 = serializers.CharField(source='element.locator_type_2', read_only=True)
    locator_value_2 = serializers.CharField(source='element.locator_value_2', read_only=True)
    locator_index_2 = serializers.IntegerField(source='element.locator_index_2', read_only=True)
    locator_type_3 = serializers.CharField(source='element.locator_type_3', read_only=True)
    locator_value_3 = serializers.CharField(source='element.locator_value_3', read_only=True)
    locator_index_3 = serializers.IntegerField(source='element.locator_index_3', read_only=True)
    wait_time = serializers.IntegerField(source='element.wait_time', read_only=True)
    is_iframe = serializers.BooleanField(source='element.is_iframe', read_only=True)
    iframe_locator = serializers.CharField(source='element.iframe_locator', read_only=True)

    class Meta:
        model = UiPageStepsDetailed
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class UiPageStepsListSerializer(serializers.ModelSerializer):
    """页面步骤列表序列化器（精简字段，提升性能）"""
    page_name = serializers.CharField(source='page.name', read_only=True)
    module_name = serializers.CharField(source='module.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    step_count = serializers.SerializerMethodField()

    class Meta:
        model = UiPageSteps
        fields = [
            'id', 'project', 'page', 'page_name', 'module', 'module_name',
            'name', 'status', 'step_count', 'creator', 'creator_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['status', 'creator', 'created_at', 'updated_at']

    def get_step_count(self, obj):
        return obj.step_details.count()


class UiPageStepsSerializer(serializers.ModelSerializer):
    """页面步骤序列化器"""
    page_name = serializers.CharField(source='page.name', read_only=True)
    module_name = serializers.CharField(source='module.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    step_count = serializers.SerializerMethodField()

    class Meta:
        model = UiPageSteps
        fields = '__all__'
        read_only_fields = ['status', 'result_data', 'creator', 'created_at', 'updated_at']

    def get_step_count(self, obj):
        return obj.step_details.count()


class UiPageStepsDetailSerializer(UiPageStepsSerializer):
    """页面步骤详情序列化器（含步骤详情列表）"""
    step_details = UiPageStepsDetailedSerializer(many=True, read_only=True)

    class Meta(UiPageStepsSerializer.Meta):
        fields = '__all__'


class UiPageStepsExecuteSerializer(UiPageStepsSerializer):
    """页面步骤执行序列化器（含步骤详情列表和元素定位信息）"""
    step_details = UiPageStepsDetailedExecuteSerializer(many=True, read_only=True)
    page_url = serializers.CharField(source='page.url', read_only=True)

    class Meta(UiPageStepsSerializer.Meta):
        fields = '__all__'


class UiCaseStepsDetailedSerializer(serializers.ModelSerializer):
    """用例步骤序列化器"""
    page_step_name = serializers.CharField(source='page_step.name', read_only=True)
    page_name = serializers.SerializerMethodField()
    module_name = serializers.SerializerMethodField()

    class Meta:
        model = UiCaseStepsDetailed
        fields = '__all__'
        read_only_fields = ['status', 'error_message', 'result_data', 'created_at', 'updated_at']

    def get_page_name(self, obj):
        if obj.page_step and obj.page_step.page:
            return obj.page_step.page.name
        return None

    def get_module_name(self, obj):
        if obj.page_step and obj.page_step.module:
            return obj.page_step.module.name
        return None


class UiCaseStepsWithDetailSerializer(serializers.ModelSerializer):
    """用例步骤序列化器（含完整page_step详情）- 用于执行时获取步骤详情"""
    page_step = UiPageStepsExecuteSerializer(read_only=True)

    class Meta:
        model = UiCaseStepsDetailed
        fields = '__all__'
        read_only_fields = ['status', 'error_message', 'result_data', 'created_at', 'updated_at']


class UiTestCaseListSerializer(serializers.ModelSerializer):
    """测试用例列表序列化器（精简字段，提升性能）"""
    module_name = serializers.CharField(source='module.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    step_count = serializers.SerializerMethodField()

    class Meta:
        model = UiTestCase
        fields = [
            'id', 'project', 'module', 'module_name', 'name', 'level', 'status',
            'step_count', 'creator', 'creator_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['status', 'creator', 'created_at', 'updated_at']

    def get_step_count(self, obj):
        return obj.case_steps.count()


class UiTestCaseSerializer(serializers.ModelSerializer):
    """测试用例序列化器"""
    module_name = serializers.CharField(source='module.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    step_count = serializers.SerializerMethodField()

    class Meta:
        model = UiTestCase
        fields = '__all__'
        read_only_fields = ['status', 'result_data', 'error_message', 'creator', 'created_at', 'updated_at']

    def get_step_count(self, obj):
        return obj.case_steps.count()


class UiTestCaseDetailSerializer(UiTestCaseSerializer):
    """测试用例详情序列化器（含步骤列表）"""
    case_steps = UiCaseStepsDetailedSerializer(many=True, read_only=True)

    class Meta(UiTestCaseSerializer.Meta):
        fields = '__all__'


class UiTestCaseExecuteSerializer(UiTestCaseSerializer):
    """测试用例执行序列化器（含完整步骤详情，用于执行器获取数据）"""
    case_step_details = UiCaseStepsWithDetailSerializer(source='case_steps', many=True, read_only=True)

    class Meta(UiTestCaseSerializer.Meta):
        fields = '__all__'


class UiExecutionRecordListSerializer(serializers.ModelSerializer):
    """执行记录列表序列化器（精简字段，提升性能）"""
    test_case_name = serializers.CharField(source='test_case.name', read_only=True)
    executor_name = serializers.CharField(source='executor.username', read_only=True)

    class Meta:
        model = UiExecutionRecord
        fields = [
            'id', 'batch', 'test_case', 'test_case_name', 'executor', 'executor_name',
            'status', 'trigger_type', 'start_time', 'end_time', 'duration', 'created_at'
        ]
        read_only_fields = ['created_at']


class UiExecutionRecordBatchDetailSerializer(serializers.ModelSerializer):
    """批量执行详情中的执行记录序列化器（包含步骤结果和错误信息，不含过大字段）"""
    test_case_name = serializers.CharField(source='test_case.name', read_only=True)
    executor_name = serializers.CharField(source='executor.username', read_only=True)

    class Meta:
        model = UiExecutionRecord
        fields = [
            'id', 'batch', 'test_case', 'test_case_name', 'executor', 'executor_name',
            'status', 'trigger_type', 'start_time', 'end_time', 'duration',
            'step_results', 'screenshots', 'error_message', 'trace_path', 'created_at'
        ]
        read_only_fields = ['created_at']


class UiExecutionRecordSerializer(serializers.ModelSerializer):
    """执行记录序列化器"""
    test_case_name = serializers.CharField(source='test_case.name', read_only=True)
    executor_name = serializers.CharField(source='executor.username', read_only=True)

    class Meta:
        model = UiExecutionRecord
        fields = '__all__'
        read_only_fields = ['created_at']


class UiPublicDataSerializer(serializers.ModelSerializer):
    """公共数据序列化器"""
    creator_name = serializers.CharField(source='creator.username', read_only=True)

    class Meta:
        model = UiPublicData
        fields = '__all__'
        read_only_fields = ['creator', 'created_at', 'updated_at']


class UiEnvironmentConfigSerializer(serializers.ModelSerializer):
    """环境配置序列化器"""
    creator_name = serializers.CharField(source='creator.username', read_only=True)

    def to_internal_value(self, data):
        mutable = data.copy()
        login_username = mutable.pop('login_username', None)
        login_password = mutable.pop('login_password', None)
        clear_credentials = mutable.pop('clear_login_credentials', False)
        mutable.pop('login_enabled', None)
        mutable.pop('has_login_password', None)
        validated = super().to_internal_value(mutable)

        current_extra = (
            self.instance.extra_config
            if self.instance and isinstance(self.instance.extra_config, dict)
            else {}
        )
        incoming_extra = validated.get('extra_config') if isinstance(validated.get('extra_config'), dict) else {}
        extra_config = {**current_extra, **incoming_extra}
        auth_config = {
            **(current_extra.get('auth') if isinstance(current_extra.get('auth'), dict) else {}),
            **(incoming_extra.get('auth') if isinstance(incoming_extra.get('auth'), dict) else {}),
        }
        if clear_credentials in {True, 'true', '1', 1}:
            extra_config.pop('auth', None)
        elif login_username is not None or login_password not in (None, ''):
            if login_username is not None:
                auth_config['username'] = str(login_username).strip()
            if login_password not in (None, ''):
                auth_config['password'] = str(login_password)
            if not auth_config.get('username') or not auth_config.get('password'):
                raise serializers.ValidationError({
                    'login_password': '启用自动登录时必须同时配置登录账号和密码',
                })
            extra_config['auth'] = auth_config
        elif auth_config:
            extra_config['auth'] = auth_config
        validated['extra_config'] = extra_config
        return validated

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        extra_config = dict(instance.extra_config or {}) if isinstance(instance.extra_config, dict) else {}
        auth_config = dict(extra_config.get('auth') or {})
        sanitized_auth = dict(auth_config)
        sanitized_auth.pop('password', None)
        if sanitized_auth:
            extra_config['auth'] = sanitized_auth
        else:
            extra_config.pop('auth', None)
        representation['extra_config'] = extra_config
        representation['login_enabled'] = bool(auth_config.get('username') and auth_config.get('password'))
        representation['login_username'] = str(auth_config.get('username') or '')
        representation['has_login_password'] = bool(auth_config.get('password'))
        return representation

    class Meta:
        model = UiEnvironmentConfig
        fields = '__all__'
        read_only_fields = ['creator', 'created_at', 'updated_at']


class UiBatchExecutionRecordSerializer(serializers.ModelSerializer):
    """批量执行记录序列化器"""
    executor_name = serializers.CharField(source='executor.username', read_only=True)
    success_rate = serializers.SerializerMethodField()

    class Meta:
        model = UiBatchExecutionRecord
        fields = '__all__'
        read_only_fields = ['created_at']

    def get_success_rate(self, obj):
        if obj.total_cases == 0:
            return 0
        return round(obj.passed_cases / obj.total_cases * 100, 1)


class UiBatchExecutionRecordDetailSerializer(UiBatchExecutionRecordSerializer):
    """批量执行记录详情序列化器（含关联执行记录详情：包含步骤结果和错误信息）"""
    execution_records = UiExecutionRecordBatchDetailSerializer(many=True, read_only=True)

    class Meta(UiBatchExecutionRecordSerializer.Meta):
        fields = '__all__'


class UiElementMapSerializer(serializers.ModelSerializer):
    """元素地图序列化器"""
    project_name = serializers.CharField(source='project.name', read_only=True)
    environment_name = serializers.CharField(source='environment_config.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    confirmed_by_name = serializers.CharField(source='confirmed_by.username', read_only=True)
    page_count = serializers.SerializerMethodField()
    element_count = serializers.SerializerMethodField()

    class Meta:
        model = UiElementMap
        fields = '__all__'
        read_only_fields = ['creator', 'confirmed_by', 'created_at', 'updated_at']

    def _pages(self, obj):
        if not isinstance(obj.map_json, dict):
            return []
        return obj.map_json.get('pages') or []

    def get_page_count(self, obj):
        return len(self._pages(obj))

    def get_element_count(self, obj):
        return sum(len(page.get('elements') or []) for page in self._pages(obj) if isinstance(page, dict))


class UiAiGenerationTaskListSerializer(serializers.ModelSerializer):
    """AI UI 生成任务列表序列化器"""
    project_name = serializers.CharField(source='project.name', read_only=True)
    environment_name = serializers.CharField(source='environment_config.name', read_only=True)
    element_map_name = serializers.CharField(source='element_map.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    execution_mode = serializers.SerializerMethodField()
    review_queue_count = serializers.SerializerMethodField()

    class Meta:
        model = UiAiGenerationTask
        fields = [
            'id', 'project', 'project_name', 'environment_config', 'environment_name',
            'element_map', 'element_map_name', 'name', 'source_type', 'target_url',
            'target_module', 'status', 'failure_category', 'execution_mode', 'actuator_id',
            'dispatch_id', 'dispatch_status', 'dispatch_attempts', 'last_dispatched_at',
            'last_ack_at', 'last_dispatch_error', 'max_repair_rounds',
            'current_repair_round', 'review_queue_count', 'error_message',
            'creator', 'creator_name', 'started_at', 'completed_at', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'status', 'failure_category', 'actuator_id', 'dispatch_id', 'dispatch_status',
            'dispatch_attempts', 'last_dispatched_at', 'last_ack_at', 'last_dispatch_error',
            'current_repair_round', 'error_message', 'creator', 'started_at',
            'completed_at', 'created_at', 'updated_at'
        ]

    def get_review_queue_count(self, obj):
        verification = obj.verification_result if isinstance(obj.verification_result, dict) else {}
        queue = verification.get('review_queue') if isinstance(verification.get('review_queue'), list) else []
        return sum(1 for item in queue if isinstance(item, dict) and item.get('status') == 'pending')

    def get_execution_mode(self, obj):
        policy = obj.safety_policy if isinstance(obj.safety_policy, dict) else {}
        return str(policy.get('execution_mode') or 'contract_planner')


class UiAiGenerationTaskSerializer(serializers.ModelSerializer):
    """AI UI 生成任务详情序列化器"""
    project_name = serializers.CharField(source='project.name', read_only=True)
    environment_name = serializers.CharField(source='environment_config.name', read_only=True)
    element_map_name = serializers.CharField(source='element_map.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    execution_mode = serializers.SerializerMethodField()

    class Meta:
        model = UiAiGenerationTask
        fields = '__all__'
        read_only_fields = [
            'status', 'actuator_id', 'test_plan', 'mcp_observations',
            'element_map_snapshot', 'generated_case', 'generated_script',
            'generated_script_hash', 'verification_result', 'repair_history',
            'failure_category', 'dispatch_id', 'dispatch_status', 'dispatch_attempts',
            'last_dispatched_at', 'last_ack_at', 'last_dispatch_error',
            'current_repair_round', 'error_message', 'creator', 'started_at',
            'completed_at', 'created_at', 'updated_at'
        ]

    def get_execution_mode(self, obj):
        policy = obj.safety_policy if isinstance(obj.safety_policy, dict) else {}
        return str(policy.get('execution_mode') or 'contract_planner')


class UiAiGenerationTaskDetailSerializer(serializers.ModelSerializer):
    """AI UI 生成任务轻量详情序列化器。

    大字段通过 detail-payload 按需加载，避免详情抽屉每次打开都返回 MB 级 JSON。
    """
    project_name = serializers.CharField(source='project.name', read_only=True)
    environment_name = serializers.CharField(source='environment_config.name', read_only=True)
    element_map_name = serializers.CharField(source='element_map.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    execution_mode = serializers.SerializerMethodField()
    test_plan = serializers.SerializerMethodField()
    mcp_observations = serializers.SerializerMethodField()
    element_map_snapshot = serializers.SerializerMethodField()
    generated_case = serializers.SerializerMethodField()
    generated_script = serializers.SerializerMethodField()
    verification_result = serializers.SerializerMethodField()
    repair_history = serializers.SerializerMethodField()
    payload_summary = serializers.SerializerMethodField()

    class Meta:
        model = UiAiGenerationTask
        fields = [
            'id', 'project', 'project_name', 'environment_config', 'environment_name',
            'element_map', 'element_map_name', 'name', 'source_type', 'source_requirement',
            'gherkin', 'target_url', 'target_module', 'safety_policy', 'status',
            'execution_mode', 'actuator_id', 'dispatch_id', 'dispatch_status', 'dispatch_attempts',
            'last_dispatched_at', 'last_ack_at', 'last_dispatch_error',
            'test_plan', 'mcp_observations', 'element_map_snapshot', 'generated_case',
            'generated_script', 'generated_script_hash', 'verification_result',
            'repair_history', 'payload_summary', 'failure_category', 'max_repair_rounds',
            'current_repair_round', 'error_message', 'creator', 'creator_name',
            'started_at', 'completed_at', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    @staticmethod
    def _json_size(value) -> int:
        if value in (None, '', {}, []):
            return 0
        if isinstance(value, str):
            return len(value.encode('utf-8'))
        try:
            return len(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8'))
        except TypeError:
            return 0

    @staticmethod
    def _steps_summary(payload: dict) -> dict:
        steps = payload.get('steps') if isinstance(payload, dict) and isinstance(payload.get('steps'), list) else []
        return {
            'step_count': len(steps),
            'objective': payload.get('objective') if isinstance(payload, dict) else '',
            'expected_results': payload.get('expected_results') if isinstance(payload, dict) else [],
            'steps_preview': [
                {
                    'step_sort': item.get('step_sort', index) if isinstance(item, dict) else index,
                    'operation': item.get('operation') if isinstance(item, dict) else '',
                    'target_name': item.get('target_name') if isinstance(item, dict) else '',
                    'description': item.get('description') if isinstance(item, dict) else '',
                }
                for index, item in enumerate(steps[:20])
                if isinstance(item, dict)
            ],
            '_lazy_summary': True,
        }

    @staticmethod
    def _map_summary(payload: dict) -> dict:
        pages = payload.get('pages') if isinstance(payload, dict) and isinstance(payload.get('pages'), list) else []
        return {
            'base_url': payload.get('base_url') if isinstance(payload, dict) else '',
            'capture_mode': payload.get('capture_mode') if isinstance(payload, dict) else '',
            'source': payload.get('source') if isinstance(payload, dict) else '',
            'page_count': len(pages),
            'element_count': sum(
                len(page.get('elements') or []) + sum(len(frame.get('elements') or []) for frame in (page.get('frames') or []) if isinstance(frame, dict))
                for page in pages
                if isinstance(page, dict)
            ),
            'pages_preview': [
                {
                    'page_key': page.get('page_key') or '',
                    'url': page.get('url') or '',
                    'title': page.get('title') or page.get('name') or '',
                    'element_count': len(page.get('elements') or []),
                    'frame_count': len(page.get('frames') or []),
                }
                for page in pages[:30]
                if isinstance(page, dict)
            ],
            '_lazy_summary': True,
        }

    @staticmethod
    def _verification_summary(payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {'_lazy_summary': True}
        report_artifacts = payload.get('report_artifacts') if isinstance(payload.get('report_artifacts'), dict) else {}
        script_artifacts = payload.get('script_artifacts') if isinstance(payload.get('script_artifacts'), dict) else {}
        summary = report_artifacts.get('summary') if isinstance(report_artifacts.get('summary'), dict) else {}
        queue = payload.get('review_queue') if isinstance(payload.get('review_queue'), list) else []
        return {
            'plan_validation': payload.get('plan_validation') if isinstance(payload.get('plan_validation'), dict) else {},
            'report_artifacts': {
                'html': report_artifacts.get('html') or '',
                'junit_xml': report_artifacts.get('junit_xml') or '',
                'json': report_artifacts.get('json') or '',
                'summary': summary,
            },
            'script_artifacts': {
                'playwright_ts_spec_hash': script_artifacts.get('playwright_ts_spec_hash') or '',
                'playwright_ts_files_hash': script_artifacts.get('playwright_ts_files_hash') or '',
                'typescript_spec_status': payload.get('typescript_spec_status') or '',
            },
            'review_queue': [
                item for item in queue
                if isinstance(item, dict) and item.get('status') == 'pending'
            ][:50],
            '_lazy_summary': True,
        }

    @staticmethod
    def _repair_history_summary(payload: list) -> list:
        if not isinstance(payload, list):
            return []
        result = []
        for index, item in enumerate(payload[:30]):
            if not isinstance(item, dict):
                continue
            changed_steps = item.get('changed_steps') if isinstance(item.get('changed_steps'), list) else []
            result.append({
                'index': index,
                'type': item.get('type') or '',
                'status': item.get('status') or '',
                'round': item.get('round'),
                'message': item.get('message') or item.get('repair_summary') or '',
                'changed_step_count': len(changed_steps),
                'review_required': bool(item.get('review_required')),
            })
        return result

    def get_test_plan(self, obj):
        return self._steps_summary(obj.test_plan if isinstance(obj.test_plan, dict) else {})

    def get_mcp_observations(self, obj):
        observations = obj.mcp_observations if isinstance(obj.mcp_observations, list) else []
        return {
            'observation_count': len(observations),
            'items_preview': observations[:20],
            '_lazy_summary': True,
        }

    def get_element_map_snapshot(self, obj):
        element_map = getattr(obj, 'element_map', None)
        coverage = (
            element_map.coverage_summary
            if element_map and isinstance(element_map.coverage_summary, dict) else {}
        )
        return {
            'base_url': getattr(element_map, 'base_url', '') if element_map else '',
            'page_count': coverage.get('page_count') or 0,
            'element_count': coverage.get('element_count') or 0,
            'state_transition_count': coverage.get('state_transition_count') or 0,
            '_lazy_summary': True,
        }

    def get_generated_case(self, obj):
        generated_case = obj.generated_case if isinstance(obj.generated_case, dict) else {}
        summary = self._steps_summary(generated_case)
        for key in [
            'applied', 'applied_at', 'applied_module_id', 'applied_page_id',
            'applied_page_ids', 'applied_page_step_id', 'applied_test_case_id',
            'applied_element_count', 'applied_step_count', 'applied_execution_mode',
            'applied_typescript_spec_hash', 'playwright_ts_spec_hash',
        ]:
            if key in generated_case:
                summary[key] = generated_case.get(key)
        return summary

    def get_generated_script(self, obj):
        script = obj.generated_script or ''
        return script[:2000] if len(script) <= 2000 else f'{script[:2000]}\n...'

    def get_verification_result(self, obj):
        return {'_lazy_summary': True}

    def get_repair_history(self, obj):
        return self._repair_history_summary(obj.repair_history if isinstance(obj.repair_history, list) else [])

    def get_execution_mode(self, obj):
        policy = obj.safety_policy if isinstance(obj.safety_policy, dict) else {}
        return str(policy.get('execution_mode') or 'contract_planner')

    def get_payload_summary(self, obj):
        heavy_fields = [
            'test_plan', 'mcp_observations', 'element_map_snapshot', 'generated_case',
            'generated_script', 'verification_result', 'repair_history',
        ]
        loaded_sizes = {
            field: self._json_size(getattr(obj, field, None))
            for field in ['test_plan', 'mcp_observations', 'generated_case', 'generated_script', 'repair_history']
        }
        return {
            'lazy': True,
            'fields': {
                field: {
                    'loaded': False,
                    'approx_bytes': loaded_sizes.get(field),
                }
                for field in heavy_fields
            },
        }
