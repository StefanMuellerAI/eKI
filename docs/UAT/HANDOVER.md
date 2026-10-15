# Übergabedokument – eKI API v1.0.0

Pflichtenheft §9 M12 „UAT-Paket (Testskripte, Protokolle, OpenAPI v1.0, Betriebsdoku),
Go-Live-Checkliste, Schulungsunterlagen" und §12 „Rechte & Eigentum".

## 1. Lieferumfang (Pflichtenheft §3.2 v1.0) und Ablageort

| Position | Stand | Ablage |
|---|---|---|
| Sicherheitsprüfung: Parser (FDX, PDF + OCR), Regelwerk, Scoring, Report (JSON/PDF) | fertig | `parsers/`, `services/taxonomy.py`, `services/report_generator.py`, `config/taxonomy/` |
| Outbound-Adapter (Push/Pull, Idempotenz, Retries, Dead Letters) | fertig | `workflows/`, `api/routers/security.py`, `api/routers/ops.py` |
| Write-Through-Löschlogik | fertig | `services/secure_buffer.py`, `deliver_report_activity`, `cleanup_buffer_activity` |
| LLM-Adapter Mistral API und lokales Mistral | fertig | `llm/mistral_cloud.py`, `llm/local_mistral.py`, `llm/factory.py`, `docs/LLM_INTEGRATION.md` |
| KB-Grundlage (Dokumentenaufnahme, Embeddings) | fertig | `services/knowledge_base.py`, `api/routers/knowledge_base.py`, `scripts/seed_kb.py`, `docs/M06_KB_GUIDE.md` |
| OpenAPI-Doku | v1.0.0 | `openapi/eki-api-v1.0.yaml` (verbindlich); Swagger/ReDoc nur bei `DEBUG=true` |
| Container-Setup | fertig | `docker/`, `docker-compose*.yml`, GHCR-Images bei `v*`-Tags; Abhängigkeiten gepinnt in `requirements.lock` / `requirements-dev.lock` |
| Monitoring-Dashboards | fertig | `docker/observability/`, `docs/M09_SLO.md` |
| Betriebsdoku | fertig | `docs/OPERATIONS_GUIDE.md` (Installation, Updates, Backups, Runbooks, Troubleshooting), `docs/CONFIGURATION.md` (alle Einstellungen), `docs/M10_FAILOVER_RUNBOOK.md` |
| Datenschutz-Inventar | fertig | `docs/DATENSCHUTZ.md` |
| Tests und Testanleitung | fertig | `tests/`, `docs/TESTING_GUIDE.md` |
| Schnittstellenbeschreibung für ePro | fertig | `docs/EPRO_INTEGRATION_GUIDE.md` |
| Postman-Collection | v1.0 | `postman/` |
| UAT-Paket | fertig | `docs/UAT/`, `scripts/uat/`, `scripts/smoke_prod.py`, `scripts/run_parity.py` |
| Änderungsprotokoll | fertig | `CHANGELOG.md` |

### 1.1 Stand des Releases

Nach dem M12-Abschluss wurden am 15.10.2026 Fehler behoben, die nur auf einem echten
PostgreSQL-Stack bzw. bei einer frischen Installation auftraten (Details: `CHANGELOG.md`):

- frische Installation: fehlendes `greenlet`, `alembic upgrade head` ohne psycopg2-Treiberangabe;
- `job_metadata.status` war `VARCHAR(9)`, der Status `delivering` passte nicht hinein – auf
  PostgreSQL scheiterte damit die Zustellphase jedes asynchronen Jobs. Neue Migration
  `a7c9e1f3b5d2`;
- KB-Dubletten werden pro Mandant statt global erkannt (Migration `b8d0f2a4c6e3`, aktueller
  Head; Kette: `docs/OPERATIONS_GUIDE.md`, Abschnitt „Datenbank-Migrationen“). Vorher führte
  derselbe Text bei einem zweiten Mandanten zu HTTP 503, ein gleichzeitiger Doppel-Upload jetzt
  zu 409 statt 503;
- Produktions-Overlay band trotz `docker-compose.prod.yml` den Quellcode vom Host ein und
  veröffentlichte die Temporal-UI (jetzt `!reset`, benötigt Docker Compose ≥ 2.24);
- Produktions-Validator akzeptierte die Platzhalter-Secrets aus `.env.example`;
- `TEMPORAL_NAMESPACE` wurde von der API ignoriert; Ablaufzeiten von API-Keys auf Hosts
  außerhalb von UTC; `request_id` fehlte in Fehlerantworten;
- Verarbeitung jetzt fail-closed (siehe §5); OCR mit `OMP_THREAD_LIMIT=1` in den Images
  (Scan-Seiten liefen unter Last in den Timeout und wurden übersprungen);
- Eingabeprüfung gehärtet (keine 5xx mehr durch Client-Eingaben, Multipart wie JSON
  validiert, `delivery` nur `pull`/`push`), abgelaufene Reports liefern 410 statt eines leeren
  Platzhalter-Reports, Prompt-Injection-Filter ohne die bekannten Fehlalarme;
- Findings laufen nur noch als verschlüsselte Buffer-Referenz durch Temporal, lange Szenen
  werden in überlappenden Abschnitten analysiert statt gekürzt.

Die Versionsnummer bleibt 1.0.0. GHCR-Images, die vor diesen Korrekturen aus dem Tag
`v1.0.0` gebaut wurden, enthalten sie **nicht**. Für den Go-Live wird ein fixierter
Patch-Release empfohlen (neues Tag, z. B. `v1.0.1`; das Tag muss zu `core/version.py`
passen, sonst bricht der CI-Release-Job ab). Die Abnahmetests 2–7 sind auf diesem Stand
erneut auszuführen (`UAT_TESTPLAN.md`).

## 2. Meilenstein-Nachweise

Die Nachweise M01 und M06–M12 sind historische Dokumente mit dem Stand ihres Meilensteins
(Hinweisbanner am Dokumentanfang); maßgeblich für den Betrieb sind die in §1 genannten
lebenden Dokumente.

| Meilenstein | Evidence-Dokument |
|---|---|
| M01 | `docs/M01_COMPLETION_REPORT.md` |
| M02–M05 | README-Abschnitte; Tests und Testaufbau: `docs/TESTING_GUIDE.md` |
| M06 | `docs/M06_ACCEPTANCE_EVIDENCE.md`, `M06_KB_GUIDE.md` |
| M07 | `docs/M07_ACCEPTANCE_EVIDENCE.md` |
| M08 | `docs/M08_ACCEPTANCE_EVIDENCE.md` |
| M09 | `docs/M09_ACCEPTANCE_EVIDENCE.md`, `M09_SLO.md` |
| M10 | `docs/M10_ACCEPTANCE_EVIDENCE.md`, `M10_FAILOVER_RUNBOOK.md` |
| M11 | `docs/M11_ACCEPTANCE_EVIDENCE.md`, `M11_PARITY_REPORT.md`, `OPERATIONS_GUIDE.md` |
| M12 | dieses Verzeichnis, `docs/M12_ACCEPTANCE_EVIDENCE.md` |

Veraltet und nur noch zur Nachvollziehbarkeit im Repository (mit Hinweisbanner):
`DEPLOYMENT_GUIDE.md`, `OLLAMA_INTEGRATION.md`, `OLLAMA_SETUP.md`, `QUICKSTART_OLLAMA.md`,
`SECURITY_AUDIT_REPORT.md`, `SECURITY_AUDIT_SUMMARY.md`, `SECURITY_IMPLEMENTATION_COMPLETE.md`,
`M06_PROD_DEPLOY.md`.

## 3. Zugänge und Geheimnisse (zu übergeben, nicht im Repo)

| Gegenstand | Übergabe an | Form |
|---|---|---|
| GitHub-Repository (Eigentum nach §12) | Filmakademie IT | Owner-Rechte / Transfer |
| GHCR-Images | Filmakademie IT | Pull-Rechte, Digest-Liste; Sichtbarkeit der Pakete prüfen (siehe §5, echte Drehbücher in den Images) |
| API-Keys (ePro-Service, Ops-Admin, Prometheus) | ePro-Team / IT | Passwort-Manager, einmalig |
| `.env.local` der Produktion (u. a. `API_SECRET_KEY`, DB-Passwort) | IT | verschlüsselt, keine Kopie beim Auftragnehmer. `API_SECRET_KEY` ist zugleich der Schlüssel für Buffer und KB-Originaltexte – Verlust oder Wechsel macht diese unlesbar |
| Compose-Variablen `GRAFANA_ADMIN_USER`/`GRAFANA_ADMIN_PASSWORD`, `EKI_IMAGE_TAG`, `GHCR_OWNER`, `LLM_PROVIDER` (Prod-Overlay) | IT | werden von Docker Compose aus der Shell bzw. einer `.env` im Projektverzeichnis gelesen, **nicht** aus `.env.local` |
| Grafana-Admin | IT | Passwort-Manager |

## 4. Abhängigkeiten von Dritten (Pflichtenheft §8)

| Abhängigkeit | Status | Verantwortlich |
|---|---|---|
| Stage-ePro mit Testfilmprojekten (AT1, AT8) | offen / terminiert: ____ | ePro-Team |
| Produktiv-Hardware GPU ≥ 24 GB (AT6, AT9) | offen / terminiert: ____ | IT |
| Fachinhalte für die KB | offen / laufend | Sicherheitsbeauftragter |
| AVV mit Mistral AI, falls AT8 mit echten Drehbüchern (`DATENSCHUTZ.md` §5) | offen / entfällt: ____ | Filmakademie |
| Supportvertrag (§14) | offen / geschlossen: ____ | Filmakademie / StefanAI |

## 5. Bekannte Einschränkungen

Verhaltensänderung seit M12 (für ePro relevant):

- **Verarbeitung fail-closed:** Früher wurde ein LLM-Fehler bei der Analyse einer Szene
  (Ollama nicht erreichbar, Timeout, Prompt vom Prompt-Injection-Filter blockiert)
  verschluckt; die Szene erhielt still null Findings und der Report sah risikofrei aus
  (ePro-Status 0). Jetzt werden vorübergehende LLM-Fehler wiederholt; bleiben sie – oder ein
  Parsing-Fehler – bestehen, endet der Job mit `failed` und inhaltsfreier `error_message`
  (z. B. „Szene 7: Risikoanalyse fehlgeschlagen (LLMException)"). Eine Blockade durch den
  Filter beendet den Job sofort mit „Szene <nr>: vom Prompt-Injection-Filter blockiert –
  manuelle Prüfung erforderlich". Zwischengespeicherte Inhalte werden dabei sofort gelöscht;
  bei konfiguriertem `EPRO_WEBHOOK_URL` geht der Webhook `security.delivery.failed` mit
  `reason` `prompt_blocked`, `parsing_failed` oder `processing_failed` an ePro. Für ePro gilt:
  `failed` heißt „keine automatische Bewertung – manuelle Prüfung", nie „kein Risiko"
  (`EPRO_INTEGRATION_GUIDE.md` §3).
- **Prompt-Injection-Filter:** prüft gezielte, englischsprachige Muster, die sich an das
  Modell richten (Anweisungs-Overrides, Rollenwechsel zu einer KI, Chat-Template-Token,
  eingeschleuste Szenen-Begrenzer). Normale Drehbuchsprache löst ihn in der Regel nicht aus;
  anderssprachige oder umschriebene Injektionen erkennt er nicht (`LLM_INTEGRATION.md`).
- **Abgebrochene Jobs** (Workflow-Cancel durch den Betrieb) erhalten den Status `cancelled`.

API und Verhalten:

- Der synchrone Endpoint `POST /v1/security/check` liefert eine Stub-Antwort (M01); die
  produktive Analyse läuft asynchron (Pflichtenheft §4.1 nennt `check(:async)`; ePro nutzt async).
- `callback_url` wird validiert, aber nicht verwendet; Push-Ziel ist immer
  `EPRO_BASE_URL/eki/scl/set-risk-assessment/{project_id}`.
- `priority` wird gespeichert, steuert die Abarbeitung aber nicht. Eine Abbruchfunktion in
  der API gibt es nicht (Abbruch nur über Temporal).
- Ein One-Shot-GET während laufender Push-Retries beendet die Zustellung als Fehler: Der
  nächste Push-Versuch findet den Report nicht mehr (Dead Letter `report_expired`).
- Neue Dead-Letter-Gründe `report_expired` und `invalid_project_id`; der Webhook meldet
  zusätzlich Verarbeitungsfehler (`prompt_blocked`, `parsing_failed`, `processing_failed`,
  ohne Dead Letter). Beides ist in der OpenAPI-Spec beschrieben.
- Die OpenAPI-Spec unter `/openapi.json` (nur `DEBUG=true`) wird von FastAPI generiert;
  verbindlich ist `openapi/eki-api-v1.0.yaml` (Contract-Test sichert Routen/Enums ab).

Daten und Datenschutz (Details: `docs/DATENSCHUTZ.md`):

- **Echte Drehbücher in `tests/fixtures/pdf/`** (15 Dateien realer Filmakademie-Produktionen)
  sind seit März 2026 im Git-Repository und werden in **beide** Docker-Images kopiert, also
  auch in die GHCR-Images. Offene Entscheidung der Filmakademie: Rechte/Einwilligung klären;
  empfohlen wird, die Dateien mindestens aus den Images zu entfernen.
- Metadaten (`job_metadata`, `report_metadata`, `delivery_dead_letters`, `api_keys`) werden
  nie automatisch gelöscht; eine Löschfrist ist festzulegen. Die Tabelle `audit_logs`
  existiert, wird aber nicht beschrieben.
- Die Temporal-Workflow-Historie enthält unverschlüsselt Metadaten (`project_id`, `user_id`,
  `script_id`, Client-`metadata`) und bei PDFs den Drehbuchtitel; sie bleiben dort bis zum
  Ablauf der Namespace-Retention. Drehbuchtext und Findings laufen nur als Buffer-Referenz.
- KB-Chunk-Texte liegen im Klartext in `kb_embeddings`; nur der Originaltext ist verschlüsselt.
  Jeder gültige API-Key darf KB-Dokumente hochladen und löschen.

LLM und Analyse:

- Mit `LLM_PROVIDER=mistral_cloud` ist die Knowledge Base nicht nutzbar (der Provider hat
  kein `embed()`): Upload schlägt fehl, Retrieval liefert keinen Kontext.
- Paritäts- und Großdokument-Messwerte liegen erst nach dem Lauf auf der Zielhardware vor
  (`M11_PARITY_REPORT.md` §3, `M07_ACCEPTANCE_EVIDENCE.md` §6); die Werkzeuge sind vollständig.
- OCR ist auf `OCR_MAX_PAGES` (Default 50) begrenzt; darüber werden Scan-Seiten mit Warnung übersprungen.
- Dead Letters lassen sich nicht aus der eKI heraus erneut zustellen (Inhalt ist gelöscht);
  Recovery = erneuter Upload im ePro. Bewusste Konsequenz des Processing-Only-Prinzips.

Technik und Wartung:

- Ungenutzte Abhängigkeiten in `pyproject.toml`: `python-jose` (mit bekannten CVEs), `PyJWT`,
  `passlib`, `bcrypt`, `aiohttp`, `python-json-logger`, `email-validator`, `pytz`,
  `python-dateutil`, `opentelemetry-exporter-prometheus`. Empfehlung: entfernen und die
  Lock-Dateien neu erzeugen. `requests` wird von Skripten genutzt, ist aber nicht direkt
  deklariert (kommt derzeit transitiv mit).
- `mypy --strict` meldet 77 Befunde (in CI nur informativ).
- Tech-Stack-Abweichungen zum Pflichtenheft §4.1, bewusst und dokumentiert: Temporal statt
  Celery/Flower (durable Timer für 6-h-Fenster), kein PgBouncer (Async-Pool von SQLAlchemy genügt
  bei der erwarteten Last).

## 6. Rechte & Eigentum (Pflichtenheft §12)

Mit vollständiger Vergütung gehen Eigentums- und Nutzungsrechte am Quellcode, den
Konfigurationen und der Dokumentation an die Filmakademie Baden-Württemberg über.
Drittkomponenten verbleiben unter ihren eigenen Lizenzen. Ausnahme vom Rechteübergang
sind außerdem die echten Drehbücher in `tests/fixtures/pdf/` (Rechte bei den Urhebern, §5).

### 6.1 Python-Laufzeitabhängigkeiten

Ermittelt aus den Paket-Metadaten (`License-Expression`, `License`, Classifier) aller
99 in `requirements.lock` gepinnten Pakete (Python 3.11, Stand 15.10.2026). Die große
Mehrheit steht unter permissiven Lizenzen (MIT, BSD-2/3-Clause, Apache-2.0, ISC, PSF-2.0,
MIT-0, MIT-CMU, Unlicense). **Nicht permissiv bzw. mit Copyleft-Anteil:**

| Paket | Lizenz | Hinweis |
|---|---|---|
| `psycopg2-binary` 2.9.13 | LGPL-3.0 (mit Ausnahmen) | dynamisch gelinkte Bibliothek, unverändert genutzt; das Binär-Wheel bündelt zusätzlich `libpq` (PostgreSQL-Lizenz), OpenSSL, Kerberos, OpenLDAP |
| `certifi` 2026.7.22 | MPL-2.0 | Datei-basiertes Copyleft, nur bei Änderung des Pakets relevant |

Zu beachten sind außerdem Binär-Wheels, die native Bibliotheken mit eigenen Lizenzen
mitbringen: `pypdfium2` (BSD-3-Clause/Apache-2.0; bündelt PDFium mit u. a. FreeType, ICU,
libjpeg-turbo, OpenJPEG, Little CMS, zlib), `pillow` (MIT-CMU; bündelt libjpeg, libpng,
libtiff, libwebp, FreeType, HarfBuzz u. a.) und `cryptography` (Apache-2.0 oder BSD-3-Clause;
bündelt OpenSSL). Die Lizenztexte liegen in den jeweiligen `*.dist-info`-Verzeichnissen der
Installation. Nachprüfen:

```bash
python -c "import importlib.metadata as m; d=m.distribution('psycopg2-binary'); print(d.metadata.get('License-Expression') or d.metadata.get('License'))"
```

### 6.2 Container-Images und Systemkomponenten

| Komponente | Lizenz | Hinweis |
|---|---|---|
| Tesseract OCR + Sprachdaten (Debian-Pakete im eKI-Image) | Apache-2.0 | |
| Debian-Basis (`python:3.11-slim`) | gemischt, u. a. GPL-Systempakete | übliche Distributionspakete, bei Weitergabe der Images Lizenzpflichten der Distribution beachten |
| PostgreSQL + pgvector (`pgvector/pgvector:pg16`) | PostgreSQL License | |
| Redis (`redis:7-alpine`) | Redis 7.4+: RSALv2 / SSPLv1 (source-available, keine OSI-Lizenz); bis 7.2: BSD-3-Clause | Das Tag `7-alpine` löst auf die aktuelle 7.x-Version auf. Interne Nutzung ist erlaubt, ein Angebot von Redis als Dienst für Dritte nicht. Alternativen: auf eine 7.2-Version pinnen oder Valkey (BSD-3-Clause) |
| Temporal Server und UI (`temporalio/auto-setup`, `temporalio/ui`) | MIT | |
| Ollama (`ollama/ollama`) | MIT | |
| Prometheus, Alertmanager, Jaeger (Observability-Overlay) | Apache-2.0 | |
| Grafana (`grafana/grafana:11.1.0`, Observability-Overlay) | AGPL-3.0 | unverändert und intern genutzt unproblematisch; Änderungen am Grafana-Code wären offenzulegen |

### 6.3 Modelle

| Modell | Verwendung | Lizenz |
|---|---|---|
| `mistral-small3.2` (Mistral Small 3.2) | Produktiv-Inferenz (`local_mistral`) | Apache-2.0 laut Modellkarte |
| `mxbai-embed-large` (mixedbread.ai) | KB-Embeddings (Standard) | Apache-2.0 laut Modellkarte |
| `bge-m3` (BAAI) | optionale KB-Embeddings | MIT laut Modellkarte |
| `gemma4` | dokumentierte Alternative (`LLM_PROVIDER=ollama`) | Lizenz des jeweiligen Modells prüfen |
| Mistral Cloud (`mistral-large-latest`) | nur Stage | kommerzielle Nutzungsbedingungen von Mistral AI |

Die Angaben zu den Modellen sind vor dem Go-Live am konkret gezogenen Modell zu bestätigen,
z. B. `docker compose exec ollama ollama show mistral-small3.2 --license`.

## 7. Kontakt nach Übergabe

StefanAI – Research & Development, info@stefanai.de, +49 177 5228242 (gemäß Supportvertrag).
