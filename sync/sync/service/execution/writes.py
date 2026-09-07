from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import frappe
from frappe.utils import cint

from sync.sync.constants import (
	FRAPPE_WRITE_ACTION_NONE,
	FRAPPE_WRITE_ACTION_SUBMIT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
	FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION,
	FRAPPE_WRITE_HOOK_TYPE_CUSTOM_SCRIPT,
	MAPPING_DIRECTION_FRAPPE_TO_PARTNER,
)
from sync.sync.service import audit as audit_service
from sync.sync.service import config_access as config_access_service
from sync.sync.service import mapping as mapping_service
from sync.sync.service import mapping_rules as mapping_rules_service
from sync.sync.service import matching as matching_service
from sync.sync.service import metadata as metadata_service
from sync.sync.service import time_utils as time_utils_service
from sync.sync.service import values as values_service
from sync.sync.service.connectors import ConnectorCreateOptions
from sync.sync.service.models import (
	AUDIT_RECORD_UNSET,
	SYNC_TYPE_FRAPPE_TO_PARTNER,
	SYNC_TYPE_PARTNER_TO_FRAPPE,
	SYSTEM_KEYS,
	FrappeWriteHookResult,
	RuntimeMappingContext,
	SyncDefinitionConfig,
	SyncFrappeWriteHookConfig,
	SyncStats,
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
	if not config_access_service._update_existing_enabled(config):
		audit_service._log_update_existing_disabled(
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
	key = matching_service._key_tuple_from_frappe(
		frappe_record, config_access_service._config_match_fields(config)
	)
	mapping_context = mapping_context or mapping_service._build_runtime_mapping_context(config)
	partner_payload = mapping_service._with_partner_timestamps(
		config,
		frappe_record,
		mapping_service._apply_partner_link_fields(config, frappe_record, partner_payload),
		create=False,
		mapping_context=mapping_context,
	)
	connector_mapping = (
		mapping_context.connector_mapping
		if mapping_context is not None
		else mapping_rules_service._flatten_mapping_for_direction(
			config.mapping, MAPPING_DIRECTION_FRAPPE_TO_PARTNER
		)
	)
	try:
		write = connector.upsert_record(
			record=partner_payload,
			key_values=matching_service._partner_key_values_for_existing_match(
				config, frappe_record, key, partner_record
			),
			mapping=connector_mapping,
			dry_run=dry_run,
			source=config.table_name,
			create_options=_build_partner_create_options(config),
		)
		if not write.ok:
			raise RuntimeError(write.message or "Partner upsert failed.")
		_persist_frappe_partner_identity(config, frappe_record, write, dry_run=dry_run)
		audit_service._register_and_log(
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
		audit_service._register_and_log(
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
	if not config_access_service._update_existing_enabled(config):
		audit_service._log_update_existing_disabled(
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
		frappe_partner_field = config_access_service._config_frappe_partner_identity_field(config)
		partner_identity_field = config_access_service._config_partner_identity_field(config)
		if frappe_partner_field and partner_identity_field:
			partner_id = partner_record.get(partner_identity_field)
			if partner_id not in (None, ""):
				frappe_payload = dict(frappe_payload)
				frappe_payload[frappe_partner_field] = partner_id
		frappe_payload = mapping_service._with_frappe_modified_timestamp(
			config,
			partner_record,
			frappe_payload,
			mapping_context=mapping_service._build_runtime_mapping_context(config),
		)
		doc_name = _upsert_frappe_record(
			doctype=config.doctype,
			existing_name=(frappe_record or {}).get("name"),
			payload=frappe_payload,
			dry_run=dry_run,
			**_frappe_write_hook_kwargs(
				config=config,
				run_doc=run_doc,
				event=FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				frappe_before_record=frappe_record,
				changes=changes,
				dry_run=dry_run,
			),
		)
		if doc_name:
			frappe_payload["name"] = doc_name
		audit_service._register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action=action,
			status=status,
			message=_append_hook_message(
				"Dry run update." if dry_run else message,
				planned=_planned_frappe_write_hook_message(config, FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE)
				if dry_run
				else None,
			),
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
		audit_service._register_and_log(
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


def _upsert_frappe_record(
	*,
	doctype: str,
	existing_name: str | None,
	payload: dict[str, Any],
	dry_run: bool,
	write_hooks: tuple[SyncFrappeWriteHookConfig, ...] | None = None,
	hook_context: dict[str, Any] | None = None,
	after_insert_action: str | None = None,
	after_update_action: str | None = None,
) -> str | None:
	if dry_run:
		return existing_name
	mapped_modified = payload["modified"] if "modified" in payload else AUDIT_RECORD_UNSET
	doctype_fieldnames = metadata_service._doctype_fieldnames(doctype)
	write_hooks = config_access_service._normalize_frappe_write_hooks(
		write_hooks,
		legacy_after_insert_action=after_insert_action,
		legacy_after_update_action=after_update_action,
	)
	event = FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE if existing_name else FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT
	event_hooks = _enabled_frappe_write_hooks(write_hooks, event)
	with _frappe_write_savepoint():
		if existing_name:
			doc = frappe.get_doc(doctype, existing_name)
			for key, value in payload.items():
				if key in SYSTEM_KEYS:
					continue
				if metadata_service._doctype_payload_allows_field(doctype, key, doctype_fieldnames):
					_set_frappe_doc_payload_value(doc, doctype, key, value, merge_child_rows=True)
			doc.save(ignore_permissions=True)
			_execute_frappe_write_hooks(
				event=event,
				hooks=event_hooks,
				doc=doc,
				context={**(hook_context or {}), "doc": doc, "docname": doc.name, "frappe_payload": payload},
			)
			_set_mapped_frappe_modified(doctype, doc.name, mapped_modified)
			return doc.name

		doc = frappe.new_doc(doctype)
		for key, value in payload.items():
			if key in SYSTEM_KEYS:
				continue
			if metadata_service._doctype_payload_allows_field(doctype, key, doctype_fieldnames):
				_set_frappe_doc_payload_value(doc, doctype, key, value, merge_child_rows=False)
		doc.insert(ignore_permissions=True)
		_execute_frappe_write_hooks(
			event=event,
			hooks=event_hooks,
			doc=doc,
			context={**(hook_context or {}), "doc": doc, "docname": doc.name, "frappe_payload": payload},
		)
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


def _enabled_frappe_write_hooks(
	hooks: tuple[SyncFrappeWriteHookConfig, ...] | None,
	event: str,
) -> tuple[SyncFrappeWriteHookConfig, ...]:
	return tuple(hook for hook in hooks or () if hook.enabled and hook.event == event)


def _execute_frappe_write_hooks(
	*,
	event: str,
	hooks: tuple[SyncFrappeWriteHookConfig, ...],
	doc: Any,
	context: dict[str, Any],
) -> FrappeWriteHookResult:
	changed = False
	messages: list[str] = []
	for hook in hooks:
		if hook.hook_type == FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION:
			_apply_frappe_write_action(doc, hook.action or FRAPPE_WRITE_ACTION_NONE)
			messages.append(_frappe_write_hook_label(hook))
			continue
		if hook.hook_type == FRAPPE_WRITE_HOOK_TYPE_CUSTOM_SCRIPT:
			result = _execute_frappe_write_script_hook(event=event, hook=hook, doc=doc, context=context)
			changed = changed or result.changed
			messages.extend(result.messages)
	return FrappeWriteHookResult(changed=changed, messages=tuple(messages))


def _execute_frappe_write_script_hook(
	*,
	event: str,
	hook: SyncFrappeWriteHookConfig,
	doc: Any,
	context: dict[str, Any],
) -> FrappeWriteHookResult:
	from frappe.utils.safe_exec import safe_exec

	helper_messages: list[str] = []
	helpers = _FrappeWriteHookHelpers(helper_messages)
	script_context = {
		"event": event,
		"sync_definition": context.get("sync_definition"),
		"sync_run": context.get("sync_run"),
		"doctype": context.get("doctype") or getattr(doc, "doctype", None),
		"docname": context.get("docname") or getattr(doc, "name", None),
		"doc": doc,
		"partner_record": context.get("partner_record"),
		"frappe_payload": context.get("frappe_payload"),
		"frappe_before_record": context.get("frappe_before_record"),
		"changes": context.get("changes"),
		"dry_run": bool(context.get("dry_run")),
		"helpers": helpers,
		"result": None,
	}
	_globals, locals_ = safe_exec(
		hook.script or "",
		_globals=script_context,
		_locals=script_context,
		restrict_commit_rollback=True,
		script_filename=f"sync_frappe_write_hook_{hook.idx}",
	)
	result = (locals_ or {}).get("result") or (_globals or {}).get("result")
	changed = False
	message = None
	if isinstance(result, dict):
		changed = values_service._as_bool(result.get("changed"))
		message = values_service._clean_string(result.get("message"))
	elif result is not None and hasattr(result, "get"):
		changed = values_service._as_bool(result.get("changed"))
		message = values_service._clean_string(result.get("message"))
	messages = list(helper_messages)
	if message:
		messages.append(message)
	if not messages:
		messages.append(_frappe_write_hook_label(hook))
	return FrappeWriteHookResult(changed=changed, messages=tuple(messages))


class _FrappeWriteHookHelpers:
	def __init__(self, messages: list[str]):
		self._messages = messages

	def db_exists(self, doctype: str, name: str) -> Any:
		return frappe.db.exists(doctype, name)

	def db_get_value(self, doctype: str, filters: Any, fieldname: str) -> Any:
		return frappe.db.get_value(doctype, filters, fieldname)

	def get_doc(self, doctype: str, name: str) -> Any:
		return frappe.get_doc(doctype, name)

	def log(self, message: Any) -> None:
		text = values_service._clean_string(message)
		if text:
			self._messages.append(text)

	def reverse_journal_entry(
		self,
		source_name: str,
		posting_date: Any = None,
		submit: bool = True,
		idempotency_key: str | None = None,
	) -> str | None:
		return _reverse_journal_entry(
			source_name=source_name,
			posting_date=posting_date,
			submit=submit,
			idempotency_key=idempotency_key,
		)


def _reverse_journal_entry(
	*,
	source_name: str,
	posting_date: Any = None,
	submit: bool = True,
	idempotency_key: str | None = None,
) -> str | None:
	if not source_name:
		raise frappe.ValidationError("source_name is required.")
	existing_filters = {"reversal_of": source_name, "docstatus": ["!=", 2]}
	if idempotency_key and frappe.get_meta("Journal Entry").has_field("sync_idempotency_key"):
		existing_filters["sync_idempotency_key"] = idempotency_key
	existing = frappe.db.exists("Journal Entry", existing_filters) or frappe.db.exists(
		"Journal Entry",
		{"reversal_of": source_name, "docstatus": 1},
	)
	if existing:
		return str(existing)

	from erpnext.accounts.doctype.journal_entry.journal_entry import make_reverse_journal_entry

	doc = make_reverse_journal_entry(source_name)
	if posting_date is not None:
		doc.posting_date = posting_date
	if idempotency_key and frappe.get_meta("Journal Entry").has_field("sync_idempotency_key"):
		doc.sync_idempotency_key = idempotency_key
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc.name


def _frappe_write_hook_label(hook: SyncFrappeWriteHookConfig) -> str:
	if hook.hook_type == FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION:
		return f"{hook.event}: {hook.action}"
	return f"{hook.event}: Custom Script"


def _planned_frappe_write_hook_message(config: SyncDefinitionConfig, event: str) -> str | None:
	labels = [
		_frappe_write_hook_label(hook)
		for hook in _enabled_frappe_write_hooks(
			config_access_service._config_frappe_write_hooks(config), event
		)
	]
	if not labels:
		return None
	return "Dry run: Frappe write hooks would run: " + "; ".join(labels)


def _append_hook_message(
	message: str, hook_result: FrappeWriteHookResult | None = None, planned: str | None = None
) -> str:
	parts = [message]
	if hook_result and hook_result.messages:
		parts.append("Hooks: " + "; ".join(hook_result.messages))
	if planned:
		parts.append(planned)
	return " ".join(part for part in parts if part)


def _frappe_write_hook_context(
	*,
	config: SyncDefinitionConfig,
	run_doc: Any,
	event: str,
	partner_record: dict[str, Any] | None,
	frappe_payload: dict[str, Any] | None,
	frappe_before_record: dict[str, Any] | None,
	changes: list[tuple[str, Any, Any]] | None,
	dry_run: bool,
) -> dict[str, Any]:
	return {
		"event": event,
		"sync_definition": config,
		"sync_run": run_doc,
		"doctype": config.doctype,
		"partner_record": partner_record,
		"frappe_payload": frappe_payload,
		"frappe_before_record": frappe_before_record,
		"changes": changes,
		"dry_run": dry_run,
	}


def _frappe_write_hook_kwargs(
	*,
	config: SyncDefinitionConfig,
	run_doc: Any,
	event: str,
	partner_record: dict[str, Any] | None,
	frappe_payload: dict[str, Any] | None,
	frappe_before_record: dict[str, Any] | None,
	changes: list[tuple[str, Any, Any]] | None,
	dry_run: bool,
) -> dict[str, Any]:
	hooks = config_access_service._config_frappe_write_hooks(config)
	if not _enabled_frappe_write_hooks(hooks, event):
		return {}
	return {
		"write_hooks": hooks,
		"hook_context": _frappe_write_hook_context(
			config=config,
			run_doc=run_doc,
			event=event,
			partner_record=partner_record,
			frappe_payload=frappe_payload,
			frappe_before_record=frappe_before_record,
			changes=changes,
			dry_run=dry_run,
		),
	}


def _run_after_match_frappe_write_hooks(
	*,
	config: SyncDefinitionConfig,
	run_doc: Any,
	partner_record: dict[str, Any],
	frappe_record: dict[str, Any],
	frappe_payload: dict[str, Any] | None,
	changes: list[tuple[str, Any, Any]] | None,
	dry_run: bool,
) -> FrappeWriteHookResult:
	hooks = _enabled_frappe_write_hooks(
		config_access_service._config_frappe_write_hooks(config), FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH
	)
	if not hooks:
		return FrappeWriteHookResult()
	if dry_run:
		return FrappeWriteHookResult(messages=tuple(_frappe_write_hook_label(hook) for hook in hooks))
	docname = frappe_record.get("name")
	if not docname:
		raise frappe.ValidationError("After Match hook requires a matched Frappe document name.")
	doc = frappe.get_doc(config.doctype, docname)
	with _frappe_write_savepoint(enabled=bool(hooks)):
		return _execute_frappe_write_hooks(
			event=FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH,
			hooks=hooks,
			doc=doc,
			context={
				**_frappe_write_hook_context(
					config=config,
					run_doc=run_doc,
					event=FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH,
					partner_record=partner_record,
					frappe_payload=frappe_payload,
					frappe_before_record=frappe_record,
					changes=changes,
					dry_run=dry_run,
				),
				"doc": doc,
				"docname": doc.name,
			},
		)


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
	table_fields = metadata_service._doctype_table_fields(doctype)
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


def _set_mapped_frappe_modified(doctype: str, name: str | None, value: Any) -> None:
	if value is AUDIT_RECORD_UNSET or not name:
		return
	modified = _normalize_mapped_frappe_modified(value)
	frappe.db.set_value(doctype, name, "modified", modified, update_modified=False)


def _normalize_mapped_frappe_modified(value: Any) -> Any:
	parsed = time_utils_service._parse_datetime(value, target_time_zone=time_utils_service._site_time_zone())
	return parsed if parsed is not None else value


def _build_partner_create_options(config: SyncDefinitionConfig) -> ConnectorCreateOptions:
	return ConnectorCreateOptions(
		identity_field=config_access_service._config_partner_identity_field(config),
		strategy=config_access_service._config_partner_create_strategy(config),
		source=getattr(config, "partner_create_id_source", None),
		scope_where=getattr(config, "partner_create_id_scope_where", None),
	)


def _persist_frappe_partner_identity(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	write_result: Any,
	*,
	dry_run: bool,
) -> None:
	frappe_partner_field = config_access_service._config_frappe_partner_identity_field(config)
	partner_identity_field = config_access_service._config_partner_identity_field(config)
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
	if metadata_service._doctype_has_field(config.doctype, frappe_partner_field):
		doc.set(frappe_partner_field, partner_id)
		doc.save(ignore_permissions=True)
		frappe_record[frappe_partner_field] = partner_id
