from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sync.sync.constants import (
	CONFLICT_POLICY_NEWEST_WINS,
	TIMESTAMP_TIE_FRAPPE_WINS,
	TIMESTAMP_TIE_PARTNER_WINS,
)
from sync.sync.service import config_access as config_access_service
from sync.sync.service import mapping_rules as mapping_rules_service
from sync.sync.service import time_utils as time_utils_service
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	SyncDefinitionConfig,
)


def _canonical_conflict_changes(
	config: SyncDefinitionConfig | Any,
	*,
	to_frappe_changes: list[tuple[str, Any, Any]],
	to_partner_changes: list[tuple[str, Any, Any]],
) -> list[tuple[str, Any, Any]]:
	if to_frappe_changes:
		return to_frappe_changes
	partner_to_frappe = {
		entry["partner_field"]: frappe_field
		for frappe_field, entry in mapping_rules_service._iter_field_mapping_entries(
			getattr(config, "mapping", {})
		)
	}
	return [
		(partner_to_frappe.get(fieldname, fieldname), old_value, new_value)
		for fieldname, old_value, new_value in to_partner_changes
	]


def _diff_target_values(
	*,
	new_record: dict[str, Any],
	old_record: dict[str, Any],
	field_names: list[str],
	exclude_fields: set[str] | None = None,
	datetime_fields: set[str] | None = None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> list[tuple[str, Any, Any]]:
	changes: list[tuple[str, Any, Any]] = []
	for field_name in field_names:
		if field_name in (exclude_fields or set()):
			continue
		old_value = mapping_rules_service._get_frappe_payload_value(old_record, field_name)
		new_value = mapping_rules_service._get_frappe_payload_value(new_record, field_name)
		if _normalize_field_value(
			field_name,
			old_value,
			datetime_fields=datetime_fields,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		) != _normalize_field_value(
			field_name,
			new_value,
			datetime_fields=datetime_fields,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		):
			changes.append((field_name, old_value, new_value))
	return changes


def _normalize_field_value(
	field_name: str,
	value: Any,
	*,
	datetime_fields: set[str] | None = None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> Any:
	if field_name in (datetime_fields or set()) or isinstance(value, datetime | date):
		parsed = time_utils_service._parse_datetime(
			value,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		)
		if parsed is not None:
			return parsed
		if isinstance(value, datetime):
			return value.replace(tzinfo=None)
		if isinstance(value, date):
			return datetime.combine(value, datetime.min.time())
	if isinstance(value, str) and values_service._finite_decimal_from_string(value.strip()) is None:
		parsed = time_utils_service._parse_datetime(
			value,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		)
		if parsed is not None:
			return parsed
	if isinstance(value, list | dict):
		return json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
	return values_service._normalize_comparable_scalar_value(value)


def _record_changed_since(
	record: dict[str, Any],
	modified_fields: list[str] | str | None,
	last_successful_sync: datetime | None,
	*,
	creation_field: str | None = None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> bool:
	if not last_successful_sync:
		return True
	modified_field = config_access_service._first_configured_field(
		[modified_fields] if isinstance(modified_fields, str) else modified_fields,
		None,
	)
	if not modified_field:
		return False
	effective = _effective_modified(
		record,
		modified_field=modified_field,
		creation_field=creation_field,
		assumed_time_zone=assumed_time_zone,
		target_time_zone=target_time_zone,
	)
	return bool(effective and effective >= last_successful_sync)


def _latest_modified(
	record: dict[str, Any],
	modified_fields: list[str] | str | None,
	*,
	creation_field: str | None = None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> datetime | None:
	modified_field = config_access_service._first_configured_field(
		[modified_fields] if isinstance(modified_fields, str) else modified_fields,
		None,
	)
	if not modified_field:
		return None
	return _effective_modified(
		record,
		modified_field=modified_field,
		creation_field=creation_field,
		assumed_time_zone=assumed_time_zone,
		target_time_zone=target_time_zone,
	)


def _compare_modified_timestamps(
	frappe_latest: datetime | None,
	partner_latest: datetime | None,
	*,
	buffer_ms: int,
) -> str | None:
	if partner_latest and not frappe_latest:
		return "partner"
	if frappe_latest and not partner_latest:
		return "frappe"
	if not frappe_latest or not partner_latest:
		return None
	if abs(partner_latest - frappe_latest) <= timedelta(milliseconds=max(0, buffer_ms)):
		return None
	return "partner" if partner_latest > frappe_latest else "frappe"


def _effective_modified(
	record: dict[str, Any],
	*,
	modified_field: str,
	creation_field: str | None,
	assumed_time_zone: str | None = None,
	target_time_zone: str | None = None,
) -> datetime | None:
	modified_value = record.get(modified_field)
	if modified_value not in (None, ""):
		return time_utils_service._parse_datetime(
			modified_value,
			assumed_time_zone=assumed_time_zone,
			target_time_zone=target_time_zone,
		)
	if not creation_field:
		return None
	return time_utils_service._parse_datetime(
		record.get(creation_field),
		assumed_time_zone=assumed_time_zone,
		target_time_zone=target_time_zone,
	)


def resolve_conflict(
	config: SyncDefinitionConfig,
	*,
	frappe_record: dict[str, Any],
	partner_record: dict[str, Any],
	last_successful_sync: datetime | None,
	site_time_zone: str,
) -> Literal[
	"frappe_changed",
	"partner_changed",
	"unsupported",
	"frappe_newest",
	"partner_newest",
	"frappe_tie",
	"partner_tie",
	"manual",
]:
	"""Choose a winner without writing records or emitting audit items."""
	frappe_changed = _record_changed_since(
		record=frappe_record,
		modified_fields=config_access_service._config_frappe_modified_field(config),
		creation_field=config_access_service._config_frappe_creation_field(config),
		last_successful_sync=last_successful_sync,
		target_time_zone=site_time_zone,
	)
	partner_changed = _record_changed_since(
		record=partner_record,
		modified_fields=config_access_service._config_partner_modified_field(config),
		creation_field=config_access_service._config_partner_creation_field(config),
		last_successful_sync=last_successful_sync,
		assumed_time_zone=getattr(config, "partner_time_zone", None),
		target_time_zone=site_time_zone,
	)
	if frappe_changed and not partner_changed:
		return "frappe_changed"
	if partner_changed and not frappe_changed:
		return "partner_changed"
	if config.conflict_policy != CONFLICT_POLICY_NEWEST_WINS:
		return "unsupported"
	frappe_latest = _latest_modified(
		record=frappe_record,
		modified_fields=config_access_service._config_frappe_modified_field(config),
		creation_field=config_access_service._config_frappe_creation_field(config),
		target_time_zone=site_time_zone,
	)
	partner_latest = _latest_modified(
		record=partner_record,
		modified_fields=config_access_service._config_partner_modified_field(config),
		creation_field=config_access_service._config_partner_creation_field(config),
		assumed_time_zone=getattr(config, "partner_time_zone", None),
		target_time_zone=site_time_zone,
	)
	winner = _compare_modified_timestamps(
		frappe_latest, partner_latest, buffer_ms=config_access_service._config_timestamp_buffer_ms(config)
	)
	if winner == "frappe":
		return "frappe_newest"
	if winner == "partner":
		return "partner_newest"
	if config_access_service._config_timestamp_tie_breaker(config) == TIMESTAMP_TIE_PARTNER_WINS:
		return "partner_tie"
	if config_access_service._config_timestamp_tie_breaker(config) == TIMESTAMP_TIE_FRAPPE_WINS:
		return "frappe_tie"
	return "manual"
