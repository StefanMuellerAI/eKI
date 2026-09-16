#!/usr/bin/env python3
"""eKI Security Check -- Standalone-Script fuer Kunden.

Schickt ein PDF-Drehbuch an die eKI API, wartet auf das Ergebnis
und speichert den Sicherheitsreport als PDF.

Voraussetzungen:
    pip install requests

Nutzung:
    python run_security_check.py mein_drehbuch.pdf
    python run_security_check.py mein_drehbuch.pdf --url https://eki.example.com --key eki_abc123
    python run_security_check.py mein_drehbuch.pdf --output report.pdf
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Fehler: 'requests' ist nicht installiert.")
    print("Bitte installieren mit:  pip install requests")
    sys.exit(1)


DEFAULT_API_URL = "http://localhost:8000"
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 3600
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="eKI Security Check -- Drehbuch-Sicherheitspruefung",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Beispiele:\n"
            "  python run_security_check.py mein_drehbuch.pdf\n"
            "  python run_security_check.py skript.pdf --key eki_abc123\n"
            "  python run_security_check.py skript.pdf --output bericht.pdf --json bericht.json\n"
        ),
    )
    parser.add_argument(
        "pdf_file",
        type=Path,
        help="Pfad zur PDF-Datei des Drehbuchs",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("EKI_API_URL", DEFAULT_API_URL),
        help=f"API Base-URL (default: {DEFAULT_API_URL}, oder EKI_API_URL env)",
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("EKI_API_KEY", ""),
        help="API Key (oder EKI_API_KEY env)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Ausgabepfad fuer den PDF-Report (default: <drehbuch>_report.pdf)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        dest="json_output",
        help="Ausgabepfad fuer den JSON-Report (optional)",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Projekt-ID (default: Dateiname ohne Endung)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f"Sekunden zwischen Status-Abfragen (default: {POLL_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=POLL_TIMEOUT_SECONDS,
        help=f"Maximale Wartezeit in Sekunden (default: {POLL_TIMEOUT_SECONDS})",
    )
    return parser.parse_args()


def print_step(step: int, total: int, msg: str) -> None:
    print(f"\n[{step}/{total}] {msg}")
    print("-" * 60)


def print_progress(msg: str) -> None:
    print(f"  {msg}")


def validate_inputs(args: argparse.Namespace) -> None:
    if not args.pdf_file.exists():
        print(f"Fehler: Datei nicht gefunden: {args.pdf_file}")
        sys.exit(1)

    if not args.pdf_file.suffix.lower() == ".pdf":
        print(f"Fehler: Nur PDF-Dateien werden unterstuetzt (erhalten: {args.pdf_file.suffix})")
        sys.exit(1)

    size = args.pdf_file.stat().st_size
    if size > MAX_FILE_SIZE:
        print(
            f"Fehler: Datei zu gross ({size / 1024 / 1024:.1f} MB, max {MAX_FILE_SIZE // 1024 // 1024} MB)"
        )
        sys.exit(1)

    if size == 0:
        print("Fehler: Datei ist leer")
        sys.exit(1)

    if not args.key:
        print("Fehler: Kein API-Key angegeben.")
        print("  Entweder per --key Parameter oder EKI_API_KEY Umgebungsvariable setzen.")
        sys.exit(1)


def submit_pdf(args: argparse.Namespace) -> str:
    """Schickt die PDF an die API und gibt die Job-ID zurueck."""
    project_id = args.project_id or args.pdf_file.stem.replace(" ", "-")[:100]
    headers = {"Authorization": f"Bearer {args.key}"}

    print_progress(f"Datei:      {args.pdf_file.name}")
    print_progress(f"Groesse:    {args.pdf_file.stat().st_size / 1024:.0f} KB")
    print_progress(f"Projekt-ID: {project_id}")
    print_progress(f"API:        {args.url}")
    print_progress("")

    with open(args.pdf_file, "rb") as f:
        resp = requests.post(
            f"{args.url}/v1/security/check:async",
            headers=headers,
            files={"file": (args.pdf_file.name, f, "application/pdf")},
            data={
                "project_id": project_id,
                "script_format": "pdf",
                "delivery": "pull",
            },
            timeout=30,
        )

    if resp.status_code == 401:
        print("Fehler: API-Key ungueltig oder abgelaufen.")
        sys.exit(1)

    if resp.status_code == 422:
        detail = resp.json().get("detail", resp.text)
        print(f"Fehler: Validierung fehlgeschlagen -- {detail}")
        sys.exit(1)

    if resp.status_code != 202:
        print(f"Fehler: Unerwarteter Status {resp.status_code}")
        try:
            print(f"  {resp.json()}")
        except Exception:
            print(f"  {resp.text[:500]}")
        sys.exit(1)

    body = resp.json()
    job_id = body["job_id"]
    print_progress(f"Job gestartet: {job_id}")
    return job_id


def poll_until_complete(args: argparse.Namespace, job_id: str) -> str:
    """Pollt den Job-Status und gibt die Report-ID zurueck."""
    headers = {"Authorization": f"Bearer {args.key}"}
    t0 = time.monotonic()
    poll_count = 0

    while True:
        elapsed = time.monotonic() - t0
        if elapsed > args.timeout:
            print(f"\nFehler: Timeout nach {args.timeout}s. Der Job laeuft moeglicherweise noch.")
            print(f"  Job-ID: {job_id}")
            print(f"  Status manuell pruefen: GET {args.url}/v1/security/jobs/{job_id}")
            sys.exit(1)

        time.sleep(args.poll_interval)
        poll_count += 1

        try:
            resp = requests.get(
                f"{args.url}/v1/security/jobs/{job_id}",
                headers=headers,
                timeout=10,
            )
        except requests.RequestException as exc:
            print_progress(f"Verbindungsfehler (wird erneut versucht): {exc}")
            continue

        if resp.status_code != 200:
            print_progress(
                f"Status-Abfrage fehlgeschlagen: HTTP {resp.status_code} (wird erneut versucht)"
            )
            continue

        data = resp.json()
        status = data.get("status", "unknown")
        progress = data.get("progress_percentage") or 0
        minutes = int(elapsed) // 60
        seconds = int(elapsed) % 60

        progress_bar = f"[{'#' * (progress // 5)}{'.' * (20 - progress // 5)}]"
        print(
            f"\r  {progress_bar} {progress:3d}% | Status: {status:<10s} | "
            f"Zeit: {minutes:02d}:{seconds:02d} | Poll #{poll_count}",
            end="",
            flush=True,
        )

        if status == "completed":
            print()
            report_id = data.get("report_id")
            if not report_id:
                print("Fehler: Job abgeschlossen, aber keine Report-ID erhalten.")
                sys.exit(1)
            print_progress(f"Abgeschlossen in {minutes}m {seconds}s")
            print_progress(f"Report-ID: {report_id}")
            return report_id

        if status == "failed":
            print()
            error_msg = data.get("error_message", "Unbekannter Fehler")
            print(f"Fehler: Job fehlgeschlagen -- {error_msg}")
            sys.exit(1)


def fetch_report(args: argparse.Namespace, report_id: str) -> dict:
    """Holt den Report ab (One-Shot) und gibt die JSON-Daten zurueck."""
    headers = {"Authorization": f"Bearer {args.key}"}

    resp = requests.get(
        f"{args.url}/v1/security/reports/{report_id}",
        headers=headers,
        timeout=30,
    )

    if resp.status_code == 410:
        print("Fehler: Report wurde bereits abgerufen (One-Shot-URL bereits verwendet).")
        sys.exit(1)

    if resp.status_code == 404:
        print("Fehler: Report nicht gefunden oder kein Zugriff.")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Fehler: Unerwarteter Status {resp.status_code}")
        sys.exit(1)

    return resp.json()


def save_outputs(args: argparse.Namespace, report_data: dict) -> None:
    """Speichert PDF und optional JSON."""
    report = report_data.get("report", {})
    pdf_b64 = report_data.get("pdf_base64")

    # Report-Zusammenfassung anzeigen
    total_findings = report.get("total_findings", 0)
    risk_summary = report.get("risk_summary", {})
    findings = report.get("findings", [])

    print_progress(f"Findings gesamt: {total_findings}")
    if risk_summary:
        parts = [f"{k}: {v}" for k, v in risk_summary.items() if v > 0]
        if parts:
            print_progress(f"Risiko-Verteilung: {', '.join(parts)}")

    # Top-Findings anzeigen
    critical_high = [f for f in findings if f.get("risk_level") in ("critical", "high")]
    if critical_high:
        print_progress("")
        print_progress(f"Kritische/Hohe Risiken ({len(critical_high)}):")
        for finding in critical_high[:5]:
            scene = finding.get("scene_number", "?")
            level = finding.get("risk_level", "?").upper()
            desc = finding.get("description", "")[:80]
            print_progress(f"  [{level}] Szene {scene}: {desc}")
        if len(critical_high) > 5:
            print_progress(f"  ... und {len(critical_high) - 5} weitere")

    # PDF speichern
    if pdf_b64:
        output_path = args.output or args.pdf_file.with_name(f"{args.pdf_file.stem}_report.pdf")
        pdf_bytes = base64.b64decode(pdf_b64)
        output_path.write_bytes(pdf_bytes)
        print_progress("")
        print_progress(f"PDF-Report gespeichert: {output_path}")
        print_progress(f"PDF-Groesse: {len(pdf_bytes) / 1024:.0f} KB")
    else:
        print_progress("")
        print_progress("Hinweis: Kein PDF im Report enthalten.")

    # JSON speichern (optional)
    if args.json_output:
        json_data = {
            "report": report,
            "metadata": {
                "source_file": args.pdf_file.name,
                "api_url": args.url,
            },
        }
        args.json_output.write_text(
            json.dumps(json_data, indent=2, ensure_ascii=False, default=str)
        )
        print_progress(f"JSON-Report gespeichert: {args.json_output}")


def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("  eKI Security Check -- Drehbuch-Sicherheitspruefung")
    print("=" * 60)

    validate_inputs(args)

    print_step(1, 3, "Drehbuch einreichen")
    job_id = submit_pdf(args)

    print_step(2, 3, "Auf Ergebnis warten")
    report_id = poll_until_complete(args, job_id)

    print_step(3, 3, "Report abrufen und speichern")
    report_data = fetch_report(args, report_id)
    save_outputs(args, report_data)

    print()
    print("=" * 60)
    print("  Fertig!")
    print("=" * 60)


if __name__ == "__main__":
    main()
