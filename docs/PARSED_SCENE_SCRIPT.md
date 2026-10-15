# Beispielausgabe des Parsers: `ParsedScene` und `ParsedScript`

> **Beispieldaten, kein Betriebsdokument.** Zeigt die Struktur, die der FDX-Parser
> (`parsers/fdx.py`) bzw. der PDF-Pfad aus einem Drehbuch erzeugt (Modelle `ParsedScene` und
> `ParsedScript` in `core/models.py`). Die Werte sind synthetisch und gekürzt (`"..."`).
> Diese Objekte sind transient: Sie liegen nur verschlüsselt im Redis-Buffer und werden nie in
> der Datenbank gespeichert ([DATENSCHUTZ.md](DATENSCHUTZ.md)). Bei Abweichungen gilt
> `core/models.py`.

## Eine Szene (`ParsedScene`)

```json
{
  "scene_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "number": "3",
  "heading": "INT. BURNING BUILDING - NIGHT",
  "location": "BURNING BUILDING",
  "location_type": "INT",
  "time_of_day": "NIGHT",
  "characters": ["FIREFIGHTER", "KARL"],
  "action_text": "Flames engulf the hallway. Smoke makes visibility near zero. A firefighter carries an unconscious child through the inferno. The ceiling collapses behind them.",
  "dialogue": [
    {
      "character": "KARL",
      "parenthetical": "(panicked)",
      "text": "We need to get out now!"
    },
    {
      "character": "FIREFIGHTER",
      "parenthetical": null,
      "text": "Stay behind me!"
    }
  ],
  "text": "INT. BURNING BUILDING - NIGHT\nFlames engulf the hallway. Smoke makes visibility near zero...",
  "parse_confidence": 1.0,
  "parse_method": "fdx"
}
```

## Ein geparstes Drehbuch (`ParsedScript`, Ausschnitt)

`total_scenes` nennt die Gesamtzahl; die Liste `scenes` ist hier auf zwei Einträge gekürzt.

```json
{
  "script_id": "f0e1d2c3-b4a5-6789-0fed-cba987654321",
  "title": "Testfilm - Stunt Heavy",
  "format": "fdx",
  "total_scenes": 4,
  "scenes": [
    {
      "scene_id": "...",
      "number": "1",
      "heading": "EXT. CLIFF EDGE - NIGHT",
      "location": "CLIFF EDGE",
      "location_type": "EXT",
      "time_of_day": "NIGHT",
      "characters": ["STUNT COORDINATOR"],
      "action_text": "A stunt performer stands at the edge of a 15-meter cliff...",
      "dialogue": [
        {
          "character": "STUNT COORDINATOR",
          "parenthetical": null,
          "text": "Clear the zone! Jump on my mark!"
        }
      ],
      "text": "EXT. CLIFF EDGE - NIGHT\nA stunt performer stands at the edge...",
      "parse_confidence": 1.0,
      "parse_method": "fdx"
    },
    {
      "scene_id": "...",
      "number": "2",
      "heading": "EXT. HIGHWAY - DAY",
      "location": "HIGHWAY",
      "location_type": "EXT",
      "time_of_day": "DAY",
      "characters": ["DRIVER"],
      "action_text": "Three cars race at high speed along the highway...",
      "dialogue": [
        {
          "character": "DRIVER",
          "parenthetical": null,
          "text": "Hold on tight!"
        }
      ],
      "text": "EXT. HIGHWAY - DAY\nThree cars race at high speed...",
      "parse_confidence": 1.0,
      "parse_method": "fdx"
    }
  ],
  "characters": [
    {
      "name": "STUNT COORDINATOR",
      "scene_appearances": ["a1b2c3d4-..."]
    },
    {
      "name": "DRIVER",
      "scene_appearances": ["b2c3d4e5-..."]
    }
  ],
  "parsing_time_seconds": 0.012,
  "metadata": {},
  "overall_confidence": 1.0,
  "warnings": []
}
```
