from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from sync.sync.constants import (
	ACTIVE_RUN_STATUSES,
	DONE_RUN_STATUSES,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
	RUN_STATUS_ERROR,
	RUN_STATUS_QUEUED,
	RUN_STATUS_SKIPPED,
	RUN_STATUS_SUCCESS,
	SYNC_DEFINITION,
	SYNC_PARTNER,
	SYNC_RUN,
	SYNC_RUN_ITEM,
)
from sync.sync.service import audit as audit_service
from sync.sync.service import config_access as config_access_service
from sync.sync.service import configuration as configuration_service
from sync.sync.service import mapping as mapping_service
from sync.sync.service import mapping_rules as mapping_rules_service
from sync.sync.service import time_utils as time_utils_service
from sync.sync.service import values as values_service
from sync.sync.service.connectors import get_connector_for_partner
from sync.sync.service.execution import writes as writes_service
from sync.sync.service.models import (
	SYNC_TYPE_FRAPPE_TO_PARTNER,
	SYNC_TYPE_PARTNER_TO_FRAPPE,
	SyncDefinitionConfig,
)


def recover_stale_runs(
	sync_definition_name: str | None = None,
	*,
	timeout_minutes: int | None = None,
	terminal_status: str | None = None,
) -> dict[str, Any]:
	settings = audit_service._get_sync_settings()
	timeout = values_service._positive_int(timeout_minutes, settings.stale_run_timeout_minutes)
	timeout = max(1, timeout)
	cutoff = now_datetime() - timedelta(minutes=timeout)
	recovered: list[dict[str, Any]] = []
	filters: dict[str, Any] = {"status": ["in", sorted(ACTIVE_RUN_STATUSES)]}
	if sync_definition_name:
		filters["sync_definition"] = str(sync_definition_name)
	rows = frappe.get_all(
		SYNC_RUN,
		filters=filters,
		fields=["name", "sync_definition", "status", "started_at", "creation", "modified"],
		order_by="creation asc",
	)

	for row in rows:
		run_status = str(values_service._row_value(row, "status") or "")
		last_activity_at = (
			time_utils_service._parse_datetime(values_service._row_value(row, "modified"))
			or time_utils_service._parse_datetime(values_service._row_value(row, "started_at"))
			or time_utils_service._parse_datetime(values_service._row_value(row, "creation"))
		)
		if last_activity_at and last_activity_at > cutoff:
			continue
		run_name = str(values_service._row_value(row, "name") or "")
		if not run_name:
			continue
		definition_name = values_service._clean_string(values_service._row_value(row, "sync_definition"))
		recovered_status = _stale_run_terminal_status(run_status, terminal_status)
		message = f"Recovered stale {run_status or 'active'} Sync Run after {timeout} minutes."
		run_doc = frappe.get_doc(SYNC_RUN, run_name)
		audit_service._update_doc_fields(
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
			audit_service._update_definition_stale_recovery(
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
	settings = audit_service._get_sync_settings()
	success_days = max(
		1, values_service._positive_int(retention_days_success, settings.run_retention_days_success)
	)
	error_days = max(1, values_service._positive_int(retention_days_error, settings.run_retention_days_error))
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
		run_name = str(values_service._row_value(row, "name") or "")
		if not run_name:
			continue
		status = str(values_service._row_value(row, "status") or "")
		cutoff = success_cutoff if status == RUN_STATUS_SUCCESS else error_cutoff
		completed_at = time_utils_service._parse_datetime(
			values_service._row_value(row, "finished_at")
		) or time_utils_service._parse_datetime(values_service._row_value(row, "creation"))
		if completed_at and completed_at > cutoff:
			continue
		for item_name in audit_service._linked_run_item_names(run_name):
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


def resolve_sync_run_item(sync_run_item_name: str, direction: str) -> dict[str, Any]:
	direction = values_service._clean_string(direction) or ""
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

	sync_definition_name = getattr(item_doc, "sync_definition", None) or getattr(
		run_doc, "sync_definition", None
	)
	if not sync_definition_name:
		raise frappe.ValidationError("Sync Run Item is missing Sync Definition.")
	config = configuration_service._build_definition_config(
		frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
	)
	if not config_access_service._update_existing_enabled(config):
		raise frappe.ValidationError("Update Existing is disabled for this Sync Definition.")

	try:
		if direction == SYNC_TYPE_PARTNER_TO_FRAPPE:
			written_after = _resolve_item_to_frappe(item_doc, config)
			message = "Manually accepted partner changes."
		else:
			written_after = _resolve_item_to_partner(item_doc, config)
			message = "Manually accepted frappe changes."
		audit_service._update_doc_fields(
			item_doc,
			{
				"action": "updated",
				"status": "success",
				"write_direction": direction,
				"message": message,
				"written_after_payload": audit_service._json_payload(written_after),
			},
		)
		return {"ok": True, "sync_run_item": item_doc.name, "write_direction": direction, "status": "success"}
	except Exception as exc:
		audit_service._update_doc_fields(
			item_doc,
			{
				"action": "error",
				"status": "error",
				"write_direction": direction,
				"message": str(exc),
			},
		)
		raise


def _resolve_item_to_frappe(item_doc: Any, config: SyncDefinitionConfig) -> dict[str, Any]:
	if not config_access_service._update_existing_enabled(config):
		raise frappe.ValidationError("Update Existing is disabled for this Sync Definition.")
	payload = _json_field_payload(item_doc, "frappe_resolution_payload")
	existing_name = values_service._clean_string(
		getattr(item_doc, "document_name", None)
	) or values_service._clean_string(payload.get("name"))
	if not existing_name:
		raise frappe.ValidationError("Sync Run Item is missing the Frappe document name.")
	doc_name = writes_service._upsert_frappe_record(
		doctype=config.doctype,
		existing_name=existing_name,
		payload=payload,
		dry_run=False,
		**writes_service._frappe_write_hook_kwargs(
			config=config,
			run_doc=None,
			event=FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
			partner_record=None,
			frappe_payload=payload,
			frappe_before_record=None,
			changes=None,
			dry_run=False,
		),
	)
	result = dict(payload)
	if doc_name:
		result["name"] = doc_name
	return result


def _resolve_item_to_partner(item_doc: Any, config: SyncDefinitionConfig) -> dict[str, Any]:
	if not config_access_service._update_existing_enabled(config):
		raise frappe.ValidationError("Update Existing is disabled for this Sync Definition.")
	payload = _json_field_payload(item_doc, "partner_resolution_payload")
	key_values = _manual_partner_key_values(config, payload)
	mapping_context = mapping_service._build_runtime_mapping_context(config)
	connector = get_connector_for_partner(frappe.get_doc(SYNC_PARTNER, config.partner))
	write = connector.upsert_record(
		record=payload,
		key_values=key_values,
		mapping=mapping_context.connector_mapping,
		dry_run=False,
		source=config.table_name,
		create_options=writes_service._build_partner_create_options(config),
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
	partner_identity_field = config_access_service._config_partner_identity_field(config)
	if partner_identity_field and payload.get(partner_identity_field) not in (None, ""):
		return {partner_identity_field: payload.get(partner_identity_field)}
	key_values = {}
	for frappe_field in config_access_service._config_match_fields(config):
		partner_field = mapping_rules_service._partner_field_for_mapping(
			config.mapping, frappe_field, frappe_field
		)
		value = payload.get(partner_field)
		if value in (None, ""):
			raise frappe.ValidationError(
				f"Manual resolution payload is missing partner key field {partner_field}."
			)
		key_values[partner_field] = value
	if not key_values:
		raise frappe.ValidationError("Manual resolution payload has no partner key values.")
	return key_values


def _stale_run_terminal_status(previous_status: str, requested_status: str | None = None) -> str:
	if requested_status in {RUN_STATUS_ERROR, RUN_STATUS_SKIPPED}:
		return requested_status
	return RUN_STATUS_SKIPPED if previous_status == RUN_STATUS_QUEUED else RUN_STATUS_ERROR


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
	audit_service._set_doc_values(partner_doc, updates)
	frappe.db.commit()
