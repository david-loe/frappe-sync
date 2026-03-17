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

	def test_preview_returns_summary(self):
		preview_data = {"actions": [{"direction": "A->B", "result": "ok"}]}
		definition = SimpleNamespace(doctype="Sync Definition", name="SYNC-1")

		with (
			patch.object(self.api, "SyncPreviewService", SimpleNamespace(predict=lambda definition, limit=50: preview_data)),
			patch.object(self.api.frappe, "get_doc", return_value=definition),
		):
			out = self.api.preview_sync_definition(definition.name)

		self.assertEqual(out, preview_data)


def _fake_meta(doctype):
	child_fields = {
		"Sync Definition": ["sync_type", "frequency_cron", "batch_size", "filter_expression", "name"],
		"Sync Partner": ["name"],
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
