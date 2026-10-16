# UAT-Protokoll – eKI API

| Feld | Wert |
|---|---|
| Release / Tag | `v<Release-Tag>` = `core/version.py`: ______ (erwartet: `1.0.1` oder neuer, `HANDOVER.md` §1.1) |
| Git-Commit | |
| Image-Digest API / Worker | `ghcr.io/…/eki-api@sha256:…` / `ghcr.io/…/eki-worker@sha256:…` |
| DB-Migrationsstand (`docker compose exec api alembic current`) | erwartet `c4e6a8b0d2f1 (head)` |
| Docker / Docker Compose | ≥ 24 / ≥ 2.24 |
| Umgebung | ☐ lokal ☐ Stage ☐ Produktion |
| Datum / Uhrzeit | |
| Durchführende (eKI) | |
| Teilnehmende (ePro / IT / Sicherheitsbeauftragter) | |
| LLM-Provider / Modell | `LLM_PROVIDER=` / Modell-Tag |
| Ollama-Instanz (URL, Version `/api/version`) | |
| Hardware Ollama-Host (GPU, VRAM) | |

---

## 1. Automatisierte Nachweise

Nur Läufe auf dem oben eingetragenen Stand (Head `c4e6a8b0d2f1`; die Zustellphase funktioniert
auf PostgreSQL erst ab `a7c9e1f3b5d2`) zählen; ältere Läufe der Tests 2–7 sind zu wiederholen
(`UAT_TESTPLAN.md`).

| Test | Status | Protokoll-Datei | Bemerkung |
|---|---|---|---|
| AT2 Push-Fluss | ☐ bestanden ☐ nicht bestanden | `tests/reports/uat_….md` | |
| AT3 Pull-Fluss | ☐ ☐ | | |
| AT4 Retries & TTL (A/B) | ☐ ☐ | | |
| AT4 6h-Fenster (Time-Skipping) | ☐ ☐ | `pytest tests/test_m10_workflow_failover.py` Ausgabe | |
| AT5 Idempotenz | ☐ ☐ | | |
| AT6 Großdokument | ☐ ☐ | `tests/reports/m07_benchmark_….json` | Seiten: __ · Dauer: __ min · Limit 120 min |
| AT7 Log-Hygiene | ☐ ☐ | | Marker: __ · Treffer: __ |
| Unit-/Contract-Suite (SQLite) | ☐ ☐ | `pytest --no-cov` Zusammenfassung: __ passed | |
| Suite gegen PostgreSQL (`TEST_POSTGRES_URL`, `docs/TESTING_GUIDE.md`) | ☐ ☐ | `pytest` Zusammenfassung: __ passed | Coverage: __ % (Gate 95 %) |
| OpenAPI | ☐ ☐ | `spectral lint` 0 Errors | |

## 2. Manuelle Nachweise

### 2.1 AT1 – Automatischer Start (Stage)

| Schritt | Ergebnis | Beleg |
|---|---|---|
| Upload im ePro (Projekt-ID, Datei, Format) | | Screenshot |
| `job_id` in eKI (`GET /v1/ops/jobs?project_id=`) | | JSON-Auszug |
| Zeit Upload → 202 | | |

### 2.2 AT8 – Stage-Sign-off (Mistral-API)

| Schritt | Ergebnis | Beleg |
|---|---|---|
| FDX-Projekt Push → Report im ePro sichtbar | | Screenshot ePro |
| PDF-Projekt Push → Report im ePro sichtbar | | Screenshot ePro |
| Pull-Projekt → One-Shot durch ePro | | ePro-Log / eKI Job-Status |
| Paritätsreport (`run_parity.py`) | | `docs/M11_PARITY_REPORT.md` §3 |
| Sign-off ePro-Tech-Lead | Name / Datum | |

### 2.3 AT9 – Prod-Cutover (lokales Mistral)

| Schritt | Ergebnis | Beleg |
|---|---|---|
| `scripts/smoke_prod.py` | ☐ PASSED | Ausgabe (JSON-Zeile) |
| `run_parity.py --providers local_mistral` | ☐ alle Schwellen | Tabelle |
| Kein externer LLM-Verkehr (Firewall/Metriken) | | `eki_llm_requests_total` Auszug |
| Sign-off IT-Betrieb | Name / Datum | |

## 3. Querschnitt

| Kriterium | Status | Beleg |
|---|---|---|
| Authentifizierung (401/403/IDOR) | ☐ | Postman „5. Security Tests" Run-Report |
| Monitoring (Dashboards, Alerts) | ☐ | Screenshots `eki-overview`, Alertmanager |
| Betriebsdoku übergeben | ☐ | `OPERATIONS_GUIDE.md`, Runbooks |
| Postman-Collection übergeben | ☐ | v1.0 + Environment |
| Schulung durchgeführt | ☐ | `TRAINING.md`, Teilnehmerliste |
| Datenschutz-Entscheidungen (`DATENSCHUTZ.md` §8) | ☐ | Entscheidungsvermerk (Fixtures/Images, Löschfristen, Temporal-Retention, AVV) |

## 4. Abweichungen / offene Punkte

| # | Beschreibung | Schwere | Maßnahme | Verantwortlich | Termin |
|---|---|---|---|---|---|
| | | | | | |

## 5. Entscheidung

☐ Abgenommen ☐ Abgenommen mit Auflagen (siehe §4) ☐ Nicht abgenommen

| Rolle | Name | Datum | Unterschrift |
|---|---|---|---|
| Auftraggeber (Filmakademie) | | | |
| Auftragnehmer (StefanAI) | | | |
