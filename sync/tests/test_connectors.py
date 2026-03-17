from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sync.sync.service.connectors import FirebirdConnector, MssqlConnector, PostgresConnector


class DummyPartner:
	def __init__(self, partner_type: str):
		self.partner_type = partner_type
		self.doctype = "Sync Partner"

	def get(self, key, default=None):
		return default


class TestConnectorPing(unittest.TestCase):
	def setUp(self):
		self.meta_patch = patch(
			"sync.sync.service.connectors.frappe.get_meta",
			return_value=SimpleNamespace(fields=[]),
		)
		self.meta_patch.start()

	def tearDown(self):
		self.meta_patch.stop()

	def test_mssql_connector_requires_server_database_user(self):
		connector = MssqlConnector(DummyPartner("mssql"))
		ping = connector.ping()
		self.assertFalse(ping.ok)
		self.assertIn("Missing required config", ping.message)

	def test_postgres_connector_requires_host_database_user(self):
		connector = PostgresConnector(DummyPartner("postgres"))
		ping = connector.ping()
		self.assertFalse(ping.ok)
		self.assertIn("Missing required config", ping.message)

	def test_firebird_connector_requires_host_database_user(self):
		connector = FirebirdConnector(DummyPartner("firebird"))
		ping = connector.ping()
		self.assertFalse(ping.ok)
		self.assertIn("Missing required config", ping.message)
