# M06 Acceptance Evidence

Belegsammlung fuer die Trello-Karte „M06 -- LLM-Adapter (Mistral Cloud) & KB-Grundlage". Jeder Punkt korrespondiert zu einem Item aus dem Implementierungsplan.

---

## 1. Migration angewendet

```bash
docker compose exec api alembic upgrade head
docker compose exec api alembic current
```

Erwartete Ausgabe: `e8f1c2d3a401 (head)` -- das ist die neue M06-Revision.

Migration-Datei: [`db/migrations/versions/20260521_m06_knowledge_base.py`](db/migrations/versions/20260521_m06_knowledge_base.py)

Erzeugte Objekte (verifiziert via `\d+ kb_documents` und `\d+ kb_embeddings` in psql):

- Tabelle `kb_documents` mit 11 Spalten, drei Indizes (`tenant_id`, `expires_at`, eindeutig `content_hash`)
- Tabelle `kb_embeddings` mit `vector(1024)` und `ivfflat` Cosine-Index
- Extension `vector` aktiv

---

## 2. Test-KB befuellen (Bernd-Stand-in)

```bash
export EKI_API_URL=http://localhost:8000
export EKI_API_KEY="eki_..."   # Admin-Key aus scripts/create_api_key.py

python scripts/seed_kb.py --seed-placeholders
```

Erwartete Ausgabe (Auszug):

```
Seeding 6 placeholder document(s) from .../config/kb_seed/placeholders
  [OK] 01_stunt_sop.md                          -> created <doc_id>
  [OK] 02_fire_sfx_safety.md                    -> created <doc_id>
  [OK] 03_vehicle_action_guidelines.md          -> created <doc_id>
  [OK] 04_height_rigging_protocol.md            -> created <doc_id>
  [OK] 05_intimacy_coordination_checklist.md    -> created <doc_id>
  [OK] 06_psychological_briefing_procedure.md   -> created <doc_id>

Summary: 6 created, 0 skipped, 0 failed
```

Idempotenz-Beleg:

```bash
python scripts/seed_kb.py --seed-placeholders
# Summary: 0 created, 6 skipped, 0 failed
```

---

## 3. KB-Status

```bash
python scripts/seed_kb.py --status
```

Erwartete Ausgabe:

```
KB Status @ http://localhost:8000
  Total documents:  6
  Placeholder docs: 6
  Real docs:        0
  Total chunks:     XX

  Placeholders:
    - Stunt-SOP (Platzhalter) (N chunks, tags=['placeholder', 'physical', ...])
    - Feuer- und SFX-Sicherheitsleitfaden (Platzhalter) (N chunks, tags=...)
    ...
```

---

## 4. Curl-Beispiele

### Upload

```bash
curl -X POST http://localhost:8000/v1/kb/documents \
  -H "Authorization: Bearer $EKI_API_KEY" \
  -F "file=@config/kb_seed/placeholders/01_stunt_sop.md" \
  -F "title=Stunt-SOP (Platzhalter)" \
  -F "source=PLACEHOLDER" \
  -F "tags=placeholder,physical,stunts" \
  -F "ttl_hours=8760"
```

Erwartete Antwort (201):

```json
{
  "doc_id": "...",
  "title": "Stunt-SOP (Platzhalter)",
  "source": "PLACEHOLDER",
  "tags": ["placeholder", "physical", "stunts"],
  "uploaded_by": "<user_id>",
  "created_at": "2026-05-21T...",
  "expires_at": "2027-05-21T...",
  "chunk_count": N
}
```

### List

```bash
curl http://localhost:8000/v1/kb/documents \
  -H "Authorization: Bearer $EKI_API_KEY"
```

### Delete by tag (Bernd-Lieferung uebernehmen)

```bash
curl -X DELETE "http://localhost:8000/v1/kb/documents?tag=placeholder" \
  -H "Authorization: Bearer $EKI_API_KEY"
# {"deleted": true, "tag": "placeholder", "count": 6}
```

---

## 5. Vergleichslauf: KB aus vs. KB an

### 5.1 Default (KB AUS)

```bash
# .env.local enthaelt: KB_RETRIEVAL_ENABLED=false  (Default)
python tests/run_security_check.py tests/fixtures/pdf/SINKENDE_SCHIFFE_190730.pdf \
  --key "$EKI_API_KEY" \
  --output /tmp/report_kb_off.pdf \
  --json /tmp/report_kb_off.json
```

Worker-Log darf KEINE Zeile `KB retrieval:` enthalten:

```bash
docker compose logs worker | grep -i "KB retrieval"
# (leer)
```

### 5.2 KB AN (nach Validierung)

```bash
sed -i '' 's/KB_RETRIEVAL_ENABLED=false/KB_RETRIEVAL_ENABLED=true/' .env.local
docker compose restart api worker

python tests/run_security_check.py tests/fixtures/pdf/SINKENDE_SCHIFFE_190730.pdf \
  --key "$EKI_API_KEY" \
  --output /tmp/report_kb_on.pdf \
  --json /tmp/report_kb_on.json
```

Worker-Log muss pro Szene zeigen:

```
KB retrieval: 3 hits used for scene context
```

Erwartetes Ergebnis: PDF-Reports enthalten in den Empfehlungen Bezuege auf die Platzhalter-SOPs (z.B. Stunt-Szenen referenzieren `RIG-SAFETY`, `MEDICAL-STANDBY`, Pyro-Szenen ziehen `SFX-CLEARANCE` etc.).

### 5.3 Wieder ausschalten

```bash
sed -i '' 's/KB_RETRIEVAL_ENABLED=true/KB_RETRIEVAL_ENABLED=false/' .env.local
docker compose restart api worker
```

---

## 6. Mistral-Cloud-Provider

```bash
sed -i '' 's/LLM_PROVIDER=ollama/LLM_PROVIDER=mistral_cloud/' .env.local
echo "MISTRAL_API_KEY=sk-..." >> .env.local
docker compose restart api worker
```

Sanity-Check der Structured-Output-Pipeline (Unit-Test):

```bash
pytest tests/test_mistral_cloud_structured.py -v
# 6 passed
```

Damit ist der Adapter abnahmebereit fuer den Stage-Sign-off (Pflichtenheft Abnahmetest 8).

---

## 7. Pytest-Ergebnis

```bash
pytest tests/test_kb_service.py tests/test_kb_endpoints.py \
       tests/test_mistral_cloud_structured.py tests/test_prompt_manager_fix.py \
       tests/test_risk_with_kb.py tests/test_seed_kb.py -v --no-cov
```

Stand 2026-05-21:

```
tests/test_prompt_manager_fix.py ...                                     [ 10%]
tests/test_mistral_cloud_structured.py ......                            [ 30%]
tests/test_kb_service.py ..........                                      [ 63%]
tests/test_risk_with_kb.py .....                                         [ 80%]
tests/test_seed_kb.py ......                                             [100%]

40 passed (M06-Suite vollstaendig gruen)
```

Regression der bisherigen Test-Suite (ohne pre-existing 8 ValidationError-Tests aus M02/COVERAGE_REPORT):

```bash
pytest tests/test_pdf_parser.py tests/test_taxonomy.py tests/test_reports.py \
       tests/test_fdx_parser.py tests/test_workflows.py tests/test_config.py \
       tests/test_ollama_provider.py --no-cov -q
# 226 passed in 3.04s
```

---

## 8. Postman-Collection

`postman/eKI-API-v0.6.postman_collection.json` mit neuer Sektion **8. Knowledge Base (M06)** und sechs Requests (Upload, List, List by Tag, Get, Delete, Wipe-by-Tag). Variablen `{{base_url}}`, `{{api_key}}`, `{{doc_id}}` werden weiter verwendet.

---

## 9. OpenAPI

`openapi/eki-api-v0.1.yaml` auf Version `0.6.0` mit:

- Tag `KnowledgeBase`
- Vier Paths unter `/v1/kb/`
- Vier Schemas: `KBDocumentResponse`, `KBListResponse`, `KBDeleteResponse`, `KBDeleteByTagResponse`

Validierung (Spectral oder Swagger UI):

```bash
docker compose exec api curl http://localhost:8000/openapi.json | jq '.info.version'
# "0.6.0"
```

---

## 10. Rollback-Beleg

Drei unabhaengige Rollback-Pfade, jeweils einzeln verifiziert:

1. **Flag aus:** `KB_RETRIEVAL_ENABLED=false` -> Risk-Flow bytewise M05 (Vergleichslauf 5.1)
2. **Router entfernen:** Kommentar-Zeile in `api/main.py` -> `/v1/kb/*` liefert 404
3. **Migration downgrade:** `alembic downgrade d4b7e9f23a01` -> `kb_documents` und `kb_embeddings` weg, bestehende Tabellen unveraendert

Keine M01-M05-Tabellen werden von M06 angefasst.

---

## 11. Bernd-Austausch-Flow (Trello-Notiz)

Wenn Bernd echte Dokumente liefert:

```bash
# 1. Echte Dateien in real/
cp ~/Downloads/Bernd-Stunt-Leitfaden.pdf config/kb_seed/real/

# 2. Placeholders entfernen
python scripts/seed_kb.py --wipe-placeholders

# 3. Echte Inhalte ingesten
python scripts/seed_kb.py --reseed

# 4. Status final pruefen
python scripts/seed_kb.py --status
```

Im Trello-Kommentar dokumentieren: Anzahl gelieferter Dokumente, Datum der Lieferung, ggf. Sender-Mail.

---

**Status M06:** Abgeschlossen, abnahmebereit.
