from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import frappe

from sync.sync.constants import (
	FRAPPE_SOURCE_MODE_PYTHON_SCRIPT,
	MATCH_MODE_IDENTITY_FIELDS,
	ONE_WAY_MATCH_ALL,
	SYNC_PARTNER,
)
from sync.sync.service import audit as audit_service
from sync.sync.service import config_access as config_access_service
from sync.sync.service import configuration as configuration_service
from sync.sync.service import mapping as mapping_service
from sync.sync.service import matching as matching_service
from sync.sync.service.connectors import get_connector_for_partner
from sync.sync.service.execution import bidirectional as bidirectional_service
from sync.sync.service.execution import one_way as one_way_service
from sync.sync.service.execution import sources as sources_service
from sync.sync.service.models import (
	SyncContext,
	SyncDefinitionConfig,
	SyncStats,
)


def _run_engine(
	sync_definition_doc: Any,
	run_doc: Any,
	*,
	context: SyncContext | None = None,
	config: SyncDefinitionConfig | Any | None = None,
	dry_run: bool = False,
	last_successful_sync: datetime | None = None,
) -> dict[str, Any]:
	if context is None:
		if config is None:
			raise ValueError("Either context or config must be provided.")
		config_obj = configuration_service._coerce_config(config)
		context = SyncContext(
			config=config_obj,
			dry_run=dry_run,
			last_successful_sync=last_successful_sync,
		)
	else:
		config_obj = configuration_service._coerce_config(context.config)
	config = config_obj
	partner_doc = frappe.get_doc(SYNC_PARTNER, config.partner)
	config = configuration_service._merge_partner_runtime_settings(config, partner_doc)
	context = replace(context, config=config)
	mapping_context = mapping_service._build_runtime_mapping_context(config)
	connector = get_connector_for_partner(partner_doc)
	ping = connector.ping()
	if not ping.ok:
		raise frappe.ValidationError(f"Partner connector validation failed: {ping.message}")

	stats = SyncStats()
	if config.sync_type == "Frappe -> Partner":
		partner_batches = sources_service._iter_partner_source_batches(
			config, connector, context, apply_delta_filter=False
		)
		if config.delete_missing and context.is_full_sync:
			partner_records = [record for batch in partner_batches for record in batch]
		elif config_access_service._config_one_way_match_mode(config) == ONE_WAY_MATCH_ALL:
			partner_records = [record for batch in partner_batches for record in batch]
		else:
			partner_records = matching_service._build_partner_index_from_batches(config, partner_batches)
		partner_lookup = matching_service._build_partner_match_lookup(config, partner_records)
		if config.delete_missing and context.is_full_sync:
			frappe_source = [
				record
				for batch in sources_service._iter_frappe_source_batches(config, context)
				for record in batch
			]
			complete_source_keys = matching_service._frappe_source_key_set_from_batches(
				config,
				sources_service._iter_frappe_source_batches(
					config,
					context,
					apply_delta_filter=False,
					use_script_source=False,
				),
			)
			one_way_service._sync_frappe_to_partner(
				run_doc=run_doc,
				config=config,
				connector=connector,
				frappe_records=frappe_source,
				partner_records=partner_records,
				partner_lookup=partner_lookup,
				mapping_context=mapping_context,
				dry_run=context.dry_run,
				stats=stats,
				label_direction="Frappe -> Partner",
				full_sync=True,
				source_keys=complete_source_keys,
			)
		else:
			source_keys: set[tuple[Any, ...]] = set()
			for frappe_batch in sources_service._iter_frappe_source_batches(config, context):
				one_way_service._sync_frappe_to_partner(
					run_doc=run_doc,
					config=config,
					connector=connector,
					frappe_records=frappe_batch,
					partner_records=partner_records,
					partner_lookup=partner_lookup,
					mapping_context=mapping_context,
					dry_run=context.dry_run,
					stats=stats,
					label_direction="Frappe -> Partner",
					full_sync=False,
					source_keys=source_keys,
				)
			audit_service._flush_pending_run_writes(run_doc, force=True)
	elif config.sync_type == "Frappe <- Partner":
		if config.delete_missing and context.is_full_sync:
			frappe_records = sources_service._get_frappe_source_records(
				config,
				context,
				apply_delta_filter=False,
				use_script_source=False,
			)
			frappe_lookup = matching_service._build_frappe_match_lookup(config, frappe_records)
			partner_source = [
				record
				for batch in sources_service._iter_partner_source_batches(config, connector, context)
				for record in batch
			]
			one_way_service._sync_partner_to_frappe(
				run_doc=run_doc,
				config=config,
				connector=connector,
				partner_records=partner_source,
				frappe_records=frappe_records,
				frappe_lookup=frappe_lookup,
				mapping_context=mapping_context,
				dry_run=context.dry_run,
				stats=stats,
				label_direction="Frappe <- Partner",
				full_sync=True,
			)
		else:
			source_keys: set[tuple[Any, ...]] = set()
			for partner_batch in sources_service._iter_partner_source_batches(config, connector, context):
				frappe_records = sources_service._load_frappe_match_candidates(config, partner_batch)
				frappe_lookup = matching_service._build_frappe_match_lookup(config, frappe_records)
				one_way_service._sync_partner_to_frappe(
					run_doc=run_doc,
					config=config,
					connector=connector,
					partner_records=partner_batch,
					frappe_records=frappe_records,
					frappe_lookup=frappe_lookup,
					mapping_context=mapping_context,
					dry_run=context.dry_run,
					stats=stats,
					label_direction="Frappe <- Partner",
					full_sync=False,
					source_keys=source_keys,
				)
			audit_service._flush_pending_run_writes(run_doc, force=True)
	else:
		if config_access_service._config_match_mode(config) == MATCH_MODE_IDENTITY_FIELDS:
			frappe_index = [
				record
				for batch in sources_service._iter_frappe_source_batches(config, context)
				for record in batch
			]
			frappe_lookup_index = (
				[
					record
					for batch in sources_service._iter_frappe_source_batches(
						config,
						context,
						apply_delta_filter=False,
						use_script_source=False,
					)
					for record in batch
				]
				if context.is_delta_sync
				or config_access_service._config_frappe_source_mode(config)
				== FRAPPE_SOURCE_MODE_PYTHON_SCRIPT
				else frappe_index
			)
			partner_index = [
				record
				for batch in sources_service._iter_partner_source_batches(config, connector, context)
				for record in batch
			]
			partner_lookup_index = (
				[
					record
					for batch in sources_service._iter_partner_source_batches(
						config, connector, context, apply_delta_filter=False
					)
					for record in batch
				]
				if context.is_delta_sync
				else partner_index
			)
		else:
			frappe_index = matching_service._build_frappe_index_from_batches(
				config,
				sources_service._iter_frappe_source_batches(config, context),
			)
			frappe_lookup_index = (
				matching_service._build_frappe_index_from_batches(
					config,
					sources_service._iter_frappe_source_batches(
						config,
						context,
						apply_delta_filter=False,
						use_script_source=False,
					),
				)
				if context.is_delta_sync
				or config_access_service._config_frappe_source_mode(config)
				== FRAPPE_SOURCE_MODE_PYTHON_SCRIPT
				else frappe_index
			)
			partner_index = matching_service._build_partner_index_from_batches(
				config,
				sources_service._iter_partner_source_batches(config, connector, context),
			)
			partner_lookup_index = (
				matching_service._build_partner_index_from_batches(
					config,
					sources_service._iter_partner_source_batches(
						config, connector, context, apply_delta_filter=False
					),
				)
				if context.is_delta_sync
				else partner_index
			)
		bidirectional_service._sync_bidirectional(
			run_doc=run_doc,
			config=config,
			connector=connector,
			frappe_records=frappe_index,
			partner_records=partner_index,
			dry_run=context.dry_run,
			stats=stats,
			last_successful_sync=context.last_successful_sync,
			frappe_lookup_records=frappe_lookup_index,
			partner_lookup_records=partner_lookup_index,
			mapping_context=mapping_context,
			full_sync=context.is_full_sync,
		)
	return {
		"sync_definition": config.name,
		"sync_type": config.sync_type,
		"last_successful_sync_before_run": context.last_successful_sync.isoformat()
		if context.last_successful_sync
		else None,
		"delta_since": context.delta_since.isoformat() if context.delta_since else None,
		"dry_run": context.dry_run,
		**stats.as_dict(),
	}
