# Last, Parallelität und Zeitlimits – alle Einstellungen im Detail

Stand: nach Release 1.0.1 (mit Dokument-Limit und Heartbeats). Zielgruppe: IT-Betrieb.
Kurzreferenz aller Variablen: [`CONFIGURATION.md`](CONFIGURATION.md). Betrieb der Warteschlange:
[`OPERATIONS_GUIDE.md`](OPERATIONS_GUIDE.md) §5.12, Ollama-Instanz: §4.11.

**Kurzfassung:** Solange eine GPU die KI trägt, ist überall `1` richtig: ein Dokument zur Zeit
(`MAX_CONCURRENT_DOCUMENTS=1`), ein KI-Aufruf zur Zeit (`OLLAMA_MAX_CONCURRENT_REQUESTS=1`) und eine
Anfrage zur Zeit auf dem Ollama-Server (`OLLAMA_NUM_PARALLEL=1`, dort gesetzt). Mehr Parallelität
bringt nur dann mehr Durchsatz, wenn der Grafikspeicher für mehrere gleichzeitige Anfragen reicht.

---

## 1. Wie ein Job die KI nutzt

```
ePro ──► API ──► Temporal ──► Warteschlange ──► Worker ──► Arbeitsschritt ──► KI-Aufruf ──► Ollama-Server ──► GPU
                              (Dokument-Limit)            (Activity)          (Semaphor     (Parallel-Plätze,
                                                                               je Prozess)   eigene Warteschlange)
```

Jede Szene ist mindestens ein KI-Aufruf: Bei PDFs gliedert die KI zuerst jeden Szenenblock
(`structure_scene_llm`), danach prüft sie jede Szene auf Risiken (`analyze_scene_risk`; sehr lange
Szenen in mehreren überlappenden Abschnitten, mit Wissensbasis zusätzlich ein Embedding-Aufruf).
Die Grenzen wirken auf fünf Ebenen:

| Ebene | Einstellung | Wo gesetzt | Begrenzt |
|---|---|---|---|
| Dokumente | `MAX_CONCURRENT_DOCUMENTS` | `.env.local` | wie viele Jobs gleichzeitig verarbeitet werden, über alle Worker |
| Szenen eines Dokuments | `LLM_PARALLEL_ENABLED`, `PDF_STRUCTURE_CONCURRENCY`, `RISK_ANALYSIS_CONCURRENCY` | `.env.local` | wie viele KI-Schritte eines Jobs gleichzeitig laufen |
| Arbeitsschritte im Worker | `WORKER_MAX_CONCURRENT_ACTIVITIES` | `.env.local` | wie viele Arbeitsschritte (aller Art) ein Worker gleichzeitig ausführt |
| KI-Aufrufe je Prozess | `OLLAMA_MAX_CONCURRENT_REQUESTS`, `OLLAMA_MIN_INTERVAL_MS` | `.env.local` | wie viele Anfragen ein Worker- bzw. API-Prozess gleichzeitig an Ollama schickt |
| Ollama-Server | `OLLAMA_NUM_PARALLEL` u. a. | Ollama-Server (systemd/Docker) | wie viele Anfragen Ollama je Modell gleichzeitig rechnet – für alle Absender |

Wichtig: Die Variablen der `.env.local` erreichen den Ollama-Server nicht. Er liest nur seine
eigenen Umgebungsvariablen (§5). Mit jeder Anfrage schickt die eKI lediglich Modell und – bei
Gliederung und Risikoanalyse – die Kontextgröße (`OLLAMA_NUM_CTX`, §3.8) mit.

## 2. Wann eine Änderung wirkt

| Wie die eKI den Wert liest | Einstellungen | Änderung wirkt |
|---|---|---|
| Beim Jobstart in den Job eingefroren (API) | `LLM_PARALLEL_ENABLED`, `PDF_STRUCTURE_CONCURRENCY`, `RISK_ANALYSIS_CONCURRENCY`, `LLM_ACTIVITY_TIMEOUT_SECONDS`, `DOCUMENT_QUEUE_TIMEOUT_SECONDS`, ob sich ein Job anstellt (`MAX_CONCURRENT_DOCUMENTS` > 0), `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT`, `BUFFER_TTL_SECONDS` als Abholfenster (Pull-Überwachung) | auch die API liest die `.env.local` nur beim Start: nach `docker compose up -d --force-recreate api`, dann für alle danach eingereichten Jobs |
| Beim Start des Prozesses gelesen | Grenze `MAX_CONCURRENT_DOCUMENTS` (Worker), `BUFFER_TTL_SECONDS` als Lebensdauer im Zwischenspeicher (API für das Drehbuch, Worker für Zwischenstände und Report), `OLLAMA_MAX_CONCURRENT_REQUESTS`, `OLLAMA_MIN_INTERVAL_MS`, `OLLAMA_TIMEOUT`, `OLLAMA_NUM_CTX`, `OLLAMA_THINK`, `WORKER_MAX_CONCURRENT_ACTIVITIES`, `WORKER_MAX_CONCURRENT_WORKFLOW_TASKS` | nach `docker compose up -d --force-recreate api worker`, dann sofort – die Grenze `MAX_CONCURRENT_DOCUMENTS` ab der nächsten Nachfrage auch für schon wartende Jobs |
| Ollama-Server | `OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_KEEP_ALIVE` … | nach Neustart des Ollama-Dienstes |

`docker compose restart` liest die `.env.local` **nicht** neu ein. Was im Container tatsächlich gilt:
`docker compose exec worker env | grep -E "OLLAMA|WORKER_MAX|LLM_|DOCUMENT"` und die Startzeile des
Workers `Worker concurrency caps: workflow_tasks=…, activities=…, documents=… (0 = no limit), ollama_requests=…`.

---

## 3. Einstellungen der eKI

### 3.1 `MAX_CONCURRENT_DOCUMENTS` – Dokument-Limit

| Standard | Bereich | gelesen von | wirkt |
|---|---|---|---|
| `1` | 0–50 | API (ob anstellen), Worker (Grenze) | Grenze nach Neuerzeugung des Workers, auch für Wartende |

**Was es tut:** Höchstens so viele Jobs werden gleichzeitig verarbeitet. Ein Job holt sich vor dem
ersten Verarbeitungsschritt (Parsen bzw. Textextraktion) einen Platz und gibt ihn nach der
Risikoanalyse zurück – vor Report-Erzeugung und Zustellung. Bei Fehler oder Abbruch (Cancel) gibt er
ihn ebenfalls frei; scheitert die Freigabe (z. B. Redis-Störung), läuft der Job trotzdem weiter und der
Platz wird mit Ablauf der Lease frei (§4). Weitere Jobs bleiben `pending` und warten in einer Warteschlange: zuerst nach
`priority` (1 = höchste), bei gleicher Priorität in der Reihenfolge der ersten Nachfrage (praktisch: Eingang;
nach einem Worker-Ausfall kann sie abweichen). Ein wartender Job fragt mit
dauerhaften Temporal-Timern nach (§4); er belegt dabei keinen Worker-Platz, und es läuft kein
Zeitlimit eines Arbeitsschritts.

**`0`** schaltet das Limit ab (Verhalten bis 1.0.1): Alle Jobs laufen verzahnt und teilen sich die KI.
Das ist nur sinnvoll, wenn die KI echte Parallelität verkraftet, oder mit `mistral_cloud` auf der Stage.

**Wann ändern:** Erhöhen nur, wenn der Ollama-Server mehrere Anfragen gleichzeitig ohne Ausweichen auf
die CPU rechnet (`OLLAMA_NUM_PARALLEL` > 1, genug Grafikspeicher, §5) – und dann zusammen mit
`OLLAMA_MAX_CONCURRENT_REQUESTS`. Bei einer GPU bringt `1` den besten Durchsatz: Die Dokumente werden
nacheinander fertig, statt alle gemeinsam spät.

**Wechselwirkungen:**
- Die Grenze gilt über alle Worker-Prozesse hinweg (Zustand in Redis, `eki:docslot:*`).
- KI-Aufrufe der API beim Einlesen von Wissensbasis-Dokumenten zählen nicht dazu.
- Zur Verarbeitungsdauer kommt die Wartezeit hinzu; ePro muss entsprechend lange abfragen (§6).
- Gilt für alle Provider, auch `mistral_cloud`.

**Prüfen:** Worker-Log `Document slot granted …` bzw. `Document limit reached, job waits: … position=…`,
Metriken `eki_document_slots_in_use` und `eki_document_queue_length`, Redis-Befehle in
`OPERATIONS_GUIDE.md` §5.12.

### 3.2 `DOCUMENT_QUEUE_TIMEOUT_SECONDS` – Höchstwartezeit

| Standard | Bereich | gelesen von | wirkt |
|---|---|---|---|
| `7200` (2 h) | ≥ 60 | API, beim Jobstart eingefroren | für neue Jobs |

**Was es tut:** Findet ein Job in dieser Zeit keinen Platz, endet er mit `failed` und der inhaltsfreien
Meldung `Kein freier Verarbeitungsplatz innerhalb von <n> min (Dokument-Limit <N>, Warteposition <p>) –
Job bitte später neu einreichen`. Er verlässt die Warteschlange, sein zwischengespeichertes Drehbuch
wird gelöscht, und – falls `EPRO_WEBHOOK_URL` gesetzt ist – geht der Webhook mit dem Grund
`processing_failed` an ePro.

**Grenzen (Start-Validator, nur bei `MAX_CONCURRENT_DOCUMENTS` > 0):**
- kürzer als `BUFFER_TTL_SECONDS`: Das Drehbuch liegt ab dem Upload nur so lange im Zwischenspeicher;
  danach wäre es beim Start der Verarbeitung schon gelöscht.
- Wartezeit + `BUFFER_TTL_SECONDS` < `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT`: Nach der Wartezeit braucht der
  Job noch Zeit für die Verarbeitung und danach bis zu `BUFFER_TTL_SECONDS` für das Abholfenster (Pull)
  bzw. bis zu 6 h für Push-Wiederholungen.

Verstößt die Konfiguration dagegen, starten API und Worker nicht und nennen beide Variablen. Beide
Prüfungen sind strikt (`<`) und reservieren **keine** Zeit für die Verarbeitung: Bei Standardwerten
lehnt der Validator erst ab 14 400 s ab, 14 399 s ließen der Verarbeitung aber nur noch 1 s.

**Wann ändern:** Länger, wenn regelmäßig viele große Drehbücher gleichzeitig kommen und sie lieber
warten als scheitern sollen – dann `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` um denselben Betrag erhöhen,
damit die Verarbeitung ihre rund 2 h behält, und ePro informieren (längeres Abfragen). Kürzer, wenn ePro schneller eine klare Absage braucht.

### 3.3 `OLLAMA_MAX_CONCURRENT_REQUESTS` – KI-Aufrufe je Prozess

| Standard | Bereich | gelesen von | wirkt |
|---|---|---|---|
| `1` | 1–8 | jeder API- und Worker-Prozess | nach Neuerzeugung |

**Was es tut:** Ein Semaphor im Prozess lässt höchstens so viele Anfragen gleichzeitig an Ollama –
für Gliederung, Risikoanalyse, Embeddings und Testaufrufe. Weitere Aufrufe warten im Prozess; diese
Wartezeit misst `eki_llm_queue_wait_seconds` (Alarm `EkiOllamaQueueWaitHigh`, wenn das 95. Perzentil
15 min lang über 2 min liegt). Die Wartezeit in der Dokument-Warteschlange (§3.1) zählt nicht dazu.

**Gilt nicht** über Prozessgrenzen: API und Worker haben je einen eigenen Semaphor, mehrere Worker
ebenso, und andere Anwendungen oder eine zweite eKI-Instanz (Stage, Entwicklung) am selben
Ollama-Server sieht er gar nicht. Gilt nur für die Provider `local_mistral` und `ollama`, nicht für
`mistral_cloud`.

**Wann ändern:** Nur zusammen mit dem Ollama-Server (`OLLAMA_NUM_PARALLEL` mindestens gleich groß) und
nur bei ausreichend Grafikspeicher (§5.1). Ein höherer Wert ohne passende Serverseite verlagert das
Warten nur in Ollamas Warteschlange.

### 3.4 `OLLAMA_MIN_INTERVAL_MS` – Mindestabstand

| Standard | Bereich | gelesen von | wirkt |
|---|---|---|---|
| `0` (aus) | ≥ 0 | jeder API- und Worker-Prozess | nach Neuerzeugung |

**Was es tut:** Zwischen dem **Start** zweier KI-Aufrufe desselben Prozesses vergehen mindestens so viele
Millisekunden. Die Pause zählt zur gemessenen Wartezeit (`eki_llm_queue_wait_seconds`).

**Wirkung in der Praxis:** Mit `OLLAMA_MAX_CONCURRENT_REQUESTS=1` startet der nächste Aufruf ohnehin erst,
wenn der vorige fertig ist. Der Abstand bremst dann nur Aufrufe, die kürzer sind als er selbst – etwa
Embeddings. Ein Wert von 500 ms ändert an Risiko-Aufrufen, die Sekunden bis Minuten dauern, nichts.
Spürbar wird er erst, wenn er über der typischen Aufrufdauer liegt (Beispiel: bei 20 s je Aufruf und
`30000` entstehen Pausen von rund 10 s) oder wenn mehrere Aufrufe gleichzeitig erlaubt sind (Werte > 1):
Dann entzerrt er deren Start.

**Wann ändern:** Selten. Teilt sich die eKI die GPU mit anderen Anwendungen, ist ein kleines
Dokument-Limit wirksamer. Ein großer Abstand kostet entsprechend Durchsatz: Jede Sekunde Pause verlängert
ein Dokument mit 300 Szenen um rund 5 min je Durchgang (Gliederung bzw. Risikoanalyse).

### 3.5 `LLM_PARALLEL_ENABLED`, `PDF_STRUCTURE_CONCURRENCY`, `RISK_ANALYSIS_CONCURRENCY` – Szenen parallel

| Variable | Standard | Bereich | wirkt |
|---|---|---|---|
| `LLM_PARALLEL_ENABLED` | `false` | true/false | eingefroren, neue Jobs |
| `PDF_STRUCTURE_CONCURRENCY` | `1` | 1–8 | eingefroren, neue Jobs |
| `RISK_ANALYSIS_CONCURRENCY` | `1` | 1–8 | eingefroren, neue Jobs |

**Was es tut:** Mit `LLM_PARALLEL_ENABLED=true` startet ein Job bis zu `PDF_STRUCTURE_CONCURRENCY`
Gliederungs- und bis zu `RISK_ANALYSIS_CONCURRENCY` Risiko-Schritte gleichzeitig. Ohne das Flag gelten
die beiden Werte nicht (immer 1). Die Reihenfolge der Ergebnisse bleibt erhalten. Scheitert ein
Schritt, startet der Job keine neuen, lässt laufende zu Ende laufen und endet dann mit `failed`.

**Wann ändern:** Wenn einzelne große Dokumente schneller fertig werden sollen (Pflichtenheft: 300–350
Seiten in ≤ 120 min) und der Ollama-Server Parallelität verkraftet. Dann
`OLLAMA_MAX_CONCURRENT_REQUESTS` und `OLLAMA_NUM_PARALLEL` auf denselben Wert setzen und
`WORKER_MAX_CONCURRENT_ACTIVITIES` mindestens auf `MAX_CONCURRENT_DOCUMENTS` × `*_CONCURRENCY` + 2
halten. Sind die `*_CONCURRENCY`-Werte größer als `OLLAMA_MAX_CONCURRENT_REQUESTS`, warten die
überzähligen Schritte am Semaphor – und diese Wartezeit zählt gegen ihr Zeitlimit (§3.6).

**Abwägung:** Bei gleicher Hardware bringt „ein Dokument mit 2 parallelen Szenen“ (Limit 1, Parallel 2)
denselben Gesamtdurchsatz wie „zwei Dokumente mit je einer Szene“ (Limit 2, Parallel aus), lässt das
einzelne Dokument aber früher fertig werden. Für viele kleine Drehbücher ist der Unterschied gering.

### 3.6 `LLM_ACTIVITY_TIMEOUT_SECONDS` – Zeitlimit eines KI-Schritts

| Standard | Bereich | gelesen von | wirkt |
|---|---|---|---|
| `600` (10 min) | ≥ 60 | API, eingefroren | für neue Jobs |

**Was es tut:** Ein Gliederungs- oder Risiko-Schritt muss innerhalb dieser Zeit fertig sein. Die Zeit
läuft ab dem Moment, in dem ein Worker den Schritt übernimmt, und **schließt das Warten am Semaphor
ein**, außerdem alle KI-Aufrufe des Schritts (Abschnitte langer Szenen, Embedding der Wissensbasis).
Läuft sie ab, wiederholt Temporal den Schritt einmal (nach 5 s); scheitert auch der zweite Versuch, endet
der Job mit `failed` (`… fehlgeschlagen (TimeoutError)`). Den abgelaufenen Versuch bricht das
Temporal-SDK wenige Sekunden nach dem Zeitlimit selbst ab.

**Wann ändern:** Erhöhen, wenn Schritte trotz Dokument-Limit regelmäßig am Limit scheitern, etwa bei
sehr langen Szenen, langsamer GPU oder mit parallelen Szenen (§3.5). Faustregel: mindestens das Doppelte
von `OLLAMA_TIMEOUT`, damit ein langsamer Aufruf nicht sofort den ganzen Schritt kostet.

### 3.7 `OLLAMA_TIMEOUT` – Zeitlimit eines KI-Aufrufs

| Standard | Bereich | gelesen von | wirkt |
|---|---|---|---|
| `300` (5 min) | Sekunden | Worker und API (`local_mistral`, `ollama`) | nach Neuerzeugung |

**Was es tut:** HTTP-Zeitlimit einer Anfrage an Ollama – kein Gesamtlimit, sondern je Phase
(Verbindungsaufbau, Senden, Warten auf die Antwort). Praktisch zählt das Warten auf die Antwort,
einschließlich der Zeit in Ollamas eigener Warteschlange; die Wartezeit am Semaphor der eKI (§3.3) zählt
nicht dazu. Die eKI fordert Antworten ohne Streaming an: Ollama schickt erst, wenn die ganze Antwort
fertig ist. Rechnet Ollama länger, bricht der Aufruf ab (`LLMException`).

**Folgen eines Abbruchs:** in der Risikoanalyse ein Fehler des Schritts (Wiederholung, dann `failed`,
Meldung `Szene <nr>: Risikoanalyse fehlgeschlagen (LLMException)`); in der PDF-Gliederung dagegen kein
Abbruch – die Szene bekommt eine Ersatzgliederung ohne Ort und Figuren, ihr Text wird trotzdem auf
Risiken geprüft.

**Wann ändern:** Erhöhen bei langsamer Hardware (CPU-Betrieb); dann `LLM_ACTIVITY_TIMEOUT_SECONDS`
mitziehen (§3.6).

### 3.8 `OLLAMA_NUM_CTX` (und `OLLAMA_THINK`) – Kontextfenster

| Variable | Standard | wirkt |
|---|---|---|
| `OLLAMA_NUM_CTX` | `32768` Token | nach Neuerzeugung |
| `OLLAMA_THINK` | `false` | nach Neuerzeugung |

**Was es tut:** Die eKI schickt `num_ctx` bei jeder Gliederungs- und Risiko-Anfrage mit; Ollama lädt
das Modell mit einem Kontextfenster dieser Größe. Es muss System-Anweisung, Taxonomie, Antwortschema,
Kontext aus der Wissensbasis und den Szenentext fassen. Das Fenster belegt Grafikspeicher – und zwar
**je Parallel-Platz** auf dem Ollama-Server (§5.1). `OLLAMA_THINK=false` schaltet den „Denkmodus“ von
Modellen wie gemma4/qwen3 ab; Mistral ignoriert den Wert.

**Wann ändern:** `16384` spart etwa die Hälfte des Kontextspeichers, wenn die GPU knapp ist. Sehr lange
Szenen werden ohnehin in Abschnitte geteilt. Nach jeder Änderung den Paritätslauf wiederholen
(`OPERATIONS_GUIDE.md` §7).

### 3.9 `WORKER_MAX_CONCURRENT_ACTIVITIES`, `WORKER_MAX_CONCURRENT_WORKFLOW_TASKS`

| Variable | Standard | Bereich | wirkt |
|---|---|---|---|
| `WORKER_MAX_CONCURRENT_ACTIVITIES` | `20` | ≥ 1 | nach Neuerzeugung |
| `WORKER_MAX_CONCURRENT_WORKFLOW_TASKS` | `10` | ≥ 1 | nach Neuerzeugung |

**Was es tut:** `WORKER_MAX_CONCURRENT_ACTIVITIES` begrenzt, wie viele Arbeitsschritte ein
Worker-Prozess gleichzeitig ausführt – KI-Schritte, Parsen, Report, Zustellung, Statusupdates,
Warteschlangen-Abfragen. Weitere Schritte warten in der Temporal-Warteschlange; dort läuft ihr
Zeitlimit **noch nicht** (Ausnahme: das 6-h-Fenster der Zustellung läuft ab ihrer Einplanung). `WORKER_MAX_CONCURRENT_WORKFLOW_TASKS` begrenzt gleichzeitige
Workflow-Entscheidungen (kurze Rechenschritte); das ist kein Limit für die Zahl der Jobs.

**Wann ändern:** Mit Dokument-Limit kaum nötig. Ohne Dokument-Limit (`MAX_CONCURRENT_DOCUMENTS=0`
oder Stand 1.0.1) verhindert ein kleiner Wert, dass viele KI-Schritte gleichzeitig am Semaphor warten
und dabei ihr Zeitlimit verbrauchen: etwa `OLLAMA_MAX_CONCURRENT_REQUESTS + 2`. Nicht kleiner als die
Zahl der KI-Schritte, die ein Dokument gleichzeitig startet (`*_CONCURRENCY`), plus 2, sonst warten
Statusupdates und Zustellungen unnötig.

### 3.10 `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` und `BUFFER_TTL_SECONDS`

| Variable | Standard | wirkt |
|---|---|---|
| `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` | `36000` (10 h) | für neue Jobs (API setzt es beim Start) |
| `BUFFER_TTL_SECONDS` | `21600` (6 h) | Abholfenster eingefroren, Zwischenspeicher nach Neuerzeugung; Pflichtenheft ≤ 6 h |

`BUFFER_TTL_SECONDS` ist die Lebensdauer von Drehbuch, Zwischenständen und Report im
Zwischenspeicher, gezählt ab dem jeweiligen Speichern, und zugleich das Abholfenster im Pull-Modus.
Eingefroren wird nur das Abholfenster (Pull-Überwachung im Workflow); die Lebensdauer im
Zwischenspeicher setzt der jeweils speichernde Prozess mit seinem Startwert – die API für das Drehbuch,
der Worker für Zwischenstände und Report.
Push-Wiederholungen laufen unabhängig davon fest bis zu 6 h; ist `BUFFER_TTL_SECONDS` kürzer, ist der
Report vorher gelöscht und die Zustellung endet als Dead Letter `report_expired`. `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` ist die Gesamtlebensdauer eines
Jobs; endet er dadurch, laufen keine Aufräumschritte mehr (der Status bleibt stehen). Deshalb muss der
Wert Warteschlange + Verarbeitung + `BUFFER_TTL_SECONDS` abdecken (§6).

### 3.11 Request-Feld `priority` (`TEMPORAL_PRIORITY_LEVELS`)

`priority` 1 (höchste) … 10, Standard 5, setzt ePro je Job. Es ordnet die Warteschlange des
Dokument-Limits (vor der Eingangsreihenfolge) und wartende Aufgaben in Temporal. Laufende Jobs werden
nicht verdrängt. `TEMPORAL_PRIORITY_LEVELS` muss zur Temporal-Dynamic-Config passen (Standard 10,
`OPERATIONS_GUIDE.md` §5.11).

---

## 4. Feste Werte (nicht konfigurierbar)

| Was | Wert | Wozu |
|---|---|---|
| Nachfragen eines wartenden Jobs | sofort, dann im Abstand von 5, 10, 20, 40 s, danach alle 60 s | dauerhafte Timer, kein Worker-Platz |
| Lease eines belegten Platzes | 30 min | gibt Plätze von Jobs frei, die ohne Freigabe verschwunden sind (z. B. Terminate) |
| Verlängerung der Lease | jede Minute, durch die laufenden KI-Schritte | Lücken ohne KI-Schritt bleiben bei PDF unter 30 min (Textextraktion und Szenenteilung im ungünstigsten Fall zusammen rund 21 min). Bekannter Grenzfall: Bei FDX kann das Parsen mit 3 Versuchen à 10 min die Lease überschreiten – dann bekommt kurzzeitig ein weiterer Job einen Platz |
| Wartende ohne Nachfrage | nach 5 min aus der Schlange | ein beendeter wartender Workflow blockiert niemanden; fragt er wieder nach, erhält er seinen alten Platz |
| Heartbeat der KI-Schritte | alle 10 s; das SDK sendet höchstens alle 30 s | endet der Job (Fehler, Cancel, Terminate), bricht ein laufender KI-Schritt nach spätestens etwa 30 s ab, statt bis zu seinem Zeitlimit weiterzulaufen |
| Wiederholung eines KI-Schritts | 2 Versuche, 5 s Abstand | danach `failed` |
| Warteschlangen-Schritte (`acquire`/`release_document_slot`) | 30 s Zeitlimit, 3 Versuche | scheitert `acquire` (Redis weg), scheitert der Job; scheitert `release`, läuft der Job weiter und der Platz wird erst mit Ablauf der Lease (≤ 30 min) frei. Scheitert die Verlängerung der Lease, läuft der KI-Schritt ebenfalls weiter |

---

## 5. Einstellungen auf dem Ollama-Server

Diese Variablen werden **auf dem Ollama-Host** gesetzt – bei systemd mit
`sudo systemctl edit ollama.service`, Abschnitt `[Service]`, z. B. `Environment="OLLAMA_NUM_PARALLEL=1"`,
danach `sudo systemctl daemon-reload && sudo systemctl restart ollama`; bei Docker per `-e` bzw.
`environment:` und neuem Container. Prüfen: `journalctl -u ollama | grep "server config" | tail -1`
zeigt die beim Start gelesenen Werte, `ollama ps` die geladenen Modelle und in der Spalte PROCESSOR, ob
sie ganz auf der GPU liegen (`100% GPU`).

| Variable | Empfehlung für die eKI | Ollama-Standard | Bedeutung |
|---|---|---|---|
| `OLLAMA_NUM_PARALLEL` | `1` | `1` (ältere Versionen wählten automatisch 1 oder 4) | Anfragen, die Ollama je Modell gleichzeitig rechnet; weitere warten in Ollamas Warteschlange |
| `OLLAMA_MAX_LOADED_MODELS` | `2`, wenn der Server nur der eKI dient | 3 × Anzahl GPUs bzw. 3 bei CPU | Modelle gleichzeitig im Speicher – hier Sprach- und Embedding-Modell |
| `OLLAMA_KEEP_ALIVE` | `30m` | 5 min | wie lange ein Modell nach der letzten Anfrage geladen bleibt |
| `OLLAMA_MAX_QUEUE` | Standard lassen | 512 | Warteschlange des Ollama-Schedulers (Anfragen, die auf das Laden eines Modells warten); ist sie voll, antwortet Ollama mit HTTP 503 (`server busy … maximum pending requests exceeded`), was die eKI als Fehler des Aufrufs wertet. Anfragen an ein schon geladenes Modell, die nur auf einen freien Parallel-Platz warten, zählen nicht dazu: Sie warten ohne Obergrenze, bis ein Platz frei wird – praktisch begrenzt das `OLLAMA_TIMEOUT` der eKI |
| `OLLAMA_HOST` | `0.0.0.0:11434` | `127.0.0.1:11434` (im offiziellen Docker-Image `0.0.0.0:11434`) | Adresse, auf der Ollama lauscht; Port per Firewall nur für den eKI-Host öffnen – Ollama hat keine Anmeldung. Im Docker-Betrieb erreichen ihn alle Container im selben Docker-Netz und alle über veröffentlichte Ports |
| `OLLAMA_FLASH_ATTENTION` | nicht setzen | automatisch an, wenn die GPU sie unterstützt (seit Ollama 0.30.0; für Mistral Small 3.x schon seit 0.13.2) | senkt den Speicherbedarf großer Kontexte; `=1` erzwingt, `=0` schaltet ab. Voraussetzung für `OLLAMA_KV_CACHE_TYPE=q8_0`, das den Kontextspeicher etwa halbiert (danach Paritätslauf) |
| `OLLAMA_DEBUG`, `OLLAMA_DEBUG_LOG_REQUESTS` | nicht setzen | aus | ab `OLLAMA_DEBUG=2` protokolliert Ollama Modellausgaben im Klartext (sie können Drehbuchpassagen zitieren), `OLLAMA_DEBUG_LOG_REQUESTS=1` speichert ganze Anfragen samt Szenentext; ältere Versionen loggten schon bei `OLLAMA_DEBUG=1` ganze Prompts |

### 5.1 Grafikspeicher abschätzen

Ollama reserviert für jeden Parallel-Platz ein eigenes Kontextfenster in der Größe von `num_ctx`. Der
Speicherbedarf wächst also mit `OLLAMA_NUM_PARALLEL` × `OLLAMA_NUM_CTX`. Richtwerte für
`mistral-small3.2` (Tag `latest` = q4_K_M; Gewichte samt Vision-Encoder rund 14 GiB bzw. 15 GB Download;
Kontextspeicher in Ollamas Standard-Genauigkeit f16: 160 KiB je Token, also 5 GiB je 32 768 Token):

| `OLLAMA_NUM_PARALLEL` | Bedarf bei `OLLAMA_NUM_CTX=32768` | passt auf 24 GB? |
|---|---|---|
| 1 | rund 19 GiB plus 1–2 GiB Laufzeit | ja, knapp |
| 2 | rund 24 GiB plus Laufzeit | nein – Teile laufen auf der CPU |
| 4 | rund 34 GiB plus Laufzeit | nein |

Passt das Modell nicht ganz in den Grafikspeicher, bricht Ollama nicht ab, sondern lagert Schichten auf
die CPU aus; jeder Aufruf dauert dann ein Vielfaches. Die Formel ist eine Schätzung – maßgeblich ist
`ollama ps` nach der ersten Anfrage mit dem produktiven `OLLAMA_NUM_CTX` (Spalten SIZE, PROCESSOR und
CONTEXT). Schicken Anwendungen am selben Server unterschiedliche `num_ctx`, lädt Ollama das Modell bei
jedem Wechsel neu.

---

## 6. Zeitbudget eines Jobs (Standardwerte)

```
|<-- Warteschlange ≤ 2 h -->|<-- Verarbeitung (Ziel ≤ 2 h) -->|<-- Zustellung / Abholfenster ≤ 6 h -->|
|<----------------------------- Workflow insgesamt ≤ 10 h (TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT) ------>|

in der Verarbeitung, je KI-Schritt:  |<-- ≤ 10 min inkl. Warten (LLM_ACTIVITY_TIMEOUT_SECONDS), 2 Versuche -->|
                     je KI-Aufruf:   |<-- ≤ 5 min (OLLAMA_TIMEOUT) -->|
```

- Für die Verarbeitung gibt es kein eigenes Zeitlimit; das Pflichtenheft verlangt ≤ 120 min für 300–350
  Seiten. Was die Warteschlange nicht verbraucht, steht der Verarbeitung zusätzlich zur Verfügung.
- ePro sollte mindestens Warteschlange + Verarbeitung + Puffer abfragen: bei Standardwerten 4,5 h.
  Ein fertiger Pull-Report bleibt danach 6 h abrufbar.
- Wer `DOCUMENT_QUEUE_TIMEOUT_SECONDS` erhöht, sollte `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` um denselben
  Betrag erhöhen und ePro informieren. Der Start-Validator (§3.2) erzwingt das nur grob: Er lehnt erst ab,
  wenn für die Verarbeitung gar keine Zeit mehr bliebe.

---

## 7. Empfohlene Profile

| Profil | eKI (`.env.local`) | Ollama-Server |
|---|---|---|
| **A – eine GPU (24 GB), nur eKI** (Standard) | `MAX_CONCURRENT_DOCUMENTS=1`, `OLLAMA_MAX_CONCURRENT_REQUESTS=1`, `LLM_PARALLEL_ENABLED=false` | `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_KEEP_ALIVE=30m` |
| **B – GPU mit anderen Anwendungen geteilt** | wie A; bei Speicherengpass `OLLAMA_NUM_CTX=16384` (danach Paritätslauf) | `OLLAMA_NUM_PARALLEL=1`; `OLLAMA_MAX_LOADED_MODELS` nicht unter die Zahl der Modelle aller Anwendungen |
| **C – große GPU mit Reserve für 2 Anfragen** (per `ollama ps` geprüft) | entweder `MAX_CONCURRENT_DOCUMENTS=1`, `LLM_PARALLEL_ENABLED=true`, `RISK_ANALYSIS_CONCURRENCY=2`, `PDF_STRUCTURE_CONCURRENCY=2` (einzelne Dokumente schneller) oder `MAX_CONCURRENT_DOCUMENTS=2` (zwei Dokumente gleichzeitig); in beiden Fällen `OLLAMA_MAX_CONCURRENT_REQUESTS=2` | `OLLAMA_NUM_PARALLEL=2` |
| **D – Stage mit `mistral_cloud`** | `MAX_CONCURRENT_DOCUMENTS` nach Bedarf (0 = kein Limit); die `OLLAMA_*`-Grenzen gelten für diesen Provider nicht | – |
| **E – Entwicklung ohne GPU** | wie A, dazu `OLLAMA_NUM_CTX=16384`, `OLLAMA_TIMEOUT=900`, `LLM_ACTIVITY_TIMEOUT_SECONDS=1800` als Ausgangswerte | Overlay `docker-compose.ollama.yml` (setzt `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=30m`) |

Auf einem Ollama-Server immer nur **eine** eKI-Instanz: Stage und Entwicklung bekommen eigene
Instanzen, sonst addieren sich die Anfragen.

---

## 8. Beobachten

| Frage | Wo |
|---|---|
| Welche Werte gelten im Worker? | Startlog `Worker concurrency caps …`; `docker compose exec worker env` |
| Wie viele Jobs warten bzw. laufen? | `eki_document_queue_length`, `eki_document_slots_in_use`; `GET /v1/ops/jobs?status=pending` |
| Warten KI-Aufrufe im Prozess? | `eki_llm_queue_wait_seconds` (Dashboard `eki-llm`), Alarm `EkiOllamaQueueWaitHigh` |
| Wie lange dauern KI-Aufrufe, wie viele scheitern? | `eki_llm_request_duration_seconds`, `eki_llm_requests_total{outcome="error"}`, Alarm `EkiLlmErrorRateHigh` |
| Rechnet Ollama auf der GPU? | Ollama-Host: `ollama ps` (PROCESSOR), `nvidia-smi` |
| Woran ist ein Job gescheitert? | `error_message`: `(TimeoutError)` = Schritt-Zeitlimit, `(LLMException)` = KI-Aufruf, `Kein freier Verarbeitungsplatz …` = Warteschlange |

Störungsbilder und Maßnahmen: `OPERATIONS_GUIDE.md` §5.4 (Ollama), §5.12 (Warteschlange).
