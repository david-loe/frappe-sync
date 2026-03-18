from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import frappe
import yaml


class DummyDoc:
	def __init__(self, payload):
		self._payload = dict(payload)
		self.name = self._payload.get("name")

	def as_dict(self):
		return dict(self._payload)

	def update(self, updates):
		self._payload.update(updates)
		self.name = self._payload.get("name", self.name)

	def insert(self, **kwargs):
		self._payload.setdefault("name", f"{self._payload.get('doctype')}-AUTO")
		self.name = self._payload["name"]
		return self

	def get(self, key, default=None):
		return self._payload.get(key, default)


class TestSyncApi(unittest.TestCase):
	def setUp(self):
		try:
			import sync.api as api  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))
		self.api = api
		self.original_get_doc = self.api.frappe.get_doc

	def test_doctype_field_choices_filters_non_selectable_fields(self):
		meta = SimpleNamespace(
			fields=[
				SimpleNamespace(fieldname="subject", label="Subject", fieldtype="Data", hidden=0),
				SimpleNamespace(fieldname="items", label="Items", fieldtype="Table", hidden=0),
				SimpleNamespace(fieldname="internal_note", label="Internal Note", fieldtype="Data", hidden=1),
			]
		)

		with patch.object(self.api.frappe, "get_meta", return_value=meta):
			response = self.api.get_sync_definition_field_choices("Task")

		self.assertEqual(response["doctype"], "Task")
		self.assertEqual([field["fieldname"] for field in response["fields"]], ["name", "modified", "subject"])
		self.assertEqual(response["fields"][0]["label"], "Name")
		self.assertEqual(response["fields"][1]["label"], "Modified")

	def test_get_sync_definition_field_choices_returns_empty_payload_for_blank_doctype(self):
		self.assertEqual(self.api.get_sync_definition_field_choices("   "), {"doctype": "", "fields": []})

	def test_get_sync_partner_table_columns_returns_connector_columns(self):
		partner = SimpleNamespace(doctype="Sync Partner", name="PARTNER-1", get=getattr)
		connector = SimpleNamespace(describe_source_columns=lambda **kwargs: ["id", "status", "updated_at"])

		with (
			patch.object(self.api.frappe, "get_doc", return_value=partner),
			patch.object(self.api, "get_connector_for_partner", return_value=connector),
		):
			response = self.api.get_sync_partner_table_columns(partner.name, table_name="  dbo.SyncTable  ")

		self.assertEqual(response["sync_partner"], partner.name)
		self.assertEqual(response["table_name"], "dbo.SyncTable")
		self.assertEqual(response["columns"], ["id", "status", "updated_at"])

	def test_get_sync_partner_table_columns_raises_validation_error_on_connector_error(self):
		partner = SimpleNamespace(doctype="Sync Partner", name="PARTNER-1", get=getattr)
		connector = SimpleNamespace(describe_source_columns=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("unsafe source")))

		with (
			patch.object(self.api.frappe, "get_doc", return_value=partner),
			patch.object(self.api, "get_connector_for_partner", return_value=connector),
			patch.object(self.api.frappe, "throw", side_effect=frappe.ValidationError("unsafe source")) as mock_throw,
		):
			with self.assertRaises(frappe.ValidationError):
				self.api.get_sync_partner_table_columns(partner.name, query=" select * from x ")

		mock_throw.assert_called_once()

	def test_yaml_export_import_roundtrip(self):
		sample_definition = {
			"doctype": "Sync Definition",
			"name": "SYNC-TEST",
			"sync_type": "A->B",
			"frequency_cron": "*/15 * * * *",
			"batch_size": 100,
			"filter_expression": '[["docstatus","=",0]]',
			"key_fields": [{"frappe_field": "name"}],
			"field_mapping": [{"frappe_field": "name", "partner_field": "name"}],
			"value_mapping": [{"frappe_field": "status", "frappe_value": "open", "partner_value": "1"}],
		}
		doc = DummyDoc(sample_definition)

		def fake_get_doc(doctype, name=None):
			if doctype == "Sync Definition":
				return doc
			if name is None:
				return self.original_get_doc(doctype)
			return self.original_get_doc(doctype, name)

		with patch.object(self.api.frappe, "get_doc", side_effect=fake_get_doc):
			exported_yaml = self.api.export_sync_definition_yaml(sample_definition["name"])

		self.assertIsInstance(exported_yaml, str)
		self.assertEqual(yaml.safe_load(exported_yaml)["sync_definition"]["name"], sample_definition["name"])

		def raise_missing(doctype, name=None):
			if isinstance(doctype, dict):
				return DummyDoc(doctype)
			if doctype == "Sync Definition":
				raise frappe.DoesNotExistError
			if name is None:
				return self.original_get_doc(doctype)
			return self.original_get_doc(doctype, name)

		inserted = []

		def fake_new_doc(doctype):
			new_doc = DummyDoc({"doctype": doctype})
			inserted.append(new_doc)
			return new_doc

		with (
			patch.object(self.api.frappe, "get_doc", side_effect=raise_missing),
			patch.object(self.api.frappe, "new_doc", side_effect=fake_new_doc),
			patch.object(self.api.frappe.db, "exists", return_value=False),
			patch.object(self.api.frappe, "get_meta", side_effect=_fake_meta),
			patch.object(self.api.frappe.db, "commit"),
		):
			result = self.api.import_sync_definition_yaml(exported_yaml, overwrite=False)

		self.assertEqual(result, sample_definition["name"])
		if inserted:
			self.assertEqual(inserted[0]._payload["doctype"], "Sync Definition")
			self.assertEqual(inserted[0]._payload["name"], sample_definition["name"])

	def test_import_sync_definition_yaml_returns_definition_document_name_when_present(self):
		with patch.object(
			self.api,
			"service_import_sync_definition_yaml",
			return_value={"documents": {"Sync Definition": "SYNC-1", "Sync Partner": "PARTNER-1"}},
		):
			result = self.api.import_sync_definition_yaml("payload", overwrite=True)

		self.assertEqual(result, "SYNC-1")

	def test_partner_connection_response_shape(self):
		partner = SimpleNamespace(doctype="Sync Partner", name="PARTNER-1", partner_type="mssql", get=getattr)
		result = {"status": "ok", "details": "reachable"}

		with (
			patch.object(self.api.frappe, "get_doc", return_value=partner),
			patch.object(
				self.api,
				"get_connector_for_partner",
				return_value=SimpleNamespace(test_connection=lambda: result, ping=lambda: SimpleNamespace(ok=True, message="ok", details={"details": "reachable"})),
			),
		):
			response = self.api.test_sync_partner(partner.name)

		self.assertIn("status", response)
		self.assertIn("details", response)
		self.assertEqual(response["status"], "ok")

	def test_test_sync_partner_falls_back_to_ping_when_test_connection_missing(self):
		partner = SimpleNamespace(doctype="Sync Partner", name="PARTNER-1", partner_type="mssql", get=getattr)
		connector = SimpleNamespace(ping=lambda: SimpleNamespace(ok=False, message="down", details={"host": "db"}))

		with (
			patch.object(self.api.frappe, "get_doc", return_value=partner),
			patch.object(self.api, "get_connector_for_partner", return_value=connector),
		):
			response = self.api.test_sync_partner(partner.name)

		self.assertEqual(response["status"], "error")
		self.assertFalse(response["ok"])
		self.assertEqual(response["message"], "down")

	def test_preview_returns_summary(self):
		preview_data = {"actions": [{"direction": "A->B", "result": "ok"}]}
		definition = SimpleNamespace(doctype="Sync Definition", name="SYNC-1")

		with (
			patch.object(self.api, "SyncPreviewService", SimpleNamespace(predict=lambda definition, limit=50: preview_data)),
			patch.object(self.api.frappe, "get_doc", return_value=definition),
		):
			out = self.api.preview_sync_definition(definition.name)

		self.assertEqual(out, preview_data)

	def test_preview_sync_coerces_limit_before_delegation(self):
		with patch.object(self.api, "preview_sync_definition", return_value={"ok": True}) as mock_preview:
			result = self.api.preview_sync("SYNC-1", limit="7")

		self.assertEqual(result, {"ok": True})
		mock_preview.assert_called_once_with("SYNC-1", limit=7)

	def test_preview_import_yaml_reports_conflicts_and_missing_sections(self):
		payload = {
			"sync_partner_type": {"doctype": "Sync Partner Type", "name": "MSSQL"},
			"sync_definition": {
				"doctype": "Sync Definition",
				"name": "SYNC-NEW",
				"sync_type": "A->B",
				"sync_partner": "PARTNER-1",
			},
		}
		yaml_payload = yaml.safe_dump(payload, sort_keys=False)

		def fake_exists(doctype, name):
			return (doctype, name) == ("Sync Partner Type", "MSSQL")

		with (
			patch("sync.sync.service.runtime.frappe.db.exists", side_effect=fake_exists),
			patch("sync.sync.service.runtime.frappe.get_meta", side_effect=_fake_meta),
		):
			preview = self.api.preview_import_sync_definition_yaml(yaml_payload, overwrite=False)

		self.assertTrue(preview["ok"])
		self.assertTrue(preview["can_import"])
		self.assertEqual(preview["missing_payload_parts"], ["sync_partner"])
		self.assertEqual(preview["documents"]["Sync Partner Type"]["status"], "conflict")
		self.assertEqual(preview["documents"]["Sync Definition"]["status"], "create")
		self.assertEqual(preview["summary"]["conflict"], 1)
		self.assertEqual(preview["summary"]["create"], 1)
		self.assertEqual(preview["summary"]["missing_payload"], 1)

	def test_preview_import_yaml_marks_existing_documents_as_updates_with_overwrite(self):
		payload = {
			"sync_partner": {"doctype": "Sync Partner", "name": "PARTNER-1"},
		}
		yaml_payload = yaml.safe_dump(payload, sort_keys=False)

		with (
			patch("sync.sync.service.runtime.frappe.db.exists", return_value=True),
			patch("sync.sync.service.runtime.frappe.get_meta", side_effect=_fake_meta),
		):
			preview = self.api.preview_import_sync_yaml(yaml_payload, overwrite=True)

		self.assertEqual(preview["documents"]["Sync Partner"]["status"], "update")
		self.assertEqual(preview["documents"]["Sync Partner"]["action"], "overwrite")
		self.assertEqual(preview["summary"]["update"], 1)


def _fake_meta(doctype):
	child_fields = {
		"Sync Definition": ["sync_type", "frequency_cron", "batch_size", "filter_expression", "name", "sync_partner"],
		"Sync Partner": ["name", "partner_type"],
		"Sync Partner Type": ["name"],
	}

	class _Meta:
		def __init__(self, fields):
			self.fields = [SimpleNamespace(fieldname=field, fieldtype="Data") for field in fields]

		def has_field(self, fieldname):
			return fieldname in {field.fieldname for field in self.fields}

		def get_table_fields(self, include_computed=True):
			return []

	return _Meta(child_fields.get(doctype, ["name"]))


class ApiContractTests(unittest.TestCase):
	def test_run_sync_definition_delegates(self):
		try:
			import sync.api as api  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		with patch("sync.api.service_enqueue_sync_definition", return_value={"status": "queued"}) as mock_enqueue:
			response = api.run_sync_definition("SYNC-1", trigger="manual", queue=True, dry_run=False)

		self.assertEqual(response["status"], "queued")
		mock_enqueue.assert_called_once_with("SYNC-1", trigger="manual", queue=True, dry_run=False)

	def test_run_due_syncs_coerces_limit_and_queue(self):
		try:
			import sync.api as api  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		with patch("sync.api.service_run_due_sync_definitions", return_value=[{"status": "queued"}]) as mock_run_due:
			response = api.run_due_syncs(limit="3", queue="0")

		self.assertEqual(response, [{"status": "queued"}])
		mock_run_due.assert_called_once_with(limit=3, queue=False)

	def test_enqueue_sync_coerces_boolean_flags(self):
		try:
			import sync.api as api  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		with patch("sync.api.service_enqueue_sync_definition", return_value={"status": "queued"}) as mock_enqueue:
			response = api.enqueue_sync("SYNC-1", queue="yes", dry_run="1")

		self.assertEqual(response["status"], "queued")
		mock_enqueue.assert_called_once_with("SYNC-1", trigger="manual", queue=True, dry_run=True)

	def test_import_sync_yaml_from_json_uses_embedded_payload_and_default_overwrite(self):
		try:
			import sync.api as api  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		payload = {"yaml_payload": "sync_definition:\n  name: SYNC-1\n"}

		with patch("sync.api.import_sync_definition_yaml", return_value={"name": "SYNC-1"}) as mock_import:
			response = api.import_sync_yaml_from_json(payload, overwrite=True)

		self.assertEqual(response, {"name": "SYNC-1"})
		mock_import.assert_called_once_with(yaml_payload=payload["yaml_payload"], overwrite=True)
