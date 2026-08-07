# Copyright (c) 2026, david-loe and contributors

import frappe

from sync.sync.constants import SYNC_RUN_ITEM


DEFAULT_PARTNER_TYPES = (
	{
		"partner_type_code": "mssql",
		"label": "MSSQL",
		"default_port": 1433,
		"db_api_module": "pyodbc",
		"supports_table": 1,
		"supports_query": 1,
		"description": "Microsoft SQL Server connector.",
	},
	{
		"partner_type_code": "postgres",
		"label": "Postgres",
		"default_port": 5432,
		"db_api_module": "psycopg",
		"supports_table": 1,
		"supports_query": 1,
		"description": "PostgreSQL connector.",
	},
	{
		"partner_type_code": "firebird",
		"label": "Firebird",
		"default_port": 3050,
		"db_api_module": "fdb",
		"supports_table": 1,
		"supports_query": 1,
		"description": "Firebird SQL connector.",
	},
)

SYNC_RUN_ITEM_INDEXES = (
	("sync_run_creation_index", ("sync_run", "creation")),
	("status_creation_index", ("status", "creation")),
	("sync_run_status_creation_index", ("sync_run", "status", "creation")),
)


def after_migrate():
	ensure_default_partner_types()
	ensure_default_sync_settings()
	ensure_sync_run_item_indexes()


def before_tests():
	ensure_default_partner_types()
	ensure_default_sync_settings()
	ensure_sync_run_item_indexes()


def ensure_sync_run_item_indexes():
	if not frappe.db.table_exists(SYNC_RUN_ITEM):
		return

	for index_name, fields in SYNC_RUN_ITEM_INDEXES:
		frappe.db.add_index(SYNC_RUN_ITEM, fields, index_name=index_name)


def ensure_default_partner_types():
	for payload in DEFAULT_PARTNER_TYPES:
		if frappe.db.exists("Sync Partner Type", payload["partner_type_code"]):
			doc = frappe.get_doc("Sync Partner Type", payload["partner_type_code"])
			doc.update(payload)
			doc.save(ignore_permissions=True)
			continue

		doc = frappe.new_doc("Sync Partner Type")
		doc.update(payload)
		doc.insert(ignore_permissions=True)


def ensure_default_sync_settings():
	exists = getattr(getattr(frappe, "db", None), "exists", None)
	if callable(exists):
		try:
			if not exists("DocType", "Sync Settings"):
				return
		except Exception:
			pass
	payload = {
		"stale_run_timeout_minutes": 180,
		"run_retention_days_success": 90,
		"run_retention_days_error": 365,
	}
	try:
		doc = frappe.get_single("Sync Settings")
	except Exception:
		doc = frappe.new_doc("Sync Settings")
	doc.update({fieldname: getattr(doc, fieldname, None) or value for fieldname, value in payload.items()})
	doc.save(ignore_permissions=True)
