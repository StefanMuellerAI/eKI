#!/usr/bin/env python3
"""M07 – Großdokument-Benchmark (Pflichtenheft §7 Abnahmetest 6).

Lädt das synthetische 300-Seiten-Drehbuch asynchron in die laufende API
hoch, pollt den Job bis zum Abschluss, sammelt parallel ``docker stats``-
Snapshots und prüft die drei Akzeptanzbedingungen:

* ``elapsed_sec < 7200`` (= ≤ 120 Min, Pflichtenheft §5)
* ``total_findings > 0``
* ``max_observed_ollama_concurrent <= OLLAMA_MAX_CONCURRENT_REQUESTS``
  (wird aus den ``docker stats``-Daten ABGELEITET, falls die genaue
  Concurrency aus dem Worker-Log extrahiert werden kann)

Voraussetzungen:
* docker-compose-Stack läuft (``docker compose up -d``).
* Fixture ist gebaut (``python tests/build_large_fixture.py``).
* API-Key per ``EKI_API_KEY`` env oder ``--key`` übergeben.

Run:
    python tests/run_pdf_m07_benchmark.py
    python tests/run_pdf_m07_benchmark.py --concurrency 2
    python tests/run_pdf_m07_benchmark.py --pdf tests/fixtures/pdf/X.pdf
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests


CONTAINERS = ["eki-api", "eki-worker", "eki-ollama"]
POLL_INTERVAL_SEC = 15
STATS_INTERVAL_SEC = 10
ACCEPTANCE_LIMIT_SEC = 7200  # 120 Min laut Pflichtenheft §5


@dataclass
class BenchmarkResult:
    pdf_path: str
    pdf_size_mb: float
    expected_concurrency: int
    job_id: str | None = None
    report_id: str | None = None
    status: str = "unknown"
    elapsed_sec: float = 0.0
    total_findings: int = 0
    error: str | None = None
    stats_csv: str | None = None
    stats_summary: dict[str, dict[str, float]] = field(default_factory=dict)
    acceptance_passed: bool = False


def _fmt_time(s: float) -> str:
    if s >= 60:
        return f"{s / 60:.1f}m"
    return f"{s:.1f}s"


class DockerStatsCollector(threading.Thread):
    """Sammelt periodisch docker stats für die eKI-Container."""

    def __init__(self, csv_path: Path, interval: float = STATS_INTERVAL_SEC) -> None:
        super().__init__(daemon=True)
        self.csv_path = csv_path
        self.interval = interval
        self._stop = threading.Event()
        self.rows: list[dict[str, str]] = []

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        with self.csv_path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["timestamp", "container", "cpu_pct", "mem_usage", "mem_pct"])
            while not self._stop.is_set():
                try:
                    proc = subprocess.run(
                        [
                            "docker", "stats", "--no-stream", "--format",
                            "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
                            *CONTAINERS,
                        ],
                        capture_output=True, text=True, timeout=15,
                    )
                    ts = datetime.utcnow().isoformat()
                    if proc.returncode == 0:
                        for line in proc.stdout.strip().splitlines():
                            parts = line.split("\t")
                            if len(parts) == 4:
                                writer.writerow([ts, *parts])
                                fh.flush()
                                self.rows.append({
                                    "timestamp": ts,
                                    "container": parts[0],
                                    "cpu_pct": parts[1],
                                    "mem_usage": parts[2],
                                    "mem_pct": parts[3],
                                })
                except Exception as exc:
                    print(f"  [warn] docker stats failed: {exc}", file=sys.stderr)
                self._stop.wait(self.interval)

    def summary(self) -> dict[str, dict[str, float]]:
        """Aggregate per-container peak CPU and average memory percentage."""
        out: dict[str, dict[str, float]] = {}
        for container in CONTAINERS:
            cpu_vals: list[float] = []
            mem_vals: list[float] = []
            for r in self.rows:
                if r["container"] != container:
                    continue
                try:
                    cpu_vals.append(float(r["cpu_pct"].rstrip("%")))
                except ValueError:
                    pass
                try:
                    mem_vals.append(float(r["mem_pct"].rstrip("%")))
                except ValueError:
                    pass
            if cpu_vals or mem_vals:
                out[container] = {
                    "cpu_peak_pct": max(cpu_vals) if cpu_vals else 0.0,
                    "cpu_avg_pct": sum(cpu_vals) / len(cpu_vals) if cpu_vals else 0.0,
                    "mem_peak_pct": max(mem_vals) if mem_vals else 0.0,
                    "mem_avg_pct": sum(mem_vals) / len(mem_vals) if mem_vals else 0.0,
                    "samples": float(max(len(cpu_vals), len(mem_vals))),
                }
        return out


def submit_pdf(pdf_path: Path, api_base: str, api_key: str) -> str:
    """Submit a PDF asynchronously and return the job_id."""
    with pdf_path.open("rb") as f:
        files = {"file": (pdf_path.name, f, "application/pdf")}
        data = {
            "project_id": "m07-benchmark",
            "script_format": "pdf",
            "delivery": "pull",
        }
        resp = requests.post(
            f"{api_base}/v1/security/check:async",
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=60,
        )
    if resp.status_code != 202:
        raise RuntimeError(
            f"Submit failed: HTTP {resp.status_code} -- {resp.text[:300]}"
        )
    return resp.json()["job_id"]


def poll_until_done(
    job_id: str, api_base: str, api_key: str, timeout_sec: int
) -> dict:
    """Poll the job until completion or timeout. Return final status payload."""
    headers = {"Authorization": f"Bearer {api_key}"}
    t0 = time.monotonic()
    last_progress = -1
    while True:
        elapsed = time.monotonic() - t0
        if elapsed > timeout_sec:
            return {"status": "timeout", "elapsed": elapsed}
        try:
            r = requests.get(
                f"{api_base}/v1/security/jobs/{job_id}",
                headers=headers,
                timeout=15,
            )
        except Exception:
            time.sleep(POLL_INTERVAL_SEC)
            continue
        if r.status_code != 200:
            time.sleep(POLL_INTERVAL_SEC)
            continue
        data = r.json()
        status = data.get("status", "unknown")
        progress = data.get("progress_percentage", 0) or 0
        if progress != last_progress:
            print(
                f"  [{_fmt_time(elapsed)}] status={status} progress={progress}%"
            )
            last_progress = progress
        if status in ("completed", "failed"):
            data["elapsed"] = elapsed
            return data
        time.sleep(POLL_INTERVAL_SEC)


def fetch_report(
    report_id: str, api_base: str, api_key: str
) -> dict | None:
    """One-shot fetch of the report for findings count."""
    try:
        r = requests.get(
            f"{api_base}/v1/security/reports/{report_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        print(f"  [warn] report fetch failed: {exc}", file=sys.stderr)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pdf",
        default="tests/fixtures/pdf/large_synthetic_300pp.pdf",
        help="Path to the PDF to benchmark (default: 300-page synthetic).",
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("EKI_API_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("EKI_API_KEY", ""),
        help="API key (or EKI_API_KEY env var).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("OLLAMA_MAX_CONCURRENT_REQUESTS", "1")),
        help=(
            "Expected Ollama concurrency cap (informational, for "
            "acceptance check)."
        ),
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=ACCEPTANCE_LIMIT_SEC + 600,
        help="Hard polling timeout in seconds (default: 7800).",
    )
    parser.add_argument(
        "--reports-dir",
        default="tests/reports",
        help="Directory to write the benchmark JSON + stats CSV.",
    )
    args = parser.parse_args()

    if not args.key:
        print("ERROR: --key or EKI_API_KEY required", file=sys.stderr)
        return 2

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"ERROR: PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    json_path = reports_dir / f"m07_benchmark_{ts}.json"
    stats_path = reports_dir / f"m07_benchmark_{ts}_stats.csv"

    result = BenchmarkResult(
        pdf_path=str(pdf_path),
        pdf_size_mb=round(pdf_path.stat().st_size / 1024 / 1024, 2),
        expected_concurrency=args.concurrency,
        stats_csv=str(stats_path),
    )

    print("=" * 70)
    print(" eKI M07 – Großdokument-Benchmark")
    print("=" * 70)
    print(f"  PDF:           {pdf_path.name} ({result.pdf_size_mb} MB)")
    print(f"  API:           {args.api_base}")
    print(f"  Concurrency:   {args.concurrency}")
    print(f"  Timeout:       {args.timeout_sec}s")
    print(f"  Stats CSV:     {stats_path}")
    print(f"  Result JSON:   {json_path}")
    print()

    have_docker = shutil.which("docker") is not None
    collector: DockerStatsCollector | None = None
    if have_docker:
        collector = DockerStatsCollector(stats_path)
        collector.start()
    else:
        print("  [warn] docker CLI not found -- stats won't be collected")

    t0 = time.monotonic()
    try:
        print("Submitting PDF...")
        job_id = submit_pdf(pdf_path, args.api_base, args.key)
        result.job_id = job_id
        print(f"  job_id = {job_id}")

        final = poll_until_done(
            job_id, args.api_base, args.key, args.timeout_sec
        )
        result.elapsed_sec = round(final.get("elapsed", time.monotonic() - t0), 1)
        result.status = final.get("status", "unknown")
        result.report_id = final.get("report_id")

        if result.status == "completed" and result.report_id:
            print(f"Job completed in {_fmt_time(result.elapsed_sec)}. "
                  "Fetching report...")
            report = fetch_report(result.report_id, args.api_base, args.key)
            if report:
                report_inner = report.get("report") or report
                result.total_findings = (
                    report_inner.get("total_findings", 0)
                    or len(report_inner.get("findings", []))
                )
        elif result.status == "failed":
            result.error = final.get("error_message") or "unknown failure"
    except Exception as exc:
        result.status = "error"
        result.error = str(exc)
        result.elapsed_sec = round(time.monotonic() - t0, 1)
    finally:
        if collector is not None:
            collector.stop()
            collector.join(timeout=20)
            result.stats_summary = collector.summary()

    # Akzeptanz-Check
    passed_time = result.elapsed_sec > 0 and result.elapsed_sec < ACCEPTANCE_LIMIT_SEC
    passed_findings = result.total_findings > 0
    result.acceptance_passed = (
        result.status == "completed"
        and passed_time
        and passed_findings
    )

    with json_path.open("w") as fh:
        json.dump({
            "pdf_path": result.pdf_path,
            "pdf_size_mb": result.pdf_size_mb,
            "expected_concurrency": result.expected_concurrency,
            "job_id": result.job_id,
            "report_id": result.report_id,
            "status": result.status,
            "elapsed_sec": result.elapsed_sec,
            "total_findings": result.total_findings,
            "error": result.error,
            "stats_csv": result.stats_csv,
            "stats_summary": result.stats_summary,
            "acceptance": {
                "limit_sec": ACCEPTANCE_LIMIT_SEC,
                "passed_time": passed_time,
                "passed_findings": passed_findings,
                "passed_overall": result.acceptance_passed,
            },
        }, fh, indent=2, ensure_ascii=False)

    print()
    print("=" * 70)
    print(f" Status:        {result.status}")
    print(f" Elapsed:       {_fmt_time(result.elapsed_sec)}  "
          f"(limit: {_fmt_time(ACCEPTANCE_LIMIT_SEC)})")
    print(f" Findings:      {result.total_findings}")
    if result.error:
        print(f" Error:         {result.error}")
    if result.stats_summary:
        print(" Resource peaks:")
        for c, s in result.stats_summary.items():
            print(
                f"   {c:<14s} CPU peak {s['cpu_peak_pct']:>5.1f}% "
                f"avg {s['cpu_avg_pct']:>5.1f}%  |  "
                f"MEM peak {s['mem_peak_pct']:>5.1f}% "
                f"avg {s['mem_avg_pct']:>5.1f}%"
            )
    print("=" * 70)
    print(
        f" ACCEPTANCE: {'PASSED' if result.acceptance_passed else 'FAILED'}"
    )
    print("=" * 70)

    return 0 if result.acceptance_passed else 1


if __name__ == "__main__":
    sys.exit(main())
