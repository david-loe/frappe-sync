from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import frappe
from frappe.model.document import Document

import sync
import sync.setup as sync_setup
from sync.sync.doctype.sync_definition import sync_definition as sync_definition_module
from sync.sync.doctype.sync_partner_type.sync_partner_type import SyncPartnerType
from sync.sync.doctype.sync_run_item.sync_run_item import SyncRunItem
from sync.sync.doctype.sync_run_item_change.sync_run_item_change import SyncRunItemChange


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


class FakeSyncDefinitionDoc:
	def __init__(self, **values):
		self.__dict__.update(values)
		self.key_fields = values.get("key_fields", [])
		self.field_mapping = values.get("field_mapping", [])
		self.value_mapping = values.get("value_mapping", [])
		self.frappe_modified_field_rows = values.get("frappe_modified_field_rows", [])
		self.partner_modified_field_rows = values.get("partner_modified_field_rows", [])
		self.frappe_modified_fields = values.get("frappe_modified_fields", "")
		self.partner_modified_fields = values.get("partner_modified_fields", "")
		self.preview_limit = values.get("preview_limit", 50)
		self.delete_missing = values.get("delete_missing", 0)
		self.query = values.get("query")
		self.table_name = values.get("table_name")
		self.use_last_sync_date = values.get("use_last_sync_date", 1)
		self.title = values.get("title", "SYNC-DEF")
		self.name = values.get("name", "SYNC-DEF")
		self.enabled = values.get("enabled", 1)
		self.partner = values.get("partner", "PARTNER-1")
		self.sync_type = values.get("sync_type", "A->B")
		self.doctype_name = values.get("doctype_name", "Task")
		self.frequency_cron = values.get("frequency_cron", "*/15 * * * *")
		self.filter_expression = values.get("filter_expression")
		self.batch_size = values.get("batch_size", 50)
		self.timestamp_buffer_seconds = values.get("timestamp_buffer_seconds", 15)
		self.create_new = values.get("create_new", 1)
		self.conflict_policy = values.get("conflict_policy", "newest_wins")
		self.export_mask_credentials = values.get("export_mask_credentials", 1)
		self.next_run_at = values.get("next_run_at")
		self.last_run_status = values.get("last_run_status")
		self.last_run_summary = values.get("last_run_summary")

	def get(self, key, default=None):
		return getattr(self, key, default)

	def append(self, fieldname, payload):
		getattr(self, fieldname).append(SimpleNamespace(**payload))

	def get_key_fields(self):
		return sync_definition_module.SyncDefinition.get_key_fields(self)

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

	def _ensure_modified_field_rows(self, table_fieldname: str, legacy_fieldname: str):
		return sync_definition_module.SyncDefinition._ensure_modified_field_rows(self, table_fieldname, legacy_fieldname)


class TestSetupModule(unittest.TestCase):
	def test_package_version_is_exposed(self):
		self.assertEqual(sync.__version__, "0.0.1")

	def test_after_migrate_delegates_to_default_partner_type_setup(self):
		with patch("sync.setup.ensure_default_partner_types") as mock_ensure:
			sync_setup.after_migrate()

		mock_ensure.assert_called_once_with()

	def test_ensure_default_partner_types_updates_existing_and_creates_missing(self):
		existing_doc = MutableDoc()
		new_doc = MutableDoc()

		def fake_exists(doctype, name):
			return name == "mssql"

		with (
			patch("sync.setup.frappe.db.exists", side_effect=fake_exists),
			patch("sync.setup.frappe.get_doc", return_value=existing_doc),
			patch("sync.setup.frappe.new_doc", return_value=new_doc),
		):
			sync_setup.ensure_default_partner_types()

		self.assertTrue(existing_doc.saved)
		self.assertEqual(existing_doc.updated[0]["partner_type_code"], "mssql")
		self.assertTrue(new_doc.inserted)
		self.assertEqual([payload["partner_type_code"] for payload in new_doc.updated], ["postgres", "firebird"])


class TestSyncDefinitionDoctype(unittest.TestCase):
	def test_document_classes_are_document_subclasses(self):
		self.assertTrue(issubclass(SyncPartnerType, Document))
		self.assertTrue(issubclass(SyncRunItem, Document))
		self.assertTrue(issubclass(SyncRunItemChange, Document))

	def test_validate_key_fields_throws_for_missing_mapping(self):
		doc = FakeSyncDefinitionDoc(
			key_fields=[SimpleNamespace(frappe_field="name"), SimpleNamespace(frappe_field="status")],
			field_mapping=[SimpleNamespace(frappe_field="name", partner_field="id")],
		)

		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("missing")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_key_fields(doc)

	def test_validate_source_settings_rejects_invalid_source_combinations(self):
		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_source_settings(
					FakeSyncDefinitionDoc(table_name="tabTask", query="select * from tabTask")
				)
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_source_settings(FakeSyncDefinitionDoc())
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_source_settings(
					FakeSyncDefinitionDoc(query="select * from tabTask", delete_missing=1)
				)

	def test_validate_modified_fields_and_preview_limit_reject_invalid_values(self):
		with patch.object(sync_definition_module.frappe, "throw", side_effect=frappe.ValidationError("invalid")):
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_modified_fields(
					FakeSyncDefinitionDoc(use_last_sync_date=1, frappe_modified_fields="", partner_modified_fields="updated_at")
				)
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_modified_fields(
					FakeSyncDefinitionDoc(use_last_sync_date=1, frappe_modified_fields="modified", partner_modified_fields="")
				)
			with self.assertRaises(frappe.ValidationError):
				sync_definition_module.SyncDefinition.validate_preview_limit(FakeSyncDefinitionDoc(preview_limit=0))

	def test_getters_and_legacy_modified_fields_are_normalized(self):
		doc = FakeSyncDefinitionDoc(
			key_fields=[SimpleNamespace(frappe_field="name"), SimpleNamespace(frappe_field="")],
			field_mapping=[
				SimpleNamespace(frappe_field="name", partner_field="id", direction=None),
				SimpleNamespace(frappe_field="", partner_field="status", direction="Both"),
			],
			value_mapping=[
				SimpleNamespace(frappe_field="status", frappe_value="Open", partner_value="1"),
				SimpleNamespace(frappe_field="", frappe_value="Closed", partner_value="0"),
			],
			frappe_modified_fields="modified\nchanged_on",
			partner_modified_fields="updated_at\npartner_changed",
		)

		self.assertEqual(sync_definition_module.SyncDefinition.get_key_fields(doc), ["name"])
		self.assertEqual(
			sync_definition_module.SyncDefinition.get_field_mapping(doc),
			{"name": {"partner_field": "id", "direction": "Both"}},
		)
		self.assertEqual(sync_definition_module.SyncDefinition.get_value_mapping(doc), {"status": {"Open": "1"}})
		sync_definition_module.SyncDefinition.ensure_modified_field_rows_from_legacy(doc)
		self.assertEqual(
			[row.field_name for row in doc.frappe_modified_field_rows],
			["modified", "changed_on"],
		)
		self.assertEqual(
			[row.field_name for row in doc.partner_modified_field_rows],
			["updated_at", "partner_changed"],
		)
		sync_definition_module.SyncDefinition.sync_modified_fields_legacy_storage(doc)
		self.assertEqual(doc.frappe_modified_fields, "modified\nchanged_on")
		self.assertEqual(doc.partner_modified_fields, "updated_at\npartner_changed")

	def test_export_payload_helpers_cover_preview_limit_and_serialization(self):
		doc = FakeSyncDefinitionDoc(
			value_mapping=[SimpleNamespace(frappe_field="status", frappe_value={"a": 1}, partner_value=["x"])],
			field_mapping=[SimpleNamespace(frappe_field="name", partner_field="id", direction="Partner to Frappe")],
			key_fields=[SimpleNamespace(frappe_field="name")],
			frappe_modified_field_rows=[SimpleNamespace(field_name="modified")],
			partner_modified_field_rows=[SimpleNamespace(field_name="updated_at")],
			preview_limit="invalid",
		)

		self.assertEqual(sync_definition_module.SyncDefinition.get_preview_limit(doc), 50)
		exported = sync_definition_module.SyncDefinition.as_export_dict(doc)
		self.assertEqual(exported["field_mapping"]["name"]["direction"], "Partner to Frappe")
		self.assertEqual(exported["value_mapping"]["status"], {'{"a": 1}': '["x"]'})
		payload = sync_definition_module.SyncDefinition.get_export_payload(doc)
		self.assertIn("sync_definition", payload)
		self.assertTrue(payload["mask_credentials"])

	def test_helper_functions_cover_edge_cases(self):
		self.assertEqual(sync_definition_module._split_lines(" a \n\n b "), ["a", "b"])
		self.assertIsNone(sync_definition_module._clean_value("   "))
		self.assertEqual(
			sync_definition_module._extract_modified_fields(
				[{"field_name": "modified"}, SimpleNamespace(modified_field="updated_at"), {"frappe_field": "changed_on"}]
			),
			["modified", "updated_at", "changed_on"],
		)
		self.assertEqual(sync_definition_module.cstr(None), "")
		self.assertEqual(sync_definition_module.cstr({"a": 1}), '{"a": 1}')
