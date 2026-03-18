from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from sync.sync.service import runtime


class TestRuntimeExecution(IntegrationTestCase):
	def _make_partner(self) -> Any:
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Sync Partner",
				"partner_name": f"Test Partner {suffix}",
				"partner_type": "mssql",
				"host": "localhost",
				"database_name": "sync_test",
				"username": "tester",
			}
		).insert(ignore_permissions=True)

	def _make_definition(self, partner_name: str, *, title_suffix: str | None = None) -> Any:
		suffix = frappe.generate_hash(length=8)
		if title_suffix:
			suffix = f"{title_suffix}-{suffix}"
		return frappe.get_doc(
			{
				"doctype": "Sync Definition",
				"title": f"SYNC-DEF-{suffix}",
				"partner": partner_name,
				"sync_type": "A->B",
				"doctype_name": "ToDo",
				"frequency_cron": "*/15 * * * *",
				"table_name": "tabToDo",
				"batch_size": 25,
				"key_fields": [{"doctype": "Sync Key Field", "frappe_field": "name"}],
				"field_mapping": [
					{"doctype": "Sync Field Mapping", "frappe_field": "name", "partner_field": "external_name"}
				],
				"frappe_modified_field_rows": [{"doctype": "Sync Modified Field", "field_name": "modified"}],
				"partner_modified_field_rows": [{"doctype": "Sync Modified Field", "field_name": "updated_at"}],
			}
		).insert(ignore_permissions=True)

	def test_execute_sync_definition_persists_success_state(self):
		partner = self._make_partner()
		definition = self._make_definition(partner.name)
		result_payload = {
			"sync_definition": definition.name,
			"sync_type": "A->B",
			"last_successful_sync_before_run": None,
			"delta_since": None,
			"dry_run": False,
			"processed_count": 3,
			"success_count": 2,
			"created_count": 1,
			"updated_count": 1,
			"deleted_count": 0,
			"skipped_count": 1,
			"conflict_count": 0,
			"error_count": 0,
		}

		with patch("sync.sync.service.runtime._run_engine", return_value=result_payload):
			result = runtime.execute_sync_definition(definition.name, trigger="api")

		self.assertEqual(result["status"], "success")
		run_doc = frappe.get_doc("Sync Run", result["run"])
		definition.reload()

		self.assertEqual(run_doc.status, "Success")
		self.assertEqual(run_doc.trigger_type, "api")
		self.assertEqual(run_doc.sync_definition, definition.name)
		self.assertEqual(run_doc.sync_partner, partner.name)
		self.assertEqual(run_doc.processed_count, 3)
		self.assertEqual(run_doc.created_count, 1)
		self.assertEqual(run_doc.updated_count, 1)
		self.assertEqual(run_doc.skipped_count, 1)
		self.assertEqual(run_doc.error_count, 0)
		self.assertIn("processed=3", run_doc.summary)
		self.assertEqual(definition.last_run, run_doc.name)
		self.assertEqual(definition.last_run_status, "Success")
		self.assertIn("created=1", definition.last_run_summary)
		self.assertIsNotNone(definition.next_run_at)

	def test_execute_sync_definition_persists_failure_state(self):
		partner = self._make_partner()
		definition = self._make_definition(partner.name)

		with (
			patch("sync.sync.service.runtime._run_engine", side_effect=RuntimeError("boom")),
			self.assertRaises(RuntimeError),
		):
			runtime.execute_sync_definition(definition.name, trigger="manual")

		run_name = frappe.get_all(
			"Sync Run",
			filters={"sync_definition": definition.name},
			order_by="creation desc",
			limit=1,
			pluck="name",
		)[0]
		run_doc = frappe.get_doc("Sync Run", run_name)
		definition.reload()

		self.assertEqual(run_doc.status, "Error")
		self.assertIn("RuntimeError: boom", run_doc.error_message)
		self.assertEqual(definition.last_run, run_doc.name)
		self.assertEqual(definition.last_run_status, "Error")
		self.assertIn("boom", definition.last_run_summary)

	def test_enqueue_sync_definition_creates_queued_run(self):
		partner = self._make_partner()
		definition = self._make_definition(partner.name)

		with patch("sync.sync.service.runtime.frappe.enqueue") as mock_enqueue:
			result = runtime.enqueue_sync_definition(definition.name, trigger="manual", queue=True, dry_run=True)

		self.assertEqual(result["status"], "queued")
		run_doc = frappe.get_doc("Sync Run", result["run"])
		self.assertEqual(run_doc.status, "Queued")
		self.assertEqual(run_doc.dry_run, 1)
		self.assertEqual(run_doc.job_id, result["job_id"])
		mock_enqueue.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.kwargs["sync_definition_name"], definition.name)
		self.assertEqual(mock_enqueue.call_args.kwargs["run_name"], run_doc.name)
		self.assertTrue(mock_enqueue.call_args.kwargs["dry_run"])

	def test_enqueue_sync_definition_returns_already_running_without_run_creation(self):
		partner = self._make_partner()
		definition = self._make_definition(partner.name)
		existing_runs = frappe.db.count("Sync Run", {"sync_definition": definition.name})

		with patch("sync.sync.service.runtime._has_active_run", return_value=True):
			result = runtime.enqueue_sync_definition(definition.name, trigger="scheduler", queue=True)

		self.assertEqual(result, {"status": "already_running", "sync_definition": definition.name})
		self.assertEqual(frappe.db.count("Sync Run", {"sync_definition": definition.name}), existing_runs)

	def test_list_due_sync_definitions_uses_next_run_and_enabled_flag(self):
		partner = self._make_partner()
		now = now_datetime().replace(microsecond=0)
		ready = self._make_definition(partner.name, title_suffix="ready")
		future = self._make_definition(partner.name, title_suffix="future")
		disabled = self._make_definition(partner.name, title_suffix="disabled")

		ready.db_set("next_run_at", now - timedelta(minutes=1), update_modified=False)
		future.db_set("next_run_at", now + timedelta(minutes=30), update_modified=False)
		disabled.db_set("next_run_at", now - timedelta(minutes=1), update_modified=False)
		disabled.db_set("enabled", 0, update_modified=False)

		with patch("sync.sync.service.runtime._is_due_by_cron", return_value=False):
			due = runtime.list_due_sync_definitions(now=now)

		self.assertIn(ready.name, due)
		self.assertNotIn(future.name, due)
		self.assertNotIn(disabled.name, due)

	def test_test_sync_partner_connection_updates_partner_status(self):
		partner = self._make_partner()
		ping = SimpleNamespace(ok=False, message="dial timeout", details={"dialect": "mssql"})

		with patch("sync.sync.service.runtime.get_connector_for_partner", return_value=SimpleNamespace(ping=lambda: ping)):
			result = runtime.test_sync_partner_connection(partner.name)

		partner.reload()
		self.assertEqual(result["status"], "error")
		self.assertFalse(result["ok"])
		self.assertEqual(result["message"], "dial timeout")
		self.assertEqual(partner.last_connection_status, "Error")
		self.assertEqual(partner.last_connection_error, "dial timeout")
