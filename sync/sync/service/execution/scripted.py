"""UI-owned, read-only planning with runtime-owned atomic writes and durable state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from itertools import batched
from time import monotonic

import frappe
from frappe import _

from sync.sync.service import audit, mapping, matching
from sync.sync.service.execution import sources, writes
from sync.sync.service.execution.script_support import (
	ReadHelpers,
	decode,
	encode,
	execute_read_script,
	fingerprint,
	prepare_source,
	record_key,
)
from sync.sync.service.models import SyncStats

STATE_DOCTYPE = "Sync Record State"
ACTIONS = {"create", "reverse", "replace", "skip", "error"}


def state_name(config, key):
	return fingerprint([config.name, key])


def load_state(config, key):
	values = frappe.db.get_value(STATE_DOCTYPE, state_name(config, key), "*", as_dict=True)
	if not values:
		return {}
	return {
		**dict(values),
		"state": decode(values.state, {}),
		"documents": decode(values.documents, []),
		"source_record": decode(values.source_record, {}),
	}


def state_values(config, key, record, payload, state, documents, revision, run_name=None):
	# Keep only match keys for missing-source audit; payload capture belongs to Sync Run Item.
	source_key = {
		config.mapping[field]["partner_field"]: record.get(config.mapping[field]["partner_field"])
		for field in config.match_fields
	}
	return {
		"sync_definition": config.name,
		"record_key": key,
		"source_fingerprint": fingerprint(record),
		"target_fingerprint": fingerprint(payload),
		"revision": revision,
		"state": encode(state),
		"documents": encode(documents),
		"source_record": encode(source_key),
		"last_run": run_name,
	}


def persist_state(config, key, record, payload, state, documents, revision, run_name=None):
	name = state_name(config, key)
	values = state_values(config, key, record, payload, state, documents, revision, run_name)
	if frappe.db.exists(STATE_DOCTYPE, name):
		frappe.db.set_value(STATE_DOCTYPE, name, values)
	else:
		frappe.get_doc({"doctype": STATE_DOCTYPE, **values}).insert(ignore_permissions=True, set_name=name)


def plan_record(config, record, mapping_context=None, state_cache=None, document_cache=None):
	def get_state(key):
		return (
			state_cache.get(state_name(config, key), {})
			if state_cache is not None
			else load_state(config, key)
		)

	key = record_key(config, record)
	old = get_state(key)
	if (old.get("state") or {}).get("alias_of"):
		raise frappe.ValidationError(
			_("Source group split requires explicit reconciliation; existing group is retained.")
		)
	payload = mapping._map_partner_to_frappe(
		record,
		config.mapping,
		config.value_mapping,
		config.value_mapping_fallbacks,
		doctype=config.doctype,
		partner_time_zone=config.partner_time_zone,
		mapping_context=mapping_context,
	)
	aliases = []
	for alias_record in record.get("_sync", {}).get("aliases", []):
		alias_key = record_key(config, alias_record)
		if alias_key == key:
			continue
		alias = get_state(alias_key)
		if alias and (alias.get("state") or {}).get("alias_of") not in (None, key):
			raise frappe.ValidationError(_("Source alias is already owned by another group."))
		if not alias:
			candidates = sources._load_frappe_match_candidates(
				replace(config, update_existing=False), [alias_record]
			)
			lookup = matching._build_frappe_match_lookup(config, candidates)
			matched = matching._find_existing_frappe_records(
				config, alias_record, lookup.groups, lookup.identity_by_value
			)
			alias = {
				"record_key": alias_key,
				"source_record": alias_record,
				"documents": [r["name"] for r in matched],
				"state": {},
				"revision": 0,
			}
		aliases.append(alias)
	documents = list(old.get("documents") or [])
	for alias in aliases:
		documents.extend(alias.get("documents") or [])
	if not old:
		candidates = sources._load_frappe_match_candidates(replace(config, update_existing=False), [record])
		lookup = matching._build_frappe_match_lookup(config, candidates)
		matched = matching._find_existing_frappe_records(
			config, record, lookup.groups, lookup.identity_by_value
		)
		documents.extend(r["name"] for r in matched)
	documents = list(dict.fromkeys(documents))
	existing = []
	for name in documents:
		doc = (document_cache or {}).get(name)
		if doc is None:
			doc = frappe.get_doc(config.doctype, name).as_dict()
		if payload.get("company") and doc.get("company") != payload["company"]:
			raise frappe.ValidationError(_("Processing plan cannot cross company boundaries."))
		if doc["docstatus"] == 2:
			raise frappe.ValidationError(_("A tracked target document was cancelled outside the sync."))
		if config.doctype == "Journal Entry":
			reversed_outside = (
				doc.get("_sync_reversed")
				if name in (document_cache or {})
				else frappe.db.exists("Journal Entry", {"reversal_of": name, "docstatus": ["!=", 2]})
			)
			if reversed_outside:
				raise frappe.ValidationError(_("A tracked target document was reversed outside the sync."))
		existing.append(dict(doc))
	result = execute_read_script(
		config.record_processing_script,
		{
			"partner_record": deepcopy(record),
			"frappe_payload": deepcopy(payload),
			"existing_documents": existing,
			"state": deepcopy(old.get("state") or {}),
			"parameters": deepcopy(config.script_parameters or {}),
			"helpers": ReadHelpers(),
			"result": None,
		},
	)
	if not isinstance(result, dict) or result.get("action") not in ACTIONS:
		raise frappe.ValidationError(_("Record script must return a valid action plan."))
	action = result["action"]
	if action in {"create", "replace"} and not config.create_new:
		raise frappe.ValidationError(_("Create New is disabled."))
	if action in {"replace", "reverse"}:
		if config.doctype != "Journal Entry" or not config.update_existing or not documents:
			raise frappe.ValidationError(
				_("Reverse and replace require existing Journal Entries and Update Existing.")
			)
		if any(doc["docstatus"] != 1 for doc in existing):
			raise frappe.ValidationError(_("Only submitted Journal Entries can be reversed."))
	if action == "create" and documents:
		raise frappe.ValidationError(_("Create plan cannot duplicate an existing target."))
	if not isinstance(result.get("state", {}), dict):
		raise frappe.ValidationError(_("Processing state must be an object."))
	# Fingerprints and document references are owned by the runtime, never by a script.
	return {
		"key": key,
		"record": record,
		"payload": payload,
		"old": old,
		"aliases": aliases,
		"documents": documents,
		"existing": existing,
		"action": action,
		"state": result.get("state", {}),
		"message": str(result.get("message") or action),
		"submit": bool(result.get("submit", False)),
		"log_unchanged": bool(result.get("log_unchanged", True)),
		"posting_date": result.get("posting_date") or payload.get("posting_date"),
	}


def apply_plan(config, plan, run_name=None):
	if plan["action"] == "error":
		raise frappe.ValidationError(plan["message"])
	with writes._frappe_write_savepoint():
		current = load_state(config, plan["key"])
		if int(current.get("revision") or 0) != int(plan["old"].get("revision") or 0):
			raise frappe.ValidationError(_("Processing state changed; retry the run."))
		for name in plan["documents"]:
			doc = frappe.get_doc(config.doctype, name)
			before = next(d for d in plan["existing"] if d["name"] == name)
			if str(doc.modified) != str(before["modified"]):
				raise frappe.ValidationError(_("Target document changed after planning."))
		revision = int(current.get("revision") or 0) + 1
		documents = list(plan["documents"])
		if plan["action"] in {"replace", "reverse"}:
			for name in documents:
				# Reverse the current revision, never a previous revision sharing the source ID.
				if frappe.db.exists("Journal Entry", {"reversal_of": name, "docstatus": ["!=", 2]}):
					raise frappe.ValidationError(
						_("Tracked Journal Entry already has a reversal outside this transition.")
					)
				writes._reverse_journal_entry(
					source_name=name,
					posting_date=plan["posting_date"],
					submit=True,
					idempotency_key="sync:" + fingerprint([config.name, plan["key"], revision, name]),
				)
			documents = []
		if plan["action"] in {"create", "replace"}:
			name = writes._upsert_frappe_record(
				doctype=config.doctype, existing_name=None, payload=plan["payload"], dry_run=False
			)
			if plan["submit"]:
				frappe.get_doc(config.doctype, name).submit()
			documents = [name]
		# Avoid changing a million state rows on an unchanged daily run.
		if (
			current.get("source_fingerprint") != fingerprint(plan["record"])
			or current.get("state") != plan["state"]
			or current.get("documents") != documents
		):
			persist_state(
				config,
				plan["key"],
				plan["record"],
				plan["payload"],
				plan["state"],
				documents,
				revision,
				run_name,
			)
		for alias in plan["aliases"]:
			if alias.get("documents") or (alias.get("state") or {}).get("alias_of") != plan["key"]:
				persist_state(
					config,
					alias["record_key"],
					alias["source_record"],
					{},
					{"alias_of": plan["key"]},
					[],
					int(alias.get("revision") or 0) + 1,
					run_name,
				)
	return documents


def prepared_batches(config, records):
	"""Fetch reconciliation state and full current documents in bounded batches."""
	meta = frappe.get_meta(config.doctype)
	child_fields = [df for df in meta.fields if df.fieldtype in ("Table", "Table MultiSelect")]
	for batch in batched(records, max(config.batch_size, 100), strict=False):
		keys = [record_key(config, record) for record in batch]
		for record in batch:
			keys.extend(record_key(config, alias) for alias in record.get("_sync", {}).get("aliases", []))
		states = {}
		for row in frappe.get_all(
			STATE_DOCTYPE, filters={"name": ["in", [state_name(config, key) for key in keys]]}, fields=["*"]
		):
			states[row.name] = {
				**dict(row),
				"state": decode(row.state, {}),
				"documents": decode(row.documents, []),
				"source_record": decode(row.source_record, {}),
			}
		names = list({name for state in states.values() for name in state["documents"]})
		documents = {}
		if names:
			for row in frappe.get_all(config.doctype, filters={"name": ["in", names]}, fields=["*"]):
				documents[row.name] = {**dict(row), "doctype": config.doctype}
			if config.doctype == "Journal Entry":
				for reversal in frappe.get_all(
					"Journal Entry",
					filters={"reversal_of": ["in", names], "docstatus": ["!=", 2]},
					fields=["reversal_of"],
				):
					if reversal.reversal_of in documents:
						documents[reversal.reversal_of]["_sync_reversed"] = True
			for df in child_fields:
				for doc in documents.values():
					doc[df.fieldname] = []
				for row in frappe.get_all(
					df.options,
					filters={
						"parent": ["in", names],
						"parenttype": config.doctype,
						"parentfield": df.fieldname,
					},
					fields=["*"],
					order_by="idx asc",
				):
					if row.parent in documents:
						documents[row.parent][df.fieldname].append({**dict(row), "doctype": df.options})
		yield batch, states, documents


def unchanged_plan(plan):
	old = plan["old"]
	return (
		plan["action"] == "skip"
		and old.get("source_fingerprint") == fingerprint(plan["record"])
		and old.get("state") == plan["state"]
		and old.get("documents") == plan["documents"]
		and all(
			not a.get("documents") and a.get("state", {}).get("alias_of") == plan["key"]
			for a in plan["aliases"]
		)
	)


def iter_states(config):
	cursor = ""
	while True:
		rows = frappe.get_all(
			STATE_DOCTYPE,
			filters={"sync_definition": config.name, "name": [">", cursor]},
			fields=["name", "record_key", "state", "source_record"],
			order_by="name asc",
			limit=1000,
		)
		if not rows:
			return
		yield from rows
		cursor = rows[-1].name


def preview_priority(item):
	result = item["result"]
	return (
		3
		if result["action"] == "error"
		else 2
		if result.get("warnings")
		else 1
		if result["action"] != "skipped"
		else 0
	)


def add_preview(preview, item, limit):
	if len(preview) < limit:
		preview.append(item)
	elif preview:
		index = min(range(len(preview)), key=lambda i: preview_priority(preview[i]))
		if preview_priority(item) > preview_priority(preview[index]):
			preview[index] = item


def run_scripted(config, connector, context, run_doc=None, preview_limit=None):
	stats = SyncStats()
	preview = []
	warning_count = 0
	seen = set()
	last_activity = monotonic()
	mapping_context = mapping._build_runtime_mapping_context(config)
	with prepare_source(config, connector) as records:
		for batch, states, documents in prepared_batches(config, records):
			for record in batch:
				key = record_key(config, record)
				seen.add(key)
				plan = None
				try:
					plan = plan_record(config, record, mapping_context, states, documents)
					seen.update(a["record_key"] for a in plan["aliases"])
					if plan["action"] == "error":
						raise frappe.ValidationError(plan["message"])
					if not context.dry_run and not unchanged_plan(plan):
						plan["result_documents"] = apply_plan(config, plan, getattr(run_doc, "name", None))
					action = {
						"create": "created",
						"replace": "updated",
						"reverse": "updated",
						"skip": "skipped",
					}[plan["action"]]
					status = "skipped" if action == "skipped" else "success"
					message = plan["message"]
				except Exception as exc:
					action, status, message = "error", "error", str(exc)
				warnings = record.get("_sync", {}).get("warnings", [])
				warning_count += bool(warnings)
				if warnings:
					message += " | " + "; ".join(str(w) for w in warnings)
				if preview_limit is not None:
					stats.register(action, status)
					add_preview(
						preview,
						{
							"direction": config.sync_type,
							"result": {
								"record_key": key,
								"action": action,
								"message": message,
								"warnings": warnings,
								"documents": plan["documents"] if plan else [],
								"payload": plan["payload"] if plan else None,
							},
						},
						preview_limit,
					)
				elif (
					action == "skipped"
					and plan
					and not plan["log_unchanged"]
					and not warnings
					and unchanged_plan(plan)
				):
					stats.register(action, status)
				else:
					audit._register_and_log(
						stats=stats,
						run_doc=run_doc,
						config=config,
						action=action,
						status=status,
						message=message,
						direction=config.sync_type,
						frappe_record={
							**plan["payload"],
							"name": next(iter(plan.get("result_documents", plan["documents"])), None),
						}
						if plan
						else None,
						partner_record=record,
						changes=[("processing_state", plan["old"].get("state"), plan["state"])]
						if plan and action in {"created", "updated"}
						else [],
						commit=False,
					)
			if run_doc and monotonic() - last_activity >= 30:
				audit._track_pending_run_writes(run_doc, 1)
				audit._flush_pending_run_writes(run_doc, force=True)
				last_activity = monotonic()

	# A missing source is never an implicit reversal, even after a complete read.
	for state in iter_states(config):
		if state.record_key in seen or decode(state.state, {}).get("alias_of"):
			continue
		message = _("Previously processed source group is missing; target retained.")
		if preview_limit is not None:
			stats.register("error", "error")
			add_preview(
				preview,
				{
					"direction": config.sync_type,
					"result": {
						"record_key": state.record_key,
						"action": "error",
						"message": message,
					},
				},
				preview_limit,
			)
		else:
			audit._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="error",
				status="error",
				message=message,
				direction=config.sync_type,
				frappe_record=None,
				partner_record=decode(state.source_record, {}),
				commit=False,
			)
	if run_doc:
		audit._flush_pending_run_writes(run_doc, force=True)
	return {
		**stats.as_dict(),
		"warning_count": warning_count,
		"actions": sorted(preview, key=preview_priority, reverse=True),
		"delta_since": None,
	}
