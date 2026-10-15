"""Final Draft XML (.fdx) parser.

Extracts scenes, characters, dialogue and action text from FDX files using
``defusedxml`` for secure XML parsing.
"""

import logging
import time
from collections import defaultdict
from uuid import uuid4

# Element is used for type annotations only; all parsing goes through defusedxml.
from xml.etree.ElementTree import Element  # nosec B405

from core.exceptions import ParsingException
from core.models import (
    CharacterInfo,
    DialogueLine,
    LocationType,
    ParsedScene,
    ParsedScript,
    ScriptFormat,
    TimeOfDay,
)
from parsers.base import ParserBase
from parsers.scene_heading import parse_scene_heading
from parsers.secure_xml import parse_xml_safe

logger = logging.getLogger(__name__)

# FDX Paragraph types we care about
_SCENE_HEADING = "Scene Heading"
_ACTION = "Action"
_CHARACTER = "Character"
_DIALOGUE = "Dialogue"
_PARENTHETICAL = "Parenthetical"
_TRANSITION = "Transition"
_SHOT = "Shot"
_GENERAL = "General"

# Content that is not attached to a scene heading is never dropped (a dropped
# block would simply not be risk-analysed); it becomes a low-confidence
# fallback scene instead, analogous to the PDF page fallback.
_FALLBACK_CONFIDENCE = 0.3
_FALLBACK_METHOD = "fdx_fallback"
_NO_HEADING_LABEL = "(no scene heading)"
_PRE_HEADING_LABEL = "(before first scene heading)"


def _paragraph_text(para: Element) -> str:
    """Extract the full text content of a ``<Paragraph>`` element.

    A paragraph may contain multiple ``<Text>`` children (style runs, e.g.
    when bold and plain text are interleaved).  Final Draft stores the
    whitespace inside the runs, so they are concatenated verbatim --
    ``<Text>Explo</Text><Text Style="Bold">sion</Text>`` is "Explosion".
    """
    parts: list[str] = []
    for text_el in para.findall("Text"):
        if text_el.text:
            parts.append(text_el.text)
    return "".join(parts).strip()


class FDXParser(ParserBase):
    """Parser for Final Draft XML (.fdx) screenplay files."""

    @property
    def supported_format(self) -> ScriptFormat:
        return ScriptFormat.FDX

    async def parse(self, content: bytes) -> ParsedScript:
        """Parse raw FDX bytes into a ``ParsedScript``."""
        t0 = time.monotonic()

        root = parse_xml_safe(content)
        self._validate_fdx_root(root)

        title = self._extract_title(root)
        paragraphs = self._extract_paragraphs(root)
        scenes, warnings = self._build_scenes(paragraphs)
        characters = self._build_character_index(scenes)
        overall_confidence = (
            sum(s.parse_confidence for s in scenes) / len(scenes) if scenes else 0.0
        )

        elapsed = time.monotonic() - t0
        logger.info(
            "FDX parsed: %d scenes, %d characters in %.2fs",
            len(scenes),
            len(characters),
            elapsed,
        )

        return ParsedScript(
            script_id=uuid4(),
            title=title,
            format=ScriptFormat.FDX,
            total_scenes=len(scenes),
            scenes=scenes,
            characters=characters,
            parsing_time_seconds=round(elapsed, 3),
            overall_confidence=round(overall_confidence, 3),
            warnings=warnings,
            metadata={"parser": "fdx", "paragraph_count": len(paragraphs)},
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_fdx_root(root: Element) -> None:
        """Ensure the root element looks like a valid FDX document."""
        if root.tag != "FinalDraft":
            raise ParsingException(
                f"Not a valid FDX file: expected root <FinalDraft>, got <{root.tag}>",
                details={"root_tag": root.tag},
            )
        content = root.find("Content")
        if content is None:
            raise ParsingException(
                "FDX file has no <Content> element",
                details={"root_tag": root.tag},
            )

    @staticmethod
    def _extract_title(root: Element) -> str | None:
        """Try to extract the script title from FDX TitlePage or HeaderAndFooter."""
        for tp in root.findall(".//TitlePage//Paragraph"):
            text = _paragraph_text(tp)
            if text:
                return text
        return None

    @staticmethod
    def _extract_paragraphs(root: Element) -> list[Element]:
        """Return the script ``<Paragraph>`` elements under ``<Content>`` in order.

        Final Draft nests side-by-side dialogue as
        ``<Paragraph><DualDialogue><Paragraph .../>...</DualDialogue></Paragraph>``;
        such a container is replaced by its inner paragraphs so dual dialogue
        is not silently dropped.  Other nested paragraphs (e.g. scene
        summaries inside ``<SceneProperties>``) are notes, not script text,
        and are deliberately not included.
        """
        content = root.find("Content")
        if content is None:
            return []
        paragraphs: list[Element] = []
        for para in content.findall("Paragraph"):
            dual = para.find("DualDialogue")
            if dual is not None:
                paragraphs.extend(dual.findall("Paragraph"))
            else:
                paragraphs.append(para)
        return paragraphs

    @classmethod
    def _build_scenes(cls, paragraphs: list[Element]) -> tuple[list[ParsedScene], list[str]]:
        """Walk paragraphs and group them into scenes; returns (scenes, warnings).

        Script content without a heading is kept as a fallback scene:
        * no ``Scene Heading`` at all -> one scene with the whole content,
        * content before the first heading -> a leading scene numbered "0".
        """
        scenes: list[ParsedScene] = []
        warnings: list[str] = []
        leading_paras: list[Element] = []
        current_paras: list[Element] = []
        current_heading_el: Element | None = None

        for para in paragraphs:
            ptype = (para.get("Type") or "").strip()

            if ptype == _SCENE_HEADING:
                # Flush previous scene
                if current_heading_el is not None:
                    scene = cls._finalize_scene(current_heading_el, current_paras)
                    scenes.append(scene)
                current_heading_el = para
                current_paras = []
            elif current_heading_el is None:
                leading_paras.append(para)
            else:
                current_paras.append(para)

        # Flush last scene
        if current_heading_el is not None:
            scenes.append(cls._finalize_scene(current_heading_el, current_paras))

        if cls._has_script_content(leading_paras):
            if scenes:
                scenes.insert(0, cls._fallback_scene(leading_paras, "0", _PRE_HEADING_LABEL))
                warnings.append(
                    "FDX content before the first scene heading was kept as scene 0 "
                    "(location unknown)."
                )
            else:
                scenes.append(cls._fallback_scene(leading_paras, "1", _NO_HEADING_LABEL))
                warnings.append(
                    "FDX contains no scene headings; the whole script was analysed as a "
                    "single scene (location unknown)."
                )
        elif not scenes:
            warnings.append("FDX contains no script content.")

        return scenes, warnings

    @staticmethod
    def _has_script_content(paras: list[Element]) -> bool:
        """True if *paras* hold any text beyond pure transitions (e.g. "FADE IN:")."""
        return any(
            _paragraph_text(p) and (p.get("Type") or "").strip() != _TRANSITION for p in paras
        )

    @classmethod
    def _fallback_scene(cls, body_paras: list[Element], number: str, label: str) -> ParsedScene:
        """Scene for content that has no scene heading (location unknown)."""
        return cls._scene_from_paragraphs(
            heading_text=label,
            number=number,
            location_type=LocationType.UNKNOWN,
            location="UNKNOWN",
            time_of_day=TimeOfDay.UNKNOWN,
            body_paras=body_paras,
            include_heading_in_text=False,
            parse_confidence=_FALLBACK_CONFIDENCE,
            parse_method=_FALLBACK_METHOD,
        )

    @classmethod
    def _finalize_scene(cls, heading_el: Element, body_paras: list[Element]) -> ParsedScene:
        """Build a ``ParsedScene`` from its heading element and body paragraphs."""
        heading_text = _paragraph_text(heading_el)
        hc = parse_scene_heading(heading_text)
        return cls._scene_from_paragraphs(
            heading_text=heading_text,
            number=heading_el.get("Number"),
            location_type=hc.location_type,
            location=hc.location,
            time_of_day=hc.time_of_day,
            body_paras=body_paras,
            include_heading_in_text=True,
        )

    @classmethod
    def _scene_from_paragraphs(
        cls,
        *,
        heading_text: str,
        number: str | None,
        location_type: LocationType,
        location: str,
        time_of_day: TimeOfDay,
        body_paras: list[Element],
        include_heading_in_text: bool,
        parse_confidence: float = 1.0,
        parse_method: str = "fdx",
    ) -> ParsedScene:
        """Separate action, characters, dialogue and parentheticals of one scene."""
        action_parts: list[str] = []
        dialogue_lines: list[DialogueLine] = []
        character_names: list[str] = []
        full_text_parts: list[str] = [heading_text] if include_heading_in_text else []

        current_character: str | None = None
        current_parenthetical: str | None = None

        for para in body_paras:
            ptype = (para.get("Type") or "").strip()
            text = _paragraph_text(para)
            if not text:
                continue

            full_text_parts.append(text)

            if ptype == _ACTION or ptype == _GENERAL:
                action_parts.append(text)
                current_character = None
            elif ptype == _CHARACTER:
                current_character = text.strip()
                current_parenthetical = None
                if current_character and current_character not in character_names:
                    character_names.append(current_character)
            elif ptype == _PARENTHETICAL:
                current_parenthetical = text.strip("() ")
            elif ptype == _DIALOGUE:
                if current_character:
                    dialogue_lines.append(
                        DialogueLine(
                            character=current_character,
                            parenthetical=current_parenthetical,
                            text=text,
                        )
                    )
                    current_parenthetical = None
            elif ptype == _TRANSITION:
                action_parts.append(text)
                current_character = None
            elif ptype == _SHOT:
                action_parts.append(text)
                current_character = None

        return ParsedScene(
            scene_id=uuid4(),
            number=number,
            heading=heading_text,
            location=location,
            location_type=location_type,
            time_of_day=time_of_day,
            characters=character_names,
            action_text="\n".join(action_parts),
            dialogue=dialogue_lines,
            text="\n".join(full_text_parts),
            parse_confidence=parse_confidence,
            parse_method=parse_method,
        )

    @staticmethod
    def _build_character_index(scenes: list[ParsedScene]) -> list[CharacterInfo]:
        """Aggregate character appearances across scenes."""
        appearances: dict[str, list[str]] = defaultdict(list)
        for scene in scenes:
            for name in scene.characters:
                appearances[name].append(str(scene.scene_id))
        return [
            CharacterInfo(name=name, scene_appearances=scene_ids)
            for name, scene_ids in appearances.items()
        ]
