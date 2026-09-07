from __future__ import annotations

from datetime import UTC, datetime, timezone
from typing import Any

from sync.sync.constants import MATCH_MODE_IDENTITY_FIELDS, ONE_WAY_MATCH_ALL
from sync.sync.service import config_access as config_access_service
from sync.sync.service import mapping_rules as mapping_rules_service
from sync.sync.service import values as values_service
from sync.sync.service.models import (
	FrappeMatchLookup,
	IdentityRecordState,
	PartnerMatchLookup,
	SyncDefinitionConfig,
)


def _build_frappe_index_from_batches(
	config: SyncDefinitionConfig,
	record_batches: Any,
) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for batch in record_batches:
		index.update(_index_frappe_records(config, batch))
	return index


def _frappe_source_key_set_from_batches(
	config: SyncDefinitionConfig, record_batches: Any
) -> set[tuple[Any, ...]]:
	source_keys: set[tuple[Any, ...]] = set()
	for batch in record_batches:
		for record in batch:
			key = _key_tuple_from_frappe(record, config_access_service._config_match_fields(config))
			if _valid_key(key):
				source_keys.add(key)
	return source_keys


def _build_partner_index_from_batches(
	config: SyncDefinitionConfig,
	record_batches: Any,
) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for batch in record_batches:
		index.update(_index_partner_records(config, batch))
	return index


def _group_frappe_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
	iterable = records.values() if isinstance(records, dict) else records
	grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
	for record in iterable:
		key = _key_tuple_from_frappe(record, config_access_service._config_match_fields(config))
		if _valid_key(key):
			grouped.setdefault(key, []).append(record)
	return grouped


def _group_partner_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
	iterable = records.values() if isinstance(records, dict) else records
	grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
	for record in iterable:
		key = _key_tuple_from_partner(
			record, config_access_service._config_match_fields(config), config.mapping
		)
		if _valid_key(key):
			grouped.setdefault(key, []).append(record)
	return grouped


def _build_partner_match_lookup(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> PartnerMatchLookup:
	lookup_records = _normalize_partner_match_records(config, records)
	groups = _group_partner_records(config, lookup_records)
	return PartnerMatchLookup(
		records=lookup_records,
		groups=groups,
		latest_by_key={key: grouped_records[-1] for key, grouped_records in groups.items()},
		identity_by_value=_build_partner_identity_index(config, lookup_records),
	)


def _build_frappe_match_lookup(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> FrappeMatchLookup:
	if isinstance(records, list):
		records = sorted(records, key=_frappe_match_sort_key)
	lookup_records = _normalize_frappe_match_records(config, records)
	groups = _group_frappe_records(config, lookup_records)
	return FrappeMatchLookup(
		records=lookup_records,
		groups=groups,
		latest_by_key={key: grouped_records[-1] for key, grouped_records in groups.items()},
		identity_by_value=_build_frappe_partner_identity_index(config, lookup_records),
	)


def _frappe_match_sort_key(record: dict[str, Any]) -> tuple[str, str]:
	return (str(record.get("modified") or ""), str(record.get("name") or ""))


def _identity_records(
	records: list[dict[str, Any]] | dict[Any, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
	if not records:
		return []
	return list(records.values()) if isinstance(records, dict) else list(records)


def _build_identity_record_state(
	config: SyncDefinitionConfig,
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None,
	partner_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None,
) -> IdentityRecordState:
	frappe_list = _identity_records(frappe_records)
	partner_list = _identity_records(partner_records)
	duplicate_conflicts: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
	frappe_by_name = _identity_unique_index(
		frappe_list,
		lambda record: _identity_frappe_name(config, record),
		lambda key: f"Multiple Frappe records use the same Frappe ID {key!r}.",
		duplicate_conflicts,
		"frappe",
	)
	frappe_by_partner_id = _identity_unique_index(
		frappe_list,
		lambda record: _identity_frappe_partner_id(config, record),
		lambda key: f"Multiple Frappe records claim the same partner ID {key!r}.",
		duplicate_conflicts,
		"frappe",
	)
	partner_by_identity = _identity_unique_index(
		partner_list,
		lambda record: _identity_partner_identity(config, record),
		lambda key: f"Multiple partner records use the same partner ID {key!r}.",
		duplicate_conflicts,
		"partner",
	)
	partner_by_frappe_id = _identity_unique_index(
		partner_list,
		lambda record: _identity_partner_frappe_id(config, record),
		lambda key: f"Multiple partner records claim the same Frappe ID {key!r}.",
		duplicate_conflicts,
		"partner",
	)
	return IdentityRecordState(
		frappe_records=frappe_list,
		partner_records=partner_list,
		frappe_by_name=frappe_by_name,
		frappe_by_partner_id=frappe_by_partner_id,
		partner_by_identity=partner_by_identity,
		partner_by_frappe_id=partner_by_frappe_id,
		duplicate_conflicts=duplicate_conflicts,
	)


def _identity_unique_index(
	records, key_getter, message_getter, duplicate_conflicts, side: str
) -> dict[Any, dict[str, Any]]:
	groups: dict[Any, list[dict[str, Any]]] = {}
	for record in records:
		key = _normalize_pairing_key_value(key_getter(record))
		if key in (None, ""):
			continue
		groups.setdefault(key, []).append(record)
	index: dict[Any, dict[str, Any]] = {}
	for key, group in groups.items():
		if len(group) == 1:
			index[key] = group[0]
			continue
		if side == "frappe":
			duplicate_conflicts.append((message_getter(key), group, []))
		else:
			duplicate_conflicts.append((message_getter(key), [], group))
	return index


def _identity_frappe_name(config: SyncDefinitionConfig, record: dict[str, Any] | None) -> Any:
	return (record or {}).get("name")


def _identity_frappe_partner_id(config: SyncDefinitionConfig, record: dict[str, Any] | None) -> Any:
	return _frappe_partner_identity_value(config, record)


def _identity_partner_identity(config: SyncDefinitionConfig, record: dict[str, Any] | None) -> Any:
	return _partner_identity_value(config, record)


def _identity_partner_frappe_id(config: SyncDefinitionConfig, record: dict[str, Any] | None) -> Any:
	fieldname = config_access_service._config_partner_frappe_identity_field(config)
	if not fieldname or not record:
		return None
	return record.get(fieldname)


def _resolve_identity_partner_for_frappe(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	state: IdentityRecordState,
) -> tuple[dict[str, Any] | None, str | None]:
	frappe_name = _normalize_pairing_key_value(_identity_frappe_name(config, frappe_record))
	frappe_partner_id = _normalize_pairing_key_value(_identity_frappe_partner_id(config, frappe_record))
	by_partner_id = (
		state.partner_by_identity.get(frappe_partner_id) if frappe_partner_id not in (None, "") else None
	)
	by_frappe_id = state.partner_by_frappe_id.get(frappe_name) if frappe_name not in (None, "") else None
	if by_partner_id and by_frappe_id and by_partner_id is not by_frappe_id:
		return by_partner_id, (
			"Identity conflict: Frappe record points to one partner by partner ID, "
			"but another partner points back by Frappe ID."
		)
	partner_record = by_partner_id or by_frappe_id
	if not partner_record:
		return None, None
	partner_identity = _normalize_pairing_key_value(_identity_partner_identity(config, partner_record))
	partner_frappe_id = _normalize_pairing_key_value(_identity_partner_frappe_id(config, partner_record))
	if (
		frappe_partner_id not in (None, "")
		and partner_identity not in (None, "")
		and frappe_partner_id != partner_identity
	):
		return (
			partner_record,
			"Identity conflict: Frappe partner ID and partner own ID point to different partners.",
		)
	if (
		frappe_name not in (None, "")
		and partner_frappe_id not in (None, "")
		and frappe_name != partner_frappe_id
	):
		return partner_record, "Identity conflict: Partner Frappe ID points to a different Frappe record."
	return partner_record, None


def _resolve_identity_frappe_for_partner(
	config: SyncDefinitionConfig,
	partner_record: dict[str, Any],
	state: IdentityRecordState,
) -> tuple[dict[str, Any] | None, str | None]:
	partner_identity = _normalize_pairing_key_value(_identity_partner_identity(config, partner_record))
	partner_frappe_id = _normalize_pairing_key_value(_identity_partner_frappe_id(config, partner_record))
	by_partner_id = (
		state.frappe_by_partner_id.get(partner_identity) if partner_identity not in (None, "") else None
	)
	by_frappe_id = (
		state.frappe_by_name.get(partner_frappe_id) if partner_frappe_id not in (None, "") else None
	)
	if by_partner_id and by_frappe_id and by_partner_id is not by_frappe_id:
		return by_frappe_id, (
			"Identity conflict: Partner record points to one Frappe record by Frappe ID, "
			"but another Frappe record points back by partner ID."
		)
	frappe_record = by_partner_id or by_frappe_id
	if not frappe_record:
		return None, None
	frappe_name = _normalize_pairing_key_value(_identity_frappe_name(config, frappe_record))
	frappe_partner_id = _normalize_pairing_key_value(_identity_frappe_partner_id(config, frappe_record))
	if (
		partner_frappe_id not in (None, "")
		and frappe_name not in (None, "")
		and partner_frappe_id != frappe_name
	):
		return (
			frappe_record,
			"Identity conflict: Partner Frappe ID and Frappe own ID point to different Frappe records.",
		)
	if (
		partner_identity not in (None, "")
		and frappe_partner_id not in (None, "")
		and partner_identity != frappe_partner_id
	):
		return frappe_record, "Identity conflict: Frappe partner ID points to a different partner record."
	return frappe_record, None


def _identity_conflict_keys(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any] | None = None,
	partner_record: dict[str, Any] | None = None,
) -> set[tuple[str, Any]]:
	# Include claimed IDs so an ambiguous link cannot become an unmatched create.
	# These keys must survive separate delta and full lookup reads.
	values = (
		("frappe", _identity_frappe_name(config, frappe_record)),
		("partner", _identity_frappe_partner_id(config, frappe_record)),
		("partner", _identity_partner_identity(config, partner_record)),
		("frappe", _identity_partner_frappe_id(config, partner_record)),
	)
	return {
		(side, key)
		for side, value in values
		if (key := _normalize_pairing_key_value(value)) not in (None, "")
	}


def _identity_pair_key(
	config: SyncDefinitionConfig, frappe_record: dict[str, Any], partner_record: dict[str, Any]
) -> tuple[Any, Any]:
	return (
		_normalize_pairing_key_value(_identity_frappe_name(config, frappe_record)) or id(frappe_record),
		_normalize_pairing_key_value(_identity_partner_identity(config, partner_record))
		or id(partner_record),
	)


def _index_frappe_records(
	config: SyncDefinitionConfig, records: list[dict[str, Any]]
) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in records:
		key = _key_tuple_from_frappe(record, config_access_service._config_match_fields(config))
		if _valid_key(key):
			index[key] = record
	return index


def _index_partner_records(
	config: SyncDefinitionConfig, records: list[dict[str, Any]]
) -> dict[tuple[Any, ...], dict[str, Any]]:
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in records:
		key = _key_tuple_from_partner(
			record, config_access_service._config_match_fields(config), config.mapping
		)
		if _valid_key(key):
			index[key] = record
	return index


def _key_tuple_from_frappe(record: dict[str, Any], key_fields: list[str]) -> tuple[Any, ...]:
	return _normalize_pairing_key_tuple(record.get(field_name) for field_name in key_fields)


def _key_tuple_from_partner(
	record: dict[str, Any], key_fields: list[str], mapping: dict[str, Any]
) -> tuple[Any, ...]:
	return _normalize_pairing_key_tuple(
		record.get(mapping_rules_service._partner_field_for_mapping(mapping, field_name, field_name))
		for field_name in key_fields
	)


def _raw_key_tuple_from_frappe(record: dict[str, Any], key_fields: list[str]) -> tuple[Any, ...]:
	return tuple(record.get(field_name) for field_name in key_fields)


def _raw_key_tuple_from_partner(
	record: dict[str, Any], key_fields: list[str], mapping: dict[str, Any]
) -> tuple[Any, ...]:
	return tuple(
		record.get(mapping_rules_service._partner_field_for_mapping(mapping, field_name, field_name))
		for field_name in key_fields
	)


def _normalize_pairing_key_tuple(values: Any) -> tuple[Any, ...]:
	return tuple(_normalize_pairing_key_value(value) for value in values)


def _normalize_pairing_key_value(value: Any) -> Any:
	if value is None:
		return None
	if isinstance(value, datetime):
		return ("datetime", _normalize_datetime_pairing_key(value))
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return ""
		datetime_key = _normalize_datetime_string_pairing_key(value)
		if datetime_key is not None:
			return ("datetime", datetime_key)
		return values_service._normalize_comparable_scalar_value(value)
	if values_service._finite_decimal_from_scalar(value) is not None:
		return values_service._normalize_comparable_scalar_value(value)
	return str(value)


def _normalize_number_pairing_key(value: str) -> str:
	decimal_value = values_service._finite_decimal_from_string(value)
	if decimal_value is None:
		return value
	return values_service._normalize_decimal_pairing_key(decimal_value)


def _normalize_datetime_pairing_key(value: datetime) -> str:
	if value.tzinfo is not None and value.utcoffset() is not None:
		value = value.astimezone(UTC)
	return value.isoformat(timespec="microseconds")


def _normalize_datetime_string_pairing_key(value: str) -> str | None:
	if "-" not in value or (":" not in value and "T" not in value):
		return None
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	return _normalize_datetime_pairing_key(parsed)


def _valid_key(key: tuple[Any, ...]) -> bool:
	return bool(key) and all(value not in (None, "") for value in key)


def _partner_key_values_from_tuple(
	config: SyncDefinitionConfig, key_values: tuple[Any, ...]
) -> dict[str, Any]:
	result = {}
	for idx, frappe_key in enumerate(config_access_service._config_match_fields(config)):
		partner_field = mapping_rules_service._partner_field_for_mapping(
			config.mapping, frappe_key, frappe_key
		)
		result[partner_field] = key_values[idx]
	return result


def _partner_key_values_from_frappe_record(
	config: SyncDefinitionConfig, record: dict[str, Any]
) -> dict[str, Any]:
	return _partner_key_values_from_tuple(
		config, _raw_key_tuple_from_frappe(record, config_access_service._config_match_fields(config))
	)


def _partner_key_values_from_partner_record(
	config: SyncDefinitionConfig, record: dict[str, Any]
) -> dict[str, Any]:
	return _partner_key_values_from_tuple(
		config,
		_raw_key_tuple_from_partner(
			record, config_access_service._config_match_fields(config), config.mapping
		),
	)


def _partner_fetch_key_fields(config: SyncDefinitionConfig) -> list[str]:
	if config_access_service._config_match_mode(config) == MATCH_MODE_IDENTITY_FIELDS:
		return [field for field in [config_access_service._config_partner_identity_field(config)] if field]
	mapping = getattr(config, "mapping", {}) or {}
	fields = [
		mapping_rules_service._partner_field_for_mapping(mapping, frappe_field, frappe_field)
		for frappe_field in config_access_service._config_match_fields(config)
	]
	if config_access_service._config_partner_identity_field(config):
		fields.append(config_access_service._config_partner_identity_field(config) or "")
	if config_access_service._config_partner_frappe_identity_field(config):
		fields.append(config_access_service._config_partner_frappe_identity_field(config) or "")
	return [field for field in fields if field]


def _frappe_partner_identity_value(config: SyncDefinitionConfig, frappe_record: dict[str, Any] | None) -> Any:
	fieldname = config_access_service._config_frappe_partner_identity_field(config)
	if not fieldname or not frappe_record:
		return None
	return frappe_record.get(fieldname)


def _partner_identity_value(config: SyncDefinitionConfig, partner_record: dict[str, Any] | None) -> Any:
	fieldname = config_access_service._config_partner_identity_field(config)
	if not fieldname or not partner_record:
		return None
	return partner_record.get(fieldname)


def _build_partner_identity_index(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[Any, dict[str, Any]]:
	iterable = records.values() if isinstance(records, dict) else records
	index: dict[Any, dict[str, Any]] = {}
	for record in iterable:
		identity = _normalize_pairing_key_value(_partner_identity_value(config, record))
		if identity not in (None, ""):
			index[identity] = record
	return index


def _normalize_partner_match_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]:
	if (
		isinstance(records, dict)
		or config_access_service._config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL
	):
		return records
	return _index_partner_records(config, records)


def _build_frappe_partner_identity_index(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[Any, dict[str, Any]]:
	fieldname = config_access_service._config_frappe_partner_identity_field(config)
	if not fieldname:
		return {}
	iterable = records.values() if isinstance(records, dict) else records
	index: dict[Any, dict[str, Any]] = {}
	for record in iterable:
		identity = _normalize_pairing_key_value(record.get(fieldname))
		if identity not in (None, ""):
			index[identity] = record
	return index


def _normalize_frappe_match_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]:
	if (
		isinstance(records, dict)
		or config_access_service._config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL
	):
		return records
	return _index_frappe_records(config, records)


def _find_existing_partner_record(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	partner_index: dict[tuple[Any, ...], dict[str, Any]],
	partner_identity_index: dict[Any, dict[str, Any]],
) -> dict[str, Any] | None:
	return next(
		iter(
			_find_existing_partner_records(
				config,
				frappe_record,
				{key: [record] for key, record in partner_index.items()},
				partner_identity_index,
			)
		),
		None,
	)


def _find_existing_partner_records(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	partner_groups: dict[tuple[Any, ...], list[dict[str, Any]]],
	partner_identity_index: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
	frappe_partner_id = _normalize_pairing_key_value(_frappe_partner_identity_value(config, frappe_record))
	if frappe_partner_id not in (None, ""):
		existing = partner_identity_index.get(frappe_partner_id)
		if existing:
			return [existing]
	key = _key_tuple_from_frappe(frappe_record, config_access_service._config_match_fields(config))
	if _valid_key(key):
		matches = list(partner_groups.get(key) or [])
		if config_access_service._config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL:
			return matches
		return matches[-1:] if matches else []
	return []


def _find_existing_frappe_record(
	config: SyncDefinitionConfig,
	partner_record: dict[str, Any],
	frappe_index: dict[tuple[Any, ...], dict[str, Any]],
	frappe_partner_identity_index: dict[Any, dict[str, Any]],
) -> dict[str, Any] | None:
	return next(
		iter(
			_find_existing_frappe_records(
				config,
				partner_record,
				{key: [record] for key, record in frappe_index.items()},
				frappe_partner_identity_index,
			)
		),
		None,
	)


def _find_existing_frappe_records(
	config: SyncDefinitionConfig,
	partner_record: dict[str, Any],
	frappe_groups: dict[tuple[Any, ...], list[dict[str, Any]]],
	frappe_partner_identity_index: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
	partner_identity = _normalize_pairing_key_value(_partner_identity_value(config, partner_record))
	if partner_identity not in (None, ""):
		existing = frappe_partner_identity_index.get(partner_identity)
		if existing:
			return [existing]
	key = _key_tuple_from_partner(
		partner_record, config_access_service._config_match_fields(config), config.mapping
	)
	if _valid_key(key):
		matches = list(frappe_groups.get(key) or [])
		if config_access_service._config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL:
			return matches
		return matches[-1:] if matches else []
	return []


def _pair_token_from_frappe(config: SyncDefinitionConfig, record: dict[str, Any]) -> tuple[Any, ...] | None:
	identity = _normalize_pairing_key_value(_frappe_partner_identity_value(config, record))
	if config_access_service._config_partner_identity_field(config) and identity not in (None, ""):
		return ("partner_identity", identity)
	key = _key_tuple_from_frappe(record, config_access_service._config_match_fields(config))
	if _valid_key(key):
		return ("match", *key)
	return None


def _pair_token_from_partner(config: SyncDefinitionConfig, record: dict[str, Any]) -> tuple[Any, ...] | None:
	identity = _normalize_pairing_key_value(_partner_identity_value(config, record))
	if config_access_service._config_partner_identity_field(config) and identity not in (None, ""):
		return ("partner_identity", identity)
	key = _key_tuple_from_partner(record, config_access_service._config_match_fields(config), config.mapping)
	if _valid_key(key):
		return ("match", *key)
	return None


def _index_paired_frappe_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[tuple[Any, ...], dict[str, Any]]:
	iterable = records.values() if isinstance(records, dict) else records
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in iterable:
		token = _pair_token_from_frappe(config, record)
		if token:
			index[token] = record
	return index


def _index_paired_partner_records(
	config: SyncDefinitionConfig,
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
) -> dict[tuple[Any, ...], dict[str, Any]]:
	iterable = records.values() if isinstance(records, dict) else records
	index: dict[tuple[Any, ...], dict[str, Any]] = {}
	for record in iterable:
		token = _pair_token_from_partner(config, record)
		if token:
			index[token] = record
	return index


def _partner_key_values_for_write(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	key: tuple[Any, ...],
) -> dict[str, Any]:
	frappe_partner_id = _frappe_partner_identity_value(config, frappe_record)
	partner_identity_field = config_access_service._config_partner_identity_field(config)
	if partner_identity_field and frappe_partner_id not in (None, ""):
		return {partner_identity_field: frappe_partner_id}
	return _partner_key_values_from_frappe_record(config, frappe_record)


def _partner_key_values_for_existing_match(
	config: SyncDefinitionConfig,
	frappe_record: dict[str, Any],
	key: tuple[Any, ...],
	partner_record: dict[str, Any] | None,
) -> dict[str, Any]:
	partner_identity_field = config_access_service._config_partner_identity_field(config)
	if (
		partner_identity_field
		and partner_record
		and partner_record.get(partner_identity_field) not in (None, "")
	):
		return {partner_identity_field: partner_record.get(partner_identity_field)}
	if partner_record:
		return _partner_key_values_from_partner_record(config, partner_record)
	return _partner_key_values_for_write(config, frappe_record, key)


def _can_write_partner_matches_individually(
	config: SyncDefinitionConfig,
	partner_records: list[dict[str, Any]],
) -> bool:
	if len(partner_records) <= 1:
		return True
	partner_identity_field = config_access_service._config_partner_identity_field(config)
	if not partner_identity_field:
		return False
	return all(record.get(partner_identity_field) not in (None, "") for record in partner_records)
