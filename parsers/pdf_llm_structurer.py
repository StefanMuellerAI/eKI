"""LLM-based scene structuring for PDF-parsed text blocks.

Each raw text block is sent to the configured LLM (Ollama / Mistral) with
a JSON schema via structured output.  The LLM extracts location, characters,
dialogue, etc. -- the parts that pure regex cannot reliably handle across
varying screenplay formats.
"""

import json
import logging
from typing import Any

from core.exceptions import LLMException
from core.models import DialogueLine, LocationType, ParsedScene, TimeOfDay
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
                "DAY", "NIGHT", "DAWN", "DUSK", "MORNING",
                "EVENING", "CONTINUOUS", "UNKNOWN",
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
        "location", "location_type", "time_of_day",
        "characters", "action_text", "dialogue",
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
    system_prompt, user_prompt = pm.get(
        "pdf_structuring", "scene", scene_text=scene_text
    )

    try:
        result = await llm_provider.generate_structured(
            prompt=user_prompt,
            schema=SCENE_SCHEMA,
            system_prompt=system_prompt,
            temperature=temperature,
        )
        return result
    except (LLMException, Exception) as exc:
        logger.warning("LLM scene structuring failed: %s", exc)
        return _fallback_scene_data()


async def extract_title_from_preamble(
    preamble_text: str,
    llm_provider: Any,
    temperature: float = 0.1,
) -> str | None:
    """Extract a script title from the preamble text via LLM."""
    pm = get_prompt_manager()
    system_prompt, user_prompt = pm.get(
        "pdf_structuring", "preamble", preamble_text=preamble_text
    )

    try:
        result = await llm_provider.generate_structured(
            prompt=user_prompt,
            schema=PREAMBLE_SCHEMA,
            system_prompt=system_prompt,
            temperature=temperature,
        )
        return result.get("title")
    except (LLMException, Exception) as exc:
        logger.warning("LLM preamble extraction failed: %s", exc)
        return None


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


def llm_result_to_parsed_scene_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Convert an LLM structured output dict into fields compatible with ParsedScene.

    Normalises enums and builds ``DialogueLine`` objects.
    """
    # Normalise enums (LLM may return slightly different casing)
    loc_type_raw = (result.get("location_type") or "UNKNOWN").upper().strip()
    try:
        loc_type = LocationType(loc_type_raw)
    except ValueError:
        loc_type = LocationType.UNKNOWN

    tod_raw = (result.get("time_of_day") or "UNKNOWN").upper().strip()
    try:
        tod = TimeOfDay(tod_raw)
    except ValueError:
        tod = TimeOfDay.UNKNOWN

    # Build dialogue lines
    dialogue_lines: list[DialogueLine] = []
    for d in result.get("dialogue") or []:
        if isinstance(d, dict) and d.get("character") and d.get("text"):
            dialogue_lines.append(
                DialogueLine(
                    character=d["character"],
                    parenthetical=d.get("parenthetical"),
                    text=d["text"],
                )
            )

    return {
        "location": result.get("location") or "UNKNOWN",
        "location_type": loc_type,
        "time_of_day": tod,
        "characters": result.get("characters") or [],
        "action_text": result.get("action_text") or "",
        "dialogue": dialogue_lines,
    }
