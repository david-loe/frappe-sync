"""Bounded progress logging for long running processing phases."""

import logging
from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic

logger = logging.getLogger(__name__)


class Progress:
	def __init__(self, definition):
		self.definition = definition
		self.started = monotonic()
		self.phases = {}
		self.timings = {}
		self.current = ("starting", 0)

	def report(self, phase, count=0, *, force=False):
		self.current = (phase, count)
		now = monotonic()
		started, last = self.phases.setdefault(phase, (now, 0))
		if force or now - last >= 30:
			elapsed = now - started
			logger.info(
				"%s | %s | count=%s elapsed=%.1fs rate=%.1f/s total=%.1fs",
				self.definition,
				phase,
				count,
				elapsed,
				count / elapsed if elapsed else 0,
				now - self.started,
			)
			self.phases[phase] = (started, now)

	@contextmanager
	def heartbeat(self):
		"""Keep CLI progress visible even while a database call blocks."""
		if not logger.isEnabledFor(logging.INFO):
			yield
			return
		stop = Event()

		def report_wait():
			while not stop.wait(30):
				self.report(*self.current, force=True)

		thread = Thread(target=report_wait, name="sync-progress", daemon=True)
		thread.start()
		try:
			yield
		finally:
			stop.set()
			thread.join(timeout=1)

	@contextmanager
	def measure(self, phase):
		started = monotonic()
		try:
			yield
		finally:
			self.timings[phase] = self.timings.get(phase, 0) + monotonic() - started

	def finish(self):
		logger.info(
			"%s | timings=%s total=%.1fs",
			self.definition,
			{phase: round(seconds, 3) for phase, seconds in self.timings.items()},
			monotonic() - self.started,
		)
