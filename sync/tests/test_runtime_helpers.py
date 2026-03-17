from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sync.sync.service import runtime
from sync.sync.service.connectors import ConnectorPingResult, ConnectorWriteResult


class FakeDoc:
	def __init__(self, values: dict[str, any]):
		self._values = dict(values)
		self.name = self._values.get("name")

	def get(self, key, default=None):
		return self._values.get(key, default)


class DummyMeta:
	def __init__(self, fields):
		self.fields = [SimpleNamespace(fieldname=f, fieldtype="Data") for f in fields]

	def has_field(self, fieldname):
		return fieldname in {field.fieldname for field in self.fields}


class TestRuntimeHelpers(unittest.TestCase):
	@patch("sync.sync.service.runtime._get_child_rows_by_options")
	def test_build_definition_config_parses_filters_and_mappings(self, mock_children):
		def fake_rows(parent, childdoctype):
			if childdoctype == "Sync Key Field":
				return [dict(frappe_field="name")]
			if childdoctype == "Sync Field Mapping":
				return [dict(frappe_field="name", partner_field="name")]
			if childdoctype == "Sync Value Mapping":
				return [dict(frappe_field="state", source_value="open", target_value="1")]
			return []

		mock_children.side_effect = fake_rows

		doc = FakeDoc(
			{
				"name": "SYNC-DELTA",
				"sync_type": "A->B",
				"frequency_cron": "*/5 * * * *",
				"use_last_sync_date": 1,
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"table_name": "tabTask",
				"filter_expression": '[["docstatus","=",0]]',
				"batch_size": 10,
				"create_new": 1,
				"delete_missing": 0,
				"conflict_policy": "newest_wins",
				"frappe_modified_field_rows": [SimpleNamespace(field_name="modified")],
				"partner_modified_field_rows": [SimpleNamespace(field_name="updated_at")],
			}
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])):
			config = runtime._build_definition_config(doc)

		self.assertEqual(config.key_fields, ["name"])
		self.assertEqual(config.mapping, {"name": "name"})
		self.assertEqual(config.value_mapping, {"state": {"open": "1"}})
		self.assertIsInstance(config.filters, list)
		self.assertEqual(config.use_last_sync_date, True)
		self.assertEqual(config.frappe_modified_fields, ["modified"])
		self.assertEqual(config.partner_modified_fields, ["updated_at"])
		self.assertEqual(config.table_name, "tabTask")
		self.assertIsNone(config.query)

	@patch("sync.sync.service.runtime._get_child_rows_by_options")
	def test_build_definition_config_falls_back_to_legacy_modified_fields(self, mock_children):
		def fake_rows(parent, childdoctype):
			if childdoctype == "Sync Key Field":
				return [dict(frappe_field="name")]
			if childdoctype == "Sync Field Mapping":
				return [dict(frappe_field="name", partner_field="name")]
			return []

		mock_children.side_effect = fake_rows

		doc = FakeDoc(
			{
				"name": "SYNC-LEGACY",
				"sync_type": "A->B",
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"table_name": "  tabTask  ",
				"query": "   ",
				"frappe_modified_fields": " modified \nchanged_on ",
				"partner_modified_fields": " updated_at \n partner_changed ",
			}
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])):
			config = runtime._build_definition_config(doc)

		self.assertEqual(config.table_name, "tabTask")
		self.assertIsNone(config.query)
		self.assertEqual(config.frappe_modified_fields, ["modified", "changed_on"])
		self.assertEqual(config.partner_modified_fields, ["updated_at", "partner_changed"])

	def test_build_record_key_consistent(self):
		key1 = runtime._build_record_key({"name": "AAA", "status": "open"})
		key2 = runtime._build_record_key({"status": "open", "name": "AAA"})
		self.assertEqual(key1, key2)

		key3 = runtime._build_record_key({"name": "BBB"})
		self.assertNotEqual(key1, key3)

	def test_sanitize_document_dict_removes_system_fields(self):
		doc = {
			"doctype": "Sync Definition",
			"name": "SYNC",
			"owner": "Administrator",
			"password": "secret",
			"_comments": "hidden",
		}

		class DummyMeta:
			def __init__(self, fields):
				self.fields = [SimpleNamespace(fieldname=f, fieldtype="Data") for f in fields]

			def has_field(self, fieldname):
				return fieldname in {field.fieldname for field in self.fields}

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=DummyMeta([])):
			sanitized = runtime._sanitize_document_dict(doc)

		self.assertNotIn("owner", sanitized)
		self.assertNotIn("_comments", sanitized)
		self.assertNotIn("password", sanitized)
		self.assertEqual(sanitized["name"], "SYNC")

	@patch("sync.sync.service.runtime.frappe.db.exists", return_value=True)
	@patch("sync.sync.service.runtime.frappe.get_doc")
	@patch("sync.sync.service.runtime.frappe.get_meta", return_value=DummyMeta([]))
	def test_upsert_document_returns_existing(self, *_):
		payload = {"doctype": "Sync Definition", "name": "SYNC-EXISTING", "status": "open"}
		name = runtime._upsert_document_from_payload("Sync Definition", payload, overwrite=False)
		self.assertEqual(name, "SYNC-EXISTING")

	@patch("sync.sync.service.runtime._create_run_item")
	@patch("sync.sync.service.runtime._create_run_item_change")
	@patch("sync.sync.service.runtime._update_doc_fields")
	@patch("sync.sync.service.runtime._get_frappe_records")
	@patch("sync.sync.service.runtime.get_connector_for_partner")
	@patch("sync.sync.service.runtime.frappe.get_doc")
	def test_run_engine_classifies_actions(
		self, mock_get_doc, mock_get_connector, mock_records, *_rest
	):
		mock_get_doc.return_value = SimpleNamespace(partner_type="mssql")
		mock_records.return_value = [{"name": "TASK-1", "status": "open"}]

		class DummyConnector:
			def ping(self):
				return ConnectorPingResult(ok=True, message="ok", details={})

			def upsert_record(self, **_kwargs):
				return ConnectorWriteResult(ok=True, message="created")

		mock_get_connector.return_value = DummyConnector()

		config = SimpleNamespace(
			name="SYNC-ENGINE",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="A->B",
			cron="* * * * *",
			filters=None,
			batch_size=10,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			table_name="tabTask",
			query=None,
			key_fields=["name"],
			mapping={"name": "name"},
			value_mapping={},
		)

		run_doc = SimpleNamespace(name="RUN-1")
		sync_definition_doc = SimpleNamespace(name="SYNC-ENGINE", sync_type="A->B")

		result = runtime._run_engine(sync_definition_doc, run_doc, config=config, dry_run=False)
		self.assertEqual(result["processed_count"], 1)
		self.assertEqual(result["success_count"], 1)
		self.assertEqual(result["skipped_count"], 0)
		self.assertEqual(result["error_count"], 0)
