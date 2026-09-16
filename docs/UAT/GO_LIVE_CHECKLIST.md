# Go-Live-Checkliste – eKI API v1.0.0

Abzuarbeiten nach abgenommenem UAT (`UAT_PROTOCOL_TEMPLATE.md` §5) und vor dem
produktiven Freischalten im eProjekt. Verantwortlichkeiten: **IT** = IT-Betrieb
Filmakademie, **eKI** = Auftragnehmer, **ePro** = eProjekt-Team, **SB** = Sicherheitsbeauftragter.

## A. Vorbedingungen

- [ ] UAT abgenommen; offene Punkte §4 des Protokolls haben Termin und Verantwortlichen (eKI/IT)
- [ ] Supportvertrag geschlossen (Pflichtenheft §14, spätestens mit M10) (IT/eKI)
- [ ] Release-Tag `v1.0.0` gesetzt, GHCR-Images vorhanden, Digest im Protokoll (eKI)
- [ ] Zielhardware: GPU ≥ 24 GB VRAM, NVIDIA Container Toolkit, Docker ≥ 24 (IT)

## B. Konfiguration & Secrets (IT)

- [ ] `.env.local` aus `.env.example` erstellt; `ENV=production`, `DEBUG=false`
- [ ] Starke Secrets: `API_SECRET_KEY`, `POSTGRES_PASSWORD`, `DATABASE_URL` (Validator lehnt Defaults ab)
- [ ] `LLM_PROVIDER=local_mistral`, `LOCAL_MISTRAL_MODEL=mistral-small3.2`, **kein** `MISTRAL_API_KEY`
- [ ] `EPRO_BASE_URL` = Prod-ePro, `EPRO_WEBHOOK_URL` (falls ePro den Webhook anbietet)
- [ ] `CORS_ORIGINS`, `TRUST_PROXY_HEADERS=true` + `TRUSTED_PROXY_IPS` (nur hinter Reverse-Proxy)
- [ ] `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT=36000`, `BUFFER_TTL_SECONDS=21600`
- [ ] `KB_RETRIEVAL_ENABLED` erst `true`, wenn SB die KB-Inhalte freigegeben hat
- [ ] `secrets/prometheus_api_key.txt` für den Scraper; Grafana-Admin-Passwort gesetzt

## C. Deployment (IT/eKI)

- [ ] `docker compose -f docker-compose.yml -f docker-compose.prod.yml [-f docker-compose.observability.yml] pull && up -d --no-build`
- [ ] `alembic upgrade head` → Revision `f1a2b3c4d5e6`
- [ ] `ollama pull mistral-small3.2`, `ollama pull bge-m3`
- [ ] API-Keys erzeugt: ePro-Service-Key, Ops-Admin-Key, Prometheus-Key; Klartext sicher übergeben
- [ ] Reverse-Proxy/TLS für `eki-api:8000` (Pflichtenheft §5 „durchgehend verschlüsselt (TLS)")
- [ ] Firewall: nur ePro → eKI:443; ausgehend nur ePro, GHCR, Ollama-Registry; `api.mistral.ai` blockiert

## D. Verifikation (eKI)

- [ ] `curl /health` → `version=1.0.0`
- [ ] `python scripts/smoke_prod.py` → `RESULT: PASSED`
- [ ] `python scripts/run_parity.py --providers local_mistral` → alle Schwellen ✅ (→ `M11_PARITY_REPORT.md`)
- [ ] Grafana `eki-overview` zeigt API/Worker up; Alertmanager-Testalarm empfangen
- [ ] Temporal-Schedule `eki-kb-cleanup` existiert
- [ ] Log-Stichprobe: JSON-Format, `request_id` vorhanden, keine Inhalte

## E. ePro-Integration (ePro)

- [ ] Service-Key im ePro hinterlegt; `delivery`-Modus (push/pull) festgelegt (Pflichtenheft §4.2)
- [ ] `Idempotency-Key`-Header wird ePro-seitig dedupliziert (Guide §4b)
- [ ] Webhook-Endpunkt für `security.delivery.failed` bereit (optional)
- [ ] Erster produktiver Upload gemeinsam beobachtet (AT1 in Prod), Report im ePro sichtbar

## F. Betrieb (IT)

- [ ] Backup-Job `pg_dump` täglich eingerichtet (nur Metadaten/KB; Redis nicht sichern)
- [ ] Alert-Receiver (Mail/Teams) in `docker/observability/alertmanager.yml` konfiguriert
- [ ] Runbooks bekannt: `M10_FAILOVER_RUNBOOK.md`, `OPERATIONS_GUIDE.md` §5
- [ ] Key-Rotationsplan (jährlich) und Ansprechpartner dokumentiert
- [ ] Brückenmeeting-Rhythmus (14 Tage) für die ersten 3 Monate bestätigt (Pflichtenheft §13)

## G. Fachliche Inbetriebnahme (SB)

- [ ] Echte KB-Dokumente (Leitfäden, Stunt-SOPs, Checklisten) in `config/kb_seed/real/` geliefert
- [ ] `scripts/seed_kb.py --wipe-placeholders && --reseed` ausgeführt, `--status` geprüft
- [ ] Taxonomie/Maßnahmenkatalog (`config/taxonomy/*.yaml`) fachlich freigegeben
- [ ] `KB_RETRIEVAL_ENABLED=true` gesetzt und Stichprobe eines Reports gesichtet

Go-Live freigegeben am: __________  durch: __________ (IT) / __________ (eKI) / __________ (ePro)
