from __future__ import annotations

import unittest
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from sync.sync.service import configuration, definition_rules
from sync.sync.service.execution import script_support as support
from sync.sync.service.execution import scripted
from sync.sync.service.models import SyncDefinitionConfig
from sync.tests.service_test_support import install_definition_metadata


def config(**updates):
	values = dict(
		name="test",
		doctype="Task",
		partner="p",
		sync_type="Frappe <- Partner",
		cron=None,
		filters=None,
		batch_size=2,
		create_new=True,
		update_existing=True,
		delete_missing=False,
		use_last_sync_date=False,
		conflict_policy="newest_wins",
		timestamp_buffer_ms=0,
		table_name=None,
		read_query="SELECT id FROM records",
		match_fields=["subject"],
		mapping={"subject": {"partner_field": "id", "direction": "Frappe <- Partner"}},
		value_mapping={},
		record_processing_script="result = {'action': 'create'}",
	)
	values.update(updates)
	return SyncDefinitionConfig(**values)


class SandboxTest(unittest.TestCase):
	def setUp(self):
		self.enterContext(patch("frappe.utils.safe_exec.is_safe_exec_enabled", return_value=True))
		self.enterContext(patch("frappe.utils.safe_exec.safe_exec_flags", return_value=nullcontext()))

	def test_real_restricted_python_supports_local_grouping_and_decimal(self):
		result = support.execute_read_script(
			"""
def total(items):
    values = {}
    for key, amount in items:
        values[key] = values.get(key, helpers.decimal(0)) + helpers.decimal(amount)
    return values
result = total([('a', '0.1'), ('a', '0.2')])
""",
			{"helpers": support.ReadHelpers()},
		)
		self.assertEqual(str(result["a"]), "0.3")

	def test_no_frappe_io_import_or_private_helper_access(self):
		for code in [
			"result = frappe.db.set_value('Task','x','subject','bad')",
			"import os",
			"result = open('/tmp/x','w')",
			"result = helpers._config",
			"result = helpers.__class__",
			"result = getattr(helpers, '_config')",
		]:
			with (
				self.subTest(code=code),
				self.assertRaises((NameError, ImportError, SyntaxError, AttributeError)),
			):
				support.execute_read_script(code, {"helpers": support.ReadHelpers()})

	def test_select_only_queries(self):
		for query in ["SELECT 'DELETE FROM x' AS text", "WITH c AS (SELECT id FROM records) SELECT * FROM c"]:
			support.check_read_query(query)
		for query in [
			"DELETE FROM x",
			"SELECT 1; DELETE FROM x",
			"SELECT * INTO x FROM y",
			"WITH c AS (DELETE FROM x RETURNING *) SELECT * FROM c",
		]:
			with self.subTest(query=query), self.assertRaises(frappe.ValidationError):
				support.check_read_query(query)

	def test_source_failure_never_yields_partial_records(self):
		def batches(**kwargs):
			yield [{"id": "one"}]
			raise RuntimeError("read failed")

		connector = SimpleNamespace(iter_record_batches=batches)
		with self.assertRaisesRegex(RuntimeError, "read failed"):
			with support.prepare_source(config(), connector):
				self.fail("must finish source before returning")

	def test_unconsumed_source_is_rejected(self):
		connector = SimpleNamespace(iter_record_batches=lambda **kwargs: iter([[{"id": "one"}]]))
		with self.assertRaisesRegex(frappe.ValidationError, "complete source"):
			with support.prepare_source(
				config(partner_source_script="helpers.emit({'id':'fabricated'})"), connector
			):
				self.fail("source not consumed")

	def test_source_staging_verifies_then_returns_records(self):
		calls = []

		def batches(**kwargs):
			calls.append(kwargs)
			yield [{"id": "one"}, {"id": "two"}]

		with support.prepare_source(config(), SimpleNamespace(iter_record_batches=batches)) as records:
			self.assertEqual(len(calls), 2)
			self.assertEqual(list(records), [{"id": "one"}, {"id": "two"}])

	def test_prepared_source_reuses_normalization_but_rechecks_source(self):
		connector = SimpleNamespace(iter_record_batches=Mock(return_value=[[{"id": "one"}]]))
		with patch.object(support, "execute_read_script", wraps=support.execute_read_script) as execute:
			with support.prepare_source_snapshot(config(), connector) as source:
				self.assertEqual(list(source), [{"id": "one"}])
				with (
					support.reuse_prepared_source(source),
					support.prepare_source(config(), connector) as records,
				):
					self.assertEqual(list(records), [{"id": "one"}])
				execute.assert_called_once()
				self.assertEqual(connector.iter_record_batches.call_count, 3)

	def test_prepared_source_change_rebuilds_and_read_failure_propagates(self):
		connector = SimpleNamespace(iter_record_batches=Mock(return_value=[[{"id": "one"}]]))
		with support.prepare_source_snapshot(config(), connector) as source:
			connector.iter_record_batches.return_value = [[{"id": "two"}]]
			with (
				support.reuse_prepared_source(source),
				support.prepare_source(config(), connector) as records,
			):
				self.assertEqual(list(records), [{"id": "two"}])
			self.assertEqual(connector.iter_record_batches.call_count, 5)
			connector.iter_record_batches.side_effect = RuntimeError("offline")
			with support.reuse_prepared_source(source), self.assertRaisesRegex(RuntimeError, "offline"):
				with support.prepare_source(config(), connector):
					self.fail("failed read must not yield the staged source")

	def test_prepared_source_configuration_and_lookup_changes_invalidate(self):
		connector = SimpleNamespace(iter_record_batches=Mock(return_value=[[{"id": "one"}]]))
		cfg = config(
			partner_source_script="label = helpers.get_all('Task', fields=['subject'])[0]['subject']\nfor row in rows:\n    helpers.emit({'id': row['id'], 'label': label})"
		)
		with patch.object(support.ReadHelpers, "get_all", return_value=[{"subject": "old"}]) as lookup:
			with support.prepare_source_snapshot(cfg, connector) as source:
				lookup.return_value = [{"subject": "new"}]
				with support.reuse_prepared_source(source), support.prepare_source(cfg, connector) as records:
					self.assertEqual(next(records)["label"], "new")
				with self.assertRaises(support.SourceChanged):
					source.verify(replace(cfg, script_parameters={"changed": True}), connector)

	def test_prepared_source_rechecks_rendered_query_and_cannot_outlive_file(self):
		connector = SimpleNamespace(iter_record_batches=Mock(return_value=[[{"id": "one"}]]))
		with patch.object(
			support.query_templates, "resolve_read_query", return_value="SELECT id FROM old_table"
		) as query:
			with support.prepare_source_snapshot(config(), connector) as source:
				query.return_value = "SELECT id FROM new_table"
				connector.iter_record_batches.return_value = [[{"id": "two"}]]
				with (
					support.reuse_prepared_source(source),
					support.prepare_source(config(), connector) as records,
				):
					self.assertEqual(list(records), [{"id": "two"}])
				self.assertEqual(connector.iter_record_batches.call_count, 4)
			with self.assertRaises(support.SourceChanged):
				source.verify(config(), connector)

	def test_document_projection_validation_and_export(self):
		install_definition_metadata(self)
		with patch.object(definition_rules, "_server_script_enabled", return_value=True):
			for projection in [[], ["name"], ["subject"]]:
				cfg = configuration._coerce_config(config(record_processing_document_fields=projection))
				self.assertEqual(cfg.record_processing_document_fields, projection)
				doc = configuration.definition_input(configuration.definition_input_from_config(cfg))
				doc.title = cfg.name
				doc.enabled = 0
				self.assertEqual(
					definition_rules.as_export_dict(doc)["record_processing_document_fields"], projection
				)
			for projection in ["not json", {}, ["bad()"], [4]]:
				with self.subTest(projection=projection), self.assertRaises(frappe.ValidationError):
					configuration._coerce_config(config(record_processing_document_fields=projection))

	def test_concurrent_change_retries_before_any_target_write(self):
		calls = []

		def batches(**kwargs):
			calls.append(1)
			yield [{"id": "old" if len(calls) == 1 else "new"}]

		with support.prepare_source(config(), SimpleNamespace(iter_record_batches=batches)) as records:
			self.assertEqual(list(records), [{"id": "new"}])
		self.assertEqual(len(calls), 4)

	def test_alias_cannot_also_be_emitted_or_owned_twice(self):
		connector = SimpleNamespace(
			iter_record_batches=lambda **kwargs: iter([[{"id": "one"}, {"id": "two"}]])
		)
		for script in [
			"for row in rows:\n    helpers.emit({'id': row['id'], '_sync': {'aliases': [{'id': 'two'}]}})",
			"for row in rows:\n    helpers.emit({'id': row['id'], '_sync': {'aliases': [{'id': 'alias'}]}})",
		]:
			with self.subTest(script=script), self.assertRaises(frappe.ValidationError):
				with support.prepare_source(config(partner_source_script=script), connector):
					self.fail("Conflicting aliases must fail before writes")

	def test_duplicate_emitted_keys_are_rejected(self):
		connector = SimpleNamespace(
			iter_record_batches=lambda **kwargs: iter([[{"id": "same"}, {"id": "same"}]])
		)
		with self.assertRaisesRegex(frappe.ValidationError, "duplicate key"):
			with support.prepare_source(config(), connector):
				self.fail("duplicate key")

	def test_json_roundtrip_preserves_script_parameters(self):
		install_definition_metadata(self)
		with patch.object(definition_rules, "_server_script_enabled", return_value=True):
			original = config(script_parameters={"nested": {"a": [1, 2]}})
			normalized = configuration._coerce_config(original)
			self.assertEqual(normalized.script_parameters, original.script_parameters)
			self.assertEqual(normalized.record_processing_script, original.record_processing_script)

	def test_full_source_safety_and_permissions(self):
		install_definition_metadata(self)
		with patch.object(definition_rules, "_server_script_enabled", return_value=True):
			for updates in [
				{"use_last_sync_date": True},
				{"delete_missing": True},
				{"sync_type": "Frappe -> Partner", "table_name": "records"},
			]:
				with self.subTest(updates=updates), self.assertRaises(frappe.ValidationError):
					configuration._coerce_config(config(**updates))
		with (
			patch.object(definition_rules, "_current_user_is_system_manager", return_value=False),
			self.assertRaises(frappe.ValidationError),
		):
			definition_rules.validate_script_permissions(SimpleNamespace(partner_source_script="pass"))


class ActionPlanTest(SandboxTest):
	def plan(self, action, old=None, existing=None, **updates):
		cfg = config(
			record_processing_script=f"result = {{'action': '{action}', 'state': {{'value': 1}}}}", **updates
		)
		self.enterContext(patch.object(scripted, "load_state", return_value=old or {}))
		self.enterContext(
			patch.object(scripted.mapping, "_map_partner_to_frappe", return_value={"subject": "one"})
		)
		self.enterContext(patch.object(scripted.sources, "_load_frappe_match_candidates", return_value=[]))
		self.enterContext(
			patch.object(
				scripted.matching,
				"_build_frappe_match_lookup",
				return_value=SimpleNamespace(groups={}, identity_by_value={}),
			)
		)
		self.enterContext(
			patch.object(scripted.matching, "_find_existing_frappe_records", return_value=existing or [])
		)
		return cfg, scripted.plan_record(cfg, {"id": "one"})

	def test_create_disabled_is_enforced_even_for_script(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Create New"):
			self.plan("create", create_new=False)

	def test_reverse_requires_existing_journal_and_update_flag(self):
		with self.assertRaisesRegex(frappe.ValidationError, "existing Journal Entries"):
			self.plan("reverse")

	def test_error_never_applies_or_updates_state(self):
		persist = Mock()
		with patch.object(scripted, "persist_state", persist), self.assertRaises(frappe.ValidationError):
			scripted.apply_plan(config(), {"action": "error", "message": "unbalanced"})
		persist.assert_not_called()

	def test_failed_replacement_rolls_back_reversal_and_does_not_advance_state(self):
		events = []
		from contextlib import contextmanager

		@contextmanager
		def transaction():
			old = list(events)
			try:
				yield
			except Exception:
				events[:] = old
				raise

		plan = {
			"action": "replace",
			"key": "one",
			"old": {"revision": 2},
			"documents": ["old"],
			"existing": [{"name": "old", "modified": "stamp"}],
			"posting_date": "2025-01-01",
			"payload": {},
			"submit": True,
		}
		with (
			patch.object(scripted, "load_state", return_value={"revision": 2}),
			patch.object(scripted.writes, "_frappe_write_savepoint", transaction),
			patch.object(scripted.frappe, "get_doc", return_value=SimpleNamespace(modified="stamp")),
			patch.object(scripted.frappe, "db", SimpleNamespace(exists=lambda *a: False)),
			patch.object(
				scripted.writes, "_reverse_journal_entry", side_effect=lambda **k: events.append("reverse")
			),
			patch.object(
				scripted.writes, "_upsert_frappe_record", side_effect=RuntimeError("invalid account")
			),
			patch.object(scripted, "persist_state") as persist,
		):
			with self.assertRaisesRegex(RuntimeError, "invalid account"):
				scripted.apply_plan(config(doctype="Journal Entry"), plan)
			self.assertEqual(events, [])
			persist.assert_not_called()
