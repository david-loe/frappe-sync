from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from sync.sync.constants import (
	FRAPPE_SOURCE_MODE_DOCTYPE_QUERY,
	FRAPPE_SOURCE_MODES,
	FRAPPE_WRITE_ACTION_NONE,
	FRAPPE_WRITE_ACTIONS,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
	FRAPPE_WRITE_HOOK_EVENTS,
	FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION,
	FRAPPE_WRITE_HOOK_TYPES,
	MATCH_MODE_MATCH_FIELDS,
	MATCH_MODES,
	ONE_WAY_MATCH_FIRST,
	TIMESTAMP_TIE_MANUAL,
)
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	SYNC_TYPE_BIDIRECTIONAL,
	SyncFrappeWriteHookConfig,
)


def _normalize_match_mode(value: Any) -> str:
	mode = values_service._clean_string(value) or MATCH_MODE_MATCH_FIELDS
	if mode not in MATCH_MODES:
		raise frappe.ValidationError(f"Match Mode must be one of: {', '.join(sorted(MATCH_MODES))}.")
	return mode


def _first_configured_field(values: Any, default: str | None) -> str | None:
	for value in values or []:
		cleaned = values_service._clean_string(value)
		if cleaned:
			return cleaned
	return default


def _normalize_frappe_source_mode(value: Any) -> str:
	mode = values_service._clean_string(value) or FRAPPE_SOURCE_MODE_DOCTYPE_QUERY
	if mode not in FRAPPE_SOURCE_MODES:
		raise frappe.ValidationError(f"Frappe Source Mode must be one of: {', '.join(FRAPPE_SOURCE_MODES)}.")
	return mode


def _config_match_fields(config: Any) -> list[str]:
	return list(getattr(config, "match_fields", None) or [])


def _config_read_query(config: Any) -> str | None:
	return getattr(config, "read_query", None)


def _config_render_read_query_template(config: Any) -> bool:
	return values_service._as_bool(getattr(config, "render_read_query_template", 0))


def _config_frappe_source_mode(config: Any) -> str:
	return _normalize_frappe_source_mode(getattr(config, "frappe_source_mode", None))


def _config_one_way_match_mode(config: Any) -> str:
	return getattr(config, "one_way_match_mode", None) or ONE_WAY_MATCH_FIRST


def _update_existing_enabled(config: Any) -> bool:
	return values_service._as_bool(getattr(config, "update_existing", 1))


def _config_match_mode(config: Any) -> str:
	return _normalize_match_mode(getattr(config, "match_mode", None))


def _config_frappe_modified_field(config: Any) -> str:
	return values_service._clean_string(
		getattr(config, "frappe_modified_field", None)
	) or _first_configured_field(
		getattr(config, "frappe_modified_fields", None),
		"modified",
	)


def _config_frappe_creation_field(config: Any) -> str:
	return values_service._clean_string(getattr(config, "frappe_creation_field", None)) or "creation"


def _config_partner_modified_field(config: Any) -> str | None:
	return values_service._clean_string(
		getattr(config, "partner_modified_field", None)
	) or _first_configured_field(
		getattr(config, "partner_modified_fields", None),
		None,
	)


def _config_partner_creation_field(config: Any) -> str | None:
	return values_service._clean_string(getattr(config, "partner_creation_field", None))


def _partner_timestamps_required(config: Any) -> bool:
	return str(getattr(config, "sync_type", "") or "") == SYNC_TYPE_BIDIRECTIONAL or values_service._as_bool(
		getattr(config, "use_last_sync_date", 0)
	)


def _config_timestamp_tie_breaker(config: Any) -> str:
	return _normalize_timestamp_tie_breaker(getattr(config, "timestamp_tie_breaker", None))


def _normalize_timestamp_tie_breaker(value: Any) -> str:
	normalized = values_service._clean_string(value)
	if not normalized:
		return TIMESTAMP_TIE_MANUAL
	return normalized


def _normalize_frappe_write_action(value: Any) -> str:
	normalized = values_service._clean_string(value) or FRAPPE_WRITE_ACTION_NONE
	if normalized in FRAPPE_WRITE_ACTIONS:
		return normalized
	return FRAPPE_WRITE_ACTION_NONE


def _normalize_frappe_write_hook_event(value: Any) -> str | None:
	normalized = values_service._clean_string(value)
	if normalized in FRAPPE_WRITE_HOOK_EVENTS:
		return normalized
	return None


def _normalize_frappe_write_hook_type(value: Any) -> str | None:
	normalized = values_service._clean_string(value)
	if normalized in FRAPPE_WRITE_HOOK_TYPES:
		return normalized
	return None


def _normalize_frappe_write_hooks(
	value: Any,
	*,
	legacy_after_insert_action: Any = None,
	legacy_after_update_action: Any = None,
) -> tuple[SyncFrappeWriteHookConfig, ...]:
	rows = list(value or [])
	hooks = [_normalize_frappe_write_hook_row(row, fallback_idx=index + 1) for index, row in enumerate(rows)]
	hooks = [hook for hook in hooks if hook is not None]
	if not hooks:
		hooks.extend(
			_legacy_frappe_write_action_hooks(legacy_after_insert_action, legacy_after_update_action)
		)
	return tuple(sorted(hooks, key=lambda hook: hook.idx))


def _config_frappe_write_hooks(config: Any) -> tuple[SyncFrappeWriteHookConfig, ...]:
	return _normalize_frappe_write_hooks(
		getattr(config, "frappe_write_hooks", None),
		legacy_after_insert_action=getattr(config, "frappe_after_insert_action", None),
		legacy_after_update_action=getattr(config, "frappe_after_update_action", None),
	)


def _normalize_frappe_write_hook_row(row: Any, *, fallback_idx: int) -> SyncFrappeWriteHookConfig | None:
	event = _normalize_frappe_write_hook_event(values_service._first_value(row, ["event"]))
	hook_type = _normalize_frappe_write_hook_type(values_service._first_value(row, ["hook_type"]))
	if not event or not hook_type:
		return None
	action = None
	script = None
	if hook_type == FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION:
		action = _normalize_frappe_write_action(values_service._first_value(row, ["action"]))
		if action == FRAPPE_WRITE_ACTION_NONE:
			return None
	else:
		script = values_service._clean_string(values_service._first_value(row, ["script"]))
		if not script:
			return None
	return SyncFrappeWriteHookConfig(
		enabled=values_service._as_bool(values_service._first_value(row, ["enabled"], default=1)),
		event=event,
		hook_type=hook_type,
		action=action,
		script=script,
		description=values_service._clean_string(values_service._first_value(row, ["description"])),
		idx=cint(values_service._first_value(row, ["idx"], default=fallback_idx)) or fallback_idx,
	)


def _legacy_frappe_write_action_hooks(
	after_insert_action: Any,
	after_update_action: Any,
) -> list[SyncFrappeWriteHookConfig]:
	result: list[SyncFrappeWriteHookConfig] = []
	for idx, (event, action_value) in enumerate(
		(
			(FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT, after_insert_action),
			(FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE, after_update_action),
		),
		start=1,
	):
		action = _normalize_frappe_write_action(action_value)
		if action == FRAPPE_WRITE_ACTION_NONE:
			continue
		result.append(
			SyncFrappeWriteHookConfig(
				enabled=True,
				event=event,
				hook_type=FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION,
				action=action,
				idx=idx,
			)
		)
	return result


def _config_timestamp_buffer_ms(config: Any) -> int:
	return values_service._coerce_timestamp_buffer_ms(getattr(config, "timestamp_buffer_ms", None))


def _config_partner_identity_field(config: Any) -> str | None:
	return getattr(config, "partner_identity_field", None)


def _config_frappe_partner_identity_field(config: Any) -> str | None:
	return getattr(config, "frappe_partner_identity_field", None)


def _config_partner_frappe_identity_field(config: Any) -> str | None:
	return getattr(config, "partner_frappe_identity_field", None)


def _config_partner_create_strategy(config: Any) -> str:
	return getattr(config, "partner_create_id_strategy", None) or "payload"


def server_script_enabled() -> bool:
	get_common_site_config = getattr(frappe, "get_common_site_config", None)
	if callable(get_common_site_config):
		try:
			return values_service._as_bool(get_common_site_config(cached=True).get("server_script_enabled"))
		except Exception:
			return False
	return values_service._as_bool(getattr(getattr(frappe, "conf", None), "server_script_enabled", None))
