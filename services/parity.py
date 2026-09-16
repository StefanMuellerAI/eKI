"""Cloud <-> Local LLM parity harness (M11).

Runs the *production* prompts (structuring + risk analysis) against one or
more providers on the golden scene set (``tests/parity/golden_scenes.yaml``)
and scores every provider on:

* ``schema_valid_rate``   -- share of structured responses that validate
                             against ``_RISK_SCHEMA`` / ``SCENE_SCHEMA``
* ``class_recall``        -- expected risk classes found / expected classes
* ``severity_agreement``  -- share of scenes whose max severity is within one
                             level of ``min_severity`` (and >= it)
* ``false_positive_rate`` -- benign scenes that produced high/critical findings
* ``location_type_acc``   -- structurer INT/EXT accuracy
* latency p50 / p95 per call

Thresholds (Pflichtenheft §8 "Paritaets-Tests" mitigation for Cloud->Lokal):
schema >= 0.98, recall >= 0.80, severity >= 0.75, false positives <= 0.10,
location accuracy >= 0.90. The harness has no LLM dependency of its own, so
it can be unit-tested with fake providers.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]

DEFAULT_THRESHOLDS: dict[str, float] = {
    "schema_valid_rate": 0.98,
    "class_recall": 0.80,
    "severity_agreement": 0.75,
    "false_positive_rate": 0.10,  # upper bound
    "location_type_accuracy": 0.90,
}


def _sev_index(name: str | None) -> int:
    try:
        return SEVERITY_ORDER.index((name or "info").lower())
    except ValueError:
        return 0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
    return float(ordered[k])


@dataclass
class GoldenScene:
    id: str
    heading: str
    location_type: str
    text: str
    expected_classes: list[str]
    min_severity: str = "info"
    negative: bool = False


def load_golden_scenes(path: Path) -> list[GoldenScene]:
    data = yaml.safe_load(path.read_text())
    scenes = []
    for raw in data.get("scenes", []):
        scenes.append(
            GoldenScene(
                id=str(raw["id"]),
                heading=str(raw["heading"]),
                location_type=str(raw.get("location_type", "UNKNOWN")).upper(),
                text=str(raw["text"]),
                expected_classes=[str(c).upper() for c in raw.get("expected_classes", [])],
                min_severity=str(raw.get("min_severity", "info")).lower(),
                negative=bool(raw.get("negative", False)),
            )
        )
    return scenes


@dataclass
class SceneResult:
    scene_id: str
    risk_schema_valid: bool
    found_classes: list[str]
    expected_classes: list[str]
    max_severity: str
    min_severity: str
    negative: bool
    structure_schema_valid: bool
    location_type_found: str
    location_type_expected: str
    risk_latency_s: float
    structure_latency_s: float
    error: str | None = None

    @property
    def recall(self) -> float | None:
        if not self.expected_classes:
            return None
        hits = len(set(self.expected_classes) & set(self.found_classes))
        return hits / len(self.expected_classes)

    @property
    def severity_ok(self) -> bool:
        got, want = _sev_index(self.max_severity), _sev_index(self.min_severity)
        if self.negative:
            return got <= _sev_index("medium")
        return got >= want - 1

    @property
    def false_positive(self) -> bool:
        return self.negative and _sev_index(self.max_severity) >= _sev_index("high")

    @property
    def location_ok(self) -> bool:
        if self.location_type_expected == "UNKNOWN":
            return True
        return self.location_type_found.upper() == self.location_type_expected


@dataclass
class ProviderReport:
    provider: str
    model: str
    scenes: list[SceneResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def metrics(self) -> dict[str, float]:
        n = len(self.scenes) or 1
        with_expectations = [s for s in self.scenes if s.recall is not None]
        negatives = [s for s in self.scenes if s.negative]
        risk_lat = [s.risk_latency_s for s in self.scenes if s.error is None]
        struct_lat = [s.structure_latency_s for s in self.scenes if s.error is None]
        return {
            "scenes": float(len(self.scenes)),
            "errors": float(sum(1 for s in self.scenes if s.error)),
            "schema_valid_rate": sum(
                1 for s in self.scenes if s.risk_schema_valid and s.structure_schema_valid
            )
            / n,
            "class_recall": (
                statistics.fmean(s.recall for s in with_expectations)  # type: ignore[misc]
                if with_expectations
                else 1.0
            ),
            "severity_agreement": sum(1 for s in self.scenes if s.severity_ok) / n,
            "false_positive_rate": (
                sum(1 for s in negatives if s.false_positive) / len(negatives) if negatives else 0.0
            ),
            "location_type_accuracy": sum(1 for s in self.scenes if s.location_ok) / n,
            "risk_latency_p50_s": _percentile(risk_lat, 50),
            "risk_latency_p95_s": _percentile(risk_lat, 95),
            "structure_latency_p50_s": _percentile(struct_lat, 50),
            "structure_latency_p95_s": _percentile(struct_lat, 95),
        }

    def verdict(self, thresholds: dict[str, float] | None = None) -> dict[str, bool]:
        t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        m = self.metrics()
        return {
            "schema_valid_rate": m["schema_valid_rate"] >= t["schema_valid_rate"],
            "class_recall": m["class_recall"] >= t["class_recall"],
            "severity_agreement": m["severity_agreement"] >= t["severity_agreement"],
            "false_positive_rate": m["false_positive_rate"] <= t["false_positive_rate"],
            "location_type_accuracy": m["location_type_accuracy"] >= t["location_type_accuracy"],
        }

    def passed(self, thresholds: dict[str, float] | None = None) -> bool:
        return all(self.verdict(thresholds).values())

    def to_dict(self, thresholds: dict[str, float] | None = None) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "started_at": self.started_at,
            "metrics": self.metrics(),
            "verdict": self.verdict(thresholds),
            "passed": self.passed(thresholds),
            "scenes": [
                {
                    **asdict(s),
                    "recall": s.recall,
                    "severity_ok": s.severity_ok,
                    "false_positive": s.false_positive,
                    "location_ok": s.location_ok,
                }
                for s in self.scenes
            ],
        }


class ParityRunner:
    """Runs the production prompts for one provider over the golden set."""

    def __init__(self, provider: Any, *, model: str | None = None, concurrency: int = 1) -> None:
        self.provider = provider
        self.model = model or getattr(provider, "model", "unknown")
        self.concurrency = max(1, concurrency)

    async def run(self, scenes: list[GoldenScene]) -> ProviderReport:
        report = ProviderReport(provider=self.provider.provider_name, model=self.model)
        sem = asyncio.Semaphore(self.concurrency)

        async def one(scene: GoldenScene) -> SceneResult:
            async with sem:
                return await self._run_scene(scene)

        report.scenes = list(await asyncio.gather(*(one(s) for s in scenes)))
        return report

    async def _run_scene(self, scene: GoldenScene) -> SceneResult:
        from llm.prompt_manager import get_prompt_manager
        from parsers.pdf_llm_structurer import SCENE_SCHEMA
        from services.taxonomy import get_taxonomy_manager
        from workflows.activities import _RISK_SCHEMA

        pm = get_prompt_manager()
        taxonomy = get_taxonomy_manager()
        risk_validator = Draft202012Validator(_RISK_SCHEMA)
        scene_validator = Draft202012Validator(SCENE_SCHEMA)

        error: str | None = None

        # --- structuring (PDF pipeline step) ---------------------------------
        structure_valid = False
        location_type_found = "UNKNOWN"
        t0 = time.perf_counter()
        try:
            sys_p, user_p = pm.get("pdf_structuring", "scene", scene_text=scene.text)
            structured = await self.provider.generate_structured(
                prompt=user_p, schema=SCENE_SCHEMA, system_prompt=sys_p, temperature=0.1
            )
            structure_valid = scene_validator.is_valid(structured)
            location_type_found = str(structured.get("location_type", "UNKNOWN")).upper()
        except Exception as exc:
            error = f"structure:{type(exc).__name__}"
        structure_latency = time.perf_counter() - t0

        # --- risk analysis ----------------------------------------------------
        risk_valid = False
        found_classes: list[str] = []
        max_sev = "info"
        t0 = time.perf_counter()
        try:
            sys_p, user_p = pm.get(
                "risk_analysis",
                "scene",
                scene_number=scene.id,
                location=scene.heading,
                location_type=scene.location_type,
                time_of_day="UNKNOWN",
                scene_text=scene.text,
                taxonomy_context=taxonomy.summary_for_prompt(),
                kb_context="(none)",
            )
            result = await self.provider.generate_structured(
                prompt=user_p, schema=_RISK_SCHEMA, system_prompt=sys_p, temperature=0.2
            )
            risk_valid = risk_validator.is_valid(result)
            for f in result.get("findings", []) or []:
                enriched = taxonomy.validate_finding(dict(f))
                cls = str(enriched.get("risk_class", "")).upper()
                if cls:
                    found_classes.append(cls)
                sev = str(enriched.get("severity") or enriched.get("risk_level") or "info")
                if _sev_index(sev) > _sev_index(max_sev):
                    max_sev = sev.lower()
        except Exception as exc:
            error = (error + ";" if error else "") + f"risk:{type(exc).__name__}"
        risk_latency = time.perf_counter() - t0

        return SceneResult(
            scene_id=scene.id,
            risk_schema_valid=risk_valid,
            found_classes=sorted(set(found_classes)),
            expected_classes=scene.expected_classes,
            max_severity=max_sev,
            min_severity=scene.min_severity,
            negative=scene.negative,
            structure_schema_valid=structure_valid,
            location_type_found=location_type_found,
            location_type_expected=scene.location_type,
            risk_latency_s=round(risk_latency, 3),
            structure_latency_s=round(structure_latency, 3),
            error=error,
        )


def render_markdown(
    reports: list[ProviderReport], thresholds: dict[str, float] | None = None
) -> str:
    """Side-by-side Markdown table for docs/M11_PARITY_REPORT.md."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    head = (
        "| Kennzahl | Schwelle | " + " | ".join(f"{r.provider} ({r.model})" for r in reports) + " |"
    )
    sep = "|---|---|" + "|".join("---" for _ in reports) + "|"
    rows = [head, sep]
    labels = [
        ("schema_valid_rate", ">= {:.2f}", "{:.2%}"),
        ("class_recall", ">= {:.2f}", "{:.2%}"),
        ("severity_agreement", ">= {:.2f}", "{:.2%}"),
        ("false_positive_rate", "<= {:.2f}", "{:.2%}"),
        ("location_type_accuracy", ">= {:.2f}", "{:.2%}"),
        ("risk_latency_p50_s", "-", "{:.1f} s"),
        ("risk_latency_p95_s", "-", "{:.1f} s"),
        ("structure_latency_p50_s", "-", "{:.1f} s"),
        ("errors", "-", "{:.0f}"),
    ]
    for key, thr_fmt, val_fmt in labels:
        thr = thr_fmt.format(t[key]) if key in t else thr_fmt
        cells = []
        for r in reports:
            m = r.metrics()
            mark = ""
            if key in t:
                mark = " ✅" if r.verdict(thresholds)[key] else " ❌"
            cells.append(val_fmt.format(m[key]) + mark)
        rows.append(f"| `{key}` | {thr} | " + " | ".join(cells) + " |")
    rows.append(
        "| **Gesamt** | alle | "
        + " | ".join(
            "**bestanden**" if r.passed(thresholds) else "**nicht bestanden**" for r in reports
        )
        + " |"
    )
    return "\n".join(rows)


def write_reports(
    reports: list[ProviderReport],
    out_dir: Path,
    *,
    thresholds: dict[str, float] | None = None,
    stamp: str | None = None,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"parity_{stamp}.json"
    md_path = out_dir / f"parity_{stamp}.md"
    json_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "thresholds": {**DEFAULT_THRESHOLDS, **(thresholds or {})},
                "providers": [r.to_dict(thresholds) for r in reports],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    md_path.write_text(render_markdown(reports, thresholds) + "\n")
    return json_path, md_path


__all__ = [
    "DEFAULT_THRESHOLDS",
    "GoldenScene",
    "ParityRunner",
    "ProviderReport",
    "SceneResult",
    "load_golden_scenes",
    "render_markdown",
    "write_reports",
]
