# Sync Contributor Guide

## Purpose

`sync` is a Frappe app for synchronizing records between Frappe DocTypes and
external partner systems. It supports one-way Frappe-to-partner sync (`A->B`),
one-way partner-to-Frappe sync (`A<-B`), and bidirectional sync (`A<->B`).

The app is built around configurable sync definitions, partner connectors,
runtime execution with audit records, YAML import/export, and Desk helpers for
operators.

## Architecture Anchors

- Runtime semantics live in `sync/sync/service/runtime.py`.
- Public whitelisted API methods live in `sync/api.py`.
- Partner connector implementations live in `sync/sync/service/connectors.py`.
- Desk JavaScript helpers and form controllers live in `sync/public/js/`.
- App hooks and default setup live in `sync/hooks.py` and `sync/setup.py`.
- The scheduler calls
  `sync.sync.service.runtime.run_due_sync_definitions_scheduled`.

## Core Doctypes

- `Sync Definition`: main sync configuration.
- `Sync Partner`: external system connection/configuration.
- `Sync Partner Type`: connector family metadata such as MSSQL, Postgres, and
  Firebird.
- `Sync Run`: one execution of a sync definition.
- `Sync Run Item`: one processed record outcome within a run.

Important child doctypes include `Sync Field Mapping`, `Sync Key Field`,
`Sync Modified Field`, and `Sync Value Mapping`.

## Runtime Invariants

- `SyncDefinitionConfig` is the normalized runtime contract. Keep runtime
  behavior aligned with that object instead of duplicating semantics elsewhere.
- Runtime code is the source of truth for sync behavior. API and frontend code
  should call into it rather than reimplementing sync decisions.
- Field mapping uses this canonical shape:

```python
{
    "frappe_field": {
        "partner_field": "external_field",
        "direction": "Both" | "Frappe to Partner" | "Partner to Frappe",
    }
}
```

- Mapping `direction` affects payload construction. Frappe-to-partner mappings
  are used only when writing partner payloads; partner-to-Frappe mappings are
  used only when writing Frappe payloads; `Both` applies in both directions.
- Match-field mappings must be valid for the active sync direction.
- `table_name` is the partner write target. `read_query` is read-only and must
  never change where writes go.
- `delete_missing` must not run with `read_query`.
- `delete_missing` may only act after a complete source load. Partial or failed
  source reads must fail before destructive cleanup.
- Delta sync requires configured modified fields on both the Frappe and partner
  sides.
- Partner time zones must be valid IANA zone names. Naive partner datetimes are
  interpreted in the configured partner time zone before comparison or mapping.
- One-way matching supports `first_match` and `all_matches`; preserve those
  semantics when changing matching code.
- Audit centers on `Sync Run` and `Sync Run Item`. Keep compact
  `change_count`/`changed_fields` summaries on run items; full payload capture
  is optional behavior, not a requirement for field-level child rows.

## API Rules

- Keep `sync/api.py` thin, permission-aware, and limited to canonical endpoint
  names.
- Do permission checks server-side. UI restrictions are helpful but are not a
  security boundary.
- Import/export responses should keep stable dictionary payload shapes consumed
  by Desk helpers and tests.

## Frontend Rules

- Desk code in `sync/public/js/` must consume canonical API names and canonical
  runtime/import/export payload shapes.
- Client-side validation may improve operator feedback, but server-side
  validation and runtime safety rules take precedence.

## Implementation Conventions

- Prefer existing service helpers, connector APIs, and test patterns.
- Keep changes scoped to the runtime, API, connector, doctype, or Desk layer
  that owns the behavior.
- Preserve batching and audit-write behavior unless the task explicitly changes
  execution semantics.
- Do not relax safety checks around deletes, matching, modified fields, or
  partner datetime normalization.

## Testing

Useful focused checks:

```bash
python -m py_compile sync/sync/service/runtime.py sync/api.py
PYTHONPATH=/workspace/development/frappe-bench/apps/frappe:/workspace/development/frappe-bench/apps/sync /workspace/development/frappe-bench/env/bin/python -m unittest sync.tests.test_api sync.tests.test_runtime_helpers sync.tests.test_runtime_additional sync.tests.test_runtime_management sync.tests.test_setup sync.tests.test_setup_and_doctypes
bench --site development.localhost run-tests --app sync
```

Run the narrowest test set that covers the change, then broaden when touching
runtime semantics, connector behavior, permissions, or import/export contracts.

## JSON Maintenance

Frappe-imported JSON files must have their `modified` field updated whenever
their content changes. This applies especially to DocType, Web Form,
Notification, workspace, sidebar, and desktop JSON files.

## Localization Maintenance

After changing any translatable string in Python, JavaScript, JSON, Web Forms,
or notification templates, run these commands in this exact order:

```bash
bench generate-pot-file --app sync
bench update-po-files --app sync
```

Then fill all missing German translations in `sync/locale/de.po`. No
`msgstr ""` entries may remain after localization work, except the standard PO
header.
