# -*- coding: utf-8 -*-
"""元素地图版本治理、差异摘要和过期检测。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


VOLATILE_KEYS = {
    'generated_at',
    'screenshot',
    'review_confirmed_at',
    'confirmed_at',
    'updated_at',
    'created_at',
}


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_value(item)
            for key, item in sorted(value.items())
            if key not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    return value


def stable_hash(value: Any) -> str:
    payload = json.dumps(_stable_value(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _page_key(page: dict[str, Any]) -> str:
    return str(page.get('page_key') or page.get('url') or page.get('title') or '')


def _element_key(element: dict[str, Any]) -> str:
    return str(element.get('element_key') or element.get('mcp_ref') or element.get('name') or element.get('text') or '')


def _locator_fingerprint(element: dict[str, Any]) -> str:
    locator_payload = {
        'recommended_locator': element.get('recommended_locator') if isinstance(element.get('recommended_locator'), dict) else {},
        'locator_candidates': element.get('locator_candidates') if isinstance(element.get('locator_candidates'), list) else [],
        'frame_path': element.get('frame_path') or element.get('iframe_path') or [],
        'shadow_host_path': element.get('shadow_host_path') or element.get('host_path') or [],
    }
    return stable_hash(locator_payload)


def _page_index(map_json: dict[str, Any]) -> dict[str, dict[str, Any]]:
    pages = map_json.get('pages') if isinstance(map_json.get('pages'), list) else []
    return {
        _page_key(page): page
        for page in pages
        if isinstance(page, dict) and _page_key(page)
    }


def _element_index(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    elements = page.get('elements') if isinstance(page.get('elements'), list) else []
    return {
        _element_key(element): element
        for element in elements
        if isinstance(element, dict) and _element_key(element)
    }


def diff_summary(previous_map: dict[str, Any] | None, current_map: dict[str, Any]) -> dict[str, Any]:
    previous = previous_map if isinstance(previous_map, dict) else {}
    current = current_map if isinstance(current_map, dict) else {}
    previous_pages = _page_index(previous)
    current_pages = _page_index(current)
    added_pages = sorted(set(current_pages) - set(previous_pages))
    removed_pages = sorted(set(previous_pages) - set(current_pages))
    changed_pages = []
    added_elements = []
    removed_elements = []
    locator_changed_elements = []

    for page_key in sorted(set(previous_pages) & set(current_pages)):
        old_elements = _element_index(previous_pages[page_key])
        new_elements = _element_index(current_pages[page_key])
        page_added = sorted(set(new_elements) - set(old_elements))
        page_removed = sorted(set(old_elements) - set(new_elements))
        page_locator_changed = []
        for element_key in sorted(set(old_elements) & set(new_elements)):
            if _locator_fingerprint(old_elements[element_key]) != _locator_fingerprint(new_elements[element_key]):
                page_locator_changed.append(element_key)
                locator_changed_elements.append({'page_key': page_key, 'element_key': element_key})
        if page_added or page_removed or page_locator_changed:
            changed_pages.append(page_key)
        added_elements.extend({'page_key': page_key, 'element_key': item} for item in page_added)
        removed_elements.extend({'page_key': page_key, 'element_key': item} for item in page_removed)

    return {
        'previous_hash': stable_hash(previous) if previous else '',
        'current_hash': stable_hash(current) if current else '',
        'page_count_before': len(previous_pages),
        'page_count_after': len(current_pages),
        'added_pages': added_pages[:50],
        'removed_pages': removed_pages[:50],
        'changed_pages': changed_pages[:80],
        'added_element_count': len(added_elements),
        'removed_element_count': len(removed_elements),
        'locator_changed_count': len(locator_changed_elements),
        'added_elements': added_elements[:80],
        'removed_elements': removed_elements[:80],
        'locator_changed_elements': locator_changed_elements[:80],
    }


def extract_scope(environment_config: Any, map_json: dict[str, Any] | None = None) -> dict[str, str]:
    extra = getattr(environment_config, 'extra_config', None) if environment_config is not None else None
    extra = extra if isinstance(extra, dict) else {}
    auth = extra.get('auth') if isinstance(extra.get('auth'), dict) else {}
    login = extra.get('login') if isinstance(extra.get('login'), dict) else {}
    map_scope = extra.get('element_map_scope') if isinstance(extra.get('element_map_scope'), dict) else {}
    map_json = map_json if isinstance(map_json, dict) else {}
    payload_scope = map_json.get('scope') if isinstance(map_json.get('scope'), dict) else {}
    role_key = (
        payload_scope.get('role_key')
        or map_scope.get('role_key')
        or auth.get('role')
        or login.get('role')
        or extra.get('role')
        or 'default'
    )
    permission_key = (
        payload_scope.get('permission_key')
        or map_scope.get('permission_key')
        or auth.get('permission')
        or login.get('permission')
        or extra.get('permission')
        or 'default'
    )
    tenant_key = (
        payload_scope.get('tenant_key')
        or map_scope.get('tenant_key')
        or auth.get('tenant')
        or login.get('tenant')
        or extra.get('tenant')
        or ''
    )
    return {
        'role_key': str(role_key or 'default')[:128],
        'permission_key': str(permission_key or 'default')[:128],
        'tenant_key': str(tenant_key or '')[:128],
    }


def version_group_for(project_id: int, environment_config_id: int | None, base_url: str, role_key: str, permission_key: str) -> str:
    payload = {
        'project': project_id,
        'environment_config': environment_config_id or 0,
        'base_url': base_url or '',
        'role_key': role_key or 'default',
        'permission_key': permission_key or 'default',
    }
    return stable_hash(payload)[:32]


def baseline_hash(map_json: dict[str, Any]) -> str:
    baseline = map_json.get('locator_baseline') if isinstance(map_json.get('locator_baseline'), list) else []
    return stable_hash(baseline)


def annotate_map_json(
    map_json: dict[str, Any],
    *,
    project_id: int,
    environment_config: Any = None,
    base_url: str = '',
    previous_map_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = {**map_json} if isinstance(map_json, dict) else {}
    scope = extract_scope(environment_config, current)
    group = version_group_for(
        project_id,
        getattr(environment_config, 'id', None),
        base_url or current.get('base_url') or '',
        scope['role_key'],
        scope['permission_key'],
    )
    diff = diff_summary(previous_map_json, current)
    current['scope'] = {
        **(current.get('scope') if isinstance(current.get('scope'), dict) else {}),
        **scope,
        'environment_config_id': getattr(environment_config, 'id', None),
        'version_group': group,
    }
    current['governance'] = {
        'map_hash': diff['current_hash'],
        'baseline_hash': baseline_hash(current),
        'version_group': group,
        'diff_summary': diff,
        'stale_status': 'current',
    }
    return current
