# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class SyncRun(Document):
	def on_trash(self):
		for item_name in _linked_names("Sync Run Item", {"sync_run": self.name}):
			frappe.delete_doc("Sync Run Item", item_name, ignore_permissions=True)

		for definition_name in _linked_names("Sync Definition", {"last_run": self.name}):
			frappe.db.set_value(
				"Sync Definition",
				definition_name,
				"last_run",
				None,
				update_modified=False,
			)


def _linked_names(doctype: str, filters: dict) -> list[str]:
	rows = frappe.get_all(doctype, filters=filters, fields=["name"], order_by=None)
	return [row.name if hasattr(row, "name") else row.get("name") for row in rows]
