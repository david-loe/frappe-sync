"""Bounded database operations for permanent Sync Run Item retention.

This deliberately bypasses item document hooks and recovery copies. Keep the
reference cleanup aligned with frappe.model.delete_doc.delete_dynamic_links.
Run document deletion itself still uses Frappe's normal lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import frappe

from sync.sync.constants import DONE_RUN_STATUSES, RUN_STATUS_SUCCESS, SYNC_RUN, SYNC_RUN_ITEM
from sync.sync.service import time_utils

RUN_PAGE_SIZE = 100

# (table, document type field, document name field). Never interpolate user input
# into these identifiers; all values are bound by Frappe's query builder.
DELETE_REFERENCES = (
	("ToDo", "reference_type", "reference_name"),
	("Email Unsubscribe", "reference_doctype", "reference_name"),
	("DocShare", "share_doctype", "share_name"),
	("Version", "ref_doctype", "docname"),
	("Comment", "reference_doctype", "reference_name"),
	("View Log", "reference_doctype", "reference_name"),
	("Document Follow", "ref_doctype", "ref_docname"),
	("Notification Log", "document_type", "document_name"),
	("Communication Link", "link_doctype", "link_name"),
	("Tag Link", "document_type", "document_name"),
	("__global_search", "doctype", "name"),
	("__Auth", "doctype", "name"),
)
CLEAR_REFERENCES = (
	("Communication", "reference_doctype", "reference_name"),
	("Activity Log", "reference_doctype", "reference_name"),
	("Activity Log", "timeline_doctype", "timeline_name"),
)


def is_expired(run, success_cutoff: datetime, error_cutoff: datetime) -> bool:
	if not run or run.status not in DONE_RUN_STATUSES:
		return False
	cutoff = success_cutoff if run.status == RUN_STATUS_SUCCESS else error_cutoff
	completed_at = time_utils._parse_datetime(run.finished_at) or time_utils._parse_datetime(run.creation)
	return completed_at is None or completed_at <= cutoff


def expired_run_names(success_cutoff: datetime, error_cutoff: datetime) -> Iterator[str]:
	"""Indexable date ranges with keyset paging, including legacy missing dates."""
	table = frappe.qb.DocType(SYNC_RUN)
	for status in sorted(DONE_RUN_STATUSES):
		cutoff = success_cutoff if status == RUN_STATUS_SUCCESS else error_cutoff
		# Separate ranges preserve index use instead of COALESCE(date, date).
		for fieldname in ("finished_at", "creation", None):
			condition = table.status == status
			if fieldname != "finished_at":
				condition &= table.finished_at.isnull()
			if fieldname is None:
				condition &= table.creation.isnull()
			else:
				condition &= table[fieldname] <= cutoff
			last = None
			while True:
				query = (
					frappe.qb.from_(table)
					.select(table.name, table.finished_at, table.creation)
					.where(condition)
				)
				if last:
					cursor = table.name > last.name
					if fieldname:
						cursor = (table[fieldname] > last[fieldname]) | (
							(table[fieldname] == last[fieldname]) & cursor
						)
					query = query.where(cursor)
				if fieldname:
					query = query.orderby(table[fieldname])
				rows = query.orderby(table.name).limit(RUN_PAGE_SIZE).run(as_dict=True)
				if not rows:
					break
				for row in rows:
					yield row.name
				last = rows[-1]
				if len(rows) < RUN_PAGE_SIZE:
					break


def item_batch(run_name: str, batch_size: int) -> list[str]:
	table = frappe.qb.DocType(SYNC_RUN_ITEM)
	rows = (
		frappe.qb.from_(table)
		.select(table.name)
		.where(table.sync_run == run_name)
		.orderby(table.creation, table.name)
		.limit(batch_size)
		.for_update()
		.run()
	)
	return [row[0] for row in rows]


def delete_item_batch(names: list[str]) -> None:
	"""Delete locked items and their references in the caller's transaction."""
	if not names:
		return
	# File owns physical file removal and handling of shared content. Only actual
	# attachments need a document lifecycle; the ordinary item path is all bulk SQL.
	for name in frappe.get_all(
		"File",
		filters={"attached_to_doctype": SYNC_RUN_ITEM, "attached_to_name": ["in", names]},
		pluck="name",
	):
		frappe.delete_doc("File", name, ignore_permissions=True, delete_permanently=True)

	# Workflow Action has child rows which direct parent deletion would leave behind.
	workflow_names = frappe.get_all(
		"Workflow Action",
		filters={"reference_doctype": SYNC_RUN_ITEM, "reference_name": ["in", names]},
		pluck="name",
	)
	if workflow_names:
		for field in frappe.get_meta("Workflow Action").get_table_fields():
			frappe.db.delete(
				field.options, {"parenttype": "Workflow Action", "parent": ["in", workflow_names]}
			)
		frappe.db.delete("Workflow Action", {"name": ["in", workflow_names]})

	# Custom child tables also belong to the removed item, though the standard
	# Sync Run Item schema currently has none.
	for field in frappe.get_meta(SYNC_RUN_ITEM).get_table_fields():
		frappe.db.delete(field.options, {"parenttype": SYNC_RUN_ITEM, "parent": ["in", names]})
	for doctype, type_field, name_field in DELETE_REFERENCES:
		if doctype == "Tag Link" and not frappe.db.table_exists(doctype):
			continue
		frappe.db.delete(doctype, {type_field: SYNC_RUN_ITEM, name_field: ["in", names]})
	for doctype, type_field, name_field in CLEAR_REFERENCES:
		table = frappe.qb.DocType(doctype)
		(
			frappe.qb.update(table)
			.set(table[type_field], None)
			.set(table[name_field], None)
			.where((table[type_field] == SYNC_RUN_ITEM) & table[name_field].isin(names))
		).run()
	frappe.db.delete(SYNC_RUN_ITEM, {"name": ["in", names]})
	_invalidate_item_caches(names)
	frappe.db.after_commit.add(lambda: _remove_search_entries(names))


def _invalidate_item_caches(names: list[str]) -> None:
	from frappe.desk.notifications import clear_doctype_notifications
	from frappe.model.document import get_document_cache_key

	keys = [get_document_cache_key(SYNC_RUN_ITEM, name) for name in names]

	def invalidate():
		frappe.db.value_cache.pop(SYNC_RUN_ITEM, None)
		frappe.cache.delete_value(keys)
		clear_doctype_notifications(SYNC_RUN_ITEM)

	invalidate()
	frappe.db.after_commit.add(invalidate)
	frappe.db.after_rollback.add(invalidate)


def _remove_search_entries(names: list[str]) -> None:
	"""SQLite indexes are outside the SQL transaction; update once per batch."""
	from frappe.search.sqlite_search import get_search_classes

	try:
		search_classes = get_search_classes()
	except Exception:
		frappe.logger("sync.retention", allow_site=True).exception("Sync cleanup search discovery failed")
		return
	for search_class in search_classes:
		try:
			search = search_class()
			if not (search.is_search_enabled() and search.index_exists()):
				continue
			if not search.doc_configs.get(SYNC_RUN_ITEM, {}).get("fields"):
				continue
			# SQLiteSearch.remove_doc uses this table and document ID format.
			# Stay below SQLite's variable limit even on older SQLite builds.
			for offset in range(0, len(names), 500):
				chunk = names[offset : offset + 500]
				placeholders = ",".join("?" for _ in chunk)
				search.sql(
					f"DELETE FROM search_fts WHERE doc_id IN ({placeholders})",
					tuple(f"{SYNC_RUN_ITEM}:{name}" for name in chunk),
					commit=True,
				)
		except Exception:
			# The SQL commit has already succeeded: external index failures must
			# not cause the caller to report a rolled-back batch or incorrect counts.
			frappe.logger("sync.retention", allow_site=True).exception(
				"Sync cleanup search index update failed"
			)
