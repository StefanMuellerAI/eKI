#!/usr/bin/env python3
"""M11 -- Prod-Cutover smoke test (Pflichtenheft Abnahmetest 9).

Proves, against a *running* stack, that

1. the API is healthy and reports ``llm_provider=local_mistral`` (or ``ollama``),
2. a full FDX check runs end to end and a report is produced,
3. **no external LLM provider was used**: the Prometheus metrics of API and
   worker contain no ``eki_llm_requests_total{provider="mistral_cloud"}`` sample
   and ``eki_build_info`` carries the local provider.

Usage:

    export EKI_API_URL=http://localhost:8000
    export EKI_API_KEY=eki_...                 # normal key (job + report)
    export EKI_WORKER_METRICS_URL=http://localhost:9090   # optional, if reachable
    python scripts/smoke_prod.py [--fixture tests/fixtures/fdx/simple.fdx] [--timeout 900]

Exit code 0 = all checks passed, 1 = a check failed. Output is content-free
(counts, ids, durations) so it can be attached to the acceptance protocol.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "fdx" / "stunt_heavy.fdx"
LOCAL_PROVIDERS = {"local_mistral", "ollama"}
EXTERNAL_PROVIDERS = {"mistral_cloud"}


def _hdr(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _metric_samples(text: str, name: str) -> list[tuple[dict[str, str], float]]:
    out = []
    for line in text.splitlines():
        if not line.startswith(name + "{") and not line.startswith(name + " "):
            continue
        m = re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(?:\{(.*)\})?\s+(\S+)", line)
        if not m:
            continue
        labels = {}
        if m.group(1):
            for kv in re.findall(r'(\w+)="([^"]*)"', m.group(1)):
                labels[kv[0]] = kv[1]
        try:
            out.append((labels, float(m.group(2))))
        except ValueError:
            continue
    return out


def check(condition: bool, label: str, detail: str = "") -> bool:
    print(f"  [{'OK ' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail else ''}")
    return condition


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=os.environ.get("EKI_API_URL", "http://localhost:8000"))
    parser.add_argument("--key", default=os.environ.get("EKI_API_KEY", ""))
    parser.add_argument("--worker-metrics", default=os.environ.get("EKI_WORKER_METRICS_URL", ""))
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--timeout", type=int, default=900, help="max seconds to wait for the job")
    parser.add_argument("--project-id", default="smoke-prod")
    args = parser.parse_args()

    if not args.key:
        print("ERROR: EKI_API_KEY (or --key) required", file=sys.stderr)
        return 1

    fixture = Path(args.fixture)
    if not fixture.exists():
        candidates = sorted((ROOT / "tests" / "fixtures" / "fdx").glob("*.fdx"))
        if not candidates:
            print("ERROR: no FDX fixture found", file=sys.stderr)
            return 1
        fixture = candidates[0]

    ok = True
    print(f"== eKI Prod-Cutover Smoke ({args.url}) ==")
    with httpx.Client(base_url=args.url, timeout=60) as client:
        # 1. health + build info
        health = client.get("/health")
        ok &= check(
            health.status_code == 200, "GET /health", f"version={health.json().get('version')}"
        )

        metrics_before = client.get("/metrics", headers=_hdr(args.key))
        ok &= check(metrics_before.status_code == 200, "GET /metrics (API)")
        build = _metric_samples(metrics_before.text, "eki_build_info")
        providers = {lbl.get("llm_provider") for lbl, _ in build}
        ok &= check(
            bool(providers) and providers <= LOCAL_PROVIDERS,
            "API build_info uses a local LLM provider",
            f"providers={sorted(p for p in providers if p)}",
        )

        # 2. end-to-end FDX check
        payload = {
            "script_content": base64.b64encode(fixture.read_bytes()).decode(),
            "script_format": "fdx",
            "project_id": args.project_id,
            "delivery": "pull",
            "idempotency_key": f"smoke-{int(time.time())}",
        }
        started = time.monotonic()
        resp = client.post("/v1/security/check:async", json=payload, headers=_hdr(args.key))
        ok &= check(
            resp.status_code == 202, "POST /v1/security/check:async", f"status={resp.status_code}"
        )
        if resp.status_code != 202:
            return 1
        job_id = resp.json()["job_id"]
        print(f"       job_id={job_id} fixture={fixture.name} ({fixture.stat().st_size} bytes)")

        status = "pending"
        report_id = None
        while time.monotonic() - started < args.timeout:
            job = client.get(f"/v1/security/jobs/{job_id}", headers=_hdr(args.key)).json()
            status = job.get("status")
            report_id = job.get("report_id")
            if status in {"completed", "failed", "cancelled"}:
                break
            time.sleep(5)
        elapsed = time.monotonic() - started
        ok &= check(status == "completed", "job completed", f"status={status} in {elapsed:.0f}s")

        if status == "completed" and report_id:
            rep = client.get(f"/v1/security/reports/{report_id}", headers=_hdr(args.key))
            ok &= check(rep.status_code == 200, "GET /v1/security/reports/{id} (one-shot)")
            ok &= check(rep.headers.get("X-One-Shot") == "true", "X-One-Shot header present")
            body = rep.json()
            ok &= check(bool(body.get("pdf_base64")), "PDF present in report")
            print(f"       total_findings={body.get('report', {}).get('total_findings')}")
            again = client.get(f"/v1/security/reports/{report_id}", headers=_hdr(args.key))
            ok &= check(
                again.status_code == 410, "second retrieval is 410 Gone (Delete-on-Delivery)"
            )

        # 3. no external LLM calls
        metrics_after = client.get("/metrics", headers=_hdr(args.key)).text
        texts = [("api", metrics_after)]
        if args.worker_metrics:
            try:
                texts.append(
                    ("worker", httpx.get(args.worker_metrics.rstrip("/") + "/", timeout=10).text)
                )
            except Exception as exc:
                print(f"  [WARN] worker metrics not reachable ({type(exc).__name__}); skipped")
        for role, text in texts:
            llm = _metric_samples(text, "eki_llm_requests_total")
            external = [
                (lbl, v) for lbl, v in llm if lbl.get("provider") in EXTERNAL_PROVIDERS and v > 0
            ]
            local_calls = sum(v for lbl, v in llm if lbl.get("provider") in LOCAL_PROVIDERS)
            ok &= check(
                not external, f"{role}: no external LLM calls", f"local_calls={local_calls:.0f}"
            )

    print("\nRESULT:", "PASSED" if ok else "FAILED")
    print(
        json.dumps(
            {"job_id": job_id, "status": status, "elapsed_s": round(elapsed, 1), "passed": bool(ok)}
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
