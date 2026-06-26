from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import frappe
import yaml


class DummyDoc:
	def __init__(self, payload):
		self._payload = dict(payload)
		self.name = self._payload.get("name")
		self.doctype = self._payload.get("doctype")

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

	def check_permission(self, permtype="read"):
		return None


class ApiTestCase(unittest.TestCase):
	def setUp(self):
		try:
			import sync.api as api  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))
		self.api = api
		self.original_get_doc = self.api.frappe.get_doc
		self.only_for_patcher = patch.object(self.api.frappe, "only_for", return_value=None)
		self.mock_only_for = self.only_for_patcher.start()
		self.addCleanup(self.only_for_patcher.stop)
		self.has_permission_patcher = patch.object(self.api.frappe, "has_permission", return_value=True)
		self.mock_has_permission = self.has_permission_patcher.start()
		self.addCleanup(self.has_permission_patcher.stop)


class TestSyncApi(ApiTestCase):

	def test_doctype_field_choices_include_hidden_data_fields(self):
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
		self.assertEqual(
			[field["fieldname"] for field in response["fields"]],
			["name", "creation", "modified", "subject", "internal_note"],
		)
		self.assertEqual(response["fields"][0]["label"], "Name")
		self.assertEqual(response["fields"][1]["label"], "Created On")
		self.assertEqual(response["fields"][2]["label"], "Modified")

	def test_get_sync_definition_field_choices_returns_empty_payload_for_blank_doctype(self):
		self.assertEqual(self.api.get_sync_definition_field_choices("   "), {"doctype": "", "fields": []})

	def test_get_sync_partner_table_columns_returns_connector_columns(self):
		partner = _doc_stub("Sync Partner", "PARTNER-1")
		calls = []
		connector = SimpleNamespace(
			describe_source_columns=lambda **kwargs: calls.append(kwargs) or ["id", "status", "updated_at"]
		)

		with (
			patch.object(self.api.frappe, "get_doc", return_value=partner),
			patch.object(self.api, "get_connector_for_partner", return_value=connector),
		):
			response = self.api.get_sync_partner_table_columns(
				partner.name,
				table_name="  dbo.SyncTable  ",
				read_query=" select id as external_id from dbo.SyncTable ",
			)

		self.assertEqual(response["sync_partner"], partner.name)
		self.assertEqual(response["table_name"], "dbo.SyncTable")
		self.assertEqual(response["read_query"], "select id as external_id from dbo.SyncTable")
		self.assertEqual(response["columns"], ["id", "status", "updated_at"])
		self.assertEqual(
			calls,
			[{"source": "dbo.SyncTable", "query": "select id as external_id from dbo.SyncTable"}],
		)

	def test_get_sync_partner_table_columns_raises_validation_error_on_connector_error(self):
		partner = _doc_stub("Sync Partner", "PARTNER-1")
		connector = SimpleNamespace(describe_source_columns=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("unsafe source")))

		with (
			patch.object(self.api.frappe, "get_doc", return_value=partner),
			patch.object(self.api, "get_connector_for_partner", return_value=connector),
			patch.object(self.api.frappe, "throw", side_effect=frappe.ValidationError("unsafe source")) as mock_throw,
		):
			with self.assertRaises(frappe.ValidationError):
				self.api.get_sync_partner_table_columns(partner.name, read_query=" select * from x ")

		mock_throw.assert_called_once()

	def test_yaml_export_import_roundtrip(self):
		sample_definition = {
			"doctype": "Sync Definition",
			"name": "SYNC-TEST",
			"sync_type": "Frappe -> Partner",
			"frequency_cron": "*/15 * * * *",
			"batch_size": 100,
			"filter_expression": '[["docstatus","=",0]]',
			"match_fields": [{"frappe_field": "name"}],
			"field_mapping": [{"frappe_field": "name", "partner_field": "name"}],
			"value_mapping": [
				{"frappe_field": "status", "frappe_value": "open", "partner_value": "1"},
				{
					"frappe_field": "gender",
					"frappe_value": None,
					"frappe_value_is_null": 1,
					"partner_value": "2",
					"partner_value_is_null": 0,
				},
			],
		}
		doc = DummyDoc(sample_definition)

		def fake_get_doc(doctype, name=None):
			if doctype == "Sync Definition":
				return doc
			if name is None:
				return self.original_get_doc(doctype)
			return self.original_get_doc(doctype, name)

		with (
			patch.object(self.api.frappe, "get_doc", side_effect=fake_get_doc),
			patch.object(self.api.frappe, "get_meta", side_effect=_fake_meta),
			patch("sync.sync.service.runtime.now_datetime", return_value=datetime(2026, 3, 18, 12, 0, 0)),
		):
			exported_yaml = self.api.export_sync_definition_yaml(sample_definition["name"])

		self.assertIsInstance(exported_yaml, str)
		exported_definition = yaml.safe_load(exported_yaml)["sync_definition"]
		self.assertEqual(exported_definition["name"], sample_definition["name"])
		self.assertEqual(exported_definition["value_mapping"][1]["frappe_value_is_null"], 1)
		self.assertIsNone(exported_definition["value_mapping"][1]["frappe_value"])

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

		runtime_frappe = SimpleNamespace(
			db=SimpleNamespace(exists=lambda *args, **kwargs: False, commit=lambda: None),
			get_doc=raise_missing,
			new_doc=fake_new_doc,
			get_meta=_fake_meta,
		)

		with (
			patch.object(self.api, "service_preview_import_sync_definition_yaml", return_value={"can_import": True, "documents": {}}),
			patch("sync.sync.service.runtime.frappe", new=runtime_frappe),
		):
			result = self.api.import_sync_definition_yaml(exported_yaml, overwrite=False)

		self.assertEqual(
			result,
			{
				"ok": True,
				"overwrite": False,
				"sync_definition": sample_definition["name"],
				"sync_partner": None,
				"sync_partner_type": None,
				"documents": {"Sync Definition": sample_definition["name"]},
			},
		)
		if inserted:
			self.assertEqual(inserted[0]._payload["doctype"], "Sync Definition")
			self.assertEqual(inserted[0]._payload["name"], sample_definition["name"])

	def test_import_sync_definition_yaml_returns_normalized_response_schema(self):
		with (
			patch.object(self.api, "service_preview_import_sync_definition_yaml", return_value={"can_import": True, "documents": {}}),
			patch.object(
				self.api,
				"service_import_sync_definition_yaml",
				return_value={
					"ok": True,
					"documents": {
						"Sync Definition": "SYNC-1",
						"Sync Partner": "PARTNER-1",
						"Sync Partner Type": "MSSQL",
					},
				},
			),
		):
			result = self.api.import_sync_definition_yaml("payload", overwrite=True)

		self.assertEqual(
			result,
			{
				"ok": True,
				"overwrite": True,
				"sync_definition": "SYNC-1",
				"sync_partner": "PARTNER-1",
				"sync_partner_type": "MSSQL",
				"documents": {
					"Sync Definition": "SYNC-1",
					"Sync Partner": "PARTNER-1",
					"Sync Partner Type": "MSSQL",
				},
			},
		)

	def test_partner_connection_response_shape(self):
		partner = _doc_stub("Sync Partner", "PARTNER-1", partner_type="mssql")
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
		partner = _doc_stub("Sync Partner", "PARTNER-1", partner_type="mssql")
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
		preview_data = {"actions": [{"direction": "Frappe -> Partner", "result": "ok"}]}
		definition = _doc_stub("Sync Definition", "SYNC-1", doctype_name="Task")
		partner = _doc_stub("Sync Partner", "PARTNER-1")
		definition.get = lambda key, default=None: {"doctype_name": "Task", "sync_partner": "PARTNER-1"}.get(key, default)

		with (
			patch.object(self.api, "SyncPreviewService", SimpleNamespace(predict=lambda definition, limit=50: preview_data)),
			patch.object(self.api.frappe, "get_doc", side_effect=[definition, partner]),
		):
			out = self.api.preview_sync_definition(definition.name)

		self.assertEqual(out, preview_data)

	def test_preview_sync_definition_coerces_limit_before_delegation(self):
		definition = _doc_stub("Sync Definition", "SYNC-1", doctype_name="Task")
		partner = _doc_stub("Sync Partner", "PARTNER-1")
		definition.get = lambda key, default=None: {"doctype_name": "Task", "sync_partner": "PARTNER-1"}.get(key, default)

		with (
			patch.object(self.api, "SyncPreviewService", SimpleNamespace(predict=lambda definition, limit=50: {"limit": limit})),
			patch.object(self.api.frappe, "get_doc", side_effect=[definition, partner]),
		):
			result = self.api.preview_sync_definition("SYNC-1", limit="7")

		self.assertEqual(result, {"limit": 7})

	def test_preview_import_yaml_reports_conflicts_and_missing_sections(self):
		payload = {
			"version": 2,
			"sync_partner_type": {"doctype": "Sync Partner Type", "name": "MSSQL"},
			"sync_definition": {
				"doctype": "Sync Definition",
				"name": "SYNC-NEW",
				"sync_type": "Frappe -> Partner",
				"sync_partner": "PARTNER-1",
			},
		}
		yaml_payload = yaml.safe_dump(payload, sort_keys=False)

		def fake_exists(doctype, name):
			return (doctype, name) == ("Sync Partner Type", "MSSQL")

		with patch(
			"sync.sync.service.runtime.frappe",
			new=SimpleNamespace(db=SimpleNamespace(exists=fake_exists), get_meta=_fake_meta),
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

	def test_preview_import_definition_yaml_marks_existing_documents_as_updates_with_overwrite(self):
		payload = {
			"version": 2,
			"sync_partner": {"doctype": "Sync Partner", "name": "PARTNER-1"},
		}
		yaml_payload = yaml.safe_dump(payload, sort_keys=False)

		with patch(
			"sync.sync.service.runtime.frappe",
			new=SimpleNamespace(db=SimpleNamespace(exists=lambda *args, **kwargs: True), get_meta=_fake_meta),
		):
			preview = self.api.preview_import_sync_definition_yaml(yaml_payload, overwrite=True)

		self.assertEqual(preview["documents"]["Sync Partner"]["status"], "update")
		self.assertEqual(preview["documents"]["Sync Partner"]["action"], "overwrite")
		self.assertEqual(preview["summary"]["update"], 1)

	def test_run_sync_now_denies_without_definition_write_permission(self):
		definition = _doc_stub("Sync Definition", "SYNC-1")
		definition.check_permission = _raise_permission_error

		with (
			patch.object(self.api.frappe, "get_doc", return_value=definition),
			patch.object(self.api, "service_execute_sync_definition") as mock_execute,
		):
			with self.assertRaises(frappe.PermissionError):
				self.api.run_sync_now("SYNC-1")

		mock_execute.assert_not_called()

	def test_run_due_sync_definitions_denies_without_system_manager_role(self):
		with (
			patch.object(self.api.frappe, "only_for", side_effect=frappe.PermissionError),
			patch.object(self.api, "service_run_due_sync_definitions") as mock_run_due,
		):
			with self.assertRaises(frappe.PermissionError):
				self.api.run_due_sync_definitions()

		mock_run_due.assert_not_called()

	def test_import_sync_definition_yaml_requires_write_permission_for_overwrite_documents(self):
		preview = {
			"can_import": True,
			"documents": {
				"Sync Definition": {
					"name": "SYNC-1",
					"status": "update",
					"exists": True,
					"action": "overwrite",
				}
			}
		}
		definition = _doc_stub("Sync Definition", "SYNC-1")
		definition.check_permission = _raise_permission_error

		with (
			patch.object(self.api, "service_preview_import_sync_definition_yaml", return_value=preview),
			patch.object(self.api.frappe, "get_doc", return_value=definition),
			patch.object(self.api, "service_import_sync_definition_yaml") as mock_import,
		):
			with self.assertRaises(frappe.PermissionError):
				self.api.import_sync_definition_yaml("payload", overwrite=True)

		mock_import.assert_not_called()

	def test_import_sync_definition_yaml_rejects_preview_that_cannot_import(self):
		preview = {"can_import": False, "error": "Invalid payload", "documents": {}}

		with (
			patch.object(self.api, "service_preview_import_sync_definition_yaml", return_value=preview),
			patch.object(self.api.frappe, "throw", side_effect=frappe.ValidationError("invalid-import")),
			patch.object(self.api, "service_import_sync_definition_yaml") as mock_import,
			self.assertRaises(frappe.ValidationError),
		):
			self.api.import_sync_definition_yaml("payload", overwrite=True)

		mock_import.assert_not_called()

	def test_import_sync_yaml_from_json_returns_same_schema_as_yaml_import(self):
		payload = {"yaml_payload": "sync_definition:\n  name: SYNC-1\n"}
		response_payload = {
			"ok": True,
			"overwrite": True,
			"sync_definition": "SYNC-1",
			"sync_partner": None,
			"sync_partner_type": None,
			"documents": {"Sync Definition": "SYNC-1"},
		}

		with patch("sync.api.import_sync_definition_yaml", return_value=response_payload) as mock_import:
			response = self.api.import_sync_yaml_from_json(payload, overwrite=True)

		self.assertEqual(response, response_payload)
		mock_import.assert_called_once_with(yaml_payload=payload["yaml_payload"], overwrite=True)

	def test_api_surface_keeps_only_canonical_alias_targets(self):
		for removed_name in (
			"run_due_syncs",
			"enqueue_sync",
			"preview_sync",
			"export_sync_yaml",
			"import_sync_yaml",
			"preview_import_sync_yaml",
		):
			self.assertFalse(hasattr(self.api, removed_name), removed_name)


def _fake_meta(doctype):
	child_fields = {
		"Sync Definition": [
			("sync_type", "Data", None),
			("frequency_cron", "Data", None),
			("batch_size", "Data", None),
			("filter_expression", "Data", None),
			("name", "Data", None),
			("sync_partner", "Data", None),
			("value_mapping", "Table", "Sync Value Mapping"),
		],
		"Sync Value Mapping": [
			("frappe_field", "Data", None),
			("frappe_value", "Data", None),
			("frappe_value_is_null", "Check", None),
			("partner_value", "Data", None),
			("partner_value_is_null", "Check", None),
		],
		"Sync Partner": [("name", "Data", None), ("partner_type", "Data", None)],
		"Sync Partner Type": [("name", "Data", None)],
	}

	class _Meta:
		def __init__(self, fields):
			self.fields = [
				SimpleNamespace(fieldname=fieldname, fieldtype=fieldtype, options=options)
				for fieldname, fieldtype, options in fields
			]

		def has_field(self, fieldname):
			return fieldname in {field.fieldname for field in self.fields}

		def get_table_fields(self, include_computed=True):
			return []

	return _Meta(child_fields.get(doctype, [("name", "Data", None)]))


class ApiContractTests(ApiTestCase):
	def test_run_sync_definition_delegates(self):
		definition = _doc_stub("Sync Definition", "SYNC-1", sync_partner="PARTNER-1")
		partner = _doc_stub("Sync Partner", "PARTNER-1")

		with (
			patch.object(self.api.frappe, "get_doc", side_effect=[definition, partner]),
			patch("sync.api.service_enqueue_sync_definition", return_value={"status": "queued"}) as mock_enqueue,
		):
			response = self.api.run_sync_definition("SYNC-1", trigger="manual", queue=True, dry_run=False)

		self.assertEqual(response["status"], "queued")
		mock_enqueue.assert_called_once_with("SYNC-1", trigger="manual", queue=True, dry_run=False)

	def test_run_sync_definition_rejects_invalid_trigger_before_service_call(self):
		definition = _doc_stub("Sync Definition", "SYNC-1", sync_partner="PARTNER-1")
		partner = _doc_stub("Sync Partner", "PARTNER-1")

		with (
			patch.object(self.api.frappe, "get_doc", side_effect=[definition, partner]),
			patch.object(self.api.frappe, "throw", side_effect=frappe.ValidationError("bad-trigger")),
			patch("sync.api.service_enqueue_sync_definition") as mock_enqueue,
			self.assertRaises(frappe.ValidationError),
		):
			self.api.run_sync_definition("SYNC-1", trigger="audit_dry_run", queue=True, dry_run=False)

		mock_enqueue.assert_not_called()

	def test_resolve_sync_run_item_checks_permissions_and_delegates(self):
		item = _doc_stub("Sync Run Item", "ITEM-1", sync_definition="SYNC-1")
		definition = _doc_stub("Sync Definition", "SYNC-1", partner="PARTNER-1", doctype_name="Task")
		partner = _doc_stub("Sync Partner", "PARTNER-1")

		with (
			patch.object(self.api.frappe, "get_doc", side_effect=[item, definition, partner]),
			patch("sync.api.service_resolve_sync_run_item", return_value={"ok": True}) as mock_resolve,
		):
			response = self.api.resolve_sync_run_item("ITEM-1", "Frappe <- Partner")

		self.assertEqual(response, {"ok": True})
		mock_resolve.assert_called_once_with("ITEM-1", "Frappe <- Partner")

	def test_run_due_sync_definitions_coerces_limit_and_queue(self):
		with patch("sync.api.service_run_due_sync_definitions", return_value=[{"status": "queued"}]) as mock_run_due:
			response = self.api.run_due_sync_definitions(limit="3", queue="0")

		self.assertEqual(response, [{"status": "queued"}])
		mock_run_due.assert_called_once_with(limit=3, queue=False)

	def test_recover_stale_runs_requires_definition_write_permission_and_delegates(self):
		definition = _doc_stub("Sync Definition", "SYNC-1")

		with (
			patch.object(self.api.frappe, "get_doc", return_value=definition),
			patch("sync.api.service_recover_stale_runs", return_value={"recovered_count": 1}) as mock_recover,
		):
			response = self.api.recover_stale_runs("SYNC-1", timeout_minutes="45")

		self.assertEqual(response, {"recovered_count": 1})
		mock_recover.assert_called_once_with(sync_definition_name="SYNC-1", timeout_minutes="45")

	def test_cleanup_sync_run_retention_delegates_with_system_manager_permission(self):
		with patch("sync.api.service_cleanup_sync_run_retention", return_value={"deleted_runs": 2}) as mock_cleanup:
			response = self.api.cleanup_sync_run_retention(retention_days_success="30", retention_days_error="120")

		self.assertEqual(response, {"deleted_runs": 2})
		mock_cleanup.assert_called_once_with(retention_days_success="30", retention_days_error="120")

	def test_import_sync_yaml_from_json_uses_embedded_payload_and_default_overwrite(self):
		payload = {"yaml_payload": "sync_definition:\n  name: SYNC-1\n"}
		response_payload = {
			"ok": True,
			"overwrite": True,
			"sync_definition": "SYNC-1",
			"sync_partner": None,
			"sync_partner_type": None,
			"documents": {"Sync Definition": "SYNC-1"},
		}

		with patch("sync.api.import_sync_definition_yaml", return_value=response_payload) as mock_import:
			response = self.api.import_sync_yaml_from_json(payload, overwrite=True)

		self.assertEqual(response, response_payload)
		mock_import.assert_called_once_with(yaml_payload=payload["yaml_payload"], overwrite=True)


def _doc_stub(doctype, name, **values):
	payload = {"doctype": doctype, "name": name, **values}
	doc = SimpleNamespace(**payload)
	doc.get = lambda key, default=None: payload.get(key, default)
	doc.check_permission = lambda permtype="read": None
	return doc


def _raise_permission_error(permtype="read"):
	raise frappe.PermissionError
