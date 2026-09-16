# M12 Acceptance Evidence – UAT-Paket & Übergabe

Belegsammlung für die Trello-Karte „M12 – UAT-Paket & Übergabe". Pflichtenheft v1,
§5 (Entwickler-Erlebnis & Doku: Swagger/Redoc, Postman, versioniertes Änderungsprotokoll;
≥ 80 % Testabdeckung in der Kernlogik), §7 Abnahme (Testumfang, 9 Abnahmetests,
Abnahmekriterien), §9 M12-Artefakte (UAT-Paket, OpenAPI v1.0, Betriebsdoku,
Go-Live-Checkliste, Schulungsunterlagen), §10 PT-Tabelle (4 PT), §12 Rechte & Eigentum.

---

## 1. Pflichtenheft-Mapping

| Pflichtenheft-Stelle | Forderung | M12-Beleg |
|---|---|---|
| §2 / §5 Design-First | OpenAPI 3.1.1 als Single Source of Truth | `openapi/eki-api-v1.0.yaml` (1.0.0), Spectral 0 Errors/0 Warnings (CI-Gate), `tests/test_m12_openapi_contract.py` prüft Routen ⇄ Spec, Enums, Version, Webhook-Payload |
| §5 Doku | Swagger/Redoc, Beispielanfragen, Postman-Sammlung, versioniertes Änderungsprotokoll | Swagger/ReDoc im Dev-Modus, Postman v1.0 + Environment, `CHANGELOG.md` (M01–M12) |
| §5 Qualität | ≥ 80 % Testabdeckung in der Kernlogik | Gesamt-Coverage 81 % (`api`, `core`, `services`, `workflows`, `parsers`, `llm`, `worker`); CI `--cov-fail-under=80` |
| §7 Testumfang 1 | Unit, Contract, Integration Push/Pull/Idempotenz, Security, Last | 460+ Tests inkl. Contract-Test, Failover-Tests auf Temporal-Testserver, Security-Suite, Benchmark-Runner |
| §7 Testumfang 2–3 | Stage-Betrieb (Mistral-API), Produktionsbetrieb (lokal) | `UAT_TESTPLAN.md` AT8/AT9, `scripts/run_parity.py`, `scripts/smoke_prod.py` |
| §7 Testumfang 4 | KB: TTL und nur freigegebene Quellen | Temporal-Schedule `eki-kb-cleanup` (M09), Tenant-Scoping + Tag-Filter (M06), Training Modul C |
| §7 Abnahmetests 1–9 | müssen bestehen | `docs/UAT/UAT_TESTPLAN.md` (Zuordnung Automatisierung/manuell), `scripts/uat/run_acceptance_tests.py` (2, 3, 4, 5, 6, 7), Protokollvorlage |
| §7 Abnahmekriterien | OpenAPI gültig, Auth + Monitoring laufen, Betriebsdoku + Postman | siehe §2 unten |
| §9 M12 | UAT-Paket, OpenAPI v1.0, Betriebsdoku, Go-Live-Checkliste, Schulungsunterlagen | `docs/UAT/*`, `docs/OPERATIONS_GUIDE.md`, `docs/UAT/GO_LIVE_CHECKLIST.md`, `docs/UAT/TRAINING.md` |
| §12 | Rechte & Eigentum, Drittkomponenten | `docs/UAT/HANDOVER.md` §6 |
| §10 PT | 4 PT | Eingehalten |

---

## 2. Abnahmekriterien (gesamt)

| Kriterium | Status | Nachweis |
|---|---|---|
| OpenAPI gültig | erfüllt | `spectral lint openapi/eki-api-v1.0.yaml --fail-severity=error` → „No results with a severity of 'error' found" |
| Authentifizierung läuft | erfüllt | `tests/test_security.py`, `tests/test_m10_ops_and_idempotency.py::TestOpsAuthorization`, Postman „5. Security Tests" |
| Monitoring läuft | erfüllt | M09-Stack, `tests/test_m09_*`, Dashboards/Alerts validiert in CI |
| Alle 9 Tests bestanden | 2–7 automatisiert bestanden (lokal); 1, 8, 9 vorbereitet (Stage/Prod-abhängig) | `run_acceptance_tests.py`, Protokollvorlage |
| Betriebsdoku liegt vor | erfüllt | `OPERATIONS_GUIDE.md`, `DEPLOYMENT_GUIDE.md`, `M09_SLO.md`, `M10_FAILOVER_RUNBOOK.md`, `M06_KB_GUIDE.md`, `EPRO_INTEGRATION_GUIDE.md` |
| Postman-Collection liegt vor | erfüllt | `postman/eKI-API-v1.0.postman_collection.json` + Environment |

---

## 3. UAT-Paket

| Dokument | Inhalt |
|---|---|
| `docs/UAT/UAT_TESTPLAN.md` | Übersicht der 9 Abnahmetests (Umgebung, Automatisierung, Nachweis), Vorbereitung, Detailschritte, Abnahme-Ablauf (Trello) |
| `docs/UAT/UAT_PROTOCOL_TEMPLATE.md` | Protokollvorlage mit Feldern für automatisierte und manuelle Nachweise, Abweichungen, Unterschriften |
| `docs/UAT/GO_LIVE_CHECKLIST.md` | Vorbedingungen, Konfiguration/Secrets, Deployment, Verifikation, ePro-Integration, Betrieb, fachliche Inbetriebnahme |
| `docs/UAT/TRAINING.md` | Drei Module: ePro-Integratoren, IT-Betrieb, Sicherheitsbeauftragter |
| `docs/UAT/HANDOVER.md` | Lieferumfang ⇄ Ablageort, Meilenstein-Nachweise, Zugänge, Drittabhängigkeiten, bekannte Einschränkungen, Rechte |
| `scripts/uat/mock_epro_server.py` | ePro-Attrappe: `set-risk-assessment` (201/422/503/flaky), Webhook-Empfang, Kontrollendpunkte; speichert nur Metadaten |
| `scripts/uat/run_acceptance_tests.py` | AT2–AT7 gegen laufenden Stack; JSON- und Markdown-Protokoll; Marker-basierter Log-Hygiene-Check |

---

## 4. Spezifikation, Postman, Changelog

- OpenAPI 1.0.0: neue `Operations`-Endpunkte, `X-One-Shot`- und `X-Request-ID`-Header,
  `JobStatus.delivering`, `DeliveryStatus`, `JobDeliveryMetadata`, Dead-Letter-Schemata,
  `Forbidden`-Response, `RiskCategory` referenziert, Webhook-Gründe vollständig, KB-Parameter beschrieben.
- Contract-Test (`tests/test_m12_openapi_contract.py`, 12 Tests): jede FastAPI-Route dokumentiert und jede
  dokumentierte Route implementiert; Enums `JobStatus`/`RiskLevel`/`ScriptFormat`/`DeliveryStatus` identisch;
  Version ⇄ `core/version.py`; Webhook-Payload inhaltsfrei; Fehlerformat `ErrorResponse` zur Laufzeit.
- Postman v1.0: Ordner 9 (Operations, Admin-Key) und 10 (Abnahmetests AT3/AT5/AT7), `X-One-Shot`-Assertion,
  Environment-Datei mit allen Variablen; v0.5/v0.6 entfernt.
- `CHANGELOG.md`: Keep-a-Changelog, alle Releases 0.1.0 → 1.0.0.

---

## 5. Qualitätsmaßnahmen im Sprint

- **Coverage-Messfehler behoben:** SQLAlchemy-Async wechselt Greenlets in `await session.execute(...)`;
  ohne `concurrency = ["thread", "greenlet"]` verlor der Tracer jeden Handler-Frame nach dem ersten DB-Aufruf
  (Router wurden mit ~40 % statt ~75 % gemessen). Nach dem Fix: 81 % Gesamtabdeckung.
- Neue Tests: Rate-Limiting (Proxy-Modell, 429, Fail-open), Factory-Smoke-Helper, Delivery-Bookkeeping
  gegen die Test-DB (`_mark_delivering` idempotent, `_record_attempt`, Dead Letter aus Job-Daten,
  Retrieval-Check).
- Toter M01-Stub `services/security_service.py` entfernt.

---

## 6. Tests

```bash
.venv/bin/python -m pytest -q                                    # gesamte Suite, Coverage-Gate 80 %
.venv/bin/python -m pytest tests/test_m12_openapi_contract.py -v
npx @stoplight/spectral-cli lint openapi/eki-api-v1.0.yaml --ruleset .spectral.yaml
```

Ergebnis: alle Tests grün, Coverage 81 %, Spectral 0 Errors.

---

## 7. Offene externe Schritte (nicht im Repo abschließbar)

| Schritt | Verantwortlich | Vorbereitung im Paket |
|---|---|---|
| AT1 Automatischer Start aus Stage-ePro | ePro-Team | Testplan §3, Protokoll §2.1 |
| AT6 Großdokument auf Zielhardware | IT + eKI | `tests/run_pdf_m07_benchmark.py`, `--with-large-document` |
| AT8 Stage-Sign-off mit Mistral-API | ePro-Team + eKI | Testplan §3, `run_parity.py`, Protokoll §2.2 |
| AT9 Prod-Cutover mit lokalem Mistral | IT + eKI | `smoke_prod.py`, `OPERATIONS_GUIDE.md` §6, Protokoll §2.3 |
| Schulungen A/B/C | eKI | `TRAINING.md` |
| Supportvertrag | Filmakademie + StefanAI | Go-Live-Checkliste A |

---

**Status M12:** UAT-Paket vollständig, Release 1.0.0 vorbereitet (Tag `v1.0.0` nach Merge des
Release-Branches; CI publiziert die GHCR-Images).
