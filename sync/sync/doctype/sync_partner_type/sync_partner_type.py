# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SyncPartnerType(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		connection_notes: DF.SmallText | None
		db_api_module: DF.Data | None
		default_port: DF.Int
		description: DF.SmallText | None
		label: DF.Data
		partner_type_code: DF.Data
		secret_fields: DF.SmallText | None
		supports_query: DF.Check
		supports_table: DF.Check
	# end: auto-generated types

	pass

