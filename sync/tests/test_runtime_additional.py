from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import frappe

from sync.sync.service import runtime
from sync.sync.service.connectors import ConnectorPingResult, ConnectorWriteResult


class FakeDoc:
	def __init__(self, name="DOC-1", doctype="Task"):
		self.name = name
		self.doctype = doctype
		self.values = {}

	def db_set(self, key, value, update_modified=False):
		self.values[key] = value

	def get(self, key, default=None):
		return self.values.get(key, default)


class FakeInsertDoc(FakeDoc):
	def __init__(self, name="DOC-1", doctype="Sync Run"):
		super().__init__(name=name, doctype=doctype)
		self.inserted = False

	def insert(self, **kwargs):
		self.inserted = True
		return self


class MetaWithFields:
	def __init__(self, fields):
		self.fields = fields

	def has_field(self, fieldname):
		return any(field.fieldname == fieldname for field in self.fields)


def _db_stub(**overrides):
	values = {"exists": lambda *args, **kwargs: False, "commit": lambda: None}
	values.update(overrides)
	return SimpleNamespace(**values)


def _runtime_frappe_stub(**overrides):
	values = {"db": _db_stub()}
	values.update(overrides)
	return SimpleNamespace(**values)


def _identity_match_config(one_way_match_mode="first_match"):
	return runtime.SyncDefinitionConfig(
		name="SYNC-IDENTITY",
		doctype="Task",
		partner="PARTNER-1",
		sync_type="Frappe <-> Partner",
		cron=None,
		filters=None,
		batch_size=10,
		create_new=True,
		delete_missing=False,
		use_last_sync_date=False,
		conflict_policy="newest_wins",
		timestamp_buffer_seconds=0,
		table_name="tabTask",
		read_query=None,
		match_fields=["name"],
		mapping={"name": {"partner_field": "id", "direction": "Frappe <-> Partner"}},
		value_mapping={},
		frappe_modified_fields=["modified"],
		partner_modified_fields=["updated_at"],
		partner_identity_field="external_id",
		frappe_partner_identity_field="partner_id",
		one_way_match_mode=one_way_match_mode,
	)


class TestRuntimeAdditional(unittest.TestCase):
	def test_run_due_sync_definitions_scheduled_sets_admin_and_delegates(self):
		delegated = [{"status": "queued", "sync_definition": "SYNC-1"}]

		with (
			patch("sync.sync.service.runtime.frappe.set_user") as mock_set_user,
			patch("sync.sync.service.runtime.run_due_sync_definitions", return_value=delegated) as mock_run_due,
		):
			result = runtime.run_due_sync_definitions_scheduled(limit=7, queue=False)

		self.assertIs(result, delegated)
		mock_set_user.assert_called_once_with("Administrator")
		mock_run_due.assert_called_once_with(limit=7, queue=False)

	def test_pairing_key_and_identity_index_helpers_cover_mapping_branches(self):
		config = _identity_match_config()

		self.assertEqual(runtime._normalize_number_pairing_key("001.2300"), "1.23")
		self.assertEqual(runtime._normalize_number_pairing_key("NaN"), "NaN")
		self.assertEqual(runtime._normalize_number_pairing_key("not-a-number"), "not-a-number")
		self.assertEqual(runtime._normalize_decimal_pairing_key(Decimal("0.000")), "0")
		self.assertEqual(runtime._normalize_decimal_pairing_key(Decimal("123.4500")), "123.45")
		self.assertEqual(runtime._normalize_decimal_pairing_key(Decimal("NaN")), "NaN")
		self.assertEqual(
			runtime._normalize_datetime_string_pairing_key("2026-03-17T12:00:00+02:00"),
			"2026-03-17T10:00:00.000000+00:00",
		)
		self.assertIsNone(runtime._normalize_datetime_string_pairing_key("2026/03/17"))
		self.assertEqual(runtime._normalize_pairing_key_value(True), ("bool", True))
		self.assertEqual(runtime._normalize_pairing_key_value(1.2300), "1.23")
		self.assertEqual(
			runtime._normalize_pairing_key_value(datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc)),
			("datetime", "2026-03-17T12:00:00.000000+00:00"),
		)
		self.assertEqual(
			runtime._normalize_pairing_key_value("2026-03-17T12:00:00+02:00"),
			("datetime", "2026-03-17T10:00:00.000000+00:00"),
		)
		self.assertTrue(runtime._normalize_pairing_key_value(object()).startswith("<object object at "))
		self.assertTrue(runtime._valid_key(("TASK-1", 1)))
		self.assertFalse(runtime._valid_key(()))
		self.assertFalse(runtime._valid_key(("TASK-1", "")))
		self.assertFalse(runtime._valid_key((None,)))

		frappe_record = {"name": "TASK-1", "partner_id": "EXT-1"}
		partner_record = {"id": "TASK-1", "external_id": "EXT-1"}
		self.assertEqual(runtime._partner_key_values_from_frappe_record(config, frappe_record), {"id": "TASK-1"})
		self.assertEqual(runtime._partner_key_values_from_partner_record(config, partner_record), {"id": "TASK-1"})
		self.assertEqual(runtime._partner_fetch_key_fields(config), ["id", "external_id"])

		list_index = runtime._build_partner_identity_index(
			config,
			[
				{"id": "TASK-1", "external_id": "EXT-1"},
				{"id": "TASK-BLANK", "external_id": ""},
				{"id": "TASK-MISSING"},
			],
		)
		self.assertEqual(list_index, {"EXT-1": {"id": "TASK-1", "external_id": "EXT-1"}})

		dict_index = runtime._build_partner_identity_index(
			config,
			{
				("TASK-2",): {"id": "TASK-2", "external_id": "EXT-2"},
				("TASK-BLANK",): {"id": "TASK-BLANK", "external_id": ""},
			},
		)
		self.assertEqual(dict_index, {"EXT-2": {"id": "TASK-2", "external_id": "EXT-2"}})

		frappe_list_index = runtime._build_frappe_partner_identity_index(
			config,
			[
				{"name": "TASK-1", "partner_id": "EXT-1"},
				{"name": "TASK-BLANK", "partner_id": ""},
				{"name": "TASK-MISSING"},
			],
		)
		self.assertEqual(frappe_list_index, {"EXT-1": {"name": "TASK-1", "partner_id": "EXT-1"}})

		frappe_dict_index = runtime._build_frappe_partner_identity_index(
			config,
			{
				("TASK-2",): {"name": "TASK-2", "partner_id": "EXT-2"},
				("TASK-BLANK",): {"name": "TASK-BLANK", "partner_id": ""},
			},
		)
		self.assertEqual(frappe_dict_index, {"EXT-2": {"name": "TASK-2", "partner_id": "EXT-2"}})
		self.assertEqual(runtime._build_frappe_partner_identity_index(SimpleNamespace(), [{"name": "TASK-1"}]), {})

	def test_identity_matching_prefers_stored_ids_and_pair_tokens_fall_back_to_keys(self):
		first_match_config = _identity_match_config()
		all_matches_config = _identity_match_config(one_way_match_mode="all_matches")

		identity_partner = {"id": "TASK-BY-IDENTITY", "external_id": "EXT-2"}
		matching_partner_old = {"id": "TASK-1", "external_id": "EXT-OLD"}
		matching_partner_new = {"id": "TASK-1", "external_id": "EXT-NEW"}
		partner_groups = {("TASK-1",): [matching_partner_old, matching_partner_new]}
		partner_identity_index = {"EXT-2": identity_partner}

		self.assertEqual(
			runtime._find_existing_partner_records(
				first_match_config,
				{"name": "TASK-1", "partner_id": "EXT-2"},
				partner_groups,
				partner_identity_index,
			),
			[identity_partner],
		)
		self.assertEqual(
			runtime._find_existing_partner_record(
				first_match_config,
				{"name": "TASK-1"},
				{("TASK-1",): matching_partner_new},
				{},
			),
			matching_partner_new,
		)
		self.assertEqual(
			runtime._find_existing_partner_records(
				first_match_config,
				{"name": "TASK-1"},
				partner_groups,
				partner_identity_index,
			),
			[matching_partner_new],
		)
		self.assertEqual(
			runtime._find_existing_partner_records(
				all_matches_config,
				{"name": "TASK-1"},
				partner_groups,
				partner_identity_index,
			),
			[matching_partner_old, matching_partner_new],
		)

		identity_frappe = {"name": "TASK-BY-IDENTITY", "partner_id": "EXT-2"}
		matching_frappe_old = {"name": "TASK-1", "partner_id": "EXT-OLD"}
		matching_frappe_new = {"name": "TASK-1", "partner_id": "EXT-NEW"}
		frappe_groups = {("TASK-1",): [matching_frappe_old, matching_frappe_new]}
		frappe_partner_identity_index = {"EXT-2": identity_frappe}

		self.assertEqual(
			runtime._find_existing_frappe_records(
				first_match_config,
				{"id": "TASK-1", "external_id": "EXT-2"},
				frappe_groups,
				frappe_partner_identity_index,
			),
			[identity_frappe],
		)
		self.assertEqual(
			runtime._find_existing_frappe_record(
				first_match_config,
				{"id": "TASK-1"},
				{("TASK-1",): matching_frappe_new},
				{},
			),
			matching_frappe_new,
		)
		self.assertEqual(
			runtime._find_existing_frappe_records(
				first_match_config,
				{"id": "TASK-1"},
				frappe_groups,
				frappe_partner_identity_index,
			),
			[matching_frappe_new],
		)
		self.assertEqual(
			runtime._find_existing_frappe_records(
				all_matches_config,
				{"id": "TASK-1"},
				frappe_groups,
				frappe_partner_identity_index,
			),
			[matching_frappe_old, matching_frappe_new],
		)

		self.assertEqual(
			runtime._pair_token_from_frappe(first_match_config, {"name": "TASK-1", "partner_id": "EXT-1"}),
			("partner_identity", "EXT-1"),
		)
		self.assertEqual(
			runtime._pair_token_from_partner(first_match_config, {"id": "TASK-1", "external_id": "EXT-1"}),
			("partner_identity", "EXT-1"),
		)
		self.assertEqual(
			runtime._pair_token_from_frappe(first_match_config, {"name": "TASK-1"}),
			("match", "TASK-1"),
		)
		self.assertEqual(
			runtime._pair_token_from_partner(first_match_config, {"id": "TASK-1"}),
			("match", "TASK-1"),
		)
		self.assertIsNone(runtime._pair_token_from_frappe(first_match_config, {"name": ""}))
		self.assertIsNone(runtime._pair_token_from_partner(first_match_config, {"id": None}))

		self.assertEqual(
			runtime._normalize_partner_match_records(first_match_config, [{"id": "TASK-1"}]),
			{("TASK-1",): {"id": "TASK-1"}},
		)
		partner_records = [{"id": "TASK-1"}]
		self.assertIs(runtime._normalize_partner_match_records(all_matches_config, partner_records), partner_records)
		self.assertEqual(
			runtime._normalize_frappe_match_records(first_match_config, [{"name": "TASK-1"}]),
			{("TASK-1",): {"name": "TASK-1"}},
		)
		frappe_records = [{"name": "TASK-1"}]
		self.assertIs(runtime._normalize_frappe_match_records(all_matches_config, frappe_records), frappe_records)
		self.assertEqual(
			runtime._partner_key_values_for_write(
				first_match_config,
				{"name": "TASK-1", "partner_id": "EXT-1"},
				("TASK-1",),
			),
			{"external_id": "EXT-1"},
		)
		self.assertEqual(
			runtime._partner_key_values_for_existing_match(
				first_match_config,
				{"name": "TASK-1", "partner_id": "EXT-1"},
				("TASK-1",),
				{"id": "TASK-1", "external_id": "EXT-MATCH"},
			),
			{"external_id": "EXT-MATCH"},
		)
		self.assertTrue(runtime._can_write_partner_matches_individually(first_match_config, [matching_partner_old]))
		self.assertFalse(runtime._can_write_partner_matches_individually(SimpleNamespace(), [matching_partner_old, matching_partner_new]))
		self.assertTrue(
			runtime._can_write_partner_matches_individually(
				first_match_config,
				[matching_partner_old, matching_partner_new],
			)
		)
		self.assertEqual(
			runtime._apply_partner_link_fields(
				SimpleNamespace(partner_frappe_identity_field="frappe_name"),
				{"name": "TASK-1"},
				{"id": "TASK-1"},
			),
			{"id": "TASK-1", "frappe_name": "TASK-1"},
		)

	def test_preview_service_predict_and_run_engine_guard_paths(self):
		with patch("sync.sync.service.runtime._build_preview", return_value={"ok": True}) as mock_preview:
			self.assertEqual(runtime.SyncPreviewService.predict(SimpleNamespace(name="SYNC-1"), limit=7), {"ok": True})
		mock_preview.assert_called_once_with(SimpleNamespace(name="SYNC-1"), limit=7)

		with self.assertRaisesRegex(ValueError, "Either context or config must be provided"):
			runtime._run_engine(SimpleNamespace(name="SYNC-1"), SimpleNamespace(name="RUN-1"))

		config = runtime.SyncDefinitionConfig(
			name="SYNC-1",
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
			timestamp_buffer_seconds=0,
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={"name": "id"},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
		)
		coerced_config = runtime._coerce_config(config)
		self.assertIsNot(coerced_config, config)
		self.assertEqual(coerced_config.mapping, {"name": {"partner_field": "id", "direction": "Frappe <-> Partner"}})

	def test_sync_stats_and_context_properties(self):
		stats = runtime.SyncStats()
		stats.register("created", "success")
		stats.register("updated", "conflict")
		stats.register("deleted", "error")
		stats.register("skipped", "skipped")

		self.assertEqual(
			stats.as_dict(),
			{
				"processed_count": 4,
				"success_count": 1,
				"created_count": 1,
				"updated_count": 1,
				"deleted_count": 1,
				"skipped_count": 1,
				"conflict_count": 1,
				"error_count": 1,
			},
		)

		config = SimpleNamespace(use_last_sync_date=1, timestamp_buffer_seconds=15)
		context = runtime.SyncContext(config=config, dry_run=False, last_successful_sync=datetime(2026, 3, 17, 12, 0))
		self.assertTrue(context.is_delta_sync)
		self.assertEqual(context.delta_since, datetime(2026, 3, 17, 11, 59, 45))
		self.assertFalse(context.is_full_sync)

	def test_run_due_sync_definitions_respects_limit_and_queue_flag(self):
		with (
			patch("sync.sync.service.runtime.list_due_sync_definitions", return_value=["A", "B", "C"]),
			patch("sync.sync.service.runtime.enqueue_sync_definition", side_effect=lambda name, **kwargs: {"name": name, **kwargs}) as mock_enqueue,
		):
			result = runtime.run_due_sync_definitions(limit=2, queue=False)

		self.assertEqual(result, [{"name": "A", "trigger": "scheduler", "queue": False}, {"name": "B", "trigger": "scheduler", "queue": False}])
		self.assertEqual(mock_enqueue.call_count, 2)

	def test_enqueue_sync_definition_without_queue_delegates_to_execute(self):
		run_doc = SimpleNamespace(name="RUN-1")
		definition = SimpleNamespace(name="SYNC-1")

		with (
			patch("sync.sync.service.runtime._definition_lock", return_value=nullcontext()),
			patch("sync.sync.service.runtime._has_active_run", return_value=False),
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=definition),
			patch("sync.sync.service.runtime._create_run_doc", return_value=run_doc),
			patch("sync.sync.service.runtime.execute_sync_definition", return_value={"status": "success"}) as mock_execute,
		):
			result = runtime.enqueue_sync_definition("SYNC-1", queue=False, dry_run=True)

		self.assertEqual(result, {"status": "success"})
		mock_execute.assert_called_once_with("SYNC-1", trigger="manual", dry_run=True, run_name="RUN-1")

	def test_run_sync_definition_job_delegates_to_execute(self):
		with patch("sync.sync.service.runtime.execute_sync_definition", return_value={"status": "ok"}) as mock_execute:
			result = runtime.run_sync_definition_job("SYNC-1", run_name="RUN-1", trigger="scheduler", dry_run=True)

		self.assertEqual(result, {"status": "ok"})
		mock_execute.assert_called_once_with("SYNC-1", trigger="scheduler", dry_run=True, run_name="RUN-1")

	def test_preview_and_import_helpers_cover_invalid_payloads_and_export(self):
		sync_definition_doc = SimpleNamespace(
			name="SYNC-1",
			as_dict=lambda: {"doctype": "Sync Definition", "name": "SYNC-1", "partner": "PARTNER-1", "export_mask_credentials": 1},
			get=lambda key, default=None: {"partner": "PARTNER-1", "export_mask_credentials": 1}.get(key, default),
		)
		partner_doc = SimpleNamespace(
			name="PARTNER-1",
			as_dict=lambda: {"doctype": "Sync Partner", "name": "PARTNER-1", "partner_type": "mssql"},
			get=lambda key, default=None: {"partner_type": "mssql"}.get(key, default),
		)
		partner_type_doc = SimpleNamespace(as_dict=lambda: {"doctype": "Sync Partner Type", "name": "mssql"})

		with (
			patch(
				"sync.sync.service.runtime.frappe",
				new=_runtime_frappe_stub(
					get_doc=Mock(side_effect=[sync_definition_doc, partner_doc, partner_type_doc]),
					db=_db_stub(exists=lambda *args, **kwargs: True),
				),
			),
			patch("sync.sync.service.runtime._sanitize_document_dict", side_effect=lambda doc, mask_credentials=False: doc),
			patch("sync.sync.service.runtime.now_datetime", return_value=datetime(2026, 3, 17, 12, 0)),
		):
			exported = runtime.export_sync_definition_yaml("SYNC-1")

		self.assertIn("sync_partner_type", exported)
		self.assertFalse(runtime.preview_import_sync_definition_yaml(":\n  bad")["ok"])
		self.assertFalse(runtime.preview_import_sync_definition_yaml("- item")["ok"])
		invalid_section = runtime.preview_import_sync_definition_yaml("sync_definition: []")
		self.assertFalse(invalid_section["ok"])
		self.assertEqual(invalid_section["documents"]["Sync Definition"]["status"], "invalid")
		with self.assertRaises(frappe.ValidationError):
			runtime.import_sync_definition_yaml("sync_definition: []")

	def test_build_preview_and_coerce_config(self):
		definition = SimpleNamespace(name="SYNC-1")
		config = SimpleNamespace(
			name="SYNC-1",
			sync_type="Frappe -> Partner",
			partner="PARTNER-1",
			doctype="Task",
			mapping={"subject": "title"},
			match_fields=["name"],
			value_mapping={"status": {"Open": "1"}},
			filters=[["status", "=", "Open"]],
		)
		connector = SimpleNamespace(ping=lambda: ConnectorPingResult(ok=True, message="ok", details={"db": "x"}))

		with (
			patch("sync.sync.service.runtime._build_definition_config", return_value=config),
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace()),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=connector),
			patch("sync.sync.service.runtime._doctype_has_field", return_value=True),
			patch("sync.sync.service.runtime.frappe.get_all", return_value=[{"name": "TASK-1"}]),
		):
			preview = runtime._build_preview(definition, limit=5)

		self.assertEqual(preview["frappe_records_sample_count"], 1)
		self.assertEqual(preview["mapping"], {"subject": {"partner_field": "title", "direction": "Frappe <-> Partner"}})
		self.assertEqual(preview["value_mapping_fields"], ["status"])

		coerced = runtime._coerce_config(SimpleNamespace(name="SYNC-2", doctype="Task", partner="PARTNER-1", sync_type="Frappe <- Partner"))
		self.assertEqual(coerced.sync_type, "Frappe <- Partner")
		self.assertEqual(coerced.batch_size, 100)
		self.assertEqual(coerced.frappe_modified_fields, ["modified"])
		self.assertEqual(coerced.mapping, {})

	def test_run_engine_routes_partner_to_frappe_branch(self):
		config = SimpleNamespace(
			name="SYNC-1",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe <- Partner",
			cron=None,
			filters=None,
			batch_size=10,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			timestamp_buffer_seconds=0,
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={"name": "id"},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
		)

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace()),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=SimpleNamespace(ping=lambda: ConnectorPingResult(ok=True, message="ok", details={}))),
			patch("sync.sync.service.runtime._iter_frappe_source_batches", return_value=iter([[]])),
			patch("sync.sync.service.runtime._iter_partner_source_batches", return_value=iter([[]])),
			patch("sync.sync.service.runtime._sync_partner_to_frappe") as mock_branch,
		):
			result = runtime._run_engine(SimpleNamespace(name="SYNC-1"), SimpleNamespace(name="RUN-1"), config=config)

		mock_branch.assert_called_once()
		self.assertEqual(result["sync_type"], "Frappe <- Partner")

	def test_run_engine_streams_a_to_b_batches_without_legacy_list_getters(self):
		config = SimpleNamespace(
			name="SYNC-A2B",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe -> Partner",
			cron=None,
			filters=None,
			batch_size=2,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			timestamp_buffer_seconds=0,
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={"name": "id"},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
		)
		recorded_batches = []

		def fake_sync(**kwargs):
			recorded_batches.append([row["name"] for row in kwargs["frappe_records"]])
			kwargs["source_keys"].update((row["name"],) for row in kwargs["frappe_records"])

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace()),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=SimpleNamespace(ping=lambda: ConnectorPingResult(ok=True, message="ok", details={}))),
			patch("sync.sync.service.runtime._get_frappe_source_records", side_effect=AssertionError("legacy list getter should not be used")),
			patch("sync.sync.service.runtime._get_partner_source_records", side_effect=AssertionError("legacy list getter should not be used")),
			patch("sync.sync.service.runtime._iter_frappe_source_batches", return_value=iter([[{"name": "TASK-1"}], [{"name": "TASK-2"}]])),
			patch("sync.sync.service.runtime._iter_partner_source_batches", return_value=iter([[{"id": "TASK-OLD"}]])),
			patch("sync.sync.service.runtime._sync_frappe_to_partner", side_effect=fake_sync) as mock_sync,
			patch("sync.sync.service.runtime._flush_pending_run_writes"),
		):
			runtime._run_engine(SimpleNamespace(name="SYNC-A2B"), SimpleNamespace(name="RUN-1"), config=config)

		self.assertEqual(recorded_batches, [["TASK-1"], ["TASK-2"]])
		self.assertEqual(mock_sync.call_count, 2)

	def test_run_engine_streams_bidirectional_batches_into_indexes(self):
		config = SimpleNamespace(
			name="SYNC-BI",
			doctype="Task",
			partner="PARTNER-1",
			sync_type="Frappe <-> Partner",
			cron=None,
			filters=None,
			batch_size=2,
			create_new=True,
			delete_missing=False,
			use_last_sync_date=False,
			conflict_policy="newest_wins",
			timestamp_buffer_seconds=0,
			table_name="tabTask",
			read_query=None,
			match_fields=["name"],
			mapping={"name": "id"},
			value_mapping={},
			frappe_modified_fields=["modified"],
			partner_modified_fields=["updated_at"],
		)

		with (
			patch("sync.sync.service.runtime.frappe.get_doc", return_value=SimpleNamespace()),
			patch("sync.sync.service.runtime.get_connector_for_partner", return_value=SimpleNamespace(ping=lambda: ConnectorPingResult(ok=True, message="ok", details={}))),
			patch("sync.sync.service.runtime._get_frappe_source_records", side_effect=AssertionError("legacy list getter should not be used")),
			patch("sync.sync.service.runtime._get_partner_source_records", side_effect=AssertionError("legacy list getter should not be used")),
			patch("sync.sync.service.runtime._iter_frappe_source_batches", return_value=iter([[{"name": "TASK-1"}], [{"name": "TASK-2"}]])),
			patch("sync.sync.service.runtime._iter_partner_source_batches", return_value=iter([[{"id": "TASK-1"}], [{"id": "TASK-3"}]])),
			patch("sync.sync.service.runtime._sync_bidirectional") as mock_sync,
		):
			runtime._run_engine(SimpleNamespace(name="SYNC-BI"), SimpleNamespace(name="RUN-1"), config=config)

		self.assertEqual(
			mock_sync.call_args.kwargs["frappe_records"],
			{("TASK-1",): {"name": "TASK-1"}, ("TASK-2",): {"name": "TASK-2"}},
		)
		self.assertEqual(
			mock_sync.call_args.kwargs["partner_records"],
			{("TASK-1",): {"id": "TASK-1"}, ("TASK-3",): {"id": "TASK-3"}},
		)

	def test_apply_update_helpers_cover_success_and_error_paths(self):
		config = SimpleNamespace(doctype="Task", match_fields=["name"], mapping={"name": "id"}, table_name="tabTask", read_query=None)
		logged = []

		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime._upsert_frappe_record", return_value="TASK-1"),
		):
			runtime._apply_partner_update(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=SimpleNamespace(upsert_record=lambda **kwargs: ConnectorWriteResult(ok=True, message="ok")),
				stats=runtime.SyncStats(),
				dry_run=True,
				frappe_record={"name": "TASK-1"},
				partner_record={"id": "TASK-1"},
				partner_payload={"id": "TASK-1"},
				changes=[("id", None, "TASK-1")],
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="updated",
			)
			runtime._apply_frappe_update(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				stats=runtime.SyncStats(),
				dry_run=False,
				frappe_record={"name": "TASK-1"},
				partner_record={"id": "TASK-1"},
				frappe_payload={"name": "TASK-1"},
				changes=[("name", None, "TASK-1")],
				direction="Frappe <-> Partner",
				action="conflict",
				status="conflict",
				message="partner won",
			)

		self.assertEqual([entry["action"] for entry in logged], ["updated", "conflict"])

		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime._upsert_frappe_record", side_effect=RuntimeError("frappe boom")),
		):
			runtime._apply_frappe_update(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				stats=runtime.SyncStats(),
				dry_run=False,
				frappe_record={"name": "TASK-1"},
				partner_record={"id": "TASK-1"},
				frappe_payload={"name": "TASK-1"},
				changes=[],
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="unused",
			)

		self.assertEqual(logged[-1]["action"], "error")

		with patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)):
			runtime._apply_partner_update(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=SimpleNamespace(upsert_record=lambda **kwargs: ConnectorWriteResult(ok=False, message="nope")),
				stats=runtime.SyncStats(),
				dry_run=False,
				frappe_record={"name": "TASK-1"},
				partner_record={"id": "TASK-1"},
				partner_payload={"id": "TASK-1"},
				changes=[],
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="unused",
			)

		self.assertEqual(logged[-1]["action"], "error")

	def test_one_way_sync_helpers_cover_remaining_skip_error_and_no_change_paths(self):
		config = SimpleNamespace(
			name="SYNC-1",
			doctype="Task",
			match_fields=["name"],
			mapping={"name": "id", "status": "state"},
			value_mapping={},
			create_new=False,
			delete_missing=False,
			table_name="tabTask",
			read_query=None,
		)
		logged = []

		with patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)):
			runtime._sync_frappe_to_partner(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=SimpleNamespace(upsert_record=lambda **kwargs: ConnectorWriteResult(ok=False, message="boom")),
				frappe_records=[{"name": "TASK-1", "status": "open"}, {"name": "TASK-2", "status": "open"}],
				partner_records=[{"id": "TASK-1", "state": "open"}, {"id": "TASK-2", "state": "closed"}],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe -> Partner",
				full_sync=False,
			)

		self.assertEqual([entry["action"] for entry in logged], ["skipped", "error"])
		logged.clear()

		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime._upsert_frappe_record", side_effect=RuntimeError("write failed")),
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				partner_records=[
					{"state": "missing-id"},
					{"id": "TASK-1", "state": "open"},
					{"id": "TASK-2", "state": "closed"},
					{"id": "TASK-3", "state": "error"},
				],
				frappe_records=[
					{"name": "TASK-1", "status": "open"},
					{"name": "TASK-2", "status": "open"},
				],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)

		self.assertEqual([entry["action"] for entry in logged], ["skipped", "error", "skipped"])
		logged.clear()

		with (
			patch("sync.sync.service.runtime._register_and_log", side_effect=lambda **kwargs: logged.append(kwargs)),
			patch("sync.sync.service.runtime._index_frappe_records", return_value={}),
			patch("sync.sync.service.runtime._index_partner_records", return_value={(None,): {"state": "missing-id"}}),
		):
			runtime._sync_partner_to_frappe(
				run_doc=SimpleNamespace(name="RUN-1"),
				config=config,
				connector=object(),
				partner_records=[],
				frappe_records=[],
				dry_run=False,
				stats=runtime.SyncStats(),
				label_direction="Frappe <- Partner",
				full_sync=False,
			)

		self.assertEqual(logged[0]["action"], "error")

	def test_low_level_runtime_helpers_cover_pagination_and_audit_doc_building(self):
		pages = [
			[{"name": "A"}],
			[{"name": "B"}],
			[],
		]
		with patch("sync.sync.service.runtime.frappe.get_all", side_effect=pages):
			records = runtime._get_frappe_records("Task", fields=["name"], filters=None, or_filters=None, batch_size=1)
		self.assertEqual(records, [{"name": "A"}, {"name": "B"}])

		run_meta = MetaWithFields(
			[
				SimpleNamespace(fieldname="sync_definition", fieldtype="Link"),
				SimpleNamespace(fieldname="status", fieldtype="Data"),
				SimpleNamespace(fieldname="trigger_type", fieldtype="Data"),
				SimpleNamespace(fieldname="dry_run", fieldtype="Check"),
				SimpleNamespace(fieldname="started_at", fieldtype="Datetime"),
				SimpleNamespace(fieldname="sync_type", fieldtype="Data"),
				SimpleNamespace(fieldname="sync_partner", fieldtype="Link"),
			]
		)
		run_item_meta = MetaWithFields(
			[
				SimpleNamespace(fieldname="sync_run", fieldtype="Link"),
				SimpleNamespace(fieldname="sync_definition", fieldtype="Link"),
				SimpleNamespace(fieldname="action", fieldtype="Data"),
				SimpleNamespace(fieldname="status", fieldtype="Data"),
				SimpleNamespace(fieldname="message", fieldtype="Data"),
				SimpleNamespace(fieldname="write_direction", fieldtype="Data"),
				SimpleNamespace(fieldname="document_name", fieldtype="Data"),
				SimpleNamespace(fieldname="record_key", fieldtype="Data"),
				SimpleNamespace(fieldname="source_id", fieldtype="Data"),
				SimpleNamespace(fieldname="target_id", fieldtype="Data"),
				SimpleNamespace(fieldname="change_count", fieldtype="Int"),
				SimpleNamespace(fieldname="changed_fields", fieldtype="Small Text"),
				SimpleNamespace(fieldname="frappe_before_payload", fieldtype="Long Text"),
				SimpleNamespace(fieldname="partner_before_payload", fieldtype="Long Text"),
				SimpleNamespace(fieldname="written_after_payload", fieldtype="Long Text"),
			]
		)
		docs = [FakeInsertDoc(name="RUN-1", doctype="Sync Run"), FakeInsertDoc(name="ITEM-1", doctype="Sync Run Item")]

		get_doc = Mock(side_effect=docs)
		with (
			patch(
				"sync.sync.service.runtime.frappe",
				new=_runtime_frappe_stub(
					get_meta=Mock(return_value=run_item_meta),
					get_doc=get_doc,
				),
			),
			patch("sync.sync.service.runtime.now_datetime", return_value=datetime(2026, 3, 17, 12, 0)),
		):
			run_doc = runtime._create_run_doc(SimpleNamespace(name="SYNC-1", get=lambda key, default=None: {"sync_type": "Frappe -> Partner", "partner": "PARTNER-1"}.get(key, default)), status="Queued", trigger="manual", dry_run=True)
			item_doc = runtime._create_run_item(
				run_doc=run_doc,
				config=SimpleNamespace(match_fields=["name"], mapping={"name": "id"}, capture_audit_payloads=1),
				sync_definition_name="SYNC-1",
				action="created",
				status="success",
				frappe_record={"name": "TASK-1"},
				partner_record={"id": "TASK-1"},
				message="created",
				direction="Frappe -> Partner",
				partner_before_record=None,
				written_after_record={"id": "TASK-1"},
				changes=[("status", "Open", "Closed")],
			)

		self.assertTrue(run_doc.inserted)
		self.assertTrue(item_doc.inserted)
		item_payload = get_doc.call_args_list[1].args[0]
		self.assertEqual(item_payload["write_direction"], "Frappe -> Partner")
		self.assertEqual(item_payload["change_count"], 1)
		self.assertEqual(item_payload["changed_fields"], "status")
		self.assertEqual(item_payload["frappe_before_payload"], '{"name": "TASK-1"}')
		self.assertEqual(item_payload["partner_before_payload"], "null")
		self.assertEqual(item_payload["written_after_payload"], '{"id": "TASK-1"}')

	def test_get_partner_source_records_returns_records_for_full_sync(self):
		config = SimpleNamespace(table_name="tabTask", read_query=None, batch_size=10, match_fields=["name"], partner_modified_fields=["updated_at"])
		context = SimpleNamespace(is_delta_sync=False, delta_since=None)
		records = [{"id": "TASK-1"}]

		with patch("sync.sync.service.runtime._iter_partner_record_batches", return_value=iter([records])):
			self.assertEqual(runtime._get_partner_source_records(config, object(), context), records)

	def test_fetch_partner_records_raises_on_partial_load_failure(self):
		class FlakyConnector:
			def __init__(self):
				self.calls = []

			def fetch_records(self, *, source, query, batch_size, cursor, key_fields):
				self.calls.append(cursor)
				if cursor is None:
					return {"records": [{"id": "TASK-1"}], "next_cursor": "page-2"}
				raise RuntimeError("partner fetch exploded")

		connector = FlakyConnector()

		with self.assertRaisesRegex(RuntimeError, "Partner source load failed at cursor 'page-2' after 1 records."):
			runtime._fetch_partner_records(
				connector=connector,
				source="tabTask",
				query=None,
				batch_size=1,
				key_fields=["name"],
			)

		self.assertEqual(connector.calls, [None, "page-2"])

	def test_runtime_metadata_helpers_cover_noops_and_status_updates(self):
		doc = FakeDoc(name="SYNC-1", doctype="Sync Definition")
		meta = MetaWithFields(
			[
				SimpleNamespace(fieldname="last_run", fieldtype="Data"),
				SimpleNamespace(fieldname="last_run_status", fieldtype="Data"),
				SimpleNamespace(fieldname="last_run_summary", fieldtype="Data"),
				SimpleNamespace(fieldname="last_sync_at", fieldtype="Datetime"),
				SimpleNamespace(fieldname="next_run_at", fieldtype="Datetime"),
			]
		)

		with (
			patch("sync.sync.service.runtime.frappe", new=_runtime_frappe_stub(get_meta=lambda *_args, **_kwargs: meta)),
			patch("sync.sync.service.runtime.croniter", side_effect=lambda expr, now: SimpleNamespace(get_next=lambda _: datetime(2026, 3, 17, 13, 0))),
			patch("sync.sync.service.runtime.now_datetime", return_value=datetime(2026, 3, 17, 12, 0)),
		):
			runtime._update_definition_runtime(doc, last_run="RUN-1", last_sync_at=datetime(2026, 3, 17, 12, 0), summary="ok")
			runtime._update_definition_failure(doc, last_run="RUN-2", error_message="Traceback\nRuntimeError: boom")
			runtime._set_next_run_at(doc, "*/15 * * * *")

		self.assertEqual(doc.values["last_run"], "RUN-2")
		self.assertEqual(doc.values["last_run_status"], "Error")
		self.assertEqual(doc.values["last_run_summary"], "RuntimeError: boom")
		self.assertEqual(doc.values["next_run_at"], datetime(2026, 3, 17, 13, 0))

		self.assertIn("processed=1", runtime._format_run_summary({"processed_count": 1}))
