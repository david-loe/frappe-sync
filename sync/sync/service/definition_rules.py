# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import json
from types import SimpleNamespace

import frappe

from sync.sync.constants import (
	CONFLICT_POLICY_NEWEST_WINS,
	FRAPPE_SOURCE_MODE_DOCTYPE_QUERY,
	FRAPPE_SOURCE_MODE_PYTHON_SCRIPT,
	FRAPPE_WRITE_ACTION_NONE,
	FRAPPE_WRITE_ACTION_SUBMIT,
	FRAPPE_WRITE_ACTIONS,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
	FRAPPE_WRITE_HOOK_EVENTS,
	FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION,
	FRAPPE_WRITE_HOOK_TYPE_CUSTOM_SCRIPT,
	FRAPPE_WRITE_HOOK_TYPES,
	MAPPING_DIRECTION_BOTH,
	MAPPING_DIRECTION_FRAPPE_TO_PARTNER,
	MAPPING_DIRECTION_PARTNER_TO_FRAPPE,
	MAPPING_DIRECTIONS,
	MATCH_MODE_IDENTITY_FIELDS,
	MATCH_MODE_MATCH_FIELDS,
	MATCH_MODES,
	ONE_WAY_MATCH_FIRST,
	ONE_WAY_MATCH_MODES,
	TIMESTAMP_TIE_BREAKERS,
	TIMESTAMP_TIE_FRAPPE_WINS,
	TIMESTAMP_TIE_MANUAL,
	TIMESTAMP_TIE_PARTNER_WINS,
	UNMAPPED_ACTION_KEEP_ORIGINAL,
	UNMAPPED_ACTION_KEYS,
	UNMAPPED_ACTION_USE_FALLBACK,
	UNMAPPED_ACTION_USE_NULL,
	UNMAPPED_ACTIONS,
)
from sync.sync.service import config_access, mapping_rules

MAPPING_SCOPE_PARENT = "Parent"
MAPPING_SCOPE_CHILD = "Child"
MAPPING_SCOPES = (MAPPING_SCOPE_PARENT, MAPPING_SCOPE_CHILD)
CHILD_FIELD_PATH_SEPARATOR = "."


def validate(doc):
	"""Normalize and validate definition fields without saving or checking user permissions."""
	if not _clean_value(getattr(doc, "doctype_name", None)):
		frappe.throw("Sync Definition is missing target DocType field.")
	if not _clean_value(getattr(doc, "partner", None)):
		frappe.throw("Sync Definition is missing Sync Partner reference.")
	if getattr(doc, "sync_type", None) not in MAPPING_DIRECTIONS:
		frappe.throw("Unsupported sync direction.")
	validate_match_mode(doc)
	validate_field_mapping(doc)
	if not get_field_mapping(doc):
		frappe.throw("Sync Definition has no field mapping entries.")
	validate_value_mapping(doc)
	validate_match_fields(doc)
	validate_source_settings(doc)
	validate_filter_expression(doc)
	validate_frappe_source_settings(doc)
	validate_modified_fields(doc)
	validate_identity_settings(doc)
	validate_one_way_match_mode(doc)
	validate_computed_fields(doc)
	validate_write_behavior(doc)
	validate_preview_limit(doc)


def validate_field_mapping(doc):
	seen: set[str] = set()
	duplicates: list[str] = []
	partner_fields_by_direction: dict[tuple[str, str], str] = {}
	partner_duplicates: list[str] = []
	for row in doc.field_mapping or []:
		entry = _normalize_field_mapping_row(
			row,
			sync_type=getattr(doc, "sync_type", None),
			doctype_name=getattr(doc, "doctype_name", None),
		)
		if not entry:
			continue
		_assign_row_value(row, "frappe_field", entry["frappe_field"])
		_assign_row_value(row, "partner_field", entry["partner_field"])
		_assign_row_value(row, "direction", entry["direction"])
		_assign_row_value(row, "mapping_scope", entry["mapping_scope"])
		if entry["mapping_scope"] == MAPPING_SCOPE_CHILD:
			_assign_row_value(row, "table_field", entry["table_field"])
			_assign_row_value(row, "row_idx", entry["row_idx"])
			_assign_row_value(row, "child_field", entry["child_field"])
			_assign_row_value(row, "child_doctype", entry["child_doctype"])
		_normalize_field_mapping_fallbacks(row)
		if entry["frappe_field"] in seen:
			duplicates.append(entry["frappe_field"])
			continue
		seen.add(entry["frappe_field"])
		for direction in _directions_for_mapping_entry(entry["direction"]):
			key = (direction, entry["partner_field"])
			if key in partner_fields_by_direction:
				partner_duplicates.append(f"{entry['partner_field']} ({direction})")
				continue
			partner_fields_by_direction[key] = entry["frappe_field"]
	if duplicates:
		frappe.throw(f"Field Mapping contains duplicate Frappe fields: {', '.join(sorted(set(duplicates)))}")
	if partner_duplicates:
		frappe.throw(
			"Field Mapping contains duplicate Partner fields for the same direction: "
			+ ", ".join(sorted(set(partner_duplicates)))
		)


def validate_match_fields(doc):
	if (_clean_value(getattr(doc, "match_mode", None)) or MATCH_MODE_MATCH_FIELDS) != MATCH_MODE_MATCH_FIELDS:
		return
	mapping_fields = set(get_field_mapping(doc).keys())
	match_fields = get_match_fields(doc)
	if mapping_fields and not match_fields:
		frappe.throw("Match fields are required in Match Fields mode.")
	missing = [field for field in match_fields if field not in mapping_fields]
	if missing:
		frappe.throw(f"Match fields must exist in field mapping: {', '.join(missing)}")
	mapping = get_field_mapping(doc)
	for field in match_fields:
		directions = _directions_for_mapping_entry(mapping[field]["direction"])
		missing_directions = [
			direction
			for direction in _directions_for_mapping_entry(doc.sync_type)
			if direction not in directions
		]
		if missing_directions:
			frappe.throw(
				"Match field mappings must allow the active sync direction(s): "
				+ ", ".join(f"{field} ({direction})" for direction in missing_directions)
			)


def validate_value_mapping(doc):
	for row in doc.value_mapping or []:
		frappe_field = _clean_value(_get_row_value(row, "frappe_field"))
		if not frappe_field:
			continue
		_normalize_value_mapping_side(
			row,
			null_fieldname="frappe_value_is_null",
			value_fieldname="frappe_value",
			label="Frappe Value",
		)
		_normalize_value_mapping_side(
			row,
			null_fieldname="partner_value_is_null",
			value_fieldname="partner_value",
			label="Partner Value",
		)


def validate_source_settings(doc):
	table_name = _clean_value(doc.table_name)
	read_query = _clean_value(getattr(doc, "read_query", None))
	if not _delete_missing_allowed(getattr(doc, "sync_type", None), getattr(doc, "match_mode", None)):
		doc.delete_missing = 0
	if not table_name and not _read_query_can_replace_table_name(getattr(doc, "sync_type", None), read_query):
		frappe.throw("Table Name is required.")
	if read_query and getattr(doc, "delete_missing", None):
		frappe.throw("Delete Missing cannot be used together with Read Query.")
	doc.table_name = table_name
	doc.read_query = read_query


def validate_modified_fields(doc):
	doc.frappe_creation_field = "creation"
	doc.frappe_modified_field = _clean_value(getattr(doc, "frappe_modified_field", None)) or "modified"
	doc.partner_modified_field = _clean_value(getattr(doc, "partner_modified_field", None))
	doc.partner_creation_field = _clean_value(getattr(doc, "partner_creation_field", None))
	doc.timestamp_tie_breaker = (
		_clean_value(getattr(doc, "timestamp_tie_breaker", None)) or TIMESTAMP_TIE_MANUAL
	)

	partner_timestamps_required = _partner_timestamps_required(doc)
	if partner_timestamps_required and not doc.partner_modified_field:
		frappe.throw("Partner Modified Field is required.")
	if partner_timestamps_required and not doc.partner_creation_field:
		frappe.throw("Partner Creation Field is required.")
	if doc.frappe_modified_field == doc.frappe_creation_field:
		frappe.throw("Frappe Modified Field and Frappe Creation Field must be different.")
	if (
		doc.partner_modified_field
		and doc.partner_creation_field
		and doc.partner_modified_field == doc.partner_creation_field
	):
		frappe.throw("Partner Modified Field and Partner Creation Field must be different.")
	if _clean_value(getattr(doc, "sync_type", None)) != MAPPING_DIRECTION_BOTH:
		doc.timestamp_tie_breaker = TIMESTAMP_TIE_MANUAL
	if doc.timestamp_tie_breaker not in TIMESTAMP_TIE_BREAKERS:
		frappe.throw(
			"Timestamp Tie Breaker must be one of: "
			+ ", ".join((TIMESTAMP_TIE_MANUAL, TIMESTAMP_TIE_FRAPPE_WINS, TIMESTAMP_TIE_PARTNER_WINS))
		)

	doctype_name = _clean_value(getattr(doc, "doctype_name", None))
	if doctype_name:
		meta = frappe.get_meta(doctype_name)
		valid_fields = {"name", "creation", "modified"} | {
			field.fieldname for field in getattr(meta, "fields", []) or []
		}
		if doc.frappe_modified_field not in valid_fields:
			frappe.throw(f"Frappe Modified Field does not exist on {doctype_name}.")

	timestamp_fields = {
		doc.frappe_modified_field,
		doc.frappe_creation_field,
	}
	mapped_timestamp_fields = timestamp_fields & set(get_field_mapping(doc))
	if mapped_timestamp_fields:
		frappe.throw(
			"Dedicated timestamp fields must not also exist in Field Mapping: "
			+ ", ".join(sorted(mapped_timestamp_fields))
		)
	partner_timestamp_fields = {doc.partner_modified_field, doc.partner_creation_field} - {None}
	mapped_partner_timestamp_fields = partner_timestamp_fields & {
		entry["partner_field"] for entry in get_field_mapping(doc).values()
	}
	if mapped_partner_timestamp_fields:
		frappe.throw(
			"Dedicated partner timestamp fields must not also exist in Field Mapping: "
			+ ", ".join(sorted(mapped_partner_timestamp_fields))
		)


def validate_identity_settings(doc):
	strategy = _clean_value(doc.partner_create_id_strategy) or "payload"
	identity_field = _clean_value(doc.partner_identity_field)
	source = _clean_value(doc.partner_create_id_source)
	scope_where = _clean_value(doc.partner_create_id_scope_where)

	doc.partner_create_id_strategy = strategy
	doc.partner_identity_field = identity_field
	doc.partner_create_id_source = source
	doc.partner_create_id_scope_where = scope_where
	doc.frappe_partner_identity_field = _clean_value(doc.frappe_partner_identity_field)
	doc.partner_frappe_identity_field = _clean_value(doc.partner_frappe_identity_field)
	if _clean_value(getattr(doc, "match_mode", None)) == MATCH_MODE_IDENTITY_FIELDS:
		missing = []
		if not identity_field:
			missing.append("Partner Identity Field")
		if not doc.frappe_partner_identity_field:
			missing.append("Frappe Partner Identity Field")
		if not doc.partner_frappe_identity_field:
			missing.append("Partner Frappe Identity Field")
		if missing:
			frappe.throw("Identity Fields mode requires: " + ", ".join(missing) + ".")

	if strategy not in {"payload", "connector_default", "sequence", "max_plus_one"}:
		frappe.throw(
			"Partner Create ID Strategy must be one of: payload, connector_default, sequence, max_plus_one."
		)
	if strategy != "payload" and not identity_field:
		frappe.throw("Partner Identity Field is required when the partner ID is not taken from the payload.")
	if strategy == "sequence" and not source:
		frappe.throw("Partner Create ID Source is required for the sequence strategy.")
	if strategy != "sequence" and source:
		frappe.throw("Partner Create ID Source is only allowed for the sequence strategy.")
	if strategy == "max_plus_one" and not scope_where:
		frappe.throw("Partner Create ID Scope Where is required for the max_plus_one strategy.")
	if strategy != "max_plus_one" and scope_where:
		frappe.throw("Partner Create ID Scope Where is only allowed for the max_plus_one strategy.")
	doctype_name = _clean_value(getattr(doc, "doctype_name", None))
	if doc.frappe_partner_identity_field and doctype_name:
		meta = frappe.get_meta(doctype_name)
		valid_fields = {"name"} | {field.fieldname for field in getattr(meta, "fields", []) or []}
		if doc.frappe_partner_identity_field not in valid_fields:
			frappe.throw(f"Frappe Partner Identity Field does not exist on {doctype_name}.")


def validate_filter_expression(doc):
	doc.filter_expression = _normalize_filter_expression(doc.filter_expression)


def validate_frappe_source_settings(doc):
	doc.frappe_source_mode = _normalize_frappe_source_mode(getattr(doc, "frappe_source_mode", None))
	doc.frappe_source_script = _clean_value(getattr(doc, "frappe_source_script", None))
	if doc.frappe_source_mode != FRAPPE_SOURCE_MODE_PYTHON_SCRIPT:
		doc.frappe_source_script = None
		return
	if not doc.frappe_source_script:
		frappe.throw("Frappe Source Script is required.")
	if not _server_script_enabled():
		frappe.throw("Frappe Source Script requires server_script_enabled.")


def validate_preview_limit(doc):
	if doc.preview_limit is not None and doc.preview_limit < 1:
		frappe.throw("Preview Limit must be at least 1.")


def validate_one_way_match_mode(doc):
	mode = _clean_value(getattr(doc, "one_way_match_mode", None)) or ONE_WAY_MATCH_FIRST
	if mode not in ONE_WAY_MATCH_MODES:
		frappe.throw("One-Way Match Mode must be one of: first_match, all_matches.")
	doc.one_way_match_mode = mode


def validate_write_behavior(doc):
	doc.update_existing = 1 if getattr(doc, "update_existing", 1) else 0
	active_submit_events: set[str] = set()
	custom_script_found = False
	submit_found = False
	for row in getattr(doc, "frappe_write_hooks", None) or []:
		enabled = _row_flag(row, "enabled")
		_assign_row_value(row, "enabled", int(enabled))
		event = _normalize_frappe_write_hook_event(_get_row_value(row, "event"))
		hook_type = _normalize_frappe_write_hook_type(_get_row_value(row, "hook_type"))
		_assign_row_value(row, "event", event)
		_assign_row_value(row, "hook_type", hook_type)
		if hook_type == FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION:
			action = _normalize_frappe_write_action(_get_row_value(row, "action"))
			_assign_row_value(row, "action", "" if action == FRAPPE_WRITE_ACTION_NONE else action)
			_assign_row_value(row, "script", None)
			if enabled and action == FRAPPE_WRITE_ACTION_SUBMIT:
				submit_found = True
				if event not in {FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT, FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE}:
					frappe.throw("Built-in Submit is only allowed for After Insert and After Update hooks.")
				if event in active_submit_events:
					frappe.throw(f"Only one active built-in Submit hook is allowed for {event}.")
				active_submit_events.add(event)
		elif hook_type == FRAPPE_WRITE_HOOK_TYPE_CUSTOM_SCRIPT:
			_assign_row_value(row, "action", "")
			script = _clean_value(_get_raw_row_value(row, "script"))
			_assign_row_value(row, "script", script)
			if enabled:
				custom_script_found = True

	if submit_found:
		doctype_name = _clean_value(getattr(doc, "doctype_name", None))
		if doctype_name and not getattr(frappe.get_meta(doctype_name), "is_submittable", False):
			frappe.throw(f"Built-in Submit hook requires submittable DocType {doctype_name}.")
	if custom_script_found and not _server_script_enabled():
		frappe.throw("Custom Script hooks require server_script_enabled.")


def validate_match_mode(doc):
	mode = _clean_value(getattr(doc, "match_mode", None)) or MATCH_MODE_MATCH_FIELDS
	if mode not in MATCH_MODES:
		frappe.throw(f"Match Mode must be one of: {', '.join(MATCH_MODES)}.")
	doc.match_mode = mode


def validate_computed_fields(doc):
	seen: set[str] = set()
	for row in getattr(doc, "computed_fields", None) or []:
		field_name = _clean_value(_get_row_value(row, "field_name"))
		template = _clean_value(_get_raw_row_value(row, "template"))
		if not field_name and not template:
			continue
		if not field_name:
			frappe.throw("Computed Field Name is required.")
		if field_name in seen:
			frappe.throw(f"Duplicate Computed Field: {field_name}.")
		if not template:
			frappe.throw(f"Computed Field {field_name} requires a template.")
		seen.add(field_name)
		_assign_row_value(row, "field_name", field_name)
		_assign_row_value(row, "template", template)
		_assign_row_value(
			row,
			"required_source_fields",
			"\n".join(_parse_required_source_fields(_get_raw_row_value(row, "required_source_fields"))),
		)


def get_match_fields(doc) -> list[str]:
	fields: list[str] = []
	for row in getattr(doc, "match_fields", None) or []:
		field = _clean_value(_get_row_value(row, "frappe_field"))
		if field:
			fields.append(field)
	return fields


def get_field_mapping(doc) -> dict[str, dict[str, str]]:
	mapping = {}
	for row in doc.field_mapping or []:
		entry = _normalize_field_mapping_row(
			row,
			sync_type=getattr(doc, "sync_type", None),
			doctype_name=getattr(doc, "doctype_name", None),
		)
		if not entry:
			continue
		mapping[entry["frappe_field"]] = {
			"partner_field": entry["partner_field"],
			"direction": entry["direction"],
		}
	return mapping


def get_value_mapping(doc) -> dict[str, dict[object, object]]:
	result: dict[str, dict[object, object]] = {}
	for row in doc.value_mapping or []:
		frappe_field = _clean_value(_get_row_value(row, "frappe_field"))
		if not frappe_field:
			continue
		field_map = result.setdefault(frappe_field, {})
		frappe_value = (
			None if _row_flag(row, "frappe_value_is_null") else cstr(_get_raw_row_value(row, "frappe_value"))
		)
		partner_value = (
			None
			if _row_flag(row, "partner_value_is_null")
			else cstr(_get_raw_row_value(row, "partner_value"))
		)
		field_map[frappe_value] = partner_value
	return result


def get_value_mapping_fallbacks(doc) -> dict[str, dict[str, str | None]]:
	result: dict[str, dict[str, str | None]] = {}
	for row in doc.field_mapping or []:
		entry = _normalize_field_mapping_row(
			row,
			sync_type=getattr(doc, "sync_type", None),
			doctype_name=getattr(doc, "doctype_name", None),
		)
		if not entry:
			continue
		result[entry["frappe_field"]] = _get_unmapped_action_config(
			_get_row_value(row, "unmapped_action"),
			_get_row_value(row, "fallback_value"),
		)
	return result


def get_frappe_modified_fields(doc) -> list[str]:
	fieldname = _clean_value(getattr(doc, "frappe_modified_field", None)) or "modified"
	return [fieldname]


def get_partner_modified_fields(doc) -> list[str]:
	fieldname = _clean_value(getattr(doc, "partner_modified_field", None))
	return [fieldname] if fieldname else []


def as_export_dict(doc) -> dict:
	return {
		"name": doc.name,
		"title": doc.title,
		"enabled": doc.enabled,
		"partner": doc.partner,
		"sync_type": doc.sync_type,
		"doctype_name": doc.doctype_name,
		"frequency_cron": doc.frequency_cron,
		"filter_expression": doc.filter_expression,
		"batch_size": doc.batch_size,
		"use_last_sync_date": doc.use_last_sync_date,
		"timestamp_buffer_ms": doc.timestamp_buffer_ms,
		"create_new": doc.create_new,
		"update_existing": getattr(doc, "update_existing", 1),
		"delete_missing": doc.delete_missing,
		"frappe_write_hooks": get_frappe_write_hooks(doc),
		"computed_fields": get_computed_fields(doc),
		"frappe_source_mode": getattr(doc, "frappe_source_mode", FRAPPE_SOURCE_MODE_DOCTYPE_QUERY),
		"frappe_source_script": getattr(doc, "frappe_source_script", None),
		"match_mode": getattr(doc, "match_mode", MATCH_MODE_MATCH_FIELDS),
		"one_way_match_mode": getattr(doc, "one_way_match_mode", ONE_WAY_MATCH_FIRST),
		"conflict_policy": doc.conflict_policy or CONFLICT_POLICY_NEWEST_WINS,
		"table_name": doc.table_name,
		"read_query": getattr(doc, "read_query", None),
		"render_read_query_template": bool(getattr(doc, "render_read_query_template", 0)),
		"preview_limit": get_preview_limit(doc),
		"export_mask_credentials": bool(doc.export_mask_credentials),
		"frappe_modified_field": getattr(doc, "frappe_modified_field", "modified"),
		"frappe_creation_field": "creation",
		"partner_modified_field": getattr(doc, "partner_modified_field", None),
		"partner_creation_field": getattr(doc, "partner_creation_field", None),
		"timestamp_tie_breaker": getattr(doc, "timestamp_tie_breaker", TIMESTAMP_TIE_MANUAL),
		"match_fields": get_match_fields(doc),
		"field_mapping": get_field_mapping(doc),
		"value_mapping": get_value_mapping(doc),
		"value_mapping_fallbacks": get_value_mapping_fallbacks(doc),
		"partner_identity_field": getattr(doc, "partner_identity_field", None),
		"frappe_partner_identity_field": getattr(doc, "frappe_partner_identity_field", None),
		"partner_frappe_identity_field": getattr(doc, "partner_frappe_identity_field", None),
		"partner_create_id_strategy": getattr(doc, "partner_create_id_strategy", "payload"),
		"partner_create_id_source": getattr(doc, "partner_create_id_source", None),
		"partner_create_id_scope_where": getattr(doc, "partner_create_id_scope_where", None),
	}


def get_preview_limit(doc) -> int:
	try:
		return int(doc.preview_limit or 50)
	except Exception:
		return 50


def get_export_payload(doc) -> dict:
	return {
		"sync_definition": as_export_dict(doc),
		"mask_credentials": bool(doc.export_mask_credentials),
	}


def get_frappe_write_hooks(doc) -> list[dict]:
	result: list[dict] = []
	for row in getattr(doc, "frappe_write_hooks", None) or []:
		entry = _normalize_frappe_write_hook_row(row, strict=False)
		if entry:
			result.append(entry)
	return result


def get_computed_fields(doc) -> list[dict]:
	result: list[dict] = []
	for row in getattr(doc, "computed_fields", None) or []:
		field_name = _clean_value(_get_row_value(row, "field_name"))
		template = _clean_value(_get_raw_row_value(row, "template"))
		if field_name and template:
			result.append(
				{
					"field_name": field_name,
					"template": template,
					"required_source_fields": "\n".join(
						_parse_required_source_fields(_get_raw_row_value(row, "required_source_fields"))
					),
				}
			)
	return result


def _split_lines(value: str | None) -> list[str]:
	if not value:
		return []
	return [line.strip() for line in value.splitlines() if line.strip()]


def _clean_value(value: str | None) -> str | None:
	if value is None:
		return None
	value = str(value).strip()
	return value or None


def _get_row_value(row, *fieldnames):
	if row is None:
		return None
	if hasattr(row, "get"):
		for fieldname in fieldnames:
			value = row.get(fieldname)
			if value not in (None, ""):
				return value
	for fieldname in fieldnames:
		value = getattr(row, fieldname, None)
		if value not in (None, ""):
			return value
	return None


def _get_raw_row_value(row, fieldname, default=None):
	if row is None:
		return default
	if hasattr(row, "get"):
		return row.get(fieldname, default)
	return getattr(row, fieldname, default)


def _row_flag(row, fieldname: str) -> bool:
	value = _get_raw_row_value(row, fieldname)
	if isinstance(value, bool):
		return value
	if isinstance(value, int | float):
		return bool(value)
	return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _assign_row_value(row, fieldname: str, value):
	if row is None:
		return
	try:
		setattr(row, fieldname, value)
	except Exception:
		if hasattr(row, "update"):
			row.update({fieldname: value})


def _normalize_mapping_direction(value, *, default: str = MAPPING_DIRECTION_BOTH) -> str:
	return mapping_rules._normalize_mapping_direction(value or default)


def _directions_for_mapping_entry(direction: str) -> tuple[str, ...]:
	if direction == MAPPING_DIRECTION_BOTH:
		return (MAPPING_DIRECTION_FRAPPE_TO_PARTNER, MAPPING_DIRECTION_PARTNER_TO_FRAPPE)
	return (direction,)


def _one_way_mapping_direction(sync_type) -> str | None:
	return mapping_rules._one_way_mapping_direction(sync_type)


def _read_query_can_replace_table_name(sync_type, read_query) -> bool:
	return _one_way_mapping_direction(sync_type) == MAPPING_DIRECTION_PARTNER_TO_FRAPPE and bool(
		_clean_value(read_query)
	)


def _delete_missing_allowed(sync_type, match_mode) -> bool:
	if _one_way_mapping_direction(sync_type):
		return True
	return (
		_clean_value(sync_type) == MAPPING_DIRECTION_BOTH
		and _clean_value(match_mode) == MATCH_MODE_IDENTITY_FIELDS
	)


def _normalize_frappe_write_action(value) -> str:
	action = _clean_value(value) or FRAPPE_WRITE_ACTION_NONE
	if action not in FRAPPE_WRITE_ACTIONS:
		frappe.throw(
			"Frappe write action must be one of: "
			+ ", ".join((FRAPPE_WRITE_ACTION_NONE, FRAPPE_WRITE_ACTION_SUBMIT))
		)
	return action


def _normalize_frappe_write_hook_event(value) -> str:
	event = _clean_value(value) or FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT
	if event not in FRAPPE_WRITE_HOOK_EVENTS:
		frappe.throw(f"Frappe write hook event must be one of: {', '.join(FRAPPE_WRITE_HOOK_EVENTS)}.")
	return event


def _normalize_frappe_write_hook_type(value) -> str:
	hook_type = _clean_value(value) or FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION
	if hook_type not in FRAPPE_WRITE_HOOK_TYPES:
		frappe.throw(f"Frappe write hook type must be one of: {', '.join(FRAPPE_WRITE_HOOK_TYPES)}.")
	return hook_type


def _normalize_frappe_write_hook_row(row, *, strict: bool) -> dict | None:
	enabled = _row_flag(row, "enabled")
	event = _normalize_frappe_write_hook_event(_get_row_value(row, "event"))
	hook_type = _normalize_frappe_write_hook_type(_get_row_value(row, "hook_type"))
	description = _clean_value(_get_raw_row_value(row, "description"))
	entry: dict = {
		"enabled": int(enabled),
		"event": event,
		"hook_type": hook_type,
	}
	if description:
		entry["description"] = description
	if hook_type == FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION:
		action = _normalize_frappe_write_action(_get_row_value(row, "action"))
		if action == FRAPPE_WRITE_ACTION_NONE and strict:
			frappe.throw("Built-in Action hooks require an action.")
		entry["action"] = "" if action == FRAPPE_WRITE_ACTION_NONE else action
	else:
		script = _clean_value(_get_raw_row_value(row, "script"))
		if not script and strict:
			frappe.throw("Custom Script hooks require a script.")
		entry["script"] = script
	return entry


def _server_script_enabled() -> bool:
	return config_access.server_script_enabled()


def _current_user_is_system_manager() -> bool:
	has_role = getattr(frappe, "has_role", None)
	if callable(has_role):
		try:
			return bool(has_role("System Manager"))
		except TypeError:
			return bool(has_role(getattr(getattr(frappe, "session", None), "user", None), "System Manager"))
		except Exception:
			return False
	get_roles = getattr(frappe, "get_roles", None)
	if callable(get_roles):
		try:
			return "System Manager" in set(get_roles())
		except Exception:
			return False
	return False


def _partner_timestamps_required(doc) -> bool:
	return _clean_value(getattr(doc, "sync_type", None)) == MAPPING_DIRECTION_BOTH or _truthy(
		getattr(doc, "use_last_sync_date", None)
	)


def _truthy(value) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, int | float):
		return bool(value)
	return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_unmapped_action(value) -> str:
	action = _clean_value(value) or UNMAPPED_ACTION_KEEP_ORIGINAL
	if action not in UNMAPPED_ACTIONS:
		frappe.throw(f"Unmapped Action must be one of: {', '.join(UNMAPPED_ACTIONS)}")
	return action


def _normalize_frappe_source_mode(value) -> str:
	return config_access._normalize_frappe_source_mode(value)


def _parse_required_source_fields(value) -> list[str]:
	if value in (None, ""):
		return []
	if isinstance(value, str):
		cleaned = value.strip()
		if not cleaned:
			return []
		try:
			loaded = json.loads(cleaned)
		except Exception:
			loaded = None
		raw_values = loaded if isinstance(loaded, list) else cleaned.replace("\n", ",").split(",")
	elif isinstance(value, (list, tuple, set)):
		raw_values = value
	else:
		raw_values = [value]
	result: list[str] = []
	seen: set[str] = set()
	for raw_value in raw_values:
		fieldname = _clean_value(raw_value)
		if fieldname and fieldname not in seen:
			result.append(fieldname)
			seen.add(fieldname)
	return result


def _normalize_field_mapping_fallbacks(row) -> None:
	_normalize_field_mapping_fallback(
		row,
		action_fieldname="unmapped_action",
		value_fieldname="fallback_value",
	)


def _normalize_value_mapping_side(row, *, null_fieldname: str, value_fieldname: str, label: str) -> None:
	is_null = _row_flag(row, null_fieldname)
	_assign_row_value(row, null_fieldname, int(is_null))
	if is_null:
		_assign_row_value(row, value_fieldname, None)
		return
	value = _clean_value(_get_raw_row_value(row, value_fieldname))
	if value is None:
		frappe.throw(f"{label} is required unless its NULL option is enabled.")
	_assign_row_value(row, value_fieldname, value)


def _normalize_field_mapping_fallback(row, *, action_fieldname: str, value_fieldname: str) -> None:
	action = _normalize_unmapped_action(_get_row_value(row, action_fieldname))
	_assign_row_value(row, action_fieldname, action)
	if action == UNMAPPED_ACTION_USE_FALLBACK:
		fallback_value = _clean_value(_get_row_value(row, value_fieldname))
		if fallback_value is None:
			frappe.throw("Fallback Value is required when Unmapped Action is Use Fallback Value.")
		_assign_row_value(row, value_fieldname, fallback_value)
		return
	if action == UNMAPPED_ACTION_USE_NULL:
		_assign_row_value(row, value_fieldname, None)


def _get_unmapped_action_config(action, value) -> dict[str, str | None]:
	normalized_action = _normalize_unmapped_action(action)
	if normalized_action == UNMAPPED_ACTION_USE_FALLBACK:
		return {"action": UNMAPPED_ACTION_KEYS[normalized_action], "value": _clean_value(value)}
	return {"action": UNMAPPED_ACTION_KEYS[normalized_action], "value": None}


def _normalize_field_mapping_row(
	row,
	*,
	sync_type: str | None = None,
	doctype_name: str | None = None,
) -> dict[str, str] | None:
	path = mapping_rules._parse_child_field_path(_get_row_value(row, "frappe_field"))
	if path and not _get_row_value(row, "table_field"):
		for key, value in zip(("table_field", "row_idx", "child_field"), path, strict=True):
			_assign_row_value(row, key, value)
		_assign_row_value(row, "mapping_scope", MAPPING_SCOPE_CHILD)
	scope = _normalize_mapping_scope(_get_row_value(row, "mapping_scope"), row=row)
	normalized_direction = _normalize_mapping_direction(_get_row_value(row, "direction"))
	partner_field = _clean_value(
		_get_row_value(row, "partner_field", "target_field", "external_field", "column_name")
	)
	if not partner_field:
		return None
	if scope == MAPPING_SCOPE_CHILD:
		child_entry = _normalize_child_mapping_row(row, doctype_name=doctype_name)
		if not child_entry:
			return None
		return {
			**child_entry,
			"partner_field": partner_field,
			"direction": _one_way_mapping_direction(sync_type) or normalized_direction,
			"mapping_scope": MAPPING_SCOPE_CHILD,
		}

	frappe_field = _clean_value(
		_get_row_value(row, "frappe_field", "source_field", "doctype_field", "field_name")
	)
	if not frappe_field:
		return None
	return {
		"frappe_field": frappe_field,
		"partner_field": partner_field,
		"direction": _one_way_mapping_direction(sync_type) or normalized_direction,
		"mapping_scope": MAPPING_SCOPE_PARENT,
	}


def _normalize_mapping_scope(value, *, row=None) -> str:
	scope = _clean_value(value)
	if not scope:
		if row is not None and any(
			_clean_value(_get_row_value(row, fieldname))
			for fieldname in ("table_field", "child_field", "child_doctype", "row_idx")
		):
			return MAPPING_SCOPE_CHILD
		return MAPPING_SCOPE_PARENT
	if scope not in MAPPING_SCOPES:
		frappe.throw(f"Mapping Scope must be one of: {', '.join(MAPPING_SCOPES)}")
	return scope


def _normalize_child_mapping_row(row, *, doctype_name: str | None = None) -> dict[str, str] | None:
	table_field = _clean_value(_get_row_value(row, "table_field"))
	child_field = _clean_value(_get_row_value(row, "child_field"))
	row_idx = _coerce_positive_int(_get_row_value(row, "row_idx"), label="Row Index")
	if not table_field or not child_field:
		return None
	child_doctype = _child_doctype_for_table_field(doctype_name, table_field)
	if not child_doctype:
		frappe.throw(f"Table Field must be a Table field on {doctype_name}.")
	configured_child_doctype = _clean_value(_get_row_value(row, "child_doctype"))
	if configured_child_doctype and configured_child_doctype != child_doctype:
		frappe.throw(f"Child DocType for {table_field} must be {child_doctype}.")
	_validate_child_field(child_doctype, child_field)
	field_path = CHILD_FIELD_PATH_SEPARATOR.join((table_field, str(row_idx), child_field))
	return {
		"frappe_field": field_path,
		"table_field": table_field,
		"child_doctype": child_doctype,
		"row_idx": str(row_idx),
		"child_field": child_field,
	}


def _coerce_positive_int(value, *, label: str) -> int:
	try:
		result = int(value)
	except Exception:
		frappe.throw(f"{label} must be a positive integer.")
		return 0
	if result < 1:
		frappe.throw(f"{label} must be a positive integer.")
	return result


def _child_doctype_for_table_field(doctype_name: str | None, table_field: str) -> str | None:
	if not doctype_name:
		return None
	meta = frappe.get_meta(doctype_name)
	for field in getattr(meta, "fields", []) or []:
		if getattr(field, "fieldname", None) == table_field and getattr(field, "fieldtype", None) == "Table":
			return _clean_value(getattr(field, "options", None))
	return None


def _validate_child_field(child_doctype: str, child_field: str) -> None:
	meta = frappe.get_meta(child_doctype)
	for field in getattr(meta, "fields", []) or []:
		if getattr(field, "fieldname", None) != child_field:
			continue
		if getattr(field, "fieldtype", None) in {"Table", "Table MultiSelect"}:
			frappe.throw(f"Child Field cannot be a table field: {child_field}.")
		return
	frappe.throw(f"Child Field does not exist on {child_doctype}: {child_field}.")


def _normalize_filter_expression(value) -> str | None:
	if value is None:
		return None

	if isinstance(value, str):
		value = value.strip()
		if not value:
			return None
		try:
			parsed = json.loads(value)
		except Exception:
			frappe.throw("Filter Expression must be valid JSON.")
			return None
		if not isinstance(parsed, (list, dict)):
			frappe.throw("Filter Expression must decode to a JSON array or object.")
		return value

	if isinstance(value, (list, dict)):
		try:
			return json.dumps(value, sort_keys=isinstance(value, dict))
		except Exception:
			frappe.throw("Filter Expression must be JSON serializable.")
			return None

	frappe.throw("Filter Expression must decode to a JSON array or object.")
	return None


def cstr(value) -> str:
	if value is None:
		return ""
	if isinstance(value, (dict, list)):
		return json.dumps(value, sort_keys=True)
	return str(value)


class DefinitionInput(SimpleNamespace):
	"""Attribute adapter for unsaved definition payloads and normalized configs."""

	def __init__(self, values):
		super().__init__(**values)

	def get(self, key, default=None):
		return getattr(self, key, default)


def validate_script_permissions(doc):
	if (
		any(
			_get_row_value(row, "hook_type") == FRAPPE_WRITE_HOOK_TYPE_CUSTOM_SCRIPT
			and _clean_value(_get_raw_row_value(row, "script"))
			for row in getattr(doc, "frappe_write_hooks", None) or []
		)
		and not _current_user_is_system_manager()
	):
		frappe.throw("Only System Manager can save non-empty Custom Script hooks.")
