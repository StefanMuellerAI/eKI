# Übergabedokument – eKI API v1.0.0

Pflichtenheft §9 M12 „UAT-Paket (Testskripte, Protokolle, OpenAPI v1.0, Betriebsdoku),
Go-Live-Checkliste, Schulungsunterlagen" und §12 „Rechte & Eigentum".

## 1. Lieferumfang (Pflichtenheft §3.2 v1.0) und Ablageort

| Position | Stand | Ablage |
|---|---|---|
| Sicherheitsprüfung: Parser (FDX, PDF + OCR), Regelwerk, Scoring, Report (JSON/PDF) | fertig | `parsers/`, `services/taxonomy.py`, `services/report_generator.py`, `config/taxonomy/` |
| Outbound-Adapter (Push/Pull, Idempotenz, Retries, Dead Letters) | fertig | `workflows/`, `api/routers/security.py`, `api/routers/ops.py` |
| Write-Through-Löschlogik | fertig | `services/secure_buffer.py`, `deliver_report_activity`, `cleanup_buffer_activity` |
| LLM-Adapter Mistral API und lokales Mistral | fertig | `llm/mistral_cloud.py`, `llm/local_mistral.py`, `llm/factory.py` |
| KB-Grundlage (Dokumentenaufnahme, Embeddings) | fertig | `services/knowledge_base.py`, `api/routers/knowledge_base.py`, `scripts/seed_kb.py` |
| OpenAPI-Doku | v1.0.0 | `openapi/eki-api-v1.0.yaml`, Swagger/ReDoc im Dev-Modus |
| Container-Setup | fertig | `docker/`, `docker-compose*.yml`, GHCR-Images bei `v*`-Tags |
| Monitoring-Dashboards | fertig | `docker/observability/`, `docs/M09_SLO.md` |
| Betriebsdoku | fertig | `docs/OPERATIONS_GUIDE.md`, `DEPLOYMENT_GUIDE.md`, Runbooks |
| Postman-Collection | v1.0 | `postman/` |
| UAT-Paket | fertig | `docs/UAT/`, `scripts/uat/`, `scripts/smoke_prod.py`, `scripts/run_parity.py` |
| Änderungsprotokoll | fertig | `CHANGELOG.md` |

## 2. Meilenstein-Nachweise

| Meilenstein | Evidence-Dokument |
|---|---|
| M01 | `docs/M01_COMPLETION_REPORT.md` |
| M02–M05 | README-Abschnitte, `docs/TEST_REPORT.md`, `docs/COVERAGE_REPORT.md` |
| M06 | `docs/M06_ACCEPTANCE_EVIDENCE.md`, `M06_KB_GUIDE.md`, `M06_PROD_DEPLOY.md` |
| M07 | `docs/M07_ACCEPTANCE_EVIDENCE.md` |
| M08 | `docs/M08_ACCEPTANCE_EVIDENCE.md` |
| M09 | `docs/M09_ACCEPTANCE_EVIDENCE.md`, `M09_SLO.md` |
| M10 | `docs/M10_ACCEPTANCE_EVIDENCE.md`, `M10_FAILOVER_RUNBOOK.md` |
| M11 | `docs/M11_ACCEPTANCE_EVIDENCE.md`, `M11_PARITY_REPORT.md`, `OPERATIONS_GUIDE.md` |
| M12 | dieses Verzeichnis |

## 3. Zugänge und Geheimnisse (zu übergeben, nicht im Repo)

| Gegenstand | Übergabe an | Form |
|---|---|---|
| GitHub-Repository (Eigentum nach §12) | Filmakademie IT | Owner-Rechte / Transfer |
| GHCR-Images | Filmakademie IT | Pull-Rechte, Digest-Liste |
| API-Keys (ePro-Service, Ops-Admin, Prometheus) | ePro-Team / IT | Passwort-Manager, einmalig |
| `.env.local` der Produktion | IT | verschlüsselt, keine Kopie beim Auftragnehmer |
| Grafana-Admin | IT | Passwort-Manager |

## 4. Abhängigkeiten von Dritten (Pflichtenheft §8)

| Abhängigkeit | Status | Verantwortlich |
|---|---|---|
| Stage-ePro mit Testfilmprojekten (AT1, AT8) | offen / terminiert: ____ | ePro-Team |
| Produktiv-Hardware GPU ≥ 24 GB (AT6, AT9) | offen / terminiert: ____ | IT |
| Fachinhalte für die KB | offen / laufend | Sicherheitsbeauftragter |
| Supportvertrag (§14) | offen / geschlossen: ____ | Filmakademie / StefanAI |

## 5. Bekannte Einschränkungen

- Paritäts- und Großdokument-Messwerte liegen erst nach dem Lauf auf der Zielhardware vor
  (`M11_PARITY_REPORT.md` §3, `M07_ACCEPTANCE_EVIDENCE.md` §6); die Werkzeuge sind vollständig.
- Dead Letters lassen sich nicht aus der eKI heraus erneut zustellen (Inhalt ist gelöscht);
  Recovery = erneuter Upload im ePro. Bewusste Konsequenz des Processing-Only-Prinzips.
- Der synchrone Endpoint `POST /v1/security/check` liefert eine Stub-Antwort (M01); die
  produktive Analyse läuft asynchron (Pflichtenheft §4.1 nennt `check(:async)`; ePro nutzt async).
- OCR ist auf `OCR_MAX_PAGES` (Default 50) begrenzt; darüber werden Scan-Seiten mit Warnung übersprungen.
- Tech-Stack-Abweichungen zum Pflichtenheft §4.1, bewusst und dokumentiert: Temporal statt
  Celery/Flower (durable Timer für 6-h-Fenster), kein PgBouncer (Async-Pool von SQLAlchemy genügt
  bei der erwarteten Last).

## 6. Rechte & Eigentum (Pflichtenheft §12)

Mit vollständiger Vergütung gehen Eigentums- und Nutzungsrechte am Quellcode, den
Konfigurationen und der Dokumentation an die Filmakademie Baden-Württemberg über.
Drittkomponenten verbleiben unter ihren Lizenzen (siehe `pyproject.toml`; alle
Abhängigkeiten sind OSS mit MIT-/BSD-/Apache-kompatiblen Lizenzen; pdfplumber MIT,
reportlab BSD, Tesseract Apache-2.0, Temporal MIT, Ollama MIT).

## 7. Kontakt nach Übergabe

StefanAI – Research & Development, info@stefanai.de, +49 177 5228242 (gemäß Supportvertrag).
