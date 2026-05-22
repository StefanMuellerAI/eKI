#!/usr/bin/env python3
"""M07 – Generator für ein synthetisches 300-Seiten-Drehbuch.

Erzeugt ``tests/fixtures/pdf/large_synthetic_300pp.pdf`` mit ~300 Szenen,
rotierend aus einer kuratierten Bibliothek von Szenen-Templates. Die
Templates decken bewusst eine breite Auswahl der M04-Risiko-Taxonomie ab
(Stunt, Höhe, Feuer, Fahrzeug, Wasser, Tier, Waffen, Intimacy, Gewalt,
Trauer, Trauma, Crowd), damit der Benchmark realistische Findings
erzeugt.

Reproduzierbar: Skript ist deterministisch (kein Random ohne Seed), das
generierte PDF kann jederzeit aus dem Quellcode neu erzeugt werden.

Run:  python tests/build_large_fixture.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)


# Drehbuch-typische Layout-Konstanten
PAGE_MARGIN = 2.0 * cm
HEADING_FONT_SIZE = 12
BODY_FONT_SIZE = 11
DIALOG_INDENT = 4.0 * cm
PARENTHETICAL_INDENT = 5.0 * cm
CHARACTER_INDENT = 6.5 * cm

TARGET_SCENES = 300


# -------------------------------------------------------------------------
# Szenen-Templates: jeweils (heading, character, parenthetical, dialogue,
# action). Heading-Format hält sich strikt an INT/EXT-Konvention, damit der
# deterministische PDF-Scene-Splitter zuverlässig greift.
# -------------------------------------------------------------------------

TEMPLATES: list[dict[str, str]] = [
    {
        "heading": "EXT. STEILE KLIPPE - NACHT",
        "action": (
            "Sturm peitscht über die Felsen. {char_a} steht am Rand, ein "
            "gesichertes Seil um die Hüfte. Der Stunt Coordinator gibt das "
            "Zeichen. Unten in 30 Metern Tiefe schäumen die Wellen."
        ),
        "char_a": "MIRA",
        "char_b": "STUNT COORDINATOR",
        "dialogue_a": "Ich sehe das Seil nicht mehr.",
        "parenthetical_b": "über Funk",
        "dialogue_b": "Atmen. Eine Hand nach der anderen.",
    },
    {
        "heading": "INT. BRENNENDES LAGERHAUS - NACHT",
        "action": (
            "Flammen lodern an der Decke. {char_a} kriecht durch dichten "
            "Rauch. SFX-Supervisor hinter der Kamera, alle Notausgänge "
            "markiert. Pyro-Effekte zünden synchron."
        ),
        "char_a": "JONAS",
        "char_b": "FEUERWEHRMANN",
        "dialogue_a": "Da hinten — der Notausgang!",
        "parenthetical_b": "schreiend",
        "dialogue_b": "Lauf, ich halte die Tür!",
    },
    {
        "heading": "EXT. AUTOBAHN - TAG",
        "action": (
            "Zwei Wagen rasen auf der leeren Strecke. {char_a} reißt das "
            "Lenkrad herum, schneidet einen LKW. Stunt-Doubles am Steuer, "
            "Pre-rigged Crash-Mat am Rand der Bahn."
        ),
        "char_a": "DARIO",
        "char_b": "BEIFAHRER",
        "dialogue_a": "Halt dich fest!",
        "parenthetical_b": "panisch",
        "dialogue_b": "Du fährst uns in den Tod!",
    },
    {
        "heading": "INT. SCHLAFZIMMER - NACHT",
        "action": (
            "Gedämpftes Licht. {char_a} und {char_b} stehen sich gegenüber. "
            "Eine zärtliche Berührung. Closed Set. Intimacy Coordinator "
            "anwesend, alle Beteiligten haben den Choreografie-Briefing "
            "unterschrieben."
        ),
        "char_a": "LENA",
        "char_b": "SEBASTIAN",
        "dialogue_a": "Bleib heute Nacht.",
        "parenthetical_b": "leise",
        "dialogue_b": "Ich kann nicht.",
    },
    {
        "heading": "EXT. PARKHAUS - NACHT",
        "action": (
            "{char_a} wird gegen die Wand gestoßen. {char_b} hält ein "
            "Messer (Stunt-Prop, gummiert). Fight Choreographer in der "
            "Ecke, jeder Schlag ist eingeübt."
        ),
        "char_a": "KIM",
        "char_b": "ANGREIFER",
        "dialogue_a": "Bitte nicht!",
        "parenthetical_b": "kalt",
        "dialogue_b": "Du hast keine Wahl.",
    },
    {
        "heading": "EXT. WALDSEE - DÄMMERUNG",
        "action": (
            "{char_a} taucht unter, kommt nicht wieder hoch. Wasser-Stunts "
            "mit Sicherungsleine, Rettungstaucher 5m entfernt, Wassertemp "
            "kontrolliert auf 22°C."
        ),
        "char_a": "ELIF",
        "char_b": "STIMME AM UFER",
        "dialogue_a": "(unter Wasser, blubbernd)",
        "parenthetical_b": "alarmiert",
        "dialogue_b": "Sie ist immer noch unten!",
    },
    {
        "heading": "INT. PFERDESTALL - TAG",
        "action": (
            "Ein nervöses Pferd scharrt. {char_a} versucht, sich zu "
            "nähern. Tiertrainer steht außerhalb des Bildes, der Reiter "
            "trägt Helm und Schutzweste."
        ),
        "char_a": "ANTON",
        "char_b": "TIERTRAINERIN",
        "dialogue_a": "Ruhig, ruhig.",
        "parenthetical_b": "leise",
        "dialogue_b": "Lass ihm Zeit.",
    },
    {
        "heading": "EXT. NÄCHTLICHER WALD - NACHT",
        "action": (
            "{char_a} hält eine Pistole (Prop, Weapons Master vor Ort, "
            "Blanks geprüft) auf {char_b}. Beide schwitzen, Sicherheits-"
            "abstand 3 Meter, kein Mündungsfeuer in Richtung Kamera."
        ),
        "char_a": "REVE",
        "char_b": "FREMDER",
        "dialogue_a": "Keinen Schritt weiter.",
        "parenthetical_b": "ruhig",
        "dialogue_b": "Du wirst nicht schießen.",
    },
    {
        "heading": "EXT. HOCHHAUSDACH - TAG",
        "action": (
            "{char_a} klettert die letzte Stahlleiter zum Antennenmast. "
            "Rigging-Lead überprüft jede Verankerung. Höhensicherung "
            "Klasse III, Doppelseil, Backup-Karabiner."
        ),
        "char_a": "TARIK",
        "char_b": "RIGGING-LEAD",
        "dialogue_a": "Ich bin oben.",
        "parenthetical_b": "über Funk",
        "dialogue_b": "Nicht runterschauen. Atmen.",
    },
    {
        "heading": "INT. POLIZEIREVIER - TAG",
        "action": (
            "{char_a} sitzt zusammengesunken auf einem Stuhl. {char_b} "
            "lehnt am Schreibtisch, blättert in einer Akte. Im Raum "
            "hängt die Spannung einer langen Vernehmung."
        ),
        "char_a": "AMARA",
        "char_b": "KOMMISSAR HARTH",
        "dialogue_a": "Ich habe alles gesagt, was ich weiß.",
        "parenthetical_b": "müde",
        "dialogue_b": "Dann sagen Sie es noch einmal.",
    },
    {
        "heading": "INT. CAFE - TAG",
        "action": (
            "Sonnenlicht fällt auf zwei Espressotassen. {char_a} und "
            "{char_b} sitzen gegenüber, schweigen. Im Hintergrund klimpert "
            "eine Kaffeemaschine."
        ),
        "char_a": "NORA",
        "char_b": "JAKOB",
        "dialogue_a": "Du hast dich verändert.",
        "parenthetical_b": "nüchtern",
        "dialogue_b": "Du auch.",
    },
    {
        "heading": "EXT. STADTPARK - TAG",
        "action": (
            "Hunde laufen frei, Eltern schieben Kinderwagen. {char_a} "
            "sitzt auf einer Bank und liest. {char_b} tritt heran, zögert."
        ),
        "char_a": "PAUL",
        "char_b": "ALTE FREUNDIN",
        "dialogue_a": "Setz dich.",
        "parenthetical_b": "vorsichtig",
        "dialogue_b": "Ich wollte mich entschuldigen.",
    },
    {
        "heading": "INT. KRANKENHAUSZIMMER - TAG",
        "action": (
            "{char_a} hält die Hand von {char_b}, der reglos im Bett liegt. "
            "Maschinen piepen leise. Trauer hängt im Raum, eine Klinik-"
            "Seelsorgerin geht draußen den Flur entlang."
        ),
        "char_a": "MIA",
        "char_b": "VATER",
        "dialogue_a": "Bitte komm zurück.",
        "parenthetical_b": "kaum hörbar",
        "dialogue_b": "Ich bin müde.",
    },
    {
        "heading": "INT. PSYCHIATRIE-WARTERAUM - TAG",
        "action": (
            "Sterile Beleuchtung. {char_a} sitzt zitternd, krallt sich in "
            "die Lehne. Therapeutin {char_b} öffnet die Tür. Vor Beginn "
            "des Drehs wurden Trauma-Briefings durchgeführt."
        ),
        "char_a": "RANIA",
        "char_b": "DR. KÖHLER",
        "dialogue_a": "Ich kann nicht reden.",
        "parenthetical_b": "ruhig",
        "dialogue_b": "Sie müssen nichts sagen. Atmen Sie.",
    },
    {
        "heading": "EXT. STRASSENFEST - TAG",
        "action": (
            "Hunderte Statisten drängen sich durch enge Gassen. {char_a} "
            "kämpft sich gegen den Strom. Crowd Marshal koordiniert "
            "Fluchtwege, jeder Statist trägt Erkennungsband."
        ),
        "char_a": "JULES",
        "char_b": "MARSHAL",
        "dialogue_a": "Lassen Sie mich durch!",
        "parenthetical_b": "lautstark",
        "dialogue_b": "Rechts halten, bitte!",
    },
]


def build_pdf(out_path: Path, n_scenes: int = TARGET_SCENES) -> None:
    """Build the synthetic large fixture PDF."""
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
        title="eKI M07 Synthetic Large Fixture",
    )

    styles = getSampleStyleSheet()

    heading_style = ParagraphStyle(
        "Heading",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=HEADING_FONT_SIZE,
        leading=14,
        spaceBefore=10,
        spaceAfter=6,
        textTransform="uppercase",
    )
    action_style = ParagraphStyle(
        "Action",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=BODY_FONT_SIZE,
        leading=13,
        spaceAfter=6,
    )
    character_style = ParagraphStyle(
        "Character",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=BODY_FONT_SIZE,
        leading=13,
        leftIndent=CHARACTER_INDENT,
    )
    parenthetical_style = ParagraphStyle(
        "Parenthetical",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=BODY_FONT_SIZE - 1,
        leading=12,
        leftIndent=PARENTHETICAL_INDENT,
    )
    dialogue_style = ParagraphStyle(
        "Dialogue",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=BODY_FONT_SIZE,
        leading=13,
        leftIndent=DIALOG_INDENT,
        rightIndent=2.0 * cm,
        spaceAfter=10,
    )

    flow: list = []
    title_style = ParagraphStyle(
        "Title",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=22,
        alignment=1,
        spaceAfter=24,
    )
    flow.append(Paragraph("DAS UNGESCHRIEBENE", title_style))
    flow.append(Paragraph(
        "Synthetisches Test-Drehbuch (eKI M07 Großdokument-Benchmark)",
        styles["Italic"],
    ))
    flow.append(Spacer(1, 1.0 * cm))
    flow.append(Paragraph("FADE IN:", action_style))
    flow.append(Spacer(1, 0.5 * cm))

    for i in range(n_scenes):
        tpl = TEMPLATES[i % len(TEMPLATES)]
        scene_no = i + 1

        # Heading bekommt eine fortlaufende Szenennummer angehängt, damit
        # auch bei rotierenden Templates jede Szene eindeutig ist. Format
        # entspricht der Drehbuch-Konvention "42 INT./EXT. ..." (Zahl
        # ohne Punkt davor) -- exakt das Pattern, das der eKI Scene
        # Splitter in parsers/pdf_scene_splitter.py erkennt.
        heading = f"{scene_no} {tpl['heading']}"
        flow.append(Paragraph(heading, heading_style))

        action = tpl["action"].format(
            char_a=tpl["char_a"], char_b=tpl["char_b"]
        )
        flow.append(Paragraph(action, action_style))

        flow.append(Paragraph(tpl["char_a"], character_style))
        flow.append(Paragraph(tpl["dialogue_a"], dialogue_style))

        flow.append(Paragraph(tpl["char_b"], character_style))
        if tpl.get("parenthetical_b"):
            flow.append(
                Paragraph(f"({tpl['parenthetical_b']})", parenthetical_style)
            )
        flow.append(Paragraph(tpl["dialogue_b"], dialogue_style))

        # Jede Szene endet auf eigener "Seite". Drehbuchstandard ist
        # 1 Seite ≈ 1 Minute Filmzeit; ein PageBreak garantiert genau
        # n_scenes Seiten -- praktisch für den Pflichtenheft-Test.
        flow.append(PageBreak())

    flow.append(Paragraph("FADE OUT.", action_style))
    flow.append(Paragraph("ENDE", title_style))

    doc.build(flow)


def main() -> int:
    out_dir = Path(__file__).resolve().parent / "fixtures" / "pdf"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "large_synthetic_300pp.pdf"

    print(f"Generating synthetic 300-page fixture -> {out_path}")
    build_pdf(out_path, n_scenes=TARGET_SCENES)
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"OK -- {size_mb:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
