"""Secure XML parsing utilities.

All XML parsing in this project MUST go through this module to prevent:
- XXE (XML External Entity) attacks
- Billion Laughs / entity expansion bombs
- DTD retrieval over network
- Oversized payload denial-of-service
"""

import logging
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden

from core.exceptions import ParsingException

logger = logging.getLogger(__name__)

# 10 MB -- consistent with SecurityCheckRequest.script_content max_length
MAX_XML_SIZE = 10 * 1024 * 1024


def parse_xml_safe(content: bytes, *, max_size: int = MAX_XML_SIZE) -> Element:
    """Parse XML bytes securely using defusedxml.

    Raises ``ParsingException`` on any XML security violation or malformed input.
    """
    if len(content) > max_size:
        raise ParsingException(
            f"XML payload exceeds size limit ({len(content)} > {max_size} bytes)",
            details={"size": len(content), "max_size": max_size},
        )

    try:
        return SafeET.fromstring(content)
    except DTDForbidden:
        raise ParsingException(
            "XML contains forbidden DTD declaration",
            details={"reason": "dtd_forbidden"},
        )
    except EntitiesForbidden:
        raise ParsingException(
            "XML contains forbidden entity definitions (possible entity expansion attack)",
            details={"reason": "entities_forbidden"},
        )
    except ExternalReferenceForbidden:
        raise ParsingException(
            "XML contains forbidden external references (possible XXE attack)",
            details={"reason": "external_reference_forbidden"},
        )
    except SafeET.ParseError as exc:
        raise ParsingException(
            f"Malformed XML: {exc}",
            details={"reason": "parse_error"},
        )
    except Exception as exc:
        raise ParsingException(
            f"XML parsing failed: {exc}",
            details={"reason": "unknown"},
        )
