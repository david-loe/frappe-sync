from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import frappe

from sync.sync.constants import (
	FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
	FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH,
	MATCH_MODE_IDENTITY_FIELDS,
)
from sync.sync.service import audit as audit_service
from sync.sync.service import changes as changes_service
from sync.sync.service import config_access as config_access_service
from sync.sync.service import mapping as mapping_service
from sync.sync.service import matching as matching_service
from sync.sync.service.execution import one_way as one_way_service
from sync.sync.service.execution import writes as writes_service
from sync.sync.service.models import (
	SYNC_TYPE_BIDIRECTIONAL,
	SYNC_TYPE_FRAPPE_TO_PARTNER,
	SYNC_TYPE_PARTNER_TO_FRAPPE,
	IdentityRecordState,
	RuntimeMappingContext,
	SyncDefinitionConfig,
	SyncStats,
)


def _sync_bidirectional(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	partner_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	last_successful_sync: datetime | None,
	frappe_lookup_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None = None,
	partner_lookup_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None = None,
	mapping_context: RuntimeMappingContext | None = None,
	full_sync: bool = False,
):
	mapping_context = mapping_context or mapping_service._build_runtime_mapping_context(config)
	if config_access_service._config_match_mode(config) == MATCH_MODE_IDENTITY_FIELDS:
		_sync_bidirectional_identity_fields(
			run_doc=run_doc,
			config=config,
			connector=connector,
			frappe_records=frappe_records,
			partner_records=partner_records,
			dry_run=dry_run,
			stats=stats,
			last_successful_sync=last_successful_sync,
			frappe_lookup_records=frappe_lookup_records,
			partner_lookup_records=partner_lookup_records,
			mapping_context=mapping_context,
			full_sync=full_sync,
		)
		return
	frappe_index = matching_service._index_paired_frappe_records(config, frappe_records)
	partner_index = matching_service._index_paired_partner_records(config, partner_records)
	frappe_target_lookup_records = frappe_lookup_records if frappe_lookup_records is not None else []
	partner_target_lookup_records = partner_lookup_records if partner_lookup_records is not None else []
	frappe_target_lookup = matching_service._build_frappe_match_lookup(config, frappe_target_lookup_records)
	partner_target_lookup = matching_service._build_partner_match_lookup(
		config, partner_target_lookup_records
	)
	all_keys = set(frappe_index.keys()) | set(partner_index.keys())

	for key in sorted(all_keys, key=lambda item: json.dumps(item, default=str, ensure_ascii=True)):
		frappe_record = frappe_index.get(key)
		partner_record = partner_index.get(key)

		if frappe_record and not partner_record:
			one_way_service._sync_frappe_to_partner(
				run_doc=run_doc,
				config=config,
				connector=connector,
				frappe_records=[frappe_record],
				partner_records=partner_target_lookup_records,
				partner_lookup=partner_target_lookup,
				mapping_context=mapping_context,
				dry_run=dry_run,
				stats=stats,
				label_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
				full_sync=False,
			)
			continue

		if partner_record and not frappe_record:
			one_way_service._sync_partner_to_frappe(
				run_doc=run_doc,
				config=config,
				connector=connector,
				partner_records=[partner_record],
				frappe_records=frappe_target_lookup_records,
				frappe_lookup=frappe_target_lookup,
				mapping_context=mapping_context,
				dry_run=dry_run,
				stats=stats,
				label_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
				full_sync=False,
			)
			continue

		if not frappe_record or not partner_record:
			continue

		try:
			after_match_result = writes_service._run_after_match_frappe_write_hooks(
				config=config,
				run_doc=run_doc,
				partner_record=partner_record,
				frappe_record=frappe_record,
				frappe_payload=None,
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
				direction="Frappe <-> Partner",
				frappe_record=frappe_record,
				partner_record=partner_record,
				commit=False,
			)
			continue

		frappe_payload = mapping_service._map_partner_to_frappe(
			partner_record,
			config.mapping,
			config.value_mapping,
			getattr(config, "value_mapping_fallbacks", None),
			doctype=getattr(config, "doctype", None),
			partner_time_zone=getattr(config, "partner_time_zone", None),
			mapping_context=mapping_context,
		)
		partner_payload = mapping_service._map_frappe_to_partner(
			frappe_record,
			config.mapping,
			config.value_mapping,
			getattr(config, "value_mapping_fallbacks", None),
			doctype=getattr(config, "doctype", None),
			partner_time_zone=getattr(config, "partner_time_zone", None),
			mapping_context=mapping_context,
		)

		to_partner_changes = changes_service._diff_target_values(
			new_record=partner_payload,
			old_record=partner_record,
			field_names=list(partner_payload.keys()),
			exclude_fields={
				config_access_service._config_partner_modified_field(config),
				config_access_service._config_partner_creation_field(config),
			},
			datetime_fields=mapping_context.partner_datetime_fields,
			assumed_time_zone=getattr(config, "partner_time_zone", None),
			target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
		)
		to_frappe_changes = changes_service._diff_target_values(
			new_record=frappe_payload,
			old_record=frappe_record,
			field_names=mapping_service._frappe_diff_field_names(frappe_payload, mapping_context),
			exclude_fields={
				config_access_service._config_frappe_modified_field(config),
				config_access_service._config_frappe_creation_field(config),
			},
			datetime_fields=mapping_context.frappe_datetime_fields,
			target_time_zone=mapping_context.site_time_zone,
		)
		if not to_partner_changes and not to_frappe_changes:
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
					direction="Frappe <-> Partner",
					frappe_record=frappe_record,
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
					"No differences between both sides.",
					after_match_result if not dry_run else None,
					planned=writes_service._planned_frappe_write_hook_message(
						config, FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH
					)
					if dry_run
					else None,
				),
				direction="Frappe <-> Partner",
				frappe_record=frappe_record,
				partner_record=partner_record,
				commit=False,
			)
			continue

		if not config_access_service._update_existing_enabled(config):
			audit_service._log_update_existing_disabled(
				stats=stats,
				run_doc=run_doc,
				config=config,
				direction="Frappe <-> Partner",
				frappe_record=frappe_record,
				partner_record=partner_record,
				write_direction=SYNC_TYPE_BIDIRECTIONAL,
				changes=changes_service._canonical_conflict_changes(
					config,
					to_frappe_changes=to_frappe_changes,
					to_partner_changes=to_partner_changes,
				),
				commit=False,
			)
			continue

		decision = changes_service.resolve_conflict(
			config,
			frappe_record=frappe_record,
			partner_record=partner_record,
			last_successful_sync=last_successful_sync,
			site_time_zone=mapping_context.site_time_zone,
		)

		if decision == "frappe_changed":
			writes_service._apply_partner_update(
				run_doc=run_doc,
				config=config,
				connector=connector,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				partner_payload=partner_payload,
				changes=to_partner_changes,
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated partner from frappe.",
				commit=False,
				mapping_context=mapping_context,
			)
			continue

		if decision == "partner_changed":
			writes_service._apply_frappe_update(
				run_doc=run_doc,
				config=config,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				changes=to_frappe_changes,
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated frappe from partner.",
				commit=False,
			)
			continue

		if decision == "unsupported":
			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="conflict",
				status="conflict",
				message=f"Unsupported conflict policy: {config.conflict_policy}",
				direction="Frappe <-> Partner",
				frappe_record=frappe_record,
				partner_record=partner_record,
				commit=False,
			)
			continue

		if decision == "partner_newest":
			writes_service._apply_frappe_update(
				run_doc=run_doc,
				config=config,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				changes=to_frappe_changes,
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated frappe from partner with newest_wins.",
				commit=False,
			)
		elif decision == "frappe_newest":
			writes_service._apply_partner_update(
				run_doc=run_doc,
				config=config,
				connector=connector,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				partner_payload=partner_payload,
				changes=to_partner_changes,
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated partner from frappe with newest_wins.",
				commit=False,
				mapping_context=mapping_context,
			)
		elif decision == "partner_tie":
			writes_service._apply_frappe_update(
				run_doc=run_doc,
				config=config,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				changes=to_frappe_changes,
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated frappe from partner by timestamp tie breaker.",
				commit=False,
			)
		elif decision == "frappe_tie":
			writes_service._apply_partner_update(
				run_doc=run_doc,
				config=config,
				connector=connector,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				partner_record=partner_record,
				partner_payload=partner_payload,
				changes=to_partner_changes,
				direction="Frappe <-> Partner",
				action="updated",
				status="success",
				message="Updated partner from frappe by timestamp tie breaker.",
				commit=False,
				mapping_context=mapping_context,
			)
		else:
			audit_service._log_manual_bidirectional_conflict(
				stats=stats,
				run_doc=run_doc,
				config=config,
				frappe_record=frappe_record,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				partner_payload=partner_payload,
				mapping_context=mapping_context,
				to_frappe_changes=to_frappe_changes,
				to_partner_changes=to_partner_changes,
			)
	audit_service._flush_pending_run_writes(run_doc)


def _sync_bidirectional_identity_fields(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	frappe_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	partner_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]],
	dry_run: bool,
	stats: SyncStats,
	last_successful_sync: datetime | None,
	frappe_lookup_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None,
	partner_lookup_records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]] | None,
	mapping_context: RuntimeMappingContext,
	full_sync: bool,
) -> None:
	operation_state = matching_service._build_identity_record_state(config, frappe_records, partner_records)
	lookup_state = matching_service._build_identity_record_state(
		config,
		frappe_lookup_records if frappe_lookup_records is not None else frappe_records,
		partner_lookup_records if partner_lookup_records is not None else partner_records,
	)
	conflicted_ids: set[tuple[str, Any]] = set()
	_logged_conflicts: set[str] = set()

	def log_conflict(
		message: str, frappe_record: dict[str, Any] | None, partner_record: dict[str, Any] | None
	) -> None:
		key = json.dumps(
			[
				message,
				matching_service._identity_frappe_name(config, frappe_record),
				matching_service._identity_frappe_partner_id(config, frappe_record),
				matching_service._identity_partner_identity(config, partner_record),
				matching_service._identity_partner_frappe_id(config, partner_record),
			],
			default=str,
			ensure_ascii=True,
		)
		conflicted_ids.update(matching_service._identity_conflict_keys(config, frappe_record, partner_record))
		if key in _logged_conflicts:
			return
		_logged_conflicts.add(key)
		audit_service._register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="conflict",
			status="conflict",
			message=message,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			commit=False,
		)

	for message, frappe_group, partner_group in lookup_state.duplicate_conflicts:
		frappe_record = frappe_group[0] if frappe_group else None
		partner_record = partner_group[0] if partner_group else None
		for record in frappe_group:
			conflicted_ids.update(matching_service._identity_conflict_keys(config, frappe_record=record))
		for record in partner_group:
			conflicted_ids.update(matching_service._identity_conflict_keys(config, partner_record=record))
		log_conflict(message, frappe_record, partner_record)

	pairs: dict[tuple[Any, Any], tuple[dict[str, Any], dict[str, Any]]] = {}
	frappe_only: list[dict[str, Any]] = []
	partner_only: list[dict[str, Any]] = []

	for frappe_record in operation_state.frappe_records:
		if conflicted_ids.intersection(
			matching_service._identity_conflict_keys(config, frappe_record=frappe_record)
		):
			continue
		partner_record, message = matching_service._resolve_identity_partner_for_frappe(
			config, frappe_record, lookup_state
		)
		if message:
			log_conflict(message, frappe_record, partner_record)
			continue
		if partner_record:
			if conflicted_ids.intersection(
				matching_service._identity_conflict_keys(config, partner_record=partner_record)
			):
				continue
			pairs[matching_service._identity_pair_key(config, frappe_record, partner_record)] = (
				frappe_record,
				partner_record,
			)
		else:
			frappe_only.append(frappe_record)

	for partner_record in operation_state.partner_records:
		if conflicted_ids.intersection(
			matching_service._identity_conflict_keys(config, partner_record=partner_record)
		):
			continue
		frappe_record, message = matching_service._resolve_identity_frappe_for_partner(
			config, partner_record, lookup_state
		)
		if message:
			log_conflict(message, frappe_record, partner_record)
			continue
		if frappe_record:
			if conflicted_ids.intersection(
				matching_service._identity_conflict_keys(config, frappe_record=frappe_record)
			):
				continue
			pairs[matching_service._identity_pair_key(config, frappe_record, partner_record)] = (
				frappe_record,
				partner_record,
			)
		else:
			partner_only.append(partner_record)

	for frappe_record, partner_record in pairs.values():
		if conflicted_ids.intersection(
			matching_service._identity_conflict_keys(config, frappe_record, partner_record)
		):
			continue
		_sync_bidirectional_identity_pair(
			run_doc=run_doc,
			config=config,
			connector=connector,
			stats=stats,
			dry_run=dry_run,
			last_successful_sync=last_successful_sync,
			frappe_record=frappe_record,
			partner_record=partner_record,
			mapping_context=mapping_context,
		)

	if config.delete_missing and full_sync:
		_delete_missing_identity_records(
			run_doc=run_doc,
			config=config,
			connector=connector,
			lookup_state=lookup_state,
			dry_run=dry_run,
			stats=stats,
			conflicted_ids=conflicted_ids,
		)

	for frappe_record in frappe_only:
		if conflicted_ids.intersection(
			matching_service._identity_conflict_keys(config, frappe_record=frappe_record)
		):
			continue
		if matching_service._identity_frappe_partner_id(config, frappe_record) in (None, ""):
			_create_identity_partner_from_frappe(
				run_doc=run_doc,
				config=config,
				connector=connector,
				stats=stats,
				dry_run=dry_run,
				frappe_record=frappe_record,
				mapping_context=mapping_context,
			)
	for partner_record in partner_only:
		if conflicted_ids.intersection(
			matching_service._identity_conflict_keys(config, partner_record=partner_record)
		):
			continue
		if matching_service._identity_partner_frappe_id(config, partner_record) in (None, ""):
			_create_identity_frappe_from_partner(
				run_doc=run_doc,
				config=config,
				connector=connector,
				stats=stats,
				dry_run=dry_run,
				partner_record=partner_record,
				mapping_context=mapping_context,
			)

	audit_service._flush_pending_run_writes(run_doc)


def _sync_bidirectional_identity_pair(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	stats: SyncStats,
	dry_run: bool,
	last_successful_sync: datetime | None,
	frappe_record: dict[str, Any],
	partner_record: dict[str, Any],
	mapping_context: RuntimeMappingContext,
) -> None:
	try:
		after_match_result = writes_service._run_after_match_frappe_write_hooks(
			config=config,
			run_doc=run_doc,
			partner_record=partner_record,
			frappe_record=frappe_record,
			frappe_payload=None,
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
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			commit=False,
		)
		return
	frappe_payload = mapping_service._map_partner_to_frappe(
		partner_record,
		config.mapping,
		config.value_mapping,
		getattr(config, "value_mapping_fallbacks", None),
		doctype=getattr(config, "doctype", None),
		partner_time_zone=getattr(config, "partner_time_zone", None),
		mapping_context=mapping_context,
	)
	partner_payload = mapping_service._map_frappe_to_partner(
		frappe_record,
		config.mapping,
		config.value_mapping,
		getattr(config, "value_mapping_fallbacks", None),
		doctype=getattr(config, "doctype", None),
		partner_time_zone=getattr(config, "partner_time_zone", None),
		mapping_context=mapping_context,
	)
	to_partner_changes = changes_service._diff_target_values(
		new_record=mapping_service._apply_partner_link_fields(config, frappe_record, partner_payload),
		old_record=partner_record,
		field_names=list(
			mapping_service._apply_partner_link_fields(config, frappe_record, partner_payload).keys()
		),
		exclude_fields={
			config_access_service._config_partner_modified_field(config),
			config_access_service._config_partner_creation_field(config),
		},
		datetime_fields=mapping_context.partner_datetime_fields,
		assumed_time_zone=getattr(config, "partner_time_zone", None),
		target_time_zone=getattr(config, "partner_time_zone", None) or mapping_context.site_time_zone,
	)
	frappe_partner_field = config_access_service._config_frappe_partner_identity_field(config)
	partner_identity_field = config_access_service._config_partner_identity_field(config)
	if (
		frappe_partner_field
		and partner_identity_field
		and partner_record.get(partner_identity_field) not in (None, "")
	):
		frappe_payload = dict(frappe_payload)
		frappe_payload[frappe_partner_field] = partner_record.get(partner_identity_field)
	to_frappe_changes = changes_service._diff_target_values(
		new_record=frappe_payload,
		old_record=frappe_record,
		field_names=mapping_service._frappe_diff_field_names(frappe_payload, mapping_context),
		exclude_fields={
			config_access_service._config_frappe_modified_field(config),
			config_access_service._config_frappe_creation_field(config),
		},
		datetime_fields=mapping_context.frappe_datetime_fields,
		target_time_zone=mapping_context.site_time_zone,
	)
	if not to_partner_changes and not to_frappe_changes:
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
				direction=SYNC_TYPE_BIDIRECTIONAL,
				frappe_record=frappe_record,
				partner_record=partner_record,
				commit=False,
			)
			return
		audit_service._register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="skipped",
			status="skipped",
			message=writes_service._append_hook_message(
				"No differences between both sides.",
				after_match_result if not dry_run else None,
				planned=writes_service._planned_frappe_write_hook_message(
					config, FRAPPE_WRITE_HOOK_EVENT_AFTER_MATCH
				)
				if dry_run
				else None,
			),
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			commit=False,
		)
		return
	if not config_access_service._update_existing_enabled(config):
		audit_service._log_update_existing_disabled(
			stats=stats,
			run_doc=run_doc,
			config=config,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_BIDIRECTIONAL,
			changes=changes_service._canonical_conflict_changes(
				config,
				to_frappe_changes=to_frappe_changes,
				to_partner_changes=to_partner_changes,
			),
			commit=False,
		)
		return
	decision = changes_service.resolve_conflict(
		config,
		frappe_record=frappe_record,
		partner_record=partner_record,
		last_successful_sync=last_successful_sync,
		site_time_zone=mapping_context.site_time_zone,
	)
	if decision == "frappe_changed":
		writes_service._apply_partner_update(
			run_doc=run_doc,
			config=config,
			connector=connector,
			stats=stats,
			dry_run=dry_run,
			frappe_record=frappe_record,
			partner_record=partner_record,
			partner_payload=partner_payload,
			changes=to_partner_changes,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated partner from frappe.",
			commit=False,
			mapping_context=mapping_context,
		)
		return
	if decision == "partner_changed":
		writes_service._apply_frappe_update(
			run_doc=run_doc,
			config=config,
			stats=stats,
			dry_run=dry_run,
			frappe_record=frappe_record,
			partner_record=partner_record,
			frappe_payload=frappe_payload,
			changes=to_frappe_changes,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated frappe from partner.",
			commit=False,
		)
		return
	if decision == "unsupported":
		audit_service._register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="conflict",
			status="conflict",
			message=f"Unsupported conflict policy: {config.conflict_policy}",
			direction=SYNC_TYPE_BIDIRECTIONAL,
			frappe_record=frappe_record,
			partner_record=partner_record,
			commit=False,
		)
		return
	if decision == "frappe_newest":
		writes_service._apply_partner_update(
			run_doc=run_doc,
			config=config,
			connector=connector,
			stats=stats,
			dry_run=dry_run,
			frappe_record=frappe_record,
			partner_record=partner_record,
			partner_payload=partner_payload,
			changes=to_partner_changes,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated partner from frappe with newest_wins.",
			commit=False,
			mapping_context=mapping_context,
		)
	elif decision == "partner_newest":
		writes_service._apply_frappe_update(
			run_doc=run_doc,
			config=config,
			stats=stats,
			dry_run=dry_run,
			frappe_record=frappe_record,
			partner_record=partner_record,
			frappe_payload=frappe_payload,
			changes=to_frappe_changes,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated frappe from partner with newest_wins.",
			commit=False,
		)
	elif decision == "partner_tie":
		writes_service._apply_frappe_update(
			run_doc=run_doc,
			config=config,
			stats=stats,
			dry_run=dry_run,
			frappe_record=frappe_record,
			partner_record=partner_record,
			frappe_payload=frappe_payload,
			changes=to_frappe_changes,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated frappe from partner by timestamp tie breaker.",
			commit=False,
		)
	elif decision == "frappe_tie":
		writes_service._apply_partner_update(
			run_doc=run_doc,
			config=config,
			connector=connector,
			stats=stats,
			dry_run=dry_run,
			frappe_record=frappe_record,
			partner_record=partner_record,
			partner_payload=partner_payload,
			changes=to_partner_changes,
			direction=SYNC_TYPE_BIDIRECTIONAL,
			action="updated",
			status="success",
			message="Updated partner from frappe by timestamp tie breaker.",
			commit=False,
			mapping_context=mapping_context,
		)
	else:
		audit_service._log_manual_bidirectional_conflict(
			stats=stats,
			run_doc=run_doc,
			config=config,
			frappe_record=frappe_record,
			partner_record=partner_record,
			frappe_payload=frappe_payload,
			partner_payload=partner_payload,
			mapping_context=mapping_context,
			to_frappe_changes=to_frappe_changes,
			to_partner_changes=to_partner_changes,
		)


def _create_identity_partner_from_frappe(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	stats: SyncStats,
	dry_run: bool,
	frappe_record: dict[str, Any],
	mapping_context: RuntimeMappingContext,
) -> None:
	if not config.create_new:
		audit_service._register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="skipped",
			status="skipped",
			message="Create disabled and target record does not exist.",
			direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			frappe_record=frappe_record,
			partner_record=None,
			commit=False,
		)
		return
	partner_payload = mapping_service._with_partner_timestamps(
		config,
		frappe_record,
		mapping_service._apply_partner_link_fields(
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
		),
		create=True,
		mapping_context=mapping_context,
	)
	try:
		write = connector.upsert_record(
			record=partner_payload,
			key_values={},
			mapping=mapping_context.connector_mapping,
			dry_run=dry_run,
			source=config.table_name,
			create_options=writes_service._build_partner_create_options(config),
		)
		if not write.ok:
			raise RuntimeError(write.message or "Partner create failed.")
		writes_service._persist_frappe_partner_identity(config, frappe_record, write, dry_run=dry_run)
		audit_service._register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="created",
			status="success",
			message="Dry run create." if dry_run else "Created partner record.",
			direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			frappe_record=frappe_record,
			partner_record=getattr(write, "record", None) or partner_payload,
			write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			written_after_record=getattr(write, "record", None) or partner_payload,
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
			direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			frappe_record=frappe_record,
			partner_record=partner_payload,
			write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			commit=False,
		)


def _create_identity_frappe_from_partner(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	stats: SyncStats,
	dry_run: bool,
	partner_record: dict[str, Any],
	mapping_context: RuntimeMappingContext,
) -> None:
	if not config.create_new:
		audit_service._register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="skipped",
			status="skipped",
			message="Create disabled and target record does not exist.",
			direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			frappe_record=None,
			partner_record=partner_record,
			commit=False,
		)
		return
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
	if (
		frappe_partner_field
		and partner_identity_field
		and partner_record.get(partner_identity_field) not in (None, "")
	):
		frappe_payload[frappe_partner_field] = partner_record.get(partner_identity_field)
	frappe_payload = mapping_service._with_frappe_modified_timestamp(
		config,
		partner_record,
		frappe_payload,
		mapping_context=mapping_context,
	)
	try:
		doc_name = writes_service._upsert_frappe_record(
			doctype=config.doctype,
			existing_name=None,
			payload=frappe_payload,
			dry_run=dry_run,
			**writes_service._frappe_write_hook_kwargs(
				config=config,
				run_doc=run_doc,
				event=FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT,
				partner_record=partner_record,
				frappe_payload=frappe_payload,
				frappe_before_record=None,
				changes=[],
				dry_run=dry_run,
			),
		)
		if doc_name:
			frappe_payload["name"] = doc_name
		audit_service._register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="created",
			status="success",
			message=writes_service._append_hook_message(
				"Dry run create." if dry_run else "Created frappe record.",
				planned=writes_service._planned_frappe_write_hook_message(
					config, FRAPPE_WRITE_HOOK_EVENT_AFTER_INSERT
				)
				if dry_run
				else None,
			),
			direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			frappe_record=frappe_payload,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			written_after_record=frappe_payload,
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
			direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			frappe_record=None,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			commit=False,
		)
		return
	try:
		_persist_partner_frappe_identity(
			config, connector, partner_record, frappe_payload.get("name"), dry_run=dry_run
		)
	except Exception as exc:
		audit_service._register_and_log(
			stats=stats,
			run_doc=run_doc,
			config=config,
			action="error",
			status="error",
			message=str(exc),
			direction=SYNC_TYPE_PARTNER_TO_FRAPPE,
			frappe_record=frappe_payload,
			partner_record=partner_record,
			write_direction=SYNC_TYPE_FRAPPE_TO_PARTNER,
			commit=False,
		)


def _persist_partner_frappe_identity(
	config: SyncDefinitionConfig,
	connector: Any,
	partner_record: dict[str, Any],
	frappe_name: Any,
	*,
	dry_run: bool,
) -> None:
	partner_frappe_field = config_access_service._config_partner_frappe_identity_field(config)
	partner_identity_field = config_access_service._config_partner_identity_field(config)
	partner_id = partner_record.get(partner_identity_field) if partner_identity_field else None
	if dry_run or not partner_frappe_field or not partner_identity_field or frappe_name in (None, ""):
		return
	if partner_record.get(partner_frappe_field) == frappe_name:
		return
	if partner_id in (None, ""):
		raise RuntimeError("Partner Frappe ID write-back requires a partner identity value.")
	write = connector.upsert_record(
		record={partner_frappe_field: frappe_name},
		key_values={partner_identity_field: partner_id},
		mapping={partner_frappe_field: partner_frappe_field},
		dry_run=dry_run,
		source=config.table_name,
		create_options=writes_service._build_partner_create_options(config),
	)
	if not write.ok:
		raise RuntimeError(write.message or "Partner Frappe ID write-back failed.")
	partner_record[partner_frappe_field] = frappe_name


def _delete_missing_identity_records(
	*,
	run_doc: Any,
	config: SyncDefinitionConfig,
	connector: Any,
	lookup_state: IdentityRecordState,
	dry_run: bool,
	stats: SyncStats,
	conflicted_ids: set[tuple[str, Any]],
) -> None:
	for frappe_record in lookup_state.frappe_records:
		if conflicted_ids.intersection(
			matching_service._identity_conflict_keys(config, frappe_record=frappe_record)
		):
			continue
		frappe_name = matching_service._normalize_pairing_key_value(
			matching_service._identity_frappe_name(config, frappe_record)
		)
		frappe_partner_id = matching_service._normalize_pairing_key_value(
			matching_service._identity_frappe_partner_id(config, frappe_record)
		)
		if frappe_name in (None, "") or frappe_partner_id in (None, ""):
			continue
		if lookup_state.partner_by_identity.get(frappe_partner_id) or lookup_state.partner_by_frappe_id.get(
			frappe_name
		):
			continue
		try:
			if not dry_run:
				frappe.delete_doc(config.doctype, frappe_record["name"], ignore_permissions=True, force=True)
			audit_service._register_and_log(
				stats=stats,
				run_doc=run_doc,
				config=config,
				action="deleted",
				status="success",
				message="Dry run delete."
				if dry_run
				else "Deleted frappe record whose identity-linked partner is missing.",
				direction=SYNC_TYPE_BIDIRECTIONAL,
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
				direction=SYNC_TYPE_BIDIRECTIONAL,
				frappe_record=frappe_record,
				partner_record=None,
				commit=False,
			)

	for partner_record in lookup_state.partner_records:
		if conflicted_ids.intersection(
			matching_service._identity_conflict_keys(config, partner_record=partner_record)
		):
			continue
		partner_identity = matching_service._normalize_pairing_key_value(
			matching_service._identity_partner_identity(config, partner_record)
		)
		partner_frappe_id = matching_service._normalize_pairing_key_value(
			matching_service._identity_partner_frappe_id(config, partner_record)
		)
		if partner_identity in (None, "") or partner_frappe_id in (None, ""):
			continue
		if lookup_state.frappe_by_name.get(partner_frappe_id) or lookup_state.frappe_by_partner_id.get(
			partner_identity
		):
			continue
		partner_identity_field = config_access_service._config_partner_identity_field(config)
		try:
			write = connector.delete_record(
				key_values={partner_identity_field: partner_record.get(partner_identity_field)},
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
				message="Dry run delete."
				if dry_run
				else "Deleted partner record whose identity-linked Frappe record is missing.",
				direction=SYNC_TYPE_BIDIRECTIONAL,
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
				direction=SYNC_TYPE_BIDIRECTIONAL,
				frappe_record=None,
				partner_record=partner_record,
				commit=False,
			)
