from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from sync.sync.service import runtime
from sync.sync.service.connectors import ConnectorWriteResult


class DummyDoc:
	def __init__(self, payload=None, *, name=None, doctype="Generic"):
		self.payload = dict(payload or {})
		self.name = name or self.payload.get("name", "DOC-1")
		self.doctype = doctype if doctype != "Generic" else self.payload.get("doctype", doctype)
		self.db_set_calls = []
		self.inserted = False

	def get(self, key, default=None):
		return self.payload.get(key, default)

	def insert(self, **kwargs):
		self.inserted = True
		return self

	def db_set(self, fieldname, value, update_modified=False):
		self.db_set_calls.append((fieldname, value, update_modified))
		self.payload[fieldname] = value

	def as_dict(self):
		return dict(self.payload)


class DummyMeta:
	def __init__(self, fields):
		self.fields = fields

	def has_field(self, fieldname):
		return fieldname in {field.fieldname for field in self.fields}


def _field(fieldname: str, fieldtype: str = "Data", options: str | None = None):
	return SimpleNamespace(fieldname=fieldname, fieldtype=fieldtype, options=options)


class DummyCroniter:
	def __init__(self, previous=None, next_value=None, error=False):
		self.previous = previous
		self.next_value = next_value
		self.error = error

	def __call__(self, expr, current):
		if self.error:
			raise RuntimeError("bad cron")
		return SimpleNamespace(get_prev=lambda *_args: self.previous, get_next=lambda *_args: self.next_value)


def _db_stub(**overrides):
	values = {"exists": lambda *args, **kwargs: False, "commit": lambda: None}
	values.update(overrides)
	return SimpleNamespace(**values)


def _runtime_frappe_stub(**overrides):
	values = {"db": _db_stub()}
	values.update(overrides)
	return SimpleNamespace(**values)


class TestRuntimeManagement(unittest.TestCase):
	def test_sync_stats_registers_actions_and_statuses(self):
		stats = runtime.SyncStats()
		stats.register("created", "success")
		stats.register("conflict", "conflict")
		stats.register("skipped", "skipped")
		stats.register("updated", "error")

		self.assertEqual(
			stats.as_dict(),
			{
				"processed_count": 4,
				"success_count": 1,
				"created_count": 1,
				"updated_count": 1,
				"deleted_count": 0,
				"skipped_count": 1,
				"conflict_count": 1,
				"error_count": 1,
			},
		)

	def test_sync_context_exposes_delta_and_full_sync_state(self):
		config = runtime.SyncDefinitionConfig(
			name="SYNC-1",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe -> Partner",
			cron="*/5 * * * *",
			filters=None,
			batch_size=100,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=True,
			conflict_policy="newest_wins",
			timestamp_buffer_ms=30,
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={"name": "id"},
			value_mapping={},
			frappe_modified_field="modified",
			frappe_creation_field="creation",
			partner_modified_field="updated_at",
			partner_creation_field="created_at",
		)
		last_sync = datetime(2026, 3, 17, 12, 0, 0)
		context = runtime.SyncContext(config=config, dry_run=False, last_successful_sync=last_sync)

		self.assertTrue(context.is_delta_sync)
		self.assertEqual(context.delta_since, datetime(2026, 3, 17, 12, 0))
		self.assertFalse(context.is_full_sync)

	def test_run_engine_partner_to_frappe_full_sync_passes_complete_partner_load_once(self):
		config = runtime.SyncDefinitionConfig(
			name="SYNC-P2F-FULL",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe <- Partner",
			cron=None,
			filters=None,
			batch_size=2,
			create_new=True,
			delete_missing=True,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			timestamp_buffer_ms=0,
			table_name="dbo.Task",
			read_query=None,
			match_fields=["name"],
			mapping={"name": {"partner_field": "id", "direction": "Frappe <-> Partner"}},
			value_mapping={},
			frappe_modified_field="modified",
			frappe_creation_field="creation",
			partner_modified_field="updated_at",
			partner_creation_field="created_at",
		)
		frappe_records = [{"name": "TASK-1"}, {"name": "TASK-STALE"}]
		partner_batches = [[{"id": "TASK-1"}], [{"id": "TASK-2"}, {"id": "TASK-3"}]]
		mapping_context = SimpleNamespace(connector_mapping={"name": "id"})

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace(name="PARTNER-1")),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=SimpleNamespace(ping=lambda: SimpleNamespace(ok=True, message="ok"))),
			patch("sync.sync.service.runtime._build_runtime_mapping_context", return_value=mapping_context),
			patch("sync.sync.service.runtime._get_frappe_source_records", return_value=frappe_records),
			patch("sync.sync.service.runtime._iter_partner_source_batches", return_value=iter(partner_batches)),
			patch("sync.sync.service.runtime._sync_partner_to_frappe") as mock_sync,
		):
			runtime._run_engine(
				SimpleNamespace(name="SYNC-P2F-FULL"),
				SimpleNamespace(name="RUN-1"),
				context=runtime.SyncContext(config=config, dry_run=False, last_successful_sync=None),
			)

		mock_sync.assert_called_once()
		call = mock_sync.call_args.kwargs
		self.assertEqual(call["partner_records"], [{"id": "TASK-1"}, {"id": "TASK-2"}, {"id": "TASK-3"}])
		self.assertEqual(call["frappe_records"], frappe_records)
		self.assertIsInstance(call["frappe_lookup"], runtime.FrappeMatchLookup)
		self.assertEqual(call["frappe_lookup"].latest_by_key[("TASK-STALE",)]["name"], "TASK-STALE")
		self.assertTrue(call["full_sync"])

	def test_list_due_sync_definitions_uses_next_run_and_cron(self):
		now = datetime(2026, 3, 17, 12, 0, 0)
		meta = DummyMeta([_field("enabled"), _field("next_run_at"), _field("frequency_cron")])

		with (
			patch("sync.sync.service.runtime.frappe.get_meta", return_value=meta),
			patch(
				"sync.sync.service.runtime.frappe.get_all",
				return_value=[
					{"name": "READY", "enabled": 1, "next_run_at": now},
					{"name": "CRON", "enabled": 1, "frequency_cron": "*/5 * * * *"},
					{"name": "OFF", "enabled": 0},
				],
			),
			patch("sync.sync.service.runtime.frappe.get_doc", side_effect=AssertionError("get_doc should not be used")),
			patch("sync.sync.service.runtime._is_due_by_cron", side_effect=[True]),
		):
			self.assertEqual(runtime.list_due_sync_definitions(now=now), ["READY", "CRON"])

	def test_run_due_sync_definitions_limits_and_delegates(self):
		with (
			patch("sync.sync.service.runtime.list_due_sync_definitions", return_value=["A", "B", "C"]),
			patch("sync.sync.service.runtime.enqueue_sync_definition", side_effect=lambda name, **kwargs: {"sync_definition": name, **kwargs}) as mock_enqueue,
		):
			result = runtime.run_due_sync_definitions(limit=2, queue=False)

		self.assertEqual(
			result,
			[
				{"sync_definition": "A", "trigger": "scheduler", "queue": False},
				{"sync_definition": "B", "trigger": "scheduler", "queue": False},
			],
		)
		self.assertEqual(mock_enqueue.call_count, 2)

	def test_recover_stale_runs_marks_old_active_runs_and_updates_definition(self):
		now = datetime(2026, 3, 17, 12, 0, 0)
		run_docs = {
			"RUN-Q": DummyDoc({"doctype": "Sync Run"}, name="RUN-Q", doctype="Sync Run"),
			"RUN-R": DummyDoc({"doctype": "Sync Run"}, name="RUN-R", doctype="Sync Run"),
		}
		definition_doc = DummyDoc({"doctype": "Sync Definition"}, name="SYNC-1", doctype="Sync Definition")

		def fake_get_doc(doctype, name):
			if doctype == "Sync Run":
				return run_docs[name]
			if doctype == "Sync Definition":
				return definition_doc
			raise AssertionError((doctype, name))

		def fake_meta(doctype):
			fields = {
				"Sync Run": ["status", "finished_at", "summary", "error_message"],
				"Sync Definition": ["last_run", "last_run_status", "last_run_summary"],
			}
			return DummyMeta([_field(fieldname) for fieldname in fields.get(doctype, [])])

		with (
			patch("sync.sync.service.runtime.now_datetime", return_value=now),
			patch("sync.sync.service.runtime._get_sync_settings", return_value=SimpleNamespace(stale_run_timeout_minutes=60)),
			patch(
				"sync.sync.service.runtime.frappe.get_all",
				return_value=[
					{
						"name": "RUN-Q",
						"sync_definition": "SYNC-1",
						"status": "Queued",
						"creation": datetime(2026, 3, 17, 10, 0, 0),
					},
					{
						"name": "RUN-R",
						"sync_definition": "SYNC-1",
						"status": "Running",
						"started_at": datetime(2026, 3, 17, 10, 30, 0),
					},
					{
						"name": "RUN-FRESH",
						"sync_definition": "SYNC-1",
						"status": "Running",
						"started_at": datetime(2026, 3, 17, 11, 30, 0),
					},
				],
			),
			patch("sync.sync.service.runtime.frappe.get_doc", side_effect=fake_get_doc),
			patch("sync.sync.service.runtime.frappe.get_meta", side_effect=fake_meta),
			patch("sync.sync.service.runtime.frappe.db", _db_stub(exists=lambda *args, **kwargs: True)),
		):
			result = runtime.recover_stale_runs("SYNC-1")

		self.assertEqual(result["recovered_count"], 2)
		self.assertEqual(run_docs["RUN-Q"].payload["status"], "Skipped")
		self.assertEqual(run_docs["RUN-R"].payload["status"], "Error")
		self.assertEqual(definition_doc.payload["last_run_status"], "Error")
		self.assertIn("Recovered stale Running", definition_doc.payload["last_run_summary"])

	def test_cleanup_sync_run_retention_deletes_items_before_runs(self):
		now = datetime(2026, 3, 17, 12, 0, 0)
		deleted = []

		def fake_get_all(doctype, **kwargs):
			if doctype == "Sync Run":
				return [
					{"name": "RUN-OLD-SUCCESS", "status": "Success", "finished_at": datetime(2025, 12, 1, 0, 0)},
					{"name": "RUN-OLD-ERROR", "status": "Error", "finished_at": datetime(2025, 1, 1, 0, 0)},
					{"name": "RUN-FRESH", "status": "Success", "finished_at": datetime(2026, 3, 1, 0, 0)},
				]
			if doctype == "Sync Run Item":
				run_name = kwargs["filters"]["sync_run"]
				return [{"name": f"ITEM-{run_name}"}]
			raise AssertionError(doctype)

		with (
			patch("sync.sync.service.runtime.now_datetime", return_value=now),
			patch(
				"sync.sync.service.runtime._get_sync_settings",
				return_value=SimpleNamespace(run_retention_days_success=90, run_retention_days_error=365),
			),
			patch("sync.sync.service.runtime.frappe.get_all", side_effect=fake_get_all),
			patch("sync.sync.service.runtime.frappe.delete_doc", side_effect=lambda doctype, name, **kwargs: deleted.append((doctype, name))),
			patch("sync.sync.service.runtime.frappe.db", _db_stub()),
		):
			result = runtime.cleanup_sync_run_retention()

		self.assertEqual(result["deleted_runs"], 2)
		self.assertEqual(result["deleted_run_items"], 2)
		self.assertEqual(
			deleted,
			[
				("Sync Run Item", "ITEM-RUN-OLD-SUCCESS"),
				("Sync Run", "RUN-OLD-SUCCESS"),
				("Sync Run Item", "ITEM-RUN-OLD-ERROR"),
				("Sync Run", "RUN-OLD-ERROR"),
			],
		)

	def test_enqueue_sync_definition_executes_immediately_when_queue_disabled(self):
		run_doc = SimpleNamespace(name="RUN-1")
		sync_definition = SimpleNamespace(name="SYNC-1")

		with (
			patch("sync.sync.service.runtime._definition_lock", return_value=nullcontext()),
			patch("sync.sync.service.runtime._has_active_run", return_value=False),
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=sync_definition),
			patch("sync.sync.service.runtime._create_run_doc", return_value=run_doc),
			patch("sync.sync.service.runtime.execute_sync_definition", return_value={"status": "success"}) as mock_execute,
		):
			result = runtime.enqueue_sync_definition("SYNC-1", queue=False, dry_run=True)

		self.assertEqual(result, {"status": "success"})
		mock_execute.assert_called_once_with("SYNC-1", trigger="manual", dry_run=True, run_name="RUN-1")

	def test_run_sync_definition_job_delegates_to_execute(self):
		with patch("sync.sync.service.runtime.execute_sync_definition", return_value={"status": "success"}) as mock_execute:
			result = runtime.run_sync_definition_job("SYNC-1", run_name="RUN-1", trigger="scheduler", dry_run=True)

		self.assertEqual(result, {"status": "success"})
		mock_execute.assert_called_once_with("SYNC-1", trigger="scheduler", dry_run=True, run_name="RUN-1")

	def test_preview_sync_definition_loads_document_and_predicts(self):
		definition = SimpleNamespace(name="SYNC-1")
		with (
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=definition),
			patch.object(runtime.SyncPreviewService, "predict", return_value={"preview": True}) as mock_predict,
		):
			result = runtime.preview_sync_definition("SYNC-1", limit=7)

		self.assertEqual(result, {"preview": True})
		mock_predict.assert_called_once_with(definition, limit=7)

	def test_sync_preview_service_delegates_to_build_preview(self):
		definition = SimpleNamespace(name="SYNC-1")
		with patch("sync.sync.service.runtime._build_preview", return_value={"ok": True}) as mock_preview:
			result = runtime.SyncPreviewService.predict(definition, limit=11)

		self.assertEqual(result, {"ok": True})
		mock_preview.assert_called_once_with(definition, limit=11)

	def test_build_preview_returns_sample_and_ping(self):
		config = SimpleNamespace(
			name="SYNC-1",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe -> Partner",
			mapping={"subject": "title"},
			match_fields=["name"],
			value_mapping={"status": {"Open": "1"}},
			filters=[["status", "=", "Open"]],
			use_last_sync_date=False,
		)
		connector = SimpleNamespace(ping=lambda: SimpleNamespace(ok=True, message="ok", details={"driver": "x"}))

		with (
			patch("sync.sync.service.runtime._build_definition_config", return_value=config),
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace(name="PARTNER-1")),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=connector),
			patch("sync.sync.service.runtime._doctype_has_field", side_effect=lambda _doctype, field: field != "missing"),
			patch("sync.sync.service.runtime.frappe.get_all", return_value=[{"name": "TASK-1", "subject": "Hello"}]) as mock_get_all,
		):
			result = runtime._build_preview(SimpleNamespace(name="SYNC-1"), limit=3)

		self.assertEqual(result["frappe_records_sample_count"], 1)
		self.assertEqual(result["partner_ping"]["ok"], True)
		self.assertEqual(result["mapping"], {"subject": {"partner_field": "title", "direction": "Frappe <-> Partner"}})
		self.assertEqual(result["value_mapping_fields"], ["status"])
		self.assertEqual(mock_get_all.call_args.kwargs["limit_page_length"], 3)

	def test_export_sync_definition_yaml_includes_partner_and_type_documents(self):
		definition = DummyDoc(
			{
				"doctype": "Sync Definition",
				"name": "SYNC-1",
				"partner": "PARTNER-1",
				"export_mask_credentials": 1,
			},
			doctype="Sync Definition",
		)
		partner = DummyDoc(
			{
				"doctype": "Sync Partner",
				"name": "PARTNER-1",
				"partner_type": "postgres",
			},
			doctype="Sync Partner",
		)
		partner_type = DummyDoc({"doctype": "Sync Partner Type", "name": "postgres"}, doctype="Sync Partner Type")

		def fake_get_doc(doctype, name=None):
			if doctype == "Sync Definition":
				return definition
			if doctype == "Sync Partner":
				return partner
			if doctype == "Sync Partner Type":
				return partner_type
			raise AssertionError((doctype, name))

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				new=_runtime_frappe_stub(
					get_doc=fake_get_doc,
					db=_db_stub(exists=lambda *args, **kwargs: True),
				),
			),
			patch("sync.sync.service.runtime._sanitize_document_dict", side_effect=lambda data, mask_credentials=False: {**data, "masked": mask_credentials}),
			patch("sync.sync.service.runtime.now_datetime", return_value=datetime(2026, 3, 17, 12, 0, 0)),
		):
			payload = yaml.safe_load(runtime.export_sync_definition_yaml("SYNC-1"))

		self.assertEqual(payload["version"], 2)
		self.assertEqual(payload["sync_definition"]["masked"], True)
		self.assertEqual(payload["sync_partner"]["name"], "PARTNER-1")
		self.assertEqual(payload["sync_partner_type"]["name"], "postgres")

	def test_export_sync_definition_yaml_omits_runtime_state_fields(self):
		definition = DummyDoc(
			{
				"doctype": "Sync Definition",
				"name": "SYNC-1",
				"title": "Sync 1",
				"partner": None,
				"frequency_cron": "*/15 * * * *",
				"next_run_at": "2026-03-17 13:00:00",
				"last_sync_at": "2026-03-17 12:00:00",
				"last_successful_sync": "2026-03-17 12:00:00",
				"last_run": "RUN-1",
				"last_run_status": "Success",
				"last_run_summary": "created=1",
			},
			doctype="Sync Definition",
		)
		meta = DummyMeta(
			[
				_field("title"),
				_field("partner"),
				_field("frequency_cron"),
				_field("next_run_at", "Datetime"),
				_field("last_sync_at", "Datetime"),
				_field("last_successful_sync", "Datetime"),
				_field("last_run", "Link", "Sync Run"),
				_field("last_run_status"),
				_field("last_run_summary"),
			]
		)

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				new=_runtime_frappe_stub(
					get_doc=lambda doctype, name=None: definition,
					get_meta=lambda doctype: meta,
				),
			),
			patch("sync.sync.service.runtime.now_datetime", return_value=datetime(2026, 3, 17, 12, 0, 0)),
		):
			payload = yaml.safe_load(runtime.export_sync_definition_yaml("SYNC-1"))

		sync_definition = payload["sync_definition"]
		self.assertEqual(sync_definition["frequency_cron"], "*/15 * * * *")
		for fieldname in runtime.SYNC_DEFINITION_RUNTIME_STATE_FIELDS:
			self.assertNotIn(fieldname, sync_definition)

	def test_normalize_sync_definition_payload_omits_runtime_state_fields(self):
		meta = DummyMeta(
			[
				_field("title"),
				_field("frequency_cron"),
				_field("next_run_at", "Datetime"),
				_field("last_sync_at", "Datetime"),
				_field("last_successful_sync", "Datetime"),
				_field("last_run", "Link", "Sync Run"),
				_field("last_run_status"),
				_field("last_run_summary"),
			]
		)
		payload = {
			"name": "SYNC-1",
			"title": "Sync 1",
			"frequency_cron": "*/15 * * * *",
			"next_run_at": "2026-03-17 13:00:00",
			"last_sync_at": "2026-03-17 12:00:00",
			"last_successful_sync": "2026-03-17 12:00:00",
			"last_run": "RUN-1",
			"last_run_status": "Success",
			"last_run_summary": "created=1",
		}

		with patch("sync.sync.service.runtime.frappe", new=_runtime_frappe_stub(get_meta=lambda doctype: meta)):
			normalized = runtime._normalize_doc_payload("Sync Definition", payload)

		self.assertEqual(normalized["name"], "SYNC-1")
		self.assertEqual(normalized["frequency_cron"], "*/15 * * * *")
		for fieldname in runtime.SYNC_DEFINITION_RUNTIME_STATE_FIELDS:
			self.assertNotIn(fieldname, normalized)

	def test_preview_import_sync_definition_yaml_rejects_invalid_yaml_and_top_level_type(self):
		invalid = runtime.preview_import_sync_definition_yaml(":\n  bad", overwrite=True)
		not_mapping = runtime.preview_import_sync_definition_yaml("- item", overwrite=False)

		self.assertFalse(invalid["ok"])
		self.assertFalse(invalid["can_import"])
		self.assertEqual(invalid["summary"]["invalid"], 1)
		self.assertFalse(not_mapping["ok"])
		self.assertEqual(not_mapping["summary"]["invalid"], 1)

	def test_preview_import_sync_definition_yaml_marks_invalid_payload_sections(self):
		payload = yaml.safe_dump(
			{"version": 2, "sync_partner": [], "sync_definition": {"doctype": "Sync Definition"}}
		)

		with patch("sync.sync.service.runtime._normalize_doc_payload", return_value={"doctype": "Sync Definition"}):
			result = runtime.preview_import_sync_definition_yaml(payload, overwrite=False)

		self.assertEqual(result["documents"]["Sync Partner"]["status"], "invalid")
		self.assertEqual(result["documents"]["Sync Definition"]["status"], "invalid")
		self.assertIn("sync_partner_type", result["missing_payload_parts"])

	def test_preview_import_sync_definition_yaml_rejects_legacy_version(self):
		result = runtime.preview_import_sync_definition_yaml(
			yaml.safe_dump({"version": 1, "sync_definition": {"name": "SYNC-1"}})
		)

		self.assertFalse(result["can_import"])
		self.assertIn("Version 2", result["error"])

	def test_import_sync_definition_yaml_only_imports_mapping_sections(self):
		payload = yaml.safe_dump(
			{
				"sync_partner": {"name": "PARTNER-1"},
				"sync_definition": {"name": "SYNC-1"},
				"ignored": "value",
			}
		)

		with (
			patch("sync.sync.service.runtime.preview_import_sync_definition_yaml", return_value={"can_import": True}),
			patch("sync.sync.service.runtime._upsert_document_from_payload", side_effect=["PARTNER-1", "SYNC-1"]) as mock_upsert,
		):
			result = runtime.import_sync_definition_yaml(payload, overwrite=True)

		self.assertEqual(result, {"ok": True, "documents": {"Sync Partner": "PARTNER-1", "Sync Definition": "SYNC-1"}})
		self.assertEqual(mock_upsert.call_count, 2)

	def test_execute_sync_definition_handles_existing_run_and_run_name_shortcuts(self):
		with (
			patch("sync.sync.service.runtime._definition_lock", return_value=nullcontext()),
			patch("sync.sync.service.runtime._has_active_run", return_value=True),
		):
			result = runtime.execute_sync_definition("SYNC-1")

		self.assertEqual(result, {"status": "already_running", "sync_definition": "SYNC-1"})

		run_doc = SimpleNamespace(name="RUN-1")
		definition = SimpleNamespace(name="SYNC-1")
		with (
			patch("sync.sync.service.runtime._definition_lock", return_value=nullcontext()),
			patch("sync.sync.service.runtime.now_datetime", return_value=datetime(2026, 3, 17, 12, 0, 0)),
			patch("sync.sync.service.runtime.frappe.get_doc", side_effect=[run_doc, definition]),
			patch("sync.sync.service.runtime._update_doc_fields"),
			patch("sync.sync.service.runtime._build_definition_config", side_effect=RuntimeError("boom")),
			patch("sync.sync.service.runtime.frappe.log_error"),
			patch("sync.sync.service.runtime.frappe.get_traceback", return_value="Traceback\nRuntimeError: boom"),
			patch("sync.sync.service.runtime._update_definition_failure"),
			self.assertRaises(RuntimeError),
		):
			runtime.execute_sync_definition("SYNC-1", run_name="RUN-1")

	def test_run_engine_requires_context_or_config_and_coerces_plain_objects(self):
		with self.assertRaises(ValueError):
			runtime._run_engine(SimpleNamespace(name="SYNC-1"), SimpleNamespace(name="RUN-1"))

		with patch("sync.sync.service.runtime.frappe.get_meta", return_value=SimpleNamespace(is_submittable=True, fields=[])):
			coerced = runtime._coerce_config(
				SimpleNamespace(
					name="SYNC-1",
					doctype="Task",
					partner="PARTNER-1",
					sync_type="Frappe <- Partner",
					batch_size="25",
					create_new="0",
					update_existing="0",
					frappe_after_insert_action="Submit",
					frappe_after_update_action="bad value",
					delete_missing="1",
					one_way_match_mode="all_matches",
					use_last_sync_date="0",
					timestamp_buffer_ms="3",
					match_fields=("name",),
					mapping={"name": "id"},
					value_mapping={"status": {"Open": "1"}},
				)
			)

		self.assertEqual(coerced.sync_type, "Frappe <- Partner")
		self.assertEqual(coerced.batch_size, 25)
		self.assertFalse(coerced.create_new)
		self.assertFalse(coerced.update_existing)
		self.assertEqual(len(coerced.frappe_write_hooks), 1)
		self.assertEqual(coerced.frappe_write_hooks[0].event, "After Insert")
		self.assertEqual(coerced.frappe_write_hooks[0].action, "Submit")
		self.assertTrue(coerced.delete_missing)
		self.assertEqual(coerced.one_way_match_mode, "all_matches")
		self.assertFalse(coerced.use_last_sync_date)
		self.assertEqual(coerced.timestamp_buffer_ms, 3)
		self.assertEqual(coerced.mapping, {"name": {"partner_field": "id", "direction": "Frappe <-> Partner"}})

	def test_legacy_sync_definition_payload_actions_convert_to_write_hooks(self):
		payload = runtime._sync_definition_payload_with_legacy_hooks(
			{
				"doctype": "Sync Definition",
				"name": "SYNC-1",
				"frappe_after_insert_action": "Submit",
				"frappe_after_update_action": "None",
			}
		)

		self.assertNotIn("frappe_after_insert_action", payload["frappe_write_hooks"][0])
		self.assertEqual(
			payload["frappe_write_hooks"],
			[
				{
					"doctype": "Sync Frappe Write Hook",
					"enabled": 1,
					"event": "After Insert",
					"hook_type": "Built-in Action",
					"action": "Submit",
					"idx": 1,
				}
			],
		)

	def test_coerce_config_clears_delete_missing_for_bidirectional_sync(self):
		coerced = runtime._coerce_config(
			SimpleNamespace(
				name="SYNC-1",
				doctype="Task",
				partner="PARTNER-1",
				sync_type="Frappe <-> Partner",
				delete_missing="1",
				use_last_sync_date=False,
				partner_modified_field="updated_at",
				partner_creation_field="created_at",
				match_fields=("name",),
				mapping={"name": "id"},
				value_mapping={},
			)
		)

		self.assertFalse(coerced.delete_missing)

	def test_mapping_helpers_support_top_level_string_payloads_and_value_reversal(self):
		doc = SimpleNamespace(
			doctype="Sync Definition",
			get=lambda key, default=None: {
				"match_fields": "name, external_id",
				"field_mapping": '{"name": {"partner_field": "id", "direction": "Frappe <-> Partner"}, "status": {"partner_field": "state", "direction": "Frappe <- Partner"}, "subject": {"partner_field": "title", "direction": "Frappe -> Partner"}}',
				"value_mapping": '{"status": {"Open": "1"}}',
				"value_mapping_fallbacks": '{"status": {"action": "null", "value": "ignored"}}',
			}.get(key, default),
		)

		with patch("sync.sync.service.runtime._get_child_rows_by_options", return_value=[]):
			mapping = runtime._get_field_mapping(doc)
			self.assertEqual(runtime._get_match_fields(doc), ["name", "external_id"])
			self.assertEqual(
				mapping,
				{
					"name": {"partner_field": "id", "direction": "Frappe <-> Partner"},
					"status": {"partner_field": "state", "direction": "Frappe <- Partner"},
					"subject": {"partner_field": "title", "direction": "Frappe -> Partner"},
				},
			)
			self.assertEqual(runtime._get_value_mapping(doc), {"status": {"Open": "1"}})
			self.assertEqual(
				runtime._get_value_mapping_fallbacks(doc),
				{"status": {"action": "null", "value": None}},
			)
		self.assertEqual(
			runtime._map_partner_to_frappe(
				{"id": "TASK-1", "state": "1", "title": "Ignored"},
				mapping,
				{"status": {"Open": "1"}},
			),
			{"name": "TASK-1", "status": "Open"},
		)
		self.assertEqual(
			runtime._map_frappe_to_partner(
				{"name": "TASK-1", "status": "Ignored", "subject": "Hello"},
				mapping,
				{"status": {"Open": "1"}},
			),
			{"id": "TASK-1", "title": "Hello"},
		)

	def test_value_mapping_fallbacks_apply_only_after_mapping_miss(self):
		mapping = {
			"status": {"partner_field": "state", "direction": "Frappe <-> Partner"},
		}
		value_mapping = {"status": {"Open": "1"}}
		value_mapping_fallbacks = {
			"status": {"action": "fallback", "value": "Unknown"}
		}

		self.assertEqual(
			runtime._map_partner_to_frappe(
				{"state": "2"},
				mapping,
				value_mapping,
				value_mapping_fallbacks,
			),
			{"status": "Unknown"},
		)
		self.assertEqual(
			runtime._map_frappe_to_partner(
				{"status": "Closed"},
				mapping,
				value_mapping,
				value_mapping_fallbacks,
			),
			{"state": "Unknown"},
		)
		self.assertEqual(
			runtime._map_partner_to_frappe(
				{"state": "1"},
				mapping,
				value_mapping,
				value_mapping_fallbacks,
			),
			{"status": "Open"},
		)
		self.assertEqual(
			runtime._map_frappe_to_partner(
				{"status": "Open"},
				mapping,
				value_mapping,
				value_mapping_fallbacks,
			),
			{"state": "1"},
		)

	def test_value_mapping_fallbacks_can_keep_original_or_write_null(self):
		mapping = {
			"status": {"partner_field": "state", "direction": "Frappe <-> Partner"},
		}

		self.assertEqual(
			runtime._map_partner_to_frappe(
				{"state": "2"},
				mapping,
				{"status": {"Open": "1"}},
			),
			{"status": "2"},
		)
		self.assertEqual(
			runtime._map_partner_to_frappe(
				{"state": "2"},
				mapping,
				{"status": {"Open": "1"}},
				{"status": {"action": "null", "value": "ignored"}},
			),
			{"status": None},
		)
		self.assertEqual(
			runtime._map_frappe_to_partner(
				{"status": "Closed"},
				mapping,
				{"status": {"Open": "1"}},
				{"status": {"action": "Use NULL", "value": "ignored"}},
			),
			{"state": None},
		)
		datetime_mapping = {"scheduled_at": {"partner_field": "scheduled_at", "direction": "Frappe <-> Partner"}}
		datetime_meta = DummyMeta([_field("scheduled_at", "Datetime")])
		with (
			patch("sync.sync.service.runtime.frappe.get_meta", return_value=datetime_meta),
			patch("sync.sync.service.runtime._site_time_zone", return_value="Europe/Berlin"),
		):
			self.assertEqual(
				runtime._map_partner_to_frappe(
					{"scheduled_at": "2026-03-17T10:00:00+00:00"},
					datetime_mapping,
					{},
					doctype="Task",
					partner_time_zone="UTC",
				),
				{"scheduled_at": datetime(2026, 3, 17, 11, 0)},
			)
			self.assertEqual(
				runtime._map_frappe_to_partner(
					{"scheduled_at": datetime(2026, 3, 17, 11, 0)},
					datetime_mapping,
					{},
					doctype="Task",
					partner_time_zone="UTC",
				),
				{"scheduled_at": datetime(2026, 3, 17, 10, 0)},
			)

	def test_value_mapping_maps_null_bidirectionally_and_matches_numeric_partner_values(self):
		mapping = {
			"gender": {"partner_field": "Geschlecht", "direction": "Frappe <-> Partner"},
		}
		value_mapping = {"gender": {None: "2", "Female": "1", "Male": "0"}}

		self.assertEqual(
			runtime._map_frappe_to_partner(
				{"gender": None},
				mapping,
				value_mapping,
			),
			{"Geschlecht": "2"},
		)
		self.assertEqual(
			runtime._map_partner_to_frappe(
				{"Geschlecht": "2"},
				mapping,
				value_mapping,
			),
			{"gender": None},
		)
		self.assertEqual(
			runtime._map_partner_to_frappe(
				{"Geschlecht": 2},
				mapping,
				value_mapping,
			),
			{"gender": None},
		)

	def test_runtime_mapping_context_reuses_reverse_maps_and_datetime_fields(self):
		config = SimpleNamespace(
			doctype="Task",
			mapping={
				"status": {"partner_field": "state", "direction": "Frappe <-> Partner"},
				"scheduled_at": {"partner_field": "scheduled_at", "direction": "Frappe <-> Partner"},
			},
			value_mapping={"status": {"Open": "1"}},
			value_mapping_fallbacks=None,
			frappe_modified_fields=[],
			partner_modified_fields=[],
			partner_time_zone="UTC",
		)

		with (
			patch("sync.sync.service.runtime._get_frappe_datetime_fields", return_value={"scheduled_at"}) as mock_datetime_fields,
			patch("sync.sync.service.runtime._doctype_fieldnames", return_value={"status", "scheduled_at"}),
			patch("sync.sync.service.runtime._site_time_zone", return_value="Europe/Berlin"),
		):
			context = runtime._build_runtime_mapping_context(config)
			self.assertEqual(
				runtime._map_partner_to_frappe(
					{"state": "1", "scheduled_at": "2026-03-17T10:00:00+00:00"},
					config.mapping,
					config.value_mapping,
					mapping_context=context,
				),
				{"status": "Open", "scheduled_at": datetime(2026, 3, 17, 11, 0)},
			)
			self.assertEqual(
				runtime._map_frappe_to_partner(
					{"status": "Open", "scheduled_at": datetime(2026, 3, 17, 11, 0)},
					config.mapping,
					config.value_mapping,
					mapping_context=context,
				),
				{"state": "1", "scheduled_at": datetime(2026, 3, 17, 10, 0)},
			)

		mock_datetime_fields.assert_called_once()
		self.assertEqual(context.reverse_value_mapping, {"status": {"1": "Open"}})

	def test_helper_functions_cover_string_lookup_and_lock_behaviour(self):
		row = SimpleNamespace(as_dict=lambda: {"field_name": "subject"})
		parent_meta = DummyMeta([_field("rows", "Table", "Sync Key Field")])
		parent_doc = SimpleNamespace(doctype="Sync Definition", get=lambda key, default=None: [row] if key == "rows" else default)
		lock_context = object()
		cache = SimpleNamespace(lock=lambda *_args, **_kwargs: lock_context)

		with (
			patch("sync.sync.service.runtime.frappe.get_meta", return_value=parent_meta),
			patch("sync.sync.service.runtime.frappe.cache", return_value=cache),
		):
			rows = runtime._get_child_rows_by_options(parent_doc, "Sync Key Field")
			lock = runtime._definition_lock("sync:lock:1")

		self.assertEqual(rows, [{"field_name": "subject"}])
		self.assertIs(lock, lock_context)
		self.assertEqual(runtime._parse_lines(" a \n\n b "), ["a", "b"])
		self.assertEqual(runtime._clean_string("  value  "), "value")
		self.assertEqual(runtime._first_value({"a": "", "b": "x"}, ["a", "b"]), "x")
		self.assertEqual(runtime._first_value_dict({"a": "", "b": "x"}, ["a", "b"]), "x")

	def test_runtime_change_detection_helpers_cover_edge_cases(self):
		self.assertEqual(
			runtime._diff_target_values(
				new_record={"payload": {"a": 1}, "timestamp": datetime(2026, 3, 17, 12, 0)},
				old_record={"payload": {"a": 1}, "timestamp": datetime(2026, 3, 17, 12, 0)},
				field_names=["payload", "timestamp"],
			),
			[],
		)
		with patch("sync.sync.service.runtime._site_time_zone", return_value="Europe/Berlin"):
			self.assertEqual(
				runtime._parse_datetime("2026-03-17T10:00:00+00:00"),
				datetime(2026, 3, 17, 11, 0),
			)
			self.assertEqual(
				runtime._parse_datetime(
					"2026-03-17 10:00:00",
					assumed_time_zone="UTC",
					target_time_zone="Europe/Berlin",
				),
				datetime(2026, 3, 17, 11, 0),
			)
		self.assertIsNone(runtime._parse_datetime("not-a-date"))
		self.assertIsNone(runtime._parse_datetime(None))

	def test_runtime_record_helpers_compact_and_trim_values(self):
		config = SimpleNamespace(match_fields=["name"], mapping={"name": "id"})

		self.assertEqual(runtime._compact_record_key(config, frappe_record={"name": "TASK-1"}, partner_record=None), "name=TASK-1")
		self.assertEqual(runtime._compact_source_id(config, frappe_record={"name": "TASK-1"}), "TASK-1")
		self.assertEqual(runtime._compact_target_id(config, partner_record={"id": "TASK-1"}), "id=TASK-1")
		self.assertEqual(runtime._fit_data_value("x" * 150), f"{'x' * 137}...")

	def test_sync_direction_helpers_cover_remaining_skip_and_error_branches(self):
		logged = []
		config_a_to_b = SimpleNamespace(
			name="SYNC-A2B",
			match_fields=["name"],
			mapping={"name": "id", "status": "state"},
			value_mapping={},
			create_new=True,
			delete_missing=True,
			table_name="dbo.SyncTable",
			read_query=None,
		)
		config_b_to_a = SimpleNamespace(
			name="SYNC-B2A",
			doctype="Task",
			match_fields=["name"],
			mapping={"name": "id", "status": "state"},
			value_mapping={},
			create_new=False,
			delete_missing=True,
		)

		with patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)):
			runtime._sync_frappe_to_partner(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config_a_to_b,
				connector=SimpleNamespace(
					upsert_record=lambda **kwargs: ConnectorWriteResult(ok=True, message="ok"),
					delete_record=lambda **kwargs: ConnectorWriteResult(ok=False, message="delete failed"),
				),
				frappe_records=[{"name": "TASK-1", "status": "Open"}],
				partner_records=[{"id": "TASK-1", "state": "Open"}, {"id": "TASK-2", "state": "Old"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe -> Partner",
				full_sync=True,
			)
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-2"),
				config=config_b_to_a,
				connector=object(),
				partner_records=[{"id": "TASK-1", "state": "Open"}],
				frappe_records=[{"name": "TASK-1", "status": "Open"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)
		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-3"),
				config=config_b_to_a,
				connector=object(),
				partner_records=[{"state": "Broken"}],
				frappe_records=[],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)

		self.assertEqual([entry["action"] for entry in logged], ["skipped", "error", "skipped"])

	def test_run_engine_aborts_before_delete_missing_when_partner_load_is_partial(self):
		config = runtime.SyncDefinitionConfig(
			name="SYNC-P2F",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe <- Partner",
			cron=None,
			filters=None,
			batch_size=10,
			create_new=True,
			delete_missing=True,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			timestamp_buffer_ms=0,
			table_name="dbo.SyncTable",
			read_query=None,
			match_fields=["name"],
			mapping={"name": "id"},
			value_mapping={},
			frappe_modified_field="modified",
			frappe_creation_field="creation",
			partner_modified_field="updated_at",
			partner_creation_field="created_at",
		)

		def fetch_records(*, source, query, batch_size, cursor, key_fields):
			if cursor is None:
				return {"records": [{"id": "TASK-1"}], "next_cursor": "page-2"}
			raise RuntimeError("fetch failed")

		connector = SimpleNamespace(
			ping=lambda: SimpleNamespace(ok=True, message="ok", details={}),
			fetch_records=fetch_records,
		)

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace(name="PARTNER-1")),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=connector),
			patch("sync.sync.service.runtime._iter_frappe_source_batches", return_value=iter([[{"name": "TASK-2"}]])),
			patch("sync.sync.service.runtime._sync_partner_to_frappe") as mock_sync,
		):
			with self.assertRaisesRegex(RuntimeError, "Partner source load failed at cursor 'page-2' after 1 records."):
				runtime._run_engine(SimpleNamespace(name="SYNC-P2F"), SimpleNamespace(name="RUN-1"), config=config)

		mock_sync.assert_not_called()

	def test_bidirectional_skips_when_no_differences_and_prefers_partner_when_only_partner_changed(self):
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

		with (
			patch("sync.sync.service.runtime._register_and_log") as mock_log,
			patch("sync.sync.service.runtime._apply_partner_update") as mock_partner,
			patch("sync.sync.service.runtime._apply_frappe_update") as mock_frappe,
		):
			runtime._sync_bidirectional(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				frappe_records=[{"name": "TASK-1", "status": "Open", "modified": "2026-03-17 09:00:00"}, {"name": "TASK-2", "status": "Closed", "modified": "2026-03-17 09:00:00"}],
				partner_records=[{"id": "TASK-1", "state": "Open", "updated_at": "2026-03-17 09:00:00"}, {"id": "TASK-2", "state": "Open", "updated_at": "2026-03-17 11:00:00"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				last_successful_sync=datetime(2026, 3, 17, 10, 0, 0),
			)

		mock_log.assert_called_once()
		mock_partner.assert_not_called()
		mock_frappe.assert_called_once()

	def test_update_partner_connection_status_and_due_by_cron_helpers(self):
		partner_doc = DummyDoc(doctype="Sync Partner")
		meta = DummyMeta([_field("last_connection_status"), _field("last_checked_on"), _field("last_connection_error")])
		cron_logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
		run_meta = DummyMeta([_field("sync_definition"), _field("finished_at"), _field("status")])
		now = datetime(2026, 3, 17, 12, 0, 0)

		with (
			patch("sync.sync.service.runtime.frappe", new=_runtime_frappe_stub(get_meta=lambda *_args, **_kwargs: meta)),
			patch("sync.sync.service.runtime.now_datetime", return_value=now),
		):
			runtime._update_partner_connection_status(partner_doc, status="error", details="boom")

		self.assertEqual(partner_doc.db_set_calls[0][0], "last_connection_status")
		self.assertEqual(partner_doc.payload["last_connection_error"], "boom")

		with patch("sync.sync.service.runtime.croniter", None):
			self.assertFalse(runtime._is_due_by_cron(SimpleNamespace(name="SYNC-1"), "*/5 * * * *", now))

		with (
			patch("sync.sync.service.runtime.croniter", DummyCroniter(error=True)),
			patch("sync.sync.service.runtime.frappe.logger", return_value=cron_logger),
		):
			self.assertFalse(runtime._is_due_by_cron(SimpleNamespace(name="SYNC-1"), "bad", now))

		with (
			patch("sync.sync.service.runtime.croniter", DummyCroniter(previous=datetime(2026, 3, 17, 11, 55, 0))),
			patch("sync.sync.service.runtime.frappe.get_meta", return_value=run_meta),
			patch("sync.sync.service.runtime.frappe.get_all", return_value=[]),
		):
			self.assertTrue(runtime._is_due_by_cron(SimpleNamespace(name="SYNC-1"), "*/5 * * * *", now))

	def test_runtime_document_helpers_create_and_update_records(self):
		run_meta = DummyMeta(
			[
				_field("sync_definition"),
				_field("status"),
				_field("trigger_type"),
				_field("dry_run"),
				_field("started_at"),
				_field("sync_type"),
				_field("sync_partner"),
			]
		)
		item_meta = DummyMeta(
			[
				_field("sync_run"),
				_field("sync_definition"),
				_field("action"),
				_field("status"),
				_field("message"),
				_field("write_direction"),
				_field("document_name"),
				_field("record_key"),
				_field("source_id"),
				_field("target_id"),
				_field("change_count"),
				_field("changed_fields"),
				_field("frappe_before_payload"),
				_field("partner_before_payload"),
				_field("written_after_payload"),
			]
		)
		run_doc = DummyDoc(name="RUN-1", doctype="Sync Run")
		run_item_doc = DummyDoc(name="ITEM-1", doctype="Sync Run Item")

		get_doc = Mock(side_effect=[run_doc, run_item_doc])
		with (
			patch(
				"sync.sync.service.runtime.frappe",
				new=_runtime_frappe_stub(
					get_meta=Mock(return_value=item_meta),
					get_doc=get_doc,
				),
			),
			patch("sync.sync.service.runtime.now_datetime", return_value=datetime(2026, 3, 17, 12, 0, 0)),
		):
			created_run = runtime._create_run_doc(SimpleNamespace(name="SYNC-1", get=lambda key, default=None: {"sync_type": "Frappe -> Partner", "partner": "PARTNER-1"}.get(key, default)), status="Queued", trigger="api", dry_run=True)
			created_item = runtime._create_run_item(
				run_doc=SimpleNamespace(name="RUN-1", get=lambda key, default=None: {"sync_type": "Frappe -> Partner"}.get(key, default)),
				config=SimpleNamespace(match_fields=["name"], mapping={"name": "id"}),
				sync_definition_name="SYNC-1",
				action="created",
				status="success",
				frappe_record={"name": "TASK-1"},
				partner_record={"id": "TASK-1"},
				message="ok",
				changes=[("status", "Open", "Closed")],
			)

		self.assertTrue(created_run.inserted)
		self.assertTrue(created_item.inserted)
		item_payload = get_doc.call_args_list[1].args[0]
		self.assertEqual(item_payload["write_direction"], "Frappe -> Partner")
		self.assertEqual(item_payload["change_count"], 1)
		self.assertEqual(item_payload["changed_fields"], "status")

	def test_runtime_update_helpers_and_last_successful_sync(self):
		meta = DummyMeta([_field("status"), _field("summary"), _field("last_run"), _field("last_run_status"), _field("last_run_summary"), _field("last_sync_at"), _field("last_successful_sync"), _field("next_run_at")])
		doc = DummyDoc(name="SYNC-1", doctype="Sync Definition")
		logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
		next_run = datetime(2026, 3, 17, 12, 15, 0)

		with (
			patch("sync.sync.service.runtime.frappe", new=_runtime_frappe_stub(get_meta=lambda *_args, **_kwargs: meta)),
		):
			runtime._update_doc_fields(SimpleNamespace(doctype="Sync Run", db_set=doc.db_set), {"status": "Success", "missing": "x"})
			runtime._update_definition_runtime(doc, last_run="RUN-1", last_sync_at=next_run, summary="ok")
			runtime._update_definition_failure(doc, last_run="RUN-2", error_message="line1\nline2")

		self.assertEqual(doc.payload["status"], "Success")
		self.assertEqual(doc.payload["last_run"], "RUN-2")
		self.assertEqual(doc.payload["last_run_summary"], "line2")

		with (
			patch("sync.sync.service.runtime.croniter", DummyCroniter(next_value=next_run)),
			patch("sync.sync.service.runtime.now_datetime", return_value=datetime(2026, 3, 17, 12, 0, 0)),
			patch("sync.sync.service.runtime.frappe", new=_runtime_frappe_stub(get_meta=lambda *_args, **_kwargs: meta)),
		):
			runtime._set_next_run_at(doc, "*/15 * * * *")

		self.assertEqual(doc.payload["next_run_at"], next_run)

		runs = [
			{"sync_definition": "SYNC-1", "status": "Success", "dry_run": 1, "finished_at": "2026-03-17 12:00:00"},
			{"sync_definition": "SYNC-1", "status": "Success", "dry_run": 0, "finished_at": "2026-03-17 11:55:00"},
		]

		def get_all(_doctype, *, filters, **_kwargs):
			return [
				{"finished_at": run["finished_at"]}
				for run in runs
				if all(run.get(fieldname) == value for fieldname, value in filters.items())
			][:1]

		with (
			patch("sync.sync.service.runtime.frappe.get_meta", return_value=DummyMeta([_field("sync_definition"), _field("status"), _field("dry_run"), _field("finished_at")])),
			patch("sync.sync.service.runtime.frappe.get_all", side_effect=get_all) as mock_get_all,
		):
			self.assertEqual(runtime._get_last_successful_sync("SYNC-1"), datetime(2026, 3, 17, 11, 55, 0))

		self.assertEqual(mock_get_all.call_args.kwargs["filters"]["dry_run"], 0)

		self.assertIn("processed=5", runtime._format_run_summary({"processed_count": 5, "error_count": 1}))

	def test_update_helpers_batch_valid_field_writes_and_preserve_commit_flags(self):
		meta = DummyMeta(
			[
				_field("status"),
				_field("last_run"),
				_field("last_run_status"),
				_field("last_run_summary"),
				_field("last_sync_at"),
				_field("last_successful_sync"),
			]
		)
		set_value = Mock()
		commit = Mock()
		db = _db_stub(set_value=set_value, commit=commit)

		with patch(
			"sync.sync.service.runtime.frappe",
			new=_runtime_frappe_stub(db=db, get_meta=lambda *_args, **_kwargs: meta),
		):
			run_doc = SimpleNamespace(name="RUN-1", doctype="Sync Run", db_set=Mock())
			runtime._update_doc_fields(run_doc, {"status": "Success", "missing": "ignored"}, commit=True)
			definition_doc = SimpleNamespace(name="SYNC-1", doctype="Sync Definition", db_set=Mock())
			runtime._update_definition_runtime(
				definition_doc,
				last_run="RUN-1",
				last_sync_at=datetime(2026, 3, 17, 12, 0),
				summary="ok",
				commit=False,
			)

		self.assertEqual(
			set_value.call_args_list[0].args,
			("Sync Run", "RUN-1", {"status": "Success"}),
		)
		self.assertEqual(set_value.call_args_list[0].kwargs, {"update_modified": False})
		self.assertFalse(run_doc.db_set.called)
		self.assertEqual(
			set_value.call_args_list[1].args,
			(
				"Sync Definition",
				"SYNC-1",
				{
					"last_run": "RUN-1",
					"last_run_status": "Success",
					"last_run_summary": "ok",
					"last_sync_at": datetime(2026, 3, 17, 12, 0),
					"last_successful_sync": datetime(2026, 3, 17, 12, 0),
				},
			),
		)
		self.assertEqual(set_value.call_args_list[1].kwargs, {"update_modified": False})
		self.assertFalse(definition_doc.db_set.called)
		commit.assert_called_once()
