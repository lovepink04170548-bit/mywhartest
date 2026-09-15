# -*- coding: utf-8 -*-
"""AI UI 生成任务的 LLM 意图理解和步骤规划。"""

import json
import logging
import os
import re
import time
from collections import defaultdict
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from langgraph_integration.models import LLMConfig
from requirements.services import (
    extract_json_from_response,
)
from ui_automation.planning_contract import (
    BindingContract,
    RequirementContract,
    binding_key_matches,
    build_requirement_contract,
    complete_requirement_contract as _contract_complete_requirement_contract,
    exploration_coverage_from_bindings as _contract_exploration_coverage_from_bindings,
    RouteContract,
    staged_binding_contract_issues as _contract_staged_binding_contract_issues,
    staged_plan_contract_issues as _contract_staged_plan_contract_issues,
    build_requirement_document as _build_requirement_document,
    is_assertion_operation,
    standardize_staged_contract_steps as _contract_standardize_staged_contract_steps,
    standardize_unresolved_gap_steps as _contract_standardize_unresolved_gap_steps,
)

logger = logging.getLogger(__name__)

ALLOWED_OPERATIONS = {
    'goto', 'fill', 'click', 'select_option', 'check', 'uncheck', 'wait', 'wait_and_reobserve',
    'assert_visible', 'assert_text', 'assert_contain_text', 'assert_value',
}

OPERATION_ALIASES = {
    'navigate': 'goto',
    'open': 'goto',
    'input': 'fill',
    'type': 'fill',
    'select': 'select_option',
    'assert': 'assert_visible',
    'visible': 'assert_visible',
    'contains': 'assert_contain_text',
}


def _normalize_text(value: Any) -> str:
    return ' '.join(str(value or '').strip().lower().split())


AUTHENTICATION_MODES = {'test_subject', 'prerequisite', 'authentication_then_business', 'unknown'}
EXECUTION_MODES = {'contract_planner', 'runtime_agent'}


def _valid_authentication_mode(value: Any) -> str:
    mode = _normalize_text(value)
    return mode if mode in AUTHENTICATION_MODES else ''


def _task_authentication_mode(task) -> str:
    explicit_mode = _valid_authentication_mode(getattr(task, 'authentication_mode', ''))
    if explicit_mode:
        return explicit_mode
    safety_policy = getattr(task, 'safety_policy', None)
    if isinstance(safety_policy, dict):
        contract = safety_policy.get('authentication_contract') if isinstance(safety_policy.get('authentication_contract'), dict) else {}
        contract_mode = _valid_authentication_mode(contract.get('mode'))
        if contract_mode:
            return contract_mode
        policy_mode = _valid_authentication_mode(safety_policy.get('authentication_mode'))
        if policy_mode:
            return policy_mode
    return 'unknown'


def _normalize_execution_mode(value: Any) -> str:
    mode = _normalize_text(value)
    if mode in {'runtime_agent', 'runtime', 'agent', 'runtime-mode', 'runtime_agent_mode'}:
        return 'runtime_agent'
    if mode in {'contract_planner', 'contract-planner', 'contract', 'planner', 'static', 'default'}:
        return 'contract_planner'
    return ''


def _task_execution_mode(task) -> str:
    safety_policy = getattr(task, 'safety_policy', None)
    if isinstance(safety_policy, dict):
        mode = _normalize_execution_mode(safety_policy.get('execution_mode'))
        if mode:
            return mode
    return 'contract_planner'


def _build_element_indexes(element_map: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_key: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}

    def add_element(
        page_key: str,
        element: dict[str, Any],
        extra: dict[str, Any] | None = None,
        *,
        overwrite: bool = True,
    ) -> None:
        enriched = {**element, **(extra or {}), 'page_key': element.get('page_key') or page_key}
        element_key = str(enriched.get('element_key') or '')
        frame_key = str(enriched.get('frame_key') or '')
        if element_key:
            if page_key:
                key = f'{page_key}:{element_key}'
                if overwrite:
                    by_key[key] = enriched
                else:
                    by_key.setdefault(key, enriched)
            if page_key and frame_key:
                key = f'{page_key}:{frame_key}:{element_key}'
                if overwrite:
                    by_key[key] = enriched
                else:
                    by_key.setdefault(key, enriched)
            if frame_key:
                key = f'{frame_key}:{element_key}'
                if overwrite:
                    by_key[key] = enriched
                else:
                    by_key.setdefault(key, enriched)
            if overwrite:
                by_key[element_key] = enriched
            else:
                by_key.setdefault(element_key, enriched)
        for value in [
            enriched.get('name'),
            enriched.get('accessible_name'),
            enriched.get('text'),
            enriched.get('label'),
            enriched.get('placeholder'),
            enriched.get('test_id'),
        ]:
            normalized = _normalize_text(value)
            if normalized:
                by_name.setdefault(normalized, enriched)

    for page in element_map.get('pages') or []:
        if not isinstance(page, dict):
            continue
        page_key = page.get('page_key') or ''
        for element in page.get('elements') or []:
            if not isinstance(element, dict):
                continue
            add_element(page_key, element, {'context_type': 'main'})
        for frame in page.get('frames') or []:
            if not isinstance(frame, dict):
                continue
            frame_key = frame.get('frame_key') or ''
            for element in frame.get('elements') or []:
                if not isinstance(element, dict):
                    continue
                add_element(page_key, element, {
                    'context_type': 'frame',
                    'frame_key': frame_key,
                    'frame_name': frame.get('name') or '',
                    'frame_url': frame.get('url') or '',
                })
        shadow_dom = page.get('shadow_dom') if isinstance(page.get('shadow_dom'), dict) else {}
        for element in shadow_dom.get('elements') or []:
            if not isinstance(element, dict):
                continue
            add_element(page_key, element, {
                'context_type': 'shadow',
                'host_path': element.get('host_path') or [],
            })
    for transition in element_map.get('state_transitions') or []:
        if not isinstance(transition, dict):
            continue
        locator = transition.get('locator_hint') if isinstance(transition.get('locator_hint'), dict) else {}
        element_key = str(transition.get('element_key') or '').strip()
        page_key = str(transition.get('from_page_key') or '').strip()
        if not element_key or not page_key or not locator.get('type'):
            continue
        target_name = transition.get('target_name') or transition.get('name') or ''
        operation = str(transition.get('operation') or 'click').strip().lower() or 'click'
        synthetic_element = {
            'element_key': element_key,
            'name': target_name,
            'accessible_name': target_name,
            'text': target_name,
            'actions': [operation],
            'recommended_locator': locator,
            'locator_candidates': [locator],
            'state_transition': {
                'stage': transition.get('stage') or '',
                'from_page_key': page_key,
                'to_page_key': transition.get('to_page_key') or '',
                'to_url': transition.get('to_url') or '',
            },
        }
        if locator.get('type') == 'role':
            synthetic_element['role'] = locator.get('value') or locator.get('role') or ''
        add_element(page_key, synthetic_element, {'context_type': 'state_transition'}, overwrite=False)
    return by_key, by_name


def _element_key_variants(page_key: str, element_key: str) -> list[str]:
    page_key = str(page_key or '').strip()
    element_key = str(element_key or '').strip()
    if not element_key:
        return []
    variants = [element_key]
    if '::' in element_key:
        frame_key, inner_key = element_key.split('::', 1)
        variants.extend([
            inner_key,
            f'{frame_key}:{inner_key}',
            f'{page_key}:{frame_key}:{inner_key}' if page_key else '',
            f'{page_key}:{inner_key}' if page_key else '',
        ])
    elif page_key:
        variants.append(f'{page_key}:{element_key}')
    return [item for item in dict.fromkeys(variants) if item]


def _lookup_bound_element(by_key: dict[str, dict[str, Any]], page_key: str, element_key: str) -> dict[str, Any] | None:
    for key in _element_key_variants(page_key, element_key):
        element = by_key.get(key)
        if element is not None:
            return element
    return None


def _binding_key_matches(actual_page: Any, actual_element: Any, expected_page: Any, expected_element: Any) -> bool:
    actual_page_text = str(actual_page or '').strip()
    expected_page_text = str(expected_page or '').strip()
    if actual_page_text != expected_page_text:
        return False
    actual_variants = set(_element_key_variants(actual_page_text, str(actual_element or '')))
    expected_variants = set(_element_key_variants(expected_page_text, str(expected_element or '')))
    return bool(actual_variants and expected_variants and actual_variants.intersection(expected_variants))


def _unresolved_action_ids(bindings: dict[str, Any]) -> set[str]:
    return {
        str(item.get('action_id'))
        for item in (bindings.get('unresolved') or [])
        if isinstance(item, dict) and item.get('action_id') and item.get('required', True)
    }


UNRESOLVED_GAP_ACTIONS = {
    'unresolved_required_gap',
    'unresolved_gap',
    'unexecutable_gap',
    'required_action_unresolved',
}


def _is_unresolved_gap_step(step: dict[str, Any]) -> bool:
    action = str(step.get('action') or '').strip().lower()
    if action in UNRESOLVED_GAP_ACTIONS:
        return True
    if action.startswith('unresolved_') and action.endswith('_gap'):
        return True
    if action.startswith('unexecutable_') and action.endswith('_gap'):
        return True
    return False


def _locator_hint(element: dict[str, Any]) -> dict[str, Any]:
    locator = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {}
    return {
        'type': locator.get('type'),
        'value': locator.get('value'),
        'name': locator.get('name'),
        'confidence': locator.get('confidence'),
    }


def _row_scope(element: dict[str, Any]) -> dict[str, Any]:
    row_text = str(element.get('row_text') or '').strip()
    row_cells = element.get('row_cells') if isinstance(element.get('row_cells'), list) else []
    if not row_text and row_cells:
        row_text = ' '.join(
            str(cell.get('text') or '').strip()
            for cell in row_cells
            if isinstance(cell, dict) and str(cell.get('text') or '').strip()
        )
    if not row_text:
        return {}
    result: dict[str, Any] = {
        'row_text': row_text[:600],
        'row_cells': row_cells[:24],
    }
    for key in ['row_index', 'column_index', 'column_name', 'column_text', 'table_name']:
        if element.get(key) not in (None, '', [], {}):
            result[key] = element.get(key)
    return result


def _compact_locator(locator: dict[str, Any]) -> dict[str, Any]:
    return {
        'type': locator.get('type'),
        'value': locator.get('value'),
        'name': locator.get('name'),
        'confidence': locator.get('confidence'),
    }


def _locator_identity(locator: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(locator.get('type') or '').strip(),
        str(locator.get('value') or '').strip(),
        str(locator.get('name') or '').strip(),
    )


def _llm_locator_allowed_for_element(locator: Any, element: dict[str, Any]) -> dict[str, Any] | None:
    """只接受 LLM 从该元素候选集中选择的 locator，避免模型臆造选择器。"""
    if not isinstance(locator, dict) or not locator.get('type'):
        return None
    allowed = []
    recommended = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else None
    if recommended:
        allowed.append(recommended)
    allowed.extend(item for item in (element.get('locator_candidates') or []) if isinstance(item, dict))
    wanted = _locator_identity(locator)
    for candidate in allowed:
        candidate_identity = _locator_identity(candidate)
        if candidate_identity == wanted:
            return _compact_locator({**candidate, **locator})
        if (
            wanted[0] == candidate_identity[0]
            and wanted[1] == candidate_identity[1]
            and (not wanted[2] or wanted[2] == candidate_identity[2])
        ):
            return _compact_locator({**candidate, **locator})
    return None


def _infer_test_value(element: dict[str, Any]) -> str:
    for option in (element.get('dynamic_options') or element.get('options') or []):
        if not isinstance(option, dict):
            continue
        label = str(option.get('label') or '').strip()
        value = str(option.get('value') or '').strip()
        if label and (label == '请选择' or label.startswith('请选择')):
            continue
        if value:
            return value
        if label:
            return label
    text = ' '.join(str(element.get(key) or '') for key in ['name', 'accessible_name', 'label', 'placeholder']).lower()
    if any(word in text for word in ['手机号', '手机', '电话', 'mobile', 'phone']):
        return '13800138000'
    if any(word in text for word in ['邮箱', 'email', 'mail']):
        return 'test@example.com'
    if any(word in text for word in ['数量', '金额', '价格', 'number', 'amount', 'price']):
        return '1'
    if any(word in text for word in ['日期', '时间', 'date', 'time']):
        return '2026-07-31'
    if any(word in text for word in ['名称', '标题', '任务', 'name', 'title', 'subject']):
        return '自动化测试数据'
    return '测试数据'


def _env_ai_generation_config(task) -> dict[str, Any]:
    env_config = getattr(task, 'environment_config', None)
    extra_config = getattr(env_config, 'extra_config', None) if env_config is not None else None
    if not isinstance(extra_config, dict):
        return {}
    ai_config = extra_config.get('ai_generation')
    return ai_config if isinstance(ai_config, dict) else {}


def _test_data_key(step: dict[str, Any]) -> str:
    raw = str(step.get('target_name') or step.get('element_key') or f"step_{step.get('step_sort', 0)}").strip()
    normalized = ''.join(ch if ch.isalnum() else '_' for ch in raw).strip('_')
    return normalized[:48] or f"step_{step.get('step_sort', 0)}"


def _build_data_lifecycle(task, steps: list[dict[str, Any]], test_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成测试数据、seed 和 cleanup 计划，供 TS fixture 模板和报告审计使用。"""
    test_plan = test_plan if isinstance(test_plan, dict) else {}
    ai_config = _env_ai_generation_config(task)
    configured_test_data = ai_config.get('test_data') if isinstance(ai_config.get('test_data'), dict) else {}
    generated_values: dict[str, Any] = {}
    runtime_inputs: list[dict[str, Any]] = []

    for step in steps:
        if not isinstance(step, dict) or step.get('operation') not in {'fill', 'select_option'}:
            continue
        key = _test_data_key(step)
        if step.get('binding_mode') == 'runtime_input':
            resolver = str(step.get('runtime_resolver') or 'image_ocr')
            step['test_data_key'] = key
            step['value'] = ''
            runtime_inputs.append({
                'key': key,
                'resolver': resolver,
                'page_key': step.get('page_key') or '',
                'element_key': step.get('element_key') or '',
                'target_name': step.get('target_name') or '',
            })
            generated_values[key] = {
                'value': None,
                'source': f'runtime_input:{resolver}',
                'page_key': step.get('page_key') or '',
                'element_key': step.get('element_key') or '',
                'target_name': step.get('target_name') or '',
            }
            continue
        value = configured_test_data.get(key, step.get('value') or f"自动化测试数据_{int(time.time())}")
        step['test_data_key'] = key
        step['value'] = value
        generated_values[key] = {
            'value': value,
            'source': 'environment_config.extra_config.ai_generation.test_data' if key in configured_test_data else 'generated_from_element_map',
            'page_key': step.get('page_key') or '',
            'element_key': step.get('element_key') or '',
            'target_name': step.get('target_name') or '',
        }

    seed_actions = ai_config.get('seed_actions') if isinstance(ai_config.get('seed_actions'), list) else []
    cleanup_actions = ai_config.get('cleanup_actions') if isinstance(ai_config.get('cleanup_actions'), list) else []
    plan_cleanup = test_plan.get('cleanup') if isinstance(test_plan.get('cleanup'), list) else []
    manual_cleanup_notes = [
        str(item)
        for item in plan_cleanup
        if not isinstance(item, dict) and str(item or '').strip()
    ]
    cleanup_actions = [
        *[item for item in cleanup_actions if isinstance(item, dict)],
        *[item for item in plan_cleanup if isinstance(item, dict)],
    ]

    return {
        'strategy': ai_config.get('data_strategy') or 'isolated_generated_values',
        'test_data': {
            key: item['value']
            for key, item in generated_values.items()
            if item.get('source', '').startswith('runtime_input:') is False
        },
        'generated_values': generated_values,
        'runtime_inputs': runtime_inputs,
        'seed_actions': [item for item in seed_actions if isinstance(item, dict)],
        'cleanup_actions': cleanup_actions,
        'manual_cleanup_notes': manual_cleanup_notes,
        'cleanup_required': bool(cleanup_actions or manual_cleanup_notes),
        'notes': [
            '测试数据由元素地图必填字段、LLM 规划和环境配置共同生成',
            'cleanup_actions 支持 api_delete/api_post/api_patch/goto 等可执行清理动作',
        ],
    }


DEFAULT_COMPACT_MAX_PAGES = 20
DEFAULT_COMPACT_MAX_ELEMENTS = 2000
MAX_COMPACT_MAX_ELEMENTS = 10000
DEFAULT_LLM_PROMPT_CHAR_BUDGET = 120000
MIN_COMPACT_MAX_ELEMENTS = 160
DEFAULT_LLM_PLAN_REQUEST_TIMEOUT = 75
DEFAULT_LLM_PLAN_TOTAL_BUDGET = 90
DEFAULT_LLM_PLAN_MAX_RETRIES = 1
DEFAULT_ASYNC_LLM_PLAN_REQUEST_TIMEOUT = 240
DEFAULT_ASYNC_LLM_PLAN_TOTAL_BUDGET = 600
DEFAULT_ASYNC_LLM_PLAN_MAX_RETRIES = 2


def _safe_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _extract_flow_keywords(task_text: str) -> list[str]:
    """从当前测试目标中提取用于元素地图裁剪排序的业务关键词。"""
    normalized = str(task_text or '')
    keywords: list[str] = []

    def add(value: Any) -> None:
        item = str(value or '').strip(' "\'“”‘’[]【】()（）,.，。;；')
        if len(item) >= 2 and item not in keywords:
            keywords.append(item[:40])

    for pattern in [
        r'[“"\'【]([^”"\'】]{2,40})[”"\'】]',
        r'([\u4e00-\u9fa5A-Za-z0-9_]{2,24})(?:页面|模块|菜单|列表|弹窗|字段|按钮|下拉|单选|复选)',
        r'([\u4e00-\u9fa5A-Za-z0-9_]{0,16}(?:任务|用例|环境|报告|方式|名称|标题))',
    ]:
        for match in re.finditer(pattern, normalized, re.IGNORECASE):
            add(match.group(1))

    return keywords[:40]


def _compact_limits_for_task(task: Any | None, max_pages: int | None, max_elements: int | None) -> tuple[int, int]:
    ai_config = _env_ai_generation_config(task) if task is not None else {}
    configured_pages = ai_config.get('compact_max_pages')
    configured_elements = ai_config.get('compact_max_elements')
    try:
        resolved_pages = int(max_pages or configured_pages or DEFAULT_COMPACT_MAX_PAGES)
    except (TypeError, ValueError):
        resolved_pages = DEFAULT_COMPACT_MAX_PAGES
    try:
        resolved_elements = int(max_elements or configured_elements or DEFAULT_COMPACT_MAX_ELEMENTS)
    except (TypeError, ValueError):
        resolved_elements = DEFAULT_COMPACT_MAX_ELEMENTS
    return (
        max(1, min(resolved_pages, 20)),
        max(MIN_COMPACT_MAX_ELEMENTS, min(resolved_elements, MAX_COMPACT_MAX_ELEMENTS)),
    )


def _llm_prompt_char_budget(task: Any | None) -> int:
    ai_config = _env_ai_generation_config(task) if task is not None else {}
    configured = ai_config.get('llm_prompt_char_budget') or os.environ.get('AI_UI_LLM_PROMPT_CHAR_BUDGET')
    return max(12000, min(_safe_positive_int(configured, DEFAULT_LLM_PROMPT_CHAR_BUDGET), 120000))


def _prompt_char_count(messages: list[Any]) -> int:
    return sum(len(str(getattr(message, 'content', '') or '')) for message in messages)


def _element_compaction_priority(element: dict[str, Any], task_text: str = '', flow_keywords: list[str] | None = None) -> int:
    """压缩 LLM 上下文时优先保留业务表单和弹窗关键元素。"""
    actions = element.get('actions') or []
    text = _normalize_text(' '.join(str(element.get(key) or '') for key in [
        'name', 'accessible_name', 'text', 'label', 'form_label', 'placeholder', 'dialog_name',
    ]))
    task_text = _normalize_text(task_text)
    flow_keywords = flow_keywords or []
    score = 0
    if element.get('in_dialog') or element.get('dialog_name'):
        score += 300
    if element.get('required'):
        score += 260
    if 'fill' in actions or 'select_option' in actions:
        score += 220
    if 'click' in actions and any(keyword in text for keyword in ['保存', '提交', '确定', '确认', 'save', 'submit', 'confirm']):
        score += 180
    if element.get('label') or element.get('form_label') or element.get('placeholder'):
        score += 90
    if any(action in actions for action in ['fill', 'select_option', 'check']) and (
        element.get('label') or element.get('form_label') or element.get('placeholder')
    ):
        score += 80
    for keyword in flow_keywords:
        normalized_keyword = _normalize_text(keyword)
        if not normalized_keyword:
            continue
        if normalized_keyword in text or text in normalized_keyword:
            score += 140 + min(len(normalized_keyword), 20)
        elif normalized_keyword in task_text and any(part and part in text for part in re.split(r'[\s_/，,。；;:：]+', normalized_keyword)):
            score += 45
    locator = element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {}
    if locator.get('type') in {'label', 'placeholder', 'test_id', 'role'}:
        score += 25
    return score


def _page_element_text(page: dict[str, Any]) -> str:
    return ' '.join(
        str(element.get(key) or '')
        for element in (page.get('elements') or [])
        if isinstance(element, dict)
        for key in ['name', 'accessible_name', 'text', 'label', 'form_label', 'placeholder', 'dialog_name']
    )


def _latest_runtime_page_text(element_map: dict[str, Any]) -> str:
    pages = [
        page for page in (element_map.get('pages') or [])
        if isinstance(page, dict)
        and (page.get('runtime_fresh') or page.get('capture_source') == 'failure_reobserve')
    ]
    if not pages:
        return ''
    page = pages[0]
    return _normalize_text(' '.join([
        str(page.get('title') or ''),
        str(page.get('name') or ''),
        str(page.get('url') or ''),
        _page_element_text(page),
    ]))


def _runtime_assertion_text_supported(step: dict[str, Any], runtime_text: str) -> bool:
    expected = _normalize_text(
        step.get('value')
        or (step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}).get('value')
        or step.get('expected')
        or ''
    )
    if not expected or not runtime_text:
        return True
    return expected in runtime_text


def _runtime_assertion_expected_text(step: dict[str, Any]) -> str:
    return str(
        step.get('value')
        or (step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}).get('value')
        or step.get('expected')
        or ''
    ).strip()


def _is_runtime_state_assertion(step: dict[str, Any]) -> bool:
    return bool(
        isinstance(step, dict)
        and step.get('binding_mode') == 'runtime_state'
        and is_assertion_operation(_plan_step_operation(step))
    )


def _runtime_state_page_supported(step: dict[str, Any], element_map: dict[str, Any]) -> bool:
    page_key = str(step.get('page_key') or '').strip()
    pages = [
        page for page in (element_map.get('pages') or [])
        if isinstance(page, dict) and (not page_key or str(page.get('page_key') or '') == page_key)
    ]
    if not pages:
        return False
    return any(
        bool(page.get('title') or page.get('name') or page.get('url'))
        or any(isinstance(element, dict) for element in (page.get('elements') or []))
        or any(isinstance(frame, dict) for frame in (page.get('frames') or []))
        for page in pages
    )


def _element_box(element: dict[str, Any]) -> dict[str, float]:
    box = element.get('bounding_box') if isinstance(element.get('bounding_box'), dict) else {}
    result: dict[str, float] = {}
    for key in ['x', 'y', 'width', 'height']:
        try:
            result[key] = float(box.get(key) or 0)
        except (TypeError, ValueError):
            result[key] = 0
    return result


def _runtime_assertion_reveal_candidates(
    step: dict[str, Any],
    steps: list[dict[str, Any]],
    element_map: dict[str, Any],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Find low-risk current-page controls that can reveal text for a runtime assertion.

    The matcher is intentionally structural: it relies on observed actionability,
    page/form proximity, element geometry and locator evidence, not on fixed UI words.
    """
    if not isinstance(step, dict) or not isinstance(element_map, dict):
        return []
    step_sort = step.get('step_sort')
    try:
        current_sort = int(step_sort)
    except (TypeError, ValueError):
        current_sort = 10**9
    def before_current(item: dict[str, Any]) -> bool:
        if current_sort == 10**9:
            return True
        try:
            return int(item.get('step_sort') or -1) < current_sort
        except (TypeError, ValueError):
            return False
    previous_bound = next((
        item for item in reversed(steps)
        if isinstance(item, dict)
        and item is not step
        and item.get('element_key')
        and item.get('page_key')
        and before_current(item)
    ), None)
    by_key, _ = _build_element_indexes(element_map)
    anchor = None
    anchor_page_key = ''
    if previous_bound:
        anchor_page_key = str(previous_bound.get('page_key') or '')
        anchor = _lookup_bound_element(
            by_key,
            anchor_page_key,
            str(previous_bound.get('element_key') or ''),
        )
    pages = [
        page for page in (element_map.get('pages') or [])
        if isinstance(page, dict)
    ]
    if anchor_page_key:
        pages.sort(key=lambda page: 0 if str(page.get('page_key') or '') == anchor_page_key else 1)
    else:
        pages.sort(key=lambda page: 0 if page.get('runtime_fresh') or page.get('capture_source') == 'failure_reobserve' else 1)

    target_text = _normalize_text(' '.join([
        str(step.get('target_name') or ''),
        str(step.get('value') or ''),
        str(step.get('expected') or ''),
    ]))
    anchor_box = _element_box(anchor) if isinstance(anchor, dict) else {}
    scored: list[tuple[float, int, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for page_index, page in enumerate(pages):
        page_key = str(page.get('page_key') or '')
        for element in page.get('elements') or []:
            if not isinstance(element, dict):
                continue
            element_key = str(element.get('element_key') or '')
            if not element_key or (page_key, element_key) in seen:
                continue
            seen.add((page_key, element_key))
            actions = {str(action).strip().lower() for action in (element.get('actions') or [])}
            role = str(element.get('role') or '').strip().lower()
            tag = str(element.get('tag') or '').strip().lower()
            if 'click' not in actions:
                continue
            if not element.get('visible', True) or element.get('enabled') is False:
                continue
            if role in {'textbox', 'combobox', 'checkbox', 'radio', 'switch'} or tag in {'input', 'textarea', 'select'}:
                continue
            if not (
                role in {'button', 'link'}
                or tag in {'button', 'a'}
                or isinstance(element.get('recommended_locator'), dict)
            ):
                continue
            href = str(element.get('href') or '').strip().lower()
            if href and not (
                href.startswith('#')
                or href.startswith('javascript:')
            ):
                continue
            locator = _locator_hint(element)
            if not locator.get('type'):
                continue
            text = _normalize_text(' '.join(str(element.get(key) or '') for key in [
                'name', 'text', 'label', 'accessible_name', 'aria_label', 'form_label',
            ]))
            if not text:
                continue
            box = _element_box(element)
            if not box.get('width') or not box.get('height'):
                continue
            score = 0.0
            if target_text:
                score += _text_overlap_score(target_text, text) * 8
            if page_key == anchor_page_key:
                score += 120
            if element.get('in_form'):
                score += 60
            if anchor_box:
                anchor_center_x = anchor_box.get('x', 0) + anchor_box.get('width', 0) / 2
                anchor_center_y = anchor_box.get('y', 0) + anchor_box.get('height', 0) / 2
                center_x = box.get('x', 0) + box.get('width', 0) / 2
                center_y = box.get('y', 0) + box.get('height', 0) / 2
                dx = abs(center_x - anchor_center_x)
                dy = abs(center_y - anchor_center_y)
                if center_y >= anchor_box.get('y', 0) - 16 and dy <= 180:
                    score += max(0, 180 - dy)
                if dx <= max(360, anchor_box.get('width', 0) * 2):
                    score += max(0, 80 - dx / 6)
            score -= page_index * 25
            if score <= 0:
                continue
            scored.append((score, page_index, {
                'page_key': page_key,
                'element_key': element_key,
                'locator_hint': locator,
                'locator_candidates': [
                    candidate for candidate in [
                        locator,
                        element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else None,
                        *((element.get('locator_candidates') or [])[:6]),
                    ]
                    if isinstance(candidate, dict) and candidate.get('type') and candidate.get('value')
                ],
                'target_name': element.get('name') or element.get('text') or element.get('accessible_name') or '',
                'score': round(score, 3),
            }))
    scored.sort(key=lambda item: item[0], reverse=True)
    candidates: list[dict[str, Any]] = []
    locator_seen: set[tuple[str, str, str]] = set()
    for _score, _page_index, candidate in scored:
        locator = candidate.get('locator_hint') if isinstance(candidate.get('locator_hint'), dict) else {}
        identity = (
            str(candidate.get('page_key') or ''),
            str(locator.get('type') or ''),
            str(locator.get('value') or ''),
        )
        if identity in locator_seen:
            continue
        locator_seen.add(identity)
        candidates.append(candidate)
        if len(candidates) >= max(0, limit):
            break
    return candidates


def _page_flow_priority(page: dict[str, Any], task_text: str, flow_keywords: list[str] | None = None) -> int:
    elements = [element for element in (page.get('elements') or []) if isinstance(element, dict)]
    if not elements:
        return 0
    flow_keywords = flow_keywords or []
    page_text = _normalize_text(_page_element_text(page))
    score = max(_element_compaction_priority(element, task_text, flow_keywords) for element in elements)
    if page.get('runtime_fresh') or page.get('capture_source') == 'failure_reobserve':
        score += 1200
    score += min(
        sum(_element_compaction_priority(element, task_text, flow_keywords) for element in elements) // 8,
        350,
    )
    if any(element.get('required') or element.get('inferred_required') for element in elements):
        score += 180
    if any(element.get('in_dialog') or element.get('dialog_name') or str(element.get('role') or '').lower() == 'dialog' for element in elements):
        score += 160
    if any(
        any(action in (element.get('actions') or []) for action in ['fill', 'select_option', 'check'])
        for element in elements
    ):
        score += 140
    for keyword in flow_keywords:
        normalized = _normalize_text(keyword)
        if normalized and normalized in page_text:
            score += 120 + min(len(normalized), 20)
    if any(keyword in page_text for keyword in ['登录', '密码', '用户名', 'login', 'password']):
        score -= 260
    return score


def _compact_element_map(
    element_map: dict[str, Any],
    max_pages: int | None = None,
    max_elements: int | None = None,
    task: Any | None = None,
) -> dict[str, Any]:
    pages = []
    required_fields = []
    max_pages, max_elements = _compact_limits_for_task(task, max_pages, max_elements)
    remaining = max_elements
    task_text = ' '.join(str(value or '') for value in [
        getattr(task, 'name', ''),
        getattr(task, 'source_requirement', ''),
        getattr(task, 'gherkin', ''),
        getattr(task, 'target_module', ''),
    ])
    flow_keywords = _extract_flow_keywords(task_text)
    explicit_requirements = _extract_explicit_action_requirements(task)
    authentication_required = any(
        isinstance(requirement, dict) and requirement.get('phase') == 'authentication'
        for requirement in [
            *(explicit_requirements.get('fields') or []),
            *(explicit_requirements.get('clicks') or []),
        ]
    )

    def raw_page_auth_priority(page: dict[str, Any]) -> int:
        if not authentication_required:
            return 0
        page_text = _normalize_text(' '.join(
            str(page.get(key) or '') for key in ['url', 'name', 'title']
        ))
        element_text = _normalize_text(' '.join(
            str(element.get(key) or '')
            for element in page.get('elements') or []
            if isinstance(element, dict)
            for key in ['name', 'accessible_name', 'placeholder', 'label', 'form_label']
        ))
        return 1 if (
            any(keyword in page_text for keyword in ['login', '登录'])
            or any(keyword in element_text for keyword in ['用户名', '密码', 'username', 'password'])
        ) else 0

    raw_pages = [
        (index, page)
        for index, page in enumerate(element_map.get('pages') or [])
        if isinstance(page, dict)
    ]
    ranked_pages = sorted(
        raw_pages,
        key=lambda item: (
            raw_page_auth_priority(item[1]),
            _page_flow_priority(item[1], task_text, flow_keywords),
            -item[0],
        ),
        reverse=True,
    )
    for _, page in ranked_pages[:max_pages]:
        if not isinstance(page, dict) or remaining <= 0:
            continue
        page_key = page.get('page_key')
        elements = []
        page_elements = [
            element for element in (page.get('elements') or [])
            if isinstance(element, dict)
        ]
        indexed_elements = list(enumerate(page_elements))
        indexed_elements.sort(
            key=lambda item: (
                _element_compaction_priority(item[1], task_text, flow_keywords),
                -item[0],
            ),
            reverse=True,
        )
        for _, element in indexed_elements[:remaining]:
            if not isinstance(element, dict):
                continue
            locator = element.get('recommended_locator') or {}
            compact_element = {
                'page_key': page_key,
                'element_key': element.get('element_key'),
                'name': element.get('name'),
                'tag': element.get('tag'),
                'role': element.get('role'),
                'accessible_name': element.get('accessible_name'),
                'text': str(element.get('text') or '')[:120],
                'label': element.get('label'),
                'form_label': element.get('form_label'),
                'placeholder': element.get('placeholder'),
                'required': element.get('required'),
                'inferred_required': bool(element.get('inferred_required')),
                'in_dialog': element.get('in_dialog'),
                'dialog_name': element.get('dialog_name'),
                'row_text': element.get('row_text'),
                'row_index': element.get('row_index'),
                'row_cells': element.get('row_cells') if isinstance(element.get('row_cells'), list) else [],
                'column_index': element.get('column_index'),
                'column_name': element.get('column_name'),
                'column_text': element.get('column_text'),
                'table_name': element.get('table_name'),
                'actions': element.get('actions') or [],
                'options': (element.get('dynamic_options') or element.get('options') or [])[:10],
                'som_index': element.get('som_index'),
                'ax_ref': element.get('ax_ref'),
                'accessibility': {
                    key: (element.get('accessibility') or {}).get(key)
                    for key in ['role', 'name', 'value', 'description', 'checked', 'disabled', 'required']
                    if (element.get('accessibility') or {}).get(key) is not None
                    and (element.get('accessibility') or {}).get(key) != ''
                },
                'screenshot_region': element.get('screenshot_region') or element.get('bounding_box'),
                'recommended_locator': _locator_hint({'recommended_locator': locator}),
                'locator_candidates': [
                    _compact_locator(candidate)
                    for candidate in (element.get('locator_candidates') or [])[:12]
                    if isinstance(candidate, dict) and candidate.get('type')
                ],
                'suggested_value': _infer_test_value(element),
            }
            elements.append(compact_element)
            if element.get('required'):
                required_fields.append({
                    'page_key': page_key,
                    'element_key': element.get('element_key'),
                    'name': element.get('name'),
                    'suggested_value': compact_element['suggested_value'],
                    'actions': element.get('actions') or [],
                    'required': bool(element.get('required')),
                    'inferred_required': False,
                })
        remaining -= len(elements)
        pages.append({
            'page_key': page_key,
            'name': page.get('name'),
            'title': page.get('title'),
            'url': page.get('url'),
            'runtime_fresh': bool(page.get('runtime_fresh')),
            'capture_source': page.get('capture_source') or '',
            'elements': elements,
            'component_context': page.get('component_context') or {},
            'visual_som': page.get('visual_som') or {},
            'virtual_lists': page.get('virtual_lists') or {},
            'supplemental_collectors': page.get('supplemental_collectors') or {},
            'network_summary': {
                key: (page.get('network_summary') or {}).get(key)
                for key in ['total_count', 'api_count', 'failed_count', 'by_method', 'by_status']
                if (page.get('network_summary') or {}).get(key) is not None
                and (page.get('network_summary') or {}).get(key) != ''
            } | {
                'failed': ((page.get('network_summary') or {}).get('failed') or [])[:3],
                'samples': ((page.get('network_summary') or {}).get('samples') or [])[:3],
            },
            'accessibility_snapshot': {
                'source': (page.get('accessibility_snapshot') or {}).get('source'),
                'status': (page.get('accessibility_snapshot') or {}).get('status'),
                'node_count': (page.get('accessibility_snapshot') or {}).get('node_count'),
                'nodes': [
                    {
                        key: node.get(key)
                        for key in ['ax_ref', 'role', 'name', 'value', 'description', 'ignored']
                        if node.get(key) is not None and node.get(key) != ''
                    }
                    for node in ((page.get('accessibility_snapshot') or {}).get('nodes') or [])[:12]
                    if isinstance(node, dict)
                ],
                'error': (page.get('accessibility_snapshot') or {}).get('error'),
            },
            'frames': [
                {
                    'frame_key': frame.get('frame_key'),
                    'name': frame.get('name'),
                    'url': frame.get('url'),
                    'element_count': frame.get('element_count'),
                    'elements': frame.get('elements') or [],
                    'error': frame.get('error'),
                }
                for frame in (page.get('frames') or [])
                if isinstance(frame, dict)
            ],
            'shadow_dom': {
                'open_shadow_root_count': (page.get('shadow_dom') or {}).get('open_shadow_root_count'),
                'element_count': (page.get('shadow_dom') or {}).get('element_count'),
                'elements': [
                    {
                        key: element.get(key)
                        for key in ['element_key', 'name', 'text', 'role', 'tag', 'actions', 'host_path', 'recommended_locator']
                        if element.get(key) is not None and element.get(key) != ''
                    }
                    for element in ((page.get('shadow_dom') or {}).get('elements') or [])[:3]
                    if isinstance(element, dict)
                ],
                'error': (page.get('shadow_dom') or {}).get('error'),
            },
        })
    if authentication_required:
        def authentication_page_priority(page: dict[str, Any]) -> tuple[int, int]:
            page_text = _normalize_text(' '.join(
                str(page.get(key) or '') for key in ['url', 'name', 'title']
            ))
            element_text = _normalize_text(' '.join(
                str(element.get(key) or '')
                for element in page.get('elements') or []
                if isinstance(element, dict)
                for key in ['name', 'accessible_name', 'placeholder', 'label', 'form_label']
            ))
            has_auth_controls = any(
                keyword in element_text
                for keyword in ['用户名', '密码', 'username', 'password']
            )
            is_login_page = any(keyword in page_text for keyword in ['login', '登录'])
            return (0 if has_auth_controls or is_login_page else 1, -_page_flow_priority(page, task_text, flow_keywords))

        pages.sort(key=authentication_page_priority)

    included_page_keys = {
        str(page.get('page_key') or '')
        for page in pages
        if isinstance(page, dict) and page.get('page_key')
    }
    if required_fields:
        page_scores = {
            str(page.get('page_key') or ''): _page_flow_priority(page, task_text, flow_keywords)
            for page in pages
            if isinstance(page, dict) and page.get('page_key')
        }
        business_page_scores = {
            page_key: score
            for page_key, score in page_scores.items()
            if any(
                isinstance(page, dict)
                and str(page.get('page_key') or '') == page_key
                and any(
                    isinstance(element, dict)
                    and (
                        element.get('required')
                        or element.get('inferred_required')
                        or element.get('in_dialog')
                        or element.get('dialog_name')
                        or any(action in (element.get('actions') or []) for action in ['fill', 'select_option', 'check'])
                    )
                    for element in page.get('elements') or []
                )
                for page in pages
            )
        }
        if business_page_scores:
            top_score = max(business_page_scores.values())
            scoped_required_page_keys = {
                page_key
                for page_key, score in business_page_scores.items()
                if score >= top_score - 120
            }
            required_fields = [
                field for field in required_fields
                if str(field.get('page_key') or '') in scoped_required_page_keys
            ]
    state_transitions = [
        transition
        for transition in element_map.get('state_transitions') or []
        if isinstance(transition, dict)
        and (
            str(transition.get('from_page_key') or '') in included_page_keys
            or str(transition.get('to_page_key') or '') in included_page_keys
            or any(
                keyword in _normalize_text(transition.get('target_name') or '')
                for keyword in flow_keywords
            )
        )
    ][:200]
    return {
        'base_url': element_map.get('base_url'),
        'latest_runtime_page_key': element_map.get('latest_runtime_page_key') or '',
        'latest_runtime_url': element_map.get('latest_runtime_url') or '',
        'coverage_summary': element_map.get('coverage_summary') or {},
        'risk_items': (element_map.get('risk_items') or [])[:20],
        'low_confidence_items': (element_map.get('low_confidence_items') or [])[:20],
        'state_transitions': state_transitions,
        'required_fields': required_fields[:30],
        'explicit_action_requirements': {
            **explicit_requirements,
            'matched_elements': _matched_elements_for_requirements(explicit_requirements, {'pages': pages}),
        },
        'compaction': {
            'max_pages': max_pages,
            'max_elements': max_elements,
            'included_pages': len(pages),
            'included_elements': sum(len(page.get('elements') or []) for page in pages),
            'strategy': 'task_flow_priority',
            'flow_keywords': flow_keywords,
        },
        'pages': pages,
    }


def _plan_page_score(page: dict[str, Any], task_text: str) -> int:
    elements = [element for element in page.get('elements') or [] if isinstance(element, dict)]
    text = _normalize_text(' '.join(
        str(element.get(key) or '')
        for element in elements
        for key in ['name', 'accessible_name', 'text', 'label', 'form_label', 'placeholder', 'dialog_name']
    ))
    score = 0
    if any('fill' in (element.get('actions') or []) or 'select_option' in (element.get('actions') or []) for element in elements):
        score += 300
    if any(element.get('required') or element.get('inferred_required') for element in elements):
        score += 260
    if any(element.get('in_dialog') or element.get('dialog_name') for element in elements):
        score += 240
    if any(
        'click' in (element.get('actions') or [])
        and any(keyword in _normalize_text(element.get('name') or element.get('text')) for keyword in ['保存', '提交', '确定', '确认'])
        for element in elements
    ):
        score += 180
    for keyword in _extract_flow_keywords(task_text):
        normalized = _normalize_text(keyword)
        if normalized and normalized in text:
            score += 80 + min(len(normalized), 20)
    if any(keyword in text for keyword in ['登录', '密码', '用户名', 'login', 'password']):
        score -= 600
    return score


def _is_business_form_covered(page: dict[str, Any], safety_policy: dict[str, Any]) -> bool:
    """判断页面快照是否已经覆盖可执行的业务表单或弹窗状态。"""
    elements = [element for element in page.get('elements') or [] if isinstance(element, dict)]
    has_fillable = any(
        'fill' in (element.get('actions') or [])
        or 'select_option' in (element.get('actions') or [])
        or element.get('role') in {'combobox', 'select'}
        for element in elements
    )
    has_required = any(element.get('required') or element.get('inferred_required') for element in elements)
    has_dialog_context = any(
        element.get('in_dialog')
        or element.get('dialog_name')
        or str(element.get('role') or '').lower() == 'dialog'
        for element in elements
    )
    confirm_keywords = list(safety_policy.get('require_confirmation_keywords') or []) or ['保存', '提交', '确定', '确认']
    has_confirmation = any(
        'click' in (element.get('actions') or [])
        and any(_normalize_text(keyword) in _normalize_text(_element_label_text(element)) for keyword in confirm_keywords if keyword)
        for element in elements
    )
    return bool(has_fillable and (has_required or has_dialog_context or has_confirmation))


def _select_plan_page(task, element_map: dict[str, Any]) -> dict[str, Any]:
    pages = [page for page in (element_map.get('pages') or []) if isinstance(page, dict)]
    if not pages:
        return {}
    task_text = ' '.join(str(value or '') for value in [
        getattr(task, 'name', ''),
        getattr(task, 'source_requirement', ''),
        getattr(task, 'gherkin', ''),
        getattr(task, 'target_module', ''),
    ])
    return max(pages, key=lambda page: _plan_page_score(page, task_text))


def _fallback_step_operation(element: dict[str, Any]) -> str:
    actions = element.get('actions') or []
    if (
        'select_option' in actions
        or element.get('tag') == 'select'
        or element.get('role') in {'combobox', 'select'}
    ):
        return 'select_option'
    if 'fill' in actions:
        return 'fill'
    if 'check' in actions:
        return 'check'
    if 'click' in actions:
        return 'click'
    return 'assert_visible'


def _task_requirement_document(task: Any) -> dict[str, Any]:
    return _build_requirement_document(task)


def _task_text(task: Any) -> str:
    document = _task_requirement_document(task)
    return ' '.join(str(value or '') for value in [
        document.get('task_name', ''),
        document.get('target_module', ''),
        document.get('source_requirement', ''),
        document.get('gherkin', ''),
    ])


def _is_minimal_required_task(task_text: str) -> bool:
    return any(keyword in task_text for keyword in ['必填', '最简', '最少', '必要', 'required', 'minimal'])


def _extract_explicit_action_requirements(task: Any | None) -> dict[str, Any]:
    """从自然语言/Gherkin 中抽取用户明确要求的动作，不把“必填”扩展成额外字段。"""
    if task is None:
        return {'fields': [], 'clicks': [], 'assertions': []}
    document = _task_requirement_document(task)
    text = '\n'.join(
        str(item.get('text') or '')
        for item in (document.get('sources') or [])
        if isinstance(item, dict)
    )
    fields: list[dict[str, Any]] = []
    clicks: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []

    def add_unique(items: list[dict[str, Any]], item: dict[str, Any]) -> None:
        identity = (
            _normalize_text(item.get('name') or ''),
            _normalize_text(item.get('value') or ''),
            _normalize_text(item.get('operation') or ''),
        )
        if identity[0] and not any(
            (
                _normalize_text(existing.get('name') or ''),
                _normalize_text(existing.get('value') or ''),
                _normalize_text(existing.get('operation') or ''),
            ) == identity
            for existing in items
        ):
            items.append(item)

    authentication_match = re.search(
        r'(?:用户名|账号|账户)\s*[“"]([^”"]+)[”"]\s*(?:和|及|、|,)\s*密码\s*[“"]([^”"]+)[”"]',
        text,
        re.IGNORECASE,
    )
    if authentication_match:
        add_unique(fields, {
            'name': '用户名',
            'value': authentication_match.group(1),
            'operation': 'fill',
            'source': 'explicit_authentication',
            'phase': 'authentication',
        })
        add_unique(fields, {
            'name': '密码',
            'value': authentication_match.group(2),
            'operation': 'fill',
            'source': 'explicit_authentication',
            'phase': 'authentication',
        })
    if re.search(r'(?:点击|单击|按下)\s*(?:[“"])?登录(?:按钮)?(?:[”"])?', text, re.IGNORECASE):
        add_unique(clicks, {
            'name': '登录',
            'operation': 'click',
            'source': 'explicit_authentication',
            'phase': 'authentication',
        })

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('|') and stripped.endswith('|'):
            cells = [cell.strip() for cell in stripped.strip('|').split('|')]
            if len(cells) >= 2 and cells[0] and cells[1] and '字段' not in cells[0]:
                value = cells[1]
                operation = 'select_option' if any(keyword in value for keyword in ['下拉', '选择', '选项']) else 'fill'
                if any(keyword in value for keyword in ['勾选', '单选', 'radio']):
                    operation = 'check'
                add_unique(fields, {
                    'name': cells[0],
                    'value': value,
                    'operation': operation,
                    'source': 'gherkin_table',
                })
            continue
        for match in re.finditer(r'(?:点击|单击|按下)[“"]([^”"]{2,60})[”"]', stripped):
            add_unique(clicks, {'name': match.group(1), 'operation': 'click', 'source': 'quoted_click'})
    return {
        'fields': fields[:30],
        'clicks': clicks[:20],
        'assertions': assertions[:20],
    }


def _text_overlap_score(left: str, right: str) -> int:
    left = _normalize_text(left)
    right = _normalize_text(right)
    if not left or not right:
        return 0
    if left in right or right in left:
        return min(len(left), len(right)) * 2
    best = 0
    for size in range(2, min(len(left), len(right)) + 1):
        if any(left[index:index + size] in right for index in range(0, len(left) - size + 1)):
            best = size
    return best


def _element_requirement_score(element: dict[str, Any], requirement: dict[str, Any]) -> int:
    wanted_name = _normalize_text(requirement.get('name') or '')
    wanted_value = _normalize_text(requirement.get('value') or '')
    wanted_operation = str(requirement.get('operation') or '').strip()
    if not wanted_name and not wanted_value:
        return 0
    text = _normalize_text(_element_label_text(element))
    actions = element.get('actions') or []
    score = 0
    if wanted_operation in {'click', 'assert_visible', 'assert_text', 'assert_contain_text'}:
        if not wanted_name or (
            wanted_name not in text
            and text not in wanted_name
            and _text_overlap_score(wanted_name, text) < 2
        ):
            return 0
    if wanted_operation in actions:
        score += 120
    elif wanted_operation in {'fill', 'select_option', 'check'} and any(action in actions for action in ['fill', 'select_option', 'check']):
        score += 30
    if wanted_name and (wanted_name in text or text in wanted_name):
        score += 90
    else:
        score += _text_overlap_score(wanted_name, text) * 8
    if wanted_name and (wanted_name in text or text in wanted_name):
        score += 40
    if wanted_value and any(part and part in text for part in re.split(r'[\s_/，,。；;:：()（）]+', wanted_value)):
        score += 160
    if element.get('required'):
        score += 20
    if element.get('in_dialog') or element.get('dialog_name'):
        score += 20
    return score


def _matched_elements_for_requirements(
    explicit_requirements: dict[str, Any],
    compact_map: dict[str, Any],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    requirements = [
        *(explicit_requirements.get('fields') or []),
        *(explicit_requirements.get('clicks') or []),
        *(explicit_requirements.get('assertions') or []),
    ]
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        wanted_operation = str(requirement.get('operation') or '').strip()
        authentication_phase = requirement.get('phase') == 'authentication'
        candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for page in compact_map.get('pages') or []:
            if not isinstance(page, dict):
                continue
            page_elements = [
                element for element in (page.get('elements') or [])
                if isinstance(element, dict)
            ]
            frame_elements = [
                {
                    **element,
                    'page_key': page.get('page_key'),
                    'context_type': 'frame',
                    'frame_key': frame.get('frame_key') or '',
                    'frame_name': frame.get('name') or '',
                    'frame_url': frame.get('url') or '',
                }
                for frame in (page.get('frames') or [])
                if isinstance(frame, dict)
                for element in (frame.get('elements') or [])
                if isinstance(element, dict)
            ]
            for element in [*page_elements, *frame_elements]:
                if not isinstance(element, dict):
                    continue
                actions = element.get('actions') or []
                if wanted_operation in {'fill', 'select_option', 'check'} and not any(action in actions for action in ['fill', 'select_option', 'check']):
                    continue
                if wanted_operation == 'click' and 'click' not in actions:
                    continue
                score = _element_requirement_score(element, requirement)
                if score <= 0:
                    continue
                element_text = _normalize_text(_element_label_text(element))
                if authentication_phase:
                    if wanted_operation == 'click' and any(
                        keyword in element_text for keyword in ['退出登录', '注销', '登出', 'logout', 'sign out']
                    ):
                        continue
                    page_text = _normalize_text(' '.join(str(page.get(key) or '') for key in ['url', 'name', 'title']))
                    if any(keyword in page_text for keyword in ['login', '登录']):
                        score += 500
                    if any(keyword in element_text for keyword in ['用户名', '密码', 'username', 'password']):
                        score += 300
                candidates.append((score, page, element))
        candidates.sort(key=lambda item: item[0], reverse=True)
        limit = 1 if authentication_phase else (6 if wanted_operation == 'click' else 1)
        for _, page, element in candidates[:limit]:
            matched_actions = element.get('actions') or []
            matched_operation = wanted_operation
            if wanted_operation in {'fill', 'select_option', 'check'}:
                matched_operation = next(
                    (action for action in ['check', 'select_option', 'fill'] if action in matched_actions),
                    wanted_operation,
                )
            matches.append({
                'requirement': {**requirement, 'operation': matched_operation},
                'page_key': page.get('page_key'),
                'element_key': element.get('element_key'),
                'name': element.get('name') or element.get('label') or element.get('placeholder') or element.get('text'),
                'actions': matched_actions,
                'locator_hint': _locator_hint(element),
                'row_scope': _row_scope(element),
                'suggested_value': requirement.get('value') or element.get('suggested_value') or _infer_test_value(element),
                'context_type': element.get('context_type') or 'main',
                'frame_key': element.get('frame_key') or '',
                'frame_url': element.get('frame_url') or '',
            })
    return matches[:60]


def _requirement_contract_text(requirement: dict[str, Any]) -> str:
    return _normalize_text(' '.join(str(requirement.get(key) or '') for key in [
        'target', 'name', 'target_name', 'value', 'expected', 'phase',
    ]))


def _requirement_contract_identity(requirement: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(requirement.get('operation') or '').strip().lower(),
        _normalize_text(requirement.get('target') or requirement.get('name') or requirement.get('target_name') or ''),
        _normalize_text(requirement.get('value') or requirement.get('expected') or ''),
    )


def _requirement_contract_covers(existing: dict[str, Any], wanted: dict[str, Any]) -> bool:
    existing_identity = _requirement_contract_identity(existing)
    wanted_identity = _requirement_contract_identity(wanted)
    if existing_identity == wanted_identity:
        return True
    existing_operation, existing_name, existing_value = existing_identity
    wanted_operation, wanted_name, wanted_value = wanted_identity
    if wanted_operation and existing_operation and wanted_operation != existing_operation:
        return False
    existing_text = _requirement_contract_text(existing)
    wanted_text = _requirement_contract_text(wanted)
    if wanted_name and existing_name and (wanted_name in existing_name or existing_name in wanted_name):
        shorter = min(len(existing_name), len(wanted_name))
        longer = max(len(existing_name), len(wanted_name))
        if longer and shorter / longer < 0.75:
            return False
        return not wanted_value or wanted_value in existing_text
    if wanted_text and existing_text and (wanted_text in existing_text or existing_text in wanted_text):
        shorter = min(len(existing_text), len(wanted_text))
        longer = max(len(existing_text), len(wanted_text))
        return bool(longer and shorter / longer >= 0.75)
    return False


def _next_requirement_action_id(existing_ids: set[str], operation: str) -> str:
    prefix = 'explicit_assert' if operation.startswith('assert_') else 'explicit_action'
    index = 1
    while f'{prefix}_{index}' in existing_ids:
        index += 1
    action_id = f'{prefix}_{index}'
    existing_ids.add(action_id)
    return action_id


def _complete_staged_requirement_contract(
    requirements: dict[str, Any],
    explicit_requirements: dict[str, Any] | None = None,
    compact_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make flow the complete hand-off contract for downstream planner roles."""
    return _contract_complete_requirement_contract(requirements, explicit_requirements, compact_map)


def _element_label_text(element: dict[str, Any]) -> str:
    return ' '.join(str(element.get(field) or '') for field in [
        'name', 'accessible_name', 'text', 'label', 'form_label', 'placeholder', 'dialog_name',
        'row_text', 'column_name', 'column_text', 'table_name',
    ])


def _is_preferred_minimal_form_field(element: dict[str, Any], task_text: str) -> bool:
    actions = element.get('actions') or []
    return (
        any(action in actions for action in ['fill', 'select_option', 'check'])
        and bool(
            element.get('required')
            or element.get('inferred_required')
            or element.get('label')
            or element.get('form_label')
            or element.get('placeholder')
        )
        and bool(element.get('in_dialog') or element.get('dialog_name') or _is_minimal_required_task(task_text))
    )


def _minimal_form_field_rank(element: dict[str, Any], task_text: str) -> tuple[int, int]:
    if element.get('required'):
        return 0, 0
    if element.get('inferred_required'):
        return 10, 0
    if _is_preferred_minimal_form_field(element, task_text):
        return 20, 0
    return 100, 0


def _is_confirmation_step(step: dict[str, Any], safety_policy: dict[str, Any]) -> bool:
    text = _normalize_text(' '.join(str(step.get(key) or '') for key in ['action', 'operation', 'description', 'target_name', 'value']))
    keywords = list(safety_policy.get('require_confirmation_keywords') or [])
    return any(_normalize_text(keyword) and _normalize_text(keyword) in text for keyword in keywords)


def _is_dangerous_step(step: dict[str, Any], safety_policy: dict[str, Any]) -> bool:
    text = _normalize_text(' '.join(str(step.get(key) or '') for key in ['action', 'operation', 'description', 'target_name', 'value']))
    keywords = list(safety_policy.get('dangerous_action_keywords') or [])
    return any(_normalize_text(keyword) and _normalize_text(keyword) in text for keyword in keywords)


def _is_low_risk_form_submit(task, step: dict[str, Any]) -> bool:
    text = _normalize_text(' '.join(str(step.get(key) or '') for key in ['action', 'description', 'target_name', 'expected']))
    safety_policy = getattr(task, 'safety_policy', {}) if task is not None else {}
    if not isinstance(safety_policy, dict):
        safety_policy = {}
    requirement_contract = safety_policy.get('requirement_contract')
    if not isinstance(requirement_contract, dict):
        requirement_contract = getattr(task, 'requirement_contract', {})
    flow = requirement_contract.get('flow') if isinstance(requirement_contract, dict) else []
    step_action_id = str(step.get('action_id') or '')
    contracted = any(
        isinstance(item, dict)
        and item.get('required', True)
        and str(item.get('operation') or '').strip().lower() == 'click'
        and (
            (step_action_id and str(item.get('action_id') or '') == step_action_id)
            or (
                _normalize_text(item.get('target') or item.get('name') or item.get('target_name') or '')
                and _normalize_text(item.get('target') or item.get('name') or item.get('target_name') or '') in text
            )
        )
        for item in (flow if isinstance(flow, list) else [])
    )
    danger_terms = [
        str(item or '').strip()
        for item in safety_policy.get('dangerous_action_keywords') or []
        if str(item or '').strip()
    ]
    return (
        step.get('operation') == 'click'
        and contracted
        and not any(_normalize_text(keyword) and _normalize_text(keyword) in text for keyword in danger_terms)
    )


def _fallback_plan(task, element_map: dict[str, Any], reason: str = '') -> dict[str, Any]:
    data_lifecycle = _build_data_lifecycle(task, [], {'cleanup': []})
    return {
        'llm_enabled': False,
        'llm_error': reason,
        'patch_available': False,
        'requires_llm_planning': True,
        'intent': {
            'goal': task.source_requirement or task.gherkin or task.name,
            'business_entities': [],
            'assumptions': [],
            'ambiguities': [],
        },
        'test_plan': {
            'objective': task.source_requirement or task.gherkin or task.name,
            'preconditions': ['目标环境可访问', '执行器已连接'],
            'test_data': {},
            'data_lifecycle': data_lifecycle,
            'steps': [],
            'expected_results': [],
            'cleanup': [],
        },
        'generated_case': {
            'name': task.target_module or task.name,
            'description': task.source_requirement or task.gherkin or '',
            'steps': [],
            'data_lifecycle': data_lifecycle,
        },
    }


def _deterministic_state_route_selection(
    compact_map: dict[str, Any],
    reason: str = '',
    *,
    initial_url: str = '',
) -> dict[str, Any]:
    pages = [
        page for page in (compact_map.get('pages') or [])
        if isinstance(page, dict) and str(page.get('page_key') or '').strip()
    ]
    normalized_initial_url = _normalize_text(initial_url)
    if normalized_initial_url:
        pages.sort(
            key=lambda page: (
                0 if _normalize_text(page.get('url') or '') == normalized_initial_url else 1,
                0 if bool(page.get('runtime_fresh')) else 1,
            )
        )
    ordered_page_keys = [str(page.get('page_key') or '') for page in pages]
    observed_page_keys = set(ordered_page_keys)
    page_position = {page_key: index for index, page_key in enumerate(ordered_page_keys)}
    transition_keys: list[str] = []
    selected_pairs: set[tuple[str, str]] = set()
    for transition in compact_map.get('state_transitions') or []:
        if not isinstance(transition, dict):
            continue
        transition_key = str(transition.get('transition_key') or transition.get('element_key') or '').strip()
        if not transition_key:
            continue
        if not isinstance(transition.get('locator_hint'), dict) or not transition.get('locator_hint', {}).get('type'):
            continue
        from_page_key = str(transition.get('from_page_key') or transition.get('page_key') or '').strip()
        to_page_key = str(transition.get('to_page_key') or '').strip()
        if from_page_key and from_page_key not in observed_page_keys:
            continue
        if to_page_key and to_page_key not in observed_page_keys:
            continue
        if not from_page_key or not to_page_key:
            continue
        if page_position.get(to_page_key, -1) <= page_position.get(from_page_key, -1):
            continue
        pair = (from_page_key, to_page_key)
        if pair in selected_pairs:
            continue
        selected_pairs.add(pair)
        transition_keys.append(transition_key)

    fallback_reason = str(reason or '').strip()
    return {
        'ordered_page_keys': ordered_page_keys,
        'page_keys': ordered_page_keys,
        'transition_keys': transition_keys[:20],
        'route_actions': [],
        'assumptions': [fallback_reason] if fallback_reason else [],
        'fallback_mode': 'deterministic_page_order',
        'fallback_reason': fallback_reason,
    }


def _is_recoverable_route_selection_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return True
    message = f'{type(exc).__name__}: {exc}'.lower()
    return any(token in message for token in [
        'read operation timed out',
        'http 502',
        'http 503',
        'http 524',
        'gateway timeout',
        'bad gateway',
        'timed out',
    ])


def _merge_route_with_seed(route_seed: dict[str, Any], route_result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(route_seed, dict):
        route_seed = {}
    if not isinstance(route_result, dict):
        route_result = {}
    merged = {**route_seed, **route_result}
    for key in ['ordered_page_keys', 'page_keys', 'transition_keys', 'route_actions', 'assumptions']:
        if not merged.get(key) and route_seed.get(key):
            merged[key] = route_seed.get(key)
    merged.setdefault('fallback_mode', route_result.get('fallback_mode') or route_seed.get('fallback_mode') or '')
    merged.setdefault('fallback_reason', route_result.get('fallback_reason') or route_seed.get('fallback_reason') or '')
    merged['route_seed'] = route_seed
    return merged


def _route_contract_dict(route: dict[str, Any] | RouteContract) -> dict[str, Any]:
    if isinstance(route, RouteContract):
        return route.as_dict()
    return route if isinstance(route, dict) else {}


def _binding_contract_dict(bindings: dict[str, Any] | BindingContract) -> dict[str, Any]:
    if isinstance(bindings, BindingContract):
        return bindings.as_dict()
    return bindings if isinstance(bindings, dict) else {}


def _planning_precondition_failure(task, element_map: dict[str, Any]) -> dict[str, Any] | None:
    """识别采集阶段已经明确声明不可规划的状态，避免 LLM 对错误页面做业务规划。"""
    coverage = element_map.get('exploration_coverage') if isinstance(element_map.get('exploration_coverage'), dict) else {}
    if not coverage:
        return None
    if coverage.get('status') == 'success' or bool(coverage.get('planning_allowed')):
        return None

    if _task_authentication_mode(task) == 'test_subject' and (
        coverage.get('blocked_by') == 'authentication'
        or '认证' in str(coverage.get('reason') or '')
        or '登录' in str(coverage.get('reason') or '')
    ):
        return None

    reason = str(coverage.get('reason') or '').strip() or '页面探索未覆盖 LLM 规划所需的业务目标阶段'
    category = str(coverage.get('failure_category') or '').strip() or 'exploration_coverage'
    blocked_by = str(coverage.get('blocked_by') or '').strip()
    if blocked_by == 'authentication' or '认证' in reason or '登录' in reason:
        category = category if category in {'permission', 'environment'} else 'permission'
    missing_stages = coverage.get('missing_stages') if isinstance(coverage.get('missing_stages'), list) else []
    issue = {
        'reason': 'planning_precondition_failed',
        'message': reason,
        'failure_category': category,
        'blocked_by': blocked_by,
        'missing_stages': missing_stages[:20],
    }
    return {
        'message': reason,
        'failure_category': category,
        'plan_validation': {
            'status': 'failed',
            'total_steps': 0,
            'executable_steps': 0,
            'bound_element_steps': 0,
            'unresolved_step_count': 0,
            'risky_step_count': 0,
            'low_confidence_step_count': 0,
            'semantic_issue_count': 1,
            'unresolved_steps': [],
            'risky_steps': [],
            'low_confidence_steps': [],
            'semantic_issues': [issue],
            'notes': ['采集阶段声明当前元素地图不可用于业务规划，已跳过 LLM 调用'],
        },
        'planning_pipeline': {
            'name': 'staged_v1',
            'status': 'blocked',
            'failed_stage_error': reason,
            'stages': [],
        },
    }


def _focused_planning_element_map(compact_map: dict[str, Any]) -> dict[str, Any]:
    """为规划器构建需求绑定优先的元素视图，完整地图仍保留在任务快照中。"""
    explicit = compact_map.get('explicit_action_requirements') if isinstance(compact_map.get('explicit_action_requirements'), dict) else {}
    mandatory_keys = {
        (str(item.get('page_key') or ''), str(item.get('element_key') or ''))
        for item in explicit.get('matched_elements') or []
        if isinstance(item, dict) and item.get('element_key')
    }
    for transition in compact_map.get('state_transitions') or []:
        if not isinstance(transition, dict):
            continue
        page_key = str(transition.get('from_page_key') or transition.get('page_key') or '')
        element_key = str(transition.get('element_key') or transition.get('source_element_key') or '')
        if element_key:
            mandatory_keys.add((page_key, element_key))

    focused_pages = []
    for page in compact_map.get('pages') or []:
        if not isinstance(page, dict):
            continue
        page_key = str(page.get('page_key') or '')
        elements = [element for element in page.get('elements') or [] if isinstance(element, dict)]
        selected = []
        selected_keys: set[str] = set()
        for element in elements:
            element_key = str(element.get('element_key') or '')
            if (page_key, element_key) in mandatory_keys or ('', element_key) in mandatory_keys:
                selected.append(element)
                selected_keys.add(element_key)
        for element in elements:
            element_key = str(element.get('element_key') or '')
            if element_key in selected_keys:
                continue
            selected.append(element)
            selected_keys.add(element_key)
        focused_pages.append({
            key: page.get(key)
            for key in ['page_key', 'name', 'title', 'url', 'runtime_fresh', 'capture_source']
        } | {
            'elements': selected,
            'frames': page.get('frames') or [],
            'shadow_dom': page.get('shadow_dom') or {},
            'virtual_lists': page.get('virtual_lists') or {},
        })

    return {
        **compact_map,
        'pages': focused_pages,
        'compaction': {
            **(compact_map.get('compaction') or {}),
            'planning_view': 'complete_elements_with_required_bindings_and_transitions_first',
            'planning_elements': sum(len(page.get('elements') or []) for page in focused_pages),
        },
    }


def _build_prompt(task, payload: dict[str, Any], compact_map: dict[str, Any]) -> list[Any]:
    requirement_document = _task_requirement_document(task)
    requirement = requirement_document.get('source_requirement') or ''
    gherkin = requirement_document.get('gherkin') or ''
    verification = payload.get('verification_result') or {}
    safety_policy = task.safety_policy or {}
    planning_map = _focused_planning_element_map(compact_map)
    prompt_element_map = {
        key: value for key, value in planning_map.items()
        if key not in {'explicit_action_requirements', 'state_transitions'}
    }
    required_action_bindings = [
        {
            'requirement': item.get('requirement') or {},
            'page_key': item.get('page_key') or '',
            'element_key': item.get('element_key') or '',
            'name': item.get('name') or '',
            'actions': item.get('actions') or [],
        }
        for item in (
            (compact_map.get('explicit_action_requirements') or {}).get('matched_elements') or []
        )
        if isinstance(item, dict)
    ]
    authentication_mode = _task_authentication_mode(task)

    system_prompt = """你是 Web UI 自动化测试规划 agent，只返回严格 JSON。
目标：结合需求、Playwright 原生观察、元素地图和安全边界，输出可落库步骤。
硬约束：
1. 元素步骤只能使用 element_map 中已有元素，必须给 page_key、element_key、target_name、locator_hint。
2. locator_hint 只能从该元素 recommended_locator/locator_candidates 选择，禁止臆造 selector。
3. operation 只能是 goto/fill/click/select_option/check/uncheck/wait/assert_visible/assert_text/assert_contain_text/assert_value。
4. required_fields、suggested_value、表单/弹窗/可访问性上下文只是规划证据，不是强制生成规则；只能选择和当前用户需求、当前业务流程、当前可见表单状态一致的字段。
5. iframe/shadow/virtual list/network 风险写入 assumptions 或步骤说明；不要臆造接口断言。
6. 高风险动作按 safety_policy 降级或标记 requires_confirmation；每步必须可执行。
7. state_transitions 是业务状态迁移的硬证据。若页面存在业务表单/弹窗迁移，生成步骤必须把该 click 放在表单字段 fill/select 之前，不能直接从 goto 编排表单操作。
8. 若 verification_result.flow_execution.failed_step_records 存在，说明上一次真实执行失败；必须结合失败信息重新输出完整 steps，补齐前置状态变化步骤，不能只返回局部 patch。
9. 若 failed_step_records.runtime_state 存在，优先按失败时当前页面快照重新判断控件类型和前置依赖；not editable/disabled/readonly 类失败必须通过选择 radio/checkbox/combobox、切换表单状态或跳过非必填字段解决，禁止重复输出同一个不可编辑 fill。
10. explicit_action_requirements 是用户在需求/Gherkin 表格中明确写出的测试点和校验点。若 matched_elements 中存在对应元素，steps 必须覆盖这些动作；不能只生成页面准备、弹窗准备或可见性断言。
11. 若 verification_result.typescript_execution.status=failed，必须结合 failure_classification、failed_spec_step_marker、stdout/stderr 摘要和 network_summary 重新输出完整计划。不得返回旧 spec 的局部替换、patch_operations 或只修复失败步骤。
12. 若 verification_result.failure_reobserve 存在，优先以其中 fresh page_state、accessibility_snapshot、URL 和 replay 结果作为失败位置的当前页面事实；历史元素地图仅用于补充候选，不得覆盖本轮实时观察。
13. 控件操作语义必须与观察证据一致：只有元素地图明确标记为原生 select 且包含可用 option/value 时才使用 select_option；role=combobox、Element UI/Arco/Ant 等自定义下拉必须规划为 click 触发展开、选择已观察到的 option、必要时再做状态断言，不能把自定义 combobox 当作原生 select。
14. element_map.latest_runtime_page_key 指向失败后刚采集的 runtime_fresh 页面。重规划时它是失败位置的最高优先级事实；相同 URL 的旧探索页面和历史确认 locator 不能覆盖该页面元素。
15. 每次执行都从 fresh browser context 和 task.target_url 开始。若 execution_contract.authentication_mode == 'test_subject'，登录页就是被测对象的一部分，允许围绕该页编排认证步骤，不得因为尚未到达业务页就把它当作前置认证失败；若为 'authentication_then_business'，登录动作是显式第一阶段，必须在计划中保留并在登录成功后继续编排后续业务步骤；否则，元素地图中的登录后页面只是观察到的状态，不是可直接 goto 的入口。除 task.target_url 外，后续页面必须通过已观察的 action/state_transition 到达。不得用 goto 跳过登录、菜单导航、弹窗状态迁移或其他用户明确动作。
16. execution_contract.required_action_bindings 是本轮完整计划的覆盖契约。每项绑定都必须在 steps 中以对应 page_key/element_key 出现；无法覆盖时应在 ambiguities 中说明，但仍不得用未绑定步骤或深链接代替。
"""
    user_prompt = {
        'task': {
            'id': task.id,
            'name': task.name,
            'source_type': task.source_type,
            'requirement': requirement,
            'gherkin': gherkin,
            'target_url': task.target_url,
            'target_module': task.target_module,
        },
        'requirement_document': requirement_document,
        'execution_contract': {
            'fresh_browser_context': True,
            'initial_url': task.target_url,
            'allowed_initial_goto_urls': [task.target_url] if task.target_url else [],
            'observed_pages_are_state_snapshots': True,
            'authentication_mode': authentication_mode,
            'authenticated_pages_are_not_direct_entry_points': authentication_mode != 'test_subject',
            'required_action_bindings': required_action_bindings,
        },
        'safety_policy': safety_policy,
            'planning_phase': payload.get('planning_phase') or 'initial',
            'repair_instruction': payload.get('repair_instruction') or '',
            'previous_candidate_plan': payload.get('previous_candidate_plan') or {},
            'verification_result': verification,
            'element_map': prompt_element_map,
            'explicit_action_requirements': compact_map.get('explicit_action_requirements') or {},
            'state_transitions': compact_map.get('state_transitions') or [],
            'output_schema': {
                'intent': {
                    'goal': '',
                'business_entities': [],
                'business_flow': [],
                'data_requirements': {},
                'assumptions': [],
                'ambiguities': [],
            },
            'test_plan': {
                'objective': '',
                'preconditions': [],
                'test_data': {},
                'data_lifecycle': {
                    'strategy': 'isolated_generated_values|readonly|seeded_api_data',
                    'seed_actions': [],
                    'cleanup_actions': [],
                    'manual_cleanup_notes': [],
                },
                'page_context_usage': [],
                'steps': [
                    {
                        'step_sort': 0,
                        'action': '',
                        'operation': '',
                        'page_key': '',
                        'element_key': '',
                        'description': '',
                        'target_name': '',
                        'locator_hint': {'type': '', 'value': '', 'name': ''},
                        'value': '',
                        'expected': '',
                        'risk_level': 'low',
                        'requires_confirmation': False,
                        'confidence': 0.8,
                    }
                ],
                'expected_results': [],
                'cleanup': [],
            },
        },
    }
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=json.dumps(user_prompt, ensure_ascii=False)),
    ]


def _message_to_openai_payload(message: Any) -> dict[str, str]:
    role = 'user'
    if isinstance(message, SystemMessage):
        role = 'system'
    elif isinstance(message, HumanMessage):
        role = 'user'
    else:
        message_type = getattr(message, 'type', '')
        if message_type == 'system':
            role = 'system'
        elif message_type in {'ai', 'assistant'}:
            role = 'assistant'
    return {
        'role': role,
        'content': str(getattr(message, 'content', '') or ''),
    }


def _llm_plan_limits(task: Any, active_config: LLMConfig, async_mode: bool = False) -> tuple[int, int, int]:
    ai_config = _env_ai_generation_config(task)
    if async_mode:
        configured_timeout = (
            ai_config.get('async_llm_plan_request_timeout')
            or os.environ.get('AI_UI_ASYNC_LLM_PLAN_REQUEST_TIMEOUT')
        )
        configured_budget = (
            ai_config.get('async_llm_plan_total_budget')
            or os.environ.get('AI_UI_ASYNC_LLM_PLAN_TOTAL_BUDGET')
        )
        configured_retries = (
            ai_config.get('async_llm_plan_max_retries')
            or os.environ.get('AI_UI_ASYNC_LLM_PLAN_MAX_RETRIES')
        )
        timeout_cap = _safe_positive_int(configured_timeout, DEFAULT_ASYNC_LLM_PLAN_REQUEST_TIMEOUT)
        total_budget = _safe_positive_int(configured_budget, DEFAULT_ASYNC_LLM_PLAN_TOTAL_BUDGET)
        retry_cap = _safe_positive_int(configured_retries, DEFAULT_ASYNC_LLM_PLAN_MAX_RETRIES)
        request_timeout_source = max(active_config.request_timeout or 0, timeout_cap)
        retry_source = max(active_config.max_retries or 1, retry_cap)
    else:
        configured_timeout = ai_config.get('llm_plan_request_timeout') or os.environ.get('AI_UI_LLM_PLAN_REQUEST_TIMEOUT')
        configured_budget = ai_config.get('llm_plan_total_budget') or os.environ.get('AI_UI_LLM_PLAN_TOTAL_BUDGET')
        configured_retries = ai_config.get('llm_plan_max_retries') or os.environ.get('AI_UI_LLM_PLAN_MAX_RETRIES')
        timeout_cap = _safe_positive_int(configured_timeout, DEFAULT_LLM_PLAN_REQUEST_TIMEOUT)
        total_budget = _safe_positive_int(configured_budget, DEFAULT_LLM_PLAN_TOTAL_BUDGET)
        retry_cap = _safe_positive_int(configured_retries, DEFAULT_LLM_PLAN_MAX_RETRIES)
        request_timeout_source = active_config.request_timeout or timeout_cap
        retry_source = active_config.max_retries or 1

    request_timeout = min(max(10, request_timeout_source), timeout_cap, total_budget)
    max_retries = min(max(1, retry_source), retry_cap)
    return request_timeout, max_retries, total_budget


def _invoke_openai_compatible_chat(
    active_config: LLMConfig,
    messages: list[Any],
    temperature: float = 0.1,
    request_timeout: int | None = None,
    max_retries: int | None = None,
    total_budget: int | None = None,
    *,
    stream: bool = False,
    reasoning_effort: str | None = None,
    max_completion_tokens: int | None = None,
) -> str:
    """直接调用 OpenAI-compatible chat/completions，避免 LangChain 包装被路由商拦截。"""
    base_url = (active_config.api_url or '').strip().rstrip('/')
    if not base_url:
        raise ValueError('LLM 配置缺少 API 地址')
    if not active_config.api_key:
        raise ValueError('LLM 配置缺少 API Key')

    url = f'{base_url}/chat/completions'
    headers = {
        'Authorization': f'Bearer {active_config.api_key}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': active_config.name or 'gpt-3.5-turbo',
        'messages': [_message_to_openai_payload(message) for message in messages],
        'temperature': temperature,
        'response_format': {'type': 'json_object'},
    }
    if stream:
        payload['stream'] = True
    if reasoning_effort:
        payload['reasoning_effort'] = reasoning_effort
    if max_completion_tokens:
        payload['max_completion_tokens'] = max_completion_tokens
    max_retries = max(1, int(max_retries or active_config.max_retries or 1))
    timeout = max(10, int(request_timeout or active_config.request_timeout or 120))
    deadline = time.monotonic() + max(timeout, int(total_budget or timeout * max_retries))
    last_error: Exception | None = None

    for attempt in range(max_retries):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('LLM 调用超过总预算')
        try:
            attempt_timeout = min(timeout, max(1.0, remaining))
            attempt_started = time.monotonic()
            with httpx.Client(timeout=httpx.Timeout(attempt_timeout, connect=min(10.0, attempt_timeout))) as client:
                if payload.get('stream'):
                    with client.stream('POST', url, headers=headers, json=payload) as response:
                        if response.status_code >= 400:
                            response.read()
                            response_text = response.text
                            content = ''
                        else:
                            response_text = ''
                            chunks: list[str] = []
                            for line in response.iter_lines():
                                if not line.startswith('data:'):
                                    continue
                                event_data = line[5:].strip()
                                if not event_data or event_data == '[DONE]':
                                    continue
                                try:
                                    event = json.loads(event_data)
                                except json.JSONDecodeError:
                                    continue
                                choices = event.get('choices') or []
                                if not choices:
                                    continue
                                choice = choices[0] if isinstance(choices[0], dict) else {}
                                delta = choice.get('delta') if isinstance(choice.get('delta'), dict) else {}
                                message = choice.get('message') if isinstance(choice.get('message'), dict) else {}
                                piece = delta.get('content') or message.get('content') or ''
                                if isinstance(piece, str):
                                    chunks.append(piece)
                            content = ''.join(chunks)
                else:
                    response = client.post(url, headers=headers, json=payload)
                    response_text = response.text
                    content = ''
            elapsed = time.monotonic() - attempt_started
            if response.status_code >= 400:
                if (
                    response.status_code == 400
                    and 'response_format' in payload
                    and 'response_format' in response_text.lower()
                ):
                    payload.pop('response_format', None)
                    logger.info('LLM 供应商不支持 response_format，回退普通 JSON 提示协议')
                    continue
                optional_fields = [
                    field for field in ('reasoning_effort', 'max_completion_tokens', 'stream')
                    if field in payload and field.lower() in response_text.lower()
                ]
                if response.status_code == 400 and optional_fields:
                    for field in optional_fields:
                        payload.pop(field, None)
                    logger.info('LLM 供应商不支持可选参数，移除后重试: %s', ','.join(optional_fields))
                    continue
                raise RuntimeError(f'LLM HTTP {response.status_code}: {response_text[:1000]}')
            if not payload.get('stream'):
                data = response.json()
                choices = data.get('choices') or []
                if not choices:
                    raise RuntimeError(f'LLM 返回空 choices: {response_text[:1000]}')
                message = choices[0].get('message') or {}
                content = message.get('content') or ''
            if not content:
                if payload.get('stream'):
                    payload.pop('stream', None)
                    logger.info('LLM 流式响应没有 content，回退非流式协议重试')
                    continue
                raise RuntimeError('LLM 返回空 content')
            logger.info(
                'OpenAI-compatible LLM 直连调用成功: model=%s, attempt=%s/%s, status=%s, elapsed=%.1fs, response_chars=%s',
                active_config.name,
                attempt + 1,
                max_retries,
                response.status_code,
                elapsed,
                len(content),
            )
            return content
        except Exception as exc:
            last_error = exc
            logger.warning(
                'OpenAI-compatible LLM 直连调用失败，尝试重试 (%s/%s, timeout=%ss, elapsed=%.1fs, budget_left=%.1fs): %s',
                attempt + 1,
                max_retries,
                timeout,
                time.monotonic() - locals().get('attempt_started', time.monotonic()),
                max(0.0, deadline - time.monotonic()),
                str(exc)[:500],
            )
            if attempt < max_retries - 1:
                sleep_seconds = min(2 * (attempt + 1), max(0.0, deadline - time.monotonic()))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
    raise last_error or RuntimeError('LLM 调用失败')


def classify_task_authentication_contract(task, safety_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """用 LLM 生成认证阶段合同，执行器只消费该合同，不解析自然语言。"""
    policy = safety_policy if isinstance(safety_policy, dict) else getattr(task, 'safety_policy', {})
    policy = policy if isinstance(policy, dict) else {}
    cached = policy.get('authentication_contract') if isinstance(policy.get('authentication_contract'), dict) else {}
    cached_mode = _valid_authentication_mode(cached.get('mode'))
    if cached_mode and cached.get('source') != 'llm_error_default':
        return {**cached, 'mode': cached_mode, 'source': cached.get('source') or 'cached'}

    explicit_mode = _valid_authentication_mode(policy.get('authentication_mode')) or _valid_authentication_mode(getattr(task, 'authentication_mode', ''))
    if explicit_mode:
        return {
            'mode': explicit_mode,
            'source': 'explicit',
            'confidence': 1.0,
            'rationale': '任务已显式指定认证合同',
        }

    requirement_document = _task_requirement_document(task)
    requirement_text = '\n'.join([
        str(requirement_document.get('task_name') or ''),
        str(requirement_document.get('target_module') or ''),
        str(requirement_document.get('source_requirement') or ''),
        str(requirement_document.get('gherkin') or ''),
    ]).strip()
    if not requirement_text:
        return {
            'mode': 'prerequisite',
            'source': 'empty_requirement_default',
            'confidence': 0.0,
            'rationale': '任务没有可分析的需求文本',
        }

    active_config = LLMConfig.objects.filter(is_active=True).first()
    if not active_config:
        return {
            'mode': 'prerequisite',
            'source': 'llm_unavailable_default',
            'confidence': 0.0,
            'rationale': '未配置启用状态的 LLMConfig，按业务探索前置认证处理',
        }

    messages = [
        SystemMessage(content=(
            '你是 Web UI 自动化需求合同分类器，只返回 JSON。'
            '任务：判断认证/登录阶段在完整测试用例中的角色。'
            '只能选择一个 mode：'
            '1. prerequisite：认证只是到达业务系统的前置条件，用户真正要测试的是后续业务功能；'
            '2. test_subject：认证本身就是最终测试对象，后续没有独立业务流程需要探索；'
            '3. authentication_then_business：用户明确把认证动作写成测试步骤，同时认证之后还有独立业务流程必须继续执行。'
            '不要按固定词表或具体按钮/菜单名称判断，要理解完整用例意图、阶段顺序和最终业务目标。'
        )),
        HumanMessage(content=json.dumps({
            'task': {
                'id': getattr(task, 'id', None),
                'name': getattr(task, 'name', '') or '',
                'target_module': getattr(task, 'target_module', '') or '',
                'target_url': getattr(task, 'target_url', '') or '',
                'source_type': getattr(task, 'source_type', '') or '',
                'requirement': getattr(task, 'source_requirement', '') or '',
                'gherkin': getattr(task, 'gherkin', '') or '',
            },
            'output_schema': {
                'mode': 'prerequisite|test_subject|authentication_then_business',
                'confidence': 0.0,
                'rationale': '一句话说明判断依据',
            },
        }, ensure_ascii=False)),
    ]
    try:
        request_timeout = _safe_positive_int(
            policy.get('authentication_contract_request_timeout') or os.environ.get('AI_UI_AUTH_CONTRACT_REQUEST_TIMEOUT'),
            45,
        )
        total_budget = _safe_positive_int(
            policy.get('authentication_contract_total_budget') or os.environ.get('AI_UI_AUTH_CONTRACT_TOTAL_BUDGET'),
            60,
        )
        response_content = _invoke_openai_compatible_chat(
            active_config,
            messages,
            temperature=0.0,
            request_timeout=request_timeout,
            max_retries=1,
            total_budget=total_budget,
            max_completion_tokens=512,
        )
        parsed = extract_json_from_response(response_content)
        if not isinstance(parsed, dict):
            raise ValueError('LLM 未返回 JSON 对象')
        mode = _valid_authentication_mode(parsed.get('mode'))
        if not mode:
            raise ValueError(f'LLM 返回未知认证模式: {parsed.get("mode")}')
        return {
            'mode': mode,
            'source': 'llm',
            'confidence': parsed.get('confidence') if isinstance(parsed.get('confidence'), (int, float)) else None,
            'rationale': str(parsed.get('rationale') or '')[:500],
        }
    except Exception as exc:
        logger.warning(
            'AI UI 认证合同 LLM 分类失败: task_id=%s, error=%s',
            getattr(task, 'id', None),
            f'{type(exc).__name__}: {str(exc)[:300]}',
        )
        return {
            'mode': 'unknown',
            'status': 'degraded',
            'source': 'llm_error_default',
            'error_type': type(exc).__name__,
            'confidence': 0.0,
            'rationale': f'LLM 分类失败，认证阶段角色不确定: {type(exc).__name__}',
        }


def _resolve_step_element(step: dict[str, Any], by_key: dict[str, dict[str, Any]], by_name: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    element_key = str(step.get('element_key') or '').strip()
    page_key = str(step.get('page_key') or '').strip()
    if element_key:
        composite_key = f'{page_key}:{element_key}' if page_key else ''
        if composite_key and composite_key in by_key:
            return by_key[composite_key]
        if element_key in by_key:
            return by_key[element_key]

    candidates = [
        step.get('target_name'),
        step.get('element_name'),
        step.get('element_hint'),
    ]
    for value in candidates:
        normalized = _normalize_text(value)
        if normalized and normalized in by_name:
            return by_name[normalized]

    target = _normalize_text(' '.join(str(value or '') for value in candidates + [step.get('description')]))
    if target:
        for name, element in by_name.items():
            if len(name) >= 2 and (name in target or target in name):
                return element
    return None


def _element_is_checkable(element: dict[str, Any]) -> bool:
    role = _normalize_text(element.get('role') or element.get('aria_role') or '')
    tag = _normalize_text(element.get('tag') or '')
    input_type = _normalize_text(element.get('input_type') or element.get('type') or '')
    return role in {'checkbox', 'radio'} or (tag == 'input' and input_type in {'checkbox', 'radio'})


def _canonical_element_operation(operation: str, element: dict[str, Any]) -> str:
    """Use the observed control semantics when compiling an LLM action."""
    if operation == 'click' and _element_is_checkable(element):
        return 'check'
    return operation


def _is_risky_step(step: dict[str, Any], safety_policy: dict[str, Any]) -> bool:
    return _is_dangerous_step(step, safety_policy) or _is_confirmation_step(step, safety_policy)


def _normalize_llm_plan(task, parsed: dict[str, Any], element_map: dict[str, Any]) -> dict[str, Any]:
    by_key, by_name = _build_element_indexes(element_map)
    safety_policy = task.safety_policy or {}
    max_steps = max(1, min(int(safety_policy.get('max_steps') or 24), 50))
    generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
    test_plan = parsed.get('test_plan') if isinstance(parsed.get('test_plan'), dict) else {}
    raw_steps = generated_case.get('steps') or test_plan.get('steps') or []
    normalized_steps = []

    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            continue
        operation = OPERATION_ALIASES.get(
            str(raw_step.get('operation') or raw_step.get('action') or '').strip().lower(),
            str(raw_step.get('operation') or raw_step.get('action') or '').strip().lower(),
        )
        if operation not in ALLOWED_OPERATIONS:
            continue

        step = {**raw_step, 'operation': operation}
        if operation != 'click':
            step.pop('state_transition', None)
        runtime_text_assertion = bool(
            operation in {'assert_visible', 'assert_text', 'assert_contain_text'}
            and step.get('binding_mode') == 'runtime_text'
            and str(step.get('value') or '').strip()
        )
        runtime_state_assertion = bool(
            is_assertion_operation(operation)
            and step.get('binding_mode') == 'runtime_state'
        )
        element = None if operation in {'goto', 'wait'} or runtime_text_assertion or runtime_state_assertion else _resolve_step_element(step, by_key, by_name)
        if element:
            step['page_key'] = element.get('page_key') or step.get('page_key') or ''
            step['element_key'] = element.get('element_key') or step.get('element_key') or ''
            step['target_name'] = element.get('name') or step.get('target_name') or ''
            step['locator_hint'] = (
                _llm_locator_allowed_for_element(step.get('locator_hint'), element)
                or _locator_hint(element)
            )
            locator_candidates = [
                step['locator_hint'],
                *(
                    [element.get('recommended_locator')]
                    if isinstance(element.get('recommended_locator'), dict)
                    else []
                ),
                *[
                    candidate for candidate in (element.get('locator_candidates') or [])
                    if isinstance(candidate, dict)
                ],
            ]
            unique_candidates = []
            seen_locator_ids = set()
            for candidate in locator_candidates:
                compact_candidate = _compact_locator(candidate)
                identity = _locator_identity(compact_candidate)
                if not identity[0] or not identity[1] or identity in seen_locator_ids:
                    continue
                seen_locator_ids.add(identity)
                unique_candidates.append(compact_candidate)
                if len(unique_candidates) >= 8:
                    break
            step['locator_candidates'] = unique_candidates
            operation = _canonical_element_operation(operation, element)
            step['operation'] = operation
            if operation == 'fill' and (
                'select_option' in (element.get('actions') or [])
                or element.get('tag') == 'select'
                or element.get('role') == 'select'
            ):
                operation = 'select_option'
                step['operation'] = operation
            if element.get('context_type'):
                step['context_type'] = element.get('context_type')
            if element.get('frame_key'):
                step['frame_key'] = element.get('frame_key')
                step['frame_name'] = element.get('frame_name') or ''
                step['frame_url'] = element.get('frame_url') or ''
            if element.get('host_path'):
                step['host_path'] = element.get('host_path')
            row_scope = _row_scope(element)
            if row_scope:
                step['row_scope'] = row_scope
            if (
                operation in {'fill', 'select_option'}
                and not step.get('value')
                and step.get('binding_mode') != 'runtime_input'
            ):
                step['value'] = _infer_test_value(element)
        elif runtime_text_assertion:
            step['page_key'] = ''
            step['element_key'] = ''
            step['locator_hint'] = {'type': 'text', 'value': str(step.get('value') or '').strip()}
            step['requires_confirmation'] = False
            step['risk_level'] = 'low'
        elif runtime_state_assertion:
            step['element_key'] = ''
            step['locator_hint'] = {}
            step['locator_candidates'] = []
            step['runtime_resolver'] = step.get('runtime_resolver') or 'state_evidence'
            if not isinstance(step.get('state_assertion'), dict):
                step['state_assertion'] = {
                    'expected': step.get('expected') or step.get('value') or '',
                    'target': step.get('target_name') or '',
                }
            step['requires_confirmation'] = False
            step['risk_level'] = 'low'
        elif operation not in {'goto', 'wait'}:
            step['requires_confirmation'] = True
            step['risk_level'] = 'medium'
            step['unresolved_reason'] = '未能匹配到元素地图中的 element_key'

        if _is_dangerous_step(step, safety_policy):
            step['requires_confirmation'] = True
            if operation in {'click', 'fill', 'select_option', 'check', 'uncheck'} and not safety_policy.get('allow_destructive_actions'):
                step['original_operation'] = operation
                step['operation'] = 'assert_visible'
                step['value'] = ''
                step['expected'] = step.get('expected') or '高风险动作入口可见，未实际执行'
            step['risk_level'] = 'high'
        elif _is_confirmation_step(step, safety_policy):
            if safety_policy.get('allow_form_submit') or _is_low_risk_form_submit(task, step):
                step['requires_confirmation'] = False
                step['risk_level'] = step.get('risk_level') or 'low'
                step.setdefault('safety_note', '普通表单保存动作，已按测试数据隔离策略允许执行')
            else:
                step['requires_confirmation'] = True
                if operation in {'click', 'fill', 'select_option', 'check', 'uncheck'}:
                    step['original_operation'] = operation
                    step['operation'] = 'assert_visible'
                    step['value'] = ''
                    step['expected'] = step.get('expected') or '需确认动作入口可见，未实际提交'
                step['risk_level'] = 'medium'

        step['step_sort'] = len(normalized_steps)
        step.setdefault('risk_level', 'low')
        step.setdefault('requires_confirmation', False)
        step.setdefault('confidence', 0.7 if element or operation in {'goto', 'wait'} else 0.3)
        normalized_steps.append(step)
        if len(normalized_steps) >= max_steps:
            break

    if not normalized_steps:
        return _fallback_plan(task, element_map, 'LLM 规划经服务端校验后没有可执行步骤')

    data_lifecycle = _build_data_lifecycle(task, normalized_steps, test_plan)
    parsed['test_plan'] = {
        **test_plan,
        'steps': normalized_steps,
        'test_data': data_lifecycle['test_data'] or test_plan.get('test_data') or {},
        'data_lifecycle': data_lifecycle,
        'page_context_usage': test_plan.get('page_context_usage') or test_plan.get('mcp_context_usage') or [
            f"已按 element_key 精确绑定 {sum(1 for step in normalized_steps if step.get('element_key'))} 个元素步骤"
        ],
    }
    parsed['generated_case'] = {
        **generated_case,
        'name': generated_case.get('name') or task.target_module or task.name,
        'description': generated_case.get('description') or task.source_requirement or task.gherkin or '',
        'steps': normalized_steps,
        'data_lifecycle': data_lifecycle,
    }
    return parsed


def _build_state_transition_step(transition: dict[str, Any], page_key: str) -> dict[str, Any]:
    locator_hint = transition.get('locator_hint') if isinstance(transition.get('locator_hint'), dict) else {}
    return {
        'step_sort': 0,
        'action': 'click',
        'operation': 'click',
        'page_key': transition.get('from_page_key') or page_key,
        'element_key': transition.get('element_key') or '',
        'target_name': transition.get('target_name') or '',
        'description': f"执行业务状态迁移：{transition.get('target_name') or '业务入口'}",
        'locator_hint': locator_hint,
        'value': '',
        'expected': '目标业务状态已就绪',
        'risk_level': 'low',
        'requires_confirmation': False,
        'confidence': 0.82,
        'state_transition': {
            'to_page_key': transition.get('to_page_key') or '',
            'to_url': transition.get('to_url') or '',
        },
    }


def _plan_step_operation(step: dict[str, Any]) -> str:
    operation = str(step.get('operation') or step.get('action') or '').strip().lower()
    return OPERATION_ALIASES.get(operation, operation)


def _plan_click_identity(step: dict[str, Any]) -> tuple[str, str, str, str]:
    locator = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
    return (
        _normalize_text(step.get('element_key') or ''),
        _normalize_text(locator.get('type') or ''),
        _normalize_text(locator.get('value') or ''),
        _normalize_text(locator.get('name') or step.get('target_name') or ''),
    )


def _equivalent_click_step(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _plan_step_operation(left) != 'click' or _plan_step_operation(right) != 'click':
        return False
    left_key, left_type, left_value, left_name = _plan_click_identity(left)
    right_key, right_type, right_value, right_name = _plan_click_identity(right)
    if left_key and right_key and left_key == right_key:
        return True
    return bool(
        left_type
        and left_type == right_type
        and left_value == right_value
        and (not left_name or not right_name or left_name == right_name or left_name in right_name or right_name in left_name)
    )


def _renumber_plan_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for step in steps:
        previous_action_id = str(normalized[-1].get('action_id') or '') if normalized and isinstance(normalized[-1], dict) else ''
        current_action_id = str(step.get('action_id') or '') if isinstance(step, dict) else ''
        if (
            normalized
            and isinstance(step, dict)
            and isinstance(normalized[-1], dict)
            and _equivalent_click_step(normalized[-1], step)
            and not step.get('state_transition')
            and not (
                previous_action_id
                and current_action_id
                and previous_action_id != current_action_id
            )
        ):
            # 相邻等价 click 不能产生新的页面状态，通常是 planner 在
            # 重规划/状态迁移编译时重复注入；保留第一次并重新编号。
            continue
        normalized.append(step)
    for index, step in enumerate(normalized):
        if isinstance(step, dict):
            step['step_sort'] = index
    return normalized


def _normalise_state_transition_step_order(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 state_transition 点击放到目标页面首次使用前，避免排到登录前。"""
    if not isinstance(steps, list) or not steps:
        return steps

    transitions: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for step in steps:
        if (
            isinstance(step, dict)
            and isinstance(step.get('state_transition'), dict)
            and step.get('state_transition', {}).get('to_page_key')
        ):
            transitions.append(dict(step))
        else:
            remaining.append(dict(step) if isinstance(step, dict) else step)

    if not transitions:
        return _renumber_plan_steps(remaining)

    for transition_step in transitions:
        to_page_key = str(transition_step.get('state_transition', {}).get('to_page_key') or '')
        target_index: int | None = None
        for index, step in enumerate(remaining):
            if not isinstance(step, dict):
                continue
            if str(step.get('page_key') or '') != to_page_key:
                continue
            if _plan_step_operation(step) in {'goto', 'wait', 'observe'}:
                continue
            target_index = index
            break

        if target_index is None:
            remaining.append(transition_step)
            continue

        if any(
            isinstance(step, dict) and _equivalent_click_step(transition_step, step)
            for step in remaining[:target_index]
        ):
            continue

        insert_at = target_index
        while insert_at > 0 and isinstance(remaining[insert_at - 1], dict) and _plan_step_operation(remaining[insert_at - 1]) in {'wait', 'observe'}:
            insert_at -= 1
        remaining.insert(insert_at, transition_step)

    return _renumber_plan_steps(remaining)


def _ensure_state_transition_steps_for_plan(
    task,
    steps: list[dict[str, Any]],
    element_map: dict[str, Any],
    max_steps: int,
) -> list[dict[str, Any]]:
    """LLM 没把状态迁移写进来时，按元素地图里的 transition 补上。"""
    if not isinstance(steps, list) or not steps or not isinstance(element_map, dict):
        return steps

    safety_policy = task.safety_policy or {}
    transitions = [
        item for item in (element_map.get('state_transitions') or [])
        if isinstance(item, dict) and isinstance(item.get('locator_hint'), dict) and item.get('locator_hint', {}).get('type')
    ]
    if not transitions:
        return _normalise_state_transition_step_order(steps)

    by_key, _ = _build_element_indexes(element_map)
    form_page_keys = set()
    form_element_keys = set()
    for page in element_map.get('pages') or []:
        if not isinstance(page, dict):
            continue
        if not _is_business_form_covered(page, safety_policy):
            continue
        page_key = str(page.get('page_key') or '')
        if page_key:
            form_page_keys.add(page_key)
        for element in page.get('elements') or []:
            if not isinstance(element, dict):
                continue
            element_key = str(element.get('element_key') or '')
            if not element_key:
                continue
            if page_key:
                form_element_keys.add(f'{page_key}:{element_key}')
            form_element_keys.add(element_key)

    first_form_index: int | None = None
    target_page_key = ''
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        operation = str(step.get('operation') or step.get('action') or '').strip().lower()
        element_key = str(step.get('element_key') or '')
        composite_key = f"{step.get('page_key') or ''}:{element_key}" if element_key else ''
        step_page_key = str(step.get('page_key') or '')
        if (
            (composite_key and composite_key in form_element_keys)
            or (element_key and element_key in form_element_keys)
            or (step_page_key and step_page_key in form_page_keys)
        ):
            first_form_index = index
            target_page_key = step_page_key or target_page_key
            break

    if first_form_index is None:
        return _normalise_state_transition_step_order(steps)

    if any(
        isinstance(step, dict)
        and not step.get('state_transition')
        and (
            str(step.get('element_key') or '') in {
                str(item.get('element_key') or '')
                for item in transitions
                if isinstance(item, dict) and item.get('element_key')
            }
        )
        for step in steps[:first_form_index]
    ):
        return _normalise_state_transition_step_order(steps)

    candidates = []
    for transition in transitions:
        score = 0
        if target_page_key and str(transition.get('to_page_key') or '') == target_page_key:
            score += 200
        if transition.get('stage') == 'open_business_form':
            score += 100
        if score > 0:
            candidates.append((score, transition))
    if not candidates:
        return steps

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected_transition = candidates[0][1]
    selected_to_url = str(selected_transition.get('to_url') or '')
    selected_to_page = str(selected_transition.get('to_page_key') or target_page_key)
    sequence = [
        transition for transition in transitions
        if (
            (selected_to_url and str(transition.get('to_url') or '') == selected_to_url)
            or (selected_to_page and str(transition.get('to_page_key') or '') == selected_to_page)
        )
    ]
    transition_steps: list[dict[str, Any]] = []
    augmented = [dict(step) if isinstance(step, dict) else step for step in steps]
    for transition in sequence:
        existing_index = next(
            (
                index for index, step in enumerate(augmented)
                if isinstance(step, dict) and _equivalent_click_step(
                    step,
                    _build_state_transition_step(transition, target_page_key),
                )
            ),
            None,
        )
        if existing_index is None:
            transition_steps.append(_build_state_transition_step(transition, target_page_key))
        else:
            existing = augmented.pop(existing_index)
            existing['state_transition'] = {
                'to_page_key': transition.get('to_page_key') or '',
                'to_url': transition.get('to_url') or '',
            }
            transition_steps.append(existing)

    first_form_index = next(
        (
            index for index, step in enumerate(augmented)
            if isinstance(step, dict)
            and str(step.get('page_key') or '') in form_page_keys
            and _plan_step_operation(step) not in {'goto', 'wait', 'observe'}
        ),
        len(augmented),
    )
    authentication_end = max(
        (
            index + 1 for index, step in enumerate(augmented)
            if isinstance(step, dict) and step.get('requirement_phase') == 'authentication'
        ),
        default=0,
    )
    first_form_index = max(first_form_index, authentication_end)
    available = max(0, max_steps - len(augmented))
    if available < len(transition_steps):
        transition_steps = transition_steps[:available]
    augmented[first_form_index:first_form_index] = transition_steps
    return _renumber_plan_steps(augmented)


def _compile_selected_route_steps(
    steps: list[dict[str, Any]],
    route: dict[str, Any],
    element_map: dict[str, Any],
    max_steps: int,
) -> list[dict[str, Any]]:
    """Compile the route role's selected transitions into the executable step sequence."""
    if not steps or not isinstance(route, dict) or not isinstance(element_map, dict):
        return steps
    selected_keys = [
        str(key) for key in (route.get('transition_keys') or [])
        if str(key or '').strip()
    ]
    transitions_by_key = {
        str(item.get('transition_key') or item.get('element_key') or ''): item
        for item in (element_map.get('state_transitions') or [])
        if isinstance(item, dict)
        and isinstance(item.get('locator_hint'), dict)
        and item.get('locator_hint', {}).get('type')
    }
    selected = [transitions_by_key[key] for key in selected_keys if key in transitions_by_key]
    if not selected:
        return _renumber_plan_steps(steps)

    augmented = [dict(step) for step in steps if isinstance(step, dict)]
    cursor = 0
    for index, transition in enumerate(selected):
        transition_step = _build_state_transition_step(
            transition,
            str(transition.get('to_page_key') or ''),
        )
        existing_index = next((
            step_index
            for step_index in range(cursor, len(augmented))
            if _equivalent_click_step(augmented[step_index], transition_step)
        ), None)
        if existing_index is not None:
            augmented[existing_index]['state_transition'] = {
                'to_page_key': transition.get('to_page_key') or '',
                'to_url': transition.get('to_url') or '',
            }
            cursor = existing_index + 1
            continue

        next_existing_index = None
        for next_transition in selected[index + 1:]:
            next_step = _build_state_transition_step(
                next_transition,
                str(next_transition.get('to_page_key') or ''),
            )
            next_existing_index = next((
                step_index
                for step_index in range(cursor, len(augmented))
                if _equivalent_click_step(augmented[step_index], next_step)
            ), None)
            if next_existing_index is not None:
                break
        insert_at = next_existing_index if next_existing_index is not None else cursor
        if len(augmented) >= max_steps:
            continue
        augmented.insert(insert_at, transition_step)
        cursor = insert_at + 1
    return _renumber_plan_steps(augmented)


def _transition_step_matches_observed(step: dict[str, Any], observed: list[dict[str, Any]]) -> bool:
    if not isinstance(step, dict) or _plan_step_operation(step) != 'click':
        return False
    transition = step.get('state_transition') if isinstance(step.get('state_transition'), dict) else {}
    if not transition:
        return False
    element_key = str(step.get('element_key') or '')
    to_page_key = str(transition.get('to_page_key') or '')
    to_url = str(transition.get('to_url') or '')
    step_locator = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
    return any(
        (
            str(item.get('element_key') or '') == element_key
            or (
                isinstance(item.get('locator_hint'), dict)
                and item.get('locator_hint', {}).get('type') == step_locator.get('type')
                and item.get('locator_hint', {}).get('value') == step_locator.get('value')
                and (
                    not item.get('locator_hint', {}).get('name')
                    or not step_locator.get('name')
                    or _normalize_text(item.get('locator_hint', {}).get('name')) == _normalize_text(step_locator.get('name'))
                )
            )
        )
        and (not to_page_key or str(item.get('to_page_key') or '') == to_page_key)
        and (not to_url or str(item.get('to_url') or '') == to_url)
        for item in observed
    )


def _transition_requirement_score(transition: dict[str, Any], requirement: dict[str, Any]) -> int:
    operation = str(requirement.get('operation') or '').strip().lower()
    if operation != 'click':
        return 0
    if str(transition.get('operation') or 'click').strip().lower() not in {'', 'click'}:
        return 0
    target = _normalize_text(requirement.get('target') or requirement.get('name') or requirement.get('target_name') or '')
    value = _normalize_text(requirement.get('value') or requirement.get('expected') or '')
    terms = [_normalize_text(term) for term in _requirement_terms(requirement)]
    text = _normalize_text(' '.join(str(transition.get(key) or '') for key in [
        'target_name', 'element_key', 'stage',
    ]))
    locator = transition.get('locator_hint') if isinstance(transition.get('locator_hint'), dict) else {}
    text = _normalize_text(' '.join([
        text,
        str(locator.get('name') or ''),
        str(locator.get('value') or ''),
    ]))
    score = 0
    if target:
        if target == text:
            score += 1000
        elif target in text or text in target:
            score += 420 + min(len(target), len(text), 80)
        else:
            score += _text_overlap_score(target, text) * 12
    if value:
        if value in text or text in value:
            score += 380 + min(len(value), len(text), 80)
        else:
            score += _text_overlap_score(value, text) * 10
    for term in terms:
        if not term:
            continue
        if term in text:
            score += min(90, len(term) * 12)
    return score


def _compile_flow_transition_steps(
    steps: list[dict[str, Any]],
    requirements: dict[str, Any],
    element_map: dict[str, Any],
    max_steps: int,
    bindings: dict[str, Any] | BindingContract | None = None,
) -> list[dict[str, Any]]:
    """Compile required click flow actions from observed transition evidence only."""
    if not isinstance(steps, list) or not isinstance(requirements, dict) or not isinstance(element_map, dict):
        return steps
    contract = build_requirement_contract(requirements)
    binding_contract = BindingContract.from_result(bindings or {})
    click_flow = [
        item for item in contract.flow
        if isinstance(item, dict)
        and item.get('required', True)
        and str(item.get('action_id') or '').strip()
        and str(item.get('operation') or '').strip().lower() == 'click'
    ]
    observed = [
        item for item in (element_map.get('state_transitions') or [])
        if isinstance(item, dict)
        and isinstance(item.get('locator_hint'), dict)
        and item.get('locator_hint', {}).get('type')
    ]
    if not click_flow or not observed:
        return _normalise_state_transition_step_order(steps)

    remaining: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if isinstance(step.get('state_transition'), dict) and not _transition_step_matches_observed(step, observed):
            step = {key: value for key, value in step.items() if key != 'state_transition'}
        remaining.append(dict(step))

    used_transition_keys: set[tuple[str, str, str]] = set()
    compiled: list[dict[str, Any]] = []
    cursor = 0
    for requirement in click_flow:
        action_id = str(requirement.get('action_id') or '')
        binding = binding_contract.bindings.get(action_id)
        scored = [
            (_transition_requirement_score(transition, requirement), transition)
            for transition in observed
            if (
                str(transition.get('element_key') or ''),
                str(transition.get('to_page_key') or ''),
                str(transition.get('to_url') or ''),
            ) not in used_transition_keys
            and (
                not binding
                or binding_key_matches(
                    transition.get('from_page_key') or '',
                    transition.get('element_key') or '',
                    binding.get('page_key') or '',
                    binding.get('element_key') or '',
                )
            )
        ]
        scored = [(score, transition) for score, transition in scored if score > 0]
        if not scored:
            continue
        scored.sort(key=lambda item: item[0], reverse=True)
        transition = scored[0][1]
        used_transition_keys.add((
            str(transition.get('element_key') or ''),
            str(transition.get('to_page_key') or ''),
            str(transition.get('to_url') or ''),
        ))
        transition_step = _build_state_transition_step(
            transition,
            str(transition.get('from_page_key') or ''),
        )
        transition_step['action_id'] = action_id
        transition_step['requirement_phase'] = requirement.get('phase') or transition.get('stage') or ''
        transition_step['expected'] = requirement.get('expected') or transition_step.get('expected') or ''
        transition_step['description'] = (
            requirement.get('description')
            or f"执行已观察业务状态迁移：{transition.get('target_name') or action_id}"
        )

        existing_index = next((
            index for index in range(cursor, len(remaining))
            if (
                isinstance(remaining[index], dict)
                and (
                    str(remaining[index].get('action_id') or '') == action_id
                    or _equivalent_click_step(remaining[index], transition_step)
                )
            )
        ), None)
        if existing_index is not None:
            existing = remaining.pop(existing_index)
            merged = {
                **existing,
                **transition_step,
                'action_id': action_id,
            }
            compiled.append(merged)
            cursor = max(cursor, existing_index)
        elif len(remaining) + len(compiled) < max_steps:
            compiled.append(transition_step)

    if not compiled:
        return _normalise_state_transition_step_order(remaining)

    compiled_action_ids = {
        str(step.get('action_id') or '')
        for step in compiled
        if isinstance(step, dict) and step.get('action_id')
    }
    compiled_click_keys = {
        _plan_click_identity(step)
        for step in compiled
        if isinstance(step, dict)
    }
    kept = [
        step for step in remaining
        if not (
            isinstance(step, dict)
            and (
                str(step.get('action_id') or '') in compiled_action_ids
                or _plan_click_identity(step) in compiled_click_keys
            )
        )
    ]

    insert_at = 0
    while insert_at < len(kept) and isinstance(kept[insert_at], dict) and _plan_step_operation(kept[insert_at]) in {'goto', 'wait'}:
        insert_at += 1
    combined = [*kept[:insert_at], *compiled, *kept[insert_at:]]
    return _normalise_state_transition_step_order(combined[:max_steps])


def _step_deduplicate_identity(step: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    if not isinstance(step, dict) or _plan_step_operation(step) != 'click':
        return ()
    locator = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
    return (
        _plan_step_operation(step),
        _normalize_text(step.get('page_key') or ''),
        _normalize_text(step.get('element_key') or ''),
        _normalize_text(locator.get('type') or ''),
        _normalize_text(locator.get('value') or ''),
        _normalize_text(locator.get('name') or step.get('target_name') or ''),
    )


def _mark_step_covers_action(step: dict[str, Any], covered_action_id: str) -> None:
    covered_action_id = str(covered_action_id or '').strip()
    if not covered_action_id:
        return
    current_action_id = str(step.get('action_id') or '').strip()
    if covered_action_id == current_action_id:
        return
    covered = [
        str(item)
        for item in (step.get('covered_action_ids') if isinstance(step.get('covered_action_ids'), list) else [])
        if str(item or '').strip()
    ]
    if covered_action_id not in covered:
        covered.append(covered_action_id)
    step['covered_action_ids'] = covered


def _can_cover_duplicate_contract_step(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    if _plan_step_operation(previous) != 'click' or _plan_step_operation(current) != 'click':
        return False
    previous_action_id = str(previous.get('action_id') or '').strip()
    current_action_id = str(current.get('action_id') or '').strip()
    if not previous_action_id or not current_action_id:
        return True
    if previous_action_id == current_action_id:
        return True
    return not isinstance(current.get('state_transition'), dict)


def _compile_final_contract_step_order(
    steps: list[dict[str, Any]],
    requirements: dict[str, Any],
    max_steps: int,
) -> list[dict[str, Any]]:
    """Make requirement_contract.flow the final ordering authority."""
    if not isinstance(steps, list) or not isinstance(requirements, dict):
        return steps
    contract = build_requirement_contract(requirements)
    flow_order = {
        str(action_id): index
        for index, action_id in enumerate(contract.action_ids)
        if str(action_id or '').strip()
    }
    if not flow_order:
        return _renumber_plan_steps([dict(step) for step in steps if isinstance(step, dict)])

    copied = [dict(step) for step in steps if isinstance(step, dict)]

    def order_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        index, step = item
        action_id = str(step.get('action_id') or '').strip()
        operation = _plan_step_operation(step)
        if operation in {'goto', 'wait'} and action_id not in flow_order:
            return (0, index, index)
        if action_id in flow_order:
            return (1, flow_order[action_id], index)
        return (2, index, index)

    ordered = [step for _, step in sorted(enumerate(copied), key=order_key)]
    result: list[dict[str, Any]] = []
    seen_clicks: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for step in ordered:
        identity = _step_deduplicate_identity(step)
        previous = seen_clicks.get(identity) if identity else None
        if previous is not None and _can_cover_duplicate_contract_step(previous, step):
            _mark_step_covers_action(previous, str(step.get('action_id') or ''))
            for covered_action_id in step.get('covered_action_ids') or []:
                _mark_step_covers_action(previous, str(covered_action_id or ''))
            continue
        result.append(step)
        if identity:
            seen_clicks[identity] = step
    return _renumber_plan_steps(result[:max_steps])


def _semantic_coverage_issues(
    task: Any | None,
    steps: list[dict[str, Any]],
    element_map: dict[str, Any],
    safety_policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if task is None:
        return []

    operations = {
        str(step.get('operation') or '').strip().lower()
        for step in steps
        if isinstance(step, dict)
    }
    issues: list[dict[str, Any]] = []
    explicit_requirements = _extract_explicit_action_requirements(task)
    step_records = [
        (
            str(step.get('action_id') or ''),
            str(step.get('operation') or '').strip().lower(),
            _normalize_text(' '.join(str(step.get(key) or '') for key in [
                'target_name', 'description', 'value', 'expected', 'action',
            ])),
        )
        for step in steps
        if isinstance(step, dict)
    ]

    policy = safety_policy if isinstance(safety_policy, dict) else {}
    task_policy = getattr(task, 'safety_policy', {}) if task is not None else {}
    if not isinstance(task_policy, dict):
        task_policy = {}
    requirement_contract = (
        policy.get('requirement_contract')
        if isinstance(policy.get('requirement_contract'), dict)
        else task_policy.get('requirement_contract')
        if isinstance(task_policy.get('requirement_contract'), dict)
        else getattr(task, 'requirement_contract', {})
        if isinstance(getattr(task, 'requirement_contract', {}), dict)
        else {}
    )
    contract_flow = [
        item for item in (requirement_contract.get('flow') or [])
        if isinstance(item, dict) and item.get('required', True)
    ]
    for requirement in contract_flow:
        action_id = str(requirement.get('action_id') or '')
        operation = str(requirement.get('operation') or '').strip().lower()
        if not operation or operation in {'goto', 'wait'}:
            continue
        expected_ops = {'fill', 'select_option', 'check', 'uncheck'} if operation in {
            'fill', 'select_option', 'check', 'uncheck', 'choose',
        } else {operation}
        target = _normalize_text(requirement.get('target') or requirement.get('name') or requirement.get('target_name') or '')
        value = _normalize_text(requirement.get('value') or requirement.get('expected') or '')
        covered = any(
            (
                (action_id and step_action_id == action_id)
                or (
                    step_operation in expected_ops
                    and (
                        (target and target in text)
                        or (value and value in text)
                    )
                )
            )
            and step_operation in expected_ops
            for step_action_id, step_operation, text in step_records
        )
        if not covered:
            issues.append({
                'reason': 'missing_required_contract_action',
                'action_id': action_id,
                'operation': operation,
                'target': requirement.get('target') or requirement.get('name') or '',
                'message': f"最终计划未覆盖需求合同动作 {action_id or operation}",
            })
    for requirement in explicit_requirements.get('fields') or []:
        name = _normalize_text(requirement.get('name') or '')
        value = _normalize_text(requirement.get('value') or '')
        expected_ops = {'fill', 'select_option', 'check', 'uncheck'}
        if not any(
            operation in expected_ops
            and (
                (name and name in text)
                or (value and value in text)
                or (value and any(part and part in text for part in re.split(r'[\s_/，,。；;:：()（）]+', value)))
            )
            for _action_id, operation, text in step_records
        ):
            issues.append({
                'reason': 'missing_explicit_field_action',
                'name': requirement.get('name') or '',
                'value': requirement.get('value') or '',
                'message': f"用户明确要求字段动作未覆盖: {requirement.get('name') or ''}",
            })
    for requirement in explicit_requirements.get('clicks') or []:
        name = _normalize_text(requirement.get('name') or '')
        requirement_matches = _matched_elements_for_requirements(
            {'fields': [], 'clicks': [requirement], 'assertions': []},
            _compact_element_map(element_map, task=task),
        )
        if not requirement_matches:
            continue
        if name and not any(operation == 'click' and name in text for _action_id, operation, text in step_records):
            issues.append({
                'reason': 'missing_explicit_click_action',
                'name': requirement.get('name') or '',
                'message': f"用户明确要求点击动作未覆盖: {requirement.get('name') or ''}",
            })
    return issues


def _unobserved_state_transition_issues(
    steps: list[dict[str, Any]],
    element_map: dict[str, Any],
) -> list[dict[str, Any]]:
    observed = [
        item for item in (element_map.get('state_transitions') or [])
        if isinstance(item, dict)
    ]
    issues: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get('state_transition'), dict):
            continue
        if _plan_step_operation(step) != 'click':
            continue
        transition = step['state_transition']
        element_key = str(step.get('element_key') or '')
        to_page_key = str(transition.get('to_page_key') or '')
        to_url = str(transition.get('to_url') or '')
        step_locator = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
        if any(
            (
                str(item.get('element_key') or '') == element_key
                or (
                    isinstance(item.get('locator_hint'), dict)
                    and item.get('locator_hint', {}).get('type') == step_locator.get('type')
                    and item.get('locator_hint', {}).get('value') == step_locator.get('value')
                    and (
                        not item.get('locator_hint', {}).get('name')
                        or not step_locator.get('name')
                        or _normalize_text(item.get('locator_hint', {}).get('name')) == _normalize_text(step_locator.get('name'))
                    )
                )
            )
            and (not to_page_key or str(item.get('to_page_key') or '') == to_page_key)
            and (not to_url or str(item.get('to_url') or '') == to_url)
            for item in observed
        ):
            continue
        issues.append({
            'reason': 'unobserved_state_transition',
            'step_sort': step.get('step_sort'),
            'element_key': element_key,
            'target_name': step.get('target_name') or '',
            'message': '步骤声明了元素地图中不存在的状态迁移边，不能作为可执行计划',
        })
    return issues


def _validate_generated_steps(
    steps: list[dict[str, Any]],
    element_map: dict[str, Any],
    safety_policy: dict[str, Any],
    task: Any | None = None,
) -> dict[str, Any]:
    """静态验证 LLM 步骤是否可落库、是否绑定元素地图、是否触发安全边界。"""
    by_key, _ = _build_element_indexes(element_map)
    unresolved_steps = []
    risky_steps = []
    low_confidence_steps = []
    runtime_assertion_issues = []
    bound_element_steps = 0
    latest_runtime_text = _latest_runtime_page_text(element_map)

    for step in steps:
        if not isinstance(step, dict):
            continue
        operation = step.get('operation')
        if _is_unresolved_gap_step(step) and operation not in {'goto', 'wait'}:
            unresolved_steps.append({
                'step_sort': step.get('step_sort'),
                'operation': operation,
                'target_name': step.get('target_name') or '',
                'action_id': step.get('action_id') or '',
                'reason': step.get('unresolved_reason') or step.get('description') or 'required_action_unresolved',
            })
            continue
        if operation not in ALLOWED_OPERATIONS:
            unresolved_steps.append({
                'step_sort': step.get('step_sort'),
                'operation': operation,
                'reason': 'operation_not_allowed',
            })
            continue

        runtime_text_assertion = bool(
            operation in {'assert_visible', 'assert_text', 'assert_contain_text'}
            and step.get('binding_mode') == 'runtime_text'
            and str(step.get('value') or '').strip()
        )
        runtime_state_assertion = _is_runtime_state_assertion(step)
        reveal_candidates = (
            _runtime_assertion_reveal_candidates(step, steps, element_map)
            if runtime_text_assertion else []
        )
        if runtime_state_assertion and not _runtime_state_page_supported(step, element_map):
            runtime_assertion_issues.append({
                'step_sort': step.get('step_sort'),
                'operation': operation,
                'target_name': step.get('target_name') or '',
                'reason': 'runtime_state_scope_not_observed',
                'message': '运行时状态断言缺少已观察页面作用域，不能采集动作前后状态证据',
                'page_key': step.get('page_key') or '',
            })
        if (
            runtime_text_assertion
            and latest_runtime_text
            and not _runtime_assertion_text_supported(step, latest_runtime_text)
            and not reveal_candidates
        ):
            expected_text = _runtime_assertion_expected_text(step)
            runtime_assertion_issues.append({
                'step_sort': step.get('step_sort'),
                'operation': operation,
                'target_name': step.get('target_name') or '',
                'reason': 'runtime_assertion_text_not_observed',
                'message': '运行时断言文本没有当前页面事实支撑',
                'expected_text': expected_text,
                'observed_runtime_text_excerpt': latest_runtime_text[:500],
            })
        if operation not in {'goto', 'wait'} and not runtime_text_assertion and not runtime_state_assertion:
            element_key = str(step.get('element_key') or '').strip()
            page_key = str(step.get('page_key') or '').strip()
            element = _lookup_bound_element(by_key, page_key, element_key)
            if not element_key or element is None:
                unresolved_steps.append({
                    'step_sort': step.get('step_sort'),
                    'operation': operation,
                    'target_name': step.get('target_name') or '',
                    'reason': 'element_not_bound_to_map',
                })
            else:
                bound_element_steps += 1
                if operation in {'check', 'uncheck'} and not _element_is_checkable(element):
                    unresolved_steps.append({
                        'step_sort': step.get('step_sort'),
                        'operation': operation,
                        'target_name': step.get('target_name') or '',
                        'reason': 'operation_not_supported_by_observed_element',
                    })

        if step.get('requires_confirmation') or _is_risky_step(step, safety_policy):
            risky_steps.append({
                'step_sort': step.get('step_sort'),
                'operation': operation,
                'target_name': step.get('target_name') or '',
                'description': step.get('description') or '',
            })

        try:
            confidence = float(step.get('confidence') or 0)
        except (TypeError, ValueError):
            confidence = 0
        if confidence and confidence < 0.6:
            low_confidence_steps.append({
                'step_sort': step.get('step_sort'),
                'operation': operation,
                'target_name': step.get('target_name') or '',
                'confidence': confidence,
            })

    executable_steps = [
        step for step in steps
        if isinstance(step, dict)
        and step.get('operation') in ALLOWED_OPERATIONS
        and not (_is_unresolved_gap_step(step) and step.get('operation') not in {'goto', 'wait'})
        and not step.get('requires_confirmation')
    ]
    semantic_issues = [
        *_semantic_coverage_issues(task, steps, element_map, safety_policy),
        *_unobserved_state_transition_issues(steps, element_map),
        *runtime_assertion_issues,
    ]
    return {
        'status': 'success' if not unresolved_steps and not semantic_issues else 'failed',
        'total_steps': len(steps),
        'executable_steps': len(executable_steps),
        'bound_element_steps': bound_element_steps,
        'unresolved_step_count': len(unresolved_steps),
        'risky_step_count': len(risky_steps),
        'low_confidence_step_count': len(low_confidence_steps),
        'semantic_issue_count': len(semantic_issues),
        'unresolved_steps': unresolved_steps[:20],
        'risky_steps': risky_steps[:20],
        'low_confidence_steps': low_confidence_steps[:20],
        'semantic_issues': semantic_issues[:20],
        'notes': ['已静态校验 LLM 步骤与元素地图绑定关系、安全边界、语义覆盖和可落库操作类型'],
    }


FORM_FIELD_CONTAINER_SELECTOR = '.el-form-item, .arco-form-item, .ant-form-item, .form-item'
FORM_FIELD_CONTROL_SELECTOR = (
    'input, textarea, select, [role="combobox"], [contenteditable="true"], '
    'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"]'
)
FORM_FIELD_OPERATIONS = {'fill', 'select_option', 'check', 'uncheck'}
TABLE_ROW_SELECTOR = 'tbody tr, .el-table__row, .ant-table-row, .arco-table-tr, [role="row"]'


def _script_form_field_expression(label: str) -> str:
    return (
        f"page.locator({json.dumps(FORM_FIELD_CONTAINER_SELECTOR, ensure_ascii=False)})"
        f".filter(has_text={json.dumps(label, ensure_ascii=False)})"
        f".locator({json.dumps(FORM_FIELD_CONTROL_SELECTOR, ensure_ascii=False)})"
    )


def _ts_form_field_expression(label: str) -> str:
    return (
        f"page.locator({json.dumps(FORM_FIELD_CONTAINER_SELECTOR, ensure_ascii=False)})"
        f".filter({{ hasText: {json.dumps(label, ensure_ascii=False)} }})"
        f".locator({json.dumps(FORM_FIELD_CONTROL_SELECTOR, ensure_ascii=False)})"
    )


def _ts_row_scoped_locator_expression(locator: dict[str, Any], row_scope: dict[str, Any], operation: str = '') -> str | None:
    row_text = str(row_scope.get('row_text') or '').strip()
    if not row_text:
        return None
    base = _ts_locator_expression(locator, operation)
    row_prefix = (
        f"page.locator({json.dumps(TABLE_ROW_SELECTOR, ensure_ascii=False)})"
        f".filter({{ hasText: {json.dumps(row_text, ensure_ascii=False)} }})"
    )
    if base.startswith('page.'):
        return row_prefix + base[len('page'):]
    return None


def _script_locator_expression(locator: dict[str, Any], operation: str = '') -> str:
    locator_type = locator.get('type')
    value = str(locator.get('value') or '')
    name = str(locator.get('name') or '')
    if locator_type == 'mcp_ref':
        return f"page.get_by_text({json.dumps(name or value, ensure_ascii=False)}, exact=True)"
    if locator_type == 'test_id':
        return f"page.get_by_test_id({json.dumps(value, ensure_ascii=False)})"
    if locator_type == 'role':
        role = value
        if name:
            return f"page.get_by_role({json.dumps(role, ensure_ascii=False)}, name={json.dumps(name, ensure_ascii=False)})"
        return f"page.get_by_role({json.dumps(role, ensure_ascii=False)})"
    if locator_type == 'label':
        if operation in FORM_FIELD_OPERATIONS:
            return _script_form_field_expression(value)
        return f"page.get_by_label({json.dumps(value, ensure_ascii=False)})"
    if locator_type == 'placeholder':
        return f"page.get_by_placeholder({json.dumps(value, ensure_ascii=False)})"
    if locator_type == 'text':
        return f"page.get_by_text({json.dumps(value, ensure_ascii=False)}, exact=True)"
    if locator_type == 'id':
        return f"page.locator({json.dumps('#' + value, ensure_ascii=False)})"
    if locator_type == 'name':
        selector = f'[name="{value}"]'
        return f"page.locator({json.dumps(selector, ensure_ascii=False)})"
    if locator_type == 'xpath':
        return f"page.locator({json.dumps('xpath=' + value, ensure_ascii=False)})"
    return f"page.locator({json.dumps(value, ensure_ascii=False)})"


def _ts_locator_expression(locator: dict[str, Any], operation: str = '') -> str:
    locator_type = locator.get('type')
    value = str(locator.get('value') or '')
    name = str(locator.get('name') or '')
    if locator_type == 'mcp_ref':
        return f"page.getByText({json.dumps(name or value, ensure_ascii=False)}, {{ exact: true }})"
    if locator_type == 'test_id':
        return f"page.getByTestId({json.dumps(value, ensure_ascii=False)})"
    if locator_type == 'role':
        role = value
        if name:
            return (
                f"page.getByRole({json.dumps(role, ensure_ascii=False)}, "
                f"{{ name: {json.dumps(name, ensure_ascii=False)}, exact: true }})"
            )
        return f"page.getByRole({json.dumps(role, ensure_ascii=False)})"
    if locator_type == 'label':
        if operation in FORM_FIELD_OPERATIONS:
            return _ts_form_field_expression(value)
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


def _build_script_from_steps(task, steps: list[dict[str, Any]], element_map: dict[str, Any]) -> str:
    base_url = element_map.get('base_url') or task.target_url or ''
    lines = [
        "import asyncio",
        "import time",
        "",
        "from playwright.async_api import expect",
        "",
        "",
        "async def first_visible(locator, timeout=5000, prefer='first'):",
        "    deadline = time.monotonic() + max(timeout, 0) / 1000",
        "    last_count = 0",
        "    while True:",
        "        last_count = await locator.count()",
        "        indexes = list(range(min(last_count, 80)))",
        "        if prefer == 'last':",
        "            indexes.reverse()",
        "        for index in indexes:",
        "            candidate = locator.nth(index)",
        "            try:",
        "                if await candidate.is_visible(timeout=200):",
        "                    return candidate",
        "            except Exception:",
        "                continue",
        "        if time.monotonic() >= deadline:",
        "            break",
        "        await asyncio.sleep(0.15)",
        "    raise TimeoutError(f'Timeout waiting for visible locator match: matched={last_count}')",
        "",
        "",
        "async def test_generated_flow(page):",
    ]
    if not any(step.get('operation') == 'goto' for step in steps):
        lines.append(f"    await page.goto({json.dumps(base_url, ensure_ascii=False)}, wait_until='networkidle')")

    for step in steps:
        operation = step.get('operation')
        value = str(step.get('value') or '')
        description = str(step.get('description') or operation or '').replace('\n', ' ')[:120]
        if description:
            lines.append(f"    # {description}")
        if operation == 'goto':
            lines.append(f"    await page.goto({json.dumps(value or base_url, ensure_ascii=False)}, wait_until='networkidle')")
            continue
        if operation == 'wait':
            try:
                wait_ms = int(float(value or 1) * 1000)
            except (TypeError, ValueError):
                wait_ms = 1000
            lines.append(f"    await page.wait_for_timeout({max(0, min(wait_ms, 30000))})")
            continue

        locator_hint = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
        if not locator_hint.get('type'):
            lines.append("    # 未匹配到可靠 locator，跳过该步骤")
            continue
        locator_base = _script_locator_expression(locator_hint, operation)
        if step.get('context_type') == 'frame':
            frame_name = str(step.get('frame_name') or '')
            frame_url = str(step.get('frame_url') or '')
            lines.append(
                f"    frame = page.frame(name={json.dumps(frame_name, ensure_ascii=False)}) "
                f"or page.frame(url={json.dumps(frame_url, ensure_ascii=False)})"
            )
            lines.append("    assert frame is not None")
            locator_base = locator_base.replace('page.', 'frame.', 1)
        locator_expr = f"step_locator_{len(lines)}"
        lines.append(f"    {locator_expr} = await first_visible({locator_base})")
        if operation == 'fill':
            lines.append(f"    await {locator_expr}.fill({json.dumps(value, ensure_ascii=False)})")
        elif operation == 'click':
            lines.append(f"    await {locator_expr}.click()")
        elif operation == 'select_option':
            lines.append("    try:")
            lines.append(f"        await {locator_expr}.select_option({json.dumps(value, ensure_ascii=False)})")
            lines.append("    except Exception:")
            lines.append(f"        await {locator_expr}.click()")
            lines.append("        await page.keyboard.press('ArrowDown')")
            lines.append("        await page.keyboard.press('Enter')")
        elif operation == 'check':
            lines.append(f"    await {locator_expr}.check()")
        elif operation == 'uncheck':
            lines.append(f"    await {locator_expr}.uncheck()")
        elif operation == 'assert_visible':
            lines.append(f"    await expect({locator_expr}).to_be_visible(timeout=5000)")
        elif operation == 'assert_text':
            lines.append(f"    await expect({locator_expr}).to_have_text({json.dumps(value, ensure_ascii=False)}, timeout=5000)")
        elif operation == 'assert_contain_text':
            lines.append(f"    await expect({locator_expr}).to_contain_text({json.dumps(value, ensure_ascii=False)}, timeout=5000)")
        elif operation == 'assert_value':
            lines.append(f"    await expect({locator_expr}).to_have_value({json.dumps(value, ensure_ascii=False)}, timeout=5000)")

    return '\n'.join(lines) + '\n'


def _ts_step_value(step: dict[str, Any]) -> str:
    key = step.get('test_data_key')
    if key:
        return f"String(testData[{json.dumps(str(key), ensure_ascii=False)}] ?? {json.dumps(str(step.get('value') or ''), ensure_ascii=False)})"
    return json.dumps(str(step.get('value') or ''), ensure_ascii=False)


def _build_typescript_runtime_helper() -> str:
    return '\n'.join([
        "import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';",
        "import { execFile } from 'node:child_process';",
        "import { mkdir } from 'node:fs/promises';",
        "import { tmpdir } from 'node:os';",
        "import { join } from 'node:path';",
        "import { promisify } from 'node:util';",
        "",
        "export type WhartCleanupAction = { type: string; url?: string; data?: unknown; description?: string };",
        "export type WhartRuntime = { page: Page; request: APIRequestContext };",
        "export type WhartLocatorFactory = (page: Page) => Locator;",
        "export type WhartLocatorInput = WhartLocatorFactory | WhartLocatorFactory[];",
        "export type WhartRuntimeInput = { resolver: 'image_ocr'; targetName?: string; maxAttempts?: number };",
        "export type WhartStateAssertion = { target?: string; expected?: string; previousActionId?: string; previous_action_id?: string; pageKey?: string; page_key?: string; actionId?: string };",
        "const execFileAsync = promisify(execFile);",
        "",
        "function escapeRegExp(value: string) {",
        "  return value.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');",
        "}",
        "",
        "async function firstVisible(locator: Locator | Locator[], timeout = 15000) {",
        "  const locators = Array.isArray(locator) ? locator : [locator];",
        "  const deadline = Date.now() + Math.max(timeout, 0);",
        "  let lastCount = 0;",
        "  while (true) {",
        "    lastCount = 0;",
        "    for (const current of locators) {",
        "      const currentCount = await current.count();",
        "      lastCount += currentCount;",
        "      const indexes = Array.from({ length: Math.min(currentCount, 80) }, (_, index) => index);",
        "      for (const index of indexes) {",
        "        const candidate = current.nth(index);",
        "        try {",
        "          await candidate.waitFor({ state: 'visible', timeout: 200 });",
        "          return candidate;",
        "        } catch {",
        "          continue;",
        "        }",
        "      }",
        "    }",
        "    if (Date.now() >= deadline) break;",
        "    await new Promise((resolve) => setTimeout(resolve, 150));",
        "  }",
        "  throw new Error(`Timeout waiting for visible locator match: matched=${lastCount}`);",
        "}",
        "",
        "async function runApiAction(request: APIRequestContext, action: WhartCleanupAction) {",
        "  if (!action.url) return;",
        "  if (action.type === 'api_delete') await request.delete(action.url);",
        "  else if (action.type === 'api_post') await request.post(action.url, { data: action.data });",
        "  else if (action.type === 'api_patch') await request.patch(action.url, { data: action.data });",
        "}",
        "",
        "export async function runSeed(runtime: WhartRuntime, actions: WhartCleanupAction[]) {",
        "  for (const action of actions) {",
        "    try {",
        "      if (action.type?.startsWith('api_')) await runApiAction(runtime.request, action);",
        "    } catch (error) {",
        "      console.warn('WHart seed action failed', action.description || action.type, error);",
        "    }",
        "  }",
        "}",
        "",
        "export async function runCleanup(runtime: WhartRuntime, actions: WhartCleanupAction[]) {",
        "  for (const action of [...actions].reverse()) {",
        "    try {",
        "      if (action.type?.startsWith('api_')) await runApiAction(runtime.request, action);",
        "      else if (action.type === 'goto' && action.url) await runtime.page.goto(action.url, { waitUntil: 'networkidle' });",
        "    } catch (error) {",
        "      console.warn('WHart cleanup action failed', action.description || action.type, error);",
        "    }",
        "  }",
        "}",
        "",
        "export class WhartGeneratedPage {",
        "  private recentFeedback: string[] = [];",
        "  private lastStateChange: { before: unknown; after: unknown } | undefined;",
        "",
        "  constructor(private readonly page: Page) {}",
        "",
        "  private async captureUiFeedback() {",
        "    this.recentFeedback = [];",
        "    await this.page.waitForTimeout(150);",
        "    const feedback = this.page.locator(",
        "      '[role=\"alert\"], .el-message, .el-notification, [aria-live=\"assertive\"], [aria-live=\"polite\"]',",
        "    );",
        "    const count = Math.min(await feedback.count(), 20);",
        "    for (let index = 0; index < count; index += 1) {",
        "      const candidate = feedback.nth(index);",
        "      if (!(await candidate.isVisible().catch(() => false))) continue;",
        "      const value = (await candidate.innerText().catch(() => '')).trim();",
        "      if (value && !this.recentFeedback.includes(value)) this.recentFeedback.push(value);",
        "    }",
        "  }",
        "",
        "  private withUiFeedback(error: unknown) {",
        "    const message = error instanceof Error ? error.message : String(error);",
        "    if (!this.recentFeedback.length) return error instanceof Error ? error : new Error(message);",
        "    return new Error(`${message}; recent UI feedback: ${this.recentFeedback.join(' | ')}`);",
        "  }",
        "",
        "  private async captureStateEvidence() {",
        "    await this.page.waitForTimeout(120);",
        "    return await this.page.locator('body').evaluate((body) => {",
        "      const normalize = (value: unknown) => String(value || '').replace(/\\s+/g, ' ').trim();",
        "      const selectedLike = (value: string) => /(^|[\\s_-])(checked|selected|active|on|true|is-checked|is-selected)([\\s_-]|$)/i.test(value);",
        "      const negativeLike = (value: string) => /(^|[\\s_-])(unchecked|unselected|inactive|off|false|disabled)([\\s_-]|$)/i.test(value);",
        "      const nodes = Array.from(body.querySelectorAll('input, [role], [aria-checked], [aria-selected], [data-state], [class]')).slice(0, 1200);",
        "      const items = nodes.map((node) => {",
        "        const element = node as HTMLElement & { checked?: boolean; value?: string };",
        "        const style = window.getComputedStyle(element);",
        "        const rect = element.getBoundingClientRect();",
        "        const visible = style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;",
        "        if (!visible) return undefined;",
        "        const ariaChecked = element.getAttribute('aria-checked') || '';",
        "        const ariaSelected = element.getAttribute('aria-selected') || '';",
        "        const dataState = element.getAttribute('data-state') || '';",
        "        const className = typeof element.className === 'string' ? element.className : '';",
        "        const checked = typeof element.checked === 'boolean' ? element.checked : undefined;",
        "        const text = normalize(element.innerText || element.textContent || '').slice(0, 120);",
        "        const stateText = normalize([ariaChecked, ariaSelected, dataState, className, checked === undefined ? '' : String(checked)].join(' '));",
        "        const positive = checked === true || selectedLike(stateText);",
        "        const negative = checked === false || negativeLike(stateText);",
        "        return {",
        "          tag: element.tagName.toLowerCase(),",
        "          role: element.getAttribute('role') || '',",
        "          text, ariaChecked, ariaSelected, dataState, className: className.slice(0, 160),",
        "          checked, positive, negative, stateText,",
        "        };",
        "      }).filter(Boolean) as Array<Record<string, unknown>>;",
        "      const stateful = items.filter((item) => Boolean(item.stateText) || typeof item.checked === 'boolean');",
        "      const positiveCount = stateful.filter((item) => item.positive).length;",
        "      const negativeCount = stateful.filter((item) => item.negative).length;",
        "      const summary = {",
        "        url: location.href,",
        "        title: document.title,",
        "        visibleText: normalize((body as HTMLElement).innerText || '').slice(0, 4000),",
        "        statefulCount: stateful.length,",
        "        positiveCount,",
        "        negativeCount,",
        "        items: stateful.slice(0, 160),",
        "      };",
        "      return { ...summary, signature: JSON.stringify(summary) };",
        "    });",
        "  }",
        "",
        "  async assertRuntimeStateChange(assertion: WhartStateAssertion) {",
        "    if (!this.lastStateChange) throw new Error('运行时状态断言缺少前序动作的 before/after 证据');",
        "    const before = this.lastStateChange.before as Record<string, unknown>;",
        "    const after = this.lastStateChange.after as Record<string, unknown>;",
        "    const expected = String(assertion.expected || '').toLowerCase();",
        "    const wantsAll = /all|every|全部|所有/.test(expected);",
        "    const wantsPositive = /(checked|selected|active|enabled|on|true|选中|勾选|启用|开启)/.test(expected) && !/(uncheck|unchecked|unselected|off|false|未选中|未勾选|取消|关闭)/.test(expected);",
        "    const wantsNegative = /(uncheck|unchecked|unselected|disabled|inactive|off|false|未选中|未勾选|取消|关闭)/.test(expected);",
        "    const beforePositive = Number(before.positiveCount || 0);",
        "    const afterPositive = Number(after.positiveCount || 0);",
        "    const afterStateful = Number(after.statefulCount || 0);",
        "    const changed = String(before.signature || '') !== String(after.signature || '');",
        "    const expectedText = String(assertion.expected || '').replace(/\\s+/g, ' ').trim().toLowerCase();",
        "    const afterVisibleText = String(after.visibleText || '').replace(/\\s+/g, ' ').trim().toLowerCase();",
        "    if (wantsPositive && afterStateful > 0) {",
        "      if (wantsAll) expect(afterPositive, '运行时状态证据未显示所有可状态项为正向状态').toBe(afterStateful);",
        "      else expect(afterPositive, '运行时状态证据未显示正向状态增加或存在').toBeGreaterThan(0);",
        "      expect(afterPositive, '运行时状态证据未相对前序动作增加').toBeGreaterThanOrEqual(beforePositive);",
        "      return;",
        "    }",
        "    if (wantsNegative && afterStateful > 0) {",
        "      if (wantsAll) expect(afterPositive, '运行时状态证据未显示所有可状态项为反向状态').toBe(0);",
        "      else expect(afterPositive, '运行时状态证据未显示正向状态减少').toBeLessThanOrEqual(beforePositive);",
        "      return;",
        "    }",
        "    if (expectedText) {",
        "      expect(afterVisibleText, '运行时状态证据未包含期望文本').toContain(expectedText);",
        "      expect(changed, '运行时状态证据没有观察到动作前后 DOM/ARIA/class/data-state 证据变化').toBeTruthy();",
        "      return;",
        "    }",
        "    expect(changed, '运行时状态断言没有观察到动作前后 DOM/ARIA/class/data-state 证据变化').toBeTruthy();",
        "  }",
        "",
        "  async goto(url: string) {",
        "    await this.page.goto(url, { waitUntil: 'networkidle' });",
        "    await expect(this.page.locator('body')).toBeVisible({ timeout: 15000 });",
        "  }",
        "",
        "  async element(locatorInput: WhartLocatorInput) {",
        "    const factories = Array.isArray(locatorInput) ? locatorInput : [locatorInput];",
        "    try {",
        "      return await firstVisible(factories.map((factory) => factory(this.page)));",
        "    } catch (error) {",
        "      throw this.withUiFeedback(error);",
        "    }",
        "  }",
        "",
        "  async fill(locatorFactory: WhartLocatorInput, value: string) {",
        "    await (await this.element(locatorFactory)).fill(value);",
        "  }",
        "",
        "  private async nearestCaptchaVisual(input: Locator) {",
        "    const inputBox = await input.boundingBox();",
        "    if (!inputBox) throw new Error('运行时输入框没有可用坐标，无法定位验证码图像');",
        "    const visuals = this.page.locator('img, canvas, svg, [style*=\"background-image\"]');",
        "    const count = Math.min(await visuals.count(), 120);",
        "    let best: { locator: Locator; score: number } | undefined;",
        "    for (let index = 0; index < count; index += 1) {",
        "      const candidate = visuals.nth(index);",
        "      if (!(await candidate.isVisible().catch(() => false))) continue;",
        "      const box = await candidate.boundingBox().catch(() => null);",
        "      if (!box || box.width < 24 || box.height < 12 || box.width > 360 || box.height > 180) continue;",
        "      const inputCenterY = inputBox.y + inputBox.height / 2;",
        "      const candidateCenterY = box.y + box.height / 2;",
        "      const verticalDistance = Math.abs(inputCenterY - candidateCenterY);",
        "      if (verticalDistance > Math.max(90, inputBox.height * 2.5)) continue;",
        "      const horizontalGap = box.x >= inputBox.x",
        "        ? Math.abs(box.x - (inputBox.x + inputBox.width))",
        "        : Math.abs(inputBox.x - (box.x + box.width)) + 240;",
        "      const score = verticalDistance * 4 + horizontalGap + Math.abs(box.height - inputBox.height);",
        "      if (!best || score < best.score) best = { locator: candidate, score };",
        "    }",
        "    if (!best) throw new Error('未找到与运行时输入框相邻的验证码图片或画布');",
        "    return best.locator;",
        "  }",
        "",
        "  async fillRuntimeInput(locatorFactory: WhartLocatorInput, inputConfig: WhartRuntimeInput) {",
        "    if (inputConfig.resolver !== 'image_ocr') throw new Error(`不支持的运行时解析器: ${inputConfig.resolver}`);",
        "    const python = process.env.WHART_OCR_PYTHON;",
        "    const resolverScript = process.env.WHART_OCR_SCRIPT;",
        "    if (!python || !resolverScript) throw new Error('执行器未配置 WHART_OCR_PYTHON/WHART_OCR_SCRIPT');",
        "    const artifactDir = process.env.WHART_OCR_ARTIFACT_DIR || join(tmpdir(), 'whart-ocr');",
        "    await mkdir(artifactDir, { recursive: true });",
        "    const target = await this.element(locatorFactory);",
        "    const maxAttempts = Math.max(1, Math.min(inputConfig.maxAttempts || 3, 5));",
        "    let lastError = '';",
        "    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {",
        "      const visual = await this.nearestCaptchaVisual(target);",
        "      const imagePath = join(artifactDir, `captcha-${process.pid}-${Date.now()}-${attempt}.png`);",
        "      await visual.screenshot({ path: imagePath });",
        "      try {",
        "        const { stdout, stderr } = await execFileAsync(",
        "          python, [resolverScript, '--resolver', 'image_ocr', '--image', imagePath],",
        "          { timeout: 30000, maxBuffer: 1024 * 1024 },",
        "        );",
        "        const lines = stdout.trim().split(/\\r?\\n/).filter(Boolean);",
        "        const payload = JSON.parse(lines[lines.length - 1] || '{}');",
        "        const value = String(payload.value || '').replace(/\\s+/g, '');",
        "        if (payload.status !== 'success' || !value) throw new Error(payload.error || stderr || 'OCR 未返回验证码');",
        "        await target.fill(value);",
        "        console.log('WHART_RUNTIME_INPUT', JSON.stringify({",
        "          resolver: 'image_ocr', targetName: inputConfig.targetName || '',",
        "          attempt, imagePath, valueLength: value.length, status: 'success',",
        "        }));",
        "        return value;",
        "      } catch (error) {",
        "        lastError = error instanceof Error ? error.message : String(error);",
        "        console.warn('WHART_RUNTIME_INPUT', JSON.stringify({",
        "          resolver: 'image_ocr', targetName: inputConfig.targetName || '',",
        "          attempt, imagePath, status: 'failed', error: lastError,",
        "        }));",
        "        if (attempt < maxAttempts) await visual.click({ force: true }).catch(() => undefined);",
        "      }",
        "    }",
        "    throw new Error(`运行时 OCR 输入解析失败: ${lastError}`);",
        "  }",
        "",
        "  async click(locatorFactory: WhartLocatorInput) {",
        "    let before: unknown | undefined;",
        "    let after: unknown | undefined;",
        "    before = await this.captureStateEvidence().catch(() => undefined);",
        "    await (await this.element(locatorFactory)).click();",
        "    await this.captureUiFeedback();",
        "    await this.page.waitForLoadState('networkidle', { timeout: 1500 }).catch(() => undefined);",
        "    after = await this.captureStateEvidence().catch(() => undefined);",
        "    if (before && after) this.lastStateChange = { before, after };",
        "  }",
        "",
        "  async revealRuntimeText(locatorInput: WhartLocatorInput, expectedText: string) {",
        "    const alreadyVisible = await this.page.getByText(expectedText, { exact: false }).first().isVisible({ timeout: 300 }).catch(() => false);",
        "    if (alreadyVisible) return;",
        "    const factories = Array.isArray(locatorInput) ? locatorInput : [locatorInput];",
        "    let lastError = '';",
        "    for (const factory of factories) {",
        "      try {",
        "        const target = await firstVisible(factory(this.page), 1500);",
        "        await target.click({ timeout: 5000 });",
        "        await this.captureUiFeedback();",
        "        const revealed = await this.page.getByText(expectedText, { exact: false }).first().isVisible({ timeout: 1500 }).catch(() => false);",
        "        if (revealed) return;",
        "      } catch (error) {",
        "        lastError = error instanceof Error ? error.message : String(error);",
        "      }",
        "    }",
        "    if (lastError) console.warn('WHART_RUNTIME_TEXT_REVEAL_UNRESOLVED', lastError);",
        "  }",
        "",
        "  async selectOption(locatorFactory: WhartLocatorInput, value: string) {",
        "    const locator = await this.element(locatorFactory);",
        "    try {",
        "      await locator.selectOption(value);",
        "    } catch {",
        "      try {",
        "        await locator.press('ArrowDown', { timeout: 5000 });",
        "        const namedOption = this.page.getByRole('option').filter({",
        "          hasText: new RegExp(`^\\\\s*${escapeRegExp(value)}\\\\s*$`, 'i'),",
        "        });",
        "        try {",
        "          await (await firstVisible(namedOption, 2000)).click({ timeout: 5000 });",
        "        } catch {",
        "          await locator.press('Enter', { timeout: 5000 });",
        "        }",
        "      } catch {",
        "        await locator.locator('..').click({ timeout: 5000 });",
        "        await this.page.keyboard.press('ArrowDown');",
        "        await this.page.keyboard.press('Enter');",
        "      }",
        "    }",
        "  }",
        "",
        "  async check(locatorFactory: WhartLocatorInput) {",
        "    const locator = await this.element(locatorFactory);",
        "    try {",
        "      await locator.check({ timeout: 5000 });",
        "    } catch {",
        "      await locator.check({ force: true, timeout: 5000 });",
        "    }",
        "  }",
        "",
        "  async uncheck(locatorFactory: WhartLocatorInput) {",
        "    await (await this.element(locatorFactory)).uncheck();",
        "  }",
        "",
        "  async assertVisible(locatorFactory: WhartLocatorInput) {",
        "    await expect(await this.element(locatorFactory)).toBeVisible({ timeout: 5000 });",
        "  }",
        "",
        "  async assertText(locatorFactory: WhartLocatorInput, value: string) {",
        "    await expect(await this.element(locatorFactory)).toHaveText(value, { timeout: 5000 });",
        "  }",
        "",
        "  async assertContainText(locatorFactory: WhartLocatorInput, value: string) {",
        "    await expect(await this.element(locatorFactory)).toContainText(value, { timeout: 5000 });",
        "  }",
        "",
        "  async assertValue(locatorFactory: WhartLocatorInput, value: string) {",
        "    await expect(await this.element(locatorFactory)).toHaveValue(value, { timeout: 5000 });",
        "  }",
        "}",
        "",
    ])


def _build_typescript_file_bundle(spec: str) -> dict[str, str]:
    return {
        'generated.spec.ts': spec,
        'whart-generated-page.ts': _build_typescript_runtime_helper(),
    }


def _build_typescript_spec_from_steps(
    task,
    steps: list[dict[str, Any]],
    element_map: dict[str, Any],
    data_lifecycle: dict[str, Any] | None = None,
) -> str:
    base_url = element_map.get('base_url') or task.target_url or ''
    title = (task.target_module or task.name or 'AI generated UI flow').replace('\n', ' ')[:120]
    data_lifecycle = data_lifecycle if isinstance(data_lifecycle, dict) else {}
    test_data = data_lifecycle.get('test_data') if isinstance(data_lifecycle.get('test_data'), dict) else {}
    seed_actions = data_lifecycle.get('seed_actions') if isinstance(data_lifecycle.get('seed_actions'), list) else []
    cleanup_actions = data_lifecycle.get('cleanup_actions') if isinstance(data_lifecycle.get('cleanup_actions'), list) else []
    manual_cleanup_notes = data_lifecycle.get('manual_cleanup_notes') if isinstance(data_lifecycle.get('manual_cleanup_notes'), list) else []
    lines = [
        "import { test, expect } from '@playwright/test';",
        "import { WhartGeneratedPage, runCleanup, runSeed, type WhartCleanupAction } from './whart-generated-page';",
        "",
        f"test.describe({json.dumps(title, ensure_ascii=False)}, () => {{",
        f"  test({json.dumps(task.name or 'generated flow', ensure_ascii=False)}, async ({{ page, request }}) => {{",
        f"    const testData: Record<string, unknown> = {json.dumps(test_data, ensure_ascii=False, indent=6)};",
        f"    const seedActions: WhartCleanupAction[] = {json.dumps(seed_actions, ensure_ascii=False, indent=6)};",
        f"    const cleanupQueue: WhartCleanupAction[] = {json.dumps(cleanup_actions, ensure_ascii=False, indent=6)};",
        f"    const manualCleanupNotes: string[] = {json.dumps([str(item) for item in manual_cleanup_notes], ensure_ascii=False, indent=6)};",
        "    const app = new WhartGeneratedPage(page);",
        "    await runSeed({ page, request }, seedActions);",
        "    try {",
    ]
    if not any(step.get('operation') == 'goto' for step in steps):
        lines.append(f"      await app.goto({json.dumps(base_url, ensure_ascii=False)});")

    for step in steps:
        operation = step.get('operation')
        value = str(step.get('value') or '')
        value_expr = _ts_step_value(step)
        description = str(step.get('description') or operation or '').replace('\n', ' ')[:120]
        if description:
            lines.append(f"      // {description}")
        if operation == 'goto':
            lines.append(f"      await app.goto({json.dumps(value or base_url, ensure_ascii=False)});")
            continue
        if operation == 'wait':
            try:
                wait_ms = int(float(value or 1) * 1000)
            except (TypeError, ValueError):
                wait_ms = 1000
            lines.append(f"      await page.waitForTimeout({max(0, min(wait_ms, 30000))});")
            continue

        if _is_runtime_state_assertion(step):
            state_assertion = (
                dict(step.get('state_assertion'))
                if isinstance(step.get('state_assertion'), dict)
                else {}
            )
            if not state_assertion.get('expected'):
                state_assertion['expected'] = step.get('expected') or step.get('value') or ''
            if not state_assertion.get('target'):
                state_assertion['target'] = step.get('target_name') or ''
            state_assertion['pageKey'] = step.get('page_key') or state_assertion.get('page_key') or ''
            if step.get('action_id'):
                state_assertion['actionId'] = step.get('action_id')
            marker = {
                'step_sort': step.get('step_sort'),
                'page_key': step.get('page_key') or '',
                'operation': operation,
                'target_name': step.get('target_name') or '',
                'binding_mode': 'runtime_state',
                'state_assertion': state_assertion,
            }
            lines.append(f"      // WHART_AI_STEP {json.dumps(marker, ensure_ascii=False, sort_keys=True)}")
            lines.append(f"      await app.assertRuntimeStateChange({json.dumps(state_assertion, ensure_ascii=False)});")
            continue

        locator_hint = step.get('locator_hint') if isinstance(step.get('locator_hint'), dict) else {}
        if not locator_hint.get('type'):
            lines.append("      // 未匹配到可靠 locator，跳过该步骤")
            continue
        marker = {
            'step_sort': step.get('step_sort'),
            'page_key': step.get('page_key') or '',
            'element_key': step.get('element_key') or '',
            'operation': operation,
            'target_name': step.get('target_name') or '',
            'locator_hint': locator_hint,
            'row_scope': step.get('row_scope') if isinstance(step.get('row_scope'), dict) else {},
        }
        lines.append(f"      // WHART_AI_STEP {json.dumps(marker, ensure_ascii=False, sort_keys=True)}")
        candidate_hints = [
            candidate for candidate in (step.get('locator_candidates') or [])
            if isinstance(candidate, dict) and candidate.get('type') and candidate.get('value')
        ]
        if not candidate_hints:
            candidate_hints = [locator_hint]
        locator_bases = [_ts_locator_expression(candidate, operation) for candidate in candidate_hints]
        row_scope = step.get('row_scope') if isinstance(step.get('row_scope'), dict) else {}
        if row_scope:
            row_scoped_bases = [
                scoped
                for scoped in [
                    _ts_row_scoped_locator_expression(candidate, row_scope, operation)
                    for candidate in candidate_hints
                ]
                if scoped
            ]
            locator_bases = [*row_scoped_bases, *locator_bases]
        if step.get('context_type') == 'frame':
            frame_name = str(step.get('frame_name') or '')
            frame_url = str(step.get('frame_url') or '')
            frame_var = f"frame{step.get('step_sort', 0)}"
            lines.append(
                f"      const {frame_var} = page.frame({{ name: {json.dumps(frame_name, ensure_ascii=False)} }}) "
                f"?? page.frame({{ url: {json.dumps(frame_url, ensure_ascii=False)} }});"
            )
            lines.append(f"      if (!{frame_var}) throw new Error({json.dumps('未找到目标 iframe 上下文', ensure_ascii=False)});")
            locator_bases = [
                locator_base.replace('page.', f'{frame_var}!.', 1)
                for locator_base in locator_bases
            ]
        locator_input = (
            f"page => {locator_bases[0]}"
            if len(locator_bases) == 1
            else '[' + ', '.join(f"page => {locator_base}" for locator_base in locator_bases) + ']'
        )
        runtime_text_assertion = bool(
            operation in {'assert_visible', 'assert_text', 'assert_contain_text'}
            and step.get('binding_mode') == 'runtime_text'
            and str(step.get('value') or '').strip()
        )
        if runtime_text_assertion:
            reveal_candidates = _runtime_assertion_reveal_candidates(step, steps, element_map)
            reveal_locator_bases: list[str] = []
            seen_reveal_locators: set[tuple[str, str, str]] = set()
            for reveal_candidate in reveal_candidates:
                for reveal_locator in reveal_candidate.get('locator_candidates') or []:
                    if not isinstance(reveal_locator, dict):
                        continue
                    compact_locator = _compact_locator(reveal_locator)
                    identity = _locator_identity(compact_locator)
                    if not identity[0] or not identity[1] or identity in seen_reveal_locators:
                        continue
                    seen_reveal_locators.add(identity)
                    reveal_locator_bases.append(_ts_locator_expression(compact_locator, 'click'))
                    if len(reveal_locator_bases) >= 8:
                        break
                if len(reveal_locator_bases) >= 8:
                    break
            if reveal_locator_bases:
                reveal_locator_input = (
                    f"page => {reveal_locator_bases[0]}"
                    if len(reveal_locator_bases) == 1
                    else '[' + ', '.join(f"page => {locator_base}" for locator_base in reveal_locator_bases) + ']'
                )
                lines.append(f"      await app.revealRuntimeText({reveal_locator_input}, {value_expr});")
            lines.append(f"      await app.assertContainText(page => page.locator('body'), {value_expr});")
            continue
        if operation == 'fill' and step.get('binding_mode') == 'runtime_input':
            runtime_input = {
                'resolver': step.get('runtime_resolver') or 'image_ocr',
                'targetName': step.get('target_name') or '',
                'maxAttempts': 3,
            }
            lines.append(
                f"      await app.fillRuntimeInput({locator_input}, "
                f"{json.dumps(runtime_input, ensure_ascii=False)});"
            )
        elif operation == 'fill':
            lines.append(f"      await app.fill({locator_input}, {value_expr});")
        elif operation == 'click':
            lines.append(f"      await app.click({locator_input});")
        elif operation == 'select_option':
            lines.append(f"      await app.selectOption({locator_input}, {value_expr});")
        elif operation == 'check':
            lines.append(f"      await app.check({locator_input});")
        elif operation == 'uncheck':
            lines.append(f"      await app.uncheck({locator_input});")
        elif operation == 'assert_visible':
            lines.append(f"      await app.assertVisible({locator_input});")
        elif operation == 'assert_text':
            lines.append(f"      await app.assertText({locator_input}, {value_expr});")
        elif operation == 'assert_contain_text':
            lines.append(f"      await app.assertContainText({locator_input}, {value_expr});")
        elif operation == 'assert_value':
            lines.append(f"      await app.assertValue({locator_input}, {value_expr});")

    lines.extend([
        "      if (manualCleanupNotes.length) console.warn('WHart manual cleanup required', manualCleanupNotes);",
        "    } finally {",
        "      await runCleanup({ page, request }, cleanupQueue);",
        "    }",
        "  });",
        "});",
        "",
    ])
    return '\n'.join(lines)


def _trim_compact_map_elements(compact_map: dict[str, Any], max_elements: int) -> dict[str, Any]:
    source_pages = [page for page in (compact_map.get('pages') or []) if isinstance(page, dict)]
    explicit = compact_map.get('explicit_action_requirements') if isinstance(compact_map.get('explicit_action_requirements'), dict) else {}
    mandatory_keys = {
        (str(item.get('page_key') or ''), str(item.get('element_key') or ''))
        for item in explicit.get('matched_elements') or []
        if isinstance(item, dict) and item.get('element_key')
    }
    for transition in compact_map.get('state_transitions') or []:
        if not isinstance(transition, dict):
            continue
        page_key = str(transition.get('from_page_key') or transition.get('page_key') or '')
        element_key = str(transition.get('element_key') or transition.get('source_element_key') or '')
        if element_key:
            mandatory_keys.add((page_key, element_key))

    included_keys: set[tuple[str, str]] = set()
    pages: list[dict[str, Any]] = []
    for page in source_pages:
        page_key = str(page.get('page_key') or '')
        candidates = [element for element in page.get('elements') or [] if isinstance(element, dict)]
        elements = [
            element for element in candidates
            if (
                (page_key, str(element.get('element_key') or '')) in mandatory_keys
                or ('', str(element.get('element_key') or '')) in mandatory_keys
            )
        ]
        for element in elements:
            included_keys.add((page_key, str(element.get('element_key') or '')))
        pages.append({**page, 'elements': elements})

    remaining = max(0, max(1, max_elements) - len(included_keys))
    while remaining > 0:
        added = False
        for index, page in enumerate(source_pages):
            current = pages[index]['elements']
            candidates = [element for element in (page.get('elements') or []) if isinstance(element, dict)]
            element = next((
                candidate for candidate in candidates
                if (str(page.get('page_key') or ''), str(candidate.get('element_key') or '')) not in included_keys
            ), None)
            if element is None:
                continue
            current.append(element)
            included_keys.add((str(page.get('page_key') or ''), str(element.get('element_key') or '')))
            remaining -= 1
            added = True
            if remaining <= 0:
                break
        if not added:
            break

    required_fields = [
        field for field in compact_map.get('required_fields') or []
        if isinstance(field, dict)
        and (str(field.get('page_key') or ''), str(field.get('element_key') or '')) in included_keys
    ][:30]
    compaction = {
        **(compact_map.get('compaction') if isinstance(compact_map.get('compaction'), dict) else {}),
        'max_elements': max_elements,
        'included_pages': len(pages),
        'included_elements': sum(len(page.get('elements') or []) for page in pages),
        'emergency_prompt_trim': True,
    }
    return {
        **compact_map,
        'required_fields': required_fields,
        'compaction': compaction,
        'pages': pages,
    }


def _build_budgeted_generation_prompt(
    task,
    payload: dict[str, Any],
    element_map: dict[str, Any],
) -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
    """按提示词预算递减裁剪元素地图，避免大页面阻塞 LLM 规划。"""
    prompt_budget = _llm_prompt_char_budget(task)
    base_pages, base_elements = _compact_limits_for_task(task, None, None)
    element_attempts = [
        base_elements,
        min(base_elements, 360),
        min(base_elements, 260),
        min(base_elements, 180),
        MIN_COMPACT_MAX_ELEMENTS,
    ]
    page_attempts = [
        base_pages,
        min(base_pages, 4),
        min(base_pages, 3),
        min(base_pages, 2),
        1,
    ]
    attempts: list[tuple[int, int]] = []
    for pages, elements in zip(page_attempts, element_attempts):
        candidate = (max(1, pages), max(MIN_COMPACT_MAX_ELEMENTS, elements))
        if candidate not in attempts:
            attempts.append(candidate)

    selected_messages: list[Any] = []
    selected_map: dict[str, Any] = {}
    selected_prompt_chars = 0
    shrink_rounds = 0
    multi_page_map: dict[str, Any] = {}
    for index, (max_pages, max_elements) in enumerate(attempts):
        compact_map = _compact_element_map(
            element_map,
            max_pages=max_pages,
            max_elements=max_elements,
            task=task,
        )
        messages = _build_prompt(task, payload, compact_map)
        prompt_chars = _prompt_char_count(messages)
        selected_messages = messages
        selected_map = compact_map
        selected_prompt_chars = prompt_chars
        shrink_rounds = index
        if len(compact_map.get('pages') or []) > 1 and not multi_page_map:
            multi_page_map = compact_map
        if prompt_chars <= prompt_budget:
            break

    if len(selected_map.get('pages') or []) <= 1 and multi_page_map:
        for max_elements in [80, 60, 40, 30, 24, 20, 16, 12, 10, 8, 6, 5]:
            trimmed_map = _trim_compact_map_elements(multi_page_map, max_elements)
            messages = _build_prompt(task, payload, trimmed_map)
            prompt_chars = _prompt_char_count(messages)
            shrink_rounds += 1
            if prompt_chars <= prompt_budget:
                selected_messages = messages
                selected_map = trimmed_map
                selected_prompt_chars = prompt_chars
                break

    if selected_prompt_chars > prompt_budget and selected_map:
        emergency_source = multi_page_map or selected_map
        for max_elements in [120, 80, 60, 40, 30, 20, 10, 8, 5]:
            trimmed_map = _trim_compact_map_elements(emergency_source, max_elements)
            messages = _build_prompt(task, payload, trimmed_map)
            prompt_chars = _prompt_char_count(messages)
            selected_messages = messages
            selected_map = trimmed_map
            selected_prompt_chars = prompt_chars
            shrink_rounds += 1
            if prompt_chars <= prompt_budget:
                break

    metrics = {
        'prompt_budget_chars': prompt_budget,
        'prompt_chars': selected_prompt_chars,
        'prompt_shrink_rounds': shrink_rounds,
        'prompt_over_budget': selected_prompt_chars > prompt_budget,
        'compaction': selected_map.get('compaction') or {},
    }
    return selected_messages, selected_map, metrics


def _runtime_step_prompt_context(
    task,
    payload: dict[str, Any],
) -> dict[str, Any]:
    requirement_document = _build_requirement_document(task)
    runtime_history = payload.get('runtime_history') if isinstance(payload.get('runtime_history'), list) else []
    completed_action_ids = [
        str(item or '').strip()
        for item in (payload.get('completed_action_ids') if isinstance(payload.get('completed_action_ids'), list) else [])
        if str(item or '').strip()
    ]
    current_observation = payload.get('current_observation') if isinstance(payload.get('current_observation'), dict) else {}
    current_candidates = payload.get('current_candidates') if isinstance(payload.get('current_candidates'), list) else []
    safety_policy = getattr(task, 'safety_policy', None)
    safety_policy = safety_policy if isinstance(safety_policy, dict) else {}
    authentication_contract = (
        safety_policy.get('authentication_contract')
        if isinstance(safety_policy.get('authentication_contract'), dict)
        else {}
    )
    return {
        'task': {
            'name': task.name,
            'target_url': task.target_url or '',
            'target_module': task.target_module or '',
        },
        'requirement_document': requirement_document,
        'authentication_contract': authentication_contract,
        'runtime_goal': {
            'source_requirement': (
                requirement_document.get('source_requirement')
                or requirement_document.get('normalized_text')
                or task.source_requirement
                or task.gherkin
                or ''
            ),
            'gherkin': task.gherkin or '',
            'target_module': task.target_module or '',
            'target_url': task.target_url or '',
            'flow': requirement_document.get('flow') if isinstance(requirement_document.get('flow'), list) else [],
        },
        'current_observation': current_observation,
        'current_candidates': current_candidates[:24],
        'completed_action_ids': completed_action_ids,
        'runtime_history': runtime_history[-12:],
        'last_step': payload.get('last_step') if isinstance(payload.get('last_step'), dict) else {},
        'last_error': str(payload.get('last_error') or ''),
        'safety_policy': safety_policy,
        'output_schema': {
            'runtime_plan': {
                'status': 'continue|completed|blocked',
                'reason': '',
                'next_step': {
                    'action_id': '',
                    'step_sort': 0,
                    'operation': '',
                    'action': '',
                    'page_key': '',
                    'element_key': '',
                    'target_name': '',
                    'description': '',
                    'locator_hint': {},
                    'locator_candidates': [],
                    'binding_mode': '',
                    'runtime_resolver': '',
                    'value': '',
                    'expected': '',
                    'context_type': '',
                    'frame_name': '',
                    'frame_url': '',
                    'row_scope': {},
                    'confidence': 0.0,
                },
            }
        },
    }


def _runtime_has_successful_business_history(runtime_history: list[dict[str, Any]]) -> bool:
    for item in runtime_history:
        if not isinstance(item, dict):
            continue
        status = str(item.get('status') or '').strip().lower()
        operation = str(item.get('operation') or item.get('action') or '').strip().lower()
        if status in {'success', 'executed', 'completed', 'repaired'} and operation not in {
            'wait_and_reobserve',
            'reobserve',
            'observe_current_page',
        }:
            return True
    return False


def _runtime_text_variants(value: Any) -> list[str]:
    text = str(value or '').strip()
    if not text:
        return []
    values = [text]
    values.extend(re.findall(r'["“”\'‘’]([^"“”\'‘’]{1,80})["“”\'‘’]', text))
    variants: list[str] = []
    for item in values:
        normalized = _normalize_text(item).strip(' "\'“”‘’[]【】()（）')
        if normalized and normalized not in variants:
            variants.append(normalized)
    return variants


def _runtime_semantic_core_text(value: Any) -> str:
    text = _normalize_text(value)
    if not text:
        return ''
    quoted = re.findall(r'["“”\'‘’]([^"“”\'‘’]{1,80})["“”\'‘’]', text)
    if quoted:
        text = quoted[-1]
    text = _normalize_text(text)
    replacements = [
        '系统应提示',
        '系统应弹出/跳转至',
        '系统应弹出',
        '系统应',
        '应弹出/跳转至',
        '弹出/跳转至',
        '跳转至',
        '我应该进入',
        '我已进入',
        '我进入',
        '我处于',
        '当前处于',
        '当前已进入',
        '已进入',
        '应该进入',
        '进入',
        '表单',
        '页面',
        '页',
        '中',
        '当前',
        '系统',
        '应',
    ]
    for item in replacements:
        text = text.replace(item, '')
    return text.strip(' "\'“”‘’[]【】()（）,，.。')


def _runtime_candidate_label(candidate: dict[str, Any]) -> str:
    actions = [str(item or '').strip().lower() for item in (candidate.get('actions') or [])]
    if any(action in actions for action in ['fill', 'select_option', 'check', 'uncheck']):
        for key in ['label', 'form_label', 'placeholder', 'name', 'accessible_name', 'text', 'test_id']:
            text = _normalize_text(candidate.get(key))
            if text:
                return text
    for key in ['name', 'accessible_name', 'label', 'placeholder', 'text', 'test_id']:
        text = _normalize_text(candidate.get(key))
        if text:
            return text
    return ''


def _runtime_candidate_label_variants(candidate: dict[str, Any]) -> list[str]:
    actions = [str(item or '').strip().lower() for item in (candidate.get('actions') or [])]
    keys = (
        ['label', 'form_label', 'name', 'accessible_name', 'placeholder', 'text', 'test_id']
        if any(action in actions for action in ['fill', 'select_option', 'check', 'uncheck'])
        else ['name', 'accessible_name', 'label', 'form_label', 'placeholder', 'text', 'test_id']
    )
    variants: list[str] = []
    for key in keys:
        text = _normalize_text(candidate.get(key))
        if text and text not in variants:
            variants.append(text)
        stripped = re.sub(r'^(?:请输入|请选择|请填写|输入|选择)', '', text).strip()
        if stripped and stripped not in variants:
            variants.append(stripped)
    return variants


def _runtime_locator_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    actions = [str(item or '').strip().lower() for item in (candidate.get('actions') or [])]
    candidates = []
    hint = candidate.get('recommended_locator') if isinstance(candidate.get('recommended_locator'), dict) else {}
    if hint:
        candidates.append(hint)
    candidates.extend(item for item in (candidate.get('locator_candidates') or []) if isinstance(item, dict))
    if any(action in actions for action in ['fill', 'select_option', 'check', 'uncheck']):
        label = _normalize_text(candidate.get('label') or candidate.get('form_label'))
        if label:
            candidates.insert(0, {'type': 'label', 'value': label, 'confidence': 0.9})
        priority = {
            'test_id': 0,
            'placeholder': 1,
            'id': 2,
            'name': 3,
            'label': 4,
            'css': 5,
            'xpath': 6,
            'role': 7,
            'text': 8,
        }
        usable = [
            dict(item)
            for item in candidates
            if item.get('type') and (item.get('value') not in (None, '') or item.get('name') not in (None, ''))
        ]
        usable.sort(key=lambda item: priority.get(str(item.get('type') or ''), 99))
        return usable[0] if usable else {}
    for item in candidates:
        if isinstance(item, dict) and item.get('type') and (item.get('value') not in (None, '') or item.get('name') not in (None, '')):
            return dict(item)
    return {}


def _runtime_option_texts(option: Any) -> set[str]:
    if not isinstance(option, dict):
        return set()
    values = {
        _normalize_text(option.get('label')),
        _normalize_text(option.get('text')),
        _normalize_text(option.get('name')),
        _normalize_text(option.get('value')),
    }
    return {value for value in values if value}


def _runtime_selected_texts(element: dict[str, Any]) -> set[str]:
    selected = set()
    selected_values = set()
    for key in ['selected_label', 'selected_text', 'selected_value', 'value', 'current_value']:
        text = _normalize_text(element.get(key))
        if text:
            selected.add(text)
        if key in {'selected_value', 'value', 'current_value'} and text:
            selected_values.add(text)
    for option in element.get('options') or []:
        if not isinstance(option, dict):
            continue
        option_raw_values = {
            _normalize_text(option.get('value')),
            _normalize_text(option.get('label')),
            _normalize_text(option.get('text')),
            _normalize_text(option.get('name')),
        }
        if option.get('selected') or any(value and value in option_raw_values for value in selected_values):
            selected.update(_runtime_option_texts(option))
    return selected


def _runtime_action_desired_value(action: dict[str, Any]) -> str:
    for key in ['value', 'expected', 'input', 'option', 'selected_value']:
        text = _normalize_text(action.get(key))
        if text:
            return text
    target = str(action.get('target') or action.get('description') or '')
    quoted = re.findall(r'["“”\']([^"“”\']+)["“”\']', target)
    if quoted:
        return _normalize_text(quoted[-1])
    return ''


def _runtime_action_looks_like_select(action: dict[str, Any]) -> bool:
    action_text = _normalize_text(' '.join([
        str(action.get('operation') or ''),
        str(action.get('action') or ''),
        str(action.get('target') or ''),
        str(action.get('description') or ''),
        str(action.get('expected') or ''),
    ]))
    if not action_text:
        return False
    return any(keyword in action_text for keyword in ['选择', '下拉', '选项'])


def _runtime_select_action_satisfied(
    action: dict[str, Any],
    current_observation: dict[str, Any],
) -> bool:
    operation = _plan_step_operation(action)
    if operation not in {'select_option', 'choose'} and not _runtime_action_looks_like_select(action):
        return False
    desired_value = _runtime_action_desired_value(action)
    if not desired_value:
        return False
    target_variants = _runtime_text_variants(
        action.get('target') or action.get('description') or action.get('expected')
    )
    for candidate in _runtime_observed_candidates(current_observation):
        if not isinstance(candidate, dict):
            continue
        actions = [str(item or '').strip().lower() for item in (candidate.get('actions') or [])]
        if 'select_option' not in actions and str(candidate.get('tag') or '') != 'select':
            continue
        label = _runtime_candidate_label(candidate)
        selected_texts = _runtime_selected_texts(candidate)
        if desired_value in selected_texts:
            if not target_variants:
                return True
            if any(label and (label == target or label in target or target in label) for target in target_variants):
                return True
            # 当前值已经命中目标选项时，即使候选标签未稳定暴露字段名，也应视为已完成。
            return True
    return False


def _runtime_candidate_has_positive_selection_state(candidate: dict[str, Any]) -> bool:
    if not isinstance(candidate, dict):
        return False
    if candidate.get('checked') is True or candidate.get('selected') is True:
        return True
    state_text = ' '.join(
        str(candidate.get(key) or '')
        for key in ['aria_checked', 'aria_selected', 'data_state', 'state_text', 'class_name']
    ).strip().lower()
    if not state_text:
        return False
    negative = re.search(
        r'(^|[\s_-])(unchecked|unselected|inactive|disabled|off|false)([\s_-]|$)|未选中|未勾选|取消选中',
        state_text,
    )
    positive = re.search(
        r'(^|[\s_-])(checked|selected|active|on|true|is-checked|is-selected)([\s_-]|$)|'
        r'check-li-checked|已选中|已勾选|选中',
        state_text,
    )
    return bool(positive and not negative)


def _runtime_check_action_satisfied(
    action: dict[str, Any],
    current_observation: dict[str, Any],
) -> bool:
    operation = _plan_step_operation(action)
    action_text = _normalize_text(' '.join([
        str(action.get('operation') or ''),
        str(action.get('action') or ''),
        str(action.get('target') or ''),
        str(action.get('description') or ''),
        str(action.get('expected') or ''),
        str(action.get('value') or ''),
    ]))
    check_like = (
        operation == 'check'
        or (
            operation in {'', 'click'}
            and any(keyword in action_text for keyword in ['勾选', '选中', '选择'])
            and not any(keyword in action_text for keyword in ['取消', '未勾选', '未选中'])
        )
    )
    if not check_like:
        return False
    target_variants = _runtime_text_variants(
        action.get('value')
        or action.get('target')
        or action.get('description')
        or action.get('expected')
    )
    desired_value = _runtime_action_desired_value(action)
    if desired_value and desired_value not in target_variants:
        target_variants.append(desired_value)
    target_variants = [variant for variant in target_variants if variant]
    if not target_variants:
        return False
    for candidate in _runtime_observed_candidates(current_observation):
        if not isinstance(candidate, dict) or not _runtime_candidate_has_positive_selection_state(candidate):
            continue
        candidate_variants = _runtime_candidate_label_variants(candidate)
        for key in ['row_text', 'column_text', 'text', 'name', 'accessible_name', 'label', 'form_label', 'title']:
            text = _normalize_text(candidate.get(key))
            if text and text not in candidate_variants:
                candidate_variants.append(text)
        if any(
            target and candidate and (target in candidate or candidate in target)
            for target in target_variants
            for candidate in candidate_variants
        ):
            return True
    return False


def _runtime_observed_candidates(current_observation: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(current_observation, dict):
        return []

    collected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    actionable_roles = {
        'button',
        'link',
        'menuitem',
        'treeitem',
        'tab',
        'checkbox',
        'radio',
        'textbox',
        'combobox',
        'option',
    }

    def add(element: Any, *, page_key: str = '', frame_key: str = '') -> None:
        if not isinstance(element, dict):
            return
        locator_hint = _runtime_locator_from_candidate(element)
        label = _runtime_candidate_label(element)
        if not locator_hint or not label:
            return
        key = (
            str(element.get('element_key') or ''),
            str(locator_hint.get('type') or ''),
            str(locator_hint.get('value') or ''),
            label,
        )
        if key in seen:
            return
        seen.add(key)
        copied = dict(element)
        if page_key and not copied.get('page_key'):
            copied['page_key'] = page_key
        if frame_key and not copied.get('frame_key'):
            copied['frame_key'] = frame_key
        collected.append(copied)

    page_key = str(current_observation.get('page_key') or '')
    for element in current_observation.get('elements') or []:
        add(element, page_key=page_key)
    for frame in current_observation.get('frames') or []:
        if not isinstance(frame, dict):
            continue
        frame_key = str(frame.get('frame_key') or '')
        for element in frame.get('elements') or []:
            add(element, page_key=page_key, frame_key=frame_key)

    accessibility_snapshot = (
        current_observation.get('accessibility_snapshot')
        if isinstance(current_observation.get('accessibility_snapshot'), dict)
        else {}
    )

    for node in accessibility_snapshot.get('nodes') or []:
        if not isinstance(node, dict):
            continue
        if node.get('ignored') and not node.get('name') and not node.get('value'):
            continue
        role = _normalize_text(node.get('role'))
        name = _normalize_text(node.get('name') or node.get('value'))
        if not name:
            continue
        if role not in actionable_roles and len(name) > 80:
            continue
        synthetic_element = {
            'element_key': node.get('ax_ref') or f"ax_{len(collected) + 1}",
            'ax_ref': node.get('ax_ref'),
            'name': name,
            'accessible_name': name,
            'text': name,
            'role': role,
            'actions': ['click'] if role in {'button', 'link', 'menuitem', 'treeitem', 'tab', 'option'} else [],
            'page_key': page_key,
        }
        if role in {'textbox', 'combobox', 'checkbox', 'radio'}:
            synthetic_element['actions'] = ['fill'] if role in {'textbox', 'combobox'} else ['check']
        if role in actionable_roles:
            synthetic_element['recommended_locator'] = (
                {'type': 'role', 'value': role, 'name': name}
                if role not in {'textbox', 'combobox'}
                else {'type': 'text', 'value': name}
            )
        else:
            synthetic_element['recommended_locator'] = {'type': 'text', 'value': name}
        add(synthetic_element, page_key=page_key)
    return collected


def _runtime_action_completed(action: dict[str, Any], runtime_history: list[dict[str, Any]]) -> bool:
    action_id = str(action.get('action_id') or '').strip()
    completed_ids = action.get('_completed_action_ids')
    if action_id and isinstance(completed_ids, set) and action_id in completed_ids:
        return True
    action_targets = _runtime_text_variants(action.get('target'))
    action_core = _runtime_semantic_core_text(action.get('target') or action.get('expected'))
    action_operation = _plan_step_operation(action)
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
        if action_id and str(item.get('action_id') or '').strip() == action_id:
            return True
        covered_ids = {
            str(covered_action_id or '').strip()
            for covered_action_id in (item.get('covered_action_ids') or [])
            if str(covered_action_id or '').strip()
        }
        if action_id and action_id in covered_ids:
            return True
        if action_operation and action_operation != operation:
            continue
        item_text = _normalize_text(' '.join([
            str(item.get('target_name') or ''),
            str(item.get('description') or ''),
            str(item.get('element_key') or ''),
        ]))
        if item_text and action_targets and any(target and (target in item_text or item_text in target) for target in action_targets):
            return True
        item_core = _runtime_semantic_core_text(item_text)
        if action_core and item_core and (action_core in item_core or item_core in action_core):
            return True
    return False


def _runtime_action_satisfied_by_current_observation(
    action: dict[str, Any],
    current_observation: dict[str, Any],
) -> bool:
    if not isinstance(action, dict) or not isinstance(current_observation, dict):
        return False
    if _runtime_select_action_satisfied(action, current_observation):
        return True
    if _runtime_check_action_satisfied(action, current_observation):
        return True
    target = action.get('target') or action.get('expected') or action.get('description')
    target_text = _normalize_text(target)
    target_core = _runtime_semantic_core_text(target)
    if not target_core or len(target_core) < 2:
        return False
    phase = _normalize_text(action.get('phase'))
    operation = _plan_step_operation(action)
    state_like = (
        phase in {'given', 'then', 'and'}
        and (
            operation in {'', 'assert_visible', 'assert_text', 'assert_contain_text'}
            or any(keyword in target_text for keyword in ['我处于', '已进入', '应该进入', '应显示', '可见'])
        )
    )
    if not state_like:
        return False

    visible_parts = [
        str(current_observation.get('title') or ''),
        str(current_observation.get('url') or ''),
    ]
    for element in current_observation.get('elements') or []:
        if isinstance(element, dict):
            visible_parts.append(_runtime_candidate_label(element))
    for frame in current_observation.get('frames') or []:
        if not isinstance(frame, dict):
            continue
        visible_parts.extend([
            str(frame.get('title') or ''),
            str(frame.get('url') or ''),
        ])
        for element in frame.get('elements') or []:
            if isinstance(element, dict):
                visible_parts.append(_runtime_candidate_label(element))
    for candidate in current_observation.get('current_candidates') or []:
        if isinstance(candidate, dict):
            visible_parts.append(_runtime_candidate_label(candidate))

    visible_text = _normalize_text(' '.join(part for part in visible_parts if part))
    visible_core = _runtime_semantic_core_text(visible_text)
    return bool(
        visible_core
        and (
            target_core in visible_core
            or any(part and part in visible_core for part in _runtime_text_variants(target_core))
        )
    )


def _runtime_state_looks_like_login_page(state: dict[str, Any]) -> bool:
    if not isinstance(state, dict):
        return False
    elements = [item for item in (state.get('elements') or []) if isinstance(item, dict)]
    page_text = _normalize_text(' '.join([
        str(state.get('url') or ''),
        str(state.get('name') or ''),
        str(state.get('title') or ''),
        _page_element_text(state),
    ]))
    has_password_field = any(
        str(element.get('input_type') or element.get('type') or '').strip().lower() == 'password'
        or any(
            keyword in _normalize_text(' '.join(str(element.get(key) or '') for key in [
                'name', 'accessible_name', 'text', 'label', 'form_label', 'placeholder',
            ]))
            for keyword in ['用户名', '密码', 'username', 'password']
        )
        for element in elements
    )
    has_login_action = any(
        'click' in (element.get('actions') or [])
        and any(
            keyword in _normalize_text(' '.join(str(element.get(key) or '') for key in [
                'name', 'accessible_name', 'text', 'label', 'form_label', 'placeholder',
            ]))
            for keyword in ['登录', 'login', 'sign in', 'signin']
        )
        for element in elements
    )
    return has_password_field and (has_login_action or any(keyword in page_text for keyword in ['login', '登录']))


def _runtime_action_looks_like_authentication(action: dict[str, Any]) -> bool:
    if not isinstance(action, dict):
        return False
    if _normalize_text(action.get('phase')) == 'authentication':
        return True
    text = _normalize_text(' '.join([
        str(action.get('operation') or ''),
        str(action.get('action') or ''),
        str(action.get('target') or ''),
        str(action.get('name') or ''),
        str(action.get('target_name') or ''),
        str(action.get('description') or ''),
        str(action.get('value') or ''),
        str(action.get('expected') or ''),
    ]))
    if not text:
        return False
    return any(keyword in text for keyword in [
        '用户名',
        '密码',
        'password',
        'username',
        '验证码',
        'otp',
        'mfa',
    ])


def _runtime_action_looks_like_state_assertion(action: dict[str, Any]) -> bool:
    if not isinstance(action, dict):
        return False
    phase = _normalize_text(action.get('phase'))
    operation = _plan_step_operation(action)
    text = _normalize_text(' '.join([
        str(action.get('target') or ''),
        str(action.get('expected') or ''),
        str(action.get('description') or ''),
    ]))
    if phase not in {'given', 'then', 'and'}:
        return False
    if operation not in {'', 'assert_visible', 'assert_text', 'assert_contain_text'}:
        return False
    return bool(any(keyword in text for keyword in [
        '进入',
        '处于',
        '跳转',
        '弹出',
        '显示',
        '可见',
        '关闭',
    ]))


def _runtime_latest_completed_flow_index(
    flow: list[dict[str, Any]],
    completed_action_ids: set[str],
    runtime_history: list[dict[str, Any]],
) -> int:
    action_indexes = {
        str(action.get('action_id') or '').strip(): index
        for index, action in enumerate(flow)
        if isinstance(action, dict) and str(action.get('action_id') or '').strip()
    }
    completed_ids = set(completed_action_ids)
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
            completed_ids.add(action_id)
        completed_ids.update(
            str(covered_action_id or '').strip()
            for covered_action_id in (item.get('covered_action_ids') or [])
            if str(covered_action_id or '').strip()
        )
    completed_indexes = [
        action_indexes[action_id]
        for action_id in completed_ids
        if action_id in action_indexes
    ]
    return max(completed_indexes) if completed_indexes else -1


def _runtime_pending_actions(runtime_context: dict[str, Any], runtime_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requirement_document = (
        runtime_context.get('requirement_document')
        if isinstance(runtime_context.get('requirement_document'), dict)
        else {}
    )
    runtime_goal = (
        runtime_context.get('runtime_goal')
        if isinstance(runtime_context.get('runtime_goal'), dict)
        else {}
    )
    flow = requirement_document.get('flow') if isinstance(requirement_document.get('flow'), list) else []
    if not flow:
        flow = runtime_goal.get('flow') if isinstance(runtime_goal.get('flow'), list) else []
    authentication_contract = (
        runtime_context.get('authentication_contract')
        if isinstance(runtime_context.get('authentication_contract'), dict)
        else {}
    )
    authentication_mode = _valid_authentication_mode(authentication_contract.get('mode'))
    completed_action_ids = {
        str(item or '').strip()
        for item in runtime_context.get('completed_action_ids') or []
        if str(item or '').strip()
    }
    latest_completed_flow_index = _runtime_latest_completed_flow_index(
        flow,
        completed_action_ids,
        runtime_history,
    )
    current_observation = (
        runtime_context.get('current_observation')
        if isinstance(runtime_context.get('current_observation'), dict)
        else {}
    )
    should_skip_remaining_authentication = (
        authentication_mode != 'test_subject'
        and flow
        and not _runtime_state_looks_like_login_page(current_observation)
    )
    pending = []
    for index, action in enumerate(flow):
        if not isinstance(action, dict):
            continue
        if action.get('required') is False:
            continue
        if index < latest_completed_flow_index and _runtime_action_looks_like_state_assertion(action):
            continue
        action_with_progress = {**action, '_completed_action_ids': completed_action_ids}
        if (
            not _runtime_action_completed(action_with_progress, runtime_history)
            and not _runtime_action_satisfied_by_current_observation(action, current_observation)
        ):
            if should_skip_remaining_authentication and _runtime_action_looks_like_authentication(action):
                continue
            pending.append(action)
    return pending


def _runtime_operation_for_pending_action(action: dict[str, Any], candidate: dict[str, Any]) -> str:
    operation = _plan_step_operation(action)
    if operation in ALLOWED_OPERATIONS and operation != 'wait_and_reobserve':
        return operation
    actions = [str(item or '').strip().lower() for item in (candidate.get('actions') or [])]
    for item in ['fill', 'select_option', 'check', 'click']:
        if item in actions:
            return item
    return 'click'


def _runtime_step_from_pending_action(
    pending_action: dict[str, Any],
    current_candidates: list[dict[str, Any]],
    current_observation: dict[str, Any],
    runtime_history: list[dict[str, Any]],
) -> dict[str, Any]:
    operation = _plan_step_operation(pending_action)
    target_variants = _runtime_text_variants(
        pending_action.get('target') or pending_action.get('description') or pending_action.get('expected')
    )
    if not target_variants:
        return {}
    merged_candidates = list(current_candidates or [])
    seen_candidate_keys = {
        (
            str(candidate.get('element_key') or ''),
            str(_runtime_locator_from_candidate(candidate).get('type') or ''),
            str(_runtime_locator_from_candidate(candidate).get('value') or ''),
            _runtime_candidate_label(candidate),
        )
        for candidate in merged_candidates
        if isinstance(candidate, dict)
    }
    for observed_candidate in _runtime_observed_candidates(current_observation):
        key = (
            str(observed_candidate.get('element_key') or ''),
            str(_runtime_locator_from_candidate(observed_candidate).get('type') or ''),
            str(_runtime_locator_from_candidate(observed_candidate).get('value') or ''),
            _runtime_candidate_label(observed_candidate),
        )
        if key not in seen_candidate_keys:
            seen_candidate_keys.add(key)
            merged_candidates.append(observed_candidate)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, candidate in enumerate(merged_candidates):
        if not isinstance(candidate, dict):
            continue
        locator_hint = _runtime_locator_from_candidate(candidate)
        if not locator_hint:
            continue
        label_variants = _runtime_candidate_label_variants(candidate)
        if not label_variants:
            continue
        score = 0
        for target in target_variants:
            if not target:
                continue
            for label_variant in label_variants:
                if label_variant == target:
                    score = max(score, 1000 + min(len(label_variant), 80))
                elif label_variant in target or target in label_variant:
                    score = max(score, 500 + min(len(label_variant), len(target), 80))
        desired_value = _runtime_action_desired_value(pending_action)
        if operation in {'select_option', 'choose'} and desired_value:
            candidate_selected_texts = _runtime_selected_texts(candidate)
            candidate_actions = [str(item or '').strip().lower() for item in (candidate.get('actions') or [])]
            if (
                desired_value in candidate_selected_texts
                and (
                    str(candidate.get('tag') or '').strip().lower() == 'select'
                    or str(candidate.get('role') or '').strip().lower() == 'combobox'
                    or 'select_option' in candidate_actions
                )
            ):
                score = max(score, 1300 + min(len(desired_value), 80))
        if score > 0:
            ranked.append((score, -index, candidate))
    if not ranked:
        return {}
    ranked.sort(reverse=True)
    candidate = ranked[0][2]
    locator_hint = _runtime_locator_from_candidate(candidate)
    operation = _runtime_operation_for_pending_action(pending_action, candidate)
    label = _runtime_candidate_label(candidate)
    action_value = pending_action.get('value') or ''
    if not action_value and operation in {'fill', 'select_option', 'choose', 'check', 'uncheck'}:
        action_value = _runtime_action_desired_value(pending_action)
    locator_candidates = []
    for item in [locator_hint, *(candidate.get('locator_candidates') or [])]:
        if not isinstance(item, dict) or not item.get('type'):
            continue
        if item.get('value') in (None, '') and item.get('name') in (None, ''):
            continue
        key = (str(item.get('type') or ''), str(item.get('value') or ''), str(item.get('name') or ''))
        if key in {
            (str(existing.get('type') or ''), str(existing.get('value') or ''), str(existing.get('name') or ''))
            for existing in locator_candidates
        }:
            continue
        locator_candidates.append(dict(item))
    return {
        'action_id': pending_action.get('action_id') or f'runtime_action_{len(runtime_history) + 1}',
        'step_sort': len(runtime_history),
        'operation': operation,
        'action': operation,
        'page_key': candidate.get('page_key') or current_observation.get('page_key') or '',
        'element_key': candidate.get('element_key') or '',
        'target_name': label or pending_action.get('target') or '',
        'description': pending_action.get('target') or label or operation,
        'locator_hint': locator_hint,
        'locator_candidates': locator_candidates,
        'binding_mode': 'runtime_observation',
        'runtime_resolver': 'pending_action_candidate_match',
        'value': action_value,
        'expected': pending_action.get('expected') or '',
        'confidence': 0.95,
        **{
            key: candidate.get(key)
            for key in [
                'context_type', 'frame_key', 'frame_name', 'frame_url', 'source_element_key',
                'options', 'selected_label', 'selected_text', 'selected_value', 'current_value',
            ]
            if candidate.get(key) not in (None, '', [], {})
        },
    }


def _normalize_runtime_step(step: dict[str, Any], current_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(step, dict):
        return {}
    if not any(
        str(step.get(key) or '').strip()
        for key in ('operation', 'action', 'target_name', 'element_key', 'description', 'runtime_resolver')
    ) and not (isinstance(step.get('locator_hint'), dict) and step.get('locator_hint')):
        return {}
    normalized = dict(step)
    normalized['operation'] = _plan_step_operation(normalized)
    if normalized['operation'] == 'goto':
        normalized.setdefault('value', '')
    if not normalized.get('description'):
        normalized['description'] = normalized.get('target_name') or normalized.get('operation') or ''
    if not isinstance(normalized.get('locator_hint'), dict) or not normalized.get('locator_hint'):
        for candidate in current_candidates:
            if not isinstance(candidate, dict):
                continue
            hint = candidate.get('recommended_locator') if isinstance(candidate.get('recommended_locator'), dict) else {}
            if hint.get('type') and hint.get('value') not in (None, ''):
                normalized['locator_hint'] = hint
                break
            locator_candidates = candidate.get('locator_candidates') if isinstance(candidate.get('locator_candidates'), list) else []
            first_locator = next(
                (
                    item for item in locator_candidates
                    if isinstance(item, dict) and item.get('type') and item.get('value') not in (None, '')
                ),
                {},
            )
            if first_locator:
                normalized['locator_hint'] = first_locator
                break
    if not isinstance(normalized.get('locator_candidates'), list) or not normalized.get('locator_candidates'):
        if isinstance(normalized.get('locator_hint'), dict) and normalized['locator_hint']:
            normalized['locator_candidates'] = [normalized['locator_hint']]
    return normalized


def _runtime_wait_and_reobserve_step(reason: str, runtime_history: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        'action_id': f'runtime_wait_{len(runtime_history) + 1}',
        'step_sort': len(runtime_history),
        'operation': 'wait_and_reobserve',
        'action': 'wait_and_reobserve',
        'target_name': 'runtime_page_render',
        'description': reason or '等待页面渲染后重新观察当前页面',
        'binding_mode': 'runtime_observation',
        'runtime_resolver': 'reobserve_current_page',
        'value': 1500,
        'expected': '当前页面完成可交互元素渲染',
        'confidence': 1.0,
        'internal_runtime_primitive': True,
    }


def _build_runtime_agent_plan(task, payload: dict[str, Any]) -> dict[str, Any]:
    active_config = LLMConfig.objects.filter(is_active=True).first()
    if not active_config:
        return {
            'execution_mode': 'runtime_agent',
            'llm_enabled': False,
            'runtime_plan': {
                'status': 'blocked',
                'reason': '没有可用的激活 LLM 配置',
                'next_step': {},
            },
            'runtime_context': {'contract_artifacts': False},
        }

    started_at = time.monotonic()
    request_timeout, max_retries, total_budget = _llm_plan_limits(task, active_config, async_mode=True)
    runtime_context = _runtime_step_prompt_context(task, payload)
    runtime_history = runtime_context.get('runtime_history') if isinstance(runtime_context.get('runtime_history'), list) else []
    pending_actions = _runtime_pending_actions(runtime_context, runtime_history)
    runtime_context['pending_actions'] = pending_actions[:12]
    runtime_context['pending_action_count'] = len(pending_actions)
    prompt_messages = [
        SystemMessage(content=(
            '你是逐步 UI 自动化运行时规划器。'
            '你必须只输出严格 JSON，不能输出 Markdown 或解释文字。'
            '每次只返回一个下一步原语，或者返回 completed/blocked。'
            '不要编造未来页面；只能根据当前页面观察、用户目标和历史执行记录决策。'
            'pending_actions 是必须按顺序覆盖的合同动作；只要 pending_actions 非空，不能返回 completed。'
            '如果当前页面尚未渲染出可交互候选元素，返回 status=continue 且 next_step.operation=wait_and_reobserve。'
            '只有当前观察或历史执行记录能证明用户目标已经达成时，才能返回 completed。'
            '业务动作可以自行生成稳定 action_id，例如 runtime_action_1；不要输出 generated_case、test_plan 或 plan_validation。'
        )),
        HumanMessage(content=json.dumps(runtime_context, ensure_ascii=False)),
    ]

    try:
        response_content = _invoke_openai_compatible_chat(
            active_config,
            prompt_messages,
            temperature=0.1,
            request_timeout=request_timeout,
            max_retries=max_retries,
            total_budget=total_budget,
        )
        parsed = extract_json_from_response(response_content)
        if not isinstance(parsed, dict):
            retry_content = _invoke_openai_compatible_chat(
                active_config,
                [
                    *prompt_messages,
                    HumanMessage(content=(
                        '上一轮输出无法解析为 JSON。请重新输出 runtime_plan JSON，'
                        '只包含 runtime_plan.status、runtime_plan.reason、runtime_plan.next_step。'
                    )),
                ],
                temperature=0.05,
                request_timeout=request_timeout,
                max_retries=max(2, min(max_retries, 3)),
                total_budget=total_budget,
            )
            parsed = extract_json_from_response(retry_content)
        runtime_plan = parsed.get('runtime_plan') if isinstance(parsed, dict) and isinstance(parsed.get('runtime_plan'), dict) else {}
        current_candidates = runtime_context.get('current_candidates') if isinstance(runtime_context.get('current_candidates'), list) else []
        current_observation = (
            runtime_context.get('current_observation')
            if isinstance(runtime_context.get('current_observation'), dict)
            else {}
        )
        observed_candidates = _runtime_observed_candidates(current_observation)
        has_runtime_candidates = bool(current_candidates or observed_candidates)
        next_step = runtime_plan.get('next_step') if isinstance(runtime_plan.get('next_step'), dict) else {}
        next_step = _normalize_runtime_step(next_step, current_candidates)
        runtime_status = str(runtime_plan.get('status') or '').strip().lower()
        deterministic_step = (
            _runtime_step_from_pending_action(
                pending_actions[0],
                current_candidates,
                current_observation,
                runtime_history,
            )
            if pending_actions
            else {}
        )
        if (
            next_step
            and _plan_step_operation(next_step) != 'wait_and_reobserve'
            and not str(next_step.get('action_id') or '').strip()
        ):
            next_step['action_id'] = f'runtime_action_{len(runtime_history) + 1}'
        if deterministic_step and (
            runtime_status in {'completed', 'blocked'}
            or not next_step
            or _plan_step_operation(next_step) == 'wait_and_reobserve'
        ):
            next_step = deterministic_step
            runtime_status = 'continue'
        if not has_runtime_candidates and next_step and _plan_step_operation(next_step) != 'wait_and_reobserve':
            next_step = _runtime_wait_and_reobserve_step(
                str(runtime_plan.get('reason') or '当前页面暂无可交互候选，等待并重新观察'),
                runtime_history,
            )
            runtime_status = 'continue'
        if not has_runtime_candidates and runtime_status in {'completed', 'blocked'}:
            next_step = _runtime_wait_and_reobserve_step(
                str(runtime_plan.get('reason') or '当前页面暂无可交互候选，等待并重新观察'),
                runtime_history,
            )
            runtime_status = 'continue'
        if not next_step and runtime_status not in {'completed', 'blocked'}:
            next_step = _runtime_wait_and_reobserve_step(
                str(runtime_plan.get('reason') or '当前页面暂无可执行下一步，等待并重新观察'),
                runtime_history,
            )
            runtime_status = 'continue'
        if runtime_status == 'completed' and not _runtime_has_successful_business_history(runtime_history):
            runtime_plan = {
                'status': 'continue',
                'reason': runtime_plan.get('reason') or '尚无真实业务动作执行证据，继续观察当前页面',
                'next_step': _runtime_wait_and_reobserve_step(
                    str(runtime_plan.get('reason') or '尚无真实业务动作执行证据，继续观察当前页面'),
                    runtime_history,
                ),
            }
        elif runtime_status == 'completed':
            runtime_plan = {
                'status': 'completed',
                'reason': runtime_plan.get('reason') or '运行时目标已完成',
                'next_step': {},
            }
        elif runtime_status == 'blocked':
            runtime_plan = {
                'status': 'blocked',
                'reason': runtime_plan.get('reason') or 'LLM 无法从当前页面和剩余动作中确定下一步',
                'next_step': next_step,
            }
        else:
            runtime_plan = {
                'status': 'continue',
                'reason': runtime_plan.get('reason') or (
                    '根据待执行动作和当前页面候选生成下一步'
                    if deterministic_step else ''
                ),
                'next_step': next_step,
            }
        return {
            'execution_mode': 'runtime_agent',
            'llm_enabled': True,
            'llm_config': {
                'id': active_config.id,
                'config_name': active_config.config_name,
                'model': active_config.name,
            },
            'llm_metrics': {
                'elapsed_seconds': round(time.monotonic() - started_at, 3),
                'response_chars': len(response_content or ''),
                'request_timeout_seconds': request_timeout,
                'max_retries': max_retries,
                'total_budget_seconds': total_budget,
            },
            'runtime_plan': runtime_plan,
            'runtime_context': {
                'contract_artifacts': bool(runtime_context.get('requirement_document') or pending_actions),
                'current_candidate_count': len(current_candidates),
                'observed_candidate_count': len(observed_candidates),
                'runtime_history_count': len(runtime_history),
                'pending_action_count': len(pending_actions),
                'pending_actions': pending_actions[:12],
                'has_successful_business_history': _runtime_has_successful_business_history(runtime_history),
            },
        }
    except Exception as exc:
        return {
            'execution_mode': 'runtime_agent',
            'llm_enabled': False,
            'llm_error': f'{type(exc).__name__}: {str(exc)[:800]}',
            'runtime_plan': {
                'status': 'blocked',
                'reason': f'{type(exc).__name__}: {str(exc)[:500]}',
                'next_step': {},
            },
            'runtime_context': {'contract_artifacts': False},
        }


def _attach_generated_plan_artifacts(
    task,
    parsed: dict[str, Any],
    element_map: dict[str, Any],
    llm_metrics: dict[str, Any],
    active_config: LLMConfig,
    started_at: float,
    response_content: str,
    async_mode: bool,
    request_timeout: int,
    max_retries: int,
    total_budget: int,
) -> dict[str, Any]:
    generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
    steps = generated_case.get('steps') or parsed.get('test_plan', {}).get('steps') or []
    parsed['llm_enabled'] = True
    parsed['llm_config'] = {
        'id': active_config.id,
        'config_name': active_config.config_name,
        'model': active_config.name,
    }
    parsed['llm_metrics'] = {
        **llm_metrics,
        'elapsed_seconds': round(time.monotonic() - started_at, 3),
        'response_chars': len(response_content or ''),
        'async_mode': async_mode,
        'request_timeout_seconds': request_timeout,
        'max_retries': max_retries,
        'total_budget_seconds': total_budget,
    }
    effective_safety_policy = task.safety_policy or {}
    if isinstance(effective_safety_policy, dict):
        effective_safety_policy = dict(effective_safety_policy)
        if isinstance(llm_metrics.get('requirement_contract'), dict):
            effective_safety_policy['requirement_contract'] = llm_metrics['requirement_contract']
    parsed['plan_validation'] = _validate_generated_steps(steps, element_map, effective_safety_policy, task)
    parsed['generated_script'] = _build_script_from_steps(task, steps, element_map)
    data_lifecycle = (parsed.get('test_plan') or {}).get('data_lifecycle') or generated_case.get('data_lifecycle') or {}
    ts_spec = _build_typescript_spec_from_steps(task, steps, element_map, data_lifecycle)
    ts_files = _build_typescript_file_bundle(ts_spec)
    parsed['generated_typescript_spec'] = ts_spec
    parsed['generated_typescript_files'] = ts_files
    generated_case['playwright_ts_spec'] = ts_spec
    generated_case['playwright_ts_files'] = ts_files
    parsed['generated_case'] = generated_case
    return parsed


def _should_retry_semantic_planning(parsed: dict[str, Any], compact_map: dict[str, Any]) -> bool:
    plan_validation = parsed.get('plan_validation') if isinstance(parsed.get('plan_validation'), dict) else {}
    if plan_validation.get('status') != 'failed':
        return False
    semantic_issues = plan_validation.get('semantic_issues') if isinstance(plan_validation.get('semantic_issues'), list) else []
    retryable_reasons = {
        'unobserved_state_transition',
        'missing_authentication_flow',
        'missing_required_contract_action',
        'runtime_assertion_text_not_observed',
    }
    if not any(
        isinstance(item, dict)
        and (
            str(item.get('reason') or '').startswith('missing_')
            or str(item.get('reason') or '') in retryable_reasons
        )
        for item in semantic_issues
    ):
        return False
    if any(
        isinstance(item, dict) and str(item.get('reason') or '') in retryable_reasons
        for item in semantic_issues
    ):
        return True
    explicit_requirements = compact_map.get('explicit_action_requirements') if isinstance(compact_map.get('explicit_action_requirements'), dict) else {}
    return bool(explicit_requirements.get('matched_elements'))


def _plan_candidate_quality(parsed: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """按校验结果比较完整计划候选，防止重规划覆盖为更差结果。"""
    validation = parsed.get('plan_validation') if isinstance(parsed.get('plan_validation'), dict) else {}
    generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
    test_plan = parsed.get('test_plan') if isinstance(parsed.get('test_plan'), dict) else {}
    steps = generated_case.get('steps') or test_plan.get('steps') or []
    semantic_issues = validation.get('semantic_issues') if isinstance(validation.get('semantic_issues'), list) else []
    unresolved_steps = validation.get('unresolved_steps') if isinstance(validation.get('unresolved_steps'), list) else []
    return (
        1 if validation.get('status') == 'success' else 0,
        -len(semantic_issues),
        -len(unresolved_steps),
        int(validation.get('bound_element_steps') or 0),
        len(steps) if isinstance(steps, list) else 0,
    )


def _semantic_replan_payload(
    payload: dict[str, Any],
    parsed: dict[str, Any],
    plan_validation: dict[str, Any],
    attempt: int,
) -> dict[str, Any]:
    semantic_issues = plan_validation.get('semantic_issues') if isinstance(plan_validation.get('semantic_issues'), list) else []
    unresolved_steps = plan_validation.get('unresolved_steps') if isinstance(plan_validation.get('unresolved_steps'), list) else []
    def issue_message(item: dict[str, Any]) -> str:
        message = str(item.get('message') or item.get('reason') or '')
        if item.get('reason') == 'runtime_assertion_text_not_observed':
            expected = str(item.get('expected_text') or '').strip()
            observed = str(item.get('observed_runtime_text_excerpt') or '').strip()
            details = []
            if expected:
                details.append(f'expected={expected[:160]}')
            if observed:
                details.append(f'observed={observed[:240]}')
            if details:
                return f"{message} ({'; '.join(details)})"
        return message

    issue_messages = [
        issue_message(item)
        for item in [*semantic_issues, *unresolved_steps]
        if isinstance(item, dict) and (item.get('message') or item.get('reason'))
    ]
    generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
    test_plan = parsed.get('test_plan') if isinstance(parsed.get('test_plan'), dict) else {}
    return {
        **payload,
        'semantic_validation_retry': True,
        'semantic_validation_attempt': attempt,
        'planning_phase': f'semantic_validation_replan_{attempt}',
        'repair_instruction': (
            '上一轮候选计划未通过语义或元素绑定校验。请保留已正确覆盖的步骤，基于 '
            'explicit_action_requirements.matched_elements 和 state_transitions 重新输出完整计划，'
            '若断言文本没有当前页面事实支撑，必须改为当前页面已观察到的只读断言，'
            '或加入能够让该文本出现的已绑定前置动作；不得保留未观察到的断言文本。'
            '修复以下缺口：' + '；'.join(issue_messages[:8])
        ),
        'previous_candidate_plan': {
            'intent': parsed.get('intent') if isinstance(parsed.get('intent'), dict) else {},
            'test_plan': {
                key: value for key, value in test_plan.items()
                if key not in {'playwright_ts_spec', 'playwright_ts_files'}
            },
            'generated_case': {
                key: value for key, value in generated_case.items()
                if key not in {'playwright_ts_spec', 'playwright_ts_files', 'playwright_ts_spec_hash'}
            },
            'plan_validation': plan_validation,
        },
        'verification_result': {
            **(payload.get('verification_result') if isinstance(payload.get('verification_result'), dict) else {}),
            'plan_validation': plan_validation,
            'semantic_issues': semantic_issues,
            'unresolved_steps': unresolved_steps,
        },
    }


def _compile_missing_explicit_actions(
    task: Any,
    parsed: dict[str, Any],
    element_map: dict[str, Any],
    *,
    append_missing: bool = True,
) -> dict[str, Any]:
    """把已结构化且已绑定的显式需求编译进计划，不扩展模糊业务意图。"""
    generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
    test_plan = parsed.get('test_plan') if isinstance(parsed.get('test_plan'), dict) else {}
    steps = [
        dict(step) for step in (generated_case.get('steps') or test_plan.get('steps') or [])
        if isinstance(step, dict)
    ]
    if not steps:
        return parsed

    compact_map = _compact_element_map(element_map, task=task)
    explicit = compact_map.get('explicit_action_requirements') if isinstance(compact_map.get('explicit_action_requirements'), dict) else {}
    matches = [item for item in (explicit.get('matched_elements') or []) if isinstance(item, dict)]
    if not matches:
        return parsed

    def covered(requirement: dict[str, Any]) -> bool:
        operation = str(requirement.get('operation') or '')
        name = _normalize_text(requirement.get('name') or '')
        value = _normalize_text(requirement.get('value') or '')
        expected_ops = {'fill', 'select_option', 'check', 'uncheck'} if operation in {
            'fill', 'select_option', 'check', 'uncheck'
        } else {operation}
        for step in steps:
            step_operation = str(step.get('operation') or '').strip().lower()
            if step_operation not in expected_ops:
                continue
            text = _normalize_text(' '.join(str(step.get(key) or '') for key in [
                'target_name', 'description', 'value', 'expected', 'action',
            ]))
            if (name and name in text) or (value and value in text):
                return True
        return False

    compiled_requirements: set[tuple[str, str, str]] = set()
    compiled_count = 0
    max_steps = max(1, min(int((task.safety_policy or {}).get('max_steps') or 24), 50))
    def step_matches_requirement(step: dict[str, Any], requirement: dict[str, Any], match: dict[str, Any]) -> bool:
        if match.get('element_key') and str(step.get('element_key') or '') == str(match.get('element_key')):
            if not match.get('page_key') or str(step.get('page_key') or '') == str(match.get('page_key')):
                return True
        name = _normalize_text(requirement.get('name') or '')
        text = _normalize_text(' '.join(str(step.get(key) or '') for key in ['target_name', 'description', 'value', 'expected']))
        return bool(name and name in text)

    for match in matches:
        requirement = match.get('requirement') if isinstance(match.get('requirement'), dict) else {}
        identity = (
            _normalize_text(requirement.get('name') or ''),
            _normalize_text(requirement.get('value') or ''),
            str(requirement.get('operation') or ''),
        )
        if identity in compiled_requirements or len(steps) >= max_steps:
            continue
        compiled_requirements.add(identity)
        operation = str(requirement.get('operation') or '').strip().lower()
        if operation not in {'fill', 'select_option', 'check', 'uncheck', 'click'}:
            continue
        existing = next((item for item in steps if isinstance(item, dict) and step_matches_requirement(item, requirement, match)), None)
        if existing is not None:
            existing['operation'] = operation
            existing['action'] = operation
            existing['page_key'] = match.get('page_key') or existing.get('page_key') or ''
            existing['element_key'] = match.get('element_key') or existing.get('element_key') or ''
            existing['target_name'] = match.get('name') or existing.get('target_name') or requirement.get('name') or ''
            existing['locator_hint'] = match.get('locator_hint') or existing.get('locator_hint') or {}
            if match.get('row_scope'):
                existing['row_scope'] = match.get('row_scope')
            if operation in {'fill', 'select_option'}:
                existing['value'] = match.get('suggested_value') or existing.get('value') or ''
            existing['compiled_from_explicit_requirement'] = True
            if requirement.get('phase'):
                existing['requirement_phase'] = requirement.get('phase')
            compiled_count += 1
            continue
        if not append_missing:
            continue
        step = {
            'action': operation,
            'operation': operation,
            'page_key': match.get('page_key') or '',
            'element_key': match.get('element_key') or '',
            'target_name': match.get('name') or requirement.get('name') or '',
            'description': f"执行用户明确要求的动作：{requirement.get('name') or match.get('name') or operation}",
            'locator_hint': match.get('locator_hint') if isinstance(match.get('locator_hint'), dict) else {},
            'row_scope': match.get('row_scope') if isinstance(match.get('row_scope'), dict) else {},
            'value': match.get('suggested_value') if operation in {'fill', 'select_option'} else '',
            'expected': '',
            'risk_level': 'low',
            'requires_confirmation': False,
            'confidence': float((match.get('locator_hint') or {}).get('confidence') or 0.8),
            'compiled_from_explicit_requirement': True,
            'requirement_phase': requirement.get('phase') or '',
        }
        steps.append(step)
        compiled_count += 1

    if not compiled_count:
        return parsed
    authentication_steps = [
        step for step in steps
        if isinstance(step, dict) and step.get('requirement_phase') == 'authentication'
    ]
    if authentication_steps:
        authentication_steps.sort(key=lambda step: 1 if _plan_step_operation(step) == 'click' else 0)
        steps = [step for step in steps if step not in authentication_steps]
        insert_at = 0
        while insert_at < len(steps) and _plan_step_operation(steps[insert_at]) in {'goto', 'wait'}:
            insert_at += 1
        steps[insert_at:insert_at] = authentication_steps
    steps = _ensure_state_transition_steps_for_plan(task, steps, element_map, max_steps)
    for index, step in enumerate(steps):
        step['step_sort'] = index
    parsed['generated_case'] = {**generated_case, 'steps': steps}
    parsed['test_plan'] = {**test_plan, 'steps': steps}
    parsed['explicit_action_compilation'] = {
        'compiled_count': compiled_count,
        'source': 'structured_requirement_and_element_map_binding',
    }
    return parsed


def _stage_json_messages(stage: str, instruction: str, stage_input: dict[str, Any]) -> list[Any]:
    return [
        SystemMessage(content=(
            '你是 Web UI 自动化规划流水线中的独立阶段。只完成当前阶段职责，'
            '只返回严格合法的 JSON 对象，不输出 Markdown、解释或后续阶段内容。'
        )),
        HumanMessage(content=json.dumps({
            'stage': stage,
            'instruction': instruction,
            'input': stage_input,
        }, ensure_ascii=False)),
    ]


def _stage_output_repair_messages(stage: str, content: str) -> list[Any]:
    return [
        SystemMessage(content=(
            '你是 JSON 格式修复器。只把用户提供的文本修复为一个严格合法、完整、紧凑的 JSON 对象；'
            '不要引入额外事实，不要解释，不要输出 Markdown。'
        )),
        HumanMessage(content=json.dumps({
            'stage': stage,
            'required_output': {
                'type': 'object',
                'only_json': True,
                'compact': True,
            },
            'previous_response': (content or '')[:12000],
        }, ensure_ascii=False)),
    ]


def _compact_stage_output(stage: str, content: str) -> str:
    if stage != 'requirement_analysis':
        return content
    parsed = extract_json_from_response(content)
    if not isinstance(parsed, dict):
        return content
    compact = {
        'goal': parsed.get('goal') or '',
        'preconditions': (parsed.get('preconditions') or [])[:6],
        'flow': (parsed.get('flow') or [])[:10],
        'actions': (parsed.get('actions') or [])[:10],
        'assertions': (parsed.get('assertions') or [])[:6],
        'ambiguities': (parsed.get('ambiguities') or [])[:6],
    }
    return json.dumps(compact, ensure_ascii=False)


def _invoke_planning_stage_json(
    active_config: LLMConfig,
    stage: str,
    instruction: str,
    stage_input: dict[str, Any],
    *,
    request_timeout: int,
    max_retries: int,
    total_budget: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    messages = _stage_json_messages(stage, instruction, stage_input)
    started_at = time.monotonic()
    deadline = started_at + max(10, total_budget)
    output_limits = {
        'requirement_analysis': 2048,
        'state_route_selection': 2048,
        'element_binding': 4096,
        'executable_plan_generation': 8192,
    }

    def invoke(
        messages_to_send: list[Any],
        temperature: float,
        *,
        max_completion_tokens: int | None = None,
    ) -> str:
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise TimeoutError(f'{stage} 阶段超过共享预算')
        return _invoke_openai_compatible_chat(
            active_config,
            messages_to_send,
            temperature=temperature,
            request_timeout=min(request_timeout, max(1, int(remaining))),
            max_retries=max_retries,
            total_budget=max(1, int(remaining)),
            stream=bool(getattr(active_config, 'enable_streaming', False)),
            reasoning_effort='low',
            max_completion_tokens=max_completion_tokens or output_limits.get(stage, 8192),
        )

    content = invoke(messages, 0.05)
    content = _compact_stage_output(stage, content)
    parsed = extract_json_from_response(content)
    if not isinstance(parsed, dict):
        repair_content = invoke(
            _stage_output_repair_messages(stage, content),
            0.0,
            max_completion_tokens=min(output_limits.get(stage, 8192), 2048),
        )
        repair_content = _compact_stage_output(stage, repair_content)
        parsed = extract_json_from_response(repair_content)
        if isinstance(parsed, dict):
            content = repair_content
    if not isinstance(parsed, dict):
        retry_messages = [
            *messages,
            HumanMessage(content=(
                '上一响应不是合法 JSON。重新执行当前阶段并返回紧凑、完整、严格合法的 JSON 对象；'
                '数组只保留对后续规划必要的项目，不要重复输入，不要输出解释。'
            )),
        ]
        content = invoke(retry_messages, 0.0)
        content = _compact_stage_output(stage, content)
        parsed = extract_json_from_response(content)
    if not isinstance(parsed, dict):
        raise ValueError(f'{stage} 阶段未返回合法 JSON')
    return parsed, {
        'stage': stage,
        'input_chars': len(json.dumps(stage_input, ensure_ascii=False)),
        'output_chars': len(content or ''),
        'elapsed_seconds': round(time.monotonic() - started_at, 3),
    }


def _planning_observed_elements(page: dict[str, Any]) -> list[dict[str, Any]]:
    elements = [
        {**element, 'context_type': element.get('context_type') or 'main'}
        for element in (page.get('elements') or [])
        if isinstance(element, dict)
    ]
    for frame in page.get('frames') or []:
        if not isinstance(frame, dict):
            continue
        frame_key = str(frame.get('frame_key') or '')
        for element in frame.get('elements') or []:
            if not isinstance(element, dict):
                continue
            source_key = str(element.get('source_element_key') or element.get('element_key') or '')
            elements.append({
                **element,
                'element_key': (
                    str(element.get('element_key') or '')
                    if str(element.get('element_key') or '').startswith(f'{frame_key}::')
                    else f'{frame_key}::{source_key}'
                ),
                'source_element_key': source_key,
                'context_type': 'frame',
                'frame_key': frame_key,
                'frame_name': frame.get('name') or '',
                'frame_url': frame.get('url') or '',
            })
    return elements


def _planning_page_summaries(compact_map: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = []
    for page in compact_map.get('pages') or []:
        if not isinstance(page, dict):
            continue
        summaries.append({
            'page_key': page.get('page_key') or '',
            'url': page.get('url') or '',
            'name': page.get('name') or page.get('title') or '',
            'runtime_fresh': bool(page.get('runtime_fresh')),
            'elements': [
                {
                    'element_key': element.get('element_key') or '',
                    'name': element.get('name') or element.get('accessible_name') or '',
                    'actions': element.get('actions') or [],
                }
                for element in _planning_observed_elements(page)
                if isinstance(element, dict)
            ],
        })
    return summaries


def _planning_binding_candidates(compact_map: dict[str, Any], route: dict[str, Any]) -> list[dict[str, Any]]:
    route_keys = {
        str(value or '')
        for value in route.get('ordered_page_keys') or route.get('page_keys') or []
        if value
    }
    pages = []
    for page in compact_map.get('pages') or []:
        if not isinstance(page, dict):
            continue
        page_key = str(page.get('page_key') or '')
        if route_keys and page_key not in route_keys:
            continue
        pages.append({
            'page_key': page_key,
            'url': page.get('url') or '',
            'elements': [
                {
                    key: element.get(key)
                    for key in [
                        'element_key', 'name', 'role', 'tag', 'label', 'form_label',
                        'placeholder', 'actions', 'options', 'recommended_locator',
                        'locator_candidates', 'in_dialog', 'dialog_name', 'required',
                        'context_type', 'frame_key', 'frame_name', 'frame_url', 'source_element_key',
                        'row_text', 'row_index', 'row_cells', 'column_index', 'column_name',
                        'column_text', 'table_name',
                    ]
                    if element.get(key) not in (None, '', [], {})
                }
                for element in _planning_observed_elements(page)
                if isinstance(element, dict)
            ],
        })
    return pages


def _requirement_terms(requirement: dict[str, Any]) -> list[str]:
    text = ' '.join(str(requirement.get(key) or '') for key in [
        'target', 'value', 'expected', 'phase', 'operation', 'action', 'description',
    ])
    terms: list[str] = []
    for token in re.split(r'(?:->|[\s_/，,。；;:：()（）"“”\'\[\]{}<>《》-])+', text):
        token = token.strip()
        if len(token) >= 2 and token not in {'when', 'then', 'and', 'given'}:
            terms.append(token)
    return list(dict.fromkeys(terms))[:24]


def _element_planning_excerpt(element: dict[str, Any]) -> dict[str, Any]:
    excerpt = {
        key: element.get(key)
        for key in [
            'element_key', 'name', 'accessible_name', 'text', 'role', 'tag',
            'label', 'form_label', 'placeholder', 'actions',
            'recommended_locator', 'in_dialog',
            'dialog_name', 'required', 'context_type', 'frame_key',
            'frame_name', 'frame_url', 'source_element_key',
            'row_text', 'row_index', 'row_cells', 'column_index', 'column_name',
            'column_text', 'table_name',
        ]
        if element.get(key) not in (None, '', [], {})
    }
    row_scope = _row_scope(element)
    if row_scope:
        excerpt['row_scope'] = row_scope
    options = element.get('options') if isinstance(element.get('options'), list) else []
    if options:
        excerpt['options'] = options[:4]
        if len(options) > 4:
            excerpt['option_count'] = len(options)
    locator_candidates = element.get('locator_candidates') if isinstance(element.get('locator_candidates'), list) else []
    if locator_candidates:
        excerpt['locator_candidates'] = locator_candidates[:1]
    structural = {
        key: element.get(key)
        for key in [
            'row_text', 'row_index', 'column_name', 'column_text', 'table_name',
            'parent_text', 'container_text', 'nearby_text', 'aria_label',
        ]
        if element.get(key) not in (None, '', [], {})
    }
    if structural:
        excerpt['structural_context'] = structural
    return excerpt


def _action_candidate_score(element: dict[str, Any], requirement: dict[str, Any]) -> int:
    operation = str(requirement.get('operation') or '').strip()
    target = _normalize_text(requirement.get('target') or '')
    value = _normalize_text(requirement.get('value') or requirement.get('expected') or '')
    terms = [_normalize_text(term) for term in _requirement_terms(requirement)]
    text = _normalize_text(' '.join(str(element.get(key) or '') for key in [
        'name', 'accessible_name', 'text', 'label', 'form_label', 'placeholder',
        'dialog_name', 'row_text', 'column_name', 'column_text', 'table_name',
        'parent_text', 'container_text', 'nearby_text',
    ]))
    actions = element.get('actions') or []
    score = 0
    if operation in {'fill', 'choose', 'select_option', 'check', 'uncheck'}:
        if any(action in actions for action in ['fill', 'select_option', 'check', 'uncheck', 'click']):
            score += 40
    elif operation in {'click', 'assert_visible', 'assert_text', 'assert_contain_text', 'assert_value'}:
        if 'click' in actions or operation.startswith('assert'):
            score += 30
        if operation == 'click':
            role = str(element.get('role') or '').strip().lower()
            tag = str(element.get('tag') or '').strip().lower()
            if role in {'button', 'link', 'menuitem'} or tag in {'button', 'a'}:
                score += 45
    if target:
        if target in text:
            score += 160
        else:
            score += _text_overlap_score(target, text) * 8
    if value:
        if value in text:
            score += 140
        else:
            score += max((_text_overlap_score(part, text) for part in terms if part), default=0) * 5
    for term in terms:
        if term and term in text:
            score += min(80, len(term) * 12)
    if element.get('required') or element.get('inferred_required'):
        score += 15
    if element.get('in_dialog') or element.get('dialog_name'):
        score += 15
    return score


def _merge_binding_results(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    base_bindings = [
        item for item in (base.get('bindings') or [])
        if isinstance(item, dict) and item.get('action_id')
    ]
    patch_bindings = [
        item for item in (patch.get('bindings') or [])
        if isinstance(item, dict) and item.get('action_id')
    ]
    by_action = {str(item.get('action_id')): item for item in base_bindings}
    for item in patch_bindings:
        by_action[str(item.get('action_id'))] = item

    unresolved = []
    seen_unresolved: set[str] = set()
    bound_ids = set(by_action)
    for item in [
        *(base.get('unresolved') or []),
        *(patch.get('unresolved') or []),
    ]:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get('action_id') or '')
        if action_id and action_id not in bound_ids and action_id not in seen_unresolved:
            seen_unresolved.add(action_id)
            unresolved.append(item)

    return {
        **base,
        **patch,
        'bindings': list(by_action.values()),
        'unresolved': unresolved[:30],
        'merge_strategy': 'incremental_action_binding',
    }


def _runtime_state_candidate_page(element_map: dict[str, Any], route: dict[str, Any] | RouteContract) -> str:
    route_contract = route if isinstance(route, RouteContract) else RouteContract.from_result(route if isinstance(route, dict) else {})
    route_keys = [str(key or '') for key in route_contract.ordered_page_keys if str(key or '').strip()]
    pages = [
        page for page in (element_map.get('pages') or [])
        if isinstance(page, dict)
    ]
    if not pages:
        return ''
    by_key = {str(page.get('page_key') or ''): page for page in pages}
    ordered = [by_key[key] for key in route_keys if key in by_key]
    if not ordered:
        ordered = pages

    def score(page: dict[str, Any]) -> tuple[int, int]:
        elements = [element for element in (page.get('elements') or []) if isinstance(element, dict)]
        frames = [frame for frame in (page.get('frames') or []) if isinstance(frame, dict)]
        return (
            1 if page.get('runtime_fresh') or page.get('capture_source') == 'failure_reobserve' else 0,
            len(elements) + sum(len(frame.get('elements') or []) for frame in frames),
        )

    ranked = sorted(enumerate(ordered), key=lambda item: (score(item[1]), item[0]), reverse=True)
    return str(ranked[0][1].get('page_key') or '') if ranked else ''


def _previous_bound_action_id(
    flow: list[dict[str, Any]],
    action_id: str,
    bindings_by_action: dict[str, dict[str, Any]],
) -> str:
    previous = ''
    for requirement in flow:
        if not isinstance(requirement, dict):
            continue
        current_id = str(requirement.get('action_id') or '')
        if current_id == action_id:
            return previous
        operation = str(requirement.get('operation') or '').strip().lower()
        if current_id in bindings_by_action and not is_assertion_operation(operation):
            previous = current_id
    return previous


def _compile_runtime_state_evidence_bindings(
    requirements: dict[str, Any],
    bindings: dict[str, Any] | BindingContract,
    element_map: dict[str, Any],
    route: dict[str, Any] | RouteContract,
) -> dict[str, Any]:
    """Convert unbound state assertions into two-stage runtime evidence bindings.

    The static stage proves only that an observed page scope exists and that a
    prior concrete action can produce a before/after sample. The runtime helper
    then validates DOM/ARIA/class/data-state evidence after the action.
    """
    contract = build_requirement_contract(requirements if isinstance(requirements, dict) else {})
    binding_contract = BindingContract.from_result(bindings)
    if not binding_contract.unresolved:
        return binding_contract.as_dict()
    page_key = _runtime_state_candidate_page(element_map, route)
    if not page_key:
        return binding_contract.as_dict()

    compiled = [dict(item) for item in binding_contract.bindings.values()]
    unresolved = [dict(item) for item in binding_contract.unresolved.values()]
    bindings_by_action = {str(item.get('action_id') or ''): item for item in compiled}
    flow_by_action = {
        str(item.get('action_id') or ''): item
        for item in contract.flow
        if isinstance(item, dict) and item.get('action_id')
    }
    converted_ids: set[str] = set()
    for gap in unresolved:
        action_id = str(gap.get('action_id') or '')
        requirement = flow_by_action.get(action_id) or {}
        operation = str(requirement.get('operation') or gap.get('operation') or '').strip().lower()
        if not action_id or not is_assertion_operation(operation):
            continue
        previous_action_id = _previous_bound_action_id(contract.flow, action_id, bindings_by_action)
        if not previous_action_id:
            continue
        expected = str(requirement.get('expected') or requirement.get('value') or gap.get('expected') or gap.get('value') or '').strip()
        target = str(requirement.get('target') or requirement.get('name') or requirement.get('target_name') or gap.get('target_name') or '').strip()
        binding = {
            'action_id': action_id,
            'operation': operation,
            'page_key': page_key,
            'element_key': '',
            'target_name': target,
            'value': expected,
            'binding_mode': 'runtime_state',
            'runtime_resolver': 'state_evidence',
            'locator_hint': {},
            'locator_candidates': [],
            'row_scope': {},
            'state_assertion': {
                'target': target,
                'expected': expected,
                'previous_action_id': previous_action_id,
                'page_key': page_key,
                'source': 'two_stage_runtime_state_evidence',
            },
            'confidence': 0.72,
            'reason': 'static_scope_observed_runtime_state_evidence_deferred',
        }
        compiled.append(binding)
        bindings_by_action[action_id] = binding
        converted_ids.add(action_id)

    if not converted_ids:
        return binding_contract.as_dict()
    return {
        'bindings': compiled,
        'unresolved': [
            item for item in unresolved
            if str(item.get('action_id') or '') not in converted_ids
        ],
        'runtime_state_evidence_compilation': {
            'converted_action_ids': sorted(converted_ids),
            'page_key': page_key,
            'source': 'two_stage_evidence_model',
        },
    }


def _standardize_unresolved_gap_steps(
    parsed: dict[str, Any],
    bindings: dict[str, Any],
) -> dict[str, Any]:
    return _contract_standardize_unresolved_gap_steps(parsed, bindings)


def _standardize_staged_contract_steps(
    parsed: dict[str, Any],
    requirements: dict[str, Any],
    bindings: dict[str, Any],
) -> dict[str, Any]:
    return _contract_standardize_staged_contract_steps(parsed, requirements, bindings)


def _has_generated_typescript_spec(result: dict[str, Any]) -> bool:
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


def _issue_action_ids(issues: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get('action_id') or '')
        for item in issues
        if isinstance(item, dict) and str(item.get('action_id') or '').strip()
    }


def _planning_action_binding_candidates(
    compact_map: dict[str, Any],
    route: dict[str, Any],
    requirements: dict[str, Any],
    *,
    focus_action_ids: set[str] | None = None,
) -> dict[str, Any]:
    route_keys = {
        str(value or '')
        for value in route.get('ordered_page_keys') or route.get('page_keys') or []
        if value
    }
    flow = requirements.get('flow') if isinstance(requirements.get('flow'), list) else []
    if not flow:
        flow = [
            item for item in [
                *(requirements.get('actions') or []),
                *(requirements.get('assertions') or []),
            ]
            if isinstance(item, dict)
        ]

    pages = [
        page for page in (compact_map.get('pages') or [])
        if isinstance(page, dict)
        and (not route_keys or str(page.get('page_key') or '') in route_keys)
    ]
    candidates_by_action: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    for requirement in flow:
        if not isinstance(requirement, dict) or not requirement.get('action_id'):
            continue
        action_id = str(requirement.get('action_id'))
        if focus_action_ids and action_id not in focus_action_ids:
            continue
        operation = str(requirement.get('operation') or '')
        if operation in {'goto', 'wait'}:
            continue
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for page in pages:
            page_key = str(page.get('page_key') or '')
            for element in _planning_observed_elements(page):
                if not isinstance(element, dict):
                    continue
                score = _action_candidate_score(element, requirement)
                if score <= 0:
                    continue
                scored.append((score, page_key, element))
        scored.sort(key=lambda item: item[0], reverse=True)
        if focus_action_ids:
            limit = 8 if operation in {'click', 'assert_visible', 'assert_text', 'assert_contain_text'} else 6
        else:
            limit = 6 if operation in {'click', 'assert_visible', 'assert_text', 'assert_contain_text'} else 4
        top = []
        seen: set[tuple[str, str]] = set()
        for score, page_key, element in scored[:limit]:
            key = (page_key, str(element.get('element_key') or ''))
            if key in seen:
                continue
            seen.add(key)
            selected_keys.add(key)
            top.append({
                'score': score,
                'page_key': page_key,
                **_element_planning_excerpt(element),
            })
        candidates_by_action.append({
            'action_id': action_id,
            'operation': operation,
            'target': requirement.get('target') or '',
            'value': requirement.get('value') or requirement.get('expected') or '',
            'keywords': _requirement_terms(requirement),
            'candidates': top,
        })

    page_context = []
    elements_by_page: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        page_key = str(page.get('page_key') or '')
        for element in _planning_observed_elements(page):
            if not isinstance(element, dict):
                continue
            key = (page_key, str(element.get('element_key') or ''))
            if key in selected_keys:
                elements_by_page[page_key].append(_element_planning_excerpt(element))
    for page in pages:
        page_key = str(page.get('page_key') or '')
        page_context.append({
            'page_key': page_key,
            'url': page.get('url') or '',
            'name': page.get('name') or page.get('title') or '',
            'runtime_fresh': bool(page.get('runtime_fresh')),
            'candidate_element_count': len(elements_by_page.get(page_key) or []),
            'elements': (elements_by_page.get(page_key) or [])[:20],
        })

    total_candidates = sum(len(item.get('candidates') or []) for item in candidates_by_action)
    return {
        'mode': 'action_scoped_candidates',
        'focus_action_ids': sorted(focus_action_ids or []),
        'candidate_count': total_candidates,
        'actions': candidates_by_action,
        'page_context': page_context,
    }


def _planning_failure_summary(payload: dict[str, Any]) -> dict[str, Any]:
    verification = payload.get('verification_result') if isinstance(payload.get('verification_result'), dict) else {}
    execution = verification.get('typescript_execution') if isinstance(verification.get('typescript_execution'), dict) else {}
    reobserve = verification.get('failure_reobserve') if isinstance(verification.get('failure_reobserve'), dict) else {}
    page_state = reobserve.get('page_state') if isinstance(reobserve.get('page_state'), dict) else {}
    return {
        'planning_phase': payload.get('planning_phase') or 'initial',
        'repair_instruction': payload.get('repair_instruction') or '',
        'plan_validation': verification.get('plan_validation') or {},
        'execution_status': execution.get('status') or '',
        'failure_classification': execution.get('failure_classification') or {},
        'failed_spec_step_marker': execution.get('failed_spec_step_marker') or {},
        'fresh_page': {
            'page_key': page_state.get('page_key') or '',
            'url': page_state.get('url') or '',
        },
    }


def _staged_plan_contract_issues(
    requirements: dict[str, Any],
    bindings: dict[str, Any],
    parsed: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate hand-offs between planner roles without rewriting their output."""
    return _contract_staged_plan_contract_issues(requirements, bindings, parsed)
    flow = requirements.get('flow') if isinstance(requirements.get('flow'), list) else []
    if not flow:
        flow = [
            item for item in [
                *(requirements.get('actions') or []),
                *(requirements.get('assertions') or []),
            ]
            if isinstance(item, dict)
        ]
    required_flow = [
        item for item in flow
        if isinstance(item, dict) and item.get('action_id') and item.get('required', True)
    ]
    binding_by_action = {
        str(item.get('action_id')): item
        for item in (bindings.get('bindings') or [])
        if isinstance(item, dict) and item.get('action_id')
    }
    unresolved_ids = _unresolved_action_ids(bindings)
    test_plan = parsed.get('test_plan') if isinstance(parsed.get('test_plan'), dict) else {}
    generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
    steps = generated_case.get('steps') or test_plan.get('steps') or []
    step_by_action = {
        str(item.get('action_id')): item
        for item in steps
        if isinstance(item, dict) and item.get('action_id')
    }
    issues: list[dict[str, Any]] = []

    expected_ids = [str(item.get('action_id')) for item in required_flow]
    actual_ids = [
        str(item.get('action_id'))
        for item in steps
        if isinstance(item, dict) and str(item.get('action_id') or '') in expected_ids
    ]
    expected_positions = {action_id: index for index, action_id in enumerate(expected_ids)}
    actual_positions = [expected_positions[action_id] for action_id in actual_ids]
    if actual_positions != sorted(actual_positions):
        issues.append({
            'reason': 'staged_action_order_mismatch',
            'message': '最终计划没有保持需求分析阶段 flow 的动作顺序',
            'expected_action_ids': expected_ids,
            'actual_action_ids': actual_ids,
        })

    for requirement in required_flow:
        action_id = str(requirement.get('action_id'))
        step = step_by_action.get(action_id)
        if not step:
            issues.append({
                'reason': 'staged_required_action_missing',
                'action_id': action_id,
                'message': f'最终计划缺少必需动作 {action_id}',
            })
            continue
        binding = binding_by_action.get(action_id)
        operation = _plan_step_operation(step)
        if operation in {'goto', 'wait'}:
            continue
        if not binding:
            if action_id in unresolved_ids and _is_unresolved_gap_step(step):
                continue
            issues.append({
                'reason': 'staged_required_binding_missing',
                'action_id': action_id,
                'message': f'必需动作 {action_id} 没有元素绑定，不能纳入最终计划',
            })
            continue
        binding_mode = str(binding.get('binding_mode') or '')
        if binding_mode == 'runtime_text':
            if (
                operation not in {'assert_visible', 'assert_text', 'assert_contain_text'}
                or not str(step.get('value') or binding.get('value') or '').strip()
                or step.get('binding_mode') != 'runtime_text'
            ):
                issues.append({
                    'reason': 'staged_runtime_assertion_contract_mismatch',
                    'action_id': action_id,
                    'message': f'运行时文本断言 {action_id} 未保持只读断言契约',
                })
            continue
        if binding_mode == 'runtime_input':
            if (
                operation != 'fill'
                or step.get('binding_mode') != 'runtime_input'
                or str(step.get('runtime_resolver') or binding.get('runtime_resolver') or '') != 'image_ocr'
            ):
                issues.append({
                    'reason': 'staged_runtime_input_contract_mismatch',
                    'action_id': action_id,
                    'message': f'运行时输入动作 {action_id} 未保持 OCR fill 契约',
                })
        expected_page = str(binding.get('page_key') or '')
        expected_element = str(binding.get('element_key') or '')
        if not _binding_key_matches(step.get('page_key'), step.get('element_key'), expected_page, expected_element):
            issues.append({
                'reason': 'staged_binding_contract_mismatch',
                'action_id': action_id,
                'message': f'最终计划动作 {action_id} 未复用元素绑定阶段的 page_key/element_key',
                'expected_page_key': expected_page,
                'expected_element_key': expected_element,
                'actual_page_key': step.get('page_key') or '',
                'actual_element_key': step.get('element_key') or '',
            })
        expected_operation = str(binding.get('operation') or '')
        if expected_operation and operation != expected_operation:
            issues.append({
                'reason': 'staged_operation_contract_mismatch',
                'action_id': action_id,
                'message': f'最终计划动作 {action_id} 未使用元素绑定阶段解析的具体操作',
                'expected_operation': expected_operation,
                'actual_operation': operation,
            })
    return issues


def _staged_binding_contract_issues(
    requirements: dict[str, Any],
    bindings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate the binding role before asking another role to write the plan."""
    return _contract_staged_binding_contract_issues(requirements, bindings)
    flow = requirements.get('flow') if isinstance(requirements.get('flow'), list) else []
    binding_by_action = {
        str(item.get('action_id')): item
        for item in (bindings.get('bindings') or [])
        if isinstance(item, dict) and item.get('action_id')
    }
    unresolved_ids = _unresolved_action_ids(bindings)
    issues: list[dict[str, Any]] = []
    for requirement in flow:
        if not isinstance(requirement, dict) or not requirement.get('action_id'):
            continue
        if not requirement.get('required', True):
            continue
        action_id = str(requirement['action_id'])
        operation = str(requirement.get('operation') or '')
        if operation in {'goto', 'wait'}:
            continue
        binding = binding_by_action.get(action_id)
        if not binding:
            if action_id in unresolved_ids:
                continue
            issues.append({
                'reason': 'staged_required_binding_missing',
                'action_id': action_id,
                'message': f'必需动作 {action_id} 没有元素绑定',
            })
            continue
        mode = str(binding.get('binding_mode') or 'element')
        if mode == 'runtime_text':
            if operation not in {'assert_visible', 'assert_text', 'assert_contain_text'}:
                issues.append({
                    'reason': 'staged_runtime_binding_not_readonly',
                    'action_id': action_id,
                    'message': f'动作 {action_id} 的 runtime_text 绑定不是只读断言',
                })
            if not str(binding.get('value') or requirement.get('value') or '').strip():
                issues.append({
                    'reason': 'staged_runtime_binding_value_missing',
                    'action_id': action_id,
                    'message': f'运行时文本断言 {action_id} 缺少精确文本值',
                })
            continue
        if mode == 'runtime_input':
            if str(binding.get('operation') or '') != 'fill':
                issues.append({
                    'reason': 'staged_runtime_input_operation_invalid',
                    'action_id': action_id,
                    'message': f'运行时输入动作 {action_id} 必须解析为 fill',
                })
            if str(binding.get('runtime_resolver') or '') != 'image_ocr':
                issues.append({
                    'reason': 'staged_runtime_input_resolver_missing',
                    'action_id': action_id,
                    'message': f'运行时输入动作 {action_id} 缺少 image_ocr 解析器',
                })
        if not str(binding.get('page_key') or '').strip() or not str(binding.get('element_key') or '').strip():
            issues.append({
                'reason': 'staged_element_binding_key_missing',
                'action_id': action_id,
                'message': f'元素绑定 {action_id} 缺少 page_key 或 element_key',
            })
    return issues


def _merge_staged_contract_validation(parsed: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    if not issues:
        return
    validation = parsed.get('plan_validation') if isinstance(parsed.get('plan_validation'), dict) else {}
    semantic_issues = validation.get('semantic_issues') if isinstance(validation.get('semantic_issues'), list) else []
    semantic_issues = [*semantic_issues, *issues]
    validation['status'] = 'failed'
    validation['semantic_issues'] = semantic_issues[:30]
    validation['semantic_issue_count'] = len(semantic_issues)
    parsed['plan_validation'] = validation


def _staged_exploration_coverage_failure(
    task: Any,
    requirements: dict[str, Any],
    route: dict[str, Any],
    bindings: dict[str, Any],
    coverage: dict[str, Any],
    stages: list[dict[str, Any]],
) -> dict[str, Any]:
    missing = coverage.get('missing_required_actions') if isinstance(coverage.get('missing_required_actions'), list) else []
    issue = {
        'reason': 'exploration_coverage_required_binding_unresolved',
        'message': '页面探索未覆盖必需动作的可绑定元素，已停止生成可执行计划',
        'missing_required_actions': missing[:30],
    }
    message = issue['message']
    if missing:
        message = f"{message}: " + '；'.join(
            f"{item.get('action_id') or ''} {item.get('target') or ''}".strip()
            for item in missing[:6]
            if isinstance(item, dict)
        )
    return {
        'llm_enabled': True,
        'failure_category': 'exploration_coverage',
        'error': message,
        'plan_validation': {
            'status': 'failed',
            'total_steps': 0,
            'executable_steps': 0,
            'bound_element_steps': 0,
            'unresolved_step_count': len(missing),
            'risky_step_count': 0,
            'low_confidence_step_count': 0,
            'semantic_issue_count': 1,
            'unresolved_steps': [
                {
                    'action_id': item.get('action_id') or '',
                    'operation': item.get('operation') or '',
                    'target_name': item.get('target') or '',
                    'reason': item.get('reason') or '',
                }
                for item in missing[:20]
                if isinstance(item, dict)
            ],
            'risky_steps': [],
            'low_confidence_steps': [],
            'semantic_issues': [issue],
            'notes': ['绑定阶段已证明当前元素地图缺少必需动作元素，需重新探索页面状态后再规划'],
        },
        'generated_case': {
            'name': task.target_module or task.name,
            'description': task.source_requirement or task.gherkin or '',
            'steps': [],
        },
        'test_plan': {
            'objective': task.source_requirement or task.gherkin or task.name,
            'steps': [],
        },
        'planning_pipeline': {
            'name': 'staged_v1',
            'status': 'blocked',
            'blocked_stage': 'exploration_coverage_gate',
            'failed_stage_error': message,
            'requirement_analysis': requirements,
            'state_route': route,
            'element_bindings': bindings,
            'exploration_coverage': coverage,
            'stages': stages,
        },
    }


class _StagedUiGenerationPlanner:
    def __init__(
        self,
        task: Any,
        payload: dict[str, Any],
        element_map: dict[str, Any],
        active_config: LLMConfig,
        *,
        async_mode: bool,
        request_timeout: int,
        max_retries: int,
        total_budget: int,
    ) -> None:
        self.task = task
        self.payload = payload
        self.element_map = element_map
        self.active_config = active_config
        self.async_mode = async_mode
        self.request_timeout = request_timeout
        self.max_retries = max(1, min(max_retries, 2))
        self.total_budget = max(40, total_budget)
        self.started_at = time.monotonic()
        self.deadline = self.started_at + self.total_budget
        self.stage_metrics: list[dict[str, Any]] = []
        payload_requirement_document = payload.get('requirement_document') if isinstance(payload.get('requirement_document'), dict) else {}
        self.requirement_document = payload_requirement_document or _task_requirement_document(task)
        self.requirement_contract: RequirementContract = build_requirement_contract({}, {}, {})
        self.route_contract = RouteContract.from_result({})
        self.binding_contract = BindingContract.from_result({})
        observed_pages = [
            page for page in (element_map.get('pages') or [])
            if isinstance(page, dict)
        ]
        observed_elements = sum(
            len([element for element in (page.get('elements') or []) if isinstance(element, dict)])
            for page in observed_pages
        )
        self.compact_map = _compact_element_map(
            element_map,
            max_pages=max(1, min(len(observed_pages), 20)),
            max_elements=max(MIN_COMPACT_MAX_ELEMENTS, observed_elements),
            task=task,
        )
        self.planning_map = _focused_planning_element_map(self.compact_map)

    def invoke(self, stage: str, instruction: str, stage_input: dict[str, Any]) -> dict[str, Any]:
        started_at = time.monotonic()
        remaining = self.deadline - started_at
        remaining_core_stages = max(1, 4 - len(self.stage_metrics))
        stage_budget = min(self.request_timeout, max(10, int(remaining / remaining_core_stages)))
        stage_input_chars = len(json.dumps(stage_input, ensure_ascii=False))
        candidate_summary = {}
        candidates = stage_input.get('candidates') if isinstance(stage_input, dict) else None
        if isinstance(candidates, dict):
            candidate_summary = {
                'candidate_mode': candidates.get('mode') or '',
                'candidate_count': candidates.get('candidate_count') or 0,
                'candidate_action_count': len(candidates.get('actions') or []),
                'focus_action_count': len(candidates.get('focus_action_ids') or []),
            }
        if remaining < 10:
            raise TimeoutError(f'{stage} 阶段启动前规划总预算已耗尽')
        logger.info(
            'AI UI 多阶段规划开始: task_id=%s, stage=%s, stage_budget=%ss, total_budget_left=%.1fs, input_chars=%s, candidates=%s',
            getattr(self.task, 'id', None),
            stage,
            stage_budget,
            remaining,
            stage_input_chars,
            candidate_summary,
        )
        try:
            result, metrics = _invoke_planning_stage_json(
                self.active_config,
                stage,
                instruction,
                stage_input,
                request_timeout=min(self.request_timeout, stage_budget),
                max_retries=self.max_retries,
                total_budget=stage_budget,
            )
        except Exception as exc:
            metrics = {
                'stage': stage,
                'status': 'failed',
                'input_chars': stage_input_chars,
                'output_chars': 0,
                'elapsed_seconds': round(time.monotonic() - started_at, 3),
                'error': f'{type(exc).__name__}: {str(exc)[:500]}',
                **candidate_summary,
            }
            self.stage_metrics.append(metrics)
            logger.warning(
                'AI UI 多阶段规划失败: task_id=%s, stage=%s, elapsed=%.1fs, error=%s',
                getattr(self.task, 'id', None),
                stage,
                time.monotonic() - started_at,
                str(exc)[:500],
            )
            raise
        metrics['status'] = 'success'
        metrics['stage_budget_seconds'] = stage_budget
        metrics.update(candidate_summary)
        self.stage_metrics.append(metrics)
        logger.info(
            'AI UI 多阶段规划完成: task_id=%s, stage=%s, elapsed=%.1fs, output_chars=%s',
            getattr(self.task, 'id', None),
            stage,
            metrics.get('elapsed_seconds') or 0,
            metrics.get('output_chars') or 0,
        )
        return result

    def analyze_requirements(self) -> dict[str, Any]:
        explicit_requirements = _extract_explicit_action_requirements(self.task)
        requirements = self.invoke(
            'requirement_analysis',
            (
                '将用户需求解析为完整动作和断言契约。不要读取或猜测页面元素。'
                '每个动作分配稳定 action_id，flow 必须保持用户流程原始先后顺序，并保留登录、导航、'
                '表单数据、提交和结果校验。没有精确期望值的模糊断言标记 required=false 并写入 ambiguities。'
                '用户表达选择某个值但未说明控件类型时使用语义操作 choose，不要提前猜成 select_option 或 check。'
            ),
            {
                'task': {
                    'name': self.task.name,
                    'target_url': self.task.target_url or '',
                    'target_module': self.task.target_module or '',
                },
                'requirement_document': self.requirement_document,
                'parsed_explicit_requirements': explicit_requirements,
                'failure_summary': _planning_failure_summary(self.payload),
                'output_schema': {
                    'goal': '',
                    'preconditions': [],
                    'flow': [{
                        'action_id': 'action_1', 'operation': 'goto|fill|click|choose|assert_visible|assert_text|assert_value',
                        'target': '', 'value': '', 'phase': '', 'expected': '', 'required': True,
                    }],
                    'actions': [{
                        'action_id': 'action_1', 'operation': 'goto|fill|click|choose',
                        'target': '', 'value': '', 'phase': '', 'expected': '', 'required': True,
                    }],
                    'assertions': [{
                        'action_id': 'assert_1', 'operation': 'assert_visible|assert_text|assert_value',
                        'target': '', 'value': '', 'required': True,
                    }],
                    'ambiguities': [],
                },
            },
        )
        completed = _complete_staged_requirement_contract(
            requirements,
            explicit_requirements,
            self.compact_map,
        )
        self.requirement_contract = build_requirement_contract(
            completed,
            explicit_requirements,
            self.compact_map,
        )
        return self.requirement_contract.as_dict()

    def select_route(self, requirements: dict[str, Any]) -> dict[str, Any]:
        state_transitions = [
            {
                **item,
                'transition_key': item.get('transition_key') or item.get('element_key') or '',
            }
            for item in (self.compact_map.get('state_transitions') or [])
            if isinstance(item, dict)
        ]
        route_seed = _deterministic_state_route_selection(
            self.compact_map,
            'route_seed',
            initial_url=self.task.target_url or '',
        )
        stage_input = {
            'initial_url': self.task.target_url or '',
            'requirements': requirements,
            'requirement_contract': self.requirement_contract.as_dict(),
            'requirement_document': self.requirement_document,
            'pages': _planning_page_summaries(self.planning_map),
            'state_transitions': state_transitions,
            'route_seed': route_seed,
            'output_schema': {
                'ordered_page_keys': [],
                'transition_keys': [],
                'route_actions': [],
                'assumptions': [],
            },
        }
        try:
            route_result = self.invoke(
                'state_route_selection',
                (
                    '根据动作契约和已观察页面状态图选择从 fresh browser context 到目标结果的完整路径。'
                    '观察到的登录后页面不是直接入口；只输出页面键和迁移键，不绑定具体字段。'
                ),
                stage_input,
            )
            merged_route = _merge_route_with_seed(route_seed, route_result)
            self.route_contract = RouteContract.from_result(merged_route)
            return merged_route
        except Exception as exc:
            if not _is_recoverable_route_selection_error(exc):
                raise
            fallback_route = _deterministic_state_route_selection(
                self.compact_map,
                f'{type(exc).__name__}: {str(exc)}',
                initial_url=self.task.target_url or '',
            )
            if self.stage_metrics and self.stage_metrics[-1].get('stage') == 'state_route_selection':
                self.stage_metrics[-1].update({
                    'status': 'degraded',
                    'fallback_used': True,
                    'fallback_mode': fallback_route.get('fallback_mode') or 'deterministic_page_order',
                    'fallback_reason': fallback_route.get('fallback_reason') or '',
                    'ordered_page_keys': fallback_route.get('ordered_page_keys') or [],
                    'transition_keys': fallback_route.get('transition_keys') or [],
                })
            logger.warning(
                'AI UI 路由选择阶段降级: task_id=%s, reason=%s',
                getattr(self.task, 'id', None),
                str(exc)[:500],
            )
            self.route_contract = RouteContract.from_result(fallback_route)
            return fallback_route

    def bind_elements(
        self,
        requirements: dict[str, Any],
        route: dict[str, Any] | RouteContract,
        validation_gap: dict[str, Any] | None = None,
        focus_action_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        explicit = self.compact_map.get('explicit_action_requirements') or {}
        route_contract = route if isinstance(route, RouteContract) else RouteContract.from_result(route)
        route_dict = route_contract.as_dict()
        transition_keys = {str(item) for item in route_contract.transition_keys if str(item or '').strip()}
        route_transitions = [
            item for item in (self.compact_map.get('state_transitions') or [])
            if isinstance(item, dict)
            and str(item.get('element_key') or item.get('transition_key') or '') in transition_keys
        ]
        binding_result = self.invoke(
            'element_binding',
            (
                '为每个必需动作选择已观察元素。只能使用 candidates、matched_elements 或 '
                'route_transitions 中存在的 '
                'page_key/element_key/locator；choose 必须根据候选元素 actions 解析为具体的 '
                'select_option、check 或 click。对于提交后才出现且具有精确非空 value 的只读断言，'
                '可输出 binding_mode=runtime_text 和 text locator，不得用于任何写操作。'
                '对于只能通过动作前后页面状态变化证明、预观察地图无法唯一绑定具体控件的状态断言，'
                '可输出 binding_mode=runtime_state、runtime_resolver=state_evidence，'
                '必须提供已观察 page_key 和 state_assertion，不得枚举或猜测控件类型。'
                '当必需动作明确要求填写图片验证码、图形校验码等运行时动态值，候选元素支持 fill、'
                '但需求没有静态 value 时，必须绑定该输入元素，输出 operation=fill、'
                'binding_mode=runtime_input、runtime_resolver=image_ocr，value 保持为空；'
                '不得将此类动作放入 unresolved，也不得生成固定验证码。'
                '不排序步骤、不生成脚本。无法绑定时写入 unresolved。'
                '如果 validation_gap 指出必需结果断言的预观察元素不存在，且该断言有精确非空 value，'
                '必须将其绑定为 binding_mode=runtime_text，并提供 type=text 的 locator_hint；'
                '不得因为元素未在探索快照中出现而丢弃该断言。'
                '当 focus_action_ids 非空时，只重新处理这些动作；previous_bindings 中已有的其他动作必须保持不变。'
            ),
            {
                'requirements': requirements,
                'requirement_contract': self.requirement_contract.as_dict(),
                'requirement_document': self.requirement_document,
                'route': route_dict,
                'validation_gap': validation_gap or {},
                'focus_action_ids': sorted(focus_action_ids or []),
                'matched_elements': explicit.get('matched_elements') or [],
                'candidates': _planning_action_binding_candidates(
                    self.planning_map,
                    route_dict,
                    requirements,
                    focus_action_ids=focus_action_ids,
                ),
                'route_transitions': route_transitions,
                'output_schema': {
                    'bindings': [{
                        'action_id': '', 'operation': '', 'page_key': '', 'element_key': '',
                        'target_name': '', 'value': '',
                        'binding_mode': 'element|runtime_text|runtime_input|runtime_state',
                        'runtime_resolver': 'image_ocr|state_evidence|',
                        'locator_hint': {}, 'row_scope': {}, 'state_assertion': {}, 'confidence': 0.0,
                    }],
                    'unresolved': [{'action_id': '', 'reason': '', 'required': True}],
                },
            },
        )
        self.binding_contract = BindingContract.from_result(binding_result if isinstance(binding_result, dict) else {})
        return binding_result

    def generate_plan(
        self,
        requirements: dict[str, Any],
        route: dict[str, Any] | RouteContract,
        bindings: dict[str, Any] | BindingContract,
        validation_gap: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        route_contract = route if isinstance(route, RouteContract) else RouteContract.from_result(route)
        binding_contract = bindings if isinstance(bindings, BindingContract) else BindingContract.from_result(bindings)
        stage_input = {
            'task': {
                'name': self.task.name,
                'target_url': self.task.target_url or '',
                'target_module': self.task.target_module or '',
            },
            'requirement_document': self.requirement_document,
            'requirements': requirements,
            'requirement_contract': self.requirement_contract.as_dict(),
            'route': route_contract.as_dict(),
            'bindings': binding_contract.as_dict(),
            'safety_policy': self.task.safety_policy or {},
            'failure_summary': _planning_failure_summary(self.payload),
            'validation_gap': validation_gap or {},
            'output_schema': {
                'intent': {'goal': '', 'assumptions': [], 'ambiguities': []},
                'test_plan': {
                    'objective': '', 'preconditions': [], 'test_data': {},
                    'data_lifecycle': {
                        'strategy': 'isolated_generated_values|readonly|seeded_api_data',
                        'seed_actions': [], 'cleanup_actions': [], 'manual_cleanup_notes': [],
                    },
                    'steps': [{
                        'step_sort': 0, 'action_id': '', 'operation': '', 'action': '', 'page_key': '',
                        'element_key': '', 'target_name': '', 'description': '',
                        'locator_hint': {},
                        'binding_mode': 'element|runtime_text|runtime_input|runtime_state',
                        'runtime_resolver': 'image_ocr|state_evidence|', 'value': '', 'expected': '',
                        'state_assertion': {},
                        'risk_level': 'low', 'requires_confirmation': False, 'confidence': 0.8,
                    }],
                    'expected_results': [], 'cleanup': [],
                },
            },
        }
        parsed = self.invoke(
            'executable_plan_generation',
            (
                '仅根据 requirements、route 和 bindings 生成完整有序计划。每个元素步骤必须复用绑定键；'
                '严格保持 requirements.flow 的 action_id 顺序并在每个步骤原样输出 action_id；'
                '从 target_url 开始，不得深链接跳过动作。没有绑定的可选断言不要编造步骤，'
                'runtime_input 绑定必须原样保留 binding_mode、runtime_resolver 和空 value；'
                'runtime_state 绑定必须原样保留 binding_mode、runtime_resolver、page_key 和 state_assertion；'
                '没有绑定的必需动作必须保留为不可执行缺口。输出完整 test_plan，不生成 Playwright 源码。'
            ),
            stage_input,
        )
        return parsed, json.dumps(stage_input, ensure_ascii=False)

    def compile_route(self, parsed: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
        route_contract = route if isinstance(route, RouteContract) else RouteContract.from_result(route)
        generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
        test_plan = parsed.get('test_plan') if isinstance(parsed.get('test_plan'), dict) else {}
        source_steps = generated_case.get('steps') or test_plan.get('steps') or []
        max_steps = max(1, min(int((self.task.safety_policy or {}).get('max_steps') or 24), 50))
        route_compiled_steps = _compile_selected_route_steps(
            [step for step in source_steps if isinstance(step, dict)],
            route_contract.as_dict(),
            self.element_map,
            max_steps,
        )
        compiled_steps = _compile_flow_transition_steps(
            route_compiled_steps,
            self.requirement_contract.as_dict(),
            self.element_map,
            max_steps,
            self.binding_contract,
        )
        compiled_steps = _compile_final_contract_step_order(
            compiled_steps,
            self.requirement_contract.as_dict(),
            max_steps,
        )
        parsed['deterministic_plan_compilation'] = {
            'route_transition_count': len(route_contract.transition_keys),
            'input_step_count': len([step for step in source_steps if isinstance(step, dict)]),
            'compiled_step_count': len(compiled_steps),
            'source': 'requirement_contract_flow_observed_state_transitions_and_final_order',
        }
        parsed['generated_case'] = {**generated_case, 'steps': compiled_steps}
        parsed['test_plan'] = {**test_plan, 'steps': compiled_steps}
        return parsed

    def run(self) -> dict[str, Any]:
        requirements = self.analyze_requirements()
        self.select_route(requirements)
        route_contract = self.route_contract
        bindings = self.bind_elements(requirements, route_contract)
        bindings = _compile_runtime_state_evidence_bindings(
            requirements,
            bindings,
            self.element_map,
            route_contract,
        )
        binding_contract = self.binding_contract
        binding_contract = BindingContract.from_result(bindings)
        self.binding_contract = binding_contract
        binding_issues = _staged_binding_contract_issues(requirements, binding_contract)
        binding_rounds = int((self.task.safety_policy or {}).get('max_repair_rounds') or 2)
        for attempt in range(1, max(0, min(binding_rounds, 1)) + 1):
            if not binding_issues:
                break
            patched_bindings = self.bind_elements(
                requirements,
                route_contract,
                {
                    'attempt': attempt,
                    'issues': binding_issues,
                    'previous_bindings': bindings,
                },
                focus_action_ids=_issue_action_ids(binding_issues),
            )
            bindings = _merge_binding_results(bindings, patched_bindings)
            bindings = _compile_runtime_state_evidence_bindings(
                requirements,
                bindings,
                self.element_map,
                route_contract,
            )
            binding_contract = BindingContract.from_result(bindings)
            self.binding_contract = binding_contract
            binding_issues = _staged_binding_contract_issues(requirements, binding_contract)
        coverage = _contract_exploration_coverage_from_bindings(requirements, binding_contract)
        if not coverage.get('planning_allowed', True):
            return _staged_exploration_coverage_failure(
                self.task,
                requirements,
                route_contract.as_dict(),
                bindings,
                coverage,
                self.stage_metrics,
            )
        parsed, final_input = self.generate_plan(requirements, route_contract, binding_contract)
        parsed = _normalize_llm_plan(self.task, parsed, self.element_map)
        parsed = self.compile_route(parsed, route_contract)
        parsed = _compile_missing_explicit_actions(self.task, parsed, self.element_map, append_missing=False)
        parsed = _standardize_staged_contract_steps(parsed, requirements, binding_contract)
        parsed = self.compile_route(parsed, route_contract)
        parsed = _attach_generated_plan_artifacts(
            self.task,
            parsed,
            self.element_map,
            {
                'pipeline': 'staged_v1',
                'stages': self.stage_metrics,
                'stage_count': len(self.stage_metrics),
                'prompt_chars': sum(item['input_chars'] for item in self.stage_metrics),
                'requirement_contract': self.requirement_contract.as_dict(),
            },
            self.active_config,
            self.started_at,
            final_input,
            self.async_mode,
            self.request_timeout,
            self.max_retries,
            self.total_budget,
        )
        _merge_staged_contract_validation(
            parsed,
            _staged_plan_contract_issues(requirements, binding_contract, parsed),
        )

        best = parsed
        configured_rounds = int((self.task.safety_policy or {}).get('max_repair_rounds') or 2)
        for attempt in range(1, max(0, min(configured_rounds, 1)) + 1):
            validation = best.get('plan_validation') if isinstance(best.get('plan_validation'), dict) else {}
            if validation.get('status') != 'failed':
                break
            contract_issues = _staged_plan_contract_issues(requirements, binding_contract, best)
            semantic_retry_needed = _should_retry_semantic_planning(best, self.compact_map)
            if not validation.get('unresolved_step_count') and not contract_issues and not semantic_retry_needed:
                break
            if validation.get('unresolved_step_count') or contract_issues:
                patched_bindings = self.bind_elements(
                    requirements,
                    route_contract,
                    {
                        'attempt': attempt,
                        'issues': contract_issues,
                        'previous_bindings': bindings,
                    },
                    focus_action_ids=_issue_action_ids(contract_issues),
                )
                bindings = _merge_binding_results(bindings, patched_bindings)
                bindings = _compile_runtime_state_evidence_bindings(
                    requirements,
                    bindings,
                    self.element_map,
                    route_contract,
                )
                binding_contract = BindingContract.from_result(bindings)
                self.binding_contract = binding_contract
            candidate, final_input = self.generate_plan(requirements, route_contract, binding_contract, validation)
            candidate = _normalize_llm_plan(self.task, candidate, self.element_map)
            candidate = self.compile_route(candidate, route_contract)
            candidate = _compile_missing_explicit_actions(self.task, candidate, self.element_map, append_missing=False)
            candidate = _standardize_staged_contract_steps(candidate, requirements, binding_contract)
            candidate = self.compile_route(candidate, route_contract)
            candidate = _attach_generated_plan_artifacts(
                self.task,
                candidate,
                self.element_map,
                {
                    'pipeline': 'staged_v1',
                    'stages': self.stage_metrics,
                    'stage_count': len(self.stage_metrics),
                    'stage_replan_attempt': attempt,
                    'prompt_chars': sum(item['input_chars'] for item in self.stage_metrics),
                },
                self.active_config,
                self.started_at,
                final_input,
                self.async_mode,
                self.request_timeout,
                self.max_retries,
                max(10, int(self.deadline - time.monotonic())),
            )
            _merge_staged_contract_validation(
                candidate,
                _staged_plan_contract_issues(requirements, binding_contract, candidate),
            )
            if _plan_candidate_quality(candidate) > _plan_candidate_quality(best):
                best = candidate
        best['planning_pipeline'] = {
            'name': 'staged_v1',
            'requirement_analysis': requirements,
            'requirement_contract': self.requirement_contract.as_dict(),
            'state_route': route_contract.as_dict(),
            'route_contract': route_contract.as_dict(),
            'element_bindings': bindings,
            'binding_contract': binding_contract.as_dict(),
            'stages': self.stage_metrics,
        }
        return best


def _build_monolithic_llm_ui_generation_plan(task, payload: dict[str, Any]) -> dict[str, Any]:
    """使用当前激活 LLM 配置进行复杂业务意图理解和步骤规划。"""
    element_map = payload.get('element_map') or task.element_map_snapshot or {}
    if not isinstance(element_map, dict):
        element_map = {}

    active_config = LLMConfig.objects.filter(is_active=True).first()
    if not active_config:
        return _fallback_plan(task, element_map, '没有可用的激活 LLM 配置')

    started_at = time.monotonic()
    try:
        async_mode = bool(
            payload.get('async_llm_plan')
            or payload.get('async_mode')
            or payload.get('background')
            or payload.get('background_executor')
        )
        request_timeout, max_retries, total_budget = _llm_plan_limits(task, active_config, async_mode=async_mode)
        prompt_messages, compact_map, llm_metrics = _build_budgeted_generation_prompt(task, payload, element_map)
        prompt_size = llm_metrics['prompt_chars']
        logger.info(
            'AI UI LLM 规划开始: task_id=%s, model=%s, timeout=%ss, retries=%s, total_budget=%ss, prompt_chars=%s/%s, shrink_rounds=%s, pages=%s, elements=%s',
            task.id,
            active_config.name,
            request_timeout,
            max_retries,
            total_budget,
            prompt_size,
            llm_metrics['prompt_budget_chars'],
            llm_metrics['prompt_shrink_rounds'],
            len(compact_map.get('pages') or []),
            sum(len(page.get('elements') or []) for page in compact_map.get('pages') or [] if isinstance(page, dict)),
        )
        response_content = _invoke_openai_compatible_chat(
            active_config,
            prompt_messages,
            temperature=0.1,
            request_timeout=request_timeout,
            max_retries=max_retries,
            total_budget=total_budget,
        )
        logger.info(
            'AI UI LLM 规划返回: task_id=%s, elapsed=%.1fs, response_chars=%s',
            task.id,
            time.monotonic() - started_at,
            len(response_content or ''),
        )
        parsed = extract_json_from_response(response_content)
        if not isinstance(parsed, dict):
            json_retry_content = _invoke_openai_compatible_chat(
                active_config,
                [
                    *prompt_messages,
                    HumanMessage(content=(
                        '上一响应无法解析为 JSON。请重新输出一份完整且严格合法的 JSON，'
                        '必须符合 output_schema 并包含完整 generated_case.steps/test_plan.steps；'
                        '不要使用 Markdown 代码块，不要输出解释文字。'
                    )),
                ],
                temperature=0.05,
                request_timeout=request_timeout,
                max_retries=max(2, min(max_retries, 3)),
                total_budget=total_budget,
            )
            parsed = extract_json_from_response(json_retry_content)
            response_content = json_retry_content
            if not isinstance(parsed, dict):
                return _fallback_plan(task, element_map, 'LLM 未返回合法 JSON')

        parsed = _normalize_llm_plan(task, parsed, element_map)
        generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
        steps = generated_case.get('steps') or parsed.get('test_plan', {}).get('steps') or []
        if not isinstance(steps, list) or not steps:
            format_retry_content = _invoke_openai_compatible_chat(
                active_config,
                [
                    *prompt_messages,
                    HumanMessage(content=(
                        '上一响应缺少 generated_case.steps/test_plan.steps。请重新输出完整 JSON，'
                        '必须包含按执行顺序排列且绑定元素地图的可执行步骤，不要输出解释文字。'
                    )),
                ],
                temperature=0.05,
                request_timeout=request_timeout,
                max_retries=max(2, min(max_retries, 3)),
                total_budget=total_budget,
            )
            format_retry_parsed = extract_json_from_response(format_retry_content)
            if isinstance(format_retry_parsed, dict):
                parsed = _normalize_llm_plan(task, format_retry_parsed, element_map)
                generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
                steps = generated_case.get('steps') or parsed.get('test_plan', {}).get('steps') or []
                response_content = format_retry_content
            if not isinstance(steps, list) or not steps:
                return _fallback_plan(task, element_map, 'LLM 规划缺少可执行步骤')

        parsed = _attach_generated_plan_artifacts(
            task,
            parsed,
            element_map,
            llm_metrics,
            active_config,
            started_at,
            response_content,
            async_mode,
            request_timeout,
            max_retries,
            total_budget,
        )
        initial_validation = parsed.get('plan_validation') if isinstance(parsed.get('plan_validation'), dict) else {}
        semantic_replan_history: list[dict[str, Any]] = []
        best_parsed = parsed
        best_response_content = response_content
        best_quality = _plan_candidate_quality(parsed)
        configured_repair_rounds = int((task.safety_policy or {}).get('max_repair_rounds') or 2)
        max_semantic_replans = max(0, min(configured_repair_rounds, 4))
        for semantic_attempt in range(1, max_semantic_replans + 1):
            if not _should_retry_semantic_planning(parsed, compact_map):
                break
            plan_validation = parsed.get('plan_validation') if isinstance(parsed.get('plan_validation'), dict) else {}
            semantic_replan_history.append({
                'attempt': semantic_attempt - 1,
                'plan_validation': plan_validation,
                'response_chars': len(response_content or ''),
            })
            retry_payload = _semantic_replan_payload(payload, parsed, plan_validation, semantic_attempt)
            retry_messages, retry_compact_map, retry_metrics = _build_budgeted_generation_prompt(
                task,
                retry_payload,
                element_map,
            )
            try:
                retry_content = _invoke_openai_compatible_chat(
                    active_config,
                    retry_messages,
                    temperature=0.05,
                    request_timeout=request_timeout,
                    max_retries=max(2, min(max_retries, 3)),
                    total_budget=total_budget,
                )
            except Exception as retry_exc:
                semantic_replan_history.append({
                    'attempt': semantic_attempt,
                    'status': 'request_failed',
                    'error': f'{type(retry_exc).__name__}: {str(retry_exc)[:500]}',
                })
                break
            retry_parsed = extract_json_from_response(retry_content)
            if not isinstance(retry_parsed, dict):
                semantic_replan_history.append({
                    'attempt': semantic_attempt,
                    'status': 'invalid_json',
                    'response_chars': len(retry_content or ''),
                })
                break
            retry_parsed = _normalize_llm_plan(task, retry_parsed, element_map)
            retry_case = retry_parsed.get('generated_case') if isinstance(retry_parsed.get('generated_case'), dict) else {}
            retry_steps = retry_case.get('steps') or retry_parsed.get('test_plan', {}).get('steps') or []
            if not isinstance(retry_steps, list) or not retry_steps:
                semantic_replan_history.append({
                    'attempt': semantic_attempt,
                    'status': 'missing_steps',
                    'response_chars': len(retry_content or ''),
                })
                break
            retry_parsed = _attach_generated_plan_artifacts(
                task,
                retry_parsed,
                element_map,
                {
                    **retry_metrics,
                    'semantic_validation_retry': True,
                    'semantic_validation_attempt': semantic_attempt,
                    'first_plan_validation': semantic_replan_history[0]['plan_validation'],
                },
                active_config,
                started_at,
                retry_content,
                async_mode,
                request_timeout,
                max_retries,
                total_budget,
            )
            retry_parsed['llm_metrics']['retry_prompt_compaction'] = retry_compact_map.get('compaction') or {}
            retry_quality = _plan_candidate_quality(retry_parsed)
            semantic_replan_history.append({
                'attempt': semantic_attempt,
                'status': 'candidate_evaluated',
                'quality': list(retry_quality),
                'selected': retry_quality > best_quality,
            })
            if retry_quality > best_quality:
                best_parsed = retry_parsed
                best_response_content = retry_content
                best_quality = retry_quality
            parsed = best_parsed
            response_content = best_response_content

        if semantic_replan_history:
            semantic_replan_history.append({
                'attempt': len(semantic_replan_history),
                'status': 'accepted' if parsed.get('plan_validation', {}).get('status') != 'failed' else 'failed',
                'plan_validation': parsed.get('plan_validation') or {},
            })
            parsed.setdefault('llm_metrics', {})['semantic_replan_history'] = semantic_replan_history
        final_validation = parsed.get('plan_validation') if isinstance(parsed.get('plan_validation'), dict) else {}
        return parsed
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        logger.warning("AI UI LLM 步骤规划失败，已降级规则规划: task_id=%s, elapsed=%.1fs, error=%s", task.id, elapsed, str(exc)[:500])
        return _fallback_plan(task, element_map, f'{type(exc).__name__}: {str(exc)}')


def build_llm_ui_generation_plan(task, payload: dict[str, Any]) -> dict[str, Any]:
    """通过职责隔离的多阶段 LLM 流水线生成并校验可执行 UI 测试计划。"""
    element_map = payload.get('element_map') or task.element_map_snapshot or {}
    if not isinstance(element_map, dict):
        element_map = {}
    execution_mode = _normalize_execution_mode(
        payload.get('execution_mode')
        or (task.safety_policy or {}).get('execution_mode')
    ) or _task_execution_mode(task)
    if execution_mode == 'runtime_agent':
        return _build_runtime_agent_plan(task, payload)
    precondition_failure = _planning_precondition_failure(task, element_map)
    if precondition_failure:
        fallback = _fallback_plan(task, element_map, precondition_failure['message'])
        fallback['failure_category'] = precondition_failure['failure_category']
        fallback['plan_validation'] = precondition_failure['plan_validation']
        fallback['planning_pipeline'] = precondition_failure['planning_pipeline']
        return fallback
    active_config = LLMConfig.objects.filter(is_active=True).first()
    if not active_config:
        return _fallback_plan(task, element_map, '没有可用的激活 LLM 配置')

    async_mode = bool(
        payload.get('async_llm_plan')
        or payload.get('async_mode')
        or payload.get('background')
        or payload.get('background_executor')
    )
    request_timeout, max_retries, total_budget = _llm_plan_limits(
        task,
        active_config,
        async_mode=async_mode,
    )
    planner = None
    try:
        planner = _StagedUiGenerationPlanner(
            task,
            payload,
            element_map,
            active_config,
            async_mode=async_mode,
            request_timeout=request_timeout,
            max_retries=max_retries,
            total_budget=total_budget,
        )
        return planner.run()
    except Exception as exc:
        logger.warning(
            'AI UI 多阶段 LLM 规划失败: task_id=%s, error=%s',
            getattr(task, 'id', None),
            str(exc)[:500],
        )
        fallback = _fallback_plan(task, element_map, f'{type(exc).__name__}: {str(exc)}')
        fallback['planning_pipeline'] = {
            'name': 'staged_v1',
            'status': 'failed',
            'failed_stage_error': f'{type(exc).__name__}: {str(exc)}',
            'stages': planner.stage_metrics if planner else [],
        }
        return fallback


def _line_window(text: str, line_number: int, radius: int = 70) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines:
        return {'start_line': 1, 'end_line': 1, 'text': ''}
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    excerpt = '\n'.join(f'{index}: {lines[index - 1]}' for index in range(start, end + 1))
    return {'start_line': start, 'end_line': end, 'text': excerpt}


def _extract_repair_spec_context(current_spec: str, failure: dict[str, Any]) -> dict[str, Any]:
    classification = failure.get('failure_classification') if isinstance(failure.get('failure_classification'), dict) else {}
    line_number = classification.get('line') or failure.get('line')
    try:
        line_number = int(line_number)
    except (TypeError, ValueError):
        line_number = 0

    if line_number > 0:
        excerpt = _line_window(current_spec, line_number)
        return {
            'mode': 'line_window',
            'line': line_number,
            'start_line': excerpt['start_line'],
            'end_line': excerpt['end_line'],
            'text': excerpt['text'],
            'full_spec_chars': len(current_spec),
        }

    if len(current_spec) <= 24000:
        return {
            'mode': 'full',
            'line': None,
            'start_line': 1,
            'end_line': len(current_spec.splitlines()),
            'text': current_spec,
            'full_spec_chars': len(current_spec),
        }

    return {
        'mode': 'head_tail',
        'line': None,
        'start_line': 1,
        'end_line': len(current_spec.splitlines()),
        'text': current_spec[:10000] + '\n\n/* ... WHART_SPEC_TRUNCATED_FOR_REPAIR ... */\n\n' + current_spec[-10000:],
        'full_spec_chars': len(current_spec),
    }


def _compact_typescript_files_for_repair(ts_files: Any) -> dict[str, str]:
    if not isinstance(ts_files, dict):
        return {}
    compact: dict[str, str] = {}
    for name, content in ts_files.items():
        name_text = str(name or '')
        content_text = str(content or '')
        if name_text == 'generated.spec.ts':
            compact[name_text] = '[see current_generated_spec_context]'
        elif len(content_text) <= 12000:
            compact[name_text] = content_text
        else:
            compact[name_text] = content_text[:6000] + '\n/* ... omitted_for_llm_repair ... */\n' + content_text[-3000:]
    return compact


def _apply_llm_patch_operations(current_spec: str, operations: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(operations, list):
        return current_spec, []
    patched = current_spec
    applied: list[dict[str, Any]] = []
    for index, operation in enumerate(operations[:8]):
        if not isinstance(operation, dict):
            continue
        operation_type = str(operation.get('type') or operation.get('operation') or '').strip()
        if operation_type not in {'replace_text', 'replace'}:
            continue
        old = str(operation.get('old') or operation.get('old_text') or '')
        new = str(operation.get('new') or operation.get('new_text') or '')
        if not old or old not in patched:
            applied.append({
                'index': index,
                'status': 'skipped',
                'reason': 'old_text_not_found',
            })
            continue
        patched = patched.replace(old, new, 1)
        applied.append({
            'index': index,
            'status': 'applied',
            'type': operation_type,
            'old_chars': len(old),
            'new_chars': len(new),
        })
    return patched, applied


def build_llm_ui_repair_patch(task, payload: dict[str, Any]) -> dict[str, Any]:
    """基于真实执行失败证据，让 LLM 生成 TypeScript spec 修复 patch。"""
    active_config = LLMConfig.objects.filter(is_active=True).first()
    if not active_config:
        return {
            'llm_enabled': False,
            'llm_error': '未配置启用状态的 LLMConfig',
            'patch_available': False,
        }

    current_spec = str(payload.get('typescript_spec') or '')
    if not current_spec.strip():
        return {
            'llm_enabled': False,
            'llm_error': '缺少待修复 TypeScript spec',
            'patch_available': False,
        }

    element_map = payload.get('element_map') if isinstance(payload.get('element_map'), dict) else {}
    runtime_artifacts = payload.get('runtime_artifacts') if isinstance(payload.get('runtime_artifacts'), dict) else {}
    failure = payload.get('failure') if isinstance(payload.get('failure'), dict) else {}
    failure_reobserve = payload.get('failure_reobserve') if isinstance(payload.get('failure_reobserve'), dict) else {}
    reobserve_state = (
        failure_reobserve.get('page_state')
        if isinstance(failure_reobserve.get('page_state'), dict)
        else {}
    )
    test_plan = payload.get('test_plan') if isinstance(payload.get('test_plan'), dict) else {}
    generated_case = payload.get('generated_case') if isinstance(payload.get('generated_case'), dict) else {}
    spec_context = _extract_repair_spec_context(current_spec, failure)

    compact_context = {
        'task': {
            'id': task.id,
            'name': task.name,
            'target_module': task.target_module,
            'target_url': task.target_url,
            'source_type': task.source_type,
            'requirement': task.source_requirement,
            'gherkin': task.gherkin,
        },
        'failure': {
            'category': failure.get('failure_classification', {}).get('category') if isinstance(failure.get('failure_classification'), dict) else failure.get('failure_category'),
            'returncode': failure.get('returncode'),
            'stderr': str(failure.get('stderr') or '')[-3000:],
            'stdout': str(failure.get('stdout') or '')[-2000:],
            'reason': failure.get('reason') or '',
            'spec_line': spec_context.get('line'),
        },
        'runtime_artifacts': {
            'trace': runtime_artifacts.get('trace') if isinstance(runtime_artifacts.get('trace'), dict) else {},
            'network_summary': runtime_artifacts.get('network_summary') if isinstance(runtime_artifacts.get('network_summary'), dict) else {},
            'counts': runtime_artifacts.get('counts') if isinstance(runtime_artifacts.get('counts'), dict) else {},
        },
        'failure_reobserve': {
            'status': failure_reobserve.get('status') or '',
            'reason': failure_reobserve.get('reason') or '',
            'url': reobserve_state.get('url') or '',
            'title': reobserve_state.get('title') or '',
            'element_count': len(reobserve_state.get('elements') or []),
            'elements': (reobserve_state.get('elements') or [])[:40],
            'replay': failure_reobserve.get('replay') if isinstance(failure_reobserve.get('replay'), dict) else {},
            'accessibility_snapshot': {
                'status': (reobserve_state.get('accessibility_snapshot') or {}).get('status')
                if isinstance(reobserve_state.get('accessibility_snapshot'), dict) else '',
                'node_count': (reobserve_state.get('accessibility_snapshot') or {}).get('node_count')
                if isinstance(reobserve_state.get('accessibility_snapshot'), dict) else 0,
                'nodes': ((reobserve_state.get('accessibility_snapshot') or {}).get('nodes') or [])[:30]
                if isinstance(reobserve_state.get('accessibility_snapshot'), dict) else [],
            },
            'network_summary': reobserve_state.get('network_summary') or {},
        },
        'test_plan': test_plan,
        'generated_steps': generated_case.get('steps') if isinstance(generated_case.get('steps'), list) else [],
        'element_map_summary': {
            'base_url': element_map.get('base_url'),
            'coverage_summary': element_map.get('coverage_summary') or {},
            'low_confidence_items': (element_map.get('low_confidence_items') or [])[:20],
            'risk_items': (element_map.get('risk_items') or [])[:20],
            'locator_baseline': (element_map.get('locator_baseline') or [])[:40],
        },
    }
    messages = [
        SystemMessage(content=(
            '你是资深 Playwright Test 修复 agent。你只能返回 JSON。'
            '目标是在不扩大业务动作风险的前提下，基于失败日志、元素地图、测试计划修复 TypeScript Playwright spec。'
            '优先修复 locator、等待条件、web-first assertion、导入路径、fixture 使用。'
            '如果错误包含 strict mode violation，必须让失败步骤使用唯一可见 locator（例如 first() 或更窄的容器作用域），不能继续对多元素集合直接断言。'
            '如果错误来自自定义 combobox/select，必须基于当前页面证据操作可交互控件并验证唯一选中结果，不要对隐藏 input 直接 click。'
            '如果提供了 failure_reobserve，必须优先结合其中的当前页面元素、accessibility_snapshot 和 URL 判断失败处真实页面状态。'
            '允许对失败步骤附近做局部重规划，但必须保持原测试目标和安全边界不变。'
            '禁止增加删除、支付、审批、提交生产数据等危险动作。'
            '如果证据不足，不要臆造，返回 patch_available=false 和 review_required=true。'
        )),
        HumanMessage(content=json.dumps({
            'output_schema': {
                'patch_available': True,
                'review_required': False,
                'failure_category': 'locator|timeout|assertion|data|permission|environment|llm|unknown',
                'repair_summary': '修复说明',
                'patched_typescript_spec': '完整 generated.spec.ts 内容；无法可靠修复时为空',
                'patched_typescript_files': {
                    'generated.spec.ts': '可选，完整 spec',
                    'whart-generated-page.ts': '可选，仅确需修改 helper 时返回',
                },
                'patch_operations': [
                    {'type': 'replace_text', 'old': '失败附近旧代码片段', 'new': '修复后的代码片段'}
                ],
                'changed_steps': [
                    {'step_sort': 1, 'reason': '修改原因', 'old': '旧片段摘要', 'new': '新片段摘要'}
                ],
                'review_notes': ['需要人工确认的点'],
            },
            'context': compact_context,
            'current_generated_spec_context': spec_context,
            'current_typescript_files': _compact_typescript_files_for_repair(payload.get('typescript_files')),
            'repair_instruction': (
                '优先返回 patch_operations 做局部 replace_text；如果需要返回 patched_typescript_spec，必须是完整 generated.spec.ts。'
                'patch_operations.old 必须逐字来自 current_generated_spec_context.text。'
            ),
        }, ensure_ascii=False)),
    ]

    try:
        request_timeout, max_retries, total_budget = _llm_plan_limits(task, active_config, async_mode=True)
        prompt_chars = _prompt_char_count(messages)
        started_at = time.monotonic()
        logger.info(
            'AI UI LLM 修复开始: task_id=%s, model=%s, timeout=%ss, retries=%s, total_budget=%ss, prompt_chars=%s, spec_context_mode=%s',
            task.id,
            active_config.name,
            request_timeout,
            max_retries,
            total_budget,
            prompt_chars,
            spec_context.get('mode'),
        )
        response_content = _invoke_openai_compatible_chat(
            active_config,
            messages,
            temperature=0.05,
            request_timeout=request_timeout,
            max_retries=max_retries,
            total_budget=total_budget,
        )
        parsed = extract_json_from_response(response_content)
        if not isinstance(parsed, dict):
            raise ValueError('LLM 未返回合法 JSON')
        patched_spec = str(parsed.get('patched_typescript_spec') or '').strip()
        patched_files = parsed.get('patched_typescript_files') if isinstance(parsed.get('patched_typescript_files'), dict) else {}
        if patched_spec and 'generated.spec.ts' not in patched_files:
            patched_files = {**patched_files, 'generated.spec.ts': patched_spec}
        applied_patch_operations: list[dict[str, Any]] = []
        if not patched_spec:
            patched_by_operations, applied_patch_operations = _apply_llm_patch_operations(
                current_spec,
                parsed.get('patch_operations'),
            )
            if any(item.get('status') == 'applied' for item in applied_patch_operations):
                patched_spec = patched_by_operations
                patched_files = {**patched_files, 'generated.spec.ts': patched_spec}
        patch_available = bool(
            (parsed.get('patch_available') or any(item.get('status') == 'applied' for item in applied_patch_operations))
            and (patched_spec or patched_files)
        )
        logger.info(
            'AI UI LLM 修复返回: task_id=%s, elapsed=%.1fs, response_chars=%s, patch_available=%s',
            task.id,
            time.monotonic() - started_at,
            len(response_content or ''),
            patch_available,
        )
        return {
            'llm_enabled': True,
            'llm_config': {
                'id': active_config.id,
                'config_name': active_config.config_name,
                'model': active_config.name,
            },
            'patch_available': patch_available,
            'review_required': bool(parsed.get('review_required')),
            'failure_category': parsed.get('failure_category') or compact_context['failure']['category'] or 'unknown',
            'repair_summary': parsed.get('repair_summary') or '',
            'patched_typescript_spec': patched_spec,
            'patched_typescript_files': patched_files,
            'patch_operations': parsed.get('patch_operations') if isinstance(parsed.get('patch_operations'), list) else [],
            'applied_patch_operations': applied_patch_operations,
            'changed_steps': parsed.get('changed_steps') if isinstance(parsed.get('changed_steps'), list) else [],
            'review_notes': parsed.get('review_notes') if isinstance(parsed.get('review_notes'), list) else [],
            'llm_metrics': {
                'prompt_chars': prompt_chars,
                'elapsed_seconds': round(time.monotonic() - started_at, 3),
                'response_chars': len(response_content or ''),
                'spec_context_mode': spec_context.get('mode'),
                'spec_context_chars': len(spec_context.get('text') or ''),
                'request_timeout_seconds': request_timeout,
                'max_retries': max_retries,
                'total_budget_seconds': total_budget,
            },
        }
    except Exception as exc:
        logger.exception("AI UI LLM 修复 patch 失败")
        return {
            'llm_enabled': False,
            'llm_error': str(exc),
            'patch_available': False,
            'review_required': True,
        }
