# M07 Acceptance Evidence – Großdokument-Optimierung

Belegsammlung für die Trello-Karte „M07 -- Großdokument-Optimierung
(>300 Seiten)". Pflichtenheft v1, §3.2 Lieferumfang, §5 Leistungsziele,
§7 Abnahmetest 6, §9 Roadmap, §10 PT-Tabelle.

---

## 1. Pflichtenheft-Mapping

| Pflichtenheft-Stelle | Forderung | M07-Beleg |
|---|---|---|
| §5 Leistung & Skalierung | 300–350 Seiten asynchron ≤ 120 Min | Benchmark §6, Abnahme-Asserts in `tests/run_pdf_m07_benchmark.py` |
| §7 Abnahmetest 6 | „Großdokumente: 300–350 Seiten werden in ≤ 120 Min verarbeitet; Ressourcenlimits werden eingehalten." | Benchmark §6 + Resource-Snapshot §7 |
| §9 M07-Artefakte | Parallelisierung, Ressourcen-Tuning, Benchmark 300–350 Seiten | §3 Parallelisierung, §4 Ressourcen-Tuning, §6 Benchmark |
| §10 PT | 4 PT | Eingehalten |

---

## 2. Leitprinzipien (Auftraggeber-Vorgaben)

1. **Fokus Ollama** – kein Mistral-Cloud-Benchmark in M07.
2. **Strikt opt-in** – Default-Verhalten nach M07 = bytewise identisch zu
   M06.
3. **Ollama-Schonung** – prozessweiter Hard-Cap, weil Ollama auf einem
   geteilten System mit anderen Anwendungen läuft.
4. **Resource-Limits konservativ** – Zielsystem noch nicht final
   spezifiziert, daher per Override-Datei vorbereitet, nicht hart in
   `docker-compose.yml`.

---

## 3. Parallelisierung – drei abgestufte Schutzschichten

```
SecurityCheckWorkflow
  └── asyncio.gather (Workflow-Semaphore, pro Workflow)
        └── workflow.execute_activity
              └── OllamaProvider
                    └── modul-globaler Semaphore (prozessweit)
                          └── optionaler Throttle (Min-Intervall)
                                └── Ollama HTTP
```

* **Schicht 1 – Feature-Flag:** `LLM_PARALLEL_ENABLED` (Default `false`).
  Solange off greift in `workflows/security_check.py` der frühere
  Sequenz-Pfad, vgl. `_resolve_concurrency`.
* **Schicht 2 – Prozess-Cap:** `OLLAMA_MAX_CONCURRENT_REQUESTS` (Default
  `1`). Modul-globaler `asyncio.Semaphore` in `llm/ollama.py`
  (`_ollama_slot`). Greift über alle Provider-Instanzen, Activities und
  parallele Workflows hinweg.
* **Schicht 3 – Throttle:** `OLLAMA_MIN_INTERVAL_MS` (Default `0`).
  Optionaler Mindestabstand zwischen zwei Calls für sehr knapp
  dimensionierte Shared-GPU-Systeme.

Parallel-Pfad wird nur aktiv, wenn **beide** Schalter gesetzt sind
(`LLM_PARALLEL_ENABLED=true` **und** mindestens eine
`*_CONCURRENCY > 1`).

---

## 4. Ressourcen-Tuning per Override

`docker-compose.yml` bleibt frei von harten Limits, weil das Zielsystem
noch in Aufbau ist. Die Empfehlung steht stattdessen in
`docker-compose.override.yml.example` als auskommentierter Block für
`worker`, `api`, `ollama`. Bei Inbetriebnahme der finalen Hardware muss
nur die Override-Datei kopiert und angepasst werden:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
# memory:/cpus: anpassen, docker compose up -d
```

Zusätzlich konfigurierbar in `api/config.py` (alle Defaults =
Vor-M07-Verhalten):

| Setting | Default | Wirkung |
|---|---|---|
| `worker_max_concurrent_workflow_tasks` | 10 | Temporal-Worker-Cap |
| `worker_max_concurrent_activities` | 20 | Temporal-Worker-Cap |
| `llm_activity_timeout_seconds` | 600 | start_to_close für LLM-Activities |
| `max_pdf_pages` | 500 | PDF-Parser-Obergrenze |
| `max_pdf_size_bytes` | 10 MB | PDF-Parser-Obergrenze |
| `max_upload_size_bytes` | 10 MB | Multipart-Upload-Cap |

---

## 5. Test-Suite (Unit, ohne Docker)

```bash
pytest tests/test_ollama_semaphore.py \
       tests/test_workflow_concurrency.py \
       tests/test_ollama_provider.py \
       tests/test_pdf_parser.py \
       tests/test_workflows.py \
       --no-cov -q
```

Erwartet: **alles grün.**

* `tests/test_ollama_semaphore.py` – 5 Tests: Cap=1 strikt
  serialisiert, Cap=2/4 erlaubt parallel aber nie mehr als der Cap,
  Throttle erzwingt Mindestabstand, Re-Allokation bei Konfig-Wechsel.
* `tests/test_workflow_concurrency.py` – 11 Tests: Gating
  (`llm_parallel_enabled=false` ⇒ immer 1), Legacy-job_data ohne neue
  Felder bleibt sequenziell, Activity-Timeout-Default 600 s,
  Reihenfolge-Invarianz im Parallel-Pfad, messbarer Speedup.

---

## 6. Benchmark 300 Seiten – Pflichtenheft-Wortlaut

### 6.1 Fixture bauen

```bash
python tests/build_large_fixture.py
# Generating synthetic 300-page fixture -> tests/fixtures/pdf/large_synthetic_300pp.pdf
# OK -- 0.24 MB
```

Verifikation:

```bash
python -c "
from parsers.pdf import extract_pdf_text
from parsers.pdf_scene_splitter import split_into_scenes
from pathlib import Path
ft, pt, _, _ = extract_pdf_text(Path('tests/fixtures/pdf/large_synthetic_300pp.pdf').read_bytes())
blocks = split_into_scenes(ft, page_texts=pt)
print('pages=', len(pt), 'scenes=', sum(1 for b in blocks if not b.is_preamble))
"
# pages= 301 scenes= 300
```

Wichtig: Der INT/EXT-Splitter findet alle 300 Marker (kein
PAGE-Fallback). Risiko-Templates rotieren durch 15 Klassen, damit die
Risikoanalyse echte Treffer erzeugt.

### 6.2 Baseline-Lauf (Concurrency 1, M06-Verhalten)

```bash
export EKI_API_KEY="eki_..."
sed -i '' 's/^LLM_PARALLEL_ENABLED=.*/LLM_PARALLEL_ENABLED=false/' .env.local
sed -i '' 's/^OLLAMA_MAX_CONCURRENT_REQUESTS=.*/OLLAMA_MAX_CONCURRENT_REQUESTS=1/' .env.local
docker compose restart api worker

python tests/run_pdf_m07_benchmark.py --concurrency 1
```

Ergebnis: `tests/reports/m07_benchmark_<ts>.json` mit
`acceptance.passed_overall`.

### 6.3 Parallel-Lauf (Concurrency 2)

```bash
sed -i '' 's/^LLM_PARALLEL_ENABLED=.*/LLM_PARALLEL_ENABLED=true/' .env.local
sed -i '' 's/^OLLAMA_MAX_CONCURRENT_REQUESTS=.*/OLLAMA_MAX_CONCURRENT_REQUESTS=2/' .env.local
sed -i '' 's/^PDF_STRUCTURE_CONCURRENCY=.*/PDF_STRUCTURE_CONCURRENCY=2/' .env.local
sed -i '' 's/^RISK_ANALYSIS_CONCURRENCY=.*/RISK_ANALYSIS_CONCURRENCY=2/' .env.local
docker compose restart api worker

python tests/run_pdf_m07_benchmark.py --concurrency 2
```

Vergleichswert: `elapsed_sec` aus 6.3 sollte deutlich kleiner sein als
aus 6.2.

### 6.4 Akzeptanz-Asserts im Runner

Der Runner schreibt ein `acceptance`-Objekt ins Result-JSON:

```json
{
  "limit_sec": 7200,
  "passed_time": true,
  "passed_findings": true,
  "passed_overall": true
}
```

Exit-Code 0 = bestanden, 1 = nicht bestanden.

---

## 7. Ressourcen-Snapshot

`tests/reports/m07_benchmark_<ts>_stats.csv` enthält pro 10 s
Periodensnapshots von `docker stats` für `eki-api`, `eki-worker`,
`eki-ollama`. Die JSON-Datei aggregiert das zu Peak/Avg CPU und Memory:

```json
"stats_summary": {
  "eki-worker": {
    "cpu_peak_pct": 145.0,
    "cpu_avg_pct":  62.1,
    "mem_peak_pct":  18.4,
    "mem_avg_pct":   14.8,
    "samples": 420.0
  },
  "eki-ollama": { ... }
}
```

Damit ist „Ressourcenlimits werden eingehalten" auf Datenbasis prüfbar.

---

## 8. Rollback-Pfade

Drei voneinander unabhängige Rollbacks, jeweils ohne Code-Änderung:

1. **Master-Flag aus:** `LLM_PARALLEL_ENABLED=false`
   → `_resolve_concurrency` liefert immer 1
   → Workflow benutzt den strikt sequenziellen Pfad
   → bytewise identisch zu M06.
2. **Ollama-Cap = 1:** `OLLAMA_MAX_CONCURRENT_REQUESTS=1`
   → Modul-globaler Semaphore lässt nur einen Ollama-Call gleichzeitig
   → identische Last wie vor M07, auch wenn andere Schalter an sind.
3. **Per-Workflow-Concurrency = 1:** `PDF_STRUCTURE_CONCURRENCY=1`
   und `RISK_ANALYSIS_CONCURRENCY=1`
   → Helper `_run_indexed` nutzt den Sequenz-Branch
   → ebenfalls bytewise identisch zu M06.

Jeder dieser Schalter ist EIN ENV-Var-Setting + Container-Restart. Keine
DB-Migration, kein Schema-Änderung.

---

## 9. Nicht im Scope (Abgrenzung)

* Keine OCR-Implementierung (gescannte PDFs bleiben Warning + Skip).
* Kein Mistral-Cloud-Benchmark (Pflichtenheft Abnahmetest 8 ist
  separater Stage-Sign-off, fällt in M11 oder Stage-Phase).
* Keine GPU-spezifischen Optimierungen / Quantisierung – siehe M11.
* Keine DB-Migration, keine Schema-Änderung.

---

**Status M07:** Implementierung abgeschlossen, abnahmebereit. Der
Acceptance-Test-Lauf (§6) muss auf dem Zielsystem (oder einer
gleichwertigen Entwicklungsmaschine) reproduziert und das resultierende
JSON dem Auftraggeber zusammen mit dieser Datei vorgelegt werden.
