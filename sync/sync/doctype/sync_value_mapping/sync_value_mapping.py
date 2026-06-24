# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SyncValueMapping(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		frappe_field: DF.Literal[None]
		frappe_value: DF.Data | None
		frappe_value_is_null: DF.Check
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		partner_value: DF.Data | None
		partner_value_is_null: DF.Check
	# end: auto-generated types

	pass

