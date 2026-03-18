from __future__ import annotations

from datetime import datetime
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
		self.fields = [SimpleNamespace(fieldname=f, fieldtype="Data") for f in fields]

	def has_field(self, fieldname):
		return fieldname in {field.fieldname for field in self.fields}


class DummyLogger:
	def __init__(self):
		self.messages = []

	def warning(self, message, *args):
		self.messages.append((message, args))


class MutableDoc:
	def __init__(self, name="DOC-1"):
		self.name = name
		self.values = {}
		self.saved = False
		self.inserted = False

	def set(self, key, value):
		self.values[key] = value

	def save(self, **kwargs):
		self.saved = True
		return self

	def insert(self, **kwargs):
		self.inserted = True
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


class LegacyConnector:
	def __init__(self):
		self.calls = []

	def fetch_records(self, *, source=None, query=None, batch_size=100, cursor=None):
		self.calls.append((source, query, batch_size, cursor))
		return [{"name": "LEGACY"}]


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
				return [dict(frappe_field="name", partner_field="name")]
			if childdoctype == "Sync Value Mapping":
				return [dict(frappe_field="state", source_value="open", target_value="1")]
			return []

		mock_children.side_effect = fake_rows

		doc = FakeDoc(
			{
				"name": "SYNC-DELTA",
				"sync_type": "A->B",
				"frequency_cron": "*/5 * * * *",
				"use_last_sync_date": 1,
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"table_name": "tabTask",
				"filter_expression": '[["docstatus","=",0]]',
				"batch_size": 10,
				"create_new": 1,
				"delete_missing": 0,
				"conflict_policy": "newest_wins",
				"frappe_modified_field_rows": [SimpleNamespace(field_name="modified")],
				"partner_modified_field_rows": [SimpleNamespace(field_name="updated_at")],
			}
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])):
			config = runtime._build_definition_config(doc)

		self.assertEqual(config.key_fields, ["name"])
		self.assertEqual(config.mapping, {"name": {"partner_field": "name", "direction": "Both"}})
		self.assertEqual(config.value_mapping, {"state": {"open": "1"}})
		self.assertIsInstance(config.filters, list)
		self.assertEqual(config.use_last_sync_date, True)
		self.assertEqual(config.frappe_modified_fields, ["modified"])
		self.assertEqual(config.partner_modified_fields, ["updated_at"])
		self.assertEqual(config.table_name, "tabTask")
		self.assertIsNone(config.query)

	@patch("sync.sync.service.runtime._get_child_rows_by_options")
	def test_build_definition_config_falls_back_to_legacy_modified_fields(self, mock_children):
		def fake_rows(parent, childdoctype):
			if childdoctype == "Sync Key Field":
				return [dict(frappe_field="name")]
			if childdoctype == "Sync Field Mapping":
				return [dict(frappe_field="name", partner_field="name")]
			return []

		mock_children.side_effect = fake_rows

		doc = FakeDoc(
			{
				"name": "SYNC-LEGACY",
				"sync_type": "A->B",
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"table_name": "  tabTask  ",
				"query": "   ",
				"frappe_modified_fields": " modified \nchanged_on ",
				"partner_modified_fields": " updated_at \n partner_changed ",
			}
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])):
			config = runtime._build_definition_config(doc)

		self.assertEqual(config.table_name, "tabTask")
		self.assertIsNone(config.query)
		self.assertEqual(config.frappe_modified_fields, ["modified", "changed_on"])
		self.assertEqual(config.partner_modified_fields, ["updated_at", "partner_changed"])

	@patch("sync.sync.service.runtime._get_child_rows_by_options", return_value=[])
	def test_build_definition_config_uses_first_mapping_key_as_default_key(self, _mock_children):
		doc = FakeDoc(
			{
				"name": "SYNC-DEFAULT-KEY",
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"field_mapping": {"subject": "title"},
			}
		)

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])):
			config = runtime._build_definition_config(doc)

		self.assertEqual(config.key_fields, ["subject"])
		self.assertEqual(config.mapping, {"subject": {"partner_field": "title", "direction": "Both"}})

	@patch("sync.sync.service.runtime._get_child_rows_by_options", return_value=[])
	def test_build_definition_config_rejects_key_mapping_direction_mismatch(self, _mock_children):
		doc = FakeDoc(
			{
				"name": "SYNC-DIR",
				"sync_type": "A->B",
				"partner": "PARTNER-1",
				"doctype_name": "Task",
				"key_fields": "name",
				"field_mapping": {
					"name": {"partner_field": "id", "direction": "Partner to Frappe"},
				},
			}
		)

		with (
			patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(fields=[])),
			self.assertRaisesRegex(frappe.ValidationError, "name \\(Frappe to Partner\\)"),
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

	def test_fetch_partner_records_retries_legacy_connector_signature(self):
		connector = LegacyConnector()

		records = runtime._fetch_partner_records(
			connector=connector,
			source="tabTask",
			query=None,
			batch_size=5,
			key_fields=["name"],
		)

		self.assertEqual(records, [{"name": "LEGACY"}])
		self.assertEqual(connector.calls, [("tabTask", None, 5, None)])

	def test_get_frappe_source_records_builds_delta_or_filters_from_existing_fields(self):
		config = SimpleNamespace(
			doctype="Task",
			sync_type="A->B",
			mapping={
				"subject": {"partner_field": "title", "direction": "Frappe to Partner"},
				"status": {"partner_field": "state", "direction": "Partner to Frappe"},
			},
			key_fields=["name"],
			frappe_modified_fields=["modified", "changed_on", "missing_field"],
			filters=[["status", "=", "Open"]],
			batch_size=20,
		)
		context = SimpleNamespace(is_delta_sync=True, delta_since=datetime(2026, 3, 17, 10, 0))

		with (
			patch("sync.sync.service.runtime._doctype_has_field", side_effect=lambda doctype, field: field != "missing_field"),
			patch("sync.sync.service.runtime._iter_frappe_record_batches", return_value=iter([[{"name": "TASK-1"}]])) as mock_records,
		):
			out = runtime._get_frappe_source_records(config, context)

		self.assertEqual(out, [{"name": "TASK-1"}])
		self.assertEqual(mock_records.call_args.kwargs["fields"], ["changed_on", "modified", "name", "subject"])
		self.assertEqual(
			mock_records.call_args.kwargs["or_filters"],
			[["modified", ">=", context.delta_since], ["changed_on", ">=", context.delta_since]],
		)

	def test_get_partner_source_records_filters_records_in_delta_mode(self):
		config = SimpleNamespace(
			table_name="tabTask",
			query=None,
			batch_size=50,
			key_fields=["name"],
			partner_modified_fields=["updated_at"],
		)
		context = SimpleNamespace(is_delta_sync=True, delta_since=datetime(2026, 3, 17, 9, 0))
		records = [
			{"name": "TASK-1", "updated_at": datetime(2026, 3, 17, 9, 5)},
			{"name": "TASK-2", "updated_at": datetime(2026, 3, 16, 8, 0)},
		]

		with patch("sync.sync.service.runtime._iter_partner_record_batches", return_value=iter([records])):
			out = runtime._get_partner_source_records(config, object(), context)

		self.assertEqual(out, [records[0]])

	def test_record_changed_since_and_latest_modified_handle_multiple_fields(self):
		record = {
			"modified": "2026-03-17 09:00:00",
			"changed_on": datetime(2026, 3, 17, 11, 0),
		}
		since = datetime(2026, 3, 17, 10, 0)

		self.assertTrue(runtime._record_changed_since(record, ["modified", "changed_on"], since))
		self.assertEqual(runtime._latest_modified(record, ["modified", "changed_on"]), datetime(2026, 3, 17, 11, 0))
		self.assertFalse(runtime._record_changed_since({"modified": "2026-03-17 08:00:00"}, ["modified"], since))

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

	def test_upsert_frappe_record_dry_run_returns_existing_name(self):
		name = runtime._upsert_frappe_record(
			doctype="Task",
			existing_name="TASK-DRY",
			payload={"subject": "Ignored"},
			dry_run=True,
		)

		self.assertEqual(name, "TASK-DRY")

	@patch("sync.sync.service.runtime._create_run_item")
	@patch("sync.sync.service.runtime._create_run_item_change")
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
			sync_type="A->B",
			cron="* * * * *",
			filters=None,
			batch_size=10,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			table_name="tabTask",
			query=None,
			key_fields=["name"],
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
			sync_type="A->B",
			batch_size=10,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			table_name="tabTask",
			query=None,
			key_fields=["name"],
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
			key_fields=["name"],
			mapping={"name": "id", "status": "state"},
			value_mapping={},
			create_new=False,
			delete_missing=True,
			table_name="dbo.SyncTable",
			query=None,
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
				label_direction="A->B",
				full_sync=True,
			)

		self.assertEqual([entry["action"] for entry in logged], ["error", "skipped", "deleted"])
		self.assertEqual(connector.deleted[0]["key_values"], {"id": "TASK-2"})

	def test_sync_frappe_to_partner_enforces_mapping_direction_and_batches_commits(self):
		config = SimpleNamespace(
			name="SYNC-F2P-DIR",
			key_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Both"},
				"status": {"partner_field": "state", "direction": "Partner to Frappe"},
				"subject": {"partner_field": "title", "direction": "Frappe to Partner"},
			},
			value_mapping={},
			create_new=True,
			delete_missing=False,
			table_name="dbo.SyncTable",
			query=None,
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
			patch("sync.sync.service.runtime._create_run_item_change"),
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
				label_direction="A->B",
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

	def test_sync_partner_to_frappe_updates_and_deletes(self):
		config = SimpleNamespace(
			name="SYNC-P2F",
			doctype="Task",
			key_fields=["name"],
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
				label_direction="A<-B",
				full_sync=True,
			)

		mock_upsert.assert_called_once()
		mock_delete.assert_called_once_with("Task", "TASK-2", ignore_permissions=True, force=True)
		self.assertEqual([entry["action"] for entry in logged], ["updated", "deleted"])

	def test_sync_partner_to_frappe_enforces_mapping_direction(self):
		config = SimpleNamespace(
			name="SYNC-P2F-DIR",
			doctype="Task",
			key_fields=["name"],
			mapping={
				"name": {"partner_field": "id", "direction": "Both"},
				"status": {"partner_field": "state", "direction": "Partner to Frappe"},
				"subject": {"partner_field": "title", "direction": "Frappe to Partner"},
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
				partner_records=[{"id": "TASK-1", "state": "1", "title": "Ignored"}],
				frappe_records=[{"name": "TASK-1", "status": "Closed", "subject": "Old"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="A<-B",
				full_sync=False,
			)

		self.assertEqual(
			mock_upsert.call_args.kwargs["payload"],
			{"name": "TASK-1", "status": "Open"},
		)

	def test_sync_bidirectional_resolves_conflicts_and_unsupported_policy(self):
		config = SimpleNamespace(
			name="SYNC-BI",
			doctype="Task",
			key_fields=["name"],
			mapping={"name": "id", "status": "state"},
			value_mapping={},
			conflict_policy="newest_wins",
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
			table_name="tabTask",
			query=None,
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
