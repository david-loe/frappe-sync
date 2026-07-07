from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import frappe

from sync.sync.doctype.sync_definition import sync_definition as sync_definition_module
from sync.sync.doctype.sync_partner import sync_partner as sync_partner_module
from sync.sync.doctype.sync_run import sync_run as sync_run_module
from sync.sync.doctype.sync_run_item import sync_run_item as sync_run_item_module


class FakeSyncDefinitionDoc:
	def __init__(self, **values):
		self.__dict__.update(values)
		self.match_fields = values.get("match_fields", [])
		self.field_mapping = values.get("field_mapping", [])
		self.value_mapping = values.get("value_mapping", [])
		self.frappe_modified_field = values.get("frappe_modified_field", "modified")
		self.frappe_creation_field = values.get("frappe_creation_field", "creation")
		self.partner_modified_field = values.get("partner_modified_field", "updated_at")
		self.partner_creation_field = values.get("partner_creation_field", "created_at")
		self.timestamp_tie_breaker = values.get("timestamp_tie_breaker", "Manual")
		self.preview_limit = values.get("preview_limit", 50)
		self.delete_missing = values.get("delete_missing", 0)
		self.read_query = values.get("read_query")
		self.render_read_query_template = values.get("render_read_query_template", 0)
		self.table_name = values.get("table_name")
		self.use_last_sync_date = values.get("use_last_sync_date", 1)
		self.title = values.get("title", "SYNC-DEF")
		self.name = values.get("name", "SYNC-DEF")
		self.enabled = values.get("enabled", 1)
		self.partner = values.get("partner", "PARTNER-1")
		self.sync_type = values.get("sync_type", "Frappe -> Partner")
		self.doctype_name = values.get("doctype_name", "Task")
		self.frequency_cron = values.get("frequency_cron", "*/15 * * * *")
		self.filter_expression = values.get("filter_expression")
		self.frappe_source_mode = values.get("frappe_source_mode", "DocType Query")
		self.frappe_source_script = values.get("frappe_source_script")
		self.batch_size = values.get("batch_size", 50)
		self.timestamp_buffer_ms = values.get("timestamp_buffer_ms", 100)
		self.create_new = values.get("create_new", 1)
		self.update_existing = values.get("update_existing", 1)
		self.frappe_after_insert_action = values.get("frappe_after_insert_action", "None")
		self.frappe_after_update_action = values.get("frappe_after_update_action", "None")
		self.frappe_write_hooks = values.get("frappe_write_hooks", [])
		self.one_way_match_mode = values.get("one_way_match_mode", "first_match")
		self.conflict_policy = values.get("conflict_policy", "newest_wins")
		self.export_mask_credentials = values.get("export_mask_credentials", 1)
		self.next_run_at = values.get("next_run_at")
		self.last_run_status = values.get("last_run_status")
		self.last_run_summary = values.get("last_run_summary")
		self.partner_identity_field = values.get("partner_identity_field")
		self.frappe_partner_identity_field = values.get("frappe_partner_identity_field")
		self.partner_frappe_identity_field = values.get("partner_frappe_identity_field")
		self.partner_create_id_strategy = values.get("partner_create_id_strategy", "payload")
		self.partner_create_id_source = values.get("partner_create_id_source")
		self.partner_create_id_scope_where = values.get("partner_create_id_scope_where")

	def get(self, key, default=None):
		return getattr(self, key, default)

	def append(self, fieldname, payload):
		getattr(self, fieldname).append(SimpleNamespace(**payload))

	def get_match_fields(self):
		return sync_definition_module.SyncDefinition.get_match_fields(self)

	def get_field_mapping(self):
		return sync_definition_module.SyncDefinition.get_field_mapping(self)

	def get_value_mapping(self):
		return sync_definition_module.SyncDefinition.get_value_mapping(self)

	def get_value_mapping_fallbacks(self):
		return sync_definition_module.SyncDefinition.get_value_mapping_fallbacks(self)

	def get_frappe_modified_fields(self):
		return sync_definition_module.SyncDefinition.get_frappe_modified_fields(self)

	def get_partner_modified_fields(self):
		return sync_definition_module.SyncDefinition.get_partner_modified_fields(self)

	def get_preview_limit(self):
		return sync_definition_module.SyncDefinition.get_preview_limit(self)

	def as_export_dict(self):
		return sync_definition_module.SyncDefinition.as_export_dict(self)

	def validate_field_mapping(self):
		return sync_definition_module.SyncDefinition.validate_field_mapping(self)

	def validate_value_mapping(self):
		return sync_definition_module.SyncDefinition.validate_value_mapping(self)

	def validate_match_fields(self):
		return sync_definition_module.SyncDefinition.validate_match_fields(self)

	def validate_source_settings(self):
		return sync_definition_module.SyncDefinition.validate_source_settings(self)

	def validate_modified_fields(self):
		return sync_definition_module.SyncDefinition.validate_modified_fields(self)

	def validate_filter_expression(self):
		return sync_definition_module.SyncDefinition.validate_filter_expression(self)

	def validate_preview_limit(self):
		return sync_definition_module.SyncDefinition.validate_preview_limit(self)

	def validate_one_way_match_mode(self):
		return sync_definition_module.SyncDefinition.validate_one_way_match_mode(self)

	def validate_write_behavior(self):
		return sync_definition_module.SyncDefinition.validate_write_behavior(self)


class TestDoctypeControllerBehavior(unittest.TestCase):
	def test_sync_run_on_trash_deletes_items_and_clears_last_run_links(self):
		def fake_get_all(doctype, filters=None, fields=None, order_by=None):
			if doctype == "Sync Run Item":
				self.assertEqual(filters, {"sync_run": "RUN-1"})
				return [{"name": "ITEM-1"}, SimpleNamespace(name="ITEM-2")]
			if doctype == "Sync Definition":
				self.assertEqual(filters, {"last_run": "RUN-1"})
				return [{"name": "SYNC-1"}]
			return []

		delete_doc = Mock()
		set_value = Mock()

		with patch.object(
			sync_run_module,
			"frappe",
			SimpleNamespace(get_all=fake_get_all, delete_doc=delete_doc, db=SimpleNamespace(set_value=set_value)),
		):
			sync_run_module.SyncRun.on_trash(SimpleNamespace(name="RUN-1"))

		delete_doc.assert_has_calls(
			[
				call("Sync Run Item", "ITEM-1", ignore_permissions=True),
				call("Sync Run Item", "ITEM-2", ignore_permissions=True),
			]
		)
		set_value.assert_called_once_with(
			"Sync Definition",
			"SYNC-1",
			"last_run",
			None,
			update_modified=False,
		)

	def test_sync_run_item_no_longer_owns_field_level_change_rows(self):
		self.assertFalse(hasattr(sync_run_item_module.SyncRunItem, "on_trash"))

	def test_sync_definition_on_trash_deletes_runs_and_leftover_items(self):
		def fake_get_all(doctype, filters=None, fields=None, order_by=None):
			if doctype == "Sync Run":
				self.assertEqual(filters, {"sync_definition": "SYNC-1"})
				return [{"name": "RUN-1"}, SimpleNamespace(name="RUN-2")]
			if doctype == "Sync Run Item":
				self.assertEqual(filters, {"sync_definition": "SYNC-1"})
				return [{"name": "ITEM-LEFT"}]
			return []

		delete_doc = Mock()

		with patch.object(
			sync_definition_module,
			"frappe",
			SimpleNamespace(get_all=fake_get_all, delete_doc=delete_doc),
		):
			sync_definition_module.SyncDefinition.on_trash(SimpleNamespace(name="SYNC-1"))

		delete_doc.assert_has_calls(
			[
				call("Sync Run", "RUN-1", ignore_permissions=True),
				call("Sync Run", "RUN-2", ignore_permissions=True),
				call("Sync Run Item", "ITEM-LEFT", ignore_permissions=True),
			]
		)

	def test_sync_partner_validate_normalizes_and_rejects_invalid_time_zone(self):
		doc = SimpleNamespace(time_zone=" Europe/Berlin ")

		sync_partner_module.SyncPartner.validate(doc)
		self.assertEqual(doc.time_zone, "Europe/Berlin")

		with patch.object(sync_partner_module.frappe, "throw", side_effect=frappe.ValidationError("bad-tz")):
			with self.assertRaises(frappe.ValidationError):
				sync_partner_module.SyncPartner.validate(SimpleNamespace(time_zone="Mars/Olympus"))

	def test_validate_match_fields_throws_for_missing_mapping(self):
		doc = FakeSyncDefinitionDoc(
			match_fields=[SimpleNamespace(frappe_field="name"), SimpleNamespace(frappe_field="status")],
			field_mapping=[SimpleNamespace(frappe_field="name", partner_field="id")],
		)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("missing")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_match_fields(doc)

	def test_validate_field_mapping_normalizes_rows_and_rejects_duplicates(self):
		doc = FakeSyncDefinitionDoc(
			field_mapping=[
				SimpleNamespace(
					frappe_field=" name ",
					partner_field=" id ",
					direction="",
					unmapped_action="Use NULL",
					fallback_value="ignored",
				)
			]
		)

		sync_definition_module.SyncDefinition.validate_field_mapping(doc)

		self.assertEqual(doc.field_mapping[0].frappe_field, "name")
		self.assertEqual(doc.field_mapping[0].partner_field, "id")
		self.assertEqual(doc.field_mapping[0].direction, "Frappe -> Partner")
		self.assertEqual(doc.field_mapping[0].unmapped_action, "Use NULL")
		self.assertIsNone(doc.field_mapping[0].fallback_value)

		duplicate_doc = FakeSyncDefinitionDoc(
			field_mapping=[
				SimpleNamespace(frappe_field="name", partner_field="id", direction="Frappe <-> Partner"),
				SimpleNamespace(frappe_field=" name ", partner_field="external_id", direction="Frappe -> Partner"),
			]
		)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("duplicate")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_field_mapping(duplicate_doc)

	def test_validate_field_mapping_normalizes_child_mapping_rows(self):
		def fake_meta(doctype):
			if doctype == "Task":
				return SimpleNamespace(
					fields=[
						SimpleNamespace(fieldname="items", fieldtype="Table", options="Task Item"),
						SimpleNamespace(fieldname="subject", fieldtype="Data"),
					]
				)
			if doctype == "Task Item":
				return SimpleNamespace(
					fields=[
						SimpleNamespace(fieldname="item_code", fieldtype="Data"),
						SimpleNamespace(fieldname="subitems", fieldtype="Table", options="Nested"),
					]
				)
			return SimpleNamespace(fields=[])

		doc = FakeSyncDefinitionDoc(
			sync_type="Frappe <-> Partner",
			field_mapping=[
				SimpleNamespace(
					mapping_scope="Child",
					table_field="items",
					row_idx=1,
					child_field="item_code",
					partner_field="external_item_code",
					direction="",
				)
			],
		)

		with patch.object(sync_definition_module.frappe, "get_meta", side_effect=fake_meta):
			sync_definition_module.SyncDefinition.validate_field_mapping(doc)

		row = doc.field_mapping[0]
		self.assertEqual(row.mapping_scope, "Child")
		self.assertEqual(row.frappe_field, "items.1.item_code")
		self.assertEqual(row.child_doctype, "Task Item")
		self.assertEqual(row.direction, "Frappe <-> Partner")

		invalid_doc = FakeSyncDefinitionDoc(
			field_mapping=[
				SimpleNamespace(
					mapping_scope="Child",
					table_field="items",
					row_idx=1,
					child_field="subitems",
					partner_field="nested",
				)
			]
		)
		with (
			patch.object(sync_definition_module.frappe, "get_meta", side_effect=fake_meta),
			patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid-child")),
		):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_field_mapping(invalid_doc)

	def test_validate_field_mapping_rejects_invalid_direction(self):
		doc = FakeSyncDefinitionDoc(
			sync_type="Frappe <-> Partner",
			field_mapping=[SimpleNamespace(frappe_field="name", partner_field="id", direction="Outbound")]
		)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid-direction")) as mock_throw:
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_field_mapping(doc)

		mock_throw.assert_called_once_with(
			"Direction must be one of: Frappe <-> Partner, Frappe -> Partner, Frappe <- Partner"
		)

	def test_validate_field_mapping_rejects_invalid_fallback_settings(self):
		invalid_action_doc = FakeSyncDefinitionDoc(
			field_mapping=[
				SimpleNamespace(
					frappe_field="status",
					partner_field="state",
					unmapped_action="Drop",
				)
			]
		)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid-action")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_field_mapping(invalid_action_doc)

		missing_literal_doc = FakeSyncDefinitionDoc(
			field_mapping=[
				SimpleNamespace(
					frappe_field="status",
					partner_field="state",
					unmapped_action="Use Fallback Value",
					fallback_value=" ",
				)
			]
		)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("missing-value")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_field_mapping(missing_literal_doc)

	def test_validate_source_settings_requires_table_name(self):
		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_source_settings(FakeSyncDefinitionDoc())
		doc = FakeSyncDefinitionDoc(table_name=" tabTask ", read_query=" select * from tabTask ")
		sync_definition_module.SyncDefinition.validate_source_settings(doc)
		self.assertEqual(doc.table_name, "tabTask")
		self.assertEqual(doc.read_query, "select * from tabTask")
		read_query_doc = FakeSyncDefinitionDoc(sync_type="Frappe <- Partner", table_name="", read_query=" select 1 ")
		sync_definition_module.SyncDefinition.validate_source_settings(read_query_doc)
		self.assertIsNone(read_query_doc.table_name)
		self.assertEqual(read_query_doc.read_query, "select 1")

	def test_validate_modified_fields_and_preview_limit_reject_invalid_values(self):
		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_modified_fields(
					FakeSyncDefinitionDoc(partner_modified_field="")
				)
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_modified_fields(
					FakeSyncDefinitionDoc(partner_creation_field="")
				)
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_modified_fields(
					FakeSyncDefinitionDoc(partner_modified_field="updated_at", partner_creation_field="updated_at")
				)
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_preview_limit(FakeSyncDefinitionDoc(preview_limit=0))

	def test_validate_write_behavior_normalizes_actions_and_requires_submittable_doctype(self):
		doc = FakeSyncDefinitionDoc(
			update_existing=0,
			frappe_write_hooks=[
				SimpleNamespace(
					enabled=1,
					event="After Insert",
					hook_type="Built-in Action",
					action="Submit",
					script="ignored",
				)
			],
		)
		with patch.object(sync_definition_module.frappe, "get_meta", return_value=SimpleNamespace(is_submittable=True)):
			sync_definition_module.SyncDefinition.validate_write_behavior(doc)

		self.assertEqual(doc.update_existing, 0)
		self.assertEqual(doc.frappe_write_hooks[0].action, "Submit")
		self.assertIsNone(doc.frappe_write_hooks[0].script)

		non_submittable = FakeSyncDefinitionDoc(
			frappe_write_hooks=[
				SimpleNamespace(enabled=1, event="After Insert", hook_type="Built-in Action", action="Submit")
			]
		)
		with (
			patch.object(sync_definition_module.frappe, "get_meta", return_value=SimpleNamespace(is_submittable=False)),
			patch.object(
				sync_definition_module.frappe,
				"throw",
				side_effect=frappe.ValidationError("not submittable"),
			),
			self.assertRaises(frappe.ValidationError),
		):
			sync_definition_module.SyncDefinition.validate_write_behavior(non_submittable)

	def test_validate_modified_fields_allows_blank_partner_timestamps_for_one_way_full_sync(self):
		doc = FakeSyncDefinitionDoc(
			sync_type="Frappe -> Partner",
			use_last_sync_date=0,
			partner_modified_field="",
			partner_creation_field="",
			timestamp_tie_breaker="invalid hidden value",
		)

		meta = SimpleNamespace(fields=[], has_field=lambda fieldname: fieldname in {"modified", "creation"})
		with patch.object(sync_definition_module.frappe, "get_meta", return_value=meta):
			sync_definition_module.SyncDefinition.validate_modified_fields(doc)

		self.assertIsNone(doc.partner_modified_field)
		self.assertIsNone(doc.partner_creation_field)
		self.assertEqual(doc.timestamp_tie_breaker, "Manual")

	def test_validate_modified_fields_requires_partner_timestamps_for_bidirectional_or_delta_sync(self):
		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_modified_fields(
					FakeSyncDefinitionDoc(
						sync_type="Frappe <-> Partner",
						use_last_sync_date=0,
						partner_modified_field="",
						partner_creation_field="created_at",
					)
				)
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_modified_fields(
					FakeSyncDefinitionDoc(
						sync_type="Frappe -> Partner",
						use_last_sync_date=1,
						partner_modified_field="updated_at",
						partner_creation_field="",
					)
				)

	def test_sync_definition_timestamp_field_metadata_is_conditional(self):
		doctype_path = Path(sync_definition_module.__file__).with_suffix(".json")
		fields = {
			field["fieldname"]: field
			for field in json.loads(doctype_path.read_text(encoding="utf-8"))["fields"]
			if field.get("fieldname")
		}

		self.assertEqual(
			fields["partner_modified_field"]["mandatory_depends_on"],
			"eval:doc.sync_type == 'Frappe <-> Partner' || doc.use_last_sync_date",
		)
		self.assertEqual(fields["partner_creation_field"]["reqd"], 0)
		self.assertEqual(fields["timestamp_buffer_ms"]["depends_on"], "eval:doc.sync_type == 'Frappe <-> Partner'")
		self.assertEqual(fields["conflict_policy"]["mandatory_depends_on"], "eval:doc.sync_type == 'Frappe <-> Partner'")
		self.assertEqual(fields["timestamp_tie_breaker"]["depends_on"], "eval:doc.sync_type == 'Frappe <-> Partner'")
		self.assertEqual(fields["one_way_match_mode"]["depends_on"], "eval:doc.sync_type != 'Frappe <-> Partner'")
		self.assertEqual(fields["frappe_source_mode"]["default"], "DocType Query")
		self.assertEqual(fields["frappe_source_mode"]["options"], "DocType Query\nPython Script")
		self.assertEqual(fields["frappe_source_script"]["depends_on"], "eval:doc.frappe_source_mode == 'Python Script'")
		self.assertEqual(
			fields["frappe_source_script"]["mandatory_depends_on"],
			"eval:doc.frappe_source_mode == 'Python Script'",
		)

	def test_validate_one_way_match_mode_normalizes_and_rejects_invalid_values(self):
		doc = FakeSyncDefinitionDoc(one_way_match_mode=" all_matches ")
		sync_definition_module.SyncDefinition.validate_one_way_match_mode(doc)
		self.assertEqual(doc.one_way_match_mode, "all_matches")

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_one_way_match_mode(
					FakeSyncDefinitionDoc(one_way_match_mode="fanout")
				)

	def test_validate_source_settings_rejects_delete_missing_with_read_query(self):
		doc = FakeSyncDefinitionDoc(
			table_name="people",
			read_query="select * from people where active = 1",
			render_read_query_template=1,
			delete_missing=1,
		)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("unsafe-source")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_source_settings(doc)

	def test_validate_source_settings_clears_delete_missing_for_bidirectional_sync(self):
		doc = FakeSyncDefinitionDoc(
			sync_type="Frappe <-> Partner",
			table_name="people",
			read_query="select * from people where active = 1",
			delete_missing=1,
		)

		sync_definition_module.SyncDefinition.validate_source_settings(doc)

		self.assertEqual(doc.delete_missing, 0)
		self.assertEqual(doc.read_query, "select * from people where active = 1")

	def test_validate_filter_expression_accepts_valid_values_during_validate(self):
		string_doc = FakeSyncDefinitionDoc(
			table_name="tabTask",
			use_last_sync_date=0,
			filter_expression='  [["status","=","Open"]]  ',
		)
		dict_doc = FakeSyncDefinitionDoc(
			table_name="tabTask",
			use_last_sync_date=0,
			filter_expression={"status": "Open"},
		)

		meta = SimpleNamespace(fields=[], has_field=lambda fieldname: fieldname in {"modified", "creation"})
		with patch.object(sync_definition_module.frappe, "get_meta", return_value=meta):
			sync_definition_module.SyncDefinition.validate(string_doc)
			sync_definition_module.SyncDefinition.validate(dict_doc)

		self.assertEqual(string_doc.filter_expression, '[["status","=","Open"]]')
		self.assertEqual(dict_doc.filter_expression, '{"status": "Open"}')

	def test_validate_filter_expression_rejects_invalid_json_and_scalar_payloads(self):
		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid-json")) as mock_throw:
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_filter_expression(
					FakeSyncDefinitionDoc(filter_expression="not-json")
				)

		mock_throw.assert_called_once_with("Filter Expression must be valid JSON.")

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid-type")) as mock_throw:
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_filter_expression(FakeSyncDefinitionDoc(filter_expression="1"))

		mock_throw.assert_called_once_with("Filter Expression must decode to a JSON array or object.")

	def test_validate_frappe_source_settings_requires_script_and_server_script_flag(self):
		doc = FakeSyncDefinitionDoc(frappe_source_mode="Python Script", frappe_source_script=" records = [] ")
		with patch.object(sync_definition_module, "_server_script_enabled", return_value=True):
			sync_definition_module.SyncDefinition.validate_frappe_source_settings(doc)
		self.assertEqual(doc.frappe_source_mode, "Python Script")
		self.assertEqual(doc.frappe_source_script, "records = []")

		blank_doc = FakeSyncDefinitionDoc(frappe_source_mode="Python Script", frappe_source_script="")
		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("missing-script")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_frappe_source_settings(blank_doc)

		disabled_doc = FakeSyncDefinitionDoc(frappe_source_mode="Python Script", frappe_source_script="records = []")
		with (
			patch.object(sync_definition_module, "_server_script_enabled", return_value=False),
			patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("disabled")),
		):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_frappe_source_settings(disabled_doc)

		query_doc = FakeSyncDefinitionDoc(frappe_source_mode="", frappe_source_script="records = []")
		sync_definition_module.SyncDefinition.validate_frappe_source_settings(query_doc)
		self.assertEqual(query_doc.frappe_source_mode, "DocType Query")
		self.assertIsNone(query_doc.frappe_source_script)

	def test_getters_and_modified_fields_are_normalized(self):
		doc = FakeSyncDefinitionDoc(
			match_fields=[SimpleNamespace(frappe_field=" name "), SimpleNamespace(frappe_field="")],
			field_mapping=[
				SimpleNamespace(
					frappe_field=" name ",
					partner_field=" id ",
					direction=None,
					unmapped_action="Use Fallback Value",
					fallback_value="unknown",
				),
				SimpleNamespace(frappe_field="", partner_field="status", direction="Frappe <-> Partner"),
			],
			value_mapping=[
				SimpleNamespace(frappe_field=" status ", frappe_value="Open", partner_value="1"),
				SimpleNamespace(frappe_field="", frappe_value="Closed", partner_value="0"),
			],
			frappe_modified_field=" changed_on ",
			partner_modified_field=" partner_changed ",
		)

		self.assertEqual(sync_definition_module.SyncDefinition.get_match_fields(doc), ["name"])
		self.assertEqual(
			sync_definition_module.SyncDefinition.get_field_mapping(doc),
			{"name": {"partner_field": "id", "direction": "Frappe -> Partner"}},
		)
		self.assertEqual(
			sync_definition_module.SyncDefinition.get_value_mapping_fallbacks(doc),
			{"name": {"action": "fallback", "value": "unknown"}},
		)
		self.assertEqual(sync_definition_module.SyncDefinition.get_value_mapping(doc), {"status": {"Open": "1"}})
		self.assertEqual(sync_definition_module.SyncDefinition.get_frappe_modified_fields(doc), ["changed_on"])
		self.assertEqual(sync_definition_module.SyncDefinition.get_partner_modified_fields(doc), ["partner_changed"])

	def test_value_mapping_supports_explicit_null_on_either_side(self):
		null_to_partner = SimpleNamespace(
			frappe_field="gender",
			frappe_value=None,
			frappe_value_is_null=1,
			partner_value=" 2 ",
			partner_value_is_null=0,
		)
		partner_to_null = SimpleNamespace(
			frappe_field="status",
			frappe_value="Unknown",
			frappe_value_is_null=0,
			partner_value=None,
			partner_value_is_null=1,
		)
		doc = FakeSyncDefinitionDoc(value_mapping=[null_to_partner, partner_to_null])

		sync_definition_module.SyncDefinition.validate_value_mapping(doc)

		self.assertIsNone(null_to_partner.frappe_value)
		self.assertEqual(null_to_partner.partner_value, "2")
		self.assertIsNone(partner_to_null.partner_value)
		self.assertEqual(
			sync_definition_module.SyncDefinition.get_value_mapping(doc),
			{"gender": {None: "2"}, "status": {"Unknown": None}},
		)

	def test_value_mapping_requires_values_when_null_flags_are_disabled(self):
		doc = FakeSyncDefinitionDoc(
			value_mapping=[
				SimpleNamespace(
					frappe_field="gender",
					frappe_value=None,
					frappe_value_is_null=0,
					partner_value="2",
					partner_value_is_null=0,
				)
			]
		)

		with patch.object(
			sync_definition_module.frappe,
			"throw",
			side_effect=frappe.ValidationError("missing value"),
		):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_value_mapping(doc)

	def test_export_payload_helpers_cover_preview_limit_and_serialization(self):
		doc = FakeSyncDefinitionDoc(
			sync_type="Frappe <- Partner",
			value_mapping=[SimpleNamespace(frappe_field="status", frappe_value={"a": 1}, partner_value=["x"])],
			field_mapping=[
				SimpleNamespace(
					frappe_field="name",
					partner_field="id",
					direction="Frappe <- Partner",
					unmapped_action="Use Fallback Value",
					fallback_value="UNKNOWN",
				)
			],
			match_fields=[SimpleNamespace(frappe_field="name")],
			frappe_modified_field="modified",
			partner_modified_field="updated_at",
			preview_limit="invalid",
			one_way_match_mode="all_matches",
			render_read_query_template=1,
		)

		self.assertEqual(sync_definition_module.SyncDefinition.get_preview_limit(doc), 50)
		exported = sync_definition_module.SyncDefinition.as_export_dict(doc)
		self.assertEqual(exported["field_mapping"]["name"]["direction"], "Frappe <- Partner")
		self.assertEqual(exported["value_mapping"]["status"], {'{"a": 1}': '["x"]'})
		self.assertEqual(exported["timestamp_buffer_ms"], 100)
		self.assertEqual(exported["update_existing"], 1)
		self.assertEqual(exported["frappe_write_hooks"], [])
		self.assertNotIn("frappe_after_insert_action", exported)
		self.assertNotIn("frappe_after_update_action", exported)
		self.assertEqual(
			exported["value_mapping_fallbacks"]["name"],
			{"action": "fallback", "value": "UNKNOWN"},
		)
		self.assertEqual(exported["one_way_match_mode"], "all_matches")
		self.assertTrue(exported["render_read_query_template"])
		self.assertNotIn("next_run_at", exported)
		self.assertNotIn("last_run_status", exported)
		self.assertNotIn("last_run_summary", exported)
		payload = sync_definition_module.SyncDefinition.get_export_payload(doc)
		self.assertIn("sync_definition", payload)
		self.assertTrue(payload["mask_credentials"])

	def test_helper_functions_cover_edge_cases(self):
		self.assertEqual(sync_definition_module._split_lines(" a \n\n b "), ["a", "b"])
		self.assertIsNone(sync_definition_module._clean_value("   "))
		self.assertIsNone(sync_definition_module._normalize_filter_expression("   "))
		self.assertEqual(sync_definition_module._normalize_mapping_direction(""), "Frappe <-> Partner")
		self.assertEqual(
			sync_definition_module._normalize_field_mapping_row(
				SimpleNamespace(frappe_field=" name ", partner_field=" id ", direction="")
			),
			{
				"frappe_field": "name",
				"partner_field": "id",
				"direction": "Frappe <-> Partner",
				"mapping_scope": "Parent",
			},
		)
		self.assertEqual(sync_definition_module.cstr(None), "")
		self.assertEqual(sync_definition_module.cstr({"a": 1}), '{"a": 1}')
