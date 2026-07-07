import frappe

from sync.sync.constants import (
	FRAPPE_WRITE_ACTION_NONE,
	FRAPPE_WRITE_ACTION_SUBMIT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
	FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION,
	SYNC_DEFINITION,
	SYNC_FRAPPE_WRITE_HOOK,
)


def execute():
	if not frappe.db.table_exists(SYNC_DEFINITION):
		return

	has_insert_action = frappe.db.has_column(SYNC_DEFINITION, "frappe_after_insert_action")
	has_update_action = frappe.db.has_column(SYNC_DEFINITION, "frappe_after_update_action")
	if not has_insert_action and not has_update_action:
		return

	fields = ["name"]
	if has_insert_action:
		fields.append("frappe_after_insert_action")
	if has_update_action:
		fields.append("frappe_after_update_action")

	for row in frappe.get_all(SYNC_DEFINITION, fields=fields):
		name = row.get("name")
		if not name:
			continue
		_migrate_action(
			parent=name,
			event=FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
			action=row.get("frappe_after_insert_action") if has_insert_action else None,
		)
		_migrate_action(
			parent=name,
			event=FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
			action=row.get("frappe_after_update_action") if has_update_action else None,
		)
		_clear_legacy_actions(
			parent=name,
			has_insert_action=has_insert_action,
			has_update_action=has_update_action,
		)


def _clear_legacy_actions(*, parent: str, has_insert_action: bool, has_update_action: bool) -> None:
	values = {}
	if has_insert_action:
		values["frappe_after_insert_action"] = FRAPPE_WRITE_ACTION_NONE
	if has_update_action:
		values["frappe_after_update_action"] = FRAPPE_WRITE_ACTION_NONE
	if values:
		frappe.db.set_value(SYNC_DEFINITION, parent, values, update_modified=False)


def _migrate_action(*, parent: str, event: str, action: str | None) -> None:
	if action != FRAPPE_WRITE_ACTION_SUBMIT:
		return
	if frappe.db.exists(
		SYNC_FRAPPE_WRITE_HOOK,
		{
			"parent": parent,
			"parenttype": SYNC_DEFINITION,
			"parentfield": "frappe_write_hooks",
			"event": event,
			"hook_type": FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION,
			"action": FRAPPE_WRITE_ACTION_SUBMIT,
		},
	):
		return
	idx = (frappe.db.count(SYNC_FRAPPE_WRITE_HOOK, {"parent": parent, "parenttype": SYNC_DEFINITION}) or 0) + 1
	doc = frappe.get_doc(
		{
			"doctype": SYNC_FRAPPE_WRITE_HOOK,
			"parent": parent,
			"parenttype": SYNC_DEFINITION,
			"parentfield": "frappe_write_hooks",
			"idx": idx,
			"enabled": 1,
			"event": event,
			"hook_type": FRAPPE_WRITE_HOOK_TYPE_BUILTIN_ACTION,
			"action": FRAPPE_WRITE_ACTION_SUBMIT,
		}
	)
	doc.insert(ignore_permissions=True)
