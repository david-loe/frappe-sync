# Copyright (c) 2026, david-loe and contributors
# For license information, please see license.txt

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe.model.document import Document


class SyncPartner(Document):
	def validate(self):
		self.time_zone = _normalize_time_zone(getattr(self, "time_zone", None))


def _normalize_time_zone(value: str | None) -> str | None:
	if value in (None, ""):
		return None
	cleaned = str(value).strip()
	if not cleaned:
		return None
	try:
		ZoneInfo(cleaned)
	except ZoneInfoNotFoundError:
		frappe.throw("Time Zone must be a valid IANA zone such as Europe/Berlin.")
	return cleaned
