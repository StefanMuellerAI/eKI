# Schulungsunterlagen – eKI API v1.0.2

Drei Zielgruppen, je ca. 45–60 Minuten. Alle Übungen laufen gegen einen
lokalen Stack (`docker compose … up -d`) oder die Stage.

---

## Modul A – ePro-Integratoren (Entwicklung eProjekt)

**Ziel:** Drehbuch einreichen, Status pollen, Report abholen bzw. Push empfangen; Fehler richtig behandeln.

1. **Architektur in 5 Minuten** – Processing-Only, Write-Through, Delete-on-Delivery
   (`README.md` „Architektur", Pflichtenheft §4.1/4.2).
2. **Authentifizierung** – `Authorization: Bearer eki_…`, Service-Account-Key, optional
   `X-Actor-User-Id` (muss zum Key passen, sonst 403). Übung: Postman „5. Security Tests".
3. **Einreichen** – JSON/Base64 vs. Multipart; Pflichtfelder; `script_id` für die Korrelation;
   `delivery=pull|push`; `idempotency_key` **immer** setzen (z. B. Dokument-ID + Version).
   Übung: Postman „3. FDX Workflow" und „2. PDF Workflow".
4. **Polling** – Status-Werte `pending → running → delivering → completed | failed`;
   `metadata.delivery_status`; empfohlene Intervalle, Timeout ≥ 4,5 h (Warteschlange) und Rate Limit je Quell-IP
   (Guide §3, §8). **`failed` heißt „keine automatische Bewertung – manuelle Prüfung", nie
   „kein Risiko"** (fail-closed, z. B. bei Parsing-Fehler, LLM-Ausfall oder
   Prompt-Injection-Filter; inhaltsfreie `error_message`, optional Webhook).
   Übung: Poll-Request wiederholen.
5. **Pull** – One-Shot-GET, `X-One-Shot: true`, 410 beim zweiten Mal, PDF dekodieren (PHP-Beispiel Guide §4);
   410 auch, wenn der Report nicht mehr im Buffer liegt (TTL abgelaufen, bereits gepusht);
   503 beim Abruf = später erneut versuchen, der Report bleibt abrufbar.
6. **Push** – Was ePro empfängt (Multipart `script_id`, `status`, `assessment`, `file`) an
   `EPRO_BASE_URL/eki/scl/set-risk-assessment/{project_id}` bzw. an `callback_url`, falls angegeben
   (nur freigegebene Hosts, impliziert `delivery=push`; `priority` 1–10 ordnet wartende Jobs),
   Header `Idempotency-Key` deduplizieren, 201 antworten; Retry-Verhalten der eKI (5xx → bis 6 h);
   im Push-Modus nicht zusätzlich per GET abholen.
7. **Fehler** – `ErrorResponse` (`error`, `message`, `details`, `request_id`, `timestamp`);
   `request_id` ist immer gesetzt und gleich dem Header `X-Request-ID` – bei Support-Anfragen
   mitgeben; 403 (`X-Actor-User-Id` passt nicht), 503 (Backend nicht erreichbar → später mit
   neuem `idempotency_key` erneut, falls der gleiche Key einen `failed`/hängenden Job liefert –
   Guide §6); Webhook `security.delivery.failed` verarbeiten.
8. **Übung Abschluss** – Postman-Ordner „10. Abnahmetests" als Collection-Run.

Material: `docs/EPRO_INTEGRATION_GUIDE.md`, `openapi/eki-api-v1.0.yaml`, Postman v1.0.

---

## Modul B – IT-Betrieb

**Ziel:** Stack betreiben, überwachen, Störungen beheben, Releases einspielen.

1. **Komponenten & Ports** – `OPERATIONS_GUIDE.md` §1; nur API veröffentlicht.
2. **Installation & Konfiguration** – Installation, `docs/CONFIGURATION.md`; `.env.local`-Validator
   (lehnt Platzhalter-Secrets ab); Compose-Variablen (`EKI_IMAGE_TAG`, `GRAFANA_ADMIN_PASSWORD`,
   `LLM_PROVIDER` im Prod-Overlay) kommen aus Shell bzw. `.env`, nicht aus `.env.local`;
   Änderungen an `.env.local` erst nach `docker compose up -d --force-recreate api worker` wirksam;
   Secrets-Handling (`secrets/README.md`).
3. **Keys** – `docker compose exec api python scripts/create_api_key.py --insert` (`--admin` für Ops
   und Knowledge Base), Rotation, Deaktivierung per SQL (`is_active=false`), Ablauf nach 365 Tagen;
   gesperrte/abgelaufene Keys löscht der Metadaten-Cleanup nach 90 Tagen.
4. **Monitoring** – Grafana-Dashboards `eki-overview`/`eki-delivery`/`eki-llm`; SLOs S1–S9
   (`M09_SLO.md`); Alerts und was sie bedeuten. Übung: Alert `EkiDeadLettersPending` provozieren
   (Mock 422) und über `/v1/ops/dead-letters` quittieren.
5. **Logs & Korrelation** – JSON-Logs, `request_id`/`job_id` in jeder Worker-Zeile; Log-Hygiene
   (keine Inhalte). Übung: Job-Verlauf anhand `job_id` durch API- und Worker-Logs verfolgen.
6. **Zustellstörungen** – `M10_FAILOVER_RUNBOOK.md` Szenarien 2.1–2.8; Temporal UI lesen
   (in Produktion nicht veröffentlicht, Zugriff per Tunnel; zeigt seit 1.0.1 Eingaben und Ergebnisse
   nur verschlüsselt, lesbar sind IDs, Activity-Namen, Zeiten und Fehlertypen); Dead Letters innerhalb
   von 90 Tagen bearbeiten.
7. **LLM-Betrieb** – externe Ollama-Instanz (URL, Modelle, Version, Erreichbarkeit – `/ready` prüft
   Ollama nicht; `OLLAMA_NUM_PARALLEL` & Co. werden auf dem Ollama-Host gesetzt), VRAM,
   Dokument-Limit und Warteschlange (`MAX_CONCURRENT_DOCUMENTS`, `OPERATIONS_GUIDE.md` §5.12),
   `OLLAMA_MAX_CONCURRENT_REQUESTS`, Queue-Wait-Metrik, OCR-Cap.
   Paritätslauf als Regressionsprüfung nach Modell-, Prompt- oder Ollama-Update.
8. **Update/Rollback** – `EKI_IMAGE_TAG=<Release-Tag>` setzen, `pull` + `up -d --no-build` (API und
   Worker immer gemeinsam), `alembic upgrade head` (Head `c4e6a8b0d2f1` prüfen); vor Releases mit
   geänderten Workflows, vor einer `API_SECRET_KEY`-Rotation und vor einem Rollback laufende Workflows
   auslaufen lassen (Temporal-Replay, Payload-Verschlüsselung). Einmalig: Upgrade 1.0.0 → 1.0.1
   (`OPERATIONS_GUIDE.md` §4.10). Wartungs-Schedules werden über `*_ENABLED`/`*_CRON` gesteuert,
   nicht in der Temporal UI.
9. **Backups** – nur Postgres (für KB- und Temporal-Restore denselben `API_SECRET_KEY`); Redis bewusst
   nicht. Redis 7.2: Support-Status beobachten (`OPERATIONS_GUIDE.md` §4.9).
10. **Datenschutz** – was wo liegt und wann es gelöscht wird: Metadaten 90 Tage, Temporal-Retention,
   Log-Rotation nach Größe (`docs/DATENSCHUTZ.md`).

Material: `docs/OPERATIONS_GUIDE.md`, `docs/CONFIGURATION.md`, `docs/DATENSCHUTZ.md`,
`docs/M09_SLO.md`, `docs/M10_FAILOVER_RUNBOOK.md`.

---

## Modul C – Sicherheitsbeauftragter (Fachliche Pflege)

**Ziel:** Fachinhalte pflegen, die die Analysequalität bestimmen (Pflichtenheft §4.3 „Fachliche Inhalte & Pflege").

1. **Was die eKI liefert** – Hinweise, keine Gutachten (§3.3); Report-Aufbau (Executive Summary,
   Szenen-Befunde, Maßnahmen-Checkliste); Severity = Likelihood × Impact.
2. **Taxonomie** – `config/taxonomy/taxonomy.yaml`: 23 Klassen in 3 Kategorien, Rule-IDs;
   `measures.yaml`: 20 Maßnahmen mit Rolle und Frist. Änderungen ohne Code-Deployment;
   Wirkung im nächsten Job. Übung: neue Maßnahme anlegen und einer Klasse zuordnen.
3. **Wissensbasis (KB)** – Welche Dokumente sinnvoll sind (Leitfäden, SOPs, Checklisten,
   regulatorische Vorgaben); Ablage in `config/kb_seed/real/`; `scripts/seed_kb.py` mit Admin-Key
   (`--wipe-placeholders`, `--reseed`, `--status`); TTL (`ttl_hours`, Standard 30 Tage) und
   automatischer Cleanup (Temporal-Schedule `eki-kb-cleanup`, täglich); Embedding-Modell
   (Wechsel nur mit Neuaufbau der KB); Verschlüsselung (nur Originaltext, Chunks im Klartext)
   und Zugriffskontrolle (`M06_KB_GUIDE.md`).
4. **Prompts** – `config/prompts/prompts.yaml`: Wo Fachsprache und Schwellen stehen; Änderungen
   immer mit `scripts/run_parity.py` gegen das Golden-Set prüfen (`tests/parity/golden_scenes.yaml`,
   Aufruf im API-Container: `UAT_TESTPLAN.md` AT8); Golden-Set um eigene Fälle erweitern.
   Der Prompt-Injection-Filter bricht einen Job ab, wenn der Drehbuchtext gezielte
   Anweisungen an das Modell enthält (englischsprachige Muster, `core/prompt_sanitizer.py`);
   solche Drehbücher manuell prüfen.
5. **Qualitätsschleife** – Report-Stichproben, False Positives/Negatives melden, Golden-Set pflegen,
   Brückenmeeting (§13) als Eskalationsweg.

Material: `docs/M06_KB_GUIDE.md`, `docs/M11_PARITY_REPORT.md`, `config/taxonomy/`, `config/prompts/`.

---

## Nachweis der Schulung

| Modul | Datum | Teilnehmende | Trainer | Bemerkung |
|---|---|---|---|---|
| A | | | | |
| B | | | | |
| C | | | | |
