# M09 – Service Level Objectives (SLOs) der eKI API

Stand: M09. Ableitung aus Pflichtenheft v1 §3.4 (Erfolgskriterien), §5
(Leistung, Zuverlässigkeit), §6 (Dashboards mit Kennzahlen) und §7
(Abnahmetests). Alle Kennzahlen werden aus den Prometheus-Metriken in
`core/metrics.py` berechnet; die Alerts stehen in
`docker/observability/alerts.yml`, die Dashboards werden aus
`scripts/observability/build_dashboards.py` generiert.

---

## 1. Service-Level-Indikatoren (SLIs)

| SLI | Definition (PromQL-Basis) | Quelle |
|---|---|---|
| Verfügbarkeit API | Anteil erfolgreicher Scrapes `up{job="eki-api"}` | Prometheus |
| Verfügbarkeit Worker | `up{job="eki-worker"}` | Prometheus |
| HTTP-Fehlerrate | `rate(eki_http_requests_total{status=~"5.."})` / `rate(eki_http_requests_total)` | API-Middleware |
| Job-Laufzeit | `histogram_quantile(0.95, eki_job_duration_seconds_bucket)` | `update_job_status_activity` (Terminalzustand) |
| Job-Erfolgsquote | `eki_jobs_total{status="completed"}` / alle Terminalzustände | dito |
| Zustellerfolg (Push) | `eki_delivery_attempts_total{mode="push",outcome="success"}` / alle Push-Versuche | `deliver_report_activity` |
| Delete-on-Delivery | `eki_buffer_deletes_total{source=~"push\|pull"}` ≥ erfolgreiche Zustellungen | Activity + Router |
| LLM-Fehlerrate | `eki_llm_requests_total{outcome="error"}` / alle | Provider-Adapter |
| LLM-Wartezeit | `histogram_quantile(0.95, eki_llm_queue_wait_seconds_bucket)` | `_ollama_slot()` |
| Dead Letters | `eki_dead_letters_unacknowledged` | M10 Ops-Endpoints |

---

## 2. Service-Level-Objectives

| # | SLO | Ziel | Messfenster | Herleitung |
|---|---|---|---|---|
| S1 | API-Verfügbarkeit | ≥ 99,5 % | 30 Tage rollierend | Intranet-Dienst, ein Ausfall von ≤ 3,6 h/Monat ist tolerierbar, da ePro Jobs asynchron nachreicht |
| S2 | Worker-Verfügbarkeit | ≥ 99,5 % | 30 Tage | Jobs puffern in Temporal, aber Laufzeit-SLOs (S3–S5) hängen davon ab |
| S3 | Laufzeit kleine Prüfung (≤ 50 Szenen) | p95 ≤ 10 min | 7 Tage | Pflichtenheft §5 |
| S4 | Laufzeit 120 Seiten | p95 ≤ 60 min | 7 Tage | Pflichtenheft §5 |
| S5 | Laufzeit 300–350 Seiten | p95 ≤ 120 min | 7 Tage | Pflichtenheft §5, Abnahmetest 6 |
| S6 | Job-Erfolgsquote | ≥ 95 % `completed` | 7 Tage | Fehlgeschlagene Jobs sind fachlich nachzuarbeiten |
| S7 | Zustellerfolg Push innerhalb 6 h | ≥ 99 % | 7 Tage | Pflichtenheft §5 Zuverlässigkeit, Abnahmetest 4 |
| S8 | Delete-on-Delivery | 100 % | fortlaufend | Pflichtenheft §4.2, Abnahmetests 2/3 – kein Report darf nach 2xx im Buffer bleiben |
| S9 | LLM-Fehlerrate | ≤ 5 % | 24 h | Ollama-Ausfälle schlagen direkt in S3–S6 durch |

### Hinweis zu S3–S5

Die Metrik `eki_job_duration_seconds` trägt bewusst kein Seiten- oder
Szenen-Label (Kardinalität). Die Alerts prüfen daher die konservative
Hülle „p95 aller Jobs ≤ 120 min". Für die Abnahme werden S3–S5 mit den
dedizierten Benchmark-Runnern (`tests/run_pdf_m07_benchmark.py`,
`tests/run_security_check.py`) auf der Zielhardware belegt; die
Prometheus-Werte dienen dem laufenden Betrieb.

---

## 3. Error Budgets

| SLO | Budget pro 30 Tage | Reaktion bei Verbrauch > 50 % |
|---|---|---|
| S1/S2 (99,5 %) | 3 h 36 min Ausfall | Ursachenanalyse, ggf. Redundanz-Worker |
| S6 (95 %) | 5 % der Jobs | LLM-Qualität/Parser prüfen, `docs/M10_FAILOVER_RUNBOOK.md` |
| S7 (99 %) | 1 % der Push-Zustellungen | ePro-Verfügbarkeit mit ePro-Team klären (Brückenmeeting §13) |
| S9 (5 %) | 5 % der LLM-Calls | VRAM/Modell prüfen, `OLLAMA_MAX_CONCURRENT_REQUESTS` senken |

---

## 4. Alert-Mapping

| Alert (`alerts.yml`) | SLO | Schwelle | Severity |
|---|---|---|---|
| `EkiApiDown` | S1 | `up == 0` für 2 min | critical |
| `EkiWorkerDown` | S2 | `up == 0` für 5 min | critical |
| `EkiHttp5xxRateHigh` | S1 | > 5 % 5xx für 10 min | warning |
| `EkiJobDurationSloBreach` | S3–S5 | p95 > 7200 s für 30 min | warning |
| `EkiJobFailureRateHigh` | S6 | > 10 % failed für 30 min | warning |
| `EkiDeliveryFailureRateHigh` | S7 | > 50 % Push-Fehlversuche für 30 min | warning |
| `EkiDeadLettersPending` | S7 | > 0 unbestätigte Dead Letters für 15 min | warning |
| `EkiWebhookFailing` | S7 | Webhook-Fehler in 1 h | warning |
| `EkiLlmErrorRateHigh` | S9 | > 20 % Fehler für 15 min | warning |
| `EkiOllamaQueueWaitHigh` | S3–S5 | p95 Wartezeit > 120 s | info |
| `EkiPromptSanitizerBlockedSpike` | Security | > 20 blockierte Prompts/h | info |

---

## 5. Dashboards

| Dashboard (UID) | Zweck |
|---|---|
| `eki-overview` | Verfügbarkeit, Job-Laufzeit vs. SLO, HTTP-Raten, Szenen/Findings |
| `eki-delivery` | Push-Erfolgsquote, Retry-Outcomes, One-Shot-Abrufe, Dead Letters, Delete-on-Delivery-Nachweis |
| `eki-llm` | Latenz p50/p95 je Provider/Operation, Queue-Wait, Fehlerrate, Sanitizer, KB |

Start des Stacks:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
# Grafana: http://localhost:3000 (admin / GRAFANA_ADMIN_PASSWORD)
# Jaeger:  http://localhost:16686
```

---

## 6. Tracing

`OTEL_ENABLED=true` aktiviert OpenTelemetry (FastAPI, SQLAlchemy, httpx,
Temporal). Eine Trace beginnt beim HTTP-Request, läuft über
`start_workflow` in den Workflow und in jede Activity. Zusätzlich trägt
jede Log-Zeile im Worker `request_id`, `job_id`, `workflow_id`,
`activity` und `attempt` (Interceptor `core/temporal_context.py`), sodass
sich Log-Zeilen ohne Tracing-Backend korrelieren lassen.
