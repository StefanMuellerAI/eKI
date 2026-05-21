# M06 Production Deploy Cheat Sheet

Kurzanleitung zum Hochziehen von M06 auf der Produktivlandschaft der
Filmakademie. Geht von einem laufenden M05-Setup aus.

---

## Vorbereitung (1 Minute)

```bash
# Auf dem Server
cd /pfad/zur/eki_api
git fetch origin
git checkout main
git pull origin main
# Erwarteter neuester Commit: 4bdfe59 "M06: LLM-Adapter (Mistral Cloud) & Knowledge Base mit pgvector"
```

---

## Schritt 1: .env.local erweitern

Folgende Zeilen in `.env.local` ergänzen (alle haben sichere Defaults im
Code, ein Auslassen ist OK — explizit hier eintragen schadet aber nicht):

```bash
# M06 KB Embeddings
OLLAMA_EMBEDDING_MODEL=bge-m3
OLLAMA_EMBEDDING_MAX_CHARS=30000

# Knowledge Base Feature-Flag (Default OFF -> M05-identisch)
KB_RETRIEVAL_ENABLED=false
KB_DEFAULT_TENANT_ID=00000000-0000-0000-0000-000000000001
KB_TOP_K=3
KB_MAX_CHUNK_CHARS_IN_PROMPT=600
```

**Hinweis Modellwahl:** `bge-m3` (1024 dim, 8192 Token Context) ist die
produktionsreife Wahl. `mxbai-embed-large` ist Fallback bei VRAM-Engpässen
(dann `OLLAMA_EMBEDDING_MAX_CHARS=1000` setzen).

---

## Schritt 2: Container neu bauen und starten

```bash
docker compose down
docker compose build api worker
docker compose up -d
```

Healthcheck:

```bash
curl -s http://localhost:8000/health
# {"status":"healthy","timestamp":"...","version":"0.6.0"}
```

---

## Schritt 3: Datenbankmigration

```bash
docker compose exec api alembic upgrade head
docker compose exec api alembic current
# Erwartete Revision: e8f1c2d3a401 (head)
```

Was passiert: neue Tabellen `kb_documents`, `kb_embeddings` und die
pgvector-Extension werden angelegt. Keine bestehenden Tabellen werden
verändert. Idempotent — mehrfaches Ausführen schadet nicht.

---

## Schritt 4: Embedding-Modell in Ollama pullen

Einmalig auf der Ollama-Instanz:

```bash
# wenn Ollama im Container laeuft:
docker compose exec ollama ollama pull bge-m3

# wenn Ollama auf dem Host laeuft:
ollama pull bge-m3
```

Verifikation:

```bash
curl -s http://localhost:11434/api/tags | grep bge-m3
```

`bge-m3` ist ca. 1.2 GB groß, einmaliger Download.

---

## Schritt 5: API-Key bereitstellen

Wenn die Filmakademie noch keinen produktiven Admin-Key für die KB-Endpoints
hat, einen erstellen:

```bash
docker compose exec api python scripts/create_api_key.py --insert
# Interaktiv: User-ID, Key-Name, Tage gültig
# WICHTIG: den ausgegebenen Key 1x sicher abspeichern, er ist nicht
# rekonstruierbar (SHA-256-Hash in DB)
```

---

## Schritt 6: KB befüllen

### Option A: Test-KB mit Placeholdern

Damit das System sofort einen produktiven RAG-Lauf machen kann:

```bash
export EKI_API_URL=http://localhost:8000
export EKI_API_KEY=eki_...   # aus Schritt 5
python scripts/seed_kb.py --seed-placeholders
python scripts/seed_kb.py --status
# -> 6 placeholder docs
```

### Option B: Echte Sicherheits-Dokumente

Sobald der Sicherheitsbeauftragte echte Inhalte liefert:

```bash
# 1. Dokumente in config/kb_seed/real/ ablegen (PDF/MD/TXT, max 10 MB pro Datei)
cp /pfad/zu/Bernds-Lieferung/*.pdf config/kb_seed/real/

# 2. Placeholder rauswerfen, echte rein
python scripts/seed_kb.py --wipe-placeholders
python scripts/seed_kb.py --reseed
python scripts/seed_kb.py --status
```

Echte Dokumente überleben jeden `--wipe-placeholders`-Lauf — der wipe
zielt ausschließlich auf Dokumente mit Tag `placeholder`.

---

## Schritt 7: KB scharf schalten

**Erst nach Validierung** der KB-Inhalte:

```bash
# .env.local bearbeiten:
KB_RETRIEVAL_ENABLED=true

# Beide Worker und API neu erstellen, damit die neue Env greift
docker compose up -d --force-recreate api worker
```

Schneller Test mit einem Drehbuch:

```bash
python tests/run_security_check.py /pfad/zu/skript.pdf \
  --key "$EKI_API_KEY" \
  --url http://localhost:8000 \
  --output report.pdf \
  --json report.json
```

Beleg, dass KB greift:

```bash
docker compose logs worker | grep "KB retrieval"
# Erwartete Zeilen pro Szene:
# KB retrieval: 3 hits used for scene context
```

---

## Schritt 8: Rollback (falls etwas zickt)

Drei voneinander unabhängige Pfade, jeweils ohne Datenverlust:

1. **Flag aus** (Risk-Pfad bytewise wie M05):
   ```bash
   sed -i 's/KB_RETRIEVAL_ENABLED=true/KB_RETRIEVAL_ENABLED=false/' .env.local
   docker compose up -d --force-recreate api worker
   ```

2. **KB-Endpoints sperren** (z.B. wenn Upload-Mechanik Probleme macht):
   in `api/main.py` die Zeile `app.include_router(knowledge_base.router, ...)`
   auskommentieren und neu deployen. `/v1/security/*` bleibt unangetastet.

3. **Migration zurückrollen** (extrem selten, nur bei Schema-Konflikten):
   ```bash
   docker compose exec api alembic downgrade d4b7e9f23a01
   ```
   Die KB-Tabellen werden gedroppt, M05-Tabellen bleiben unverändert.

---

## Schritt 9: Postman-Collection an die Filmakademie

Falls das ePro-Team an den KB-Endpoints testen möchte, die neue Collection
mitschicken:

```
postman/eKI-API-v0.6.postman_collection.json
```

Sektion „8. Knowledge Base (M06)" enthält Upload, List, Get, Delete und
Wipe-by-Tag. Variablen `{{base_url}}`, `{{api_key}}`, `{{doc_id}}` setzen.

---

## Anhang: Quick-Verification-Skript

Ein-Zeiler, der den gesamten M06-Stack durchprüft:

```bash
docker compose exec api alembic current && \
docker compose exec api python -c "from api.config import get_settings; s=get_settings(); print(f'KB_RETRIEVAL_ENABLED={s.kb_retrieval_enabled}, embed={s.ollama_embedding_model}, max_chars={s.ollama_embedding_max_chars}')" && \
curl -s http://localhost:8000/health && \
curl -sH "Authorization: Bearer $EKI_API_KEY" http://localhost:8000/v1/kb/documents | python3 -m json.tool | head -20
```

Erwartet:
- `e8f1c2d3a401 (head)`
- `KB_RETRIEVAL_ENABLED=True, embed=bge-m3, max_chars=30000`
- `{"status":"healthy",...,"version":"0.6.0"}`
- KB-Dokument-Liste

---

## Bekannte Details

- **Performance:** Auf der Test-HW (M-Mac, gemma4:e4b) braucht ein 20-Szenen-
  Drehbuch ca. 25 Minuten. Auf Produktiv-HW mit GPU ≥ 24 GB und gemma4:31b
  sollte das deutlich unter Pflichtenheft-Limit (≤ 10 Min für ≤ 50 Szenen)
  liegen — bitte vor Übergabe kurz prüfen.
- **Chunking:** Pflichtenheft-konform 800–1500 Tokens je Chunk. Mit bge-m3
  passt das ohne Truncation, mit mxbai-embed-large nur durch
  `OLLAMA_EMBEDDING_MAX_CHARS=1000`-Cap (Retrieval-Qualität sinkt).
- **TTL:** KB-Dokumente haben `expires_at`. Aktuell kein Auto-Cron eingerichtet.
  Bei Bedarf manuell:
  ```bash
  docker compose exec api python -c "import asyncio; from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine; from api.config import get_settings; from llm.factory import get_llm_provider; from services.knowledge_base import KnowledgeBaseService; s=get_settings(); e=create_async_engine(str(s.database_url)); S=async_sessionmaker(e, expire_on_commit=False); 
  async def run():
      async with S() as session:
          kb=KnowledgeBaseService(db=session, llm=get_llm_provider(s), secret_key=s.api_secret_key)
          n=await kb.cleanup_expired()
          print(f'removed={n}')
  asyncio.run(run())"
  ```

---

**Stand:** 2026-05-21, Commit `4bdfe59`.
