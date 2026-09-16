# M09 Acceptance Evidence – Observability & SLOs

Belegsammlung für die Trello-Karte „M09 – Observability & SLOs".
Pflichtenheft v1, §3.2 Lieferumfang (Monitoring-Dashboards), §4.1
Observability-Stack, §5 Leistungsziele, §6 Dashboards/Trace-IDs, §9
M09-Artefakte (Prometheus-Metriken, Grafana-Dashboards, Alerts,
SLO-Definitionen), §10 PT-Tabelle (3 PT).

---

## 1. Pflichtenheft-Mapping

| Pflichtenheft-Stelle | Forderung | M09-Beleg |
|---|---|---|
| §4.1 | Prometheus/Grafana (Metriken), strukturierte JSON-Logs, OpenTelemetry (Tracing) | `core/metrics.py`, `docker-compose.observability.yml`, `core/tracing.py` |
| §6 | Dashboards mit Kennzahlen (Zustellraten, Latenzen), Job-Übersicht ohne Inhalte, Logs mit Trace-IDs | 3 Grafana-Dashboards, `core/temporal_context.py` (request_id/job_id in jeder Worker-Log-Zeile) |
| §5 Leistung | Laufzeitziele 10/60/120 min | `eki_job_duration_seconds` mit Buckets auf den SLO-Schwellen, Alert `EkiJobDurationSloBreach`, `docs/M09_SLO.md` S3–S5 |
| §5 Zuverlässigkeit | Zustellraten, Retry-Verhalten sichtbar | `eki_delivery_attempts_total{mode,outcome}`, `eki_delivery_failures_total`, Dashboard `eki-delivery` |
| §4.2 / Abnahmetests 2+3 | Delete-on-Delivery nachweisbar | `eki_buffer_deletes_total{source=push\|pull\|cleanup}` als laufender Beleg |
| §4.3 | TTL-Jobs für die KB | Temporal-Schedule `eki-kb-cleanup` (`workflows/maintenance.py`) |
| §9 M09 | Prometheus-Metriken, Grafana-Dashboards, Alerts, SLO-Definitionen | dieses Dokument, §2–§6 |
| §10 PT | 3 PT | Eingehalten |

---

## 2. Metriken

Alle Metriken liegen in `core/metrics.py` und werden von API **und** Worker exportiert
(gleiche Registry). Kardinalität ist beschränkt: keine Job-, Projekt- oder Nutzer-IDs.

| Familie | Typ | Labels | Einbauort |
|---|---|---|---|
| `eki_http_requests_total`, `eki_http_request_duration_seconds`, `eki_http_requests_in_flight` | Counter/Histogram/Gauge | method, route (Template), status | Middleware `api/main.py` |
| `eki_jobs_total`, `eki_job_duration_seconds` | Counter/Histogram | script_format, status (completed/failed/delivery_failed) | `update_job_status_activity` |
| `eki_scenes_processed_total`, `eki_findings_total` | Counter | stage / severity | `structure_scene_llm_activity`, `analyze_scene_risk_activity` |
| `eki_llm_requests_total`, `eki_llm_request_duration_seconds`, `eki_llm_requests_in_flight`, `eki_llm_queue_wait_seconds` | Counter/Histogram/Gauge | provider, operation, outcome | `observe_llm_call()` in `llm/ollama.py`, `llm/mistral_cloud.py`; `_ollama_slot()` |
| `eki_prompt_sanitizer_hits_total` | Counter | action (blocked/allowed) | `core/prompt_sanitizer.py` |
| `eki_delivery_attempts_total`, `eki_delivery_duration_seconds`, `eki_delivery_failures_total` | Counter/Histogram | mode, outcome / reason | `deliver_report_activity` |
| `eki_webhook_sent_total` | Counter | outcome | `send_delivery_failed_webhook_activity` |
| `eki_report_retrievals_total`, `eki_buffer_deletes_total` | Counter | outcome / source | `GET /v1/security/reports/{id}`, Activities |
| `eki_kb_documents`, `eki_kb_retrieval_hits_total`, `eki_kb_cleanup_removed_total` | Gauge/Counter | – | `_build_kb_context`, `kb_cleanup_expired_activity` |
| `eki_dead_letters_unacknowledged` | Gauge | – | M10 |
| `eki_build_info` | Info | version, llm_provider, role | API-Lifespan, Worker-Start |

Worker-Exporter: `prometheus_client.start_http_server(PROMETHEUS_PORT)` in `worker/main.py`;
der Port wird in `docker-compose.observability.yml` nur per `expose` freigegeben.

---

## 3. Tracing & Log-Korrelation

- `core/tracing.py::configure_tracing(settings, role, app)` – TracerProvider mit OTLP/HTTP-Exporter,
  FastAPI-, SQLAlchemy-, httpx-Instrumentierung. Gate `OTEL_ENABLED` (Default `false`).
- `core/tracing.py::temporal_interceptors(settings)` – liefert `ContextPropagationInterceptor`
  (immer) und `temporalio.contrib.opentelemetry.TracingInterceptor` (bei OTel).
  Verdrahtet in `api/dependencies.py::get_temporal_client` und `worker/main.py`.
- `core/temporal_context.py` – trägt `request_id` als Temporal-Header vom API-Client in den
  Workflow und in jede Activity; bindet dort `request_id`, `job_id`, `workflow_id`, `activity`,
  `attempt` als structlog-Kontext.

Beispiel-Log-Zeile aus dem Worker (Test `test_log_line_contains_correlation_fields`):

```json
{"event": "hello from activity", "level": "info", "timestamp": "...",
 "request_id": "req-e2e", "job_id": "job-e2e", "workflow_id": "job-e2e",
 "activity": "analyze", "attempt": 1}
```

---

## 4. Stack, Alerts, Dashboards

| Artefakt | Pfad |
|---|---|
| Compose-Overlay (Prometheus, Alertmanager, Grafana, Jaeger) | `docker-compose.observability.yml` |
| Prometheus-Scrape-Konfiguration (API per Bearer-Key-File, Worker intern) | `docker/observability/prometheus.yml` |
| 11 Alert-Regeln in 4 Gruppen | `docker/observability/alerts.yml` |
| Alertmanager-Routing (Receiver-Platzhalter) | `docker/observability/alertmanager.yml` |
| Grafana-Provisioning (Datasources Prometheus + Jaeger, Dashboard-Provider) | `docker/observability/grafana/provisioning/` |
| Dashboards `eki-overview`, `eki-delivery`, `eki-llm` | `docker/observability/grafana/dashboards/*.json` (generiert aus `scripts/observability/build_dashboards.py`) |
| SLOs, Error-Budgets, Alert-Mapping | `docs/M09_SLO.md` |

CI validiert `alerts.yml` mit `promtool check rules`, `alertmanager.yml` mit `amtool check-config`
und das Compose-Overlay mit `docker compose config` (Job `observability-validation`).

---

## 5. KB-TTL-Schedule

`workflows/maintenance.py`: Activity `kb_cleanup_expired` + Workflow `KBCleanupWorkflow`;
`ensure_kb_cleanup_schedule()` legt beim Worker-Start den Schedule `eki-kb-cleanup`
(`KB_CLEANUP_CRON`, Default `0 3 * * *`, Overlap-Policy SKIP) idempotent an. Schließt die
in M08 offen gelassene Lücke (Pflichtenheft §4.3 „TTL-Jobs").

---

## 6. Tests

```bash
.venv/bin/python -m pytest tests/test_m09_metrics.py \
    tests/test_m09_tracing_context.py \
    tests/test_m09_context_propagation_e2e.py \
    tests/test_m09_maintenance_and_rules.py --no-cov -v
```

| Datei | Tests | Schwerpunkt |
|---|---|---|
| `tests/test_m09_metrics.py` | 13 | Registry vollständig, Idempotenz, SLO-Buckets, `observe_llm_call`, Job-Terminal, Sanitizer-Zähler, HTTP-Middleware inkl. Route-Template und Fehlerfälle, `/metrics` Auth |
| `tests/test_m09_tracing_context.py` | 9 | OTel-Gate, Interceptor-Auswahl, Provider-Installation, Header-Roundtrip, structlog-Bindung, JSON-Log enthält Korrelationsfelder |
| `tests/test_m09_context_propagation_e2e.py` | 1 | Echter Temporal-Testserver: request_id API-Client → Workflow → Activity (Marker `temporal`) |
| `tests/test_m09_maintenance_and_rules.py` | 9 | Schedule-Bootstrap idempotent/abschaltbar, Cleanup-Activity aktualisiert Metriken, Alert-Regeln wohlgeformt und referenzieren registrierte Metriken, Prometheus-Config, Dashboards valide und generator-identisch |

Gesamtsuite nach M09: alle Tests grün, Coverage-Gate ≥ 70 % erfüllt.

---

## 7. Manuelle Smoke-Prüfung

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
curl -sH "Authorization: Bearer $EKI_API_KEY" localhost:8000/metrics | grep -c '^eki_'   # > 20
docker compose exec worker python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:9090/').read()[:200])"
# Grafana: http://localhost:3000 -> Ordner eKI -> 3 Dashboards
# Prometheus-Alerts: docker compose exec prometheus promtool check rules /etc/prometheus/alerts.yml
# Temporal UI: Schedule "eki-kb-cleanup" sichtbar
```

---

## 8. Rollback

1. `OTEL_ENABLED=false` (Default) – kein Tracing, keine Exporter-Verbindungen.
2. `METRICS_ENABLED=false` – `/metrics` und Worker-Exporter aus; Zähler laufen im Prozess weiter, kosten aber nichts.
3. Overlay weglassen (`docker compose -f docker-compose.yml up -d`) – Kernsystem bytewise unverändert.
4. `KB_CLEANUP_ENABLED=false` – Schedule wird nicht angelegt (bestehender Schedule ggf. über Temporal UI pausieren).

---

**Status M09:** Implementierung abgeschlossen, abnahmebereit.
