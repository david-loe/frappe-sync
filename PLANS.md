# Sync App v1 Status

## Ziel
- Die App `sync` soll Frappe-Doctypes mit externen relationalen Datenquellen synchronisieren.
- Start-Typen fuer v1: `mssql`, `postgres`, `firebird`.
- Prioritaeten: Performance, Nachvollziehbarkeit der Sync-Laeufe und gute UX fuer die Konfiguration.

## Aktueller Stand
- Die App ist strukturell umgesetzt, auf `development.localhost` installiert und nach den letzten Integrationen erneut migriert und getestet.
- Default-Partner-Typen werden ueber `sync.setup.ensure_default_partner_types()` gepflegt.
- Die App hat jetzt nicht mehr nur ein Geruest, sondern eine funktionierende v1-Basis fuer:
  - Konfiguration ueber Doctypes
  - Scheduler/Queueing
  - Run-Historie
  - YAML-Export/-Import
  - Preview und Connection-Test
  - Delta-Sync-Grundlogik
  - One-way und bidirektionale Runtime-Pfade
  - Desk-UX fuer Bedienung und Debugging
- Pakete & Dokumentation:
  - Runtime-Abhaengigkeiten fuer Connectoren (`pyodbc`, `psycopg[binary]`, `firebird-driver`, `croniter`) stehen in `pyproject.toml` und `requirements.txt`.
  - README beschreibt Installation, notwendige DB-Treiber und Security-Hinweise zu Secrets, YAML und Run-History.

## Bereits umgesetzt
- Konfigurations-Doctypes:
  - `Sync Partner Type`
  - `Sync Partner`
  - `Sync Definition`
  - `Sync Key Field`
  - `Sync Field Mapping`
  - `Sync Value Mapping`
  - `Sync Modified Field`
- Audit-Doctypes:
  - `Sync Run`
  - `Sync Run Item`
  - `Sync Run Item Change`
- Hook-Integration:
  - `scheduler_events` fuer periodisches Pruefen faelliger Definitionen
  - `after_migrate` zum Seeden der Partner-Typen
  - Desk-JS fuer `Sync Definition`, `Sync Partner`, `Sync Run` und `Sync Run Item`
  - List-View-JS fuer `Sync Run` und `Sync Run Item`
- Backend:
  - API-Endpunkte in `sync/api.py`
  - Service-Layer unter `sync/sync/service/`
  - echte treiberfaehige Connector-Basis fuer `mssql`, `postgres`, `firebird`
  - Queueing und Duplicate-Run-Schutz
  - Preview
  - YAML-Export/-Import
  - Connection-Test
  - Last-success-Tracking ueber erfolgreiche `Sync Run`-Datensaetze
  - Delta-Window-Logik ueber `use_last_sync_date` und `timestamp_buffer_seconds`
  - Sync-Richtungen:
    - `A->B`
    - `A<-B`
    - `A<->B`
  - Konfliktstrategie `newest_wins`
  - Aktionstypen und Counters fuer `created`, `updated`, `deleted`, `skipped`, `conflict`, `error`
- UX:
  - Buttons fuer Run, Preview, Export, Import, Latest Run, Connection Test
  - dynamische Feldsichtbarkeit fuer Query/Table, Partner-Typ und Auth-Typ
  - neue Konfigurationsfelder wie `preview_limit`, `export_mask_credentials`, `next_run_at`, `last_run_status`, `last_run_summary`
  - Partner-Felder fuer `auth_type`, `api_key`, `api_secret`, `certificate_path`, `connection_notes`, `secret_fields`
  - gefuehrte Feldauswahl fuer Frappe-Feldreferenzen in `Sync Definition`, basierend auf dem gewaehlten Doctype
  - Child-Table-basierte Modified-Fields in `Sync Definition`, mit Legacy-Fallback auf die bisherigen Textfelder
  - lesbare Preview-Darstellung mit Zusammenfassung, Mapping-/Action-Tabellen und Raw-JSON-Fallback
  - `Sync Run` zeigt zugehoerige `Sync Run Item` direkt in der Form
  - `Sync Run Item` kann auf das zugehoerige Frappe-Dokument verlinken, wenn `doctype_name` aufgeloest werden kann
  - native List-Views fuer `Sync Run` und `Sync Run Item` mit Status-/Fehlerfokus und Direktaktionen
  - Desk-seitige Source-/Query-Validierung in `Sync Definition`
  - klarere Partner-Form mit dynamischen Pflichtfeldern, Statushinweisen und besseren Hilfetexten
  - klarere Partner-Abschnitte fuer Connection Health, Driver Options, Authentication und Security Notes
  - manuell ladbare Partner-Spaltenlisten fuer tabellenbasierte Definitionen mit Refresh-Status in `Sync Definition`
  - Partner-Spaltenlisten als Auswahlhilfe fuer `partner_field` und partnerseitige Modified-Fields
  - YAML-Import mit Voransicht, Konflikt-/Warnhinweisen und blockierter Bestaetigung bei ungueltiger Preview
  - erweiterte Monitoring-Ansichten direkt in `Sync Run` und `Sync Run Item`, zusaetzlich zu den List-Views
  - systematischere Layout-Ueberarbeitung mit `Column Breaks`, Gruppen und operatorfreundlicheren Formularabschnitten fuer `Sync Definition`, `Sync Partner`, `Sync Run` und `Sync Run Item`
- Security/Export:
  - YAML-Export kann Credentials maskieren
  - Secret-Felder aus Partnern/Partner-Typen werden beim Export beruecksichtigt
- Tests:
  - Service-Tests fuer Due-Selection und Duplicate-Run-Guard
  - API-Tests fuer Preview, Partner-Test, YAML-Roundtrip, `run_sync_definition` und die Doctype-Feldauswahl
  - Runtime-Helper-Tests fuer Config-Building, Record-Key-Stabilitaet, YAML-Sanitizing und Upsert-Helfer
  - Connector-Tests fuer Basisverhalten bei fehlender Konfiguration

## Verifiziert
- `python -m compileall sync`
- `bench build --app sync`
- `bench --site development.localhost migrate`
- `bench --site development.localhost run-tests --app sync`
- `bench --site development.localhost run-tests --app sync --module sync.tests.test_api`
- `bench --site development.localhost execute frappe.db.count --args '["Sync Partner Type"]'` liefert `3`
- `bench --site development.localhost execute frappe.get_meta --args '["Sync Definition"]'` zeigt die neuen Runtime-/UX-Felder auf der Site

## Oeffentliche Schnittstellen
- Kern-APIs:
- `run_sync_definition(sync_definition_name, trigger="manual", queue=True, dry_run=False)`
- `run_due_sync_definitions(limit=20, queue=True)`
- `test_sync_partner(sync_partner_name)`
- `preview_sync_definition(sync_definition_name, limit=50)`
- `export_sync_definition_yaml(sync_definition_name)`
- `import_sync_definition_yaml(yaml_payload, overwrite=False)`
- Weitere whitelisted Endpunkte / Kompatibilitaets- und Hilfsfunktionen:
- `list_due_syncs()`
- `run_due_syncs(limit=20, queue=True)`
- `enqueue_sync(sync_definition_name, trigger="manual", queue=True, dry_run=False)`
- `run_sync_now(sync_definition_name, trigger="manual", dry_run=False)`
- `preview_sync(sync_definition_name, limit=50)`
- `export_sync_yaml(sync_definition_name)`
- `import_sync_yaml(yaml_payload, overwrite=False)`
- `import_sync_yaml_from_json(payload, overwrite=False)`
- `get_sync_definition_field_choices(doctype_name)`

## Was fuer eine komplett belastbare v1 noch fehlt

### 1. Live-Validierung gegen echte Datenbanken
- Die Connectoren sind code-seitig implementiert, aber noch nicht gegen echte MSSQL-, Postgres- und Firebird-Systeme im Projekt verifiziert.
- Offen:
  - echte Verbindungs- und Schreibtests je Datenbanktyp
  - Validierung der SQL-Syntax pro Dialekt
  - Verifikation der Pagination und Upsert-Strategie gegen reale Tabellen
  - Treiber-/OS-Kombinationen auf Zielsystemen pruefen

### 2. Produktionshaertung der Sync-Engine
- Die Runtime-Pfade fuer `A->B`, `A<-B` und `A<->B` sind da, aber noch nicht als produktionsreif abgesichert.
- Offen:
  - End-to-End-Tests mit realen Partnerdaten
  - robustes Verhalten bei partiellen Fehlern
  - klarere Retry-/Recovery-Strategien
  - bessere Kontrolle fuer destructive Pfade bei `delete_missing`

### 3. Performance-Optimierung fuer groessere Datenmengen
- Die Logik ist korrekt und batch-orientiert, aber noch nicht fuer hohe Last optimiert.
- Offen:
  - Audit-Schreiben in Batches statt item-by-item
  - moeglichst serverseitige Delta-Filterung im Connector
  - Reduktion redundanter Reads/Writes
  - Lasttests mit realistischen Datenvolumina

### 4. UX fuer produktive Operatoren abrunden
- Die groben v1-Operator-UX-Luecken sind jetzt geschlossen.
- Restliches UX-Polish:
  - dedizierte Dashboard-Seite, falls Monitoring ueber Form-/List-Ansichten hinaus zentral gebuendelt werden soll
  - weitere Konsistenzpruefung fuer selten genutzte Formularpfade und Randfaelle im Import-Dialog

### 5. Betriebsdokumentation
- Installation und Treiberhinweise sind dokumentiert.
- Offen:
  - Beispielkonfigurationen pro Partner-Typ
  - Betriebsdoku fuer Scheduler, Workers und Queue-Diagnostik
  - Hinweise fuer Fehleranalyse bei gescheiterten Sync-Runs

### 6. Erweiterte Tests
- Die aktuelle Test-Suite deckt Geruest, Runtime-Helfer und API-Vertraege ab.
- Vor produktivem Einsatz sollten noch dazu:
  - echte Integrations-Tests gegen Testdatenbanken
  - End-to-End-Tests fuer `A->B`, `A<-B`, `A<->B`
  - Konflikt-Tests mit realen Modified-Timestamps
  - Delete-/Create-New-Tests gegen echte Tabellen
  - Frappe-Integrations-Tests mit Test-Doctypes und echten Hintergrundjobs

## Empfohlene naechste Schritte
1. Fuer jeden Partner-Typ einen echten Integrationspfad gegen Testdatenbanken aufsetzen.
2. Die aktuellen Runtime-Pfade mit realen Daten verifizieren und die Delete-/Conflict-Pfade haerten.
3. Echte Integrationspfade fuer MSSQL, Postgres und Firebird mit Testdatenbanken aufsetzen und die Connector-Dialekte dort verifizieren.
4. Delete-/Conflict-/Recovery-Pfade der Runtime mit realen Daten haerten.
5. Betriebsdokumentation und Beispielkonfigurationen fuer Operatoren fertigziehen.
6. Danach Lasttests und gegebenenfalls eine dedizierte Monitoring-Seite ergaenzen.
