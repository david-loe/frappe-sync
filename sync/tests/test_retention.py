from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import frappe

from sync.sync.service import audit, management, retention


class TestRetention(unittest.TestCase):
	def test_expiration_preserves_statuses_cutoff_and_fallback(self):
		cutoff = datetime(2026, 1, 1)
		for status in (
			"Success",
			"Error",
			"Partial Error",
			"Needs Review",
			"Skipped",
			"Running",
			"Queued",
			"Preview",
		):
			for delta in (-1, 0, 1):
				for fallback in (False, True):
					with self.subTest(status=status, delta=delta, fallback=fallback):
						date = cutoff + timedelta(seconds=delta)
						run = SimpleNamespace(
							status=status, finished_at=None if fallback else date, creation=date
						)
						self.assertEqual(
							retention.is_expired(run, cutoff, cutoff),
							status in ("Success", "Error", "Partial Error", "Needs Review", "Skipped")
							and delta <= 0,
						)
		self.assertFalse(retention.is_expired(None, cutoff, cutoff))
		self.assertTrue(
			retention.is_expired(
				SimpleNamespace(status="Error", finished_at=None, creation=None), cutoff, cutoff
			)
		)
		self.assertFalse(
			retention.is_expired(
				SimpleNamespace(status="Error", finished_at=cutoff, creation=None),
				cutoff,
				cutoff - timedelta(days=1),
			)
		)

	def test_invalid_batch_size_fails_before_database_access(self):
		with patch.object(audit, "_get_sync_settings") as settings:
			for value in (0, -1, 10001, 1.5, "1000", True, None):
				with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
					management.cleanup_sync_run_retention(batch_size=value)
			settings.assert_not_called()

	def context(self, stack):
		db = Mock()
		stack.enter_context(patch.object(management, "now_datetime", return_value=datetime(2026, 1, 1)))
		db.get_value.return_value = SimpleNamespace(
			status="Success", finished_at=datetime(2020, 1, 1), creation=None
		)
		stack.enter_context(patch.object(management.frappe, "db", db))
		stack.enter_context(patch.object(management.frappe, "logger"))
		stack.enter_context(
			patch.object(
				audit,
				"_get_sync_settings",
				return_value=SimpleNamespace(
					run_retention_days_success=90,
					run_retention_days_error=365,
				),
			)
		)
		stack.enter_context(patch.object(retention, "expired_run_names", return_value=["RUN-OLD"]))
		return db

	def test_failure_only_counts_committed_batches_and_reports_progress(self):
		for failure in (RuntimeError("failed"), KeyboardInterrupt()):
			with self.subTest(failure=type(failure)), ExitStack() as stack:
				db = self.context(stack)
				stack.enter_context(patch.object(retention, "item_batch", side_effect=[["one"], ["two"]]))
				stack.enter_context(patch.object(retention, "delete_item_batch", side_effect=[None, failure]))
				delete_doc = stack.enter_context(patch.object(management.frappe, "delete_doc"))
				output = stack.enter_context(patch("builtins.print"))
				with self.assertRaises(type(failure)):
					management.cleanup_sync_run_retention(verbose=True)
				db.commit.assert_called_once()
				db.rollback.assert_called_once()
				delete_doc.assert_not_called()
				lines = [call.args[0] for call in output.call_args_list]
				self.assertIn("started", lines[0])
				self.assertIn("failed", lines[-1])
				self.assertIn("deleted_run_items=1", lines[-1])
				self.assertTrue(all(call.kwargs["flush"] for call in output.call_args_list))

	def test_commit_failure_does_not_increment_counts(self):
		with ExitStack() as stack:
			db = self.context(stack)
			db.commit.side_effect = RuntimeError("commit failed")
			stack.enter_context(patch.object(retention, "item_batch", return_value=["one"]))
			stack.enter_context(patch.object(retention, "delete_item_batch"))
			output = stack.enter_context(patch("builtins.print"))
			with self.assertRaisesRegex(RuntimeError, "commit failed"):
				management.cleanup_sync_run_retention(verbose=True)
			self.assertIn("deleted_run_items=0", output.call_args.args[0])

	def test_changed_or_deleted_parent_is_rechecked_under_lock(self):
		for parent in (None, SimpleNamespace(status="Running", finished_at=None, creation=None)):
			with self.subTest(parent=parent), ExitStack() as stack:
				db = self.context(stack)
				db.get_value.return_value = parent
				items = stack.enter_context(patch.object(retention, "item_batch"))
				result = management.cleanup_sync_run_retention()
				self.assertEqual(result["deleted_runs"], 0)
				items.assert_not_called()
				self.assertTrue(db.get_value.call_args.kwargs["for_update"])

	def test_regular_bulk_path_never_loads_or_deletes_individual_documents(self):
		with ExitStack() as stack:
			db = Mock()
			stack.enter_context(patch.object(retention.frappe, "db", db))
			stack.enter_context(patch.object(retention.frappe, "get_all", return_value=[]))
			meta = stack.enter_context(patch.object(retention.frappe, "get_meta"))
			meta.return_value.get_table_fields.return_value = []
			get_doc = stack.enter_context(patch.object(retention.frappe, "get_doc"))
			delete_doc = stack.enter_context(patch.object(retention.frappe, "delete_doc"))
			stack.enter_context(patch.object(retention.frappe, "qb", MagicMock()))
			stack.enter_context(patch.object(retention, "_invalidate_item_caches"))
			for size in (1, 1000):
				db.delete.reset_mock()
				retention.delete_item_batch([f"item-{i}" for i in range(size)])
				self.assertEqual(db.delete.call_count, len(retention.DELETE_REFERENCES) + 1)
			get_doc.assert_not_called()
			delete_doc.assert_not_called()

	def test_external_search_cleanup_is_batched_and_failures_are_logged(self):
		from frappe.search import sqlite_search

		search = Mock()
		search.doc_configs = {"Sync Run Item": {"fields": ["record_key"]}}
		factory = Mock(return_value=search)
		names = [f"item-{i}" for i in range(1000)]
		with (
			patch.object(sqlite_search, "get_search_classes", return_value=[factory]),
			patch.object(retention.frappe, "logger") as logger,
		):
			retention._remove_search_entries(names)
			self.assertEqual(search.sql.call_count, 2)
			parameters = [name for call in search.sql.call_args_list for name in call.args[1]]
			self.assertEqual(parameters, [f"Sync Run Item:{name}" for name in names])
			search.sql.side_effect = RuntimeError("index unavailable")
			retention._remove_search_entries(names)
			logger.return_value.exception.assert_called_once()
			logger.return_value.exception.reset_mock()
			factory.side_effect = RuntimeError("invalid search configuration")
			retention._remove_search_entries(names)
			logger.return_value.exception.assert_called_once()
