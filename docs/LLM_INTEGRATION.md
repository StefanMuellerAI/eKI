# LLM Integration Guide

Stand: Oktober 2026 (Release 1.0.0). Bei Abweichungen gilt der Code in `llm/`; alle
Einstellungen mit Default und Bedeutung stehen in [`CONFIGURATION.md`](CONFIGURATION.md).

## Architektur

Die eKI kapselt jedes Sprachmodell hinter der abstrakten Klasse `BaseLLMProvider`
(`llm/base.py`). Welche Implementierung verwendet wird, entscheidet `get_llm_provider(settings)`
(`llm/factory.py`) anhand von `LLM_PROVIDER`. Die Factory wird bei jedem Aufruf neu
ausgewertet; es gibt keinen Provider-Singleton.

```
 Activities (workflows/activities.py)          KB (services/knowledge_base.py)
 structure_scene_llm  analyze_scene_risk        ingest / search -> embed()
          \                 |                         /
           +----------- get_llm_provider() ----------+        Paritäts-Harness
                            |                                  (services/parity.py)
        +-------------------+--------------------+
        |                   |                    |
  MistralCloudProvider  LocalMistralProvider  OllamaProvider
  (mistral_cloud)       (local_mistral)       (ollama)
  api.mistral.ai        Ollama, Modell         Ollama, beliebiges Modell
                        mistral-small3.2
```

Aufrufer im Produktivpfad:

- `structure_scene_llm` (PDF-Pfad): `generate_structured` mit `SCENE_SCHEMA`
  (`parsers/pdf_llm_structurer.py`), dazu Titel-Extraktion aus dem Vorspann.
- `analyze_scene_risk`: `generate_structured` mit `_RISK_SCHEMA` (`workflows/activities.py`),
  Prompts aus `config/prompts/prompts.yaml`, Ergebnis wird von `TaxonomyManager.validate_finding`
  geprüft und angereichert.
- Knowledge Base: `embed()` beim Upload und, wenn `KB_RETRIEVAL_ENABLED=true`, für die Suche pro Szene.

Die Risikoanalyse arbeitet **fail-closed**: Ein LLM-Fehler, der nach den Retries bestehen bleibt,
eine unbrauchbare Antwort oder eine Blockade durch den Prompt-Injection-Filter beendet den Job mit
`failed` und inhaltsfreier Fehlermeldung, statt die Szene ohne Findings zu bewerten
(`EPRO_INTEGRATION_GUIDE.md` §3). Die Findings einer Szene speichert `analyze_scene_risk`
verschlüsselt im SecureBuffer; durch Temporal läuft nur der Referenzschlüssel.

## Provider-Implementierungen

| `LLM_PROVIDER` | Klasse | Inferenz | Embeddings | Zulässig |
|---|---|---|---|---|
| `local_mistral` | `LocalMistralProvider` (`llm/local_mistral.py`) | lokal über Ollama, Modell `LOCAL_MISTRAL_MODEL` (Default `mistral-small3.2`) | ja (Ollama) | **Produktion** (Standard im Prod-Overlay) |
| `ollama` | `OllamaProvider` (`llm/ollama.py`) | lokal über Ollama, Modell `OLLAMA_MODEL` | ja (Ollama) | Produktion möglich (z. B. `gemma4`, Paritätsreport beachten) |
| `mistral_cloud` | `MistralCloudProvider` (`llm/mistral_cloud.py`) | Cloud-API `api.mistral.ai` | **nein** | nur Stage/Test; in Produktion vom Start-Validator abgelehnt |

### Mistral Cloud (`mistral_cloud`)

- `generate_structured` nutzt `response_format={"type": "json_object"}`, validiert die Antwort mit
  `jsonschema` (Draft 2020-12) gegen das übergebene Schema und versucht bei einer
  Schemaverletzung genau einmal eine Selbstkorrektur.
- Ohne `MISTRAL_API_KEY` scheitert schon der Konstruktor.
- Kein `embed()`: Mit diesem Provider ist die Knowledge Base nicht nutzbar (Upload schlägt fehl,
  Retrieval liefert keinen Kontext). Eine separate Einstellung für einen Embedding-Provider gibt es
  nicht – der Hinweis auf `EMBEDDING_PROVIDER` im Docstring von `BaseLLMProvider.embed` ist
  gegenstandslos.
- Daten verlassen das Netz der Filmakademie: nur mit AVV/DPA einsetzen ([`DATENSCHUTZ.md`](DATENSCHUTZ.md) §5).

### Ollama (`ollama`) und lokales Mistral (`local_mistral`)

`LocalMistralProvider` erbt von `OllamaProvider` und unterscheidet sich nur in:

- festem Default-Modell `mistral-small3.2` (kein stiller Rückfall auf ein anderes Modell),
- eigenem Endpoint `LOCAL_MISTRAL_BASE_URL` (Fallback `OLLAMA_BASE_URL`),
- `health_check()`, der zusätzlich prüft, ob das Modell gezogen ist, und `ensure_model_available()`
  mit `ollama pull`-Hinweis,
- `describe()` für Paritätsreport und Build-Info.

Gemeinsames Verhalten:

- `generate_structured` übergibt das JSON-Schema als `format` an `/api/chat`; Ollama erzwingt damit
  syntaktisch gültiges JSON. `OLLAMA_THINK=false` schaltet den Denkmodus von Modellen wie
  `gemma4`/`qwen3` ab; `<think>`-Blöcke werden zusätzlich entfernt.
- Prozessweiter Concurrency-Cap: Alle Ollama-Aufrufe eines Prozesses (Worker oder API) teilen sich
  einen Semaphor der Größe `OLLAMA_MAX_CONCURRENT_REQUESTS` (Default 1), optional mit Mindestabstand
  `OLLAMA_MIN_INTERVAL_MS`. Die Wartezeit misst `eki_llm_queue_wait_seconds`.
- `embed()` ruft `/api/embeddings` mit `OLLAMA_EMBEDDING_MODEL` auf und kürzt die Eingabe auf
  `OLLAMA_EMBEDDING_MAX_CHARS`. Das Modell muss 1024-dimensionale Vektoren liefern (Spalte
  `kb_embeddings.vector`): Standard `mxbai-embed-large` (mit 1000 Zeichen), alternativ `bge-m3`
  (mit ca. 30 000 Zeichen). Ein Modellwechsel erfordert, die KB zu leeren und neu einzuspielen.
- Zusätzlich vorhanden: `generate_chat`, `list_models`, `pull_model`.

## Schnittstelle `BaseLLMProvider`

```python
class BaseLLMProvider(ABC):
    def __init__(self, config: dict[str, Any]) -> None: ...

    @abstractmethod
    async def generate(self, prompt: str, system_prompt: str | None = None,
                       temperature: float = 0.7, max_tokens: int = 2000, **kwargs) -> str: ...

    @abstractmethod
    async def generate_structured(self, prompt: str, schema: dict[str, Any],
                                  system_prompt: str | None = None,
                                  temperature: float = 0.2, **kwargs) -> dict[str, Any]: ...

    @abstractmethod
    async def health_check(self) -> bool: ...

    async def embed(self, text: str) -> list[float]:      # optional, Default: NotImplementedError
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...                    # Label in Metriken, Logs, Fehlern
```

Fehler werden als `core.exceptions.LLMException` gemeldet (Nachricht und `details` ohne
Prompt- oder Antwortinhalte, siehe Log-Hygiene). `llm/base.py` enthält dafür Hilfsfunktionen zum
Auswerten der Antwort-Hülle (z. B. `decode_json_body`, `require_field`), die eine unbrauchbare
2xx-Antwort in eine inhaltsfreie `LLMException` übersetzen.

## Verwendung im Code

```python
from api.config import get_settings
from llm.factory import get_llm_provider

provider = get_llm_provider(get_settings())
text = await provider.generate("Say hello", temperature=0.1, max_tokens=50)
data = await provider.generate_structured(prompt, schema=my_schema, system_prompt=system)
ok = await provider.health_check()
```

`get_settings()` ist prozessweit gecacht. Für Abweichungen (Tests, Paritätslauf) eine Kopie
anlegen statt das gecachte Objekt zu verändern – so macht es auch `scripts/run_parity.py`:

```python
settings = get_settings().model_copy(update={"llm_provider": "ollama", "ollama_model": "gemma4:31b"})
provider = get_llm_provider(settings)
```

## Konfiguration

| Variable | Code-Default | Bedeutung |
|---|---|---|
| `LLM_PROVIDER` | `mistral_cloud` | `local_mistral`, `ollama` oder `mistral_cloud` (Unterstrich; Bindestrich-Varianten werden beim Start abgelehnt). `.env.example` und das Prod-Overlay setzen `local_mistral`; das Prod-Overlay überschreibt den Wert aus `.env.local` mit `${LLM_PROVIDER:-local_mistral}` aus der Shell |
| `LLM_ALLOW_EXTERNAL_PROVIDERS` | `false` | erlaubt `mistral_cloud` bei `ENV=production` |
| `LOCAL_MISTRAL_MODEL` | `mistral-small3.2` | Modell für `local_mistral` |
| `LOCAL_MISTRAL_BASE_URL` | – (→ `OLLAMA_BASE_URL`) | Ollama-Endpoint für `local_mistral` |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama-Endpoint |
| `OLLAMA_MODEL` | `mistral` | Modell für `ollama` (`.env.example`: `mistral-small3.2`) |
| `OLLAMA_TIMEOUT` | `300` | Timeout pro Request in Sekunden (gilt auch für `local_mistral`) |
| `OLLAMA_THINK` | `false` | Denkmodus für Thinking-Modelle |
| `OLLAMA_NUM_CTX` | `32768` | Kontextfenster |
| `OLLAMA_EMBEDDING_MODEL` | `mxbai-embed-large` | Embedding-Modell (1024 Dimensionen) |
| `OLLAMA_EMBEDDING_MAX_CHARS` | `1000` | Kürzung der Embedding-Eingabe (`bge-m3`: ca. 30000) |
| `OLLAMA_MAX_CONCURRENT_REQUESTS` | `1` | prozessweiter Cap (1–8) |
| `OLLAMA_MIN_INTERVAL_MS` | `0` | Mindestabstand zwischen Aufrufen |
| `MISTRAL_API_KEY` / `MISTRAL_API_KEY_FILE` | leer | API-Key Mistral Cloud |
| `MISTRAL_MODEL` | `mistral-large-latest` | Modell Mistral Cloud |
| `MISTRAL_TIMEOUT` | `120` | Timeout in Sekunden |
| `LLM_ACTIVITY_TIMEOUT_SECONDS` | `600` | `start_to_close_timeout` der LLM-Activities |
| `LLM_PARALLEL_ENABLED`, `PDF_STRUCTURE_CONCURRENCY`, `RISK_ANALYSIS_CONCURRENCY` | `false`, 1, 1 | parallele LLM-Activities pro Workflow (M07) |

Änderungen an `.env.local` erst nach `docker compose up -d --force-recreate api worker` wirksam
(`restart` liest `env_file` nicht neu ein).

## Prompt-Schutz

Jeder Provider schickt den Nutzer-Prompt (er enthält Drehbuchtext) zuerst durch
`PromptSanitizer.validate_and_sanitize(prompt, raise_on_unsafe=True)` (`core/prompt_sanitizer.py`)
und sperrt den System-Prompt mit `PromptSanitizer.wrap_with_system_lock`. Der Sanitizer kürzt auf
10 000 Zeichen, entfernt Steuer- und unsichtbare Zeichen und fasst Leerraum (auch Zeilenumbrüche)
zu einzelnen Leerzeichen zusammen. Damit lange Szenen nicht abgeschnitten werden, zerlegt
`analyze_scene_risk` sie vorher in überlappende Abschnitte (höchstens ca. 8000 Zeichen, 500
Zeichen Überlappung) und führt die Findings zusammen; doppelte Findings aus den Überlappungen
(gleiche Klasse, Regel und Zitat) werden entfernt.

Die Muster sind bewusst eng und **nur englischsprachig**: Sie zielen auf Text, der sich an das
Modell selbst richtet – Anweisungs-Overrides („ignore previous instructions“), Rollenwechsel zu
einer KI, Rollenmarker mit anschließender Anweisung („SYSTEM: you must …“), Chat-Template-Token
(`<|im_start|>`, `[INST]`, `<<SYS>>`), eingeschleuste eigene Begrenzer (`[SCENE START]`/`[SCENE END]`),
Prompt-Exfiltration und Skript-Payloads. Normale Drehbuchsprache (Trennlinien, „User:innen“,
„act as if …“) löst sie nicht aus; anderssprachige oder umschriebene Injektionen und
Unicode-Tricks erkennt der Filter dagegen nicht – er ist ein Stolperdraht, die eigentliche
Absicherung sind der gerahmte Prompt, der gesperrte System-Prompt und die Schema-Validierung der
Antwort. Trifft ein Muster, lehnt der Provider den Aufruf mit
`LLMException("Prompt blocked by security policy")` ab; die Risikoanalyse beendet den Job dann
ohne Retry mit `failed` („Szene <nr>: vom Prompt-Injection-Filter blockiert – manuelle Prüfung
erforderlich“). Details stehen im Modul-Docstring von `core/prompt_sanitizer.py`. Treffer zählt
`eki_prompt_sanitizer_hits_total{action="blocked"}`; geloggt werden nur Regelname und
Prompt-Länge, nie der Prompt.

## Neuen LLM-Provider hinzufügen

Checkliste; jeder Schritt ist nötig, sonst greifen Validierung, Produktionsschutz, Metriken oder
Abnahmewerkzeuge nicht.

1. **Klasse anlegen** – `llm/<name>.py`, erbt von `BaseLLMProvider`, implementiert `generate`,
   `generate_structured`, `health_check` und `provider_name` (eindeutiger Name, z. B.
   `"my_provider"`; er erscheint als Label `provider` in allen Metriken). Ein lokaler Dienst mit
   Ollama-kompatibler API erbt besser direkt von `OllamaProvider` (wie `LocalMistralProvider`) und
   übernimmt damit Concurrency-Cap, Metriken, Sanitizer und Embeddings.
2. **Pflichten jeder Aufrufmethode** (Vorbild `llm/mistral_cloud.py`):
   - Nutzer-Prompt mit `PromptSanitizer.validate_and_sanitize(..., raise_on_unsafe=True)` prüfen,
     einen `ValueError` als `LLMException("Prompt blocked by security policy", ...)` weiterreichen;
     System-Prompt mit `PromptSanitizer.wrap_with_system_lock` sperren.
   - Den eigentlichen HTTP-Aufruf in `async with observe_llm_call(self.provider_name, "<operation>")`
     (`core/metrics.py`) kapseln; das liefert `eki_llm_requests_total`,
     `eki_llm_request_duration_seconds` und `eki_llm_requests_in_flight`.
   - Bei einer GPU-gebundenen lokalen Engine einen prozessweiten Cap vorsehen (Vorbild
     `_ollama_slot()` in `llm/ollama.py` mit `LLM_QUEUE_WAIT`).
   - Transport- und Formatfehler als `LLMException` ohne Prompt-/Antwortinhalt melden
     (Hilfsfunktionen in `llm/base.py`); nichts davon ins Log schreiben (Abnahmetest 7).
   - `generate_structured` muss ein `dict` liefern, das dem übergebenen JSON-Schema entspricht –
     ohne native Schema-Unterstützung des Modells wie bei Mistral Cloud mit `jsonschema`
     nachvalidieren.
3. **Embeddings** – `embed()` implementieren, wenn die KB mit diesem Provider funktionieren soll:
   Vektor mit genau 1024 Werten (sonst passt er nicht in `kb_embeddings.vector`). Ohne `embed()`
   ist die KB wie bei `mistral_cloud` nicht nutzbar.
4. **Einstellungen** (`api/config.py`):
   - Felder für Modell, Endpoint, Timeout, ggf. API-Key ergänzen; für Geheimnisse zusätzlich ein
     `<name>_api_key_file`-Feld und einen Eintrag im `file_mapping` von `load_secrets_from_files`.
   - Den Namen in die Menge `allowed` von `validate_llm_provider` aufnehmen (sonst startet die
     Anwendung mit dem neuen Wert nicht), ggf. eine Bindestrich-Variante in `_LLM_PROVIDER_ALIASES`.
5. **Factory** (`llm/factory.py`) – Zweig in `get_llm_provider` mit dem Konfigurations-Dict.
   **Verlässt der Provider das lokale Netz:** denselben Produktionsschutz wie für `mistral_cloud`
   einbauen, und zwar an beiden Stellen – im Factory-Zweig **und** in
   `Settings.validate_production_security` (prüft derzeit nur den Namen `mistral_cloud`) – und
   `is_external_provider()` erweitern.
6. **Skripte** – `scripts/run_parity.py` (`build_provider`: welche Einstellung ein
   `<provider>:<modell>`-Override setzt; unbekannte Namen landen sonst bei `ollama_model`) und
   `scripts/smoke_prod.py` (`LOCAL_PROVIDERS` / `EXTERNAL_PROVIDERS`, sonst schlägt der
   Abnahmetest 9 fehl bzw. erkennt externe Aufrufe nicht).
7. **Tests** – Provider-Unit-Tests mit gemocktem HTTP (Vorlagen: `tests/test_ollama_provider.py`,
   `tests/test_mistral_cloud_structured.py`, `tests/test_m11_local_mistral.py`), Factory- und
   Validator-Tests (`tests/test_m12_rate_limiting_and_factory.py`, `tests/test_config.py`).
8. **Paritätslauf** – `scripts/run_parity.py --providers <name>[:<modell>]` gegen das Golden-Set;
   alle Schwellen aus [`M11_PARITY_REPORT.md`](M11_PARITY_REPORT.md) müssen erfüllt sein, bevor der
   Provider produktiv genutzt wird (Aufruf im API-Container: `UAT/UAT_TESTPLAN.md` AT8).
9. **Dokumentation und Betrieb** – `.env.example`, [`CONFIGURATION.md`](CONFIGURATION.md), dieses
   Dokument; bei externen Providern Datenschutz (AVV, [`DATENSCHUTZ.md`](DATENSCHUTZ.md)) und
   Firewall-Freigabe. Grafana-Dashboards gruppieren nach `provider` und brauchen keine Anpassung;
   `eki_build_info` zeigt den aktiven Provider.

## Retries, Timeouts, Nebenläufigkeit

- Retries übernimmt Temporal: LLM-Activities laufen mit höchstens 2 Versuchen (`_RETRY_LLM` in
  `workflows/security_check.py`) und `LLM_ACTIVITY_TIMEOUT_SECONDS` pro Versuch. Eigene
  Retry-Schleifen im Provider sind nicht nötig (Bibliotheken wie `tenacity` oder `aiolimiter`
  gehören nicht zu den Abhängigkeiten).
- Drosselung über `OLLAMA_MAX_CONCURRENT_REQUESTS`; Parallelität pro Workflow über
  `LLM_PARALLEL_ENABLED` und `*_CONCURRENCY` (M07). Werte erst erhöhen, wenn
  `eki_llm_queue_wait_seconds` hoch und VRAM frei ist.
- LLM-Antworten nicht cachen – sie enthalten Drehbuchbezüge (Processing-Only).
- Streaming wird nicht verwendet; es gibt keine `generate_stream`-Methode.

## Testen

```bash
# Provider der laufenden Konfiguration prüfen (Health-Check, Text, strukturierte Ausgabe)
docker compose exec api python scripts/test_llm.py

# Unit-Tests der Provider (ohne echtes Modell, HTTP gemockt)
pytest tests/test_ollama_provider.py tests/test_mistral_cloud_structured.py tests/test_m11_local_mistral.py --no-cov
```

Für die Abnahme zählt der Paritätslauf (Schritt 8 oben) bzw. `scripts/smoke_prod.py`.
Weitere Testarten und Voraussetzungen: [`TESTING_GUIDE.md`](TESTING_GUIDE.md).

## Monitoring

| Metrik | Labels | Inhalt |
|---|---|---|
| `eki_llm_requests_total` | `provider`, `operation`, `outcome` | Aufrufe nach Ergebnis (`success`/`error`) |
| `eki_llm_request_duration_seconds` | `provider`, `operation` | Transportzeit ohne Wartezeit |
| `eki_llm_queue_wait_seconds` | `provider` | Wartezeit auf einen Ollama-Slot (Label derzeit immer `ollama`, auch für `local_mistral`) |
| `eki_llm_requests_in_flight` | `provider` | laufende Aufrufe |
| `eki_prompt_sanitizer_hits_total` | `action` | Sanitizer-Treffer (`blocked`/`allowed`) |
| `eki_build_info` | `version`, `llm_provider`, `role` | aktiver Provider je Prozess |

Dashboard `eki-llm`, Alerts `EkiLlmErrorRateHigh`, `EkiOllamaQueueWaitHigh`,
`EkiPromptSanitizerBlockedSpike` ([`M09_SLO.md`](M09_SLO.md)).

## Siehe auch

- [`CONFIGURATION.md`](CONFIGURATION.md) – alle Einstellungen
- [`OPERATIONS_GUIDE.md`](OPERATIONS_GUIDE.md) – Modelle laden, Betrieb, Modellwechsel
- [`M11_PARITY_REPORT.md`](M11_PARITY_REPORT.md) – Paritätsschwellen Cloud ↔ Lokal
- [`M06_KB_GUIDE.md`](M06_KB_GUIDE.md) – Knowledge Base und Embeddings
- [`DATENSCHUTZ.md`](DATENSCHUTZ.md) – LLM-Verarbeitung und Datenflüsse
- [`../openapi/eki-api-v1.0.yaml`](../openapi/eki-api-v1.0.yaml) – API-Spezifikation
- [`../workflows/security_check.py`](../workflows/security_check.py) – Workflow, Retry-Policies
