# eProjekt-Integration: eKI API Anbindung

**Stand:** Februar 2026 | **API-Version:** 0.5.0 | **Zielgruppe:** eProjekt-Entwicklungsteam

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

---

## 1. Authentifizierung

Alle /v1/security/ Endpunkte erfordern einen API-Key als Bearer-Token.

```
Authorization: Bearer eki_<api_key>
```

### API-Key erhalten

Der API-Key wird vom eKI-Administrator erstellt und sicher uebergeben.
Der Key ist an einen user_id gebunden -- alle Jobs und Reports
sind diesem User zugeordnet (Ownership-Check).

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
| script_format | Ja | "fdx" / "pdf" | Format des Drehbuchs |
| project_id | Ja | string | eProjekt Projekt-ID (alphanumerisch, max 100 Zeichen) |
| delivery | Nein | "pull" / "push" | Delivery-Modus (Default: "pull") |
| idempotency_key | Nein | string | Verhindert doppelte Jobs bei Retry |
| metadata | Nein | object | Zusaetzliche Metadaten fuer Audit |

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

Wenn idempotency_key gesetzt ist und ein Job mit diesem Key bereits
existiert, wird der bestehende Job zurueckgegeben (kein neuer Workflow).
Das ist wichtig fuer Retry-Szenarien, damit keine Duplikate entstehen.

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
    "delivery_mode": "pull"
  }
}
```

### Status-Werte

| Status | Bedeutung | Naechster Schritt |
|--------|-----------|-------------------|
| pending | Job angenommen, Verarbeitung steht aus | Weiter pollen |
| running | Verarbeitung laeuft | Weiter pollen |
| completed | Fertig, report_id vorhanden | Report abholen |
| failed | Fehlgeschlagen, error_message vorhanden | Fehler pruefen |
| cancelled | Abgebrochen | -- |

### Empfohlenes Polling-Intervall

```
Erste 2 Minuten:  alle 10 Sekunden
Danach:           alle 30 Sekunden
Timeout:          nach 60 Minuten abbrechen
```

### Wann ist der Report bereit?

Sobald status == "completed" und report_id nicht null ist.

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
        "line_reference": null
      }
    ],
    "processing_time_seconds": 45.2,
    "metadata": {
      "engine_version": "0.5.0",
      "taxonomy_version": "1.0"
    }
  },
  "pdf_base64": "JVBERi0xLjQK...",
  "message": "Report retrieved successfully. URL is now invalidated."
}
```

### Was enthaelt die Response?

| Feld | Typ | Beschreibung |
|------|-----|--------------|
| report | object | Vollstaendiger JSON-Report (maschinenlesbar) |
| report.findings | array | Liste aller Risiko-Findings mit Severity, Klasse, Massnahmen |
| report.risk_summary | object | Aggregierte Zaehlung nach Severity-Level |
| pdf_base64 | string | Base64-kodiertes PDF (menschenlesbar, zum Anzeigen/Download) |
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

{ "detail": "Report already retrieved. URL is no longer valid." }
```

Der Report ist nach dem ersten Abruf unwiderruflich geloescht.
Bei Verlust muss ein neuer Job gestartet werden.

### Report-Verfuegbarkeit

- Reports sind maximal **6 Stunden** nach Fertigstellung abrufbar
- Danach werden sie automatisch geloescht (TTL)
- Reports sind waehrend der Aufbewahrung AES-verschluesselt gespeichert

---

## 5. Finding-Felder im Detail

Jedes Finding im Report enthaelt:

| Feld | Typ | Beispiel | Beschreibung |
|------|-----|---------|--------------|
| id | UUID | "eff159cf-..." | Eindeutige Finding-ID |
| scene_number | string | "3" | Szenennummer |
| risk_level | enum | "critical" | Berechnete Severity (critical/high/medium/low/info) |
| category | enum | "PHYSICAL" | Hauptkategorie (PHYSICAL/ENVIRONMENTAL/PSYCHOLOGICAL) |
| risk_class | string | "FIRE" | Risiko-Klasse aus Taxonomie (23 moegliche Werte) |
| rule_id | string | "SEC-P-008" | Taxonomie Rule-ID |
| likelihood | int 1-5 | 4 | Eintrittswahrscheinlichkeit |
| impact | int 1-5 | 5 | Schwere der Auswirkung |
| description | string | "Building fire..." | Beschreibung des Risikos |
| recommendation | string | "Fire dept on standby" | Konkrete Massnahmenempfehlung |
| measures | array | siehe unten | Kodifizierte Massnahmen aus dem Katalog |
| confidence | float 0-1 | 0.9 | Konfidenz der KI-Bewertung |

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

| Code | Bedeutung | Aktion |
|------|-----------|--------|
| 200 | Erfolg | Daten verarbeiten |
| 202 | Job angenommen | Polling starten |
| 400 | Ungueltige Anfrage | Request pruefen |
| 401 | Nicht authentifiziert | API-Key pruefen |
| 404 | Nicht gefunden / Kein Zugriff | Job-ID/Report-ID pruefen |
| 410 | Report bereits abgeholt | Report war schon einmal abgerufen |
| 413 | Datei zu gross | Max 10 MB |
| 422 | Validierungsfehler | Request-Format pruefen |
| 429 | Rate Limit ueberschritten | Warten, spaeter erneut versuchen |
| 500 | Serverfehler | Support kontaktieren |

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

    // Warten und Report abholen
    public function waitAndGetReport($jobId, $timeoutSec = 3600) {
        $start = time();
        $interval = 10;

        while (time() - $start < $timeoutSec) {
            $status = $this->getJobStatus($jobId);

            if ($status['status'] === 'completed' && !empty($status['report_id'])) {
                return $this->getReport($status['report_id']);
            }
            if ($status['status'] === 'failed') {
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

| Limit | Wert | Beschreibung |
|-------|------|--------------|
| IP-basiert | 60 Requests/Minute | DoS-Schutz |
| API-Key-basiert | 1000 Requests/Stunde | Abuse-Prevention |

Bei Ueberschreitung: HTTP 429 mit Retry-After Header.

---

## 9. Sicherheitshinweise

- **API-Keys** niemals im Klartext in eProjekt-Quellcode committen.
  Umgebungsvariablen oder einen Secret Manager verwenden.
- **HTTPS-Only**: Alle API-Aufrufe muessen ueber TLS laufen.
- **Report sofort speichern**: Der Report ist nur einmal abrufbar.
  Sofort in der eProjekt-DB persistieren.
- **Idempotency-Key verwenden**: Bei Retries immer den gleichen Key senden,
  um Duplikate zu vermeiden.
- Keine Drehbuchinhalte werden in der eKI dauerhaft gespeichert.
  Nach Abholung/Zustellung werden alle Daten geloescht.

---

## 10. Kontakt und Support

Bei technischen Fragen zur Integration:

**StefanAI -- Research & Development**
E-Mail: info@stefanai.de
