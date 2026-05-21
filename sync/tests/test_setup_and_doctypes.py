from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import frappe
from frappe.model.document import Document

import sync
import sync.setup as sync_setup
from sync.sync.doctype.sync_definition import sync_definition as sync_definition_module
from sync.sync.doctype.sync_partner import sync_partner as sync_partner_module
from sync.sync.doctype.sync_partner.sync_partner import SyncPartner
from sync.sync.doctype.sync_partner_type.sync_partner_type import SyncPartnerType
from sync.sync.doctype.sync_run import sync_run as sync_run_module
from sync.sync.doctype.sync_run.sync_run import SyncRun
from sync.sync.doctype.sync_run_item import sync_run_item as sync_run_item_module
from sync.sync.doctype.sync_run_item.sync_run_item import SyncRunItem


class MutableDoc:
	def __init__(self):
		self.updated = []
		self.saved = False
		self.inserted = False

	def update(self, payload):
		self.updated.append(dict(payload))

	def save(self, **kwargs):
		self.saved = True
		return self

	def insert(self, **kwargs):
		self.inserted = True
		return self


def _db_stub(**overrides):
	values = {"exists": lambda *args, **kwargs: False, "commit": lambda: None}
	values.update(overrides)
	return SimpleNamespace(**values)


class FakeSyncDefinitionDoc:
	def __init__(self, **values):
		self.__dict__.update(values)
		self.match_fields = values.get("match_fields", [])
		self.field_mapping = values.get("field_mapping", [])
		self.value_mapping = values.get("value_mapping", [])
		self.frappe_modified_field_rows = values.get("frappe_modified_field_rows", [])
		self.partner_modified_field_rows = values.get("partner_modified_field_rows", [])
		self.preview_limit = values.get("preview_limit", 50)
		self.delete_missing = values.get("delete_missing", 0)
		self.read_query = values.get("read_query")
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
		self.batch_size = values.get("batch_size", 50)
		self.timestamp_buffer_seconds = values.get("timestamp_buffer_seconds", 15)
		self.create_new = values.get("create_new", 1)
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


class TestSetupModule(unittest.TestCase):
	def test_after_migrate_delegates_to_default_partner_type_setup(self):
		with patch("sync.setup.ensure_default_partner_types") as mock_ensure:
			sync_setup.after_migrate()

		mock_ensure.assert_called_once_with()

	def test_before_tests_delegates_to_default_partner_type_setup(self):
		with patch("sync.setup.ensure_default_partner_types") as mock_ensure:
			sync_setup.before_tests()

		mock_ensure.assert_called_once_with()

	def test_ensure_default_partner_types_updates_existing_and_creates_missing(self):
		existing_doc = MutableDoc()
		new_doc = MutableDoc()

		def fake_exists(doctype, name):
			return name == "mssql"

		with (
			patch.object(sync_setup, "frappe", SimpleNamespace(db=_db_stub(exists=fake_exists), get_doc=lambda *args, **kwargs: existing_doc, new_doc=lambda *args, **kwargs: new_doc)),
		):
			sync_setup.ensure_default_partner_types()

		self.assertTrue(existing_doc.saved)
		self.assertEqual(existing_doc.updated[0]["partner_type_code"], "mssql")
		self.assertTrue(new_doc.inserted)
		self.assertEqual([payload["partner_type_code"] for payload in new_doc.updated], ["postgres", "firebird"])


class TestSyncDefinitionDoctype(unittest.TestCase):
	def _load_doctype_json(self, relative_path):
		path = Path(sync.__file__).resolve().parent / relative_path
		return json.loads(path.read_text())

	def _field_by_name(self, doctype_json, fieldname):
		return next(field for field in doctype_json["fields"] if field["fieldname"] == fieldname)

	def test_document_classes_are_document_subclasses(self):
		self.assertTrue(issubclass(SyncPartner, Document))
		self.assertTrue(issubclass(SyncPartnerType, Document))
		self.assertTrue(issubclass(SyncRun, Document))
		self.assertTrue(issubclass(SyncRunItem, Document))

	def test_sync_definition_partner_column_metadata_uses_stored_select_options(self):
		sync_definition_json = self._load_doctype_json("sync/doctype/sync_definition/sync_definition.json")
		field_mapping_json = self._load_doctype_json("sync/doctype/sync_field_mapping/sync_field_mapping.json")
		modified_field_json = self._load_doctype_json("sync/doctype/sync_modified_field/sync_modified_field.json")

		for fieldname in ("partner_columns", "partner_columns_signature", "partner_columns_loaded_at"):
			field = self._field_by_name(sync_definition_json, fieldname)
			self.assertEqual(field.get("hidden"), 1)
			self.assertEqual(field.get("read_only"), 1)

		self.assertEqual(self._field_by_name(sync_definition_json, "partner_columns")["fieldtype"], "JSON")
		self.assertEqual(self._field_by_name(sync_definition_json, "partner_identity_field")["fieldtype"], "Select")
		self.assertEqual(self._field_by_name(sync_definition_json, "partner_frappe_identity_field")["fieldtype"], "Select")
		self.assertEqual(self._field_by_name(sync_definition_json, "frappe_partner_identity_field")["fieldtype"], "Select")
		self.assertEqual(self._field_by_name(field_mapping_json, "partner_field")["fieldtype"], "Select")
		self.assertEqual(self._field_by_name(modified_field_json, "field_name")["fieldtype"], "Select")

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
			field_mapping=[SimpleNamespace(frappe_field=" name ", partner_field=" id ", direction="")]
		)

		sync_definition_module.SyncDefinition.validate_field_mapping(doc)

		self.assertEqual(doc.field_mapping[0].frappe_field, "name")
		self.assertEqual(doc.field_mapping[0].partner_field, "id")
		self.assertEqual(doc.field_mapping[0].direction, "Frappe <-> Partner")

		duplicate_doc = FakeSyncDefinitionDoc(
			field_mapping=[
				SimpleNamespace(frappe_field="name", partner_field="id", direction="Frappe <-> Partner"),
				SimpleNamespace(frappe_field=" name ", partner_field="external_id", direction="Frappe -> Partner"),
			]
		)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("duplicate")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_field_mapping(duplicate_doc)

	def test_validate_field_mapping_rejects_invalid_direction(self):
		doc = FakeSyncDefinitionDoc(
			field_mapping=[SimpleNamespace(frappe_field="name", partner_field="id", direction="Outbound")]
		)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid-direction")) as mock_throw:
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_field_mapping(doc)

		mock_throw.assert_called_once_with(
			"Direction must be one of: Frappe <-> Partner, Frappe -> Partner, Frappe <- Partner"
		)

	def test_validate_source_settings_requires_table_name(self):
		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_source_settings(FakeSyncDefinitionDoc())
		doc = FakeSyncDefinitionDoc(table_name=" tabTask ", read_query=" select * from tabTask ")
		sync_definition_module.SyncDefinition.validate_source_settings(doc)
		self.assertEqual(doc.table_name, "tabTask")
		self.assertEqual(doc.read_query, "select * from tabTask")

	def test_validate_modified_fields_and_preview_limit_reject_invalid_values(self):
		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_modified_fields(
					FakeSyncDefinitionDoc(use_last_sync_date=1, frappe_modified_field_rows=[], partner_modified_field_rows=[SimpleNamespace(field_name="updated_at")])
				)
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_modified_fields(
					FakeSyncDefinitionDoc(use_last_sync_date=1, frappe_modified_field_rows=[SimpleNamespace(field_name="modified")], partner_modified_field_rows=[])
				)
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_preview_limit(FakeSyncDefinitionDoc(preview_limit=0))

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
		doc = FakeSyncDefinitionDoc(table_name="people", read_query="select * from people where active = 1", delete_missing=1)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("unsafe-source")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_source_settings(doc)

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

	def test_getters_and_modified_field_rows_are_normalized(self):
		doc = FakeSyncDefinitionDoc(
			match_fields=[SimpleNamespace(frappe_field=" name "), SimpleNamespace(frappe_field="")],
			field_mapping=[
				SimpleNamespace(frappe_field=" name ", partner_field=" id ", direction=None),
				SimpleNamespace(frappe_field="", partner_field="status", direction="Frappe <-> Partner"),
			],
			value_mapping=[
				SimpleNamespace(frappe_field=" status ", frappe_value="Open", partner_value="1"),
				SimpleNamespace(frappe_field="", frappe_value="Closed", partner_value="0"),
			],
			frappe_modified_field_rows=[SimpleNamespace(field_name="modified"), SimpleNamespace(field_name="changed_on")],
			partner_modified_field_rows=[SimpleNamespace(field_name="updated_at"), SimpleNamespace(field_name="partner_changed")],
		)

		self.assertEqual(sync_definition_module.SyncDefinition.get_match_fields(doc), ["name"])
		self.assertEqual(
			sync_definition_module.SyncDefinition.get_field_mapping(doc),
			{"name": {"partner_field": "id", "direction": "Frappe <-> Partner"}},
		)
		self.assertEqual(sync_definition_module.SyncDefinition.get_value_mapping(doc), {"status": {"Open": "1"}})
		self.assertEqual(sync_definition_module.SyncDefinition.get_frappe_modified_fields(doc), ["modified", "changed_on"])
		self.assertEqual(sync_definition_module.SyncDefinition.get_partner_modified_fields(doc), ["updated_at", "partner_changed"])

	def test_export_payload_helpers_cover_preview_limit_and_serialization(self):
		doc = FakeSyncDefinitionDoc(
			value_mapping=[SimpleNamespace(frappe_field="status", frappe_value={"a": 1}, partner_value=["x"])],
			field_mapping=[SimpleNamespace(frappe_field="name", partner_field="id", direction="Frappe <- Partner")],
			match_fields=[SimpleNamespace(frappe_field="name")],
			frappe_modified_field_rows=[SimpleNamespace(field_name="modified")],
			partner_modified_field_rows=[SimpleNamespace(field_name="updated_at")],
			preview_limit="invalid",
			one_way_match_mode="all_matches",
		)

		self.assertEqual(sync_definition_module.SyncDefinition.get_preview_limit(doc), 50)
		exported = sync_definition_module.SyncDefinition.as_export_dict(doc)
		self.assertEqual(exported["field_mapping"]["name"]["direction"], "Frappe <- Partner")
		self.assertEqual(exported["value_mapping"]["status"], {'{"a": 1}': '["x"]'})
		self.assertEqual(exported["one_way_match_mode"], "all_matches")
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
			{"frappe_field": "name", "partner_field": "id", "direction": "Frappe <-> Partner"},
		)
		self.assertEqual(
			sync_definition_module._extract_modified_fields(
				[{"field_name": "modified"}, SimpleNamespace(modified_field="updated_at"), {"frappe_field": "changed_on"}]
			),
			["modified", "updated_at", "changed_on"],
		)
		self.assertEqual(sync_definition_module.cstr(None), "")
		self.assertEqual(sync_definition_module.cstr({"a": 1}), '{"a": 1}')
