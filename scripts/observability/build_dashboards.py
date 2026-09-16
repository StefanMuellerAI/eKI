#!/usr/bin/env python3
"""Generate the provisioned Grafana dashboards (M09).

Run after changing a panel definition:

    python scripts/observability/build_dashboards.py

Writes JSON into docker/observability/grafana/dashboards/. Keeping the
definitions in Python avoids hand-editing 1000-line JSON files and keeps the
PromQL next to the metric names in core/metrics.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT_DIR = (
    Path(__file__).resolve().parents[2] / "docker" / "observability" / "grafana" / "dashboards"
)
DS = {"type": "prometheus", "uid": "eki-prometheus"}


def _target(expr: str, legend: str = "") -> dict[str, Any]:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": "A"}


def _panel(
    panel_id: int,
    title: str,
    targets: list[dict[str, Any]],
    *,
    kind: str = "timeseries",
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    unit: str | None = None,
    thresholds: list[tuple[str, float | None]] | None = None,
    description: str = "",
) -> dict[str, Any]:
    for i, t in enumerate(targets):
        t["refId"] = chr(ord("A") + i)
    field_config: dict[str, Any] = {"defaults": {}, "overrides": []}
    if unit:
        field_config["defaults"]["unit"] = unit
    if thresholds:
        field_config["defaults"]["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": c, "value": v} for c, v in thresholds],
        }
    return {
        "id": panel_id,
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": field_config,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}}
        if kind == "timeseries"
        else {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value"},
    }


def _dashboard(
    uid: str, title: str, panels: list[dict[str, Any]], tags: list[str]
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "tags": ["eki", *tags],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "30s",
        "templating": {"list": []},
        "panels": panels,
    }


def overview() -> dict[str, Any]:
    p = [
        _panel(
            1,
            "API up",
            [_target('up{job="eki-api"}', "api")],
            kind="stat",
            x=0,
            y=0,
            w=4,
            h=4,
            thresholds=[("red", None), ("green", 1)],
        ),
        _panel(
            2,
            "Worker up",
            [_target('up{job="eki-worker"}', "worker")],
            kind="stat",
            x=4,
            y=0,
            w=4,
            h=4,
            thresholds=[("red", None), ("green", 1)],
        ),
        _panel(
            3,
            "Jobs completed (24h)",
            [_target('sum(increase(eki_jobs_total{status="completed"}[24h]))')],
            kind="stat",
            x=8,
            y=0,
            w=4,
            h=4,
        ),
        _panel(
            4,
            "Jobs failed (24h)",
            [_target('sum(increase(eki_jobs_total{status=~"failed|delivery_failed"}[24h]))')],
            kind="stat",
            x=12,
            y=0,
            w=4,
            h=4,
            thresholds=[("green", None), ("red", 1)],
        ),
        _panel(
            5,
            "Dead letters (unacked)",
            [_target("eki_dead_letters_unacknowledged")],
            kind="stat",
            x=16,
            y=0,
            w=4,
            h=4,
            thresholds=[("green", None), ("orange", 1)],
        ),
        _panel(
            6,
            "Version",
            [_target('eki_build_info{role="api"}', "{{version}} / {{llm_provider}}")],
            kind="stat",
            x=20,
            y=0,
            w=4,
            h=4,
        ),
        _panel(
            10,
            "Job duration p50 / p95 vs SLO (s)",
            [
                _target(
                    "histogram_quantile(0.5, sum by (le) (rate(eki_job_duration_seconds_bucket[6h])))",
                    "p50",
                ),
                _target(
                    "histogram_quantile(0.95, sum by (le) (rate(eki_job_duration_seconds_bucket[6h])))",
                    "p95",
                ),
            ],
            x=0,
            y=4,
            unit="s",
            thresholds=[("green", None), ("orange", 3600), ("red", 7200)],
            description="Pflichtenheft §5: <=50 scenes 600s, 120 pages 3600s, 300-350 pages 7200s.",
        ),
        _panel(
            11,
            "Jobs by terminal status (rate/h)",
            [_target("sum by (status) (rate(eki_jobs_total[1h])) * 3600", "{{status}}")],
            x=12,
            y=4,
        ),
        _panel(
            12,
            "HTTP request rate by route",
            [_target("sum by (route) (rate(eki_http_requests_total[5m]))", "{{route}}")],
            x=0,
            y=12,
            unit="reqps",
        ),
        _panel(
            13,
            "HTTP p95 latency by route",
            [
                _target(
                    "histogram_quantile(0.95, sum by (le, route) (rate(eki_http_request_duration_seconds_bucket[5m])))",
                    "{{route}}",
                )
            ],
            x=12,
            y=12,
            unit="s",
        ),
        _panel(
            14,
            "HTTP 4xx / 5xx share",
            [
                _target(
                    'sum(rate(eki_http_requests_total{status=~"4.."}[5m])) / clamp_min(sum(rate(eki_http_requests_total[5m])), 1e-9)',
                    "4xx",
                ),
                _target(
                    'sum(rate(eki_http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(eki_http_requests_total[5m])), 1e-9)',
                    "5xx",
                ),
            ],
            x=0,
            y=20,
            unit="percentunit",
        ),
        _panel(
            15,
            "Scenes processed / findings (rate per h)",
            [
                _target(
                    "sum by (stage) (rate(eki_scenes_processed_total[1h])) * 3600",
                    "scenes {{stage}}",
                ),
                _target(
                    "sum by (severity) (rate(eki_findings_total[1h])) * 3600",
                    "findings {{severity}}",
                ),
            ],
            x=12,
            y=20,
        ),
    ]
    return _dashboard("eki-overview", "eKI Overview & SLOs", p, ["slo"])


def delivery() -> dict[str, Any]:
    p = [
        _panel(
            1,
            "Push success ratio (30m)",
            [
                _target(
                    'sum(rate(eki_delivery_attempts_total{mode="push",outcome="success"}[30m])) / clamp_min(sum(rate(eki_delivery_attempts_total{mode="push"}[30m])), 1e-9)'
                )
            ],
            kind="stat",
            x=0,
            y=0,
            w=6,
            h=4,
            unit="percentunit",
            thresholds=[("red", None), ("orange", 0.9), ("green", 0.99)],
        ),
        _panel(
            2,
            "Pull reports ready (24h)",
            [
                _target(
                    'sum(increase(eki_delivery_attempts_total{mode="pull",outcome="pull_ready"}[24h]))'
                )
            ],
            kind="stat",
            x=6,
            y=0,
            w=6,
            h=4,
        ),
        _panel(
            3,
            "One-shot retrievals (24h)",
            [
                _target(
                    "sum by (outcome) (increase(eki_report_retrievals_total[24h]))", "{{outcome}}"
                )
            ],
            kind="stat",
            x=12,
            y=0,
            w=6,
            h=4,
        ),
        _panel(
            4,
            "Dead letters (unacked)",
            [_target("eki_dead_letters_unacknowledged")],
            kind="stat",
            x=18,
            y=0,
            w=6,
            h=4,
            thresholds=[("green", None), ("orange", 1)],
        ),
        _panel(
            10,
            "Push attempts by outcome (rate/h)",
            [
                _target(
                    'sum by (outcome) (rate(eki_delivery_attempts_total{mode="push"}[1h])) * 3600',
                    "{{outcome}}",
                )
            ],
            x=0,
            y=4,
        ),
        _panel(
            11,
            "Push attempt latency p50 / p95",
            [
                _target(
                    'histogram_quantile(0.5, sum by (le) (rate(eki_delivery_duration_seconds_bucket{mode="push"}[1h])))',
                    "p50",
                ),
                _target(
                    'histogram_quantile(0.95, sum by (le) (rate(eki_delivery_duration_seconds_bucket{mode="push"}[1h])))',
                    "p95",
                ),
            ],
            x=12,
            y=4,
            unit="s",
        ),
        _panel(
            12,
            "Delivery failures by reason (24h)",
            [_target("sum by (reason) (increase(eki_delivery_failures_total[24h]))", "{{reason}}")],
            x=0,
            y=12,
        ),
        _panel(
            13,
            "Webhook outcomes (24h)",
            [_target("sum by (outcome) (increase(eki_webhook_sent_total[24h]))", "{{outcome}}")],
            x=12,
            y=12,
        ),
        _panel(
            14,
            "Buffer deletes by trigger (Delete-on-Delivery evidence)",
            [_target("sum by (source) (rate(eki_buffer_deletes_total[1h])) * 3600", "{{source}}")],
            x=0,
            y=20,
            w=24,
            description="Every successful push/pull must be followed by a buffer delete (Pflichtenheft Abnahmetests 2/3).",
        ),
    ]
    return _dashboard("eki-delivery", "eKI Delivery (Push/Pull, Retries, DLQ)", p, ["delivery"])


def llm() -> dict[str, Any]:
    p = [
        _panel(
            1,
            "LLM calls in flight",
            [_target("sum by (provider) (eki_llm_requests_in_flight)", "{{provider}}")],
            kind="stat",
            x=0,
            y=0,
            w=6,
            h=4,
        ),
        _panel(
            2,
            "LLM error ratio (15m)",
            [
                _target(
                    'sum(rate(eki_llm_requests_total{outcome="error"}[15m])) / clamp_min(sum(rate(eki_llm_requests_total[15m])), 1e-9)'
                )
            ],
            kind="stat",
            x=6,
            y=0,
            w=6,
            h=4,
            unit="percentunit",
            thresholds=[("green", None), ("orange", 0.05), ("red", 0.2)],
        ),
        _panel(
            3,
            "Sanitizer hits (24h)",
            [
                _target(
                    "sum by (action) (increase(eki_prompt_sanitizer_hits_total[24h]))", "{{action}}"
                )
            ],
            kind="stat",
            x=12,
            y=0,
            w=6,
            h=4,
        ),
        _panel(
            4,
            "KB documents / retrieval hits (24h)",
            [
                _target("eki_kb_documents", "documents"),
                _target("sum(increase(eki_kb_retrieval_hits_total[24h]))", "hits 24h"),
            ],
            kind="stat",
            x=18,
            y=0,
            w=6,
            h=4,
        ),
        _panel(
            10,
            "LLM latency p50 / p95 by operation",
            [
                _target(
                    "histogram_quantile(0.5, sum by (le, provider, operation) (rate(eki_llm_request_duration_seconds_bucket[15m])))",
                    "p50 {{provider}}/{{operation}}",
                ),
                _target(
                    "histogram_quantile(0.95, sum by (le, provider, operation) (rate(eki_llm_request_duration_seconds_bucket[15m])))",
                    "p95 {{provider}}/{{operation}}",
                ),
            ],
            x=0,
            y=4,
            unit="s",
        ),
        _panel(
            11,
            "Queue wait for Ollama slot p95",
            [
                _target(
                    "histogram_quantile(0.95, sum by (le, provider) (rate(eki_llm_queue_wait_seconds_bucket[15m])))",
                    "{{provider}}",
                )
            ],
            x=12,
            y=4,
            unit="s",
            description="High values mean OLLAMA_MAX_CONCURRENT_REQUESTS is the bottleneck (M07 tuning).",
        ),
        _panel(
            12,
            "LLM calls by provider/operation/outcome (rate/h)",
            [
                _target(
                    "sum by (provider, operation, outcome) (rate(eki_llm_requests_total[1h])) * 3600",
                    "{{provider}}/{{operation}}/{{outcome}}",
                )
            ],
            x=0,
            y=12,
            w=24,
        ),
    ]
    return _dashboard("eki-llm", "eKI LLM (Latency, Errors, Queue)", p, ["llm"])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, builder in (("eki_overview", overview), ("eki_delivery", delivery), ("eki_llm", llm)):
        path = OUT_DIR / f"{name}.json"
        path.write_text(json.dumps(builder(), indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path.relative_to(OUT_DIR.parents[3])}")


if __name__ == "__main__":
    main()
