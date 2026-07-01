import frappe


def execute():
	if not frappe.db.has_column("Sync Definition", "match_mode"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabSync Definition`
		SET `match_mode` = 'Match Fields'
		WHERE `match_mode` IS NULL OR `match_mode` = ''
		"""
	)
