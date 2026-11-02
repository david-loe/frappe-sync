import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.boot import get_app_data
from frappe.desk.doctype.desktop_icon.desktop_icon import (
	create_desktop_icons_from_installed_apps,
	get_desktop_icons,
)
from frappe.desk.doctype.sidebar.sidebar import resolve_sidebar
from frappe.tests import IntegrationTestCase

from sync import hooks
from sync.api import open_app
from sync.config.desktop import has_app_permission


class TestDesktopNavigation(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(frappe.local.response.pop, "type", None)
		self.addCleanup(frappe.local.response.pop, "location", None)

	def test_administrator_can_open_shipped_navigation(self):
		self.assertTrue(has_app_permission())
		sidebar = resolve_sidebar("Sync", frappe.session.user)
		self.assertFalse(sidebar.computed)
		self.assertEqual(
			[row["link_to"] for row in sidebar.items if row["type"] == "Link"],
			["Sync Run", "Sync Definition", "Sync Partner", "Sync Settings"],
		)
		open_app()
		self.assertEqual(frappe.local.response.type, "redirect")
		self.assertEqual(frappe.local.response.location, "/desk/sync-run")

	def test_system_manager_has_apps_entry_and_dock(self):
		user = self.make_user("System Manager")
		frappe.set_user(user.name)
		self.assertTrue(has_app_permission())
		app = self.get_app_entry()
		self.assertTrue(app["on_apps_screen"])
		self.assertEqual(app["app_route"], "/api/method/sync.api.open_app")
		self.assertEqual(app["app_logo_url"], hooks.app_logo_url)
		self.assertTrue(
			Path(frappe.get_app_path("sync", "public", *hooks.app_logo_url.split("/")[3:])).is_file()
		)
		self.assertEqual(
			[(row["link_type"], row["link_to"]) for row in app["dock"]],
			[("Sidebar", "Sync")],
		)

	def test_classic_app_icon_can_open_module(self):
		frappe.db.delete("Desktop Icon", {"name": "Sync"})
		# Limit icon generation without hiding installed apps from permission and query hooks.
		get_hooks = frappe.get_hooks

		def app_hooks(hook=None, *args, **kwargs):
			if hook == "add_to_apps_screen" and kwargs.get("app_name") != "sync":
				return []
			return get_hooks(hook, *args, **kwargs)

		with patch("frappe.get_hooks", side_effect=app_hooks):
			create_desktop_icons_from_installed_apps()
		icon = frappe.get_doc("Desktop Icon", "Sync")
		self.assertEqual(icon.icon_type, "App")
		self.assertEqual(icon.link_type, "External")
		self.assertEqual(icon.link, "/api/method/sync.api.open_app")
		self.assertEqual(icon.logo_url, hooks.app_logo_url)
		open_app()
		self.assertEqual(frappe.local.response.location, "/desk/sync-run")

	def test_shipped_classic_icon_uses_resolved_sidebar(self):
		frappe.db.delete("Desktop Icon", {"name": "Sync"})
		payload = json.loads(Path(frappe.get_app_path("sync", "desktop_icon", "sync.json")).read_text())
		# The legacy dynamic link is retained by Frappe, but its target is now an archive.
		with patch.dict(frappe.flags, {"in_import": True}):
			frappe.get_doc(payload).insert(ignore_permissions=True, ignore_links=True)
		sidebar = resolve_sidebar("Sync", frappe.session.user)
		bootinfo = frappe._dict(module_sidebars={"Sync": sidebar.as_boot_entry()})
		icons = get_desktop_icons(bootinfo=bootinfo)
		icon = next(icon for icon in icons if icon.name == "Sync")
		self.assertEqual(icon.logo_url, hooks.app_logo_url)
		self.assertEqual(icon.link_to, "Sync")
		self.assertEqual(sidebar.landing, "/desk/sync-run")

	def test_open_app_follows_sidebar_customizations(self):
		user = self.make_user("System Manager")
		frappe.get_doc(
			{
				"doctype": "Custom Sidebar",
				"module": "Sync",
				"user": user.name,
				"sidebar_items": [
					{"type": "Link", "link_type": "DocType", "link_to": "Sync Run", "hidden": 1}
				],
			}
		).insert()
		frappe.set_user(user.name)
		open_app()
		self.assertEqual(frappe.local.response.type, "redirect")
		self.assertEqual(frappe.local.response.location, "/desk/sync-definition")

	def test_user_without_access_has_no_apps_entry(self):
		user = self.make_user("Desk User")
		frappe.set_user(user.name)
		self.assertFalse(has_app_permission())
		with self.assertRaises(frappe.PermissionError):
			open_app()
		app = self.get_app_entry()
		self.assertFalse(app["on_apps_screen"])
		self.assertEqual(app["dock"], [])

	def test_blocked_module_hides_app_for_system_manager(self):
		user = self.make_user("System Manager")
		user.append("block_modules", {"module": "Sync"})
		user.save()
		frappe.set_user(user.name)
		self.assertFalse(has_app_permission())
		with self.assertRaises(frappe.PermissionError):
			open_app()
		app = self.get_app_entry()
		self.assertFalse(app["on_apps_screen"])
		self.assertEqual(app["dock"], [])

	@staticmethod
	def make_user(role):
		return frappe.get_doc(
			{
				"doctype": "User",
				"email": f"sync-desk-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Desk Test",
				"send_welcome_email": 0,
				"roles": [{"role": role}],
			}
		).insert()

	@staticmethod
	def get_app_entry():
		return next(app for app in get_app_data() if app["app_name"] == "sync")
