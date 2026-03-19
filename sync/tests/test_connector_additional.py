from __future__ import annotations

from datetime import datetime
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sync.sync.service import connectors
from sync.sync.service.connectors import BasePartnerConnector, ConnectorPingResult, FirebirdConnector, MssqlConnector, PostgresConnector


def _field(fieldname: str, fieldtype: str = "Data"):
	return SimpleNamespace(fieldname=fieldname, fieldtype=fieldtype)


PARTNER_META = SimpleNamespace(
	fields=[
		_field("host"),
		_field("database_name"),
		_field("username"),
		_field("password", "Password"),
		_field("port"),
		_field("connect_timeout"),
		_field("query_timeout"),
		_field("encrypt"),
		_field("trusted_connection"),
		_field("trust_server_certificate"),
		_field("odbc_driver"),
		_field("charset"),
		_field("sslmode"),
		_field("connection_options"),
	]
)


class DummyPartner:
	def __init__(self, partner_type: str, **values):
		self.doctype = "Sync Partner"
		self.name = values.get("name", "PARTNER-1")
		self._values = {"partner_type": partner_type, **values}

	def get(self, key, default=None):
		return self._values.get(key, default)


class DummyBaseConnector(BasePartnerConnector):
	def ping(self) -> ConnectorPingResult:
		return ConnectorPingResult(ok=True, message="ok", details={})


class DummyFallbackConnector:
	def __init__(self, partner_doc):
		self.partner_doc = partner_doc


class TestConnectorAdditional(unittest.TestCase):
	def setUp(self):
		self.meta_patch = patch("sync.sync.service.connectors.frappe.get_meta", return_value=PARTNER_META)
		self.decrypt_patch = patch("sync.sync.service.connectors.get_decrypted_password", return_value=None)
		self.meta_patch.start()
		self.decrypt_patch.start()

	def tearDown(self):
		self.decrypt_patch.stop()
		self.meta_patch.stop()

	def test_base_partner_connector_default_methods(self):
		connector = DummyBaseConnector(DummyPartner("base"))

		self.assertEqual(connector.fetch_records().records, [])
		self.assertTrue(connector.upsert_record(record={"id": "A1"}, key_fields=["id"], mapping={}, dry_run=True).ok)
		self.assertFalse(connector.upsert_record(record={"id": "A1"}, key_fields=["id"], mapping={}, dry_run=False).ok)
		self.assertTrue(connector.delete_record(key_values={"id": "A1"}, dry_run=True).ok)
		self.assertFalse(connector.delete_record(key_values={"id": "A1"}, dry_run=False).ok)
		with self.assertRaisesRegex(RuntimeError, "does not support source-column inspection"):
			connector.describe_source_columns(source="table")

	def test_connector_text_parsers_and_normalizers(self):
		self.assertEqual(connectors._parse_config_text({"a": 1}), {"a": 1})
		self.assertEqual(connectors._parse_config_text('{"sslmode": "disable"}'), {"sslmode": "disable"})
		self.assertEqual(connectors._parse_config_text("host=db\nport=5432"), {"host": "db", "port": "5432"})
		self.assertEqual(connectors._parse_config_text(123), {})
		self.assertTrue(connectors._to_bool("yes"))
		self.assertFalse(connectors._to_bool("off"))
		self.assertEqual(connectors._to_non_negative_int("-3"), 0)
		self.assertEqual(connectors._to_non_negative_int("7"), 7)
		self.assertEqual(connectors._strip_trailing_semicolon("SELECT 1;"), "SELECT 1")

	def test_mssql_connect_builds_connection_string(self):
		driver_module = SimpleNamespace(connect=lambda conn_string, timeout=0: (conn_string, timeout))
		connector = MssqlConnector(
			DummyPartner(
				"mssql",
				host="db.internal",
				port="1433",
				database_name="sync_test",
				username="sa",
				password="secret",
				odbc_driver="ODBC Driver 18 for SQL Server",
				trusted_connection="1",
				encrypt="0",
				trust_server_certificate="1",
				connect_timeout="9",
			)
		)

		with patch.object(connector, "_load_driver_module", return_value=driver_module):
			conn_string, timeout = connector._connect()

		self.assertIn("SERVER=db.internal,1433", conn_string)
		self.assertIn("DATABASE=sync_test", conn_string)
		self.assertIn("UID=sa", conn_string)
		self.assertIn("PWD=secret", conn_string)
		self.assertIn("Trusted_Connection=yes", conn_string)
		self.assertIn("Encrypt=no", conn_string)
		self.assertIn("TrustServerCertificate=yes", conn_string)
		self.assertEqual(timeout, 9)

	def test_postgres_connect_builds_expected_kwargs(self):
		calls = []
		driver_module = SimpleNamespace(connect=lambda **kwargs: calls.append(kwargs) or kwargs)
		connector = PostgresConnector(
			DummyPartner(
				"postgres",
				host="db.internal",
				port="5433",
				database_name="sync_test",
				username="sync_user",
				password="secret",
				connect_timeout="8",
				sslmode="disable",
			)
		)

		with patch.object(connector, "_load_driver_module", return_value=driver_module):
			result = connector._connect()

		self.assertEqual(result["host"], "db.internal")
		self.assertEqual(result["port"], 5433)
		self.assertEqual(result["dbname"], "sync_test")
		self.assertEqual(result["user"], "sync_user")
		self.assertEqual(result["password"], "secret")
		self.assertEqual(result["connect_timeout"], 8)
		self.assertEqual(result["sslmode"], "disable")
		self.assertEqual(calls[0], result)

	def test_firebird_connect_builds_expected_kwargs(self):
		calls = []
		driver_module = SimpleNamespace(__name__="fdb", connect=lambda **kwargs: calls.append(kwargs) or kwargs)
		connector = FirebirdConnector(
			DummyPartner(
				"firebird",
				host="db.internal",
				port="3051",
				database_name="/firebird/data/sync_test.fdb",
				username="sysdba",
				password="masterkey",
				charset="WIN1252",
			)
		)

		with patch.object(connector, "_load_driver_module", return_value=driver_module):
			result = connector._connect()

		self.assertEqual(result["host"], "db.internal")
		self.assertEqual(result["port"], 3051)
		self.assertEqual(result["database"], "/firebird/data/sync_test.fdb")
		self.assertEqual(result["user"], "sysdba")
		self.assertEqual(result["password"], "masterkey")
		self.assertEqual(result["charset"], "WIN1252")
		self.assertEqual(calls[0], result)

	def test_get_partner_type_and_factory_cover_known_and_fallback_paths(self):
		with (
			patch(
				"sync.sync.service.connectors.frappe",
				new=SimpleNamespace(
					db=SimpleNamespace(exists=lambda *args, **kwargs: True),
					get_doc=lambda *args, **kwargs: SimpleNamespace(get=lambda key, default=None: {"partner_type_code": "postgres"}.get(key, default)),
				),
			),
		):
			self.assertEqual(connectors.get_partner_type(DummyPartner("custom")), "postgres")

		self.assertIsInstance(connectors.get_connector_for_partner(DummyPartner("mssql", host="db", database_name="sync")), MssqlConnector)
		self.assertIsInstance(connectors.get_connector_for_partner(DummyPartner("postgres", host="db", database_name="sync", username="u")), PostgresConnector)
		self.assertIsInstance(connectors.get_connector_for_partner(DummyPartner("firebird", host="db", database_name="sync", username="u")), FirebirdConnector)

		with patch("sync.sync.service.connectors.RelationalConnector", DummyFallbackConnector):
			fallback = connectors.get_connector_for_partner(DummyPartner("unknown"))

		self.assertIsInstance(fallback, DummyFallbackConnector)


@unittest.skipUnless(os.environ.get("SYNC_TEST_FIREBIRD_HOST"), "Firebird integration env not configured")
class TestFirebirdConnectorIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.meta_patch = patch("sync.sync.service.connectors.frappe.get_meta", return_value=PARTNER_META)
		cls.decrypt_patch = patch("sync.sync.service.connectors.get_decrypted_password", return_value=None)
		cls.meta_patch.start()
		cls.decrypt_patch.start()
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
		cls._wait_for_connection()

	def setUp(self):
		with self.connector._connection() as connection:
			cursor = connection.cursor()
			cursor.execute(
				f"RECREATE TABLE {self.table_name} (id VARCHAR(64) NOT NULL PRIMARY KEY, status VARCHAR(64) NOT NULL, updated_at TIMESTAMP)",
			)
			connection.commit()
			cursor.close()

	@classmethod
	def tearDownClass(cls):
		cls.decrypt_patch.stop()
		cls.meta_patch.stop()
		super().tearDownClass()

	@classmethod
	def _wait_for_connection(cls):
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
		raise last_error or RuntimeError("Firebird was not ready in time")

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

		page = self.connector.fetch_records(source=self.table_name, batch_size=5, cursor="0", key_fields=["id"])
		self.assertEqual(len(page.records), 1)
		self.assertEqual(page.records[0]["ID"], "A1")
		self.assertEqual(page.records[0]["STATUS"], "closed")

		delete = self.connector.delete_record(key_values={"id": "A1"}, source=self.table_name)
		self.assertTrue(delete.ok)
		self.assertEqual(self.connector.fetch_records(source=self.table_name, batch_size=5, cursor="0", key_fields=["id"]).records, [])


@unittest.skipUnless(os.environ.get("SYNC_TEST_MSSQL_SERVER"), "MSSQL integration env not configured")
class TestMssqlConnectorIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.meta_patch = patch("sync.sync.service.connectors.frappe.get_meta", return_value=PARTNER_META)
		cls.decrypt_patch = patch("sync.sync.service.connectors.get_decrypted_password", return_value=None)
		cls.meta_patch.start()
		cls.decrypt_patch.start()
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
		cls._wait_for_connection()

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

	@classmethod
	def tearDownClass(cls):
		cls.decrypt_patch.stop()
		cls.meta_patch.stop()
		super().tearDownClass()

	@classmethod
	def _wait_for_connection(cls):
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
		raise last_error or RuntimeError("MSSQL was not ready in time")

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

		page = self.connector.fetch_records(source=self.table_name, batch_size=5, cursor="0", key_fields=["id"])
		self.assertEqual(len(page.records), 1)
		self.assertEqual(page.records[0]["id"], "A1")
		self.assertEqual(page.records[0]["status"], "closed")

		delete = self.connector.delete_record(key_values={"id": "A1"}, source=self.table_name)
		self.assertTrue(delete.ok)
		self.assertEqual(self.connector.fetch_records(source=self.table_name, batch_size=5, cursor="0", key_fields=["id"]).records, [])
