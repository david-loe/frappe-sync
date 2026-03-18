from __future__ import annotations

import unittest
from unittest.mock import patch

from sync import setup


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


class TestSetupHooks(unittest.TestCase):
	def test_after_migrate_delegates_to_default_partner_type_setup(self):
		with patch("sync.setup.ensure_default_partner_types") as mock_ensure:
			setup.after_migrate()

		mock_ensure.assert_called_once_with()

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
			patch("sync.setup.frappe.db.exists", side_effect=fake_exists),
			patch("sync.setup.frappe.get_doc", return_value=existing_doc) as mock_get_doc,
			patch("sync.setup.frappe.new_doc", side_effect=fake_new_doc),
		):
			setup.ensure_default_partner_types()

		mock_get_doc.assert_called_once_with("Sync Partner Type", "mssql")
		self.assertEqual(existing_doc.updated["partner_type_code"], "mssql")
		self.assertTrue(existing_doc.saved)
		self.assertEqual(len(created_docs), 2)
		self.assertEqual(
			{doc.updated["partner_type_code"] for doc in created_docs},
			{"postgres", "firebird"},
		)
		self.assertTrue(all(doc.inserted for doc in created_docs))
