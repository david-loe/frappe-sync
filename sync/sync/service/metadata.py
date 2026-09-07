from __future__ import annotations

from typing import Any

import frappe

from sync.sync.service import config_access as config_access_service
from sync.sync.service import mapping_rules as mapping_rules_service
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	SYSTEM_KEYS,
	SyncDefinitionConfig,
)


def _get_frappe_datetime_fields(doctype: str | None, field_names: list[str] | set[str]) -> set[str]:
	if not doctype:
		return set()
	candidates = {
		values_service._clean_string(field_name)
		for field_name in field_names
		if values_service._clean_string(field_name)
	}
	result = {field_name for field_name in candidates if field_name in {"modified", "creation"}}
	if not candidates:
		return result
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return result
	fieldtypes = {
		values_service._clean_string(getattr(field, "fieldname", None)): getattr(field, "fieldtype", None)
		for field in getattr(meta, "fields", [])
	}
	table_fields = {
		values_service._clean_string(getattr(field, "fieldname", None)): getattr(field, "options", None)
		for field in getattr(meta, "fields", [])
		if getattr(field, "fieldtype", None) == "Table"
	}
	for field_name in candidates:
		parsed = mapping_rules_service._parse_child_field_path(field_name)
		if parsed:
			table_field, _row_idx, child_field = parsed
			child_doctype = table_fields.get(table_field)
			if child_doctype and _doctype_fieldtype(child_doctype, child_field) == "Datetime":
				result.add(field_name)
			continue
		if fieldtypes.get(field_name) == "Datetime":
			result.add(field_name)
	return result


def _frappe_datetime_fields(config: SyncDefinitionConfig) -> set[str]:
	candidates = {
		config_access_service._config_frappe_modified_field(config),
		config_access_service._config_frappe_creation_field(config),
	}
	candidates.update(
		frappe_field
		for frappe_field, _entry in mapping_rules_service._iter_field_mapping_entries(
			getattr(config, "mapping", {})
		)
	)
	return _get_frappe_datetime_fields(getattr(config, "doctype", None), candidates)


def _partner_datetime_fields(config: SyncDefinitionConfig) -> set[str]:
	partner_fields = {
		field
		for field in (
			config_access_service._config_partner_modified_field(config),
			config_access_service._config_partner_creation_field(config),
		)
		if field
	}
	for frappe_field in _frappe_datetime_fields(config):
		partner_field = mapping_rules_service._partner_field_for_mapping(
			getattr(config, "mapping", {}), frappe_field, frappe_field
		)
		if partner_field:
			partner_fields.add(partner_field)
	return partner_fields


def _get_child_rows_by_options(parent_doc: Any, child_doctype: str) -> list[dict[str, Any]]:
	meta = frappe.get_meta(parent_doc.doctype)
	for field in meta.fields:
		if field.fieldtype != "Table" or field.options != child_doctype:
			continue
		rows = parent_doc.get(field.fieldname) or []
		return [row.as_dict() if hasattr(row, "as_dict") else dict(row) for row in rows]
	return []


def _doctype_fieldnames(doctype: str | None) -> set[str] | None:
	if not doctype:
		return None
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return None
	fieldnames = set(SYSTEM_KEYS) | {"name", "creation", "modified", "owner", "modified_by"}
	for field in getattr(meta, "fields", []) or []:
		fieldname = values_service._clean_string(getattr(field, "fieldname", None))
		if fieldname:
			fieldnames.add(fieldname)
	return fieldnames


def _doctype_table_fields(doctype: str | None) -> dict[str, str]:
	if not doctype:
		return {}
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return {}
	result: dict[str, str] = {}
	for field in getattr(meta, "fields", []) or []:
		if getattr(field, "fieldtype", None) != "Table":
			continue
		fieldname = values_service._clean_string(getattr(field, "fieldname", None))
		options = values_service._clean_string(getattr(field, "options", None))
		if fieldname and options:
			result[fieldname] = options
	return result


def _doctype_fieldtype(doctype: str | None, fieldname: str | None) -> str | None:
	if not doctype or not fieldname:
		return None
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return None
	for field in getattr(meta, "fields", []) or []:
		if getattr(field, "fieldname", None) == fieldname:
			return getattr(field, "fieldtype", None)
	return None


def _doctype_payload_allows_field(
	doctype: str,
	fieldname: str,
	doctype_fieldnames: set[str] | None,
) -> bool:
	if doctype_fieldnames is not None:
		return fieldname in doctype_fieldnames
	return _doctype_has_field(doctype, fieldname)


def _doctype_has_field(doctype: str, fieldname: str) -> bool:
	if fieldname in {"name", "creation", "modified", "owner", "modified_by"}:
		return True
	fieldnames = _doctype_fieldnames(doctype)
	if fieldnames is not None:
		return fieldname in fieldnames
	return bool(frappe.get_meta(doctype).has_field(fieldname))


def _find_field(meta: Any, candidates: list[str]) -> str | None:
	for candidate in candidates:
		if meta.has_field(candidate):
			return candidate
	return None


def _set_first_existing(payload: dict[str, Any], meta: Any, candidates: list[str], value: Any) -> str | None:
	if value is None:
		return None
	for candidate in candidates:
		if meta.has_field(candidate):
			payload[candidate] = value
			return candidate
	return None
