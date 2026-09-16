# M10 Acceptance Evidence – Outbound-Adapter Hardening (Push/Pull)

Belegsammlung für die Trello-Karte „M10 – Outbound-Adapter Hardening".
Pflichtenheft v1, §3.2 Lieferumfang (Outbound-Adapter: Push/Pull, Idempotenz,
Retries), §4.2 Write-Back-Kontrakt, §5 Zuverlässigkeit, §7 Abnahmetests 2/3/4/5,
§9 M10-Artefakte (Retries/Backoff, Dead-Letter-Queues, Idempotenz-Nachweise,
Failover-Szenarien), §10 PT-Tabelle (4 PT).

---

## 1. Pflichtenheft-Mapping

| Pflichtenheft-Stelle | Forderung | M10-Beleg |
|---|---|---|
| §4.2 Push | 201 → Sofort-Löschung; bei Nichtverfügbarkeit Retry in definierter Dauer/Häufigkeit | `deliver_report_activity`: 2xx → `buffer.delete`; 5xx/408/425/429/Transport → Temporal-Retry (2 s→10 min, 6 h) |
| §4.2 Pull | One-Shot, ≤ 6 h verschlüsselt, danach Auto-Löschung | `_watch_pull_retrieval`: durable Timer bis Buffer-TTL, dann `check_report_retrieved` → Cleanup + Dead Letter + Webhook |
| §5 Zuverlässigkeit | Fehlgelaufene Aufträge landen in sicherer Warteschlange und werden ausgewertet | Tabelle `delivery_dead_letters` + `/v1/ops/dead-letters` + Alert `EkiDeadLettersPending` |
| §5 Zuverlässigkeit | Doppelte Zustellungen technisch vermieden (idempotent) | Header `Idempotency-Key: <report_id>` an ePro; Unique `(user_id, idempotency_key)`; atomarer Insert-or-Get |
| §6 | Job-Übersicht ohne Inhalte | `GET /v1/ops/jobs`, `GET /v1/ops/summary` (nur Metadaten) |
| §7 Test 2 | Push: nach 2xx keine Inhalte in eKI | `test_m10_delivery_activity.py::test_success_marks_delivering_then_delivered_with_headers` + `test_m08_buffer_lifecycle.py` |
| §7 Test 3 | Pull: einmalig abrufbar | `X-One-Shot: true`-Header, `test_get_report_sets_one_shot_header_and_marks_delivered` |
| §7 Test 4 | Retries bis 6 h, danach Löschung + Metadaten-Webhook | `test_m10_workflow_failover.py::test_push_transient_errors_exhaust_6h_window_then_dead_letter` (echter Temporal-Testserver, Time-Skipping) und `::test_pull_not_retrieved_within_ttl_is_dead_lettered` |
| §7 Test 5 | Gleicher Idempotency-Key erzeugt keine Duplikate | `TestUserScopedIdempotency` (gleicher Nutzer → gleicher Job, Race → bestehender Job, andere Nutzer → getrennt) |
| Anhang 1 | `X-One-Shot`-Header, `JobStatus.delivering` | `api/routers/security.py`, `core/models.py::JobStatus.DELIVERING` |
| Anhang 2 | `AuditMetadata.delivery {last_code, attempts}` | `job_metadata.delivery_attempts`, `delivery_last_status_code`, `delivered_at` |
| §10 PT | 4 PT | Eingehalten |

---

## 2. Behobene Defekte aus M05–M08

| Defekt | Auswirkung | Fix |
|---|---|---|
| Job wurde **vor** dem Push auf `COMPLETED` gesetzt | ePro sah „fertig", obwohl Zustellung noch lief/fehlschlug | Status `DELIVERING`; `COMPLETED` erst nach 2xx (`_record_attempt(delivered=True)`) |
| `ReportMetadata` wurde bei jedem Retry neu eingefügt (IntegrityError verschluckt) | DB-Fehlerlog bei jedem Retry | Idempotenter Insert (`_mark_delivering`) |
| `attempts_used` hartkodiert 1 | Webhook meldete falsche Versuchszahl | `activity.info().attempt` persistiert und gemeldet |
| Keine Idempotenz-Header an ePro | Doppel-Persistierung bei Retry nach Timeout möglich | `Idempotency-Key`, `X-EKI-Job-Id`, `X-EKI-Attempt`, `X-Request-ID` |
| 429/408 als Hard-Fail behandelt | Rate-Limit von ePro beendete Zustellung endgültig | `_RETRYABLE_4XX = {408, 425, 429}` |
| `idempotency_key` global unique | Mandant A erhielt `job_id` von Mandant B bei Key-Kollision | Unique `(user_id, idempotency_key)`, Lookup nutzerbezogen |
| Race zweier gleicher POSTs → unbehandelter IntegrityError (500) | Duplikat oder Fehler | Rollback + Re-Select → 202 mit bestehendem Job |
| Buffer-Eintrag blieb bei fehlgeschlagenem `start_workflow` | Verwaistes Skript bis TTL | `buffer.delete(ref_key)` im Fehlerpfad |
| `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` = 4 h < 6 h Retry-Fenster | Temporal hätte den Workflow **vor** Cleanup/Webhook beendet | Default 10 h |
| Pull-Modus ohne Ablaufbehandlung | Nicht abgeholte Reports verfielen still (kein Webhook) | Pull-TTL-Watch mit Dead Letter `pull_ttl_expired` |

---

## 3. Schema (Alembic `f1a2b3c4d5e6`, additiv)

- `job_metadata`: `delivery_status`, `delivery_attempts`, `delivery_last_status_code`,
  `delivery_last_attempt_at`, `delivered_at`; Backfill für Altbestand.
- `delivery_dead_letters`: `id, job_id, report_id, project_id, user_id, delivery_mode,
  reason, attempts, last_status_code, last_error_type, webhook_sent, created_at,
  acknowledged_at, acknowledged_by, note` – **kein Inhalt**.
- Index `ix_job_metadata_idempotency_key` nicht mehr unique; neu
  `uq_job_metadata_user_idempotency (user_id, idempotency_key)`.
- `api_keys.is_admin` (Default `false`).

Rollback: `alembic downgrade e8f1c2d3a401` (droppt Tabelle/Spalten, stellt globalen Unique-Index wieder her).

---

## 4. Workflow-Änderungen (`workflows/security_check.py`)

- `_report_and_deliver`: Ergebnis enthält `delivery_attempts`; Failure-Branch erhält
  `delivery_mode` und `last_status_code`.
- Neu `_watch_pull_retrieval`: `workflow.sleep(buffer_ttl_seconds)` (aus `job_data` eingefroren)
  → `check_report_retrieved_activity` → bei `retrieved=false` Failure-Branch `pull_ttl_expired`.
- `_handle_delivery_failure`: Reihenfolge Cleanup → Job `failed` → Webhook → **Dead Letter**
  (`record_dead_letter_activity`, mit `webhook_sent`).
- Workflow-Signatur unverändert; bestehende Historien replayen.

---

## 5. Tests

```bash
.venv/bin/python -m pytest tests/test_m10_delivery_activity.py \
    tests/test_m10_workflow_failover.py \
    tests/test_m10_ops_and_idempotency.py --no-cov -v
```

| Datei | Tests | Schwerpunkt |
|---|---|---|
| `tests/test_m10_delivery_activity.py` | 13 | Bookkeeping-Aufrufe, Idempotenz-Header, 2xx/4xx/429/5xx/Transport-Klassifikation, Pull ohne Transport, Non-Fatal-Pfade |
| `tests/test_m10_workflow_failover.py` | 5 | **Echter `SecurityCheckWorkflow` auf Temporal-Testserver**: Push-Erfolg, Hard-4xx → Failure-Branch, 6h-Fenster erschöpft (Time-Skipping), Pull-TTL abgelaufen, Pull abgeholt |
| `tests/test_m10_ops_and_idempotency.py` | 12 | Admin-Gate (401/403), Job-Übersicht inhaltsfrei + Filter, Summary, Dead-Letter list/get/ack/409/404, nutzerbezogene Idempotenz inkl. Race, Buffer-Cleanup bei Start-Fehler, `X-One-Shot` + `delivered` |

Bestehende M05/M08-Tests (`test_workflows.py`, `test_m08_*`) laufen unverändert grün.

---

## 6. Failover-Szenarien

Dokumentiert und mit Handlungsanweisungen in `docs/M10_FAILOVER_RUNBOOK.md`:
ePro 5xx/Timeout, ePro 4xx, 6h-Fenster erschöpft, Pull nicht abgeholt, Webhook
nicht zustellbar, Worker-Ausfall mid-delivery, Redis-Ausfall, doppelte Einreichung.

---

## 7. Manuelle Smoke-Prüfung

```bash
docker compose exec api alembic upgrade head          # f1a2b3c4d5e6 (head)
docker compose exec api python scripts/create_api_key.py --insert --admin
curl -sH "Authorization: Bearer $EKI_ADMIN_KEY" localhost:8000/v1/ops/summary | jq
# Push gegen nicht erreichbares ePro (EPRO_BASE_URL auf Dummy setzen), Job starten,
# Temporal UI: deliver_report retried; nach Ablauf (oder Abbruch) Dead Letter sichtbar:
curl -sH "Authorization: Bearer $EKI_ADMIN_KEY" localhost:8000/v1/ops/dead-letters | jq '.items[0]'
```

---

**Status M10:** Implementierung abgeschlossen, abnahmebereit.
