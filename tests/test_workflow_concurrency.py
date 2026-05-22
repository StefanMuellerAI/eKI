"""M07 – Tests für den optionalen Concurrency-Pfad im SecurityCheckWorkflow.

Die Workflow-Klasse läuft normalerweise im Temporal-Replay-Sandbox.
Die hier getesteten Bausteine -- die freie Funktion ``_resolve_concurrency``,
``_resolve_activity_timeout`` und die Methode ``_run_indexed`` -- sind aber
unabhängig von Temporal und können direkt instanziiert und gerufen werden.

Geprüft wird:

* ``_resolve_concurrency`` aktiviert Parallelität AUSSCHLIESSLICH dann, wenn
  ``llm_parallel_enabled=true`` UND der jeweilige Concurrency-Wert > 1 ist.
  Das ist die Stabilitätsgarantie für M06.
* ``_resolve_activity_timeout`` fällt auf 600s zurück, wenn job_data den
  neuen Schlüssel noch nicht enthält (Rückwärtskompatibilität mit alten
  Workflows, die noch ohne diese Felder gestartet wurden).
* ``_run_indexed`` liefert bei concurrency=1 strikt sequenzielles Verhalten
  mit indexgetreuer Ergebnisliste und monotonem Progress-Reporting.
* ``_run_indexed`` liefert bei concurrency=4 messbaren Speedup gegenüber
  concurrency=1, und die Ergebnisreihenfolge ist trotzdem indexgetreu.
"""

import asyncio
import time
from typing import Any
from datetime import timedelta

import pytest

from workflows.security_check import (
    SecurityCheckWorkflow,
    _resolve_activity_timeout,
    _resolve_concurrency,
)


# ---------------------------------------------------------------------------
# Gating: llm_parallel_enabled MUSS gesetzt sein, sonst immer 1
# ---------------------------------------------------------------------------


def test_resolve_concurrency_returns_1_when_flag_off() -> None:
    """Ohne llm_parallel_enabled bleibt das Verhalten M06-konform (=1)."""
    job_data: dict[str, Any] = {
        "pdf_structure_concurrency": 4,
        "risk_analysis_concurrency": 8,
    }
    assert _resolve_concurrency(job_data, "pdf_structure_concurrency") == 1
    assert _resolve_concurrency(job_data, "risk_analysis_concurrency") == 1


def test_resolve_concurrency_returns_1_for_legacy_job_data() -> None:
    """Workflows ohne die neuen Felder dürfen niemals den Parallelpfad nutzen."""
    legacy: dict[str, Any] = {"script_format": "pdf"}
    assert _resolve_concurrency(legacy, "pdf_structure_concurrency") == 1
    assert _resolve_concurrency(legacy, "risk_analysis_concurrency") == 1


def test_resolve_concurrency_returns_configured_value_when_flag_on() -> None:
    job_data: dict[str, Any] = {
        "llm_parallel_enabled": True,
        "pdf_structure_concurrency": 3,
        "risk_analysis_concurrency": 5,
    }
    assert _resolve_concurrency(job_data, "pdf_structure_concurrency") == 3
    assert _resolve_concurrency(job_data, "risk_analysis_concurrency") == 5


def test_resolve_concurrency_clamps_invalid_values() -> None:
    """0, negative oder unparsbare Werte fallen sicher auf 1 zurück."""
    cases: list[Any] = [0, -2, "abc", None]
    for v in cases:
        job_data = {
            "llm_parallel_enabled": True,
            "pdf_structure_concurrency": v,
        }
        assert _resolve_concurrency(job_data, "pdf_structure_concurrency") == 1, (
            f"Wert {v!r} ergab nicht 1"
        )


# ---------------------------------------------------------------------------
# Activity-Timeout aus job_data
# ---------------------------------------------------------------------------


def test_resolve_activity_timeout_default_600s_for_legacy_jobs() -> None:
    """Legacy-Workflows ohne llm_activity_timeout_seconds = 600s."""
    assert _resolve_activity_timeout({}) == timedelta(seconds=600)


def test_resolve_activity_timeout_uses_job_data_value() -> None:
    assert _resolve_activity_timeout(
        {"llm_activity_timeout_seconds": 1200}
    ) == timedelta(seconds=1200)


def test_resolve_activity_timeout_enforces_minimum_60s() -> None:
    """Sehr kleine Werte werden auf 60s gehoben (verhindert Selbst-DoS)."""
    assert _resolve_activity_timeout(
        {"llm_activity_timeout_seconds": 5}
    ) == timedelta(seconds=60)


# ---------------------------------------------------------------------------
# _run_indexed: Sequenz-Pfad (bytewise M06)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_indexed_sequential_preserves_order_and_calls_progress_monotonic() -> None:
    wf = SecurityCheckWorkflow()
    seen_progress: list[int] = []

    async def factory(i: int) -> int:
        return i * 10

    async def progress_cb(done: int) -> None:
        seen_progress.append(done)

    results = await wf._run_indexed(
        total=5, concurrency=1, factory=factory, progress_cb=progress_cb,
    )

    assert results == [0, 10, 20, 30, 40]
    assert seen_progress == [1, 2, 3, 4, 5], (
        "Im Sequenz-Pfad muss progress_cb strikt monoton 1..N aufgerufen werden"
    )


# ---------------------------------------------------------------------------
# _run_indexed: Parallel-Pfad
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_indexed_parallel_preserves_index_order_in_results() -> None:
    """Auch mit künstlich umgekehrter Latenz bleibt die Ergebnisreihenfolge
    indexgetreu -- entscheidend für die Findings-Sortierung im Report."""
    wf = SecurityCheckWorkflow()

    async def factory(i: int) -> int:
        # Frühere Indizes warten LÄNGER -- ohne indexgetreue Sammlung
        # wäre die Reihenfolge im Result umgekehrt.
        await asyncio.sleep((6 - i) * 0.01)
        return i * 10

    results = await wf._run_indexed(
        total=6, concurrency=4, factory=factory, progress_cb=None,
    )
    assert results == [0, 10, 20, 30, 40, 50]


@pytest.mark.asyncio
async def test_run_indexed_parallel_is_faster_than_sequential() -> None:
    """8 Tasks mit je 80ms Latenz: bei concurrency=4 mindestens 1.6x
    schneller als bei concurrency=1. Konservative Schwelle, um keinen
    flaky-Test zu produzieren."""
    wf = SecurityCheckWorkflow()
    hold = 0.08

    async def factory(_i: int) -> int:
        await asyncio.sleep(hold)
        return 0

    t0 = time.monotonic()
    await wf._run_indexed(total=8, concurrency=1, factory=factory)
    seq = time.monotonic() - t0

    t0 = time.monotonic()
    await wf._run_indexed(total=8, concurrency=4, factory=factory)
    par = time.monotonic() - t0

    assert par * 1.6 < seq, (
        f"Parallel sollte mindestens 1.6x schneller sein: "
        f"seq={seq:.3f}s, par={par:.3f}s"
    )


@pytest.mark.asyncio
async def test_run_indexed_parallel_calls_progress_total_times() -> None:
    """Im Parallel-Pfad ist die Aufruf-Reihenfolge zwar Eintreffe-Reihenfolge,
    aber die Gesamtanzahl der Progress-Aufrufe entspricht total und der
    letzte Wert ist total."""
    wf = SecurityCheckWorkflow()
    seen: list[int] = []

    async def factory(i: int) -> int:
        await asyncio.sleep(0.005)
        return i

    async def progress_cb(done: int) -> None:
        seen.append(done)

    await wf._run_indexed(
        total=10, concurrency=3, factory=factory, progress_cb=progress_cb,
    )

    assert len(seen) == 10
    assert seen[-1] == 10
    assert max(seen) == 10
    assert min(seen) >= 1
