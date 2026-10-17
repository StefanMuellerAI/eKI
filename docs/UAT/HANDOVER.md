# Übergabedokument – eKI API v1.0.2

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
| OpenAPI-Doku | 1.0.1 | `openapi/eki-api-v1.0.yaml` (verbindlich); Swagger/ReDoc nur bei `DEBUG=true` |
| Container-Setup | fertig | `docker/`, `docker-compose*.yml` (Ollama extern; optionales Overlay `docker-compose.ollama.yml` für Entwicklung), `.dockerignore`, GHCR-Images bei `v*`-Tags; Abhängigkeiten gepinnt in `requirements.lock` / `requirements-dev.lock` |
| Monitoring-Dashboards | fertig | `docker/observability/`, `docs/M09_SLO.md` |
| Betriebsdoku | fertig | `docs/OPERATIONS_GUIDE.md` (Installation, Updates, Backups, Runbooks, Troubleshooting), `docs/CONFIGURATION.md` (alle Einstellungen), `docs/LASTSTEUERUNG.md` (Last, Parallelität, Zeitlimits, Ollama-Server), `docs/M10_FAILOVER_RUNBOOK.md` |
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
- KB-Dubletten werden pro Mandant statt global erkannt (Migration `b8d0f2a4c6e3`; Kette:
  `docs/OPERATIONS_GUIDE.md`, Abschnitt „Datenbank-Migrationen“). Vorher führte derselbe Text
  bei einem zweiten Mandanten zu HTTP 503, ein gleichzeitiger Doppel-Upload jetzt zu 409 statt 503;
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

Dazu kommen die Projektentscheidungen vom 16.10.2026, umgesetzt in **Release 1.0.1**
(Details: `CHANGELOG.md`, Upgrade-Schritte: `docs/OPERATIONS_GUIDE.md` §4.10 „Upgrade 1.0.0 → 1.0.1“):

- echte Drehbücher aus Repository und Images entfernt (synthetische Ersatz-Fixtures; Git-Historie
  und Images ≤ 1.0.0 enthalten sie weiterhin, §5);
- Metadaten werden nach 90 Tagen automatisch gelöscht (Schedule `eki-metadata-cleanup`,
  Migration `c4e6a8b0d2f1`, aktueller Head);
- Temporal-Payloads AES-256-GCM-verschlüsselt (Schlüssel aus `API_SECRET_KEY`);
- `/v1/kb/*` nur mit Admin-Key; synchroner Stub `POST /v1/security/check` entfernt (404);
  `callback_url` und `priority` wirken;
- Inferenz auf externer Ollama-Instanz (kein Ollama-Dienst mehr im Stack), Redis auf 7.2
  gepinnt, ungenutzte Abhängigkeiten entfernt, mypy (strict) als blockierendes CI-Gate.

Am 17.10.2026 folgte **1.0.2** (Details: `CHANGELOG.md`):

- Dokument-Limit mit Warteschlange (`MAX_CONCURRENT_DOCUMENTS`, Standard 1) und Höchstwartezeit, weil
  mehrere gleichzeitig verarbeitete Drehbücher die externe Ollama-Instanz so ausbremsten, dass keines
  fertig wurde; Heartbeats in den KI-Schritten. Ausführlich: `docs/LASTSTEUERUNG.md`;
- Drehbücher ohne erkennbare Szene enden mit `failed` statt mit einem leeren Report; nicht geprüfte
  Teile (Seiten über `MAX_PDF_PAGES`, Seiten ohne lesbaren Text, Titeltext) nennt der Report unter
  „Hinweise zur Vollständigkeit“ und in `report.metadata.analysis_complete`/`coverage_notes`;
- FDX ohne Szenennummern, Konfidenzwerte über 1 (Report-Abruf mit 500) und „AUßEN“ mit ß behoben;
- Faktencheck der gesamten Betriebs- und Schnittstellendoku gegen den Code.

Für den Go-Live ist **1.0.2** vorgesehen: GHCR-Images aus dem Tag `v1.0.0` enthalten weder die
Korrekturen noch die Entscheidungen (und noch echte Drehbücher), Images aus `v1.0.1` haben kein
Dokument-Limit. Das Release-Tag muss zu `core/version.py` passen, sonst bricht der CI-Release-Job ab.
Die Abnahmetests 2–7 sind auf diesem Stand erneut auszuführen (`UAT_TESTPLAN.md`).

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
| GHCR-Images | Filmakademie IT | Pull-Rechte, Digest-Liste; Sichtbarkeit der Pakete prüfen; Versionen ≤ 1.0.0 enthalten noch echte Drehbücher – löschen bzw. Zugriff beschränken (§5) |
| Externe Ollama-Instanz (URL, Version, Modelle) | Filmakademie IT | Betriebsdokumentation der Instanz; Version mit dem Overlay-Pin abgleichen (`OPERATIONS_GUIDE.md` §4.11) |
| API-Keys (ePro-Service, Ops-Admin, Prometheus) | ePro-Team / IT | Passwort-Manager, einmalig |
| `.env.local` der Produktion (u. a. `API_SECRET_KEY`, DB-Passwort) | IT | verschlüsselt, keine Kopie beim Auftragnehmer. `API_SECRET_KEY` ist zugleich der Schlüssel für Buffer, KB-Originaltexte und (seit 1.0.1) die Temporal-Payloads – Verlust oder Wechsel macht diese unlesbar |
| Compose-Variablen `GRAFANA_ADMIN_USER`/`GRAFANA_ADMIN_PASSWORD`, `EKI_IMAGE_TAG`, `GHCR_OWNER`, `LLM_PROVIDER` (Prod-Overlay) | IT | werden von Docker Compose aus der Shell bzw. einer `.env` im Projektverzeichnis gelesen, **nicht** aus `.env.local` |
| Grafana-Admin | IT | Passwort-Manager |

## 4. Abhängigkeiten von Dritten (Pflichtenheft §8)

| Abhängigkeit | Status | Verantwortlich |
|---|---|---|
| Stage-ePro mit Testfilmprojekten (AT1, AT8) | offen / terminiert: ____ | ePro-Team |
| Externe Ollama-Instanz auf Produktiv-Hardware GPU ≥ 24 GB (AT6, AT9) | offen / terminiert: ____ | IT |
| Fachinhalte für die KB | offen / laufend | Sicherheitsbeauftragter |
| AVV mit Mistral AI, falls AT8 mit echten Drehbüchern (`DATENSCHUTZ.md` §5) – schließt die Filmakademie selbst ab | offen / entfällt: ____ | Filmakademie |
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
- **Offen (zurückgestellt):** Die Abstimmung des fail-closed-Verhaltens mit dem ePro-Team
  (Darstellung von `failed`, `cancelled` und 410 als „keine automatische Bewertung“, Umgang mit
  den Webhook-Gründen) ist vertagt und vor dem Go-Live nachzuholen (`GO_LIVE_CHECKLIST.md` E).

API und Verhalten (Stand 1.0.2):

- Einziger Prüf-Endpunkt ist `POST /v1/security/check:async`; der synchrone M01-Stub
  `POST /v1/security/check` ist entfernt und antwortet mit 404.
- `callback_url` ist das Push-Ziel des Jobs (statt `EPRO_BASE_URL/eki/scl/set-risk-assessment/{project_id}`),
  aber nur für Hosts aus `CALLBACK_URL_ALLOWED_HOSTS`, die auf öffentliche Adressen auflösen.
  Split-Horizon-DNS mit interner Adresse führt zum Dead Letter `invalid_callback_url`. Ein Push an
  `callback_url` lässt sich lokal nicht gegen den HTTP-ePro-Mock testen (HTTPS und öffentliche
  Adresse nötig).
- `priority` ordnet nur **wartende** Aufgaben einer Task-Queue (kein Abbruch, keine Verdrängung
  laufender Jobs; bei mehreren Task-Queue-Partitionen nur näherungsweise). Wirkt nur mit
  `matching.useNewMatcher: true` (in `docker/temporal/dynamicconfig/eki.yaml` gesetzt); bei
  einem extern betriebenen Temporal-Cluster muss dessen Betreiber das setzen. Eine
  Abbruchfunktion in der API gibt es nicht (Abbruch nur über Temporal).
- Für `/v1/kb/*` ist ein Admin-Key nötig (auch für `scripts/seed_kb.py`).
- Ein One-Shot-GET während laufender Push-Retries beendet die Zustellung als Fehler: Der
  nächste Push-Versuch findet den Report nicht mehr (Dead Letter `report_expired`).
- Dead-Letter-Gründe `report_expired`, `invalid_project_id` und (1.0.1) `invalid_callback_url`;
  der Webhook meldet zusätzlich Verarbeitungsfehler (`prompt_blocked`, `parsing_failed`,
  `processing_failed`, ohne Dead Letter). Beides ist in der OpenAPI-Spec beschrieben.
- Die OpenAPI-Spec unter `/openapi.json` (nur `DEBUG=true`) wird von FastAPI generiert;
  verbindlich ist `openapi/eki-api-v1.0.yaml` (Contract-Test sichert Routen/Enums ab).

Daten und Datenschutz (Details: `docs/DATENSCHUTZ.md`):

- **Echte Drehbücher:** Seit 1.0.1 aus Repository und Images entfernt (nur noch synthetische
  Fixtures; lokale echte Drehbücher nur im git-/docker-ignorierten `tests/fixtures/pdf/private/`).
  Die 15 Dateien realer Filmakademie-Produktionen bleiben aber in der **Git-Historie** (seit März
  2026) und in den **GHCR-Images bis einschließlich 1.0.0**; die Historie wird nach
  Projektentscheidung nicht umgeschrieben. Alte Images löschen bzw. Zugriff beschränken
  (`DATENSCHUTZ.md` §8).
- Metadaten (`job_metadata`, `report_metadata`, `delivery_dead_letters`, nicht mehr nutzbare
  `api_keys`, `audit_logs`) werden nach **90 Tagen** automatisch gelöscht – Dead Letters auch
  unquittiert, also innerhalb der Frist bearbeiten. Die Tabelle `audit_logs` existiert, wird aber
  nicht beschrieben.
- Die Temporal-Workflow-Historie ist seit 1.0.1 verschlüsselt (lesbar nur `job_id` und technische
  Namen); Historien von Workflows, die vor dem Update gestartet wurden, enthalten bis zum Ablauf
  der Namespace-Retention (Standard 24 h) noch Metadaten und Drehbuchtitel im Klartext.
  Drehbuchtext und Findings laufen nur als Buffer-Referenz.
- Container-Logs werden in Produktion nur nach Größe rotiert (20 MB × 5 je Container), nicht nach
  Zeit; eine zeitliche Frist braucht zentrale Log-Weiterleitung.
- KB-Chunk-Texte liegen im Klartext in `kb_embeddings`; nur der Originaltext ist verschlüsselt.
  Hochladen und Löschen nur mit Admin-Key.

LLM und Analyse:

- Mit `LLM_PROVIDER=mistral_cloud` ist die Knowledge Base nicht nutzbar (der Provider hat
  kein `embed()`): Upload schlägt fehl, Retrieval liefert keinen Kontext.
- Paritäts- und Großdokument-Messwerte liegen erst nach dem Lauf auf der Zielhardware vor
  (`M11_PARITY_REPORT.md` §3, `M07_ACCEPTANCE_EVIDENCE.md` §6); die Werkzeuge sind vollständig.
- OCR ist auf `OCR_MAX_PAGES` (Default 50) begrenzt; darüber werden Scan-Seiten mit Warnung übersprungen.
- Dead Letters lassen sich nicht aus der eKI heraus erneut zustellen (Inhalt ist gelöscht);
  Recovery = erneuter Upload im ePro. Bewusste Konsequenz des Processing-Only-Prinzips.

Technik und Wartung:

- **Redis 7.2** ist gepinnt (letzte BSD-3-Clause-Linie). Upstream wird 7.2 nur so lange
  unterstützt wie 7.4; Termine sind nicht veröffentlicht. Danach muss Redis ersetzt werden –
  durch Valkey (BSD-3-Clause, kompatibel zu Redis 7.2) oder Redis ≥ 8 (Lizenzwahl inkl. AGPLv3,
  rechtlich prüfen). Vorgehen: `OPERATIONS_GUIDE.md` §4.9. Bestehende Installationen liefen mit
  Redis 7.4; deren Redis-Volume ist beim Update auf 1.0.1 zu leeren (§4.10).
- **Ollama extern:** Die eKI ist nicht gegen eine bestimmte Ollama-Version getestet; der Pin des
  Entwicklungs-Overlays (`ollama/ollama:0.33.3`) ist an die Version der Produktionsinstanz
  anzugleichen, nach Ollama-Updates den Paritätslauf wiederholen (`OPERATIONS_GUIDE.md` §4.11).
- Ungenutzte Abhängigkeiten sind seit 1.0.1 entfernt; `requests` ist direkt deklariert.
- `mypy` (strict) über `api core services workflows worker llm parsers` ist seit 1.0.1
  befundfrei (vorher 71 Befunde, keiner davon ein Laufzeitfehler) und blockiert die CI
  (`TESTING_GUIDE.md` §5); `db/` und `scripts/` werden nicht typgeprüft.
- Technische Schuld: Die Dev-Abhängigkeit `types-redis` ist veraltet (Stubs für redis 4.6;
  redis ≥ 5 bringt eigene Typen mit) und verdeckt die Typen von redis 8.1. Beim nächsten
  Lock-Update entfernen; dann in `api/dependencies.py` den Alias `RedisClient` auf
  `aioredis.Redis` vereinfachen, das `# type: ignore[attr-defined]` an `aclose()` streichen und in
  `services/secure_buffer.py` `aioredis.Redis[Any]` durch `aioredis.Redis` ersetzen.
- Tech-Stack-Abweichungen zum Pflichtenheft §4.1, bewusst und dokumentiert: Temporal statt
  Celery/Flower (durable Timer für 6-h-Fenster), kein PgBouncer (Async-Pool von SQLAlchemy genügt
  bei der erwarteten Last).

## 6. Rechte & Eigentum (Pflichtenheft §12)

Mit vollständiger Vergütung gehen Eigentums- und Nutzungsrechte am Quellcode, den
Konfigurationen und der Dokumentation an die Filmakademie Baden-Württemberg über.
Drittkomponenten verbleiben unter ihren eigenen Lizenzen. Ausnahme vom Rechteübergang
sind außerdem die echten Drehbücher, die bis 1.0.0 in `tests/fixtures/pdf/` lagen und in der
Git-Historie sowie in Images ≤ 1.0.0 noch enthalten sind (Rechte bei den Urhebern, §5).

### 6.1 Python-Laufzeitabhängigkeiten

Ermittelt aus den Paket-Metadaten (`License-Expression`, `License`, Classifier) aller
78 in `requirements.lock` gepinnten Pakete (Python 3.11, Stand 16.10.2026; mit 1.0.1 wurden
ungenutzte Pakete entfernt, keine verbliebene Version hat sich geändert). Die große
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
| Redis (`redis:7.2-alpine`, seit 1.0.1 gepinnt) | BSD-3-Clause (bis einschließlich 7.2; ab 7.4 RSALv2/SSPLv1, ab 8 zusätzlich AGPLv3) | Support-Risiko: 7.2 wird upstream nur so lange unterstützt wie 7.4, danach Wechsel auf Valkey (BSD-3-Clause) oder Redis ≥ 8 nötig (`OPERATIONS_GUIDE.md` §4.9) |
| Temporal Server und UI (`temporalio/auto-setup`, `temporalio/ui`) | MIT | |
| Ollama | MIT | Produktion: externe Instanz, Betrieb und Lizenz beim Betreiber der Instanz; im Repository nur das optionale Entwicklungs-Overlay `docker-compose.ollama.yml` (`ollama/ollama:0.33.3`) |
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
z. B. auf dem Ollama-Host `ollama show mistral-small3.2 --license`.

## 7. Kontakt nach Übergabe

StefanAI – Research & Development, info@stefanai.de, +49 177 5228242 (gemäß Supportvertrag).
