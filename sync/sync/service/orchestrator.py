from __future__ import annotations

from contextlib import nullcontext, suppress
from dataclasses import replace
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

try:
	from rq.timeouts import JobTimeoutException
except Exception:  # pragma: no cover - RQ is available in normal Frappe workers

	class JobTimeoutException(Exception):
		pass


from sync.sync.constants import (
	RUN_STATUS_ERROR,
	RUN_STATUS_QUEUED,
	RUN_STATUS_RUNNING,
	RUN_STATUS_SUCCESS,
	SYNC_DEFINITION,
	SYNC_PARTNER,
	SYNC_RUN,
	TRIGGER_MANUAL,
)
from sync.sync.service import audit as audit_service
from sync.sync.service import config_access as config_access_service
from sync.sync.service import configuration as configuration_service
from sync.sync.service import management as management_service
from sync.sync.service import query_templates as query_templates_service
from sync.sync.service import scheduling as scheduling_service
from sync.sync.service import values as values_service
from sync.sync.service.connectors import get_connector_for_partner
from sync.sync.service.execution import engine as engine_service
from sync.sync.service.execution import sources as sources_service
from sync.sync.service.models import (
	DEFAULT_STALE_RUN_TIMEOUT_MINUTES,
	SYNC_DEFINITION_LOCK_TIMEOUT_SECONDS,
	SyncContext,
)


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


def enqueue_sync_definition(
	sync_definition_name: str,
	*,
	trigger: str = TRIGGER_MANUAL,
	queue: bool = True,
	dry_run: bool = False,
) -> dict[str, Any]:
	sync_definition_name = str(sync_definition_name)
	trigger = audit_service._normalize_trigger_type(trigger)
	lock_key = f"sync:lock:{sync_definition_name}"
	with _definition_lock(lock_key):
		if audit_service._has_active_run(sync_definition_name):
			return {"status": "already_running", "sync_definition": sync_definition_name}

		sync_definition = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
		run_doc = audit_service._create_run_doc(
			sync_definition, status=RUN_STATUS_QUEUED, trigger=trigger, dry_run=dry_run
		)

	if not queue:
		return execute_sync_definition(
			sync_definition_name,
			trigger=trigger,
			dry_run=dry_run,
			run_name=run_doc.name,
		)

	job_id = f"sync:run:{sync_definition_name}:{frappe.generate_hash(length=8)}"
	audit_service._update_doc_fields(run_doc, {"status": RUN_STATUS_QUEUED, "job_id": job_id})
	job_timeout = (
		values_service._positive_int(
			audit_service._get_sync_settings().stale_run_timeout_minutes,
			DEFAULT_STALE_RUN_TIMEOUT_MINUTES,
		)
		* 60
	)
	frappe.enqueue(
		"sync.sync.service.orchestrator.run_sync_definition_job",
		queue="long",
		timeout=job_timeout,
		job_id=job_id,
		sync_definition_name=sync_definition_name,
		run_name=run_doc.name,
		trigger=trigger,
		dry_run=dry_run,
	)
	return {
		"status": "queued",
		"sync_definition": sync_definition_name,
		"run": run_doc.name,
		"job_id": job_id,
	}


def run_sync_definition_job(
	sync_definition_name: str,
	run_name: str | None = None,
	trigger: str = TRIGGER_MANUAL,
	dry_run: bool = False,
):
	return execute_sync_definition(sync_definition_name, trigger=trigger, dry_run=dry_run, run_name=run_name)


def execute_sync_definition(
	sync_definition_name: str,
	*,
	trigger: str = TRIGGER_MANUAL,
	dry_run: bool = False,
	run_name: str | None = None,
) -> dict[str, Any]:
	sync_definition_name = str(sync_definition_name)
	trigger = audit_service._normalize_trigger_type(trigger)
	lock_key = f"sync:lock:{sync_definition_name}"
	with _definition_lock(lock_key):
		if run_name:
			run_doc = frappe.get_doc(SYNC_RUN, run_name)
		else:
			if audit_service._has_active_run(sync_definition_name):
				return {"status": "already_running", "sync_definition": sync_definition_name}
			sync_definition_doc = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
			run_doc = audit_service._create_run_doc(
				sync_definition_doc, status=RUN_STATUS_QUEUED, trigger=trigger, dry_run=dry_run
			)

		sync_definition = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
		run_started_at = now_datetime()
		audit_service._update_doc_fields(
			run_doc, {"status": RUN_STATUS_RUNNING, "started_at": run_started_at, "trigger_type": trigger}
		)

		try:
			config = configuration_service._build_definition_config(sync_definition)
			last_successful_sync = audit_service._get_last_successful_sync(sync_definition_name)
			context = SyncContext(config=config, dry_run=dry_run, last_successful_sync=last_successful_sync)
			result_payload = engine_service._run_engine(sync_definition, run_doc, context=context)

			terminal_status = audit_service._terminal_status_for_result(result_payload)
			sync_stamp = run_started_at if terminal_status == RUN_STATUS_SUCCESS and not dry_run else None
			audit_service._update_doc_fields(
				run_doc,
				{
					"status": terminal_status,
					"finished_at": now_datetime(),
					"last_sync_at": sync_stamp,
					"summary": audit_service._format_run_summary(result_payload),
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
				audit_service._update_definition_runtime(
					sync_definition,
					last_run=run_doc.name,
					status=terminal_status,
					last_sync_at=sync_stamp,
					summary=audit_service._format_run_summary(result_payload),
					commit=False,
				)
			scheduling_service._set_next_run_at(sync_definition, config.cron, commit=False)
			frappe.db.commit()
			return {
				"status": audit_service._api_status_for_run_status(terminal_status),
				"run": run_doc.name,
				"result": result_payload,
			}
		except Exception as exc:
			error_traceback = frappe.get_traceback(with_context=False)
			if isinstance(exc, JobTimeoutException):
				_reconnect_database_after_job_timeout()
				run_doc = frappe.get_doc(SYNC_RUN, run_doc.name)
				sync_definition = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
			frappe.log_error(error_traceback, f"Sync execution failed for {sync_definition_name}")
			audit_service._update_doc_fields(
				run_doc,
				{
					"status": RUN_STATUS_ERROR,
					"finished_at": now_datetime(),
					"error_message": error_traceback,
				},
				commit=False,
			)
			if not dry_run:
				audit_service._update_definition_failure(
					sync_definition,
					last_run=run_doc.name,
					error_message=error_traceback,
					commit=False,
				)
			frappe.db.commit()
			raise


def _reconnect_database_after_job_timeout() -> None:
	"""Discard a connection interrupted by RQ's timeout signal before audit writes."""
	database = frappe.db
	with suppress(Exception):
		database.close()
	database.connect()


def test_sync_partner_connection(sync_partner_name: str) -> dict[str, Any]:
	partner_doc = frappe.get_doc(SYNC_PARTNER, sync_partner_name)
	connector = get_connector_for_partner(partner_doc)
	result = connector.ping()
	status = "ok" if result.ok else "error"
	management_service._update_partner_connection_status(partner_doc, status=status, details=result.message)
	return {"status": status, "ok": result.ok, "message": result.message, "details": result.details}


def preview_sync_definition(sync_definition_name: str, limit: int = 50) -> dict[str, Any]:
	sync_definition = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
	return SyncPreviewService.predict(sync_definition, limit=limit)


def _build_preview(sync_definition: Any, *, limit: int) -> dict[str, Any]:
	config = configuration_service._build_definition_config(sync_definition)
	partner_doc = frappe.get_doc(SYNC_PARTNER, config.partner)
	config = configuration_service._merge_partner_runtime_settings(config, partner_doc)
	mapping = config.mapping
	connector = get_connector_for_partner(partner_doc)
	ping = connector.ping()
	preview_context = SyncContext(
		config=replace(config, batch_size=cint(limit) or 50), dry_run=True, last_successful_sync=None
	)
	frappe_records = []
	for batch in sources_service._iter_frappe_source_batches(
		preview_context.config, preview_context, apply_delta_filter=False
	):
		frappe_records.extend(batch)
		if len(frappe_records) >= cint(limit):
			frappe_records = frappe_records[: cint(limit)]
			break
	return {
		"sync_definition": config.name,
		"sync_type": config.sync_type,
		"frappe_source_mode": config.frappe_source_mode,
		"partner": config.partner,
		"connector": type(connector).__name__,
		"partner_ping": {"ok": ping.ok, "message": ping.message, "details": ping.details},
		"frappe_records_sample_count": len(frappe_records),
		"frappe_records_sample": frappe_records,
		"mapping": mapping,
		"match_mode": config_access_service._config_match_mode(config),
		"match_fields": config_access_service._config_match_fields(config),
		"read_query": config_access_service._config_read_query(config),
		"render_read_query_template": config_access_service._config_render_read_query_template(config),
		"rendered_read_query": query_templates_service.resolve_read_query(config, connector),
		"partner_identity_field": config_access_service._config_partner_identity_field(config),
		"value_mapping_fields": sorted(config.value_mapping.keys()),
		"computed_fields": [field.field_name for field in config.computed_fields],
		"actions": [{"direction": config.sync_type, "result": "preview"}],
	}


def _definition_lock(lock_key: str):
	cache = frappe.cache()
	lock = getattr(cache, "lock", None)
	if not callable(lock):
		return nullcontext()
	return cache.lock(lock_key, timeout=SYNC_DEFINITION_LOCK_TIMEOUT_SECONDS, blocking_timeout=10)
