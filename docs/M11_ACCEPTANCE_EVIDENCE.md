# M11 Acceptance Evidence – Lokaler LLM-Adapter & Paritätstests

Belegsammlung für die Trello-Karte „M11 – Lokaler LLM-Adapter & Paritätstests".
Pflichtenheft v1, §3.2 Lieferumfang (LLM-Adapter lokales Mistral, Container-Setup,
Betriebsdoku), §4.1 Provider-Abstraktion (`LLM_PROVIDER=mistral_cloud|local_mistral`),
§4.3 Lokale KI-Integration, §7 Abnahmetest 9 (Prod-Cutover ohne externe Calls),
§8 Risiko Provider-Wechsel, §9 M11-Artefakte (LocalMistralAdapter, Paritäts-Reports,
Container-Images, Betriebsleitfaden), §10 PT-Tabelle (4 PT).

Auftraggeber-Entscheidung (dieses Sprints): Produktiv-Default `mistral-small3.2`,
`gemma4` als dokumentierte Alternative, Paritätstests für beide.

---

## 1. Pflichtenheft-Mapping

| Pflichtenheft-Stelle | Forderung | M11-Beleg |
|---|---|---|
| §4.1 | `LLM_PROVIDER=mistral_cloud\|local_mistral` umschaltbar | `llm/factory.py`, Settings `LOCAL_MISTRAL_MODEL`, `LOCAL_MISTRAL_BASE_URL` |
| §4.3 | Ohne externe Cloud, nur lokales Mistral Small/Medium via Ollama | `llm/local_mistral.py` (Default `mistral-small3.2`), Guard `LLM_ALLOW_EXTERNAL_PROVIDERS=false` verweigert `mistral_cloud` in Produktion (Settings-Validator **und** Factory) |
| §7 Test 9 | Paritäts-/Smoke-Tests mit lokalem Mistral ohne externe Calls | `scripts/smoke_prod.py` prüft E2E-Job, One-Shot, Delete-on-Delivery und `eki_llm_requests_total` ohne `provider="mistral_cloud"` |
| §8 | Paritäts-Tests als Mitigation für Cloud→Lokal | `services/parity.py`, `scripts/run_parity.py`, `tests/parity/golden_scenes.yaml`, `docs/M11_PARITY_REPORT.md` |
| §9 M11 | Container-Images | CI-Job `release-images`: `ghcr.io/<owner>/eki-api|eki-worker:<version>` bei Tag `v*`, Tag-Version wird gegen `core/version.py` geprüft |
| §9 M11 | Betriebsleitfaden | `docs/OPERATIONS_GUIDE.md` (Installation, Konfigurationsmatrix, Skalierung, Key-Rotation, Backups, Updates, Runbooks, Cutover) |
| §4.1 | Server: Uvicorn + Gunicorn | `docker-compose.prod.yml` startet die API mit Gunicorn/UvicornWorker |
| §10 PT | 4 PT | Eingehalten |

---

## 2. Adapter (`llm/local_mistral.py`)

- Erbt Transport, Semaphore/Throttle (M07), Metriken (M09) und Structured Output vom
  `OllamaProvider`; `provider_name="local_mistral"` in allen Fehlerdetails und Metrik-Labels.
- Modell ausschließlich über `LOCAL_MISTRAL_MODEL` (Default `mistral-small3.2`), kein stiller
  Fallback auf `"mistral"` mehr; Embedding-Konfiguration kommt jetzt aus den Settings
  (vor M11 hartkodiert ignoriert).
- `health_check()` = Endpoint erreichbar **und** Modell gepullt; `ensure_model_available()`
  liefert `LLMException` mit `ollama pull <model>`-Hinweis.
- `describe()` liefert inhaltsfreie Metadaten für Paritätsreport und Build-Info.

Paritätsangleichung der Provider:

| Punkt | Vorher | Nachher |
|---|---|---|
| `generate_structured` Temperature-Default | Ollama 0.7 / Mistral 0.2 | beide 0.2 (auch Basisklasse) |
| `generate_chat` | ohne Sanitizer/System-Lock | User-Messages fail-closed sanitisiert, System-Message gelockt |
| `embed` | außerhalb des Ollama-Caps | unter `_ollama_slot()` (Cap + Throttle) |

---

## 3. Paritäts-Harness

- Golden-Set: 16 synthetische Szenen (23-Klassen-Taxonomie abgedeckt: Feuer, Höhe, Fahrzeuge,
  Waffen, Wasser, Tiere, Wetter/Ermüdung, Menge, Elektrik, Enge/Staub, Gewalt/Trauma, Intimität,
  Tod/Trauer, Diskriminierung) + 2 Negativkontrollen.
- Kennzahlen und Schwellen: siehe `docs/M11_PARITY_REPORT.md` §1.
- Ausgabe JSON + Markdown nach `tests/reports/` (git-ignoriert); Exit-Code = Verdikt.
- **Der reale Lauf benötigt die Zielhardware bzw. Stage-Cloud-Zugang** und ist im Report als
  ausstehend markiert; die Harness-Mathematik ist offline getestet.

---

## 4. Container & Betrieb

- `docker-compose.prod.yml`: GHCR-Images (`GHCR_OWNER`, `EKI_IMAGE_TAG`), Gunicorn+Uvicorn,
  keine Host-Ports für Infra, keine Bind-Mounts, Redis `appendonly` + `noeviction`, NVIDIA-GPU-Block
  für Ollama, Ressourcenlimits, `LLM_PROVIDER=local_mistral` Default.
- CI `release-images`: Build + Push nach GHCR bei `v*`-Tags, OCI-Labels (version, revision, source).
- `docs/OPERATIONS_GUIDE.md`: vollständiger Betriebsleitfaden inkl. Cutover-Checkliste (§6) und
  Nachweistabelle (§7).

---

## 5. Tests

```bash
.venv/bin/python -m pytest tests/test_m11_local_mistral.py tests/test_m11_parity_harness.py --no-cov -v
```

| Datei | Tests | Schwerpunkt |
|---|---|---|
| `tests/test_m11_local_mistral.py` | 23 | Factory-Verdrahtung (Modell, Base-URL, Embeddings), Prod-Guard (Factory + Settings-Validator, Override-Flag), Tag-Matching, `ensure_model_available` mit Pull-Hinweis + Cache, `health_check` mit Modellprüfung, Temperature-Parität, gemeinsamer Vertrag, `generate_chat`-Sanitizing/Lock, `embed` unter Cap |
| `tests/test_m11_parity_harness.py` | 8 | Golden-Set referenziert nur gültige Klassen, perfekter Provider besteht, Recall/False-Positive/Severity-Logik, Fehler werden erfasst statt geworfen, Markdown/JSON-Report |

---

## 6. Manuelle Nachweise auf Zielhardware (ausstehend, Runbook)

```bash
docker compose exec ollama ollama pull mistral-small3.2
docker compose exec api python -c "import asyncio;from api.config import get_settings;from llm.factory import get_llm_provider;print(asyncio.run(get_llm_provider(get_settings()).health_check()))"
python scripts/run_parity.py --providers local_mistral,ollama:gemma4:31b    # -> docs/M11_PARITY_REPORT.md §3
python scripts/smoke_prod.py                                               # -> RESULT: PASSED
```

---

**Status M11:** Implementierung abgeschlossen; Paritäts- und Smoke-Lauf auf der
Zielhardware sind als Abnahmeschritt vorbereitet (Kommandos oben, Protokollvorlage in M12).
