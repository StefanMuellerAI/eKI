"""M11 -- parity harness math and reporting with deterministic fake providers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services.parity import (
    DEFAULT_THRESHOLDS,
    GoldenScene,
    ParityRunner,
    ProviderReport,
    load_golden_scenes,
    render_markdown,
    write_reports,
)

GOLDEN = Path(__file__).parent / "parity" / "golden_scenes.yaml"


class FakeProvider:
    """Answers the structuring and risk prompts from a lookup table."""

    provider_name = "fake"
    model = "fake-1"

    def __init__(self, answers: dict[str, list[dict[str, Any]]], location_type: str = "INT"):
        self.answers = answers
        self.location_type = location_type
        self.calls = 0

    async def health_check(self) -> bool:
        return True

    async def generate_structured(
        self, prompt: str, schema: dict, system_prompt=None, temperature=0.2, **kw
    ):
        self.calls += 1
        if "findings" in schema.get("properties", {}):
            # risk prompt: pick answers whose scene id appears in the prompt
            for scene_id, findings in self.answers.items():
                if scene_id in prompt:
                    return {"findings": findings}
            return {"findings": []}
        return {
            "location": "SOMEWHERE",
            "location_type": self.location_type,
            "time_of_day": "DAY",
            "characters": [],
            "action_text": "",
            "dialogue": [],
        }


def _finding(cls: str, likelihood: int = 4, impact: int = 4) -> dict[str, Any]:
    return {
        "risk_class": cls,
        "category": "PHYSICAL",
        "likelihood": likelihood,
        "impact": impact,
        "description": "x",
        "recommendation": "y",
        "evidence": "quote",
        "measure_codes": [],
    }


class TestGoldenSet:
    def test_golden_scenes_load_and_reference_known_classes(self):
        from services.taxonomy import get_taxonomy_manager

        scenes = load_golden_scenes(GOLDEN)
        assert len(scenes) >= 15
        taxonomy = get_taxonomy_manager()
        for scene in scenes:
            for cls in scene.expected_classes:
                assert taxonomy.is_valid_class(cls), f"{scene.id}: unknown class {cls}"
            assert scene.location_type in {"INT", "EXT", "UNKNOWN"}
        assert sum(1 for s in scenes if s.negative) >= 2


@pytest.mark.asyncio
class TestRunnerScoring:
    async def test_perfect_provider_passes_all_thresholds(self):
        scenes = load_golden_scenes(GOLDEN)
        answers = {
            s.id: [_finding(c, 5, 5) for c in s.expected_classes] for s in scenes if not s.negative
        }
        provider = FakeProvider(answers)
        # location_type per scene: fake returns INT; make expectations match for the test
        for s in scenes:
            s.location_type = "INT"
        report = await ParityRunner(provider).run(scenes)
        m = report.metrics()
        assert m["schema_valid_rate"] == 1.0
        assert m["class_recall"] == 1.0
        assert m["false_positive_rate"] == 0.0
        assert m["location_type_accuracy"] == 1.0
        assert report.passed() is True
        assert provider.calls == 2 * len(scenes)

    async def test_missing_classes_lower_recall_and_fail(self):
        scenes = [
            GoldenScene("a", "INT. X", "INT", "a text", ["FIRE", "SMOKE_DUST"], "high"),
            GoldenScene("b", "INT. Y", "INT", "b text", ["HEIGHT"], "high"),
        ]
        provider = FakeProvider({"a": [_finding("FIRE")], "b": []})
        report = await ParityRunner(provider).run(scenes)
        m = report.metrics()
        assert m["class_recall"] == pytest.approx(0.25)  # (0.5 + 0) / 2
        assert report.verdict()["class_recall"] is False
        assert report.passed() is False

    async def test_false_positive_on_benign_scene(self):
        scenes = [GoldenScene("benign", "INT. X", "INT", "x", [], "info", negative=True)]
        provider = FakeProvider({"benign": [_finding("FIRE", 5, 5)]})
        report = await ParityRunner(provider).run(scenes)
        m = report.metrics()
        assert m["false_positive_rate"] == 1.0
        assert report.verdict()["false_positive_rate"] is False

    async def test_severity_within_one_level_is_accepted(self):
        scenes = [GoldenScene("s", "INT. X", "INT", "x", ["FIRE"], "critical")]
        # likelihood 3 x impact 4 = 12 -> high, one below critical -> accepted
        provider = FakeProvider({"s": [_finding("FIRE", 3, 4)]})
        report = await ParityRunner(provider).run(scenes)
        assert report.scenes[0].max_severity == "high"
        assert report.scenes[0].severity_ok is True
        # 2x2 = 4 -> low: two levels below -> rejected
        provider = FakeProvider({"s": [_finding("FIRE", 2, 2)]})
        report = await ParityRunner(provider).run(scenes)
        assert report.scenes[0].severity_ok is False

    async def test_provider_errors_are_captured_not_raised(self):
        class Broken(FakeProvider):
            async def generate_structured(self, *a, **k):
                raise RuntimeError("boom")

        scenes = [GoldenScene("s", "INT. X", "INT", "x", ["FIRE"], "high")]
        report = await ParityRunner(Broken({})).run(scenes)
        assert report.scenes[0].error == "structure:RuntimeError;risk:RuntimeError"
        assert report.metrics()["errors"] == 1.0
        assert report.passed() is False


class TestReporting:
    def _report(self) -> ProviderReport:
        from services.parity import SceneResult

        r = ProviderReport(provider="local_mistral", model="mistral-small3.2")
        r.scenes = [
            SceneResult(
                "a", True, ["FIRE"], ["FIRE"], "high", "high", False, True, "INT", "INT", 1.0, 0.5
            ),
            SceneResult("b", True, [], [], "info", "info", True, True, "EXT", "EXT", 1.2, 0.4),
        ]
        return r

    def test_markdown_contains_thresholds_and_verdict(self):
        md = render_markdown([self._report()])
        assert "`class_recall`" in md
        assert f">= {DEFAULT_THRESHOLDS['class_recall']:.2f}" in md
        assert "local_mistral (mistral-small3.2)" in md
        assert "**bestanden**" in md

    def test_write_reports_creates_json_and_markdown(self, tmp_path):
        json_path, md_path = write_reports([self._report()], tmp_path, stamp="t")
        data = json.loads(json_path.read_text())
        assert data["providers"][0]["passed"] is True
        assert data["providers"][0]["scenes"][0]["recall"] == 1.0
        assert md_path.read_text().startswith("| Kennzahl")
