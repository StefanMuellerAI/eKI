"""M08 -- Log-Hygiene-Tests (Pflichtenheft Abnahmetest 7).

Verifiziert, dass die zentrale Logging-Konfiguration aus
``core.logging_config`` weder Drehbuchtexte noch Reportinhalte in der
finalen Log-Ausgabe erscheinen laesst. Geprueft werden drei Wege:

1. stdlib-Logger mit ``extra=...`` (z.B. ``logger.info("...", extra={"text": ...})``).
2. structlog-Logger mit ``log.info("...", findings=[...])``.
3. stdlib-Logger mit Args-Dict (selten, aber moeglich).

Die Tests benutzen ``capsys``, weil die Logging-Pipeline ueber den
Root-Handler nach STDOUT schreibt. Klassisches ``caplog`` wuerde den
Formatter umgehen und die Filter-Wirkung verschleiern.
"""

import io
import json
import logging
from types import SimpleNamespace

import pytest
import structlog

from core.logging_config import (
    SensitiveContentFilter,
    _redact_event_dict,
    _redact_value,
    configure_logging,
    set_request_id,
)


def _make_settings(log_format: str = "json", log_level: str = "INFO") -> SimpleNamespace:
    return SimpleNamespace(log_format=log_format, log_level=log_level)


# ---------------------------------------------------------------------------
# Pure unit-tests on the filter helpers (no I/O)
# ---------------------------------------------------------------------------


class TestRedactionHelpers:
    """Direct unit-tests on the redaction primitives."""

    def test_redact_string_keeps_length_marker(self):
        out = _redact_value("Klaus stirbt in einer Explosion." * 20)
        assert "<redacted>" in out
        assert "chars=" in out
        assert "Klaus" not in out
        assert "Explosion" not in out

    def test_redact_list_keeps_length_marker(self):
        out = _redact_value(["a", "b", "c", "d"])
        assert out == "<redacted>(len=4)"

    def test_redact_dict_keeps_key_count(self):
        out = _redact_value({"k1": "x", "k2": "y"})
        assert out == "<redacted>(keys=2)"

    def test_redact_event_dict_replaces_known_keys(self):
        event = {
            "event": "Scene processed",
            "scene_number": "12",
            "text": "Klaus stirbt.",
            "findings": [{"id": "1", "description": "Gefahr"}],
            "non_sensitive": "keep me",
        }
        out = _redact_event_dict(dict(event))

        assert out["event"] == "Scene processed"
        assert out["scene_number"] == "12"
        assert out["non_sensitive"] == "keep me"
        assert "Klaus" not in str(out["text"])
        assert "Gefahr" not in str(out["findings"])

    def test_sensitive_filter_masks_record_extras(self):
        f = SensitiveContentFilter()
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="ok", args=None, exc_info=None,
        )
        record.text = "Klaus zueckt ein Messer."
        record.findings = [{"id": "1", "description": "Waffe"}]
        record.scene_number = "12"

        assert f.filter(record) is True
        assert "Klaus" not in str(record.text)
        assert "Waffe" not in str(record.findings)
        assert record.scene_number == "12"


# ---------------------------------------------------------------------------
# End-to-end tests: configure_logging() -> emit -> inspect stdout
# ---------------------------------------------------------------------------


class TestJsonLoggingPipeline:
    """End-to-end: konfiguriere Logging und pruefe das Output."""

    def test_stdlib_logger_extra_is_redacted(self, capsys):
        configure_logging(_make_settings(log_format="json"))
        log = logging.getLogger("eki.test")

        log.info(
            "Scene processed",
            extra={
                "scene_number": "5",
                "text": "Geheime Szene: Klaus stirbt in Flammen.",
                "findings": [{"id": "f1", "description": "Brandgefahr"}],
            },
        )

        captured = capsys.readouterr().out
        # Pflichtenheft Abnahmetest 7: KEINE Drehbuch- oder
        # Reportinhalte im Output. Wir pruefen nur den Inhalt
        # (nicht die exakte Strukturwiedergabe der whitelisted Felder,
        # die ist Implementations-Detail des Formatters).
        assert "Klaus" not in captured
        assert "Brandgefahr" not in captured
        assert "Flammen" not in captured
        # Der Event-String selbst ist nicht sensibel und muss erhalten bleiben.
        assert "Scene processed" in captured

    def test_structlog_logger_keyword_is_redacted(self, capsys):
        configure_logging(_make_settings(log_format="json"))
        log = structlog.get_logger("eki.test")

        log.info(
            "structured event",
            scene_number="7",
            text="Eine sehr lange geheime Szene mit Klaus.",
            findings=[{"id": "f2", "description": "Explosionsrisiko"}],
            assessment="Sehr hohes Risiko bei Szene 7.",
        )

        captured = capsys.readouterr().out
        assert "Klaus" not in captured
        assert "Explosionsrisiko" not in captured
        assert "Sehr hohes Risiko" not in captured
        # event-Schluessel selbst bleibt erhalten
        assert "structured event" in captured

    def test_request_id_propagates_to_log_lines(self, capsys):
        configure_logging(_make_settings(log_format="json"))
        set_request_id("test-req-abc-123")
        log = structlog.get_logger("eki.test")

        log.info("hello with request id")

        captured = capsys.readouterr().out
        assert "test-req-abc-123" in captured

    def test_console_format_still_redacts(self, capsys):
        configure_logging(_make_settings(log_format="console"))
        log = logging.getLogger("eki.test")

        log.warning(
            "console event",
            extra={"text": "Klaus springt vom Dach.", "scene_number": "9"},
        )

        captured = capsys.readouterr().out
        assert "Klaus" not in captured
        assert "Dach" not in captured

    def test_args_dict_with_sensitive_key_is_redacted(self, capsys):
        configure_logging(_make_settings(log_format="json"))
        log = logging.getLogger("eki.test")

        # Bewusster Edge-Case: jemand uebergibt args als dict mit
        # sensiblem Schluessel. Filter muss greifen.
        log.info("templated %(text)s", {"text": "Klaus mit Waffe."})

        captured = capsys.readouterr().out
        assert "Klaus mit Waffe" not in captured

    def test_configure_logging_is_idempotent(self, capsys):
        configure_logging(_make_settings())
        configure_logging(_make_settings())
        log = logging.getLogger("eki.test")

        log.info("ping")
        captured = capsys.readouterr().out

        # Genau eine Zeile -- kein doppelter Handler.
        non_empty = [line for line in captured.splitlines() if line.strip()]
        assert len(non_empty) == 1
