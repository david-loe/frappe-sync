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
- Audit-Doctypes:
  - `Sync Run`
  - `Sync Run Item`
  - `Sync Run Item Change`
- Hook-Integration:
  - `scheduler_events` fuer periodisches Pruefen faelliger Definitionen
  - `after_migrate` zum Seeden der Partner-Typen
  - Desk-JS fuer `Sync Definition` und `Sync Partner`
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
- Security/Export:
  - YAML-Export kann Credentials maskieren
  - Secret-Felder aus Partnern/Partner-Typen werden beim Export beruecksichtigt
- Tests:
  - Service-Tests fuer Due-Selection und Duplicate-Run-Guard
  - API-Tests fuer Preview, Partner-Test, YAML-Roundtrip und `run_sync_definition`
  - Runtime-Helper-Tests fuer Config-Building, Record-Key-Stabilitaet, YAML-Sanitizing und Upsert-Helfer
  - Connector-Tests fuer Basisverhalten bei fehlender Konfiguration

## Verifiziert
- `python -m compileall sync`
- `bench build --app sync`
- `bench --site development.localhost migrate`
- `bench --site development.localhost run-tests --app sync`
- `bench --site development.localhost execute frappe.db.count --args '["Sync Partner Type"]'` liefert `3`
- `bench --site development.localhost execute frappe.get_meta --args '["Sync Definition"]'` zeigt die neuen Runtime-/UX-Felder auf der Site

## Oeffentliche Schnittstellen
- `run_sync_definition(sync_definition_name, trigger="manual", queue=True, dry_run=False)`
- `run_due_sync_definitions(limit=20, queue=True)`
- `test_sync_partner(sync_partner_name)`
- `preview_sync_definition(sync_definition_name, limit=50)`
- `export_sync_definition_yaml(sync_definition_name)`
- `import_sync_definition_yaml(yaml_payload, overwrite=False)`

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

### 4. Sicherheit und Datenminimierung
- Secret-Masking im YAML-Export ist eingefuehrt, aber die produktive Sicherheitsgeschichte ist noch nicht vollstaendig.
- Offen:
  - Redaction sensibler Daten in `Sync Run Item` / `Sync Run Item Change`
  - Schutz sensibler Payloads in Run-History und Logs
  - Rollen-/Berechtigungskonzept ueber `System Manager` hinaus
  - Betriebsmodell fuer Secret-Injection oder Vault-Anbindung

### 5. UX fuer produktive Operatoren abrunden
- Die Grund-UX ist vorhanden, aber noch eher technisch.
- Offen:
  - `Sync Run` soll die zugehoerigen `Sync Run Item` direkt in der Form sichtbar machen, idealerweise als Child-Table-artige Liste oder eng integrierte Unteransicht zur besseren Nachvollziehbarkeit
  - `Sync Run Item` soll einen klickbaren Bezug zum zugehoerigen Frappe-Dokument haben, damit man in der UI direkt per Pfeil/Link in den Datensatz springen kann
  - Doctype-Layouts sollen systematisch mit `Column Breaks` und klareren Formulargruppen ueberarbeitet werden; aktuell sind mehrere Formulare noch zu linear und dadurch unuebersichtlich
  - Feldauswahl fuer alle Frappe-Feldreferenzen in `Sync Definition`, basierend auf dem gewaehlten Doctype statt freier Texteingabe
  - Child-Table-basierte Modified-Fields statt Freitextfeldern, inklusive Feldvorschlaegen aus dem gewaehlten Doctype
  - optional ladbare Spaltenliste fuer Partner-Tabellen nach Eingabe des Tabellennamens, idealerweise mit manuellem Refresh-Button
  - Nutzung dieser Partner-Spaltenliste als Auswahlhilfe fuer Mapping- und Modified-Field-Eintraege auf der Partner-Seite
  - besser lesbare Preview-Darstellung statt reinem JSON-Block
  - Query-/Source-Validierung direkt im Desk
  - bessere Listen-/Dashboard-Sicht fuer Runs und Fehler
  - Import-Workflow mit Voransicht und Konflikthinweisen
  - klarere Pflichtfelder und Hilfetexte je Partner-Typ

### 6. Betriebsdokumentation
- Installation und Treiberhinweise sind dokumentiert.
- Offen:
  - Beispielkonfigurationen pro Partner-Typ
  - Betriebsdoku fuer Scheduler, Workers und Queue-Diagnostik
  - Hinweise fuer Fehleranalyse bei gescheiterten Sync-Runs

### 7. Erweiterte Tests
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
3. Die Definitions-UX auf feldbasierte Auswahllisten umstellen:
   - Frappe-Felder aus dem gewaehlten Doctype laden
   - Modified-Fields auf Child Tables umstellen
   - Partner-Spaltenlisten bei Tabellen-basierten Definitionen ladbar machen
4. Die Run-UX verbessern:
   - `Sync Run Items` direkt im `Sync Run` sichtbar machen
   - klickbare Verlinkung vom `Sync Run Item` zum Frappe-Dokument ergaenzen
   - Doctype-Layouts mit Columns und klareren Abschnitten ueberarbeiten
5. Audit- und Payload-Redaction fuer sensible Daten nachziehen.
6. Preview, Run-Monitoring und Import-UX fuer Operatoren weiter verbessern.
7. Danach Lasttests und Betriebsdoku fertigziehen.
