# M06 — LLM-Adapter (Mistral Cloud) & Knowledge Base Guide

**Status:** Implementiert (additive Erweiterung von M05); Stand dieses Leitfadens: Oktober 2026 (Release 1.0.1)
**Default-Verhalten:** Knowledge-Base-Retrieval ist standardmäßig AUS. Der Risk-Flow ist byte-identisch mit M05 bis das Feature-Flag aktiviert wird.

Alle Einstellungen: [`CONFIGURATION.md`](CONFIGURATION.md); Datenhaltung der KB: [`DATENSCHUTZ.md`](DATENSCHUTZ.md) §3.2.

---

## 1. Was M06 mitbringt

| Lieferumfang Pflichtenheft | Artefakt |
|---|---|
| Cloud-Adapter (Mistral API) | `llm/mistral_cloud.py` mit nativem `response_format=json_object` + Schema-Validierung + Self-Correcting-Retry |
| Prompt-Templates | KB-Kontext-Block in `config/prompts/prompts.yaml` (`{kb_context}`-Platzhalter) |
| KB-Ingest (pgvector) | Migration `20260521_m06_knowledge_base.py` + `services/knowledge_base.py` + Endpoints `/v1/kb/*` |
| Beispielunterlagen | Sechs synthetische Sicherheits-SOPs in `config/kb_seed/placeholders/`, austauschbar via `scripts/seed_kb.py` |

Zusätzlich: ein Pre-Existing-Bug im `PromptManager` ist behoben (System-Prompt-Variablen wurden bisher nicht substituiert), siehe Abschnitt 6.

---

## 2. Architektur

```
                                              KB_RETRIEVAL_ENABLED?
                                                       │
                                              ┌────────┴────────┐
                                              │                 │
   analyze_scene_risk_activity                ▼ false           ▼ true
   ──────────────────────────                                   │
                                                          KBService.search
                                                                │
   build prompt with                                       (top_k via cosine
   {taxonomy_context} +  ───────────────────►  kb_context  distance, tenant-
   {kb_context}                                            scoped, expires>now)
                                                                │
   LLM (Ollama / Mistral Cloud) ◄──────────────────────────────┘
                │
                ▼
        Findings JSON (Schema-validated)
```

Wenn das Flag `KB_RETRIEVAL_ENABLED=false` ist, wird der Suchpfad nicht einmal importiert; `kb_context` bekommt den Platzhalter `"(none)"` und das Prompt-Template verträgt das fugenlos.

---

## 3. Setup

### 3.1 Dependencies installieren

```bash
# Neue Python-Dependencies aus pyproject.toml
uv pip install -e . --python .venv/bin/python   # oder pip install -e .

# Embedding-Modell auf der externen Ollama-Instanz herunterladen (Standard; auf dem Ollama-Host)
ollama pull mxbai-embed-large
# lokal mit dem Overlay docker-compose.ollama.yml stattdessen:
# docker compose -f docker-compose.yml -f docker-compose.ollama.yml exec ollama ollama pull mxbai-embed-large
```

**Embedding-Modell vor dem ersten Befüllen festlegen.** Standard ist `mxbai-embed-large`
(`OLLAMA_EMBEDDING_MODEL=mxbai-embed-large`, `OLLAMA_EMBEDDING_MAX_CHARS=1000`; das Modell
verarbeitet nur ca. 512 Token, längere Chunks werden für den Vektor gekürzt). Alternative ist
`bge-m3` (8192 Token): `ollama pull bge-m3` und **beide** Werte setzen,
`OLLAMA_EMBEDDING_MODEL=bge-m3` und `OLLAMA_EMBEDDING_MAX_CHARS=30000`. Beide liefern die von
der Spalte `kb_embeddings.vector` verlangten 1024 Dimensionen. Vektoren verschiedener Modelle
sind nicht vergleichbar: Ein späterer Wechsel erfordert, die KB zu leeren und alle Dokumente neu
einzuspielen (§5).

### 3.2 Migration ausführen

```bash
docker compose exec api alembic upgrade head
docker compose exec api alembic current
# M06 hat die Revision e8f1c2d3a401 eingeführt; aktueller Head: c4e6a8b0d2f1
```

Die Migration legt zwei Tabellen an (`kb_documents`, `kb_embeddings`) und einen `ivfflat`-Index. Bestehende Tabellen werden nicht verändert.

### 3.3 Settings prüfen

In `.env.local`:

```
OLLAMA_EMBEDDING_MODEL=mxbai-embed-large
OLLAMA_EMBEDDING_MAX_CHARS=1000     # bei bge-m3: 30000
KB_RETRIEVAL_ENABLED=false          # Default: AUS
KB_DEFAULT_TENANT_ID=00000000-0000-0000-0000-000000000001
KB_TOP_K=3
KB_MAX_CHUNK_CHARS_IN_PROMPT=600
```

---

## 4. Test-KB befüllen (Bernd-Stand-in)

`scripts/seed_kb.py` läuft auf einem Rechner mit Python ≥ 3.11 und `requests`
(`pip install requests`) und spricht die API über HTTP an (`EKI_API_URL`, Standard
`http://localhost:8000`). Seit 1.0.1 akzeptieren die KB-Endpunkte nur **Admin-Keys**
(`python scripts/create_api_key.py --insert --admin`, im Container per `docker compose exec api …`);
ein normaler Key erhält 403, und `seed_kb.py` nennt dann den Befehl zum Anlegen eines Admin-Keys.
`uploaded_by` ist die `user_id` des Admin-Keys.

```bash
# API-Key in env setzen
export EKI_API_URL="http://localhost:8000"
export EKI_API_KEY="eki_<admin_key>"

# Sechs Placeholder-Dokumente uploaden (idempotent)
python scripts/seed_kb.py --seed-placeholders

# Status prüfen
python scripts/seed_kb.py --status
# Erwartete Ausgabe:
#   Total documents:  6
#   Placeholder docs: 6
#   Real docs:        0
#   Total chunks:     ...
```

Die Placeholder decken die wichtigsten Risikokategorien ab:

| Datei | Themen | Tags |
|---|---|---|
| `01_stunt_sop.md` | Stunts, Stürze, Kämpfe | `placeholder, physical, stunts, falls, fights` |
| `02_fire_sfx_safety.md` | Feuer, Pyro, SFX, Rauch | `placeholder, physical, fire, sfx, pyro, smoke` |
| `03_vehicle_action_guidelines.md` | Fahrzeuge, Stunt-Driving | `placeholder, physical, vehicles, stunt-driving` |
| `04_height_rigging_protocol.md` | Höhen, Rigging, Fall-Arrest | `placeholder, physical, height, rigging` |
| `05_intimacy_coordination_checklist.md` | Intimacy, Closed Set | `placeholder, psychological, intimacy, sexualized` |
| `06_psychological_briefing_procedure.md` | Trauma, Briefing, Debriefing | `placeholder, psychological, trauma, violence` |

---

## 5. Echte Inhalte einspielen (Bernd liefert)

Der Inhalt von `config/kb_seed/real/` ist git-ignoriert und seit 1.0.1 per `.dockerignore` vom
Image-Build ausgeschlossen. Im Entwicklungs-Stack sieht der API-Container den Ordner über den
Bind-Mount; in Produktion (ohne Bind-Mounts) `scripts/seed_kb.py` auf dem Host ausführen. Die
Dokumente nach dem Einspielen dort trotzdem entfernen.

```bash
# Bernds Dokumente in config/kb_seed/real/ ablegen (.pdf / .md / .txt)
cp ~/Downloads/Stunt-Leitfaden-FABW-2025.pdf config/kb_seed/real/

# Placeholders entfernen (echte Docs bleiben unberührt)
python scripts/seed_kb.py --wipe-placeholders

# Alles aus real/ ingesten
python scripts/seed_kb.py --reseed

# Oder Einzeldokument:
python scripts/seed_kb.py --add path/to/doc.pdf --title "Stunt-SOP FABW 2025" --tags stunt,official
```

Die KB ist **Single-Tenant** (Filmakademie). Tag-basierte Suche unterstützt späteren Multi-Tenant-Ausbau ohne Schema-Migration.

---

## 6. Pre-Existing-Fix: PromptManager

Vor M06 wurde der System-Prompt im `PromptManager.get()` nicht mit `.format(**kwargs)` aufgelöst (`llm/prompt_manager.py:56`). Das hat dazu geführt, dass `{taxonomy_context}` als Literal an das LLM ging — die wahrscheinliche Ursache für die schwache Risk-Quality mit Mistral Small 3.2. M06 hat diesen 1-Zeilen-Bug mitbehoben und der Test `tests/test_prompt_manager_fix.py` sichert die Substitution dauerhaft ab.

---

## 7. KB-Retrieval einschalten (nur nach Validierung)

```bash
# .env.local: KB_RETRIEVAL_ENABLED=true setzen (Editor oder portabel:
#   sed -i.bak 's/^KB_RETRIEVAL_ENABLED=.*/KB_RETRIEVAL_ENABLED=true/' .env.local)
# Container neu erzeugen -- "docker compose restart" liest .env.local NICHT neu ein:
docker compose up -d --force-recreate api worker

# Vergleichslauf:
# 1. Drehbuch mit KB_RETRIEVAL_ENABLED=false durchschicken -> Findings A
# 2. KB_RETRIEVAL_ENABLED=true setzen, Container neu erzeugen, gleiches Drehbuch -> Findings B
# 3. Vergleichen: KB-Treffer erscheinen in den Empfehlungen
```

Worker-Logs zeigen bei aktivem Flag: `KB retrieval: 3 hits used for scene context`.

---

## 8. Mistral Cloud als zweite Inferenz-Quelle

Für den Stage-Sign-off (Pflichtenheft Abnahmetest 8) und für Qualitätsvergleiche steht Mistral Cloud
zur Verfügung – **nicht in Produktion** (Pflichtenheft §4.3; der Start-Validator lehnt
`mistral_cloud` bei `ENV=production` ab, solange `LLM_ALLOW_EXTERNAL_PROVIDERS` nicht `true` ist).
Vorher Datenschutz klären (AVV, [`DATENSCHUTZ.md`](DATENSCHUTZ.md) §5).

```bash
# In .env.local:
LLM_PROVIDER=mistral_cloud
MISTRAL_API_KEY=sk-...

docker compose up -d --force-recreate api worker
```

Der neue Adapter nutzt:

- `response_format={"type": "json_object"}` für garantiert parsebares JSON
- Post-Hoc Schema-Validierung mit `jsonschema` (Draft 2020-12)
- Genau einen Self-Correcting-Retry bei Schema-Verletzung (mit der vorigen Antwort + Fehlertext im Prompt)

Der Provider wird per `LLM_PROVIDER` umgeschaltet. Der Code-Default ist `mistral_cloud`;
`.env.example` und das Produktions-Overlay setzen `local_mistral`, daher immer explizit setzen.

**Mit `mistral_cloud` ist die Knowledge Base nicht nutzbar:** Der Provider hat kein `embed()`.
Ein Upload (`POST /v1/kb/documents`, auch `scripts/seed_kb.py`) schlägt mit HTTP 500 fehl, und
die Retrieval-Stufe der Risikoanalyse liefert stillschweigend keinen KB-Kontext (`(none)`). Eine
eigene Einstellung für einen separaten Embedding-Provider gibt es nicht (der Hinweis auf
`EMBEDDING_PROVIDER` im Docstring von `llm/base.py` ist gegenstandslos); Embeddings kommen immer
vom konfigurierten Ollama-basierten Provider (`ollama` oder `local_mistral`). Für AT8 mit Mistral
Cloud daher `KB_RETRIEVAL_ENABLED=false` lassen.

---

## 9. Sicherheit

| Maßnahme | Stelle |
|---|---|
| Originaltext Fernet-verschlüsselt (Chunk-Texte und Vektoren in `kb_embeddings` dagegen im Klartext, da pgvector nicht auf Chiffraten sucht) | `KnowledgeBaseService.ingest` |
| Tenant-Filter auf jeder Query | `kb.search`, `kb.list_documents`, `kb.delete_document` |
| API-Key-Authentifizierung – seit 1.0.1 nur **Admin-Keys** (`is_admin=true`), sonst 403 `Admin API key required for this endpoint`; der ePro-Service-Key hat keinen Zugriff | `require_admin_key` Dependency in allen `/v1/kb/*` Routen (wie `/v1/ops/*`) |
| Rate-Limiting (IP + Key) | `rate_limit_combined` Dependency |
| TTL pro Dokument (`ttl_hours`, Standard 720 h = 30 Tage, max. 8760 h) | `expires_at`-Spalte; automatische Löschung durch den Temporal-Schedule `eki-kb-cleanup` (seit M09, täglich `KB_CLEANUP_CRON` = `0 3 * * *` UTC; `KB_CLEANUP_ENABLED=false` pausiert ihn beim nächsten Worker-Start) |
| Quellen-Tagging | `tags` JSONB + `source`-Spalte (UPLOAD/SHARE/URL/PLACEHOLDER) |
| Dedup via SHA-256 pro Mandant (seit Migration `b8d0f2a4c6e3`; vorher global eindeutig) | Unique-Constraint `(tenant_id, content_hash)` — derselbe Text im selben Mandanten ergibt 409, auch bei gleichzeitigem Upload |
| Prompt-Sanitizer auf Embed-Input | `OllamaProvider.embed` |

---

## 10. Rollback

Falls die KB Probleme verursacht:

1. `KB_RETRIEVAL_ENABLED=false` in `.env.local`, dann `docker compose up -d --force-recreate api worker` → Risk-Flow ist für neue Jobs wieder bytewise wie M05
2. KB-Endpoints aus Router nehmen: Zeile `app.include_router(knowledge_base.router, ...)` in `api/main.py` auskommentieren (Code-Änderung, neues Image)
3. ~~Migration zurückrollen~~: **Heute nicht mehr anwenden.** Seit M06 sind weitere
   Migrationen hinzugekommen (`f1a2b3c4d5e6` (M10), `a7c9e1f3b5d2`, `b8d0f2a4c6e3`);
   `alembic downgrade d4b7e9f23a01` würde auch diese zurückrollen (Dead Letters,
   Zustell-Buchhaltung, Statusspalte, KB-Dedupe). Schritt 1 genügt.

Die Schritte 1 und 2 sind unabhängig voneinander wirksam.

---

## 11. Tests

```bash
# Alle M06-spezifischen Tests
pytest tests/test_kb_service.py tests/test_kb_endpoints.py \
       tests/test_mistral_cloud_structured.py tests/test_prompt_manager_fix.py \
       tests/test_risk_with_kb.py tests/test_seed_kb.py -v

# Erwartetes Ergebnis: 56 passed (Stand Release 1.0.1, SQLite-Modus)
```

Die KB-Endpoint-Tests mocken den `KnowledgeBaseService`, weil pgvector in der SQLite-In-Memory-Test-DB
nicht verfügbar ist. Gegen eine echte PostgreSQL+pgvector-Instanz laufen die KB-Tests in
`tests/test_pg_integration.py` (nur mit `TEST_POSTGRES_URL`, siehe [`TESTING_GUIDE.md`](TESTING_GUIDE.md)).
Ein Lauf gegen den kompletten Stack mit aktivem `KB_RETRIEVAL_ENABLED` ist mit
`tests/run_pdf_full_pipeline.py` möglich.

---

## 12. Bekannte Grenzen

- Embeddings nur über Ollama (1024 dim, Standard `mxbai-embed-large`, optional `bge-m3`). Mistral-Cloud-Embeddings sind bewusst nicht aktiv, weil §4.3 Pflichtenheft „ohne externe Cloud" für die Prod-Datenverarbeitung fordert; mit `LLM_PROVIDER=mistral_cloud` ist die KB daher nicht nutzbar (§8).
- Kein OCR für gescannte KB-Dokumente. PDFs müssen Text-PDFs sein.
- TTL-Cleanup: seit M09 automatisch per Temporal-Schedule `eki-kb-cleanup` (§9). Änderungen von `KB_CLEANUP_CRON`/`KB_CLEANUP_ENABLED` wirken seit 1.0.1 beim nächsten Worker-Start (der Schedule wird aktualisiert bzw. pausiert); der Cron wird beim Start geprüft (5 Felder, UTC, `CONFIGURATION.md` §3.13).
- Single-Tenant — `tenant_id` ist Spalte/Filter, aber `kb_default_tenant_id` ist fix in den Settings.
