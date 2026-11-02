import frappe
from frappe.desk.doctype.sidebar.sidebar import ResolvedSidebar, resolve_sidebar
from frappe.utils.modules import is_module_visible


def get_app_sidebar() -> ResolvedSidebar | None:
	"""Resolve the current user's accessible Sync navigation."""
	if is_module_visible("Sync"):
		return resolve_sidebar("Sync", frappe.session.user)
	return None


def has_app_permission() -> bool:
	"""Show the app only when the current user can navigate its sidebar."""
	return bool(get_app_sidebar())
