from __future__ import annotations

from datetime import date
from typing import Any

import frappe

try:
	from jinja2 import StrictUndefined
	from jinja2.exceptions import TemplateError
	from jinja2.sandbox import SandboxedEnvironment
except Exception:  # pragma: no cover - Frappe depends on Jinja, but keep import-time safe
	StrictUndefined = None
	TemplateError = Exception
	SandboxedEnvironment = None

from sync.sync.service import config_access as config_access_service
from sync.sync.service import values as values_service


def resolve_read_query(config: Any, connector: Any, context: dict[str, Any] | None = None) -> str | None:
	read_query = values_service._clean_string(config_access_service._config_read_query(config))
	if not read_query or not config_access_service._config_render_read_query_template(config):
		return read_query
	if SandboxedEnvironment is None or StrictUndefined is None:
		raise frappe.ValidationError("Read Query templating is unavailable because Jinja is not installed.")

	template_context = _build_read_query_template_context(connector, context=context)
	try:
		rendered_query = (
			SandboxedEnvironment(undefined=StrictUndefined).from_string(read_query).render(template_context)
		)
	except TemplateError as exc:
		raise frappe.ValidationError(f"Read Query template rendering failed: {exc}") from exc
	except Exception as exc:
		raise frappe.ValidationError(f"Read Query template helper failed: {exc}") from exc

	rendered_query = values_service._clean_string(rendered_query)
	if not rendered_query:
		raise frappe.ValidationError("Read Query template rendered an empty query.")
	return rendered_query


def _build_read_query_template_context(
	connector: Any, context: dict[str, Any] | None = None
) -> dict[str, Any]:
	today = date.today()
	template_context: dict[str, Any] = {
		"current_year": today.year,
		"previous_year": today.year - 1,
		"quote_identifier": lambda value: _template_quote_identifier(connector, value),
		"source_tables": lambda schema=None, filter=None: _template_source_tables(
			connector,
			schema=schema,
			filter_text=filter,
		),
	}
	if context:
		for key, value in context.items():
			if key not in template_context:
				template_context[str(key)] = _safe_read_query_template_value(value)
	return template_context


def _safe_read_query_template_value(value: Any) -> Any:
	if value is None or isinstance(value, (str, int, float, bool)):
		return value
	if isinstance(value, (list, tuple)):
		return [_safe_read_query_template_value(entry) for entry in value]
	if isinstance(value, dict):
		return {
			str(key): _safe_read_query_template_value(nested_value) for key, nested_value in value.items()
		}
	raise frappe.ValidationError(f"Unsafe Read Query template context value: {type(value).__name__}")


def _template_quote_identifier(connector: Any, value: Any) -> str:
	quote = getattr(connector, "quote_identifier", None)
	if not callable(quote):
		raise RuntimeError("Connector does not support identifier quoting")
	identifier = values_service._clean_string(value)
	if not identifier:
		raise RuntimeError("Identifier is required")
	return str(quote(identifier))


def _template_source_tables(connector: Any, *, schema: Any = None, filter_text: Any = None) -> list[Any]:
	list_tables = getattr(connector, "list_source_tables", None)
	if not callable(list_tables):
		raise RuntimeError("Connector does not support source-table inspection")
	tables = list(list_tables() or [])
	schema_filter = values_service._clean_string(schema)
	text_filter = values_service._clean_string(filter_text)
	if schema_filter:
		tables = [
			table
			for table in tables
			if str(getattr(table, "schema", "") or "").lower() == schema_filter.lower()
		]
	if text_filter:
		needle = text_filter.lower()
		tables = [
			table
			for table in tables
			if needle in str(getattr(table, "name", "") or "").lower()
			or needle in str(getattr(table, "full_name", "") or "").lower()
		]
	return tables
