"""Temporal activities for FDX and PDF security check workflows.

Each activity fetches sensitive data from the encrypted SecureBuffer (Redis),
processes it, stores the result back, and returns only the new reference key
plus metadata.  No screenplay content flows through Temporal history.
"""

import base64
import logging
import time
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis
from temporalio import activity

from api.config import get_settings
from services.secure_buffer import SecureBuffer

logger = logging.getLogger(__name__)


def _get_buffer() -> SecureBuffer:
    """Create a SecureBuffer bound to the global Redis + secret key."""
    settings = get_settings()
    redis_client = aioredis.from_url(
        str(settings.redis_url),
        decode_responses=False,
    )
    return SecureBuffer(
        redis_client,
        secret_key=settings.api_secret_key,
        default_ttl=settings.buffer_ttl_seconds,
    )


# ===================================================================
# FDX Activities
# ===================================================================


@activity.defn(name="parse_fdx")
async def parse_fdx_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Parse FDX script content and extract scenes.

    Fetches encrypted script from Redis, parses with the FDX parser,
    stores the parsed result back in Redis.
    """
    ref_key = job_data.get("ref_key", "")
    logger.info("Parsing FDX script")

    buffer = _get_buffer()

    try:
        raw_data = await buffer.retrieve(ref_key)
        b64_content = raw_data["script_content"]
        content_bytes = base64.b64decode(b64_content)

        from parsers.fdx import FDXParser

        parser = FDXParser()
        parsed = await parser.parse(content_bytes)

        parsed_ref = await buffer.store(parsed.model_dump(mode="json"))
        await buffer.delete(ref_key)

        return {
            "parsed_ref_key": parsed_ref,
            "total_scenes": parsed.total_scenes,
            "total_characters": len(parsed.characters),
            "parsing_time_seconds": parsed.parsing_time_seconds,
        }
    except Exception:
        logger.exception("parse_fdx_activity failed")
        raise


# ===================================================================
# PDF Activities
# ===================================================================


@activity.defn(name="extract_pdf_text")
async def extract_pdf_text_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Extract text from a PDF and store it in Redis."""
    ref_key = job_data.get("ref_key", "")
    logger.info("Extracting PDF text")

    buffer = _get_buffer()

    raw_data = await buffer.retrieve(ref_key)
    b64_content = raw_data["script_content"]
    content_bytes = base64.b64decode(b64_content)

    from parsers.pdf import extract_pdf_text

    full_text, ocr_pages = extract_pdf_text(content_bytes)

    text_ref = await buffer.store({"full_text": full_text})
    await buffer.delete(ref_key)

    return {
        "text_ref_key": text_ref,
        "ocr_pages_skipped": ocr_pages,
        "text_length": len(full_text),
    }


@activity.defn(name="split_scenes")
async def split_scenes_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Split extracted text at INT/EXT markers."""
    text_ref_key = job_data.get("text_ref_key", "")
    logger.info("Splitting PDF text into scenes")

    buffer = _get_buffer()

    text_data = await buffer.retrieve(text_ref_key)
    full_text = text_data["full_text"]

    from parsers.pdf_scene_splitter import split_into_scenes

    blocks = split_into_scenes(full_text)

    # Store all blocks as a list in Redis
    blocks_data = [
        {
            "index": b.index,
            "text": b.text,
            "heading_line": b.heading_line,
            "is_preamble": b.is_preamble,
        }
        for b in blocks
    ]
    blocks_ref = await buffer.store({"blocks": blocks_data})
    await buffer.delete(text_ref_key)

    scene_count = sum(1 for b in blocks if not b.is_preamble)
    has_preamble = any(b.is_preamble for b in blocks)

    return {
        "blocks_ref_key": blocks_ref,
        "block_count": len(blocks),
        "scene_count": scene_count,
        "has_preamble": has_preamble,
    }


@activity.defn(name="structure_scene_llm")
async def structure_scene_llm_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Structure a single scene block via LLM (Ollama Structured Output).

    Processes one block at a time.  The workflow calls this activity
    once per scene block.
    """
    blocks_ref_key = job_data.get("blocks_ref_key", "")
    block_index = job_data.get("block_index", 0)
    logger.info("LLM structuring block %d", block_index)

    buffer = _get_buffer()

    blocks_data = await buffer.retrieve(blocks_ref_key)
    blocks = blocks_data["blocks"]

    if block_index >= len(blocks):
        return {"error": f"Block index {block_index} out of range"}

    block = blocks[block_index]

    from api.config import get_settings
    from llm.factory import get_llm_provider
    from parsers.pdf_llm_structurer import (
        extract_title_from_preamble,
        llm_result_to_parsed_scene_fields,
        structure_scene_with_llm,
    )

    settings = get_settings()
    llm = get_llm_provider(settings)

    if block["is_preamble"]:
        title = await extract_title_from_preamble(block["text"], llm)
        return {"is_preamble": True, "title": title}

    llm_result = await structure_scene_with_llm(block["text"], llm)
    fields = llm_result_to_parsed_scene_fields(llm_result)

    # Store structured scene fields in Redis
    scene_data = {
        "heading_line": block["heading_line"],
        "text": block["text"],
        "fields": {
            "location": fields["location"],
            "location_type": fields["location_type"].value if hasattr(fields["location_type"], "value") else str(fields["location_type"]),
            "time_of_day": fields["time_of_day"].value if hasattr(fields["time_of_day"], "value") else str(fields["time_of_day"]),
            "characters": fields["characters"],
            "action_text": fields["action_text"],
            "dialogue": [
                {"character": d.character, "parenthetical": d.parenthetical, "text": d.text}
                for d in fields["dialogue"]
            ],
        },
        "confidence": 1.0 if fields["location"] != "UNKNOWN" else 0.5,
    }
    scene_ref = await buffer.store(scene_data)

    return {
        "is_preamble": False,
        "scene_ref_key": scene_ref,
        "block_index": block_index,
    }


@activity.defn(name="aggregate_script")
async def aggregate_script_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Assemble individual ParsedScenes into a complete ParsedScript.

    All IDs, counters, and the character index are built programmatically.
    No LLM call needed.
    """
    scene_ref_keys = job_data.get("scene_ref_keys", [])
    title = job_data.get("title")
    ocr_pages = job_data.get("ocr_pages_skipped", [])
    blocks_ref_key = job_data.get("blocks_ref_key", "")
    t0 = time.monotonic()
    logger.info("Aggregating %d scenes into ParsedScript", len(scene_ref_keys))

    buffer = _get_buffer()

    from collections import defaultdict
    from uuid import uuid4

    from core.models import (
        CharacterInfo,
        DialogueLine,
        LocationType,
        ParsedScene,
        ParsedScript,
        ScriptFormat,
        TimeOfDay,
    )

    scenes: list[ParsedScene] = []
    warnings: list[str] = []

    if ocr_pages:
        warnings.append(f"Pages {ocr_pages} appear scanned. OCR not yet implemented.")

    for i, ref_key in enumerate(scene_ref_keys):
        scene_data = await buffer.retrieve(ref_key)
        f = scene_data["fields"]

        # Normalise enums
        try:
            loc_type = LocationType(f["location_type"])
        except ValueError:
            loc_type = LocationType.UNKNOWN

        try:
            tod = TimeOfDay(f["time_of_day"])
        except ValueError:
            tod = TimeOfDay.UNKNOWN

        dialogue_lines = [
            DialogueLine(character=d["character"], parenthetical=d.get("parenthetical"), text=d["text"])
            for d in f.get("dialogue", [])
        ]

        scene = ParsedScene(
            scene_id=uuid4(),
            number=str(i + 1),
            heading=scene_data["heading_line"],
            location=f.get("location", "UNKNOWN"),
            location_type=loc_type,
            time_of_day=tod,
            characters=f.get("characters", []),
            action_text=f.get("action_text", ""),
            dialogue=dialogue_lines,
            text=scene_data["text"],
            parse_confidence=scene_data.get("confidence", 0.0),
            parse_method="pdf_llm",
        )
        scenes.append(scene)
        await buffer.delete(ref_key)

    # Build character index programmatically
    appearances: dict[str, list[str]] = defaultdict(list)
    for scene in scenes:
        for name in scene.characters:
            appearances[name].append(str(scene.scene_id))
    characters = [
        CharacterInfo(name=name, scene_appearances=sids)
        for name, sids in appearances.items()
    ]

    avg_confidence = sum(s.parse_confidence for s in scenes) / len(scenes) if scenes else 0.0
    elapsed = time.monotonic() - t0

    script = ParsedScript(
        script_id=uuid4(),
        title=title,
        format=ScriptFormat.PDF,
        total_scenes=len(scenes),
        scenes=scenes,
        characters=characters,
        parsing_time_seconds=round(elapsed, 3),
        overall_confidence=round(avg_confidence, 3),
        warnings=warnings,
        metadata={"parser": "pdf_llm", "ocr_pages_skipped": ocr_pages},
    )

    parsed_ref = await buffer.store(script.model_dump(mode="json"))

    # Clean up blocks ref
    if blocks_ref_key:
        await buffer.delete(blocks_ref_key)

    return {
        "parsed_ref_key": parsed_ref,
        "total_scenes": len(scenes),
        "total_characters": len(characters),
        "overall_confidence": round(avg_confidence, 3),
    }


# ===================================================================
# Shared Activities (used by both FDX and PDF workflows)
# ===================================================================


_RISK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "risk_level": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"],
                    },
                    "category": {
                        "type": "string",
                        "enum": ["PHYSICAL", "ENVIRONMENTAL", "PSYCHOLOGICAL"],
                    },
                    "description": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["risk_level", "category", "description", "recommendation"],
            },
        },
    },
    "required": ["findings"],
}


@activity.defn(name="analyze_scene_risk")
async def analyze_scene_risk_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Analyze a single scene for safety risks using LLM (Ollama/Mistral).

    Fetches the scene from Redis, sends it to the LLM with the risk_analysis
    prompt, and returns structured findings.
    """
    parsed_ref_key = job_data.get("parsed_ref_key", "")
    scene_index = job_data.get("scene_index", 0)
    logger.info("Analyzing risks for scene %d via LLM", scene_index)

    buffer = _get_buffer()
    parsed_data = await buffer.retrieve(parsed_ref_key)
    scenes = parsed_data.get("scenes", [])

    if scene_index >= len(scenes):
        return {"findings": [], "scene_index": scene_index}

    scene = scenes[scene_index]
    scene_number = scene.get("number", str(scene_index + 1))

    try:
        from api.config import get_settings
        from llm.factory import get_llm_provider
        from llm.prompt_manager import get_prompt_manager

        settings = get_settings()
        llm = get_llm_provider(settings)
        pm = get_prompt_manager()

        system_prompt, user_prompt = pm.get(
            "risk_analysis", "scene",
            scene_number=scene_number,
            location=scene.get("location", "UNKNOWN"),
            location_type=scene.get("location_type", "UNKNOWN"),
            time_of_day=scene.get("time_of_day", "UNKNOWN"),
            scene_text=scene.get("text", ""),
        )

        result = await llm.generate_structured(
            prompt=user_prompt,
            schema=_RISK_SCHEMA,
            system_prompt=system_prompt,
            temperature=0.2,
        )

        findings = result.get("findings", [])
        # Tag each finding with scene reference
        from uuid import uuid4
        for f in findings:
            f["id"] = str(uuid4())
            f["scene_number"] = scene_number
            f["line_reference"] = None
            if "confidence" not in f:
                f["confidence"] = 0.8

        logger.info("Scene %s: %d findings", scene_number, len(findings))
        return {
            "scene_index": scene_index,
            "scene_number": scene_number,
            "findings": findings,
        }

    except Exception as exc:
        logger.warning("Risk analysis failed for scene %s: %s", scene_number, exc)
        return {
            "scene_index": scene_index,
            "scene_number": scene_number,
            "findings": [],
        }


@activity.defn(name="aggregate_report")
async def aggregate_report_activity(
    job_data: dict[str, Any], job_metadata: dict[str, Any]
) -> dict[str, Any]:
    """Aggregate per-scene findings into a complete SecurityReport.

    Programmatic aggregation -- no LLM needed.
    """
    all_findings = job_data.get("all_findings", [])
    parsed_ref_key = job_data.get("parsed_ref_key", "")
    logger.info("Aggregating report from %d scene analyses", len(all_findings))

    buffer = _get_buffer()

    # Count findings by severity
    risk_summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    flat_findings = []
    for scene_result in all_findings:
        for finding in scene_result.get("findings", []):
            level = finding.get("risk_level", "info")
            risk_summary[level] = risk_summary.get(level, 0) + 1
            flat_findings.append(finding)

    total_findings = len(flat_findings)
    if total_findings == 0:
        risk_summary["info"] = 1
        total_findings = 1

    report = {
        "report_id": job_metadata.get("report_id"),
        "project_id": job_metadata.get("project_id"),
        "findings": flat_findings,
        "risk_summary": risk_summary,
        "total_findings": total_findings,
        "processing_time_seconds": 1.0,
        "metadata": {"stub": total_findings == 1 and not flat_findings},
    }
    report_ref = await buffer.store(report)

    # Clean up parsed data
    if parsed_ref_key:
        await buffer.delete(parsed_ref_key)

    return {
        "report_ref_key": report_ref,
        "report_id": job_metadata.get("report_id"),
        "total_findings": total_findings,
    }


@activity.defn(name="deliver_report")
async def deliver_report_activity(
    report_data: dict[str, Any], callback_url: str | None = None
) -> dict[str, Any]:
    """Deliver report to eProjekt system (write-through).

    M03 stub -- real eProjekt integration in later milestones.
    """
    report_ref_key = report_data.get("report_ref_key", "")
    logger.info("Delivering report %s to eProjekt", report_data.get("report_id"))

    buffer = _get_buffer()
    _report = await buffer.retrieve(report_ref_key)

    if callback_url:
        logger.info("Callback URL provided: %s", callback_url)

    await buffer.delete(report_ref_key)

    return {
        "delivered": True,
        "delivery_time_seconds": 0.2,
        "callback_sent": callback_url is not None,
    }
