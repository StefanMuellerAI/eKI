"""Centralized prompt management loaded from YAML configuration.

All LLM prompts are defined in ``config/prompts/prompts.yaml`` and accessed
via the ``PromptManager`` singleton.  This allows prompt changes without
touching Python code.
"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_YAML_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "prompts.yaml"


class PromptManager:
    """Load and format prompts from a YAML configuration file.

    Usage::

        pm = get_prompt_manager()
        system, user = pm.get("risk_analysis", "scene",
            scene_number="3",
            location="HIGHWAY",
            scene_text="Three cars race at high speed...",
        )
    """

    def __init__(self, yaml_path: Path | str | None = None) -> None:
        path = Path(yaml_path) if yaml_path else _DEFAULT_YAML_PATH
        if not path.exists():
            raise FileNotFoundError(f"Prompt YAML not found: {path}")
        self._prompts: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        self._version = self._prompts.get("version", "unknown")
        logger.info("PromptManager loaded v%s from %s", self._version, path)

    @property
    def version(self) -> str:
        return self._version

    def get(self, section: str, name: str, **kwargs: Any) -> tuple[str, str]:
        """Return ``(system_prompt, user_prompt)`` with variables substituted.

        Raises ``KeyError`` if section/name does not exist.
        Raises ``KeyError`` if a required template variable is missing from *kwargs*.
        """
        try:
            entry = self._prompts[section][name]
        except KeyError:
            raise KeyError(f"Prompt not found: {section}.{name}")

        system = entry["system"].strip()
        user = entry["user"].strip().format(**kwargs)
        return system, user

    def get_system(self, section: str, name: str) -> str:
        """Return only the system prompt (no variable substitution needed)."""
        try:
            return self._prompts[section][name]["system"].strip()
        except KeyError:
            raise KeyError(f"Prompt not found: {section}.{name}")

    def sections(self) -> list[str]:
        """List available top-level sections (excluding 'version')."""
        return [k for k in self._prompts if k != "version"]


@lru_cache
def get_prompt_manager() -> PromptManager:
    """Return a cached singleton ``PromptManager``."""
    return PromptManager()
