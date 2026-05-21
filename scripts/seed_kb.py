#!/usr/bin/env python3
"""KB seed CLI (M06).

Loads placeholder documents (Bernd-Stand-in) or real safety-officer
documents into the eKI Knowledge Base.  All operations go through the
public ``/v1/kb/documents`` API endpoint -- there is no test backdoor.

The CLI is idempotent: re-uploading the same content (matched by
SHA-256) returns 409 from the API and is treated as a no-op skip.

Usage:
    python scripts/seed_kb.py --seed-placeholders
    python scripts/seed_kb.py --add path/to/doc.pdf --title "Stunt FABW 2025" --tags stunt,official
    python scripts/seed_kb.py --wipe-placeholders
    python scripts/seed_kb.py --reseed
    python scripts/seed_kb.py --status

Environment:
    EKI_API_URL   default http://localhost:8000
    EKI_API_KEY   required (admin API key from scripts/create_api_key.py)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' is not installed. Run: pip install requests")
    sys.exit(1)

DEFAULT_API_URL = "http://localhost:8000"
PLACEHOLDER_TAG = "placeholder"
SEED_DIR = Path(__file__).resolve().parent.parent / "config" / "kb_seed"
PLACEHOLDER_DIR = SEED_DIR / "placeholders"
REAL_DIR = SEED_DIR / "real"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Extract a YAML-ish front-matter block. Minimal parser, no PyYAML dep.

    Supports keys: title (str), source (str), tags (list of str), ttl_hours (int).
    Returns (meta, body_without_frontmatter).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    block = match.group(1)
    meta: dict[str, object] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            items = [p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()]
            meta[key] = items
        elif value.startswith('"') and value.endswith('"'):
            meta[key] = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            meta[key] = value[1:-1]
        elif value.isdigit():
            meta[key] = int(value)
        else:
            meta[key] = value
    body = text[match.end():]
    return meta, body


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _upload(
    api_url: str,
    api_key: str,
    *,
    filename: str,
    file_bytes: bytes,
    title: str,
    source: str,
    tags: list[str],
    ttl_hours: int,
) -> tuple[bool, str]:
    """Upload one file. Returns (ok, message)."""
    files = {"file": (filename, file_bytes, "text/markdown")}
    data = {
        "title": title,
        "source": source,
        "tags": ",".join(tags),
        "ttl_hours": str(ttl_hours),
    }
    try:
        resp = requests.post(
            f"{api_url}/v1/kb/documents",
            headers=_headers(api_key),
            files=files,
            data=data,
            timeout=120,
        )
    except requests.RequestException as exc:
        return False, f"request error: {exc}"

    if resp.status_code == 201:
        try:
            return True, f"created {resp.json().get('doc_id', '?')}"
        except Exception:
            return True, "created"
    if resp.status_code == 409:
        return True, "skip (already in KB)"
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def cmd_seed_placeholders(api_url: str, api_key: str) -> int:
    if not PLACEHOLDER_DIR.exists():
        print(f"Error: {PLACEHOLDER_DIR} not found")
        return 2
    files = sorted(p for p in PLACEHOLDER_DIR.iterdir() if p.suffix.lower() in (".md", ".markdown", ".txt"))
    if not files:
        print(f"No placeholder files found in {PLACEHOLDER_DIR}")
        return 1

    print(f"Seeding {len(files)} placeholder document(s) from {PLACEHOLDER_DIR}")
    created = 0
    skipped = 0
    failed = 0
    for path in files:
        raw = path.read_text(encoding="utf-8")
        meta, _ = _parse_frontmatter(raw)
        title = str(meta.get("title") or path.stem)
        tags = list(meta.get("tags") or [])
        if PLACEHOLDER_TAG not in tags:
            tags.append(PLACEHOLDER_TAG)
        ttl_hours = int(meta.get("ttl_hours") or 8760)
        ok, msg = _upload(
            api_url, api_key,
            filename=path.name,
            file_bytes=raw.encode("utf-8"),
            title=title,
            source="PLACEHOLDER",
            tags=tags,
            ttl_hours=ttl_hours,
        )
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {path.name:<60s} -> {msg}")
        if not ok:
            failed += 1
        elif "skip" in msg:
            skipped += 1
        else:
            created += 1

    print(f"\nSummary: {created} created, {skipped} skipped, {failed} failed")
    return 0 if failed == 0 else 1


def cmd_add(
    api_url: str,
    api_key: str,
    *,
    path: Path,
    title: str,
    tags: list[str],
    ttl_hours: int,
    source: str,
) -> int:
    if not path.exists():
        print(f"Error: {path} not found")
        return 2
    raw = path.read_bytes()
    ok, msg = _upload(
        api_url, api_key,
        filename=path.name,
        file_bytes=raw,
        title=title,
        source=source,
        tags=tags,
        ttl_hours=ttl_hours,
    )
    status = "OK" if ok else "FAIL"
    print(f"[{status}] {path.name} -> {msg}")
    return 0 if ok else 1


def cmd_wipe_placeholders(api_url: str, api_key: str) -> int:
    try:
        resp = requests.delete(
            f"{api_url}/v1/kb/documents",
            headers=_headers(api_key),
            params={"tag": PLACEHOLDER_TAG},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"Error: {exc}")
        return 1
    if resp.status_code != 200:
        print(f"Error: HTTP {resp.status_code}: {resp.text[:200]}")
        return 1
    body = resp.json()
    print(f"Wiped {body.get('count', 0)} placeholder document(s)")
    return 0


def cmd_reseed(api_url: str, api_key: str) -> int:
    """Wipe placeholders, then ingest every file in real/."""
    rc = cmd_wipe_placeholders(api_url, api_key)
    if rc != 0:
        return rc
    if not REAL_DIR.exists():
        print(f"No {REAL_DIR}/ directory found; nothing to ingest")
        return 0
    files = sorted(
        p for p in REAL_DIR.iterdir()
        if p.suffix.lower() in (".pdf", ".md", ".markdown", ".txt")
    )
    if not files:
        print(f"No real documents found in {REAL_DIR}/")
        return 0
    print(f"Ingesting {len(files)} real document(s) from {REAL_DIR}")
    failed = 0
    for path in files:
        raw = path.read_bytes()
        title = path.stem.replace("_", " ").replace("-", " ")
        ok, msg = _upload(
            api_url, api_key,
            filename=path.name,
            file_bytes=raw,
            title=title[:255],
            source="UPLOAD",
            tags=["official"],
            ttl_hours=8760,
        )
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {path.name:<60s} -> {msg}")
        if not ok:
            failed += 1
    return 0 if failed == 0 else 1


def cmd_status(api_url: str, api_key: str) -> int:
    try:
        resp = requests.get(
            f"{api_url}/v1/kb/documents",
            headers=_headers(api_key),
            params={"limit": 500},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"Error: {exc}")
        return 1
    if resp.status_code != 200:
        print(f"Error: HTTP {resp.status_code}: {resp.text[:200]}")
        return 1
    body = resp.json()
    docs = body.get("documents", [])
    placeholders = [d for d in docs if PLACEHOLDER_TAG in (d.get("tags") or [])]
    real = [d for d in docs if PLACEHOLDER_TAG not in (d.get("tags") or [])]
    total_chunks = sum(int(d.get("chunk_count") or 0) for d in docs)
    print(f"KB Status @ {api_url}")
    print(f"  Total documents:  {len(docs)}")
    print(f"  Placeholder docs: {len(placeholders)}")
    print(f"  Real docs:        {len(real)}")
    print(f"  Total chunks:     {total_chunks}")
    if placeholders:
        print("\n  Placeholders:")
        for d in placeholders:
            print(f"    - {d['title']} ({d['chunk_count']} chunks, tags={d.get('tags')})")
    if real:
        print("\n  Real:")
        for d in real:
            print(f"    - {d['title']} ({d['chunk_count']} chunks, tags={d.get('tags')})")
    return 0


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="eKI Knowledge Base seeder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("EKI_API_URL", DEFAULT_API_URL),
        help=f"API base URL (default: {DEFAULT_API_URL}, or EKI_API_URL env)",
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("EKI_API_KEY", ""),
        help="API key (or EKI_API_KEY env)",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--seed-placeholders",
        action="store_true",
        help="Ingest all placeholder documents (Bernd-Stand-in).",
    )
    group.add_argument(
        "--add",
        metavar="FILE",
        help="Ingest a single document file (use with --title / --tags / --ttl-hours).",
    )
    group.add_argument(
        "--wipe-placeholders",
        action="store_true",
        help="Delete all documents tagged 'placeholder'. Real docs are untouched.",
    )
    group.add_argument(
        "--reseed",
        action="store_true",
        help="Wipe placeholders, then ingest config/kb_seed/real/*.",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="Print current KB contents grouped by placeholder/real.",
    )

    parser.add_argument("--title", default=None, help="Title for --add")
    parser.add_argument(
        "--tags",
        default="official",
        help="Comma-separated tags for --add (default: official)",
    )
    parser.add_argument(
        "--ttl-hours",
        type=int,
        default=8760,
        help="TTL in hours for --add (default: 8760 = 1 year)",
    )
    parser.add_argument(
        "--source",
        default="UPLOAD",
        help="Source field for --add (UPLOAD | SHARE | URL)",
    )
    return parser


def main() -> int:
    args = _make_parser().parse_args()
    if not args.key:
        print("Error: --key or EKI_API_KEY required")
        return 2

    if args.seed_placeholders:
        return cmd_seed_placeholders(args.url, args.key)
    if args.wipe_placeholders:
        return cmd_wipe_placeholders(args.url, args.key)
    if args.reseed:
        return cmd_reseed(args.url, args.key)
    if args.status:
        return cmd_status(args.url, args.key)
    if args.add:
        if not args.title:
            print("Error: --title is required with --add")
            return 2
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        return cmd_add(
            args.url, args.key,
            path=Path(args.add),
            title=args.title,
            tags=tags,
            ttl_hours=args.ttl_hours,
            source=args.source,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
