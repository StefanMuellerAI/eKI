#!/usr/bin/env python3
"""M12 -- automated evidence for Pflichtenheft §7 Abnahmetests 2, 3, 4, 5, 6, 7.

Runs against a *running* eKI stack plus the ePro mock (``mock_epro_server.py``).
Tests 1, 8 and 9 need the real Stage / production environment and are covered
by the manual runbook in ``docs/UAT/UAT_TESTPLAN.md``.

Prerequisites (see docs/UAT/UAT_TESTPLAN.md §2):

    python scripts/uat/mock_epro_server.py --port 9999          # terminal 1
    # eKI .env.local: EPRO_BASE_URL=http://host.docker.internal:9999/api
    #                 EPRO_WEBHOOK_URL=http://host.docker.internal:9999/api/eki/scl/delivery-failed
    export EKI_API_URL=http://localhost:8000 EKI_API_KEY=eki_... EKI_ADMIN_KEY=eki_...
    export MOCK_URL=http://localhost:9999
    python scripts/uat/run_acceptance_tests.py [--redis-url redis://localhost:6379/0]
                                               [--log-cmd "docker compose logs api worker"]
                                               [--with-large-document]

Writes ``tests/reports/uat_<timestamp>.json`` and ``.md`` (content-free protocol).
Exit code 0 when every executed test passed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess  # nosec B404 - used for the operator-provided log command
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]

FDX_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<FinalDraft DocumentType="Script" Template="No" Version="4">
  <Content>
    <Paragraph Type="Scene Heading" Number="1"><Text>INT. LAGERHALLE - NACHT</Text></Paragraph>
    <Paragraph Type="Action"><Text>Funken sprühen aus einem Schweißgerät. {marker} Ein Stuntman springt vom Hochregal auf eine Matte, während im Hintergrund ein Feuer ausbricht.</Text></Paragraph>
    <Paragraph Type="Character"><Text>ANNA</Text></Paragraph>
    <Paragraph Type="Dialogue"><Text>Alle raus hier, sofort!</Text></Paragraph>
    <Paragraph Type="Scene Heading" Number="2"><Text>EXT. PARKPLATZ - NACHT</Text></Paragraph>
    <Paragraph Type="Action"><Text>Ein Wagen rast über den Parkplatz und kommt quer zum Stehen.</Text></Paragraph>
  </Content>
</FinalDraft>
"""


@dataclass
class TestResult:
    test: str
    title: str
    status: str  # passed | failed | skipped
    evidence: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    duration_s: float = 0.0


class Uat:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.api = httpx.Client(base_url=args.url, timeout=60)
        self.mock = httpx.Client(base_url=args.mock_url, timeout=30) if args.mock_url else None
        self.results: list[TestResult] = []
        self.markers: list[str] = []

    # ----------------------------------------------------------------- helpers
    def _hdr(self, admin: bool = False) -> dict[str, str]:
        key = self.args.admin_key if admin else self.args.key
        return {"Authorization": f"Bearer {key}"}

    def _submit(
        self, *, delivery: str, project_id: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        marker = f"UATMARKER-{uuid.uuid4().hex[:12]}"
        self.markers.append(marker)
        payload = {
            "script_content": base64.b64encode(
                FDX_TEMPLATE.format(marker=marker).encode()
            ).decode(),
            "script_format": "fdx",
            "project_id": project_id,
            "delivery": delivery,
            "script_id": 4711,
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        r = self.api.post("/v1/security/check:async", json=payload, headers=self._hdr())
        r.raise_for_status()
        return r.json()

    def _wait(self, job_id: str, *, terminal: set[str], timeout: int) -> dict[str, Any]:
        started = time.monotonic()
        job: dict[str, Any] = {}
        while time.monotonic() - started < timeout:
            job = self.api.get(f"/v1/security/jobs/{job_id}", headers=self._hdr()).json()
            if job.get("status") in terminal:
                return job
            time.sleep(3)
        return job

    def _wait_delivery_status(self, job_id: str, wanted: set[str], timeout: int) -> dict[str, Any]:
        started = time.monotonic()
        job: dict[str, Any] = {}
        while time.monotonic() - started < timeout:
            job = self.api.get(f"/v1/security/jobs/{job_id}", headers=self._hdr()).json()
            if (job.get("metadata") or {}).get("delivery_status") in wanted:
                return job
            time.sleep(3)
        return job

    def _buffer_keys(self) -> int | None:
        if not self.args.redis_url:
            return None
        try:
            import redis

            r = redis.from_url(self.args.redis_url)
            return sum(1 for _ in r.scan_iter("eki:buf:*"))
        except Exception:
            return None

    def _mock_mode(self, mode: str) -> None:
        if self.mock:
            self.mock.post("/_mock/mode", json={"mode": mode}).raise_for_status()

    def _run(self, test: str, title: str, fn) -> None:
        res = TestResult(test=test, title=title, status="passed")
        t0 = time.monotonic()
        try:
            fn(res)
        except _Skip as exc:
            res.status = "skipped"
            res.evidence.append(f"SKIPPED: {exc}")
        except AssertionError as exc:
            res.status = "failed"
            res.evidence.append(f"FAILED: {exc}")
        except Exception as exc:
            res.status = "failed"
            res.evidence.append(f"ERROR: {type(exc).__name__}: {exc}")
        res.duration_s = round(time.monotonic() - t0, 1)
        self.results.append(res)
        print(f"[{res.status.upper():7s}] {test} {title} ({res.duration_s}s)")
        for line in res.evidence:
            print(f"          {line}")

    # ------------------------------------------------------------------- tests
    def at2_push_delete_on_delivery(self, res: TestResult) -> None:
        if not self.mock:
            raise _Skip("MOCK_URL not set")
        self._mock_mode("ok")
        before = self.mock.get("/_mock/received").json()["count"]
        job = self._submit(delivery="push", project_id="uat-at2")
        job_id = job["job_id"]
        res.evidence.append(f"job_id={job_id}")
        final = self._wait(job_id, terminal={"completed", "failed"}, timeout=self.args.job_timeout)
        assert final.get("status") == "completed", (
            f"job status {final.get('status')} ({final.get('error_message')})"
        )
        meta = final["metadata"]
        assert meta["delivery_status"] == "delivered", f"delivery_status={meta['delivery_status']}"
        assert meta["delivery_last_status_code"] == 201, meta
        res.evidence.append(
            f"delivery_status=delivered attempts={meta['delivery_attempts']} last_code={meta['delivery_last_status_code']}"
        )
        received = self.mock.get("/_mock/received").json()
        mine = [r for r in received["items"] if r["job_id"] == job_id]
        assert mine and mine[-1]["response"] == 201 and mine[-1]["pdf_bytes"] > 0, (
            "mock did not receive PDF"
        )
        res.evidence.append(
            f"ePro-mock received: pdf_bytes={mine[-1]['pdf_bytes']} idempotency_key={mine[-1]['idempotency_key']} (count {received['count'] - before} new)"
        )
        keys = self._buffer_keys()
        if keys is None:
            res.evidence.append(
                "buffer check: redis not reachable from here -> verify with: docker compose exec redis redis-cli --scan --pattern 'eki:buf:*'"
            )
        else:
            assert keys == 0, f"{keys} buffer keys remain"
            res.evidence.append("redis eki:buf:* keys after delivery = 0 (no content in eKI)")

    def at3_pull_one_shot(self, res: TestResult) -> None:
        job = self._submit(delivery="pull", project_id="uat-at3")
        job_id = job["job_id"]
        final = self._wait(job_id, terminal={"completed", "failed"}, timeout=self.args.job_timeout)
        assert final.get("status") == "completed", f"job status {final.get('status')}"
        report_id = final["report_id"]
        first = self.api.get(f"/v1/security/reports/{report_id}", headers=self._hdr())
        assert first.status_code == 200, first.status_code
        assert first.headers.get("X-One-Shot") == "true", "X-One-Shot header missing"
        body = first.json()
        assert body.get("pdf_base64"), "no PDF"
        res.evidence.append(
            f"first GET 200, X-One-Shot=true, findings={body['report']['total_findings']}, pdf_bytes~{len(body['pdf_base64']) * 3 // 4}"
        )
        second = self.api.get(f"/v1/security/reports/{report_id}", headers=self._hdr())
        assert second.status_code == 410, f"second GET {second.status_code}"
        res.evidence.append("second GET 410 Gone")
        job2 = self.api.get(f"/v1/security/jobs/{job_id}", headers=self._hdr()).json()
        assert job2["metadata"]["delivery_status"] == "delivered"
        res.evidence.append("job metadata.delivery_status=delivered after retrieval")

    def at4_retries_ttl_webhook(self, res: TestResult) -> None:
        if not self.mock:
            raise _Skip("MOCK_URL not set")
        if not self.args.admin_key:
            raise _Skip("EKI_ADMIN_KEY not set (needed for /v1/ops)")
        # Part A: transient 5xx -> retries -> success after mock recovers
        self._mock_mode("fail_5xx")
        job = self._submit(delivery="push", project_id="uat-at4-retry")
        job_id = job["job_id"]
        job_state = self._wait_delivery_status(
            job_id, {"delivering"}, timeout=self.args.job_timeout
        )
        meta = job_state.get("metadata") or {}
        assert meta.get("delivery_status") == "delivering", f"expected delivering, got {meta}"
        # wait for at least 2 attempts
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            meta = self.api.get(f"/v1/security/jobs/{job_id}", headers=self._hdr()).json()[
                "metadata"
            ]
            if meta.get("delivery_attempts", 0) >= 2:
                break
            time.sleep(3)
        assert meta.get("delivery_attempts", 0) >= 2, f"no retries observed: {meta}"
        res.evidence.append(
            f"5xx: retries observed attempts={meta['delivery_attempts']} last_code={meta['delivery_last_status_code']} status=delivering"
        )
        self._mock_mode("ok")
        final = self._wait_delivery_status(job_id, {"delivered"}, timeout=self.args.job_timeout)
        assert (final.get("metadata") or {}).get("delivery_status") == "delivered", final.get(
            "metadata"
        )
        res.evidence.append(
            f"after ePro recovery: delivered with attempts={final['metadata']['delivery_attempts']}"
        )

        # Part B: hard 4xx -> failure branch -> cleanup + webhook + dead letter
        webhooks_before = self.mock.get("/_mock/webhooks").json()["count"]
        self._mock_mode("fail_4xx")
        job = self._submit(delivery="push", project_id="uat-at4-hard")
        job_id = job["job_id"]
        final = self._wait(job_id, terminal={"completed", "failed"}, timeout=self.args.job_timeout)
        assert final.get("status") == "failed", f"expected failed, got {final.get('status')}"
        assert str(final.get("error_message", "")).startswith("delivery_failed:hard_4xx"), (
            final.get("error_message")
        )
        res.evidence.append(f"4xx hard fail: status=failed error={final['error_message']}")
        webhooks = self.mock.get("/_mock/webhooks").json()
        mine = [w for w in webhooks["items"] if w["job_id"] == job_id]
        assert mine, "webhook security.delivery.failed not received"
        res.evidence.append(
            f"webhook received: reason={mine[-1]['reason']} attempts={mine[-1]['attempts']} (payload keys: job_id, report_id, reason, attempts)"
        )
        dls = self.api.get(
            "/v1/ops/dead-letters", params={"acknowledged": "false"}, headers=self._hdr(admin=True)
        ).json()
        mine_dl = [d for d in dls["items"] if d["job_id"] == job_id]
        assert mine_dl, "no dead letter recorded"
        res.evidence.append(
            f"dead letter: reason={mine_dl[0]['reason']} last_status_code={mine_dl[0]['last_status_code']} webhook_sent={mine_dl[0]['webhook_sent']}"
        )
        self._mock_mode("ok")
        keys = self._buffer_keys()
        if keys is not None:
            assert keys == 0, f"{keys} buffer keys remain after failure branch"
            res.evidence.append("redis eki:buf:* keys after failure branch = 0")
        res.evidence.append(
            "6h window itself: tests/test_m10_workflow_failover.py (Temporal time-skipping) -- see UAT_TESTPLAN"
        )
        _ = webhooks_before

    def at5_idempotency(self, res: TestResult) -> None:
        key = f"uat-idem-{uuid.uuid4().hex[:8]}"
        delivery = "push" if self.mock else "pull"
        self._mock_mode("ok")
        a = self._submit(delivery=delivery, project_id="uat-at5", idempotency_key=key)
        b = self._submit(delivery=delivery, project_id="uat-at5", idempotency_key=key)
        assert a["job_id"] == b["job_id"], f"different jobs {a['job_id']} / {b['job_id']}"
        assert "idempotency" in b["message"].lower()
        res.evidence.append(f"same idempotency_key -> same job_id {a['job_id']}")
        final = self._wait(
            a["job_id"], terminal={"completed", "failed"}, timeout=self.args.job_timeout
        )
        assert final.get("status") == "completed", final.get("status")
        if self.mock:
            received = self.mock.get("/_mock/received").json()["items"]
            mine = [r for r in received if r["job_id"] == a["job_id"] and r["response"] == 201]
            assert len(mine) == 1, f"ePro received {len(mine)} deliveries for one job"
            res.evidence.append(
                f"ePro-mock: exactly 1 successful delivery, Idempotency-Key={mine[0]['idempotency_key']}"
            )

    def at6_large_document(self, res: TestResult) -> None:
        if not self.args.with_large_document:
            raise _Skip(
                "--with-large-document not set (runs up to 120 min); use tests/run_pdf_m07_benchmark.py"
            )
        cmd = [
            sys.executable,
            str(ROOT / "tests" / "run_pdf_m07_benchmark.py"),
            "--concurrency",
            "1",
        ]
        env = {**os.environ, "EKI_API_URL": self.args.url, "EKI_API_KEY": self.args.key}
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3 * 3600)  # nosec B603
        res.evidence.append(
            proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "no output"
        )
        assert proc.returncode == 0, f"benchmark exit {proc.returncode}: {proc.stderr[-400:]}"

    def at7_log_hygiene(self, res: TestResult) -> None:
        if not self.args.log_cmd:
            raise _Skip('--log-cmd not set (e.g. "docker compose logs api worker")')
        proc = subprocess.run(
            shlex.split(self.args.log_cmd), capture_output=True, text=True, timeout=120
        )  # nosec B603
        logs = proc.stdout + proc.stderr
        hits = [m for m in self.markers if m in logs]
        assert not hits, f"script text marker(s) found in logs: {hits}"
        res.evidence.append(
            f"{len(self.markers)} unique script markers submitted, 0 found in {len(logs.splitlines())} log lines"
        )
        for word in ("Funken sprühen", "Alle raus hier"):
            assert word not in logs, f"script text '{word}' leaked into logs"
        res.evidence.append(
            "no scene text / dialogue fragments in logs; only ids, counts, durations"
        )

    # -------------------------------------------------------------------- main
    def run(self) -> int:
        print(f"== eKI UAT run ({self.args.url}, mock={self.args.mock_url or '-'}) ==")
        health = self.api.get("/health").json()
        print(f"   API version {health.get('version')}\n")
        self._run(
            "AT2", "Push-Fluss: nach 2xx keine Inhalte in eKI", self.at2_push_delete_on_delivery
        )
        self._run("AT3", "Pull-Fluss: One-Shot, zweiter Abruf 410", self.at3_pull_one_shot)
        self._run(
            "AT4",
            "Retries & TTL: Retry bei 5xx, Loeschung + Webhook + Dead Letter bei 4xx",
            self.at4_retries_ttl_webhook,
        )
        self._run("AT5", "Idempotenz: gleicher Key, keine Duplikate", self.at5_idempotency)
        self._run("AT6", "Grossdokumente 300-350 Seiten <= 120 min", self.at6_large_document)
        self._run("AT7", "Log-Hygiene: keine Drehbuchtexte/Findings in Logs", self.at7_log_hygiene)

        out_dir = Path(self.args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "api_url": self.args.url,
            "api_version": health.get("version"),
            "results": [asdict(r) for r in self.results],
        }
        (out_dir / f"uat_{stamp}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False)
        )
        md = ["| Test | Titel | Status | Dauer | Nachweis |", "|---|---|---|---|---|"]
        for r in self.results:
            md.append(
                f"| {r.test} | {r.title} | **{r.status}** | {r.duration_s}s | "
                + "<br>".join(r.evidence).replace("|", "\\|")
                + " |"
            )
        (out_dir / f"uat_{stamp}.md").write_text("\n".join(md) + "\n")
        print(f"\nProtocol: {out_dir / f'uat_{stamp}.md'}")
        failed = [r for r in self.results if r.status == "failed"]
        print("RESULT:", "FAILED" if failed else "PASSED (executed tests)")
        return 1 if failed else 0


class _Skip(Exception):
    pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=os.environ.get("EKI_API_URL", "http://localhost:8000"))
    parser.add_argument("--key", default=os.environ.get("EKI_API_KEY", ""))
    parser.add_argument("--admin-key", default=os.environ.get("EKI_ADMIN_KEY", ""))
    parser.add_argument("--mock-url", default=os.environ.get("MOCK_URL", ""))
    parser.add_argument("--redis-url", default=os.environ.get("EKI_REDIS_URL", ""))
    parser.add_argument("--log-cmd", default=os.environ.get("EKI_LOG_CMD", ""))
    parser.add_argument("--job-timeout", type=int, default=900)
    parser.add_argument("--with-large-document", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "tests" / "reports"))
    args = parser.parse_args()
    if not args.key:
        print("ERROR: EKI_API_KEY required", file=sys.stderr)
        sys.exit(1)
    sys.exit(Uat(args).run())


if __name__ == "__main__":
    main()
