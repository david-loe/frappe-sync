from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import MISSING, asdict, fields, replace
from types import SimpleNamespace
from typing import Any

import frappe
from frappe.utils import cint

from sync.sync.constants import (
	CONFLICT_POLICY_NEWEST_WINS,
	FRAPPE_SOURCE_MODE_DOCTYPE_QUERY,
	MATCH_MODE_MATCH_FIELDS,
	ONE_WAY_MATCH_FIRST,
	SYNC_FRAPPE_WRITE_HOOK,
	VALUE_MAPPING_FALLBACK_ACTIONS,
	VALUE_MAPPING_FALLBACK_KEEP_ORIGINAL,
	VALUE_MAPPING_FALLBACK_USE_FALLBACK,
	VALUE_MAPPING_FALLBACK_USE_NULL,
)
from sync.sync.service import config_access as config_access_service
from sync.sync.service import definition_rules
from sync.sync.service import mapping_rules as mapping_rules_service
from sync.sync.service import metadata as metadata_service
from sync.sync.service import time_utils as time_utils_service
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	DEFAULT_TIMESTAMP_BUFFER_MS,
	SYNC_TYPE_FRAPPE_TO_PARTNER,
	SyncComputedFieldConfig,
	SyncDefinitionConfig,
	SyncFrappeWriteHookConfig,
)


def _coerce_config(config: SyncDefinitionConfig | Any) -> SyncDefinitionConfig:
	timestamp_buffer_ms = values_service._coerce_timestamp_buffer_ms(
		getattr(config, "timestamp_buffer_ms", None)
	)
	match_mode = config_access_service._normalize_match_mode(getattr(config, "match_mode", None))
	normalized = SyncDefinitionConfig(
		name=str(getattr(config, "name", "")),
		doctype=str(getattr(config, "doctype", "")),
		partner=str(getattr(config, "partner", "")),
		sync_type=str(getattr(config, "sync_type", "Frappe -> Partner")),
		cron=getattr(config, "cron", None),
		filters=getattr(config, "filters", None),
		batch_size=cint(getattr(config, "batch_size", 100)) or 100,
		create_new=values_service._as_bool(getattr(config, "create_new", 1)),
		delete_missing=_delete_missing_enabled(
			getattr(config, "sync_type", "Frappe -> Partner"),
			getattr(config, "delete_missing", 0),
			match_mode=match_mode,
		),
		one_way_match_mode=values_service._clean_string(getattr(config, "one_way_match_mode", None))
		or ONE_WAY_MATCH_FIRST,
		use_last_sync_date=values_service._as_bool(getattr(config, "use_last_sync_date", 1)),
		conflict_policy=str(getattr(config, "conflict_policy", CONFLICT_POLICY_NEWEST_WINS)),
		timestamp_buffer_ms=timestamp_buffer_ms,
		table_name=getattr(config, "table_name", None),
		read_query=getattr(config, "read_query", None),
		match_fields=list(getattr(config, "match_fields", []) or []),
		mapping=mapping_rules_service._normalize_field_mapping(getattr(config, "mapping", {}) or {}),
		value_mapping=dict(getattr(config, "value_mapping", {}) or {}),
		match_mode=match_mode,
		frappe_modified_field=values_service._clean_string(getattr(config, "frappe_modified_field", None))
		or config_access_service._first_configured_field(
			getattr(config, "frappe_modified_fields", None), "modified"
		),
		frappe_creation_field=values_service._clean_string(getattr(config, "frappe_creation_field", None))
		or "creation",
		partner_modified_field=values_service._clean_string(getattr(config, "partner_modified_field", None))
		or config_access_service._first_configured_field(
			getattr(config, "partner_modified_fields", None), None
		),
		partner_creation_field=values_service._clean_string(getattr(config, "partner_creation_field", None)),
		timestamp_tie_breaker=config_access_service._normalize_timestamp_tie_breaker(
			getattr(config, "timestamp_tie_breaker", None)
		),
		value_mapping_fallbacks=_normalize_value_mapping_fallbacks(
			getattr(config, "value_mapping_fallbacks", {}) or {}
		),
		partner_identity_field=values_service._clean_string(getattr(config, "partner_identity_field", None)),
		frappe_partner_identity_field=values_service._clean_string(
			getattr(config, "frappe_partner_identity_field", None)
		),
		partner_frappe_identity_field=values_service._clean_string(
			getattr(config, "partner_frappe_identity_field", None)
		),
		partner_create_id_strategy=values_service._clean_string(
			getattr(config, "partner_create_id_strategy", None)
		)
		or "payload",
		partner_create_id_source=values_service._clean_string(
			getattr(config, "partner_create_id_source", None)
		),
		partner_create_id_scope_where=values_service._clean_string(
			getattr(config, "partner_create_id_scope_where", None)
		),
		partner_time_zone=time_utils_service._normalize_time_zone_name(
			getattr(config, "partner_time_zone", None)
		),
		capture_audit_payloads=values_service._as_bool(getattr(config, "capture_audit_payloads", 0)),
		update_existing=values_service._as_bool(getattr(config, "update_existing", 1)),
		render_read_query_template=values_service._as_bool(getattr(config, "render_read_query_template", 0)),
		computed_fields=_normalize_computed_fields(getattr(config, "computed_fields", None)),
		frappe_source_mode=config_access_service._normalize_frappe_source_mode(
			getattr(config, "frappe_source_mode", None)
		),
		frappe_source_script=values_service._clean_string(getattr(config, "frappe_source_script", None)),
		frappe_write_hooks=config_access_service._normalize_frappe_write_hooks(
			getattr(config, "frappe_write_hooks", None),
			legacy_after_insert_action=getattr(config, "frappe_after_insert_action", None),
			legacy_after_update_action=getattr(config, "frappe_after_update_action", None),
		),
	)
	return validate_config(normalized)


def _build_definition_config(sync_definition_doc: Any) -> SyncDefinitionConfig:
	sync_definition_doc = definition_input(sync_definition_doc)
	normalize_definition_document(sync_definition_doc)
	doctype = values_service._first_value(sync_definition_doc, ["doctype_name"])
	if not doctype:
		raise frappe.ValidationError("Sync Definition is missing target DocType field.")

	partner = values_service._first_value(sync_definition_doc, ["partner"])
	if not partner:
		raise frappe.ValidationError("Sync Definition is missing Sync Partner reference.")

	sync_type = values_service._first_value(sync_definition_doc, ["sync_type"], default="Frappe -> Partner")
	cron_expr = values_service._first_value(sync_definition_doc, ["frequency_cron"])
	filters = _parse_filter_expression(
		values_service._first_value(sync_definition_doc, ["filter_expression"])
	)
	batch_size = cint(values_service._first_value(sync_definition_doc, ["batch_size"], default=100)) or 100
	create_new = values_service._as_bool(
		values_service._first_value(sync_definition_doc, ["create_new"], default=1)
	)
	match_mode = config_access_service._normalize_match_mode(
		values_service._first_value(sync_definition_doc, ["match_mode"], default=MATCH_MODE_MATCH_FIELDS)
	)
	delete_missing = _delete_missing_enabled(
		sync_type,
		values_service._first_value(sync_definition_doc, ["delete_missing"], default=0),
		match_mode=match_mode,
	)
	use_last_sync_date = values_service._as_bool(
		values_service._first_value(sync_definition_doc, ["use_last_sync_date"], default=1)
	)
	conflict_policy = str(
		values_service._first_value(
			sync_definition_doc, ["conflict_policy"], default=CONFLICT_POLICY_NEWEST_WINS
		)
	)
	timestamp_buffer_ms = values_service._coerce_timestamp_buffer_ms(
		values_service._first_value(sync_definition_doc, ["timestamp_buffer_ms"])
	)

	match_fields = definition_rules.get_match_fields(sync_definition_doc)
	mapping = definition_rules.get_field_mapping(sync_definition_doc)
	mapping = mapping_rules_service._force_mapping_direction(mapping, sync_type)
	value_mapping = definition_rules.get_value_mapping(sync_definition_doc)
	value_mapping_fallbacks = definition_rules.get_value_mapping_fallbacks(sync_definition_doc)
	computed_fields = _normalize_computed_fields(sync_definition_doc.computed_fields)
	if not mapping:
		raise frappe.ValidationError("Sync Definition has no field mapping entries.")

	frappe_modified_field = (
		values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["frappe_modified_field"])
		)
		or "modified"
	)
	frappe_creation_field = (
		values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["frappe_creation_field"])
		)
		or "creation"
	)
	partner_modified_field = values_service._clean_string(
		values_service._first_value(sync_definition_doc, ["partner_modified_field"])
	)
	partner_creation_field = values_service._clean_string(
		values_service._first_value(sync_definition_doc, ["partner_creation_field"])
	)
	timestamp_tie_breaker = config_access_service._normalize_timestamp_tie_breaker(
		values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["timestamp_tie_breaker"])
		)
	)
	config = SyncDefinitionConfig(
		name=sync_definition_doc.name,
		doctype=str(doctype),
		partner=str(partner),
		sync_type=str(sync_type),
		cron=str(cron_expr) if cron_expr else None,
		filters=filters,
		batch_size=batch_size,
		create_new=create_new,
		delete_missing=delete_missing,
		one_way_match_mode=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["one_way_match_mode"])
		)
		or ONE_WAY_MATCH_FIRST,
		update_existing=values_service._as_bool(
			values_service._first_value(sync_definition_doc, ["update_existing"], default=1)
		),
		frappe_write_hooks=config_access_service._normalize_frappe_write_hooks(
			sync_definition_doc.frappe_write_hooks
		),
		use_last_sync_date=use_last_sync_date,
		conflict_policy=conflict_policy,
		timestamp_buffer_ms=timestamp_buffer_ms,
		table_name=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["table_name"])
		),
		read_query=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["read_query"])
		),
		match_fields=match_fields,
		mapping=mapping,
		value_mapping=value_mapping,
		match_mode=match_mode,
		frappe_modified_field=frappe_modified_field,
		frappe_creation_field=frappe_creation_field,
		partner_modified_field=partner_modified_field,
		partner_creation_field=partner_creation_field,
		timestamp_tie_breaker=timestamp_tie_breaker,
		value_mapping_fallbacks=value_mapping_fallbacks,
		partner_identity_field=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["partner_identity_field"])
		),
		frappe_partner_identity_field=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["frappe_partner_identity_field"])
		),
		partner_frappe_identity_field=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["partner_frappe_identity_field"])
		),
		partner_create_id_strategy=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["partner_create_id_strategy"])
		)
		or "payload",
		partner_create_id_source=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["partner_create_id_source"])
		),
		partner_create_id_scope_where=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["partner_create_id_scope_where"])
		),
		capture_audit_payloads=values_service._as_bool(
			values_service._first_value(sync_definition_doc, ["capture_audit_payloads"], default=0)
		),
		render_read_query_template=values_service._as_bool(
			values_service._first_value(sync_definition_doc, ["render_read_query_template"], default=0)
		),
		computed_fields=computed_fields,
		frappe_source_mode=config_access_service._normalize_frappe_source_mode(
			values_service._first_value(
				sync_definition_doc, ["frappe_source_mode"], default=FRAPPE_SOURCE_MODE_DOCTYPE_QUERY
			)
		),
		frappe_source_script=values_service._clean_string(
			values_service._first_value(sync_definition_doc, ["frappe_source_script"])
		),
	)
	return config


def _get_match_fields(source):
	return definition_rules.get_match_fields(definition_input(source))


def _get_field_mapping(source):
	return definition_rules.get_field_mapping(definition_input(source))


def _delete_missing_enabled(sync_type: Any, value: Any, *, match_mode: Any = MATCH_MODE_MATCH_FIELDS) -> bool:
	return definition_rules._delete_missing_allowed(sync_type, match_mode) and values_service._as_bool(value)


def validate_config(config: SyncDefinitionConfig) -> SyncDefinitionConfig:
	"""Validate every runtime entry through the same rules as an unsaved definition."""
	doc = definition_input_from_config(config)
	normalize_definition_document(doc)
	return replace(
		config,
		mapping=definition_rules.get_field_mapping(doc),
		match_fields=definition_rules.get_match_fields(doc),
		table_name=doc.table_name,
		read_query=doc.read_query,
		filters=json.loads(doc.filter_expression) if doc.filter_expression else None,
		frappe_write_hooks=config_access_service._normalize_frappe_write_hooks(doc.frappe_write_hooks),
		delete_missing=bool(doc.delete_missing),
		frappe_creation_field=doc.frappe_creation_field,
		timestamp_tie_breaker=doc.timestamp_tie_breaker,
		frappe_source_script=doc.frappe_source_script,
		partner_time_zone=time_utils_service._normalize_time_zone_name(config.partner_time_zone),
	)


def _normalize_computed_fields(rows: Any) -> tuple[SyncComputedFieldConfig, ...]:
	normalized = [
		{
			"field_name": _first_row_value(row, ["field_name", "fieldname", "frappe_field"]),
			"template": _first_row_value(row, ["template", "jinja_template"]),
			"required_source_fields": _first_row_value(row, ["required_source_fields"]),
			"idx": cint(_first_row_value(row, ["idx"], default=idx)) or idx,
		}
		for idx, row in enumerate(rows or [], start=1)
	]
	definition_rules.validate_computed_fields(SimpleNamespace(computed_fields=normalized))
	return tuple(
		sorted(
			(
				SyncComputedFieldConfig(
					field_name=row["field_name"],
					template=row["template"],
					required_source_fields=tuple(
						_parse_required_source_fields(row["required_source_fields"])
					),
					idx=row["idx"],
				)
				for row in normalized
				if row["field_name"] or row["template"]
			),
			key=lambda field: field.idx,
		)
	)


def _first_row_value(row: Any, candidates: list[str], default: Any = None) -> Any:
	for candidate in candidates:
		value = values_service._row_value(row, candidate)
		if value not in (None, ""):
			return value
	return default


def _parse_required_source_fields(value):
	return definition_rules._parse_required_source_fields(value)


def _computed_field_names(config: SyncDefinitionConfig | Any) -> set[str]:
	return {
		field.field_name for field in _normalize_computed_fields(getattr(config, "computed_fields", None))
	}


def _computed_required_source_fields(config: SyncDefinitionConfig | Any) -> set[str]:
	result: set[str] = set()
	for field in _normalize_computed_fields(getattr(config, "computed_fields", None)):
		result.update(field.required_source_fields)
	return result


def _get_value_mapping(source):
	return definition_rules.get_value_mapping(definition_input(source))


def _get_value_mapping_fallbacks(source):
	return definition_rules.get_value_mapping_fallbacks(definition_input(source))


def _normalize_value_mapping_fallbacks(raw_fallbacks: Any) -> dict[str, dict[str, Any]]:
	if isinstance(raw_fallbacks, str):
		try:
			raw_fallbacks = json.loads(raw_fallbacks)
		except Exception:
			return {}
	if not isinstance(raw_fallbacks, dict):
		return {}

	result: dict[str, dict[str, Any]] = {}
	for frappe_field, raw_field_fallbacks in raw_fallbacks.items():
		frappe_field = values_service._clean_string(frappe_field)
		if not frappe_field or not isinstance(raw_field_fallbacks, dict):
			continue
		if "action" in raw_field_fallbacks:
			result[frappe_field] = _normalize_value_mapping_fallback(
				raw_field_fallbacks.get("action"),
				raw_field_fallbacks.get("value"),
			)
	return result


def _normalize_value_mapping_fallback(action: Any, value: Any = None) -> dict[str, Any]:
	normalized_action = _normalize_value_mapping_fallback_action(action)
	if normalized_action == VALUE_MAPPING_FALLBACK_KEEP_ORIGINAL:
		return {"action": normalized_action, "value": None}
	if normalized_action == VALUE_MAPPING_FALLBACK_USE_NULL:
		return {"action": normalized_action, "value": None}
	return {"action": normalized_action, "value": value}


def _normalize_value_mapping_fallback_action(action: Any) -> str:
	cleaned = values_service._clean_string(action)
	if not cleaned:
		return VALUE_MAPPING_FALLBACK_KEEP_ORIGINAL
	normalized = cleaned.lower().replace("-", "_").replace(" ", "_")
	aliases = {
		"keep_original": VALUE_MAPPING_FALLBACK_KEEP_ORIGINAL,
		"fallback": VALUE_MAPPING_FALLBACK_USE_FALLBACK,
		"use_fallback_value": VALUE_MAPPING_FALLBACK_USE_FALLBACK,
		"null": VALUE_MAPPING_FALLBACK_USE_NULL,
		"use_null": VALUE_MAPPING_FALLBACK_USE_NULL,
	}
	result = aliases.get(normalized)
	if result:
		return result
	if cleaned in VALUE_MAPPING_FALLBACK_ACTIONS:
		return cleaned
	raise frappe.ValidationError(
		"Value Mapping fallback action must be one of: keep_original, fallback, null."
	)


def _get_frappe_write_hooks(sync_definition_doc: Any) -> tuple[SyncFrappeWriteHookConfig, ...]:
	try:
		rows = metadata_service._get_child_rows_by_options(sync_definition_doc, SYNC_FRAPPE_WRITE_HOOK)
	except Exception:
		return config_access_service._normalize_frappe_write_hooks(
			[],
			legacy_after_insert_action=values_service._first_value(
				sync_definition_doc, ["frappe_after_insert_action"]
			),
			legacy_after_update_action=values_service._first_value(
				sync_definition_doc, ["frappe_after_update_action"]
			),
		)
	return config_access_service._normalize_frappe_write_hooks(rows)


def _merge_partner_runtime_settings(config: SyncDefinitionConfig, partner_doc: Any) -> SyncDefinitionConfig:
	partner_time_zone = time_utils_service._get_partner_time_zone(partner_doc)
	if isinstance(config, SyncDefinitionConfig):
		return replace(config, partner_time_zone=partner_time_zone)
	config.partner_time_zone = partner_time_zone
	return _coerce_config(config)


def _parse_filter_expression(raw: Any) -> list | dict | None:
	if raw in (None, ""):
		return None
	if isinstance(raw, (list, dict)):
		return raw
	if isinstance(raw, str):
		try:
			loaded = json.loads(raw)
			if isinstance(loaded, (list, dict)):
				return loaded
		except Exception:
			frappe.logger("sync").warning("Invalid filter JSON in Sync Definition. Raw value ignored.")
	return None


# Defaults used by all unsaved inputs, including YAML documents and DocType validation.
_DEFINITION_DEFAULTS = {
	"name": "",
	"doctype_name": None,
	"partner": None,
	"sync_type": SYNC_TYPE_FRAPPE_TO_PARTNER,
	"frequency_cron": None,
	"filter_expression": None,
	"batch_size": 100,
	"create_new": 1,
	"delete_missing": 0,
	"use_last_sync_date": 1,
	"conflict_policy": CONFLICT_POLICY_NEWEST_WINS,
	"timestamp_buffer_ms": DEFAULT_TIMESTAMP_BUFFER_MS,
	"table_name": None,
	"read_query": None,
	"preview_limit": 50,
	"update_existing": 1,
	"partner_create_id_strategy": "payload",
	"partner_create_id_source": None,
	"partner_create_id_scope_where": None,
	"partner_identity_field": None,
	"frappe_partner_identity_field": None,
	"partner_frappe_identity_field": None,
}
_CHILD_TYPES = {
	"match_fields": "Sync Key Field",
	"field_mapping": "Sync Field Mapping",
	"value_mapping": "Sync Value Mapping",
	"computed_fields": "Sync Computed Field",
	"frappe_write_hooks": "Sync Frappe Write Hook",
}


def definition_input(source: Any) -> definition_rules.DefinitionInput:
	"""Copy configuration fields without saving or mutating the supplied document."""
	names = (
		{field.name for field in fields(SyncDefinitionConfig)}
		| set(_DEFINITION_DEFAULTS)
		| {
			"frappe_after_insert_action",
			"frappe_after_update_action",
			"export_mask_credentials",
		}
	)
	defaults = {
		field.name: field.default for field in fields(SyncDefinitionConfig) if field.default is not MISSING
	}
	defaults.update(_DEFINITION_DEFAULTS)
	data = {
		name: deepcopy(values_service._first_value(source, [name], default=defaults.get(name)))
		for name in names
	}
	for name, child_type in _CHILD_TYPES.items():
		rows = values_service._first_value(source, [name])
		if rows is None:
			try:
				rows = metadata_service._get_child_rows_by_options(source, child_type)
			except AttributeError:
				rows = []
			except Exception:
				if name != "frappe_write_hooks":
					raise
				rows = [asdict(hook) for hook in _get_frappe_write_hooks(source)]
		data[name] = deepcopy(rows or [])
	if isinstance(data["match_fields"], str):
		data["match_fields"] = [
			{"frappe_field": value.strip()} for value in data["match_fields"].split(",") if value.strip()
		]
	if isinstance(data["field_mapping"], (str, dict)):
		data["field_mapping"] = [
			{"frappe_field": name, **entry}
			for name, entry in mapping_rules_service._normalize_field_mapping(data["field_mapping"]).items()
		]
	if isinstance(data["value_mapping"], str):
		data["value_mapping"] = json.loads(data["value_mapping"])
	if isinstance(data["value_mapping"], dict):
		data["value_mapping"] = [
			{
				"frappe_field": field,
				"frappe_value": source,
				"partner_value": target,
				"frappe_value_is_null": source is None,
				"partner_value_is_null": target is None,
			}
			for field, entries in data["value_mapping"].items()
			for source, target in entries.items()
		]
	fallbacks = _normalize_value_mapping_fallbacks(data.get("value_mapping_fallbacks"))
	for row in data["field_mapping"]:
		fallback = fallbacks.get(values_service._row_value(row, "frappe_field"))
		if fallback:
			definition_rules._assign_row_value(
				row,
				"unmapped_action",
				{
					"keep_original": "Keep Original",
					"null": "Use NULL",
					"fallback": "Use Fallback Value",
				}[fallback["action"]],
			)
			definition_rules._assign_row_value(row, "fallback_value", fallback["value"])
	for row in data["value_mapping"]:
		for target, aliases in (("frappe_value", ["source_value"]), ("partner_value", ["target_value"])):
			if values_service._row_value(row, target) is None:
				value = _first_row_value(row, aliases)
				if value is not None:
					definition_rules._assign_row_value(row, target, value)
	return definition_rules.DefinitionInput(data)


def normalize_definition_document(doc: Any) -> None:
	"""Normalize in place only at the document boundary; semantic checks live in definition_rules."""
	for name, default in _DEFINITION_DEFAULTS.items():
		if getattr(doc, name, None) is None:
			setattr(doc, name, default)
	for name in _CHILD_TYPES:
		if getattr(doc, name, None) is None:
			setattr(doc, name, [])
	definition_rules.validate(doc)


def definition_input_from_config(config: SyncDefinitionConfig) -> definition_rules.DefinitionInput:
	data = asdict(config)
	data.update(doctype_name=config.doctype, frequency_cron=config.cron, filter_expression=config.filters)
	data["match_fields"] = [{"frappe_field": name} for name in config.match_fields]
	data["field_mapping"] = [{"frappe_field": name, **entry} for name, entry in config.mapping.items()]
	data["value_mapping"] = [
		{
			"frappe_field": field,
			"frappe_value": source,
			"partner_value": target,
			"frappe_value_is_null": source is None,
			"partner_value_is_null": target is None,
		}
		for field, entries in config.value_mapping.items()
		for source, target in entries.items()
	]
	return definition_input(data)
