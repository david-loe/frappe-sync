from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import json
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe.utils import cint, get_datetime, get_system_timezone, now_datetime
import yaml

from .connectors import ConnectorCreateOptions, get_connector_for_partner

try:
	from croniter import croniter
except Exception:  # pragma: no cover - optional runtime dependency
	croniter = None


SYNC_DEFINITION = "Sync Definition"
SYNC_PARTNER = "Sync Partner"
SYNC_RUN = "Sync Run"
SYNC_RUN_ITEM = "Sync Run Item"

ACTIVE_RUN_STATUSES = {"Queued", "Running"}
DONE_RUN_STATUSES = {"Success", "Partial Error", "Needs Review", "Error", "Skipped"}
VALID_TRIGGER_TYPES = {"manual", "scheduler", "api"}

SYSTEM_KEYS = {
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

MAPPING_DIRECTION_BOTH = "Both"
MAPPING_DIRECTION_FRAPPE_TO_PARTNER = "Frappe to Partner"
MAPPING_DIRECTION_PARTNER_TO_FRAPPE = "Partner to Frappe"

DEFAULT_RUNTIME_COMMIT_BATCH = 50
RUN_DOC_PENDING_WRITES_ATTR = "_sync_pending_write_count"


@dataclass(slots=True)
class SyncDefinitionConfig:
	name: str
	doctype: str
	partner: str
	sync_type: str
	cron: str | None
	filters: list | dict | None
	batch_size: int
	create_new: bool
	delete_missing: bool
	use_last_sync_date: bool
	conflict_policy: str
	timestamp_buffer_seconds: int
	table_name: str | None
	read_query: str | None
	match_fields: list[str]
	mapping: dict[str, dict[str, str]]
	value_mapping: dict[str, dict[Any, Any]]
	frappe_modified_fields: list[str]
	partner_modified_fields: list[str]
	partner_identity_field: str | None = None
	frappe_partner_identity_field: str | None = None
	partner_frappe_identity_field: str | None = None
	partner_create_id_strategy: str = "payload"
	partner_create_id_source: str | None = None
	partner_create_id_scope_where: str | None = None
	partner_time_zone: str | None = None
	one_way_match_mode: str = "first_match"
	capture_audit_payloads: bool = False


@dataclass(slots=True)
class SyncStats:
	processed_count: int = 0
	success_count: int = 0
	created_count: int = 0
	updated_count: int = 0
	deleted_count: int = 0
	skipped_count: int = 0
	conflict_count: int = 0
	error_count: int = 0

	def register(self, action: str, status: str):
		self.processed_count += 1
		if action == "created":
			self.created_count += 1
		elif action == "updated":
			self.updated_count += 1
		elif action == "deleted":
			self.deleted_count += 1

		if status == "success":
			self.success_count += 1
		elif status == "error":
			self.error_count += 1
		elif status == "skipped":
			self.skipped_count += 1
		elif status == "conflict":
			self.conflict_count += 1

	def as_dict(self) -> dict[str, int]:
		return {
			"processed_count": self.processed_count,
			"success_count": self.success_count,
			"created_count": self.created_count,
			"updated_count": self.updated_count,
			"deleted_count": self.deleted_count,
			"skipped_count": self.skipped_count,
			"conflict_count": self.conflict_count,
			"error_count": self.error_count,
		}


@dataclass(slots=True)
class SyncContext:
	config: SyncDefinitionConfig
	dry_run: bool
	last_successful_sync: datetime | None

	@property
	def is_delta_sync(self) -> bool:
		return bool(self.config.use_last_sync_date and self.last_successful_sync)

	@property
	def delta_since(self) -> datetime | None:
		if not self.is_delta_sync:
			return None
		return self.last_successful_sync - timedelta(seconds=max(0, self.config.timestamp_buffer_seconds))

	@property
	def is_full_sync(self) -> bool:
		return not self.is_delta_sync


class SyncScheduler:
	@staticmethod
	def select_due_definitions(definitions: list[Any], now: datetime | None = None) -> list[Any]:
		now = now or now_datetime()
		result: list[Any] = []
		for definition in definitions:
			if not _as_bool(getattr(definition, "enabled", True)):
				continue
			next_run_at = getattr(definition, "next_run_at", None)
			if isinstance(next_run_at, datetime) and next_run_at <= now:
				result.append(definition)
		return result


class SyncRunTracker:
	def __init__(self):
		self._active_runs: set[str] = set()

	def start_run(self, sync_definition_name: str) -> None:
		if sync_definition_name in self._active_runs:
			raise RuntimeError(f"Sync Definition {sync_definition_name} is already running")
		self._active_runs.add(sync_definition_name)

	def finish_run(self, sync_definition_name: str) -> None:
		self._active_runs.discard(sync_definition_name)


class SyncPreviewService:
	@staticmethod
	def predict(sync_definition: Any, limit: int = 50) -> dict[str, Any]:
		return _build_preview(sync_definition, limit=limit)


def list_due_sync_definitions(now: datetime | None = None) -> list[str]:
	now = now or now_datetime()
	definitions = frappe.get_all(SYNC_DEFINITION, fields=["name", "enabled", "next_run_at", "frequency_cron"])
	due: list[str] = []
	for definition in definitions:
		if not _is_enabled(definition):
			continue

		next_run_at = _parse_datetime(definition.get("next_run_at"))
		if next_run_at and next_run_at <= now:
			due.append(str(definition.get("name")))
			continue

		cron_expr = definition.get("frequency_cron")
		if cron_expr and _is_due_by_cron(definition, str(cron_expr), now):
			due.append(str(definition.get("name")))
	return due


def run_due_sync_definitions(limit: int = 20, queue: bool = True) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for name in list_due_sync_definitions()[:limit]:
		results.append(enqueue_sync_definition(name, trigger="scheduler", queue=queue))
	return results


def run_due_sync_definitions_scheduled(limit: int = 20, queue: bool = True) -> list[dict[str, Any]]:
	frappe.set_user("Administrator")
	return run_due_sync_definitions(limit=limit, queue=queue)


def enqueue_sync_definition(
	sync_definition_name: str,
	*,
	trigger: str = "manual",
	queue: bool = True,
	dry_run: bool = False,
) -> dict[str, Any]:
	sync_definition_name = str(sync_definition_name)
	trigger = _normalize_trigger_type(trigger)
	lock_key = f"sync:lock:{sync_definition_name}"
	with _definition_lock(lock_key):
		if _has_active_run(sync_definition_name):
			return {"status": "already_running", "sync_definition": sync_definition_name}

		sync_definition = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
		run_doc = _create_run_doc(sync_definition, status="Queued", trigger=trigger, dry_run=dry_run)

	if not queue:
		return execute_sync_definition(
			sync_definition_name,
			trigger=trigger,
			dry_run=dry_run,
			run_name=run_doc.name,
		)

	job_id = f"sync:run:{sync_definition_name}:{frappe.generate_hash(length=8)}"
	_update_doc_fields(run_doc, {"status": "Queued", "job_id": job_id})
	frappe.enqueue(
		"sync.sync.service.runtime.run_sync_definition_job",
		queue="long",
		job_id=job_id,
		sync_definition_name=sync_definition_name,
		run_name=run_doc.name,
		trigger=trigger,
		dry_run=dry_run,
	)
	return {"status": "queued", "sync_definition": sync_definition_name, "run": run_doc.name, "job_id": job_id}


def run_sync_definition_job(
	sync_definition_name: str,
	run_name: str | None = None,
	trigger: str = "manual",
	dry_run: bool = False,
):
	return execute_sync_definition(sync_definition_name, trigger=trigger, dry_run=dry_run, run_name=run_name)


def execute_sync_definition(
	sync_definition_name: str,
	*,
	trigger: str = "manual",
	dry_run: bool = False,
	run_name: str | None = None,
) -> dict[str, Any]:
	sync_definition_name = str(sync_definition_name)
	trigger = _normalize_trigger_type(trigger)
	lock_key = f"sync:lock:{sync_definition_name}"
	with _definition_lock(lock_key):
		if run_name:
			run_doc = frappe.get_doc(SYNC_RUN, run_name)
		else:
			if _has_active_run(sync_definition_name):
				return {"status": "already_running", "sync_definition": sync_definition_name}
			sync_definition_doc = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
			run_doc = _create_run_doc(sync_definition_doc, status="Queued", trigger=trigger, dry_run=dry_run)

		sync_definition = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
		_update_doc_fields(run_doc, {"status": "Running", "started_at": now_datetime(), "trigger_type": trigger})

		try:
			config = _build_definition_config(sync_definition)
			last_successful_sync = _get_last_successful_sync(sync_definition_name)
			context = SyncContext(config=config, dry_run=dry_run, last_successful_sync=last_successful_sync)
			result_payload = _run_engine(sync_definition, run_doc, context=context)

			terminal_status = _terminal_status_for_result(result_payload)
			sync_stamp = now_datetime() if terminal_status == "Success" and not dry_run else None
			_update_doc_fields(
				run_doc,
				{
					"status": terminal_status,
					"finished_at": now_datetime(),
					"last_sync_at": sync_stamp,
					"summary": _format_run_summary(result_payload),
					"processed_count": result_payload.get("processed_count", 0),
					"success_count": result_payload.get("success_count", 0),
					"created_count": result_payload.get("created_count", 0),
					"updated_count": result_payload.get("updated_count", 0),
					"deleted_count": result_payload.get("deleted_count", 0),
					"skipped_count": result_payload.get("skipped_count", 0),
					"conflict_count": result_payload.get("conflict_count", 0),
					"error_count": result_payload.get("error_count", 0),
				},
				commit=False,
			)
			if not dry_run:
				_update_definition_runtime(
					sync_definition,
					last_run=run_doc.name,
					status=terminal_status,
					last_sync_at=sync_stamp,
					summary=_format_run_summary(result_payload),
					commit=False,
				)
			_set_next_run_at(sync_definition, config.cron, commit=False)
			frappe.db.commit()
			return {"status": _api_status_for_run_status(terminal_status), "run": run_doc.name, "result": result_payload}
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Sync execution failed for {sync_definition_name}")
			_update_doc_fields(
				run_doc,
				{
					"status": "Error",
					"finished_at": now_datetime(),
					"error_message": frappe.get_traceback(with_context=False),
				},
				commit=False,
			)
			if not dry_run:
				_update_definition_failure(
					sync_definition,
					last_run=run_doc.name,
					error_message=frappe.get_traceback(with_context=False),
					commit=False,
				)
			frappe.db.commit()
			raise


def test_sync_partner_connection(sync_partner_name: str) -> dict[str, Any]:
	partner_doc = frappe.get_doc(SYNC_PARTNER, sync_partner_name)
	connector = get_connector_for_partner(partner_doc)
	result = connector.ping()
	status = "ok" if result.ok else "error"
	_update_partner_connection_status(partner_doc, status=status, details=result.message)
	return {"status": status, "ok": result.ok, "message": result.message, "details": result.details}


def preview_sync_definition(sync_definition_name: str, limit: int = 50) -> dict[str, Any]:
	sync_definition = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
	return SyncPreviewService.predict(sync_definition, limit=limit)


def _build_preview(sync_definition: Any, *, limit: int) -> dict[str, Any]:
	config = _build_definition_config(sync_definition)
	mapping = _normalize_field_mapping(config.mapping)
	partner_doc = frappe.get_doc(SYNC_PARTNER, config.partner)
	config = _merge_partner_runtime_settings(config, partner_doc)
	connector = get_connector_for_partner(partner_doc)
	ping = connector.ping()

	fields = sorted(
		set(mapping.keys())
		| set(_config_match_fields(config))
		| {"name", "modified"}
		| ({_config_frappe_partner_identity_field(config)} if _config_frappe_partner_identity_field(config) else set())
	)
	filters = config.filters
	frappe_records = frappe.get_all(
		config.doctype,
		fields=[field for field in fields if _doctype_has_field(config.doctype, field)],
		filters=filters,
		limit_page_length=cint(limit),
		order_by="modified desc",
	)
	return {
		"sync_definition": config.name,
		"sync_type": config.sync_type,
		"partner": config.partner,
		"connector": type(connector).__name__,
		"partner_ping": {"ok": ping.ok, "message": ping.message, "details": ping.details},
		"frappe_records_sample_count": len(frappe_records),
		"frappe_records_sample": frappe_records,
		"mapping": mapping,
		"match_fields": _config_match_fields(config),
		"read_query": _config_read_query(config),
		"partner_identity_field": _config_partner_identity_field(config),
		"value_mapping_fields": sorted(config.value_mapping.keys()),
		"actions": [{"direction": config.sync_type, "result": "preview"}],
	}


def export_sync_definition_yaml(sync_definition_name: str) -> str:
	sync_definition_doc = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
	mask_credentials = _as_bool(_first_value(sync_definition_doc, ["export_mask_credentials"], default=1))
	config_doc = _sanitize_document_dict(sync_definition_doc.as_dict(), mask_credentials=mask_credentials)
	partner_name = _first_value(sync_definition_doc, ["partner"])

	payload: dict[str, Any] = {
		"version": 1,
		"exported_at": now_datetime().isoformat(),
		"sync_definition": config_doc,
	}
	if partner_name:
		partner_doc = frappe.get_doc(SYNC_PARTNER, partner_name)
		payload["sync_partner"] = _sanitize_document_dict(partner_doc.as_dict(), mask_credentials=mask_credentials)
		partner_type_name = _first_value(partner_doc, ["partner_type"])
		if partner_type_name and frappe.db.exists("Sync Partner Type", partner_type_name):
			partner_type_doc = frappe.get_doc("Sync Partner Type", partner_type_name)
			payload["sync_partner_type"] = _sanitize_document_dict(
				partner_type_doc.as_dict(),
				mask_credentials=mask_credentials,
			)

	return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)


def preview_import_sync_definition_yaml(yaml_payload: str, overwrite: bool = False) -> dict[str, Any]:
	try:
		data = yaml.safe_load(yaml_payload) or {}
	except yaml.YAMLError as exc:
		return {
			"ok": False,
			"can_import": False,
			"overwrite": _as_bool(overwrite),
			"error": f"Invalid YAML payload: {exc}",
			"missing_payload_parts": [],
			"documents": {},
			"summary": {
				"create": 0,
				"update": 0,
				"conflict": 0,
				"invalid": 1,
				"missing_payload": 0,
			},
		}

	if not isinstance(data, dict):
		return {
			"ok": False,
			"can_import": False,
			"overwrite": _as_bool(overwrite),
			"error": "YAML payload must decode to a mapping/object at the top level.",
			"missing_payload_parts": [],
			"documents": {},
			"summary": {
				"create": 0,
				"update": 0,
				"conflict": 0,
				"invalid": 1,
				"missing_payload": 0,
			},
		}

	overwrite = _as_bool(overwrite)
	missing_payload_parts: list[str] = []
	documents: dict[str, dict[str, Any]] = {}
	summary = {
		"create": 0,
		"update": 0,
		"conflict": 0,
		"invalid": 0,
		"missing_payload": 0,
	}

	for payload_key, doctype in (
		("sync_partner_type", "Sync Partner Type"),
		("sync_partner", SYNC_PARTNER),
		("sync_definition", SYNC_DEFINITION),
	):
		payload = data.get(payload_key)
		if payload is None:
			missing_payload_parts.append(payload_key)
			summary["missing_payload"] += 1
			documents[doctype] = {
				"payload_key": payload_key,
				"doctype": doctype,
				"name": None,
				"status": "missing_payload",
				"exists": False,
				"action": "skip",
				"hint": f"Payload section `{payload_key}` is missing.",
			}
			continue
		if not isinstance(payload, dict):
			summary["invalid"] += 1
			documents[doctype] = {
				"payload_key": payload_key,
				"doctype": doctype,
				"name": None,
				"status": "invalid",
				"exists": False,
				"action": "skip",
				"hint": f"Payload section `{payload_key}` must be a mapping/object.",
			}
			continue

		normalized = _normalize_doc_payload(doctype, payload)
		name = _first_value_dict(normalized, ["name"])
		if not name:
			summary["invalid"] += 1
			documents[doctype] = {
				"payload_key": payload_key,
				"doctype": doctype,
				"name": None,
				"status": "invalid",
				"exists": False,
				"action": "skip",
				"hint": f"Payload section `{payload_key}` is missing a document name.",
			}
			continue

		exists = bool(frappe.db.exists(doctype, name))
		if not exists:
			status = "create"
			action = "insert"
			hint = "Document does not exist and would be created."
		elif overwrite:
			status = "update"
			action = "overwrite"
			hint = "Document exists and would be updated because overwrite is enabled."
		else:
			status = "conflict"
			action = "keep_existing"
			hint = "Document exists already. Import without overwrite will keep the current document."
		summary[status] += 1
		documents[doctype] = {
			"payload_key": payload_key,
			"doctype": doctype,
			"name": str(name),
			"status": status,
			"exists": exists,
			"action": action,
			"hint": hint,
		}

	return {
		"ok": summary["invalid"] == 0,
		"can_import": summary["invalid"] == 0,
		"overwrite": overwrite,
		"missing_payload_parts": missing_payload_parts,
		"documents": documents,
		"summary": summary,
	}


def import_sync_definition_yaml(yaml_payload: str, overwrite: bool = False) -> dict[str, Any]:
	preview = preview_import_sync_definition_yaml(yaml_payload, overwrite=overwrite)
	if not preview.get("can_import"):
		raise frappe.ValidationError(preview.get("error") or "YAML payload cannot be imported.")
	data = yaml.safe_load(yaml_payload) or {}
	created_or_updated: dict[str, str] = {}
	for key, doctype in (
		("sync_partner_type", "Sync Partner Type"),
		("sync_partner", SYNC_PARTNER),
		("sync_definition", SYNC_DEFINITION),
	):
		if key not in data or not isinstance(data[key], dict):
			continue
		name = _upsert_document_from_payload(doctype, data[key], overwrite=overwrite)
		created_or_updated[doctype] = name
	if not created_or_updated:
		raise frappe.ValidationError("YAML payload contains no importable Sync documents.")
	return {"ok": True, "documents": created_or_updated}


def _run_engine(
	sync_definition_doc: Any,
	run_doc: Any,
	*,
	context: SyncContext | None = None,
	config: SyncDefinitionConfig | Any | None = None,
	dry_run: bool = False,
	last_successful_sync: datetime | None = None,
) -> dict[str, Any]:
	if context is None:
		if config is None:
			raise ValueError("Either context or config must be provided.")
		config_obj = _coerce_config(config)
		context = SyncContext(
			config=config_obj,
			dry_run=dry_run,
			last_successful_sync=last_successful_sync,
		)
	config = context.config
	partner_doc = frappe.get_doc(SYNC_PARTNER, config.partner)
	config = _merge_partner_runtime_settings(config, partner_doc)
	connector = get_connector_for_partner(partner_doc)
	ping = connector.ping()
	if not ping.ok:
		raise frappe.ValidationError(f"Partner connector validation failed: {ping.message}")

	stats = SyncStats()
	if config.sync_type == "A->B":
		partner_batches = _iter_partner_source_batches(config, connector, context)
		if config.delete_missing and context.is_full_sync:
			partner_records = [
				record
				for batch in partner_batches
				for record in batch
			]
		elif _config_one_way_match_mode(config) == "all_matches":
			partner_records = [
				record
				for batch in partner_batches
				for record in batch
			]
		else:
			partner_records = _build_partner_index_from_batches(config, partner_batches)
		if config.delete_missing and context.is_full_sync:
			frappe_source = [
				record
				for batch in _iter_frappe_source_batches(config, context)
				for record in batch
			]
			_sync_frappe_to_partner(
				run_doc=run_doc,
				config=config,
				connector=connector,
				frappe_records=frappe_source,
				partner_records=partner_records,
				dry_run=context.dry_run,
				stats=stats,
				label_direction="A->B",
				full_sync=True,
			)
		else:
			source_keys: set[tuple[Any, ...]] = set()
			for frappe_batch in _iter_frappe_source_batches(config, context):
				_sync_frappe_to_partner(
					run_doc=run_doc,
					config=config,
					connector=connector,
					frappe_records=frappe_batch,
					partner_records=partner_records,
					dry_run=context.dry_run,
					stats=stats,
					label_direction="A->B",
					full_sync=False,
					source_keys=source_keys,
				)
			_flush_pending_run_writes(run_doc, force=True)
	elif config.sync_type == "A<-B":
		frappe_records = _get_frappe_source_records(config, context)
		if config.delete_missing and context.is_full_sync:
			partner_source = [
				record
				for batch in _iter_partner_source_batches(config, connector, context)
				for record in batch
			]
			_sync_partner_to_frappe(
				run_doc=run_doc,
				config=config,
				connector=connector,
				partner_records=partner_source,
				frappe_records=frappe_records,
				dry_run=context.dry_run,
				stats=stats,
				label_direction="A<-B",
				full_sync=True,
			)
		else:
			source_keys: set[tuple[Any, ...]] = set()
			for partner_batch in _iter_partner_source_batches(config, connector, context):
				_sync_partner_to_frappe(
					run_doc=run_doc,
					config=config,
					connector=connector,
					partner_records=partner_batch,
					frappe_records=frappe_records,
					dry_run=context.dry_run,
					stats=stats,
					label_direction="A<-B",
					full_sync=False,
					source_keys=source_keys,
				)
			_flush_pending_run_writes(run_doc, force=True)
	else:
		frappe_index = _build_frappe_index_from_batches(
			config,
			_iter_frappe_source_batches(config, context),
		)
		partner_index = _build_partner_index_from_batches(
			config,
			_iter_partner_source_batches(config, connector, context),
		)
		_sync_bidirectional(
			run_doc=run_doc,
			config=config,
			connector=connector,
			frappe_records=frappe_index,
			partner_records=partner_index,
			dry_run=context.dry_run,
			stats=stats,
			last_successful_sync=context.last_successful_sync,
		)
	return {
		"sync_definition": config.name,
		"sync_type": config.sync_type,
		"last_successful_sync_before_run": context.last_successful_sync.isoformat()
		if context.last_successful_sync
		else None,
		"delta_since": context.delta_since.isoformat() if context.delta_since else None,
		"dry_run": context.dry_run,
		**stats.as_dict(),
	}


def _coerce_config(config: SyncDefinitionConfig | Any) -> SyncDefinitionConfig:
	if isinstance(config, SyncDefinitionConfig):
		mapping = _normalize_field_mapping(config.mapping)
		if mapping == config.mapping:
			normalized_config = replace(
				config,
				partner_time_zone=_normalize_time_zone_name(config.partner_time_zone),
			)
			_validate_runtime_mapping(normalized_config)
			return normalized_config
	normalized = SyncDefinitionConfig(
		name=str(getattr(config, "name", "")),
		doctype=str(getattr(config, "doctype", "")),
		partner=str(getattr(config, "partner", "")),
		sync_type=str(getattr(config, "sync_type", "A->B")),
		cron=getattr(config, "cron", None),
		filters=getattr(config, "filters", None),
		batch_size=cint(getattr(config, "batch_size", 100)) or 100,
		create_new=_as_bool(getattr(config, "create_new", 1)),
		delete_missing=_as_bool(getattr(config, "delete_missing", 0)),
		one_way_match_mode=_clean_string(getattr(config, "one_way_match_mode", None)) or "first_match",
		use_last_sync_date=_as_bool(getattr(config, "use_last_sync_date", 1)),
		conflict_policy=str(getattr(config, "conflict_policy", "newest_wins")),
		timestamp_buffer_seconds=cint(getattr(config, "timestamp_buffer_seconds", 15)) or 0,
		table_name=getattr(config, "table_name", None),
		read_query=getattr(config, "read_query", None),
		match_fields=list(getattr(config, "match_fields", []) or []),
		mapping=_normalize_field_mapping(getattr(config, "mapping", {}) or {}),
		value_mapping=dict(getattr(config, "value_mapping", {}) or {}),
		frappe_modified_fields=list(getattr(config, "frappe_modified_fields", ["modified"]) or ["modified"]),
		partner_modified_fields=list(getattr(config, "partner_modified_fields", ["modified"]) or ["modified"]),
		partner_identity_field=_clean_string(getattr(config, "partner_identity_field", None)),
		frappe_partner_identity_field=_clean_string(getattr(config, "frappe_partner_identity_field", None)),
		partner_frappe_identity_field=_clean_string(getattr(config, "partner_frappe_identity_field", None)),
		partner_create_id_strategy=_clean_string(getattr(config, "partner_create_id_strategy", None)) or "payload",
		partner_create_id_source=_clean_string(getattr(config, "partner_create_id_source", None)),
		partner_create_id_scope_where=_clean_string(getattr(config, "partner_create_id_scope_where", None)),
		partner_time_zone=_normalize_time_zone_name(getattr(config, "partner_time_zone", None)),
		capture_audit_payloads=_as_bool(getattr(config, "capture_audit_payloads", 0)),
	)
	_validate_runtime_mapping(normalized)
	return normalized


def _build_frappe_index_from_batches(
	config: SyncDefinitionConfig,
	record_batches: Any,
) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for batch in record_batches:
		index.update(_index_frappe_records(config, batch))
	return index


def _build_partner_index_from_batches(
	config: SyncDefinitionConfig,
	record_batches: Any,
) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for batch in record_batches:
		index.update(_index_partner_records(config, batch))
	return index


def _group_frappe_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
	iterable = records.values() if isinstance(records, dict) else records
	grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
	for record in iterable:
		key = _key_tuple_from_frappe(record, _config_match_fields(config))
		if _valid_key(key):
			grouped.setdefault(key, []).append(record)
	return grouped


def _group_partner_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
	iterable = records.values() if isinstance(records, dict) else records
	grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
	for record in iterable:
		key = _key_tuple_from_partner(record, _config_match_fields(config), config.mapping)
		if _valid_key(key):
			grouped.setdefault(key, []).append(record)
	return grouped


def _sync_frappe_to_partner(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	frappe_records: list[dict[str, Any]],
	partner_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
	full_sync: bool,
	source_keys: set[tuple[Any, ...]] | None = None,
):
	partner_lookup_records = _normalize_partner_match_records(config, partner_records)
	partner_groups = _group_partner_records(config, partner_lookup_records)
	partner_index = {key: records[-1] for key, records in partner_groups.items()}
	partner_identity_index = _build_partner_identity_index(config, partner_lookup_records)
	collected_source_keys = source_keys if source_keys is not None else set()
	connector_mapping = _flatten_mapping_for_direction(config.mapping, MAPPING_DIRECTION_FRAPPE_TO_PARTNER)

	for frappe_record in frappe_records:
		key = _key_tuple_from_frappe(frappe_record, _config_match_fields(config))
		if not _valid_key(key):
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="error",
				status="error",
				message="Record has incomplete key fields.",
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=None,
				commit=False,
			)
			continue

		collected_source_keys.add(key)
		partner_payload = _apply_partner_link_fields(
			config,
			frappe_record,
			_map_frappe_to_partner(
				frappe_record,
				config.mapping,
				config.value_mapping,
				doctype=getattr(config, "doctype", None),
				partner_time_zone=getattr(config, "partner_time_zone", None),
			),
		)
		existing_partners = _find_existing_partner_records(
			config,
			frappe_record,
			partner_groups,
			partner_identity_index,
		)
		existing_partner = existing_partners[-1] if existing_partners else None

		if not existing_partners and not config.create_new:
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="skipped",
				status="skipped",
				message="Create disabled and target record does not exist.",
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=existing_partner,
				commit=False,
			)
			continue

		if not existing_partners:
			try:
				write = connector.upsert_record(
					record=partner_payload,
					key_values=_partner_key_values_for_write(config, frappe_record, key),
					mapping=connector_mapping,
					dry_run=dry_run,
					source=config.table_name,
					create_options=_build_partner_create_options(config),
				)
				if not write.ok:
					raise RuntimeError(write.message or "Partner upsert failed.")
				_persist_frappe_partner_identity(config, frappe_record, write, dry_run=dry_run)
			except Exception as exc:
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=None,
					commit=False,
				)
				continue

			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="created",
				status="success",
				message="Dry run upsert." if dry_run else "Upserted partner record.",
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=getattr(write, "record", None) or partner_payload,
				changes=[],
				commit=False,
			)
			continue

		if len(existing_partners) > 1 and not _can_write_partner_matches_individually(config, existing_partners):
			change_sets = [
				_diff_target_values(
					new_record=partner_payload,
					old_record=matched_partner,
					field_names=list(partner_payload.keys()),
					datetime_fields=_partner_datetime_fields(config),
					assumed_time_zone=getattr(config, "partner_time_zone", None),
					target_time_zone=getattr(config, "partner_time_zone", None) or _site_time_zone(),
				)
				for matched_partner in existing_partners
			]
			if not any(change_sets):
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="skipped",
					status="skipped",
					message="No changes detected across matched partner records.",
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=existing_partner,
					commit=False,
				)
				continue
			try:
				write = connector.upsert_record(
					record=partner_payload,
					key_values=_partner_key_values_for_write(config, frappe_record, key),
					mapping=connector_mapping,
					dry_run=dry_run,
					source=config.table_name,
					create_options=_build_partner_create_options(config),
				)
				if not write.ok:
					raise RuntimeError(write.message or "Partner upsert failed.")
			except Exception as exc:
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=existing_partner,
					commit=False,
				)
				continue

			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="updated",
				status="success",
				message=(
					"Dry run upsert."
					if dry_run
					else f"Upserted {len(existing_partners)} matched partner records."
				),
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=getattr(write, "record", None) or existing_partner or partner_payload,
				changes=change_sets[-1],
				commit=False,
			)
			continue

		for matched_partner in existing_partners:
			changes = _diff_target_values(
				new_record=partner_payload,
				old_record=matched_partner or {},
				field_names=list(partner_payload.keys()),
				datetime_fields=_partner_datetime_fields(config),
				assumed_time_zone=getattr(config, "partner_time_zone", None),
				target_time_zone=getattr(config, "partner_time_zone", None) or _site_time_zone(),
			)
			if not changes:
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="skipped",
					status="skipped",
					message="No changes detected.",
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=matched_partner,
					commit=False,
				)
				continue

			try:
				write = connector.upsert_record(
					record=partner_payload,
					key_values=_partner_key_values_for_existing_match(config, frappe_record, key, matched_partner),
					mapping=connector_mapping,
					dry_run=dry_run,
					source=config.table_name,
					create_options=_build_partner_create_options(config),
				)
				if not write.ok:
					raise RuntimeError(write.message or "Partner upsert failed.")
				if len(existing_partners) == 1:
					_persist_frappe_partner_identity(config, frappe_record, write, dry_run=dry_run)
			except Exception as exc:
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=matched_partner,
					commit=False,
				)
				continue

			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="updated",
				status="success",
				message="Dry run upsert." if dry_run else "Upserted partner record.",
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=getattr(write, "record", None) or partner_payload,
				changes=changes,
				commit=False,
			)

	if config.delete_missing and full_sync:
		_delete_missing_partner_records(
			run_doc=run_doc,
			config=config,
			connector=connector,
			partner_index=partner_index,
			source_keys=collected_source_keys,
			dry_run=dry_run,
			stats=stats,
			label_direction=label_direction,
		)
	_flush_pending_run_writes(run_doc)
	return collected_source_keys


def _sync_partner_to_frappe(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	partner_records: list[dict[str, Any]],
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
	full_sync: bool,
	source_keys: set[tuple[Any, ...]] | None = None,
):
	frappe_lookup_records = _normalize_frappe_match_records(config, frappe_records)
	partner_input_records = _normalize_partner_match_records(config, partner_records)
	frappe_groups = _group_frappe_records(config, frappe_lookup_records)
	frappe_index = {key: records[-1] for key, records in frappe_groups.items()}
	frappe_partner_identity_index = _build_frappe_partner_identity_index(config, frappe_lookup_records)
	collected_source_keys = source_keys if source_keys is not None else set()

	for partner_record in partner_input_records.values() if isinstance(partner_input_records, dict) else partner_input_records:
		key = _key_tuple_from_partner(partner_record, _config_match_fields(config), config.mapping)
		if not _valid_key(key):
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="error",
				status="error",
				message="Partner record has incomplete key fields.",
				direction=label_direction,
				frappe_record=None,
				partner_record=partner_record,
				commit=False,
			)
			continue

		collected_source_keys.add(key)
		frappe_payload = _map_partner_to_frappe(
			partner_record,
			config.mapping,
			config.value_mapping,
			doctype=getattr(config, "doctype", None),
			partner_time_zone=getattr(config, "partner_time_zone", None),
		)
		frappe_partner_field = _config_frappe_partner_identity_field(config)
		partner_identity_field = _config_partner_identity_field(config)
		if frappe_partner_field and partner_identity_field:
			partner_id = partner_record.get(partner_identity_field)
			if partner_id not in (None, ""):
				frappe_payload[frappe_partner_field] = partner_id
		existing_frappe_records = _find_existing_frappe_records(
			config,
			partner_record,
			frappe_groups,
			frappe_partner_identity_index,
		)
		existing_frappe = existing_frappe_records[-1] if existing_frappe_records else None

		if not existing_frappe_records and not config.create_new:
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="skipped",
				status="skipped",
				message="Create disabled and target record does not exist.",
				direction=label_direction,
				frappe_record=existing_frappe,
				partner_record=partner_record,
				commit=False,
			)
			continue

		if not existing_frappe_records:
			try:
				doc_name = _upsert_frappe_record(
					doctype=config.doctype,
					existing_name=None,
					payload=frappe_payload,
					dry_run=dry_run,
				)
				if doc_name:
					frappe_payload["name"] = doc_name
			except Exception as exc:
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=None,
					partner_record=partner_record,
					commit=False,
				)
				continue

			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="created",
				status="success",
				message="Dry run upsert." if dry_run else "Upserted frappe record.",
				direction=label_direction,
				frappe_record=frappe_payload,
				partner_record=partner_record,
				changes=[],
				commit=False,
			)
			continue

		for matched_frappe in existing_frappe_records:
			changes = _diff_target_values(
				new_record=frappe_payload,
				old_record=matched_frappe or {},
				field_names=list(frappe_payload.keys()),
				datetime_fields=_frappe_datetime_fields(config),
				target_time_zone=_site_time_zone(),
			)
			if not changes:
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="skipped",
					status="skipped",
					message="No changes detected.",
					direction=label_direction,
					frappe_record=matched_frappe,
					partner_record=partner_record,
					commit=False,
				)
				continue

			try:
				target_payload = dict(frappe_payload)
				target_payload["name"] = matched_frappe.get("name")
				doc_name = _upsert_frappe_record(
					doctype=config.doctype,
					existing_name=matched_frappe.get("name"),
					payload=target_payload,
					dry_run=dry_run,
				)
				if doc_name:
					target_payload["name"] = doc_name
			except Exception as exc:
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=matched_frappe,
					partner_record=partner_record,
					commit=False,
				)
				continue

			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="updated",
				status="success",
				message="Dry run upsert." if dry_run else "Upserted frappe record.",
				direction=label_direction,
				frappe_record=target_payload,
				partner_record=partner_record,
				changes=changes,
				commit=False,
			)

	if config.delete_missing and full_sync:
		_delete_missing_frappe_records(
			run_doc=run_doc,
			config=config,
			frappe_records=frappe_records,
			source_keys=collected_source_keys,
			dry_run=dry_run,
			stats=stats,
			label_direction=label_direction,
		)
	_flush_pending_run_writes(run_doc)
	return collected_source_keys


def _delete_missing_partner_records(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	partner_index: dict[tuple[Any, ...], dict[str, Any]],
	source_keys: set[tuple[Any, ...]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
):
	for key, partner_record in partner_index.items():
		if key in source_keys:
			continue
		key_values = _partner_key_values_from_tuple(config, key)
		try:
			write = connector.delete_record(
				key_values=key_values,
				dry_run=dry_run,
				source=config.table_name,
			)
			if not write.ok:
				raise RuntimeError(write.message or "Partner delete failed.")
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="deleted",
				status="success",
				message="Dry run delete." if dry_run else "Deleted partner record missing in source.",
				direction=label_direction,
				frappe_record=None,
				partner_record=partner_record,
				commit=False,
			)
		except Exception as exc:
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="error",
				status="error",
				message=str(exc),
				direction=label_direction,
				frappe_record=None,
				partner_record=partner_record,
				commit=False,
			)


def _delete_missing_frappe_records(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	source_keys: set[tuple[Any, ...]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
):
	frappe_groups = _group_frappe_records(config, frappe_records)
	for key, matched_frappe_records in frappe_groups.items():
		if key in source_keys:
			continue
		for frappe_record in matched_frappe_records:
			try:
				if not dry_run:
					frappe.delete_doc(config.doctype, frappe_record["name"], ignore_permissions=True, force=True)
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="deleted",
					status="success",
					message="Dry run delete." if dry_run else "Deleted frappe record missing in source.",
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=None,
					commit=False,
				)
			except Exception as exc:
				_register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=None,
					commit=False,
				)


def _sync_bidirectional(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	partner_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	last_successful_sync: datetime | None,
):
	frappe_index = _index_paired_frappe_records(config, frappe_records)
	partner_index = _index_paired_partner_records(config, partner_records)
	all_keys = set(frappe_index.keys()) | set(partner_index.keys())

	for key in sorted(all_keys, key=lambda item: json.dumps(item, default=str, ensure_ascii=True)):
		frappe_record = frappe_index.get(key)
		partner_record = partner_index.get(key)

		if frappe_record and not partner_record:
			_sync_frappe_to_partner(
				run_doc=run_doc,
				config=config,
				connector=connector,
				frappe_records=[frappe_record],
				partner_records=[],
				dry_run=dry_run,
				stats=stats,
				label_direction="A<->B",
				full_sync=False,
			)
			continue

		if partner_record and not frappe_record:
			_sync_partner_to_frappe(
				run_doc=run_doc,
				config=config,
				connector=connector,
				partner_records=[partner_record],
				frappe_records=[],
				dry_run=dry_run,
				stats=stats,
				label_direction="A<->B",
				full_sync=False,
			)
			continue

		if not frappe_record or not partner_record:
			continue

		frappe_payload = _map_partner_to_frappe(
			partner_record,
			config.mapping,
			config.value_mapping,
			doctype=getattr(config, "doctype", None),
			partner_time_zone=getattr(config, "partner_time_zone", None),
		)
		partner_payload = _map_frappe_to_partner(
			frappe_record,
			config.mapping,
			config.value_mapping,
			doctype=getattr(config, "doctype", None),
			partner_time_zone=getattr(config, "partner_time_zone", None),
		)

		to_partner_changes = _diff_target_values(
			new_record=partner_payload,
			old_record=partner_record,
			field_names=list(partner_payload.keys()),
			datetime_fields=_partner_datetime_fields(config),
			assumed_time_zone=getattr(config, "partner_time_zone", None),
			target_time_zone=getattr(config, "partner_time_zone", None) or _site_time_zone(),
		)
		to_frappe_changes = _diff_target_values(
			new_record=frappe_payload,
			old_record=frappe_record,
			field_names=list(frappe_payload.keys()),
			datetime_fields=_frappe_datetime_fields(config),
			target_time_zone=_site_time_zone(),
		)
		if not to_partner_changes and not to_frappe_changes:
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="skipped",
				status="skipped",
				message="No differences between both sides.",
				direction="A<->B",
				frappe_record=frappe_record,
				partner_record=partner_record,
				commit=False,
			)
			continue

		frappe_changed_since_last = _record_changed_since(
			record=frappe_record,
			modified_fields=config.frappe_modified_fields,
			last_successful_sync=last_successful_sync,
			target_time_zone=_site_time_zone(),
		)
		partner_changed_since_last = _record_changed_since(
			record=partner_record,
			modified_fields=config.partner_modified_fields,
			last_successful_sync=last_successful_sync,
			assumed_time_zone=getattr(config, "partner_time_zone", None),
			target_time_zone=_site_time_zone(),
		)

		if frappe_changed_since_last and not partner_changed_since_last:
			_apply_partner_update(
				run_doc=run_doc,
				config=config,
				connector=connector,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				partner_payload=partner_payload,
				changes=to_partner_changes,
				direction="A<->B",
				action="updated",
				status="success",
				message="Updated partner from frappe.",
				commit=False,
			)
			continue

		if partner_changed_since_last and not frappe_changed_since_last:
			_apply_frappe_update(
				run_doc=run_doc,
				config=config,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				changes=to_frappe_changes,
				direction="A<->B",
				action="updated",
				status="success",
				message="Updated frappe from partner.",
				commit=False,
			)
			continue

		if config.conflict_policy != "newest_wins":
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="conflict",
				status="conflict",
				message=f"Unsupported conflict policy: {config.conflict_policy}",
				direction="A<->B",
				frappe_record=frappe_record,
				partner_record=partner_record,
				commit=False,
			)
			continue

		frappe_latest = _latest_modified(
			record=frappe_record,
			modified_fields=config.frappe_modified_fields,
			target_time_zone=_site_time_zone(),
		)
		partner_latest = _latest_modified(
			record=partner_record,
			modified_fields=config.partner_modified_fields,
			assumed_time_zone=getattr(config, "partner_time_zone", None),
			target_time_zone=_site_time_zone(),
		)
		if partner_latest and frappe_latest and partner_latest > frappe_latest:
			_apply_frappe_update(
				run_doc=run_doc,
				config=config,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				changes=to_frappe_changes,
				direction="A<->B",
				action="conflict",
				status="conflict",
				message="Conflict resolved with newest_wins: partner won.",
				commit=False,
			)
		else:
			_apply_partner_update(
				run_doc=run_doc,
				config=config,
				connector=connector,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				partner_payload=partner_payload,
				changes=to_partner_changes,
				direction="A<->B",
				action="conflict",
				status="conflict",
				message="Conflict resolved with newest_wins: frappe won.",
				commit=False,
			)
	_flush_pending_run_writes(run_doc)


def _apply_partner_update(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	stats: SyncStats,
	dry_run: bool,
	frappe_record: dict[str, Any],
	partner_record: dict[str, Any],
	partner_payload: dict[str, Any],
	changes: list[tuple[str, Any, Any]],
	direction: str,
	action: str,
	status: str,
	message: str,
	commit: bool = True,
):
	key = _key_tuple_from_frappe(frappe_record, _config_match_fields(config))
	partner_payload = _apply_partner_link_fields(config, frappe_record, partner_payload)
	connector_mapping = _flatten_mapping_for_direction(config.mapping, MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	try:
		write = connector.upsert_record(
			record=partner_payload,
			key_values=_partner_key_values_for_write(config, frappe_record, key),
			mapping=connector_mapping,
			dry_run=dry_run,
			source=config.table_name,
			create_options=_build_partner_create_options(config),
		)
		if not write.ok:
			raise RuntimeError(write.message or "Partner upsert failed.")
		_persist_frappe_partner_identity(config, frappe_record, write, dry_run=dry_run)
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action=action,
			status=status,
			message=("Dry run update." if dry_run else message),
			direction=direction,
			frappe_record=frappe_record,
			partner_record=getattr(write, "record", None) or partner_payload,
			changes=changes,
			commit=commit,
		)
	except Exception as exc:
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="error",
			status="error",
			message=str(exc),
			direction=direction,
			frappe_record=frappe_record,
			partner_record=partner_record,
			commit=commit,
		)


def _apply_frappe_update(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	stats: SyncStats,
	dry_run: bool,
	frappe_record: dict[str, Any],
	partner_record: dict[str, Any],
	frappe_payload: dict[str, Any],
	changes: list[tuple[str, Any, Any]],
	direction: str,
	action: str,
	status: str,
	message: str,
	commit: bool = True,
):
	try:
		doc_name = _upsert_frappe_record(
			doctype=config.doctype,
			existing_name=(frappe_record or {}).get("name"),
			payload=frappe_payload,
			dry_run=dry_run,
		)
		if doc_name:
			frappe_payload["name"] = doc_name
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action=action,
			status=status,
			message=("Dry run update." if dry_run else message),
			direction=direction,
			frappe_record=frappe_payload,
			partner_record=partner_record,
			changes=changes,
			commit=commit,
		)
	except Exception as exc:
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="error",
			status="error",
			message=str(exc),
			direction=direction,
			frappe_record=frappe_record,
			partner_record=partner_record,
			commit=commit,
		)


def _get_frappe_source_records(config: SyncDefinitionConfig, context: SyncContext) -> list[dict[str, Any]]:
	return [
		record
		for batch in _iter_frappe_source_batches(config, context)
		for record in batch
	]


def _iter_frappe_source_batches(config: SyncDefinitionConfig, context: SyncContext):
	fields = sorted(
		_mapping_fields_for_sync_type(config.mapping, config.sync_type)
		| set(_config_match_fields(config))
		| set(config.frappe_modified_fields)
		| {"name", "modified"}
		| ({_config_frappe_partner_identity_field(config)} if _config_frappe_partner_identity_field(config) else set())
	)
	valid_fields = [field for field in fields if _doctype_has_field(config.doctype, field)]
	or_filters = None
	if context.is_delta_sync:
		since = context.delta_since
		or_filters = []
		for modified_field in config.frappe_modified_fields:
			if _doctype_has_field(config.doctype, modified_field):
				or_filters.append([modified_field, ">=", since])
	if not valid_fields:
		valid_fields = ["name", "modified"]
	return _iter_frappe_record_batches(
		doctype=config.doctype,
		fields=valid_fields,
		filters=config.filters,
		or_filters=or_filters,
		batch_size=config.batch_size,
	)


def _get_partner_source_records(config: SyncDefinitionConfig, connector: Any, context: SyncContext) -> list[dict[str, Any]]:
	return [
		record
		for batch in _iter_partner_source_batches(config, connector, context)
		for record in batch
	]


def _iter_partner_source_batches(config: SyncDefinitionConfig, connector: Any, context: SyncContext):
	record_batches = _iter_partner_record_batches(
		connector=connector,
		source=config.table_name,
		query=_config_read_query(config),
		batch_size=config.batch_size,
		key_fields=_partner_fetch_key_fields(config),
	)
	if not context.is_delta_sync:
		return record_batches
	since = context.delta_since
	def _filtered_batches():
		for batch in record_batches:
			filtered = [
				record for record in batch
				if _record_changed_since(
					record,
					config.partner_modified_fields,
					since,
					assumed_time_zone=getattr(config, "partner_time_zone", None),
					target_time_zone=_site_time_zone(),
				)
			]
			if filtered:
				yield filtered
	return _filtered_batches()


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


def _upsert_frappe_record(
	*,
	doctype: str,
	existing_name: str | None,
	payload: dict[str, Any],
	dry_run: bool,
) -> str | None:
	if dry_run:
		return existing_name
	if existing_name:
		doc = frappe.get_doc(doctype, existing_name)
		for key, value in payload.items():
			if key in SYSTEM_KEYS:
				continue
			if _doctype_has_field(doctype, key):
				doc.set(key, value)
		doc.save(ignore_permissions=True)
		return doc.name

	doc = frappe.new_doc(doctype)
	for key, value in payload.items():
		if key in SYSTEM_KEYS:
			continue
		if _doctype_has_field(doctype, key):
			doc.set(key, value)
	doc.insert(ignore_permissions=True)
	return doc.name


def _build_definition_config(sync_definition_doc: Any) -> SyncDefinitionConfig:
	doctype = _first_value(sync_definition_doc, ["doctype_name"])
	if not doctype:
		raise frappe.ValidationError("Sync Definition is missing target DocType field.")

	partner = _first_value(sync_definition_doc, ["partner"])
	if not partner:
		raise frappe.ValidationError("Sync Definition is missing Sync Partner reference.")

	sync_type = _first_value(sync_definition_doc, ["sync_type"], default="A->B")
	cron_expr = _first_value(sync_definition_doc, ["frequency_cron"])
	filters = _parse_filter_expression(_first_value(sync_definition_doc, ["filter_expression"]))
	batch_size = cint(_first_value(sync_definition_doc, ["batch_size"], default=100)) or 100
	create_new = _as_bool(_first_value(sync_definition_doc, ["create_new"], default=1))
	delete_missing = _as_bool(_first_value(sync_definition_doc, ["delete_missing"], default=0))
	use_last_sync_date = _as_bool(_first_value(sync_definition_doc, ["use_last_sync_date"], default=1))
	conflict_policy = str(_first_value(sync_definition_doc, ["conflict_policy"], default="newest_wins"))
	timestamp_buffer_seconds = cint(_first_value(sync_definition_doc, ["timestamp_buffer_seconds"], default=15)) or 0

	match_fields = _get_match_fields(sync_definition_doc)
	mapping = _get_field_mapping(sync_definition_doc)
	value_mapping = _get_value_mapping(sync_definition_doc)
	if not mapping:
		raise frappe.ValidationError("Sync Definition has no field mapping entries.")
	if not match_fields:
		match_fields = [next(iter(mapping.keys()))]

	frappe_modified_fields = _get_modified_fields(sync_definition_doc, "frappe_modified_field_rows") or ["modified"]
	partner_modified_fields = _get_modified_fields(sync_definition_doc, "partner_modified_field_rows") or ["modified"]

	config = SyncDefinitionConfig(
		name=sync_definition_doc.name,
		doctype=str(doctype),
		partner=str(partner),
		sync_type=str(sync_type),
		cron=str(cron_expr) if cron_expr else None,
		filters=filters,
		batch_size=batch_size,
		create_new=create_new,
		delete_missing=delete_missing,
		one_way_match_mode=_clean_string(_first_value(sync_definition_doc, ["one_way_match_mode"])) or "first_match",
		use_last_sync_date=use_last_sync_date,
		conflict_policy=conflict_policy,
		timestamp_buffer_seconds=timestamp_buffer_seconds,
		table_name=_clean_string(_first_value(sync_definition_doc, ["table_name"])),
		read_query=_clean_string(_first_value(sync_definition_doc, ["read_query"])),
		match_fields=match_fields,
		mapping=mapping,
		value_mapping=value_mapping,
		frappe_modified_fields=frappe_modified_fields,
		partner_modified_fields=partner_modified_fields,
		partner_identity_field=_clean_string(_first_value(sync_definition_doc, ["partner_identity_field"])),
		frappe_partner_identity_field=_clean_string(_first_value(sync_definition_doc, ["frappe_partner_identity_field"])),
		partner_frappe_identity_field=_clean_string(_first_value(sync_definition_doc, ["partner_frappe_identity_field"])),
		partner_create_id_strategy=_clean_string(_first_value(sync_definition_doc, ["partner_create_id_strategy"])) or "payload",
		partner_create_id_source=_clean_string(_first_value(sync_definition_doc, ["partner_create_id_source"])),
		partner_create_id_scope_where=_clean_string(_first_value(sync_definition_doc, ["partner_create_id_scope_where"])),
		capture_audit_payloads=_as_bool(_first_value(sync_definition_doc, ["capture_audit_payloads"], default=0)),
	)
	if config.delete_missing and config.read_query:
		raise frappe.ValidationError("Delete Missing cannot be used together with Read Query.")
	_validate_runtime_mapping(config)
	return config


def _get_match_fields(sync_definition_doc: Any) -> list[str]:
	rows = _get_child_rows_by_options(sync_definition_doc, "Sync Key Field")
	match_fields: list[str] = []
	for row in rows:
		fieldname = _first_value_dict(row, ["field_name", "key_field", "frappe_field", "fieldname"])
		if fieldname:
			match_fields.append(str(fieldname).strip())
	top_level = _first_value(sync_definition_doc, ["match_fields"])
	if not match_fields and isinstance(top_level, str):
		match_fields = [entry.strip() for entry in top_level.split(",") if entry.strip()]
	return [field for field in match_fields if field]


def _get_field_mapping(sync_definition_doc: Any) -> dict[str, dict[str, str]]:
	rows = _get_child_rows_by_options(sync_definition_doc, "Sync Field Mapping")
	mapping: dict[str, dict[str, str]] = {}
	for row in rows:
		frappe_field = _clean_string(
			_first_value_dict(
				row,
				["frappe_field", "source_field", "doctype_field", "source_fieldname", "field_name"],
			)
		)
		entry = _normalize_field_mapping_entry(row)
		if frappe_field and entry:
			mapping[frappe_field] = entry
	top_level = _first_value(sync_definition_doc, ["field_mapping"])
	if not mapping and isinstance(top_level, str):
		mapping = _normalize_field_mapping(top_level)
	if not mapping and isinstance(top_level, dict):
		mapping = _normalize_field_mapping(top_level)
	return mapping


def _mapping_entry_value(raw_entry: Any, candidates: list[str], default: Any = None) -> Any:
	if isinstance(raw_entry, dict):
		return _first_value_dict(raw_entry, candidates, default=default)
	for candidate in candidates:
		value = getattr(raw_entry, candidate, None)
		if value not in (None, ""):
			return value
	return default


def _normalize_mapping_direction(direction: Any) -> str:
	value = _clean_string(direction)
	if not value:
		return MAPPING_DIRECTION_BOTH
	normalized = value.lower()
	if normalized in {"both", "a<->b", "bidirectional"}:
		return MAPPING_DIRECTION_BOTH
	if normalized in {"frappe to partner", "frappe_to_partner", "a->b"}:
		return MAPPING_DIRECTION_FRAPPE_TO_PARTNER
	if normalized in {"partner to frappe", "partner_to_frappe", "a<-b"}:
		return MAPPING_DIRECTION_PARTNER_TO_FRAPPE
	return value


def _normalize_field_mapping_entry(raw_entry: Any) -> dict[str, str] | None:
	if raw_entry in (None, ""):
		return None
	if isinstance(raw_entry, str):
		partner_field = raw_entry
		direction = MAPPING_DIRECTION_BOTH
	else:
		partner_field = _mapping_entry_value(
			raw_entry,
			["partner_field", "partnerField", "target_field", "external_field", "partner_column", "column_name"],
		)
		direction = _mapping_entry_value(raw_entry, ["direction", "label_direction", "sync_direction"])
	partner_field = _clean_string(partner_field)
	if not partner_field:
		return None
	return {
		"partner_field": partner_field,
		"direction": _normalize_mapping_direction(direction),
	}


def _iter_field_mapping_entries(mapping: Any):
	if not isinstance(mapping, dict):
		return
	for frappe_field, raw_entry in mapping.items():
		frappe_field = _clean_string(frappe_field)
		entry = _normalize_field_mapping_entry(raw_entry)
		if frappe_field and entry:
			yield frappe_field, entry


def _normalize_field_mapping(mapping: Any) -> dict[str, dict[str, str]]:
	if isinstance(mapping, str):
		try:
			mapping = json.loads(mapping)
		except Exception:
			return {}
	if not isinstance(mapping, dict):
		return {}
	return {frappe_field: entry for frappe_field, entry in _iter_field_mapping_entries(mapping)}


def _mapping_allows_direction(mapping_entry: dict[str, str], direction: str) -> bool:
	entry_direction = _normalize_mapping_direction(mapping_entry.get("direction"))
	required_direction = _normalize_mapping_direction(direction)
	return entry_direction in {MAPPING_DIRECTION_BOTH, required_direction}


def _partner_field_for_mapping(mapping: dict[str, Any], frappe_field: str, default: str | None = None) -> str | None:
	raw_mapping = mapping if isinstance(mapping, dict) else {}
	entry = _normalize_field_mapping_entry(raw_mapping.get(frappe_field))
	if entry:
		return entry["partner_field"]
	return default if default is not None else frappe_field


def _flatten_mapping_for_direction(mapping: dict[str, Any], direction: str) -> dict[str, str]:
	result: dict[str, str] = {}
	for frappe_field, entry in _iter_field_mapping_entries(mapping):
		if _mapping_allows_direction(entry, direction):
			result[frappe_field] = entry["partner_field"]
	return result


def _mapping_fields_for_sync_type(mapping: dict[str, Any], sync_type: str) -> set[str]:
	required_directions = _required_mapping_directions(sync_type)
	if not required_directions:
		return {frappe_field for frappe_field, _entry in _iter_field_mapping_entries(mapping)}
	return {
		frappe_field
		for frappe_field, entry in _iter_field_mapping_entries(mapping)
		if any(_mapping_allows_direction(entry, direction) for direction in required_directions)
	}


def _required_mapping_directions(sync_type: str) -> list[str]:
	required: list[str] = []
	if sync_type in {"A->B", "A<->B"}:
		required.append(MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	if sync_type in {"A<-B", "A<->B"}:
		required.append(MAPPING_DIRECTION_PARTNER_TO_FRAPPE)
	if not required:
		required.append(MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	return required


def _validate_runtime_mapping(config: SyncDefinitionConfig) -> None:
	mapping = _normalize_field_mapping(config.mapping)
	missing_or_invalid: list[str] = []
	for match_field in _config_match_fields(config):
		entry = mapping.get(match_field)
		if not entry:
			continue
		for required_direction in _required_mapping_directions(config.sync_type):
			if not _mapping_allows_direction(entry, required_direction):
				missing_or_invalid.append(f"{match_field} ({required_direction})")
	if missing_or_invalid:
		raise frappe.ValidationError(
			"Match field mappings must allow the active sync direction(s): " + ", ".join(missing_or_invalid)
		)


def _get_modified_fields(sync_definition_doc: Any, table_fieldname: str) -> list[str]:
	rows = _first_value(sync_definition_doc, [table_fieldname], default=[]) or []
	values: list[str] = []
	for row in rows:
		if hasattr(row, "as_dict"):
			row = row.as_dict()
		elif not isinstance(row, dict):
			row = {"field_name": getattr(row, "field_name", None)}
		fieldname = _first_value_dict(row, ["field_name", "modified_field", "frappe_field"])
		fieldname = _clean_string(fieldname)
		if fieldname:
			values.append(fieldname)
	return values


def _get_value_mapping(sync_definition_doc: Any) -> dict[str, dict[Any, Any]]:
	rows = _get_child_rows_by_options(sync_definition_doc, "Sync Value Mapping")
	result: dict[str, dict[Any, Any]] = {}
	for row in rows:
		frappe_field = _first_value_dict(row, ["frappe_field", "field_name", "source_field"])
		source_value = _first_value_dict(row, ["source_value", "frappe_value", "from_value"])
		target_value = _first_value_dict(row, ["target_value", "partner_value", "to_value"])
		if frappe_field is None or source_value is None:
			continue
		result.setdefault(str(frappe_field), {})[source_value] = target_value
	top_level = _first_value(sync_definition_doc, ["value_mapping"])
	if not result and isinstance(top_level, str):
		try:
			loaded = json.loads(top_level)
			if isinstance(loaded, dict):
				return loaded
		except Exception:
			pass
	if not result and isinstance(top_level, dict):
		return top_level
	return result


def _map_frappe_to_partner(
	record: dict[str, Any],
	mapping: dict[str, Any],
	value_mapping: dict[str, dict[Any, Any]],
	*,
	doctype: str | None = None,
	partner_time_zone: str | None = None,
) -> dict[str, Any]:
	result: dict[str, Any] = {}
	datetime_fields = _get_frappe_datetime_fields(
		doctype,
		[frappe_field for frappe_field, _entry in _iter_field_mapping_entries(mapping)],
	)
	for frappe_field, entry in _iter_field_mapping_entries(mapping):
		if not _mapping_allows_direction(entry, MAPPING_DIRECTION_FRAPPE_TO_PARTNER):
			continue
		partner_field = entry["partner_field"]
		value = record.get(frappe_field)
		field_map = value_mapping.get(frappe_field) or {}
		if value in field_map:
			value = field_map[value]
		if frappe_field in datetime_fields:
			value = _convert_datetime_between_time_zones(
				value,
				source_time_zone=_site_time_zone(),
				target_time_zone=partner_time_zone or _site_time_zone(),
			)
		result[partner_field] = value
	return result


def _map_partner_to_frappe(
	record: dict[str, Any],
	mapping: dict[str, Any],
	value_mapping: dict[str, dict[Any, Any]],
	*,
	doctype: str | None = None,
	partner_time_zone: str | None = None,
) -> dict[str, Any]:
	result: dict[str, Any] = {}
	datetime_fields = _get_frappe_datetime_fields(
		doctype,
		[frappe_field for frappe_field, _entry in _iter_field_mapping_entries(mapping)],
	)
	for frappe_field, entry in _iter_field_mapping_entries(mapping):
		if not _mapping_allows_direction(entry, MAPPING_DIRECTION_PARTNER_TO_FRAPPE):
			continue
		partner_field = entry["partner_field"]
		value = record.get(partner_field)
		field_map = value_mapping.get(frappe_field) or {}
		reverse_map = {mapped_value: source_value for source_value, mapped_value in field_map.items()}
		if value in reverse_map:
			value = reverse_map[value]
		if frappe_field in datetime_fields:
			value = _convert_datetime_between_time_zones(
				value,
				source_time_zone=partner_time_zone,
				target_time_zone=_site_time_zone(),
			)
		result[frappe_field] = value
	return result


def _diff_target_values(
	*,
	new_record: dict[str, Any],
	old_record: dict[str, Any],
	field_names: list[str],
	datetime_fields: set[str] | None = None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> list[tuple[str, Any, Any]]:
	changes: list[tuple[str, Any, Any]] = []
	for field_name in field_names:
		old_value = old_record.get(field_name)
		new_value = new_record.get(field_name)
		if _normalize_field_value(
			field_name,
			old_value,
			datetime_fields=datetime_fields,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		) != _normalize_field_value(
			field_name,
			new_value,
			datetime_fields=datetime_fields,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		):
			changes.append((field_name, old_value, new_value))
	return changes


def _normalize_field_value(
	field_name: str,
	value: Any,
	*,
	datetime_fields: set[str] | None = None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> Any:
	if field_name in (datetime_fields or set()) or isinstance(value, datetime):
		parsed = _parse_datetime(
			value,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		)
		if parsed is not None:
			return parsed
		if isinstance(value, datetime):
			return value.replace(tzinfo=None)
	if isinstance(value, list | dict):
		return json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
	return value


def _record_changed_since(
	record: dict[str, Any],
	modified_fields: list[str],
	last_successful_sync: datetime | None,
	*,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> bool:
	if not last_successful_sync:
		return True
	for field_name in modified_fields:
		field_value = record.get(field_name)
		parsed = _parse_datetime(
			field_value,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		)
		if parsed and parsed >= last_successful_sync:
			return True
	return False


def _latest_modified(
	record: dict[str, Any],
	modified_fields: list[str],
	*,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> datetime | None:
	latest: datetime | None = None
	for field_name in modified_fields:
		parsed = _parse_datetime(
			record.get(field_name),
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		)
		if not parsed:
			continue
		if not latest or parsed > latest:
			latest = parsed
	return latest


def _parse_datetime(
	value: Any,
	*,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> datetime | None:
	if value in (None, ""):
		return None
	try:
		parsed = get_datetime(value)
	except Exception:
		return None
	if not isinstance(parsed, datetime):
		return None
	assumed_zone = _normalize_time_zone_name(assumed_time_zone)
	target_zone = _normalize_time_zone_name(target_time_zone) or _site_time_zone()
	if parsed.tzinfo is None:
		if not assumed_zone:
			return parsed
		try:
			parsed = parsed.replace(tzinfo=ZoneInfo(assumed_zone))
		except ZoneInfoNotFoundError:
			return parsed
	try:
		return parsed.astimezone(ZoneInfo(target_zone)).replace(tzinfo=None)
	except ZoneInfoNotFoundError:
		return parsed.replace(tzinfo=None)


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
	cursor: tuple[Any, Any] | None = None
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


def _frappe_cursor_tuple(record: dict[str, Any]) -> tuple[str, str]:
	return (str(record.get("modified") or ""), str(record.get("name") or ""))


def _get_frappe_keyset_page(
	doctype: str,
	*,
	fields: list[str],
	filters: list | dict | None,
	or_filters: list | None,
	batch_size: int,
	cursor: tuple[Any, Any] | None,
) -> list[dict[str, Any]]:
	if not cursor:
		return frappe.get_all(
			doctype,
			fields=fields,
			filters=filters,
			or_filters=or_filters,
			limit_page_length=batch_size,
			order_by="modified asc, name asc",
		)

	page: list[dict[str, Any]] = []
	start = 0
	while len(page) < batch_size:
		raw_page = frappe.get_all(
			doctype,
			fields=fields,
			filters=_filters_with_frappe_cursor(filters, cursor),
			or_filters=or_filters,
			limit_start=start,
			limit_page_length=batch_size,
			order_by="modified asc, name asc",
		)
		if not raw_page:
			break
		page.extend(record for record in raw_page if _frappe_cursor_tuple(record) > cursor)
		if len(raw_page) < batch_size:
			break
		start += batch_size
	return page[:batch_size]


def _filters_with_frappe_cursor(filters: list | dict | None, cursor: tuple[Any, Any] | None) -> list | dict | None:
	if not cursor:
		return filters
	cursor_filter = ["modified", ">=", cursor[0]]
	if filters is None:
		return [cursor_filter]
	if isinstance(filters, list):
		return [*filters, cursor_filter]
	if isinstance(filters, dict):
		result = dict(filters)
		result["modified"] = [">=", cursor[0]]
		return result
	return filters


def _index_frappe_records(config: SyncDefinitionConfig, records: list[dict[str, Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in records:
		key = _key_tuple_from_frappe(record, _config_match_fields(config))
		if _valid_key(key):
			index[key] = record
	return index


def _index_partner_records(config: SyncDefinitionConfig, records: list[dict[str, Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in records:
		key = _key_tuple_from_partner(record, _config_match_fields(config), config.mapping)
		if _valid_key(key):
			index[key] = record
	return index


def _key_tuple_from_frappe(record: dict[str, Any], key_fields: list[str]) -> tuple[Any, ...]:
	return tuple(record.get(field_name) for field_name in key_fields)


def _key_tuple_from_partner(record: dict[str, Any], key_fields: list[str], mapping: dict[str, Any]) -> tuple[Any, ...]:
	return tuple(record.get(_partner_field_for_mapping(mapping, field_name, field_name)) for field_name in key_fields)


def _valid_key(key: tuple[Any, ...]) -> bool:
	return bool(key) and all(value not in (None, "") for value in key)


def _partner_key_values_from_tuple(config: SyncDefinitionConfig, key_values: tuple[Any, ...]) -> dict[str, Any]:
	result = {}
	for idx, frappe_key in enumerate(_config_match_fields(config)):
		partner_field = _partner_field_for_mapping(config.mapping, frappe_key, frappe_key)
		result[partner_field] = key_values[idx]
	return result


def _partner_fetch_key_fields(config: SyncDefinitionConfig) -> list[str]:
	mapping = getattr(config, "mapping", {}) or {}
	fields = [
		_partner_field_for_mapping(mapping, frappe_field, frappe_field)
		for frappe_field in _config_match_fields(config)
	]
	if _config_partner_identity_field(config):
		fields.append(_config_partner_identity_field(config) or "")
	return [field for field in fields if field]


def _config_match_fields(config: Any) -> list[str]:
	return list(getattr(config, "match_fields", None) or [])


def _config_read_query(config: Any) -> str | None:
	return getattr(config, "read_query", None)


def _config_one_way_match_mode(config: Any) -> str:
	return getattr(config, "one_way_match_mode", None) or "first_match"


def _config_partner_identity_field(config: Any) -> str | None:
	return getattr(config, "partner_identity_field", None)


def _config_frappe_partner_identity_field(config: Any) -> str | None:
	return getattr(config, "frappe_partner_identity_field", None)


def _config_partner_frappe_identity_field(config: Any) -> str | None:
	return getattr(config, "partner_frappe_identity_field", None)


def _config_partner_create_strategy(config: Any) -> str:
	return getattr(config, "partner_create_id_strategy", None) or "payload"


def _frappe_partner_identity_value(config: SyncDefinitionConfig, frappe_record: dict[str, Any] | None) -> Any:
	fieldname = _config_frappe_partner_identity_field(config)
	if not fieldname or not frappe_record:
		return None
	return frappe_record.get(fieldname)


def _partner_identity_value(config: SyncDefinitionConfig, partner_record: dict[str, Any] | None) -> Any:
	fieldname = _config_partner_identity_field(config)
	if not fieldname or not partner_record:
		return None
	return partner_record.get(fieldname)


def _build_partner_identity_index(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[Any, dict[str, Any]]:
	iterable = records.values() if isinstance(records, dict) else records
	index: dict[Any, dict[str, Any]] = {}
	for record in iterable:
		identity = _partner_identity_value(config, record)
		if identity not in (None, ""):
			index[identity] = record
	return index


def _normalize_partner_match_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]:
	if isinstance(records, dict) or _config_one_way_match_mode(config) == "all_matches":
		return records
	return _index_partner_records(config, records)


def _build_frappe_partner_identity_index(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[Any, dict[str, Any]]:
	fieldname = _config_frappe_partner_identity_field(config)
	if not fieldname:
		return {}
	iterable = records.values() if isinstance(records, dict) else records
	index: dict[Any, dict[str, Any]] = {}
	for record in iterable:
		identity = record.get(fieldname)
		if identity not in (None, ""):
			index[identity] = record
	return index


def _normalize_frappe_match_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]:
	if isinstance(records, dict) or _config_one_way_match_mode(config) == "all_matches":
		return records
	return _index_frappe_records(config, records)


def _find_existing_partner_record(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	partner_index: dict[tuple[Any, ...], dict[str, Any]],
	partner_identity_index: dict[Any, dict[str, Any]],
) -> dict[str, Any] | None:
	return next(
		iter(
			_find_existing_partner_records(
				config,
				frappe_record,
				{key: [record] for key, record in partner_index.items()},
				partner_identity_index,
			)
		),
		None,
	)


def _find_existing_partner_records(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	partner_groups: dict[tuple[Any, ...], list[dict[str, Any]]],
	partner_identity_index: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
	frappe_partner_id = _frappe_partner_identity_value(config, frappe_record)
	if frappe_partner_id not in (None, ""):
		existing = partner_identity_index.get(frappe_partner_id)
		if existing:
			return [existing]
	key = _key_tuple_from_frappe(frappe_record, _config_match_fields(config))
	if _valid_key(key):
		matches = list(partner_groups.get(key) or [])
		if _config_one_way_match_mode(config) == "all_matches":
			return matches
		return matches[-1:] if matches else []
	return []


def _find_existing_frappe_record(
	config: SyncDefinitionConfig,
	partner_record: dict[str, Any],
	frappe_index: dict[tuple[Any, ...], dict[str, Any]],
	frappe_partner_identity_index: dict[Any, dict[str, Any]],
) -> dict[str, Any] | None:
	return next(
		iter(
			_find_existing_frappe_records(
				config,
				partner_record,
				{key: [record] for key, record in frappe_index.items()},
				frappe_partner_identity_index,
			)
		),
		None,
	)


def _find_existing_frappe_records(
	config: SyncDefinitionConfig,
	partner_record: dict[str, Any],
	frappe_groups: dict[tuple[Any, ...], list[dict[str, Any]]],
	frappe_partner_identity_index: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
	partner_identity = _partner_identity_value(config, partner_record)
	if partner_identity not in (None, ""):
		existing = frappe_partner_identity_index.get(partner_identity)
		if existing:
			return [existing]
	key = _key_tuple_from_partner(partner_record, _config_match_fields(config), config.mapping)
	if _valid_key(key):
		matches = list(frappe_groups.get(key) or [])
		if _config_one_way_match_mode(config) == "all_matches":
			return matches
		return matches[-1:] if matches else []
	return []


def _pair_token_from_frappe(config: SyncDefinitionConfig, record: dict[str, Any]) -> tuple[Any, ...] | None:
	identity = _frappe_partner_identity_value(config, record)
	if _config_partner_identity_field(config) and identity not in (None, ""):
		return ("partner_identity", identity)
	key = _key_tuple_from_frappe(record, _config_match_fields(config))
	if _valid_key(key):
		return ("match", *key)
	return None


def _pair_token_from_partner(config: SyncDefinitionConfig, record: dict[str, Any]) -> tuple[Any, ...] | None:
	identity = _partner_identity_value(config, record)
	if _config_partner_identity_field(config) and identity not in (None, ""):
		return ("partner_identity", identity)
	key = _key_tuple_from_partner(record, _config_match_fields(config), config.mapping)
	if _valid_key(key):
		return ("match", *key)
	return None


def _index_paired_frappe_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[tuple[Any, ...], dict[str, Any]]:
	iterable = records.values() if isinstance(records, dict) else records
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in iterable:
		token = _pair_token_from_frappe(config, record)
		if token:
			index[token] = record
	return index


def _index_paired_partner_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[tuple[Any, ...], dict[str, Any]]:
	iterable = records.values() if isinstance(records, dict) else records
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in iterable:
		token = _pair_token_from_partner(config, record)
		if token:
			index[token] = record
	return index


def _partner_key_values_for_write(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	key: tuple[Any, ...],
) -> dict[str, Any]:
	frappe_partner_id = _frappe_partner_identity_value(config, frappe_record)
	partner_identity_field = _config_partner_identity_field(config)
	if partner_identity_field and frappe_partner_id not in (None, ""):
		return {partner_identity_field: frappe_partner_id}
	return _partner_key_values_from_tuple(config, key)


def _partner_key_values_for_existing_match(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	key: tuple[Any, ...],
	partner_record: dict[str, Any] | None,
) -> dict[str, Any]:
	partner_identity_field = _config_partner_identity_field(config)
	if partner_identity_field and partner_record and partner_record.get(partner_identity_field) not in (None, ""):
		return {partner_identity_field: partner_record.get(partner_identity_field)}
	return _partner_key_values_for_write(config, frappe_record, key)


def _can_write_partner_matches_individually(
	config: SyncDefinitionConfig,
	partner_records: list[dict[str, Any]],
) -> bool:
	if len(partner_records) <= 1:
		return True
	partner_identity_field = _config_partner_identity_field(config)
	if not partner_identity_field:
		return False
	return all(record.get(partner_identity_field) not in (None, "") for record in partner_records)


def _build_partner_create_options(config: SyncDefinitionConfig) -> ConnectorCreateOptions:
	return ConnectorCreateOptions(
		identity_field=_config_partner_identity_field(config),
		strategy=_config_partner_create_strategy(config),
		source=getattr(config, "partner_create_id_source", None),
		scope_where=getattr(config, "partner_create_id_scope_where", None),
	)


def _apply_partner_link_fields(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	partner_payload: dict[str, Any],
) -> dict[str, Any]:
	payload = dict(partner_payload)
	partner_frappe_field = _config_partner_frappe_identity_field(config)
	if partner_frappe_field and frappe_record.get("name") not in (None, ""):
		payload[partner_frappe_field] = frappe_record.get("name")
	return payload


def _persist_frappe_partner_identity(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	write_result: Any,
	*,
	dry_run: bool,
) -> None:
	frappe_partner_field = _config_frappe_partner_identity_field(config)
	partner_identity_field = _config_partner_identity_field(config)
	if dry_run or not frappe_partner_field or not partner_identity_field:
		return
	doc_name = frappe_record.get("name")
	if not doc_name:
		return
	resolved = {}
	if isinstance(getattr(write_result, "resolved_key_values", None), dict):
		resolved = write_result.resolved_key_values
	partner_id = resolved.get(partner_identity_field)
	record = getattr(write_result, "record", None)
	if partner_id in (None, "") and isinstance(record, dict):
		partner_id = record.get(partner_identity_field)
	if partner_id in (None, ""):
		return
	if frappe_record.get(frappe_partner_field) == partner_id:
		return
	doc = frappe.get_doc(config.doctype, doc_name)
	if _doctype_has_field(config.doctype, frappe_partner_field):
		doc.set(frappe_partner_field, partner_id)
		doc.save(ignore_permissions=True)
		frappe_record[frappe_partner_field] = partner_id


def _register_and_log(
	*,
	stats: SyncStats,
	run_doc: Any,
	config: SyncDefinitionConfig,
	action: str,
	status: str,
	message: str,
	direction: str,
	frappe_record: dict[str, Any] | None,
	partner_record: dict[str, Any] | None,
	changes: list[tuple[str, Any, Any]] | None = None,
	commit: bool = True,
):
	stats.register(action=action, status=status)
	_create_run_item(
		run_doc=run_doc,
		config=config,
		sync_definition_name=config.name,
		action=action,
		status=status,
		frappe_record=frappe_record,
		partner_record=partner_record,
		message=message,
		direction=direction,
		changes=changes,
		commit=False,
	)
	_track_pending_run_writes(run_doc, 1)
	if commit:
		_flush_pending_run_writes(run_doc, force=True)
	else:
		_flush_pending_run_writes(run_doc, threshold=_runtime_commit_batch_size(config))


def _has_active_run(sync_definition_name: str) -> bool:
	meta = frappe.get_meta(SYNC_RUN)
	if not meta.has_field("sync_definition") or not meta.has_field("status"):
		return False
	return bool(
		frappe.db.exists(
			SYNC_RUN,
			{
				"sync_definition": sync_definition_name,
				"status": ["in", sorted(ACTIVE_RUN_STATUSES)],
			},
		)
	)


def _create_run_doc(sync_definition_doc: Any, *, status: str, trigger: str, dry_run: bool) -> Any:
	payload: dict[str, Any] = {"doctype": SYNC_RUN}
	payload.update(
		{
			"sync_definition": sync_definition_doc.name,
			"status": status,
			"trigger_type": trigger,
			"dry_run": cint(dry_run),
			"started_at": now_datetime(),
			"sync_type": _first_value(sync_definition_doc, ["sync_type"], default="A->B"),
			"sync_partner": _first_value(sync_definition_doc, ["partner"]),
		}
	)
	run_doc = frappe.get_doc(payload)
	run_doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return run_doc


def _create_run_item(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig | None,
	sync_definition_name: str,
	action: str,
	status: str,
	frappe_record: dict[str, Any] | None,
	partner_record: dict[str, Any] | None,
	message: str | None,
	direction: str | None = None,
	changes: list[tuple[str, Any, Any]] | None = None,
	commit: bool = True,
) -> Any:
	payload: dict[str, Any] = {"doctype": SYNC_RUN_ITEM}
	meta = frappe.get_meta(SYNC_RUN_ITEM)
	payload.update(
		{
			"sync_run": run_doc.name,
			"sync_definition": sync_definition_name,
			"action": action,
			"status": status,
			"message": message,
			"direction": direction or _first_value(run_doc, ["sync_type"]),
		}
	)

	record_name = (frappe_record or {}).get("name")
	record_key = _compact_record_key(config, frappe_record=frappe_record, partner_record=partner_record)
	source_id = record_name or _compact_source_id(config, frappe_record=frappe_record)
	target_id = _compact_target_id(config, partner_record=partner_record)
	_set_first_existing(payload, meta, ["document_name", "frappe_name", "frappe_record_name"], record_name)
	_set_first_existing(payload, meta, ["record_key"], _fit_data_value(record_key))
	_set_first_existing(payload, meta, ["source_id"], _fit_data_value(source_id))
	_set_first_existing(payload, meta, ["target_id"], _fit_data_value(target_id))
	_set_first_existing(payload, meta, ["change_count"], len(changes or []))
	_set_first_existing(payload, meta, ["changed_fields"], _summarize_changed_fields(changes))
	if _capture_audit_payloads(config):
		_set_first_existing(payload, meta, ["frappe_payload", "frappe_record_json"], json.dumps(frappe_record, default=str, ensure_ascii=True) if frappe_record else None)
		_set_first_existing(payload, meta, ["partner_payload", "partner_record_json"], json.dumps(partner_record, default=str, ensure_ascii=True) if partner_record else None)

	doc = frappe.get_doc(payload)
	doc.insert(ignore_permissions=True)
	if commit:
		frappe.db.commit()
	return doc


def _summarize_changed_fields(changes: list[tuple[str, Any, Any]] | None) -> str | None:
	field_names = [_clean_string(field_name) for field_name, _old_value, _new_value in changes or []]
	normalized = [field_name for field_name in field_names if field_name]
	if not normalized:
		return None
	return ", ".join(normalized)


def _capture_audit_payloads(config: SyncDefinitionConfig | Any | None) -> bool:
	return _as_bool(getattr(config, "capture_audit_payloads", 0))


def _merge_partner_runtime_settings(config: SyncDefinitionConfig, partner_doc: Any) -> SyncDefinitionConfig:
	partner_time_zone = _get_partner_time_zone(partner_doc)
	if isinstance(config, SyncDefinitionConfig):
		return replace(config, partner_time_zone=partner_time_zone)
	setattr(config, "partner_time_zone", partner_time_zone)
	return _coerce_config(config)


def _get_partner_time_zone(partner_doc: Any) -> str | None:
	return _normalize_time_zone_name(_first_value(partner_doc, ["time_zone"]))


def _normalize_time_zone_name(value: Any) -> str | None:
	cleaned = _clean_string(value)
	if not cleaned:
		return None
	try:
		ZoneInfo(cleaned)
	except ZoneInfoNotFoundError:
		return None
	return cleaned


def _site_time_zone() -> str:
	try:
		return _normalize_time_zone_name(get_system_timezone()) or "UTC"
	except Exception:
		return "UTC"


def _convert_datetime_between_time_zones(
	value: Any,
	*,
	source_time_zone: str | None,
	target_time_zone: str | None,
) -> Any:
	parsed = _parse_datetime(
		value,
		assumed_time_zone=source_time_zone,
		target_time_zone=target_time_zone,
	)
	return parsed if parsed is not None else value


def _get_frappe_datetime_fields(doctype: str | None, field_names: list[str] | set[str]) -> set[str]:
	if not doctype:
		return set()
	candidates = {_clean_string(field_name) for field_name in field_names if _clean_string(field_name)}
	result = {field_name for field_name in candidates if field_name in {"modified", "creation"}}
	if not candidates:
		return result
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return result
	fieldtypes = {
		_clean_string(getattr(field, "fieldname", None)): getattr(field, "fieldtype", None)
		for field in getattr(meta, "fields", [])
	}
	for field_name in candidates:
		if fieldtypes.get(field_name) == "Datetime":
			result.add(field_name)
	return result


def _frappe_datetime_fields(config: SyncDefinitionConfig) -> set[str]:
	candidates = set(getattr(config, "frappe_modified_fields", []) or [])
	candidates.update(frappe_field for frappe_field, _entry in _iter_field_mapping_entries(getattr(config, "mapping", {})))
	return _get_frappe_datetime_fields(getattr(config, "doctype", None), candidates)


def _partner_datetime_fields(config: SyncDefinitionConfig) -> set[str]:
	partner_fields = set(getattr(config, "partner_modified_fields", []) or [])
	for frappe_field in _frappe_datetime_fields(config):
		partner_field = _partner_field_for_mapping(getattr(config, "mapping", {}), frappe_field, frappe_field)
		if partner_field:
			partner_fields.add(partner_field)
	return partner_fields


def _runtime_commit_batch_size(config: SyncDefinitionConfig | None) -> int:
	if not config:
		return DEFAULT_RUNTIME_COMMIT_BATCH
	batch_size = cint(getattr(config, "batch_size", DEFAULT_RUNTIME_COMMIT_BATCH)) or DEFAULT_RUNTIME_COMMIT_BATCH
	return max(1, min(batch_size, DEFAULT_RUNTIME_COMMIT_BATCH))


def _track_pending_run_writes(run_doc: Any, write_count: int) -> None:
	pending = cint(getattr(run_doc, RUN_DOC_PENDING_WRITES_ATTR, 0)) or 0
	setattr(run_doc, RUN_DOC_PENDING_WRITES_ATTR, pending + max(0, write_count))


def _flush_pending_run_writes(run_doc: Any, *, threshold: int | None = None, force: bool = False) -> None:
	pending = cint(getattr(run_doc, RUN_DOC_PENDING_WRITES_ATTR, 0)) or 0
	if pending <= 0:
		return
	if not force and threshold is not None and pending < threshold:
		return
	frappe.db.commit()
	setattr(run_doc, RUN_DOC_PENDING_WRITES_ATTR, 0)


def _update_doc_fields(doc: Any, values: dict[str, Any], *, commit: bool = True) -> None:
	meta = frappe.get_meta(doc.doctype)
	for key, value in values.items():
		fieldname = _find_field(meta, [key])
		if fieldname:
			doc.db_set(fieldname, value, update_modified=False)
	if commit:
		frappe.db.commit()


def _update_definition_runtime(
	sync_definition_doc: Any,
	*,
	last_run: str,
	status: str = "Success",
	last_sync_at: datetime | None,
	summary: str | None = None,
	commit: bool = True,
):
	meta = frappe.get_meta(sync_definition_doc.doctype)
	updates = {
		"last_run": last_run,
		"last_run_status": status,
		"last_run_summary": summary,
		"last_sync_at": last_sync_at,
	}
	if status == "Success" and last_sync_at is not None:
		updates["last_successful_sync"] = last_sync_at
	for fieldname, value in updates.items():
		if value is None:
			continue
		if meta.has_field(fieldname):
			sync_definition_doc.db_set(fieldname, value, update_modified=False)
	if commit:
		frappe.db.commit()


def _update_definition_failure(sync_definition_doc: Any, *, last_run: str, error_message: str, commit: bool = True):
	meta = frappe.get_meta(sync_definition_doc.doctype)
	updates = {
		"last_run": last_run,
		"last_run_status": "Error",
		"last_run_summary": error_message.splitlines()[-1] if error_message else "Sync failed",
	}
	for fieldname, value in updates.items():
		if value is None:
			continue
		if meta.has_field(fieldname):
			sync_definition_doc.db_set(fieldname, value, update_modified=False)
	if commit:
		frappe.db.commit()


def _set_next_run_at(sync_definition_doc: Any, cron_expr: str | None, *, commit: bool = True):
	if not cron_expr or not croniter:
		return
	if not frappe.get_meta(sync_definition_doc.doctype).has_field("next_run_at"):
		return
	try:
		next_run = croniter(cron_expr, now_datetime()).get_next(datetime)
	except Exception:
		frappe.logger("sync").warning("Invalid cron expression for %s: %s", _doc_name(sync_definition_doc), cron_expr)
		return
	sync_definition_doc.db_set("next_run_at", next_run, update_modified=False)
	if commit:
		frappe.db.commit()


def _get_last_successful_sync(sync_definition_name: str) -> datetime | None:
	run_meta = frappe.get_meta(SYNC_RUN)
	fields = [field for field in ("last_sync_at", "finished_at", "started_at") if run_meta.has_field(field)]
	if not fields:
		fields = ["modified"]
	runs = frappe.get_all(
		SYNC_RUN,
		filters={"sync_definition": sync_definition_name, "status": "Success"},
		fields=fields,
		order_by="creation desc",
		limit=1,
	)
	if not runs:
		return None
	for fieldname in ("last_sync_at", "finished_at", "started_at", "modified"):
		value = runs[0].get(fieldname)
		parsed = _parse_datetime(value)
		if parsed:
			return parsed
	return None


def _format_run_summary(result_payload: dict[str, Any]) -> str:
	return (
		f"processed={result_payload.get('processed_count', 0)}, "
		f"created={result_payload.get('created_count', 0)}, "
		f"updated={result_payload.get('updated_count', 0)}, "
		f"deleted={result_payload.get('deleted_count', 0)}, "
		f"skipped={result_payload.get('skipped_count', 0)}, "
		f"conflict={result_payload.get('conflict_count', 0)}, "
		f"errors={result_payload.get('error_count', 0)}, "
		f"delta_since={result_payload.get('delta_since') or 'none'}"
	)


def _normalize_trigger_type(trigger: Any) -> str:
	normalized = _clean_string(trigger) or "manual"
	if normalized not in VALID_TRIGGER_TYPES:
		raise frappe.ValidationError(f"Trigger Type must be one of: {', '.join(sorted(VALID_TRIGGER_TYPES))}.")
	return normalized


def _terminal_status_for_result(result_payload: dict[str, Any]) -> str:
	if cint(result_payload.get("error_count")) > 0:
		return "Partial Error"
	if cint(result_payload.get("conflict_count")) > 0:
		return "Needs Review"
	return "Success"


def _api_status_for_run_status(run_status: str) -> str:
	if run_status == "Success":
		return "success"
	if run_status == "Partial Error":
		return "partial_error"
	if run_status == "Needs Review":
		return "needs_review"
	return str(run_status or "").strip().lower().replace(" ", "_")


def _is_enabled(doc: Any) -> bool:
	return _as_bool(_first_value(doc, ["enabled"], default=1))


def _parse_filter_expression(raw: Any) -> list | dict | None:
	if raw in (None, ""):
		return None
	if isinstance(raw, (list, dict)):
		return raw
	if isinstance(raw, str):
		try:
			loaded = json.loads(raw)
			if isinstance(loaded, (list, dict)):
				return loaded
		except Exception:
			frappe.logger("sync").warning("Invalid filter JSON in Sync Definition. Raw value ignored.")
	return None


def _parse_lines(raw: Any) -> list[str]:
	if not isinstance(raw, str):
		return []
	return [line.strip() for line in raw.splitlines() if line.strip()]


def _clean_string(raw: Any) -> str | None:
	if raw is None:
		return None
	value = str(raw).strip()
	return value or None


def _first_value(doc: Any, candidates: list[str], default: Any = None) -> Any:
	for candidate in candidates:
		try:
			value = doc.get(candidate)
		except Exception:
			value = None
		if value not in (None, ""):
			return value
	return default


def _doc_name(doc: Any) -> str | None:
	if isinstance(doc, dict):
		name = doc.get("name")
	else:
		name = getattr(doc, "name", None)
	return str(name) if name not in (None, "") else None


def _first_value_dict(doc: dict[str, Any], candidates: list[str], default: Any = None) -> Any:
	for candidate in candidates:
		value = doc.get(candidate)
		if value not in (None, ""):
			return value
	return default


def _as_bool(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_child_rows_by_options(parent_doc: Any, child_doctype: str) -> list[dict[str, Any]]:
	meta = frappe.get_meta(parent_doc.doctype)
	for field in meta.fields:
		if field.fieldtype != "Table" or field.options != child_doctype:
			continue
		rows = parent_doc.get(field.fieldname) or []
		return [row.as_dict() if hasattr(row, "as_dict") else dict(row) for row in rows]
	return []


def _doctype_has_field(doctype: str, fieldname: str) -> bool:
	if fieldname in {"name", "creation", "modified", "owner", "modified_by"}:
		return True
	return bool(frappe.get_meta(doctype).has_field(fieldname))


def _find_field(meta: Any, candidates: list[str]) -> str | None:
	for candidate in candidates:
		if meta.has_field(candidate):
			return candidate
	return None


def _set_first_existing(payload: dict[str, Any], meta: Any, candidates: list[str], value: Any) -> str | None:
	if value is None:
		return None
	for candidate in candidates:
		if meta.has_field(candidate):
			payload[candidate] = value
			return candidate
	return None


def _definition_lock(lock_key: str):
	cache = frappe.cache()
	lock = getattr(cache, "lock", None)
	if not callable(lock):
		return nullcontext()
	return cache.lock(lock_key, timeout=600, blocking_timeout=10)


def _build_record_key(record: dict[str, Any]) -> str:
	if not record:
		return ""
	if "name" in record and record["name"]:
		return str(record["name"])
	items = sorted((key, value) for key, value in record.items() if value not in (None, ""))
	return json.dumps(items, default=str, ensure_ascii=True)


def _compact_record_key(
	config: SyncDefinitionConfig | None,
	*,
	frappe_record: dict[str, Any] | None,
	partner_record: dict[str, Any] | None,
) -> str:
	if config:
		parts = []
		for frappe_field in _config_match_fields(config):
			value = None
			if frappe_record:
				value = frappe_record.get(frappe_field)
			if value in (None, "") and partner_record:
				partner_field = _partner_field_for_mapping(config.mapping, frappe_field, frappe_field)
				value = partner_record.get(partner_field)
			if value not in (None, ""):
				parts.append(f"{frappe_field}={value}")
		if parts:
			return " | ".join(parts)
	return _build_record_key(frappe_record or partner_record or {})


def _compact_source_id(config: SyncDefinitionConfig | None, *, frappe_record: dict[str, Any] | None) -> str:
	if frappe_record and frappe_record.get("name"):
		return str(frappe_record["name"])
	if config and frappe_record:
		parts = [f"{field}={frappe_record.get(field)}" for field in _config_match_fields(config) if frappe_record.get(field) not in (None, "")]
		if parts:
			return " | ".join(parts)
	return _build_record_key(frappe_record or {})


def _compact_target_id(config: SyncDefinitionConfig | None, *, partner_record: dict[str, Any] | None) -> str:
	if config and partner_record:
		parts = []
		for frappe_field in _config_match_fields(config):
			partner_field = _partner_field_for_mapping(config.mapping, frappe_field, frappe_field)
			value = partner_record.get(partner_field)
			if value not in (None, ""):
				parts.append(f"{partner_field}={value}")
		if parts:
			return " | ".join(parts)
	return _build_record_key(partner_record or {})


def _fit_data_value(value: str | None, max_length: int = 140) -> str | None:
	if value in (None, ""):
		return value
	text = str(value)
	if len(text) <= max_length:
		return text
	return f"{text[: max_length - 3]}..."


def _update_partner_connection_status(partner_doc: Any, *, status: str, details: str) -> None:
	values = {
		"last_connection_status": "Success" if status == "ok" else "Error",
		"last_checked_on": now_datetime(),
		"last_connection_error": "" if status == "ok" else details,
	}
	for fieldname, value in values.items():
		if frappe.get_meta(partner_doc.doctype).has_field(fieldname):
			partner_doc.db_set(fieldname, value, update_modified=False)
	frappe.db.commit()


def _is_due_by_cron(sync_definition_doc: Any, cron_expr: str, now: datetime) -> bool:
	if not croniter:
		return False
	try:
		previous_tick = croniter(cron_expr, now).get_prev(datetime)
	except Exception:
		frappe.logger("sync").warning("Invalid cron expression for %s: %s", _doc_name(sync_definition_doc), cron_expr)
		return False

	run_meta = frappe.get_meta(SYNC_RUN)
	if not run_meta.has_field("sync_definition"):
		return False

	definition_name = _doc_name(sync_definition_doc)
	if not definition_name:
		return False
	filters = {"sync_definition": definition_name}
	if run_meta.has_field("status"):
		filters["status"] = ["in", sorted(DONE_RUN_STATUSES)]
	fields = ["finished_at"] if run_meta.has_field("finished_at") else ["modified"]
	last_runs = frappe.get_all(SYNC_RUN, filters=filters, fields=fields, order_by=f"{fields[0]} desc", limit=1)
	if not last_runs:
		return True
	last_run = _parse_datetime(last_runs[0].get(fields[0]))
	if not last_run:
		return True
	return last_run < previous_tick


def _sanitize_document_dict(data: dict[str, Any], *, mask_credentials: bool = False) -> dict[str, Any]:
	meta = frappe.get_meta(data["doctype"])
	child_fields = {field.fieldname: field.options for field in meta.fields if field.fieldtype == "Table"}
	secret_fields = set(_collect_secret_fieldnames(meta, data))
	result: dict[str, Any] = {"doctype": data["doctype"]}
	if data.get("name"):
		result["name"] = data["name"]
	for key, value in data.items():
		if key in SYSTEM_KEYS or key.startswith("_"):
			continue
		if key == "doctype":
			continue
		if key in child_fields and isinstance(value, list):
			child_doctype = child_fields[key]
			result[key] = [_sanitize_child_row(child_doctype, row) for row in value]
			continue
		if meta.has_field(key):
			result[key] = "***" if mask_credentials and key in secret_fields and value not in (None, "") else value
	return result


def _collect_secret_fieldnames(meta: Any, data: dict[str, Any]) -> list[str]:
	secrets = []
	for field in getattr(meta, "fields", []):
		if getattr(field, "fieldtype", None) == "Password":
			secrets.append(field.fieldname)
	for fieldname in ("secret_fields",):
		raw = data.get(fieldname)
		if not raw:
			continue
		secrets.extend(line.strip() for line in str(raw).splitlines() if line.strip())
	return secrets


def _sanitize_child_row(child_doctype: str, row: dict[str, Any]) -> dict[str, Any]:
	child_meta = frappe.get_meta(child_doctype)
	result: dict[str, Any] = {"doctype": child_doctype}
	for key, value in row.items():
		if key in SYSTEM_KEYS or key.startswith("_"):
			continue
		if key in {"parent", "parenttype", "parentfield"}:
			continue
		if child_meta.has_field(key):
			result[key] = value
	return result


def _normalize_doc_payload(doctype: str, payload: dict[str, Any]) -> dict[str, Any]:
	meta = frappe.get_meta(doctype)
	table_fields = {field.fieldname: field.options for field in meta.fields if field.fieldtype == "Table"}
	result: dict[str, Any] = {"doctype": doctype}
	if payload.get("name"):
		result["name"] = payload["name"]

	for field in meta.fields:
		if field.fieldname in table_fields:
			continue
		if field.fieldname in payload:
			result[field.fieldname] = payload[field.fieldname]

	for table_field, child_doctype in table_fields.items():
		rows = payload.get(table_field)
		if not isinstance(rows, list):
			continue
		child_meta = frappe.get_meta(child_doctype)
		child_rows: list[dict[str, Any]] = []
		for row in rows:
			if not isinstance(row, dict):
				continue
			child_row = {"doctype": child_doctype}
			for child_field in child_meta.fields:
				if child_field.fieldname in row:
					child_row[child_field.fieldname] = row[child_field.fieldname]
			child_rows.append(child_row)
		result[table_field] = child_rows
	return result


def _upsert_document_from_payload(doctype: str, payload: dict[str, Any], *, overwrite: bool) -> str:
	normalized = _normalize_doc_payload(doctype, payload)
	name = normalized.get("name")
	if name and frappe.db.exists(doctype, name):
		if not overwrite:
			return str(name)
		doc = frappe.get_doc(doctype, name)
		meta = frappe.get_meta(doctype)
		table_fields = [field.fieldname for field in meta.fields if field.fieldtype == "Table"]
		for field in meta.fields:
			if field.fieldtype == "Table":
				continue
			if field.fieldname in normalized:
				doc.set(field.fieldname, normalized[field.fieldname])
		for table_field in table_fields:
			if table_field in normalized:
				doc.set(table_field, normalized[table_field])
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	doc = frappe.get_doc(normalized)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name
