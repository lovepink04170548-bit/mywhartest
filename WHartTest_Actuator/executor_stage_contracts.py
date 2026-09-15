"""Stage contracts for the Playwright executor.

This module owns the hand-off from task text/contracts to executor stages.
Callers should consume the returned contracts instead of re-parsing the same
text for different stages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


ROUTE_SEPARATOR_RE = re.compile(r'(?:->|→|—>|＞|>|\||/|／)')
BDD_LINE_RE = re.compile(r'^(given|when|then|and|but|background|scenario|feature)\b[:：]?\s*(.*)$', re.IGNORECASE)
QUOTED_LABEL_RE = re.compile(r'[“"\']([^”"\']{1,80})[”"\']')


def clean_visible_text(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def structured_dict(container: Any, key: str) -> dict[str, Any]:
    if not isinstance(container, dict):
        return {}
    value = container.get(key)
    return value if isinstance(value, dict) else {}


def structured_requirement_document(args: Any) -> dict[str, Any]:
    return structured_dict(args, 'requirement_document')


def structured_authentication_contract(args: Any) -> dict[str, Any]:
    contract = structured_dict(args, 'authentication_contract')
    if contract:
        return contract
    safety_policy = args.get('safety_policy') if isinstance(args, dict) and isinstance(args.get('safety_policy'), dict) else {}
    return structured_dict(safety_policy, 'authentication_contract')


def requirement_lines(source: Any) -> list[str]:
    if not isinstance(source, dict):
        return str(source or '').splitlines()

    document = structured_requirement_document(source)
    lines: list[str] = []
    for key in ['source_requirement', 'gherkin', 'normalized_text']:
        value = document.get(key)
        if value:
            lines.extend(str(value).splitlines())
    for item in document.get('sources') or []:
        if not isinstance(item, dict):
            continue
        for step in item.get('steps') or []:
            if isinstance(step, dict):
                lines.append(clean_visible_text(step.get('text')))
        if item.get('text'):
            lines.extend(str(item.get('text') or '').splitlines())
    if not lines:
        for key in ['requirement', 'gherkin', 'target_module']:
            value = source.get(key)
            if value:
                lines.extend(str(value).splitlines())
    return lines


def _line_content(raw_line: Any) -> tuple[str, str]:
    line = clean_visible_text(raw_line)
    if not line or line.startswith('@') or line.startswith('#') or line.startswith('//'):
        return '', ''
    bdd_match = BDD_LINE_RE.match(line)
    bdd_keyword = bdd_match.group(1).lower() if bdd_match else ''
    content = clean_visible_text(bdd_match.group(2) if bdd_match else line)
    if bdd_keyword in {'feature', 'scenario', 'background'}:
        return bdd_keyword, ''
    return bdd_keyword, content


@dataclass(frozen=True)
class RouteTarget:
    label: str
    opens_form: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            'label': self.label,
            'opens_form': self.opens_form,
        }


@dataclass(frozen=True)
class ExecutionStageContract:
    authentication_entry_labels: tuple[str, ...] = ()
    business_route: tuple[RouteTarget, ...] = ()
    flow_actions: tuple[dict[str, Any], ...] = ()
    source: str = 'execution_stage_contract'
    assumptions: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            'authentication_entry_labels': list(self.authentication_entry_labels),
            'business_route': [item.as_dict() for item in self.business_route],
            'flow_actions': [dict(item) for item in self.flow_actions],
            'source': self.source,
            'assumptions': list(self.assumptions),
        }


def extract_business_route(source: Any) -> list[RouteTarget]:
    route: list[RouteTarget] = []
    seen: set[str] = set()
    for raw_line in requirement_lines(source):
        bdd_keyword, content = _line_content(raw_line)
        if not content or bdd_keyword == 'then':
            continue
        if not ROUTE_SEPARATOR_RE.search(content):
            continue

        labels = [clean_visible_text(match) for match in QUOTED_LABEL_RE.findall(content)]
        if not labels:
            labels = [
                clean_visible_text(item)
                for item in ROUTE_SEPARATOR_RE.split(content)
                if clean_visible_text(item)
            ]
        for label in labels:
            if not label or label in seen:
                continue
            seen.add(label)
            route.append(RouteTarget(label=label))
    return route


def extract_authentication_entry_labels(args: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        label = clean_visible_text(value)
        if not label or label in seen:
            return
        seen.add(label)
        labels.append(label)

    contract = structured_authentication_contract(args)
    for key in ['entry_action', 'entry_target', 'target', 'action_label']:
        value = contract.get(key)
        if isinstance(value, str):
            add(value)
        elif isinstance(value, dict):
            for nested_key in ['label', 'target', 'name']:
                add(value.get(nested_key))
    for item in contract.get('entry_actions') or contract.get('actions') or []:
        if isinstance(item, dict):
            for key in ['label', 'target', 'name']:
                add(item.get(key))

    for raw_line in requirement_lines(args):
        bdd_keyword, content = _line_content(raw_line)
        if not content or bdd_keyword == 'then':
            continue
        if ROUTE_SEPARATOR_RE.search(content):
            break
        for label in QUOTED_LABEL_RE.findall(content):
            add(label)

    return labels[:5]


def structured_requirement_contract(args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    contract = structured_dict(args, 'requirement_contract')
    if contract:
        return contract
    safety_policy = args.get('safety_policy') if isinstance(args.get('safety_policy'), dict) else {}
    return structured_dict(safety_policy, 'requirement_contract')


def extract_flow_actions(args: dict[str, Any]) -> list[dict[str, Any]]:
    contract = structured_requirement_contract(args)
    flow = contract.get('flow') if isinstance(contract.get('flow'), list) else []
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in flow:
        if not isinstance(item, dict) or item.get('required') is False:
            continue
        operation = clean_visible_text(item.get('operation')).lower()
        target = clean_visible_text(item.get('target') or item.get('name') or item.get('target_name'))
        value = clean_visible_text(item.get('value'))
        expected = clean_visible_text(item.get('expected'))
        phase = clean_visible_text(item.get('phase'))
        action_id = clean_visible_text(item.get('action_id'))
        if not operation or not any([target, value, expected]):
            continue
        key = (action_id, operation, target, value or expected)
        if key in seen:
            continue
        seen.add(key)
        actions.append({
            'action_id': action_id,
            'operation': operation,
            'target': target,
            'value': value,
            'expected': expected,
            'phase': phase,
        })
    return actions


def build_execution_stage_contract(args: Any) -> ExecutionStageContract:
    source = args if isinstance(args, dict) else {'requirement': args}
    return ExecutionStageContract(
        authentication_entry_labels=tuple(extract_authentication_entry_labels(source)),
        business_route=tuple(extract_business_route(source)),
        flow_actions=tuple(extract_flow_actions(source)),
    )
