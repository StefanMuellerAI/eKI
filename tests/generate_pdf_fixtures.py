#!/usr/bin/env python3
"""Generate synthetic PDF test fixtures for the PDF parser tests.

Run once:  python tests/generate_pdf_fixtures.py
"""

import random
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pdf"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

# Standard screenplay: Courier 12pt
FONT = "Courier"
FONT_SIZE = 12
LINE_HEIGHT = 14


def _write_lines(c: canvas.Canvas, lines: list[str], start_y: float) -> float:
    y = start_y
    for line in lines:
        if y < 72:  # Bottom margin
            c.showPage()
            c.setFont(FONT, FONT_SIZE)
            y = LETTER[1] - 72
        c.drawString(72, y, line)
        y -= LINE_HEIGHT
    return y


def create_simple_screenplay():
    """2 scenes, standard format."""
    path = FIXTURES_DIR / "simple_screenplay.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont(FONT, FONT_SIZE)

    lines = [
        "FADE IN:",
        "",
        "INT. LIVING ROOM - DAY",
        "",
        "A cozy living room with sunlight streaming through windows.",
        "",
        "                         SARAH",
        "          Good morning, everyone.",
        "",
        "                         TOM",
        "               (yawning)",
        "          Morning. Coffee ready?",
        "",
        "",
        "EXT. GARDEN - MORNING",
        "",
        "Tom steps outside and stretches. Birds sing in the trees.",
        "",
        "                         TOM",
        "          What a beautiful day.",
        "",
        "FADE OUT.",
    ]
    _write_lines(c, lines, LETTER[1] - 72)
    c.save()
    print(f"Created: {path}")


def create_german_screenplay():
    """German scene headings."""
    path = FIXTURES_DIR / "german_screenplay.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont(FONT, FONT_SIZE)

    lines = [
        "INNEN. BUERO - TAG",
        "",
        "Ein Mann sitzt am Schreibtisch und telefoniert.",
        "",
        "                         KARL",
        "          Ja, ich bin gleich fertig.",
        "",
        "",
        "AUSSEN. WALD - NACHT",
        "",
        "Eine Frau rennt durch den dunklen Wald.",
        "",
        "                         LENA",
        "          Hilfe! Ist da jemand?",
        "",
        "",
        "INNEN/AUSSEN. AUTO - DAEMMERUNG",
        "",
        "Karl faehrt durch die Daemmerung.",
    ]
    _write_lines(c, lines, LETTER[1] - 72)
    c.save()
    print(f"Created: {path}")


def create_multi_scene():
    """8 scenes with mixed content."""
    path = FIXTURES_DIR / "multi_scene.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont(FONT, FONT_SIZE)

    scenes = [
        ["INT. POLICE STATION - DAY", "Detective JONES reviews case files.", "JONES", "Something doesn't add up."],
        ["EXT. ALLEY - NIGHT", "Jones follows a suspect into a dark alley.", "JONES", "Stop right there!"],
        ["INT. INTERROGATION ROOM - DAY", "A suspect sits across from Jones.", "SUSPECT", "I want my lawyer."],
        ["EXT. ROOFTOP - DUSK", "A chase ends on a rooftop.", "JONES", "It's over."],
        ["INT. HOSPITAL - NIGHT", "Jones visits an injured colleague.", "JONES", "You're going to be fine."],
        ["EXT. PARKING LOT - DAY", "Jones gets into his car.", "JONES", "Time to end this."],
        ["INT. COURTROOM - DAY", "The trial begins.", "JUDGE", "Order in the court."],
        ["EXT. BEACH - EVENING", "Jones walks along the shore.", "JONES", "Finally, some peace."],
    ]

    all_lines = ["THE CASE OF THE MISSING CLUE", "by Test Author", "", ""]
    for scene_data in scenes:
        all_lines.append(scene_data[0])
        all_lines.append("")
        all_lines.append(scene_data[1])
        all_lines.append("")
        all_lines.append(f"                         {scene_data[2]}")
        all_lines.append(f"          {scene_data[3]}")
        all_lines.append("")
        all_lines.append("")

    _write_lines(c, all_lines, LETTER[1] - 72)
    c.save()
    print(f"Created: {path}")


def create_no_structure():
    """Normal prose text, no screenplay format at all."""
    path = FIXTURES_DIR / "no_structure.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont(FONT, FONT_SIZE)

    lines = [
        "This is a regular document that has nothing to do",
        "with a screenplay. It contains no INT. or EXT. markers",
        "and should be treated as unstructured text by the parser.",
        "",
        "The parser should detect that this is not a screenplay",
        "and return it as a single block with low confidence.",
        "",
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
        "Sed do eiusmod tempor incididunt ut labore et dolore.",
    ]
    _write_lines(c, lines, LETTER[1] - 72)
    c.save()
    print(f"Created: {path}")


def create_large_120_pages():
    """~120 pages with ~60 scenes for benchmarking."""
    path = FIXTURES_DIR / "large_120_pages.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont(FONT, FONT_SIZE)

    locations_int = ["OFFICE", "KITCHEN", "BEDROOM", "HALLWAY", "LIBRARY",
                     "HOSPITAL", "CLASSROOM", "ELEVATOR", "PRISON CELL", "STUDIO"]
    locations_ext = ["PARK", "STREET", "ROOFTOP", "BEACH", "FOREST",
                     "PARKING LOT", "BRIDGE", "HARBOR", "HIGHWAY", "GARDEN"]
    times = ["DAY", "NIGHT", "DAWN", "DUSK", "MORNING", "EVENING"]
    characters = ["ANNA", "MAX", "SARAH", "DAVID", "LENA", "KARL", "JONES", "MARIA"]
    actions = [
        "The room is quiet. Only the ticking of a clock.",
        "People hurry past, lost in their own thoughts.",
        "Wind howls through the space.",
        "A tense atmosphere fills the air.",
        "The camera pans across the scene.",
        "Silence. Then a sudden noise breaks the tension.",
        "Rain falls steadily. Everyone is soaked.",
    ]
    dialogues = [
        "We need to move now.",
        "I didn't expect to see you here.",
        "There's something you should know.",
        "It's too dangerous.",
        "Trust me on this one.",
        "Everything is going to be fine.",
    ]

    all_lines = ["THE LONG SCRIPT", "A Benchmark Test", "", ""]

    for i in range(60):
        if random.random() < 0.5:
            loc = random.choice(locations_int)
            prefix = "INT"
        else:
            loc = random.choice(locations_ext)
            prefix = "EXT"
        time_val = random.choice(times)

        all_lines.append(f"{prefix}. {loc} - {time_val}")
        all_lines.append("")

        # 10-15 lines of action per scene to fill pages
        for _ in range(random.randint(8, 15)):
            all_lines.append(random.choice(actions))

        all_lines.append("")
        char = random.choice(characters)
        all_lines.append(f"                         {char}")
        all_lines.append(f"          {random.choice(dialogues)}")
        all_lines.append("")

        # Sometimes add a second character
        if random.random() < 0.5:
            char2 = random.choice([c for c in characters if c != char])
            all_lines.append(f"                         {char2}")
            all_lines.append(f"          {random.choice(dialogues)}")
            all_lines.append("")

        all_lines.append("")

    y = LETTER[1] - 72
    for line in all_lines:
        if y < 72:
            c.showPage()
            c.setFont(FONT, FONT_SIZE)
            y = LETTER[1] - 72
        c.drawString(72, y, line)
        y -= LINE_HEIGHT

    c.save()
    print(f"Created: {path} ({c.getPageNumber()} pages)")


if __name__ == "__main__":
    create_simple_screenplay()
    create_german_screenplay()
    create_multi_scene()
    create_no_structure()
    create_large_120_pages()
    print("All PDF fixtures generated!")
