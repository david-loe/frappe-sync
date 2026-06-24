# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SyncSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		run_retention_days_error: DF.Int
		run_retention_days_success: DF.Int
		stale_run_timeout_minutes: DF.Int
	# end: auto-generated types

	pass
