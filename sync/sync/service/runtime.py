from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any

import frappe
from frappe.utils import cint, get_datetime, now_datetime
import yaml

from .connectors import get_connector_for_partner

try:
	from croniter import croniter
except Exception:  # pragma: no cover - optional runtime dependency
	croniter = None


SYNC_DEFINITION = "Sync Definition"
SYNC_PARTNER = "Sync Partner"
SYNC_RUN = "Sync Run"
SYNC_RUN_ITEM = "Sync Run Item"
SYNC_RUN_ITEM_CHANGE = "Sync Run Item Change"

ACTIVE_RUN_STATUSES = {"Queued", "Running"}
DONE_RUN_STATUSES = {"Success", "Error", "Skipped"}

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
	query: str | None
	key_fields: list[str]
	mapping: dict[str, str]
	value_mapping: dict[str, dict[Any, Any]]
	frappe_modified_fields: list[str]
	partner_modified_fields: list[str]


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
		elif action == "skipped":
			self.skipped_count += 1
		elif action == "conflict":
			self.conflict_count += 1

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
	names = frappe.get_all(SYNC_DEFINITION, pluck="name")
	due: list[str] = []
	for name in names:
		doc = frappe.get_doc(SYNC_DEFINITION, name)
		if not _is_enabled(doc):
			continue

		next_run_at = _first_value(doc, ["next_run_at", "next_execution_at"])
		if isinstance(next_run_at, datetime) and next_run_at <= now:
			due.append(name)
			continue

		cron_expr = _first_value(doc, ["frequency_cron", "cron", "schedule_cron"])
		if cron_expr and _is_due_by_cron(doc, str(cron_expr), now):
			due.append(name)
	return due


def run_due_sync_definitions(limit: int = 20, queue: bool = True) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for name in list_due_sync_definitions()[:limit]:
		results.append(enqueue_sync_definition(name, trigger="scheduler", queue=queue))
	return results


def enqueue_sync_definition(
	sync_definition_name: str,
	*,
	trigger: str = "manual",
	queue: bool = True,
	dry_run: bool = False,
) -> dict[str, Any]:
	sync_definition_name = str(sync_definition_name)
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

			sync_stamp = None if dry_run else now_datetime()
			_update_doc_fields(
				run_doc,
				{
					"status": "Success",
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
			)
			_update_definition_runtime(
				sync_definition,
				last_run=run_doc.name,
				last_sync_at=sync_stamp,
				summary=_format_run_summary(result_payload),
			)
			_set_next_run_at(sync_definition, config.cron)

			return {"status": "success", "run": run_doc.name, "result": result_payload}
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Sync execution failed for {sync_definition_name}")
			_update_doc_fields(
				run_doc,
				{
					"status": "Error",
					"finished_at": now_datetime(),
					"error_message": frappe.get_traceback(with_context=False),
				},
			)
			_update_definition_failure(sync_definition, last_run=run_doc.name, error_message=frappe.get_traceback(with_context=False))
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
	partner_doc = frappe.get_doc(SYNC_PARTNER, config.partner)
	connector = get_connector_for_partner(partner_doc)
	ping = connector.ping()

	fields = sorted(set(config.mapping.keys()) | set(config.key_fields) | {"name", "modified"})
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
		"mapping": config.mapping,
		"key_fields": config.key_fields,
		"value_mapping_fields": sorted(config.value_mapping.keys()),
		"actions": [{"direction": config.sync_type, "result": "preview"}],
	}


def export_sync_definition_yaml(sync_definition_name: str) -> str:
	sync_definition_doc = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
	mask_credentials = _as_bool(_first_value(sync_definition_doc, ["export_mask_credentials"], default=1))
	config_doc = _sanitize_document_dict(sync_definition_doc.as_dict(), mask_credentials=mask_credentials)
	partner_name = _first_value(sync_definition_doc, ["sync_partner", "partner", "sync_partner_name"])

	payload: dict[str, Any] = {
		"version": 1,
		"exported_at": now_datetime().isoformat(),
		"sync_definition": config_doc,
	}
	if partner_name:
		partner_doc = frappe.get_doc(SYNC_PARTNER, partner_name)
		payload["sync_partner"] = _sanitize_document_dict(partner_doc.as_dict(), mask_credentials=mask_credentials)
		partner_type_name = _first_value(partner_doc, ["partner_type", "sync_partner_type", "type"])
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
	connector = get_connector_for_partner(partner_doc)
	ping = connector.ping()
	if not ping.ok:
		raise frappe.ValidationError(f"Partner connector validation failed: {ping.message}")

	frappe_source = _get_frappe_source_records(config, context)
	partner_source = _get_partner_source_records(config, connector, context)

	stats = SyncStats()
	if config.sync_type == "A->B":
		_sync_frappe_to_partner(
			run_doc=run_doc,
			config=config,
			connector=connector,
			frappe_records=frappe_source,
			partner_records=partner_source,
			dry_run=context.dry_run,
			stats=stats,
			label_direction="A->B",
			full_sync=context.is_full_sync,
		)
	elif config.sync_type == "A<-B":
		_sync_partner_to_frappe(
			run_doc=run_doc,
			config=config,
			connector=connector,
			partner_records=partner_source,
			frappe_records=frappe_source,
			dry_run=context.dry_run,
			stats=stats,
			label_direction="A<-B",
			full_sync=context.is_full_sync,
		)
	else:
		_sync_bidirectional(
			run_doc=run_doc,
			config=config,
			connector=connector,
			frappe_records=frappe_source,
			partner_records=partner_source,
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
		return config
	return SyncDefinitionConfig(
		name=str(getattr(config, "name", "")),
		doctype=str(getattr(config, "doctype", "")),
		partner=str(getattr(config, "partner", "")),
		sync_type=str(getattr(config, "sync_type", "A->B")),
		cron=getattr(config, "cron", None),
		filters=getattr(config, "filters", None),
		batch_size=cint(getattr(config, "batch_size", 100)) or 100,
		create_new=_as_bool(getattr(config, "create_new", 1)),
		delete_missing=_as_bool(getattr(config, "delete_missing", 0)),
		use_last_sync_date=_as_bool(getattr(config, "use_last_sync_date", 1)),
		conflict_policy=str(getattr(config, "conflict_policy", "newest_wins")),
		timestamp_buffer_seconds=cint(getattr(config, "timestamp_buffer_seconds", 15)) or 0,
		table_name=getattr(config, "table_name", None),
		query=getattr(config, "query", None),
		key_fields=list(getattr(config, "key_fields", []) or []),
		mapping=dict(getattr(config, "mapping", {}) or {}),
		value_mapping=dict(getattr(config, "value_mapping", {}) or {}),
		frappe_modified_fields=list(getattr(config, "frappe_modified_fields", ["modified"]) or ["modified"]),
		partner_modified_fields=list(getattr(config, "partner_modified_fields", ["modified"]) or ["modified"]),
	)


def _sync_frappe_to_partner(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	frappe_records: list[dict[str, Any]],
	partner_records: list[dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
	full_sync: bool,
):
	partner_index = _index_partner_records(config, partner_records)
	source_keys: set[tuple[Any, ...]] = set()

	for frappe_record in frappe_records:
		key = _key_tuple_from_frappe(frappe_record, config.key_fields)
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
			)
			continue

		source_keys.add(key)
		partner_payload = _map_frappe_to_partner(frappe_record, config.mapping, config.value_mapping)
		existing_partner = partner_index.get(key)
		exists = existing_partner is not None

		if not exists and not config.create_new:
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
			)
			continue

		changes = _diff_target_values(
			new_record=partner_payload,
			old_record=existing_partner or {},
			field_names=list(partner_payload.keys()),
		)
		if exists and not changes:
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="skipped",
				status="skipped",
				message="No changes detected.",
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=existing_partner,
			)
			continue

		action = "created" if not exists else "updated"
		try:
			write = connector.upsert_record(
				record=partner_payload,
				key_fields=config.key_fields,
				mapping=config.mapping,
				dry_run=dry_run,
				source=config.table_name,
				query=config.query,
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
			)
			continue

		change_rows = [
			(field_name, old_value, new_value, "frappe", "partner")
			for field_name, old_value, new_value in changes
		]
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action=action,
			status="success",
			message="Dry run upsert." if dry_run else "Upserted partner record.",
			direction=label_direction,
			frappe_record=frappe_record,
			partner_record=partner_payload,
			changes=change_rows,
		)

	if config.delete_missing and full_sync:
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
				)


def _sync_partner_to_frappe(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	partner_records: list[dict[str, Any]],
	frappe_records: list[dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
	full_sync: bool,
):
	frappe_index = _index_frappe_records(config, frappe_records)
	partner_index = _index_partner_records(config, partner_records)
	source_keys: set[tuple[Any, ...]] = set()

	for key, partner_record in partner_index.items():
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
			)
			continue

		source_keys.add(key)
		frappe_payload = _map_partner_to_frappe(partner_record, config.mapping, config.value_mapping)
		existing_frappe = frappe_index.get(key)
		exists = existing_frappe is not None

		if not exists and not config.create_new:
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
			)
			continue

		changes = _diff_target_values(
			new_record=frappe_payload,
			old_record=existing_frappe or {},
			field_names=list(frappe_payload.keys()),
		)
		if exists and not changes:
			_register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="skipped",
				status="skipped",
				message="No changes detected.",
				direction=label_direction,
				frappe_record=existing_frappe,
				partner_record=partner_record,
			)
			continue

		action = "created" if not exists else "updated"
		try:
			doc_name = _upsert_frappe_record(
				doctype=config.doctype,
				existing_name=(existing_frappe or {}).get("name"),
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
				frappe_record=existing_frappe,
				partner_record=partner_record,
			)
			continue

		change_rows = [
			(field_name, old_value, new_value, "partner", "frappe")
			for field_name, old_value, new_value in changes
		]
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action=action,
			status="success",
			message="Dry run upsert." if dry_run else "Upserted frappe record.",
			direction=label_direction,
			frappe_record=frappe_payload,
			partner_record=partner_record,
			changes=change_rows,
		)

	if config.delete_missing and full_sync:
		for key, frappe_record in frappe_index.items():
			if key in source_keys:
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
					message="Dry run delete." if dry_run else "Deleted frappe record missing in source.",
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=None,
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
				)


def _sync_bidirectional(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	frappe_records: list[dict[str, Any]],
	partner_records: list[dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	last_successful_sync: datetime | None,
):
	frappe_index = _index_frappe_records(config, frappe_records)
	partner_index = _index_partner_records(config, partner_records)
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

		frappe_payload = _map_partner_to_frappe(partner_record, config.mapping, config.value_mapping)
		partner_payload = _map_frappe_to_partner(frappe_record, config.mapping, config.value_mapping)

		to_partner_changes = _diff_target_values(
			new_record=partner_payload,
			old_record=partner_record,
			field_names=list(partner_payload.keys()),
		)
		to_frappe_changes = _diff_target_values(
			new_record=frappe_payload,
			old_record=frappe_record,
			field_names=list(frappe_payload.keys()),
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
			)
			continue

		frappe_changed_since_last = _record_changed_since(
			record=frappe_record,
			modified_fields=config.frappe_modified_fields,
			last_successful_sync=last_successful_sync,
		)
		partner_changed_since_last = _record_changed_since(
			record=partner_record,
			modified_fields=config.partner_modified_fields,
			last_successful_sync=last_successful_sync,
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
			)
			continue

		frappe_latest = _latest_modified(record=frappe_record, modified_fields=config.frappe_modified_fields)
		partner_latest = _latest_modified(record=partner_record, modified_fields=config.partner_modified_fields)
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
):
	try:
		write = connector.upsert_record(
			record=partner_payload,
			key_fields=config.key_fields,
			mapping=config.mapping,
			dry_run=dry_run,
			source=config.table_name,
			query=config.query,
		)
		if not write.ok:
			raise RuntimeError(write.message or "Partner upsert failed.")
		change_rows = [(field_name, old_value, new_value, "frappe", "partner") for field_name, old_value, new_value in changes]
		_register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action=action,
			status=status,
			message=("Dry run update." if dry_run else message),
			direction=direction,
			frappe_record=frappe_record,
			partner_record=partner_payload,
			changes=change_rows,
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
		change_rows = [(field_name, old_value, new_value, "partner", "frappe") for field_name, old_value, new_value in changes]
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
			changes=change_rows,
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
		)


def _get_frappe_source_records(config: SyncDefinitionConfig, context: SyncContext) -> list[dict[str, Any]]:
	fields = sorted(
		set(config.mapping.keys())
		| set(config.key_fields)
		| set(config.frappe_modified_fields)
		| {"name", "modified"}
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
	return _get_frappe_records(
		doctype=config.doctype,
		fields=valid_fields,
		filters=config.filters,
		or_filters=or_filters,
		batch_size=config.batch_size,
	)


def _get_partner_source_records(config: SyncDefinitionConfig, connector: Any, context: SyncContext) -> list[dict[str, Any]]:
	records = _fetch_partner_records(
		connector=connector,
		source=config.table_name,
		query=config.query,
		batch_size=config.batch_size,
		key_fields=config.key_fields,
	)
	if not context.is_delta_sync:
		return records
	since = context.delta_since
	filtered = []
	for record in records:
		if _record_changed_since(record, config.partner_modified_fields, since):
			filtered.append(record)
	return filtered


def _fetch_partner_records(
	*,
	connector: Any,
	source: str | None,
	query: str | None,
	batch_size: int,
	key_fields: list[str],
) -> list[dict[str, Any]]:
	cursor = None
	results: list[dict[str, Any]] = []
	for _ in range(10_000):
		try:
			page = connector.fetch_records(
				source=source,
				query=query,
				batch_size=batch_size,
				cursor=cursor,
				key_fields=key_fields,
			)
		except TypeError:
			page = connector.fetch_records(
				source=source,
				query=query,
				batch_size=batch_size,
				cursor=cursor,
			)
		except Exception:
			break

		records, next_cursor = _normalize_fetch_result(page)
		if not records:
			break
		results.extend(records)
		if not next_cursor:
			break
		cursor = next_cursor
	return results


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
	doctype = _first_value(sync_definition_doc, ["doctype_name", "ref_doctype", "doctype", "doc_type"])
	if doctype == SYNC_DEFINITION:
		doctype = _first_value(sync_definition_doc, ["target_doctype", "source_doctype"])
	if not doctype:
		raise frappe.ValidationError("Sync Definition is missing target DocType field.")

	partner = _first_value(sync_definition_doc, ["sync_partner", "partner", "sync_partner_name"])
	if not partner:
		raise frappe.ValidationError("Sync Definition is missing Sync Partner reference.")

	sync_type = _first_value(sync_definition_doc, ["sync_type", "direction"], default="A->B")
	cron_expr = _first_value(sync_definition_doc, ["frequency_cron", "cron", "schedule_cron"])
	filters = _parse_filter_expression(_first_value(sync_definition_doc, ["filter_expression", "filters", "frappe_filters"]))
	batch_size = cint(_first_value(sync_definition_doc, ["batch_size", "chunk_size"], default=100)) or 100
	create_new = _as_bool(_first_value(sync_definition_doc, ["create_new"], default=1))
	delete_missing = _as_bool(_first_value(sync_definition_doc, ["delete_missing", "delete"], default=0))
	use_last_sync_date = _as_bool(_first_value(sync_definition_doc, ["use_last_sync_date"], default=1))
	conflict_policy = str(_first_value(sync_definition_doc, ["conflict_policy"], default="newest_wins"))
	timestamp_buffer_seconds = cint(_first_value(sync_definition_doc, ["timestamp_buffer_seconds"], default=15)) or 0

	key_fields = _get_key_fields(sync_definition_doc)
	mapping = _get_field_mapping(sync_definition_doc)
	value_mapping = _get_value_mapping(sync_definition_doc)
	if not mapping:
		raise frappe.ValidationError("Sync Definition has no field mapping entries.")
	if not key_fields:
		key_fields = [next(iter(mapping.keys()))]

	frappe_modified_fields = _get_modified_fields(sync_definition_doc, "frappe_modified_field_rows", "frappe_modified_fields") or ["modified"]
	partner_modified_fields = _get_modified_fields(sync_definition_doc, "partner_modified_field_rows", "partner_modified_fields") or ["modified"]

	return SyncDefinitionConfig(
		name=sync_definition_doc.name,
		doctype=str(doctype),
		partner=str(partner),
		sync_type=str(sync_type),
		cron=str(cron_expr) if cron_expr else None,
		filters=filters,
		batch_size=batch_size,
		create_new=create_new,
		delete_missing=delete_missing,
		use_last_sync_date=use_last_sync_date,
		conflict_policy=conflict_policy,
		timestamp_buffer_seconds=timestamp_buffer_seconds,
		table_name=_clean_string(_first_value(sync_definition_doc, ["table_name", "partner_table"])),
		query=_clean_string(_first_value(sync_definition_doc, ["query", "partner_query"])),
		key_fields=key_fields,
		mapping=mapping,
		value_mapping=value_mapping,
		frappe_modified_fields=frappe_modified_fields,
		partner_modified_fields=partner_modified_fields,
	)


def _get_key_fields(sync_definition_doc: Any) -> list[str]:
	rows = _get_child_rows_by_options(sync_definition_doc, "Sync Key Field")
	key_fields: list[str] = []
	for row in rows:
		fieldname = _first_value_dict(row, ["field_name", "key_field", "frappe_field", "fieldname"])
		if fieldname:
			key_fields.append(str(fieldname))
	top_level = _first_value(sync_definition_doc, ["key_fields"])
	if not key_fields and isinstance(top_level, str):
		key_fields = [entry.strip() for entry in top_level.split(",") if entry.strip()]
	return key_fields


def _get_field_mapping(sync_definition_doc: Any) -> dict[str, str]:
	rows = _get_child_rows_by_options(sync_definition_doc, "Sync Field Mapping")
	mapping: dict[str, str] = {}
	for row in rows:
		frappe_field = _first_value_dict(
			row,
			["frappe_field", "source_field", "doctype_field", "source_fieldname", "field_name"],
		)
		partner_field = _first_value_dict(
			row,
			["partner_field", "target_field", "external_field", "partner_column", "column_name"],
		)
		if frappe_field and partner_field:
			mapping[str(frappe_field)] = str(partner_field)
	top_level = _first_value(sync_definition_doc, ["field_mapping", "mapping"])
	if not mapping and isinstance(top_level, str):
		try:
			loaded = json.loads(top_level)
			if isinstance(loaded, dict):
				mapping = {str(k): str(v) for k, v in loaded.items()}
		except Exception:
			pass
	if not mapping and isinstance(top_level, dict):
		mapping = {str(k): str(v) for k, v in top_level.items()}
	return mapping


def _get_modified_fields(sync_definition_doc: Any, table_fieldname: str, legacy_fieldname: str) -> list[str]:
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
	return values or _parse_lines(_first_value(sync_definition_doc, [legacy_fieldname]))


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
	mapping: dict[str, str],
	value_mapping: dict[str, dict[Any, Any]],
) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for frappe_field, partner_field in mapping.items():
		value = record.get(frappe_field)
		field_map = value_mapping.get(frappe_field) or {}
		if value in field_map:
			value = field_map[value]
		result[partner_field] = value
	return result


def _map_partner_to_frappe(
	record: dict[str, Any],
	mapping: dict[str, str],
	value_mapping: dict[str, dict[Any, Any]],
) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for frappe_field, partner_field in mapping.items():
		value = record.get(partner_field)
		field_map = value_mapping.get(frappe_field) or {}
		reverse_map = {mapped_value: source_value for source_value, mapped_value in field_map.items()}
		if value in reverse_map:
			value = reverse_map[value]
		result[frappe_field] = value
	return result


def _diff_target_values(
	*,
	new_record: dict[str, Any],
	old_record: dict[str, Any],
	field_names: list[str],
) -> list[tuple[str, Any, Any]]:
	changes: list[tuple[str, Any, Any]] = []
	for field_name in field_names:
		old_value = old_record.get(field_name)
		new_value = new_record.get(field_name)
		if _normalize_value(old_value) != _normalize_value(new_value):
			changes.append((field_name, old_value, new_value))
	return changes


def _normalize_value(value: Any) -> Any:
	if isinstance(value, datetime):
		return value.replace(tzinfo=None)
	if isinstance(value, list | dict):
		return json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
	return value


def _record_changed_since(
	record: dict[str, Any],
	modified_fields: list[str],
	last_successful_sync: datetime | None,
) -> bool:
	if not last_successful_sync:
		return True
	for field_name in modified_fields:
		field_value = record.get(field_name)
		parsed = _parse_datetime(field_value)
		if parsed and parsed >= last_successful_sync:
			return True
	return False


def _latest_modified(record: dict[str, Any], modified_fields: list[str]) -> datetime | None:
	latest: datetime | None = None
	for field_name in modified_fields:
		parsed = _parse_datetime(record.get(field_name))
		if not parsed:
			continue
		if not latest or parsed > latest:
			latest = parsed
	return latest


def _parse_datetime(value: Any) -> datetime | None:
	if value in (None, ""):
		return None
	try:
		parsed = get_datetime(value)
	except Exception:
		return None
	if not isinstance(parsed, datetime):
		return None
	return parsed.replace(tzinfo=None)


def _get_frappe_records(
	doctype: str,
	*,
	fields: list[str],
	filters: list | dict | None,
	or_filters: list | None,
	batch_size: int,
) -> list[dict[str, Any]]:
	start = 0
	records: list[dict[str, Any]] = []
	while True:
		page = frappe.get_all(
			doctype,
			fields=fields,
			filters=filters,
			or_filters=or_filters,
			limit_start=start,
			limit_page_length=batch_size,
			order_by="modified asc",
		)
		if not page:
			break
		records.extend(page)
		if len(page) < batch_size:
			break
		start += batch_size
	return records


def _index_frappe_records(config: SyncDefinitionConfig, records: list[dict[str, Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in records:
		key = _key_tuple_from_frappe(record, config.key_fields)
		if _valid_key(key):
			index[key] = record
	return index


def _index_partner_records(config: SyncDefinitionConfig, records: list[dict[str, Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in records:
		key = _key_tuple_from_partner(record, config.key_fields, config.mapping)
		if _valid_key(key):
			index[key] = record
	return index


def _key_tuple_from_frappe(record: dict[str, Any], key_fields: list[str]) -> tuple[Any, ...]:
	return tuple(record.get(field_name) for field_name in key_fields)


def _key_tuple_from_partner(record: dict[str, Any], key_fields: list[str], mapping: dict[str, str]) -> tuple[Any, ...]:
	return tuple(record.get(mapping.get(field_name, field_name)) for field_name in key_fields)


def _valid_key(key: tuple[Any, ...]) -> bool:
	return bool(key) and all(value not in (None, "") for value in key)


def _partner_key_values_from_tuple(config: SyncDefinitionConfig, key_values: tuple[Any, ...]) -> dict[str, Any]:
	result = {}
	for idx, frappe_key in enumerate(config.key_fields):
		partner_field = config.mapping.get(frappe_key, frappe_key)
		result[partner_field] = key_values[idx]
	return result


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
	changes: list[tuple[str, Any, Any, str, str]] | None = None,
):
	stats.register(action=action, status=status)
	run_item_doc = _create_run_item(
		run_doc=run_doc,
		config=config,
		sync_definition_name=config.name,
		action=action,
		status=status,
		frappe_record=frappe_record,
		partner_record=partner_record,
		message=message,
		direction=direction,
	)
	for field_name, old_value, new_value, source_side, target_side in changes or []:
		_create_run_item_change(
			run_item_name=run_item_doc.name,
			fieldname=field_name,
			old_value=old_value,
			new_value=new_value,
			source_side=source_side,
			target_side=target_side,
		)


def _has_active_run(sync_definition_name: str) -> bool:
	meta = frappe.get_meta(SYNC_RUN)
	sync_definition_field = _find_field(meta, ["sync_definition", "definition", "sync_definition_name"])
	status_field = _find_field(meta, ["status", "run_status"])
	if not sync_definition_field or not status_field:
		return False
	return bool(
		frappe.db.exists(
			SYNC_RUN,
			{
				sync_definition_field: sync_definition_name,
				status_field: ["in", sorted(ACTIVE_RUN_STATUSES)],
			},
		)
	)


def _create_run_doc(sync_definition_doc: Any, *, status: str, trigger: str, dry_run: bool) -> Any:
	payload: dict[str, Any] = {"doctype": SYNC_RUN}
	meta = frappe.get_meta(SYNC_RUN)
	_set_first_existing(payload, meta, ["sync_definition", "definition", "sync_definition_name"], sync_definition_doc.name)
	_set_first_existing(payload, meta, ["status", "run_status"], status)
	_set_first_existing(payload, meta, ["trigger_type", "trigger"], trigger)
	_set_first_existing(payload, meta, ["dry_run", "is_dry_run"], cint(dry_run))
	_set_first_existing(payload, meta, ["started_at", "start_time"], now_datetime())
	_set_first_existing(payload, meta, ["sync_type", "direction"], _first_value(sync_definition_doc, ["sync_type", "direction"], default="A->B"))
	_set_first_existing(payload, meta, ["sync_partner", "partner"], _first_value(sync_definition_doc, ["sync_partner", "partner"]))
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
) -> Any:
	payload: dict[str, Any] = {"doctype": SYNC_RUN_ITEM}
	meta = frappe.get_meta(SYNC_RUN_ITEM)
	_set_first_existing(payload, meta, ["sync_run", "run", "sync_run_name"], run_doc.name)
	_set_first_existing(payload, meta, ["sync_definition", "definition", "sync_definition_name"], sync_definition_name)
	_set_first_existing(payload, meta, ["action"], action)
	_set_first_existing(payload, meta, ["status", "result_status"], status)
	_set_first_existing(payload, meta, ["message", "details", "note"], message)
	_set_first_existing(payload, meta, ["direction"], direction or _first_value(run_doc, ["sync_type", "direction"]))

	record_name = (frappe_record or {}).get("name")
	record_key = _compact_record_key(config, frappe_record=frappe_record, partner_record=partner_record)
	source_id = record_name or _compact_source_id(config, frappe_record=frappe_record)
	target_id = _compact_target_id(config, partner_record=partner_record)
	_set_first_existing(payload, meta, ["document_name", "frappe_name", "frappe_record_name"], record_name)
	_set_first_existing(payload, meta, ["record_key"], _fit_data_value(record_key))
	_set_first_existing(payload, meta, ["source_id"], _fit_data_value(source_id))
	_set_first_existing(payload, meta, ["target_id"], _fit_data_value(target_id))
	_set_first_existing(payload, meta, ["frappe_payload", "frappe_record_json"], json.dumps(frappe_record, default=str, ensure_ascii=True) if frappe_record else None)
	_set_first_existing(payload, meta, ["partner_payload", "partner_record_json"], json.dumps(partner_record, default=str, ensure_ascii=True) if partner_record else None)

	doc = frappe.get_doc(payload)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def _create_run_item_change(
	*,
	run_item_name: str,
	fieldname: str,
	old_value: Any,
	new_value: Any,
	source_side: str,
	target_side: str,
) -> Any:
	payload: dict[str, Any] = {"doctype": SYNC_RUN_ITEM_CHANGE}
	meta = frappe.get_meta(SYNC_RUN_ITEM_CHANGE)
	_set_first_existing(payload, meta, ["sync_run_item", "run_item"], run_item_name)
	_set_first_existing(payload, meta, ["changed_field", "fieldname", "field_name"], fieldname)
	_set_first_existing(payload, meta, ["old_value"], json.dumps(old_value, default=str, ensure_ascii=True))
	_set_first_existing(payload, meta, ["source_value"], json.dumps(old_value, default=str, ensure_ascii=True))
	_set_first_existing(payload, meta, ["new_value"], json.dumps(new_value, default=str, ensure_ascii=True))
	_set_first_existing(payload, meta, ["target_value"], json.dumps(new_value, default=str, ensure_ascii=True))
	_set_first_existing(payload, meta, ["source_side"], source_side)
	_set_first_existing(payload, meta, ["target_side"], target_side)
	doc = frappe.get_doc(payload)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def _update_doc_fields(doc: Any, values: dict[str, Any]) -> None:
	meta = frappe.get_meta(doc.doctype)
	for key, value in values.items():
		fieldname = _find_field(meta, [key])
		if fieldname:
			doc.db_set(fieldname, value, update_modified=False)
	frappe.db.commit()


def _update_definition_runtime(sync_definition_doc: Any, *, last_run: str, last_sync_at: datetime | None, summary: str | None = None):
	meta = frappe.get_meta(sync_definition_doc.doctype)
	updates = {
		"last_run": last_run,
		"last_run_status": "Success",
		"last_run_summary": summary,
		"last_sync_at": last_sync_at,
		"last_successful_sync": last_sync_at,
	}
	for fieldname, value in updates.items():
		if value is None:
			continue
		if meta.has_field(fieldname):
			sync_definition_doc.db_set(fieldname, value, update_modified=False)
	frappe.db.commit()


def _update_definition_failure(sync_definition_doc: Any, *, last_run: str, error_message: str):
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
	frappe.db.commit()


def _set_next_run_at(sync_definition_doc: Any, cron_expr: str | None):
	if not cron_expr or not croniter:
		return
	meta = frappe.get_meta(sync_definition_doc.doctype)
	next_field = _find_field(meta, ["next_run_at", "next_execution_at"])
	if not next_field:
		return
	try:
		next_run = croniter(cron_expr, now_datetime()).get_next(datetime)
	except Exception:
		frappe.logger("sync").warning("Invalid cron expression for %s: %s", sync_definition_doc.name, cron_expr)
		return
	sync_definition_doc.db_set(next_field, next_run, update_modified=False)
	frappe.db.commit()


def _get_last_successful_sync(sync_definition_name: str) -> datetime | None:
	run_meta = frappe.get_meta(SYNC_RUN)
	definition_field = _find_field(run_meta, ["sync_definition", "definition", "sync_definition_name"])
	status_field = _find_field(run_meta, ["status", "run_status"])
	if not definition_field or not status_field:
		return None
	fields = [field for field in ("last_sync_at", "finished_at", "started_at") if run_meta.has_field(field)]
	if not fields:
		fields = ["modified"]
	runs = frappe.get_all(
		SYNC_RUN,
		filters={definition_field: sync_definition_name, status_field: "Success"},
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


def _is_enabled(doc: Any) -> bool:
	return _as_bool(_first_value(doc, ["enabled", "is_enabled", "active"], default=1))


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
		for frappe_field in config.key_fields:
			value = None
			if frappe_record:
				value = frappe_record.get(frappe_field)
			if value in (None, "") and partner_record:
				partner_field = config.mapping.get(frappe_field, frappe_field)
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
		parts = [f"{field}={frappe_record.get(field)}" for field in config.key_fields if frappe_record.get(field) not in (None, "")]
		if parts:
			return " | ".join(parts)
	return _build_record_key(frappe_record or {})


def _compact_target_id(config: SyncDefinitionConfig | None, *, partner_record: dict[str, Any] | None) -> str:
	if config and partner_record:
		parts = []
		for frappe_field in config.key_fields:
			partner_field = config.mapping.get(frappe_field, frappe_field)
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
		frappe.logger("sync").warning("Invalid cron expression for %s: %s", sync_definition_doc.name, cron_expr)
		return False

	run_meta = frappe.get_meta(SYNC_RUN)
	definition_field = _find_field(run_meta, ["sync_definition", "definition", "sync_definition_name"])
	finished_field = _find_field(run_meta, ["finished_at", "end_time", "completed_at", "modified"])
	status_field = _find_field(run_meta, ["status", "run_status"])
	if not definition_field:
		return False

	filters = {definition_field: sync_definition_doc.name}
	if status_field:
		filters[status_field] = ["in", sorted(DONE_RUN_STATUSES)]
	fields = [finished_field] if finished_field else ["modified"]
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
