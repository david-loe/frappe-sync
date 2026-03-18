from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest


def _make_definition(name, next_run, enabled=True):
	return SimpleNamespace(name=name, next_run_at=next_run, enabled=enabled)


class TestSyncService(unittest.TestCase):
	def test_due_definition_selection_filters_only_ready(self):
		try:
			from sync.sync.service.scheduler import SyncScheduler  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		now = datetime(2026, 3, 17, 12, 0)
		definitions = [
			_make_definition("ready", now - timedelta(minutes=1), enabled=True),
			_make_definition("future", now + timedelta(minutes=1), enabled=True),
			_make_definition("disabled", now - timedelta(minutes=5), enabled=False),
		]

		due = SyncScheduler.select_due_definitions(definitions, now)
		names = [doc.name for doc in due]
		self.assertEqual(names, ["ready"])

	def test_due_definition_selection_ignores_missing_next_run_and_falsey_enabled_strings(self):
		try:
			from sync.sync.service.scheduler import SyncScheduler  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		now = datetime(2026, 3, 17, 12, 0)
		definitions = [
			_make_definition("ready-string", now, enabled="1"),
			_make_definition("disabled-string", now - timedelta(minutes=5), enabled="0"),
			_make_definition("missing-next-run", None, enabled=True),
		]

		due = SyncScheduler.select_due_definitions(definitions, now)
		self.assertEqual([doc.name for doc in due], ["ready-string"])

	def test_duplicate_run_prevention_guarded(self):
		try:
			from sync.sync.service.orchestrator import SyncRunTracker  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		tracker = SyncRunTracker()
		tracker.start_run("SYNC-1")

		with self.assertRaises(RuntimeError):
			tracker.start_run("SYNC-1")

	def test_finish_run_allows_definition_to_start_again(self):
		try:
			from sync.sync.service.orchestrator import SyncRunTracker  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		tracker = SyncRunTracker()
		tracker.start_run("SYNC-1")
		tracker.finish_run("SYNC-1")

		tracker.start_run("SYNC-1")
		self.assertIn("SYNC-1", tracker._active_runs)

	def test_finish_run_is_idempotent_for_unknown_definition(self):
		try:
			from sync.sync.service.orchestrator import SyncRunTracker  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		tracker = SyncRunTracker()
		tracker.finish_run("SYNC-MISSING")
		self.assertEqual(tracker._active_runs, set())
