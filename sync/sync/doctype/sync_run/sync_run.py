# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class SyncRun(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		conflict_count: DF.Int
		created_count: DF.Int
		deleted_count: DF.Int
		dry_run: DF.Check
		error_count: DF.Int
		error_message: DF.LongText | None
		finished_at: DF.Datetime | None
		job_id: DF.Data | None
		last_sync_at: DF.Datetime | None
		processed_count: DF.Int
		skipped_count: DF.Int
		started_at: DF.Datetime | None
		status: DF.Literal["Queued", "Running", "Success", "Partial Error", "Needs Review", "Error", "Skipped", "Preview"]
		success_count: DF.Int
		summary: DF.SmallText | None
		sync_definition: DF.Link
		sync_partner: DF.Link | None
		sync_type: DF.Literal["Frappe -> Partner", "Frappe <- Partner", "Frappe <-> Partner"]
		trigger_type: DF.Literal["manual", "scheduler", "api"]
		updated_count: DF.Int
	# end: auto-generated types

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
