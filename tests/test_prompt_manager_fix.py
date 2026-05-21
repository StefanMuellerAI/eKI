"""Verify that PromptManager.get formats BOTH system and user templates.

Before M06 this was a real bug: the YAML template
``risk_analysis.scene.system`` references ``{taxonomy_context}``, but
``PromptManager.get()`` only called ``.format()`` on the user template,
so the system message reached the LLM with literal ``{taxonomy_context}``
text -- and now also with ``{kb_context}``.  This test pins the fix.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from llm.prompt_manager import PromptManager


def _write_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "prompts.yaml"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def test_system_template_variables_are_substituted(tmp_path: Path) -> None:
    yaml_path = _write_yaml(
        tmp_path,
        """
        version: "1.0"
        risk_analysis:
          scene:
            system: |
              Use the following risk taxonomy:
              {taxonomy_context}
              Relevant safety knowledge:
              {kb_context}
            user: |
              Analyze scene {scene_number}: {scene_text}
        """,
    )
    pm = PromptManager(yaml_path)

    system, user = pm.get(
        "risk_analysis",
        "scene",
        taxonomy_context="TAXONOMY_BODY",
        kb_context="KB_BODY",
        scene_number="42",
        scene_text="A man falls from a roof.",
    )

    assert "TAXONOMY_BODY" in system
    assert "KB_BODY" in system
    assert "{taxonomy_context}" not in system
    assert "{kb_context}" not in system
    assert "42" in user
    assert "A man falls from a roof." in user


def test_system_template_works_without_placeholders(tmp_path: Path) -> None:
    """A system template free of placeholders is unaffected by the fix."""
    yaml_path = _write_yaml(
        tmp_path,
        """
        version: "1.0"
        pdf_structuring:
          scene:
            system: |
              You are a screenplay structure parser.
            user: |
              Extract data from this block:
              {scene_text}
        """,
    )
    pm = PromptManager(yaml_path)

    system, user = pm.get(
        "pdf_structuring",
        "scene",
        scene_text="INT. KITCHEN -- NIGHT",
    )

    assert system == "You are a screenplay structure parser."
    assert "INT. KITCHEN -- NIGHT" in user


def test_missing_required_variable_raises_key_error(tmp_path: Path) -> None:
    yaml_path = _write_yaml(
        tmp_path,
        """
        version: "1.0"
        risk_analysis:
          scene:
            system: |
              Context: {taxonomy_context}
            user: |
              Analyze: {scene_text}
        """,
    )
    pm = PromptManager(yaml_path)

    with pytest.raises(KeyError):
        pm.get("risk_analysis", "scene", scene_text="x")  # taxonomy_context missing
