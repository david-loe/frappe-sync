### Overview

Sync anything from and to Frappe, allowing a single `Sync Definition` to describe how a DocType mirrors data from MSSQL, PostgreSQL or Firebird sources.

### Prerequisites

1. A Frappe bench (>=16.0) with Python 3.14 from the base repo.
2. The connector drivers listed below installed globally, because bench apps share the bench-level environment.

### Database driver requirements
- **MSSQL:** install `pyodbc` plus the Microsoft ODBC driver for SQL Server. On Debian/Ubuntu this is typically `sudo apt install msodbcsql18 unixodbc-dev` and `pip install pyodbc`.
- **PostgreSQL:** use `psycopg[binary]`; the wheel bundles libpq, so no system package is strictly required, but libssl and libc6 must be modern.
- **Firebird:** install the Firebird client (`sudo apt install firebird3.0-dev` or equivalent). The app uses `fdb>=2.0.4`, which supports Firebird 2.5 and limited Firebird 3.0 compatibility.
- **Auxiliary:** `croniter` is required for cron parsing when computing due sync definitions.

Those packages are declared in the app dependencies so `bench setup requirements` pulls them once the app is added to your bench.

### Installation

You can install the app with bench:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app sync
bench setup requirements
```

### Configuration snapshot

Each sync entry links to a `Sync Partner` (host / port / credentials) plus a `Sync Definition` describing the DocType, cron schedule, batch size, direction (`Frappe -> Partner`, `Frappe <- Partner`, `Frappe <-> Partner`), field mapping, value mapping and granular options such as `create_new` or `delete_missing`.

The app ships with Desk helpers (Run, Preview, Export YAML, Import YAML, Open Latest Run, Test Connection). Use the YAML export/import as a transport format, but keep secret values out of shared exports.

### Contributing & CI

The project uses `pre-commit` with the usual suspects (ruff, eslint, prettier, pyupgrade). Running `bench lint` indirectly benefits from the same setup. CI workflows run unit tests and pip-audit on every push into `develop`.

### License

agpl-3.0
