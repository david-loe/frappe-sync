"""Configuration parity across Desk, portable documents, and runtime entrypoints."""

from __future__ import annotations

import ast
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import yaml

from sync.sync.doctype.sync_definition.sync_definition import SyncDefinition
from sync.sync.doctype.sync_partner.sync_partner import SyncPartner
from sync.sync.service import changes, configuration, definition_rules, models, time_utils, yaml_io
from sync.sync.service.execution import engine
from sync.tests.service_test_support import install_definition_metadata
from sync.tests.test_api import DummyDoc, _fake_meta


class TestSharedDefinitionValidation(unittest.TestCase):
	def setUp(self):
		install_definition_metadata(self)
		self.enterContext(patch.object(frappe, "get_meta", side_effect=_fake_meta))
		self.partner = {
			"doctype": "Sync Partner",
			"name": "PARTNER-1",
			"partner_type": "mssql",
			"time_zone": "Europe/Berlin",
		}
		self.partner_type = {"doctype": "Sync Partner Type", "name": "mssql"}
		self.definition = {
			"doctype": "Sync Definition",
			"name": "SYNC-1",
			"doctype_name": "Task",
			"partner": "PARTNER-1",
			"sync_type": "Frappe -> Partner",
			"match_mode": "Match Fields",
			"table_name": "tasks",
			"use_last_sync_date": 0,
			"match_fields": [{"frappe_field": "name"}],
			"field_mapping": [{"frappe_field": "name", "partner_field": "id"}],
		}
		self.db = SimpleNamespace(exists=Mock(return_value=False), commit=Mock())
		self.enterContext(patch.object(frappe, "db", self.db))
		self.get_doc = self.enterContext(patch.object(frappe, "get_doc"))
		self.insert = self.enterContext(patch.object(yaml_io, "_upsert_document_from_payload"))

	def payload(self, definition=None, partner=None):
		return yaml.safe_dump(
			{
				"version": 2,
				"sync_definition": definition if definition is not None else self.definition,
				"sync_partner": partner if partner is not None else self.partner,
				"sync_partner_type": self.partner_type,
			}
		)

	def test_valid_inputs_produce_the_same_normalized_config_without_writes(self):
		doc = configuration.definition_input(self.definition)
		SyncDefinition.validate(doc)
		from_document = configuration._build_definition_config(doc)
		from_payload = configuration._build_definition_config(self.definition)
		from_config = configuration._coerce_config(from_payload)
		self.assertEqual(asdict(from_document), asdict(from_config))
		self.assertEqual(
			from_payload.mapping, {"name": {"partner_field": "id", "direction": "Frappe -> Partner"}}
		)
		self.assertTrue(from_payload.update_existing)
		self.assertTrue(yaml_io.preview_import_sync_definition_yaml(self.payload())["can_import"])
		self.get_doc.assert_not_called()
		self.insert.assert_not_called()
		self.db.commit.assert_not_called()

	def test_invalid_document_rules_are_shared_by_save_yaml_and_runtime(self):
		cases = {
			"missing matches": {"match_fields": []},
			"unmapped match": {"match_fields": [{"frappe_field": "subject"}]},
			"duplicate source fields": {
				"field_mapping": [{"frappe_field": "name", "partner_field": "id"}] * 2
			},
			"duplicate target fields": {
				"field_mapping": [
					{"frappe_field": "name", "partner_field": "id"},
					{"frappe_field": "subject", "partner_field": "id"},
				]
			},
			"missing target": {"table_name": None},
			"query with deletes": {"read_query": "select * from tasks", "delete_missing": 1},
			"delta without timestamps": {"use_last_sync_date": 1},
			"timestamp overlap": {"partner_modified_field": "id"},
			"missing identity fields": {"match_mode": "Identity Fields"},
			"invalid id strategy": {"partner_create_id_strategy": "sequence"},
			"invalid match mode": {"one_way_match_mode": "random"},
			"invalid source mode": {"frappe_source_mode": "Unknown"},
			"duplicate computed fields": {
				"computed_fields": [{"field_name": "label", "template": "fixed"}] * 2
			},
			"unknown child table": {
				"field_mapping": [{"frappe_field": "items.1.code", "partner_field": "id"}]
			},
			"invalid filters": {"filter_expression": "42"},
			"missing fallback": {
				"field_mapping": [
					{"frappe_field": "name", "partner_field": "id", "unmapped_action": "Use Fallback Value"}
				]
			},
			"invalid hook": {
				"frappe_write_hooks": [
					{"enabled": 1, "event": "After Match", "hook_type": "Built-in Action", "action": "Submit"}
				]
			},
			"match direction": {
				"sync_type": "Frappe <-> Partner",
				"field_mapping": [
					{"frappe_field": "name", "partner_field": "id", "direction": "Frappe <- Partner"}
				],
			},
		}
		for label, updates in cases.items():
			with self.subTest(label=label):
				definition = {**deepcopy(self.definition), **updates}
				with self.assertRaises(frappe.ValidationError) as desk:
					SyncDefinition.validate(configuration.definition_input(definition))
				with self.assertRaises(frappe.ValidationError) as runtime:
					configuration._build_definition_config(definition)
				preview = yaml_io.preview_import_sync_definition_yaml(self.payload(definition))
				self.assertFalse(preview["can_import"])
				self.assertEqual(preview["documents"]["Sync Definition"]["hint"], str(desk.exception))
				self.assertEqual(str(runtime.exception), str(desk.exception))
				with self.assertRaises(frappe.ValidationError):
					yaml_io.import_sync_definition_yaml(self.payload(definition))
		self.insert.assert_not_called()
		self.db.commit.assert_not_called()

	def test_direct_config_and_context_cannot_bypass_validation_before_source_access(self):
		valid = configuration._build_definition_config(self.definition)
		for updates in (
			{"match_fields": []},
			{"table_name": None},
			{"one_way_match_mode": "random"},
			{"use_last_sync_date": True},
		):
			with self.subTest(updates=updates):
				config = replace(valid, **updates)
				with self.assertRaises(frappe.ValidationError):
					configuration._coerce_config(config)
				with self.assertRaises(frappe.ValidationError):
					engine._run_engine(
						SimpleNamespace(name="SYNC-1"),
						SimpleNamespace(name="RUN-1"),
						context=models.SyncContext(config, False, None),
					)
		self.get_doc.assert_not_called()

	def test_invalid_time_zone_is_rejected_by_partner_yaml_and_runtime(self):
		config = configuration._build_definition_config(self.definition)
		for zone in ("Mars/Olympus", "/etc/passwd"):
			with self.subTest(zone=zone):
				with self.assertRaises(frappe.ValidationError):
					SyncPartner.validate(SimpleNamespace(time_zone=zone))
				with self.assertRaises(frappe.ValidationError):
					configuration._coerce_config(replace(config, partner_time_zone=zone))
				preview = yaml_io.preview_import_sync_definition_yaml(
					self.payload(partner={**self.partner, "time_zone": zone})
				)
				self.assertFalse(preview["can_import"])
				self.assertEqual(preview["documents"]["Sync Partner"]["status"], "invalid")
		self.assertEqual(time_utils._normalize_time_zone_name(" Europe/Berlin "), "Europe/Berlin")

	def test_overwrite_validates_effective_fields_and_replaces_supplied_child_tables(self):
		self.db.exists.side_effect = lambda doctype, name: doctype == "Sync Definition"
		self.get_doc.return_value = DummyDoc(self.definition)
		partial = {"name": "SYNC-1", "match_mode": "Match Fields", "batch_size": 25}
		self.assertTrue(
			yaml_io.preview_import_sync_definition_yaml(self.payload(partial), overwrite=True)["can_import"]
		)
		self.assertFalse(
			yaml_io.preview_import_sync_definition_yaml(
				self.payload({**partial, "field_mapping": []}), overwrite=True
			)["can_import"]
		)
		# Without overwrite the existing, valid rows are retained.
		self.assertTrue(
			yaml_io.preview_import_sync_definition_yaml(
				self.payload({**partial, "field_mapping": []}), overwrite=False
			)["can_import"]
		)
		self.insert.assert_not_called()
		self.db.commit.assert_not_called()

	def test_script_save_permission_is_separate_from_runtime_semantics(self):
		definition = {
			**self.definition,
			"frappe_write_hooks": [
				{
					"enabled": 1,
					"event": "After Insert",
					"hook_type": "Custom Script",
					"script": "result = None",
				}
			],
		}
		with (
			patch("sync.sync.service.config_access.server_script_enabled", return_value=True),
			patch.object(definition_rules, "_current_user_is_system_manager", return_value=False),
		):
			config = configuration._build_definition_config(definition)
			configuration._coerce_config(config)
			with self.assertRaisesRegex(frappe.ValidationError, "System Manager"):
				SyncDefinition.validate(configuration.definition_input(definition))
			self.assertFalse(
				yaml_io.preview_import_sync_definition_yaml(self.payload(definition))["can_import"]
			)
		self.insert.assert_not_called()

	def test_direct_config_requires_configured_fallback_value(self):
		config = configuration._build_definition_config(self.definition)
		with self.assertRaisesRegex(frappe.ValidationError, "Fallback Value is required"):
			configuration._coerce_config(
				replace(config, value_mapping_fallbacks={"name": {"action": "fallback", "value": None}})
			)

	def test_conflict_decision_handles_delta_newest_and_buffered_ties(self):
		config = configuration._build_definition_config(self.definition)
		config = replace(
			config,
			sync_type="Frappe <-> Partner",
			partner_modified_field="updated_at",
			partner_creation_field="created_at",
			partner_time_zone="UTC",
			timestamp_buffer_ms=100,
		)
		stamp = datetime(2026, 3, 17, 12)
		cases = [
			(stamp, stamp - timedelta(hours=2), stamp - timedelta(hours=1), "Manual", "frappe_changed"),
			(stamp - timedelta(hours=2), stamp, stamp - timedelta(hours=1), "Manual", "partner_changed"),
			(stamp, stamp - timedelta(seconds=1), None, "Manual", "frappe_newest"),
			(stamp, stamp + timedelta(seconds=1), None, "Manual", "partner_newest"),
			(stamp, stamp + timedelta(milliseconds=50), None, "Manual", "manual"),
			(stamp, stamp, None, "Frappe Wins", "frappe_tie"),
			(stamp, stamp, None, "Partner Wins", "partner_tie"),
		]
		for frappe_stamp, partner_stamp, last_sync, tie, expected in cases:
			with self.subTest(expected=expected):
				decision = changes.resolve_conflict(
					replace(config, timestamp_tie_breaker=tie),
					frappe_record={"modified": frappe_stamp},
					partner_record={"updated_at": partner_stamp},
					last_successful_sync=last_sync,
					site_time_zone="UTC",
				)
				self.assertEqual(decision, expected)
		self.get_doc.assert_not_called()
		self.db.commit.assert_not_called()


class TestServiceDependencies(unittest.TestCase):
	def test_service_imports_are_acyclic_and_do_not_depend_on_controllers(self):
		root = Path(__file__).parents[1] / "sync" / "service"
		graph = {}
		for path in root.rglob("*.py"):
			module = ".".join(path.relative_to(root).with_suffix("").parts)
			edges = set()
			for node in ast.walk(ast.parse(path.read_text())):
				if isinstance(node, ast.ImportFrom):
					self.assertNotIn("sync.sync.doctype", node.module or "")
					if (node.module or "").startswith("sync.sync.service"):
						prefix = node.module.removeprefix("sync.sync.service").strip(".")
						for alias in node.names:
							candidate = ".".join(filter(None, (prefix, alias.name)))
							edges.add(
								candidate
								if (root / (candidate.replace(".", "/") + ".py")).exists()
								else prefix
							)
			graph[module] = edges
		visited = set()

		def visit(module, active):
			self.assertNotIn(module, active, f"Import cycle through {module}")
			if module in visited:
				return
			for dependency in graph.get(module, ()):
				visit(dependency, active | {module})
			visited.add(module)

		for module in graph:
			visit(module, set())
		self.assertFalse((root / "runtime.py").exists())
