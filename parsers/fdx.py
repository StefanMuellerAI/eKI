"""Final Draft XML (.fdx) parser.

Extracts scenes, characters, dialogue and action text from FDX files using
``defusedxml`` for secure XML parsing.
"""

import logging
import time
from collections import defaultdict
from uuid import uuid4
from xml.etree.ElementTree import Element

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


def _paragraph_text(para: Element) -> str:
    """Extract the full text content of a ``<Paragraph>`` element.

    A paragraph may contain multiple ``<Text>`` children (e.g. when bold
    and plain text are interleaved).  We concatenate them.
    """
    parts: list[str] = []
    for text_el in para.findall("Text"):
        if text_el.text:
            parts.append(text_el.text)
    return " ".join(parts).strip()


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
        scenes = self._build_scenes(paragraphs)
        characters = self._build_character_index(scenes)

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
        """Return all ``<Paragraph>`` elements under ``<Content>``."""
        content = root.find("Content")
        if content is None:
            return []
        return list(content.findall("Paragraph"))

    @classmethod
    def _build_scenes(cls, paragraphs: list[Element]) -> list[ParsedScene]:
        """Walk paragraphs and group them into scenes."""
        scenes: list[ParsedScene] = []
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
            else:
                current_paras.append(para)

        # Flush last scene
        if current_heading_el is not None:
            scenes.append(cls._finalize_scene(current_heading_el, current_paras))

        return scenes

    @classmethod
    def _finalize_scene(
        cls, heading_el: Element, body_paras: list[Element]
    ) -> ParsedScene:
        """Build a ``ParsedScene`` from its heading element and body paragraphs."""
        heading_text = _paragraph_text(heading_el)
        number = heading_el.get("Number")

        # Parse scene heading into components
        hc = parse_scene_heading(heading_text)

        # Separate action, characters, dialogue, parenthetical
        action_parts: list[str] = []
        dialogue_lines: list[DialogueLine] = []
        character_names: list[str] = []
        full_text_parts: list[str] = [heading_text]

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
            location=hc.location,
            location_type=hc.location_type,
            time_of_day=hc.time_of_day,
            characters=character_names,
            action_text="\n".join(action_parts),
            dialogue=dialogue_lines,
            text="\n".join(full_text_parts),
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
