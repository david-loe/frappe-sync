from __future__ import annotations

import json
from typing import Any

import frappe

from sync.sync.service import (
	SyncPreviewService,
	enqueue_sync_definition as service_enqueue_sync_definition,
	execute_sync_definition as service_execute_sync_definition,
	export_sync_definition_yaml as service_export_sync_definition_yaml,
	import_sync_definition_yaml as service_import_sync_definition_yaml,
	list_due_sync_definitions as service_list_due_sync_definitions,
	preview_sync_definition as service_preview_sync_definition,
	run_due_sync_definitions as service_run_due_sync_definitions,
	test_sync_partner_connection,
)
from sync.sync.service.connectors import get_connector_for_partner
from sync.sync.service.runtime import preview_import_sync_definition_yaml as service_preview_import_sync_definition_yaml


def _as_bool(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Any, default: int) -> int:
	try:
		return int(value)
	except Exception:
		return default


def _parse_json_payload(value: Any) -> dict[str, Any]:
	if isinstance(value, dict):
		return value
	if not value:
		return {}
	if isinstance(value, str):
		loaded = json.loads(value)
		if isinstance(loaded, dict):
			return loaded
	return {}


def _clean_string(value: Any) -> str | None:
	if value is None:
		return None
	cleaned = str(value).strip()
	return cleaned or None


_NON_SELECTABLE_FIELD_TYPES = {
	"Button",
	"Column Break",
	"Fold",
	"HTML",
	"Section Break",
	"Tab Break",
	"Table",
	"Table MultiSelect",
}


def _build_doctype_field_choices(doctype_name: str) -> list[dict[str, str]]:
	meta = frappe.get_meta(doctype_name)
	choices: list[dict[str, str]] = []
	seen: set[str] = set()

	for fieldname, label in (("name", "Name"), ("modified", "Modified")):
		choices.append({"fieldname": fieldname, "label": label, "fieldtype": "Data"})
		seen.add(fieldname)

	for field in getattr(meta, "fields", []) or []:
		fieldname = str(getattr(field, "fieldname", "") or "").strip()
		if not fieldname or fieldname in seen:
			continue
		if _as_bool(getattr(field, "hidden", 0)):
			continue
		fieldtype = str(getattr(field, "fieldtype", "") or "").strip()
		if fieldtype in _NON_SELECTABLE_FIELD_TYPES:
			continue
		label = str(getattr(field, "label", "") or "").strip() or fieldname
		choices.append({"fieldname": fieldname, "label": label, "fieldtype": fieldtype or "Data"})
		seen.add(fieldname)

	return choices


@frappe.whitelist()
def list_due_syncs() -> list[str]:
	return service_list_due_sync_definitions()


@frappe.whitelist()
def get_sync_definition_field_choices(doctype_name: str) -> dict[str, Any]:
	doctype_name = str(doctype_name or "").strip()
	if not doctype_name:
		return {"doctype": "", "fields": []}
	return {"doctype": doctype_name, "fields": _build_doctype_field_choices(doctype_name)}


@frappe.whitelist()
def get_sync_partner_table_columns(
	sync_partner_name: str,
	table_name: str | None = None,
	query: str | None = None,
) -> dict[str, Any]:
	partner_doc = frappe.get_doc("Sync Partner", sync_partner_name)
	connector = get_connector_for_partner(partner_doc)
	normalized_table_name = _clean_string(table_name)
	normalized_query = _clean_string(query)
	try:
		columns = connector.describe_source_columns(source=normalized_table_name, query=normalized_query)
	except Exception as exc:
		frappe.throw(str(exc), exc=frappe.ValidationError)
	return {
		"sync_partner": partner_doc.name,
		"table_name": normalized_table_name,
		"columns": columns,
	}


@frappe.whitelist()
def run_due_syncs(limit: int = 20, queue: bool = True) -> list[dict[str, Any]]:
	return service_run_due_sync_definitions(limit=_as_int(limit, 20), queue=_as_bool(queue))


@frappe.whitelist()
def enqueue_sync(sync_definition_name: str, trigger: str = "manual", queue: bool = True, dry_run: bool = False):
	return service_enqueue_sync_definition(
		sync_definition_name,
		trigger=trigger,
		queue=_as_bool(queue),
		dry_run=_as_bool(dry_run),
	)


@frappe.whitelist()
def run_sync_now(sync_definition_name: str, trigger: str = "manual", dry_run: bool = False):
	return service_execute_sync_definition(sync_definition_name, trigger=trigger, dry_run=_as_bool(dry_run))


@frappe.whitelist()
def run_sync_definition(sync_definition_name: str, trigger: str = "manual", queue: bool = True, dry_run: bool = False):
	return service_enqueue_sync_definition(
		sync_definition_name,
		trigger=trigger,
		queue=_as_bool(queue),
		dry_run=_as_bool(dry_run),
	)


@frappe.whitelist()
def test_sync_partner(sync_partner_name: str) -> dict[str, Any]:
	partner_doc = frappe.get_doc("Sync Partner", sync_partner_name)
	connector = get_connector_for_partner(partner_doc)
	if hasattr(connector, "test_connection"):
		result = connector.test_connection()
		if isinstance(result, dict):
			return result
	ping = connector.ping()
	return {
		"status": "ok" if ping.ok else "error",
		"ok": ping.ok,
		"message": ping.message,
		"details": ping.details,
	}


@frappe.whitelist()
def preview_sync(sync_definition_name: str, limit: int = 50) -> dict[str, Any]:
	return preview_sync_definition(sync_definition_name, limit=_as_int(limit, 50))


@frappe.whitelist()
def preview_sync_definition(sync_definition_name: str, limit: int = 50) -> dict[str, Any]:
	sync_definition = frappe.get_doc("Sync Definition", sync_definition_name)
	return SyncPreviewService.predict(sync_definition, limit=_as_int(limit, 50))


@frappe.whitelist()
def export_sync_yaml(sync_definition_name: str) -> str:
	return service_export_sync_definition_yaml(sync_definition_name)


@frappe.whitelist()
def export_sync_definition_yaml(sync_definition_name: str) -> str:
	return service_export_sync_definition_yaml(sync_definition_name)


@frappe.whitelist()
def import_sync_yaml(yaml_payload: str, overwrite: bool = False) -> dict[str, Any]:
	return service_import_sync_definition_yaml(yaml_payload=yaml_payload, overwrite=_as_bool(overwrite))


@frappe.whitelist()
def preview_import_sync_yaml(yaml_payload: str, overwrite: bool = False) -> dict[str, Any]:
	return service_preview_import_sync_definition_yaml(yaml_payload=yaml_payload, overwrite=_as_bool(overwrite))


@frappe.whitelist()
def preview_import_sync_definition_yaml(yaml_payload: str, overwrite: bool = False) -> dict[str, Any]:
	return service_preview_import_sync_definition_yaml(yaml_payload=yaml_payload, overwrite=_as_bool(overwrite))


@frappe.whitelist()
def import_sync_definition_yaml(yaml_payload: str, overwrite: bool = False):
	result = service_import_sync_definition_yaml(yaml_payload=yaml_payload, overwrite=_as_bool(overwrite))
	documents = result.get("documents", {})
	return documents.get("Sync Definition") or result


@frappe.whitelist()
def run_due_sync_definitions(limit: int = 20, queue: bool = True) -> list[dict[str, Any]]:
	return service_run_due_sync_definitions(limit=_as_int(limit, 20), queue=_as_bool(queue))


@frappe.whitelist()
def import_sync_yaml_from_json(payload: str | dict[str, Any], overwrite: bool = False) -> dict[str, Any]:
	body = _parse_json_payload(payload)
	return import_sync_definition_yaml(
		yaml_payload=str(body.get("yaml_payload", "")),
		overwrite=_as_bool(body.get("overwrite", overwrite)),
	)
