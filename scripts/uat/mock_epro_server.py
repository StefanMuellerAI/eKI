#!/usr/bin/env python3
"""M12 -- ePro mock for UAT runs (push delivery + delivery-failed webhook).

Stands in for the eProjekt endpoints the eKI calls, so Abnahmetests 2, 4 and 5
can be demonstrated without the real Stage:

    POST /api/eki/scl/set-risk-assessment/{project_id}   (multipart, like ePro)
    POST /api/eki/scl/delivery-failed                     (security.delivery.failed webhook)

Control plane (never exposed to eKI in a real deployment):

    GET  /_mock/received            deliveries received (metadata only)
    GET  /_mock/webhooks            webhooks received
    POST /_mock/mode  {"mode": "ok" | "fail_4xx" | "fail_5xx" | "flaky"}
    POST /_mock/reset

Run:

    python scripts/uat/mock_epro_server.py --port 9999
    # eKI stack: EPRO_BASE_URL=http://host.docker.internal:9999/api
    #            EPRO_WEBHOOK_URL=http://host.docker.internal:9999/api/eki/scl/delivery-failed

Stores nothing but metadata (project_id, status, script_id, PDF size, headers).
The assessment text and PDF bytes are discarded immediately (Log-Hygiene).
"""

from __future__ import annotations

import argparse
import itertools
import threading
from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse

app = FastAPI(title="ePro Mock", version="1.0.0", docs_url=None, redoc_url=None)

_state: dict[str, Any] = {
    "mode": "ok",
    "received": [],
    "webhooks": [],
    "counter": itertools.count(1),
    "flaky_calls": 0,
}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat()


@app.post("/api/eki/scl/set-risk-assessment/{project_id}")
async def set_risk_assessment(
    project_id: str,
    request: Request,
    script_id: str = Form("-1"),
    status: str = Form("0"),
    assessment: str = Form(""),
    file: UploadFile | None = File(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    eki_job_id: str | None = Header(None, alias="X-EKI-Job-Id"),
    eki_attempt: str | None = Header(None, alias="X-EKI-Attempt"),
    request_id: str | None = Header(None, alias="X-Request-ID"),
) -> JSONResponse:
    pdf_size = len(await file.read()) if file is not None else 0

    with _lock:
        mode = _state["mode"]
        record = {
            "seq": next(_state["counter"]),
            "received_at": _now(),
            "project_id": project_id,
            "script_id": script_id,
            "status": status,
            "assessment_chars": len(assessment),
            "pdf_bytes": pdf_size,
            "idempotency_key": idempotency_key,
            "job_id": eki_job_id,
            "attempt": eki_attempt,
            "request_id": request_id,
            "mode": mode,
        }
        if mode == "flaky":
            _state["flaky_calls"] += 1
            # fail the first two attempts, then succeed -> exercises the retry path
            if _state["flaky_calls"] <= 2:
                record["response"] = 503
                _state["received"].append(record)
                return JSONResponse({"status": False, "message": "mock flaky 503"}, status_code=503)
        if mode == "fail_5xx":
            record["response"] = 503
            _state["received"].append(record)
            return JSONResponse({"status": False, "message": "mock 503"}, status_code=503)
        if mode == "fail_4xx":
            record["response"] = 422
            _state["received"].append(record)
            return JSONResponse({"status": False, "message": "mock 422"}, status_code=422)

        # idempotent success: same key -> same persisted id
        existing = next(
            (
                r
                for r in _state["received"]
                if r.get("idempotency_key") == idempotency_key and r.get("response") == 201
            ),
            None,
        )
        record["response"] = 201
        record["duplicate_of"] = existing["seq"] if existing and idempotency_key else None
        _state["received"].append(record)
    return JSONResponse(
        {"status": True, "message": f"Risk assessment persisted ({record['seq']})"}, status_code=201
    )


@app.post("/api/eki/scl/delivery-failed")
async def delivery_failed(request: Request) -> JSONResponse:
    body = await request.json()
    with _lock:
        _state["webhooks"].append(
            {
                "received_at": _now(),
                **{k: body.get(k) for k in ("job_id", "report_id", "reason", "attempts")},
            }
        )
    return JSONResponse({"ok": True})


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": _state["mode"]}


@app.get("/_mock/received")
async def received() -> dict[str, Any]:
    with _lock:
        return {
            "mode": _state["mode"],
            "count": len(_state["received"]),
            "items": list(_state["received"]),
        }


@app.get("/_mock/webhooks")
async def webhooks() -> dict[str, Any]:
    with _lock:
        return {"count": len(_state["webhooks"]), "items": list(_state["webhooks"])}


@app.post("/_mock/mode")
async def set_mode(request: Request) -> dict[str, str]:
    body = await request.json()
    mode = str(body.get("mode", "ok"))
    if mode not in {"ok", "fail_4xx", "fail_5xx", "flaky"}:
        return JSONResponse({"error": "invalid mode"}, status_code=422)  # type: ignore[return-value]
    with _lock:
        _state["mode"] = mode
        _state["flaky_calls"] = 0
    return {"mode": mode}


@app.post("/_mock/reset")
async def reset() -> dict[str, str]:
    with _lock:
        _state["received"].clear()
        _state["webhooks"].clear()
        _state["mode"] = "ok"
        _state["flaky_calls"] = 0
    return {"status": "reset"}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="0.0.0.0")  # nosec B104 - test tool, bind for docker access
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
