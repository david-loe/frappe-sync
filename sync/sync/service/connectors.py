from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import importlib
import json
import re
from typing import Any

import frappe
from frappe.utils import cint
from frappe.utils.password import get_decrypted_password

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
JSON_CONFIG_FIELDS = {
	"connection_config",
	"config_json",
	"settings_json",
	"options_json",
	"connection_options",
}


@dataclass(slots=True)
class ConnectorPingResult:
	ok: bool
	message: str
	details: dict[str, Any]


@dataclass(slots=True)
class ConnectorFetchResult:
	records: list[dict[str, Any]]
	next_cursor: str | None = None


@dataclass(slots=True)
class ConnectorWriteResult:
	ok: bool
	message: str = ""
	changed_fields: list[str] | None = None


class BasePartnerConnector(ABC):
	partner_type = "base"

	def __init__(self, partner_doc: Any):
		self.partner_doc = partner_doc
		self.config = self._extract_partner_config(partner_doc)

	@staticmethod
	def _extract_partner_config(partner_doc: Any) -> dict[str, Any]:
		meta = frappe.get_meta(partner_doc.doctype)
		system_fields = {
			"name",
			"owner",
			"creation",
			"modified",
			"modified_by",
			"docstatus",
			"idx",
			"_user_tags",
			"_comments",
			"_assign",
			"_liked_by",
		}
		config: dict[str, Any] = {}

		for field in meta.fields:
			if field.fieldname in system_fields:
				continue
			value = partner_doc.get(field.fieldname)
			if value in (None, ""):
				continue
			if field.fieldname in JSON_CONFIG_FIELDS:
				parsed = _parse_config_text(value)
				if parsed:
					config.update(parsed)
					continue
			config[field.fieldname] = value

		if partner_doc.get("name"):
			with suppress(Exception):
				decrypted_password = get_decrypted_password(
					partner_doc.doctype,
					partner_doc.name,
					"password",
					raise_exception=False,
				)
				if decrypted_password:
					config["password"] = decrypted_password

		alias_map = {
			"server": config.get("server") or config.get("host"),
			"host": config.get("host") or config.get("server"),
			"database": config.get("database") or config.get("database_name"),
			"database_name": config.get("database_name") or config.get("database"),
			"user": config.get("user") or config.get("username"),
			"username": config.get("username") or config.get("user"),
		}
		for key, value in alias_map.items():
			if value not in (None, ""):
				config[key] = value

		for int_key in ("port", "connect_timeout", "query_timeout"):
			if int_key in config and str(config[int_key]).strip():
				with suppress(Exception):
					config[int_key] = cint(config[int_key])

		if "trust_server_certificate" in config:
			config["trust_server_certificate"] = _to_bool(config["trust_server_certificate"])
		if "encrypt" in config:
			config["encrypt"] = _to_bool(config["encrypt"])

		return config

	@abstractmethod
	def ping(self) -> ConnectorPingResult:
		pass

	def test_connection(self) -> dict[str, Any]:
		result = self.ping()
		return {
			"status": "ok" if result.ok else "error",
			"ok": result.ok,
			"message": result.message,
			"details": result.details,
		}

	def fetch_records(
		self,
		*,
		source: str | None = None,
		query: str | None = None,
		batch_size: int = 100,
		cursor: str | None = None,
		key_fields: list[str] | None = None,
	) -> ConnectorFetchResult:
		return ConnectorFetchResult(records=[], next_cursor=None)

	def upsert_record(
		self,
		*,
		record: dict[str, Any],
		key_fields: list[str],
		mapping: dict[str, str],
		dry_run: bool = False,
		source: str | None = None,
		query: str | None = None,
	) -> ConnectorWriteResult:
		if dry_run:
			return ConnectorWriteResult(ok=True, message="dry_run")
		return ConnectorWriteResult(ok=False, message="Connector does not support write operations")

	def delete_record(
		self,
		*,
		key_values: dict[str, Any],
		dry_run: bool = False,
		source: str | None = None,
	) -> ConnectorWriteResult:
		if dry_run:
			return ConnectorWriteResult(ok=True, message="dry_run")
		return ConnectorWriteResult(ok=False, message="Connector does not support delete operations")

	def describe_source_columns(
		self,
		*,
		source: str | None = None,
		query: str | None = None,
	) -> list[str]:
		raise RuntimeError("Connector does not support source-column inspection")


class RelationalConnector(BasePartnerConnector):
	partner_type = "relational"
	dialect = "sql"
	required_config: tuple[str, ...] = ()
	driver_candidates: tuple[str, ...] = ()
	paramstyle = "qmark"
	healthcheck_sql = "SELECT 1"

	def ping(self) -> ConnectorPingResult:
		missing = [key for key in self.required_config if not self.config.get(key)]
		if missing:
			return ConnectorPingResult(
				ok=False,
				message=f"Missing required config: {', '.join(missing)}",
				details={"dialect": self.dialect, "missing": missing},
			)

		driver_module = self._load_driver_module()
		if not driver_module:
			return ConnectorPingResult(
				ok=False,
				message=f"No compatible driver installed for {self.dialect}",
				details={"dialect": self.dialect, "driver_candidates": list(self.driver_candidates)},
			)

		try:
			with self._connection() as connection:
				db_cursor = connection.cursor()
				db_cursor.execute(self.healthcheck_sql, [])
				with suppress(Exception):
					db_cursor.fetchall()
				with suppress(Exception):
					db_cursor.close()
		except Exception as err:
			return ConnectorPingResult(
				ok=False,
				message=f"{self.dialect} connection test failed: {err}",
				details={"dialect": self.dialect, "driver": driver_module.__name__},
			)

		return ConnectorPingResult(
			ok=True,
			message=f"{self.dialect} connection test succeeded",
			details={"dialect": self.dialect, "driver": driver_module.__name__},
		)

	def fetch_records(
		self,
		*,
		source: str | None = None,
		query: str | None = None,
		batch_size: int = 100,
		cursor: str | None = None,
		key_fields: list[str] | None = None,
	) -> ConnectorFetchResult:
		source_name, query_text = self._resolve_source(source=source, query=query)
		if not source_name and not query_text:
			raise RuntimeError(f"{self.dialect} fetch requires source table or query")

		offset = _to_non_negative_int(cursor)
		batch_size = max(cint(batch_size) or 100, 1)
		sql = self._build_fetch_sql(
			source=source_name,
			query=query_text,
			batch_size=batch_size,
			offset=offset,
			key_fields=key_fields or [],
		)

		rows = self._run_select(sql, [])
		next_cursor = str(offset + batch_size) if len(rows) >= batch_size else None
		return ConnectorFetchResult(records=rows, next_cursor=next_cursor)

	def upsert_record(
		self,
		*,
		record: dict[str, Any],
		key_fields: list[str],
		mapping: dict[str, str],
		dry_run: bool = False,
		source: str | None = None,
		query: str | None = None,
	) -> ConnectorWriteResult:
		source_name, query_text = self._resolve_source(source=source, query=query)
		if query_text and not source_name:
			return ConnectorWriteResult(ok=False, message=f"{self.dialect} upsert requires a writable table source")
		if not source_name:
			return ConnectorWriteResult(
				ok=False,
				message=f"{self.dialect} upsert missing target table. Provide source/table_name.",
			)
		if not record:
			return ConnectorWriteResult(ok=False, message=f"{self.dialect} upsert received empty record")
		if dry_run:
			return ConnectorWriteResult(ok=True, message="dry_run", changed_fields=list(record.keys()))

		partner_key_fields = [mapping.get(field, field) for field in key_fields]
		missing_key_values = [field for field in partner_key_fields if record.get(field) in (None, "")]
		if missing_key_values:
			return ConnectorWriteResult(
				ok=False,
				message=f"{self.dialect} upsert missing key values for: {', '.join(missing_key_values)}",
			)

		table = self._quote_compound_identifier(source_name)
		record_columns = list(record.keys())
		non_key_columns = [column for column in record_columns if column not in partner_key_fields]

		try:
			with self._connection() as connection:
				db_cursor = connection.cursor()
				updated_rows = 0
				if non_key_columns:
					set_clause = ", ".join(
						f"{self._quote_compound_identifier(column)} = {self._placeholder()}" for column in non_key_columns
					)
					where_clause = " AND ".join(
						f"{self._quote_compound_identifier(column)} = {self._placeholder()}"
						for column in partner_key_fields
					)
					update_sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
					update_params = [record[column] for column in non_key_columns] + [
						record[column] for column in partner_key_fields
					]
					db_cursor.execute(update_sql, update_params)
					updated_rows = max(cint(getattr(db_cursor, "rowcount", 0)), 0)

				if updated_rows == 0:
					insert_columns = ", ".join(self._quote_compound_identifier(column) for column in record_columns)
					insert_values = ", ".join(self._placeholder() for _ in record_columns)
					insert_sql = f"INSERT INTO {table} ({insert_columns}) VALUES ({insert_values})"
					db_cursor.execute(insert_sql, [record[column] for column in record_columns])
					connection.commit()
					with suppress(Exception):
						db_cursor.close()
					return ConnectorWriteResult(
						ok=True,
						message=f"{self.dialect} insert succeeded",
						changed_fields=record_columns,
					)

				connection.commit()
				with suppress(Exception):
					db_cursor.close()
				return ConnectorWriteResult(
					ok=True,
					message=f"{self.dialect} update succeeded",
					changed_fields=non_key_columns,
				)
		except Exception as err:
			return ConnectorWriteResult(ok=False, message=f"{self.dialect} upsert failed: {err}")

	def delete_record(
		self,
		*,
		key_values: dict[str, Any],
		dry_run: bool = False,
		source: str | None = None,
	) -> ConnectorWriteResult:
		source_name, _ = self._resolve_source(source=source, query=None)
		if not source_name:
			return ConnectorWriteResult(
				ok=False,
				message=f"{self.dialect} delete missing target table. Provide source/table_name.",
			)
		if not key_values:
			return ConnectorWriteResult(ok=False, message=f"{self.dialect} delete requires key values")
		if dry_run:
			return ConnectorWriteResult(ok=True, message="dry_run", changed_fields=list(key_values.keys()))

		try:
			table = self._quote_compound_identifier(source_name)
			where_clause = " AND ".join(
				f"{self._quote_compound_identifier(column)} = {self._placeholder()}" for column in key_values
			)
			sql = f"DELETE FROM {table} WHERE {where_clause}"
			with self._connection() as connection:
				db_cursor = connection.cursor()
				db_cursor.execute(sql, list(key_values.values()))
				connection.commit()
				with suppress(Exception):
					db_cursor.close()
			return ConnectorWriteResult(ok=True, message=f"{self.dialect} delete succeeded")
		except Exception as err:
			return ConnectorWriteResult(ok=False, message=f"{self.dialect} delete failed: {err}")

	def describe_source_columns(
		self,
		*,
		source: str | None = None,
		query: str | None = None,
	) -> list[str]:
		source_name, query_text = self._resolve_source(source=source, query=query)
		if query_text and not source_name:
			raise RuntimeError(
				f"{self.dialect} column inspection currently supports table sources only; query inspection is disabled"
			)
		if not source_name:
			raise RuntimeError(f"{self.dialect} column inspection requires a table source")

		table = self._quote_compound_identifier(source_name)
		sql = f"SELECT * FROM {table} WHERE 1 = 0"
		with self._connection() as connection:
			db_cursor = connection.cursor()
			db_cursor.execute(sql, [])
			columns = [column[0] for column in db_cursor.description or []]
			with suppress(Exception):
				db_cursor.close()
		return [str(column) for column in columns if column]

	def _load_driver_module(self):
		for module_name in self._get_driver_candidates():
			with suppress(Exception):
				return importlib.import_module(module_name)
		return None

	def _get_driver_candidates(self) -> tuple[str, ...]:
		candidates: list[str] = []
		preferred_driver = self.config.get("driver_module") or self.config.get("db_api_module")
		if not preferred_driver:
			with suppress(Exception):
				partner_type_name = self.partner_doc.get("partner_type") or self.partner_doc.get("sync_partner_type")
				if partner_type_name and frappe.db.exists("Sync Partner Type", partner_type_name):
					partner_type_doc = frappe.get_doc("Sync Partner Type", partner_type_name)
					preferred_driver = partner_type_doc.get("db_api_module")
		if preferred_driver:
			candidates.append(str(preferred_driver).strip())
		candidates.extend(self.driver_candidates)
		seen: set[str] = set()
		ordered: list[str] = []
		for module_name in candidates:
			if not module_name or module_name in seen:
				continue
			seen.add(module_name)
			ordered.append(module_name)
		return tuple(ordered)

	@contextmanager
	def _connection(self):
		connection = self._connect()
		try:
			yield connection
		except Exception:
			with suppress(Exception):
				connection.rollback()
			raise
		finally:
			with suppress(Exception):
				connection.close()

	@abstractmethod
	def _connect(self):
		raise NotImplementedError

	def _run_select(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
		with self._connection() as connection:
			db_cursor = connection.cursor()
			db_cursor.execute(sql, params)
			rows = db_cursor.fetchall()
			columns = [column[0] for column in db_cursor.description or []]
			with suppress(Exception):
				db_cursor.close()
		return [dict(zip(columns, row, strict=False)) for row in rows]

	def _resolve_source(self, *, source: str | None, query: str | None) -> tuple[str | None, str | None]:
		source_name = source or self.config.get("table_name") or self.config.get("default_table") or self.config.get("source")
		query_text = query or self.config.get("query")
		if isinstance(query_text, str):
			query_text = _strip_trailing_semicolon(query_text.strip()) or None
		if source_name:
			source_name = str(source_name).strip()
		return source_name, query_text

	def _build_fetch_sql(
		self,
		*,
		source: str | None,
		query: str | None,
		batch_size: int,
		offset: int,
		key_fields: list[str],
	) -> str:
		if query:
			if self.dialect == "mssql":
				order_clause = self._mssql_order_clause(key_fields)
				return (
					f"SELECT * FROM ({query}) AS source_rows "
					f"{order_clause} OFFSET {offset} ROWS FETCH NEXT {batch_size} ROWS ONLY"
				)
			if self.dialect == "firebird":
				start_row = offset + 1
				end_row = offset + batch_size
				return f"SELECT * FROM ({query}) source_rows ROWS {start_row} TO {end_row}"
			return f"SELECT * FROM ({query}) AS source_rows LIMIT {batch_size} OFFSET {offset}"

		table = self._quote_compound_identifier(source or "")
		if self.dialect == "mssql":
			order_clause = self._mssql_order_clause(key_fields)
			return f"SELECT * FROM {table} {order_clause} OFFSET {offset} ROWS FETCH NEXT {batch_size} ROWS ONLY"
		if self.dialect == "firebird":
			start_row = offset + 1
			end_row = offset + batch_size
			return f"SELECT * FROM {table} ROWS {start_row} TO {end_row}"
		return f"SELECT * FROM {table} LIMIT {batch_size} OFFSET {offset}"

	def _mssql_order_clause(self, key_fields: list[str]) -> str:
		valid_key_fields = [field for field in key_fields if IDENTIFIER_RE.match(str(field))]
		if not valid_key_fields:
			return "ORDER BY (SELECT NULL)"
		quoted = ", ".join(self._quote_compound_identifier(field) for field in valid_key_fields)
		return f"ORDER BY {quoted}"

	def _placeholder(self) -> str:
		return "%s" if self.paramstyle == "pyformat" else "?"

	def _quote_compound_identifier(self, identifier: str) -> str:
		parts = [part.strip() for part in identifier.split(".") if part.strip()]
		if not parts:
			raise ValueError("Identifier is empty")
		quote_char = self._identifier_quote_char()
		quoted_parts = []
		for part in parts:
			if not IDENTIFIER_RE.match(part):
				raise ValueError(f"Unsafe SQL identifier '{identifier}'")
			quoted_parts.append(f"{quote_char[0]}{part}{quote_char[1]}")
		return ".".join(quoted_parts)

	def _identifier_quote_char(self) -> tuple[str, str]:
		if self.dialect == "mssql":
			return ("[", "]")
		return ('"', '"')


class MssqlConnector(RelationalConnector):
	partner_type = "mssql"
	dialect = "mssql"
	required_config = ("server", "database")
	driver_candidates = ("pyodbc",)
	paramstyle = "qmark"

	def _connect(self):
		driver_module = self._load_driver_module()
		if not driver_module:
			raise RuntimeError("pyodbc is not installed")

		server = self.config.get("server")
		port = self.config.get("port")
		server_part = f"{server},{port}" if port else str(server)
		driver_name = self.config.get("odbc_driver") or "ODBC Driver 18 for SQL Server"

		connection_parts = [
			f"DRIVER={{{driver_name}}}",
			f"SERVER={server_part}",
			f"DATABASE={self.config.get('database')}",
		]
		if self.config.get("user"):
			connection_parts.append(f"UID={self.config.get('user')}")
		if self.config.get("password"):
			connection_parts.append(f"PWD={self.config.get('password')}")
		if _to_bool(self.config.get("trusted_connection")):
			connection_parts.append("Trusted_Connection=yes")
		connection_parts.append(f"Encrypt={'yes' if _to_bool(self.config.get('encrypt', True)) else 'no'}")
		connection_parts.append(
			f"TrustServerCertificate={'yes' if _to_bool(self.config.get('trust_server_certificate')) else 'no'}"
		)
		timeout = max(cint(self.config.get("connect_timeout") or 5), 1)
		return driver_module.connect(";".join(connection_parts), timeout=timeout)


class PostgresConnector(RelationalConnector):
	partner_type = "postgres"
	dialect = "postgres"
	required_config = ("host", "database", "user")
	driver_candidates = ("psycopg", "psycopg2")
	paramstyle = "pyformat"

	def _connect(self):
		driver_module = self._load_driver_module()
		if not driver_module:
			raise RuntimeError("Neither psycopg nor psycopg2 is installed")

		connect_kwargs = {
			"host": self.config.get("host"),
			"port": self.config.get("port") or 5432,
			"dbname": self.config.get("database"),
			"user": self.config.get("user"),
			"password": self.config.get("password"),
			"connect_timeout": max(cint(self.config.get("connect_timeout") or 5), 1),
		}
		if self.config.get("sslmode"):
			connect_kwargs["sslmode"] = self.config.get("sslmode")
		return driver_module.connect(**connect_kwargs)


class FirebirdConnector(RelationalConnector):
	partner_type = "firebird"
	dialect = "firebird"
	required_config = ("host", "database", "user")
	driver_candidates = ("fdb",)
	paramstyle = "qmark"
	healthcheck_sql = "SELECT 1 FROM RDB$DATABASE"

	def _connect(self):
		driver_module = self._load_driver_module()
		if not driver_module:
			raise RuntimeError("fdb is not installed")

		connect_kwargs = {
			"host": self.config.get("host"),
			"port": self.config.get("port") or 3050,
			"database": self.config.get("database"),
			"user": self.config.get("user"),
			"password": self.config.get("password"),
			"charset": self.config.get("charset") or "UTF8",
		}
		return driver_module.connect(**connect_kwargs)


def get_partner_type(partner_doc: Any) -> str:
	for fieldname in ("partner_type", "sync_partner_type", "type", "partner_type_name"):
		value = partner_doc.get(fieldname)
		if value:
			value = str(value).strip().lower()
			if value in {"mssql", "postgres", "firebird"}:
				return value
			if frappe.db.exists("Sync Partner Type", value):
				partner_type_doc = frappe.get_doc("Sync Partner Type", value)
				return str(partner_type_doc.get("partner_type_code") or value).strip().lower()
			return value
	return ""


def get_connector_for_partner(partner_doc: Any) -> BasePartnerConnector:
	partner_type = get_partner_type(partner_doc)
	if partner_type == "mssql":
		return MssqlConnector(partner_doc)
	if partner_type == "postgres":
		return PostgresConnector(partner_doc)
	if partner_type == "firebird":
		return FirebirdConnector(partner_doc)
	return RelationalConnector(partner_doc)


def _parse_config_text(value: Any) -> dict[str, Any]:
	if isinstance(value, dict):
		return value
	if not isinstance(value, str):
		return {}
	text = value.strip()
	if not text:
		return {}

	with suppress(Exception):
		loaded = json.loads(text)
		if isinstance(loaded, dict):
			return loaded

	parsed: dict[str, Any] = {}
	for line in text.splitlines():
		line = line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, raw_value = line.split("=", 1)
		parsed[key.strip()] = raw_value.strip()
	return parsed


def _to_bool(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_non_negative_int(value: Any) -> int:
	with suppress(Exception):
		parsed = int(value)
		return max(parsed, 0)
	return 0


def _strip_trailing_semicolon(query: str) -> str:
	return query[:-1].strip() if query.endswith(";") else query
