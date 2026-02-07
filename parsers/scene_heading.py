"""Scene heading (slug line) parser with German and English support.

Parses strings like:
    INT. BÜRO - TAG          -> (INT, "BÜRO", DAY)
    EXT. FOREST - NIGHT      -> (EXT, "FOREST", NIGHT)
    INT./EXT. AUTO - DAWN    -> (INT/EXT, "AUTO", DAWN)
"""

import re
from dataclasses import dataclass

from core.models import LocationType, TimeOfDay

# ---------------------------------------------------------------------------
# Time-of-day mapping (German + English)
# ---------------------------------------------------------------------------

_TIME_MAP: dict[str, TimeOfDay] = {
    # English
    "DAY": TimeOfDay.DAY,
    "NIGHT": TimeOfDay.NIGHT,
    "DAWN": TimeOfDay.DAWN,
    "DUSK": TimeOfDay.DUSK,
    "MORNING": TimeOfDay.MORNING,
    "EVENING": TimeOfDay.EVENING,
    "CONTINUOUS": TimeOfDay.CONTINUOUS,
    "CONT": TimeOfDay.CONTINUOUS,
    "LATER": TimeOfDay.CONTINUOUS,
    "SAME": TimeOfDay.CONTINUOUS,
    "MOMENTS LATER": TimeOfDay.CONTINUOUS,
    # German
    "TAG": TimeOfDay.DAY,
    "NACHT": TimeOfDay.NIGHT,
    "MORGEN": TimeOfDay.MORNING,
    "MORGENS": TimeOfDay.MORNING,
    "ABEND": TimeOfDay.EVENING,
    "ABENDS": TimeOfDay.EVENING,
    "DÄMMERUNG": TimeOfDay.DUSK,
    "DAEMMERUNG": TimeOfDay.DUSK,
    "MORGENDÄMMERUNG": TimeOfDay.DAWN,
    "FORTLAUFEND": TimeOfDay.CONTINUOUS,
    "SPÄTER": TimeOfDay.CONTINUOUS,
    "SPAETER": TimeOfDay.CONTINUOUS,
}

# ---------------------------------------------------------------------------
# Location-type prefixes (order matters -- longer matches first)
# ---------------------------------------------------------------------------

_LOC_PREFIXES: list[tuple[str, LocationType]] = [
    ("INT./EXT.", LocationType.INT_EXT),
    ("INT/EXT.", LocationType.INT_EXT),
    ("I./E.", LocationType.INT_EXT),
    ("I/E.", LocationType.INT_EXT),
    ("EXT./INT.", LocationType.INT_EXT),
    ("EXT/INT.", LocationType.INT_EXT),
    ("INNEN/AUSSEN", LocationType.INT_EXT),
    ("AUSSEN/INNEN", LocationType.INT_EXT),
    ("INT.", LocationType.INT),
    ("INNEN", LocationType.INT),
    ("I.", LocationType.INT),
    ("EXT.", LocationType.EXT),
    ("AUSSEN", LocationType.EXT),
    ("E.", LocationType.EXT),
]

# Separator between location and time-of-day (dash variants)
_SEP_RE = re.compile(r"\s*[-–—]\s*")


@dataclass(frozen=True, slots=True)
class HeadingComponents:
    """Parsed components of a scene heading."""

    location_type: LocationType
    location: str
    time_of_day: TimeOfDay


def parse_scene_heading(heading: str) -> HeadingComponents:
    """Parse a scene heading string into its constituent parts.

    Returns ``HeadingComponents`` with best-effort extraction.  Unknown
    location types or times default to ``UNKNOWN``.
    """
    text = heading.strip()
    upper = text.upper()

    # 1. Determine location type by prefix
    loc_type = LocationType.UNKNOWN
    remainder = text
    for prefix, lt in _LOC_PREFIXES:
        if upper.startswith(prefix):
            loc_type = lt
            remainder = text[len(prefix) :].strip()
            break

    # 2. Split remainder on last separator to get location and time-of-day
    parts = _SEP_RE.split(remainder)
    if len(parts) >= 2:
        location = " - ".join(parts[:-1]).strip()
        raw_time = parts[-1].strip().upper()
    else:
        location = remainder.strip()
        raw_time = ""

    # Strip leading/trailing dots and whitespace from location
    location = location.strip(". ")

    # 3. Map time-of-day
    tod = _TIME_MAP.get(raw_time, TimeOfDay.UNKNOWN)

    return HeadingComponents(
        location_type=loc_type,
        location=location if location else heading.strip(),
        time_of_day=tod,
    )
