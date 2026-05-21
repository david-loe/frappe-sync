# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document

MAPPING_DIRECTIONS = ("Frappe <-> Partner", "Frappe -> Partner", "Frappe <- Partner")


class SyncDefinition(Document):
	def validate(self):
		SyncDefinition.validate_field_mapping(self)
		SyncDefinition.validate_match_fields(self)
		SyncDefinition.validate_source_settings(self)
		SyncDefinition.validate_filter_expression(self)
		SyncDefinition.validate_modified_fields(self)
		SyncDefinition.validate_identity_settings(self)
		SyncDefinition.validate_one_way_match_mode(self)
		SyncDefinition.validate_preview_limit(self)

	def on_trash(self):
		for run_name in _linked_names("Sync Run", {"sync_definition": self.name}):
			frappe.delete_doc("Sync Run", run_name, ignore_permissions=True)

		for item_name in _linked_names("Sync Run Item", {"sync_definition": self.name}):
			frappe.delete_doc("Sync Run Item", item_name, ignore_permissions=True)

	def validate_field_mapping(self):
		seen: set[str] = set()
		duplicates: list[str] = []
		for row in self.field_mapping or []:
			entry = _normalize_field_mapping_row(row)
			if not entry:
				continue
			_assign_row_value(row, "frappe_field", entry["frappe_field"])
			_assign_row_value(row, "partner_field", entry["partner_field"])
			_assign_row_value(row, "direction", entry["direction"])
			if entry["frappe_field"] in seen:
				duplicates.append(entry["frappe_field"])
				continue
			seen.add(entry["frappe_field"])
		if duplicates:
			frappe.throw(f"Field Mapping contains duplicate Frappe fields: {', '.join(sorted(set(duplicates)))}")

	def validate_match_fields(self):
		mapping_fields = set(self.get_field_mapping().keys())
		missing = [field for field in SyncDefinition.get_match_fields(self) if field not in mapping_fields]
		if missing:
			frappe.throw(f"Match fields must exist in field mapping: {', '.join(missing)}")

	def validate_source_settings(self):
		table_name = _clean_value(self.table_name)
		read_query = _clean_value(getattr(self, "read_query", None))
		if not table_name:
			frappe.throw("Table Name is required.")
		if read_query and getattr(self, "delete_missing", None):
			frappe.throw("Delete Missing cannot be used together with Read Query.")
		self.table_name = table_name
		self.read_query = read_query

	def validate_modified_fields(self):
		if not self.use_last_sync_date:
			return
		if not self.get_frappe_modified_fields():
			frappe.throw("At least one Frappe Modified Field is required when delta sync is enabled.")
		if not self.get_partner_modified_fields():
			frappe.throw("At least one Partner Modified Field is required when delta sync is enabled.")

	def validate_identity_settings(self):
		strategy = _clean_value(self.partner_create_id_strategy) or "payload"
		identity_field = _clean_value(self.partner_identity_field)
		source = _clean_value(self.partner_create_id_source)
		scope_where = _clean_value(self.partner_create_id_scope_where)

		self.partner_create_id_strategy = strategy
		self.partner_identity_field = identity_field
		self.partner_create_id_source = source
		self.partner_create_id_scope_where = scope_where
		self.frappe_partner_identity_field = _clean_value(self.frappe_partner_identity_field)
		self.partner_frappe_identity_field = _clean_value(self.partner_frappe_identity_field)

		if strategy not in {"payload", "connector_default", "sequence", "max_plus_one"}:
			frappe.throw("Partner Create ID Strategy must be one of: payload, connector_default, sequence, max_plus_one.")
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
		doctype_name = _clean_value(getattr(self, "doctype_name", None))
		if self.frappe_partner_identity_field and doctype_name:
			meta = frappe.get_meta(doctype_name)
			valid_fields = {"name"} | {field.fieldname for field in getattr(meta, "fields", []) or []}
			if self.frappe_partner_identity_field not in valid_fields:
				frappe.throw(f"Frappe Partner Identity Field does not exist on {doctype_name}.")

	def validate_filter_expression(self):
		self.filter_expression = _normalize_filter_expression(self.filter_expression)

	def validate_preview_limit(self):
		if self.preview_limit is not None and self.preview_limit < 1:
			frappe.throw("Preview Limit must be at least 1.")

	def validate_one_way_match_mode(self):
		mode = _clean_value(getattr(self, "one_way_match_mode", None)) or "first_match"
		if mode not in {"first_match", "all_matches"}:
			frappe.throw("One-Way Match Mode must be one of: first_match, all_matches.")
		self.one_way_match_mode = mode

	def get_match_fields(self) -> list[str]:
		fields: list[str] = []
		for row in getattr(self, "match_fields", None) or []:
			field = _clean_value(_get_row_value(row, "frappe_field"))
			if field:
				fields.append(field)
		return fields

	def get_field_mapping(self) -> dict[str, dict[str, str]]:
		mapping = {}
		for row in self.field_mapping or []:
			entry = _normalize_field_mapping_row(row)
			if not entry:
				continue
			mapping[entry["frappe_field"]] = {
				"partner_field": entry["partner_field"],
				"direction": entry["direction"],
			}
		return mapping

	def get_value_mapping(self) -> dict[str, dict[str, str]]:
		result: dict[str, dict[str, str]] = {}
		for row in self.value_mapping or []:
			frappe_field = _clean_value(_get_row_value(row, "frappe_field"))
			if not frappe_field:
				continue
			field_map = result.setdefault(frappe_field, {})
			field_map[cstr(_get_row_value(row, "frappe_value"))] = cstr(_get_row_value(row, "partner_value"))
		return result

	def get_frappe_modified_fields(self) -> list[str]:
		return _extract_modified_fields(getattr(self, "frappe_modified_field_rows", None))

	def get_partner_modified_fields(self) -> list[str]:
		return _extract_modified_fields(getattr(self, "partner_modified_field_rows", None))

	def as_export_dict(self) -> dict:
		return {
			"name": self.name,
			"title": self.title,
			"enabled": self.enabled,
			"partner": self.partner,
			"sync_type": self.sync_type,
			"doctype_name": self.doctype_name,
			"frequency_cron": self.frequency_cron,
			"next_run_at": self.next_run_at,
			"filter_expression": self.filter_expression,
			"batch_size": self.batch_size,
			"use_last_sync_date": self.use_last_sync_date,
			"timestamp_buffer_seconds": self.timestamp_buffer_seconds,
			"create_new": self.create_new,
			"delete_missing": self.delete_missing,
			"one_way_match_mode": getattr(self, "one_way_match_mode", "first_match"),
			"conflict_policy": self.conflict_policy,
			"table_name": self.table_name,
			"read_query": getattr(self, "read_query", None),
			"preview_limit": self.get_preview_limit(),
			"export_mask_credentials": bool(self.export_mask_credentials),
			"frappe_modified_fields": SyncDefinition.get_frappe_modified_fields(self),
			"partner_modified_fields": SyncDefinition.get_partner_modified_fields(self),
			"match_fields": SyncDefinition.get_match_fields(self),
			"field_mapping": SyncDefinition.get_field_mapping(self),
			"value_mapping": SyncDefinition.get_value_mapping(self),
			"partner_identity_field": getattr(self, "partner_identity_field", None),
			"frappe_partner_identity_field": getattr(self, "frappe_partner_identity_field", None),
			"partner_frappe_identity_field": getattr(self, "partner_frappe_identity_field", None),
			"partner_create_id_strategy": getattr(self, "partner_create_id_strategy", "payload"),
			"partner_create_id_source": getattr(self, "partner_create_id_source", None),
			"partner_create_id_scope_where": getattr(self, "partner_create_id_scope_where", None),
			"last_run_status": self.last_run_status,
			"last_run_summary": self.last_run_summary,
		}

	def get_preview_limit(self) -> int:
		try:
			return int(self.preview_limit or 50)
		except Exception:
			return 50

	def get_export_payload(self) -> dict:
		return {
			"sync_definition": self.as_export_dict(),
			"mask_credentials": bool(self.export_mask_credentials),
		}


def _split_lines(value: str | None) -> list[str]:
	if not value:
		return []
	return [line.strip() for line in value.splitlines() if line.strip()]


def _linked_names(doctype: str, filters: dict) -> list[str]:
	rows = frappe.get_all(doctype, filters=filters, fields=["name"], order_by=None)
	return [row.name if hasattr(row, "name") else row.get("name") for row in rows]


def _clean_value(value: str | None) -> str | None:
	if value is None:
		return None
	value = str(value).strip()
	return value or None


def _extract_modified_fields(rows) -> list[str]:
	result: list[str] = []
	for row in rows or []:
		clean_value = _clean_value(_get_row_value(row, "field_name", "modified_field", "frappe_field"))
		if clean_value:
			result.append(clean_value)
	return [value for value in result if value]


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


def _assign_row_value(row, fieldname: str, value):
	if row is None:
		return
	try:
		setattr(row, fieldname, value)
	except Exception:
		if hasattr(row, "update"):
			row.update({fieldname: value})


def _normalize_mapping_direction(value, *, default: str = "Frappe <-> Partner") -> str:
	direction = _clean_value(value) or default
	if direction not in MAPPING_DIRECTIONS:
		frappe.throw(f"Direction must be one of: {', '.join(MAPPING_DIRECTIONS)}")
	return direction


def _normalize_field_mapping_row(row) -> dict[str, str] | None:
	frappe_field = _clean_value(
		_get_row_value(row, "frappe_field", "source_field", "doctype_field", "field_name")
	)
	partner_field = _clean_value(
		_get_row_value(row, "partner_field", "target_field", "external_field", "column_name")
	)
	if not frappe_field or not partner_field:
		return None
	return {
		"frappe_field": frappe_field,
		"partner_field": partner_field,
		"direction": _normalize_mapping_direction(_get_row_value(row, "direction")),
	}


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
