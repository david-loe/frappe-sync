# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class SyncRunItem(Document):
	def on_trash(self):
		for change_name in _linked_names("Sync Run Item Change", {"sync_run_item": self.name}):
			frappe.delete_doc("Sync Run Item Change", change_name, ignore_permissions=True)


def _linked_names(doctype: str, filters: dict) -> list[str]:
	rows = frappe.get_all(doctype, filters=filters, fields=["name"], order_by=None)
	return [row.name if hasattr(row, "name") else row.get("name") for row in rows]
