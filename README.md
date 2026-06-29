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

### Production operation

`Sync Settings` controls production housekeeping. `stale_run_timeout_minutes` lets the scheduler and the Desk action recover old `Queued` or `Running` runs that would otherwise block a definition. `run_retention_days_success` and `run_retention_days_error` prune `Sync Run` and `Sync Run Item` audit rows; failures, review runs, and skipped runs are retained longer by default.

For destructive flows, take a backup and run a dry run before enabling scheduled execution. `delete_missing` is allowed only for complete source loads, cannot be combined with `read_query`.

### Contributing & CI

The project uses `pre-commit` with the usual suspects (ruff, eslint, prettier, pyupgrade). Running `bench lint` indirectly benefits from the same setup. CI workflows run unit tests and pip-audit on every push into `develop`.

### License

agpl-3.0
