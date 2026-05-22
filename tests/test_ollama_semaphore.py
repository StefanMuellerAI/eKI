"""M07 – Tests für den prozessweiten Ollama-Concurrency-Cap.

Verifiziert, dass die in ``llm/ollama.py`` eingeführte modul-globale
Semaphore die parallel laufenden Ollama-Calls strikt deckelt, unabhängig
davon, wie viele Workflows oder Activities gleichzeitig den Provider
benutzen.

Strategie:
* httpx.AsyncClient.post wird gemockt und protokolliert beim Eintritt
  einen Concurrency-Counter, schläft kurz (damit Überlappung möglich
  wäre), zählt beim Verlassen wieder herunter.
* Beobachtetes Maximum wird gegen den konfigurierten Cap geprüft.
* ``_get_throttle_config`` wird direkt monkey-gepatcht statt
  ``get_settings()``, damit die lru_cache-Semantik der Settings nicht
  beeinflusst wird und auch keine echten env vars benötigt werden.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from llm import ollama as ollama_module
from llm.ollama import OllamaProvider


_SIMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"x": {"type": "integer"}},
    "required": ["x"],
}


def _make_provider() -> OllamaProvider:
    return OllamaProvider({
        "base_url": "http://test-ollama:11434",
        "model": "test-model",
        "timeout": 5,
        "think": False,
        "num_ctx": 1024,
    })


def _make_chat_response() -> httpx.Response:
    """Minimal fake /api/chat response with valid JSON content."""
    request = httpx.Request("POST", "http://test-ollama:11434/api/chat")
    return httpx.Response(
        status_code=200,
        json={
            "model": "test-model",
            "message": {"role": "assistant", "content": '{"x": 1}'},
            "done": True,
        },
        request=request,
    )


class _ConcurrencyTracker:
    """Tracks the maximum number of in-flight calls observed."""

    def __init__(self) -> None:
        self.current = 0
        self.max_observed = 0
        self._lock = asyncio.Lock()

    async def enter(self) -> None:
        async with self._lock:
            self.current += 1
            if self.current > self.max_observed:
                self.max_observed = self.current

    async def leave(self) -> None:
        async with self._lock:
            self.current -= 1


def _make_tracking_post(tracker: _ConcurrencyTracker, hold_seconds: float = 0.05):
    """Build a fake httpx.AsyncClient.post that overlaps deliberately."""

    async def _post(*args: Any, **kwargs: Any) -> httpx.Response:
        await tracker.enter()
        try:
            await asyncio.sleep(hold_seconds)
            return _make_chat_response()
        finally:
            await tracker.leave()

    return _post


@pytest.fixture(autouse=True)
def _reset_throttle_state() -> None:
    """Ensure each test starts with a fresh module-global semaphore."""
    ollama_module.reset_ollama_throttle_state_for_testing()
    yield
    ollama_module.reset_ollama_throttle_state_for_testing()


# ---------------------------------------------------------------------------
# Cap = 1: strict serialisation (matches M06 behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_1_serialises_all_calls() -> None:
    """Mit OLLAMA_MAX_CONCURRENT_REQUESTS=1 darf zu keinem Zeitpunkt mehr
    als ein Ollama-Call gleichzeitig laufen, auch wenn 10 Provider-Tasks
    parallel gestartet werden."""
    tracker = _ConcurrencyTracker()

    with patch.object(ollama_module, "_get_throttle_config", return_value=(1, 0)):
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_make_tracking_post(tracker))):
            provider = _make_provider()
            tasks = [
                provider.generate_structured(
                    prompt=f"Prompt {i}",
                    schema=_SIMPLE_SCHEMA,
                    temperature=0.1,
                )
                for i in range(10)
            ]
            results = await asyncio.gather(*tasks)

    assert len(results) == 10
    assert tracker.max_observed == 1, (
        f"Cap=1 verletzt: maximal {tracker.max_observed} gleichzeitige Calls"
    )


# ---------------------------------------------------------------------------
# Cap = 2: parallel allowed, hard limit enforced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_2_allows_two_but_never_more() -> None:
    """Mit Cap=2 dürfen genau zwei Calls parallel laufen, mehr nicht.

    Mit 10 parallel gestarteten Tasks und einem 50ms-Hold pro Mock-Call
    überlappen die Tasks zwingend, sodass ``max_observed`` exakt 2
    erreichen muss – wenn es < 2 wäre, würde der Test als Diagnose-
    Signal darauf hinweisen, dass die Tasks gar nicht überlappen.
    """
    tracker = _ConcurrencyTracker()

    with patch.object(ollama_module, "_get_throttle_config", return_value=(2, 0)):
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_make_tracking_post(tracker))):
            provider = _make_provider()
            tasks = [
                provider.generate_structured(
                    prompt=f"Prompt {i}",
                    schema=_SIMPLE_SCHEMA,
                    temperature=0.1,
                )
                for i in range(10)
            ]
            await asyncio.gather(*tasks)

    assert tracker.max_observed == 2, (
        f"Cap=2 erwartet (max 2 gleichzeitig), gemessen: {tracker.max_observed}"
    )


# ---------------------------------------------------------------------------
# Cap = 4: scales up cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_4_allows_four() -> None:
    """Cap=4 erlaubt vier gleichzeitige Calls, nicht mehr."""
    tracker = _ConcurrencyTracker()

    with patch.object(ollama_module, "_get_throttle_config", return_value=(4, 0)):
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_make_tracking_post(tracker))):
            provider = _make_provider()
            tasks = [
                provider.generate_structured(
                    prompt=f"Prompt {i}",
                    schema=_SIMPLE_SCHEMA,
                    temperature=0.1,
                )
                for i in range(12)
            ]
            await asyncio.gather(*tasks)

    assert tracker.max_observed == 4, (
        f"Cap=4 erwartet, gemessen: {tracker.max_observed}"
    )


# ---------------------------------------------------------------------------
# Throttle (Min-Intervall) erzwingt Mindestabstand zwischen Calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_interval_enforces_gap_between_calls() -> None:
    """Mit ``ollama_min_interval_ms=50`` dauern 5 sequenzielle Calls
    (Cap=1) mindestens 4 * 50 ms = 200 ms, weil der Throttle nach jedem
    Call den nächsten verzögert."""
    tracker = _ConcurrencyTracker()

    with patch.object(ollama_module, "_get_throttle_config", return_value=(1, 50)):
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_make_tracking_post(tracker, hold_seconds=0.0))):
            provider = _make_provider()
            t0 = asyncio.get_event_loop().time()
            for _ in range(5):
                await provider.generate_structured(
                    prompt="P",
                    schema=_SIMPLE_SCHEMA,
                    temperature=0.1,
                )
            elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000

    assert elapsed_ms >= 200, (
        f"Throttle griff nicht: 5 Calls in {elapsed_ms:.0f}ms (erwartet >= 200ms)"
    )
    assert tracker.max_observed == 1


# ---------------------------------------------------------------------------
# Semaphore wird re-allokiert, wenn sich der Cap zur Laufzeit ändert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semaphore_reallocates_when_capacity_changes() -> None:
    """Erst Cap=1 nutzen, dann auf Cap=3 hochschalten und prüfen, dass
    der neue Wert tatsächlich greift. Sichert, dass Live-Konfig-Wechsel
    (z.B. in Tests oder via Reload) sauber landen."""
    tracker_a = _ConcurrencyTracker()
    tracker_b = _ConcurrencyTracker()

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_make_tracking_post(tracker_a))):
        with patch.object(ollama_module, "_get_throttle_config", return_value=(1, 0)):
            provider = _make_provider()
            await asyncio.gather(*[
                provider.generate_structured(
                    prompt="P", schema=_SIMPLE_SCHEMA, temperature=0.1,
                )
                for _ in range(4)
            ])

    assert tracker_a.max_observed == 1

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_make_tracking_post(tracker_b))):
        with patch.object(ollama_module, "_get_throttle_config", return_value=(3, 0)):
            provider = _make_provider()
            await asyncio.gather(*[
                provider.generate_structured(
                    prompt="P", schema=_SIMPLE_SCHEMA, temperature=0.1,
                )
                for _ in range(8)
            ])

    assert tracker_b.max_observed == 3, (
        f"Re-Allokation fehlerhaft: nach Cap=3 max_observed={tracker_b.max_observed}"
    )
