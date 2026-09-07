from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint

from sync.sync.constants import FRAPPE_SOURCE_MODE_PYTHON_SCRIPT

try:
	from jinja2 import StrictUndefined
	from jinja2.exceptions import TemplateError
	from jinja2.sandbox import SandboxedEnvironment
except Exception:  # pragma: no cover - Frappe depends on Jinja, but keep import-time safe
	StrictUndefined = None
	TemplateError = Exception
	SandboxedEnvironment = None

from sync.sync.service import changes as changes_service
from sync.sync.service import config_access as config_access_service
from sync.sync.service import configuration as configuration_service
from sync.sync.service import mapping_rules as mapping_rules_service
from sync.sync.service import matching as matching_service
from sync.sync.service import metadata as metadata_service
from sync.sync.service import query_templates as query_templates_service
from sync.sync.service import time_utils as time_utils_service
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	SYNC_TYPE_PARTNER_TO_FRAPPE,
	SyncComputedFieldConfig,
	SyncContext,
	SyncDefinitionConfig,
)


def _get_frappe_source_records(
	config: SyncDefinitionConfig,
	context: SyncContext,
	*,
	apply_delta_filter: bool = True,
	use_script_source: bool = True,
) -> list[dict[str, Any]]:
	return [
		record
		for batch in _iter_frappe_source_batches(
			config,
			context,
			apply_delta_filter=apply_delta_filter,
			use_script_source=use_script_source,
		)
		for record in batch
	]


def _iter_frappe_source_batches(
	config: SyncDefinitionConfig,
	context: SyncContext,
	*,
	apply_delta_filter: bool = True,
	use_script_source: bool = True,
):
	if (
		use_script_source
		and config_access_service._config_frappe_source_mode(config) == FRAPPE_SOURCE_MODE_PYTHON_SCRIPT
	):
		return _iter_frappe_source_script_batches(config, context)
	return _iter_frappe_doctype_source_batches(config, context, apply_delta_filter=apply_delta_filter)


def _iter_frappe_doctype_source_batches(
	config: SyncDefinitionConfig,
	context: SyncContext,
	*,
	apply_delta_filter: bool = True,
):
	doctype_fieldnames = metadata_service._doctype_fieldnames(config.doctype)
	computed_fieldnames = configuration_service._computed_field_names(config)
	fields = sorted(
		mapping_rules_service._parent_mapping_fields_for_sync_type(config.mapping, config.sync_type)
		| set(config_access_service._config_match_fields(config))
		| configuration_service._computed_required_source_fields(config)
		| {
			config_access_service._config_frappe_modified_field(config),
			config_access_service._config_frappe_creation_field(config),
		}
		| {"name", "modified"}
		| (
			{config_access_service._config_frappe_partner_identity_field(config)}
			if config_access_service._config_frappe_partner_identity_field(config)
			else set()
		)
	)
	fields = [field for field in fields if field not in computed_fieldnames]
	if doctype_fieldnames is None:
		valid_fields = [
			field for field in fields if metadata_service._doctype_has_field(config.doctype, field)
		]
	else:
		valid_fields = [field for field in fields if field in doctype_fieldnames]
	or_filters = None
	if apply_delta_filter and context.is_delta_sync:
		since = context.delta_since
		or_filters = []
		for timestamp_field in (
			config_access_service._config_frappe_modified_field(config),
			config_access_service._config_frappe_creation_field(config),
		):
			if (
				(timestamp_field in doctype_fieldnames)
				if doctype_fieldnames is not None
				else metadata_service._doctype_has_field(config.doctype, timestamp_field)
			):
				or_filters.append([timestamp_field, ">=", since])
	if not valid_fields:
		valid_fields = ["name", "modified"]
	record_batches = _iter_frappe_record_batches(
		doctype=config.doctype,
		fields=valid_fields,
		filters=config.filters,
		or_filters=or_filters,
		batch_size=config.batch_size,
	)
	record_batches = _with_configured_child_rows(config, record_batches)
	record_batches = _with_computed_fields(config, record_batches)
	if not apply_delta_filter or not context.is_delta_sync:
		return record_batches

	def _filtered_batches():
		for batch in record_batches:
			filtered = [
				record
				for record in batch
				if changes_service._record_changed_since(
					record,
					config_access_service._config_frappe_modified_field(config),
					context.delta_since,
					creation_field=config_access_service._config_frappe_creation_field(config),
					target_time_zone=time_utils_service._site_time_zone(),
				)
			]
			if filtered:
				yield filtered

	return _filtered_batches()


def _load_frappe_match_candidates(
	config: SyncDefinitionConfig,
	partner_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	"""Load only Frappe rows that can match the current partner batch."""
	if not partner_records:
		return []

	fields = _frappe_match_candidate_fields(config)
	match_conditions: list[list[Any]] = []
	for index, frappe_field in enumerate(config_access_service._config_match_fields(config)):
		values = _unique_filter_values(
			matching_service._raw_key_tuple_from_partner(
				record, config_access_service._config_match_fields(config), config.mapping
			)[index]
			for record in partner_records
		)
		if values:
			match_conditions.append([frappe_field, "in", values])

	candidates: list[dict[str, Any]] = []
	if len(match_conditions) == len(config_access_service._config_match_fields(config)) and match_conditions:
		candidates.extend(
			record
			for batch in _iter_frappe_record_batches(
				doctype=config.doctype,
				fields=fields,
				filters=_filters_with_conditions(config.filters, match_conditions),
				or_filters=None,
				batch_size=config.batch_size,
			)
			for record in batch
		)

	frappe_identity_field = config_access_service._config_frappe_partner_identity_field(config)
	partner_identity_field = config_access_service._config_partner_identity_field(config)
	if frappe_identity_field and partner_identity_field:
		identity_values = _unique_filter_values(
			record.get(partner_identity_field) for record in partner_records
		)
		if identity_values:
			candidates.extend(
				record
				for batch in _iter_frappe_record_batches(
					doctype=config.doctype,
					fields=fields,
					filters=_filters_with_conditions(
						config.filters,
						[[frappe_identity_field, "in", identity_values]],
					),
					or_filters=None,
					batch_size=config.batch_size,
				)
				for record in batch
			)

	deduplicated: dict[str, dict[str, Any]] = {}
	for record in candidates:
		name = values_service._clean_string(record.get("name"))
		if name:
			deduplicated[name] = record
	result = list(deduplicated.values())
	if config_access_service._update_existing_enabled(config):
		for record in result:
			_enrich_record_with_child_rows(
				config.doctype,
				record,
				mapping_rules_service._child_table_fields_for_mapping(
					config.mapping, SYNC_TYPE_PARTNER_TO_FRAPPE
				),
			)
	return result


def _frappe_match_candidate_fields(config: SyncDefinitionConfig) -> list[str]:
	fields = {
		"name",
		"modified",
		config_access_service._config_frappe_modified_field(config),
		config_access_service._config_frappe_creation_field(config),
		*config_access_service._config_match_fields(config),
	}
	frappe_identity_field = config_access_service._config_frappe_partner_identity_field(config)
	if frappe_identity_field:
		fields.add(frappe_identity_field)
	if config_access_service._update_existing_enabled(config):
		fields.update(
			mapping_rules_service._parent_mapping_fields_for_sync_type(
				config.mapping, SYNC_TYPE_PARTNER_TO_FRAPPE
			)
		)

	doctype_fieldnames = metadata_service._doctype_fieldnames(config.doctype)
	if doctype_fieldnames is not None:
		return sorted(field for field in fields if field in doctype_fieldnames)
	return sorted(field for field in fields if metadata_service._doctype_has_field(config.doctype, field))


def _unique_filter_values(values: Any) -> list[Any]:
	result: list[Any] = []
	seen: set[Any] = set()
	for value in values:
		if value in (None, ""):
			continue
		try:
			key = matching_service._normalize_pairing_key_value(value)
			if key in seen:
				continue
			seen.add(key)
		except TypeError:
			if value in result:
				continue
		result.append(value)
	return result


def _filters_with_conditions(filters: list | dict | None, conditions: list[list[Any]]) -> list[list[Any]]:
	if isinstance(filters, dict):
		return [*_dict_filters_as_list(filters), *conditions]
	if isinstance(filters, list):
		return [*filters, *conditions]
	return list(conditions)


def _iter_frappe_source_script_batches(config: SyncDefinitionConfig, context: SyncContext):
	records = _execute_frappe_source_script(config, context)
	batch_size = cint(getattr(config, "batch_size", 100)) or 100
	for start in range(0, len(records), batch_size):
		yield records[start : start + batch_size]


def _execute_frappe_source_script(config: SyncDefinitionConfig, context: SyncContext) -> list[dict[str, Any]]:
	from frappe.utils.safe_exec import safe_exec

	helper_messages: list[str] = []
	helpers = _FrappeSourceScriptHelpers(helper_messages)
	script_context = {
		"doctype": config.doctype,
		"sync_definition": config.name,
		"sync_type": config.sync_type,
		"dry_run": bool(context.dry_run),
		"last_successful_sync": context.last_successful_sync,
		"delta_since": context.delta_since,
		"is_delta_sync": context.is_delta_sync,
		"batch_size": config.batch_size,
		"filters": config.filters,
		"helpers": helpers,
		"records": None,
	}
	_globals, locals_ = safe_exec(
		getattr(config, "frappe_source_script", None) or "",
		_globals=script_context,
		_locals=script_context,
		restrict_commit_rollback=True,
		script_filename=f"sync_frappe_source_{config.name}",
	)
	records = (locals_ or {}).get("records")
	if records is None:
		records = (_globals or {}).get("records")
	return _normalize_frappe_source_script_records(records)


def _normalize_frappe_source_script_records(records: Any) -> list[dict[str, Any]]:
	if records is None:
		raise frappe.ValidationError("Frappe Source Script must set records.")
	if not isinstance(records, list | tuple):
		raise frappe.ValidationError("Frappe Source Script records must be a list.")
	normalized: list[dict[str, Any]] = []
	for idx, record in enumerate(records, start=1):
		if not isinstance(record, dict):
			raise frappe.ValidationError(f"Frappe Source Script record {idx} must be a dictionary.")
		normalized.append(dict(record))
	return normalized


class _FrappeSourceScriptHelpers:
	def __init__(self, messages: list[str]):
		self._messages = messages

	def get_all(self, doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
		return frappe.get_all(doctype, **kwargs)

	def get_doc(self, doctype: str, name: str) -> Any:
		return frappe.get_doc(doctype, name)

	def db_get_value(self, doctype: str, filters: Any, fieldname: str) -> Any:
		return frappe.db.get_value(doctype, filters, fieldname)

	def log(self, message: Any) -> None:
		text = values_service._clean_string(message)
		if text:
			self._messages.append(text)

	def to_json(self, value: Any) -> str:
		return json.dumps(value, default=str, ensure_ascii=True)


def _with_computed_fields(
	config: SyncDefinitionConfig,
	record_batches: Any,
):
	if not getattr(config, "computed_fields", None):
		return record_batches

	def _computed_batches():
		for batch in record_batches:
			for record in batch:
				_apply_computed_fields(config, record)
			yield batch

	return _computed_batches()


def _apply_computed_fields(config: SyncDefinitionConfig | Any, record: dict[str, Any]) -> dict[str, Any]:
	for field in configuration_service._normalize_computed_fields(getattr(config, "computed_fields", None)):
		record[field.field_name] = _render_computed_field(field, record)
	return record


def _render_computed_field(field: SyncComputedFieldConfig, record: dict[str, Any]) -> str:
	if SandboxedEnvironment is None:
		raise frappe.ValidationError("Computed Field templates require Jinja.")
	try:
		environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
		template = environment.from_string(field.template)
		return template.render(doc=record)
	except TemplateError as exc:
		raise frappe.ValidationError(f"Computed Field {field.field_name} rendering failed: {exc}") from exc


def _with_configured_child_rows(
	config: SyncDefinitionConfig,
	record_batches: Any,
):
	table_fields = mapping_rules_service._child_table_fields_for_mapping(config.mapping, config.sync_type)
	if not table_fields:
		return record_batches

	def _enriched_batches():
		for batch in record_batches:
			for record in batch:
				_enrich_record_with_child_rows(config.doctype, record, table_fields)
			yield batch

	return _enriched_batches()


def _enrich_record_with_child_rows(doctype: str, record: dict[str, Any], table_fields: set[str]) -> None:
	name = record.get("name")
	if not name:
		return
	try:
		doc = frappe.get_doc(doctype, name)
	except Exception:
		return
	for table_field in table_fields:
		rows = []
		for row in getattr(doc, table_field, None) or []:
			if hasattr(row, "as_dict"):
				rows.append(row.as_dict())
			elif isinstance(row, dict):
				rows.append(dict(row))
			else:
				rows.append({key: value for key, value in vars(row).items() if not key.startswith("_")})
		record[table_field] = rows


def _get_partner_source_records(
	config: SyncDefinitionConfig,
	connector: Any,
	context: SyncContext,
	*,
	apply_delta_filter: bool = True,
) -> list[dict[str, Any]]:
	return [
		record
		for batch in _iter_partner_source_batches(
			config, connector, context, apply_delta_filter=apply_delta_filter
		)
		for record in batch
	]


def _iter_partner_source_batches(
	config: SyncDefinitionConfig,
	connector: Any,
	context: SyncContext,
	*,
	apply_delta_filter: bool = True,
):
	record_batches = _iter_partner_record_batches(
		connector=connector,
		source=config.table_name,
		query=query_templates_service.resolve_read_query(
			config, connector, context=_read_query_runtime_context(context)
		),
		batch_size=config.batch_size,
		key_fields=matching_service._partner_fetch_key_fields(config),
	)
	if not apply_delta_filter or not context.is_delta_sync:
		return record_batches
	since = context.delta_since

	def _filtered_batches():
		for batch in record_batches:
			filtered = [
				record
				for record in batch
				if changes_service._record_changed_since(
					record,
					config_access_service._config_partner_modified_field(config),
					since,
					creation_field=config_access_service._config_partner_creation_field(config),
					assumed_time_zone=getattr(config, "partner_time_zone", None),
					target_time_zone=time_utils_service._site_time_zone(),
				)
			]
			if filtered:
				yield filtered

	return _filtered_batches()


def _read_query_runtime_context(context: SyncContext) -> dict[str, str | None]:
	delta_since = getattr(context, "delta_since", None)
	return {
		"delta_since": delta_since.isoformat(sep=" ") if delta_since else None,
		"delta_since_date": delta_since.date().isoformat() if delta_since else None,
	}


def _fetch_partner_records(
	*,
	connector: Any,
	source: str | None,
	query: str | None,
	batch_size: int,
	key_fields: list[str],
) -> list[dict[str, Any]]:
	return [
		record
		for batch in _iter_partner_record_batches(
			connector=connector,
			source=source,
			query=query,
			batch_size=batch_size,
			key_fields=key_fields,
		)
		for record in batch
	]


def _iter_partner_record_batches(
	*,
	connector: Any,
	source: str | None,
	query: str | None,
	batch_size: int,
	key_fields: list[str],
):
	batch_iterator = getattr(connector, "iter_record_batches", None)
	if callable(batch_iterator):
		processed_count = 0
		try:
			for batch in batch_iterator(
				source=source,
				query=query,
				batch_size=batch_size,
				key_fields=key_fields,
			):
				records = [dict(record) for record in batch if isinstance(record, dict)]
				if not records:
					continue
				processed_count += len(records)
				yield records
		except Exception as exc:
			raise RuntimeError(f"Partner source load failed after {processed_count} records.") from exc
		return

	cursor = None
	processed_count = 0
	for _ in range(10_000):
		try:
			page = connector.fetch_records(
				source=source,
				query=query,
				batch_size=batch_size,
				cursor=cursor,
				key_fields=key_fields,
			)
		except Exception as exc:
			raise RuntimeError(
				f"Partner source load failed at cursor {cursor!r} after {processed_count} records."
			) from exc

		records, next_cursor = _normalize_fetch_result(page)
		if not records:
			break
		processed_count += len(records)
		yield records
		if not next_cursor:
			break
		cursor = next_cursor


def _normalize_fetch_result(fetch_result: Any) -> tuple[list[dict[str, Any]], Any]:
	if fetch_result is None:
		return [], None
	if isinstance(fetch_result, list):
		return [dict(row) for row in fetch_result if isinstance(row, dict)], None

	records = getattr(fetch_result, "records", None)
	next_cursor = getattr(fetch_result, "next_cursor", None)
	if isinstance(fetch_result, dict):
		records = fetch_result.get("records", records)
		next_cursor = fetch_result.get("next_cursor", next_cursor)
	if not isinstance(records, list):
		return [], next_cursor
	return [dict(row) for row in records if isinstance(row, dict)], next_cursor


def _get_frappe_records(
	doctype: str,
	*,
	fields: list[str],
	filters: list | dict | None,
	or_filters: list | None,
	batch_size: int,
) -> list[dict[str, Any]]:
	return [
		record
		for batch in _iter_frappe_record_batches(
			doctype,
			fields=fields,
			filters=filters,
			or_filters=or_filters,
			batch_size=batch_size,
		)
		for record in batch
	]


def _iter_frappe_record_batches(
	doctype: str,
	*,
	fields: list[str],
	filters: list | dict | None,
	or_filters: list | None,
	batch_size: int,
):
	cursor: str | None = None
	while True:
		page = _get_frappe_keyset_page(
			doctype,
			fields=fields,
			filters=filters,
			or_filters=or_filters,
			batch_size=batch_size,
			cursor=cursor,
		)
		if not page:
			break
		yield page
		if len(page) < batch_size:
			break
		cursor = _frappe_cursor_tuple(page[-1])


def _frappe_cursor_tuple(record: dict[str, Any]) -> str:
	return str(record.get("name") or "")


def _get_frappe_keyset_page(
	doctype: str,
	*,
	fields: list[str],
	filters: list | dict | None,
	or_filters: list | None,
	batch_size: int,
	cursor: str | tuple[Any, ...] | None,
) -> list[dict[str, Any]]:
	return frappe.get_all(
		doctype,
		fields=fields,
		filters=_filters_with_frappe_cursor(filters, cursor),
		or_filters=or_filters,
		limit_page_length=batch_size,
		order_by="name asc",
	)


def _filters_with_frappe_cursor(
	filters: list | dict | None,
	cursor: str | tuple[Any, ...] | None,
) -> list | dict | None:
	if not cursor:
		return filters
	cursor_name = str(cursor[-1] if isinstance(cursor, tuple) else cursor)
	cursor_filter = ["name", ">", cursor_name]
	if filters is None:
		return [cursor_filter]
	if isinstance(filters, list):
		return [*filters, cursor_filter]
	if isinstance(filters, dict):
		return [*_dict_filters_as_list(filters), cursor_filter]
	return filters


def _dict_filters_as_list(filters: dict[str, Any]) -> list[list[Any]]:
	result = []
	for fieldname, value in filters.items():
		if isinstance(value, (list, tuple)) and len(value) >= 2:
			result.append([fieldname, *value])
		else:
			result.append([fieldname, "=", value])
	return result
