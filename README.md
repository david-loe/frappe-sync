### Overview

Sync anything from and to Frappe, allowing a single `Sync Definition` to describe how a DocType mirrors data from MSSQL, PostgreSQL or Firebird sources.

### Prerequisites

1. A Frappe bench (>=16.0) with Python 3.14 from the base repo.
2. The operating-system database drivers listed below.

### Database driver requirements

| Database | ⚠️ Manual system installation required | Debian/Ubuntu command |
| --- | --- | --- |
| MSSQL | [Add the Microsoft repository](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server), then install UnixODBC and Microsoft ODBC Driver 18 | `sudo ACCEPT_EULA=Y apt install msodbcsql18 unixodbc-dev` |
| PostgreSQL | Nothing | - |
| Firebird | Firebird client library | `sudo apt install libfbclient2` |

For debian the [install-requirments.sh](/install-requirements.sh) can be used.

### Installation

Install the required system driver for the database you intend to use before
testing a connection. Then install the app and its Python requirements:

```bash
bench get-app https://github.com/david-loe/frappe-sync
bench install-app sync
bench setup requirements
```

### Configuration snapshot

Each sync entry links to a `Sync Partner` (host / port / credentials) plus a `Sync Definition` describing the DocType, cron schedule, batch size, direction (`Frappe -> Partner`, `Frappe <- Partner`, `Frappe <-> Partner`), field mapping, value mapping and granular options such as `create_new` or `delete_missing`.

The app ships with Desk helpers (Run, Preview, Export YAML, Import YAML, Open Latest Run, Test Connection). Use the YAML export/import as a transport format, but keep secret values out of shared exports.

### Frappe source scripts

`Frappe -> Partner` sync definitions normally load Frappe records through the selected DocType plus `Filter Expression`. For custom source shapes, set `Frappe Source Mode` to `Python Script` and assign `records = [...]` in the script. Script sources require `server_script_enabled`. Each record is mapped and written through the normal partner row upsert path, so a script can also emit one record with a prebuilt JSON field when the partner expects aggregated data.

### Read Query templating

`Read Query` can optionally be rendered as a safe Jinja template before partner reads by enabling `Render Read Query Template` on the `Sync Definition`. Templating only affects `read_query`; `table_name` remains the partner write target. `delete_missing` still cannot be combined with any `read_query`, templated or not.

Available template values and helpers:

| Name | Returns |
| --- | --- |
| `current_year` | Current calendar year as an integer. |
| `previous_year` | Previous calendar year as an integer. |
| `quote_identifier(value)` | The connector-quoted SQL identifier for the active partner dialect. |
| `source_tables(schema=None, filter=None)` | A list of source table objects with `schema`, `name`, `full_name` and `quoted_name`. Optional filters match schema exactly and table name/full name by substring. |

No Frappe objects, database handles, connector objects or Python globals are exposed to templates.

### Production operation

`Sync Settings` controls production housekeeping. `stale_run_timeout_minutes` lets the scheduler and the Desk action recover old `Queued` or `Running` runs that would otherwise block a definition. `run_retention_days_success` and `run_retention_days_error` prune `Sync Run` and `Sync Run Item` audit rows; failures, review runs, and skipped runs are retained longer by default.

For destructive flows, take a backup and run a dry run before enabling scheduled execution. `delete_missing` is allowed only for complete source loads, cannot be combined with `read_query`.

### Dry run mode

Dry runs create `Sync Run` and `Sync Run Item` audit rows without writing to Frappe or the partner system.

Limitations:
- Frappe and partner creates, updates and deletes are not executed.
- `Sync Definition` runtime fields such as `last_run`, `last_run_status`, `last_sync_at` and `last_successful_sync` are not updated.
- Dry-run successes are not used as the delta baseline for later runs.
- Dry-run items cannot be manually resolved.
- Connector-side create behavior is only simulated.
- `partner_create_id_strategy = max_plus_one` does not reserve or calculate the final partner ID, so the previewed partner ID may stay empty even though a real insert would assign it.

### Contributing & CI

The project uses `pre-commit` with the usual suspects (ruff, eslint, prettier, pyupgrade). Running `bench lint` indirectly benefits from the same setup. CI workflows run unit tests and pip-audit on every push into `develop`.

### License

agpl-3.0
