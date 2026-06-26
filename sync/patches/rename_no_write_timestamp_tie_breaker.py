import frappe


def execute():
	frappe.db.sql(
		"""
		UPDATE `tabSync Definition`
		SET `timestamp_tie_breaker` = 'Manual'
		WHERE `timestamp_tie_breaker` = 'No Write'
		"""
	)
