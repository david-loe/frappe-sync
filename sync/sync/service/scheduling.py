from __future__ import annotations

from datetime import datetime
from typing import Any

import frappe
from frappe.utils import now_datetime

from sync.sync.constants import DONE_RUN_STATUSES, SYNC_RUN

try:
	from croniter import croniter
except Exception:  # pragma: no cover - optional runtime dependency
	croniter = None

from sync.sync.service import time_utils as time_utils_service
from sync.sync.service import values as values_service


def _set_next_run_at(sync_definition_doc: Any, cron_expr: str | None, *, commit: bool = True):
	if not cron_expr or not croniter:
		return
	if not frappe.get_meta(sync_definition_doc.doctype).has_field("next_run_at"):
		return
	try:
		next_run = croniter(cron_expr, now_datetime()).get_next(datetime)
	except Exception:
		frappe.logger("sync").warning(
			"Invalid cron expression for %s: %s", values_service._doc_name(sync_definition_doc), cron_expr
		)
		return
	sync_definition_doc.db_set("next_run_at", next_run, update_modified=False)
	if commit:
		frappe.db.commit()


def _is_due_by_cron(sync_definition_doc: Any, cron_expr: str, now: datetime) -> bool:
	if not croniter:
		return False
	try:
		previous_tick = croniter(cron_expr, now).get_prev(datetime)
	except Exception:
		frappe.logger("sync").warning(
			"Invalid cron expression for %s: %s", values_service._doc_name(sync_definition_doc), cron_expr
		)
		return False

	run_meta = frappe.get_meta(SYNC_RUN)
	if not run_meta.has_field("sync_definition"):
		return False

	definition_name = values_service._doc_name(sync_definition_doc)
	if not definition_name:
		return False
	filters = {"sync_definition": definition_name}
	if run_meta.has_field("status"):
		filters["status"] = ["in", sorted(DONE_RUN_STATUSES)]
	fields = ["finished_at"] if run_meta.has_field("finished_at") else ["modified"]
	last_runs = frappe.get_all(
		SYNC_RUN, filters=filters, fields=fields, order_by=f"{fields[0]} desc", limit=1
	)
	if not last_runs:
		return True
	last_run = time_utils_service._parse_datetime(last_runs[0].get(fields[0]))
	if not last_run:
		return True
	return last_run < previous_tick
