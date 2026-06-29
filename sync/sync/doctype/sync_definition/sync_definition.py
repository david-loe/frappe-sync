# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document

MAPPING_DIRECTIONS = ("Frappe <-> Partner", "Frappe -> Partner", "Frappe <- Partner")
UNMAPPED_ACTION_KEEP_ORIGINAL = "Keep Original"
UNMAPPED_ACTION_USE_FALLBACK = "Use Fallback Value"
UNMAPPED_ACTION_USE_NULL = "Use NULL"
UNMAPPED_ACTIONS = (
	UNMAPPED_ACTION_KEEP_ORIGINAL,
	UNMAPPED_ACTION_USE_FALLBACK,
	UNMAPPED_ACTION_USE_NULL,
)
UNMAPPED_ACTION_KEYS = {
	UNMAPPED_ACTION_KEEP_ORIGINAL: "keep_original",
	UNMAPPED_ACTION_USE_FALLBACK: "fallback",
	UNMAPPED_ACTION_USE_NULL: "null",
}


class SyncDefinition(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from sync.sync.doctype.sync_field_mapping.sync_field_mapping import SyncFieldMapping
		from sync.sync.doctype.sync_key_field.sync_key_field import SyncKeyField
		from sync.sync.doctype.sync_value_mapping.sync_value_mapping import SyncValueMapping

		batch_size: DF.Int
		capture_audit_payloads: DF.Check
		conflict_policy: DF.Literal["newest_wins"]
		create_new: DF.Check
		delete_missing: DF.Check
		doctype_name: DF.Link
		enabled: DF.Check
		export_mask_credentials: DF.Check
		field_mapping: DF.Table[SyncFieldMapping]
		filter_expression: DF.Code | None
		frappe_creation_field: DF.Data
		frappe_modified_field: DF.Literal[None]
		frappe_partner_identity_field: DF.Literal[None]
		frequency_cron: DF.Data
		last_run: DF.Link | None
		last_run_status: DF.Literal["", "Queued", "Running", "Success", "Partial Error", "Needs Review", "Error", "Preview", "Skipped"]
		last_run_summary: DF.SmallText | None
		last_successful_sync: DF.Datetime | None
		last_sync_at: DF.Datetime | None
		match_fields: DF.Table[SyncKeyField]
		next_run_at: DF.Datetime | None
		one_way_match_mode: DF.Literal["first_match", "all_matches"]
		partner: DF.Link
		partner_columns: DF.JSON | None
		partner_columns_loaded_at: DF.Datetime | None
		partner_columns_signature: DF.Data | None
		partner_create_id_scope_where: DF.Code | None
		partner_create_id_source: DF.Data | None
		partner_create_id_strategy: DF.Literal["payload", "connector_default", "sequence", "max_plus_one"]
		partner_creation_field: DF.Literal[None]
		partner_frappe_identity_field: DF.Literal[None]
		partner_identity_field: DF.Literal[None]
		partner_modified_field: DF.Literal[None]
		preview_limit: DF.Int
		read_query: DF.Code | None
		sync_type: DF.Literal["Frappe -> Partner", "Frappe <-> Partner", "Frappe <- Partner"]
		table_name: DF.Data | None
		timestamp_buffer_ms: DF.Int
		timestamp_tie_breaker: DF.Literal["Manual", "Frappe Wins", "Partner Wins"]
		title: DF.Data
		use_last_sync_date: DF.Check
		value_mapping: DF.Table[SyncValueMapping]
	# end: auto-generated types

	def validate(self):
		SyncDefinition.validate_field_mapping(self)
		SyncDefinition.validate_value_mapping(self)
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
			entry = _normalize_field_mapping_row(row, sync_type=getattr(self, "sync_type", None))
			if not entry:
				continue
			_assign_row_value(row, "frappe_field", entry["frappe_field"])
			_assign_row_value(row, "partner_field", entry["partner_field"])
			_assign_row_value(row, "direction", entry["direction"])
			_normalize_field_mapping_fallbacks(row)
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

	def validate_value_mapping(self):
		for row in self.value_mapping or []:
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

	def validate_source_settings(self):
		table_name = _clean_value(self.table_name)
		read_query = _clean_value(getattr(self, "read_query", None))
		if not _one_way_mapping_direction(getattr(self, "sync_type", None)):
			self.delete_missing = 0
		if not table_name and not _read_query_can_replace_table_name(getattr(self, "sync_type", None), read_query):
			frappe.throw("Table Name is required.")
		if read_query and getattr(self, "delete_missing", None):
			frappe.throw("Delete Missing cannot be used together with Read Query.")
		self.table_name = table_name
		self.read_query = read_query

	def validate_modified_fields(self):
		self.frappe_creation_field = "creation"
		self.frappe_modified_field = _clean_value(getattr(self, "frappe_modified_field", None)) or "modified"
		self.partner_modified_field = _clean_value(getattr(self, "partner_modified_field", None))
		self.partner_creation_field = _clean_value(getattr(self, "partner_creation_field", None))
		self.timestamp_tie_breaker = _clean_value(getattr(self, "timestamp_tie_breaker", None)) or "Manual"

		partner_timestamps_required = _partner_timestamps_required(self)
		if partner_timestamps_required and not self.partner_modified_field:
			frappe.throw("Partner Modified Field is required.")
		if partner_timestamps_required and not self.partner_creation_field:
			frappe.throw("Partner Creation Field is required.")
		if self.frappe_modified_field == self.frappe_creation_field:
			frappe.throw("Frappe Modified Field and Frappe Creation Field must be different.")
		if (
			self.partner_modified_field
			and self.partner_creation_field
			and self.partner_modified_field == self.partner_creation_field
		):
			frappe.throw("Partner Modified Field and Partner Creation Field must be different.")
		if _clean_value(getattr(self, "sync_type", None)) != "Frappe <-> Partner":
			self.timestamp_tie_breaker = "Manual"
		if self.timestamp_tie_breaker not in {"Manual", "Frappe Wins", "Partner Wins"}:
			frappe.throw("Timestamp Tie Breaker must be one of: Manual, Frappe Wins, Partner Wins.")

		doctype_name = _clean_value(getattr(self, "doctype_name", None))
		if doctype_name:
			meta = frappe.get_meta(doctype_name)
			valid_fields = {"name", "creation", "modified"} | {
				field.fieldname for field in getattr(meta, "fields", []) or []
			}
			if self.frappe_modified_field not in valid_fields:
				frappe.throw(f"Frappe Modified Field does not exist on {doctype_name}.")

		timestamp_fields = {
			self.frappe_modified_field,
			self.frappe_creation_field,
		}
		mapped_timestamp_fields = timestamp_fields & set(self.get_field_mapping())
		if mapped_timestamp_fields:
			frappe.throw(
				"Dedicated timestamp fields must not also exist in Field Mapping: "
				+ ", ".join(sorted(mapped_timestamp_fields))
			)
		partner_timestamp_fields = {self.partner_modified_field, self.partner_creation_field} - {None}
		mapped_partner_timestamp_fields = partner_timestamp_fields & {
			entry["partner_field"] for entry in self.get_field_mapping().values()
		}
		if mapped_partner_timestamp_fields:
			frappe.throw(
				"Dedicated partner timestamp fields must not also exist in Field Mapping: "
				+ ", ".join(sorted(mapped_partner_timestamp_fields))
			)

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
			entry = _normalize_field_mapping_row(row, sync_type=getattr(self, "sync_type", None))
			if not entry:
				continue
			mapping[entry["frappe_field"]] = {
				"partner_field": entry["partner_field"],
				"direction": entry["direction"],
			}
		return mapping

	def get_value_mapping(self) -> dict[str, dict[object, object]]:
		result: dict[str, dict[object, object]] = {}
		for row in self.value_mapping or []:
			frappe_field = _clean_value(_get_row_value(row, "frappe_field"))
			if not frappe_field:
				continue
			field_map = result.setdefault(frappe_field, {})
			frappe_value = (
				None
				if _row_flag(row, "frappe_value_is_null")
				else cstr(_get_raw_row_value(row, "frappe_value"))
			)
			partner_value = (
				None
				if _row_flag(row, "partner_value_is_null")
				else cstr(_get_raw_row_value(row, "partner_value"))
			)
			field_map[frappe_value] = partner_value
		return result

	def get_value_mapping_fallbacks(self) -> dict[str, dict[str, str | None]]:
		result: dict[str, dict[str, str | None]] = {}
		for row in self.field_mapping or []:
			entry = _normalize_field_mapping_row(row, sync_type=getattr(self, "sync_type", None))
			if not entry:
				continue
			result[entry["frappe_field"]] = _get_unmapped_action_config(
				_get_row_value(row, "unmapped_action"),
				_get_row_value(row, "fallback_value"),
			)
		return result

	def get_frappe_modified_fields(self) -> list[str]:
		fieldname = _clean_value(getattr(self, "frappe_modified_field", None)) or "modified"
		return [fieldname]

	def get_partner_modified_fields(self) -> list[str]:
		fieldname = _clean_value(getattr(self, "partner_modified_field", None))
		return [fieldname] if fieldname else []

	def as_export_dict(self) -> dict:
		return {
			"name": self.name,
			"title": self.title,
			"enabled": self.enabled,
			"partner": self.partner,
			"sync_type": self.sync_type,
			"doctype_name": self.doctype_name,
			"frequency_cron": self.frequency_cron,
			"filter_expression": self.filter_expression,
			"batch_size": self.batch_size,
			"use_last_sync_date": self.use_last_sync_date,
			"timestamp_buffer_ms": self.timestamp_buffer_ms,
			"create_new": self.create_new,
			"delete_missing": self.delete_missing,
			"one_way_match_mode": getattr(self, "one_way_match_mode", "first_match"),
			"conflict_policy": self.conflict_policy,
			"table_name": self.table_name,
			"read_query": getattr(self, "read_query", None),
			"preview_limit": self.get_preview_limit(),
			"export_mask_credentials": bool(self.export_mask_credentials),
			"frappe_modified_field": getattr(self, "frappe_modified_field", "modified"),
			"frappe_creation_field": "creation",
			"partner_modified_field": getattr(self, "partner_modified_field", None),
			"partner_creation_field": getattr(self, "partner_creation_field", None),
			"timestamp_tie_breaker": getattr(self, "timestamp_tie_breaker", "Manual"),
			"match_fields": SyncDefinition.get_match_fields(self),
			"field_mapping": SyncDefinition.get_field_mapping(self),
			"value_mapping": SyncDefinition.get_value_mapping(self),
			"value_mapping_fallbacks": SyncDefinition.get_value_mapping_fallbacks(self),
			"partner_identity_field": getattr(self, "partner_identity_field", None),
			"frappe_partner_identity_field": getattr(self, "frappe_partner_identity_field", None),
			"partner_frappe_identity_field": getattr(self, "partner_frappe_identity_field", None),
			"partner_create_id_strategy": getattr(self, "partner_create_id_strategy", "payload"),
			"partner_create_id_source": getattr(self, "partner_create_id_source", None),
			"partner_create_id_scope_where": getattr(self, "partner_create_id_scope_where", None),
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


def _normalize_mapping_direction(value, *, default: str = "Frappe <-> Partner") -> str:
	direction = _clean_value(value) or default
	if direction not in MAPPING_DIRECTIONS:
		frappe.throw(f"Direction must be one of: {', '.join(MAPPING_DIRECTIONS)}")
	return direction


def _one_way_mapping_direction(sync_type) -> str | None:
	sync_type = _clean_value(sync_type)
	if sync_type in {"Frappe -> Partner", "Frappe <- Partner"}:
		return sync_type
	return None


def _read_query_can_replace_table_name(sync_type, read_query) -> bool:
	return _one_way_mapping_direction(sync_type) == "Frappe <- Partner" and bool(_clean_value(read_query))


def _partner_timestamps_required(doc) -> bool:
	return _clean_value(getattr(doc, "sync_type", None)) == "Frappe <-> Partner" or _truthy(
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


def _normalize_field_mapping_row(row, *, sync_type: str | None = None) -> dict[str, str] | None:
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
		"direction": _one_way_mapping_direction(sync_type)
		or _normalize_mapping_direction(_get_row_value(row, "direction")),
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
