# M10 – Failover-Runbook: Outbound-Zustellung (Push/Pull)

> **Revisionshinweis (15.10.2026).** Die Szenarien gelten weiter. Ergänzt bzw. korrigiert:
> Auf PostgreSQL funktioniert der Zustand `delivering` erst ab Migration `a7c9e1f3b5d2`
> (vorher zu kurze Statusspalte). Die Temporal-UI ist in Produktion nicht veröffentlicht
> (Zugriff per Tunnel, `OPERATIONS_GUIDE.md`). `last_error_type` in Dead Letters wird derzeit
> nie befüllt (§2.7). Ein Report, der nicht mehr im Buffer liegt, liefert beim Abruf 410
> (§2.4); fehlt er beim Push, endet die Zustellung sofort mit dem neuen Grund `report_expired`
> statt erst nach 6 h. Weitere neue Gründe: `invalid_project_id` (Push-Schutz). Jobs mit
> `failed` **ohne** Präfix `delivery_failed:` sind keine Zustellstörung, sondern eine
> abgebrochene Verarbeitung (fail-closed: Parsing, LLM-Fehler, Prompt-Injection-Filter); der
> Webhook meldet sie mit `prompt_blocked`, `parsing_failed` oder `processing_failed`, ein Dead
> Letter entsteht nicht – dafür gilt dieses Runbook nicht.

Betriebsanleitung für Störungen der Zustellung an ePro. Ergänzt
`docs/M09_SLO.md` (Alerts) und `docs/EPRO_INTEGRATION_GUIDE.md` (Kontrakt).

---

## 1. Zustandsmodell

```
pending ──(aggregate_report ok)──▶ delivering ──(Push 2xx | One-Shot-GET)──▶ delivered
                                       │
                 ┌─────────────────────┼──────────────────────┐
        4xx Hard-Fail          6h Retry erschöpft       Pull-TTL (6h) abgelaufen
                 └─────────────────────┴──────────────────────┘
                                       ▼
                               dead_lettered
                    (Buffer gelöscht, Job failed, Webhook, Dead Letter)
```

Persistiert in `job_metadata`: `delivery_status`, `delivery_attempts`,
`delivery_last_status_code`, `delivery_last_attempt_at`, `delivered_at`.
Im Pull-Modus steht `delivery_status` nach `delivering` wieder auf `pending`, sobald der Report
bereitliegt, und wechselt mit dem One-Shot-GET auf `delivered`.
Dead Letters in `delivery_dead_letters` (inhaltsarm).

**Wichtig:** Nach `dead_lettered` existiert der Report nicht mehr in der eKI
(Delete-on-Delivery, Pflichtenheft §4.2). Recovery = ePro löst den Check neu aus.

---

## 2. Szenarien

### 2.1 ePro antwortet 5xx / Timeout / nicht erreichbar

Erkennung: Alert `EkiDeliveryFailureRateHigh`, Dashboard `eki-delivery`
(Outcome `retryable` / `transport_error` steigt), Jobs mit
`delivery_status=delivering` und wachsendem `delivery_attempts`.

Verhalten: Temporal retried automatisch (2 s → 10 min Backoff) bis 6 h.
Kein manueller Eingriff nötig, solange ePro innerhalb des Fensters zurückkommt.

Maßnahmen:
1. ePro-Erreichbarkeit aus dem Worker-Container prüfen (jeder HTTP-Status zeigt Erreichbarkeit,
   eine Exception nicht):
   `docker compose exec worker python -c "import os,httpx;print(httpx.get(os.environ['EPRO_BASE_URL'],timeout=10).status_code)"`
2. Laufende Zustellungen einsehen: `GET /v1/ops/jobs?delivery_status=delivering` (Admin-Key).
3. Temporal UI (Entwicklung `:8080`, Produktion nur per Tunnel): Workflow `job_id` → Pending Activities → `deliver_report` mit Attempt-Zähler.
4. Ist ePro > 6 h down: Dead Letters sammeln sich (Szenario 2.3); nach Wiederanlauf mit ePro-Team die betroffenen `project_id`s neu anstoßen.

### 2.2 ePro antwortet 4xx (außer 408/425/429)

Erkennung: Outcome `hard_fail`, sofortiger Dead Letter mit `reason=hard_4xx`,
`last_status_code` = ePro-Status.

Ursachen: falscher `project_id`, IP nicht freigeschaltet (401/403), geändertes
ePro-Schema (422), zu große PDF (413).

Maßnahmen:
1. `GET /v1/ops/dead-letters?acknowledged=false` → `last_status_code`, `project_id`.
2. Bei 401/403: IP-Whitelist/`EPRO_AUTH_TOKEN` mit ePro-Team klären (Brückenmeeting §13).
3. Bei 413: `MAX_UPLOAD_SIZE_BYTES`/PDF-Größe prüfen; ePro-Limit 7 MB.
4. Nach Behebung: ePro löst Check neu aus; Dead Letter quittieren
   (`POST /v1/ops/dead-letters/{id}:acknowledge`, Notiz mit Ticket-Nr.).

### 2.3 Retry-Fenster (6 h) erschöpft

Erkennung: `reason=retry_window_exhausted`, Alert `EkiDeadLettersPending`,
Webhook `security.delivery.failed` bei ePro (falls konfiguriert).

Maßnahmen: wie 2.1 Schritt 4; zusätzlich prüfen, ob
`TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` (Default 10 h) nicht unter Verarbeitungs-
zeit + 6 h liegt – sonst bricht Temporal den Workflow vor dem Failure-Branch ab.

### 2.4 Pull-Report nicht abgeholt

Erkennung: `reason=pull_ttl_expired`, Job `failed` mit
`error_message=delivery_failed:pull_ttl_expired`.

Ursache: ePro pollt den Job-Status nicht oder ignoriert `report_id`.
Maßnahmen: ePro-Polling prüfen (Guide §3), ggf. auf `delivery=push` wechseln.
Ruft ePro den Report danach doch noch ab, antwortet die API mit 410 („Report is no longer
available …“); Job-Status und Dead Letter bleiben unverändert.

### 2.5 Webhook nicht zustellbar

Erkennung: Alert `EkiWebhookFailing`, `eki_webhook_sent_total{outcome="failed"}`,
Dead Letter mit `webhook_sent=false`.

Maßnahmen: `EPRO_WEBHOOK_URL` und Erreichbarkeit prüfen; der Dead Letter bleibt
als Quelle der Wahrheit erhalten – Ops informiert ePro manuell.

### 2.6 Worker-Ausfall während der Zustellung

Temporal führt die Activity nach Worker-Neustart am selben Punkt fort
(`attempt` zählt weiter, `ReportMetadata`-Insert ist idempotent). Kein Datenverlust,
kein Doppel-Push ohne `Idempotency-Key`-Wiederholung. Prüfen: `EkiWorkerDown`.

### 2.7 Redis-Ausfall

Report-Inhalte liegen ausschließlich in Redis. Ist Redis nicht erreichbar, scheitert der
Push-Versuch → Retry bis 6 h, danach Dead Letter `retry_window_exhausted`. Sind die Daten
verloren (Redis ohne Persistenz neu gestartet), endet die Zustellung beim nächsten Versuch
sofort mit `report_expired`. `last_error_type` bleibt leer, die Ursache steht in den
Worker-Logs. Redis-Persistenz (`appendonly`) ist im Prod-Compose aktiv; Recovery = ePro
triggert neu. Ist Redis bei einem Pull-Abruf nicht erreichbar, antwortet
`GET /v1/security/reports/{id}` mit 503; der Report bleibt abrufbar.

### 2.8 Doppelte Einreichung (Idempotenz)

Gleicher `idempotency_key` **desselben API-Keys** → derselbe Job (202 mit
bestehender `job_id`), auch bei gleichzeitigen Requests (Unique-Constraint
`(user_id, idempotency_key)`). Unterschiedliche Keys/Nutzer → getrennte Jobs.
Push-Retries tragen `Idempotency-Key: <report_id>`; ePro soll doppelte Eingänge
mit 200 quittieren.

---

## 3. Ops-Endpoints (Admin-Key, `scripts/create_api_key.py --admin`)

| Endpoint | Zweck |
|---|---|
| `GET /v1/ops/summary` | Jobs nach Status/Zustellstatus, offene Dead Letters |
| `GET /v1/ops/jobs?status=&delivery_status=&project_id=&limit=&offset=` | Job-Übersicht ohne Inhalte |
| `GET /v1/ops/dead-letters?acknowledged=false` | Offene Dead Letters |
| `GET /v1/ops/dead-letters/{id}` | Einzelansicht |
| `POST /v1/ops/dead-letters/{id}:acknowledge` `{"note": "..."}` | Quittieren nach Analyse (409 bei Doppelquittung) |

---

## 4. Verifikation nach Störung

```bash
# offene Dead Letters
curl -sH "Authorization: Bearer $EKI_ADMIN_KEY" localhost:8000/v1/ops/dead-letters?acknowledged=false | jq '.unacknowledged'
# Zustellquote der letzten 30 min (Prometheus)
sum(rate(eki_delivery_attempts_total{mode="push",outcome="success"}[30m]))
  / sum(rate(eki_delivery_attempts_total{mode="push"}[30m]))
# Kein Inhalt im Buffer nach Abschluss (Delete-on-Delivery)
docker compose exec redis redis-cli --scan --pattern 'eki:buf:*' | wc -l
```
