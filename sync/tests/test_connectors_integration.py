from __future__ import annotations

from datetime import datetime
import os
import time
import unittest
from unittest.mock import patch

from sync.sync.service.connectors import FirebirdConnector, MssqlConnector, PostgresConnector
from sync.tests.test_connectors import DummyPartner, PARTNER_META


class LiveConnectorTestMixin:
	connector = None
	table_name = ""
	meta_patch = None
	decrypt_patch = None

	@classmethod
	def _patch_frappe_helpers(cls):
		cls.meta_patch = patch("sync.sync.service.connectors.frappe.get_meta", return_value=PARTNER_META)
		cls.decrypt_patch = patch("sync.sync.service.connectors.get_decrypted_password", return_value=None)
		cls.meta_patch.start()
		cls.decrypt_patch.start()

	@classmethod
	def tearDownClass(cls):
		if cls.decrypt_patch:
			cls.decrypt_patch.stop()
		if cls.meta_patch:
			cls.meta_patch.stop()
		super().tearDownClass()

	@classmethod
	def _wait_for_connection(cls, label: str):
		last_error = None
		for _ in range(30):
			try:
				ping = cls.connector.ping()
				if ping.ok:
					return
				last_error = RuntimeError(ping.message)
			except Exception as exc:  # pragma: no cover - readiness loop
				last_error = exc
			time.sleep(2)
		raise last_error or RuntimeError(f"{label} was not ready in time")


@unittest.skipUnless(os.environ.get("SYNC_TEST_POSTGRES_HOST"), "Postgres integration env not configured")
class TestPostgresConnectorIntegration(LiveConnectorTestMixin, unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._patch_frappe_helpers()
		cls.connector = PostgresConnector(
			DummyPartner(
				"postgres",
				host=os.environ["SYNC_TEST_POSTGRES_HOST"],
				port=os.environ.get("SYNC_TEST_POSTGRES_PORT", "5432"),
				database_name=os.environ["SYNC_TEST_POSTGRES_DB"],
				username=os.environ["SYNC_TEST_POSTGRES_USER"],
				password=os.environ["SYNC_TEST_POSTGRES_PASSWORD"],
				connect_timeout="10",
				sslmode=os.environ.get("SYNC_TEST_POSTGRES_SSLMODE", "disable"),
			)
		)
		cls.table_name = os.environ.get("SYNC_TEST_POSTGRES_TABLE", "public.sync_connector_test")

		with cls.connector._connection() as connection:
			cursor = connection.cursor()
			cursor.execute(
				f"""
				CREATE TABLE IF NOT EXISTS {cls.table_name} (
					id TEXT PRIMARY KEY,
					status TEXT NOT NULL,
					updated_at TIMESTAMP NULL
				)
				"""
			)
			connection.commit()
			cursor.close()

	def setUp(self):
		with self.connector._connection() as connection:
			cursor = connection.cursor()
			cursor.execute(f"TRUNCATE TABLE {self.table_name}")
			connection.commit()
			cursor.close()

	def test_live_postgres_connector_crud_and_introspection(self):
		insert = self.connector.upsert_record(
			record={"id": "A1", "status": "open", "updated_at": datetime(2026, 3, 17, 12, 0)},
			key_fields=["name"],
			mapping={"name": "id", "status": "status", "modified": "updated_at"},
			source=self.table_name,
		)
		self.assertTrue(insert.ok)
		self.assertIn("insert succeeded", insert.message)

		describe = self.connector.describe_source_columns(source=self.table_name)
		self.assertEqual(describe, ["id", "status", "updated_at"])

		update = self.connector.upsert_record(
			record={"id": "A1", "status": "closed", "updated_at": datetime(2026, 3, 17, 12, 5)},
			key_fields=["name"],
			mapping={"name": "id", "status": "status", "modified": "updated_at"},
			source=self.table_name,
		)
		self.assertTrue(update.ok)
		self.assertIn("update succeeded", update.message)

		page = self.connector.fetch_records(source=self.table_name, batch_size=1, key_fields=["id"])
		self.assertEqual(len(page.records), 1)
		self.assertEqual(page.records[0]["id"], "A1")
		self.assertEqual(page.records[0]["status"], "closed")
		self.assertIsNone(page.next_cursor)

		query_page = self.connector.fetch_records(
			query=f"SELECT id, status, updated_at FROM {self.table_name}",
			batch_size=5,
			key_fields=["id"],
		)
		self.assertEqual(len(query_page.records), 1)
		self.assertEqual(query_page.records[0]["id"], "A1")

		delete = self.connector.delete_record(key_values={"id": "A1"}, source=self.table_name)
		self.assertTrue(delete.ok)
		self.assertIn("delete succeeded", delete.message)

		final_page = self.connector.fetch_records(source=self.table_name, batch_size=5, key_fields=["id"])
		self.assertEqual(final_page.records, [])


@unittest.skipUnless(os.environ.get("SYNC_TEST_FIREBIRD_HOST"), "Firebird integration env not configured")
class TestFirebirdConnectorIntegration(LiveConnectorTestMixin, unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._patch_frappe_helpers()
		cls.connector = FirebirdConnector(
			DummyPartner(
				"firebird",
				host=os.environ["SYNC_TEST_FIREBIRD_HOST"],
				port=os.environ.get("SYNC_TEST_FIREBIRD_PORT", "3050"),
				database_name=os.environ["SYNC_TEST_FIREBIRD_DB"],
				username=os.environ.get("SYNC_TEST_FIREBIRD_USER", "sysdba"),
				password=os.environ["SYNC_TEST_FIREBIRD_PASSWORD"],
				charset=os.environ.get("SYNC_TEST_FIREBIRD_CHARSET", "UTF8"),
			)
		)
		cls.table_name = os.environ.get("SYNC_TEST_FIREBIRD_TABLE", "SYNC_CONNECTOR_TEST")
		cls._wait_for_connection("Firebird")

	def setUp(self):
		with self.connector._connection() as connection:
			cursor = connection.cursor()
			cursor.execute(
				f"RECREATE TABLE {self.table_name} (id VARCHAR(64) NOT NULL PRIMARY KEY, status VARCHAR(64) NOT NULL, updated_at TIMESTAMP)",
			)
			connection.commit()
			cursor.close()

	def test_live_firebird_connector_crud_and_introspection(self):
		insert = self.connector.upsert_record(
			record={"id": "A1", "status": "open", "updated_at": datetime(2026, 3, 17, 12, 0)},
			key_fields=["name"],
			mapping={"name": "id", "status": "status", "modified": "updated_at"},
			source=self.table_name,
		)
		self.assertTrue(insert.ok)

		describe = self.connector.describe_source_columns(source=self.table_name)
		self.assertEqual(describe, ["ID", "STATUS", "UPDATED_AT"])

		update = self.connector.upsert_record(
			record={"id": "A1", "status": "closed", "updated_at": datetime(2026, 3, 17, 12, 5)},
			key_fields=["name"],
			mapping={"name": "id", "status": "status", "modified": "updated_at"},
			source=self.table_name,
		)
		self.assertTrue(update.ok)

		page = self.connector.fetch_records(source=self.table_name, batch_size=5, key_fields=["id"])
		self.assertEqual(len(page.records), 1)
		self.assertEqual(page.records[0]["ID"], "A1")
		self.assertEqual(page.records[0]["STATUS"], "closed")

		delete = self.connector.delete_record(key_values={"id": "A1"}, source=self.table_name)
		self.assertTrue(delete.ok)
		self.assertEqual(self.connector.fetch_records(source=self.table_name, batch_size=5, key_fields=["id"]).records, [])


@unittest.skipUnless(os.environ.get("SYNC_TEST_MSSQL_SERVER"), "MSSQL integration env not configured")
class TestMssqlConnectorIntegration(LiveConnectorTestMixin, unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._patch_frappe_helpers()
		cls.connector = MssqlConnector(
			DummyPartner(
				"mssql",
				host=os.environ["SYNC_TEST_MSSQL_SERVER"],
				port=os.environ.get("SYNC_TEST_MSSQL_PORT", "1433"),
				database_name=os.environ["SYNC_TEST_MSSQL_DB"],
				username=os.environ.get("SYNC_TEST_MSSQL_USER", "sa"),
				password=os.environ["SYNC_TEST_MSSQL_PASSWORD"],
				odbc_driver=os.environ.get("SYNC_TEST_MSSQL_DRIVER", "ODBC Driver 18 for SQL Server"),
				encrypt=os.environ.get("SYNC_TEST_MSSQL_ENCRYPT", "0"),
				trust_server_certificate=os.environ.get("SYNC_TEST_MSSQL_TRUST_SERVER_CERTIFICATE", "1"),
				connect_timeout="10",
			)
		)
		cls.table_name = os.environ.get("SYNC_TEST_MSSQL_TABLE", "dbo.sync_connector_test")
		cls._wait_for_connection("MSSQL")

	def setUp(self):
		with self.connector._connection() as connection:
			cursor = connection.cursor()
			cursor.execute(
				f"IF OBJECT_ID('{self.table_name}', 'U') IS NULL CREATE TABLE {self.table_name} (id NVARCHAR(64) PRIMARY KEY, status NVARCHAR(64) NOT NULL, updated_at DATETIME2 NULL)",
				[],
			)
			cursor.execute(f"DELETE FROM {self.table_name}", [])
			connection.commit()
			cursor.close()

	def test_live_mssql_connector_crud_and_introspection(self):
		insert = self.connector.upsert_record(
			record={"id": "A1", "status": "open", "updated_at": datetime(2026, 3, 17, 12, 0)},
			key_fields=["name"],
			mapping={"name": "id", "status": "status", "modified": "updated_at"},
			source=self.table_name,
		)
		self.assertTrue(insert.ok)

		describe = self.connector.describe_source_columns(source=self.table_name)
		self.assertEqual(describe, ["id", "status", "updated_at"])

		update = self.connector.upsert_record(
			record={"id": "A1", "status": "closed", "updated_at": datetime(2026, 3, 17, 12, 5)},
			key_fields=["name"],
			mapping={"name": "id", "status": "status", "modified": "updated_at"},
			source=self.table_name,
		)
		self.assertTrue(update.ok)

		page = self.connector.fetch_records(source=self.table_name, batch_size=5, key_fields=["id"])
		self.assertEqual(len(page.records), 1)
		self.assertEqual(page.records[0]["id"], "A1")
		self.assertEqual(page.records[0]["status"], "closed")

		delete = self.connector.delete_record(key_values={"id": "A1"}, source=self.table_name)
		self.assertTrue(delete.ok)
		self.assertEqual(self.connector.fetch_records(source=self.table_name, batch_size=5, key_fields=["id"]).records, [])
