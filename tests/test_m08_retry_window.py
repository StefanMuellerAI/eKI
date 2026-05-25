"""M08 -- Retry-Window-Konfigurationstests.

Pflichtenheft Abnahmetest 4 verlangt, dass bei Fehlern bis zu 6 Stunden
automatisch erneut zugestellt wird; danach Auto-Loeschung und Webhook.
Diese Tests pruefen die Workflow-seitige Konfiguration ohne Temporal-
Runtime: sie verifizieren rein die Konstanten und die Aufruf-Struktur.
"""

import inspect
from datetime import timedelta

import workflows.security_check as sc


class TestRetryConstants:
    def test_retry_delivery_has_no_maximum_attempts(self):
        """M08: Begrenzung erfolgt ueber schedule_to_close_timeout (6h),
        NICHT mehr ueber maximum_attempts. Damit kann Temporal innerhalb
        des Fensters beliebig oft retrien."""
        policy = sc._RETRY_DELIVERY
        assert policy.maximum_attempts in (0, None), (
            "RetryPolicy.maximum_attempts darf nicht gesetzt sein -- "
            "sonst greift das 6h-Window nicht."
        )

    def test_retry_delivery_uses_exponential_backoff(self):
        policy = sc._RETRY_DELIVERY
        assert policy.backoff_coefficient >= 2.0
        assert policy.maximum_interval >= timedelta(minutes=5)
        assert policy.initial_interval <= timedelta(seconds=5)

    def test_schedule_to_close_is_six_hours(self):
        """Pflichtenheft Abnahmetest 4: ``Bei Fehlern wird bis zu 6
        Stunden automatisch erneut zugestellt``."""
        assert sc._DELIVERY_SCHEDULE_TO_CLOSE == timedelta(hours=6)

    def test_per_attempt_timeout_reasonable(self):
        """Ein einzelner Push-Versuch darf nicht die ganzen 6h blocken."""
        per_attempt = sc._DELIVERY_START_TO_CLOSE_PER_ATTEMPT
        assert per_attempt <= timedelta(minutes=10)
        assert per_attempt >= timedelta(minutes=1)


class TestWorkflowFailureBranchWiring:
    def test_handle_delivery_failure_method_exists(self):
        """Sicherheitsnetz: Refactor darf den Failure-Branch nicht
        versehentlich entfernen."""
        cls = sc.SecurityCheckWorkflow
        assert hasattr(cls, "_handle_delivery_failure")
        sig = inspect.signature(cls._handle_delivery_failure)
        # Muss mindestens die folgenden Parameter haben (alle keyword-only
        # nach self in der aktuellen Implementierung):
        params = sig.parameters
        expected = {
            "job_id", "report_id", "report_ref_key", "reason",
            "attempts", "workflow_id", "total_findings",
        }
        assert expected.issubset(set(params.keys())), (
            f"Erwartete Parameter fehlen: {expected - set(params.keys())}"
        )

    def test_report_and_deliver_uses_schedule_to_close(self):
        """Statische Code-Pruefung: das 6h-Fenster muss im
        _report_and_deliver-Aufruf der deliver-Activity verwendet werden."""
        src = inspect.getsource(sc.SecurityCheckWorkflow._report_and_deliver)
        assert "_DELIVERY_SCHEDULE_TO_CLOSE" in src
        assert "schedule_to_close_timeout" in src
