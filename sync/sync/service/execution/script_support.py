"""UI-owned, read-only planning with runtime-owned atomic writes and durable state."""

from __future__ import annotations

import hashlib
import heapq
import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from tempfile import TemporaryFile

import frappe
from frappe import _
from RestrictedPython import compile_restricted
from RestrictedPython.Guards import (
	full_write_guard,
	guarded_iter_unpack_sequence,
	guarded_unpack_sequence,
	safe_builtins,
	safer_getattr,
)

from sync.sync.service import mapping_rules, matching, query_templates
from sync.sync.service.execution.progress import Progress

_reusable_source = ContextVar("sync_prepared_source", default=None)


def json_default(value):
	if isinstance(value, (datetime, date)):
		return value.isoformat()
	if isinstance(value, Decimal):
		return str(value)
	if isinstance(value, bytes):
		return value.hex()
	raise TypeError(f"Unsupported script value: {type(value).__name__}")


def encode(value):
	return json.dumps(value, sort_keys=True, default=json_default, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value):
	return hashlib.sha256(encode(value).encode()).hexdigest()


def decode(value, default=None):
	return (
		json.loads(value) if isinstance(value, str) and value else (value if value is not None else default)
	)


@lru_cache(maxsize=128)
def compile_script(script):
	from frappe.utils.safe_exec import FrappeTransformer

	return compile_restricted(script, filename="<sync processing script>", policy=FrappeTransformer)


def execute_read_script(script, context):
	"""Do not expose Frappe's ordinary safe_exec globals: those include writes and HTTP."""
	from frappe.utils.safe_exec import is_safe_exec_enabled, protected_inplacevar, safe_exec_flags

	if not is_safe_exec_enabled():
		raise frappe.ValidationError(_("Partner processing scripts require server_script_enabled."))
	globals_ = {
		"_getattr_": safer_getattr,
		"_getitem_": lambda obj, key: obj[key],
		"_getiter_": iter,
		"_write_": full_write_guard,
		"_inplacevar_": protected_inplacevar,
		"_iter_unpack_sequence_": guarded_iter_unpack_sequence,
		"_unpack_sequence_": guarded_unpack_sequence,
	}
	globals_["__builtins__"] = {
		**safe_builtins,
		"dict": dict,
		"list": list,
		"set": set,
		"tuple": tuple,
		"sum": sum,
		"min": min,
		"max": max,
		"sorted": sorted,
		"enumerate": enumerate,
		"zip": zip,
		"all": all,
		"any": any,
		"reversed": reversed,
	}
	globals_.update(context)
	with safe_exec_flags():
		exec(compile_script(script), globals_)
	return globals_.get("result")


def check_read_query(query):
	import sqlparse
	from sqlparse import tokens

	statements = [s for s in sqlparse.parse(str(query)) if str(s).strip()]
	if len(statements) != 1 or statements[0].get_type() != "SELECT":
		raise frappe.ValidationError(_("Processing scripts only allow a single SELECT query."))
	for token in statements[0].flatten():
		if token.ttype in tokens.Keyword and (
			token.normalized in {"INTO", "EXEC", "EXECUTE", "OPENROWSET", "OPENQUERY", "FOR UPDATE"}
			or token.ttype in tokens.Keyword.DDL
			or (token.ttype in tokens.Keyword.DML and token.normalized != "SELECT")
		):
			raise frappe.ValidationError(_("Processing scripts only allow a single SELECT query."))


class ReadHelpers:
	def heap_push(self, heap, value):
		heapq.heappush(heap, value)

	def heap_pop(self, heap):
		return heapq.heappop(heap)

	def decimal(self, value):
		return Decimal(str(value or 0))

	def fingerprint(self, value):
		return fingerprint(value)

	def get_all(self, doctype, filters=None, fields=None):
		frappe.has_permission(doctype, "read", throw=True)
		# Plain fields only; do not expose SQL expressions through the script helper.
		fields = fields or ["name"]
		meta = frappe.get_meta(doctype)
		if any(field != "name" and not meta.has_field(field) for field in fields):
			raise frappe.ValidationError(_("Script lookup fields must exist on the DocType."))
		return [dict(row) for row in frappe.get_list(doctype, filters=filters, fields=fields, limit=0)]

	def get_doc(self, doctype, name):
		doc = frappe.get_doc(doctype, name)
		doc.check_permission("read")
		return deepcopy(doc.as_dict())


class SourceHelpers(ReadHelpers):
	def __init__(self, config, connector, output):
		self._config, self._connector, self._output = config, connector, output
		self._reads = []
		self._lookups = []
		self._progress = Progress(config.name)
		self._keys = set()
		self._aliases = set()
		self._record_key = RecordKeyResolver(config)
		self.count = 0

	def emit(self, record):
		if not isinstance(record, dict):
			raise frappe.ValidationError(_("Source script must emit dictionaries."))
		key = self._record_key(record)
		if key in self._aliases:
			raise frappe.ValidationError(_("Emitted source key is also claimed by another group."))
		if key in self._keys:
			raise frappe.ValidationError(f"Source script emitted duplicate key: {key}")
		self._keys.add(key)
		for alias in record.get("_sync", {}).get("aliases", []):
			alias_key = self._record_key(alias)
			if alias_key in self._keys or alias_key in self._aliases:
				raise frappe.ValidationError(_("Source alias is claimed by multiple groups."))
			self._aliases.add(alias_key)
		self._output.write(encode(record) + "\n")
		self.count += 1
		if self.count % 1000 == 0:
			self._progress.report("normalized groups", self.count)

	def progress(self, phase, count=0):
		self._progress.report(str(phase), count)

	def get_all(self, doctype, filters=None, fields=None):
		result = super().get_all(doctype, filters, fields)
		self._lookups.append(("get_all", deepcopy((doctype, filters, fields)), fingerprint(result)))
		return result

	def get_doc(self, doctype, name):
		result = super().get_doc(doctype, name)
		self._lookups.append(("get_doc", (doctype, name), fingerprint(result)))
		return result

	def source_tables(self):
		result = self._source_tables()
		self._lookups.append(("source_tables", (), fingerprint(result)))
		return result

	def _source_tables(self):
		return [
			{"name": t.name, "schema": t.schema, "quoted_name": t.quoted_name}
			for t in self._connector.list_source_tables()
		]

	def quote_identifier(self, identifier):
		return self._connector.quote_identifier(str(identifier))

	def query(self, query, key_fields):
		return list(self._read(query, key_fields))

	def _read(self, query, key_fields):
		check_read_query(query)
		if not key_fields:
			raise frappe.ValidationError(_("Script queries require stable key fields."))
		read = {"query": query, "key_fields": list(key_fields), "complete": False}
		self._reads.append(read)

		def records():
			digest = hashlib.sha256()
			count = 0
			phase = "source read " + str(len(self._reads))
			self._progress.report(phase)
			batches = iter(
				self._connector.iter_record_batches(
					source=None,
					query=query,
					batch_size=max(self._config.batch_size, 1000),
					key_fields=key_fields,
				)
			)
			while True:
				with self._progress.measure("source fetch"):
					batch = next(batches, None)
				if batch is None:
					break
				for row in batch:
					digest.update((encode(row) + "\n").encode())
					yield row
				count += len(batch)
				self._progress.report(phase, count)
			read.update(complete=True, digest=digest.hexdigest())
			self._progress.report(phase, count, force=True)

		return records()

	def _verify(self):
		for index, read in enumerate(self._reads):
			if not read["complete"]:
				raise frappe.ValidationError(_("Source script did not consume the complete source."))
			digest = hashlib.sha256()
			count = 0
			phase = "verify source " + str(index + 1)
			self._progress.report(phase, force=True)
			for batch in self._connector.iter_record_batches(
				source=None,
				query=read["query"],
				batch_size=max(self._config.batch_size, 1000),
				key_fields=read["key_fields"],
			):
				for row in batch:
					digest.update((encode(row) + "\n").encode())
				count += len(batch)
				self._progress.report(phase, count)
			if digest.hexdigest() != read["digest"]:
				raise SourceChanged("Partner source changed while preparing the run.")
			self._progress.report(phase, count, force=True)
		for method, args, expected in self._lookups:
			result = self._source_tables() if method == "source_tables" else getattr(super(), method)(*args)
			if fingerprint(result) != expected:
				raise SourceChanged("Source lookup changed while preparing the run.")


class SourceChanged(RuntimeError):
	pass


@contextmanager
def prepare_source_snapshot(config, connector):
	"""Stage on disk before any target write. Retry concurrent source changes, not read failures."""
	with TemporaryFile(mode="w+t", encoding="utf-8") as output:
		for attempt in range(3):
			output.seek(0)
			output.truncate()
			helpers = SourceHelpers(config, connector, output)
			query = query_templates.resolve_read_query(config, connector)
			if not query:
				query = f"SELECT * FROM {connector.quote_identifier(config.table_name)}"
			rows = helpers._read(query, matching._partner_fetch_key_fields(config))
			with helpers._progress.heartbeat(), helpers._progress.measure("read and normalize"):
				execute_read_script(
					config.partner_source_script or "for row in rows:\n    helpers.emit(row)",
					{
						"rows": rows,
						"helpers": helpers,
						"parameters": deepcopy(config.script_parameters or {}),
					},
				)
			try:
				with helpers._progress.heartbeat(), helpers._progress.measure("verify"):
					helpers._verify()
			except SourceChanged:
				if attempt == 2:
					raise
				continue
			break
		# Only the file and dependency digests are needed after successful staging.
		helpers._keys.clear()
		helpers._aliases.clear()
		helpers._progress.timings["normalize and stage"] = helpers._progress.timings[
			"read and normalize"
		] - helpers._progress.timings.get("source fetch", 0)
		helpers._progress.finish()
		yield PreparedSource(config, connector, output, helpers, query)


class PreparedSource:
	"""A process-local staged source. Reuse always revalidates its dependencies."""

	def __init__(self, config, connector, output, helpers, query):
		self.config_digest = fingerprint(asdict(config))
		self.connector_digest = fingerprint(getattr(connector, "config", {}))
		self.output, self.helpers, self.query = output, helpers, query

	def __iter__(self):
		self.output.seek(0)
		return (json.loads(line) for line in self.output)

	def verify(self, config, connector):
		if self.output.closed or self.config_digest != fingerprint(asdict(config)):
			raise SourceChanged("Prepared source configuration changed.")
		if self.connector_digest != fingerprint(getattr(connector, "config", {})):
			raise SourceChanged("Prepared source connection changed.")
		query = query_templates.resolve_read_query(config, connector)
		if not query:
			query = f"SELECT * FROM {connector.quote_identifier(config.table_name)}"
		if query != self.query:
			raise SourceChanged("Prepared source query changed.")
		self.helpers._connector = connector
		self.helpers._progress = Progress(config.name)
		self.helpers._progress.report("revalidate prepared source", self.helpers.count)
		with self.helpers._progress.heartbeat(), self.helpers._progress.measure("verify"):
			self.helpers._verify()
		self.helpers._progress.finish()


@contextmanager
def reuse_prepared_source(source):
	"""Offer a snapshot to an immediately following run in the same process."""
	if not isinstance(source, PreparedSource):
		raise TypeError("Expected a prepared source")
	token = _reusable_source.set(source)
	try:
		yield
	finally:
		_reusable_source.reset(token)


@contextmanager
def prepare_source(config, connector):
	prepared = _reusable_source.get()
	if prepared is not None:
		try:
			prepared.verify(config, connector)
		except SourceChanged:
			pass
		else:
			yield iter(prepared)
			return
	with prepare_source_snapshot(config, connector) as source:
		yield iter(source)


def record_key(config, record):
	key = matching._key_tuple_from_partner(record, config.match_fields, config.mapping)
	return _encode_record_key(key)


class RecordKeyResolver:
	"""Resolve immutable field mappings once, retaining canonical key normalization."""

	def __init__(self, config):
		self.fields = tuple(
			mapping_rules._partner_field_for_mapping(config.mapping, field, field)
			for field in config.match_fields
		)

	def __call__(self, record):
		return _encode_record_key(
			matching._normalize_pairing_key_tuple(record.get(field) for field in self.fields)
		)


def _encode_record_key(key):
	if not matching._valid_key(key):
		raise frappe.ValidationError(_("Partner record has incomplete key fields."))
	return encode(key)
