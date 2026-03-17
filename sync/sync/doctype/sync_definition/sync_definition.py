# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document


class SyncDefinition(Document):
	def validate(self):
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
		if self.table_name and self.query:
			frappe.throw("Use either Table Name or Query, not both.")
		if not self.table_name and not self.query:
			frappe.throw("Either Table Name or Query is required.")
		if self.delete_missing and self.query:
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
		return _split_lines(self.frappe_modified_fields)

	def get_partner_modified_fields(self) -> list[str]:
		return _split_lines(self.partner_modified_fields)

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


def cstr(value) -> str:
	if value is None:
		return ""
	if isinstance(value, (dict, list)):
		return json.dumps(value, sort_keys=True)
	return str(value)
