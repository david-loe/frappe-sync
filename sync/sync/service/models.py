from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sync.sync.constants import (
	FRAPPE_SOURCE_MODE_DOCTYPE_QUERY,
	MAPPING_DIRECTION_BOTH,
	MAPPING_DIRECTION_FRAPPE_TO_PARTNER,
	MAPPING_DIRECTION_PARTNER_TO_FRAPPE,
	MATCH_MODE_MATCH_FIELDS,
	ONE_WAY_MATCH_FIRST,
	TIMESTAMP_TIE_MANUAL,
)

SYSTEM_KEYS = {
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"idx",
	"_user_tags",
	"_comments",
	"_assign",
	"_liked_by",
}


SYNC_DEFINITION_RUNTIME_STATE_FIELDS = {
	"frappe_after_insert_action",
	"frappe_after_update_action",
	"last_run",
	"last_run_status",
	"last_run_summary",
	"last_sync_at",
	"last_successful_sync",
	"next_run_at",
}


SYNC_DEFINITION_LOCK_TIMEOUT_SECONDS = 2 * 60 * 60


SYNC_TYPE_FRAPPE_TO_PARTNER = MAPPING_DIRECTION_FRAPPE_TO_PARTNER


SYNC_TYPE_PARTNER_TO_FRAPPE = MAPPING_DIRECTION_PARTNER_TO_FRAPPE


SYNC_TYPE_BIDIRECTIONAL = MAPPING_DIRECTION_BOTH


DEFAULT_RUNTIME_COMMIT_BATCH = 50


DEFAULT_STALE_RUN_TIMEOUT_MINUTES = 180


DEFAULT_RUN_RETENTION_DAYS_SUCCESS = 90


DEFAULT_RUN_RETENTION_DAYS_ERROR = 365


RUN_DOC_PENDING_WRITES_ATTR = "_sync_pending_write_count"


AUDIT_RECORD_UNSET = object()


VALUE_MAPPING_UNSET = object()


DEFAULT_TIMESTAMP_BUFFER_MS = 100


CHILD_FIELD_PATH_SEPARATOR = "."


@dataclass(slots=True)
class SyncFrappeWriteHookConfig:
	enabled: bool
	event: str
	hook_type: str
	action: str | None = None
	script: str | None = None
	description: str | None = None
	idx: int = 0


@dataclass(slots=True)
class SyncComputedFieldConfig:
	field_name: str
	template: str
	required_source_fields: tuple[str, ...] = ()
	idx: int = 0


@dataclass(slots=True)
class FrappeWriteHookResult:
	changed: bool = False
	messages: tuple[str, ...] = ()


@dataclass(slots=True)
class SyncDefinitionConfig:
	name: str
	doctype: str
	partner: str
	sync_type: str
	cron: str | None
	filters: list | dict | None
	batch_size: int
	create_new: bool
	delete_missing: bool
	use_last_sync_date: bool
	conflict_policy: str
	timestamp_buffer_ms: int
	table_name: str | None
	read_query: str | None
	match_fields: list[str]
	mapping: dict[str, dict[str, str]]
	value_mapping: dict[str, dict[Any, Any]]
	match_mode: str = MATCH_MODE_MATCH_FIELDS
	frappe_modified_field: str = "modified"
	frappe_creation_field: str = "creation"
	partner_modified_field: str | None = None
	partner_creation_field: str | None = None
	timestamp_tie_breaker: str = TIMESTAMP_TIE_MANUAL
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None = None
	partner_identity_field: str | None = None
	frappe_partner_identity_field: str | None = None
	partner_frappe_identity_field: str | None = None
	partner_create_id_strategy: str = "payload"
	partner_create_id_source: str | None = None
	partner_create_id_scope_where: str | None = None
	partner_time_zone: str | None = None
	one_way_match_mode: str = ONE_WAY_MATCH_FIRST
	capture_audit_payloads: bool = False
	update_existing: bool = True
	frappe_write_hooks: tuple[SyncFrappeWriteHookConfig, ...] = ()
	render_read_query_template: bool = False
	computed_fields: tuple[SyncComputedFieldConfig, ...] = ()
	frappe_source_mode: str = FRAPPE_SOURCE_MODE_DOCTYPE_QUERY
	frappe_source_script: str | None = None


@dataclass(slots=True)
class PartnerMatchLookup:
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]
	groups: dict[tuple[Any, ...], list[dict[str, Any]]]
	latest_by_key: dict[tuple[Any, ...], dict[str, Any]]
	identity_by_value: dict[Any, dict[str, Any]]


@dataclass(slots=True)
class FrappeMatchLookup:
	records: list[dict[str, Any]] | dict[tuple[Any, ...], dict[str, Any]]
	groups: dict[tuple[Any, ...], list[dict[str, Any]]]
	latest_by_key: dict[tuple[Any, ...], dict[str, Any]]
	identity_by_value: dict[Any, dict[str, Any]]


@dataclass(slots=True)
class IdentityRecordState:
	frappe_records: list[dict[str, Any]]
	partner_records: list[dict[str, Any]]
	frappe_by_name: dict[Any, dict[str, Any]]
	frappe_by_partner_id: dict[Any, dict[str, Any]]
	partner_by_identity: dict[Any, dict[str, Any]]
	partner_by_frappe_id: dict[Any, dict[str, Any]]
	duplicate_conflicts: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]]


@dataclass(slots=True)
class RuntimeMappingContext:
	mapping: dict[str, dict[str, str]]
	value_mapping: dict[str, dict[Any, Any]]
	value_mapping_fallbacks: dict[str, dict[str, Any]] | None
	to_partner_entries: tuple[tuple[str, str], ...]
	to_frappe_entries: tuple[tuple[str, str], ...]
	connector_mapping: dict[str, str]
	reverse_value_mapping: dict[str, dict[Any, Any]]
	frappe_datetime_fields: set[str]
	partner_datetime_fields: set[str]
	frappe_fieldnames: set[str] | None
	child_table_options: dict[str, str]
	site_time_zone: str
	partner_time_zone: str | None


@dataclass(slots=True)
class SyncStats:
	processed_count: int = 0
	success_count: int = 0
	created_count: int = 0
	updated_count: int = 0
	deleted_count: int = 0
	skipped_count: int = 0
	conflict_count: int = 0
	error_count: int = 0

	def register(self, action: str, status: str):
		self.processed_count += 1
		if action == "created":
			self.created_count += 1
		elif action == "updated":
			self.updated_count += 1
		elif action == "deleted":
			self.deleted_count += 1

		if status == "success":
			self.success_count += 1
		elif status == "error":
			self.error_count += 1
		elif status == "skipped":
			self.skipped_count += 1
		elif status == "conflict":
			self.conflict_count += 1

	def as_dict(self) -> dict[str, int]:
		return {
			"processed_count": self.processed_count,
			"success_count": self.success_count,
			"created_count": self.created_count,
			"updated_count": self.updated_count,
			"deleted_count": self.deleted_count,
			"skipped_count": self.skipped_count,
			"conflict_count": self.conflict_count,
			"error_count": self.error_count,
		}


@dataclass(slots=True)
class SyncContext:
	config: SyncDefinitionConfig
	dry_run: bool
	last_successful_sync: datetime | None

	@property
	def is_delta_sync(self) -> bool:
		return bool(self.config.use_last_sync_date and self.last_successful_sync)

	@property
	def delta_since(self) -> datetime | None:
		if not self.is_delta_sync:
			return None
		return self.last_successful_sync

	@property
	def is_full_sync(self) -> bool:
		return not self.is_delta_sync
