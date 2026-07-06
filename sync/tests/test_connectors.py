from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import frappe

from sync.sync.service import connectors as connector_module
from sync.sync.service.connectors import (
	BasePartnerConnector,
	ConnectorPingResult,
	FirebirdConnector,
	ConnectorCreateOptions,
	MssqlConnector,
	PartnerSourceTable,
	PostgresConnector,
	get_connector_for_partner,
	get_partner_type,
	_parse_config_text,
	_decode_keyset_cursor,
	_encode_keyset_cursor,
	_validate_scope_where,
	_strip_trailing_semicolon,
	_to_bool,
	_to_non_negative_int,
)


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
		_field("trust_server_certificate"),
		_field("default_table"),
		_field("table_name"),
		_field("source"),
		_field("sslmode"),
		_field("connection_options"),
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


class DummyBaseConnector(BasePartnerConnector):
	def ping(self) -> ConnectorPingResult:
		return ConnectorPingResult(ok=True, message="ok", details={})


class TestBasePartnerConnector(unittest.TestCase):
	def test_default_methods_report_unsupported_writes_and_empty_reads(self):
		with patch("sync.sync.service.connectors.frappe.get_meta", return_value=PARTNER_META):
			connector = DummyBaseConnector(DummyPartner("base"))

		self.assertEqual(connector.fetch_records().records, [])
		self.assertTrue(connector.upsert_record(record={"id": "A1"}, key_fields=["id"], mapping={}, dry_run=True).ok)
		self.assertFalse(connector.upsert_record(record={"id": "A1"}, key_fields=["id"], mapping={}, dry_run=False).ok)
		self.assertTrue(connector.delete_record(key_values={"id": "A1"}, dry_run=True).ok)
		self.assertFalse(connector.delete_record(key_values={"id": "A1"}, dry_run=False).ok)
		with self.assertRaisesRegex(RuntimeError, "does not support source-column inspection"):
			connector.describe_source_columns(source="table")
		with self.assertRaisesRegex(RuntimeError, "does not support identifier quoting"):
			connector.quote_identifier("table")
		with self.assertRaisesRegex(RuntimeError, "does not support source-table inspection"):
			connector.list_source_tables()


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

	def test_extract_partner_config_loads_canonical_fields_json_password_and_bools(self):
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
			connection_options='{"default_table": "public.sync_records", "sslmode": "disable"}',
		)

		connector = PostgresConnector(partner)

		self.assertEqual(connector.config["host"], "db.internal")
		self.assertEqual(connector.config["database_name"], "sync_db")
		self.assertEqual(connector.config["username"], "sync_user")
		self.assertEqual(connector.config["default_table"], "public.sync_records")
		self.assertEqual(connector.config["sslmode"], "disable")
		self.assertEqual(connector.config["password"], "secret-password")
		self.assertEqual(connector.config["port"], 5439)
		self.assertEqual(connector.config["connect_timeout"], 12)
		self.assertFalse(connector.config["encrypt"])
		self.assertTrue(connector.config["trust_server_certificate"])

	def test_test_connection_wraps_ping_result(self):
		connector = MssqlConnector(DummyPartner("mssql", host="localhost", database_name="sync"))

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

	def test_mssql_connector_requires_host_and_database_name(self):
		connector = MssqlConnector(DummyPartner("mssql"))
		ping = connector.ping()
		self.assertFalse(ping.ok)
		self.assertEqual(ping.details["missing"], ["host", "database_name"])

	def test_postgres_connector_requires_host_database_name_username(self):
		connector = PostgresConnector(DummyPartner("postgres"))
		ping = connector.ping()
		self.assertFalse(ping.ok)
		self.assertEqual(ping.details["missing"], ["host", "database_name", "username"])

	def test_firebird_connector_requires_host_database_name_username(self):
		connector = FirebirdConnector(DummyPartner("firebird"))
		ping = connector.ping()
		self.assertFalse(ping.ok)
		self.assertEqual(ping.details["missing"], ["host", "database_name", "username"])

	def test_ping_reports_missing_driver(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))

		with patch.object(connector, "_load_driver_module", return_value=None):
			ping = connector.ping()

		self.assertFalse(ping.ok)
		self.assertIn("No compatible driver installed", ping.message)
		self.assertEqual(ping.details["dialect"], "postgres")

	def test_ping_reports_successful_connection(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))
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
		connector = FirebirdConnector(DummyPartner("firebird", host="localhost", database_name="sync", username="tester"))

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

	def test_resolve_source_uses_explicit_query_and_default_table(self):
		connector = PostgresConnector(
			DummyPartner(
				"postgres",
				host="localhost",
				database_name="sync",
				username="tester",
				default_table="public.sync_records",
			)
		)

		source, query = connector._resolve_source(source=None, query=" SELECT * FROM sync_records; ")

		self.assertEqual(source, "public.sync_records")
		self.assertEqual(query, "SELECT * FROM sync_records")

	def test_driver_candidates_use_config_type_metadata_and_import_fallback(self):
		config_connector = PostgresConnector(DummyPartner("postgres", connection_options='{"driver_module": "custom.psycopg"}'))
		self.assertEqual(config_connector._get_driver_candidates(), ("custom.psycopg", "psycopg", "psycopg2"))

		type_connector = PostgresConnector(DummyPartner("Custom Type"))
		type_doc = SimpleNamespace(get=lambda key, default=None: "metadata.driver" if key == "db_api_module" else default)
		with (
			patch.dict(connector_module.frappe.__dict__, {"db": SimpleNamespace(exists=lambda *args, **kwargs: True)}),
			patch("sync.sync.service.connectors.frappe.get_doc", return_value=type_doc),
		):
			self.assertEqual(type_connector._get_driver_candidates(), ("metadata.driver", "psycopg", "psycopg2"))

		loaded_driver = SimpleNamespace(__name__="psycopg2")
		with patch(
			"sync.sync.service.connectors.importlib.import_module",
			side_effect=[ImportError("missing"), loaded_driver],
		):
			self.assertIs(type_connector._load_driver_module(), loaded_driver)

		with patch("sync.sync.service.connectors.importlib.import_module", side_effect=ImportError("missing")):
			self.assertIsNone(type_connector._load_driver_module())

	def test_build_fetch_sql_for_table_sources(self):
		self.assertEqual(
			MssqlConnector(DummyPartner("mssql"))._build_fetch_sql(
				source="dbo.SyncTable",
				query=None,
				batch_size=25,
				key_fields=["id"],
				where_clause=None,
			),
			"SELECT * FROM [dbo].[SyncTable] ORDER BY [id] OFFSET 0 ROWS FETCH NEXT 25 ROWS ONLY",
		)
		self.assertEqual(
			MssqlConnector(DummyPartner("mssql"))._build_fetch_sql(
				source="01adr_Spender",
				query=None,
				batch_size=25,
				key_fields=["Nr"],
				where_clause="[Nr] > ?",
			),
			"SELECT * FROM [01adr_Spender] WHERE [Nr] > ? ORDER BY [Nr] OFFSET 0 ROWS FETCH NEXT 25 ROWS ONLY",
		)
		self.assertEqual(
			PostgresConnector(DummyPartner("postgres"))._build_fetch_sql(
				source="public.sync_table",
				query=None,
				batch_size=10,
				key_fields=["id"],
				where_clause=None,
			),
			'SELECT * FROM "public"."sync_table" ORDER BY "id" LIMIT 10',
		)
		self.assertEqual(
			FirebirdConnector(DummyPartner("firebird"))._build_fetch_sql(
				source="SYNC_TABLE",
				query=None,
				batch_size=10,
				key_fields=["id"],
				where_clause=None,
			),
			'SELECT * FROM "SYNC_TABLE" ORDER BY "ID" ROWS 1 TO 10',
		)

	def test_build_fetch_sql_for_query_sources(self):
		self.assertEqual(
			MssqlConnector(DummyPartner("mssql"))._build_fetch_sql(
				source=None,
				query="SELECT id FROM dbo.SyncTable",
				batch_size=10,
				key_fields=["id"],
				where_clause=None,
			),
			"SELECT * FROM (SELECT id FROM dbo.SyncTable) AS source_rows ORDER BY [id] OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY",
		)
		self.assertEqual(
			MssqlConnector(DummyPartner("mssql"))._build_fetch_sql(
				source=None,
				query="WITH rows AS (SELECT id FROM dbo.SyncTable) SELECT id FROM rows",
				batch_size=10,
				key_fields=["id"],
				where_clause="[id] > ?",
			),
			"WITH rows AS (SELECT id FROM dbo.SyncTable), source_rows AS (SELECT id FROM rows) "
			"SELECT * FROM source_rows WHERE [id] > ? ORDER BY [id] OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY",
		)
		self.assertEqual(
			PostgresConnector(DummyPartner("postgres"))._build_fetch_sql(
				source=None,
				query="SELECT id FROM sync_table",
				batch_size=10,
				key_fields=["id"],
				where_clause='"id" > %s',
			),
			'SELECT * FROM (SELECT id FROM sync_table) AS source_rows WHERE "id" > %s ORDER BY "id" LIMIT 10',
		)
		self.assertEqual(
			FirebirdConnector(DummyPartner("firebird"))._build_fetch_sql(
				source=None,
				query="SELECT id FROM sync_table",
				batch_size=10,
				key_fields=["id"],
				where_clause=None,
			),
			'SELECT * FROM (SELECT id FROM sync_table) source_rows ORDER BY "ID" ROWS 1 TO 10',
		)

	def test_mssql_order_clause_requires_stable_keys(self):
		connector = MssqlConnector(DummyPartner("mssql"))
		with self.assertRaisesRegex(RuntimeError, "stable key fields"):
			connector._mssql_order_clause([])
		with self.assertRaisesRegex(RuntimeError, "stable key fields"):
			connector._mssql_order_clause(["[broken"])
		self.assertEqual(connector._mssql_order_clause(["unsafe-key"]), "ORDER BY [unsafe-key]")
		self.assertEqual(connector._mssql_order_clause(["Telefon mobil"]), "ORDER BY [Telefon mobil]")

	def test_quote_compound_identifier_rejects_postgres_unsafe_values(self):
		connector = PostgresConnector(DummyPartner("postgres"))

		for identifier in ('bad"name', "bad\x00name", "public..table"):
			with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, "Unsafe SQL identifier"):
				connector._quote_compound_identifier(identifier)

		with self.assertRaisesRegex(ValueError, "Identifier is empty"):
			connector._quote_compound_identifier("  ")

	def test_quote_compound_identifier_accepts_postgres_unicode_parts(self):
		connector = PostgresConnector(DummyPartner("postgres"))

		self.assertEqual(connector._quote_compound_identifier("01adr_Spender"), '"01adr_Spender"')
		self.assertEqual(connector._quote_compound_identifier("Telefon mobil"), '"Telefon mobil"')
		self.assertEqual(connector._quote_compound_identifier("änderung"), '"änderung"')
		self.assertEqual(connector._quote_compound_identifier("public.änderung"), '"public"."änderung"')

	def test_quote_compound_identifier_accepts_firebird_regular_parts_as_uppercase(self):
		connector = FirebirdConnector(DummyPartner("firebird"))

		self.assertEqual(connector._quote_compound_identifier("sync_table"), '"SYNC_TABLE"')
		self.assertEqual(connector._quote_compound_identifier("SyncTable"), '"SYNCTABLE"')
		self.assertEqual(connector._quote_compound_identifier("personen.sync_table"), '"PERSONEN"."SYNC_TABLE"')

	def test_quote_compound_identifier_accepts_firebird_delimited_parts(self):
		connector = FirebirdConnector(DummyPartner("firebird"))

		self.assertEqual(connector._quote_compound_identifier("01adr_Spender"), '"01adr_Spender"')
		self.assertEqual(connector._quote_compound_identifier("Telefon mobil"), '"Telefon mobil"')
		self.assertEqual(connector._quote_compound_identifier("änderung"), '"änderung"')

	def test_quote_compound_identifier_rejects_firebird_unsafe_values(self):
		connector = FirebirdConnector(DummyPartner("firebird"))

		for identifier in ('bad"name', "bad\x00name", "public..table", "a" * 32, "änderung" * 6):
			with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, "Unsafe SQL identifier"):
				connector._quote_compound_identifier(identifier)

		with self.assertRaisesRegex(ValueError, "Identifier is empty"):
			connector._quote_compound_identifier("  ")

	def test_quote_compound_identifier_accepts_mssql_numeric_bracketed_and_unicode_parts(self):
		connector = MssqlConnector(DummyPartner("mssql"))

		self.assertEqual(connector._quote_compound_identifier("01adr_Spender"), "[01adr_Spender]")
		self.assertEqual(connector._quote_compound_identifier("[01adr_Spender]"), "[01adr_Spender]")
		self.assertEqual(connector._quote_compound_identifier("dbo.[01adr_Spender]"), "[dbo].[01adr_Spender]")
		self.assertEqual(connector._quote_compound_identifier("Telefon mobil"), "[Telefon mobil]")
		self.assertEqual(connector._quote_compound_identifier("[Änderung]"), "[Änderung]")
		self.assertEqual(connector._quote_compound_identifier("[dbo].[a]]b]"), "[dbo].[a]]b]")

	def test_quote_compound_identifier_rejects_malformed_mssql_brackets(self):
		connector = MssqlConnector(DummyPartner("mssql"))

		for identifier in ("[01adr_Spender", "dbo[SyncTable]", "dbo]", "[]"):
			with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, "Unsafe SQL identifier|Identifier is empty"):
				connector._quote_compound_identifier(identifier)

	def test_public_quote_identifier_delegates_to_dialect_quoting(self):
		self.assertEqual(MssqlConnector(DummyPartner("mssql")).quote_identifier("dbo.SyncTable"), "[dbo].[SyncTable]")
		self.assertEqual(PostgresConnector(DummyPartner("postgres")).quote_identifier("public.sync_table"), '"public"."sync_table"')
		self.assertEqual(FirebirdConnector(DummyPartner("firebird")).quote_identifier("sync_table"), '"SYNC_TABLE"')

	def test_list_source_tables_normalizes_mssql_metadata(self):
		connector = MssqlConnector(DummyPartner("mssql"))

		with patch.object(
			connector,
			"_run_select",
			return_value=[{"schema": "dbo", "name": "SyncTable"}, {"schema": "audit", "name": "Sync Log"}],
		) as mock_select:
			tables = connector.list_source_tables()

		self.assertEqual(mock_select.call_args.args[1], [])
		self.assertIn("sys.tables", mock_select.call_args.args[0])
		self.assertEqual(
			tables,
			[
				PartnerSourceTable(schema="dbo", name="SyncTable", full_name="dbo.SyncTable", quoted_name="[dbo].[SyncTable]"),
				PartnerSourceTable(schema="audit", name="Sync Log", full_name="audit.Sync Log", quoted_name="[audit].[Sync Log]"),
			],
		)

	def test_list_source_tables_normalizes_postgres_metadata(self):
		connector = PostgresConnector(DummyPartner("postgres"))

		with patch.object(connector, "_run_select", return_value=[{"table_schema": "public", "table_name": "sync_table"}]) as mock_select:
			tables = connector.list_source_tables()

		self.assertIn("information_schema.tables", mock_select.call_args.args[0])
		self.assertEqual(
			tables,
			[PartnerSourceTable(schema="public", name="sync_table", full_name="public.sync_table", quoted_name='"public"."sync_table"')],
		)

	def test_list_source_tables_normalizes_firebird_metadata(self):
		connector = FirebirdConnector(DummyPartner("firebird"))

		with patch.object(connector, "_run_select", return_value=[{"NAME": "SYNC_TABLE"}]) as mock_select:
			tables = connector.list_source_tables()

		self.assertIn("RDB$RELATIONS", mock_select.call_args.args[0])
		self.assertEqual(
			tables,
			[PartnerSourceTable(schema=None, name="SYNC_TABLE", full_name="SYNC_TABLE", quoted_name='"SYNC_TABLE"')],
		)

	def test_cursor_helpers_reject_invalid_values(self):
		self.assertIsNone(_encode_keyset_cursor({}, ["id"]))
		self.assertIsNone(_encode_keyset_cursor({"id": ""}, ["id"]))
		self.assertIsNone(_decode_keyset_cursor(None))
		with self.assertRaisesRegex(ValueError, "Invalid pagination cursor"):
			_decode_keyset_cursor("bm90LWpzb24=")
		with self.assertRaisesRegex(ValueError, "Invalid pagination cursor"):
			_decode_keyset_cursor("eyJhIjoxfQ==")

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
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))

		with self.assertRaisesRegex(RuntimeError, "requires source table or query"):
			connector.fetch_records()

	def test_fetch_records_returns_records_and_cursor(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))

		with patch.object(
			connector,
			"_run_select",
			return_value=[{"id": "A1"}, {"id": "A2"}, {"id": "A3"}],
		) as mock_select:
			result = connector.fetch_records(source="public.sync_records", batch_size=2, key_fields=["id"])

		self.assertEqual(result.records, [{"id": "A1"}, {"id": "A2"}])
		self.assertEqual(result.next_cursor, _encode_keyset_cursor({"id": "A2"}, ["id"]))
		mock_select.assert_called_once_with(
			'SELECT * FROM "public"."sync_records" ORDER BY "id" LIMIT 3',
			[],
		)

	def test_fetch_records_returns_no_cursor_without_lookahead_row(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))

		with patch.object(connector, "_run_select", return_value=[{"id": "A1"}, {"id": "A2"}]):
			result = connector.fetch_records(source="public.sync_records", batch_size=2, key_fields=["id"])

		self.assertEqual(result.records, [{"id": "A1"}, {"id": "A2"}])
		self.assertIsNone(result.next_cursor)

	def test_fetch_records_rejects_duplicate_page_boundary_keys(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))

		with patch.object(
			connector,
			"_run_select",
			return_value=[{"id": "A1"}, {"id": "A1"}],
		):
			with self.assertRaisesRegex(RuntimeError, "requires unique key fields"):
				connector.fetch_records(source="public.sync_records", batch_size=1, key_fields=["id"])

	def test_fetch_records_rejects_blank_cursor_key_when_more_rows_exist(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))

		with patch.object(
			connector,
			"_run_select",
			return_value=[{"id": ""}, {"id": "A2"}],
		):
			with self.assertRaisesRegex(RuntimeError, "requires non-empty values"):
				connector.fetch_records(source="public.sync_records", batch_size=1, key_fields=["id"])

	def test_fetch_records_allows_duplicate_match_key_with_unique_identity_key(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))

		with patch.object(
			connector,
			"_run_select",
			return_value=[{"group_id": "A", "row_id": 1}, {"group_id": "A", "row_id": 2}],
		):
			result = connector.fetch_records(source="public.sync_records", batch_size=1, key_fields=["group_id", "row_id"])

		self.assertEqual(result.records, [{"group_id": "A", "row_id": 1}])
		self.assertEqual(result.next_cursor, _encode_keyset_cursor({"group_id": "A", "row_id": 1}, ["group_id", "row_id"]))

	def test_fetch_records_builds_cursor_from_firebird_uppercase_result_keys(self):
		connector = FirebirdConnector(DummyPartner("firebird", host="localhost", database_name="sync", username="tester"))

		with patch.object(connector, "_run_select", return_value=[{"ID": "A1"}, {"ID": "A2"}]):
			result = connector.fetch_records(source="SYNC_RECORDS", batch_size=1, key_fields=["id"])

		self.assertEqual(result.records, [{"ID": "A1"}])
		self.assertEqual(result.next_cursor, _encode_keyset_cursor({"id": "A1"}, ["id"]))

	def test_upsert_record_rejects_query_only_targets(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))

		result = connector.upsert_record(
			record={"id": "A1"},
			key_fields=["name"],
			mapping={"name": "id"},
			query="SELECT * FROM sync_records",
		)

		self.assertFalse(result.ok)
		self.assertIn("writable table source", result.message)

	def test_upsert_record_validates_target_record_and_keys(self):
		connector = MssqlConnector(DummyPartner("mssql", host="localhost", database_name="sync"))

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
		connector = FirebirdConnector(DummyPartner("firebird", host="localhost", database_name="sync", username="tester"))

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
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))
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
		connector = MssqlConnector(DummyPartner("mssql", host="localhost", database_name="sync"))
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
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))
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

	def test_upsert_record_accepts_postgres_unicode_columns(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))
		cursor = _FakeCursor(rowcount=1)
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			result = connector.upsert_record(
				record={"id": "A1", "änderung": "2026-05-21 16:54:02"},
				key_fields=["name"],
				mapping={"name": "id", "modified": "änderung"},
				source="public.personen",
			)

		self.assertTrue(result.ok)
		self.assertEqual(result.message, "postgres update succeeded")
		self.assertEqual(
			cursor.executed[0],
			('UPDATE "public"."personen" SET "änderung" = %s WHERE "id" = %s', ["2026-05-21 16:54:02", "A1"]),
		)

	def test_upsert_record_uses_insert_path_when_update_touches_no_rows(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))
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
		connector = FirebirdConnector(DummyPartner("firebird", host="localhost", database_name="sync", username="tester"))
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
		connector = MssqlConnector(DummyPartner("mssql", host="localhost", database_name="sync"))
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
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))
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
		connector = MssqlConnector(DummyPartner("mssql", host="localhost", database_name="sync"))
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
		self.assertIn("[NR] BETWEEN ? AND ?", cursor.executed[0][0])
		self.assertEqual(cursor.executed[0][1], [1, 89999])
		self.assertEqual(
			cursor.executed[1],
			('INSERT INTO [dbo].[Address] ([status], [NR]) VALUES (?, ?)', ["open", 900]),
		)

	def test_scope_where_rejects_unsafe_predicates(self):
		connector = PostgresConnector(DummyPartner("postgres"))
		for predicate in (
			"1=1 OR id IS NOT NULL",
			"id = nextval('seq')",
			"id IN (SELECT id FROM other)",
			"id = 1; DROP TABLE x",
			"id = 1 -- comment",
		):
			with self.subTest(predicate=predicate), self.assertRaisesRegex(ValueError, "Unsafe scope predicate"):
				connector._build_scope_where(predicate)

	def test_scope_where_accepts_supported_predicate_forms(self):
		connector = PostgresConnector(DummyPartner("postgres"))

		sql, params = connector._build_scope_where(
			"(id IS NOT NULL) AND status IN ('open', 'won''t fix') AND amount >= -1.5 AND name LIKE 'A%' AND deleted_at IS NULL"
		)

		self.assertEqual(
			sql,
			'("id" IS NOT NULL) AND "status" IN (%s, %s) AND "amount" >= %s AND "name" LIKE %s AND "deleted_at" IS NULL',
		)
		self.assertEqual(params, ["open", "won't fix", -1.5, "A%"])
		self.assertEqual(_validate_scope_where("id IS NULL"), "id IS NULL")

	def test_upsert_record_connector_default_returns_loaded_identity_record(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))
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
		connector = FirebirdConnector(DummyPartner("firebird", host="localhost", database_name="sync", username="tester"))

		self.assertFalse(connector.delete_record(key_values={}, source="SYNC_TABLE").ok)

		result = connector.delete_record(key_values={"id": "A1"}, source="SYNC_TABLE", dry_run=True)

		self.assertTrue(result.ok)
		self.assertEqual(result.changed_fields, ["id"])

	def test_delete_record_executes_statement(self):
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))
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
		connector = PostgresConnector(DummyPartner("postgres", host="localhost", database_name="sync", username="tester"))
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

	def test_relational_connector_describes_query_columns(self):
		connector = PostgresConnector(DummyPartner("postgres"))
		cursor = _FakeCursor(description=[("external_id",), ("status_label",)])
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			columns = connector.describe_source_columns(query="select id as external_id, status as status_label from sync_table")

		self.assertEqual(columns, ["external_id", "status_label"])
		self.assertEqual(
			cursor.executed[0][0],
			"SELECT * FROM (select id as external_id, status as status_label from sync_table) AS source_rows WHERE 1 = 0",
		)

	def test_relational_connector_builds_firebird_query_column_inspection_sql(self):
		connector = FirebirdConnector(DummyPartner("firebird"))

		self.assertEqual(
			connector._build_describe_source_columns_sql(source="SYNC_TABLE", query="select id from sync_table"),
			"SELECT * FROM (select id from sync_table) source_rows WHERE 1 = 0",
		)

	def test_relational_connector_prefers_query_for_column_inspection_when_source_is_also_present(self):
		connector = PostgresConnector(DummyPartner("postgres"))
		cursor = _FakeCursor(description=[("query_id",), ("computed_status",)])
		connection = _FakeConnection(cursor)

		with patch.object(connector, "_connect", return_value=connection):
			columns = connector.describe_source_columns(source="public.sync_table", query="select id as query_id, 1 as computed_status from sync_table")

		self.assertEqual(columns, ["query_id", "computed_status"])
		self.assertIn("query_id", cursor.executed[0][0])
		self.assertNotIn('"public"."sync_table"', cursor.executed[0][0])

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
				host="db.internal",
				database_name="sync",
				username="tester",
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
				database_name="sync",
				username="tester",
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
				database_name="/var/lib/firebird/data/sync.fdb",
				username="SYSDBA",
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
			(MssqlConnector(DummyPartner("mssql", host="db", database_name="sync")), "pyodbc is not installed"),
			(PostgresConnector(DummyPartner("postgres", host="db", database_name="sync", username="tester")), "Neither psycopg nor psycopg2 is installed"),
			(FirebirdConnector(DummyPartner("firebird", host="db", database_name="sync", username="tester")), "fdb is not installed"),
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
		with self.assertRaises(frappe.ValidationError):
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
