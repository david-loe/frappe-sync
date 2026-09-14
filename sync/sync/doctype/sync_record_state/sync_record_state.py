from frappe.model.document import Document


class SyncRecordState(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		documents: DF.JSON | None
		last_run: DF.Data | None
		record_key: DF.SmallText | None
		revision: DF.Int
		source_fingerprint: DF.Data | None
		source_record: DF.JSON | None
		state: DF.JSON | None
		sync_definition: DF.Link
		target_fingerprint: DF.Data | None
	# end: auto-generated types

	pass
