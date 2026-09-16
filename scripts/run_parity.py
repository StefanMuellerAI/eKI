#!/usr/bin/env python3
"""M11 -- Cloud <-> Local parity run over the golden scene set.

Examples:

    # Stage (cloud) vs. production (local mistral) vs. the gemma alternative
    export MISTRAL_API_KEY=...
    python scripts/run_parity.py --providers mistral_cloud,local_mistral,ollama:gemma4:e4b

    # Local only, 2 parallel calls, custom output dir
    python scripts/run_parity.py --providers local_mistral --concurrency 2 --out tests/reports

Provider spec: ``<provider>[:<model>]`` where provider is one of
``mistral_cloud``, ``local_mistral``, ``ollama``. The optional model overrides
the configured default (e.g. ``ollama:gemma4:e4b``, ``local_mistral:mistral-small3.2``).

Exit code 0 when every provider passes all thresholds, 1 otherwise.
Reports (JSON + Markdown) are written to ``--out`` (default ``tests/reports/``,
git-ignored). Copy the Markdown table into ``docs/M11_PARITY_REPORT.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.config import get_settings  # noqa: E402
from llm.factory import get_llm_provider  # noqa: E402
from services.parity import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    ParityRunner,
    load_golden_scenes,
    render_markdown,
    write_reports,
)

GOLDEN = ROOT / "tests" / "parity" / "golden_scenes.yaml"


def build_provider(spec: str):
    """Instantiate a provider from ``provider[:model]`` using the app settings."""
    provider_name, _, model = spec.partition(":")
    settings = get_settings().model_copy(update={"llm_provider": provider_name})
    if model:
        if provider_name == "mistral_cloud":
            settings = settings.model_copy(update={"mistral_model": model})
        elif provider_name == "local_mistral":
            settings = settings.model_copy(update={"local_mistral_model": model})
        else:
            settings = settings.model_copy(update={"ollama_model": model})
    return get_llm_provider(settings)


async def main_async(args: argparse.Namespace) -> int:
    scenes = load_golden_scenes(Path(args.golden))
    if args.limit:
        scenes = scenes[: args.limit]
    thresholds = dict(DEFAULT_THRESHOLDS)
    if args.min_recall is not None:
        thresholds["class_recall"] = args.min_recall

    reports = []
    for spec in [s.strip() for s in args.providers.split(",") if s.strip()]:
        provider = build_provider(spec)
        print(f"== {provider.provider_name} ({getattr(provider, 'model', '?')}) ==", flush=True)
        if not await provider.health_check():
            print("   health check FAILED -- skipping provider", flush=True)
            continue
        runner = ParityRunner(provider, concurrency=args.concurrency)
        report = await runner.run(scenes)
        reports.append(report)
        for key, value in report.metrics().items():
            print(f"   {key:28s} {value:.3f}")
        print(f"   passed: {report.passed(thresholds)}", flush=True)

    if not reports:
        print("no provider produced a report", file=sys.stderr)
        return 1

    json_path, md_path = write_reports(reports, Path(args.out), thresholds=thresholds)
    print(f"\nJSON: {json_path}\nMarkdown: {md_path}\n")
    print(render_markdown(reports, thresholds))
    return 0 if all(r.passed(thresholds) for r in reports) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--providers", required=True, help="comma-separated provider[:model] specs")
    parser.add_argument("--golden", default=str(GOLDEN), help="golden scene YAML")
    parser.add_argument("--out", default=str(ROOT / "tests" / "reports"), help="output directory")
    parser.add_argument(
        "--concurrency", type=int, default=1, help="parallel LLM calls per provider"
    )
    parser.add_argument("--limit", type=int, default=0, help="only the first N scenes (smoke)")
    parser.add_argument(
        "--min-recall", type=float, default=None, help="override class_recall threshold"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
