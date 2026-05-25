"""Central logging configuration for the eKI API (M08).

Implements two Pflichtenheft requirements simultaneously:

1. Strukturierte JSON-Logs (Pflichtenheft 4.1) ueber ``structlog``.
2. Log-Hygiene (Pflichtenheft Abnahmetest 7): Bekannte Felder, die
   Drehbuch- oder Reportinhalte tragen koennten, werden in jeder
   Log-Zeile automatisch maskiert -- in der Message, in den Args,
   und in jedem ``extra``-Dict.

Defaults:
* ``LOG_FORMAT=json`` ergibt strukturierte JSON-Zeilen mit den Schluesseln
  ``timestamp``, ``level``, ``logger``, ``event``, ``request_id`` etc.
* ``LOG_FORMAT=console`` faellt auf einen menschenfreundlichen Renderer
  zurueck (gleiche Maskierung, nur andere Ausgabe). Fuer lokale Dev-Arbeit.

Die Konfiguration ist idempotent (mehrfache Aufrufe sind safe) und
veraendert keinen Logger ausserhalb der ``root``-Hierarchie.
"""

from __future__ import annotations

import contextvars
import logging
import sys
from typing import Any
from uuid import uuid4

import structlog

# ---------------------------------------------------------------------------
# Request-ID propagation
# ---------------------------------------------------------------------------

# Context-Var, die in der Request-ID-Middleware gesetzt wird und in der
# Log-Pipeline gelesen wird. Default leerer String, damit der Worker
# (der keine HTTP-Middleware hat) ein gut definiertes "nicht gesetzt"
# loggt.
_REQUEST_ID_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "eki_request_id", default=""
)


def set_request_id(request_id: str | None) -> str:
    """Set the request_id ContextVar; returns the value actually stored."""
    value = (request_id or "").strip() or uuid4().hex
    _REQUEST_ID_CTX.set(value)
    return value


def get_request_id() -> str:
    """Return the current request_id (empty string if not set)."""
    return _REQUEST_ID_CTX.get()


def _request_id_processor(_logger: Any, _method_name: str, event_dict: dict) -> dict:
    """Add the current request_id to every log event (if set)."""
    rid = _REQUEST_ID_CTX.get()
    if rid:
        event_dict.setdefault("request_id", rid)
    return event_dict


# ---------------------------------------------------------------------------
# Sensitive-content filter
# ---------------------------------------------------------------------------

# Feldnamen, die Drehbuch- oder Reportinhalte tragen koennen und niemals
# im Log auftauchen duerfen. Wird in zwei Stufen angewendet:
#   1. Auf dem stdlib-LogRecord (Filter), damit auch fremde Logger
#      (z.B. httpx, sqlalchemy, temporalio) gefiltert werden.
#   2. Im structlog-Processor, damit ``log.info("…", findings=[…])``
#      ebenfalls greift.
_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "script_content",
    "text",
    "full_text",
    "findings",
    "description",
    "evidence",
    "assessment",
    "action_text",
    "dialogue",
    "recommendation",
    "page_texts",
    "scene_text",
    "scenes",
    "report",
    "report_package",
    "epro_body",
    "epro_response",
})

_REDACTED = "<redacted>"


def _redact_value(value: Any) -> Any:
    """Return a redaction marker proportional to the original size."""
    if isinstance(value, (list, tuple)):
        return f"{_REDACTED}(len={len(value)})"
    if isinstance(value, dict):
        return f"{_REDACTED}(keys={len(value)})"
    if isinstance(value, (bytes, bytearray)):
        return f"{_REDACTED}(bytes={len(value)})"
    if isinstance(value, str):
        return f"{_REDACTED}(chars={len(value)})"
    return _REDACTED


def _redact_event_dict(event_dict: dict) -> dict:
    """Replace any sensitive key in *event_dict* with a redaction marker."""
    for key in list(event_dict.keys()):
        if key in _SENSITIVE_KEYS:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def _sensitive_content_processor(
    _logger: Any, _method_name: str, event_dict: dict
) -> dict:
    """structlog processor: maskiert sensible Schluessel."""
    return _redact_event_dict(event_dict)


class SensitiveContentFilter(logging.Filter):
    """stdlib-Filter: maskiert sensible Felder in ``record.__dict__``.

    Wird an den Root-Handler gehaengt. Damit greifen die Maskierungen
    auch bei Logs aus Bibliotheken, die nicht ueber structlog laufen.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 1) extra-Attribute (custom keyword args) maskieren.
        for key in list(record.__dict__.keys()):
            if key in _SENSITIVE_KEYS:
                record.__dict__[key] = _redact_value(record.__dict__[key])

        # 2) Falls jemand args mit dict uebergeben hat: dict-Werte
        #    pruefen. ``args`` ist meistens ein Tuple oder None.
        if isinstance(record.args, dict):
            redacted = {}
            for k, v in record.args.items():
                redacted[k] = _redact_value(v) if k in _SENSITIVE_KEYS else v
            record.args = redacted

        return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


_CONFIGURED: bool = False


def configure_logging(settings: Any) -> None:
    """Configure root logger and structlog according to ``settings``.

    Reads:
        settings.log_level    -- ``"DEBUG"`` | ``"INFO"`` | ``"WARNING"`` | ...
        settings.log_format   -- ``"json"`` (default) | ``"console"``

    Idempotent: ein zweiter Aufruf re-konfiguriert die Handler-Liste,
    haengt sie aber nicht doppelt an.
    """
    global _CONFIGURED

    level_name = (getattr(settings, "log_level", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = (getattr(settings, "log_format", "json") or "json").lower()

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _request_id_processor,
        _sensitive_content_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if fmt == "console":
        renderer: Any = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()

    # Bestehende Handler entfernen, damit ein erneuter Aufruf nicht
    # doppelt ausgibt. Wir verlassen uns ausschliesslich auf den von
    # uns gesetzten StreamHandler.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(SensitiveContentFilter())
    root.addHandler(handler)
    root.setLevel(level)

    # Bibliotheks-Logger ruhig stellen, ohne sie komplett zu deaktivieren.
    # WARN reicht fuer Betrieb und vermeidet Drehbuch-/Trace-Spam.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # SQLAlchemy installiert bei ``create_async_engine(echo=True)`` einen
    # eigenen StreamHandler am Logger ``sqlalchemy.engine.Engine``. Dadurch
    # entstehen Doppelausgaben (einmal plain via SQLAlchemy-Handler, einmal
    # JSON via Root-Propagation). Wir entfernen den SQLAlchemy-eigenen
    # Handler, damit ausschliesslich der zentrale Root-Handler greift.
    for sa_name in ("sqlalchemy.engine", "sqlalchemy.engine.Engine"):
        sa_logger = logging.getLogger(sa_name)
        for h in list(sa_logger.handlers):
            sa_logger.removeHandler(h)
        sa_logger.propagate = True

    _CONFIGURED = True
