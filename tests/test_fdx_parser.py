"""Tests for the FDX parser, secure XML parsing, scene heading parser, and SecureBuffer."""

import base64
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.exceptions import NotFoundException, ParsingException
from core.models import LocationType, ParsedScript, ScriptFormat, TimeOfDay
from parsers.base import ParserBase, get_parser
from parsers.fdx import FDXParser
from parsers.scene_heading import HeadingComponents, parse_scene_heading
from parsers.secure_xml import parse_xml_safe
from services.secure_buffer import SecureBuffer, _derive_fernet_key

FIXTURES = Path(__file__).parent / "fixtures" / "fdx"


def _read_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ===================================================================
# Scene Heading Parser
# ===================================================================


class TestSceneHeadingParser:
    """Tests for parsers.scene_heading.parse_scene_heading."""

    def test_english_int_day(self):
        result = parse_scene_heading("INT. OFFICE - DAY")
        assert result.location_type == LocationType.INT
        assert result.location == "OFFICE"
        assert result.time_of_day == TimeOfDay.DAY

    def test_english_ext_night(self):
        result = parse_scene_heading("EXT. FOREST - NIGHT")
        assert result.location_type == LocationType.EXT
        assert result.location == "FOREST"
        assert result.time_of_day == TimeOfDay.NIGHT

    def test_english_int_ext_continuous(self):
        result = parse_scene_heading("INT./EXT. CAR - CONTINUOUS")
        assert result.location_type == LocationType.INT_EXT
        assert result.location == "CAR"
        assert result.time_of_day == TimeOfDay.CONTINUOUS

    def test_english_dawn(self):
        result = parse_scene_heading("EXT. BEACH - DAWN")
        assert result.location_type == LocationType.EXT
        assert result.location == "BEACH"
        assert result.time_of_day == TimeOfDay.DAWN

    def test_english_evening(self):
        result = parse_scene_heading("INT. RESTAURANT - EVENING")
        assert result.location_type == LocationType.INT
        assert result.location == "RESTAURANT"
        assert result.time_of_day == TimeOfDay.EVENING

    def test_english_morning(self):
        result = parse_scene_heading("INT. KITCHEN - MORNING")
        assert result.location_type == LocationType.INT
        assert result.location == "KITCHEN"
        assert result.time_of_day == TimeOfDay.MORNING

    def test_german_innen_tag(self):
        result = parse_scene_heading("INNEN. BUERO - TAG")
        assert result.location_type == LocationType.INT
        assert result.location == "BUERO"
        assert result.time_of_day == TimeOfDay.DAY

    def test_german_aussen_nacht(self):
        result = parse_scene_heading("AUSSEN. WALD - NACHT")
        assert result.location_type == LocationType.EXT
        assert result.location == "WALD"
        assert result.time_of_day == TimeOfDay.NIGHT

    def test_german_innen_aussen_daemmerung(self):
        result = parse_scene_heading("INNEN/AUSSEN. AUTO - DAEMMERUNG")
        assert result.location_type == LocationType.INT_EXT
        assert result.location == "AUTO"
        assert result.time_of_day == TimeOfDay.DUSK

    def test_unknown_time(self):
        result = parse_scene_heading("INT. ROOM - SOMETIMEWEIRD")
        assert result.time_of_day == TimeOfDay.UNKNOWN

    def test_no_separator(self):
        result = parse_scene_heading("INT. ROOM")
        assert result.location_type == LocationType.INT
        assert result.location == "ROOM"
        assert result.time_of_day == TimeOfDay.UNKNOWN

    def test_location_with_dash(self):
        result = parse_scene_heading("EXT. KEVIN'S HOUSE - DAY")
        assert result.location == "KEVIN'S HOUSE"
        assert result.time_of_day == TimeOfDay.DAY


# ===================================================================
# Secure XML Parser
# ===================================================================


class TestSecureXML:
    """Tests for parsers.secure_xml.parse_xml_safe."""

    def test_valid_xml(self):
        xml = b"<root><child>text</child></root>"
        el = parse_xml_safe(xml)
        assert el.tag == "root"

    def test_oversized_xml_rejected(self):
        big = b"<r>" + b"x" * (10 * 1024 * 1024 + 1) + b"</r>"
        with pytest.raises(ParsingException, match="exceeds size limit"):
            parse_xml_safe(big)

    def test_xxe_attack_blocked(self):
        content = _read_fixture("xxe_attack.fdx")
        with pytest.raises(ParsingException):
            parse_xml_safe(content)

    def test_entity_bomb_blocked(self):
        content = _read_fixture("entity_bomb.fdx")
        with pytest.raises(ParsingException):
            parse_xml_safe(content)

    def test_malformed_xml_raises(self):
        content = _read_fixture("malformed.fdx")
        with pytest.raises(ParsingException):
            parse_xml_safe(content)


# ===================================================================
# FDX Parser
# ===================================================================


class TestFDXParser:
    """Tests for parsers.fdx.FDXParser."""

    def test_simple_scene(self):
        content = _read_fixture("simple_scene.fdx")
        parser = FDXParser()
        result = parser.parse(content)

        assert isinstance(result, ParsedScript)
        assert result.format == ScriptFormat.FDX
        assert result.total_scenes == 1
        assert result.scenes[0].heading == "INT. OFFICE - DAY"
        assert result.scenes[0].location == "OFFICE"
        assert result.scenes[0].location_type == LocationType.INT
        assert result.scenes[0].time_of_day == TimeOfDay.DAY
        assert result.scenes[0].number == "1"
        assert "ANNA" in result.scenes[0].characters
        assert len(result.scenes[0].dialogue) == 1
        assert result.scenes[0].dialogue[0].character == "ANNA"

    def test_multi_scene(self):
        content = _read_fixture("multi_scene.fdx")
        result = FDXParser().parse(content)

        assert result.total_scenes == 5
        assert result.scenes[0].location == "PARK"
        assert result.scenes[1].location == "RESTAURANT"
        assert result.scenes[2].location == "STREET"
        assert result.scenes[4].time_of_day == TimeOfDay.DAWN

        # David and Maria appear in multiple scenes
        char_names = [c.name for c in result.characters]
        assert "DAVID" in char_names
        assert "MARIA" in char_names

        # Maria has a parenthetical in scene 2
        scene2_dialogue = result.scenes[1].dialogue
        maria_lines = [d for d in scene2_dialogue if d.character == "MARIA"]
        assert any(d.parenthetical == "nervous" for d in maria_lines)

    def test_stunt_heavy(self):
        content = _read_fixture("stunt_heavy.fdx")
        result = FDXParser().parse(content)

        assert result.total_scenes == 4
        assert result.scenes[0].location == "CLIFF EDGE"
        assert result.scenes[1].location == "HIGHWAY"
        assert "explosion" in result.scenes[1].action_text.lower()

    def test_psychological(self):
        content = _read_fixture("psychological.fdx")
        result = FDXParser().parse(content)

        assert result.total_scenes == 4
        # Child has a parenthetical whisper
        scene4_dialogue = result.scenes[3].dialogue
        assert any(d.parenthetical == "whispering" for d in scene4_dialogue)

    def test_german_format(self):
        content = _read_fixture("german_format.fdx")
        result = FDXParser().parse(content)

        assert result.total_scenes == 3
        assert result.scenes[0].location_type == LocationType.INT
        assert result.scenes[0].time_of_day == TimeOfDay.DAY
        assert result.scenes[1].location_type == LocationType.EXT
        assert result.scenes[1].time_of_day == TimeOfDay.NIGHT
        assert result.scenes[2].location_type == LocationType.INT_EXT
        assert result.scenes[2].time_of_day == TimeOfDay.DUSK

    def test_english_format(self):
        content = _read_fixture("english_format.fdx")
        result = FDXParser().parse(content)

        assert result.total_scenes == 3
        assert result.scenes[2].location_type == LocationType.INT_EXT
        assert result.scenes[2].time_of_day == TimeOfDay.CONTINUOUS
        # Parenthetical
        jones_lines = result.scenes[2].dialogue
        assert any(d.parenthetical == "into radio" for d in jones_lines)

    def test_empty_scenes(self):
        content = _read_fixture("empty_scenes.fdx")
        result = FDXParser().parse(content)

        assert result.total_scenes == 3
        for scene in result.scenes:
            assert scene.characters == []
            assert scene.dialogue == []
            assert scene.action_text == ""

    def test_no_scene_numbers(self):
        content = _read_fixture("no_scene_numbers.fdx")
        result = FDXParser().parse(content)

        assert result.total_scenes == 2
        assert result.scenes[0].number is None
        assert result.scenes[1].number is None
        assert result.scenes[0].time_of_day == TimeOfDay.MORNING

    def test_large_script_performance(self):
        content = _read_fixture("large_script.fdx")
        t0 = time.monotonic()
        result = FDXParser().parse(content)
        elapsed = time.monotonic() - t0

        assert result.total_scenes == 55
        assert elapsed < 1.0, f"Parsing took {elapsed:.2f}s, should be < 1s"

    def test_invalid_root_tag_raises(self):
        xml = b"<NotFinalDraft><Content></Content></NotFinalDraft>"
        with pytest.raises(ParsingException, match="Not a valid FDX file"):
            FDXParser().parse(xml)

    def test_missing_content_raises(self):
        xml = b"<FinalDraft></FinalDraft>"
        with pytest.raises(ParsingException, match="no <Content> element"):
            FDXParser().parse(xml)

    def test_xxe_attack_blocked(self):
        content = _read_fixture("xxe_attack.fdx")
        with pytest.raises(ParsingException):
            FDXParser().parse(content)


# ===================================================================
# Parser Factory
# ===================================================================


class TestParserFactory:
    """Tests for parsers.base.get_parser."""

    def test_fdx_parser(self):
        parser = get_parser("fdx")
        assert isinstance(parser, FDXParser)
        assert parser.supported_format == ScriptFormat.FDX

    def test_unsupported_format(self):
        with pytest.raises(ParsingException, match="Unsupported script format"):
            get_parser("docx")

    def test_case_insensitive(self):
        parser = get_parser("FDX")
        assert isinstance(parser, FDXParser)


# ===================================================================
# SecureBuffer
# ===================================================================


class TestSecureBuffer:
    """Tests for services.secure_buffer.SecureBuffer."""

    @pytest.fixture()
    def mock_redis(self):
        redis = MagicMock()
        redis.setex = AsyncMock(return_value=None)
        redis.get = AsyncMock(return_value=None)
        redis.delete = AsyncMock(return_value=0)
        return redis

    @pytest.fixture()
    def buffer(self, mock_redis):
        return SecureBuffer(mock_redis, secret_key="test-secret-key-at-least-32-chars", default_ttl=3600)

    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, buffer, mock_redis):
        data = {"script_content": "SGVsbG8gV29ybGQ="}

        # Capture what gets written to Redis
        stored_value = None

        async def capture_setex(key, ttl, value):
            nonlocal stored_value
            stored_value = value

        mock_redis.setex.side_effect = capture_setex

        ref_key = await buffer.store(data)
        assert ref_key.startswith("eki:buf:")
        mock_redis.setex.assert_called_once()

        # Now simulate retrieve
        mock_redis.get.return_value = stored_value
        retrieved = await buffer.retrieve(ref_key)
        assert retrieved == data

    @pytest.mark.asyncio
    async def test_retrieve_expired_raises(self, buffer, mock_redis):
        mock_redis.get.return_value = None
        with pytest.raises(NotFoundException, match="expired or not found"):
            await buffer.retrieve("eki:buf:nonexistent")

    @pytest.mark.asyncio
    async def test_delete(self, buffer, mock_redis):
        mock_redis.delete.return_value = 2
        count = await buffer.delete("eki:buf:a", "eki:buf:b")
        assert count == 2
        mock_redis.delete.assert_called_once_with("eki:buf:a", "eki:buf:b")

    @pytest.mark.asyncio
    async def test_delete_empty(self, buffer, mock_redis):
        count = await buffer.delete()
        assert count == 0
        mock_redis.delete.assert_not_called()

    def test_derive_fernet_key_deterministic(self):
        key1 = _derive_fernet_key("my-secret")
        key2 = _derive_fernet_key("my-secret")
        assert key1 == key2

    def test_derive_fernet_key_differs_for_different_secrets(self):
        key1 = _derive_fernet_key("secret-a")
        key2 = _derive_fernet_key("secret-b")
        assert key1 != key2

    @pytest.mark.asyncio
    async def test_custom_ttl(self, buffer, mock_redis):
        mock_redis.setex.return_value = None
        await buffer.store({"key": "value"}, ttl_seconds=60)
        call_args = mock_redis.setex.call_args
        assert call_args[0][1] == 60  # TTL argument


# ===================================================================
# Integration: Parse from base64 (simulates activity flow)
# ===================================================================


class TestParserIntegration:
    """Integration tests simulating the activity data flow."""

    def test_base64_fdx_roundtrip(self):
        """Simulate: base64 encode -> decode -> parse."""
        raw = _read_fixture("multi_scene.fdx")
        b64 = base64.b64encode(raw).decode("ascii")

        content = base64.b64decode(b64)
        result = FDXParser().parse(content)

        assert result.total_scenes == 5
        assert result.format == ScriptFormat.FDX

    def test_parsed_script_serializable(self):
        """ParsedScript must be JSON-serializable for SecureBuffer storage."""
        content = _read_fixture("simple_scene.fdx")
        result = FDXParser().parse(content)
        dumped = result.model_dump(mode="json")
        assert isinstance(dumped, dict)
        assert dumped["total_scenes"] == 1

        # Ensure it round-trips through JSON
        json_str = json.dumps(dumped)
        loaded = json.loads(json_str)
        assert loaded["total_scenes"] == 1
