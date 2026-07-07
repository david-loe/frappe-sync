# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document

from sync.sync.constants import (
	CONFLICT_POLICY_NEWEST_WINS,
	FRAPPE_WRITE_ACTION_NONE,
	FRAPPE_WRITE_ACTION_SUBMIT,
	FRAPPE_WRITE_ACTIONS,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH,
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
	PARTNER_WRITE_MODE_JSON_ARRAY_AGGREGATE,
	PARTNER_WRITE_MODE_ROW_UPSERT,
	PARTNER_WRITE_MODES,
	SYNC_RUN,
	SYNC_RUN_ITEM,
	TIMESTAMP_TIE_BREAKERS,
	TIMESTAMP_TIE_MANUAL,
	TIMESTAMP_TIE_FRAPPE_WINS,
	TIMESTAMP_TIE_PARTNER_WINS,
	UNMAPPED_ACTIONS,
	UNMAPPED_ACTION_KEEP_ORIGINAL,
	UNMAPPED_ACTION_KEYS,
	UNMAPPED_ACTION_USE_FALLBACK,
	UNMAPPED_ACTION_USE_NULL,
)

MAPPING_SCOPE_PARENT = "Parent"
MAPPING_SCOPE_CHILD = "Child"
MAPPING_SCOPES = (MAPPING_SCOPE_PARENT, MAPPING_SCOPE_CHILD)
CHILD_FIELD_PATH_SEPARATOR = "."


class SyncDefinition(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from sync.sync.doctype.sync_computed_field.sync_computed_field import SyncComputedField
		from sync.sync.doctype.sync_field_mapping.sync_field_mapping import SyncFieldMapping
		from sync.sync.doctype.sync_frappe_write_hook.sync_frappe_write_hook import SyncFrappeWriteHook
		from sync.sync.doctype.sync_key_field.sync_key_field import SyncKeyField
		from sync.sync.doctype.sync_value_mapping.sync_value_mapping import SyncValueMapping

		aggregate_item_key_field: DF.Data | None
		aggregate_json_array_path: DF.Data | None
		aggregate_json_column: DF.Data | None
		aggregate_preserve_unmatched: DF.Check
		aggregate_sort_field: DF.Data | None
		aggregate_sort_order: DF.Literal["", "Ascending", "Descending"]
		aggregate_target_key_field: DF.Data | None
		aggregate_target_key_value: DF.Data | None
		aggregate_target_table_name: DF.Data | None
		batch_size: DF.Int
		capture_audit_payloads: DF.Check
		computed_fields: DF.Table[SyncComputedField]
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
		frappe_write_hooks: DF.Table[SyncFrappeWriteHook]
		frequency_cron: DF.Data
		last_run: DF.Link | None
		last_run_status: DF.Literal["", "Queued", "Running", "Success", "Partial Error", "Needs Review", "Error", "Preview", "Skipped"]
		last_run_summary: DF.SmallText | None
		last_successful_sync: DF.Datetime | None
		last_sync_at: DF.Datetime | None
		match_fields: DF.Table[SyncKeyField]
		match_mode: DF.Literal["Match Fields", "Identity Fields"]
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
		partner_write_mode: DF.Literal["Row Upsert", "JSON Array Aggregate"]
		preview_limit: DF.Int
		read_query: DF.Code | None
		render_read_query_template: DF.Check
		sync_type: DF.Literal["Frappe -> Partner", "Frappe <-> Partner", "Frappe <- Partner"]
		table_name: DF.Data | None
		timestamp_buffer_ms: DF.Int
		timestamp_tie_breaker: DF.Literal["Manual", "Frappe Wins", "Partner Wins"]
		title: DF.Data
		update_existing: DF.Check
		use_last_sync_date: DF.Check
		value_mapping: DF.Table[SyncValueMapping]
	# end: auto-generated types

	def validate(self):
		SyncDefinition.validate_match_mode(self)
		SyncDefinition.validate_field_mapping(self)
		SyncDefinition.validate_value_mapping(self)
		SyncDefinition.validate_match_fields(self)
		SyncDefinition.validate_source_settings(self)
		SyncDefinition.validate_filter_expression(self)
		SyncDefinition.validate_modified_fields(self)
		SyncDefinition.validate_identity_settings(self)
		SyncDefinition.validate_one_way_match_mode(self)
		SyncDefinition.validate_computed_fields(self)
		SyncDefinition.validate_write_behavior(self)
		SyncDefinition.validate_preview_limit(self)

	def on_trash(self):
		for run_name in _linked_names(SYNC_RUN, {"sync_definition": self.name}):
			frappe.delete_doc(SYNC_RUN, run_name, ignore_permissions=True)

		for item_name in _linked_names(SYNC_RUN_ITEM, {"sync_definition": self.name}):
			frappe.delete_doc(SYNC_RUN_ITEM, item_name, ignore_permissions=True)

	def validate_field_mapping(self):
		seen: set[str] = set()
		duplicates: list[str] = []
		partner_fields_by_direction: dict[tuple[str, str], str] = {}
		partner_duplicates: list[str] = []
		for row in self.field_mapping or []:
			entry = _normalize_field_mapping_row(
				row,
				sync_type=getattr(self, "sync_type", None),
				doctype_name=getattr(self, "doctype_name", None),
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

	def validate_match_fields(self):
		if (_clean_value(getattr(self, "match_mode", None)) or MATCH_MODE_MATCH_FIELDS) != MATCH_MODE_MATCH_FIELDS:
			return
		mapping_fields = set(self.get_field_mapping().keys())
		match_fields = SyncDefinition.get_match_fields(self)
		if mapping_fields and not match_fields:
			frappe.throw("Match fields are required in Match Fields mode.")
		missing = [field for field in match_fields if field not in mapping_fields]
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
		if not _delete_missing_allowed(getattr(self, "sync_type", None), getattr(self, "match_mode", None)):
			self.delete_missing = 0
		if _clean_value(getattr(self, "partner_write_mode", None)) == PARTNER_WRITE_MODE_JSON_ARRAY_AGGREGATE:
			self.delete_missing = 0
			self.use_last_sync_date = 0
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
		self.timestamp_tie_breaker = _clean_value(getattr(self, "timestamp_tie_breaker", None)) or TIMESTAMP_TIE_MANUAL

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
		if _clean_value(getattr(self, "sync_type", None)) != MAPPING_DIRECTION_BOTH:
			self.timestamp_tie_breaker = TIMESTAMP_TIE_MANUAL
		if self.timestamp_tie_breaker not in TIMESTAMP_TIE_BREAKERS:
			frappe.throw(
				"Timestamp Tie Breaker must be one of: "
				+ ", ".join((TIMESTAMP_TIE_MANUAL, TIMESTAMP_TIE_FRAPPE_WINS, TIMESTAMP_TIE_PARTNER_WINS))
			)

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
		if _clean_value(getattr(self, "match_mode", None)) == MATCH_MODE_IDENTITY_FIELDS:
			missing = []
			if not identity_field:
				missing.append("Partner Identity Field")
			if not self.frappe_partner_identity_field:
				missing.append("Frappe Partner Identity Field")
			if not self.partner_frappe_identity_field:
				missing.append("Partner Frappe Identity Field")
			if missing:
				frappe.throw("Identity Fields mode requires: " + ", ".join(missing) + ".")

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
		mode = _clean_value(getattr(self, "one_way_match_mode", None)) or ONE_WAY_MATCH_FIRST
		if mode not in ONE_WAY_MATCH_MODES:
			frappe.throw("One-Way Match Mode must be one of: first_match, all_matches.")
		self.one_way_match_mode = mode

	def validate_write_behavior(self):
		self.update_existing = 1 if getattr(self, "update_existing", 1) else 0
		self.partner_write_mode = _normalize_partner_write_mode(getattr(self, "partner_write_mode", None))
		self.aggregate_target_table_name = _clean_value(getattr(self, "aggregate_target_table_name", None))
		self.aggregate_target_key_field = _clean_value(getattr(self, "aggregate_target_key_field", None))
		self.aggregate_target_key_value = _clean_value(getattr(self, "aggregate_target_key_value", None))
		self.aggregate_json_column = _clean_value(getattr(self, "aggregate_json_column", None))
		self.aggregate_json_array_path = _clean_value(getattr(self, "aggregate_json_array_path", None))
		self.aggregate_item_key_field = _clean_value(getattr(self, "aggregate_item_key_field", None))
		self.aggregate_sort_field = _clean_value(getattr(self, "aggregate_sort_field", None))
		self.aggregate_sort_order = _normalize_aggregate_sort_order(getattr(self, "aggregate_sort_order", None))
		self.aggregate_preserve_unmatched = 1 if getattr(self, "aggregate_preserve_unmatched", 1) else 0
		if self.partner_write_mode == PARTNER_WRITE_MODE_JSON_ARRAY_AGGREGATE:
			_validate_aggregate_settings(self)
		active_submit_events: set[str] = set()
		custom_script_found = False
		custom_script_has_code = False
		submit_found = False
		for row in getattr(self, "frappe_write_hooks", None) or []:
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
				if script:
					custom_script_has_code = True

		if submit_found:
			doctype_name = _clean_value(getattr(self, "doctype_name", None))
			if doctype_name and not getattr(frappe.get_meta(doctype_name), "is_submittable", False):
				frappe.throw(f"Built-in Submit hook requires submittable DocType {doctype_name}.")
		if custom_script_found and not _server_script_enabled():
			frappe.throw("Custom Script hooks require server_script_enabled.")
		if custom_script_has_code and not _current_user_is_system_manager():
			frappe.throw("Only System Manager can save non-empty Custom Script hooks.")

	def validate_match_mode(self):
		mode = _clean_value(getattr(self, "match_mode", None)) or MATCH_MODE_MATCH_FIELDS
		if mode not in MATCH_MODES:
			frappe.throw(f"Match Mode must be one of: {', '.join(MATCH_MODES)}.")
		self.match_mode = mode

	def validate_computed_fields(self):
		seen: set[str] = set()
		for row in getattr(self, "computed_fields", None) or []:
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
			entry = _normalize_field_mapping_row(
				row,
				sync_type=getattr(self, "sync_type", None),
				doctype_name=getattr(self, "doctype_name", None),
			)
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
			entry = _normalize_field_mapping_row(
				row,
				sync_type=getattr(self, "sync_type", None),
				doctype_name=getattr(self, "doctype_name", None),
			)
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
			"update_existing": getattr(self, "update_existing", 1),
			"delete_missing": self.delete_missing,
			"frappe_write_hooks": SyncDefinition.get_frappe_write_hooks(self),
			"computed_fields": SyncDefinition.get_computed_fields(self),
			"partner_write_mode": getattr(self, "partner_write_mode", PARTNER_WRITE_MODE_ROW_UPSERT),
			"aggregate_target_table_name": getattr(self, "aggregate_target_table_name", None),
			"aggregate_target_key_field": getattr(self, "aggregate_target_key_field", None),
			"aggregate_target_key_value": getattr(self, "aggregate_target_key_value", None),
			"aggregate_json_column": getattr(self, "aggregate_json_column", None),
			"aggregate_json_array_path": getattr(self, "aggregate_json_array_path", None),
			"aggregate_item_key_field": getattr(self, "aggregate_item_key_field", None),
			"aggregate_sort_field": getattr(self, "aggregate_sort_field", None),
			"aggregate_sort_order": getattr(self, "aggregate_sort_order", None),
			"aggregate_preserve_unmatched": bool(getattr(self, "aggregate_preserve_unmatched", 1)),
			"match_mode": getattr(self, "match_mode", MATCH_MODE_MATCH_FIELDS),
			"one_way_match_mode": getattr(self, "one_way_match_mode", ONE_WAY_MATCH_FIRST),
			"conflict_policy": self.conflict_policy or CONFLICT_POLICY_NEWEST_WINS,
			"table_name": self.table_name,
			"read_query": getattr(self, "read_query", None),
			"render_read_query_template": bool(getattr(self, "render_read_query_template", 0)),
			"preview_limit": self.get_preview_limit(),
			"export_mask_credentials": bool(self.export_mask_credentials),
			"frappe_modified_field": getattr(self, "frappe_modified_field", "modified"),
			"frappe_creation_field": "creation",
			"partner_modified_field": getattr(self, "partner_modified_field", None),
			"partner_creation_field": getattr(self, "partner_creation_field", None),
			"timestamp_tie_breaker": getattr(self, "timestamp_tie_breaker", TIMESTAMP_TIE_MANUAL),
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

	def get_frappe_write_hooks(self) -> list[dict]:
		result: list[dict] = []
		for row in getattr(self, "frappe_write_hooks", None) or []:
			entry = _normalize_frappe_write_hook_row(row, strict=False)
			if entry:
				result.append(entry)
		return result

	def get_computed_fields(self) -> list[dict]:
		result: list[dict] = []
		for row in getattr(self, "computed_fields", None) or []:
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


def _normalize_mapping_direction(value, *, default: str = MAPPING_DIRECTION_BOTH) -> str:
	direction = _clean_value(value) or default
	if direction not in MAPPING_DIRECTIONS:
		frappe.throw(f"Direction must be one of: {', '.join(MAPPING_DIRECTIONS)}")
	return direction


def _directions_for_mapping_entry(direction: str) -> tuple[str, ...]:
	if direction == MAPPING_DIRECTION_BOTH:
		return (MAPPING_DIRECTION_FRAPPE_TO_PARTNER, MAPPING_DIRECTION_PARTNER_TO_FRAPPE)
	return (direction,)


def _one_way_mapping_direction(sync_type) -> str | None:
	sync_type = _clean_value(sync_type)
	if sync_type in {MAPPING_DIRECTION_FRAPPE_TO_PARTNER, MAPPING_DIRECTION_PARTNER_TO_FRAPPE}:
		return sync_type
	return None


def _read_query_can_replace_table_name(sync_type, read_query) -> bool:
	return _one_way_mapping_direction(sync_type) == MAPPING_DIRECTION_PARTNER_TO_FRAPPE and bool(_clean_value(read_query))


def _delete_missing_allowed(sync_type, match_mode) -> bool:
	if _one_way_mapping_direction(sync_type):
		return True
	return _clean_value(sync_type) == MAPPING_DIRECTION_BOTH and _clean_value(match_mode) == MATCH_MODE_IDENTITY_FIELDS


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
	get_common_site_config = getattr(frappe, "get_common_site_config", None)
	if callable(get_common_site_config):
		try:
			return _truthy(get_common_site_config(cached=True).get("server_script_enabled"))
		except Exception:
			return False
	return _truthy(getattr(getattr(frappe, "conf", None), "server_script_enabled", None))


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
	if _normalize_partner_write_mode(getattr(doc, "partner_write_mode", None)) == PARTNER_WRITE_MODE_JSON_ARRAY_AGGREGATE:
		return False
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


def _normalize_partner_write_mode(value) -> str:
	mode = _clean_value(value) or PARTNER_WRITE_MODE_ROW_UPSERT
	if mode not in PARTNER_WRITE_MODES:
		frappe.throw(f"Partner Write Mode must be one of: {', '.join(PARTNER_WRITE_MODES)}.")
	return mode


def _normalize_aggregate_sort_order(value) -> str | None:
	order = _clean_value(value)
	if not order:
		return None
	normalized = order.lower()
	if normalized in {"asc", "ascending"}:
		return "Ascending"
	if normalized in {"desc", "descending"}:
		return "Descending"
	frappe.throw("Aggregate Sort Order must be Ascending or Descending.")
	return None


def _validate_aggregate_settings(doc) -> None:
	if _clean_value(getattr(doc, "sync_type", None)) != MAPPING_DIRECTION_FRAPPE_TO_PARTNER:
		frappe.throw("JSON Array Aggregate writes are only supported for Frappe -> Partner sync.")
	missing: list[str] = []
	for fieldname, label in (
		("aggregate_target_key_field", "Aggregate Target Key Field"),
		("aggregate_target_key_value", "Aggregate Target Key Value"),
		("aggregate_json_column", "Aggregate JSON Column"),
		("aggregate_json_array_path", "Aggregate JSON Array Path"),
		("aggregate_item_key_field", "Aggregate Item Key Field"),
	):
		if not _clean_value(getattr(doc, fieldname, None)):
			missing.append(label)
	if not (_clean_value(getattr(doc, "aggregate_target_table_name", None)) or _clean_value(getattr(doc, "table_name", None))):
		missing.append("Aggregate Target Table Name")
	if missing:
		frappe.throw("JSON Array Aggregate requires: " + ", ".join(missing) + ".")


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
	scope = _normalize_mapping_scope(_get_row_value(row, "mapping_scope"), row=row)
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
			"direction": _one_way_mapping_direction(sync_type)
			or _normalize_mapping_direction(_get_row_value(row, "direction")),
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
		"direction": _one_way_mapping_direction(sync_type)
		or _normalize_mapping_direction(_get_row_value(row, "direction")),
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
