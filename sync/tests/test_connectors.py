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

	def test_relational_connector_describes_table_columns(self):
		connector = MssqlConnector(DummyPartner("mssql"))
		cursor = _FakeCursor(description=[("id",), ("status",), ("updated_at",)])
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			columns = connector.describe_source_columns(source="dbo.SyncTable")

		self.assertEqual(columns, ["id", "status", "updated_at"])
		self.assertEqual(len(cursor.executed), 1)
		self.assertIn("WHERE 1 = 0", cursor.executed[0][0])
		self.assertEqual(cursor.executed[0][1], [])

	def test_relational_connector_rejects_query_column_inspection(self):
		connector = PostgresConnector(DummyPartner("postgres"))

		with self.assertRaisesRegex(RuntimeError, "table sources only"):
			connector.describe_source_columns(query="select * from sync_table")


class _FakeCursor:
	def __init__(self, description):
		self.description = description
		self.executed = []

	def execute(self, sql, params):
		self.executed.append((sql, params))

	def close(self):
		return None


class _FakeConnection:
	def __init__(self, cursor):
		self._cursor = cursor

	def cursor(self):
		return self._cursor

	def rollback(self):
		return None

	def close(self):
		return None
