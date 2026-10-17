# eProjekt-Integration: eKI API Anbindung

**Stand:** Oktober 2026 | **API-Version:** 1.0.1 | **Zielgruppe:** eProjekt-Entwicklungsteam

**Neu in 1.0.1 (fuer ePro relevant):** `POST /v1/security/check` (synchroner Stub) entfernt → 404;
`callback_url` ist jetzt das Push-Ziel des Jobs (§2, §4b); `priority` steuert die Reihenfolge
wartender Jobs (§2); `/v1/kb/*` nur noch mit Admin-Key – der ePro-Service-Key erhaelt dort 403
(ePro nutzt die KB nicht).

Verbindlicher Schnittstellenvertrag: `openapi/eki-api-v1.0.yaml`. Datenhaltung und Loeschfristen:
`docs/DATENSCHUTZ.md`.

---

## Uebersicht

Die eKI API nimmt Drehbuecher aus dem eProjekt entgegen, analysiert sie
KI-gestuetzt auf Sicherheitsrisiken und liefert strukturierte Reports
(JSON + PDF) zurueck. Die Integration erfolgt ausschliesslich ueber REST --
keine direkten Datenbankzugriffe.

### Ablauf in 4 Schritten

```
1. ePro sendet Drehbuch          POST /v1/security/check:async
2. ePro fragt Job-Status ab      GET  /v1/security/jobs/{job_id}     (Polling)
3. Job ist fertig (status=completed, report_id vorhanden)
4. ePro holt Report ab           GET  /v1/security/reports/{report_id} (One-Shot!)
```

**Wichtig:** Der Report kann nur **einmal** abgeholt werden. Nach dem
ersten erfolgreichen Abruf (HTTP 200) wird er sofort geloescht.

Einziger Pruef-Endpunkt ist `POST /v1/security/check:async`. Der fruehere synchrone
Stub `POST /v1/security/check` (M01, ohne Analyse) wurde in 1.0.1 entfernt und
antwortet mit **404** (`ErrorResponse`, fuer jede Methode, mit oder ohne Key).

---

## 1. Authentifizierung

Alle /v1/security/ Endpunkte erfordern einen API-Key als Bearer-Token.
Fehlt der Header oder ist der Key ungueltig, abgelaufen oder deaktiviert, antwortet
die eKI mit 401.

```
Authorization: Bearer eki_<api_key>
```

### API-Key erhalten

Der API-Key wird vom eKI-Administrator erstellt und sicher uebergeben
(Standard-Gueltigkeit 365 Tage). Der Key ist an eine `user_id` gebunden --
alle Jobs und Reports sind diesem User zugeordnet (Ownership-Check: fremde
Job-/Report-IDs liefern 404).

Optionale Header:

| Header | Bedeutung |
|--------|-----------|
| `X-Request-ID` | Korrelations-ID; wird uebernommen und in der Antwort zurueckgegeben. Fehlt sie, erzeugt die eKI eine. |
| `X-Actor-User-Id` | Muss, falls gesetzt, der `user_id` des API-Keys entsprechen -- sonst **403** |
| `X-Actor-Project-Id` | Nur informativ, wird weder gespeichert noch ausgewertet |

### Beispiel

```http
GET /v1/security/jobs/550e8400-... HTTP/1.1
Host: eki.filmakademie.de
Authorization: Bearer eki_7f3a9b2c4d5e6f1a2b3c4d5e6f7a8b9c...
```

---

## 2. Drehbuch einreichen

### Option A: JSON mit Base64 (empfohlen fuer Service-to-Service)

```http
POST /v1/security/check:async HTTP/1.1
Host: eki.filmakademie.de
Authorization: Bearer eki_<api_key>
Content-Type: application/json

{
  "script_content": "<base64-kodierter Dateiinhalt>",
  "script_format": "fdx",
  "project_id": "filmprojekt-2026-042",
  "delivery": "pull",
  "idempotency_key": "upload-2026-042-v3",
  "metadata": {
    "uploaded_by": "disposition",
    "version": "3"
  }
}
```

### Option B: Multipart File Upload

```http
POST /v1/security/check:async HTTP/1.1
Host: eki.filmakademie.de
Authorization: Bearer eki_<api_key>
Content-Type: multipart/form-data

file: <Drehbuch-Datei (.fdx oder .pdf)>
project_id: filmprojekt-2026-042
delivery: pull
```

### Request-Felder

| Feld | Pflicht | Typ | Beschreibung |
|------|---------|-----|--------------|
| script_content | Ja (JSON) | string | Base64-kodierter Dateiinhalt |
| file | Ja (Multipart) | binary | Drehbuch-Datei (.fdx oder .pdf) |
| script_format | Ja (JSON) | "fdx" / "pdf" | Format des Drehbuchs; bei Multipart optional (sonst aus der Dateiendung) |
| project_id | Ja (JSON); bei Multipart optional | string | eProjekt Projekt-ID (poAllgemeinId, alphanumerisch, max 100 Zeichen). Fehlt sie im Multipart-Upload, speichert die eKI `unknown` und pusht an `.../set-risk-assessment/unknown` -- fuer Push daher immer angeben |
| script_id | Nein | int (32 Bit) | eProjekt Drehbuch-Dokument-ID. Wird bei Push an ePro zurueckgegeben, damit ePro das Assessment dem korrekten Dokument zuordnen kann. -1 wenn unbekannt. |
| delivery | Nein | "pull" / "push" | Delivery-Modus. Default "pull"; mit `callback_url` Default "push"; "pull" zusammen mit `callback_url` → 422. Gross-/Kleinschreibung egal, leer = Default, jeder andere Wert → 422 |
| idempotency_key | Nein | string (max 255) | Verhindert doppelte Jobs bei Retry (gilt pro API-Key-User); leer = kein Key, Steuerzeichen → 422. Derselbe Key liefert den bestehenden Job in **jedem** Status zurueck, auch `failed`/`cancelled` -- zum Neueinreichen einen neuen Key verwenden (siehe Idempotenz) |
| priority | Nein | int 1-10 | 1 (hoechste) ... 10 (niedrigste), Default 5; ausserhalb 1..10 → 422 (Multipart: als Text). Ordnet die Warteschlange des Dokument-Limits (siehe 3, Polling) und die Temporal-Task-Queue: Warten mehr Jobs als Verarbeitungsplaetze frei sind, werden hoeher priorisierte zuerst gestartet bzw. fortgesetzt; laufende Jobs werden nicht unterbrochen, gleiche Prioritaet bleibt in Eingangsreihenfolge (bei mehreren Task-Queue-Partitionen nur naeherungsweise) |
| callback_url | Nein | string (HTTPS, max. 2083 Zeichen) | Push-Ziel **dieses** Jobs anstelle von `EPRO_BASE_URL/eki/scl/set-risk-assessment/{project_id}`; die eKI haengt nichts an (vollstaendige Ziel-URL senden). Impliziert `delivery=push`. Nur HTTPS, keine Zugangsdaten in der URL, keine private/interne IP, Host muss in `CALLBACK_URL_ALLOWED_HOSTS` stehen (Standard `epro.filmakademie.de`, `staging.epro.filmakademie.de`), sonst 422. Details: 4b „Push an callback_url" |
| metadata | Nein (nur JSON; Multipart ignoriert das Feld) | object | Max. 50 Schluessel (`[A-Za-z0-9_-]`, max. 50 Zeichen), Werte Text bis 1000 Zeichen, Zahl, Wahrheitswert oder null. Wird nicht an ePro zurueckgegeben und nicht in der Datenbank gespeichert; laeuft durch die Workflow-Historie (seit 1.0.1 verschluesselt) -- trotzdem keine Drehbuchinhalte oder Personendaten hineinschreiben. |

Groessenlimits: Bei JSON gilt das Limit von 10 485 760 Zeichen fuer den
**Base64-String**, also Dateien bis ca. 7,5 MB (darueber 422). Per Multipart sind
Dateien bis `MAX_UPLOAD_SIZE_BYTES` (Standard 10 MB) moeglich (darueber 413).
Im Multipart-Pfad gelten fuer alle Felder dieselben Regeln wie bei JSON
(`project_id` z. B. `^[a-zA-Z0-9_-]+$`, max. 100 Zeichen; fehlt sie, speichert die
eKI `unknown`). Ein Textfeld, das als Datei-Teil gesendet wird, ergibt 422.

### Response (HTTP 202 Accepted)

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "Security check job started (delivery=pull)",
  "status_url": "/v1/security/jobs/550e8400-e29b-41d4-a716-446655440000",
  "estimated_completion_seconds": 120
}
```

**Merken:** Die job_id wird fuer alle weiteren Abfragen benoetigt.

### Idempotenz

Wenn idempotency_key gesetzt ist und fuer denselben API-Key-User bereits ein
Job mit diesem Key existiert, wird der bestehende Job zurueckgegeben (HTTP 202,
gleiche `job_id`, kein neuer Workflow) -- auch bei gleichzeitigen Requests.
Das ist wichtig fuer Retry-Szenarien, damit keine Duplikate entstehen. Der bestehende Job
kommt in **jedem** Status zurueck, auch `failed` oder `cancelled`: Wer nach einem Fehlschlag,
Abbruch oder verlorenem Report neu einreicht, braucht einen **neuen** Key (z. B. Dokument-ID +
Version + Versuchszaehler). Nur wenn ein Job gar nicht starten konnte (`503`), gibt die eKI den Key
wieder frei. Ein Key wirkt, solange der Job gespeichert ist: Seit 1.0.1 werden Job-Metadaten 90 Tage nach der letzten
Aenderung geloescht; danach startet derselbe Key einen neuen Job.

---

## 3. Job-Status abfragen (Polling)

```http
GET /v1/security/jobs/550e8400-e29b-41d4-a716-446655440000 HTTP/1.1
Host: eki.filmakademie.de
Authorization: Bearer eki_<api_key>
```

### Response

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "created_at": "2026-02-08T11:08:40.520195Z",
  "updated_at": "2026-02-08T11:12:06.039089Z",
  "progress_percentage": 100,
  "report_id": "7c84b9c2-493f-40ca-8cfc-8e8535e6c1c2",
  "error_message": null,
  "metadata": {
    "delivery_mode": "pull",
    "delivery_status": "pending",
    "delivery_attempts": 1,
    "delivery_last_status_code": null,
    "delivered_at": "2026-02-08T11:12:06.039089Z"
  }
}
```

Im Pull-Modus bedeutet das: Report liegt bereit (`delivery_attempts=1`,
`delivered_at` = Bereitstellungszeit), ist aber noch nicht abgeholt
(`delivery_status=pending`). Nach dem One-Shot-GET steht `delivery_status=delivered`
und `delivered_at` auf dem Abrufzeitpunkt.

### Status-Werte

| Status | Bedeutung | Naechster Schritt |
|--------|-----------|-------------------|
| pending | Job angenommen, Verarbeitung steht aus -- auch, solange der Job auf einen freien Verarbeitungsplatz wartet (Dokument-Limit, siehe unten) | Weiter pollen |
| running | Verarbeitung laeuft | Weiter pollen |
| delivering | Report erzeugt, Zustellung an ePro laeuft (Push; im Pull-Modus nur kurz, bis der Report bereitliegt) | Weiter pollen |
| completed | Fertig, report_id vorhanden | Report abholen (Pull) bzw. bereits zugestellt (Push) |
| failed | Fehlgeschlagen, error_message vorhanden | **Keine automatische Bewertung verfuegbar -- manuelle Pruefung.** Siehe unten. |
| cancelled | Job wurde vom Betrieb abgebrochen (Workflow-Cancel) | Check bei Bedarf neu ausloesen |

### Wenn ein Job `failed` ist

`failed` bedeutet immer: **Fuer dieses Drehbuch liegt keine automatische
Sicherheitsbewertung vor.** ePro darf das nie als "kein Risiko" darstellen,
sondern muss eine manuelle Pruefung anstossen oder den Check erneut ausloesen.

Die Verarbeitung arbeitet fail-closed: Scheitert das Parsen, die KI-Analyse einer
Szene dauerhaft (LLM nicht erreichbar, Timeout, unbrauchbare Antwort) oder
blockiert der Prompt-Injection-Filter eine Szene, endet der Job mit `failed`, statt
die Szene stillschweigend ohne Findings zu bewerten. Alle zwischengespeicherten
Inhalte werden dabei sofort geloescht. `error_message` ist inhaltsfrei, zum Beispiel:

```
Parsing fehlgeschlagen: PDF ist passwortgeschützt
Parsing fehlgeschlagen: keine Szene erkannt (kein lesbarer Text, z. B. nur Scan-Seiten ohne Texterkennung) – Drehbuch manuell prüfen
Szene 7: Risikoanalyse fehlgeschlagen (LLMException)
Kein freier Verarbeitungsplatz innerhalb von 120 min (Dokument-Limit 1, Warteposition 3) – Job bitte später neu einreichen
Szene <nr>: vom Prompt-Injection-Filter blockiert – manuelle Prüfung erforderlich
```

Der Prompt-Injection-Filter prueft auf gezielte, englischsprachige Muster, die sich
an das Modell selbst richten (z. B. "ignore previous instructions", Rollenwechsel
zu einer KI, Chat-Template-Token, eingeschleuste Szenen-Begrenzer), sowie auf Code- und
Steuerzeichenfolgen (drei Anfuehrungszeichen oder Backticks in Folge, `<script`, `javascript:`,
`eval(`, `exec(`). Deutschsprachige Formulierungen erkennt er nicht. Normale
Drehbuchsprache loest ihn in der Regel nicht aus; ein Treffer ist trotzdem ein
Fall fuer die manuelle Pruefung.

Ist `EPRO_WEBHOOK_URL` konfiguriert, meldet die eKI auch diese Verarbeitungsfehler
ueber den Webhook `security.delivery.failed` (siehe 4b, `reason` =
`prompt_blocked`, `parsing_failed` oder `processing_failed`, `attempts` = 0).

`error_message` mit Praefix `delivery_failed:` bedeutet dagegen: Die Analyse war
fertig, die Zustellung ist endgueltig gescheitert (siehe 4b).

### Zustell-Metadaten (M10)

`metadata.delivery_status` beschreibt den Lieferweg unabhaengig vom Job-Status:
`pending` (noch nicht zugestellt/abgeholt), `delivering` (Push-Versuche laufen),
`delivered` (Push 2xx bzw. One-Shot-GET erfolgt), `dead_lettered`
(endgueltig gescheitert, Inhalt geloescht, Webhook gesendet, sofern konfiguriert).
`failed` ist im Schema vorgesehen, wird derzeit aber nicht gesetzt.
`delivery_attempts` und `delivery_last_status_code` zeigen den letzten Push-Versuch
(im Pull-Modus `delivery_attempts=1` und kein Statuscode, sobald der Report bereitliegt).

### Empfohlenes Polling-Intervall

```
Erste 2 Minuten:  alle 10 Sekunden
Danach:           alle 30 Sekunden
Timeout:          fruehestens nach 4,5 Stunden abbrechen
```

Grossdokumente (300-350 Seiten) duerfen laut Pflichtenheft §5 bis zu 120 Minuten
Verarbeitung brauchen; ein frueherer Abbruch des Pollings verliert fertige Reports.

**Warteschlange (Dokument-Limit, seit 1.0.2):** Die eKI verarbeitet nur so viele Drehbuecher
gleichzeitig, wie `MAX_CONCURRENT_DOCUMENTS` erlaubt (Standard 1). Weitere Jobs bleiben `pending`,
bis ein Platz frei wird -- hoeher priorisierte (`priority`) zuerst, sonst in Eingangsreihenfolge
(genauer: Reihenfolge der ersten Nachfrage des Jobs nach einem Platz).
Ein Job wartet hoechstens `DOCUMENT_QUEUE_TIMEOUT_SECONDS` (Standard 2 h) und endet danach mit
`failed` und der Meldung `Kein freier Verarbeitungsplatz innerhalb von 120 min (Dokument-Limit 1,
Warteposition 3) – Job bitte später neu einreichen` (Webhook-Grund `processing_failed`). Daraus
ergibt sich der Polling-Timeout von 4,5 h: bis 2 h Wartezeit (harte Grenze), rund 2 h
Verarbeitung (Zielwert des Pflichtenhefts, keine harte Grenze -- ein spaeter fertiger Job liefert
trotzdem einen Report), Puffer. Ein Pull-Report bleibt nach Fertigstellung 6 Stunden abrufbar.
Alle Polls zaehlen gegen die Rate Limits (Abschnitt 8): Mit Warteschlange sind oft viele Jobs
gleichzeitig `pending`. Bei 30-s-Takt verbraucht jeder Job 120 Anfragen pro Stunde, das Limit von
1000 Anfragen/h je API-Key reicht also fuer rund 8 gleichzeitig gepollte Jobs. Wartende Jobs daher
seltener abfragen (z. B. alle 60-120 s).

### Wann ist der Report bereit?

Sobald status == "completed" und report_id nicht null ist (Pull-Modus).
Im Push-Modus bedeutet `completed`, dass ePro den Report bereits mit 2xx
angenommen hat -- dann **nicht** zusaetzlich per GET abholen.

---

## 4. Report abholen (One-Shot-GET)

**ACHTUNG: Der Report kann nur EINMAL abgeholt werden!**

```http
GET /v1/security/reports/7c84b9c2-493f-40ca-8cfc-8e8535e6c1c2 HTTP/1.1
Host: eki.filmakademie.de
Authorization: Bearer eki_<api_key>
```

### Response (HTTP 200)

```json
{
  "report": {
    "report_id": "7c84b9c2-493f-40ca-8cfc-8e8535e6c1c2",
    "project_id": "filmprojekt-2026-042",
    "script_format": "fdx",
    "created_at": "2026-02-08T11:12:05.846334",
    "risk_summary": {
      "critical": 3,
      "high": 5,
      "medium": 2,
      "low": 1,
      "info": 0
    },
    "total_findings": 11,
    "findings": [
      {
        "id": "eff159cf-...",
        "scene_number": "1",
        "risk_level": "critical",
        "category": "PHYSICAL",
        "risk_class": "HEIGHT",
        "rule_id": "SEC-P-006",
        "likelihood": 4,
        "impact": 5,
        "description": "Stunt performer jumping from 15-meter cliff...",
        "recommendation": "Safety harness, stunt coordinator, safety divers...",
        "measures": [
          {
            "code": "RIG-SAFETY",
            "title": "Rigging und Sicherungsseile",
            "responsible": "Stunt Coordination",
            "due": "shooting-3d"
          },
          {
            "code": "MEDICAL-STANDBY",
            "title": "Sanitaeter/Notarzt am Set",
            "responsible": "Production",
            "due": "shooting-1d"
          }
        ],
        "confidence": 0.9,
        "line_reference": null,
        "evidence": "Stunt performer jumps from the 15-meter cliff into the water.",
        "vulnerability": "stunt doubles",
        "complexity": "water + height, night shoot",
        "exposure_duration": "several hours"
      }
    ],
    "processing_time_seconds": 3445.2,
    "metadata": {
      "engine_version": "1.0.2",
      "taxonomy_version": "1.0",
      "analysis_complete": true,
      "coverage_notes": []
    }
  },
  "pdf_base64": "JVBERi0xLjQK...",
  "message": "Report retrieved successfully. URL is now invalidated."
}
```

### Was enthaelt die Response?

| Feld | Typ | Beschreibung |
|------|-----|--------------|
| report | object | Vollstaendiger JSON-Report (maschinenlesbar); `processing_time_seconds` = Sekunden von der Job-Annahme bis zur Report-Erzeugung |
| report.findings | array | Liste aller Risiko-Findings mit Severity, Klasse, Massnahmen |
| report.risk_summary | object | Aggregierte Zaehlung nach Severity-Level |
| report.metadata.analysis_complete | bool | `false`, wenn Teile des Drehbuchs nicht auf Risiken geprueft wurden (Seiten ueber `MAX_PDF_PAGES`, Seiten ohne lesbaren Text, Text vor der ersten Szenenueberschrift ab rund 1500 Zeichen, bei fehlenden Ueberschriften Seite 1). Fehlt in Reports aelterer Versionen |
| report.metadata.coverage_notes | array of string | Deutsche, inhaltsfreie Hinweise dazu (nur Seitenzahlen und Mengen), dieselben wie im PDF unter "Hinweise zur Vollstaendigkeit"; leer, wenn alles geprueft wurde |
| pdf_base64 | string / null | Base64-kodiertes PDF (menschenlesbar, zum Anzeigen/Download); null, wenn die PDF-Erzeugung fehlschlug |
| message | string | Status-Nachricht |

### PDF dekodieren und speichern (PHP)

```php
$response = json_decode($httpResponse, true);
$pdfBytes = base64_decode($response['pdf_base64']);
file_put_contents('/pfad/zum/report.pdf', $pdfBytes);
```

### Zweiter Abruf-Versuch

```http
GET /v1/security/reports/7c84b9c2-... HTTP/1.1
Authorization: Bearer eki_<api_key>
```

```
HTTP/1.1 410 Gone
X-Request-ID: 3f2c9e0a8b7d4c1e9f0a1b2c3d4e5f60

{
  "error": "Gone",
  "message": "Report already retrieved. URL is no longer valid.",
  "details": [],
  "request_id": "3f2c9e0a8b7d4c1e9f0a1b2c3d4e5f60",
  "timestamp": "2026-02-08T11:20:00.123456"
}
```

Der Report ist nach dem ersten Abruf unwiderruflich geloescht.
Bei Verlust muss ein neuer Job gestartet werden.

Die erfolgreiche Antwort traegt den Header `X-One-Shot: true`.

### Report-Verfuegbarkeit

- Reports sind maximal **6 Stunden** nach Fertigstellung abrufbar
  (`BUFFER_TTL_SECONDS`, Standard 21 600 s)
- Danach werden sie automatisch geloescht (TTL); die eKI markiert den Job als
  `failed` mit `delivery_failed:pull_ttl_expired` und sendet den Webhook (siehe 4b)
- Reports sind waehrend der Aufbewahrung verschluesselt gespeichert (Fernet/AES)

### Abgelaufener oder bereits zugestellter Report

Ist der Report nicht mehr im Buffer -- TTL abgelaufen, im Push-Modus bereits an
ePro uebertragen oder nicht lesbar --, antwortet der GET mit **410** und der
Meldung `Report is no longer available (expired or already delivered). URL is no
longer valid.` Es wird kein Report geliefert und nichts als zugestellt markiert;
es liegt **keine** Bewertung vor (Check neu ausloesen bzw. manuelle Pruefung).

Ist der Buffer (Redis) beim Abruf nicht erreichbar, antwortet die eKI mit **503**;
der Report bleibt in diesem Fall abrufbar -- spaeter erneut versuchen.

---

## 4b. Push-Modus (Alternative zu Pull)

Wenn `delivery=push` (oder ein `callback_url`) beim Einreichen gesetzt wird, sendet eKI
den Report nach Fertigstellung **automatisch** per `POST` an die eProjekt-API.
Ein manuelles Abholen (Schritt 4) entfaellt -- ein GET nach erfolgreichem Push
liefert 410 (siehe 4).

### Ablauf

```
1. ePro sendet Drehbuch        POST /v1/security/check:async  (delivery=push, script_id=...)
2. ePro fragt Job-Status ab    GET  /v1/security/jobs/{job_id} (Polling)
3. Job ist fertig -> eKI pusht automatisch zu ePro
4. ePro empfaengt das Assessment via eigene API
```

### Was eKI an ePro sendet

eKI ruft folgende ePro-Schnittstelle auf (Basis-URL = `EPRO_BASE_URL` der
eKI-Konfiguration, z. B. `https://staging.epro.filmakademie.de/api`). Enthielt der
Request ein `callback_url`, geht der Push stattdessen an genau diese URL (siehe
„Push an callback_url" unten) -- mit demselben Inhalt, denselben Headern und demselben Token.

```
POST {EPRO_BASE_URL}/eki/scl/set-risk-assessment/{project_id}
Content-Type: multipart/form-data
```

| Feld | Typ | Beschreibung |
|------|-----|--------------|
| script_id | int | Drehbuch-Dokument-ID aus dem urspruenglichen Request (-1 wenn unbekannt) |
| status | int | 0 = kein hohes Risiko gefunden, 1 = hohe/kritische Risiken vorhanden |
| assessment | string | Textuelle Zusammenfassung aller Findings |
| file | PDF | Report als PDF-Datei `safety-check-result_{project_id}.pdf`. Das 7-MB-Limit setzt nur ePro durch; die eKI prueft die Groesse nicht -- ein 413 von ePro fuehrt zum Hard-Fail. Fehlt das PDF (Erzeugung fehlgeschlagen), wird ohne `file` gesendet. |

Zusaetzlich sendet eKI seit M10 folgende HTTP-Header, damit ePro wiederholte
Pushes (Temporal-Retry nach Timeout) als Duplikat erkennen kann
(Pflichtenheft Abnahmetest 5):

| Header | Inhalt |
|--------|--------|
| `Idempotency-Key` | `report_id` -- pro Job stabil ueber alle Retries |
| `X-EKI-Job-Id` | `job_id` |
| `X-EKI-Attempt` | laufende Nummer des Zustellversuchs (1, 2, ...) |
| `X-Request-ID` | Korrelations-ID des urspruenglichen Einreichungs-Requests (vom Client gesendet oder von der eKI erzeugt) |

Empfehlung fuer ePro: `Idempotency-Key` persistieren und bei erneutem Eingang
mit demselben Key `200 OK` ohne zweite Speicherung antworten.

### Autorisierung

Die Autorisierung beim Push zu ePro erfolgt per **IP-Whitelist**: Der
eKI-Server muss von der freigeschalteten IP zugreifen. Ist in der eKI
`EPRO_AUTH_TOKEN` gesetzt, sendet sie zusaetzlich `Authorization: Bearer <Token>`
(Push, auch an ein `callback_url`, und Webhook); Standard ist ohne Token.

### Push an callback_url (seit 1.0.1)

Ein `callback_url` im Request ersetzt fuer diesen Job das Standard-Ziel. Fuer ePro gilt:

- Der Callback-Endpunkt muss denselben Multipart-Vertrag annehmen wie
  `set-risk-assessment` (Felder `script_id`, `status`, `assessment`, `file`; Header
  `Idempotency-Key`, `X-EKI-Job-Id`, `X-EKI-Attempt`, `X-Request-ID`, ggf. `Authorization`).
  Es gelten dasselbe Retry-Fenster (6 h) und dieselben Regeln fuer Statuscodes (siehe
  Fehlerverhalten). Weiterleitungen (3xx) folgt die eKI nicht; sie gelten als voruebergehender Fehler.
- Der Host muss in `CALLBACK_URL_ALLOWED_HOSTS` der eKI-Konfiguration stehen (Standard
  `epro.filmakademie.de`, `staging.epro.filmakademie.de`; exakter Hostname, keine
  Subdomains, beliebiger Port), muss auf **oeffentliche** IP-Adressen aufloesen und ein gueltiges
  oeffentliches TLS-Zertifikat fuer den Hostnamen vorweisen.
- **Erneute Pruefung vor jedem Push:** Unmittelbar vor jedem Versuch prueft der Worker die URL
  erneut gegen die dann gueltige Allow-List, loest den Host per DNS auf (jede Antwort muss eine
  oeffentliche Adresse sein) und verbindet sich genau mit der geprueften Adresse (Host-Header,
  SNI und Zertifikatspruefung nutzen weiter den Hostnamen). So kann ein geaenderter DNS-Eintrag
  den Push samt Token nicht auf einen internen Dienst umlenken.
- Verletzt die URL dabei die Regeln (Host nicht mehr freigegeben, kein HTTPS, Zugangsdaten,
  Aufloesung auf eine private/interne Adresse), wird **nichts gesendet** und nicht wiederholt:
  Job `failed` mit `delivery_failed:invalid_callback_url`, Dead Letter und Webhook mit
  `reason: "invalid_callback_url"`, `attempts: 0`. Ein voruebergehender DNS-Fehler (z. B.
  Timeout) wird dagegen innerhalb des 6-h-Fensters wiederholt.
- Der Webhook `security.delivery.failed` geht weiterhin an `EPRO_WEBHOOK_URL`, nicht an das
  `callback_url`.
- `callback_url` wird nicht in der Datenbank gespeichert und erscheint nicht im Job-Status.

### Status-Mapping

| eKI-Findings | ePro status |
|--------------|-------------|
| Mindestens 1 Finding mit risk_level critical oder high | 1 |
| Keine critical/high Findings | 0 |

Scheitert die Analyse selbst (Job `failed`, siehe 3), wird **nichts** gepusht --
`status=0` bedeutet also eine durchgelaufene Analyse ohne hohe/kritische Findings. Den
Abbruch erkennt ePro am Job-Status; erkennt die eKI gar keine Szene (z. B. PDF ohne lesbaren
Text), endet der Job ebenfalls mit `failed` (`parsing_failed`). Grenze: Teile, die die eKI nicht
prueft (Seiten ueber 500, Seiten ohne lesbaren Text, laengerer Text vor der ersten
Szenenueberschrift, bei fehlenden Ueberschriften Seite 1), lassen den Job nicht scheitern; der Report
nennt sie unter "Hinweise zur Vollstaendigkeit" (PDF), in der Kurzbewertung (`WARNING: Analysis
incomplete ...`) und in `report.metadata` (`analysis_complete: false`, `coverage_notes`).
`status=0` mit unvollstaendiger Analyse heisst also nur "im gepruften Teil nichts Kritisches". Den
Status gibt es nur im Push; im Pull-Modus leitet ePro ihn selbst aus `risk_summary` ab
(`critical` + `high` > 0 → 1).

### Fehlerverhalten

| ePro-Antwort | Verhalten der eKI |
|--------------|-------------------|
| 2xx | Zustellung abgeschlossen, Report wird sofort aus dem Buffer geloescht |
| 3xx (Weiterleitungen werden nicht verfolgt), 5xx, 408, 425, 429, Netzwerkfehler | Automatischer Retry mit exponentiellem Backoff (2 s ... 10 min) bis zu 6 Stunden |
| sonstige 4xx | Kein Retry (Hard-Fail): Loeschung, Job `failed`, Dead Letter, Webhook |
| 6 h ohne Erfolg | Loeschung, Job `failed` (`delivery_failed:retry_window_exhausted`), Dead Letter, Webhook |
| (vor dem Senden) Report nicht mehr im Buffer, z. B. nach einem One-Shot-GET | Kein Retry: Job `failed` (`delivery_failed:report_expired`), Dead Letter, Webhook |
| (vor dem Senden) `callback_url` verletzt die Regeln (siehe oben) | Kein Request, kein Retry: Loeschung, Job `failed` (`delivery_failed:invalid_callback_url`), Dead Letter, Webhook (`attempts: 0`) |
| (vor dem Senden) DNS-Aufloesung des `callback_url`-Hosts scheitert voruebergehend | Retry wie bei Netzwerkfehlern, bis zu 6 Stunden |

Waehrend des Retry-Fensters bleibt der Report verschluesselt im Buffer.
Ein One-Shot-GET ist in dieser Zeit technisch moeglich, beendet die Zustellung
aber als Fehler: Der naechste Push-Versuch findet den Report nicht mehr, der Job
endet mit `delivery_failed:report_expired` (Dead Letter, Webhook), obwohl der
Report abgeholt wurde. Deshalb im Push-Modus nicht per GET abholen. Nach
endgueltigem Scheitern existiert kein Inhalt mehr in der eKI; ePro muss den Check
erneut ausloesen. Der Metadaten-Webhook `security.delivery.failed` (siehe OpenAPI
`webhooks`) informiert darueber, sofern `EPRO_WEBHOOK_URL` konfiguriert ist:
JSON-Body mit `job_id`, `report_id`, `reason` und `attempts`. Gruende bei der
Zustellung: `retry_window_exhausted`, `hard_4xx`, `pull_ttl_expired`,
`report_expired`, `invalid_project_id`, `invalid_callback_url`, `delivery_failed`; bei Verarbeitungsfehlern
(kein Report entstanden, siehe 3): `prompt_blocked`, `parsing_failed`,
`processing_failed`. Bei Nicht-2xx-Antwort versucht die eKI den Webhook insgesamt
hoechstens dreimal.

Im Pull-Modus gilt dieselbe 6-h-Frist: Wird der Report nicht per One-Shot-GET
abgeholt, wird er geloescht und `delivery_failed:pull_ttl_expired` gemeldet.

### Beispiel-Request (wie ePro ihn empfaengt)

```bash
curl -X POST https://staging.epro.filmakademie.de/api/eki/scl/set-risk-assessment/75 \
  -F "status=1" \
  -F "script_id=123" \
  -F 'assessment="eKI Safety Risk Assessment — Project 75 ..."' \
  -F "file=@/path/to/safety-check-result_75.pdf"
```

### Beispiel-Response von ePro

```json
{"status": true, "message": "Risk assessment persisted (4711)"}
```

---

## 5. Finding-Felder im Detail

Jedes Finding im Report enthaelt:

| Feld | Typ | Beispiel | Beschreibung |
|------|-----|---------|--------------|
| id | UUID | "eff159cf-..." | Eindeutige Finding-ID |
| scene_number | string | "3" | FDX: Szenennummer aus Final Draft; ist das Drehbuch nicht nummeriert, die Position der Szene (1, 2, …; der Report weist darauf hin). PDF: laufende Nummer der erkannten Szene, nicht die gedruckte |
| risk_level | enum | "critical" | Berechnete Severity (critical/high/medium/low/info) |
| category | enum | "PHYSICAL" | Hauptkategorie (PHYSICAL/ENVIRONMENTAL/PSYCHOLOGICAL) |
| risk_class | string | "FIRE" | Risiko-Klasse aus Taxonomie (23 moegliche Werte) |
| rule_id | string | "SEC-P-008" | Taxonomie Rule-ID |
| likelihood | int 0-5 | 4 | Eintrittswahrscheinlichkeit (0 = nicht bewertet) |
| impact | int 0-5 | 5 | Schwere der Auswirkung (0 = nicht bewertet) |
| description | string | "Building fire..." | Beschreibung des Risikos |
| recommendation | string | "Fire dept on standby" | Konkrete Massnahmenempfehlung |
| measures | array | siehe unten | Kodifizierte Massnahmen aus dem Katalog |
| confidence | float 0-1 | 0.9 | Selbsteinschaetzung des Modells, keine Messung; fehlt sie (oder ist 0, negativ oder keine Zahl), setzt die eKI 0.8, Werte ueber 1 liest sie als Prozent (85 → 0.85, hoechstens 1.0). Nur im JSON, nicht im PDF und nicht im Push |
| line_reference | string / null | null | derzeit immer null |
| evidence | string | "Flames engulf the hallway." | Textbeleg (soll ein Zitat aus der Szene sein; die eKI prueft das nicht) oder Regelbegruendung |
| vulnerability | string | "children" | Verletzlichkeitsfaktoren |
| complexity | string | "extras, night shoot" | Komplexitaetsfaktoren |
| exposure_duration | string | "several hours" | Geschaetzte Expositionsdauer |

### Massnahmen-Objekte

```json
{
  "code": "RIG-SAFETY",
  "title": "Rigging und Sicherungsseile",
  "responsible": "Stunt Coordination",
  "due": "shooting-3d"
}
```

due-Format: "shooting-Xd" = X Tage vor Drehtag, "pre-production" = in der Vorbereitung

---

## 6. Fehlerbehandlung

### HTTP-Statuscodes

Alle Fehler kommen im einheitlichen Format `ErrorResponse`:

```json
{
  "error": "ServiceUnavailable",
  "message": "Database service is temporarily unavailable. Please try again later.",
  "details": [],
  "request_id": "3f2c9e0a8b7d4c1e9f0a1b2c3d4e5f60",
  "timestamp": "2026-10-15T10:00:00.000000"
}
```

`request_id` ist immer gesetzt und identisch mit dem Response-Header
`X-Request-ID` (vom Client uebernommen oder von der eKI erzeugt). Bei
Support-Anfragen bitte immer die `request_id` angeben.

| Code | `error` | Bedeutung | Aktion |
|------|---------|-----------|--------|
| 200 | -- | Erfolg | Daten verarbeiten |
| 202 | -- | Job angenommen | Polling starten |
| 400 | `BadRequest` | Ungueltige Anfrage: Body ist kein gueltiges JSON (Syntax, leer, nicht UTF-8, form-urlencoded); beim Multipart-Upload ausserdem: ohne `file`, leere Datei, falsche Dateiendung, kein gueltiges PDF (dieselben Fehler im JSON-Weg → 422) | Request pruefen |
| 401 | `Unauthorized` | Nicht authentifiziert (Key fehlt, ungueltig, abgelaufen, deaktiviert) | API-Key pruefen |
| 403 | `Forbidden` | `X-Actor-User-Id` passt nicht zur `user_id` des API-Keys; bzw. `/v1/kb/*` oder `/v1/ops/*` mit einem Nicht-Admin-Key (`Admin API key required for this endpoint`) | Header korrigieren oder weglassen; KB/Ops sind nicht fuer den ePro-Key vorgesehen |
| 404 | `NotFound` | Nicht gefunden / Kein Zugriff (fremder Job/Report); auch der in 1.0.1 entfernte Endpunkt `POST /v1/security/check` | Job-ID/Report-ID pruefen; `check:async` verwenden |
| 410 | `Gone` | Report bereits abgeholt | Report war schon einmal abgerufen |
| 413 | `PayloadTooLarge` | Multipart-Datei groesser als 10 MB | Datei verkleinern |
| 422 | `ValidationError` | Validierungsfehler: JSON ist kein Objekt, Base64, `project_id`-Format, `delivery`, `script_format`, `script_id` ausserhalb 32 Bit, Steuerzeichen im `idempotency_key`, `priority` ausserhalb 1..10, `callback_url` nicht erlaubt (kein HTTPS, Host nicht in der Allow-List, Zugangsdaten in der URL, private IP), `delivery=pull` zusammen mit `callback_url`, Base64-String zu lang, leerer Inhalt oder Inhalt passt nicht zum Format (kein PDF, FDX mit Binaerdaten) im JSON-Weg, Textfeld als Datei-Teil | Request-Format pruefen |
| 429 | `RateLimitExceeded` | Rate Limit ueberschritten | `Retry-After` abwarten |
| 500 | `InternalServerError` | Unerwarteter Serverfehler | mit `request_id` an den Support |
| 503 | `ServiceUnavailable` / `ServiceUnavailableException` | PostgreSQL, Redis oder Temporal voruebergehend nicht erreichbar | spaeter erneut senden (siehe Hinweis unten) |

Auch unbekannte Pfade (404) und falsche Methoden (405) antworten im Format
`ErrorResponse`.

**503 beim Einreichen und Idempotenz:** Je nach Ausfall kann die eKI den Job bereits
angelegt haben, bevor der Fehler auftrat. Ein erneuter Aufruf mit demselben
`idempotency_key` liefert dann diesen Job zurueck (202 mit bestehender `job_id`) --
bei nicht erreichbarer Workflow-Engine im Status `failed`, bei nicht erreichbarem
Redis dauerhaft im Status `pending`. In diesen beiden Faellen mit einem **neuen**
`idempotency_key` erneut einreichen.

### Retry-Strategie

```
Bei 429 (Rate Limit):  Retry-After Header beachten
Bei 500 (Server):      3x Retry mit Backoff (2s, 4s, 8s)
Bei 502/503:           5x Retry mit Backoff (5s, 10s, 20s, 40s, 60s)
Immer:                 idempotency_key setzen fuer sichere Retries
```

---

## 7. Komplettes PHP-Beispiel (eProjekt)

```php
// eKI API Client fuer eProjekt
class EkiClient {
    private $baseUrl;
    private $apiKey;

    public function __construct($baseUrl, $apiKey) {
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->apiKey = $apiKey;
    }

    // Drehbuch einreichen
    public function submitScript($filePath, $projectId) {
        $content = file_get_contents($filePath);
        $ext = strtolower(pathinfo($filePath, PATHINFO_EXTENSION));

        $payload = json_encode([
            'script_content'  => base64_encode($content),
            'script_format'   => $ext,
            'project_id'      => $projectId,
            'delivery'        => 'pull',
            'idempotency_key' => $projectId . '-' . md5($content),
        ]);

        return $this->post('/v1/security/check:async', $payload);
    }

    // Job-Status abfragen
    public function getJobStatus($jobId) {
        return $this->get("/v1/security/jobs/{$jobId}");
    }

    // Report abholen (One-Shot!)
    public function getReport($reportId) {
        return $this->get("/v1/security/reports/{$reportId}");
    }

    // Warten und Report abholen (Pull-Modus)
    // Timeout >= 4,5 h: bis 2 h Warteschlange + bis 120 min Verarbeitung.
    public function waitAndGetReport($jobId, $timeoutSec = 16200) {
        $start = time();
        $interval = 10;

        while (time() - $start < $timeoutSec) {
            $status = $this->getJobStatus($jobId);

            if ($status['status'] === 'completed' && !empty($status['report_id'])) {
                // 410 = bereits abgeholt oder nicht mehr verfuegbar -> keine Bewertung
                return $this->getReport($status['report_id']);
            }
            if ($status['status'] === 'failed' || $status['status'] === 'cancelled') {
                // Keine automatische Bewertung -> manuelle Pruefung, nie "kein Risiko"
                throw new Exception('eKI Job failed: ' . $status['error_message']);
            }

            sleep($interval);
            if (time() - $start > 120) $interval = 30;
        }
        throw new Exception('eKI Job timeout');
    }

    private function get($path) {
        $ch = curl_init($this->baseUrl . $path);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_HTTPHEADER, [
            'Authorization: Bearer ' . $this->apiKey,
        ]);
        $resp = curl_exec($ch);
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($code >= 400) throw new Exception("eKI Error {$code}: {$resp}");
        return json_decode($resp, true);
    }

    private function post($path, $body) {
        $ch = curl_init($this->baseUrl . $path);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_POST, true);
        curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
        curl_setopt($ch, CURLOPT_HTTPHEADER, [
            'Authorization: Bearer ' . $this->apiKey,
            'Content-Type: application/json',
        ]);
        $resp = curl_exec($ch);
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($code >= 400) throw new Exception("eKI Error {$code}: {$resp}");
        return json_decode($resp, true);
    }
}

// --- Verwendung ---

$eki = new EkiClient('https://eki.filmakademie.de', 'eki_<api_key>');

// Beim Drehbuch-Upload in eProjekt:
$job = $eki->submitScript('/path/to/drehbuch.fdx', 'projekt-2026-042');
$jobId = $job['job_id'];

// Spaeter (Cronjob oder Hintergrund-Task):
$report = $eki->waitAndGetReport($jobId);

// JSON-Findings in eProjekt-DB speichern
$findings = $report['report']['findings'];
$riskSummary = $report['report']['risk_summary'];

// PDF speichern
$pdf = base64_decode($report['pdf_base64']);
file_put_contents("/reports/{$jobId}.pdf", $pdf);
```

---

## 8. Rate Limits

| Limit | Wert (Standard) | Beschreibung |
|-------|------|--------------|
| IP-basiert | 60 Requests/Minute **je Quell-IP** | gilt fuer alle Endpunkte zusammen (Einreichen, Polling, Abruf) |
| API-Key-basiert | 1000 Requests/Stunde je API-Key | Abuse-Prevention |

Bei Ueberschreitung: HTTP 429 mit Retry-After Header.

Da alle Anfragen des ePro-Servers von derselben IP kommen, teilen sich alle
parallel gepollten Jobs das Minutenbudget: Zehn Jobs, die alle 10 Sekunden
gepollt werden, erreichen das Limit bereits. Polling-Intervall an die Zahl
paralleler Jobs anpassen oder die Limits in der eKI-Konfiguration
(`RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_HOUR`) erhoehen lassen. Steht vor der
eKI ein Reverse-Proxy, muss er mit `TRUST_PROXY_HEADERS=true` und
`TRUSTED_PROXY_IPS` eingetragen sein -- sonst teilen sich alle Clients die IP
des Proxys.

---

## 9. Sicherheitshinweise

- **API-Keys** niemals im Klartext in eProjekt-Quellcode committen.
  Umgebungsvariablen oder einen Secret Manager verwenden.
- **HTTPS-Only**: Alle API-Aufrufe muessen ueber TLS laufen.
- **Report sofort speichern**: Der Report ist nur einmal abrufbar.
  Sofort in der eProjekt-DB persistieren.
- **Idempotency-Key verwenden**: Bei Retries immer den gleichen Key senden,
  um Duplikate zu vermeiden.
- Keine Drehbuchinhalte werden in der eKI dauerhaft gespeichert:
  Drehbuch, Zwischenergebnisse, Findings und Report liegen nur verschluesselt im
  Buffer und werden nach Abholung/Zustellung bzw. bei einem Fehler sofort,
  spaetestens nach 6 Stunden geloescht. Metadaten (Job-/Report-IDs, `project_id`,
  Zeitstempel) bleiben 90 Tage erhalten und werden dann automatisch geloescht; ein
  `GET /v1/security/jobs/{job_id}` auf einen so alten Job liefert danach 404. Details:
  `docs/DATENSCHUTZ.md`.
- **`failed`, `cancelled` und 410 beim Abruf** nie als "kein Risiko" anzeigen --
  dann liegt keine automatische Bewertung vor.

---

## 10. Kontakt und Support

Bei technischen Fragen zur Integration:

**StefanAI -- Research & Development**
E-Mail: info@stefanai.de
