from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sync import setup
from sync.patches import migrate_frappe_write_hooks


class DummyDoc:
	def __init__(self):
		self.updated = None
		self.saved = False
		self.inserted = False

	def update(self, payload):
		self.updated = dict(payload)

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


class TestSetupHooks(unittest.TestCase):
	def test_after_migrate_delegates_to_default_partner_type_setup(self):
		with patch("sync.setup.ensure_default_partner_types") as mock_ensure:
			with patch("sync.setup.ensure_default_sync_settings") as mock_settings:
				setup.after_migrate()

		mock_ensure.assert_called_once_with()
		mock_settings.assert_called_once_with()

	def test_before_tests_delegates_to_default_partner_type_setup(self):
		with patch("sync.setup.ensure_default_partner_types") as mock_ensure:
			with patch("sync.setup.ensure_default_sync_settings") as mock_settings:
				setup.before_tests()

		mock_ensure.assert_called_once_with()
		mock_settings.assert_called_once_with()

	def test_ensure_default_partner_types_updates_existing_and_creates_missing(self):
		existing_codes = {"mssql"}
		existing_doc = DummyDoc()
		created_docs: list[DummyDoc] = []

		def fake_exists(doctype, name):
			return doctype == "Sync Partner Type" and name in existing_codes

		def fake_new_doc(_doctype):
			doc = DummyDoc()
			created_docs.append(doc)
			return doc

		with (
			patch.object(
				setup,
				"frappe",
				SimpleNamespace(
					db=_db_stub(exists=fake_exists),
					get_doc=lambda *args, **kwargs: existing_doc,
					new_doc=fake_new_doc,
				),
			),
		):
			setup.ensure_default_partner_types()

		self.assertEqual(existing_doc.updated["partner_type_code"], "mssql")
		self.assertTrue(existing_doc.saved)
		self.assertEqual(len(created_docs), 2)
		self.assertEqual(
			{doc.updated["partner_type_code"] for doc in created_docs},
			{"postgres", "firebird"},
		)
		self.assertTrue(all(doc.inserted for doc in created_docs))

	def test_ensure_default_sync_settings_sets_missing_defaults(self):
		doc = DummyDoc()

		with patch.object(
			setup,
			"frappe",
			SimpleNamespace(get_single=lambda doctype: doc),
		):
			setup.ensure_default_sync_settings()

		self.assertEqual(doc.updated["stale_run_timeout_minutes"], 180)
		self.assertEqual(doc.updated["run_retention_days_success"], 90)
		self.assertEqual(doc.updated["run_retention_days_error"], 365)
		self.assertTrue(doc.saved)

	def test_migrate_frappe_write_hooks_inserts_child_rows_and_clears_legacy_actions(self):
		inserted_docs: list[SimpleNamespace] = []
		set_values: list[tuple[str, str, dict, bool]] = []

		def fake_get_doc(payload):
			doc = SimpleNamespace(payload=dict(payload))
			doc.insert = lambda **kwargs: inserted_docs.append(doc)
			return doc

		def fake_has_column(_doctype, column):
			return column in {"frappe_after_insert_action", "frappe_after_update_action"}

		db = _db_stub(
			table_exists=lambda doctype: doctype == "Sync Definition",
			has_column=fake_has_column,
			exists=lambda *args, **kwargs: False,
			count=lambda *args, **kwargs: 0,
			set_value=lambda doctype, name, values, update_modified=False: set_values.append(
				(doctype, name, dict(values), update_modified)
			),
		)

		with patch.object(
			migrate_frappe_write_hooks,
			"frappe",
			SimpleNamespace(
				db=db,
				get_all=lambda doctype, fields: [
					{
						"name": "SYNC-1",
						"frappe_after_insert_action": "Submit",
						"frappe_after_update_action": "None",
					}
				],
				get_doc=fake_get_doc,
			),
		):
			migrate_frappe_write_hooks.execute()

		self.assertEqual(len(inserted_docs), 1)
		self.assertEqual(inserted_docs[0].payload["event"], "After Insert")
		self.assertEqual(inserted_docs[0].payload["action"], "Submit")
		self.assertEqual(
			set_values,
			[
				(
					"Sync Definition",
					"SYNC-1",
					{
						"frappe_after_insert_action": "None",
						"frappe_after_update_action": "None",
					},
					False,
				)
			],
		)
