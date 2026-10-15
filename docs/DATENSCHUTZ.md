# Datenschutz und Datenhaltung – eKI API v1.0.0

Datenschutz-Inventar für die Übergabe: Welche personenbezogenen oder vertraulichen Daten
die eKI verarbeitet, wo sie liegen, ob sie verschlüsselt sind, wann sie gelöscht werden
und wer darauf zugreifen kann. Alle Angaben sind gegen den Code geprüft (Stand Oktober 2026,
Release 1.0.0 einschließlich der Korrekturen vom 15.10.2026). Das Dokument ist eine
technische Zuarbeit für das Verzeichnis von Verarbeitungstätigkeiten und die TOM-Dokumentation
der Filmakademie; es ersetzt keine rechtliche Bewertung.

> **Wichtigster offener Punkt:** Im Repository liegen **echte Drehbücher und Projektmappen**
> (`tests/fixtures/pdf/`), und sie werden in **beide Docker-Images** kopiert – also auch in
> die GHCR-Images. Siehe [§8](#8-offene-entscheidungen-für-die-filmakademie), Punkt 1.

---

## 1. Grundsatz und Abweichungen davon

Die eKI arbeitet nach dem Processing-Only-Prinzip (Pflichtenheft §4.1/§4.2): Drehbuch und
Report liegen nur transient und verschlüsselt im Redis-Buffer und werden nach Zustellung
gelöscht. Dauerhaft gespeichert werden Metadaten.

Davon gibt es drei Abweichungen, die bei der datenschutzrechtlichen Bewertung zu
berücksichtigen sind:

1. **Temporal-Historie:** Drehbuchtext und Findings laufen seit Oktober 2026 nur als
   Buffer-Referenz durch den Workflow. Unverschlüsselt in der Workflow-Historie in PostgreSQL
   stehen aber Metadaten (`project_id`, `user_id`, `script_id`, Client-`metadata`) und bei PDFs
   der **Titel des Drehbuchs** (§3.3).
2. **Knowledge Base:** Die Chunk-Texte der KB-Dokumente liegen im Klartext in
   `kb_embeddings.chunk_text`; nur der Originaltext ist verschlüsselt (§3.2).
3. **Metadaten** (Jobs, Reports, Dead Letters, API-Keys) werden nie automatisch gelöscht (§4).

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

### 3.2 PostgreSQL – Anwendungsdatenbank (`eki_db`)

Alle Tabellen aus `core/db_models.py`. Die Datenbank selbst ist **nicht** von der eKI
verschlüsselt; Verschlüsselung des Volumes `postgres_data` ist Sache des Hosts.

| Tabelle | Inhalt | Klartext / verschlüsselt | Automatische Löschung |
|---|---|---|---|
| `api_keys` | `user_id`, `organization_id`, `name`, `description`, `key_hash` (SHA-256), `is_active`, `is_admin`, `created_at`, `expires_at`, `last_used_at`, `usage_count` | Klartext; der Key selbst nur als Hash | nie (Deaktivierung per `is_active=false`, Ablauf per `expires_at`, Standard 365 Tage) |
| `job_metadata` | `job_id`, `project_id`, `user_id`, `script_format`, `status`, `priority`, `progress_percentage`, `report_id`, `error_message` (max. 1000 Zeichen, inhaltsfrei), `idempotency_key`, `delivery_mode`, `script_id`, Zustell-Buchhaltung (`delivery_*`, `delivered_at`), Zeitstempel. `extra_metadata` und `workflow_id` werden nicht befüllt. | Klartext, keine Drehbuchinhalte | nie |
| `report_metadata` | `report_id`, `job_id`, `project_id`, `user_id`, `script_format`, `total_findings`, `processing_time_seconds`, `report_ref_key` (Redis-Schlüssel), `is_retrieved`, `retrieved_at`, `delivery_mode` | Klartext, keine Findings | nie |
| `delivery_dead_letters` | `job_id`, `report_id`, `project_id`, `user_id`, `delivery_mode`, `reason`, `attempts`, `last_status_code`, `last_error_type` (derzeit nie befüllt), `webhook_sent`, `acknowledged_at`, `acknowledged_by` (Admin-`user_id`), `note` (Freitext des Operators, max. 1000 Zeichen) | Klartext; inhaltsfrei (geprüft: keine Report- oder Drehbuchfelder) | nie |
| `audit_logs` | Tabelle angelegt (Endpoint, Methode, `user_id`, `project_id`, `request_id`, Status …), wird aber **nirgends beschrieben** | – | – (immer leer) |
| `kb_documents` | `title`, `source`, `tenant_id`, `tags`, `uploaded_by` (`user_id`), `content_hash` (SHA-256 des Texts), `ttl_hours`, `created_at`, `expires_at`; `original_text_encrypted` | Metadaten Klartext; **Originaltext Fernet-verschlüsselt** (Schlüssel = SHA-256(`API_SECRET_KEY` + `eki-kb-doc-v1`)) | ja, nach `expires_at` (siehe §4) |
| `kb_embeddings` | `chunk_text`, `vector` (1024 dim), `chunk_offset`, `length`, `hash` | **`chunk_text` im Klartext** (pgvector kann nicht auf Chiffraten suchen); die Chunks ergeben zusammen den vollständigen Dokumenttext | mit dem Dokument (`ON DELETE CASCADE`) |
| `alembic_version` | Migrationsstand | – | – |

Hinweis zu `API_SECRET_KEY`: Wird der Schlüssel gewechselt, sind Buffer-Inhalte und die
verschlüsselten KB-Originaltexte nicht mehr lesbar. Die KB-Suche arbeitet mit `chunk_text`
und Vektoren und funktioniert weiter; für einen konsistenten Stand die KB neu einspielen
(`scripts/seed_kb.py --reseed`).

### 3.3 PostgreSQL – Temporal (`temporal`, `temporal_visibility`)

Temporal (`temporalio/auto-setup`) legt seine Datenbanken im selben PostgreSQL-Server an.
Die Workflow-Historie enthält Eingaben und Ergebnisse jeder Activity. Geprüft
(`workflows/security_check.py`, `workflows/activities.py`):

| In der Historie | Inhalt |
|---|---|
| Workflow-Eingabe `job_data` | Buffer-Referenzschlüssel (kein Drehbuchtext), `job_id`, `report_id`, `project_id`, `user_id`, `script_id`, `delivery_mode`, Client-`metadata`, Konfigurationswerte |
| Ergebnisse von `parse_fdx`, `extract_pdf_text`, `split_scenes`, `aggregate_script` | Referenzschlüssel, Zähler, OCR-Seitennummern, Warnungen – kein Text |
| Ergebnis von `structure_scene_llm` für den Vorspann (PDF), Eingabe von `aggregate_script` | **Titel des Drehbuchs** |
| Ergebnisse von `analyze_scene_risk`, Eingabe von `aggregate_report` | Szenennummer und Referenzschlüssel auf die verschlüsselten Findings im Buffer – keine Findings im Klartext |
| Ergebnis von `deliver_report` (Push) | Statuscode und Antwort-Body von ePro |
| Fehlerpfade | inhaltsfreie Meldungen (`delivery_failed:<reason>`, „Parsing fehlgeschlagen: …“, „Szene 7: Risikoanalyse fehlgeschlagen (LLMException)“); Webhook-/Dead-Letter-Payload ist inhaltsfrei |

Bis Oktober 2026 liefen die Findings (einschließlich wörtlicher Zitate im Feld `evidence`)
im Klartext durch die Historie. Workflows, die vor dem Update gestartet wurden, enthalten sie
bis zum Ablauf ihrer Retention noch.

Ein Payload-Codec (Verschlüsselung der Temporal-Payloads) ist **nicht** konfiguriert.
Aufbewahrung: Laufzeit des Workflows (bis zur Zustellung bzw. bis Ablauf der Pull-TTL,
höchstens `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` = 10 h) **plus** die Retention des Namespaces.
Der Compose setzt keine eigene Retention; das Image `temporalio/auto-setup` legt den
Namespace `default` nach unserem Kenntnisstand mit 24 h an (`DEFAULT_NAMESPACE_RETENTION`).
Vor dem Go-Live prüfen:

```bash
docker compose exec temporal tctl --address temporal:7233 --namespace default namespace describe
```

Die Temporal-UI zeigt diese Daten an. Sie ist in Produktion nicht veröffentlicht
(`docker-compose.prod.yml`), im Entwicklungs-Compose aber ohne Anmeldung auf Port 8080.

Nachweis: `tests/test_e2e_pipeline.py` prüft die Historie eines echten Workflow-Laufs auf
Drehbuchtext.

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
- **Aufbewahrung:** Die Logs gehen an den Docker-Logging-Treiber. Die Compose-Dateien
  konfigurieren **keine Rotation**; ohne Einstellung im Docker-Daemon wachsen die Logs bis zum
  Entfernen des Containers. Rotation/Weiterleitung legt der Betrieb fest (§8).
- **Ollama** protokolliert im Normalbetrieb keine Prompt-Inhalte. Den Debug-Modus
  (`OLLAMA_DEBUG`) im Betrieb nicht aktivieren.

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
- `config/kb_seed/real/`: lokale Ablage echter KB-Dokumente vor dem Einspielen. Der Inhalt ist
  git-ignoriert, aber die Dockerfiles kopieren `config/` in die Images (es gibt keine
  `.dockerignore`): Bei einem lokalen Image-Build landen dort liegende Dokumente im Image.
  Nach dem Einspielen entfernen.
- Backups: Ein `pg_dump` von `eki_db` enthält Metadaten und die KB (Chunk-Texte im Klartext).
  Wird der ganze PostgreSQL-Server gesichert, sind auch die Temporal-Datenbanken (Metadaten,
  Drehbuchtitel) enthalten.

---

## 4. Löschfristen im Überblick

| Daten | Ablage | Wird automatisch gelöscht | Durch |
|---|---|---|---|
| Drehbuch, Zwischenergebnisse | Redis | ja, nach dem jeweiligen Verarbeitungsschritt, spätestens nach `BUFFER_TTL_SECONDS` (6 h) | Activities, Redis-TTL |
| Report-Paket (JSON + PDF) | Redis | ja: Push nach 2xx, Pull beim One-Shot-GET, bei endgültigem Fehler sofort, spätestens nach 6 h | `deliver_report`, `GET /v1/security/reports/{id}`, `cleanup_buffer`, Redis-TTL |
| Drehbuchtitel (PDF), Client-`metadata`, IDs | Temporal-Historie | ja, nach Workflow-Ende + Namespace-Retention (§3.3) | Temporal |
| KB-Dokumente inkl. Chunks | `kb_documents`, `kb_embeddings` | ja, nach `expires_at` (`ttl_hours`, Standard 720 h = 30 Tage, max. 8760 h) | Temporal-Schedule `eki-kb-cleanup`, täglich 03:00 UTC (`KB_CLEANUP_CRON`), angelegt beim Worker-Start; zusätzlich manuell per `DELETE /v1/kb/documents…` |
| Rate-Limit-Zähler (IP) | Redis | ja, 60 s bzw. 1 h | Redis-TTL |
| Metriken | Prometheus | ja, 30 Tage | Prometheus |
| Job-/Report-Metadaten | `job_metadata`, `report_metadata` | **nein** | – |
| Dead Letters | `delivery_dead_letters` | **nein** (auch nicht nach Quittierung) | – |
| API-Keys (auch abgelaufene/deaktivierte) | `api_keys` | **nein** | – |
| Container-Logs | Docker | **nein** (keine Rotation konfiguriert) | Betrieb |

Eine Löschfrist für die Metadaten muss die Filmakademie festlegen (§8). Bis eine Funktion
dafür existiert, wäre eine manuelle Bereinigung möglich, zum Beispiel (Vorschlag, nicht
Teil des Release, vorher sichern und mit dem Datenschutz abstimmen):

```sql
-- in: docker compose exec postgres psql -U eki_user -d eki_db
DELETE FROM report_metadata       WHERE created_at < now() - interval '90 days';
DELETE FROM job_metadata          WHERE created_at < now() - interval '90 days'
                                    AND status IN ('completed', 'failed');
DELETE FROM delivery_dead_letters WHERE acknowledged_at < now() - interval '90 days';
```

---

## 5. LLM-Verarbeitung

| Provider | Wo läuft die Inferenz | Zulässig |
|---|---|---|
| `local_mistral` (Standard in Produktion, Modell `mistral-small3.2`) | lokal im Container `eki-ollama` | Produktion |
| `ollama` (beliebiges lokales Modell) | lokal im Container `eki-ollama` | Produktion möglich (Paritätsreport beachten) |
| `mistral_cloud` | **Mistral AI (Cloud-API `api.mistral.ai`)** – Drehbuchszenen verlassen das Netz der Filmakademie | nur Stage/Test |

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
  freigegebene Testdrehbücher verwenden.
- Embeddings für die KB entstehen immer lokal über Ollama; mit `mistral_cloud` ist die KB nicht
  nutzbar (`M06_KB_GUIDE.md`).

---

## 6. Zugriffskontrolle

| Zugang | Wer | Sieht / darf |
|---|---|---|
| `POST /v1/security/check:async`, `GET /v1/security/jobs/{id}`, `GET /v1/security/reports/{id}` | jeder gültige API-Key | nur **eigene** Jobs und Reports: Abfragen sind auf die `user_id` des Keys gefiltert, fremde IDs liefern 404 (IDOR-Schutz); Idempotenz-Keys gelten pro `user_id` |
| `X-Actor-User-Id` | optionaler Header | muss zur `user_id` des Keys passen, sonst 403; wird nicht gespeichert |
| `X-Actor-Project-Id` | optionaler Header | nur informativ (erscheint lediglich in der Stub-Antwort des synchronen Endpoints), nicht gespeichert |
| `/v1/kb/*` | **jeder** gültige API-Key | alle KB-Dokumente des (einzigen) Mandanten auflisten, hochladen und löschen; der Originaltext wird nie ausgeliefert |
| `/v1/ops/*` | nur Keys mit `is_admin=true` | Metadaten **aller** Jobs und Dead Letters (`project_id`, `user_id`, Fehlermeldung) – keine Inhalte |
| `/metrics`, `/ready` | jeder gültige API-Key | Metriken bzw. Dienststatus |
| `/health` | ohne Anmeldung | Version und Status |
| Swagger/ReDoc/`/openapi.json` | nur bei `DEBUG=true` | – (in Produktion abgeschaltet; der Validator erzwingt `DEBUG=false`) |
| PostgreSQL, Redis, Temporal, Ollama | nur im internen Docker-Netz | wer Shell-Zugriff auf den Host hat, sieht Metadaten, KB-Chunks und Temporal-Historien (Titel, Client-`metadata`); mit zusätzlich `API_SECRET_KEY` auch die Buffer-Inhalte |
| Temporal-UI | Produktion: nur per SSH-Tunnel | alle Workflow-Eingaben und -Ergebnisse (Metadaten, Drehbuchtitel, Fehlermeldungen) |

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

---

## 7. Löschung auf Anfrage

- **Ein einzelner Job:** Inhalte sind spätestens nach 6 h aus Redis verschwunden. Titel und
  Metadaten in der Temporal-Historie verschwinden mit der Namespace-Retention; die Metadatenzeilen
  (`job_metadata`, `report_metadata`, ggf. `delivery_dead_letters`) per SQL nach `job_id`
  löschen.
- **Ein Nutzer/API-Key:** Key deaktivieren (`UPDATE api_keys SET is_active=false …`), die
  Metadaten nach `user_id` löschen.
- **KB-Dokument:** `DELETE /v1/kb/documents/{doc_id}` löscht Dokument und Chunks.
- Backups und Redis-AOF enthalten gelöschte Daten bis zu ihrem eigenen Ablauf.

---

## 8. Offene Entscheidungen für die Filmakademie

1. **Echte Drehbücher im Repository und in den Images (dringend).** `tests/fixtures/pdf/`
   enthält 15 echte Drehbücher, Treatments, Projektmappen und eine Production Bible von
   Filmakademie-Produktionen (u. a. TRANCE, Sinkende Schiffe, H2Gold, Babystar, Matroshki,
   Königskinder, Gaïa). Sie sind seit März 2026 in der Git-Historie und werden über
   `COPY tests/fixtures ./tests/fixtures` in **beide** Images (`eki-api`, `eki-worker`)
   kopiert, also auch in die GHCR-Images. Projektmappen können Kontaktdaten realer Personen
   enthalten. Zu klären: Rechte und Einwilligung der Urheber und Beteiligten, Sichtbarkeit
   des Repositorys und der GHCR-Pakete. Empfehlung unabhängig davon: die Dateien aus den
   Images entfernen (zur Laufzeit wird nichts aus `tests/fixtures/` benötigt; nur
   `scripts/smoke_prod.py` nutzt synthetische FDX-Dateien und läuft auf dem Host) und – falls
   keine Freigabe vorliegt – aus Repository, Git-Historie und bereits veröffentlichten Images
   entfernen. Die übrigen PDF- und alle FDX-Fixtures sind synthetisch
   (`tests/generate_pdf_fixtures.py`, `tests/build_large_fixture.py`).
2. **Löschfrist für Metadaten** (`job_metadata`, `report_metadata`, `delivery_dead_letters`,
   deaktivierte `api_keys`) festlegen; bis zu einer automatischen Lösung manuell (§4).
3. **Temporal-Historie:** Retention des Namespaces festlegen und prüfen (§3.3); entscheiden,
   ob die verbliebenen Klartext-Metadaten (Titel, Client-`metadata`) zusätzlich verschlüsselt
   werden sollen (Temporal-Payload-Codec, Code-Änderung).
4. **AVV mit Mistral AI** für den Stage-Sign-off, oder nur freigegebene Testdrehbücher (§5).
5. **Log-Aufbewahrung:** Rotation und Aufbewahrungsdauer der Container-Logs (enthalten
   IP-Adressen) im Docker-Daemon oder per Log-Weiterleitung festlegen.
6. **KB-Zugriff:** Soll das Hochladen und Löschen von KB-Dokumenten auf Admin-Keys beschränkt
   werden? Derzeit genügt jeder gültige Key, also auch der ePro-Service-Key (Code-Änderung).
7. **Verschlüsselung der Volumes** (`postgres_data`, `redis_data`) auf Host-Ebene und
   Aufbewahrung der Backups.
8. **Konvention für `user_id`** (pseudonym, §6).
