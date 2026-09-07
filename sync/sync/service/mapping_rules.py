from __future__ import annotations

import json
from typing import Any

import frappe

from sync.sync.constants import (
	MAPPING_DIRECTION_BOTH,
	MAPPING_DIRECTION_FRAPPE_TO_PARTNER,
	MAPPING_DIRECTION_PARTNER_TO_FRAPPE,
)
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	CHILD_FIELD_PATH_SEPARATOR,
	SYNC_TYPE_BIDIRECTIONAL,
)


def _field_mapping_row_fieldname(row: Any) -> str | None:
	table_field = values_service._clean_string(values_service._first_value_dict(row, ["table_field"]))
	row_idx = values_service._clean_string(
		values_service._first_value_dict(row, ["row_idx", "child_row_idx"])
	)
	child_field = values_service._clean_string(values_service._first_value_dict(row, ["child_field"]))
	if table_field and row_idx and child_field:
		return CHILD_FIELD_PATH_SEPARATOR.join((table_field, row_idx, child_field))
	return values_service._clean_string(
		values_service._first_value_dict(
			row,
			["frappe_field", "source_field", "doctype_field", "source_fieldname", "field_name"],
		)
	)


def _mapping_entry_value(raw_entry: Any, candidates: list[str], default: Any = None) -> Any:
	if isinstance(raw_entry, dict):
		return values_service._first_value_dict(raw_entry, candidates, default=default)
	for candidate in candidates:
		value = getattr(raw_entry, candidate, None)
		if value not in (None, ""):
			return value
	return default


def _normalize_mapping_direction(direction: Any) -> str:
	value = values_service._clean_string(direction)
	if not value:
		return MAPPING_DIRECTION_BOTH
	normalized = value.lower()
	if normalized in {"frappe <-> partner", "bidirectional"}:
		return MAPPING_DIRECTION_BOTH
	if normalized in {"frappe -> partner", "frappe_to_partner"}:
		return MAPPING_DIRECTION_FRAPPE_TO_PARTNER
	if normalized in {"frappe <- partner", "partner_to_frappe"}:
		return MAPPING_DIRECTION_PARTNER_TO_FRAPPE
	frappe.throw("Direction must be one of: Frappe <-> Partner, Frappe -> Partner, Frappe <- Partner")


def _one_way_mapping_direction(sync_type: Any) -> str | None:
	sync_type = values_service._clean_string(sync_type)
	if sync_type in {MAPPING_DIRECTION_FRAPPE_TO_PARTNER, MAPPING_DIRECTION_PARTNER_TO_FRAPPE}:
		return sync_type
	return None


def _normalize_field_mapping_entry(raw_entry: Any, *, sync_type: str | None = None) -> dict[str, str] | None:
	if raw_entry in (None, ""):
		return None
	if isinstance(raw_entry, str):
		partner_field = raw_entry
		direction = MAPPING_DIRECTION_BOTH
	else:
		partner_field = _mapping_entry_value(
			raw_entry,
			[
				"partner_field",
				"partnerField",
				"target_field",
				"external_field",
				"partner_column",
				"column_name",
			],
		)
		direction = _mapping_entry_value(raw_entry, ["direction", "label_direction", "sync_direction"])
	normalized_direction = _normalize_mapping_direction(direction)
	partner_field = values_service._clean_string(partner_field)
	if not partner_field:
		return None
	return {
		"partner_field": partner_field,
		"direction": _one_way_mapping_direction(sync_type) or normalized_direction,
	}


def _iter_field_mapping_entries(mapping: Any):
	if not isinstance(mapping, dict):
		return
	for frappe_field, raw_entry in mapping.items():
		frappe_field = values_service._clean_string(frappe_field)
		entry = _normalize_field_mapping_entry(raw_entry)
		if frappe_field and entry:
			yield frappe_field, entry


def _normalize_field_mapping(mapping: Any) -> dict[str, dict[str, str]]:
	if isinstance(mapping, str):
		try:
			mapping = json.loads(mapping)
		except Exception:
			return {}
	if not isinstance(mapping, dict):
		return {}
	return {frappe_field: entry for frappe_field, entry in _iter_field_mapping_entries(mapping)}


def _force_mapping_direction(mapping: dict[str, Any], sync_type: str | None) -> dict[str, dict[str, str]]:
	direction = _one_way_mapping_direction(sync_type)
	if not direction:
		return _normalize_field_mapping(mapping)
	return {
		frappe_field: {"partner_field": entry["partner_field"], "direction": direction}
		for frappe_field, entry in _iter_field_mapping_entries(mapping)
	}


def _mapping_allows_direction(mapping_entry: dict[str, str], direction: str) -> bool:
	entry_direction = _normalize_mapping_direction(mapping_entry.get("direction"))
	required_direction = _normalize_mapping_direction(direction)
	return entry_direction in {MAPPING_DIRECTION_BOTH, required_direction}


def _partner_field_for_mapping(
	mapping: dict[str, Any], frappe_field: str, default: str | None = None
) -> str | None:
	raw_mapping = mapping if isinstance(mapping, dict) else {}
	entry = _normalize_field_mapping_entry(raw_mapping.get(frappe_field))
	if entry:
		return entry["partner_field"]
	return default if default is not None else frappe_field


def _flatten_mapping_for_direction(mapping: dict[str, Any], direction: str) -> dict[str, str]:
	result: dict[str, str] = {}
	for frappe_field, entry in _iter_field_mapping_entries(mapping):
		if _mapping_allows_direction(entry, direction):
			result[frappe_field] = entry["partner_field"]
	return result


def _mapping_fields_for_sync_type(mapping: dict[str, Any], sync_type: str) -> set[str]:
	required_directions = _required_mapping_directions(sync_type)
	if not required_directions:
		return {frappe_field for frappe_field, _entry in _iter_field_mapping_entries(mapping)}
	return {
		frappe_field
		for frappe_field, entry in _iter_field_mapping_entries(mapping)
		if any(_mapping_allows_direction(entry, direction) for direction in required_directions)
	}


def _parent_mapping_fields_for_sync_type(mapping: dict[str, Any], sync_type: str) -> set[str]:
	return {
		fieldname
		for fieldname in _mapping_fields_for_sync_type(mapping, sync_type)
		if not _parse_child_field_path(fieldname)
	}


def _parse_child_field_path(fieldname: Any) -> tuple[str, int, str] | None:
	cleaned = values_service._clean_string(fieldname)
	if not cleaned:
		return None
	parts = cleaned.split(CHILD_FIELD_PATH_SEPARATOR)
	if len(parts) != 3:
		return None
	table_field, row_idx, child_field = (values_service._clean_string(part) for part in parts)
	if not table_field or not row_idx or not child_field:
		return None
	try:
		row_number = int(row_idx)
	except Exception:
		return None
	if row_number < 1:
		return None
	return table_field, row_number, child_field


def _child_table_fields_for_mapping(mapping: dict[str, Any], sync_type: str | None = None) -> set[str]:
	fields = _mapping_fields_for_sync_type(mapping, sync_type or SYNC_TYPE_BIDIRECTIONAL)
	result: set[str] = set()
	for fieldname in fields:
		parsed = _parse_child_field_path(fieldname)
		if parsed:
			result.add(parsed[0])
	return result


def _required_mapping_directions(sync_type: str) -> list[str]:
	required: list[str] = []
	if sync_type in {"Frappe -> Partner", "Frappe <-> Partner"}:
		required.append(MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	if sync_type in {"Frappe <- Partner", "Frappe <-> Partner"}:
		required.append(MAPPING_DIRECTION_PARTNER_TO_FRAPPE)
	if not required:
		required.append(MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	return required


def _get_frappe_payload_value(record: dict[str, Any], fieldname: str) -> Any:
	parsed = _parse_child_field_path(fieldname)
	if not parsed:
		return record.get(fieldname)
	table_field, row_idx, child_field = parsed
	rows = record.get(table_field)
	if not isinstance(rows, list) or len(rows) < row_idx:
		return None
	row = rows[row_idx - 1]
	if isinstance(row, dict):
		return row.get(child_field)
	getter = getattr(row, "get", None)
	if callable(getter):
		return getter(child_field)
	return getattr(row, child_field, None)
