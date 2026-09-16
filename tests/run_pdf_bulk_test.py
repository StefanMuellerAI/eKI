#!/usr/bin/env python3
"""Bulk-test all PDFs in the fixtures directory through the extraction + splitting pipeline.

Run:  python tests/run_pdf_bulk_test.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsers.pdf import extract_pdf_text
from parsers.pdf_scene_splitter import split_into_scenes

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pdf"

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
    if n >= 1_000:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def run_single(pdf_path: Path) -> dict:
    result = {
        "file": pdf_path.name,
        "size": pdf_path.stat().st_size,
        "status": "OK",
        "pages": 0,
        "ocr_pages": [],
        "markers_found": True,
        "page_fallback": False,
        "preamble": False,
        "scenes": 0,
        "warnings": [],
        "error": None,
        "elapsed": 0.0,
    }

    t0 = time.monotonic()
    try:
        content = pdf_path.read_bytes()
        full_text, page_texts, ocr_pages, warnings = extract_pdf_text(content)

        result["pages"] = len(page_texts)
        result["ocr_pages"] = ocr_pages
        result["warnings"] = warnings

        if not full_text.strip():
            result["status"] = "EMPTY"
            result["warnings"].append("No extractable text")
            result["elapsed"] = time.monotonic() - t0
            return result

        blocks = split_into_scenes(full_text, page_texts=page_texts)

        preambles = [b for b in blocks if b.is_preamble]
        scenes = [b for b in blocks if not b.is_preamble]

        result["preamble"] = len(preambles) > 0
        result["scenes"] = len(scenes)

        has_markers = (
            any(not b.heading_line.startswith("PAGE ") for b in scenes) if scenes else False
        )
        page_fallback = any(b.heading_line.startswith("PAGE ") for b in scenes)

        result["markers_found"] = has_markers
        result["page_fallback"] = page_fallback

        if page_fallback:
            result["warnings"].append("Page-by-page fallback active")

        if not scenes and not preambles:
            result["status"] = "EMPTY"
        elif not scenes:
            result["status"] = "PREAMBLE_ONLY"

    except Exception as exc:
        result["status"] = "ERROR"
        result["error"] = str(exc)

    result["elapsed"] = time.monotonic() - t0
    return result


def main():
    pdfs = sorted(FIXTURES_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"{RED}No PDFs found in {FIXTURES_DIR}{RESET}")
        sys.exit(1)

    print(f"\n{BOLD}{CYAN}{'=' * 90}{RESET}")
    print(f"{BOLD}{CYAN}  PDF Bulk Processing Test -- {len(pdfs)} files{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 90}{RESET}\n")

    results = []
    ok_count = 0
    warn_count = 0
    error_count = 0

    for i, pdf_path in enumerate(pdfs, 1):
        tag = f"[{i:2d}/{len(pdfs)}]"
        print(
            f"{DIM}{tag} Processing: {pdf_path.name} ({fmt_size(pdf_path.stat().st_size)})...{RESET}",
            end=" ",
            flush=True,
        )

        r = run_single(pdf_path)
        results.append(r)

        if r["status"] == "ERROR":
            error_count += 1
            print(f"{RED}ERROR{RESET} -- {r['error']}")
        elif r["status"] == "EMPTY":
            warn_count += 1
            print(f"{YELLOW}EMPTY{RESET} (no text)")
        elif r["status"] == "PREAMBLE_ONLY":
            warn_count += 1
            print(f"{YELLOW}PREAMBLE ONLY{RESET} ({r['pages']} pages, 0 scenes)")
        else:
            ok_count += 1
            fallback_tag = f" {YELLOW}[PAGE FALLBACK]{RESET}" if r["page_fallback"] else ""
            marker_tag = f" {CYAN}[MARKERS]{RESET}" if r["markers_found"] else ""
            print(
                f"{GREEN}OK{RESET} -- "
                f"{r['pages']} pages, {r['scenes']} scenes{marker_tag}{fallback_tag} "
                f"({r['elapsed']:.2f}s)"
            )

        if r["ocr_pages"]:
            print(f"         {YELLOW}OCR needed: pages {r['ocr_pages']}{RESET}")
        for w in r["warnings"]:
            if w and "OCR" not in w and "Page-by-page" not in w:
                print(f"         {YELLOW}Warning: {w}{RESET}")

    # Summary
    print(f"\n{BOLD}{CYAN}{'=' * 90}{RESET}")
    print(f"{BOLD}  SUMMARY{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 90}{RESET}")
    print(f"  Total files:      {len(results)}")
    print(f"  {GREEN}OK:               {ok_count}{RESET}")
    print(f"  {YELLOW}Warnings:         {warn_count}{RESET}")
    print(f"  {RED}Errors:           {error_count}{RESET}")
    print()

    # Detail table
    print(f"  {'File':<60s} {'Size':>8s} {'Pages':>5s} {'Scenes':>6s} {'Mode':<15s} {'Time':>6s}")
    print(f"  {'-' * 60} {'-' * 8} {'-' * 5} {'-' * 6} {'-' * 15} {'-' * 6}")

    for r in results:
        name = r["file"][:58]
        if r["status"] == "ERROR":
            mode = f"{RED}ERROR{RESET}"
        elif r["page_fallback"]:
            mode = f"{YELLOW}page-fallback{RESET}"
        elif r["markers_found"]:
            mode = f"{GREEN}markers{RESET}"
        elif r["status"] == "PREAMBLE_ONLY":
            mode = f"{YELLOW}preamble-only{RESET}"
        else:
            mode = f"{DIM}none{RESET}"

        print(
            f"  {name:<60s} {fmt_size(r['size']):>8s} {r['pages']:>5d} {r['scenes']:>6d} {mode:<25s} {r['elapsed']:>5.2f}s"
        )

    print()

    # Error details
    errors = [r for r in results if r["status"] == "ERROR"]
    if errors:
        print(f"{BOLD}{RED}  ERROR DETAILS:{RESET}")
        for r in errors:
            print(f"  {RED}{r['file']}{RESET}")
            print(f"    {r['error']}")
        print()

    sys.exit(1 if error_count > 0 else 0)


if __name__ == "__main__":
    main()
