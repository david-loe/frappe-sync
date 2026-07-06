# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SyncFrappeWriteHook(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		action: DF.Literal["", "Submit"]
		description: DF.SmallText | None
		enabled: DF.Check
		event: DF.Literal["After Match", "After Insert", "After Update"]
		hook_type: DF.Literal["Built-in Action", "Custom Script"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		script: DF.Code | None
	# end: auto-generated types

	pass
