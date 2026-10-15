# eKI API – Test- und Coverage-Bericht

**Stand:** 2026-10-15 (Übergabe-Stabilisierung, siehe `CHANGELOG.md` → *Unreleased*)
**Gemessen:** Python 3.11, `pip install -c requirements-dev.lock -e ".[dev]"`, PostgreSQL 16 + pgvector,
Redis 7, Tesseract (`deu`, `eng`) – genau wie der CI-Job `test`:

```bash
TEST_POSTGRES_URL=postgresql+asyncpg://eki_user:eki_password@localhost:5432/eki_test \
TEST_REDIS_URL=redis://localhost:6379/6 ENV=test \
pytest --cov --cov-report=term-missing --cov-fail-under=95
```

Wie die Suite aufgebaut ist und lokal läuft: `docs/TESTING_GUIDE.md`. Der frühere Bericht (Stand
0.5.0, 167 Tests, 65,75 %) ist durch dieses Dokument ersetzt.

---

## Zusammenfassung

| Kennzahl | Vorher (v1.0.0-Tag) | Jetzt |
|---|---|---|
| Tests gesamt | 465 | **1742** |
| PostgreSQL-Lauf | – (nie ausgeführt) | **1741 bestanden, 1 xfail** |
| SQLite-Lauf | 463 bestanden, 2 übersprungen | **1703 bestanden, 38 übersprungen** (reine PostgreSQL-Tests), 1 xfail |
| Line-Coverage gesamt | 80,78 % (SQLite) | **98,59 %** (PostgreSQL-Lauf) |
| CI-Gate | 80 % | **95 %** |
| Laufzeit | ~15 s | ~60 s (SQLite) / ~150 s (PostgreSQL inkl. Coverage) |

Das eine `xfail` (`tests/test_e2e_pipeline.py::test_sync_check_returns_the_real_analysis`) dokumentiert
eine bekannte Grenze: der synchrone Endpunkt `POST /v1/security/check` ist ein M01-Stub. Er ist als
`xfail(strict=True)` hinterlegt und wird rot, sobald der Endpunkt echt analysiert.

## Tests nach Ebene

| Ebene | Dateien | Tests |
|---|---|---|
| Unit (Lücken geschlossen) | `test_cov_parsers.py`, `test_cov_llm_providers.py`, `test_cov_activities.py`, `test_cov_security_check_workflow.py`, `test_cov_api.py` | 505 |
| Edge-Cases / Grenzwerte | `test_edge_api.py`, `test_edge_parsers_and_services.py` | 698 |
| End-to-End (API → Temporal → echte Activities → Redis → Pull/Push an ePro-Mock) | `test_e2e_pipeline.py`, `test_m09_context_propagation_e2e.py` | 18 |
| PostgreSQL-Integration (Migrationen, pgvector-KB, Zustell-Buchhaltung) | `test_pg_integration.py`, `test_db_dialect_regressions.py` | 39 |
| Contract (FastAPI ⇄ OpenAPI) | `test_m12_openapi_contract.py` | 13 |
| Bestehende Modul- und Meilenstein-Tests | übrige `tests/test_*.py` | 469 |

## Coverage nach Modul

„Vorher" = Messung am v1.0.0-Stand (SQLite, `worker/` nicht gemessen); „Jetzt" = PostgreSQL-Lauf.

| Modul | Vorher | Jetzt | Nicht gedeckt / Anmerkung |
|---|---:|---:|---|
| `api/config.py` | 87,36 % | 98,88 % | 402, 420: JSON-Listen-Prüfung, unerreichbar |
| `api/dependencies.py` | 71,93 % | 100 % | |
| `api/main.py` | 79,14 % | 100 % | |
| `api/rate_limiting.py` | 95,52 % | 97,01 % | |
| `api/routers/health.py` | 86,36 % | 100 % | |
| `api/routers/knowledge_base.py` | 92,78 % | 100 % | |
| `api/routers/ops.py` | 95,96 % | 98,67 % | |
| `api/routers/security.py` | 91,10 % | 85,59 % ¹ | Messartefakt, siehe unten |
| `core/db_models.py` | 96,80 % | 96,85 % | `__repr__` |
| `core/models.py` | 92,36 % | 100 % | |
| `core/prompt_sanitizer.py` | 95,35 % | 96,61 % | Metrik-Fallback |
| `core/temporal_context.py` | 91,67 % | 100 % | |
| `llm/base.py` | 75,00 % | 100 % | |
| `llm/mistral_cloud.py` | 69,37 % | 100 % | |
| `llm/ollama.py` | 70,44 % | 99,55 % | |
| `llm/local_mistral.py`, `factory.py`, `prompt_manager.py` | 91–97 % | 100 % | |
| `parsers/pdf.py` | 49,28 % | 100 % | |
| `parsers/fdx.py` | 93,44 % | 100 % | |
| `parsers/pdf_llm_structurer.py`, `pdf_ocr.py`, `secure_xml.py`, `pdf_scene_splitter.py` | 82–98 % | 100 % | |
| `services/knowledge_base.py` | 48,82 % | 97,30 % | braucht pgvector (nur PostgreSQL-Lauf) |
| `services/report_generator.py` | 100 % | 99,37 % | |
| `services/secure_buffer.py` | 95,24 % | 100 % | |
| `services/taxonomy.py` | 99,07 % | 99,26 % | |
| `workflows/activities.py` | 55,15 % | 100 % | |
| `workflows/security_check.py` | 69,76 % | 99,33 % | Vor-Patch-Abbruchpfad, unerreichbarer Guard |
| `workflows/maintenance.py` | 97,96 % | 100 % | |
| `worker/main.py` | – | 97,67 % | `__main__`-Guard |
| **Gesamt** | **80,78 %** | **98,59 %** | |

¹ Die als ungedeckt gemeldeten Zeilen in `api/routers/security.py` (u. a. 166–187, `_resolve_json`)
laufen in Hunderten Tests – die Edge-Case-Tests prüfen die dort erzeugten Fehlermeldungen wörtlich.
coverage.py verliert bei ASGI-Requests über den `TestClient` (Thread-Portal + greenlet) den Frame nach
`await request.body()`. Gegenprobe: mit reinem Thread-Tracing fällt das Modul auf 54 %; die
konfigurierte `concurrency = ["thread", "greenlet"]` misst am vollständigsten. Die Gesamtzahl ist
daher eine Untergrenze. Der Rückgang gegenüber „vorher" kommt zusätzlich durch neuen Validierungscode.

## Was die neuen Tests gefunden haben

Die Tests haben über 40 Fehler aufgedeckt, die alle behoben und per Regressionstest abgesichert sind
(Details: `CHANGELOG.md` → *Unreleased*). Die gravierendsten:

- **Installation/Betrieb:** frische Installation ohne `greenlet`, `alembic upgrade` mit falschem
  Treiber, `VARCHAR(9)`-Statusspalte (jeder asynchrone Job scheiterte in der Zustellphase),
  Prod-Overlay band den Quellcode vom Host ein und veröffentlichte die Temporal-UI,
  Platzhalter-Secrets wurden in Produktion akzeptiert.
- **Sicherheitsanalyse:** LLM-Fehler und Filter-Treffer wurden als „keine Risiken" gemeldet;
  der Prompt-Injection-Filter blockte normalen Drehbuchtext („User:innen", „---");
  lange Szenen wurden ab 10 000 Zeichen abgeschnitten; FDX-Inhalte ohne Szenenüberschrift bzw. in
  `<DualDialogue>` gingen verloren; OCR-Seiten liefen unter Last in den Timeout.
- **Datenschutz:** Findings mit Drehbuchzitaten in der Temporal-History, Skriptinhalt nach
  Verarbeitungsfehlern 6 h im Puffer, Bildschirm-Vorschau von Drehbuchtext im Log.
- **API:** über 15 Eingaben führten zu 500/503 statt 4xx, Pfad-Traversal über `project_id` in die
  ePro-Push-URL, Rate-Limit-Umgehung über die Schreibweise des `Authorization`-Headers,
  Platzhalter-Report mit 0 Befunden nach Ablauf des Puffers.
