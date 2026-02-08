"""Abstract base class for script parsers and parser factory."""

from abc import ABC, abstractmethod

from core.exceptions import ParsingException
from core.models import ParsedScript, ScriptFormat


class ParserBase(ABC):
    """Interface that every script parser must implement.

    The ``parse`` method is async to support LLM-backed parsers (PDF).
    Synchronous parsers (FDX) simply return directly from an async method.
    """

    @abstractmethod
    async def parse(self, content: bytes) -> ParsedScript:
        """Parse raw file bytes and return a ``ParsedScript``.

        Implementations MUST NOT write anything to disk.
        """

    @property
    @abstractmethod
    def supported_format(self) -> ScriptFormat:
        """The ``ScriptFormat`` this parser handles."""


def get_parser(fmt: str) -> ParserBase:
    """Return the appropriate parser for *fmt* (e.g. ``"fdx"``, ``"pdf"``).

    Raises ``ParsingException`` for unsupported formats.
    """
    from parsers.fdx import FDXParser
    from parsers.pdf import PDFParser

    _registry: dict[str, type[ParserBase]] = {
        ScriptFormat.FDX.value: FDXParser,
        ScriptFormat.PDF.value: PDFParser,
    }

    parser_cls = _registry.get(fmt.lower())
    if parser_cls is None:
        raise ParsingException(
            f"Unsupported script format: {fmt}",
            details={"format": fmt, "supported": list(_registry.keys())},
        )
    return parser_cls()
