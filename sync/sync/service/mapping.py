from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sync.sync.constants import (
	MAPPING_DIRECTION_FRAPPE_TO_PARTNER,
	MAPPING_DIRECTION_PARTNER_TO_FRAPPE,
	VALUE_MAPPING_FALLBACK_USE_FALLBACK,
	VALUE_MAPPING_FALLBACK_USE_NULL,
)
from sync.sync.service import changes as changes_service
from sync.sync.service import config_access as config_access_service
from sync.sync.service import configuration as configuration_service
from sync.sync.service import mapping_rules as mapping_rules_service
from sync.sync.service import metadata as metadata_service
from sync.sync.service import time_utils as time_utils_service
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	VALUE_MAPPING_UNSET,
	RuntimeMappingContext,
	SyncDefinitionConfig,
)


def _build_runtime_mapping_context(config: SyncDefinitionConfig | Any) -> RuntimeMappingContext:
	mapping = mapping_rules_service._normalize_field_mapping(getattr(config, "mapping", {}) or {})
	value_mapping = dict(getattr(config, "value_mapping", {}) or {})
	value_mapping_fallbacks = configuration_service._normalize_value_mapping_fallbacks(
		getattr(config, "value_mapping_fallbacks", {}) or {}
	)
	child_table_options = metadata_service._doctype_table_fields(getattr(config, "doctype", None))
	to_partner_entries = tuple(
		(frappe_field, entry["partner_field"])
		for frappe_field, entry in mapping_rules_service._iter_field_mapping_entries(mapping)
		if mapping_rules_service._mapping_allows_direction(entry, MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	)
	to_frappe_entries = tuple(
		(frappe_field, entry["partner_field"])
		for frappe_field, entry in mapping_rules_service._iter_field_mapping_entries(mapping)
		if mapping_rules_service._mapping_allows_direction(entry, MAPPING_DIRECTION_PARTNER_TO_FRAPPE)
	)
	frappe_fields = set(mapping.keys()) | {
		config_access_service._config_frappe_modified_field(config),
		config_access_service._config_frappe_creation_field(config),
	}
	frappe_datetime_fields = metadata_service._get_frappe_datetime_fields(
		getattr(config, "doctype", None), frappe_fields
	)
	partner_datetime_fields = {
		field
		for field in (
			config_access_service._config_partner_modified_field(config),
			config_access_service._config_partner_creation_field(config),
		)
		if field
	}
	for frappe_field in frappe_datetime_fields:
		partner_field = mapping_rules_service._partner_field_for_mapping(mapping, frappe_field, frappe_field)
		if partner_field:
			partner_datetime_fields.add(partner_field)
	return RuntimeMappingContext(
		mapping=mapping,
		value_mapping=value_mapping,
		value_mapping_fallbacks=value_mapping_fallbacks,
		to_partner_entries=to_partner_entries,
		to_frappe_entries=to_frappe_entries,
		connector_mapping=dict(to_partner_entries),
		reverse_value_mapping=_build_reverse_value_mapping(value_mapping),
		frappe_datetime_fields=frappe_datetime_fields,
		partner_datetime_fields=partner_datetime_fields,
		frappe_fieldnames=metadata_service._doctype_fieldnames(getattr(config, "doctype", None)),
		child_table_options=child_table_options,
		site_time_zone=time_utils_service._site_time_zone(),
		partner_time_zone=getattr(config, "partner_time_zone", None),
	)


def _build_reverse_value_mapping(value_mapping: dict[str, dict[Any, Any]]) -> dict[str, dict[Any, Any]]:
	result: dict[str, dict[Any, Any]] = {}
	for frappe_field, field_map in value_mapping.items():
		if not isinstance(field_map, dict):
			continue
		reverse_map = {}
		for source_value, mapped_value in field_map.items():
			try:
				reverse_map[mapped_value] = source_value
			except TypeError:
				continue
		result[frappe_field] = reverse_map
	return result


def _build_ad_hoc_mapping_context(
	*,
	mapping: dict[str, Any],
	value_mapping: dict[str, dict[Any, Any]],
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None,
	doctype: str | None,
	partner_time_zone: str | None,
) -> RuntimeMappingContext:
	return _build_runtime_mapping_context(
		SimpleNamespace(
			doctype=doctype,
			mapping=mapping,
			value_mapping=value_mapping,
			value_mapping_fallbacks=value_mapping_fallbacks,
			frappe_modified_fields=[],
			partner_modified_fields=[],
			partner_time_zone=partner_time_zone,
		)
	)


def _map_frappe_to_partner(
	record: dict[str, Any],
	mapping: dict[str, Any],
	value_mapping: dict[str, dict[Any, Any]],
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None = None,
	*,
	doctype: str | None = None,
	partner_time_zone: str | None = None,
	mapping_context: RuntimeMappingContext | None = None,
) -> dict[str, Any]:
	result: dict[str, Any] = {}
	context = mapping_context or _build_ad_hoc_mapping_context(
		mapping=mapping,
		value_mapping=value_mapping,
		value_mapping_fallbacks=value_mapping_fallbacks,
		doctype=doctype,
		partner_time_zone=partner_time_zone,
	)
	for frappe_field, partner_field in context.to_partner_entries:
		value = mapping_rules_service._get_frappe_payload_value(record, frappe_field)
		field_map = context.value_mapping.get(frappe_field) or {}
		value = _mapped_value_with_fallback(
			field_map,
			value,
			_value_mapping_fallback_for_direction(
				context.value_mapping_fallbacks,
				frappe_field,
				MAPPING_DIRECTION_FRAPPE_TO_PARTNER,
			),
		)
		if frappe_field in context.frappe_datetime_fields:
			value = time_utils_service._convert_datetime_between_time_zones(
				value,
				source_time_zone=context.site_time_zone,
				target_time_zone=context.partner_time_zone or context.site_time_zone,
			)
		result[partner_field] = value
	return result


def _map_partner_to_frappe(
	record: dict[str, Any],
	mapping: dict[str, Any],
	value_mapping: dict[str, dict[Any, Any]],
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None = None,
	*,
	doctype: str | None = None,
	partner_time_zone: str | None = None,
	mapping_context: RuntimeMappingContext | None = None,
) -> dict[str, Any]:
	result: dict[str, Any] = {}
	context = mapping_context or _build_ad_hoc_mapping_context(
		mapping=mapping,
		value_mapping=value_mapping,
		value_mapping_fallbacks=value_mapping_fallbacks,
		doctype=doctype,
		partner_time_zone=partner_time_zone,
	)
	for frappe_field, partner_field in context.to_frappe_entries:
		value = record.get(partner_field)
		reverse_map = context.reverse_value_mapping.get(frappe_field) or {}
		value = _mapped_value_with_fallback(
			reverse_map,
			value,
			_value_mapping_fallback_for_direction(
				context.value_mapping_fallbacks,
				frappe_field,
				MAPPING_DIRECTION_PARTNER_TO_FRAPPE,
			),
		)
		if frappe_field in context.frappe_datetime_fields:
			value = time_utils_service._convert_datetime_between_time_zones(
				value,
				source_time_zone=context.partner_time_zone,
				target_time_zone=context.site_time_zone,
			)
		_set_frappe_payload_value(result, frappe_field, value, mapping_context=context)
	return result


def _set_frappe_payload_value(
	payload: dict[str, Any],
	fieldname: str,
	value: Any,
	*,
	mapping_context: RuntimeMappingContext | None = None,
) -> None:
	parsed = mapping_rules_service._parse_child_field_path(fieldname)
	if not parsed:
		payload[fieldname] = value
		return
	table_field, row_idx, child_field = parsed
	rows = payload.setdefault(table_field, [])
	if not isinstance(rows, list):
		rows = []
		payload[table_field] = rows
	while len(rows) < row_idx:
		child_row: dict[str, Any] = {}
		child_doctype = (mapping_context.child_table_options if mapping_context else {}).get(table_field)
		if child_doctype:
			child_row["doctype"] = child_doctype
		rows.append(child_row)
	row = rows[row_idx - 1]
	if not isinstance(row, dict):
		row = {}
		rows[row_idx - 1] = row
	child_doctype = (mapping_context.child_table_options if mapping_context else {}).get(table_field)
	if child_doctype:
		row.setdefault("doctype", child_doctype)
	row[child_field] = value


def _frappe_diff_field_names(payload: dict[str, Any], mapping_context: RuntimeMappingContext) -> list[str]:
	fields: list[str] = []
	child_tables: set[str] = set()
	for frappe_field, _partner_field in mapping_context.to_frappe_entries:
		parsed = mapping_rules_service._parse_child_field_path(frappe_field)
		if parsed:
			if parsed[0] in payload:
				fields.append(frappe_field)
				child_tables.add(parsed[0])
			continue
		if frappe_field in payload:
			fields.append(frappe_field)
	for fieldname in payload:
		if fieldname in fields or fieldname in child_tables:
			continue
		fields.append(fieldname)
	return fields or list(payload.keys())


def _mapped_value_with_fallback(
	field_map: dict[Any, Any], value: Any, fallback: dict[str, Any] | None
) -> Any:
	mapped = _mapped_value(field_map, value, default=VALUE_MAPPING_UNSET)
	if mapped is not VALUE_MAPPING_UNSET:
		return mapped
	return _apply_value_mapping_fallback(value, fallback)


def _mapped_value(field_map: dict[Any, Any], value: Any, *, default: Any = None) -> Any:
	try:
		if value in field_map:
			return field_map[value]
	except TypeError:
		pass
	normalized_value = values_service._normalize_comparable_scalar_value(value)
	for source_value, target_value in field_map.items():
		if values_service._normalize_comparable_scalar_value(source_value) == normalized_value:
			return target_value
	return default


def _with_partner_timestamps(
	config: SyncDefinitionConfig | Any,
	frappe_record: dict[str, Any],
	payload: dict[str, Any],
	*,
	create: bool,
	mapping_context: RuntimeMappingContext,
) -> dict[str, Any]:
	result = dict(payload)
	partner_modified_field = config_access_service._config_partner_modified_field(config)
	partner_creation_field = config_access_service._config_partner_creation_field(config)
	effective_modified = changes_service._effective_modified(
		frappe_record,
		modified_field=config_access_service._config_frappe_modified_field(config),
		creation_field=config_access_service._config_frappe_creation_field(config),
		target_time_zone=mapping_context.site_time_zone,
	)
	if partner_modified_field and effective_modified is not None:
		result[partner_modified_field] = time_utils_service._convert_datetime_between_time_zones(
			effective_modified,
			source_time_zone=mapping_context.site_time_zone,
			target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
		)
	if create and partner_creation_field:
		creation_value = time_utils_service._parse_datetime(
			frappe_record.get(config_access_service._config_frappe_creation_field(config)),
			target_time_zone=mapping_context.site_time_zone,
		)
		if creation_value is not None:
			result[partner_creation_field] = time_utils_service._convert_datetime_between_time_zones(
				creation_value,
				source_time_zone=mapping_context.site_time_zone,
				target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
			)
	return result


def _with_frappe_modified_timestamp(
	config: SyncDefinitionConfig | Any,
	partner_record: dict[str, Any],
	payload: dict[str, Any],
	*,
	mapping_context: RuntimeMappingContext,
) -> dict[str, Any]:
	result = dict(payload)
	partner_modified_field = config_access_service._config_partner_modified_field(config)
	if not partner_modified_field:
		result.pop(config_access_service._config_frappe_creation_field(config), None)
		return result
	effective_modified = changes_service._effective_modified(
		partner_record,
		modified_field=partner_modified_field,
		creation_field=config_access_service._config_partner_creation_field(config),
		assumed_time_zone=getattr(config, "partner_time_zone", None),
		target_time_zone=mapping_context.site_time_zone,
	)
	if effective_modified is not None:
		result[config_access_service._config_frappe_modified_field(config)] = effective_modified
	result.pop(config_access_service._config_frappe_creation_field(config), None)
	return result


def _value_mapping_fallback_for_direction(
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None,
	frappe_field: str,
	direction: str,
) -> dict[str, Any] | None:
	if not isinstance(value_mapping_fallbacks, dict):
		return None
	field_fallbacks = value_mapping_fallbacks.get(frappe_field)
	if not isinstance(field_fallbacks, dict):
		return None
	return field_fallbacks if "action" in field_fallbacks else None


def _apply_value_mapping_fallback(value: Any, fallback: dict[str, Any] | None) -> Any:
	if not fallback:
		return value
	action = configuration_service._normalize_value_mapping_fallback_action(fallback.get("action"))
	if action == VALUE_MAPPING_FALLBACK_USE_FALLBACK:
		return fallback.get("value")
	if action == VALUE_MAPPING_FALLBACK_USE_NULL:
		return None
	return value


def _apply_partner_link_fields(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	partner_payload: dict[str, Any],
) -> dict[str, Any]:
	payload = dict(partner_payload)
	partner_frappe_field = config_access_service._config_partner_frappe_identity_field(config)
	if partner_frappe_field and frappe_record.get("name") not in (None, ""):
		payload[partner_frappe_field] = frappe_record.get("name")
	return payload
