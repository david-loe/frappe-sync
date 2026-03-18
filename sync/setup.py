# Copyright (c) 2026, david-loe and contributors

import frappe


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


def after_migrate():
	ensure_default_partner_types()


def before_tests():
	ensure_default_partner_types()


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
