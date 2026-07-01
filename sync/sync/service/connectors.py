from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import base64
import importlib
import json
import re
from typing import Any

import frappe
from frappe.utils import cint
from frappe.utils.password import get_decrypted_password

from sync.sync.constants import SYNC_PARTNER_TYPE

POSTGRES_DELIMITED_IDENTIFIER_RE = re.compile(r"^[^\x00\"]+$")
FIREBIRD_REGULAR_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,30}$")
FIREBIRD_DELIMITED_IDENTIFIER_RE = re.compile(r"^[^\x00\"]+$")
FIREBIRD_MAX_IDENTIFIER_BYTES = 31
JSON_CONFIG_FIELDS = {
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
	action: str | None = None
	record: dict[str, Any] | None = None
	resolved_key_values: dict[str, Any] | None = None


@dataclass(slots=True)
class ConnectorCreateOptions:
	identity_field: str | None = None
	strategy: str = "payload"
	source: str | None = None
	scope_where: str | None = None


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
		key_values: dict[str, Any] | None = None,
		key_fields: list[str] | None = None,
		mapping: dict[str, str] | None = None,
		dry_run: bool = False,
		source: str | None = None,
		query: str | None = None,
		create_options: ConnectorCreateOptions | None = None,
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

		key_fields = self._normalize_fetch_key_fields(key_fields or [])
		if not key_fields:
			raise RuntimeError(f"{self.dialect} fetch requires stable key fields for pagination")
		cursor_values = _decode_keyset_cursor(cursor)
		batch_size = max(cint(batch_size) or 100, 1)
		where_clause, params = self._keyset_where_clause(key_fields, cursor_values)
		query_batch_size = batch_size + 1
		sql = self._build_fetch_sql(
			source=source_name,
			query=query_text,
			batch_size=query_batch_size,
			key_fields=key_fields or [],
			where_clause=where_clause,
		)

		rows = self._run_select(sql, params)
		has_more = len(rows) > batch_size
		records = rows[:batch_size]
		next_cursor = self._next_fetch_cursor(records, rows[batch_size] if has_more else None, key_fields)
		return ConnectorFetchResult(records=records, next_cursor=next_cursor)

	def upsert_record(
		self,
		*,
		record: dict[str, Any],
		key_values: dict[str, Any] | None = None,
		key_fields: list[str] | None = None,
		mapping: dict[str, str] | None = None,
		dry_run: bool = False,
		source: str | None = None,
		query: str | None = None,
		create_options: ConnectorCreateOptions | None = None,
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
			return ConnectorWriteResult(
				ok=True,
				message="dry_run",
				changed_fields=list(record.keys()),
				action="updated",
				record=dict(record),
				resolved_key_values=dict(key_values or {}),
			)

		create_options = create_options or ConnectorCreateOptions()
		try:
			key_values = self._normalize_key_values(
				key_values=key_values,
				key_fields=key_fields or [],
				mapping=mapping or {},
				record=record,
			)
		except RuntimeError as err:
			return ConnectorWriteResult(ok=False, message=str(err))

		table = self._quote_compound_identifier(source_name)

		try:
			with self._connection() as connection:
				db_cursor = connection.cursor()
				updated_rows = 0
				record_to_write = dict(record)
				record_columns = list(record_to_write.keys())
				non_key_columns = [column for column in record_columns if column not in key_values]
				if key_values and non_key_columns:
					set_clause = ", ".join(
						f"{self._quote_compound_identifier(column)} = {self._placeholder()}" for column in non_key_columns
					)
					where_clause = " AND ".join(
						f"{self._quote_compound_identifier(column)} = {self._placeholder()}"
						for column in key_values
					)
					update_sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
					update_params = [record_to_write[column] for column in non_key_columns] + list(key_values.values())
					db_cursor.execute(update_sql, update_params)
					updated_rows = max(cint(getattr(db_cursor, "rowcount", 0)), 0)

				if updated_rows == 0:
					record_to_write, insert_lookup_values = self._prepare_insert_record(
						connection=connection,
						source_name=source_name,
						record=record_to_write,
						key_values=key_values,
						create_options=create_options,
					)
					record_columns = list(record_to_write.keys())
					if not record_columns:
						return ConnectorWriteResult(ok=False, message=f"{self.dialect} upsert received empty record")
					insert_columns = ", ".join(self._quote_compound_identifier(column) for column in record_columns)
					insert_values = ", ".join(self._placeholder() for _ in record_columns)
					insert_sql = f"INSERT INTO {table} ({insert_columns}) VALUES ({insert_values})"
					db_cursor.execute(insert_sql, [record_to_write[column] for column in record_columns])
					connection.commit()
					resolved_record = (
						self._load_record_by_key_values(source_name, insert_lookup_values)
						if create_options.identity_field or create_options.strategy != "payload"
						else None
					) or dict(record_to_write)
					with suppress(Exception):
						db_cursor.close()
					return ConnectorWriteResult(
						ok=True,
						message=f"{self.dialect} insert succeeded",
						changed_fields=record_columns,
						action="created",
						record=resolved_record,
						resolved_key_values=self._resolved_key_values(create_options, insert_lookup_values, resolved_record),
					)

				connection.commit()
				resolved_record = (
					self._load_record_by_key_values(source_name, key_values)
					if create_options.identity_field or create_options.strategy != "payload"
					else None
				) or dict(record_to_write)
				with suppress(Exception):
					db_cursor.close()
				return ConnectorWriteResult(
					ok=True,
					message=f"{self.dialect} update succeeded",
					changed_fields=non_key_columns,
					action="updated",
					record=resolved_record,
					resolved_key_values=self._resolved_key_values(create_options, key_values, resolved_record),
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

	def _normalize_key_values(
		self,
		*,
		key_values: dict[str, Any] | None,
		key_fields: list[str],
		mapping: dict[str, str],
		record: dict[str, Any],
	) -> dict[str, Any]:
		if isinstance(key_values, dict) and key_values:
			return {str(field): value for field, value in key_values.items() if field and value not in (None, "")}
		partner_key_fields = [mapping.get(field, field) for field in key_fields]
		result = {field: record.get(field) for field in partner_key_fields if record.get(field) not in (None, "")}
		missing = [field for field in partner_key_fields if field not in result]
		if missing:
			raise RuntimeError(f"{self.dialect} upsert missing key values for: {', '.join(missing)}")
		return result

	def _prepare_insert_record(
		self,
		*,
		connection: Any,
		source_name: str,
		record: dict[str, Any],
		key_values: dict[str, Any],
		create_options: ConnectorCreateOptions,
	) -> tuple[dict[str, Any], dict[str, Any]]:
		record_to_write = dict(record)
		lookup_values = dict(key_values)
		identity_field = (create_options.identity_field or "").strip()
		strategy = (create_options.strategy or "payload").strip().lower()

		if strategy == "connector_default":
			if identity_field and record_to_write.get(identity_field) in (None, ""):
				record_to_write.pop(identity_field, None)
			return record_to_write, lookup_values

		if strategy == "sequence":
			if not identity_field:
				raise RuntimeError(f"{self.dialect} sequence create requires an identity field")
			next_value = self._next_sequence_value(connection, create_options.source or "")
			record_to_write[identity_field] = next_value
			lookup_values = {identity_field: next_value}
			return record_to_write, lookup_values

		if strategy == "max_plus_one":
			if not identity_field:
				raise RuntimeError(f"{self.dialect} max_plus_one create requires an identity field")
			next_value = self._next_scoped_max_plus_one(
				connection,
				source_name=source_name,
				identity_field=identity_field,
				scope_where=create_options.scope_where or "",
			)
			record_to_write[identity_field] = next_value
			lookup_values = {identity_field: next_value}
			return record_to_write, lookup_values

		if identity_field and record_to_write.get(identity_field) not in (None, ""):
			lookup_values = {identity_field: record_to_write[identity_field]}
		return record_to_write, lookup_values

	def _resolved_key_values(
		self,
		create_options: ConnectorCreateOptions,
		lookup_values: dict[str, Any],
		record: dict[str, Any] | None,
	) -> dict[str, Any]:
		result = dict(lookup_values)
		identity_field = (create_options.identity_field or "").strip()
		if identity_field and isinstance(record, dict) and record.get(identity_field) not in (None, ""):
			result[identity_field] = record.get(identity_field)
		return result

	def _load_record_by_key_values(self, source_name: str, key_values: dict[str, Any]) -> dict[str, Any] | None:
		if not key_values:
			return None
		table = self._quote_compound_identifier(source_name)
		where_clause = " AND ".join(
			f"{self._quote_compound_identifier(column)} = {self._placeholder()}" for column in key_values
		)
		sql = f"SELECT * FROM {table} WHERE {where_clause}"
		rows = self._run_select(sql, list(key_values.values()))
		return rows[0] if rows else None

	def _next_sequence_value(self, connection: Any, sequence_name: str) -> Any:
		clean_sequence = str(sequence_name or "").strip()
		if not clean_sequence:
			raise RuntimeError(f"{self.dialect} sequence create requires a source name")
		db_cursor = connection.cursor()
		try:
			if self.dialect == "postgres":
				db_cursor.execute("SELECT nextval(%s)", [clean_sequence])
			elif self.dialect == "firebird":
				db_cursor.execute(f"SELECT NEXT VALUE FOR {self._quote_compound_identifier(clean_sequence)} FROM RDB$DATABASE", [])
			else:
				db_cursor.execute(f"SELECT NEXT VALUE FOR {self._quote_compound_identifier(clean_sequence)}", [])
			row = db_cursor.fetchone()
			if not row:
				raise RuntimeError(f"{self.dialect} sequence returned no value")
			return row[0]
		finally:
			with suppress(Exception):
				db_cursor.close()

	def _next_scoped_max_plus_one(
		self,
		connection: Any,
		*,
		source_name: str,
		identity_field: str,
		scope_where: str,
	) -> int:
		scope_sql, scope_params = self._build_scope_where(scope_where)
		if not scope_sql:
			raise RuntimeError(f"{self.dialect} max_plus_one create requires a scope predicate")
		table = self._quote_compound_identifier(source_name)
		field = self._quote_compound_identifier(identity_field)
		lock_clause = " WITH (UPDLOCK, HOLDLOCK)" if self.dialect == "mssql" else ""
		null_fn = "ISNULL" if self.dialect == "mssql" else "COALESCE"
		sql = f"SELECT {null_fn}(MAX({field}), 0) + 1 FROM {table}{lock_clause} WHERE {scope_sql}"
		db_cursor = connection.cursor()
		try:
			db_cursor.execute(sql, scope_params)
			row = db_cursor.fetchone()
			if not row:
				raise RuntimeError(f"{self.dialect} max_plus_one returned no value")
			return cint(row[0]) or 0
		finally:
			with suppress(Exception):
				db_cursor.close()

	def describe_source_columns(
		self,
		*,
		source: str | None = None,
		query: str | None = None,
	) -> list[str]:
		source_name, query_text = self._resolve_source(source=source, query=query)
		if not source_name and not query_text:
			raise RuntimeError(f"{self.dialect} column inspection requires a table source or read query")

		sql = self._build_describe_source_columns_sql(source=source_name, query=query_text)
		with self._connection() as connection:
			db_cursor = connection.cursor()
			db_cursor.execute(sql, [])
			columns = [column[0] for column in db_cursor.description or []]
			with suppress(Exception):
				db_cursor.close()
		return [str(column) for column in columns if column]

	def _build_describe_source_columns_sql(self, *, source: str | None, query: str | None) -> str:
		if query:
			if self.dialect == "firebird":
				return f"SELECT * FROM ({query}) source_rows WHERE 1 = 0"
			return f"SELECT * FROM ({query}) AS source_rows WHERE 1 = 0"
		table = self._quote_compound_identifier(source or "")
		return f"SELECT * FROM {table} WHERE 1 = 0"

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
				if partner_type_name and frappe.db.exists(SYNC_PARTNER_TYPE, partner_type_name):
					partner_type_doc = frappe.get_doc(SYNC_PARTNER_TYPE, partner_type_name)
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
		query_text = query
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
		key_fields: list[str],
		where_clause: str | None,
	) -> str:
		order_clause = self._order_clause(key_fields)
		where_sql = f" WHERE {where_clause}" if where_clause else ""
		if query:
			if self.dialect == "mssql":
				return (
					f"SELECT * FROM ({query}) AS source_rows{where_sql} "
					f"{order_clause} OFFSET 0 ROWS FETCH NEXT {batch_size} ROWS ONLY"
				)
			if self.dialect == "firebird":
				return f"SELECT * FROM ({query}) source_rows{where_sql} {order_clause} ROWS 1 TO {batch_size}"
			return f"SELECT * FROM ({query}) AS source_rows{where_sql} {order_clause} LIMIT {batch_size}"

		table = self._quote_compound_identifier(source or "")
		if self.dialect == "mssql":
			return f"SELECT * FROM {table}{where_sql} {order_clause} OFFSET 0 ROWS FETCH NEXT {batch_size} ROWS ONLY"
		if self.dialect == "firebird":
			return f"SELECT * FROM {table}{where_sql} {order_clause} ROWS 1 TO {batch_size}"
		return f"SELECT * FROM {table}{where_sql} {order_clause} LIMIT {batch_size}"

	def _mssql_order_clause(self, key_fields: list[str]) -> str:
		return self._order_clause(key_fields)

	def _order_clause(self, key_fields: list[str]) -> str:
		quoted_fields: list[str] = []
		for field in key_fields:
			try:
				quoted_fields.append(self._quote_compound_identifier(str(field)))
			except ValueError:
				continue
		if not quoted_fields:
			raise RuntimeError(f"{self.dialect} fetch requires stable key fields for pagination")
		return f"ORDER BY {', '.join(quoted_fields)}"

	def _normalize_fetch_key_fields(self, key_fields: list[str]) -> list[str]:
		seen: set[str] = set()
		result: list[str] = []
		for field in key_fields:
			cleaned = str(field or "").strip()
			if not cleaned or cleaned in seen:
				continue
			self._quote_compound_identifier(cleaned)
			seen.add(cleaned)
			result.append(cleaned)
		return result

	def _keyset_where_clause(self, key_fields: list[str], cursor_values: list[Any] | None) -> tuple[str | None, list[Any]]:
		if not cursor_values:
			return None, []
		if len(cursor_values) != len(key_fields):
			raise RuntimeError(f"{self.dialect} fetch received an invalid pagination cursor")
		clauses: list[str] = []
		params: list[Any] = []
		for idx, field in enumerate(key_fields):
			prefix = [
				f"{self._quote_compound_identifier(key_fields[prefix_idx])} = {self._placeholder()}"
				for prefix_idx in range(idx)
			]
			params.extend(cursor_values[:idx])
			prefix.append(f"{self._quote_compound_identifier(field)} > {self._placeholder()}")
			params.append(cursor_values[idx])
			clauses.append("(" + " AND ".join(prefix) + ")")
		return "(" + " OR ".join(clauses) + ")", params

	def _next_fetch_cursor(
		self,
		records: list[dict[str, Any]],
		lookahead_record: dict[str, Any] | None,
		key_fields: list[str],
	) -> str | None:
		if not lookahead_record:
			return None
		cursor_record = records[-1] if records else lookahead_record
		cursor_values = self._record_keyset_values(cursor_record, key_fields)
		cursor = _encode_keyset_cursor_values(cursor_values)
		if not cursor:
			raise RuntimeError(
				f"{self.dialect} fetch pagination requires non-empty values for key fields: {', '.join(key_fields)}"
			)
		if cursor_values == self._record_keyset_values(lookahead_record, key_fields):
			raise RuntimeError(
				f"{self.dialect} fetch pagination requires unique key fields. Configure Partner Identity Field "
				"or another unique non-empty partner fetch key."
			)
		return cursor

	def _record_keyset_values(self, record: dict[str, Any], key_fields: list[str]) -> list[Any]:
		values: list[Any] = []
		for field in key_fields:
			if field in record:
				values.append(record.get(field))
			elif self.dialect == "firebird" and field.upper() in record:
				values.append(record.get(field.upper()))
			else:
				values.append(record.get(field))
		return values

	def _build_scope_where(self, scope_where: str | None) -> tuple[str | None, list[Any]]:
		return _parse_scope_where(scope_where, quote_identifier=self._quote_compound_identifier, placeholder=self._placeholder())

	def _placeholder(self) -> str:
		return "%s" if self.paramstyle == "pyformat" else "?"

	def _quote_compound_identifier(self, identifier: str) -> str:
		parts = _split_compound_identifier(identifier, dialect=self.dialect)
		if not parts:
			raise ValueError("Identifier is empty")
		quote_char = self._identifier_quote_char()
		quoted_parts = []
		for part in parts:
			if self.dialect == "mssql":
				part = _normalize_mssql_identifier_part(part, original_identifier=identifier)
			elif self.dialect == "firebird":
				part = _normalize_firebird_identifier_part(part, original_identifier=identifier)
			elif not POSTGRES_DELIMITED_IDENTIFIER_RE.match(part):
				raise ValueError(f"Unsafe SQL identifier '{identifier}'")
			if self.dialect == "mssql":
				part = part.replace("]", "]]")
			quoted_parts.append(f"{quote_char[0]}{part}{quote_char[1]}")
		return ".".join(quoted_parts)

	def _identifier_quote_char(self) -> tuple[str, str]:
		if self.dialect == "mssql":
			return ("[", "]")
		return ('"', '"')


class MssqlConnector(RelationalConnector):
	partner_type = "mssql"
	dialect = "mssql"
	required_config = ("host", "database_name")
	driver_candidates = ("pyodbc",)
	paramstyle = "qmark"

	def _connect(self):
		driver_module = self._load_driver_module()
		if not driver_module:
			raise RuntimeError("pyodbc is not installed")

		server = self.config.get("host")
		port = self.config.get("port")
		server_part = f"{server},{port}" if port else str(server)
		driver_name = self.config.get("odbc_driver") or "ODBC Driver 18 for SQL Server"

		connection_parts = [
			f"DRIVER={{{driver_name}}}",
			f"SERVER={server_part}",
			f"DATABASE={self.config.get('database_name')}",
		]
		if self.config.get("username"):
			connection_parts.append(f"UID={self.config.get('username')}")
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
	required_config = ("host", "database_name", "username")
	driver_candidates = ("psycopg", "psycopg2")
	paramstyle = "pyformat"

	def _connect(self):
		driver_module = self._load_driver_module()
		if not driver_module:
			raise RuntimeError("Neither psycopg nor psycopg2 is installed")

		connect_kwargs = {
			"host": self.config.get("host"),
			"port": self.config.get("port") or 5432,
			"dbname": self.config.get("database_name"),
			"user": self.config.get("username"),
			"password": self.config.get("password"),
			"connect_timeout": max(cint(self.config.get("connect_timeout") or 5), 1),
		}
		if self.config.get("sslmode"):
			connect_kwargs["sslmode"] = self.config.get("sslmode")
		return driver_module.connect(**connect_kwargs)


class FirebirdConnector(RelationalConnector):
	partner_type = "firebird"
	dialect = "firebird"
	required_config = ("host", "database_name", "username")
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
			"database": self.config.get("database_name"),
			"user": self.config.get("username"),
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
			with suppress(Exception):
				if frappe.db.exists(SYNC_PARTNER_TYPE, value):
					partner_type_doc = frappe.get_doc(SYNC_PARTNER_TYPE, value)
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
	raise frappe.ValidationError(f"Unsupported Sync Partner Type: {partner_type or 'unknown'}")


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


def _split_compound_identifier(identifier: str, *, dialect: str) -> list[str]:
	raw_identifier = str(identifier or "").strip()
	if not raw_identifier:
		return []
	if dialect != "mssql":
		parts = [part.strip() for part in raw_identifier.split(".")]
		if any(not part for part in parts):
			raise ValueError(f"Unsafe SQL identifier '{identifier}'")
		return parts

	parts: list[str] = []
	current: list[str] = []
	in_brackets = False
	i = 0
	length = len(raw_identifier)
	while i < length:
		char = raw_identifier[i]
		if in_brackets:
			if char == "]":
				if i + 1 < length and raw_identifier[i + 1] == "]":
					current.append("]")
					i += 2
					continue
				in_brackets = False
				i += 1
				continue
			current.append(char)
			i += 1
			continue

		if char == ".":
			part = "".join(current).strip()
			if not part:
				raise ValueError(f"Unsafe SQL identifier '{identifier}'")
			parts.append(part)
			current = []
			i += 1
			continue
		if char == "[":
			if "".join(current).strip():
				raise ValueError(f"Unsafe SQL identifier '{identifier}'")
			in_brackets = True
			i += 1
			continue
		if char == "]":
			raise ValueError(f"Unsafe SQL identifier '{identifier}'")
		current.append(char)
		i += 1

	if in_brackets:
		raise ValueError(f"Unsafe SQL identifier '{identifier}'")

	part = "".join(current).strip()
	if part:
		parts.append(part)
	if not parts:
		raise ValueError("Identifier is empty")
	return parts


def _normalize_mssql_identifier_part(part: str, *, original_identifier: str) -> str:
	value = str(part or "").strip()
	if not value:
		raise ValueError(f"Unsafe SQL identifier '{original_identifier}'")
	return value


def _normalize_firebird_identifier_part(part: str, *, original_identifier: str) -> str:
	value = str(part or "").strip()
	if not value:
		raise ValueError(f"Unsafe SQL identifier '{original_identifier}'")
	if FIREBIRD_REGULAR_IDENTIFIER_RE.match(value):
		return value.upper()
	if not FIREBIRD_DELIMITED_IDENTIFIER_RE.match(value):
		raise ValueError(f"Unsafe SQL identifier '{original_identifier}'")
	if len(value.encode("utf-8")) > FIREBIRD_MAX_IDENTIFIER_BYTES:
		raise ValueError(f"Unsafe SQL identifier '{original_identifier}'")
	return value


def _encode_keyset_cursor(record: dict[str, Any], key_fields: list[str]) -> str | None:
	if not record:
		return None
	values = _keyset_cursor_values(record, key_fields)
	return _encode_keyset_cursor_values(values)


def _encode_keyset_cursor_values(values: list[Any]) -> str | None:
	if any(value in (None, "") for value in values):
		return None
	payload = json.dumps(values, default=str, ensure_ascii=True, separators=(",", ":"))
	return base64.urlsafe_b64encode(payload.encode()).decode()


def _keyset_cursor_values(record: dict[str, Any], key_fields: list[str]) -> list[Any]:
	return [record.get(field) for field in key_fields]


def _decode_keyset_cursor(value: str | None) -> list[Any] | None:
	if not value:
		return None
	try:
		decoded = base64.urlsafe_b64decode(str(value).encode()).decode()
		payload = json.loads(decoded)
	except Exception as exc:
		raise ValueError("Invalid pagination cursor") from exc
	if not isinstance(payload, list):
		raise ValueError("Invalid pagination cursor")
	return payload


SCOPE_TOKEN_RE = re.compile(
	r"""
	\s*(
		<=|>=|<>|!=|=|<|>|
		\(|\)|,|
		'(?:''|[^'])*'|
		-?\d+(?:\.\d+)?|
		[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*
	)
	""",
	re.VERBOSE,
)
SCOPE_COMPARISON_OPERATORS = {"=", "!=", "<>", "<", "<=", ">", ">=", "LIKE"}


def _parse_scope_where(
	value: str | None,
	*,
	quote_identifier,
	placeholder: str,
) -> tuple[str | None, list[Any]]:
	if value is None:
		return None, []
	cleaned = _strip_trailing_semicolon(str(value).strip())
	if not cleaned:
		return None, []
	for forbidden in (";", "--", "/*", "*/"):
		if forbidden in cleaned:
			raise ValueError("Unsafe scope predicate")
	tokens = _tokenize_scope_where(cleaned)
	parser = _ScopeWhereParser(tokens, quote_identifier=quote_identifier, placeholder=placeholder)
	sql, params = parser.parse_expression()
	if parser.has_tokens:
		raise ValueError("Unsafe scope predicate")
	return sql, params


def _validate_scope_where(value: str | None) -> str | None:
	sql, _params = _parse_scope_where(value, quote_identifier=lambda identifier: identifier, placeholder="?")
	return sql


def _tokenize_scope_where(value: str) -> list[str]:
	tokens: list[str] = []
	position = 0
	while position < len(value):
		match = SCOPE_TOKEN_RE.match(value, position)
		if not match:
			raise ValueError("Unsafe scope predicate")
		token = match.group(1)
		tokens.append(token)
		position = match.end()
	if not tokens:
		raise ValueError("Unsafe scope predicate")
	return tokens


class _ScopeWhereParser:
	def __init__(self, tokens: list[str], *, quote_identifier, placeholder: str):
		self.tokens = tokens
		self.index = 0
		self.quote_identifier = quote_identifier
		self.placeholder = placeholder
		self.params: list[Any] = []

	@property
	def has_tokens(self) -> bool:
		return self.index < len(self.tokens)

	def parse_expression(self) -> tuple[str, list[Any]]:
		sql = self.parse_term()
		while self._accept_keyword("AND"):
			sql = f"{sql} AND {self.parse_term()}"
		return sql, self.params

	def parse_term(self) -> str:
		if self._accept("("):
			sql, _params = self.parse_expression()
			self._expect(")")
			return f"({sql})"
		return self.parse_predicate()

	def parse_predicate(self) -> str:
		identifier = self._expect_identifier()
		field_sql = self.quote_identifier(identifier)
		if self._accept_keyword("IS"):
			if self._accept_keyword("NOT"):
				self._expect_keyword("NULL")
				return f"{field_sql} IS NOT NULL"
			self._expect_keyword("NULL")
			return f"{field_sql} IS NULL"
		if self._accept_keyword("BETWEEN"):
			first = self._expect_literal()
			self._expect_keyword("AND")
			second = self._expect_literal()
			self.params.extend([first, second])
			return f"{field_sql} BETWEEN {self.placeholder} AND {self.placeholder}"
		if self._accept_keyword("IN"):
			self._expect("(")
			values = [self._expect_literal()]
			while self._accept(","):
				values.append(self._expect_literal())
			self._expect(")")
			self.params.extend(values)
			return f"{field_sql} IN ({', '.join([self.placeholder] * len(values))})"
		operator = self._next()
		if operator is None or operator.upper() not in SCOPE_COMPARISON_OPERATORS:
			raise ValueError("Unsafe scope predicate")
		value = self._expect_literal()
		self.params.append(value)
		return f"{field_sql} {operator.upper()} {self.placeholder}"

	def _expect_identifier(self) -> str:
		token = self._next()
		if not token or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$", token):
			raise ValueError("Unsafe scope predicate")
		if token.upper() in {"AND", "OR", "LIKE", "IN", "BETWEEN", "IS", "NOT", "NULL"}:
			raise ValueError("Unsafe scope predicate")
		return token

	def _expect_literal(self) -> Any:
		token = self._next()
		if token is None:
			raise ValueError("Unsafe scope predicate")
		if token.startswith("'") and token.endswith("'"):
			return token[1:-1].replace("''", "'")
		if re.match(r"^-?\d+(?:\.\d+)?$", token):
			return float(token) if "." in token else int(token)
		raise ValueError("Unsafe scope predicate")

	def _next(self) -> str | None:
		if self.index >= len(self.tokens):
			return None
		token = self.tokens[self.index]
		self.index += 1
		return token

	def _peek(self) -> str | None:
		if self.index >= len(self.tokens):
			return None
		return self.tokens[self.index]

	def _accept(self, token: str) -> bool:
		if self._peek() == token:
			self.index += 1
			return True
		return False

	def _accept_keyword(self, keyword: str) -> bool:
		if str(self._peek() or "").upper() == keyword:
			self.index += 1
			return True
		return False

	def _expect(self, token: str) -> None:
		if not self._accept(token):
			raise ValueError("Unsafe scope predicate")

	def _expect_keyword(self, keyword: str) -> None:
		if not self._accept_keyword(keyword):
			raise ValueError("Unsafe scope predicate")
