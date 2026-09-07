# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from sync.sync.constants import (
	SYNC_RUN,
	SYNC_RUN_ITEM,
)
from sync.sync.service import configuration, definition_rules


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
		frappe_source_mode: DF.Literal["DocType Query", "Python Script"]
		frappe_source_script: DF.Code | None
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
		configuration.normalize_definition_document(self)
		definition_rules.validate_script_permissions(self)

	def on_trash(self):
		for run_name in _linked_names(SYNC_RUN, {"sync_definition": self.name}):
			frappe.delete_doc(SYNC_RUN, run_name, ignore_permissions=True)

		for item_name in _linked_names(SYNC_RUN_ITEM, {"sync_definition": self.name}):
			frappe.delete_doc(SYNC_RUN_ITEM, item_name, ignore_permissions=True)

	def validate_field_mapping(self):
		return definition_rules.validate_field_mapping(self)

	def validate_match_fields(self):
		return definition_rules.validate_match_fields(self)

	def validate_value_mapping(self):
		return definition_rules.validate_value_mapping(self)

	def validate_source_settings(self):
		return definition_rules.validate_source_settings(self)

	def validate_modified_fields(self):
		return definition_rules.validate_modified_fields(self)

	def validate_identity_settings(self):
		return definition_rules.validate_identity_settings(self)

	def validate_filter_expression(self):
		return definition_rules.validate_filter_expression(self)

	def validate_frappe_source_settings(self):
		return definition_rules.validate_frappe_source_settings(self)

	def validate_preview_limit(self):
		return definition_rules.validate_preview_limit(self)

	def validate_one_way_match_mode(self):
		return definition_rules.validate_one_way_match_mode(self)

	def validate_write_behavior(self):
		return definition_rules.validate_write_behavior(self)

	def validate_match_mode(self):
		return definition_rules.validate_match_mode(self)

	def validate_computed_fields(self):
		return definition_rules.validate_computed_fields(self)

	def get_match_fields(self):
		return definition_rules.get_match_fields(self)

	def get_field_mapping(self):
		return definition_rules.get_field_mapping(self)

	def get_value_mapping(self):
		return definition_rules.get_value_mapping(self)

	def get_value_mapping_fallbacks(self):
		return definition_rules.get_value_mapping_fallbacks(self)

	def get_frappe_modified_fields(self):
		return definition_rules.get_frappe_modified_fields(self)

	def get_partner_modified_fields(self):
		return definition_rules.get_partner_modified_fields(self)

	def as_export_dict(self):
		return definition_rules.as_export_dict(self)

	def get_preview_limit(self):
		return definition_rules.get_preview_limit(self)

	def get_export_payload(self):
		return definition_rules.get_export_payload(self)

	def get_frappe_write_hooks(self):
		return definition_rules.get_frappe_write_hooks(self)

	def get_computed_fields(self):
		return definition_rules.get_computed_fields(self)


def _linked_names(doctype: str, filters: dict) -> list[str]:
	rows = frappe.get_all(doctype, filters=filters, fields=["name"], order_by=None)
	return [row.name if hasattr(row, "name") else row.get("name") for row in rows]
