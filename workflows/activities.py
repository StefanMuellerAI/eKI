"""Temporal activities for FDX and PDF security check workflows.

Each activity fetches sensitive data from the encrypted SecureBuffer (Redis),
processes it, stores the result back, and returns only the new reference key
plus metadata.  No screenplay content flows through Temporal history.
"""

import base64
import logging
import time
from datetime import UTC
from typing import Any

import redis.asyncio as aioredis
from temporalio import activity

from api.config import get_settings
from core import metrics
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

    import asyncio

    from parsers.pdf import extract_pdf_text

    # OCR (if triggered) is CPU-bound -> keep the worker event loop responsive.
    extraction = await asyncio.to_thread(extract_pdf_text, content_bytes)
    full_text, page_texts = extraction.full_text, extraction.page_texts
    if extraction.ocr_pages_done:
        metrics.SCENES_PROCESSED_TOTAL.labels(stage="ocr_page").inc(len(extraction.ocr_pages_done))

    text_ref = await buffer.store(
        {
            "full_text": full_text,
            "page_texts": page_texts,
        }
    )
    await buffer.delete(ref_key)

    return {
        "text_ref_key": text_ref,
        "ocr_pages_done": extraction.ocr_pages_done,
        "ocr_pages_skipped": extraction.ocr_pages_skipped,
        "text_length": len(full_text),
        "extraction_warnings": extraction.warnings,
    }


@activity.defn(name="split_scenes")
async def split_scenes_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Split extracted text at INT/EXT markers (with page-based fallback)."""
    text_ref_key = job_data.get("text_ref_key", "")
    logger.info("Splitting PDF text into scenes")

    buffer = _get_buffer()

    text_data = await buffer.retrieve(text_ref_key)
    full_text = text_data["full_text"]
    page_texts = text_data.get("page_texts")

    from parsers.pdf_scene_splitter import split_into_scenes

    blocks = split_into_scenes(full_text, page_texts=page_texts)

    used_page_fallback = any(
        not b.is_preamble and b.heading_line.startswith("PAGE ") for b in blocks
    )

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
        "used_page_fallback": used_page_fallback,
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
    metrics.SCENES_PROCESSED_TOTAL.labels(stage="structure").inc()
    fields = llm_result_to_parsed_scene_fields(llm_result)

    is_page_fallback = job_data.get("used_page_fallback", False)
    if is_page_fallback:
        confidence = 0.3 if fields["location"] != "UNKNOWN" else 0.1
    else:
        confidence = 1.0 if fields["location"] != "UNKNOWN" else 0.5

    # Store structured scene fields in Redis
    scene_data = {
        "heading_line": block["heading_line"],
        "text": block["text"],
        "fields": {
            "location": fields["location"],
            "location_type": fields["location_type"].value
            if hasattr(fields["location_type"], "value")
            else str(fields["location_type"]),
            "time_of_day": fields["time_of_day"].value
            if hasattr(fields["time_of_day"], "value")
            else str(fields["time_of_day"]),
            "characters": fields["characters"],
            "action_text": fields["action_text"],
            "dialogue": [
                {"character": d.character, "parenthetical": d.parenthetical, "text": d.text}
                for d in fields["dialogue"]
            ],
        },
        "confidence": confidence,
        "parse_method": "pdf_page_fallback" if is_page_fallback else "pdf_llm",
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
    ocr_pages_done = job_data.get("ocr_pages_done", [])
    blocks_ref_key = job_data.get("blocks_ref_key", "")
    used_page_fallback = job_data.get("used_page_fallback", False)
    extra_warnings = job_data.get("extra_warnings", [])
    t0 = time.monotonic()
    logger.info("Aggregating %d scenes into ParsedScript", len(scene_ref_keys))

    # M07-Tripwire: Bei sehr großen Drehbüchern (z.B. 300+ Seiten unter
    # paralleler Strukturierung) können mehrere hundert Scene-Refs
    # gleichzeitig in Redis liegen. Eine Warn-Schwelle hilft im Betrieb,
    # frühzeitig zu erkennen, falls der SecureBuffer ungewöhnlich wächst.
    if len(scene_ref_keys) > 200:
        logger.warning(
            "aggregate_script_activity: %d pending scene refs to clean up "
            "(Großdokument-Pfad, M07). Wird inkrementell in dieser "
            "Activity abgeräumt.",
            len(scene_ref_keys),
        )

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
    warnings: list[str] = list(extra_warnings)

    if ocr_pages_done:
        warnings.append(f"Pages {ocr_pages_done} had no text layer; text recovered via OCR.")
    if ocr_pages:
        warnings.append(
            f"Pages {ocr_pages} appear to be image-only and could not be OCR'd "
            "(OCR disabled, unavailable, capped or no text recognised)."
        )

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
            DialogueLine(
                character=d["character"], parenthetical=d.get("parenthetical"), text=d["text"]
            )
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
            parse_method=scene_data.get("parse_method", "pdf_llm"),
        )
        scenes.append(scene)
        await buffer.delete(ref_key)

    # Build character index programmatically
    appearances: dict[str, list[str]] = defaultdict(list)
    for scene in scenes:
        for name in scene.characters:
            appearances[name].append(str(scene.scene_id))
    characters = [
        CharacterInfo(name=name, scene_appearances=sids) for name, sids in appearances.items()
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
        metadata={
            "parser": "pdf_page_fallback" if used_page_fallback else "pdf_llm",
            "ocr_pages_done": ocr_pages_done,
            "ocr_pages_skipped": ocr_pages,
            "used_page_fallback": used_page_fallback,
        },
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
# Job Status Activity (failure handling)
# ===================================================================


def _record_job_terminal_metric(
    *,
    script_format: str,
    status: str,
    error_message: Any,
    created_at: Any,
) -> None:
    """Emit eki_jobs_total / eki_job_duration_seconds for a terminal transition.

    ``delivery_failed:*`` errors are counted under their own status label so
    the SLO dashboards can separate processing failures from outbound ones.
    """
    label = status
    if status == "failed" and str(error_message or "").startswith("delivery_failed"):
        label = "delivery_failed"

    duration: float | None = None
    if created_at is not None:
        from datetime import datetime

        now = datetime.now(UTC)
        start = created_at
        if getattr(start, "tzinfo", None) is None:
            start = start.replace(tzinfo=UTC)
        duration = max(0.0, (now - start).total_seconds())

    metrics.record_job_terminal(script_format, label, duration)


@activity.defn(name="update_job_status")
async def update_job_status_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Update job status and optional fields in the database.

    Accepts: job_id, status, and optionally error_message, progress_percentage.
    """
    job_id = job_data.get("job_id", "")
    new_status = job_data.get("status", "")
    error_message = job_data.get("error_message")
    progress = job_data.get("progress_percentage")

    if not job_id or not new_status:
        return {"updated": False, "reason": "missing job_id or status"}

    logger.info("Updating job %s -> %s (progress=%s)", job_id, new_status, progress)

    try:
        from uuid import UUID as UUIDType

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from api.config import get_settings
        from core.db_models import JobMetadata
        from core.models import JobStatus

        settings = get_settings()
        engine = create_async_engine(str(settings.database_url))
        Session = async_sessionmaker(engine, expire_on_commit=False)

        values: dict[str, Any] = {"status": JobStatus(new_status)}
        if error_message is not None:
            values["error_message"] = str(error_message)[:1000]
        if progress is not None:
            values["progress_percentage"] = int(progress)

        terminal = new_status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value)

        async with Session() as session:
            from sqlalchemy import select, update

            script_format = "unknown"
            created_at = None
            if terminal:
                row = (
                    await session.execute(
                        select(JobMetadata.script_format, JobMetadata.created_at).where(
                            JobMetadata.job_id == UUIDType(job_id)
                        )
                    )
                ).first()
                if row is not None:
                    script_format = str(getattr(row[0], "value", row[0]) or "unknown")
                    created_at = row[1]

            await session.execute(
                update(JobMetadata).where(JobMetadata.job_id == UUIDType(job_id)).values(**values)
            )
            await session.commit()

        await engine.dispose()

        if terminal:
            _record_job_terminal_metric(
                script_format=script_format,
                status=new_status,
                error_message=error_message,
                created_at=created_at,
            )
        return {"updated": True}

    except Exception as exc:
        logger.warning("Failed to update job status in DB: %s", exc)
        return {"updated": False, "reason": str(exc)}


# ===================================================================
# Shared Activities (used by both FDX and PDF workflows)
# ===================================================================


async def _build_kb_context(*, scene_text: str, settings: Any) -> str:
    """Return KB context string for the risk-analysis prompt.

    Strictly opt-in:

    * Returns ``"(none)"`` when ``KB_RETRIEVAL_ENABLED=false`` (default).
    * Returns ``"(none)"`` if scene_text is empty.
    * Returns ``"(none)"`` on ANY failure during KB lookup, so a flaky
      DB or empty KB never breaks the risk activity.  Failures are logged
      at WARNING level for operators.

    When successful, returns ``"[Title] chunk_text_excerpt\\n\\n[...]"``
    with at most ``settings.kb_top_k`` snippets, each truncated to
    ``settings.kb_max_chunk_chars_in_prompt`` characters.
    """
    if not getattr(settings, "kb_retrieval_enabled", False):
        return "(none)"
    if not scene_text or not scene_text.strip():
        return "(none)"

    try:
        from uuid import UUID as UUIDType

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from llm.factory import get_llm_provider
        from services.knowledge_base import KnowledgeBaseService

        engine = create_async_engine(str(settings.database_url))
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            kb = KnowledgeBaseService(
                db=session,
                llm=get_llm_provider(settings),
                secret_key=settings.api_secret_key,
            )
            hits = await kb.search(
                query_text=scene_text,
                tenant_id=UUIDType(settings.kb_default_tenant_id),
                top_k=settings.kb_top_k,
            )

        await engine.dispose()

        if not hits:
            return "(none)"

        max_chars = settings.kb_max_chunk_chars_in_prompt
        parts = []
        for h in hits:
            snippet = (h.chunk_text or "").strip()
            if len(snippet) > max_chars:
                snippet = snippet[:max_chars].rstrip() + "..."
            parts.append(f"[{h.title}] {snippet}")
        logger.info("KB retrieval: %d hits used for scene context", len(hits))
        metrics.KB_RETRIEVAL_HITS_TOTAL.inc(len(hits))
        return "\n\n".join(parts)

    except Exception as exc:
        logger.warning(
            "KB retrieval failed (non-fatal, falling back to no-KB context): %s",
            exc,
        )
        return "(none)"


_RISK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "risk_class": {
                        "type": "string",
                        "description": "Risk class code from taxonomy (e.g. FIRE, HEIGHT, STUNTS)",
                    },
                    "rule_id": {
                        "type": "string",
                        "description": "Rule ID from taxonomy (e.g. SEC-P-008)",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["PHYSICAL", "ENVIRONMENTAL", "PSYCHOLOGICAL"],
                    },
                    "likelihood": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Likelihood 1-5",
                    },
                    "impact": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Impact severity 1-5",
                    },
                    "description": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "measure_codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Applicable measure codes from the catalog",
                    },
                    "confidence": {"type": "number"},
                    "evidence": {
                        "type": "string",
                        "description": "Direct quote or excerpt from the scene text that triggers this risk, or rule rationale",
                    },
                    "vulnerability": {
                        "type": "string",
                        "description": "Vulnerability factors: children, animals, stunt doubles, elderly, or 'none'",
                    },
                    "complexity": {
                        "type": "string",
                        "description": "Complexity factors: extras, multi-camera, water/fire combination, night shoot, or 'none'",
                    },
                    "exposure_duration": {
                        "type": "string",
                        "description": "Estimated duration of risk exposure: brief, several hours, full shooting day, or multi-day",
                    },
                },
                "required": [
                    "risk_class",
                    "category",
                    "likelihood",
                    "impact",
                    "description",
                    "recommendation",
                    "evidence",
                ],
            },
        },
    },
    "required": ["findings"],
}


@activity.defn(name="analyze_scene_risk")
async def analyze_scene_risk_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Analyze a single scene for safety risks using LLM with taxonomy context.

    The LLM receives the full risk taxonomy and measures catalog in the prompt.
    It returns structured findings with risk_class, likelihood, impact, and
    measure_codes.  The TaxonomyManager then validates and enriches the results:
    - Calculates severity from likelihood x impact
    - Resolves measure codes to full measure objects
    - Fills in missing rule_ids from the taxonomy
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
        from services.taxonomy import get_taxonomy_manager

        settings = get_settings()
        llm = get_llm_provider(settings)
        pm = get_prompt_manager()
        taxonomy = get_taxonomy_manager()

        # KB retrieval (M06): strictly opt-in via KB_RETRIEVAL_ENABLED.
        # When the flag is off, no KB code path is taken, so the risk
        # activity behaves byte-identically to M05.
        kb_context_str = await _build_kb_context(
            scene_text=scene.get("text", ""),
            settings=settings,
        )

        # Build prompt with taxonomy + (optional) KB context
        system_prompt, user_prompt = pm.get(
            "risk_analysis",
            "scene",
            scene_number=scene_number,
            location=scene.get("location", "UNKNOWN"),
            location_type=scene.get("location_type", "UNKNOWN"),
            time_of_day=scene.get("time_of_day", "UNKNOWN"),
            scene_text=scene.get("text", ""),
            taxonomy_context=taxonomy.summary_for_prompt(),
            kb_context=kb_context_str,
        )

        result = await llm.generate_structured(
            prompt=user_prompt,
            schema=_RISK_SCHEMA,
            system_prompt=system_prompt,
            temperature=0.2,
        )

        findings = result.get("findings", [])
        from uuid import uuid4

        enriched_findings = []
        for f in findings:
            # Validate and enrich via TaxonomyManager
            f = taxonomy.validate_finding(f)
            f["id"] = str(uuid4())
            f["scene_number"] = scene_number
            f["line_reference"] = None
            if "confidence" not in f or not f["confidence"]:
                f["confidence"] = 0.8

            # Ensure Pflichtenheft context fields have defaults
            if "evidence" not in f:
                f["evidence"] = ""
            if "vulnerability" not in f:
                f["vulnerability"] = ""
            if "complexity" not in f:
                f["complexity"] = ""
            if "exposure_duration" not in f:
                f["exposure_duration"] = ""

            # Convert measures to serializable dicts
            measures = f.get("measures", [])
            f["measures"] = [
                {
                    "code": m.get("code", ""),
                    "title": m.get("title", ""),
                    "responsible": m.get("responsible", ""),
                    "due": m.get("due", ""),
                }
                for m in measures
            ]
            enriched_findings.append(f)

        logger.info(
            "Scene %s: %d findings (taxonomy-enriched)", scene_number, len(enriched_findings)
        )
        metrics.SCENES_PROCESSED_TOTAL.labels(stage="risk").inc()
        for f in enriched_findings:
            metrics.FINDINGS_TOTAL.labels(severity=str(f.get("risk_level", "unknown"))).inc()
        return {
            "scene_index": scene_index,
            "scene_number": scene_number,
            "findings": enriched_findings,
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
    """Aggregate per-scene findings into a complete SecurityReport with PDF.

    Builds a full report dict, generates a PDF, and stores both in Redis.
    """
    all_findings = job_data.get("all_findings", [])
    parsed_ref_key = job_data.get("parsed_ref_key", "")
    logger.info("Aggregating report from %d scene analyses", len(all_findings))

    buffer = _get_buffer()

    # Flatten findings from all scenes
    flat_findings = []
    for scene_result in all_findings:
        for finding in scene_result.get("findings", []):
            flat_findings.append(finding)

    # Build report using the report generator
    from services.report_generator import build_report_dict, generate_pdf_base64

    report = build_report_dict(
        report_id=job_metadata.get("report_id", ""),
        project_id=job_metadata.get("project_id", ""),
        script_format=job_metadata.get("script_format", "fdx"),
        findings=flat_findings,
        processing_time_seconds=1.0,
    )

    # Generate PDF
    try:
        pdf_b64 = generate_pdf_base64(report)
        logger.info("PDF report generated (%d chars base64)", len(pdf_b64))
    except Exception as exc:
        logger.warning("PDF generation failed: %s", exc)
        pdf_b64 = None

    # Store report + PDF in Redis for retrieval
    report_package = {
        "report": report,
        "pdf_base64": pdf_b64,
    }
    report_ref = await buffer.store(report_package)

    # Clean up parsed data
    if parsed_ref_key:
        await buffer.delete(parsed_ref_key)

    return {
        "report_ref_key": report_ref,
        "report_id": job_metadata.get("report_id"),
        "total_findings": len(flat_findings),
    }


# ===================================================================
# M10 - Delivery bookkeeping helpers (metadata only)
# ===================================================================

# HTTP status codes that are transient by contract and therefore retried
# within the 6h window even though they are 4xx.
_RETRYABLE_4XX = frozenset({408, 425, 429})


def _current_attempt() -> int:
    """Temporal attempt number (1-based); 1 when called outside an activity."""
    try:
        if activity.in_activity():
            return int(activity.info().attempt)
    except Exception:  # nosec B110
        pass
    return 1


def _session_factory() -> tuple[Any, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    settings = get_settings()
    engine = create_async_engine(str(settings.database_url))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _mark_delivering(
    *,
    report_id: str,
    job_id: str,
    project_id: str,
    user_id: str,
    script_format: str,
    total_findings: int,
    report_ref_key: str,
    delivery_mode: str,
) -> None:
    """Create ReportMetadata (idempotent) and put the job into DELIVERING.

    M10 fix: before, the job was flagged COMPLETED *before* the push was even
    attempted and ReportMetadata was re-inserted on every retry (IntegrityError
    swallowed). Now the insert is skipped when the row exists and the job only
    reaches COMPLETED via ``_record_attempt(delivered=True)``.
    """
    from datetime import UTC, datetime
    from uuid import UUID as UUIDType

    from sqlalchemy import select, update

    from core.db_models import JobMetadata, ReportMetadata
    from core.models import JobStatus, ScriptFormat

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            processing_seconds = 1.0
            if job_id:
                created = (
                    await session.execute(
                        select(JobMetadata.created_at).where(JobMetadata.job_id == UUIDType(job_id))
                    )
                ).scalar_one_or_none()
                if created is not None:
                    start = created if created.tzinfo else created.replace(tzinfo=UTC)
                    processing_seconds = max(0.0, (datetime.now(UTC) - start).total_seconds())

            if report_id:
                exists = (
                    await session.execute(
                        select(ReportMetadata.report_id).where(
                            ReportMetadata.report_id == UUIDType(report_id)
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    session.add(
                        ReportMetadata(
                            report_id=UUIDType(report_id),
                            job_id=UUIDType(job_id) if job_id else None,
                            project_id=project_id,
                            user_id=user_id,
                            script_format=ScriptFormat(script_format),
                            total_findings=total_findings,
                            processing_time_seconds=round(processing_seconds, 3),
                            report_ref_key=report_ref_key,
                            delivery_mode=delivery_mode,
                        )
                    )

            if job_id:
                await session.execute(
                    update(JobMetadata)
                    .where(JobMetadata.job_id == UUIDType(job_id))
                    .values(
                        status=JobStatus.DELIVERING,
                        delivery_status="delivering",
                        report_id=UUIDType(report_id) if report_id else None,
                    )
                )
            await session.commit()
    finally:
        await engine.dispose()


async def _record_attempt(
    *,
    job_id: str,
    attempt: int,
    status_code: int | None,
    delivered: bool,
    delivery_mode: str,
) -> None:
    """Persist one delivery attempt (count, last status code, timestamps)."""
    from datetime import UTC, datetime
    from uuid import UUID as UUIDType

    from sqlalchemy import update

    from core.db_models import JobMetadata
    from core.models import JobStatus

    if not job_id:
        return

    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "delivery_attempts": attempt,
        "delivery_last_status_code": status_code,
        "delivery_last_attempt_at": now,
    }
    if delivered:
        values.update(
            status=JobStatus.COMPLETED,
            progress_percentage=100,
            delivered_at=now,
            # Pull: the report is *ready*; "delivered" is set by the one-shot GET.
            delivery_status="delivered" if delivery_mode == "push" else "pending",
        )

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            await session.execute(
                update(JobMetadata).where(JobMetadata.job_id == UUIDType(job_id)).values(**values)
            )
            await session.commit()
    finally:
        await engine.dispose()


@activity.defn(name="deliver_report")
async def deliver_report_activity(
    report_data: dict[str, Any], delivery_config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Deliver report via Push or Pull mode.

    Pull (default): Report stays in Redis for One-Shot-GET retrieval.
    Push: Report is POSTed to ePro, then deleted from Redis.

    M10 hardening on top of M08:
    * job goes PENDING/RUNNING -> DELIVERING -> COMPLETED only after a real 2xx,
    * ReportMetadata insert is idempotent across Temporal retries,
    * every attempt is persisted (``delivery_attempts``, last status code),
    * ``Idempotency-Key`` (= report_id) and ``X-Request-ID`` headers are sent
      so ePro can de-duplicate retried pushes (Pflichtenheft Abnahmetest 5),
    * 408/425/429 are treated as retryable, other 4xx as hard failures.
    """
    report_ref_key = report_data.get("report_ref_key", "")
    report_id = report_data.get("report_id", "")
    delivery_config = delivery_config or {}
    delivery_mode = delivery_config.get("delivery_mode", "pull")
    job_id = delivery_config.get("job_id", "")
    project_id = delivery_config.get("project_id", "")
    user_id = delivery_config.get("user_id", "")
    script_format = delivery_config.get("script_format", "fdx")
    attempt = _current_attempt()

    logger.info("Delivering report %s (mode=%s, attempt=%d)", report_id, delivery_mode, attempt)

    buffer = _get_buffer()

    try:
        await _mark_delivering(
            report_id=str(report_id or ""),
            job_id=str(job_id or ""),
            project_id=project_id,
            user_id=user_id,
            script_format=script_format,
            total_findings=int(report_data.get("total_findings", 0) or 0),
            report_ref_key=report_ref_key,
            delivery_mode=delivery_mode,
        )
    except Exception as exc:
        logger.warning("DB update failed (non-fatal): %s", type(exc).__name__)

    async def _persist_attempt(status_code: int | None, delivered: bool) -> None:
        try:
            await _record_attempt(
                job_id=str(job_id or ""),
                attempt=attempt,
                status_code=status_code,
                delivered=delivered,
                delivery_mode=delivery_mode,
            )
        except Exception as exc:
            logger.warning("Attempt bookkeeping failed (non-fatal): %s", type(exc).__name__)

    if delivery_mode == "push":
        # Push: multipart POST to ePro set-risk-assessment, then delete from Redis.
        #
        # Fehlerklassifikation (M08 + M10):
        #   * 2xx                    -> Erfolg, Buffer wird geloescht.
        #   * 408/425/429            -> transient, raise -> Temporal-Retry (6h-Fenster).
        #   * sonstige 4xx (Hard)    -> kein Retry; ``delivered=False, hard_fail=True``.
        #   * 5xx / 3xx              -> transient, raise -> Temporal-Retry.
        #   * Transport-Errors       -> transient, raise -> Temporal-Retry.
        import base64 as b64mod

        import httpx

        from api.config import get_settings
        from core.logging_config import get_request_id
        from services.report_generator import compute_epro_status, generate_assessment_text

        settings = get_settings()
        report_package = await buffer.retrieve(report_ref_key)
        report = report_package.get("report", {})
        pdf_b64 = report_package.get("pdf_base64", "")

        epro_status = compute_epro_status(report)
        assessment = generate_assessment_text(report)
        script_id = delivery_config.get("script_id")
        if script_id is None:
            script_id = -1

        push_url = f"{settings.epro_base_url}/eki/scl/set-risk-assessment/{project_id}"

        form_data = {
            "script_id": str(int(script_id)),
            "status": str(epro_status),
            "assessment": assessment,
        }

        files = {}
        if pdf_b64:
            pdf_bytes = b64mod.b64decode(pdf_b64)
            files["file"] = (
                f"safety-check-result_{project_id}.pdf",
                pdf_bytes,
                "application/pdf",
            )
            # Log-Hygiene (M08, Abnahmetest 7): kein Inhalt aus assessment
            # oder pdf_bytes wird geloggt -- nur Laengen/Counts.
            logger.info(
                "Push payload: script_id=%s, status=%s, assessment=%d chars, PDF=%d bytes",
                script_id,
                epro_status,
                len(assessment),
                len(pdf_bytes),
            )
        else:
            logger.warning(
                "Push payload: script_id=%s, status=%s, assessment=%d chars, PDF=MISSING",
                script_id,
                epro_status,
                len(assessment),
            )

        # M10: Idempotenz-Header. report_id ist pro Job stabil -> ePro kann
        # wiederholte Pushes (Temporal-Retry nach Timeout) als Duplikat erkennen.
        headers: dict[str, str] = {
            "Idempotency-Key": str(report_id),
            "X-EKI-Job-Id": str(job_id),
            "X-EKI-Attempt": str(attempt),
        }
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id
        if settings.epro_auth_token:
            headers["Authorization"] = f"Bearer {settings.epro_auth_token}"

        push_started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=settings.epro_timeout) as client:
                response = await client.post(
                    push_url,
                    data=form_data,
                    files=files if files else None,
                    headers=headers,
                )
            metrics.DELIVERY_DURATION.labels(mode="push").observe(
                time.perf_counter() - push_started
            )

            status_code = response.status_code

            if 200 <= status_code < 300:
                try:
                    epro_body = response.json()
                except Exception:
                    epro_body = {}
                logger.info(
                    "Push delivery succeeded: HTTP %d attempt=%d -- msg=%s",
                    status_code,
                    attempt,
                    str(epro_body.get("message", ""))[:120],
                )

                # Pflichtenheft Abnahmetest 2: Buffer-Cleanup direkt nach 2xx.
                await buffer.delete(report_ref_key)
                metrics.BUFFER_DELETES_TOTAL.labels(source="push").inc()
                metrics.DELIVERY_ATTEMPTS_TOTAL.labels(mode="push", outcome="success").inc()
                await _persist_attempt(status_code, delivered=True)

                return {
                    "delivered": True,
                    "delivery_mode": "push",
                    "epro_status": epro_status,
                    "status_code": status_code,
                    "attempts_used": attempt,
                    "epro_response": epro_body,
                }

            if 400 <= status_code < 500 and status_code not in _RETRYABLE_4XX:
                # Hard-Fail: kein Temporal-Retry. Workflow-Failure-Branch
                # uebernimmt Buffer-Cleanup, Job-Status, Dead Letter und Webhook.
                logger.error(
                    "Push delivery hard-fail (HTTP %d, no retry): job=%s attempt=%d",
                    status_code,
                    job_id,
                    attempt,
                )
                metrics.DELIVERY_ATTEMPTS_TOTAL.labels(mode="push", outcome="hard_fail").inc()
                await _persist_attempt(status_code, delivered=False)
                return {
                    "delivered": False,
                    "delivery_mode": "push",
                    "hard_fail": True,
                    "status_code": status_code,
                    "error": f"HTTP {status_code}",
                    "attempts_used": attempt,
                }

            # 5xx / 3xx / retryable 4xx -> transient, raise damit RetryPolicy greift.
            logger.warning(
                "Push delivery transient failure, will retry (HTTP %d): job=%s attempt=%d",
                status_code,
                job_id,
                attempt,
            )
            metrics.DELIVERY_ATTEMPTS_TOTAL.labels(mode="push", outcome="retryable").inc()
            await _persist_attempt(status_code, delivered=False)
            response.raise_for_status()
            # Defensiv: falls raise_for_status nichts wirft (3xx, 408/429 ohne
            # raise), explizit selbst raisen.
            raise httpx.HTTPStatusError(
                f"unexpected status {status_code}",
                request=response.request,
                response=response,
            )

        except httpx.HTTPStatusError:
            raise
        except Exception as exc:
            # Transport-Fehler (DNS, Timeout, Connection-Reset).
            # Log NICHT den Exception-Text, weil der durch ePro
            # einen Auszug aus Payload-Bytes enthalten koennte. Nur Typ.
            logger.warning(
                "Push delivery transport error, will retry: job=%s attempt=%d exc_type=%s",
                job_id,
                attempt,
                type(exc).__name__,
            )
            metrics.DELIVERY_ATTEMPTS_TOTAL.labels(mode="push", outcome="transport_error").inc()
            await _persist_attempt(None, delivered=False)
            raise

    else:
        # Pull: Report stays in Redis for One-Shot-GET
        # (already stored by aggregate_report_activity)
        logger.info("Pull mode: report %s available for One-Shot-GET (TTL 6h)", report_id)
        metrics.DELIVERY_ATTEMPTS_TOTAL.labels(mode="pull", outcome="pull_ready").inc()
        await _persist_attempt(None, delivered=True)
        return {
            "delivered": True,
            "delivery_mode": "pull",
            "attempts_used": attempt,
            "report_url": f"/v1/security/reports/{report_id}",
        }


# ===================================================================
# M10 - Dead Letter + Pull-TTL-Watch
# ===================================================================


@activity.defn(name="record_dead_letter")
async def record_dead_letter_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a content-free dead letter for a definitively failed delivery.

    Also flips ``job_metadata.delivery_status`` to ``dead_lettered`` and
    refreshes the ``eki_dead_letters_unacknowledged`` gauge. Never raises.
    """
    from uuid import UUID as UUIDType

    from sqlalchemy import func, select, update

    from core.db_models import DeliveryDeadLetter, JobMetadata

    job_id = str(payload.get("job_id") or "")
    reason = str(payload.get("reason") or "delivery_failed")
    if not job_id:
        return {"recorded": False, "reason": "missing job_id"}

    metrics.DELIVERY_FAILURES_TOTAL.labels(reason=reason).inc()

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            job = (
                await session.execute(
                    select(JobMetadata).where(JobMetadata.job_id == UUIDType(job_id))
                )
            ).scalar_one_or_none()

            report_id_raw = payload.get("report_id") or (job.report_id if job else None)
            record = DeliveryDeadLetter(
                job_id=UUIDType(job_id),
                report_id=UUIDType(str(report_id_raw)) if report_id_raw else None,
                project_id=(job.project_id if job else payload.get("project_id")) or "unknown",
                user_id=(job.user_id if job else payload.get("user_id")) or "unknown",
                delivery_mode=str(
                    payload.get("delivery_mode") or (job.delivery_mode if job else "push")
                ),
                reason=reason[:50],
                attempts=int(payload.get("attempts") or (job.delivery_attempts if job else 0) or 0),
                last_status_code=payload.get("last_status_code")
                or (job.delivery_last_status_code if job else None),
                last_error_type=(str(payload.get("last_error_type") or "")[:100] or None),
                webhook_sent=bool(payload.get("webhook_sent", False)),
            )
            session.add(record)
            await session.execute(
                update(JobMetadata)
                .where(JobMetadata.job_id == UUIDType(job_id))
                .values(delivery_status="dead_lettered")
            )
            await session.commit()

            unacked = (
                await session.execute(
                    select(func.count())
                    .select_from(DeliveryDeadLetter)
                    .where(DeliveryDeadLetter.acknowledged_at.is_(None))
                )
            ).scalar_one()
            metrics.DEAD_LETTERS_UNACKNOWLEDGED.set(int(unacked))
            dead_letter_id = str(record.id)
    except Exception as exc:
        logger.error(
            "record_dead_letter_activity failed (non-fatal): job=%s exc_type=%s",
            job_id,
            type(exc).__name__,
        )
        return {"recorded": False, "reason": type(exc).__name__}
    finally:
        await engine.dispose()

    logger.warning(
        "Delivery dead-lettered: job=%s reason=%s attempts=%s dead_letter_id=%s",
        job_id,
        reason,
        payload.get("attempts"),
        dead_letter_id,
    )
    return {"recorded": True, "dead_letter_id": dead_letter_id}


@activity.defn(name="check_report_retrieved")
async def check_report_retrieved_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Return whether a pull-mode report has been fetched via one-shot GET."""
    from uuid import UUID as UUIDType

    from sqlalchemy import select

    from core.db_models import ReportMetadata

    report_id = str(payload.get("report_id") or "")
    if not report_id:
        return {"found": False, "retrieved": False}

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(ReportMetadata.is_retrieved).where(
                        ReportMetadata.report_id == UUIDType(report_id)
                    )
                )
            ).scalar_one_or_none()
    finally:
        await engine.dispose()

    if row is None:
        return {"found": False, "retrieved": False}
    return {"found": True, "retrieved": bool(row)}


# ===================================================================
# M08 - Buffer-Cleanup nach endgueltigem Delivery-Fehler
# Wird vom Workflow-Failure-Branch aufgerufen, wenn der 6h-Retry erschoepft
# ist oder ein 4xx-Hard-Fail die Zustellung beendet hat. Garantiert die
# Pflichtenheft-Forderung "keine Inhalte in der eKI nach Abschluss".
# ===================================================================


@activity.defn(name="cleanup_buffer")
async def cleanup_buffer_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Explicitly delete one or more SecureBuffer keys.

    Defensiv: leere oder fehlende Keys werden uebersprungen. Schlaegt die
    eigentliche Redis-Loeschung fehl, wird das nur geloggt; der
    Workflow-Failure-Branch laeuft auch dann weiter, weil der Redis-TTL
    (6h) als sicheres Netz wirkt.
    """
    ref_keys_raw = payload.get("ref_keys") or []
    if isinstance(ref_keys_raw, str):
        ref_keys_raw = [ref_keys_raw]
    ref_keys = [k for k in ref_keys_raw if k]

    if not ref_keys:
        return {"deleted": 0, "reason": "no_keys"}

    buffer = _get_buffer()
    try:
        count = await buffer.delete(*ref_keys)
    except Exception as exc:
        logger.warning(
            "cleanup_buffer_activity: delete failed (non-fatal, "
            "TTL will reap eventually): keys=%d exc_type=%s",
            len(ref_keys),
            type(exc).__name__,
        )
        return {"deleted": 0, "reason": "delete_error"}

    logger.info(
        "cleanup_buffer_activity: removed %d/%d keys",
        count,
        len(ref_keys),
    )
    metrics.BUFFER_DELETES_TOTAL.labels(source="cleanup").inc(int(count))
    return {"deleted": int(count)}


# ===================================================================
# M08 - security.delivery.failed Webhook (opt-in)
# Pflichtenheft Anhang 1 + Abnahmetest 4: Bei dauerhaft gescheiterter
# Zustellung oder Ablauf des 6h-Retry-Fensters wird ein
# Metadaten-Webhook an ePro abgesetzt. Default ist OFF, d.h. ohne
# konfigurierte EPRO_WEBHOOK_URL ist die Activity ein No-Op-Log.
# ===================================================================


@activity.defn(name="send_delivery_failed_webhook")
async def send_delivery_failed_webhook_activity(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST a security.delivery.failed metadata webhook to ePro.

    Pflichtenheft Anhang 1 schreibt vor:
        {"job_id", "report_id", "reason", "attempts"}

    Diese Activity ist defensiv:
    * No-Op, wenn EPRO_WEBHOOK_URL leer ist.
    * Wirft NIE -- ein Webhook-Fehler darf den Workflow nicht erneut
      faillen lassen. Stattdessen wird der Fehler nur geloggt und in
      der Rueckgabe als ``sent=False`` markiert. Temporal-Retry des
      Webhook-Calls erfolgt ausschliesslich ueber die hier konfigurierte
      kleine Retry-Schleife innerhalb des Webhook-Requests.
    * Schreibt KEINE Reportinhalte ins Log (Log-Hygiene, Test 7) --
      nur job_id, report_id, reason, attempts und HTTP-Statuscode.
    """
    import httpx

    from api.config import get_settings

    settings = get_settings()
    webhook_url = (settings.epro_webhook_url or "").strip()

    job_id = payload.get("job_id") or ""
    report_id = payload.get("report_id") or ""
    reason = payload.get("reason") or "unknown"
    attempts = int(payload.get("attempts") or 0)

    body = {
        "job_id": str(job_id),
        "report_id": str(report_id),
        "reason": str(reason),
        "attempts": attempts,
    }

    if not webhook_url:
        logger.info(
            "delivery.failed webhook skipped (EPRO_WEBHOOK_URL unset): "
            "job_id=%s report_id=%s reason=%s attempts=%d",
            job_id,
            report_id,
            reason,
            attempts,
        )
        metrics.WEBHOOK_SENT_TOTAL.labels(outcome="disabled").inc()
        return {"sent": False, "reason": "no_webhook_url"}

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.epro_auth_token:
        headers["Authorization"] = f"Bearer {settings.epro_auth_token}"

    last_error: str | None = None
    last_status: int | None = None

    # Kleine interne Retry-Schleife (drei Versuche, kurzer Backoff).
    # Bewusst kein workflow.execute_activity retry, damit dieser Pfad
    # nie selbst zur Endlos-Schleife wird. Falls Temporal die Activity
    # spaeter ein zweites Mal aufruft, ist das idempotent (gleiche
    # Payload, ePro-Seite muss idempotent reagieren).
    import asyncio

    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=settings.epro_timeout) as client:
                response = await client.post(webhook_url, json=body, headers=headers)
            last_status = response.status_code
            if 200 <= response.status_code < 300:
                logger.info(
                    "delivery.failed webhook delivered: job_id=%s "
                    "report_id=%s status=%d attempt=%d",
                    job_id,
                    report_id,
                    response.status_code,
                    attempt,
                )
                metrics.WEBHOOK_SENT_TOTAL.labels(outcome="sent").inc()
                return {
                    "sent": True,
                    "status_code": response.status_code,
                    "attempts_used": attempt,
                }
            # Non-2xx -> retry
            last_error = f"HTTP {response.status_code}"
            logger.warning(
                "delivery.failed webhook non-2xx response: job_id=%s status=%d attempt=%d",
                job_id,
                response.status_code,
                attempt,
            )
        except Exception as exc:
            last_error = type(exc).__name__
            logger.warning(
                "delivery.failed webhook transport error: job_id=%s error=%s attempt=%d",
                job_id,
                last_error,
                attempt,
            )
        if attempt < 3:
            await asyncio.sleep(2 ** (attempt - 1))

    logger.error(
        "delivery.failed webhook gave up after 3 attempts: job_id=%s "
        "report_id=%s last_error=%s last_status=%s",
        job_id,
        report_id,
        last_error,
        last_status,
    )
    metrics.WEBHOOK_SENT_TOTAL.labels(outcome="failed").inc()
    return {
        "sent": False,
        "status_code": last_status,
        "error": last_error,
        "attempts_used": 3,
    }
