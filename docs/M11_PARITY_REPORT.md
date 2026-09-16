# M11 – Paritätsreport Cloud ↔ Lokal

Nachweis für Pflichtenheft §8 (Risiko „Provider-Wechsel Cloud→Lokal:
Modell-/Prompt-Parität"; Mitigation „Paritäts-Tests, Tuning") und §9 M11
(„Paritäts-Reports Cloud↔Lokal"). Erzeugt mit `scripts/run_parity.py` auf dem
Golden-Set `tests/parity/golden_scenes.yaml` (16 Szenen, 14 mit erwarteten
Risikoklassen, 2 Negativkontrollen).

---

## 1. Methodik

Für jeden Provider werden die **produktiven Prompts** (`config/prompts/prompts.yaml`)
und Schemata (`SCENE_SCHEMA`, `_RISK_SCHEMA`) unverändert ausgeführt:

1. **Strukturierung** (`pdf_structuring.scene`) → Schema-Validität, INT/EXT-Klassifikation.
2. **Risikoanalyse** (`risk_analysis.scene`) → Schema-Validität, gefundene Klassen,
   Severity nach `TaxonomyManager.validate_finding` (Likelihood × Impact).

| Kennzahl | Definition | Schwelle |
|---|---|---|
| `schema_valid_rate` | beide Antworten pro Szene schema-konform | ≥ 98 % |
| `class_recall` | erwartete Klassen gefunden / erwartete Klassen (Mittel über Szenen) | ≥ 80 % |
| `severity_agreement` | max. Severity ≥ erwartete Mindest-Severity − 1 Stufe; Negativszenen ≤ medium | ≥ 75 % |
| `false_positive_rate` | Negativszenen mit high/critical-Finding | ≤ 10 % |
| `location_type_accuracy` | INT/EXT korrekt | ≥ 90 % |
| Latenz p50/p95 | pro Call, Transportzeit | informativ (SLO-Ableitung) |

Ein Provider gilt als **paritätsfähig**, wenn alle fünf Schwellen erfüllt sind. Die
Schwellen sind bewusst so gewählt, dass ein kleineres lokales Modell nicht identische,
aber fachlich gleichwertige Ergebnisse liefern muss (Hinweise statt Gutachten, §3.3).

---

## 2. Ausführung

```bash
# Stage (Cloud, Abnahmetest 8) gegen Produktions-Kandidaten
export MISTRAL_API_KEY=...            # nur auf Stage!
python scripts/run_parity.py --providers mistral_cloud,local_mistral,ollama:gemma4:31b

# Nur lokal (Prod-Hardware, ohne Cloud-Zugang)
python scripts/run_parity.py --providers local_mistral,ollama:gemma4:31b --concurrency 2
```

Ausgabe: `tests/reports/parity_<timestamp>.json` (vollständig, je Szene) und
`tests/reports/parity_<timestamp>.md` (Tabelle wie unten). Exit-Code 0 = alle Provider bestanden.

---

## 3. Ergebnis

> **Status:** Ausführung auf der Zielhardware (GPU ≥ 24 GB, Pflichtenheft §8 Annahme)
> steht aus – die Entwicklungsumgebung dieses Meilensteins hat weder GPU noch
> Mistral-Cloud-Zugang. Die Tabelle wird nach dem Lauf 1:1 aus
> `tests/reports/parity_<timestamp>.md` übernommen. Die Harness selbst ist mit
> deterministischen Fake-Providern getestet (`tests/test_m11_parity_harness.py`).

| Kennzahl | Schwelle | mistral_cloud (mistral-large-latest) | local_mistral (mistral-small3.2) | ollama (gemma4:31b) |
|---|---|---|---|---|
| `schema_valid_rate` | ≥ 0.98 | _ausstehend_ | _ausstehend_ | _ausstehend_ |
| `class_recall` | ≥ 0.80 | _ausstehend_ | _ausstehend_ | _ausstehend_ |
| `severity_agreement` | ≥ 0.75 | _ausstehend_ | _ausstehend_ | _ausstehend_ |
| `false_positive_rate` | ≤ 0.10 | _ausstehend_ | _ausstehend_ | _ausstehend_ |
| `location_type_accuracy` | ≥ 0.90 | _ausstehend_ | _ausstehend_ | _ausstehend_ |
| `risk_latency_p50_s` | – | _ausstehend_ | _ausstehend_ | _ausstehend_ |
| `risk_latency_p95_s` | – | _ausstehend_ | _ausstehend_ | _ausstehend_ |
| **Gesamt** | alle | _ausstehend_ | _ausstehend_ | _ausstehend_ |

Entscheidung Produktiv-Default: `mistral-small3.2` (Pflichtenheft §4.3). `gemma4` bleibt
als dokumentierte Alternative konfigurierbar (`LLM_PROVIDER=ollama`, `OLLAMA_MODEL=gemma4:31b`),
sofern es die Schwellen ebenfalls erfüllt.

---

## 4. Tuning-Hebel bei Nichtbestehen

| Symptom | Hebel |
|---|---|
| `schema_valid_rate` < 98 % | Ollama nutzt Grammar-Constraints (`format=schema`) – prüfen, ob das Modell `format` unterstützt; `OLLAMA_THINK=false` |
| `class_recall` niedrig | Taxonomie-Kontext im Prompt (`summary_for_prompt`) verdichten; Beispiele im System-Prompt; größeres Modell (`mistral-small3.2:24b`) |
| `severity_agreement` niedrig | Skalen-Anker (1–5) im Prompt schärfen; `temperature` 0.2 → 0.1 |
| `false_positive_rate` hoch | Negativbeispiele im Prompt; „nur wenn im Text belegt (evidence)" betonen |
| Latenz p95 > SLO-Budget | `OLLAMA_NUM_CTX` senken, `OLLAMA_MAX_CONCURRENT_REQUESTS` erhöhen (VRAM), Quantisierung Q4_K_M |

Prompt-Änderungen sind ohne Code-Deployment möglich (`config/prompts/prompts.yaml`) und
werden erneut gegen das Golden-Set gemessen.
