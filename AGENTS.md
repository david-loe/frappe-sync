# Sync

## Purpose

`sync` is a Frappe app for synchronizing records between Frappe DocTypes and external partner systems. It supports one-way sync from Frappe to partner (`A->B`), one-way sync from partner to Frappe (`A<-B`), and bidirectional sync (`A<->B`).

The app is built around configurable sync definitions, runtime execution with audit trails, YAML import/export of configuration, and Desk UI helpers for operators.

## Main Building Blocks

### App Hooks

- `sync/hooks.py` registers Desk JS for the core doctypes.
- `after_migrate` points to `sync.setup.after_migrate`, which ensures default partner types exist.
- The scheduler triggers `sync.api.run_due_sync_definitions` every 5 minutes.

### Core Doctypes

The implementation is centered on these doctypes:

- `Sync Definition`: main sync configuration.
- `Sync Partner`: connection/configuration for an external system.
- `Sync Partner Type`: connector family metadata such as MSSQL, Postgres, Firebird.
- `Sync Run`: one execution of a sync definition.
- `Sync Run Item`: one processed record within a run.

Supporting child doctypes:

- `Sync Field Mapping`
- `Sync Key Field`
- `Sync Modified Field`
- `Sync Value Mapping`

## Service Layer

The main runtime lives in [runtime.py](/workspace/development/frappe-bench/apps/sync/sync/sync/service/runtime.py). `sync/sync/service/__init__.py` exports the service functions used by the API layer.

Important exported functions:

- `enqueue_sync_definition`
- `execute_sync_definition`
- `run_due_sync_definitions`
- `list_due_sync_definitions`
- `preview_sync_definition`
- `export_sync_definition_yaml`
- `import_sync_definition_yaml`
- `test_sync_partner_connection`

## Execution Model

### Definition Config

`SyncDefinitionConfig` is the normalized runtime configuration object. It contains:

- target Frappe doctype
- sync partner reference
- sync type
- filters
- batch size
- create/delete/conflict settings
- modified-field configuration
- match fields
- partner source settings (`table_name` plus optional `read_query`)
- partner identity and create-ID settings
- structured field mapping
- value mapping

### Runtime Flow

`execute_sync_definition()` performs the full execution lifecycle:

1. acquire a lock per definition
2. create or load a `Sync Run`
3. mark the run as `Running`
4. build normalized config
5. validate the partner connector with `ping()`
6. execute `_run_engine(...)`
7. persist run counters and summary
8. update `Sync Definition` runtime fields
9. schedule the next run from cron if applicable

On failure it writes error state to the run and to the definition summary.

### Batch Processing

The runtime is batch-oriented on the source side.

- Frappe records are fetched through paged iteration helpers.
- Partner records are fetched through paged connector iteration helpers.
- One-way syncs process source records batchwise instead of loading both sides into global lists first.
- Bidirectional syncs build keyed indices from paged batches without creating intermediate full source lists.

There is one deliberate exception: when `delete_missing` is enabled during a full sync, the source side is loaded fully before writes begin. This is a safety rule so partial source loads cannot cause destructive behavior.

### Audit Write Strategy

Run-item writes are buffered in commit batches instead of forcing a database commit after every single audit row.

The runtime still creates:

- one `Sync Run` per execution
- one `Sync Run Item` per processed record outcome
- compact change metadata on the run item itself (`change_count` and `changed_fields`)

The current implementation does not depend on field-level child rows for audit logging. Detailed before/after inspection is done through the captured Frappe and partner payloads on the run item, while the run item itself keeps a compact field-name summary.

## Mapping Model

### Structured Mapping

Field mappings are canonicalized to this schema:

```python
{
  "frappe_field_name": {
    "partner_field": "external_field_name",
    "direction": "Both" | "Frappe to Partner" | "Partner to Frappe",
  }
}
```

This schema is used consistently in:

- `Sync Definition` export payloads
- runtime config building
- runtime mapping helpers
- preview payloads
- frontend preview rendering

### Direction Semantics

`direction` is not decorative. It changes runtime behavior.

- `Frappe to Partner` mappings are only used when building partner payloads.
- `Partner to Frappe` mappings are only used when building Frappe payloads.
- `Both` applies in both directions.

Match-field mappings must permit the active sync direction. Runtime config validation rejects invalid combinations early.

### Value Mapping

Value mappings are keyed by Frappe field. When syncing toward the partner, Frappe values are translated to partner values. When syncing toward Frappe, the mapping is reversed automatically.

### Identity and Pairing

The runtime distinguishes between logical matching and stored cross-system identity.

- `match_fields` define how records are matched when no stored foreign identity is available yet
- `partner_identity_field` is the technical key on the partner side, for example `NR`
- `frappe_partner_identity_field` can store that partner identity back on the Frappe document after a create
- `partner_frappe_identity_field` can optionally store the Frappe `name` on the partner side
- subsequent runs prefer the stored foreign identity over re-matching purely through `match_fields`

Partner-side create behavior is configurable through:

- `partner_create_id_strategy`
- `partner_create_id_source`
- `partner_create_id_scope_where`

For relational partners this supports payload-driven IDs, connector defaults, sequences, and scoped `max_plus_one` allocation.

## Source Selection and Safety Rules

### Source Settings

In `Sync Definition`:

- `table_name` is required and is always the write target
- `read_query` is optional and affects reads only
- if `read_query` is blank, reads default to `table_name`
- `delete_missing` is not allowed together with `read_query`
- partner-side ID allocation scopes are configured separately through `partner_create_id_scope_where`

### Filter Expression

`filter_expression` is validated server-side in `Sync Definition.validate()`.

- blank values normalize to `None`
- JSON strings must decode to a list or dict
- programmatic list/dict values are serialized back to JSON
- invalid JSON causes a hard validation error

The runtime parser only consumes already validated filter JSON.

### Delete Missing

`delete_missing` is only allowed to act on complete source loads.

- partner fetch errors raise hard failures
- partial partner loads do not proceed into destructive delete-missing logic
- for full syncs with `delete_missing`, the source is fully loaded before write processing begins

### Time Zones and Datetime Normalization

The project treats cross-system datetime handling as an explicit runtime concern.

- `Sync Partner.time_zone` can define the partner's IANA time zone, for example `Europe/Berlin`.
- if a partner timestamp already carries an offset or timezone, the runtime respects that embedded timezone
- if a partner timestamp is naive and `time_zone` is configured, the runtime interprets that value in the partner timezone
- partner datetimes are normalized into the Frappe site timezone before delta-sync comparisons against `last_successful_sync`
- mapped Frappe `Datetime` fields are converted from site timezone to partner timezone on writes to the partner
- mapped partner datetime values are converted from partner timezone into site timezone on writes to Frappe

If no partner time zone is configured, naive partner timestamps are treated as-is. For heterogeneous installations, setting `Sync Partner.time_zone` is the intended way to make delta sync and datetime field mapping deterministic.

## API Layer

The public server API lives in [sync/api.py](/workspace/development/frappe-bench/apps/sync/sync/api.py).

### Security Model

Administrative endpoints are protected server-side.

- critical endpoints require `System Manager`
- sync-definition endpoints check document permissions
- partner endpoints check document permissions
- import paths pre-check create/read/write permissions against the previewed documents before internal upsert logic runs

The implementation does not rely on client-side restrictions alone.

### Canonical API Surface

The API surface is intentionally kept to canonical endpoint names. The project no longer exposes multiple alias names for the same operation.

Important endpoints:

- `run_sync_definition`
- `run_sync_now`
- `run_due_sync_definitions`
- `preview_sync_definition`
- `export_sync_definition_yaml`
- `preview_import_sync_definition_yaml`
- `import_sync_definition_yaml`
- `import_sync_yaml_from_json`
- `test_sync_partner`
- `get_sync_definition_field_choices`
- `get_sync_partner_table_columns`

### Import/Export Format

YAML import/export is centered on these top-level sections:

- `sync_partner_type`
- `sync_partner`
- `sync_definition`

API import responses use a stable dictionary schema with keys such as:

- `ok`
- `overwrite`
- `sync_definition`
- `sync_partner`
- `sync_partner_type`
- `documents`

## Frontend

The Desk frontend is implemented in `sync/public/js/`.

### Shared Helpers

[sync_helpers.js](/workspace/development/frappe-bench/apps/sync/sync/public/js/sync_helpers.js) is the central helper module. It contains:

- API-call wrappers
- preview rendering
- mapping normalization for preview payloads
- partner connection test flow
- YAML import/export helpers
- list/read helpers for Desk views
- target-doctype lookup helpers for run views

### Sync Definition Form

[sync_definition.js](/workspace/development/frappe-bench/apps/sync/sync/public/js/sync_definition.js) controls the operator workflow for a sync definition.

It handles:

- run/preview/export/import buttons
- source-setting validation
- dynamic Frappe field choices
- partner-column introspection for table-based partner sources
- visibility of modified-field sections based on sync direction

### Sync Partner Form

[sync_partner.js](/workspace/development/frappe-bench/apps/sync/sync/public/js/sync_partner.js) manages partner-specific UI behavior.

It handles:

- connection hints by partner type
- partner time-zone guidance for naive upstream timestamps
- auth-field visibility
- status-field descriptions
- `Test Connection`

The connection test flow saves the form if needed, calls the first available supported API, persists connection status fields, and reloads the form so the operator sees the stored state immediately.

### Run Monitoring

[sync_run.js](/workspace/development/frappe-bench/apps/sync/sync/public/js/sync_run.js), [sync_run_item.js](/workspace/development/frappe-bench/apps/sync/sync/public/js/sync_run_item.js), and [sync_run_item_list.js](/workspace/development/frappe-bench/apps/sync/sync/public/js/sync_run_item_list.js) provide run health, recent items, navigation to target documents, compact changed-field summaries, and related monitoring helpers.

## Default Partner Types

`sync/setup.py` ensures the app always has baseline partner types for:

- MSSQL
- Postgres
- Firebird

This is executed from `after_migrate`.

## Testing

The project has both unit-style test modules and Bench-driven integration coverage.

Important test modules:

- `sync/tests/test_api.py`
- `sync/tests/test_runtime_helpers.py`
- `sync/tests/test_runtime_additional.py`
- `sync/tests/test_runtime_management.py`
- `sync/tests/test_runtime_execution.py`
- `sync/tests/test_setup.py`
- `sync/tests/test_setup_and_doctypes.py`
- connector test modules under `sync/tests/test_connectors.py` and `sync/tests/test_connector_additional.py`

Practical commands:

```bash
python -m py_compile sync/sync/service/runtime.py sync/api.py
PYTHONPATH=/workspace/development/frappe-bench/apps/frappe:/workspace/development/frappe-bench/apps/sync /workspace/development/frappe-bench/env/bin/python -m unittest sync.tests.test_api sync.tests.test_runtime_helpers sync.tests.test_runtime_additional sync.tests.test_runtime_management sync.tests.test_setup sync.tests.test_setup_and_doctypes
bench --site development.localhost run-tests --app sync
```

At the time this document was written, the app-level Bench test run for `sync` passed successfully.

## Implementation Conventions

- The runtime is the source of truth for sync semantics.
- The API layer should stay thin and permission-aware.
- The frontend should consume canonical API names and canonical preview/import payload shapes.
- Mapping shape and direction semantics must stay aligned across doctype model, runtime, preview, and frontend.
- Safety takes precedence over maximal batching when destructive behavior such as `delete_missing` is involved.


## Localization

After changing any translatable strings, always run these commands in this exact order:

```bash
bench generate-pot-file --app sync
bench update-po-files --app sync
```

Important:

- Never run `update-po-files` before `generate-pot-file`.
- Immediately after `bench update-po-files --app sync`, add all missing translations to `sync/locale/de.po`.
- No `msgstr ""` entries may remain after localization is complete, except for the standard PO header at the top of the file.
- Translatable strings may originate from Python, JS, JSON, Web Forms, or notification templates.
- Changes to UI labels, error messages, button text, or JSON labels without updating PO/POT files leave the repository in an inconsistent state.

## JSON Updates

Frappe apps are heavily JSON-driven. Many system objects are maintained via JSON files and imported by Frappe.
Important:
- Any change to JSON files used for Doctypes or other system imports must also update the `modified` field.
- This applies in particular to:
  - `doctype/-/-.json`
  - `web_form/-/-.json`
  - `notification/-/-.json`
  - workspace/sidebar/desktop JSON files
- Outdated `modified` timestamps commonly cause difficult-to-diagnose sync or import issues.
