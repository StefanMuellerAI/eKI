"""LLM-based scene structuring for PDF-parsed text blocks.

Each raw text block is sent to the configured LLM (Ollama / Mistral) with
a JSON schema via structured output.  The LLM extracts location, characters,
dialogue, etc. -- the parts that pure regex cannot reliably handle across
varying screenplay formats.
"""

import logging
from typing import Any

from core.exceptions import LLMException
from core.models import DialogueLine, LocationType, TimeOfDay
from llm.prompt_manager import get_prompt_manager

logger = logging.getLogger(__name__)

# JSON Schema that the LLM must conform to for scene structuring.
SCENE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "location": {
            "type": "string",
            "description": "Location name extracted from the scene heading (e.g. OFFICE, FOREST)",
        },
        "location_type": {
            "type": "string",
            "enum": ["INT", "EXT", "INT/EXT", "UNKNOWN"],
            "description": "Interior/exterior designation",
        },
        "time_of_day": {
            "type": "string",
            "enum": [
                "DAY",
                "NIGHT",
                "DAWN",
                "DUSK",
                "MORNING",
                "EVENING",
                "CONTINUOUS",
                "UNKNOWN",
            ],
            "description": "Time of day from the scene heading",
        },
        "characters": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of speaking character names (UPPERCASE)",
        },
        "action_text": {
            "type": "string",
            "description": "Action/description text (everything that is not dialogue)",
        },
        "dialogue": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "character": {"type": "string"},
                    "parenthetical": {"type": ["string", "null"]},
                    "text": {"type": "string"},
                },
                "required": ["character", "text"],
            },
            "description": "Dialogue lines with character attribution",
        },
    },
    "required": [
        "location",
        "location_type",
        "time_of_day",
        "characters",
        "action_text",
        "dialogue",
    ],
}

PREAMBLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": ["string", "null"],
            "description": "Script title if found, null otherwise",
        },
    },
    "required": ["title"],
}


async def structure_scene_with_llm(
    scene_text: str,
    llm_provider: Any,
    temperature: float = 0.1,
) -> dict[str, Any]:
    """Send a raw scene text block to the LLM and get structured data back.

    Returns a dict matching ``SCENE_SCHEMA``.  On LLM failure, returns a
    minimal fallback dict so the pipeline does not break.
    """
    pm = get_prompt_manager()
    system_prompt, user_prompt = pm.get("pdf_structuring", "scene", scene_text=scene_text)

    try:
        result = await llm_provider.generate_structured(
            prompt=user_prompt,
            schema=SCENE_SCHEMA,
            system_prompt=system_prompt,
            temperature=temperature,
        )
    except (LLMException, Exception) as exc:
        logger.warning("LLM scene structuring failed: %s", exc)
        return _fallback_scene_data()
    if not isinstance(result, dict):
        logger.warning(
            "LLM scene structuring returned %s instead of an object; using fallback",
            type(result).__name__,
        )
        return _fallback_scene_data()
    return result


async def extract_title_from_preamble(
    preamble_text: str,
    llm_provider: Any,
    temperature: float = 0.1,
) -> str | None:
    """Extract a script title from the preamble text via LLM."""
    pm = get_prompt_manager()
    system_prompt, user_prompt = pm.get("pdf_structuring", "preamble", preamble_text=preamble_text)

    try:
        result = await llm_provider.generate_structured(
            prompt=user_prompt,
            schema=PREAMBLE_SCHEMA,
            system_prompt=system_prompt,
            temperature=temperature,
        )
    except (LLMException, Exception) as exc:
        logger.warning("LLM preamble extraction failed: %s", exc)
        return None
    title = result.get("title") if isinstance(result, dict) else None
    # ParsedScript.title is ``str | None``: anything else would fail validation
    # of the whole script, so non-string / blank titles are dropped.
    if not isinstance(title, str) or not title.strip():
        return None
    return title.strip()


def _fallback_scene_data() -> dict[str, Any]:
    """Minimal scene data when LLM fails."""
    return {
        "location": "UNKNOWN",
        "location_type": "UNKNOWN",
        "time_of_day": "UNKNOWN",
        "characters": [],
        "action_text": "",
        "dialogue": [],
    }


def _str_or_none(value: Any) -> str | None:
    """Return *value* stripped if it is a non-blank string, else ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _enum_value(value: Any) -> str:
    """Upper-cased enum candidate; non-strings map to ``UNKNOWN``."""
    return value.upper().strip() if isinstance(value, str) else "UNKNOWN"


def llm_result_to_parsed_scene_fields(result: Any) -> dict[str, Any]:
    """Convert an LLM structured output dict into fields compatible with ParsedScene.

    Normalises enums and builds ``DialogueLine`` objects.  The LLM output is
    untrusted: every field is type-checked and coerced (non-string text ->
    ``"UNKNOWN"``/``""``, ``characters`` -> de-duplicated ``list[str]``,
    incomplete dialogue entries dropped) so a single malformed answer can
    never make ``ParsedScene`` validation fail.  A non-dict *result* is
    treated like an empty answer.
    """
    if not isinstance(result, dict):
        logger.warning(
            "LLM scene result is %s, not an object; using UNKNOWN fields",
            type(result).__name__,
        )
        result = {}

    try:
        loc_type = LocationType(_enum_value(result.get("location_type")))
    except ValueError:
        loc_type = LocationType.UNKNOWN

    try:
        tod = TimeOfDay(_enum_value(result.get("time_of_day")))
    except ValueError:
        tod = TimeOfDay.UNKNOWN

    raw_characters = result.get("characters")
    characters: list[str] = []
    if isinstance(raw_characters, list):
        for name in raw_characters:
            clean = _str_or_none(name)
            if clean is not None and clean not in characters:
                characters.append(clean)

    dialogue_lines: list[DialogueLine] = []
    raw_dialogue = result.get("dialogue")
    for d in raw_dialogue if isinstance(raw_dialogue, list) else []:
        if not isinstance(d, dict):
            continue
        speaker = _str_or_none(d.get("character"))
        text = d.get("text")
        if speaker is None or not isinstance(text, str) or not text.strip():
            continue
        dialogue_lines.append(
            DialogueLine(
                character=speaker,
                parenthetical=_str_or_none(d.get("parenthetical")),
                text=text,
            )
        )

    action_text = result.get("action_text")
    return {
        "location": _str_or_none(result.get("location")) or "UNKNOWN",
        "location_type": loc_type,
        "time_of_day": tod,
        "characters": characters,
        "action_text": action_text if isinstance(action_text, str) else "",
        "dialogue": dialogue_lines,
    }
