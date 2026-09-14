from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from sync.sync.service.execution import scripted
from sync.sync.service.execution.script_support import fingerprint, record_key
from sync.tests.test_runtime_execution import _has_frappe_site_context
from sync.tests.test_scripted_processing import config

SCRIPT = """
hash = helpers.fingerprint(frappe_payload)
if state.get('hash') == hash:
    action = 'skip'
elif not frappe_payload.get('accounts'):
    action = 'reverse' if existing_documents else 'skip'
else:
    action = 'replace' if existing_documents else 'create'
result = {'action': action, 'submit': True, 'state': {'hash': hash}}
"""


@unittest.skipUnless(_has_frappe_site_context(), "requires Frappe site context")
class TestScriptedProcessingDatabase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.enterContext(patch("frappe.utils.safe_exec.is_safe_exec_enabled", return_value=True))
		suffix = frappe.generate_hash(length=8)
		self.savepoint = "scripted_" + suffix
		frappe.db.savepoint(self.savepoint)
		self.addCleanup(frappe.db.rollback, save_point=self.savepoint)
		partner = frappe.get_doc(
			{
				"doctype": "Sync Partner",
				"partner_name": "Script test " + suffix,
				"partner_type": "mssql",
				"host": "localhost",
				"database_name": "test",
				"username": "test",
			}
		).insert()
		self.definition = frappe.get_doc(
			{
				"doctype": "Sync Definition",
				"title": "Script test " + suffix,
				"partner": partner.name,
				"sync_type": "Frappe <- Partner",
				"doctype_name": "Journal Entry",
				"frequency_cron": "0 0 1 1 *",
				"enabled": 0,
				"table_name": "source",
				"use_last_sync_date": 0,
				"match_fields": [{"frappe_field": "user_remark"}],
				"field_mapping": [
					{"frappe_field": "user_remark", "partner_field": "id", "direction": "Frappe <- Partner"}
				],
			}
		).insert()
		mapping = {
			field: {"partner_field": source, "direction": "Frappe <- Partner"}
			for field, source in [
				("user_remark", "id"),
				("company", "company"),
				("posting_date", "posting_date"),
				("accounts", "accounts"),
			]
		}
		self.config = config(
			name=self.definition.name,
			doctype="Journal Entry",
			match_fields=["user_remark"],
			mapping=mapping,
			record_processing_script=SCRIPT,
		)
		self.ident = "scripted " + suffix

	def record(self, amount):
		return {
			"id": self.ident,
			"company": "_Test Company",
			"posting_date": frappe.utils.nowdate(),
			"accounts": (
				[]
				if not amount
				else [
					{
						"account": "Cash - _TC",
						"debit_in_account_currency": amount,
						"cost_center": "Main - _TC",
					},
					{
						"account": "Earnest Money - _TC",
						"credit_in_account_currency": amount,
						"cost_center": "Main - _TC",
					},
				]
			),
		}

	def transition(self, amount, expected):
		record = self.record(amount)
		plan = scripted.plan_record(self.config, record)
		self.assertEqual(plan["action"], expected)
		return scripted.apply_plan(self.config, plan)

	def test_actual_journal_revisions_repeat_reverse_restore_and_state_name(self):
		original = self.transition(10, "create")[0]
		key = record_key(self.config, self.record(10))
		self.assertTrue(frappe.db.exists("Sync Record State", scripted.state_name(self.config, key)))
		revision = scripted.load_state(self.config, key)["revision"]
		self.assertEqual(self.transition(10, "skip"), [original])
		self.assertEqual(scripted.load_state(self.config, key)["revision"], revision)
		updated = self.transition(20, "replace")[0]
		restored = self.transition(10, "replace")[0]
		self.assertEqual(len({original, updated, restored}), 3)
		self.assertEqual(self.transition(0, "reverse"), [])
		self.assertEqual(self.transition(0, "skip"), [])
		self.transition(10, "create")
		self.assertEqual(
			frappe.db.count(
				"Journal Entry", {"reversal_of": ["in", [original, updated, restored]], "docstatus": 1}
			),
			3,
		)
		# Net ledger effect is the current revision, even after A -> B -> A -> empty -> A.
		entries = frappe.get_all("Journal Entry", filters={"user_remark": self.ident}, pluck="name")
		entries += frappe.get_all("Journal Entry", filters={"reversal_of": ["in", entries]}, pluck="name")
		ledger = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": ["in", entries], "account": "Cash - _TC", "is_cancelled": 0},
			fields=["debit", "credit"],
		)
		self.assertEqual(sum(r.debit - r.credit for r in ledger), 10)
		batch, states, documents = next(scripted.prepared_batches(self.config, [self.record(10)]))
		self.assertEqual(
			scripted.plan_record(self.config, batch[0], state_cache=states, document_cache=documents)[
				"action"
			],
			"skip",
		)

	def test_preview_reads_all_records_and_writes_nothing(self):
		from types import SimpleNamespace

		from sync.sync.service.models import SyncContext

		records = [
			self.record(10),
			{**self.record(20), "id": self.ident + " second"},
			{**self.record(30), "id": self.ident + " last", "bad": True},
		]
		cfg = replace(
			self.config,
			record_processing_script=SCRIPT
			+ "\nif partner_record.get('bad'):\n    result = {'action': 'error', 'message': 'late error'}",
		)
		reads = []

		def batches(**kwargs):
			reads.append(1)
			yield records

		before = {
			dt: frappe.db.count(dt)
			for dt in ("Sync Record State", "Journal Entry", "GL Entry", "Sync Run Item")
		}
		result = scripted.run_scripted(
			cfg,
			SimpleNamespace(iter_record_batches=batches),
			SyncContext(config=self.config, dry_run=True, last_successful_sync=None),
			preview_limit=1,
		)
		self.assertEqual(result["created_count"], 2)
		self.assertEqual(len(result["actions"]), 1)
		self.assertEqual(result["error_count"], 1)
		self.assertEqual(result["actions"][0]["result"]["action"], "error")
		self.assertEqual(len(reads), 2)
		self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})

	def test_absorbed_group_reverses_both_current_documents_and_owns_alias(self):
		first = self.transition(10, "create")[0]
		other = {**self.record(20), "id": self.ident + " other"}
		second = scripted.apply_plan(self.config, scripted.plan_record(self.config, other))[0]
		merged = {**self.record(30), "_sync": {"aliases": [{"id": other["id"]}]}}
		plan = scripted.plan_record(self.config, merged)
		self.assertEqual(set(plan["documents"]), {first, second})
		scripted.apply_plan(self.config, plan)
		self.assertEqual(
			frappe.db.count("Journal Entry", {"reversal_of": ["in", [first, second]], "docstatus": 1}), 2
		)
		alias = scripted.load_state(self.config, record_key(self.config, other))
		self.assertEqual(alias["state"]["alias_of"], record_key(self.config, merged))
		with self.assertRaisesRegex(frappe.ValidationError, "split"):
			scripted.plan_record(self.config, other)

	def test_unchanged_audit_suppression_keeps_counts_and_never_hides_warnings(self):
		from types import SimpleNamespace

		from sync.sync.service.models import SyncContext

		original = self.transition(10, "create")[0]
		record = self.record(10)
		cfg = replace(self.config, record_processing_script=SCRIPT + "\nresult['log_unchanged'] = False")
		connector = SimpleNamespace(iter_record_batches=lambda **kwargs: iter([[record]]))
		context = SyncContext(config=cfg, dry_run=True, last_successful_sync=None)
		with patch.object(scripted.audit, "_register_and_log") as log:
			result = scripted.run_scripted(cfg, connector, context)
			self.assertEqual(result["skipped_count"], 1)
			log.assert_not_called()
		record["_sync"] = {"warnings": ["fallback"]}
		key = record_key(cfg, record)
		old = scripted.load_state(cfg, key)
		scripted.persist_state(cfg, key, record, {}, old["state"], [original], old["revision"] + 1)
		with patch.object(scripted.audit, "_register_and_log") as log:
			scripted.run_scripted(cfg, connector, context)
			log.assert_called_once()
			self.assertIn("fallback", log.call_args.kwargs["message"])

	def test_definition_rename_cannot_orphan_processing_state(self):
		self.transition(10, "create")
		with self.assertRaisesRegex(frappe.ValidationError, "renamed"):
			self.definition.before_rename(self.definition.name, self.definition.name + " renamed")

	def test_external_reversal_is_detected_on_unchanged_source(self):
		original = self.transition(10, "create")[0]
		scripted.writes._reverse_journal_entry(source_name=original, posting_date=frappe.utils.nowdate())
		with self.assertRaisesRegex(frappe.ValidationError, "outside the sync"):
			scripted.plan_record(self.config, self.record(10))

	def test_failed_real_replacement_rolls_back_ledger_and_state(self):
		original = self.transition(10, "create")[0]
		before = scripted.load_state(self.config, record_key(self.config, self.record(10)))
		broken = self.record(20)
		broken["accounts"][1]["account"] = "Missing account for rollback test"
		plan = scripted.plan_record(self.config, broken)
		with self.assertRaises(frappe.LinkValidationError):
			scripted.apply_plan(self.config, plan)
		self.assertFalse(frappe.db.exists("Journal Entry", {"reversal_of": original}))
		self.assertEqual(scripted.load_state(self.config, record_key(self.config, self.record(10))), before)
