# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document


class SyncDefinition(Document):
	def validate(self):
		self.ensure_modified_field_rows_from_legacy()
		self.sync_modified_fields_legacy_storage()
		self.validate_key_fields()
		self.validate_source_settings()
		self.validate_modified_fields()
		self.validate_preview_limit()

	def validate_key_fields(self):
		mapping_fields = {row.frappe_field for row in self.field_mapping or [] if row.frappe_field}
		missing = [row.frappe_field for row in self.key_fields or [] if row.frappe_field not in mapping_fields]
		if missing:
			frappe.throw(f"Key fields must exist in field mapping: {', '.join(missing)}")

	def validate_source_settings(self):
		table_name = _clean_value(self.table_name)
		query = _clean_value(self.query)
		if table_name and query:
			frappe.throw("Use either Table Name or Query, not both.")
		if not table_name and not query:
			frappe.throw("Either Table Name or Query is required.")
		if self.delete_missing and query:
			frappe.throw("Delete Missing cannot be enabled when Query is used.")

	def validate_modified_fields(self):
		if not self.use_last_sync_date:
			return
		if not self.get_frappe_modified_fields():
			frappe.throw("At least one Frappe Modified Field is required when delta sync is enabled.")
		if not self.get_partner_modified_fields():
			frappe.throw("At least one Partner Modified Field is required when delta sync is enabled.")

	def validate_preview_limit(self):
		if self.preview_limit is not None and self.preview_limit < 1:
			frappe.throw("Preview Limit must be at least 1.")

	def get_key_fields(self) -> list[str]:
		return [row.frappe_field for row in self.key_fields or [] if row.frappe_field]

	def get_field_mapping(self) -> dict[str, dict[str, str]]:
		mapping = {}
		for row in self.field_mapping or []:
			if not row.frappe_field or not row.partner_field:
				continue
			mapping[row.frappe_field] = {
				"partner_field": row.partner_field,
				"direction": row.direction or "Both",
			}
		return mapping

	def get_value_mapping(self) -> dict[str, dict[str, str]]:
		result: dict[str, dict[str, str]] = {}
		for row in self.value_mapping or []:
			if not row.frappe_field:
				continue
			field_map = result.setdefault(row.frappe_field, {})
			field_map[cstr(row.frappe_value)] = cstr(row.partner_value)
		return result

	def get_frappe_modified_fields(self) -> list[str]:
		return _extract_modified_fields(getattr(self, "frappe_modified_field_rows", None)) or _split_lines(self.frappe_modified_fields)

	def get_partner_modified_fields(self) -> list[str]:
		return _extract_modified_fields(getattr(self, "partner_modified_field_rows", None)) or _split_lines(self.partner_modified_fields)

	def ensure_modified_field_rows_from_legacy(self):
		self._ensure_modified_field_rows("frappe_modified_field_rows", "frappe_modified_fields")
		self._ensure_modified_field_rows("partner_modified_field_rows", "partner_modified_fields")

	def _ensure_modified_field_rows(self, table_fieldname: str, legacy_fieldname: str):
		if self.get(table_fieldname):
			return
		for fieldname in _split_lines(self.get(legacy_fieldname)):
			self.append(table_fieldname, {"field_name": fieldname})

	def sync_modified_fields_legacy_storage(self):
		self.frappe_modified_fields = "\n".join(self.get_frappe_modified_fields())
		self.partner_modified_fields = "\n".join(self.get_partner_modified_fields())

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
			"conflict_policy": self.conflict_policy,
			"table_name": self.table_name,
			"query": self.query,
			"preview_limit": self.get_preview_limit(),
			"export_mask_credentials": bool(self.export_mask_credentials),
			"frappe_modified_fields": self.get_frappe_modified_fields(),
			"partner_modified_fields": self.get_partner_modified_fields(),
			"key_fields": self.get_key_fields(),
			"field_mapping": self.get_field_mapping(),
			"value_mapping": self.get_value_mapping(),
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


def _clean_value(value: str | None) -> str | None:
	if value is None:
		return None
	value = str(value).strip()
	return value or None


def _extract_modified_fields(rows) -> list[str]:
	result: list[str] = []
	for row in rows or []:
		value = None
		if hasattr(row, "get"):
			value = row.get("field_name") or row.get("modified_field") or row.get("frappe_field")
		else:
			value = getattr(row, "field_name", None) or getattr(row, "modified_field", None) or getattr(row, "frappe_field", None)
		clean_value = _clean_value(value)
		if clean_value:
			result.append(clean_value)
	return [value for value in result if value]


def cstr(value) -> str:
	if value is None:
		return ""
	if isinstance(value, (dict, list)):
		return json.dumps(value, sort_keys=True)
	return str(value)
