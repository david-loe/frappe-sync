from __future__ import annotations

from datetime import datetime
from typing import Any

import frappe
from frappe.utils import now_datetime

from sync.sync.constants import SYNC_DEFINITION, TRIGGER_SCHEDULER
from sync.sync.service import management as management_service
from sync.sync.service import orchestrator as orchestrator_service
from sync.sync.service import scheduling as scheduling_service
from sync.sync.service import time_utils as time_utils_service
from sync.sync.service import values as values_service


class SyncScheduler:
	@staticmethod
	def select_due_definitions(definitions: list[Any], now: datetime | None = None) -> list[Any]:
		now = now or now_datetime()
		result: list[Any] = []
		for definition in definitions:
			if not values_service._as_bool(getattr(definition, "enabled", True)):
				continue
			next_run_at = getattr(definition, "next_run_at", None)
			if isinstance(next_run_at, datetime) and next_run_at <= now:
				result.append(definition)
		return result


def list_due_sync_definitions(now: datetime | None = None) -> list[str]:
	now = now or now_datetime()
	definitions = frappe.get_all(SYNC_DEFINITION, fields=["name", "enabled", "next_run_at", "frequency_cron"])
	due: list[str] = []
	for definition in definitions:
		if not values_service._is_enabled(definition):
			continue

		next_run_at = time_utils_service._parse_datetime(definition.get("next_run_at"))
		if next_run_at and next_run_at <= now:
			due.append(str(definition.get("name")))
			continue

		cron_expr = definition.get("frequency_cron")
		if cron_expr and scheduling_service._is_due_by_cron(definition, str(cron_expr), now):
			due.append(str(definition.get("name")))
	return due


def run_due_sync_definitions(limit: int = 20, queue: bool = True) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for name in list_due_sync_definitions()[:limit]:
		results.append(
			orchestrator_service.enqueue_sync_definition(name, trigger=TRIGGER_SCHEDULER, queue=queue)
		)
	return results


def run_due_sync_definitions_scheduled(limit: int = 20, queue: bool = True) -> list[dict[str, Any]]:
	frappe.set_user("Administrator")
	management_service.recover_stale_runs()
	return run_due_sync_definitions(limit=limit, queue=queue)
