from __future__ import annotations

import json
from contextlib import suppress
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from sync.sync.constants import (
	ACTIVE_RUN_STATUSES,
	RUN_STATUS_ERROR,
	RUN_STATUS_NEEDS_REVIEW,
	RUN_STATUS_PARTIAL_ERROR,
	RUN_STATUS_SUCCESS,
	SYNC_DEFINITION,
	SYNC_RUN,
	SYNC_RUN_ITEM,
	SYNC_SETTINGS,
	TRIGGER_MANUAL,
	VALID_TRIGGER_TYPES,
)
from sync.sync.service import changes as changes_service
from sync.sync.service import config_access as config_access_service
from sync.sync.service import mapping as mapping_service
from sync.sync.service import mapping_rules as mapping_rules_service
from sync.sync.service import metadata as metadata_service
from sync.sync.service import time_utils as time_utils_service
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	AUDIT_RECORD_UNSET,
	DEFAULT_RUN_RETENTION_DAYS_ERROR,
	DEFAULT_RUN_RETENTION_DAYS_SUCCESS,
	DEFAULT_RUNTIME_COMMIT_BATCH,
	DEFAULT_STALE_RUN_TIMEOUT_MINUTES,
	RUN_DOC_PENDING_WRITES_ATTR,
	SYNC_TYPE_BIDIRECTIONAL,
	SYNC_TYPE_FRAPPE_TO_PARTNER,
	SYNC_TYPE_PARTNER_TO_FRAPPE,
	RuntimeMappingContext,
	SyncDefinitionConfig,
	SyncStats,
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
	frappe_resolution_payload = mapping_service._with_frappe_modified_timestamp(
		config,
		partner_record,
		frappe_payload,
		mapping_context=mapping_context,
	)
	if frappe_record.get("name"):
		frappe_resolution_payload["name"] = frappe_record.get("name")
	partner_resolution_payload = mapping_service._with_partner_timestamps(
		config,
		frappe_record,
		mapping_service._apply_partner_link_fields(config, frappe_record, partner_payload),
		create=False,
		mapping_context=mapping_context,
	)
	partner_identity_field = config_access_service._config_partner_identity_field(config)
	if partner_identity_field and partner_record.get(partner_identity_field) not in (None, ""):
		partner_resolution_payload[partner_identity_field] = partner_record.get(partner_identity_field)
	return frappe_resolution_payload, partner_resolution_payload


def _log_manual_bidirectional_conflict(
	*,
	stats: SyncStats,
	run_doc: Any,
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	partner_record: dict[str, Any],
	frappe_payload: dict[str, Any],
	partner_payload: dict[str, Any],
	mapping_context: RuntimeMappingContext,
	to_frappe_changes: list[tuple[str, Any, Any]],
	to_partner_changes: list[tuple[str, Any, Any]],
) -> None:
	frappe_resolution_payload, partner_resolution_payload = _manual_conflict_resolution_payloads(
		config=config,
		frappe_record=frappe_record,
		partner_record=partner_record,
		frappe_payload=frappe_payload,
		partner_payload=partner_payload,
		mapping_context=mapping_context,
	)
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
		changes=changes_service._canonical_conflict_changes(
			config,
			to_frappe_changes=to_frappe_changes,
			to_partner_changes=to_partner_changes,
		),
		frappe_before_record=frappe_record,
		partner_before_record=partner_record,
		frappe_resolution_payload=frappe_resolution_payload,
		partner_resolution_payload=partner_resolution_payload,
		write_direction=None,
		commit=False,
	)


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
	frappe_before_record: dict[str, Any] | object | None = AUDIT_RECORD_UNSET,
	partner_before_record: dict[str, Any] | object | None = AUDIT_RECORD_UNSET,
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
			"sync_type": values_service._first_value(
				sync_definition_doc, ["sync_type"], default="Frappe -> Partner"
			),
			"sync_partner": values_service._first_value(sync_definition_doc, ["partner"]),
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
	frappe_before_record: dict[str, Any] | object | None = AUDIT_RECORD_UNSET,
	partner_before_record: dict[str, Any] | object | None = AUDIT_RECORD_UNSET,
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
	planned_direction = direction or values_service._first_value(run_doc, ["sync_type"])
	actual_write_direction = write_direction or _default_write_direction(action, status, planned_direction)
	source_id, target_id = _source_and_target_ids(
		config,
		direction=actual_write_direction or planned_direction,
		frappe_record=frappe_record,
		partner_record=partner_record,
	)
	metadata_service._set_first_existing(
		payload, meta, ["document_name", "frappe_name", "frappe_record_name"], record_name
	)
	metadata_service._set_first_existing(payload, meta, ["record_key"], _fit_data_value(record_key))
	metadata_service._set_first_existing(payload, meta, ["write_direction"], actual_write_direction)
	metadata_service._set_first_existing(payload, meta, ["source_id"], _fit_data_value(source_id))
	metadata_service._set_first_existing(payload, meta, ["target_id"], _fit_data_value(target_id))
	metadata_service._set_first_existing(payload, meta, ["change_count"], len(changes or []))
	metadata_service._set_first_existing(
		payload, meta, ["changed_fields"], _summarize_changed_fields(changes)
	)
	if frappe_resolution_payload is not None:
		metadata_service._set_first_existing(
			payload, meta, ["frappe_resolution_payload"], _json_payload(frappe_resolution_payload)
		)
	if partner_resolution_payload is not None:
		metadata_service._set_first_existing(
			payload, meta, ["partner_resolution_payload"], _json_payload(partner_resolution_payload)
		)
	if _capture_audit_payloads(config):
		frappe_before_record = (
			frappe_record if frappe_before_record is AUDIT_RECORD_UNSET else frappe_before_record
		)
		partner_before_record = (
			partner_record if partner_before_record is AUDIT_RECORD_UNSET else partner_before_record
		)
		metadata_service._set_first_existing(
			payload, meta, ["frappe_before_payload"], _json_payload(frappe_before_record)
		)
		metadata_service._set_first_existing(
			payload, meta, ["partner_before_payload"], _json_payload(partner_before_record)
		)
		metadata_service._set_first_existing(
			payload, meta, ["written_after_payload"], _json_payload(written_after_record)
		)

	doc = frappe.get_doc(payload)
	doc.insert(ignore_permissions=True)
	if commit:
		_touch_run_activity(run_doc)
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


def _json_payload(record: dict[str, Any] | object | None) -> str:
	return json.dumps(record, default=str, ensure_ascii=True)


def _summarize_changed_fields(changes: list[tuple[str, Any, Any]] | None) -> str | None:
	field_names = [
		values_service._clean_string(field_name) for field_name, _old_value, _new_value in changes or []
	]
	normalized = [field_name for field_name in field_names if field_name]
	if not normalized:
		return None
	return ", ".join(normalized)


def _capture_audit_payloads(config: SyncDefinitionConfig | Any | None) -> bool:
	return values_service._as_bool(getattr(config, "capture_audit_payloads", 0))


def _runtime_commit_batch_size(config: SyncDefinitionConfig | None) -> int:
	if not config:
		return DEFAULT_RUNTIME_COMMIT_BATCH
	batch_size = (
		cint(getattr(config, "batch_size", DEFAULT_RUNTIME_COMMIT_BATCH)) or DEFAULT_RUNTIME_COMMIT_BATCH
	)
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
	_touch_run_activity(run_doc)
	frappe.db.commit()
	setattr(run_doc, RUN_DOC_PENDING_WRITES_ATTR, 0)


def _touch_run_activity(run_doc: Any) -> None:
	run_name = values_service._doc_name(run_doc)
	set_value = getattr(getattr(frappe, "db", None), "set_value", None)
	if not run_name or not callable(set_value):
		return
	activity_at = now_datetime()
	set_value(SYNC_RUN, run_name, "modified", activity_at, update_modified=False)
	with suppress(Exception):
		run_doc.modified = activity_at


def _set_doc_values(doc: Any, values: dict[str, Any]) -> None:
	if not values:
		return
	set_value = getattr(getattr(frappe, "db", None), "set_value", None)
	doc_name = values_service._doc_name(doc)
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
		fieldname = metadata_service._find_field(meta, [key])
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


def _update_definition_failure(
	sync_definition_doc: Any, *, last_run: str, error_message: str, commit: bool = True
):
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
		definition_value = frappe.db.get_value(SYNC_DEFINITION, sync_definition_name, "last_successful_sync")
		return time_utils_service._parse_datetime(definition_value)
	for fieldname in ("last_sync_at", "finished_at", "started_at", "modified"):
		value = runs[0].get(fieldname)
		parsed = time_utils_service._parse_datetime(value)
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
			values[fieldname] = values_service._positive_int(
				get_single_value(SYNC_SETTINGS, fieldname), default
			)
		except Exception:
			values[fieldname] = default
	return SimpleNamespace(**values)


def _linked_run_item_names(run_name: str) -> list[str]:
	rows = frappe.get_all(
		SYNC_RUN_ITEM,
		filters={"sync_run": run_name},
		fields=["name"],
		order_by=None,
	)
	return [
		str(values_service._row_value(row, "name")) for row in rows if values_service._row_value(row, "name")
	]


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
	normalized = values_service._clean_string(trigger) or TRIGGER_MANUAL
	if normalized not in VALID_TRIGGER_TYPES:
		raise frappe.ValidationError(
			f"Trigger Type must be one of: {', '.join(sorted(VALID_TRIGGER_TYPES))}."
		)
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


def _build_record_key(record: dict[str, Any]) -> str:
	if not record:
		return ""
	if record.get("name"):
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
		for frappe_field in config_access_service._config_match_fields(config):
			value = None
			if frappe_record:
				value = frappe_record.get(frappe_field)
			if value in (None, "") and partner_record:
				partner_field = mapping_rules_service._partner_field_for_mapping(
					config.mapping, frappe_field, frappe_field
				)
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
		parts = [
			f"{field}={frappe_record.get(field)}"
			for field in config_access_service._config_match_fields(config)
			if frappe_record.get(field) not in (None, "")
		]
		if parts:
			return " | ".join(parts)
	return _build_record_key(frappe_record or {})


def _compact_target_id(config: SyncDefinitionConfig | None, *, partner_record: dict[str, Any] | None) -> str:
	if config and partner_record:
		parts = []
		for frappe_field in config_access_service._config_match_fields(config):
			partner_field = mapping_rules_service._partner_field_for_mapping(
				config.mapping, frappe_field, frappe_field
			)
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
