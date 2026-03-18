from __future__ import annotations

from datetime import datetime
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sync.sync.service.connectors import (
	FirebirdConnector,
	ConnectorCreateOptions,
	MssqlConnector,
	PostgresConnector,
	get_connector_for_partner,
	get_partner_type,
	_parse_config_text,
	_strip_trailing_semicolon,
	_to_bool,
	_to_non_negative_int,
)


def _field(fieldname: str, fieldtype: str = "Data"):
	return SimpleNamespace(fieldname=fieldname, fieldtype=fieldtype)


PARTNER_META = SimpleNamespace(
	fields=[
		_field("host"),
		_field("server"),
		_field("database"),
		_field("database_name"),
		_field("user"),
		_field("username"),
		_field("password", "Password"),
		_field("port"),
		_field("connect_timeout"),
		_field("query_timeout"),
		_field("encrypt"),
		_field("trust_server_certificate"),
		_field("default_table"),
		_field("table_name"),
		_field("source"),
		_field("query"),
		_field("sslmode"),
		_field("connection_config"),
	]
)


class DummyPartner:
	def __init__(self, partner_type: str, **values):
		self.partner_type = partner_type
		self.doctype = "Sync Partner"
		self.name = values.get("name", "PARTNER-1")
		self._values = {"partner_type": partner_type, **values}

	def get(self, key, default=None):
		return self._values.get(key, default)


class TestConnectorConfig(unittest.TestCase):
	def setUp(self):
		self.meta_patch = patch(
			"sync.sync.service.connectors.frappe.get_meta",
			return_value=PARTNER_META,
		)
		self.decrypt_patch = patch(
			"sync.sync.service.connectors.get_decrypted_password",
			return_value="secret-password",
		)
		self.meta_patch.start()
		self.decrypt_patch.start()

	def tearDown(self):
		self.decrypt_patch.stop()
		self.meta_patch.stop()

	def test_extract_partner_config_merges_aliases_json_password_and_bools(self):
		partner = DummyPartner(
			"postgres",
			name="PARTNER-CONFIG",
			host="db.internal",
			database_name="sync_db",
			username="sync_user",
			port="5439",
			connect_timeout="12",
			encrypt="0",
			trust_server_certificate="yes",
			connection_config='{"default_table": "public.sync_records", "sslmode": "disable"}',
		)

		connector = PostgresConnector(partner)

		self.assertEqual(connector.config["host"], "db.internal")
		self.assertEqual(connector.config["server"], "db.internal")
		self.assertEqual(connector.config["database"], "sync_db")
		self.assertEqual(connector.config["database_name"], "sync_db")
		self.assertEqual(connector.config["user"], "sync_user")
		self.assertEqual(connector.config["username"], "sync_user")
		self.assertEqual(connector.config["default_table"], "public.sync_records")
		self.assertEqual(connector.config["sslmode"], "disable")
		self.assertEqual(connector.config["password"], "secret-password")
		self.assertEqual(connector.config["port"], 5439)
		self.assertEqual(connector.config["connect_timeout"], 12)
		self.assertFalse(connector.config["encrypt"])
		self.assertTrue(connector.config["trust_server_certificate"])

	def test_test_connection_wraps_ping_result(self):
		connector = MssqlConnector(DummyPartner("mssql", server="localhost", database="sync"))

		with patch.object(
			connector,
			"ping",
			return_value=SimpleNamespace(ok=True, message="reachable", details={"driver": "pyodbc"}),
		):
			result = connector.test_connection()

		self.assertEqual(
			result,
			{
				"status": "ok",
				"ok": True,
				"message": "reachable",
				"details": {"driver": "pyodbc"},
			},
		)


class TestConnectorPing(unittest.TestCase):
	def setUp(self):
		self.meta_patch = patch(
			"sync.sync.service.connectors.frappe.get_meta",
			return_value=PARTNER_META,
		)
		self.decrypt_patch = patch(
			"sync.sync.service.connectors.get_decrypted_password",
			return_value=None,
		)
		self.meta_patch.start()
		self.decrypt_patch.start()

	def tearDown(self):
		self.decrypt_patch.stop()
		self.meta_patch.stop()

	def test_mssql_connector_requires_server_and_database(self):
		connector = MssqlConnector(DummyPartner("mssql"))
		ping = connector.ping()
		self.assertFalse(ping.ok)
		self.assertEqual(ping.details["missing"], ["server", "database"])

	def test_postgres_connector_requires_host_database_user(self):
		connector = PostgresConnector(DummyPartner("postgres"))
		ping = connector.ping()
		self.assertFalse(ping.ok)
		self.assertEqual(ping.details["missing"], ["host", "database", "user"])

	def test_firebird_connector_requires_host_database_user(self):
		connector = FirebirdConnector(DummyPartner("firebird"))
		ping = connector.ping()
		self.assertFalse(ping.ok)
		self.assertEqual(ping.details["missing"], ["host", "database", "user"])

	def test_ping_reports_missing_driver(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))

		with patch.object(connector, "_load_driver_module", return_value=None):
			ping = connector.ping()

		self.assertFalse(ping.ok)
		self.assertIn("No compatible driver installed", ping.message)
		self.assertEqual(ping.details["dialect"], "postgres")

	def test_ping_reports_successful_connection(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))
		cursor = _FakeCursor(rows=[(1,)])
		connection = _FakeConnection(cursor)

		with (
			patch.object(connector, "_load_driver_module", return_value=SimpleNamespace(__name__="psycopg")),
			patch.object(connector, "_connect", return_value=connection),
		):
			ping = connector.ping()

		self.assertTrue(ping.ok)
		self.assertEqual(cursor.executed[0], ("SELECT 1", []))
		self.assertEqual(ping.details["driver"], "psycopg")

	def test_ping_reports_connection_failure(self):
		connector = FirebirdConnector(DummyPartner("firebird", host="localhost", database="sync", user="tester"))

		with (
			patch.object(connector, "_load_driver_module", return_value=SimpleNamespace(__name__="fdb")),
			patch.object(connector, "_connect", side_effect=RuntimeError("boom")),
		):
			ping = connector.ping()

		self.assertFalse(ping.ok)
		self.assertIn("connection test failed", ping.message)
		self.assertEqual(ping.details["driver"], "fdb")


class TestRelationalConnectorSql(unittest.TestCase):
	def setUp(self):
		self.meta_patch = patch(
			"sync.sync.service.connectors.frappe.get_meta",
			return_value=PARTNER_META,
		)
		self.decrypt_patch = patch(
			"sync.sync.service.connectors.get_decrypted_password",
			return_value=None,
		)
		self.meta_patch.start()
		self.decrypt_patch.start()

	def tearDown(self):
		self.decrypt_patch.stop()
		self.meta_patch.stop()

	def test_resolve_source_uses_trimmed_query_and_default_table(self):
		connector = PostgresConnector(
			DummyPartner(
				"postgres",
				host="localhost",
				database="sync",
				user="tester",
				default_table="public.sync_records",
				query=" SELECT * FROM sync_records; ",
			)
		)

		source, query = connector._resolve_source(source=None, query=None)

		self.assertEqual(source, "public.sync_records")
		self.assertEqual(query, "SELECT * FROM sync_records")

	def test_build_fetch_sql_for_table_sources(self):
		self.assertEqual(
			MssqlConnector(DummyPartner("mssql"))._build_fetch_sql(
				source="dbo.SyncTable",
				query=None,
				batch_size=25,
				offset=50,
				key_fields=["id"],
			),
			"SELECT * FROM [dbo].[SyncTable] ORDER BY [id] OFFSET 50 ROWS FETCH NEXT 25 ROWS ONLY",
		)
		self.assertEqual(
			MssqlConnector(DummyPartner("mssql"))._build_fetch_sql(
				source="01adr_Spender",
				query=None,
				batch_size=25,
				offset=50,
				key_fields=["Nr"],
			),
			"SELECT * FROM [01adr_Spender] ORDER BY [Nr] OFFSET 50 ROWS FETCH NEXT 25 ROWS ONLY",
		)
		self.assertEqual(
			PostgresConnector(DummyPartner("postgres"))._build_fetch_sql(
				source="public.sync_table",
				query=None,
				batch_size=10,
				offset=5,
				key_fields=["id"],
			),
			'SELECT * FROM "public"."sync_table" LIMIT 10 OFFSET 5',
		)
		self.assertEqual(
			FirebirdConnector(DummyPartner("firebird"))._build_fetch_sql(
				source="SYNC_TABLE",
				query=None,
				batch_size=10,
				offset=5,
				key_fields=["id"],
			),
			'SELECT * FROM "SYNC_TABLE" ROWS 6 TO 15',
		)

	def test_build_fetch_sql_for_query_sources(self):
		self.assertEqual(
			MssqlConnector(DummyPartner("mssql"))._build_fetch_sql(
				source=None,
				query="SELECT id FROM dbo.SyncTable",
				batch_size=10,
				offset=20,
				key_fields=["id"],
			),
			"SELECT * FROM (SELECT id FROM dbo.SyncTable) AS source_rows ORDER BY [id] OFFSET 20 ROWS FETCH NEXT 10 ROWS ONLY",
		)
		self.assertEqual(
			PostgresConnector(DummyPartner("postgres"))._build_fetch_sql(
				source=None,
				query="SELECT id FROM sync_table",
				batch_size=10,
				offset=20,
				key_fields=["id"],
			),
			"SELECT * FROM (SELECT id FROM sync_table) AS source_rows LIMIT 10 OFFSET 20",
		)
		self.assertEqual(
			FirebirdConnector(DummyPartner("firebird"))._build_fetch_sql(
				source=None,
				query="SELECT id FROM sync_table",
				batch_size=10,
				offset=20,
				key_fields=["id"],
			),
			"SELECT * FROM (SELECT id FROM sync_table) source_rows ROWS 21 TO 30",
		)

	def test_mssql_order_clause_falls_back_for_unsafe_or_missing_keys(self):
		connector = MssqlConnector(DummyPartner("mssql"))
		self.assertEqual(connector._mssql_order_clause([]), "ORDER BY (SELECT NULL)")
		self.assertEqual(connector._mssql_order_clause(["[broken"]), "ORDER BY (SELECT NULL)")
		self.assertEqual(connector._mssql_order_clause(["unsafe-key"]), "ORDER BY [unsafe-key]")
		self.assertEqual(connector._mssql_order_clause(["Telefon mobil"]), "ORDER BY [Telefon mobil]")

	def test_quote_compound_identifier_rejects_unsafe_values(self):
		connector = PostgresConnector(DummyPartner("postgres"))

		with self.assertRaisesRegex(ValueError, "Unsafe SQL identifier"):
			connector._quote_compound_identifier("public.sync-table")

		with self.assertRaisesRegex(ValueError, "Identifier is empty"):
			connector._quote_compound_identifier("  ")

	def test_quote_compound_identifier_accepts_mssql_numeric_bracketed_and_unicode_parts(self):
		connector = MssqlConnector(DummyPartner("mssql"))

		self.assertEqual(connector._quote_compound_identifier("01adr_Spender"), "[01adr_Spender]")
		self.assertEqual(connector._quote_compound_identifier("[01adr_Spender]"), "[01adr_Spender]")
		self.assertEqual(connector._quote_compound_identifier("dbo.[01adr_Spender]"), "[dbo].[01adr_Spender]")
		self.assertEqual(connector._quote_compound_identifier("Telefon mobil"), "[Telefon mobil]")
		self.assertEqual(connector._quote_compound_identifier("[Änderung]"), "[Änderung]")

	def test_quote_compound_identifier_rejects_malformed_mssql_brackets(self):
		connector = MssqlConnector(DummyPartner("mssql"))

		with self.assertRaisesRegex(ValueError, "Unsafe SQL identifier"):
			connector._quote_compound_identifier("[01adr_Spender")

	def test_placeholder_matches_paramstyle(self):
		self.assertEqual(MssqlConnector(DummyPartner("mssql"))._placeholder(), "?")
		self.assertEqual(PostgresConnector(DummyPartner("postgres"))._placeholder(), "%s")


class TestRelationalConnectorOperations(unittest.TestCase):
	def setUp(self):
		self.meta_patch = patch(
			"sync.sync.service.connectors.frappe.get_meta",
			return_value=PARTNER_META,
		)
		self.decrypt_patch = patch(
			"sync.sync.service.connectors.get_decrypted_password",
			return_value=None,
		)
		self.meta_patch.start()
		self.decrypt_patch.start()

	def tearDown(self):
		self.decrypt_patch.stop()
		self.meta_patch.stop()

	def test_fetch_records_requires_source_or_query(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))

		with self.assertRaisesRegex(RuntimeError, "requires source table or query"):
			connector.fetch_records()

	def test_fetch_records_returns_records_and_cursor(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))

		with patch.object(
			connector,
			"_run_select",
			return_value=[{"id": "A1"}, {"id": "A2"}],
		) as mock_select:
			result = connector.fetch_records(source="public.sync_records", batch_size=2, cursor="3", key_fields=["id"])

		self.assertEqual(result.records, [{"id": "A1"}, {"id": "A2"}])
		self.assertEqual(result.next_cursor, "5")
		mock_select.assert_called_once_with(
			'SELECT * FROM "public"."sync_records" LIMIT 2 OFFSET 3',
			[],
		)

	def test_upsert_record_rejects_query_only_targets(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))

		result = connector.upsert_record(
			record={"id": "A1"},
			key_fields=["name"],
			mapping={"name": "id"},
			query="SELECT * FROM sync_records",
		)

		self.assertFalse(result.ok)
		self.assertIn("writable table source", result.message)

	def test_upsert_record_validates_target_record_and_keys(self):
		connector = MssqlConnector(DummyPartner("mssql", server="localhost", database="sync"))

		self.assertFalse(
			connector.upsert_record(record={}, key_fields=["name"], mapping={"name": "id"}, source="dbo.SyncTable").ok
		)

		result = connector.upsert_record(
			record={"status": "open"},
			key_fields=["name"],
			mapping={"name": "id"},
			source="dbo.SyncTable",
		)
		self.assertFalse(result.ok)
		self.assertIn("missing key values", result.message)

	def test_upsert_record_dry_run_reports_changed_fields(self):
		connector = FirebirdConnector(DummyPartner("firebird", host="localhost", database="sync", user="tester"))

		result = connector.upsert_record(
			record={"id": "A1", "status": "open"},
			key_fields=["name"],
			mapping={"name": "id", "status": "status"},
			dry_run=True,
			source="SYNC_TABLE",
		)

		self.assertTrue(result.ok)
		self.assertEqual(result.changed_fields, ["id", "status"])
		self.assertEqual(result.action, "updated")
		self.assertEqual(result.resolved_key_values, {})

	def test_upsert_record_uses_sequence_strategy_and_returns_resolved_identity(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))
		cursor = _FakeCursor(rowcount=0)
		connection = _FakeConnection(cursor)

		with (
			patch.object(connector, "_connect", return_value=connection),
			patch.object(connector, "_next_sequence_value", return_value=42) as mock_sequence,
			patch.object(connector, "_load_record_by_key_values", return_value={"id": 42, "status": "open"}) as mock_load,
		):
			result = connector.upsert_record(
				record={"status": "open"},
				key_values={"id": "TEMP"},
				key_fields=[],
				mapping={},
				source="public.people",
				create_options=ConnectorCreateOptions(identity_field="id", strategy="sequence", source="people_seq"),
			)

		self.assertTrue(result.ok)
		self.assertEqual(result.action, "created")
		self.assertEqual(result.record, {"id": 42, "status": "open"})
		self.assertEqual(result.resolved_key_values, {"id": 42})
		self.assertEqual(
			cursor.executed,
			[
				('UPDATE "public"."people" SET "status" = %s WHERE "id" = %s', ["open", "TEMP"]),
				('INSERT INTO "public"."people" ("status", "id") VALUES (%s, %s)', ["open", 42]),
			],
		)
		mock_sequence.assert_called_once_with(connection, "people_seq")
		mock_load.assert_called_once_with("public.people", {"id": 42})

	def test_upsert_record_uses_scoped_max_plus_one_strategy_and_returns_resolved_identity(self):
		connector = MssqlConnector(DummyPartner("mssql", server="localhost", database="sync"))
		cursor = _FakeCursor(rowcount=0)
		connection = _FakeConnection(cursor)

		with (
			patch.object(connector, "_connect", return_value=connection),
			patch.object(connector, "_next_scoped_max_plus_one", return_value=90001) as mock_next,
			patch.object(connector, "_load_record_by_key_values", return_value={"NR": 90001, "NAME": "Alice"}) as mock_load,
		):
			result = connector.upsert_record(
				record={"NAME": "Alice"},
				key_values={"NR": "TEMP"},
				key_fields=[],
				mapping={},
				source="dbo.Persons",
				create_options=ConnectorCreateOptions(
					identity_field="NR",
					strategy="max_plus_one",
					scope_where="NR >= 1 AND NR < 90000",
				),
			)

		self.assertTrue(result.ok)
		self.assertEqual(result.action, "created")
		self.assertEqual(result.record, {"NR": 90001, "NAME": "Alice"})
		self.assertEqual(result.resolved_key_values, {"NR": 90001})
		self.assertEqual(
			cursor.executed,
			[
				('UPDATE [dbo].[Persons] SET [NAME] = ? WHERE [NR] = ?', ["Alice", "TEMP"]),
				('INSERT INTO [dbo].[Persons] ([NAME], [NR]) VALUES (?, ?)', ["Alice", 90001]),
			],
		)
		mock_next.assert_called_once_with(connection, source_name="dbo.Persons", identity_field="NR", scope_where="NR >= 1 AND NR < 90000")
		mock_load.assert_called_once_with("dbo.Persons", {"NR": 90001})

	def test_upsert_record_uses_update_path_when_rowcount_is_positive(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))
		cursor = _FakeCursor(rowcount=1)
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.upsert_record(
				record={"id": "A1", "status": "closed"},
				key_fields=["name"],
				mapping={"name": "id", "status": "status"},
				source="public.sync_records",
			)

		self.assertTrue(result.ok)
		self.assertEqual(result.message, "postgres update succeeded")
		self.assertEqual(result.changed_fields, ["status"])
		self.assertEqual(
			cursor.executed[0],
			('UPDATE "public"."sync_records" SET "status" = %s WHERE "id" = %s', ["closed", "A1"]),
		)
		self.assertEqual(connection.commit_count, 1)

	def test_upsert_record_uses_insert_path_when_update_touches_no_rows(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))
		cursor = _FakeCursor(rowcount=0)
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.upsert_record(
				record={"id": "A1", "status": "open"},
				key_fields=["name"],
				mapping={"name": "id", "status": "status"},
				source="public.sync_records",
			)

		self.assertTrue(result.ok)
		self.assertEqual(result.message, "postgres insert succeeded")
		self.assertEqual(result.action, "created")
		self.assertEqual(result.record, {"id": "A1", "status": "open"})
		self.assertEqual(result.resolved_key_values, {"id": "A1"})
		self.assertEqual(
			cursor.executed,
			[
				('UPDATE "public"."sync_records" SET "status" = %s WHERE "id" = %s', ["open", "A1"]),
				('INSERT INTO "public"."sync_records" ("id", "status") VALUES (%s, %s)', ["A1", "open"]),
			],
		)
		self.assertEqual(connection.commit_count, 1)

	def test_firebird_upsert_uppercases_identifiers_for_unquoted_objects(self):
		connector = FirebirdConnector(DummyPartner("firebird", host="localhost", database="sync", user="tester"))
		cursor = _FakeCursor(rowcount=0)
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.upsert_record(
				record={"id": "A1", "status": "open"},
				key_fields=["name"],
				mapping={"name": "id", "status": "status"},
				source="sync_table",
			)

		self.assertTrue(result.ok)
		self.assertEqual(
			cursor.executed,
			[
				('UPDATE "SYNC_TABLE" SET "STATUS" = ? WHERE "ID" = ?', ["open", "A1"]),
				('INSERT INTO "SYNC_TABLE" ("ID", "STATUS") VALUES (?, ?)', ["A1", "open"]),
			],
		)
		self.assertEqual(connection.commit_count, 1)

	def test_upsert_record_reports_sql_errors(self):
		connector = MssqlConnector(DummyPartner("mssql", server="localhost", database="sync"))
		cursor = _FakeCursor(rowcount=0, raise_on_execute=RuntimeError("db exploded"))
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.upsert_record(
				record={"id": "A1", "status": "open"},
				key_fields=["name"],
				mapping={"name": "id", "status": "status"},
				source="dbo.SyncTable",
			)

		self.assertFalse(result.ok)
		self.assertIn("upsert failed", result.message)
		self.assertEqual(connection.rollback_count, 1)

	def test_upsert_record_sequence_strategy_returns_resolved_identity(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))
		cursor = _FakeCursor(
			rowcount=0,
			rows=[(41, "open")],
			description=[("id",), ("status",)],
			fetchone_row=(41,),
		)
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.upsert_record(
				record={"status": "open"},
				key_values={},
				source="public.sync_records",
				create_options=ConnectorCreateOptions(identity_field="id", strategy="sequence", source="sync_records_id_seq"),
			)

		self.assertTrue(result.ok)
		self.assertEqual(result.action, "created")
		self.assertEqual(result.resolved_key_values, {"id": 41})
		self.assertEqual(result.record, {"id": 41, "status": "open"})
		self.assertEqual(cursor.executed[0], ("SELECT nextval(%s)", ["sync_records_id_seq"]))
		self.assertEqual(
			cursor.executed[1],
			('INSERT INTO "public"."sync_records" ("status", "id") VALUES (%s, %s)', ["open", 41]),
		)

	def test_upsert_record_scoped_max_plus_one_uses_scope_and_returns_identity(self):
		connector = MssqlConnector(DummyPartner("mssql", server="localhost", database="sync"))
		cursor = _FakeCursor(
			rowcount=0,
			rows=[(900, "open")],
			description=[("NR",), ("status",)],
			fetchone_row=(900,),
		)
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.upsert_record(
				record={"status": "open"},
				key_values={},
				source="dbo.Address",
				create_options=ConnectorCreateOptions(
					identity_field="NR",
					strategy="max_plus_one",
					scope_where="NR BETWEEN 1 AND 89999",
				),
			)

		self.assertTrue(result.ok)
		self.assertEqual(result.action, "created")
		self.assertEqual(result.resolved_key_values, {"NR": 900})
		self.assertIn("NR BETWEEN 1 AND 89999", cursor.executed[0][0])
		self.assertEqual(
			cursor.executed[1],
			('INSERT INTO [dbo].[Address] ([status], [NR]) VALUES (?, ?)', ["open", 900]),
		)

	def test_upsert_record_connector_default_returns_loaded_identity_record(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))
		cursor = _FakeCursor(rowcount=0, rows=[("AUTO-7", "open")], description=[("id",), ("status",)])
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.upsert_record(
				record={"status": "open"},
				key_values={"id": "AUTO-7"},
				source="public.sync_records",
				create_options=ConnectorCreateOptions(identity_field="id", strategy="connector_default"),
			)

		self.assertTrue(result.ok)
		self.assertEqual(result.resolved_key_values, {"id": "AUTO-7"})
		self.assertEqual(result.record, {"id": "AUTO-7", "status": "open"})

	def test_delete_record_validates_inputs_and_supports_dry_run(self):
		connector = FirebirdConnector(DummyPartner("firebird", host="localhost", database="sync", user="tester"))

		self.assertFalse(connector.delete_record(key_values={}, source="SYNC_TABLE").ok)

		result = connector.delete_record(key_values={"id": "A1"}, source="SYNC_TABLE", dry_run=True)

		self.assertTrue(result.ok)
		self.assertEqual(result.changed_fields, ["id"])

	def test_delete_record_executes_statement(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))
		cursor = _FakeCursor()
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.delete_record(key_values={"id": "A1"}, source="public.sync_records")

		self.assertTrue(result.ok)
		self.assertEqual(
			cursor.executed[0],
			('DELETE FROM "public"."sync_records" WHERE "id" = %s', ["A1"]),
		)
		self.assertEqual(connection.commit_count, 1)

	def test_delete_record_reports_sql_errors(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database="sync", user="tester"))
		cursor = _FakeCursor(raise_on_execute=RuntimeError("delete exploded"))
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.delete_record(key_values={"id": "A1"}, source="public.sync_records")

		self.assertFalse(result.ok)
		self.assertIn("delete failed", result.message)
		self.assertEqual(connection.rollback_count, 1)

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

	def test_relational_connector_requires_source_for_column_inspection(self):
		connector = PostgresConnector(DummyPartner("postgres"))

		with self.assertRaisesRegex(RuntimeError, "requires a table source"):
			connector.describe_source_columns()


class TestConnectorFactoryAndConnectMethods(unittest.TestCase):
	def setUp(self):
		self.meta_patch = patch(
			"sync.sync.service.connectors.frappe.get_meta",
			return_value=PARTNER_META,
		)
		self.decrypt_patch = patch(
			"sync.sync.service.connectors.get_decrypted_password",
			return_value=None,
		)
		self.meta_patch.start()
		self.decrypt_patch.start()

	def tearDown(self):
		self.decrypt_patch.stop()
		self.meta_patch.stop()

	def test_mssql_connect_builds_connection_string(self):
		connector = MssqlConnector(
			DummyPartner(
				"mssql",
				server="db.internal",
				database="sync",
				user="tester",
				password="secret",
				port=1433,
				odbc_driver="ODBC Driver 18 for SQL Server",
				encrypt=1,
				trust_server_certificate=0,
				connect_timeout=7,
			)
		)
		driver = SimpleNamespace(connect=lambda conn_string, timeout=0: (conn_string, timeout))

		with patch.object(connector, "_load_driver_module", return_value=driver):
			conn_string, timeout = connector._connect()

		self.assertIn("SERVER=db.internal,1433", conn_string)
		self.assertIn("DATABASE=sync", conn_string)
		self.assertIn("UID=tester", conn_string)
		self.assertIn("PWD=secret", conn_string)
		self.assertIn("Encrypt=yes", conn_string)
		self.assertIn("TrustServerCertificate=no", conn_string)
		self.assertEqual(timeout, 7)

	def test_postgres_connect_builds_kwargs(self):
		connector = PostgresConnector(
			DummyPartner(
				"postgres",
				host="db.internal",
				database="sync",
				user="tester",
				password="secret",
				port=5440,
				connect_timeout=9,
				sslmode="disable",
			)
		)
		calls = []
		driver = SimpleNamespace(connect=lambda **kwargs: calls.append(kwargs) or kwargs)

		with patch.object(connector, "_load_driver_module", return_value=driver):
			result = connector._connect()

		self.assertEqual(result["dbname"], "sync")
		self.assertEqual(result["port"], 5440)
		self.assertEqual(result["sslmode"], "disable")
		self.assertEqual(calls[0]["connect_timeout"], 9)

	def test_firebird_connect_builds_kwargs(self):
		connector = FirebirdConnector(
			DummyPartner(
				"firebird",
				host="db.internal",
				database="/var/lib/firebird/data/sync.fdb",
				user="SYSDBA",
				password="masterkey",
				port=3051,
				charset="UTF8",
			)
		)
		calls = []
		driver = SimpleNamespace(__name__="fdb", connect=lambda **kwargs: calls.append(kwargs) or kwargs)

		with patch.object(connector, "_load_driver_module", return_value=driver):
			result = connector._connect()

		self.assertEqual(result["host"], "db.internal")
		self.assertEqual(result["port"], 3051)
		self.assertEqual(result["database"], "/var/lib/firebird/data/sync.fdb")
		self.assertEqual(result["user"], "SYSDBA")
		self.assertEqual(result["password"], "masterkey")
		self.assertEqual(result["charset"], "UTF8")
		self.assertEqual(calls[0], result)

	def test_connect_methods_raise_when_drivers_missing(self):
		for connector, expected in (
			(MssqlConnector(DummyPartner("mssql", server="db", database="sync")), "pyodbc is not installed"),
			(PostgresConnector(DummyPartner("postgres", host="db", database="sync", user="tester")), "Neither psycopg nor psycopg2 is installed"),
			(FirebirdConnector(DummyPartner("firebird", host="db", database="sync", user="tester")), "fdb is not installed"),
		):
			with (
				self.subTest(expected=expected),
				patch.object(connector, "_load_driver_module", return_value=None),
				self.assertRaisesRegex(RuntimeError, expected),
			):
				connector._connect()

	def test_get_partner_type_and_connector_selection_cover_aliases_and_unknown(self):
		partner = DummyPartner("postgres", sync_partner_type="ignored")
		self.assertEqual(type(get_connector_for_partner(partner)).__name__, "PostgresConnector")

		lookup_partner = DummyPartner("ignored", sync_partner_type="ExternalType")
		with (
			patch(
				"sync.sync.service.connectors.frappe",
				new=SimpleNamespace(
					db=SimpleNamespace(exists=lambda *args, **kwargs: True),
					get_doc=lambda *args, **kwargs: SimpleNamespace(get=lambda key, default=None: "firebird"),
				),
			),
		):
			self.assertEqual(get_partner_type(lookup_partner), "firebird")

		self.assertEqual(get_partner_type(DummyPartner("", partner_type_name="custom")), "custom")
		with self.assertRaises(TypeError):
			get_connector_for_partner(DummyPartner("unknown"))

	def test_parse_config_text_and_scalar_helpers(self):
		self.assertEqual(_parse_config_text('{"sslmode": "disable"}'), {"sslmode": "disable"})
		self.assertEqual(_parse_config_text("sslmode=disable\n#comment\nport=5432"), {"sslmode": "disable", "port": "5432"})
		self.assertEqual(_parse_config_text(None), {})
		self.assertTrue(_to_bool("yes"))
		self.assertFalse(_to_bool(None))
		self.assertEqual(_to_non_negative_int("-5"), 0)
		self.assertEqual(_to_non_negative_int("7"), 7)
		self.assertEqual(_strip_trailing_semicolon("select 1;"), "select 1")


@unittest.skipUnless(os.environ.get("SYNC_TEST_POSTGRES_HOST"), "Postgres integration env not configured")
class TestPostgresConnectorIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.meta_patch = patch(
			"sync.sync.service.connectors.frappe.get_meta",
			return_value=PARTNER_META,
		)
		cls.decrypt_patch = patch(
			"sync.sync.service.connectors.get_decrypted_password",
			return_value=None,
		)
		cls.meta_patch.start()
		cls.decrypt_patch.start()
		cls.connector = PostgresConnector(
			DummyPartner(
				"postgres",
				host=os.environ["SYNC_TEST_POSTGRES_HOST"],
				port=os.environ.get("SYNC_TEST_POSTGRES_PORT", "5432"),
				database=os.environ["SYNC_TEST_POSTGRES_DB"],
				user=os.environ["SYNC_TEST_POSTGRES_USER"],
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

	@classmethod
	def tearDownClass(cls):
		cls.decrypt_patch.stop()
		cls.meta_patch.stop()
		super().tearDownClass()

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

		page = self.connector.fetch_records(source=self.table_name, batch_size=1, cursor="0", key_fields=["id"])
		self.assertEqual(len(page.records), 1)
		self.assertEqual(page.records[0]["id"], "A1")
		self.assertEqual(page.records[0]["status"], "closed")
		self.assertEqual(page.next_cursor, "1")

		query_page = self.connector.fetch_records(
			query=f"SELECT id, status, updated_at FROM {self.table_name}",
			batch_size=5,
			cursor="0",
			key_fields=["id"],
		)
		self.assertEqual(len(query_page.records), 1)
		self.assertEqual(query_page.records[0]["id"], "A1")

		delete = self.connector.delete_record(key_values={"id": "A1"}, source=self.table_name)
		self.assertTrue(delete.ok)
		self.assertIn("delete succeeded", delete.message)

		final_page = self.connector.fetch_records(source=self.table_name, batch_size=5, cursor="0", key_fields=["id"])
		self.assertEqual(final_page.records, [])


class _FakeCursor:
	def __init__(self, description=None, rows=None, rowcount=0, raise_on_execute: Exception | None = None, fetchone_row=None):
		self.description = description or []
		self.rows = rows or []
		self.rowcount = rowcount
		self.raise_on_execute = raise_on_execute
		self.fetchone_row = fetchone_row
		self.executed = []

	def execute(self, sql, params=None):
		if self.raise_on_execute:
			raise self.raise_on_execute
		self.executed.append((sql, params if params is not None else []))

	def fetchall(self):
		return list(self.rows)

	def fetchone(self):
		return self.fetchone_row

	def close(self):
		return None


class _FakeConnection:
	def __init__(self, cursor):
		self._cursor = cursor
		self.commit_count = 0
		self.rollback_count = 0

	def cursor(self):
		return self._cursor

	def commit(self):
		self.commit_count += 1

	def rollback(self):
		self.rollback_count += 1

	def close(self):
		return None
