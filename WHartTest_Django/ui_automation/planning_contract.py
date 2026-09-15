# -*- coding: utf-8 -*-
"""Deterministic contracts for AI-assisted UI planning.

LLM stages may suggest requirements, bindings, and step descriptions. This
module owns the facts that later stages must not rewrite: action order,
binding keys, unresolved gaps, and operation hand-offs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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

UNRESOLVED_GAP_ACTIONS = {
    'unresolved_required_gap',
    'unresolved_gap',
    'unexecutable_gap',
    'required_action_unresolved',
}


def normalize_text(value: Any) -> str:
    return ' '.join(str(value or '').strip().lower().split())


def _gherkin_table_rows(gherkin: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(str(gherkin or '').splitlines(), start=1):
        stripped = raw_line.strip()
        if not (stripped.startswith('|') and stripped.endswith('|')):
            continue
        cells = [cell.strip() for cell in stripped.strip('|').split('|')]
        if len(cells) < 2:
            continue
        rows.append({
            'line': line_no,
            'cells': cells,
        })
    return rows


def _gherkin_step_lines(gherkin: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(str(gherkin or '').splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        keyword = ''
        for candidate in ('Given', 'When', 'Then', 'And', 'But'):
            if stripped.startswith(candidate + ' '):
                keyword = candidate
                break
        if not keyword:
            continue
        steps.append({
            'line': line_no,
            'keyword': keyword,
            'text': stripped[len(keyword):].strip(),
        })
    return steps


def _requirement_flow_from_gherkin_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flow: list[dict[str, Any]] = []
    last_primary_keyword = ''
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        keyword = str(step.get('keyword') or '').strip()
        text = str(step.get('text') or '').strip()
        if not text:
            continue
        effective_keyword = keyword
        if keyword in {'And', 'But'} and last_primary_keyword:
            effective_keyword = last_primary_keyword
        elif keyword not in {'And', 'But'}:
            last_primary_keyword = keyword
        is_assertion = effective_keyword == 'Then'
        flow.append({
            'action_id': f'gherkin_step_{index}',
            'operation': 'assert_visible' if is_assertion else '',
            'target': text,
            'expected': text if is_assertion else '',
            'phase': keyword,
            'source': 'gherkin_step',
            'source_line': step.get('line'),
            'required': True,
        })
    return flow


def _requirement_flow_from_natural_language(source_requirement: str) -> list[dict[str, Any]]:
    flow: list[dict[str, Any]] = []
    for index, raw_line in enumerate(str(source_requirement or '').splitlines(), start=1):
        text = raw_line.strip()
        if not text:
            continue
        flow.append({
            'action_id': f'natural_step_{index}',
            'operation': '',
            'target': text,
            'expected': '',
            'phase': 'natural_language',
            'source': 'natural_language_line',
            'source_line': index,
            'required': True,
        })
    if not flow and str(source_requirement or '').strip():
        flow.append({
            'action_id': 'natural_step_1',
            'operation': '',
            'target': str(source_requirement or '').strip(),
            'expected': '',
            'phase': 'natural_language',
            'source': 'natural_language_text',
            'source_line': 1,
            'required': True,
        })
    return flow


def build_requirement_document(task: Any) -> dict[str, Any]:
    source_requirement = str(getattr(task, 'source_requirement', '') or '')
    gherkin = str(getattr(task, 'gherkin', '') or '')
    task_name = str(getattr(task, 'name', '') or '')
    target_module = str(getattr(task, 'target_module', '') or '')
    target_url = str(getattr(task, 'target_url', '') or '')
    sources = []
    if source_requirement.strip():
        sources.append({
            'kind': 'natural_language',
            'text': source_requirement,
        })
    gherkin_steps: list[dict[str, Any]] = []
    if gherkin.strip():
        gherkin_steps = _gherkin_step_lines(gherkin)
        sources.append({
            'kind': 'gherkin',
            'text': gherkin,
            'tables': _gherkin_table_rows(gherkin),
            'steps': gherkin_steps,
        })
    merged_text = '\n'.join(item['text'] for item in sources if item.get('text'))
    flow = _requirement_flow_from_gherkin_steps(gherkin_steps)
    if not flow:
        flow = _requirement_flow_from_natural_language(source_requirement)
    return {
        'task_name': task_name,
        'target_module': target_module,
        'target_url': target_url,
        'source_requirement': source_requirement,
        'gherkin': gherkin,
        'has_natural_language': bool(source_requirement.strip()),
        'has_gherkin': bool(gherkin.strip()),
        'normalized_text': normalize_text(merged_text),
        'sources': sources,
        'flow': flow,
    }


def plan_step_operation(step: dict[str, Any]) -> str:
    operation = str(step.get('operation') or step.get('action') or '').strip().lower()
    return OPERATION_ALIASES.get(operation, operation)


def is_assertion_operation(operation: str) -> bool:
    return str(operation or '').strip().lower() in {
        'assert_visible',
        'assert_text',
        'assert_contain_text',
        'assert_value',
    }


def is_unresolved_gap_step(step: dict[str, Any]) -> bool:
    action = str(step.get('action') or '').strip().lower()
    if action in UNRESOLVED_GAP_ACTIONS:
        return True
    if action.startswith('unresolved_') and action.endswith('_gap'):
        return True
    if action.startswith('unexecutable_') and action.endswith('_gap'):
        return True
    return False


def element_key_variants(page_key: str, element_key: str) -> list[str]:
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


def binding_key_matches(actual_page: Any, actual_element: Any, expected_page: Any, expected_element: Any) -> bool:
    actual_page_text = str(actual_page or '').strip()
    expected_page_text = str(expected_page or '').strip()
    if actual_page_text != expected_page_text:
        return False
    actual_variants = set(element_key_variants(actual_page_text, str(actual_element or '')))
    expected_variants = set(element_key_variants(expected_page_text, str(expected_element or '')))
    return bool(actual_variants and expected_variants and actual_variants.intersection(expected_variants))


def renumber_plan_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index, step in enumerate(steps):
        if isinstance(step, dict):
            step['step_sort'] = index
    return steps


@dataclass(frozen=True)
class RequirementContract:
    """Stable action contract handed from requirement analysis to planners."""

    raw: dict[str, Any]
    flow: list[dict[str, Any]]
    action_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {**self.raw, 'flow': [dict(item) for item in self.flow]}


@dataclass(frozen=True)
class RouteContract:
    """Stable route hand-off from route selection to binding and plan compilation."""

    ordered_page_keys: tuple[str, ...]
    transition_keys: tuple[str, ...]
    route_actions: tuple[dict[str, Any], ...] = ()
    assumptions: tuple[str, ...] = ()
    fallback_mode: str = ''
    fallback_reason: str = ''
    seed: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> 'RouteContract':
        if isinstance(result, cls):
            return result
        route_seed = result.get('route_seed') if isinstance(result.get('route_seed'), dict) else {}
        return cls(
            ordered_page_keys=tuple(
                str(item or '')
                for item in (result.get('ordered_page_keys') or route_seed.get('ordered_page_keys') or [])
                if str(item or '').strip()
            ),
            transition_keys=tuple(
                str(item or '')
                for item in (result.get('transition_keys') or route_seed.get('transition_keys') or [])
                if str(item or '').strip()
            ),
            route_actions=tuple(
                dict(item) for item in (result.get('route_actions') or route_seed.get('route_actions') or [])
                if isinstance(item, dict)
            ),
            assumptions=tuple(
                str(item or '')
                for item in (result.get('assumptions') or route_seed.get('assumptions') or [])
                if str(item or '').strip()
            ),
            fallback_mode=str(result.get('fallback_mode') or route_seed.get('fallback_mode') or ''),
            fallback_reason=str(result.get('fallback_reason') or route_seed.get('fallback_reason') or ''),
            seed=route_seed,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            'ordered_page_keys': list(self.ordered_page_keys),
            'page_keys': list(self.ordered_page_keys),
            'transition_keys': list(self.transition_keys),
            'route_actions': [dict(item) for item in self.route_actions],
            'assumptions': list(self.assumptions),
            'fallback_mode': self.fallback_mode,
            'fallback_reason': self.fallback_reason,
            'route_seed': dict(self.seed),
        }


@dataclass(frozen=True)
class BindingContract:
    """Stable element hand-off from binding to plan compilation."""

    bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    unresolved: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> 'BindingContract':
        if isinstance(result, cls):
            return result
        bindings = {
            str(item.get('action_id') or ''): item
            for item in (result.get('bindings') or [])
            if isinstance(item, dict) and item.get('action_id')
        }
        unresolved = {
            str(item.get('action_id') or ''): item
            for item in (result.get('unresolved') or [])
            if isinstance(item, dict) and item.get('action_id') and item.get('required', True)
        }
        return cls(bindings=bindings, unresolved=unresolved)

    def as_dict(self) -> dict[str, Any]:
        return {
            'bindings': [dict(item) for item in self.bindings.values()],
            'unresolved': [dict(item) for item in self.unresolved.values()],
        }


def requirement_contract_text(requirement: dict[str, Any]) -> str:
    return normalize_text(' '.join(str(requirement.get(key) or '') for key in [
        'target', 'name', 'target_name', 'value', 'expected', 'phase',
    ]))


def requirement_contract_identity(requirement: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(requirement.get('operation') or '').strip().lower(),
        normalize_text(requirement.get('target') or requirement.get('name') or requirement.get('target_name') or ''),
        normalize_text(requirement.get('value') or requirement.get('expected') or ''),
    )


def requirement_contract_covers(existing: dict[str, Any], wanted: dict[str, Any]) -> bool:
    existing_identity = requirement_contract_identity(existing)
    wanted_identity = requirement_contract_identity(wanted)
    if existing_identity == wanted_identity:
        return True
    existing_operation, existing_name, _existing_value = existing_identity
    wanted_operation, wanted_name, wanted_value = wanted_identity
    if wanted_operation and existing_operation and wanted_operation != existing_operation:
        return False
    existing_text = requirement_contract_text(existing)
    wanted_text = requirement_contract_text(wanted)
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


def next_requirement_action_id(existing_ids: set[str], operation: str) -> str:
    prefix = 'explicit_assert' if operation.startswith('assert_') else 'explicit_action'
    index = 1
    while f'{prefix}_{index}' in existing_ids:
        index += 1
    action_id = f'{prefix}_{index}'
    existing_ids.add(action_id)
    return action_id


def build_requirement_contract(
    requirements: dict[str, Any],
    explicit_requirements: dict[str, Any] | None = None,
    compact_map: dict[str, Any] | None = None,
) -> RequirementContract:
    if not isinstance(requirements, dict):
        requirements = {}
    explicit_requirements = explicit_requirements if isinstance(explicit_requirements, dict) else {}
    compact_map = compact_map if isinstance(compact_map, dict) else {}
    completed = {**requirements}
    flow = [
        dict(item)
        for item in (requirements.get('flow') if isinstance(requirements.get('flow'), list) else [])
        if isinstance(item, dict)
    ]
    actions = [
        dict(item)
        for item in (requirements.get('actions') if isinstance(requirements.get('actions'), list) else [])
        if isinstance(item, dict)
    ]
    assertions = [
        dict(item)
        for item in (requirements.get('assertions') if isinstance(requirements.get('assertions'), list) else [])
        if isinstance(item, dict)
    ]
    if not flow:
        flow = [*actions, *assertions]

    existing_ids = {
        str(item.get('action_id') or '')
        for item in [*flow, *actions, *assertions]
        if isinstance(item, dict) and item.get('action_id')
    }
    covered_contracts = [
        item for item in [*flow, *actions, *assertions]
        if isinstance(item, dict)
    ]

    for item in [*actions, *assertions]:
        action_id = str(item.get('action_id') or '').strip()
        if action_id and any(str(existing.get('action_id') or '') == action_id for existing in flow):
            continue
        if not action_id and any(requirement_contract_covers(existing, item) for existing in flow):
            continue
        flow.append(dict(item))
        if action_id:
            existing_ids.add(action_id)
        covered_contracts.append(item)

    matched_elements = (
        (compact_map.get('explicit_action_requirements') or {}).get('matched_elements')
        if isinstance(compact_map.get('explicit_action_requirements'), dict)
        else []
    )
    for match in matched_elements or []:
        if not isinstance(match, dict) or not isinstance(match.get('requirement'), dict):
            continue
        requirement = match['requirement']
        operation = str(requirement.get('operation') or '').strip().lower()
        if not operation:
            continue
        candidate = {
            'action_id': '',
            'operation': operation,
            'target': requirement.get('name') or match.get('name') or '',
            'value': requirement.get('value') or match.get('suggested_value') or '',
            'phase': requirement.get('phase') or 'explicit_requirement',
            'expected': requirement.get('expected') or '',
            'required': requirement.get('required', True),
            'source': requirement.get('source') or 'explicit_matched_element',
            'matched_page_key': match.get('page_key') or '',
            'matched_element_key': match.get('element_key') or '',
        }
        if any(requirement_contract_covers(existing, candidate) for existing in covered_contracts):
            continue
        candidate['action_id'] = next_requirement_action_id(existing_ids, operation)
        flow.append(candidate)
        covered_contracts.append(candidate)

    completed['flow'] = flow
    return RequirementContract(
        raw=completed,
        flow=flow,
        action_ids=tuple(
            str(item.get('action_id') or '')
            for item in flow
            if isinstance(item, dict) and item.get('action_id')
        ),
    )


def complete_requirement_contract(
    requirements: dict[str, Any],
    explicit_requirements: dict[str, Any] | None = None,
    compact_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_requirement_contract(requirements, explicit_requirements, compact_map).as_dict()


def _gap_step(step: dict[str, Any], unresolved: dict[str, Any]) -> dict[str, Any]:
    return {
        **step,
        'action': 'unresolved_required_gap',
        'page_key': '',
        'element_key': '',
        'locator_hint': {},
        'locator_candidates': [],
        'binding_mode': 'unresolved',
        'runtime_resolver': '',
        'state_transition': None,
        'requires_confirmation': False,
        'risk_level': 'low',
        'confidence': 0.0,
        'unresolved_reason': unresolved.get('reason') or step.get('unresolved_reason') or step.get('description') or 'required_action_unresolved',
        'description': step.get('description') or unresolved.get('reason') or '必需动作缺少元素绑定，保留不可执行缺口。',
    }


def standardize_unresolved_gap_steps(
    parsed: dict[str, Any],
    bindings: dict[str, Any] | BindingContract,
) -> dict[str, Any]:
    binding_contract = BindingContract.from_result(bindings)
    if not binding_contract.unresolved:
        return parsed

    generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
    test_plan = parsed.get('test_plan') if isinstance(parsed.get('test_plan'), dict) else {}
    source_steps = generated_case.get('steps') or test_plan.get('steps') or []
    normalized = []
    for step in source_steps or []:
        if not isinstance(step, dict):
            continue
        action_id = str(step.get('action_id') or '')
        unresolved = binding_contract.unresolved.get(action_id)
        if not unresolved or plan_step_operation(step) in {'goto', 'wait'}:
            normalized.append(step)
        else:
            normalized.append(_gap_step(step, unresolved))
    normalized = renumber_plan_steps(normalized)
    parsed['generated_case'] = {**generated_case, 'steps': normalized}
    parsed['test_plan'] = {**test_plan, 'steps': normalized}
    return parsed


def standardize_staged_contract_steps(
    parsed: dict[str, Any],
    requirements: dict[str, Any],
    bindings: dict[str, Any] | BindingContract,
) -> dict[str, Any]:
    binding_contract = BindingContract.from_result(bindings)
    requirement_contract = build_requirement_contract(requirements if isinstance(requirements, dict) else {})
    flow_order = {
        action_id: index
        for index, action_id in enumerate(requirement_contract.action_ids)
        if action_id
    }
    if not binding_contract.bindings and not binding_contract.unresolved and not flow_order:
        return parsed

    generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
    test_plan = parsed.get('test_plan') if isinstance(parsed.get('test_plan'), dict) else {}
    source_steps = generated_case.get('steps') or test_plan.get('steps') or []
    normalized_steps: list[dict[str, Any]] = []
    for step in source_steps or []:
        if not isinstance(step, dict):
            continue
        action_id = str(step.get('action_id') or '')
        unresolved = binding_contract.unresolved.get(action_id)
        if unresolved and plan_step_operation(step) not in {'goto', 'wait'}:
            normalized_steps.append(_gap_step(step, unresolved))
            continue

        binding = binding_contract.bindings.get(action_id)
        if not binding or plan_step_operation(step) in {'goto', 'wait'}:
            normalized_steps.append(step)
            continue

        binding_mode = str(binding.get('binding_mode') or step.get('binding_mode') or 'element')
        operation = str(binding.get('operation') or step.get('operation') or '').strip().lower()
        patched_step = {
            **step,
            'operation': operation or step.get('operation') or '',
            'binding_mode': binding_mode,
            'runtime_resolver': binding.get('runtime_resolver') or step.get('runtime_resolver') or '',
            'target_name': binding.get('target_name') or step.get('target_name') or '',
            'value': step.get('value') if str(step.get('value') or '').strip() else binding.get('value') or '',
            'requires_confirmation': False if binding_mode in {'runtime_text', 'runtime_input'} else step.get('requires_confirmation', False),
        }
        if binding_mode == 'runtime_text':
            patched_step.update({
                'page_key': '',
                'element_key': '',
                'locator_hint': binding.get('locator_hint') if isinstance(binding.get('locator_hint'), dict) else patched_step.get('locator_hint') or {},
                'locator_candidates': [],
                'state_transition': None,
            })
        elif binding_mode == 'runtime_state':
            patched_step.update({
                'page_key': binding.get('page_key') or step.get('page_key') or '',
                'element_key': '',
                'locator_hint': {},
                'locator_candidates': [],
                'state_transition': None,
                'state_assertion': (
                    binding.get('state_assertion')
                    if isinstance(binding.get('state_assertion'), dict)
                    else step.get('state_assertion')
                    if isinstance(step.get('state_assertion'), dict)
                    else {}
                ),
            })
        else:
            patched_step.update({
                'page_key': binding.get('page_key') or step.get('page_key') or '',
                'element_key': binding.get('element_key') or step.get('element_key') or '',
                'locator_hint': binding.get('locator_hint') if isinstance(binding.get('locator_hint'), dict) else patched_step.get('locator_hint') or {},
            })
            if plan_step_operation(patched_step) != 'click':
                patched_step.pop('state_transition', None)
        normalized_steps.append(patched_step)

    def order_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        index, step = item
        action_id = str(step.get('action_id') or '')
        if plan_step_operation(step) == 'goto' and action_id not in flow_order:
            return (0, index, index)
        if action_id in flow_order:
            return (1, flow_order[action_id], index)
        return (2, index, index)

    normalized_steps = [
        step for _, step in sorted(enumerate(normalized_steps), key=order_key)
    ]
    normalized_steps = renumber_plan_steps(normalized_steps)
    parsed['generated_case'] = {**generated_case, 'steps': normalized_steps}
    parsed['test_plan'] = {**test_plan, 'steps': normalized_steps}
    return parsed


def staged_plan_contract_issues(
    requirements: dict[str, Any],
    bindings: dict[str, Any] | BindingContract,
    parsed: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate the staged role hand-off after plan compilation."""
    requirement_contract = build_requirement_contract(requirements if isinstance(requirements, dict) else {})
    binding_contract = BindingContract.from_result(bindings)
    required_flow = [
        item for item in requirement_contract.flow
        if isinstance(item, dict) and item.get('action_id') and item.get('required', True)
    ]
    test_plan = parsed.get('test_plan') if isinstance(parsed.get('test_plan'), dict) else {}
    generated_case = parsed.get('generated_case') if isinstance(parsed.get('generated_case'), dict) else {}
    steps = generated_case.get('steps') or test_plan.get('steps') or []
    step_by_action: dict[str, dict[str, Any]] = {}
    for item in steps:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get('action_id') or '')
        if action_id:
            step_by_action[action_id] = item
        for covered_action_id in item.get('covered_action_ids') or []:
            covered_action_id = str(covered_action_id or '')
            if covered_action_id and covered_action_id not in step_by_action:
                step_by_action[covered_action_id] = item
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
        binding = binding_contract.bindings.get(action_id)
        operation = plan_step_operation(step)
        if operation in {'goto', 'wait'}:
            continue
        if not binding:
            if action_id in binding_contract.unresolved and is_unresolved_gap_step(step):
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
            continue
        if binding_mode == 'runtime_state':
            if (
                not is_assertion_operation(operation)
                or step.get('binding_mode') != 'runtime_state'
                or not isinstance(step.get('state_assertion'), dict)
            ):
                issues.append({
                    'reason': 'staged_runtime_state_contract_mismatch',
                    'action_id': action_id,
                    'message': f'运行时状态断言 {action_id} 未保持两阶段证据契约',
                })
            continue
        expected_page = str(binding.get('page_key') or '')
        expected_element = str(binding.get('element_key') or '')
        if not binding_key_matches(step.get('page_key'), step.get('element_key'), expected_page, expected_element):
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


def staged_binding_contract_issues(
    requirements: dict[str, Any],
    bindings: dict[str, Any] | BindingContract,
) -> list[dict[str, Any]]:
    """Validate the binding role before plan generation."""
    requirement_contract = build_requirement_contract(requirements if isinstance(requirements, dict) else {})
    binding_contract = BindingContract.from_result(bindings)
    issues: list[dict[str, Any]] = []
    for requirement in requirement_contract.flow:
        if not isinstance(requirement, dict) or not requirement.get('action_id'):
            continue
        if not requirement.get('required', True):
            continue
        action_id = str(requirement['action_id'])
        operation = str(requirement.get('operation') or '')
        if operation in {'goto', 'wait'}:
            continue
        binding = binding_contract.bindings.get(action_id)
        if not binding:
            if action_id in binding_contract.unresolved:
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
            continue
        if mode == 'runtime_state':
            if not is_assertion_operation(operation):
                issues.append({
                    'reason': 'staged_runtime_state_binding_not_assertion',
                    'action_id': action_id,
                    'message': f'运行时状态绑定 {action_id} 只能用于断言动作',
                })
            if str(binding.get('runtime_resolver') or '') != 'state_evidence':
                issues.append({
                    'reason': 'staged_runtime_state_resolver_missing',
                    'action_id': action_id,
                    'message': f'运行时状态断言 {action_id} 缺少 state_evidence 解析器',
                })
            if not str(binding.get('page_key') or '').strip():
                issues.append({
                    'reason': 'staged_runtime_state_page_scope_missing',
                    'action_id': action_id,
                    'message': f'运行时状态断言 {action_id} 缺少页面证据作用域',
                })
            if not isinstance(binding.get('state_assertion'), dict):
                issues.append({
                    'reason': 'staged_runtime_state_assertion_missing',
                    'action_id': action_id,
                    'message': f'运行时状态断言 {action_id} 缺少 state_assertion 合同',
                })
            continue
        if not str(binding.get('page_key') or '').strip() or not str(binding.get('element_key') or '').strip():
            issues.append({
                'reason': 'staged_element_binding_key_missing',
                'action_id': action_id,
                'message': f'元素绑定 {action_id} 缺少 page_key 或 element_key',
            })
    return issues


def exploration_coverage_from_bindings(
    requirements: dict[str, Any],
    bindings: dict[str, Any] | BindingContract,
) -> dict[str, Any]:
    """Report required actions that binding proved impossible from observed data."""
    requirement_contract = build_requirement_contract(requirements if isinstance(requirements, dict) else {})
    binding_contract = BindingContract.from_result(bindings)
    by_action = {
        str(item.get('action_id') or ''): item
        for item in requirement_contract.flow
        if isinstance(item, dict) and item.get('action_id')
    }
    missing = []
    for action_id, unresolved in binding_contract.unresolved.items():
        requirement = by_action.get(action_id) or {}
        operation = str(requirement.get('operation') or '').strip().lower()
        if operation in {'goto', 'wait'}:
            continue
        if not requirement.get('required', True):
            continue
        missing.append({
            'action_id': action_id,
            'operation': operation,
            'target': requirement.get('target') or requirement.get('name') or requirement.get('target_name') or '',
            'value': requirement.get('value') or '',
            'phase': requirement.get('phase') or '',
            'reason': unresolved.get('reason') or 'required_action_unresolved',
            'required': True,
        })
    return {
        'planning_allowed': not missing,
        'reason': 'required_binding_unresolved' if missing else '',
        'missing_required_actions': missing,
        'missing_action_count': len(missing),
    }
