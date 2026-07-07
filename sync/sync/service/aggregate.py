from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
from typing import Any


def merge_json_array_document(
	document: Any,
	*,
	array_path: str,
	items: list[dict[str, Any]],
	item_key_field: str,
	preserve_unmatched: bool = True,
	sort_field: str | None = None,
	sort_order: str | None = None,
) -> dict[str, Any]:
	result = _normalize_json_document(document)
	path = _parse_array_path(array_path)
	if not path:
		raise ValueError("JSON array path is required.")
	if not item_key_field:
		raise ValueError("Aggregate item key field is required.")

	parent = result
	for part in path[:-1]:
		next_value = parent.get(part)
		if not isinstance(next_value, dict):
			next_value = {}
			parent[part] = next_value
		parent = next_value

	array_field = path[-1]
	existing_items = parent.get(array_field)
	if existing_items in (None, ""):
		existing_items = []
	if not isinstance(existing_items, list):
		raise ValueError(f"JSON array path must point to an array: {array_path}.")

	merged_items = merge_json_array_items(
		existing_items,
		items,
		item_key_field=item_key_field,
		preserve_unmatched=preserve_unmatched,
		sort_field=sort_field,
		sort_order=sort_order,
	)
	parent[array_field] = merged_items
	return result


def merge_json_array_items(
	existing_items: list[Any],
	items: list[dict[str, Any]],
	*,
	item_key_field: str,
	preserve_unmatched: bool = True,
	sort_field: str | None = None,
	sort_order: str | None = None,
) -> list[dict[str, Any]]:
	if not item_key_field:
		raise ValueError("Aggregate item key field is required.")

	incoming_by_key: dict[Any, dict[str, Any]] = {}
	for item in items:
		if not isinstance(item, dict):
			raise ValueError("Aggregate items must be JSON objects.")
		key = item.get(item_key_field)
		if key in (None, ""):
			raise ValueError(f"Aggregate item is missing key field {item_key_field}.")
		incoming_by_key[key] = dict(item)

	result: list[dict[str, Any]] = []
	seen: set[Any] = set()
	for existing_item in existing_items:
		if not isinstance(existing_item, dict):
			continue
		key = existing_item.get(item_key_field)
		if key in incoming_by_key:
			result.append({**existing_item, **incoming_by_key[key]})
			seen.add(key)
			continue
		if preserve_unmatched:
			result.append(dict(existing_item))

	for key, item in incoming_by_key.items():
		if key not in seen:
			result.append(dict(item))

	if sort_field:
		reverse = str(sort_order or "").strip().lower() in {"desc", "descending", "z-a"}
		result = sorted(result, key=lambda item: _aggregate_sort_key(item.get(sort_field)), reverse=reverse)
	return result


def encode_json_document(document: Any) -> str:
	return json.dumps(document, default=str, ensure_ascii=True, separators=(",", ":"))


def _normalize_json_document(document: Any) -> dict[str, Any]:
	if document in (None, ""):
		return {}
	if isinstance(document, str):
		document = json.loads(document)
	if isinstance(document, dict):
		return deepcopy(document)
	raise ValueError("Aggregate JSON document must be a JSON object.")


def _parse_array_path(path: str) -> list[str]:
	value = str(path or "").strip()
	if value.startswith("$."):
		value = value[2:]
	elif value == "$":
		value = ""
	return [part.strip() for part in value.split(".") if part.strip()]


def _aggregate_sort_key(value: Any) -> tuple[int, Any]:
	if value in (None, ""):
		return (2, "")
	decimal_value = _finite_decimal(value)
	if decimal_value is not None:
		return (0, decimal_value)
	return (1, str(value))


def _finite_decimal(value: Any) -> Decimal | None:
	if isinstance(value, bool):
		return Decimal(1 if value else 0)
	try:
		decimal_value = Decimal(str(value).strip())
	except (InvalidOperation, ValueError):
		return None
	return decimal_value if decimal_value.is_finite() else None
