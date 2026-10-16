#!/usr/bin/env python3
"""Generate synthetic PDF test fixtures for the PDF parser tests.

Every PDF committed under ``tests/fixtures/pdf/`` is synthetic and produced by
this script or by ``tests/build_large_fixture.py`` (``german_screenplay_report.pdf``
is an eKI report rendered for the synthetic ``german_screenplay.pdf``).  Real
screenplays never go into the repository: put them into the git-ignored
``tests/fixtures/pdf/private/`` (or ``EKI_PRIVATE_FIXTURES_DIR``) for the
``tests/run_pdf_*.py`` stack runners.

Run:  python tests/generate_pdf_fixtures.py                 # all fixtures
      python tests/generate_pdf_fixtures.py realworld_headings suffix_headings
"""

import random
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4, LETTER
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
        [
            "INT. POLICE STATION - DAY",
            "Detective JONES reviews case files.",
            "JONES",
            "Something doesn't add up.",
        ],
        [
            "EXT. ALLEY - NIGHT",
            "Jones follows a suspect into a dark alley.",
            "JONES",
            "Stop right there!",
        ],
        [
            "INT. INTERROGATION ROOM - DAY",
            "A suspect sits across from Jones.",
            "SUSPECT",
            "I want my lawyer.",
        ],
        ["EXT. ROOFTOP - DUSK", "A chase ends on a rooftop.", "JONES", "It's over."],
        [
            "INT. HOSPITAL - NIGHT",
            "Jones visits an injured colleague.",
            "JONES",
            "You're going to be fine.",
        ],
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


def create_no_structure_multi_page():
    """Multi-page document without any screenplay markers (for page-fallback testing)."""
    path = FIXTURES_DIR / "no_structure_multi_page.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont(FONT, FONT_SIZE)

    page_contents = [
        [
            "THE MYSTERIOUS TALE",
            "by Unknown Author",
            "",
            "A story told in prose, without any screenplay formatting.",
            "This first page serves as the preamble / title page.",
        ],
        [
            "Chapter One",
            "",
            "The man walked slowly through the rain-soaked streets.",
            "He pulled his collar up against the wind and kept moving.",
            "Somewhere behind him, footsteps echoed on the cobblestones.",
            "",
            "He stopped at the corner and lit a cigarette.",
            "The flame flickered in the darkness.",
        ],
        [
            "Chapter Two",
            "",
            "Morning came too quickly. The alarm clock shattered the",
            "silence and he rolled over, pulling the blanket up.",
            "",
            "MARIA knocked on the door. She carried two cups of coffee.",
            "They sat together in silence for a long time.",
        ],
        [
            "Chapter Three",
            "",
            "The car sped through the narrow roads outside the city.",
            "KARL gripped the steering wheel, his knuckles white.",
            "",
            "They had to reach the harbor before midnight.",
            "Everything depended on it.",
        ],
    ]

    for i, page_lines in enumerate(page_contents):
        if i > 0:
            c.showPage()
            c.setFont(FONT, FONT_SIZE)
        _write_lines(c, page_lines, LETTER[1] - 72)

    c.save()
    print(f"Created: {path}")


def create_password_protected():
    """Password-protected PDF for testing rejection."""
    path = FIXTURES_DIR / "password_protected.pdf"
    from reportlab.lib.pagesizes import LETTER as L

    c = canvas.Canvas(str(path), pagesize=L)
    c.setFont(FONT, FONT_SIZE)
    c.drawString(72, L[1] - 72, "This PDF is password-protected.")
    c.save()

    try:
        import pikepdf

        pdf = pikepdf.open(str(path), allow_overwriting_input=True)
        pdf.save(
            str(path),
            encryption=pikepdf.Encryption(owner="owner123", user="user123", R=4),
        )
        print(f"Created: {path} (password-protected)")
    except ImportError:
        print(f"Skipped: {path} (pikepdf not installed)")


def create_large_120_pages():
    """~120 pages with ~60 scenes for benchmarking."""
    path = FIXTURES_DIR / "large_120_pages.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont(FONT, FONT_SIZE)

    locations_int = [
        "OFFICE",
        "KITCHEN",
        "BEDROOM",
        "HALLWAY",
        "LIBRARY",
        "HOSPITAL",
        "CLASSROOM",
        "ELEVATOR",
        "PRISON CELL",
        "STUDIO",
    ]
    locations_ext = [
        "PARK",
        "STREET",
        "ROOFTOP",
        "BEACH",
        "FOREST",
        "PARKING LOT",
        "BRIDGE",
        "HARBOR",
        "HIGHWAY",
        "GARDEN",
    ]
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

    for _i in range(60):
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


SCANNED_LINES = [
    "INT. KUECHE - TAG",
    "",
    "Eine helle Kueche. ANNA schneidet Gemuese.",
    "Ein Topf kocht auf dem Herd.",
    "",
    "                    ANNA",
    "          Das Essen ist gleich fertig.",
    "",
    "EXT. HOF - NACHT",
    "",
    "PAUL laeuft ueber den dunklen Hof.",
    "Ein Hund bellt in der Ferne.",
]


def _render_text_image(lines: list[str], width_px: int = 1700, height_px: int = 2200):
    """Render screenplay lines to a PIL image (simulates a scanned page)."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("L", (width_px, height_px), color=255)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size=40)
    except OSError:
        font = ImageFont.load_default()
    y = 150
    for line in lines:
        draw.text((150, y), line, fill=0, font=font)
        y += 70
    return image


def create_scanned_screenplay():
    """OCR fixture: one image-only page followed by one page with a text layer.

    Page 1 carries no text layer at all (pure raster), page 2 is a normal
    text page, so the fixture also verifies page-index alignment.
    """
    import io

    from reportlab.lib.utils import ImageReader

    path = FIXTURES_DIR / "scanned_screenplay.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)

    image = _render_text_image(SCANNED_LINES)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    c.drawImage(ImageReader(buf), 0, 0, width=LETTER[0], height=LETTER[1])
    c.showPage()

    c.setFont(FONT, FONT_SIZE)
    _write_lines(
        c,
        [
            "INT. WOHNZIMMER - ABEND",
            "",
            "ANNA und PAUL sitzen auf dem Sofa.",
            "",
            "                    PAUL",
            "          Was fuer ein Tag.",
        ],
        LETTER[1] - 72,
    )
    c.save()
    print(f"Created {path}")


def create_blank_middle_page():
    """Alignment fixture: text page, completely blank page, text page."""
    path = FIXTURES_DIR / "blank_middle_page.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont(FONT, FONT_SIZE)
    _write_lines(c, ["INT. BUERO - TAG", "", "Erste Seite mit Text."], LETTER[1] - 72)
    c.showPage()  # page 2: intentionally empty
    c.showPage()
    c.setFont(FONT, FONT_SIZE)
    _write_lines(c, ["EXT. STRASSE - NACHT", "", "Dritte Seite mit Text."], LETTER[1] - 72)
    c.save()
    print(f"Created {path}")


# ---------------------------------------------------------------------------
# Layout features of real screenplays, rebuilt synthetically (no real text).
# Both fixtures use ``invariant=1`` so that regenerating them is byte-stable.
# ---------------------------------------------------------------------------

_CUE = " " * 20  # character cue indent
_DLG = " " * 10  # dialogue indent

REALWORLD_PAGES: list[list[str]] = [
    [
        "DAS UNGESCHRIEBENE HAUS",
        "",
        "Drehbuch",
        "nach einer Idee von Testautorin",
        "",
        "5. Fassung, Stand 01.01.2026",
        "Synthetisches Testdokument - keine reale Produktion",
    ],
    [
        "1 INT. KÜCHE - TAG",
        "",
        "Eine helle Küche. MARA (34) schneidet Brot.",
        "",
        f"{_CUE}MARA",
        f"{_DLG}Kaffee ist fertig.",
        "",
        f"{_CUE}JONAS (O.S.)",
        f"{_DLG}Komme gleich!",
        "",
        "2 EXT. HOF - NACHT",
        "",
        "Regen. JONAS (36) läuft über den Hof zum Auto.",
        "",
        "3 INT./EXT. AUTO - FORTLAUFEND",
        "",
        "Jonas startet den Motor. Die Scheiben beschlagen.",
        "",
        "4 INT/EXT. TANKSTELLE - ABEND",
        "",
        "Neonlicht. Jonas tankt und sieht sich um.",
    ],
    [
        "2.",
        "5A EXT. LANDSTRASSE - NACHT",
        "",
        "Scheinwerfer auf nassem Asphalt.",
        "",
        "5B EXT. LANDSTRASSE - SPÄTER",
        "",
        "Das Auto steht am Straßenrand, der Motor läuft.",
        "",
        f"{_CUE}JONAS (CONT'D)",
        f"{_DLG}Ich weiß nicht mehr weiter --",
        "",
        "6. EXT. FELD - TAG",
        "",
        "Mara geht über ein Stoppelfeld.",
        "",
        "7. INT. SCHEUNE - NACHT",
        "",
        "Staub tanzt im Licht einer Taschenlampe.",
    ],
    [
        "3.",
        "12.1 INT. FLUR - TAG",
        "",
        "Ein langer Flur. Türen links und rechts.",
        "",
        "12.1A. INT. BAD - TAG",
        "",
        "Mara wäscht sich das Gesicht.",
        "",
        "13 I/E. AUTO - NACHT",
        "",
        "Jonas am Steuer, Mara auf dem Beifahrersitz.",
        "",
        f"{_CUE}MARA (INTER)",
        f"{_DLG}Fahr langsamer.",
        "",
        "14 E/I. TREPPENHAUS - NACHT",
        "",
        "Schritte hallen durch das Treppenhaus.",
    ],
    [
        "4.",
        "15 INNEN. WOHNUNG - ABEND",
        "",
        "Auf dem Tablet läuft die Lern-App. Die Avatare passen",
        "sich den User:innen an, alle Nutzer:innen sehen ihr Profil.",
        "",
        f"{_CUE}COMPUTERSTIMME (V.O.)",
        f"{_DLG}System: Zugriff verweigert.",
        "",
        "Auf dem Bildschirm: USER: hallo?",
        "",
        "16 AUSSEN. BALKON - NACHT",
        "",
        "RÜCKBLENDE --- zehn Jahre früher.",
        "",
        "17 INNEN-AUSSEN. GARTEN - TAG",
        "",
        "Kinder spielen zwischen den Beeten.",
        "",
        "18 INT. CUISINE - JOUR",
        "",
        "Élodie entre, un café à la main.",
        "",
        f"{_CUE}ÉLODIE",
        f"{_DLG}Ça va ? On commence à huit heures.",
    ],
]

SUFFIX_HEADING_PAGES: list[list[str]] = [
    [
        "DAS UNGESCHRIEBENE UFER",
        "",
        "Drehbuch, 3. Fassung",
        "Synthetisches Testdokument - keine reale Produktion",
    ],
    [
        "1 WALDSTRASSE, DÄMMERUNG/AUSSEN",
        "",
        "Ein Geländewagen fährt durch den Wald.",
        "",
        f"{_CUE}SARA",
        f"{_DLG}Wie weit ist es noch?",
    ],
    [
        "2 GELÄNDEWAGEN, DÄMMERUNG/INNEN",
        "",
        "SARA (32) und THEO (34) schweigen. Das Navi piept.",
        "",
        f"{_CUE}NAVI-STIMME",
        f"{_DLG}Bitte wenden.",
    ],
    [
        "3 SEEUFER, NACHT/AUSSEN",
        "",
        "Nebel liegt über dem Wasser. Theo steigt aus.",
    ],
]


def _write_pages(path: Path, pages: list[list[str]], pagesize=A4) -> None:
    c = canvas.Canvas(str(path), pagesize=pagesize, invariant=1)
    for i, page_lines in enumerate(pages):
        if i:
            c.showPage()
        c.setFont(FONT, FONT_SIZE)
        _write_lines(c, page_lines, pagesize[1] - 72)
    c.save()
    print(f"Created {path}")


def create_realworld_headings():
    """Heading/layout variants found in real German screenplays, synthetic text.

    Title page, numbered headings ("1 INT.", "5A EXT.", "6. EXT.", "12.1 INT.",
    "12.1A. INT."), "INT./EXT."/"INT/EXT."/"I/E."/"E/I.", German
    "INNEN."/"AUSSEN."/"INNEN-AUSSEN.", page numbers at the top of each page,
    cue extensions (O.S., V.O., CONT'D, INTER), the German gender colon
    ("User:innen") and on-screen computer text for the prompt-injection screen,
    plus one French scene (accents).
    """
    _write_pages(FIXTURES_DIR / "realworld_headings.pdf", REALWORLD_PAGES)


def create_suffix_headings():
    """Headings with the INNEN/AUSSEN designation at the end ("1 ORT, ZEIT/AUSSEN").

    The splitter does not recognise this layout, so the document must fall
    back to page-by-page splitting.
    """
    _write_pages(FIXTURES_DIR / "suffix_headings.pdf", SUFFIX_HEADING_PAGES)


GENERATORS = {
    "simple_screenplay": create_simple_screenplay,
    "german_screenplay": create_german_screenplay,
    "multi_scene": create_multi_scene,
    "no_structure": create_no_structure,
    "no_structure_multi_page": create_no_structure_multi_page,
    "password_protected": create_password_protected,
    "large_120_pages": create_large_120_pages,
    "scanned_screenplay": create_scanned_screenplay,
    "blank_middle_page": create_blank_middle_page,
    "realworld_headings": create_realworld_headings,
    "suffix_headings": create_suffix_headings,
}


if __name__ == "__main__":
    selected = sys.argv[1:] or list(GENERATORS)
    unknown = sorted(set(selected) - set(GENERATORS))
    if unknown:
        sys.exit(f"Unknown fixture(s): {unknown}; choose from {sorted(GENERATORS)}")
    for name in selected:
        GENERATORS[name]()
    print("PDF fixtures generated:", ", ".join(selected))
