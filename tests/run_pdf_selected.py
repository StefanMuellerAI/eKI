#!/usr/bin/env python3
"""Test selected representative PDFs through the full pipeline, one at a time."""

import os
import sys
import time
from pathlib import Path

import requests

API_BASE = os.environ.get("EKI_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("EKI_API_KEY", "")
if not API_KEY:
    sys.exit("ERROR: EKI_API_KEY Umgebungsvariable setzen (z.B. export EKI_API_KEY=eki_...)")
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pdf"

SELECTED = [
    "simple_screenplay.pdf",
    "no_structure_multi_page.pdf",
    "Gaïa_Scipt_French.pdf",
    "SINKENDE_SCHIFFE_190730.pdf",
]

POLL_INTERVAL = 10
POLL_TIMEOUT = 3600

R = "\033[0m"
G = "\033[92m"
E = "\033[91m"
Y = "\033[93m"
C = "\033[96m"
B = "\033[1m"


def submit_and_wait(pdf_path: Path) -> dict:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    t0 = time.monotonic()
    result = {"file": pdf_path.name, "size": pdf_path.stat().st_size}

    try:
        with open(pdf_path, "rb") as f:
            resp = requests.post(
                f"{API_BASE}/v1/security/check:async",
                headers=headers,
                files={"file": (pdf_path.name, f, "application/pdf")},
                data={"project_id": "selected-test", "script_format": "pdf", "delivery": "pull"},
                timeout=30,
            )
    except Exception as exc:
        result.update(status="submit_error", error=str(exc), elapsed=time.monotonic() - t0)
        return result

    if resp.status_code != 202:
        body = resp.json()
        result.update(
            status="rejected",
            error=body.get("detail", f"HTTP {resp.status_code}"),
            elapsed=time.monotonic() - t0,
        )
        return result

    job_id = resp.json()["job_id"]
    result["job_id"] = job_id
    print(f"    Job: {job_id}")

    last_print = time.monotonic()
    while True:
        elapsed = time.monotonic() - t0
        if elapsed > POLL_TIMEOUT:
            result.update(status="timeout", elapsed=elapsed)
            return result

        time.sleep(POLL_INTERVAL)

        try:
            poll = requests.get(
                f"{API_BASE}/v1/security/jobs/{job_id}", headers=headers, timeout=10
            )
        except Exception:
            continue

        if poll.status_code != 200:
            continue

        data = poll.json()
        status = data.get("status", "unknown")

        now = time.monotonic()
        if now - last_print > 30:
            print(f"    ... {status} ({elapsed:.0f}s)", flush=True)
            last_print = now

        if status == "completed":
            result.update(
                status="completed", elapsed=time.monotonic() - t0, report_id=data.get("report_id")
            )
            return result
        if status == "failed":
            result.update(
                status="failed", elapsed=time.monotonic() - t0, error=data.get("error_message", "?")
            )
            return result


def main():
    pdfs = [FIXTURES_DIR / name for name in SELECTED]
    for p in pdfs:
        if not p.exists():
            print(f"{E}Missing: {p}{R}")
            sys.exit(1)

    print(f"\n{B}{C}{'=' * 80}{R}")
    print(f"{B}{C}  Selected Pipeline Test -- {len(pdfs)} PDFs{R}")
    print(f"{B}{C}{'=' * 80}{R}\n")

    results = []
    total_t0 = time.monotonic()

    for i, pdf_path in enumerate(pdfs, 1):
        sz = pdf_path.stat().st_size
        print(f"  [{i}/{len(pdfs)}] {pdf_path.name} ({sz / 1024:.0f} KB)")
        print("    Submitting...", flush=True)

        r = submit_and_wait(pdf_path)
        results.append(r)

        if r["status"] == "completed":
            print(
                f"    {G}COMPLETED{R} in {r['elapsed']:.0f}s  report={r.get('report_id', '?')[:12]}"
            )
        elif r["status"] == "failed":
            print(f"    {E}FAILED{R} in {r['elapsed']:.0f}s  error={r.get('error', '?')[:80]}")
        else:
            print(f"    {Y}{r['status'].upper()}{R}  {r.get('error', '')[:80]}")
        print()

    total_elapsed = time.monotonic() - total_t0
    ok = sum(1 for r in results if r["status"] == "completed")
    fail = sum(1 for r in results if r["status"] != "completed")

    print(f"{B}{C}{'=' * 80}{R}")
    print(f"{B}  SUMMARY  ({total_elapsed:.0f}s total){R}")
    print(f"  {G}Completed: {ok}{R}  {E}Failed/Other: {fail}{R}")
    print(f"{B}{C}{'=' * 80}{R}\n")

    for r in results:
        st = f"{G}OK{R}" if r["status"] == "completed" else f"{E}{r['status']}{R}"
        print(f"  {r['file']:<55s} {st}")

    print()
    sys.exit(1 if fail > 0 else 0)


if __name__ == "__main__":
    main()
