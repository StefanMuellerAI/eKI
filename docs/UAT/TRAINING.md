# Schulungsunterlagen – eKI API v1.0.0

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
   `metadata.delivery_status`; empfohlene Intervalle (Guide §3). Übung: Poll-Request wiederholen.
5. **Pull** – One-Shot-GET, `X-One-Shot: true`, 410 beim zweiten Mal, PDF dekodieren (PHP-Beispiel Guide §4).
6. **Push** – Was ePro empfängt (Multipart `script_id`, `status`, `assessment`, `file`),
   Header `Idempotency-Key` deduplizieren, 201 antworten; Retry-Verhalten der eKI (5xx → bis 6 h).
7. **Fehler** – `ErrorResponse` (`error`, `message`, `details`, `request_id`); `request_id` bei
   Support-Anfragen mitgeben; Webhook `security.delivery.failed` verarbeiten.
8. **Übung Abschluss** – Postman-Ordner „10. Abnahmetests" als Collection-Run.

Material: `docs/EPRO_INTEGRATION_GUIDE.md`, `openapi/eki-api-v1.0.yaml`, Postman v1.0.

---

## Modul B – IT-Betrieb

**Ziel:** Stack betreiben, überwachen, Störungen beheben, Releases einspielen.

1. **Komponenten & Ports** – `OPERATIONS_GUIDE.md` §1; nur API veröffentlicht.
2. **Installation & Konfiguration** – §2/§3; `.env.local`-Validator; Secrets-Handling (`*_FILE`).
3. **Keys** – `scripts/create_api_key.py` (`--admin` für Ops), Rotation, Deaktivierung.
4. **Monitoring** – Grafana-Dashboards `eki-overview`/`eki-delivery`/`eki-llm`; SLOs S1–S9
   (`M09_SLO.md`); Alerts und was sie bedeuten. Übung: Alert `EkiDeadLettersPending` provozieren
   (Mock 422) und über `/v1/ops/dead-letters` quittieren.
5. **Logs & Korrelation** – JSON-Logs, `request_id`/`job_id` in jeder Worker-Zeile; Log-Hygiene
   (keine Inhalte). Übung: Job-Verlauf anhand `job_id` durch API- und Worker-Logs verfolgen.
6. **Zustellstörungen** – `M10_FAILOVER_RUNBOOK.md` Szenarien 2.1–2.8; Temporal UI lesen.
7. **LLM-Betrieb** – Ollama-Modelle, VRAM, `OLLAMA_MAX_CONCURRENT_REQUESTS`, Queue-Wait-Metrik,
   OCR-Cap. Paritätslauf als Regressionsprüfung nach Modell-/Prompt-Änderung.
8. **Update/Rollback** – Image-Tag wechseln, `alembic upgrade head`, Workflow-Replay-Sicherheit.
9. **Backups** – nur Postgres; Redis bewusst nicht.

Material: `docs/OPERATIONS_GUIDE.md`, `docs/M09_SLO.md`, `docs/M10_FAILOVER_RUNBOOK.md`,
`docs/DEPLOYMENT_GUIDE.md`.

---

## Modul C – Sicherheitsbeauftragter (Fachliche Pflege)

**Ziel:** Fachinhalte pflegen, die die Analysequalität bestimmen (Pflichtenheft §4.3 „Fachliche Inhalte & Pflege").

1. **Was die eKI liefert** – Hinweise, keine Gutachten (§3.3); Report-Aufbau (Executive Summary,
   Szenen-Befunde, Maßnahmen-Checkliste); Severity = Likelihood × Impact.
2. **Taxonomie** – `config/taxonomy/taxonomy.yaml`: 23 Klassen in 3 Kategorien, Rule-IDs;
   `measures.yaml`: 20 Maßnahmen mit Rolle und Frist. Änderungen ohne Code-Deployment;
   Wirkung im nächsten Job. Übung: neue Maßnahme anlegen und einer Klasse zuordnen.
3. **Wissensbasis (KB)** – Welche Dokumente sinnvoll sind (Leitfäden, SOPs, Checklisten,
   regulatorische Vorgaben); Ablage in `config/kb_seed/real/`; `scripts/seed_kb.py`
   (`--wipe-placeholders`, `--reseed`, `--status`); TTL (`ttl_hours`) und automatischer Cleanup;
   Verschlüsselung und Zugriffskontrolle (`M06_KB_GUIDE.md`).
4. **Prompts** – `config/prompts/prompts.yaml`: Wo Fachsprache und Schwellen stehen; Änderungen
   immer mit `scripts/run_parity.py` gegen das Golden-Set prüfen (`tests/parity/golden_scenes.yaml`);
   Golden-Set um eigene Fälle erweitern.
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
