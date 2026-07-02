from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import frappe

from sync.sync.service import runtime
from sync.sync.service.connectors import ConnectorPingResult, ConnectorWriteResult


class FakeDoc:
	def __init__(self, values: dict[str, any]):
		self._values = dict(values)
		self.name = self._values.get("name")

	def get(self, key, default=None):
		return self._values.get(key, default)


class DummyMeta:
	def __init__(self, fields):
		self.fields = [
			field if hasattr(field, "fieldname") else SimpleNamespace(fieldname=f, fieldtype="Data")
			for f, field in ((field, field) for field in fields)
		]

	def has_field(self, fieldname):
		return fieldname in {field.fieldname for field in self.fields}


class DummyLogger:
	def __init__(self):
		self.messages = []

	def warning(self, message, *args):
		self.messages.append((message, args))


class MutableDoc:
	def __init__(self, name="DOC-1", docstatus=0):
		self.name = name
		self.docstatus = docstatus
		self.values = {}
		self.saved = False
		self.inserted = False
		self.submitted = False

	def set(self, key, value):
		self.values[key] = value

	def get(self, key, default=None):
		return getattr(self, key, self.values.get(key, default))

	def save(self, **kwargs):
		self.saved = True
		return self

	def insert(self, **kwargs):
		self.inserted = True
		return self

	def submit(self):
		self.submitted = True
		self.docstatus = 1
		return self


class SequenceConnector:
	def __init__(self, pages):
		self.pages = list(pages)
		self.calls = []

	def fetch_records(self, **kwargs):
		self.calls.append(kwargs)
		page = self.pages.pop(0)
		if isinstance(page, Exception):
			raise page
		return page

def _db_stub(**overrides):
	values = {"exists": lambda *args, **kwargs: False, "commit": lambda: None}
	values.update(overrides)
	return SimpleNamespace(**values)


def _runtime_frappe_stub(**overrides):
	values = {"db": _db_stub()}
	values.update(overrides)
	return SimpleNamespace(**values)


class TestRuntimeHelpers(unittest.TestCase):
	@patch("sync.sync.service.runtime._get_child_rows_by_options")
	def test_build_definition_config_parses_filters_and_mappings(self, mock_children):
		def fake_rows(parent, childdoctype):
			if childdoctype == "Sync Key Field":
				return [dict(frappe_field="name")]
			if childdoctype == "Sync Field Mapping":
				return [
					dict(
						frappe_field="name",
						partner_field="name",
						unmapped_action="Use NULL",
						fallback_value="ignored",
					)
				]
			if childdoctype == "Sync Value Mapping":
				return [
					dict(frappe_field="state", source_value="open", target_value="1"),
					dict(
						frappe_field="gender",
						frappe_value_is_null=1,
						frappe_value=None,
						partner_value="2",
					),
				]
			return []

		mock_children.side_effect = fake_rows

		doc = FakeDoc(
			{
				"name": "SYNC-DELTA",
				"sync_type": "Frappe -> Partner",
				"frequency_cron": "*/5 * * * *",
				"use_last_sync_date": 1,
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"table_name": "tabTask",
				"filter_expression": '[["docstatus","=",0]]',
				"batch_size": 10,
				"create_new": 1,
				"update_existing": 0,
				"frappe_after_insert_action": "Submit",
				"frappe_after_update_action": "Invalid Value",
				"delete_missing": 0,
				"conflict_policy": "newest_wins",
				"frappe_modified_field": "modified",
				"frappe_creation_field": "creation",
				"partner_modified_field": "updated_at",
				"partner_creation_field": "created_at",
			}
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])):
			config = runtime._build_definition_config(doc)

		self.assertEqual(config.match_fields, ["name"])
		self.assertEqual(config.mapping, {"name": {"partner_field": "name", "direction": "Frappe -> Partner"}})
		self.assertEqual(config.value_mapping, {"state": {"open": "1"}, "gender": {None: "2"}})
		self.assertEqual(
			config.value_mapping_fallbacks,
			{"name": {"action": "null", "value": None}},
		)
		self.assertIsInstance(config.filters, list)
		self.assertEqual(config.use_last_sync_date, True)
		self.assertEqual(config.frappe_modified_field, "modified")
		self.assertEqual(config.partner_modified_field, "updated_at")
		self.assertEqual(config.frappe_creation_field, "creation")
		self.assertEqual(config.partner_creation_field, "created_at")
		self.assertEqual(config.table_name, "tabTask")
		self.assertFalse(config.update_existing)
		self.assertEqual(config.frappe_after_insert_action, "Submit")
		self.assertEqual(config.frappe_after_update_action, "None")

	def test_child_field_mapping_maps_in_both_directions(self):
		def fake_meta(doctype):
			if doctype == "Task":
				return DummyMeta(
					[
						SimpleNamespace(fieldname="subject", fieldtype="Data"),
						SimpleNamespace(fieldname="items", fieldtype="Table", options="Task Item"),
					]
				)
			if doctype == "Task Item":
				return DummyMeta([SimpleNamespace(fieldname="item_code", fieldtype="Data")])
			return DummyMeta([])

		config = SimpleNamespace(
			doctype="Task",
			mapping={
				"subject": {"partner_field": "title", "direction": "Frappe <-> Partner"},
				"items.1.item_code": {"partner_field": "external_item_code", "direction": "Frappe <-> Partner"},
			},
			value_mapping={},
			value_mapping_fallbacks={},
			frappe_modified_field="modified",
			frappe_creation_field="creation",
			partner_modified_field=None,
			partner_creation_field=None,
			partner_time_zone=None,
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", side_effect=fake_meta):
			context = runtime._build_runtime_mapping_context(config)
			self.assertEqual(
				runtime._map_frappe_to_partner(
					{"subject": "Parent", "items": [{"item_code": "ITEM-1"}]},
					config.mapping,
					{},
					mapping_context=context,
				),
				{"title": "Parent", "external_item_code": "ITEM-1"},
			)
			self.assertEqual(
				runtime._map_partner_to_frappe(
					{"title": "Parent", "external_item_code": "ITEM-1"},
					config.mapping,
					{},
					mapping_context=context,
				),
				{"subject": "Parent", "items": [{"doctype": "Task Item", "item_code": "ITEM-1"}]},
			)

	def test_upsert_frappe_record_merges_configured_child_rows_on_update(self):
		def fake_meta(doctype):
			if doctype == "Task":
				return DummyMeta(
					[
						SimpleNamespace(fieldname="subject", fieldtype="Data"),
						SimpleNamespace(fieldname="items", fieldtype="Table", options="Task Item"),
					]
				)
			return DummyMeta([SimpleNamespace(fieldname="item_code", fieldtype="Data")])

		doc = MutableDoc("TASK-1")
		doc.doctype = "Task"
		doc.items = [{"doctype": "Task Item", "item_code": "OLD", "description": "Keep"}]

		with patch(
			"sync.sync.service.runtime.frappe",
			new=_runtime_frappe_stub(
				get_meta=fake_meta,
				get_doc=lambda doctype, name: doc,
				db=_db_stub(),
			),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name="TASK-1",
				payload={"items": [{"doctype": "Task Item", "item_code": "NEW"}]},
				dry_run=False,
			)

		self.assertEqual(name, "TASK-1")
		self.assertTrue(doc.saved)
		self.assertEqual(doc.values["items"][0]["item_code"], "NEW")
		self.assertEqual(doc.values["items"][0]["description"], "Keep")

	def test_upsert_frappe_record_drops_empty_child_slot_rows_on_insert(self):
		def fake_meta(doctype):
			if doctype == "Task":
				return DummyMeta(
					[
						SimpleNamespace(fieldname="subject", fieldtype="Data"),
						SimpleNamespace(fieldname="items", fieldtype="Table", options="Task Item"),
					]
				)
			return DummyMeta(
				[
					SimpleNamespace(fieldname="item_code", fieldtype="Data"),
					SimpleNamespace(fieldname="quantity", fieldtype="Float"),
				]
			)

		doc = MutableDoc("TASK-1")
		doc.doctype = "Task"

		with patch(
			"sync.sync.service.runtime.frappe",
			new=_runtime_frappe_stub(
				get_meta=fake_meta,
				new_doc=lambda doctype: doc,
				db=_db_stub(),
			),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name=None,
				payload={
					"items": [
						{"doctype": "Task Item", "item_code": "ITEM-1", "quantity": 0},
						{"doctype": "Task Item", "item_code": None, "quantity": None},
						{"doctype": "Task Item"},
					]
				},
				dry_run=False,
			)

		self.assertEqual(name, "TASK-1")
		self.assertTrue(doc.inserted)
		self.assertEqual(doc.values["items"], [{"doctype": "Task Item", "item_code": "ITEM-1", "quantity": 0}])

	@patch("sync.sync.service.runtime._get_child_rows_by_options")
	def test_build_definition_config_uses_dedicated_timestamp_fields(self, mock_children):
		def fake_rows(parent, childdoctype):
			if childdoctype == "Sync Key Field":
				return [dict(frappe_field="name")]
			if childdoctype == "Sync Field Mapping":
				return [dict(frappe_field="name", partner_field="name")]
			return []

		mock_children.side_effect = fake_rows

		doc = FakeDoc(
			{
				"name": "SYNC-TIMESTAMPS",
				"sync_type": "Frappe -> Partner",
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"table_name": "  tabTask  ",
				"read_query": "   ",
				"frappe_modified_field": "changed_on",
				"frappe_creation_field": "creation",
				"partner_modified_field": "partner_changed",
				"partner_creation_field": "created_at",
			}
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])):
			config = runtime._build_definition_config(doc)

		self.assertEqual(config.table_name, "tabTask")
		self.assertIsNone(config.read_query)
		self.assertEqual(config.frappe_modified_field, "changed_on")
		self.assertEqual(config.partner_modified_field, "partner_changed")
		self.assertEqual(config.partner_creation_field, "created_at")

	@patch("sync.sync.service.runtime._get_child_rows_by_options")
	def test_build_definition_config_allows_partner_to_frappe_read_query_without_table_name(self, mock_children):
		def fake_rows(parent, childdoctype):
			if childdoctype == "Sync Key Field":
				return [dict(frappe_field="name")]
			if childdoctype == "Sync Field Mapping":
				return [dict(frappe_field="name", partner_field="id")]
			return []

		mock_children.side_effect = fake_rows

		doc = FakeDoc(
			{
				"name": "SYNC-QUERY",
				"sync_type": "Frappe <- Partner",
				"use_last_sync_date": 0,
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"read_query": "select id from remote_tasks",
			}
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])):
			config = runtime._build_definition_config(doc)

		self.assertIsNone(config.table_name)
		self.assertEqual(config.read_query, "select id from remote_tasks")
		self.assertEqual(config.mapping, {"name": {"partner_field": "id", "direction": "Frappe <- Partner"}})

	@patch("sync.sync.service.runtime._get_child_rows_by_options", return_value=[])
	def test_build_definition_config_uses_first_mapping_key_as_default_key(self, _mock_children):
		doc = FakeDoc(
			{
				"name": "SYNC-DEFAULT-KEY",
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"use_last_sync_date": 0,
				"table_name": "tabTask",
				"field_mapping": {"subject": "title"},
			}
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])):
			config = runtime._build_definition_config(doc)

		self.assertEqual(config.match_fields, ["subject"])
		self.assertEqual(config.mapping, {"subject": {"partner_field": "title", "direction": "Frappe -> Partner"}})

	@patch("sync.sync.service.runtime._get_child_rows_by_options", return_value=[])
	def test_build_definition_config_rejects_key_mapping_direction_mismatch(self, _mock_children):
		doc = FakeDoc(
			{
				"name": "SYNC-DIR",
				"sync_type": "Frappe <-> Partner",
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"table_name": "tabTask",
				"match_fields": "name",
				"field_mapping": {
					"name": {"partner_field": "id", "direction": "Frappe <- Partner"},
				},
			}
		)

		with (
			patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])),
			self.assertRaisesRegex(frappe.ValidationError, "name \\(Frappe -> Partner\\)"),
		):
			runtime._build_definition_config(doc)

	@patch("sync.sync.service.runtime._get_child_rows_by_options", return_value=[])
	def test_build_definition_config_requires_mapping(self, _mock_children):
		doc = FakeDoc({"name": "SYNC-NO-MAP", "partner": "PARTNER-1", "doctype_name": "Task"})

		with self.assertRaises(frappe.ValidationError):
			runtime._build_definition_config(doc)

	def test_parse_filter_expression_handles_valid_and_invalid_json(self):
		logger = DummyLogger()

		with patch("sync.sync.service.runtime.frappe.logger", return_value=logger):
			self.assertEqual(runtime._parse_filter_expression('[["status","=","Open"]]'), [["status", "=", "Open"]])
			self.assertEqual(runtime._parse_filter_expression({"status": "Open"}), {"status": "Open"})
			self.assertIsNone(runtime._parse_filter_expression("not-json"))

		self.assertEqual(len(logger.messages), 1)

	def test_filters_with_frappe_cursor_preserves_existing_modified_filters(self):
		cursor = ("2026-03-17 10:00:00", "TASK-1")
		self.assertEqual(
			runtime._filters_with_frappe_cursor(
				{"status": "Open", "modified": ["<", "2026-03-18 00:00:00"]},
				cursor,
			),
			[
				["status", "=", "Open"],
				["modified", "<", "2026-03-18 00:00:00"],
				["modified", ">=", "2026-03-17 10:00:00"],
			],
		)
		self.assertEqual(
			runtime._filters_with_frappe_cursor([["status", "=", "Open"]], cursor),
			[["status", "=", "Open"], ["modified", ">=", "2026-03-17 10:00:00"]],
		)
		self.assertEqual(
			runtime._filters_with_frappe_cursor(None, cursor),
			[["modified", ">=", "2026-03-17 10:00:00"]],
		)

	def test_build_record_key_consistent(self):
		key1 = runtime._build_record_key({"name": "AAA", "status": "open"})
		key2 = runtime._build_record_key({"status": "open", "name": "AAA"})
		self.assertEqual(key1, key2)

		key3 = runtime._build_record_key({"name": "BBB"})
		self.assertNotEqual(key1, key3)

	def test_sanitize_document_dict_removes_system_fields(self):
		doc = {
			"doctype": "Sync Definition",
			"name": "SYNC",
			"owner": "Administrator",
			"password": "secret",
			"_comments": "hidden",
		}

		class MetaWithoutPassword:
			def __init__(self, fields):
				self.fields = [SimpleNamespace(fieldname=f, fieldtype="Data") for f in fields]

			def has_field(self, fieldname):
				return fieldname in {field.fieldname for field in self.fields}

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=MetaWithoutPassword([])):
			sanitized = runtime._sanitize_document_dict(doc)

		self.assertNotIn("owner", sanitized)
		self.assertNotIn("_comments", sanitized)
		self.assertNotIn("password", sanitized)
		self.assertEqual(sanitized["name"], "SYNC")

	def test_sanitize_document_dict_masks_secrets_and_child_rows(self):
		doc = {
			"doctype": "Sync Partner",
			"name": "PARTNER-1",
			"api_secret": "top-secret",
			"secret_fields": "api_secret",
			"mappings": [{"idx": 1, "field_name": "status", "parent": "PARTNER-1"}],
		}

		parent_meta = SimpleNamespace(
			fields=[
				SimpleNamespace(fieldname="api_secret", fieldtype="Password"),
				SimpleNamespace(fieldname="secret_fields", fieldtype="Small Text"),
				SimpleNamespace(fieldname="mappings", fieldtype="Table", options="Sync Field Mapping"),
			],
			has_field=lambda fieldname: fieldname in {"api_secret", "secret_fields", "mappings"},
		)
		child_meta = SimpleNamespace(
			fields=[SimpleNamespace(fieldname="field_name", fieldtype="Data")],
			has_field=lambda fieldname: fieldname == "field_name",
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", side_effect=[parent_meta, child_meta]):
			sanitized = runtime._sanitize_document_dict(doc, mask_credentials=True)

		self.assertEqual(sanitized["api_secret"], "***")
		self.assertEqual(sanitized["mappings"], [{"doctype": "Sync Field Mapping", "field_name": "status"}])

	def test_upsert_document_returns_existing(self):
		payload = {"doctype": "Sync Definition", "name": "SYNC-EXISTING", "status": "open"}
		with patch(
			"sync.sync.service.runtime.frappe",
			new=_runtime_frappe_stub(
				get_doc=lambda *args, **kwargs: None,
				get_meta=lambda *args, **kwargs: DummyMeta([]),
				db=_db_stub(exists=lambda *args, **kwargs: True),
			),
		):
			name = runtime._upsert_document_from_payload("Sync Definition", payload, overwrite=False)
		self.assertEqual(name, "SYNC-EXISTING")

	def test_upsert_document_overwrite_updates_scalar_and_table_fields(self):
		doc = MutableDoc(name="SYNC-EXISTING")
		meta = SimpleNamespace(
			fields=[
				SimpleNamespace(fieldname="status", fieldtype="Data"),
				SimpleNamespace(fieldname="rows", fieldtype="Table"),
			]
		)

		with (
			patch("sync.sync.service.runtime._normalize_doc_payload", return_value={"name": "SYNC-EXISTING", "status": "Closed", "rows": [{"doctype": "Child"}]}),
			patch(
				"sync.sync.service.runtime.frappe",
				new=_runtime_frappe_stub(
					get_doc=lambda *args, **kwargs: doc,
					get_meta=lambda *args, **kwargs: meta,
					db=_db_stub(exists=lambda *args, **kwargs: True),
				),
			),
		):
			name = runtime._upsert_document_from_payload("Sync Definition", {"name": "SYNC-EXISTING"}, overwrite=True)

		self.assertEqual(name, "SYNC-EXISTING")
		self.assertTrue(doc.saved)
		self.assertEqual(doc.values["status"], "Closed")
		self.assertEqual(doc.values["rows"], [{"doctype": "Child"}])

	def test_normalize_fetch_result_supports_list_dict_and_none(self):
		records, next_cursor = runtime._normalize_fetch_result([{"name": "A"}, {"name": "B"}])
		self.assertEqual(records, [{"name": "A"}, {"name": "B"}])
		self.assertIsNone(next_cursor)

		records, next_cursor = runtime._normalize_fetch_result({"records": [{"name": "C"}], "next_cursor": "2"})
		self.assertEqual(records, [{"name": "C"}])
		self.assertEqual(next_cursor, "2")

		records, next_cursor = runtime._normalize_fetch_result(None)
		self.assertEqual(records, [])
		self.assertIsNone(next_cursor)

	def test_fetch_partner_records_paginates_and_stops_on_empty_page(self):
		connector = SequenceConnector(
			[
				{"records": [{"name": "A"}], "next_cursor": "1"},
				{"records": [{"name": "B"}], "next_cursor": None},
			]
		)

		records = runtime._fetch_partner_records(
			connector=connector,
			source="tabTask",
			query=None,
			batch_size=1,
			key_fields=["name"],
		)

		self.assertEqual(records, [{"name": "A"}, {"name": "B"}])
		self.assertEqual(connector.calls[0]["cursor"], None)
		self.assertEqual(connector.calls[1]["cursor"], "1")

	def test_get_frappe_source_records_builds_delta_or_filters_from_existing_fields(self):
		config = SimpleNamespace(
			doctype="Task",
			sync_type="Frappe -> Partner",
			mapping={
				"subject": {"partner_field": "title", "direction": "Frappe -> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
			},
			match_fields=["name"],
			frappe_modified_fields=["modified", "changed_on", "missing_field"],
			frappe_modified_field="modified",
			frappe_creation_field="creation",
			filters=[["status", "=", "Open"]],
			batch_size=20,
		)
		context = SimpleNamespace(is_delta_sync=True, delta_since=datetime(2026, 3, 17, 10, 0))

		with (
			patch("sync.sync.service.runtime._doctype_fieldnames", return_value={"name", "subject", "status", "modified", "creation"}),
			patch(
				"sync.sync.service.runtime._iter_frappe_record_batches",
				return_value=iter([[{"name": "TASK-1", "modified": "2026-03-17 10:30:00"}]]),
			) as mock_records,
		):
			out = runtime._get_frappe_source_records(config, context)

		self.assertEqual(out, [{"name": "TASK-1", "modified": "2026-03-17 10:30:00"}])
		self.assertEqual(mock_records.call_args.kwargs["fields"], ["creation", "modified", "name", "subject"])
		self.assertEqual(
			mock_records.call_args.kwargs["or_filters"],
			[["modified", ">=", context.delta_since], ["creation", ">=", context.delta_since]],
		)

	def test_get_frappe_source_records_can_skip_delta_filters_for_target_lookup(self):
		config = SimpleNamespace(
			doctype="Task",
			sync_type="Frappe <- Partner",
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
			},
			match_fields=["name"],
			frappe_modified_fields=["modified"],
			filters=[["status", "!=", "Cancelled"]],
			batch_size=20,
		)
		context = SimpleNamespace(is_delta_sync=True, delta_since=datetime(2026, 3, 17, 10, 0))

		with (
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
			patch("sync.sync.service.runtime._iter_frappe_record_batches", return_value=iter([[{"name": "TASK-1"}]])) as mock_records,
		):
			out = runtime._get_frappe_source_records(config, context, apply_delta_filter=False)

		self.assertEqual(out, [{"name": "TASK-1"}])
		self.assertEqual(mock_records.call_args.kwargs["filters"], [["status", "!=", "Cancelled"]])
		self.assertIsNone(mock_records.call_args.kwargs["or_filters"])

	def test_get_partner_source_records_filters_records_in_delta_mode(self):
		config = SimpleNamespace(
			table_name="tabTask",
			read_query=None,
			batch_size=50,
			match_fields=["name"],
			partner_modified_fields=["updated_at"],
			partner_creation_field="created_at",
		)
		context = SimpleNamespace(is_delta_sync=True, delta_since=datetime(2026, 3, 17, 9, 0))
		records = [
			{"name": "TASK-1", "updated_at": datetime(2026, 3, 17, 9, 5)},
			{"name": "TASK-2", "updated_at": datetime(2026, 3, 16, 8, 0)},
		]

		with patch("sync.sync.service.runtime._iter_partner_record_batches", return_value=iter([records])):
			out = runtime._get_partner_source_records(config, object(), context)

		self.assertEqual(out, [records[0]])

	def test_get_partner_source_records_can_skip_delta_filters_for_target_lookup(self):
		config = SimpleNamespace(
			table_name="tabTask",
			read_query=None,
			batch_size=50,
			match_fields=["name"],
			partner_modified_fields=["updated_at"],
			partner_creation_field="created_at",
		)
		context = SimpleNamespace(is_delta_sync=True, delta_since=datetime(2026, 3, 17, 9, 0))
		records = [
			{"name": "TASK-1", "updated_at": datetime(2026, 3, 16, 8, 0)},
			{"name": "TASK-2", "updated_at": datetime(2026, 3, 17, 9, 5)},
		]

		with patch("sync.sync.service.runtime._iter_partner_record_batches", return_value=iter([records])):
			out = runtime._get_partner_source_records(
				config,
				object(),
				context,
				apply_delta_filter=False,
			)

		self.assertEqual(out, records)

	def test_record_changed_since_and_latest_modified_use_creation_fallback(self):
		record = {
			"modified": None,
			"creation": datetime(2026, 3, 17, 11, 0),
		}
		since = datetime(2026, 3, 17, 10, 0)

		self.assertTrue(runtime._record_changed_since(record, "modified", since, creation_field="creation"))
		self.assertEqual(
			runtime._latest_modified(record, "modified", creation_field="creation"),
			datetime(2026, 3, 17, 11, 0),
		)
		self.assertFalse(
			runtime._record_changed_since(
				{"modified": "2026-03-17 09:00:00", "creation": "2026-03-17 11:00:00"},
				"modified",
				since,
				creation_field="creation",
			)
		)
		self.assertFalse(runtime._record_changed_since({"modified": "2026-03-17 08:00:00"}, ["modified"], since))
		with patch("sync.sync.service.runtime._site_time_zone", return_value="Europe/Berlin"):
			self.assertTrue(
				runtime._record_changed_since(
					{"updated_at": "2026-03-17 10:30:00"},
					["updated_at"],
					datetime(2026, 3, 17, 11, 0),
					assumed_time_zone="UTC",
					target_time_zone="Europe/Berlin",
				)
			)
			self.assertEqual(
				runtime._latest_modified(
					{"updated_at": "2026-03-17 10:30:00"},
					["updated_at"],
					assumed_time_zone="UTC",
					target_time_zone="Europe/Berlin",
				),
				datetime(2026, 3, 17, 11, 30),
			)

	@patch("sync.sync.service.runtime._doctype_has_field", return_value=True)
	@patch("sync.sync.service.runtime.frappe.get_doc")
	def test_upsert_frappe_record_updates_existing_document(self, mock_get_doc, _mock_has_field):
		doc = MutableDoc(name="TASK-1")
		mock_get_doc.return_value = doc

		name = runtime._upsert_frappe_record(
			doctype="Task",
			existing_name="TASK-1",
			payload={"name": "TASK-1", "subject": "Updated"},
			dry_run=False,
		)

		self.assertEqual(name, "TASK-1")
		self.assertTrue(doc.saved)
		self.assertEqual(doc.values["subject"], "Updated")
		self.assertNotIn("name", doc.values)

	def test_diff_target_values_normalizes_equivalent_scalar_values(self):
		self.assertEqual(
			runtime._diff_target_values(
				new_record={"count": 9},
				old_record={"count": "9"},
				field_names=["count"],
			),
			[],
		)
		self.assertEqual(
			runtime._diff_target_values(
				new_record={"count": Decimal("9")},
				old_record={"count": "9.0"},
				field_names=["count"],
			),
			[],
		)
		self.assertEqual(
			runtime._diff_target_values(
				new_record={"count": 9},
				old_record={"count": "9.0"},
				field_names=["count"],
			),
			[],
		)
		self.assertEqual(
			runtime._diff_target_values(
				new_record={"enabled": 1},
				old_record={"enabled": True},
				field_names=["enabled"],
			),
			[],
		)
		self.assertEqual(
			runtime._diff_target_values(
				new_record={"changed_at": "2025-12-03 16:33:48"},
				old_record={"changed_at": "2025-12-03T16:33:48"},
				field_names=["changed_at"],
			),
			[],
		)
		self.assertEqual(
			runtime._diff_target_values(
				new_record={"birth_date": "1972-08-21 00:00:00"},
				old_record={"birth_date": date(1972, 8, 21)},
				field_names=["birth_date"],
			),
			[],
		)
		self.assertEqual(
			runtime._diff_target_values(
				new_record={"value": None},
				old_record={"value": ""},
				field_names=["value"],
			),
			[("value", "", None)],
		)

	def test_upsert_frappe_record_writes_mapped_modified_after_existing_save(
		self,
	):
		doc = MutableDoc(name="TASK-1")
		mock_set_value = Mock()
		mapped_modified = datetime(2026, 3, 17, 10, 0)

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					get_doc=Mock(return_value=doc),
					db=_db_stub(set_value=mock_set_value),
				),
			),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name="TASK-1",
				payload={
					"subject": "Updated",
					"modified": mapped_modified,
					"owner": "ignored@example.com",
					"creation": datetime(2026, 3, 1, 8, 0),
					"docstatus": 1,
					"idx": 2,
				},
				dry_run=False,
			)

		self.assertEqual(name, "TASK-1")
		self.assertTrue(doc.saved)
		self.assertEqual(doc.values, {"subject": "Updated"})
		mock_set_value.assert_called_once_with(
			"Task",
			"TASK-1",
			"modified",
			mapped_modified,
			update_modified=False,
		)

	@patch("sync.sync.service.runtime._doctype_has_field", return_value=True)
	@patch("sync.sync.service.runtime.frappe.new_doc")
	def test_upsert_frappe_record_inserts_new_document(self, mock_new_doc, _mock_has_field):
		doc = MutableDoc(name="TASK-NEW")
		mock_new_doc.return_value = doc

		name = runtime._upsert_frappe_record(
			doctype="Task",
			existing_name=None,
			payload={"subject": "Created"},
			dry_run=False,
		)

		self.assertEqual(name, "TASK-NEW")
		self.assertTrue(doc.inserted)
		self.assertEqual(doc.values["subject"], "Created")

	def test_upsert_frappe_record_writes_mapped_modified_after_insert(
		self,
	):
		doc = MutableDoc(name="TASK-NEW")
		mock_set_value = Mock()
		mapped_modified = datetime(2026, 3, 17, 10, 0)

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					new_doc=Mock(return_value=doc),
					db=_db_stub(set_value=mock_set_value),
				),
			),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name=None,
				payload={
					"subject": "Created",
					"modified": mapped_modified,
					"modified_by": "ignored@example.com",
				},
				dry_run=False,
			)

		self.assertEqual(name, "TASK-NEW")
		self.assertTrue(doc.inserted)
		self.assertEqual(doc.values, {"subject": "Created"})
		mock_set_value.assert_called_once_with(
			"Task",
			"TASK-NEW",
			"modified",
			mapped_modified,
			update_modified=False,
		)

	def test_upsert_frappe_record_submits_after_insert_before_restoring_mapped_modified(self):
		doc = MutableDoc(name="TASK-NEW")
		calls = []

		def set_value(*args, **kwargs):
			calls.append(("set_value", args, kwargs))

		original_submit = doc.submit

		def submit():
			calls.append(("submit", (), {}))
			return original_submit()

		doc.submit = submit

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					new_doc=Mock(return_value=doc),
					db=_db_stub(set_value=set_value),
				),
			),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name=None,
				payload={"subject": "Created", "modified": datetime(2026, 3, 17, 10, 0)},
				dry_run=False,
				after_insert_action="Submit",
			)

		self.assertEqual(name, "TASK-NEW")
		self.assertTrue(doc.inserted)
		self.assertTrue(doc.submitted)
		self.assertEqual([call[0] for call in calls], ["submit", "set_value"])

	def test_upsert_frappe_record_without_action_does_not_create_savepoint(self):
		doc = MutableDoc(name="TASK-NEW")
		db = _db_stub(set_value=Mock())
		db.savepoint = Mock()
		db.rollback = Mock()
		db.release_savepoint = Mock()

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					new_doc=Mock(return_value=doc),
					db=db,
				),
			),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name=None,
				payload={"subject": "Created"},
				dry_run=False,
			)

		self.assertEqual(name, "TASK-NEW")
		db.savepoint.assert_not_called()
		db.rollback.assert_not_called()
		db.release_savepoint.assert_not_called()

	def test_upsert_frappe_record_releases_savepoint_after_successful_submit_action(self):
		doc = MutableDoc(name="TASK-NEW")
		db = _db_stub(set_value=Mock())
		db.savepoint = Mock()
		db.rollback = Mock()
		db.release_savepoint = Mock()

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					new_doc=Mock(return_value=doc),
					db=db,
					generate_hash=Mock(return_value="abc123"),
				),
			),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name=None,
				payload={"subject": "Created"},
				dry_run=False,
				after_insert_action="Submit",
			)

		self.assertEqual(name, "TASK-NEW")
		self.assertTrue(doc.submitted)
		db.savepoint.assert_called_once_with("sync_frappe_write_abc123")
		db.rollback.assert_not_called()
		db.release_savepoint.assert_called_once_with("sync_frappe_write_abc123")

	def test_upsert_frappe_record_submits_after_existing_save(self):
		doc = MutableDoc(name="TASK-1")

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					get_doc=Mock(return_value=doc),
					db=_db_stub(set_value=Mock()),
				),
			),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name="TASK-1",
				payload={"subject": "Updated"},
				dry_run=False,
				after_update_action="Submit",
			)

		self.assertEqual(name, "TASK-1")
		self.assertTrue(doc.saved)
		self.assertTrue(doc.submitted)

	def test_upsert_frappe_record_submit_action_is_noop_for_submitted_doc(self):
		doc = MutableDoc(name="TASK-1", docstatus=1)

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					get_doc=Mock(return_value=doc),
					db=_db_stub(set_value=Mock()),
				),
			),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name="TASK-1",
				payload={"subject": "Updated"},
				dry_run=False,
				after_update_action="Submit",
			)

		self.assertEqual(name, "TASK-1")
		self.assertTrue(doc.saved)
		self.assertFalse(doc.submitted)

	def test_upsert_frappe_record_rolls_back_savepoint_when_submit_fails(self):
		class FailingSubmitDoc(MutableDoc):
			def submit(self):
				raise frappe.ValidationError("submit failed")

		doc = FailingSubmitDoc(name="TASK-NEW")
		db = _db_stub(set_value=Mock())
		db.savepoint = Mock()
		db.rollback = Mock()
		db.release_savepoint = Mock()

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					new_doc=Mock(return_value=doc),
					db=db,
					generate_hash=Mock(return_value="abc123"),
					ValidationError=frappe.ValidationError,
				),
			),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
			self.assertRaises(frappe.ValidationError),
		):
			runtime._upsert_frappe_record(
				doctype="Task",
				existing_name=None,
				payload={"subject": "Created"},
				dry_run=False,
				after_insert_action="Submit",
			)

		db.savepoint.assert_called_once_with("sync_frappe_write_abc123")
		db.rollback.assert_called_once_with(save_point="sync_frappe_write_abc123")
		db.release_savepoint.assert_not_called()

	def test_upsert_frappe_record_dry_run_returns_existing_name(self):
		mock_get_doc = Mock()
		mock_new_doc = Mock()
		mock_set_value = Mock()
		with (
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					get_doc=mock_get_doc,
					new_doc=mock_new_doc,
					db=_db_stub(set_value=mock_set_value),
				),
			),
		):
			name = runtime._upsert_frappe_record(
				doctype="Task",
				existing_name="TASK-DRY",
				payload={"subject": "Ignored", "modified": datetime(2026, 3, 17, 10, 0)},
				dry_run=True,
			)

		self.assertEqual(name, "TASK-DRY")
		mock_get_doc.assert_not_called()
		mock_new_doc.assert_not_called()
		mock_set_value.assert_not_called()

	def test_update_existing_disabled_skips_partner_and_frappe_update_helpers(self):
		config = SimpleNamespace(update_existing=0)
		stats = runtime.SyncStats()
		run_doc = SimpleNamespace(name="RUN-1")
		connector = SimpleNamespace(upsert_record=Mock())
		logged = []

		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime._upsert_frappe_record") as mock_upsert_frappe,
		):
			runtime._apply_partner_update(
				run_doc=run_doc,
				config=config,
				connector=connector,
				stats=stats,
				dry_run=False,
				frappe_record={"name": "TASK-1"},
				partner_record={"id": "TASK-1"},
				partner_payload={"status": "Closed"},
				changes=[("status", "Open", "Closed")],
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated partner.",
			)
			runtime._apply_frappe_update(
				run_doc=run_doc,
				config=config,
				stats=stats,
				dry_run=False,
				frappe_record={"name": "TASK-1"},
				partner_record={"id": "TASK-1"},
				frappe_payload={"status": "Closed"},
				changes=[("status", "Open", "Closed")],
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated frappe.",
			)

		connector.upsert_record.assert_not_called()
		mock_upsert_frappe.assert_not_called()
		self.assertEqual([entry["action"] for entry in logged], ["skipped", "skipped"])
		self.assertTrue(all("Update Existing is disabled" in entry["message"] for entry in logged))

	def test_bidirectional_update_existing_disabled_skips_changed_pair_without_conflict(self):
		config = SimpleNamespace(
			name="SYNC-1",
			doctype="Task",
			update_existing=0,
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <-> Partner"},
			},
			value_mapping={},
			value_mapping_fallbacks={},
			conflict_policy="newest_wins",
			frappe_modified_field="modified",
			frappe_creation_field="creation",
			partner_modified_field="updated_at",
			partner_creation_field="created_at",
			timestamp_buffer_ms=100,
		)
		logged = []

		with (
			patch("sync.sync.service.runtime._map_partner_to_frappe", return_value={"name": "TASK-1", "status": "Closed"}),
			patch("sync.sync.service.runtime._map_frappe_to_partner", return_value={"id": "TASK-1", "state": "Open"}),
			patch(
				"sync.sync.service.runtime._diff_target_values",
				side_effect=[
					[("state", "Closed", "Open")],
					[("status", "Open", "Closed")],
				],
			),
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime._flush_pending_run_writes"),
		):
			runtime._sync_bidirectional(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=SimpleNamespace(),
				frappe_records=[{"name": "TASK-1", "status": "Open"}],
				partner_records=[{"id": "TASK-1", "state": "Closed"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				last_successful_sync=None,
				mapping_context=SimpleNamespace(
					frappe_datetime_fields=set(),
					partner_datetime_fields=set(),
					site_time_zone="UTC",
					to_frappe_entries=(("name", "id"), ("status", "state")),
				),
			)

		self.assertEqual(len(logged), 1)
		self.assertEqual(logged[0]["action"], "skipped")
		self.assertEqual(logged[0]["status"], "skipped")
		self.assertIn("Update Existing is disabled", logged[0]["message"])

	def test_manual_resolution_rejects_when_update_existing_disabled(self):
		item_doc = SimpleNamespace(
			name="ITEM-1",
			sync_run="RUN-1",
			status="conflict",
			action="conflict",
			write_direction=None,
			sync_definition="SYNC-1",
		)
		run_doc = SimpleNamespace(name="RUN-1", dry_run=0, sync_definition="SYNC-1")
		definition_doc = SimpleNamespace(name="SYNC-1")

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", side_effect=[item_doc, run_doc, definition_doc]),
			patch("sync.sync.service.runtime._build_definition_config", return_value=SimpleNamespace(update_existing=0)),
			self.assertRaises(frappe.ValidationError),
		):
			runtime.resolve_sync_run_item("ITEM-1", runtime.SYNC_TYPE_PARTNER_TO_FRAPPE)

	@patch("sync.sync.service.runtime._create_run_item")
	@patch("sync.sync.service.runtime._flush_pending_run_writes")
	@patch("sync.sync.service.runtime._update_doc_fields")
	@patch("sync.sync.service.runtime._iter_partner_source_batches", return_value=iter([[]]))
	@patch("sync.sync.service.runtime._iter_frappe_source_batches", return_value=iter([[{"name": "TASK-1", "status": "open"}]]))
	@patch("sync.sync.service.runtime.get_connector_for_partner")
	@patch("sync.sync.service.runtime.frappe.get_doc")
	def test_run_engine_classifies_create_action(
		self, mock_get_doc, mock_get_connector, _mock_frappe_records, _mock_partner_records, *_rest
	):
		mock_get_doc.return_value = SimpleNamespace(partner_type="mssql")

		class DummyConnector:
			def ping(self):
				return ConnectorPingResult(ok=True, message="ok", details={})

			def upsert_record(self, **_kwargs):
				return ConnectorWriteResult(ok=True, message="created")

		mock_get_connector.return_value = DummyConnector()

		config = SimpleNamespace(
			name="SYNC-ENGINE",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe -> Partner",
			cron="* * * * *",
			filters=None,
			batch_size=10,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={"name": "name"},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["modified"],
		)

		result = runtime._run_engine(SimpleNamespace(name="SYNC-ENGINE"), SimpleNamespace(name="RUN-1"), config=config)
		self.assertEqual(result["processed_count"], 1)
		self.assertEqual(result["success_count"], 1)
		self.assertEqual(result["created_count"], 1)
		self.assertEqual(result["error_count"], 0)

	@patch("sync.sync.service.runtime._update_doc_fields")
	@patch("sync.sync.service.runtime._register_and_log")
	@patch("sync.sync.service.runtime._iter_frappe_source_batches")
	@patch("sync.sync.service.runtime._iter_partner_source_batches")
	@patch("sync.sync.service.runtime.get_connector_for_partner")
	@patch("sync.sync.service.runtime.frappe.get_doc")
	def test_run_engine_frappe_delta_uses_unchanged_partner_target_lookup(
		self, mock_get_doc, mock_get_connector, mock_partner_batches, mock_frappe_batches, _mock_log, _mock_update
	):
		mock_get_doc.return_value = SimpleNamespace(partner_type="mssql")

		class DummyConnector:
			def __init__(self):
				self.upsert_calls = []

			def ping(self):
				return ConnectorPingResult(ok=True, message="ok", details={})

			def upsert_record(self, **kwargs):
				self.upsert_calls.append(kwargs)
				return ConnectorWriteResult(ok=True, message="updated", action="updated")

		connector = DummyConnector()
		mock_get_connector.return_value = connector
		changed_frappe = {"name": "TASK-1", "status": "Open", "modified": "2026-03-17 10:00:00"}
		existing_partner = {"id": "TASK-1", "state": "Closed", "updated_at": "2026-03-17 09:00:00"}
		partner_delta_flags = []

		def partner_batches(*_args, **kwargs):
			partner_delta_flags.append(kwargs.get("apply_delta_filter", True))
			return iter([[existing_partner]] if kwargs.get("apply_delta_filter", True) is False else [[]])

		mock_partner_batches.side_effect = partner_batches
		mock_frappe_batches.return_value = iter([[changed_frappe]])

		config = SimpleNamespace(
			name="SYNC-F2P-DELTA",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe -> Partner",
			cron="* * * * *",
			filters=None,
			batch_size=10,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=True,
			timestamp_buffer_ms=0,
			conflict_policy="newest_wins",
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe -> Partner"},
			},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
			partner_creation_field="created_at",
		)

		runtime._run_engine(
			SimpleNamespace(name="SYNC-F2P-DELTA"),
			SimpleNamespace(name="RUN-1"),
			context=runtime.SyncContext(
				config=config,
				dry_run=False,
				last_successful_sync=datetime(2026, 3, 17, 9, 30),
			),
		)

		self.assertEqual(partner_delta_flags, [False])
		self.assertEqual(connector.upsert_calls[0]["key_values"], {"id": "TASK-1"})

	@patch("sync.sync.service.runtime._iter_partner_source_batches", return_value=iter([[]]))
	@patch("sync.sync.service.runtime._iter_frappe_source_batches", return_value=iter([[]]))
	@patch("sync.sync.service.runtime.get_connector_for_partner")
	@patch("sync.sync.service.runtime.frappe.get_doc")
	def test_run_engine_rejects_failed_connector_ping(
		self, mock_get_doc, mock_get_connector, _mock_frappe_records, _mock_partner_records
	):
		mock_get_doc.return_value = SimpleNamespace(partner_type="mssql")
		mock_get_connector.return_value = SimpleNamespace(
			ping=lambda: ConnectorPingResult(ok=False, message="down", details={})
		)

		config = SimpleNamespace(
			name="SYNC-PING",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe -> Partner",
			batch_size=10,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={"name": "name"},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["modified"],
		)

		with self.assertRaises(frappe.ValidationError):
			runtime._run_engine(SimpleNamespace(name="SYNC-PING"), SimpleNamespace(name="RUN-1"), config=config)

	def test_sync_frappe_to_partner_handles_skip_error_and_delete(self):
		config = SimpleNamespace(
			name="SYNC-F2P",
			match_fields=["name"],
			mapping={"name": "id", "status": "state"},
			value_mapping={},
			create_new=False,
			delete_missing=True,
			table_name="dbo.SyncTable",
			read_query=None,
		)
		stats = runtime.SyncStats()
		logged = []

		class DummyConnector:
			def __init__(self):
				self.deleted = []

			def upsert_record(self, **kwargs):
				return ConnectorWriteResult(ok=True, message="ok")

			def delete_record(self, **kwargs):
				self.deleted.append(kwargs)
				return ConnectorWriteResult(ok=True, message="ok")

		connector = DummyConnector()

		with patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)):
			runtime._sync_frappe_to_partner(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=connector,
				frappe_records=[{"status": "open"}, {"name": "TASK-1", "status": "open"}],
				partner_records=[{"id": "TASK-2", "state": "closed"}],
				dry_run=False,
				stats=stats,
				label_direction="Frappe -> Partner",
				full_sync=True,
			)

		self.assertEqual([entry["action"] for entry in logged], ["error", "skipped", "deleted"])
		self.assertEqual(connector.deleted[0]["key_values"], {"id": "TASK-2"})

	def test_sync_frappe_to_partner_enforces_mapping_direction_and_batches_commits(self):
		config = SimpleNamespace(
			name="SYNC-F2P-DIR",
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
				"subject": {"partner_field": "title", "direction": "Frappe -> Partner"},
			},
			value_mapping={},
			create_new=True,
			delete_missing=False,
			table_name="dbo.SyncTable",
			read_query=None,
			batch_size=20,
		)
		upsert_calls = []

		def upsert_record(**kwargs):
			upsert_calls.append(kwargs)
			return ConnectorWriteResult(ok=True, message="ok")

		mock_commit = Mock()
		with (
			patch(
				"sync.sync.service.runtime._create_run_item",
				side_effect=[SimpleNamespace(name="ITEM-1"), SimpleNamespace(name="ITEM-2")],
			),
			patch("sync.sync.service.runtime.frappe", SimpleNamespace(db=SimpleNamespace(commit=mock_commit))),
		):
			runtime._sync_frappe_to_partner(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=SimpleNamespace(upsert_record=upsert_record),
				frappe_records=[
					{"name": "TASK-1", "status": "Open", "subject": "Hello"},
					{"name": "TASK-2", "status": "Closed", "subject": "World"},
				],
				partner_records=[],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe -> Partner",
				full_sync=False,
			)

		self.assertEqual(mock_commit.call_count, 1)
		self.assertEqual(
			[call["record"] for call in upsert_calls],
			[
				{"id": "TASK-1", "title": "Hello"},
				{"id": "TASK-2", "title": "World"},
			],
		)
		self.assertEqual(upsert_calls[0]["mapping"], {"name": "id", "subject": "title"})

	def test_sync_frappe_to_partner_uses_partner_raw_keys_for_normalized_match(self):
		config = SimpleNamespace(
			name="SYNC-F2P-RAW-KEY",
			match_fields=["custom_f_key"],
			mapping={
				"custom_f_key": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe -> Partner"},
			},
			value_mapping={},
			create_new=True,
			delete_missing=False,
			table_name="dbo.SyncTable",
			read_query=None,
			batch_size=20,
		)
		upsert_calls = []

		def upsert_record(**kwargs):
			upsert_calls.append(kwargs)
			return ConnectorWriteResult(ok=True, message="ok", record={"id": "001", "state": "Open"})

		with (
			patch("sync.sync.service.runtime._create_run_item", return_value=SimpleNamespace(name="ITEM-1")),
			patch("sync.sync.service.runtime.frappe", SimpleNamespace(db=SimpleNamespace(commit=lambda: None))),
		):
			runtime._sync_frappe_to_partner(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=SimpleNamespace(upsert_record=upsert_record),
				frappe_records=[{"name": "TASK-1", "custom_f_key": 1, "status": "Open"}],
				partner_records=[{"id": "001", "state": "Closed"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe -> Partner",
				full_sync=False,
			)

		self.assertEqual(upsert_calls[0]["key_values"], {"id": "001"})

	def test_sync_frappe_to_partner_persists_partner_identity_and_partner_link_field(self):
		config = SimpleNamespace(
			name="SYNC-F2P-ID",
			doctype="Task",
			match_fields=["customer_code"],
			mapping={
				"customer_code": {"partner_field": "code", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe -> Partner"},
			},
			value_mapping={},
			create_new=True,
			delete_missing=False,
			table_name="dbo.Person",
			read_query="SELECT * FROM dbo.Person WHERE NR < 90000",
			partner_identity_field="NR",
			frappe_partner_identity_field="partner_nr",
			partner_frappe_identity_field="frappe_name",
			partner_create_id_strategy="max_plus_one",
			partner_create_id_scope_where="NR BETWEEN 1 AND 89999",
			batch_size=20,
		)
		mutable_doc = MutableDoc(name="TASK-1")
		upsert_calls = []

		def upsert_record(**kwargs):
			upsert_calls.append(kwargs)
			return ConnectorWriteResult(
				ok=True,
				message="ok",
				action="created",
				record={"NR": 101, "code": "CUST-1", "state": "Open", "frappe_name": "TASK-1"},
				resolved_key_values={"NR": 101},
			)

		with (
			patch("sync.sync.service.runtime._create_run_item", return_value=SimpleNamespace(name="ITEM-1")),
			patch("sync.sync.service.runtime.frappe", SimpleNamespace(db=SimpleNamespace(commit=lambda: None), get_doc=lambda *args, **kwargs: mutable_doc)),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
		):
			runtime._sync_frappe_to_partner(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=SimpleNamespace(upsert_record=upsert_record),
				frappe_records=[{"name": "TASK-1", "customer_code": "CUST-1", "status": "Open"}],
				partner_records=[],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe -> Partner",
				full_sync=False,
			)

		self.assertEqual(upsert_calls[0]["record"]["frappe_name"], "TASK-1")
		self.assertEqual(upsert_calls[0]["key_values"], {"code": "CUST-1"})
		self.assertEqual(upsert_calls[0]["source"], "dbo.Person")
		self.assertEqual(upsert_calls[0]["create_options"].identity_field, "NR")
		self.assertEqual(mutable_doc.values["partner_nr"], 101)
		self.assertTrue(mutable_doc.saved)

	def test_sync_partner_to_frappe_updates_and_deletes(self):
		config = SimpleNamespace(
			name="SYNC-P2F",
			doctype="Task",
			match_fields=["name"],
			mapping={"name": "id", "status": "state"},
			value_mapping={},
			create_new=True,
			delete_missing=True,
		)
		stats = runtime.SyncStats()
		logged = []

		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-1") as mock_upsert,
			patch("sync.sync.service.runtime.frappe.delete_doc") as mock_delete,
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				partner_records=[{"id": "TASK-1", "state": "open"}],
				frappe_records=[{"name": "TASK-1", "status": "closed"}, {"name": "TASK-2", "status": "stale"}],
				dry_run=False,
				stats=stats,
				label_direction="Frappe <- Partner",
				full_sync=True,
			)

		mock_upsert.assert_called_once()
		mock_delete.assert_called_once_with("Task", "TASK-2", ignore_permissions=True, force=True)
		self.assertEqual([entry["action"] for entry in logged], ["updated", "deleted"])

	def test_sync_partner_to_frappe_creates_new_document_when_no_match_exists(self):
		config = SimpleNamespace(
			name="SYNC-P2F-CREATE",
			doctype="Task",
			match_fields=["name"],
			mapping={"name": "id", "status": "state"},
			value_mapping={},
			create_new=True,
			delete_missing=False,
		)
		logged = []

		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-NEW") as mock_upsert,
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				partner_records=[{"id": "TASK-NEW", "state": "Open"}],
				frappe_records=[],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)

		mock_upsert.assert_called_once_with(
			doctype="Task",
			existing_name=None,
			payload={"name": "TASK-NEW", "status": "Open"},
			dry_run=False,
		)
		self.assertEqual([entry["action"] for entry in logged], ["created"])
		self.assertEqual(logged[0]["frappe_record"], {"name": "TASK-NEW", "status": "Open"})
		self.assertEqual(logged[0]["partner_record"], {"id": "TASK-NEW", "state": "Open"})

	def test_run_engine_partner_delta_uses_unchanged_frappe_target_lookup(self):
		config = SimpleNamespace(
			name="SYNC-P2F-DELTA",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe <- Partner",
			filters=[["status", "!=", "Cancelled"]],
			batch_size=20,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=True,
			timestamp_buffer_ms=0,
			conflict_policy="newest_wins",
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
			},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
			partner_creation_field="created_at",
		)
		existing_frappe = {"name": "TASK-1", "status": "Closed", "modified": "2026-03-17 09:00:00"}
		changed_partner = {"id": "TASK-1", "state": "Open", "updated_at": "2026-03-17 10:00:00"}
		frappe_batch_filters = []

		def frappe_batches(**kwargs):
			frappe_batch_filters.append(kwargs["or_filters"])
			return iter([[existing_frappe]] if kwargs["or_filters"] is None else [[]])

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace(partner_type="mssql")),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=SimpleNamespace(ping=lambda: ConnectorPingResult(ok=True, message="ok", details={}))),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
			patch("sync.sync.service.runtime._iter_frappe_record_batches", side_effect=frappe_batches),
			patch("sync.sync.service.runtime._iter_partner_source_batches", return_value=iter([[changed_partner]])),
			patch("sync.sync.service.runtime._register_and_log"),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-1") as mock_upsert,
		):
			runtime._run_engine(
				SimpleNamespace(name="SYNC-P2F-DELTA"),
				SimpleNamespace(name="RUN-1"),
				context=runtime.SyncContext(
					config=config,
					dry_run=False,
					last_successful_sync=datetime(2026, 3, 17, 9, 30),
				),
			)

		self.assertEqual(frappe_batch_filters, [None])
		mock_upsert.assert_called_once()
		self.assertEqual(mock_upsert.call_args.kwargs["existing_name"], "TASK-1")

	def test_sync_partner_to_frappe_prefers_partner_identity_link_over_match_fields(self):
		config = SimpleNamespace(
			name="SYNC-P2F-ID",
			doctype="Task",
			match_fields=["customer_code"],
			mapping={
				"customer_code": {"partner_field": "code", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
			},
			value_mapping={},
			create_new=True,
			delete_missing=False,
			partner_identity_field="NR",
			frappe_partner_identity_field="partner_nr",
		)

		with (
			patch("sync.sync.service.runtime._register_and_log"),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-LOCAL") as mock_upsert,
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				partner_records=[{"NR": 77, "code": "DIFFERENT", "state": "Open"}],
				frappe_records=[{"name": "TASK-LOCAL", "partner_nr": 77, "customer_code": "OLD", "status": "Closed"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)

		self.assertEqual(mock_upsert.call_args.kwargs["existing_name"], "TASK-LOCAL")
		self.assertEqual(
			mock_upsert.call_args.kwargs["payload"],
			{"name": "TASK-LOCAL", "customer_code": "DIFFERENT", "status": "Open", "partner_nr": 77},
		)

	def test_sync_partner_to_frappe_update_payload_preserves_existing_document_identity(self):
		config = SimpleNamespace(
			name="SYNC-P2F-UPDATE-ID",
			doctype="Task",
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
			},
			value_mapping={},
			create_new=True,
			delete_missing=False,
		)
		logged = []

		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-1") as mock_upsert,
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				partner_records=[{"id": "TASK-1", "state": "Open"}],
				frappe_records=[{"name": "TASK-1", "status": "Closed"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)

		mock_upsert.assert_called_once_with(
			doctype="Task",
			existing_name="TASK-1",
			payload={"name": "TASK-1", "status": "Open"},
			dry_run=False,
		)
		self.assertEqual(logged[0]["written_after_record"], {"name": "TASK-1", "status": "Open"})

	def test_sync_partner_to_frappe_all_matches_updates_all_matching_docs(self):
		config = SimpleNamespace(
			name="SYNC-P2F-ALL",
			doctype="Task",
			match_fields=["customer_code"],
			mapping={
				"customer_code": {"partner_field": "code", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
			},
			value_mapping={},
			create_new=True,
			delete_missing=False,
			one_way_match_mode="all_matches",
		)

		with (
			patch("sync.sync.service.runtime._register_and_log"),
			patch("sync.sync.service.runtime._upsert_frappe_record", side_effect=["TASK-1", "TASK-2"]) as mock_upsert,
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				partner_records=[{"code": "CUST-1", "state": "Open"}],
				frappe_records=[
					{"name": "TASK-1", "customer_code": "CUST-1", "status": "Closed"},
					{"name": "TASK-2", "customer_code": "CUST-1", "status": "Closed"},
				],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)

		self.assertEqual(mock_upsert.call_count, 2)
		self.assertEqual(
			[call.kwargs["existing_name"] for call in mock_upsert.call_args_list],
			["TASK-1", "TASK-2"],
		)

	def test_sync_partner_to_frappe_first_match_updates_only_one_matching_doc(self):
		config = SimpleNamespace(
			name="SYNC-P2F-FIRST",
			doctype="Task",
			match_fields=["customer_code"],
			mapping={
				"customer_code": {"partner_field": "code", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
			},
			value_mapping={},
			create_new=True,
			delete_missing=False,
			one_way_match_mode="first_match",
		)

		with (
			patch("sync.sync.service.runtime._register_and_log"),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-2") as mock_upsert,
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				partner_records=[{"code": "CUST-1", "state": "Open"}],
				frappe_records=[
					{"name": "TASK-1", "customer_code": "CUST-1", "status": "Closed"},
					{"name": "TASK-2", "customer_code": "CUST-1", "status": "Closed"},
				],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)

		mock_upsert.assert_called_once()
		self.assertEqual(mock_upsert.call_args.kwargs["existing_name"], "TASK-2")

	def test_sync_frappe_to_partner_all_matches_updates_all_matching_partner_records(self):
		config = SimpleNamespace(
			name="SYNC-F2P-ALL",
			doctype="Task",
			match_fields=["customer_code"],
			mapping={
				"customer_code": {"partner_field": "code", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe -> Partner"},
			},
			value_mapping={},
			create_new=True,
			delete_missing=False,
			one_way_match_mode="all_matches",
			partner_identity_field="NR",
			table_name="dbo.Person",
			batch_size=20,
		)
		upsert_calls = []

		def upsert_record(**kwargs):
			upsert_calls.append(kwargs)
			return ConnectorWriteResult(
				ok=True,
				message="ok",
				action="updated",
				record={"NR": kwargs["key_values"]["NR"], "code": "CUST-1", "state": "Open"},
				resolved_key_values=dict(kwargs["key_values"]),
			)

		with (
			patch("sync.sync.service.runtime._create_run_item", return_value=SimpleNamespace(name="ITEM-1")),
			patch("sync.sync.service.runtime._flush_pending_run_writes"),
			patch("sync.sync.service.runtime._persist_frappe_partner_identity") as mock_persist_identity,
		):
			runtime._sync_frappe_to_partner(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=SimpleNamespace(upsert_record=upsert_record),
				frappe_records=[{"name": "TASK-1", "customer_code": "CUST-1", "status": "Open"}],
				partner_records=[
					{"NR": 101, "code": "CUST-1", "state": "Closed"},
					{"NR": 202, "code": "CUST-1", "state": "Closed"},
				],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe -> Partner",
				full_sync=False,
			)

		self.assertEqual([call["key_values"] for call in upsert_calls], [{"NR": 101}, {"NR": 202}])
		mock_persist_identity.assert_not_called()

	def test_sync_partner_to_frappe_enforces_mapping_direction(self):
		config = SimpleNamespace(
			name="SYNC-P2F-DIR",
			doctype="Task",
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
				"subject": {"partner_field": "title", "direction": "Frappe -> Partner"},
			},
			value_mapping={"status": {"Open": "1"}},
			create_new=True,
			delete_missing=False,
		)

		with (
			patch("sync.sync.service.runtime._register_and_log"),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-1") as mock_upsert,
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				partner_records=[{"id": "TASK-1", "state": 1, "title": "Ignored"}],
				frappe_records=[{"name": "TASK-1", "status": "Closed", "subject": "Old"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)

		self.assertEqual(
			mock_upsert.call_args.kwargs["payload"],
			{"name": "TASK-1", "status": "Open"},
		)

	def test_pairing_key_normalization_matches_scalar_type_differences(self):
		config = SimpleNamespace(
			name="SYNC-CONTACT-BI",
			doctype="Contact",
			match_fields=["custom_f_key"],
			mapping={"custom_f_key": "id"},
			value_mapping={},
			conflict_policy="newest_wins",
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
			partner_creation_field="created_at",
			table_name="contacts",
			read_query=None,
		)
		frappe_record = {"name": "CONTACT-1", "custom_f_key": "1", "modified": "2026-03-17 10:00:00"}
		partner_record = {"id": 1, "updated_at": "2026-03-17 10:00:00"}

		self.assertEqual(
			runtime._pair_token_from_frappe(config, frappe_record),
			runtime._pair_token_from_partner(config, partner_record),
		)
		self.assertEqual(
			runtime._key_tuple_from_frappe({"custom_f_key": Decimal("1.0")}, ["custom_f_key"]),
			runtime._key_tuple_from_partner({"id": 1}, ["custom_f_key"], {"custom_f_key": "id"}),
		)
		self.assertEqual(
			runtime._key_tuple_from_frappe({"custom_f_key": True}, ["custom_f_key"]),
			runtime._key_tuple_from_partner({"id": 1}, ["custom_f_key"], {"custom_f_key": "id"}),
		)
		self.assertFalse(runtime._valid_key(runtime._key_tuple_from_frappe({"custom_f_key": ""}, ["custom_f_key"])))
		identity_config = SimpleNamespace(
			**{
				**config.__dict__,
				"partner_identity_field": "id",
				"frappe_partner_identity_field": "custom_f_key",
			}
		)
		self.assertEqual(
			runtime._find_existing_partner_records(
				identity_config,
				frappe_record,
				{},
				runtime._build_partner_identity_index(identity_config, [partner_record]),
			),
			[partner_record],
		)
		self.assertEqual(
			runtime._find_existing_frappe_records(
				identity_config,
				partner_record,
				{},
				runtime._build_frappe_partner_identity_index(identity_config, [frappe_record]),
			),
			[frappe_record],
		)

		with (
			patch("sync.sync.service.runtime._sync_frappe_to_partner") as mock_f2p,
			patch("sync.sync.service.runtime._sync_partner_to_frappe") as mock_p2f,
			patch("sync.sync.service.runtime._apply_partner_update") as mock_apply_partner,
			patch("sync.sync.service.runtime._apply_frappe_update") as mock_apply_frappe,
			patch("sync.sync.service.runtime._diff_target_values", return_value=[]),
			patch("sync.sync.service.runtime._site_time_zone", return_value="UTC"),
			patch("sync.sync.service.runtime._register_and_log") as mock_log,
		):
			runtime._sync_bidirectional(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				frappe_records=[frappe_record],
				partner_records=[partner_record],
				dry_run=False,
				stats=runtime.SyncStats(),
				last_successful_sync=datetime(2026, 3, 17, 9, 0),
			)

		mock_f2p.assert_not_called()
		mock_p2f.assert_not_called()
		mock_apply_partner.assert_not_called()
		mock_apply_frappe.assert_not_called()
		self.assertEqual(mock_log.call_args.kwargs["action"], "skipped")

	def test_sync_bidirectional_ignores_timestamp_only_differences(self):
		config = SimpleNamespace(
			name="SYNC-CONTACT-BI",
			doctype="Contact",
			match_fields=["custom_f_key"],
			mapping={
				"custom_f_key": {"partner_field": "id", "direction": "Frappe <-> Partner"},
			},
			value_mapping={},
			conflict_policy="newest_wins",
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
			frappe_modified_field="modified",
			frappe_creation_field="creation",
			partner_modified_field="updated_at",
			partner_creation_field="created_at",
			table_name="contacts",
			read_query=None,
			create_new=True,
			delete_missing=False,
			capture_audit_payloads=1,
		)
		doc = MutableDoc(name="CONTACT-1")
		mock_set_value = Mock()

		with (
			patch("sync.sync.service.runtime._site_time_zone", return_value="UTC"),
			patch(
				"sync.sync.service.runtime.frappe",
				_runtime_frappe_stub(
					get_meta=Mock(return_value=DummyMeta(["custom_f_key"])),
					get_doc=Mock(return_value=doc),
					db=_db_stub(set_value=mock_set_value),
				),
			),
			patch("sync.sync.service.runtime._create_run_item") as mock_create_run_item,
			patch("sync.sync.service.runtime._track_pending_run_writes"),
			patch("sync.sync.service.runtime._flush_pending_run_writes"),
		):
			runtime._sync_bidirectional(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				frappe_records=[
					{"name": "CONTACT-1", "custom_f_key": "9", "modified": "2026-03-17 09:00:00"}
				],
				partner_records=[{"id": 9, "updated_at": "2026-03-17 10:00:00"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				last_successful_sync=datetime(2026, 3, 17, 9, 30),
			)

		self.assertFalse(doc.saved)
		mock_set_value.assert_not_called()
		run_item_kwargs = mock_create_run_item.call_args.kwargs
		self.assertEqual(run_item_kwargs["action"], "skipped")
		self.assertEqual(run_item_kwargs["changes"], None)

	def test_run_engine_bidirectional_partner_delta_uses_unchanged_frappe_target_lookup(self):
		config = SimpleNamespace(
			name="SYNC-BI-DELTA",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe <-> Partner",
			filters=[["status", "!=", "Cancelled"]],
			batch_size=20,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=True,
			timestamp_buffer_ms=0,
			conflict_policy="newest_wins",
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
			},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
			partner_creation_field="created_at",
		)
		existing_frappe = {"name": "TASK-1", "status": "Closed", "modified": "2026-03-17 09:00:00"}
		changed_partner = {"id": "TASK-1", "state": "Open", "updated_at": "2026-03-17 10:00:00"}
		frappe_batch_filters = []

		def frappe_batches(**kwargs):
			frappe_batch_filters.append(kwargs["or_filters"])
			return iter([[existing_frappe]] if kwargs["or_filters"] is None else [[]])

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace(partner_type="mssql")),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=SimpleNamespace(ping=lambda: ConnectorPingResult(ok=True, message="ok", details={}))),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
			patch("sync.sync.service.runtime._iter_frappe_record_batches", side_effect=frappe_batches),
			patch("sync.sync.service.runtime._iter_partner_source_batches", return_value=iter([[changed_partner]])),
			patch("sync.sync.service.runtime._register_and_log"),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-1") as mock_upsert,
		):
			runtime._run_engine(
				SimpleNamespace(name="SYNC-BI-DELTA"),
				SimpleNamespace(name="RUN-1"),
				context=runtime.SyncContext(
					config=config,
					dry_run=False,
					last_successful_sync=datetime(2026, 3, 17, 9, 30),
				),
			)

		self.assertEqual(
			frappe_batch_filters,
			[
				[
					["modified", ">=", datetime(2026, 3, 17, 9, 30)],
					["creation", ">=", datetime(2026, 3, 17, 9, 30)],
				],
				None,
			],
		)
		mock_upsert.assert_called_once()
		self.assertEqual(mock_upsert.call_args.kwargs["existing_name"], "TASK-1")

	def test_run_engine_bidirectional_frappe_delta_uses_unchanged_partner_target_lookup(self):
		config = SimpleNamespace(
			name="SYNC-BI-FRAPPE-DELTA",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe <-> Partner",
			filters=[["status", "!=", "Cancelled"]],
			batch_size=20,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=True,
			timestamp_buffer_ms=0,
			conflict_policy="newest_wins",
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe -> Partner"},
			},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
			partner_creation_field="created_at",
		)
		changed_frappe = {"name": "TASK-1", "status": "Open", "modified": "2026-03-17 10:00:00"}
		existing_partner = {"id": "TASK-1", "state": "Closed", "updated_at": "2026-03-17 09:00:00"}
		upsert_calls = []

		def upsert_record(**kwargs):
			upsert_calls.append(kwargs)
			return ConnectorWriteResult(ok=True, message="updated", action="updated")

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace(partner_type="mssql")),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=SimpleNamespace(
				ping=lambda: ConnectorPingResult(ok=True, message="ok", details={}),
				upsert_record=upsert_record,
			)),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
			patch("sync.sync.service.runtime._iter_frappe_record_batches", side_effect=lambda **_kwargs: iter([[changed_frappe]])),
			patch("sync.sync.service.runtime._iter_partner_record_batches", side_effect=lambda **_kwargs: iter([[existing_partner]])),
			patch("sync.sync.service.runtime._register_and_log"),
		):
			runtime._run_engine(
				SimpleNamespace(name="SYNC-BI-FRAPPE-DELTA"),
				SimpleNamespace(name="RUN-1"),
				context=runtime.SyncContext(
					config=config,
					dry_run=False,
					last_successful_sync=datetime(2026, 3, 17, 9, 30),
				),
			)

		self.assertEqual(upsert_calls[0]["key_values"], {"id": "TASK-1"})

	def test_sync_bidirectional_resolves_conflicts_and_unsupported_policy(self):
		config = SimpleNamespace(
			name="SYNC-BI",
			doctype="Task",
			match_fields=["name"],
			mapping={"name": "id", "status": "state"},
			value_mapping={},
			conflict_policy="newest_wins",
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
			partner_creation_field="created_at",
			table_name="tabTask",
			read_query=None,
		)

		frappe_records = [
			{"name": "TASK-FRAPPE-ONLY", "status": "open", "modified": "2026-03-17 10:00:00"},
			{"name": "TASK-BOTH", "status": "done", "modified": "2026-03-17 09:00:00"},
			{"name": "TASK-CONFLICT", "status": "closed", "modified": "2026-03-17 11:00:00"},
		]
		partner_records = [
			{"id": "TASK-PARTNER-ONLY", "state": "open", "updated_at": "2026-03-17 10:30:00"},
			{"id": "TASK-BOTH", "state": "open", "updated_at": "2026-03-17 10:00:00"},
			{"id": "TASK-CONFLICT", "state": "open", "updated_at": "2026-03-17 11:30:00"},
		]

		with (
			patch("sync.sync.service.runtime._sync_frappe_to_partner") as mock_f2p,
			patch("sync.sync.service.runtime._sync_partner_to_frappe") as mock_p2f,
			patch("sync.sync.service.runtime._apply_partner_update") as mock_apply_partner,
			patch("sync.sync.service.runtime._apply_frappe_update") as mock_apply_frappe,
		):
			runtime._sync_bidirectional(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				frappe_records=frappe_records,
				partner_records=partner_records,
				dry_run=False,
				stats=runtime.SyncStats(),
				last_successful_sync=datetime(2026, 3, 17, 9, 30),
			)

		mock_f2p.assert_called_once()
		mock_p2f.assert_called_once()
		self.assertEqual(mock_apply_frappe.call_count, 2)
		self.assertTrue(all(call.kwargs["action"] == "updated" for call in mock_apply_frappe.call_args_list))
		self.assertTrue(all(call.kwargs["status"] == "success" for call in mock_apply_frappe.call_args_list))
		mock_apply_partner.assert_not_called()

		with patch("sync.sync.service.runtime._register_and_log") as mock_log:
			runtime._sync_bidirectional(
				run_doc=SimpleNamespace(name="RUN-2"),
				config=SimpleNamespace(**{**config.__dict__, "conflict_policy": "manual"}),
				connector=object(),
				frappe_records=[{"name": "TASK-1", "status": "closed", "modified": "2026-03-17 10:00:00"}],
				partner_records=[{"id": "TASK-1", "state": "open", "updated_at": "2026-03-17 10:30:00"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				last_successful_sync=datetime(2026, 3, 17, 9, 0),
			)

		self.assertEqual(mock_log.call_args.kwargs["action"], "conflict")

	def test_effective_modified_uses_creation_only_for_null_modified(self):
		self.assertEqual(
			runtime._effective_modified(
				{"updated_at": None, "created_at": "2026-03-17 10:00:00"},
				modified_field="updated_at",
				creation_field="created_at",
			),
			datetime(2026, 3, 17, 10, 0),
		)
		self.assertIsNone(
			runtime._effective_modified(
				{"updated_at": "not-a-date", "created_at": "2026-03-17 10:00:00"},
				modified_field="updated_at",
				creation_field="created_at",
			)
		)

	def test_timestamp_payload_helpers_apply_create_and_update_rules(self):
		config = SimpleNamespace(
			frappe_modified_field="changed_at",
			frappe_creation_field="creation",
			partner_modified_field="updated_at",
			partner_creation_field="created_at",
			partner_time_zone="UTC",
		)
		context = SimpleNamespace(site_time_zone="UTC")
		frappe_record = {
			"changed_at": None,
			"creation": "2026-03-17 09:00:00",
		}

		created = runtime._with_partner_timestamps(
			config,
			frappe_record,
			{"state": "Open"},
			create=True,
			mapping_context=context,
		)
		updated = runtime._with_partner_timestamps(
			config,
			frappe_record,
			{"state": "Open"},
			create=False,
			mapping_context=context,
		)
		self.assertEqual(created["updated_at"], datetime(2026, 3, 17, 9, 0))
		self.assertEqual(created["created_at"], datetime(2026, 3, 17, 9, 0))
		self.assertNotIn("created_at", updated)

		frappe_payload = runtime._with_frappe_modified_timestamp(
			config,
			{"updated_at": None, "created_at": "2026-03-18 08:00:00"},
			{"status": "Open", "creation": "2000-01-01 00:00:00"},
			mapping_context=context,
		)
		self.assertEqual(frappe_payload["changed_at"], datetime(2026, 3, 18, 8, 0))
		self.assertNotIn("creation", frappe_payload)

	def test_timestamp_payload_helpers_skip_blank_optional_partner_timestamp_fields(self):
		config = SimpleNamespace(
			frappe_modified_field="modified",
			frappe_creation_field="creation",
			partner_modified_field=None,
			partner_creation_field=None,
			partner_time_zone="UTC",
		)
		context = SimpleNamespace(site_time_zone="UTC")

		partner_payload = runtime._with_partner_timestamps(
			config,
			{"modified": "2026-03-17 09:00:00", "creation": "2026-03-16 09:00:00"},
			{"state": "Open"},
			create=True,
			mapping_context=context,
		)
		frappe_payload = runtime._with_frappe_modified_timestamp(
			config,
			{"modified": "2026-03-18 08:00:00", "creation": "2026-03-17 08:00:00"},
			{"status": "Open", "creation": "2000-01-01 00:00:00"},
			mapping_context=context,
		)

		self.assertEqual(partner_payload, {"state": "Open"})
		self.assertEqual(frappe_payload, {"status": "Open"})

	def test_diff_target_values_excludes_dedicated_timestamp_fields(self):
		changes = runtime._diff_target_values(
			new_record={"status": "Open", "updated_at": "2026-03-17 10:00:00"},
			old_record={"status": "Closed", "updated_at": "2026-03-16 10:00:00"},
			field_names=["status", "updated_at"],
			exclude_fields={"updated_at"},
		)

		self.assertEqual(changes, [("status", "Closed", "Open")])

	def test_sync_bidirectional_timestamp_tie_breaker_controls_writes(self):
		base_config = {
			"name": "SYNC-TIE",
			"doctype": "Task",
			"match_fields": ["name"],
			"mapping": {
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <-> Partner"},
			},
			"value_mapping": {},
			"conflict_policy": "newest_wins",
			"frappe_modified_fields": ["modified"],
			"partner_modified_fields": ["updated_at"],
			"frappe_modified_field": "modified",
			"frappe_creation_field": "creation",
			"partner_modified_field": "updated_at",
			"partner_creation_field": "created_at",
			"table_name": "tabTask",
			"read_query": None,
		}
		frappe_record = {"name": "TASK-1", "status": "Closed", "modified": "2026-03-17 10:00:00"}
		partner_record = {"id": "TASK-1", "state": "Open", "updated_at": "2026-03-17 10:00:00"}

		for tie_breaker, expected in (
			(runtime.TIMESTAMP_TIE_MANUAL, "log"),
			(runtime.TIMESTAMP_TIE_FRAPPE_WINS, "partner"),
			(runtime.TIMESTAMP_TIE_PARTNER_WINS, "frappe"),
		):
			with (
				patch("sync.sync.service.runtime._apply_partner_update") as mock_partner,
				patch("sync.sync.service.runtime._apply_frappe_update") as mock_frappe,
				patch("sync.sync.service.runtime._register_and_log") as mock_log,
			):
				runtime._sync_bidirectional(
					run_doc=SimpleNamespace(name="RUN-1"),
					config=SimpleNamespace(**base_config, timestamp_tie_breaker=tie_breaker),
					connector=object(),
					frappe_records=[frappe_record],
					partner_records=[partner_record],
					dry_run=True,
					stats=runtime.SyncStats(),
					last_successful_sync=None,
				)

			self.assertEqual(mock_partner.called, expected == "partner")
			self.assertEqual(mock_frappe.called, expected == "frappe")
			if expected == "log":
				self.assertIn("no write", mock_log.call_args.kwargs["message"])
			if expected in {"partner", "frappe"}:
				applied = mock_partner if expected == "partner" else mock_frappe
				self.assertEqual(applied.call_args.kwargs["action"], "updated")
				self.assertEqual(applied.call_args.kwargs["status"], "success")

	def test_sync_bidirectional_manual_conflict_stores_resolution_payloads(self):
		config = SimpleNamespace(
			name="SYNC-MANUAL",
			doctype="Task",
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <-> Partner"},
			},
			value_mapping={},
			conflict_policy="newest_wins",
			timestamp_tie_breaker=runtime.TIMESTAMP_TIE_MANUAL,
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
			frappe_modified_field="modified",
			frappe_creation_field="creation",
			partner_modified_field="updated_at",
			partner_creation_field="created_at",
			table_name="tabTask",
			read_query=None,
			partner_time_zone="UTC",
		)
		frappe_record = {"name": "TASK-1", "status": "Closed", "modified": "2026-03-17 10:00:00"}
		partner_record = {"id": "TASK-1", "state": "Open", "updated_at": "2026-03-17 10:00:00"}

		with (
			patch("sync.sync.service.runtime._apply_partner_update") as mock_partner,
			patch("sync.sync.service.runtime._apply_frappe_update") as mock_frappe,
			patch("sync.sync.service.runtime._register_and_log") as mock_log,
			patch("sync.sync.service.runtime._site_time_zone", return_value="UTC"),
		):
			runtime._sync_bidirectional(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				frappe_records=[frappe_record],
				partner_records=[partner_record],
				dry_run=True,
				stats=runtime.SyncStats(),
				last_successful_sync=None,
			)

		mock_partner.assert_not_called()
		mock_frappe.assert_not_called()
		self.assertEqual(mock_log.call_args.kwargs["action"], "conflict")
		frappe_payload = mock_log.call_args.kwargs["frappe_resolution_payload"]
		partner_payload = mock_log.call_args.kwargs["partner_resolution_payload"]
		self.assertEqual(frappe_payload["name"], "TASK-1")
		self.assertEqual(frappe_payload["status"], "Open")
		self.assertEqual(partner_payload["id"], "TASK-1")
		self.assertEqual(partner_payload["state"], "Closed")

	def test_resolve_sync_run_item_accepts_partner_changes_to_frappe(self):
		item = SimpleNamespace(
			doctype=runtime.SYNC_RUN_ITEM,
			name="ITEM-1",
			sync_run="RUN-1",
			sync_definition="SYNC-1",
			document_name="TASK-1",
			status="conflict",
			action="conflict",
			write_direction=None,
			frappe_resolution_payload=json.dumps({"name": "TASK-1", "status": "Open"}),
		)
		run = SimpleNamespace(doctype=runtime.SYNC_RUN, name="RUN-1", dry_run=0, sync_definition="SYNC-1")
		definition = SimpleNamespace(doctype=runtime.SYNC_DEFINITION, name="SYNC-1")
		config = SimpleNamespace(doctype="Task")

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", side_effect=[item, run, definition]),
			patch("sync.sync.service.runtime._build_definition_config", return_value=config),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-1") as mock_upsert,
			patch("sync.sync.service.runtime._update_doc_fields") as mock_update,
		):
			response = runtime.resolve_sync_run_item("ITEM-1", runtime.SYNC_TYPE_PARTNER_TO_FRAPPE)

		self.assertEqual(response["status"], "success")
		mock_upsert.assert_called_once_with(
			doctype="Task",
			existing_name="TASK-1",
			payload={"name": "TASK-1", "status": "Open"},
			dry_run=False,
		)
		updated_fields = mock_update.call_args.args[1]
		self.assertEqual(updated_fields["action"], "updated")
		self.assertEqual(updated_fields["status"], "success")
		self.assertEqual(updated_fields["write_direction"], runtime.SYNC_TYPE_PARTNER_TO_FRAPPE)

	def test_resolve_sync_run_item_accepts_frappe_changes_to_partner(self):
		item = SimpleNamespace(
			doctype=runtime.SYNC_RUN_ITEM,
			name="ITEM-1",
			sync_run="RUN-1",
			sync_definition="SYNC-1",
			document_name="TASK-1",
			status="conflict",
			action="conflict",
			write_direction=None,
			partner_resolution_payload=json.dumps({"id": "TASK-1", "state": "Closed"}),
		)
		run = SimpleNamespace(doctype=runtime.SYNC_RUN, name="RUN-1", dry_run=0, sync_definition="SYNC-1")
		definition = SimpleNamespace(doctype=runtime.SYNC_DEFINITION, name="SYNC-1")
		partner = SimpleNamespace(doctype=runtime.SYNC_PARTNER, name="PARTNER-1")
		config = SimpleNamespace(
			name="SYNC-1",
			doctype="Task",
			partner="PARTNER-1",
			table_name="dbo.Task",
			match_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <-> Partner"},
			},
			value_mapping={},
		)
		upsert_calls = []

		def upsert_record(**kwargs):
			upsert_calls.append(kwargs)
			return ConnectorWriteResult(ok=True, message="ok", record={"id": "TASK-1", "state": "Closed"})

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", side_effect=[item, run, definition, partner]),
			patch("sync.sync.service.runtime._build_definition_config", return_value=config),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=SimpleNamespace(upsert_record=upsert_record)),
			patch("sync.sync.service.runtime._update_doc_fields") as mock_update,
		):
			response = runtime.resolve_sync_run_item("ITEM-1", runtime.SYNC_TYPE_FRAPPE_TO_PARTNER)

		self.assertEqual(response["status"], "success")
		self.assertEqual(upsert_calls[0]["record"], {"id": "TASK-1", "state": "Closed"})
		self.assertEqual(upsert_calls[0]["key_values"], {"id": "TASK-1"})
		self.assertEqual(upsert_calls[0]["mapping"], {"name": "id", "status": "state"})
		self.assertEqual(upsert_calls[0]["source"], "dbo.Task")
		updated_fields = mock_update.call_args.args[1]
		self.assertEqual(updated_fields["action"], "updated")
		self.assertEqual(updated_fields["status"], "success")
		self.assertEqual(updated_fields["write_direction"], runtime.SYNC_TYPE_FRAPPE_TO_PARTNER)
		self.assertEqual(json.loads(updated_fields["written_after_payload"]), {"id": "TASK-1", "state": "Closed"})

	def test_json_field_payload_rejects_missing_invalid_and_non_object_payloads(self):
		for value in (None, "", "{broken", "[]", '"string"'):
			with self.subTest(value=value):
				with self.assertRaises(frappe.ValidationError):
					runtime._json_field_payload(SimpleNamespace(partner_resolution_payload=value), "partner_resolution_payload")

	def test_delete_missing_frappe_records_logs_failed_delete_and_continues(self):
		config = SimpleNamespace(
			name="SYNC-DELETE-FRAPPE",
			doctype="Task",
			match_fields=["name"],
			mapping={"name": {"partner_field": "id", "direction": "Frappe <-> Partner"}},
			value_mapping={},
		)
		logged = []

		def delete_doc(_doctype, name, **_kwargs):
			if name == "TASK-2":
				raise RuntimeError("delete failed")

		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime.frappe.delete_doc", side_effect=delete_doc) as mock_delete,
		):
			runtime._delete_missing_frappe_records(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				frappe_records=[
					{"name": "TASK-1"},
					{"name": "TASK-2"},
					{"name": "TASK-3"},
				],
				source_keys=set(),
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
			)

		self.assertEqual([call.args[1] for call in mock_delete.call_args_list], ["TASK-1", "TASK-2", "TASK-3"])
		self.assertEqual([entry["action"] for entry in logged], ["deleted", "error", "deleted"])
		self.assertEqual(logged[1]["frappe_record"]["name"], "TASK-2")
		self.assertEqual(logged[1]["message"], "delete failed")

	def test_create_run_item_records_partner_to_frappe_source_target_and_resolution_payloads(self):
		run_item_meta = DummyMeta(
			[
				"sync_run",
				"sync_definition",
				"action",
				"status",
				"message",
				"write_direction",
				"document_name",
				"record_key",
				"source_id",
				"target_id",
				"change_count",
				"changed_fields",
				"frappe_resolution_payload",
				"partner_resolution_payload",
			]
		)
		inserted_docs = []

		class InsertDoc:
			def __init__(self, payload):
				self.payload = payload
				self.name = "ITEM-1"

			def insert(self, **_kwargs):
				inserted_docs.append(self)
				return self

		with patch(
			"sync.sync.service.runtime.frappe",
			new=_runtime_frappe_stub(
				get_meta=lambda *_args, **_kwargs: run_item_meta,
				get_doc=lambda payload: InsertDoc(payload),
			),
		):
			runtime._create_run_item(
				run_doc=SimpleNamespace(name="RUN-1", sync_type="Frappe <- Partner"),
				config=SimpleNamespace(match_fields=["name"], mapping={"name": "id"}),
				sync_definition_name="SYNC-1",
				action="updated",
				status="success",
				frappe_record={"name": "TASK-1", "status": "Open"},
				partner_record={"id": "PARTNER-1", "state": "Open"},
				message="updated",
				direction="Frappe <- Partner",
				write_direction="Frappe <- Partner",
				frappe_resolution_payload={"name": "TASK-1", "status": "Open"},
				partner_resolution_payload={"id": "PARTNER-1", "state": "Closed"},
				changes=[("status", "Closed", "Open")],
			)

		payload = inserted_docs[0].payload
		self.assertEqual(payload["source_id"], "id=PARTNER-1")
		self.assertEqual(payload["target_id"], "TASK-1")
		self.assertEqual(json.loads(payload["frappe_resolution_payload"]), {"name": "TASK-1", "status": "Open"})
		self.assertEqual(json.loads(payload["partner_resolution_payload"]), {"id": "PARTNER-1", "state": "Closed"})

	def test_get_frappe_keyset_page_filters_repeated_cursor_rows_and_advances_start(self):
		pages = [
			[
				{"modified": "2026-03-17 10:00:00", "name": "TASK-1"},
				{"modified": "2026-03-17 10:00:00", "name": "TASK-2"},
			],
			[
				{"modified": "2026-03-17 10:00:00", "name": "TASK-1"},
				{"modified": "2026-03-17 10:00:00", "name": "TASK-3"},
			],
			[
				{"modified": "2026-03-17 10:00:00", "name": "TASK-4"},
				{"modified": "2026-03-17 10:00:00", "name": "TASK-5"},
			],
		]

		with patch("sync.sync.service.runtime.frappe.get_all", side_effect=pages) as mock_get_all:
			page = runtime._get_frappe_keyset_page(
				"Task",
				fields=["name", "modified"],
				filters=None,
				or_filters=None,
				batch_size=2,
				cursor=("2026-03-17 10:00:00", "TASK-2"),
			)

		self.assertEqual([record["name"] for record in page], ["TASK-3", "TASK-4"])
		self.assertEqual([call.kwargs["limit_start"] for call in mock_get_all.call_args_list], [0, 2, 4])

	def test_merge_partner_runtime_settings_copies_only_valid_time_zone(self):
		base = runtime.SyncDefinitionConfig(
			name="SYNC-TZ",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe -> Partner",
			cron=None,
			filters=None,
			batch_size=10,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			timestamp_buffer_ms=0,
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={"name": {"partner_field": "id", "direction": "Frappe <-> Partner"}},
			value_mapping={},
		)

		valid = runtime._merge_partner_runtime_settings(base, FakeDoc({"time_zone": "Europe/Berlin"}))
		invalid = runtime._merge_partner_runtime_settings(base, FakeDoc({"time_zone": "No/Such_Zone"}))

		self.assertEqual(valid.partner_time_zone, "Europe/Berlin")
		self.assertIsNone(invalid.partner_time_zone)

	def test_sync_bidirectional_timestamp_buffer_treats_nearby_modified_values_as_equal(self):
		base_config = {
			"name": "SYNC-BUFFER",
			"doctype": "Task",
			"match_fields": ["name"],
			"mapping": {
				"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
				"status": {"partner_field": "state", "direction": "Frappe <-> Partner"},
			},
			"value_mapping": {},
			"conflict_policy": "newest_wins",
			"frappe_modified_field": "modified",
			"frappe_creation_field": "creation",
			"partner_modified_field": "updated_at",
			"partner_creation_field": "created_at",
			"timestamp_tie_breaker": runtime.TIMESTAMP_TIE_MANUAL,
			"table_name": "tabTask",
			"read_query": None,
		}
		frappe_record = {"name": "TASK-1", "status": "Closed", "modified": "2026-03-17 10:00:00.000000"}
		partner_record = {"id": "TASK-1", "state": "Open", "updated_at": "2026-03-17 10:00:00.050000"}

		with (
			patch("sync.sync.service.runtime._apply_partner_update") as mock_partner,
			patch("sync.sync.service.runtime._apply_frappe_update") as mock_frappe,
			patch("sync.sync.service.runtime._register_and_log") as mock_log,
		):
			runtime._sync_bidirectional(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=SimpleNamespace(**base_config, timestamp_buffer_ms=100),
				connector=object(),
				frappe_records=[frappe_record],
				partner_records=[partner_record],
				dry_run=True,
				stats=runtime.SyncStats(),
				last_successful_sync=None,
			)

		self.assertFalse(mock_partner.called)
		self.assertFalse(mock_frappe.called)
		self.assertIn("no write", mock_log.call_args.kwargs["message"])

		with (
			patch("sync.sync.service.runtime._apply_partner_update") as mock_partner,
			patch("sync.sync.service.runtime._apply_frappe_update") as mock_frappe,
			patch("sync.sync.service.runtime._register_and_log") as mock_log,
		):
			runtime._sync_bidirectional(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=SimpleNamespace(**base_config, timestamp_buffer_ms=10),
				connector=object(),
				frappe_records=[frappe_record],
				partner_records=[partner_record],
				dry_run=True,
				stats=runtime.SyncStats(),
				last_successful_sync=None,
			)

		self.assertFalse(mock_partner.called)
		self.assertTrue(mock_frappe.called)
		self.assertFalse(mock_log.called)
