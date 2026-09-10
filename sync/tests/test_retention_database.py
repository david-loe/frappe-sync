from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from sync.sync.service import management, retention


def _has_site():
	return bool(getattr(frappe.local, "site", None))


@unittest.skipUnless(_has_site(), "requires Frappe site context")
class TestRetentionDatabase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.prefix = f"retention-test-{frappe.generate_hash(length=10)}"
		self.created = []
		self.addCleanup(self.cleanup_records)
		self.definition = self.record("Sync Definition", title=self.prefix)

	def record(self, doctype, **values):
		doc = frappe.get_doc({"doctype": doctype, "name": f"{self.prefix}-{len(self.created)}", **values})
		doc.db_insert()
		self.created.append((doctype, doc.name))
		return doc

	def run_record(self, status="Success", finished_at=datetime(2000, 1, 1)):
		return self.record(
			"Sync Run",
			sync_definition=self.definition.name,
			sync_type="Frappe -> Partner",
			status=status,
			finished_at=finished_at,
		)

	def item(self, run):
		return self.record(
			"Sync Run Item", sync_run=run.name, record_key="key", action="updated", status="success"
		)

	def cleanup_records(self):
		frappe.db.rollback()
		for doctype, name in reversed(self.created):
			if doctype == "File" and frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, ignore_permissions=True, delete_permanently=True)
			else:
				frappe.db.delete(doctype, {"name": name})
			frappe.db.delete("Deleted Document", {"deleted_doctype": doctype, "deleted_name": name})
		frappe.db.commit()

	def cleanup_runs(self, runs, **kwargs):
		# Never prune unrelated data on the developer's site during a test.
		with patch.object(retention, "expired_run_names", side_effect=lambda *_: iter(runs)):
			return management.cleanup_sync_run_retention(
				retention_days_success=3, retention_days_error=3, **kwargs
			)

	def test_bulk_cleanup_removes_references_and_preserves_other_documents(self):
		run = self.run_record()
		items = [self.item(run) for _ in range(5)]
		fresh = self.run_record(finished_at=datetime.now())
		fresh_item = self.item(fresh)
		frappe.db.set_value("Sync Definition", self.definition.name, "last_run", run.name)
		version = self.record("Version", ref_doctype="Sync Run Item", docname=items[0].name, data="{}")
		comment = self.record(
			"Comment", reference_doctype="Sync Run Item", reference_name=items[0].name, content="test"
		)
		share = self.record(
			"DocShare", share_doctype="Sync Run Item", share_name=items[0].name, user="Administrator"
		)
		communication = self.record(
			"Communication", reference_doctype="Sync Run Item", reference_name=items[0].name
		)
		other_comment = self.record(
			"Comment", reference_doctype="Sync Run Item", reference_name=fresh_item.name
		)
		workflow = self.record(
			"Workflow Action", reference_doctype="Sync Run Item", reference_name=items[0].name
		)
		role = self.record(
			"Workflow Action Permitted Role",
			parenttype="Workflow Action",
			parent=workflow.name,
			parentfield="permitted_roles",
			role="System Manager",
		)
		frappe.db.commit()
		result = self.cleanup_runs([run.name, fresh.name], batch_size=2)
		self.assertEqual((result["deleted_runs"], result["deleted_run_items"]), (1, 5))
		for doc in [run, *items, version, comment, share, workflow, role]:
			self.assertFalse(frappe.db.exists(doc.doctype, doc.name), (doc.doctype, doc.name))
			self.assertFalse(
				frappe.db.exists(
					"Deleted Document", {"deleted_doctype": doc.doctype, "deleted_name": doc.name}
				)
			)
		self.assertFalse(frappe.db.get_value("Sync Definition", self.definition.name, "last_run"))
		self.assertFalse(frappe.db.get_value("Communication", communication.name, "reference_name"))
		for doc in (fresh, fresh_item, other_comment, communication):
			self.assertTrue(frappe.db.exists(doc.doctype, doc.name))

	def test_attachments_use_file_lifecycle(self):
		run = self.run_record()
		item = self.item(run)
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{self.prefix}.txt",
				"content": self.prefix,
				"attached_to_doctype": "Sync Run Item",
				"attached_to_name": item.name,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		self.created.append(("File", file.name))
		path = Path(file.get_full_path())
		self.assertTrue(path.exists())
		frappe.db.commit()
		self.cleanup_runs([run.name])
		self.assertFalse(frappe.db.exists("File", file.name))
		self.assertFalse(path.exists())

	def test_committed_batch_survives_failure_and_restart_finishes(self):
		run = self.run_record()
		for _ in range(5):
			self.item(run)
		frappe.db.commit()
		delete = retention.delete_item_batch
		calls = 0

		def fail_second(names):
			nonlocal calls
			calls += 1
			delete(names)
			if calls == 2:
				raise RuntimeError("simulated interruption")

		with (
			patch.object(retention, "delete_item_batch", side_effect=fail_second),
			self.assertRaisesRegex(RuntimeError, "simulated interruption"),
		):
			self.cleanup_runs([run.name], batch_size=2)
		self.assertEqual(frappe.db.count("Sync Run Item", {"sync_run": run.name}), 3)
		self.assertTrue(frappe.db.exists("Sync Run", run.name))
		result = self.cleanup_runs([run.name], batch_size=2)
		self.assertEqual((result["deleted_runs"], result["deleted_run_items"]), (1, 3))
		self.assertEqual(self.cleanup_runs([run.name])["deleted_runs"], 0)

	def test_candidate_paging_and_date_fallback(self):
		cutoff = datetime(2000, 1, 1)
		expected = {self.run_record().name for _ in range(5)}
		fallback = self.run_record(finished_at=None)
		frappe.db.set_value("Sync Run", fallback.name, "creation", cutoff)
		expected.add(fallback.name)
		missing = self.run_record(finished_at=None)
		frappe.db.set_value("Sync Run", missing.name, "creation", None)
		expected.add(missing.name)
		excluded = {self.run_record(status=status).name for status in ("Queued", "Running", "Preview")}
		excluded.add(self.run_record(finished_at=cutoff + timedelta(seconds=1)).name)
		error = self.run_record(status="Error")
		excluded.add(error.name)
		with patch.object(retention, "RUN_PAGE_SIZE", 2):
			actual = list(retention.expired_run_names(cutoff, cutoff - timedelta(days=1)))
		self.assertTrue(expected.issubset(actual))
		self.assertTrue(excluded.isdisjoint(actual))
		self.assertEqual(len(actual), len(set(actual)))

	def test_two_connections_do_not_double_count_deletions(self):
		run = self.run_record()
		for _ in range(7):
			self.item(run)
		frappe.db.commit()
		barrier = Barrier(2, timeout=20)
		site = frappe.local.site
		sites_path = str(Path(frappe.local.sites_path).resolve())

		def candidates(*_):
			barrier.wait()
			yield run.name

		def worker():
			try:
				frappe.init(site, sites_path=sites_path)
				frappe.connect()
				frappe.set_user("Administrator")
				frappe.flags.in_test = True
				return management.cleanup_sync_run_retention(batch_size=2)
			finally:
				frappe.destroy()

		with (
			patch.object(retention, "expired_run_names", side_effect=candidates),
			ThreadPoolExecutor(max_workers=2) as pool,
		):
			results = list(pool.map(lambda _: worker(), range(2)))
		self.assertEqual(sum(result["deleted_runs"] for result in results), 1)
		self.assertEqual(sum(result["deleted_run_items"] for result in results), 7)
		frappe.db.rollback()
		self.assertFalse(frappe.db.exists("Sync Run", run.name))
