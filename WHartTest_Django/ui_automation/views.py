# -*- coding: utf-8 -*-
"""UI 自动化视图"""

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import quote as urlquote

from asgiref.sync import async_to_sync
from django.conf import settings
from django.http import FileResponse
from django.urls import reverse
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db.models.deletion import ProtectedError
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone

from .models import (
    UiModule, UiPage, UiElement, UiPageSteps, UiPageStepsDetailed,
    UiTestCase, UiCaseStepsDetailed, UiExecutionRecord, UiPublicData, UiEnvironmentConfig,
    UiBatchExecutionRecord, UiElementMap, UiAiGenerationTask
)
from .serializers import (
    UiModuleSerializer, UiPageSerializer, UiPageDetailSerializer,
    UiElementSerializer, UiPageStepsSerializer, UiPageStepsListSerializer, UiPageStepsDetailSerializer,
    UiPageStepsDetailedSerializer, UiTestCaseSerializer, UiTestCaseListSerializer, UiTestCaseDetailSerializer,
    UiCaseStepsDetailedSerializer, UiExecutionRecordSerializer, UiExecutionRecordListSerializer,
    UiPublicDataSerializer, UiEnvironmentConfigSerializer, UiTestCaseExecuteSerializer,
    UiPageStepsExecuteSerializer, UiBatchExecutionRecordSerializer, UiBatchExecutionRecordDetailSerializer,
    UiElementMapSerializer, UiAiGenerationTaskSerializer, UiAiGenerationTaskListSerializer,
    UiAiGenerationTaskDetailSerializer
)
from .ai_planning import (
    build_llm_ui_generation_plan,
    build_llm_ui_repair_patch,
    classify_task_authentication_contract,
    _normalize_execution_mode,
)
from .planning_contract import build_requirement_document
from .element_map_governance import annotate_map_json

logger = logging.getLogger(__name__)
_LLM_PLAN_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ai-ui-llm-plan')


def _default_ai_safety_policy() -> dict:
    return {
        'url_allowlist': [],
        'url_blocklist': [],
        'dangerous_action_keywords': [],
        'require_confirmation_keywords': [],
        'max_steps': 50,
        'max_pages': 20,
        'max_repair_rounds': 2,
        'allow_form_submit': True,
        'allow_destructive_actions': False,
        'headless': True,
    }


def _merge_safety_policy(raw_policy: dict | None) -> dict:
    policy = _default_ai_safety_policy()
    if isinstance(raw_policy, dict):
        for key, value in raw_policy.items():
            if value not in (None, ''):
                policy[key] = value
    return policy


def _build_ws_url(request) -> str:
    scheme = 'wss' if request.is_secure() else 'ws'
    host = request.get_host()
    return f'{scheme}://{host}/ws/ui/actuator/'


def _build_api_url(request) -> str:
    return request.build_absolute_uri('/').rstrip('/')


def _build_actuator_install_steps(api_url: str, ws_url: str, registration_token_required: bool) -> list[dict[str, str]]:
    steps = [
        {
            'title': '下载执行器',
            'content': '选择与你的操作系统匹配的安装包；如果平台未配置下载包，也可以直接使用执行器目录中的打包产物。',
        },
        {
            'title': '解压或安装',
            'content': 'Windows 和 macOS 双击对应安装包完成安装；macOS 安装完成后会尝试自动启动执行器。Linux 使用对应的发布包或源码目录。',
        },
        {
            'title': '修改 config.toml',
            'content': f'把 api_url 配成 {api_url}，ws_url 配成 {ws_url}，并保持 headless=false 以便人工采集窗口在桌面打开。',
        },
        {
            'title': '配置接入令牌',
            'content': '如果平台开启了注册令牌校验，请把 registration_token 写入执行器配置。',
        },
        {
            'title': '启动执行器',
            'content': '执行器启动后会主动连回平台，平台页面会显示在线状态和最近心跳时间。',
        },
    ]
    if not registration_token_required:
        steps.insert(3, {
            'title': '可选令牌',
            'content': '当前环境没有强制注册令牌，但生产环境建议启用。',
        })
    return steps


def _build_actuator_config_template(api_url: str, ws_url: str, registration_token: str, heartbeat_interval: int) -> str:
    token_line = f'registration_token = "{registration_token}"' if registration_token else '# registration_token = "replace-with-your-token"'
    return "\n".join([
        '[server]',
        f'ws_url = "{ws_url}"',
        f'api_url = "{api_url}"',
        'use_gui = false',
        'api_username = "admin"',
        'api_password = "admin123456"',
        token_line,
        f'heartbeat_interval = {heartbeat_interval}',
        '',
        '[actuator]',
        'name = "WHartTest-001"',
        '',
        '[browser]',
        'browser_type = "chromium"',
        'headless = false',
        'persistent = true',
        'user_data_dir = "./data/browser"',
        'launch_timeout = 30',
        'action_timeout = 10',
        'ignore_https_errors = true',
    ])


class UiModuleViewSet(viewsets.ModelViewSet):
    """模块管理视图"""
    queryset = UiModule.objects.select_related('project', 'parent', 'creator')
    serializer_class = UiModuleSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['project', 'parent', 'level']
    search_fields = ['name']
    ordering_fields = ['name', 'level', 'order', 'created_at']
    ordering = ['level', 'order', 'id']

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            self.perform_destroy(instance)
        except ProtectedError:
            return Response(
                {'error': '存在关联，无法删除。请先解除关联'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['get'])
    def tree(self, request):
        """获取模块树形结构"""
        project_id = request.query_params.get('project')
        if not project_id:
            return Response({'error': 'project 参数必填'}, status=status.HTTP_400_BAD_REQUEST)
        modules = UiModule.objects.filter(project_id=project_id, parent__isnull=True)
        serializer = self.get_serializer(modules, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def move(self, request, pk=None):
        """
        移动模块：支持移动到另一个模块的之前、之后或作为其子模块。
        """
        from django.db.models import Max

        instance = self.get_object()
        project_id = instance.project_id
        target_id = request.data.get("target_id")
        drop_position = request.data.get("drop_position")  # -1 (before), 1 (after), 0 (inside)

        if drop_position is None:
            return Response(
                {"error": "参数 drop_position 必填。"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            drop_position = int(drop_position)
            if drop_position not in [-1, 0, 1]:
                raise ValueError()
        except (TypeError, ValueError):
            return Response(
                {"error": "参数 drop_position 必须为 -1、0 或 1。"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            # 如果 target_id 为 None，说明移动到根节点层级
            if target_id is None:
                if drop_position == 0:
                    return Response(
                        {"error": "无法将模块拖入空位置中。"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                instance.parent = None
                instance.level = 1
                instance.save()

                # 重新排序根节点模块
                root_modules = UiModule.objects.filter(
                    project_id=project_id, parent=None
                ).exclude(id=instance.id).order_by("order", "id")

                reordered = list(root_modules)
                reordered.append(instance)

                for index, m in enumerate(reordered, start=1):
                    m.order = index
                    m.save(update_fields=["order"])

                serializer = self.get_serializer(instance)
                return Response(serializer.data)

            # 如果 target_id 不为 None
            try:
                target_module = UiModule.objects.get(
                    id=target_id, project_id=project_id
                )
            except UiModule.DoesNotExist:
                return Response(
                    {"error": "目标模块不存在。"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # 循环引用校验：目标模块不能是自己或自己的子模块
            descendant_ids = instance.get_all_descendant_ids()
            if target_module.id in descendant_ids:
                return Response(
                    {"error": "无法移动模块到自身或其子模块下。"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if drop_position == 0:
                # 移动到目标模块内部，作为其子模块
                if target_module.level >= 5:
                    return Response(
                        {"error": "模块级别不能超过5级。"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                # 校验子树最大深度
                subtree_depth = instance.get_max_depth()
                if target_module.level + subtree_depth > 5:
                    return Response(
                        {"error": f"移动后模块层级将超过5级限制（当前子树深度: {subtree_depth}）。"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                instance.parent = target_module
                instance.level = target_module.level + 1

                # 获取目标模块下已有子模块的最大 order
                max_order = UiModule.objects.filter(
                    parent=target_module
                ).aggregate(Max("order"))["order__max"] or 0

                instance.order = max_order + 1
                instance.save()

            else:
                # 移动到目标模块的前面或后面，成为同级模块
                parent = target_module.parent

                # 校验子树最大深度
                target_parent_level = target_module.parent.level if target_module.parent else 0
                subtree_depth = instance.get_max_depth()
                if target_parent_level + subtree_depth > 5:
                    return Response(
                        {"error": f"移动后模块层级将超过5级限制（当前子树深度: {subtree_depth}）。"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                instance.parent = parent
                instance.level = target_module.level
                instance.save()

                # 重新排序所有同级模块
                siblings = UiModule.objects.filter(
                    project_id=project_id, parent=parent
                ).exclude(id=instance.id).order_by("order", "id")

                reordered = []
                for s in siblings:
                    if s.id == target_module.id and drop_position == -1:
                        reordered.append(instance)
                        reordered.append(s)
                    elif s.id == target_module.id and drop_position == 1:
                        reordered.append(s)
                        reordered.append(instance)
                    else:
                        reordered.append(s)

                # 防御，如果目标模块没在 siblings 里（理论上不可能）
                if instance not in reordered:
                    reordered.append(instance)

                for index, m in enumerate(reordered, start=1):
                    m.order = index
                    m.save(update_fields=["order"])

            serializer = self.get_serializer(instance)
            return Response(serializer.data)


class UiPageViewSet(viewsets.ModelViewSet):
    """页面管理视图"""
    queryset = UiPage.objects.select_related('project', 'module', 'creator')
    serializer_class = UiPageSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['project', 'module']
    search_fields = ['name', 'url']
    ordering_fields = ['name', 'created_at']
    ordering = ['-id']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return UiPageDetailSerializer
        return UiPageSerializer

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            self.perform_destroy(instance)
        except ProtectedError:
            return Response(
                {'error': '存在关联，无法删除。请先解除关联'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class UiElementViewSet(viewsets.ModelViewSet):
    """元素管理视图"""
    queryset = UiElement.objects.select_related('page', 'creator')
    serializer_class = UiElementSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['page', 'locator_type', 'is_iframe']
    search_fields = ['name', 'locator_value']
    ordering_fields = ['name', 'created_at']
    ordering = ['-id']

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)


class UiPageStepsViewSet(viewsets.ModelViewSet):
    """页面步骤管理视图"""
    queryset = UiPageSteps.objects.select_related('project', 'page', 'module', 'creator')
    serializer_class = UiPageStepsSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['project', 'page', 'module', 'status']
    search_fields = ['name']
    ordering_fields = ['name', 'created_at']
    ordering = ['-id']

    def get_queryset(self):
        """列表查询时排除大字段"""
        queryset = super().get_queryset()
        if self.action == 'list':
            return queryset.defer('result_data', 'flow_data', 'run_flow', 'description')
        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return UiPageStepsListSerializer
        if self.action == 'retrieve':
            return UiPageStepsDetailSerializer
        if self.action == 'execute_data':
            return UiPageStepsExecuteSerializer
        return UiPageStepsSerializer

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            self.perform_destroy(instance)
        except ProtectedError:
            return Response(
                {'error': '存在关联，无法删除。请先解除关联'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'], url_path='execute-data')
    def execute_data(self, request, pk=None):
        """获取页面步骤执行数据（包含元素定位信息）"""
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)


class UiPageStepsDetailedViewSet(viewsets.ModelViewSet):
    """步骤详情管理视图"""
    queryset = UiPageStepsDetailed.objects.select_related('page_step', 'element')
    serializer_class = UiPageStepsDetailedSerializer
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['page_step', 'step_type']
    ordering_fields = ['step_sort', 'created_at']
    ordering = ['page_step', 'step_sort']

    @action(detail=False, methods=['post'])
    def batch_update(self, request):
        """批量更新步骤详情"""
        page_step_id = request.data.get('page_step')
        steps = request.data.get('steps', [])
        if not page_step_id:
            return Response({'error': 'page_step 参数必填'}, status=status.HTTP_400_BAD_REQUEST)
        # 删除旧步骤，创建新步骤
        UiPageStepsDetailed.objects.filter(page_step_id=page_step_id).delete()
        for idx, step_data in enumerate(steps):
            step_data['page_step'] = page_step_id
            step_data['step_sort'] = idx
            # 兼容 element_id 和 element 两种参数名
            if 'element_id' in step_data and 'element' not in step_data:
                step_data['element'] = step_data.pop('element_id')
            serializer = self.get_serializer(data=step_data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        return Response({'message': '批量更新成功'})


class UiTestCaseViewSet(viewsets.ModelViewSet):
    """测试用例管理视图"""
    queryset = UiTestCase.objects.select_related('project', 'module', 'creator')
    serializer_class = UiTestCaseSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['project', 'module', 'level', 'status']
    search_fields = ['name']
    ordering_fields = ['name', 'level', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """列表查询时排除大字段"""
        queryset = super().get_queryset()
        if self.action == 'list':
            return queryset.defer(
                'result_data', 'front_custom', 'front_sql', 'posterior_sql',
                'parametrize', 'case_flow', 'error_message', 'description'
            )
        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return UiTestCaseListSerializer
        if self.action == 'retrieve':
            return UiTestCaseDetailSerializer
        if self.action == 'execute_data':
            return UiTestCaseExecuteSerializer
        return UiTestCaseSerializer

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)

    @action(detail=True, methods=['get'], url_path='execute-data')
    def execute_data(self, request, pk=None):
        """获取测试用例执行数据（包含完整的步骤详情）"""
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = dict(serializer.data)
        result_data = data.get('result_data') if isinstance(data.get('result_data'), dict) else {}
        artifact = result_data.get('ai_execution_artifact') if isinstance(result_data.get('ai_execution_artifact'), dict) else {}
        if not artifact:
            source_task_id = result_data.get('ai_generation_task_id')
            if not source_task_id:
                for case_step in instance.case_steps.select_related('page_step').all().order_by('case_sort'):
                    flow_data = case_step.page_step.flow_data if isinstance(case_step.page_step.flow_data, dict) else {}
                    if flow_data.get('source') == 'ai_generation_task' and flow_data.get('task_id'):
                        source_task_id = flow_data.get('task_id')
                        break
            if source_task_id:
                task = UiAiGenerationTask.objects.filter(id=source_task_id, project_id=instance.project_id).first()
                if task:
                    artifact = UiAiGenerationTaskViewSet._build_ai_execution_artifact(task)
                    if artifact:
                        result_data = {
                            **result_data,
                            'source': 'ai_generation_task',
                            'ai_generation_task_id': task.id,
                            'ai_execution_mode': 'typescript_spec',
                            'ai_execution_artifact': artifact,
                        }
                        data['result_data'] = result_data
        return Response(data)

    @action(detail=True, methods=['post'], url_path='execute')
    def execute(self, request, pk=None):
        """启动测试用例执行，并立即持久化执行中状态。"""
        from .consumers import SocketUserManager
        from .socket_models import SocketDataModel, QueueModel, NoticeType, ResponseCode, UiSocketEnum

        instance = self.get_object()
        actuator_id = request.data.get('actuator_id')
        env_config_id = request.data.get('env_config_id')
        actuator = (
            SocketUserManager.get_actuator_by_id(actuator_id)
            if actuator_id else SocketUserManager.get_actuator()
        )
        if not actuator:
            return Response(
                {'error': f'执行器 {actuator_id} 不在线' if actuator_id else '没有可用的执行器'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        with transaction.atomic():
            record = UiExecutionRecord.objects.create(
                test_case=instance,
                executor=request.user,
                status=1,
                trigger_type='manual',
                environment={'env_config_id': env_config_id} if env_config_id else None,
                start_time=timezone.now(),
            )
            instance.status = 1
            instance.result_data = {'last_execution': record.id, 'steps': []}
            instance.error_message = None
            instance.save(update_fields=['status', 'result_data', 'error_message', 'updated_at'])

        args = {
            'case_id': instance.id,
            'env_config_id': env_config_id,
            'actuator_id': actuator_id,
            'executor_id': request.user.id,
            'executor_name': request.user.username,
            'execution_record_id': record.id,
        }
        try:
            async_to_sync(actuator.send_json)(SocketDataModel(
                code=ResponseCode.SUCCESS,
                msg='execute',
                user=request.user.username,
                is_notice=NoticeType.ACTUATOR,
                data=QueueModel(
                    func_name=UiSocketEnum.TEST_CASE,
                    func_args=args,
                ),
            ))
        except Exception as exc:
            record.status = 3
            record.error_message = f'发送执行任务失败: {exc}'
            record.end_time = timezone.now()
            record.save(update_fields=['status', 'error_message', 'end_time'])
            instance.status = 3
            instance.error_message = record.error_message
            instance.save(update_fields=['status', 'error_message', 'updated_at'])
            logger.error("发送 UI 测试用例执行任务失败: case_id=%s", instance.id, exc_info=True)
            return Response({'error': record.error_message}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            'message': '任务已发送给执行器',
            'record_id': record.id,
            'test_case_id': instance.id,
            'status': 1,
        })

    @action(detail=False, methods=['post'], url_path='batch-delete')
    def batch_delete(self, request, **kwargs):
        """
        批量删除UI自动化测试用例
        POST请求体格式: {"ids": [1, 2, 3, 4]}
        """
        # 获取要删除的用例ID列表
        ids_data = request.data.get('ids', [])

        if not ids_data:
            return Response(
                {'error': '请提供要删除的用例ID列表'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 验证ID格式
        try:
            testcase_ids = [int(id) for id in ids_data]
        except (ValueError, TypeError):
            return Response(
                {'error': 'ids参数格式错误，应为数字列表'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not testcase_ids:
            return Response(
                {'error': '用例ID列表不能为空'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 获取当前查询集，确保数据隔离
        queryset = self.get_queryset()

        # 过滤出要删除的用例
        testcases_to_delete = queryset.filter(id__in=testcase_ids)

        # 检查是否所有请求的ID都存在
        found_ids = list(testcases_to_delete.values_list('id', flat=True))
        not_found_ids = [id for id in testcase_ids if id not in found_ids]

        if not_found_ids:
            return Response(
                {
                    'error': f'以下用例ID不存在: {not_found_ids}',
                    'not_found_ids': not_found_ids
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # 记录删除前的信息用于返回
        deleted_testcases_info = []
        for testcase in testcases_to_delete:
            deleted_testcases_info.append({
                'id': testcase.id,
                'name': testcase.name,
                'module': testcase.module.name if testcase.module else None
            })

        # 执行批量删除
        try:
            with transaction.atomic():
                # 删除用例（关联的步骤会因为外键级联删除而自动删除）
                deleted_count, deleted_details = testcases_to_delete.delete()

                return Response({
                    'message': f'成功删除 {len(deleted_testcases_info)} 个UI自动化测试用例',
                    'deleted_count': len(deleted_testcases_info),
                    'deleted_testcases': deleted_testcases_info,
                    'deletion_details': deleted_details
                }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': f'删除过程中发生错误: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class UiCaseStepsDetailedViewSet(viewsets.ModelViewSet):
    """用例步骤管理视图"""
    queryset = UiCaseStepsDetailed.objects.select_related('test_case', 'page_step')
    serializer_class = UiCaseStepsDetailedSerializer
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['test_case', 'status']
    ordering_fields = ['case_sort', 'created_at']
    ordering = ['test_case', 'case_sort']

    @action(detail=False, methods=['post'])
    def batch_update(self, request):
        """批量更新用例步骤"""
        test_case_id = request.data.get('test_case')
        steps = request.data.get('steps', [])
        if not test_case_id:
            return Response({'error': 'test_case 参数必填'}, status=status.HTTP_400_BAD_REQUEST)
        # 删除旧步骤，创建新步骤
        UiCaseStepsDetailed.objects.filter(test_case_id=test_case_id).delete()
        for idx, step_data in enumerate(steps):
            step_data['test_case'] = test_case_id
            step_data['case_sort'] = idx
            serializer = self.get_serializer(data=step_data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        return Response({'message': '批量更新成功'})


class UiExecutionRecordViewSet(viewsets.ModelViewSet):
    """执行记录管理视图"""
    queryset = UiExecutionRecord.objects.select_related('test_case', 'executor')
    serializer_class = UiExecutionRecordSerializer
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = {'test_case': ['exact'], 'status': ['exact'], 'trigger_type': ['exact'], 'test_case__project': ['exact']}
    ordering_fields = ['created_at', 'duration']
    ordering = ['-created_at']

    def get_queryset(self):
        """列表查询时排除大字段，支持 project 参数过滤"""
        queryset = super().get_queryset()
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(test_case__project_id=project_id)
        if self.action == 'list':
            return queryset.defer(
                'step_results', 'screenshots', 'trace_data', 'log',
                'error_message', 'environment'
            )
        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return UiExecutionRecordListSerializer
        return UiExecutionRecordSerializer

    def perform_create(self, serializer):
        serializer.save(executor=self.request.user)

    def perform_destroy(self, instance):
        """删除执行记录及其关联文件"""
        import os
        from django.conf import settings

        def safe_delete(path):
            if not path:
                return
            full_path = path if os.path.isabs(path) else os.path.join(settings.MEDIA_ROOT, path.lstrip('/'))
            if os.path.exists(full_path):
                os.remove(full_path)

        # 删除截图
        for screenshot in instance.screenshots or []:
            if isinstance(screenshot, str):
                safe_delete(screenshot.replace(settings.MEDIA_URL, ''))

        # 删除视频
        safe_delete(instance.video_path)

        # 删除 Trace 文件
        safe_delete(instance.trace_path)

        instance.delete()

    @action(detail=True, methods=['get'], url_path='trace')
    def get_trace_data(self, request, pk=None):
        """获取执行记录的 Trace 数据

        如果 trace_data 已解析则直接返回，否则尝试解析 trace_path
        可通过 ?refresh=1 强制重新解析
        """
        instance = self.get_object()
        refresh = request.query_params.get('refresh', '').lower() in ('1', 'true')

        # 如果已有解析数据且不需要刷新，直接返回
        if instance.trace_data and not refresh:
            return Response({
                'status': 'success',
                'data': instance.trace_data
            })

        # 尝试解析 trace 文件
        if not instance.trace_path:
            return Response({
                'status': 'error',
                'message': '此执行记录没有 Trace 数据'
            }, status=status.HTTP_404_NOT_FOUND)

        from .trace_parser import parse_trace_file
        import os
        from django.conf import settings

        # 构建完整路径
        trace_path = instance.trace_path
        if not os.path.isabs(trace_path):
            trace_path = os.path.join(settings.MEDIA_ROOT, trace_path)

        trace_data = parse_trace_file(trace_path)
        if not trace_data:
            return Response({
                'status': 'error',
                'message': 'Trace 文件解析失败'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # 保存解析结果
        instance.trace_data = trace_data
        instance.save(update_fields=['trace_data'])

        return Response({
            'status': 'success',
            'data': trace_data
        })


class UiPublicDataViewSet(viewsets.ModelViewSet):
    """公共数据管理视图"""
    queryset = UiPublicData.objects.select_related('project', 'creator')
    serializer_class = UiPublicDataSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['project', 'type', 'is_enabled']
    search_fields = ['key']
    ordering_fields = ['key', 'created_at']
    ordering = ['project', 'key']

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)

    @action(detail=False, methods=['get'], url_path='by-project/(?P<project_id>[^/.]+)')
    def by_project(self, request, project_id=None):
        """获取指定项目的所有启用公共数据（供执行器使用）

        返回格式（经 UnifiedResponseRenderer 包装后）:
        {"status": "success", "code": 200, "data": [{"key": "username", "value": "admin", "type": 0}, ...]}
        """
        public_data = UiPublicData.objects.filter(
            project_id=project_id,
            is_enabled=True
        ).values('key', 'value', 'type')
        # 直接返回列表，由 UnifiedResponseRenderer 统一包装为标准格式
        return Response(list(public_data))


class UiEnvironmentConfigViewSet(viewsets.ModelViewSet):
    """环境配置管理视图"""
    queryset = UiEnvironmentConfig.objects.select_related('project', 'creator')
    serializer_class = UiEnvironmentConfigSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['project', 'browser', 'headless', 'is_default']
    search_fields = ['name', 'base_url']
    ordering_fields = ['name', 'created_at']
    ordering = ['project', 'name']

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)


class ActuatorViewSet(viewsets.ViewSet):
    """执行器管理视图"""
    permission_classes = []  # 公开访问，不需要特殊权限

    @staticmethod
    def _parse_iso_datetime(value: str | None):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _build_package_item(platform: str, url: str, sha256: str, file_name: str) -> dict:
        return {
            'platform': platform,
            'url': url,
            'sha256': sha256,
            'file_name': file_name,
            'available': bool(url),
        }

    @action(detail=False, methods=['get'])
    def list_actuators(self, request):
        """获取所有在线执行器列表"""
        from .consumers import SocketUserManager

        actuators = [
            SocketUserManager.describe_actuator(actuator_id, consumer)
            for actuator_id, consumer in SocketUserManager._actuator_users.items()
        ]

        return Response({
            'status': 'success',
            'data': {
                'count': len(actuators),
                'items': actuators
            }
        })

    @action(detail=False, methods=['get'])
    def status(self, request):
        """获取执行器状态统计"""
        from .consumers import SocketUserManager

        return Response({
            'status': 'success',
            'data': {
                'total_actuators': SocketUserManager.get_actuator_count(),
                'available_actuators': SocketUserManager.get_available_actuator_count(),
                'has_available': SocketUserManager.has_actuator(),
                'web_users': len(SocketUserManager._web_users),
            }
        })

    @staticmethod
    def _package_download_fallback_url(request, platform: str) -> str:
        return request.build_absolute_uri(
            reverse('ui-actuators-download-package', kwargs={'platform': platform})
        )

    @staticmethod
    def _package_source_dir() -> Path:
        candidates = [
            Path(__file__).resolve().parents[2] / 'WHartTest_Actuator',
            Path(__file__).resolve().parents[1] / 'WHartTest_Actuator',
            Path('/app/WHartTest_Actuator'),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    @staticmethod
    def _package_dist_dir() -> Path:
        configured_dir = str(getattr(settings, 'UI_ACTUATOR_PACKAGE_DIR', '') or '').strip()
        if configured_dir:
            return Path(configured_dir)
        return ActuatorViewSet._package_source_dir() / 'dist'

    @staticmethod
    def _package_installer_path(platform: str) -> Path | None:
        if platform != 'windows':
            return None
        configured_name = str(
            getattr(settings, 'UI_ACTUATOR_PACKAGE_WINDOWS_FILE_NAME', '')
            or 'WHartTest_Actuator_Installer.exe'
        ).strip()
        candidates = [
            ActuatorViewSet._package_dist_dir() / configured_name,
            ActuatorViewSet._package_dist_dir() / 'WHartTest_Actuator_Installer.exe',
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _package_archive_path(platform: str) -> Path | None:
        if platform != 'macos':
            return None
        configured_name = str(
            getattr(settings, 'UI_ACTUATOR_PACKAGE_MACOS_FILE_NAME', '')
            or 'WHartTest_Actuator_MacOS.pkg'
        ).strip()
        candidates = []
        for name in (
            configured_name,
            'WHartTest_Actuator_MacOS.pkg',
            'WHartTest_Actuator_MacOS.zip',
        ):
            candidate = ActuatorViewSet._package_dist_dir() / name
            if candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _package_release_dir(platform: str) -> Path | None:
        dist_dir = ActuatorViewSet._package_dist_dir()
        candidates = []
        if platform == 'windows':
            candidates.append(dist_dir / 'WHartTest_Actuator')
            marker = 'WHartTest_Actuator.exe'
        elif platform == 'macos':
            candidates.extend([
                dist_dir / 'WHartTest_Actuator_MacOS',
                dist_dir / 'macos' / 'WHartTest_Actuator_MacOS',
            ])
            marker = 'WHartTest_Actuator.app'
        else:
            return None

        for release_dir in candidates:
            if (release_dir / marker).exists():
                return release_dir
        return None

    @staticmethod
    def _package_file_name(platform: str) -> str:
        if platform == 'windows':
            installer_path = ActuatorViewSet._package_installer_path(platform)
            if installer_path is not None:
                return installer_path.name
            return 'WHartTest_Actuator.zip'
        if platform == 'macos':
            archive_path = ActuatorViewSet._package_archive_path(platform)
            if archive_path is not None:
                return archive_path.name
            if ActuatorViewSet._package_release_dir(platform) is not None:
                return 'WHartTest_Actuator_MacOS.zip'
            return str(
                getattr(settings, 'UI_ACTUATOR_PACKAGE_MACOS_FILE_NAME', '')
                or 'WHartTest_Actuator_MacOS.pkg'
            ).strip()
        if platform == 'linux':
            return 'WHartTest_Actuator.tar.gz'
        return 'WHartTest_Actuator.zip'

    @staticmethod
    def _package_is_available(platform: str) -> bool:
        return (
            ActuatorViewSet._package_installer_path(platform) is not None
            or ActuatorViewSet._package_archive_path(platform) is not None
            or ActuatorViewSet._package_release_dir(platform) is not None
        )

    @staticmethod
    def _archive_directory_response(root_dir: Path, archive_name: str, content_type: str):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in root_dir.rglob('*'):
                if not file_path.is_file():
                    continue
                arcname = file_path.relative_to(root_dir.parent)
                zf.write(file_path, arcname=str(arcname))
        buffer.seek(0)
        response = FileResponse(buffer, as_attachment=True, filename=archive_name)
        response['Content-Type'] = content_type
        return response

    @staticmethod
    def _should_skip_package_path(path: Path) -> bool:
        excluded_parts = {
            '.git',
            '.agents',
            '.codex',
            '.venv',
            '__pycache__',
            'build',
            'dist',
            '.pytest_cache',
            '.mypy_cache',
            '.ruff_cache',
            '.playwright-mcp',
            'node_modules',
            'data',
        }
        return any(part in excluded_parts for part in path.parts)

    @action(detail=False, methods=['post'])
    def keep_latest(self, request):
        """仅保留最近连接的执行器，关闭其余执行器。"""
        from .consumers import SocketUserManager

        keep_actuator_id = str(request.data.get('actuator_id') or '').strip()
        actuators = list(SocketUserManager._actuator_users.items())
        if not actuators:
            return Response({
                'status': 'success',
                'data': {
                    'kept_actuator_id': '',
                    'closed_actuator_ids': [],
                    'count': 0,
                }
            })

        if keep_actuator_id and keep_actuator_id in SocketUserManager._actuator_users:
            keep_id = keep_actuator_id
        else:
            latest_item = max(
                actuators,
                key=lambda item: (
                    self._parse_iso_datetime(getattr(item[1], 'actuator_info', {}).get('connected_at'))
                    or datetime.min.replace(tzinfo=dt_timezone.utc),
                    getattr(item[1], 'user_id', ''),
                ),
            )
            keep_id = latest_item[0]

        closed_ids: list[str] = []
        for actuator_id, consumer in actuators:
            if actuator_id == keep_id:
                continue
            try:
                async_to_sync(consumer.close)(4000)
                SocketUserManager.remove_actuator(actuator_id)
                closed_ids.append(actuator_id)
            except Exception as exc:
                logger.warning("关闭执行器失败: %s, err=%s", actuator_id, exc)
                SocketUserManager.remove_actuator(actuator_id)
                closed_ids.append(actuator_id)

        return Response({
            'status': 'success',
            'data': {
                'kept_actuator_id': keep_id,
                'closed_actuator_ids': closed_ids,
                'count': SocketUserManager.get_actuator_count(),
            }
        })

    @action(detail=False, methods=['get'], url_path=r'download-package/(?P<platform>[^/.]+)')
    def download_package(self, request, platform: str):
        """下载执行器安装包。"""
        normalized_platform = (platform or '').strip().lower()
        if normalized_platform not in {'windows', 'linux', 'macos'}:
            return Response({'error': '不支持的平台类型'}, status=status.HTTP_400_BAD_REQUEST)

        if normalized_platform == 'windows':
            installer_path = self._package_installer_path(normalized_platform)
            if installer_path is not None:
                response = FileResponse(
                    installer_path.open('rb'),
                    as_attachment=True,
                    filename=installer_path.name,
                )
                response['Content-Type'] = 'application/vnd.microsoft.portable-executable'
                return response

            release_dir = self._package_release_dir(normalized_platform)
            if release_dir is not None:
                return self._archive_directory_response(
                    release_dir,
                    'WHartTest_Actuator.zip',
                    'application/zip',
                )

            return Response(
                {'error': 'Windows 执行器安装包尚未发布，请配置下载源或先完成 Windows 打包'},
                status=status.HTTP_404_NOT_FOUND,
            )

        archive_path = self._package_archive_path(normalized_platform)
        if archive_path is not None:
            content_type = (
                'application/zip'
                if archive_path.suffix.lower() == '.zip'
                else 'application/octet-stream'
            )
            response = FileResponse(
                archive_path.open('rb'),
                as_attachment=True,
                filename=archive_path.name,
            )
            response['Content-Type'] = content_type
            return response

        archive_name = self._package_file_name(normalized_platform)
        release_dir = self._package_release_dir(normalized_platform)
        if release_dir is not None:
            return self._archive_directory_response(release_dir, archive_name, 'application/zip')

        source_dir = self._package_source_dir()
        if not source_dir.exists():
            return Response({'error': '执行器源码目录不存在，无法生成安装包'}, status=status.HTTP_404_NOT_FOUND)

        if normalized_platform in {'windows', 'macos'}:
            import io
            import zipfile

            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                for file_path in source_dir.rglob('*'):
                    if not file_path.is_file() or self._should_skip_package_path(file_path):
                        continue
                    arcname = file_path.relative_to(source_dir.parent)
                    zf.write(file_path, arcname=str(arcname))
            buffer.seek(0)
            response = FileResponse(buffer, as_attachment=True, filename=archive_name)
            response['Content-Type'] = 'application/zip'
            return response

        import io
        import tarfile

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tf:
            for file_path in source_dir.rglob('*'):
                if not file_path.is_file() or self._should_skip_package_path(file_path):
                    continue
                arcname = file_path.relative_to(source_dir.parent)
                tf.add(file_path, arcname=str(arcname))
        buffer.seek(0)
        response = FileResponse(buffer, as_attachment=True, filename=archive_name)
        response['Content-Type'] = 'application/gzip'
        return response

    @action(detail=False, methods=['get'])
    def onboarding(self, request):
        """获取执行器下载与接入指引。"""
        api_url = _build_api_url(request)
        ws_url = _build_ws_url(request)
        registration_token = str(getattr(settings, 'UI_ACTUATOR_REGISTRATION_TOKEN', '') or '').strip()
        heartbeat_interval = int(getattr(settings, 'UI_ACTUATOR_HEARTBEAT_INTERVAL_SECONDS', 30))

        packages = [
            self._build_package_item(
                'windows',
                str(getattr(settings, 'UI_ACTUATOR_PACKAGE_WINDOWS_URL', '') or '').strip(),
                str(getattr(settings, 'UI_ACTUATOR_PACKAGE_WINDOWS_SHA256', '') or '').strip(),
                str(
                    getattr(settings, 'UI_ACTUATOR_PACKAGE_WINDOWS_FILE_NAME', '')
                    or self._package_file_name('windows')
                ).strip(),
            ),
            self._build_package_item(
                'linux',
                str(getattr(settings, 'UI_ACTUATOR_PACKAGE_LINUX_URL', '') or '').strip(),
                str(getattr(settings, 'UI_ACTUATOR_PACKAGE_LINUX_SHA256', '') or '').strip(),
                'WHartTest_Actuator.tar.gz',
            ),
            self._build_package_item(
                'macos',
                str(getattr(settings, 'UI_ACTUATOR_PACKAGE_MACOS_URL', '') or '').strip(),
                str(getattr(settings, 'UI_ACTUATOR_PACKAGE_MACOS_SHA256', '') or '').strip(),
                self._package_file_name('macos'),
            ),
        ]

        for package in packages:
            if not package['url']:
                if self._package_is_available(package['platform']):
                    package['url'] = self._package_download_fallback_url(request, package['platform'])
                    package['available'] = True

        return Response({
            'status': 'success',
            'data': {
                'api_url': api_url,
                'ws_url': ws_url,
                'registration_token_required': bool(getattr(settings, 'UI_ACTUATOR_REGISTRATION_TOKEN_REQUIRED', False)),
                'registration_token': registration_token,
                'online_ttl_seconds': int(getattr(settings, 'UI_ACTUATOR_ONLINE_TTL_SECONDS', 90)),
                'heartbeat_interval_seconds': heartbeat_interval,
                'packages': packages,
                'install_steps': _build_actuator_install_steps(
                    api_url,
                    ws_url,
                    bool(getattr(settings, 'UI_ACTUATOR_REGISTRATION_TOKEN_REQUIRED', False)),
                ),
                'config_template': _build_actuator_config_template(
                    api_url,
                    ws_url,
                    registration_token,
                    heartbeat_interval,
                ),
                'notes': [
                    '执行器必须主动出站连接平台，不需要平台开放入站端口。',
                    '人工采集要求 headless=false，窗口会在用户桌面会话中打开。',
                    '如果平台开启了注册令牌，请把配置中的 token 与平台保持一致。',
                ],
            }
        })


class UiBatchExecutionRecordViewSet(viewsets.ModelViewSet):
    """批量执行记录管理视图"""
    queryset = UiBatchExecutionRecord.objects.select_related('executor')
    serializer_class = UiBatchExecutionRecordSerializer
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['status', 'trigger_type']
    ordering_fields = ['created_at', 'duration', 'total_cases']
    ordering = ['-created_at']

    def get_queryset(self):
        """列表查询时不预加载执行记录，支持 project 参数过滤"""
        queryset = super().get_queryset()
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(execution_records__test_case__project_id=project_id).distinct()
        # 详情时预加载执行记录
        if self.action == 'retrieve':
            queryset = queryset.prefetch_related('execution_records', 'execution_records__test_case')
        return queryset

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return UiBatchExecutionRecordDetailSerializer
        return UiBatchExecutionRecordSerializer

    def perform_destroy(self, instance):
        """删除批量执行记录及其关联的执行记录"""
        instance.execution_records.all().delete()
        instance.delete()


class UiElementMapViewSet(viewsets.ModelViewSet):
    """元素地图管理视图"""
    queryset = UiElementMap.objects.select_related('project', 'environment_config', 'creator', 'confirmed_by')
    serializer_class = UiElementMapSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['project', 'environment_config', 'status', 'version_group', 'role_key', 'permission_key', 'stale_status']
    search_fields = ['name', 'base_url', 'role_key', 'permission_key', 'version_group']
    ordering_fields = ['created_at', 'updated_at', 'name', 'version', 'stale_status']
    ordering = ['-updated_at', '-id']

    def perform_create(self, serializer):
        instance = serializer.save(creator=self.request.user)
        self._refresh_governance_fields(instance)

    @staticmethod
    def _refresh_governance_fields(element_map: UiElementMap, previous_json: dict | None = None) -> None:
        annotated = annotate_map_json(
            element_map.map_json if isinstance(element_map.map_json, dict) else {},
            project_id=element_map.project_id,
            environment_config=element_map.environment_config,
            base_url=element_map.base_url or '',
            previous_map_json=previous_json,
        )
        governance = annotated.get('governance') if isinstance(annotated.get('governance'), dict) else {}
        scope = annotated.get('scope') if isinstance(annotated.get('scope'), dict) else {}
        element_map.map_json = annotated
        element_map.version_group = governance.get('version_group') or scope.get('version_group') or element_map.version_group
        element_map.role_key = scope.get('role_key') or element_map.role_key or 'default'
        element_map.permission_key = scope.get('permission_key') or element_map.permission_key or 'default'
        element_map.map_hash = governance.get('map_hash') or element_map.map_hash
        element_map.baseline_hash = governance.get('baseline_hash') or element_map.baseline_hash
        element_map.diff_summary = governance.get('diff_summary') or element_map.diff_summary or {}
        element_map.stale_status = governance.get('stale_status') or element_map.stale_status or 'current'
        element_map.save(update_fields=[
            'map_json', 'version_group', 'role_key', 'permission_key', 'map_hash',
            'baseline_hash', 'diff_summary', 'stale_status', 'updated_at'
        ])

    @action(detail=False, methods=['post'], url_path='manual-capture-start')
    def manual_capture_start(self, request, **kwargs):
        """创建人工采集元素地图任务，并下发给在线执行器。"""
        from asgiref.sync import async_to_sync
        from .consumers import SocketUserManager
        from .socket_models import SocketDataModel, QueueModel, NoticeType, ResponseCode, UiSocketEnum

        project_id = request.data.get('project') or request.data.get('project_id')
        if not project_id:
            return Response({'error': 'project 参数必填'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            return Response({'error': 'project 参数必须是数字'}, status=status.HTTP_400_BAD_REQUEST)

        environment_config = None
        environment_id = request.data.get('environment_config') or request.data.get('environment_config_id')
        if environment_id not in (None, ''):
            try:
                environment_config = UiEnvironmentConfig.objects.get(id=int(environment_id), project_id=project_id)
            except (TypeError, ValueError, UiEnvironmentConfig.DoesNotExist):
                return Response({'error': 'environment_config 不存在或不属于当前项目'}, status=status.HTTP_400_BAD_REQUEST)

        base_url = (
            request.data.get('base_url')
            or request.data.get('target_url')
            or (environment_config.base_url if environment_config else '')
            or ''
        )
        base_url = str(base_url).strip()
        if not base_url:
            return Response({'error': 'base_url 参数必填'}, status=status.HTTP_400_BAD_REQUEST)

        requested_actuator_id = str(request.data.get('actuator_id') or '').strip()
        actuator = SocketUserManager.get_actuator_by_id(requested_actuator_id) if requested_actuator_id else None
        if not actuator and not requested_actuator_id:
            actuator = SocketUserManager.get_actuator()
        if not actuator:
            return Response(
                {'error': f'执行器 {requested_actuator_id} 不在线' if requested_actuator_id else '没有可用的执行器，请先启动执行器服务'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            interval_ms = int(request.data.get('interval_ms') or 1000)
        except (TypeError, ValueError):
            interval_ms = 1000
        try:
            max_duration_seconds = int(request.data.get('max_duration_seconds') or 600)
        except (TypeError, ValueError):
            max_duration_seconds = 600
        interval_ms = max(500, min(interval_ms, 5000))
        max_duration_seconds = max(30, min(max_duration_seconds, 1800))

        now = timezone.now()
        map_name = str(request.data.get('name') or '').strip() or f"人工采集元素地图 {now.strftime('%Y%m%d %H:%M:%S')}"
        element_map = UiElementMap.objects.create(
            project_id=project_id,
            environment_config=environment_config,
            name=map_name[:128],
            base_url=base_url,
            status='draft',
            creator=request.user,
            map_json={
                'base_url': base_url,
                'capture_mode': 'manual_capture',
                'source': 'manual_capture',
                'manual_capture': {
                    'status': 'capturing',
                    'started_at': now.isoformat(),
                    'actuator_id': getattr(actuator, 'user_id', '') or requested_actuator_id,
                    'interval_ms': interval_ms,
                    'max_duration_seconds': max_duration_seconds,
                },
                'pages': [],
                'state_transitions': [],
                'action_trace': [],
                'coverage_summary': {'page_count': 0, 'element_count': 0, 'state_transition_count': 0},
            },
            coverage_summary={'page_count': 0, 'element_count': 0, 'state_transition_count': 0},
        )
        self._refresh_governance_fields(element_map)

        env_payload = UiEnvironmentConfigSerializer(environment_config).data if environment_config else None
        if env_payload is not None:
            env_payload['extra_config'] = (
                environment_config.extra_config if isinstance(environment_config.extra_config, dict) else {}
            )
        dispatch_id = f"manual-map-{element_map.id}-{int(timezone.now().timestamp() * 1000)}"
        args = {
            'element_map_id': element_map.id,
            'dispatch_id': dispatch_id,
            'project_id': project_id,
            'environment_config': env_payload,
            'environment_config_id': environment_config.id if environment_config else None,
            'base_url': base_url,
            'target_url': base_url,
            'name': element_map.name,
            'interval_ms': interval_ms,
            'max_duration_seconds': max_duration_seconds,
            'capture_mode': 'manual_capture',
            'headless': False,
        }
        try:
            async_to_sync(actuator.send_json)(SocketDataModel(
                code=ResponseCode.SUCCESS,
                msg='element_map_manual_capture',
                user=request.user.username,
                is_notice=NoticeType.ACTUATOR,
                data=QueueModel(
                    func_name=UiSocketEnum.ELEMENT_MAP_MANUAL_CAPTURE,
                    func_args=args,
                ),
            ))
        except Exception as exc:
            element_map.stale_status = 'superseded'
            element_map.stale_reason = f'人工采集任务下发失败: {type(exc).__name__}: {str(exc)[:500]}'
            element_map.map_json = {
                **(element_map.map_json if isinstance(element_map.map_json, dict) else {}),
                'manual_capture': {
                    **((element_map.map_json or {}).get('manual_capture') if isinstance(element_map.map_json, dict) and isinstance((element_map.map_json or {}).get('manual_capture'), dict) else {}),
                    'status': 'failed',
                    'error_message': element_map.stale_reason,
                    'finished_at': timezone.now().isoformat(),
                },
            }
            element_map.save(update_fields=['map_json', 'stale_status', 'stale_reason', 'updated_at'])
            return Response({'error': element_map.stale_reason}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({
            'message': '人工元素地图采集任务已发送给执行器，请在弹出的浏览器中完成业务操作后关闭窗口或等待采集结束',
            'dispatch_id': dispatch_id,
            'element_map': self.get_serializer(element_map).data,
        })

    @action(detail=True, methods=['post'], url_path='manual-capture-result')
    def manual_capture_result(self, request, pk=None):
        """接收执行器回填的人工采集元素地图。"""
        element_map = self.get_object()
        status_value = str(request.data.get('status') or '').strip().lower()
        map_payload = request.data.get('element_map') or request.data.get('map_json') or {}
        if status_value != 'success' or not isinstance(map_payload, dict):
            manual_capture = (
                element_map.map_json.get('manual_capture')
                if isinstance(element_map.map_json, dict) and isinstance(element_map.map_json.get('manual_capture'), dict)
                else {}
            )
            element_map.map_json = {
                **(element_map.map_json if isinstance(element_map.map_json, dict) else {}),
                'capture_mode': 'manual_capture',
                'source': 'manual_capture',
                'manual_capture': {
                    **manual_capture,
                    'status': 'failed',
                    'finished_at': timezone.now().isoformat(),
                    'error_message': str(request.data.get('message') or request.data.get('error') or '人工采集失败')[:1000],
                },
            }
            element_map.stale_status = 'superseded'
            element_map.stale_reason = element_map.map_json['manual_capture']['error_message']
            element_map.save(update_fields=['map_json', 'stale_status', 'stale_reason', 'updated_at'])
            return Response(self.get_serializer(element_map).data, status=status.HTTP_200_OK)

        previous_json = element_map.map_json if isinstance(element_map.map_json, dict) else None
        map_payload = {
            **map_payload,
            'capture_mode': 'manual_capture',
            'source': 'manual_capture',
            'manual_capture': {
                **(map_payload.get('manual_capture') if isinstance(map_payload.get('manual_capture'), dict) else {}),
                'status': 'completed',
                'finished_at': timezone.now().isoformat(),
                'dispatch_id': request.data.get('dispatch_id') or '',
                'actuator_id': request.data.get('actuator_id') or '',
            },
        }
        annotated = annotate_map_json(
            map_payload,
            project_id=element_map.project_id,
            environment_config=element_map.environment_config,
            base_url=map_payload.get('base_url') or element_map.base_url or '',
            previous_map_json=previous_json,
        )
        governance = annotated.get('governance') if isinstance(annotated.get('governance'), dict) else {}
        scope = annotated.get('scope') if isinstance(annotated.get('scope'), dict) else {}
        coverage = annotated.get('coverage_summary') if isinstance(annotated.get('coverage_summary'), dict) else {}
        element_map.map_json = annotated
        element_map.base_url = annotated.get('base_url') or element_map.base_url
        element_map.coverage_summary = coverage
        element_map.low_confidence_items = annotated.get('low_confidence_items') or []
        element_map.risk_items = annotated.get('risk_items') or []
        element_map.version_group = governance.get('version_group') or scope.get('version_group') or element_map.version_group
        element_map.role_key = scope.get('role_key') or element_map.role_key or 'default'
        element_map.permission_key = scope.get('permission_key') or element_map.permission_key or 'default'
        element_map.map_hash = governance.get('map_hash') or element_map.map_hash
        element_map.baseline_hash = governance.get('baseline_hash') or element_map.baseline_hash
        element_map.diff_summary = governance.get('diff_summary') or {}
        element_map.stale_status = governance.get('stale_status') or 'current'
        element_map.stale_reason = ''
        element_map.status = 'confirmed'
        element_map.confirmed_by = element_map.creator or request.user
        element_map.save(update_fields=[
            'map_json', 'base_url', 'coverage_summary', 'low_confidence_items', 'risk_items',
            'version_group', 'role_key', 'permission_key', 'map_hash', 'baseline_hash',
            'diff_summary', 'stale_status', 'stale_reason', 'status', 'confirmed_by', 'updated_at'
        ])
        return Response(self.get_serializer(element_map).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        """确认元素地图，用于后续高成功率生成。"""
        element_map = self.get_object()
        element_map.status = 'confirmed'
        element_map.confirmed_by = request.user
        element_map.stale_status = 'current'
        element_map.stale_reason = ''
        element_map.save(update_fields=['status', 'confirmed_by', 'stale_status', 'stale_reason', 'updated_at'])
        self._refresh_governance_fields(element_map)
        return Response(self.get_serializer(element_map).data)

    @action(detail=False, methods=['post'], url_path='batch-delete')
    def batch_delete(self, request, **kwargs):
        """批量删除元素地图"""
        ids_data = request.data.get('ids', [])
        if not ids_data:
            return Response({'error': '请提供要删除的元素地图ID列表'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            map_ids = [int(map_id) for map_id in ids_data]
        except (TypeError, ValueError):
            return Response({'error': 'ids参数格式错误，应为数字列表'}, status=status.HTTP_400_BAD_REQUEST)

        if not map_ids:
            return Response({'error': '元素地图ID列表不能为空'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = self.get_queryset().filter(id__in=map_ids)
        found_ids = list(queryset.values_list('id', flat=True))
        not_found_ids = [map_id for map_id in map_ids if map_id not in found_ids]
        if not_found_ids:
            return Response(
                {'error': f'以下元素地图ID不存在: {not_found_ids}', 'not_found_ids': not_found_ids},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                _deleted_count, deleted_details = queryset.delete()
                return Response(
                    {
                        'message': f'成功删除 {len(found_ids)} 个元素地图',
                        'deleted_count': len(found_ids),
                        'deletion_details': deleted_details,
                    },
                    status=status.HTTP_200_OK,
                )
        except Exception as exc:
            return Response(
                {'error': f'删除过程中发生错误: {str(exc)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


def _merge_ai_generation_history(task: UiAiGenerationTask, item: dict) -> None:
    repair_history = task.repair_history if isinstance(task.repair_history, list) else []
    repair_history.append(item)
    task.repair_history = repair_history[-200:]


def _ai_generation_structural_steps(payload: dict) -> list[dict]:
    generated_case = payload.get('generated_case') if isinstance(payload.get('generated_case'), dict) else {}
    test_plan = payload.get('test_plan') if isinstance(payload.get('test_plan'), dict) else {}
    steps = generated_case.get('steps') if isinstance(generated_case.get('steps'), list) else None
    if steps is None:
        steps = test_plan.get('steps') if isinstance(test_plan.get('steps'), list) else []
    return [step for step in steps if isinstance(step, dict)]


def _ai_generation_step_text(step: dict) -> str:
    return ' '.join(str(step.get(key) or '') for key in [
        'action', 'operation', 'description', 'target_name', 'value',
    ])


def _ai_generation_structural_quality(payload: dict) -> int:
    steps = _ai_generation_structural_steps(payload)
    if not steps:
        return 0

    score = len(steps)
    has_goto = False
    has_opener = False
    has_dialog_assert = False
    has_form_action = False
    has_confirmation = False
    first_business_operation = ''
    first_form_action_has_prior_opener = True
    prior_opener = False

    for step in steps:
        operation = str(step.get('operation') or step.get('action') or '').strip().lower()
        text = _ai_generation_step_text(step)
        if operation in {'goto', 'navigate', 'open'}:
            has_goto = True
            continue
        if operation == 'click' and isinstance(step.get('state_transition'), dict):
            has_opener = True
            prior_opener = True
        if not first_business_operation:
            first_business_operation = operation
        if operation in {'assert_visible', 'assert_text', 'assert_contain_text'} and any(keyword in text for keyword in ['弹窗', 'dialog']):
            has_dialog_assert = True
        if operation in {'fill', 'select_option', 'check', 'uncheck'}:
            has_form_action = True
            if not prior_opener:
                first_form_action_has_prior_opener = False
        if operation == 'click' and any(keyword in text for keyword in ['保存', '提交', '确定', '确认']):
            has_confirmation = True

    if has_goto:
        score += 5
    if has_opener:
        score += 30
    if has_dialog_assert:
        score += 15
    if has_form_action:
        score += 10
    if has_confirmation:
        score += 20
    if first_business_operation in {'fill', 'select_option', 'check', 'uncheck'} and not has_opener:
        score -= 40
    if not first_form_action_has_prior_opener:
        score -= 80
    return score


def _should_apply_async_llm_plan(task: UiAiGenerationTask, plan: dict) -> tuple[bool, str]:
    plan_validation = plan.get('plan_validation') if isinstance(plan.get('plan_validation'), dict) else {}
    if plan_validation.get('status') == 'failed':
        semantic_issues = plan_validation.get('semantic_issues') if isinstance(plan_validation.get('semantic_issues'), list) else []
        unresolved_steps = plan_validation.get('unresolved_steps') if isinstance(plan_validation.get('unresolved_steps'), list) else []
        issue_messages = [
            str(item.get('message') or item.get('reason') or '')
            for item in [*semantic_issues, *unresolved_steps]
            if isinstance(item, dict) and (item.get('message') or item.get('reason'))
        ]
        if issue_messages:
            return False, '后台 LLM 规划未通过语义/绑定校验，拒绝写回: ' + '；'.join(issue_messages[:3])
        return False, '后台 LLM 规划未通过 plan_validation，拒绝写回'

    current_payload = {
        'generated_case': task.generated_case if isinstance(task.generated_case, dict) else {},
        'test_plan': task.test_plan if isinstance(task.test_plan, dict) else {},
    }
    current_steps = _ai_generation_structural_steps(current_payload)
    plan_steps = _ai_generation_structural_steps(plan)
    if not current_steps:
        return True, '当前无可用结构步骤，采用后台 LLM 规划'
    if not plan_steps:
        return False, '后台 LLM 规划缺少结构步骤，保留执行器规则规划'
    current_quality = _ai_generation_structural_quality(current_payload)
    plan_quality = _ai_generation_structural_quality(plan)
    if plan_quality >= current_quality:
        return True, f'后台 LLM 规划质量不低于当前规划: {plan_quality}>={current_quality}'
    return False, f'后台 LLM 规划质量低于当前规划: {plan_quality}<{current_quality}'


def _mark_async_llm_plan(task_id: int, payload: dict, planning_status: str, **extra) -> None:
    task = UiAiGenerationTask.objects.get(id=task_id)
    verification = task.verification_result if isinstance(task.verification_result, dict) else {}
    verification['async_llm_planning'] = {
        **(verification.get('async_llm_planning') if isinstance(verification.get('async_llm_planning'), dict) else {}),
        'status': planning_status,
        **extra,
    }
    task.verification_result = verification
    _merge_ai_generation_history(task, {
        'type': 'async_llm_business_planning',
        'status': planning_status,
        'task_id': task_id,
        'queued_at': payload.get('queued_at') or '',
        'updated_at': timezone.now().isoformat(),
        **{key: value for key, value in extra.items() if key in {'message', 'llm_enabled', 'llm_error'}},
    })
    task.save(update_fields=['verification_result', 'repair_history', 'updated_at'])


def _run_async_llm_generation_plan(task_id: int, payload: dict) -> None:
    """后台执行 LLM 规划。结果写回任务，但不阻塞执行器主流程。"""
    close_old_connections()
    started_at = timezone.now()
    try:
        _mark_async_llm_plan(
            task_id,
            payload,
            'running',
            started_at=started_at.isoformat(),
            message='LLM UI 步骤规划后台执行中',
        )
        task = UiAiGenerationTask.objects.select_related('project', 'environment_config', 'creator').get(id=task_id)
        plan = build_llm_ui_generation_plan(task, {**payload, 'async_llm_plan': True})
        completed_at = timezone.now()

        task.refresh_from_db()
        verification = task.verification_result if isinstance(task.verification_result, dict) else {}
        verification['async_llm_planning'] = {
            'status': 'completed',
            'started_at': started_at.isoformat(),
            'completed_at': completed_at.isoformat(),
            'duration_seconds': (completed_at - started_at).total_seconds(),
            'llm_enabled': bool(plan.get('llm_enabled')),
            'llm_error': plan.get('llm_error') or '',
            'llm_config': plan.get('llm_config') or None,
            'plan_validation': plan.get('plan_validation') if isinstance(plan.get('plan_validation'), dict) else {},
            'message': 'LLM UI 步骤规划后台执行完成',
        }
        if isinstance(plan.get('intent'), dict):
            verification['async_llm_intent'] = plan['intent']
        if isinstance(plan.get('generated_typescript_files'), dict):
            script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
            script_artifacts['async_playwright_ts_files'] = plan['generated_typescript_files']
            verification['script_artifacts'] = script_artifacts
        task.verification_result = verification

        update_fields = ['verification_result', 'repair_history', 'updated_at']
        apply_plan, apply_reason = _should_apply_async_llm_plan(task, plan)
        verification['async_llm_planning']['applied_to_task'] = bool(plan.get('llm_enabled') and apply_plan)
        verification['async_llm_planning']['apply_reason'] = apply_reason
        task.verification_result = verification

        if plan.get('llm_enabled') and apply_plan:
            if isinstance(plan.get('test_plan'), dict):
                task.test_plan = plan['test_plan']
                update_fields.append('test_plan')
            if isinstance(plan.get('generated_case'), dict):
                generated_case = plan['generated_case']
                if isinstance(plan.get('intent'), dict):
                    generated_case['intent_analysis'] = plan['intent']
                task.generated_case = generated_case
                update_fields.append('generated_case')
            if isinstance(plan.get('generated_script'), str) and plan['generated_script'].strip():
                task.generated_script = plan['generated_script']
                task.generated_script_hash = hashlib.sha256(task.generated_script.encode('utf-8')).hexdigest()
                update_fields.extend(['generated_script', 'generated_script_hash'])

        _merge_ai_generation_history(task, {
            'type': 'async_llm_business_planning',
            'status': 'completed',
            'task_id': task_id,
            'llm_enabled': bool(plan.get('llm_enabled')),
            'llm_config': plan.get('llm_config'),
            'llm_error': plan.get('llm_error'),
            'started_at': started_at.isoformat(),
            'completed_at': completed_at.isoformat(),
            'applied_to_task': bool(plan.get('llm_enabled') and apply_plan),
            'apply_reason': apply_reason,
            'message': '后台 LLM 规划已完成；执行器主流程未被阻塞',
        })
        task.save(update_fields=sorted(set(update_fields)))
        logger.info("AI UI 后台 LLM 规划完成: task_id=%s, llm_enabled=%s", task_id, bool(plan.get('llm_enabled')))
    except Exception as exc:
        logger.warning("AI UI 后台 LLM 规划失败: task_id=%s, error=%s", task_id, str(exc)[:500], exc_info=True)
        try:
            _mark_async_llm_plan(
                task_id,
                payload,
                'failed',
                completed_at=timezone.now().isoformat(),
                message='LLM UI 步骤规划后台执行失败',
                llm_enabled=False,
                llm_error=f'{type(exc).__name__}: {str(exc)}',
            )
        except Exception:
            logger.exception("AI UI 后台 LLM 规划失败状态写回失败: task_id=%s", task_id)
    finally:
        close_old_connections()


class UiAiGenerationTaskViewSet(viewsets.ModelViewSet):
    """Playwright MCP 全能力生成、验证与修复任务。"""
    queryset = UiAiGenerationTask.objects.select_related(
        'project', 'environment_config', 'element_map', 'creator'
    )
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['project', 'environment_config', 'element_map', 'status', 'source_type', 'failure_category']
    search_fields = ['name', 'source_requirement', 'target_url', 'target_module']
    ordering_fields = ['created_at', 'updated_at', 'started_at', 'completed_at']
    ordering = ['-created_at']
    DETAIL_PAYLOAD_FIELDS = {
        'test_plan',
        'mcp_observations',
        'element_map_snapshot',
        'generated_case',
        'generated_script',
        'verification_result',
        'repair_history',
    }

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == 'retrieve':
            return queryset.defer('element_map_snapshot', 'verification_result')
        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return UiAiGenerationTaskListSerializer
        if self.action in {'retrieve', 'apply_to_ui_case', 'start'}:
            return UiAiGenerationTaskDetailSerializer
        return UiAiGenerationTaskSerializer

    def perform_create(self, serializer):
        safety_policy = _merge_safety_policy(serializer.validated_data.get('safety_policy'))
        max_repair_rounds = int(safety_policy.get('max_repair_rounds') or 2)
        serializer.save(
            creator=self.request.user,
            safety_policy=safety_policy,
            max_repair_rounds=max(0, min(max_repair_rounds, 5)),
        )

    @staticmethod
    def _build_dispatch_id(task: UiAiGenerationTask) -> str:
        return f"ai-generation-{task.id}-{int(timezone.now().timestamp() * 1000)}"

    @staticmethod
    def _build_ai_generation_args(
        task: UiAiGenerationTask,
        safety_policy: dict | None = None,
        dispatch_id: str | None = None,
    ) -> dict:
        """构建执行器任务参数，供 WebSocket 下发和执行器拉取复用。"""
        env_config = None
        if task.environment_config_id:
            env_config = UiEnvironmentConfigSerializer(task.environment_config).data
            env_config['extra_config'] = (
                task.environment_config.extra_config
                if isinstance(task.environment_config.extra_config, dict) else {}
            )

        merged_policy = _merge_safety_policy(safety_policy or task.safety_policy)
        authentication_contract = classify_task_authentication_contract(task, merged_policy)
        requirement_document = build_requirement_document(task)
        requested_execution_mode = _normalize_execution_mode(
            merged_policy.get('execution_mode')
            or (task.safety_policy or {}).get('execution_mode')
        ) or 'contract_planner'
        merged_policy = {
            **merged_policy,
            'authentication_contract': authentication_contract,
            'execution_mode': requested_execution_mode,
        }
        element_map_json = task.element_map.map_json if task.element_map_id else None
        element_map_source = ''
        execution_mode = requested_execution_mode if requested_execution_mode == 'runtime_agent' else 'auto_exploration'
        if execution_mode != 'runtime_agent' and isinstance(element_map_json, dict):
            element_map_source = (
                element_map_json.get('source')
                or element_map_json.get('capture_mode')
                or ''
            )
            if element_map_json.get('capture_mode') == 'manual_capture' or element_map_source == 'manual_capture':
                execution_mode = 'manual_map'
        return {
            'task_id': task.id,
            'dispatch_id': dispatch_id or task.dispatch_id or UiAiGenerationTaskViewSet._build_dispatch_id(task),
            'project_id': task.project_id,
            'environment_config': env_config,
            'environment_config_id': task.environment_config_id,
            'source_type': task.source_type,
            'requirement': task.source_requirement or '',
            'gherkin': task.gherkin or '',
            'target_url': task.target_url or '',
            'target_module': task.target_module or '',
            'requirement_document': requirement_document,
            'safety_policy': merged_policy,
            'execution_mode': execution_mode,
            'authentication_mode': authentication_contract.get('mode') or 'unknown',
            'authentication_contract': authentication_contract,
            'max_repair_rounds': task.max_repair_rounds,
            'element_map_id': task.element_map_id,
            'element_map': element_map_json,
            'element_map_source': element_map_source,
            'execution_mode': execution_mode,
        }

    @staticmethod
    def _first_map_page(task: UiAiGenerationTask) -> dict:
        pages = UiAiGenerationTaskViewSet._map_pages(task)
        return pages[0] if pages and isinstance(pages[0], dict) else {}

    @staticmethod
    def _map_pages(task: UiAiGenerationTask) -> list[dict]:
        element_map = task.element_map_snapshot or {}
        if not element_map and task.element_map_id:
            element_map = task.element_map.map_json or {}
        pages = element_map.get('pages') if isinstance(element_map, dict) else []
        return [page for page in pages if isinstance(page, dict)]

    @staticmethod
    def _locators_for_ui_element(raw_element: dict) -> list[tuple[str, str]]:
        """把 MCP locator 候选转换为当前执行器可执行的主备定位。"""
        candidates = []
        recommended = raw_element.get('recommended_locator')
        if isinstance(recommended, dict):
            candidates.append(recommended)
        candidates.extend(raw_element.get('locator_candidates') or [])

        priority = ['test_id', 'label', 'role', 'placeholder', 'text', 'id', 'name', 'css', 'xpath']
        normalized: list[tuple[str, str, int]] = []
        for locator in candidates:
            if not isinstance(locator, dict):
                continue
            locator_type = locator.get('type')
            value = locator.get('value')
            if locator_type == 'role':
                value = json.dumps({
                    'exact': True,
                    'name': str(locator.get('name') or ''),
                    'role': str(value or ''),
                }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            elif locator_type == 'text' and (locator.get('scope') or locator.get('exact')):
                value = json.dumps({
                    'exact': bool(locator.get('exact')),
                    'scope': locator.get('scope') if isinstance(locator.get('scope'), dict) else {},
                    'value': str(value or ''),
                }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if not locator_type or value in (None, ''):
                continue
            if locator_type not in priority:
                continue
            normalized.append((locator_type, str(value), priority.index(locator_type)))

        if normalized:
            normalized.sort(key=lambda item: item[2])
            result = []
            seen = set()
            for locator_type, locator_value, _ in normalized:
                key = (locator_type, locator_value)
                if key in seen:
                    continue
                seen.add(key)
                result.append(key)
                if len(result) >= 3:
                    break
            return result

        text = raw_element.get('accessible_name') or raw_element.get('text') or raw_element.get('name')
        if text:
            return [('text', str(text)[:120])]
        return [('xpath', raw_element.get('xpath') or '//*')]

    @staticmethod
    def _locator_for_ui_element(raw_element: dict) -> tuple[str, str]:
        """兼容旧调用：返回优先 locator。"""
        locators = UiAiGenerationTaskViewSet._locators_for_ui_element(raw_element)
        return locators[0]

    @staticmethod
    def _normalized_page_key(page_key: str) -> str:
        match = re.fullmatch(r'page_probe_(\d+)', str(page_key or ''))
        return f'page_{match.group(1)}' if match else str(page_key or '')

    @staticmethod
    def _contextual_locator_hint(step: dict, locator_hint: dict) -> dict:
        hint = dict(locator_hint or {})
        semantic_context = ' '.join([
            str(step.get('target_name') or ''),
            str(step.get('description') or ''),
            str(step.get('context_type') or ''),
        ]).casefold()
        if hint.get('type') == 'text' and any(
            token in semantic_context for token in ('弹窗', '对话框', 'dialog', 'modal')
        ):
            hint['exact'] = True
            hint['scope'] = {'role': 'dialog'}
        return hint

    @classmethod
    def _raw_element_matches_step(cls, raw_element: dict, step: dict) -> bool:
        locator = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
        target_name = str(step.get('target_name') or '').strip().casefold()
        raw_names = {
            str(raw_element.get(key) or '').strip().casefold()
            for key in ('name', 'accessible_name', 'text', 'placeholder', 'label')
            if raw_element.get(key)
        }
        locator_type = str(locator.get('type') or '')
        locator_value = str(locator.get('value') or '')
        locator_name = str(locator.get('name') or '').strip().casefold()
        candidates = []
        recommended = raw_element.get('recommended_locator')
        if isinstance(recommended, dict):
            candidates.append(recommended)
        candidates.extend(item for item in (raw_element.get('locator_candidates') or []) if isinstance(item, dict))
        for candidate in candidates:
            if str(candidate.get('type') or '') != locator_type:
                continue
            if str(candidate.get('value') or '') != locator_value:
                continue
            candidate_name = str(candidate.get('name') or '').strip().casefold()
            if locator_name and candidate_name != locator_name:
                continue
            return True
        return bool(not locator_type and target_name and target_name in raw_names)

    @classmethod
    def _resolve_map_element_for_plan_step(
        cls,
        step: dict,
        pages: list[dict],
        fallback_page_key: str = '',
    ) -> tuple[dict, dict] | None:
        requested_page_key = cls._normalized_page_key(str(step.get('page_key') or fallback_page_key or ''))
        element_key = str(step.get('element_key') or '').strip()
        ordered_pages = sorted(
            pages,
            key=lambda page: 0 if str(page.get('page_key') or '') == requested_page_key else 1,
        )

        if element_key:
            for page in ordered_pages:
                if requested_page_key and str(page.get('page_key') or '') != requested_page_key:
                    continue
                for raw_element in page.get('elements') or []:
                    if not isinstance(raw_element, dict):
                        continue
                    if str(raw_element.get('element_key') or '') != element_key:
                        continue
                    if cls._raw_element_matches_step(raw_element, step):
                        return page, raw_element

        for page in ordered_pages:
            for raw_element in page.get('elements') or []:
                if isinstance(raw_element, dict) and cls._raw_element_matches_step(raw_element, step):
                    return page, raw_element
        return None

    @staticmethod
    def _get_or_create_root_module(project_id: int, module_name: str, user) -> UiModule:
        module_name = (module_name or 'AI 生成用例').strip()[:100]
        existing = UiModule.objects.filter(project_id=project_id, parent__isnull=True, name=module_name).first()
        if existing:
            return existing
        return UiModule.objects.create(project_id=project_id, name=module_name, parent=None, level=1, creator=user)

    @staticmethod
    def _match_element_for_plan_step(
        step: dict,
        elements: list[UiElement],
        elements_by_key: dict[str, UiElement] | None = None,
    ) -> UiElement | None:
        element_key = str(step.get('element_key') or '').strip()
        if element_key and elements_by_key and element_key in elements_by_key:
            return elements_by_key[element_key]

        target = ' '.join([
            str(step.get('target_name') or ''),
            str(step.get('element_name') or ''),
            str(step.get('element_hint') or ''),
            str(step.get('description') or ''),
        ]).strip().lower()
        if not target:
            return None
        for element in elements:
            name = (element.name or '').lower()
            description = (element.description or '').lower()
            if name and (name in target or target in name):
                return element
            if description and target in description:
                return element
        target_parts = [part for part in target.replace(':', ' ').replace('：', ' ').split() if len(part) >= 2]
        for element in elements:
            haystack = f"{element.name or ''} {element.description or ''}".lower()
            if any(part in haystack for part in target_parts):
                return element
        return None

    @staticmethod
    def _normalize_plan_operation(step: dict) -> str:
        operation = str(step.get('operation') or step.get('action') or '').strip().lower()
        aliases = {
            'navigate': 'goto',
            'open': 'goto',
            'input': 'fill',
            'type': 'fill',
            'select': 'select_option',
            'assert': 'assert_visible',
            'visible': 'assert_visible',
            'contains': 'assert_contain_text',
        }
        return aliases.get(operation, operation)

    @staticmethod
    def _is_runtime_state_step(step: dict) -> bool:
        if not isinstance(step, dict):
            return False
        operation = UiAiGenerationTaskViewSet._normalize_plan_operation(step)
        return (
            operation.startswith('assert_')
            and str(step.get('binding_mode') or '').strip() == 'runtime_state'
            and str(step.get('runtime_resolver') or '').strip() == 'state_evidence'
        )

    def _create_details_from_generated_steps(
        self,
        page_step: UiPageSteps,
        generated_steps: list,
        resolved_elements_by_step: dict[int, UiElement],
        base_url: str,
    ) -> int:
        allowed_operations = {
            'goto', 'fill', 'click', 'select_option', 'check', 'uncheck', 'wait',
            'assert_visible', 'assert_text', 'assert_contain_text', 'assert_value',
        }
        created_count = 0
        for index, raw_step in enumerate(generated_steps):
            if not isinstance(raw_step, dict):
                continue
            operation = self._normalize_plan_operation(raw_step)
            if operation not in allowed_operations:
                continue
            if raw_step.get('requires_confirmation') and operation in {'click', 'fill', 'select_option', 'check', 'uncheck'}:
                operation = 'assert_visible'
            persisted_operation = 'select' if operation == 'select_option' else operation

            value = raw_step.get('value')
            if value is None:
                value = raw_step.get('input') or raw_step.get('expected') or ''
            value = str(value or '')

            if operation == 'goto':
                UiPageStepsDetailed.objects.create(
                    page_step=page_step,
                    step_type=0,
                    step_sort=created_count,
                    ope_key='goto',
                    ope_value={'url': value or base_url},
                    description=raw_step.get('description') or '加载目标页面',
                )
                created_count += 1
                continue

            element = resolved_elements_by_step.get(index)
            is_runtime_state = self._is_runtime_state_step(raw_step)
            if not element and operation not in {'wait'} and not is_runtime_state:
                raise ValueError(
                    f"步骤 {index + 1} 无法绑定页面元素: "
                    f"page_key={raw_step.get('page_key') or ''}, "
                    f"element_key={raw_step.get('element_key') or ''}, "
                    f"target={raw_step.get('target_name') or ''}"
                )

            step_type = 1 if operation.startswith('assert_') else 0
            ope_value = {}
            if operation in {'fill'}:
                ope_value = {'text': value, 'value': value}
                binding_mode = str(raw_step.get('binding_mode') or '').strip()
                runtime_resolver = str(raw_step.get('runtime_resolver') or '').strip()
                if binding_mode:
                    ope_value['binding_mode'] = binding_mode
                if runtime_resolver:
                    ope_value['runtime_resolver'] = runtime_resolver
            elif operation in {'select_option', 'wait', 'assert_text', 'assert_contain_text', 'assert_value'}:
                ope_value = {'value': value}
                if is_runtime_state:
                    state_assertion = raw_step.get('state_assertion')
                    ope_value.update({
                        'binding_mode': 'runtime_state',
                        'runtime_resolver': 'state_evidence',
                        'page_key': raw_step.get('page_key') or '',
                        'target_name': raw_step.get('target_name') or '',
                    })
                    if isinstance(state_assertion, dict):
                        ope_value['state_assertion'] = state_assertion

            UiPageStepsDetailed.objects.create(
                page_step=page_step,
                step_type=step_type,
                element=element,
                step_sort=created_count,
                ope_key=persisted_operation,
                ope_value=ope_value or None,
                description=raw_step.get('description') or f"{persisted_operation} {element.name if element else ''}".strip(),
            )
            created_count += 1
        return created_count

    @staticmethod
    def _review_queue(task: UiAiGenerationTask) -> list[dict]:
        verification = task.verification_result if isinstance(task.verification_result, dict) else {}
        queue = verification.get('review_queue') if isinstance(verification.get('review_queue'), list) else []
        return [item for item in queue if isinstance(item, dict)]

    @staticmethod
    def _locator_signature(page_key: str, element_key: str, name: str, locator: dict) -> str:
        payload = {
            'page_key': page_key or '',
            'element_key': element_key or '',
            'name': name or '',
            'locator': locator if isinstance(locator, dict) else {},
        }
        import hashlib
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    @staticmethod
    def _locator_stability_score(locator: dict) -> float:
        if not isinstance(locator, dict):
            return 0
        try:
            confidence = float(locator.get('confidence') or 0)
        except (TypeError, ValueError):
            confidence = 0
        type_weight = {
            'mcp_ref': 0.98,
            'test_id': 1.0,
            'label': 0.88,
            'placeholder': 0.82,
            'role': 0.78,
            'id': 0.72,
            'name': 0.68,
            'text': 0.58,
            'css': 0.42,
            'xpath': 0.32,
        }.get(locator.get('type'), 0.3)
        return round(min(1.0, max(0.0, (confidence * 0.65) + (type_weight * 0.35))), 3)

    @classmethod
    def _apply_locator_patch_to_map(cls, map_json: dict, review_item: dict) -> dict:
        if not isinstance(map_json, dict):
            map_json = {}
        page_key = str(review_item.get('page_key') or '')
        element_key = str(review_item.get('element_key') or '')
        new_locator = review_item.get('new_locator') if isinstance(review_item.get('new_locator'), dict) else {}
        target_name = str(review_item.get('target_name') or '')
        if not element_key or not new_locator:
            raise ValueError('确认项缺少 element_key 或 new_locator')

        patched_element = None
        for page_state in map_json.get('pages') or []:
            if not isinstance(page_state, dict):
                continue
            if page_key and str(page_state.get('page_key') or '') != page_key:
                continue
            for element in page_state.get('elements') or []:
                if not isinstance(element, dict):
                    continue
                if str(element.get('element_key') or '') != element_key:
                    continue
                old_locator = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {}
                candidates = element.get('locator_candidates') if isinstance(element.get('locator_candidates'), list) else []
                locator_key = json.dumps(new_locator, ensure_ascii=False, sort_keys=True)
                if not any(
                    isinstance(candidate, dict)
                    and json.dumps(candidate, ensure_ascii=False, sort_keys=True) == locator_key
                    for candidate in candidates
                ):
                    candidates.insert(0, new_locator)
                element['locator_candidates'] = candidates
                element['previous_recommended_locator'] = old_locator
                element['recommended_locator'] = new_locator
                element['review_confirmed_at'] = timezone.now().isoformat()
                element['review_confirmed'] = True
                patched_element = element
                break
            if patched_element:
                break
        if not patched_element:
            raise ValueError(f'元素地图中未找到 page_key={page_key}, element_key={element_key}')

        baseline = map_json.get('locator_baseline') if isinstance(map_json.get('locator_baseline'), list) else []
        updated = False
        baseline_item = {
            'page_key': page_key,
            'element_key': element_key,
            'name': target_name or patched_element.get('name') or patched_element.get('accessible_name') or '',
            'tag': patched_element.get('tag') or '',
            'recommended_locator': new_locator,
            'candidate_count': len(patched_element.get('locator_candidates') or []),
            'stability_score': cls._locator_stability_score(new_locator),
            'signature': cls._locator_signature(page_key, element_key, target_name, new_locator),
            'source': 'human_review_confirmed',
            'confirmed_at': timezone.now().isoformat(),
        }
        for index, item in enumerate(baseline):
            if (
                isinstance(item, dict)
                and str(item.get('page_key') or '') == page_key
                and str(item.get('element_key') or '') == element_key
            ):
                baseline[index] = {**item, **baseline_item}
                updated = True
                break
        if not updated:
            baseline.append(baseline_item)
        map_json['locator_baseline'] = baseline

        summary = map_json.get('coverage_summary') if isinstance(map_json.get('coverage_summary'), dict) else {}
        summary['human_review_confirmed_count'] = sum(
            1
            for item in baseline
            if isinstance(item, dict) and item.get('source') == 'human_review_confirmed'
        )
        map_json['coverage_summary'] = summary
        return map_json

    @staticmethod
    def _apply_locator_patch_to_generated_steps(task: UiAiGenerationTask, review_item: dict) -> bool:
        new_locator = review_item.get('new_locator') if isinstance(review_item.get('new_locator'), dict) else {}
        if not new_locator:
            return False
        step_sort = review_item.get('step_sort')
        element_key = str(review_item.get('element_key') or '')
        changed = False

        def patch_steps(container: dict, key: str) -> None:
            nonlocal changed
            steps = container.get(key)
            if not isinstance(steps, list):
                return
            for step in steps:
                if not isinstance(step, dict):
                    continue
                same_sort = step_sort is not None and str(step.get('step_sort')) == str(step_sort)
                same_element = element_key and str(step.get('element_key') or '') == element_key
                if same_sort or same_element:
                    step['locator_hint'] = new_locator
                    step['human_review_confirmed'] = True
                    step['human_review_confirmed_at'] = timezone.now().isoformat()
                    changed = True

        generated_case = task.generated_case if isinstance(task.generated_case, dict) else {}
        test_plan = task.test_plan if isinstance(task.test_plan, dict) else {}
        patch_steps(generated_case, 'steps')
        patch_steps(test_plan, 'steps')
        task.generated_case = generated_case
        task.test_plan = test_plan
        return changed

    @action(detail=True, methods=['post'], url_path='llm-plan')
    def llm_plan(self, request, pk=None):
        """结合页面状态元素地图和需求，用 LLM 做复杂业务意图理解与步骤规划。"""
        task = self.get_object()
        payload = request.data if isinstance(request.data, dict) else {}
        async_requested = bool(
            payload.get('async')
            or payload.get('async_mode')
            or payload.get('background')
            or payload.get('async_llm_plan')
        )
        if async_requested:
            queued_at = timezone.now().isoformat()
            async_payload = {**payload, 'async_llm_plan': True, 'queued_at': queued_at}
            verification = task.verification_result if isinstance(task.verification_result, dict) else {}
            verification['async_llm_planning'] = {
                'status': 'queued',
                'queued_at': queued_at,
                'message': 'LLM UI 步骤规划已提交后台执行',
            }
            task.verification_result = verification
            _merge_ai_generation_history(task, {
                'type': 'async_llm_business_planning',
                'status': 'queued',
                'task_id': task.id,
                'queued_at': queued_at,
                'message': '执行器已提交后台 LLM 规划，主流程继续执行',
            })
            task.save(update_fields=['verification_result', 'repair_history', 'updated_at'])
            _LLM_PLAN_EXECUTOR.submit(_run_async_llm_generation_plan, task.id, async_payload)
            return Response({
                'message': 'LLM UI 步骤规划已提交后台执行',
                'async': True,
                'status': 'queued',
                'task_id': task.id,
                'queued_at': queued_at,
            }, status=status.HTTP_202_ACCEPTED)
        plan = build_llm_ui_generation_plan(task, payload)
        return Response({
            'message': 'LLM UI 步骤规划完成',
            'plan': plan,
        })

    @action(detail=True, methods=['post'], url_path='report-result')
    def report_result(self, request, pk=None):
        """执行器通过 HTTP 上报 AI 生成结果，避免超大结果阻塞 WebSocket 队列。"""
        task = self.get_object()
        payload = request.data if isinstance(request.data, dict) else {}
        result_payload = dict(payload)
        result_payload['task_id'] = task.id
        from .consumers import save_ai_generation_result_sync

        save_ai_generation_result_sync(result_payload)
        task.refresh_from_db()
        return Response({
            'message': 'AI UI 生成任务结果已保存',
            'task_id': task.id,
            'status': task.status,
            'dispatch_status': task.dispatch_status,
        })

    @action(detail=True, methods=['post'], url_path='llm-repair')
    def llm_repair(self, request, pk=None):
        """结合真实执行失败证据、trace 摘要和元素地图，用 LLM 生成 spec 修复 patch。"""
        task = self.get_object()
        payload = request.data if isinstance(request.data, dict) else {}
        patch = build_llm_ui_repair_patch(task, payload)
        return Response({
            'message': 'LLM UI 修复规划完成',
            'patch': patch,
        })

    @action(detail=True, methods=['get'], url_path='review-queue')
    def review_queue(self, request, pk=None):
        """获取 AI 生成任务的人审队列。"""
        task = self.get_object()
        queue = self._review_queue(task)
        return Response({
            'task_id': task.id,
            'element_map_id': task.element_map_id,
            'pending_count': sum(1 for item in queue if item.get('status') == 'pending'),
            'items': queue,
        })

    @action(detail=True, methods=['post'], url_path='review-queue/resolve')
    def resolve_review_queue(self, request, pk=None):
        """确认或驳回人审队列项；确认 locator patch 时反写元素地图基线。"""
        task = self.get_object()
        payload = request.data if isinstance(request.data, dict) else {}
        action_value = str(payload.get('action') or 'approve').strip().lower()
        if action_value not in {'approve', 'reject'}:
            return Response({'error': 'action 只支持 approve 或 reject'}, status=status.HTTP_400_BAD_REQUEST)

        indexes = payload.get('indexes')
        if indexes is None:
            indexes = payload.get('item_indexes')
        if indexes is None:
            indexes = []
        if isinstance(indexes, int):
            indexes = [indexes]
        if not isinstance(indexes, list):
            return Response({'error': 'indexes 必须是数组'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            target_indexes = set(int(index) for index in indexes) if indexes else set()
        except (TypeError, ValueError):
            return Response({'error': 'indexes 必须是数字数组'}, status=status.HTTP_400_BAD_REQUEST)

        verification = task.verification_result if isinstance(task.verification_result, dict) else {}
        queue = verification.get('review_queue') if isinstance(verification.get('review_queue'), list) else []
        queue = [item for item in queue if isinstance(item, dict)]
        if not queue:
            return Response({'error': '当前任务没有人审队列'}, status=status.HTTP_400_BAD_REQUEST)
        target_indexes = target_indexes or {
            index for index, item in enumerate(queue) if item.get('status') == 'pending'
        }
        if not target_indexes:
            return Response({'error': '没有可处理的人审项'}, status=status.HTTP_400_BAD_REQUEST)

        element_map = task.element_map
        if action_value == 'approve' and not element_map:
            return Response({'error': '当前任务没有关联元素地图，无法反写基线'}, status=status.HTTP_400_BAD_REQUEST)

        comment = str(payload.get('comment') or '').strip()
        processed = []
        errors = []
        map_json = element_map.map_json if element_map and isinstance(element_map.map_json, dict) else {}
        now_text = timezone.now().isoformat()

        with transaction.atomic():
            for index, item in enumerate(queue):
                if index not in target_indexes:
                    continue
                if item.get('status') != 'pending':
                    continue
                try:
                    if action_value == 'approve' and item.get('type') == 'locator_patch_review':
                        map_json = self._apply_locator_patch_to_map(map_json, item)
                        self._apply_locator_patch_to_generated_steps(task, item)
                        item['status'] = 'approved'
                        item['applied_to_element_map'] = True
                    elif action_value == 'approve':
                        item['status'] = 'approved'
                        item['applied_to_element_map'] = False
                    else:
                        item['status'] = 'rejected'
                        item['applied_to_element_map'] = False
                    item['reviewed_at'] = now_text
                    item['reviewed_by'] = request.user.username
                    if comment:
                        item['review_comment'] = comment
                    processed.append(index)
                except Exception as exc:
                    errors.append({'index': index, 'error': str(exc)})

            verification['review_queue'] = queue
            verification['review_queue_summary'] = {
                'pending_count': sum(1 for item in queue if item.get('status') == 'pending'),
                'approved_count': sum(1 for item in queue if item.get('status') == 'approved'),
                'rejected_count': sum(1 for item in queue if item.get('status') == 'rejected'),
                'updated_at': now_text,
            }
            task.verification_result = verification
            task_update_fields = ['verification_result', 'updated_at']
            if element_map and action_value == 'approve':
                task.element_map_snapshot = map_json
                task_update_fields.extend(['element_map_snapshot', 'generated_case', 'test_plan'])
            task.save(update_fields=task_update_fields)

            if element_map and action_value == 'approve':
                previous_json = element_map.map_json if isinstance(element_map.map_json, dict) else {}
                element_map.map_json = map_json
                element_map.coverage_summary = map_json.get('coverage_summary') or element_map.coverage_summary
                element_map.status = 'confirmed'
                element_map.confirmed_by = request.user
                element_map.stale_status = 'current'
                element_map.stale_reason = ''
                element_map.save(update_fields=[
                    'map_json', 'coverage_summary', 'status', 'confirmed_by',
                    'stale_status', 'stale_reason', 'updated_at'
                ])
                UiElementMapViewSet._refresh_governance_fields(element_map, previous_json=previous_json)

        return Response({
            'message': '人审队列已处理',
            'processed': processed,
            'errors': errors,
            'pending_count': verification['review_queue_summary']['pending_count'],
            'task': self.get_serializer(task).data,
            'element_map_id': element_map.id if element_map else None,
        }, status=status.HTTP_207_MULTI_STATUS if errors else status.HTTP_200_OK)

    @staticmethod
    def _download_response(content: str, filename: str, content_type: str) -> HttpResponse:
        response = HttpResponse(content or '', content_type=content_type)
        ascii_filename = ''.join(
            ch if ch.isascii() and (ch.isalnum() or ch in {'-', '_', '.'}) else '_'
            for ch in filename
        ) or 'artifact'
        response['Content-Disposition'] = (
            f'attachment; filename="{ascii_filename}"; '
            f"filename*=UTF-8''{urlquote(filename)}"
        )
        return response

    @action(detail=True, methods=['get'], url_path='download-artifact')
    def download_artifact(self, request, pk=None):
        """下载 AI 生成任务产物：html、junit、json、ts、python。"""
        task = self.get_object()
        artifact_type = (request.query_params.get('type') or 'html').strip().lower()
        verification = task.verification_result if isinstance(task.verification_result, dict) else {}
        report_artifacts = verification.get('report_artifacts') if isinstance(verification.get('report_artifacts'), dict) else {}
        script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
        generated_case = task.generated_case if isinstance(task.generated_case, dict) else {}

        safe_name = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '_' for ch in (task.name or f'task_{task.id}'))[:80]
        if artifact_type == 'html':
            content = report_artifacts.get('html') or ''
            if not content:
                return Response({'error': '当前任务没有 HTML 报告产物'}, status=status.HTTP_404_NOT_FOUND)
            return self._download_response(content, f'{safe_name}_report.html', 'text/html; charset=utf-8')
        if artifact_type in {'junit', 'xml'}:
            content = report_artifacts.get('junit_xml') or ''
            if not content:
                return Response({'error': '当前任务没有 JUnit XML 产物'}, status=status.HTTP_404_NOT_FOUND)
            return self._download_response(content, f'{safe_name}_junit.xml', 'application/xml; charset=utf-8')
        if artifact_type in {'ts', 'typescript'}:
            content = (
                script_artifacts.get('playwright_ts_spec')
                or generated_case.get('playwright_ts_spec')
                or ''
            )
            if not content:
                return Response({'error': '当前任务没有 TypeScript Playwright spec 产物'}, status=status.HTTP_404_NOT_FOUND)
            return self._download_response(content, f'{safe_name}.spec.ts', 'text/typescript; charset=utf-8')
        if artifact_type in {'ts_bundle', 'typescript_bundle', 'zip'}:
            ts_files = (
                script_artifacts.get('playwright_ts_files')
                if isinstance(script_artifacts.get('playwright_ts_files'), dict) else
                generated_case.get('playwright_ts_files') if isinstance(generated_case.get('playwright_ts_files'), dict) else {}
            )
            ts_spec = script_artifacts.get('playwright_ts_spec') or generated_case.get('playwright_ts_spec') or ''
            if ts_spec and 'generated.spec.ts' not in ts_files:
                ts_files = {**ts_files, 'generated.spec.ts': ts_spec}
            if not ts_files:
                return Response({'error': '当前任务没有 TypeScript Playwright 文件包产物'}, status=status.HTTP_404_NOT_FOUND)
            import io
            import zipfile
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                for relative_name, content in ts_files.items():
                    safe_parts = [
                        ''.join(ch if ch.isalnum() or ch in {'-', '_', '.'} else '_' for ch in part)
                        for part in str(relative_name).split('/')
                        if part and part not in {'.', '..'}
                    ]
                    if safe_parts:
                        zf.writestr('/'.join(safe_parts), str(content or ''))
            response = HttpResponse(buffer.getvalue(), content_type='application/zip')
            response['Content-Disposition'] = (
                f'attachment; filename="{safe_name}_playwright_ts_bundle.zip"; '
                f"filename*=UTF-8''{urlquote(f'{safe_name}_playwright_ts_bundle.zip')}"
            )
            return response
        if artifact_type in {'python', 'py'}:
            if not task.generated_script:
                return Response({'error': '当前任务没有 Python Playwright 脚本产物'}, status=status.HTTP_404_NOT_FOUND)
            return self._download_response(task.generated_script, f'{safe_name}.py', 'text/x-python; charset=utf-8')
        if artifact_type == 'json':
            payload = {
                'task_id': task.id,
                'name': task.name,
                'status': task.status,
                'failure_category': task.failure_category,
                'test_plan': task.test_plan,
                'generated_case': task.generated_case,
                'verification_result': task.verification_result,
                'repair_history': task.repair_history,
            }
            return self._download_response(
                json.dumps(payload, ensure_ascii=False, indent=2),
                f'{safe_name}_artifact.json',
                'application/json; charset=utf-8',
            )
        return Response(
            {'error': '不支持的产物类型，可选 html、junit、json、ts、ts_bundle、python'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    @staticmethod
    def _build_ai_execution_artifact(task: UiAiGenerationTask) -> dict:
        """Build the replay artifact consumed by normal UI case execution."""
        verification = task.verification_result if isinstance(task.verification_result, dict) else {}
        script_artifacts = verification.get('script_artifacts') if isinstance(verification.get('script_artifacts'), dict) else {}
        generated_case = task.generated_case if isinstance(task.generated_case, dict) else {}
        ts_spec = (
            script_artifacts.get('playwright_ts_spec')
            or generated_case.get('playwright_ts_spec')
            or ''
        )
        ts_files = (
            script_artifacts.get('playwright_ts_files')
            if isinstance(script_artifacts.get('playwright_ts_files'), dict) else
            generated_case.get('playwright_ts_files')
            if isinstance(generated_case.get('playwright_ts_files'), dict) else {}
        )
        if ts_spec and 'generated.spec.ts' not in ts_files:
            ts_files = {**ts_files, 'generated.spec.ts': ts_spec}
        if not ts_spec and isinstance(ts_files, dict):
            ts_spec = str(ts_files.get('generated.spec.ts') or '')
        if not str(ts_spec or '').strip():
            return {}
        return {
            'source': 'ai_generation_task',
            'source_task_id': task.id,
            'source_task_name': task.name,
            'playwright_ts_spec': ts_spec,
            'playwright_ts_files': ts_files,
            'playwright_ts_spec_hash': (
                script_artifacts.get('playwright_ts_spec_hash')
                or generated_case.get('playwright_ts_spec_hash')
                or hashlib.sha256(str(ts_spec).encode('utf-8')).hexdigest()
            ),
            'script_artifacts': {
                'playwright_ts_spec': ts_spec,
                'playwright_ts_files': ts_files,
                'playwright_ts_spec_hash': (
                    script_artifacts.get('playwright_ts_spec_hash')
                    or generated_case.get('playwright_ts_spec_hash')
                    or hashlib.sha256(str(ts_spec).encode('utf-8')).hexdigest()
                ),
            },
            'generated_case': {
                **generated_case,
                'playwright_ts_spec': ts_spec,
                'playwright_ts_files': ts_files,
            },
            'verification_result': {
                'plan_validation': verification.get('plan_validation') if isinstance(verification.get('plan_validation'), dict) else {},
                'typescript_spec_status': verification.get('typescript_spec_status') or '',
            },
            'applied_at': timezone.now().isoformat(),
        }

    @staticmethod
    def _application_generated_case_and_steps(task: UiAiGenerationTask) -> tuple[dict, list[dict]]:
        """Return the most complete generated case/step payload available for UI application."""
        generated_case = task.generated_case if isinstance(task.generated_case, dict) else {}
        generated_steps = generated_case.get('steps') if isinstance(generated_case.get('steps'), list) else []
        if generated_steps:
            return generated_case, generated_steps

        verification = task.verification_result if isinstance(task.verification_result, dict) else {}
        runtime_artifact = verification.get('runtime_execution_artifact')
        if isinstance(runtime_artifact, dict):
            runtime_steps = runtime_artifact.get('steps') if isinstance(runtime_artifact.get('steps'), list) else []
            if runtime_steps:
                return {
                    **generated_case,
                    'steps': runtime_steps,
                    'source': runtime_artifact.get('artifact_type') or 'runtime_execution_artifact',
                }, runtime_steps

        runtime_agent_execution = verification.get('runtime_agent_execution')
        if isinstance(runtime_agent_execution, dict):
            runtime_steps = runtime_agent_execution.get('generated_steps')
            if isinstance(runtime_steps, list) and runtime_steps:
                return {
                    **generated_case,
                    'steps': runtime_steps,
                    'source': 'runtime_agent_execution',
                }, runtime_steps

        return generated_case, []

    @action(detail=True, methods=['get'], url_path='detail-payload')
    def detail_payload(self, request, pk=None):
        """按需返回 AI 生成任务的大字段，避免详情页默认加载超大 JSON。"""
        field_name = str(request.query_params.get('field') or '').strip()
        if field_name not in self.DETAIL_PAYLOAD_FIELDS:
            return Response({
                'error': 'field 参数不合法',
                'allowed_fields': sorted(self.DETAIL_PAYLOAD_FIELDS),
            }, status=status.HTTP_400_BAD_REQUEST)
        task = self.get_object()
        return Response({
            'field': field_name,
            'value': getattr(task, field_name),
        })

    @action(detail=True, methods=['post'], url_path='apply-to-ui-case')
    def apply_to_ui_case(self, request, pk=None):
        """把生成结果应用为 UI 自动化模块、页面步骤和测试用例。"""
        task = self.get_object()
        if task.status != 'success':
            return Response({'error': '只有成功的 AI 生成任务才能应用到 UI 自动化用例'}, status=status.HTTP_400_BAD_REQUEST)
        generated_case, generated_steps = self._application_generated_case_and_steps(task)
        if not task.generated_script and not generated_steps:
            return Response({'error': '当前任务没有可应用的生成结果'}, status=status.HTTP_400_BAD_REQUEST)

        map_pages = self._map_pages(task)
        page_snapshot = map_pages[0] if map_pages else {}
        persisted_map = task.element_map.map_json if task.element_map_id and isinstance(task.element_map.map_json, dict) else {}
        base_url = (
            task.target_url
            or (task.element_map_snapshot or {}).get('base_url')
            or persisted_map.get('base_url')
            or page_snapshot.get('url')
            or ''
        )
        module_name = task.target_module or generated_case.get('name') or task.name
        if not isinstance(generated_steps, list) or not generated_steps:
            return Response({'error': '生成结果没有可应用步骤'}, status=status.HTTP_400_BAD_REQUEST)

        resolved_raw_by_step: dict[int, tuple[dict, dict]] = {}
        unresolved_steps = []
        current_page_key = ''
        for index, raw_step in enumerate(generated_steps):
            if not isinstance(raw_step, dict):
                continue
            operation = self._normalize_plan_operation(raw_step)
            requested_page_key = self._normalized_page_key(str(raw_step.get('page_key') or current_page_key or ''))
            if operation == 'goto':
                current_page_key = requested_page_key or current_page_key
                continue
            if operation == 'wait':
                continue
            locator_hint = self._contextual_locator_hint(
                raw_step,
                raw_step.get('locator_hint') if isinstance(raw_step.get('locator_hint'), dict) else {},
            )
            element_key = str(raw_step.get('element_key') or '').strip()
            if self._is_runtime_state_step(raw_step):
                current_page_key = requested_page_key or current_page_key
                continue
            resolved = None
            # Runtime locators are complete executable bindings in their own right.
            # Do not replace them with a same-name map element of a different role.
            if not element_key and locator_hint.get('type') and locator_hint.get('value') not in (None, ''):
                target_page = next(
                    (page for page in map_pages if str(page.get('page_key') or '') == requested_page_key),
                    next(
                        (page for page in map_pages if str(page.get('page_key') or '') == current_page_key),
                        page_snapshot,
                    ),
                )
                resolved = (
                    target_page,
                    {
                        'element_key': f'runtime_step_{index + 1}',
                        'name': raw_step.get('target_name') or locator_hint.get('name') or locator_hint.get('value'),
                        'recommended_locator': locator_hint,
                        'locator_candidates': raw_step.get('locator_candidates') or [],
                    },
                )
            if not resolved:
                resolved = self._resolve_map_element_for_plan_step(raw_step, map_pages, current_page_key)
            if not resolved:
                if locator_hint.get('type') and locator_hint.get('value') not in (None, ''):
                    target_page = next(
                        (page for page in map_pages if str(page.get('page_key') or '') == requested_page_key),
                        next(
                            (page for page in map_pages if str(page.get('page_key') or '') == current_page_key),
                            page_snapshot,
                        ),
                    )
                    resolved = (
                        target_page,
                        {
                            'element_key': f'runtime_step_{index + 1}',
                            'name': raw_step.get('target_name') or locator_hint.get('name') or locator_hint.get('value'),
                            'recommended_locator': locator_hint,
                            'locator_candidates': raw_step.get('locator_candidates') or [],
                        },
                    )
            if not resolved:
                unresolved_steps.append({
                    'step_sort': raw_step.get('step_sort', index),
                    'page_key': raw_step.get('page_key') or '',
                    'element_key': raw_step.get('element_key') or '',
                    'target_name': raw_step.get('target_name') or '',
                })
                continue
            resolved_raw_by_step[index] = resolved
            current_page_key = str(resolved[0].get('page_key') or current_page_key)

        if unresolved_steps:
            return Response({
                'error': '生成步骤存在无法精确绑定的页面元素，已拒绝应用',
                'unresolved_steps': unresolved_steps,
            }, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            module = self._get_or_create_root_module(task.project_id, module_name, request.user)
            required_page_states = []
            seen_page_keys = set()
            for page_state in [page_snapshot, *(item[0] for item in resolved_raw_by_step.values())]:
                page_key = str(page_state.get('page_key') or '')
                if not page_state or page_key in seen_page_keys:
                    continue
                seen_page_keys.add(page_key)
                required_page_states.append(page_state)

            created_pages_by_key: dict[str, UiPage] = {}
            for page_state in required_page_states:
                page_key = str(page_state.get('page_key') or '')
                source_name = str(
                    page_state.get('name') or page_state.get('title') or task.target_module or task.name
                )
                page_name = f'{source_name} [{page_key}]'[:64] if page_key else source_name[:64]
                page_url = str(page_state.get('url') or base_url or '')
                page, _ = UiPage.objects.update_or_create(
                    project_id=task.project_id,
                    module=module,
                    name=page_name,
                    defaults={
                        'url': page_url,
                        'description': f'AI 生成任务 {task.id} 页面状态; page_key={page_key}',
                        'creator': request.user,
                    },
                )
                created_pages_by_key[page_key] = page

            first_page_key = str(page_snapshot.get('page_key') or '')
            page = created_pages_by_key.get(first_page_key) or next(iter(created_pages_by_key.values()))

            created_elements: list[UiElement] = []
            created_elements_by_identity: dict[tuple[str, str], UiElement] = {}
            resolved_elements_by_step: dict[int, UiElement] = {}
            for step_index, (page_state, raw_element) in resolved_raw_by_step.items():
                page_key = str(page_state.get('page_key') or '')
                element_key = str(raw_element.get('element_key') or f'runtime_step_{step_index + 1}')
                identity = (page_key, element_key)
                if identity in created_elements_by_identity:
                    resolved_elements_by_step[step_index] = created_elements_by_identity[identity]
                    continue
                locators = self._locators_for_ui_element(raw_element)
                locator_type, locator_value = locators[0]
                element_name = str(
                    raw_element.get('name') or raw_element.get('accessible_name') or raw_element.get('text') or raw_element.get('element_key') or 'AI元素'
                )[:64]
                element_page = created_pages_by_key[page_key]
                element = UiElement.objects.filter(
                    page=element_page,
                    name=element_name,
                    locator_type=locator_type,
                    locator_value=locator_value,
                ).first()
                if not element:
                    element = UiElement.objects.create(
                        page=element_page,
                        name=element_name,
                        locator_type=locator_type,
                        locator_value=locator_value,
                        locator_type_2=locators[1][0] if len(locators) > 1 else None,
                        locator_value_2=locators[1][1] if len(locators) > 1 else None,
                        locator_type_3=locators[2][0] if len(locators) > 2 else None,
                        locator_value_3=locators[2][1] if len(locators) > 2 else None,
                        description=(
                            f"AI 采集元素，来源任务 {task.id}; "
                            f"page_key={page_key}; element_key={element_key}"
                        ),
                        creator=request.user,
                    )
                else:
                    update_fields = []
                    if len(locators) > 1 and not element.locator_value_2:
                        element.locator_type_2 = locators[1][0]
                        element.locator_value_2 = locators[1][1]
                        update_fields.extend(['locator_type_2', 'locator_value_2'])
                    if len(locators) > 2 and not element.locator_value_3:
                        element.locator_type_3 = locators[2][0]
                        element.locator_value_3 = locators[2][1]
                        update_fields.extend(['locator_type_3', 'locator_value_3'])
                    if update_fields:
                        update_fields.append('updated_at')
                        element.save(update_fields=update_fields)
                created_elements.append(element)
                created_elements_by_identity[identity] = element
                resolved_elements_by_step[step_index] = element

            ai_execution_artifact = self._build_ai_execution_artifact(task)
            page_step_name = f"{task.name[:48]}-AI执行步骤"
            page_step = UiPageSteps.objects.create(
                project_id=task.project_id,
                page=page,
                module=module,
                name=page_step_name[:64],
                description='由 Playwright 原生探索 AI 生成任务应用',
                run_flow=task.generated_script or '',
                flow_data={
                    'source': 'ai_generation_task',
                    'task_id': task.id,
                    'script_hash': task.generated_script_hash,
                    'test_plan': task.test_plan,
                    'ai_execution_artifact': ai_execution_artifact,
                },
                creator=request.user,
            )

            UiPageStepsDetailed.objects.create(
                page_step=page_step,
                step_type=0,
                step_sort=0,
                ope_key='goto',
                ope_value={'url': base_url},
                description='加载目标页面',
            )
            created_detail_count = 1
            if isinstance(generated_steps, list) and generated_steps:
                UiPageStepsDetailed.objects.filter(page_step=page_step).delete()
                created_detail_count = self._create_details_from_generated_steps(
                    page_step,
                    generated_steps,
                    resolved_elements_by_step,
                    base_url,
                )
            if created_detail_count == 0:
                UiPageStepsDetailed.objects.create(
                    page_step=page_step,
                    step_type=0,
                    step_sort=0,
                    ope_key='goto',
                    ope_value={'url': base_url},
                    description='加载目标页面',
                )
                for index, element in enumerate(created_elements[:8], start=1):
                    UiPageStepsDetailed.objects.create(
                        page_step=page_step,
                        step_type=1,
                        element=element,
                        step_sort=index,
                        ope_key='assert_visible',
                        description=f'验证元素可见：{element.name}',
                    )
                created_detail_count = 1 + min(len(created_elements), 8)

            test_case = UiTestCase.objects.create(
                project_id=task.project_id,
                module=module,
                name=task.name[:255],
                description=task.source_requirement or task.gherkin or '由 Playwright 原生探索 AI 生成任务创建',
                level='P2',
                case_flow=task.generated_script or '',
                result_data={
                    'source': 'ai_generation_task',
                    'ai_generation_task_id': task.id,
                    'ai_execution_mode': 'typescript_spec' if ai_execution_artifact else 'page_steps',
                    'ai_execution_artifact': ai_execution_artifact,
                },
                creator=request.user,
            )
            UiCaseStepsDetailed.objects.create(
                test_case=test_case,
                page_step=page_step,
                case_sort=0,
                switch_step_open_url=False,
                error_retry=1,
            )

            generated_case = {
                **generated_case,
                'applied': True,
                'applied_at': timezone.now().isoformat(),
                'applied_module_id': module.id,
                'applied_page_id': page.id,
                'applied_page_ids': [item.id for item in created_pages_by_key.values()],
                'applied_page_step_id': page_step.id,
                'applied_test_case_id': test_case.id,
                'applied_element_count': len(created_elements),
                'applied_step_count': created_detail_count,
                'applied_execution_mode': 'typescript_spec' if ai_execution_artifact else 'page_steps',
                'applied_typescript_spec_hash': ai_execution_artifact.get('playwright_ts_spec_hash') if ai_execution_artifact else '',
            }
            task.generated_case = generated_case
            task.save(update_fields=['generated_case', 'updated_at'])

        return Response({
            'message': '生成结果已应用到 UI 自动化测试用例',
            'module_id': module.id,
            'page_id': page.id,
            'page_step_id': page_step.id,
            'test_case_id': test_case.id,
            'task': self.get_serializer(task).data,
        })

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        """下发 AI 生成任务到执行器。"""
        task = self.get_object()
        if task.status == 'running':
            return Response({'error': '任务正在执行中'}, status=status.HTTP_400_BAD_REQUEST)

        from asgiref.sync import async_to_sync
        from .consumers import SocketUserManager
        from .socket_models import SocketDataModel, QueueModel, NoticeType, ResponseCode, UiSocketEnum

        requested_actuator_id = (request.data.get('actuator_id') or '').strip()
        previous_actuator_id = (task.actuator_id or '').strip()
        actuator_id = requested_actuator_id or previous_actuator_id
        actuator = SocketUserManager.get_actuator_by_id(actuator_id) if actuator_id else None
        if not actuator and not requested_actuator_id:
            actuator = SocketUserManager.get_actuator()
            actuator_id = getattr(actuator, 'user_id', '') if actuator else ''
        if not actuator:
            return Response(
                {'error': f'执行器 {requested_actuator_id or previous_actuator_id} 不在线' if (requested_actuator_id or previous_actuator_id) else '没有可用的执行器，请先启动执行器服务'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        safety_policy = _merge_safety_policy(task.safety_policy)
        safety_policy = {
            **safety_policy,
            'authentication_contract': classify_task_authentication_contract(task, safety_policy),
        }
        task.status = 'running'
        task.actuator_id = actuator_id or getattr(actuator, 'user_id', '') or ''
        dispatch_id = self._build_dispatch_id(task)
        task.dispatch_id = dispatch_id
        task.dispatch_status = 'pending'
        task.dispatch_attempts = min(int(task.dispatch_attempts or 0) + 1, 255)
        task.last_dispatched_at = None
        task.last_ack_at = None
        task.last_dispatch_error = ''
        task.started_at = timezone.now()
        task.completed_at = None
        task.error_message = ''
        task.safety_policy = safety_policy
        task.current_repair_round = 0
        task.save(update_fields=[
            'status', 'actuator_id', 'dispatch_id', 'dispatch_status', 'dispatch_attempts',
            'last_dispatched_at', 'last_ack_at', 'last_dispatch_error', 'started_at',
            'completed_at', 'error_message', 'safety_policy', 'current_repair_round', 'updated_at'
        ])

        args = self._build_ai_generation_args(task, safety_policy, dispatch_id=dispatch_id)
        try:
            async_to_sync(actuator.send_json)(SocketDataModel(
                code=ResponseCode.SUCCESS,
                msg='ai_generation',
                user=request.user.username,
                is_notice=NoticeType.ACTUATOR,
                data=QueueModel(
                    func_name=UiSocketEnum.AI_GENERATION,
                    func_args=args,
                ),
            ))
        except Exception as exc:
            task.status = 'failed'
            task.dispatch_status = 'failed'
            task.last_dispatch_error = f'{type(exc).__name__}: {str(exc)[:500]}'
            task.completed_at = timezone.now()
            task.error_message = 'AI UI 生成任务下发执行器失败'
            task.save(update_fields=[
                'status', 'dispatch_status', 'last_dispatch_error',
                'completed_at', 'error_message', 'updated_at'
            ])
            return Response(
                {'error': f'AI UI 生成任务下发失败: {task.last_dispatch_error}'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        task.dispatch_status = 'sent'
        task.last_dispatched_at = timezone.now()
        task.save(update_fields=['dispatch_status', 'last_dispatched_at', 'updated_at'])
        return Response({
            'message': 'AI UI 生成任务已发送给执行器',
            'task': self.get_serializer(task).data,
        })

    @action(detail=False, methods=['get'], url_path='pending-for-actuator')
    def pending_for_actuator(self, request):
        """执行器拉取已分配但可能因 WebSocket 重连丢失的 AI 生成任务。"""
        from .socket_models import UiSocketEnum

        actuator_id = (request.query_params.get('actuator_id') or '').strip()
        if not actuator_id:
            return Response({'error': 'actuator_id 参数必填'}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        active_task_ids = {
            int(value)
            for value in str(request.query_params.get('active_task_ids') or '').split(',')
            if value.strip().isdigit()
        }
        if active_task_ids:
            self.get_queryset().filter(
                id__in=active_task_ids,
                status='running',
                actuator_id=actuator_id,
                completed_at__isnull=True,
            ).update(
                dispatch_status='acked',
                last_ack_at=now,
                updated_at=now,
            )

        stale_ack_before = now - timedelta(minutes=5)
        tasks = list(
            self.get_queryset()
            .filter(status='running', actuator_id=actuator_id, completed_at__isnull=True)
            .exclude(id__in=active_task_ids)
            .filter(
                Q(dispatch_status__in=['pending', 'sent', 'failed'])
                | Q(last_ack_at__isnull=True)
                | Q(last_ack_at__lt=stale_ack_before)
            )
            .order_by('started_at', 'id')[:5]
        )
        response_tasks = []
        for task in tasks:
            dispatch_id = self._build_dispatch_id(task)
            task.dispatch_id = dispatch_id
            task.dispatch_status = 'sent'
            task.dispatch_attempts = min(int(task.dispatch_attempts or 0) + 1, 255)
            task.last_dispatched_at = now
            task.last_dispatch_error = ''
            task.save(update_fields=[
                'dispatch_id', 'dispatch_status', 'dispatch_attempts',
                'last_dispatched_at', 'last_dispatch_error', 'updated_at'
            ])
            response_tasks.append({
                'task_id': task.id,
                'func_name': UiSocketEnum.AI_GENERATION,
                'func_args': self._build_ai_generation_args(task, task.safety_policy, dispatch_id=dispatch_id),
                'started_at': task.started_at.isoformat() if task.started_at else None,
                'updated_at': task.updated_at.isoformat() if task.updated_at else None,
            })

        return Response({
            'actuator_id': actuator_id,
            'count': len(response_tasks),
            'tasks': response_tasks,
        })


# ---------- 截图上传 ----------
import os
import uuid
from datetime import datetime
from django.conf import settings
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated


from rest_framework.permissions import AllowAny


@api_view(['POST'])
@parser_classes([MultiPartParser])
@permission_classes([IsAuthenticated])
def upload_ai_artifact(request):
    """上传 AI 生成任务执行产物，返回可访问 URL。"""
    file = request.FILES.get('file')
    if not file:
        return Response({'error': '未提供文件'}, status=status.HTTP_400_BAD_REQUEST)

    artifact_type = (request.data.get('type') or 'artifact').strip().lower()
    safe_type = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '_' for ch in artifact_type)[:40] or 'artifact'
    date_dir = datetime.now().strftime('%Y%m%d')
    upload_dir = os.path.join(settings.MEDIA_ROOT, 'ui_ai_artifacts', date_dir)
    os.makedirs(upload_dir, exist_ok=True)

    ext = os.path.splitext(file.name)[1] or '.bin'
    filename = f"{safe_type}_{uuid.uuid4().hex[:12]}{ext}"
    file_path = os.path.join(upload_dir, filename)

    with open(file_path, 'wb') as f:
        for chunk in file.chunks():
            f.write(chunk)

    relative_path = f"ui_ai_artifacts/{date_dir}/{filename}"
    url = f"{settings.MEDIA_URL}{relative_path}"
    return Response({
        'status': 'success',
        'url': url,
        'path': relative_path,
        'type': safe_type,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@parser_classes([MultiPartParser])
@permission_classes([IsAuthenticated])
def upload_screenshot(request):
    """上传执行截图，返回可访问 URL

    注意：此接口使用 Bearer Token 认证
    执行器通过 /api/token/ 获取 JWT Token 后调用此接口
    """
    file = request.FILES.get('file')
    if not file:
        return Response({'error': '未提供文件'}, status=status.HTTP_400_BAD_REQUEST)

    # 保存到 media/ui_screenshots/{日期}/
    date_dir = datetime.now().strftime('%Y%m%d')
    upload_dir = os.path.join(settings.MEDIA_ROOT, 'ui_screenshots', date_dir)
    os.makedirs(upload_dir, exist_ok=True)

    # 生成唯一文件名
    ext = os.path.splitext(file.name)[1] or '.png'
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    file_path = os.path.join(upload_dir, filename)

    with open(file_path, 'wb') as f:
        for chunk in file.chunks():
            f.write(chunk)

    url = f"{settings.MEDIA_URL}ui_screenshots/{date_dir}/{filename}"
    return Response({'status': 'success', 'url': url}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@parser_classes([MultiPartParser])
@permission_classes([IsAuthenticated])
def upload_trace(request):
    """上传 Playwright Trace 文件，返回可访问 URL

    注意：此接口使用 Bearer Token 认证
    执行器执行完成后调用此接口上传 trace.zip 文件
    """
    file = request.FILES.get('file')
    if not file:
        return Response({'error': '未提供文件'}, status=status.HTTP_400_BAD_REQUEST)

    # 保存到 media/ui_traces/{日期}/
    date_dir = datetime.now().strftime('%Y%m%d')
    upload_dir = os.path.join(settings.MEDIA_ROOT, 'ui_traces', date_dir)
    os.makedirs(upload_dir, exist_ok=True)

    # 生成唯一文件名
    ext = os.path.splitext(file.name)[1] or '.zip'
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    file_path = os.path.join(upload_dir, filename)

    with open(file_path, 'wb') as f:
        for chunk in file.chunks():
            f.write(chunk)

    # 返回相对路径（用于存储到数据库）和 URL（用于下载）
    relative_path = f"ui_traces/{date_dir}/{filename}"
    url = f"{settings.MEDIA_URL}{relative_path}"
    return Response({
        'status': 'success',
        'url': url,
        'path': relative_path
    }, status=status.HTTP_201_CREATED)


# ---------- 内部触发批量执行（供 Celery 任务调用） ----------
from asgiref.sync import async_to_sync


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def trigger_batch_execution(request):
    """内部 API：创建批量执行记录并通过 WebSocket 发送给执行器

    请求体:
        case_ids: list[int] - 用例 ID 列表
        actuator_id: str - 执行器 ID
        batch_name: str - 批次名称（可选）
        trigger_type: str - 触发类型（默认 scheduled）
    """
    from .consumers import SocketUserManager
    from .socket_models import SocketDataModel, QueueModel, NoticeType, ResponseCode, UiSocketEnum

    case_ids = request.data.get('case_ids', [])
    actuator_id = request.data.get('actuator_id', '')
    batch_name = request.data.get('batch_name', '')
    trigger_type = request.data.get('trigger_type', 'scheduled')

    if not case_ids:
        return Response({'error': '未提供用例 ID'}, status=status.HTTP_400_BAD_REQUEST)

    # 查找执行器
    if actuator_id:
        actuator = SocketUserManager.get_actuator_by_id(actuator_id)
    else:
        actuator = SocketUserManager.get_actuator()

    if not actuator:
        return Response(
            {'error': f'执行器 {actuator_id} 不在线' if actuator_id else '没有可用的执行器'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    # 创建批量执行记录
    from django.utils import timezone as tz
    case_names = list(UiTestCase.objects.filter(id__in=case_ids).values_list('name', flat=True)[:3])
    if not batch_name:
        batch_name = f"定时任务: {', '.join(case_names)}"
        if len(case_ids) > 3:
            batch_name += f" 等{len(case_ids)}个用例"

    batch = UiBatchExecutionRecord.objects.create(
        name=batch_name,
        total_cases=len(case_ids),
        status=1,
        trigger_type=trigger_type,
        executor=request.user,
        start_time=tz.now(),
    )

    args = {
        'case_ids': case_ids,
        'actuator_id': actuator_id,
        'batch_id': batch.id,
    }

    # 通过 WebSocket 发送给执行器
    async_to_sync(actuator.send_json)(SocketDataModel(
        code=ResponseCode.SUCCESS,
        msg='execute_batch',
        user='system',
        is_notice=NoticeType.ACTUATOR,
        data=QueueModel(
            func_name=UiSocketEnum.TEST_CASE_BATCH,
            func_args=args,
        ),
    ))

    return Response({
        'status': 'success',
        'data': {'batch_id': batch.id, 'total_cases': len(case_ids)},
    })
