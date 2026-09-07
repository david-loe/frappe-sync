"""Shared environment patches for tests spanning several service modules."""

from contextlib import ExitStack
from importlib import import_module
from unittest.mock import patch


class ServiceDependencyPatch:
	def __init__(self, name, *args, **kwargs):
		self.name, self.args, self.kwargs = name, args, kwargs

	def __enter__(self):
		self.stack = ExitStack()
		modules = [
			import_module("sync.sync.service." + name)
			for name in [
				"definition_rules",
				"audit",
				"changes",
				"config_access",
				"configuration",
				"execution.bidirectional",
				"execution.engine",
				"execution.one_way",
				"execution.sources",
				"execution.writes",
				"management",
				"mapping",
				"mapping_rules",
				"matching",
				"metadata",
				"models",
				"orchestrator",
				"query_templates",
				"scheduler",
				"scheduling",
				"time_utils",
				"values",
				"yaml_io",
			]
		]
		modules = [module for module in modules if hasattr(module, self.name)]
		try:
			replacement = self.stack.enter_context(
				patch.object(modules[0], self.name, *self.args, **self.kwargs)
			)
			for module in modules[1:]:
				self.stack.enter_context(patch.object(module, self.name, replacement))
			return replacement
		except BaseException:
			self.stack.close()
			raise

	def __exit__(self, *args):
		return self.stack.__exit__(*args)


def patch_service_dependency(name, *args, **kwargs):
	return ServiceDependencyPatch(name, *args, **kwargs)


def install_definition_metadata(test):
	from types import SimpleNamespace

	import frappe

	fields = [
		SimpleNamespace(fieldname=name, fieldtype="Data", options=None)
		for name in (
			"subject",
			"status",
			"title",
			"partner_nr",
			"partner_id",
			"external_id",
			"frappe_name",
			"updated_at",
			"custom_modified",
		)
	]
	test.enterContext(
		patch.object(frappe, "get_meta", return_value=SimpleNamespace(fields=fields, is_submittable=False))
	)

	def reject(message, *args, **kwargs):
		raise frappe.ValidationError(message)

	test.enterContext(patch.object(frappe, "throw", side_effect=reject))
