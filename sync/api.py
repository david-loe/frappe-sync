from __future__ import annotations

import json
from typing import Any

import frappe

from sync.sync.constants import (
	SYNC_DEFINITION,
	SYNC_PARTNER,
	SYNC_PARTNER_TYPE,
	SYNC_RUN_ITEM,
	TRIGGER_MANUAL,
	VALID_TRIGGER_TYPES,
)
from sync.sync.service import (
	SyncPreviewService,
	cleanup_sync_run_retention as service_cleanup_sync_run_retention,
	enqueue_sync_definition as service_enqueue_sync_definition,
	execute_sync_definition as service_execute_sync_definition,
	export_sync_definition_yaml as service_export_sync_definition_yaml,
	import_sync_definition_yaml as service_import_sync_definition_yaml,
	list_due_sync_definitions as service_list_due_sync_definitions,
	recover_stale_runs as service_recover_stale_runs,
	resolve_sync_run_item as service_resolve_sync_run_item,
	run_due_sync_definitions as service_run_due_sync_definitions,
)
from sync.sync.service.connectors import get_connector_for_partner
from sync.sync.service.runtime import (
	preview_import_sync_definition_yaml as service_preview_import_sync_definition_yaml,
)


SYSTEM_MANAGER_ROLE = "System Manager"
SYNC_DEFINITION_DOCTYPE = SYNC_DEFINITION
SYNC_PARTNER_DOCTYPE = SYNC_PARTNER
SYNC_PARTNER_TYPE_DOCTYPE = SYNC_PARTNER_TYPE
SYNC_RUN_ITEM_DOCTYPE = SYNC_RUN_ITEM


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


def _require_system_manager() -> None:
	frappe.only_for(SYSTEM_MANAGER_ROLE)


def _get_doc_value(doc: Any, *fieldnames: str) -> Any:
	getter = getattr(doc, "get", None)
	for fieldname in fieldnames:
		value = getter(fieldname) if callable(getter) else getattr(doc, fieldname, None)
		if value not in (None, ""):
			return value
	return None


def _require_doctype_permission(doctype_name: str | None, permtype: str = "read") -> str | None:
	normalized_doctype = _clean_string(doctype_name)
	if not normalized_doctype:
		return None
	frappe.has_permission(doctype=normalized_doctype, ptype=permtype, throw=True)
	return normalized_doctype


def _check_doc_permission(doc: Any, permtype: str = "read") -> None:
	check_permission = getattr(doc, "check_permission", None)
	if callable(check_permission):
		check_permission(permtype)
		return
	frappe.has_permission(doctype=getattr(doc, "doctype", None), ptype=permtype, doc=doc, throw=True)


def _require_doc_permission(doctype: str, docname: str, permtype: str = "read") -> Any:
	doc = frappe.get_doc(doctype, docname)
	_check_doc_permission(doc, permtype=permtype)
	return doc


def _require_sync_definition_permission(
	sync_definition_name: str,
	*,
	permtype: str = "read",
	check_partner: bool = False,
	check_target_doctype: bool = False,
) -> Any:
	sync_definition = _require_doc_permission(SYNC_DEFINITION_DOCTYPE, sync_definition_name, permtype=permtype)
	if check_partner:
		partner_name = _clean_string(_get_doc_value(sync_definition, "partner"))
		if partner_name:
			_require_doc_permission(SYNC_PARTNER_DOCTYPE, partner_name, permtype="read")
	if check_target_doctype:
		_require_doctype_permission(_get_doc_value(sync_definition, "doctype_name"), permtype="read")
	return sync_definition


def _normalize_import_result(result: dict[str, Any], *, overwrite: bool) -> dict[str, Any]:
	documents = result.get("documents") or {}
	sync_definition_name = _clean_string(documents.get(SYNC_DEFINITION_DOCTYPE))
	return {
		"ok": _as_bool(result.get("ok", True)),
		"overwrite": _as_bool(overwrite),
		"sync_definition": sync_definition_name,
		"sync_partner": _clean_string(documents.get(SYNC_PARTNER_DOCTYPE)),
		"sync_partner_type": _clean_string(documents.get(SYNC_PARTNER_TYPE_DOCTYPE)),
		"documents": documents,
	}


def _validate_trigger_type(trigger: str | None) -> str:
	normalized = _clean_string(trigger) or TRIGGER_MANUAL
	if normalized not in VALID_TRIGGER_TYPES:
		frappe.throw(
			f"Trigger Type must be one of: {', '.join(sorted(VALID_TRIGGER_TYPES))}.",
			exc=frappe.ValidationError,
		)
	return normalized


def _require_import_permissions(preview: dict[str, Any], *, overwrite: bool) -> None:
	for doctype, document_info in (preview.get("documents") or {}).items():
		status = str(document_info.get("status") or "")
		name = _clean_string(document_info.get("name"))
		exists = _as_bool(document_info.get("exists"))
		action = str(document_info.get("action") or "")
		if status in {"invalid", "missing_payload"}:
			continue
		if exists and name:
			permtype = "write" if action == "overwrite" else "read"
			_require_doc_permission(doctype, name, permtype=permtype)
			continue
		frappe.has_permission(doctype=doctype, ptype="create", throw=True)


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

	for fieldname, label in (("name", "Name"), ("creation", "Created On"), ("modified", "Modified")):
		choices.append({"fieldname": fieldname, "label": label, "fieldtype": "Data"})
		seen.add(fieldname)

	for field in getattr(meta, "fields", []) or []:
		fieldname = str(getattr(field, "fieldname", "") or "").strip()
		if not fieldname or fieldname in seen:
			continue
		fieldtype = str(getattr(field, "fieldtype", "") or "").strip()
		if fieldtype in _NON_SELECTABLE_FIELD_TYPES:
			continue
		label = str(getattr(field, "label", "") or "").strip() or fieldname
		choices.append({"fieldname": fieldname, "label": label, "fieldtype": fieldtype or "Data"})
		seen.add(fieldname)

	return choices


def _build_doctype_table_field_choices(doctype_name: str) -> list[dict[str, str]]:
	meta = frappe.get_meta(doctype_name)
	choices: list[dict[str, str]] = []
	for field in getattr(meta, "fields", []) or []:
		fieldname = str(getattr(field, "fieldname", "") or "").strip()
		if not fieldname or str(getattr(field, "fieldtype", "") or "").strip() != "Table":
			continue
		label = str(getattr(field, "label", "") or "").strip() or fieldname
		options = str(getattr(field, "options", "") or "").strip()
		choices.append({"fieldname": fieldname, "label": label, "fieldtype": "Table", "options": options})
	return choices


def _build_child_field_choices(table_fields: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
	result: dict[str, list[dict[str, str]]] = {}
	for table_field in table_fields:
		child_doctype = table_field.get("options")
		if not child_doctype:
			continue
		choices: list[dict[str, str]] = []
		try:
			child_meta = frappe.get_meta(child_doctype)
		except Exception:
			result[table_field["fieldname"]] = choices
			continue
		for child_field in getattr(child_meta, "fields", []) or []:
			fieldname = str(getattr(child_field, "fieldname", "") or "").strip()
			if not fieldname:
				continue
			fieldtype = str(getattr(child_field, "fieldtype", "") or "").strip()
			if fieldtype in _NON_SELECTABLE_FIELD_TYPES:
				continue
			label = str(getattr(child_field, "label", "") or "").strip() or fieldname
			choices.append({"fieldname": fieldname, "label": label, "fieldtype": fieldtype or "Data"})
		result[table_field["fieldname"]] = choices
	return result


@frappe.whitelist()
def list_due_syncs() -> list[str]:
	_require_system_manager()
	return service_list_due_sync_definitions()


@frappe.whitelist()
def get_sync_definition_field_choices(doctype_name: str) -> dict[str, Any]:
	_require_system_manager()
	doctype_name = str(doctype_name or "").strip()
	if not doctype_name:
		return {"doctype": "", "fields": []}
	_require_doctype_permission(doctype_name, permtype="read")
	table_fields = _build_doctype_table_field_choices(doctype_name)
	return {
		"doctype": doctype_name,
		"fields": _build_doctype_field_choices(doctype_name),
		"table_fields": table_fields,
		"child_fields": _build_child_field_choices(table_fields),
	}


@frappe.whitelist()
def get_sync_partner_table_columns(
	sync_partner_name: str,
	table_name: str | None = None,
	read_query: str | None = None,
) -> dict[str, Any]:
	_require_system_manager()
	partner_doc = _require_doc_permission(SYNC_PARTNER_DOCTYPE, sync_partner_name, permtype="write")
	connector = get_connector_for_partner(partner_doc)
	normalized_table_name = _clean_string(table_name)
	normalized_read_query = _clean_string(read_query)
	try:
		columns = connector.describe_source_columns(source=normalized_table_name, query=normalized_read_query)
	except Exception as exc:
		frappe.throw(str(exc), exc=frappe.ValidationError)
	return {
		"sync_partner": partner_doc.name,
		"table_name": normalized_table_name,
		"read_query": normalized_read_query,
		"columns": columns,
	}


@frappe.whitelist()
def run_sync_now(sync_definition_name: str, trigger: str = TRIGGER_MANUAL, dry_run: bool = False):
	_require_system_manager()
	_require_sync_definition_permission(sync_definition_name, permtype="write", check_partner=True)
	return service_execute_sync_definition(sync_definition_name, trigger=_validate_trigger_type(trigger), dry_run=_as_bool(dry_run))


@frappe.whitelist()
def run_sync_definition(sync_definition_name: str, trigger: str = TRIGGER_MANUAL, queue: bool = True, dry_run: bool = False):
	_require_system_manager()
	_require_sync_definition_permission(sync_definition_name, permtype="write", check_partner=True)
	return service_enqueue_sync_definition(
		sync_definition_name,
		trigger=_validate_trigger_type(trigger),
		queue=_as_bool(queue),
		dry_run=_as_bool(dry_run),
	)


@frappe.whitelist()
def resolve_sync_run_item(sync_run_item_name: str, direction: str) -> dict[str, Any]:
	_require_system_manager()
	item_doc = _require_doc_permission(SYNC_RUN_ITEM_DOCTYPE, sync_run_item_name, permtype="write")
	sync_definition_name = _clean_string(_get_doc_value(item_doc, "sync_definition"))
	if sync_definition_name:
		_require_sync_definition_permission(
			sync_definition_name,
			permtype="write",
			check_partner=True,
			check_target_doctype=True,
		)
	return service_resolve_sync_run_item(sync_run_item_name, direction)


@frappe.whitelist()
def test_sync_partner(sync_partner_name: str) -> dict[str, Any]:
	_require_system_manager()
	partner_doc = _require_doc_permission(SYNC_PARTNER_DOCTYPE, sync_partner_name, permtype="write")
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
def preview_sync_definition(sync_definition_name: str, limit: int = 50) -> dict[str, Any]:
	_require_system_manager()
	sync_definition = _require_sync_definition_permission(
		sync_definition_name,
		permtype="read",
		check_partner=True,
		check_target_doctype=True,
	)
	return SyncPreviewService.predict(sync_definition, limit=_as_int(limit, 50))


@frappe.whitelist()
def export_sync_definition_yaml(sync_definition_name: str) -> str:
	_require_system_manager()
	_require_sync_definition_permission(sync_definition_name, permtype="read", check_partner=True)
	return service_export_sync_definition_yaml(sync_definition_name)


@frappe.whitelist()
def preview_import_sync_definition_yaml(yaml_payload: str, overwrite: bool = False) -> dict[str, Any]:
	_require_system_manager()
	return service_preview_import_sync_definition_yaml(yaml_payload=yaml_payload, overwrite=_as_bool(overwrite))


@frappe.whitelist()
def import_sync_definition_yaml(yaml_payload: str, overwrite: bool = False) -> dict[str, Any]:
	_require_system_manager()
	overwrite = _as_bool(overwrite)
	preview = service_preview_import_sync_definition_yaml(yaml_payload=yaml_payload, overwrite=overwrite)
	if not preview.get("can_import"):
		frappe.throw(preview.get("error") or "YAML payload cannot be imported.", exc=frappe.ValidationError)
	_require_import_permissions(preview, overwrite=overwrite)
	result = service_import_sync_definition_yaml(yaml_payload=yaml_payload, overwrite=overwrite)
	return _normalize_import_result(result, overwrite=overwrite)


@frappe.whitelist()
def run_due_sync_definitions(limit: int = 20, queue: bool = True) -> list[dict[str, Any]]:
	_require_system_manager()
	return service_run_due_sync_definitions(limit=_as_int(limit, 20), queue=_as_bool(queue))


@frappe.whitelist()
def recover_stale_runs(sync_definition_name: str | None = None, timeout_minutes: int | None = None) -> dict[str, Any]:
	_require_system_manager()
	if sync_definition_name:
		_require_sync_definition_permission(sync_definition_name, permtype="write")
	return service_recover_stale_runs(sync_definition_name=sync_definition_name, timeout_minutes=timeout_minutes)


@frappe.whitelist()
def cleanup_sync_run_retention(
	retention_days_success: int | None = None,
	retention_days_error: int | None = None,
) -> dict[str, Any]:
	_require_system_manager()
	return service_cleanup_sync_run_retention(
		retention_days_success=retention_days_success,
		retention_days_error=retention_days_error,
	)


@frappe.whitelist()
def import_sync_yaml_from_json(payload: str | dict[str, Any], overwrite: bool = False) -> dict[str, Any]:
	_require_system_manager()
	body = _parse_json_payload(payload)
	return import_sync_definition_yaml(
		yaml_payload=str(body.get("yaml_payload", "")),
		overwrite=_as_bool(body.get("overwrite", overwrite)),
	)
