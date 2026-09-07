# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from sync.sync.service import time_utils


class SyncPartner(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Data | None
		api_secret: DF.Password | None
		auth_type: DF.Literal["Password", "API Key", "Certificate", "None"]
		certificate_path: DF.Data | None
		charset: DF.Data | None
		connection_notes: DF.SmallText | None
		connection_options: DF.Code | None
		database_name: DF.Data
		enabled: DF.Check
		host: DF.Data
		last_checked_on: DF.Datetime | None
		last_connection_error: DF.SmallText | None
		last_connection_status: DF.Literal["", "Unknown", "Success", "Error"]
		partner_name: DF.Data
		partner_type: DF.Link
		password: DF.Password | None
		port: DF.Int
		secret_fields: DF.SmallText | None
		time_zone: DF.Data | None
		trust_server_certificate: DF.Check
		username: DF.Data | None
	# end: auto-generated types

	def validate(self):
		self.time_zone = _normalize_time_zone(getattr(self, "time_zone", None))


def _normalize_time_zone(value: str | None) -> str | None:
	return time_utils._normalize_time_zone_name(value)
