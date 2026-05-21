# M06 — LLM-Adapter (Mistral Cloud) & Knowledge Base Guide

**Status:** Implementiert (additive Erweiterung von M05)
**Default-Verhalten:** Knowledge-Base-Retrieval ist standardmäßig AUS. Der Risk-Flow ist byte-identisch mit M05 bis das Feature-Flag aktiviert wird.

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

# Embedding-Modell in Ollama herunterladen
docker compose exec ollama ollama pull mxbai-embed-large
```

### 3.2 Migration ausführen

```bash
docker compose exec api alembic upgrade head
# Erwartete neue Revision: e8f1c2d3a401
```

Die Migration legt zwei Tabellen an (`kb_documents`, `kb_embeddings`) und einen `ivfflat`-Index. Bestehende Tabellen werden nicht verändert.

### 3.3 Settings prüfen

In `.env.local`:

```
OLLAMA_EMBEDDING_MODEL=mxbai-embed-large
KB_RETRIEVAL_ENABLED=false          # Default: AUS
KB_DEFAULT_TENANT_ID=00000000-0000-0000-0000-000000000001
KB_TOP_K=3
KB_MAX_CHUNK_CHARS_IN_PROMPT=600
```

---

## 4. Test-KB befüllen (Bernd-Stand-in)

```bash
# API-Key in env setzen
export EKI_API_KEY="eki_<dein_key>"

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
# .env.local: KB_RETRIEVAL_ENABLED=true setzen
docker compose restart api worker

# Vergleichslauf:
# 1. Drehbuch mit KB_RETRIEVAL_ENABLED=false durchschicken -> Findings A
# 2. KB_RETRIEVAL_ENABLED=true setzen, restart, gleiches Drehbuch -> Findings B
# 3. Vergleichen: KB-Treffer erscheinen in den Empfehlungen
```

Worker-Logs zeigen bei aktivem Flag: `KB retrieval: 3 hits used for scene context`.

---

## 8. Mistral Cloud als zweite Inferenz-Quelle

Für den Stage-Sign-off (Pflichtenheft Abnahmetest 8) und für Qualitätsvergleiche ist Mistral Cloud nun produktionsreif:

```bash
# In .env.local:
LLM_PROVIDER=mistral_cloud
MISTRAL_API_KEY=sk-...

docker compose restart api worker
```

Der neue Adapter nutzt:

- `response_format={"type": "json_object"}` für garantiert parsebares JSON
- Post-Hoc Schema-Validierung mit `jsonschema` (Draft 2020-12)
- Genau einen Self-Correcting-Retry bei Schema-Verletzung (mit der vorigen Antwort + Fehlertext im Prompt)

Ollama bleibt der Default-Provider, der Provider wird per `LLM_PROVIDER` umgeschaltet.

---

## 9. Sicherheit

| Maßnahme | Stelle |
|---|---|
| Originaltext Fernet-verschlüsselt | `KnowledgeBaseService.ingest` |
| Tenant-Filter auf jeder Query | `kb.search`, `kb.list_documents`, `kb.delete_document` |
| API-Key-Authentifizierung | `verify_api_key` Dependency in allen `/v1/kb/*` Routen |
| Rate-Limiting (IP + Key) | `rate_limit_combined` Dependency |
| TTL pro Dokument | `expires_at`-Spalte + `cleanup_expired()` Hook (Cron-Skript optional) |
| Quellen-Tagging | `tags` JSONB + `source`-Spalte (UPLOAD/SHARE/URL/PLACEHOLDER) |
| Dedup via SHA-256 | `content_hash` unique index — verhindert doppeltes Ingest |
| Prompt-Sanitizer auf Embed-Input | `OllamaProvider.embed` |

---

## 10. Rollback

Falls die KB Probleme verursacht:

1. `KB_RETRIEVAL_ENABLED=false` in `.env.local` → Risk-Flow ist sofort wieder bytewise wie M05
2. KB-Endpoints aus Router nehmen: Zeile `app.include_router(knowledge_base.router, ...)` in `api/main.py` auskommentieren
3. Migration zurückrollen: `docker compose exec api alembic downgrade d4b7e9f23a01`

Alle drei Schritte sind unabhängig voneinander wirksam.

---

## 11. Tests

```bash
# Alle M06-spezifischen Tests
pytest tests/test_kb_service.py tests/test_kb_endpoints.py \
       tests/test_mistral_cloud_structured.py tests/test_prompt_manager_fix.py \
       tests/test_risk_with_kb.py tests/test_seed_kb.py -v

# Erwartetes Ergebnis: 40 passed
```

Die KB-Endpoint-Tests mocken den `KnowledgeBaseService`, weil pgvector in der SQLite-In-Memory-Test-DB nicht verfügbar ist. Integrationstests gegen die echte Postgres+pgvector-Instanz laufen über `tests/run_pdf_full_pipeline.py` mit aktivem `KB_RETRIEVAL_ENABLED`.

---

## 12. Bekannte Grenzen

- Embeddings nur über Ollama (1024 dim, `mxbai-embed-large`). Mistral-Cloud-Embeddings sind bewusst nicht aktiv, weil §4.3 Pflichtenheft „ohne externe Cloud" für die Prod-Datenverarbeitung fordert.
- Kein OCR für gescannte KB-Dokumente. PDFs müssen Text-PDFs sein.
- Kein automatischer TTL-Cleanup-Cron — `services.knowledge_base.cleanup_expired()` ist als Hook vorhanden, ein Trigger-Skript folgt bei Bedarf (M08).
- Single-Tenant — `tenant_id` ist Spalte/Filter, aber `kb_default_tenant_id` ist fix in den Settings.
