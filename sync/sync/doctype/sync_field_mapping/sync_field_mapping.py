# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SyncFieldMapping(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		direction: DF.Literal["Frappe <-> Partner", "Frappe -> Partner", "Frappe <- Partner"]
		fallback_value: DF.Data | None
		frappe_field: DF.Literal[None]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		partner_field: DF.Literal[None]
		unmapped_action: DF.Literal["Keep Original", "Use Fallback Value", "Use NULL"]
	# end: auto-generated types

	pass

