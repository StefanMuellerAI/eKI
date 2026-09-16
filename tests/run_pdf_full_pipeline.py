#!/usr/bin/env python3
"""Send all PDF fixtures through the full async pipeline via the API, ONE AT A TIME.

Submits each PDF to POST /v1/security/check:async, waits for completion,
then submits the next. This avoids overwhelming the Ollama LLM.

Run:  python tests/run_pdf_full_pipeline.py
"""

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

SKIP_FILES = {"password_protected.pdf"}

POLL_INTERVAL = 15
POLL_TIMEOUT = 7200

RESET = "\033[0m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"


def fmt_size(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_048_576:.1f} MB"
    return f"{n / 1024:.0f} KB"


def fmt_time(s: float) -> str:
    if s >= 60:
        return f"{s / 60:.1f}m"
    return f"{s:.0f}s"


def submit_and_wait(pdf_path: Path) -> dict:
    """Submit a PDF and wait for the workflow to complete."""
    headers = {"Authorization": f"Bearer {API_KEY}"}
    result = {
        "file": pdf_path.name,
        "size": pdf_path.stat().st_size,
    }

    # Submit
    t0 = time.monotonic()
    try:
        with open(pdf_path, "rb") as f:
            files = {"file": (pdf_path.name, f, "application/pdf")}
            data = {
                "project_id": "bulk-test",
                "script_format": "pdf",
                "delivery": "pull",
            }
            resp = requests.post(
                f"{API_BASE}/v1/security/check:async",
                headers=headers,
                files=files,
                data=data,
                timeout=30,
            )
    except Exception as exc:
        result["status"] = "submit_error"
        result["error"] = str(exc)
        result["elapsed"] = time.monotonic() - t0
        return result

    if resp.status_code != 202:
        body = resp.json()
        result["status"] = "rejected"
        result["error"] = body.get("detail") or body.get("message", f"HTTP {resp.status_code}")
        result["elapsed"] = time.monotonic() - t0
        return result

    job_id = resp.json()["job_id"]
    result["job_id"] = job_id

    # Poll until done
    while True:
        elapsed = time.monotonic() - t0
        if elapsed > POLL_TIMEOUT:
            result["status"] = "timeout"
            result["elapsed"] = elapsed
            return result

        time.sleep(POLL_INTERVAL)

        try:
            poll = requests.get(
                f"{API_BASE}/v1/security/jobs/{job_id}",
                headers=headers,
                timeout=10,
            )
        except Exception:
            continue

        if poll.status_code != 200:
            continue

        data = poll.json()
        status = data.get("status", "unknown")

        if status == "completed":
            result["status"] = "completed"
            result["elapsed"] = time.monotonic() - t0
            result["report_id"] = data.get("report_id")
            return result

        if status == "failed":
            result["status"] = "failed"
            result["elapsed"] = time.monotonic() - t0
            result["error"] = data.get("error_message") or "unknown"
            return result


def main():
    pdfs = sorted(p for p in FIXTURES_DIR.glob("*.pdf") if p.name not in SKIP_FILES)

    if not pdfs:
        print(f"{RED}No PDFs found in {FIXTURES_DIR}{RESET}")
        sys.exit(1)

    print(f"\n{BOLD}{CYAN}{'=' * 90}{RESET}")
    print(
        f"{BOLD}{CYAN}  Full Pipeline Test -- {len(pdfs)} PDFs (sequential, one at a time){RESET}"
    )
    print(f"{BOLD}{CYAN}  Skipping: {SKIP_FILES}{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 90}{RESET}\n")

    results = []
    total_t0 = time.monotonic()

    for i, pdf_path in enumerate(pdfs, 1):
        tag = f"[{i:2d}/{len(pdfs)}]"
        size = fmt_size(pdf_path.stat().st_size)
        print(f"  {tag} {pdf_path.name[:60]} ({size})")
        print("       Submitting + waiting...", end=" ", flush=True)

        r = submit_and_wait(pdf_path)
        results.append(r)

        if r["status"] == "completed":
            print(
                f"{GREEN}COMPLETED{RESET} in {fmt_time(r['elapsed'])} (report={r.get('report_id', '?')[:8]}...)"
            )
        elif r["status"] == "failed":
            print(f"{RED}FAILED{RESET} in {fmt_time(r['elapsed'])} -- {r.get('error', '')[:60]}")
        elif r["status"] == "timeout":
            print(f"{YELLOW}TIMEOUT{RESET} after {fmt_time(r['elapsed'])}")
        elif r["status"] == "rejected":
            print(f"{RED}REJECTED{RESET} -- {r.get('error', '')[:60]}")
        else:
            print(f"{RED}{r['status']}{RESET} -- {r.get('error', '')[:60]}")

    total_elapsed = time.monotonic() - total_t0

    # Summary
    completed = [r for r in results if r["status"] == "completed"]
    failed = [r for r in results if r["status"] == "failed"]
    timeouts = [r for r in results if r["status"] == "timeout"]
    rejected = [r for r in results if r["status"] in ("rejected", "submit_error")]

    print(f"\n{BOLD}{CYAN}{'=' * 90}{RESET}")
    print(f"{BOLD}  SUMMARY  (total time: {fmt_time(total_elapsed)}){RESET}")
    print(f"{BOLD}{CYAN}{'=' * 90}{RESET}")
    print(f"  Total:     {len(results)}")
    print(f"  {GREEN}Completed: {len(completed)}{RESET}")
    print(f"  {RED}Failed:    {len(failed)}{RESET}")
    print(f"  {YELLOW}Timeout:   {len(timeouts)}{RESET}")
    print(f"  {RED}Rejected:  {len(rejected)}{RESET}")
    print()

    print(f"  {'File':<55s} {'Size':>7s} {'Status':<12s} {'Time':>7s}")
    print(f"  {'-' * 55} {'-' * 7} {'-' * 12} {'-' * 7}")

    for r in results:
        name = r["file"][:53]
        size = fmt_size(r["size"])
        elapsed = fmt_time(r.get("elapsed", 0))
        st = r["status"]
        if st == "completed":
            status_str = f"{GREEN}completed{RESET}"
        elif st == "failed":
            status_str = f"{RED}failed{RESET}"
        elif st == "timeout":
            status_str = f"{YELLOW}timeout{RESET}"
        else:
            status_str = f"{RED}{st}{RESET}"
        print(f"  {name:<55s} {size:>7s} {status_str:<22s} {elapsed:>7s}")

    if failed:
        print(f"\n{BOLD}{RED}  FAILURE DETAILS:{RESET}")
        for r in failed:
            print(f"    {RED}{r['file']}{RESET}: {r.get('error', '?')}")

    print()
    sys.exit(1 if (len(failed) + len(timeouts) + len(rejected)) > 0 else 0)


if __name__ == "__main__":
    main()
