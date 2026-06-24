# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SyncRunItem(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		action: DF.Literal["created", "updated", "deleted", "skipped", "conflict", "error"]
		change_count: DF.Int
		changed_fields: DF.SmallText | None
		document_name: DF.Data | None
		frappe_before_payload: DF.LongText | None
		message: DF.SmallText | None
		partner_before_payload: DF.LongText | None
		record_key: DF.Data
		source_id: DF.Data | None
		status: DF.Literal["success", "skipped", "conflict", "error"]
		sync_definition: DF.Link | None
		sync_run: DF.Link
		target_id: DF.Data | None
		write_direction: DF.Literal["", "Frappe -> Partner", "Frappe <- Partner"]
		written_after_payload: DF.LongText | None
	# end: auto-generated types

	pass
