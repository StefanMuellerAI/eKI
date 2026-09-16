"""M09 -- KB TTL cleanup schedule, alert rules and dashboards sanity."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from temporalio.client import ScheduleAlreadyRunningError

from core import metrics
from workflows.maintenance import (
    KB_CLEANUP_SCHEDULE_ID,
    ensure_kb_cleanup_schedule,
    kb_cleanup_expired_activity,
)

ROOT = Path(__file__).resolve().parents[1]
OBS = ROOT / "docker" / "observability"


class TestKbCleanupSchedule:
    @pytest.mark.asyncio
    async def test_creates_schedule_when_missing(self):
        client = MagicMock()
        client.create_schedule = AsyncMock()
        settings = SimpleNamespace(
            kb_cleanup_enabled=True, kb_cleanup_cron="0 3 * * *", temporal_task_queue="q"
        )
        assert await ensure_kb_cleanup_schedule(client, settings) is True
        client.create_schedule.assert_awaited_once()
        args, _ = client.create_schedule.call_args
        assert args[0] == KB_CLEANUP_SCHEDULE_ID
        assert args[1].spec.cron_expressions == ["0 3 * * *"]

    @pytest.mark.asyncio
    async def test_existing_schedule_is_idempotent(self):
        client = MagicMock()
        client.create_schedule = AsyncMock(side_effect=ScheduleAlreadyRunningError())
        settings = SimpleNamespace(
            kb_cleanup_enabled=True, kb_cleanup_cron="0 3 * * *", temporal_task_queue="q"
        )
        assert await ensure_kb_cleanup_schedule(client, settings) is True

    @pytest.mark.asyncio
    async def test_disabled_flag_skips(self):
        client = MagicMock()
        client.create_schedule = AsyncMock()
        settings = SimpleNamespace(kb_cleanup_enabled=False, temporal_task_queue="q")
        assert await ensure_kb_cleanup_schedule(client, settings) is False
        client.create_schedule.assert_not_awaited()


class TestKbCleanupActivity:
    @pytest.mark.asyncio
    async def test_activity_updates_metrics(self):
        fake_kb = MagicMock()
        fake_kb.cleanup_expired = AsyncMock(return_value=3)

        scalar_result = MagicMock()
        scalar_result.scalar_one.return_value = 7
        session = MagicMock()
        session.execute = AsyncMock(return_value=scalar_result)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.dispose = AsyncMock()

        removed_before = metrics.KB_CLEANUP_REMOVED_TOTAL._value.get()

        with (
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=lambda: session),
            patch("services.knowledge_base.KnowledgeBaseService", return_value=fake_kb),
            patch("llm.factory.get_llm_provider", return_value=MagicMock()),
            patch("api.config.get_settings") as get_settings,
        ):
            get_settings.return_value = SimpleNamespace(
                database_url="sqlite+aiosqlite:///:memory:", api_secret_key="k"
            )
            result = await kb_cleanup_expired_activity({})

        assert result == {"removed": 3, "remaining": 7}
        assert metrics.KB_CLEANUP_REMOVED_TOTAL._value.get() == removed_before + 3
        assert metrics.KB_DOCUMENTS._value.get() == 7
        engine.dispose.assert_awaited_once()


class TestObservabilityArtifacts:
    def test_alert_rules_are_well_formed(self):
        data = yaml.safe_load((OBS / "alerts.yml").read_text())
        names = []
        for group in data["groups"]:
            for rule in group["rules"]:
                assert "alert" in rule and "expr" in rule
                assert rule["labels"]["severity"] in {"critical", "warning", "info"}
                assert "summary" in rule["annotations"]
                names.append(rule["alert"])
        for required in (
            "EkiApiDown",
            "EkiWorkerDown",
            "EkiJobDurationSloBreach",
            "EkiDeliveryFailureRateHigh",
            "EkiDeadLettersPending",
            "EkiLlmErrorRateHigh",
        ):
            assert required in names

    def test_alert_expressions_reference_registered_metrics(self):
        from prometheus_client import REGISTRY

        registered = set(REGISTRY._names_to_collectors.keys())
        data = yaml.safe_load((OBS / "alerts.yml").read_text())
        import re

        for group in data["groups"]:
            for rule in group["rules"]:
                for name in re.findall(r"\beki_[a-z_]+", str(rule["expr"])):
                    base = re.sub(r"_(bucket|count|sum|total)$", "", name)
                    assert (
                        base in registered or f"{base}_total" in registered or name in registered
                    ), f"{rule['alert']} references unknown metric {name}"

    def test_prometheus_config_scrapes_api_and_worker(self):
        cfg = yaml.safe_load((OBS / "prometheus.yml").read_text())
        jobs = {j["job_name"]: j for j in cfg["scrape_configs"]}
        assert jobs["eki-api"]["authorization"]["credentials_file"]
        assert jobs["eki-worker"]["static_configs"][0]["targets"] == ["worker:9090"]
        assert "/etc/prometheus/alerts.yml" in cfg["rule_files"]

    def test_dashboards_are_valid_json_with_panels(self):
        files = sorted((OBS / "grafana" / "dashboards").glob("*.json"))
        assert {f.stem for f in files} == {"eki_overview", "eki_delivery", "eki_llm"}
        for f in files:
            dashboard = json.loads(f.read_text())
            assert dashboard["uid"].startswith("eki-")
            assert dashboard["panels"], f"{f.name} has no panels"
            for panel in dashboard["panels"]:
                assert panel["targets"], f"{f.name}: panel {panel['title']} has no targets"

    def test_dashboards_match_generator(self, tmp_path, monkeypatch):
        """Committed JSON must equal what the generator produces."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_dashboards", ROOT / "scripts" / "observability" / "build_dashboards.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        for name, builder in (
            ("eki_overview", module.overview),
            ("eki_delivery", module.delivery),
            ("eki_llm", module.llm),
        ):
            committed = json.loads((OBS / "grafana" / "dashboards" / f"{name}.json").read_text())
            assert committed == builder(), f"{name}.json is stale -- rerun build_dashboards.py"
