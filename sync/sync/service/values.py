from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from frappe.utils import cint

from sync.sync.service.models import (
	DEFAULT_TIMESTAMP_BUFFER_MS,
)


def _normalize_comparable_scalar_value(value: Any) -> Any:
	decimal_value = _finite_decimal_from_scalar(value)
	if decimal_value is not None:
		return ("number", _normalize_decimal_pairing_key(decimal_value))
	return value


def _finite_decimal_from_scalar(value: Any) -> Decimal | None:
	if isinstance(value, bool):
		return Decimal(1 if value else 0)
	if isinstance(value, Decimal):
		return value if value.is_finite() else None
	if isinstance(value, int | float):
		return _finite_decimal_from_string(str(value))
	if isinstance(value, str):
		return _finite_decimal_from_string(value.strip())
	return None


def _finite_decimal_from_string(value: str) -> Decimal | None:
	if not value:
		return None
	try:
		decimal_value = Decimal(value)
	except InvalidOperation, ValueError:
		return None
	return decimal_value if decimal_value.is_finite() else None


def _normalize_decimal_pairing_key(value: Decimal) -> str:
	if not value.is_finite():
		return str(value)
	if value.is_zero():
		return "0"
	return format(value.normalize(), "f")


def _positive_int(value: Any, default: int) -> int:
	try:
		normalized = int(value)
	except Exception:
		return default
	return normalized if normalized > 0 else default


def _is_enabled(doc: Any) -> bool:
	return _as_bool(_first_value(doc, ["enabled"], default=1))


def _parse_lines(raw: Any) -> list[str]:
	if not isinstance(raw, str):
		return []
	return [line.strip() for line in raw.splitlines() if line.strip()]


def _clean_string(raw: Any) -> str | None:
	if raw is None:
		return None
	value = str(raw).strip()
	return value or None


def _first_value(doc: Any, candidates: list[str], default: Any = None) -> Any:
	for candidate in candidates:
		try:
			value = doc.get(candidate)
		except Exception:
			value = getattr(doc, candidate, None)
		if value not in (None, ""):
			return value
	return default


def _doc_name(doc: Any) -> str | None:
	if isinstance(doc, dict):
		name = doc.get("name")
	else:
		name = getattr(doc, "name", None)
	return str(name) if name not in (None, "") else None


def _row_value(row: Any, fieldname: str, default: Any = None) -> Any:
	if isinstance(row, dict):
		return row.get(fieldname, default)
	getter = getattr(row, "get", None)
	if callable(getter):
		return getter(fieldname, default)
	return getattr(row, fieldname, default)


def _first_value_dict(doc: dict[str, Any], candidates: list[str], default: Any = None) -> Any:
	for candidate in candidates:
		value = doc.get(candidate)
		if value not in (None, ""):
			return value
	return default


def _coerce_timestamp_buffer_ms(value: Any) -> int:
	if value not in (None, ""):
		return max(0, cint(value) or 0)
	return DEFAULT_TIMESTAMP_BUFFER_MS


def _as_bool(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
