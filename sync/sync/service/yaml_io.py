from __future__ import annotations

from typing import Any

import frappe
import yaml
from frappe.utils import now_datetime

from sync.sync.constants import (
	FRAPPE_WRITE_ACTION_NONE,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
	FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION,
	MATCH_MODE_MATCH_FIELDS,
	SYNC_DEFINITION,
	SYNC_FRAPPE_WRITE_HOOK,
	SYNC_PARTNER,
	SYNC_PARTNER_TYPE,
)
from sync.sync.service import config_access as config_access_service
from sync.sync.service import configuration, definition_rules, time_utils
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	SYNC_DEFINITION_RUNTIME_STATE_FIELDS,
	SYSTEM_KEYS,
)


def export_sync_definition_yaml(sync_definition_name: str) -> str:
	sync_definition_doc = frappe.get_doc(SYNC_DEFINITION, sync_definition_name)
	mask_credentials = values_service._as_bool(
		values_service._first_value(sync_definition_doc, ["export_mask_credentials"], default=1)
	)
	config_doc = _sanitize_document_dict(sync_definition_doc.as_dict(), mask_credentials=mask_credentials)
	config_doc["match_mode"] = values_service._first_value(
		sync_definition_doc, ["match_mode"], default=MATCH_MODE_MATCH_FIELDS
	)
	partner_name = values_service._first_value(sync_definition_doc, ["partner"])

	payload: dict[str, Any] = {
		"version": 2,
		"exported_at": now_datetime().isoformat(),
		"sync_definition": config_doc,
	}
	if partner_name:
		partner_doc = frappe.get_doc(SYNC_PARTNER, partner_name)
		payload["sync_partner"] = _sanitize_document_dict(
			partner_doc.as_dict(), mask_credentials=mask_credentials
		)
		partner_type_name = values_service._first_value(partner_doc, ["partner_type"])
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
			"overwrite": values_service._as_bool(overwrite),
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
			"overwrite": values_service._as_bool(overwrite),
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
			"overwrite": values_service._as_bool(overwrite),
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

	overwrite = values_service._as_bool(overwrite)
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
				"name": values_service._first_value_dict(normalized, ["name"]),
				"status": "invalid",
				"exists": False,
				"action": "skip",
				"hint": "Payload section `sync_definition` is missing required field `match_mode`.",
			}
			continue
		name = values_service._first_value_dict(normalized, ["name"])
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

	_validate_import_documents(data, documents, summary, overwrite=overwrite)

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
			result[key] = (
				"***" if mask_credentials and key in secret_fields and value not in (None, "") else value
			)
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
	if doctype == SYNC_DEFINITION:
		payload = _sync_definition_payload_with_legacy_hooks(payload)
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


def _sync_definition_payload_with_legacy_hooks(payload: dict[str, Any]) -> dict[str, Any]:
	if isinstance(payload.get("frappe_write_hooks"), list) and payload.get("frappe_write_hooks"):
		return payload
	legacy_rows = _legacy_frappe_write_action_hook_rows(
		payload.get("frappe_after_insert_action"),
		payload.get("frappe_after_update_action"),
	)
	if not legacy_rows:
		return payload
	result = dict(payload)
	result["frappe_write_hooks"] = legacy_rows
	return result


def _legacy_frappe_write_action_hook_rows(
	after_insert_action: Any,
	after_update_action: Any,
) -> list[dict[str, Any]]:
	rows: list[dict[str, Any]] = []
	for idx, (event, action_value) in enumerate(
		(
			(FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT, after_insert_action),
			(FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE, after_update_action),
		),
		start=1,
	):
		action = config_access_service._normalize_frappe_write_action(action_value)
		if action == FRAPPE_WRITE_ACTION_NONE:
			continue
		rows.append(
			{
				"doctype": SYNC_FRAPPE_WRITE_HOOK,
				"enabled": 1,
				"event": event,
				"hook_type": FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION,
				"action": action,
				"idx": idx,
			}
		)
	return rows


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


def _effective_import_document(doctype, payload, *, overwrite):
	normalized = _normalize_doc_payload(doctype, payload)
	name = normalized.get("name")
	if name and frappe.db.exists(doctype, name):
		existing = dict(frappe.get_doc(doctype, name).as_dict())
		if overwrite:
			existing.update(normalized)
		return existing
	return normalized


def _validate_import_documents(data, documents, summary, *, overwrite):
	"""Validate the effective configuration before any partner or definition is saved."""
	effective = {}
	for doctype, entry in documents.items():
		if entry["status"] not in {"create", "update", "conflict"}:
			continue
		effective[doctype] = _effective_import_document(
			doctype, data[entry["payload_key"]], overwrite=overwrite
		)
	for doctype in (SYNC_PARTNER, SYNC_DEFINITION):
		if doctype not in effective:
			continue
		try:
			if doctype == SYNC_PARTNER:
				time_utils._normalize_time_zone_name(effective[doctype].get("time_zone"))
			else:
				doc = configuration.definition_input(effective[doctype])
				configuration.normalize_definition_document(doc)
				if documents[doctype]["action"] != "keep_existing":
					definition_rules.validate_script_permissions(doc)
				partner = effective.get(SYNC_PARTNER)
				if not partner or partner.get("name") != doc.partner:
					if not frappe.db.exists(SYNC_PARTNER, doc.partner):
						raise frappe.ValidationError("Sync Definition is missing Sync Partner reference.")
					partner = frappe.get_doc(SYNC_PARTNER, doc.partner).as_dict()
				time_utils._normalize_time_zone_name(partner.get("time_zone"))
				partner_type = partner.get("partner_type")
				provided_type = effective.get(SYNC_PARTNER_TYPE, {}).get("name")
				if (
					partner_type
					and partner_type != provided_type
					and not frappe.db.exists(SYNC_PARTNER_TYPE, partner_type)
				):
					raise frappe.ValidationError("Sync Partner Type does not exist.")
		except frappe.ValidationError as exc:
			entry = documents[doctype]
			summary[entry["status"]] -= 1
			summary["invalid"] += 1
			entry.update(status="invalid", action="skip", hint=str(exc))
