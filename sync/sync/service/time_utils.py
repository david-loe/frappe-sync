from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe.utils import get_datetime, get_system_timezone

from sync.sync.service import values as values_service


def _parse_datetime(
	value: Any,
	*,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> datetime | None:
	if value in (None, ""):
		return None
	try:
		parsed = get_datetime(value)
	except Exception:
		return None
	if not isinstance(parsed, datetime):
		return None
	assumed_zone = _normalize_time_zone_name(assumed_time_zone)
	target_zone = _normalize_time_zone_name(target_time_zone) or _site_time_zone()
	if parsed.tzinfo is None:
		if not assumed_zone:
			return parsed
		try:
			parsed = parsed.replace(tzinfo=ZoneInfo(assumed_zone))
		except ZoneInfoNotFoundError:
			return parsed
	try:
		return parsed.astimezone(ZoneInfo(target_zone)).replace(tzinfo=None)
	except ZoneInfoNotFoundError:
		return parsed.replace(tzinfo=None)


def _get_partner_time_zone(partner_doc: Any) -> str | None:
	return _normalize_time_zone_name(values_service._first_value(partner_doc, ["time_zone"]))


def _normalize_time_zone_name(value: Any) -> str | None:
	cleaned = values_service._clean_string(value)
	if not cleaned:
		return None
	try:
		ZoneInfo(cleaned)
	except (ZoneInfoNotFoundError, ValueError) as exc:
		raise frappe.ValidationError("Time Zone must be a valid IANA zone such as Europe/Berlin.") from exc
	return cleaned


def _site_time_zone() -> str:
	try:
		return _normalize_time_zone_name(get_system_timezone()) or "UTC"
	except Exception:
		return "UTC"


def _convert_datetime_between_time_zones(
	value: Any,
	*,
	source_time_zone: str | None,
	target_time_zone: str | None,
) -> Any:
	parsed = _parse_datetime(
		value,
		assumed_time_zone=source_time_zone,
		target_time_zone=target_time_zone,
	)
	return parsed if parsed is not None else value
