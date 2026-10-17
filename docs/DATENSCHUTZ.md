# Datenschutz und Datenhaltung – eKI API v1.0.1

Datenschutz-Inventar für die Übergabe: Welche personenbezogenen oder vertraulichen Daten
die eKI verarbeitet, wo sie liegen, ob sie verschlüsselt sind, wann sie gelöscht werden
und wer darauf zugreifen kann. Alle Angaben sind gegen den Code geprüft (Stand 16.10.2026,
Release 1.0.1). Das Dokument ist eine technische Zuarbeit für das Verzeichnis von
Verarbeitungstätigkeiten und die TOM-Dokumentation der Filmakademie; es ersetzt keine
rechtliche Bewertung.

> **Neu in 1.0.1 (Projektentscheidungen):** Metadaten werden nach **90 Tagen**
> automatisch gelöscht (§4); die **Temporal-Historie ist verschlüsselt** (§3.3); die **echten
> Drehbücher** sind aus Repository und Images entfernt – in der Git-Historie und in Images bis
> einschließlich 1.0.0 sind sie weiterhin enthalten (§8); die **Knowledge Base** ist nur noch mit
> Admin-Keys zugänglich (§6); die Inferenz läuft auf einer **externen Ollama-Instanz** (§5).

---

## 1. Grundsatz und Abweichungen davon

Die eKI arbeitet nach dem Processing-Only-Prinzip (Pflichtenheft §4.1/§4.2): Drehbuch und
Report liegen nur transient und verschlüsselt im Redis-Buffer und werden nach Zustellung
gelöscht. Dauerhaft gespeichert werden Metadaten.

Bei der datenschutzrechtlichen Bewertung sind folgende Punkte zu berücksichtigen:

1. **Temporal-Historie:** Drehbuchtext und Findings laufen nur als Buffer-Referenz durch den
   Workflow. Seit 1.0.1 sind zusätzlich alle Temporal-Payloads (Workflow- und Activity-Ein- und
   -Ausgaben, Header, Fehlermeldungen und Stacktraces) AES-256-GCM-verschlüsselt (Schlüssel aus
   `API_SECRET_KEY`, HKDF-Label `eki-temporal-codec-v1`). Im Klartext bleiben nur technische
   Kennungen: `job_id` (als Workflow-ID), Workflow-/Activity-Namen, Task-Queue, Zeitstempel,
   Retry-Zähler, Priorität und Fehlertypen. Historien von Workflows, die **vor dem 1.0.1-Update**
   gestartet wurden, enthalten weiterhin unverschlüsselt Metadaten (`project_id`, `user_id`,
   `script_id`, Client-`metadata`) und bei PDFs den **Titel des Drehbuchs**, bis die
   Namespace-Retention sie löscht; ein über das Update hinweg laufender Workflow hat eine
   gemischte Historie (§3.3).
2. **Knowledge Base:** Die Chunk-Texte der KB-Dokumente liegen im Klartext in
   `kb_embeddings.chunk_text`; nur der Originaltext ist verschlüsselt (§3.2).
3. **Metadaten** (Jobs, Reports, Dead Letters, API-Keys, Audit-Log) werden dauerhaft gespeichert,
   seit 1.0.1 aber nach **90 Tagen** automatisch gelöscht (§4).

---

## 2. Welche Daten verarbeitet werden

| Kategorie | Beispiele | Personenbezug |
|---|---|---|
| Drehbuchinhalt | FDX/PDF-Datei, extrahierter Text, Szenen, Dialoge, Titel | vertraulich (urheberrechtlich geschützt); kann Personendaten enthalten (Titelseite, Projektmappen mit Kontaktdaten, biografische Stoffe) |
| Analyseergebnis | Findings (`description`, `recommendation`, `evidence`, `vulnerability` …), PDF-Report, `assessment`-Text für ePro | vertraulich; `evidence` zitiert das Drehbuch |
| Projekt-/Dokumentbezug | `project_id` (ePro poAllgemeinId), `script_id` | vertraulich, kein direkter Personenbezug |
| Nutzerkennung | `user_id` (frei gewählt beim Anlegen des API-Keys), `organization_id`, `uploaded_by`, `acknowledged_by` | personenbezogen, sobald Klarnamen/E-Mail-Adressen verwendet werden |
| Zugangsdaten | API-Key (nur als SHA-256-Hash gespeichert), `API_SECRET_KEY`, DB-Passwort, `MISTRAL_API_KEY`, `EPRO_AUTH_TOKEN` | Geheimnisse |
| Netzwerkdaten | Client-IP-Adresse (Rate Limiting, Access-Logs) | personenbezogen |
| KB-Dokumente | Sicherheitsleitfäden, SOPs, Checklisten | in der Regel ohne Personenbezug; abhängig vom Inhalt |
| Client-`metadata` | frei wählbare Schlüssel/Werte im Request (max. 50) | abhängig vom Inhalt; nicht für Personendaten vorgesehen |

---

## 3. Speicherorte

### 3.1 Redis – SecureBuffer (transient, verschlüsselt)

- **Was:** Drehbuch (Base64), Zwischenergebnisse (extrahierter Text, Szenenblöcke,
  strukturierte Szenen, geparstes Drehbuch) und das Report-Paket (JSON-Report + PDF).
  Schlüssel `eki:buf:<uuid>` (`services/secure_buffer.py`).
- **Verschlüsselung:** Fernet (AES-128-CBC + HMAC-SHA256). Schlüssel = SHA-256 von
  `API_SECRET_KEY`. Wer `API_SECRET_KEY` und Zugriff auf Redis hat, kann entschlüsseln.
- **Löschung:**
  - Jeder Verarbeitungsschritt löscht seine Eingabe, sobald das Ergebnis gespeichert ist
    (Drehbuch nach dem Parsen, Text nach dem Split usw.).
  - Findings pro Szene (seit Oktober 2026 ebenfalls im Buffer statt in der Temporal-Historie)
    → beim Zusammenbau des Reports.
  - Report-Paket: Push → direkt nach 2xx von ePro; Pull → beim One-Shot-GET; endgültiger
    Zustellfehler → `cleanup_buffer_activity`.
  - Scheitert die Verarbeitung (Parsing, Risikoanalyse, Prompt-Injection-Filter) oder wird der
    Workflow abgebrochen, löscht der Workflow sofort alle bis dahin erzeugten Buffer-Einträge.
  - Sicherheitsnetz: Redis-TTL `BUFFER_TTL_SECONDS` (Standard 21 600 s = 6 h) für jeden Eintrag,
    auch wenn ein Workflow mittendrin abbricht.
- **Persistenz auf Platte:** In Produktion läuft Redis mit `appendonly yes`
  (`docker-compose.prod.yml`), im Entwicklungs-Compose mit den Redis-Standard-Snapshots. Die
  verschlüsselten Einträge landen damit auch im Volume `redis_data`; gelöschte Einträge
  verschwinden dort erst mit dem nächsten AOF-Rewrite bzw. Snapshot. Redis wird nicht
  gesichert (`OPERATIONS_GUIDE.md`).
- **Rate Limiting:** Ebenfalls in Redis: `rate_limit:ip:<Client-IP>` (TTL 60 s) und
  `rate_limit:api_key:<16 Hex-Zeichen des Key-Hashes>` (TTL 1 h).
- **Dokument-Limit:** `eki:docslot:holders`, `eki:docslot:queue`, `eki:docslot:seen` enthalten nur
  Job-IDs mit Zeitstempeln bzw. Priorität, keine Inhalte. Ein Eintrag verschwindet mit der Freigabe nach
  der Risikoanalyse bzw. bei Fehler/Abbruch. Die Schlüssel haben keine Redis-TTL: Endet ein Workflow
  ohne Freigabe (Terminate, Workflow-Timeout, gescheiterte Freigabe), bleibt der Eintrag stehen, bis ein
  beliebiger Job das nächste Mal nach einem Platz fragt; dann entfernt die eKI Einträge mit abgelaufener
  Lease (30 min) bzw. Wartende ohne Nachfrage seit 5 min. Kommen keine neuen Jobs, bleiben die Job-IDs
  so lange in Redis (und im AOF) stehen (`OPERATIONS_GUIDE.md` §5.12).

### 3.2 PostgreSQL – Anwendungsdatenbank (`eki_db`)

Alle Tabellen aus `core/db_models.py`. Die Datenbank selbst ist **nicht** von der eKI
verschlüsselt; Verschlüsselung des Volumes `postgres_data` ist Sache des Hosts.

| Tabelle | Inhalt | Klartext / verschlüsselt | Automatische Löschung |
|---|---|---|---|
| `api_keys` | `user_id`, `organization_id`, `name`, `description`, `key_hash` (SHA-256), `is_active`, `is_admin`, `created_at`, `expires_at`, `last_used_at`, `usage_count` | Klartext; der Key selbst nur als Hash | nur nicht mehr nutzbare Keys: gesperrte 90 Tage nach letzter Nutzung, abgelaufene 90 Tage nach Ablauf (§4); aktive Keys nie (Ablauf per `expires_at`, Standard 365 Tage) |
| `job_metadata` | `job_id`, `project_id`, `user_id`, `script_format`, `status`, `priority` (Request-Wert 1–10), `progress_percentage`, `report_id`, `error_message` (max. 1000 Zeichen, inhaltsfrei), `idempotency_key`, `delivery_mode`, `script_id`, Zustell-Buchhaltung (`delivery_*`, `delivered_at`), Zeitstempel. `extra_metadata` und `workflow_id` werden nicht befüllt; `callback_url` wird nicht gespeichert. | Klartext, keine Drehbuchinhalte | ja, 90 Tage nach Anlage und letzter Änderung (§4) |
| `report_metadata` | `report_id`, `job_id`, `project_id`, `user_id`, `script_format`, `total_findings`, `processing_time_seconds`, `report_ref_key` (Redis-Schlüssel), `is_retrieved`, `retrieved_at`, `delivery_mode` | Klartext, keine Findings | ja, 90 Tage nach Anlage bzw. Abruf (§4) |
| `delivery_dead_letters` | `job_id`, `report_id`, `project_id`, `user_id`, `delivery_mode`, `reason`, `attempts`, `last_status_code`, `last_error_type` (derzeit nie befüllt), `webhook_sent`, `acknowledged_at`, `acknowledged_by` (Admin-`user_id`), `note` (Freitext des Operators, max. 1000 Zeichen) | Klartext; inhaltsfrei (geprüft: keine Report- oder Drehbuchfelder) | ja, 90 Tage nach Anlage bzw. Quittierung, auch unquittiert (§4) |
| `audit_logs` | Tabelle angelegt (Endpoint, Methode, `user_id`, `project_id`, `request_id`, Status …), wird aber **nirgends beschrieben** | – | ja, 90 Tage (§4; derzeit immer leer) |
| `kb_documents` | `title`, `source`, `tenant_id`, `tags`, `uploaded_by` (`user_id`), `content_hash` (SHA-256 des Texts), `ttl_hours`, `created_at`, `expires_at`; `original_text_encrypted` | Metadaten Klartext; **Originaltext Fernet-verschlüsselt** (Schlüssel = SHA-256(`API_SECRET_KEY` + `eki-kb-doc-v1`)) | ja, nach `expires_at` (siehe §4) |
| `kb_embeddings` | `chunk_text`, `vector` (1024 dim), `chunk_offset`, `length`, `hash` | **`chunk_text` im Klartext** (pgvector kann nicht auf Chiffraten suchen); die Chunks ergeben zusammen den vollständigen Dokumenttext | mit dem Dokument (`ON DELETE CASCADE`) |
| `alembic_version` | Migrationsstand | – | – |

Hinweis zu `API_SECRET_KEY`: Wird der Schlüssel gewechselt, sind Buffer-Inhalte, die
verschlüsselten KB-Originaltexte und (seit 1.0.1) die Temporal-Payloads nicht mehr lesbar. Die
KB-Suche arbeitet mit `chunk_text` und Vektoren und funktioniert weiter; für einen konsistenten
Stand die KB neu einspielen (`scripts/seed_kb.py --reseed`, Admin-Key).

### 3.3 PostgreSQL – Temporal (`temporal`, `temporal_visibility`)

Temporal (`temporalio/auto-setup`) legt seine Datenbanken im selben PostgreSQL-Server an.
Die Workflow-Historie enthält Eingaben und Ergebnisse jeder Activity. Geprüft
(`workflows/security_check.py`, `workflows/activities.py`, `workflows/maintenance.py`):

| In der Historie | Inhalt (seit 1.0.1 verschlüsselt, siehe unten) |
|---|---|
| Workflow-Eingabe `job_data` | Buffer-Referenzschlüssel (kein Drehbuchtext), `job_id`, `report_id`, `project_id`, `user_id`, `script_id`, `delivery_mode`, `priority`, `callback_url` (falls gesetzt), Client-`metadata`, Konfigurationswerte |
| Ergebnisse von `parse_fdx`, `extract_pdf_text`, `split_scenes`, `aggregate_script` | Referenzschlüssel, Zähler, OCR-Seitennummern, Warnungen – kein Text |
| Ergebnis von `structure_scene_llm` für den Vorspann (PDF), Eingabe von `aggregate_script` | **Titel des Drehbuchs** |
| Ergebnisse von `analyze_scene_risk`, Eingabe von `aggregate_report` | Szenennummer und Referenzschlüssel auf die verschlüsselten Findings im Buffer – keine Findings im Klartext |
| Ergebnis von `deliver_report` (Push) | Statuscode und Antwort-Body von ePro |
| Fehlerpfade | inhaltsfreie Meldungen (`delivery_failed:<reason>`, „Parsing fehlgeschlagen: …“, „Szene 7: Risikoanalyse fehlgeschlagen (LLMException)“); Webhook-/Dead-Letter-Payload ist inhaltsfrei |
| Wartungs-Workflows (`KBCleanupWorkflow`, `MetadataCleanupWorkflow`) | nur Zähler und der Stichtag, keine IDs |

**Verschlüsselung (seit 1.0.1).** Ein Payload-Codec (`core/temporal_codec.py`) verschlüsselt
jede Payload, bevor sie API oder Worker verlässt: AES-256-GCM mit zufälliger Nonce; Schlüssel per
HKDF-SHA256 aus `API_SECRET_KEY` (Label `eki-temporal-codec-v1`, unabhängig von den Schlüsseln für
Buffer und KB). Erfasst sind Workflow-Eingabe und -Ergebnis, alle Activity-Ein- und -Ausgaben, die
Temporal-Header (`request_id`, Trace-Kontext) sowie Fehlermeldungen, Stacktraces und Fehlerdetails.
Lesbar bleiben nur, was Temporal selbst braucht: Workflow-ID (= `job_id`), Run-IDs, Workflow- und
Activity-Namen, Task-Queue, Timer, Zeitstempel, Versuchszähler und Retry-Zustand, Timeouts, die
Task-Priorität (eine aus `priority` abgeleitete Zahl) und die Namen von Fehlertypen
(Exception-Klassen wie `ParsingFailed`). Memos und Search Attributes werden nicht verwendet. Die
Schedule-Definitionen (`eki-kb-cleanup`, `eki-metadata-cleanup`: Cron, Task-Queue) enthalten keine
Payloads.

Historien von Workflows, die **vor dem 1.0.1-Update** gestartet wurden, bleiben im Klartext
(Metadaten, Client-`metadata`, Drehbuchtitel wie in der Tabelle; bei sehr alten Workflows bis
Oktober 2026 auch Findings mit Zitaten im Feld `evidence`), bis die Retention sie löscht. Ein
Workflow, der über das Update hinweg lief, hat eine gemischte Historie.

**Aufbewahrung:** Laufzeit des Workflows (bis zur Zustellung bzw. bis Ablauf der Pull-TTL,
höchstens `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` = 10 h) **plus** die Retention des Namespace. Der
Compose setzt `DEFAULT_NAMESPACE_RETENTION` nicht; `temporalio/auto-setup` legt den Namespace
`default` deshalb mit **24 h** an – nur beim ersten Anlegen, eine spätere Variable ändert ihn
nicht. Die Retention ist eine eigene Einstellung und wird vom Metadaten-Cleanup (§4) nicht
erfasst. Vor dem Go-Live prüfen und bei Bedarf ändern (z. B. 72 h):

```bash
docker compose exec temporal temporal operator namespace describe --namespace default --address temporal:7233
docker compose exec temporal temporal operator namespace update --namespace default --retention 72h --address temporal:7233
```

Die Temporal-UI zeigt seit 1.0.1 Eingaben und Ergebnisse nur als `binary/encrypted`
(Base64-Chiffrat) und Fehlermeldungen als `Encoded failure`; lesbar sind IDs, Activity-Namen,
Zeiten und Fehlertypen. Entschlüsseln kann nur, wer `API_SECRET_KEY` hat (optionaler Codec-Server:
`OPERATIONS_GUIDE.md` §8.2). Die UI ist in Produktion nicht veröffentlicht
(`docker-compose.prod.yml`), im Entwicklungs-Compose aber ohne Anmeldung auf Port 8080.

Nachweis: `tests/test_temporal_codec.py` (die gespeicherte Historie eines Prüf-Workflows enthält
keinen Klartext von Workflow-Eingabe, Activity-Ergebnis, Header und Fehlermeldung; ein
Kontrolllauf ohne Codec enthält ihn; Historien von vor 1.0.1 und ein über das Update laufender
Workflow spielen weiter ab) und
`tests/test_e2e_pipeline.py::test_temporal_history_is_encrypted_at_rest` (echter PDF-Lauf: die
Historie enthält weder `project_id`, `user_id`, Client-`metadata`, Drehbuchtitel,
Buffer-Schlüssel `eki:buf:` noch Skripttext; jede Payload ist `binary/encrypted`).

### 3.4 Logs

- **Anwendungslogs** (API und Worker): JSON auf stdout (`core/logging_config.py`). Die
  Log-Hygiene (M08, Abnahmetest 7) maskiert bekannte Inhaltsfelder (`script_content`, `text`,
  `findings`, `description`, `evidence`, `report`, `assessment` u. a.) in strukturierten
  Feldern und `extra`-Attributen; Push-Payloads werden nur als Längen geloggt. Die Maskierung
  ist **schlüsselbasiert**: Frei formulierte Fehlermeldungen (z. B. Schema-Validierungsfehler
  einer LLM-Antwort) können in Einzelfällen kurze Inhaltsfragmente enthalten. Nachweis:
  `tests/test_m08_log_hygiene.py`, Abnahmetest 7 (Marker-Suche in den Logs).
- Logs enthalten `request_id`, `job_id`, `report_id`, teils `project_id` und `user_id`
  (z. B. beim Quittieren von Dead Letters).
- **Access-Logs:** In Produktion schreibt Gunicorn Access-Logs (`--access-logfile -`) mit
  **Client-IP**, Zeitstempel, Request-Zeile (inkl. Job-/Report-ID) und User-Agent; im
  Entwicklungs-Compose tut Uvicorn Entsprechendes.
- **Aufbewahrung:** Die Logs gehen an den Docker-Logging-Treiber (`json-file`). Das Prod-Overlay
  (`docker-compose.prod.yml`) begrenzt sie für alle Dienste **nach Größe**: 20 MB × 5 Dateien je
  Container (ca. 100 MB; die älteste Datei fällt weg, wenn alle voll sind). Eine **zeitliche**
  Frist kennt Docker nicht – wie lange eine Logzeile erhalten bleibt, hängt vom Log-Volumen ab.
  Der Entwicklungs-Compose setzt keine Rotation (Docker-Daemon-Standard). Ist eine zeitliche
  Grenze gefordert (z. B. 90 Tage), die Logs an einen zentralen Log-Speicher mit entsprechender
  Retention weiterleiten (journald/syslog/Loki o. Ä.) – siehe §8.
- **Ollama** läuft seit 1.0.1 als externe Instanz außerhalb des Compose-Stacks; ihre Logs liegen
  beim Betreiber der Instanz. Im Normalbetrieb protokolliert Ollama keine Prompt-Inhalte; den
  Debug-Modus (`OLLAMA_DEBUG`) und `OLLAMA_DEBUG_LOG_REQUESTS` im Betrieb nicht aktivieren – ab
  `OLLAMA_DEBUG=2` landen Modellausgaben (ggf. mit Drehbuchzitaten) im Log, `OLLAMA_DEBUG_LOG_REQUESTS=1`
  speichert ganze Anfragen samt Szenentext.

### 3.5 Observability (nur mit `docker-compose.observability.yml`)

- **Prometheus:** Metriken ohne Inhalte und ohne Nutzer-IDs (Labels: Route-Template, Status,
  Provider, Ergebnis); Aufbewahrung 30 Tage (`--storage.tsdb.retention.time=30d`).
- **Jaeger:** Das Overlay schaltet Tracing ein (`OTEL_ENABLED` Standard `true`). Spans
  enthalten HTTP-Routen und -URLs (Job-/Report-IDs, beim Push auch `project_id` im Pfad),
  SQL-Statements (ohne gebundene Parameterwerte) und Temporal-Activity-Namen, keine
  Request-Bodies. Jaeger all-in-one hält die Daten im Speicher (Verlust bei Neustart).
- **Grafana:** nur Dashboards und das Admin-Konto.

### 3.6 Weitere Ablagen

- `tests/reports/` (git-ignoriert): Ausgaben von Abnahme-, Benchmark- und Paritätsläufen;
  kann echte Report-Inhalte enthalten. Nicht in Tickets/E-Mails weitergeben.
- `tests/fixtures/pdf/private/` (bzw. `EKI_PRIVATE_FIXTURES_DIR`): einzige vorgesehene Ablage für
  echte Drehbücher, die Entwickler lokal mit den Stack-Runnern (`tests/run_pdf_*.py`) prüfen;
  git- und docker-ignoriert. Im Repository liegen nur synthetische Fixtures (§8, Punkt 1).
- `config/kb_seed/real/`: lokale Ablage echter KB-Dokumente vor dem Einspielen. Der Inhalt ist
  git-ignoriert und seit 1.0.1 per `.dockerignore` vom Image-Build ausgeschlossen (vorher konnte
  ein lokaler Build ihn ins Image übernehmen). Im Entwicklungs-Stack sieht der API-Container den
  Ordner über den Bind-Mount. Nach dem Einspielen trotzdem entfernen.
- Backups: Ein `pg_dump` von `eki_db` enthält Metadaten (bis zu ihrer 90-Tage-Löschung in der
  Datenbank, im Backup bis zu dessen Löschung) und die KB (Chunk-Texte im Klartext). Wird der
  ganze PostgreSQL-Server gesichert, sind auch die Temporal-Datenbanken enthalten: deren Payloads
  seit 1.0.1 verschlüsselt (lesbar nur mit dem `API_SECRET_KEY`, der beim Workflow-Lauf galt),
  Historien von vor 1.0.1 im Klartext (Metadaten, Drehbuchtitel), bis ihre Retention abgelaufen ist.

---

## 4. Löschfristen im Überblick

| Daten | Ablage | Wird automatisch gelöscht | Durch |
|---|---|---|---|
| Drehbuch, Zwischenergebnisse | Redis | ja, nach dem jeweiligen Verarbeitungsschritt, spätestens nach `BUFFER_TTL_SECONDS` (6 h) | Activities, Redis-TTL |
| Report-Paket (JSON + PDF) | Redis | ja: Push nach 2xx, Pull beim One-Shot-GET, bei endgültigem Fehler sofort, spätestens nach 6 h | `deliver_report`, `GET /v1/security/reports/{id}`, `cleanup_buffer`, Redis-TTL |
| Workflow-Historie (Drehbuchtitel, Client-`metadata`, IDs – seit 1.0.1 verschlüsselt, im Klartext nur `job_id` und technische Namen) | Temporal-Historie (PostgreSQL `temporal`) | ja, nach Workflow-Ende + Namespace-Retention (Standard von `temporalio/auto-setup`: **24 h**, §3.3) | Temporal |
| KB-Dokumente inkl. Chunks | `kb_documents`, `kb_embeddings` | ja, nach `expires_at` (`ttl_hours`, Standard 720 h = 30 Tage, max. 8760 h) | Temporal-Schedule `eki-kb-cleanup`, täglich 03:00 UTC (`KB_CLEANUP_CRON`); zusätzlich manuell per `DELETE /v1/kb/documents…` (Admin-Key) |
| Rate-Limit-Zähler (IP) | Redis | ja, 60 s bzw. 1 h | Redis-TTL |
| Metriken | Prometheus | ja, 30 Tage | Prometheus |
| Job-Metadaten | `job_metadata` | ja, **90 Tage** (`METADATA_RETENTION_DAYS`) nach Anlage **und** letzter Änderung – in den Endzuständen `completed`/`failed`/`cancelled` und ebenso **verwaiste** Jobs, die noch `pending`/`running`/`delivering` zeigen (ein Workflow lebt höchstens `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` = 10 h plus Pull-Fenster 6 h; ohne Änderung seit 90 Tagen kann er nicht mehr laufen). **Ausnahme:** Ein Job mit Änderung innerhalb der Frist wird (noch) nicht gelöscht; verwaiste Jobs nur, wenn die Frist länger ist als Workflow-Timeout + Buffer-TTL (Sicherung im Code) | Temporal-Schedule `eki-metadata-cleanup`, täglich 03:30 UTC (`METADATA_CLEANUP_CRON`) |
| Report-Metadaten | `report_metadata` | ja, 90 Tage nach Anlage und (falls erfolgt) Abruf, auch die Reports verwaister Jobs. **Ausnahme:** solange der zugehörige Job noch läuft | dto. |
| Dead Letters | `delivery_dead_letters` | ja, 90 Tage nach Anlage und (falls erfolgt) Quittierung – **auch unquittierte**. Der Betrieb muss Dead Letters innerhalb der Frist bearbeiten | dto. |
| API-Keys | `api_keys` | nur nicht mehr nutzbare Keys: gesperrte Keys 90 Tage nach **letzter Nutzung** bzw. Anlage (der Zeitpunkt der Sperrung wird nicht gespeichert), abgelaufene Keys 90 Tage nach Ablauf (und frühestens 90 Tage nach letzter Nutzung). **Ausnahme:** aktive, nicht abgelaufene Keys werden nie gelöscht | dto. |
| Audit-Log | `audit_logs` | ja, 90 Tage (die Tabelle wird derzeit nicht beschrieben) | dto. |
| Container-Logs (Access-Logs mit Client-IP) | Docker (`json-file`) | Produktion: **nur nach Größe** (20 MB × 5 Dateien je Container), keine zeitliche Frist; Entwicklungs-Compose: keine Rotation | Docker / Betrieb (zeitliche Frist nur per zentraler Log-Weiterleitung, §8) |

**Metadaten-Aufbewahrung (seit 1.0.1).** Die Frist ist auf **90 Tage** festgelegt (Standard; die
Filmakademie als Verantwortliche kann sie über `METADATA_RETENTION_DAYS` ≥ 1 anpassen,
`CONFIGURATION.md` §3.13). Regel:
Eine Zeile wird erst gelöscht, wenn **alle** ihre Zeitstempel älter als der Stichtag (jetzt minus
Frist) sind; ein fehlender Zeitstempel (z. B. nie abgerufen, nie quittiert) zählt als „Ereignis
fand nicht statt“. Gelöscht wird in Stapeln von 1000 Zeilen je Transaktion, mit erneuter Prüfung
beim Löschen (eine inzwischen geänderte Zeile, etwa ein reaktivierter Key, bleibt). Der Cleanup
protokolliert nur Zähler je Tabelle und den Stichtag – keine IDs, keine Nutzer- oder
Projektkennungen; seine Temporal-Historie enthält ebenfalls nur Zähler und Stichtag. Werden
verwaiste Jobs gelöscht, warnt der Worker im Log (Hinweis auf Workflows ohne abschließendes
DB-Update). Betrieb und Kontrollabfragen: `OPERATIONS_GUIDE.md` §5.10.

Nicht vom Metadaten-Cleanup erfasst und weiter eine Einstellung des Betriebs: die Retention des
Temporal-Namespace (§3.3) und eine zeitliche Grenze für Container-Logs. Für Logs mit Client-IPs
wird empfohlen, sie an einen zentralen Log-Speicher mit 90 Tagen Retention weiterzuleiten, weil
Docker nur nach Größe rotiert. Backups enthalten gelöschte Metadaten bis zu ihrer eigenen
Löschung (§7).

---

## 5. LLM-Verarbeitung

| Provider | Wo läuft die Inferenz | Zulässig |
|---|---|---|
| `local_mistral` (Standard in Produktion, Modell `mistral-small3.2`) | lokal auf der externen Ollama-Instanz der Filmakademie (`OLLAMA_BASE_URL`, GPU-Host im internen Netz) | Produktion |
| `ollama` (beliebiges lokales Modell) | ebenda | Produktion möglich (Paritätsreport beachten) |
| `mistral_cloud` | **Mistral AI (Cloud-API `api.mistral.ai`)** – Drehbuchszenen verlassen das Netz der Filmakademie | nur Stage/Test |

- Seit 1.0.1 ist Ollama nicht mehr Teil des Compose-Stacks: Drehbuchszenen gehen per HTTP an die
  externe Ollama-Instanz. Sie gehört damit zur Verarbeitungsumgebung: nur im internen Netz
  betreiben, Port 11434 nur für die eKI-Hosts freigeben (die Ollama-API hat keine Anmeldung),
  keine Prompt-Protokollierung (`OLLAMA_DEBUG` und `OLLAMA_DEBUG_LOG_REQUESTS` aus); für die Instanz dieselben TOM ansetzen wie
  für die eKI selbst (`OPERATIONS_GUIDE.md` §4.11).

- Der Produktions-Validator (`api/config.py`) verweigert den Start mit
  `LLM_PROVIDER=mistral_cloud`, solange `LLM_ALLOW_EXTERNAL_PROVIDERS` nicht ausdrücklich auf
  `true` steht; die Factory (`llm/factory.py`) prüft das ein zweites Mal. Als zusätzliche
  Absicherung ausgehenden Verkehr zu `api.mistral.ai` per Firewall sperren
  (`GO_LIVE_CHECKLIST.md`).
- Achtung: Die Code-Voreinstellung von `LLM_PROVIDER` ist `mistral_cloud`. Außerhalb von
  `ENV=production` greift der Validator nicht – Stage- und Testsysteme also immer explizit
  konfigurieren.
- **Stage-Sign-off (Abnahmetest 8) mit Mistral Cloud:** Vorher einen
  Auftragsverarbeitungsvertrag (AVV/DPA nach Art. 28 DSGVO) mit Mistral AI abschließen und
  die Bedingungen zur Datennutzung prüfen – oder ausschließlich synthetische bzw. zur Nutzung
  freigegebene Testdrehbücher verwenden. Den AVV schließt die Filmakademie als
  Verantwortliche selbst ab.
- Embeddings für die KB entstehen immer lokal über Ollama; mit `mistral_cloud` ist die KB nicht
  nutzbar (`M06_KB_GUIDE.md`).

---

## 6. Zugriffskontrolle

| Zugang | Wer | Sieht / darf |
|---|---|---|
| `POST /v1/security/check:async`, `GET /v1/security/jobs/{id}`, `GET /v1/security/reports/{id}` | jeder gültige API-Key | nur **eigene** Jobs und Reports: Abfragen sind auf die `user_id` des Keys gefiltert, fremde IDs liefern 404 (IDOR-Schutz); Idempotenz-Keys gelten pro `user_id` |
| `X-Actor-User-Id` | optionaler Header | muss zur `user_id` des Keys passen, sonst 403; wird nicht gespeichert |
| `X-Actor-Project-Id` | optionaler Header | nur informativ, wird weder gespeichert noch ausgegeben |
| `/v1/kb/*` | seit 1.0.1 nur Keys mit `is_admin=true` (sonst 403) | alle KB-Dokumente des (einzigen) Mandanten auflisten, hochladen und löschen; `uploaded_by` = `user_id` des Admin-Keys; der Originaltext wird nie ausgeliefert. Der ePro-Service-Key hat keinen Zugriff |
| `/v1/ops/*` | nur Keys mit `is_admin=true` | Metadaten **aller** Jobs und Dead Letters (`project_id`, `user_id`, Fehlermeldung) – keine Inhalte |
| `/metrics`, `/ready` | jeder gültige API-Key | Metriken bzw. Dienststatus |
| `/health` | ohne Anmeldung | Version und Status |
| Swagger/ReDoc/`/openapi.json` | nur bei `DEBUG=true` | – (in Produktion abgeschaltet; der Validator erzwingt `DEBUG=false`) |
| PostgreSQL, Redis, Temporal | nur im internen Docker-Netz | wer Shell-Zugriff auf den Host hat, sieht Metadaten und KB-Chunks; Temporal-Historien seit 1.0.1 nur verschlüsselt (ältere Historien bis zum Ablauf der Retention im Klartext: Titel, Client-`metadata`); mit zusätzlich `API_SECRET_KEY` auch die Buffer-Inhalte und die Temporal-Payloads |
| Ollama (externe Instanz) | eKI-Hosts über das interne Netz (die Ollama-API hat keine Anmeldung) | wer die Instanz betreibt bzw. erreicht, kann Modelle nutzen; Prompts mit Drehbuchszenen werden nicht gespeichert, solange `OLLAMA_DEBUG` und `OLLAMA_DEBUG_LOG_REQUESTS` aus sind |
| Temporal-UI | Produktion: nur per SSH-Tunnel | Workflow-IDs (`job_id`), Activity-Namen, Zeiten, Fehlertypen; Eingaben, Ergebnisse und Fehlermeldungen seit 1.0.1 nur verschlüsselt (`binary/encrypted`, `Encoded failure`) |

Weitere Punkte:

- API-Keys sind zufällige 256-Bit-Token (`eki_` + 64 Hex-Zeichen) und werden nur als SHA-256-Hash
  gespeichert; der Klartext wird beim Anlegen einmal angezeigt. Es gibt keine Scopes: Auch der
  Prometheus-Key ist ein vollwertiger Nutzer-Key.
- Die `user_id` ist frei wählbar. Empfehlung: technische Kennungen (`epro-service`,
  `ops-admin-01`, `prometheus-scraper`) statt Namen oder E-Mail-Adressen.
- Rate Limits: 60 Anfragen/min je Quell-IP und 1000/h je API-Key (Redis; bei Redis-Ausfall
  wird nicht begrenzt). Hinter einem Reverse-Proxy nur mit `TRUST_PROXY_HEADERS=true` und
  `TRUSTED_PROXY_IPS` die echte Client-IP verwenden.
- Transport: Die API spricht selbst nur HTTP; TLS terminiert der Reverse-Proxy (Go-Live-Checkliste).
  Der Push zu ePro und der Webhook gehen an `EPRO_BASE_URL`/`EPRO_WEBHOOK_URL`, optional mit
  `Authorization: Bearer <EPRO_AUTH_TOKEN>`.
- `callback_url` (seit 1.0.1): Ein Request kann ein eigenes Push-Ziel angeben; Report, PDF und
  ggf. der ePro-Bearer-Token gehen dann dorthin. Deshalb sind nur HTTPS-URLs ohne Zugangsdaten auf
  Hosts aus `CALLBACK_URL_ALLOWED_HOSTS` (Standard: die beiden ePro-Hosts) zulässig; vor jedem Push
  prüft der Worker die Allow-List erneut, löst den Host auf, verlangt ausschließlich öffentliche
  Adressen und verbindet sich mit der geprüften Adresse (Schutz gegen DNS-Rebinding); das
  TLS-Zertifikat wird gegen den Hostnamen geprüft. Die URL wird nicht in PostgreSQL gespeichert,
  sondern läuft (verschlüsselt) in der Workflow-Eingabe durch Temporal.

---

## 7. Löschung auf Anfrage

Ohne Anfrage löscht der Metadaten-Cleanup nach 90 Tagen ohnehin (§4); die folgenden Schritte
sind nur nötig, wenn früher gelöscht werden soll.

- **Ein einzelner Job:** Inhalte sind spätestens nach 6 h aus Redis verschwunden. Die
  Temporal-Historie (seit 1.0.1 verschlüsselt) verschwindet mit der Namespace-Retention; die
  Metadatenzeilen (`job_metadata`, `report_metadata`, ggf. `delivery_dead_letters`) per SQL nach
  `job_id` löschen. Achtung: `job_metadata.status` speichert die Enum-Namen in **Großbuchstaben**
  (`'COMPLETED'`, `'FAILED'`, `'CANCELLED'` …) – eigene SQL-Filter auf den Status müssen sie so
  schreiben.
- **Ein Nutzer/API-Key:** Key deaktivieren (`UPDATE api_keys SET is_active=false …`), die
  Metadaten nach `user_id` löschen. Der gesperrte Key selbst wird automatisch gelöscht, sobald
  seine letzte Nutzung 90 Tage zurückliegt (§4).
- **KB-Dokument:** `DELETE /v1/kb/documents/{doc_id}` (Admin-Key) löscht Dokument und Chunks.
- Backups und Redis-AOF enthalten gelöschte Daten bis zu ihrem eigenen Ablauf.

---

## 8. Entscheidungen und offene Punkte für die Filmakademie

### 8.1 Entschieden und umgesetzt (Release 1.0.1)

1. **Echte Drehbücher – aus Repository und Images entfernt; Git-Historie und alte Images
   bleiben.** Die 15 echten Drehbücher, Treatments, Projektmappen und die Production Bible von
   Filmakademie-Produktionen (u. a. TRANCE, Sinkende Schiffe, H2Gold, Babystar, Matroshki,
   Königskinder, Gaïa) wurden mit Release 1.0.1 aus dem Repository
   (Arbeitsstand) und aus beiden Images entfernt: Das API-Image enthält nur noch die synthetischen
   FDX-Dateien (`tests/fixtures/fdx/`, Standardeingabe von `scripts/smoke_prod.py`), das
   Worker-Image keine Testdaten; `.dockerignore` hält `tests/fixtures/pdf/` (inkl. `private/`),
   Testberichte, Secrets und `.env*` aus jedem Build heraus. Ersetzt wurden sie durch
   reproduzierbare synthetische Fixtures mit denselben Layout-Merkmalen.
   **Sie bleiben aber in der Git-Historie** (seit März 2026) und in **bereits veröffentlichten
   GHCR-Images bis einschließlich 1.0.0**. Projektmappen können Kontaktdaten realer Personen
   enthalten. Die Historie wird nach Projektentscheidung nicht umgeschrieben. Eine
   vollständige Entfernung bräuchte ein Umschreiben der Historie (z. B. je Datei
   `git filter-repo --path tests/fixtures/pdf/<datei> --invert-paths`, Force-Push, Neuklonen aller
   Kopien und Forks, Bitte an den GitHub-Support, zwischengespeicherte Ansichten/Pull-Requests zu
   bereinigen) und das Löschen der alten GHCR-Image-Versionen. Empfehlung: Sichtbarkeit von
   Repository und GHCR-Paketen prüfen (privat halten) und die GHCR-Versionen ≤ 1.0.0 löschen
   bzw. den Zugriff beschränken.
   **Fixture-Regel:** Im Repository liegen nur synthetische Fixtures (in `.gitignore` einzeln
   freigegeben, erzeugt von `tests/generate_pdf_fixtures.py` und `tests/build_large_fixture.py`);
   ein neues PDF unter `tests/fixtures/pdf/` wird ignoriert, solange es nicht bewusst freigegeben
   wird. Echte Drehbücher gehören nur lokal nach `tests/fixtures/pdf/private/` (§3.6).
2. **Löschfrist für Metadaten: 90 Tage**, automatisch per `eki-metadata-cleanup` (§4). Dazu
   entschieden: Dead Letters werden nach Ablauf gelöscht, **quittiert oder nicht** (der Betrieb
   bearbeitet sie innerhalb der Frist); **verwaiste** Jobs (`pending`/`running`/`delivering` ohne
   Änderung seit Fristbeginn) werden mitgelöscht; es gibt **keine** Spalte für den
   Sperrzeitpunkt von API-Keys – die Frist eines gesperrten Keys läuft ab seiner letzten Nutzung.
3. **Temporal-Historie verschlüsselt:** Payload-Codec mit AES-256-GCM (§3.3). Offen bleibt die
   Namespace-Retention (§8.2).
4. **KB-Zugriff nur mit Admin-Keys:** `/v1/kb/*` verlangt `is_admin=true` (§6).
5. **Inferenz auf externer Ollama-Instanz** statt eines Ollama-Containers im Stack (§5).

### 8.2 Offen

1. **Temporal-Namespace-Retention** festlegen, setzen und prüfen (Standard 24 h, §3.3); sie ist
   eine Betriebseinstellung und wird vom Metadaten-Cleanup nicht erfasst.
2. **AVV mit Mistral AI** für den Stage-Sign-off – schließt die Filmakademie selbst ab – oder nur
   freigegebene Testdrehbücher (§5).
3. **Log-Aufbewahrung:** Das Prod-Overlay begrenzt Container-Logs nur nach Größe (20 MB × 5 je
   Container). Eine zeitliche Grenze für die Logs (sie enthalten IP-Adressen) nur per
   Weiterleitung an einen zentralen Log-Speicher; empfohlen 90 Tage (§3.4).
4. **Verschlüsselung der Volumes** (`postgres_data`, `redis_data`) auf Host-Ebene und
   Aufbewahrung der Backups.
5. **Konvention für `user_id`** (pseudonym, §6).
6. **Alte GHCR-Images ≤ 1.0.0** löschen bzw. Zugriff beschränken; über ein Umschreiben der
   Git-Historie bei Bedarf gesondert entscheiden (§8.1, Punkt 1).
