from __future__ import annotations

from typing import Any

import frappe

from sync.sync.constants import (
	FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
)
from sync.sync.service import audit as audit_service
from sync.sync.service import changes as changes_service
from sync.sync.service import config_access as config_access_service
from sync.sync.service import mapping as mapping_service
from sync.sync.service import matching as matching_service
from sync.sync.service.execution import writes as writes_service
from sync.sync.service.models import (
	SYNC_TYPE_FRAPPE_TO_PARTNER,
	SYNC_TYPE_PARTNER_TO_FRAPPE,
	FrappeMatchLookup,
	PartnerMatchLookup,
	RuntimeMappingContext,
	SyncDefinitionConfig,
	SyncStats,
)


def _sync_frappe_to_partner(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	frappe_records: list[dict[str, Any]],
	partner_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
	full_sync: bool,
	source_keys: set[tuple[Any, ...]] | None = None,
	partner_lookup: PartnerMatchLookup | None = None,
	mapping_context: RuntimeMappingContext | None = None,
):
	partner_lookup = partner_lookup or matching_service._build_partner_match_lookup(config, partner_records)
	mapping_context = mapping_context or mapping_service._build_runtime_mapping_context(config)
	partner_groups = partner_lookup.groups
	partner_index = partner_lookup.latest_by_key
	partner_identity_index = partner_lookup.identity_by_value
	collected_source_keys = source_keys if source_keys is not None else set()
	connector_mapping = mapping_context.connector_mapping

	for frappe_record in frappe_records:
		key = matching_service._key_tuple_from_frappe(
			frappe_record, config_access_service._config_match_fields(config)
		)
		if not matching_service._valid_key(key):
			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="error",
				status="error",
				message="Record has incomplete key fields.",
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=None,
				commit=False,
			)
			continue

		collected_source_keys.add(key)
		partner_payload = mapping_service._apply_partner_link_fields(
			config,
			frappe_record,
			mapping_service._map_frappe_to_partner(
				frappe_record,
				config.mapping,
				config.value_mapping,
				getattr(config, "value_mapping_fallbacks", None),
				doctype=getattr(config, "doctype", None),
				partner_time_zone=getattr(config, "partner_time_zone", None),
				mapping_context=mapping_context,
			),
		)
		existing_partners = matching_service._find_existing_partner_records(
			config,
			frappe_record,
			partner_groups,
			partner_identity_index,
		)
		existing_partner = existing_partners[-1] if existing_partners else None

		if not existing_partners and not config.create_new:
			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="skipped",
				status="skipped",
				message="Create disabled and target record does not exist.",
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=existing_partner,
				commit=False,
			)
			continue

		if not existing_partners:
			write_payload = mapping_service._with_partner_timestamps(
				config,
				frappe_record,
				partner_payload,
				create=True,
				mapping_context=mapping_context,
			)
			try:
				write = connector.upsert_record(
					record=write_payload,
					key_values=matching_service._partner_key_values_for_write(config, frappe_record, key),
					mapping=connector_mapping,
					dry_run=dry_run,
					source=config.table_name,
					create_options=writes_service._build_partner_create_options(config),
				)
				if not write.ok:
					raise RuntimeError(write.message or "Partner upsert failed.")
				writes_service._persist_frappe_partner_identity(config, frappe_record, write, dry_run=dry_run)
			except Exception as exc:
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=None,
					commit=False,
				)
				continue

			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="created",
				status="success",
				message="Dry run upsert." if dry_run else "Upserted partner record.",
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=getattr(write, "record", None) or write_payload,
				partner_before_record=None,
				written_after_record=getattr(write, "record", None) or write_payload,
				changes=[],
				commit=False,
			)
			continue

		if len(existing_partners) > 1 and not matching_service._can_write_partner_matches_individually(
			config, existing_partners
		):
			change_sets = [
				changes_service._diff_target_values(
					new_record=partner_payload,
					old_record=matched_partner,
					field_names=list(partner_payload.keys()),
					exclude_fields={
						config_access_service._config_partner_modified_field(config),
						config_access_service._config_partner_creation_field(config),
					},
					datetime_fields=mapping_context.partner_datetime_fields,
					assumed_time_zone=getattr(config, "partner_time_zone", None),
					target_time_zone=getattr(config, "partner_time_zone", None)
					or mapping_context.site_time_zone,
				)
				for matched_partner in existing_partners
			]
			if not any(change_sets):
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="skipped",
					status="skipped",
					message="No changes detected across matched partner records.",
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=existing_partner,
					commit=False,
				)
				continue
			if not config_access_service._update_existing_enabled(config):
				audit_service._log_update_existing_disabled(
					stats=stats,
					run_doc=run_doc,
					config=config,
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=existing_partner,
					write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
					changes=change_sets[-1],
					commit=False,
				)
				continue
			write_payload = mapping_service._with_partner_timestamps(
				config,
				frappe_record,
				partner_payload,
				create=False,
				mapping_context=mapping_context,
			)
			try:
				write = connector.upsert_record(
					record=write_payload,
					key_values=matching_service._partner_key_values_for_write(config, frappe_record, key),
					mapping=connector_mapping,
					dry_run=dry_run,
					source=config.table_name,
					create_options=writes_service._build_partner_create_options(config),
				)
				if not write.ok:
					raise RuntimeError(write.message or "Partner upsert failed.")
			except Exception as exc:
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=existing_partner,
					commit=False,
				)
				continue

			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="updated",
				status="success",
				message=(
					"Dry run upsert."
					if dry_run
					else f"Upserted {len(existing_partners)} matched partner records."
				),
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=getattr(write, "record", None) or existing_partner or write_payload,
				partner_before_record=existing_partner,
				written_after_record=getattr(write, "record", None) or existing_partner or write_payload,
				changes=change_sets[-1],
				commit=False,
			)
			continue

		for matched_partner in existing_partners:
			changes = changes_service._diff_target_values(
				new_record=partner_payload,
				old_record=matched_partner or {},
				field_names=list(partner_payload.keys()),
				exclude_fields={
					config_access_service._config_partner_modified_field(config),
					config_access_service._config_partner_creation_field(config),
				},
				datetime_fields=mapping_context.partner_datetime_fields,
				assumed_time_zone=getattr(config, "partner_time_zone", None),
				target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
			)
			if not changes:
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="skipped",
					status="skipped",
					message="No changes detected.",
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=matched_partner,
					commit=False,
				)
				continue
			if not config_access_service._update_existing_enabled(config):
				audit_service._log_update_existing_disabled(
					stats=stats,
					run_doc=run_doc,
					config=config,
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=matched_partner,
					write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
					changes=changes,
					commit=False,
				)
				continue
			write_payload = mapping_service._with_partner_timestamps(
				config,
				frappe_record,
				partner_payload,
				create=False,
				mapping_context=mapping_context,
			)

			try:
				write = connector.upsert_record(
					record=write_payload,
					key_values=matching_service._partner_key_values_for_existing_match(
						config, frappe_record, key, matched_partner
					),
					mapping=connector_mapping,
					dry_run=dry_run,
					source=config.table_name,
					create_options=writes_service._build_partner_create_options(config),
				)
				if not write.ok:
					raise RuntimeError(write.message or "Partner upsert failed.")
				if len(existing_partners) == 1:
					writes_service._persist_frappe_partner_identity(
						config, frappe_record, write, dry_run=dry_run
					)
			except Exception as exc:
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=matched_partner,
					commit=False,
				)
				continue

			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="updated",
				status="success",
				message="Dry run upsert." if dry_run else "Upserted partner record.",
				direction=label_direction,
				frappe_record=frappe_record,
				partner_record=getattr(write, "record", None) or write_payload,
				partner_before_record=matched_partner,
				written_after_record=getattr(write, "record", None) or write_payload,
				changes=changes,
				commit=False,
			)

	if config.delete_missing and full_sync:
		_delete_missing_partner_records(
			run_doc=run_doc,
			config=config,
			connector=connector,
			partner_index=partner_index,
			source_keys=collected_source_keys,
			dry_run=dry_run,
			stats=stats,
			label_direction=label_direction,
		)
	audit_service._flush_pending_run_writes(run_doc)
	return collected_source_keys


def _sync_partner_to_frappe(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	partner_records: list[dict[str, Any]],
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
	full_sync: bool,
	source_keys: set[tuple[Any, ...]] | None = None,
	frappe_lookup: FrappeMatchLookup | None = None,
	mapping_context: RuntimeMappingContext | None = None,
):
	frappe_lookup = frappe_lookup or matching_service._build_frappe_match_lookup(config, frappe_records)
	mapping_context = mapping_context or mapping_service._build_runtime_mapping_context(config)
	partner_input_records = matching_service._normalize_partner_match_records(config, partner_records)
	frappe_groups = frappe_lookup.groups
	frappe_partner_identity_index = frappe_lookup.identity_by_value
	collected_source_keys = source_keys if source_keys is not None else set()

	for partner_record in (
		partner_input_records.values() if isinstance(partner_input_records, dict) else partner_input_records
	):
		key = matching_service._key_tuple_from_partner(
			partner_record, config_access_service._config_match_fields(config), config.mapping
		)
		if not matching_service._valid_key(key):
			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="error",
				status="error",
				message="Partner record has incomplete key fields.",
				direction=label_direction,
				frappe_record=None,
				partner_record=partner_record,
				commit=False,
			)
			continue

		collected_source_keys.add(key)
		frappe_payload = mapping_service._map_partner_to_frappe(
			partner_record,
			config.mapping,
			config.value_mapping,
			getattr(config, "value_mapping_fallbacks", None),
			doctype=getattr(config, "doctype", None),
			partner_time_zone=getattr(config, "partner_time_zone", None),
			mapping_context=mapping_context,
		)
		frappe_partner_field = config_access_service._config_frappe_partner_identity_field(config)
		partner_identity_field = config_access_service._config_partner_identity_field(config)
		if frappe_partner_field and partner_identity_field:
			partner_id = partner_record.get(partner_identity_field)
			if partner_id not in (None, ""):
				frappe_payload[frappe_partner_field] = partner_id
		existing_frappe_records = matching_service._find_existing_frappe_records(
			config,
			partner_record,
			frappe_groups,
			frappe_partner_identity_index,
		)
		existing_frappe = existing_frappe_records[-1] if existing_frappe_records else None

		if not existing_frappe_records and not config.create_new:
			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="skipped",
				status="skipped",
				message="Create disabled and target record does not exist.",
				direction=label_direction,
				frappe_record=existing_frappe,
				partner_record=partner_record,
				commit=False,
			)
			continue

		if not existing_frappe_records:
			write_payload = mapping_service._with_frappe_modified_timestamp(
				config,
				partner_record,
				frappe_payload,
				mapping_context=mapping_context,
			)
			try:
				doc_name = writes_service._upsert_frappe_record(
					doctype=config.doctype,
					existing_name=None,
					payload=write_payload,
					dry_run=dry_run,
					**writes_service._frappe_write_hook_kwargs(
						config=config,
						run_doc=run_doc,
						event=FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
						partner_record=partner_record,
						frappe_payload=write_payload,
						frappe_before_record=None,
						changes=[],
						dry_run=dry_run,
					),
				)
				if doc_name:
					write_payload["name"] = doc_name
			except Exception as exc:
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=None,
					partner_record=partner_record,
					commit=False,
				)
				continue

			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="created",
				status="success",
				message=writes_service._append_hook_message(
					"Dry run upsert." if dry_run else "Upserted frappe record.",
					planned=writes_service._planned_frappe_write_hook_message(
						config, FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT
					)
					if dry_run
					else None,
				),
				direction=label_direction,
				frappe_record=write_payload,
				partner_record=partner_record,
				frappe_before_record=None,
				written_after_record=write_payload,
				changes=[],
				commit=False,
			)
			continue

		for matched_frappe in existing_frappe_records:
			try:
				after_match_result = writes_service._run_after_match_frappe_write_hooks(
					config=config,
					run_doc=run_doc,
					partner_record=partner_record,
					frappe_record=matched_frappe,
					frappe_payload=frappe_payload,
					changes=None,
					dry_run=dry_run,
				)
			except Exception as exc:
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=matched_frappe,
					partner_record=partner_record,
					commit=False,
				)
				continue
			if not config_access_service._update_existing_enabled(config):
				if after_match_result.changed:
					audit_service._register_and_log(
						stats=stats,
						run_doc=run_doc,
						config=config,
						action="updated",
						status="success",
						message=writes_service._append_hook_message(
							"After Match hook changed matched frappe record.",
							after_match_result,
						),
						direction=label_direction,
						frappe_record=matched_frappe,
						partner_record=partner_record,
						commit=False,
					)
				else:
					audit_service._log_update_existing_disabled(
						stats=stats,
						run_doc=run_doc,
						config=config,
						direction=label_direction,
						frappe_record=matched_frappe,
						partner_record=partner_record,
						write_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
						changes=[],
						commit=False,
					)
				continue
			changes = changes_service._diff_target_values(
				new_record=frappe_payload,
				old_record=matched_frappe or {},
				field_names=mapping_service._frappe_diff_field_names(frappe_payload, mapping_context),
				exclude_fields={
					config_access_service._config_frappe_modified_field(config),
					config_access_service._config_frappe_creation_field(config),
				},
				datetime_fields=mapping_context.frappe_datetime_fields,
				target_time_zone=mapping_context.site_time_zone,
			)
			if not changes:
				if after_match_result.changed:
					audit_service._register_and_log(
						stats=stats,
						run_doc=run_doc,
						config=config,
						action="updated",
						status="success",
						message=writes_service._append_hook_message(
							"After Match hook changed matched frappe record.", after_match_result
						),
						direction=label_direction,
						frappe_record=matched_frappe,
						partner_record=partner_record,
						commit=False,
					)
					continue
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="skipped",
					status="skipped",
					message=writes_service._append_hook_message(
						"No changes detected.",
						after_match_result if not dry_run else None,
						planned=writes_service._planned_frappe_write_hook_message(
							config, FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH
						)
						if dry_run
						else None,
					),
					direction=label_direction,
					frappe_record=matched_frappe,
					partner_record=partner_record,
					commit=False,
				)
				continue

			try:
				target_payload = mapping_service._with_frappe_modified_timestamp(
					config,
					partner_record,
					frappe_payload,
					mapping_context=mapping_context,
				)
				target_payload["name"] = matched_frappe.get("name")
				doc_name = writes_service._upsert_frappe_record(
					doctype=config.doctype,
					existing_name=matched_frappe.get("name"),
					payload=target_payload,
					dry_run=dry_run,
					**writes_service._frappe_write_hook_kwargs(
						config=config,
						run_doc=run_doc,
						event=FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE,
						partner_record=partner_record,
						frappe_payload=target_payload,
						frappe_before_record=matched_frappe,
						changes=changes,
						dry_run=dry_run,
					),
				)
				if doc_name:
					target_payload["name"] = doc_name
			except Exception as exc:
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=matched_frappe,
					partner_record=partner_record,
					commit=False,
				)
				continue

			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="updated",
				status="success",
				message=writes_service._append_hook_message(
					"Dry run upsert." if dry_run else "Upserted frappe record.",
					planned=writes_service._planned_frappe_write_hook_message(
						config, FRAPPE_WRITE_HOOK_EVENT_AFTER_UPDATE
					)
					if dry_run
					else None,
				),
				direction=label_direction,
				frappe_record=target_payload,
				partner_record=partner_record,
				frappe_before_record=matched_frappe,
				written_after_record=target_payload,
				changes=changes,
				commit=False,
			)

	if config.delete_missing and full_sync:
		_delete_missing_frappe_records(
			run_doc=run_doc,
			config=config,
			frappe_records=frappe_records,
			source_keys=collected_source_keys,
			dry_run=dry_run,
			stats=stats,
			label_direction=label_direction,
		)
	audit_service._flush_pending_run_writes(run_doc)
	return collected_source_keys


def _delete_missing_partner_records(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	partner_index: dict[tuple[Any, ...], dict[str, Any]],
	source_keys: set[tuple[Any, ...]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
):
	for key, partner_record in partner_index.items():
		if key in source_keys:
			continue
		key_values = matching_service._partner_key_values_from_partner_record(config, partner_record)
		try:
			write = connector.delete_record(
				key_values=key_values,
				dry_run=dry_run,
				source=config.table_name,
			)
			if not write.ok:
				raise RuntimeError(write.message or "Partner delete failed.")
			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="deleted",
				status="success",
				message="Dry run delete." if dry_run else "Deleted partner record missing in source.",
				direction=label_direction,
				frappe_record=None,
				partner_record=partner_record,
				commit=False,
			)
		except Exception as exc:
			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="error",
				status="error",
				message=str(exc),
				direction=label_direction,
				frappe_record=None,
				partner_record=partner_record,
				commit=False,
			)


def _delete_missing_frappe_records(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	source_keys: set[tuple[Any, ...]],
	dry_run: bool,
	stats: SyncStats,
	label_direction: str,
):
	frappe_groups = matching_service._group_frappe_records(config, frappe_records)
	for key, matched_frappe_records in frappe_groups.items():
		if key in source_keys:
			continue
		for frappe_record in matched_frappe_records:
			try:
				if not dry_run:
					frappe.delete_doc(
						config.doctype, frappe_record["name"], ignore_permissions=True, force=True
					)
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="deleted",
					status="success",
					message="Dry run delete." if dry_run else "Deleted frappe record missing in source.",
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=None,
					commit=False,
				)
			except Exception as exc:
				audit_service._register_and_log(
					stats=stats,
					run_doc=run_doc,
					config=config,
					action="error",
					status="error",
					message=str(exc),
					direction=label_direction,
					frappe_record=frappe_record,
					partner_record=None,
					commit=False,
				)
