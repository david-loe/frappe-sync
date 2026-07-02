from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe.utils import cint, get_datetime, get_system_timezone, now_datetime
import yaml

from sync.sync.constants import (
	ACTIVE_RUN_STATUSES,
	CONFLICT_POLICY_NEWEST_WINS,
	DONE_RUN_STATUSES,
	FRAPPE_WRITE_ACTION_NONE,
	FRAPPE_WRITE_ACTION_SUBMIT,
	FRAPPE_WRITE_ACTIONS,
	MAPPING_DIRECTION_BOTH,
	MAPPING_DIRECTION_FRAPPE_TO_PARTNER,
	MAPPING_DIRECTION_PARTNER_TO_FRAPPE,
	MATCH_MODE_IDENTITY_FIELDS,
	MATCH_MODE_MATCH_FIELDS,
	MATCH_MODES,
	ONE_WAY_MATCH_ALL,
	ONE_WAY_MATCH_FIRST,
	RUN_STATUS_ERROR,
	RUN_STATUS_NEEDS_REVIEW,
	RUN_STATUS_PARTIAL_ERROR,
	RUN_STATUS_QUEUED,
	RUN_STATUS_RUNNING,
	RUN_STATUS_SKIPPED,
	RUN_STATUS_SUCCESS,
	SYNC_DEFINITION,
	SYNC_PARTNER,
	SYNC_PARTNER_TYPE,
	SYNC_RUN,
	SYNC_RUN_ITEM,
	SYNC_SETTINGS,
	TIMESTAMP_TIE_BREAKERS,
	TIMESTAMP_TIE_FRAPPE_WINS,
	TIMESTAMP_TIE_MANUAL,
	TIMESTAMP_TIE_PARTNER_WINS,
	TRIGGER_MANUAL,
	TRIGGER_SCHEDULER,
	VALID_TRIGGER_TYPES,
	VALUE_MAPPING_FALLBACK_ACTIONS,
	VALUE_MAPPING_FALLBACK_KEEP_ORIGINAL,
	VALUE_MAPPING_FALLBACK_USE_FALLBACK,
	VALUE_MAPPING_FALLBACK_USE_NULL,
)

from .connectors import ConnectorCreateOptions, get_connector_for_partner

try:
	from croniter import croniter
except Exception:  # pragma: no cover - optional runtime dependency
	croniter = None


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
SYNC_DEFINITION_RUNTIME_STATE_FIELDS = {
	"last_run",
	"last_run_status",
	"last_run_summary",
	"last_sync_at",
	"last_successful_sync",
	"next_run_at",
}
SYNC_DEFINITION_LOCK_TIMEOUT_SECONDS = 2 * 60 * 60

SYNC_TYPE_FRAPPE_TO_PARTNER = MAPPING_DIRECTION_FRAPPE_TO_PARTNER
SYNC_TYPE_PARTNER_TO_FRAPPE = MAPPING_DIRECTION_PARTNER_TO_FRAPPE
SYNC_TYPE_BIDIRECTIONAL = MAPPING_DIRECTION_BOTH

DEFAULT_RUNTIME_COMMIT_BATCH = 50
DEFAULT_STALE_RUN_TIMEOUT_MINUTES = 180
DEFAULT_RUN_RETENTION_DAYS_SUCCESS = 90
DEFAULT_RUN_RETENTION_DAYS_ERROR = 365
RUN_DOC_PENDING_WRITES_ATTR = "_sync_pending_write_count"
AUDIT_RECORD_UNSET = object()
VALUE_MAPPING_UNSET = object()
DEFAULT_TIMESTAMP_BUFFER_MS = 100
CHILD_FIELD_PATH_SEPARATOR = "."


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
	timestamp_buffer_ms: int
	table_name: str | None
	read_query: str | None
	match_fields: list[str]
	mapping: dict[str, dict[str, str]]
	value_mapping: dict[str, dict[Any, Any]]
	match_mode: str = MATCH_MODE_MATCH_FIELDS
	frappe_modified_field: str = "modified"
	frappe_creation_field: str = "creation"
	partner_modified_field: str | None = None
	partner_creation_field: str | None = None
	timestamp_tie_breaker: str = TIMESTAMP_TIE_MANUAL
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None = None
	partner_identity_field: str | None = None
	frappe_partner_identity_field: str | None = None
	partner_frappe_identity_field: str | None = None
	partner_create_id_strategy: str = "payload"
	partner_create_id_source: str | None = None
	partner_create_id_scope_where: str | None = None
	partner_time_zone: str | None = None
	one_way_match_mode: str = ONE_WAY_MATCH_FIRST
	capture_audit_payloads: bool = False
	update_existing: bool = True
	frappe_after_insert_action: str = FRAPPE_WRITE_ACTION_NONE
	frappe_after_update_action: str = FRAPPE_WRITE_ACTION_NONE

@dataclass(slots=True)
class PartnerMatchLookup:
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]
	groups: dict[tuple[Any, ...], list[dict[str, Any]]]
	latest_by_key: dict[tuple[Any, ...], dict[str, Any]]
	identity_by_value: dict[Any, dict[str, Any]]


@dataclass(slots=True)
class FrappeMatchLookup:
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]
	groups: dict[tuple[Any, ...], list[dict[str, Any]]]
	latest_by_key: dict[tuple[Any, ...], dict[str, Any]]
	identity_by_value: dict[Any, dict[str, Any]]


@dataclass(slots=True)
class IdentityRecordState:
	frappe_records: list[dict[str, Any]]
	partner_records: list[dict[str, Any]]
	frappe_by_name: dict[Any, dict[str, Any]]
	frappe_by_partner_id: dict[Any, dict[str, Any]]
	partner_by_identity: dict[Any, dict[str, Any]]
	partner_by_frappe_id: dict[Any, dict[str, Any]]
	duplicate_conflicts: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]]


@dataclass(slots=True)
class RuntimeMappingContext:
	mapping: dict[str, dict[str, str]]
	value_mapping: dict[str, dict[Any, Any]]
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None
	to_partner_entries: tuple[tuple[str, str], ...]
	to_frappe_entries: tuple[tuple[str, str], ...]
	connector_mapping: dict[str, str]
	reverse_value_mapping: dict[str, dict[Any, Any]]
	frappe_datetime_fields: set[str]
	partner_datetime_fields: set[str]
	frappe_fieldnames: set[str] | None
	child_table_options: dict[str, str]
	site_time_zone: str
	partner_time_zone: str | None


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
		return self.last_successful_sync

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
		results.append(enqueue_sync_definition(name, trigger=TRIGGER_SCHEDULER, queue=queue))
	return results


def run_due_sync_definitions_scheduled(limit: int = 20, queue: bool = True) -> list[dict[str, Any]]:
	frappe.set_user("Administrator")
	recover_stale_runs()
	return run_due_sync_definitions(limit=limit, queue=queue)


def recover_stale_runs(
	sync_definition_name: str | None = None,
	*,
	timeout_minutes: int | None = None,
	terminal_status: str | None = None,
) -> dict[str, Any]:
	settings = _get_sync_settings()
	timeout = _positive_int(timeout_minutes, settings.stale_run_timeout_minutes)
	timeout = max(1, timeout)
	cutoff = now_datetime() - timedelta(minutes=timeout)
	recovered: list[dict[str, Any]] = []
	filters: dict[str, Any] = {"status": ["in", sorted(ACTIVE_RUN_STATUSES)]}
	if sync_definition_name:
		filters["sync_definition"] = str(sync_definition_name)
	rows = frappe.get_all(
		SYNC_RUN,
		filters=filters,
		fields=["name", "sync_definition", "status", "started_at", "creation"],
		order_by="creation asc",
	)

	for row in rows:
		run_status = str(_row_value(row, "status") or "")
		run_started_at = _parse_datetime(_row_value(row, "started_at")) or _parse_datetime(_row_value(row, "creation"))
		if run_started_at and run_started_at > cutoff:
			continue
		run_name = str(_row_value(row, "name") or "")
		if not run_name:
			continue
		definition_name = _clean_string(_row_value(row, "sync_definition"))
		recovered_status = _stale_run_terminal_status(run_status, terminal_status)
		message = f"Recovered stale {run_status or 'active'} Sync Run after {timeout} minutes."
		run_doc = frappe.get_doc(SYNC_RUN, run_name)
		_update_doc_fields(
			run_doc,
			{
				"status": recovered_status,
				"finished_at": now_datetime(),
				"summary": message,
				"error_message": message if recovered_status == RUN_STATUS_ERROR else None,
			},
			commit=False,
		)
		if definition_name and frappe.db.exists(SYNC_DEFINITION, definition_name):
			definition_doc = frappe.get_doc(SYNC_DEFINITION, definition_name)
			_update_definition_stale_recovery(
				definition_doc,
				last_run=run_name,
				status=recovered_status,
				summary=message,
				commit=False,
			)
		recovered.append(
			{
				"run": run_name,
				"sync_definition": definition_name,
				"previous_status": run_status,
				"status": recovered_status,
			}
		)

	frappe.db.commit()
	return {
		"ok": True,
		"timeout_minutes": timeout,
		"cutoff": cutoff.isoformat(),
		"recovered_count": len(recovered),
		"runs": recovered,
	}


def cleanup_sync_run_retention(
	*,
	retention_days_success: int | None = None,
	retention_days_error: int | None = None,
) -> dict[str, Any]:
	settings = _get_sync_settings()
	success_days = max(1, _positive_int(retention_days_success, settings.run_retention_days_success))
	error_days = max(1, _positive_int(retention_days_error, settings.run_retention_days_error))
	now = now_datetime()
	success_cutoff = now - timedelta(days=success_days)
	error_cutoff = now - timedelta(days=error_days)
	deleted_runs = 0
	deleted_items = 0
	rows = frappe.get_all(
		SYNC_RUN,
		filters={"status": ["in", sorted(DONE_RUN_STATUSES)]},
		fields=["name", "status", "finished_at", "creation"],
		order_by="creation asc",
	)

	for row in rows:
		run_name = str(_row_value(row, "name") or "")
		if not run_name:
			continue
		status = str(_row_value(row, "status") or "")
		cutoff = success_cutoff if status == RUN_STATUS_SUCCESS else error_cutoff
		completed_at = _parse_datetime(_row_value(row, "finished_at")) or _parse_datetime(_row_value(row, "creation"))
		if completed_at and completed_at > cutoff:
			continue
		for item_name in _linked_run_item_names(run_name):
			frappe.delete_doc(SYNC_RUN_ITEM, item_name, ignore_permissions=True, force=True)
			deleted_items += 1
		frappe.delete_doc(SYNC_RUN, run_name, ignore_permissions=True, force=True)
		deleted_runs += 1

	frappe.db.commit()
	return {
		"ok": True,
		"retention_days_success": success_days,
		"retention_days_error": error_days,
		"deleted_runs": deleted_runs,
		"deleted_run_items": deleted_items,
	}


def cleanup_sync_run_retention_scheduled() -> dict[str, Any]:
	frappe.set_user("Administrator")
	return cleanup_sync_run_retention()


def enqueue_sync_definition(
	sync_definition_name: str,
	*,
	trigger: str = TRIGGER_MANUAL,
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
		run_doc = _create_run_doc(sync_definition, status=RUN_STATUS_QUEUED, trigger=trigger, dry_run=dry_run)

	if not queue:
		return execute_sync_definition(
			sync_definition_name,
			trigger=trigger,
			dry_run=dry_run,
			run_name=run_doc.name,
		)

	job_id = f"sync:run:{sync_definition_name}:{frappe.generate_hash(length=8)}"
	_update_doc_fields(run_doc, {"status": RUN_STATUS_QUEUED, "job_id": job_id})
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
	trigger: str = TRIGGER_MANUAL,
	dry_run: bool = False,
):
	return execute_sync_definition(sync_definition_name, trigger=trigger, dry_run=dry_run, run_name=run_name)


def resolve_sync_run_item(sync_run_item_name: str, direction: str) -> dict[str, Any]:
	direction = _clean_string(direction) or ""
	if direction not in {SYNC_TYPE_FRAPPE_TO_PARTNER, SYNC_TYPE_PARTNER_TO_FRAPPE}:
		raise frappe.ValidationError("Resolution direction must be Frappe -> Partner or Frappe <- Partner.")

	item_doc = frappe.get_doc(SYNC_RUN_ITEM, sync_run_item_name)
	run_doc = frappe.get_doc(SYNC_RUN, item_doc.sync_run)
	if cint(getattr(run_doc, "dry_run", 0)):
		raise frappe.ValidationError("Dry run items cannot be manually resolved.")
	if getattr(item_doc, "status", None) != "conflict" or getattr(item_doc, "action", None) != "conflict":
		raise frappe.ValidationError("Only open conflict Sync Run Items can be manually resolved.")
	if getattr(item_doc, "write_direction", None):
		raise frappe.ValidationError("Sync Run Item already has a write direction.")

	sync_definition_name = getattr(item_doc, "sync_definition", None) or getattr(run_doc, "sync_definition", None)
	if not sync_definition_name:
		raise frappe.ValidationError("Sync Run Item is missing Sync Definition.")
	config = _build_definition_config(frappe.get_doc(SYNC_DEFINITION, sync_definition_name))
	if not _update_existing_enabled(config):
		raise frappe.ValidationError("Update Existing is disabled for this Sync Definition.")

	try:
		if direction == SYNC_TYPE_PARTNER_TO_FRAPPE:
			written_after = _resolve_item_to_frappe(item_doc, config)
			message = "Manually accepted partner changes."
		else:
			written_after = _resolve_item_to_partner(item_doc, config)
			message = "Manually accepted frappe changes."
		_update_doc_fields(
			item_doc,
			{
				"action": "updated",
				"status": "success",
				"write_direction": direction,
				"message": message,
				"written_after_payload": _json_payload(written_after),
			},
		)
		return {"ok": True, "sync_run_item": item_doc.name, "write_direction": direction, "status": "success"}
	except Exception as exc:
		_update_doc_fields(
			item_doc,
			{
				"action": "error",
				"status": "error",
				"write_direction": direction,
				"message": str(exc),
			},
		)
		raise


def execute_sync_definition(
	sync_definition_name: str,
	*,
	trigger: str = TRIGGER_MANUAL,
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
			run_doc = _create_run_doc(sync_definition_doc, status=RUN_STATUS_QUEUED, trigger=trigger, dry_run=dry_run)

		sync_definition = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
		run_started_at = now_datetime()
		_update_doc_fields(run_doc, {"status": RUN_STATUS_RUNNING, "started_at": run_started_at, "trigger_type": trigger})

		try:
			config = _build_definition_config(sync_definition)
			last_successful_sync = _get_last_successful_sync(sync_definition_name)
			context = SyncContext(config=config, dry_run=dry_run, last_successful_sync=last_successful_sync)
			result_payload = _run_engine(sync_definition, run_doc, context=context)

			terminal_status = _terminal_status_for_result(result_payload)
			sync_stamp = run_started_at if terminal_status == RUN_STATUS_SUCCESS and not dry_run else None
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
					"status": RUN_STATUS_ERROR,
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
	doctype_fieldnames = _doctype_fieldnames(config.doctype)
	valid_fields = (
		[field for field in fields if field in doctype_fieldnames]
		if doctype_fieldnames is not None
		else [field for field in fields if _doctype_has_field(config.doctype, field)]
	)
	filters = config.filters
	frappe_records = frappe.get_all(
		config.doctype,
		fields=valid_fields,
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
		"match_mode": _config_match_mode(config),
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
	config_doc["match_mode"] = _first_value(sync_definition_doc, ["match_mode"], default=MATCH_MODE_MATCH_FIELDS)
	partner_name = _first_value(sync_definition_doc, ["partner"])

	payload: dict[str, Any] = {
		"version": 2,
		"exported_at": now_datetime().isoformat(),
		"sync_definition": config_doc,
	}
	if partner_name:
		partner_doc = frappe.get_doc(SYNC_PARTNER, partner_name)
		payload["sync_partner"] = _sanitize_document_dict(partner_doc.as_dict(), mask_credentials=mask_credentials)
		partner_type_name = _first_value(partner_doc, ["partner_type"])
		if partner_type_name and frappe.db.exists(SYNC_PARTNER_TYPE, partner_type_name):
			partner_type_doc = frappe.get_doc(SYNC_PARTNER_TYPE, partner_type_name)
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

	if data.get("version") != 2:
		return {
			"ok": False,
			"can_import": False,
			"overwrite": _as_bool(overwrite),
			"error": "Unsupported Sync YAML version. Version 2 is required.",
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
		("sync_partner_type", SYNC_PARTNER_TYPE),
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
		if doctype == SYNC_DEFINITION and "match_mode" not in payload:
			summary["invalid"] += 1
			documents[doctype] = {
				"payload_key": payload_key,
				"doctype": doctype,
				"name": _first_value_dict(normalized, ["name"]),
				"status": "invalid",
				"exists": False,
				"action": "skip",
				"hint": "Payload section `sync_definition` is missing required field `match_mode`.",
			}
			continue
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
		("sync_partner_type", SYNC_PARTNER_TYPE),
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
	mapping_context = _build_runtime_mapping_context(config)
	connector = get_connector_for_partner(partner_doc)
	ping = connector.ping()
	if not ping.ok:
		raise frappe.ValidationError(f"Partner connector validation failed: {ping.message}")

	stats = SyncStats()
	if config.sync_type == "Frappe -> Partner":
		partner_batches = _iter_partner_source_batches(config, connector, context, apply_delta_filter=False)
		if config.delete_missing and context.is_full_sync:
			partner_records = [
				record
				for batch in partner_batches
				for record in batch
			]
		elif _config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL:
			partner_records = [
				record
				for batch in partner_batches
				for record in batch
			]
		else:
			partner_records = _build_partner_index_from_batches(config, partner_batches)
		partner_lookup = _build_partner_match_lookup(config, partner_records)
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
				partner_lookup=partner_lookup,
				mapping_context=mapping_context,
				dry_run=context.dry_run,
				stats=stats,
				label_direction="Frappe -> Partner",
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
					partner_lookup=partner_lookup,
					mapping_context=mapping_context,
					dry_run=context.dry_run,
					stats=stats,
					label_direction="Frappe -> Partner",
					full_sync=False,
					source_keys=source_keys,
				)
			_flush_pending_run_writes(run_doc, force=True)
	elif config.sync_type == "Frappe <- Partner":
		frappe_records = _get_frappe_source_records(config, context, apply_delta_filter=False)
		frappe_lookup = _build_frappe_match_lookup(config, frappe_records)
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
				frappe_lookup=frappe_lookup,
				mapping_context=mapping_context,
				dry_run=context.dry_run,
				stats=stats,
				label_direction="Frappe <- Partner",
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
					frappe_lookup=frappe_lookup,
					mapping_context=mapping_context,
					dry_run=context.dry_run,
					stats=stats,
					label_direction="Frappe <- Partner",
					full_sync=False,
					source_keys=source_keys,
				)
			_flush_pending_run_writes(run_doc, force=True)
	else:
		if _config_match_mode(config) == MATCH_MODE_IDENTITY_FIELDS:
			frappe_index = [
				record
				for batch in _iter_frappe_source_batches(config, context)
				for record in batch
			]
			frappe_lookup_index = (
				[
					record
					for batch in _iter_frappe_source_batches(config, context, apply_delta_filter=False)
					for record in batch
				]
				if context.is_delta_sync
				else frappe_index
			)
			partner_index = [
				record
				for batch in _iter_partner_source_batches(config, connector, context)
				for record in batch
			]
			partner_lookup_index = (
				[
					record
					for batch in _iter_partner_source_batches(config, connector, context, apply_delta_filter=False)
					for record in batch
				]
				if context.is_delta_sync
				else partner_index
			)
		else:
			frappe_index = _build_frappe_index_from_batches(
				config,
				_iter_frappe_source_batches(config, context),
			)
			frappe_lookup_index = (
				_build_frappe_index_from_batches(
					config,
					_iter_frappe_source_batches(config, context, apply_delta_filter=False),
				)
				if context.is_delta_sync
				else frappe_index
			)
			partner_index = _build_partner_index_from_batches(
				config,
				_iter_partner_source_batches(config, connector, context),
			)
			partner_lookup_index = (
				_build_partner_index_from_batches(
					config,
					_iter_partner_source_batches(config, connector, context, apply_delta_filter=False),
				)
				if context.is_delta_sync
				else partner_index
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
			frappe_lookup_records=frappe_lookup_index,
			partner_lookup_records=partner_lookup_index,
			mapping_context=mapping_context,
			full_sync=context.is_full_sync,
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
			match_mode = _normalize_match_mode(getattr(config, "match_mode", None))
			normalized_config = replace(
				config,
				match_mode=match_mode,
				delete_missing=_delete_missing_enabled(config.sync_type, config.delete_missing, match_mode=match_mode),
				partner_time_zone=_normalize_time_zone_name(config.partner_time_zone),
				timestamp_tie_breaker=_normalize_timestamp_tie_breaker(config.timestamp_tie_breaker),
				value_mapping_fallbacks=_normalize_value_mapping_fallbacks(config.value_mapping_fallbacks),
				update_existing=_as_bool(getattr(config, "update_existing", 1)),
				frappe_after_insert_action=_normalize_frappe_write_action(
					getattr(config, "frappe_after_insert_action", None)
				),
				frappe_after_update_action=_normalize_frappe_write_action(
					getattr(config, "frappe_after_update_action", None)
				),
			)
			_validate_runtime_mapping(normalized_config)
			return normalized_config
	timestamp_buffer_ms = _coerce_timestamp_buffer_ms(getattr(config, "timestamp_buffer_ms", None))
	match_mode = _normalize_match_mode(getattr(config, "match_mode", None))
	normalized = SyncDefinitionConfig(
		name=str(getattr(config, "name", "")),
		doctype=str(getattr(config, "doctype", "")),
		partner=str(getattr(config, "partner", "")),
		sync_type=str(getattr(config, "sync_type", "Frappe -> Partner")),
		cron=getattr(config, "cron", None),
		filters=getattr(config, "filters", None),
		batch_size=cint(getattr(config, "batch_size", 100)) or 100,
		create_new=_as_bool(getattr(config, "create_new", 1)),
		delete_missing=_delete_missing_enabled(
			getattr(config, "sync_type", "Frappe -> Partner"),
			getattr(config, "delete_missing", 0),
			match_mode=match_mode,
		),
		one_way_match_mode=_clean_string(getattr(config, "one_way_match_mode", None)) or ONE_WAY_MATCH_FIRST,
		use_last_sync_date=_as_bool(getattr(config, "use_last_sync_date", 1)),
		conflict_policy=str(getattr(config, "conflict_policy", CONFLICT_POLICY_NEWEST_WINS)),
		timestamp_buffer_ms=timestamp_buffer_ms,
		table_name=getattr(config, "table_name", None),
		read_query=getattr(config, "read_query", None),
		match_fields=list(getattr(config, "match_fields", []) or []),
		mapping=_normalize_field_mapping(getattr(config, "mapping", {}) or {}),
		value_mapping=dict(getattr(config, "value_mapping", {}) or {}),
		match_mode=match_mode,
		frappe_modified_field=_clean_string(getattr(config, "frappe_modified_field", None))
		or _first_configured_field(getattr(config, "frappe_modified_fields", None), "modified"),
		frappe_creation_field=_clean_string(getattr(config, "frappe_creation_field", None)) or "creation",
		partner_modified_field=_clean_string(getattr(config, "partner_modified_field", None))
		or _first_configured_field(getattr(config, "partner_modified_fields", None), None),
		partner_creation_field=_clean_string(getattr(config, "partner_creation_field", None)),
		timestamp_tie_breaker=_normalize_timestamp_tie_breaker(getattr(config, "timestamp_tie_breaker", None)),
		value_mapping_fallbacks=_normalize_value_mapping_fallbacks(
			getattr(config, "value_mapping_fallbacks", {}) or {}
		),
		partner_identity_field=_clean_string(getattr(config, "partner_identity_field", None)),
		frappe_partner_identity_field=_clean_string(getattr(config, "frappe_partner_identity_field", None)),
		partner_frappe_identity_field=_clean_string(getattr(config, "partner_frappe_identity_field", None)),
		partner_create_id_strategy=_clean_string(getattr(config, "partner_create_id_strategy", None)) or "payload",
		partner_create_id_source=_clean_string(getattr(config, "partner_create_id_source", None)),
		partner_create_id_scope_where=_clean_string(getattr(config, "partner_create_id_scope_where", None)),
		partner_time_zone=_normalize_time_zone_name(getattr(config, "partner_time_zone", None)),
		capture_audit_payloads=_as_bool(getattr(config, "capture_audit_payloads", 0)),
		update_existing=_as_bool(getattr(config, "update_existing", 1)),
		frappe_after_insert_action=_normalize_frappe_write_action(
			getattr(config, "frappe_after_insert_action", None)
		),
		frappe_after_update_action=_normalize_frappe_write_action(
			getattr(config, "frappe_after_update_action", None)
		),
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


def _build_partner_match_lookup(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> PartnerMatchLookup:
	lookup_records = _normalize_partner_match_records(config, records)
	groups = _group_partner_records(config, lookup_records)
	return PartnerMatchLookup(
		records=lookup_records,
		groups=groups,
		latest_by_key={key: grouped_records[-1] for key, grouped_records in groups.items()},
		identity_by_value=_build_partner_identity_index(config, lookup_records),
	)


def _build_frappe_match_lookup(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> FrappeMatchLookup:
	lookup_records = _normalize_frappe_match_records(config, records)
	groups = _group_frappe_records(config, lookup_records)
	return FrappeMatchLookup(
		records=lookup_records,
		groups=groups,
		latest_by_key={key: grouped_records[-1] for key, grouped_records in groups.items()},
		identity_by_value=_build_frappe_partner_identity_index(config, lookup_records),
	)


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
	partner_lookup: PartnerMatchLookup | None = None,
	mapping_context: RuntimeMappingContext | None = None,
):
	partner_lookup = partner_lookup or _build_partner_match_lookup(config, partner_records)
	mapping_context = mapping_context or _build_runtime_mapping_context(config)
	partner_groups = partner_lookup.groups
	partner_index = partner_lookup.latest_by_key
	partner_identity_index = partner_lookup.identity_by_value
	collected_source_keys = source_keys if source_keys is not None else set()
	connector_mapping = mapping_context.connector_mapping

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
				getattr(config, "value_mapping_fallbacks", None),
				doctype=getattr(config, "doctype", None),
				partner_time_zone=getattr(config, "partner_time_zone", None),
				mapping_context=mapping_context,
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
			write_payload = _with_partner_timestamps(
				config,
				frappe_record,
				partner_payload,
				create=True,
				mapping_context=mapping_context,
			)
			try:
				write = connector.upsert_record(
					record=write_payload,
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
				partner_record=getattr(write, "record", None) or write_payload,
				partner_before_record=None,
				written_after_record=getattr(write, "record", None) or write_payload,
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
					exclude_fields={
						_config_partner_modified_field(config),
						_config_partner_creation_field(config),
					},
					datetime_fields=mapping_context.partner_datetime_fields,
					assumed_time_zone=getattr(config, "partner_time_zone", None),
					target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
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
			if not _update_existing_enabled(config):
				_log_update_existing_disabled(
					stats=stats,
					run_doc=run_doc,
					config=config,
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=existing_partner,
					write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
					changes=change_sets[-1],
					commit=False,
				)
				continue
			write_payload = _with_partner_timestamps(
				config,
				frappe_record,
				partner_payload,
				create=False,
				mapping_context=mapping_context,
			)
			try:
				write = connector.upsert_record(
					record=write_payload,
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
				partner_record=getattr(write, "record", None) or existing_partner or write_payload,
				partner_before_record=existing_partner,
				written_after_record=getattr(write, "record", None) or existing_partner or write_payload,
				changes=change_sets[-1],
				commit=False,
			)
			continue

		for matched_partner in existing_partners:
			changes = _diff_target_values(
				new_record=partner_payload,
				old_record=matched_partner or {},
				field_names=list(partner_payload.keys()),
				exclude_fields={
					_config_partner_modified_field(config),
					_config_partner_creation_field(config),
				},
				datetime_fields=mapping_context.partner_datetime_fields,
				assumed_time_zone=getattr(config, "partner_time_zone", None),
				target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
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
			if not _update_existing_enabled(config):
				_log_update_existing_disabled(
					stats=stats,
					run_doc=run_doc,
					config=config,
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=matched_partner,
					write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
					changes=changes,
					commit=False,
				)
				continue
			write_payload = _with_partner_timestamps(
				config,
				frappe_record,
				partner_payload,
				create=False,
				mapping_context=mapping_context,
			)

			try:
				write = connector.upsert_record(
					record=write_payload,
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
				partner_record=getattr(write, "record", None) or write_payload,
				partner_before_record=matched_partner,
				written_after_record=getattr(write, "record", None) or write_payload,
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
	frappe_lookup: FrappeMatchLookup | None = None,
	mapping_context: RuntimeMappingContext | None = None,
):
	frappe_lookup = frappe_lookup or _build_frappe_match_lookup(config, frappe_records)
	mapping_context = mapping_context or _build_runtime_mapping_context(config)
	partner_input_records = _normalize_partner_match_records(config, partner_records)
	frappe_groups = frappe_lookup.groups
	frappe_index = frappe_lookup.latest_by_key
	frappe_partner_identity_index = frappe_lookup.identity_by_value
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
			getattr(config, "value_mapping_fallbacks", None),
			doctype=getattr(config, "doctype", None),
			partner_time_zone=getattr(config, "partner_time_zone", None),
			mapping_context=mapping_context,
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
			write_payload = _with_frappe_modified_timestamp(
				config,
				partner_record,
				frappe_payload,
				mapping_context=mapping_context,
			)
			try:
				doc_name = _upsert_frappe_record(
					doctype=config.doctype,
					existing_name=None,
					payload=write_payload,
					dry_run=dry_run,
					**_frappe_write_action_kwargs(config),
				)
				if doc_name:
					write_payload["name"] = doc_name
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
				frappe_record=write_payload,
				partner_record=partner_record,
				frappe_before_record=None,
				written_after_record=write_payload,
				changes=[],
				commit=False,
			)
			continue

		for matched_frappe in existing_frappe_records:
			changes = _diff_target_values(
				new_record=frappe_payload,
				old_record=matched_frappe or {},
				field_names=_frappe_diff_field_names(frappe_payload, mapping_context),
				exclude_fields={
					_config_frappe_modified_field(config),
					_config_frappe_creation_field(config),
				},
				datetime_fields=mapping_context.frappe_datetime_fields,
				target_time_zone=mapping_context.site_time_zone,
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

			if not _update_existing_enabled(config):
				_log_update_existing_disabled(
					stats=stats,
					run_doc=run_doc,
					config=config,
					direction=label_direction,
					frappe_record=matched_frappe,
					partner_record=partner_record,
					write_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
					changes=changes,
					commit=False,
				)
				continue

			try:
				target_payload = _with_frappe_modified_timestamp(
					config,
					partner_record,
					frappe_payload,
					mapping_context=mapping_context,
				)
				target_payload["name"] = matched_frappe.get("name")
				doc_name = _upsert_frappe_record(
					doctype=config.doctype,
					existing_name=matched_frappe.get("name"),
					payload=target_payload,
					dry_run=dry_run,
					**_frappe_write_action_kwargs(config),
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
				frappe_before_record=matched_frappe,
				written_after_record=target_payload,
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
		key_values = _partner_key_values_from_partner_record(config, partner_record)
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
	frappe_lookup_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None = None,
	partner_lookup_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None = None,
	mapping_context: RuntimeMappingContext | None = None,
	full_sync: bool = False,
):
	mapping_context = mapping_context or _build_runtime_mapping_context(config)
	if _config_match_mode(config) == MATCH_MODE_IDENTITY_FIELDS:
		_sync_bidirectional_identity_fields(
			run_doc=run_doc,
			config=config,
			connector=connector,
			frappe_records=frappe_records,
			partner_records=partner_records,
			dry_run=dry_run,
			stats=stats,
			last_successful_sync=last_successful_sync,
			frappe_lookup_records=frappe_lookup_records,
			partner_lookup_records=partner_lookup_records,
			mapping_context=mapping_context,
			full_sync=full_sync,
		)
		return
	frappe_index = _index_paired_frappe_records(config, frappe_records)
	partner_index = _index_paired_partner_records(config, partner_records)
	frappe_target_lookup_records = frappe_lookup_records if frappe_lookup_records is not None else []
	partner_target_lookup_records = partner_lookup_records if partner_lookup_records is not None else []
	frappe_target_lookup = _build_frappe_match_lookup(config, frappe_target_lookup_records)
	partner_target_lookup = _build_partner_match_lookup(config, partner_target_lookup_records)
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
				partner_records=partner_target_lookup_records,
				partner_lookup=partner_target_lookup,
				mapping_context=mapping_context,
				dry_run=dry_run,
				stats=stats,
				label_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
				full_sync=False,
			)
			continue

		if partner_record and not frappe_record:
			_sync_partner_to_frappe(
				run_doc=run_doc,
				config=config,
				connector=connector,
				partner_records=[partner_record],
				frappe_records=frappe_target_lookup_records,
				frappe_lookup=frappe_target_lookup,
				mapping_context=mapping_context,
				dry_run=dry_run,
				stats=stats,
				label_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
				full_sync=False,
			)
			continue

		if not frappe_record or not partner_record:
			continue

		frappe_payload = _map_partner_to_frappe(
			partner_record,
			config.mapping,
			config.value_mapping,
			getattr(config, "value_mapping_fallbacks", None),
			doctype=getattr(config, "doctype", None),
			partner_time_zone=getattr(config, "partner_time_zone", None),
			mapping_context=mapping_context,
		)
		partner_payload = _map_frappe_to_partner(
			frappe_record,
			config.mapping,
			config.value_mapping,
			getattr(config, "value_mapping_fallbacks", None),
			doctype=getattr(config, "doctype", None),
			partner_time_zone=getattr(config, "partner_time_zone", None),
			mapping_context=mapping_context,
		)

		to_partner_changes = _diff_target_values(
			new_record=partner_payload,
			old_record=partner_record,
			field_names=list(partner_payload.keys()),
			exclude_fields={
				_config_partner_modified_field(config),
				_config_partner_creation_field(config),
			},
			datetime_fields=mapping_context.partner_datetime_fields,
			assumed_time_zone=getattr(config, "partner_time_zone", None),
			target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
		)
		to_frappe_changes = _diff_target_values(
			new_record=frappe_payload,
			old_record=frappe_record,
			field_names=_frappe_diff_field_names(frappe_payload, mapping_context),
			exclude_fields={
				_config_frappe_modified_field(config),
				_config_frappe_creation_field(config),
			},
			datetime_fields=mapping_context.frappe_datetime_fields,
			target_time_zone=mapping_context.site_time_zone,
		)
		if not to_partner_changes and not to_frappe_changes:
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="skipped",
				status="skipped",
				message="No differences between both sides.",
				direction="Frappe <-> Partner",
				frappe_record=frappe_record,
				partner_record=partner_record,
				commit=False,
			)
			continue

		if not _update_existing_enabled(config):
			_log_update_existing_disabled(
				stats=stats,
				run_doc=run_doc,
				config=config,
				direction="Frappe <-> Partner",
				frappe_record=frappe_record,
				partner_record=partner_record,
				write_direction=SYNC_TYPE_BIDIRECTIONAL,
				changes=_canonical_conflict_changes(
					config,
					to_frappe_changes=to_frappe_changes,
					to_partner_changes=to_partner_changes,
				),
				commit=False,
			)
			continue

		frappe_changed_since_last = _record_changed_since(
			record=frappe_record,
			modified_fields=_config_frappe_modified_field(config),
			last_successful_sync=last_successful_sync,
			creation_field=_config_frappe_creation_field(config),
			target_time_zone=mapping_context.site_time_zone,
		)
		partner_changed_since_last = _record_changed_since(
			record=partner_record,
			modified_fields=_config_partner_modified_field(config),
			last_successful_sync=last_successful_sync,
			creation_field=_config_partner_creation_field(config),
			assumed_time_zone=getattr(config, "partner_time_zone", None),
			target_time_zone=mapping_context.site_time_zone,
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
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated partner from frappe.",
				commit=False,
				mapping_context=mapping_context,
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
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated frappe from partner.",
				commit=False,
			)
			continue

		if config.conflict_policy != CONFLICT_POLICY_NEWEST_WINS:
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="conflict",
				status="conflict",
				message=f"Unsupported conflict policy: {config.conflict_policy}",
				direction="Frappe <-> Partner",
				frappe_record=frappe_record,
				partner_record=partner_record,
				commit=False,
			)
			continue

		frappe_latest = _latest_modified(
			record=frappe_record,
			modified_fields=_config_frappe_modified_field(config),
			creation_field=_config_frappe_creation_field(config),
			target_time_zone=mapping_context.site_time_zone,
		)
		partner_latest = _latest_modified(
			record=partner_record,
			modified_fields=_config_partner_modified_field(config),
			creation_field=_config_partner_creation_field(config),
			assumed_time_zone=getattr(config, "partner_time_zone", None),
			target_time_zone=mapping_context.site_time_zone,
		)
		timestamp_winner = _compare_modified_timestamps(
			frappe_latest,
			partner_latest,
			buffer_ms=_config_timestamp_buffer_ms(config),
		)
		if timestamp_winner == "partner":
			_apply_frappe_update(
				run_doc=run_doc,
				config=config,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				changes=to_frappe_changes,
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated frappe from partner with newest_wins.",
				commit=False,
			)
		elif timestamp_winner == "frappe":
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
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated partner from frappe with newest_wins.",
				commit=False,
				mapping_context=mapping_context,
			)
		elif _config_timestamp_tie_breaker(config) == TIMESTAMP_TIE_PARTNER_WINS:
			_apply_frappe_update(
				run_doc=run_doc,
				config=config,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				changes=to_frappe_changes,
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated frappe from partner by timestamp tie breaker.",
				commit=False,
			)
		elif _config_timestamp_tie_breaker(config) == TIMESTAMP_TIE_FRAPPE_WINS:
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
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated partner from frappe by timestamp tie breaker.",
				commit=False,
				mapping_context=mapping_context,
			)
		else:
			frappe_resolution_payload, partner_resolution_payload = _manual_conflict_resolution_payloads(
				config=config,
				frappe_record=frappe_record,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				partner_payload=partner_payload,
				mapping_context=mapping_context,
			)
			conflict_changes = _canonical_conflict_changes(
				config,
				to_frappe_changes=to_frappe_changes,
				to_partner_changes=to_partner_changes,
			)
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="conflict",
				status="conflict",
				message="Manual conflict requires review; no write performed.",
				direction="Frappe <-> Partner",
				frappe_record=frappe_record,
				partner_record=partner_record,
				changes=conflict_changes,
				frappe_before_record=frappe_record,
				partner_before_record=partner_record,
				frappe_resolution_payload=frappe_resolution_payload,
				partner_resolution_payload=partner_resolution_payload,
				write_direction=None,
				commit=False,
			)
	_flush_pending_run_writes(run_doc)


def _sync_bidirectional_identity_fields(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	partner_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	last_successful_sync: datetime | None,
	frappe_lookup_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None,
	partner_lookup_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None,
	mapping_context: RuntimeMappingContext,
	full_sync: bool,
) -> None:
	_validate_identity_field_config(config)
	operation_state = _build_identity_record_state(config, frappe_records, partner_records)
	lookup_state = _build_identity_record_state(
		config,
		frappe_lookup_records if frappe_lookup_records is not None else frappe_records,
		partner_lookup_records if partner_lookup_records is not None else partner_records,
	)
	conflicted_frappe: set[int] = set()
	conflicted_partner: set[int] = set()
	_logged_conflicts: set[str] = set()

	def log_conflict(message: str, frappe_record: dict[str, Any] | None, partner_record: dict[str, Any] | None) -> None:
		key = json.dumps(
			[
				message,
				_identity_frappe_name(config, frappe_record),
				_identity_frappe_partner_id(config, frappe_record),
				_identity_partner_identity(config, partner_record),
				_identity_partner_frappe_id(config, partner_record),
			],
			default=str,
			ensure_ascii=True,
		)
		if key in _logged_conflicts:
			return
		_logged_conflicts.add(key)
		if frappe_record is not None:
			conflicted_frappe.add(id(frappe_record))
		if partner_record is not None:
			conflicted_partner.add(id(partner_record))
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="conflict",
			status="conflict",
			message=message,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			commit=False,
		)

	for message, frappe_group, partner_group in lookup_state.duplicate_conflicts:
		frappe_record = frappe_group[0] if frappe_group else None
		partner_record = partner_group[0] if partner_group else None
		for record in frappe_group:
			conflicted_frappe.add(id(record))
		for record in partner_group:
			conflicted_partner.add(id(record))
		log_conflict(message, frappe_record, partner_record)

	pairs: dict[tuple[Any, Any], tuple[dict[str, Any], dict[str, Any]]] = {}
	frappe_only: list[dict[str, Any]] = []
	partner_only: list[dict[str, Any]] = []

	for frappe_record in operation_state.frappe_records:
		if id(frappe_record) in conflicted_frappe:
			continue
		partner_record, message = _resolve_identity_partner_for_frappe(config, frappe_record, lookup_state)
		if message:
			log_conflict(message, frappe_record, partner_record)
			continue
		if partner_record:
			if id(partner_record) in conflicted_partner:
				continue
			pairs[_identity_pair_key(config, frappe_record, partner_record)] = (frappe_record, partner_record)
		else:
			frappe_only.append(frappe_record)

	for partner_record in operation_state.partner_records:
		if id(partner_record) in conflicted_partner:
			continue
		frappe_record, message = _resolve_identity_frappe_for_partner(config, partner_record, lookup_state)
		if message:
			log_conflict(message, frappe_record, partner_record)
			continue
		if frappe_record:
			if id(frappe_record) in conflicted_frappe:
				continue
			pairs[_identity_pair_key(config, frappe_record, partner_record)] = (frappe_record, partner_record)
		else:
			partner_only.append(partner_record)

	for frappe_record, partner_record in pairs.values():
		_sync_bidirectional_identity_pair(
			run_doc=run_doc,
			config=config,
			connector=connector,
			stats=stats,
			dry_run=dry_run,
			last_successful_sync=last_successful_sync,
			frappe_record=frappe_record,
			partner_record=partner_record,
			mapping_context=mapping_context,
		)

	if config.delete_missing and full_sync:
		_delete_missing_identity_records(
			run_doc=run_doc,
			config=config,
			connector=connector,
			lookup_state=lookup_state,
			dry_run=dry_run,
			stats=stats,
			conflicted_frappe=conflicted_frappe,
			conflicted_partner=conflicted_partner,
		)
	else:
		for frappe_record in frappe_only:
			if _identity_frappe_partner_id(config, frappe_record) in (None, ""):
				_create_identity_partner_from_frappe(
					run_doc=run_doc,
					config=config,
					connector=connector,
					stats=stats,
					dry_run=dry_run,
					frappe_record=frappe_record,
					mapping_context=mapping_context,
				)
		for partner_record in partner_only:
			if _identity_partner_frappe_id(config, partner_record) in (None, ""):
				_create_identity_frappe_from_partner(
					run_doc=run_doc,
					config=config,
					connector=connector,
					stats=stats,
					dry_run=dry_run,
					partner_record=partner_record,
					mapping_context=mapping_context,
				)

	if config.delete_missing and full_sync:
		for frappe_record in frappe_only:
			if _identity_frappe_partner_id(config, frappe_record) in (None, ""):
				_create_identity_partner_from_frappe(
					run_doc=run_doc,
					config=config,
					connector=connector,
					stats=stats,
					dry_run=dry_run,
					frappe_record=frappe_record,
					mapping_context=mapping_context,
				)
		for partner_record in partner_only:
			if _identity_partner_frappe_id(config, partner_record) in (None, ""):
				_create_identity_frappe_from_partner(
					run_doc=run_doc,
					config=config,
					connector=connector,
					stats=stats,
					dry_run=dry_run,
					partner_record=partner_record,
					mapping_context=mapping_context,
				)

	_flush_pending_run_writes(run_doc)


def _validate_identity_field_config(config: SyncDefinitionConfig) -> None:
	missing = []
	if not _config_partner_identity_field(config):
		missing.append("Partner Identity Field")
	if not _config_frappe_partner_identity_field(config):
		missing.append("Frappe Partner Identity Field")
	if not _config_partner_frappe_identity_field(config):
		missing.append("Partner Frappe Identity Field")
	if missing:
		raise frappe.ValidationError("Identity Fields mode requires: " + ", ".join(missing) + ".")


def _identity_records(records: list[dict[str, Any]] | dict[Any, dict[str, Any]] | None) -> list[dict[str, Any]]:
	if not records:
		return []
	return list(records.values()) if isinstance(records, dict) else list(records)


def _build_identity_record_state(
	config: SyncDefinitionConfig,
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None,
	partner_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None,
) -> IdentityRecordState:
	frappe_list = _identity_records(frappe_records)
	partner_list = _identity_records(partner_records)
	duplicate_conflicts: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
	frappe_by_name = _identity_unique_index(
		frappe_list,
		lambda record: _identity_frappe_name(config, record),
		lambda key: f"Multiple Frappe records use the same Frappe ID {key!r}.",
		duplicate_conflicts,
		"frappe",
	)
	frappe_by_partner_id = _identity_unique_index(
		frappe_list,
		lambda record: _identity_frappe_partner_id(config, record),
		lambda key: f"Multiple Frappe records claim the same partner ID {key!r}.",
		duplicate_conflicts,
		"frappe",
	)
	partner_by_identity = _identity_unique_index(
		partner_list,
		lambda record: _identity_partner_identity(config, record),
		lambda key: f"Multiple partner records use the same partner ID {key!r}.",
		duplicate_conflicts,
		"partner",
	)
	partner_by_frappe_id = _identity_unique_index(
		partner_list,
		lambda record: _identity_partner_frappe_id(config, record),
		lambda key: f"Multiple partner records claim the same Frappe ID {key!r}.",
		duplicate_conflicts,
		"partner",
	)
	return IdentityRecordState(
		frappe_records=frappe_list,
		partner_records=partner_list,
		frappe_by_name=frappe_by_name,
		frappe_by_partner_id=frappe_by_partner_id,
		partner_by_identity=partner_by_identity,
		partner_by_frappe_id=partner_by_frappe_id,
		duplicate_conflicts=duplicate_conflicts,
	)


def _identity_unique_index(records, key_getter, message_getter, duplicate_conflicts, side: str) -> dict[Any, dict[str, Any]]:
	groups: dict[Any, list[dict[str, Any]]] = {}
	for record in records:
		key = _normalize_pairing_key_value(key_getter(record))
		if key in (None, ""):
			continue
		groups.setdefault(key, []).append(record)
	index: dict[Any, dict[str, Any]] = {}
	for key, group in groups.items():
		if len(group) == 1:
			index[key] = group[0]
			continue
		if side == "frappe":
			duplicate_conflicts.append((message_getter(key), group, []))
		else:
			duplicate_conflicts.append((message_getter(key), [], group))
	return index


def _identity_frappe_name(config: SyncDefinitionConfig, record: dict[str, Any] | None) -> Any:
	return (record or {}).get("name")


def _identity_frappe_partner_id(config: SyncDefinitionConfig, record: dict[str, Any] | None) -> Any:
	return _frappe_partner_identity_value(config, record)


def _identity_partner_identity(config: SyncDefinitionConfig, record: dict[str, Any] | None) -> Any:
	return _partner_identity_value(config, record)


def _identity_partner_frappe_id(config: SyncDefinitionConfig, record: dict[str, Any] | None) -> Any:
	fieldname = _config_partner_frappe_identity_field(config)
	if not fieldname or not record:
		return None
	return record.get(fieldname)


def _resolve_identity_partner_for_frappe(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	state: IdentityRecordState,
) -> tuple[dict[str, Any] | None, str | None]:
	frappe_name = _normalize_pairing_key_value(_identity_frappe_name(config, frappe_record))
	frappe_partner_id = _normalize_pairing_key_value(_identity_frappe_partner_id(config, frappe_record))
	by_partner_id = state.partner_by_identity.get(frappe_partner_id) if frappe_partner_id not in (None, "") else None
	by_frappe_id = state.partner_by_frappe_id.get(frappe_name) if frappe_name not in (None, "") else None
	if by_partner_id and by_frappe_id and by_partner_id is not by_frappe_id:
		return by_partner_id, (
			"Identity conflict: Frappe record points to one partner by partner ID, "
			"but another partner points back by Frappe ID."
		)
	partner_record = by_partner_id or by_frappe_id
	if not partner_record:
		return None, None
	partner_identity = _normalize_pairing_key_value(_identity_partner_identity(config, partner_record))
	partner_frappe_id = _normalize_pairing_key_value(_identity_partner_frappe_id(config, partner_record))
	if frappe_partner_id not in (None, "") and partner_identity not in (None, "") and frappe_partner_id != partner_identity:
		return partner_record, "Identity conflict: Frappe partner ID and partner own ID point to different partners."
	if frappe_name not in (None, "") and partner_frappe_id not in (None, "") and frappe_name != partner_frappe_id:
		return partner_record, "Identity conflict: Partner Frappe ID points to a different Frappe record."
	return partner_record, None


def _resolve_identity_frappe_for_partner(
	config: SyncDefinitionConfig,
	partner_record: dict[str, Any],
	state: IdentityRecordState,
) -> tuple[dict[str, Any] | None, str | None]:
	partner_identity = _normalize_pairing_key_value(_identity_partner_identity(config, partner_record))
	partner_frappe_id = _normalize_pairing_key_value(_identity_partner_frappe_id(config, partner_record))
	by_partner_id = state.frappe_by_partner_id.get(partner_identity) if partner_identity not in (None, "") else None
	by_frappe_id = state.frappe_by_name.get(partner_frappe_id) if partner_frappe_id not in (None, "") else None
	if by_partner_id and by_frappe_id and by_partner_id is not by_frappe_id:
		return by_frappe_id, (
			"Identity conflict: Partner record points to one Frappe record by Frappe ID, "
			"but another Frappe record points back by partner ID."
		)
	frappe_record = by_partner_id or by_frappe_id
	if not frappe_record:
		return None, None
	frappe_name = _normalize_pairing_key_value(_identity_frappe_name(config, frappe_record))
	frappe_partner_id = _normalize_pairing_key_value(_identity_frappe_partner_id(config, frappe_record))
	if partner_frappe_id not in (None, "") and frappe_name not in (None, "") and partner_frappe_id != frappe_name:
		return frappe_record, "Identity conflict: Partner Frappe ID and Frappe own ID point to different Frappe records."
	if partner_identity not in (None, "") and frappe_partner_id not in (None, "") and partner_identity != frappe_partner_id:
		return frappe_record, "Identity conflict: Frappe partner ID points to a different partner record."
	return frappe_record, None


def _identity_pair_key(config: SyncDefinitionConfig, frappe_record: dict[str, Any], partner_record: dict[str, Any]) -> tuple[Any, Any]:
	return (
		_normalize_pairing_key_value(_identity_frappe_name(config, frappe_record)) or id(frappe_record),
		_normalize_pairing_key_value(_identity_partner_identity(config, partner_record)) or id(partner_record),
	)


def _sync_bidirectional_identity_pair(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	stats: SyncStats,
	dry_run: bool,
	last_successful_sync: datetime | None,
	frappe_record: dict[str, Any],
	partner_record: dict[str, Any],
	mapping_context: RuntimeMappingContext,
) -> None:
	frappe_payload = _map_partner_to_frappe(
		partner_record,
		config.mapping,
		config.value_mapping,
		getattr(config, "value_mapping_fallbacks", None),
		doctype=getattr(config, "doctype", None),
		partner_time_zone=getattr(config, "partner_time_zone", None),
		mapping_context=mapping_context,
	)
	partner_payload = _map_frappe_to_partner(
		frappe_record,
		config.mapping,
		config.value_mapping,
		getattr(config, "value_mapping_fallbacks", None),
		doctype=getattr(config, "doctype", None),
		partner_time_zone=getattr(config, "partner_time_zone", None),
		mapping_context=mapping_context,
	)
	to_partner_changes = _diff_target_values(
		new_record=_apply_partner_link_fields(config, frappe_record, partner_payload),
		old_record=partner_record,
		field_names=list(_apply_partner_link_fields(config, frappe_record, partner_payload).keys()),
		exclude_fields={_config_partner_modified_field(config), _config_partner_creation_field(config)},
		datetime_fields=mapping_context.partner_datetime_fields,
		assumed_time_zone=getattr(config, "partner_time_zone", None),
		target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
	)
	frappe_partner_field = _config_frappe_partner_identity_field(config)
	partner_identity_field = _config_partner_identity_field(config)
	if frappe_partner_field and partner_identity_field and partner_record.get(partner_identity_field) not in (None, ""):
		frappe_payload = dict(frappe_payload)
		frappe_payload[frappe_partner_field] = partner_record.get(partner_identity_field)
	to_frappe_changes = _diff_target_values(
		new_record=frappe_payload,
		old_record=frappe_record,
		field_names=_frappe_diff_field_names(frappe_payload, mapping_context),
		exclude_fields={_config_frappe_modified_field(config), _config_frappe_creation_field(config)},
		datetime_fields=mapping_context.frappe_datetime_fields,
		target_time_zone=mapping_context.site_time_zone,
	)
	if not to_partner_changes and not to_frappe_changes:
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="skipped",
			status="skipped",
			message="No differences between both sides.",
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			commit=False,
		)
		return
	if not _update_existing_enabled(config):
		_log_update_existing_disabled(
			stats=stats,
			run_doc=run_doc,
			config=config,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_BIDIRECTIONAL,
			changes=_canonical_conflict_changes(
				config,
				to_frappe_changes=to_frappe_changes,
				to_partner_changes=to_partner_changes,
			),
			commit=False,
		)
		return
	frappe_changed_since_last = _record_changed_since(
		record=frappe_record,
		modified_fields=_config_frappe_modified_field(config),
		last_successful_sync=last_successful_sync,
		creation_field=_config_frappe_creation_field(config),
		target_time_zone=mapping_context.site_time_zone,
	)
	partner_changed_since_last = _record_changed_since(
		record=partner_record,
		modified_fields=_config_partner_modified_field(config),
		last_successful_sync=last_successful_sync,
		creation_field=_config_partner_creation_field(config),
		assumed_time_zone=getattr(config, "partner_time_zone", None),
		target_time_zone=mapping_context.site_time_zone,
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
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated partner from frappe.",
			commit=False,
			mapping_context=mapping_context,
		)
		return
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
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated frappe from partner.",
			commit=False,
		)
		return
	if config.conflict_policy != CONFLICT_POLICY_NEWEST_WINS:
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="conflict",
			status="conflict",
			message=f"Unsupported conflict policy: {config.conflict_policy}",
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			commit=False,
		)
		return
	frappe_latest = _latest_modified(
		record=frappe_record,
		modified_fields=_config_frappe_modified_field(config),
		creation_field=_config_frappe_creation_field(config),
		target_time_zone=mapping_context.site_time_zone,
	)
	partner_latest = _latest_modified(
		record=partner_record,
		modified_fields=_config_partner_modified_field(config),
		creation_field=_config_partner_creation_field(config),
		assumed_time_zone=getattr(config, "partner_time_zone", None),
		target_time_zone=mapping_context.site_time_zone,
	)
	timestamp_winner = _compare_modified_timestamps(frappe_latest, partner_latest, buffer_ms=_config_timestamp_buffer_ms(config))
	if timestamp_winner == "frappe":
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
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated partner from frappe with newest_wins.",
			commit=False,
			mapping_context=mapping_context,
		)
	elif timestamp_winner == "partner":
		_apply_frappe_update(
			run_doc=run_doc,
			config=config,
			stats=stats,
			dry_run=dry_run,
			frappe_record=frappe_record,
			partner_record=partner_record,
			frappe_payload=frappe_payload,
			changes=to_frappe_changes,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated frappe from partner with newest_wins.",
			commit=False,
		)
	else:
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="conflict",
			status="conflict",
			message="Manual conflict requires review; no write performed.",
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			changes=_canonical_conflict_changes(config, to_frappe_changes=to_frappe_changes, to_partner_changes=to_partner_changes),
			commit=False,
		)


def _create_identity_partner_from_frappe(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	stats: SyncStats,
	dry_run: bool,
	frappe_record: dict[str, Any],
	mapping_context: RuntimeMappingContext,
) -> None:
	if not config.create_new:
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="skipped",
			status="skipped",
			message="Create disabled and target record does not exist.",
			direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			frappe_record=frappe_record,
			partner_record=None,
			commit=False,
		)
		return
	partner_payload = _with_partner_timestamps(
		config,
		frappe_record,
		_apply_partner_link_fields(
			config,
			frappe_record,
			_map_frappe_to_partner(
				frappe_record,
				config.mapping,
				config.value_mapping,
				getattr(config, "value_mapping_fallbacks", None),
				doctype=getattr(config, "doctype", None),
				partner_time_zone=getattr(config, "partner_time_zone", None),
				mapping_context=mapping_context,
			),
		),
		create=True,
		mapping_context=mapping_context,
	)
	try:
		write = connector.upsert_record(
			record=partner_payload,
			key_values={},
			mapping=mapping_context.connector_mapping,
			dry_run=dry_run,
			source=config.table_name,
			create_options=_build_partner_create_options(config),
		)
		if not write.ok:
			raise RuntimeError(write.message or "Partner create failed.")
		_persist_frappe_partner_identity(config, frappe_record, write, dry_run=dry_run)
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="created",
			status="success",
			message="Dry run create." if dry_run else "Created partner record.",
			direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			frappe_record=frappe_record,
			partner_record=getattr(write, "record", None) or partner_payload,
			write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			written_after_record=getattr(write, "record", None) or partner_payload,
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
			direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			frappe_record=frappe_record,
			partner_record=partner_payload,
			write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			commit=False,
		)


def _create_identity_frappe_from_partner(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	stats: SyncStats,
	dry_run: bool,
	partner_record: dict[str, Any],
	mapping_context: RuntimeMappingContext,
) -> None:
	if not config.create_new:
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="skipped",
			status="skipped",
			message="Create disabled and target record does not exist.",
			direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			frappe_record=None,
			partner_record=partner_record,
			commit=False,
		)
		return
	frappe_payload = _map_partner_to_frappe(
		partner_record,
		config.mapping,
		config.value_mapping,
		getattr(config, "value_mapping_fallbacks", None),
		doctype=getattr(config, "doctype", None),
		partner_time_zone=getattr(config, "partner_time_zone", None),
		mapping_context=mapping_context,
	)
	frappe_partner_field = _config_frappe_partner_identity_field(config)
	partner_identity_field = _config_partner_identity_field(config)
	if frappe_partner_field and partner_identity_field and partner_record.get(partner_identity_field) not in (None, ""):
		frappe_payload[frappe_partner_field] = partner_record.get(partner_identity_field)
	frappe_payload = _with_frappe_modified_timestamp(
		config,
		partner_record,
		frappe_payload,
		mapping_context=mapping_context,
	)
	try:
		doc_name = _upsert_frappe_record(
			doctype=config.doctype,
			existing_name=None,
			payload=frappe_payload,
			dry_run=dry_run,
			**_frappe_write_action_kwargs(config),
		)
		if doc_name:
			frappe_payload["name"] = doc_name
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="created",
			status="success",
			message="Dry run create." if dry_run else "Created frappe record.",
			direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			frappe_record=frappe_payload,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			written_after_record=frappe_payload,
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
			direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			frappe_record=None,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			commit=False,
		)
		return
	try:
		_persist_partner_frappe_identity(config, connector, partner_record, frappe_payload.get("name"), dry_run=dry_run)
	except Exception as exc:
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="error",
			status="error",
			message=str(exc),
			direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			frappe_record=frappe_payload,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			commit=False,
		)


def _persist_partner_frappe_identity(
	config: SyncDefinitionConfig,
	connector: Any,
	partner_record: dict[str, Any],
	frappe_name: Any,
	*,
	dry_run: bool,
) -> None:
	partner_frappe_field = _config_partner_frappe_identity_field(config)
	partner_identity_field = _config_partner_identity_field(config)
	partner_id = partner_record.get(partner_identity_field) if partner_identity_field else None
	if dry_run or not partner_frappe_field or not partner_identity_field or frappe_name in (None, ""):
		return
	if partner_record.get(partner_frappe_field) == frappe_name:
		return
	if partner_id in (None, ""):
		raise RuntimeError("Partner Frappe ID write-back requires a partner identity value.")
	write = connector.upsert_record(
		record={partner_frappe_field: frappe_name},
		key_values={partner_identity_field: partner_id},
		mapping={partner_frappe_field: partner_frappe_field},
		dry_run=dry_run,
		source=config.table_name,
		create_options=_build_partner_create_options(config),
	)
	if not write.ok:
		raise RuntimeError(write.message or "Partner Frappe ID write-back failed.")
	partner_record[partner_frappe_field] = frappe_name


def _delete_missing_identity_records(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	lookup_state: IdentityRecordState,
	dry_run: bool,
	stats: SyncStats,
	conflicted_frappe: set[int],
	conflicted_partner: set[int],
) -> None:
	for frappe_record in lookup_state.frappe_records:
		if id(frappe_record) in conflicted_frappe:
			continue
		frappe_name = _normalize_pairing_key_value(_identity_frappe_name(config, frappe_record))
		frappe_partner_id = _normalize_pairing_key_value(_identity_frappe_partner_id(config, frappe_record))
		if frappe_name in (None, "") or frappe_partner_id in (None, ""):
			continue
		if lookup_state.partner_by_identity.get(frappe_partner_id) or lookup_state.partner_by_frappe_id.get(frappe_name):
			continue
		try:
			if not dry_run:
				frappe.delete_doc(config.doctype, frappe_record["name"], ignore_permissions=True, force=True)
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="deleted",
				status="success",
				message="Dry run delete." if dry_run else "Deleted frappe record whose identity-linked partner is missing.",
				direction=SYNC_TYPE_BIDIRECTIONAL,
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
				direction=SYNC_TYPE_BIDIRECTIONAL,
				frappe_record=frappe_record,
				partner_record=None,
				commit=False,
			)

	for partner_record in lookup_state.partner_records:
		if id(partner_record) in conflicted_partner:
			continue
		partner_identity = _normalize_pairing_key_value(_identity_partner_identity(config, partner_record))
		partner_frappe_id = _normalize_pairing_key_value(_identity_partner_frappe_id(config, partner_record))
		if partner_identity in (None, "") or partner_frappe_id in (None, ""):
			continue
		if lookup_state.frappe_by_name.get(partner_frappe_id) or lookup_state.frappe_by_partner_id.get(partner_identity):
			continue
		partner_identity_field = _config_partner_identity_field(config)
		try:
			write = connector.delete_record(
				key_values={partner_identity_field: partner_record.get(partner_identity_field)},
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
				message="Dry run delete." if dry_run else "Deleted partner record whose identity-linked Frappe record is missing.",
				direction=SYNC_TYPE_BIDIRECTIONAL,
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
				direction=SYNC_TYPE_BIDIRECTIONAL,
				frappe_record=None,
				partner_record=partner_record,
				commit=False,
			)


def _manual_conflict_resolution_payloads(
	*,
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	partner_record: dict[str, Any],
	frappe_payload: dict[str, Any],
	partner_payload: dict[str, Any],
	mapping_context: RuntimeMappingContext,
) -> tuple[dict[str, Any], dict[str, Any]]:
	frappe_resolution_payload = _with_frappe_modified_timestamp(
		config,
		partner_record,
		frappe_payload,
		mapping_context=mapping_context,
	)
	if frappe_record.get("name"):
		frappe_resolution_payload["name"] = frappe_record.get("name")
	partner_resolution_payload = _with_partner_timestamps(
		config,
		frappe_record,
		_apply_partner_link_fields(config, frappe_record, partner_payload),
		create=False,
		mapping_context=mapping_context,
	)
	return frappe_resolution_payload, partner_resolution_payload


def _canonical_conflict_changes(
	config: SyncDefinitionConfig | Any,
	*,
	to_frappe_changes: list[tuple[str, Any, Any]],
	to_partner_changes: list[tuple[str, Any, Any]],
) -> list[tuple[str, Any, Any]]:
	if to_frappe_changes:
		return to_frappe_changes
	partner_to_frappe = {
		entry["partner_field"]: frappe_field
		for frappe_field, entry in _iter_field_mapping_entries(getattr(config, "mapping", {}))
	}
	return [
		(partner_to_frappe.get(fieldname, fieldname), old_value, new_value)
		for fieldname, old_value, new_value in to_partner_changes
	]


def _log_update_existing_disabled(
	*,
	stats: SyncStats,
	run_doc: Any,
	config: SyncDefinitionConfig,
	direction: str,
	frappe_record: dict[str, Any] | None,
	partner_record: dict[str, Any] | None,
	write_direction: str,
	changes: list[tuple[str, Any, Any]] | None = None,
	commit: bool = True,
) -> None:
	_register_and_log(
		stats=stats,
		run_doc=run_doc,
		config=config,
		action="skipped",
		status="skipped",
		message="Update Existing is disabled; matched target record was not updated.",
		direction=direction,
		frappe_record=frappe_record,
		partner_record=partner_record,
		write_direction=write_direction,
		changes=changes,
		commit=commit,
	)


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
	mapping_context: RuntimeMappingContext | None = None,
):
	if not _update_existing_enabled(config):
		_log_update_existing_disabled(
			stats=stats,
			run_doc=run_doc,
			config=config,
			direction=direction,
			frappe_record=frappe_record,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			changes=changes,
			commit=commit,
		)
		return
	key = _key_tuple_from_frappe(frappe_record, _config_match_fields(config))
	mapping_context = mapping_context or _build_runtime_mapping_context(config)
	partner_payload = _with_partner_timestamps(
		config,
		frappe_record,
		_apply_partner_link_fields(config, frappe_record, partner_payload),
		create=False,
		mapping_context=mapping_context,
	)
	connector_mapping = (
		mapping_context.connector_mapping
		if mapping_context is not None
		else _flatten_mapping_for_direction(config.mapping, MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	)
	try:
		write = connector.upsert_record(
			record=partner_payload,
			key_values=_partner_key_values_for_existing_match(config, frappe_record, key, partner_record),
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
			write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			partner_before_record=partner_record,
			written_after_record=getattr(write, "record", None) or partner_payload,
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
			write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
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
	if not _update_existing_enabled(config):
		_log_update_existing_disabled(
			stats=stats,
			run_doc=run_doc,
			config=config,
			direction=direction,
			frappe_record=frappe_record,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			changes=changes,
			commit=commit,
		)
		return
	try:
		frappe_partner_field = _config_frappe_partner_identity_field(config)
		partner_identity_field = _config_partner_identity_field(config)
		if frappe_partner_field and partner_identity_field:
			partner_id = partner_record.get(partner_identity_field)
			if partner_id not in (None, ""):
				frappe_payload = dict(frappe_payload)
				frappe_payload[frappe_partner_field] = partner_id
		frappe_payload = _with_frappe_modified_timestamp(
			config,
			partner_record,
			frappe_payload,
			mapping_context=_build_runtime_mapping_context(config),
		)
		doc_name = _upsert_frappe_record(
			doctype=config.doctype,
			existing_name=(frappe_record or {}).get("name"),
			payload=frappe_payload,
			dry_run=dry_run,
			**_frappe_write_action_kwargs(config),
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
			write_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			frappe_before_record=frappe_record,
			written_after_record=frappe_payload,
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
			write_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			commit=commit,
		)


def _get_frappe_source_records(
	config: SyncDefinitionConfig,
	context: SyncContext,
	*,
	apply_delta_filter: bool = True,
) -> list[dict[str, Any]]:
	return [
		record
		for batch in _iter_frappe_source_batches(config, context, apply_delta_filter=apply_delta_filter)
		for record in batch
	]


def _iter_frappe_source_batches(
	config: SyncDefinitionConfig,
	context: SyncContext,
	*,
	apply_delta_filter: bool = True,
):
	doctype_fieldnames = _doctype_fieldnames(config.doctype)
	fields = sorted(
		_parent_mapping_fields_for_sync_type(config.mapping, config.sync_type)
		| set(_config_match_fields(config))
		| {_config_frappe_modified_field(config), _config_frappe_creation_field(config)}
		| {"name", "modified"}
		| ({_config_frappe_partner_identity_field(config)} if _config_frappe_partner_identity_field(config) else set())
	)
	if doctype_fieldnames is None:
		valid_fields = [field for field in fields if _doctype_has_field(config.doctype, field)]
	else:
		valid_fields = [field for field in fields if field in doctype_fieldnames]
	or_filters = None
	if apply_delta_filter and context.is_delta_sync:
		since = context.delta_since
		or_filters = []
		for timestamp_field in (
			_config_frappe_modified_field(config),
			_config_frappe_creation_field(config),
		):
			if (
				(timestamp_field in doctype_fieldnames)
				if doctype_fieldnames is not None
				else _doctype_has_field(config.doctype, timestamp_field)
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
	if not apply_delta_filter or not context.is_delta_sync:
		return record_batches

	def _filtered_batches():
		for batch in record_batches:
			filtered = [
				record
				for record in batch
				if _record_changed_since(
					record,
					_config_frappe_modified_field(config),
					context.delta_since,
					creation_field=_config_frappe_creation_field(config),
					target_time_zone=_site_time_zone(),
				)
			]
			if filtered:
				yield filtered

	return _filtered_batches()


def _with_configured_child_rows(
	config: SyncDefinitionConfig,
	record_batches: Any,
):
	table_fields = _child_table_fields_for_mapping(config.mapping, config.sync_type)
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
				rows.append(
					{
						key: value
						for key, value in vars(row).items()
						if not key.startswith("_")
					}
				)
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
		for batch in _iter_partner_source_batches(config, connector, context, apply_delta_filter=apply_delta_filter)
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
		query=_config_read_query(config),
		batch_size=config.batch_size,
		key_fields=_partner_fetch_key_fields(config),
	)
	if not apply_delta_filter or not context.is_delta_sync:
		return record_batches
	since = context.delta_since
	def _filtered_batches():
		for batch in record_batches:
			filtered = [
				record for record in batch
				if _record_changed_since(
					record,
					_config_partner_modified_field(config),
					since,
					creation_field=_config_partner_creation_field(config),
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
	after_insert_action: str | None = None,
	after_update_action: str | None = None,
) -> str | None:
	if dry_run:
		return existing_name
	mapped_modified = payload["modified"] if "modified" in payload else AUDIT_RECORD_UNSET
	doctype_fieldnames = _doctype_fieldnames(doctype)
	insert_action = _normalize_frappe_write_action(after_insert_action)
	update_action = _normalize_frappe_write_action(after_update_action)
	write_action = update_action if existing_name else insert_action
	with _frappe_write_savepoint(enabled=write_action != FRAPPE_WRITE_ACTION_NONE):
		if existing_name:
			doc = frappe.get_doc(doctype, existing_name)
			for key, value in payload.items():
				if key in SYSTEM_KEYS:
					continue
				if _doctype_payload_allows_field(doctype, key, doctype_fieldnames):
					_set_frappe_doc_payload_value(doc, doctype, key, value, merge_child_rows=True)
			doc.save(ignore_permissions=True)
			_apply_frappe_write_action(doc, update_action)
			_set_mapped_frappe_modified(doctype, doc.name, mapped_modified)
			return doc.name

		doc = frappe.new_doc(doctype)
		for key, value in payload.items():
			if key in SYSTEM_KEYS:
				continue
			if _doctype_payload_allows_field(doctype, key, doctype_fieldnames):
				_set_frappe_doc_payload_value(doc, doctype, key, value, merge_child_rows=False)
		doc.insert(ignore_permissions=True)
		_apply_frappe_write_action(doc, insert_action)
		_set_mapped_frappe_modified(doctype, doc.name, mapped_modified)
		return doc.name


@contextmanager
def _frappe_write_savepoint(*, enabled: bool = True):
	if not enabled:
		yield
		return
	try:
		db = getattr(frappe, "db", None)
		savepoint = getattr(db, "savepoint", None)
	except Exception:
		db = None
		savepoint = None
	if not callable(savepoint):
		yield
		return
	name = _new_frappe_write_savepoint_name()
	savepoint(name)
	try:
		yield
	except Exception:
		rollback = getattr(db, "rollback", None)
		if callable(rollback):
			rollback(save_point=name)
		raise
	else:
		release = getattr(db, "release_savepoint", None)
		if callable(release):
			release(name)


def _new_frappe_write_savepoint_name() -> str:
	try:
		return f"sync_frappe_write_{frappe.generate_hash(length=8)}"
	except Exception:
		return "sync_frappe_write"


def _apply_frappe_write_action(doc: Any, action: str) -> None:
	if action != FRAPPE_WRITE_ACTION_SUBMIT:
		return
	docstatus = _docstatus_value(doc)
	if docstatus == 1:
		return
	if docstatus == 2:
		raise frappe.ValidationError("Cannot submit a cancelled document.")
	submit = getattr(doc, "submit", None)
	if not callable(submit):
		raise frappe.ValidationError("Frappe document does not support submit.")
	submit()


def _docstatus_value(doc: Any) -> int:
	value = getattr(doc, "docstatus", None)
	if value is None and hasattr(doc, "get"):
		value = doc.get("docstatus", 0)
	try:
		return cint(value)
	except Exception:
		return 0


def _set_frappe_doc_payload_value(
	doc: Any,
	doctype: str,
	fieldname: str,
	value: Any,
	*,
	merge_child_rows: bool,
) -> None:
	table_fields = _doctype_table_fields(doctype)
	if fieldname not in table_fields or not isinstance(value, list):
		doc.set(fieldname, value)
		return
	if not merge_child_rows:
		doc.set(fieldname, _prepare_child_row_payloads(table_fields[fieldname], value))
		return

	existing_rows = []
	for row in getattr(doc, fieldname, None) or []:
		if hasattr(row, "as_dict"):
			existing_rows.append(row.as_dict())
		elif isinstance(row, dict):
			existing_rows.append(dict(row))
		else:
			existing_rows.append(dict(vars(row)))
	for index, incoming in enumerate(value):
		if not isinstance(incoming, dict):
			continue
		prepared = _prepare_child_row_payload(table_fields[fieldname], incoming)
		if not _child_row_has_payload_value(prepared):
			continue
		while len(existing_rows) <= index:
			existing_rows.append({})
		merged = dict(existing_rows[index])
		merged.update(prepared)
		existing_rows[index] = merged
	doc.set(fieldname, existing_rows)


def _prepare_child_row_payloads(child_doctype: str | None, rows: list[Any]) -> list[dict[str, Any]]:
	return [
		prepared
		for row in rows
		if isinstance(row, dict)
		for prepared in [_prepare_child_row_payload(child_doctype, row)]
		if _child_row_has_payload_value(prepared)
	]


def _prepare_child_row_payload(child_doctype: str | None, row: dict[str, Any]) -> dict[str, Any]:
	result = dict(row)
	if child_doctype:
		result.setdefault("doctype", child_doctype)
	return result


def _child_row_has_payload_value(row: dict[str, Any]) -> bool:
	for key, value in row.items():
		if key in SYSTEM_KEYS or key in {"doctype", "parent", "parenttype", "parentfield"}:
			continue
		if value not in (None, ""):
			return True
	return False


def _resolve_item_to_frappe(item_doc: Any, config: SyncDefinitionConfig) -> dict[str, Any]:
	if not _update_existing_enabled(config):
		raise frappe.ValidationError("Update Existing is disabled for this Sync Definition.")
	payload = _json_field_payload(item_doc, "frappe_resolution_payload")
	existing_name = _clean_string(getattr(item_doc, "document_name", None)) or _clean_string(payload.get("name"))
	if not existing_name:
		raise frappe.ValidationError("Sync Run Item is missing the Frappe document name.")
	doc_name = _upsert_frappe_record(
		doctype=config.doctype,
		existing_name=existing_name,
		payload=payload,
		dry_run=False,
		**_frappe_write_action_kwargs(config),
	)
	result = dict(payload)
	if doc_name:
		result["name"] = doc_name
	return result


def _resolve_item_to_partner(item_doc: Any, config: SyncDefinitionConfig) -> dict[str, Any]:
	if not _update_existing_enabled(config):
		raise frappe.ValidationError("Update Existing is disabled for this Sync Definition.")
	payload = _json_field_payload(item_doc, "partner_resolution_payload")
	key_values = _manual_partner_key_values(config, payload)
	mapping_context = _build_runtime_mapping_context(config)
	connector = get_connector_for_partner(frappe.get_doc(SYNC_PARTNER, config.partner))
	write = connector.upsert_record(
		record=payload,
		key_values=key_values,
		mapping=mapping_context.connector_mapping,
		dry_run=False,
		source=config.table_name,
		create_options=_build_partner_create_options(config),
	)
	if not getattr(write, "ok", False):
		raise RuntimeError(getattr(write, "message", None) or "Partner upsert failed.")
	return dict(getattr(write, "record", None) or payload)


def _json_field_payload(doc: Any, fieldname: str) -> dict[str, Any]:
	raw = getattr(doc, fieldname, None)
	if not raw:
		raise frappe.ValidationError(f"Sync Run Item is missing {fieldname}.")
	try:
		payload = json.loads(raw)
	except Exception as exc:
		raise frappe.ValidationError(f"Sync Run Item has invalid {fieldname}.") from exc
	if not isinstance(payload, dict) or not payload:
		raise frappe.ValidationError(f"Sync Run Item has no usable {fieldname}.")
	return payload


def _manual_partner_key_values(config: SyncDefinitionConfig, payload: dict[str, Any]) -> dict[str, Any]:
	partner_identity_field = _config_partner_identity_field(config)
	if partner_identity_field and payload.get(partner_identity_field) not in (None, ""):
		return {partner_identity_field: payload.get(partner_identity_field)}
	key_values = {}
	for frappe_field in _config_match_fields(config):
		partner_field = _partner_field_for_mapping(config.mapping, frappe_field, frappe_field)
		value = payload.get(partner_field)
		if value in (None, ""):
			raise frappe.ValidationError(f"Manual resolution payload is missing partner key field {partner_field}.")
		key_values[partner_field] = value
	if not key_values:
		raise frappe.ValidationError("Manual resolution payload has no partner key values.")
	return key_values


def _set_mapped_frappe_modified(doctype: str, name: str | None, value: Any) -> None:
	if value is AUDIT_RECORD_UNSET or not name:
		return
	modified = _normalize_mapped_frappe_modified(value)
	frappe.db.set_value(doctype, name, "modified", modified, update_modified=False)


def _normalize_mapped_frappe_modified(value: Any) -> Any:
	parsed = _parse_datetime(value, target_time_zone=_site_time_zone())
	return parsed if parsed is not None else value


def _build_definition_config(sync_definition_doc: Any) -> SyncDefinitionConfig:
	doctype = _first_value(sync_definition_doc, ["doctype_name"])
	if not doctype:
		raise frappe.ValidationError("Sync Definition is missing target DocType field.")

	partner = _first_value(sync_definition_doc, ["partner"])
	if not partner:
		raise frappe.ValidationError("Sync Definition is missing Sync Partner reference.")

	sync_type = _first_value(sync_definition_doc, ["sync_type"], default="Frappe -> Partner")
	cron_expr = _first_value(sync_definition_doc, ["frequency_cron"])
	filters = _parse_filter_expression(_first_value(sync_definition_doc, ["filter_expression"]))
	batch_size = cint(_first_value(sync_definition_doc, ["batch_size"], default=100)) or 100
	create_new = _as_bool(_first_value(sync_definition_doc, ["create_new"], default=1))
	match_mode = _normalize_match_mode(_first_value(sync_definition_doc, ["match_mode"], default=MATCH_MODE_MATCH_FIELDS))
	delete_missing = _delete_missing_enabled(
		sync_type,
		_first_value(sync_definition_doc, ["delete_missing"], default=0),
		match_mode=match_mode,
	)
	use_last_sync_date = _as_bool(_first_value(sync_definition_doc, ["use_last_sync_date"], default=1))
	conflict_policy = str(_first_value(sync_definition_doc, ["conflict_policy"], default=CONFLICT_POLICY_NEWEST_WINS))
	timestamp_buffer_ms = _coerce_timestamp_buffer_ms(_first_value(sync_definition_doc, ["timestamp_buffer_ms"]))

	match_fields = _get_match_fields(sync_definition_doc)
	mapping = _get_field_mapping(sync_definition_doc)
	mapping = _force_mapping_direction(mapping, sync_type)
	value_mapping = _get_value_mapping(sync_definition_doc)
	value_mapping_fallbacks = _get_value_mapping_fallbacks(sync_definition_doc)
	if not mapping:
		raise frappe.ValidationError("Sync Definition has no field mapping entries.")
	if not match_fields and match_mode == MATCH_MODE_MATCH_FIELDS:
		match_fields = [next(iter(mapping.keys()))]

	frappe_modified_field = _clean_string(_first_value(sync_definition_doc, ["frappe_modified_field"])) or "modified"
	frappe_creation_field = _clean_string(_first_value(sync_definition_doc, ["frappe_creation_field"])) or "creation"
	partner_modified_field = _clean_string(_first_value(sync_definition_doc, ["partner_modified_field"]))
	partner_creation_field = _clean_string(_first_value(sync_definition_doc, ["partner_creation_field"]))
	timestamp_tie_breaker = _normalize_timestamp_tie_breaker(
		_clean_string(_first_value(sync_definition_doc, ["timestamp_tie_breaker"]))
	)
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
		one_way_match_mode=_clean_string(_first_value(sync_definition_doc, ["one_way_match_mode"])) or ONE_WAY_MATCH_FIRST,
		update_existing=_as_bool(_first_value(sync_definition_doc, ["update_existing"], default=1)),
		frappe_after_insert_action=_normalize_frappe_write_action(
			_first_value(sync_definition_doc, ["frappe_after_insert_action"])
		),
		frappe_after_update_action=_normalize_frappe_write_action(
			_first_value(sync_definition_doc, ["frappe_after_update_action"])
		),
		use_last_sync_date=use_last_sync_date,
		conflict_policy=conflict_policy,
		timestamp_buffer_ms=timestamp_buffer_ms,
		table_name=_clean_string(_first_value(sync_definition_doc, ["table_name"])),
		read_query=_clean_string(_first_value(sync_definition_doc, ["read_query"])),
		match_fields=match_fields,
		mapping=mapping,
		value_mapping=value_mapping,
		match_mode=match_mode,
		frappe_modified_field=frappe_modified_field,
		frappe_creation_field=frappe_creation_field,
		partner_modified_field=partner_modified_field,
		partner_creation_field=partner_creation_field,
		timestamp_tie_breaker=timestamp_tie_breaker,
		value_mapping_fallbacks=value_mapping_fallbacks,
		partner_identity_field=_clean_string(_first_value(sync_definition_doc, ["partner_identity_field"])),
		frappe_partner_identity_field=_clean_string(_first_value(sync_definition_doc, ["frappe_partner_identity_field"])),
		partner_frappe_identity_field=_clean_string(_first_value(sync_definition_doc, ["partner_frappe_identity_field"])),
		partner_create_id_strategy=_clean_string(_first_value(sync_definition_doc, ["partner_create_id_strategy"])) or "payload",
		partner_create_id_source=_clean_string(_first_value(sync_definition_doc, ["partner_create_id_source"])),
		partner_create_id_scope_where=_clean_string(_first_value(sync_definition_doc, ["partner_create_id_scope_where"])),
		capture_audit_payloads=_as_bool(_first_value(sync_definition_doc, ["capture_audit_payloads"], default=0)),
	)
	if not config.table_name and not _read_query_can_replace_table_name(config.sync_type, config.read_query):
		raise frappe.ValidationError("Table Name is required.")
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
	sync_type = _clean_string(_first_value(sync_definition_doc, ["sync_type"]))
	for row in rows:
		frappe_field = _field_mapping_row_fieldname(row)
		entry = _normalize_field_mapping_entry(row, sync_type=sync_type)
		if frappe_field and entry:
			mapping[frappe_field] = entry
	top_level = _first_value(sync_definition_doc, ["field_mapping"])
	if not mapping and isinstance(top_level, str):
		mapping = _normalize_field_mapping(top_level)
	if not mapping and isinstance(top_level, dict):
		mapping = _normalize_field_mapping(top_level)
	if _one_way_mapping_direction(sync_type):
		mapping = _force_mapping_direction(mapping, sync_type)
	return mapping


def _field_mapping_row_fieldname(row: Any) -> str | None:
	table_field = _clean_string(_first_value_dict(row, ["table_field"]))
	row_idx = _clean_string(_first_value_dict(row, ["row_idx", "child_row_idx"]))
	child_field = _clean_string(_first_value_dict(row, ["child_field"]))
	if table_field and row_idx and child_field:
		return CHILD_FIELD_PATH_SEPARATOR.join((table_field, row_idx, child_field))
	return _clean_string(
		_first_value_dict(
			row,
			["frappe_field", "source_field", "doctype_field", "source_fieldname", "field_name"],
		)
	)


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
	if normalized in {"frappe <-> partner", "bidirectional"}:
		return MAPPING_DIRECTION_BOTH
	if normalized in {"frappe -> partner", "frappe_to_partner"}:
		return MAPPING_DIRECTION_FRAPPE_TO_PARTNER
	if normalized in {"frappe <- partner", "partner_to_frappe"}:
		return MAPPING_DIRECTION_PARTNER_TO_FRAPPE
	return value


def _one_way_mapping_direction(sync_type: Any) -> str | None:
	sync_type = _clean_string(sync_type)
	if sync_type in {MAPPING_DIRECTION_FRAPPE_TO_PARTNER, MAPPING_DIRECTION_PARTNER_TO_FRAPPE}:
		return sync_type
	return None


def _read_query_can_replace_table_name(sync_type: Any, read_query: Any) -> bool:
	return _one_way_mapping_direction(sync_type) == MAPPING_DIRECTION_PARTNER_TO_FRAPPE and bool(_clean_string(read_query))


def _delete_missing_enabled(sync_type: Any, value: Any, *, match_mode: Any = MATCH_MODE_MATCH_FIELDS) -> bool:
	if _one_way_mapping_direction(sync_type):
		return _as_bool(value)
	return (
		_clean_string(sync_type) == SYNC_TYPE_BIDIRECTIONAL
		and _normalize_match_mode(match_mode) == MATCH_MODE_IDENTITY_FIELDS
		and _as_bool(value)
	)


def _normalize_match_mode(value: Any) -> str:
	mode = _clean_string(value) or MATCH_MODE_MATCH_FIELDS
	if mode not in MATCH_MODES:
		raise frappe.ValidationError(f"Match Mode must be one of: {', '.join(sorted(MATCH_MODES))}.")
	return mode


def _normalize_field_mapping_entry(raw_entry: Any, *, sync_type: str | None = None) -> dict[str, str] | None:
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
		"direction": _one_way_mapping_direction(sync_type) or _normalize_mapping_direction(direction),
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


def _force_mapping_direction(mapping: dict[str, Any], sync_type: str | None) -> dict[str, dict[str, str]]:
	direction = _one_way_mapping_direction(sync_type)
	if not direction:
		return _normalize_field_mapping(mapping)
	return {
		frappe_field: {"partner_field": entry["partner_field"], "direction": direction}
		for frappe_field, entry in _iter_field_mapping_entries(mapping)
	}


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


def _parent_mapping_fields_for_sync_type(mapping: dict[str, Any], sync_type: str) -> set[str]:
	return {
		fieldname
		for fieldname in _mapping_fields_for_sync_type(mapping, sync_type)
		if not _parse_child_field_path(fieldname)
	}


def _parse_child_field_path(fieldname: Any) -> tuple[str, int, str] | None:
	cleaned = _clean_string(fieldname)
	if not cleaned:
		return None
	parts = cleaned.split(CHILD_FIELD_PATH_SEPARATOR)
	if len(parts) != 3:
		return None
	table_field, row_idx, child_field = (_clean_string(part) for part in parts)
	if not table_field or not row_idx or not child_field:
		return None
	try:
		row_number = int(row_idx)
	except Exception:
		return None
	if row_number < 1:
		return None
	return table_field, row_number, child_field


def _child_table_fields_for_mapping(mapping: dict[str, Any], sync_type: str | None = None) -> set[str]:
	fields = _mapping_fields_for_sync_type(mapping, sync_type or SYNC_TYPE_BIDIRECTIONAL)
	result: set[str] = set()
	for fieldname in fields:
		parsed = _parse_child_field_path(fieldname)
		if parsed:
			result.add(parsed[0])
	return result


def _required_mapping_directions(sync_type: str) -> list[str]:
	required: list[str] = []
	if sync_type in {"Frappe -> Partner", "Frappe <-> Partner"}:
		required.append(MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	if sync_type in {"Frappe <- Partner", "Frappe <-> Partner"}:
		required.append(MAPPING_DIRECTION_PARTNER_TO_FRAPPE)
	if not required:
		required.append(MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	return required


def _validate_runtime_mapping(config: SyncDefinitionConfig) -> None:
	mapping = _normalize_field_mapping(config.mapping)
	if _config_match_mode(config) == MATCH_MODE_MATCH_FIELDS and mapping and not _config_match_fields(config):
		raise frappe.ValidationError("Match fields are required in Match Fields mode.")
	if _config_match_mode(config) == MATCH_MODE_IDENTITY_FIELDS:
		_validate_identity_field_config(config)
	_validate_child_mapping_paths(config, mapping)
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
	frappe_modified_field = _config_frappe_modified_field(config)
	frappe_creation_field = _config_frappe_creation_field(config)
	partner_modified_field = _config_partner_modified_field(config)
	partner_creation_field = _config_partner_creation_field(config)
	partner_timestamps_required = _partner_timestamps_required(config)
	if partner_timestamps_required and not partner_modified_field:
		raise frappe.ValidationError("Partner Modified Field is required.")
	if partner_timestamps_required and not partner_creation_field:
		raise frappe.ValidationError("Partner Creation Field is required.")
	if not partner_modified_field and not partner_creation_field:
		partner_timestamp_fields = set()
	elif not partner_modified_field or not partner_creation_field:
		partner_timestamp_fields = {field for field in (partner_modified_field, partner_creation_field) if field}
	else:
		partner_timestamp_fields = {partner_modified_field, partner_creation_field}
	if partner_timestamps_required and not partner_timestamp_fields:
		raise frappe.ValidationError("Partner Modified Field and Partner Creation Field are required.")
	if frappe_modified_field == frappe_creation_field:
		raise frappe.ValidationError("Frappe Modified Field and Frappe Creation Field must be different.")
	if partner_modified_field and partner_creation_field and partner_modified_field == partner_creation_field:
		raise frappe.ValidationError("Partner Modified Field and Partner Creation Field must be different.")
	if _config_timestamp_tie_breaker(config) not in TIMESTAMP_TIE_BREAKERS:
		raise frappe.ValidationError("Unsupported Timestamp Tie Breaker.")
	mapped_frappe_fields = set(mapping)
	mapped_partner_fields = {entry["partner_field"] for entry in mapping.values()}
	duplicate_frappe = {frappe_modified_field, frappe_creation_field} & mapped_frappe_fields
	duplicate_partner = partner_timestamp_fields & mapped_partner_fields
	if duplicate_frappe or duplicate_partner:
		raise frappe.ValidationError("Dedicated timestamp fields must not also exist in Field Mapping.")


def _validate_child_mapping_paths(config: SyncDefinitionConfig, mapping: dict[str, dict[str, str]]) -> None:
	child_paths = [fieldname for fieldname in mapping if _parse_child_field_path(fieldname)]
	if not child_paths:
		return
	table_fields = _doctype_table_fields(getattr(config, "doctype", None))
	for fieldname in child_paths:
		parsed = _parse_child_field_path(fieldname)
		if not parsed:
			continue
		table_field, row_idx, child_field = parsed
		child_doctype = table_fields.get(table_field)
		if not child_doctype:
			raise frappe.ValidationError(f"Child mapping table field does not exist: {table_field}.")
		if row_idx < 1:
			raise frappe.ValidationError(f"Child mapping row index must be greater than zero: {fieldname}.")
		fieldtype = _doctype_fieldtype(child_doctype, child_field)
		if not fieldtype:
			raise frappe.ValidationError(f"Child mapping field does not exist: {fieldname}.")
		if fieldtype in {"Table", "Table MultiSelect"}:
			raise frappe.ValidationError(f"Child mapping field cannot be a table field: {fieldname}.")


def _first_configured_field(values: Any, default: str | None) -> str | None:
	for value in values or []:
		cleaned = _clean_string(value)
		if cleaned:
			return cleaned
	return default


def _get_value_mapping(sync_definition_doc: Any) -> dict[str, dict[Any, Any]]:
	rows = _get_child_rows_by_options(sync_definition_doc, "Sync Value Mapping")
	result: dict[str, dict[Any, Any]] = {}
	for row in rows:
		frappe_field = _first_value_dict(row, ["frappe_field", "field_name", "source_field"])
		source_is_null = _as_bool(
			_first_value_dict(row, ["source_value_is_null", "frappe_value_is_null"], default=False)
		)
		target_is_null = _as_bool(
			_first_value_dict(row, ["target_value_is_null", "partner_value_is_null"], default=False)
		)
		source_value = (
			None
			if source_is_null
			else _first_value_dict(row, ["source_value", "frappe_value", "from_value"])
		)
		target_value = (
			None
			if target_is_null
			else _first_value_dict(row, ["target_value", "partner_value", "to_value"])
		)
		if frappe_field is None or (source_value is None and not source_is_null):
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


def _get_value_mapping_fallbacks(sync_definition_doc: Any) -> dict[str, dict[str, Any]]:
	rows = _get_child_rows_by_options(sync_definition_doc, "Sync Field Mapping")
	result: dict[str, dict[str, Any]] = {}
	for row in rows:
		frappe_field = _field_mapping_row_fieldname(row)
		if not frappe_field:
			continue
		result[frappe_field] = _normalize_value_mapping_fallback(
			_first_value_dict(row, ["unmapped_action"]),
			_first_value_dict(row, ["fallback_value"]),
		)

	top_level = _first_value(sync_definition_doc, ["value_mapping_fallbacks"])
	if not result:
		return _normalize_value_mapping_fallbacks(top_level)
	return result


def _normalize_value_mapping_fallbacks(raw_fallbacks: Any) -> dict[str, dict[str, Any]]:
	if isinstance(raw_fallbacks, str):
		try:
			raw_fallbacks = json.loads(raw_fallbacks)
		except Exception:
			return {}
	if not isinstance(raw_fallbacks, dict):
		return {}

	result: dict[str, dict[str, Any]] = {}
	for frappe_field, raw_field_fallbacks in raw_fallbacks.items():
		frappe_field = _clean_string(frappe_field)
		if not frappe_field or not isinstance(raw_field_fallbacks, dict):
			continue
		if "action" in raw_field_fallbacks:
			result[frappe_field] = _normalize_value_mapping_fallback(
				raw_field_fallbacks.get("action"),
				raw_field_fallbacks.get("value"),
			)
	return result


def _normalize_value_mapping_fallback(action: Any, value: Any = None) -> dict[str, Any]:
	normalized_action = _normalize_value_mapping_fallback_action(action)
	if normalized_action == VALUE_MAPPING_FALLBACK_KEEP_ORIGINAL:
		return {"action": normalized_action, "value": None}
	if normalized_action == VALUE_MAPPING_FALLBACK_USE_NULL:
		return {"action": normalized_action, "value": None}
	return {"action": normalized_action, "value": value}


def _normalize_value_mapping_fallback_action(action: Any) -> str:
	cleaned = _clean_string(action)
	if not cleaned:
		return VALUE_MAPPING_FALLBACK_KEEP_ORIGINAL
	normalized = cleaned.lower().replace("-", "_").replace(" ", "_")
	aliases = {
		"keep_original": VALUE_MAPPING_FALLBACK_KEEP_ORIGINAL,
		"fallback": VALUE_MAPPING_FALLBACK_USE_FALLBACK,
		"use_fallback_value": VALUE_MAPPING_FALLBACK_USE_FALLBACK,
		"null": VALUE_MAPPING_FALLBACK_USE_NULL,
		"use_null": VALUE_MAPPING_FALLBACK_USE_NULL,
	}
	result = aliases.get(normalized)
	if result:
		return result
	if cleaned in VALUE_MAPPING_FALLBACK_ACTIONS:
		return cleaned
	raise frappe.ValidationError(
		"Value Mapping fallback action must be one of: keep_original, fallback, null."
	)


def _build_runtime_mapping_context(config: SyncDefinitionConfig | Any) -> RuntimeMappingContext:
	mapping = _normalize_field_mapping(getattr(config, "mapping", {}) or {})
	value_mapping = dict(getattr(config, "value_mapping", {}) or {})
	value_mapping_fallbacks = _normalize_value_mapping_fallbacks(
		getattr(config, "value_mapping_fallbacks", {}) or {}
	)
	child_table_options = _doctype_table_fields(getattr(config, "doctype", None))
	to_partner_entries = tuple(
		(frappe_field, entry["partner_field"])
		for frappe_field, entry in _iter_field_mapping_entries(mapping)
		if _mapping_allows_direction(entry, MAPPING_DIRECTION_FRAPPE_TO_PARTNER)
	)
	to_frappe_entries = tuple(
		(frappe_field, entry["partner_field"])
		for frappe_field, entry in _iter_field_mapping_entries(mapping)
		if _mapping_allows_direction(entry, MAPPING_DIRECTION_PARTNER_TO_FRAPPE)
	)
	frappe_fields = set(mapping.keys()) | {
		_config_frappe_modified_field(config),
		_config_frappe_creation_field(config),
	}
	frappe_datetime_fields = _get_frappe_datetime_fields(getattr(config, "doctype", None), frappe_fields)
	partner_datetime_fields = {
		field
		for field in (
			_config_partner_modified_field(config),
			_config_partner_creation_field(config),
		)
		if field
	}
	for frappe_field in frappe_datetime_fields:
		partner_field = _partner_field_for_mapping(mapping, frappe_field, frappe_field)
		if partner_field:
			partner_datetime_fields.add(partner_field)
	return RuntimeMappingContext(
		mapping=mapping,
		value_mapping=value_mapping,
		value_mapping_fallbacks=value_mapping_fallbacks,
		to_partner_entries=to_partner_entries,
		to_frappe_entries=to_frappe_entries,
		connector_mapping=dict(to_partner_entries),
		reverse_value_mapping=_build_reverse_value_mapping(value_mapping),
		frappe_datetime_fields=frappe_datetime_fields,
		partner_datetime_fields=partner_datetime_fields,
		frappe_fieldnames=_doctype_fieldnames(getattr(config, "doctype", None)),
		child_table_options=child_table_options,
		site_time_zone=_site_time_zone(),
		partner_time_zone=getattr(config, "partner_time_zone", None),
	)


def _build_reverse_value_mapping(value_mapping: dict[str, dict[Any, Any]]) -> dict[str, dict[Any, Any]]:
	result: dict[str, dict[Any, Any]] = {}
	for frappe_field, field_map in value_mapping.items():
		if not isinstance(field_map, dict):
			continue
		reverse_map = {}
		for source_value, mapped_value in field_map.items():
			try:
				reverse_map[mapped_value] = source_value
			except TypeError:
				continue
		result[frappe_field] = reverse_map
	return result


def _build_ad_hoc_mapping_context(
	*,
	mapping: dict[str, Any],
	value_mapping: dict[str, dict[Any, Any]],
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None,
	doctype: str | None,
	partner_time_zone: str | None,
) -> RuntimeMappingContext:
	return _build_runtime_mapping_context(
		SimpleNamespace(
			doctype=doctype,
			mapping=mapping,
			value_mapping=value_mapping,
			value_mapping_fallbacks=value_mapping_fallbacks,
			frappe_modified_fields=[],
			partner_modified_fields=[],
			partner_time_zone=partner_time_zone,
		)
	)


def _map_frappe_to_partner(
	record: dict[str, Any],
	mapping: dict[str, Any],
	value_mapping: dict[str, dict[Any, Any]],
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None = None,
	*,
	doctype: str | None = None,
	partner_time_zone: str | None = None,
	mapping_context: RuntimeMappingContext | None = None,
) -> dict[str, Any]:
	result: dict[str, Any] = {}
	context = mapping_context or _build_ad_hoc_mapping_context(
		mapping=mapping,
		value_mapping=value_mapping,
		value_mapping_fallbacks=value_mapping_fallbacks,
		doctype=doctype,
		partner_time_zone=partner_time_zone,
	)
	for frappe_field, partner_field in context.to_partner_entries:
		value = _get_frappe_payload_value(record, frappe_field)
		field_map = context.value_mapping.get(frappe_field) or {}
		value = _mapped_value_with_fallback(
			field_map,
			value,
			_value_mapping_fallback_for_direction(
				context.value_mapping_fallbacks,
				frappe_field,
				MAPPING_DIRECTION_FRAPPE_TO_PARTNER,
			),
		)
		if frappe_field in context.frappe_datetime_fields:
			value = _convert_datetime_between_time_zones(
				value,
				source_time_zone=context.site_time_zone,
				target_time_zone=context.partner_time_zone or context.site_time_zone,
			)
		result[partner_field] = value
	return result


def _map_partner_to_frappe(
	record: dict[str, Any],
	mapping: dict[str, Any],
	value_mapping: dict[str, dict[Any, Any]],
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None = None,
	*,
	doctype: str | None = None,
	partner_time_zone: str | None = None,
	mapping_context: RuntimeMappingContext | None = None,
) -> dict[str, Any]:
	result: dict[str, Any] = {}
	context = mapping_context or _build_ad_hoc_mapping_context(
		mapping=mapping,
		value_mapping=value_mapping,
		value_mapping_fallbacks=value_mapping_fallbacks,
		doctype=doctype,
		partner_time_zone=partner_time_zone,
	)
	for frappe_field, partner_field in context.to_frappe_entries:
		value = record.get(partner_field)
		reverse_map = context.reverse_value_mapping.get(frappe_field) or {}
		value = _mapped_value_with_fallback(
			reverse_map,
			value,
			_value_mapping_fallback_for_direction(
				context.value_mapping_fallbacks,
				frappe_field,
				MAPPING_DIRECTION_PARTNER_TO_FRAPPE,
			),
		)
		if frappe_field in context.frappe_datetime_fields:
			value = _convert_datetime_between_time_zones(
				value,
				source_time_zone=context.partner_time_zone,
				target_time_zone=context.site_time_zone,
			)
		_set_frappe_payload_value(result, frappe_field, value, mapping_context=context)
	return result


def _get_frappe_payload_value(record: dict[str, Any], fieldname: str) -> Any:
	parsed = _parse_child_field_path(fieldname)
	if not parsed:
		return record.get(fieldname)
	table_field, row_idx, child_field = parsed
	rows = record.get(table_field)
	if not isinstance(rows, list) or len(rows) < row_idx:
		return None
	row = rows[row_idx - 1]
	if isinstance(row, dict):
		return row.get(child_field)
	getter = getattr(row, "get", None)
	if callable(getter):
		return getter(child_field)
	return getattr(row, child_field, None)


def _set_frappe_payload_value(
	payload: dict[str, Any],
	fieldname: str,
	value: Any,
	*,
	mapping_context: RuntimeMappingContext | None = None,
) -> None:
	parsed = _parse_child_field_path(fieldname)
	if not parsed:
		payload[fieldname] = value
		return
	table_field, row_idx, child_field = parsed
	rows = payload.setdefault(table_field, [])
	if not isinstance(rows, list):
		rows = []
		payload[table_field] = rows
	while len(rows) < row_idx:
		child_row: dict[str, Any] = {}
		child_doctype = (mapping_context.child_table_options if mapping_context else {}).get(table_field)
		if child_doctype:
			child_row["doctype"] = child_doctype
		rows.append(child_row)
	row = rows[row_idx - 1]
	if not isinstance(row, dict):
		row = {}
		rows[row_idx - 1] = row
	child_doctype = (mapping_context.child_table_options if mapping_context else {}).get(table_field)
	if child_doctype:
		row.setdefault("doctype", child_doctype)
	row[child_field] = value


def _frappe_diff_field_names(payload: dict[str, Any], mapping_context: RuntimeMappingContext) -> list[str]:
	fields: list[str] = []
	child_tables: set[str] = set()
	for frappe_field, _partner_field in mapping_context.to_frappe_entries:
		parsed = _parse_child_field_path(frappe_field)
		if parsed:
			if parsed[0] in payload:
				fields.append(frappe_field)
				child_tables.add(parsed[0])
			continue
		if frappe_field in payload:
			fields.append(frappe_field)
	for fieldname in payload:
		if fieldname in fields or fieldname in child_tables:
			continue
		fields.append(fieldname)
	return fields or list(payload.keys())


def _mapped_value_with_fallback(field_map: dict[Any, Any], value: Any, fallback: dict[str, Any] | None) -> Any:
	mapped = _mapped_value(field_map, value, default=VALUE_MAPPING_UNSET)
	if mapped is not VALUE_MAPPING_UNSET:
		return mapped
	return _apply_value_mapping_fallback(value, fallback)


def _mapped_value(field_map: dict[Any, Any], value: Any, *, default: Any = None) -> Any:
	try:
		if value in field_map:
			return field_map[value]
	except TypeError:
		pass
	normalized_value = _normalize_comparable_scalar_value(value)
	for source_value, target_value in field_map.items():
		if _normalize_comparable_scalar_value(source_value) == normalized_value:
			return target_value
	return default


def _with_partner_timestamps(
	config: SyncDefinitionConfig | Any,
	frappe_record: dict[str, Any],
	payload: dict[str, Any],
	*,
	create: bool,
	mapping_context: RuntimeMappingContext,
) -> dict[str, Any]:
	result = dict(payload)
	partner_modified_field = _config_partner_modified_field(config)
	partner_creation_field = _config_partner_creation_field(config)
	effective_modified = _effective_modified(
		frappe_record,
		modified_field=_config_frappe_modified_field(config),
		creation_field=_config_frappe_creation_field(config),
		target_time_zone=mapping_context.site_time_zone,
	)
	if partner_modified_field and effective_modified is not None:
		result[partner_modified_field] = _convert_datetime_between_time_zones(
			effective_modified,
			source_time_zone=mapping_context.site_time_zone,
			target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
		)
	if create and partner_creation_field:
		creation_value = _parse_datetime(
			frappe_record.get(_config_frappe_creation_field(config)),
			target_time_zone=mapping_context.site_time_zone,
		)
		if creation_value is not None:
			result[partner_creation_field] = _convert_datetime_between_time_zones(
				creation_value,
				source_time_zone=mapping_context.site_time_zone,
				target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
			)
	return result


def _with_frappe_modified_timestamp(
	config: SyncDefinitionConfig | Any,
	partner_record: dict[str, Any],
	payload: dict[str, Any],
	*,
	mapping_context: RuntimeMappingContext,
) -> dict[str, Any]:
	result = dict(payload)
	partner_modified_field = _config_partner_modified_field(config)
	if not partner_modified_field:
		result.pop(_config_frappe_creation_field(config), None)
		return result
	effective_modified = _effective_modified(
		partner_record,
		modified_field=partner_modified_field,
		creation_field=_config_partner_creation_field(config),
		assumed_time_zone=getattr(config, "partner_time_zone", None),
		target_time_zone=mapping_context.site_time_zone,
	)
	if effective_modified is not None:
		result[_config_frappe_modified_field(config)] = effective_modified
	result.pop(_config_frappe_creation_field(config), None)
	return result


def _value_mapping_fallback_for_direction(
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None,
	frappe_field: str,
	direction: str,
) -> dict[str, Any] | None:
	if not isinstance(value_mapping_fallbacks, dict):
		return None
	field_fallbacks = value_mapping_fallbacks.get(frappe_field)
	if not isinstance(field_fallbacks, dict):
		return None
	return field_fallbacks if "action" in field_fallbacks else None


def _apply_value_mapping_fallback(value: Any, fallback: dict[str, Any] | None) -> Any:
	if not fallback:
		return value
	action = _normalize_value_mapping_fallback_action(fallback.get("action"))
	if action == VALUE_MAPPING_FALLBACK_USE_FALLBACK:
		return fallback.get("value")
	if action == VALUE_MAPPING_FALLBACK_USE_NULL:
		return None
	return value


def _diff_target_values(
	*,
	new_record: dict[str, Any],
	old_record: dict[str, Any],
	field_names: list[str],
	exclude_fields: set[str] | None = None,
	datetime_fields: set[str] | None = None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> list[tuple[str, Any, Any]]:
	changes: list[tuple[str, Any, Any]] = []
	for field_name in field_names:
		if field_name in (exclude_fields or set()):
			continue
		old_value = _get_frappe_payload_value(old_record, field_name)
		new_value = _get_frappe_payload_value(new_record, field_name)
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
	if field_name in (datetime_fields or set()) or isinstance(value, datetime | date):
		parsed = _parse_datetime(
			value,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		)
		if parsed is not None:
			return parsed
		if isinstance(value, datetime):
			return value.replace(tzinfo=None)
		if isinstance(value, date):
			return datetime.combine(value, datetime.min.time())
	if isinstance(value, str) and _finite_decimal_from_string(value.strip()) is None:
		parsed = _parse_datetime(
			value,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		)
		if parsed is not None:
			return parsed
	if isinstance(value, list | dict):
		return json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
	return _normalize_comparable_scalar_value(value)


def _normalize_comparable_scalar_value(value: Any) -> Any:
	decimal_value = _finite_decimal_from_scalar(value)
	if decimal_value is not None:
		return ("number", _normalize_decimal_pairing_key(decimal_value))
	return value


def _finite_decimal_from_scalar(value: Any) -> Decimal | None:
	if isinstance(value, bool):
		return Decimal(1 if value else 0)
	if isinstance(value, Decimal):
		return value if value.is_finite() else None
	if isinstance(value, int | float):
		return _finite_decimal_from_string(str(value))
	if isinstance(value, str):
		return _finite_decimal_from_string(value.strip())
	return None


def _finite_decimal_from_string(value: str) -> Decimal | None:
	if not value:
		return None
	try:
		decimal_value = Decimal(value)
	except (InvalidOperation, ValueError):
		return None
	return decimal_value if decimal_value.is_finite() else None


def _record_changed_since(
	record: dict[str, Any],
	modified_fields: list[str] | str | None,
	last_successful_sync: datetime | None,
	*,
	creation_field: str | None = None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> bool:
	if not last_successful_sync:
		return True
	modified_field = _first_configured_field(
		[modified_fields] if isinstance(modified_fields, str) else modified_fields,
		None,
	)
	if not modified_field:
		return False
	effective = _effective_modified(
		record,
		modified_field=modified_field,
		creation_field=creation_field,
		assumed_time_zone=assumed_time_zone,
		target_time_zone=target_time_zone,
	)
	return bool(effective and effective >= last_successful_sync)


def _latest_modified(
	record: dict[str, Any],
	modified_fields: list[str] | str | None,
	*,
	creation_field: str | None = None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> datetime | None:
	modified_field = _first_configured_field(
		[modified_fields] if isinstance(modified_fields, str) else modified_fields,
		None,
	)
	if not modified_field:
		return None
	return _effective_modified(
		record,
		modified_field=modified_field,
		creation_field=creation_field,
		assumed_time_zone=assumed_time_zone,
		target_time_zone=target_time_zone,
	)


def _compare_modified_timestamps(
	frappe_latest: datetime | None,
	partner_latest: datetime | None,
	*,
	buffer_ms: int,
) -> str | None:
	if partner_latest and not frappe_latest:
		return "partner"
	if frappe_latest and not partner_latest:
		return "frappe"
	if not frappe_latest or not partner_latest:
		return None
	if abs(partner_latest - frappe_latest) <= timedelta(milliseconds=max(0, buffer_ms)):
		return None
	return "partner" if partner_latest > frappe_latest else "frappe"


def _effective_modified(
	record: dict[str, Any],
	*,
	modified_field: str,
	creation_field: str | None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> datetime | None:
	modified_value = record.get(modified_field)
	if modified_value not in (None, ""):
		return _parse_datetime(
			modified_value,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		)
	if not creation_field:
		return None
	return _parse_datetime(
		record.get(creation_field),
		assumed_time_zone=assumed_time_zone,
		target_time_zone=target_time_zone,
	)


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
	return _normalize_pairing_key_tuple(record.get(field_name) for field_name in key_fields)


def _key_tuple_from_partner(record: dict[str, Any], key_fields: list[str], mapping: dict[str, Any]) -> tuple[Any, ...]:
	return _normalize_pairing_key_tuple(
		record.get(_partner_field_for_mapping(mapping, field_name, field_name)) for field_name in key_fields
	)


def _raw_key_tuple_from_frappe(record: dict[str, Any], key_fields: list[str]) -> tuple[Any, ...]:
	return tuple(record.get(field_name) for field_name in key_fields)


def _raw_key_tuple_from_partner(record: dict[str, Any], key_fields: list[str], mapping: dict[str, Any]) -> tuple[Any, ...]:
	return tuple(record.get(_partner_field_for_mapping(mapping, field_name, field_name)) for field_name in key_fields)


def _normalize_pairing_key_tuple(values: Any) -> tuple[Any, ...]:
	return tuple(_normalize_pairing_key_value(value) for value in values)


def _normalize_pairing_key_value(value: Any) -> Any:
	if value is None:
		return None
	if isinstance(value, datetime):
		return ("datetime", _normalize_datetime_pairing_key(value))
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return ""
		datetime_key = _normalize_datetime_string_pairing_key(value)
		if datetime_key is not None:
			return ("datetime", datetime_key)
		return _normalize_comparable_scalar_value(value)
	if _finite_decimal_from_scalar(value) is not None:
		return _normalize_comparable_scalar_value(value)
	return str(value)


def _normalize_number_pairing_key(value: str) -> str:
	decimal_value = _finite_decimal_from_string(value)
	if decimal_value is None:
		return value
	return _normalize_decimal_pairing_key(decimal_value)


def _normalize_decimal_pairing_key(value: Decimal) -> str:
	if not value.is_finite():
		return str(value)
	if value.is_zero():
		return "0"
	return format(value.normalize(), "f")


def _normalize_datetime_pairing_key(value: datetime) -> str:
	if value.tzinfo is not None and value.utcoffset() is not None:
		value = value.astimezone(timezone.utc)
	return value.isoformat(timespec="microseconds")


def _normalize_datetime_string_pairing_key(value: str) -> str | None:
	if "-" not in value or (":" not in value and "T" not in value):
		return None
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	return _normalize_datetime_pairing_key(parsed)


def _valid_key(key: tuple[Any, ...]) -> bool:
	return bool(key) and all(value not in (None, "") for value in key)


def _partner_key_values_from_tuple(config: SyncDefinitionConfig, key_values: tuple[Any, ...]) -> dict[str, Any]:
	result = {}
	for idx, frappe_key in enumerate(_config_match_fields(config)):
		partner_field = _partner_field_for_mapping(config.mapping, frappe_key, frappe_key)
		result[partner_field] = key_values[idx]
	return result


def _partner_key_values_from_frappe_record(config: SyncDefinitionConfig, record: dict[str, Any]) -> dict[str, Any]:
	return _partner_key_values_from_tuple(config, _raw_key_tuple_from_frappe(record, _config_match_fields(config)))


def _partner_key_values_from_partner_record(config: SyncDefinitionConfig, record: dict[str, Any]) -> dict[str, Any]:
	return _partner_key_values_from_tuple(
		config,
		_raw_key_tuple_from_partner(record, _config_match_fields(config), config.mapping),
	)


def _partner_fetch_key_fields(config: SyncDefinitionConfig) -> list[str]:
	if _config_match_mode(config) == MATCH_MODE_IDENTITY_FIELDS:
		return [field for field in [_config_partner_identity_field(config)] if field]
	mapping = getattr(config, "mapping", {}) or {}
	fields = [
		_partner_field_for_mapping(mapping, frappe_field, frappe_field)
		for frappe_field in _config_match_fields(config)
	]
	if _config_partner_identity_field(config):
		fields.append(_config_partner_identity_field(config) or "")
	if _config_partner_frappe_identity_field(config):
		fields.append(_config_partner_frappe_identity_field(config) or "")
	return [field for field in fields if field]


def _config_match_fields(config: Any) -> list[str]:
	return list(getattr(config, "match_fields", None) or [])


def _config_read_query(config: Any) -> str | None:
	return getattr(config, "read_query", None)


def _config_one_way_match_mode(config: Any) -> str:
	return getattr(config, "one_way_match_mode", None) or ONE_WAY_MATCH_FIRST


def _update_existing_enabled(config: Any) -> bool:
	return _as_bool(getattr(config, "update_existing", 1))


def _config_match_mode(config: Any) -> str:
	return _normalize_match_mode(getattr(config, "match_mode", None))


def _config_frappe_modified_field(config: Any) -> str:
	return _clean_string(getattr(config, "frappe_modified_field", None)) or _first_configured_field(
		getattr(config, "frappe_modified_fields", None),
		"modified",
	)


def _config_frappe_creation_field(config: Any) -> str:
	return _clean_string(getattr(config, "frappe_creation_field", None)) or "creation"


def _config_partner_modified_field(config: Any) -> str | None:
	return _clean_string(getattr(config, "partner_modified_field", None)) or _first_configured_field(
		getattr(config, "partner_modified_fields", None),
		None,
	)


def _config_partner_creation_field(config: Any) -> str | None:
	return _clean_string(getattr(config, "partner_creation_field", None))


def _partner_timestamps_required(config: Any) -> bool:
	return str(getattr(config, "sync_type", "") or "") == SYNC_TYPE_BIDIRECTIONAL or _as_bool(
		getattr(config, "use_last_sync_date", 0)
	)


def _config_timestamp_tie_breaker(config: Any) -> str:
	return _normalize_timestamp_tie_breaker(getattr(config, "timestamp_tie_breaker", None))


def _normalize_timestamp_tie_breaker(value: Any) -> str:
	normalized = _clean_string(value)
	if not normalized:
		return TIMESTAMP_TIE_MANUAL
	return normalized


def _normalize_frappe_write_action(value: Any) -> str:
	normalized = _clean_string(value) or FRAPPE_WRITE_ACTION_NONE
	if normalized in FRAPPE_WRITE_ACTIONS:
		return normalized
	return FRAPPE_WRITE_ACTION_NONE


def _config_frappe_after_insert_action(config: Any) -> str:
	return _normalize_frappe_write_action(getattr(config, "frappe_after_insert_action", None))


def _config_frappe_after_update_action(config: Any) -> str:
	return _normalize_frappe_write_action(getattr(config, "frappe_after_update_action", None))


def _frappe_write_action_kwargs(config: Any) -> dict[str, str]:
	kwargs: dict[str, str] = {}
	insert_action = _config_frappe_after_insert_action(config)
	update_action = _config_frappe_after_update_action(config)
	if insert_action != FRAPPE_WRITE_ACTION_NONE:
		kwargs["after_insert_action"] = insert_action
	if update_action != FRAPPE_WRITE_ACTION_NONE:
		kwargs["after_update_action"] = update_action
	return kwargs


def _config_timestamp_buffer_ms(config: Any) -> int:
	return _coerce_timestamp_buffer_ms(getattr(config, "timestamp_buffer_ms", None))


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
		identity = _normalize_pairing_key_value(_partner_identity_value(config, record))
		if identity not in (None, ""):
			index[identity] = record
	return index


def _normalize_partner_match_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]:
	if isinstance(records, dict) or _config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL:
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
		identity = _normalize_pairing_key_value(record.get(fieldname))
		if identity not in (None, ""):
			index[identity] = record
	return index


def _normalize_frappe_match_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]:
	if isinstance(records, dict) or _config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL:
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
	frappe_partner_id = _normalize_pairing_key_value(_frappe_partner_identity_value(config, frappe_record))
	if frappe_partner_id not in (None, ""):
		existing = partner_identity_index.get(frappe_partner_id)
		if existing:
			return [existing]
	key = _key_tuple_from_frappe(frappe_record, _config_match_fields(config))
	if _valid_key(key):
		matches = list(partner_groups.get(key) or [])
		if _config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL:
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
	partner_identity = _normalize_pairing_key_value(_partner_identity_value(config, partner_record))
	if partner_identity not in (None, ""):
		existing = frappe_partner_identity_index.get(partner_identity)
		if existing:
			return [existing]
	key = _key_tuple_from_partner(partner_record, _config_match_fields(config), config.mapping)
	if _valid_key(key):
		matches = list(frappe_groups.get(key) or [])
		if _config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL:
			return matches
		return matches[-1:] if matches else []
	return []


def _pair_token_from_frappe(config: SyncDefinitionConfig, record: dict[str, Any]) -> tuple[Any, ...] | None:
	identity = _normalize_pairing_key_value(_frappe_partner_identity_value(config, record))
	if _config_partner_identity_field(config) and identity not in (None, ""):
		return ("partner_identity", identity)
	key = _key_tuple_from_frappe(record, _config_match_fields(config))
	if _valid_key(key):
		return ("match", *key)
	return None


def _pair_token_from_partner(config: SyncDefinitionConfig, record: dict[str, Any]) -> tuple[Any, ...] | None:
	identity = _normalize_pairing_key_value(_partner_identity_value(config, record))
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
	return _partner_key_values_from_frappe_record(config, frappe_record)


def _partner_key_values_for_existing_match(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	key: tuple[Any, ...],
	partner_record: dict[str, Any] | None,
) -> dict[str, Any]:
	partner_identity_field = _config_partner_identity_field(config)
	if partner_identity_field and partner_record and partner_record.get(partner_identity_field) not in (None, ""):
		return {partner_identity_field: partner_record.get(partner_identity_field)}
	if partner_record:
		return _partner_key_values_from_partner_record(config, partner_record)
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
	write_direction: str | None = None,
	frappe_before_record: dict[str, Any] | None | object = AUDIT_RECORD_UNSET,
	partner_before_record: dict[str, Any] | None | object = AUDIT_RECORD_UNSET,
	written_after_record: dict[str, Any] | None = None,
	frappe_resolution_payload: dict[str, Any] | None = None,
	partner_resolution_payload: dict[str, Any] | None = None,
	changes: list[tuple[str, Any, Any]] | None = None,
	commit: bool = True,
):
	stats.register(action=action, status=status)
	actual_write_direction = write_direction or _default_write_direction(action, status, direction)
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
		write_direction=actual_write_direction,
		frappe_before_record=frappe_before_record,
		partner_before_record=partner_before_record,
		written_after_record=written_after_record,
		frappe_resolution_payload=frappe_resolution_payload,
		partner_resolution_payload=partner_resolution_payload,
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
			"sync_type": _first_value(sync_definition_doc, ["sync_type"], default="Frappe -> Partner"),
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
	write_direction: str | None = None,
	frappe_before_record: dict[str, Any] | None | object = AUDIT_RECORD_UNSET,
	partner_before_record: dict[str, Any] | None | object = AUDIT_RECORD_UNSET,
	written_after_record: dict[str, Any] | None = None,
	frappe_resolution_payload: dict[str, Any] | None = None,
	partner_resolution_payload: dict[str, Any] | None = None,
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
		}
	)

	record_name = (frappe_record or {}).get("name")
	record_key = _compact_record_key(config, frappe_record=frappe_record, partner_record=partner_record)
	planned_direction = direction or _first_value(run_doc, ["sync_type"])
	actual_write_direction = write_direction or _default_write_direction(action, status, planned_direction)
	source_id, target_id = _source_and_target_ids(
		config,
		direction=actual_write_direction or planned_direction,
		frappe_record=frappe_record,
		partner_record=partner_record,
	)
	_set_first_existing(payload, meta, ["document_name", "frappe_name", "frappe_record_name"], record_name)
	_set_first_existing(payload, meta, ["record_key"], _fit_data_value(record_key))
	_set_first_existing(payload, meta, ["write_direction"], actual_write_direction)
	_set_first_existing(payload, meta, ["source_id"], _fit_data_value(source_id))
	_set_first_existing(payload, meta, ["target_id"], _fit_data_value(target_id))
	_set_first_existing(payload, meta, ["change_count"], len(changes or []))
	_set_first_existing(payload, meta, ["changed_fields"], _summarize_changed_fields(changes))
	if frappe_resolution_payload is not None:
		_set_first_existing(payload, meta, ["frappe_resolution_payload"], _json_payload(frappe_resolution_payload))
	if partner_resolution_payload is not None:
		_set_first_existing(payload, meta, ["partner_resolution_payload"], _json_payload(partner_resolution_payload))
	if _capture_audit_payloads(config):
		frappe_before_record = frappe_record if frappe_before_record is AUDIT_RECORD_UNSET else frappe_before_record
		partner_before_record = partner_record if partner_before_record is AUDIT_RECORD_UNSET else partner_before_record
		_set_first_existing(payload, meta, ["frappe_before_payload"], _json_payload(frappe_before_record))
		_set_first_existing(payload, meta, ["partner_before_payload"], _json_payload(partner_before_record))
		_set_first_existing(payload, meta, ["written_after_payload"], _json_payload(written_after_record))

	doc = frappe.get_doc(payload)
	doc.insert(ignore_permissions=True)
	if commit:
		frappe.db.commit()
	return doc


def _default_write_direction(action: str, status: str, direction: str | None) -> str | None:
	if action == "skipped" or status == "skipped":
		return None
	if action in {"created", "updated", "deleted", "conflict", "error"}:
		return _one_way_write_direction(direction)
	return None


def _one_way_write_direction(direction: str | None) -> str | None:
	if direction == SYNC_TYPE_FRAPPE_TO_PARTNER:
		return SYNC_TYPE_FRAPPE_TO_PARTNER
	if direction == SYNC_TYPE_PARTNER_TO_FRAPPE:
		return SYNC_TYPE_PARTNER_TO_FRAPPE
	return None


def _source_and_target_ids(
	config: SyncDefinitionConfig | None,
	*,
	direction: str | None,
	frappe_record: dict[str, Any] | None,
	partner_record: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
	frappe_id = _compact_source_id(config, frappe_record=frappe_record)
	partner_id = _compact_target_id(config, partner_record=partner_record)
	if direction == SYNC_TYPE_PARTNER_TO_FRAPPE:
		return partner_id, frappe_id
	return frappe_id, partner_id


def _json_payload(record: dict[str, Any] | None | object) -> str:
	return json.dumps(record, default=str, ensure_ascii=True)


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
	table_fields = {
		_clean_string(getattr(field, "fieldname", None)): getattr(field, "options", None)
		for field in getattr(meta, "fields", [])
		if getattr(field, "fieldtype", None) == "Table"
	}
	for field_name in candidates:
		parsed = _parse_child_field_path(field_name)
		if parsed:
			table_field, _row_idx, child_field = parsed
			child_doctype = table_fields.get(table_field)
			if child_doctype and _doctype_fieldtype(child_doctype, child_field) == "Datetime":
				result.add(field_name)
			continue
		if fieldtypes.get(field_name) == "Datetime":
			result.add(field_name)
	return result


def _frappe_datetime_fields(config: SyncDefinitionConfig) -> set[str]:
	candidates = {
		_config_frappe_modified_field(config),
		_config_frappe_creation_field(config),
	}
	candidates.update(frappe_field for frappe_field, _entry in _iter_field_mapping_entries(getattr(config, "mapping", {})))
	return _get_frappe_datetime_fields(getattr(config, "doctype", None), candidates)


def _partner_datetime_fields(config: SyncDefinitionConfig) -> set[str]:
	partner_fields = {
		field
		for field in (
			_config_partner_modified_field(config),
			_config_partner_creation_field(config),
		)
		if field
	}
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


def _set_doc_values(doc: Any, values: dict[str, Any]) -> None:
	if not values:
		return
	set_value = getattr(getattr(frappe, "db", None), "set_value", None)
	doc_name = _doc_name(doc)
	if callable(set_value) and doc_name:
		set_value(doc.doctype, doc_name, values, update_modified=False)
		if hasattr(doc, "payload") and isinstance(doc.payload, dict):
			doc.payload.update(values)
		if hasattr(doc, "values") and isinstance(doc.values, dict):
			doc.values.update(values)
		return
	for fieldname, value in values.items():
		doc.db_set(fieldname, value, update_modified=False)


def _update_doc_fields(doc: Any, values: dict[str, Any], *, commit: bool = True) -> None:
	meta = frappe.get_meta(doc.doctype)
	updates = {}
	for key, value in values.items():
		fieldname = _find_field(meta, [key])
		if fieldname:
			updates[fieldname] = value
	_set_doc_values(doc, updates)
	if commit:
		frappe.db.commit()


def _update_definition_runtime(
	sync_definition_doc: Any,
	*,
	last_run: str,
	status: str = RUN_STATUS_SUCCESS,
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
	if status == RUN_STATUS_SUCCESS and last_sync_at is not None:
		updates["last_successful_sync"] = last_sync_at
	valid_updates = {}
	for fieldname, value in updates.items():
		if value is None:
			continue
		if meta.has_field(fieldname):
			valid_updates[fieldname] = value
	_set_doc_values(sync_definition_doc, valid_updates)
	if commit:
		frappe.db.commit()


def _update_definition_failure(sync_definition_doc: Any, *, last_run: str, error_message: str, commit: bool = True):
	meta = frappe.get_meta(sync_definition_doc.doctype)
	updates = {
		"last_run": last_run,
		"last_run_status": RUN_STATUS_ERROR,
		"last_run_summary": error_message.splitlines()[-1] if error_message else "Sync failed",
	}
	valid_updates = {}
	for fieldname, value in updates.items():
		if value is None:
			continue
		if meta.has_field(fieldname):
			valid_updates[fieldname] = value
	_set_doc_values(sync_definition_doc, valid_updates)
	if commit:
		frappe.db.commit()


def _update_definition_stale_recovery(
	sync_definition_doc: Any,
	*,
	last_run: str,
	status: str,
	summary: str,
	commit: bool = True,
):
	meta = frappe.get_meta(sync_definition_doc.doctype)
	updates = {
		"last_run": last_run,
		"last_run_status": status,
		"last_run_summary": summary,
	}
	valid_updates = {}
	for fieldname, value in updates.items():
		if value is None:
			continue
		if meta.has_field(fieldname):
			valid_updates[fieldname] = value
	_set_doc_values(sync_definition_doc, valid_updates)
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
		filters={"sync_definition": sync_definition_name, "status": RUN_STATUS_SUCCESS, "dry_run": 0},
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


def _get_sync_settings() -> SimpleNamespace:
	values = {
		"stale_run_timeout_minutes": DEFAULT_STALE_RUN_TIMEOUT_MINUTES,
		"run_retention_days_success": DEFAULT_RUN_RETENTION_DAYS_SUCCESS,
		"run_retention_days_error": DEFAULT_RUN_RETENTION_DAYS_ERROR,
	}
	get_single_value = getattr(getattr(frappe, "db", None), "get_single_value", None)
	if not callable(get_single_value):
		return SimpleNamespace(**values)
	for fieldname, default in list(values.items()):
		try:
			values[fieldname] = _positive_int(get_single_value(SYNC_SETTINGS, fieldname), default)
		except Exception:
			values[fieldname] = default
	return SimpleNamespace(**values)


def _positive_int(value: Any, default: int) -> int:
	try:
		normalized = int(value)
	except Exception:
		return default
	return normalized if normalized > 0 else default


def _stale_run_terminal_status(previous_status: str, requested_status: str | None = None) -> str:
	if requested_status in {RUN_STATUS_ERROR, RUN_STATUS_SKIPPED}:
		return requested_status
	return RUN_STATUS_SKIPPED if previous_status == RUN_STATUS_QUEUED else RUN_STATUS_ERROR


def _linked_run_item_names(run_name: str) -> list[str]:
	rows = frappe.get_all(
		SYNC_RUN_ITEM,
		filters={"sync_run": run_name},
		fields=["name"],
		order_by=None,
	)
	return [str(_row_value(row, "name")) for row in rows if _row_value(row, "name")]


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
	normalized = _clean_string(trigger) or TRIGGER_MANUAL
	if normalized not in VALID_TRIGGER_TYPES:
		raise frappe.ValidationError(f"Trigger Type must be one of: {', '.join(sorted(VALID_TRIGGER_TYPES))}.")
	return normalized


def _terminal_status_for_result(result_payload: dict[str, Any]) -> str:
	if cint(result_payload.get("error_count")) > 0:
		return RUN_STATUS_PARTIAL_ERROR
	if cint(result_payload.get("conflict_count")) > 0:
		return RUN_STATUS_NEEDS_REVIEW
	return RUN_STATUS_SUCCESS


def _api_status_for_run_status(run_status: str) -> str:
	if run_status == RUN_STATUS_SUCCESS:
		return "success"
	if run_status == RUN_STATUS_PARTIAL_ERROR:
		return "partial_error"
	if run_status == RUN_STATUS_NEEDS_REVIEW:
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


def _row_value(row: Any, fieldname: str, default: Any = None) -> Any:
	if isinstance(row, dict):
		return row.get(fieldname, default)
	getter = getattr(row, "get", None)
	if callable(getter):
		return getter(fieldname, default)
	return getattr(row, fieldname, default)


def _first_value_dict(doc: dict[str, Any], candidates: list[str], default: Any = None) -> Any:
	for candidate in candidates:
		value = doc.get(candidate)
		if value not in (None, ""):
			return value
	return default


def _coerce_timestamp_buffer_ms(value: Any) -> int:
	if value not in (None, ""):
		return max(0, cint(value) or 0)
	return DEFAULT_TIMESTAMP_BUFFER_MS


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


def _doctype_fieldnames(doctype: str | None) -> set[str] | None:
	if not doctype:
		return None
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return None
	fieldnames = set(SYSTEM_KEYS) | {"name", "creation", "modified", "owner", "modified_by"}
	for field in getattr(meta, "fields", []) or []:
		fieldname = _clean_string(getattr(field, "fieldname", None))
		if fieldname:
			fieldnames.add(fieldname)
	return fieldnames


def _doctype_table_fields(doctype: str | None) -> dict[str, str]:
	if not doctype:
		return {}
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return {}
	result: dict[str, str] = {}
	for field in getattr(meta, "fields", []) or []:
		if getattr(field, "fieldtype", None) != "Table":
			continue
		fieldname = _clean_string(getattr(field, "fieldname", None))
		options = _clean_string(getattr(field, "options", None))
		if fieldname and options:
			result[fieldname] = options
	return result


def _doctype_fieldtype(doctype: str | None, fieldname: str | None) -> str | None:
	if not doctype or not fieldname:
		return None
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return None
	for field in getattr(meta, "fields", []) or []:
		if getattr(field, "fieldname", None) == fieldname:
			return getattr(field, "fieldtype", None)
	return None


def _doctype_payload_allows_field(
	doctype: str,
	fieldname: str,
	doctype_fieldnames: set[str] | None,
) -> bool:
	if doctype_fieldnames is not None:
		return fieldname in doctype_fieldnames
	return _doctype_has_field(doctype, fieldname)


def _doctype_has_field(doctype: str, fieldname: str) -> bool:
	if fieldname in {"name", "creation", "modified", "owner", "modified_by"}:
		return True
	fieldnames = _doctype_fieldnames(doctype)
	if fieldnames is not None:
		return fieldname in fieldnames
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
	return cache.lock(lock_key, timeout=SYNC_DEFINITION_LOCK_TIMEOUT_SECONDS, blocking_timeout=10)


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
		"last_connection_status": RUN_STATUS_SUCCESS if status == "ok" else RUN_STATUS_ERROR,
		"last_checked_on": now_datetime(),
		"last_connection_error": "" if status == "ok" else details,
	}
	meta = frappe.get_meta(partner_doc.doctype)
	updates = {}
	for fieldname, value in values.items():
		if meta.has_field(fieldname):
			updates[fieldname] = value
	_set_doc_values(partner_doc, updates)
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
	excluded_fields = _portable_excluded_fields(data["doctype"])
	result: dict[str, Any] = {"doctype": data["doctype"]}
	if data.get("name"):
		result["name"] = data["name"]
	for key, value in data.items():
		if key in SYSTEM_KEYS or key in excluded_fields or key.startswith("_"):
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
	excluded_fields = _portable_excluded_fields(doctype)
	result: dict[str, Any] = {"doctype": doctype}
	if payload.get("name"):
		result["name"] = payload["name"]

	for field in meta.fields:
		if field.fieldname in table_fields:
			continue
		if field.fieldname in excluded_fields:
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


def _portable_excluded_fields(doctype: str) -> set[str]:
	if doctype == SYNC_DEFINITION:
		return SYNC_DEFINITION_RUNTIME_STATE_FIELDS
	return set()


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
