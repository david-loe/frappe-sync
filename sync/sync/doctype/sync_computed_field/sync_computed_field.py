# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SyncComputedField(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		field_name: DF.Data
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		required_source_fields: DF.SmallText | None
		template: DF.Code
	# end: auto-generated types

	pass
