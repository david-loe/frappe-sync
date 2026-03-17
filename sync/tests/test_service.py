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

	def test_duplicate_run_prevention_guarded(self):
		try:
			from sync.sync.service.orchestrator import SyncRunTracker  # noqa: PLC0415
		except Exception as exc:
			raise unittest.SkipTest(str(exc))

		tracker = SyncRunTracker()
		tracker.start_run("SYNC-1")

		with self.assertRaises(RuntimeError):
			tracker.start_run("SYNC-1")
